from __future__ import annotations

from modal_dashboard import app as app_module


class _FakeStore:
    def put_if_absent(self, key, value):
        return True

    def get(self, key):
        return None

    def delete(self, key):
        pass


def test_build_fastapi_app_seeds_and_verifies_before_returning_app(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr(
        app_module.bootstrap,
        "seed_config_yaml",
        lambda home: calls.append(("seed_config", home)) or True,
    )
    monkeypatch.setattr(
        app_module.bootstrap,
        "verify_pretool_hooks_registered",
        lambda: calls.append(("verify",)),
    )
    monkeypatch.setattr(
        app_module.token_refresh,
        "seed_agent_token_if_missing",
        lambda home, *, store: calls.append(("seed_token", home)) or True,
    )
    sentinel_app = object()
    monkeypatch.setattr(app_module, "_import_web_server_app", lambda: sentinel_app)

    result = app_module._build_fastapi_app(tmp_path, store=_FakeStore())

    assert result is sentinel_app
    # Order matters: config must be seeded and the hook verified *before*
    # the token is touched or the app is imported (D-14 -- fail closed
    # before anything else happens).
    assert calls == [
        ("seed_config", tmp_path),
        ("verify",),
        ("seed_token", tmp_path),
    ]


def test_build_fastapi_app_propagates_startup_error(tmp_path, monkeypatch):
    def _raise():
        raise app_module.bootstrap.DashboardStartupError("hook not registered")

    monkeypatch.setattr(app_module.bootstrap, "seed_config_yaml", lambda home: True)
    monkeypatch.setattr(app_module.bootstrap, "verify_pretool_hooks_registered", _raise)

    import pytest

    with pytest.raises(app_module.bootstrap.DashboardStartupError, match="hook not registered"):
        app_module._build_fastapi_app(tmp_path, store=_FakeStore())
