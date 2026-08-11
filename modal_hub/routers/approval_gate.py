"""modal_hub/routers/approval_gate.py — H-H Agent 承認ゲート API 一式（Phase 1a）。

設計上の位置づけ:
    - 親設計書 docs/hh-agent/03_Architecture.md §4.3, §5, §9
    - 実装契約   docs/hh-agent/05_Phase1a_Spec.md 全体（特に §1 API 契約、
                 §2 状態遷移表、§2.1 エラー判定の優先順位、§4 workspace_id、
                 §7 PWA 認証、§10 監査）

本ファイルは `docs/hh-agent/04_Task_Allocation.md` の割当表どおり
`modal_hub/routers/approval_gate.py` を所有する。既存の `core/store.py` /
`core/security.py` / `core/risk.py` / `core/config.py` / `main.py` は
**他の担当者の実装物であり、一切編集していない**（import して使うのみ）。

== アーキテクチャ: 「純粋な状態機械関数」と「FastAPI ハンドラ」を分離する ==

`status_of()` は非負交渉不変条件（ユーザー指示）で「純関数・副作用禁止」と
明記されている。これに加えて本ファイルは、`security.py` が確立した DI
パターン（`CredentialStore` Protocol を関数引数として受け取る）を踏襲し、
状態機械のロジック関数はすべて `store: ApprovalStore`（下記）を明示的な
引数として受け取る形にした。理由:

    1. `status_of()` 以外の関数（`_claim_core` 等）も可能な限り「入力→出力」
       の形にしておくと、Modal 実行環境（`modal.Dict` への実ネットワーク
       接続）が無いユニットテストでも、インメモリのフェイクストアを注入
       するだけで状態遷移の全パス（write-once 競合・エラー優先順位・
       冪等再取得）を検証できる。
    2. FastAPI のルートハンドラ（`@router.post(...)` の関数）は「HTTP の
       着脱（ヘッダ読み取り・JSON パース・レスポンス整形）」だけを担当する
       薄い層にし、`_LIVE_STORE`（本番用アダプタ）を束縛して上記のロジック
       関数を呼ぶだけにする。

== store.py に無い操作をどう扱ったか（重要な設計判断・REQUESTS TO OTHER OWNERS） ==

`core/store.py` が公開する書き込みプリミティブは `put_if_absent` のみであり、
意図的に `update`/`overwrite` 系メソッドを持たない（`decision:`/`lease:`/
`idem:` の write-once 競合を守るため）。しかし Phase1a spec §1.2 手順5・
親設計書 §4.3 は `pending:index` と `gc:index:<day>` に**逐次追加**すること
を要求しており、これは「既存キーの値を書き換える」操作であって
`put_if_absent` だけでは実現できない（2回目以降の追加が必ず no-op になる）。

`pending:index`/`gc:index:<day>` は `decision:`/`lease:`/`idem:` のような
「1回勝負で安全性が決まる」キーとは性質が異なる（ユーザー指示の
non-negotiable invariants は decision:/lease:/idem: の3種類だけを
"never read-then-write" の対象として明示している）。これらのインデックスは
**表示・GC 対象列挙のための補助データ**であり、更新が稀に競合して 1 件
取りこぼしても、影響は「その承認が PWA の一覧に一瞬出ない／GC 対象から漏れる」
だけで、承認状態機械そのもの（req:/decision:/lease:/idem:）の正しさには
一切影響しない。取りこぼした場合も該当の承認は最終的に grace_deadline を
過ぎて `timeout`（＝ deny 側）になるだけであり、フェイルクローズの原則を
破らない。

そのため本ファイルでは、この2キーに限り `modal.Dict` への**直接アクセス**
による「無条件 put（読み取り→追加→上書き）」を実装した（`_index_add`/
`_index_remove`。`store.APPROVALS_DICT_NAME` という公開定数を使い、
`modal.Dict.from_name(...)` を直接呼ぶ。`core/store.py` のファイル自体は
一切変更していない）。

**2026-08-12（Codexレビュー指摘M13の修正）**: 上記の「無条件 put」は、2つの
リクエストがほぼ同時に同じインデックスキーへ追加/削除しようとすると、
後勝ちが前勝ちの変更を静かに上書きして消してしまう（lost update）という
欠陥があった。`_index_add`/`_index_remove` を「書き込み直前にもう一度
読み直して最初の読み取りと一致するか確認し、不一致なら最新値から再計算して
やり直す」リトライ方式に変更し、競合窓を大幅に狭めた（詳細は各関数の
docstring）。`modal.Dict` に真の compare-and-set が無いため完全な排他では
ないが、上記の「取りこぼしても実害は軽微」というリスク評価を踏まえると
この程度の緩和で十分と判断し、`core/store.py` 側への専用ヘルパー追加は
見送った（元々の REQUESTS TO OTHER OWNERS は撤回する）。

**Codexレビューで確認された既知の残存リスク（Medium・受容済み）**: この
リトライ方式は「最初の読み取り→書き込み直前の再読み取り」の間の競合は
検出できるが、「書き込み直前の再読み取り→実際の `overwrite()` 呼び出し」
の間（Dict への1回のRPC呼び出しが完走するまでのごく短い時間）に別の
書き込みが割り込むケースは検出できない（真の compare-and-set が無い以上、
原理的にこの窓は残る）。個人単一開発者・低頻度の承認リクエストという
運用規模では、この窓に2つの書き込みが実際に重なる確率は無視できるほど
小さいと判断し、ロック等の追加実装（設計判断の履歴どおり、ロックは
再入・例外安全性・テストのハングという新たなリスクを持ち込むため導入を
避ける方針）はしないことにした。

`notify:<approval_id>` は通常系ではこのファイルから書き込まない。
`modal_hub/services/notifier.py`（別担当・着地済み）の
`send_approval_request()` が `core/store.py` を直接使って write-once
（`put_if_absent`）で更新する自己完結な設計になっているため、本ファイルは
`_ensure_notified()` からその関数を呼び、戻り値の notify_state
（`"sent"`/`"failed"`）をそのまま使うだけにした（`overwrite` は使わない）。

例外: BUG-6 の修正として、`notifier.send_approval_request()` の**内部**の
write-once 書き込みを経由しない異常系（import 失敗・呼び出し自体が例外を
送出した場合）に限り、`_mark_notify_failed_if_absent()` が `put_if_absent`
のみで `notify:<id>` を `failed` として埋める（`overwrite` は使わない。
既に値があれば無条件に負けて何もしない）。理由は `_ensure_notified()` の
docstring 参照。

== ペアリングオファーの発行エンドポイントについて（BLOCKED 相当・報告） ==

Phase1a spec §7.1 は `pairing_offer:<hash>` を「`hh pwa pair`（ローカル CLI）
が作る」としているが、**そのローカル CLI が Hub のどの HTTP エンドポイント
を呼ぶのかは親設計書にも Phase1a spec にも明記されていない**
（`03_Architecture.md` §4.3 のエンドポイント表にも該当行が無い）。
本ファイルは `POST /api/pwa/pair`（オファーの**消費**側。エンドポイント表に
明記されている）のみを実装し、オファーの**発行**側エンドポイントは実装して
いない。§7.1 の「全体で30回失敗したら有効な pairing_offer をすべて無効化
する」も、発行側の設計が定まらない限り安全に実装できない（列挙する索引を
どちらが管理するか未定のため）ので同様に未実装。実装完了報告の
「REQUESTS TO OTHER OWNERS」で改めて提起する。

== エラーコードのうち Phase1a spec に明記が無いもの ==

§1.4/§1.6/§2.1 に明記されているコード（`NOT_APPROVED`/`CLAIM_EXPIRED`/
`ALREADY_CLAIMED`/`MISMATCH`/`AUDIT_FAILED`/`ALREADY_DECIDED`/
`GRACE_EXPIRED`/`CSRF_FAILED`/`NOT_CLAIMED`）はそのまま使う。それ以外の
汎用エラー（400 のスキーマ不正、401 の一般認証失敗、404、413、429、500）
には spec がコード文字列を与えていないため、本ファイルは
`INVALID_REQUEST` / `UNAUTHORIZED` / `NOT_FOUND` / `PAYLOAD_TOO_LARGE` /
`RATE_LIMITED` / `INTERNAL_ERROR` / `PAIRING_INVALID` / `PAIRING_CONSUMED`
（後2つは §7.1 の例外クラス名をそのまま流用）を独自に定めた。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

import modal
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from modal_hub.core import config
from modal_hub.core import redact
from modal_hub.core import risk as risk_module
from modal_hub.core import security
from modal_hub.core import store
from modal_hub.services import audit

logger = logging.getLogger("hh_agent.approval_gate")

router = APIRouter()

# ---------------------------------------------------------------------------
# 定数（Phase1a spec の数値をそのまま使う。実装都合で変更しない）
# ---------------------------------------------------------------------------

GRACE_SECONDS = 150  # 親設計書 §4.3: grace_deadline = created_at + 150
CLAIM_WINDOW_SECONDS = 180  # 同上: claim_deadline = created_at + 180

HIGH_REQUEST_RATE_LIMIT = 20  # 親設計書 §5.1: HIGH承認要求 20件/時
HIGH_REQUEST_RATE_WINDOW = 3600

PAIRING_IP_LIMIT = 10  # Phase1a spec §7.1
PAIRING_IP_WINDOW = 600
PAIRING_GLOBAL_LIMIT = 30
PAIRING_GLOBAL_WINDOW = 600

PWA_COOKIE_NAME = "hh_pwa"

MAX_PAYLOAD_BYTES = 4096  # Phase1a spec §1.2
MAX_TARGETS = 32
MAX_TOTAL_BODY_BYTES = 65536
MAX_DETAIL_BYTES = 1024  # Phase1a spec §1.5

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")

_ALLOWED_RISK_LEVELS = ("HIGH", "MEDIUM", "LOW")
_ALLOWED_DECISIONS = ("approved", "rejected")
_ALLOWED_OUTCOMES = ("consumed", "failed", "mismatch")


# ---------------------------------------------------------------------------
# API エラー（統一エラー封筒。Phase1a spec §1.1）
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """`{"error": {"code","message","retryable"}}` へ変換されるエラー。

    FastAPI の `APIRouter` はアプリレベルの例外ハンドラを登録できない
    （`FastAPI` インスタンスにしか `add_exception_handler` が無く、
    `main.py` は編集禁止のため登録できない）。そのため各エンドポイント
    ハンドラが自前で `try/except ApiError` して :func:`error_response` を
    返す。pydantic 由来の自動 422（`RequestValidationError`）は使わない
    — Phase1a spec の 400/422 の使い分け（400=スキーマ不正、
    422=意味的に不正）と食い違うため、本ファイルはリクエストボディを
    生 JSON として読み、手動でバリデーションする。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.headers = headers or {}


