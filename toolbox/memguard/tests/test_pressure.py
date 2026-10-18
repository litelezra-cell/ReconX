"""Tests for `memguard.pressure`: level injection (`inject_level_for_test`), not a real kernel
event (that requires root; see the pressure.py docstring)."""

from __future__ import annotations

import unittest

from memguard.pressure import MemoryPressureMonitor, PressureLevel


class InjectLevelTests(unittest.TestCase):
    def test_starts_unknown(self) -> None:
        monitor = MemoryPressureMonitor()
        self.assertEqual(monitor.current_level(), PressureLevel.UNKNOWN)

    def test_inject_updates_current_level(self) -> None:
        monitor = MemoryPressureMonitor()
        monitor.inject_level_for_test(PressureLevel.WARN)
        self.assertEqual(monitor.current_level(), PressureLevel.WARN)

    def test_listener_is_notified_with_old_and_new(self) -> None:
        monitor = MemoryPressureMonitor()
        seen = []
        monitor.add_listener(lambda old, new: seen.append((old, new)))
        monitor.inject_level_for_test(PressureLevel.CRITICAL)
        self.assertEqual(seen, [(PressureLevel.UNKNOWN, PressureLevel.CRITICAL)])

    def test_setting_the_same_level_does_not_notify_again(self) -> None:
        monitor = MemoryPressureMonitor()
        monitor.inject_level_for_test(PressureLevel.NORMAL)
        seen = []
        monitor.add_listener(lambda old, new: seen.append((old, new)))
        monitor.inject_level_for_test(PressureLevel.NORMAL)
        self.assertEqual(seen, [])

    def test_listener_exception_does_not_break_monitor(self) -> None:
        monitor = MemoryPressureMonitor()

        def _boom(_old, _new):
            raise RuntimeError("boom")

        monitor.add_listener(_boom)
        monitor.inject_level_for_test(PressureLevel.WARN)  # must not raise
        self.assertEqual(monitor.current_level(), PressureLevel.WARN)


if __name__ == "__main__":
    unittest.main()
