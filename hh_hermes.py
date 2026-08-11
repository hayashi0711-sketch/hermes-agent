#!/usr/bin/env python3
"""HH-Agent launcher.

Loosely-coupled addon (D-20 / Codex finding C5): runs the startup
self-diagnosis before handing off to the real Hermes CLI entry point.
Does not modify any Hermes source file. Use this instead of ./hermes
so a broken or unregistered pre_tool_call hook aborts startup instead
of silently running with the approval gate disabled.
"""

import hermes_bootstrap  # noqa: F401  (same bootstrap every Hermes entry point uses)


if __name__ == "__main__":
    from hh_hooks.startup_guard import enforce_or_exit

    enforce_or_exit()

    from hermes_cli.main import main

    main()
