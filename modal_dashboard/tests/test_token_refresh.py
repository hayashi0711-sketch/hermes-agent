from __future__ import annotations

import json

import pytest

from modal_dashboard import token_refresh


class _FakeStore:
    """Minimal CredentialStore double -- matches modal_hub.core.store's
    module-level get/put_if_absent/delete contract, in-memory only.
    """

    def __init__(self):
        self._data: dict[str, object] = {}

    def put_if_absent(self, key, value):
        if key in self._data:
            return False
        self._data[key] = value
        return True

    def get(self, key):
        return self._data.get(key)

    def delete(self, key):
        self._data.pop(key, None)


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("HH_AGENT_TOKEN_SIGNING_KEY", "test-signing-key-0123456789abcdef")


def test_issue_dashboard_agent_token_writes_token_file(tmp_path):
    token = token_refresh.issue_dashboard_agent_token(tmp_path, store=_FakeStore())

    assert token.startswith("hha1.")
    token_path = tmp_path / ".hh-agent" / "agent_token.json"
    assert token_path.is_file()
    data = json.loads(token_path.read_text(encoding="utf-8"))
    assert data["token"] == token


def test_issue_dashboard_agent_token_requires_signing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("HH_AGENT_TOKEN_SIGNING_KEY", raising=False)

    with pytest.raises(RuntimeError, match="HH_AGENT_TOKEN_SIGNING_KEY"):
        token_refresh.issue_dashboard_agent_token(tmp_path, store=_FakeStore())


def test_seed_agent_token_if_missing_writes_on_first_boot(tmp_path):
    seeded = token_refresh.seed_agent_token_if_missing(tmp_path, store=_FakeStore())

    assert seeded is True
    assert (tmp_path / ".hh-agent" / "agent_token.json").is_file()


def test_seed_agent_token_if_missing_does_not_overwrite_existing(tmp_path):
    token_dir = tmp_path / ".hh-agent"
    token_dir.mkdir(parents=True)
    token_path = token_dir / "agent_token.json"
    token_path.write_text('{"token": "hha1.sentinel"}', encoding="utf-8")

    seeded = token_refresh.seed_agent_token_if_missing(tmp_path, store=_FakeStore())

    assert seeded is False
    assert token_path.read_text(encoding="utf-8") == '{"token": "hha1.sentinel"}'
