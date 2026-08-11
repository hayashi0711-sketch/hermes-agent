"""modal_hub/services/session_reader.py — SessionDB の読み取り専用ラッパー（Phase 1b）。

設計上の位置づけ:
    - 実装契約   docs/hh-agent/07_Phase1b_Spec.md §0.2 item4（メッセージ単位の
      20KB 切り詰め）、§3.2（既存スキルとの重複判定・SKILL.md 生成に渡す
      会話内容の取得元）
    - 担当表     docs/hh-agent/04_Task_Allocation.md Phase 1b 表
      「SessionDB.get_session()/get_messages() の読み取り。active=1 の扱い、
      timestamp ではなく id 順を間違えない」

== ローカル専用（D-17） ==

Distiller 系はすべてローカル PC でのみ動く。このモジュールは Modal コンテナ内
からは一度も import されない（``modal_hub/main.py`` が結線するルータは
``approval_gate``/``skills`` のみで、いずれもこのモジュールに依存しない）。
``hermes_state.SessionDB`` はリポジトリルート直下のトップレベルモジュールで
あり、ローカル実行時（``python scripts/hh_distill.py`` 等）はリポジトリ
ルートが cwd もしくは sys.path 上にある前提で import できる。

== 絶対に守る 2 点（test_phase1b_guards.py が固定する不変条件） ==

1. **メッセージの並びは AUTOINCREMENT `id` 昇順**（``timestamp`` ではない）。
   ``hermes_state.SessionDB.get_messages()`` が既にこの順序で返す
   （WSL2 のクロック巻き戻り対策。``hermes_state.py`` 内のコミット
   ``c03acca50`` 参照）ため、本モジュールは並び替えを一切行わない
   （並び替えを追加するとこの保証を壊しかねない）。
2. **`sessions.end_reason` を一切参照しない。** 抽出条件①（成功終了）の
   判定材料は `hh_hooks/journal.py` が記録した `post_tool_call` の
   `status` のみであり（`07_Phase1b_Spec.md` §3.1）、`SessionMetadata` は
   意図的に `end_reason` フィールドを持たない。「セッションの終わり方」を
   このモジュール経由で成功/失敗判定に使えるようにしてしまうと、
   `services/skill_distiller.py`（別所有者）が誤ってそちらを判定材料に
   使う余地を作ってしまう。

== このモジュールが独自に決めた設計判断 ==

- **§0.2 item4 のメッセージ単位 20KB 切り詰めをこの読み取り層で強制する。**
  仕様は「``get_messages()`` の結果はメッセージ単位で 1 件あたり 20KB を
  超える content/tool_calls 結果を切り詰める」とだけ書いており、どの層で
  行うかは明記していない。呼び出し側（`services/skill_distiller.py`、
  MiniMax 所有）がこの上限適用を忘れるとトークン予算・redaction コストが
  無制限に膨らむ安全上のガードなので、読み取りの入口であるここで確実に
  適用する（呼び出し側で「思い出す」ことに依存しない）。**redaction
  （`core/redact.py` の適用）はここでは行わない** — §0.2 item3 は
  「Batch リクエストへ含める直前」に適用すると規定しており、これは
  `skill_distiller.py` 側の責務（このモジュールは SessionDB からの
  生読み取りとサイズ上限の適用のみを担当し、Batch リクエスト構築の文脈を
  持たない）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_MAX_MESSAGE_BYTES = 20 * 1024


class SessionReaderError(RuntimeError):
    """SessionDB アクセスに失敗した。呼び出し側は該当キューエントリを
    `failed/` へ倒すこと（フェイルオープンではあるが、読み取り自体の失敗は
    「セッションが存在しない」とは別種のエラーなので握りつぶさない）。
    """


@dataclass(frozen=True)
class SessionMetadata:
    session_id: str
    cwd: Optional[str]
    git_repo_root: Optional[str]
    git_branch: Optional[str]
    started_at: Optional[float]
    ended_at: Optional[float]


@dataclass(frozen=True)
class SessionMessage:
    id: int
    role: str
    content: Any  # str（通常）。20KB を超えた場合は切り詰め済みの str に
    #                 正規化される（元が list/dict の multimodal でも同じ）。
    tool_name: Optional[str]
    tool_call_id: Optional[str]
    tool_calls: Any
    truncated: bool


def _open_session_db(db_path: Optional[Path] = None):
    """``hermes_state.SessionDB`` を読み取り専用で開く。

    import はここ（呼び出し時）で行う。モジュールロード時に import すると、
    このファイルが誤って Modal コンテナ側から import された場合に
    `hermes_state` の依存関係解決が起動時クラッシュの原因になりうる
    （`modal_hub/core/risk.py` が過去に同種の「import 時の即時実行」で
    クラッシュした教訓 — `08_Handoff_Note.md` 落とし穴を参照）。
    """
    import hermes_state

    return hermes_state.SessionDB(db_path=db_path, read_only=True)


def get_session_metadata(
    session_id: str, *, db_path: Optional[Path] = None
) -> Optional[SessionMetadata]:
    """セッション 1 件のメタデータを返す。存在しなければ None。

    `end_reason` は意図的に返さない（本ファイル docstring 参照）。

    Raises:
        SessionReaderError: DB のオープン、または `get_session()` 呼び出しに
            失敗した場合（`sqlite3` の生例外を外へ漏らさない — 2026-08-11
            Codex レビュー Medium 指摘の修正。`_open_session_db()` 自体も
            try 節の**外**にあると、DB ファイル不在時の
            `sqlite3.OperationalError` が契約どおりの `SessionReaderError`
            に変換されずそのまま伝播していた）。
    """
    try:
        db = _open_session_db(db_path)
    except Exception as exc:  # noqa: BLE001
        raise SessionReaderError(f"failed to open SessionDB: {type(exc).__name__}") from exc
    try:
        try:
            row = db.get_session(session_id)
        except Exception as exc:  # noqa: BLE001 — SessionDB 内部実装への依存を型で縛らない
            raise SessionReaderError(f"get_session failed: {type(exc).__name__}") from exc
    finally:
        # close() を呼ばずに戻ると SessionDB の追跡対象コネクションが
        # 残り続け、Hermes 側のバックアップ/保護系操作を後々ブロックしうる
        # （2026-08-11 Codex レビュー Medium 指摘の修正）。
        db.close()
    if row is None:
        return None
    return SessionMetadata(
        session_id=row.get("id") or session_id,
        cwd=row.get("cwd") if isinstance(row.get("cwd"), str) else None,
        git_repo_root=row.get("git_repo_root") if isinstance(row.get("git_repo_root"), str) else None,
        git_branch=row.get("git_branch") if isinstance(row.get("git_branch"), str) else None,
        started_at=row.get("started_at"),
        ended_at=row.get("ended_at"),
    )


def _truncate_text(value: str, max_bytes: int) -> Tuple[str, bool]:
    """`value` を UTF-8 で `max_bytes` バイト**以内**（マーカー込み）に
    切り詰める。

    2026-08-11 Codex レビュー Low 指摘の修正: 旧実装は本文を `max_bytes`
    まで切り詰めた**後に**マーカーを追記していたため、戻り値の総バイト数が
    `max_bytes` を超えることがあった（呼び出し側の上限保証を破る）。
    マーカー分の余白を先に見積もってから本文を切り詰め、最後に実バイト数で
    再確認する。
    """
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False

    dropped_estimate = len(encoded)
    marker_estimate = f"...[truncated {dropped_estimate} bytes]".encode("utf-8")
    content_budget = max(0, max_bytes - len(marker_estimate))

    truncated_bytes = encoded[:content_budget]
    # マルチバイト文字の境界で壊れた末尾を落としてから decode する。
    while truncated_bytes:
        try:
            text = truncated_bytes.decode("utf-8")
            break
        except UnicodeDecodeError:
            truncated_bytes = truncated_bytes[:-1]
    else:
        text = ""

    dropped = len(encoded) - len(truncated_bytes)
    result = f"{text}...[truncated {dropped} bytes]"

    # 最終確認: それでも max_bytes を超えていたら（極端に小さい max_bytes
    # や桁数見積もりのずれ）、マーカーごと機械的に切り詰める。
    result_bytes = result.encode("utf-8")
    if len(result_bytes) > max_bytes:
        result_bytes = result_bytes[:max_bytes]
        while result_bytes:
            try:
                result = result_bytes.decode("utf-8")
                break
            except UnicodeDecodeError:
                result_bytes = result_bytes[:-1]
        else:
            result = ""
    return result, True


def _truncate_content_field(content: Any, max_bytes: int) -> Tuple[Any, bool]:
    """`content` を切り詰める。文字列以外（multimodal の list/dict）も
    対象にする。

    2026-08-11 Codex レビュー Medium 指摘の修正: 旧実装は
    ``isinstance(content, str)`` の場合しか切り詰めておらず、
    `hermes_state.py` がマルチモーダルメッセージを list/dict へデコードする
    経路（`_decode_content`）が 20KB 上限を素通りしていた。文字列でない
    場合は JSON シリアライズしたバイト数で判定し、超えていれば
    シリアライズ後の文字列を切り詰めた形（`truncated=True`）で返す
    （`tool_calls` の切り詰めと同じ扱い）。
    """
    if isinstance(content, str):
        return _truncate_text(content, max_bytes)
    if content is None:
        return content, False
    serialized = json.dumps(content, ensure_ascii=False)
    if len(serialized.encode("utf-8")) <= max_bytes:
        return content, False
    truncated_text, _ = _truncate_text(serialized, max_bytes)
    return truncated_text, True


def get_session_messages(
    session_id: str,
    *,
    db_path: Optional[Path] = None,
    max_bytes_per_message: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> List[SessionMessage]:
    """アクティブメッセージ（`active=1`）を id 昇順（挿入順）で返す。

    §0.2 item4 の 1 メッセージあたり 20KB 上限を `content`/`tool_calls` の
    両方へ適用する（`content` は文字列でも multimodal の list/dict でも
    適用される）。切り詰めが起きた行は `truncated=True` になる。

    Raises:
        SessionReaderError: DB のオープン、または `get_messages()` 呼び出し
            に失敗した場合。
    """
    try:
        db = _open_session_db(db_path)
    except Exception as exc:  # noqa: BLE001
        raise SessionReaderError(f"failed to open SessionDB: {type(exc).__name__}") from exc
    try:
        try:
            rows = db.get_messages(session_id, include_inactive=False)
        except Exception as exc:  # noqa: BLE001
            raise SessionReaderError(f"get_messages failed: {type(exc).__name__}") from exc
    finally:
        db.close()

    out: List[SessionMessage] = []
    for row in rows:
        content, content_truncated = _truncate_content_field(row.get("content"), max_bytes_per_message)

        tool_calls = row.get("tool_calls")
        tool_calls_truncated = False
        if tool_calls is not None:
            serialized = json.dumps(tool_calls, ensure_ascii=False)
            if len(serialized.encode("utf-8")) > max_bytes_per_message:
                tool_calls, tool_calls_truncated = _truncate_text(serialized, max_bytes_per_message)

        out.append(
            SessionMessage(
                id=row["id"],
                role=row.get("role") or "",
                content=content,
                tool_name=row.get("tool_name") if isinstance(row.get("tool_name"), str) else None,
                tool_call_id=row.get("tool_call_id") if isinstance(row.get("tool_call_id"), str) else None,
                tool_calls=tool_calls,
                truncated=content_truncated or tool_calls_truncated,
            )
        )
    return out


__all__ = [
    "SessionReaderError",
    "SessionMetadata",
    "SessionMessage",
    "DEFAULT_MAX_MESSAGE_BYTES",
    "get_session_metadata",
    "get_session_messages",
]
