"""`routers/approval_gate.py` の純粋な状態機械 — 親設計書 §8.1「approval_gate」。

    - `status_of()` が純関数であること（同じ入力で必ず同じ出力、副作用なし）
    - `decision:` の二重書き込みが 2 回目に必ず失敗すること
    - `lease:` の二重取得が失敗すること
    - `grace_deadline` 超過後の `decision` 書き込みが拒否されること
    - `idempotency_key` が同一なら同じ `approval_id` を返すこと
    - `payload_sha256` / `cwd` / `base_revision` の不一致で `mismatch` になること
    - `decision.at > grace_deadline` の遅延決定が `timeout` になること
    - `claim_deadline` 超過後の `claim` が失敗すること
    - 別 subject のトークンで poll/claim/complete/idempotent request が 404 になること
    - 対象ファイルが symlink に差し替えられた場合に `mismatch` になること

HTTP 層を通した検証は test_approval_gate_http.py にある。こちらは
「時刻とレコードだけで決まる導出ロジック」を隔離して検証する。
"""

from __future__ import annotations

import inspect

import pytest

from modal_hub.routers import approval_gate as gate
from modal_hub.tests.conftest import CLAIM_WINDOW, GRACE, make_req, make_target

CREATED = 1_000_000.0
GRACE_DL = CREATED + GRACE
CLAIM_DL = CREATED + CLAIM_WINDOW


def approved(at: float, by: str = "pwa") -> dict:
    return {"decision": "approved", "at": at, "by": by}


def rejected(at: float) -> dict:
    return {"decision": "rejected", "at": at, "by": "pwa"}


def timed_out(at: float) -> dict:
    return {"decision": "timeout", "at": at, "by": "system"}


def lease(claim_attempt_id: str = "attempt-1", lease_id: str = "lease-1") -> dict:
    return {
        "lease_id": lease_id,
        "claim_attempt_id": claim_attempt_id,
        "claimed_at": CREATED + 10,
        "claimant_sub": "claude_code:desktop-haruki",
    }


# ===========================================================================
# status_of(): 純関数であること
# ===========================================================================


def test_status_of_takes_only_records_and_now() -> None:
    """副作用を持ちようがないシグネチャであること（store を受け取らない）。"""
    params = list(inspect.signature(gate.status_of).parameters)
    assert params == ["req", "decision", "lease", "now"]


def test_status_of_is_deterministic_and_side_effect_free(req_record) -> None:
    """同じ入力から必ず同じ出力。入力レコードを書き換えない。"""
    import copy

    dec = approved(CREATED + 5)
    lea = lease()
    snapshot = copy.deepcopy((req_record, dec, lea))

    results = {gate.status_of(req_record, dec, lea, CREATED + 20) for _ in range(50)}
    assert results == {"claimed"}
    assert (req_record, dec, lea) == snapshot, "status_of が入力レコードを変更した"


@pytest.mark.parametrize(
    "decision,lease_rec,now,expected",
    [
        # pending: 決定が無く、猶予内
        (None, None, CREATED, "pending"),
        (None, None, GRACE_DL, "pending"),  # 境界: now == grace_deadline は pending
        # timeout: 決定が無いまま猶予超過
        (None, None, GRACE_DL + 0.001, "timeout"),
        # approved
        (approved(CREATED + 5), None, CREATED + 10, "approved"),
        # rejected
        (rejected(CREATED + 5), None, CREATED + 10, "rejected"),
        # decision に timeout が書かれていればそのまま timeout
        (timed_out(GRACE_DL), None, CREATED + 10, "timeout"),
        # claimed
        (approved(CREATED + 5), lease(), CREATED + 10, "claimed"),
        # 承認は無期限に有効ではない: claim されないまま claim_deadline 超過 → timeout
        (approved(CREATED + 5), None, CLAIM_DL + 0.001, "timeout"),
        # claim 済みなら claim_deadline を過ぎても claimed のまま（終端）
        (approved(CREATED + 5), lease(), CLAIM_DL + 1000, "claimed"),
    ],
)
def test_status_of_transition_table(req_record, decision, lease_rec, now, expected) -> None:
    assert gate.status_of(req_record, decision, lease_rec, now) == expected