def error_response(exc: ApiError) -> JSONResponse:
    body = {"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}}
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


def _unexpected_error_response(exc: Exception) -> JSONResponse:
    """想定外の例外を握りつぶさず、フェイルクローズの 500 として返す。

    ユーザー指示 hard rule 3「Unexpected shapes raise, never silently
    return empty」に従い、ここでは例外を「見なかったことにしない」— ログへ
    残し、クライアントには retryable な 500 として汎用メッセージのみ返す
    （トークン・生ペイロード等を含みうる例外メッセージをそのまま返さない。
    ユーザー指示 hard rule 5「Never log tokens, keys, cookie values, or raw
    command payloads」にも配慮し、例外オブジェクト自体はログ側でも
    メッセージのみ・スタックトレースに留め、機微値の埋め込みは呼び出し元
    の設計で防ぐ）。
    """
    logger.exception("unexpected error in approval_gate handler: %s", exc)
    body = {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "internal error",
            "retryable": True,
        }
    }
    return JSONResponse(status_code=500, content=body)


# ---------------------------------------------------------------------------
# ストア層: ApprovalStore Protocol + 本番アダプタ
# ---------------------------------------------------------------------------


class ApprovalStore(Protocol):
    """状態機械ロジックが要求するストアの最小インターフェース。

    `get`/`put_if_absent`/`delete` は `core/store.py` と同一シグネチャ
    （`security.CredentialStore` と同じ形）。`overwrite` だけは
    `core/store.py` に存在しない操作で、`pending:index`/`gc:index:<day>`
    の逐次追加専用（本ファイル冒頭の docstring 参照）。
    """

    def get(self, key: str) -> Optional[Any]: ...

    def put_if_absent(self, key: str, value: Any) -> bool: ...

    def delete(self, key: str) -> None: ...

    def overwrite(self, key: str, value: Any) -> None:
        """key の値を無条件で上書きする。`pending:index`/`gc:index:<day>`
        専用（write-once キーには絶対に使わない）。"""
        ...

    def outbox_register(self, event_id: str, payload: dict[str, Any]) -> bool: ...

    def outbox_consume(self, event_id: str) -> None: ...

    def write_json(self, rel_path: str, obj: Any) -> None: ...


class _LiveApprovalStore:
    """本番用アダプタ。`core/store.py` の実物 + 生 `modal.Dict` アクセス
    （`overwrite` のみ）を束ねて :class:`ApprovalStore` を満たす。"""

    def get(self, key: str) -> Optional[Any]:
        return store.get(key)

    def put_if_absent(self, key: str, value: Any) -> bool:
        return store.put_if_absent(key, value)

    def delete(self, key: str) -> None:
        store.delete(key)

    def overwrite(self, key: str, value: Any) -> None:
        d = modal.Dict.from_name(store.APPROVALS_DICT_NAME, create_if_missing=True)
        d.put(key, value)  # skip_if_exists=False (既定) = 無条件上書き

    def outbox_register(self, event_id: str, payload: dict[str, Any]) -> bool:
        return store.outbox_register(event_id, payload)

    def outbox_consume(self, event_id: str) -> None:
        store.outbox_consume(event_id)

    def write_json(self, rel_path: str, obj: Any) -> None:
        store.write_json(rel_path, obj)


_LIVE_STORE = _LiveApprovalStore()


def _index_get(s: ApprovalStore, key: str) -> list[str]:
    value = s.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ApiError(
            500, "INTERNAL_ERROR", f"corrupt index at {key!r}: not a list", retryable=True
        )
    return value


_INDEX_WRITE_MAX_ATTEMPTS = 5


def _index_add(s: ApprovalStore, key: str, item_id: str) -> None:
    """`key` の値（文字列リスト）へ `item_id` を追加する（既存なら何もしない）。

    M13修正: 従来は読み取り→追加→`s.overwrite()`の単発・非アトミック操作で、
    競合下では取りこぼしが起こりえた。`modal.Dict` に真の compare-and-set が
    無いため完全な排他はできないが、書き込み直前にもう一度読み直して最初の
    読み取りと一致するか確認し、不一致なら（誰かが間に書き込んだ証拠）最新値
    から再計算してやり直す方式にして競合窓を大幅に狭めた。最大
    `_INDEX_WRITE_MAX_ATTEMPTS` 回まで再試行し、それでも決着しなければログに
    残して諦める（安全性クリティカルな write-once キーには絶対に使わない。
    本ファイル冒頭の docstring 参照 — この2キーは表示・GC用の補助データで
    あり、稀な取りこぼしも承認状態機械の正しさには影響しない）。
    """
    for _ in range(_INDEX_WRITE_MAX_ATTEMPTS):
        current = _index_get(s, key)
        if item_id in current:
            return
        candidate = current + [item_id]
        recheck = _index_get(s, key)
        if recheck != current:
            continue
        s.overwrite(key, candidate)
        return
    logger.error("index_add exhausted retries for key=%s item_id=%s", key, item_id)


def _index_remove(s: ApprovalStore, key: str, item_id: str) -> None:
    """`_index_add` と対称。`key` から `item_id` を取り除く。"""
    for _ in range(_INDEX_WRITE_MAX_ATTEMPTS):
        current = _index_get(s, key)
        if item_id not in current:
            return
        candidate = [x for x in current if x != item_id]
        recheck = _index_get(s, key)
        if recheck != current:
            continue
        s.overwrite(key, candidate)
        return
    logger.error("index_remove exhausted retries for key=%s item_id=%s", key, item_id)


def _gc_index_key(now: float) -> str:
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    return store.PREFIX_GC_INDEX + day


# ---------------------------------------------------------------------------
# 純関数: status_of()（非負交渉不変条件。副作用なし）
# ---------------------------------------------------------------------------


def status_of(
    req: dict[str, Any],
    decision: Optional[dict[str, Any]],
    lease: Optional[dict[str, Any]],
    now: float,
) -> str:
    """承認の現在状態を時刻とレコードの有無から純粋に導出する。

    親設計書 §4.3 の疑似コードそのまま。**副作用なし・同じ入力には常に
    同じ出力**（ユーザー指示 non-negotiable invariant）。
    """
    if decision is None:
        return "pending" if now <= req["grace_deadline"] else "timeout"

    # 遅延して書き込まれた決定は無効。
    if decision["at"] > req["grace_deadline"]:
        return "timeout"

    if decision["decision"] != "approved":
        return decision["decision"]  # rejected | timeout

    if lease is not None:
        return "claimed"

    # 承認は無期限に有効ではない。claim されないまま期限を過ぎたら失効。
    if now > req["claim_deadline"]:
        return "timeout"
    return "approved"


