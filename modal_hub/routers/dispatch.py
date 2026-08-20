"""modal_hub/routers/dispatch.py — POST /api/dispatch/headless（Agentic_OS ヘッドレスディスパッチ）。

設計上の位置づけ:
    - 依頼元: リポジトリルート `.agentic_os_headless_dispatch_task.md`
      （Agentic_OS プロジェクトが「1個のプロンプトを渡して実行させ、最終応答を
      受け取る」ために使う。設計書: Agentic_OS/03_Architecture.md §2.1）。
    - 実装は既存 router（`approval_gate.py` / `skills.py`）の確立したパターンを
      そのまま踏襲する: `ApiError`/`error_response` による統一エラー封筒、
      Bearer トークン検証 + `security.verify_agent_token` + `require_scope`、
      DI 可能な Store Protocol + `_LiveXxxStore` 本番アダプタ。
      `approval_gate.ApiError`/`error_response` は完全に汎用的な部品のため
      import して再利用し、複製しない（skills.py と同じ方針）。

== このファイルが独自に決めた設計判断 ==

1. **認証は既存機構を再利用し、署名鍵だけを新しい環境変数へ分離する。**
   新しい認証方式は発明しない。`security.verify_agent_token` + `require_scope`
   （スコープは新設の `security.SCOPE_DISPATCH`）をそのまま使い、署名鍵を
   `AGENTIC_OS_DISPATCH_KEY`（config.py の optional secret。未設定でも Hub は
   起動するが、dispatch の検証は 401 で fail-closed になる）に変える。
   トークンの発行（`security.issue_agent_token` で AGENTIC_OS_DISPATCH_KEY 署名・
   `scopes=["dispatch"]`、source は claude_code/cloud_agent のいずれか）は
   Agentic_OS 側の運用で行う前提。
   要確認: 現状 modal_hub 配下にこの鍵でトークンを発行するエンドポイントは
   存在しない（`issue_agent_token` の呼び出し元はテストのみ）。Agentic_OS 側の
   トークン発行経路（`hh auth login` の Hub 側処理をどこで実行するか）が別途
   必要になる。

2. **hermes 本体は毎回の呼び出しごとに一時 HERMES_HOME を使う完全ステートレス設計。**
   既存 modal_dashboard の永続 Volume（`hh-agent-dashboard-home`）やその
   HERMES_HOME は一切共有しない。毎回 `tempfile.mkdtemp()` を使い、実行後に
   削除する。Modal Volume はシンボリックリンク実装のため `Path.resolve()` 比較が
   静かに壊れる既知の罠（落とし穴73: Handoff Note の既知罠リスト）があり、この
   経路は Volume 自体を使わないことで回避する。

3. **記憶だけは Corpus2Skill 経由で共有する。** 一時 HERMES_HOME に config.yaml を
   都度生成し `memory.provider: corpus2skill` を設定する。この設定キーは
   `scripts/install_corpus2skill_plugin.ps1` が `hermes config set memory.provider
   corpus2skill` で使う実在キー（ネスト形式 `memory: {provider: corpus2skill}`）。
   Corpus2Skill プラグイン（`.hermes/plugins/corpus2skill/`）は
   `$HERMES_HOME/plugins/` ユーザー層へコピーする（`plugins/memory/__init__.py`
   の発見順序: bundled → user → project。一時 HOME なので user 層を使う）。
   API キーは環境変数経由でのみ渡し、値はログへ一切出さない（config.safe_repr の
   規律）。

4. **実行は `python -m hermes_cli.main -z <prompt> --usage-file <path>` のサブプロセス。**
   `-z`/`--oneshot` は公式のヘッドレス単発モード（stdout = 最終応答のみ。
   hermes_cli/_parser.py）。`session_id` は usage-file（JSON レポートの
   `session_id` キー）から抽出する — oneshot の stdout には session_id が出ない
   ため（hermes_cli/oneshot.py の `_write_usage_file` が書く。失敗時も書かれる）。
   exit code 0 = 成功 / 1 = 失敗 / 2 = usage エラー（hermes_cli/main.py
   `_run_and_exit_oneshot`）。内部で HERMES_YOLO_MODE=1 / HERMES_ACCEPT_HOOKS=1 を
   設定して進むため、承認プロンプトでハングしない。

5. **タイムアウトは 300 秒。** 超過時は Popen のプロセスグループごと kill して
   504 GATEWAY_TIMEOUT を返す。サブプロセスが孫プロセス（Bash ツール等）を
   残さないよう `start_new_session=True` + `os.killpg` でツリーごと殺す。

6. **レート制限を適用する。** 最大 300 秒の高コストなサブプロセス実行を無制限に
   許さないため、skills.py と同じ `security.check_rate_limit` をトークン
   subject 単位で適用する（10 回/時）。
   要確認: 上限値はデプロイ運用で調整の余地あり。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional, Protocol

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from modal_hub.core import config
from modal_hub.core import security
from modal_hub.core import store
from modal_hub.routers import approval_gate

logger = logging.getLogger("hh_agent.dispatch")

router = APIRouter()

# `ApiError`/`error_response` は approval_gate.py の完全に汎用的な部品を
# そのまま再利用する（skills.py と同じ方針・重複実装を避ける）。
ApiError = approval_gate.ApiError
error_response = approval_gate.error_response

# リクエスト本文の上限（skills.py と同じ 64KB。既存パターンに倣う）。
MAX_BODY_BYTES = 64 * 1024

# oneshot サブプロセスのタイムアウト。Codexレビュー指摘（P2、2026-08-20）で
# 300秒から270秒へ短縮: 元の300秒だとModal Functionのリクエストタイムアウトと
# 一致・競合し、プラットフォームがこのハンドラの504応答より先にコンテナごと
# 打ち切る恐れがあった。504を確実に返すための余裕を持たせる。
ONESHOT_TIMEOUT_SECONDS = 270

# レート制限: トークン subject ごとの dispatch 実行回数（1時間あたり）。
DISPATCH_RATE_LIMIT = 10
DISPATCH_RATE_WINDOW_SECONDS = 3600

#: コンテナ内に焼き込まれた Corpus2Skill プラグインの置き場所（main.py の
#: add_local_dir で /opt/hh-agent/corpus2skill-plugin へコピーされる）。
#: ローカル（テスト・開発）ではリポジトリ相対パスへフォールバックする
#: （main.py の _mount_pwa と同じ「コンテナ優先 → ローカル」パターン）。
_CONTAINER_C2S_PLUGIN_PATH = Path("/opt/hh-agent/corpus2skill-plugin")
_LOCAL_C2S_PLUGIN_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / ".hermes"
    / "plugins"
    / "corpus2skill"
)


# ---------------------------------------------------------------------------
# ストア層: DispatchStore Protocol + 本番アダプタ
# ---------------------------------------------------------------------------


class DispatchStore(Protocol):
    """認証検証に必要なストア最小インターフェース。

    `security.CredentialStore`（get / put_if_absent / delete）と同形で、
    トークン検証の肯定リスト（`agent_session:<tid>`）の読み書きに使う。
    `modal_hub/core/store.py` のモジュール関数群はこのシグネチャと一致しており、
    そのまま渡せる。
    """

    def get(self, key: str) -> Optional[Any]: ...

    def put_if_absent(self, key: str, value: Any) -> bool: ...

    def delete(self, key: str) -> None: ...


class _LiveDispatchStore:
    def get(self, key: str) -> Optional[Any]:
        return store.get(key)

    def put_if_absent(self, key: str, value: Any) -> bool:
        return store.put_if_absent(key, value)

    def delete(self, key: str) -> None:
        store.delete(key)


_LIVE_STORE = _LiveDispatchStore()


# ---------------------------------------------------------------------------
# ヘルパー（skills.py の同一ヘルパー群を踏襲）
# ---------------------------------------------------------------------------


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization")
    if not header or not header.startswith("Bearer "):
        raise ApiError(401, "UNAUTHORIZED", "missing bearer token", retryable=False)
    return header[len("Bearer ") :].strip()


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


def _verify_dispatch_agent(request: Request, s: DispatchStore) -> security.AgentIdentity:
    """既存 `POST /api/skills/publish` と同じ認可の仕組み（Bearer 検証 +
    `verify_agent_token` + `require_scope`）。新しい認証方式は発明しない。

    署名鍵だけを `AGENTIC_OS_DISPATCH_KEY`（config.py）に分離する
    （タスク指示: 「鍵の名前空間は既存と衝突しない新規の環境変数名」）。
    """
    token = _bearer_token(request)
    dispatch_key = config.agentic_os_dispatch_key()
    if not dispatch_key:
        # 鍵が未設定 = fail-closed。401 のまま理由を区別する情報を与えない。
        raise ApiError(401, "UNAUTHORIZED", "agent token invalid", retryable=False)
    try:
        identity = security.verify_agent_token(
            s,
            token,
            signing_key=dispatch_key.encode("utf-8"),
        )
    except security.SecurityError as exc:
        raise ApiError(401, "UNAUTHORIZED", "agent token invalid", retryable=False) from exc

    try:
        security.require_scope(identity, security.SCOPE_DISPATCH)
    except security.InsufficientScopeError as exc:
        raise ApiError(
            403, "FORBIDDEN", f"token lacks the {security.SCOPE_DISPATCH} scope", retryable=False
        ) from exc

    return identity


def _resolve_plugin_source_dir() -> Path:
    """コンテナ内（焼き込み先）→ ローカル（リポジトリ相対）の順で解決する。"""
    if _CONTAINER_C2S_PLUGIN_PATH.is_dir():
        return _CONTAINER_C2S_PLUGIN_PATH
    return _LOCAL_C2S_PLUGIN_PATH


def _prepare_hermes_home(hermes_home: Path, plugin_src: Path) -> None:
    """一時 HERMES_HOME へ config.yaml と Corpus2Skill プラグインを配置する。

    config.yaml のキー:
        - `memory.provider: corpus2skill` — install_corpus2skill_plugin.ps1 が
          `hermes config set memory.provider corpus2skill` で使う実在キー。
        - `model.provider: anthropic` — hermes の実在プロバイダー名。Hub Secret
          の ANTHROPIC_API_KEY（env 経由で subprocess へ渡す）が使われる。
        - `model.default` — AGENTIC_OS_DISPATCH_MODEL が設定されていればその値、
          未設定なら `"claude-sonnet-5"` にフォールバックする（Codexレビュー
          指摘P1で修正、2026-08-20。未設定時に空のままだと
          hermes_cli.oneshot._run_agent() がモデルを解決できず必ず失敗していた）。
    """
    plugin_dir = hermes_home / "plugins" / "corpus2skill"
    if plugin_src.is_dir():
        plugin_dir.mkdir(parents=True, exist_ok=True)
        for item in plugin_src.iterdir():
            if item.name == "__pycache__":
                continue
            dst = plugin_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)
    else:
        # プラグインが焼き込まれていなくても実行は続行する
        # （plugin の is_available()=False → 記憶プロバイダ無しで走る）。
        logger.warning("corpus2skill plugin not found at %s; dispatch runs without memory", plugin_src)

    # Codexレビュー指摘（P1、2026-08-20）で修正: AGENTIC_OS_DISPATCH_MODEL 未設定
    # （ドキュメント上の既定運用）のとき model.default が空のままになり、
    # hermes_cli.oneshot._run_agent() がモデルを解決できず HERMES_RUN_FAILED で
    # 必ず失敗していた。未設定時のフォールバック既定値を常に設定する。
    _DEFAULT_DISPATCH_MODEL = "claude-sonnet-5"
    model_cfg: dict[str, Any] = {
        "provider": "anthropic",
        "default": config.agentic_os_dispatch_model() or _DEFAULT_DISPATCH_MODEL,
    }
    config_yaml = {
        "memory": {"provider": "corpus2skill"},
        "model": model_cfg,
    }
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(config_yaml, sort_keys=False), encoding="utf-8"
    )


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """`start_new_session=True` で起動したプロセスのグループごと殺す。

    タイムアウト時に孫プロセス（Bash ツール等）を残さないため。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _read_usage_session_id(usage_path: Path) -> Optional[str]:
    """usage-file JSON から session_id を抽出する。

    hermes_cli/oneshot.py の `_write_usage_file` が書く形式（失敗時にも書かれる）。
    ファイルが無い・読めない場合は None（応答の session_id は null になる）。
    """
    try:
        report = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    session_id = report.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def _run_hermes_oneshot(
    prompt: str,
    hermes_home: Path,
    *,
    timeout_seconds: int = ONESHOT_TIMEOUT_SECONDS,
) -> tuple[str, Optional[str]]:
    """`python -m hermes_cli.main -z <prompt> --usage-file <path>` を実行する。

    Returns:
        (最終応答テキスト, session_id)。session_id は usage-file から抽出
        （oneshot の stdout には出ない）。

    Raises:
        ApiError(504, GATEWAY_TIMEOUT): タイムアウト超過（プロセスツリーごと kill）。
        ApiError(500, HERMES_RUN_FAILED): exit code != 0。
    """
    usage_path = hermes_home / "usage.json"
    cmd = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "-z",
        prompt,
        "--usage-file",
        str(usage_path),
    ]

    # Codexレビュー指摘（P1、2026-08-20）で修正: os.environ.copy() は Hub 全体の
    # Modal Secret（HH_AGENT_TOKEN_SIGNING_KEY・PWA セッション/ペアリング鍵等）を
    # 丸ごとサブプロセスへ渡してしまっていた。oneshot は YOLO モードでツール実行
    # 可能なエージェントのため、悪意あるプロンプトが環境変数を読むだけで
    # dispatch 権限を Hub 全体の制御へエスカレーションできる経路になっていた。
    # ディスパッチの実行に必要な最小限だけを明示的に許可リストする。
    _SAFE_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
    env = {k: os.environ[k] for k in _SAFE_ENV_PASSTHROUGH if k in os.environ}
    env["HERMES_HOME"] = str(hermes_home)
    # API キーは環境変数経由でのみ渡す。値はログへ一切出さない。
    c2s_key = config.c2s_api_key()
    if c2s_key:
        # Hub Secret 側は C2S_API_KEY、Corpus2Skill プラグイン側は
        # CORPUS2SKILL_API_KEY を読む（.hermes/plugins/corpus2skill/
        # __init__.py の _ENV_API_KEY = "CORPUS2SKILL_API_KEY"）。
        # 同一値であることは install_corpus2skill_plugin.ps1 の注記
        # （"same value as C2S_API_KEY on the Corpus2Skill side"）で確認済み。
        env["CORPUS2SKILL_API_KEY"] = c2s_key
    anthropic_key = config.anthropic_api_key()
    if anthropic_key:
        env["ANTHROPIC_API_KEY"] = anthropic_key

    proc = subprocess.Popen(
        cmd,
        cwd=str(hermes_home),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # タイムアウト時にプロセスグループごと殺せるように
    )
    try:
        stdout, _stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        proc.wait()
        raise ApiError(
            504,
            "GATEWAY_TIMEOUT",
            f"hermes oneshot exceeded {timeout_seconds}s timeout",
            retryable=True,
        ) from None

    if proc.returncode != 0:
        # stderr の中身はプロンプト・機微情報を含み得るためログにも応答にも
        # 出さない（失敗理由は rc だけを記録する）。
        logger.warning("hermes oneshot exited with rc=%s", proc.returncode)
        raise ApiError(
            500,
            "HERMES_RUN_FAILED",
            "hermes oneshot run failed",
            retryable=True,
        )

    response_text = stdout.strip()
    session_id = _read_usage_session_id(usage_path)
    return response_text, session_id


