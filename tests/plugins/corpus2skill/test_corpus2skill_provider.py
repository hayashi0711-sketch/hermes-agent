"""Tests for the Corpus2Skill memory provider plugin.

The plugin lives at ``.hermes/plugins/corpus2skill/`` — a Hermes "Project
Provider" (see docs/hh-agent/03_Architecture.md §13 M-05). That directory
name starts with a dot, so it cannot be imported with a normal dotted
``import`` statement (``.hermes`` is not a legal top-level package name).
We load it the same way Hermes' own project-plugin discovery does: by file
path via importlib, not by adding it to sys.path under a fake name.

None of these tests talk to the real Corpus2Skill backend. The HTTP client
class (``_Corpus2SkillClient``) is monkeypatched out everywhere so no
network call is ever attempted.
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
    Path(__file__).resolve().parents[3] / ".hermes" / "plugins" / "corpus2skill" / "__init__.py"
)


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location("corpus2skill_plugin_under_test", _PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def plugin():
    return _load_plugin_module()


class FakeClient:
    """Stand-in for _Corpus2SkillClient. Records calls, never touches the network."""

    def __init__(self, api_key="", base_url="", timeout=15.0):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.search_calls = []
        self.search_timeouts = []
        self.recall_calls = []
        self.recall_timeouts = []
        self.write_calls = []
        self.health_calls = []
        self.search_response = {"results": []}
        self.recall_response = {"results": []}
        self.health_response = {"status": "ok"}
        self.search_exc = None
        self.recall_exc = None
        self.write_exc = None
        self.health_exc = None
        self.write_delay = 0.0
        self.search_delay = 0.0
        self.recall_delay = 0.0

    def search(self, query, limit=10, *, timeout=None):
        if self.search_delay:
            time.sleep(self.search_delay)
        self.search_calls.append((query, limit))
        self.search_timeouts.append(timeout)
        if self.search_exc:
            raise self.search_exc
        return self.search_response

    def journal_recall(self, session_id, query, limit=10, *, timeout=None):
        if self.recall_delay:
            time.sleep(self.recall_delay)
        self.recall_calls.append((session_id, query, limit))
        self.recall_timeouts.append(timeout)
        if self.recall_exc:
            raise self.recall_exc
        return self.recall_response

    def journal_write(self, session_id, role, content, turn_index):
        if self.write_delay:
            time.sleep(self.write_delay)
        self.write_calls.append((session_id, role, content, turn_index))
        if self.write_exc:
            raise self.write_exc
        return {}

    def health(self, *, timeout=None):
        self.health_calls.append(timeout)
        if self.health_exc:
            raise self.health_exc
        return self.health_response


@pytest.fixture
def fake_client(monkeypatch, plugin):
    client = FakeClient()
    monkeypatch.setattr(plugin, "_Corpus2SkillClient", lambda *a, **kw: client)
    return client


@pytest.fixture
def provider(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.setenv("CORPUS2SKILL_API_KEY", "test-token")
    p = plugin.Corpus2SkillMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path), platform="cli")
    return p


# ---------------------------------------------------------------------------
# is_available() — env var presence only, no network
# ---------------------------------------------------------------------------


def test_is_available_true_when_key_set(monkeypatch, plugin):
    monkeypatch.setenv("CORPUS2SKILL_API_KEY", "some-token")
    p = plugin.Corpus2SkillMemoryProvider()
    assert p.is_available() is True


def test_is_available_false_when_key_missing(monkeypatch, plugin):
    monkeypatch.delenv("CORPUS2SKILL_API_KEY", raising=False)
    p = plugin.Corpus2SkillMemoryProvider()
    assert p.is_available() is False


def test_get_api_key_does_not_leak_to_os_environ_when_scoped_secret_is_empty(monkeypatch, plugin):
    """Codex review (2026-08-15, Critical) regression guard: when the real
    scoped secret resolver (agent.secret_scope.get_secret) is available and
    says "empty" or raises, that answer is final — os.environ must never be
    consulted as a fallback. The old code did fall back, which in multiplex
    mode let a profile with no CORPUS2SKILL_API_KEY of its own silently
    inherit another profile's process-global env var.
    """
    monkeypatch.setenv("CORPUS2SKILL_API_KEY", "leaked-from-another-profile")
    monkeypatch.setattr(plugin, "_get_secret", lambda key, default: "")
    assert plugin._get_api_key() == ""


def test_get_api_key_does_not_leak_to_os_environ_when_scoped_secret_raises(monkeypatch, plugin):
    def _boom(key, default):
        raise RuntimeError("no scope installed")

    monkeypatch.setenv("CORPUS2SKILL_API_KEY", "leaked-from-another-profile")
    monkeypatch.setattr(plugin, "_get_secret", _boom)
    assert plugin._get_api_key() == ""


def test_get_api_key_uses_scoped_secret_when_present(monkeypatch, plugin):
    monkeypatch.setenv("CORPUS2SKILL_API_KEY", "should-be-ignored")
    monkeypatch.setattr(plugin, "_get_secret", lambda key, default: "scoped-token")
    assert plugin._get_api_key() == "scoped-token"


def test_get_api_key_falls_back_to_os_environ_only_when_secret_scope_unimportable(monkeypatch, plugin):
    """The only legitimate os.environ fallback: agent.secret_scope itself
    isn't importable at all, so there is no scope system to violate."""
    monkeypatch.setattr(plugin, "_get_secret", None)
    monkeypatch.setenv("CORPUS2SKILL_API_KEY", "env-token")
    assert plugin._get_api_key() == "env-token"


