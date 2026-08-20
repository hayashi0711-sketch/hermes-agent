"""modal_hub/approval_token.py — issue_approval_agent_token Modal Function（Agentic_OS 承認オラクル用トークン発行）。

設計上の位置づけ:
    - 依頼元: リポジトリルート `.agentic_os_issue_approval_token_task.md`
      （Wave 2a・2026-08-20）。設計書: Agentic_OS/03_Architecture.md §7 B7・§11
      （承認ゲート連携）。
    - `routers/approval_gate.py` の `POST /api/approval/request` /
      `GET /api/approval/poll` が要求する `HH_AGENT_TOKEN_SIGNING_KEY`
      （＝ `config.agent_token_signing_key()`）署名トークン（scope は
      request/poll）の発行経路。**HTTP エンドポイントとしては公開しない** —
      Modal ネイティブのクロスアプリ関数呼び出し
      （`modal.Function.from_name("hh-agent-hub", "issue_approval_agent_token")
      .remote(sub=..., session_id=...)`）としてのみ存在する。Modal ワークスペース
      資格情報が実質的な認証層になる（dispatch_token.py と同じ設計方針）。

    - `routers/approval_gate.py`・`core/security.py`・`core/config.py`・
      `core/store.py`・既存 FastAPI ルートは無改造（タスク指示）。本ファイルは
      新規追加のみ（main.py 末尾の import 1行のみ配線として追加。dispatch_token.py
      と同じ登録機構 — 本ファイル冒頭の「Modal Function エントリポイント」参照）。

    - `dispatch_token.py`（Wave 1a）の対になる関数: あちらは
      `AGENTIC_OS_DISPATCH_KEY` 署名・`scopes=[dispatch]`、こちらは
      `HH_AGENT_TOKEN_SIGNING_KEY` 署名・`scopes=[request, poll]`。

== このファイルが独自に決めた設計判断 ==

1. **source / workspace_id は dispatch_token.py の定数をそのまま再利用する。**
   タスク文面の「dispatch_token.py の issue_dispatch_token と同じ考え方で
   固定文字列でよい」に従い、`SOURCE_AGENTIC_OS`（= `SOURCE_CLAUDE_CODE`）と
   `WORKSPACE_AGENTIC_OS`（= sha256("agentic_os")）を import して使う。
   二重定義しないのは「Agentic_OS Hub からの呼び出し」という同一の主体性を
   トークン種別をまたいで保証するため — approval_gate の `_check_agent_ownership`
   は source/session_id/workspace_id の完全一致で所有権を判定するので、dispatch
   と approval で同じ主体値が使われることが設計上の前提になる。

2. **scopes は ["request", "poll"] に限定する（最小権限・既存慣習の部分集合）。**
   `scripts/hh_issue_agent_token.py` が発行する承認フロー用トークン
   （agent_token.json）は Phase1a 互換のレガシーデフォルト
   （`_LEGACY_DEFAULT_SCOPES` = request/poll/claim/complete の4つ）を持つが、
   本関数は「承認オラクル」としての用途（申請とポーリング）だけをタスク文面
   どおり与える。レガシーデフォルトの**部分集合**であり、Phase1b §5 の scopes
   機構に乗って最小権限へ絞る方向（`dispatch`/`publish` 等は含まない）で既存の
   慣習と矛盾しない。定数名は security.py の SCOPE_* 命名に合わせて新設する
   （"request"/"poll" に対応する公開定数が security.py に無いため）。

3. **`HH_AGENT_TOKEN_SIGNING_KEY` 未設定は security.SecurityError で fail-closed。**
   `config.agent_token_signing_key()` は必須鍵で未設定なら
   `config.SecretMissingError` を送出するが、dispatch_token.py と同じ
   「鍵未設定は発行段階で明示的にエラー」の契約（`SecurityError`）を保つため
   読み替えて送出する。トークン値・鍵値は例外メッセージに一切含めない
   （hard rule 4）。

4. **ストアは dispatch_token.py と同じモジュールレベル singleton パターン。**
   `_LiveTokenStore`（本番アダプタ）は dispatch_token.py のものを再利用する
   （同一パッケージ内の実装詳細。二重定義を避ける）。テスト時は
   `approval_token._LIVE_STORE` を conftest の FakeStore へ差し替える。
"""

