"""`modal_hub/core/store.py` — write-once プリミティブと Volume I/O。

親設計書 §9 落とし穴 7（`modal.Dict` に compare-and-set は無い）・
8（Volume への複数コンテナ同時追記は行が消える）・
22（`len()` と全走査を使わない）の回帰。
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from modal_hub.core import store
from modal_hub.tests.conftest import FakeModalDict

# `fake_dict` は conftest.py の共有フィクスチャを使う。conftest の
# `FakeModalDict.put()` はいまや実 SDK と同じ戻り値契約（書き込めたら True /
# `skip_if_exists=True` で既存キーなら False）を持つので、このファイル専用の
# 上書きフィクスチャは不要（かつては conftest 側が None を返す旧実装だった
# ため、このファイルだけローカルに `_RealPutSemanticsDict` で契約を修正して
# いたが、その回避策は撤去した）。


# ===========================================================================
# キー prefix / ビルダー
# ===========================================================================


def test_resource_names_match_the_design_doc() -> None:
    """親設計書 §6 の表。名前を変えると既存リソースを踏むか新設してしまう。"""
    assert store.APPROVALS_DICT_NAME == "hh-agent-approvals"
    assert store.STORE_VOLUME_NAME == "hh-agent-store"
    assert store.VOLUME_MOUNT_PATH == "/mnt/hh_store"


def test_prefixes_are_unique() -> None:
    assert len(set(store.ALL_PREFIXES)) == len(store.ALL_PREFIXES)


@pytest.mark.parametrize(
    "builder,prefix",
    [
        (store.req_key, "req:"),
        (store.decision_key, "decision:"),
        (store.lease_key, "lease:"),
        (store.notify_key, "notify:"),
        (store.pairing_offer_key, "pairing_offer:"),
        (store.pairing_used_key, "pairing_used:"),
        (store.ws_ticket_key, "wsticket:"),
        (store.agent_session_key, "agent_session:"),
        (store.pwa_session_key, "pwa_session:"),
        (store.outbox_key, "outbox:"),
    ],
)
def test_key_builders_use_their_prefix(builder, prefix: str) -> None:
    assert builder("abc") == prefix + "abc"


def test_idem_key_is_namespaced_by_subject() -> None:
    """§1.2/§4.3: 他クライアントの idempotency key と衝突させない。"""
    a = store.idem_key("claude_code:alice", "same-key")
    b = store.idem_key("claude_code:bob", "same-key")
    assert a != b
    assert a.startswith("idem:")


def test_idem_key_does_not_normalize_the_subject() -> None:
    """`sub` は verbatim。正規化すると別 subject が同一名前空間に落ちうる。"""
    assert store.idem_key("Alice", "k") != store.idem_key("alice", "k")


# ===========================================================================
# write-once プリミティブ
# ===========================================================================


def test_put_if_absent_writes_once(fake_dict) -> None:
    assert store.put_if_absent("decision:1", {"decision": "approved"}) is True
    assert store.put_if_absent("decision:1", {"decision": "rejected"}) is False
    assert store.get("decision:1") == {"decision": "approved"}


def test_put_if_absent_always_passes_skip_if_exists(fake_dict) -> None:
    """CAS が無い以上、原子性が保証されるのは `skip_if_exists=True` のみ。"""
    store.put_if_absent("k", 1)
    store.put_if_absent("k", 2)
    assert all(skip is True for _key, skip in fake_dict.put_calls), fake_dict.put_calls


def test_get_returns_none_for_missing_key(fake_dict) -> None:
    assert store.get("nope") is None


def test_contains_is_a_predicate(fake_dict) -> None:
    assert store.contains("k") is False
    store.put_if_absent("k", {"v": 1})
    assert store.contains("k") is True


def test_get_many_omits_missing_keys(fake_dict) -> None:
    store.put_if_absent("a", 1)
    assert store.get_many(["a", "b"]) == {"a": 1}


def test_delete_is_idempotent(fake_dict) -> None:
    store.delete("missing")  # 例外にならない
    store.put_if_absent("agent_session:t", {"x": 1})
    store.delete("agent_session:t")
    assert store.contains("agent_session:t") is False


def test_module_exposes_no_overwrite_or_update_helper() -> None:
    """spec §1.2: 「`store.py` に `overwrite()` を生やしてはならない」。

    承認状態機械そのものが「書き込み 1 回勝負」であることに依存しており、
    同じストアに read-then-write の経路を作ることは、この設計が回避する
    ために作られたバグ類型そのものを持ち込む。
    """
    banned = {"overwrite", "update", "set", "merge", "put", "increment", "incr", "append"}
    exported = set(store.__all__)
    assert banned & exported == set(), f"禁止された書き込み API が公開されている: {banned & exported}"
    assert banned & {n for n in dir(store) if not n.startswith("_")} == set()


def test_module_exposes_no_dict_iteration_helper() -> None:
    """§9 落とし穴 22: `len()` と全走査を使わない（高コスト・上限 10 万件）。"""
    exported = {n for n in dir(store) if not n.startswith("_")}
    assert not (exported & {"keys", "items", "values", "count", "length", "scan", "iterate"})


def test_store_never_calls_len_or_iterates_the_dict(fake_dict) -> None:
    """FakeModalDict は `__len__`/`__iter__` を実装していない。

    実装がうっかり全走査したら TypeError で落ちる。
    """
    with pytest.raises(TypeError):
        len(fake_dict)
    store.put_if_absent("a", 1)
    store.get_many(["a"])
    store.delete("a")  # ここまでで一度も走査していない


# ===========================================================================
# outbox（§10.1b）
# ===========================================================================


def test_outbox_register_is_idempotent_on_the_same_event_id(fake_dict) -> None:
    """`event_id` が決定的なので、再書き出しが重複を生まない。"""
    assert store.outbox_register("evt1", {"event": "claim_granted"}) is True
    assert store.outbox_register("evt1", {"event": "claim_granted"}) is False


def test_outbox_consume_removes_the_entry(fake_dict) -> None:
    store.outbox_register("evt1", {"x": 1})
    store.outbox_consume("evt1")
    assert store.contains(store.outbox_key("evt1")) is False


def test_outbox_shares_the_atomic_store_with_state_keys() -> None:
    """§10.1b: 状態更新と同じストアなので、こちらは確実に残る。"""
    assert store.OUTBOX_DICT_NAME == store.APPROVALS_DICT_NAME


# ===========================================================================
# Volume I/O（§9 落とし穴 8 / spec §10.1）
# ===========================================================================


@pytest.fixture()
def volume_root(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_volume_root", lambda: tmp_path)
    return tmp_path


def test_atomic_write_creates_parents_and_commits(volume_root, fake_dict) -> None:
    store.atomic_write_file("audit/2026-08/a.requested.deadbeef.json", b'{"x":1}')
    written = volume_root / "audit" / "2026-08" / "a.requested.deadbeef.json"
    assert written.read_bytes() == b'{"x":1}'
    assert fake_dict.volume.commits == 1, "volume.commit() が呼ばれていない"


def test_atomic_write_leaves_no_temp_files(volume_root, fake_dict) -> None:
    store.atomic_write_file("audit/2026-08/a.json", b"{}")
    leftovers = [p.name for p in (volume_root / "audit" / "2026-08").iterdir() if p.name.startswith(".tmp.")]
    assert leftovers == []


def test_atomic_write_uses_a_temp_file_in_the_same_directory(volume_root, fake_dict, monkeypatch) -> None:
    """`os.replace` が原子的なのは同一ファイルシステム上だけ。"""
    import tempfile as tf

    seen: list[str] = []
    real_mkstemp = tf.mkstemp

    def spy(*args, **kwargs):
        seen.append(kwargs.get("dir"))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(store.tempfile, "mkstemp", spy)
    store.atomic_write_file("audit/2026-08/a.json", b"{}")
    assert seen == [str(volume_root / "audit" / "2026-08")]


def test_atomic_write_overwrite_is_idempotent(volume_root, fake_dict) -> None:
    """spec §10.1: 決定的な名前なら同じファイルへの上書きになり冪等になる。"""
    for _ in range(3):
        store.write_json("audit/2026-08/a.requested.abc.json", {"event": "requested"})
    assert len(list((volume_root / "audit" / "2026-08").iterdir())) == 1


def test_one_event_one_file_no_shared_append_log(volume_root, fake_dict) -> None:
    """§9 落とし穴 8 / D-11: 共有 JSONL への複数コンテナ追記は行が消える。

    `store.py` に「追記」API が存在しないことを確認する。
    """
    assert not any("append" in n or "jsonl" in n.lower() for n in store.__all__)


def test_atomic_write_rejects_str_content(volume_root, fake_dict) -> None:
    with pytest.raises(store.StoreError):
        store.atomic_write_file("a.json", "not bytes")  # type: ignore[arg-type]


def test_read_json_round_trip(volume_root, fake_dict) -> None:
    store.write_json("audit/2026-08/a.json", {"b": 2, "a": 1})
    assert store.read_json("audit/2026-08/a.json") == {"a": 1, "b": 2}


def test_write_json_is_byte_stable(volume_root, fake_dict) -> None:
    store.write_json("a.json", {"b": 2, "a": 1})
    first = store.read_file("a.json")
    store.write_json("a.json", {"a": 1, "b": 2})
    assert store.read_file("a.json") == first == b'{"a":1,"b":2}'


def test_read_missing_file_returns_none(volume_root, fake_dict) -> None:
    assert store.read_file("nope.json") is None
    assert store.read_json("nope.json") is None
    assert store.file_exists("nope.json") is False
    assert store.list_dir("nope") == []


def test_malformed_audit_json_raises_not_returns_none(volume_root, fake_dict) -> None:
    """§9 落とし穴 15: 黙って空を返す実装は原因を隠す。"""
    (volume_root / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        store.read_json("bad.json")


# ===========================================================================
# BUG: put_if_absent の競合時の戻り値
# ===========================================================================


def test_put_if_absent_returns_false_when_another_writer_won(monkeypatch) -> None:
    """他コンテナが先に同じキーへ書き込んだ場合、負けた側は False を受け取る。

    修正後の `put_if_absent` は `put(skip_if_exists=True)` の戻り値
    （サーバー側で原子的に決まる「書けたか」の真偽値）をそのまま返す。
    `contains` 前後読みを挟まないので、TOCTOU の隙間自体が存在しない。
    """

    class RacingDict(FakeModalDict):
        def put(self, key, value, skip_if_exists=False):
            # 我々の put が届く直前に、別コンテナが同じキーを書いた体で
            # 競合を再現する。skip_if_exists=True のときの実際の判定
            # （キーが既に存在するか）はこの後 super().put() が下す。
            if key == "lease:1" and key not in self._data:
                self._data[key] = {"lease_id": "OTHER", "claim_attempt_id": "other-attempt"}
            return super().put(key, value, skip_if_exists=skip_if_exists)

    d = RacingDict()
    monkeypatch.setattr(store, "_approvals_dict", lambda: d)

    won = store.put_if_absent("lease:1", {"lease_id": "MINE", "claim_attempt_id": "my-attempt"})
    assert store.get("lease:1")["lease_id"] == "OTHER"
    assert won is False, "自分の書き込みが負けたのに True を返した（二重実行の経路）"


# ===========================================================================
# S-08c: quarantine 消し込みマーカー（mark/get_quarantine_resolved）
# ===========================================================================


def test_quarantine_resolved_marker_round_trip(fake_dict) -> None:
    """書いた値がそのまま読めること・未設定は None であること。"""
    assert store.get_quarantine_resolved("alpha-skill") is None
    store.mark_quarantine_resolved("alpha-skill", "a" * 64)
    assert store.get_quarantine_resolved("alpha-skill") == "a" * 64


def test_quarantine_resolved_marker_overwrites_unconditionally(fake_dict) -> None:
    """書き手は sync_dashboard_skills 唯一なので put_if_absent は使わない
    無条件上書き（dict[key] = value 相当）でよい。

    `FakeModalDict.put_calls` の skip フラグで「skip_if_exists=True の
    排他ではない」ことを確認する。
    """
    store.mark_quarantine_resolved("alpha-skill", "a" * 64)
    store.mark_quarantine_resolved("alpha-skill", "b" * 64)
    assert store.get_quarantine_resolved("alpha-skill") == "b" * 64
    assert fake_dict.put_calls and all(skip is False for _key, skip in fake_dict.put_calls)


# ===========================================================================
# S-08b/S-08c: quarantine の安全な読み取り（read_quarantine_entry_safe）
# ===========================================================================


def _write_quarantine_entry(root, name: str, *, skill_md: str | None = None, meta: dict | None = None) -> None:
    """テスト用に volume root 直下へ quarantine エントリ（SKILL.md + meta.json）を書く。"""
    d = root / "skills_quarantine" / name
    d.mkdir(parents=True, exist_ok=True)
    md = skill_md if skill_md is not None else f"---\nname: {name}\ndescription: d\n---\n\nBody.\n"
    (d / "SKILL.md").write_bytes(md.encode("utf-8"))
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_read_quarantine_entry_safe_returns_full_entry(volume_root) -> None:
    md = (
        "---\nname: alpha-skill\ndescription: d\nversion: 0.1.0\n"
        "distilled_from_session_id: sess-abc\n---\n\nBody.\n"
    )
    _write_quarantine_entry(
        volume_root, "alpha-skill", skill_md=md,
        meta={"origin_instance": "win-1", "published_at": 123.0},
    )
    entry = store.read_quarantine_entry_safe("alpha-skill")
    assert entry is not None
    assert entry["content"] == md
    assert entry["content_sha256"] == hashlib.sha256(md.encode("utf-8")).hexdigest()
    assert entry["origin_instance"] == "win-1"
    assert entry["published_at"] == 123.0
    assert entry["distilled_from_session_id"] == "sess-abc"


def test_read_quarantine_entry_safe_rejects_invalid_names(volume_root) -> None:
    """NAME_RE を再利用し、不正な name は None（パストラバーサル等も含む）。"""
    _write_quarantine_entry(volume_root, "alpha-skill")
    for bad in ("Bad Name", "../etc/passwd", ".hidden", "a", "", "alpha-skill/"):
        assert store.read_quarantine_entry_safe(bad) is None


def test_read_quarantine_entry_safe_missing_entries_return_none(volume_root) -> None:
    """root 自体・<name> ディレクトリ・SKILL.md が無い場合は None。"""
    assert store.read_quarantine_entry_safe("no-root") is None  # skills_quarantine 自体が無い
    _write_quarantine_entry(volume_root, "no-md")
    (volume_root / "skills_quarantine" / "no-md" / "SKILL.md").unlink()
    assert store.read_quarantine_entry_safe("no-md") is None


def test_read_quarantine_entry_safe_meta_missing_or_corrupt_falls_back(volume_root) -> None:
    """meta.json の欠損・破損・非 object・フィールド型不正は例外にせず
    origin_instance: None / published_at: None へフォールバックする。"""
    # 欠損
    _write_quarantine_entry(volume_root, "no-meta")
    entry = store.read_quarantine_entry_safe("no-meta")
    assert entry is not None
    assert entry["origin_instance"] is None and entry["published_at"] is None

    # 破損 JSON
    _write_quarantine_entry(volume_root, "bad-meta")
    (volume_root / "skills_quarantine" / "bad-meta" / "meta.json").write_text("{not json", encoding="utf-8")
    entry = store.read_quarantine_entry_safe("bad-meta")
    assert entry is not None
    assert entry["origin_instance"] is None and entry["published_at"] is None

    # valid JSON だが object でない
    _write_quarantine_entry(volume_root, "list-meta")
    (volume_root / "skills_quarantine" / "list-meta" / "meta.json").write_text("[1,2,3]", encoding="utf-8")
    entry = store.read_quarantine_entry_safe("list-meta")
    assert entry is not None
    assert entry["origin_instance"] is None and entry["published_at"] is None

    # フィールド型不正（origin_instance が数値・published_at が文字列）
    _write_quarantine_entry(volume_root, "type-meta")
    (volume_root / "skills_quarantine" / "type-meta" / "meta.json").write_text(
        json.dumps({"origin_instance": 123, "published_at": "x"}), encoding="utf-8"
    )
    entry = store.read_quarantine_entry_safe("type-meta")
    assert entry is not None
    assert entry["origin_instance"] is None and entry["published_at"] is None


def test_read_quarantine_entry_safe_rejects_oversized_skill_md(volume_root) -> None:
    """64KB を超える本文は一括 read ではなく境界付き read（fstat サイズ検査 +
    上限ループ）で弾かれる。"""
    big = f"---\nname: big-skill\ndescription: d\n---\n\n{'x' * (store.MAX_BODY_BYTES + 1)}\n"
    _write_quarantine_entry(volume_root, "big-skill", skill_md=big)
    assert store.read_quarantine_entry_safe("big-skill") is None


def test_read_quarantine_entry_safe_distilled_field_handles_broken_frontmatter(volume_root) -> None:
    """frontmatter が壊れている・無い場合、distilled_from_session_id は None
    （本文は読めるためエントリ自体は返る）。"""
    _write_quarantine_entry(volume_root, "broken-fm", skill_md="---\nname: broken-fm\nbroken: [\n---\n\nBody.\n")
    entry = store.read_quarantine_entry_safe("broken-fm")
    assert entry is not None
    assert entry["distilled_from_session_id"] is None

    _write_quarantine_entry(volume_root, "no-fm", skill_md="No frontmatter here.\n")
    entry = store.read_quarantine_entry_safe("no-fm")
    assert entry is not None
    assert entry["distilled_from_session_id"] is None


def test_read_quarantine_entry_safe_rejects_non_utf8_skill_md(volume_root) -> None:
    d = volume_root / "skills_quarantine" / "bin-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(b"\xff\xfe\x00binary")
    assert store.read_quarantine_entry_safe("bin-skill") is None


# ---------------------------------------------------------------------------
# symlink 差し替え攻撃への防御
# ---------------------------------------------------------------------------


def _try_make_symlink(link_path, target, *, directory: bool) -> bool:
    """実 symlink を作れる環境なら作って True、作れない（Windows 非管理者等）
    環境では False（呼び出し側が代替検証テストに委ねる）。"""
    try:
        os.symlink(target, link_path, target_is_directory=directory)
        return True
    except OSError:
        return False


def test_read_quarantine_entry_safe_rejects_symlinked_entry_dir(volume_root, tmp_path) -> None:
    """<name> ディレクトリ自体が symlink に差し替えられたら読み取りを拒否
    （openat 経路は O_NOFOLLOW で ELOOP、代替経路は islink で拒否）。"""
    outside = tmp_path / "attacker-dir"
    outside.mkdir()
    (outside / "SKILL.md").write_text("pwned", encoding="utf-8")
    (volume_root / "skills_quarantine").mkdir(parents=True, exist_ok=True)
    entry_dir = volume_root / "skills_quarantine" / "real-skill"
    if not _try_make_symlink(entry_dir, outside, directory=True):
        pytest.skip("symlink creation is not permitted on this platform")
    assert store.read_quarantine_entry_safe("real-skill") is None


def test_read_quarantine_entry_safe_rejects_symlinked_skill_md(volume_root, tmp_path) -> None:
    """SKILL.md 自体が symlink に差し替えられたら読み取りを拒否。"""
    _write_quarantine_entry(volume_root, "md-link-skill")
    outside = tmp_path / "outside.md"
    outside.write_text("pwned", encoding="utf-8")
    skill_file = volume_root / "skills_quarantine" / "md-link-skill" / "SKILL.md"
    skill_file.unlink()
    if not _try_make_symlink(skill_file, outside, directory=False):
        pytest.skip("symlink creation is not permitted on this platform")
    assert store.read_quarantine_entry_safe("md-link-skill") is None


def test_read_quarantine_entry_safe_meta_symlink_falls_back(volume_root, tmp_path) -> None:
    """meta.json だけ symlink に差し替えられても本文は返し、メタは None へ
    フォールバックする（本文の安全性には影響しないため 500 にしない）。"""
    _write_quarantine_entry(
        volume_root, "meta-link-skill", meta={"origin_instance": "win-1", "published_at": 1.0}
    )
    outside = tmp_path / "outside.json"
    outside.write_text('{"origin_instance": "pwned", "published_at": 9.0}', encoding="utf-8")
    meta_file = volume_root / "skills_quarantine" / "meta-link-skill" / "meta.json"
    meta_file.unlink()
    if not _try_make_symlink(meta_file, outside, directory=False):
        pytest.skip("symlink creation is not permitted on this platform")
    entry = store.read_quarantine_entry_safe("meta-link-skill")
    assert entry is not None
    assert entry["origin_instance"] is None and entry["published_at"] is None


@pytest.mark.parametrize(
    "suffix,entry_name,expect",
    [
        # <name> ディレクトリ自体が symlink → エントリ全体を拒否
        ("skills_quarantine/evil-dir", "evil-dir", None),
        # SKILL.md 自体が symlink → エントリ全体を拒否
        ("SKILL.md", "evil-md", None),
        # meta.json 自体が symlink → 例外にせず null フィールドへフォールバック
        ("meta.json", "evil-meta", "fallback"),
    ],
)
def test_read_quarantine_fallback_refuses_islink_marked_paths(
    volume_root, monkeypatch, suffix: str, entry_name: str, expect
) -> None:
    """実 symlink を作成できない環境（Windows 非管理者等）での代替検証。

    `os.path.islink()` が True を返す状態＝symlink 差し替えを模擬し、
    O_NOFOLLOW / dir_fd を持たない代替実装が「symlink なら拒否する」こと
    （meta.json は本文と非対称に null へフォールバック）を確認する。
    """
    _write_quarantine_entry(volume_root, entry_name, meta={"origin_instance": "win-1", "published_at": 1.0})
    monkeypatch.setattr(store, "_HAVE_DIRFD_NOFOLLOW", False)  # 代替実装経路を強制
    real_islink = os.path.islink
    monkeypatch.setattr(
        os.path,
        "islink",
        # Windows は `\` 区切りのため、照合前に両側を `/` 区切りへ正規化する
        lambda p: os.fspath(p).replace(os.sep, "/").endswith(suffix) or real_islink(p),
    )

    entry = store.read_quarantine_entry_safe(entry_name)
    if expect == "fallback":
        assert entry is not None
        assert entry["origin_instance"] is None and entry["published_at"] is None
    else:
        assert entry is None
