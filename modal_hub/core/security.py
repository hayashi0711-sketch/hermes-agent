"""modal_hub/core/security.py — H-H Agent 資格情報検証・署名・CSRF・WS チケット・レート制限。

設計上の位置づけ:
    - 親設計書 docs/hh-agent/03_Architecture.md §5「セキュリティ」
    - 実装契約   docs/hh-agent/05_Phase1a_Spec.md §6（エージェントトークン）、
                 §7（PWA 認証: ペアリング / Cookie / CSRF / WS チケット）、
                 §8（バイパスファイルは対象外。hh_hooks 側の管轄）、
                 §11（資格情報の有効性は「肯定リスト」で判定する）

このモジュールの責務（docs/hh-agent/04_Task_Allocation.md の割当表どおり）:
    資格情報検証・署名・CSRF・WS チケット・レート制限。

このモジュールの責務**外**（他ファイル所有者の担当。ここには実装しない）:
    - 承認リクエストの状態機械・所有権照合（`routers/approval_gate.py`）
    - payload/preimage の canonical hash 計算（`core/canonical.py`）
    - risk 判定（`core/risk.py`）
    - Secret の実値読み込み（`core/config.py`）— このモジュールは鍵材料を
      すべて呼び出し側から bytes として受け取る。環境変数・Modal Secret を
      直接読まない。

== ストア依存の設計方針 ==

`modal_hub/core/store.py` は既に実装済み（本タスク着手時点で確認）。
`get(key) -> Optional[Any]` / `put_if_absent(key, value) -> bool` /
`delete(key) -> None` の 3 関数と、キー名の唯一の正となるキービルダー関数
（`agent_session_key` 等）を公開している。

本モジュールは:
    1. **キービルダー関数は `store` モジュールから直接 import して使う**
       （`from . import store as store_keys`）。キー prefix 文字列
       (`"agent_session:"` 等) をこのファイルで再定義すると、将来 store.py
       側の prefix が変わったときに黙って乖離し、
       「トークンは正しいのにストアと噛み合わない」という発見しづらい
       バグを生む。キービルダーは副作用のない純粋な文字列関数なので
       import して直接呼ぶのが安全。
    2. **実際の読み書き（get/put_if_absent/delete）は関数引数として注入
       される `store` オブジェクトを経由する**（`CredentialStore` Protocol）。
       `modal.Dict` への実アクセスをこのモジュールの import 時点で
       発生させないため、また本番の `core/store.py` 実装が無くても
       （あるいはテストダブルでも）このモジュール単体でユニットテスト
       できるようにするため。呼び出し側（routers/approval_gate.py 等）は
       `from modal_hub.core import store` した実物をそのまま渡せる
       （`store` モジュールは `get`/`put_if_absent`/`delete` を同名で
       公開しているため、そのまま `CredentialStore` を満たす）。

== フェイルクローズの方針（ユーザー指示 hard rule 4） ==

検証系関数（`verify_*` / `consume_*`）は成功時のみ Identity 値オブジェクトを
返し、それ以外は必ず例外を送出する。「無効を意味する None を返す」実装は
呼び出し側の判定漏れを誘発するため行わない。ストア層の障害
（`store.StoreError` 相当）は catch して握りつぶさず、そのまま呼び出し側へ
伝播させる（インフラ障害を「資格情報が無効」に読み替えない）。

== 定数時間比較（ユーザー指示 hard rule） ==

秘密由来の値（署名・HMAC ダイジェスト）の比較はすべて `hmac.compare_digest`
を使う。`==` は一度も使わない。`constant_time_equals()` として汎用ヘルパーも
公開する（他ファイルの所有者が秘密比較に使えるように）。

== このモジュールが独自に埋めた判断（BLOCKED にはせず、報告で開示する） ==

親設計書・Phase1a spec の文面だけでは一意に決まらない実装詳細が 3 点ある。
いずれも「安全側に倒す」「既存の確定仕様と矛盾しない」ことを条件に選んだ。
詳細は関数ごとの docstring と、実装完了報告の
「REQUESTS TO OTHER OWNERS / 設計判断の開示」を参照。

    1. トークンの HMAC 署名対象（"header+payload" の具体的なバイト列）。
    2. WS チケットの単回性・TTL・pwa_session 紐付けをどう表現するか
       （`modal.Dict` に update が無いため）。
    3. レート制限カウンタの実装方法（`modal.Dict` に increment が無いため）。

== scopes 拡張（Phase 1b、07_Phase1b_Spec.md §5） ==

エージェントトークンに `scopes: list[str]` を追加した。`POST /api/skills/publish`
は `publish` スコープを持つトークンのみが呼べる（`modal_hub/routers/skills.py`
が `require_scope(identity, "publish")` を呼ぶ）。

**後方互換**: Phase 1a で既に発行済みのトークン（payload にも `agent_session:`
レコードにも `scopes` フィールドが無い）を拒否してはならない。`payload`/
`record` どちらでも `scopes` キーが欠落している場合は
`_LEGACY_DEFAULT_SCOPES = {"request", "poll", "claim", "complete"}` を
補って扱う（`publish` は含まれない — 欠落時に安全側へ倒す）。新規発行時に
`issue_agent_token()` の `scopes` 引数を省略した場合も同じデフォルトを使う。
Distiller ローカルワーカー用トークンは `scopes=["publish"]` を明示して
発行すること（07_Phase1b_Spec.md §5「認証」）。

== ペアリングコードについて（03 と 05 の食い違い） ==

親設計書 §4.3 の表は単一キー `pairing:<code_hash>` を挙げているが、これは
Phase1a spec の「v1 から破棄した設計」表（05 冒頭）が明示的に破棄した設計
そのものである（「正規の初回ペアリングが必ず 409 で失敗する論理バグ」）。
Phase1a spec §7.1 はこれを `pairing_offer:<hash>` / `pairing_used:<hash>` の
二鍵方式に置き換えて確定させており、`core/store.py` の
`pairing_offer_key` / `pairing_used_key` も二鍵方式で実装済みである。
本モジュールは Phase1a spec §7.1（新しい・バグ修正済みの版）に従う。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import string
import time
import uuid
from dataclasses import dataclass
from typing import Any, FrozenSet, Optional, Protocol, Sequence

from . import store as store_keys

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: エージェントトークンのリテラル prefix（Phase1a spec §6.1）。
TOKEN_PREFIX = "hha1"

#: `source` クレームに許される値（親設計書 §4.3 の req: モデル、
#: Phase1a spec §6.1 の payload_json 例で使われている 2 値のみ）。
SOURCE_CLAUDE_CODE = "claude_code"
SOURCE_CLOUD_AGENT = "cloud_agent"
ALLOWED_SOURCES = (SOURCE_CLAUDE_CODE, SOURCE_CLOUD_AGENT)

#: エージェントトークンの有効期限（Phase1a spec §6.1「有効期限24時間」）。
AGENT_TOKEN_TTL_SECONDS = 24 * 3600

#: Phase1a で発行済みのトークン（payload/record に `scopes` フィールドが
#: 無い）を検証するときに補うデフォルト値（Phase1b, 07_Phase1b_Spec.md §5）。
_LEGACY_DEFAULT_SCOPES = frozenset({"request", "poll", "claim", "complete"})

#: `POST /api/skills/publish` を呼べるスコープ名（Phase1b §5）。
SCOPE_PUBLISH = "publish"

#: `GET /api/skills/quarantine` を呼べるスコープ名（S-08b。既存の
#: エージェントトークン + scopes 機構を再利用。新規 Secret は作らない）。
SCOPE_QUARANTINE_READ = "quarantine_read"

#: PWA セッション Cookie の有効期限（Phase1a spec §7.2「exp は発行から30日」）。
PWA_SESSION_TTL_SECONDS = 30 * 24 * 3600

#: CSRF トークンの有効期間（Phase1a spec §7.3「有効2時間」）。
CSRF_VALID_HOURS = 2

#: WS チケットの TTL（Phase1a spec §7.4「expires_in: 30」）。
WS_TICKET_TTL_SECONDS = 30

#: ペアリングオファーの TTL（Phase1a spec §7.1「有効5分」）。
PAIRING_OFFER_TTL_SECONDS = 300

#: ペアリングコードの桁数（Phase1a spec §7.1「8桁コード」）。
PAIRING_CODE_LENGTH = 8

_WORKSPACE_ID_RE_LEN = 64  # sha256hex


# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------


class SecurityError(Exception):
    """このモジュールが送出する例外の基底クラス。

    呼び出し側（routers）は、このモジュールの `verify_*`/`consume_*`
    系関数が例外を送出した場合、必ず 401/403/409/422 相当の拒否として
    扱うこと（ユーザー指示 hard rule 4「FAIL CLOSED everywhere」）。
    具体的な HTTP ステータス・エラーコードの割当は Phase1a spec の
    各エンドポイント仕様（§1）に従う責務であり、本モジュールは
    HTTP のことを一切知らない（意図的な設計。security.py は
    HTTP/FastAPI に依存しない）。
    """


class InvalidCredentialError(SecurityError):
    """資格情報の形式が壊れている、または署名検証に失敗した。"""


class ExpiredCredentialError(SecurityError):
    """資格情報の形式・署名は正しいが、有効期限を過ぎている。"""


class UnknownCredentialError(SecurityError):
    """署名は正しいが、対応する肯定リストレコードがストアに存在しない。

    Phase1a spec §11「資格情報の有効性は肯定リストで判定する」の核心:
    レコードが無い＝失効済みまたは未発行であり、絶対に有効扱いにしない。
    """


class ReplayedTicketError(SecurityError):
    """単回使用の資格情報（WS チケット等）が既に消費済みだった。"""


class InvalidCsrfError(SecurityError):
    """CSRF トークンが不正、または Origin ヘッダが一致しない。"""


class PairingCodeInvalidError(SecurityError):
    """ペアリングコードが存在しない、または期限切れ。"""


class PairingCodeConsumedError(SecurityError):
    """ペアリングコードは有効だったが、既に使用済みだった。"""


class InsufficientScopeError(SecurityError):
    """トークンは有効だが、要求されたスコープを持たない（Phase1b §5）。

    呼び出し側は 403 として扱う（401 ではない — 資格情報自体は有効なため）。
    """


class RateLimitExceededError(SecurityError):
    """レート制限を超過した。

    Attributes:
        retry_after_seconds: クライアントへ返す `Retry-After` の目安値
            （Phase1a spec §1.1「429 は retryable: true。Retry-After 秒を
            尊重する」）。
    """

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


# ---------------------------------------------------------------------------
# ストア依存性（注入。core/store.py の実物をそのまま渡せる）
# ---------------------------------------------------------------------------


class CredentialStore(Protocol):
    """本モジュールが要求するストア層の最小インターフェース。

    `modal_hub/core/store.py`（別所有者・本タスクの所有範囲外）が提供する
    `get` / `put_if_absent` / `delete` の 3 関数はこのシグネチャと一致して
    おり、呼び出し側は `from modal_hub.core import store` した
    **モジュールそのもの**をここへ渡せる（モジュールは属性アクセスで
    関数を解決できるため、構造的にこの Protocol を満たす）。

    テスト時はこの 3 メソッドだけを持つ軽量なダブルに差し替えられる。

    フェイルクローズ上の注意: 実装は「確定できない」場合に空 dict や
    False をごまかしで返してはならない。ストア層固有の障害
    （`store.StoreError` 相当）はそのまま例外として送出し、本モジュールは
    それを catch せずに呼び出し側へ伝播させる。
    """

    def get(self, key: str) -> Optional[dict[str, Any]]:
        """key が存在すればレコード(dict)を、存在しなければ None を返す。"""
        ...

    def put_if_absent(self, key: str, value: dict[str, Any]) -> bool:
        """key が存在しない場合のみ value を原子的に書き込み True を返す。
        既に存在する場合は書き込まず False を返す
        （`modal.Dict.put(key, value, skip_if_exists=True)` 相当）。"""
        ...

    def delete(self, key: str) -> None:
        """key を削除する（存在しなくても例外にしない、冪等）。"""
        ...


# ---------------------------------------------------------------------------
# 値オブジェクト
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentIdentity:
    """検証済みエージェントトークンから得られる身元情報。

    すべてのフィールドは HMAC 署名で保護された payload から取得したもの
    （リクエストボディの自己申告値ではない）。呼び出し側は
    `source` / `session_id` / `workspace_id` の権限判定に**必ずこの値**を
    使うこと。リクエストボディの同名フィールドは「検証にのみ使い、
    不一致なら 400」という Phase1a spec §6.1 の規則に従う（本モジュールは
    ボディとの突合せ自体は行わない。呼び出し側の責務）。
    """

    tid: str
    sub: str
    source: str
    session_id: str
    workspace_id: str
    issued_at: int
    expires_at: int
    scopes: FrozenSet[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class PwaIdentity:
    """検証済み PWA セッション Cookie から得られる身元情報。"""

    session_id: str
    device_name: str
    issued_at: float
    expires_at: float


@dataclass(frozen=True)
class WsTicketIdentity:
    """消費に成功した WS チケットから得られる身元情報。"""

    pwa_session_id: str
    consumed_at: float


# ---------------------------------------------------------------------------
# 内部ヘルパー: base64url / JSON
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    if not isinstance(segment, str) or not segment:
        raise InvalidCredentialError("empty base64url segment")
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise InvalidCredentialError("malformed base64url segment") from exc


def _encode_json_segment(obj: dict[str, Any]) -> str:
    """トークン/チケットの payload を決定的な JSON バイト列へ変換し base64url化する。

    ここでの決定性は「同じ入力から同じバイト列が作れること」のみが目的
    であり（HMAC の再計算に使うため）、Phase1a spec §3 の
    `canonical_json`（NFC 正規化・float 禁止など、payload_sha256 の
    比較安定性のための規約）とは別物。トークン payload の照合はハッシュ
    比較ではなく HMAC 署名の再計算で行うため、この用途では
    `sort_keys=True` の素朴な JSON エンコードで十分であり、
    `core/canonical.py`（別所有者・未実装の可能性がある）への依存を
    このモジュールに持ち込まない。
    """
    raw = json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _b64url_encode(raw)


def _decode_json_segment(segment_b64: str) -> dict[str, Any]:
    raw = _b64url_decode(segment_b64)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCredentialError("malformed JSON payload segment") from exc
    if not isinstance(obj, dict):
        raise InvalidCredentialError("payload segment must be a JSON object")
    return obj


# ---------------------------------------------------------------------------
# 汎用: 定数時間比較
# ---------------------------------------------------------------------------


def constant_time_equals(a: str, b: str) -> bool:
    """秘密由来の文字列比較はすべてこの関数を通す（内部で
    `hmac.compare_digest` を使用。長さの違いも一定時間で判定される）。

    他ファイルの所有者（`routers/approval_gate.py` の `lease_id` /
    `claim_attempt_id` 比較など）も秘密由来の値を比較する際はこの関数を
    再利用できる。
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _hmac_sha256(key: bytes, message: bytes) -> bytes:
    if not key:
        raise InvalidCredentialError("signing key must not be empty")
    return hmac.new(key, message, hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# エージェントトークン（Phase1a spec §6）
# ---------------------------------------------------------------------------
#
# 独自に埋めた判断 1: HMAC 署名対象の具体的なバイト列
# ---------------------------------------------------------------------------
# 契約上のトークン形式は文字列として一意に決まっている:
#
#     hha1.<base64url(payload_json)>.<base64url(hmac_sha256(KEY, header+payload))>
#
# これはトークン文字列そのものが「先頭2セグメントを dot 区切りで繋いだもの
# = 署名対象」であることを直接示している（JWT の
# `HMAC(header_b64 + "." + payload_b64)` と同型で、単にヘッダー部分が
# base64 化された JSON ではなくリテラル文字列 "hha1" である点だけが違う）。
# したがって署名対象 = ASCII バイト列 `"hha1." + base64url(payload_json)`。
# これは契約の文字列表記をそのまま読めば一意に定まる解釈であり、
# 推測ではなく「他に矛盾なく読める余地がない」ため BLOCKED にはしない。


def issue_agent_token(
    store: CredentialStore,
    *,
    sub: str,
    source: str,
    session_id: str,
    workspace_id: str,
    signing_key: bytes,
    scopes: Optional[Sequence[str]] = None,
    now: Optional[float] = None,
    ttl_seconds: int = AGENT_TOKEN_TTL_SECONDS,
) -> str:
    """新しいエージェントトークンを発行する（`hh auth login` の Hub 側処理）。

    `agent_session:<tid>` レコードを肯定リストストアへ書き込んでから
    トークン文字列を返す。**発行には常に「現行鍵」を渡すこと**
    （Phase1a spec §6.2「発行は新しい方のみ」）。`signing_key_prev` を
    受け取る引数は存在しない — 呼び出し側が誤って旧鍵で発行できないよう
    シグネチャ自体で防ぐ。

    Args:
        scopes: 付与するスコープ名の集合。省略した場合は
            `_LEGACY_DEFAULT_SCOPES`（`{"request","poll","claim","complete"}`）
            を使う（Phase1a 互換。`publish` は含まれない）。Distiller
            ローカルワーカー用トークンは呼び出し側が明示的に
            `scopes=["publish"]` を渡すこと（07_Phase1b_Spec.md §5）。

    Raises:
        ValueError: `source` が許可された値以外、または `workspace_id` が
            sha256hex 形式でない（呼び出し側のプログラミングエラー）。
        SecurityError: `tid`（UUID4）がストアに既に存在した場合
            （事実上あり得ないが、フェイルクローズのため黙って上書きせず
            例外にする）。
    """
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"unsupported source: {source!r}")
    if not sub:
        raise ValueError("sub must not be empty")
    if not session_id:
        raise ValueError("session_id must not be empty")
    if len(workspace_id) != _WORKSPACE_ID_RE_LEN or not _is_hex(workspace_id):
        raise ValueError("workspace_id must be a 64-hex sha256 digest")

    resolved_scopes = (
        sorted(_LEGACY_DEFAULT_SCOPES) if scopes is None else sorted(set(scopes))
    )
    if scopes is not None and not all(isinstance(s, str) and s for s in scopes):
        raise ValueError("scopes must be a sequence of non-empty strings")

    now = time.time() if now is None else now
    iat = int(now)
    exp = iat + ttl_seconds
    tid = str(uuid.uuid4())

    payload = {
        "tid": tid,
        "sub": sub,
        "source": source,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "iat": iat,
        "exp": exp,
        "scopes": resolved_scopes,
    }
    payload_b64 = _encode_json_segment(payload)
    signing_input = f"{TOKEN_PREFIX}.{payload_b64}".encode("ascii")
    sig_b64 = _b64url_encode(_hmac_sha256(signing_key, signing_input))
    token = f"{TOKEN_PREFIX}.{payload_b64}.{sig_b64}"

    record = {
        "sub": sub,
        "source": source,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "issued_at": iat,
        "exp": exp,
        "scopes": resolved_scopes,
    }
    if not store.put_if_absent(store_keys.agent_session_key(tid), record):
        raise SecurityError(
            "agent_session tid collision on issuance (uuid4 collision)"
        )
    return token