def test_late_decision_after_grace_deadline_is_timeout(req_record) -> None:
    """§8.1 明示項目: `decision.at > grace_deadline` の遅延決定は `timeout`。

    コンテナ停止や競合で猶予後に decision: が入りうる。承認は
    「猶予内に到達した書き込み 1 回」のみが有効。
    """
    late = approved(GRACE_DL + 0.001)
    assert gate.status_of(req_record, late, None, CREATED + 10) == "timeout"


def test_decision_exactly_at_grace_deadline_is_still_valid(req_record) -> None:
    """境界: `at == grace_deadline` は「猶予内」。`>` であって `>=` ではない。"""
    assert gate.status_of(req_record, approved(GRACE_DL), None, GRACE_DL) == "approved"


def test_late_decision_wins_over_lease(req_record) -> None:
    """遅延決定は lease があっても timeout（承認そのものが無効なので実行不可）。"""
    assert gate.status_of(req_record, approved(GRACE_DL + 1), lease(), CREATED + 10) == "timeout"


def test_timeout_written_at_grace_deadline_does_not_self_trip(req_record) -> None:
    """§1.7: `at` に `now` ではなく `grace_deadline` を入れる理由の回帰。

    `now` を入れると `at > grace_deadline` に自分で引っかかる（結果は同じ
    `timeout` だが、判定経路が変わり `decided_by` の意味が壊れる）。
    """
    rec = timed_out(GRACE_DL)
    assert rec["at"] == req_record["grace_deadline"]
    assert gate.status_of(req_record, rec, None, GRACE_DL + 500) == "timeout"


def test_grace_and_claim_deadlines_match_the_spec() -> None:
    """親設計書 §4.3: grace = created+150, claim = created+180。"""
    assert gate.GRACE_SECONDS == 150
    assert gate.CLAIM_WINDOW_SECONDS == 180
    req = make_req(created_at=CREATED)
    assert req["grace_deadline"] - req["created_at"] == 150
    assert req["claim_deadline"] - req["created_at"] == 180


def test_status_is_not_stored_on_the_record(req_record) -> None:
    """親設計書 §4.3: `status` はレコードに持たない（純関数で導出する）。"""
    assert "status" not in req_record


# ===========================================================================
# §1.7: タイムアウトの一度きり記録
# ===========================================================================


def test_timeout_is_recorded_exactly_once_by_the_first_observer(fake_store, req_record) -> None:
    fake_store.put_if_absent("req:" + req_record["approval_id"], req_record)

    now = GRACE_DL + 1
    for _ in range(5):  # 5 コンテナが同時に観測した想定
        gate._observe_and_maybe_record_timeout(fake_store, req_record, None, now)

    audit_events = [rec["event"] for rec in fake_store.outbox.values()] + [
        obj["event"] for obj in fake_store.files.values()
    ]
    assert audit_events.count("timed_out") == 1, f"timed_out が {audit_events} 回記録された"


def test_timeout_write_once_uses_grace_deadline_as_at(fake_store, req_record) -> None:
    gate._observe_and_maybe_record_timeout(fake_store, req_record, None, GRACE_DL + 50)
    decision = fake_store.data["decision:" + req_record["approval_id"]]
    assert decision == {"decision": "timeout", "at": req_record["grace_deadline"], "by": "system"}


def test_no_timeout_record_while_still_pending(fake_store, req_record) -> None:
    gate._observe_and_maybe_record_timeout(fake_store, req_record, None, CREATED + 1)
    assert "decision:" + req_record["approval_id"] not in fake_store.data
    assert fake_store.files == {}


def test_no_timeout_record_when_a_decision_already_exists(fake_store, req_record) -> None:
    gate._observe_and_maybe_record_timeout(fake_store, req_record, approved(CREATED + 5), GRACE_DL + 50)
    assert "decision:" + req_record["approval_id"] not in fake_store.data


def test_timeout_observation_prunes_the_pending_index(fake_store, req_record) -> None:
    from modal_hub.core import store as store_mod

    fake_store.overwrite(store_mod.PREFIX_PENDING_INDEX, [req_record["approval_id"], "other"])
    gate._observe_and_maybe_record_timeout(fake_store, req_record, None, GRACE_DL + 1)
    assert fake_store.data[store_mod.PREFIX_PENDING_INDEX] == ["other"]


# ===========================================================================
# 検証項目の照合（§1.4 の表）
# ===========================================================================


