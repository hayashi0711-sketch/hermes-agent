"""scripts/hh_distill.py — Phase 1b 段階2: Batch 投入・回収 CLI。

設計上の位置づけ:
    - 実装契約   docs/hh-agent/07_Phase1b_Spec.md §1.4（手動実行のみ）、
      §2（状態機械）、§2.3（回収）、§2.4（リトライ分類）、§2.5（CLI）、
      §4.3（Volume publish）
    - 担当表     docs/hh-agent/04_Task_Allocation.md Phase 1b 表
      「run / retry / status CLI。状態機械（pending→submitted→completed/
      failed）は 07_Phase1b_Spec.md §2 のとおり実装し変更しない」

== このスクリプトがやること ==

`hh_hooks/session_end_distill.py`（別所有者）が書いた `~/.hh-agent/
distill_queue/pending/` エントリを消費し、

    pending → submitting → submitted → completed
                                  → failed

の状態機械に沿って Batch API 投入・結果回収・SKILL.md 隔離保存・Hub への
`POST /api/skills/publish` までを行う。**状態の不変条件は §2 のとおり
（変更しない）。** 状態の不変条件に違反する操作が必要になった場合は
BLOCKED として報告する。

== サブコマンド ==

    run                — ローカル preflight → 投入 → 回収（§2.2 + §2.3）
                         を 1 回実行。終了コード 0/1。
    status             — 各状態のファイル数、`submitting/`・`failed/` の
                         理由一覧を表示するだけ（副作用なし）。
    retry <session_id> — 該当セッションの `failed/` エントリを全て
                         `pending/` へ戻す（投入済みフィールドを削除）。
                         §2.5。
    resolve-submitting <manifest_id>
                       — `submitting/` に残った `api_call_attempted: true`
                         のマニフェストに対し、`batches.list()` を呼び
                         該当バッチをユーザーに確定させ、エントリを
                         `submitted/` へ手動遷移（または `pending/` へ
                         差し戻し）。

== このファイルが独自に決めた設計判断 ==

1. **標準ライブラリ + `anthropic` SDK のみ**: 既存の `hh_hooks/
   tool_gate.py` は標準ライブラリのみで組まれている（200ms 性能要件）。
   本スクリプトは「手動実行」かつ「数分〜数十分かけてよい」用途のため
   `urllib.request` 自作ではなく公式 `anthropic` SDK を使う方が事故が
   少ない。Hub への publish 呼び出しだけ tool_gate.py の `urllib.request`
   流儀を踏襲する（単発 HTTP・認証ヘッダのみで十分なため）。
2. **canonical JSON は `json.dumps(sort_keys=True, separators=(",", ":"))`
   で十分**: §2.2 手順3 の「sha256(canonical_json(request))」は Batch
   API 完全性チェック用途のため、キー順固定＋セパレータ固定で十分
   （NFC 正規化や float 禁止を含む `core/canonical.py` ほどの厳密性は
   不要）。
3. **`anthropic` SDK の `messages.batches.create(requests=...)` を直接
   呼ぶ**: 中間的な独自 dataclass を噛まさず SDK の dict 形式のまま
   受け渡す（`skill_distiller.py:build_batch_request()` の戻り値を
   そのまま SDK に渡す）。
4. **`submitting/` から `pending/` への手動 retry は実装しない**:
   `retry` コマンドは §2.5 どおり `failed/` → `pending/` のみ。
   `submitting/` の手動再投入は `resolve-submitting` の経路で
   `pending/` へ差し戻す手順があるため、二系統に分けない。
5. **publish 失敗のリトライはローカルでのみ管理**: §4.3「失敗 →
   publish_attempts をインクリメント、5 回失敗で abandoned」は
   publish_status フィールドで完結するため、リトライ自体は次回の `run`
   末尾で行う。Hub 側の 429/5xx は `failed/` とは区別しない（同じ
   `publish_attempts++` のループで再試行）。
6. **`resolve-submitting` で `batches.list()` の結果が見つからない場合
   はユーザー入力を待つ**: §2.2 手順5 末尾「特定できなければ pending/
   へ差し戻す」は **ユーザー入力を介さない自動判別なし**。実装は
   「候補バッチを表示 → ユーザーに番号選択 or pending 戻しを聞く」
   ではなく、**「候補バッチを stderr に列挙して exit 2」** に留める
   （TTY 経由の対話取得は `hh_skill_promote.py` 同様、非対話実行でも
   安全側に倒す）。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Distiller library（同じ所有者の隣接ファイル）。
from modal_hub.services import skill_distiller  # noqa: E402

# Session reader は別所有者だが、メッセージ取得のために import する。
from modal_hub.services import session_reader  # noqa: E402
from modal_hub.services import skill_quarantine  # noqa: E402

# hh_hooks/session_end_distill.py から QUEUE_STATES / compute_queue_entry_id /
# _safe_state_file_path 相当の規則を共有するため、関数を import する
# （rule の手書き複製を避ける）。
from hh_hooks.session_end_distill import (  # noqa: E402
    QUEUE_STATES,
    compute_queue_entry_id,
)

_HH_AGENT_HOME_ENV = "USERPROFILE"

# §2.2 手順2: チャンクサイズ安全マージン。
MAX_REQUESTS_PER_CHUNK = 100
MAX_BYTES_PER_CHUNK = 80 * 1024 * 1024  # 80 MB。256 MB 上限に対する余裕。

# §2.3 手順7: 結果取得可能期限（29 日）。余裕を持って 28 日に縮める。
RESULT_EXPIRY_SECONDS = 28 * 24 * 3600

# §4.3: publish 失敗の上限。
PUBLISH_MAX_ATTEMPTS = 5

# queue_entry_id 正規表現（§1.3）。「Anthropic custom_id と同一」の固定。
QUEUE_ENTRY_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Manifest ファイル名のパターン。
_MANIFEST_NAME_RE = re.compile(r"^_manifest_(?P<id>[0-9a-f]+)\.json$")

# publish 失敗時のローカルリトライ間隔（秒）。
PUBLISH_BACKOFF_SECONDS = 2.0


# ---------------------------------------------------------------------------
# 基本パス
# ---------------------------------------------------------------------------


def hh_agent_home() -> Path:
    userprofile = os.environ.get(_HH_AGENT_HOME_ENV)
    base = Path(userprofile) if userprofile else Path.home()
    return base / ".hh-agent"


def distill_queue_dir(base: Optional[Path] = None) -> Path:
    return (base or hh_agent_home()) / "distill_queue"


def _state_dir(state: str, *, base: Optional[Path] = None) -> Path:
    if state not in QUEUE_STATES:
        raise ValueError(f"unknown queue state: {state!r}")
    return distill_queue_dir(base) / state


def _safe_state_file_path(state_dir: Path, queue_entry_id: str) -> Path:
    """`hh_hooks/session_end_distill.py` と同じ規約の直接の子ファイル検証。

    §1.3 末尾「すべてのファイル操作は対象パスの ``Path(...).resolve()``
    が対応する状態ディレクトリ直下であることを確認してから行う」を
    満たす。多層防御のため、queue_entry_id 自体の正規表現一致も
    行う（手書き複製は許すが食い違いは許さない）。
    """
    if not isinstance(queue_entry_id, str) or not QUEUE_ENTRY_ID_RE.match(queue_entry_id):
        raise ValueError(f"invalid queue_entry_id: {queue_entry_id!r}")
    # 2026-08-11 Codex レビュー Critical 指摘の修正: `state_dir` 自体が
    # symlink/junction の場合、resolve() の一致チェックだけでは
    # `hh_hooks/session_end_distill.py` で発見済みの穴と同じ形で素通り
    # する（`state_dir` と `candidate.resolve().parent` がどちらも
    # 置き換え先を指すため一致してしまう）。`state_dir` 自身が
    # symlink/junction でないことを先に確認する。
    if state_dir.exists() and state_dir.is_symlink():
        raise ValueError(f"{state_dir} 自体が symlink/junction。拒否する")
    candidate = state_dir / f"{queue_entry_id}.json"
    if candidate.resolve().parent != state_dir.resolve():
        raise ValueError(
            f"{candidate} は {state_dir} の直接の子ではない"
        )
    return candidate


# ---------------------------------------------------------------------------
# 状態ディレクトリ走査
# ---------------------------------------------------------------------------


def _list_state_files(state: str, *, base: Optional[Path] = None) -> List[Path]:
    state_dir = _state_dir(state, base=base)
    if not state_dir.is_dir():
        return []
    out: List[Path] = []
    for entry in sorted(state_dir.iterdir()):
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        qid = entry.name[: -len(".json")]
        try:
            _safe_state_file_path(state_dir, qid)
        except ValueError:
            continue
        out.append(entry)
    return out


def _list_manifest_files(*, base: Optional[Path] = None) -> List[Path]:
    state_dir = _state_dir("submitting", base=base)
    if not state_dir.is_dir():
        return []
    out: List[Path] = []
    for entry in sorted(state_dir.iterdir()):
        m = _MANIFEST_NAME_RE.match(entry.name)
        if not m or not entry.is_file():
            continue
        out.append(entry)
    return out


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write_json(path: Path, payload: dict) -> None:
    """`path` を UTF-8 + LF で原子的に書く（temp + fsync + replace）。

    `path` が既に存在していても上書きする。manifest の `api_call_attempted`
    false→true 更新など、同一ファイルへの部分更新で必要。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "xb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_write_text_raw(path: Path, encoded: bytes) -> None:
    """バイト列を原子的に書く（改行変換を避けるためバイナリモード固定）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "xb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_entry(path: Path) -> Optional[dict]:
    return _read_json(path)


def _write_entry(path: Path, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    _atomic_write_text_raw(path, encoded)


# ---------------------------------------------------------------------------
# §2.2 手順6: 起動時の manifest 整合性チェック
# ---------------------------------------------------------------------------


def _recover_manifests(*, base: Optional[Path] = None) -> Tuple[int, int]:
    """`submitting/_manifest_*.json` を読み、§2.2 手順6 のとおり整合性を
    回復する。

    Returns:
        (recovered_to_pending_count, retained_for_manual_count)。
    """
    recovered = 0
    retained = 0

    for manifest_path in _list_manifest_files(base=base):
        manifest = _read_json(manifest_path)
        if not isinstance(manifest, dict):
            # 壊れた manifest は削除（手動介入の余地が無いので潔く）。
            try:
                manifest_path.unlink()
            except FileNotFoundError:
                pass
            continue

        manifest_id = manifest.get("manifest_id")
        queue_entry_ids = manifest.get("queue_entry_ids")
        api_call_attempted = manifest.get("api_call_attempted")
        if not (
            isinstance(manifest_id, str)
            and isinstance(queue_entry_ids, list)
            and all(isinstance(s, str) for s in queue_entry_ids)
            and isinstance(api_call_attempted, bool)
        ):
            try:
                manifest_path.unlink()
            except FileNotFoundError:
                pass
            continue

        if api_call_attempted is False:
            # §2.2 手順6 「api_call_attempted == false → 全部 pending/ へ戻す」
            for qid in queue_entry_ids:
                _force_move_to_pending(qid, base=base)
                recovered += 1
            try:
                manifest_path.unlink()
            except FileNotFoundError:
                pass
            continue

        # api_call_attempted == True
        any_still_in_submitting = False
        for qid in queue_entry_ids:
            current_path = _find_entry(qid, base=base)
            if current_path is None:
                continue
            if current_path.parent.name == "submitted":
                continue  # OK: 既に submitted/ に居る
            if current_path.parent.name == "submitting":
                any_still_in_submitting = True
            # それ以外（pending/ や completed/）なら何もしない
            # （spec の規定外。理論上起きないが、安全側に倒して触らない）。

        if any_still_in_submitting:
            retained += 1
        else:
            # 全エントリが submitting/ から居なくなった → manifest を消す。
            try:
                manifest_path.unlink()
            except FileNotFoundError:
                pass

    return recovered, retained


def _find_entry(
    queue_entry_id: str, *, base: Optional[Path] = None
) -> Optional[Path]:
    """5 つの状態ディレクトリから `queue_entry_id` を探す。"""
    for state in QUEUE_STATES:
        state_dir = _state_dir(state, base=base)
        if not state_dir.is_dir():
            continue
        try:
            path = _safe_state_file_path(state_dir, queue_entry_id)
        except ValueError:
            continue
        if path.is_file():
            return path
    return None


def _force_move_to_pending(queue_entry_id: str, *, base: Optional[Path] = None) -> None:
    """指定 ID を現在どの状態ディレクトリに居ても `pending/` へ移動する。

    manifest 整合性回復専用。`os.replace()` ができない別ドライブを跨ぐ
    環境は単一ユーザー運用では想定しないため、copy + delete フォール
    バックは持たない（失敗時は例外を上位へ）。
    """
    pending_dir = _state_dir("pending", base=base)
    pending_dir.mkdir(parents=True, exist_ok=True)
    target = _safe_state_file_path(pending_dir, queue_entry_id)
    current = _find_entry(queue_entry_id, base=base)
    if current is None:
        # 元々無い（既に他で消された？）→ 何もしない
        return
    # 既に pending/ に居れば何もしない。ただし `_move_pending_to_submitting()`
    # は submitting/ へ書き込んでから元の pending/ を unlink する順序のため、
    # そのごく短い窓でクラッシュすると pending/ と submitting/ の両方に
    # コピーが残りうる（2026-08-11 Codex 指摘 Medium）。`_find_entry()` は
    # QUEUE_STATES の並び順（pending が submitting より先）でこちらを
    # 先に見つけるため、孤立した submitting/ 側のコピーも合わせて片付ける。
    if current.parent.name == "pending":
        try:
            stray = _safe_state_file_path(_state_dir("submitting", base=base), queue_entry_id)
        except ValueError:
            stray = None
        if stray is not None and stray.is_file():
            try:
                stray.unlink()
            except FileNotFoundError:
                pass
        return
    # 投入済みフィールド（batch_id 等）が残っていればクリアして戻す。
    payload = _read_entry(current)
    if not isinstance(payload, dict):
        payload = {}
    for field in ("batch_id", "submitted_at", "api_call_attempted", "manifest_id"):
        payload.pop(field, None)
    _atomic_write_text_raw(target, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
    try:
        current.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# §2.2 手順0: manifest 列挙 ID を除外集合にする
# ---------------------------------------------------------------------------


def _excluded_queue_entry_ids(*, base: Optional[Path] = None) -> set:
    """`submitting/_manifest_*.json` が列挙する `queue_entry_id` の集合。"""
    excluded: set = set()
    for manifest_path in _list_manifest_files(base=base):
        manifest = _read_json(manifest_path)
        if not isinstance(manifest, dict):
            continue
        qids = manifest.get("queue_entry_ids")
        if isinstance(qids, list):
            excluded.update(qids)
    return excluded


# ---------------------------------------------------------------------------
# §2.2 手順1〜2: preflight + Batch リクエスト組み立て + チャンク分割
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PendingEntry:
    queue_entry_id: str
    session_id: str
    turn_id: Optional[str]
    cwd: str
    completed: bool
    interrupted: bool
    queued_at: str
    source_path: Path


def _load_pending_entries(*, base: Optional[Path] = None) -> List[PendingEntry]:
    """`pending/` を読む。

    2026-08-11 Codex レビュー Critical 指摘の修正: 旧実装は
    `payload.get("queue_entry_id")`（ファイルの**内容**）をそのまま信用
    しており、`_list_state_files()` が検証済みのファイル**名**と一致する
    保証が無かった。`session_id`/`turn_id`/`cwd` はこの queue_entry_id で
    SessionDB/journal/git diff の取得元を決めるため、内容だけが改ざん・
    破損した pending ファイルが「別セッション・別 cwd」を指せてしまうと、
    `session_end_distill.py` の除外ルート判定を経由していないデータが
    紛れ込みうる。

    修正: `queue_entry_id` は**ファイル名**（`path.stem`。既に
    `_list_state_files()` が正規表現・直接の子であることを検証済み）を
    正とし、`compute_queue_entry_id(session_id, turn_id)` で再計算した値が
    ファイル名と一致することを確認する。一致しなければそのエントリ全体を
    スキップする（黙って別の値で処理を続けない）。
    """
    out: List[PendingEntry] = []
    for path in _list_state_files("pending", base=base):
        payload = _read_entry(path)
        if not isinstance(payload, dict):
            continue
        expected_qid = path.stem
        session_id = str(payload.get("session_id") or "")
        turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
        if not session_id:
            continue
        recomputed_qid = compute_queue_entry_id(session_id, turn_id)
        if recomputed_qid != expected_qid:
            continue
        qid = expected_qid
        out.append(
            PendingEntry(
                queue_entry_id=qid,
                session_id=session_id,
                turn_id=turn_id,
                cwd=str(payload.get("cwd") or ""),
                completed=bool(payload.get("completed")),
                interrupted=bool(payload.get("interrupted")),
                queued_at=str(payload.get("queued_at") or ""),
                source_path=path,
            )
        )
    return out


def _read_git_diff(cwd: str, max_bytes: int) -> Tuple[str, bool]:
    """`cwd` で `git diff HEAD` を実行し、stdout を切り詰めて返す。

    `git` 未インストール・非リポジトリ・タイムアウト・エラーは **全て空
    文字列**に倒す（§判断: セッションが git リポジトリ内で動いていない
    ことは普通にあり得る。LLM に渡す diff が無いだけ。preflight ①②③に
    は影響しない）。

    Args:
        max_bytes: UTF-8 バイト単位の上限。`MAX_GIT_DIFF_BYTES` を基本とする。

    Returns:
        `(diff_text, was_truncated)`。
    """
    if not cwd:
        return "", False
    if not os.path.isdir(cwd):
        return "", False
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=cwd,
            capture_output=True,
            timeout=5.0,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "", False
    if proc.returncode != 0:
        return "", False
    raw = proc.stdout.decode("utf-8", errors="replace")
    return skill_distiller.truncate_git_diff(raw, max_bytes=max_bytes)


@dataclasses.dataclass
class ChunkItem:
    pending: PendingEntry
    request: dict  # build_batch_request() の戻り値
    preflight_failed_reason: Optional[str]  # 失敗時のみ
    preflight_failed: bool = False


def _build_chunk(
    pending_entries: List[PendingEntry],
    *,
    base: Optional[Path] = None,
) -> Tuple[List[ChunkItem], List[Tuple[PendingEntry, str]]]:
    """`pending_entries` を読み込み、preflight + Batch リクエスト組み立て
    を行う。

    Returns:
        (preflight_passed_items, preflight_failed_pairs)。
        戻り値には manifest 移動は行わない（呼び出し側で行う）。
    """
    passed: List[ChunkItem] = []
    failed: List[Tuple[PendingEntry, str]] = []
    for entry in pending_entries:
        try:
            messages = session_reader.get_session_messages(entry.session_id)
        except session_reader.SessionReaderError:
            # セッション読み取り失敗は preflight 失敗として扱う（§2.3 手順4
            # の failed/ への遷移と同種のローカル障害）。
            failed.append((entry, "session_read_failed"))
            continue

        try:
            from hh_hooks import journal

            journal_entries = journal.read_journal_entries(entry.session_id, base=base)
        except Exception:  # noqa: BLE001 — journal 読み取りの例外も preflight 失敗扱い
            journal_entries = []

        passed_flag, reason = skill_distiller.evaluate_preconditions(journal_entries)
        if not passed_flag:
            failed.append((entry, reason or "precondition_failed"))
            continue

        diff_text, _diff_truncated = _read_git_diff(
            entry.cwd, skill_distiller.MAX_GIT_DIFF_BYTES
        )

        try:
            existing_headers = skill_distiller.collect_existing_skill_headers()
        except Exception:  # noqa: BLE001
            existing_headers = []

        try:
            request = skill_distiller.build_batch_request(
                queue_entry_id=entry.queue_entry_id,
                messages=messages,
                journal_entries=journal_entries,
                existing_skill_headers=existing_headers,
                git_diff_truncated=diff_text,
            )
        except Exception as exc:
            failed.append((entry, f"build_request_failed:{type(exc).__name__}"))
            continue

        passed.append(
            ChunkItem(
                pending=entry,
                request=request,
                preflight_failed_reason=None,
                preflight_failed=False,
            )
        )

    return passed, failed


def _split_into_chunks(items: List[ChunkItem]) -> List[List[ChunkItem]]:
    """MAX_REQUESTS_PER_CHUNK / MAX_BYTES_PER_CHUNK を超えないように分割する。

    `request["params"]["messages"][0]["content"]` のバイト数を近似値として
    使う（system は固定で小さいので無視して十分）。
    """
    chunks: List[List[ChunkItem]] = []
    current: List[ChunkItem] = []
    current_bytes = 0
    for item in items:
        try:
            content = item.request["params"]["messages"][0]["content"]
            approx_bytes = len(content.encode("utf-8"))
        except Exception:
            approx_bytes = 0
        if current and (
            len(current) >= MAX_REQUESTS_PER_CHUNK
            or current_bytes + approx_bytes > MAX_BYTES_PER_CHUNK
        ):
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(item)
        current_bytes += approx_bytes
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# §2.2 手順3〜4: manifest 作成 + 移動 + Anthropic 投入
# ---------------------------------------------------------------------------


def _make_manifest(
    manifest_id: str, items: List[ChunkItem]
) -> Tuple[Path, dict]:
    """manifest dict を作って返す（まだ書き込まない）。"""
    request_hashes = {}
    for item in items:
        canonical = skill_distiller.canonical_request_json(item.request)
        request_hashes[item.pending.queue_entry_id] = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
    manifest = {
        "manifest_id": manifest_id,
        "queue_entry_ids": [it.pending.queue_entry_id for it in items],
        "request_hashes": request_hashes,
        "api_call_attempted": False,
        "created_at": time.time(),
    }
    manifest_path = _state_dir("submitting") / f"_manifest_{manifest_id}.json"
    return manifest_path, manifest


def _write_manifest_then_move_to_submitting(
    manifest_path: Path, manifest: dict, items: List[ChunkItem], manifest_id: str
) -> None:
    """§2.2 手順3の順序を1箇所に固定する: manifest を先に書き込んでから
    pending/ を submitting/ へ移す。

    逆順だと、移動後・manifest 書き込み前のクラッシュで submitting/ 側に
    manifest_id 参照だけが残り、`_recover_manifests()` がその manifest を
    永久に見つけられずエントリが迷子になる（2026-08-11 Codex 指摘 Medium
    の修正）。呼び出し側（`_cmd_run_locked`）はこの関数を経由すること。
    """
    _atomic_write_json(manifest_path, manifest)
    _move_pending_to_submitting(items, manifest_id)


def _move_pending_to_submitting(
    items: List[ChunkItem], manifest_id: str
) -> None:
    """§2.2 手順3 末尾: 各 `pending/<id>.json` を `submitting/<id>.json` へ
    `os.replace()` で移動し、`manifest_id` を書き込む。"""
    submitting_dir = _state_dir("submitting")
    submitting_dir.mkdir(parents=True, exist_ok=True)
    for item in items:
        qid = item.pending.queue_entry_id
        target = _safe_state_file_path(submitting_dir, qid)
        payload = _read_entry(item.pending.source_path)
        if not isinstance(payload, dict):
            payload = {}
        payload["manifest_id"] = manifest_id
        payload["submitting_started_at"] = time.time()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        _atomic_write_text_raw(target, encoded)
        # 書き込めた後で元ファイルを消す（順序: 成功後に unlink）。
        try:
            item.pending.source_path.unlink()
        except FileNotFoundError:
            pass


@dataclasses.dataclass
class SubmitOutcome:
    manifest_id: str
    batch_id: Optional[str]
    state: str  # "submitted" / "rolled_back" / "ambiguous"


def _submit_chunk(
    manifest_path: Path,
    manifest: dict,
    items: List[ChunkItem],
    *,
    anthropic_client: Any,
) -> SubmitOutcome:
    """§2.2 手順4: 1 チャンク分の投入。

    1. manifest の `api_call_attempted` を true にして fsync。
    2. `client.messages.batches.create()` を呼ぶ。
    3. 成功 → 各エントリに `batch_id` を書き加えて `submitted/` へ移動、
       manifest を削除。
    4. 明確な 4xx → 各エントリを `pending/` へ戻す、manifest を削除。
    5. その他（タイムアウト・接続断等） → 何もしない（`submitting/` のまま、
       manifest は `api_call_attempted: true` のまま残す）。
    """
    manifest_id = manifest["manifest_id"]

    # 手順4 冒頭: api_call_attempted を true にして fsync。
    manifest["api_call_attempted"] = True
    _atomic_write_json(manifest_path, manifest)

    requests = [item.request for item in items]
    try:
        batch = anthropic_client.messages.batches.create(requests=requests)
    except Exception as exc:  # noqa: BLE001 — 大雑把に拾って曖昧ケースへ倒す
        # 明らかな 4xx か、曖昧（タイムアウト等）かを見分けたいが、
        # SDK 例外型の差分を完全には信用できないため、ヒューリスティックで
        # 判定する。明示的にメッセージで「4xx」と分かる場合のみ
        # rolled_back とし、その他は ambiguous とする。
        msg = str(exc)
        is_clear_client_error = (
            "400" in msg or "invalid_request" in msg.lower() or "BatchRequestError" in type(exc).__name__
        )
        if is_clear_client_error:
            for item in items:
                _force_move_to_pending(item.pending.queue_entry_id)
            try:
                manifest_path.unlink()
            except FileNotFoundError:
                pass
            return SubmitOutcome(manifest_id=manifest_id, batch_id=None, state="rolled_back")
        return SubmitOutcome(manifest_id=manifest_id, batch_id=None, state="ambiguous")

    batch_id = getattr(batch, "id", None) or (batch.get("id") if isinstance(batch, dict) else None)
    if not isinstance(batch_id, str) or not batch_id:
        return SubmitOutcome(manifest_id=manifest_id, batch_id=None, state="ambiguous")

    # 成功: submitted/ へ移動 + batch_id を書き込む。
    submitted_dir = _state_dir("submitted")
    submitted_dir.mkdir(parents=True, exist_ok=True)
    for item in items:
        qid = item.pending.queue_entry_id
        target = _safe_state_file_path(submitted_dir, qid)
        payload = _read_entry(_safe_state_file_path(_state_dir("submitting"), qid))
        if not isinstance(payload, dict):
            payload = {}
        payload["batch_id"] = batch_id
        payload["submitted_at"] = time.time()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        _atomic_write_text_raw(target, encoded)
        try:
            _safe_state_file_path(_state_dir("submitting"), qid).unlink()
        except FileNotFoundError:
            pass

    try:
        manifest_path.unlink()
    except FileNotFoundError:
        pass

    return SubmitOutcome(manifest_id=manifest_id, batch_id=batch_id, state="submitted")


# ---------------------------------------------------------------------------
# preflight 不合格エントリ → completed/ への直接遷移
# ---------------------------------------------------------------------------


def _move_to_completed(
    entry_path: Path,
    *,
    queue_entry_id: str,
    extracted: bool,
    reason: Optional[str],
    extra: Optional[dict] = None,
    base: Optional[Path] = None,
) -> None:
    completed_dir = _state_dir("completed", base=base)
    completed_dir.mkdir(parents=True, exist_ok=True)
    target = _safe_state_file_path(completed_dir, queue_entry_id)
    payload = _read_entry(entry_path)
    if not isinstance(payload, dict):
        payload = {}
    payload["extracted"] = extracted
    payload["reason"] = reason
    payload["completed_at"] = time.time()
    if isinstance(extra, dict):
        for k, v in extra.items():
            payload[k] = v
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    _atomic_write_text_raw(target, encoded)
    try:
        entry_path.unlink()
    except FileNotFoundError:
        pass


def _move_to_failed(
    entry_path: Path,
    *,
    queue_entry_id: str,
    reason: str,
    extra: Optional[dict] = None,
    base: Optional[Path] = None,
) -> None:
    failed_dir = _state_dir("failed", base=base)
    failed_dir.mkdir(parents=True, exist_ok=True)
    target = _safe_state_file_path(failed_dir, queue_entry_id)
    payload = _read_entry(entry_path)
    if not isinstance(payload, dict):
        payload = {}
    payload["failed_at"] = time.time()
    payload["reason"] = reason
    if isinstance(extra, dict):
        for k, v in extra.items():
            payload[k] = v
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    _atomic_write_text_raw(target, encoded)
    try:
        entry_path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# §2.3 回収フェーズ
# ---------------------------------------------------------------------------


def _collect_submitted_batches(*, base: Optional[Path] = None) -> dict:
    """`submitted/` 内のファイルを走査し、`batch_id` → entries の辞書を作る。"""
    out: dict = {}
    for path in _list_state_files("submitted", base=base):
        payload = _read_entry(path)
        if not isinstance(payload, dict):
            continue
        bid = payload.get("batch_id")
        qid = payload.get("queue_entry_id")
        if not (isinstance(bid, str) and isinstance(qid, str)):
            continue
        out.setdefault(bid, []).append((qid, path))
    return out


def _to_dict(obj: Any) -> Optional[dict]:
    """SDK オブジェクト（pydantic `BaseModel`）・dict・None のいずれからも
    dict を得る。得られなければ None。

    2026-08-11 Codex レビュー Critical 指摘の修正の一部: 実 Anthropic SDK
    の `batches.results()` は `MessageBatchIndividualResponse`（pydantic
    モデル）を yield する。テストの手作り dict と実 SDK オブジェクトの
    両方を同じコードパスで扱えるよう、ここで dict へ正規化する。
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:  # noqa: BLE001 — 正規化できなければ None（呼び出し側が failed/ へ倒す）
            return None
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return None


