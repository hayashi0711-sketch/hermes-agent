"""Tests for the AmadeuS-Remote memory provider plugin.

The plugin lives at ``.hermes/plugins/amadeus_remote/`` — a Hermes
"Project Provider" (see docs/hh-agent/03_Architecture.md §13 M-05, which
governs the corpus2skill plugin whose structure this one mirrors). That
directory name starts with a dot, so it cannot be imported with a normal
dotted ``import`` statement. We load it the same way Hermes' own
project-plugin discovery does: by file path via importlib.

None of these tests talk to the real AmadeuS-Remote backend (or to NCAM /
Corpus2Skill behind it). The HTTP client class (``_AmadeusRemoteClient``)
is monkeypatched out everywhere so no network call is ever attempted.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import threading
import time
import urllib.error
from pathlib import Path

import pytest

_PLUGIN_PATH = (
    Path(__file__).resolve().parents[3] / ".hermes" / "plugins" / "amadeus_remote" / "__init__.py"
)


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location("amadeus_remote_plugin_under_test", _PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def plugin():
    return _load_plugin_module()


class FakeClient:
    """Stand-in for _AmadeusRemoteClient. Records calls, never touches the network."""

    def __init__(self, api_key="", base_url="", timeout=15.0):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.query_calls = []
        self.query_timeouts = []
        self.write_calls = []
        self.health_calls = []
        self.query_response = {"routing": [], "evidence": [], "devices": {}, "truncated": False}
        self.health_response = {"status": "ok"}
        self.query_exc = None
        self.write_exc = None
        self.health_exc = None
        self.write_delay = 0.0
        self.query_delay = 0.0

    def query(self, question, *, project=None, per_device_limit=None,
              snippet_chars=None, timeout=None):
        if self.query_delay:
            time.sleep(self.query_delay)
        self.query_calls.append(
            {
                "question": question,
                "project": project,
                "per_device_limit": per_device_limit,
                "snippet_chars": snippet_chars,
            }
        )
        self.query_timeouts.append(timeout)
        if self.query_exc:
            raise self.query_exc
        return self.query_response

    def journal_write(self, session_id, role, content, turn_index):
        if self.write_delay:
            time.sleep(self.write_delay)
        self.write_calls.append(
            {
                "write_type": "journal",
                "content": content,
                "apply": True,
                "session_id": session_id,
                "role": role,
                "turn_index": turn_index,
            }
        )
        if self.write_exc:
            raise self.write_exc
        return {
            "plan": [], "applied": True, "written_to": ["c2s"],
            "ids": [], "skipped": [], "rejected": [], "needs_manual_check": [],
        }

    def episode_write(self, session_id, role, content, subject, namespace):
        if self.write_delay:
            time.sleep(self.write_delay)
        self.write_calls.append(
            {
                "write_type": "episode",
                "content": content,
                "apply": True,
                "session_id": session_id,
                "role": role,
                "subject": subject,
                "namespace": namespace,
            }
        )
        if self.write_exc:
            raise self.write_exc
        return {
            "plan": [], "applied": True, "written_to": ["ncam"],
            "ids": [], "skipped": [], "rejected": [], "needs_manual_check": [],
        }

    def health(self, *, timeout=None):
        self.health_calls.append(timeout)
        if self.health_exc:
            raise self.health_exc
        return self.health_response


@pytest.fixture
def fake_client(monkeypatch, plugin):
    client = FakeClient()
    monkeypatch.setattr(plugin, "_AmadeusRemoteClient", lambda *a, **kw: client)
    return client


@pytest.fixture
def provider(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.setenv("AMADEUS_REMOTE_API_KEY", "test-token")
    p = plugin.AmadeusRemoteMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path), platform="cli")
    return p


# ---------------------------------------------------------------------------
# is_available() — env var presence only, no network
# ---------------------------------------------------------------------------


def test_is_available_true_when_key_set(monkeypatch, plugin):
    monkeypatch.setenv("AMADEUS_REMOTE_API_KEY", "some-token")
    p = plugin.AmadeusRemoteMemoryProvider()
    assert p.is_available() is True


def test_is_available_false_when_key_missing(monkeypatch, plugin):
    monkeypatch.delenv("AMADEUS_REMOTE_API_KEY", raising=False)
    p = plugin.AmadeusRemoteMemoryProvider()
    assert p.is_available() is False


def test_get_api_key_does_not_leak_to_os_environ_when_scoped_secret_is_empty(monkeypatch, plugin):
    """Fail-closed regression guard (same contract as the corpus2skill
    plugin, Codex review 2026-08-15 Critical): when the real scoped secret
    resolver says "empty" or raises, that answer is final — os.environ must
    never be consulted as a fallback (multiplex-mode profile leak).
    """
    monkeypatch.setenv("AMADEUS_REMOTE_API_KEY", "leaked-from-another-profile")
    monkeypatch.setattr(plugin, "_get_secret", lambda key, default: "")
    assert plugin._get_api_key() == ""


def test_get_api_key_does_not_leak_to_os_environ_when_scoped_secret_raises(monkeypatch, plugin):
    def _boom(key, default):
        raise RuntimeError("no scope installed")

    monkeypatch.setenv("AMADEUS_REMOTE_API_KEY", "leaked-from-another-profile")
    monkeypatch.setattr(plugin, "_get_secret", _boom)
    assert plugin._get_api_key() == ""


def test_get_api_key_uses_scoped_secret_when_present(monkeypatch, plugin):
    monkeypatch.setenv("AMADEUS_REMOTE_API_KEY", "should-be-ignored")
    monkeypatch.setattr(plugin, "_get_secret", lambda key, default: "scoped-token")
    assert plugin._get_api_key() == "scoped-token"


def test_get_api_key_falls_back_to_os_environ_only_when_secret_scope_unimportable(monkeypatch, plugin):
    """The only legitimate os.environ fallback: agent.secret_scope itself
    isn't importable at all, so there is no scope system to violate."""
    monkeypatch.setattr(plugin, "_get_secret", None)
    monkeypatch.setenv("AMADEUS_REMOTE_API_KEY", "env-token")
    assert plugin._get_api_key() == "env-token"


