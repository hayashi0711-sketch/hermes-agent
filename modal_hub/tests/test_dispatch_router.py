"""dispatch router の HTTP 契約 — `.agentic_os_headless_dispatch_task.md` の最低3要件。

    ① 認証が無い/不正なリクエストが拒否される
    ② 正常系で hermes_cli 呼び出しが期待通りの引数で行われる
    ③ タイムアウト処理（504）

Hermes の実実行は一切行わない。`dispatch._run_hermes_oneshot`（ハンドラ側の
マッピング検証用）または `dispatch.subprocess.Popen`（起動引数検証用）をモックに
差し替える。`_LIVE_STORE` だけを conftest の FakeStore に差し替え、認証・
レート制限・エラー封筒は実装の本物を通す（test_approval_gate_http.py と同じ方針）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modal_hub.core import config as hub_config
from modal_hub.core import security
from modal_hub.routers import dispatch
from modal_hub.tests.conftest import (
    TEST_AGENT_SIGNING_KEY,
    WS_ID,
    FakeStore,
)

ORIGIN = "https://testserver"

# AGENTIC_OS_DISPATCH_KEY 用のテスト鍵（本物の Secret ではない）。
TEST_DISPATCH_KEY = "test-agentic-os-dispatch-key-0123456789"


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------


@pytest.fixture()
def dispatch_env(monkeypatch, secret_env):
    """`secret_env`（既存の必須キー一式）+ AGENTIC_OS_DISPATCH_KEY。"""
    monkeypatch.setenv(hub_config.AGENTIC_OS_DISPATCH_KEY, TEST_DISPATCH_KEY)
    return hub_config


@pytest.fixture()
def store(monkeypatch, dispatch_env) -> FakeStore:
    s = FakeStore()
    monkeypatch.setattr(dispatch, "_LIVE_STORE", s)
    return s


@pytest.fixture()
def client(store) -> TestClient:
    app = FastAPI()
    app.include_router(dispatch.router)
    return TestClient(app, base_url=ORIGIN)


def dispatch_token(
    store: FakeStore,
    *,
    scopes: tuple[str, ...] = ("dispatch",),
    signing_key: str = TEST_DISPATCH_KEY,
) -> str:
    return security.issue_agent_token(
        store,
        sub="claude_code:agentic-os",
        source="claude_code",
        session_id="sess-dispatch-1",
        workspace_id=WS_ID,
        signing_key=signing_key.encode("utf-8"),
        scopes=list(scopes),
    )


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# ① 認証なし / 不正リクエストの拒否
# ---------------------------------------------------------------------------


def test_rejects_missing_bearer_token(client, store) -> None:
    resp = client.post("/api/dispatch/headless", json={"prompt": "hello"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_rejects_token_signed_with_the_wrong_key(client, store) -> None:
    """AGENTIC_OS_DISPATCH_KEY ではなく既存鍵（HH_AGENT_TOKEN_SIGNING_KEY）で
    署名されたトークンは 401（署名鍵の名前空間が分離されていることの検証）。"""
    wrong = dispatch_token(store, signing_key=TEST_AGENT_SIGNING_KEY)
    resp = client.post(
        "/api/dispatch/headless", json={"prompt": "hello"}, headers=auth(wrong)
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_rejects_token_without_dispatch_scope(client, store) -> None:
    """正しい鍵で署名されていても dispatch スコープが無ければ 403。"""
    token = dispatch_token(store, scopes=("publish",))
    resp = client.post(
        "/api/dispatch/headless", json={"prompt": "hello"}, headers=auth(token)
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_rejects_empty_prompt(client, store) -> None:
    token = dispatch_token(store)
    resp = client.post("/api/dispatch/headless", json={"prompt": ""}, headers=auth(token))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_rejects_missing_prompt_field(client, store) -> None:
    token = dispatch_token(store)
    resp = client.post("/api/dispatch/headless", json={}, headers=auth(token))
    assert resp.status_code == 400


def test_rejects_malformed_json_body(client, store) -> None:
    token = dispatch_token(store)
    resp = client.post(
        "/api/dispatch/headless", content=b"not json", headers=auth(token)
    )
    assert resp.status_code == 400


def test_rejects_when_dispatch_key_is_unset(monkeypatch, store, secret_env, client) -> None:
    """AGENTIC_OS_DISPATCH_KEY 未設定でも Hub は起動するが、検証は fail-closed で 401。"""
    monkeypatch.delenv(hub_config.AGENTIC_OS_DISPATCH_KEY, raising=False)
    token = dispatch_token(store)
    resp = client.post(
        "/api/dispatch/headless", json={"prompt": "hello"}, headers=auth(token)
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# ② 正常系: hermes_cli 呼び出しが期待通りの引数で行われる（Popen をモック）
# ---------------------------------------------------------------------------


class FakePopen:
    """`subprocess.Popen` の最小模倣。

    本物の hermes と同様に `--usage-file` へ JSON を書き、stdout へ最終応答を
    返す。起動引数・環境・cwd は `FakePopen.last` に記録され、テストから
    検証できる。`communicate(timeout=...)` を正しく受けられるよう
    `timeout` 引数を実装している。

    ハンドラは communicate 後に一時 HERMES_HOME を削除してしまうため、
    一時ディレクトリの中身（config.yaml・プラグイン配置）の検証は
    communicate() 実行時点（ディレクトリ生存中）に `captured` へ保存して
    行う。
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
        # 一時 HERMES_HOME の中身を削除前に保存（config.yaml / プラグイン）。
        hermes_home = Path(self.kwargs["cwd"])
        self.captured = {
            "config_yaml": (hermes_home / "config.yaml").read_text(encoding="utf-8"),
            "plugin_init": (hermes_home / "plugins" / "corpus2skill" / "__init__.py").is_file(),
        }
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


