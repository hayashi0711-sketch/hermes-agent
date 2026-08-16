"""`scripts/hh_issue_agent_token.py` の `load_ntfy_credentials()` の単体テスト。

`.hh-secret.env` 相当の一時ファイルを使い、`NTFY_TOPIC` / `NTFY_TOKEN` の
有無に応じた挙動を確認する。既存の `_load_signing_key()` やトークン発行
ロジックには一切触らない。

実行: ``pytest scripts/tests/test_hh_issue_agent_token_ntfy.py``
（`pyproject.toml` の `testpaths = ["tests"]` に含まれないため、明示パスで
起動する）。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def token_module(monkeypatch, tmp_path):
    """`hh_issue_agent_token` を SECRET_ENV_PATH=tmp の状態で再 import する。

    モジュールは import 時に `SECRET_ENV_PATH = REPO_ROOT / ".hh-secret.env"`
    を確定するため、各テストで `tmp_path` を向いた秘密ファイルを作るには
    モジュールを再読込する必要がある。
    """
    secret_env_path = tmp_path / ".hh-secret.env"
    secret_env_path.write_text("", encoding="utf-8")

    # 直前のテストが残した import を消す
    sys.modules.pop("hh_issue_agent_token", None)

    # モジュールが import 時に SECRET_ENV_PATH を読むため、sys.path に
    # scripts/ を入れる。REPO_ROOT 自体は modules[0] 経由で取れているので
    # ここでは scripts/ のみ追加。
    scripts_dir = str(REPO_ROOT / "scripts")
    added = False
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
        added = True

    try:
        mod = importlib.import_module("hh_issue_agent_token")
        # 念のため SECRET_ENV_PATH を tmp 側に明示的に向け直す
        monkeypatch.setattr(mod, "SECRET_ENV_PATH", secret_env_path)
        yield mod
    finally:
        sys.modules.pop("hh_issue_agent_token", None)
        if added:
            try:
                sys.path.remove(scripts_dir)
            except ValueError:
                pass


def _write_env(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_both_present(token_module, tmp_path):
    """`NTFY_TOPIC` と `NTFY_TOKEN` 両方あり → 両方の値が返る。"""
    secret = tmp_path / ".hh-secret.env"
    _write_env(
        secret,
        [
            "NTFY_TOPIC=hh-test-topic-1234",
            'NTFY_TOKEN="tk_test_token_abcdef"',
        ],
    )
    token_module.SECRET_ENV_PATH = secret

    topic, token = token_module.load_ntfy_credentials()
    assert topic == "hh-test-topic-1234"
    # 引用符が剥がれていること
    assert token == "tk_test_token_abcdef"


def test_topic_only(token_module, tmp_path):
    """`NTFY_TOPIC` だけ設定 → topic は値、token は None。"""
    secret = tmp_path / ".hh-secret.env"
    _write_env(secret, ["NTFY_TOPIC=hh-topic-only-9999"])
    token_module.SECRET_ENV_PATH = secret

    topic, token = token_module.load_ntfy_credentials()
    assert topic == "hh-topic-only-9999"
    assert token is None


def test_token_only(token_module, tmp_path):
    """`NTFY_TOKEN` だけ設定 → token は値、topic は None。"""
    secret = tmp_path / ".hh-secret.env"
    _write_env(secret, ["NTFY_TOKEN=tk_only_token_xxxxxx"])
    token_module.SECRET_ENV_PATH = secret

    topic, token = token_module.load_ntfy_credentials()
    assert topic is None
    assert token == "tk_only_token_xxxxxx"


def test_neither(token_module, tmp_path):
    """両方とも未設定 → (None, None)。例外を投げない。"""
    secret = tmp_path / ".hh-secret.env"
    _write_env(
        secret,
        [
            "OTHER_KEY=other_value",
            "ANTHROPIC_API_KEY=sk-test",
        ],
    )
    token_module.SECRET_ENV_PATH = secret

    topic, token = token_module.load_ntfy_credentials()
    assert topic is None
    assert token is None


def test_empty_values_become_none(token_module, tmp_path):
    """`KEY=` のような空設定値は None 扱い。"""
    secret = tmp_path / ".hh-secret.env"
    _write_env(
        secret,
        [
            "NTFY_TOPIC=",
            "NTFY_TOKEN=",
        ],
    )
    token_module.SECRET_ENV_PATH = secret

    topic, token = token_module.load_ntfy_credentials()
    assert topic is None
    assert token is None


def test_missing_file(token_module, tmp_path):
    """.hh-secret.env が無い → 例外にせず (None, None)。"""
    nonexistent = tmp_path / "does-not-exist.env"
    # 念のためファイルを作らない
    assert not nonexistent.exists()
    token_module.SECRET_ENV_PATH = nonexistent

    topic, token = token_module.load_ntfy_credentials()
    assert topic is None
    assert token is None


def test_env_var_takes_precedence(token_module, tmp_path, monkeypatch):
    """環境変数があればファイルより優先される。

    `NTFY_TOKEN` は `tests/conftest.py` の hermetic フィクスチャで
    毎回 unset されるため、テスト内では monkeypatch で明示的に再設定する。
    """
    # ファイルには別値を入れておく（環境変数が勝つことを確認）
    secret = tmp_path / ".hh-secret.env"
    _write_env(
        secret,
        [
            "NTFY_TOPIC=from-file-topic",
            "NTFY_TOKEN=from-file-token",
        ],
    )
    token_module.SECRET_ENV_PATH = secret

    monkeypatch.setenv("NTFY_TOPIC", "from-env-topic")
    monkeypatch.setenv("NTFY_TOKEN", "from-env-token")

    topic, token = token_module.load_ntfy_credentials()
    assert topic == "from-env-topic"
    assert token == "from-env-token"


def test_comments_and_blank_lines_skipped(token_module, tmp_path):
    """コメント行・空行は無視される。"""
    secret = tmp_path / ".hh-secret.env"
    _write_env(
        secret,
        [
            "# this is a comment",
            "",
            "NTFY_TOPIC=hh-clean-topic",
            "   # indented comment with no = sign at all",
            "NTFY_TOKEN=tk_clean_token",
        ],
    )
    token_module.SECRET_ENV_PATH = secret

    topic, token = token_module.load_ntfy_credentials()
    assert topic == "hh-clean-topic"
    assert token == "tk_clean_token"


def test_quotes_and_whitespace_stripped(token_module, tmp_path):
    """値の前後空白と両端の引用符は `_load_signing_key()` と同じく剥がす。"""
    secret = tmp_path / ".hh-secret.env"
    _write_env(
        secret,
        [
            "NTFY_TOPIC=  'hh-quoted-topic'  ",
            'NTFY_TOKEN=  "tk_quoted_token"  ',
        ],
    )
    token_module.SECRET_ENV_PATH = secret

    topic, token = token_module.load_ntfy_credentials()
    assert topic == "hh-quoted-topic"
    assert token == "tk_quoted_token"


def test_existing_token_issuance_unchanged(token_module, tmp_path):
    """追加が既存のトークン発行ロジックを壊していないこと（最低限の import 確認）。

    `_load_signing_key()` や `main()` の呼び出し経路は触っていないことを
    担保するスモーク。`NTFY_TOPIC` / `NTFY_TOKEN` の追加で `agent_token.json`
    / `distill_token.json` の発行に影響が出ないことを確認する。
    """
    # モジュールから必要な名前が引き続き export されている
    assert callable(token_module._load_signing_key)
    assert callable(token_module.main)
    assert callable(token_module.load_ntfy_credentials)
    # SECRET_ENV_PATH も引き続き存在する
    assert isinstance(token_module.SECRET_ENV_PATH, Path)