def _process_batch_results(
    batch_id: str,
    entries: List[Tuple[str, Path]],
    *,
    anthropic_client: Any,
    base: Optional[Path] = None,
) -> dict:
    """1 バッチ分の results を処理する。

    Returns:
        {"processed": int, "completed_extracted": int,
         "completed_not_extracted": int, "failed": int,
         "skipped_due_to_error": bool}
    """
    summary = {
        "processed": 0,
        "completed_extracted": 0,
        "completed_not_extracted": 0,
        "failed": 0,
        "skipped_due_to_error": False,
    }

    try:
        batch = anthropic_client.messages.batches.retrieve(batch_id)
    except Exception as exc:  # noqa: BLE001
        # §2.4: 認証エラー・404・5xx・タイムアウトを分類。
        msg = str(exc)
        lowered = msg.lower()
        if "401" in msg or "403" in msg or "unauthorized" in lowered or "forbidden" in lowered:
            # 認証系は即中断。状態は変えない（status で可視化される）。
            summary["skipped_due_to_error"] = True
            return summary
        if "404" in msg or "not_found" in lowered or "batch_not_found" in lowered:
            for qid, entry_path in entries:
                _move_to_failed(
                    entry_path,
                    queue_entry_id=qid,
                    reason="batch_not_found",
                    base=base,
                )
                summary["failed"] += 1
                summary["processed"] += 1
            return summary
        if "429" in msg or "rate" in lowered:
            summary["skipped_due_to_error"] = True
            return summary
        # 5xx やタイムアウト → スキップ（次回再試行）
        summary["skipped_due_to_error"] = True
        return summary

    processing_status = getattr(batch, "processing_status", None) or (
        batch.get("processing_status") if isinstance(batch, dict) else None
    )
    if processing_status != "ended":
        # まだ終わっていない → スキップ
        return summary

    # results() を反復して個別結果を読む。
    try:
        results_iter = anthropic_client.messages.batches.results(batch_id)
    except Exception:  # noqa: BLE001
        summary["skipped_due_to_error"] = True
        return summary

    # qid → entry_path の lookup を先に作っておく。
    qid_to_path = {qid: p for qid, p in entries}
    processed_qids = set()

    for result_obj in results_iter:
        outer = _to_dict(result_obj) or {}
        custom_id = outer.get("custom_id")
        if not isinstance(custom_id, str):
            continue
        entry_path = qid_to_path.get(custom_id)
        if entry_path is None:
            # §2.3 手順4: custom_id が submitted/ に存在しない（重複配信等）
            # → ログして無視。ストリーム処理は止めない。
            continue
        processed_qids.add(custom_id)

        # 2026-08-11 Codex レビュー Critical 指摘の修正: Anthropic SDK の
        # `MessageBatchIndividualResponse` は `custom_id`/`result` の2
        # フィールドのみを持つ外側オブジェクトで、`type`/`message` は
        # `result_obj.result` の**内側**にある。旧実装は外側オブジェクトを
        # そのまま `parse_llm_response()` へ渡していたため、実 SDK の
        # レスポンスでは常に type 取得に失敗していた（テストの手作り dict
        # では偶然 outer==inner の形にしていたため気づけなかった）。
        inner = _to_dict(outer.get("result"))
        if inner is None:
            _move_to_failed(
                entry_path,
                queue_entry_id=custom_id,
                reason="malformed_result_shape",
                base=base,
            )
            summary["failed"] += 1
            summary["processed"] += 1
            continue

        result_type = inner.get("type")
        if result_type in ("errored", "canceled", "expired"):
            # §2.3 手順4: errored/canceled/expired は failed/ へ、
            # result.type とエラー内容を記録する。旧実装は
            # `parse_llm_response()` にこれらの type も渡し、戻ってきた
            # `not_extractable` を `completed/` として保存していた
            # （spec が要求する `failed/` ではない）。
            error_detail = inner.get("error")
            extra = {"result_type": result_type}
            if error_detail is not None:
                extra["error"] = _to_dict(error_detail) or error_detail
            _move_to_failed(
                entry_path,
                queue_entry_id=custom_id,
                reason=f"batch_result_{result_type}",
                extra=extra,
                base=base,
            )
            summary["failed"] += 1
            summary["processed"] += 1
            continue

        # `message.content` の各要素も SDK オブジェクトなら dict 化する
        # （`skill_distiller._extract_text_from_content()` は dict の
        # `block.get("type")`/`block.get("text")` を前提にしている）。
        message = _to_dict(inner.get("message"))
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            message = dict(message)
            message["content"] = [_to_dict(b) or b for b in message["content"]]
            inner = dict(inner)
            inner["message"] = message

        try:
            decision = skill_distiller.parse_llm_response(custom_id, inner)
        except skill_distiller.DistillParseError as exc:
            _move_to_failed(
                entry_path,
                queue_entry_id=custom_id,
                reason=f"parse_failed:{type(exc).__name__}",
                extra={"parse_error": str(exc)},
                base=base,
            )
            summary["failed"] += 1
            summary["processed"] += 1
            continue

        if decision.decision == "novel":
            entry_payload = _read_entry(entry_path) or {}
            sid = entry_payload.get("session_id") if isinstance(entry_payload, dict) else None
            sid_str = sid if isinstance(sid, str) else ""
            try:
                mat = skill_distiller.materialize_skill_md(
                    queue_entry_id=custom_id,
                    name=decision.name or "",
                    description=decision.description or "",
                    body=decision.body or "",
                    session_id=sid_str,
                    base=hh_agent_home(),
                )
                _move_to_completed(
                    entry_path,
                    queue_entry_id=custom_id,
                    extracted=True,
                    reason=None,
                    extra={
                        "name": mat.name,
                        "output_path": mat.output_path,
                        "content_sha256": mat.content_sha256,
                        "materialized": True,
                        "publish_status": "pending",
                        "publish_attempts": 0,
                    },
                    base=base,
                )
                summary["completed_extracted"] += 1
            except Exception as exc:  # noqa: BLE001 — QuarantineError 等は failed/ へ
                _move_to_failed(
                    entry_path,
                    queue_entry_id=custom_id,
                    reason=f"materialize_failed:{type(exc).__name__}",
                    extra={"materialize_error": str(exc)},
                    base=base,
                )
                summary["failed"] += 1
        elif decision.decision == "duplicate":
            _move_to_completed(
                entry_path,
                queue_entry_id=custom_id,
                extracted=False,
                reason="duplicate",
                extra={"duplicate_of": decision.duplicate_of},
                base=base,
            )
            summary["completed_not_extracted"] += 1
        elif decision.decision == "not_extractable":
            _move_to_completed(
                entry_path,
                queue_entry_id=custom_id,
                extracted=False,
                reason=decision.not_extractable_reason or "not_extractable",
                base=base,
            )
            summary["completed_not_extracted"] += 1
        else:
            _move_to_failed(
                entry_path,
                queue_entry_id=custom_id,
                reason=f"unknown_decision:{decision.decision!r}",
                base=base,
            )
            summary["failed"] += 1
        summary["processed"] += 1

    # 29 日経過した submitted/ エントリは強制 failed 化（§2.3 手順7）。
    now = time.time()
    for qid, entry_path in entries:
        if qid in processed_qids:
            continue
        payload = _read_entry(entry_path) or {}
        submitted_at = payload.get("submitted_at") if isinstance(payload, dict) else None
        if isinstance(submitted_at, (int, float)) and (now - submitted_at) > RESULT_EXPIRY_SECONDS:
            _move_to_failed(
                entry_path,
                queue_entry_id=qid,
                reason="result_expired",
                base=base,
            )
            summary["failed"] += 1

    return summary


