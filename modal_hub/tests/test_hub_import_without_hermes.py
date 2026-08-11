"""modal_hub がリポジトリルート（Hermes 本体）を持たない環境でも import できること。

背景（2026-08-11、Modal デプロイで実際に発生した障害）:

    Modal イメージには `modal_hub/` と `mobile_app/pwa_approval/` しか焼き込まれず、
    `tools/approval.py` / `hermes_cli/` を含む Hermes 本体は存在しない。
    05_Phase1a_Spec.md §1.2 の通りサーバは risk 判定を再計算しない設計であり、
    `modal_hub/routers/approval_gate.py` は `core/risk.py` を表示文言の
    `_load_rules()` にしか使わず `classify()` は一度も呼ばない。

    にもかかわらず `core/risk.py` はかつて `_find_repo_root()` と `sys.path` の
    設定をモジュール import 時に**即時実行**しており、Hermes 本体が存在しない
    Modal コンテナでは `approval_gate` の import そのものが `RuntimeError` で
    失敗し、承認リクエストのたびに Hub 全体がクラッシュしていた。

    修正: `_find_repo_root()` / `sys.path` の設定を、既存の遅延 import 関数
    `_get_detect_dangerous_command()`（shell カテゴリの分類が実際に呼ばれた
    時点で初めて実行される）の中へ移動した。

    このテストは「Hermes 本体が存在しない環境で `approval_gate` が import
    できること」を固定し、この障害の再発を検出する。単一プロセス内の
    monkeypatch では `sys.modules` にキャッシュされた本物の `tools.approval`
    を隠せないため、サブプロセスで隔離コピーを使って検証する（Codex レビュー
    2026-08-11 の指摘）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize("copied_dirs", [("modal_hub",)])
def test_approval_gate_imports_without_hermes_repo(
    repo_root: Path, tmp_path: Path, copied_dirs: tuple
) -> None:
    """Hub 相当の隔離環境（Hermes 本体なし）で approval_gate の import が成功する。"""
    isolated = tmp_path / "hub_isolated"
    isolated.mkdir()

    for rel in copied_dirs:
        shutil.copytree(
            repo_root / rel,
            isolated / rel,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    # 隔離コピーの祖先に tools/approval.py・hermes_cli/ が存在しないことが前提。
    assert not (isolated / "tools" / "approval.py").exists()
    assert not (isolated / "hermes_cli").exists()

    probe = textwrap.dedent(
        """
        import sys
        from modal_hub.routers import approval_gate

        # 表示文言ヘルパー（_load_rules 経由）は Hermes 抜きで動く。
        reason = approval_gate._reason_for_rule_id("any_push")
        assert reason, "risk_rules.yaml からの表示文言取得に失敗した"

        # classify() を server 側で呼んでいないことの間接証跡：
        # Hermes 検出器 (tools.approval) が一度も import されていない。
        assert "tools.approval" not in sys.modules

        print("OK")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=isolated,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        "Hermes 本体が無い環境で approval_gate の import が失敗した"
        "（Modal Hub クラッシュの再発）。\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout
