"""AmadeuS-Remote memory provider plugin for Hermes Agent.

Bridges the Modal-hosted Hermes (hh-agent-dashboard) onto AmadeuS — the
unified memory gateway that routes every query/write across NCAM and
Corpus2Skill — by calling the already-deployed AmadeuS-Remote Modal
service (https://hayashi0711--amadeus-remote-serve.modal.run), reused as
a Hermes "Memory Provider" (see the ``MemoryProvider`` ABC in
``agent/memory_provider.py`` and
website/docs/developer-guide/memory-provider-plugin.md).

Background / task brief (2026-09-09, Hermes-Hyper-Agent_HHAgent):
    AmadeuS project "Phase 5b" — the Modal-side Hermes equivalent of the
    Windows-native amadeus_hooks cutover (AmadeuS 05_Current_State.md
    「2026-09-09: Phase 4 / Phase 5」). This plugin is a *complement* to the
    existing ``.hermes/plugins/corpus2skill/`` provider, which is left in
    place untouched; switching ``memory.provider`` on the live Volume is a
    separate, later task. Do not delete or modify the corpus2skill plugin.

Design contract:
    The corpus2skill plugin is the structural template for this file
    (docs/hh-agent/03_Architecture.md §13 M-05/M-06/M-07 spell out the
    placement, hook set and tool-surface rules that apply unchanged here —
    that section is written for the Corpus2Skill plugin; a dedicated
    AmadeuS-Remote entry in it is a separate doc task, so this module's
    docstring is the in-repo record for now). The backend side of the
    contract lives in the AmadeuS repo: ``doc/12_Phase5_Architecture.md``
    (D-50〜D-62) and the deployed ``services/modal/app.py``, read together
    with the Windows-native ``amadeus_hooks/`` package (behavioral parity
    target for the automatic hooks).

Key decisions baked into this implementation:

- **Transport**: plain HTTPS REST over the stdlib ``urllib`` — no MCP
  client, no third-party HTTP dependency. AmadeuS-Remote exposes exactly
  this REST fallback (``/api/query``, ``/api/write``, ``/api/status``,
  ``/health``) for clients that cannot use the MCP SDK — this Hermes runs
  with ``HERMES_DISABLE_LAZY_INSTALLS=1`` (sealed venv), so the stdlib-only
  constraint is structural, not stylistic (AmadeuS doc/12 D-60〜D-62 was
  added for precisely this reason).
- **Read-only tool surface**: the ONLY tool this provider exposes to the
  agent is ``amadeus_query`` (a thin wrapper over ``GET /api/query``).
  Nothing that can write (journal / episode / ...) is ever exposed as an
  agent-callable tool — see ``get_tool_schemas()`` and the regression test
  that asserts this (M-07's "書き込み系ツールは絶対に公開しない").
- **Automatic hooks write for real** (``apply=True``): ``sync_turn()``
  journals every turn (``write_type=journal``) and ``on_session_end()``
  records one session-end episode (``write_type=episode``, role
  ``system``), matching the Windows-native amadeus_hooks behavior — its
  journal and session_end hooks call ``route_write(..., apply=True)`` so
  entries actually land in the AmadeuS ledger (``state: committed``).
  ``apply=False`` (dry-run) is the AmadeuS default only because the MCP
  ``memory_write`` tool is an LLM-facing surface where plan-first is
  right; these two hooks are deterministic background recorders with no
  human in the loop, where a dry-run would silently record nothing.
  (The corpus2skill plugin's v1 ``on_session_end()`` is a no-op; here it
  is implemented deliberately — see the AmadeuS Phase-4 cutover, which
  records session boundaries as episodes.)
- **Fail-soft**: ``prefetch()`` swallows backend errors and returns an
  empty string (context loss is acceptable; a hung/broken turn is not).
  ``sync_turn()`` runs in a daemon thread per the developer-guide's
  threading contract and only ever logs failures — it must never raise
  back into the agent's turn loop. ``on_session_end()`` is allowed to be
  synchronous (not under the threading contract) but also only logs.
- **Session/turn bookkeeping**: the turn-index counter is per-session and
  monotonic; it resets in ``initialize()`` and ``on_session_switch()`` so
  journal writes always carry a valid ``turn_index`` for the session they
  belong to (AmadeuS's journal idempotency key is
  ``journal.s{session_id}.t{turn_index}`` — a wrong index silently
  duplicates/skips entries, the bug the Windows side hit in Phase 4).
  The session_id itself is passed through as-is (it must match
  ``^[A-Za-z0-9_-]{1,128}$``, the C2S format AmadeuS-Remote validates
  against — Hermes session ids are UUID-style and satisfy this).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

# `agent.memory_provider` is part of the Hermes core the plugin runs inside
# of, so this import is expected to always succeed when Hermes actually
# loads this plugin. The fallback below exists only so this module can still
# be imported (and its non-ABC-dependent logic tested) in a context where
# the Hermes core package tree isn't on sys.path at all -- e.g. a bare
# static-analysis pass over just this directory. Same pattern as the
# corpus2skill plugin.
try:
    from agent.memory_provider import MemoryProvider
except Exception:  # pragma: no cover - only exercised outside a real Hermes checkout
    class MemoryProvider:  # type: ignore[no-redef]
        """Fallback stand-in used only when the real Hermes core is unavailable."""


logger = logging.getLogger(__name__)

_ENV_API_KEY = "AMADEUS_REMOTE_API_KEY"
_DEFAULT_BASE_URL = "https://hayashi0711--amadeus-remote-serve.modal.run"
# AmadeuS-Remote is a Modal app with scaledown_window=600 (10 min), so cold
# starts are the normal case and explicit tool/journal calls must tolerate
# them. NOTE: this does NOT govern prefetch(), which keeps its own tighter
# deadline budget below to stay under MemoryManager's 8s external-prefetch
# abandonment window (same split as the corpus2skill plugin).
_DEFAULT_TIMEOUT_SECONDS = 15.0
_CONFIG_FILENAME = "amadeus_remote.json"
# prefetch() runs under a TOTAL 7.0s deadline (MemoryManager abandons
# external prefetch after 8s, so a genuine cold start must never stall the
# turn past that window). Unlike corpus2skill there is no lane split to
# budget across: /api/query already fans out across NCAM and Corpus2Skill
# server-side, so prefetch is a SINGLE call. The per-attempt cap and the
# one-retry-for-connection-class-errors rule are kept from the corpus2skill
# design.
_PREFETCH_BUDGET_SECONDS = 7.0
_PREFETCH_ATTEMPT_TIMEOUT_SECONDS = 4.0
_PREFETCH_HEALTH_TIMEOUT_SECONDS = 2.0
# Automatic (every-turn) recall stays tight to keep prompts small: same
# choices as the Windows-native amadeus_prefetch hook (AmadeuS doc/10 §2.2:
# per_device_limit=3, snippet_chars=300).
_PREFETCH_DEVICE_LIMIT = 3
_PREFETCH_SNIPPET_CHARS = 300
# Agent-initiated explicit searches may afford more results.
_DEFAULT_TOOL_LIMIT = 10
# on_session_end() episode record: fixed subject/namespace (task brief).
_EPISODE_SUBJECT_SESSION_END = "session_end"
_EPISODE_NAMESPACE = "hh-agent-dashboard"

# get_secret() (agent/secret_scope.py) resolves an env var honoring Hermes'
# per-profile secret scoping (multiplexed gateway sessions, etc.), which is
# the correct thing to use here. Fall back to plain os.environ if the
# Hermes core isn't importable, for the same isolation reason as the
# MemoryProvider import above. Same fail-closed pattern as corpus2skill.
try:
    from agent.secret_scope import get_secret as _get_secret
except Exception:  # pragma: no cover - only exercised outside a real Hermes checkout
    _get_secret = None


def _get_api_key() -> str:
    # Fail-closed contract (identical reasoning to the corpus2skill plugin,
    # Codex review 2026-08-15 Critical): if the real scoping module is
    # importable, its answer (including "empty" or "raised -> not
    # configured") is final; only when agent.secret_scope itself isn't
    # importable at all is os.environ the correct fallback source of truth.
    if _get_secret is not None:
        try:
            value = _get_secret(_ENV_API_KEY, "")
        except Exception:
            logger.debug(
                "AmadeusRemote: get_secret() raised, treating as not configured",
                exc_info=True,
            )
            return ""
        return str(value).strip() if value else ""
    return os.environ.get(_ENV_API_KEY, "").strip()


def _config_path(hermes_home: str) -> Path:
    return Path(hermes_home) / _CONFIG_FILENAME


def _load_config(hermes_home: str) -> dict:
    """Read the non-secret config file. Missing/corrupt file -> defaults."""
    if not hermes_home:
        return {}
    path = _config_path(hermes_home)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.debug("AmadeusRemote: failed to parse %s", path, exc_info=True)
        return {}


def _resolve_base_url(config: dict) -> str:
    raw = str((config or {}).get("base_url") or "").strip()
    return (raw or _DEFAULT_BASE_URL).rstrip("/") or _DEFAULT_BASE_URL


def _is_retryable_prefetch_error(exc: BaseException) -> bool:
    """Connection-class failures worth one cold-start retry.

    Modal cold starts surface as exactly these errors: URLError covers DNS
    failures / refused / reset connections, TimeoutError covers connect and
    read timeouts (``socket.timeout`` is a TimeoutError alias on 3.10+),
    and HTTP 502/503/504 are the proxy/cold-start window. Everything else
    (4xx — including the 401 AmadeuS-Remote returns for a missing/wrong
    key — malformed JSON, ...) is permanent and must not burn budget.
    Note: ``urllib.error.HTTPError`` subclasses ``URLError``, so HTTPError
    must be checked first or 4xx would wrongly count as retryable.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (502, 503, 504)
    return isinstance(exc, (urllib.error.URLError, TimeoutError))