# ---------------------------------------------------------------------------
# §4.3 publish フェーズ（completed/ → Hub POST /api/skills/publish）
# ---------------------------------------------------------------------------


def _load_agent_token() -> Optional[str]:
    """`%USERPROFILE%\\.hh-agent\\distill_token.json` から publish 用トークンを読む。

    07_Phase1b_Spec.md §5 の設計契約どおり、承認フロー用の `agent_token.json`
    とは別ファイル。Distiller ローカルワーカー専用に `scopes=["publish"]` のみで
    発行されたトークンを最小権限で保持する。
    形式: tool_gate.py と同じく `{"token": "hha1.<payload>.<sig>"}`。
    `scopes` が `publish` を含むかどうかは Hub 側で検証される前提なので、
    ここでは機械的に読めればトークン文字列を返す（読み込み失敗は None）。
    """
    token_path = hh_agent_home() / "distill_token.json"
    if not token_path.is_file():
        return None
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        token = data.get("token")
        if isinstance(token, str) and token:
            return token
    return None


def _load_hub_base_url() -> Optional[str]:
    """Hub のベース URL。優先順位は tool_gate.py と同じ（環境変数 → config.json）。"""
    env_url = os.environ.get("HH_AGENT_HUB_URL")
    if env_url:
        return env_url.rstrip("/")
    config_path = hh_agent_home() / "config.json"
    if not config_path.is_file():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        url = data.get("hub_url")
        if isinstance(url, str) and url:
            return url.rstrip("/")
    return None