def test_is_available_makes_no_network_call(monkeypatch, plugin):
    """is_available() must never touch the network (MemoryProvider contract)."""
    monkeypatch.setenv("AMADEUS_REMOTE_API_KEY", "some-token")

    def _boom(*a, **kw):
        raise AssertionError("is_available() must not open a network connection")

    monkeypatch.setattr(plugin.urllib.request, "urlopen", _boom)
    p = plugin.AmadeusRemoteMemoryProvider()
    assert p.is_available() is True  # would have raised above if it tried the network


# ---------------------------------------------------------------------------
# prefetch() — one /api/query call (server-side NCAM+C2S fan-out), fails soft
# ---------------------------------------------------------------------------


def _evidence_item(layer="ncam", title="Title A", path_or_id="/x", snippet="snip A",
                   score=0.87, retrieved_at="2026-09-09T00:00:00Z"):
    return {
        "layer": layer,
        "path_or_id": path_or_id,
        "title": title,
        "snippet": snippet,
        "score": score,
        "retrieved_at": retrieved_at,
    }


def test_prefetch_calls_query_once_and_renders_evidence(provider, fake_client):
    fake_client.query_response = {
        "routing": ["ncam"],
        "evidence": [_evidence_item()],
        "devices": {"ncam": {"status": "ok"}},
        "truncated": False,
    }

    result = provider.prefetch("what do we know?")

    assert len(fake_client.query_calls) == 1
    call = fake_client.query_calls[0]
    assert call["question"] == "what do we know?"
    assert call["project"] is None
    assert call["per_device_limit"] == 3  # same tight default as the Windows prefetch hook
    assert call["snippet_chars"] == 300
    assert result.startswith("<amadeus-context>")
    assert result.endswith("</amadeus-context>")
    assert "[ncam] Title A: snip A" in result


