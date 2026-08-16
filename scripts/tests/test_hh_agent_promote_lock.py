"""scripts/tests/test_hh_agent_promote_lock.py — 共有プロセス間ロック（S-10 手順0）。

検証する不変条件（.lane_c_wave3_promote_task.md §1）:
    1. 生きている heartbeat のロックを奪取しないこと
    2. 旧所有者が復帰しても他人のロックを解放しないこと
    3. nonblocking=True で取れない場合は即座に False（例外にしない）
    4. timeout 付きで取れない場合 PromoteLockTimeout
    5. BaseException がクリティカルセクション内で起きても解放されること
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import hh_agent_promote_lock as lock_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch):
    """heartbeat スレッドの sleep を 10ms にして、with 文終了時の
    join(timeout=2.0) が実待ちにならないようにする。"""
    monkeypatch.setattr(lock_mod, "HEARTBEAT_INTERVAL_SECONDS", 0.01)


def _dead_pid() -> int:
    """確実に終了したプロセスの PID を返す（PID 再利用は許容する）。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


def _write_lock_manual(
    path: Path, nonce: str, pid: int, heartbeat_at: float, started_at: float = 0.0
) -> None:
    """テスト用にロックファイルを直接書く（heartbeat 時刻を任意に制御する）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "nonce": nonce,
                "pid": pid,
                "started_at": started_at or time.time(),
                "heartbeat_at": heartbeat_at,
            }
        ),
        encoding="utf-8",
    )


def _lock_path(tmp_path: Path) -> Path:
    # promote_lock(base=tmp_path) のロックは base/locks/skill_promote.lock
    # （base 指定時は base 自体が「.hh-agent に相当するホーム」になる）。
    return tmp_path / "locks" / "skill_promote.lock"


# ---------------------------------------------------------------------------
# 1. 生きている heartbeat のロックを奪取しない
# ---------------------------------------------------------------------------


def test_no_takeover_of_live_heartbeat_lock(tmp_path):
    path = _lock_path(tmp_path)
    _write_lock_manual(path, nonce="owner-a", pid=lock_mod.os.getpid(), heartbeat_at=time.time())

    # 内部関数経由
    assert lock_mod._acquire_once(path, nonce="b", pid=lock_mod.os.getpid()) is False
    # 公開 API（nonblocking）経由
    with lock_mod.promote_lock(base=tmp_path, nonblocking=True) as acquired:
        assert acquired is False
    # 奪取されていない（nonce は owner-a のまま）
    data = lock_mod._read_lock_file(path)
    assert data["nonce"] == "owner-a"


def test_no_takeover_when_heartbeat_fresh_but_pid_dead(tmp_path):
    """heartbeat が新しい場合は PID が死んでいても奪取しない（生存確認は
    AND 条件。heartbeat が 5 分未満なら所有者は単に処理中とみなす）。"""
    path = _lock_path(tmp_path)
    _write_lock_manual(path, nonce="owner-a", pid=_dead_pid(), heartbeat_at=time.time())
    assert lock_mod._acquire_once(path, nonce="b", pid=lock_mod.os.getpid()) is False


# ---------------------------------------------------------------------------
# 2. 旧所有者が復帰しても他人のロックを解放しない
# ---------------------------------------------------------------------------


def test_old_owner_release_does_not_free_others_lock(tmp_path):
    path = _lock_path(tmp_path)
    # 旧所有者 A のロックが stale（heartbeat 古い & PID 死亡）→ B が奪取する。
    _write_lock_manual(path, nonce="owner-a", pid=_dead_pid(), heartbeat_at=time.time() - 3600)
    with lock_mod.promote_lock(base=tmp_path, nonblocking=True) as acquired:
        assert acquired is True
        b_nonce = lock_mod._read_lock_file(path)["nonce"]
        assert b_nonce != "owner-a"

        # 「復帰した旧所有者 A」が自分の nonce で解放を試みる。
        lock_mod._release(path, "owner-a")
        # B のロックは残っている（nonce 一致でなければ解放しない）。
        assert path.exists()
        assert lock_mod._read_lock_file(path)["nonce"] == b_nonce

    # B が正規に抜けた後は消えている（nonce 一致の解放は機能する）。
    assert not path.exists()


# ---------------------------------------------------------------------------
# 3. nonblocking=True で取れない場合は即座に False
# ---------------------------------------------------------------------------


def test_nonblocking_returns_false_immediately_when_held(tmp_path):
    path = _lock_path(tmp_path)
    _write_lock_manual(path, nonce="owner-a", pid=lock_mod.os.getpid(), heartbeat_at=time.time())
    started = time.monotonic()
    with lock_mod.promote_lock(base=tmp_path, nonblocking=True) as acquired:
        elapsed = time.monotonic() - started
        assert acquired is False
    # 再試行ループに入らず即座に返る（ブロッキング取得の 0.25s 再試行間隔より大幅に短い）。
    assert elapsed < 0.2


def test_nonblocking_returns_true_when_free(tmp_path):
    with lock_mod.promote_lock(base=tmp_path, nonblocking=True) as acquired:
        assert acquired is True
    assert not _lock_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# 4. timeout 付きで取れない場合 PromoteLockTimeout
# ---------------------------------------------------------------------------


def test_blocking_timeout_raises_promote_lock_timeout(tmp_path):
    path = _lock_path(tmp_path)
    _write_lock_manual(path, nonce="owner-a", pid=lock_mod.os.getpid(), heartbeat_at=time.time())
    with pytest.raises(lock_mod.PromoteLockTimeout):
        with lock_mod.promote_lock(base=tmp_path, timeout=0.1):
            pytest.fail("should not acquire")
    # 他人のロックは無傷。
    assert lock_mod._read_lock_file(path)["nonce"] == "owner-a"


# ---------------------------------------------------------------------------
# 5. BaseException が中で起きても解放される
# ---------------------------------------------------------------------------


def test_release_on_base_exception(tmp_path):
    path = _lock_path(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        with lock_mod.promote_lock(base=tmp_path, timeout=5):
            assert path.exists()
            raise KeyboardInterrupt
    assert not path.exists()


def test_release_on_plain_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with lock_mod.promote_lock(base=tmp_path, timeout=5):
            raise RuntimeError("boom")
    assert not _lock_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# 補足: heartbeat 更新がクリティカルセクション中も継続する
# ---------------------------------------------------------------------------


def test_heartbeat_keeps_updating_during_long_critical_section(tmp_path):
    path = _lock_path(tmp_path)
    with lock_mod.promote_lock(base=tmp_path, timeout=5) as acquired:
        assert acquired is True
        first = lock_mod._read_lock_file(path)["heartbeat_at"]
        time.sleep(0.05)  # HEARTBEAT_INTERVAL_SECONDS=0.01 なので数回更新される
        second = lock_mod._read_lock_file(path)["heartbeat_at"]
        assert second > first
    assert not path.exists()
