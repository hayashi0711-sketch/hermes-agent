from __future__ import annotations

import yaml
import pytest

from modal_dashboard import bootstrap


def test_seed_config_yaml_writes_hooks_on_first_boot(tmp_path):
    wrote = bootstrap.seed_config_yaml(tmp_path)

    assert wrote is True
    config_path = tmp_path / "config.yaml"
    assert config_path.is_file()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["hooks_auto_accept"] is True
    hooks = data["hooks"]["pre_tool_call"]
    assert len(hooks) == 1
    assert hooks[0]["command"] == "python /opt/hermes/hh_hooks/tool_gate.py"
    assert hooks[0]["matcher"] == ".*"
    assert hooks[0]["fail_closed"] is True
    assert hooks[0]["timeout"] == 200


def test_seed_config_yaml_does_not_overwrite_existing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("hooks_auto_accept: false\n", encoding="utf-8")

    wrote = bootstrap.seed_config_yaml(tmp_path)

    assert wrote is False
    assert config_path.read_text(encoding="utf-8") == "hooks_auto_accept: false\n"


def test_verify_pretool_hooks_registered_raises_when_not_ok(monkeypatch):
    from hh_hooks.startup_guard import DiagnosisResult

    def fake_diagnose(*args, **kwargs):
        return DiagnosisResult(ok=False, reasons=["no pre_tool_call hook is registered"], registered_count=0)

    monkeypatch.setattr("hh_hooks.startup_guard.diagnose_pretool_hooks", fake_diagnose)

    with pytest.raises(bootstrap.DashboardStartupError, match="no pre_tool_call hook is registered"):
        bootstrap.verify_pretool_hooks_registered()


def test_verify_pretool_hooks_registered_passes_when_ok(monkeypatch):
    from hh_hooks.startup_guard import DiagnosisResult

    def fake_diagnose(*args, **kwargs):
        return DiagnosisResult(ok=True, reasons=[], registered_count=1)

    monkeypatch.setattr("hh_hooks.startup_guard.diagnose_pretool_hooks", fake_diagnose)

    bootstrap.verify_pretool_hooks_registered()  # must not raise