def verify_agent_token(
    store: CredentialStore,
    token: str,
    *,
    signing_key: bytes,
    signing_key_prev: Optional[bytes] = None,
    now: Optional[float] = None,
) -> AgentIdentity:
    """エージェントトークンを検証する。

    検証手順（Phase1a spec §11 のとおり。順序を変えない）:
        1. 形式検証（3 セグメント、先頭が厳密に "hha1"）。
        2. 署名検証: `signing_key`（現行鍵）で試し、失敗したら
           `signing_key_prev`（あれば）でも試す（鍵ローテーション対応。
           Phase1a spec §6.2「検証は両方で試す」）。
        3. payload のスキーマ検証（欠落・型不正は InvalidCredentialError）。
        4. **肯定リスト**: `agent_session:<tid>` がストアに存在することを
           確認する。存在しなければ 401 相当
           （`UnknownCredentialError`）— 「失効レコードが無い」を
           「有効」と読み替えない（墓標方式の否定）。
        5. ストアレコードの `sub`/`source`/`session_id`/`workspace_id` が
           トークン payload と一致することを確認する（発行時に同時に
           書いた 2 つのコピーが食い違うのは実装バグの兆候であり、
           フェイルクローズのため InvalidCredentialError にする）。
        6. **ストアレコードの `exp`** が現在時刻を過ぎていないことを
           確認する（`ExpiredCredentialError`）。トークン payload 自身の
           `exp` は署名で保護されているため改ざんは検出できるが、
           失効の一次情報源は常にストア側とする
           （Phase1a spec §11「有効性は record の存在と exp で判定する」）。

    Returns:
        AgentIdentity。`source`/`session_id`/`workspace_id` は
        **必ずこの戻り値を使う**こと。リクエストボディの同名フィールドを
        信用しない（ユーザー指示 hard rule）。

    Raises:
        InvalidCredentialError / ExpiredCredentialError /
        UnknownCredentialError: 検証失敗。呼び出し側は 401 として扱う。
    """
    now = time.time() if now is None else now

    if not isinstance(token, str) or token.count(".") != 2:
        raise InvalidCredentialError("malformed agent token")
    header, payload_b64, sig_b64 = token.split(".", 2)
    if header != TOKEN_PREFIX:
        raise InvalidCredentialError("unsupported agent token prefix")

    signing_input = f"{header}.{payload_b64}".encode("ascii")
    provided_sig = _b64url_decode(sig_b64)

    candidate_keys = [k for k in (signing_key, signing_key_prev) if k]
    if not candidate_keys:
        raise InvalidCredentialError("no agent token signing key configured")
    if not any(
        hmac.compare_digest(_hmac_sha256(k, signing_input), provided_sig)
        for k in candidate_keys
    ):
        raise InvalidCredentialError("agent token signature verification failed")

    payload = _decode_json_segment(payload_b64)
    identity = _agent_identity_from_payload(payload)

    record = store.get(store_keys.agent_session_key(identity.tid))
    if record is None:
        raise UnknownCredentialError(
            "agent session not found (revoked or never issued)"
        )
    _require_matching_agent_record(record, identity)

    record_exp = record.get("exp")
    if not isinstance(record_exp, (int, float)):
        raise InvalidCredentialError("malformed agent_session record: exp")
    if now >= record_exp:
        raise ExpiredCredentialError("agent session expired")

    return identity


