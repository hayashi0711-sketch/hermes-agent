#!/usr/bin/env python
"""H-H Agent: agent_token.json / distill_token.json を再発行する。

`hh auth login`（未実装）の代替。エージェントトークンは spec 上 TTL 24h で
必ず失効する設計のため（`modal_hub/core/security.py` の
`AGENT_TOKEN_TTL_SECONDS`）、非対話運用ではこのスクリプトを Windows タスク
スケジューラ等で定期実行し、失効前に再発行し続ける前提で作る。

発行する3本（Phase1b spec 07 §5「認証」の scopes 分離を維持する）:
    - agent_token.json      : 承認フロー用（request/poll/claim/complete の
                              レガシーデフォルトスコープ）
    - distill_token.json    : Skill Distiller の publish フェーズ専用
                              （scopes=["publish"] のみ）
    - quarantine_read_token.json: `hh_skill_promote.py --remote <source>`
                              （S-08b）が読み取り専用で利用する
                              （scopes=["quarantine_read"] のみ）

このスクリプトは実際に本番 Modal の hh-agent-approvals Dict へ
agent_session レコードを書き込む。ローカルに `modal token` 認証済みで
なければ失敗する。トークン文字列そのものは標準出力・ログに出さない。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from modal_hub.core import security, store  # noqa: E402

HH_AGENT_HOME = Path(os.environ.get("USERPROFILE") or Path.home()) / ".hh-agent"
SECRET_ENV_PATH = REPO_ROOT / ".hh-secret.env"
SIGNING_KEY_VAR = "HH_AGENT_TOKEN_SIGNING_KEY"


def _load_signing_key() -> bytes:
    if SIGNING_KEY_VAR in os.environ:
        return os.environ[SIGNING_KEY_VAR].encode("utf-8")
    if not SECRET_ENV_PATH.is_file():
        raise RuntimeError(
            f"署名鍵が見つからない: 環境変数 {SIGNING_KEY_VAR} も {SECRET_ENV_PATH} も無い"
        )
    for line in SECRET_ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == SIGNING_KEY_VAR:
            return value.strip().strip('"').strip("'").encode("utf-8")
    raise RuntimeError(f"{SECRET_ENV_PATH} に {SIGNING_KEY_VAR} が無い")


def _parse_secret_env_var(name: str) -> str | None:
    """`.hh-secret.env` から `name=...` の値を1つ読む（無ければ None）。

    `_load_signing_key()` と同じパース方式（KEY=VALUE 行形式、コメント行
    スキップ、両端の引用符は剥がす）を踏襲する。SECRET_ENV_PATH が無い・
    該当キーが無い・値が空文字の場合は None を返す（例外にしない）。
    """
    env_value = os.environ.get(name)
    if env_value is not None:
        return env_value.strip().strip('"').strip("'") or None
    if not SECRET_ENV_PATH.is_file():
        return None
    for line in SECRET_ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            stripped = value.strip().strip('"').strip("'")
            return stripped or None
    return None


def load_ntfy_credentials() -> tuple[str | None, str | None]:
    """`.hh-secret.env` から NTFY_TOPIC / NTFY_TOKEN を読む共通ヘルパー。

    `scripts/hh_skill_sync.py`（Windowsスケジュールタスクから起動される。
    まだ存在しないファイルだが、将来そこから import されることを想定する）
    が再利用するため、`hh_issue_agent_token.py` 側にこのロジックを重複させず
    1箇所にまとめる。`_load_signing_key()` と同じ `.hh-secret.env` の
    パース方式（`SECRET_ENV_PATH` を使う）を踏襲すること。
    未設定の場合は該当する方を None で返す（例外にしない）。
    """
    topic = _parse_secret_env_var("NTFY_TOPIC")
    token = _parse_secret_env_var("NTFY_TOKEN")
    return topic, token


def _compute_workspace_id() -> str:
    import hashlib
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        root = proc.stdout.strip() if proc.returncode == 0 else str(REPO_ROOT)
    except OSError:
        root = str(REPO_ROOT)
    real = os.path.realpath(root or str(REPO_ROOT)).replace("\\", "/")
    return hashlib.sha256(real.encode("utf-8")).hexdigest()


def _write_token_file(path: Path, token: str) -> None:
    HH_AGENT_HOME.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(HH_AGENT_HOME), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"token": token}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main() -> int:
    signing_key = _load_signing_key()
    workspace_id = _compute_workspace_id()
    now = time.time()

    approval_token = security.issue_agent_token(
        store,
        sub="claude_code:desktop-haruki",
        source=security.SOURCE_CLAUDE_CODE,
        session_id="haruki-local-desktop",
        workspace_id=workspace_id,
        signing_key=signing_key,
        now=now,
    )
    _write_token_file(HH_AGENT_HOME / "agent_token.json", approval_token)

    distill_token = security.issue_agent_token(
        store,
        sub="hh-distill-worker",
        source=security.SOURCE_CLAUDE_CODE,
        session_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        signing_key=signing_key,
        scopes=[security.SCOPE_PUBLISH],
        now=now,
    )
    _write_token_file(HH_AGENT_HOME / "distill_token.json", distill_token)

    quarantine_read_token = security.issue_agent_token(
        store,
        sub="hh-skill-promote-remote",
        source=security.SOURCE_CLAUDE_CODE,
        session_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        signing_key=signing_key,
        scopes=[security.SCOPE_QUARANTINE_READ],
        now=now,
    )
    _write_token_file(HH_AGENT_HOME / "quarantine_read_token.json", quarantine_read_token)

    exp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + security.AGENT_TOKEN_TTL_SECONDS))
    message = f"[hh_issue_agent_token] agent_token.json / distill_token.json / quarantine_read_token.json を再発行しました（失効目安: {exp} ローカル時刻）"
    _log(message)
    return 0


def _log(message: str) -> None:
    """コンソール（タスクスケジューラでは非表示）とログファイルの両方へ出す。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("utf-8", errors="replace"))
    HH_AGENT_HOME.mkdir(parents=True, exist_ok=True)
    log_path = HH_AGENT_HOME / "token_refresh.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — タスクスケジューラの失敗検知用に必ずログへ残す
        _log(f"[hh_issue_agent_token] FAILED: {type(exc).__name__}: {exc}")
        raise
