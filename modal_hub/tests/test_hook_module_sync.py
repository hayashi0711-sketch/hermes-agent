"""`hh_hooks/` の生成複製が `modal_hub/core/` と一致すること。

Phase1a spec §5.3:

    「`modal_hub/tests/test_hook_module_sync.py` が両者の差分を検出したら
      失敗させる。手で編集された複製を CI で落とす。」

親設計書 §4.3 の 2026-08-11 改訂も念を押している:

    「やってはいけない対処: Hermes の危険コマンド検出ロジックを `hh_hooks/`
      側へ複製・自前実装して時間を稼ぐこと。**Hermes 本体との判定のズレを
      生む**。`sync_hook_modules.py` と `test_hook_module_sync.py` は、
      まさにその複製ズレを防ぐために存在する。」
"""

from __future__ import annotations

import subprocess
import sys

import pytest

GENERATED_HEADER = "# GENERATED FILE - DO NOT EDIT\n"

# (元ファイル, 複製先, 必須か)
SYNCED = [
    ("modal_hub/core/risk.py", "hh_hooks/risk.py", True),
    ("modal_hub/core/canonical.py", "hh_hooks/canonical.py", True),
    ("modal_hub/core/risk_rules.yaml", "hh_hooks/risk_rules.yaml", True),
]


@pytest.mark.parametrize("source_rel,dest_rel,required", SYNCED)
def test_generated_copy_is_byte_identical_to_the_source(repo_root, source_rel, dest_rel, required) -> None:
    source = repo_root / source_rel
    dest = repo_root / dest_rel

    assert source.is_file(), f"正となる {source_rel} が無い"
    if not dest.is_file():
        if not required:
            pytest.skip(f"{dest_rel} は未生成（任意モジュール）")
        pytest.fail(f"{dest_rel} が無い。`python scripts/sync_hook_modules.py` を実行すること")

    src_text = source.read_text(encoding="utf-8")
    dest_text = dest.read_text(encoding="utf-8")

    assert dest_text.startswith(GENERATED_HEADER), (
        f"{dest_rel} の先頭に生成ヘッダが無い（手書きされた疑い）"
    )
    body = dest_text[len(GENERATED_HEADER) :]
    # YAML はヘッダが 2 行（Python コメント形式ではない注記が続く）。
    if dest.suffix == ".yaml":
        body = body.split("\n", 1)[1] if body.startswith("#") else body
    assert body == src_text, (
        f"{dest_rel} が {source_rel} と一致しない。"
        "複製を手で編集したか、sync_hook_modules.py の再実行を忘れている。"
        "判定のズレは承認ゲートの偽陰性そのものになる。"
    )


def test_sync_script_is_idempotent(repo_root) -> None:
    """同期スクリプトを回してもファイルが変わらない＝今が同期済みの状態。"""
    before = {rel: (repo_root / rel).read_bytes() for _s, rel, _r in SYNCED if (repo_root / rel).is_file()}
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "sync_hook_modules.py")],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = {rel: (repo_root / rel).read_bytes() for rel in before}
    assert after == before, "sync 実行でファイルが変化した＝同期されていなかった"


def test_hook_copy_of_risk_classifies_identically(repo_root) -> None:
    """バイト一致に加えて **挙動** の一致も見る。

    `risk_rules.yaml` は兄弟ファイルとして解決されるため、複製先の
    ディレクトリに YAML が無いと複製 risk.py は FileNotFoundError で落ちる。
    ここで実際に import して分類させることで、その依存も同時に検証する。
    """
    script = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        f"sys.path.insert(0, {str(repo_root / 'hh_hooks')!r})\n"
        "import risk as hook_risk\n"
        "from modal_hub.core import risk as core_risk\n"
        "cases = [('Bash', {'command': 'git push --force origin main'}),\n"
        "         ('Bash', {'command': 'npm install left-pad'}),\n"
        "         ('Bash', {'command': 'ls -la'}),\n"
        "         ('Bash', {'command': 'cat .env'}),\n"
        "         ('Write', {'file_path': 'C:/tmp/.env'}),\n"
        "         ('mcp__x__y', {})]\n"
        "out = []\n"
        "for name, arg in cases:\n"
        "    a = hook_risk.classify(name, arg)\n"
        "    b = core_risk.classify(name, arg)\n"
        "    out.append([[a.level, a.rule_id], [b.level, b.rule_id]])\n"
        "print(json.dumps(out))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(repo_root), capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stdout + result.stderr
    import json

    pairs = json.loads(result.stdout.strip().splitlines()[-1])
    for hook_result, core_result in pairs:
        assert hook_result == core_result, f"複製の判定がズレている: {hook_result} != {core_result}"


def test_hook_copy_of_canonical_hashes_identically(repo_root) -> None:
    """§3: フックとサーバで **完全に同一の実装** でなければならない。"""
    script = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        f"sys.path.insert(0, {str(repo_root / 'hh_hooks')!r})\n"
        "import canonical as hook_canonical\n"
        "from modal_hub.core import canonical as core_canonical\n"
        "payloads = [{'command': 'git push --force'},\n"
        "            {'command': '日本語のコマンド', 'n': 1, 'b': True, 'z': None},\n"
        "            {'targets': [{'path': 'C:/x', 'exists': True}]}]\n"
        "print(json.dumps([[list(hook_canonical.payload_hashes(p)),\n"
        "                   list(core_canonical.payload_hashes(p))] for p in payloads]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(repo_root), capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stdout + result.stderr
    import json

    for hook_hashes, core_hashes in json.loads(result.stdout.strip().splitlines()[-1]):
        assert hook_hashes == core_hashes


def test_risk_rules_yaml_is_shipped_next_to_the_hook_copy(repo_root) -> None:
    """`risk.py` は `Path(__file__).with_name("risk_rules.yaml")` で兄弟を探す。

    risk.py だけを複製して YAML を置き忘れると、フックは毎回
    FileNotFoundError で落ちる＝フェイルクローズで全ツールが deny になる。
    """
    assert (repo_root / "hh_hooks" / "risk_rules.yaml").is_file()


def test_hook_directory_contains_no_hand_written_duplicate_logic(repo_root) -> None:
    """親設計書 §4.3: 検出ロジックを hh_hooks 側へ自前実装しない。

    `hh_hooks/` にある `.py` は、生成物か、生成物でないと明示された
    ファイル（tool_gate.py / journal.py）だけであること。
    """
    allowed_hand_written = {
        "tool_gate.py",
        "journal.py",
        "session_end_distill.py",
        "startup_guard.py",
        "__init__.py",
    }
    for path in sorted((repo_root / "hh_hooks").glob("*.py")):
        if path.name in allowed_hand_written:
            continue
        text = path.read_text(encoding="utf-8")
        assert text.startswith(GENERATED_HEADER), (
            f"{path.name} は生成物でも許可された手書きファイルでもない"
        )


def test_tool_gate_does_not_reimplement_dangerous_command_detection(repo_root) -> None:
    """`tool_gate.py` が自前の危険コマンド正規表現を持っていないこと。"""
    text = (repo_root / "hh_hooks" / "tool_gate.py").read_text(encoding="utf-8")
    for marker in ("rm -rf", "rm\\s+-rf", "git\\s+push", "detect_dangerous_command"):
        assert marker not in text.split('"""')[-1], (
            f"tool_gate.py が独自の危険コマンド判定 ({marker}) を持っている"
        )
