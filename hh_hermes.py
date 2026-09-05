#!/usr/bin/env python3
"""HH-Agent launcher.

Loosely-coupled addon (D-20 / Codex finding C5): runs the startup
self-diagnosis before handing off to the real Hermes CLI entry point.
Does not modify any Hermes source file. Use this instead of ./hermes
so a broken or unregistered pre_tool_call hook aborts startup instead
of silently running with the approval gate disabled.
"""

import os

import hermes_bootstrap  # noqa: F401  (same bootstrap every Hermes entry point uses)


def _patch_dashboard_update_gate() -> None:
    """Work around a Hermes/Modal interaction bug -- no Hermes source touched.

    Hermes's own ``_dashboard_local_update_managed_externally()`` is
    *supposed* to disable the dashboard's built-in "Update Hermes" button
    for hosted layouts where ``HERMES_HOME=/opt/data`` (this deployment).
    It identifies that layout by resolving the Hermes root and comparing it
    to ``Path("/opt/data")`` (see ``_default_hermes_root_is_opt_data()``),
    which Hermes assumes is a no-op for a normal Docker bind mount. Modal
    instead mounts Volumes as an actual symlink to an internal
    ``/__modal/volumes/<id>`` path, so ``.resolve()`` unwinds it and the
    comparison silently fails -- the dashboard reports
    ``can_update_hermes: true`` and offers a working-looking "Update now"
    button. Clicking it always fails: this image's ``.git`` is excluded by
    ``.dockerignore`` (no working tree to pull into), and even a
    successful in-place update would be pointless here -- Modal container
    filesystems are ephemeral, so real updates only ever happen via
    ``modal deploy``. Patched here (H-H-Agent's own launcher) rather than
    in Hermes source to keep Hermes itself unmodified.

    As of the 2026-09-05 upstream merge (0.20.6 -> 0.21.0), this function
    lives in ``hermes_cli.web_server_files`` (extracted out of
    ``web_server.py``) and is invoked through a late-binding proxy
    (``hermes_cli.web_deps.late()``) that re-resolves the attribute on that
    module at call time -- so patching the attribute there (rather than on
    the old ``hermes_cli.web_server`` module) is what the proxy will
    actually pick up.
    """
    if os.environ.get("HERMES_HOME") != "/opt/data":
        return  # only this Modal-hosted layout hits the bug; leave local/dev runs alone
    try:
        import hermes_cli.web_server_files as _wsf
    except ImportError:
        return
    _wsf._dashboard_local_update_managed_externally = lambda: True


if __name__ == "__main__":
    _patch_dashboard_update_gate()

    from hh_hooks.startup_guard import enforce_or_exit

    enforce_or_exit()

    from hermes_cli.main import main

    main()