def test_prefetch_renders_multiple_bullets_and_falls_back_to_path_or_id(provider, fake_client):
    fake_client.query_response = {
        "evidence": [
            _evidence_item(layer="ncam", title="", path_or_id="ncam/abc", snippet="s1"),
            _evidence_item(layer="c2s", title="Skill X", path_or_id="p2", snippet="s2"),
        ],
    }

    result = provider.prefetch("query")

    lines = [ln for ln in result.splitlines() if ln.startswith("- ")]
    assert lines == ["- [ncam] ncam/abc: s1", "- [c2s] Skill X: s2"]


def test_prefetch_empty_query_short_circuits(provider, fake_client):
    assert provider.prefetch("") == ""
    assert provider.prefetch("   ") == ""
    assert fake_client.query_calls == []


def test_prefetch_returns_empty_context_on_backend_failure(provider, fake_client):
    """Fail-soft: an unreachable backend must not raise or block the turn."""
    fake_client.query_exc = ConnectionError("backend down")

    result = provider.prefetch("anything")

    assert result == ""
    assert len(fake_client.health_calls) == 1  # total failure -> warm/cold diagnostic probe


def test_prefetch_success_without_evidence_does_not_probe_health(provider, fake_client):
    """A healthy 200 with no matching memory is NOT a failure — no probe."""
    fake_client.query_response = {"routing": [], "evidence": [], "devices": {}, "truncated": False}

    assert provider.prefetch("nothing relevant") == ""
    assert fake_client.health_calls == []


def test_prefetch_does_not_probe_health_when_query_succeeds(provider, fake_client):
    fake_client.query_response = {"evidence": [_evidence_item()]}

    provider.prefetch("query")

    assert fake_client.health_calls == []


def test_prefetch_inactive_provider_returns_empty(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.delenv("AMADEUS_REMOTE_API_KEY", raising=False)
    p = plugin.AmadeusRemoteMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path))
    assert p.prefetch("query") == ""
    assert fake_client.query_calls == []


# ---------------------------------------------------------------------------
# prefetch() deadline budget: total 7.0s deadline, per-attempt cap
# min(4.0, remaining), ONE retry for connection-class errors only.
# ---------------------------------------------------------------------------


def test_prefetch_retries_connection_class_error_once(provider, fake_client, caplog):
    """Cold-start rescue: TimeoutError gets exactly one retry within budget."""
    caplog.set_level(logging.INFO)
    fake_client.query_exc = TimeoutError("cold start")

    result = provider.prefetch("query")

    assert len(fake_client.query_calls) == 2  # original + 1 retry
    assert "cold-start retry (attempt 2)" in caplog.text
    assert result == ""


def test_prefetch_retries_http_502_once(provider, fake_client, caplog):
    """HTTP 502/503/504 (Modal cold-start window) also gets one retry."""
    caplog.set_level(logging.INFO)
    fake_client.query_exc = urllib.error.HTTPError("url", 502, "bad gateway", None, None)

    assert provider.prefetch("query") == ""

    assert len(fake_client.query_calls) == 2
    assert "cold-start retry (attempt 2)" in caplog.text


def test_prefetch_does_not_retry_permanent_errors(provider, fake_client, caplog):
    """4xx / malformed-JSON style errors are permanent: no budget wasted."""
    caplog.set_level(logging.INFO)
    fake_client.query_exc = ValueError("malformed JSON")

    result = provider.prefetch("query")

    assert len(fake_client.query_calls) == 1
    assert "cold-start retry" not in caplog.text
    assert result == ""


def test_prefetch_does_not_retry_authentication_error(provider, fake_client):
    """401 (missing/wrong AMADEUS_REMOTE_API_KEY) is permanent — retrying a
    wrong credential cannot help and would just burn budget."""
    fake_client.query_exc = urllib.error.HTTPError("url", 401, "unauthorized", None, None)

    assert provider.prefetch("query") == ""

    assert len(fake_client.query_calls) == 1


