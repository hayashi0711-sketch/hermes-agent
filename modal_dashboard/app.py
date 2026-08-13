"""hh-agent-dashboard Modal entrypoint.

Phase 1c: hosts hermes_cli.web_server:app (the `hermes dashboard` FastAPI
app, including /api/pty) as a single scale-to-zero Modal container.
Separate Modal App from modal_hub/ (the Phase1a/1b approval-gate addon).
See docs/hh-agent/08_Phase1c_Spec.md for the full design.
"""

from __future__ import annotations

from pathlib import Path

import modal

from modal_dashboard import bootstrap, token_refresh

_DASHBOARD_VOLUME_NAME = "hh-agent-dashboard-home"
_DASHBOARD_MOUNT_PATH = "/opt/data"
_DASHBOARD_SECRET_NAME = "hh-agent-dashboard-secret"
_HUB_SECRET_NAME = "hh-agent-secret"  # existing Phase1a secret, reused for HH_AGENT_TOKEN_SIGNING_KEY only

app = modal.App("hh-agent-dashboard")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE_PATH = Path(__file__).resolve().parent / "Dockerfile"

image = modal.Image.from_dockerfile(_DOCKERFILE_PATH, context_dir=_REPO_ROOT)


def _import_web_server_app():
    """Import boundary, so tests can substitute a sentinel without needing
    hermes_cli's full dependency stack importable in the test environment.
    """
    from hermes_cli import web_server

    return web_server.app


def _build_fastapi_app(hermes_home: Path, *, store):
    """Seed HERMES_HOME, verify the approval gate, then return web_server.app.

    Split out from fastapi_app() so it's testable without a real Modal
    container (Task 4 test). Order is deliberate: config must be seeded
    and the hook verified *before* touching the token or importing the
    dashboard app -- D-14 requires failing closed before anything else.
    """
    bootstrap.seed_config_yaml(hermes_home)
    bootstrap.verify_pretool_hooks_registered()
    token_refresh.seed_agent_token_if_missing(hermes_home, store=store)
    return _import_web_server_app()


@app.function(
    image=image,
    volumes={_DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True)},
    secrets=[
        modal.Secret.from_name(_DASHBOARD_SECRET_NAME),
        modal.Secret.from_name(_HUB_SECRET_NAME),
    ],
    min_containers=0,       # scale-to-zero -- cost floor is $0 (docs/hh-agent/08_Phase1c_Spec.md §2.2)
    max_containers=1,       # required -- see Global Constraints
    scaledown_window=300,
)
@modal.asgi_app()
def fastapi_app():
    from modal_hub.core import store

    return _build_fastapi_app(Path(_DASHBOARD_MOUNT_PATH), store=store)


@app.function(
    image=image,
    volumes={_DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True)},
    secrets=[
        modal.Secret.from_name(_DASHBOARD_SECRET_NAME),
        modal.Secret.from_name(_HUB_SECRET_NAME),
    ],
    max_containers=1,
    schedule=modal.Period(hours=8),   # TTL is 24h; 8h interval leaves ample margin
)
def refresh_dashboard_agent_token():
    from modal_hub.core import store

    token_refresh.issue_dashboard_agent_token(Path(_DASHBOARD_MOUNT_PATH), store=store)