def test_happy_path_invokes_hermes_with_expected_args(client, store, monkeypatch) -> None:
    monkeypatch.setattr(dispatch.subprocess, "Popen", FakePopen)
    token = dispatch_token(store)

    resp = client.post(
        "/api/dispatch/headless", json={"prompt": "hello agent"}, headers=auth(token)
    )

    # レスポンス形状: {"response": ..., "session_id": ...}
    assert resp.status_code == 200
    body = resp.json()
    assert body["response"] == "final answer"
    assert body["session_id"] == "sess-abc123"

    # --- 呼び出し引数の検証 ---
    popen = FakePopen.last
    assert popen is not None
    # python -m hermes_cli.main -z <prompt> --usage-file <path>
    assert popen.cmd[0] == sys.executable
    assert "-z" in popen.cmd
    assert popen.cmd[popen.cmd.index("-z") + 1] == "hello agent"
    assert "--usage-file" in popen.cmd

    # --- 完全ステートレス: HERMES_HOME は毎回の一時ディレクトリ ---
    hermes_home = Path(popen.kwargs["env"]["HERMES_HOME"])
    assert popen.kwargs["cwd"] == str(hermes_home)
    assert hermes_home.parent == Path(tempfile.gettempdir())
    assert hermes_home.name.startswith("hh-agent-dispatch-")
    # usage-file は HERMES_HOME 配下
    usage_i = popen.cmd.index("--usage-file")
    assert Path(popen.cmd[usage_i + 1]).parent == hermes_home

    # --- 一時 config.yaml: memory.provider: corpus2skill が書かれている ---
    # （中身は communicate() 時点で FakePopen.captured に保存済み）
    cfg = yaml.safe_load(popen.captured["config_yaml"])
    assert cfg["memory"]["provider"] == "corpus2skill"

    # --- Corpus2Skill プラグインが user 層（$HERMES_HOME/plugins/）へ配置されている ---
    assert popen.captured["plugin_init"] is True

    # --- レスポンス後は一時ディレクトリごと削除されている（使い捨て） ---
    assert not hermes_home.exists()


def test_env_carries_corpus2skill_api_key(monkeypatch, store, client) -> None:
    """C2S_API_KEY（Hub Secret）が CORPUS2SKILL_API_KEY（プラグインが読む変数名）
    としてサブプロセス環境へ翻訳される。値自体は検証せず、キー存在と非空のみ。"""
    monkeypatch.setenv(hub_config.C2S_API_KEY, "c2s-test-key-value")
    monkeypatch.setattr(dispatch.subprocess, "Popen", FakePopen)
    token = dispatch_token(store)

    resp = client.post(
        "/api/dispatch/headless", json={"prompt": "hello"}, headers=auth(token)
    )
    assert resp.status_code == 200
    env = FakePopen.last.kwargs["env"]
    assert env["CORPUS2SKILL_API_KEY"] == "c2s-test-key-value"


# ---------------------------------------------------------------------------
# ③ タイムアウト処理（504）
# ---------------------------------------------------------------------------


class TimeoutPopen:
    """communicate が TimeoutExpired を送出する Popen の模倣（実タイムアウト経路）。"""

    pid = 9999

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs

    def communicate(self, timeout=None):
        raise subprocess.TimeoutExpired(self.cmd, timeout)

    def wait(self) -> int:
        return -9


def test_timeout_is_reported_as_504(client, store, monkeypatch) -> None:
    """Popen.communicate の TimeoutExpired → プロセスグループ kill → 504。"""
    killed: list[int] = []

    def fake_kill(proc):
        killed.append(proc.pid)

    monkeypatch.setattr(dispatch.subprocess, "Popen", TimeoutPopen)
    monkeypatch.setattr(dispatch, "_kill_process_tree", fake_kill)
    token = dispatch_token(store)

    resp = client.post(
        "/api/dispatch/headless", json={"prompt": "hello"}, headers=auth(token)
    )
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "GATEWAY_TIMEOUT"
    assert resp.json()["error"]["retryable"] is True
    assert killed == [9999], "タイムアウト時にプロセスツリーが kill されていない"


def test_hermes_failure_is_reported_as_500(client, store, monkeypatch) -> None:
    """oneshot の exit code != 0 は 500 HERMES_RUN_FAILED（stderr は漏らさない）。"""

    class FailPopen:
        returncode = 1
        pid = 1234

        def __init__(self, cmd, **kwargs):
            pass

        def communicate(self, timeout=None):
            return ("", "agent failed: boom")

        def wait(self) -> int:
            return 1

    monkeypatch.setattr(dispatch.subprocess, "Popen", FailPopen)
    token = dispatch_token(store)

    resp = client.post(
        "/api/dispatch/headless", json={"prompt": "hello"}, headers=auth(token)
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "HERMES_RUN_FAILED"
    assert "boom" not in json.dumps(body), "stderr の中身を応答へ漏らしてはならない"


# ---------------------------------------------------------------------------
# レート制限（高コスト実行の無制限化防止）
# ---------------------------------------------------------------------------


def test_rate_limit_applies_per_subject(client, store, monkeypatch) -> None:
    monkeypatch.setattr(dispatch.subprocess, "Popen", FakePopen)
    token = dispatch_token(store)

    for _ in range(dispatch.DISPATCH_RATE_LIMIT):
        resp = client.post(
            "/api/dispatch/headless", json={"prompt": "hello"}, headers=auth(token)
        )
        assert resp.status_code == 200

    resp = client.post(
        "/api/dispatch/headless", json={"prompt": "hello"}, headers=auth(token)
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMITED"
