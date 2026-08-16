"""modal_hub/services/skill_sync.py — Lane C（Hermes スキル同期）クライアント。

設計上の位置づけ:
    - 親設計書 docs/hh-agent/03_Architecture.md §14
      （S-06b: promote receipt / S-08: push ペイロードとサイズ上限 /
      S-09: Lane C サーバー側の契約と鍵の使い分け / S-10: pull 側の
      受信側検証と差分判定 / S-11: 衝突と revision/CAS）
    - 実装契約   docs/hh-agent/03_Architecture.md「新規・変更ファイル」表
      「Lane C の HTTP クライアント（push / list / pull / events-ack のみ。
      削除系の関数は作らない・S-12）・差分判定・受信側検証・promote
      receipt の生成と検証（S-06b）」

== このモジュールの責務 ==

1. **HTTP クライアント**（push / list / pull / events-ack。削除系は作らない。
   S-12）。書き込み系（push / events-ack）は `C2S_SKILL_WRITE_KEY`、
   読み取り系（list / pull）は既存の読み取り専用 Bearer キーを
   Authorization に使う（S-09）。全呼び出しにタイムアウト必須。
   通信方式は stdlib `urllib.request`（既存
   `.hermes/plugins/` 配下の REST クライアント実装と同じ流儀）。
2. **promote receipt の生成・検証**（S-06b）。canonical 表現:
   `hhskill1|<key_id>|<name>|<content_sha256>|<origin_instance>|
   <promoted_at_ms>|<promotion_seq>|<distilled_from_session_id or "">`
   HMAC は `modal_hub/core/security.py` の `_hmac_sha256` /
   `_b64url_encode` を再利用し、手書きしない。
3. **受信側検証**（S-10 手順4）。pull したものを書き込む前に必ず通す。
   1 つでも落ちたら `SyncValidationError`（書き込まない）。
4. **差分判定（分類）**（S-10 手順3）。フェーズ A（整合性異常）→
   フェーズ B（noop / pull / push / conflict / metadata_repair）の 2 段。
   **ローカル時刻とリモート時刻を比較する式を一切書かない**
   （確定事項 I。判定材料はダイジェストとサーバー採番の `revision` のみ）。

== 設計上の注意 ==

- `promotion_seq` の逆行はサーバー側が advisory にしか扱わない前提で、
  クライアント側も独自の「seq が逆行しているから拒否」ロジックを**足さない**
  （それは S-10 の watermark チェックの役目）。
- このモジュールは `~/.hh-agent/skill_sync_state.json` の I/O を持たない
  （呼び出し元の CLI が読み書きする）。分類ロジックだけがここにある。
- `modal` パッケージへの依存は許容（`modal_hub/core/security.py` 経由で
  `store` → `modal` が入る）。ただし `ntfy_client.py`（同一 Lane C の
  通知側）は `modal` を一切 import しないこと（S-11 の必須要件）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Literal, Optional, Union

from ..core import security
from ..core.redact import redact_text
from . import skill_quarantine

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: スキル名の正規表現。`skill_quarantine.py` の NAME_RE と完全同一のものを
#: import して使う（手書き複製で規則が食い違わないように）。
NAME_RE = skill_quarantine.NAME_RE

#: `origin_instance` の形式（S-10 手順4）。`\Z` を使う（`$` は末尾改行の
#: 直前にもマッチする既知の罠。NAME_RE と同じ理由）。
ORIGIN_INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")

#: `distilled_from_session_id` の文字種（S-10 手順4）。`null` は許容。
DISTILLED_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_DISTILLED_SESSION_LEN = 128

#: S-08: `skill_md` は UTF-8 バイト列で 64KB 以下、JSON ボディ全体は
#: 256KB 以下。`ensure_ascii=False` でエンコードした実測バイト数で判定する。
MAX_SKILL_MD_BYTES = 64 * 1024
MAX_BODY_BYTES = 256 * 1024

DEFAULT_TIMEOUT_SECONDS = 10.0

#: S-10 フェーズ A「異常に大きい」の閾値（int64 上限。revision /
#: promotion_seq カウンタとしてあり得ない値）。
MAX_SANE_COUNTER = 2**63 - 1

#: promote receipt の形式（S-06b）。base64url（パディングなし）の 32 バイト
#: HMAC は 43 文字になる。
RECEIPT_PREFIX = "hhskill1"
RECEIPT_RE = re.compile(r"^[0-9a-f]{8}\.[A-Za-z0-9_-]{43}\Z")

# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------


class LaneCApiError(RuntimeError):
    """Lane C サーバーとの通信失敗（タイムアウト・非 JSON 応答・未定義 HTTP ステータス等）。

    CAS 不一致（409）・形式不正（400）は例外にせず `PushResult` で区別する
    ため、この例外は「プロトコルとして解釈できない失敗」にだけ使う。
    """


class IntegrityAnomalyError(RuntimeError):
    """S-10 手順3 フェーズ A: 整合性異常を検出した。

    異常を検出した name は分類フェーズ（フェーズ B）へ進めず、呼び出し元は
    通知・監査してローカルへ書かない（S-11）。
    """

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"integrity anomaly for {name!r}: {reason}")
        self.name = name
        self.reason = reason


class SyncValidationError(RuntimeError):
    """S-10 手順4 の受信側検証に 1 つでも落ちた。

    落ちた内容は**書き込まない**。呼び出し元はスキップ＋監査＋
    （receipt 不一致・整合性異常の場合）ntfy 通知を行う（S-06b・S-11）。
    """


# ---------------------------------------------------------------------------
# promote receipt（S-06b）
# ---------------------------------------------------------------------------


def _is_sha256_hex(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _canonical_receipt_input(
    key_id: str,
    name: str,
    content_sha256: str,
    origin_instance: str,
    promoted_at_ms: int,
    promotion_seq: int,
    distilled_from_session_id: Optional[str],
) -> bytes:
    """S-06b の canonical 表現を UTF-8 バイト列として組み立てる。

    `distilled_from_session_id` は null の場合空文字として署名する
    （canonical 表現の末尾要素が `or ""`）。
    """
    session = "" if distilled_from_session_id is None else distilled_from_session_id
    if not all(
        isinstance(v, str) for v in (key_id, name, content_sha256, origin_instance, session)
    ):
        raise ValueError("receipt canonical input must be strings")
    canonical = (
        f"{RECEIPT_PREFIX}|{key_id}|{name}|{content_sha256}|"
        f"{origin_instance}|{promoted_at_ms}|{promotion_seq}|{session}"
    )
    return canonical.encode("utf-8")


def derive_key_id(signing_key: bytes) -> str:
    """署名鍵の key_id（鍵の sha256 の先頭 8 桁）。S-06b。"""
    if not isinstance(signing_key, bytes) or not signing_key:
        raise ValueError("signing_key must be non-empty bytes")
    return hashlib.sha256(signing_key).hexdigest()[:8]


def is_valid_receipt_format(receipt: str) -> bool:
    r"""receipt 形式チェック。`^[0-9a-f]{8}\.[A-Za-z0-9_-]{43}\Z` に完全一致するか。"""
    return isinstance(receipt, str) and bool(RECEIPT_RE.match(receipt))


def _resolve_content_sha256(
    content_bytes_or_sha256: Union[bytes, str], digest: Optional[str]
) -> str:
    """署名対象の content_sha256 を確定する。

    `digest`（呼び出し側が read_quarantined_skill() 等で確定済みの値）が
    与えられた場合はそれを正とし、`content_bytes_or_sha256` がバイト列の
    場合は実測 sha256 と突き合わせて一致しなければ ValueError
    （間違ったダイジェストに署名するのを防ぐ）。
    """
    if isinstance(content_bytes_or_sha256, bytes):
        computed = hashlib.sha256(content_bytes_or_sha256).hexdigest()
        if digest is None:
            return computed
        if not _is_sha256_hex(digest) or computed != digest:
            raise ValueError("digest does not match content")
        return digest
    if isinstance(content_bytes_or_sha256, str):
        candidate = (
            content_bytes_or_sha256 if _is_sha256_hex(content_bytes_or_sha256)
            else hashlib.sha256(content_bytes_or_sha256.encode("utf-8")).hexdigest()
        )
        if digest is None:
            return candidate
        if not _is_sha256_hex(digest) or candidate != digest:
            raise ValueError("digest does not match content")
        return digest
    raise TypeError("content_bytes_or_sha256 must be bytes or str")


def write_receipt(
    name: str,
    content_bytes_or_sha256: Union[bytes, str],
    digest: Optional[str],
    seq: int,
    promoted_at_ms: int,
    origin_instance: str,
    distilled_from_session_id: Optional[str],
    *,
    signing_key: bytes,
    key_id: str,
) -> str:
    """promote receipt を生成して `<key_id>.<base64url hmac>` を返す（S-06b）。

    Args:
        name: スキル名（kebab-case。呼び出し側で NAME_RE 検証済みであること）。
        content_bytes_or_sha256: 本文の UTF-8 バイト列、または既知の
            content_sha256（64 hex）。
        digest: content_sha256 の正（S-08 の promote 経路では
            read_quarantined_skill() が返す値をそのまま渡す）。None の場合は
            content_bytes_or_sha256 から計算する。
        seq: この origin における promotion_seq（非負整数）。
        promoted_at_ms: 整数ミリ秒。**float は拒否する**（S-06b: float を
            署名対象に含めない）。
        origin_instance: 署名対象の origin。呼び出し側が確定した値を使う
            （S-08b: リモートの応答値をそのまま転記しないこと）。
        distilled_from_session_id: null の場合は空文字として署名する。
        signing_key: HMAC-SHA256 の鍵（非空 bytes）。
        key_id: 鍵の sha256 先頭 8 桁。`derive_key_id(signing_key)` と
            一致しない場合は ValueError（鍵と key_id の取り違えを防ぐ）。

    Notes:
        HMAC は `security._hmac_sha256` / `_b64url_encode` を再利用する
        （手書きしない）。`receipt` の形式は
        `<key_id>.<base64url(hmac_sha256(signing_key, canonical))>`。
    """
    if not isinstance(signing_key, bytes) or not signing_key:
        raise ValueError("signing_key must be non-empty bytes")
    if not isinstance(key_id, str) or not key_id:
        raise ValueError("key_id must be a non-empty string")
    if key_id != derive_key_id(signing_key):
        raise ValueError("key_id does not match sha256(signing_key)[:8]")
    if not isinstance(promoted_at_ms, int) or isinstance(promoted_at_ms, bool):
        raise ValueError(f"promoted_at_ms must be an integer millisecond timestamp, got {promoted_at_ms!r}")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError(f"seq must be a non-negative integer, got {seq!r}")
    if not isinstance(name, str) or not isinstance(origin_instance, str):
        raise ValueError("name and origin_instance must be strings")
    content_sha256 = _resolve_content_sha256(content_bytes_or_sha256, digest)
    canonical = _canonical_receipt_input(
        key_id, name, content_sha256, origin_instance,
        promoted_at_ms, seq, distilled_from_session_id,
    )
    signature = security._b64url_encode(security._hmac_sha256(signing_key, canonical))
    return f"{key_id}.{signature}"


def verify_receipt(
    receipt: str,
    name: str,
    content_sha256: str,
    origin_instance: str,
    promoted_at_ms: int,
    promotion_seq: int,
    distilled_from_session_id: Optional[str],
    *,
    verify_keys: dict,
) -> bool:
    """promote receipt を検証する（S-06b）。S-10 手順4 の最重要チェック。

    Args:
        verify_keys: `{key_id: 鍵 bytes}`。**複数世代の鍵を保持できる**。
            鍵の世代交代後も、旧鍵の key_id を残している限り旧 receipt を
            検証できる。unknown key_id や不正形式は False（失敗）。

    戻り値: 署名が一致した場合のみ True。形式・型・鍵不明のいずれでも
    False を返し、例外は投げない（fail-closed な検証関数）。
    """
    if not is_valid_receipt_format(receipt):
        return False
    if not isinstance(verify_keys, dict):
        return False
    key_id, sig_b64 = receipt.split(".", 1)
    key = verify_keys.get(key_id)
    if key is None:
        return False
    if not isinstance(name, str) or not isinstance(content_sha256, str) or not isinstance(origin_instance, str):
        return False
    if not isinstance(promoted_at_ms, int) or isinstance(promoted_at_ms, bool):
        return False
    if not isinstance(promotion_seq, int) or isinstance(promotion_seq, bool):
        return False
    if distilled_from_session_id is not None and not isinstance(distilled_from_session_id, str):
        return False
    try:
        canonical = _canonical_receipt_input(
            key_id, name, content_sha256, origin_instance,
            promoted_at_ms, promotion_seq, distilled_from_session_id,
        )
        expected = security._hmac_sha256(key, canonical)
        provided = security._b64url_decode(sig_b64)
    except Exception:
        return False
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# HTTP クライアント（push / list / pull / events-ack。削除系は作らない・S-12）
# ---------------------------------------------------------------------------


def _urlopen(req, timeout: float):
    """urllib.request.urlopen の薄いラッパー（テストで差し替える注入点）。"""
    return urllib.request.urlopen(req, timeout=timeout)


def _bearer_headers(key: str) -> dict:
    """Authorization ヘッダを組み立てる。key 未設定なら fail-closed で例外。"""
    if not key:
        raise LaneCApiError("Lane C credential is not configured (refusing to send)")
    return {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _request_json(
    *,
    method: str,
    url: str,
    headers: dict,
    payload: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
):
    """JSON で 1 往復する。`(status, parsed_dict)` を返す。

    - `ensure_ascii=False` で JSON エンコードする（S-08）。
    - 4xx/5xx は urllib の HTTPError として来るためここで捕捉して
      `(status, body)` に揃える。
    - タイムアウト・接続失敗・非 JSON 応答は `LaneCApiError` を投げる。
    """
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except OSError:
            raw = b""
    except (OSError, TimeoutError) as exc:
        raise LaneCApiError(f"Lane C HTTP request failed: {type(exc).__name__}: {exc}") from exc
    if not raw:
        return status, {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LaneCApiError(f"Lane C returned non-JSON body (HTTP {status})") from exc
    if not isinstance(parsed, dict):
        raise LaneCApiError(f"Lane C returned non-object JSON (HTTP {status})")
    return status, parsed


@dataclass(frozen=True)
class PushResult:
    """`push_skill()` の戻り値。HTTP プロトコルの結果を例外ではなく値で区別する。

    - 成功（200）: `sent=True`、`revision` / `received_at` /
      `replaced_content_sha256` が埋まる。
    - CAS 不一致（409）: `conflict=True`、`current_revision` が埋まる
      （例外にしない。呼び出し元は re-pull → 再分類へ）。
    - ローカル拒否: `reason="size_limit"`（64KB/256KB 超過）または
      `reason="redact_diff"`（送信前に redact_text() で差分が出た）で
      **何も送らない**。
    - 400（形式不正）: `reason="rejected"`、`error` にサーバーのエラー情報。
    """

    sent: bool
    reason: Optional[str] = None
    conflict: bool = False
    error: Optional[dict] = None
    revision: Optional[int] = None
    received_at: Optional[str] = None
    replaced_content_sha256: Optional[str] = None
    current_revision: Optional[int] = None


def push_skill(
    name: str,
    skill_md: str,
    content_sha256: str,
    promoted_at_ms: int,
    origin_instance: str,
    distilled_from_session_id: Optional[str],
    promotion_seq: int,
    receipt: str,
    base_revision: int,
    *,
    base_url: str,
    write_key: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> PushResult:
    """Lane C へスキルを push する（POST /api/skills/push。S-08）。

    送信前チェック（S-08）:
    - `redact_text()` を適用して差分が出たら**送らない**
      （`PushResult(sent=False, reason="redact_diff")`。redact 後の本文を
      送るのではなく拒否する）。
    - `skill_md` が UTF-8 バイト列で 64KB 以下、JSON ボディ全体が 256KB
      以下（`ensure_ascii=False` 実測バイト数）。超過はローカルで拒否し
      送らない（`reason="size_limit"`）。

    CAS は `base_revision` で行う: 成功（200）なら新しい `revision` が返り、
    不一致（409）なら `conflict=True` と `current_revision` が返る
    （例外にしない）。
    """
    if not isinstance(skill_md, str):
        raise TypeError("skill_md must be str")
    md_bytes = skill_md.encode("utf-8")
    if len(md_bytes) > MAX_SKILL_MD_BYTES:
        return PushResult(
            sent=False, reason="size_limit",
            error={"detail": f"skill_md is {len(md_bytes)} bytes (max {MAX_SKILL_MD_BYTES})"},
        )
    if redact_text(skill_md) != skill_md:
        return PushResult(sent=False, reason="redact_diff")
    payload = {
        "name": name,
        "skill_md": skill_md,
        "content_sha256": content_sha256,
        "promoted_at_ms": promoted_at_ms,
        "origin_instance": origin_instance,
        "distilled_from_session_id": distilled_from_session_id,
        "promotion_seq": promotion_seq,
        "receipt": receipt,
        "base_revision": base_revision,
    }
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body_bytes) > MAX_BODY_BYTES:
        return PushResult(
            sent=False, reason="size_limit",
            error={"detail": f"JSON body is {len(body_bytes)} bytes (max {MAX_BODY_BYTES})"},
        )
    url = f"{base_url.rstrip('/')}/api/skills/push"
    status, parsed = _request_json(
        method="POST", url=url, headers=_bearer_headers(write_key),
        payload=payload, timeout=timeout,
    )
    if status == 200:
        return PushResult(
            sent=True,
            revision=parsed.get("revision"),
            received_at=parsed.get("received_at"),
            replaced_content_sha256=parsed.get("replaced_content_sha256"),
        )
    if status == 409:
        return PushResult(
            sent=False, conflict=True, error=parsed,
            current_revision=parsed.get("current_revision"),
        )
    if status == 400:
        return PushResult(sent=False, reason="rejected", error=parsed)
    raise LaneCApiError(f"Lane C push failed: HTTP {status} {parsed}")


def list_skills(
    *,
    base_url: str,
    read_key: str,
    cursor: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Lane C のスキル一覧を 1 ページ取得する（GET /api/skills/list）。

    戻り値: `{"skills": [...], "events": [...], "next_cursor": ...}`。
    `next_cursor` が null でなくなるまで読むには `list_all_skills()` を使う。
    """
    params = {}
    if cursor is not None:
        params["cursor"] = cursor
    url = f"{base_url.rstrip('/')}/api/skills/list"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    status, parsed = _request_json(
        method="GET", url=url, headers=_bearer_headers(read_key), timeout=timeout,
    )
    if status != 200:
        raise LaneCApiError(f"Lane C list failed: HTTP {status} {parsed}")
    return parsed


