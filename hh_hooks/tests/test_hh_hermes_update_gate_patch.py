"""Regression test for hh_hermes.py's dashboard-update-gate monkeypatch.

Pothole 73 (2026-08-18, see Obsidian H-H-Agent/08_Handoff_Note.md): Modal
mounts a Volume at /opt/data as a symlink to /__modal/volumes/<id>, so
Hermes's own `.resolve()`-based "is this the hosted /opt/data layout"
check silently fails and `can_update_hermes` reports True in production.
H-H-Agent works around this with its own launcher-level monkeypatch
(hh_hermes.py) rather than touching Hermes source.

The 2026-09-05 upstream merge (0.20.6 -> 0.21.0) moved the target
function from `hermes_cli.web_server` to `hermes_cli.web_server_files`
(accessed elsewhere via a late-binding proxy in `hermes_cli.web_deps`).
The old patch silently became a no-op because it patched an attribute on
a module the live code no longer reads. This test pins the patch to the
module it must actually target so a future refactor breaks loudly here
instead of silently in production.
"""

from __future__ import annotations

import importlib
import os

import pytest


def test_patch_targets_web_server_files_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", "/opt/data")

    import hermes_cli.web_server_files as web_server_files

    original = web_server_files._dashboard_local_update_managed_externally
    try:
        import hh_hermes

        hh_hermes._patch_dashboard_update_gate()

        assert web_server_files._dashboard_local_update_managed_externally() is True
    finally:
        web_server_files._dashboard_local_update_managed_externally = original


def test_patch_is_noop_outside_opt_data_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", "/some/other/path")

    import hermes_cli.web_server_files as web_server_files

    original = web_server_files._dashboard_local_update_managed_externally
    try:
        import hh_hermes

        importlib.reload(hh_hermes)
        hh_hermes._patch_dashboard_update_gate()

        assert web_server_files._dashboard_local_update_managed_externally is original
    finally:
        web_server_files._dashboard_local_update_managed_externally = original


def test_late_proxy_actually_resolves_the_patched_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the exact production failure mode: the status router calls the
    function through `web_deps.late(...)`, not through a direct import. If a
    future change makes the proxy resolve against a *copy* of the module
    (or caches the callable at import time) instead of the live module
    object, this catches it even though the two prior tests would still pass.
    """
    monkeypatch.setenv("HERMES_HOME", "/opt/data")

    import hermes_cli.web_server_files as web_server_files
    from hermes_cli.web_deps import late

    original = web_server_files._dashboard_local_update_managed_externally
    try:
        import hh_hermes

        hh_hermes._patch_dashboard_update_gate()

        proxy = late(
            "_dashboard_local_update_managed_externally", "hermes_cli.web_server_files"
        )
        assert proxy() is True
    finally:
        web_server_files._dashboard_local_update_managed_externally = original
