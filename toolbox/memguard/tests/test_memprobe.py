"""Real integration against this machine (read-only: ctypes / sysctl, no mutation). Verifies that
the package itself is sound, independent of any consuming project."""

from __future__ import annotations

import os
import unittest

from memguard import memprobe


class PageSizeTests(unittest.TestCase):
    def test_page_size_is_a_sane_power_of_two(self) -> None:
        ps = memprobe.page_size_bytes()
        self.assertGreater(ps, 0)
        self.assertEqual(ps & (ps - 1), 0, f"page_size {ps} is not a power of 2")


class TotalRamTests(unittest.TestCase):
    def test_total_ram_gb_is_measured_not_none_on_macos(self) -> None:
        total = memprobe.total_ram_gb()
        self.assertIsNotNone(total)
        self.assertGreater(total, 0.0)


class MachineMemoryTests(unittest.TestCase):
    def test_machine_memory_fields_are_non_negative(self) -> None:
        mem = memprobe.machine_memory()
        self.assertIsNotNone(mem)
        for field in ("free", "inactive", "purgeable", "speculative", "wired", "compressor"):
            value = getattr(mem, field)
            self.assertIsNotNone(value, field)
            self.assertGreaterEqual(value, 0.0, field)

    def test_reclaimable_is_sum_of_four_components(self) -> None:
        mem = memprobe.machine_memory()
        expected = round(mem.free + mem.inactive + mem.purgeable + (mem.speculative or 0.0), 3)
        self.assertEqual(mem.reclaimable, expected)

    def test_available_memory_gb_matches_reclaimable(self) -> None:
        self.assertEqual(memprobe.available_memory_gb(), memprobe.machine_memory().reclaimable)

    def test_free_memory_gb_matches_free_field(self) -> None:
        self.assertEqual(memprobe.free_memory_gb(), memprobe.machine_memory().free)


class ProcessFootprintTests(unittest.TestCase):
    def test_self_footprint_is_measured(self) -> None:
        fp = memprobe.process_footprint_gb(os.getpid())
        self.assertIsNotNone(fp)
        self.assertGreaterEqual(fp, 0.0)

    def test_nonexistent_pid_returns_zero_not_none(self) -> None:
        # A huge pid that is very unlikely to exist: rc != 0 from the kernel, not a libproc error.
        fp = memprobe.process_footprint_gb(99_999_999)
        self.assertEqual(fp, 0.0)


class SwapUsageTests(unittest.TestCase):
    def test_swap_usage_returns_a_pair_of_optional_floats(self) -> None:
        total, used = memprobe.swap_usage()
        for value in (total, used):
            if value is not None:
                self.assertGreaterEqual(value, 0.0)


class SwapStarvationTests(unittest.TestCase):
    def test_low_usage_is_not_starvation(self) -> None:
        starving, _ = memprobe.swap_starvation(swap_total_gb=1.0, swap_used_gb=0.08)
        self.assertFalse(starving)

    def test_high_absolute_usage_is_starvation(self) -> None:
        starving, reason = memprobe.swap_starvation(swap_total_gb=23.0, swap_used_gb=22.1)
        self.assertTrue(starving)
        self.assertIn("swap", reason)

    def test_none_inputs_are_not_starvation(self) -> None:
        starving, reason = memprobe.swap_starvation(swap_total_gb=None, swap_used_gb=None)
        self.assertFalse(starving)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
