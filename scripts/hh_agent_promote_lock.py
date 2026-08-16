"""scripts/hh_agent_promote_lock.py — promote/sync 共有のプロセス間ロック（S-10 手順0）。

設計上の位置づけ:
    - 親設計書 docs/hh-agent/03_Architecture.md S-10 手順0（ロックの設計）
    - `scripts/hh_skill_promote.py` と `scripts/hh_skill_sync.py`（別担当が
      並行実装中）の両方が使う共有プリミティブのため、どちらのファイルにも
      属させず独立モジュールとする。

== 何を守るのか ==

promote と sync は同じ資源を共有する — `promote_staging/<name>`、
配置先 `~/.hermes/skills/<name>/`、`promote_backups/`、`promote_log.jsonl`、
`promote_receipts/`、`promote_seq.json`、`skill_sync_state.json`。
排他しないと、`_write_staging()` の検証後から `install_staged_skill()` までに
別プロセスが同じ staging パスを差し替え、「人間が確認したバイト列」「実際に
配置されたバイト列」「監査に記録した digest」が食い違う。

== 方式 ==

- ロックファイル `~/.hh-agent/locks/skill_promote.lock`（O_EXCL 作成）。
- ロックファイルには所有者 nonce（uuid4）・PID・開始時刻・heartbeat 時刻を書く。
- 保持側は 30 秒ごとに heartbeat を更新する（バックグラウンドスレッド。
  クリティカルセクションが 30 秒を大きく超えても更新し続ける）。
- 奪取してよいのは「heartbeat が 5 分以上更新されておらず、かつ記録された
  PID が生存していない」場合のみ。奪取時は所有者 nonce を照合してから
  置き換え、ログに残す。
- 解放は所有者 nonce が一致するときだけ。`try/finally` で行い、
  `BaseException`（`KeyboardInterrupt` 含む）でも必ず実行する。
- 同一プロセス内での再入は想定しない（呼び出し側が二重に取らないよう
  注意する。このモジュール自体は再入検出を持たない）。

この方式（ローカルファイル・O_EXCL・PID 生存確認）が有効なのは Windows
ネイティブ側だけ（同一 OS 上の単一ファイルシステム。S-10 手順0）。Modal
側のロック（`modal.Dict`）とは別の名前空間であり、互いに関知しない。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

#: heartbeat の更新間隔（秒）。クリティカルセクションがこれを超えても
#: バックグラウンドスレッドが更新し続ける。
HEARTBEAT_INTERVAL_SECONDS = 30.0

#: この秒数 heartbeat が更新されていないロックは奪取候補になる。
STALE_AFTER_SECONDS = 5 * 60.0

#: ブロッキング取得時の再試行間隔。
_RETRY_INTERVAL_SECONDS = 0.25


class PromoteLockTimeout(RuntimeError):
    """`promote_lock(timeout=...)` が期限内にロックを取得できなかった。"""


class _LockHeldError(RuntimeError):
    """直近の取得試行でロックが他人に保持されていた。"""


def _pid_is_alive(pid: int) -> bool:
    """PID 生存確認。`os.kill(pid, 0)` 相当（Windows では不可のため
    psutil を使う。プロジェクトの既存依存 `psutil==7.2.2` を優先し、
    無ければプラットフォーム別の代替手段へフォールバックする）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import psutil  # noqa: PLC0415 — 実行時 import（任意依存として扱う）

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if sys.platform == "win32":
        return _pid_is_alive_win32(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在するが別ユーザーのプロセス
    except OSError:
        return False
    return True


def _pid_is_alive_win32(pid: int) -> bool:
    """ctypes で OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) を試み、
    プロセスハンドルが開けたら生存とみなす。"""
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
        )
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:  # noqa: BLE001 — 生存確認のフォールバック。失敗は「生存とみなさない」
        return False


def _lock_path(base: Path | None) -> Path:
    home = base if base is not None else Path(
        os.environ.get("USERPROFILE") or Path.home()
    ) / ".hh-agent"
    return home / "locks" / "skill_promote.lock"