def _remaining(req: dict[str, Any], now: float) -> tuple[float, float]:
    grace_remaining = max(0.0, req["grace_deadline"] - now)
    claim_remaining = max(0.0, req["claim_deadline"] - now)
    return grace_remaining, claim_remaining


# ---------------------------------------------------------------------------
# §1.7: タイムアウトの一度きり記録
# ---------------------------------------------------------------------------


def _observe_and_maybe_record_timeout(
    s: ApprovalStore, req: dict[str, Any], decision: Optional[dict[str, Any]], now: float
) -> None:
    """`decision:` が無く `now > grace_deadline` を観測したら write-once で記録する。

    成功した者だけが監査 `timed_out` を1行書く（Phase1a spec §1.7）。
    `status_of()` の戻り値には影響しない副作用専用の処理であり、呼び出し側
    は先に `status_of()` で状態を確定させてから、これを "ついでに" 呼ぶ。
    """
    if decision is not None:
        return
    if now <= req["grace_deadline"]:
        return

    timeout_decision = {
        "decision": "timeout",
        "at": req["grace_deadline"],  # now ではなく grace_deadline（自己判定回避）
        "by": "system",
    }
    won = s.put_if_absent(store.decision_key(req["approval_id"]), timeout_decision)
    if not won:
        return
    try:
        _index_remove(s, store.PREFIX_PENDING_INDEX, req["approval_id"])
    except Exception:
        # index の破損はここでは致命的にしない。この関数は「ついでに」
        # 呼ばれる副作用専用の経路であり（docstring 参照）、`/poll` など
        # 高頻度パスをインデックス破損だけで 500 化させたくない。
        # `_list_pending_items`（一覧表示そのもの）側は引き続き素通り
        # させず 500 にする（そちらは破損の実害が直接ユーザーに見える）。
        logger.exception("failed to prune pending:index for %s", req["approval_id"])
    try:
        audit.record_event(
            s,
            at=now,
            event="timed_out",
            discriminator="",
            approval_id=req["approval_id"],
            sub=req["sub"],
            source=req["source"],
            session_id=req["session_id"],
            workspace_id=req["workspace_id"],
            tool_name=req["tool_name"],
            risk=req["risk"],
            rule_id=req["rule_id"],
            payload=req.get("payload"),
        )
    except Exception:
        # timed_out の監査失敗は「実行を止める方向」の遷移なので、記録に
        # 失敗してもクライアントへの応答には影響させない（§10.4 の原則）。
        logger.exception("failed to record timed_out audit for %s", req["approval_id"])


# ---------------------------------------------------------------------------
# 共通ヘルパー: 認証
# ---------------------------------------------------------------------------


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization")
    if not header or not header.startswith("Bearer "):
        raise ApiError(401, "UNAUTHORIZED", "missing bearer token", retryable=False)
    token = header[len("Bearer ") :].strip()
    if not token:
        raise ApiError(401, "UNAUTHORIZED", "empty bearer token", retryable=False)
    return token


def _verify_agent(request: Request) -> security.AgentIdentity:
    token = _bearer_token(request)
    try:
        return security.verify_agent_token(
            _LIVE_STORE,
            token,
            signing_key=config.agent_token_signing_key().encode("utf-8"),
            signing_key_prev=_encode_optional(config.agent_token_signing_key_prev()),
        )
    except security.SecurityError as exc:
        raise ApiError(401, "UNAUTHORIZED", "agent token invalid", retryable=False) from exc


def _encode_optional(value: Optional[str]) -> Optional[bytes]:
    return value.encode("utf-8") if value else None


def _require_scope(identity: security.AgentIdentity, scope: str) -> None:
    """`identity` が `scope` を持つことを確認する（403 に変換して送出）。

    07_Phase1b_Spec.md §5 で `publish` スコープ専用トークン
    （`distill_token.json`）を導入した際、承認フロー4エンドポイント
    （request/poll/claim/complete）が一度も `require_scope` を呼んでいない
    ことが判明した（Codex 指摘）。`_verify_agent` はトークンの真正性のみ
    確認しスコープを見ないため、修正前は `publish` のみのトークンでも
    承認フローを操作できてしまい、最小権限の分離が Hub 側で機能していなかった。
    """
    try:
        security.require_scope(identity, scope)
    except security.InsufficientScopeError as exc:
        raise ApiError(403, "FORBIDDEN", f"token lacks required scope: {scope!r}", retryable=False) from exc


def _verify_pwa(request: Request) -> security.PwaIdentity:
    cookie_value = request.cookies.get(PWA_COOKIE_NAME)
    if not cookie_value:
        raise ApiError(401, "UNAUTHORIZED", "missing pwa session cookie", retryable=False)
    try:
        return security.verify_pwa_session(
            _LIVE_STORE, cookie_value, session_key=config.pwa_session_key().encode("utf-8")
        )
    except security.SecurityError as exc:
        raise ApiError(401, "UNAUTHORIZED", "pwa session invalid", retryable=False) from exc