def list_all_skills(
    *,
    base_url: str,
    read_key: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """`next_cursor` が null になるまで全ページ取得し、skills/events を連結する。

    S-10 手順2: events（50 件/ページ）は `next_cursor` が null になるまで
    読み切る。戻り値の `next_cursor` は常に None。
    """
    skills: list = []
    events: list = []
    cursor = None
    while True:
        page = list_skills(base_url=base_url, read_key=read_key, cursor=cursor, timeout=timeout)
        skills.extend(page.get("skills") or [])
        events.extend(page.get("events") or [])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return {"skills": skills, "events": events, "next_cursor": None}


def pull_skill(
    name: str,
    *,
    base_url: str,
    read_key: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Lane C からスキル本文＋meta を取得する（GET /api/skills/pull?name=）。

    戻り値（本文＋meta。S-10 手順4 の `validate_pulled_skill()` に通して
    から書き込むこと）:
    `{"name", "content", "content_sha256", "revision", "receipt",
    "origin_instance", "promoted_at_ms", "promotion_seq",
    "distilled_from_session_id", "received_at"}` ほか。
    """
    url = (
        f"{base_url.rstrip('/')}/api/skills/pull?"
        f"{urllib.parse.urlencode({'name': name})}"
    )
    status, parsed = _request_json(
        method="GET", url=url, headers=_bearer_headers(read_key), timeout=timeout,
    )
    if status != 200:
        raise LaneCApiError(f"Lane C pull failed: HTTP {status} {parsed}")
    return parsed


def ack_events(
    event_ids: list,
    *,
    base_url: str,
    write_key: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """イベントの受領確認を送る（POST /api/skills/events/ack）。

    未 ACK イベントの上限（S-10: 500 件）の超過処理は呼び出し側の CLI が
    行う（この関数は送るだけ）。
    """
    if not isinstance(event_ids, list) or not all(isinstance(i, str) for i in event_ids):
        raise ValueError("event_ids must be a list of str")
    url = f"{base_url.rstrip('/')}/api/skills/events/ack"
    status, parsed = _request_json(
        method="POST", url=url, headers=_bearer_headers(write_key),
        payload={"event_ids": event_ids}, timeout=timeout,
    )
    if status != 200:
        raise LaneCApiError(f"Lane C events ack failed: HTTP {status} {parsed}")


# ---------------------------------------------------------------------------
# 受信側検証（S-10 手順4）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PulledSkill:
    """検証を通過した pull 結果。CLI はこの値だけを使って書き込む。"""

    name: str
    content: str
    content_sha256: str
    revision: int
    receipt: str
    origin_instance: str
    promoted_at_ms: int
    promotion_seq: int
    distilled_from_session_id: Optional[str]
    received_at: Optional[str] = None


def validate_pulled_skill(remote: dict, *, verify_keys: dict) -> PulledSkill:
    """S-10 手順4 の受信側検証。**1 つでも落ちたら `SyncValidationError`**。

    検証項目（順不同。全て必須）:
    1. `name` が NAME_RE に一致
    2. 本文が UTF-8 として妥当・64KB 以下
    3. `skill_quarantine.parse_frontmatter_name()` の結果が `name` と一致
    4. 実測 sha256 が `content_sha256` と一致
    5. promote receipt の検証（`verify_receipt`）。**これが最重要で、
       他が全部通ってもこれに落ちたら書き込まない**
    6. `origin_instance` が `^[a-z0-9][a-z0-9._-]{0,63}$`、
       `distilled_from_session_id` が null または 128 文字以下の
       `^[A-Za-z0-9_-]+$`、`promoted_at_ms` / `promotion_seq` が非負整数
    7. `redact_text()` を適用して差分ゼロ

    戻り値: 検証済みの `PulledSkill`（revision も非負整数として受け取る。
    CLI はこれを watermark に使う）。
    """
    if not isinstance(remote, dict):
        raise SyncValidationError("pull response is not a JSON object")
    name = remote.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise SyncValidationError(f"name が NAME_RE に一致しない: {name!r}")
    content = remote.get("content")
    if content is None:
        content = remote.get("skill_md")
    if not isinstance(content, str):
        raise SyncValidationError("content が文字列でない")
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SyncValidationError("content が UTF-8 としてエンコードできない") from exc
    if len(content_bytes) > MAX_SKILL_MD_BYTES:
        raise SyncValidationError(f"content が {MAX_SKILL_MD_BYTES} バイトを超える")
    fm_name = skill_quarantine.parse_frontmatter_name(content)
    if fm_name != name:
        raise SyncValidationError(
            f"frontmatter name ({fm_name!r}) が name ({name!r}) と一致しない"
        )
    content_sha256 = remote.get("content_sha256")
    if not isinstance(content_sha256, str):
        raise SyncValidationError("content_sha256 が文字列でない")
    actual = hashlib.sha256(content_bytes).hexdigest()
    if not hmac.compare_digest(actual, content_sha256):
        raise SyncValidationError("実測 sha256 が content_sha256 と一致しない")
    receipt = remote.get("receipt")
    if not isinstance(receipt, str):
        raise SyncValidationError("receipt が文字列でない")
    origin_instance = remote.get("origin_instance")
    promoted_at_ms = remote.get("promoted_at_ms")
    promotion_seq = remote.get("promotion_seq")
    distilled_from_session_id = remote.get("distilled_from_session_id")
    # 最重要チェック。他が全部通ってもここに落ちたら書き込まない。
    if not verify_receipt(
        receipt, name, content_sha256, origin_instance,
        promoted_at_ms, promotion_seq, distilled_from_session_id,
        verify_keys=verify_keys,
    ):
        raise SyncValidationError("promote receipt の検証に失敗（最重要チェック）")
    if not isinstance(origin_instance, str) or not ORIGIN_INSTANCE_RE.match(origin_instance):
        raise SyncValidationError(f"origin_instance が形式に一致しない: {origin_instance!r}")
    if distilled_from_session_id is not None:
        if (
            not isinstance(distilled_from_session_id, str)
            or len(distilled_from_session_id) > MAX_DISTILLED_SESSION_LEN
            or not DISTILLED_SESSION_RE.match(distilled_from_session_id)
        ):
            raise SyncValidationError(
                f"distilled_from_session_id が形式に一致しない: {distilled_from_session_id!r}"
            )
    if not _is_non_negative_int(promoted_at_ms) or not _is_non_negative_int(promotion_seq):
        raise SyncValidationError(
            f"promoted_at_ms/promotion_seq が非負整数でない: "
            f"{promoted_at_ms!r}/{promotion_seq!r}"
        )
    if redact_text(content) != content:
        raise SyncValidationError("redact_text() 適用で差分が出た（秘密の混入疑い）")
    revision = remote.get("revision")
    if not _is_sane_counter(revision):
        raise SyncValidationError(f"revision が異常: {revision!r}")
    received_at = remote.get("received_at")
    if not isinstance(received_at, str):
        received_at = None  # 監査・表示専用フィールド。型不正は None に落とす
    return PulledSkill(
        name=name,
        content=content,
        content_sha256=content_sha256,
        revision=revision,
        receipt=receipt,
        origin_instance=origin_instance,
        promoted_at_ms=promoted_at_ms,
        promotion_seq=promotion_seq,
        distilled_from_session_id=distilled_from_session_id,
        received_at=received_at,
    )


# ---------------------------------------------------------------------------
# 差分判定（S-10 手順3。フェーズ A 整合性検証 → フェーズ B 分類）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalSkillState:
    """ローカルディスク上の SKILL.md の状態（分類入力）。"""

    exists: bool
    content_sha256: Optional[str] = None


@dataclass(frozen=True)
class RemoteSkillState:
    """Lane C list 応答の 1 エントリ（分類入力）。CLI が list 応答から組み立てる。"""

    name: str
    revision: int
    content_sha256: str
    origin_instance: str
    promotion_seq: int
    origin_seq_watermarks: Optional[dict] = None


def _is_non_negative_int(value) -> bool:
    """非負整数か（bool は int なので明示的に拒否）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_sane_counter(value) -> bool:
    """revision/promotion_seq の型・範囲検査（S-10 フェーズ A）。"""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_SANE_COUNTER
    )


def _normalize_state(state):
    """`~/.hh-agent/skill_sync_state.json` の当該 name のエントリを
    `(lane_c_revision, content_sha256)` に正規化する。

    壊れている（dict でない・必須キー欠落・型不正）場合は `(None, None)` —
    「初回」扱い（安全側。S-10 の「状態ファイルのスキーマが壊れている場合は
    全エントリを破棄し、全 name を初回として扱う」と同じ方針を name 単位に
    適用したもの）。
    """
    if not isinstance(state, dict):
        return None, None
    rev = state.get("lane_c_revision")
    sha = state.get("content_sha256")
    if not _is_sane_counter(rev) or not isinstance(sha, str):
        return None, None
    return rev, sha


def check_integrity(remote: RemoteSkillState, state: Optional[dict]) -> Optional[str]:
    """S-10 手順3 フェーズ A: 整合性異常を検査する。

    異常が 1 つでも見つかったら理由文字列を返し、無ければ None を返す。
    異常を見つけた name は分類フェーズ（フェーズ B）へ進めず「整合性異常」
    として扱う（`classify_sync_action()` は `IntegrityAnomalyError` を投げる）。

    検査項目:
    - `revision` / `promotion_seq` が整数でない・負数・bool・異常に大きい
      （`MAX_SANE_COUNTER` = 2^63-1 超）
    - `remote.revision < state.lane_c_revision`（巻き戻り）
    - `remote.revision == state.lane_c_revision` かつ
      `remote.sha != state.content_sha256`（CAS 不変条件の破れ）
    - `remote.promotion_seq < origin_seq_watermarks[remote.origin_instance]`
      （watermark との矛盾）

    Notes:
        `promotion_seq` の「逆行」をここで直接拒否するのではなく、あくまで
        watermark との比較で検出する（タスク指示: クライアント側に独自の
        seq 逆行拒否ロジックを足さない）。
    """
    if not _is_sane_counter(remote.revision):
        return (
            f"revision が異常（非負整数・2^63-1 以下であるべき）: {remote.revision!r}"
        )
    if not _is_sane_counter(remote.promotion_seq):
        return f"promotion_seq が異常（非負整数・2^63-1 以下であるべき）: {remote.promotion_seq!r}"
    state_rev, state_sha = _normalize_state(state)
    if state_rev is not None:
        if remote.revision < state_rev:
            return f"revision が巻き戻っている: remote={remote.revision} < state={state_rev}"
        if remote.revision == state_rev and remote.content_sha256 != state_sha:
            return "CAS 不変条件の破れ: 同一 revision で内容が異なる"
    watermarks = remote.origin_seq_watermarks
    if isinstance(watermarks, dict):
        wm = watermarks.get(remote.origin_instance)
        if _is_sane_counter(wm) and remote.promotion_seq < wm:
            return (
                f"watermark との矛盾: promotion_seq={remote.promotion_seq} < "
                f"origin_seq_watermarks[{remote.origin_instance!r}]={wm}"
            )
    return None


def classify_sync_action(
    name: str,
    local: LocalSkillState,
    remote: Optional[RemoteSkillState],
    state: Optional[dict],
) -> Literal["noop", "pull", "push", "conflict", "metadata_repair"]:
    """S-10 手順3 の分類。**判定材料はダイジェストと `revision` のみ**。

    **ローカル時刻とリモート時刻を比較する式を一切書かない**
    （確定事項 I。`received_at` / `promoted_at_ms` は判定に使わない）。

    フェーズ A（`check_integrity`）に落ちた場合は `IntegrityAnomalyError` を
    送出し、分類フェーズ（フェーズ B）へ進まない。呼び出し元はこれを捕捉して
    通知・監査し、ローカルへ書かない。

    フェーズ B の表（設計書 1225〜1234 行目）:
    | 状況 | 判定 |
    |---|---|
    | local.sha == remote.sha（revision は同期済み） | noop |
    | local.sha == remote.sha かつ revision だけ進んだ（/ 状態欠損） | metadata_repair |
    | リモートにのみ存在 | pull |
    | ローカルにのみ存在（base_revision=0。来歴条件は呼び出し側が確認） | push |
    | sha 不一致・ローカルは最後の同期から不変（state.content_sha256 == local.sha） | pull |
    | sha 不一致・ローカルが再 promote 済み・remote.revision == state.lane_c_revision | push（base_revision=state.lane_c_revision） |
    | sha 不一致・双方が進んだ | conflict |
    | 状態なし（初回・欠損・破損）・sha 不一致 | conflict（安全側。推測で消さない） |

    `push` 判定時、呼び出し元は promote 来歴条件（promote_receipts/current
    が存在し local の digest と一致すること）を確認してから push する
    （S-10 の注記。base_revision の値は上表のとおり）。
    """
    if not isinstance(local, LocalSkillState):
        raise TypeError("local must be LocalSkillState")
    if remote is not None and not isinstance(remote, RemoteSkillState):
        raise TypeError("remote must be RemoteSkillState or None")
    if remote is not None:
        anomaly = check_integrity(remote, state)
        if anomaly is not None:
            raise IntegrityAnomalyError(name=name, reason=anomaly)

    # --- フェーズ B ---
    if local.exists and local.content_sha256 is None:
        return "conflict"  # ローカルは存在するがダイジェスト不明 → 安全側
    local_sha = local.content_sha256 if local.exists else None
    state_rev, state_sha = _normalize_state(state)

    if remote is None:
        return "push" if local_sha is not None else "noop"
    if local_sha is None:
        return "pull"
    if local_sha == remote.content_sha256:
        # 内容は同じ。revision だけ進んだ（または状態欠損）なら
        # メタデータの自己修復＋ウォーターマークの更新が必要。
        if state_rev is None or remote.revision > state_rev:
            return "metadata_repair"
        return "noop"
    # ここから先は sha 不一致。
    if state_sha == local_sha:
        return "pull"  # ローカルは最後の同期から変わっていない → リモートだけ進んだ
    if state_sha is None:
        return "conflict"  # 初回・状態ファイル欠損・破損 → 安全側
    if remote.revision == state_rev:
        return "push"  # ローカルだけが進んだ（base_revision=state.lane_c_revision）
    return "conflict"  # 双方が進んだ


__all__ = [
    "NAME_RE",
    "ORIGIN_INSTANCE_RE",
    "MAX_SKILL_MD_BYTES",
    "MAX_BODY_BYTES",
    "RECEIPT_RE",
    "RECEIPT_PREFIX",
    "LaneCApiError",
    "IntegrityAnomalyError",
    "SyncValidationError",
    "PushResult",
    "LocalSkillState",
    "RemoteSkillState",
    "PulledSkill",
    "derive_key_id",
    "is_valid_receipt_format",
    "write_receipt",
    "verify_receipt",
    "push_skill",
    "list_skills",
    "list_all_skills",
    "pull_skill",
    "ack_events",
    "validate_pulled_skill",
    "check_integrity",
    "classify_sync_action",
]
