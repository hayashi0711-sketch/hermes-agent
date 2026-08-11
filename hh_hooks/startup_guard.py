"""Hermes startup self-diagnosis for the HH-Agent approval gate (D-20)."""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass


@dataclass
class DiagnosisResult:
    ok: bool
    reasons: list[str]
    registered_count: int


def _probe_tool_names() -> tuple[str, ...]:
    """Return varied tool names, including one unpredictable per invocation."""
    return (
        "Bash",
        "terminal",
        "mcp__server__tool",
        "hyphenated-tool",
        f"hh_startup_probe_{uuid.uuid4().hex}",
    )


def diagnose_pretool_hooks(
    cfg: dict | None = None,
    *,
    accept_hooks: bool = False,
) -> DiagnosisResult:
    """Diagnose the pre-tool shell hooks that Hermes actually registers."""
    if cfg is None:
        from hermes_cli.config import load_config

        cfg = load_config()

    from agent.shell_hooks import register_from_config

    registered = register_from_config(cfg, accept_hooks=accept_hooks)
    pretool_specs = [
        spec for spec in registered if spec.event == "pre_tool_call"
    ]
    registered_count = len(pretool_specs)

    if not pretool_specs:
        return DiagnosisResult(
            ok=False,
            reasons=["no pre_tool_call hook is registered"],
            registered_count=0,
        )

    probes = _probe_tool_names()
    reasons: list[str] = []

    uncovered = [
        probe
        for probe in probes
        if not any(spec.matches_tool(probe) for spec in pretool_specs)
    ]
    if uncovered:
        reasons.append(
            "pre_tool_call hook coverage is incomplete; uncovered probe "
            f"tool(s): {', '.join(uncovered)}"
        )

    for spec in pretool_specs:
        if any(spec.matches_tool(probe) for probe in probes) and not spec.fail_closed:
            reasons.append(
                "pre_tool_call hook has fail_closed=false: "
                f"{spec.command}"
            )

    return DiagnosisResult(
        ok=not reasons,
        reasons=reasons,
        registered_count=registered_count,
    )


def enforce_or_exit(
    cfg: dict | None = None,
    *,
    accept_hooks: bool = False,
) -> None:
    """Abort startup unless the registered pre-tool hooks satisfy D-20."""
    result = diagnose_pretool_hooks(cfg, accept_hooks=accept_hooks)
    if not result.ok:
        for reason in result.reasons:
            print(
                f"[HH-AGENT] STARTUP BLOCKED (D-20): {reason}",
                file=sys.stderr,
            )
        sys.exit(1)

    print(
        "[HH-AGENT] startup guard: "
        f"{result.registered_count} pre_tool_call hook(s) registered, "
        "full coverage, fail_closed=true. OK.",
        file=sys.stderr,
    )
