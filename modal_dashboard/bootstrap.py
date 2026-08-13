"""Startup bootstrap for the Phase1c cloud Hermes dashboard.

Seeds a fresh HERMES_HOME (empty Modal Volume on first boot) with the
pre_tool_call approval-gate hook, and verifies at ASGI-construction time
that the hook actually registered before the app is allowed to serve
traffic. D-14 (docs/hh-agent/03_Architecture.md): never boot a cloud
Hermes instance that can run tools without the approval gate.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_TOOL_GATE_COMMAND = "python /opt/hermes/hh_hooks/tool_gate.py"

_HOOKS_CONFIG = {
    "hooks_auto_accept": True,
    "hooks": {
        "pre_tool_call": [
            {
                "command": _TOOL_GATE_COMMAND,
                "matcher": ".*",
                "fail_closed": True,
                "timeout": 200,
            }
        ]
    },
}


class DashboardStartupError(RuntimeError):
    """Raised when the dashboard cannot safely serve traffic and must not boot.

    Mirrors modal_hub/main.py's HubStartupError: every caller of this
    module's functions must let this propagate, not catch-and-log. A
    container that boots without a working approval gate would silently
    let the cloud agent run any tool with no human in the loop.
    """


def seed_config_yaml(hermes_home: Path) -> bool:
    """Write config.yaml with the approval-gate hook if it doesn't exist yet.

    Returns True if this call wrote the file (first boot on an empty
    Volume), False if config.yaml already existed and was left untouched
    (every boot after the first -- never clobber operator-made changes).
    """
    config_path = hermes_home / "config.yaml"
    if config_path.is_file():
        return False
    hermes_home.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(_HOOKS_CONFIG, sort_keys=False),
        encoding="utf-8",
    )
    return True


def verify_pretool_hooks_registered() -> None:
    """Raise DashboardStartupError unless a fail-closed pre_tool_call hook is registered.

    Wraps hh_hooks.startup_guard.diagnose_pretool_hooks() (D-20) so a
    Modal container that somehow ends up with an unprotected agent
    refuses to serve traffic instead of silently running tools with no
    approval gate.
    """
    from hh_hooks.startup_guard import diagnose_pretool_hooks

    result = diagnose_pretool_hooks()
    if not result.ok:
        raise DashboardStartupError(
            "pre_tool_call approval-gate hook is not fully registered; "
            "refusing to boot a dashboard that could run tools without "
            "the approval gate (D-14): " + "; ".join(result.reasons)
        )
