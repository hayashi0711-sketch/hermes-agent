"""Corpus2Skill memory provider plugin for Hermes Agent.

Bridges Windows-native Hermes and the Modal-hosted Hermes (Phase1c dashboard)
onto a single shared memory backend: the existing Corpus2Skill Modal app
(https://hayashi0711--corpus2skill-serve.modal.run), reused as a Hermes
"Memory Provider" (see the ``MemoryProvider`` ABC in ``agent/memory_provider.py``
and website/docs/developer-guide/memory-provider-plugin.md).

Design contract for this file: docs/hh-agent/03_Architecture.md §13
(M-05, M-06, M-07). That section is the single source of truth for what this
plugin must/must not do; do not "improve" behavior here without updating it
there first.

Key decisions baked into this implementation (see §13 for the "why"):

- **Location**: this lives under ``./.hermes/plugins/corpus2skill/`` (a
  "Project Provider" per Hermes' plugin discovery order). The bundled
  ``plugins/memory/`` tree is upstream-reserved for new providers and is
  never touched by this plugin (M-05).
- **Transport**: plain HTTPS REST over the stdlib ``urllib`` — no MCP
  protocol client, no third-party HTTP dependency. Corpus2Skill exposes a
  REST fallback alongside its MCP tools for exactly this purpose.
- **Read-only tool surface**: the ONLY tool this provider exposes to the
  agent is ``corpus2skill_search`` (a thin wrapper over ``GET /api/search``,
  Lane A). Nothing that can write to Corpus2Skill (journal writes, "add new
  memory") is ever exposed as an agent-callable tool — see
  ``get_tool_schemas()`` below and the regression test that asserts this.
- **Fail-soft**: ``prefetch()`` swallows backend errors and returns an empty
  string (context loss is acceptable; a hung/broken turn is not).
  ``sync_turn()`` runs in a daemon thread per the developer-guide's
  threading contract and only ever logs failures — it must never raise back
  into the agent's turn loop.
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
# static-analysis pass over just this directory. See task brief: "mypyでの
# 型チェック用にTYPE_CHECKINGでラップするか...テストではagentモジュールを
# スキップして直接インスタンスのメソッドだけをテストする方式で構わない".
try:
    from agent.memory_provider import MemoryProvider
except Exception:  # pragma: no cover - only exercised outside a real Hermes checkout
    class MemoryProvider:  # type: ignore[no-redef]
        """Fallback stand-in used only when the real Hermes core is unavailable."""


logger = logging.getLogger(__name__)

_ENV_API_KEY = "CORPUS2SKILL_API_KEY"
_DEFAULT_BASE_URL = "https://hayashi0711--corpus2skill-serve.modal.run"
# Grand design 2026-09-01 (08_Architecture_Design.md §3, T1.2 / D2): 5s ->
# 15s. Modal cold starts routinely exceed 5s (observed 8/30 and 9/1:
# prefetch warnings while a direct curl right after returned 200). Explicit
# corpus2skill_search / journal_write calls now tolerate a cold start.
# NOTE: this does NOT govern prefetch(), which keeps its own tighter
# deadline budget below to stay under MemoryManager's 8s external-prefetch
# abandonment window.
_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_SEARCH_LIMIT = 10
_CONFIG_FILENAME = "corpus2skill.json"
# Grand design 2026-09-01 (§3.3, D2): prefetch() runs under a TOTAL 7.0s
# deadline (MemoryManager abandons external prefetch after 8s, so a genuine
# cold start must never stall the turn past that window). Each lane attempt
# is capped at 4.0s, and connection-class errors (URLError / TimeoutError /
# HTTP 502-503-504) get exactly ONE retry within the remaining budget — the
# Modal cold-start rescue. Explicit tool/journal calls are NOT governed by
# this budget; they use _DEFAULT_TIMEOUT_SECONDS (15s) instead.
_PREFETCH_BUDGET_SECONDS = 7.0
_PREFETCH_LANE_TIMEOUT_SECONDS = 4.0
_PREFETCH_HEALTH_TIMEOUT_SECONDS = 2.0

# get_secret() (agent/secret_scope.py) resolves an env var honoring Hermes'
# per-profile secret scoping (multiplexed gateway sessions, etc.), which is
# the correct thing to use here. Fall back to plain os.environ if the
# Hermes core isn't importable, for the same isolation reason as above.
try:
    from agent.secret_scope import get_secret as _get_secret
except Exception:  # pragma: no cover - only exercised outside a real Hermes checkout
    _get_secret = None


def _get_api_key() -> str:
    # Codex review (2026-08-15, Critical): the previous version fell back to
    # plain os.environ whenever get_secret() returned empty or raised. That
    # defeats agent.secret_scope's fail-closed contract -- in multiplex mode
    # a profile with no CORPUS2SKILL_API_KEY of its own would silently
    # inherit another profile's process-global env var, leaking that
    # profile's memories and journaling its own turns into the wrong
    # backend. If the real scoping module is importable, its answer
    # (including "empty" or "raised -> not configured") is final; only when
    # agent.secret_scope itself isn't importable at all (no scope system
    # exists to violate -- e.g. this module imported outside a real Hermes
    # checkout for a bare static-analysis pass) is os.environ the correct
    # fallback source of truth.
    if _get_secret is not None:
        try:
            value = _get_secret(_ENV_API_KEY, "")
        except Exception:
            logger.debug(
                "Corpus2Skill: get_secret() raised, treating as not configured",
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
        logger.debug("Corpus2Skill: failed to parse %s", path, exc_info=True)
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
    (4xx, malformed JSON, ...) is permanent and must not burn budget.
    Note: ``urllib.error.HTTPError`` subclasses ``URLError``, so HTTPError
    must be checked first or 4xx would wrongly count as retryable.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (502, 503, 504)
    return isinstance(exc, (urllib.error.URLError, TimeoutError))


# ---------------------------------------------------------------------------
# Response shape helpers
#
# /api/search, /api/journal/write, /api/journal/recall are being added in
# parallel by a different task (see task brief) and did not exist yet at the
# time this plugin was written. These helpers are deliberately permissive
# about the response shape so a reasonable real-world JSON body (a bare
# list, or a dict with a "results"/"items"/... list, of either strings or
# dicts with a "content"-ish field) formats sensibly without this plugin
# needing to change once the real endpoints land. Anything unrecognized
# degrades to an empty result rather than raising.
# ---------------------------------------------------------------------------


def _extract_items(raw: Any) -> List[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("results", "items", "matches", "memories", "entries"):
            value = raw.get(key)
            if isinstance(value, list):
                return value
    return []


def _stringify_item(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("content", "text", "summary", "snippet", "memory"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                label = item.get("title") or item.get("path") or item.get("name")
                return f"[{label}] {value.strip()}" if label else value.strip()
        try:
            return json.dumps(item, ensure_ascii=False)
        except Exception:
            return str(item)
    return str(item)


def _format_results(raw: Any, *, heading: str) -> str:
    lines = [f"- {text}" for text in (_stringify_item(item) for item in _extract_items(raw)) if text]
    if not lines:
        return ""
    return f"## {heading}\n" + "\n".join(lines)


def _results_as_json(raw: Any) -> str:
    items = _extract_items(raw)
    return json.dumps({"results": items, "count": len(items)}, ensure_ascii=False)


def _tool_error(message: str) -> str:
    return json.dumps({"error": str(message)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# HTTP client — stdlib urllib only, plain HTTPS REST (no MCP client).
# ---------------------------------------------------------------------------


class _Corpus2SkillClient:
    """Thin synchronous REST client for the Corpus2Skill backend.

    Every method raises on failure (HTTP error, timeout, malformed JSON) —
    it does NOT swallow errors itself. Fail-soft behavior is the caller's
    responsibility (see prefetch()/sync_turn()/handle_tool_call() below),
    per the task brief: "httpx等でHTTPエラーを握りつぶさず例外として扱い".
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

    def search(self, query: str, limit: int = _DEFAULT_SEARCH_LIMIT, *, timeout: Optional[float] = None) -> dict:
        """Lane A (classified long-term memory): GET /api/search."""
        return self._request(
            "GET", "/api/search", params={"query": query, "limit": limit}, timeout=timeout
        )

    def journal_recall(
        self, session_id: str, query: str, limit: int = _DEFAULT_SEARCH_LIMIT, *, timeout: Optional[float] = None
    ) -> dict:
        """Lane B (raw session journal): GET /api/journal/recall."""
        return self._request(
            "GET", "/api/journal/recall",
            params={"session_id": session_id, "query": query, "limit": limit},
            timeout=timeout,
        )

    def journal_write(self, session_id: str, role: str, content: str, turn_index: int) -> dict:
        """Lane B write: POST /api/journal/write. NEVER exposed as an agent tool."""
        return self._request(
            "POST", "/api/journal/write",
            payload={"session_id": session_id, "role": role, "content": content, "turn_index": turn_index},
        )


