"""modal_hub/dispatch_token.py — issue_dispatch_token Modal Function（Agentic_OS 向けトークン発行）。

設計上の位置づけ:
    - 依頼元: リポジトリルート `.agentic_os_issue_dispatch_token_task.md`
      （Wave 1a・2026-08-20）。設計書: Agentic_OS/03_Architecture.md §11.0。
    - `routers/dispatch.py` の `POST /api/dispatch/headless` が要求する
      `AGENTIC_OS_DISPATCH_KEY` 署名トークンの発行経路。**HTTP エンドポイントとして
      は公開しない** — Modal ネイティブのクロスアプリ関数呼び出し
      （`modal.Function.from_name("hh-agent-hub", "issue_dispatch_token").remote(...)`）
      としてのみ存在する。Modal ワークスペース資格情報が実質的な認証層になる
      （既存の Deepseek-harness `dispatch_headless` と同じ設計方針）。

    - `routers/dispatch.py` ・`routers/approval_gate.py` ・`routers/skills.py`・
      既存 FastAPI ルートは無改造（タスク指示）。本ファイルは新規追加のみ。

== このファイルが独自に決めた設計判断 ==

1. **source は `security.SOURCE_CLAUDE_CODE` を固定で使う。**
   タスク文面は `source` に `"agentic_os_hub"` のような固定文字列を提案しているが、
   `security.ALLOWED_SOURCES` は `("claude_code", "cloud_agent")` のみであり、
   `issue_agent_token` も `verify_agent_token`（＝ `POST /api/dispatch/headless` の
   `_verify_dispatch_agent` が呼ぶ）もそれ以外の source を例外で拒否する。
   `"agentic_os_hub"` を採用すると完了報告要件「発行したトークンで認証を実際に
   通過する」と両立できない（発行時に ValueError、検証時に 401）。
   既存 `routers/dispatch.py` の設計注記（「source は claude_code/cloud_agent の
   いずれか」）と `tests/test_dispatch_router.py` の既存テストに合わせ、
   claude_code に固定する。security.py は無改造前提（Wave 1a: 新規追加のみ）のため
   ALLOWED_SOURCES の拡張は行わない。

2. **workspace_id は固定文字列 "agentic_os" の sha256 hexdigest を使う。**
   `issue_agent_token` は workspace_id に 64-hex（sha256 hexdigest）形式を強制する
   （`security._WORKSPACE_ID_RE_LEN`）。平文 `"agentic_os"` は形式違反で ValueError
   になるため、タスクの「固定文字列でよい」を満たしつつ形式要件も満たす値として
   `sha256("agentic_os")` をワークスペース識別子に固定する。

3. **`AGENTIC_OS_DISPATCH_KEY` 未設定は `security.SecurityError` で fail-closed。**
   `config.agentic_os_dispatch_key()` は Optional。未設定のままトークンを発行すると
   `POST /api/dispatch/headless` 側は検証できず 401 になるだけなので、発行段階で
   明示的にエラーにする（`_verify_dispatch_agent` と同じ fail-closed の一貫性）。
   トークン値・鍵値は例外メッセージに一切含めない（hard rule 4）。

4. **ストアは dispatch.py の `_LiveDispatchStore` と同型のアダプタを介す。**
   `core/store.py` のモジュール関数（get / put_if_absent / delete）は構造的に
   `security.CredentialStore` を満たすため直接渡してもよいが、テスト時に
   `dispatch_token._LIVE_STORE` を conftest の FakeStore へ差し替えられるよう、
   dispatch.py と同じ「モジュールレベル singleton + monkeypatch 差し替え」パターン
   を踏襲する。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

import modal

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

logger = logging.getLogger("hh_agent.dispatch_token")

#: `issue_agent_token` の source クレームに使う固定値。`security.ALLOWED_SOURCES`
#: のどちらかである必要がある（ファイル docstring の判断 1 参照）。
SOURCE_AGENTIC_OS = security.SOURCE_CLAUDE_CODE

#: workspace_id クレームに使う固定値。`issue_agent_token` は 64-hex の sha256
#: hexdigest 形式を要求するため、固定文字列 "agentic_os" の sha256 を使う
#: （ファイル docstring の判断 2 参照）。
WORKSPACE_AGENTIC_OS = hashlib.sha256(b"agentic_os").hexdigest()


# ---------------------------------------------------------------------------
# ストア層: dispatch.py の _LiveDispatchStore と同型の本番アダプタ
# ---------------------------------------------------------------------------


class _LiveTokenStore:
    """`security.CredentialStore` の本番アダプタ。

    `core/store.py` のモジュール関数（get / put_if_absent / delete）をそのまま
    束ねる。テスト時は `dispatch_token._LIVE_STORE` を conftest の FakeStore に
    差し替える（routers/dispatch.py の `_LIVE_STORE` と同じパターン）。
    """

    def get(self, key: str) -> Optional[Any]:
        return store.get(key)

    def put_if_absent(self, key: str, value: Any) -> bool:
        return store.put_if_absent(key, value)

    def delete(self, key: str) -> None:
        store.delete(key)


_LIVE_STORE = _LiveTokenStore()


# ---------------------------------------------------------------------------
# 発行コア（テスト可能な分離）
# ---------------------------------------------------------------------------


def _issue_dispatch_token_core(
    s: security.CredentialStore, *, sub: str, session_id: str
) -> str:
    """`agent_session:<tid>` 肯定リストレコードを書き、dispatch トークンを返す。

    `security.issue_agent_token` を `AGENTIC_OS_DISPATCH_KEY` 署名・
    `scopes=[SCOPE_DISPATCH]`・固定 source/workspace_id で呼ぶ。トークン文字列は
    この関数の戻り値としてのみ呼び出し元へ渡り、ログへは一切出さない。

    Args:
        s: 肯定リストストア（`agent_session:<tid>` の書き込み先）。
        sub: Hub Backend が指定するトークン主体識別子（dispatch のレート制限
             subject にも使われる。非空必須 — 検証は issue_agent_token が行う）。
        session_id: ディスパッチ対象セッションの識別子（非空必須）。

    Raises:
        security.SecurityError: `AGENTIC_OS_DISPATCH_KEY` 未設定（fail-closed）。
        ValueError: sub / session_id が空など、呼び出し側のプログラミングエラー
            （issue_agent_token のバリデーションに従う）。
    """
    signing_key = config.agentic_os_dispatch_key()
    if not signing_key:
        raise security.SecurityError(
            "AGENTIC_OS_DISPATCH_KEY is not configured; cannot issue "
            "dispatch tokens (fail-closed)"
        )
    return security.issue_agent_token(
        s,
        sub=sub,
        source=SOURCE_AGENTIC_OS,
        session_id=session_id,
        workspace_id=WORKSPACE_AGENTIC_OS,
        signing_key=signing_key.encode("utf-8"),
        scopes=[security.SCOPE_DISPATCH],
    )


# ---------------------------------------------------------------------------
# Modal Function エントリポイント
# ---------------------------------------------------------------------------
#
# HTTP エンドポイントではない（設計書 §11.0「新規公開HTTPは増やさない」）。
# main.py のモジュール末尾が本モジュールを import することで
# `modal deploy modal_hub.main` の際に @app.function() が実行され、
# Hub Backend の `modal.Function.from_name("hh-agent-hub",
# "issue_dispatch_token")` から解決できるようになる。


@app.function(
    image=image,
    # AGENTIC_OS_DISPATCH_KEY は既存の hh-agent-secret には含まれておらず（本番未設定
    # のためfail-closedだった。03_Architecture.md §7.1参照）、hh-agent-secret を
    # 無理に上書き・再作成すると既存キー（HH_AGENT_TOKEN_SIGNING_KEY等）を消しかねない
    # ため、新規の独立したSecret agentic-os-dispatch-secret へ分離する（2026-08-21、
    # 本番反映時に追加）。
    secrets=[
        modal.Secret.from_name(_SECRET_NAME),
        modal.Secret.from_name("agentic-os-dispatch-secret"),
    ],
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
def issue_dispatch_token(sub: str, session_id: str) -> str:
    """`AGENTIC_OS_DISPATCH_KEY` で署名した dispatch トークンを発行する。

    Hub Backend 側はこの関数を Modal のクロスアプリ関数呼び出しで利用する:
    ``modal.Function.from_name("hh-agent-hub", "issue_dispatch_token").remote(
    sub=..., session_id=...)``。公開 HTTP エンドポイントは増やさない
    （Agentic_OS/03_Architecture.md §11.0）。

    Returns:
        発行されたトークン文字列（`hha1.<payload>.<sig>`）。トークン値は
        呼び出し元にのみ返され、ログへは出さない。

    Raises:
        security.SecurityError: `AGENTIC_OS_DISPATCH_KEY` 未設定（fail-closed）。
    """
    return _issue_dispatch_token_core(_LIVE_STORE, sub=sub, session_id=session_id)


__all__ = [
    "SOURCE_AGENTIC_OS",
    "WORKSPACE_AGENTIC_OS",
    "issue_dispatch_token",
    "_issue_dispatch_token_core",
]
