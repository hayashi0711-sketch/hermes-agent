"""scripts/hh_check_upstream.py — notify (ntfy) when upstream (NousResearch
Hermes Agent) has drifted ahead of this fork's local checkout.

Context: this repo tracks `origin` (nousresearch/hermes-agent) and merges it
into `main` (pushed to `myfork/hh-agent`) periodically. Relying on someone
remembering to check divergence at the start of every session has already
let the gap grow to 1334 commits once (2026-08-18). This script is the
notify-only half of the fix: a scheduled task runs it, it fetches and counts
how far behind we are, and sends an ntfy alert once the gap crosses a
threshold — so a human sees it without anyone having to ask. It never merges,
tests, pushes, or deploys anything; that stays a manual/Claude-Code-driven
decision (see docs/hh-agent/08_Handoff_Note.md, 21st session).

CLI: `python scripts/hh_check_upstream.py` (no args). Exits 0 always —
network/git failures are logged to stderr and swallowed, matching the same
fail-open convention as hh_skill_sync.py's notification path, so a flaky
run never shows up as a scheduled-task failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "modal_hub" / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hh_issue_agent_token import load_ntfy_credentials  # noqa: E402

#: Minimum "behind" count before we bother notifying at all. Small day-to-day
#: drift (upstream is a very active repo) isn't worth an alert.
BEHIND_THRESHOLD = 20

#: Re-notify once the gap has grown by at least this many additional commits
#: since the last notification, even within the same day.
RENOTIFY_STEP = 200

_HH_AGENT_HOME_ENV = "USERPROFILE"
_STATE_FILENAME = "upstream_check_state.json"


def hh_agent_home() -> Path:
    userprofile = os.environ.get(_HH_AGENT_HOME_ENV)
    base = Path(userprofile) if userprofile else Path.home()
    return base / ".hh-agent"


def _state_path() -> Path:
    return hh_agent_home() / _STATE_FILENAME


def _load_state() -> dict:
    path = _state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _behind_count() -> Optional[int]:
    fetch = _git("fetch", "origin", "main", "--quiet")
    if fetch.returncode != 0:
        print(f"[hh_check_upstream] WARN: git fetch failed: {fetch.stderr.strip()}", file=sys.stderr)
        return None
    count = _git("rev-list", "--count", "HEAD..origin/main")
    if count.returncode != 0:
        print(f"[hh_check_upstream] WARN: rev-list failed: {count.stderr.strip()}", file=sys.stderr)
        return None
    try:
        return int(count.stdout.strip())
    except ValueError:
        return None


def _send_ntfy(behind: int) -> bool:
    topic, token = load_ntfy_credentials()
    if not topic:
        print("[hh_check_upstream] WARN: NTFY_TOPIC not configured, skipping notification", file=sys.stderr)
        return False
    sys.path.insert(0, str(_REPO_ROOT))
    from modal_hub.services.ntfy_client import send_via_ntfy  # noqa: PLC0415

    return send_via_ntfy(
        topic,
        token,
        title="H-H-Agent upstream sync",
        message=f"Hermes Agent 本家が {behind} コミット先行しています。同期を検討してください。",
        tags=["arrow_up", "hermes"],
    )


def main() -> int:
    behind = _behind_count()
    if behind is None:
        return 0  # network/git failure already logged; never fail the task

    state = _load_state()

    if behind == 0:
        if state:
            _save_state({})
        return 0

    if behind < BEHIND_THRESHOLD:
        return 0

    today = date.today().isoformat()
    last_notified_behind = state.get("last_notified_behind", 0)
    last_notified_date = state.get("last_notified_date", "")

    should_notify = (
        last_notified_date != today
        or behind >= last_notified_behind + RENOTIFY_STEP
    )
    if not should_notify:
        return 0

    sent = _send_ntfy(behind)
    if sent:
        _save_state({"last_notified_behind": behind, "last_notified_date": today})
    else:
        print(f"[hh_check_upstream] behind={behind} but notification send failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