def test_is_available_makes_no_network_call(monkeypatch, plugin):
    """is_available() must never touch the network (MemoryProvider contract)."""
    monkeypatch.setenv("CORPUS2SKILL_API_KEY", "some-token")

    def _boom(*a, **kw):
        raise AssertionError("is_available() must not open a network connection")

    monkeypatch.setattr(plugin.urllib.request, "urlopen", _boom)
    p = plugin.Corpus2SkillMemoryProvider()
    assert p.is_available() is True  # would have raised above if it tried the network


# ---------------------------------------------------------------------------
# prefetch() — calls both lanes, merges results, fails soft
# ---------------------------------------------------------------------------


def test_prefetch_calls_both_lanes_and_merges(provider, fake_client):
    fake_client.search_response = {"results": ["fact from lane A"]}
    fake_client.recall_response = {"results": ["turn from lane B"]}

    result = provider.prefetch("what do we know?", session_id="session-1")

    assert fake_client.search_calls == [("what do we know?", 10)]
    assert fake_client.recall_calls == [("session-1", "what do we know?", 10)]
    assert "fact from lane A" in result
    assert "turn from lane B" in result
    assert "long-term memory" in result
    assert "recent session journal" in result


def test_prefetch_uses_provider_session_id_when_not_passed(provider, fake_client):
    fake_client.search_response = {"results": []}
    fake_client.recall_response = {"results": []}
    provider.prefetch("query")
    assert fake_client.recall_calls[0][0] == "session-1"  # from initialize()


def test_prefetch_empty_query_short_circuits(provider, fake_client):
    assert provider.prefetch("") == ""
    assert provider.prefetch("   ") == ""
    assert fake_client.search_calls == []
    assert fake_client.recall_calls == []


def test_prefetch_returns_empty_context_on_backend_failure(provider, fake_client):
    """Fail-soft: an unreachable backend must not raise or block the turn."""
    fake_client.search_exc = ConnectionError("backend down")
    fake_client.recall_exc = TimeoutError("backend timeout")

    result = provider.prefetch("anything")

    assert result == ""


def test_prefetch_partial_failure_still_returns_the_other_lane(provider, fake_client):
    fake_client.search_exc = ConnectionError("lane A down")
    fake_client.recall_response = {"results": ["still here"]}

    result = provider.prefetch("query")

    assert "still here" in result
    assert "long-term memory" not in result


