"""issue_dispatch_token Modal Function のテスト — `.agentic_os_issue_dispatch_token_task.md`。

タスクの完了報告要件:
    - `issue_dispatch_token` が発行したトークンで `POST /api/dispatch/headless` の
      認証（`_verify_dispatch_agent`）を実際に通過する（③の HTTP 統合テスト）。
    - `agent_session:<tid>` 肯定リストレコードがストアへ書き込まれること
      （`issue_agent_token` → `verify_agent_token` の往復）。

方針（test_dispatch_router.py と同じ）:
    - Modal には触れない。ストアだけを conftest の FakeStore に差し替え、
      署名・検証は実装の本物を通す。
    - `AGENTIC_OS_DISPATCH_KEY` はテスト用の固定値（本物の Secret ではない）。
    - Modal Function のラッパー（`@app.function()` デコレータ付き）自体も
      ローカル呼び出しで 1 本通す（Modal SDK のローカル実行経路）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modal_hub.core import config as hub_config
from modal_hub.core import security
from modal_hub.core import store as store_keys
from modal_hub.dispatch_token import (
    WORKSPACE_AGENTIC_OS,
    _issue_dispatch_token_core,
)
from modal_hub.routers import dispatch
from modal_hub.tests.conftest import FakeStore

ORIGIN = "https://testserver"

# AGENTIC_OS_DISPATCH_KEY 用のテスト鍵（test_dispatch_router.py と同じ値。
# 本物の Secret ではない）。
TEST_DISPATCH_KEY = "test-agentic-os-dispatch-key-0123456789"


# ---------------------------------------------------------------------------
# フィクスチャ（test_dispatch_router.py と同型）
# ---------------------------------------------------------------------------


@pytest.fixture()
def dispatch_env(monkeypatch, secret_env):
    """`secret_env`（既存の必須キー一式）+ AGENTIC_OS_DISPATCH_KEY。"""
    monkeypatch.setenv(hub_config.AGENTIC_OS_DISPATCH_KEY, TEST_DISPATCH_KEY)
    return hub_config


@pytest.fixture()
def store(monkeypatch, dispatch_env) -> FakeStore:
    """1 つの FakeStore を発行側と検証側の両方へ束ねる。

    トークン発行（`_issue_dispatch_token_core` に渡す）と
    `POST /api/dispatch/headless` の検証（`dispatch._LIVE_STORE`）が
    **同じ**ストアを共有して初めて「発行したトークンで認証を通過する」ことを
    実証できる（肯定リストはストア内の `agent_session:<tid>` だから）。
    """
    s = FakeStore()
    monkeypatch.setattr(dispatch, "_LIVE_STORE", s)
    return s


@pytest.fixture()
def client(store) -> TestClient:
    app = FastAPI()
    app.include_router(dispatch.router)
    return TestClient(app, base_url=ORIGIN)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# ① 発行 → 検証の往復（ユニット）
# ---------------------------------------------------------------------------


def test_issue_dispatch_token_round_trips_through_verify(store) -> None:
    """`issue_agent_token` → `verify_agent_token` の往復（タスク要件②）。"""
    token = _issue_dispatch_token_core(
        store, sub="agentic_os_hub", session_id="sess-os-1"
    )
    assert token.startswith(security.TOKEN_PREFIX + ".")

    identity = security.verify_agent_token(
        store, token, signing_key=TEST_DISPATCH_KEY.encode("utf-8")
    )
    assert identity.sub == "agentic_os_hub"
    assert identity.session_id == "sess-os-1"
    assert identity.source == security.SOURCE_CLAUDE_CODE
    assert identity.workspace_id == WORKSPACE_AGENTIC_OS
    assert identity.scopes == frozenset({security.SCOPE_DISPATCH})
    assert identity.has_scope(security.SCOPE_DISPATCH)
    # dispatch 以外のスコープ（publish 等）は付与しない
    assert not identity.has_scope(security.SCOPE_PUBLISH)


def test_issue_dispatch_token_writes_allowlist_record(store) -> None:
    """`agent_session:<tid>` 肯定リストレコードが実際にストアへ書かれる。"""
    token = _issue_dispatch_token_core(
        store, sub="agentic_os_hub", session_id="sess-os-2"
    )
    tid = security.verify_agent_token(
        store, token, signing_key=TEST_DISPATCH_KEY.encode("utf-8")
    ).tid

    record = store.get(store_keys.agent_session_key(tid))
    assert record is not None
    assert record["sub"] == "agentic_os_hub"
    assert record["session_id"] == "sess-os-2"
    assert record["source"] == security.SOURCE_CLAUDE_CODE
    assert record["workspace_id"] == WORKSPACE_AGENTIC_OS
    assert record["scopes"] == [security.SCOPE_DISPATCH]
    assert isinstance(record["exp"], int) and record["exp"] > record["issued_at"]


def test_issue_dispatch_token_fails_closed_without_key(monkeypatch, secret_env) -> None:
    """AGENTIC_OS_DISPATCH_KEY 未設定では発行せずエラー（fail-closed）。"""
    monkeypatch.delenv(hub_config.AGENTIC_OS_DISPATCH_KEY, raising=False)
    with pytest.raises(security.SecurityError):
        _issue_dispatch_token_core(
            FakeStore(), sub="agentic_os_hub", session_id="sess-os-3"
        )


def test_issue_dispatch_token_rejects_empty_sub(store) -> None:
    """空の sub / session_id は issue_agent_token のバリデーションで拒否される。"""
    with pytest.raises(ValueError):
        _issue_dispatch_token_core(store, sub="", session_id="sess-os-4")
    with pytest.raises(ValueError):
        _issue_dispatch_token_core(store, sub="agentic_os_hub", session_id="")


# ---------------------------------------------------------------------------
# ② Modal Function ラッパー（@app.function() 付き）のローカル呼び出し
# ---------------------------------------------------------------------------


def test_modal_function_wrapper_issues_token_locally(monkeypatch, dispatch_env) -> None:
    """デコレータ付きの実物 `issue_dispatch_token` をローカル実行できる。

    Modal の `Function` オブジェクトは直接呼び出せないため（`__call__` なし）、
    `Function.local()`（modal/_functions.py のローカル実行経路）で呼ぶ。これは
    コンテナへは出ず生の関数本体を同一プロセスで実行するだけで、本番の
    `.remote()` と同一の配線（@app.function() が包んだ関数本体）を通る。
    """
    from modal_hub import dispatch_token

    s = FakeStore()
    monkeypatch.setattr(dispatch_token, "_LIVE_STORE", s)

    token = dispatch_token.issue_dispatch_token.local(
        "agentic_os_hub", "sess-os-5"
    )
    assert token.startswith(security.TOKEN_PREFIX + ".")

    identity = security.verify_agent_token(
        s, token, signing_key=TEST_DISPATCH_KEY.encode("utf-8")
    )
    assert identity.sub == "agentic_os_hub"
    assert identity.session_id == "sess-os-5"
    assert identity.has_scope(security.SCOPE_DISPATCH)


# ---------------------------------------------------------------------------
# ③ 発行したトークンで /api/dispatch/headless の認証を通過する（HTTP 統合）
# ---------------------------------------------------------------------------


class FakePopen:
    """`subprocess.Popen` の最小模倣（test_dispatch_router.py と同一の模倣）。

    `--usage-file` へ JSON を書き、stdout へ最終応答を返す。hermes の実実行は
    一切行わない。起動引数・環境・cwd は `FakePopen.last` に記録される。
    """

    last = None
    returncode = 0
    pid = 4242
    stdout_text = "final answer\n"
    session_id = "sess-abc123"

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        FakePopen.last = self

    def communicate(self, timeout=None):
        for i, arg in enumerate(self.cmd):
            if arg == "--usage-file":
                usage_path = Path(self.cmd[i + 1])
                usage_path.parent.mkdir(parents=True, exist_ok=True)
                usage_path.write_text(
                    json.dumps({"session_id": self.session_id, "completed": True}),
                    encoding="utf-8",
                )
                break
        return (self.stdout_text, "")

    def wait(self) -> int:
        return self.returncode


def test_issued_token_passes_headless_authentication(client, store, monkeypatch) -> None:
    """`issue_dispatch_token` が発行したトークンで認証を実際に通過する（要件①）。

    `_verify_dispatch_agent`（Bearer → verify_agent_token → require_scope）を
    本物のまま通し、200（hermes 実行は FakePopen）で返ることを確認する。
    """
    monkeypatch.setattr(dispatch.subprocess, "Popen", FakePopen)
    token = _issue_dispatch_token_core(
        store, sub="agentic_os_hub", session_id="sess-os-6"
    )

    resp = client.post(
        "/api/dispatch/headless", json={"prompt": "hello agent"}, headers=auth(token)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["response"] == "final answer"
    assert body["session_id"] == "sess-abc123"
    # FakePopen が実際に起動される = 認証・レート制限・ボディ検証を通過した証拠
    assert FakePopen.last is not None
    popen = FakePopen.last
    assert popen.cmd[0] == sys.executable
    assert "-z" in popen.cmd
    assert popen.cmd[popen.cmd.index("-z") + 1] == "hello agent"
