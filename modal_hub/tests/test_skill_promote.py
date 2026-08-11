"""modal_hub/tests/test_skill_promote.py — scripts/hh_skill_promote.py の不変条件テスト。

07_Phase1b_Spec.md §4.2（TOCTOU・TTY必須・クラッシュ回復・create-or-match
ではなく退避方式）を検証する。`agent.skill_utils`/`hermes_constants` への
実アクセスは `assert_staging_root_is_safe` 経由でのみ発生するため、その
下請け関数を monkeypatch して隔離する。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import hh_skill_promote as promote  # noqa: E402


def _skill_md(name: str, description: str = "d", *, session_id: str = None) -> str:
    extra = f"distilled_from_session_id: {session_id}\n" if session_id else ""
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\nBody.\n"


@pytest.fixture(autouse=True)
def _isolate_hermes_scan_dirs(monkeypatch):
    """`assert_staging_root_is_safe` が実ホームディレクトリへ触らないよう、
    デフォルトでは「候補ゼロ（衝突なし）」に固定する。個別テストで上書きする。
    """
    monkeypatch.setattr(promote, "_existing_hermes_scan_dirs", lambda: [])
    monkeypatch.setattr(promote, "_declared_hermes_scan_dirs_including_nonexistent", lambda: [])


@pytest.fixture()
def quarantine(tmp_path, monkeypatch):
    from modal_hub.services import skill_quarantine as sq

    # promote.py と skill_quarantine.py は意図的に別々の hh_agent_home()
    # を持つ（本ファイルのプロダクションコードでは常に同じ実ホームを指す）。
    # テストではその両方を同じ tmp_path へ向けないと、
    # read_quarantined_skill() が base 未指定で実ホームを見に行ってしまう。
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    monkeypatch.setattr(sq, "hh_agent_home", lambda: tmp_path)
    return sq


# ---------------------------------------------------------------------------
# validate_name
# ---------------------------------------------------------------------------


def test_validate_name_accepts_valid():
    assert promote.validate_name("ok-name") == "ok-name"


def test_validate_name_rejects_invalid():
    with pytest.raises(promote.PromoteError):
        promote.validate_name("Not Valid")


# ---------------------------------------------------------------------------
# read_quarantined_skill
# ---------------------------------------------------------------------------


def test_read_quarantined_skill_reads_bytes_and_digest(tmp_path, quarantine):
    content = _skill_md("alpha")
    quarantine.materialize("qid-1", "alpha", content, base=tmp_path)

    content_bytes, digest = promote.read_quarantined_skill("alpha", base=tmp_path)
    assert content_bytes == content.encode("utf-8")
    assert digest == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_read_quarantined_skill_missing_raises(tmp_path, quarantine):
    with pytest.raises(promote.PromoteError):
        promote.read_quarantined_skill("no-such-skill", base=tmp_path)


def test_read_quarantined_skill_rejects_symlink_escape(tmp_path, quarantine):
    root = quarantine.quarantine_root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(_skill_md("evil"), encoding="utf-8")

    try:
        (root / "evil").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")

    with pytest.raises(promote.PromoteError):
        promote.read_quarantined_skill("evil", base=tmp_path)


# ---------------------------------------------------------------------------
# _escape_control_and_ansi
# ---------------------------------------------------------------------------


def test_escape_control_and_ansi_neutralizes_escape_byte():
    raw = "before\x1b[31mred\x1b[0mafter"
    escaped = promote._escape_control_and_ansi(raw)
    assert "\x1b" not in escaped
    assert "\\x1b" in escaped


def test_escape_control_and_ansi_preserves_newlines_and_tabs():
    raw = "line1\nline2\tindented"
    assert promote._escape_control_and_ansi(raw) == raw


# ---------------------------------------------------------------------------
# confirm_or_abort: TTY 必須
# ---------------------------------------------------------------------------


def test_confirm_or_abort_rejects_non_interactive(monkeypatch):
    class _NoTty:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", _NoTty())
    with pytest.raises(promote.PromoteError):
        promote.confirm_or_abort("name", "a" * 64)


def test_confirm_or_abort_accepts_y(monkeypatch):
    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    promote.confirm_or_abort("name", "a" * 64)  # 例外が出なければ成功


def test_confirm_or_abort_rejects_non_y_answer(monkeypatch):
    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    with pytest.raises(promote.PromoteError):
        promote.confirm_or_abort("name", "a" * 64)


# ---------------------------------------------------------------------------
# assert_staging_root_is_safe
# ---------------------------------------------------------------------------


def test_assert_staging_root_is_safe_passes_when_no_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    promote.assert_staging_root_is_safe()  # 例外なし


def test_assert_staging_root_is_safe_rejects_when_staging_is_a_scan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    staging_root = promote._promote_staging_root(tmp_path)
    monkeypatch.setattr(promote, "_existing_hermes_scan_dirs", lambda: [staging_root.resolve()])
    with pytest.raises(promote.PromoteError):
        promote.assert_staging_root_is_safe()


def test_assert_staging_root_is_safe_rejects_ancestor_relationship(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    staging_root = promote._promote_staging_root(tmp_path)
    # スキャンルートがステージング領域の**祖先**であるケース。
    monkeypatch.setattr(
        promote, "_declared_hermes_scan_dirs_including_nonexistent", lambda: [tmp_path.resolve()]
    )
    with pytest.raises(promote.PromoteError):
        promote.assert_staging_root_is_safe()


# ---------------------------------------------------------------------------
# install_staged_skill / self_heal_orphaned_promotions
# ---------------------------------------------------------------------------


def _stage(tmp_path, monkeypatch, name: str, content: bytes):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    digest = hashlib.sha256(content).hexdigest()
    staged_dir = promote._write_staging(name, content, digest, base=tmp_path)
    return staged_dir, digest


def test_install_new_skill_moves_staging_into_place(tmp_path, monkeypatch):
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)
    staged_dir, _ = _stage(tmp_path, monkeypatch, "new-skill", b"content-a")

    backup = promote.install_staged_skill("new-skill", staged_dir, force=False)

    assert backup is None
    assert (hermes_root / "new-skill" / "SKILL.md").read_bytes() == b"content-a"


def test_install_existing_without_force_raises_and_cleans_staging(tmp_path, monkeypatch):
    hermes_root = tmp_path / "hermes_skills"
    (hermes_root / "dup-skill").mkdir(parents=True)
    (hermes_root / "dup-skill" / "SKILL.md").write_bytes(b"already-there")
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)
    staged_dir, _ = _stage(tmp_path, monkeypatch, "dup-skill", b"content-b")

    with pytest.raises(promote.PromoteError):
        promote.install_staged_skill("dup-skill", staged_dir, force=False)

    assert not staged_dir.exists()  # クリーンアップ済み
    assert (hermes_root / "dup-skill" / "SKILL.md").read_bytes() == b"already-there"  # 無傷


def test_install_existing_with_force_backs_up_then_replaces(tmp_path, monkeypatch):
    hermes_root = tmp_path / "hermes_skills"
    (hermes_root / "force-skill").mkdir(parents=True)
    (hermes_root / "force-skill" / "SKILL.md").write_bytes(b"old-content")
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)
    staged_dir, _ = _stage(tmp_path, monkeypatch, "force-skill", b"new-content")

    backup = promote.install_staged_skill("force-skill", staged_dir, force=True)

    assert backup is not None
    assert Path(backup).name.startswith("force-skill.bak.")
    assert (Path(backup) / "SKILL.md").read_bytes() == b"old-content"
    assert (hermes_root / "force-skill" / "SKILL.md").read_bytes() == b"new-content"


def test_self_heal_completes_orphaned_force_promotion(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)

    staging = promote._promote_staging_root(tmp_path) / "orphan-skill"
    staging.mkdir(parents=True)
    (staging / "SKILL.md").write_bytes(b"orphaned-content")

    backups = promote._promote_backups_root(tmp_path) / "orphan-skill.bak.20260101_000000"
    backups.mkdir(parents=True)
    (backups / "SKILL.md").write_bytes(b"old-content")

    healed = promote.self_heal_orphaned_promotions(base=tmp_path)

    assert healed == ["orphan-skill"]
    assert (hermes_root / "orphan-skill" / "SKILL.md").read_bytes() == b"orphaned-content"
    assert not staging.exists()


def test_self_heal_skips_when_target_already_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)

    staging = promote._promote_staging_root(tmp_path) / "already-there"
    staging.mkdir(parents=True)
    (staging / "SKILL.md").write_bytes(b"staged")

    backups = promote._promote_backups_root(tmp_path) / "already-there.bak.20260101_000000"
    backups.mkdir(parents=True)

    (hermes_root / "already-there").mkdir(parents=True)
    (hermes_root / "already-there" / "SKILL.md").write_bytes(b"lives-here-now")

    healed = promote.self_heal_orphaned_promotions(base=tmp_path)

    assert healed == []
    assert staging.exists()  # 触っていない
    assert (hermes_root / "already-there" / "SKILL.md").read_bytes() == b"lives-here-now"


def test_self_heal_skips_without_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)

    staging = promote._promote_staging_root(tmp_path) / "no-backup"
    staging.mkdir(parents=True)
    (staging / "SKILL.md").write_bytes(b"staged")

    healed = promote.self_heal_orphaned_promotions(base=tmp_path)

    assert healed == []
    assert staging.exists()


# ---------------------------------------------------------------------------
# append_promote_log
# ---------------------------------------------------------------------------


def test_append_promote_log_extracts_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    content = _skill_md("logged-skill", session_id="sess-42").encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()

    promote.append_promote_log(
        name="logged-skill",
        digest=digest,
        content_bytes=content,
        destination=tmp_path / "dest",
        forced=False,
        backup_path=None,
        base=tmp_path,
    )

    log_path = tmp_path / "promote_log.jsonl"
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert record["name"] == "logged-skill"
    assert record["distilled_from_session_id"] == "sess-42"
    assert record["source_digest"] == digest
    assert record["forced"] is False
    assert record["license_confirmed"] is True


def test_append_promote_log_null_session_id_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    content = _skill_md("no-session-skill").encode("utf-8")
    promote.append_promote_log(
        name="no-session-skill",
        digest="abc",
        content_bytes=content,
        destination=tmp_path / "dest",
        forced=False,
        backup_path=None,
        base=tmp_path,
    )
    record = json.loads((tmp_path / "promote_log.jsonl").read_text(encoding="utf-8").strip())
    assert record["distilled_from_session_id"] is None


# ---------------------------------------------------------------------------
# run_promote: end-to-end
# ---------------------------------------------------------------------------


def test_run_promote_end_to_end(tmp_path, monkeypatch, quarantine):
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)

    content = _skill_md("e2e-skill", session_id="sess-e2e")
    quarantine.materialize("qid-e2e", "e2e-skill", content, base=tmp_path)

    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    promote.run_promote("e2e-skill", force=False)

    installed = hermes_root / "e2e-skill" / "SKILL.md"
    assert installed.read_text(encoding="utf-8") == content

    log_record = json.loads((tmp_path / "promote_log.jsonl").read_text(encoding="utf-8").strip())
    assert log_record["distilled_from_session_id"] == "sess-e2e"
    assert log_record["forced"] is False


def test_run_promote_aborts_on_declined_confirmation_writes_nothing(tmp_path, monkeypatch, quarantine):
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)

    content = _skill_md("declined-skill")
    quarantine.materialize("qid-declined", "declined-skill", content, base=tmp_path)

    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    with pytest.raises(promote.PromoteError):
        promote.run_promote("declined-skill", force=False)


def test_run_promote_rejects_unsafe_staging_root_before_self_heal(tmp_path, monkeypatch):
    """2026-08-11 Codex レビュー Critical 指摘の回帰テスト: scan-root の
    重複チェックはセルフヒールより先に走り、セルフヒールが実行される前に
    中止すること（そうでなければ、TTY 拒否や不安全な config.yaml のもとでも
    セルフヒールが先に `~/.hermes/skills/` へファイルを移動してしまう）。
    """
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)

    # 孤児化した staging + backup のペアを用意する(セルフヒールの対象)。
    staging = promote._promote_staging_root(tmp_path) / "orphan-skill"
    staging.mkdir(parents=True)
    (staging / "SKILL.md").write_bytes(b"orphaned-content")
    backups = promote._promote_backups_root(tmp_path) / "orphan-skill.bak.20260101_000000"
    backups.mkdir(parents=True)

    # staging_root 自体が scan 対象と重なっている、という不安全な状態を模す。
    staging_root = promote._promote_staging_root(tmp_path)
    monkeypatch.setattr(promote, "_existing_hermes_scan_dirs", lambda: [staging_root.resolve()])
    monkeypatch.setattr(promote, "_declared_hermes_scan_dirs_including_nonexistent", lambda: [])

    with pytest.raises(promote.PromoteError):
        promote.run_promote("orphan-skill", force=False)

    # セルフヒールは実行されていない(孤児の staging がまだそのまま残っている)。
    assert staging.exists()
    assert not (hermes_root / "orphan-skill").exists()

    assert not hermes_root.exists()