def full_verification(req: dict) -> dict:
    return {
        "payload_sha256": req["payload_sha256"],
        "payload_raw_sha256": req["payload_raw_sha256"],
        "context": dict(req["context"]),
        "targets": [dict(t) for t in req["targets"]],
    }


def test_identical_verification_has_no_mismatches(req_record) -> None:
    assert gate._verification_mismatches(req_record, full_verification(req_record)) == []


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("payload_sha256", "0" * 64),
        ("payload_raw_sha256", "0" * 64),
    ],
)
def test_payload_hash_mismatch_detected(req_record, field, new_value) -> None:
    ver = full_verification(req_record)
    ver[field] = new_value
    assert gate._verification_mismatches(req_record, ver) == [field]


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("cwd", "C:/Users/Haruki/Projects/Other"),
        ("workspace_id", "9" * 64),
        ("base_revision", "1" * 40),
    ],
)
def test_context_mismatch_detected(req_record, field, new_value) -> None:
    """`Bash` で targets が空でも context の 3 項目は必ず照合する（§1.4）。"""
    ver = full_verification(req_record)
    ver["context"][field] = new_value
    assert gate._verification_mismatches(req_record, ver) == [f"context.{field}"]


def test_both_null_base_revision_is_a_match() -> None:
    """§4: `base_revision` が両方 null なら一致とみなす（Git 管理外を止めない）。"""
    req = make_req(base_revision=None)
    assert gate._verification_mismatches(req, full_verification(req)) == []


def test_null_vs_non_null_base_revision_is_a_mismatch() -> None:
    req = make_req(base_revision=None)
    ver = full_verification(req)
    ver["context"]["base_revision"] = "a" * 40
    assert gate._verification_mismatches(req, ver) == ["context.base_revision"]


def test_target_count_mismatch_detected() -> None:
    req = make_req(targets=[make_target()])
    ver = full_verification(req)
    ver["targets"] = []
    assert gate._verification_mismatches(req, ver) == ["targets"]


def test_target_order_matters() -> None:
    """§1.4: 件数**と順序**が一致すること。"""
    a = make_target(path="C:/p/a.txt")
    b = make_target(path="C:/p/b.txt")
    req = make_req(targets=[a, b])
    ver = full_verification(req)
    ver["targets"] = [dict(b), dict(a)]
    assert gate._verification_mismatches(req, ver) != []


@pytest.mark.parametrize("field", ["path", "realpath", "identity", "preimage_sha256", "exists"])
def test_each_target_field_is_compared(field) -> None:
    req = make_req(targets=[make_target()])
    ver = full_verification(req)
    ver["targets"][0][field] = False if field == "exists" else "CHANGED"
    assert gate._verification_mismatches(req, ver) == [f"targets[0].{field}"]


def test_symlink_swap_is_detected_by_realpath_and_identity() -> None:
    """§8.1 明示項目 / §9 落とし穴 24。

    `workspace/output.txt` への書き込みを承認した 150 秒の待機中に、別プロセスが
    同パスを機密ファイルへの symlink へ差し替えるシナリオ。`path` は変わらず、
    `payload_sha256` / `cwd` / `workspace_id` / `base_revision` もすべて一致する。
    検出できるのは **realpath + lstat 識別子 + 内容ハッシュ** だけである。
    """
    approved_target = make_target(
        path="C:/proj/workspace/output.txt",
        realpath="C:/proj/workspace/output.txt",
        identity="17735206716449772873:100",
        preimage_sha256="a" * 64,
    )
    req = make_req(targets=[approved_target])

    swapped = dict(
        approved_target,
        realpath="C:/Users/Haruki/.ssh/id_rsa",  # symlink を辿った先が変わった
        identity="17735206716449772873:999",  # 実体（st_dev:st_ino）が別物
        preimage_sha256="b" * 64,  # 上書き前の内容も別物
    )
    ver = full_verification(req)
    ver["targets"] = [swapped]

    mismatches = gate._verification_mismatches(req, ver)
    assert "targets[0].realpath" in mismatches
    assert "targets[0].identity" in mismatches
    assert "targets[0].preimage_sha256" in mismatches
    # 4 項目（payload_sha256/cwd/workspace_id/base_revision）は一致したまま。
    assert not any(m.startswith("context.") or m.startswith("payload") for m in mismatches)