def test_prefetch_skips_retry_when_budget_exhausted(monkeypatch, plugin, provider, fake_client):
    """remaining <= 0 after the first failure must skip the retry."""
    values = iter([100.0, 100.0, 108.0, 108.0])  # deadline = 107.0; retry check sees -1
    monkeypatch.setattr(plugin.time, "monotonic", lambda: next(values))
    fake_client.query_exc = TimeoutError("cold start")

    result = provider.prefetch("query")

    assert len(fake_client.query_calls) == 1  # budget exhausted -> no retry
    assert fake_client.health_calls == []  # and no budget left for the probe either
    assert result == ""


def test_prefetch_attempt_timeout_capped_at_four_seconds(provider, fake_client):
    """Each attempt is capped at min(4.0, remaining)."""
    fake_client.query_response = {"evidence": [_evidence_item()]}

    provider.prefetch("query")

    assert fake_client.query_timeouts == [4.0]


def test_prefetch_completes_under_eight_seconds_with_slow_backend(provider, fake_client):
    """Acceptance: prefetch() worst case < 8s (7s deadline) even when both
    attempts of a slow cold backend time out and the health probe runs."""
    fake_client.query_delay = 0.4
    fake_client.query_exc = TimeoutError("cold start")

    start = time.monotonic()
    result = provider.prefetch("query")
    elapsed = time.monotonic() - start

    assert result == ""
    assert elapsed < 8.0


def test_prefetch_health_probe_failure_is_logged_not_raised(provider, fake_client):
    """A failing /health probe must not turn prefetch into an exception."""
    fake_client.query_exc = ConnectionError("backend down")
    fake_client.health_exc = ConnectionError("probe also down")

    assert provider.prefetch("query") == ""  # must not raise


# ---------------------------------------------------------------------------
# sync_turn() — MUST be non-blocking (developer-guide threading contract)
# ---------------------------------------------------------------------------


def test_sync_turn_runs_in_a_background_thread(provider, fake_client):
    fake_client.write_delay = 0.3  # long enough that a blocking call would fail this test's timing

    start = time.monotonic()
    provider.sync_turn("hello", "hi there", session_id="session-1")
    elapsed = time.monotonic() - start

    assert elapsed < 0.1, "sync_turn() blocked the caller — must run in a background thread"

    thread = provider._worker_thread
    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True
    assert "amadeus" in thread.name

    # Wait for the queue to drain rather than joining the (persistent,
    # never-exiting) worker thread itself.
    provider._sync_queue.join()
    assert len(fake_client.write_calls) == 2


def test_sync_turn_writes_user_and_assistant_as_journal_with_shared_turn_index(provider, fake_client):
    provider.sync_turn("hello", "hi there", session_id="session-1")
    provider._sync_queue.join()

    assert len(fake_client.write_calls) == 2
    assert {call["role"] for call in fake_client.write_calls} == {"user", "assistant"}
    for call in fake_client.write_calls:
        assert call["write_type"] == "journal"
        assert call["apply"] is True  # background recorders write for real (Windows-hooks parity)
        assert call["session_id"] == "session-1"
    assert {call["turn_index"] for call in fake_client.write_calls} == {0}


def test_sync_turn_increments_turn_index_across_turns(provider, fake_client):
    provider.sync_turn("t0 user", "t0 assistant", session_id="session-1")
    provider._sync_queue.join()
    provider.sync_turn("t1 user", "t1 assistant", session_id="session-1")
    provider._sync_queue.join()

    indices = sorted({call["turn_index"] for call in fake_client.write_calls})
    assert indices == [0, 1]


def test_sync_turn_skips_empty_halves(provider, fake_client):
    provider.sync_turn("", "only assistant", session_id="session-1")
    provider._sync_queue.join()
    assert len(fake_client.write_calls) == 1
    assert fake_client.write_calls[0]["role"] == "assistant"


def test_sync_turn_failure_is_logged_not_raised(provider, fake_client, caplog):
    fake_client.write_exc = ConnectionError("backend down")
    # Must not raise, even though the backend call inside the worker fails.
    provider.sync_turn("hello", "hi", session_id="session-1")
    provider._sync_queue.join()
    assert provider._worker_thread.is_alive()  # persistent worker survives a failed write


