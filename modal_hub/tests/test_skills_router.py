"""modal_hub/tests/test_skills_router.py — POST /api/skills/publish の HTTP 契約。

07_Phase1b_Spec.md §5 の検証順序・create-or-match-only セマンティクス・
publish スコープ強制を、`_LIVE_STORE` を fake に差し替えた実装本体
（`security.py`/`redact.py`/`skill_quarantine.py`/`audit.py` は本物）で
端から端まで検証する。
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modal_hub.core import security
from modal_hub.routers import skills
from modal_hub.tests.conftest import TEST_AGENT_SIGNING_KEY, WS_ID, FakeStore

AGENT_KEY = TEST_AGENT_SIGNING_KEY.encode("utf-8")


def _skill_md(name: str, description: str = "a test skill", body: str = "Body text.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def store(monkeypatch, secret_env) -> FakeStore:
    s = FakeStore()
    monkeypatch.setattr(skills, "_LIVE_STORE", s)
    return s


@pytest.fixture()
def client(store) -> TestClient:
    app = FastAPI()
    app.include_router(skills.router)
    return TestClient(app)


def token_with_scopes(store: FakeStore, scopes) -> str:
    return security.issue_agent_token(
        store,
        sub="claude_code:desktop-haruki",
        source="claude_code",
        session_id="sess-1",
        workspace_id=WS_ID,
        signing_key=AGENT_KEY,
        scopes=scopes,
    )


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def publish_body(name: str = "alpha-skill", **overrides) -> dict:
    skill_md = overrides.pop("skill_md", _skill_md(name))
    body = {
        "name": name,
        "skill_md": skill_md,
        "content_sha256": _sha256(skill_md),
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 認証・スコープ
# ---------------------------------------------------------------------------


def test_missing_bearer_token_is_401(client):
    resp = client.post("/api/skills/publish", json=publish_body())
    assert resp.status_code == 401


def test_legacy_token_without_publish_scope_is_403(store, client):
    """scopes を省略して発行した(＝ Phase1a 互換のレガシーデフォルト)
    トークンは publish を含まないため 403。"""
    token = security.issue_agent_token(
        store,
        sub="claude_code:desktop-haruki",
        source="claude_code",
        session_id="sess-1",
        workspace_id=WS_ID,
        signing_key=AGENT_KEY,
    )
    resp = client.post("/api/skills/publish", json=publish_body(), headers=auth(token))
    assert resp.status_code == 403


def test_token_with_publish_scope_succeeds(store, client):
    token = token_with_scopes(store, ["publish"])
    resp = client.post("/api/skills/publish", json=publish_body(), headers=auth(token))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "unchanged": False}


def test_invalid_token_is_401(client):
    resp = client.post("/api/skills/publish", json=publish_body(), headers=auth("garbage"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 検証順序(§5): 1 name regex → 2 frontmatter match → 3 sha256 match → 4 redaction
# ---------------------------------------------------------------------------


def test_invalid_name_is_400(store, client):
    token = token_with_scopes(store, ["publish"])
    body = publish_body(name="Invalid Name")
    resp = client.post("/api/skills/publish", json=body, headers=auth(token))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_frontmatter_name_mismatch_is_400(store, client):
    token = token_with_scopes(store, ["publish"])
    skill_md = _skill_md("other-name")
    body = publish_body(name="alpha-skill", skill_md=skill_md, content_sha256=_sha256(skill_md))
    resp = client.post("/api/skills/publish", json=body, headers=auth(token))
    assert resp.status_code == 400


def test_content_sha256_mismatch_is_400(store, client):
    token = token_with_scopes(store, ["publish"])
    body = publish_body(content_sha256="a" * 64)
    resp = client.post("/api/skills/publish", json=body, headers=auth(token))
    assert resp.status_code == 400


def test_content_sha256_non_hex_is_400(store, client):
    token = token_with_scopes(store, ["publish"])
    body = publish_body(content_sha256="not-hex")
    resp = client.post("/api/skills/publish", json=body, headers=auth(token))
    assert resp.status_code == 400


def test_redaction_triggers_rejection_and_audit_event(store, client):
    token = token_with_scopes(store, ["publish"])
    skill_md = _skill_md("secret-skill", body="token: abcdefgh12345678")
    body = publish_body(name="secret-skill", skill_md=skill_md, content_sha256=_sha256(skill_md))

    resp = client.post("/api/skills/publish", json=body, headers=auth(token))

    assert resp.status_code == 400
    assert store.raw_files == {}  # 何も保存されていない
    # outbox_consume が成功した後は outbox から消え、files(Volume 相当)へ
    # 移る(audit.py の outbox パターン。成功時は outbox に残らない)。
    assert any(
        rec.get("event") == "skill_publish_rejected" for rec in store.files.values()
    )


def test_name_and_frontmatter_and_digest_rejections_are_all_audited(store, client):
    """2026-08-11 Codex レビュー Medium 指摘: redaction/conflict 以外の
    拒否（frontmatter 不一致・digest 不一致）も監査されること。"""
    token = token_with_scopes(store, ["publish"])

    mismatched_md = _skill_md("other-name")
    resp1 = client.post(
        "/api/skills/publish",
        json=publish_body(name="rho-skill", skill_md=mismatched_md, content_sha256=_sha256(mismatched_md)),
        headers=auth(token),
    )
    assert resp1.status_code == 400

    resp2 = client.post(
        "/api/skills/publish",
        json=publish_body(name="sigma-skill", content_sha256="a" * 64),
        headers=auth(token),
    )
    assert resp2.status_code == 400

    rejected = [rec for rec in store.files.values() if rec.get("event") == "skill_publish_rejected"]
    assert len(rejected) == 2


# ---------------------------------------------------------------------------
# create-or-match-only（§5 書き込みセマンティクス）
# ---------------------------------------------------------------------------


def test_publish_race_is_closed_by_atomic_reservation(store, client):
    """2026-08-11 Codex レビュー Critical 指摘の回帰テスト: read-file-then-
    write-file の check-then-write ではなく `put_if_absent` による予約が
    実際に「最初の書き手」を1人だけに絞ることを確認する。

    `FakeStore.put_if_absent` は本物の `modal.Dict` と同じ write-once
    意味論（既存キーなら False）を実装しているため、この予約ロックを
    介さずに旧来の read-then-write を行っていれば、同時に「無い」を観測
    した2件が両方 write してしまう——このテストは新しい実装がロックの
    勝者だけを書き込ませることを検証する。
    """
    token = token_with_scopes(store, ["publish"])
    first_md = _skill_md("upsilon-skill", body="version A")
    second_md = _skill_md("upsilon-skill", body="version B")

    r1 = client.post(
        "/api/skills/publish",
        json=publish_body(name="upsilon-skill", skill_md=first_md, content_sha256=_sha256(first_md)),
        headers=auth(token),
    )
    r2 = client.post(
        "/api/skills/publish",
        json=publish_body(name="upsilon-skill", skill_md=second_md, content_sha256=_sha256(second_md)),
        headers=auth(token),
    )

    assert r1.status_code == 200 and r1.json()["unchanged"] is False
    assert r2.status_code == 409
    # 最初の書き手の内容だけが残っている(2人目に上書きされていない)。
    assert store.raw_files["skills_quarantine/upsilon-skill/SKILL.md"] == first_md.encode("utf-8")


def test_new_skill_is_stored(store, client):
    token = token_with_scopes(store, ["publish"])
    skill_md = _skill_md("beta-skill")
    resp = client.post(
        "/api/skills/publish",
        json=publish_body(name="beta-skill", skill_md=skill_md, content_sha256=_sha256(skill_md)),
        headers=auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["unchanged"] is False
    assert store.raw_files["skills_quarantine/beta-skill/SKILL.md"] == skill_md.encode("utf-8")
    assert any(rec.get("event") == "skill_published" for rec in store.files.values())


def test_resubmitting_identical_content_is_idempotent(store, client):
    token = token_with_scopes(store, ["publish"])
    skill_md = _skill_md("gamma-skill")
    body = publish_body(name="gamma-skill", skill_md=skill_md, content_sha256=_sha256(skill_md))

    first = client.post("/api/skills/publish", json=body, headers=auth(token))
    second = client.post("/api/skills/publish", json=body, headers=auth(token))

    assert first.status_code == 200 and first.json()["unchanged"] is False
    assert second.status_code == 200 and second.json()["unchanged"] is True


def test_conflicting_content_for_same_name_is_409(store, client):
    token = token_with_scopes(store, ["publish"])
    first_md = _skill_md("delta-skill", body="version one")
    second_md = _skill_md("delta-skill", body="version two")

    first = client.post(
        "/api/skills/publish",
        json=publish_body(name="delta-skill", skill_md=first_md, content_sha256=_sha256(first_md)),
        headers=auth(token),
    )
    second = client.post(
        "/api/skills/publish",
        json=publish_body(name="delta-skill", skill_md=second_md, content_sha256=_sha256(second_md)),
        headers=auth(token),
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "SKILL_ALREADY_PUBLISHED_WITH_DIFFERENT_CONTENT"
    # 既存の内容は上書きされていない。
    assert store.raw_files["skills_quarantine/delta-skill/SKILL.md"] == first_md.encode("utf-8")


# ---------------------------------------------------------------------------
# リクエストの形状
# ---------------------------------------------------------------------------


def test_malformed_json_body_is_400(store, client):
    token = token_with_scopes(store, ["publish"])
    resp = client.post(
        "/api/skills/publish",
        content=b"not json",
        headers={**auth(token), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_oversized_body_is_413(store, client):
    token = token_with_scopes(store, ["publish"])
    skill_md = _skill_md("huge-skill", body="x" * (skills.MAX_BODY_BYTES + 1))
    resp = client.post(
        "/api/skills/publish",
        json=publish_body(name="huge-skill", skill_md=skill_md, content_sha256=_sha256(skill_md)),
        headers=auth(token),
    )
    assert resp.status_code == 413


def test_missing_field_is_400(store, client):
    token = token_with_scopes(store, ["publish"])
    body = publish_body()
    del body["content_sha256"]
    resp = client.post("/api/skills/publish", json=body, headers=auth(token))
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# レート制限
# ---------------------------------------------------------------------------


def test_rate_limit_exceeded_returns_429(store, client):
    token = token_with_scopes(store, ["publish"])
    for i in range(skills.PUBLISH_RATE_LIMIT):
        skill_md = _skill_md(f"rl-skill-{i}")
        resp = client.post(
            "/api/skills/publish",
            json=publish_body(name=f"rl-skill-{i}", skill_md=skill_md, content_sha256=_sha256(skill_md)),
            headers=auth(token),
        )
        assert resp.status_code == 200, resp.text

    over_limit_md = _skill_md("rl-skill-over")
    resp = client.post(
        "/api/skills/publish",
        json=publish_body(name="rl-skill-over", skill_md=over_limit_md, content_sha256=_sha256(over_limit_md)),
        headers=auth(token),
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
