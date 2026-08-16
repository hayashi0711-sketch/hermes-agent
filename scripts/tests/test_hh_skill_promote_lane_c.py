"""scripts/tests/test_hh_skill_promote_lane_c.py — Lane C 第3弾（S-06b/S-08/S-08b）のテスト。

`.lane_c_wave3_promote_task.md` のテスト要件に対応する。既存
`modal_hub/tests/test_skill_promote.py` の隔離パターン（hh_agent_home の
monkeypatch・quarantine fixture・TTY）を踏襲する。

テスト実行時に実ホームへ触らせないため、`run_promote()` / `run_remote_promote()`
内部の `promote_lock` は fixture `_lock_at_tmp` で `base=tmp_path` に差し替える
（`promote_seq.json` 等は `hh_agent_home` の monkeypatch 経由で tmp_path に入る）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import hh_agent_promote_lock as lock_mod  # noqa: E402
import hh_skill_promote as promote  # noqa: E402
from modal_hub.services import skill_sync  # noqa: E402

DUMMY_SIGNING_KEY = "k" * 64  # HMAC の鍵。テスト専用ダミー


def _skill_md(name: str, description: str = "d", *, session_id: str = None) -> str:
    extra = f"distilled_from_session_id: {session_id}\n" if session_id else ""
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\nBody.\n"


@pytest.fixture(autouse=True)
def _isolate_hermes_scan_dirs(monkeypatch):
    """`assert_staging_root_is_safe` が実ホームディレクトリへ触らないよう固定する。"""
    monkeypatch.setattr(promote, "_existing_hermes_scan_dirs", lambda: [])
    monkeypatch.setattr(promote, "_declared_hermes_scan_dirs_including_nonexistent", lambda: [])


@pytest.fixture()
def quarantine(tmp_path, monkeypatch):
    from modal_hub.services import skill_quarantine as sq

    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    monkeypatch.setattr(sq, "hh_agent_home", lambda: tmp_path)
    return sq


@pytest.fixture()
def signing_env(monkeypatch):
    """`.hh-signing.env` 相当: 署名鍵をダミーで返す（write_receipt が通るように）。"""
    monkeypatch.setattr(
        promote, "_load_signing_env",
        lambda: {promote.SIGNING_KEY_VAR: DUMMY_SIGNING_KEY,
                 promote.WRITE_KEY_VAR: "w" * 64},
    )


@pytest.fixture()
def tty_yes(monkeypatch):
    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda prompt: "y")


@pytest.fixture()
def lock_at_tmp(monkeypatch, tmp_path):
    """run_promote 内部の promote_lock を base=tmp_path で実行させる。

    本物のロック機構（nonce/PID/heartbeat/奪取）はそのまま使い、ロックの
    置き場所だけ tmp_path に移す。呼び出し順の記録は `calls` リストへ
    "lock:enter"/"lock:exit" を追記する。
    """
    calls: list[str] = []

    @contextlib.contextmanager
    def _wrapped(*, base=None, timeout=60.0, nonblocking=False):
        calls.append("lock:enter")
        try:
            with lock_mod.promote_lock(base=tmp_path, timeout=timeout, nonblocking=nonblocking) as acquired:
                yield acquired
        finally:
            calls.append("lock:exit")

    monkeypatch.setattr(promote, "promote_lock", _wrapped)
    return calls


def _spy(monkeypatch, name: str, calls: list[str]):
    """promote モジュールの関数を enter マーカーを記録しながら実行する spy に差し替える。"""
    original = getattr(promote, name)

    def _wrapped(*args, **kwargs):
        calls.append(name)
        return original(*args, **kwargs)

    monkeypatch.setattr(promote, name, _wrapped)
    return _wrapped


# ---------------------------------------------------------------------------
# 2a: install_confirmed_skill（リファクタの回帰）
# ---------------------------------------------------------------------------


def test_install_confirmed_skill_installs_like_staging_flow(tmp_path, monkeypatch):
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)
    content = _skill_md("ic-skill").encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()

    result = promote.install_confirmed_skill(
        "ic-skill", content, digest, force=False, provenance="local-promote"
    )

    assert result["backup_path"] is None
    assert result["destination"] == hermes_root / "ic-skill"
    assert (hermes_root / "ic-skill" / "SKILL.md").read_bytes() == content
    # staging は消費されている（既存フローと同じ）
    assert not (promote._promote_staging_root(tmp_path) / "ic-skill").exists()


def test_install_confirmed_skill_force_backs_up(tmp_path, monkeypatch):
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)
    (hermes_root / "force-skill").mkdir(parents=True)
    (hermes_root / "force-skill" / "SKILL.md").write_bytes(b"old-content")
    content = _skill_md("force-skill").encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()

    result = promote.install_confirmed_skill(
        "force-skill", content, digest, force=True, provenance="local-promote"
    )

    assert result["backup_path"] is not None
    assert Path(result["backup_path"]).name.startswith("force-skill.bak.")
    assert (hermes_root / "force-skill" / "SKILL.md").read_bytes() == content


# ---------------------------------------------------------------------------
# 2b: append_promote_log の新フィールド（provenance / promoted_at_ms）
# ---------------------------------------------------------------------------


def test_append_promote_log_new_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    content = _skill_md("log-skill", session_id="sess-9").encode("utf-8")

    record = promote.append_promote_log(
        name="log-skill",
        digest="d" * 64,
        content_bytes=content,
        destination=tmp_path / "dest",
        forced=False,
        backup_path=None,
        provenance="local-promote",
        promoted_at_ms=1700000000000,
        base=tmp_path,
    )

    assert record["provenance"] == "local-promote"
    assert record["promoted_at_ms"] == 1700000000000
    assert record["promoted_at"] == pytest.approx(1700000000.0)  # 後方互換フィールド
    assert record["name"] == "log-skill"
    written = json.loads((tmp_path / "promote_log.jsonl").read_text(encoding="utf-8").strip())
    assert written == record  # 戻り値はファイルに書いた内容そのもの


# ---------------------------------------------------------------------------
# 2f: run_promote の呼び出し順序（S-10 疑似コード）を spy で固定
# ---------------------------------------------------------------------------


def test_run_promote_call_order(tmp_path, monkeypatch, quarantine, signing_env, tty_yes, lock_at_tmp):
    calls = lock_at_tmp
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)
    content = _skill_md("order-skill", session_id="sess-order")
    quarantine.materialize("qid-order", "order-skill", content, base=tmp_path)

    for fn in (
        "assert_staging_root_is_safe",
        "self_heal_orphaned_promotions",
        "display_for_confirmation",
        "confirm_or_abort",
        "recheck_quarantined_digest",
        "install_confirmed_skill",
        "allocate_promotion_seq",
        "write_receipt",
        "append_promote_log",
        "push_to_lane_c",
    ):
        _spy(monkeypatch, fn, calls)

    promote.run_promote("order-skill", force=False)

    expected = [
        "assert_staging_root_is_safe",
        "self_heal_orphaned_promotions",
        "display_for_confirmation",
        "confirm_or_abort",
        "lock:enter",
        "assert_staging_root_is_safe",  # ロック内で再検査
        "self_heal_orphaned_promotions",  # べき等な再実行
        "recheck_quarantined_digest",  # ダイジェスト再検査
        "install_confirmed_skill",
        "allocate_promotion_seq",  # install → seq → receipt → log
        "write_receipt",
        "append_promote_log",
        "lock:exit",
        "push_to_lane_c",  # ロック外（フェイルオープン）
    ]
    assert calls == expected


# ---------------------------------------------------------------------------
# S-08 フェイルオープン: push 失敗でも promote は成功・終了コード 0
# ---------------------------------------------------------------------------


def _setup_e2e(tmp_path, monkeypatch, quarantine, name, *, session_id=None):
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)
    content = _skill_md(name, session_id=session_id)
    quarantine.materialize(f"qid-{name}", name, content, base=tmp_path)
    return hermes_root, content


def test_push_exception_still_succeeds(tmp_path, monkeypatch, quarantine, signing_env, tty_yes, lock_at_tmp):
    hermes_root, content = _setup_e2e(tmp_path, monkeypatch, quarantine, "push-fail-skill")
    # Lane C 設定が存在する状態で push まで進め、push_skill が例外を投げる。
    (tmp_path / promote.LANE_C_CONFIG_FILENAME).write_text(
        json.dumps({"base_url": "https://lane.invalid", "read_key": "rk"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        skill_sync, "push_skill",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("connection refused")),
    )

    promote.run_promote("push-fail-skill", force=False)  # 例外は伝播しない

    assert (hermes_root / "push-fail-skill" / "SKILL.md").read_text(encoding="utf-8") == content
    # 別スキルで main 経由でも終了コード 0（再 promote は force なしだと失敗するため名前を変える）
    _, content2 = _setup_e2e(tmp_path, monkeypatch, quarantine, "push-fail-2")
    assert promote.main(["push-fail-2"]) == 0
    assert (hermes_root / "push-fail-2" / "SKILL.md").read_text(encoding="utf-8") == content2


def test_promote_body_failure_does_not_push(tmp_path, monkeypatch, quarantine, tty_yes, lock_at_tmp):
    _setup_e2e(tmp_path, monkeypatch, quarantine, "body-fail-skill")
    calls: list[str] = []
    _spy(monkeypatch, "push_to_lane_c", calls)

    def _boom(*args, **kwargs):
        raise promote.PromoteError("install refused")

    monkeypatch.setattr(promote, "install_confirmed_skill", _boom)

    with pytest.raises(promote.PromoteError):
        promote.run_promote("body-fail-skill", force=False)
    assert calls == []  # promote 本体が失敗したとき push は呼ばれない


# ---------------------------------------------------------------------------
# argparse: --yes / --non-interactive / --no-confirm を絶対に受け付けない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--yes", "--non-interactive", "--no-confirm"])
def test_argparse_rejects_confirmation_bypass_flags(flag):
    with pytest.raises(SystemExit) as exc:
        promote.main([flag, "some-skill"])
    assert exc.value.code == 2
    # remote モードでも同様に拒否
    with pytest.raises(SystemExit):
        promote.main(["--remote", "modal-dashboard", flag, "some-skill"])


def test_argparse_rejects_force_with_repair_seq():
    with pytest.raises(SystemExit):
        promote.main(["--repair-seq", "--force"])


def test_argparse_rejects_origin_without_repair_seq():
    with pytest.raises(SystemExit):
        promote.main(["--origin", "win-1", "some-skill"])


# ---------------------------------------------------------------------------
# 2c: allocate_promotion_seq / resolve_seq_from_watermark
# ---------------------------------------------------------------------------


def test_allocate_promotion_seq_independent_per_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)

    assert promote.allocate_promotion_seq("shared", origin_instance="win-1") == 0
    assert promote.allocate_promotion_seq("shared", origin_instance="modal-1") == 0
    assert promote.allocate_promotion_seq("shared", origin_instance="win-1") == 1
    assert promote.allocate_promotion_seq("shared", origin_instance="modal-1") == 1
    assert promote.allocate_promotion_seq("shared", origin_instance="win-1") == 2

    data = json.loads((tmp_path / "promote_seq.json").read_text(encoding="utf-8"))
    assert data["shared"] == {"win-1": 3, "modal-1": 2}


def test_old_schema_migration_happens_inside_allocate_only(tmp_path, monkeypatch):
    """旧スキーマ（{name: int}）の移行がロック外で先読みされないこと。

    - allocate を呼ぶまではファイルに一切触らない（書き込みが発生しない）。
    - 移行 + 採番は 1 回の原子的書き込みで完了し、旧値がそのまま引き継がれる。
    """
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    seq_path = tmp_path / "promote_seq.json"
    seq_path.write_text(json.dumps({"alpha": 5}), encoding="utf-8")

    writes: list[Path] = []
    original_write = promote._atomic_write_json
    monkeypatch.setattr(
        promote, "_atomic_write_json",
        lambda path, data: (writes.append(path), original_write(path, data))[1],
    )

    # 呼ぶ前は何も書き換わっていない（ロック外での先読み・移行なし）
    assert json.loads(seq_path.read_text(encoding="utf-8")) == {"alpha": 5}
    assert writes == []

    first = promote.allocate_promotion_seq("alpha", origin_instance="win-1")
    assert first == 5  # 旧スキーマの値がそのまま「次の seq」として引き継がれる
    assert json.loads(seq_path.read_text(encoding="utf-8")) == {"alpha": {"win-1": 6}}
    assert len(writes) == 1  # 移行と採番で 1 回だけ

    second = promote.allocate_promotion_seq("alpha", origin_instance="win-1")
    assert second == 6


def test_resolve_seq_from_watermark(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)

    assert promote.resolve_seq_from_watermark("known", "win-1", 7) == 8  # watermark+1
    assert promote.resolve_seq_from_watermark("unknown", "win-1", None) == 0  # 未 push は 0 から
    data = json.loads((tmp_path / "promote_seq.json").read_text(encoding="utf-8"))
    assert data == {"known": {"win-1": 8}, "unknown": {"win-1": 0}}
    assert promote.allocate_promotion_seq("known", origin_instance="win-1") == 8


# ---------------------------------------------------------------------------
# 2j: --resign の安全契約（S-06b）
# ---------------------------------------------------------------------------


def _make_receipt_record(tmp_path, name, old_key: bytes, *, filename: str = None):
    """本物の skill_sync.write_receipt() で署名した record を保存する。"""
    content = _skill_md(name).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    origin = "win-1"
    promoted_at_ms = 1700000000000
    seq = 3
    key_id = skill_sync.derive_key_id(old_key)
    receipt = skill_sync.write_receipt(
        name=name,
        content_bytes_or_sha256=content,
        digest=digest,
        seq=seq,
        promoted_at_ms=promoted_at_ms,
        origin_instance=origin,
        distilled_from_session_id=None,
        signing_key=old_key,
        key_id=key_id,
    )
    record = {
        "name": name,
        "content_sha256": digest,
        "origin_instance": origin,
        "promoted_at_ms": promoted_at_ms,
        "promotion_seq": seq,
        "distilled_from_session_id": None,
        "key_id": key_id,
        "receipt": receipt,
    }
    if filename is None:
        filename = f"{digest[:8]}-{hashlib.sha256(receipt.encode('utf-8')).hexdigest()[:8]}.json"
    name_dir = tmp_path / "promote_receipts" / name
    name_dir.mkdir(parents=True, exist_ok=True)
    (name_dir / filename).write_text(json.dumps(record), encoding="utf-8")
    return name_dir, record


def _set_resign_env(monkeypatch, old_key: str, new_key: str):
    monkeypatch.setattr(promote, "_load_signing_env", lambda: {promote.SIGNING_KEY_VAR: old_key})
    monkeypatch.setenv("HH_AGENT_TOKEN_SIGNING_KEY_NEW", new_key)


def test_resign_resigns_only_with_old_key_verification(tmp_path, monkeypatch):
    old_key = "old-key-" + "x" * 20
    new_key = "new-key-" + "y" * 20
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    name_dir, record = _make_receipt_record(tmp_path, "resign-ok", old_key.encode("utf-8"))

    # current を正しく指させる（ファイル名を確定させる）
    current_name = next(p.name for p in name_dir.glob("*.json"))
    (name_dir / "current").write_text(current_name, encoding="utf-8")

    _set_resign_env(monkeypatch, old_key, new_key)
    resigned = promote.resign_receipts(base=tmp_path)
    assert resigned == 1

    # 新ファイルが作られ、旧ファイルは残っている。current は新ファイルを指す。
    files = sorted(p.name for p in name_dir.glob("*.json"))
    assert len(files) == 2
    assert current_name in files  # 旧ファイルは残る
    new_name = (name_dir / "current").read_text(encoding="utf-8").strip()
    assert new_name != current_name
    new_record = json.loads((name_dir / new_name).read_text(encoding="utf-8"))
    assert new_record["key_id"] == skill_sync.derive_key_id(new_key.encode("utf-8"))
    assert new_record["receipt"] != record["receipt"]
    # 署名対象のタプルは不変（promotion_seq 等が変わっていない）
    assert new_record["promotion_seq"] == record["promotion_seq"]
    assert new_record["content_sha256"] == record["content_sha256"]
    # 新鍵で検証できる
    assert skill_sync.verify_receipt(
        new_record["receipt"], "resign-ok", new_record["content_sha256"],
        "win-1", 1700000000000, 3, None,
        verify_keys={skill_sync.derive_key_id(new_key.encode("utf-8")): new_key.encode("utf-8")},
    )


def _file_of(name_dir: Path, record: dict) -> Path:
    digest = record["content_sha256"]
    return name_dir / f"{digest[:8]}-{hashlib.sha256(record['receipt'].encode('utf-8')).hexdigest()[:8]}.json"


def test_resign_refuses_when_no_old_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    (tmp_path / "promote_receipts" / "empty-skill").mkdir(parents=True)
    _set_resign_env(monkeypatch, "old" * 32, "new" * 32)

    with pytest.raises(promote.PromoteError) as exc:
        promote.resign_receipts(base=tmp_path)
    assert "empty-skill" in str(exc.value)
    assert "旧 receipt が存在しない" in str(exc.value)


def test_resign_refuses_when_verification_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    _make_receipt_record(tmp_path, "tampered", b"k" * 40)
    _set_resign_env(monkeypatch, "old" * 32, "new" * 32)  # 旧鍵が record の鍵と違う

    with pytest.raises(promote.PromoteError) as exc:
        promote.resign_receipts(base=tmp_path)
    assert "tampered" in str(exc.value)
    assert "旧鍵で検証できない" in str(exc.value)


def test_resign_refuses_on_content_mismatch(tmp_path, monkeypatch):
    """ファイル名（<content_sha8>-<receipt_sha8>.json）が内容と一致しない。"""
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    old_key = b"old-key-for-mismatch"
    _make_receipt_record(tmp_path, "mismatch", old_key, filename="deadbeef-12345678.json")
    _set_resign_env(monkeypatch, old_key.decode("utf-8"), "new" * 32)

    with pytest.raises(promote.PromoteError) as exc:
        promote.resign_receipts(base=tmp_path)
    assert "mismatch" in str(exc.value)
    assert "ファイル名が署名内容と一致しない" in str(exc.value)


def test_resign_refuses_when_new_key_equals_old_key(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    _make_receipt_record(tmp_path, "same-key", b"k" * 40)
    _set_resign_env(monkeypatch, "k" * 40, "k" * 40)  # 同一鍵（key_id 一致）

    with pytest.raises(promote.PromoteError) as exc:
        promote.resign_receipts(base=tmp_path)
    assert "同一" in str(exc.value)


# ---------------------------------------------------------------------------
# 2g: run_remote_promote（S-08b）
# ---------------------------------------------------------------------------


def _remote_config(tmp_path, *, origin_instance: str = "modal-fixed"):
    (tmp_path / "remote_sources.json").write_text(
        json.dumps(
            {
                "test-source": {
                    "hub_base_url": "https://hub.example",
                    "quarantine_read_token_path": str(tmp_path / "quarantine_read_token.json"),
                    "origin_instance": origin_instance,
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "quarantine_read_token.json").write_text(
        json.dumps({"token": "tok-123"}), encoding="utf-8"
    )


def test_remote_promote_aborts_on_origin_mismatch_without_signing_or_push(
    tmp_path, monkeypatch, quarantine, signing_env, tty_yes, lock_at_tmp
):
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: tmp_path / "hermes_skills")
    _remote_config(tmp_path, origin_instance="modal-fixed")
    content = _skill_md("remote-skill", session_id="sess-r")
    monkeypatch.setattr(
        promote, "fetch_quarantine_list",
        lambda cfg, **kwargs: {
            "skills": [
                {
                    "name": "remote-skill",
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "origin_instance": "self-declared-other",  # 設定値と食い違う
                    "distilled_from_session_id": "sess-r",
                    "published_at": "2026-08-17T00:00:00Z",
                }
            ]
        },
    )
    not_called: list[str] = []
    for fn in ("confirm_or_abort", "write_receipt", "push_to_lane_c", "install_confirmed_skill"):
        _spy(monkeypatch, fn, not_called)

    with pytest.raises(promote.PromoteError) as exc:
        promote.run_remote_promote("test-source")

    assert "自己申告 origin_instance" in str(exc.value)
    assert not_called == []  # 署名・push・確認すら行わず中断


def test_remote_promote_aborts_on_sha256_mismatch(tmp_path, monkeypatch, quarantine):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    _remote_config(tmp_path)
    content = _skill_md("remote-bad").encode("utf-8")
    monkeypatch.setattr(
        promote, "fetch_quarantine_list",
        lambda cfg, **kwargs: {
            "skills": [
                {
                    "name": "remote-bad",
                    "content": content.decode("utf-8"),
                    "content_sha256": "0" * 64,  # 実測と不一致（無検証で信用しない）
                    "origin_instance": "modal-fixed",
                }
            ]
        },
    )

    with pytest.raises(promote.PromoteError) as exc:
        promote.run_remote_promote("test-source")
    assert "sha256" in str(exc.value)


def test_remote_promote_uses_fixed_origin_and_provenance(
    tmp_path, monkeypatch, quarantine, signing_env, tty_yes, lock_at_tmp
):
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)
    _remote_config(tmp_path, origin_instance="modal-fixed")
    content = _skill_md("remote-ok", session_id="sess-r")
    monkeypatch.setattr(
        promote, "fetch_quarantine_list",
        lambda cfg, **kwargs: {
            "skills": [
                {
                    "name": "remote-ok",
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "origin_instance": "modal-fixed",  # 設定値と一致
                    "distilled_from_session_id": "sess-r",
                    "published_at": "2026-08-17T00:00:00Z",
                }
            ]
        },
    )
    receipt_kwargs: dict = {}
    push_kwargs: dict = {}
    original_write_receipt = promote.write_receipt
    original_push = promote.push_to_lane_c
    calls: list[str] = []

    def _recording_write_receipt(name, content_bytes, digest, seq, promoted_at_ms, **kwargs):
        receipt_kwargs.update(kwargs)
        return original_write_receipt(
            name, content_bytes, digest, seq, promoted_at_ms, **kwargs
        )

    def _recording_push(**kwargs):
        calls.append("push")
        push_kwargs.update(kwargs)
        return original_push(**kwargs)

    monkeypatch.setattr(promote, "write_receipt", _recording_write_receipt)
    monkeypatch.setattr(promote, "push_to_lane_c", _recording_push)

    promote.run_remote_promote("test-source")

    # 署名の origin_instance は接続先設定の固定値（自分自身の instance_id ではない）
    assert receipt_kwargs["origin_instance"] == "modal-fixed"
    assert push_kwargs["origin_instance"] == "modal-fixed"
    assert push_kwargs["distilled_from_session_id"] == "sess-r"
    # provenance は f"remote-promote:{cfg.origin_instance}"
    log_record = json.loads((tmp_path / "promote_log.jsonl").read_text(encoding="utf-8").strip())
    assert log_record["provenance"] == "remote-promote:modal-fixed"
    assert (hermes_root / "remote-ok" / "SKILL.md").read_text(encoding="utf-8") == content


def test_remote_promote_missing_source_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    with pytest.raises(promote.PromoteError):
        promote.run_remote_promote("no-such-source")