def test_sync_turn_preserves_order_across_back_to_back_turns(provider, fake_client):
    """Regression guard (single persistent worker + FIFO queue): writes must
    be strictly ordered — no per-call threads that could reorder turns.
    """
    fake_client.write_delay = 0.05
    provider.sync_turn("t0 user", "t0 assistant", session_id="session-1")
    provider.sync_turn("t1 user", "t1 assistant", session_id="session-1")
    provider._sync_queue.join()

    assert [call["turn_index"] for call in fake_client.write_calls] == [0, 0, 1, 1]
    assert [call["role"] for call in fake_client.write_calls] == ["user", "assistant", "user", "assistant"]


def test_sync_turn_inactive_provider_is_a_noop(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.delenv("AMADEUS_REMOTE_API_KEY", raising=False)
    p = plugin.AmadeusRemoteMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path))
    p.sync_turn("hello", "hi", session_id="session-1")
    assert p._worker_thread is None
    assert fake_client.write_calls == []


def test_shutdown_drains_pending_writes_before_returning(provider, fake_client):
    """shutdown() must not lose a final in-flight write: the persistent
    worker + sentinel design processes everything already queued before
    exiting."""
    fake_client.write_delay = 0.05
    provider.sync_turn("hello", "hi there", session_id="session-1")
    worker = provider._worker_thread
    provider.shutdown()

    assert provider._worker_thread is None  # cleaned up
    assert not worker.is_alive()
    assert len(fake_client.write_calls) == 2


def test_on_session_switch_updates_session_and_resets_turn_index(provider, fake_client):
    """session reassignment mid-process (/resume, /branch, /new) must rebind
    the session id AND reset the turn counter — the journal idempotency key
    is journal.s{session_id}.t{turn_index}, so a stale counter would make
    the new session's first write collide with the old session's entries.
    """
    provider.sync_turn("t0 user", "t0 assistant", session_id="session-1")
    provider.sync_turn("t1 user", "t1 assistant", session_id="session-1")
    provider._sync_queue.join()

    provider.on_session_switch("session-2")
    assert provider._session_id == "session-2"

    provider.sync_turn("t0 user", "t0 assistant", session_id="session-2")
    provider._sync_queue.join()

    assert [call["session_id"] for call in fake_client.write_calls] == [
        "session-1", "session-1", "session-1", "session-1", "session-2", "session-2",
    ]
    # Old session: [0, 0, 1, 1]; new session restarts at 0.
    assert [call["turn_index"] for call in fake_client.write_calls] == [0, 0, 1, 1, 0, 0]


# ---------------------------------------------------------------------------
# on_session_end() — one session-end episode (Windows amadeus_session_end
# parity; the corpus2skill plugin's v1 no-op is deliberately NOT copied)
# ---------------------------------------------------------------------------


def test_on_session_end_writes_one_session_end_episode(provider, fake_client):
    messages = [
        {"role": "user", "content": "remember this"},
        {"role": "assistant", "content": "done"},
    ]

    provider.on_session_end(messages)

    assert len(fake_client.write_calls) == 1
    call = fake_client.write_calls[0]
    assert call["write_type"] == "episode"
    assert call["apply"] is True
    assert call["session_id"] == "session-1"
    assert call["role"] == "system"
    assert call["subject"] == "session_end"
    assert call["namespace"] == "hh-agent-dashboard"
    assert isinstance(call["content"], str) and call["content"]
    assert "2 message" in call["content"]


def test_on_session_end_is_synchronous_best_effort_on_backend_failure(provider, fake_client):
    fake_client.write_exc = ConnectionError("backend down")
    # Must not raise, even though the synchronous backend call fails.
    provider.on_session_end([{"role": "user", "content": "hi"}])


def test_on_session_end_inactive_provider_is_a_noop(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.delenv("AMADEUS_REMOTE_API_KEY", raising=False)
    p = plugin.AmadeusRemoteMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path))
    p.on_session_end([{"role": "user", "content": "hi"}])
    assert fake_client.write_calls == []


