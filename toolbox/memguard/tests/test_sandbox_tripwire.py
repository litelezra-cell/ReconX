"""Tripwire: MEMGUARD_STATE_DIR must be set and must not point at the real state before any
other test runs. A sandbox that depends on the way the suite is invoked is not a sandbox;
enforcement here is mechanical (it fails loudly), not just a note in the README."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from memguard import state


class SandboxTripwireTests(unittest.TestCase):
    def test_state_dir_is_sandboxed(self) -> None:
        self.assertTrue(
            os.environ.get("MEMGUARD_STATE_DIR"),
            "MEMGUARD_STATE_DIR is not set; the tests would write to the real "
            "~/.local/state/memguard. Run via run_tests.sh (or export MEMGUARD_STATE_DIR "
            "to a temporary directory).",
        )
        real_default = (Path.home() / ".local" / "state" / "memguard").resolve()
        self.assertNotEqual(
            state.memguard_state_dir().resolve(), real_default,
            f"memguard_state_dir() points at the real state: {state.memguard_state_dir()}",
        )

    def test_is_real_state_dir_reports_false_under_sandbox(self) -> None:
        self.assertFalse(state.is_real_state_dir())

    def test_ledger_and_daemons_paths_live_under_the_sandbox(self) -> None:
        sandbox = state.memguard_state_dir().resolve()
        self.assertEqual(state.default_ledger_path().resolve().parent, sandbox)
        self.assertEqual(state.daemons_dir().resolve().parent, sandbox)


if __name__ == "__main__":
    unittest.main()
