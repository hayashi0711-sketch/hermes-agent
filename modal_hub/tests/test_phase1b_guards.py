"""Phase 1b（Skill Distiller）の残存ガード。

`services/skill_distiller.py`／`scripts/hh_distill.py`（MiniMax 所有）と
`services/session_reader.py`／`services/skill_quarantine.py`／
`routers/skills.py`／`scripts/hh_skill_promote.py`（Sonnet 5 所有）は
すべて実装済みになった。対応する「存在したら失敗する」ガードはそれぞれの
実装着地と同時に削除し、代わりに `modal_hub/tests/test_distiller.py`／
`test_session_reader.py`／`test_skill_quarantine.py`／
`test_skills_router.py`／`test_skill_promote.py` へ実際の不変条件テストを
移した。

このファイルに残っているのは Phase 1c（Modal クラウドエージェント）・
Phase 2（Qwen バックエンド・音声ゲートウェイ）向けの「まだ実装しない」
ガードと、Phase 1a のコードに対して今すぐ担保できる不変条件——
**Obsidian への書き込み経路が物理的に存在しないこと**（D-12・親設計書
§2「絶対原則」）——のみ。
"""

from __future__ import annotations

import re

import pytest

# ===========================================================================
# 今すぐ担保できる不変条件: Obsidian への書き込み経路が存在しないこと
# ===========================================================================


def test_no_obsidian_path_appears_anywhere_in_the_hub_or_hooks(repo_root) -> None:
    """D-12 / 親設計書 §2「絶対原則」。

        「Skill Distiller の出力が Obsidian に書き込まれることは絶対に
          あってはならない。」

    §2 は「Modal 側から Obsidian へのパスは物理的に存在しない」ため
    アーキテクチャで担保されるとしたうえで、§8.1 でパス検証テストを課す。
    Phase 1a のコードに対しては今から実行できる。
    """
    patterns = [
        r"Obsidian",
        r"obsidian",
        r"マイドライブ",
        r"Projects[\\/]H-H-Agent",
        r"Vault",
        r"vault",
    ]
    offenders = []
    for base in ("modal_hub", "hh_hooks"):
        for path in sorted((repo_root / base).rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                if re.search(pattern, text):
                    offenders.append(f"{path.relative_to(repo_root)}: {pattern}")
    assert offenders == [], f"Obsidian への参照がある: {offenders}"


def test_no_qwen_backend_silently_falls_back(repo_root) -> None:
    """D-05 / 親設計書 §4.8: `QwenBackend` は `NotImplementedError`。

        「プライベート指定なのに黙って外部 API に送る」は情報漏洩そのもの。

    Phase 1a には `core/router.py` 自体が無い。存在したらフォールバックが
    無いことを確認する。
    """
    router = repo_root / "modal_hub" / "core" / "router.py"
    if not router.is_file():
        pytest.skip("core/router.py は Phase 1a のスコープ外（D-05）")
    text = router.read_text(encoding="utf-8")
    assert "NotImplementedError" in text
    assert "fallback" not in text.lower()


def test_voice_gateway_is_absent_or_returns_501(repo_root) -> None:
    """D-08: Phase 1 では `/api/voice/*` はルータ枠と 501 のみ。"""
    voice = repo_root / "modal_hub" / "routers" / "voice_gateway.py"
    if not voice.is_file():
        return  # Phase 1a では未着手でよい
    assert "501" in voice.read_text(encoding="utf-8")


def test_cloud_agent_router_is_absent_in_phase_1a(repo_root) -> None:
    """§4.6: Phase 1c。PoC 合否判定の前に実装を入れない。"""
    cloud = repo_root / "modal_hub" / "routers" / "cloud_agent.py"
    if cloud.is_file():
        text = cloud.read_text(encoding="utf-8")
        assert 'env_type="modal"' not in text, "D-14: 承認ガードが丸ごとスキップされる"
        assert "hermes serve" not in text, "D-18: serve は headless で SPA を配信しない"
