"""modal_hub/services/notifier.py — ntfy.sh 承認要求通知の送信。

設計上の位置づけ:
    - 親設計書 docs/hh-agent/03_Architecture.md §5.2（通知経路には権限を載せない）
    - 実装契約   docs/hh-agent/05_Phase1a_Spec.md §1.2 step 7（ntfy 送信と
                   ``notify:<id>`` 更新）、§4.3（通知の送達保証）、
                   §10.3（通知本文には redaction を適用しない＝そもそも
                   機微情報を載せない構造的担保）

このモジュールの責務:
    - 承認要求発生時に ntfy.sh へ通知を 1 通送る
    - 送信結果を ``notify:<approval_id>`` に書き込む（write-once）
    - 既存再利用パス（idempotent retry）で ``notify:`` が ``sent`` でない
      場合に限り再送を試みる
    - 最大 3 回・指数バックオフで ntfy POST を試みる（05 §4.3）
    - ``ntfy_topic`` / ``ntfy_token`` を Modal Secret から読む（config 経由）。
      ``ntfy_token`` は任意（2026-08-11 決定: 公開トピック運用では空。
      その場合 Authorization ヘッダそのものを付けない）

このモジュールの責務外:
    - payload の redaction（通知本文にはそもそも payload を載せない。
      03 §5.2「ntfy には権限を一切載せない」の構造的担保）
    - 監査ボリューム書き出し（``notify:<id>`` の state 更新のみ担当。
      監査イベント ``notified`` / ``notify_failed`` の永続化は
      ``services/audit.py`` の所有 — このモジュールから import しない）
    - 承認リクエストの状態管理（``routers/approval_gate.py``）
    - modal.Dict の raw 操作（``core/store.py`` 経由のみ）

== 通知本文の構造（03 §5.2 + 05 §1.2 step 7） ==

本文に乗せるのは opaque な ``approval_id`` とリスクレベルの 2 項目のみ。
コマンド本文・パス・payload・差分・理由・理由は **絶対に載せない**。
これらは認証後の PWA でのみ表示する。ntfy サーバ・通知ログ・スマホ
ロック画面のすべてに payload が露出する経路は作らない。

== write-once 制約（store.py の制約に由来） ==

``store.put_if_absent`` しか存在しない（modal.Dict には update が無い）
ため ``notify:<id>`` は **最初に書き込まれた値で固定**される。

- 同一 ``approval_id`` に対する初回の送信結果が ``failed`` のまま固まる
  可能性がある。これは仕様で受容されている挙動:
  ``notify_state == "failed"`` を poll で受け取ったエージェントは
  即 deny する（05 §1.3・§4.3）。
- HTTP レベルの再送（idempotent retry）で「前回 failed → 今回 sent」に
  なる状況でも、``notify:<id>`` のレコード自体は上書きされない。
  poll 側は依然 ``failed`` を返す。これにより「失敗が確定したら
  必ず deny される」という強い保証が成立する。
- ``attempts`` フィールドは **同一 request 内の試行回数** を意味する。
  リクエストをまたぐ累積値ではない（write-once なので累積できない）。

== シークレットの扱い ==

``NTFY_TOPIC`` / ``NTFY_TOKEN`` はログ・traceback・``__repr__`` に出さない。
``config.safe_repr`` でマスクした形を使うか、touch 自体を避ける。
例外メッセージに URL を入れる際も topic 部分は伏せる（``topic=<masked>``）。

== 失敗時の挙動 ==

- ntfy POST がネットワーク失敗・タイムアウト・4xx/5xx を返した場合:
  残りの試行回数内で指数バックオフ + ジッタで再試行。
- 規定回数すべて失敗 → ``state = "failed"`` を ``notify:<id>`` に書き、
  ``send_approval_request`` の戻り値も ``"failed"``。
- ``SecretMissingError``（config 由来）は ``NotifyError`` に変換せず
  そのまま上位へ伝播させる（デプロイ／Secret 設定不良は 500 系として
  routers に渡すのが正）。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Final, Optional

import httpx

from ..core import config, store

logger = logging.getLogger("hh_agent.notifier")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NTFY_BASE_URL: Final = "https://ntfy.sh"

# 05 §4.3 通知の送達保証: 送信は最大 3 回・指数バックオフ。
# 05 §1.1 リトライ方針（共通規約）: 初回 0.5 秒・以後 2 倍・上限 4 秒。
MAX_ATTEMPTS: Final = 3
INITIAL_BACKOFF_SECONDS: Final = 0.5
MAX_BACKOFF_SECONDS: Final = 4.0
HTTP_TIMEOUT_SECONDS: Final = 10.0

# ntfy の priority は 1=min..5=max。HIGH リスクのみ通知するため
# default（3）で十分。意図的に上げる／下げる要件は無いため固定値。
NTFY_PRIORITY: Final = "default"

# 通知タイトル。スマホでロック画面／通知一覧に表示される文字列。
# 機微情報を含めず、ユーザが「これは承認要求だ」と一目で分かる文言に固定。
#
# **ASCII 固定にする（2026-08-11 障害修正）**: httpx は HTTP ヘッダ値を
# ascii でエンコードするため、CJK 文字（例: "承認待ち" の
# 承 U+6279 / 認 U+8A8D / 待 U+5F85）を含めると ``client.post`` の中で
# ``UnicodeEncodeError: 'ascii' codec can't encode character ...`` が
# 送出される。``UnicodeEncodeError`` は ``httpx.HTTPError`` のサブクラスではない
# ため、このファイルの ``except httpx.HTTPError`` では捕捉できず
# ``send_approval_request`` まで素のまま伝播する。結果として **全承認要求が
# notifier の例外で落ちる** という 03 §5.2 / 05 §1.2 step 7 の fail-closed
# 約束（戻り値は ``"sent"`` か ``"failed"`` のみ）を破る。
# タイトルを ASCII に固定することでこの抜け道を塞ぐ。本文側は JSON として
# UTF-8 で送られるので PWA 表示は問題ない（既に承認画面は各言語で
# ローカライズ済み）。
NOTIFICATION_TITLE: Final = "HH-Agent: Awaiting approval"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NotifyError(RuntimeError):
    """通知サブシステムに回復不能な障害が発生したことを示す。

    ネットワーク起因の一過性失敗（HTTP エラー・タイムアウト）は
    リトライで吸収し最終的に ``state="failed"`` を返す経路で扱うため、
    この例外は「Secret 不在・設定エラー」など **即時失敗** にだけ使う。
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send_approval_request(approval_id: str, risk: str) -> str:
    """承認要求発生時に ntfy 通知を 1 通送る。

    戻り値は ``notify_state``。``"sent"`` / ``"failed"`` のいずれかを返す。
    05 §1.2 step 7 + §1.3 と同一語彙。``"pending"`` はこの関数からは
    返さない（指定試行回数内で同期的に決着させるため）。poll 応答で
    ``"pending"`` を返す経路は routers/approval_gate.py 側の責務。

    **冪等性**: 既に ``notify:<id>`` が存在し ``state == "sent"`` であれば
    no-op で ``"sent"`` を返す。それ以外（未存在 / ``state != "sent"``）
    では最大 ``MAX_ATTEMPTS`` 回のリトライ付きで送信を試み、最終状態を
    ``put_if_absent`` で書き込む。

    **引数**:

    - ``approval_id``: 不変な UUID4。opaque な識別子としてのみ本文に載る。
    - ``risk``: ``"HIGH"`` / ``"MEDIUM"`` / ``"LOW"`` のいずれか。本実装は
      HIGH のみが呼び出される想定（MEDIUM は同期 fire-and-forget は routers
      側の判断 — 05 §4.4）。リスク値自体は本文に載せるだけで分岐には使わな
      い（ntfy priority は固定）。

    **失敗時の契約**: ``state="failed"`` を返した時点で、呼び出し側は
    poll 応答に ``notify_state="failed"`` を載せてエージェントへ返す。
    エージェントは即 deny する（05 §1.3）。
    """
    key = store.notify_key(approval_id)
    existing = store.get(key)
    if isinstance(existing, dict) and existing.get("state") == "sent":
        return "sent"

    body = _build_body(approval_id, risk)
    attempts_used, state = _send_with_retries(approval_id, body)
    _record_notify_state(approval_id, state, attempts_used)
    return state


