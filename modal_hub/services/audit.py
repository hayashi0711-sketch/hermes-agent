"""modal_hub/services/audit.py — H-H Agent 不変監査ログ（Phase 1a）。

設計上の位置づけ:
    - 親設計書 docs/hh-agent/03_Architecture.md §5.3
    - 実装契約   docs/hh-agent/05_Phase1a_Spec.md §10（監査ログ全体）、
                 特に §10.1（ファイル名・決定的 event_id）、
                 §10.1b（永続 outbox）、§10.2（レコード形式）、
                 §10.3（redaction）、§10.4（失敗時の順序保証）

このモジュールの責務: **1 イベント 1 ファイルの不変 JSON** を Volume へ書く。
状態キー（`modal.Dict`）の更新と Volume への書き込みは別ストアでトランザク
ションにできないため（§10.1b）、outbox パターンを使う:

    1. まず `outbox:<event_id>` へ登録する（`modal.Dict` の
       `put(skip_if_exists=True)` なので確実に残る）。
    2. Volume への書き出しを試みる。成功したら `outbox:<event_id>` を削除。
    3. 失敗しても、定期 GC ジョブ（本タスクの所有範囲外）が `outbox:*` を
       走査して Volume へ再書き出しする。

**「監査に失敗したら」の判定は「outbox への登録に失敗したら」を意味する**
（§10.1b）。Volume 書き込みの失敗単体は :func:`record_event` からは例外として
外へは伝播しない（呼び出し側は 500 を返さない）。

== ストア依存性の設計（security.py と同じ DI パターン） ==

`modal_hub/core/store.py`（別所有者）は `outbox_register` / `outbox_consume` /
`write_json` を公開している。本モジュールはこれらを **関数引数として注入
される `store` オブジェクト経由**で呼ぶ（`AuditStore` Protocol）。これにより:

    - `modal_hub/core/store.py` の実物（`from modal_hub.core import store`
      した**モジュールそのもの**）をそのまま渡せる（モジュールは属性アクセス
      で関数を解決できるため、構造的に Protocol を満たす）。
    - Modal 実行環境が無いユニットテストでも、この 3 メソッドだけを持つ
      軽量なダブルに差し替えてテストできる（`modal.Dict`/Volume への実際の
      ネットワーク呼び出しを発生させない）。

== event_id の決定性（§10.1） ==

``event_id = sha256(f"{approval_id}|{event}|{discriminator}").hexdigest()[:16]``。
ランダム名（v1 の `rand8`）は使わない — commit は成功したが HTTP 応答が
失われて呼び出し側がリトライした場合、ランダム名では別ファイルが作られて
同じ事象が重複記録される。決定的な名前なら同じファイルへの上書きになり
冪等になる（`os.replace` によるアトミック上書き。`store.write_json` が担保）。

== 呼び出し側の責務（`routers/approval_gate.py`） ==

`discriminator` の値はイベント種別ごとに §10.1 の表に従って呼び出し側が渡す
（`requested`/`approved`/`rejected`/`timed_out` は `""`、`claim_granted`/
`mismatch` は `claim_attempt_id`、`consumed`/`failed` は `lease_id`）。
本モジュールは discriminator の意味を解釈しない（呼び出し側の責務）。

== このモジュールが独自に埋めた判断 ==

`§10.1` のイベント種別のうち `auth_failed`/`rate_limited`/`pairing_rate_limited`/
`bypass_used`/`unexpected_transition` の discriminator は表に明記が無い。
本モジュールは discriminator を単なる文字列として受け取るだけで意味を解釈
しないため、この判断は呼び出し側（`routers/approval_gate.py`）の責務であり、
そちらの docstring で開示する。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from modal_hub.core import redact

logger = logging.getLogger("hh_agent.audit")

# ---------------------------------------------------------------------------
# ストア依存性（注入）
# ---------------------------------------------------------------------------


class AuditStore(Protocol):
    """本モジュールが要求するストア層の最小インターフェース。

    `modal_hub/core/store.py` が公開する `outbox_register` / `outbox_consume` /
    `write_json` と同一シグネチャ。呼び出し側は `from modal_hub.core import
    store` した**モジュールそのもの**をここへ渡せる。
    """

    def outbox_register(self, event_id: str, payload: dict[str, Any]) -> bool:
        """outbox へイベントを登録する（`put_if_absent` 相当）。

        戻り値は「この呼び出しで新規登録したか」であり、`False`（既に
        登録済み）は失敗ではない（冪等な再登録）。例外は真の障害
        （ストア層の異常）を表し、:func:`record_event` はそのまま伝播する。
        """
        ...

    def outbox_consume(self, event_id: str) -> None:
        """Volume への書き出しに成功した outbox エントリを削除する（冪等）。"""
        ...

    def write_json(self, rel_path: str, obj: Any) -> None:
        """Volume へ JSON をアトミックに書き込む。失敗時は例外を送出する。"""
        ...


# ---------------------------------------------------------------------------
# event_id / ファイルパス
# ---------------------------------------------------------------------------


def compute_event_id(approval_id: str, event: str, discriminator: str) -> str:
    """Phase1a spec §10.1 の決定的 event_id を計算する。

    ``sha256(f"{approval_id}|{event}|{discriminator}").hexdigest()[:16]``。
    同じ3引数からは常に同じ event_id が得られる（＝再送安全）。
    """
    raw = f"{approval_id}|{event}|{discriminator}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _audit_rel_path(approval_id: str, event: str, event_id: str, at: float) -> str:
    """``audit/<YYYY-MM>/<approval_id>.<event>.<event_id>.json`` を組み立てる。

    月ディレクトリはイベント発生時刻（`at`、UTC）から決める。
    """
    month = datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m")
    return f"audit/{month}/{approval_id}.{event}.{event_id}.json"


# ---------------------------------------------------------------------------
# レコード形式（Phase1a spec §10.2）
# ---------------------------------------------------------------------------


def build_record(
    *,
    at: float,
    event: str,
    approval_id: str,
    sub: str,
    source: str,
    session_id: str,
    workspace_id: str,
    tool_name: str,
    risk: str,
    rule_id: str,
    payload: Optional[dict[str, Any]],
    detail: Optional[str],
) -> dict[str, Any]:
    """Phase1a spec §10.2 のレコード形状を組み立てる。

    `payload_redacted` は §10.3 の :func:`core.redact.redact_value` を適用
    済みの状態で格納する（生の `payload` をそのまま監査に残さない）。
    `detail` も自由文フィールドなので同様に redaction を通す。
    """
    return {
        "at": at,
        "event": event,
        "approval_id": approval_id,
        "sub": sub,
        "source": source,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "tool_name": tool_name,
        "risk": risk,
        "rule_id": rule_id,
        "payload_redacted": redact.redact_value(payload) if payload is not None else {},
        "detail": redact.redact_text(detail) if isinstance(detail, str) else None,
    }


# ---------------------------------------------------------------------------
# 公開エントリポイント
# ---------------------------------------------------------------------------


def record_event(
    store: AuditStore,
    *,
    at: float,
    event: str,
    discriminator: str,
    approval_id: str,
    sub: str,
    source: str,
    session_id: str,
    workspace_id: str,
    tool_name: str,
    risk: str,
    rule_id: str,
    payload: Optional[dict[str, Any]] = None,
    detail: Optional[str] = None,
) -> None:
    """監査イベントを記録する（outbox 登録 → Volume flush 試行 → 成功なら consume）。

    Phase1a spec §10.4「原則」: 「実行を許す方向の遷移」は監査の永続化が
    確定しない限り許さない。この関数の**呼び出し側**がその判断（500 を返す
    かどうか）を行う。この関数自身は「outbox 登録が失敗したら例外を送出する
    （＝呼び出し側は 500 AUDIT_FAILED にできる）」「Volume flush の失敗は
    outbox に残して黙って戻る（＝500 にしない）」という2段階の失敗許容度を
    実装するだけである。

    Raises:
        Exception: `store.outbox_register` が送出したもの（例:
            `store.StoreError`）をそのまま伝播する。呼び出し側は
            これを「監査失敗」として扱う（§10.1b「監査に失敗したら＝
            outboxへの登録に失敗したら」）。
    """
    event_id = compute_event_id(approval_id, event, discriminator)
    record = build_record(
        at=at,
        event=event,
        approval_id=approval_id,
        sub=sub,
        source=source,
        session_id=session_id,
        workspace_id=workspace_id,
        tool_name=tool_name,
        risk=risk,
        rule_id=rule_id,
        payload=payload,
        detail=detail,
    )

    # ここで例外が飛んだ場合は呼び出し側へそのまま伝播させる。もみ消さない
    # （outbox 登録の失敗こそが「監査失敗」の一次情報源のため）。
    store.outbox_register(event_id, record)

    # Volume への書き出しは best-effort。失敗しても「監査失敗」にはしない
    # （§10.1b: 定期 GC ジョブが outbox を再送する前提）。
    rel_path = _audit_rel_path(approval_id, event, event_id, at)
    try:
        store.write_json(rel_path, record)
    except Exception:
        # 失敗時は outbox にレコードを残したまま戻る。GC ジョブ（本タスクの
        # 所有範囲外）が後で再送する。ここで再送出すると「Volume 書き込みの
        # 失敗単体で 500 を返さない」という §10.1b の規則に反するため、
        # 意図的に握りつぶす（制御フローは変えない。ログのみ。呼び出し側
        # にも例外を見せない）。
        #
        # BUG-10 修正: 「ログのみ」と書きながら実際には logger を一切
        # 呼ばずに return するだけだったため、Volume 障害が
        # outbox の無制限な滞留としてしか観測できなかった（03_Architecture.md
        # §9 落とし穴 15「黙って空を返す実装は原因を隠す」に抵触）。
        # ここで実際にログを出す。診断に必要な event_id/event/approval_id/
        # rel_path のみを載せ、payload（生コマンド・秘密を含みうる）や
        # record 全体・token 類は一切ログに含めない（redaction 済みの
        # `record["payload_redacted"]` すら、ここでは意図的に載せない）。
        logger.error(
            "audit volume write failed; event remains in outbox for later GC re-flush "
            "(event_id=%s event=%s approval_id=%s rel_path=%s)",
            event_id,
            event,
            approval_id,
            rel_path,
            exc_info=True,
        )
        return

    store.outbox_consume(event_id)


# ---------------------------------------------------------------------------
# skill_published / skill_publish_rejected（Phase1b, 07_Phase1b_Spec.md §5）
# ---------------------------------------------------------------------------
#
# `record_event`/`build_record` は Phase1a の承認レコード形状
# （approval_id/tool_name/risk/rule_id...）に固定されており、スキル publish
# イベントにはそのまま使えない（該当する概念が無い）。既存の「1イベント1
# ファイル・outbox 経由・決定的 event_id」というパターン自体は
# `07_Phase1b_Spec.md` §5 が明示的に指示するとおり再利用しつつ、レコード
# 形状だけスキル publish 用に別立てする。既存の `record_event` は変更しない
# （Phase1a でテスト済みの経路に手を入れない）。


def compute_skill_publish_event_id(name: str, content_sha256: str, event: str) -> str:
    """`skill_published`/`skill_publish_rejected` 用の決定的 event_id。

    同一 `(name, content_sha256, event)` の再送は同じファイルへの冪等な
    上書きになる（`content_sha256` を鍵に含めるのは、同名スキルの
    「内容が同じ再送」と「内容が違う再送（409 案件）」を別イベントとして
    区別するため）。
    """
    raw = f"skill_publish|{name}|{content_sha256}|{event}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _skill_publish_rel_path(name: str, event: str, event_id: str, at: float) -> str:
    month = datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m")
    return f"audit/{month}/skill_publish.{name}.{event}.{event_id}.json"


def build_skill_publish_record(
    *,
    at: float,
    event: str,
    name: str,
    sub: str,
    content_sha256: str,
    detail: Optional[str],
) -> dict[str, Any]:
    return {
        "at": at,
        "event": event,
        "name": name,
        "sub": sub,
        "content_sha256": content_sha256,
        "detail": redact.redact_text(detail) if isinstance(detail, str) else None,
    }


def record_skill_publish_event(
    store: AuditStore,
    *,
    at: float,
    event: str,
    name: str,
    sub: str,
    content_sha256: str,
    detail: Optional[str] = None,
) -> None:
    """`skill_published`/`skill_publish_rejected` を記録する。

    失敗許容度は `record_event` と同じ2段階（outbox 登録失敗は伝播、
    Volume flush 失敗は outbox に残して黙って戻る）。

    Raises:
        ValueError: `event` が既定の2値以外。
        Exception: `store.outbox_register` が送出したもの。
    """
    if event not in ("skill_published", "skill_publish_rejected"):
        raise ValueError(f"unsupported skill publish event: {event!r}")

    event_id = compute_skill_publish_event_id(name, content_sha256, event)
    record = build_skill_publish_record(
        at=at, event=event, name=name, sub=sub, content_sha256=content_sha256, detail=detail
    )

    store.outbox_register(event_id, record)

    rel_path = _skill_publish_rel_path(name, event, event_id, at)
    try:
        store.write_json(rel_path, record)
    except Exception:
        logger.error(
            "audit volume write failed for skill publish event; remains in outbox "
            "for later GC re-flush (event_id=%s event=%s name=%s rel_path=%s)",
            event_id,
            event,
            name,
            rel_path,
            exc_info=True,
        )
        return

    store.outbox_consume(event_id)


__all__ = [
    "AuditStore",
    "compute_event_id",
    "build_record",
    "record_event",
    "compute_skill_publish_event_id",
    "build_skill_publish_record",
    "record_skill_publish_event",
]
