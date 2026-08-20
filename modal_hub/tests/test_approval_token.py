"""issue_approval_agent_token Modal Function のテスト — `.agentic_os_issue_approval_token_task.md`。

タスクの完了報告要件:
    - `issue_approval_agent_token` が発行したトークンで `POST /api/approval/request`
      の認証（`_verify_agent` ＋ `_require_scope(identity, "request")`）を
      実際に通過する（④の HTTP 統合テスト）。
    - `agent_session:<tid>` 肯定リストレコードがストアへ書き込まれること
      （`issue_agent_token` → `verify_agent_token` の往復）。

方針（test_dispatch_token.py / test_approval_gate_http.py と同じ）:
    - Modal には触れない。ストアだけを conftest の FakeStore に差し替え、
      署名・検証は実装の本物を通す。
    - 署名鍵は conftest の `TEST_AGENT_SIGNING_KEY`（＝ `secret_env` が
      `HH_AGENT_TOKEN_SIGNING_KEY` に設定する値。本物の Secret ではない）。
      `issue_approval_agent_token` の発行鍵（`config.agent_token_signing_key()`）
      と `approval_gate._verify_agent` の検証鍵はどちらもこの env を経由する
      ため、「発行したトークンで認証を通過する」が成り立つ。
    - Modal Function のラッパー（`@app.function()` デコレータ付き）自体も
      ローカル呼び出しで 1 本通す（Modal SDK のローカル実行経路）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modal_hub.approval_token import (
    APPROVAL_SCOPES,
    WORKSPACE_AGENTIC_OS,
    _issue_approval_agent_token_core,
)
from modal_hub.core import config as hub_config
from modal_hub.core import security
from modal_hub.core import store as store_keys
from modal_hub.routers import approval_gate as gate
from modal_hub.tests.conftest import (
    HEAD_REV,
    SHA_PAYLOAD,
    SHA_PAYLOAD_RAW,
    TEST_AGENT_SIGNING_KEY,
    WS_ID,
    FakeStore,
)

ORIGIN = "https://testserver"

# `secret_env` が HH_AGENT_TOKEN_SIGNING_KEY に設定するテスト鍵（本物の
# Secret ではない。approval_gate._verify_agent と同じ鍵を使う）。
AGENT_KEY = TEST_AGENT_SIGNING_KEY.encode("utf-8")

IDEM = "idem-" + "k" * 20


# ---------------------------------------------------------------------------
# フィクスチャ（test_dispatch_token.py / test_approval_gate_http.py と同型）
# ---------------------------------------------------------------------------


@pytest.fixture()
def approval_env(monkeypatch, secret_env):
    """`secret_env`（必須キー一式、HH_AGENT_TOKEN_SIGNING_KEY 含む）を使う。

    追加の環境変数は不要 — 本関数の署名鍵は標準の
    `HH_AGENT_TOKEN_SIGNING_KEY` だから（dispatch_token と違い新規鍵は無い）。
    """
    return hub_config


@pytest.fixture()
def store(monkeypatch, approval_env) -> FakeStore:
    """1 つの FakeStore を発行側と検証側の両方へ束ねる。

    トークン発行（`_issue_approval_agent_token_core` に渡す）と
    `POST /api/approval/request` の検証（`gate._LIVE_STORE`）が**同じ**ストアを
    共有して初めて「発行したトークンで認証を通過する」ことを実証できる
    （肯定リストはストア内の `agent_session:<tid>` だから）。
    """
    s = FakeStore()
    monkeypatch.setattr(gate, "_LIVE_STORE", s)
    return s


@pytest.fixture()
def notify_sent(monkeypatch, store):
    """ntfy をモック（test_approval_gate_http.py の notify_sent と同型）。

    本物の `services/notifier.py` は実 ntfy への HTTP 送信を行うため、テスト
    中にネットワークへ出ないよう差し替える。write-once で `notify:<id>` を
    書く副作用は実装と同じにし、poll 側の `notify_state` まで実コードで
    検証できるようにする。
    """
    from modal_hub.services import notifier

    calls: list[tuple[str, str]] = []

    def fake_send(approval_id: str, risk: str) -> str:
        calls.append((approval_id, risk))
        existing = store.get(store_keys.notify_key(approval_id))
        if isinstance(existing, dict) and existing.get("state") == "sent":
            return "sent"
        store.put_if_absent(
            store_keys.notify_key(approval_id), {"state": "sent", "attempts": 1}
        )
        return "sent"

    monkeypatch.setattr(notifier, "send_approval_request", fake_send)
    return fake_send, calls


@pytest.fixture()
def client(store, notify_sent) -> TestClient:
    app = FastAPI()
    app.include_router(gate.router)
    return TestClient(app, base_url=ORIGIN)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def request_body(**overrides) -> dict:
    """`POST /api/approval/request` の正常系ボディ（test_approval_gate_http.py
    と同一形）。"""
    body = {
        "idempotency_key": IDEM,
        "tool_name": "Bash",
        "payload": {"command": "git push --force origin main"},
        "payload_sha256": SHA_PAYLOAD,
        "payload_raw_sha256": SHA_PAYLOAD_RAW,
        "context": {
            "cwd": "C:/Users/Haruki/Projects/Foo",
            "workspace_id": WS_ID,
            "base_revision": HEAD_REV,
        },
        "risk": "HIGH",
        "rule_id": "force_push",
        "reason_code": "force_push",
        "targets": [],
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# ① 発行 → 検証の往復（ユニット）
# ---------------------------------------------------------------------------


def test_issue_approval_agent_token_round_trips_through_verify(store) -> None:
    """`issue_agent_token` → `verify_agent_token` の往復（タスク要件②）。"""
    token = _issue_approval_agent_token_core(
        store, sub="agentic_os_hub", session_id="sess-os-1"
    )
    assert token.startswith(security.TOKEN_PREFIX + ".")

    identity = security.verify_agent_token(
        store, token, signing_key=AGENT_KEY
    )
    assert identity.sub == "agentic_os_hub"
    assert identity.session_id == "sess-os-1"
    assert identity.source == security.SOURCE_CLAUDE_CODE
    assert identity.workspace_id == WORKSPACE_AGENTIC_OS
    assert identity.scopes == frozenset(APPROVAL_SCOPES)
    assert identity.has_scope("request")
    assert identity.has_scope("poll")
    # 最小権限: 承認フローの残り（claim/complete）や publish/dispatch は付与しない
    assert not identity.has_scope("claim")
    assert not identity.has_scope("complete")
    assert not identity.has_scope(security.SCOPE_PUBLISH)
    assert not identity.has_scope(security.SCOPE_DISPATCH)


def test_issue_approval_agent_token_writes_allowlist_record(store) -> None:
    """`agent_session:<tid>` 肯定リストレコードが実際にストアへ書かれる。"""
    token = _issue_approval_agent_token_core(
        store, sub="agentic_os_hub", session_id="sess-os-2"
    )
    tid = security.verify_agent_token(
        store, token, signing_key=AGENT_KEY
    ).tid

    record = store.get(store_keys.agent_session_key(tid))
    assert record is not None
    assert record["sub"] == "agentic_os_hub"
    assert record["session_id"] == "sess-os-2"
    assert record["source"] == security.SOURCE_CLAUDE_CODE
    assert record["workspace_id"] == WORKSPACE_AGENTIC_OS
    # issue_agent_token は scopes を sorted(set(...)) で正規化して記録する
    assert record["scopes"] == sorted(APPROVAL_SCOPES)
    assert isinstance(record["exp"], int) and record["exp"] > record["issued_at"]


def test_issue_approval_agent_token_fails_closed_without_key(
    monkeypatch, secret_env
) -> None:
    """HH_AGENT_TOKEN_SIGNING_KEY 未設定では発行せずエラー（fail-closed）。"""
    monkeypatch.delenv(hub_config.HH_AGENT_TOKEN_SIGNING_KEY, raising=False)
    with pytest.raises(security.SecurityError):
        _issue_approval_agent_token_core(
            FakeStore(), sub="agentic_os_hub", session_id="sess-os-3"
        )


def test_issue_approval_agent_token_rejects_empty_sub(store) -> None:
    """空の sub / session_id は issue_agent_token のバリデーションで拒否される。"""
    with pytest.raises(ValueError):
        _issue_approval_agent_token_core(store, sub="", session_id="sess-os-4")
    with pytest.raises(ValueError):
        _issue_approval_agent_token_core(store, sub="agentic_os_hub", session_id="")


# ---------------------------------------------------------------------------
# ② Modal Function ラッパー（@app.function() 付き）のローカル呼び出し
# ---------------------------------------------------------------------------


def test_modal_function_wrapper_issues_token_locally(
    monkeypatch, approval_env
) -> None:
    """デコレータ付きの実物 `issue_approval_agent_token` をローカル実行できる。

    Modal の `Function` オブジェクトは直接呼び出せないため（`__call__` なし）、
    `Function.local()`（modal/_functions.py のローカル実行経路）で呼ぶ。これは
    コンテナへは出ず生の関数本体を同一プロセスで実行するだけで、本番の
    `.remote()` と同一の配線（@app.function() が包んだ関数本体）を通る。
    """
    from modal_hub import approval_token

    s = FakeStore()
    monkeypatch.setattr(approval_token, "_LIVE_STORE", s)

    token = approval_token.issue_approval_agent_token.local(
        "agentic_os_hub", "sess-os-5"
    )
    assert token.startswith(security.TOKEN_PREFIX + ".")

    identity = security.verify_agent_token(
        s, token, signing_key=AGENT_KEY
    )
    assert identity.sub == "agentic_os_hub"
    assert identity.session_id == "sess-os-5"
    assert identity.has_scope("request")
    assert identity.has_scope("poll")


# ---------------------------------------------------------------------------
# ③ main.py 経由で app へ登録される（@app.function() の配線）
# ---------------------------------------------------------------------------


def test_issue_approval_agent_token_registered_on_hub_app() -> None:
    """`modal deploy modal_hub.main` の際に app へ登録されることを確認する。

    `modal.Function.from_name("hh-agent-hub", "issue_approval_agent_token")`
    が解決できる前提は「main.py の末尾 import が @app.function() を実行する」
    こと。ここでは main を import した後、`app` の登録済み関数名に
    `issue_approval_agent_token` が含まれることを直接確認する（Modal へは
    接続しない。from_name の解決自体はデプロイ後に Modal API 経由で行われる）。
    """
    import modal_hub.main  # noqa: F401  （末尾 import が function を登録する）

    from modal_hub.main import app as hub_app

    names = list(hub_app.registered_functions)
    assert "issue_approval_agent_token" in names
    assert "issue_dispatch_token" in names


# ---------------------------------------------------------------------------
# ④ 発行したトークンで /api/approval/request の認証を通過する（HTTP 統合）
# ---------------------------------------------------------------------------


def test_issued_token_passes_approval_request_authentication(client, store) -> None:
    """`issue_approval_agent_token` が発行したトークンで認証を実際に通過する。

    `_verify_agent`（Bearer → verify_agent_token）と
    `_require_scope(identity, "request")` を本物のまま通し、201 で返ることを
    確認する。発行鍵（`config.agent_token_signing_key()` ＝
    `HH_AGENT_TOKEN_SIGNING_KEY`）は `_verify_agent` が要求する鍵と同一経路で
    解決される — これが「通過する」ことの証明の核心。
    """
    token = _issue_approval_agent_token_core(
        store, sub="agentic_os_hub", session_id="sess-os-6"
    )

    resp = client.post(
        "/api/approval/request", json=request_body(), headers=auth(token)
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reused"] is False
    assert body["notify_state"] == "sent"
    approval_id = body["approval_id"]

    # 同じトークンが /api/approval/poll の _require_scope(identity, "poll") も
    # 通過する（scopes=["request","poll"] の両面を確認）。
    poll_resp = client.get(
        f"/api/approval/poll?id={approval_id}", headers=auth(token)
    )
    assert poll_resp.status_code == 200, poll_resp.text
    assert poll_resp.json()["status"] == "pending"
    assert poll_resp.json()["notify_state"] == "sent"