def revoke_agent_session(store: CredentialStore, tid: str) -> None:
    """`hh auth revoke`: `agent_session:<tid>` を削除する（失効の唯一の方法）。

    Phase1a spec §11「失効＝キーの削除」。「revoked:」墓標は作らない。
    """
    store.delete(store_keys.agent_session_key(tid))


def _parse_scopes_field(container: dict[str, Any], *, context: str) -> FrozenSet[str]:
    """`container["scopes"]` を解釈する。**キー自体が無い**場合のみ Phase1a
    互換のレガシーデフォルトを返す（本ファイル docstring「scopes 拡張」参照）。

    キーが存在するが値が不正（`null`・非リスト・非文字列要素を含む）な
    場合はフェイルクローズで例外にする — `.get("scopes")` で `None` を
    受け取ると「キー無し」と「値が明示的に `null`」を区別できず、後者は
    レコード改ざんの兆候であってもレガシーデフォルトへ静かに読み替えて
    しまう（2026-08-11 Codex レビュー指摘・Low）。
    """
    if "scopes" not in container:
        return frozenset(_LEGACY_DEFAULT_SCOPES)
    raw = container["scopes"]
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        raise InvalidCredentialError(f"invalid scopes in {context}")
    return frozenset(raw)


def _agent_identity_from_payload(payload: dict[str, Any]) -> AgentIdentity:
    required = ("tid", "sub", "source", "session_id", "workspace_id", "iat", "exp")
    missing = [k for k in required if k not in payload]
    if missing:
        raise InvalidCredentialError("agent token payload missing required fields")

    tid, sub, source, session_id, workspace_id, iat, exp = (
        payload[k] for k in required
    )
    if not isinstance(tid, str) or not tid:
        raise InvalidCredentialError("invalid tid")
    if not isinstance(sub, str) or not sub:
        raise InvalidCredentialError("invalid sub")
    if source not in ALLOWED_SOURCES:
        raise InvalidCredentialError("invalid source")
    if not isinstance(session_id, str) or not session_id:
        raise InvalidCredentialError("invalid session_id")
    if (
        not isinstance(workspace_id, str)
        or len(workspace_id) != _WORKSPACE_ID_RE_LEN
        or not _is_hex(workspace_id)
    ):
        raise InvalidCredentialError("invalid workspace_id")
    if not isinstance(iat, (int, float)) or not isinstance(exp, (int, float)):
        raise InvalidCredentialError("invalid iat/exp")

    scopes = _parse_scopes_field(payload, context="token payload")

    return AgentIdentity(
        tid=tid,
        sub=sub,
        source=source,
        session_id=session_id,
        workspace_id=workspace_id,
        issued_at=int(iat),
        expires_at=int(exp),
        scopes=scopes,
    )


