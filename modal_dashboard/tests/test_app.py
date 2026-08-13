from __future__ import annotations

from modal_dashboard import app as app_module


def test_ensure_agent_token_seeded_calls_remote_when_missing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        app_module.refresh_dashboard_agent_token, "remote", lambda: calls.append(1)
    )

    app_module._ensure_agent_token_seeded(tmp_path)

    assert calls == [1]


def test_ensure_agent_token_seeded_skips_remote_when_token_present(tmp_path, monkeypatch):
    token_dir = tmp_path / ".hh-agent"
    token_dir.mkdir(parents=True)
    (token_dir / "agent_token.json").write_text('{"token": "hha1.sentinel"}', encoding="utf-8")

    calls = []
    monkeypatch.setattr(
        app_module.refresh_dashboard_agent_token, "remote", lambda: calls.append(1)
    )

    app_module._ensure_agent_token_seeded(tmp_path)

    assert calls == []
