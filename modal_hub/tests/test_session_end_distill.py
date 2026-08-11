"""modal_hub/tests/test_session_end_distill.py — hh_hooks/session_end_distill.py の不変条件テスト。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HH_HOOKS_DIR = REPO_ROOT / "hh_hooks"
if str(HH_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HH_HOOKS_DIR))

import session_end_distill as sed  # noqa: E402


def test_compute_queue_entry_id_matches_custom_id_regex():
    import re

    entry_id = sed.compute_queue_entry_id("sess-1", "turn-1")
    assert re.match(r"^[a-zA-Z0-9_-]{1,64}$", entry_id)
    assert entry_id.startswith("s")


def test_compute_queue_entry_id_distinguishes_boundary_ambiguity():
    """`("a:b", "c")` と `("a", "b:c")` のような文字列結合の衝突を JSON
    配列エンコードで回避していることを確認する（07_Phase1b_Spec.md §1.3）。
    """
    id_1 = sed.compute_queue_entry_id("a:b", "c")
    id_2 = sed.compute_queue_entry_id("a", "b:c")
    assert id_1 != id_2


def test_compute_queue_entry_id_none_and_empty_turn_id_collapse():
    """spec の `turn_id or ""` は None と "" を区別しない仕様どおりの挙動。"""
    assert sed.compute_queue_entry_id("sess-1", None) == sed.compute_queue_entry_id("sess-1", None)
    assert sed.compute_queue_entry_id("sess-1", None) == sed.compute_queue_entry_id("sess-1", "")


def test_safe_state_file_path_rejects_traversal(tmp_path):
    state_dir = tmp_path / "pending"
    state_dir.mkdir()
    with pytest.raises(sed.PathEscapesStateDirError):
        sed._safe_state_file_path(state_dir, "../../escaped")


def test_safe_state_file_path_accepts_normal_id(tmp_path):
    state_dir = tmp_path / "pending"
    state_dir.mkdir()
    result = sed._safe_state_file_path(state_dir, "s" + "a" * 32)
    assert result.parent == state_dir


def test_load_excluded_roots_missing_config_raises(tmp_path):
    with pytest.raises(sed.ExcludedRootsNotConfiguredError):
        sed.load_excluded_roots(base=tmp_path)


def test_load_excluded_roots_missing_key_raises(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"hub_url": "https://x"}), encoding="utf-8")
    with pytest.raises(sed.ExcludedRootsNotConfiguredError):
        sed.load_excluded_roots(base=tmp_path)


def test_load_excluded_roots_explicit_empty_list_is_allowed(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"excluded_roots": []}), encoding="utf-8")
    assert sed.load_excluded_roots(base=tmp_path) == []


def test_load_excluded_roots_returns_list(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"excluded_roots": ["C:\\notes"]}), encoding="utf-8"
    )
    assert sed.load_excluded_roots(base=tmp_path) == ["C:\\notes"]


def test_is_excluded_cwd_matches_subdirectory(tmp_path):
    root = tmp_path / "notes"
    sub = root / "sub" / "dir"
    sub.mkdir(parents=True)
    assert sed.is_excluded_cwd(str(sub), [str(root)]) is True


def test_is_excluded_cwd_does_not_match_sibling(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    sibling = tmp_path / "other"
    sibling.mkdir()
    assert sed.is_excluded_cwd(str(sibling), [str(root)]) is False


# ---------------------------------------------------------------------------
# main() / _run() の結合テスト
# ---------------------------------------------------------------------------


def _stdin_main(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc_info:
        sed.main()
    assert exc_info.value.code == 0


def _setup_home(tmp_path, monkeypatch, excluded_roots=None):
    monkeypatch.setattr(sed, "hh_agent_home", lambda: tmp_path)
    if excluded_roots is not None:
        (tmp_path / "config.json").write_text(
            json.dumps({"excluded_roots": excluded_roots}), encoding="utf-8"
        )


def test_main_writes_pending_entry(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch, excluded_roots=[])
    payload = {
        "session_id": "sess-1",
        "cwd": str(tmp_path),
        "extra": {"turn_id": "turn-1", "completed": True, "interrupted": False},
    }
    _stdin_main(monkeypatch, payload)

    entry_id = sed.compute_queue_entry_id("sess-1", "turn-1")
    pending_file = tmp_path / "distill_queue" / "pending" / f"{entry_id}.json"
    assert pending_file.is_file()
    data = json.loads(pending_file.read_text(encoding="utf-8"))
    assert data["session_id"] == "sess-1"
    assert data["turn_id"] == "turn-1"
    assert data["completed"] is True


def test_main_does_not_double_enqueue(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch, excluded_roots=[])
    payload = {
        "session_id": "sess-1",
        "cwd": str(tmp_path),
        "extra": {"turn_id": "turn-1"},
    }
    _stdin_main(monkeypatch, payload)
    entry_id = sed.compute_queue_entry_id("sess-1", "turn-1")
    pending_file = tmp_path / "distill_queue" / "pending" / f"{entry_id}.json"
    first_mtime = pending_file.stat().st_mtime_ns

    _stdin_main(monkeypatch, payload)
    assert pending_file.stat().st_mtime_ns == first_mtime  # 上書きされていない


def test_main_skips_excluded_cwd_and_leaves_no_trace(tmp_path, monkeypatch):
    excluded_dir = tmp_path / "notes"
    excluded_dir.mkdir()
    work_dir = excluded_dir / "session-cwd"
    work_dir.mkdir()
    _setup_home(tmp_path, monkeypatch, excluded_roots=[str(excluded_dir)])

    payload = {"session_id": "sess-2", "cwd": str(work_dir), "extra": {"turn_id": "t"}}
    _stdin_main(monkeypatch, payload)

    assert not (tmp_path / "distill_queue" / "pending").exists()
    assert not (tmp_path / "distill_queue" / "enqueue_errors.log").exists()


def test_main_refuses_registration_when_excluded_roots_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(sed, "hh_agent_home", lambda: tmp_path)  # config.json を書かない
    payload = {"session_id": "sess-3", "cwd": str(tmp_path), "extra": {"turn_id": "t"}}
    _stdin_main(monkeypatch, payload)

    assert not (tmp_path / "distill_queue" / "pending").exists()
    log_path = tmp_path / "distill_queue" / "enqueue_errors.log"
    assert log_path.is_file()
    line = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert "excluded_roots" in line["error"] or "registration refused" in line["error"]