def _write_lock_file(path: Path, nonce: str, pid: int, started_at: float, heartbeat_at: float) -> None:
    """ロックファイルを原子的に書く（temp + os.replace。torn read 防止）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "nonce": nonce,
                    "pid": pid,
                    "started_at": started_at,
                    "heartbeat_at": heartbeat_at,
                },
                f,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_lock_file(path: Path) -> dict | None:
    """ロックファイルを読む。存在しない/解釈できない場合は None。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _is_stale(data: dict) -> bool:
    """heartbeat が 5 分以上更新されておらず、かつ PID が生存していない。"""
    try:
        heartbeat_at = float(data["heartbeat_at"])
        pid = int(data["pid"])
    except (KeyError, TypeError, ValueError):
        return False  # 解釈できないロックは奪取しない（フェイルクローズ）
    if time.time() - heartbeat_at < STALE_AFTER_SECONDS:
        return False
    return not _pid_is_alive(pid)


def _acquire_once(path: Path, nonce: str, pid: int) -> bool:
    """O_EXCL でロックファイルを作る。既に存在すれば奪取条件を評価する。

    Returns:
        True ならこのプロセスがロックを所有している。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        existing = _read_lock_file(path)
        if existing is None:
            # 解釈できないロックは奪取しない（フェイルクローズ。人間が消す）。
            return False
        if not _is_stale(existing):
            return False
        # 奪取: 所有者 nonce を照合してから置き換える（read→replace の間に
        # 別プロセスが差し替えていたら nonce が変わり、置き換えを諦める）。
        current = _read_lock_file(path)
        if current is None or current.get("nonce") != existing.get("nonce"):
            return False
        print(
            f"[hh_agent_promote_lock] taking over stale lock "
            f"(pid={existing.get('pid')!r}, heartbeat_at={existing.get('heartbeat_at')!r})",
            file=sys.stderr,
        )
        _write_lock_file(path, nonce, pid, time.time(), time.time())
        return True
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(
            {"nonce": nonce, "pid": pid, "started_at": time.time(), "heartbeat_at": time.time()},
            f,
        )
        f.flush()
        os.fsync(f.fileno())
    return True


def _release(path: Path, nonce: str) -> None:
    """所有者 nonce が一致するときだけロックを解放する。"""
    data = _read_lock_file(path)
    if data is None:
        return
    if data.get("nonce") != nonce:
        return  # 他人のロックを解放しない
    try:
        os.unlink(path)
    except OSError:
        pass


@contextlib.contextmanager
def promote_lock(*, base: Path | None = None, timeout: float | None = 60.0, nonblocking: bool = False):
    """`~/.hh-agent/locks/skill_promote.lock` を取得する（S-10 手順0）。

    - `nonblocking=True`: 取れなければ即座に諦め、`yield False` する
      （呼び出し側で「今回はスキップ」扱いにする）。
    - `nonblocking=False`: `timeout` 秒まで待つ。取れなければ
      `PromoteLockTimeout`。`timeout=None` は無期限待ち。
    - 取得できた場合は `yield True` し、クリティカルセクション実行中は
      バックグラウンドスレッドが 30 秒ごとに heartbeat を更新する。
    - 解放は `try/finally` で行い、`BaseException`（`KeyboardInterrupt`
      を含む）でも必ず実行する。解放は所有者 nonce が一致するときだけ。

    Args:
        base: `~/.hh-agent` の代わりに使うベースディレクトリ（テスト用）。
        timeout: ブロッキング取得の待ち時間（秒）。None は無期限。
        nonblocking: True なら取得試行 1 回で諦める。
    """
    nonce = uuid.uuid4().hex
    pid = os.getpid()
    path = _lock_path(base)

    if nonblocking:
        acquired = _acquire_once(path, nonce, pid)
        if not acquired:
            yield False
            return
    else:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if _acquire_once(path, nonce, pid):
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise PromoteLockTimeout(
                    "could not acquire ~/.hh-agent/locks/skill_promote.lock "
                    f"within {timeout} seconds (another promote/sync process holds it)"
                )
            time.sleep(_RETRY_INTERVAL_SECONDS)

    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)
            if stop_event.is_set():
                return
            data = _read_lock_file(path)
            if data is not None and data.get("nonce") == nonce:
                _write_lock_file(path, nonce, pid, data.get("started_at", time.time()), time.time())

    thread = threading.Thread(target=_heartbeat_loop, name="promote-lock-heartbeat", daemon=True)
    thread.start()
    try:
        yield True
    finally:
        stop_event.set()
        thread.join(timeout=2.0)
        _release(path, nonce)