def _post_publish(
    hub_base_url: str, token: str, payload: dict
) -> Tuple[int, Optional[dict]]:
    """`POST /api/skills/publish` を 1 回呼ぶ。戻り値は `(status, parsed_body)`。

    タイムアウト・接続エラーは `(None, None)` を返す（呼び出し側で
    リトライ判定に使う）。
    """
    url = hub_base_url.rstrip("/") + "/api/skills/publish"
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            status = resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, None
    parsed: Optional[dict] = None
    if raw:
        try:
            candidate = json.loads(raw.decode("utf-8"))
            if isinstance(candidate, dict):
                parsed = candidate
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
    return status, parsed


def _record_publish_local_failure(payload: dict, path: Path, reason: str, summary: dict) -> None:
    """Hub へリクエストを送る前に判明したローカル側の失敗（隔離領域の
    マーカー欠落・出力パス不正・読み込みエラー等）も `publish_attempts` を
    消費させ、`PUBLISH_MAX_ATTEMPTS` に達したら abandoned にする。

    2026-08-11 Codex 指摘 Medium の修正: 修正前はこれらのケースで
    `continue` するだけで attempts が一切増えず、`publish_status` が
    "pending" のまま永久に（`status` コマンドでも異常として見えずに）
    残り続けていた。
    """
    attempts = int(payload.get("publish_attempts") or 0) + 1
    payload["publish_attempts"] = attempts
    payload["publish_last_error"] = reason
    if attempts >= PUBLISH_MAX_ATTEMPTS:
        payload["publish_status"] = "abandoned"
        summary["abandoned"] += 1
    else:
        summary["still_pending"] += 1
    _atomic_write_text_raw(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
    )