def test_prefetch_inactive_provider_returns_empty(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.delenv("CORPUS2SKILL_API_KEY", raising=False)
    p = plugin.Corpus2SkillMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path))
    assert p.prefetch("query") == ""
    assert fake_client.search_calls == []


# ---------------------------------------------------------------------------
# prefetch() deadline budget (grand design 2026-09-01 §3.3, T1.2 / D2):
# total 7.0s deadline, per-attempt cap min(4.0, remaining), ONE retry for
# connection-class errors only, lanes skipped when remaining <= 0.
# ---------------------------------------------------------------------------


def test_prefetch_retries_connection_class_error_once(provider, fake_client, caplog):
    """Cold-start rescue: TimeoutError gets exactly one retry within budget."""
    caplog.set_level(logging.INFO)
    fake_client.search_exc = TimeoutError("cold start")
    fake_client.recall_response = {"results": ["lane b ok"]}

    result = provider.prefetch("query")

    assert len(fake_client.search_calls) == 2  # original + 1 retry
    assert "cold-start retry (attempt 2)" in caplog.text
    assert "lane b ok" in result


def test_prefetch_retries_http_502_once(provider, fake_client, caplog):
    """HTTP 502/503/504 (Modal cold-start window) also gets one retry."""
    fake_client.search_exc = urllib.error.HTTPError("url", 502, "bad gateway", None, None)
    fake_client.recall_response = {"results": ["lane b ok"]}

    result = provider.prefetch("query")

    assert len(fake_client.search_calls) == 2
    assert "lane b ok" in result


def test_prefetch_does_not_retry_permanent_errors(provider, fake_client, caplog):
    """4xx / malformed-JSON style errors are permanent: no budget wasted."""
    caplog.set_level(logging.INFO)
    fake_client.search_exc = ValueError("malformed JSON")
    fake_client.recall_response = {"results": ["lane b ok"]}

    result = provider.prefetch("query")

    assert len(fake_client.search_calls) == 1
    assert "cold-start retry" not in caplog.text
    assert "lane b ok" in result


def test_prefetch_skips_lane_b_when_deadline_exhausted(monkeypatch, plugin, provider, fake_client):
    """remaining <= 0 must skip the lane instead of delaying the turn."""
    values = iter([100.0, 100.0, 108.0, 108.0])  # deadline = 107.0; Lane B sees remaining < 0
    monkeypatch.setattr(plugin.time, "monotonic", lambda: next(values))
    fake_client.search_response = {"results": ["lane a"]}
    fake_client.recall_response = {"results": ["lane b"]}

    result = provider.prefetch("query")

    assert len(fake_client.search_calls) == 1
    assert fake_client.recall_calls == []  # budget exhausted -> skipped
    assert "lane a" in result and "lane b" not in result


def test_prefetch_lane_timeouts_capped_at_four_seconds(provider, fake_client):
    """Each lane attempt is capped at min(4.0, remaining)."""
    fake_client.search_response = {"results": ["a"]}
    fake_client.recall_response = {"results": ["b"]}

    provider.prefetch("query")

    assert fake_client.search_timeouts + fake_client.recall_timeouts == [4.0, 4.0]


def test_prefetch_completes_under_eight_seconds_with_slow_backend(provider, fake_client):
    """Grand design acceptance: prefetch() worst case < 8s (7s deadline).

    A slow, cold backend failing with TimeoutError on both lanes (2 attempts
    each) plus the health probe must still return well inside the 8s window
    MemoryManager allows before abandoning external prefetch.
    """
    fake_client.search_delay = 0.4
    fake_client.recall_delay = 0.4
    fake_client.search_exc = TimeoutError("cold start")
    fake_client.recall_exc = TimeoutError("cold start")

    start = time.monotonic()
    result = provider.prefetch("query")
    elapsed = time.monotonic() - start

    assert result == ""
    assert elapsed < 8.0


def test_prefetch_probes_health_when_all_lanes_fail(provider, fake_client):
    """After a total prefetch failure, one /health probe runs (warm/cold diag)."""
    fake_client.search_exc = ConnectionError("backend down")
    fake_client.recall_exc = ConnectionError("backend down")

    assert provider.prefetch("query") == ""
    assert len(fake_client.health_calls) == 1


