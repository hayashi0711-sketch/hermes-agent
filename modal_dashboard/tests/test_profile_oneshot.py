"""modal_dashboard/profile_oneshot.py の単体テスト。

実サブプロセス（hermes CLI）は起動しない。subprocess.Popenをモックし、
コマンド構築・環境変数・タイムアウト処理を検証する。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modal_dashboard.profile_oneshot import run_profile_oneshot_sync


class _FakeCompletedProcess:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.pid = 12345


class RunProfileOneshotTests(unittest.TestCase):
    def test_builds_command_with_profile_flag_and_hermes_home(self):
        captured = {}

        def fake_popen(cmd, cwd, env, stdout, stderr, text, start_new_session):
            captured["cmd"] = cmd
            captured["env"] = env
            captured["cwd"] = cwd
            proc = mock.MagicMock()
            proc.communicate.return_value = ("こんにちは、テストです", "")
            proc.returncode = 0
            return proc

        with mock.patch("subprocess.Popen", side_effect=fake_popen), \
                mock.patch(
                    "modal_dashboard.profile_oneshot._read_usage_session_id",
                    return_value="sess-123",
                ):
            result = run_profile_oneshot_sync(
                "coder", "hello", Path("/opt/data")
            )

        self.assertEqual(result, {"response": "こんにちは、テストです", "session_id": "sess-123"})
        self.assertIn("-p", captured["cmd"])
        self.assertIn("coder", captured["cmd"])
        self.assertIn("-z", captured["cmd"])
        self.assertIn("hello", captured["cmd"])
        self.assertEqual(captured["env"]["HERMES_HOME"], "/opt/data")
        self.assertEqual(captured["cwd"], "/opt/data")

    def test_timeout_kills_process_tree_and_raises(self):
        import subprocess

        def fake_popen(cmd, cwd, env, stdout, stderr, text, start_new_session):
            proc = mock.MagicMock()
            proc.communicate.side_effect = subprocess.TimeoutExpired(cmd, 300)
            return proc

        with mock.patch("subprocess.Popen", side_effect=fake_popen), \
                mock.patch("modal_dashboard.profile_oneshot._kill_process_tree") as kill_mock:
            with self.assertRaises(TimeoutError):
                run_profile_oneshot_sync("coder", "hello", Path("/opt/data"), timeout_seconds=1)
        kill_mock.assert_called_once()

    def test_nonzero_exit_raises_runtime_error(self):
        def fake_popen(cmd, cwd, env, stdout, stderr, text, start_new_session):
            proc = mock.MagicMock()
            proc.communicate.return_value = ("", "some error")
            proc.returncode = 1
            return proc

        with mock.patch("subprocess.Popen", side_effect=fake_popen):
            with self.assertRaises(RuntimeError):
                run_profile_oneshot_sync("coder", "hello", Path("/opt/data"))

    def test_env_passthrough_is_allowlisted(self):
        os.environ["SOME_UNRELATED_SECRET"] = "should-not-leak"
        captured = {}

        def fake_popen(cmd, cwd, env, stdout, stderr, text, start_new_session):
            captured["env"] = env
            proc = mock.MagicMock()
            proc.communicate.return_value = ("ok", "")
            proc.returncode = 0
            return proc

        try:
            with mock.patch("subprocess.Popen", side_effect=fake_popen), \
                    mock.patch(
                        "modal_dashboard.profile_oneshot._read_usage_session_id",
                        return_value=None,
                    ):
                run_profile_oneshot_sync("coder", "hello", Path("/opt/data"))
        finally:
            del os.environ["SOME_UNRELATED_SECRET"]

        self.assertNotIn("SOME_UNRELATED_SECRET", captured["env"])


if __name__ == "__main__":
    unittest.main()
