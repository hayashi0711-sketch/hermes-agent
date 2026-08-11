"""modal_hub/routers/skills.py — POST /api/skills/publish（Phase 1b、安全性クリティカル）。

設計上の位置づけ:
    - 実装契約   docs/hh-agent/07_Phase1b_Spec.md §5
    - 担当表     docs/hh-agent/04_Task_Allocation.md Phase 1b 表
      「新規 POST /api/skills/publish。publish スコープのトークン検証」

**「読み取り専用の複製」ではなく認証済みの永続書き込みエンドポイントである。
承認ゲートと同等の警戒で実装する**（§5 冒頭）。`routers/approval_gate.py`
（同一所有者）が確立したパターン（`ApiError`/`error_response` による統一
エラー封筒、DI 可能な Store Protocol + `_LiveXxxStore` 本番アダプタ）を
そのまま踏襲する。`approval_gate.ApiError`/`error_response` は例外クラス・
レスポンス整形という完全に汎用的な部品のため import して再利用し、
同じものをこのファイルへ手で複製しない。

== 検証順序は §5 の番号どおり（変更しない） ==

    1. `name` の正規表現。
    2. `skill_md` の frontmatter `name` とリクエストの `name` が一致。
    3. `content_sha256` が実際の本文ダイジェストと一致。
    4. `core/redact.py` を適用して差分が出たら拒否。

その後に「create-or-match-only」の書き込みセマンティクス（新規/不変/409）。

== このファイルが独自に決めた設計判断 ==

1. **`content_sha256`/`payload` は秘密ではないため `hmac.compare_digest`
   を使わない。** `security.py` の「秘密由来の値は必ず定数時間比較」は
   HMAC 署名・トークンなど攻撃者がタイミングから推測しうる秘密の比較を
   指す。SKILL.md 本文の sha256 はクライアントが公開できる完全性チェック
   であり、通常の `==` 比較で十分（タイミング攻撃の対象にならない）。
2. **監査書き込み（`audit.record_skill_publish_event`）の失敗は
   このファイル内で個別の `AUDIT_FAILED` コードにしない。** 例外を
   キャッチせずそのまま外側の `try/except Exception` へ伝播させ、汎用
   500 `INTERNAL_ERROR` になる。`skill_published` の場合、この時点で
   Volume への SKILL.md 書き込み自体は既に成功しているため、クライアントは
   500 を見てリトライしても create-or-match-only の「不変」パス（200
   `unchanged: true`）に着地するだけで、二重書き込みや不整合は起きない。
   05_Phase1a_Spec.md の `AUDIT_FAILED`（承認ゲート固有のコード）を
   ここへ流用する必要性が無いため、独自コードを増やさなかった。
3. **`name` の正規表現は `services/skill_quarantine.py`（同一所有者）の
   `NAME_RE` をそのまま import して使う。** promote 時（ローカル）と
   publish 時（リモート）で名前の許容範囲が食い違うと、「ローカルの
   隔離領域には保存できたのに publish は拒否される」ような分かりにくい
   不整合を生むため、正規表現の定義箇所を1つに保つ。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Optional, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from modal_hub.core import config
from modal_hub.core import redact
from modal_hub.core import security
from modal_hub.core import store
from modal_hub.routers import approval_gate
from modal_hub.services import audit
from modal_hub.services import skill_quarantine

logger = logging.getLogger("hh_agent.skills")

router = APIRouter()

MAX_BODY_BYTES = 64 * 1024  # §5: 本文サイズ上限64KB
PUBLISH_RATE_LIMIT = 20  # §5: トークンごとに publish 20件/時
PUBLISH_RATE_WINDOW_SECONDS = 3600

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# `ApiError`/`error_response` は approval_gate.py の完全に汎用的な部品を
# そのまま再利用する（同一所有者・重複実装を避ける）。
ApiError = approval_gate.ApiError
error_response = approval_gate.error_response


# ---------------------------------------------------------------------------
# ストア層: SkillsStore Protocol + 本番アダプタ
# ---------------------------------------------------------------------------


class SkillsStore(Protocol):
    def get(self, key: str) -> Optional[Any]: ...

    def put_if_absent(self, key: str, value: Any) -> bool: ...

    def delete(self, key: str) -> None: ...

    def outbox_register(self, event_id: str, payload: dict[str, Any]) -> bool: ...

    def outbox_consume(self, event_id: str) -> None: ...

    def write_json(self, rel_path: str, obj: Any) -> None: ...

    def read_file(self, rel_path: str) -> Optional[bytes]: ...

    def atomic_write_file(self, rel_path: str, content: bytes) -> None: ...


class _LiveSkillsStore:
    def get(self, key: str) -> Optional[Any]:
        return store.get(key)

    def put_if_absent(self, key: str, value: Any) -> bool:
        return store.put_if_absent(key, value)

    def delete(self, key: str) -> None:
        store.delete(key)

    def outbox_register(self, event_id: str, payload: dict[str, Any]) -> bool:
        return store.outbox_register(event_id, payload)

    def outbox_consume(self, event_id: str) -> None:
        store.outbox_consume(event_id)

    def write_json(self, rel_path: str, obj: Any) -> None:
        store.write_json(rel_path, obj)

    def read_file(self, rel_path: str) -> Optional[bytes]:
        return store.read_file(rel_path)

    def atomic_write_file(self, rel_path: str, content: bytes) -> None:
        store.atomic_write_file(rel_path, content)


_LIVE_STORE = _LiveSkillsStore()


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _skill_rel_path(name: str) -> str:
    return f"skills_quarantine/{name}/SKILL.md"


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _read_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "request body exceeds 64KB", retryable=False)
    if not raw:
        raise ApiError(400, "INVALID_REQUEST", "empty request body", retryable=False)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApiError(400, "INVALID_REQUEST", f"malformed JSON body: {exc}", retryable=False) from exc
    if not isinstance(parsed, dict):
        raise ApiError(400, "INVALID_REQUEST", "body must be a JSON object", retryable=False)
    return parsed


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ApiError(400, "INVALID_REQUEST", f"{key} must be a non-empty string", retryable=False)
    return value


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization")
    if not header or not header.startswith("Bearer "):
        raise ApiError(401, "UNAUTHORIZED", "missing bearer token", retryable=False)
    return header[len("Bearer ") :].strip()


def _encode_optional(value: Optional[str]) -> Optional[bytes]:
    return value.encode("utf-8") if value else None


def _verify_publish_agent(request: Request, s: SkillsStore) -> security.AgentIdentity:
    token = _bearer_token(request)
    try:
        identity = security.verify_agent_token(
            s,
            token,
            signing_key=config.agent_token_signing_key().encode("utf-8"),
            signing_key_prev=_encode_optional(config.agent_token_signing_key_prev()),
        )
    except security.SecurityError as exc:
        raise ApiError(401, "UNAUTHORIZED", "agent token invalid", retryable=False) from exc

    try:
        security.require_scope(identity, security.SCOPE_PUBLISH)
    except security.InsufficientScopeError as exc:
        raise ApiError(403, "FORBIDDEN", "token lacks the publish scope", retryable=False) from exc

    return identity


def _record_publish_event(
    s: SkillsStore,
    *,
    event: str,
    name: str,
    sub: str,
    content_sha256: str,
    detail: Optional[str] = None,
) -> None:
    audit.record_skill_publish_event(
        s, at=time.time(), event=event, name=name, sub=sub, content_sha256=content_sha256, detail=detail
    )


# ---------------------------------------------------------------------------
# POST /api/skills/publish
# ---------------------------------------------------------------------------


async def _publish_skill_core(request: Request, s: SkillsStore) -> JSONResponse:
    identity = _verify_publish_agent(request, s)

    try:
        security.check_rate_limit(
            s, subject=identity.sub, limit=PUBLISH_RATE_LIMIT, window_seconds=PUBLISH_RATE_WINDOW_SECONDS
        )
    except security.RateLimitExceededError as exc:
        raise ApiError(
            429,
            "RATE_LIMITED",
            str(exc),
            retryable=True,
            headers={"Retry-After": str(int(exc.retry_after_seconds) + 1)},
        ) from exc

    body = await _read_json_body(request)

    # 検証1: name の正規表現。§5 の番号順どおり、他フィールドの欠落チェック
    # より先に行う（2026-08-11 Codex レビュー Low 指摘: 旧実装は3フィールド
    # 全部を先に抽出していたため、name が正しくても後続フィールドが欠落
    # しているだけで INVALID_REQUEST の理由が「name不正」ではなく「フィールド
    # 欠落」にすり替わっていた）。
    name = _require_str(body, "name")
    if not skill_quarantine.NAME_RE.match(name):
        raise ApiError(
            400, "INVALID_REQUEST", "name does not match ^[a-z0-9][a-z0-9-]{1,48}$", retryable=False
        )

    skill_md = _require_str(body, "skill_md")
    claimed_sha256 = _require_str(body, "content_sha256")
    # 監査用に、クライアント申告値ではなく実測ダイジェストを常に使う
    # （検証3で一致を確認する前でも、監査記録は信頼できる値にしたい）。
    actual_sha256 = _content_sha256(skill_md)

    # 検証2: frontmatter の name がリクエストの name と一致。
    frontmatter_name = skill_quarantine.parse_frontmatter_name(skill_md)
    if frontmatter_name != name:
        _record_publish_event(
            s,
            event="skill_publish_rejected",
            name=name,
            sub=identity.sub,
            content_sha256=actual_sha256,
            detail="rejected: frontmatter name does not match the request name",
        )
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "skill_md frontmatter name does not match the request name",
            retryable=False,
        )

    # 検証3: content_sha256 が実際の本文ダイジェストと一致。
    if not _HEX64_RE.match(claimed_sha256):
        _record_publish_event(
            s,
            event="skill_publish_rejected",
            name=name,
            sub=identity.sub,
            content_sha256=actual_sha256,
            detail="rejected: content_sha256 is not a 64-hex digest",
        )
        raise ApiError(400, "INVALID_REQUEST", "content_sha256 must be a 64-hex digest", retryable=False)
    if actual_sha256 != claimed_sha256:
        _record_publish_event(
            s,
            event="skill_publish_rejected",
            name=name,
            sub=identity.sub,
            content_sha256=actual_sha256,
            detail="rejected: content_sha256 does not match the actual skill_md digest",
        )
        raise ApiError(
            400, "INVALID_REQUEST", "content_sha256 does not match the actual skill_md digest", retryable=False
        )
    content_sha256 = actual_sha256

    # 検証4: redaction 適用前後で差分が出たら保存を拒否する。
    if redact.redact_text(skill_md) != skill_md:
        _record_publish_event(
            s,
            event="skill_publish_rejected",
            name=name,
            sub=identity.sub,
            content_sha256=content_sha256,
            detail="rejected: content matched a redaction pattern",
        )
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "skill_md content matched a secret-like pattern and was rejected",
            retryable=False,
        )

    # 書き込みセマンティクス: create-or-match-only（§5、無条件上書き禁止）。
    #
    # 2026-08-11 Codex レビュー Critical 指摘の修正: 旧実装は
    # 「read_file で既存を確認 → 無ければ atomic_write_file」という
    # check-then-write だった。`atomic_write_file` は「存在しなければ作る」
    # ではなく無条件上書きのため、異なる内容の2リクエストが同時に
    # read_file で「無い」を観測してから両方書き込むと、後勝ちが前勝ちを
    # 静かに上書きする（実際に再現された）。`modal.Dict.put(skip_if_exists
    # =True)` の真の原子性を持つ `put_if_absent` を「この name の最初の
    # 書き手」を決める予約ロックとして使い、ファイル書き込み自体は
    # ロックの勝者（または同一内容の再送）だけが行う形にする。
    rel_path = _skill_rel_path(name)
    lock_key = store.skill_publish_key(name)
    reservation = {"content_sha256": content_sha256}
    won_reservation = s.put_if_absent(lock_key, reservation)

    if not won_reservation:
        existing_lock = s.get(lock_key)
        existing_lock_sha256 = existing_lock.get("content_sha256") if isinstance(existing_lock, dict) else None
        if existing_lock_sha256 != content_sha256:
            _record_publish_event(
                s,
                event="skill_publish_rejected",
                name=name,
                sub=identity.sub,
                content_sha256=content_sha256,
                detail="conflict: an existing skill with this name has different content",
            )
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "SKILL_ALREADY_PUBLISHED_WITH_DIFFERENT_CONTENT",
                        "message": "a skill with this name is already published with different content",
                        "retryable": False,
                    }
                },
            )
        # ロックの内容と一致する冪等な再送。ファイルが実在し一致していれば
        # 何もしない。ロック取得後・ファイル書き込み前にクラッシュした
        # ケースの自己修復として、ファイルが無ければここで書く（ロックは
        # 既にこの内容で確保済みなので、新たな競合を生まない）。
        existing_bytes = s.read_file(rel_path)
        if existing_bytes == skill_md.encode("utf-8"):
            return JSONResponse(status_code=200, content={"status": "ok", "unchanged": True})
        s.atomic_write_file(rel_path, skill_md.encode("utf-8"))
        return JSONResponse(status_code=200, content={"status": "ok", "unchanged": True})

    # ロックを勝ち取った ＝ この name への最初の書き込み。
    s.atomic_write_file(rel_path, skill_md.encode("utf-8"))
    _record_publish_event(s, event="skill_published", name=name, sub=identity.sub, content_sha256=content_sha256)

    return JSONResponse(status_code=200, content={"status": "ok", "unchanged": False})


@router.post("/api/skills/publish")
async def publish_skill(request: Request) -> JSONResponse:
    try:
        return await _publish_skill_core(request, _LIVE_STORE)
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # noqa: BLE001 — フェイルクローズ: 想定外は 500、握りつぶさない
        logger.exception("unexpected error in skills.publish_skill: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "internal error", "retryable": True}},
        )


__all__ = ["router", "SkillsStore", "ApiError"]