# ---------------------------------------------------------------------------
# Response shape helpers
#
# /api/query returns the AmadeuS memory_query() output: a dict with
# routing / evidence / devices / truncated keys. Each evidence entry is a
# 6-key dict: layer, path_or_id, title, snippet, score, retrieved_at.
# These helpers are deliberately permissive about the shape (bare list or
# a dict missing "evidence", entries missing fields) so an unrecognized
# body degrades to an empty result rather than raising.
# ---------------------------------------------------------------------------


def _extract_evidence(raw: Any) -> List[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        value = raw.get("evidence")
        if isinstance(value, list):
            return value
    return []


def _render_evidence(raw: Any) -> str:
    """Format the evidence[] of one /api/query response as bullet lines.

    Each line is ``- [<layer>] <title or path_or_id>: <snippet>`` — the
    same rendering the Windows-native amadeus_prefetch hook produces
    (amadeus_hooks/amadeus_prefetch.py render_evidence), so both Hermes
    instances inject the same shape. Entries without a label AND without a
    snippet are skipped; an empty result degrades to "".
    """
    lines: List[str] = []
    for item in _extract_evidence(raw):
        if not isinstance(item, dict):
            continue
        layer = item.get("layer")
        if not isinstance(layer, str) or not layer:
            layer = "?"
        title = item.get("title")
        label = title if isinstance(title, str) and title else item.get("path_or_id")
        label = label if isinstance(label, str) and label else ""
        snippet = item.get("snippet")
        snippet = snippet if isinstance(snippet, str) else ""
        if not label and not snippet:
            continue
        if snippet:
            lines.append(f"- [{layer}] {label}: {snippet}")
        else:
            lines.append(f"- [{layer}] {label}")
    return "\n".join(lines)


def _tool_error(message: str) -> str:
    return json.dumps({"error": str(message)}, ensure_ascii=False)


def _summarize_session(messages: List[Dict[str, Any]]) -> str:
    """Compact session-end summary used as the episode content.

    Mirrors the role of amadeus_session_end's build_content(): produces a
    short, deterministic record of how the session ended. content must be
    a non-empty str for route_write's episode validation.
    """
    msgs = list(messages or [])
    if not msgs:
        return "Session ended (no messages recorded)."

    def _text(m: Any) -> str:
        if not isinstance(m, dict):
            return str(m)
        content = m.get("content")
        if isinstance(content, list):  # OpenAI-style content blocks
            content = " ".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("text")
            )
        return str(content or "").strip().replace("\n", " ")

    last_user = next((_text(m) for m in reversed(msgs) if m.get("role") == "user"), "")
    last_assistant = next(
        (_text(m) for m in reversed(msgs) if m.get("role") == "assistant"), ""
    )
    lines = [f"Session ended after {len(msgs)} message(s)."]
    if last_user:
        lines.append(f"last user: {last_user[:200]}")
    if last_assistant:
        lines.append(f"last assistant: {last_assistant[:200]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP client — stdlib urllib only, plain HTTPS REST (no MCP client).
# ---------------------------------------------------------------------------


class _AmadeusRemoteClient:
    """Thin synchronous REST client for the AmadeuS-Remote backend.

    Every method raises on failure (HTTP error, timeout, malformed JSON) —
    it does NOT swallow errors itself. Fail-soft behavior is the caller's
    responsibility (see prefetch()/sync_turn()/handle_tool_call()/
    on_session_end() below), same contract as the corpus2skill plugin's
    client class.
    """

    def __init__(self, api_key: str, base_url: str, timeout: float = _DEFAULT_TIMEOUT_SECONDS):
        self._api_key = api_key
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                 payload: Optional[dict] = None, timeout: Optional[float] = None,
                 auth: bool = True) -> dict:
        url = f"{self._base_url}{path}"
        if params:
            query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
            if query:
                url = f"{url}?{query}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = self._headers() if auth else {"Accept": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout if timeout is not None else self._timeout) as resp:
            body = resp.read()
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def health(self, *, timeout: Optional[float] = None) -> dict:
        """Unauthenticated liveness probe: GET /health (warm/cold diagnostic).

        Called only after a total prefetch failure to tell a Modal cold
        start (backend warming up) apart from a hard outage in the logs.
        """
        return self._request("GET", "/health", timeout=timeout, auth=False)

    def query(self, question: str, *, project: Optional[str] = None,
              per_device_limit: Optional[int] = None,
              snippet_chars: Optional[int] = None,
              timeout: Optional[float] = None) -> dict:
        """Unified recall across NCAM + Corpus2Skill: GET /api/query."""
        return self._request(
            "GET", "/api/query",
            params={
                "question": question,
                "project": project,
                "per_device_limit": per_device_limit,
                "snippet_chars": snippet_chars,
            },
            timeout=timeout,
        )

    def journal_write(self, session_id: str, role: str, content: str, turn_index: int) -> dict:
        """Per-turn journal entry: POST /api/write (write_type=journal).

        apply=True: automatic background recorders write for real (see the
        module docstring; the Windows amadeus_hooks do the same). NEVER
        exposed as an agent tool.
        """
        return self._request(
            "POST", "/api/write",
            payload={
                "write_type": "journal",
                "content": content,
                "apply": True,
                "session_id": session_id,
                "role": role,
                "turn_index": turn_index,
            },
        )

    def episode_write(self, session_id: str, role: str, content: str,
                      subject: str, namespace: str) -> dict:
        """Session-boundary episode record: POST /api/write (write_type=episode).

        apply=True for the same reason as journal_write(). NEVER exposed
        as an agent tool.
        """
        return self._request(
            "POST", "/api/write",
            payload={
                "write_type": "episode",
                "content": content,
                "apply": True,
                "session_id": session_id,
                "role": role,
                "subject": subject,
                "namespace": namespace,
            },
        )