from __future__ import annotations

import logging

import modal

import hashlib

from modal_hub.core import config
from modal_hub.core import security
from modal_hub.core import store
from modal_hub.main import (
    _SECRET_NAME,
    _STORE_MOUNT_PATH,
    _STORE_VOLUME_NAME,
    app,
    image,
)

# SOURCE_AGENTIC_OS / WORKSPACE_AGENTIC_OS / _LiveTokenStore は dispatch_token.py
# と同じ値・同じ実装だが、そこから import せずここで独立に定義する（2026-08-21、
# 本番デプロイで発覚した修正）。main.py がモジュール末尾で
# `from modal_hub import dispatch_token` の直後に `from modal_hub import
# approval_token` を実行する構成だと、`modal deploy modal_hub/main.py` の
# 実行方式（ファイルを直接ロードするため modal_hub.main が __main__ とは別に
# 再importされうる）のもとで dispatch_token モジュールが「初期化途中」のまま
# approval_token から参照され、
# `ImportError: cannot import name 'SOURCE_AGENTIC_OS' from partially
# initialized module 'modal_hub.dispatch_token' (most likely due to a
# circular import)` で本番デプロイが失敗した。値は単純な定数・薄いラッパー
# クラスのため、importで共有せず複製することで循環参照そのものを断つ。

#: dispatch_token.py の SOURCE_AGENTIC_OS と同一値（Agentic_OS Hubからの
#: トークン発行という同一の主体性を、dispatch用途・approval用途の両方で
#: 一致させる必要があるため。approval_gate._check_agent_ownership は
#: source/session_id/workspace_id の完全一致で所有権を判定する）。
SOURCE_AGENTIC_OS = security.SOURCE_CLAUDE_CODE

#: dispatch_token.py の WORKSPACE_AGENTIC_OS と同一値（sha256("agentic_os")）。
WORKSPACE_AGENTIC_OS = hashlib.sha256(b"agentic_os").hexdigest()


class _LiveTokenStore:
    """`security.CredentialStore` の本番アダプタ（dispatch_token.py と同型・複製）。"""

    def get(self, key: str):
        return store.get(key)

    def put_if_absent(self, key: str, value) -> bool:
        return store.put_if_absent(key, value)

    def delete(self, key: str) -> None:
        store.delete(key)

logger = logging.getLogger("hh_agent.approval_token")

#: `POST /api/approval/request` を呼べるスコープ名（approval_gate.py の
#: `_require_scope(identity, "request")` に対応。security.py に公開定数が
#: 無いため本ファイルで定義する — 判断 2 参照）。
SCOPE_APPROVAL_REQUEST = "request"

#: `GET /api/approval/poll` を呼べるスコープ名（同上・"poll" 対応）。
SCOPE_APPROVAL_POLL = "poll"

#: 本関数が発行するトークンの scopes。`_LEGACY_DEFAULT_SCOPES`（request/poll/
#: claim/complete）の部分集合で、承認オラクル用途に必要な申請・ポーリングのみ
#: に絞る（判断 2 参照）。
APPROVAL_SCOPES = (SCOPE_APPROVAL_REQUEST, SCOPE_APPROVAL_POLL)


_LIVE_STORE: security.CredentialStore = _LiveTokenStore()


# ---------------------------------------------------------------------------
# 発行コア（テスト可能な分離）
# ---------------------------------------------------------------------------


