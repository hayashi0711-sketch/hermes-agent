"""modal_hub/tests/test_hh_distill_codex_fixes.py — 2026-08-11 Codex レビュー
（MiniMax 作成の scripts/hh_distill.py に対する Critical 指摘）の回帰テスト。

`hh_distill.py` は `sys.path` 経由のスクリプト import であり `test_distiller.py`
は動的 import ヘルパー（`importlib.util.spec_from_file_location` +
`sys.modules[name] = module` 登録）を使っている。同じ流儀をここでも使う。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_HH_DISTILL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "hh_distill.py"


def _load_hh_distill(name: str):
    spec = importlib.util.spec_from_file_location(name, _HH_DISTILL_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def hh_distill():
    return _load_hh_distill(f"hh_distill_codexfix_{id(object())}")


# ---------------------------------------------------------------------------
# Critical #1: バッチ結果は result_obj.result の内側から type/message を読む
# ---------------------------------------------------------------------------


class _FakeResults:
    def __init__(self, items):
        self._items = items

    def __iter__(self):
        return iter(self._items)


class _FakeBatchesAPI:
    def __init__(self, batch, results):
        self._batch = batch
        self._results = results

    def retrieve(self, batch_id):
        return self._batch

    def results(self, batch_id):
        return _FakeResults(self._results)


class _FakeAnthropicClient:
    def __init__(self, batch, results):
        self.messages = type("M", (), {"batches": _FakeBatchesAPI(batch, results)})()


def _make_submitted_entry(hh_distill, tmp_path, qid: str) -> Path:
    submitted_dir = tmp_path / "distill_queue" / "submitted"
    submitted_dir.mkdir(parents=True, exist_ok=True)
    path = submitted_dir / f"{qid}.json"
    path.write_text(
        json.dumps({"queue_entry_id": qid, "session_id": "sess-1", "batch_id": "batch-x"}),
        encoding="utf-8",
    )
    return path


def test_process_batch_results_reads_nested_result_object(hh_distill, tmp_path, monkeypatch):
    """`MessageBatchIndividualResponse(custom_id, result={...})` の**内側**
    の `result.type`/`result.message` を読むこと（外側をそのまま渡さない）。
    """
    qid = "s" + "a" * 32
    entry_path = _make_submitted_entry(hh_distill, tmp_path, qid)

    outer_result = {
        "custom_id": qid,
        "result": {
            "type": "succeeded",
            "message": {
                "content": [
                    {"type": "text", "text": '{"decision":"not_extractable","reason":"x"}'}
                ]
            },
        },
    }
    batch = {"processing_status": "ended"}
    client = _FakeAnthropicClient(batch, [outer_result])

    summary = hh_distill._process_batch_results(
        "batch-x", [(qid, entry_path)], anthropic_client=client, base=tmp_path
    )

    assert summary["completed_not_extracted"] == 1
    completed_file = tmp_path / "distill_queue" / "completed" / f"{qid}.json"
    assert completed_file.is_file()


def test_process_batch_results_routes_errored_to_failed_not_completed(hh_distill, tmp_path):
    """§2.3 手順4: `result.type in (errored, canceled, expired)` は `failed/`
    へ（`not_extractable` として `completed/` へ吸収してはならない）。"""
    qid = "s" + "b" * 32
    entry_path = _make_submitted_entry(hh_distill, tmp_path, qid)

    outer_result = {
        "custom_id": qid,
        "result": {"type": "errored", "error": {"type": "invalid_request", "message": "boom"}},
    }
    batch = {"processing_status": "ended"}
    client = _FakeAnthropicClient(batch, [outer_result])

    summary = hh_distill._process_batch_results(
        "batch-x", [(qid, entry_path)], anthropic_client=client, base=tmp_path
    )

    assert summary["failed"] == 1
    assert summary["completed_not_extracted"] == 0
    failed_file = tmp_path / "distill_queue" / "failed" / f"{qid}.json"
    assert failed_file.is_file()
    completed_file = tmp_path / "distill_queue" / "completed" / f"{qid}.json"
    assert not completed_file.exists()


# ---------------------------------------------------------------------------
# Critical #3: pending エントリはファイル名から queue_entry_id を再確認する
# ---------------------------------------------------------------------------


def test_load_pending_entries_rejects_content_id_mismatching_filename(hh_distill, tmp_path):
    real_qid = hh_distill.compute_queue_entry_id("sess-real", "turn-1")
    pending_dir = tmp_path / "distill_queue" / "pending"
    pending_dir.mkdir(parents=True)
    # ファイル名は正規の queue_entry_id だが、中身は別セッションを詐称する。
    (pending_dir / f"{real_qid}.json").write_text(
        json.dumps(
            {
                "queue_entry_id": real_qid,
                "session_id": "sess-attacker-controlled",
                "turn_id": "turn-1",
                "cwd": "C:/some/other/path",
                "completed": True,
                "interrupted": False,
            }
        ),
        encoding="utf-8",
    )

    entries = hh_distill._load_pending_entries(base=tmp_path)
    assert entries == []


def test_load_pending_entries_accepts_consistent_entry(hh_distill, tmp_path):
    session_id = "sess-real"
    turn_id = "turn-1"
    qid = hh_distill.compute_queue_entry_id(session_id, turn_id)
    pending_dir = tmp_path / "distill_queue" / "pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / f"{qid}.json").write_text(
        json.dumps(
            {
                "queue_entry_id": qid,
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": "C:/real/path",
                "completed": True,
                "interrupted": False,
            }
        ),
        encoding="utf-8",
    )

    entries = hh_distill._load_pending_entries(base=tmp_path)
    assert len(entries) == 1
    assert entries[0].queue_entry_id == qid
    assert entries[0].session_id == session_id


# ---------------------------------------------------------------------------
# Critical #2: run の排他ロック
# ---------------------------------------------------------------------------


def test_run_lock_prevents_concurrent_acquisition(hh_distill, tmp_path):
    lock_path = hh_distill._acquire_run_lock(tmp_path)
    assert lock_path.is_file()
    with pytest.raises(RuntimeError):
        hh_distill._acquire_run_lock(tmp_path)


def test_run_lock_released_allows_reacquire(hh_distill, tmp_path):
    lock_path = hh_distill._acquire_run_lock(tmp_path)
    hh_distill._release_run_lock(lock_path)
    # 再取得できる(例外が出なければ成功)。
    lock_path2 = hh_distill._acquire_run_lock(tmp_path)
    assert lock_path2 == lock_path


def test_run_lock_steals_stale_lock(hh_distill, tmp_path):
    lock_path = tmp_path / "distill_queue" / "run.lock"
    lock_path.parent.mkdir(parents=True)
    stale_time = time.time() - hh_distill._RUN_LOCK_STALE_SECONDS - 60
    lock_path.write_text(json.dumps({"pid": 999999, "started_at": stale_time}), encoding="utf-8")

    # 古いロックは奪ってよい(例外が出なければ成功)。
    new_lock = hh_distill._acquire_run_lock(tmp_path)
    assert new_lock == lock_path


# ---------------------------------------------------------------------------
# Critical #4: publish の queue_entry_id 検証・パス封じ込め
# ---------------------------------------------------------------------------


def test_publish_pending_entries_ignores_completed_file_with_unsafe_stem(
    hh_distill, tmp_path, monkeypatch
):
    """`completed/` のファイル名自体は `_list_state_files()` で既に検証
    済みだが、念のため `_publish_pending_entries()` 内の qid 導出（`path.stem`）
    が正規表現チェックを通ることを確認する回帰テスト。"""
    monkeypatch.setattr(hh_distill, "_load_hub_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(hh_distill, "_load_agent_token", lambda: "hha1.x.y")

    completed_dir = tmp_path / "distill_queue" / "completed"
    completed_dir.mkdir(parents=True)
    qid = "s" + "c" * 32
    (completed_dir / f"{qid}.json").write_text(
        json.dumps(
            {
                "queue_entry_id": qid,
                "extracted": True,
                "publish_status": "pending",
                "name": "some-skill",
            }
        ),
        encoding="utf-8",
    )
    # .materialized/<qid>.json が存在しない -> output_path が引けず publish は
    # 何もせず素通りする(例外を投げない)ことだけを確認する。
    summary = hh_distill._publish_pending_entries(base=tmp_path)
    assert summary["published"] == 0

    # 2026-08-11 Codex 指摘 Medium の修正確認: マーカー欠落でも
    # publish_attempts が消費されること(修正前は無限に "pending" のまま
    # attempts が増えなかった)。
    assert summary["still_pending"] == 1
    written = json.loads((completed_dir / f"{qid}.json").read_text(encoding="utf-8"))
    assert written["publish_attempts"] == 1
    assert written["publish_status"] == "pending"
    assert written["publish_last_error"] == "materialized_marker_missing_or_invalid"


def test_publish_pending_entries_records_output_path_outside_quarantine(
    hh_distill, tmp_path, monkeypatch
):
    """`.materialized/<qid>.json` の `output_path` が隔離領域の外を指す
    場合も、素通りせず publish_attempts を消費すること。"""
    monkeypatch.setattr(hh_distill, "_load_hub_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(hh_distill, "_load_agent_token", lambda: "hha1.x.y")
    monkeypatch.setattr(
        hh_distill.skill_quarantine, "_materialized_dir", lambda base: tmp_path / "distill_queue" / ".materialized"
    )
    monkeypatch.setattr(
        hh_distill.skill_quarantine, "quarantine_root", lambda base: tmp_path / "quarantine"
    )

    completed_dir = tmp_path / "distill_queue" / "completed"
    completed_dir.mkdir(parents=True)
    qid = "s" + "1" * 32
    (completed_dir / f"{qid}.json").write_text(
        json.dumps(
            {
                "queue_entry_id": qid,
                "extracted": True,
                "publish_status": "pending",
                "name": "some-skill",
            }
        ),
        encoding="utf-8",
    )
    materialized_dir = tmp_path / "distill_queue" / ".materialized"
    materialized_dir.mkdir(parents=True)
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("not a skill", encoding="utf-8")
    (materialized_dir / f"{qid}.json").write_text(
        json.dumps({"output_path": str(outside_file)}), encoding="utf-8"
    )

    summary = hh_distill._publish_pending_entries(base=tmp_path)
    assert summary["still_pending"] == 1
    written = json.loads((completed_dir / f"{qid}.json").read_text(encoding="utf-8"))
    assert written["publish_attempts"] == 1
    assert written["publish_last_error"] == "output_path_outside_quarantine"


def test_publish_pending_entries_records_output_read_error(hh_distill, tmp_path, monkeypatch):
    """`output_path` が隔離領域の中でも、実ファイルが読めなければ
    publish_attempts を消費すること。"""
    monkeypatch.setattr(hh_distill, "_load_hub_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(hh_distill, "_load_agent_token", lambda: "hha1.x.y")
    monkeypatch.setattr(
        hh_distill.skill_quarantine, "_materialized_dir", lambda base: tmp_path / "distill_queue" / ".materialized"
    )
    monkeypatch.setattr(
        hh_distill.skill_quarantine, "quarantine_root", lambda base: tmp_path / "quarantine"
    )

    completed_dir = tmp_path / "distill_queue" / "completed"
    completed_dir.mkdir(parents=True)
    qid = "s" + "2" * 32
    (completed_dir / f"{qid}.json").write_text(
        json.dumps(
            {
                "queue_entry_id": qid,
                "extracted": True,
                "publish_status": "pending",
                "name": "some-skill",
            }
        ),
        encoding="utf-8",
    )
    materialized_dir = tmp_path / "distill_queue" / ".materialized"
    materialized_dir.mkdir(parents=True)
    quarantine_dir = tmp_path / "quarantine"
    quarantine_dir.mkdir(parents=True)
    missing_file = quarantine_dir / f"{qid}.md"  # 意図的に作らない
    (materialized_dir / f"{qid}.json").write_text(
        json.dumps({"output_path": str(missing_file)}), encoding="utf-8"
    )

    summary = hh_distill._publish_pending_entries(base=tmp_path)
    assert summary["still_pending"] == 1
    written = json.loads((completed_dir / f"{qid}.json").read_text(encoding="utf-8"))
    assert written["publish_attempts"] == 1
    assert written["publish_last_error"] == "output_read_error"


def test_publish_pending_entries_records_missing_name(hh_distill, tmp_path, monkeypatch):
    """`name` が欠落/非文字列でも publish_attempts を消費すること
    （invalid_payload と同種のローカル失敗として扱う）。"""
    monkeypatch.setattr(hh_distill, "_load_hub_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(hh_distill, "_load_agent_token", lambda: "hha1.x.y")

    completed_dir = tmp_path / "distill_queue" / "completed"
    completed_dir.mkdir(parents=True)
    qid = "s" + "3" * 32
    (completed_dir / f"{qid}.json").write_text(
        json.dumps(
            {
                "queue_entry_id": qid,
                "extracted": True,
                "publish_status": "pending",
                # "name" フィールド自体が無い。
            }
        ),
        encoding="utf-8",
    )

    summary = hh_distill._publish_pending_entries(base=tmp_path)
    assert summary["still_pending"] == 1
    written = json.loads((completed_dir / f"{qid}.json").read_text(encoding="utf-8"))
    assert written["publish_attempts"] == 1
    assert written["publish_last_error"] == "missing_or_invalid_name"


# ---------------------------------------------------------------------------
# 判断待ち6(a): publish のローカル失敗も PUBLISH_MAX_ATTEMPTS で abandoned へ
# ---------------------------------------------------------------------------


def test_publish_local_failure_abandons_after_max_attempts(hh_distill, tmp_path, monkeypatch):
    monkeypatch.setattr(hh_distill, "_load_hub_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(hh_distill, "_load_agent_token", lambda: "hha1.x.y")

    completed_dir = tmp_path / "distill_queue" / "completed"
    completed_dir.mkdir(parents=True)
    qid = "s" + "d" * 32
    (completed_dir / f"{qid}.json").write_text(
        json.dumps(
            {
                "queue_entry_id": qid,
                "extracted": True,
                "publish_status": "pending",
                "name": "some-skill",
                "publish_attempts": hh_distill.PUBLISH_MAX_ATTEMPTS - 1,
            }
        ),
        encoding="utf-8",
    )
    summary = hh_distill._publish_pending_entries(base=tmp_path)
    assert summary["abandoned"] == 1
    written = json.loads((completed_dir / f"{qid}.json").read_text(encoding="utf-8"))
    assert written["publish_attempts"] == hh_distill.PUBLISH_MAX_ATTEMPTS
    assert written["publish_status"] == "abandoned"


# ---------------------------------------------------------------------------
# 判断待ち6(b): 401/403 は即abandonedでなく他のリトライ可能エラーと同じ経路
# ---------------------------------------------------------------------------


def _make_publishable_completed_entry(hh_distill, tmp_path, qid: str, *, publish_attempts: int = 0) -> Path:
    """`_post_publish` まで到達する completed/ + .materialized/ エントリを作る。"""
    completed_dir = tmp_path / "distill_queue" / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    path = completed_dir / f"{qid}.json"
    path.write_text(
        json.dumps(
            {
                "queue_entry_id": qid,
                "extracted": True,
                "publish_status": "pending",
                "name": "some-skill",
                "publish_attempts": publish_attempts,
            }
        ),
        encoding="utf-8",
    )

    materialized_dir = tmp_path / "distill_queue" / ".materialized"
    materialized_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir = tmp_path / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    skill_md = quarantine_dir / f"{qid}.md"
    skill_md.write_text(
        "---\nname: some-skill\n---\n\nBody.\n", encoding="utf-8"
    )
    (materialized_dir / f"{qid}.json").write_text(
        json.dumps({"output_path": str(skill_md)}), encoding="utf-8"
    )
    return path


def test_401_aborts_publish_phase_without_consuming_attempts(hh_distill, tmp_path, monkeypatch):
    """2026-08-11 Codex design concern の反映確認: 401/403 はエントリ個別の
    budget を消費させず、publish フェーズ全体をその場で打ち切ること。
    1つの壊れたトークンで pending 全件が retry を重ねた末に abandoned に
    なるのを防ぐ(旧設計は各エントリへ1リクエストずつ送っていた)。"""
    monkeypatch.setattr(hh_distill, "_load_hub_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(hh_distill, "_load_agent_token", lambda: "hha1.x.y")
    monkeypatch.setattr(
        hh_distill.skill_quarantine, "_materialized_dir", lambda base: tmp_path / "distill_queue" / ".materialized"
    )
    monkeypatch.setattr(
        hh_distill.skill_quarantine, "quarantine_root", lambda base: tmp_path / "quarantine"
    )
    monkeypatch.setattr(hh_distill, "hh_agent_home", lambda: tmp_path)
    calls = []

    def fake_post_publish(hub_url, token, payload):
        calls.append(payload)
        return 401, None

    monkeypatch.setattr(hh_distill, "_post_publish", fake_post_publish)

    # `_list_state_files()` は `sorted(state_dir.iterdir())` の辞書順で
    # 処理するため、"s999...".json は "seee...".json より先に来る
    # （qid_b が実際に1回リクエストされる側）。2026-08-11 Codex 指摘:
    # 「実際にリクエストされた側」を検証しないと、break 前に attempts を
    # 消費してしまう回帰があっても気づけない。両方を検証する。
    qid_a = "s" + "e" * 32
    qid_b = "s" + "9" * 32
    path_a = _make_publishable_completed_entry(hh_distill, tmp_path, qid_a)
    path_b = _make_publishable_completed_entry(hh_distill, tmp_path, qid_b)

    summary = hh_distill._publish_pending_entries(base=tmp_path)
    assert summary["auth_error"] == "http_401"
    assert summary["abandoned"] == 0
    assert summary["still_pending"] == 0
    # 最初の 401 で即座に打ち切るため、2件目には一度もリクエストしない。
    assert len(calls) == 1
    for path in (path_a, path_b):
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["publish_attempts"] == 0
        assert written["publish_status"] == "pending"


# ---------------------------------------------------------------------------
# 判断待ち6(c): manifest はエントリ移動より先に書き込む(クラッシュ耐性)
# ---------------------------------------------------------------------------


def test_manifest_written_before_entries_moved_to_submitting(hh_distill, tmp_path, monkeypatch):
    """`_write_manifest_then_move_to_submitting()`（`_cmd_run_locked` が実際に
    呼ぶ本番コードパス）が、`_atomic_write_json(manifest)` を
    `_move_pending_to_submitting()` より先に呼ぶこと。

    2026-08-11 Codex 指摘の反映: 以前のテストは2つの呼び出しを手で順番に
    並べて呼んでいるだけで、本番側が誤って逆順に戻っても検出できなかった。
    今回は本番が実際に呼ぶ関数そのものを検証対象にする。また
    `hh_agent_home()` を明示的に monkeypatch し、`tmp_path` の外を
    書き換えないようにする（従来のテストは未パッチのままだった）。
    """
    monkeypatch.setattr(hh_distill, "hh_agent_home", lambda: tmp_path)

    call_order: list[str] = []

    original_write_json = hh_distill._atomic_write_json
    original_move = hh_distill._move_pending_to_submitting

    def tracking_write_json(path, data):
        if isinstance(data, dict) and "manifest_id" in data and "queue_entry_ids" in data:
            call_order.append("write_manifest")
        return original_write_json(path, data)

    def tracking_move(items, manifest_id):
        call_order.append("move_to_submitting")
        return original_move(items, manifest_id)

    monkeypatch.setattr(hh_distill, "_atomic_write_json", tracking_write_json)
    monkeypatch.setattr(hh_distill, "_move_pending_to_submitting", tracking_move)

    pending_dir = tmp_path / "distill_queue" / "pending"
    pending_dir.mkdir(parents=True)
    qid = "s" + "0" * 32
    (pending_dir / f"{qid}.json").write_text(
        json.dumps({"queue_entry_id": qid, "session_id": "sess-1"}), encoding="utf-8"
    )

    class _Item:
        def __init__(self, pending, request):
            self.pending = pending
            self.request = request

    class _Pending:
        def __init__(self, qid, source_path):
            self.queue_entry_id = qid
            self.source_path = source_path

    item = _Item(
        _Pending(qid, pending_dir / f"{qid}.json"),
        {"custom_id": qid, "params": {"messages": [{"content": "x"}]}},
    )

    manifest_id = "abc123"
    manifest_path, manifest = hh_distill._make_manifest(manifest_id, [item])
    hh_distill._write_manifest_then_move_to_submitting(manifest_path, manifest, [item], manifest_id)

    assert call_order == ["write_manifest", "move_to_submitting"]
    assert manifest_path.is_file()
    assert manifest_path.is_relative_to(tmp_path)


# ---------------------------------------------------------------------------
# 判断待ち6(a)続き: _force_move_to_pending の孤立 submitting/ コピー掃除
# ---------------------------------------------------------------------------


def test_force_move_to_pending_cleans_up_stray_submitting_copy(hh_distill, tmp_path, monkeypatch):
    """`_move_pending_to_submitting()` は submitting/ へ書き込んでから
    元の pending/ を unlink する順序のため、その一瞬でクラッシュすると
    両方にコピーが残りうる（2026-08-11 Codex 指摘 Medium）。
    `_find_entry()` は QUEUE_STATES の並び順(pending が先)でこちらを
    見つけて早期 return するため、孤立した submitting/ 側のコピーも
    合わせて削除すること。"""
    qid = "s" + "4" * 32
    pending_dir = tmp_path / "distill_queue" / "pending"
    submitting_dir = tmp_path / "distill_queue" / "submitting"
    pending_dir.mkdir(parents=True)
    submitting_dir.mkdir(parents=True)
    (pending_dir / f"{qid}.json").write_text(
        json.dumps({"queue_entry_id": qid, "session_id": "sess-1"}), encoding="utf-8"
    )
    (submitting_dir / f"{qid}.json").write_text(
        json.dumps({"queue_entry_id": qid, "session_id": "sess-1", "manifest_id": "abc"}),
        encoding="utf-8",
    )

    hh_distill._force_move_to_pending(qid, base=tmp_path)

    assert (pending_dir / f"{qid}.json").is_file()
    assert not (submitting_dir / f"{qid}.json").is_file()


# ---------------------------------------------------------------------------
# 判断待ち6(a)続き: status に publish_status_counts を出す
# ---------------------------------------------------------------------------


def test_cmd_status_reports_publish_status_counts(hh_distill, tmp_path, monkeypatch, capsys):
    """2026-08-11 Codex 指摘 Medium の反映確認: マーカー欠落・401/403 の
    retry-then-abandon 化で `publish_attempts`/`publish_status` を消費
    させても、`status` コマンドに出なければ運用者には見えない「見える
    劣化」になっていなかった。"""
    monkeypatch.setattr(hh_distill, "hh_agent_home", lambda: tmp_path)

    completed_dir = tmp_path / "distill_queue" / "completed"
    completed_dir.mkdir(parents=True)
    for i, status in enumerate(["pending", "pending", "abandoned", "published"]):
        qid = "s" + str(i) * 32
        (completed_dir / f"{qid}.json").write_text(
            json.dumps({"queue_entry_id": qid, "extracted": True, "publish_status": status}),
            encoding="utf-8",
        )

    import argparse

    hh_distill.cmd_status(argparse.Namespace())
    out = json.loads(capsys.readouterr().out)
    assert out["publish_status_counts"] == {"pending": 2, "abandoned": 1, "published": 1}