# ---------------------------------------------------------------------------
# Body construction
# ---------------------------------------------------------------------------


def _build_body(approval_id: str, risk: str) -> str:
    """通知本文を組み立てる。

    03 §5.2: 本文は opaque な approval_id とリスクレベルのみ。
    コマンド本文・パス・差分・payload・理由は **絶対に載せない**。

    JSON 1 行として返す（ntfy 本文は text / json のどちらも受理する）。
    PWA 側での構造化は ntfy 本文に依存せず Hub API 経由で行うため、
    ntfy 本文は「ロック画面で 1 行見える」用途に最適化した形にする。
    """
    payload = {"approval_id": approval_id, "risk": risk}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# HTTP send + retry
# ---------------------------------------------------------------------------


def _send_with_retries(
    approval_id: str, body: str
) -> tuple[int, str]:
    """ntfy.sh へ POST を ``MAX_ATTEMPTS`` 回試みる。

    戻り値: ``(attempts_used, state)``。``state`` は ``"sent"`` か
    ``"failed"``。各試行で HTTP ステータス 2xx/3xx を成功とみなす
    （4xx/5xx も例外にせず retry する — ntfy は 5xx を一時障害として
    返すことがあるため。403/404 のような恒久的失敗は規定回数試行後に
    ``failed`` に落ちる）。

    失敗時のバックオフは指数 + ジッタ（05 §1.1 共通規約と整合: 初回
    0.5 秒・以後 2 倍・上限 4 秒）。

    **シークレット取り扱い**: ``topic`` / ``token`` はローカル変数にだけ
    置き、ログ・例外メッセージ・戻り値には載せない。
    """
    topic = config.ntfy_topic()
    token = config.ntfy_token()
    url = f"{NTFY_BASE_URL}/{topic}"

    headers = {
        "Title": NOTIFICATION_TITLE,
        "Priority": NTFY_PRIORITY,
        "Tags": "bell",
        "Content-Type": "application/json; charset=utf-8",
    }
    # NTFY_TOKEN is optional (2026-08-11 決定): 公開トピック運用では None の
    # まま。Authorization ヘッダそのものを省く -- 空文字の Bearer トークンを
    # 送るとntfy側の挙動が未定義になりうるうえ、03_Architecture.md §5.2
    # 「ntfy には権限を一切載せない」の意図にも反する。
    if token:
        if not token.isascii():
            logger.error(
                "ntfy failed: approval_id=%s reason=Authorization credential "
                "rejected as non-ASCII",
                approval_id,
            )
            return MAX_ATTEMPTS, "failed"
        headers["Authorization"] = f"Bearer {token}"

    last_error: Optional[BaseException] = None
    backoff = INITIAL_BACKOFF_SECONDS

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = client.post(
                    url,
                    headers=headers,
                    content=body.encode("utf-8"),
                )
            if 200 <= response.status_code < 400:
                logger.info(
                    "ntfy sent: approval_id=%s attempt=%d status=%d",
                    approval_id, attempt, response.status_code,
                )
                return attempt, "sent"
            last_error = httpx.HTTPStatusError(
                f"ntfy HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
        except httpx.HTTPError as exc:
            last_error = exc

        # Log every failure for debuggability, then sleep with jitter.
        logger.warning(
            "ntfy attempt failed: approval_id=%s attempt=%d/%d err=%s",
            approval_id, attempt, MAX_ATTEMPTS,
            type(last_error).__name__ if last_error else "unknown",
        )
        if attempt < MAX_ATTEMPTS:
            sleep_for = backoff * (0.5 + random.random())
            time.sleep(sleep_for)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    logger.error(
        "ntfy failed: approval_id=%s attempts=%d topic=%s",
        approval_id, MAX_ATTEMPTS, config.safe_repr(topic),
    )
    return MAX_ATTEMPTS, "failed"


# ---------------------------------------------------------------------------
# State recording (write-once via put_if_absent)
# ---------------------------------------------------------------------------


def _record_notify_state(
    approval_id: str, state: str, attempts: int
) -> None:
    """``notify:<approval_id>`` に送信結果を書き込む。

    write-once: 既に値があれば何もしない（最初の試行結果が勝る）。
    05 §1.2 step 7「``notify:<id>`` を更新」の実装は ``put_if_absent`` の
    冪等性で近似する。詳細はモジュール docstring「write-once 制約」参照。

    ``sent_at`` はサーバ wall clock（``time.time()``）。05 §1.1「絶対時刻を
    応答に含めない」は **API 応答** の規則であり、内部状態の wall clock
    格納は除外される（GC の TTL 判定等で時刻が必要になる）。

    ``attempts`` は **同一 request 内** の試行回数（1 〜 MAX_ATTEMPTS）。
    """
    if state not in ("sent", "failed"):
        # Defensive: prevents a typo or refactor regression from
        # silently writing an invalid state that poll consumers can't
        # interpret. The notify_state vocabulary is closed (05 §1.3).
        raise NotifyError(f"invalid notify_state: {state!r}")

    payload: dict[str, Any] = {
        "state": state,
        "sent_at": time.time(),
        "attempts": attempts,
    }
    store.put_if_absent(store.notify_key(approval_id), payload)


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------


__all__ = [
    "NotifyError",
    "send_approval_request",
    "NTFY_BASE_URL",
    "MAX_ATTEMPTS",
    "NOTIFICATION_TITLE",
]
