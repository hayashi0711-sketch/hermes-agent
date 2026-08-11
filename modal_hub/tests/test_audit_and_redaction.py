"""監査ログ（`services/audit.py`）と redaction（`core/redact.py`）。

    - 親設計書 §8.1「audit: 監査書き込み失敗時に `claim` が成功しないこと」
    - Phase1a spec §10.1（決定的 event_id）・§10.1b（永続 outbox）・
      §10.2（レコード）・§10.3（redaction）・§10.4（失敗時の順序保証）
    - Phase1a spec §10.3 が名指しで要求する `test_redaction_coverage.py`
      相当の網羅チェックも本ファイルに含める。
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from modal_hub.core import redact
from modal_hub.services import audit

AT = 1_786_000_000.123

BASE = dict(
    approval_id="11111111-1111-4111-8111-111111111111",
    sub="claude_code:desktop-haruki",
    source="claude_code",
    session_id="opaque",
    workspace_id="a" * 64,
    tool_name="Bash",
    risk="HIGH",
    rule_id="force_push",
)


# ===========================================================================
# §10.1 決定的 event_id
# ===========================================================================


def test_event_id_matches_the_spec_formula() -> None:
    expected = hashlib.sha256(b"aid|claim_granted|att-1").hexdigest()[:16]
    assert audit.compute_event_id("aid", "claim_granted", "att-1") == expected
    assert len(expected) == 16


def test_event_id_is_deterministic_not_random() -> None:
    """§10.1: v1 の `rand8` は commit 成功＋応答喪失のリトライで
    **別ファイルが作られ同じ事象が重複記録される**。"""
    ids = {audit.compute_event_id("aid", "requested", "") for _ in range(50)}
    assert len(ids) == 1


@pytest.mark.parametrize(
    "a,b",
    [
        (("aid", "requested", ""), ("aid", "rejected", "")),
        (("aid", "claim_granted", "att-1"), ("aid", "claim_granted", "att-2")),
        (("aid-1", "consumed", "l"), ("aid-2", "consumed", "l")),
    ],
)
def test_event_id_separates_distinct_events(a, b) -> None:
    assert audit.compute_event_id(*a) != audit.compute_event_id(*b)


def test_audit_path_format() -> None:
    """§10.1: `audit/<YYYY-MM>/<approval_id>.<event>.<event_id>.json`。"""
    path = audit._audit_rel_path("aid", "requested", "cafebabecafebabe", AT)
    assert re.fullmatch(r"audit/\d{4}-\d{2}/aid\.requested\.cafebabecafebabe\.json", path), path


def test_audit_path_month_is_utc() -> None:
    """月ディレクトリはコンテナのローカル TZ に依らない。"""
    # 2026-01-01T00:30:00Z — JST なら 2026-01-01 09:30、UTC-8 なら 2025-12-31。
    path = audit._audit_rel_path("aid", "requested", "x", 1_767_227_400.0)
    assert "/2026-01/" in path


def test_no_sequence_number_is_allocated() -> None:
    """§10.1: seq 番号は採番しない（複数コンテナでの採番は競合する）。"""
    import inspect

    source = inspect.getsource(audit)
    assert "seq" not in source.replace("seq 番号は採番しない", "")


# ===========================================================================
# §10.2 レコード
# ===========================================================================


def test_record_has_the_spec_fields() -> None:
    record = audit.build_record(at=AT, event="claim_granted", payload={"command": "ls"}, detail=None, **BASE)
    assert set(record) == {
        "at",
        "event",
        "approval_id",
        "sub",
        "source",
        "session_id",
        "workspace_id",
        "tool_name",
        "risk",
        "rule_id",
        "payload_redacted",
        "detail",
    }


def test_record_stores_payload_redacted_never_raw() -> None:
    """`payload` というキー名そのものが存在しない（生ペイロードを残さない）。"""
    record = audit.build_record(
        at=AT, event="requested", payload={"command": "export A=sk-ant-" + "x" * 30}, detail=None, **BASE
    )
    assert "payload" not in record
    assert "sk-ant-" not in json.dumps(record, ensure_ascii=False)
    assert "<REDACTED:anthropic>" in record["payload_redacted"]["command"]


def test_record_redacts_the_detail_field() -> None:
    record = audit.build_record(
        at=AT, event="failed", payload=None, detail="exit 1: password=hunter2hunter2", **BASE
    )
    assert "hunter2hunter2" not in record["detail"]


def test_missing_payload_becomes_empty_dict_not_none() -> None:
    record = audit.build_record(at=AT, event="timed_out", payload=None, detail=None, **BASE)
    assert record["payload_redacted"] == {}
    assert record["detail"] is None


def test_record_is_json_serializable() -> None:
    record = audit.build_record(at=AT, event="requested", payload={"command": "ls"}, detail=None, **BASE)
    json.dumps(record, ensure_ascii=False)


# ===========================================================================
# §10.1b 永続 outbox
# ===========================================================================


def test_record_event_registers_outbox_then_writes_then_consumes(fake_store) -> None:
    audit.record_event(fake_store, at=AT, event="requested", discriminator="", payload={"command": "ls"}, **BASE)
    assert fake_store.outbox == {}, "flush 成功後に outbox が残っている"
    assert len(fake_store.files) == 1
    (path,) = fake_store.files
    assert path.endswith(".json") and path.startswith("audit/")


def test_volume_write_failure_leaves_the_entry_in_the_outbox(fake_store) -> None:
    """§10.1b: Volume 書き込みの失敗単体では 500 を返さない（後で回収される）。"""
    fake_store.fail_write_json = True
    audit.record_event(fake_store, at=AT, event="requested", discriminator="", **BASE)  # 例外にならない
    assert len(fake_store.outbox) == 1
    assert fake_store.files == {}


def test_outbox_registration_failure_propagates(fake_store) -> None:
    """§10.1b:「監査に失敗したら」＝「outbox への登録に失敗したら」。"""
    fake_store.fail_outbox_register = True
    with pytest.raises(RuntimeError, match="injected outbox failure"):
        audit.record_event(fake_store, at=AT, event="claim_granted", discriminator="att-1", **BASE)


def test_reflush_after_outage_produces_exactly_one_file(fake_store) -> None:
    """決定的 event_id なので、再書き出しが重複を生まない。"""
    fake_store.fail_write_json = True
    audit.record_event(fake_store, at=AT, event="claim_granted", discriminator="att-1", **BASE)
    fake_store.fail_write_json = False
    audit.record_event(fake_store, at=AT, event="claim_granted", discriminator="att-1", **BASE)
    assert len(fake_store.files) == 1
    assert fake_store.outbox == {}


def test_one_event_one_file(fake_store) -> None:
    """D-11 / §9 落とし穴 8: 共有 JSONL への追記ではなく 1 イベント 1 ファイル。"""
    for event, disc in (("requested", ""), ("approved", ""), ("claim_granted", "att-1"), ("consumed", "lease-1")):
        audit.record_event(fake_store, at=AT, event=event, discriminator=disc, **BASE)
    assert len(fake_store.files) == 4
    assert all(p.endswith(".json") for p in fake_store.files)
    assert not any(p.endswith(".jsonl") for p in fake_store.files)


def test_audit_module_does_not_touch_the_store_module_directly() -> None:
    """DI パターン: ストアは引数で注入される（import 時に Modal へ触らない）。"""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(audit))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "modal" not in modules
    assert not any(m.endswith("store") for m in modules), modules
    assert not hasattr(audit, "modal")


# ===========================================================================
# §10.3 Redaction
# ===========================================================================

SECRET_SAMPLES = {
    "anthropic": "sk-ant-api03-" + "A" * 40,
    "openai": "sk-proj-" + "B" * 40,
    "github": "ghp_" + "C" * 36,
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "slack": "xoxb-1234567890-abcdefghijkl",
    "google": "AIza" + "D" * 35,
    "bearer": "Bearer " + "E" * 40,
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQ==\n-----END RSA PRIVATE KEY-----",
    "conn_string": "postgres://user:secretpass@db.example.com/app",
    "generic": "api_key=abcdefghijklmnop",
}


@pytest.mark.parametrize("kind,sample", sorted(SECRET_SAMPLES.items()))
def test_every_pattern_actually_matches_a_real_sample(kind: str, sample: str) -> None:
    """§10.3 の 11 パターンがすべて生きていることを確認する。

    v1 では Markdown の表に正規表現を書いたせいでパイプのエスケープ `\\|` が
    **交替ではなくリテラルの縦棒**として実装される事故が起きた。
    """
    out = redact.redact_text(f"prefix {sample} suffix")
    assert f"<REDACTED:{kind}>" in out, f"{kind} パターンが一致しない"
    assert sample not in out


def test_pattern_list_matches_the_spec_vocabulary() -> None:
    kinds = [name for name, _ in redact.REDACTION_PATTERNS]
    assert kinds == [
        "anthropic",
        "openai",
        "github",
        "aws",
        "slack",
        "google",
        "bearer",
        "jwt",
        "private_key",
        "conn_string",
        "generic",
    ]


def test_no_pattern_contains_an_escaped_pipe() -> None:
    """Markdown 表由来の `\\|` 混入の回帰（05 冒頭「v1から破棄した設計」）。"""
    for name, pattern in redact.REDACTION_PATTERNS:
        assert "\\|" not in pattern, f"{name} にエスケープされたパイプが混入している"


def test_redact_value_walks_nested_structures() -> None:
    value = {
        "outer": {"inner": ["ghp_" + "a" * 36, {"deep": "AKIAIOSFODNN7EXAMPLE"}]},
        "n": 1,
        "b": True,
        "none": None,
    }
    out = redact.redact_value(value)
    blob = json.dumps(out, ensure_ascii=False)
    assert "ghp_" not in blob and "AKIAIOSFODNN7EXAMPLE" not in blob
    assert out["n"] == 1 and out["b"] is True and out["none"] is None


def test_redact_text_rejects_non_str() -> None:
    """黙って str() 変換しない（型不正は呼び出し側のバグ）。"""
    for value in (None, 1, {"a": 1}, [1]):
        with pytest.raises(TypeError):
            redact.redact_text(value)  # type: ignore[arg-type]


def test_redaction_is_shared_by_audit_and_the_pwa_surface() -> None:
    """§10.3: 「実装は `modal_hub/core/redact.py` の 1 か所」。"""
    import inspect

    from modal_hub.routers import approval_gate

    assert "from modal_hub.core import redact" in inspect.getsource(approval_gate)
    assert "from modal_hub.core import redact" in inspect.getsource(audit)
    assert redact.redact_text.__module__ == "modal_hub.core.redact"


def test_no_module_defines_its_own_redaction_patterns() -> None:
    """redaction を 2 か所に書くと片方だけが更新され、必ず乖離する。"""
    import inspect

    from modal_hub.routers import approval_gate
    from modal_hub.services import notifier

    for module in (approval_gate, audit, notifier):
        source = inspect.getsource(module)
        assert "REDACTION_PATTERNS" not in source.replace("redact.REDACTION_PATTERNS", "")
        assert "sk-ant-" not in source


# ---------------------------------------------------------------------------
# §10.3「新しいフィールドを追加したら redaction 対象に含めること」の網羅チェック
# ---------------------------------------------------------------------------


def test_every_free_text_field_in_an_audit_record_is_redacted(fake_store) -> None:
    """監査レコードの全 str フィールドに秘密が生で残らないことを確認する。

    フィールドを増やしたときに redaction を通し忘れたら落ちる。
    """
    poison = "ghp_" + "z" * 36
    audit.record_event(
        fake_store,
        at=AT,
        event="failed",
        discriminator="lease-1",
        payload={"command": poison, "nested": {"arg": poison}, "args": [poison]},
        detail=poison,
        **{**BASE, "rule_id": "force_push"},
    )
    (record,) = fake_store.files.values()

    def walk(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for v in value.values():
                yield from walk(v)
        elif isinstance(value, list):
            for v in value:
                yield from walk(v)

    leaked = [s for s in walk(record) if poison in s]
    assert leaked == [], f"redaction を通していない自由文フィールドがある: {leaked}"


def test_pwa_facing_fields_are_redacted(secret_env, monkeypatch) -> None:
    """§10.3: redaction は監査だけでなく PWA へ返す `summary`/`detail` にも適用する。"""
    from modal_hub.routers import approval_gate as gate

    poison = "sk-ant-api03-" + "y" * 40
    assert poison not in gate._summarize_payload({"command": poison})
    assert poison not in json.dumps(redact.redact_value({"command": poison}), ensure_ascii=False)


def test_summary_redacts_a_secret_straddling_the_200_char_truncation_boundary() -> None:
    """BUG-5 回帰: `_summarize_payload` は以前 `redact_text(text[:200])`

    （**切り詰めてから** redaction）だった。秘密のパターンが 200 文字境界を
    またぐと正規表現が分断されて一致しなくなり、切り詰め後の文字列に
    秘密の生の先頭部分がそのまま残って PWA へ表示されていた。

    修正後は `redact_text(text)[:200]`（**先に redact してから**
    truncate）であるべき。ここでは秘密が境界をまたぐ配置（190 文字の埋め
    草の直後に github トークンパターン）で確認する。
    """
    from modal_hub.routers import approval_gate as gate

    command = "x" * 190 + "ghp_" + "a" * 30
    summary = gate._summarize_payload({"command": command})
    assert "ghp_" not in summary
    assert "a" * 30 not in summary
    assert len(summary) <= 200


# ---------------------------------------------------------------------------
# redaction は「最後の砦」ではない（§10.3 末尾）
# ---------------------------------------------------------------------------


def test_redaction_is_documented_as_a_secondary_measure_not_the_guarantee() -> None:
    """秘密が外部へ漏れないことの担保は、ntfy にペイロードを載せない構造。

    その構造が守られていることは test_notifier.py が検証する。ここでは
    「redaction が取りこぼす例が実在する」ことを明示して、redaction を
    保証と読み違えないようにする。
    """
    # 未知の形式のトークンは検出できない（設計上の既知の限界）。
    unknown_format = "hha1.eyBub3RhaGVhZGVyIH0.c2lnbmF0dXJl"
    assert redact.redact_text(unknown_format) == unknown_format
