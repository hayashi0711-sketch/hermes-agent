"""`scripts/hh_pwa_pair.py`（`hh pwa pair`の実体）の単体テスト。

`main()` が `security.create_pairing_offer(store)` を正しく呼び、コードを
標準出力にのみ表示する（ログファイルへは一切書かない）こと、および
`SecurityError` 時は標準エラー出力に出して非ゼロ終了することを、
`store` をダミーオブジェクトへ、`security.create_pairing_offer` をフェイク
関数へ差し替えた上で検証する。本番 Modal への書き込みは一切行わない。

実行: ``pytest scripts/tests/test_hh_pwa_pair.py``
（`pyproject.toml` の `testpaths = ["tests"]` に含まれないため、明示パスで
起動する）。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

FIXED_PAIRING_CODE = "87654321"


@pytest.fixture()
def pair_module(monkeypatch):
    """`hh_pwa_pair` を再 import してテスト対象モジュールを返す。

    `test_hh_issue_agent_token_quarantine.py` と同じ再importパターン。
    モジュール自身が import 時に `REPO_ROOT` を `sys.path` へ入れるため、
    ここでは `scripts/` のみを `sys.path` に追加すれば良い。
    """
    # 直前のテストが残した import を消す
    sys.modules.pop("hh_pwa_pair", None)

    scripts_dir = str(REPO_ROOT / "scripts")
    added = False
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
        added = True

    try:
        mod = importlib.import_module("hh_pwa_pair")
        yield mod
    finally:
        sys.modules.pop("hh_pwa_pair", None)
        if added:
            try:
                sys.path.remove(scripts_dir)
            except ValueError:
                pass


def _run_main_patched(pair_module, monkeypatch, code=FIXED_PAIRING_CODE):
    """`main()` を本番Modalに触れない形で実行し、呼び出し記録を返す。

    - `store` をダミーオブジェクトへ差し替え
    - `security.create_pairing_offer` を記録用フェイクへ差し替え
      （実装は `security.create_pairing_offer` をモジュール属性として
      参照するため、差し替えは `security` モジュール側の属性に対して行う）

    戻り値: `[(store_arg, kwargs), ...]`（発行呼び出しの記録、呼び出し順）。
    """
    dummy_store = object()
    monkeypatch.setattr(pair_module, "store", dummy_store)

    calls: list[tuple[object, dict]] = []

    def fake_create_pairing_offer(store_arg, **kwargs):
        calls.append((store_arg, kwargs))
        return code

    monkeypatch.setattr(
        pair_module.security, "create_pairing_offer", fake_create_pairing_offer
    )

    return calls, dummy_store


def test_main_returns_zero_and_prints_code(pair_module, monkeypatch, capsys):
    """正常系: 0 を返し、コードが標準出力に表示される。

    - `security.create_pairing_offer` の第1引数（store）がダミーオブジェクトと
      同一であること（本番 Modal に触れていないことの確認）
    - `now` が指定されないこと（＝実時刻で発行される）
    - 標準出力にコード文字列が含まれること
    """
    calls, dummy_store = _run_main_patched(pair_module, monkeypatch)

    assert pair_module.main() == 0

    assert len(calls) == 1
    store_arg, kwargs = calls[0]
    assert store_arg is dummy_store
    # `now` は main() が発行直前に確定した時刻（表示期限のズレ防止のため）。
    # ttl_seconds は指定しない＝既定 TTL。
    assert set(kwargs) == {"now"}
    assert isinstance(kwargs["now"], float)

    out = capsys.readouterr().out
    assert "ペアリングコード" in out
    assert FIXED_PAIRING_CODE in out
    assert "有効期限" in out
    assert "まで有効" in out
    assert "(5分間)" in out
    assert "スマホのPWA画面でこのコードを入力してください。" in out


def test_no_log_file_is_written(pair_module, monkeypatch, capsys, tmp_path):
    """ログファイルが一切作られないこと（`hh_issue_agent_token.py` との最大の違い）。

    `main()` 実行後も `tmp_path` 配下に新規ファイルが増えていないことを確認
    する回帰テスト。コードは「画面にのみ表示、ログに残さない」仕様
    （Phase1a spec §7.1 手順1）のため、ファイル書き込み自体が存在しない。
    """
    _run_main_patched(pair_module, monkeypatch)

    assert pair_module.main() == 0
    assert list(tmp_path.iterdir()) == []

    # コードは標準出力にのみ出ている（stderr には何も出ていない）
    captured = capsys.readouterr()
    assert FIXED_PAIRING_CODE in captured.out
    assert captured.err == ""


def test_security_error_returns_nonzero_and_writes_stderr(pair_module, monkeypatch, capsys):
    """`SecurityError`（sha256衝突。事実上起きないが仕様上の例外）を捕捉する。

    - `main()` が非ゼロ（1）を返す
    - エラーメッセージが標準エラー出力に出る（標準出力には出ない）
    """
    def raise_collision(store_arg, **kwargs):
        raise pair_module.security.SecurityError(
            "pairing_offer hash collision on issuance"
        )

    monkeypatch.setattr(
        pair_module.security, "create_pairing_offer", raise_collision
    )

    assert pair_module.main() == 1

    captured = capsys.readouterr()
    assert "失敗" in captured.err
    assert "hash collision" in captured.err
    assert captured.out == ""


def test_non_security_error_propagates(pair_module, monkeypatch):
    """`SecurityError`/`store.StoreError` 以外の例外は握りつぶさずそのまま伝播させる。"""
    def raise_other(store_arg, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        pair_module.security, "create_pairing_offer", raise_other
    )

    with pytest.raises(RuntimeError, match="boom"):
        pair_module.main()


def test_store_error_returns_nonzero_without_leaking_key_or_hash(
    pair_module, monkeypatch, capsys
):
    """`store.StoreError` は固定文言のみをstderrへ出し、例外本文（キー/ハッシュ）を出さない。

    `store.put_if_absent()` は書き込み障害時に
    `f"put failed for key {key!r}: {exc}"`（store.py）という形でキー
    （= `pairing_offer:<sha256(code)>`）を例外メッセージへ埋め込む。8桁数字は
    総当たりでハッシュから元コードを復元できるため、この本文をそのまま
    stderrへ出すと「コードは画面にのみ表示、ログに残さない」仕様
    （Phase1a spec §7.1手順1）を実質的に破る（Codexレビュー指摘の回帰テスト）。
    """
    leaking_message = (
        f"put failed for key 'pairing_offer:{'a' * 64}': "
        f"connection reset by peer"
    )

    def raise_store_error(store_arg, **kwargs):
        raise pair_module.store.StoreError(leaking_message)

    monkeypatch.setattr(
        pair_module.security, "create_pairing_offer", raise_store_error
    )

    assert pair_module.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "失敗" in captured.err
    # 例外本文（キー・ハッシュ・詳細メッセージ）が漏れていないこと
    assert "pairing_offer:" not in captured.err
    assert "a" * 64 not in captured.err
    assert "connection reset" not in captured.err
