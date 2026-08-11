"""hh_hooks/journal.py — post_tool_call ジャーナル追記フック（Phase 1b）。

設計上の位置づけ:
    - 実装契約   docs/hh-agent/07_Phase1b_Spec.md §3.1（抽出条件①②③はこの
      ジャーナルの `status`/`tool_name` のみで判定し、LLM には渡さない）
    - 担当表     docs/hh-agent/04_Task_Allocation.md Phase 1b 表
      「post_tool_call の status/error_type/duration_ms をジャーナルへ追記。
      フェイルオープンでよい」

== tool_gate.py との違い: フェイルオープン ==

tool_gate.py（PreToolUse/pre_tool_call）はフェイルクローズが絶対原則だが、
このファイルは **承認ゲートではない**。ジャーナルの欠落は「そのセッションが
Phase 1b の抽出候補から漏れる」以上の実害を持たず、ツール実行そのものを
止める権限を持たせてはならない（04_Task_Allocation.md の該当行）。したがって
`main()` は何が起きても例外を外に漏らさず、常に exit 0 で終了する。

== ワイヤプロトコル ==

`agent/shell_hooks.py` のモジュール docstring（``post_tool_call`` セクション）
が定める形:

    トップレベル: hook_event_name / tool_name / session_id / cwd / extra
    extra 内:     result / status / error_type / error_message / duration_ms /
                  task_id / tool_call_id / turn_id / api_request_id /
                  middleware_trace

Claude Code 側の PostToolUse も同一ワイヤプロトコル（D-07）で届く前提だが、
念のため `tool_call_id` 等はトップレベルにも extra 内にも両方存在しうる
ものとして両方を探す（tool_gate.py の `_extract_call_id()` と同じ方針）。

== このファイルが独自に決めた設計判断 ==

1. **保存場所とファイル名**: `07_Phase1b_Spec.md` はジャーナルの保存場所を
   規定していない。`~/.hh-agent/journal/<sha256(session_id)[:40]>.jsonl` を
   採用した。`session_id` をそのままファイル名にしないのは、ホストが供給する
   `session_id` の文字種が保証されていないため（tool_gate.py 同様、ホスト
   入力を信頼しない）。`session_end_distill.py`/`skill_distiller.py` は
   `journal_path_for_session()` を必ず import して使うこと — ハッシュ規則を
   2 箇所に手書きで複製すると将来の食い違いを生む（`core/store.py` の
   キービルダー import 方針と同じ理由）。
2. **1 行の形**: `{"tool_call_id", "tool_name", "status", "error_type",
   "duration_ms", "recorded_at"}`。`session_id` は行内容に含めない
   （ファイル自体がセッションごとに分かれているため冗長）。
3. **サイズ上限を設けない**: 単一ユーザー・ローカル運用であり、1 セッションの
   ツール呼び出し数は実運用上高々数百件。`07_Phase1b_Spec.md` §1.2 の
   診断ログ（1MB 上限）のような明示規定が無いため、無い機能を先回りして
   作らない（YAGNI）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

_HH_AGENT_HOME_ENV = "USERPROFILE"


def hh_agent_home() -> Path:
    """``%USERPROFILE%\\.hh-agent`` を返す（tool_gate.py の同名関数と同じ規則）。"""
    userprofile = os.environ.get(_HH_AGENT_HOME_ENV)
    base = Path(userprofile) if userprofile else Path.home()
    return base / ".hh-agent"


def journal_dir(base: Optional[Path] = None) -> Path:
    return (base or hh_agent_home()) / "journal"


def journal_path_for_session(session_id: str, *, base: Optional[Path] = None) -> Path:
    """セッションごとのジャーナルファイルパスを返す。

    ``session_id`` は信頼できないホスト入力のため、ファイル名には
    ``sha256(session_id)`` の先頭 40 桁だけを使う（パストラバーサル対策。
    元の値は行内容にも含めない設計にしたため、ファイル名の可逆性は不要）。
    このハッシュ規則は本モジュールの唯一の正であり、
    ``hh_hooks/session_end_distill.py`` と ``modal_hub/services/
    skill_distiller.py`` はこの関数を import して使うこと（手書き複製禁止）。
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:40]
    return journal_dir(base) / f"{digest}.jsonl"


def read_journal_entries(session_id: str, *, base: Optional[Path] = None) -> List[dict]:
    """セッションのジャーナル行を順序どおり（＝発生順）に読み込む。

    ファイルが存在しない場合は空リスト（「ジャーナルが無い」＝
    「抽出条件を満たす証拠が無い」として preflight 側で不合格に倒すのが
    呼び出し側の責務であり、ここでは例外にしない）。壊れた行は無視して
    継続する（フェイルオープンの精神をジャーナル書き込み側だけでなく
    読み込み側にも適用する）。
    """
    path = journal_path_for_session(session_id, base=base)
    if not path.is_file():
        return []
    entries: List[dict] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def append_journal_entry(session_id: str, entry: dict, *, base: Optional[Path] = None) -> None:
    """1 行追記する。呼び出し側（``main()``）が例外を握りつぶす前提の関数
    なので、ここでは意図的に例外を透過させる（呼び出し側の一箇所で
    フェイルオープンを保証する方が、書き込み経路を分散させるより検証しやすい）。
    """
    path = journal_path_for_session(session_id, base=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _extract_call_id(request: dict) -> Optional[str]:
    for key in ("tool_call_id", "tool_use_id", "id"):
        value = request.get(key)
        if isinstance(value, str) and value:
            return value
    extra = request.get("extra")
    if isinstance(extra, dict):
        for key in ("tool_call_id", "tool_use_id"):
            value = extra.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _extract_field(request: dict, key: str) -> Any:
    """トップレベルと ``extra`` の両方を探す（extract order: top-level 優先）。"""
    if key in request:
        return request[key]
    extra = request.get("extra")
    if isinstance(extra, dict) and key in extra:
        return extra[key]
    return None


def _build_entry(request: dict) -> Optional[dict]:
    tool_name = request.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None

    status = _extract_field(request, "status")
    if not isinstance(status, str) or status not in ("ok", "error", "blocked"):
        # status が想定外の形なら記録しない（preflight 側が誤って
        # 「成功終了」等と誤判定する材料を残さない）。
        return None

    error_type = _extract_field(request, "error_type")
    if error_type is not None and not isinstance(error_type, str):
        error_type = None

    duration_ms = _extract_field(request, "duration_ms")
    if not isinstance(duration_ms, (int, float)):
        duration_ms = None

    tool_call_id = _extract_call_id(request)

    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "status": status,
        "error_type": error_type,
        "duration_ms": duration_ms,
        "recorded_at": time.time(),
    }


def main() -> None:
    """フックのエントリポイント。何が起きても exit 0（フェイルオープン）。"""
    try:
        raw = sys.stdin.read()
        if not raw or not raw.strip():
            return
        request = json.loads(raw)
        if not isinstance(request, dict):
            return

        session_id = request.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return

        entry = _build_entry(request)
        if entry is None:
            return

        append_journal_entry(session_id, entry)
    except Exception:  # noqa: BLE001 — フェイルオープンの絶対原則（承認ゲートではない）
        pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