def test_prefetch_does_not_probe_health_when_a_lane_succeeds(provider, fake_client):
    fake_client.search_response = {"results": ["lane a ok"]}

    provider.prefetch("query")

    assert fake_client.health_calls == []


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
    assert "corpus2skill" in thread.name

    # Wait for the queue to drain rather than joining the (persistent,
    # never-exiting) worker thread itself.
    provider._sync_queue.join()
    assert len(fake_client.write_calls) == 2


def test_sync_turn_writes_user_and_assistant_with_shared_turn_index(provider, fake_client):
    provider.sync_turn("hello", "hi there", session_id="session-1")
    provider._sync_queue.join()

    assert len(fake_client.write_calls) == 2
    roles = {call[1] for call in fake_client.write_calls}
    assert roles == {"user", "assistant"}
    turn_indices = {call[3] for call in fake_client.write_calls}
    assert turn_indices == {0}  # same turn_index for both halves of one turn


def test_sync_turn_increments_turn_index_across_turns(provider, fake_client):
    provider.sync_turn("t0 user", "t0 assistant", session_id="session-1")
    provider._sync_queue.join()
    provider.sync_turn("t1 user", "t1 assistant", session_id="session-1")
    provider._sync_queue.join()

    indices = sorted({call[3] for call in fake_client.write_calls})
    assert indices == [0, 1]


def test_sync_turn_skips_empty_halves(provider, fake_client):
    provider.sync_turn("", "only assistant", session_id="session-1")
    provider._sync_queue.join()
    assert len(fake_client.write_calls) == 1
    assert fake_client.write_calls[0][1] == "assistant"


def test_sync_turn_failure_is_logged_not_raised(provider, fake_client, caplog):
    fake_client.write_exc = ConnectionError("backend down")
    # Must not raise, even though the backend call inside the worker fails.
    provider.sync_turn("hello", "hi", session_id="session-1")
    provider._sync_queue.join()
    assert provider._worker_thread.is_alive()  # persistent worker survives a failed write


def test_sync_turn_preserves_order_across_back_to_back_turns(provider, fake_client):
    """Codex review (2026-08-15, High) regression guard: the old per-call
    daemon-thread-with-5s-join design could start a second thread before the
    first one (still mid-write) finished, reordering writes across turns.
    The single persistent worker must process strictly in enqueue order.
    """
    fake_client.write_delay = 0.05
    provider.sync_turn("t0 user", "t0 assistant", session_id="session-1")
    provider.sync_turn("t1 user", "t1 assistant", session_id="session-1")
    provider._sync_queue.join()

    assert [call[3] for call in fake_client.write_calls] == [0, 0, 1, 1]
    assert [call[1] for call in fake_client.write_calls] == ["user", "assistant", "user", "assistant"]


def test_sync_turn_inactive_provider_is_a_noop(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.delenv("CORPUS2SKILL_API_KEY", raising=False)
    p = plugin.Corpus2SkillMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path))
    p.sync_turn("hello", "hi", session_id="session-1")
    assert p._worker_thread is None
    assert fake_client.write_calls == []


def test_shutdown_drains_pending_writes_before_returning(provider, fake_client):
    """Codex review (2026-08-15, High) regression guard: shutdown() must not
    lose a final in-flight write. The old design only joined the most
    recently spawned thread with a timeout; a slow write mid-flight during
    shutdown could be dropped. The persistent worker + sentinel design
    processes everything already queued before exiting.
    """
    fake_client.write_delay = 0.05
    provider.sync_turn("hello", "hi there", session_id="session-1")
    worker = provider._worker_thread
    provider.shutdown()

    assert provider._worker_thread is None  # cleaned up
    assert not worker.is_alive()
    assert len(fake_client.write_calls) == 2


# ---------------------------------------------------------------------------
# get_tool_schemas() — read-only tool surface (BUG-5 style regression guard)
# ---------------------------------------------------------------------------