def _issue_approval_agent_token_core(
    s: security.CredentialStore, *, sub: str, session_id: str
) -> str:
    """`agent_session:<tid>` 肯定リストレコードを書き、承認フロー用トークンを返す。

    `security.issue_agent_token` を `config.agent_token_signing_key()`
    （＝ `HH_AGENT_TOKEN_SIGNING_KEY`）署名・`scopes=[request, poll]`・
    dispatch_token.py と同じ固定 source/workspace_id で呼ぶ。トークン文字列は
    この関数の戻り値としてのみ呼び出し元へ渡り、ログへは一切出さない。

    Args:
        s: 肯定リストストア（`agent_session:<tid>` の書き込み先）。
        sub: Hub Backend が指定するトークン主体識別子（承認レコードの
             `sub`・レート制限 subject にも使われる。非空必須 — 検証は
             issue_agent_token が行う）。
        session_id: 承認対象セッションの識別子（非空必須）。

    Raises:
        security.SecurityError: `HH_AGENT_TOKEN_SIGNING_KEY` 未設定
            （fail-closed）。
        ValueError: sub / session_id が空など、呼び出し側のプログラミングエラー
            （issue_agent_token のバリデーションに従う）。
    """
    try:
        signing_key = config.agent_token_signing_key()
    except config.SecretMissingError as exc:
        raise security.SecurityError(
            "HH_AGENT_TOKEN_SIGNING_KEY is not configured; cannot issue "
            "approval agent tokens (fail-closed)"
        ) from exc
    return security.issue_agent_token(
        s,
        sub=sub,
        source=SOURCE_AGENTIC_OS,
        session_id=session_id,
        workspace_id=WORKSPACE_AGENTIC_OS,
        signing_key=signing_key.encode("utf-8"),
        scopes=APPROVAL_SCOPES,
    )


# ---------------------------------------------------------------------------
# Modal Function エントリポイント
# ---------------------------------------------------------------------------
#
# HTTP エンドポイントではない（設計書 §7 B7「承認オラクルは Modal ネイティブ
# 関数呼び出しで使う」）。main.py のモジュール末尾が本モジュールを import
# することで `modal deploy modal_hub.main` の際に @app.function() が実行され、
# Hub Backend の `modal.Function.from_name("hh-agent-hub",
# "issue_approval_agent_token")` から解決できるようになる。


@app.function(
    image=image,
    secrets=[modal.Secret.from_name(_SECRET_NAME)],
    volumes={
        _STORE_MOUNT_PATH: modal.Volume.from_name(
            _STORE_VOLUME_NAME, create_if_missing=True
        )
    },
    # D-19 / scale-to-zero: コスト下限はゼロ（main.py の ASGI エントリポイントと
    # 同じ設定）。Warm-path SLO は 1s、cold-path SLO は 10s（spec はこれを
    # 別予算として扱う）。
    min_containers=0,
    scaledown_window=300,
)
def issue_approval_agent_token(sub: str, session_id: str) -> str:
    """`HH_AGENT_TOKEN_SIGNING_KEY` で署名した承認フロー用トークンを発行する。

    Hub Backend 側はこの関数を Modal のクロスアプリ関数呼び出しで利用する:
    ``modal.Function.from_name("hh-agent-hub", "issue_approval_agent_token"
    ).remote(sub=..., session_id=...)``。公開 HTTP エンドポイントは増やさない
    （Agentic_OS/03_Architecture.md §7 B7）。

    発行されるトークンの scopes は `["request", "poll"]` であり、
    `POST /api/approval/request`（`_require_scope(identity, "request")`）と
    `GET /api/approval/poll`（`_require_scope(identity, "poll")`）の両方を
    通す。claim/complete は付与しない（最小権限・判断 2 参照）。

    Returns:
        発行されたトークン文字列（`hha1.<payload>.<sig>`）。トークン値は
        呼び出し元にのみ返され、ログへは出さない。

    Raises:
        security.SecurityError: `HH_AGENT_TOKEN_SIGNING_KEY` 未設定
            （fail-closed）。
    """
    return _issue_approval_agent_token_core(_LIVE_STORE, sub=sub, session_id=session_id)


__all__ = [
    "SCOPE_APPROVAL_REQUEST",
    "SCOPE_APPROVAL_POLL",
    "APPROVAL_SCOPES",
    "SOURCE_AGENTIC_OS",
    "WORKSPACE_AGENTIC_OS",
    "issue_approval_agent_token",
    "_issue_approval_agent_token_core",
]
