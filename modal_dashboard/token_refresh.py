"""Agent-token lifecycle for the Phase1c cloud Hermes dashboard.

Two call sites use this module (docs/hh-agent/08_Phase1c_Spec.md §3.3-3.4):
  - modal_dashboard.app._build_fastapi_app() -- seeds a token only if none
    exists yet (first boot on an empty Volume).
  - modal_dashboard.app.refresh_dashboard_agent_token() -- a Modal cron
    function that unconditionally reissues the token every 8h, well
    inside the 24h TTL (modal_hub.core.security.AGENT_TOKEN_TTL_SECONDS).

Reuses modal_hub.core.security.issue_agent_token() directly rather than
reimplementing signing, so this module never duplicates
HH_AGENT_TOKEN_SIGNING_KEY handling or the Hub ownership-record write.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

_SUB = "cloud_agent:phase1c-dashboard"
_SESSION_PREFIX = "phase1c-dashboard"

# Phase1c has no single git-repo cwd to hash the way tool_gate.py's local
# _compute_workspace_id() does (the cloud agent's cwd varies per request).
# A fixed sentinel is safe: approval_gate.py never cross-checks a token's
# workspace_id/session_id against what tool_gate.py sends at request time
# -- it only compares values already stored in its own record against
# each other (docs/hh-agent/08_Handoff_Note.md pitfall #24). Any
# well-formed 64-hex sha256 digest satisfies issue_agent_token()'s
# validation.
_WORKSPACE_ID = hashlib.sha256(b"hh-agent-dashboard").hexdigest()


def _signing_key() -> bytes:
    key = os.environ.get("HH_AGENT_TOKEN_SIGNING_KEY")
    if not key:
        raise RuntimeError("HH_AGENT_TOKEN_SIGNING_KEY is not set")
    return key.encode("utf-8")


def _write_token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"token": token}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def issue_dashboard_agent_token(hermes_home: Path, *, store, now: Optional[float] = None) -> str:
    """Issue a fresh agent token and write it to <hermes_home>/.hh-agent/agent_token.json.

    Always issues (unconditional overwrite) -- callers decide whether
    that's appropriate for their call site (see module docstring).
    Returns the raw token string. Callers must not log it.
    """
    from modal_hub.core import security

    token = security.issue_agent_token(
        store,
        sub=_SUB,
        source=security.SOURCE_CLOUD_AGENT,
        session_id=f"{_SESSION_PREFIX}-{uuid.uuid4()}",
        workspace_id=_WORKSPACE_ID,
        signing_key=_signing_key(),
        now=now,
    )
    token_path = hermes_home / ".hh-agent" / "agent_token.json"
    _write_token_file(token_path, token)
    return token


def seed_agent_token_if_missing(hermes_home: Path, *, store) -> bool:
    """Issue a token only if agent_token.json does not exist yet.

    Returns True if it issued a token (first boot), False if the file
    already existed (left untouched -- avoids clobbering a token the
    refresh cron just wrote with a stale one; see spec §3.3).
    """
    token_path = hermes_home / ".hh-agent" / "agent_token.json"
    if token_path.is_file():
        return False
    issue_dashboard_agent_token(hermes_home, store=store)
    return True