_FORBIDDEN_TOOL_NAME_FRAGMENTS = ("journal_write", "add_new_memory", "write", "add_memory", "store")


def test_get_tool_schemas_exposes_only_search(provider):
    schemas = provider.get_tool_schemas()
    names = [s["name"] for s in schemas]
    assert names == ["corpus2skill_search"]


def test_get_tool_schemas_excludes_write_tools(provider):
    """Regression guard: no write-capable tool must ever be agent-callable.

    Mirrors the BUG-5 pattern referenced in 03_Architecture.md §13 M-07 —
    a memory-provider tool schema silently gaining a write capability.
    """
    schemas = provider.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert "journal_write" not in names
    assert "add_new_memory" not in names
    for name in names:
        for fragment in _FORBIDDEN_TOOL_NAME_FRAGMENTS:
            if fragment == "write" and name == "corpus2skill_search":
                continue
            assert fragment not in name, f"tool {name!r} looks write-capable ({fragment!r})"


def test_get_tool_schemas_not_gated_on_active_state(monkeypatch, plugin, fake_client, tmp_path):
    """get_tool_schemas() must stay static regardless of is_available()/initialize().

    MemoryManager.add_provider() calls get_tool_schemas() to build its
    tool-routing table immediately at registration time, BEFORE
    initialize_all() ever runs (see agent/memory_manager.py add_provider()).
    A provider that is not yet initialized — or never becomes available —
    must still return the same schema, or add_provider() -> initialize_all()
    (the documented usage order) would silently register zero tools.
    """
    monkeypatch.delenv("CORPUS2SKILL_API_KEY", raising=False)
    p = plugin.Corpus2SkillMemoryProvider()
    # Not initialized at all yet.
    assert [s["name"] for s in p.get_tool_schemas()] == ["corpus2skill_search"]
    # Initialized but without credentials (inactive).
    p.initialize("session-1", hermes_home=str(tmp_path))
    assert [s["name"] for s in p.get_tool_schemas()] == ["corpus2skill_search"]


# ---------------------------------------------------------------------------
# handle_tool_call()
# ---------------------------------------------------------------------------


def test_handle_tool_call_search_returns_results(provider, fake_client):
    fake_client.search_response = {"results": [{"content": "hit 1"}, "hit 2"]}
    raw = provider.handle_tool_call("corpus2skill_search", {"query": "hermes", "limit": 5})
    payload = json.loads(raw)
    assert payload["count"] == 2
    assert fake_client.search_calls == [("hermes", 5)]


def test_handle_tool_call_requires_query(provider):
    raw = provider.handle_tool_call("corpus2skill_search", {})
    payload = json.loads(raw)
    assert "error" in payload


def test_handle_tool_call_unknown_tool_errors(provider):
    raw = provider.handle_tool_call("journal_write", {"content": "sneaky"})
    payload = json.loads(raw)
    assert "error" in payload


def test_handle_tool_call_backend_failure_returns_error_json(provider, fake_client):
    fake_client.search_exc = ConnectionError("backend down")
    raw = provider.handle_tool_call("corpus2skill_search", {"query": "x"})
    payload = json.loads(raw)
    assert "error" in payload


def test_handle_tool_call_inactive_provider_returns_error(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.delenv("CORPUS2SKILL_API_KEY", raising=False)
    p = plugin.Corpus2SkillMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path))
    raw = p.handle_tool_call("corpus2skill_search", {"query": "x"})
    payload = json.loads(raw)
    assert "error" in payload
    assert fake_client.search_calls == []


# ---------------------------------------------------------------------------
# save_config() / get_config_schema()
# ---------------------------------------------------------------------------


