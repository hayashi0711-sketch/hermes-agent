"""CF-Hermes-Hub用のoneshot実行（2026-09-05新規）。

CF-Hermes-Hub（Cloudflare Durable ObjectsでDiscord Gatewayの常時接続を
維持し、メンション/DM受信時だけModalを叩く構成、Obsidian Projects/
CF-Hermes-Hub参照）から、ルートプロフィール（`-p`指定なし、
`dashboard_server`が使うのと同じ`/opt/data`直下のconfig.yaml）へ1つの
プロンプトを実行し、1つの応答を返す。

`modal_dashboard/profile_oneshot.py`の`run_profile_oneshot_sync()`とほぼ
同じ実装（環境変数許可リスト・タイムアウト時のプロセスツリーkill）だが、
`-p <profile>`を付けない点が異なる（対象は名前付きプロフィールではなく
`dashboard_server`と同じルートのHermesインスタンスそのもの）。

Appをまたぐimportを避けるため、`_kill_process_tree()`・
`_read_usage_session_id()`は`profile_oneshot.py`・
`modal_hub/routers/dispatch.py`の実装と同一のコードをこのファイル内に
複製している（既存方針と同じ理由）。
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
# 許可リスト（modal_hub/routers/dispatch.py・profile_oneshot.pyと同じ方針・
# 同じリスト）。
_SAFE_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


def run_root_oneshot_sync(
    prompt: str,
    hermes_home: Path,
    *,
    timeout_seconds: int = 60,
) -> dict:
    """`python -m hermes_cli.main -z <prompt> --usage-file <path>` を実行する。

    CF-Hermes-HubはDiscordメッセージ1件あたり1回呼ばれる短命な用途のため、
    `run_profile_oneshot`（timeout=310秒）より短いデフォルトタイムアウト
    （60秒）にしている。呼び出し元（Discord Gateway Durable Object）は
    さらに短いHTTPタイムアウトを設けている想定。

    Returns:
        {"response": str, "session_id": str | None}

    Raises:
        TimeoutError: タイムアウト超過（プロセスツリーごとkill）。
        RuntimeError: exit code != 0。
    """
    usage_path = hermes_home / f".cf-hub-oneshot-usage-{os.getpid()}.json"
    # `hh_hermes.py`経由で起動する(素の`-m hermes_cli.main`ではない)。
    # Codexレビュー指摘(P1): 素のhermes_cli.mainを直接叩くと、
    # `hh_hermes.py`が担うD-14/D-20の起動時ガード(_patch_dashboard_update_gate
    # ・enforce_or_exit())を経由せず、Discord発の外部入力(プロンプトイン
    # ジェクションを含みうる)がpre_tool_call承認ゲート未検証のまま
    # ツールを実行できてしまう。`dashboard_server()`が`hh_hermes.py
    # dashboard`を起動するのと同じ入口を、oneshotモード(`-z`)でも使う。
    cmd = [
        sys.executable,
        "/opt/hermes/hh_hermes.py",
        "-z",
        prompt,
        "--usage-file",
        str(usage_path),
    ]

    env = {k: os.environ[k] for k in _SAFE_ENV_PASSTHROUGH if k in os.environ}
    # as_posix(): 実行先は常にModalコンテナ内のLinuxパス（/opt/data）。
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
