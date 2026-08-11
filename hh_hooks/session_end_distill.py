"""hh_hooks/session_end_distill.py — on_session_end フック: Phase 1b キュー登録（段階1）。

設計上の位置づけ:
    - 実装契約   docs/hh-agent/07_Phase1b_Spec.md §1（起動契機・段階1）、
      §0.2（除外ルート判定・fail-closed）
    - 担当表     docs/hh-agent/04_Task_Allocation.md Phase 1b 表
      「on_session_end フック。キュー登録のみ。Batch 投入はしない・
      フェイルオープン」

== このフックがやること・やらないこと ==

**やる**: `queue_entry_id` を計算し、まだどの状態ディレクトリにも同じ ID の
ファイルが無く、かつ cwd が除外ルート配下でなければ、`pending/` へ
排他作成で 1 ファイルだけ書く。これだけ。

**やらない**: `git diff` の取得・LLM 呼び出し・Batch API 投入・抽出条件
①②③の判定。すべて段階2（`scripts/hh_distill.py run`、MiniMax 所有）の
責務（§1.4）。

== フェイルオープン、ただし除外ルート判定自体は fail-closed ==

一見矛盾するが役割が違う。「このフック全体がツール実行やセッション終了を
妨げてはならない」という意味でフェイルオープン（例外はすべて
`_log_enqueue_error` へ記録した上で exit 0）。一方「`excluded_roots` が
読めない／未設定のときにキュー登録してよいか」という**個別の判断**は
fail-closed（＝登録を拒否する）。§0.2 item1 の「除外ルートが分からない
＝何でも許可、を絶対にしない」はこの後者を指す。両者は独立した話であり、
「fail-closed でキュー登録を拒否する」という判断そのものは正常系の一部
として exit 0 で終わる（フックとしての失敗ではない）。

== このファイルが独自に決めた設計判断 ==

1. **状態ディレクトリ内のファイルパス検証**: §1.3 最終段落
   「すべてのファイル操作は対象パスの ``Path(...).resolve()`` が対応する
   状態ディレクトリ直下（直接の子）であることを確認してから行う」を
   `_safe_state_file_path()` として実装した。`queue_entry_id` 自体は
   `^[a-zA-Z0-9_-]{1,64}$` に一致する値しか生成されない（`compute_
   queue_entry_id()` 参照）ためパストラバーサル文字列にはなり得ないが、
   仕様が明示的に要求する多層防御としてこのチェックを実施する。
   `scripts/hh_distill.py`（MiniMax 所有・段階2）はこのファイルの
   `compute_queue_entry_id()` / `distill_queue_dir()` / `QUEUE_STATES` /
   `_safe_state_file_path()` 相当のロジックを再利用または完全に同一の
   規則で実装すること（規則の複製は許すが、規則そのものの食い違いは
   許さない — `hh_hooks/journal.py` のハッシュ規則と同じ理由）。
2. **enqueue_errors.log の 1MB ローテーション**: 「超過したら古い方から
   切り詰める」の具体的な実装として、既存ファイルが上限を超えていたら
   直近 50% 相当のバイト数だけを末尾から残し、行境界に合わせてから
   新しい行を追記する。厳密なログ管理システムではなく「無限に肥大化
   しない」ことが目的のため、これで十分と判断した。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_HH_AGENT_HOME_ENV = "USERPROFILE"

QUEUE_STATES = ("pending", "submitting", "submitted", "completed", "failed")
ENQUEUE_ERROR_LOG_MAX_BYTES = 1024 * 1024


def hh_agent_home() -> Path:
    userprofile = os.environ.get(_HH_AGENT_HOME_ENV)
    base = Path(userprofile) if userprofile else Path.home()
    return base / ".hh-agent"


def distill_queue_dir(base: Optional[Path] = None) -> Path:
    return (base or hh_agent_home()) / "distill_queue"


def compute_queue_entry_id(session_id: str, turn_id: Optional[str]) -> str:
    """07_Phase1b_Spec.md §1.3 のとおり。JSON 配列としてエンコードしてから
    ハッシュする（文字列結合の衝突を避けるため）。
    """
    key = json.dumps([session_id, turn_id or ""], separators=(",", ":"))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return "s" + digest


class PathEscapesStateDirError(RuntimeError):
    pass


def _safe_state_file_path(state_dir: Path, queue_entry_id: str) -> Path:
    """``state_dir`` 直下の ``<queue_entry_id>.json`` を返す。

    ``resolve()`` した結果の親が ``state_dir`` の ``resolve()`` と一致しない
    場合（symlink・reparse point・孫階層への迂回等）は拒否する。

    **2026-08-11 Codex レビュー Critical 指摘の修正**: 上記の
    resolve()-一致チェックだけでは、``state_dir`` 自体が symlink/junction
    （例: ``pending/`` そのものが外部ディレクトリへの junction）の場合に
    抜け穴になる — ``candidate.resolve().parent`` と ``state_dir.resolve()``
    はどちらも同じ junction の解決先を指すため一致してしまい、チェックを
    素通りする。``state_dir`` 自身が symlink/junction でないことを
    **先に**確認することで閉じる（``Path.is_symlink()`` は Windows の
    ディレクトリ junction も検出する — reparse point の判定であり
    symlink 限定ではない）。

    完全な TOCTOU 除去（このチェックと実際の ``open()`` の間で
    ``state_dir`` が置き換えられる競合）は Windows で `O_NOFOLLOW` 相当が
    無いため構造的に閉じられない。単一ユーザー・ローカル運用という
    前提のもとで許容する残存リスクとして記録する
    （`scripts/hh_skill_promote.py` の同種の判断と同じ扱い）。
    """
    if state_dir.is_symlink():
        raise PathEscapesStateDirError(
            f"{state_dir} 自体が symlink/junction。書き込みを拒否する"
        )
    candidate = state_dir / f"{queue_entry_id}.json"
    resolved_parent = state_dir.resolve()
    if candidate.resolve().parent != resolved_parent:
        raise PathEscapesStateDirError(
            f"{candidate} は {state_dir} の直接の子ではない"
        )
    return candidate


def _existing_entry_path(queue_entry_id: str, *, base: Optional[Path] = None) -> Optional[Path]:
    qdir = distill_queue_dir(base)
    for state in QUEUE_STATES:
        state_dir = qdir / state
        if not state_dir.is_dir():
            continue
        try:
            candidate = _safe_state_file_path(state_dir, queue_entry_id)
        except PathEscapesStateDirError:
            continue
        if candidate.is_file():
            return candidate
    return None


class ExcludedRootsNotConfiguredError(RuntimeError):
    """§0.2 item1: `excluded_roots` キーが読めない／存在しない。呼び出し側は
    キュー登録を拒否すること（fail-closed）。"""


def load_excluded_roots(base: Optional[Path] = None) -> List[str]:
    config_path = (base or hh_agent_home()) / "config.json"
    if not config_path.is_file():
        raise ExcludedRootsNotConfiguredError(f"{config_path} が存在しない")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExcludedRootsNotConfiguredError(
            f"config.json の読み込みに失敗: {type(exc).__name__}"
        ) from exc
    if not isinstance(data, dict) or "excluded_roots" not in data:
        raise ExcludedRootsNotConfiguredError("excluded_roots キーが無い")
    roots = data["excluded_roots"]
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        raise ExcludedRootsNotConfiguredError("excluded_roots は文字列配列でなければならない")
    return roots


def is_excluded_cwd(cwd_realpath: str, excluded_roots: List[str]) -> bool:
    """`cwd_realpath` が `excluded_roots` のいずれかと一致 or その配下か。

    Windows のファイルシステム大文字小文字非区別に対応するため
    ``os.path.normcase`` で正規化してから比較する。
    """
    cwd_norm = os.path.normcase(cwd_realpath)
    for root in excluded_roots:
        try:
            root_real = os.path.normcase(os.path.realpath(root))
        except OSError:
            continue
        try:
            common = os.path.commonpath([cwd_norm, root_real])
        except ValueError:
            continue  # 別ドライブ等、共通パスが無い
        if common == root_real:
            return True
    return False


def _truncate_log_if_oversized(log_path: Path) -> None:
    try:
        size = log_path.stat().st_size
    except OSError:
        return
    if size <= ENQUEUE_ERROR_LOG_MAX_BYTES:
        return
    try:
        raw = log_path.read_bytes()
    except OSError:
        return
    keep_from = len(raw) - (ENQUEUE_ERROR_LOG_MAX_BYTES // 2)
    tail = raw[max(0, keep_from):]
    newline_pos = tail.find(b"\n")
    if newline_pos != -1:
        tail = tail[newline_pos + 1 :]
    log_path.write_bytes(tail)


def _log_enqueue_error(queue_entry_id: Optional[str], error: str, *, base: Optional[Path] = None) -> None:
    log_path = distill_queue_dir(base) / "enqueue_errors.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.is_file():
            _truncate_log_if_oversized(log_path)

        try:
            from modal_hub.core import redact

            safe_error = redact.redact_text(error)
        except Exception:  # noqa: BLE001 — redact 不能でも診断ログ自体は残す
            # 2026-08-11 Codex レビュー Medium 指摘の修正: redaction に
            # 失敗したからといって未redactの原文を書いてしまうと、§1.2 が
            # 要求する「redact.py 適用済み」を破る。generic なプレース
            # ホルダへ倒す（診断ログとしての最低限の価値——エラーが起きた
            # という事実——は残しつつ、内容は一切書かない）。
            safe_error = "<redaction unavailable; original message withheld>"

        line = json.dumps(
            {"at": time.time(), "queue_entry_id": queue_entry_id, "error": safe_error},
            ensure_ascii=False,
        )
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _write_pending_entry(
    queue_entry_id: str,
    *,
    session_id: str,
    turn_id: Optional[str],
    completed: bool,
    interrupted: bool,
    cwd_realpath: str,
    base: Optional[Path] = None,
) -> None:
    pending_dir = distill_queue_dir(base) / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    target = _safe_state_file_path(pending_dir, queue_entry_id)

    payload = {
        "queue_entry_id": queue_entry_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "completed": bool(completed),
        "interrupted": bool(interrupted),
        "cwd": cwd_realpath,
    }
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)

    # 排他作成（§1.1 手順5）。"xb" は OS レベルで O_CREAT|O_EXCL 相当の
    # 原子性を持つ。既に存在する場合は _existing_entry_path() のチェックを
    # すり抜けた競合（同時終了イベント）として静かに諦める。バイナリモード
    # 固定で Windows の改行変換（\n → \r\n）を避ける。
    try:
        with open(target, "xb") as f:
            f.write(content.encode("utf-8"))
    except FileExistsError:
        return


def _extract_field(request: dict, key: str):
    if key in request:
        return request[key]
    extra = request.get("extra")
    if isinstance(extra, dict) and key in extra:
        return extra[key]
    return None


def _run(request: dict) -> None:
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return  # session_id が無ければ何もできない（ログすら残せない）

    turn_id = _extract_field(request, "turn_id")
    if turn_id is not None and not isinstance(turn_id, str):
        turn_id = str(turn_id)

    completed = _extract_field(request, "completed")
    interrupted = _extract_field(request, "interrupted")

    cwd = request.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()
    cwd_realpath = os.path.realpath(cwd)

    queue_entry_id = compute_queue_entry_id(session_id, turn_id)

    try:
        if _existing_entry_path(queue_entry_id) is not None:
            return  # §1.1 手順3: 二重キュー登録の防止

        try:
            excluded_roots = load_excluded_roots()
        except ExcludedRootsNotConfiguredError as exc:
            _log_enqueue_error(queue_entry_id, f"registration refused: {exc}")
            return

        if is_excluded_cwd(cwd_realpath, excluded_roots):
            return  # §1.1 手順4: 除外対象は記録も残さない

        _write_pending_entry(
            queue_entry_id,
            session_id=session_id,
            turn_id=turn_id,
            completed=bool(completed),
            interrupted=bool(interrupted),
            cwd_realpath=cwd_realpath,
        )
    except Exception as exc:  # noqa: BLE001 — フェイルオープンの絶対原則
        _log_enqueue_error(queue_entry_id, f"{type(exc).__name__}: {exc}")


def main() -> None:
    try:
        raw = sys.stdin.read()
        if raw and raw.strip():
            request = json.loads(raw)
            if isinstance(request, dict):
                _run(request)
    except Exception:  # noqa: BLE001 — stdin パース自体が壊れていても session_end を止めない
        pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