def test_on_session_end_without_session_id_is_a_noop(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.setenv("AMADEUS_REMOTE_API_KEY", "test-token")
    p = plugin.AmadeusRemoteMemoryProvider()
    p.initialize("", hermes_home=str(tmp_path))  # no session context
    p.on_session_end([{"role": "user", "content": "hi"}])
    assert fake_client.write_calls == []


# ---------------------------------------------------------------------------
# get_tool_schemas() — read-only tool surface (BUG-5 style regression guard)
# ---------------------------------------------------------------------------

_FORBIDDEN_TOOL_NAME_FRAGMENTS = ("write", "add_memory", "store", "journal", "episode", "memory_write")


def test_get_tool_schemas_exposes_only_query(provider):
    schemas = provider.get_tool_schemas()
    names = [s["name"] for s in schemas]
    assert names == ["amadeus_query"]


def test_get_tool_schemas_excludes_write_tools(provider):
    """Regression guard: no write-capable tool must ever be agent-callable.

    Mirrors the BUG-5 pattern referenced in 03_Architecture.md §13 M-07 —
    a memory-provider tool schema silently gaining a write capability.
    """
    schemas = provider.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert "journal_write" not in names
    assert "memory_write" not in names
    assert "episode_write" not in names
    for name in names:
        for fragment in _FORBIDDEN_TOOL_NAME_FRAGMENTS:
            assert fragment not in name, f"tool {name!r} looks write-capable ({fragment!r})"


def test_get_tool_schemas_not_gated_on_active_state(monkeypatch, plugin, fake_client, tmp_path):
    """get_tool_schemas() must stay static regardless of is_available()/initialize().

    MemoryManager.add_provider() calls get_tool_schemas() to build its
    tool-routing table immediately at registration time, BEFORE
    initialize_all() ever runs (see agent/memory_manager.py add_provider()).
    """
    monkeypatch.delenv("AMADEUS_REMOTE_API_KEY", raising=False)
    p = plugin.AmadeusRemoteMemoryProvider()
    # Not initialized at all yet.
    assert [s["name"] for s in p.get_tool_schemas()] == ["amadeus_query"]
    # Initialized but without credentials (inactive).
    p.initialize("session-1", hermes_home=str(tmp_path))
    assert [s["name"] for s in p.get_tool_schemas()] == ["amadeus_query"]


# ---------------------------------------------------------------------------
# handle_tool_call()
# ---------------------------------------------------------------------------


def test_handle_tool_call_query_returns_backend_json(provider, fake_client):
    fake_client.query_response = {"evidence": [_evidence_item(), _evidence_item(layer="c2s")]}
    raw = provider.handle_tool_call("amadeus_query", {"query": "hermes", "project": "HH-Agent", "limit": 5})
    payload = json.loads(raw)
    assert len(payload["evidence"]) == 2
    call = fake_client.query_calls[0]
    assert call == {
        "question": "hermes",
        "project": "HH-Agent",
        "per_device_limit": 5,
        "snippet_chars": None,
    }


def test_handle_tool_call_defaults_limit(provider, fake_client):
    provider.handle_tool_call("amadeus_query", {"query": "hermes"})
    call = fake_client.query_calls[0]
    assert call["per_device_limit"] == 10
    assert call["project"] is None


def test_handle_tool_call_requires_query(provider):
    raw = provider.handle_tool_call("amadeus_query", {})
    payload = json.loads(raw)
    assert "error" in payload


def test_handle_tool_call_unknown_tool_errors(provider):
    raw = provider.handle_tool_call("journal_write", {"content": "sneaky"})
    payload = json.loads(raw)
    assert "error" in payload


def test_handle_tool_call_backend_failure_returns_error_json(provider, fake_client):
    fake_client.query_exc = ConnectionError("backend down")
    raw = provider.handle_tool_call("amadeus_query", {"query": "x"})
    payload = json.loads(raw)
    assert "error" in payload


def test_handle_tool_call_inactive_provider_returns_error(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.delenv("AMADEUS_REMOTE_API_KEY", raising=False)
    p = plugin.AmadeusRemoteMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path))
    raw = p.handle_tool_call("amadeus_query", {"query": "x"})
    payload = json.loads(raw)
    assert "error" in payload
    assert fake_client.query_calls == []


