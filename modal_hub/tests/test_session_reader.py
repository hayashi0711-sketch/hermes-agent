"""modal_hub/tests/test_session_reader.py — session_reader.py の不変条件テスト。

07_Phase1b_Spec.md 親設計書 §4.7 / test_phase1b_guards.py の元ガードが
課していた2点を実DBで固定する:

    1. メッセージの並びは AUTOINCREMENT `id` 順であり、`timestamp` 順では
       ない。
    2. `end_reason` を成功判定に使っていない（`SessionMetadata` に
       `end_reason` フィールドが存在しないことと、ソースに文字列が
       出現しないことの両方で固定する）。

`hermes_state.SessionDB` は本タスクの所有範囲外（既存 Hermes 本体）だが、
`read_only=False` でスキーマ初期化まで行い、行は直接 SQL で挿入する
（`_insert_message_rows` 等の内部契約に依存せず、読み取り側の契約だけを
検証するため）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from modal_hub.services import session_reader


def _make_session_db(tmp_path: Path, session_id: str = "sess-1"):
    import hermes_state

    db_path = tmp_path / "state.db"
    db = hermes_state.SessionDB(db_path=db_path, read_only=False)
    db.create_session(session_id, "cli")
    return db, db_path


def test_messages_ordered_by_id_not_by_timestamp(tmp_path):
    db, db_path = _make_session_db(tmp_path)
    now = time.time()
    try:
        # id が小さい方(先に挿入)の timestamp をわざと大きくする。
        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?,?,?,?,1)",
            ("sess-1", "user", "inserted-first-later-timestamp", now + 100),
        )
        # id が大きい方(後に挿入)の timestamp をわざと小さくする。
        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?,?,?,?,1)",
            ("sess-1", "assistant", "inserted-second-earlier-timestamp", now - 100),
        )
        db._conn.commit()
    finally:
        db.close()

    messages = session_reader.get_session_messages("sess-1", db_path=db_path)
    assert [m.content for m in messages] == [
        "inserted-first-later-timestamp",
        "inserted-second-earlier-timestamp",
    ], "timestamp 昇順ではなく id(挿入順)で並んでいなければならない"


def test_inactive_messages_are_excluded(tmp_path):
    db, db_path = _make_session_db(tmp_path)
    try:
        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?,?,?,?,1)",
            ("sess-1", "user", "active-message", time.time()),
        )
        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?,?,?,?,0)",
            ("sess-1", "user", "soft-deleted-message", time.time()),
        )
        db._conn.commit()
    finally:
        db.close()

    messages = session_reader.get_session_messages("sess-1", db_path=db_path)
    assert [m.content for m in messages] == ["active-message"]


def test_missing_session_returns_none_metadata(tmp_path):
    _, db_path = _make_session_db(tmp_path)
    assert session_reader.get_session_metadata("does-not-exist", db_path=db_path) is None


def test_session_metadata_has_no_end_reason_field(tmp_path):
    """`SessionMetadata` は `end_reason` を一切公開しない（本ファイル
    docstring 参照）。dataclass のフィールド集合を直接検査する。
    """
    fields = {f for f in session_reader.SessionMetadata.__dataclass_fields__}
    assert "end_reason" not in fields


def test_message_content_is_truncated_at_max_bytes(tmp_path):
    db, db_path = _make_session_db(tmp_path)
    huge = "x" * 1000
    try:
        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?,?,?,?,1)",
            ("sess-1", "user", huge, time.time()),
        )
        db._conn.commit()
    finally:
        db.close()

    messages = session_reader.get_session_messages("sess-1", db_path=db_path, max_bytes_per_message=100)
    assert len(messages) == 1
    assert messages[0].truncated is True
    assert len(messages[0].content.encode("utf-8")) < 1000


def test_message_content_under_limit_is_not_truncated(tmp_path):
    db, db_path = _make_session_db(tmp_path)
    try:
        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) "
            "VALUES (?,?,?,?,1)",
            ("sess-1", "user", "short", time.time()),
        )
        db._conn.commit()
    finally:
        db.close()

    messages = session_reader.get_session_messages("sess-1", db_path=db_path, max_bytes_per_message=100)
    assert messages[0].truncated is False
    assert messages[0].content == "short"
