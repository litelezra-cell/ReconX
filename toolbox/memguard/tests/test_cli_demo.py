"""Automated version of the live demonstration without models (see protocol.md, section 4):
runs `memguard-demo` twice as **separate real OS processes** (not two objects inside the
same test process; the point is to test **cross-process** ledger sharing). Both processes
inherit the suite's sandboxed `MEMGUARD_STATE_DIR` (`tests/__init__.py`), so the machine's
real state is never touched. **No model at all**, only a simulated Lease.

**Sizes are derived from the actually-free memory right now, not fixed numbers:** the
available memory on a busy development machine fluctuates, and a test that depends on a
fixed amount on a machine under unknown load is an anti-pattern. So: measure now, and
derive two sizes relative to what is usable, which makes the outcome deterministic
regardless of how much is free at that moment."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from memguard import broker as broker_module
from memguard import memprobe

_MEMGUARD_ROOT = Path(__file__).resolve().parents[1]
_DEMO_CODE = "from memguard.cli import main_demo; import sys; sys.exit(main_demo())"

_SYSTEM_MARGIN_GB = broker_module.DEFAULT_SYSTEM_MARGIN_GB  # 16.0, the broker's actual default


def _run_demo(project: str, gb: float, hold_seconds: float, *, wait: bool = False):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_MEMGUARD_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    args = [sys.executable, "-c", _DEMO_CODE, "--project", project, "--gb", f"{gb:.3f}", "--hold-seconds", str(hold_seconds)]
    proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if wait:
        out, _ = proc.communicate(timeout=hold_seconds + 15)
        return proc.returncode, out
    return proc


class CrossProcessDemoTests(unittest.TestCase):
    def test_second_process_refused_then_granted_after_first_exits(self) -> None:
        available_now = memprobe.available_memory_gb()
        self.assertIsNotNone(available_now, "could not measure free memory right now; cannot run the live demonstration")
        usable_now = available_now - _SYSTEM_MARGIN_GB
        self.assertGreater(usable_now, 4.0, f"less than 4GB usable right now ({usable_now:.1f}GB); the machine is too busy for this demonstration")

        # `first` alone fits comfortably; first + second together exceed what is usable now,
        # regardless of exactly how much is free at that moment (0.5 + 0.7 = 1.2 > 1.0 always).
        first_gb = usable_now * 0.5
        second_gb = usable_now * 0.7

        first = _run_demo("alpha", first_gb, hold_seconds=4.0)
        try:
            deadline = time.time() + 6.0
            first_out = ""
            while time.time() < deadline:
                line = first.stdout.readline()
                if line:
                    first_out += line
                if "granted" in first_out:
                    break
            self.assertIn("granted", first_out, f"the first process did not confirm the lease in time: {first_out!r}")

            rc, second_out = _run_demo("beta", second_gb, hold_seconds=1.0, wait=True)
            self.assertNotEqual(rc, 0, f"the second process was expected to be refused: {second_out}")
            self.assertIn("refused", second_out)
            self.assertIn("alpha", second_out)

            first_rc = first.wait(timeout=10)
            self.assertEqual(first_rc, 0)

            rc3, third_out = _run_demo("beta", second_gb, hold_seconds=1.0, wait=True)
            self.assertEqual(rc3, 0, f"after the release, the third attempt was expected to be granted: {third_out}")
        finally:
            if first.poll() is None:
                first.kill()
                first.wait(timeout=5)
            if first.stdout is not None:
                first.stdout.close()


if __name__ == "__main__":
    unittest.main()
