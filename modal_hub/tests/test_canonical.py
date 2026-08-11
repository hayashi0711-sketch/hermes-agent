"""`modal_hub/core/canonical.py` — Phase1a spec §3「Canonical JSON とハッシュ」。

フックとサーバで **bit-for-bit 同一** の出力になることが要件。
判定に迷う型・値は best-effort で丸めず必ず例外を送出する。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

import pytest

from modal_hub.core import canonical


# ===========================================================================
# §3 の canonical_json() の形
# ===========================================================================


def test_returns_bytes_not_str() -> None:
    """呼び出し元は `hashlib.sha256(canonical_json(p))` と書く。str だと落ちる。"""
    assert isinstance(canonical.canonical_json({"a": 1}), bytes)


def test_keys_are_sorted_and_separators_are_compact() -> None:
    assert canonical.canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_non_ascii_is_not_escaped() -> None:
    """`ensure_ascii=False`。エスケープ方式の揺れを排除する。"""
    assert canonical.canonical_json({"k": "日本語"}) == '{"k":"日本語"}'.encode("utf-8")


def test_nested_structures_are_normalized_recursively() -> None:
    out = canonical.canonical_json({"z": [{"b": 1, "a": 2}], "a": "x"})
    assert out == b'{"a":"x","z":[{"a":2,"b":1}]}'


def test_list_order_is_preserved() -> None:
    assert canonical.canonical_json([3, 1, 2]) == b"[3,1,2]"


# ===========================================================================
# _normalize() の型テーブル（§3）
# ===========================================================================


def test_strings_are_nfc_normalized() -> None:
    """NFD の「が」と NFC の「が」は同じ canonical 形になる。"""
    nfd = unicodedata.normalize("NFD", "が")
    nfc = unicodedata.normalize("NFC", "が")
    assert nfd != nfc
    assert canonical.canonical_json({"k": nfd}) == canonical.canonical_json({"k": nfc})


def test_dict_keys_are_nfc_normalized_too() -> None:
    nfd = unicodedata.normalize("NFD", "パス")
    nfc = unicodedata.normalize("NFC", "パス")
    assert canonical.canonical_json({nfd: 1}) == canonical.canonical_json({nfc: 1})


def test_bool_and_none_pass_through() -> None:
    assert canonical.canonical_json({"t": True, "f": False, "n": None}) == b'{"f":false,"n":null,"t":true}'


def test_floats_are_rejected() -> None:
    """§3: float は禁止。表現の揺れでハッシュが一致しなくなる。"""
    with pytest.raises(canonical.CanonicalizationError):
        canonical.canonical_json({"x": 1.5})
    with pytest.raises(canonical.CanonicalizationError):
        canonical.canonical_json([0.1])
    with pytest.raises(canonical.CanonicalizationError):
        canonical.canonical_json({"nested": {"deep": [1, 2.0]}})


@pytest.mark.parametrize("key", [True, 1, None, (1, 2)])
def test_non_string_dict_keys_are_rejected(key) -> None:
    """§3: `json.dumps({True:'a', 1:'b'})` はキー 1 個に潰れる。一意性が壊れる。"""
    with pytest.raises(canonical.CanonicalizationError):
        canonical.canonical_json({key: "v"})


def test_nfc_key_collision_is_detected_not_silently_dropped() -> None:
    """異なる 2 キーが NFC 後に同一になったら黙って片方を消さない。"""
    nfd = unicodedata.normalize("NFD", "が")
    nfc = unicodedata.normalize("NFC", "が")
    with pytest.raises(canonical.CanonicalizationError):
        canonical.canonical_json({nfd: 1, nfc: 2})


def test_unsupported_types_are_rejected() -> None:
    for value in (b"bytes", {1, 2}, object(), 1 + 2j):
        with pytest.raises(canonical.CanonicalizationError):
            canonical.canonical_json({"x": value})


def test_huge_integers_are_rejected() -> None:
    """§3: `st_dev` のような巨大整数は文字列で運ぶ契約。JS の 2^53 を超える
    値を JSON 数値にすると PWA 側で丸められ照合が壊れる。"""
    assert canonical.JS_MAX_SAFE_INTEGER == 2**53 - 1
    with pytest.raises(canonical.CanonicalizationError):
        canonical.canonical_json({"st_dev": 17735206716449772873})
    with pytest.raises(canonical.CanonicalizationError):
        canonical.canonical_json({"x": -(2**53)})
    # 範囲内はそのまま通る（§3 の「int はそのまま」を変えていない）。
    assert canonical.canonical_json({"x": 2**53 - 1}) == b'{"x":9007199254740991}'


def test_identity_string_form_passes_canonicalization() -> None:
    """実測の巨大な st_dev も、契約どおり文字列なら問題なく通る。"""
    payload = {"identity": "17735206716449772873:562949955562867"}
    assert b"17735206716449772873:562949955562867" in canonical.canonical_json(payload)


# ===========================================================================
# raw ハッシュ（§3「ハッシュは 2 本取る」）
# ===========================================================================


def test_raw_json_matches_the_spec_expression() -> None:
    payload = {"command": "git push --force", "n": 1}
    expected = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert canonical.raw_json(payload) == expected


def test_raw_json_does_not_normalize() -> None:
    """raw 側は「正規化前の生の姿」。NFC も巨大整数拒否も適用しない。"""
    nfd = unicodedata.normalize("NFD", "が")
    nfc = unicodedata.normalize("NFC", "が")
    assert canonical.raw_json({"k": nfd}) != canonical.raw_json({"k": nfc})
    canonical.raw_json({"st_dev": 17735206716449772873})  # 例外にならない


def test_nfc_is_not_injective_so_two_hashes_are_needed() -> None:
    """§3 の核心: NFC は単射ではない。canonical ハッシュだけで照合すると
    「承認された文字列 A」と「実行される文字列 B」を取り違える。"""
    nfd = unicodedata.normalize("NFD", "テストが")
    nfc = unicodedata.normalize("NFC", "テストが")
    a = canonical.payload_hashes({"command": nfd})
    b = canonical.payload_hashes({"command": nfc})
    assert a.payload_sha256 == b.payload_sha256, "canonical ハッシュは一致するはず"
    assert a.payload_raw_sha256 != b.payload_raw_sha256, (
        "raw ハッシュまで一致してしまうと NFC 経由のすり替えを塞げない"
    )


def test_payload_hashes_returns_both_and_matches_manual_computation() -> None:
    payload = {"command": "rm -rf ./build"}
    hashes = canonical.payload_hashes(payload)
    assert hashes.payload_sha256 == hashlib.sha256(canonical.canonical_json(payload)).hexdigest()
    assert hashes.payload_raw_sha256 == hashlib.sha256(canonical.raw_json(payload)).hexdigest()
    assert len(hashes.payload_sha256) == len(hashes.payload_raw_sha256) == 64


def test_payload_hashes_is_a_named_pair_so_neither_can_be_forgotten() -> None:
    hashes = canonical.payload_hashes({"command": "ls"})
    assert hashes._fields == ("payload_sha256", "payload_raw_sha256")


# ===========================================================================
# サーバ側でパス正規化を行わないこと（§3）
# ===========================================================================


def test_canonical_module_never_resolves_paths() -> None:
    """§3: サーバ（Linux）で `os.path.realpath("C:/Users/...")` を評価すると
    `/root/C:/Users/...` のような別物になる。PathStr 正規化はフック側の責務。

    このモジュールが `os` を import しておらず、realpath を呼ぶコードを
    1 行も持たないことで担保する（docstring 内の言及は除外する）。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(canonical))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "os" not in imported and "pathlib" not in imported, imported
    assert not hasattr(canonical, "os")

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"realpath", "abspath", "normpath", "resolve"}), called


def test_windows_path_string_passes_through_untouched() -> None:
    payload = {"cwd": "C:/Users/Haruki/Projects/Foo"}
    assert b"C:/Users/Haruki/Projects/Foo" in canonical.canonical_json(payload)


# ===========================================================================
# 決定性（同一プロセス内・キー挿入順に依存しない）
# ===========================================================================


def test_output_is_independent_of_dict_insertion_order() -> None:
    a = {"z": 1, "a": 2, "m": 3}
    b = {"m": 3, "a": 2, "z": 1}
    assert canonical.canonical_json(a) == canonical.canonical_json(b)
    assert canonical.payload_hashes(a) == canonical.payload_hashes(b)


def test_repeated_calls_are_stable() -> None:
    payload = {"command": "git push --force", "targets": [{"path": "C:/x", "exists": True}]}
    assert len({canonical.payload_hashes(payload) for _ in range(20)}) == 1
