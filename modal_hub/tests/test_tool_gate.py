"""`hh_hooks/tool_gate.py` — 共通ツールゲート（親設計書 §4.4）。

このファイルはフックスクリプトなので、モジュールとしてロードして内部
関数を直接検証する。ネットワークは張らない。

検証の主眼:

    - **フェイルクローズの絶対原則**: Hub 到達不能・不正レスポンス・
      内部デッドライン超過・想定外の例外——すべて deny で終わること。
    - deny の「三重掛け」出力（exit 2 ＋ stderr ＋ 両ホストで通る stdout JSON）。
    - 時計の規則（§4.4 の 5 項目）: monotonic 予算・HTTP タイムアウトの
      クランプ・未来 mtime の拒否。
    - 標準ライブラリのみ（`requests`/`httpx` を import しない）。
    - バイパスファイルの署名・TTL・未来時刻。
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path

import pytest


def _load_fresh_tool_gate(repo_root, unique_name: str):
    """`hh_hooks/tool_gate.py` を新しいモジュール名で毎回ゼロから import する。

    実運用の `tool_gate.py` は「フック呼び出し 1 回 ＝ 新規 Python プロセス
    1 個」で動く（INSTALL.md の `command: python .../tool_gate.py` が毎回
    新規起動される）。テストプロセス内で「2 つの独立したフック呼び出し」を
    忠実に再現するには、モジュールを毎回新しい名前で再 import し、モジュール
    レベルの `_PROCESS_INSTANCE_ID`（DEFECT 1 の修正）が import のたびに
    再評価されることを利用する。`tool_gate` フィクスチャ（module scope）を
    使い回すテストとは別に、この関数は「別プロセス」を模擬したいテスト専用。
    """
    path = repo_root / "hh_hooks" / "tool_gate.py"
    for extra in (str(repo_root), str(repo_root / "hh_hooks")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool_gate(repo_root):
    """`hh_hooks/tool_gate.py` を一意な名前でロードする。"""
    return _load_fresh_tool_gate(repo_root, "hh_tool_gate_under_test")


@pytest.fixture()
def hh_home(monkeypatch, tmp_path):
    """`%USERPROFILE%\\.hh-agent` を tmp へ隔離する（実ユーザーの状態を読まない）。"""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    home = tmp_path / ".hh-agent"
    home.mkdir()
    return home


# ===========================================================================
# 標準ライブラリのみ（性能要件。親設計書 §4.4）
# ===========================================================================


def test_tool_gate_imports_only_stdlib(repo_root) -> None:
    """`requests` / `httpx` を import しない。HTTP は `urllib.request`。

    全ツール呼び出しごとに Python プロセスが起動するため、重い依存が
    そのまま 200ms 予算を食い潰す。
    """
    tree = ast.parse((repo_root / "hh_hooks" / "tool_gate.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names
    } | {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    third_party = imported & {"requests", "httpx", "aiohttp", "yaml", "pydantic", "modal", "fastapi"}
    assert third_party == set(), f"サードパーティ製ライブラリを import している: {third_party}"
    assert "urllib" in imported


def test_time_budget_constants_match_the_design_doc(tool_gate) -> None:
    """D-13: 猶予 150 / 内部デッドライン 170 / ホストタイムアウト 200。

    内部デッドラインをホストタイムアウトより 30 秒短くすることで、
    「deny を返す前にフックが強制終了される」事故を防ぐ。
    """
    assert tool_gate.INTERNAL_DEADLINE_SECONDS == 170.0
    assert tool_gate.POLL_INTERVAL_SECONDS == 5.0
    assert tool_gate.MEDIUM_NOTIFY_TIMEOUT_SECONDS == 0.2
    assert tool_gate.INTERNAL_DEADLINE_SECONDS < 200
    assert tool_gate.INTERNAL_DEADLINE_SECONDS > 150


def test_local_waiting_uses_monotonic_not_wall_clock(repo_root) -> None:
    """§4.4 規則 1: wall clock だと NTP 同期やスリープ復帰で予算が飛ぶ。"""
    source = (repo_root / "hh_hooks" / "tool_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body_calls = [
        ast.unparse(n)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("monotonic", "time")
    ]
    assert "time.monotonic()" in body_calls, "monotonic による予算計測が無い"


def test_retry_policy_matches_the_spec(tool_gate) -> None:
    """§1.1: 最大 3 回、初回 0.5 秒・以後 2 倍・上限 4 秒。"""
    assert tool_gate._RETRY_MAX_ATTEMPTS == 3
    assert tool_gate._RETRY_BASE_DELAY_SECONDS == 0.5
    assert tool_gate._RETRY_MAX_DELAY_SECONDS == 4.0


def test_payload_and_request_size_limits(tool_gate) -> None:
    assert tool_gate._PAYLOAD_MAX_BYTES == 4096
    assert tool_gate._REQUEST_MAX_BYTES == 65536


# ===========================================================================
# deny の三重掛け出力（親設計書 §4.4）
# ===========================================================================


def test_deny_emits_exit2_stderr_and_both_wire_formats(tool_gate, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        tool_gate._emit_deny("テスト理由")
    assert exit_info.value.code == 2

    captured = capsys.readouterr()
    assert "テスト理由" in captured.err
    assert captured.err.startswith("[HH-AGENT] ")

    payload = json.loads(captured.out)
    # Claude Code（レガシー）
    assert payload["decision"] == "block"
    assert payload["reason"] == "テスト理由"
    # Hermes canonical
    assert payload["action"] == "block"
    assert payload["message"] == "テスト理由"
    # Claude Code 新形式
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_deny_always_has_a_reason(tool_gate, capsys) -> None:
    with pytest.raises(SystemExit):
        tool_gate._emit_deny("")
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"], "理由が空の deny は原因を隠す"


def test_allow_emits_nothing_and_exits_zero(tool_gate, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        tool_gate._emit_allow()
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == ""


# ===========================================================================
# フェイルクローズ: `main()` はあらゆる例外を deny へ変換する
# ===========================================================================


def test_unexpected_exception_becomes_deny(tool_gate, monkeypatch, capsys) -> None:
    monkeypatch.setattr(tool_gate, "_run", lambda _s: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(SystemExit) as exit_info:
        tool_gate.main()
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_keyboard_interrupt_also_fails_closed(tool_gate, monkeypatch, capsys) -> None:
    """`BaseException` まで拾う。「わからないので許可する」分岐は存在しない。"""
    monkeypatch.setattr(tool_gate, "_run", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(SystemExit) as exit_info:
        tool_gate.main()
    assert exit_info.value.code == 2


def test_exception_message_does_not_leak_secrets(tool_gate, monkeypatch, capsys) -> None:
    """理由文字列にトークン値・署名鍵の値を絶対に含めない。"""
    secret = "hha1.SUPERSECRETPAYLOAD.SIGNATURE"
    monkeypatch.setattr(
        tool_gate, "_run", lambda _s: (_ for _ in ()).throw(ValueError(f"token was {secret}"))
    )
    with pytest.raises(SystemExit):
        tool_gate.main()
    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err
    assert "SUPERSECRET" not in out.out


def test_no_allow_branch_exists_outside_the_three_known_paths(repo_root) -> None:
    """`_emit_allow()` の呼び出し箇所を数え、想定外の allow 経路が無いか見る。

    想定: LOW / MEDIUM（通知後）/ HIGH shell 承認成功 / バイパス有効 の 4 箇所。
    増えていたら「フェイルクローズを迂回する新しい経路」が生えた可能性がある。
    """
    tree = ast.parse((repo_root / "hh_hooks" / "tool_gate.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_emit_allow"
    ]
    assert len(calls) == 4, f"_emit_allow の呼び出しが {len(calls)} 箇所ある（想定 4）"


# ===========================================================================
# HTTP 呼び出しの予算クランプ（§4.4 規則 2）
# ===========================================================================


def test_http_timeout_is_clamped_to_the_remaining_budget(tool_gate, monkeypatch) -> None:
    """規則 2: 明示的な `timeout=` を渡し、残り予算でクランプする。

    これを怠ると、内部デッドラインが 170 秒を判定していても最後の HTTP
    要求が返らないまま 200 秒を超え、deny JSON を返せない。
    """
    seen: list[float] = []

    def fake_request(method, url, body, token, timeout_seconds):
        seen.append(timeout_seconds)
        return 200, {"ok": True}, None

    monkeypatch.setattr(tool_gate, "_http_request", fake_request)
    deadline = time.monotonic() + 1.5  # 残り 1.5 秒しかない
    tool_gate._call_hub("GET", "/x", None, "tok", "https://hub", deadline)
    assert seen and seen[0] <= 1.5


def test_no_request_is_attempted_once_the_budget_is_gone(tool_gate, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(tool_gate, "_http_request", lambda *a, **k: calls.append(a) or (200, {}, None))
    outcome = tool_gate._call_hub("GET", "/x", None, "tok", "https://hub", time.monotonic() - 1)
    assert calls == []
    assert outcome.ok is False


def test_http_timeout_never_exceeds_the_hard_cap(tool_gate, monkeypatch) -> None:
    seen: list[float] = []
    monkeypatch.setattr(
        tool_gate, "_http_request", lambda m, u, b, t, timeout_seconds: seen.append(timeout_seconds) or (200, {}, None)
    )
    tool_gate._call_hub("GET", "/x", None, "tok", "https://hub", time.monotonic() + 10_000)
    assert seen[0] <= tool_gate._HTTP_MAX_TIMEOUT_SECONDS


# ===========================================================================
# フェイルクローズ: Hub 到達不能・不正レスポンス
# ===========================================================================


def test_connection_errors_are_retried_then_fail_closed(tool_gate, monkeypatch) -> None:
    attempts: list = []
    monkeypatch.setattr(tool_gate.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        tool_gate,
        "_http_request",
        lambda *a, **k: (attempts.append(1), (None, None, "URLError: unreachable"))[1],
    )
    outcome = tool_gate._call_hub("GET", "/x", None, "tok", "https://hub", time.monotonic() + 60)
    assert outcome.ok is False
    assert len(attempts) == tool_gate._RETRY_MAX_ATTEMPTS


def test_retryable_is_read_from_the_body_not_guessed_from_the_status(tool_gate, monkeypatch) -> None:
    """§1.1:「`retryable` の判定は応答本文の値を正とする」。"""
    calls: list = []

    def fake(method, url, body, token, timeout_seconds):
        calls.append(1)
        # ステータスは 500（普通なら retryable に見える）だが本文は false。
        return 500, {"error": {"code": "INTERNAL_ERROR", "message": "x", "retryable": False}}, None

    monkeypatch.setattr(tool_gate.time, "sleep", lambda _s: None)
    monkeypatch.setattr(tool_gate, "_http_request", fake)
    outcome = tool_gate._call_hub("POST", "/x", {}, "tok", "https://hub", time.monotonic() + 60)
    assert outcome.ok is False
    assert len(calls) == 1, "retryable=false なのにリトライした"


def test_retryable_true_body_is_retried(tool_gate, monkeypatch) -> None:
    calls: list = []

    def fake(method, url, body, token, timeout_seconds):
        calls.append(1)
        if len(calls) < 3:
            return 429, {"error": {"code": "RATE_LIMITED", "message": "x", "retryable": True}}, None
        return 200, {"approval_id": "a"}, None

    monkeypatch.setattr(tool_gate.time, "sleep", lambda _s: None)
    monkeypatch.setattr(tool_gate, "_http_request", fake)
    outcome = tool_gate._call_hub("POST", "/x", {}, "tok", "https://hub", time.monotonic() + 60)
    assert outcome.ok is True
    assert len(calls) == 3


def test_unparseable_body_fails_closed(tool_gate, monkeypatch) -> None:
    """§9 落とし穴 15: 想定外のレスポンス形状は素通りさせない。"""
    monkeypatch.setattr(tool_gate.time, "sleep", lambda _s: None)
    monkeypatch.setattr(tool_gate, "_http_request", lambda *a, **k: (503, None, None))
    outcome = tool_gate._call_hub("GET", "/x", None, "tok", "https://hub", time.monotonic() + 60)
    assert outcome.ok is False


# ===========================================================================
# stdin の検証
# ===========================================================================


@pytest.mark.parametrize("raw", ["", "   ", "not json", "[]", '"str"', "123"])
def test_bad_stdin_is_rejected(tool_gate, monkeypatch, raw) -> None:
    import io

    monkeypatch.setattr(tool_gate.sys, "stdin", io.StringIO(raw))
    with pytest.raises((ValueError, json.JSONDecodeError)):
        tool_gate._read_stdin_request()


def test_missing_tool_name_denies(tool_gate, monkeypatch, capsys) -> None:
    import io

    monkeypatch.setattr(tool_gate.sys, "stdin", io.StringIO(json.dumps({"tool_input": {}})))
    with pytest.raises(SystemExit) as exit_info:
        tool_gate._run(time.monotonic())
    assert exit_info.value.code == 2
    assert "tool_name" in capsys.readouterr().err


def test_non_dict_tool_input_denies(tool_gate, monkeypatch, capsys) -> None:
    import io

    payload = json.dumps({"tool_name": "Bash", "tool_input": "rm -rf /"})
    monkeypatch.setattr(tool_gate.sys, "stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exit_info:
        tool_gate._run(time.monotonic())
    assert exit_info.value.code == 2


def test_missing_risk_module_denies(tool_gate, monkeypatch, capsys) -> None:
    """`hh_hooks/risk.py` が無ければ Hub へ行かず即 deny。"""
    import io

    monkeypatch.setattr(tool_gate, "_load_generated_module", lambda name: None)
    monkeypatch.setattr(
        tool_gate.sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    )
    with pytest.raises(SystemExit) as exit_info:
        tool_gate._run(time.monotonic())
    assert exit_info.value.code == 2
    assert "risk" in capsys.readouterr().err


def test_unknown_risk_level_denies(tool_gate, monkeypatch, capsys) -> None:
    """`classify()` が想定外のレベルを返したら allow しない。"""
    import io
    import types

    fake_risk = types.SimpleNamespace(
        classify=lambda n, i: types.SimpleNamespace(level="UNKNOWN", rule_id="x", reason="", normalized_target=None),
        _alias_lookup=lambda n: "shell",
    )
    monkeypatch.setattr(tool_gate, "_load_generated_module", lambda name: fake_risk if name == "risk" else None)
    monkeypatch.setattr(
        tool_gate.sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    )
    with pytest.raises(SystemExit) as exit_info:
        tool_gate._run(time.monotonic())
    assert exit_info.value.code == 2


# ===========================================================================
# HIGH パスのフェイルクローズ（Hub 設定・トークン・canonical モジュール）
# ===========================================================================


def _risk(level="HIGH", rule_id="force_push", target="git push --force"):
    import types

    return types.SimpleNamespace(level=level, rule_id=rule_id, reason="", normalized_target=target)


def test_high_shell_denies_when_canonical_module_is_missing(tool_gate, monkeypatch, hh_home) -> None:
    monkeypatch.setattr(tool_gate, "_require_canonical_module", lambda: None)
    reason = tool_gate._handle_high_shell(
        _risk(), "Bash", {"command": "git push --force"}, "s", str(hh_home), "cid", time.monotonic() + 60
    )
    assert reason is not None and "canonical" in reason


def test_high_shell_denies_when_hub_url_is_unset(tool_gate, monkeypatch, hh_home) -> None:
    import types

    monkeypatch.delenv("HH_AGENT_HUB_URL", raising=False)
    monkeypatch.setattr(tool_gate, "_require_canonical_module", lambda: types.SimpleNamespace(canonical_json=lambda p: b"{}"))
    reason = tool_gate._handle_high_shell(
        _risk(), "Bash", {"command": "git push --force"}, "s", str(hh_home), "cid", time.monotonic() + 60
    )
    assert reason is not None and "Hub" in reason


def test_high_shell_denies_when_token_is_missing(tool_gate, monkeypatch, hh_home) -> None:
    import types

    monkeypatch.setenv("HH_AGENT_HUB_URL", "https://hub.example")
    monkeypatch.setattr(tool_gate, "_require_canonical_module", lambda: types.SimpleNamespace(canonical_json=lambda p: b"{}"))
    reason = tool_gate._handle_high_shell(
        _risk(), "Bash", {"command": "git push --force"}, "s", str(hh_home), "cid", time.monotonic() + 60
    )
    assert reason is not None and "認証" in reason


def test_expired_token_is_rejected_locally_without_a_hub_round_trip(tool_gate, hh_home) -> None:
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1}).encode()).rstrip(b"=").decode()
    (hh_home / "agent_token.json").write_text(
        json.dumps({"token": f"hha1.{payload}.sig"}), encoding="utf-8"
    )
    with pytest.raises(tool_gate.AgentTokenError, match="期限切れ"):
        tool_gate._load_agent_token()


def test_token_error_messages_never_contain_the_token(tool_gate, hh_home) -> None:
    (hh_home / "agent_token.json").write_text(
        json.dumps({"token": "hha1.NOTBASE64!!!.SECRETSIGNATURE"}), encoding="utf-8"
    )
    with pytest.raises(tool_gate.AgentTokenError) as exc:
        tool_gate._load_agent_token()
    assert "SECRETSIGNATURE" not in str(exc.value)


@pytest.mark.parametrize(
    "content", ['{"token": "not-hha1"}', '{"token": "hha1.only-two"}', "{}", '{"token": 123}']
)
def test_malformed_token_file_is_rejected(tool_gate, hh_home, content) -> None:
    (hh_home / "agent_token.json").write_text(content, encoding="utf-8")
    with pytest.raises(tool_gate.AgentTokenError):
        tool_gate._load_agent_token()


def test_write_edit_high_is_denied_locally_without_contacting_the_hub(tool_gate, monkeypatch, capsys) -> None:
    """親設計書 §4.3 が許容した縮退。「検証が甘いまま実行する」より安全側。

    Hub へ 1 度も接続しないことを、`_call_hub` の tripwire で確認する。
    """
    import io
    import types

    def tripwire(*args, **kwargs):
        raise AssertionError("Write/Edit の HIGH で Hub へ接続した")

    monkeypatch.setattr(tool_gate, "_call_hub", tripwire)
    fake_risk = types.SimpleNamespace(
        classify=lambda n, i: _risk(rule_id="secret_path", target="C:/x/.env"),
        _alias_lookup=lambda n: "write",
    )
    monkeypatch.setattr(tool_gate, "_load_generated_module", lambda name: fake_risk if name == "risk" else None)
    monkeypatch.setattr(tool_gate, "_check_bypass", lambda: None)
    monkeypatch.setattr(
        tool_gate.sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Write", "tool_input": {"file_path": "C:/x/.env"}})),
    )
    with pytest.raises(SystemExit) as exit_info:
        tool_gate._run(time.monotonic())
    assert exit_info.value.code == 2
    assert "Write/Edit" in capsys.readouterr().err


def test_unknown_tool_is_denied_without_contacting_the_hub(tool_gate, monkeypatch, capsys) -> None:
    import io
    import types

    monkeypatch.setattr(
        tool_gate, "_call_hub", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Hub へ接続した"))
    )
    fake_risk = types.SimpleNamespace(
        classify=lambda n, i: _risk(rule_id="unknown_tool", target=None), _alias_lookup=lambda n: None
    )
    monkeypatch.setattr(tool_gate, "_load_generated_module", lambda name: fake_risk if name == "risk" else None)
    monkeypatch.setattr(tool_gate, "_check_bypass", lambda: None)
    monkeypatch.setattr(
        tool_gate.sys, "stdin", io.StringIO(json.dumps({"tool_name": "mcp__x__y", "tool_input": {}}))
    )
    with pytest.raises(SystemExit) as exit_info:
        tool_gate._run(time.monotonic())
    assert exit_info.value.code == 2
    assert "unknown_tool" in capsys.readouterr().err


# ===========================================================================
# HIGH shell: poll の結果に応じた分岐
# ===========================================================================


@pytest.fixture()
def high_shell_env(tool_gate, monkeypatch, hh_home):
    import base64
    import types

    monkeypatch.setenv("HH_AGENT_HUB_URL", "https://hub.example")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode()).rstrip(b"=").decode()
    (hh_home / "agent_token.json").write_text(json.dumps({"token": f"hha1.{payload}.sig"}), encoding="utf-8")
    monkeypatch.setattr(
        tool_gate, "_require_canonical_module", lambda: types.SimpleNamespace(canonical_json=lambda p: b"{}")
    )
    monkeypatch.setattr(tool_gate.time, "sleep", lambda _s: None)
    monkeypatch.setattr(tool_gate, "_compute_workspace_id", lambda cwd: "a" * 64)
    monkeypatch.setattr(tool_gate, "_compute_base_revision", lambda cwd: None)
    return hh_home


def run_high_shell(tool_gate, monkeypatch, responses, cwd):
    """`_call_hub` を台本で置き換えて HIGH shell フローを回す。"""
    script = list(responses)

    def fake_call(method, path, body, token, hub, deadline):
        for key, outcome in script:
            if key in path:
                script.remove((key, outcome))
                return outcome
        raise AssertionError(f"予期しない Hub 呼び出し: {method} {path}")

    monkeypatch.setattr(tool_gate, "_call_hub", fake_call)
    return tool_gate._handle_high_shell(
        _risk(), "Bash", {"command": "git push --force"}, "s", cwd, "cid", time.monotonic() + 60
    )


def ok(body):
    return lambda: None  # placeholder, replaced below


def test_rejected_status_denies(tool_gate, monkeypatch, high_shell_env) -> None:
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [
            ("/request", H(True, 201, {"approval_id": "a1", "notify_state": "sent"}, None)),
            ("/poll", H(True, 200, {"status": "rejected", "notify_state": "sent"}, None)),
        ],
        str(high_shell_env),
    )
    assert reason is not None and "rejected" in reason


def test_timeout_status_denies(tool_gate, monkeypatch, high_shell_env) -> None:
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [
            ("/request", H(True, 201, {"approval_id": "a1", "notify_state": "sent"}, None)),
            ("/poll", H(True, 200, {"status": "timeout", "notify_state": "sent"}, None)),
        ],
        str(high_shell_env),
    )
    assert reason is not None and "timeout" in reason


def test_notify_failed_denies_immediately(tool_gate, monkeypatch, high_shell_env) -> None:
    """§1.3/§4.3: 通知が届いていないのでユーザーは承認しようがない。即 deny。"""
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [("/request", H(True, 201, {"approval_id": "a1", "notify_state": "failed"}, None))],
        str(high_shell_env),
    )
    assert reason is not None and "通知" in reason


def test_hub_unreachable_at_request_denies(tool_gate, monkeypatch, high_shell_env) -> None:
    """親設計書 §7 の 1a 完了条件: 「Hub 停止時に deny されること」。"""
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [("/request", H(False, None, None, "URLError: unreachable"))],
        str(high_shell_env),
    )
    assert reason is not None and "フェイルクローズ" in reason


def test_hub_unreachable_at_poll_denies(tool_gate, monkeypatch, high_shell_env) -> None:
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [
            ("/request", H(True, 201, {"approval_id": "a1", "notify_state": "sent"}, None)),
            ("/poll", H(False, None, None, "timeout")),
        ],
        str(high_shell_env),
    )
    assert reason is not None and "poll" in reason


def test_claim_failure_denies(tool_gate, monkeypatch, high_shell_env) -> None:
    """`ALREADY_CLAIMED` を受け取ったクライアントは deny する（§1.4）。"""
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [
            ("/request", H(True, 201, {"approval_id": "a1", "notify_state": "sent"}, None)),
            ("/poll", H(True, 200, {"status": "approved", "notify_state": "sent"}, None)),
            ("/claim", H(False, 409, {"error": {"code": "ALREADY_CLAIMED", "retryable": False}}, "409")),
        ],
        str(high_shell_env),
    )
    assert reason is not None and "claim" in reason


def test_claim_without_granted_true_denies(tool_gate, monkeypatch, high_shell_env) -> None:
    """想定外のレスポンス形状は素通りさせない（§9 落とし穴 15）。"""
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [
            ("/request", H(True, 201, {"approval_id": "a1", "notify_state": "sent"}, None)),
            ("/poll", H(True, 200, {"status": "approved", "notify_state": "sent"}, None)),
            ("/claim", H(True, 200, {"granted": "yes"}, None)),
        ],
        str(high_shell_env),
    )
    assert reason is not None


# ===========================================================================
# DEFECT 2: claim 応答の成功スキーマ厳密検証
#
# `lease_id` は「このプロセスが実行権を勝ち取った」唯一の証拠（§1.4）。
# `granted` が緩く判定される、または `lease_id` が検証されずに allow へ
# 抜けるのは、証拠なしで実行を許すことと同義であり CRITICAL。
# ===========================================================================


def test_claim_granted_true_with_no_lease_id_key_at_all_denies(tool_gate, monkeypatch, high_shell_env) -> None:
    """`{"granted": true}` に `lease_id` キー自体が無い → DENY。"""
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [
            ("/request", H(True, 201, {"approval_id": "a1", "notify_state": "sent"}, None)),
            ("/poll", H(True, 200, {"status": "approved", "notify_state": "sent"}, None)),
            ("/claim", H(True, 200, {"granted": True}, None)),
        ],
        str(high_shell_env),
    )
    assert reason is not None and "lease_id" in reason


@pytest.mark.parametrize(
    "lease_id",
    ["", None, 12345, 1.5, ["not", "a", "string"], {"nested": "dict"}, "not-a-valid-uuid", "12345678"],
    ids=["empty_string", "none", "int", "float", "list", "dict", "non_uuid_string", "short_string"],
)
def test_claim_with_invalid_lease_id_denies(tool_gate, monkeypatch, high_shell_env, lease_id) -> None:
    """`lease_id` が空・None・非文字列・UUID として不正な文字列 → いずれも DENY。"""
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [
            ("/request", H(True, 201, {"approval_id": "a1", "notify_state": "sent"}, None)),
            ("/poll", H(True, 200, {"status": "approved", "notify_state": "sent"}, None)),
            ("/claim", H(True, 200, {"granted": True, "lease_id": lease_id}, None)),
        ],
        str(high_shell_env),
    )
    assert reason is not None, f"lease_id={lease_id!r} なのに allow された"


@pytest.mark.parametrize(
    "granted_value",
    ["true", "True", 1, 1.0, [True]],
    ids=["str_true", "str_True", "int_1", "float_1", "list_true"],
)
def test_claim_granted_truthy_but_not_exact_bool_true_denies(
    tool_gate, monkeypatch, high_shell_env, granted_value
) -> None:
    """`granted` が truthy でも厳密な bool `True` でなければ DENY（暗黙変換を許さない）。

    仮に `lease_id` が有効な UUID であっても、`granted` の型が正しくなければ
    信用しない。
    """
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [
            ("/request", H(True, 201, {"approval_id": "a1", "notify_state": "sent"}, None)),
            ("/poll", H(True, 200, {"status": "approved", "notify_state": "sent"}, None)),
            ("/claim", H(True, 200, {"granted": granted_value, "lease_id": str(uuid.uuid4())}, None)),
        ],
        str(high_shell_env),
    )
    assert reason is not None, f"granted={granted_value!r} なのに allow された"


def test_claim_with_well_formed_response_allows(tool_gate, monkeypatch, high_shell_env) -> None:
    """厳密な bool True の granted ＋ 有効な UUID の lease_id → ALLOW（happy path 回帰防止）。"""
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [
            ("/request", H(True, 201, {"approval_id": "a1", "notify_state": "sent"}, None)),
            ("/poll", H(True, 200, {"status": "approved", "notify_state": "sent"}, None)),
            ("/claim", H(True, 200, {"granted": True, "lease_id": str(uuid.uuid4())}, None)),
            ("/complete", H(True, 200, {"recorded": True}, None)),
        ],
        str(high_shell_env),
    )
    assert reason is None, f"正しい形式の claim 応答なのに deny された: {reason}"


def test_missing_approval_id_in_response_denies(tool_gate, monkeypatch, high_shell_env) -> None:
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [("/request", H(True, 201, {"notify_state": "sent"}, None))],
        str(high_shell_env),
    )
    assert reason is not None and "approval_id" in reason


def test_full_approval_flow_allows(tool_gate, monkeypatch, high_shell_env) -> None:
    """happy path: `lease_id` は DEFECT 2 の修正により有効な UUID でなければ
    ならない（旧テストは非 UUID 文字列 `"l1"` を使っており、それ自体が
    修正前の甘い検証を前提にした不備だった）。"""
    H = tool_gate.HttpOutcome
    reason = run_high_shell(
        tool_gate,
        monkeypatch,
        [
            ("/request", H(True, 201, {"approval_id": "a1", "notify_state": "sent"}, None)),
            ("/poll", H(True, 200, {"status": "approved", "notify_state": "sent"}, None)),
            ("/claim", H(True, 200, {"granted": True, "lease_id": str(uuid.uuid4())}, None)),
            ("/complete", H(True, 200, {"recorded": True}, None)),
        ],
        str(high_shell_env),
    )
    assert reason is None, f"承認済みなのに deny された: {reason}"


# ===========================================================================
# 決定的な idempotency_key / claim_attempt_id（§1.2 / §1.4）
# ===========================================================================


def test_idempotency_key_is_deterministic_and_matches_the_charset(tool_gate) -> None:
    import re

    key = tool_gate._derive_idempotency_key("sess", "Bash", "C:/p", "a" * 64, "call-1")
    assert key == tool_gate._derive_idempotency_key("sess", "Bash", "C:/p", "a" * 64, "call-1")
    assert re.fullmatch(r"[A-Za-z0-9._:-]{16,128}", key), key


def test_idempotency_key_varies_with_every_input(tool_gate) -> None:
    base = ("sess", "Bash", "C:/p", "a" * 64, "call-1")
    keys = {tool_gate._derive_idempotency_key(*base)}
    for i in range(len(base)):
        variant = list(base)
        variant[i] = variant[i] + "X"
        keys.add(tool_gate._derive_idempotency_key(*variant))
    assert len(keys) == len(base) + 1


def test_claim_attempt_id_is_stable_across_retries(tool_gate) -> None:
    """§1.4:「1 回の実行意図」につき 1 つ生成し、リトライ間で変えない。"""
    key = "idem-abc"
    assert tool_gate._derive_claim_attempt_id(key) == tool_gate._derive_claim_attempt_id(key)
    assert tool_gate._derive_claim_attempt_id(key) != tool_gate._derive_claim_attempt_id("idem-abd")


# ===========================================================================
# DEFECT 1: ホストが tool call ID を供給しない場合の claim_attempt_id
#
# 「同一コマンドの内容だけから導出した attempt id」は、同一セッション内の
# 2 つの独立した並行呼び出しを区別できず、Hub 側の冪等化により同じ lease が
# 両方へ返って「1 承認で 2 回実行」される（タスク指示の DEFECT 1 参照）。
# 修正: ホストが ID を供給しない場合、フックプロセスの import 時に一度だけ
# 生成される `_PROCESS_INSTANCE_ID` をフォールバックとして使う。
# ===========================================================================


def test_host_supplied_call_id_is_still_used(tool_gate) -> None:
    """ホストが tool call ID を渡す場合は従来どおりそれを使う（回帰防止）。"""
    assert tool_gate._extract_call_id({"tool_call_id": "abc-123"}) == "abc-123"
    assert tool_gate._extract_call_id({"tool_use_id": "xyz-789"}) == "xyz-789"
    assert tool_gate._extract_call_id({"id": "id-1"}) == "id-1"
    assert tool_gate._extract_call_id({"extra": {"tool_call_id": "e-1"}}) == "e-1"
    # ホスト供給の ID はプロセスのフォールバック ID とは無関係。
    assert tool_gate._extract_call_id({"tool_call_id": "abc-123"}) != tool_gate._PROCESS_INSTANCE_ID


def test_missing_call_id_falls_back_to_the_per_process_id(tool_gate) -> None:
    """ID 無しのリクエストは、このプロセス用に一度だけ生成された ID を返す。"""
    assert tool_gate._extract_call_id({}) == tool_gate._PROCESS_INSTANCE_ID
    assert tool_gate._extract_call_id({"session_id": "s"}) == tool_gate._PROCESS_INSTANCE_ID


def test_retries_within_one_invocation_reuse_the_same_claim_attempt_id(repo_root) -> None:
    """1 回のフック呼び出し（＝ 1 プロセス）内で HTTP リトライが起きても、
    `claim_attempt_id` は変わってはならない（05_Phase1a_Spec.md §1.4）。

    「1 回の呼び出し」を、モジュールを一度だけ import した状態で複数回
    `_extract_call_id()` / 鍵導出を呼ぶことで模擬する（実プロセスでは
    HTTP リトライのたびに `_call_hub()` がループするだけで、モジュールの
    再 import は起きないため、_PROCESS_INSTANCE_ID は変わらない）。
    """
    mod = _load_fresh_tool_gate(repo_root, "hh_tool_gate_proc_single_invocation")
    request = {"tool_name": "Bash", "session_id": "s", "cwd": "C:/p"}

    call_id_attempt_1 = mod._extract_call_id(request)
    call_id_attempt_2 = mod._extract_call_id(request)  # 2回目の HTTP リトライを模擬
    call_id_attempt_3 = mod._extract_call_id(request)  # 3回目
    assert call_id_attempt_1 == call_id_attempt_2 == call_id_attempt_3

    idem_1 = mod._derive_idempotency_key("s", "Bash", "C:/p", "a" * 64, call_id_attempt_1)
    idem_2 = mod._derive_idempotency_key("s", "Bash", "C:/p", "a" * 64, call_id_attempt_2)
    assert idem_1 == idem_2

    claim_1 = mod._derive_claim_attempt_id(idem_1)
    claim_2 = mod._derive_claim_attempt_id(idem_2)
    assert claim_1 == claim_2


def test_concurrent_invocations_of_identical_command_get_different_claim_attempt_id(repo_root) -> None:
    """DEFECT 1 の核心テスト: 2 つの独立したフックプロセス（＝2 回の別々の
    モジュール import で模擬）が、まったく同じセッション・ツール・cwd・
    payload で同時に呼ばれても、`claim_attempt_id` は一致してはならない。

    一致すると Hub は 2 件目を 1 件目の冪等リトライと見なして同じ lease を
    返し、両方の hook プロセスが allow してしまう（1 回のスマホ承認で
    `rm -rf target` が 2 回実行される、というタスク指示の失敗シナリオ）。
    """
    mod_a = _load_fresh_tool_gate(repo_root, "hh_tool_gate_proc_concurrent_a")
    mod_b = _load_fresh_tool_gate(repo_root, "hh_tool_gate_proc_concurrent_b")

    # 2 つの「別プロセス」が、ホストから ID を供給されないまま
    # 完全に同一内容のツール呼び出しを処理しようとしている状況。
    request = {"tool_name": "Bash", "session_id": "s", "cwd": "C:/p"}
    call_id_a = mod_a._extract_call_id(request)
    call_id_b = mod_b._extract_call_id(request)
    assert call_id_a != call_id_b, "別プロセスなのに call_id が一致した"

    idem_a = mod_a._derive_idempotency_key("s", "Bash", "C:/p", "a" * 64, call_id_a)
    idem_b = mod_b._derive_idempotency_key("s", "Bash", "C:/p", "a" * 64, call_id_b)
    assert idem_a != idem_b, "別プロセスなのに idempotency_key が一致した"

    claim_a = mod_a._derive_claim_attempt_id(idem_a)
    claim_b = mod_b._derive_claim_attempt_id(idem_b)
    assert claim_a != claim_b, (
        "別プロセスの同一コマンドが同じ claim_attempt_id を生成した"
        "（1 承認で 2 回実行される脆弱性が再発している）"
    )


# ===========================================================================
# workspace_id / base_revision（spec §4）
# ===========================================================================


def test_workspace_id_is_sha256_of_the_realpath(tool_gate, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tool_gate, "_run_git", lambda *a, **k: None)  # Git 管理外
    expected = hashlib.sha256(str(tmp_path.resolve()).replace("\\", "/").encode("utf-8")).hexdigest()
    assert tool_gate._compute_workspace_id(str(tmp_path)) == expected


def test_workspace_id_falls_back_to_cwd_when_git_fails(tool_gate, tmp_path, monkeypatch) -> None:
    """§4: git が失敗しても **エラーにしない**（Git 管理外の作業を止めない）。"""
    monkeypatch.setattr(tool_gate, "_run_git", lambda *a, **k: None)
    assert len(tool_gate._compute_workspace_id(str(tmp_path))) == 64
    assert tool_gate._compute_base_revision(str(tmp_path)) is None


def test_workspace_id_uses_the_git_toplevel_when_available(tool_gate, tmp_path, monkeypatch) -> None:
    top = tmp_path / "repo"
    (top / "sub").mkdir(parents=True)
    monkeypatch.setattr(tool_gate, "_run_git", lambda args, cwd, **k: str(top) if "--show-toplevel" in args else None)
    assert tool_gate._compute_workspace_id(str(top / "sub")) == tool_gate._compute_workspace_id(str(top))


def test_pathstr_normalization_uses_forward_slashes(tool_gate, tmp_path) -> None:
    """§3 PathStr: realpath → `\\` を `/` へ。大文字小文字は変換しない。"""
    normalized = tool_gate._normalize_pathstr(str(tmp_path))
    assert "\\" not in normalized


# ===========================================================================
# バイパスファイル（spec §8 / §4.4 規則 5）
# ===========================================================================


def write_bypass(home: Path, *, enabled_at: float, reason: str = "hub outage", key: bytes = b"local-key"):
    (home / "local.key").write_bytes(key)
    sig = hmac.new(key, f"{enabled_at}|{reason}".encode("utf-8"), hashlib.sha256).hexdigest()
    (home / "bypass").write_text(
        json.dumps({"enabled_at": enabled_at, "reason": reason, "sig": sig}), encoding="utf-8"
    )


def test_valid_bypass_is_recognized(tool_gate, hh_home) -> None:
    write_bypass(hh_home, enabled_at=time.time() - 60)
    assert tool_gate._check_bypass() is not None


def test_bypass_expires_after_30_minutes(tool_gate, hh_home) -> None:
    write_bypass(hh_home, enabled_at=time.time() - 31 * 60)
    assert tool_gate._check_bypass() is None


def test_future_timestamp_bypass_is_rejected(tool_gate, hh_home) -> None:
    """§4.4 規則 5 / §8: 時計変更による無期限バイパスを防ぐ。"""
    write_bypass(hh_home, enabled_at=time.time() + 3600)
    assert tool_gate._check_bypass() is None


def test_unsigned_bypass_is_ignored(tool_gate, hh_home) -> None:
    (hh_home / "local.key").write_bytes(b"local-key")
    (hh_home / "bypass").write_text(
        json.dumps({"enabled_at": time.time(), "reason": "x", "sig": "deadbeef"}), encoding="utf-8"
    )
    assert tool_gate._check_bypass() is None


def test_bypass_signed_with_the_wrong_key_is_ignored(tool_gate, hh_home) -> None:
    write_bypass(hh_home, enabled_at=time.time(), key=b"attacker-key")
    (hh_home / "local.key").write_bytes(b"real-key")
    assert tool_gate._check_bypass() is None


def test_bypass_without_a_local_key_is_ignored(tool_gate, hh_home) -> None:
    (hh_home / "bypass").write_text(
        json.dumps({"enabled_at": time.time(), "reason": "x", "sig": "y"}), encoding="utf-8"
    )
    assert tool_gate._check_bypass() is None


def test_missing_bypass_file_means_no_bypass(tool_gate, hh_home) -> None:
    assert tool_gate._check_bypass() is None


def test_bypass_is_never_controlled_by_an_environment_variable(repo_root) -> None:
    """§4.4「緊急脱出口」: 環境変数では行わない。

    環境変数はエージェント自身が設定でき、プロンプトインジェクションによる
    権限昇格経路になる（Hermes が `HERMES_YOLO_MODE` を凍結しているのと
    同じ理由）。
    """
    source = (repo_root / "hh_hooks" / "tool_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    env_reads = [
        ast.unparse(n)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("get", "getenv")
        and "environ" in ast.unparse(n.func)
    ]
    for read in env_reads:
        assert not any(
            token in read.upper() for token in ("BYPASS", "YOLO", "DISABLE", "SKIP", "ALLOW")
        ), f"環境変数でゲートを無効化できる経路がある: {read}"


def test_bypass_usage_is_logged_locally_independent_of_the_hub(tool_gate, hh_home) -> None:
    """§8: Hub 障害時に使う機能なので、監査を Hub に依存させない。"""
    tool_gate._log_bypass_usage("Bash", "HIGH", "force_push")
    log = (hh_home / "bypass_audit.log").read_text(encoding="utf-8")
    entry = json.loads(log.strip().splitlines()[-1])
    assert entry["tool_name"] == "Bash" and entry["risk"] == "HIGH"


def test_bypass_warns_on_stderr_every_time(tool_gate, monkeypatch, hh_home, capsys) -> None:
    import io
    import types

    write_bypass(hh_home, enabled_at=time.time() - 60)
    fake_risk = types.SimpleNamespace(classify=lambda n, i: _risk(), _alias_lookup=lambda n: "shell")
    monkeypatch.setattr(tool_gate, "_load_generated_module", lambda name: fake_risk if name == "risk" else None)
    monkeypatch.setattr(
        tool_gate.sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push --force"}})),
    )
    with pytest.raises(SystemExit) as exit_info:
        tool_gate._run(time.monotonic())
    assert exit_info.value.code == 0
    assert "BYPASS ACTIVE" in capsys.readouterr().err


# ===========================================================================
# MEDIUM: 明示的に諦めてログに残す（親設計書 §4.4）
# ===========================================================================


def test_medium_never_blocks_execution(tool_gate, monkeypatch, hh_home) -> None:
    """MEDIUM は実行を許可する。通知に失敗しても例外を投げない。"""
    monkeypatch.setattr(
        tool_gate, "_require_canonical_module", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    tool_gate._handle_medium(_risk(level="MEDIUM"), "Bash", {"command": "npm i"}, "s", str(hh_home), "cid")


def test_medium_failure_is_written_to_the_local_log(tool_gate, monkeypatch, hh_home) -> None:
    """「黙って消えるのが最悪」（親設計書 §4.4）。"""
    monkeypatch.setattr(tool_gate, "_require_canonical_module", lambda: None)
    tool_gate._handle_medium(_risk(level="MEDIUM"), "Bash", {"command": "npm i"}, "s", str(hh_home), "cid")
    log = (hh_home / "tool_gate.log").read_text(encoding="utf-8")
    assert "MEDIUM notify skipped" in log


def test_medium_uses_a_synchronous_200ms_timeout_not_fire_and_forget(repo_root) -> None:
    """フックは短命プロセス。投げっぱなしの非同期送信はプロセス終了で破棄される。"""
    source = (repo_root / "hh_hooks" / "tool_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    medium = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_handle_medium"
    )
    body = ast.unparse(medium)
    assert "MEDIUM_NOTIFY_TIMEOUT_SECONDS" in body
    for forbidden in ("Thread", "Popen", "asyncio", "fork", "daemon"):
        assert forbidden not in body, f"MEDIUM が非同期送信 ({forbidden}) を使っている"
