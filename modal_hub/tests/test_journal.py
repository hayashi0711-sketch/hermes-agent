"""modal_hub/tests/test_journal.py — hh_hooks/journal.py の不変条件テスト。

`hh_hooks/` はパッケージではないため、リポジトリルートを sys.path へ入れて
モジュールを直接 import する（`hh_hooks/tool_gate.py` 自身が採用している
のと同じ「素朴なモジュール import」の流儀を、テスト側も踏襲する）。
"""

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

import journal  # noqa: E402


def test_journal_path_is_deterministic_and_hash_based(tmp_path):
    p1 = journal.journal_path_for_session("sess-abc", base=tmp_path)
    p2 = journal.journal_path_for_session("sess-abc", base=tmp_path)
    assert p1 == p2
    assert "sess-abc" not in p1.name  # ファイル名は生の session_id を含まない
    assert p1.suffix == ".jsonl"


def test_journal_path_rejects_empty_session_id(tmp_path):
    with pytest.raises(ValueError):
        journal.journal_path_for_session("", base=tmp_path)


def test_append_then_read_round_trip(tmp_path):
    journal.append_journal_entry(
        "sess-1", {"tool_name": "Bash", "status": "ok"}, base=tmp_path
    )
    journal.append_journal_entry(
        "sess-1", {"tool_name": "Read", "status": "error"}, base=tmp_path
    )
    entries = journal.read_journal_entries("sess-1", base=tmp_path)
    assert [e["tool_name"] for e in entries] == ["Bash", "Read"]


def test_read_journal_entries_missing_file_returns_empty(tmp_path):
    assert journal.read_journal_entries("no-such-session", base=tmp_path) == []


def test_read_journal_entries_skips_malformed_lines(tmp_path):
    path = journal.journal_path_for_session("sess-2", base=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"tool_name": "Bash", "status": "ok"}\nnot json\n\n', encoding="utf-8")
    entries = journal.read_journal_entries("sess-2", base=tmp_path)
    assert len(entries) == 1
    assert entries[0]["tool_name"] == "Bash"


def _run_main_with_stdin(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc_info:
        journal.main()
    assert exc_info.value.code == 0


def test_main_appends_entry_for_valid_post_tool_call(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "hh_agent_home", lambda: tmp_path)
    payload = {
        "hook_event_name": "post_tool_call",
        "tool_name": "Bash",
        "session_id": "sess-live",
        "extra": {
            "status": "ok",
            "error_type": None,
            "duration_ms": 42.5,
            "tool_call_id": "call-1",
        },
    }
    _run_main_with_stdin(monkeypatch, payload)

    entries = journal.read_journal_entries("sess-live", base=tmp_path)
    assert len(entries) == 1
    assert entries[0]["tool_name"] == "Bash"
    assert entries[0]["status"] == "ok"
    assert entries[0]["tool_call_id"] == "call-1"
    assert entries[0]["duration_ms"] == 42.5


def test_main_is_fail_open_on_garbage_stdin(tmp_path, monkeypatch):
    """壊れた入力でもツール実行を止めない（exit 0）。ジャーナルには何も
    書かれない。"""
    monkeypatch.setattr(journal, "hh_agent_home", lambda: tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not valid json {{{"))
    with pytest.raises(SystemExit) as exc_info:
        journal.main()
    assert exc_info.value.code == 0
    assert not (tmp_path / "journal").exists()


def test_main_ignores_missing_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "hh_agent_home", lambda: tmp_path)
    payload = {"tool_name": "Bash", "extra": {"status": "ok"}}
    _run_main_with_stdin(monkeypatch, payload)
    assert not (tmp_path / "journal").exists()


def test_main_ignores_unrecognized_status(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "hh_agent_home", lambda: tmp_path)
    payload = {
        "tool_name": "Bash",
        "session_id": "sess-x",
        "extra": {"status": "not-a-real-status"},
    }
    _run_main_with_stdin(monkeypatch, payload)
    assert journal.read_journal_entries("sess-x", base=tmp_path) == []
