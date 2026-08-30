from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import advisor_hook  # noqa: E402
import advisor_process  # noqa: E402


def pid_exists(pid: int) -> bool:
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class AdvisorProcessTreeTests(unittest.TestCase):
    def test_terminate_process_tree_kills_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pid_file = Path(raw) / "child.pid"
            code = (
                "import subprocess,sys,time;"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
                "open(sys.argv[1],'w').write(str(child.pid));"
                "time.sleep(60)"
            )
            parent = subprocess.Popen(
                [sys.executable, "-c", code, str(pid_file)],
                **advisor_process.advisor_process_group_kwargs(),
            )
            deadline = time.monotonic() + 10
            while not pid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.is_file())
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self.assertTrue(pid_exists(child_pid))

            advisor_process.terminate_process_tree(parent)

            deadline = time.monotonic() + 10
            while pid_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(pid_exists(child_pid))
            self.assertIsNotNone(parent.poll())


if __name__ == "__main__":
    unittest.main()
