"""`modal_hub/core/config.py` — Modal Secret の読み取り。

親設計書 §6「Secret `hh-agent-secret` のキー」と、
「Secret の実値はコード・Obsidian・Git に一切書かない」（§5.4）。
"""

from __future__ import annotations

import inspect

import pytest

from modal_hub.core import config


def test_all_design_doc_secret_keys_are_declared() -> None:
    """親設計書 §6 の表と 1 対 1（dispatch タスク追加分のみ §6 外の追記）。"""
    assert set(config.ALL_SECRET_KEYS) == {
        "HH_AGENT_TOKEN_SIGNING_KEY",
        "HH_AGENT_TOKEN_SIGNING_KEY_PREV",
        "HH_PWA_SESSION_KEY",
        "HH_PAIRING_CODE",
        "NTFY_TOPIC",
        "NTFY_TOKEN",
        "ANTHROPIC_API_KEY",
        "C2S_API_KEY",
        # 以下は .agentic_os_headless_dispatch_task.md の指示で追加した
        # optional キー（§6 の表には無い。未設定でも既存デプロイは起動する）。
        "AGENTIC_OS_DISPATCH_KEY",
        "AGENTIC_OS_DISPATCH_MODEL",
    }


def test_secrets_are_read_lazily_from_the_environment(monkeypatch) -> None:
    """Modal は関数呼び出し時に環境へ注入する。import 時に読むと必ず空になる。"""
    monkeypatch.setenv(config.NTFY_TOPIC, "first")
    assert config.ntfy_topic() == "first"
    monkeypatch.setenv(config.NTFY_TOPIC, "second")
    assert config.ntfy_topic() == "second"


def test_missing_required_secret_raises(monkeypatch) -> None:
    """空文字列で代用しない。フェイルクローズの signal にする。"""
    monkeypatch.delenv(config.HH_AGENT_TOKEN_SIGNING_KEY, raising=False)
    with pytest.raises(config.SecretMissingError):
        config.agent_token_signing_key()


def test_empty_string_is_treated_as_missing(monkeypatch) -> None:
    monkeypatch.setenv(config.HH_PAIRING_CODE, "")
    with pytest.raises(config.SecretMissingError):
        config.pairing_code()


def test_secret_missing_is_not_an_auth_error() -> None:
    """デプロイ不良を 401 に読み替えない（原因を隠さない）。"""
    from modal_hub.core import security

    assert issubclass(config.SecretMissingError, RuntimeError)
    assert not issubclass(config.SecretMissingError, security.SecurityError)


@pytest.mark.parametrize(
    "accessor",
    [config.agent_token_signing_key_prev, config.ntfy_token, config.anthropic_api_key, config.c2s_api_key],
)
def test_optional_secrets_return_none(monkeypatch, accessor) -> None:
    for key in config.ALL_SECRET_KEYS:
        monkeypatch.delenv(key, raising=False)
    assert accessor() is None


def test_all_required_present_self_diagnostic(monkeypatch, secret_env) -> None:
    assert config.all_required_present() is True
    monkeypatch.delenv(config.NTFY_TOPIC, raising=False)
    assert config.all_required_present() is False


def test_ntfy_token_is_optional_not_required(monkeypatch, secret_env) -> None:
    """2026-08-11 決定: 公開トピック運用では NTFY_TOKEN は空のまま。

    Hub は NTFY_TOKEN 不在を理由に起動を拒否してはならない
    （08_Handoff_Note.md。以前はここが _REQUIRED_KEYS に入っており
    Modal デプロイのたびに HubStartupError で全リクエストがクラッシュしていた）。
    """
    monkeypatch.delenv(config.NTFY_TOKEN, raising=False)
    assert config.all_required_present() is True
    assert config.ntfy_token() is None


def test_no_secret_value_is_hardcoded_in_the_module() -> None:
    """§5.4: Secret の実値をコードに書かない。"""
    source = inspect.getsource(config)
    for marker in ("sk-ant-", "sk-proj-", "ghp_", "AKIA", "xoxb-", "tk_"):
        assert marker not in source


def test_safe_repr_masks_the_tail_and_keeps_a_short_prefix() -> None:
    value = "sk-abcdefghijklmnop"  # 19 文字
    assert config.safe_repr(value) == "sk-a" + "*" * (len(value) - 4)
    assert config.safe_repr("abcd", keep=4) == "****"
    assert config.safe_repr("") == ""


def test_safe_repr_docstring_doctest_passes() -> None:
    """BUG-9（修正済み）の回帰ガード。docstring 内 doctest が実出力とずれない。"""
    import doctest

    results = doctest.testmod(config, verbose=False)
    assert results.failed == 0


def test_safe_repr_never_leaks_more_than_keep_characters() -> None:
    secret = "supersecrettokenvalue"
    for keep in range(0, 8):
        masked = config.safe_repr(secret, keep=keep)
        assert len(masked) == len(secret)
        assert masked[keep:] == "*" * (len(secret) - keep)
        assert secret not in masked


def test_config_module_has_no_side_effects() -> None:
    """環境を書き換えない・ネットワークを開かない・ストアへ触らない。"""
    import ast

    tree = ast.parse(inspect.getsource(config))
    modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert modules <= {"os", "typing", "__future__"}, modules
    assert "os.environ[" not in inspect.getsource(config)
    assert "setenv" not in inspect.getsource(config)
