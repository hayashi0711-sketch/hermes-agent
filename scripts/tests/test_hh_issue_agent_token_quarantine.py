"""`scripts/hh_issue_agent_token.py` の quarantine_read_token.json 発行の単体テスト。

`main()` が3本目の `quarantine_read_token.json`（scopes=["quarantine_read"]、
S-08b `--remote` モード用）を発行することを、`security.issue_agent_token` と
`store` を monkeypatch した上で検証する。本番 Modal への書き込み・
`.hh-secret.env` 等の実秘密ファイルの読み書きは一切行わない。

実行: ``pytest scripts/tests/test_hh_issue_agent_token_quarantine.py``
（`pyproject.toml` の `testpaths = ["tests"]` に含まれないため、明示パスで
起動する）。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def token_module(monkeypatch, tmp_path):
    """`hh_issue_agent_token` を SECRET_ENV_PATH=tmp の状態で再 import する。

    `test_hh_issue_agent_token_ntfy.py` の同名フィクスチャと同じ
    モジュール再importパターン。モジュールは import 時に
    `SECRET_ENV_PATH = REPO_ROOT / ".hh-secret.env"` を確定するため、
    各テストで `tmp_path` を向いた秘密ファイルを作るにはモジュールを
    再読込する必要がある。実ファイル（`.hh-secret.env`）を読ませない
    防御層として、空のダミーファイルを向けておく。
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


def _run_main_patched(token_module, monkeypatch, tmp_path):
    """`main()` を本番Modal・実秘密ファイルに触れない形で実行し、呼び出し記録を返す。

    - `HH_AGENT_TOKEN_SIGNING_KEY` にダミー値を注入（`.hh-secret.env` 非依存）
    - `HH_AGENT_HOME` を `tmp_path` へ monkeypatch（本番 `~/.hh-agent/` 無書き込み）
    - `store` をダミーオブジェクトへ差し替え
    - `security.issue_agent_token` を記録用フェイクへ差し替え
    （実装は `security.issue_agent_token` をモジュール属性として参照するため、
    差し替えは `security` モジュール側の属性に対して行う）

    戻り値: `[(store_arg, kwargs), ...]`（発行呼び出しの記録、呼び出し順）。
    """
    monkeypatch.setenv("HH_AGENT_TOKEN_SIGNING_KEY", "test-signing-key")
    monkeypatch.setattr(token_module, "HH_AGENT_HOME", tmp_path)

    dummy_store = object()
    monkeypatch.setattr(token_module, "store", dummy_store)

    calls: list[tuple[object, dict]] = []

    def fake_issue_agent_token(store_arg, **kwargs):
        calls.append((store_arg, kwargs))
        return f"token-{len(calls)}"

    monkeypatch.setattr(
        token_module.security, "issue_agent_token", fake_issue_agent_token
    )

    assert token_module.main() == 0
    return calls, dummy_store


def test_main_issues_quarantine_read_token(token_module, monkeypatch, tmp_path):
    """3回目の発行が quarantine_read_token（scopes=["quarantine_read"]）であること。

    - `security.issue_agent_token` が合計3回呼ばれる
    - 3回目の `sub` が "hh-skill-promote-remote"
    - 3回目の `scopes` が `security.SCOPE_QUARANTINE_READ`（"quarantine_read"）
    - `quarantine_read_token.json`（`{"token": "..."}` 形式）が tmp 配下に書かれる
    """
    calls, dummy_store = _run_main_patched(token_module, monkeypatch, tmp_path)

    # 3本すべて発行される
    assert len(calls) == 3

    # 3回とも同じ署名鍵が渡されていること（回帰確認）
    # monkeypatch した HH_AGENT_TOKEN_SIGNING_KEY="test-signing-key" を
    # _load_signing_key() が b"test-signing-key" として渡すことを固定する
    for _, call_kwargs in calls:
        assert call_kwargs["signing_key"] == b"test-signing-key"

    # 3回目 = quarantine_read トークン
    store_arg, kwargs = calls[2]
    assert store_arg is dummy_store  # 本番 Modal の store には触れていない
    assert kwargs["sub"] == "hh-skill-promote-remote"
    assert token_module.security.SCOPE_QUARANTINE_READ == "quarantine_read"
    assert kwargs["scopes"] == [token_module.security.SCOPE_QUARANTINE_READ]

    # 3つ目の JSON ファイルが実際に書かれる（HH_AGENT_HOME は tmp_path）
    quarantine_path = tmp_path / "quarantine_read_token.json"
    assert quarantine_path.is_file()
    assert json.loads(quarantine_path.read_text(encoding="utf-8")) == {
        "token": "token-3"
    }


def test_main_writes_all_three_token_files(token_module, monkeypatch, tmp_path):
    """agent_token.json / distill_token.json / quarantine_read_token.json の3本が書かれる。

    本番の `~/.hh-agent/` には一切書き込まない（HH_AGENT_HOME は tmp_path）。
    """
    _run_main_patched(token_module, monkeypatch, tmp_path)

    expected = {
        "agent_token.json": "token-1",
        "distill_token.json": "token-2",
        "quarantine_read_token.json": "token-3",
    }
    for name, token in expected.items():
        path = tmp_path / name
        assert path.is_file(), f"{name} が書かれていない"
        assert json.loads(path.read_text(encoding="utf-8")) == {"token": token}


def test_first_two_issuance_calls_unchanged(token_module, monkeypatch, tmp_path):
    """既存2本（agent / distill）の発行内容が変わっていないこと（回帰確認）。

    - 1回目: レガシーデフォルト（scopes 未指定）・sub="claude_code:desktop-haruki"
    - 2回目: scopes=["publish"]・sub="hh-distill-worker"
    """
    calls, dummy_store = _run_main_patched(token_module, monkeypatch, tmp_path)

    # 1回目: 承認フロー用（scopes 省略 = レガシーデフォルト）
    store_arg, kwargs = calls[0]
    assert store_arg is dummy_store
    assert kwargs["sub"] == "claude_code:desktop-haruki"
    assert kwargs.get("scopes") is None

    # 2回目: Skill Distiller 用
    store_arg, kwargs = calls[1]
    assert store_arg is dummy_store
    assert kwargs["sub"] == "hh-distill-worker"
    assert kwargs["scopes"] == [token_module.security.SCOPE_PUBLISH]