def _publish_pending_entries(*, base: Optional[Path] = None) -> dict:
    """`completed/` 内の `extracted: true` かつ `publish_status: pending`
    エントリに対し、Hub への publish を試行する。

    Returns:
        {"published": int, "still_pending": int, "abandoned": int,
         "conflicts": int, "skipped_no_config": int}
    """
    summary = {
        "published": 0,
        "still_pending": 0,
        "abandoned": 0,
        "conflicts": 0,
        "skipped_no_config": 0,
        "auth_error": None,
    }
    hub_url = _load_hub_base_url()
    token = _load_agent_token()
    if hub_url is None or token is None:
        # publish が設定されていなくても Distiller 自体は止まらない。
        # 該当エントリの publish_status はそのまま "pending" を維持する。
        for path in _list_state_files("completed", base=base):
            payload = _read_entry(path) or {}
            if payload.get("extracted") is True and payload.get("publish_status") == "pending":
                summary["skipped_no_config"] += 1
        return summary

    for path in _list_state_files("completed", base=base):
        payload = _read_entry(path) or {}
        if payload.get("extracted") is not True:
            continue
        if payload.get("publish_status") != "pending":
            continue
        name = payload.get("name")
        if not isinstance(name, str):
            _record_publish_local_failure(payload, path, "missing_or_invalid_name", summary)
            continue
        # SKILL.md を隔離領域から読み直す（完成形は frontmatter 付き）。
        #
        # 2026-08-11 Codex レビュー Critical 指摘の修正: 旧実装は
        # `payload.get("queue_entry_id")`（completed/ ファイルの**内容**）
        # をそのままパス構成要素として使っていた。`_list_state_files()` は
        # ファイル**名**しか検証しておらず、内容の `queue_entry_id`
        # フィールドは検証済みファイル名と一致する保証が無い（改ざん・
        # 破損の両方でずれうる）。`"../../marker"` のような値が入っていると
        # `.materialized/` の外を指す任意のパスを読みに行き、その
        # `output_path` を経由して任意ファイルの内容を Hub へ publish
        # できてしまう（実機で再現された）。
        #
        # 修正: `queue_entry_id` は**検証済みファイル名**（`path.stem`）
        # から導出し、正規表現でも再確認する。`.materialized/<qid>.json`
        # が直接の子であることと、そこに書かれた `output_path` が隔離
        # 領域の配下であることの両方を確認してから初めて読む。
        qid = path.stem
        if not skill_quarantine.QUEUE_ENTRY_ID_RE.match(qid):
            continue
        try:
            materialized_dir = skill_quarantine._materialized_dir(hh_agent_home())
            mat_payload_path = materialized_dir / f"{qid}.json"
            if mat_payload_path.resolve().parent != materialized_dir.resolve():
                _record_publish_local_failure(payload, path, "materialized_marker_path_invalid", summary)
                continue
            mat_payload = _read_json(mat_payload_path) or {}
            output_path_raw = mat_payload.get("output_path")
            if not isinstance(output_path_raw, str):
                _record_publish_local_failure(payload, path, "materialized_marker_missing_or_invalid", summary)
                continue
            output_path = Path(output_path_raw).resolve()
            quarantine_root_resolved = skill_quarantine.quarantine_root(hh_agent_home()).resolve()
            try:
                output_path.relative_to(quarantine_root_resolved)
            except ValueError:
                _record_publish_local_failure(payload, path, "output_path_outside_quarantine", summary)
                continue
            skill_md_text = output_path.read_text(encoding="utf-8")
        except OSError:
            _record_publish_local_failure(payload, path, "output_read_error", summary)
            continue

        try:
            pub = skill_distiller.build_publish_payload(name, skill_md_text)
        except ValueError:
            # 名前不正等。abandoned 扱い。
            payload["publish_status"] = "abandoned"
            payload["publish_attempts"] = PUBLISH_MAX_ATTEMPTS
            payload["publish_last_error"] = "invalid_payload"
            _atomic_write_text_raw(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
            summary["abandoned"] += 1
            continue

        status_code, parsed_body = _post_publish(hub_url, token, pub)

        if status_code in (401, 403):
            # 2026-08-11 Codex 指摘（design concern）の反映: 401/403 は
            # このエントリ固有の問題ではなく、トークン自体がグローバルに
            # 失効・誤設定である signal。ここでエントリごとの
            # publish_attempts を消費して retry-then-abandon させると、
            # 1つの壊れたトークンだけで pending 全件を毎回1リクエストずつ
            # 徒に叩き、最終的に全部 abandoned にしてしまいうる。
            # attempts は増やさずエントリを触らずに publish フェーズ全体を
            # ここで打ち切り、次回 run 時にトークンが復旧していれば
            # 通常どおり再試行させる。
            summary["auth_error"] = f"http_{status_code}"
            print(
                f"[hh_distill] publish auth error (http_{status_code}); "
                "aborting publish phase for this run (pending entries left untouched)",
                file=sys.stderr,
            )
            break

        attempts = int(payload.get("publish_attempts") or 0)
        attempts += 1
        payload["publish_attempts"] = attempts

        if status_code is None:
            # 接続エラー・タイムアウト
            payload["publish_last_error"] = "network_error"
            if attempts >= PUBLISH_MAX_ATTEMPTS:
                payload["publish_status"] = "abandoned"
                summary["abandoned"] += 1
            else:
                summary["still_pending"] += 1
        elif status_code == 200:
            payload["publish_status"] = "published"
            payload["publish_last_error"] = None
            summary["published"] += 1
        elif status_code == 409:
            # §5: SKILL_ALREADY_PUBLISHED_WITH_DIFFERENT_CONTENT
            payload["publish_status"] = "failed_conflict"
            payload["publish_last_error"] = "conflict"
            summary["conflicts"] += 1
        else:
            # 429 / 5xx 等はリトライ対象（401/403 は上で個別に処理済み、
            # ここには来ない）。
            payload["publish_last_error"] = f"http_{status_code}"
            if attempts >= PUBLISH_MAX_ATTEMPTS:
                payload["publish_status"] = "abandoned"
                summary["abandoned"] += 1
            else:
                summary["still_pending"] += 1

        _atomic_write_text_raw(
            path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
        )
        # 429 のバックオフ
        if status_code == 429 and payload.get("publish_status") == "pending":
            time.sleep(PUBLISH_BACKOFF_SECONDS)

    return summary


# ---------------------------------------------------------------------------
# run コマンド
# ---------------------------------------------------------------------------


def _build_anthropic_client():
    """`anthropic.Anthropic()` クライアントを返す。API キーは環境変数
    `ANTHROPIC_API_KEY` から読む（SDK のデフォルト動作）。
    """
    import anthropic

    return anthropic.Anthropic()


#: 2026-08-11 Codex レビュー Critical 指摘対応: `run` 排他ロックの陳腐化
#: 判定しきい値。`run` 自体は Batch の完了を待たない設計（提出・回収は
#: それぞれ 1 パス）なので、通常は数秒〜数分で終わる。前回プロセスの
#: クラッシュでロックが残ったケースだけを想定した緩めの値にする。
_RUN_LOCK_STALE_SECONDS = 30 * 60


def _acquire_run_lock(base: Path) -> Path:
    """`run` の排他ロックを取る。

    2026-08-11 Codex レビュー Critical 指摘の修正: 旧実装は複数の `run`
    プロセスが同時に同じ `pending/` エントリを選択できてしまい、
    それぞれが別の manifest を作って `batches.create()` を呼ぶ
    ——**同一セッションの二重投入・二重課金**が実機で再現された。
    プロセス間の排他制御を持たない状態機械へ後付けで CAS を足すのではなく、
    「同時に1個の `run` しか実行しない」という単純な排他ロックで根本を断つ
    （単一ユーザー・手動実行という運用前提に対して十分な粒度）。
    """
    lock_path = base / "distill_queue" / "run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "started_at": time.time()}).encode("utf-8")
    try:
        with open(lock_path, "xb") as f:
            f.write(payload)
        return lock_path
    except FileExistsError:
        pass

    # 既存ロックが古すぎる場合のみ、前回プロセスのクラッシュ跡とみなして奪う。
    started_at = None
    try:
        existing = json.loads(lock_path.read_text(encoding="utf-8"))
        started_at = existing.get("started_at")
    except (OSError, json.JSONDecodeError):
        pass
    if isinstance(started_at, (int, float)) and (time.time() - started_at) > _RUN_LOCK_STALE_SECONDS:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        with open(lock_path, "xb") as f:
            f.write(payload)
        return lock_path

    raise RuntimeError(
        f"another `hh_distill.py run` appears to be in progress (lock: {lock_path}); "
        "refusing to start a second one (would risk double-submitting the same "
        "queue entries to the paid Batch API)"
    )


def _release_run_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def cmd_run(args: argparse.Namespace) -> int:
    base = hh_agent_home()
    try:
        lock_path = _acquire_run_lock(base)
    except RuntimeError as exc:
        print(f"[hh_distill] {exc}", file=sys.stderr)
        return 1
    try:
        return _cmd_run_locked(args, base)
    finally:
        _release_run_lock(lock_path)


def _cmd_run_locked(args: argparse.Namespace, base: Path) -> int:
    # §2.2 手順6: 起動時の manifest 整合性回復
    recovered, retained = _recover_manifests(base=base)
    if recovered > 0:
        print(f"[hh_distill] recovered {recovered} manifest entries back to pending/", file=sys.stderr)
    if retained > 0:
        print(f"[hh_distill] {retained} manifest(s) retained for manual resolution", file=sys.stderr)

    # §2.2 手順0: manifest 列挙 ID の除外
    excluded = _excluded_queue_entry_ids(base=base)
    pending = _load_pending_entries(base=base)
    pending = [p for p in pending if p.queue_entry_id not in excluded]
    if not pending:
        print("[hh_distill] no pending entries", file=sys.stderr)
    else:
        print(f"[hh_distill] {len(pending)} pending entries to evaluate", file=sys.stderr)

    # §2.2 手順1〜2: preflight + チャンク分割
    passed_items, failed_pairs = _build_chunk(pending, base=base)
    for entry, reason in failed_pairs:
        _move_to_completed(
            entry.source_path,
            queue_entry_id=entry.queue_entry_id,
            extracted=False,
            reason=reason,
            base=base,
        )
    if failed_pairs:
        print(f"[hh_distill] {len(failed_pairs)} entries failed preflight", file=sys.stderr)

    # §2.2 手順3〜4: チャンクごとに manifest + 投入
    if passed_items:
        try:
            client = _build_anthropic_client()
        except Exception as exc:
            print(f"[hh_distill] failed to build Anthropic client: {exc}", file=sys.stderr)
            # 投入できなかったので、passed_items を pending/ に戻す。
            for item in passed_items:
                _force_move_to_pending(item.pending.queue_entry_id)
            return 1

        chunks = _split_into_chunks(passed_items)
        for chunk in chunks:
            manifest_id = uuid.uuid4().hex
            manifest_path, manifest = _make_manifest(manifest_id, chunk)
            _write_manifest_then_move_to_submitting(manifest_path, manifest, chunk, manifest_id)
            outcome = _submit_chunk(
                manifest_path, manifest, chunk, anthropic_client=client
            )
            if outcome.state == "submitted":
                print(
                    f"[hh_distill] submitted manifest={outcome.manifest_id[:8]} "
                    f"batch_id={outcome.batch_id} size={len(chunk)}",
                    file=sys.stderr,
                )
            elif outcome.state == "rolled_back":
                print(
                    f"[hh_distill] rolled back manifest={outcome.manifest_id[:8]} "
                    f"(client error, no charge)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[hh_distill] ambiguous submission for manifest="
                    f"{outcome.manifest_id[:8]} (left in submitting/)",
                    file=sys.stderr,
                )

    # §2.3 回収フェーズ
    try:
        client = _build_anthropic_client()
    except Exception:
        client = None

    if client is not None:
        submitted_groups = _collect_submitted_batches(base=base)
        if submitted_groups:
            print(
                f"[hh_distill] {len(submitted_groups)} submitted batch(es) to check",
                file=sys.stderr,
            )
            for batch_id, entries in submitted_groups.items():
                summary = _process_batch_results(
                    batch_id, entries, anthropic_client=client, base=base
                )
                print(
                    f"[hh_distill] batch={batch_id[:12]} "
                    f"processed={summary['processed']} "
                    f"extracted={summary['completed_extracted']} "
                    f"not_extracted={summary['completed_not_extracted']} "
                    f"failed={summary['failed']} "
                    f"skipped={summary['skipped_due_to_error']}",
                    file=sys.stderr,
                )

    # §4.3 publish フェーズ
    pub_summary = _publish_pending_entries(base=base)
    if any(pub_summary.values()):
        print(f"[hh_distill] publish: {pub_summary}", file=sys.stderr)

    return 0


# ---------------------------------------------------------------------------
# status コマンド
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    base = hh_agent_home()
    counts = {state: 0 for state in QUEUE_STATES}
    failed_reasons: dict = {}
    # 2026-08-11 Codex 指摘 Medium の修正: publish_attempts/publish_status を
    # 消費するようにした2つの修正（マーカー欠落・401/403リトライ化）が
    # 実際に「見える劣化」になるよう、completed/ の publish_status 内訳を
    # status に出す（それまでは abandoned になっても status からは分からず、
    # リトライを重ねていることに気づく手段が無かった）。
    publish_status_counts: dict = {}
    for state in QUEUE_STATES:
        for path in _list_state_files(state, base=base):
            counts[state] += 1
            if state == "failed":
                payload = _read_entry(path) or {}
                reason = payload.get("reason")
                if isinstance(reason, str):
                    failed_reasons[reason] = failed_reasons.get(reason, 0) + 1
            if state == "completed":
                payload = _read_entry(path) or {}
                pub_status = payload.get("publish_status")
                if isinstance(pub_status, str):
                    publish_status_counts[pub_status] = publish_status_counts.get(pub_status, 0) + 1

    manifests = _list_manifest_files(base=base)
    manual_resolution: List[str] = []
    for manifest_path in manifests:
        manifest = _read_json(manifest_path)
        if not isinstance(manifest, dict):
            continue
        if manifest.get("api_call_attempted") is True:
            # まだ submitting/ に残エントリがある manifest を列挙。
            qids = manifest.get("queue_entry_ids") or []
            still_pending = []
            for qid in qids:
                if not isinstance(qid, str):
                    continue
                current = _find_entry(qid, base=base)
                if current is not None and current.parent.name == "submitting":
                    still_pending.append(qid)
            if still_pending:
                manual_resolution.append(manifest.get("manifest_id") or "")

    print(json.dumps({
        "counts": counts,
        "failed_reasons": failed_reasons,
        "publish_status_counts": publish_status_counts,
        "manifests": len(manifests),
        "needs_manual_resolution": manual_resolution,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# retry コマンド
# ---------------------------------------------------------------------------


def cmd_retry(args: argparse.Namespace) -> int:
    base = hh_agent_home()
    target_session_id = args.session_id
    if not isinstance(target_session_id, str) or not target_session_id:
        print("[hh_distill] retry: session_id is required", file=sys.stderr)
        return 2

    pending_dir = _state_dir("pending", base=base)
    pending_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    for path in _list_state_files("failed", base=base):
        payload = _read_entry(path) or {}
        sid = payload.get("session_id")
        if sid != target_session_id:
            continue
        qid = payload.get("queue_entry_id")
        if not isinstance(qid, str) or not QUEUE_ENTRY_ID_RE.match(qid):
            continue
        target = _safe_state_file_path(pending_dir, qid)
        # 投入済みフィールドをクリア。
        for field in (
            "batch_id",
            "submitted_at",
            "manifest_id",
            "submitting_started_at",
            "failed_at",
            "reason",
            "publish_status",
            "publish_attempts",
        ):
            payload.pop(field, None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        _atomic_write_text_raw(target, encoded)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        moved += 1

    print(f"[hh_distill] retry: moved {moved} entries from failed/ to pending/")
    return 0


# ---------------------------------------------------------------------------
# resolve-submitting コマンド
# ---------------------------------------------------------------------------


def cmd_resolve_submitting(args: argparse.Namespace) -> int:
    """§2.2 手順5: `submitting/` に残った `api_call_attempted: true` の
    マニフェストに対し、batches.list() を呼び、候補バッチを stderr に
    列挙する。

    自動判定はせず、人間が `submitted/` への遷移または `pending/` への
    差し戻しを決める運用を前提とする（対話部分は別経路）。
    """
    base = hh_agent_home()
    target_manifest_id = args.manifest_id
    if not isinstance(target_manifest_id, str) or not target_manifest_id:
        print("[hh_distill] resolve-submitting: manifest_id is required", file=sys.stderr)
        return 2

    manifest_path = _state_dir("submitting", base=base) / f"_manifest_{target_manifest_id}.json"
    if not manifest_path.is_file():
        print(f"[hh_distill] manifest not found: {manifest_path}", file=sys.stderr)
        return 1
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        print(f"[hh_distill] manifest unreadable: {manifest_path}", file=sys.stderr)
        return 1
    if manifest.get("api_call_attempted") is not True:
        print(
            f"[hh_distill] manifest {target_manifest_id} has "
            f"api_call_attempted={manifest.get('api_call_attempted')!r}; "
            "nothing to resolve (it should have been auto-recovered)",
            file=sys.stderr,
        )
        return 1

    created_at = manifest.get("created_at")
    qids = manifest.get("queue_entry_ids") or []
    print(
        f"[hh_distill] manifest {target_manifest_id}: {len(qids)} entries, "
        f"created_at={created_at}",
        file=sys.stderr,
    )
    print(
        "[hh_distill] candidates must be inspected manually with the "
        "Anthropic dashboard / batches.list(). Once identified, use "
        "'hh_distill submit-manifest' (not yet implemented) to finalize. "
        "Until then, entries remain in submitting/ and are excluded from "
        "future runs (preventing double-charge).",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hh_distill")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run one preflight+submit+collect+publish cycle")
    sub.add_parser("status", help="print counts and manual-resolution hints")
    p_retry = sub.add_parser("retry", help="move failed/ entries for one session back to pending/")
    p_retry.add_argument("session_id")
    p_resolve = sub.add_parser(
        "resolve-submitting",
        help="show candidates for an ambiguous manifest",
    )
    p_resolve.add_argument("manifest_id")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "retry":
        return cmd_retry(args)
    if args.command == "resolve-submitting":
        return cmd_resolve_submitting(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
