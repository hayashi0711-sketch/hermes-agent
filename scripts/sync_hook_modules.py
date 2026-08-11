"""scripts/sync_hook_modules.py — modal_hub/core/ の共有モジュールを hh_hooks/ へ複製する。

設計上の位置づけ:
    docs/hh-agent/05_Phase1a_Spec.md §5.3「risk.py の複製同期」・§3（canonical.py の扱い）:

        「scripts/sync_hook_modules.py が core/risk.py と core/canonical.py を
         hh_hooks/ へコピーし、先頭に '# GENERATED FILE - DO NOT EDIT' を付与する。」

このスクリプトが行うこと（それだけ）:
    1. modal_hub/core/risk.py を読み込み、生成ヘッダを付与して hh_hooks/risk.py へ書き込む。
       risk.py は Phase1a の安全性ゲートの中核（HIGH/MEDIUM/LOW 判定）であり必須。
       存在しない場合はエラーで終了する（黙って空の複製を作らない）。
    2. modal_hub/core/canonical.py が **存在すれば** 同様に hh_hooks/canonical.py へコピーする。
       このファイルは本タスク実行時点 (2026-08-11) では未実装（所有者未割当 — 詳細は
       このモジュールの docstring 末尾「既知のギャップ」を参照）。存在しない場合は
       エラーにせず、その旨を標準出力に明示して正常終了する。
       hh_hooks/tool_gate.py 側は canonical モジュールの不在を検出して HIGH リスクの
       shell 承認フローをフェイルクローズ（deny）する設計になっている。

このスクリプトが行わないこと:
    - hh_hooks/risk.py・hh_hooks/canonical.py の内容を書き換える／手を加える
      （生成物であり、常に modal_hub/core/ 側が正。差分検出は
      modal_hub/tests/test_hook_module_sync.py の責務であり、本ファイルの所有範囲外）。
    - modal_hub/ 配下のいずれのファイルも編集しない（読み込み専用）。

3. modal_hub/core/risk_rules.yaml を hh_hooks/risk_rules.yaml へコピーする。
   **これは 05_Phase1a_Spec.md §5.3 / 03_Architecture.md のどこにも明記されていない
   追加の同期対象だが、実機検証で判明した必須の依存関係である**: 生成された
   hh_hooks/risk.py は ``Path(__file__).resolve().with_name("risk_rules.yaml")``
   （modal_hub/core/risk.py 側の実装。本タスクの所有範囲外のため書き換えない）で
   ルールファイルを「自分と同じディレクトリの兄弟ファイル」として探すため、
   risk.py だけを複製しても risk_rules.yaml が hh_hooks/ に無ければ
   ``FileNotFoundError`` で必ず落ちる（2026-08-11 実機確認済み）。
   YAML はコード生成物ではなくデータなので Python の生成ヘッダは付けず、
   YAML コメント形式の同等の注記を先頭に挿入する。

== 既知のギャップ（実装時に判明。BLOCKED として報告済み） ==

1. `docs/hh-agent/04_Task_Allocation.md` の Phase 1a 表には modal_hub/core/canonical.py の
   所有者が**どちらの担当表にも存在しない**（Sonnet 5 表・MiniMax M3 表のいずれにも記載なし）。
   05_Phase1a_Spec.md §3 は canonical_json() の実装を必須としているため、これは実装着手前に
   埋めるべきだったタスク割り当ての穴である。本スクリプトは "canonical.py が無くても壊れない"
   形で書いてあるが、司令塔側で所有者を確定し実装してもらう必要がある。
2. risk_rules.yaml の同期（上記 3.）は 05_Phase1a_Spec.md / 03_Architecture.md に
   明記が無い本スクリプト独自の実装判断である。仕様を変更するものではなく
   （ファイルの中身・値・順序には一切手を触れない、単なる複製先の追加）、
   risk.py を「書き換えず再利用する」という制約の中で実機テストが通るようにするための
   必然的な補完措置として報告する。
"""

from __future__ import annotations

import sys
from pathlib import Path

GENERATED_HEADER = "# GENERATED FILE - DO NOT EDIT\n"
GENERATED_HEADER_YAML = (
    "# GENERATED FILE - DO NOT EDIT\n"
    "# 複製元: modal_hub/core/risk_rules.yaml (scripts/sync_hook_modules.py が生成)\n"
)

# このファイル自身の場所から repo ルートを固定的に求める。
# (scripts/sync_hook_modules.py -> 1つ上が repo ルート)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORE_DIR = _REPO_ROOT / "modal_hub" / "core"
_DEST_DIR = _REPO_ROOT / "hh_hooks"

# (元ファイル名, 必須かどうか) のリスト。
# risk.py は Phase1a の安全性ゲートの中核であり必須。
# canonical.py は §3 で「core/canonical.py があれば」複製すると規定されており、
# 未実装でも同期自体は失敗させない（tool_gate.py 側がその不在を検出してフェイルクローズする）。
_PY_MODULES = (
    ("risk.py", True),
    ("canonical.py", False),
)

# risk.py が実行時に兄弟ファイルとして要求するデータファイル（上記 docstring の
# 「既知のギャップ」参照）。risk.py が必須である以上、これも必須。
_DATA_FILES = (
    ("risk_rules.yaml", True),
)


class SyncError(RuntimeError):
    """必須モジュールが見つからない等、同期を継続できない場合に送出する。"""


def _generated_copy(source_path: Path, header: str) -> str:
    """元ファイルの内容の先頭に生成ヘッダを付与したテキストを返す。

    元ファイルの最初の文がトリプルクォートで囲われた module docstring であっても、
    Python はコメント行の後の文字列リテラルを引き続き docstring として扱うため、
    先頭にコメント行を1本追加するだけで安全にラップできる。
    """
    original = source_path.read_text(encoding="utf-8")
    return header + original


def _sync_one(filename: str, required: bool, header: str, missing_required: list) -> None:
    source_path = _CORE_DIR / filename
    dest_path = _DEST_DIR / filename

    if not source_path.is_file():
        if required:
            missing_required.append(str(source_path))
            print(f"[sync_hook_modules] ERROR: 必須ファイルが見つからない: {source_path}")
        else:
            print(
                f"[sync_hook_modules] SKIP: {source_path} は存在しない"
                "（未実装。任意モジュールのため同期をスキップする）"
            )
        return

    content = _generated_copy(source_path, header)
    dest_path.write_text(content, encoding="utf-8", newline="\n")
    print(f"[sync_hook_modules] OK: {source_path} -> {dest_path}")


def sync() -> int:
    """同期を実行する。成功したら 0、必須ファイル欠落なら 1 を返す（例外は投げない）。"""
    _DEST_DIR.mkdir(parents=True, exist_ok=True)

    missing_required: list = []

    for filename, required in _PY_MODULES:
        _sync_one(filename, required, GENERATED_HEADER, missing_required)

    for filename, required in _DATA_FILES:
        _sync_one(filename, required, GENERATED_HEADER_YAML, missing_required)

    if missing_required:
        print(
            "[sync_hook_modules] 同期は失敗した。上記の必須ファイルを用意してから"
            "再実行すること。"
        )
        return 1

    return 0


def main() -> int:
    try:
        return sync()
    except OSError as exc:
        # ファイル I/O エラーは握りつぶさず、非ゼロ終了で明示する。
        print(f"[sync_hook_modules] ERROR: I/O エラー: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