def _require_matching_agent_record(
    record: dict[str, Any], identity: AgentIdentity
) -> None:
    for field, expected in (
        ("sub", identity.sub),
        ("source", identity.source),
        ("session_id", identity.session_id),
        ("workspace_id", identity.workspace_id),
    ):
        actual = record.get(field)
        if not isinstance(actual, str) or actual != expected:
            raise InvalidCredentialError(
                "agent_session record does not match token payload"
            )

    record_scopes = _parse_scopes_field(record, context="agent_session record")
    if record_scopes != identity.scopes:
        raise InvalidCredentialError(
            "agent_session record scopes do not match token payload"
        )


def require_scope(identity: AgentIdentity, scope: str) -> None:
    """`identity` が `scope` を持つことを確認する。持たなければ
    `InsufficientScopeError`（呼び出し側は 403 として扱う）。

    ルータ側（`routers/skills.py` の `publish` エンドポイント等）は
    `verify_agent_token()` の後にこの関数を呼ぶこと。
    """
    if not identity.has_scope(scope):
        raise InsufficientScopeError(f"token lacks required scope: {scope!r}")


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return all(c in string.hexdigits for c in value)


# ---------------------------------------------------------------------------
# PWA セッション（Cookie）（Phase1a spec §7.2、§11）
# ---------------------------------------------------------------------------


