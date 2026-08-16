"""modal_hub/services/ntfy_client.py — store 非依存の ntfy.sh 送信クライアント。

設計上の位置づけ:
    - 親設計書 docs/hh-agent/03_Architecture.md §14（S-11: スキル同期の
      衝突通知・store 非依存の切り出し要件）
    - 実装契約   docs/hh-agent/03_Architecture.md S-11
      「`_send_with_retries()` 相当の HTTP 送信部を
      `modal_hub/services/ntfy_client.py`（store 非依存・新規）へ切り出し。
      追加する公開関数は `send_skill_conflict(event) -> str` 1 つ」

== なぜこのファイルが存在するか ==

`modal_hub/services/notifier.py`（承認通知）はモジュール読み込み時に
`modal_hub.core.store` を import し、`store.py` は**モジュールトップで
`modal` を import** する（store.py 38 行目）。Windows のスケジュールタスク
から動く `hh_skill_sync.py` を `modal` パッケージに依存させたくないため、
ntfy の HTTP 送信部を store 非依存のこのファイルへ切り出す。
notifier.py 側のリファクタ（この関数への委譲）は第 2 弾の別担当が行う。

**必須要件: このファイルは `modal` パッケージを一切 import しない。**
`modal_hub.core.store` も import しない。`modal_hub.core.config` は os
しか import しないため安全で、env 変数名の定数だけを再利用する。

== 既存 notifier.py との挙動の一致 ==

`send_via_ntfy()` は notifier.py の `_send_with_retries()` のロジック
（最大 3 回・指数バックオフ＋ジッタ・2xx/3xx を成功とみなす・
`NTFY_TOKEN` 省略時の Authorization ヘッダ非付与・非 ASCII 資格情報拒否）を
そのまま踏襲する。承認通知（send_approval_request）の外部挙動は
一切変更しない（notifier.py は触らない）。

== 通知本文の構造（S-11） ==

`send_skill_conflict()` の本文は
`{"event":"skill_conflict","name":"<name>","winner":"<origin_instance>",
"winner_sha8":"...","loser_sha8":"..."}` まで。**SKILL.md 本文・差分を
絶対に載せない**（ntfy 運営者・通知ログ・ロック画面に露出する経路を
作らない。§5.2「通知には権限を一切載せない」と同じ構造的担保）。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Final, Optional, Sequence

import httpx

from ..core import config

logger = logging.getLogger("hh_agent.ntfy_client")

# ---------------------------------------------------------------------------
# Constants（notifier.py と同一値。承認通知のリトライ挙動を変えない）
# ---------------------------------------------------------------------------

NTFY_BASE_URL: Final = "https://ntfy.sh"

#: 送信は最大 3 回・指数バックオフ（notifier.py の
#: MAX_ATTEMPTS / INITIAL_BACKOFF_SECONDS / MAX_BACKOFF_SECONDS と同値）。
MAX_ATTEMPTS: Final = 3
INITIAL_BACKOFF_SECONDS: Final = 0.5
MAX_BACKOFF_SECONDS: Final = 4.0
HTTP_TIMEOUT_SECONDS: Final = 10.0

NTFY_PRIORITY: Final = "default"

#: 衝突通知のタイトル。**ASCII 固定**。notifier.py の NOTIFICATION_TITLE が
#: 2026-08-11 に非 ASCII で UnicodeEncodeError を起こした教訓（httpx は
#: HTTP ヘッダ値を ascii でエンコードする）に従う。
CONFLICT_TITLE: Final = "HH-Agent: Skill conflict"
CONFLICT_TAGS: Final = ("warning",)

#: 衝突以外のスキル同期イベント（署名検証失敗・整合性異常・サーバーイベント）
#: のタイトル。`CONFLICT_TITLE` と同じ理由で **ASCII 固定**。
SYNC_EVENT_TITLE: Final = "HH-Agent: Skill sync alert"
SYNC_EVENT_TAGS: Final = ("warning",)

#: `send_skill_sync_event()` の本文に載せる reason の長さ上限（切り詰めてから
#: 載せる。本文の肥大化防止 + SKILL.md 本文が万一 reason に混入しても
#: この切り詰めとフィールド・ホワイトリストの二重防御で漏れない）。
SYNC_EVENT_REASON_MAX: Final = 120


def post_ntfy_once(
    url: str, headers: dict, body: bytes, *, timeout: float
) -> "httpx.Response":
    """ntfy.sh へ 1 回だけ POST する（リトライ・バックオフを持たない最小単位）。

    `send_via_ntfy()`（このファイル）と `notifier._send_with_retries()`
    （承認通知。リトライ回数・バックオフ・`attempts` カウントを自前で
    保持する必要があるため送信部だけをここへ委譲する）の両方から呼ばれる
    共有プリミティブ。呼び出し側が `httpx.HTTPError` を捕捉しリトライ判断・
    ステータスコード判定を行う（このヘルパーは 4xx/5xx でも例外にせず
    そのまま `Response` を返す。接続失敗・タイムアウトのみ `httpx.HTTPError`
    系の例外が飛ぶ）。
    """
    with httpx.Client(timeout=timeout) as client:
        return client.post(url, headers=headers, content=body)


def send_via_ntfy(
    topic: str,
    token: Optional[str],
    title: str,
    message: str,
    tags: Optional[Sequence[str]] = None,
    *,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> bool:
    """ntfy.sh へ 1 通送る低レベル関数（notifier.py の `_send_with_retries()` 相当）。

    既存 notifier.py の外部挙動をそのまま踏襲する:

    - 最大 `MAX_ATTEMPTS`（3）回リトライし、指数バックオフ＋ジッタ
      （`backoff * (0.5 + random.random())`、上限 `MAX_BACKOFF_SECONDS`）。
    - 2xx/3xx を成功とみなす。
    - `token` が空/None のときは Authorization ヘッダそのものを付けない。
    - 非 ASCII の `token`/`title`/`tags` は**送信前に弾く**（httpx が
      UnicodeEncodeError を起こすため。notifier.py が Authorization について
      行っている ASCII 検査と同じ考え方を Title/Tags にも適用）。

    送信できた場合 True、諦めた場合 False（例外は投げない）。
    """
    if not isinstance(topic, str) or not topic:
        raise ValueError("topic must be a non-empty string")
    if not isinstance(message, str):
        raise ValueError("message must be a str")
    if not isinstance(title, str) or not title.isascii():
        logger.error("ntfy rejected: Title ヘッダに非 ASCII が含まれるため送信しない")
        return False
    tag_list = list(tags) if tags else []
    if any(not isinstance(t, str) or not t.isascii() for t in tag_list):
        logger.error("ntfy rejected: Tags ヘッダに非 ASCII が含まれるため送信しない")
        return False
    if token is not None and (not isinstance(token, str) or not token.isascii()):
        logger.error("ntfy rejected: Authorization 資格情報に非 ASCII が含まれるため送信しない")
        return False

    url = f"{NTFY_BASE_URL}/{topic}"
    headers = {
        "Title": title,
        "Priority": NTFY_PRIORITY,
        "Tags": ",".join(tag_list),
        "Content-Type": "application/json; charset=utf-8",
    }
    # NTFY_TOKEN 省略時は Authorization ヘッダそのものを付けない
    # （notifier.py と同じルール。空文字の Bearer を送ると ntfy 側の挙動が
    # 未定義になるため）。
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error: Optional[BaseException] = None
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = post_ntfy_once(
                url, headers, message.encode("utf-8"), timeout=timeout
            )
            if 200 <= response.status_code < 400:
                return True
            last_error = httpx.HTTPStatusError(
                f"ntfy returned HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
        except httpx.HTTPError as exc:
            last_error = exc
        logger.warning(
            "ntfy attempt failed: attempt=%d/%d err=%s",
            attempt,
            MAX_ATTEMPTS,
            type(last_error).__name__ if last_error else "unknown",
        )
        if attempt < MAX_ATTEMPTS:
            time.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    logger.error("ntfy failed: attempts=%d (topic=%s)", MAX_ATTEMPTS, topic)
    return False


def _build_conflict_body(event: dict) -> str:
    """S-11 の通知本文を組み立てる。**5 フィールド以外は一切載せない。**

    受け取った event から name / winner / winner_sha8 / loser_sha8 だけを
    選別して組み立てるため、呼び出し側が誤って event に `content` や
    `diff`（SKILL.md 本文・差分）を混ぜても本文には漏れない（構造的担保）。
    """
    required = ("name", "winner", "winner_sha8", "loser_sha8")
    for key in required:
        value = event.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"send_skill_conflict: event の {key!r} が欠落または非文字列")
    payload = {
        "event": "skill_conflict",
        "name": event["name"],
        "winner": event["winner"],
        "winner_sha8": event["winner_sha8"],
        "loser_sha8": event["loser_sha8"],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def send_skill_conflict(event: dict) -> str:
    """スキル衝突（S-10 手順3 の「衝突」判定）の ntfy 通知を 1 通送る。

    Args:
        event: 通知本文に載せる最小情報。
            `{"name": "<name>", "winner": "<origin_instance>",
            "winner_sha8": "...", "loser_sha8": "..."}`。
            この 4 フィールド以外は本文に含まれない（SKILL.md 本文・差分を
            絶対に含めない。S-11）。

    Returns:
        `"sent"` または `"failed"`（notify_state 語彙）。失敗しても
        例外は投げず、同期フローを失敗させない（フェイルオープン。S-11）。

    Notes:
        NTFY_TOPIC / NTFY_TOKEN は環境変数から読む（供給元の解決——
        `.hh-secret.env` の読み込み等——はこの関数の外、呼び出し側の責務）。
        NTFY_TOPIC が取得できない場合は通知を諦めて `"failed"` を返し、
        stderr 経由のログに残す。監査（promote_log.jsonl / outbox）への
        記録は呼び出し側の責務。
    """
    body = _build_conflict_body(event)
    topic = os.environ.get(config.NTFY_TOPIC, "").strip()
    if not topic:
        logger.error(
            "ntfy skill_conflict: NTFY_TOPIC が取得できないため通知を諦める "
            "(name=%s)。監査への記録は呼び出し側の責務",
            event.get("name"),
        )
        return "failed"
    token = os.environ.get(config.NTFY_TOKEN, "").strip() or None
    ok = send_via_ntfy(
        topic,
        token,
        title=CONFLICT_TITLE,
        message=body,
        tags=CONFLICT_TAGS,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    return "sent" if ok else "failed"


def _build_sync_event_body(event: dict) -> str:
    """衝突以外のスキル同期イベントの通知本文を組み立てる。

    **event / name / reason の 3 フィールド以外は一切載せない**（S-11 の
    「5 フィールド以内」）。`_build_conflict_body()` と同じ構造的担保:
    呼び出し側が event に `content` や `diff`（SKILL.md 本文・差分）を
    混ぜても本文には漏れない。reason は `SYNC_EVENT_REASON_MAX` 文字へ
    切り詰める。
    """
    event_type = event.get("event")
    name = event.get("name")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("send_skill_sync_event: event の 'event' が欠落または非文字列")
    if not isinstance(name, str) or not name:
        raise ValueError("send_skill_sync_event: event の 'name' が欠落または非文字列")
    payload = {"event": event_type, "name": name}
    reason = event.get("reason")
    if isinstance(reason, str) and reason:
        payload["reason"] = reason[:SYNC_EVENT_REASON_MAX]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def send_skill_sync_event(event: dict) -> str:
    """衝突以外のスキル同期イベント（署名検証失敗・整合性異常・サーバーイベント）
    の ntfy 通知を 1 通送る。

    `send_skill_conflict()` と同型（store 非依存・modal 未 import・本文
    5 フィールド以内）。`event["event"]` を通知本文の `event` 種別として
    載せ、`"name"` と任意の `"reason"` だけを本文に載せる。

    Args:
        event: `{"event": "<種別>", "name": "<name>", "reason": "<任意>"}`。
            `"event"` / `"name"` は必須（非空文字列でないと ValueError）。
            `"reason"` は省略可（120 文字へ切り詰める）。この 3 フィールド
            以外は本文に含まれない（SKILL.md 本文・差分を絶対に含めない。
            S-11）。

    Returns:
        `"sent"` または `"failed"`（notify_state 語彙）。失敗しても
        例外は投げず、同期フローを失敗させない（フェイルオープン。S-11）。

    Notes:
        NTFY_TOPIC / NTFY_TOKEN は環境変数から読む（供給元の解決——
        `.hh-secret.env` の読み込み等——はこの関数の外、呼び出し側の責務）。
    """
    body = _build_sync_event_body(event)
    topic = os.environ.get(config.NTFY_TOPIC, "").strip()
    if not topic:
        logger.error(
            "ntfy skill_sync_event: NTFY_TOPIC が取得できないため通知を諦める (name=%s)",
            event.get("name"),
        )
        return "failed"
    token = os.environ.get(config.NTFY_TOKEN, "").strip() or None
    ok = send_via_ntfy(
        topic,
        token,
        title=SYNC_EVENT_TITLE,
        message=body,
        tags=SYNC_EVENT_TAGS,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    return "sent" if ok else "failed"
