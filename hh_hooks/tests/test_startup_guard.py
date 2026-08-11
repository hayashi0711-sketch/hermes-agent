"""Tests for the HH-Agent Hermes startup self-diagnosis (D-20)."""

from __future__ import annotations

import pytest

from agent import shell_hooks
from hh_hooks.startup_guard import diagnose_pretool_hooks, enforce_or_exit


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    from hermes_cli import plugins

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.delenv("HERMES_ACCEPT_HOOKS", raising=False)
    monkeypatch.setattr(plugins, "_plugin_manager", plugins.PluginManager())
    shell_hooks.reset_for_tests()
    yield
    shell_hooks.reset_for_tests()


def _hook_config(*, matcher=None, fail_closed=True):
    entry = {
        "command": "python hh_hooks/tool_gate.py",
        "fail_closed": fail_closed,
    }
    if matcher is not None:
        entry["matcher"] = matcher
    return {
        "hooks_auto_accept": True,
        "hooks": {"pre_tool_call": [entry]},
    }


def test_empty_config_has_no_registered_pretool_hook():
    result = diagnose_pretool_hooks({})

    assert result.ok is False
    assert result.registered_count == 0
    assert "no pre_tool_call hook" in " ".join(result.reasons)


def test_list_hooks_block_is_rejected_by_real_parser():
    result = diagnose_pretool_hooks(
        {
            "hooks_auto_accept": True,
            "hooks": [
                {
                    "pre_tool_call": [
                        {
                            "command": "python hh_hooks/tool_gate.py",
                            "fail_closed": True,
                        }
                    ]
                }
            ],
        }
    )

    assert result.ok is False
    assert result.registered_count == 0
    assert "no pre_tool_call hook" in " ".join(result.reasons)


def test_full_coverage_fail_closed_hook_passes():
    result = diagnose_pretool_hooks(_hook_config())

    assert result.ok is True
    assert result.reasons == []
    assert result.registered_count == 1


def test_fail_open_hook_is_rejected():
    result = diagnose_pretool_hooks(_hook_config(fail_closed=False))

    assert result.ok is False
    assert result.registered_count == 1
    reasons = " ".join(result.reasons)
    assert "fail_closed=false" in reasons
    assert "python hh_hooks/tool_gate.py" in reasons


def test_narrow_matcher_leaves_named_probes_uncovered():
    result = diagnose_pretool_hooks(_hook_config(matcher="^Bash$"))

    assert result.ok is False
    assert result.registered_count == 1
    reasons = " ".join(result.reasons)
    assert "uncovered probe tool(s)" in reasons
    assert "terminal" in reasons
    assert "mcp__server__tool" in reasons
    assert "hyphenated-tool" in reasons


def test_enforce_or_exit_blocks_failure_and_allows_success(capsys):
    with pytest.raises(SystemExit) as exc_info:
        enforce_or_exit({})

    assert exc_info.value.code == 1
    assert "[HH-AGENT] STARTUP BLOCKED (D-20):" in capsys.readouterr().err

    enforce_or_exit(_hook_config())
    stderr = capsys.readouterr().err
    assert "[HH-AGENT] startup guard: 1 pre_tool_call hook(s) registered" in stderr
    assert "full coverage, fail_closed=true. OK." in stderr