AMADEUS_QUERY_TOOL_SCHEMA = {
    "name": "amadeus_query",
    "description": (
        "Query AmadeuS, an integrated memory gateway that automatically "
        "routes the question across NCAM and Corpus2Skill, for relevant "
        "past context (conversations, decisions, work artifacts, project "
        "state, concepts). The routing decides which device(s) to search; "
        "read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look up."},
            "project": {
                "type": "string",
                "description": "Optional project name to scope the search to.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results per device (default 10).",
            },
        },
        "required": ["query"],
    },
}


class AmadeusRemoteMemoryProvider(MemoryProvider):
    """Memory provider backed by the deployed AmadeuS-Remote Modal REST API.

    Complement to (not a replacement of) the corpus2skill provider: both
    plugins can coexist, and switching ``memory.provider`` is a separate,
    later task.
    """

    def __init__(self) -> None:
        self._api_key = ""
        self._base_url = _DEFAULT_BASE_URL
        self._session_id = ""
        self._hermes_home = ""
        self._client: Optional[_AmadeusRemoteClient] = None
        self._active = False
        # Single long-lived worker consuming a FIFO queue — identical design
        # to the corpus2skill plugin (Codex review 2026-08-15 High): a
        # per-call daemon thread with join-timeouts could reorder writes
        # across turns (turn N+1 landing before turn N) and lose shutdown()
        # durability. sync_turn() only ever enqueues (true non-blocking),
        # and exactly one worker thread ever exists.
        self._sync_queue: "queue.Queue[Optional[tuple[str, str, str, int]]]" = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self._turn_index = 0
        self._turn_index_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "amadeus_remote"

    # -- Core lifecycle -------------------------------------------------

    def is_available(self) -> bool:
        # Presence check ONLY. NO network calls — required by the
        # MemoryProvider contract (called during agent init to decide
        # whether to activate the provider at all).
        return bool(_get_api_key())

    def initialize(self, session_id: str, **kwargs) -> None:
        # Profile isolation: always use the hermes_home kwarg Hermes passes
        # in, never a hardcoded ~/.hermes path (developer-guide "Profile
        # Isolation").
        self._hermes_home = str(kwargs.get("hermes_home") or "")
        self._session_id = session_id or ""
        self._turn_index = 0

        self._api_key = _get_api_key()
        config = _load_config(self._hermes_home)
        self._base_url = _resolve_base_url(config)

        self._active = bool(self._api_key)
        self._client = _AmadeusRemoteClient(self._api_key, self._base_url) if self._active else None

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        # session_id reassigned mid-process (/resume, /branch, /new, ...):
        # rebind per-session state. The turn counter MUST reset here —
        # AmadeuS's journal idempotency key is
        # journal.s{session_id}.t{turn_index}, so a stale counter would
        # make the first write of the new session collide with (or skip
        # past) the old session's entries. The corpus2skill plugin has the
        # same hook; it is load-bearing for journal turn_index management.
        self._session_id = str(new_session_id or "") or self._session_id
        with self._turn_index_lock:
            self._turn_index = 0

    def system_prompt_block(self) -> str:
        if not self._active:
            return ""
        return (
            "# AmadeuS\n"
            "You have access to AmadeuS, an integrated memory gateway that "
            "routes queries across NCAM and Corpus2Skill. Relevant memories "
            "are recalled automatically each turn; use amadeus_query to look "
            "something up explicitly when needed."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._active or self._client is None or not query or not query.strip():
            return ""
        # Single call under a TOTAL 7.0s deadline (MemoryManager abandons
        # external prefetch after 8s). /api/query fans out across NCAM and
        # Corpus2Skill server-side, so — unlike corpus2skill — there are no
        # lanes to split the budget across; connection-class errors still
        # get ONE retry within the remaining budget (the Modal cold-start
        # rescue).
        start = time.monotonic()
        deadline = start + _PREFETCH_BUDGET_SECONDS

        text, backend_ok = self._prefetch_query(deadline, query.strip())
        if not text:
            # Probe only when the backend itself failed (cold start vs hard
            # outage diagnostic). A 200 with empty evidence is a healthy
            # "nothing relevant in memory" and must not spam the logs.
            if not backend_ok:
                self._probe_health(deadline)
            return ""

        logger.debug(
            "AmadeusRemote prefetch: returned in %.0f ms",
            (time.monotonic() - start) * 1000.0,
        )
        return "<amadeus-context>\n" + text + "\n</amadeus-context>"

    def _prefetch_query(self, deadline: float, question: str) -> tuple:
        """One budget-bounded /api/query under the shared deadline; fail-soft.

        Returns ``(rendered_text, backend_ok)`` — ``backend_ok`` is False
        only when the backend call failed (all attempts), which is the
        signal for the cold-start health probe. Connection-class failures
        (see ``_is_retryable_prefetch_error``) get ONE retry within the
        remaining budget — Modal cold starts surface as exactly those
        errors. Everything else is permanent and returns immediately. Any
        failure returns ("", False) so a hung or broken backend can never
        stall the turn (context loss is acceptable).
        """
        client = self._client
        if client is None:
            return "", False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "", False

        def _attempt(timeout: float) -> dict:
            return client.query(
                question,
                per_device_limit=_PREFETCH_DEVICE_LIMIT,
                snippet_chars=_PREFETCH_SNIPPET_CHARS,
                timeout=timeout,
            )

        timeout = min(_PREFETCH_ATTEMPT_TIMEOUT_SECONDS, remaining)
        try:
            raw = _attempt(timeout)
        except Exception as exc:
            if not _is_retryable_prefetch_error(exc):
                logger.warning(
                    "AmadeusRemote prefetch: /api/query failed", exc_info=True
                )
                return "", False

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "AmadeusRemote prefetch: /api/query failed (no retry budget left)",
                    exc_info=True,
                )
                return "", False
            logger.info("AmadeusRemote prefetch: cold-start retry (attempt 2)")
            try:
                raw = _attempt(min(_PREFETCH_ATTEMPT_TIMEOUT_SECONDS, remaining))
            except Exception:
                logger.warning(
                    "AmadeusRemote prefetch: /api/query failed", exc_info=True
                )
                return "", False
        return _render_evidence(raw), True

    def _probe_health(self, deadline: float) -> None:
        """One budget-bounded GET /health probe after a total prefetch failure.

        Diagnostic aid (same role as corpus2skill's _probe_health):
        distinguishes a Modal cold start (probe succeeds, possibly slowly,
        or itself times out) from a hard outage. Never pushes prefetch
        past the deadline.
        """
        if self._client is None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            health = self._client.health(
                timeout=min(_PREFETCH_HEALTH_TIMEOUT_SECONDS, remaining)
            )
            status = health.get("status") if isinstance(health, dict) else health
            logger.warning(
                "AmadeusRemote prefetch: backend health probe -> %r "
                "(no context returned; warm/cold diagnostic)",
                status,
            )
        except Exception:
            logger.warning(
                "AmadeusRemote prefetch: backend health probe failed "
                "(backend likely cold or unreachable)",
                exc_info=True,
            )

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._worker_thread = threading.Thread(
                target=self._worker_loop, daemon=True, name="amadeus-remote-sync-worker"
            )
            self._worker_thread.start()

    def _worker_loop(self) -> None:
        while True:
            item = self._sync_queue.get()
            try:
                if item is None:  # shutdown sentinel
                    return
                sid, role, content, turn_index = item
                client = self._client
                if client is None:
                    continue
                try:
                    client.journal_write(sid, role, content, turn_index)
                except Exception:
                    logger.warning(
                        "AmadeusRemote sync_turn: journal_write(%s) failed", role, exc_info=True
                    )
            finally:
                self._sync_queue.task_done()

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        # MUST be non-blocking (developer-guide "Threading Contract"):
        # sync_turn() only ever enqueues onto self._sync_queue and returns.
        # A single persistent worker (see _worker_loop) drains the queue
        # FIFO, so writes for this turn are strictly ordered relative to
        # every other turn regardless of backend latency.
        if not self._active or self._client is None:
            return

        sid = session_id or self._session_id
        with self._turn_index_lock:
            turn_index = self._turn_index
            self._turn_index += 1

        self._ensure_worker()
        if user_content and user_content.strip():
            self._sync_queue.put((sid, "user", user_content, turn_index))
        if assistant_content and assistant_content.strip():
            self._sync_queue.put((sid, "assistant", assistant_content, turn_index))

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # One session-end episode record — write_type=episode, role system,
        # subject session_end, namespace hh-agent-dashboard (task brief).
        # Unlike corpus2skill's v1 no-op, this IS implemented, to match the
        # Windows-native amadeus_hooks session-end hook (AmadeuS Phase 4
        # cutover records session boundaries as episodes via
        # route_write(apply=True)).
        #
        # Not required to be non-blocking (not under the threading
        # contract), but must never raise: failures are logged and the
        # session boundary is simply not recorded.
        if not self._active or self._client is None:
            return
        sid = self._session_id
        if not sid:  # no session context -> nothing to attribute the record to
            return
        content = _summarize_session(messages)
        try:
            self._client.episode_write(
                sid,
                "system",
                content,
                subject=_EPISODE_SUBJECT_SESSION_END,
                namespace=_EPISODE_NAMESPACE,
            )
        except Exception:
            logger.warning(
                "AmadeusRemote on_session_end: episode_write(session_end) failed",
                exc_info=True,
            )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Deliberately exposes ONLY amadeus_query (read-only). Anything
        # write-capable (journal/episode/...) must NEVER appear here — see
        # docs/hh-agent/03_Architecture.md §13 M-07 and
        # test_get_tool_schemas_excludes_write_tools below (BUG-5 style
        # regression guard).
        #
        # NOT gated on self._active: MemoryManager.add_provider() calls
        # get_tool_schemas() to build its tool-name routing table at
        # registration time, BEFORE initialize_all() ever runs — so
        # self._active (set in initialize()) is still False at that point.
        # The "not configured" case is instead handled inside
        # handle_tool_call(), which is the actual point of use.
        return [AMADEUS_QUERY_TOOL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "amadeus_query":
            return _tool_error(f"Unknown tool: {tool_name}")
        if not self._active or self._client is None:
            return _tool_error("AmadeuS-Remote is not configured")

        query = str((args or {}).get("query") or "").strip()
        if not query:
            return _tool_error("query is required")
        project = str((args or {}).get("project") or "").strip() or None
        try:
            limit = max(1, min(50, int((args or {}).get("limit", _DEFAULT_TOOL_LIMIT) or _DEFAULT_TOOL_LIMIT)))
        except Exception:
            limit = _DEFAULT_TOOL_LIMIT

        try:
            raw = self._client.query(query, project=project, per_device_limit=limit)
        except Exception as exc:
            logger.warning("AmadeusRemote amadeus_query tool call failed", exc_info=True)
            return _tool_error(f"AmadeusRemote query failed: {exc}")
        return json.dumps(raw, ensure_ascii=False)

    def shutdown(self) -> None:
        thread = self._worker_thread
        if thread and thread.is_alive():
            self._sync_queue.put(None)  # sentinel: drain remaining items, then exit
            thread.join(timeout=10.0)
        self._worker_thread = None

    # -- Config -----------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        # Minimal schema on purpose (see memory-provider-plugin.md "Minimal
        # vs Full Schema"): every field returned here is prompted during
        # `hermes memory setup`, so only api_key (the one thing users MUST
        # configure) goes here. base_url is an advanced, file-only setting
        # (see save_config()/README "Configuration") — same design as the
        # corpus2skill plugin.
        return [
            {
                "key": "api_key",
                "description": "AmadeuS-Remote API key (Bearer token for the Modal-hosted REST API)",
                "secret": True,
                "required": True,
                "env_var": _ENV_API_KEY,
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        # Only non-secret values reach here — api_key is `secret: True` and
        # is written to .env by the Hermes config framework before this is
        # called, never passed in `values`.
        base_url = str((values or {}).get("base_url") or "").strip() or _DEFAULT_BASE_URL
        path = _config_path(hermes_home)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"base_url": base_url}, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            logger.warning("AmadeusRemote: failed to write %s", path, exc_info=True)


def register(ctx) -> None:
    """Plugin entry point, called by Hermes' memory plugin discovery system."""
    ctx.register_memory_provider(AmadeusRemoteMemoryProvider())
