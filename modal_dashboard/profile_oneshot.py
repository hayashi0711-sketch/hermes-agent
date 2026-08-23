"""Profile Agent用のoneshot実行（2026-08-23新規）。

Agentic OS Hubの「Profile Agent」機能から、Hermesの本物のプロフィール
（永続Volume `hh-agent-dashboard-home` 上、`hermes profile create` 等で
作成された実プロフィール）に対して1つのプロンプトを実行し、1つの応答を
返す。

modal_hub/routers/dispatch.py の _run_hermes_oneshot() とほぼ同じ実装
（環境変数許可リスト・タイムアウト時のプロセスツリーkill）だが、以下が
異なる:
  - 使い捨て一時ディレクトリではなく、hh-agent-dashboard-home Volumeの
    実マウント先（modal_dashboard/app.py の _DASHBOARD_MOUNT_PATH）を
    そのまま HERMES_HOME として使う。
  - `-p <profile>` を追加し、対象プロフィールを明示する。
  - Corpus2Skillプラグインの配置・config.yaml生成は行わない（対象は
    既に `hermes profile create` 等で作成済みの実プロフィールであり、
    config.yaml・.env は既にそのプロフィール配下に存在するため）。

Appをまたぐimportを避けるため、_kill_process_tree()・
_read_usage_session_id() は modal_hub/routers/dispatch.py の実装と
同一のコードをこのファイル内に複製している（chatterbox_service /
qwen3_tts_service が独立している既存方針と同じ理由）。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """`start_new_session=True` で起動したプロセスのグループごと殺す。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _read_usage_session_id(usage_path: Path) -> Optional[str]:
    """usage-file JSON から session_id を抽出する（失敗時は None）。"""
    try:
        report = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    session_id = report.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


# Hub全体のModal Secret（署名鍵等）を丸ごとサブプロセスへ渡さないための
# 許可リスト（modal_hub/routers/dispatch.py と同じ方針・同じリスト）。
_SAFE_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


def run_profile_oneshot_sync(
    profile: str,
    prompt: str,
    hermes_home: Path,
    *,
    timeout_seconds: int = 300,
) -> dict:
    """`python -m hermes_cli.main -p <profile> -z <prompt> --usage-file <path>` を実行する。

    Returns:
        {"response": str, "session_id": str | None}

    Raises:
        TimeoutError: タイムアウト超過（プロセスツリーごとkill）。
        RuntimeError: exit code != 0。
    """
    usage_path = hermes_home / f".profile-oneshot-usage-{os.getpid()}.json"
    cmd = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "-p",
        profile,
        "-z",
        prompt,
        "--usage-file",
        str(usage_path),
    ]

    env = {k: os.environ[k] for k in _SAFE_ENV_PASSTHROUGH if k in os.environ}
    # as_posix(): 実行先は常にModalコンテナ内のLinuxパス（/opt/data）。
    # Windowsでローカル実行した場合も str() だと `\opt\data` になるため、
    # POSIX形式へ明示的に揃える（Linux上では str() と同一挙動）。
    env["HERMES_HOME"] = hermes_home.as_posix()

    proc = subprocess.Popen(
        cmd,
        cwd=hermes_home.as_posix(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        proc.wait()
        raise TimeoutError(f"hermes oneshot exceeded {timeout_seconds}s timeout") from None

    if proc.returncode != 0:
        raise RuntimeError(f"hermes oneshot run failed (rc={proc.returncode})")

    response_text = stdout.strip()
    session_id = _read_usage_session_id(usage_path)
    try:
        usage_path.unlink(missing_ok=True)
    except OSError:
        pass
    return {"response": response_text, "session_id": session_id}