def issue_pwa_session(
    store: CredentialStore,
    *,
    device_name: str,
    session_key: bytes,
    now: Optional[float] = None,
    ttl_seconds: int = PWA_SESSION_TTL_SECONDS,
) -> tuple[str, str]:
    """新しい PWA セッションを作成する（ペアリング成功時に呼ぶ）。

    `pwa_session:<sid>` を肯定リストへ書き込んでから Cookie 値を返す。

    Returns:
        `(cookie_value, session_id)`。`cookie_value` は
        ``Set-Cookie: hh_pwa=<cookie_value>; HttpOnly; Secure; SameSite=Strict;
        Path=/; Max-Age=2592000`` としてそのまま使う。**`Max-Age` は
        ブラウザへのヒントに過ぎず、実際の有効性判定は常にストア側の
        `exp` で行われる**（cookie_value 自体には有効期限を埋め込まない。
        Phase1a spec §11「Cookie の Max-Age はブラウザ側のヒントにすぎず、
        盗まれた Cookie 値の再送を止められない」）。
    """
    if not device_name:
        raise ValueError("device_name must not be empty")

    now = time.time() if now is None else now
    sid = str(uuid.uuid4())
    sig = _hmac_sha256(session_key, sid.encode("ascii"))
    cookie_value = f"{sid}.{_b64url_encode(sig)}"

    record = {
        "device_name": device_name,
        "issued_at": now,
        "exp": now + ttl_seconds,
    }
    if not store.put_if_absent(store_keys.pwa_session_key(sid), record):
        raise SecurityError("pwa_session sid collision on issuance (uuid4 collision)")
    return cookie_value, sid


def verify_pwa_session(
    store: CredentialStore,
    cookie_value: str,
    *,
    session_key: bytes,
    now: Optional[float] = None,
) -> PwaIdentity:
    """PWA セッション Cookie を検証する。

    署名検証 → 肯定リスト（`pwa_session:<sid>`）存在確認 → ストア側 `exp`
    確認、の順（Phase1a spec §11）。PWA Cookie の署名鍵ローテーションは
    仕様に規定が無いため単一鍵のみで検証する（エージェントトークンの
    ような `_prev` 引数は持たない）。

    Raises:
        InvalidCredentialError / ExpiredCredentialError /
        UnknownCredentialError
    """
    now = time.time() if now is None else now

    if not isinstance(cookie_value, str) or cookie_value.count(".") != 1:
        raise InvalidCredentialError("malformed pwa session cookie")
    sid, sig_b64 = cookie_value.split(".", 1)
    if not sid:
        raise InvalidCredentialError("malformed pwa session cookie")

    expected = _hmac_sha256(session_key, sid.encode("ascii"))
    provided = _b64url_decode(sig_b64)
    if not hmac.compare_digest(expected, provided):
        raise InvalidCredentialError("pwa session cookie signature mismatch")

    record = store.get(store_keys.pwa_session_key(sid))
    if record is None:
        raise UnknownCredentialError(
            "pwa session not found (logged out or never issued)"
        )

    exp = record.get("exp")
    device_name = record.get("device_name")
    issued_at = record.get("issued_at")
    if not isinstance(exp, (int, float)):
        raise InvalidCredentialError("malformed pwa_session record: exp")
    if not isinstance(device_name, str):
        raise InvalidCredentialError("malformed pwa_session record: device_name")
    if not isinstance(issued_at, (int, float)):
        raise InvalidCredentialError("malformed pwa_session record: issued_at")
    if now >= exp:
        raise ExpiredCredentialError("pwa session expired")

    return PwaIdentity(
        session_id=sid, device_name=device_name, issued_at=issued_at, expires_at=exp
    )


