"""hh-agent-dashboard Modal entrypoint.

Phase 1c: runs the REAL `hermes dashboard` CLI (via hh_hermes.py, the
Phase1a launcher that enforces the pre_tool_call approval-gate hook
before handing off -- D-14/D-20) as a subprocess inside a
`@modal.web_server` container.

2026-08-13 redesign: the original `@modal.asgi_app()` approach (return
`hermes_cli.web_server.app` directly) was reviewed and found to bypass
everything `hermes_cli.web_server.start_server()` normally wires up:
`app.state.bound_host`/`bound_port` (needed for `/api/pty` to attach to
THIS process's in-process gateway instead of spawning an unverified
`tui_gateway.entry` subprocess -- Codex finding C-1) and
`app.state.auth_required` (needed to gate unauthenticated access --
Codex finding C-2). Rather than re-implement that wiring by hand (fragile,
drifts from Hermes's own tested behavior), this launches the real
`hermes dashboard --host 0.0.0.0 ...` subcommand, which already handles
all of it correctly for a non-loopback bind. Separate Modal App from
modal_hub/ (the Phase1a/1b approval-gate addon).

`hh-agent-secret` (containing `HH_AGENT_TOKEN_SIGNING_KEY`, a Hub root
credential) is attached ONLY to `refresh_dashboard_agent_token` -- never
to `dashboard_server`, which runs untrusted model-authored commands
(Codex finding C-3). First-boot token seeding calls the refresh function
remotely instead of signing locally.

See docs/hh-agent/08_Phase1c_Spec.md for the full design.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import modal

from modal_dashboard import bootstrap, token_refresh

_DASHBOARD_VOLUME_NAME = "hh-agent-dashboard-home"
_DASHBOARD_MOUNT_PATH = "/opt/data"
_DASHBOARD_SECRET_NAME = "hh-agent-dashboard-secret"
_HUB_SECRET_NAME = "hh-agent-secret"  # ONLY attached to refresh_dashboard_agent_token -- see module docstring, C-3
_DASHBOARD_PORT = 8000

app = modal.App("hh-agent-dashboard")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE_PATH = Path(__file__).resolve().parent / "Dockerfile"

image = modal.Image.from_dockerfile(_DOCKERFILE_PATH, context_dir=_REPO_ROOT)


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


def _ensure_agent_token_seeded(hermes_home: Path) -> None:
    """First-boot only: mint a token via a remote call if none exists yet.

    Deliberately does NOT sign locally -- this function runs inside
    dashboard_server(), which never has hh-agent-secret attached (C-3),
    so it has no signing key to sign with even if it wanted to. Calling
    refresh_dashboard_agent_token.remote() delegates issuance to the one
    function that does carry that credential, short-lived and isolated
    from the untrusted agent execution environment.
    """
    token_path = hermes_home / ".hh-agent" / "agent_token.json"
    if token_path.is_file():
        return
    refresh_dashboard_agent_token.remote()


@app.function(
    image=image,
    volumes={_DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True)},
    secrets=[modal.Secret.from_name(_DASHBOARD_SECRET_NAME)],  # NOT hh-agent-secret -- see module docstring, C-3
    min_containers=0,       # scale-to-zero -- cost floor is $0 (docs/hh-agent/08_Phase1c_Spec.md §2.2)
    max_containers=1,       # required -- see Global Constraints
    scaledown_window=300,
    timeout=86400,          # persistent interactive WS sessions, not a batch job (fixes I-2)
)
@modal.concurrent(max_inputs=8)  # dashboard needs several simultaneous WS connections per browser tab (fixes I-1)
@modal.web_server(port=_DASHBOARD_PORT, startup_timeout=90)
def dashboard_server():
    hermes_home = Path(_DASHBOARD_MOUNT_PATH)
    bootstrap.seed_config_yaml(hermes_home)
    # Cheap pre-flight check: fail fast with a clear error before even
    # spawning the subprocess. The REAL enforcement -- the one that
    # actually closes C-1 -- is hh_hermes.py's own enforce_or_exit(),
    # which runs inside the process that actually executes tools.
    bootstrap.verify_pretool_hooks_registered()
    _ensure_agent_token_seeded(hermes_home)

    # `--host 0.0.0.0` (not 127.0.0.1) is load-bearing: it's what makes
    # Hermes's own should_require_auth() correctly treat this as a public
    # bind (auth_required=True, refuses to boot without a registered
    # dashboard_auth provider -- HERMES_DASHBOARD_BASIC_AUTH_USERNAME/
    # _PASSWORD in hh-agent-dashboard-secret registers the bundled
    # `basic` provider) and sets app.state.bound_host so
    # _resolve_client_ws_host() substitutes 127.0.0.1 for the wildcard,
    # giving /api/pty a valid loopback URL to attach to this same
    # process instead of spawning a fresh, unverified tui_gateway.entry.
    # `--skip-build`: the web dist is already baked into the image at
    # Docker build time (§4 of the Dockerfile) -- no npm at runtime.
    subprocess.Popen(
        [
            "python", "/opt/hermes/hh_hermes.py", "dashboard",
            "--host", "0.0.0.0",
            "--port", str(_DASHBOARD_PORT),
            "--no-open",
            "--skip-build",
        ],
        cwd="/opt/hermes",
    )
