from __future__ import annotations

import inspect
import re

from modal_dashboard import app as app_module


def _sync_dashboard_skills_source() -> str:
    """sync_dashboard_skills のソースを返す。

    `@app.function(...)` でラップされた関数は modal.functions.Function に
    なっており `__code__` を持たないため inspect.getsource() が直接使えない
    （TypeError: ... was expected, got Function）。モジュールソースから
    def 行で始まる関数ブロックを抽出する（Modal 上では entrypoint が
    /root/app.py にマウントされるため __file__ 依存が動かない — その回帰を
    ソース検査で防ぐ）。
    """
    module_src = inspect.getsource(app_module)
    match = re.search(
        r"^def sync_dashboard_skills\(.*?\):(?P<body>.*?)(?=^def |\Z)",
        module_src,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "sync_dashboard_skills のソースが見つからない"
    return "def sync_dashboard_skills():" + match.group("body")


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


def test_sync_dashboard_skills_no_file_based_repo_root():
    # 回帰: Modal は entrypoint ファイルを /root/app.py へマウントする
    # ため、__file__ から親2階層を辿る repo_root 解決はリモートで / になり
    # hh_skill_sync の import が失敗する。ソース内に Path(__file__) による
    # 解決が残っていないことを確認する。
    source = _sync_dashboard_skills_source()
    assert "Path(__file__)" not in source


def test_sync_dashboard_skills_repo_root_hardcoded_to_opt_hermes():
    # 関数を実行せずソース検査のみで、repo_root が /opt/hermes への
    # ハードコード参照になっていること（= リモートでも正しいパスに
    # 解決される）を確認する。
    source = _sync_dashboard_skills_source()
    assert re.search(r'repo_root = _Path\((["\'])/opt/hermes\1\)', source), (
        "repo_root は _Path(\"/opt/hermes\") のハードコードに置き換わっていること"
    )