# ---------------------------------------------------------------------------
# save_config() / get_config_schema()
# ---------------------------------------------------------------------------


def test_get_config_schema_has_only_api_key(plugin):
    """base_url must NOT be listed: `hermes memory setup` should only prompt
    for the API key; base_url is an advanced, file-only setting
    (amadeus_remote.json) — same design as the corpus2skill plugin.
    """
    p = plugin.AmadeusRemoteMemoryProvider()
    schema = p.get_config_schema()
    keys = [f["key"] for f in schema]
    assert keys == ["api_key"]
    api_key_field = schema[0]
    assert api_key_field["secret"] is True
    assert api_key_field["env_var"] == "AMADEUS_REMOTE_API_KEY"


def test_save_config_writes_base_url_only(plugin, tmp_path):
    p = plugin.AmadeusRemoteMemoryProvider()
    p.save_config({"base_url": "http://localhost:8000", "api_key": "should-not-be-written"}, str(tmp_path))

    config_path = tmp_path / "amadeus_remote.json"
    assert config_path.exists()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data == {"base_url": "http://localhost:8000"}
    assert "api_key" not in data


def test_save_config_then_initialize_picks_up_base_url(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.setenv("AMADEUS_REMOTE_API_KEY", "test-token")
    p = plugin.AmadeusRemoteMemoryProvider()
    p.save_config({"base_url": "http://localhost:9000"}, str(tmp_path))
    p.initialize("session-1", hermes_home=str(tmp_path))
    assert p._base_url == "http://localhost:9000"


def test_save_config_defaults_when_base_url_missing(plugin, tmp_path):
    p = plugin.AmadeusRemoteMemoryProvider()
    p.save_config({}, str(tmp_path))
    data = json.loads((tmp_path / "amadeus_remote.json").read_text(encoding="utf-8"))
    assert data == {"base_url": plugin._DEFAULT_BASE_URL}


# ---------------------------------------------------------------------------
# system_prompt_block() / register()
# ---------------------------------------------------------------------------


def test_system_prompt_block_mentions_amadeus_when_active(provider):
    block = provider.system_prompt_block()
    assert "AmadeuS" in block
    assert "amadeus_query" in block


def test_system_prompt_block_empty_when_inactive(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.delenv("AMADEUS_REMOTE_API_KEY", raising=False)
    p = plugin.AmadeusRemoteMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path))
    assert p.system_prompt_block() == ""


def test_register_registers_the_provider(plugin):
    calls = []

    class FakeCtx:
        def register_memory_provider(self, provider):
            calls.append(provider)

    plugin.register(FakeCtx())
    assert len(calls) == 1
    assert isinstance(calls[0], plugin.AmadeusRemoteMemoryProvider)
    assert calls[0].name == "amadeus_remote"


# ---------------------------------------------------------------------------
# MemoryManager integration (developer-guide "Testing" pattern)
# ---------------------------------------------------------------------------


def test_integrates_with_real_memory_manager(monkeypatch, plugin, fake_client, tmp_path):
    """End-to-end sanity check against the real agent.memory_manager.MemoryManager,
    mirroring the pattern in website/docs/developer-guide/memory-provider-plugin.md
    ("Testing"). The HTTP client is still faked — no network call is made.
    """
    from agent.memory_manager import MemoryManager

    monkeypatch.setenv("AMADEUS_REMOTE_API_KEY", "test-token")
    fake_client.query_response = {"evidence": [_evidence_item(snippet="amadeus memory fact")]}

    mgr = MemoryManager()
    provider = plugin.AmadeusRemoteMemoryProvider()
    mgr.add_provider(provider)
    mgr.initialize_all(session_id="test-1", platform="cli", hermes_home=str(tmp_path))

    schemas = mgr.get_all_tool_schemas()
    names = [s["name"] for s in schemas]
    assert "amadeus_query" in names
    assert "journal_write" not in names

    result = mgr.handle_tool_call("amadeus_query", {"query": "hermes"})
    payload = json.loads(result)
    assert len(payload["evidence"]) == 1

    mgr.shutdown_all()