def test_get_config_schema_has_only_api_key(plugin):
    """Codex review (2026-08-15, Low): base_url used to be listed here too,
    which meant `hermes memory setup` would prompt for it — contradicting
    the "minimal schema, Supermemory-style" design intent (base_url is
    meant to be an advanced, file-only setting via corpus2skill.json).
    """
    p = plugin.Corpus2SkillMemoryProvider()
    schema = p.get_config_schema()
    keys = [f["key"] for f in schema]
    assert keys == ["api_key"]
    api_key_field = schema[0]
    assert api_key_field["secret"] is True
    assert api_key_field["env_var"] == "CORPUS2SKILL_API_KEY"


def test_save_config_writes_base_url_only(plugin, tmp_path):
    p = plugin.Corpus2SkillMemoryProvider()
    p.save_config({"base_url": "http://localhost:8000", "api_key": "should-not-be-written"}, str(tmp_path))

    config_path = tmp_path / "corpus2skill.json"
    assert config_path.exists()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data == {"base_url": "http://localhost:8000"}
    assert "api_key" not in data


def test_save_config_then_initialize_picks_up_base_url(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.setenv("CORPUS2SKILL_API_KEY", "test-token")
    p = plugin.Corpus2SkillMemoryProvider()
    p.save_config({"base_url": "http://localhost:9000"}, str(tmp_path))
    p.initialize("session-1", hermes_home=str(tmp_path))
    assert p._base_url == "http://localhost:9000"


def test_save_config_defaults_when_base_url_missing(plugin, tmp_path):
    p = plugin.Corpus2SkillMemoryProvider()
    p.save_config({}, str(tmp_path))
    data = json.loads((tmp_path / "corpus2skill.json").read_text(encoding="utf-8"))
    assert data == {"base_url": plugin._DEFAULT_BASE_URL}


# ---------------------------------------------------------------------------
# system_prompt_block() / on_session_end() / register()
# ---------------------------------------------------------------------------


def test_system_prompt_block_mentions_corpus2skill_when_active(provider):
    block = provider.system_prompt_block()
    assert "Corpus2Skill" in block


def test_system_prompt_block_empty_when_inactive(monkeypatch, plugin, fake_client, tmp_path):
    monkeypatch.delenv("CORPUS2SKILL_API_KEY", raising=False)
    p = plugin.Corpus2SkillMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path))
    assert p.system_prompt_block() == ""


def test_on_session_end_is_a_noop(provider, fake_client):
    # Must not raise and must not touch the client (v1 scope, see docstring).
    provider.on_session_end([{"role": "user", "content": "hi"}])
    assert fake_client.write_calls == []
    assert fake_client.search_calls == []
    assert fake_client.recall_calls == []


def test_register_registers_the_provider(plugin):
    calls = []

    class FakeCtx:
        def register_memory_provider(self, provider):
            calls.append(provider)

    plugin.register(FakeCtx())
    assert len(calls) == 1
    assert isinstance(calls[0], plugin.Corpus2SkillMemoryProvider)
    assert calls[0].name == "corpus2skill"


# ---------------------------------------------------------------------------
# MemoryManager integration (developer-guide "Testing" pattern)
# ---------------------------------------------------------------------------


def test_integrates_with_real_memory_manager(monkeypatch, plugin, fake_client, tmp_path):
    """End-to-end sanity check against the real agent.memory_manager.MemoryManager,
    mirroring the pattern in website/docs/developer-guide/memory-provider-plugin.md
    ("Testing"). The HTTP client is still faked — no network call is made.
    """
    from agent.memory_manager import MemoryManager

    monkeypatch.setenv("CORPUS2SKILL_API_KEY", "test-token")
    fake_client.search_response = {"results": ["hermes memory fact"]}

    mgr = MemoryManager()
    provider = plugin.Corpus2SkillMemoryProvider()
    mgr.add_provider(provider)
    mgr.initialize_all(session_id="test-1", platform="cli", hermes_home=str(tmp_path))

    schemas = mgr.get_all_tool_schemas()
    names = [s["name"] for s in schemas]
    assert "corpus2skill_search" in names
    assert "journal_write" not in names

    result = mgr.handle_tool_call("corpus2skill_search", {"query": "hermes"})
    payload = json.loads(result)
    assert payload["count"] == 1

    mgr.shutdown_all()
