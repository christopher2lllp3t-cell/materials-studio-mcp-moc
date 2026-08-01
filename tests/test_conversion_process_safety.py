from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, patch

from materials_studio_mcp.conversion_executor import _terminate_process_tree


class ConversionProcessSafetyTests(unittest.TestCase):
    def test_already_exited_process_is_not_killed(self) -> None:
        process = Mock(spec=subprocess.Popen)
        process.pid = 1234
        process.poll.return_value = 0
        result = _terminate_process_tree(process)
        self.assertFalse(result["requested"])
        process.kill.assert_not_called()

    @patch("materials_studio_mcp.conversion_executor.os.name", "nt")
    @patch("materials_studio_mcp.conversion_executor.subprocess.run")
    def test_windows_cleanup_targets_only_spawned_root_pid(self, run: Mock) -> None:
        process = Mock(spec=subprocess.Popen)
        process.pid = 4321
        process.poll.return_value = None
        run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        result = _terminate_process_tree(process)
        args = run.call_args.args[0]
        self.assertEqual(args[-4:], ["/PID", "4321", "/T", "/F"])
        self.assertEqual(result["pid"], 4321)
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(run.call_args.kwargs["close_fds"])


if __name__ == "__main__":
    unittest.main()
