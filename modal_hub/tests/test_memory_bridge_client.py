"""modal_hub/tests/test_memory_bridge_client.py — memory_bridge.py の不変条件テスト。

D-03（Corpus2Skill は参照専用のサイドチャネル・Distiller のハード依存に
しない）を検証する。実 MCP サーバーへの接続は行わない（`connect()` は
現状 BLOCKED — モジュール docstring 参照）。
"""

from __future__ import annotations

import inspect

from modal_hub.services import memory_bridge


class _FakeClient:
    def __init__(self, results):
        self._results = results
        self.queries: list[str] = []

    def get_memory_index(self, path: str = "") -> str:
        return ""

    def search_memory(self, query: str, limit: int = 10):
        self.queries.append(query)
        return self._results[:limit]


class _BrokenClient:
    def get_memory_index(self, path: str = "") -> str:
        return ""

    def search_memory(self, query: str, limit: int = 10):
        raise RuntimeError("simulated MCP failure")


def test_search_existing_memory_returns_empty_when_no_client_configured():
    """`connect()` が BLOCKED のため、クライアント未指定時は常に空リスト。"""
    assert memory_bridge.search_existing_memory("query") == []


def test_search_existing_memory_never_raises_on_connect_failure(monkeypatch):
    def _boom():
        raise RuntimeError("unexpected connect failure")

    monkeypatch.setattr(memory_bridge, "connect", _boom)
    assert memory_bridge.search_existing_memory("query") == []


def test_search_existing_memory_uses_injected_client():
    client = _FakeClient([{"name": "a"}, {"name": "b"}, {"name": "c"}])
    result = memory_bridge.search_existing_memory("query", limit=2, client=client)
    assert result == [{"name": "a"}, {"name": "b"}]
    assert client.queries == ["query"]


def test_search_existing_memory_returns_empty_when_client_raises():
    assert memory_bridge.search_existing_memory("q", client=_BrokenClient()) == []


def test_no_add_new_memory_function_exists():
    """D-03: 書き込み関数は一切実装しない。属性としても存在しないこと。"""
    assert not hasattr(memory_bridge, "add_new_memory")
    for name in dir(memory_bridge):
        assert "add_new_memory" not in name.lower()
        assert "write_memory" not in name.lower()


def test_no_personal_notes_path_references_in_source():
    """モジュール自身のソースにも Obsidian/Vault 系の語が無いことを固定する
    （リポジトリ全体を見る test_phase1b_guards.py とは独立に、この
    ファイル単体でも regression をすぐ検出できるように）。"""
    source = inspect.getsource(memory_bridge)
    for marker in ("Obsidian", "obsidian", "Vault", "vault"):
        assert marker not in source


def test_connect_raises_memory_bridge_unavailable_error():
    import pytest

    with pytest.raises(memory_bridge.MemoryBridgeUnavailableError):
        memory_bridge.connect()