def _client_ip(request: Request) -> str:
    """レート制限の subject に使うクライアント IP を決める。

    Modal はプロキシ経由での着信になりうるため `X-Forwarded-For` の先頭
    エントリを優先し、無ければ `request.client.host` を使う（判断の詳細は
    ファイル冒頭 docstring 未収録の軽微な判断のため、ここに明記する:
    Phase1a spec は IP の取得方法を規定していない。フォワードヘッダは
    クライアントが偽装しうるが、レート制限の目的上「無いよりまし」の
    フォールバックとして採用した）。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        candidate = forwarded.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
    if request.client is not None:
        return request.client.host
    return "unknown"


def _origin_header_expected(request: Request) -> str:
    """Hub 自身の Origin を現在のリクエスト URL から組み立てる。

    Phase1a spec §7.3 は「Origin ヘッダが Hub 自身の Origin と完全一致する
    こと」を要求するが、Hub 自身の Origin 値をどこから取得するかは規定が
    無い。専用の設定値を新設する代わりに、常にそのリクエストを受けた
    URL（`request.url`）から `scheme://netloc` を組み立てる方式を採った
    — Modal がリクエストを転送する際に Host ヘッダを書き換えないため、
    デプロイ先が変わっても追随できる。
    """
    url = request.url
    return f"{url.scheme}://{url.netloc}"


# ---------------------------------------------------------------------------
# 共通ヘルパー: ownership / req 読み込み
# ---------------------------------------------------------------------------


def _load_req_or_404(s: ApprovalStore, approval_id: str) -> dict[str, Any]:
    req = s.get(store.req_key(approval_id))
    if req is None:
        raise ApiError(404, "NOT_FOUND", "approval not found", retryable=False)
    return req


def _check_agent_ownership(req: dict[str, Any], identity: security.AgentIdentity) -> None:
    if (
        req.get("source") != identity.source
        or req.get("session_id") != identity.session_id
        or req.get("workspace_id") != identity.workspace_id
    ):
        raise ApiError(404, "NOT_FOUND", "approval not found", retryable=False)


# ---------------------------------------------------------------------------
# 共通ヘルパー: リクエストボディの読み取り・バリデーション
# ---------------------------------------------------------------------------


async def _read_json_body(request: Request, *, max_bytes: int = MAX_TOTAL_BODY_BYTES) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > max_bytes:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "request body too large", retryable=False)
    if not raw:
        raise ApiError(400, "INVALID_REQUEST", "empty request body", retryable=False)
    try:
        obj = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(400, "INVALID_REQUEST", "malformed JSON body", retryable=False) from exc
    if not isinstance(obj, dict):
        raise ApiError(400, "INVALID_REQUEST", "request body must be a JSON object", retryable=False)
    return obj


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ApiError(400, "INVALID_REQUEST", f"missing/invalid field: {key}", retryable=False)
    return value


def _require_hex(body: dict[str, Any], key: str, pattern: "re.Pattern[str]") -> str:
    value = _require_str(body, key)
    if not pattern.match(value):
        raise ApiError(400, "INVALID_REQUEST", f"malformed field: {key}", retryable=False)
    return value


def _require_optional_hex40(body: dict[str, Any], key: str) -> Optional[str]:
    """``40-hex or null`` フィールドを読む。**キー自体が無ければ 400**
    （Phase1a spec §1.4「1つでも欠けたら400」の「欠けたら」はキーの不在を
    指す。明示的な JSON `null` は許された値であり 400 にしない）。
    """
    if key not in body:
        raise ApiError(400, "INVALID_REQUEST", f"missing field: {key}", retryable=False)
    value = body[key]
    if value is None:
        return None
    if not isinstance(value, str) or not _HEX40_RE.match(value):
        raise ApiError(400, "INVALID_REQUEST", f"malformed field: {key}", retryable=False)
    return value


def _require_dict(body: dict[str, Any], key: str) -> dict[str, Any]:
    value = body.get(key)
    if not isinstance(value, dict):
        raise ApiError(400, "INVALID_REQUEST", f"missing/invalid field: {key}", retryable=False)
    return value


def _require_list(body: dict[str, Any], key: str) -> list[Any]:
    value = body.get(key)
    if not isinstance(value, list):
        raise ApiError(400, "INVALID_REQUEST", f"missing/invalid field: {key}", retryable=False)
    return value


_TARGET_KEYS = ("path", "realpath", "identity", "preimage_sha256", "exists")


def _validate_target(target: Any) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise ApiError(400, "INVALID_REQUEST", "target must be an object", retryable=False)
    for key in ("path", "realpath", "identity"):
        if not isinstance(target.get(key), str) or not target[key]:
            raise ApiError(400, "INVALID_REQUEST", f"target missing field: {key}", retryable=False)
    if "preimage_sha256" not in target or (
        target["preimage_sha256"] is not None
        and (not isinstance(target["preimage_sha256"], str) or not _HEX64_RE.match(target["preimage_sha256"]))
    ):
        raise ApiError(400, "INVALID_REQUEST", "target missing/invalid field: preimage_sha256", retryable=False)
    if not isinstance(target.get("exists"), bool):
        raise ApiError(400, "INVALID_REQUEST", "target missing field: exists", retryable=False)
    return {k: target[k] for k in _TARGET_KEYS}


# ---------------------------------------------------------------------------
# reason_code / rule_id → 表示文言
# ---------------------------------------------------------------------------

# classify() 内で合成される rule_id（risk_rules.yaml には無い）の表示文言。
# core/risk.py の docstring・実装（Risk.reason の組み立て）と対応させる。
_SYNTHETIC_RULE_REASONS = {
    "unknown_tool": "未知のツールは副作用ありとみなし、承認が必要です",
    "hermes_dangerous_command": "Hermes 危険コマンド検出器がマッチしました",
    "read_only": "",
    "no_match": "",
}


def _reason_for_rule_id(rule_id: str) -> str:
    """`rule_id`（＝ `reason_code`）から PWA 表示用の日本語文言を引く。

    Phase1a spec §1.2「表示文言はサーバが rule_id から引く」。`risk.py` は
    この用途の公開 API を持たないため、`risk.py` が既にロード・検証済みの
    `risk_rules.yaml` を再利用する（`risk_module._load_rules()` は
    アンダースコア始まりの非公開関数だが、読み取り専用でパース処理の
    重複を避けるためにここから参照する。REQUESTS TO OTHER OWNERS として
    `risk.py` に公開ヘルパーの追加を提案する）。
    """
    if rule_id in _SYNTHETIC_RULE_REASONS:
        return _SYNTHETIC_RULE_REASONS[rule_id]
    try:
        rules = risk_module._load_rules()  # type: ignore[attr-defined]
    except Exception:
        logger.exception("failed to load risk_rules.yaml for reason lookup")
        return ""
    for level in ("high", "medium"):
        for rule in rules.get(level, []):
            if rule.get("id") == rule_id:
                return rule.get("reason", "")
    return ""


def _summarize_payload(payload: Any) -> str:
    """PWA 一覧表示用の `summary`（redaction 済みコマンドの先頭200文字）。

    BUG-5 修正: 必ず **redact してから truncate** する。逆順（切り詰めてから
    redact）だと、200 文字境界をまたぐ秘密のパターンが分断されて正規表現が
    一致しなくなり、秘密の生の先頭部分が truncate 後の文字列にそのまま残って
    PWA へ表示されてしまう（`test_secret_straddling_the_200_char_boundary_is_
    still_redacted` / `test_pwa_summary_redacts_secrets_straddling_the_200_
    char_truncation_boundary` の回帰）。
    """
    if isinstance(payload, dict) and isinstance(payload.get("command"), str):
        text = payload["command"]
    else:
        try:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(payload)
    return redact.redact_text(text)[:200]


# ===========================================================================
# POST /api/approval/request
# ===========================================================================


def _new_approval_id(s: ApprovalStore) -> str:
    for _ in range(5):
        candidate = str(uuid.uuid4())
        if s.get(store.req_key(candidate)) is None:
            return candidate
    raise ApiError(500, "INTERNAL_ERROR", "failed to allocate approval_id", retryable=True)


def _mark_notify_failed_if_absent(s: ApprovalStore, approval_id: str, now: float) -> None:
    """BUG-6 修正: `notify:<id>` が未だ無ければ `failed` を write-once で記録する。

    `services/notifier.py.send_approval_request()` は「`MAX_ATTEMPTS` 回
    リトライしてすべて失敗した」という**正常系の失敗経路**では自分で
    `notify:<id>` に `state="failed"` を書いてから戻る。しかし import 失敗
    （未着地時の防御）や `send_approval_request` 自体が予期せず例外を送出
    した経路（ネットワーク層の想定外の例外・バグ等）は、その内部の
    書き込みを一切経由しないため `notify:<id>` が空のまま残ってしまう。

    `/poll` は `notify:<id>` が無ければ `notify_state="pending"` を返し、
    spec §1.3 は `"pending"` を「エージェントは待ち続けてよい」と定義して
    いるため、通知が実際には一度も届いていないのに応答を取りこぼした
    エージェントが猶予 150 秒をまるごと待たされる（この関数が直す不具合）。

    ここでは `put_if_absent`（write-once）だけを使う。`store.py` に
    `overwrite`/`update` 系プリミティブは追加しない — 2026-08-11 追記
    (05_Phase1a_Spec.md §1.2 手順7 直下) のとおり `notify:<id>` の
    `failed` は**意図的に sticky**（一度 `failed` が確定したら以後変わら
    ない）。この関数はその sticky な `failed` を「取りこぼした場合に限って
    埋める」だけであり、既に何か（`sent`/`failed`）が書かれていれば
    `put_if_absent` は無条件に負けて何もしない（先着優先・上書きしない）。
    """
    s.put_if_absent(store.notify_key(approval_id), {"state": "failed", "sent_at": now, "attempts": 0})


def _ensure_notified(s: ApprovalStore, req: dict[str, Any], *, now: float) -> str:
    """`notify:<id>` が `sent` でなければ通知を試みる。返り値は notify_state。

    `services/notifier.py`（別担当。着地済み — `send_approval_request(
    approval_id, risk) -> str` が `notify:<id>` の write-once 更新と
    ntfy への最大3回リトライ送信を**自己完結で**行い、最終的な
    notify_state（`"sent"`/`"failed"`）をそのまま返す）へ**基本的に委譲**
    する。通常系（リトライ規定回数を使い切って `failed` に落ちる経路を
    含む）では本ファイル側は `notify:` に書き込まない（notifier.py が
    `core/store.py` を直接使って `put_if_absent` する設計のため、ここで
    二重に書くと責務が重複し、意味論が食い違う余地を作る）。

    ただし BUG-6 の修正として、notifier.py 側の書き込みを経由しない**異常
    系**（import 失敗・`send_approval_request` 自体が例外を送出した場合）
    に限り、:func:`_mark_notify_failed_if_absent` で `notify:<id>` を
    write-once で埋める。これにより `/poll` が `pending` を返し続けて
    エージェントを 150 秒待たせる事態を防ぐ（親設計書 §4.3「全失敗した
    場合はエージェントのpollにnotify_failed:trueを含めて返す」→
    エージェントは待たずに deny できる。「通知経路が無いのに承認待ちで
    固まる」よりも安全側）。
    """
    approval_id = req["approval_id"]
    try:
        from modal_hub.services import notifier  # type: ignore[import-not-found]
    except ImportError:
        logger.error("services.notifier not importable; treating notify as failed for %s", approval_id)
        _mark_notify_failed_if_absent(s, approval_id, now)
        return "failed"

    try:
        return notifier.send_approval_request(approval_id, req["risk"])
    except Exception:
        logger.exception("notifier.send_approval_request failed for %s", approval_id)
        _mark_notify_failed_if_absent(s, approval_id, now)
        return "failed"


@router.post("/api/approval/request")
async def approval_request(request: Request) -> JSONResponse:
    try:
        identity = _verify_agent(request)
        _require_scope(identity, "request")

        try:
            security.check_rate_limit(
                _LIVE_STORE,
                identity.sub,
                limit=HIGH_REQUEST_RATE_LIMIT,
                window_seconds=HIGH_REQUEST_RATE_WINDOW,
            )
        except security.RateLimitExceededError as exc:
            raise ApiError(
                429,
                "RATE_LIMITED",
                "HIGH approval request rate limit exceeded",
                retryable=True,
                headers={"Retry-After": str(int(exc.retry_after_seconds) + 1)},
            ) from exc

        body = await _read_json_body(request)
        idempotency_key = _require_str(body, "idempotency_key")
        if not _IDEMPOTENCY_KEY_RE.match(idempotency_key):
            raise ApiError(400, "INVALID_REQUEST", "malformed idempotency_key", retryable=False)

        now = time.time()

        # --- 手順2: idem: を先に読む（既存があれば手順7へ）---
        idem_key = store.idem_key(identity.sub, idempotency_key)
        existing_idem = _LIVE_STORE.get(idem_key)
        if existing_idem is not None:
            existing_id = existing_idem.get("approval_id") if isinstance(existing_idem, dict) else None
            if not isinstance(existing_id, str):
                raise ApiError(500, "INTERNAL_ERROR", "corrupt idem record", retryable=True)
            existing_req = _LIVE_STORE.get(store.req_key(existing_id))
            if existing_req is None:
                raise ApiError(500, "INTERNAL_ERROR", "idem points to missing req", retryable=True)
            _check_agent_ownership(existing_req, identity)
            notify_state = _ensure_notified(_LIVE_STORE, existing_req, now=now)
            grace_remaining, claim_remaining = _remaining(existing_req, now)
            return JSONResponse(
                status_code=200,
                content={
                    "approval_id": existing_id,
                    "grace_remaining_seconds": grace_remaining,
                    "claim_remaining_seconds": claim_remaining,
                    "reused": True,
                    "notify_state": notify_state,
                },
            )

        # --- スキーマ検証（新規作成パス）---
        tool_name = _require_str(body, "tool_name")
        payload = _require_dict(body, "payload")
        payload_sha256 = _require_hex(body, "payload_sha256", _HEX64_RE)
        payload_raw_sha256 = _require_hex(body, "payload_raw_sha256", _HEX64_RE)
        context_body = _require_dict(body, "context")
        cwd = _require_str(context_body, "cwd")
        context_workspace_id = _require_hex(context_body, "workspace_id", _HEX64_RE)
        base_revision = _require_optional_hex40(context_body, "base_revision")
        risk_level = _require_str(body, "risk")
        if risk_level not in _ALLOWED_RISK_LEVELS:
            raise ApiError(400, "INVALID_REQUEST", "invalid risk level", retryable=False)
        rule_id = _require_str(body, "rule_id")
        reason_code = _require_str(body, "reason_code")
        raw_targets = _require_list(body, "targets")
        if len(raw_targets) > MAX_TARGETS:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "too many targets", retryable=False)
        targets = [_validate_target(t) for t in raw_targets]

        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(payload_bytes) > MAX_PAYLOAD_BYTES:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "payload too large", retryable=False)

        approval_id = _new_approval_id(_LIVE_STORE)
        req_record = {
            "approval_id": approval_id,
            "idempotency_key": idempotency_key,
            "sub": identity.sub,
            "source": identity.source,
            "session_id": identity.session_id,
            "workspace_id": identity.workspace_id,  # トークン由来（所有権照合用）
            "tool_name": tool_name,
            "payload": payload,
            "payload_sha256": payload_sha256,
            "payload_raw_sha256": payload_raw_sha256,
            "context": {
                "cwd": cwd,
                "workspace_id": context_workspace_id,  # フック計算値（claim時のドリフト検知用）
                "base_revision": base_revision,
            },
            "risk": risk_level,
            "rule_id": rule_id,
            "reason_code": reason_code,
            "targets": targets,
            "created_at": now,
            "grace_deadline": now + GRACE_SECONDS,
            "claim_deadline": now + CLAIM_WINDOW_SECONDS,
        }

        # --- 手順3: 先に req: を作る ---
        if not _LIVE_STORE.put_if_absent(store.req_key(approval_id), req_record):
            raise ApiError(500, "INTERNAL_ERROR", "approval_id collision", retryable=True)

        # --- 手順4: idem: を後で作る。負けたら勝者を採用（作った req: は孤児に）---
        if not _LIVE_STORE.put_if_absent(idem_key, {"approval_id": approval_id}):
            winner_idem = _LIVE_STORE.get(idem_key)
            winner_id = winner_idem.get("approval_id") if isinstance(winner_idem, dict) else None
            if not isinstance(winner_id, str):
                raise ApiError(500, "INTERNAL_ERROR", "corrupt idem record", retryable=True)
            winner_req = _LIVE_STORE.get(store.req_key(winner_id))
            if winner_req is None:
                raise ApiError(500, "INTERNAL_ERROR", "idem points to missing req", retryable=True)
            _check_agent_ownership(winner_req, identity)
            notify_state = _ensure_notified(_LIVE_STORE, winner_req, now=now)
            grace_remaining, claim_remaining = _remaining(winner_req, now)
            return JSONResponse(
                status_code=200,
                content={
                    "approval_id": winner_id,
                    "grace_remaining_seconds": grace_remaining,
                    "claim_remaining_seconds": claim_remaining,
                    "reused": True,
                    "notify_state": notify_state,
                },
            )

        # --- 手順5: インデックスへ追加 ---
        _index_add(_LIVE_STORE, store.PREFIX_PENDING_INDEX, approval_id)
        _index_add(_LIVE_STORE, _gc_index_key(now), approval_id)

        # --- 手順6: 監査 requested（outbox 登録失敗のみ 500）---
        try:
            audit.record_event(
                _LIVE_STORE,
                at=now,
                event="requested",
                discriminator="",
                approval_id=approval_id,
                sub=identity.sub,
                source=identity.source,
                session_id=identity.session_id,
                workspace_id=identity.workspace_id,
                tool_name=tool_name,
                risk=risk_level,
                rule_id=rule_id,
                payload=payload,
            )
        except Exception as exc:
            # 親設計書 §4.3「要求の受付は妨げない」（requested/notified の
            # outbox 失敗は警告のみ。§10.4 表）。ここでは 500 にしない。
            logger.exception("failed to register requested audit for %s: %s", approval_id, exc)

        # --- 手順7: ntfy 送信 ---
        notify_state = _ensure_notified(_LIVE_STORE, req_record, now=now)

        # --- 手順8: 応答 ---
        grace_remaining, claim_remaining = _remaining(req_record, now)
        return JSONResponse(
            status_code=201,
            content={
                "approval_id": approval_id,
                "grace_remaining_seconds": grace_remaining,
                "claim_remaining_seconds": claim_remaining,
                "reused": False,
                "notify_state": notify_state,
            },
        )
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return _unexpected_error_response(exc)


# ===========================================================================
# GET /api/approval/poll
# ===========================================================================


@router.get("/api/approval/poll")
async def approval_poll(request: Request) -> JSONResponse:
    try:
        identity = _verify_agent(request)
        _require_scope(identity, "poll")
        approval_id = request.query_params.get("id")
        if not approval_id:
            raise ApiError(400, "INVALID_REQUEST", "missing query param: id", retryable=False)

        req = _load_req_or_404(_LIVE_STORE, approval_id)
        _check_agent_ownership(req, identity)

        now = time.time()
        decision = _LIVE_STORE.get(store.decision_key(approval_id))
        lease = _LIVE_STORE.get(store.lease_key(approval_id))
        status = status_of(req, decision, lease, now)

        _observe_and_maybe_record_timeout(_LIVE_STORE, req, decision, now)

        notify = _LIVE_STORE.get(store.notify_key(approval_id))
        notify_state = notify.get("state", "pending") if isinstance(notify, dict) else "pending"
        decided_by = decision.get("by") if isinstance(decision, dict) else None

        grace_remaining, claim_remaining = _remaining(req, now)
        return JSONResponse(
            status_code=200,
            content={
                "approval_id": approval_id,
                "status": status,
                "grace_remaining_seconds": grace_remaining,
                "claim_remaining_seconds": claim_remaining,
                "notify_state": notify_state,
                "decided_by": decided_by,
            },
        )
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return _unexpected_error_response(exc)


# ===========================================================================
# POST /api/approval/claim
# ===========================================================================


def _verification_mismatches(req: dict[str, Any], verification: dict[str, Any]) -> list[str]:
    """§1.4 の照合表を実施する。欠落は呼び出し側で 400 に振り分け済みの前提。

    戻り値は不一致だったフィールド名のリスト（空なら完全一致）。
    """
    mismatches: list[str] = []
    if verification["payload_sha256"] != req["payload_sha256"]:
        mismatches.append("payload_sha256")
    if verification["payload_raw_sha256"] != req["payload_raw_sha256"]:
        mismatches.append("payload_raw_sha256")

    ctx = verification["context"]
    req_ctx = req["context"]
    if ctx["cwd"] != req_ctx["cwd"]:
        mismatches.append("context.cwd")
    if ctx["workspace_id"] != req_ctx["workspace_id"]:
        mismatches.append("context.workspace_id")
    # base_revision: 両方 null なら一致とみなす（Phase1a spec §4）。
    if ctx["base_revision"] != req_ctx["base_revision"]:
        mismatches.append("context.base_revision")

    req_targets = req["targets"]
    ver_targets = verification["targets"]
    if len(req_targets) != len(ver_targets):
        mismatches.append("targets")
    else:
        for i, (rt, vt) in enumerate(zip(req_targets, ver_targets)):
            for key in _TARGET_KEYS:
                if rt.get(key) != vt.get(key):
                    mismatches.append(f"targets[{i}].{key}")
    return mismatches


def _parse_claim_verification(body: dict[str, Any]) -> dict[str, Any]:
    verification = _require_dict(body, "verification")
    payload_sha256 = _require_hex(verification, "payload_sha256", _HEX64_RE)
    payload_raw_sha256 = _require_hex(verification, "payload_raw_sha256", _HEX64_RE)
    context_body = _require_dict(verification, "context")
    cwd = _require_str(context_body, "cwd")
    workspace_id = _require_hex(context_body, "workspace_id", _HEX64_RE)
    base_revision = _require_optional_hex40(context_body, "base_revision")
    raw_targets = _require_list(verification, "targets")
    targets = [_validate_target(t) for t in raw_targets]
    return {
        "payload_sha256": payload_sha256,
        "payload_raw_sha256": payload_raw_sha256,
        "context": {"cwd": cwd, "workspace_id": workspace_id, "base_revision": base_revision},
        "targets": targets,
    }


@router.post("/api/approval/claim")
async def approval_claim(request: Request) -> JSONResponse:
    try:
        identity = _verify_agent(request)
        _require_scope(identity, "claim")
        body = await _read_json_body(request)
        approval_id = _require_str(body, "approval_id")
        claim_attempt_id = _require_str(body, "claim_attempt_id")
        # DEFECT 3 修正: `verification` の完全なスキーマ解析は §2.1 判定順序 8
        # の直前まで遅らせる（ここではまだ呼ばない）。手順 1〜7 は
        # approval_id / claim_attempt_id だけで判定できる — verification を
        # 先に解析すると、同一 attempt の冪等リトライ（手順 2）が
        # malformed verification のせいで 400 になってしまう
        # （本来は idempotent 200 が返るべき正当な応答喪失リトライ）。

        now = time.time()

        # 1. 所有権不一致 → 404
        req = _load_req_or_404(_LIVE_STORE, approval_id)
        _check_agent_ownership(req, identity)

        lease = _LIVE_STORE.get(store.lease_key(approval_id))

        # 2/3. lease 既存 → attempt_id で分岐
        if lease is not None:
            if lease.get("claim_attempt_id") == claim_attempt_id:
                # 冪等な再取得。監査も冪等に再試行する（event_id が決定的
                # なので、初回の outbox 登録が失敗していた場合の回復経路
                # として機能する。Phase1a spec §1.4「ストア復旧後のリトライ
                # で同じleaseを受け取って実行できる」）。
                _record_claim_granted_or_raise(req, lease, claim_attempt_id, now)
                return JSONResponse(status_code=200, content={"granted": True, "lease_id": lease["lease_id"]})
            raise ApiError(409, "ALREADY_CLAIMED", "lease already held by a different attempt", retryable=False)

        decision = _LIVE_STORE.get(store.decision_key(approval_id))

        # 4/5. decision 無し、または approved でない → NOT_APPROVED
        if decision is None or decision.get("decision") != "approved":
            raise ApiError(409, "NOT_APPROVED", "approval not granted", retryable=False)

        # 6. 遅延決定 → GRACE_EXPIRED
        if decision["at"] > req["grace_deadline"]:
            raise ApiError(422, "GRACE_EXPIRED", "decision arrived after grace_deadline", retryable=False)

        # 7. claim_deadline 超過 → CLAIM_EXPIRED
        if now > req["claim_deadline"]:
            raise ApiError(422, "CLAIM_EXPIRED", "claim window expired", retryable=False)

        # 8. 検証不一致 → 監査 mismatch → MISMATCH
        # ここで初めて verification の完全なスキーマ解析を行う（DEFECT 3）。
        # 手順 1〜7（所有権・冪等再取得・ALREADY_CLAIMED・NOT_APPROVED・
        # GRACE_EXPIRED・CLAIM_EXPIRED）をすべて通過した場合のみ到達する。
        verification = _parse_claim_verification(body)
        mismatches = _verification_mismatches(req, verification)
        if mismatches:
            try:
                audit.record_event(
                    _LIVE_STORE,
                    at=now,
                    event="mismatch",
                    discriminator=claim_attempt_id,
                    approval_id=approval_id,
                    sub=req["sub"],
                    source=req["source"],
                    session_id=req["session_id"],
                    workspace_id=req["workspace_id"],
                    tool_name=req["tool_name"],
                    risk=req["risk"],
                    rule_id=req["rule_id"],
                    payload=req.get("payload"),
                    detail=f"mismatched fields: {', '.join(mismatches)}",
                )
            except Exception:
                # mismatch は「実行を止める方向」の遷移。監査失敗があっても
                # deny 応答自体は届ける（§10.4 の原則）。
                logger.exception("failed to record mismatch audit for %s", approval_id)
            raise ApiError(
                422, "MISMATCH", f"verification mismatch: {', '.join(mismatches)}", retryable=False
            )

        # 9. lease 取得へ
        new_lease = {
            "lease_id": str(uuid.uuid4()),
            "claim_attempt_id": claim_attempt_id,
            "claimed_at": now,
            "claimant_sub": identity.sub,
        }
        if not _LIVE_STORE.put_if_absent(store.lease_key(approval_id), new_lease):
            # この直前の読み取り以降に他者が lease を取った（真の競合）。
            existing_lease = _LIVE_STORE.get(store.lease_key(approval_id))
            if existing_lease and existing_lease.get("claim_attempt_id") == claim_attempt_id:
                _record_claim_granted_or_raise(req, existing_lease, claim_attempt_id, now)
                return JSONResponse(
                    status_code=200, content={"granted": True, "lease_id": existing_lease["lease_id"]}
                )
            raise ApiError(409, "ALREADY_CLAIMED", "lease already held by a different attempt", retryable=False)

        _record_claim_granted_or_raise(req, new_lease, claim_attempt_id, now)
        return JSONResponse(status_code=200, content={"granted": True, "lease_id": new_lease["lease_id"]})
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return _unexpected_error_response(exc)


def _record_claim_granted_or_raise(
    req: dict[str, Any], lease: dict[str, Any], claim_attempt_id: str, now: float
) -> None:
    """claim_granted の監査を試みる。outbox 登録に失敗したら 500 AUDIT_FAILED。

    Phase1a spec §1.4「監査 outbox 登録失敗 → 500 AUDIT_FAILED（retryable:
    true。同一 claim_attempt_id でのリトライは安全）」。lease は既に書かれた
    ままにする（焼かない）。
    """
    try:
        audit.record_event(
            _LIVE_STORE,
            at=now,
            event="claim_granted",
            discriminator=claim_attempt_id,
            approval_id=req["approval_id"],
            sub=req["sub"],
            source=req["source"],
            session_id=req["session_id"],
            workspace_id=req["workspace_id"],
            tool_name=req["tool_name"],
            risk=req["risk"],
            rule_id=req["rule_id"],
            payload=req.get("payload"),
        )
    except Exception as exc:
        raise ApiError(
            500,
            "AUDIT_FAILED",
            "audit outbox registration failed; lease retained, retry with same claim_attempt_id",
            retryable=True,
        ) from exc


# ===========================================================================
# POST /api/approval/complete
# ===========================================================================

_COMPLETE_PREFIX = "complete:"  # store.py に無い独自キー（本ファイル冒頭参照）


def _complete_key(approval_id: str) -> str:
    return _COMPLETE_PREFIX + approval_id


@router.post("/api/approval/complete")
async def approval_complete(request: Request) -> JSONResponse:
    try:
        identity = _verify_agent(request)
        _require_scope(identity, "complete")
        body = await _read_json_body(request)
        approval_id = _require_str(body, "approval_id")
        lease_id = _require_str(body, "lease_id")
        # DEFECT 3 修正: `outcome`/`detail` のスキーマ検証は §2.1 判定順序 5
        # （記録）の直前まで遅らせる。ここで先に検証すると、他人の承認への
        # complete が所有権不一致の 404（判定順序 1）より先にスキーマ
        # エラー（400/413）を返してしまい、「承認が存在すること」を
        # 未所有者へ漏らしてしまう。

        now = time.time()

        # 1. 所有権不一致 → 404
        req = _load_req_or_404(_LIVE_STORE, approval_id)
        _check_agent_ownership(req, identity)

        # 2/3. lease 無し、または lease_id 不一致 → NOT_CLAIMED
        lease = _LIVE_STORE.get(store.lease_key(approval_id))
        if lease is None or lease.get("lease_id") != lease_id:
            raise ApiError(409, "NOT_CLAIMED", "no matching lease", retryable=False)

        # 4. 既に complete 済み → 200 冪等
        complete_key = _complete_key(approval_id)
        existing_complete = _LIVE_STORE.get(complete_key)
        if existing_complete is not None:
            return JSONResponse(status_code=200, content={"recorded": False, "already": True})

        # 5. 以上を通過 → 記録。ここで初めて outcome/detail を検証する。
        outcome = _require_str(body, "outcome")
        if outcome not in _ALLOWED_OUTCOMES:
            raise ApiError(400, "INVALID_REQUEST", "invalid outcome", retryable=False)
        detail = body.get("detail")
        if detail is not None:
            if not isinstance(detail, str):
                raise ApiError(400, "INVALID_REQUEST", "detail must be a string", retryable=False)
            if len(detail.encode("utf-8")) > MAX_DETAIL_BYTES:
                raise ApiError(413, "PAYLOAD_TOO_LARGE", "detail too large", retryable=False)

        complete_record = {
            "lease_id": lease_id,
            "outcome": outcome,
            "detail": detail,
            "at": now,
            "by": identity.sub,
        }
        if not _LIVE_STORE.put_if_absent(complete_key, complete_record):
            # 競合: 他の呼び出しが先に complete した。冪等応答。
            return JSONResponse(status_code=200, content={"recorded": False, "already": True})

        _index_remove(_LIVE_STORE, store.PREFIX_PENDING_INDEX, approval_id)

        # 監査。complete は「実行を止める／既に確定した」遷移（実行そのもの
        # は claim の時点で既に許可済み）なので、§10.4 の原則により監査失敗
        # があっても 200 を返す（この endpoint の失敗時挙動は spec §1.5/§10.4
        # に明記が無いための判断。REQUESTS TO OTHER OWNERS で開示）。
        try:
            audit.record_event(
                _LIVE_STORE,
                at=now,
                event=outcome,
                discriminator=lease_id,
                approval_id=approval_id,
                sub=req["sub"],
                source=req["source"],
                session_id=req["session_id"],
                workspace_id=req["workspace_id"],
                tool_name=req["tool_name"],
                risk=req["risk"],
                rule_id=req["rule_id"],
                payload=req.get("payload"),
                detail=detail,
            )
        except Exception:
            logger.exception("failed to record %s audit for %s", outcome, approval_id)

        return JSONResponse(status_code=200, content={"recorded": True})
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return _unexpected_error_response(exc)


# ===========================================================================
# GET /api/approval/pending, GET /api/approval/detail
# ===========================================================================


def _list_pending_items(s: ApprovalStore, *, now: float) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for approval_id in list(_index_get(s, store.PREFIX_PENDING_INDEX)):
        req = s.get(store.req_key(approval_id))
        if req is None:
            _index_remove(s, store.PREFIX_PENDING_INDEX, approval_id)
            continue
        decision = s.get(store.decision_key(approval_id))
        lease = s.get(store.lease_key(approval_id))
        status = status_of(req, decision, lease, now)
        _observe_and_maybe_record_timeout(s, req, decision, now)
        if status != "pending":
            _index_remove(s, store.PREFIX_PENDING_INDEX, approval_id)
            continue
        grace_remaining, _claim_remaining = _remaining(req, now)
        items.append(
            {
                "approval_id": approval_id,
                "tool_name": req["tool_name"],
                "risk": req["risk"],
                "rule_id": req["rule_id"],
                "reason": _reason_for_rule_id(req.get("reason_code", req["rule_id"])),
                "summary": _summarize_payload(req.get("payload")),
                # DEFECT 1 修正: PWA が「どのディレクトリ/リポジトリを対象と
                # するコマンドか」を判断できるよう、pending 一覧にも context
                # を含める（06_PWA_Design.md）。redaction は payload と同じ
                # 経路（redact.redact_value、truncate は行わない）を通す —
                # cwd はファイルシステムパスでユーザー名やトークンを含み
                # うる（05_Phase1a_Spec.md 本タスク指示参照）。
                "context": redact.redact_value(req["context"]),
                "grace_remaining_seconds": grace_remaining,
            }
        )
    return items


@router.get("/api/approval/pending")
async def approval_pending(request: Request) -> JSONResponse:
    try:
        identity = _verify_pwa(request)
        now = time.time()
        items = _list_pending_items(_LIVE_STORE, now=now)
        csrf_token = security.issue_csrf_token(
            identity.session_id, session_key=config.pwa_session_key().encode("utf-8"), now=now
        )
        return JSONResponse(status_code=200, content={"items": items, "csrf_token": csrf_token})
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return _unexpected_error_response(exc)


@router.get("/api/approval/detail")
async def approval_detail(request: Request) -> JSONResponse:
    """全文と差分の個別取得（Phase1a spec §1.6 で存在は明記されているが、

    応答スキーマ自体は spec に明記が無い）。`pending` の1件分を、
    `summary`（先頭200文字）ではなく `payload` 全文（redaction 済み）で
    返す拡張として実装した。これは埋めた判断であり REQUESTS TO OTHER
    OWNERS で開示する。
    """
    try:
        identity = _verify_pwa(request)
        approval_id = request.query_params.get("id")
        if not approval_id:
            raise ApiError(400, "INVALID_REQUEST", "missing query param: id", retryable=False)

        req = _load_req_or_404(_LIVE_STORE, approval_id)
        now = time.time()
        decision = _LIVE_STORE.get(store.decision_key(approval_id))
        lease = _LIVE_STORE.get(store.lease_key(approval_id))
        status = status_of(req, decision, lease, now)
        _observe_and_maybe_record_timeout(_LIVE_STORE, req, decision, now)

        grace_remaining, claim_remaining = _remaining(req, now)
        return JSONResponse(
            status_code=200,
            content={
                "approval_id": approval_id,
                "status": status,
                "tool_name": req["tool_name"],
                "risk": req["risk"],
                "rule_id": req["rule_id"],
                "reason": _reason_for_rule_id(req.get("reason_code", req["rule_id"])),
                "payload": redact.redact_value(req.get("payload")),
                # DEFECT 1 修正: detail にも context を含める（pending と
                # 同じ shape・同じ redaction 経路）。
                "context": redact.redact_value(req["context"]),
                # DEFECT 2 修正: targets はクライアント（エージェント）由来の
                # 自由文（path 等）を含み、認証済みエージェントが認証情報を
                # 埋め込んで PWA に表示させることができる。payload と同じく
                # redact してから返す（truncate はここでは行わないため
                # 順序問題は生じないが、念のため payload と同一の
                # redact.redact_value を通す）。
                "targets": redact.redact_value(req.get("targets", [])),
                "grace_remaining_seconds": grace_remaining,
                "claim_remaining_seconds": claim_remaining,
            },
        )
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return _unexpected_error_response(exc)


# ===========================================================================
# POST /api/approval/respond
# ===========================================================================


@router.post("/api/approval/respond")
async def approval_respond(request: Request) -> JSONResponse:
    try:
        identity = _verify_pwa(request)
        body = await _read_json_body(request)
        # DEFECT 3 修正: 手順 1（CSRF/Origin）に必要な最小限のフィールド
        # （csrf）だけをここで読む。`decision` の閉じた語彙チェックは
        # §2.1 判定順序の最後（手順 5）まで遅らせる — CSRF/Origin 不正と
        # 不正な decision が同時に来た場合でも 403 CSRF_FAILED を返す
        # 必要があるため（400 が先に返ってしまうと CSRF 不正が隠れる）。
        csrf = _require_str(body, "csrf")

        # 1. CSRF / Origin 不正 → 403 CSRF_FAILED
        try:
            security.verify_origin(request.headers.get("origin"), _origin_header_expected(request))
            security.verify_csrf_token(
                identity.session_id, csrf, session_key=config.pwa_session_key().encode("utf-8")
            )
        except security.InvalidCsrfError as exc:
            raise ApiError(403, "CSRF_FAILED", "csrf or origin check failed", retryable=False) from exc

        now = time.time()
        approval_id = _require_str(body, "approval_id")

        # 2. req: が無い → 404
        req = _load_req_or_404(_LIVE_STORE, approval_id)

        # 3. decision: が既存 → 409 ALREADY_DECIDED（timeout でも同じ）
        existing_decision = _LIVE_STORE.get(store.decision_key(approval_id))
        if existing_decision is not None:
            raise ApiError(409, "ALREADY_DECIDED", "a decision already exists", retryable=False)

        # 4. now > grace_deadline → §1.7 write-once → 422 GRACE_EXPIRED
        if now > req["grace_deadline"]:
            _observe_and_maybe_record_timeout(_LIVE_STORE, req, None, now)
            raise ApiError(422, "GRACE_EXPIRED", "grace period expired", retryable=False)

        # 5. decision: 書き込みへ。ここで初めて decision の閉じた語彙を検証する。
        decision_value = _require_str(body, "decision")
        if decision_value not in _ALLOWED_DECISIONS:
            raise ApiError(400, "INVALID_REQUEST", "invalid decision", retryable=False)
        new_decision = {"decision": decision_value, "at": now, "by": "pwa"}
        if not _LIVE_STORE.put_if_absent(store.decision_key(approval_id), new_decision):
            # 競合: 他者（timeoutのwrite-once含む）が先に書いた。
            raise ApiError(409, "ALREADY_DECIDED", "a decision already exists", retryable=False)

        _index_remove(_LIVE_STORE, store.PREFIX_PENDING_INDEX, approval_id)

        event_name = "approved" if decision_value == "approved" else "rejected"
        try:
            audit.record_event(
                _LIVE_STORE,
                at=now,
                event=event_name,
                discriminator="",
                approval_id=approval_id,
                sub=req["sub"],
                source=req["source"],
                session_id=req["session_id"],
                workspace_id=req["workspace_id"],
                tool_name=req["tool_name"],
                risk=req["risk"],
                rule_id=req["rule_id"],
                payload=req.get("payload"),
            )
        except Exception as exc:
            # 決定自体は成立している。ロールバックしない（§10.4）。
            raise ApiError(
                500,
                "AUDIT_FAILED",
                "decision recorded but audit outbox registration failed",
                retryable=True,
            ) from exc

        return JSONResponse(status_code=200, content={"decision": decision_value, "recorded": True})
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return _unexpected_error_response(exc)


# ===========================================================================
# PWA 認証: pair / ws-ticket / logout
# ===========================================================================


def _check_pairing_rate_limits(request: Request, *, now: float) -> None:
    ip = _client_ip(request)
    try:
        security.check_rate_limit(
            _LIVE_STORE, f"pair_ip:{ip}", limit=PAIRING_IP_LIMIT, window_seconds=PAIRING_IP_WINDOW, now=now
        )
        security.check_rate_limit(
            _LIVE_STORE, "pair_global", limit=PAIRING_GLOBAL_LIMIT, window_seconds=PAIRING_GLOBAL_WINDOW, now=now
        )
    except security.RateLimitExceededError as exc:
        try:
            audit.record_event(
                _LIVE_STORE,
                at=now,
                event="pairing_rate_limited",
                discriminator=f"{int(now // PAIRING_GLOBAL_WINDOW)}",
                approval_id="_pairing_",
                sub=ip,
                source="pwa",
                session_id="",
                workspace_id="",
                tool_name="",
                risk="",
                rule_id="",
                payload=None,
            )
        except Exception:
            logger.exception("failed to record pairing_rate_limited audit")
        raise ApiError(
            429,
            "RATE_LIMITED",
            "pairing rate limit exceeded",
            retryable=True,
            headers={"Retry-After": str(int(exc.retry_after_seconds) + 1)},
        ) from exc


@router.post("/api/pwa/pair")
async def pwa_pair(request: Request) -> JSONResponse:
    try:
        now = time.time()
        _check_pairing_rate_limits(request, now=now)

        body = await _read_json_body(request)
        code = _require_str(body, "code")
        device_name = _require_str(body, "device_name")

        # Phase1a spec §7.1b: bootstrap_done が無い間だけ HH_PAIRING_CODE
        # との照合を許す。存在すれば動的フロー(pairing_offer/pairing_used)
        # のみを試みる。
        bootstrap_key = "bootstrap_done"
        bootstrap_done = _LIVE_STORE.get(bootstrap_key) is not None

        if not bootstrap_done and security.constant_time_equals(code, config.pairing_code()):
            if not _LIVE_STORE.put_if_absent(bootstrap_key, {"used_at": now}):
                # 直前に他のリクエストが bootstrap_done を作った。動的
                # フローへフォールバック（下記 try へ）。
                pass
            else:
                cookie_value, sid = security.issue_pwa_session(
                    _LIVE_STORE, device_name=device_name, session_key=config.pwa_session_key().encode("utf-8"), now=now
                )
                csrf_token = security.issue_csrf_token(
                    sid, session_key=config.pwa_session_key().encode("utf-8"), now=now
                )
                response = JSONResponse(status_code=200, content={"csrf_token": csrf_token, "device_name": device_name})
                response.set_cookie(
                    PWA_COOKIE_NAME,
                    cookie_value,
                    httponly=True,
                    secure=True,
                    samesite="strict",
                    path="/",
                    max_age=security.PWA_SESSION_TTL_SECONDS,
                )
                return response

        try:
            security.verify_and_consume_pairing_code(_LIVE_STORE, code, now=now)
        except security.PairingCodeInvalidError as exc:
            raise ApiError(401, "PAIRING_INVALID", "pairing code invalid or expired", retryable=False) from exc
        except security.PairingCodeConsumedError as exc:
            raise ApiError(409, "PAIRING_CONSUMED", "pairing code already used", retryable=False) from exc

        cookie_value, sid = security.issue_pwa_session(
            _LIVE_STORE, device_name=device_name, session_key=config.pwa_session_key().encode("utf-8"), now=now
        )
        csrf_token = security.issue_csrf_token(sid, session_key=config.pwa_session_key().encode("utf-8"), now=now)
        response = JSONResponse(status_code=200, content={"csrf_token": csrf_token, "device_name": device_name})
        response.set_cookie(
            PWA_COOKIE_NAME,
            cookie_value,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/",
            max_age=security.PWA_SESSION_TTL_SECONDS,
        )
        return response
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return _unexpected_error_response(exc)


@router.post("/api/pwa/ws-ticket")
async def pwa_ws_ticket(request: Request) -> JSONResponse:
    try:
        identity = _verify_pwa(request)
        body = await _read_json_body(request)
        csrf = _require_str(body, "csrf")
        try:
            security.verify_origin(request.headers.get("origin"), _origin_header_expected(request))
            security.verify_csrf_token(
                identity.session_id, csrf, session_key=config.pwa_session_key().encode("utf-8")
            )
        except security.InvalidCsrfError as exc:
            raise ApiError(403, "CSRF_FAILED", "csrf or origin check failed", retryable=False) from exc

        # 手引き#1: issue_ws_ticket はストアへ何も書き込まない。
        ticket = security.issue_ws_ticket(identity.session_id, session_key=config.pwa_session_key().encode("utf-8"))
        return JSONResponse(
            status_code=200, content={"ticket": ticket, "expires_in": security.WS_TICKET_TTL_SECONDS}
        )
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return _unexpected_error_response(exc)


@router.post("/api/pwa/logout")
async def pwa_logout(request: Request) -> JSONResponse:
    try:
        identity = _verify_pwa(request)
        security.logout_pwa_session(_LIVE_STORE, identity.session_id)
        response = JSONResponse(status_code=200, content={"logged_out": True})
        response.delete_cookie(PWA_COOKIE_NAME, path="/")
        return response
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return _unexpected_error_response(exc)


# ===========================================================================
# WS /ws/approval
# ===========================================================================

_WS_PUSH_INTERVAL_SECONDS = 5.0


@router.websocket("/ws/approval")
async def ws_approval(websocket: WebSocket) -> None:
    """状態変化の push（最適化用途。正しさの前提にしない。親設計書 §4.3）。

    手引き#1: `consume_ws_ticket` を接続時に**一度だけ**呼ぶ。発行時に
    `wsticket:` を先出しで書かない（`issue_ws_ticket` はストアへ書かない
    ため、これは自然に満たされる）。
    """
    ticket = websocket.query_params.get("ticket")
    if not ticket:
        await websocket.close(code=4401)
        return
    try:
        security.consume_ws_ticket(
            _LIVE_STORE, ticket, session_key=config.pwa_session_key().encode("utf-8")
        )
    except security.SecurityError:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    try:
        while True:
            items = _list_pending_items(_LIVE_STORE, now=time.time())
            await websocket.send_json({"type": "pending", "items": items})
            await asyncio.sleep(_WS_PUSH_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("ws_approval loop terminated unexpectedly")
        return


__all__ = ["router", "status_of", "ApiError", "ApprovalStore"]