# ---------------------------------------------------------------------------
# POST /api/dispatch/headless
# ---------------------------------------------------------------------------


async def _dispatch_headless_core(request: Request, s: DispatchStore) -> JSONResponse:
    identity = _verify_dispatch_agent(request, s)

    try:
        security.check_rate_limit(
            s, subject=identity.sub, limit=DISPATCH_RATE_LIMIT, window_seconds=DISPATCH_RATE_WINDOW_SECONDS
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
    prompt = _require_str(body, "prompt")

    # 完全ステートレス: 一時 HERMES_HOME を毎回作って使い捨てにする
    # （既存 modal_dashboard の永続 Volume / HERMES_HOME とは一切共有しない。
    #  落とし穴73: Modal Volume の symlink 実装を踏まない設計）。
    hermes_home = Path(tempfile.mkdtemp(prefix="hh-agent-dispatch-"))
    try:
        _prepare_hermes_home(hermes_home, _resolve_plugin_source_dir())
        # Codexレビュー指摘（P1、2026-08-20）で修正: _run_hermes_oneshot() は
        # subprocess.Popen(...).communicate(timeout=...) を使う同期関数であり、
        # このasyncハンドラから直接呼ぶと共有ASGIイベントループを最大300秒
        # ブロックしていた。同じコンテナが処理する承認ゲート・PWA・health
        # リクエストが、ディスパッチ実行中まったく進まなくなる安全性クリティカル
        # な不具合だったため、専用スレッドへオフロードする。
        response_text, session_id = await asyncio.to_thread(
            _run_hermes_oneshot, prompt, hermes_home
        )
    finally:
        shutil.rmtree(hermes_home, ignore_errors=True)

    return JSONResponse(
        status_code=200,
        content={"response": response_text, "session_id": session_id},
    )


@router.post("/api/dispatch/headless")
async def dispatch_headless(request: Request) -> JSONResponse:
    try:
        return await _dispatch_headless_core(request, _LIVE_STORE)
    except ApiError as exc:
        return error_response(exc)
    except Exception as exc:  # noqa: BLE001 — フェイルクローズ: 想定外は 500、握りつぶさない
        logger.exception("unexpected error in dispatch.dispatch_headless: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "internal error", "retryable": True}},
        )


__all__ = ["router", "DispatchStore", "ApiError"]
