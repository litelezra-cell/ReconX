"""Real integration against this machine (read-only). Sensors tied to a
specific host application live in consuming projects, not here (see the sensors.py docstring)."""

from __future__ import annotations

import os
import unittest

from memguard import sensors


class ProcessScanningTests(unittest.TestCase):
    def test_list_pids_includes_self(self) -> None:
        self.assertIn(os.getpid(), sensors.list_pids())

    def test_pid_executable_path_of_self_is_resolved(self) -> None:
        path = sensors.pid_executable_path(os.getpid())
        self.assertIsNotNone(path)
        self.assertTrue(len(path) > 0)

    def test_find_pid_by_path_suffix_finds_self(self) -> None:
        my_path = sensors.pid_executable_path(os.getpid())
        suffix = my_path[-8:]  # unique enough for this test
        found = sensors.find_pid_by_path_suffix(suffix)
        self.assertEqual(found, os.getpid())

    def test_find_pid_by_path_suffix_returns_none_when_absent(self) -> None:
        self.assertIsNone(sensors.find_pid_by_path_suffix("no-such-binary-xyz-123"))

    def test_proc_rss_bytes_of_self_is_positive(self) -> None:
        rss = sensors.proc_rss_bytes(os.getpid())
        self.assertIsNotNone(rss)
        self.assertGreater(rss, 0)

    def test_proc_rss_bytes_of_dead_pid_is_none(self) -> None:
        self.assertIsNone(sensors.proc_rss_bytes(99_999_999))


class HeavyProcessesTests(unittest.TestCase):
    def test_heavy_processes_returns_list_sorted_descending(self) -> None:
        heavy = sensors.heavy_processes(threshold_bytes=1024)  # tiny threshold: almost every process qualifies
        self.assertIsInstance(heavy, list)
        footprints = [p["footprint_gb"] for p in heavy]
        self.assertEqual(footprints, sorted(footprints, reverse=True))

    def test_absurdly_high_threshold_returns_empty(self) -> None:
        self.assertEqual(sensors.heavy_processes(threshold_bytes=10 * 1024**4), [])  # 10TB


class SystemMemoryStateTests(unittest.TestCase):
    def test_total_physical_memory_bytes_is_positive(self) -> None:
        self.assertGreater(sensors.total_physical_memory_bytes(), 0)

    def test_system_memory_state_fields_are_consistent(self) -> None:
        st = sensors.system_memory_state()
        self.assertEqual(st.free_bytes, st.pages_free * st.page_size_bytes)
        self.assertGreaterEqual(st.swap_total_bytes, st.swap_used_bytes)


class BatteryPowerTests(unittest.TestCase):
    def test_on_battery_power_returns_a_bool(self) -> None:
        # The value is machine-dependent; only check that it is measured and does not raise.
        self.assertIsInstance(sensors.on_battery_power(), bool)


if __name__ == "__main__":
    unittest.main()
