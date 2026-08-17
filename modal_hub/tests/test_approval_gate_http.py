"""承認ゲートの HTTP 契約 — Phase1a spec §1（API）・§2（状態遷移表）・
§2.1（エラー判定の優先順位）を端から端まで検証する。

`_LIVE_STORE` だけをインメモリの fake に差し替え、認証・状態遷移・write-once
競合・監査はすべて**実装の本物**を通す。Modal には接続しない。

§2 の表に無い (状態, イベント) の組は存在しない。表の各行に対応する
テストが 1 つ以上あることを意識して並べている。
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modal_hub.core import security, store as store_keys
from modal_hub.routers import approval_gate as gate
from modal_hub.tests.conftest import (
    HEAD_REV,
    SHA_PAYLOAD,
    SHA_PAYLOAD_RAW,
    TEST_AGENT_SIGNING_KEY,
    TEST_PWA_SESSION_KEY,
    WS_ID,
    WS_ID_OTHER,
    FakeStore,
    make_target,
)

ORIGIN = "https://testserver"
AGENT_KEY = TEST_AGENT_SIGNING_KEY.encode("utf-8")
PWA_KEY = TEST_PWA_SESSION_KEY.encode("utf-8")

IDEM = "idem-" + "k" * 20


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(monkeypatch, secret_env) -> FakeStore:
    s = FakeStore()
    monkeypatch.setattr(gate, "_LIVE_STORE", s)
    return s


@pytest.fixture()
def notify_sent(monkeypatch, store):
    """ntfy をモック。

    本物の `services/notifier.py` は送信結果を `notify:<id>` へ
    `put_if_absent`（write-once）で書き、同じ文字列を戻り値にする。
    `poll` はストア側の値を読むため、**両方を再現しないと** poll の
    `notify_state` が検証できない。ここでは実装と同じ 2 つの副作用を持たせる。
    """
    from modal_hub.services import notifier

    calls: list[tuple[str, str]] = []

    def fake_send(approval_id: str, risk: str) -> str:
        calls.append((approval_id, risk))
        existing = store.get(store_keys.notify_key(approval_id))
        if isinstance(existing, dict) and existing.get("state") == "sent":
            return "sent"
        store.put_if_absent(
            store_keys.notify_key(approval_id), {"state": fake_send.state, "attempts": 1}
        )
        return fake_send.state

    fake_send.state = "sent"
    monkeypatch.setattr(notifier, "send_approval_request", fake_send)
    return fake_send, calls


@pytest.fixture()
def client(store, notify_sent) -> TestClient:
    app = FastAPI()
    app.include_router(gate.router)
    return TestClient(app, base_url=ORIGIN)


def agent_token(
    store: FakeStore,
    *,
    sub: str = "claude_code:desktop-haruki",
    session_id: str = "sess-1",
    workspace_id: str = WS_ID,
) -> str:
    return security.issue_agent_token(
        store,
        sub=sub,
        source="claude_code",
        session_id=session_id,
        workspace_id=workspace_id,
        signing_key=AGENT_KEY,
    )


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def request_body(**overrides) -> dict:
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


def verification(**overrides) -> dict:
    ver = {
        "payload_sha256": SHA_PAYLOAD,
        "payload_raw_sha256": SHA_PAYLOAD_RAW,
        "context": {
            "cwd": "C:/Users/Haruki/Projects/Foo",
            "workspace_id": WS_ID,
            "base_revision": HEAD_REV,
        },
        "targets": [],
    }
    ver.update(overrides)
    return ver


def create_approval(client: TestClient, token: str, **overrides) -> str:
    resp = client.post("/api/approval/request", json=request_body(**overrides), headers=auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["approval_id"]


def pair_pwa(client: TestClient) -> str:
    """PWA をペアリング（初回のみ）して CSRF トークンを返す。

    既にペアリング済み（Cookie 保持済み）なら `/api/approval/pending` の
    応答に含まれる `csrf_token` を使う（§7.3: CSRF は Cookie とは別経路で
    配られ、`pending` のたびに更新される）。
    """
    from modal_hub.tests.conftest import TEST_PAIRING_CODE

    existing = client.get("/api/approval/pending")
    if existing.status_code == 200:
        return existing.json()["csrf_token"]

    resp = client.post("/api/pwa/pair", json={"code": TEST_PAIRING_CODE, "device_name": "iPhone"})
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


def respond(client: TestClient, approval_id: str, decision: str, csrf: str, origin: str = ORIGIN):
    return client.post(
        "/api/approval/respond",
        json={"approval_id": approval_id, "decision": decision, "csrf": csrf},
        headers={"Origin": origin},
    )


def err(resp) -> dict:
    return resp.json()["error"]


# ===========================================================================
# 共通規約（§1.1）
# ===========================================================================


def test_error_envelope_shape(client, store) -> None:
    resp = client.get("/api/approval/poll?id=x")
    assert resp.status_code == 401
    body = err(resp)
    assert set(body) == {"code", "message", "retryable"}
    assert body["code"].isupper()
    assert isinstance(body["retryable"], bool)


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer "}, {"Authorization": "Basic xyz"}, {"Authorization": "Bearer garbage"}],
)
def test_missing_or_bad_bearer_is_401_not_retryable(client, store, headers) -> None:
    resp = client.post("/api/approval/request", json=request_body(), headers=headers)
    assert resp.status_code == 401
    assert err(resp)["retryable"] is False


def _publish_only_token(store: FakeStore) -> str:
    """distill_token.json 相当（`scopes=["publish"]` のみ）のトークン。"""
    return security.issue_agent_token(
        store,
        sub="claude_code:distill-worker",
        source="claude_code",
        session_id="sess-distill",
        workspace_id=WS_ID,
        signing_key=AGENT_KEY,
        scopes=["publish"],
    )


@pytest.mark.parametrize(
    "method,path,body_factory",
    [
        ("post", "/api/approval/request", lambda: request_body()),
        ("get", "/api/approval/poll?id=x", None),
        ("post", "/api/approval/claim", lambda: {"approval_id": "x", "claim_attempt_id": "y"}),
        ("post", "/api/approval/complete", lambda: {"approval_id": "x", "lease_id": "y"}),
    ],
)
def test_publish_only_token_is_403_on_approval_flow_endpoints(
    client, store, method, path, body_factory
) -> None:
    """distill_token.json 由来の `publish` 専用トークンは承認フロー
    （request/poll/claim/complete）を一切操作できないこと（least privilege）。

    2026-08-11・Codex 指摘: `_verify_agent` はトークンの真正性のみ確認し
    スコープを見ていなかったため、修正前はこのテストが 403 でなく
    200/201/404 等（＝スコープ無視で処理が進んでしまう）になっていた。
    """
    token = _publish_only_token(store)
    kwargs = {"headers": auth(token)}
    if body_factory is not None:
        kwargs["json"] = body_factory()
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 403
    assert err(resp)["retryable"] is False


def test_responses_contain_no_absolute_timestamps(client, store) -> None:
    """§1.1: 応答に絶対時刻を含めない。すべて相対秒で返す。"""
    token = agent_token(store)
    resp = client.post("/api/approval/request", json=request_body(), headers=auth(token))
    body = resp.json()
    assert "grace_remaining_seconds" in body and "claim_remaining_seconds" in body
    assert not any(k.endswith(("_at", "_deadline")) for k in body), body


# ===========================================================================
# POST /api/approval/request（§1.2）
# ===========================================================================


def test_request_creates_approval_and_returns_201(client, store, notify_sent) -> None:
    token = agent_token(store)
    resp = client.post("/api/approval/request", json=request_body(), headers=auth(token))
    assert resp.status_code == 201
    body = resp.json()
    assert body["reused"] is False
    assert body["notify_state"] == "sent"
    assert body["grace_remaining_seconds"] == pytest.approx(150, abs=2)
    assert body["claim_remaining_seconds"] == pytest.approx(180, abs=2)
    assert store_keys.req_key(body["approval_id"]) in store.data


def test_req_record_is_created_before_idem(client, store) -> None:
    """§1.2「`req:` を `idem:` より先に作る理由」の回帰。

    逆順だと、存在しない approval_id を指す idempotency レコードが残り、
    以後そのキーでのリトライが**永久に 404** になる（回復不能）。
    """
    token = agent_token(store)
    approval_id = create_approval(client, token)
    idem_rec = store.data[store_keys.idem_key("claude_code:desktop-haruki", IDEM)]
    assert idem_rec["approval_id"] == approval_id
    assert store_keys.req_key(approval_id) in store.data


def test_same_idempotency_key_returns_the_same_approval_id(client, store) -> None:
    """§8.1 明示項目。"""
    token = agent_token(store)
    first = client.post("/api/approval/request", json=request_body(), headers=auth(token))
    second = client.post("/api/approval/request", json=request_body(), headers=auth(token))
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["approval_id"] == second.json()["approval_id"]
    assert second.json()["reused"] is True


def test_source_and_session_come_from_the_token_not_the_body(client, store) -> None:
    """自己申告を信じない（親設計書 §5.1）。ボディの source は無視される。"""
    token = agent_token(store, session_id="real-session")
    body = request_body()
    body["source"] = "cloud_agent"
    body["session_id"] = "attacker-chosen"
    approval_id = None
    resp = client.post("/api/approval/request", json=body, headers=auth(token))
    approval_id = resp.json()["approval_id"]
    req = store.data[store_keys.req_key(approval_id)]
    assert req["source"] == "claude_code"
    assert req["session_id"] == "real-session"


def test_request_stores_both_workspace_ids_separately(client, store) -> None:
    """トークン由来（所有権照合用）とフック計算値（claim 時のドリフト検知用）。"""
    token = agent_token(store)
    body = request_body()
    body["context"]["workspace_id"] = WS_ID  # フック再計算値
    approval_id = create_approval(client, token)
    req = store.data[store_keys.req_key(approval_id)]
    assert req["workspace_id"] == WS_ID
    assert req["context"]["workspace_id"] == WS_ID


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("idempotency_key"),
        lambda b: b.update(idempotency_key="short"),  # 16 文字未満
        lambda b: b.update(idempotency_key="bad key with spaces!!!!!!!!!!!!"),
        lambda b: b.pop("tool_name"),
        lambda b: b.pop("payload"),
        lambda b: b.update(payload_sha256="not-hex"),
        lambda b: b.update(payload_raw_sha256="abc"),
        lambda b: b["context"].pop("cwd"),
        lambda b: b["context"].update(workspace_id="zz"),
        lambda b: b["context"].pop("base_revision"),  # キー自体の欠落は 400
        lambda b: b.update(risk="CRITICAL"),
        lambda b: b.pop("rule_id"),
        lambda b: b.pop("reason_code"),
        lambda b: b.pop("targets"),
        lambda b: b.update(targets="not-a-list"),
    ],
)
def test_schema_violations_are_400_not_retryable(client, store, mutate) -> None:
    """§1.1: 400 = スキーマ不正・retryable false。pydantic の自動 422 に流さない。"""
    token = agent_token(store)
    body = request_body()
    mutate(body)
    resp = client.post("/api/approval/request", json=body, headers=auth(token))
    assert resp.status_code == 400, resp.text
    assert err(resp)["retryable"] is False


def test_explicit_null_base_revision_is_accepted(client, store) -> None:
    """§4: Git 管理外での作業をブロックしない。明示的な null は正当な値。"""
    token = agent_token(store)
    body = request_body()
    body["context"]["base_revision"] = None
    resp = client.post("/api/approval/request", json=body, headers=auth(token))
    assert resp.status_code == 201


def test_oversized_payload_is_413(client, store) -> None:
    token = agent_token(store)
    body = request_body(payload={"command": "x" * 5000})
    resp = client.post("/api/approval/request", json=body, headers=auth(token))
    assert resp.status_code == 413
    assert err(resp)["retryable"] is False


def test_too_many_targets_is_413(client, store) -> None:
    token = agent_token(store)
    body = request_body(targets=[make_target(path=f"C:/p/{i}.txt") for i in range(33)])
    resp = client.post("/api/approval/request", json=body, headers=auth(token))
    assert resp.status_code == 413


def test_malformed_json_body_is_400(client, store) -> None:
    token = agent_token(store)
    resp = client.post(
        "/api/approval/request",
        content=b"{not json",
        headers={**auth(token), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_rate_limit_returns_429_with_retry_after(client, store) -> None:
    """親設計書 §5.1: HIGH 承認要求 20 件/時。通知疲れ攻撃の封じ込め。"""
    token = agent_token(store)
    for i in range(20):
        body = request_body(idempotency_key=f"idem-{i:016d}-aaaa")
        assert client.post("/api/approval/request", json=body, headers=auth(token)).status_code == 201
    resp = client.post(
        "/api/approval/request", json=request_body(idempotency_key="idem-overflow-000000"), headers=auth(token)
    )
    assert resp.status_code == 429
    assert err(resp)["retryable"] is True
    assert int(resp.headers["Retry-After"]) > 0


def test_notify_failure_is_reported_to_the_agent(client, store, notify_sent) -> None:
    """§4.3: 全失敗した場合はエージェントに伝え、150 秒待たせない。"""
    fake_send, _ = notify_sent
    fake_send.state = "failed"
    token = agent_token(store)
    resp = client.post("/api/approval/request", json=request_body(), headers=auth(token))
    assert resp.json()["notify_state"] == "failed"


def test_reuse_retries_notification_when_not_yet_sent(client, store, notify_sent) -> None:
    """§1.2: 既存再利用でも `notify:` が `sent` でなければ再送する。

    v2 の設計にあった「登録は成功・通知は失敗 → 以後二度と通知されず必ず
    タイムアウト」という穴の回帰テスト。
    """
    fake_send, calls = notify_sent
    token = agent_token(store)
    fake_send.state = "failed"
    client.post("/api/approval/request", json=request_body(), headers=auth(token))
    assert len(calls) == 1
    fake_send.state = "sent"
    resp = client.post("/api/approval/request", json=request_body(), headers=auth(token))
    assert len(calls) == 2, "再利用時に通知の再送が試みられていない"
    assert resp.json()["notify_state"] == "sent"


def test_notifier_exception_fails_closed_to_failed(client, store, monkeypatch) -> None:
    from modal_hub.services import notifier

    monkeypatch.setattr(
        notifier, "send_approval_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ntfy down"))
    )
    token = agent_token(store)
    resp = client.post("/api/approval/request", json=request_body(), headers=auth(token))
    assert resp.json()["notify_state"] == "failed"


def test_notify_failure_is_visible_to_poll_not_just_to_request(client, store, monkeypatch) -> None:
    from modal_hub.services import notifier

    monkeypatch.setattr(
        notifier, "send_approval_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ntfy down"))
    )
    token = agent_token(store)
    approval_id = client.post(
        "/api/approval/request", json=request_body(), headers=auth(token)
    ).json()["approval_id"]

    polled = client.get(f"/api/approval/poll?id={approval_id}", headers=auth(token)).json()
    assert polled["notify_state"] == "failed"


def test_audit_requested_failure_does_not_block_acceptance(client, store) -> None:
    """§10.4: `requested`/`notified` の outbox 失敗は警告のみ。受付は妨げない。"""
    store.fail_outbox_register = True
    token = agent_token(store)
    resp = client.post("/api/approval/request", json=request_body(), headers=auth(token))
    assert resp.status_code == 201


def test_request_indexes_pending_and_gc(client, store) -> None:
    token = agent_token(store)
    approval_id = create_approval(client, token)
    assert approval_id in store.data[store_keys.PREFIX_PENDING_INDEX]
    gc_keys = [k for k in store.data if k.startswith(store_keys.PREFIX_GC_INDEX)]
    assert gc_keys and approval_id in store.data[gc_keys[0]]


# ===========================================================================
# 所有権照合: 別 subject のトークンで 404（§8.1 明示項目・§2 の表）
# ===========================================================================


@pytest.fixture()
def foreign_token(store) -> str:
    return agent_token(store, sub="claude_code:other", session_id="sess-2", workspace_id=WS_ID_OTHER)


def test_poll_with_foreign_token_is_404(client, store, foreign_token) -> None:
    approval_id = create_approval(client, agent_token(store))
    resp = client.get(f"/api/approval/poll?id={approval_id}", headers=auth(foreign_token))
    assert resp.status_code == 404
    assert err(resp)["code"] == "NOT_FOUND"


def test_claim_with_foreign_token_is_404(client, store, foreign_token) -> None:
    """漏洩トークンで他エージェントの approved な lease を先取りする DoS の封じ込め。"""
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)
    assert respond(client, approval_id, "approved", csrf).status_code == 200

    resp = client.post(
        "/api/approval/claim",
        json={"approval_id": approval_id, "claim_attempt_id": "att-x", "verification": verification()},
        headers=auth(foreign_token),
    )
    assert resp.status_code == 404
    assert store_keys.lease_key(approval_id) not in store.data, "他人の lease が焼かれた"


def test_complete_with_foreign_token_is_404(client, store, foreign_token) -> None:
    """偽の完了監査を書き込めないこと。"""
    token = agent_token(store)
    approval_id = create_approval(client, token)
    resp = client.post(
        "/api/approval/complete",
        json={"approval_id": approval_id, "lease_id": "whatever", "outcome": "consumed"},
        headers=auth(foreign_token),
    )
    assert resp.status_code == 404


def test_idempotent_request_with_foreign_token_is_404(client, store) -> None:
    """§2 の表: idem 既存・所有者不一致 → 404。何も返さない。

    `idem:` を subject で名前空間化していても照合が無ければ、同じ
    idempotency key を送るだけで他セッションの approval_id を引ける。
    ここでは **同一 sub・別 session_id** のトークンで衝突を作る。
    """
    token_a = agent_token(store, sub="shared-sub", session_id="sess-A")
    token_b = agent_token(store, sub="shared-sub", session_id="sess-B")
    first = client.post("/api/approval/request", json=request_body(), headers=auth(token_a))
    assert first.status_code == 201
    second = client.post("/api/approval/request", json=request_body(), headers=auth(token_b))
    assert second.status_code == 404, second.text


def test_404_is_used_instead_of_403_to_avoid_leaking_existence(client, store, foreign_token) -> None:
    approval_id = create_approval(client, agent_token(store))
    known = client.get(f"/api/approval/poll?id={approval_id}", headers=auth(foreign_token))
    unknown = client.get("/api/approval/poll?id=00000000-0000-4000-8000-000000000000", headers=auth(foreign_token))
    assert known.status_code == unknown.status_code == 404
    assert known.json() == unknown.json(), "存在の有無で応答が変わっている"


# ===========================================================================
# GET /api/approval/poll（§1.3）
# ===========================================================================


def test_poll_returns_pending_then_approved(client, store) -> None:
    token = agent_token(store)
    approval_id = create_approval(client, token)
    resp = client.get(f"/api/approval/poll?id={approval_id}", headers=auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["decided_by"] is None
    assert body["notify_state"] == "sent"

    csrf = pair_pwa(client)
    respond(client, approval_id, "approved", csrf)

    body = client.get(f"/api/approval/poll?id={approval_id}", headers=auth(token)).json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "pwa"


def test_poll_without_id_is_400(client, store) -> None:
    resp = client.get("/api/approval/poll", headers=auth(agent_token(store)))
    assert resp.status_code == 400


def test_poll_records_timeout_write_once(client, store, monkeypatch) -> None:
    """§1.3/§1.7: poll で状態を書き換えないが、タイムアウトだけは write-once 記録。"""
    token = agent_token(store)
    approval_id = create_approval(client, token)

    req = store.data[store_keys.req_key(approval_id)]
    req["grace_deadline"] = req["created_at"] - 1  # 既に猶予切れ扱いにする

    for _ in range(3):
        body = client.get(f"/api/approval/poll?id={approval_id}", headers=auth(token)).json()
        assert body["status"] == "timeout"

    decision = store.data[store_keys.decision_key(approval_id)]
    assert decision["by"] == "system"
    events = [rec["event"] for rec in store.files.values()]
    assert events.count("timed_out") == 1


# ===========================================================================
# POST /api/approval/respond（§1.6・§2.1 の判定順序）
# ===========================================================================


def test_respond_requires_pwa_cookie_not_agent_bearer(client, store) -> None:
    """§5.1: エージェントトークンでは `respond` を呼べない。自分で承認できない。"""
    token = agent_token(store)
    approval_id = create_approval(client, token)
    resp = client.post(
        "/api/approval/respond",
        json={"approval_id": approval_id, "decision": "approved", "csrf": "x"},
        headers={**auth(token), "Origin": ORIGIN},
    )
    assert resp.status_code == 401


def test_respond_writes_decision_once(client, store) -> None:
    """§8.1 明示項目: `decision:` の二重書き込みは 2 回目に必ず失敗する。"""
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)

    first = respond(client, approval_id, "approved", csrf)
    assert first.status_code == 200

    second = respond(client, approval_id, "rejected", csrf)
    assert second.status_code == 409
    assert err(second)["code"] == "ALREADY_DECIDED"
    assert store.data[store_keys.decision_key(approval_id)]["decision"] == "approved"


def test_respond_after_grace_deadline_is_422(client, store) -> None:
    """§8.1 明示項目: `grace_deadline` 超過後の decision 書き込みは拒否される。"""
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)
    store.data[store_keys.req_key(approval_id)]["grace_deadline"] = 0.0

    resp = respond(client, approval_id, "approved", csrf)
    assert resp.status_code == 422
    assert err(resp)["code"] == "GRACE_EXPIRED"
    # §1.7 の write-once 記録も同時に行われる。
    assert store.data[store_keys.decision_key(approval_id)]["decision"] == "timeout"


def test_respond_existing_decision_check_precedes_grace_check(client, store) -> None:
    """§2.1 `respond` の判定順序 3 と 4。

    既存判定を期限判定より先に置かないと、再送で 422 と 409 が入れ替わる。
    """
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)
    respond(client, approval_id, "approved", csrf)
    store.data[store_keys.req_key(approval_id)]["grace_deadline"] = 0.0

    resp = respond(client, approval_id, "approved", csrf)
    assert resp.status_code == 409
    assert err(resp)["code"] == "ALREADY_DECIDED"


def test_respond_with_bad_csrf_is_403(client, store) -> None:
    token = agent_token(store)
    approval_id = create_approval(client, token)
    pair_pwa(client)
    resp = respond(client, approval_id, "approved", "wrong-csrf")
    assert resp.status_code == 403
    assert err(resp)["code"] == "CSRF_FAILED"


def test_respond_with_wrong_origin_is_403(client, store) -> None:
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)
    resp = respond(client, approval_id, "approved", csrf, origin="https://evil.example")
    assert resp.status_code == 403
    assert err(resp)["code"] == "CSRF_FAILED"


def test_respond_with_missing_origin_is_403(client, store) -> None:
    """`Referer` は使わない。`Origin` が無いだけで拒否する。"""
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)
    resp = client.post(
        "/api/approval/respond",
        json={"approval_id": approval_id, "decision": "approved", "csrf": csrf},
    )
    assert resp.status_code == 403


def test_csrf_and_origin_precede_existence_check(client, store) -> None:
    """§2.1 `respond` 判定順序 1: CSRF/Origin 不正は 404 より先に 403。"""
    pair_pwa(client)
    resp = respond(client, "00000000-0000-4000-8000-000000000000", "approved", "bad")
    assert resp.status_code == 403


def test_respond_with_bad_csrf_and_bad_decision_returns_csrf_failed_not_400(client, store) -> None:
    """DEFECT 3 回帰: §2.1 `respond` 判定順序 1（CSRF/Origin）は decision の
    閉じた語彙チェックより先でなければならない。

    両方が不正な場合に 400 が先に返ると、CSRF/Origin 不正という事実が
    スキーマエラーの陰に隠れてしまう。
    """
    token = agent_token(store)
    approval_id = create_approval(client, token)
    pair_pwa(client)
    resp = client.post(
        "/api/approval/respond",
        json={"approval_id": approval_id, "decision": "not-a-real-decision", "csrf": "wrong-csrf"},
        headers={"Origin": ORIGIN},
    )
    assert resp.status_code == 403, resp.text
    assert err(resp)["code"] == "CSRF_FAILED"


def test_respond_to_unknown_approval_is_404(client, store) -> None:
    csrf = pair_pwa(client)
    resp = respond(client, "00000000-0000-4000-8000-000000000000", "approved", csrf)
    assert resp.status_code == 404


@pytest.mark.parametrize("decision", ["timeout", "maybe", "", "APPROVED"])
def test_respond_rejects_decisions_outside_the_closed_vocabulary(client, store, decision) -> None:
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)
    resp = respond(client, approval_id, decision, csrf)
    assert resp.status_code == 400


def test_respond_audit_failure_returns_500_but_decision_stands(client, store) -> None:
    """§10.4: 決定は既に有効。覆す方が危険なのでロールバックしない。"""
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)
    store.fail_outbox_register = True

    resp = respond(client, approval_id, "approved", csrf)
    assert resp.status_code == 500
    assert err(resp)["code"] == "AUDIT_FAILED"
    assert err(resp)["retryable"] is True
    assert store.data[store_keys.decision_key(approval_id)]["decision"] == "approved"


# ===========================================================================
# POST /api/approval/claim（§1.4・§2.1 の判定順序）
# ===========================================================================


def approve(client: TestClient, store: FakeStore, token: str, **overrides) -> str:
    approval_id = create_approval(client, token, **overrides)
    csrf = pair_pwa(client)
    assert respond(client, approval_id, "approved", csrf).status_code == 200
    return approval_id


def claim(client: TestClient, token: str, approval_id: str, attempt: str = "att-1", ver=None):
    return client.post(
        "/api/approval/claim",
        json={
            "approval_id": approval_id,
            "claim_attempt_id": attempt,
            "verification": ver if ver is not None else verification(),
        },
        headers=auth(token),
    )


def test_claim_grants_a_lease(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    resp = claim(client, token, approval_id)
    assert resp.status_code == 200
    assert resp.json()["granted"] is True
    assert resp.json()["lease_id"]


def test_second_claim_with_a_different_attempt_id_is_409(client, store) -> None:
    """§8.1 明示項目: `lease:` の二重取得は失敗する。"""
    token = agent_token(store)
    approval_id = approve(client, store, token)
    first = claim(client, token, approval_id, attempt="att-1")
    second = claim(client, token, approval_id, attempt="att-2")
    assert first.status_code == 200
    assert second.status_code == 409
    assert err(second)["code"] == "ALREADY_CLAIMED"


def test_same_attempt_id_is_idempotent_and_returns_the_same_lease(client, store) -> None:
    """§1.4: v1 の「lease を焼いて 500」は破棄。応答喪失からの正当な回復。"""
    token = agent_token(store)
    approval_id = approve(client, store, token)
    first = claim(client, token, approval_id, attempt="att-1")
    second = claim(client, token, approval_id, attempt="att-1")
    assert second.status_code == 200
    assert second.json()["lease_id"] == first.json()["lease_id"]


def test_claim_retry_with_malformed_verification_is_idempotent_200_not_400(client, store) -> None:
    """DEFECT 3 回帰: §2.1 判定順序 2 は 8 より先。

    応答喪失後の同一 attempt でのリトライは、たとえ今回の `verification` が
    スキーマ不正（本来なら 400 になる形）でも、既に取得済みの lease を
    そのまま 200 で冪等に返さなければならない。verification の完全な
    スキーマ解析は判定順序 8（最後）まで到達しなければ行われない。
    """
    token = agent_token(store)
    approval_id = approve(client, store, token)
    first = claim(client, token, approval_id, attempt="att-1")
    assert first.status_code == 200

    malformed_ver = verification()
    malformed_ver.pop("payload_raw_sha256")  # 単独なら 400 になるスキーマ不正
    second = claim(client, token, approval_id, attempt="att-1", ver=malformed_ver)
    assert second.status_code == 200, second.text
    assert second.json()["lease_id"] == first.json()["lease_id"]


def test_claim_before_approval_is_409_not_approved(client, store) -> None:
    """§2 の表: pending + claim → 409 NOT_APPROVED。"""
    token = agent_token(store)
    approval_id = create_approval(client, token)
    resp = claim(client, token, approval_id)
    assert resp.status_code == 409
    assert err(resp)["code"] == "NOT_APPROVED"


def test_claim_after_rejection_is_409_not_approved(client, store) -> None:
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)
    respond(client, approval_id, "rejected", csrf)
    resp = claim(client, token, approval_id)
    assert resp.status_code == 409
    assert err(resp)["code"] == "NOT_APPROVED"


def test_claim_after_claim_deadline_is_422_claim_expired(client, store) -> None:
    """§8.1 明示項目: `claim_deadline` 超過後の claim は失敗（無期限再利用の防止）。

    §9 落とし穴 23。`claim_deadline` が無いと、承認済みで claim されなかった
    ものが 24 時間有効な Bearer トークンで数時間後に再利用できてしまう。
    """
    token = agent_token(store)
    approval_id = approve(client, store, token)
    store.data[store_keys.req_key(approval_id)]["claim_deadline"] = 0.0
    resp = claim(client, token, approval_id)
    assert resp.status_code == 422
    assert err(resp)["code"] == "CLAIM_EXPIRED"
    assert store_keys.lease_key(approval_id) not in store.data


def test_claim_expired_is_not_masked_by_not_approved(client, store) -> None:
    """§2.1 の主題: `status_of()` を先に評価すると `CLAIM_EXPIRED` が
    `NOT_APPROVED` に落ちてしまう。判定順序 4/5 より 7 が後にあること。"""
    token = agent_token(store)
    approval_id = approve(client, store, token)
    store.data[store_keys.req_key(approval_id)]["claim_deadline"] = 0.0
    assert err(claim(client, token, approval_id))["code"] == "CLAIM_EXPIRED"


def test_claim_with_late_decision_is_422_grace_expired(client, store) -> None:
    """§2.1 判定順序 6: 遅延して入った決定 → GRACE_EXPIRED。"""
    token = agent_token(store)
    approval_id = approve(client, store, token)
    req = store.data[store_keys.req_key(approval_id)]
    store.data[store_keys.decision_key(approval_id)]["at"] = req["grace_deadline"] + 1
    resp = claim(client, token, approval_id)
    assert resp.status_code == 422
    assert err(resp)["code"] == "GRACE_EXPIRED"


def test_idempotent_reclaim_precedes_all_other_checks(client, store) -> None:
    """§2.1 判定順序 2 は 4〜8 より先。期限切れでも同一 attempt なら 200。"""
    token = agent_token(store)
    approval_id = approve(client, store, token)
    first = claim(client, token, approval_id, attempt="att-1")
    store.data[store_keys.req_key(approval_id)]["claim_deadline"] = 0.0
    second = claim(client, token, approval_id, attempt="att-1")
    assert second.status_code == 200
    assert second.json()["lease_id"] == first.json()["lease_id"]


@pytest.mark.parametrize(
    "mutate,expected_field",
    [
        (lambda v: v.update(payload_sha256="0" * 64), "payload_sha256"),
        (lambda v: v.update(payload_raw_sha256="0" * 64), "payload_raw_sha256"),
        (lambda v: v["context"].update(cwd="C:/elsewhere"), "context.cwd"),
        (lambda v: v["context"].update(workspace_id="9" * 64), "context.workspace_id"),
        (lambda v: v["context"].update(base_revision="1" * 40), "context.base_revision"),
    ],
)
def test_verification_mismatch_is_422_mismatch(client, store, mutate, expected_field) -> None:
    """§8.1 明示項目: payload_sha256 / cwd / base_revision の不一致で mismatch。"""
    token = agent_token(store)
    approval_id = approve(client, store, token)
    ver = verification()
    mutate(ver)
    resp = claim(client, token, approval_id, ver=ver)
    assert resp.status_code == 422
    assert err(resp)["code"] == "MISMATCH"
    assert expected_field in err(resp)["message"]
    assert store_keys.lease_key(approval_id) not in store.data


def test_mismatch_writes_an_audit_record(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    ver = verification()
    ver["context"]["cwd"] = "C:/elsewhere"
    claim(client, token, approval_id, ver=ver)
    assert any(rec["event"] == "mismatch" for rec in store.files.values())


def test_symlink_swap_between_request_and_claim_is_mismatch(client, store) -> None:
    """親設計書 §4.3 / §9 落とし穴 24 の実シナリオ。

    承認時と実行直前で `path` / payload / cwd / HEAD はすべて同一だが、
    realpath と lstat 識別子と内容ハッシュだけが変わっている。
    """
    token = agent_token(store)
    original = make_target(
        path="C:/proj/workspace/output.txt",
        realpath="C:/proj/workspace/output.txt",
        identity="17735206716449772873:100",
        preimage_sha256="a" * 64,
    )
    approval_id = approve(client, store, token, targets=[original])

    swapped = dict(
        original,
        realpath="C:/Users/Haruki/.ssh/id_rsa",
        identity="17735206716449772873:999",
        preimage_sha256="b" * 64,
    )
    resp = claim(client, token, approval_id, ver=verification(targets=[swapped]))
    assert resp.status_code == 422
    assert err(resp)["code"] == "MISMATCH"
    assert "realpath" in err(resp)["message"]


def test_claim_verification_schema_violation_is_400(client, store) -> None:
    """§1.4「1 つでも欠けたら 400、1 つでも違ったら 422」。"""
    token = agent_token(store)
    approval_id = approve(client, store, token)
    ver = verification()
    ver.pop("payload_raw_sha256")
    resp = claim(client, token, approval_id, ver=ver)
    assert resp.status_code == 400


def test_claim_audit_failure_is_500_and_lease_is_retained(client, store) -> None:
    """§1.4/§10.4: outbox 登録に失敗したら 500 AUDIT_FAILED。**lease は焼かない**。

    同一 `claim_attempt_id` でのリトライで同じ lease を受け取って実行できる。
    """
    token = agent_token(store)
    approval_id = approve(client, store, token)
    store.fail_outbox_register = True

    resp = claim(client, token, approval_id, attempt="att-1")
    assert resp.status_code == 500
    assert err(resp)["code"] == "AUDIT_FAILED"
    assert err(resp)["retryable"] is True
    assert store_keys.lease_key(approval_id) in store.data, "lease が焼かれている"

    store.fail_outbox_register = False
    retry = claim(client, token, approval_id, attempt="att-1")
    assert retry.status_code == 200


def test_claim_granted_writes_an_audit_record(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    claim(client, token, approval_id)
    assert any(rec["event"] == "claim_granted" for rec in store.files.values())


# ===========================================================================
# POST /api/approval/complete（§1.5・§2.1 の判定順序）
# ===========================================================================


def complete(client, token, approval_id, lease_id, outcome="consumed", detail=None):
    body = {"approval_id": approval_id, "lease_id": lease_id, "outcome": outcome}
    if detail is not None:
        body["detail"] = detail
    return client.post("/api/approval/complete", json=body, headers=auth(token))


def test_complete_records_and_is_idempotent(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    lease_id = claim(client, token, approval_id).json()["lease_id"]

    first = complete(client, token, approval_id, lease_id)
    assert first.status_code == 200 and first.json() == {"recorded": True}

    second = complete(client, token, approval_id, lease_id)
    assert second.status_code == 200
    assert second.json() == {"recorded": False, "already": True}


def test_complete_owned_by_different_session_is_404_not_a_validation_error(client, store, foreign_token) -> None:
    """DEFECT 3 回帰: §2.1 `complete` 判定順序 1（所有権不一致→404）は
    outcome/detail のスキーマ検証より先でなければならない。

    さもないと、他人の承認への complete が「不正な outcome です」という
    400 を返してしまい、承認 ID が実在すること自体を未所有者へ漏らす。
    """
    token = agent_token(store)
    approval_id = approve(client, store, token)
    lease_id = claim(client, token, approval_id).json()["lease_id"]

    resp = complete(client, foreign_token, approval_id, lease_id, outcome="not-a-real-outcome")
    assert resp.status_code == 404, resp.text
    assert err(resp)["code"] == "NOT_FOUND"


def test_complete_without_a_lease_is_409_not_claimed(client, store) -> None:
    """§2 の表: approved + complete → 409（lease を取らずに完了報告はできない）。"""
    token = agent_token(store)
    approval_id = approve(client, store, token)
    resp = complete(client, token, approval_id, "made-up-lease")
    assert resp.status_code == 409
    assert err(resp)["code"] == "NOT_CLAIMED"


def test_complete_with_wrong_lease_id_is_409(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    claim(client, token, approval_id)
    resp = complete(client, token, approval_id, "not-the-right-lease")
    assert resp.status_code == 409
    assert err(resp)["code"] == "NOT_CLAIMED"


def test_complete_while_pending_is_409_not_claimed(client, store) -> None:
    token = agent_token(store)
    approval_id = create_approval(client, token)
    resp = complete(client, token, approval_id, "x")
    assert resp.status_code == 409
    assert err(resp)["code"] == "NOT_CLAIMED"


@pytest.mark.parametrize("outcome", ["consumed", "failed", "mismatch"])
def test_complete_accepts_the_closed_outcome_vocabulary(client, store, outcome) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    lease_id = claim(client, token, approval_id).json()["lease_id"]
    assert complete(client, token, approval_id, lease_id, outcome=outcome).status_code == 200
    assert any(rec["event"] == outcome for rec in store.files.values())


def test_complete_rejects_unknown_outcome(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    lease_id = claim(client, token, approval_id).json()["lease_id"]
    assert complete(client, token, approval_id, lease_id, outcome="succeeded").status_code == 400


def test_complete_detail_over_1kb_is_413(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    lease_id = claim(client, token, approval_id).json()["lease_id"]
    resp = complete(client, token, approval_id, lease_id, detail="x" * 2000)
    assert resp.status_code == 413


def test_complete_detail_is_redacted_in_the_audit_record(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    lease_id = claim(client, token, approval_id).json()["lease_id"]
    complete(client, token, approval_id, lease_id, detail="failed: token=abcdefghijklmnop")
    consumed = [r for r in store.files.values() if r["event"] == "consumed"][0]
    assert "abcdefghijklmnop" not in json.dumps(consumed, ensure_ascii=False)


# ===========================================================================
# 終端状態からの遷移（§2 の表の最下部 3 行）
# ===========================================================================


def test_rejected_is_terminal(client, store) -> None:
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)
    respond(client, approval_id, "rejected", csrf)

    assert err(respond(client, approval_id, "approved", csrf))["code"] == "ALREADY_DECIDED"
    assert err(claim(client, token, approval_id))["code"] == "NOT_APPROVED"
    assert err(complete(client, token, approval_id, "x"))["code"] == "NOT_CLAIMED"


def test_timeout_is_terminal(client, store) -> None:
    token = agent_token(store)
    approval_id = create_approval(client, token)
    csrf = pair_pwa(client)
    req = store.data[store_keys.req_key(approval_id)]
    req["grace_deadline"] = 0.0
    req["claim_deadline"] = 0.0

    assert err(respond(client, approval_id, "approved", csrf))["code"] == "GRACE_EXPIRED"
    assert err(claim(client, token, approval_id))["code"] == "NOT_APPROVED"
    assert err(complete(client, token, approval_id, "x"))["code"] == "NOT_CLAIMED"


def test_claimed_blocks_further_respond_and_claim(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    csrf = pair_pwa(client)
    claim(client, token, approval_id, attempt="att-1")

    assert err(respond(client, approval_id, "rejected", csrf))["code"] == "ALREADY_DECIDED"
    assert err(claim(client, token, approval_id, attempt="att-9"))["code"] == "ALREADY_CLAIMED"


def test_crashed_lease_holder_is_not_released(client, store) -> None:
    """§2「lease 保持者がクラッシュした場合」: 解放しない（意図的）。

    解放すると「1 回の承認で 2 回実行される」経路ができる。
    """
    token = agent_token(store)
    approval_id = approve(client, store, token)
    claim(client, token, approval_id, attempt="att-1")
    store.data[store_keys.req_key(approval_id)]["claim_deadline"] = 0.0

    assert err(claim(client, token, approval_id, attempt="att-2"))["code"] == "ALREADY_CLAIMED"
    body = client.get(f"/api/approval/poll?id={approval_id}", headers=auth(token)).json()
    assert body["status"] == "claimed"


# ===========================================================================
# PWA 側エンドポイント
# ===========================================================================


def test_pending_lists_only_pending_items_with_csrf(client, store) -> None:
    token = agent_token(store)
    a = create_approval(client, token)
    b = create_approval(client, token, idempotency_key="idem-second-aaaaaaaaaa")
    csrf = pair_pwa(client)
    respond(client, b, "rejected", csrf)

    body = client.get("/api/approval/pending").json()
    ids = [item["approval_id"] for item in body["items"]]
    assert ids == [a]
    assert body["csrf_token"]
    item = body["items"][0]
    assert set(item) >= {"approval_id", "tool_name", "risk", "rule_id", "reason", "summary", "grace_remaining_seconds"}


def test_pending_requires_a_pwa_cookie(client, store) -> None:
    assert client.get("/api/approval/pending").status_code == 401


def test_pending_summary_is_redacted_and_truncated(client, store) -> None:
    token = agent_token(store)
    create_approval(client, token, payload={"command": "curl -H 'Authorization: Bearer " + "a" * 40 + "'"})
    pair_pwa(client)
    item = client.get("/api/approval/pending").json()["items"][0]
    assert "<REDACTED:bearer>" in item["summary"]
    assert len(item["summary"]) <= 200


def test_detail_returns_the_full_redacted_payload(client, store) -> None:
    token = agent_token(store)
    approval_id = create_approval(client, token, payload={"command": "echo sk-ant-" + "a" * 30})
    pair_pwa(client)
    body = client.get(f"/api/approval/detail?id={approval_id}").json()
    assert body["approval_id"] == approval_id
    assert "sk-ant-" not in json.dumps(body, ensure_ascii=False)
    assert "<REDACTED:anthropic>" in body["payload"]["command"]


def test_pending_and_detail_both_include_context_with_all_three_keys(client, store) -> None:
    """DEFECT 1 回帰: PWA が「どのディレクトリ/リポジトリを対象とするか」を
    判断できるよう、pending 一覧・detail の両方に context を含める
    （06_PWA_Design.md）。app.js は `raw.context.cwd` /
    `raw.context.base_revision` を読む契約になっている。
    """
    token = agent_token(store)
    approval_id = create_approval(client, token)
    pair_pwa(client)

    pending_item = client.get("/api/approval/pending").json()["items"][0]
    assert set(pending_item["context"]) == {"cwd", "workspace_id", "base_revision"}
    assert pending_item["context"]["cwd"] == "C:/Users/Haruki/Projects/Foo"
    assert pending_item["context"]["workspace_id"] == WS_ID
    assert pending_item["context"]["base_revision"] == HEAD_REV

    detail = client.get(f"/api/approval/detail?id={approval_id}").json()
    assert set(detail["context"]) == {"cwd", "workspace_id", "base_revision"}
    assert detail["context"] == pending_item["context"]


def test_secret_in_cwd_is_redacted_in_pending_and_detail(client, store) -> None:
    """DEFECT 1 回帰: `cwd` はファイルシステムパスであり、ユーザー名や
    トークンを含みうる。`payload` と同じ redaction 経路を通すこと。
    """
    token = agent_token(store)
    secret_cwd = "C:/Users/Haruki/AKIAIOSFODNN7EXAMPLE/Projects/Foo"
    approval_id = create_approval(
        client, token, context={"cwd": secret_cwd, "workspace_id": WS_ID, "base_revision": HEAD_REV}
    )
    pair_pwa(client)

    pending_item = client.get("/api/approval/pending").json()["items"][0]
    assert "AKIAIOSFODNN7EXAMPLE" not in pending_item["context"]["cwd"]
    assert "<REDACTED:aws>" in pending_item["context"]["cwd"]

    detail = client.get(f"/api/approval/detail?id={approval_id}").json()
    assert "AKIAIOSFODNN7EXAMPLE" not in detail["context"]["cwd"]
    assert "<REDACTED:aws>" in detail["context"]["cwd"]


def test_secret_in_a_target_is_redacted_in_detail(client, store) -> None:
    """DEFECT 2 回帰: `targets` は認証済みエージェント由来の自由文
    （path 等）であり、認証情報を埋め込んで PWA に表示させうる。

    `pending` はそもそも `targets` を返さない（summary/context のみ、
    §1.6）ため、そちらには同じ欠落は存在しないことも併せて確認する。
    """
    token = agent_token(store)
    secret_target = make_target(
        path="C:/Users/Haruki/AKIAIOSFODNN7EXAMPLE/out.txt",
        realpath="C:/Users/Haruki/AKIAIOSFODNN7EXAMPLE/out.txt",
    )
    approval_id = create_approval(client, token, tool_name="Write", targets=[secret_target])
    pair_pwa(client)

    detail = client.get(f"/api/approval/detail?id={approval_id}").json()
    blob = json.dumps(detail["targets"], ensure_ascii=False)
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "<REDACTED:aws>" in blob

    pending_item = client.get("/api/approval/pending").json()["items"][0]
    assert "targets" not in pending_item


def test_secret_straddling_the_summary_truncation_boundary_is_fully_redacted(client, store) -> None:
    """redaction は truncate より前でなければならない。逆順だと 200 文字
    境界をまたぐ秘密のパターンが分断されて正規表現が一致しなくなり、
    生の秘密の先頭部分が truncate 後の文字列にそのまま残ってしまう
    （このバグは本コードベースで一度実際に起きている）。
    """
    token = agent_token(store)
    secret = "sk-ant-" + "a" * 30  # 37 文字
    command = "x" * 185 + secret  # 185..222 が 200 文字境界をまたぐ
    approval_id = create_approval(client, token, payload={"command": command})
    pair_pwa(client)

    pending_item = client.get("/api/approval/pending").json()["items"][0]
    assert "sk-ant-" not in pending_item["summary"]
    assert "a" * 30 not in pending_item["summary"]
    assert len(pending_item["summary"]) <= 200


def test_ws_ticket_requires_csrf_and_origin(client, store) -> None:
    csrf = pair_pwa(client)
    bad = client.post("/api/pwa/ws-ticket", json={"csrf": "nope"}, headers={"Origin": ORIGIN})
    assert bad.status_code == 403
    good = client.post("/api/pwa/ws-ticket", json={"csrf": csrf}, headers={"Origin": ORIGIN})
    assert good.status_code == 200
    assert good.json()["expires_in"] == 30


def test_logout_invalidates_the_session(client, store) -> None:
    pair_pwa(client)
    assert client.get("/api/approval/pending").status_code == 200
    assert client.post("/api/pwa/logout").status_code == 200
    assert client.get("/api/approval/pending").status_code == 401


def test_pwa_cookie_is_httponly_secure_samesite_strict(client, store) -> None:
    from modal_hub.tests.conftest import TEST_PAIRING_CODE

    resp = client.post("/api/pwa/pair", json={"code": TEST_PAIRING_CODE, "device_name": "iPhone"})
    raw = resp.headers["set-cookie"]
    assert "HttpOnly" in raw
    assert "Secure" in raw
    assert "SameSite=strict" in raw or "SameSite=Strict" in raw


def test_static_bootstrap_code_works_only_once(client, store) -> None:
    """§7.1b: `HH_PAIRING_CODE` は初回ブートストラップ 1 回限り。使ったら閉じる。"""
    from modal_hub.tests.conftest import TEST_PAIRING_CODE

    first = client.post("/api/pwa/pair", json={"code": TEST_PAIRING_CODE, "device_name": "iPhone"})
    assert first.status_code == 200
    assert store.data.get("bootstrap_done") is not None

    client.cookies.clear()
    second = client.post("/api/pwa/pair", json={"code": TEST_PAIRING_CODE, "device_name": "iPad"})
    assert second.status_code in (401, 409), "静的コード経路が閉じていない"


def test_dynamic_pairing_offer_flow_after_bootstrap(client, store) -> None:
    from modal_hub.tests.conftest import TEST_PAIRING_CODE

    client.post("/api/pwa/pair", json={"code": TEST_PAIRING_CODE, "device_name": "iPhone"})
    client.cookies.clear()

    code = security.create_pairing_offer(store)
    resp = client.post("/api/pwa/pair", json={"code": code, "device_name": "iPad"})
    assert resp.status_code == 200


def test_dynamic_pairing_as_first_ever_pairing_also_closes_static_code(client, store) -> None:
    """§7.1b 回帰テスト（Codexレビュー指摘）: 静的コードを一度も使わず、
    動的オファー（`scripts/hh_pwa_pair.py` 相当）だけで初回ペアリングした
    場合でも、`bootstrap_done` が立って `HH_PAIRING_CODE` が恒久的に
    無効化されること。

    修正前は bootstrap_done を書くのが静的コード経路（`if not
    bootstrap_done and ...` ブロック）だけだったため、動的コードで初回
    ペアリングを済ませると HH_PAIRING_CODE がその後も有効なまま残り、
    Secret を明示的にローテーションしない限り恒久的な残存資格情報になって
    いた。
    """
    from modal_hub.tests.conftest import TEST_PAIRING_CODE

    assert store.data.get("bootstrap_done") is None  # 前提: まだ誰もペアリングしていない

    code = security.create_pairing_offer(store)
    resp = client.post("/api/pwa/pair", json={"code": code, "device_name": "iPhone"})
    assert resp.status_code == 200
    assert store.data.get("bootstrap_done") is not None

    client.cookies.clear()
    static_attempt = client.post(
        "/api/pwa/pair", json={"code": TEST_PAIRING_CODE, "device_name": "iPad"}
    )
    assert static_attempt.status_code in (401, 409), "動的経路での初回ペアリング後も静的コードが有効なまま"


def test_pairing_is_rate_limited_and_audited(client, store) -> None:
    """§7.1: IP ごと 10 回/10 分。上限超過は 429 ＋ `pairing_rate_limited` 監査。"""
    for _ in range(10):
        client.post("/api/pwa/pair", json={"code": "00000000", "device_name": "x"})
    resp = client.post("/api/pwa/pair", json={"code": "00000000", "device_name": "x"})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert any(rec["event"] == "pairing_rate_limited" for rec in store.files.values())


def test_wrong_pairing_code_is_401(client, store) -> None:
    resp = client.post("/api/pwa/pair", json={"code": "99999999", "device_name": "x"})
    assert resp.status_code == 401
    assert err(resp)["code"] == "PAIRING_INVALID"


# ===========================================================================
# 監査に秘密が残らないこと（親設計書 §5.2 / spec §10.3）
# ===========================================================================


def test_no_audit_record_contains_a_raw_secret(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(
        client, store, token, payload={"command": "AWS_KEY=AKIAIOSFODNN7EXAMPLE aws s3 rm s3://b --recursive"}
    )
    lease_id = claim(client, token, approval_id).json()["lease_id"]
    complete(client, token, approval_id, lease_id)

    blob = json.dumps(list(store.files.values()), ensure_ascii=False)
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "<REDACTED:aws>" in blob


def test_no_response_body_ever_contains_the_agent_token(client, store) -> None:
    token = agent_token(store)
    approval_id = approve(client, store, token)
    for resp in (
        client.get(f"/api/approval/poll?id={approval_id}", headers=auth(token)),
        claim(client, token, approval_id),
    ):
        assert token not in resp.text