def test_identity_is_compared_as_a_string_not_a_number() -> None:
    """§3: `identity` は必ず文字列。JS の 2^53 を超える st_dev が丸められると
    「異なるデバイスを同一と誤判定」しうる。"""
    big = "17735206716449772873:562949955562867"
    req = make_req(targets=[make_target(identity=big)])
    assert isinstance(req["targets"][0]["identity"], str)
    ver = full_verification(req)
    # 2^53 超で丸められた別値。文字列比較なら確実に不一致になる。
    ver["targets"][0]["identity"] = "17735206716449772000:562949955562867"
    assert gate._verification_mismatches(req, ver) == ["targets[0].identity"]


def test_multiple_mismatches_are_all_reported(req_record) -> None:
    """`message` に不一致項目名を含めるため、最初の 1 件で打ち切らない（§1.4）。"""
    ver = full_verification(req_record)
    ver["payload_sha256"] = "0" * 64
    ver["context"]["cwd"] = "C:/elsewhere"
    assert set(gate._verification_mismatches(req_record, ver)) == {"payload_sha256", "context.cwd"}


# ===========================================================================
# インデックス操作（write-once キーには絶対に使わない補助データ）
# ===========================================================================


def test_index_add_is_idempotent(fake_store) -> None:
    gate._index_add(fake_store, "pending:index", "a")
    gate._index_add(fake_store, "pending:index", "a")
    gate._index_add(fake_store, "pending:index", "b")
    assert fake_store.data["pending:index"] == ["a", "b"]


def test_index_remove(fake_store) -> None:
    fake_store.overwrite("pending:index", ["a", "b"])
    gate._index_remove(fake_store, "pending:index", "a")
    assert fake_store.data["pending:index"] == ["b"]


def test_corrupt_index_raises_rather_than_returning_empty(fake_store) -> None:
    """§9 落とし穴 15「黙って空を返す実装は原因を隠す」。"""
    fake_store.overwrite("pending:index", {"not": "a list"})
    with pytest.raises(gate.ApiError):
        gate._index_get(fake_store, "pending:index")


def test_overwrite_is_never_used_on_write_once_keys(fake_store, req_record) -> None:
    """`overwrite` を承認状態機械のキーへ使っていないことの回帰。

    `decision:` / `lease:` / `idem:` / `req:` は 1 回勝負であり、
    read-then-write の経路をこれらへ通した瞬間に安全性が崩れる。
    """
    from modal_hub.core import store as store_mod

    gate._observe_and_maybe_record_timeout(fake_store, req_record, None, GRACE_DL + 1)
    gate._index_add(fake_store, store_mod.PREFIX_PENDING_INDEX, "x")
    gate._index_add(fake_store, gate._gc_index_key(CREATED), "x")

    forbidden = ("req:", "decision:", "lease:", "idem:", "notify:")
    assert not any(k.startswith(forbidden) for k in fake_store.overwrite_calls), (
        f"write-once キーへ overwrite した: {fake_store.overwrite_calls}"
    )


def test_gc_index_key_is_a_utc_day_bucket() -> None:
    key = gate._gc_index_key(1_786_000_000.0)
    assert key.startswith("gc:index:")
    assert len(key.split("gc:index:")[1]) == len("2026-08-11")


# ===========================================================================
# サマリ / 表示文言
# ===========================================================================


def test_summary_is_truncated_to_200_chars() -> None:
    summary = gate._summarize_payload({"command": "x" * 500})
    assert len(summary) <= 200


def test_summary_redacts_secrets() -> None:
    summary = gate._summarize_payload({"command": "export GITHUB=ghp_" + "a" * 30})
    assert "ghp_" not in summary
    assert "<REDACTED:github>" in summary


def test_reason_lookup_uses_closed_vocabulary() -> None:
    """§1.2: 表示文言はサーバが `rule_id` から引く（自由文を受け取らない）。"""
    assert gate._reason_for_rule_id("force_push") == "履歴を破壊する"
    assert gate._reason_for_rule_id("unknown_tool") != ""
    assert gate._reason_for_rule_id("totally-unknown-rule-id") == ""


def test_secret_straddling_the_200_char_boundary_is_still_redacted() -> None:
    command = "x" * 190 + "ghp_" + "a" * 30
    summary = gate._summarize_payload({"command": command})
    assert "ghp_" not in summary
