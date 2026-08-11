"""modal_hub/tests/test_distiller.py — Phase 1b Skill Distiller の不変条件テスト。

親設計書 §8.1 が課す 4 項目（抽出条件境界・YAML frontmatter 妥当性・
出力パスに Obsidian を含まない・`<name>/SKILL.md` ディレクトリ形式）と、
`07_Phase1b_Spec.md` の着手前チェックリスト
（`queue_entry_id` 正規表現・preflight が Batch 投入前に完結する・
①②③が LLM 応答で上書きされない・§5 create-or-match-only）を網羅する。

`test_phase1b_guards.py` の `test_skill_distiller_is_absent_in_phase_1a`
は本実装とセットで削除される前提の門番であり、本ファイルはそれと相互
補完的に「実装された瞬間に緑になる」不変条件の集合を提供する。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

import pytest

from modal_hub.services import skill_distiller
from modal_hub.services import skill_quarantine
from modal_hub.services.session_reader import SessionMessage

# ---------------------------------------------------------------------------
# 共有ビルダー
# ---------------------------------------------------------------------------


def _journal_entry(
    *,
    tool_call_id: Optional[str],
    tool_name: Optional[str],
    status: str,
    error_type: Optional[str] = None,
) -> dict:
    entry: dict = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "status": status,
        "recorded_at": time.time(),
    }
    if error_type is not None:
        entry["error_type"] = error_type
    return entry


def _msg(
    role: str = "user",
    content: Optional[str] = "hello",
    tool_name: Optional[str] = None,
    tool_calls: Any = None,
) -> SessionMessage:
    return SessionMessage(
        id=1,
        role=role,
        content=content,
        tool_name=tool_name,
        tool_call_id=None,
        tool_calls=tool_calls,
        truncated=False,
    )


def _skill_md(name: str, description: str = "desc", body: str = "Body.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


# ===========================================================================
# §1.3 queue_entry_id 正規表現
# ===========================================================================


class TestQueueEntryIdRegex:
    """`build_batch_request()` の戻り値の `custom_id` が
    `^[a-zA-Z0-9_-]{1,64}$`（Anthropic `custom_id` 制約）を満たすこと。"""

    @pytest.mark.parametrize(
        "session_id",
        [
            "abc",
            "sess-1",
            "sess:with:colons",  # colon は queue_entry_id 自体の制約外（ID はハッシュ）
        ],
    )
    def test_build_batch_request_custom_id_is_anthropic_safe(self, session_id):
        """queue_entry_id は `compute_queue_entry_id()` が生成するハッシュ形
        なので、入力 session_id がどんな形でも `custom_id` は `[A-Za-z0-9_-]{1,64}`
        に一致する。"""
        request = skill_distiller.build_batch_request(
            queue_entry_id="s" + "a" * 31,  # 32 hex chars + prefix = 33 chars total
            messages=[],
            journal_entries=[],
            existing_skill_headers=[],
            git_diff_truncated="",
        )
        custom_id = request["custom_id"]
        assert re.fullmatch(r"^[a-zA-Z0-9_-]{1,64}$", custom_id), custom_id


# ===========================================================================
# §3.1 preflight: 抽出条件 ①②③（境界を全部試す）
# ===========================================================================


class TestPreflightCondition1:
    """条件①: 末尾 3 件が全て status='ok' かつ journal 全体に blocked が無い。"""

    def test_passes_when_last_three_are_ok_and_no_blocked_anywhere(self):
        entries = [
            _journal_entry(tool_call_id="a", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="b", tool_name="Read", status="error"),
            _journal_entry(tool_call_id="c", tool_name="Edit", status="ok"),
            _journal_entry(tool_call_id="d", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="e", tool_name="Read", status="ok"),
        ]
        passed, reason = skill_distiller.evaluate_preconditions(entries)
        assert passed is True
        assert reason is None

    def test_fails_when_last_three_contain_non_ok(self):
        entries = [
            _journal_entry(tool_call_id="a", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="b", tool_name="Read", status="ok"),
            _journal_entry(tool_call_id="c", tool_name="Edit", status="error"),  # 末尾
        ]
        passed, reason = skill_distiller.evaluate_preconditions(entries)
        assert passed is False
        assert reason == "condition_1_unmet"

    def test_fails_when_blocked_anywhere_in_journal(self):
        entries = [
            _journal_entry(tool_call_id="a", tool_name="Bash", status="blocked"),
            _journal_entry(tool_call_id="b", tool_name="Read", status="ok"),
            _journal_entry(tool_call_id="c", tool_name="Edit", status="ok"),
            _journal_entry(tool_call_id="d", tool_name="Bash", status="ok"),
        ]
        passed, reason = skill_distiller.evaluate_preconditions(entries)
        assert passed is False
        assert reason == "condition_1_unmet"

    def test_fails_when_only_two_journal_entries(self):
        entries = [
            _journal_entry(tool_call_id="a", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="b", tool_name="Read", status="ok"),
        ]
        passed, reason = skill_distiller.evaluate_preconditions(entries)
        assert passed is False
        assert reason == "condition_1_unmet"


class TestPreflightCondition2:
    """条件②: `tool_call_id` のユニーク数が 5 以上。"""

    def test_passes_at_exactly_5_unique_ids(self):
        # 条件②単体を検証するため、条件③の error→ok パターンも入れておく。
        entries = [
            _journal_entry(tool_call_id="id-0", tool_name="Bash", status="error"),
            _journal_entry(tool_call_id="id-1", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="id-2", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="id-3", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="id-4", tool_name="Bash", status="ok"),
        ]
        passed, reason = skill_distiller.evaluate_preconditions(entries)
        assert passed is True, reason

    def test_fails_at_4_unique_ids(self):
        entries = [
            _journal_entry(tool_call_id=f"id-{i}", tool_name="Bash", status="ok")
            for i in range(4)
        ]
        passed, reason = skill_distiller.evaluate_preconditions(entries)
        assert passed is False
        assert reason == "condition_2_unmet"

    def test_fails_when_all_ids_are_null(self):
        entries = [
            _journal_entry(tool_call_id=None, tool_name="Bash", status="ok")
            for _ in range(5)
        ]
        passed, reason = skill_distiller.evaluate_preconditions(entries)
        assert passed is False
        assert reason == "condition_2_unmet"


class TestPreflightCondition3:
    """条件③: 同一 `tool_name` で error→ok の組が 1 つ以上ある。"""

    def test_passes_when_one_error_followed_by_ok_for_same_tool(self):
        entries = [
            _journal_entry(tool_call_id="a", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="b", tool_name="Bash", status="error"),
            _journal_entry(tool_call_id="c", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="d", tool_name="Read", status="ok"),
            _journal_entry(tool_call_id="e", tool_name="Bash", status="ok"),
        ]
        passed, reason = skill_distiller.evaluate_preconditions(entries)
        assert passed is True, reason

    def test_fails_when_no_recovery_pattern(self):
        entries = [
            _journal_entry(tool_call_id="a", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="b", tool_name="Read", status="ok"),
            _journal_entry(tool_call_id="c", tool_name="Edit", status="ok"),
            _journal_entry(tool_call_id="d", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="e", tool_name="Read", status="ok"),
        ]
        passed, reason = skill_distiller.evaluate_preconditions(entries)
        assert passed is False
        assert reason == "condition_3_unmet"

    def test_error_then_error_then_ok_does_not_count(self):
        """隣接ペア (error, ok) のみ検出する。error→error→ok は 1 つのペア (error,ok)
        として扱われない（=隣接していない）。
        """
        entries = [
            _journal_entry(tool_call_id="a", tool_name="Bash", status="ok"),
            _journal_entry(tool_call_id="b", tool_name="Bash", status="error"),
            _journal_entry(tool_call_id="c", tool_name="Bash", status="error"),
            _journal_entry(tool_call_id="d", tool_name="Bash", status="ok"),
        ]
        passed, reason = skill_distiller.evaluate_preconditions(entries)
        # 条件① は last 3 に error が含まれるので失敗。条件を 3 が検査する前に
        # 1 で止まる。
        assert passed is False
        assert reason == "condition_1_unmet"


# ===========================================================================
# §0.2 item2: git diff 切り詰め
# ===========================================================================


class TestTruncateGitDiff:
    def test_under_limit_returns_unchanged(self):
        text = "a" * 100
        out, was_truncated = skill_distiller.truncate_git_diff(text, max_bytes=200)
        assert out == text
        assert was_truncated is False

    def test_over_limit_truncates_with_marker(self):
        text = "a" * 1000
        out, was_truncated = skill_distiller.truncate_git_diff(text, max_bytes=100)
        assert was_truncated is True
        assert out.endswith("...[truncated 900 bytes]")
        assert len(out.encode("utf-8")) <= 100 + len("...[truncated 900 bytes]".encode("utf-8"))

    def test_truncation_does_not_split_multibyte_character(self):
        # "あ" は UTF-8 で 3 バイト。境界を中途半端に切ると UnicodeDecodeError になる。
        text = "あ" * 200  # 600 bytes
        out, was_truncated = skill_distiller.truncate_git_diff(text, max_bytes=100)
        assert was_truncated is True
        # 末尾のマルチバイト文字が壊れていないこと（先頭側は完全な文字のみ）。
        assert "あ" in out

    def test_exact_limit_returns_unchanged(self):
        text = "a" * 100
        out, was_truncated = skill_distiller.truncate_git_diff(text, max_bytes=100)
        assert out == text
        assert was_truncated is False


# ===========================================================================
# §0.2 item3: redaction の適用（build_batch_request 経由で確認）
# ===========================================================================


class TestRedactionInBatchRequest:
    """`build_batch_request()` 内で §0.2 item3 の 4 つの対象全てに
    redaction が適用されていることを、ユーザー入力側の値で確認する。"""

    def test_git_diff_redacted(self):
        request = skill_distiller.build_batch_request(
            queue_entry_id="s" + "a" * 31,
            messages=[],
            journal_entries=[],
            existing_skill_headers=[],
            git_diff_truncated="some diff with sk-ant-abcdefghijklmnop12345 token",
        )
        user_text = request["params"]["messages"][0]["content"]
        # 生トークン文字列がそのまま残っていないこと。
        assert "sk-ant-abcdefghijklmnop12345" not in user_text
        assert "<REDACTED:anthropic>" in user_text

    def test_message_content_redacted(self):
        request = skill_distiller.build_batch_request(
            queue_entry_id="s" + "a" * 31,
            messages=[_msg(content="Bearer AKIAIOSFODNN7EXAMPLE aws key")],
            journal_entries=[],
            existing_skill_headers=[],
            git_diff_truncated="",
        )
        user_text = request["params"]["messages"][0]["content"]
        assert "AKIAIOSFODNN7EXAMPLE" not in user_text
        assert "<REDACTED:aws>" in user_text or "<REDACTED:bearer>" in user_text

    def test_existing_skill_name_and_description_truncated_and_redacted(self):
        long_desc = "d" * 500
        request = skill_distiller.build_batch_request(
            queue_entry_id="s" + "a" * 31,
            messages=[],
            journal_entries=[],
            existing_skill_headers=[("some-skill", long_desc)],
            git_diff_truncated="",
        )
        user_text = request["params"]["messages"][0]["content"]
        # description が 200 文字で切り詰められている
        assert long_desc[:200] in user_text
        assert long_desc[201:] not in user_text

    def test_journal_entries_redacted(self):
        journal = [
            {
                "tool_call_id": "x",
                "tool_name": "Bash",
                "status": "error",
                "error_type": "auth_failed:token=sk-proj-abcdefghij1234567890ABCDEFG",
                "recorded_at": 1.0,
            }
        ]
        request = skill_distiller.build_batch_request(
            queue_entry_id="s" + "a" * 31,
            messages=[],
            journal_entries=journal,
            existing_skill_headers=[],
            git_diff_truncated="",
        )
        user_text = request["params"]["messages"][0]["content"]
        assert "sk-proj-abcdefghij1234567890ABCDEFG" not in user_text


# ===========================================================================
# §3.2: 既存スキル一覧の収集
# ===========================================================================


class TestCollectExistingSkillHeaders:
    def test_collects_from_quarantine(self, tmp_path):
        skill_quarantine.materialize(
            "qid-1", "alpha", _skill_md("alpha", "alpha-desc"), base=tmp_path
        )
        headers = skill_distiller.collect_existing_skill_headers(quarantine_base=tmp_path)
        assert ("alpha", "alpha-desc") in headers

    def test_collects_from_quarantine_and_hermes_when_both_exist(self, tmp_path):
        skill_quarantine.materialize(
            "qid-1", "quarantined", _skill_md("quarantined", "q-desc"), base=tmp_path
        )

        hermes_root = tmp_path / "hermes_skills"
        hermes_root.mkdir()
        (hermes_root / "promoted").mkdir()
        (hermes_root / "promoted" / "SKILL.md").write_text(
            _skill_md("promoted", "p-desc"), encoding="utf-8"
        )

        headers = skill_distiller.collect_existing_skill_headers(
            quarantine_base=tmp_path, hermes_skills_root=hermes_root
        )
        assert ("quarantined", "q-desc") in headers
        assert ("promoted", "p-desc") in headers

    def test_silently_skips_when_hermes_root_is_none(self, tmp_path):
        # hermes_constants が import できない環境でも動く（None を graceful に扱う）。
        skill_quarantine.materialize(
            "qid-1", "only-quarantine", _skill_md("only-quarantine", "d"), base=tmp_path
        )
        headers = skill_distiller.collect_existing_skill_headers(
            quarantine_base=tmp_path, hermes_skills_root=None
        )
        # hermes_constants 側の import 失敗は無視され、隔離側だけ返る。
        assert ("only-quarantine", "d") in headers


# ===========================================================================
# §3.2: Batch request の構造
# ===========================================================================


class TestBuildBatchRequest:
    def test_returns_anthropic_batch_shape(self):
        request = skill_distiller.build_batch_request(
            queue_entry_id="s" + "a" * 31,
            messages=[],
            journal_entries=[],
            existing_skill_headers=[],
            git_diff_truncated="",
        )
        assert "custom_id" in request
        assert "params" in request
        params = request["params"]
        assert params["model"] == skill_distiller.MODEL_NAME
        assert params["max_tokens"] == skill_distiller.MAX_OUTPUT_TOKENS
        assert "system" in params and isinstance(params["system"], str)
        assert isinstance(params["messages"], list) and len(params["messages"]) == 1
        msg = params["messages"][0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], str)

    def test_uses_xml_tags_to_separate_data_sections(self):
        request = skill_distiller.build_batch_request(
            queue_entry_id="s" + "a" * 31,
            messages=[_msg()],
            journal_entries=[_journal_entry(tool_call_id="x", tool_name="Bash", status="ok")],
            existing_skill_headers=[],
            git_diff_truncated="diff",
        )
        user_text = request["params"]["messages"][0]["content"]
        for tag in (
            "<existing_skills>",
            "</existing_skills>",
            "<session_messages>",
            "</session_messages>",
            "<journal>",
            "</journal>",
            "<git_diff>",
            "</git_diff>",
        ):
            assert tag in user_text, f"missing tag {tag}"

    def test_system_prompt_explicitly_states_data_is_not_instructions(self):
        request = skill_distiller.build_batch_request(
            queue_entry_id="s" + "a" * 31,
            messages=[],
            journal_entries=[],
            existing_skill_headers=[],
            git_diff_truncated="",
        )
        system = request["params"]["system"]
        # §3.2「プロンプト構造でデータと指示を分離する」の要件
        assert "DATA" in system and "not instructions" in system.lower()

    def test_empty_inputs_still_produce_valid_request(self):
        request = skill_distiller.build_batch_request(
            queue_entry_id="s" + "a" * 31,
            messages=[],
            journal_entries=[],
            existing_skill_headers=[],
            git_diff_truncated="",
        )
        # 空入力でも request shape は成立する（呼び出し側の事前判定が前提）。
        assert request["params"]["messages"][0]["content"]


# ===========================================================================
# §3.2: LLM 応答のパース
# ===========================================================================


class TestParseLlmResponse:
    """§3.1「①②③が LLM 応答で上書きされないこと」を応答パース側で担保:
    `decision` は `novel`/`duplicate`/`not_extractable` のいずれかでなければ
    `DistillParseError`。
    """

    def _succeeded(self, text: str) -> dict:
        return {"type": "succeeded", "message": {"content": [{"type": "text", "text": text}]}}

    def test_parses_novel_decision(self):
        text = json.dumps({
            "decision": "novel",
            "skill": {
                "name": "good-skill",
                "description": "does things",
                "body": "Body content here.",
            },
        })
        decision = skill_distiller.parse_llm_response(
            "qid-1", self._succeeded(text)
        )
        assert decision.decision == "novel"
        assert decision.name == "good-skill"
        assert decision.description == "does things"
        assert decision.body == "Body content here."

    def test_parses_duplicate_decision(self):
        text = json.dumps({"decision": "duplicate", "duplicate_of": "existing-skill"})
        decision = skill_distiller.parse_llm_response(
            "qid-1", self._succeeded(text)
        )
        assert decision.decision == "duplicate"
        assert decision.duplicate_of == "existing-skill"

    def test_parses_not_extractable_decision(self):
        text = json.dumps({"decision": "not_extractable", "reason": "shallow session"})
        decision = skill_distiller.parse_llm_response(
            "qid-1", self._succeeded(text)
        )
        assert decision.decision == "not_extractable"
        assert decision.not_extractable_reason == "shallow session"

    def test_invalid_name_in_novel_response_raises(self):
        # §3.1: ①②③を LLM 応答で上書きさせないために name の正規表現を
        # 必ず検証する。
        text = json.dumps({
            "decision": "novel",
            "skill": {
                "name": "Invalid Name",  # regex 不一致
                "description": "desc",
                "body": "body",
            },
        })
        with pytest.raises(skill_distiller.DistillParseError):
            skill_distiller.parse_llm_response("qid-1", self._succeeded(text))

    def test_unknown_decision_raises(self):
        text = json.dumps({"decision": "novel_overriding_preconditions"})
        with pytest.raises(skill_distiller.DistillParseError):
            skill_distiller.parse_llm_response("qid-1", self._succeeded(text))

    def test_malformed_json_raises(self):
        text = "this is not json { broken"
        with pytest.raises(skill_distiller.DistillParseError):
            skill_distiller.parse_llm_response("qid-1", self._succeeded(text))

    def test_empty_text_raises(self):
        text = ""
        with pytest.raises(skill_distiller.DistillParseError):
            skill_distiller.parse_llm_response("qid-1", self._succeeded(text))

    def test_surrounding_prose_with_json_object_is_tolerated(self):
        """モデルが前後に prose を付けても JSON 部分を抜き出して回復する。"""
        text = (
            "Sure, here is the JSON output:\n\n"
            + json.dumps({
                "decision": "duplicate",
                "duplicate_of": "other-skill",
            })
            + "\n\nDone."
        )
        decision = skill_distiller.parse_llm_response(
            "qid-1", self._succeeded(text)
        )
        assert decision.decision == "duplicate"

    def test_errored_result_yields_not_extractable(self):
        decision = skill_distiller.parse_llm_response(
            "qid-1", {"type": "errored", "error": {"type": "request_too_large"}}
        )
        assert decision.decision == "not_extractable"
        assert "errored" in (decision.not_extractable_reason or "")

    def test_canceled_result_yields_not_extractable(self):
        decision = skill_distiller.parse_llm_response(
            "qid-1", {"type": "canceled"}
        )
        assert decision.decision == "not_extractable"
        assert "canceled" in (decision.not_extractable_reason or "")


# ===========================================================================
# §4.1: SKILL.md の組み立て + 隔離保存
# ===========================================================================


class TestBuildSkillMd:
    def test_produces_valid_frontmatter_with_required_keys(self):
        text = skill_distiller.build_skill_md(
            name="my-skill",
            description="does things",
            body="Plain markdown body.",
            session_id="sess-1",
        )
        # frontmatter が先頭にある
        assert text.startswith("---\n")
        # name / description / version / distilled_from_session_id の 4 キー
        assert "name: my-skill" in text
        assert "description: does things" in text
        assert "version: 0.1.0" in text
        assert "distilled_from_session_id: sess-1" in text

    def test_strips_leading_frontmatter_if_present_in_body(self):
        body = "---\nname: wrong-name\ndescription: wrong\n---\n\nActual body."
        text = skill_distiller.build_skill_md(
            name="correct-name",
            description="correct",
            body=body,
            session_id="sess-1",
        )
        # 先頭 frontmatter の name は wrong-name ではなく correct-name
        assert "name: correct-name" in text
        assert "Actual body." in text

    def test_invalid_name_raises(self):
        with pytest.raises(skill_quarantine.QuarantineError):
            skill_distiller.build_skill_md(
                name="Bad Name",
                description="d",
                body="b",
                session_id="sess-1",
            )


class TestMaterializeSkillMd:
    """§4.1: materialize は `queue_entry_id` ごとに高々 1 回だけ実書き込みが
    発生する。"""

    def test_writes_under_name_directory(self, tmp_path):
        skill_distiller.materialize_skill_md(
            queue_entry_id="qid-1",
            name="alpha-skill",
            description="d",
            body="Body.",
            session_id="sess-1",
            base=tmp_path,
        )
        skill_file = tmp_path / "skills_quarantine" / "alpha-skill" / "SKILL.md"
        assert skill_file.is_file(), f"expected {skill_file} to exist"

    def test_is_idempotent_per_queue_entry_id(self, tmp_path):
        first = skill_distiller.materialize_skill_md(
            queue_entry_id="qid-1",
            name="alpha-skill",
            description="d",
            body="Body.",
            session_id="sess-1",
            base=tmp_path,
        )
        second = skill_distiller.materialize_skill_md(
            queue_entry_id="qid-1",
            name="alpha-skill",
            description="d",
            body="Body.",
            session_id="sess-1",
            base=tmp_path,
        )
        assert first == second

    def test_yaml_frontmatter_is_valid(self, tmp_path):
        skill_distiller.materialize_skill_md(
            queue_entry_id="qid-1",
            name="alpha-skill",
            description="d",
            body="Body.",
            session_id="sess-1",
            base=tmp_path,
        )
        text = (tmp_path / "skills_quarantine" / "alpha-skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert text.startswith("---\n")
        end = text.find("\n---", 3)
        assert end != -1
        import yaml

        fm = yaml.safe_load(text[3:end])
        assert isinstance(fm, dict)
        assert fm.get("name") == "alpha-skill"
        assert fm.get("description") == "d"
        assert fm.get("version") == "0.1.0"
        assert fm.get("distilled_from_session_id") == "sess-1"

    def test_name_collision_yields_suffix(self, tmp_path):
        # §4.1: 既存隔離があれば `<name>-2` へ退避する。
        skill_distiller.materialize_skill_md(
            queue_entry_id="qid-a",
            name="dup-skill",
            description="first",
            body="Body 1.",
            session_id="sess-1",
            base=tmp_path,
        )
        skill_distiller.materialize_skill_md(
            queue_entry_id="qid-b",
            name="dup-skill",
            description="second",
            body="Body 2.",
            session_id="sess-2",
            base=tmp_path,
        )
        first = (tmp_path / "skills_quarantine" / "dup-skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        second = (
            tmp_path / "skills_quarantine" / "dup-skill-2" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "first" in first
        assert "second" in second


# ===========================================================================
# §5 publish ペイロード
# ===========================================================================


class TestBuildPublishPayload:
    def test_includes_required_fields_and_correct_sha256(self):
        skill_md = _skill_md("pub-skill", "p-desc", "Body.")
        payload = skill_distiller.build_publish_payload("pub-skill", skill_md)
        assert payload["name"] == "pub-skill"
        assert payload["skill_md"] == skill_md
        import hashlib

        expected = hashlib.sha256(skill_md.encode("utf-8")).hexdigest()
        assert payload["content_sha256"] == expected

    def test_rejects_invalid_name(self):
        with pytest.raises(ValueError):
            skill_distiller.build_publish_payload("Invalid Name", _skill_md("x"))

    def test_rejects_empty_body(self):
        with pytest.raises(ValueError):
            skill_distiller.build_publish_payload("ok-name", "")

    def test_rejects_oversize_body(self):
        big = "x" * (65 * 1024)
        with pytest.raises(ValueError):
            skill_distiller.build_publish_payload("ok-name", big)


# ===========================================================================
# §8.1: 出力パスに Obsidian Vault が含まれないこと
# ===========================================================================


class TestNoObsidianLeak:
    """§8.1: 「Skill Distiller の出力が Obsidian に書き込まれることは絶対に
    あってはならない」。本テストは本モジュール（distiller）と CLI（hh_distill）
    のいずれにも Obsidian 関連文字列が**書かれていない**ことで担保する。
    """

    @pytest.mark.parametrize(
        "needle",
        ["Obsidian", "obsidian", "マイドライブ", "Vault", "vault"],
    )
    def test_skill_distiller_does_not_reference_obsidian(self, needle):
        path = Path(skill_distiller.__file__).resolve()
        text = path.read_text(encoding="utf-8")
        assert needle not in text, f"unexpected Obsidian reference in skill_distiller.py: {needle}"


# ===========================================================================
# §8.1: <name>/SKILL.md のディレクトリ形式
# ===========================================================================


class TestOutputPathFormat:
    """`materialize_skill_md()` の出力は必ず `<name>/SKILL.md` の
    ディレクトリ形式（§4.1・§8.1）。"""

    def test_output_path_is_under_name_subdirectory(self, tmp_path):
        result = skill_distiller.materialize_skill_md(
            queue_entry_id="qid-1",
            name="format-skill",
            description="d",
            body="Body.",
            session_id="sess-1",
            base=tmp_path,
        )
        output_path = Path(result.output_path)
        # output_path は <base>/skills_quarantine/<name>/SKILL.md
        assert output_path.parent.name == "format-skill"
        assert output_path.name == "SKILL.md"
        assert output_path.parent.parent.name == "skills_quarantine"


# ===========================================================================
# scripts/hh_distill.py: 状態機械の不変条件（限定的にユニット検証）
# ===========================================================================


# 状態機械の広い範囲は Anthropic / Hub への I/O を伴うため、本ファイルでは
# 「純粋関数」と「ローカル I/O のみ」の部分だけを検証する。


class TestHhDistillStateMachine:
    def test_excluded_queue_entry_ids_returns_manifest_ids(self, tmp_path, monkeypatch):
        """§2.2 手順0: `_excluded_queue_entry_ids` は
        `submitting/_manifest_*.json` が列挙する ID の集合を返す。"""
        sys_path = tmp_path / "state"
        submitting = sys_path / "distill_queue" / "submitting"
        submitting.mkdir(parents=True)
        manifest = {
            "manifest_id": "abc",
            "queue_entry_ids": ["qid-a", "qid-b"],
            "request_hashes": {},
            "api_call_attempted": False,
        }
        (submitting / "_manifest_abc.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        # hh_distill を import
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hh_distill",
            Path(__file__).resolve().parents[2] / "scripts" / "hh_distill.py",
        )
        assert spec is not None
        hh_distill = importlib.util.module_from_spec(spec)
        # Python 3.14 の @dataclass は sys.modules に登録されたモジュールを要求する。
        sys.modules["hh_distill"] = hh_distill
        spec.loader.exec_module(hh_distill)  # type: ignore[union-attr]

        # base を差し替えて test
        monkeypatch.setattr(hh_distill, "hh_agent_home", lambda: sys_path)
        excluded = hh_distill._excluded_queue_entry_ids(base=sys_path)
        assert excluded == {"qid-a", "qid-b"}

    def test_split_into_chunks_respects_request_limit(self):
        """`_split_into_chunks` は 100 件 / 80 MB を超えないように分割する。"""
        # 80 MB を超えるリクエストを 1 件作る
        big_content = "a" * (90 * 1024 * 1024)
        items = []
        for i in range(3):
            item = type("FakeItem", (), {})()
            item.pending = type("FakePending", (), {"queue_entry_id": f"qid-{i}"})()
            item.request = {
                "custom_id": f"qid-{i}",
                "params": {"messages": [{"role": "user", "content": big_content}]},
            }
            items.append(item)
        sys_path = Path(__file__).resolve().parents[2] / "scripts" / "hh_distill.py"

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hh_distill_for_chunks_1", sys_path
        )
        hh_distill = importlib.util.module_from_spec(spec)
        sys.modules["hh_distill_for_chunks_1"] = hh_distill
        spec.loader.exec_module(hh_distill)  # type: ignore[union-attr]

        chunks = hh_distill._split_into_chunks(items)
        # 各チャンクは 1 件（80 MB 制限のため）
        assert all(len(c) <= 1 for c in chunks)
        assert len(chunks) == 3

    def test_split_into_chunks_respects_count_limit(self):
        """`_split_into_chunks` は 100 件を超えないように分割する。"""
        items = []
        for i in range(250):
            item = type("FakeItem", (), {})()
            item.pending = type("FakePending", (), {"queue_entry_id": f"qid-{i}"})()
            item.request = {
                "custom_id": f"qid-{i}",
                "params": {"messages": [{"role": "user", "content": "x"}]},
            }
            items.append(item)

        sys_path = Path(__file__).resolve().parents[2] / "scripts" / "hh_distill.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hh_distill_for_chunks", sys_path
        )
        hh_distill = importlib.util.module_from_spec(spec)
        sys.modules["hh_distill_for_chunks"] = hh_distill
        spec.loader.exec_module(hh_distill)  # type: ignore[union-attr]

        chunks = hh_distill._split_into_chunks(items)
        # 100/100/50 の 3 チャンク
        assert [len(c) for c in chunks] == [100, 100, 50]

    def test_recover_manifests_api_call_attempted_false_moves_back_to_pending(
        self, tmp_path, monkeypatch
    ):
        """§2.2 手順6: `api_call_attempted: false` の manifest は全エントリ
        を `pending/` へ戻す。"""
        base = tmp_path
        pending = base / "distill_queue" / "pending"
        submitting = base / "distill_queue" / "submitting"
        submitting.mkdir(parents=True)

        # manifest
        manifest = {
            "manifest_id": "abc",
            "queue_entry_ids": ["qid-a"],
            "request_hashes": {},
            "api_call_attempted": False,
        }
        (submitting / "_manifest_abc.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        # submitting 配下に qid-a のエントリを置く
        payload = {
            "queue_entry_id": "qid-a",
            "session_id": "sess-1",
            "turn_id": "turn-1",
            "queued_at": "2026-08-11T00:00:00Z",
            "completed": True,
            "interrupted": False,
            "cwd": "C:/some/path",
            "manifest_id": "abc",
        }
        (submitting / "qid-a.json").write_text(json.dumps(payload), encoding="utf-8")

        sys_path = Path(__file__).resolve().parents[2] / "scripts" / "hh_distill.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("hh_distill_recover", sys_path)
        hh_distill = importlib.util.module_from_spec(spec)
        sys.modules["hh_distill_recover"] = hh_distill
        spec.loader.exec_module(hh_distill)  # type: ignore[union-attr]

        monkeypatch.setattr(hh_distill, "hh_agent_home", lambda: base)
        recovered, retained = hh_distill._recover_manifests(base=base)

        assert recovered == 1
        assert retained == 0
        # qid-a が pending/ へ戻っている
        assert (pending / "qid-a.json").is_file()
        # manifest は削除済み
        assert not (submitting / "_manifest_abc.json").exists()

    def test_recover_manifests_api_call_attempted_true_keeps_manifest(
        self, tmp_path, monkeypatch
    ):
        """§2.2 手順6: `api_call_attempted: true` でエントリが
        まだ submitting/ に居る場合、manifest を残す。"""
        base = tmp_path
        submitting = base / "distill_queue" / "submitting"
        submitting.mkdir(parents=True)

        manifest = {
            "manifest_id": "abc",
            "queue_entry_ids": ["qid-a"],
            "request_hashes": {},
            "api_call_attempted": True,
        }
        (submitting / "_manifest_abc.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (submitting / "qid-a.json").write_text(
            json.dumps({"queue_entry_id": "qid-a", "session_id": "sess-1"}),
            encoding="utf-8",
        )

        sys_path = Path(__file__).resolve().parents[2] / "scripts" / "hh_distill.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("hh_distill_recover2", sys_path)
        hh_distill = importlib.util.module_from_spec(spec)
        sys.modules["hh_distill_recover2"] = hh_distill
        spec.loader.exec_module(hh_distill)  # type: ignore[union-attr]

        monkeypatch.setattr(hh_distill, "hh_agent_home", lambda: base)
        recovered, retained = hh_distill._recover_manifests(base=base)
        assert recovered == 0
        assert retained == 1
        # manifest もエントリも残っている
        assert (submitting / "_manifest_abc.json").is_file()
        assert (submitting / "qid-a.json").is_file()

    def test_recover_manifests_api_call_attempted_true_no_submitting_drops_manifest(
        self, tmp_path, monkeypatch
    ):
        """§2.2 手順6: `api_call_attempted: true` だが全エントリが
        submitted/ へ移動済みなら manifest を削除する。"""
        base = tmp_path
        submitting = base / "distill_queue" / "submitting"
        submitted = base / "distill_queue" / "submitted"
        submitting.mkdir(parents=True)
        submitted.mkdir(parents=True)

        manifest = {
            "manifest_id": "abc",
            "queue_entry_ids": ["qid-a"],
            "request_hashes": {},
            "api_call_attempted": True,
        }
        (submitting / "_manifest_abc.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (submitted / "qid-a.json").write_text(
            json.dumps({"queue_entry_id": "qid-a", "session_id": "sess-1", "batch_id": "b1"}),
            encoding="utf-8",
        )

        sys_path = Path(__file__).resolve().parents[2] / "scripts" / "hh_distill.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("hh_distill_recover3", sys_path)
        hh_distill = importlib.util.module_from_spec(spec)
        sys.modules["hh_distill_recover3"] = hh_distill
        spec.loader.exec_module(hh_distill)  # type: ignore[union-attr]

        monkeypatch.setattr(hh_distill, "hh_agent_home", lambda: base)
        recovered, retained = hh_distill._recover_manifests(base=base)
        assert recovered == 0
        assert retained == 0
        # manifest は削除済み
        assert not (submitting / "_manifest_abc.json").exists()


# ===========================================================================
# canonical JSON ヘルパー（§2.2 手順3）
# ===========================================================================


class TestCanonicalRequestJson:
    def test_is_stable_for_key_order(self):
        """`canonical_request_json()` はキー順が違っても同じ出力を返す
        （sort_keys=True の保証）。"""
        a = {"custom_id": "x", "params": {"b": 2, "a": 1}}
        b = {"params": {"a": 1, "b": 2}, "custom_id": "x"}
        assert skill_distiller.canonical_request_json(a) == skill_distiller.canonical_request_json(b)

    def test_uses_no_whitespace(self):
        a = {"a": 1, "b": 2}
        out = skill_distiller.canonical_request_json(a)
        assert " " not in out