SEARCH_TOOL_SCHEMA = {
    "name": "corpus2skill_search",
    "description": (
        "Search Corpus2Skill, a persistent external memory shared across every "
        "Hermes instance (this machine and the Modal-hosted dashboard), for "
        "previously learned facts and skills relevant to the query. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {"type": "integer", "description": "Maximum results to return (default 10)."},
        },
        "required": ["query"],
    },
}


class Corpus2SkillMemoryProvider(MemoryProvider):
    """Memory provider backed by the existing Corpus2Skill Modal REST API."""

    def __init__(self) -> None:
        self._api_key = ""
        self._base_url = _DEFAULT_BASE_URL
        self._session_id = ""
        self._hermes_home = ""
        self._client: Optional[_Corpus2SkillClient] = None
        self._active = False
        # Codex review (2026-08-15, High): the original design spawned one
        # daemon thread per sync_turn() call and joined the previous one
        # with a 5s timeout before starting the next. Two sequential
        # 5s-timeout writes per turn means a thread can stay alive for ~10s,
        # so a fast follow-up turn could time out the join, leave the old
        # thread untracked, and start a second thread concurrently --
        # reordering writes (turn N+1 landing before turn N) and losing
        # shutdown() durability for the orphaned thread. A single
        # long-lived worker consuming a FIFO queue makes ordering and
        # tracking structural instead of timing-dependent: sync_turn() only
        # ever enqueues (true non-blocking), and exactly one worker thread
        # ever exists.
        self._sync_queue: "queue.Queue[Optional[tuple[str, str, str, int]]]" = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self._turn_index = 0
        self._turn_index_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "corpus2skill"

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
        self._client = _Corpus2SkillClient(self._api_key, self._base_url) if self._active else None

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = str(new_session_id or "") or self._session_id
        with self._turn_index_lock:
            self._turn_index = 0

    def system_prompt_block(self) -> str:
        if not self._active:
            return ""
        return (
            "# Corpus2Skill\n"
            "You have access to Corpus2Skill, a persistent external memory shared "
            "across every Hermes instance you run as (this machine and the "
            "Modal-hosted cloud dashboard). Relevant memories are recalled "
            "automatically each turn; use corpus2skill_search to look something "
            "up explicitly when needed."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._active or self._client is None or not query or not query.strip():
            return ""
        sid = session_id or self._session_id
        sections: List[str] = []
        # Grand design 2026-09-01 (§3.3, D2): total 7.0s deadline shared by
        # both lanes AND the health probe. MemoryManager abandons external
        # prefetch after 8s, so this method must never exceed the deadline —
        # remaining <= 0 skips a lane rather than stalling the turn.
        start = time.monotonic()
        deadline = start + _PREFETCH_BUDGET_SECONDS

        text = self._prefetch_lane(
            lambda timeout: self._client.search(
                query, limit=_DEFAULT_SEARCH_LIMIT, timeout=timeout
            ),
            heading="Corpus2Skill — long-term memory",
            lane_label="A search",
            deadline=deadline,
        )
        if text:
            sections.append(text)

        text = self._prefetch_lane(
            lambda timeout: self._client.journal_recall(
                sid, query, limit=_DEFAULT_SEARCH_LIMIT, timeout=timeout
            ),
            heading="Corpus2Skill — recent session journal",
            lane_label="B journal recall",
            deadline=deadline,
        )
        if text:
            sections.append(text)

        if not sections:
            self._probe_health(deadline)
            return ""

        logger.debug(
            "Corpus2Skill prefetch: returned in %.0f ms",
            (time.monotonic() - start) * 1000.0,
        )
        return "<corpus2skill-context>\n" + "\n\n".join(sections) + "\n</corpus2skill-context>"

    def _prefetch_lane(
        self,
        call: Any,
        *,
        heading: str,
        lane_label: str,
        deadline: float,
    ) -> str:
        """Run one prefetch lane under the shared deadline; fail-soft.

        ``call(timeout)`` performs the backend request with the given
        per-attempt timeout. Connection-class failures (see
        ``_is_retryable_prefetch_error``) get ONE retry within the remaining
        budget — Modal cold starts surface as exactly those errors, and the
        retry is the cold-start rescue. Everything else is permanent and
        returns immediately. Any failure returns "" so a hung or broken
        backend can never stall the turn (context loss is acceptable).
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        timeout = min(_PREFETCH_LANE_TIMEOUT_SECONDS, remaining)
        try:
            raw = call(timeout)
        except Exception as exc:
            if not _is_retryable_prefetch_error(exc):
                logger.warning(
                    "Corpus2Skill prefetch: Lane %s failed", lane_label, exc_info=True
                )
                return ""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "Corpus2Skill prefetch: Lane %s failed (no retry budget left)",
                    lane_label,
                    exc_info=True,
                )
                return ""
            logger.info("Corpus2Skill prefetch: cold-start retry (attempt 2)")
            try:
                raw = call(min(_PREFETCH_LANE_TIMEOUT_SECONDS, remaining))
            except Exception:
                logger.warning(
                    "Corpus2Skill prefetch: Lane %s failed", lane_label, exc_info=True
                )
                return ""
        return _format_results(raw, heading=heading)

    def _probe_health(self, deadline: float) -> None:
        """One budget-bounded GET /health probe after a total prefetch failure.

        Diagnostic aid (grand design §3.3 item 3): distinguishes a Modal
        cold start (probe succeeds, possibly slowly, or itself times out)
        from a hard outage. Never pushes prefetch past the deadline.
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
                "Corpus2Skill prefetch: backend health probe -> %r "
                "(all lanes failed; warm/cold diagnostic)",
                status,
            )
        except Exception:
            logger.warning(
                "Corpus2Skill prefetch: backend health probe failed "
                "(backend likely cold or unreachable)",
                exc_info=True,
            )

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._worker_thread = threading.Thread(
                target=self._worker_loop, daemon=True, name="corpus2skill-sync-worker"
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
                        "Corpus2Skill sync_turn: journal_write(%s) failed", role, exc_info=True
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
        # every other turn regardless of backend latency, and there is
        # never more than one in-flight write thread to lose track of.
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
        # v1: intentionally a no-op. Promoting Lane B (raw session journal)
        # entries into Lane A (curated long-term memory) is out of scope for
        # this provider — see Corpus2Skill/doc/03_Architecture.md §12
        # (unresolved item D) and this repo's docs/hh-agent/03_Architecture.md
        # §13 M-06. sync_turn() already journals every turn as it happens;
        # there is nothing additional to do at the session boundary in v1.
        return

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Deliberately exposes ONLY corpus2skill_search. Anything
        # write-capable (a journal_write or add_new_memory equivalent) must
        # NEVER appear here — see docs/hh-agent/03_Architecture.md §13 M-07
        # and test_get_tool_schemas_excludes_write_tools below (BUG-5 style
        # regression guard).
        #
        # NOT gated on self._active: MemoryManager.add_provider() calls
        # get_tool_schemas() to build its tool-name routing table at
        # registration time, BEFORE initialize_all() ever runs — so
        # self._active (set in initialize()) is still False at that point.
        # Gating here would silently register zero tools under the
        # documented add_provider() -> initialize_all() usage order. The
        # "not configured" case is instead handled inside
        # handle_tool_call(), which is the actual point of use.
        return [SEARCH_TOOL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "corpus2skill_search":
            return _tool_error(f"Unknown tool: {tool_name}")
        if not self._active or self._client is None:
            return _tool_error("Corpus2Skill is not configured")

        query = str((args or {}).get("query") or "").strip()
        if not query:
            return _tool_error("query is required")
        try:
            limit = max(1, min(50, int((args or {}).get("limit", _DEFAULT_SEARCH_LIMIT) or _DEFAULT_SEARCH_LIMIT)))
        except Exception:
            limit = _DEFAULT_SEARCH_LIMIT

        try:
            raw = self._client.search(query, limit=limit)
        except Exception as exc:
            logger.warning("Corpus2Skill corpus2skill_search tool call failed", exc_info=True)
            return _tool_error(f"Corpus2Skill search failed: {exc}")
        return _results_as_json(raw)

    def shutdown(self) -> None:
        thread = self._worker_thread
        if thread and thread.is_alive():
            self._sync_queue.put(None)  # sentinel: drain remaining items, then exit
            thread.join(timeout=10.0)
        self._worker_thread = None

    # -- Config -----------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        # Minimal schema on purpose (see memory-provider-plugin.md "Minimal
        # vs Full Schema" and the Supermemory provider it points to as the
        # reference example): every field returned here is prompted during
        # `hermes memory setup`, so only api_key (the one thing users MUST
        # configure) goes here. base_url is documented as an advanced,
        # file-only setting (see save_config()/README "Configuration")
        # rather than added here — Codex review (2026-08-15, Low) caught
        # base_url previously being listed here too, which silently
        # contradicted this comment and the README's claim that setup only
        # prompts for the API key.
        return [
            {
                "key": "api_key",
                "description": "Corpus2Skill API key (Bearer token for the Modal-hosted REST API)",
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
            logger.warning("Corpus2Skill: failed to write %s", path, exc_info=True)


def register(ctx) -> None:
    """Plugin entry point, called by Hermes' memory plugin discovery system."""
    ctx.register_memory_provider(Corpus2SkillMemoryProvider())