def logout_pwa_session(store: CredentialStore, session_id: str) -> None:
    """`POST /api/pwa/logout`: `pwa_session:<sid>` を削除する（墓標を立てない）。"""
    store.delete(store_keys.pwa_session_key(session_id))


# ---------------------------------------------------------------------------
# CSRF（Phase1a spec §7.3）
# ---------------------------------------------------------------------------


def issue_csrf_token(
    pwa_session_id: str, *, session_key: bytes, now: Optional[float] = None
) -> str:
    """`csrf_token = hmac(HH_PWA_SESSION_KEY, pwa_session_id + issued_hour)`。

    ペアリング成功時と `GET /api/approval/pending` の応答に含める
    （Cookie とは別経路。Phase1a spec §7.3）。
    """
    now = time.time() if now is None else now
    hour_bucket = int(now // 3600)
    return _csrf_digest(pwa_session_id, hour_bucket, session_key)


def verify_csrf_token(
    pwa_session_id: str,
    csrf_token: str,
    *,
    session_key: bytes,
    now: Optional[float] = None,
) -> None:
    """CSRF トークンを検証する。有効2時間。

    トークン自体に有効期限は埋め込まれていない（`issued_hour` の値は
    トークンから読み出せない、ハッシュに焼き込まれているだけ）ため、
    検証側は「現在の時間バケット」と「直前の時間バケット」の 2 通りで
    ダイジェストを再計算し、どちらかと一致すれば許可するローリング
    ウィンドウ方式で「有効2時間」を実現する（実際の有効期間は
    発行タイミングにより 1〜2 時間の幅を持つ。埋め込み exp が無い
    設計上、この幅は避けられない）。

    Raises:
        InvalidCsrfError: 不一致または欠落。
    """
    if not isinstance(csrf_token, str) or not csrf_token:
        raise InvalidCsrfError("missing csrf token")

    now = time.time() if now is None else now
    current_bucket = int(now // 3600)
    for bucket in (current_bucket, current_bucket - 1):
        expected = _csrf_digest(pwa_session_id, bucket, session_key)
        if hmac.compare_digest(expected.encode("ascii"), csrf_token.encode("ascii")):
            return
    raise InvalidCsrfError("csrf token invalid or expired")


def _csrf_digest(pwa_session_id: str, hour_bucket: int, session_key: bytes) -> str:
    message = f"{pwa_session_id}{hour_bucket}".encode("utf-8")
    return _hmac_sha256(session_key, message).hex()


def verify_origin(origin_header: Optional[str], expected_origin: str) -> None:
    """`Origin` ヘッダが Hub 自身の Origin と完全一致することを確認する。

    `Referer` は使わない（Phase1a spec §7.3「Referer は使わない」）。
    `POST /api/approval/respond` はこの関数と `verify_csrf_token` の
    **両方**を通すこと（どちらか片方では CSRF 対策として不十分）。

    Raises:
        InvalidCsrfError: ヘッダが無い、または不一致。
    """
    if not origin_header or origin_header != expected_origin:
        raise InvalidCsrfError("origin header missing or mismatched")


# ---------------------------------------------------------------------------
# WS チケット（Phase1a spec §7.4）
# ---------------------------------------------------------------------------
#
# 独自に埋めた判断 2: WS チケットの単回性・TTL・sid 紐付けの表現方法
# ---------------------------------------------------------------------------
# 親設計書 §4.3 は `wsticket:<ticket_id>` を「単回 WS チケットの使用済み
# マーク（put(skip_if_exists=True) で消費）」とだけ定義しており、
# それ以外のキーは存在しない。一方 `core/store.py` は update/increment を
# 一切公開しない（put_if_absent のみ）。つまり:
#
#   - `wsticket:<ticket>` は「消費された」ことだけを記録する 1 個のキー。
#   - 発行時点ではこのキーへ何も書けない（書けば「消費」と区別できない）。
#   - にもかかわらず「TTL 30秒」「どの pwa_session_id に紐づくか」は
#     接続時に検証できなければならない。
#
# これらを両立させる唯一の方法は、**チケット文字列自体に
# sid・発行時刻・有効期限を HMAC 署名で埋め込む**（自己記述式・
# ステートレスな検証）ことである。ストアの `wsticket:<ticket>` キーは
# 文字どおり「使用済みマーク」としてのみ使い、単回性の担保に専念させる。
# これは Phase1a spec が既に agent token や csrf_token で採用している
# 「署名付き自己記述トークン」という同じパターンをもう一箇所へ適用した
# だけであり、恣意的な発明ではない。
#
# エンドポイント応答例の `"ticket": "<uuid4>"` は opaque な一意文字列と
# いう意味のプレースホルダとして扱い、実際のバイト列表現（dot 区切りの
# base64url 2 セグメント）は当関数の内部実装詳細とする。
#
# ※ この設計判断は REQUESTS TO OTHER OWNERS として報告する
#   （`routers/approval_gate.py` 側で `/api/pwa/ws-ticket` /
#   `/ws/approval` を実装する担当者が前提を共有できるように）。


def issue_ws_ticket(
    pwa_session_id: str,
    *,
    session_key: bytes,
    now: Optional[float] = None,
    ttl_seconds: int = WS_TICKET_TTL_SECONDS,
) -> str:
    """単回使用 WS チケットを発行する。ストアへは何も書き込まない。

    戻り値は `WS /ws/approval?ticket=<...>` にそのまま使う不透明な文字列。
    """
    now = time.time() if now is None else now
    payload = {
        "sid": pwa_session_id,
        "iat": int(now),
        "exp": int(now) + ttl_seconds,
    }
    payload_b64 = _encode_json_segment(payload)
    sig = _hmac_sha256(session_key, payload_b64.encode("ascii"))
    return f"{payload_b64}.{_b64url_encode(sig)}"


def consume_ws_ticket(
    store: CredentialStore,
    ticket: str,
    *,
    session_key: bytes,
    now: Optional[float] = None,
) -> WsTicketIdentity:
    """WS 接続確立時に一度だけ呼ぶ。署名検証 → TTL 検証 → 単回消費の順。

    2 回目の呼び出し（同一チケットでの再接続試行）は
    `ReplayedTicketError` になる（Phase1a spec §7.4 手順3「既存なら
    接続を拒否」）。

    Raises:
        InvalidCredentialError: 形式不正・署名不一致。
        ExpiredCredentialError: TTL 超過。
        ReplayedTicketError: 既に消費済み。
    """
    now = time.time() if now is None else now

    if not isinstance(ticket, str) or ticket.count(".") != 1:
        raise InvalidCredentialError("malformed ws ticket")
    payload_b64, sig_b64 = ticket.split(".", 1)

    expected = _hmac_sha256(session_key, payload_b64.encode("ascii"))
    provided = _b64url_decode(sig_b64)
    if not hmac.compare_digest(expected, provided):
        raise InvalidCredentialError("ws ticket signature mismatch")

    payload = _decode_json_segment(payload_b64)
    sid = payload.get("sid")
    exp = payload.get("exp")
    if not isinstance(sid, str) or not sid:
        raise InvalidCredentialError("invalid ws ticket sid")
    if not isinstance(exp, (int, float)):
        raise InvalidCredentialError("invalid ws ticket exp")
    if now > exp:
        raise ExpiredCredentialError("ws ticket expired")

    if not store.put_if_absent(
        store_keys.ws_ticket_key(ticket), {"sid": sid, "consumed_at": now}
    ):
        raise ReplayedTicketError("ws ticket already used")

    return WsTicketIdentity(pwa_session_id=sid, consumed_at=now)


# ---------------------------------------------------------------------------
# ペアリング（Phase1a spec §7.1）
# ---------------------------------------------------------------------------

_PAIRING_CODE_ALPHABET = string.digits


def hash_pairing_code(code: str) -> str:
    """ペアリングコードの sha256 hex を返す（ストアキーの一部に使う）。

    コード自体はログに残さない — 呼び出し側もこの点を守ること。
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def create_pairing_offer(
    store: CredentialStore,
    *,
    now: Optional[float] = None,
    ttl_seconds: int = PAIRING_OFFER_TTL_SECONDS,
) -> str:
    """`hh pwa pair`: 8桁のペアリングコードを新規発行する。

    `pairing_offer:<sha256(code)>` を作成する（有効5分）。コードは
    端末の画面にのみ表示する想定であり、この関数はコードをそのまま
    返す以外に永続化しない。

    Returns:
        8桁の数字コード文字列。

    Raises:
        SecurityError: sha256 衝突（事実上あり得ない）。
    """
    now = time.time() if now is None else now
    code = "".join(
        secrets.choice(_PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH)
    )
    code_hash = hash_pairing_code(code)
    record = {"created_at": now, "exp": now + ttl_seconds}
    if not store.put_if_absent(store_keys.pairing_offer_key(code_hash), record):
        raise SecurityError("pairing_offer hash collision on issuance")
    return code


def verify_and_consume_pairing_code(
    store: CredentialStore, code: str, *, now: Optional[float] = None
) -> None:
    """`POST /api/pwa/pair`: ペアリングコードを検証し単回消費する。

    手順（BUG-4 修正版。判定順序を意図的に変更している — 理由は下記）:
        a. まず `pairing_used:<hash>` の**存在だけを覗く**（`get`。まだ
           `put_if_absent` はしない）。存在すれば即座に
           `PairingCodeConsumedError`。
        b. `pairing_offer:<hash>` が存在し `exp` 内であることを確認。
           無ければ／期限切れなら `PairingCodeInvalidError`。
        c. `pairing_used:<hash>` を `put_if_absent` で原子的に確保する。
           ここで失敗する（＝手順 a の後、ここまでの間に他リクエストが
           先に消費した）場合も `PairingCodeConsumedError`。
        d. 消費に成功したら `pairing_offer:<hash>` を削除する
           （残っていても `pairing_used:` があるので再利用はできないが、
           仕様の手順4で明示的に削除するとされているため実施する）。

    **BUG-4（修正前の問題）**: 旧実装は「offer 存在確認 → used 消費」の順で
    判定していた。手順 d で成功時に `pairing_offer:` を削除するため、
    2 回目（コード再利用）の試行では offer が既に無く、手順 a 相当の
    判定に到達する前に `PairingCodeInvalidError`（401 `PAIRING_INVALID`）
    へ落ちてしまい、`PairingCodeConsumedError`（409 `PAIRING_CONSUMED`）は
    事実上の競合時にしか発生し得なかった。06_PWA_Design.md §3.4 は両者に
    異なる日本語文言を割り当てているため、コードを使い回したユーザーへ
    「使用済みです」ではなく「コードが違う/期限切れです」という誤った
    表示になっていた。

    **安全性は弱めていない**: 未知のコード・期限切れのコードは、この
    修正後も両方とも同じ `PairingCodeInvalidError` に落ちる（手順 a では
    `pairing_used:` が無いため通過し、手順 b で offer 不在/期限切れとして
    弾かれる）。したがって「未知」と「期限切れ」は依然として区別できない
    （攻撃者に情報を与えない）。単回消費の原子性も、実際の確保は依然
    `put_if_absent`（手順 c）が担っており、手順 a の `get` は事前チェック
    （早期リターンの最適化）に過ぎない。真の競合（手順 a と c の間に別
    リクエストが割り込む）は手順 c 側で捕捉され、同じ
    `PairingCodeConsumedError` になる。呼び出し側のレート制限
    （spec §7.1 の IP 10回/10分・全体 30回/10分）はこの関数の外側で
    適用される経路であり、判定順序の変更による影響を受けない。

    この関数はコード検証のみを行う。成功後の `pwa_session:` 発行は
    呼び出し側が `issue_pwa_session()` を別途呼ぶ。

    Raises:
        PairingCodeInvalidError: オファーが存在しない、または期限切れ。
        PairingCodeConsumedError: 既に使用済み（判定時点、または
            判定と消費の間に他リクエストが先に消費した場合の両方）。
    """
    now = time.time() if now is None else now
    code_hash = hash_pairing_code(code)
    used_key = store_keys.pairing_used_key(code_hash)

    if store.get(used_key) is not None:
        raise PairingCodeConsumedError("pairing code already used")

    offer = store.get(store_keys.pairing_offer_key(code_hash))
    if offer is None:
        raise PairingCodeInvalidError("pairing code not found or already deleted")
    exp = offer.get("exp")
    if not isinstance(exp, (int, float)) or now >= exp:
        raise PairingCodeInvalidError("pairing code expired")

    if not store.put_if_absent(used_key, {"used_at": now}):
        raise PairingCodeConsumedError("pairing code already used")

    store.delete(store_keys.pairing_offer_key(code_hash))


# ---------------------------------------------------------------------------
# レート制限（親設計書 §5.1、Phase1a spec §7.1）
# ---------------------------------------------------------------------------
#
# 独自に埋めた判断 3: カウンタの実装方法
# ---------------------------------------------------------------------------
# 親設計書 §4.3 は `rate:<subject>:<hour_bucket>` を「レート制限カウンタ」
# と呼ぶが、`modal.Dict` に increment/update は存在しない
# （`core/store.py` が公開するのは `put_if_absent` のみ）。単一キーへの
# 書き込みだけで「N 回目の要求か」を判定することはできない。
#
# ここでは create-once セマンティクスだけで有限の上限を検査できる
# 唯一の方法として、**バケット内にスロット番号付きの子キーを 1..limit
# の範囲で順に put_if_absent していき、空きスロットを取れれば許可、
# limit まで全て埋まっていたら拒否**という方式を採る
# （`rate:<subject>:<bucket>:<slot>`）。`store.rate_key(subject, bucket)`
# の `hour_bucket` 引数へ `"<bucket>:<slot>"` という合成文字列を渡すことで、
# store.py 側のキービルダーをそのまま再利用しながら実現している
# （store.py へ変更を加える必要が無い）。
#
# 上限判定に必要な store 呼び出しは最悪 `limit` 回（HIGH 承認要求は
# 20件/時、ペアリング総当たりは 10〜30件/10分という低頻度パスのみで
# 使われるため許容範囲）。


def check_rate_limit(
    store: CredentialStore,
    subject: str,
    *,
    limit: int,
    window_seconds: int,
    now: Optional[float] = None,
) -> None:
    """`subject` の `window_seconds` 秒あたりの利用回数が `limit` を
    超えていないか確認し、超えていなければ 1 枠を消費する。

    汎用関数。呼び出し例:
        - エージェントトークンごとの HIGH 承認要求 20件/時
          (`subject=token.sub`, `limit=20`, `window_seconds=3600`)
        - ペアリングコード試行の IP ごと 10回/10分
          (`subject=f"pair_ip:{ip}"`, `limit=10`, `window_seconds=600`)
        - ペアリングコード試行の全体 30回/10分
          (`subject="pair_global"`, `limit=30`, `window_seconds=600`)

    Raises:
        ValueError: `limit` が正の整数でない。
        RateLimitExceededError: 上限超過。`retry_after_seconds` に
            次のウィンドウ境界までの秒数を持つ。
    """
    if limit <= 0:
        raise ValueError("limit must be a positive integer")
    now = time.time() if now is None else now
    bucket = int(now // window_seconds)

    for slot in range(1, limit + 1):
        key = store_keys.rate_key(subject, f"{bucket}:{slot}")
        if store.put_if_absent(key, {"claimed_at": now}):
            return

    retry_after = window_seconds - (now % window_seconds)
    raise RateLimitExceededError(
        f"rate limit exceeded ({limit} per {window_seconds}s) for subject",
        retry_after_seconds=retry_after,
    )


__all__ = [
    # 定数
    "TOKEN_PREFIX",
    "SOURCE_CLAUDE_CODE",
    "SOURCE_CLOUD_AGENT",
    "ALLOWED_SOURCES",
    "AGENT_TOKEN_TTL_SECONDS",
    "PWA_SESSION_TTL_SECONDS",
    "CSRF_VALID_HOURS",
    "WS_TICKET_TTL_SECONDS",
    "PAIRING_OFFER_TTL_SECONDS",
    "PAIRING_CODE_LENGTH",
    "SCOPE_PUBLISH",
    "SCOPE_QUARANTINE_READ",
    # 例外
    "SecurityError",
    "InvalidCredentialError",
    "ExpiredCredentialError",
    "UnknownCredentialError",
    "ReplayedTicketError",
    "InvalidCsrfError",
    "PairingCodeInvalidError",
    "PairingCodeConsumedError",
    "RateLimitExceededError",
    "InsufficientScopeError",
    # ストア依存性
    "CredentialStore",
    # 値オブジェクト
    "AgentIdentity",
    "PwaIdentity",
    "WsTicketIdentity",
    # 汎用
    "constant_time_equals",
    # エージェントトークン
    "issue_agent_token",
    "verify_agent_token",
    "revoke_agent_session",
    "require_scope",
    # PWA セッション
    "issue_pwa_session",
    "verify_pwa_session",
    "logout_pwa_session",
    # CSRF
    "issue_csrf_token",
    "verify_csrf_token",
    "verify_origin",
    # WS チケット
    "issue_ws_ticket",
    "consume_ws_ticket",
    # ペアリング
    "hash_pairing_code",
    "create_pairing_offer",
    "verify_and_consume_pairing_code",
    # レート制限
    "check_rate_limit",
]
