"""scripts/hh_skill_sync.py — Lane C 同期エンジン（S-10 手順0〜8）＋ CLI。

親設計書: docs/hh-agent/03_Architecture.md §14
    - S-10: 同期の手順（フェーズ A 整合性検査 → フェーズ B 差分分類 → 分岐）
    - S-11: 通知（通常の CAS 成功では通知ゼロ。クライアント判定事象と
      サーバーイベントだけを outbox 経由で通知。送信成功したものだけ ACK）
    - S-12: 緊急停止（kill switch）と denylist
    - S-06b: promote receipt（pull 時の受信側検証・accepted_seq のリプレイ
      対策・push 時の来歴確認）

実行タイミング（S-10 確定事項 G）: Windows ネイティブ（12h スケジュール
タスク）と Modal ダッシュボード（`sync_dashboard_skills`、8h）が同じ
Python コードを共有する。CLI 固有の処理（argparse・print・終了コード）は
`main()` にだけ置き、それ以外は import 可能な関数として定義する。

== このモジュールが新規に実装するもの ==

既存の `hh_skill_promote.py` / `hh_agent_promote_lock.py` /
`modal_hub/services/skill_sync.py` を**呼ぶだけ**で、何も再実装しない:

    - ロック:        promote_lock(nonblocking=True)（S-10 手順0）
    - 配置:          install_confirmed_skill() / assert_staging_root_is_safe()
                     / self_heal_orphaned_promotions()（同一の 2 段フロー）
    - 監査:          append_promote_log()（promote_log.jsonl を同期と共有）
    - receipt:       promote_receipts/ の記録・current ポインタ・
                     accepted_seq（S-06b と同じ領域・同じ形式）
    - 受信側検証:    validate_pulled_skill() / verify_receipt()
    - 差分判定:      check_integrity() / classify_sync_action()
    - 送信:          push_to_lane_c()（フェイルオープン。push 後の state
                     更新は行わない — 応答の revision が返らないため。
                     次回実行時に metadata_repair が正しい revision へ進める）

== 通知方針（S-11・wave4 タスク指示） ==

- 通常の CAS 成功（衝突でない普通の pull）では ntfy 通知を一切出さない。
- 通知対象は「クライアントだけが判定できる事象」: 署名検証失敗
  （SyncValidationError）・整合性異常（フェーズ A）・双方変更の衝突。
  これらは `~/.hh-agent/skill_sync_outbox.jsonl`（アウトボックス）に積み、
  送信に成功したイベントだけを削除する（失敗は次回実行時に再送）。
- サーバー側が durable に保持するイベント（list の events）は、通知に
  成功したものだけを ACK する（通知失敗時は ACK しない → 次回再通知）。
- 通知本文はフィールド・ホワイトリスト（event/name/reason 等）のみ。
  SKILL.md 本文・差分は構造的に載らない。

== 時刻比較の禁止（確定事項 I） ==

ローカル時刻とリモート時刻を比較する式を一切書かない。判定材料は
ダイジェストとサーバー採番の revision のみ（classify_sync_action() が
保証済み。このファイルでは新たな時刻比較を追加しない）。

== 鍵・設定の供給元 ==

- Lane C 接続設定: hh_skill_promote.load_lane_c_config()（lane_c_config.json）
- receipt 検証鍵（pull）: .hh-signing.env の HH_AGENT_TOKEN_SIGNING_KEY と
  HH_AGENT_TOKEN_SIGNING_KEY_PREV（鍵ローテーション中も旧世代の receipt を
  検証できるよう両方を verify_keys に載せる）
- 書き込み鍵（ACK）: 同ファイルの C2S_SKILL_WRITE_KEY
- ntfy 資格情報: hh_issue_agent_token.load_ntfy_credentials()
  （.hh-secret.env / 環境変数。送信時に os.environ へ注入する）
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

# `scripts/__init__.py` は存在しないため、REPO_ROOT と scripts ディレクトリを
# 自分で sys.path へ入れる（hh_skill_promote.py と同じ方式。Modal 側の
# sync_dashboard_skills から `import hh_skill_sync` するときも同じ二段挿入が
# 必要になる）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _dir in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from modal_hub.services import ntfy_client, skill_sync  # noqa: E402
from modal_hub.services.skill_sync import (  # noqa: E402
    IntegrityAnomalyError,
    LaneCApiError,
    LocalSkillState,
    PulledSkill,
    RemoteSkillState,
    SyncValidationError,
    derive_key_id,
)
from hh_agent_promote_lock import promote_lock  # noqa: E402
from hh_issue_agent_token import load_ntfy_credentials  # noqa: E402
import hh_skill_promote as promote  # noqa: E402

# ---------------------------------------------------------------------------
# 定数（S-12 / S-11 / S-06b）
# ---------------------------------------------------------------------------

#: 同期状態の保存先（`~/.hh-agent/skill_sync_state.json`）。
#: 形式: `{name: {"content_sha256": str, "lane_c_revision": int}}`
SYNC_STATE_FILENAME = "skill_sync_state.json"

#: denylist の保存先（S-12）。形式: `{"names": [...], "content_sha256": [...]}`
DENYLIST_FILENAME = "skill_sync_denylist.json"

#: 緊急停止フラグ（S-12）。ファイルの存在、または
#: `HH_SKILL_SYNC_DISABLED=1` で同期が止まる。
DISABLED_FLAG_FILENAME = "skill_sync_disabled"
DISABLED_ENV_VAR = "HH_SKILL_SYNC_DISABLED"

#: 通知アウトボックス（S-11）。`{event_id, event}` の JSONL。
OUTBOX_FILENAME = "skill_sync_outbox.jsonl"

#: accepted_seq の保存先（promote_receipts/<name>/accepted_seq.json、S-06b）。
#: 形式: `{origin_instance: int}`（受け入れ済みの最大 promotion_seq）。
ACCEPTED_SEQ_FILENAME = "accepted_seq.json"

#: receipt 検証鍵の旧世代用環境変数名（.hh-signing.env / 環境変数）。
SIGNING_KEY_PREV_VAR = "HH_AGENT_TOKEN_SIGNING_KEY_PREV"

#: 通知本文に載せる reason の長さ上限（S-11。本文が肥大化しないよう
#: 切り詰めてから送る。SKILL.md 本文の混入経路があってもこの切り詰めと
#: フィールド・ホワイトリストの二重防御で漏れない）。
MAX_REASON_CHARS = 120


# ---------------------------------------------------------------------------
# S-12: 緊急停止（kill switch）と denylist
# ---------------------------------------------------------------------------


def is_sync_disabled(*, base: Optional[Path] = None) -> bool:
    """`~/.hh-agent/skill_sync_disabled` ファイル、または環境変数
    `HH_SKILL_SYNC_DISABLED=1` で True（S-12）。どちらか一方が効く。

    Args:
        base: `~/.hh-agent` の代わりに使うベースディレクトリ（テスト用）。
    """
    home = base if base is not None else promote.hh_agent_home()
    if (home / DISABLED_FLAG_FILENAME).exists():
        return True
    return os.environ.get(DISABLED_ENV_VAR) == "1"


def load_denylist(*, base: Optional[Path] = None) -> dict:
    """`~/.hh-agent/skill_sync_denylist.json` を読む（S-12）。

    形式: `{"names": ["<name>", ...], "content_sha256": ["<sha256>", ...]}`。
    ファイルが無ければ空 denylist。壊れた JSON・型不正は警告して空
    denylist 扱い（fail-open。真の緊急停止は `skill_sync_disabled`
    ファイルの役目 — ここを fail-closed にすると denylist の一時破損で
    全同期が止まる）。
    """
    home = base if base is not None else promote.hh_agent_home()
    path = home / DENYLIST_FILENAME
    if not path.is_file():
        return {"names": [], "content_sha256": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(
            f"[hh_skill_sync] WARN: denylist を読み込めない（空扱いで続行）: {path}",
            file=sys.stderr,
        )
        return {"names": [], "content_sha256": []}
    if not isinstance(data, dict):
        return {"names": [], "content_sha256": []}
    names = data.get("names")
    digests = data.get("content_sha256")
    return {
        "names": [n for n in names if isinstance(n, str)] if isinstance(names, list) else [],
        "content_sha256": [d for d in digests if isinstance(d, str)] if isinstance(digests, list) else [],
    }


def is_denied(name: str, content_sha256: Optional[str], denylist: dict) -> bool:
    """`name` または `content_sha256` が denylist に載っていれば True（S-12）。

    denylist に載ったスキルは pull / push / 通知のすべてから除外される。
    """
    if name in denylist.get("names", []):
        return True
    if content_sha256 and content_sha256 in denylist.get("content_sha256", []):
        return True
    return False


# ---------------------------------------------------------------------------
# 同期状態（skill_sync_state.json）
# ---------------------------------------------------------------------------


def load_sync_state(*, base: Optional[Path] = None) -> dict:
    """`~/.hh-agent/skill_sync_state.json` を読む。

    形式: `{name: {"content_sha256": str, "lane_c_revision": int}}`。

    ファイルが無い・JSON として壊れている・型不正エントリは破棄して
    「状態なし」扱いにする（`classify_sync_action()` が安全側に倒す:
    リモートのみ存在 → pull、双方存在・sha 不一致 → conflict）。
    """
    home = base if base is not None else promote.hh_agent_home()
    path = home / SYNC_STATE_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for name, entry in data.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        sha = entry.get("content_sha256")
        rev = entry.get("lane_c_revision")
        if not isinstance(sha, str) or not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
            continue
        out[name] = {"content_sha256": sha, "lane_c_revision": rev}
    return out


def save_sync_state(state: dict, *, base: Optional[Path] = None) -> None:
    """`skill_sync_state.json` を原子的に保存する（temp + os.replace）。

    `_atomic_write_json()` を再利用するため promote と同期の書き込みは
    同じ原子性保証を持つ（S-10: クラッシュしても state が torn にならない）。
    """
    home = base if base is not None else promote.hh_agent_home()
    promote._atomic_write_json(home / SYNC_STATE_FILENAME, state)


# ---------------------------------------------------------------------------
# S-06b: accepted_seq（リプレイ対策）と receipt の記録
# ---------------------------------------------------------------------------


def _accepted_seq_path(name: str, base: Optional[Path] = None) -> Path:
    return promote._promote_receipts_root(base) / name / ACCEPTED_SEQ_FILENAME


def load_accepted_seq(name: str, *, base: Optional[Path] = None) -> dict:
    """`promote_receipts/<name>/accepted_seq.json` を読む（S-06b）。

    戻り値: `{"<origin_instance>": <受け入れ済み最大 seq>}`。
    非負整数以外のエントリ・壊れた JSON・ファイル不在は無視して
    空 dict（=未受け入れ）を返す。**検証済みの値だけがここに到達する**
    （書き込み側 `update_accepted_seq()` の呼び出し元が検証済みであること。
    この関数自体は読み取り専用）。
    """
    path = _accepted_seq_path(name, base)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for origin, seq in data.items():
        if isinstance(origin, str) and isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
            out[origin] = seq
    return out


def update_accepted_seq(name: str, origin_instance: str, seq: int, *, base: Optional[Path] = None) -> None:
    """`accepted_seq[origin_instance]` を `max(既存値, seq)` に更新して保存する。

    **既に検証済みの promotion_seq だけを渡すこと**（この関数は受け取った
    seq を検証しない。リプレイ対策は「検証済みの値だけを受理する」で機能
    する — S-06b）。更新が無ければ書き込まない（無駄な書き込みを避ける）。
    保存は `_atomic_write_json()`（temp + os.replace）。
    """
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError(f"seq must be a non-negative int, got {seq!r}")
    accepted = load_accepted_seq(name, base=base)
    if accepted.get(origin_instance, -1) >= seq:
        return
    accepted[origin_instance] = seq
    promote._atomic_write_json(_accepted_seq_path(name, base), accepted)


def load_verified_receipt_current(name: str, *, base: Optional[Path] = None) -> Optional[str]:
    """`promote_receipts/<name>/current` の内容（receipt ファイル名）を返す。

    hh_skill_promote.write_receipt() が書く形式
    （`<content_sha8>-<receipt_sha8>.json`）と完全に一致する。
    無ければ None。
    """
    name_dir = promote._promote_receipts_root(base) / name
    try:
        filename = (name_dir / "current").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return filename or None


def _read_receipt_record(name: str, filename: Optional[str], *, base: Optional[Path] = None) -> Optional[dict]:
    """`promote_receipts/<name>/<filename>` の JSON record を読む。無ければ None。"""
    if not filename:
        return None
    name_dir = promote._promote_receipts_root(base) / name
    try:
        data = json.loads((name_dir / filename).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _as_record_dict(remote) -> dict:
    """検証済みの pull 結果を record dict に正規化する。

    `validate_pulled_skill()` の戻り値（PulledSkill フローズンデータクラス）
    またはその `dataclasses.asdict()` 結果を受け付ける。どちらでもない型は
    呼び出し側のバグなので TypeError。
    """
    if isinstance(remote, PulledSkill):
        return dataclasses.asdict(remote)
    if isinstance(remote, dict):
        return remote
    raise TypeError("remote must be a validated PulledSkill or its asdict")


def save_verified_receipt(name: str, remote: dict, *, base: Optional[Path] = None) -> None:
    """pull 成功時、検証済みのリモート receipt を保存して current を差し替える（S-06b）。

    hh_skill_promote.write_receipt() と同じファイル形式・同じディレクトリ
    （`promote_receipts/<name>/`）を使い、2 つの別実装を作らない:

    - ファイル名: `<content_sha8>-<receipt の sha8>.json`
    - current ポインタ: `_atomic_write_text()` で書く

    `remote` は `validate_pulled_skill()` を通過済みの PulledSkill（または
    its asdict）。**検証済みの値だけを書き込む**（自己申告値を信用しない）。
    既に current が同じ receipt を指していれば何もしない（べき等）。
    """
    data = _as_record_dict(remote)
    digest = data.get("content_sha256")
    receipt = data.get("receipt")
    origin_instance = data.get("origin_instance")
    promoted_at_ms = data.get("promoted_at_ms")
    promotion_seq = data.get("promotion_seq")
    if not all(isinstance(v, str) for v in (digest, receipt, origin_instance)):
        raise ValueError("remote に content_sha256 / receipt / origin_instance の文字列が必要")
    if not isinstance(promoted_at_ms, int) or isinstance(promoted_at_ms, bool) or not isinstance(promotion_seq, int) or isinstance(promotion_seq, bool):
        raise ValueError("remote に promoted_at_ms / promotion_seq の非負整数が必要")
    distilled = data.get("distilled_from_session_id")
    if distilled is not None and not isinstance(distilled, str):
        raise ValueError("distilled_from_session_id は文字列または None")
    filename = f"{digest[:8]}-{hashlib.sha256(receipt.encode('utf-8')).hexdigest()[:8]}.json"
    current = load_verified_receipt_current(name, base=base)
    if current == filename:
        existing = _read_receipt_record(name, filename, base=base)
        if existing is not None and existing.get("receipt") == receipt:
            return  # 既に同じ receipt が保存済み。書き込まない
    record = {
        "name": name,
        "content_sha256": digest,
        "origin_instance": origin_instance,
        "promoted_at_ms": promoted_at_ms,
        "promotion_seq": promotion_seq,
        "distilled_from_session_id": distilled,
        "key_id": receipt.split(".", 1)[0],  # receipt に埋め込まれた key_id（write_receipt と同形式）
        "receipt": receipt,
    }
    name_dir = promote._promote_receipts_root(base) / name
    promote._atomic_write_json(name_dir / filename, record)
    promote._atomic_write_text(name_dir / "current", filename)


# ---------------------------------------------------------------------------
# S-11: 通知アウトボックス（送信成功したイベントだけを削除する）
# ---------------------------------------------------------------------------


def _outbox_path(base: Optional[Path] = None) -> Path:
    home = base if base is not None else promote.hh_agent_home()
    return home / OUTBOX_FILENAME


def _event_id(event: dict) -> str:
    """イベント内容（canonical JSON）の sha256。重複排除・送信結果の対応付け ID。"""
    canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def append_outbox(event: dict, *, base: Optional[Path] = None) -> str:
    """`~/.hh-agent/skill_sync_outbox.jsonl` へイベントを 1 件追記する（S-11）。

    各行は `{"event_id": <内容ハッシュ>, "event": {...}}`。event_id が既に
    ファイル内にあれば追記しない（重複排除 — 同一事象を二重に通知しない）。
    送信は `flush_outbox()` が行い、成功した行だけを消す（失敗は次回再送）。

    Returns:
        event_id（テスト・監査で送信結果と対応付けるために返す）。
    """
    event_id = _event_id(event)
    path = _outbox_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("event_id") == event_id:
                    return event_id  # 既に積まれている
        except OSError:
            pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"event_id": event_id, "event": event}, ensure_ascii=False) + "\n")
    return event_id


def _ensure_ntfy_env() -> None:
    """ntfy 資格情報を `.hh-secret.env` から os.environ へ注入する。

    `hh_issue_agent_token.load_ntfy_credentials()` を再利用する
    （2 スクリプトで重複実装しない）。環境変数が既に設定されていれば
    それが優先される（注入は None 以外のみ）。
    """
    topic, token = load_ntfy_credentials()
    if topic:
        os.environ["NTFY_TOPIC"] = topic
    if token:
        os.environ["NTFY_TOKEN"] = token


def _send_notification(event: dict) -> str:
    """イベント種別に応じた ntfy 送信を 1 回試みる（S-11）。

    - `"skill_conflict"` → ntfy_client.send_skill_conflict()（既存。S-11 の
      5 フィールド本文）
    - それ以外 → ntfy_client.send_skill_sync_event()（wave4 追加。
      event/name/reason のホワイトリスト本文）

    Returns:
        `"sent"` / `"failed"`。失敗しても例外は投げない（フェイルオープン。
        同期フローを止めない）。
    """
    try:
        if event.get("event") == "skill_conflict":
            return ntfy_client.send_skill_conflict(event)
        return ntfy_client.send_skill_sync_event(event)
    except Exception as exc:  # noqa: BLE001 — 通知の失敗は同期を止めない
        print(
            f"[hh_skill_sync] WARN: 通知の送信に失敗（{type(exc).__name__}: {exc}）。"
            "次回実行時に再送する",
            file=sys.stderr,
        )
        return "failed"


def flush_outbox(*, base: Optional[Path] = None) -> dict:
    """outbox の各未送信イベントを ntfy へ送る（S-11）。

    - 送信成功したイベントのみ outbox から削除する（失敗は次回実行時に再送）。
    - 種別ごとに送信関数を振り分ける（`_send_notification()`）。
    - ntfy 資格情報は `.hh-secret.env` から注入してから送る。
    - 解釈できない行は消さない（フェイルクローズ。人間が確認できるように）。

    Returns:
        `{"attempted": int, "sent": int, "failed": int,
         "results": {event_id: "sent"|"failed"}}`。
        `results` は呼び出し側が監査行（promote_log.jsonl の notify_state）に
        反映するためのもの。
    """
    path = _outbox_path(base)
    empty = {"attempted": 0, "sent": 0, "failed": 0, "results": {}}
    if not path.is_file():
        return empty
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return empty
    if not lines:
        return empty
    _ensure_ntfy_env()
    results: dict = {}
    remaining: list[str] = []
    for line in lines:
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            remaining.append(line)  # 解釈できない行は消さない
            continue
        if not isinstance(entry, dict):
            remaining.append(line)
            continue
        event_id = entry.get("event_id")
        event = entry.get("event")
        if not isinstance(event_id, str) or not isinstance(event, dict):
            remaining.append(line)
            continue
        outcome = _send_notification(event)
        results[event_id] = outcome
        if outcome != "sent":
            remaining.append(line)
    if len(remaining) != len(lines):
        text = "\n".join(remaining)
        if remaining:
            text += "\n"
        try:
            promote._atomic_write_text(path, text)
        except OSError:
            print("[hh_skill_sync] WARN: outbox の書戻しに失敗（次回再試行する）", file=sys.stderr)
    sent = sum(1 for v in results.values() if v == "sent")
    return {
        "attempted": len(results),
        "sent": sent,
        "failed": len(results) - sent,
        "results": results,
    }


# ---------------------------------------------------------------------------
# S-06b: pull 受信側の検証鍵（現在鍵 + 前世代鍵）
# ---------------------------------------------------------------------------


def _read_signing_env_value(name: str) -> Optional[str]:
    """`.hh-signing.env`（または環境変数）から 1 つの値を読む。

    `promote._load_signing_env()` と同じパース方式（KEY=VALUE・コメント行
    スキップ・両端の引用符を剥がす。環境変数優先）を踏襲する。`_load_signing_
    env()` が返さない追加キー（`HH_AGENT_TOKEN_SIGNING_KEY_PREV`）を読む
    ための最小ヘルパー。
    """
    env_value = os.environ.get(name)
    if env_value is not None:
        stripped = env_value.strip().strip('"').strip("'")
        return stripped or None
    env_path = _REPO_ROOT / promote.SIGNING_ENV_FILENAME
    if not env_path.is_file():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                stripped = value.strip().strip('"').strip("'")
                return stripped or None
    except OSError:
        return None
    return None


def load_verify_keys() -> dict:
    """pull の receipt 検証に使う鍵群（S-06b）。

    `.hh-signing.env`（または環境変数）の `HH_AGENT_TOKEN_SIGNING_KEY` と
    `HH_AGENT_TOKEN_SIGNING_KEY_PREV` の両方を `{key_id: 鍵 bytes}` で返す。
    鍵ローテーション中、旧鍵で署名された receipt も検証できるようにする
    ため。PREV が無ければ現在鍵だけ。

    この関数を呼ぶ時点で**検証する鍵が 1 つも無ければ空 dict**（= 受け付け
    ない）。空 dict のまま pull すると全 receipt が「未知の鍵」で弾かれる
    （フェイルクローズ — 鍵の無い環境で pull を許可しない）。
    """
    env = promote._load_signing_env()
    keys: dict = {}
    signing_raw = env.get(promote.SIGNING_KEY_VAR)
    if signing_raw:
        key_bytes = signing_raw.encode("utf-8")
        keys[derive_key_id(key_bytes)] = key_bytes
    prev_raw = _read_signing_env_value(SIGNING_KEY_PREV_VAR)
    if prev_raw:
        key_bytes = prev_raw.encode("utf-8")
        keys[derive_key_id(key_bytes)] = key_bytes
    return keys


# ---------------------------------------------------------------------------
# sync_pull（S-10 手順4〜5 の適用部分。ロック内で完結する）
# ---------------------------------------------------------------------------


def sync_pull(
    name: str,
    remote: dict,
    *,
    base: Optional[Path] = None,
    state: Optional[dict] = None,
    provenance: Optional[str] = None,
    write_promote_log: bool = True,
    install_info: Optional[dict] = None,
) -> str:
    """検証済みのリモート版をロック内で適用する（S-10 疑似コードのとおり）。

    ```
    with promote_lock(nonblocking=True) as got:
        if not got:
            return "skipped(locked)"
        assert_staging_root_is_safe()
        self_heal_orphaned_promotions()
        install_confirmed_skill(name, content, digest, force=True,
                                provenance=label)
        save_verified_receipt(name, remote)
        append_promote_log(...)
        update_accepted_seq(...)
        update_sync_state(...)      # state を渡した場合のみ
    ```

    Args:
        name: スキル名。
        remote: `validate_pulled_skill()` を通過済みの PulledSkill（または
            its asdict）。検証済みの値だけを書き込む。
        state: run_sync が保持する `skill_sync_state` の dict。渡すとロック内
            でこの name のエントリを更新・保存する（state を渡さなければ
            更新しない — 呼び出し側が自前で管理する場合用）。
        provenance: 監査ラベル。None なら `"sync-pull:<origin_instance>"`。
            衝突解決経路（S-10: provenance="sync-conflict"）から呼ばれる
            場合は明示的に渡す。
        write_promote_log: True（既定）なら `append_promote_log()` を
            この関数の内部で書く（通常の pull 経路はこれが唯一の書き手）。
            False の場合はここでは書かず、`install_info` へ install 結果
            （`destination`/`backup_path`）を詰めるだけにする——呼び出し元
            （`_do_conflict()`）が winner/loser/notify_state と統合した
            **1行だけ**を書けるようにするため（S-10「promote_log.jsonl に
            provenance="sync-conflict" で 1 件記録する」。内部の書き込みを
            止めずに呼び出し元も別に書くと同一イベントに 2 行残ってしまう）。
        install_info: 渡された場合、`install_confirmed_skill()` の戻り値
            （`destination`/`backup_path`）で更新する（out パラメータ）。

    Returns:
        `"pulled"` — 配置した。
        `"skipped(locked)"` — ロックを取れなかった（次回実行時に再試行）。
    """
    pulled = _as_record_dict(remote)
    origin = pulled["origin_instance"]
    content = pulled["content"]
    digest = pulled["content_sha256"]
    label = provenance or f"sync-pull:{origin}"
    with promote_lock(nonblocking=True, base=base) as got:
        if not got:
            return "skipped(locked)"
        promote.assert_staging_root_is_safe(base=base)
        promote.self_heal_orphaned_promotions(base=base)
        install_result = promote.install_confirmed_skill(
            name,
            content.encode("utf-8"),
            digest,
            force=True,
            provenance=label,
        )
        if install_info is not None:
            install_info.update(install_result)
        save_verified_receipt(name, pulled, base=base)
        if write_promote_log:
            promote.append_promote_log(
                name=name,
                digest=digest,
                content_bytes=content.encode("utf-8"),
                destination=install_result["destination"],
                forced=True,
                backup_path=install_result["backup_path"],
                provenance=label,
                promoted_at_ms=pulled["promoted_at_ms"],
                base=base,
            )
        update_accepted_seq(name, origin, pulled["promotion_seq"], base=base)
        if state is not None:
            state[name] = {"content_sha256": digest, "lane_c_revision": pulled["revision"]}
            save_sync_state(state, base=base)
    return "pulled"


# ---------------------------------------------------------------------------
# 分岐ハンドラ（S-10 手順5 の各ケース）
# ---------------------------------------------------------------------------


def _queue_client_event(name: str, kind: str, reason: str, *, base, pending, result) -> str:
    """クライアント側判定イベント（整合性異常・署名検証失敗）を通知・監査の
    キューに積む（S-11）。**ローカルへは一切書かない。**

    - アウトボックスへ通知イベントを 1 件積む（`append_outbox()`。送信は
      run_sync の最後にまとめて flush）。
    - `pending` に監査行（provenance は `sync-integrity-anomaly` /
      `sync-validation-failed`）を積む。promote_log.jsonl への書き込みは
      flush 後に `notify_state` を確定して行う（`_write_pending_audits()`）。

    Returns:
        event_id。
    """
    if kind == "integrity":
        event_type = "skill_sync_integrity_anomaly"
        provenance = "sync-integrity-anomaly"
        result.setdefault("integrity_anomalies", []).append(name)
    else:
        event_type = "skill_sync_validation_failed"
        provenance = "sync-validation-failed"
        result.setdefault("validation_failures", []).append(name)
    event = {"event": event_type, "name": name, "reason": reason[:MAX_REASON_CHARS]}
    event_id = append_outbox(event, base=base)
    pending.append({
        "name": name,
        "provenance": provenance,
        "reason": reason,
        "event_id": event_id,
        "promoted_at_ms": int(time.time() * 1000),
    })
    return event_id


def _write_pending_audits(pending: list, flush_results: dict, *, base: Optional[Path] = None) -> None:
    """flush 後にクライアント判定イベントの監査行を promote_log.jsonl へ書く。

    `notify_state` は `flush_outbox()` の実結果を反映する
    （`"sent"` / `"failed"`。送られなかったイベントは既定で `"failed"`）。
    """
    if not pending:
        return
    log_path = promote._promote_log_path(base)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        for entry in pending:
            record = {
                "name": entry["name"],
                "promoted_at": entry["promoted_at_ms"] / 1000.0,
                "promoted_at_ms": entry["promoted_at_ms"],
                "provenance": entry["provenance"],
                "notify_state": flush_results.get(entry["event_id"], "failed"),
            }
            for key in ("reason", "winner_sha8", "loser_sha8", "winner_origin", "backup_path", "destination"):
                if entry.get(key) is not None:
                    record[key] = entry[key]
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _append_skipped_audit(name: str, reason: str, *, base: Optional[Path] = None) -> None:
    """push を送らなかったことの監査行を promote_log.jsonl へ残す（S-10）。"""
    promoted_at_ms = int(time.time() * 1000)
    record = {
        "name": name,
        "promoted_at": promoted_at_ms / 1000.0,
        "promoted_at_ms": promoted_at_ms,
        "provenance": "sync-push-skipped",
        "reason": reason,
    }
    log_path = promote._promote_log_path(base)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _pull_and_validate(name: str, cfg, verify_keys: dict):
    """pull → 受信側検証（S-10 手順4）。SyncValidationError / LaneCApiError
    はそのまま伝播する（呼び出し側が通知・再試行を判断する）。"""
    remote = skill_sync.pull_skill(name, base_url=cfg.base_url, read_key=cfg.read_key)
    return skill_sync.validate_pulled_skill(remote, verify_keys=verify_keys)


def _do_pull(name, cfg, verify_keys, *, dry_run, base, pending, result, sync_state) -> None:
    """S-10「pull」分岐: リモートを pull して検証し、`sync_pull()` で適用する。

    受信側検証に失敗したら**何も書かず**、通知（アウトボックス）と監査だけ
    を積む（S-11）。署名検証失敗はクライアントだけが判定できる事象。
    """
    if dry_run:
        result["pulled"].append(name)  # 判定のみ。書き込み・通知なし
        return
    try:
        pulled = _pull_and_validate(name, cfg, verify_keys)
    except SyncValidationError as exc:
        _queue_client_event(name, "validation", str(exc), base=base, pending=pending, result=result)
        return
    except LaneCApiError as exc:
        print(f"[hh_skill_sync] WARN: {name} の pull に失敗した（次回再試行）: {type(exc).__name__}", file=sys.stderr)
        result["pull_deferred"].append(name)
        return
    outcome = sync_pull(name, pulled, base=base, state=sync_state)
    if outcome == "pulled":
        result["pulled"].append(name)
    else:
        result["skipped_locked"].append(name)


def _do_metadata_repair(name, cfg, verify_keys, *, dry_run, base, pending, result, sync_state) -> None:
    """S-10「metadata_repair」分岐: メタデータの自己修復（設計書 1236〜1244）。

    本文は書き換えず、検証済みの値で receipt・accepted_seq を自己修復し、
    `skill_sync_state` の revision だけを更新する。リモート receipt は通常の
    pull 検証（S-10 手順4）に必ず通す — 検証に失敗したら何も更新せず中断
    （自己修復が署名されていないデータを書くことを禁止する）。

    この自己修復も state と promote_receipts/ を書くため `promote_lock`
    （nonblocking）の下で行う（取れなければ次回へ）。
    """
    if dry_run:
        result["metadata_repair"].append(name)
        return
    try:
        pulled = _pull_and_validate(name, cfg, verify_keys)
    except SyncValidationError as exc:
        _queue_client_event(name, "validation", str(exc), base=base, pending=pending, result=result)
        return
    except LaneCApiError as exc:
        print(f"[hh_skill_sync] WARN: {name} の pull に失敗した（自己修復は次回へ）: {type(exc).__name__}", file=sys.stderr)
        result["pull_deferred"].append(name)
        return
    with promote_lock(nonblocking=True, base=base) as got:
        if not got:
            result["skipped_locked"].append(name)
            return
        save_verified_receipt(name, pulled, base=base)
        update_accepted_seq(name, pulled.origin_instance, pulled.promotion_seq, base=base)
        sync_state[name] = {"content_sha256": pulled.content_sha256, "lane_c_revision": pulled.revision}
        save_sync_state(sync_state, base=base)
    result["metadata_repair"].append(name)


def _do_push(name, local_sha, state_entry, cfg, *, base, result, hermes_skills_root) -> None:
    """S-10「push」分岐（設計書 1257〜1266 行目: push 候補の追加の必須条件）。

    `promote_receipts/<name>/current` が存在し、その `content_sha256` が
    **現在のローカル本文の sha256 と一致する**場合だけ push する（= 来歴が
    確認できる）。満たさない場合は `skipped(no-valid-receipt)` として監査に
    残し送らない（無来歴のローカル版を勝手に Lane C へ流さない）。

    receipt は既存の current ファイルから読む（**新規に署名し直さない** —
    S-06b: 署名は promote 実行時にだけ作られる）。`push_to_lane_c()` は
    フェイルオープン（応答の revision を返さない）ため、push 後の
    skill_sync_state 更新は行わない。次回実行時に
    local.sha == remote.sha → metadata_repair が正しい revision へ進める
    （S-10 の安全側。誤った revision を書き込むより、余分な pull が 1 回
    走る方が害が小さい）。
    """
    current_name = load_verified_receipt_current(name, base=base)
    record = _read_receipt_record(name, current_name, base=base) if current_name else None
    if record is None or record.get("content_sha256") != local_sha:
        _append_skipped_audit(
            name,
            reason="no-valid-receipt（promote_receipts/current が無い・ローカル digest と一致しない）",
            base=base,
        )
        result["skipped_no_valid_receipt"].append(name)
        return
    receipt = record.get("receipt")
    if not isinstance(receipt, str) or not skill_sync.is_valid_receipt_format(receipt):
        _append_skipped_audit(name, reason="no-valid-receipt（receipt 形式不正）", base=base)
        result["skipped_no_valid_receipt"].append(name)
        return
    promoted_at_ms = record.get("promoted_at_ms")
    origin_instance = record.get("origin_instance")
    if not isinstance(promoted_at_ms, int) or isinstance(promoted_at_ms, bool) or not isinstance(origin_instance, str):
        _append_skipped_audit(name, reason="no-valid-receipt（record のフィールド型不正）", base=base)
        result["skipped_no_valid_receipt"].append(name)
        return
    distilled = record.get("distilled_from_session_id")
    if distilled is not None and not isinstance(distilled, str):
        distilled = None
    state_rev = state_entry.get("lane_c_revision") if isinstance(state_entry, dict) else None
    base_revision = state_rev if isinstance(state_rev, int) and not isinstance(state_rev, bool) else 0
    try:
        content_bytes = (hermes_skills_root / name / "SKILL.md").read_bytes()
    except OSError:
        _append_skipped_audit(name, reason="push failed（ローカル SKILL.md が読めない）", base=base)
        result["pull_deferred"].append(name)
        return
    promote.push_to_lane_c(
        name=name,
        digest=local_sha,
        promoted_at_ms=promoted_at_ms,
        receipt=receipt,
        origin_instance=origin_instance,
        distilled_from_session_id=distilled,
        content_bytes=content_bytes,
        base_revision=base_revision,
    )
    result["pushed"].append(name)


def _do_conflict(name, local_sha, remote_state, cfg, verify_keys, *, dry_run, base, pending, result, sync_state) -> None:
    """S-10「conflict」分岐（設計書 1248〜1254 行目の順序どおり）:

    (1) ntfy 通知を先に出す（アウトボックスへ積む。送信は run_sync 末尾の
        flush で行い、結果を監査行の notify_state に反映する）
    (2) ローカル版を `promote_backups/<name>.bak.<ts>/` へ複製する（写し。
        退避ではない。続くステップが失敗してもローカル版が消えない）
    (3) リモート版を pull → 受信側検証 → `sync_pull()`（provenance=
        "sync-conflict"）で配置する
    (4) promote_log.jsonl に provenance="sync-conflict"・勝者/敗者 digest・
        退避先・通知結果を記録する（flush 後に確定）
    (5) 衝突時は自動 push しない（解決は次の同期サイクルがリモート版を
        前提に継続する）
    """
    if dry_run:
        result["conflicts"].append(name)
        return
    loser_sha8 = local_sha[:8] if local_sha else "unknown"
    winner_sha8 = remote_state.content_sha256[:8]
    notify_event = {
        "event": "skill_conflict",
        "name": name,
        "winner": remote_state.origin_instance,
        "winner_sha8": winner_sha8,
        "loser_sha8": loser_sha8,
    }
    event_id = append_outbox(notify_event, base=base)
    # (2) ローカル版の写しを promote_backups/<name>.conflict-local.<ts>/ へ複製。
    # `install_staged_skill(force=True)`（sync_pull() 経由で直後に呼ばれる）も
    # 同じ promote_backups/ 配下へ `<name>.bak.<ts>` という秒精度タイムスタンプの
    # バックアップを作る。この複製呼び出しとその後続呼び出しは数ミリ秒しか
    #離れておらず、同一秒内で両者が同じファイル名を生成すると
    # `os.replace()` が既存ディレクトリへの置換を拒否し WinError 5 で失敗する
    # （Windows は MOVEFILE_REPLACE_EXISTING をディレクトリ宛先には使えない）。
    # 接頭辞を変えて命名空間を完全に分離することで、タイミングに関わらず
    # 衝突を構造的に排除する。
    hermes_skills_root = promote._hermes_skills_root()
    local_dir = hermes_skills_root / name
    backups_root = promote._promote_backups_root(base)
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    backup_dir = backups_root / f"{name}.conflict-local.{ts}"
    if local_dir.is_dir():
        backups_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(local_dir, backup_dir)
    # (3) リモート版を pull → 検証 → sync_pull() で配置
    try:
        pulled = _pull_and_validate(name, cfg, verify_keys)
    except SyncValidationError as exc:
        _queue_client_event(name, "validation", str(exc), base=base, pending=pending, result=result)
        return
    except LaneCApiError as exc:
        print(f"[hh_skill_sync] WARN: {name} の衝突解決 pull に失敗した（次回再試行）: {type(exc).__name__}", file=sys.stderr)
        result["pull_deferred"].append(name)
        return
    # sync_pull() 自身の promote_log 書き込みは止める（write_promote_log=False）。
    # ここで書く 1 行に winner/loser/notify_state と install 結果（destination・
    # 内部バックアップの有無）をすべて統合し、同一イベントに 2 行残さない。
    install_info: dict = {}
    outcome = sync_pull(
        name, pulled, base=base, state=sync_state, provenance="sync-conflict",
        write_promote_log=False, install_info=install_info,
    )
    # (4) 監査行は flush 後に notify_state を確定して書く（pending に積む）
    pending.append({
        "name": name,
        "provenance": "sync-conflict",
        "winner_sha8": winner_sha8,
        "loser_sha8": loser_sha8,
        "winner_origin": remote_state.origin_instance,
        "backup_path": str(backup_dir),
        "destination": str(install_info["destination"]) if install_info.get("destination") is not None else None,
        "event_id": event_id,
        "promoted_at_ms": int(time.time() * 1000),
    })
    if outcome == "skipped(locked)":
        result["skipped_locked"].append(name)
    else:
        result["conflicts"].append(name)


# ---------------------------------------------------------------------------
# run_sync（S-10 手順0〜8 の統合）
# ---------------------------------------------------------------------------


def run_sync(*, pull: bool, reconcile: bool, dry_run: bool = False, base: Optional[Path] = None) -> dict:
    """メインの同期ループ（S-10 手順0〜8）。戻り値は集計結果の dict。

    手順:
        1. `is_sync_disabled()`（S-12）なら何もせず終了。
        2. Lane C 設定（`load_lane_c_config()`）が無ければ警告して終了。
        3. `list_all_skills()` で全ページ取得。未 ACK のサーバーイベントは
           通知に成功したものだけ ACK する（S-11。通知失敗時は ACK しない
           → 次回実行時に再通知）。
        4. denylist（S-12）で除外（name / content_sha256 の両方で判定）。
        5. 各 name についてフェーズ A（`check_integrity()`）→ フェーズ B
           （`classify_sync_action()`）で分類し、結果ごとに分岐:
           noop / metadata_repair（自己修復）/ pull / push（来歴確認済みのみ）/
           conflict（S-10 設計書 1248〜1254 の順序）/ 整合性異常（通知のみ）。
        6. `flush_outbox()` でクライアント側アウトボックスの通知を送る
           （送信成功したものだけ削除）。監査行の notify_state を確定する。

    Args:
        pull: True なら pull 分岐を実行する（False なら pull_deferred に
            集計して次回へ）。
        reconcile: True なら push 分岐を実行する（Modal 側は False —
            S-08「Modal コンテナ上での push は v1 では発生しない」）。
        dry_run: True なら分類・集計だけ行い、書き込み・push・通知・
            ロック・HTTP（list 除く）を一切行わない。
        base: `~/.hh-agent` の代わりに使うベースディレクトリ（テスト用）。

    Returns:
        `{
          "disabled": bool,
          "config_present": bool,
          "list_failed": bool,
          "observed": [Lane C に存在し denylist 除外されなかった name],
          "remote_sha256": {name: content_sha256},   # S-08c 消し込み用
          "denied": [...],
          "noop": [...],
          "metadata_repair": [...],
          "pulled": [...],
          "pushed": [...],
          "conflicts": [...],
          "integrity_anomalies": [...],
          "validation_failures": [...],
          "skipped_locked": [...],
          "skipped_no_valid_receipt": [...],
          "pull_deferred": [...],
          "push_deferred": [...],
          "events_seen": int, "events_acked": int,
          "notifications": {"attempted": int, "sent": int, "failed": int},
        }`
    """
    result = {
        "disabled": False,
        "config_present": True,
        "list_failed": False,
        "observed": [],
        "remote_sha256": {},
        "denied": [],
        "noop": [],
        "metadata_repair": [],
        "pulled": [],
        "pushed": [],
        "conflicts": [],
        "integrity_anomalies": [],
        "validation_failures": [],
        "skipped_locked": [],
        "skipped_no_valid_receipt": [],
        "pull_deferred": [],
        "push_deferred": [],
        "events_seen": 0,
        "events_acked": 0,
        "notifications": {"attempted": 0, "sent": 0, "failed": 0},
    }
    # 手順1: 緊急停止（S-12）
    if is_sync_disabled(base=base):
        result["disabled"] = True
        print("[hh_skill_sync] sync disabled（skill_sync_disabled / HH_SKILL_SYNC_DISABLED）", file=sys.stderr)
        return result
    # 手順2: Lane C 設定
    cfg = promote.load_lane_c_config(base=base)
    if cfg is None:
        result["config_present"] = False
        print("[hh_skill_sync] WARN: Lane C 設定（lane_c_config.json）が無いため同期しない", file=sys.stderr)
        return result
    if not cfg.read_key:
        result["config_present"] = False
        print("[hh_skill_sync] WARN: Lane C の読み取り鍵が無いため同期しない", file=sys.stderr)
        return result
    # サーバーイベント通知（S-11）は flush_outbox() を経由しないため、ntfy
    # 資格情報はここで先に注入しておく。注入が無いと send_skill_sync_event()
    # が NTFY_TOPIC を取得できず、サーバーイベントの通知が必ず失敗して
    # ACK も進まない（flush_outbox() 内の既存呼び出しは冪等なので残す）。
    _ensure_ntfy_env()
    verify_keys = load_verify_keys()
    denylist = load_denylist(base=base)
    sync_state = load_sync_state(base=base)
    # 手順3: 一覧取得
    try:
        listing = skill_sync.list_all_skills(base_url=cfg.base_url, read_key=cfg.read_key)
    except LaneCApiError as exc:
        result["list_failed"] = True
        print(f"[hh_skill_sync] WARN: Lane C 一覧取得に失敗した（次回再試行）: {type(exc).__name__}: {exc}", file=sys.stderr)
        return result
    skills = listing.get("skills") or []
    events = listing.get("events") or []
    # サーバーイベント（S-11）: 通知に成功したものだけ ACK する
    events_acked: list[str] = []
    if not dry_run:
        write_key = promote._load_signing_env().get(promote.WRITE_KEY_VAR)
        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id") or event.get("id")
            if not isinstance(event_id, str) or not event_id:
                continue
            name = event.get("name") if isinstance(event.get("name"), str) else None
            ev_type = event.get("type") if isinstance(event.get("type"), str) else "server"
            notify_event = {
                "event": "skill_sync_server_event",
                "reason": f"server event type={ev_type}",
            }
            if name:
                notify_event["name"] = name
            outcome = _send_notification(notify_event)
            if outcome == "sent":
                events_acked.append(event_id)
        result["events_seen"] = len(events)
        if events_acked:
            if not write_key:
                print(
                    "[hh_skill_sync] WARN: サーバーイベントを通知したが書き込み鍵が無いため ACK しない（次回再通知）",
                    file=sys.stderr,
                )
            else:
                try:
                    skill_sync.ack_events(events_acked, base_url=cfg.base_url, write_key=write_key)
                    result["events_acked"] = len(events_acked)
                except LaneCApiError as exc:
                    print(
                        f"[hh_skill_sync] WARN: events ACK に失敗した（次回再通知）: {type(exc).__name__}",
                        file=sys.stderr,
                    )
    # 手順4〜5: denylist 除外 → フェーズ A → フェーズ B 分岐
    hermes_skills_root = promote._hermes_skills_root()
    pending: list[dict] = []
    for entry in skills:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        remote_sha = entry.get("content_sha256")
        if isinstance(remote_sha, str):
            result["remote_sha256"][name] = remote_sha
        # list エントリの型・必須フィールドを手動検査して RemoteSkillState を
        # 組み立てる（欠落・型不正は整合性異常扱い — ローカルへ書かない）
        rev = entry.get("revision")
        seq = entry.get("promotion_seq")
        origin = entry.get("origin_instance")
        if (
            not isinstance(rev, int) or isinstance(rev, bool) or rev < 0
            or not isinstance(seq, int) or isinstance(seq, bool) or seq < 0
        ):
            _queue_client_event(
                name, "integrity",
                f"list エントリの revision/promotion_seq が非負整数でない: {rev!r}/{seq!r}",
                base=base, pending=pending, result=result,
            )
            continue
        if not isinstance(remote_sha, str) or not isinstance(origin, str):
            _queue_client_event(
                name, "integrity",
                "list エントリの content_sha256/origin_instance が文字列でない",
                base=base, pending=pending, result=result,
            )
            continue
        watermarks = entry.get("origin_seq_watermarks")
        if not isinstance(watermarks, dict):
            watermarks = None
        remote_state = RemoteSkillState(
            name=name,
            revision=rev,
            content_sha256=remote_sha,
            origin_instance=origin,
            promotion_seq=seq,
            origin_seq_watermarks=watermarks,
        )
        # denylist（S-12）: name または digest が載っていれば除外（通知もしない）
        if is_denied(name, remote_sha, denylist):
            result["denied"].append(name)
            continue
        result["observed"].append(name)
        # ローカル側の状態
        local_path = hermes_skills_root / name / "SKILL.md"
        if local_path.is_file():
            try:
                local_sha = hashlib.sha256(local_path.read_bytes()).hexdigest()
                local = LocalSkillState(exists=True, content_sha256=local_sha)
            except OSError:
                local = LocalSkillState(exists=True, content_sha256=None)
        else:
            local = LocalSkillState(exists=False)
            local_sha = None
        # フェーズ A → フェーズ B
        try:
            action = skill_sync.classify_sync_action(name, local, remote_state, sync_state.get(name))
        except IntegrityAnomalyError as exc:
            _queue_client_event(name, "integrity", exc.reason, base=base, pending=pending, result=result)
            continue
        if action == "noop":
            result["noop"].append(name)
        elif action == "metadata_repair":
            _do_metadata_repair(
                name, cfg, verify_keys, dry_run=dry_run, base=base,
                pending=pending, result=result, sync_state=sync_state,
            )
        elif action == "pull":
            if not pull:
                result["pull_deferred"].append(name)
                continue
            _do_pull(
                name, cfg, verify_keys, dry_run=dry_run, base=base,
                pending=pending, result=result, sync_state=sync_state,
            )
        elif action == "push":
            if not reconcile:
                result["push_deferred"].append(name)
                continue
            _do_push(
                name, local_sha, sync_state.get(name), cfg, base=base,
                result=result, hermes_skills_root=hermes_skills_root,
            )
        elif action == "conflict":
            _do_conflict(
                name, local_sha, remote_state, cfg, verify_keys, dry_run=dry_run,
                base=base, pending=pending, result=result, sync_state=sync_state,
            )
    # 手順6: アウトボックス送信（送信成功したものだけ削除）
    flush_results = flush_outbox(base=base)
    result["notifications"] = {
        "attempted": flush_results.get("attempted", 0),
        "sent": flush_results.get("sent", 0),
        "failed": flush_results.get("failed", 0),
    }
    # クライアント判定イベントの監査行に通知結果（notify_state）を反映
    _write_pending_audits(pending, flush_results.get("results", {}), base=base)
    return result


# ---------------------------------------------------------------------------
# CLI（Windows スケジュールタスク: hh_skill_sync.py --pull --reconcile）
# ---------------------------------------------------------------------------


def _print_summary(result: dict) -> None:
    """CLI 用の集計表示（主要カウントを 1 行に）。"""
    keys = (
        "pulled", "noop", "metadata_repair", "conflicts", "pushed",
        "integrity_anomalies", "validation_failures", "skipped_locked",
        "skipped_no_valid_receipt", "denied",
    )
    counts = " ".join(f"{k}={len(result.get(k, []))}" for k in keys)
    notifications = result.get("notifications") or {}
    print(
        f"[hh_skill_sync] {counts} "
        f"events={result.get('events_seen', 0)}/{result.get('events_acked', 0)} "
        f"notifications={notifications.get('attempted', 0)}/{notifications.get('sent', 0)}/{notifications.get('failed', 0)}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI エントリポイント: `python scripts/hh_skill_sync.py [--pull] [--reconcile] [--dry-run]`

    `--forget` は v1 では作らない（denylist の編集は手動。S-12）。
    """
    parser = argparse.ArgumentParser(prog="hh_skill_sync")
    parser.add_argument("--pull", action="store_true", help="pull（リモート→ローカル）を実行する")
    parser.add_argument(
        "--reconcile", action="store_true",
        help="reconcile push（ローカル→リモート。来歴確認済みのもののみ）を実行する",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="分類・集計だけを行い、書き込み・push・通知は一切行わない",
    )
    args = parser.parse_args(argv)
    try:
        result = run_sync(pull=args.pull, reconcile=args.reconcile, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — CLI 境界。例外はログに出して次回再試行
        print(f"[hh_skill_sync] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
