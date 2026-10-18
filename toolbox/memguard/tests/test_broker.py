"""Tests for `memguard.broker`: each gate separately, in both directions (passes / fails at
exactly that gate).

Includes `LedgerAsDecisionInputTests`, which covers the deliberate design point: the global
ledger as a decision input, not just an observation (see the broker.py docstring).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from memguard import broker as broker_module
from memguard import pressure

GB = 1024**3


def _fake_machine_memory(available_gb: float) -> "broker_module.memprobe.MachineMemory":
    return broker_module.memprobe.MachineMemory(
        free=available_gb, inactive=0.0, purgeable=0.0, speculative=0.0, wired=1.0, compressor=0.0
    )


def make_broker(
    *,
    project: str = "test-project",
    require_kv_ceiling: bool = False,
    total_ram_gb: float = 128.0,
    external_rss_gb: float = 0.0,
    safety_floor_gb: float = 28.0,
    level: pressure.PressureLevel = pressure.PressureLevel.NORMAL,
    on_battery: bool = False,
    system_margin_gb: float = 16.0,
    swap_max_gb: float = 2.0,
    machine_available_gb: float = 100.0,
    swap_total_gb: float = 0.0,
    swap_used_gb: float = 0.0,
    machine_memory_probe: Optional[Any] = None,
    swap_usage_probe: Optional[Any] = None,
    heavy_processes: Optional[List[Dict[str, Any]]] = None,
    sweep_interval_s: float = 0.0,
    ledger_path: Optional[Path] = None,
    battery_heavy_threshold_gb: float = 4.0,
) -> broker_module.MemoryBroker:
    """A broker with every sensor injected: deterministic, independent of the real machine state."""
    monitor = pressure.MemoryPressureMonitor()
    monitor.inject_level_for_test(level)
    mem_probe = machine_memory_probe or (lambda: _fake_machine_memory(machine_available_gb))
    swap_probe = swap_usage_probe or (lambda: (swap_total_gb, swap_used_gb))
    heavy_probe = lambda _threshold: (heavy_processes if heavy_processes is not None else [])
    ledger = ledger_path or (Path(tempfile.mkdtemp(prefix="memguard-test-ledger-")) / "leases.json")
    return broker_module.MemoryBroker(
        project=project,
        require_kv_ceiling=require_kv_ceiling,
        sweep_interval_s=sweep_interval_s,
        safety_floor_bytes=int(safety_floor_gb * GB),
        pressure_monitor=monitor,
        total_ram_probe=lambda: int(total_ram_gb * GB),
        external_rss_probe=lambda: (int(external_rss_gb * GB) if external_rss_gb else None),
        on_battery_probe=lambda: on_battery,
        system_margin_bytes=int(system_margin_gb * GB),
        swap_max_bytes=int(swap_max_gb * GB),
        battery_heavy_threshold_bytes=int(battery_heavy_threshold_gb * GB),
        machine_memory_probe=mem_probe,
        swap_usage_probe=swap_probe,
        heavy_processes_probe=heavy_probe,
        ledger_path=ledger,
    )


class ReserveHappyPathTests(unittest.TestCase):
    def test_reasonable_request_is_granted(self) -> None:
        b = make_broker()
        lease = b.reserve("engine:test", 10 * GB, "unit-test")
        self.assertEqual(lease.reserved_bytes, 10 * GB)
        self.assertEqual(lease.project, "test-project")

    def test_request_exceeding_headroom_is_refused(self) -> None:
        b = make_broker(total_ram_gb=32, safety_floor_gb=8)
        with self.assertRaises(broker_module.MemoryReserveError):
            b.reserve("engine:too-big", 30 * GB)

    def test_zero_or_negative_bytes_is_refused(self) -> None:
        b = make_broker()
        with self.assertRaises(broker_module.MemoryReserveError):
            b.reserve("engine:zero", 0)
        with self.assertRaises(broker_module.MemoryReserveError):
            b.reserve("engine:neg", -1)


class KvCeilingPolicyTests(unittest.TestCase):
    """`require_kv_ceiling` is an explicit toggle (a project may enable it; a generic broker
    does not need it at all)."""

    def test_kv_ceiling_optional_by_default(self) -> None:
        b = make_broker(require_kv_ceiling=False)
        lease = b.reserve("engine:no-kv", 1 * GB)  # no kv_ceiling_tokens at all
        self.assertIsNone(lease.kv_ceiling_tokens)

    def test_kv_ceiling_required_when_policy_enabled(self) -> None:
        b = make_broker(require_kv_ceiling=True)
        with self.assertRaises(broker_module.MissingKVCeilingError):
            b.reserve("engine:no-kv", 1 * GB)
        with self.assertRaises(broker_module.MissingKVCeilingError):
            b.reserve("engine:zero-kv", 1 * GB, kv_ceiling_tokens=0)
        lease = b.reserve("engine:has-kv", 1 * GB, kv_ceiling_tokens=4096)
        self.assertEqual(lease.kv_ceiling_tokens, 4096)


class PressureGateTests(unittest.TestCase):
    def test_warn_blocks_new_reservations(self) -> None:
        b = make_broker(level=pressure.PressureLevel.WARN)
        with self.assertRaises(broker_module.MemoryReserveError):
            b.reserve("engine:warn-blocked", 1 * GB)

    def test_critical_blocks_new_reservations(self) -> None:
        b = make_broker(level=pressure.PressureLevel.CRITICAL)
        with self.assertRaises(broker_module.MemoryReserveError):
            b.reserve("engine:critical-blocked", 1 * GB)

    def test_normal_does_not_block(self) -> None:
        b = make_broker(level=pressure.PressureLevel.NORMAL)
        self.assertIsNotNone(b.reserve("engine:normal", 1 * GB))

    def test_unknown_level_does_not_block(self) -> None:
        b = make_broker(level=pressure.PressureLevel.UNKNOWN)
        self.assertIsNotNone(b.reserve("engine:unknown-level", 1 * GB))


class BatteryLegTests(unittest.TestCase):
    def test_light_request_on_battery_is_not_blocked(self) -> None:
        b = make_broker(on_battery=True, battery_heavy_threshold_gb=4.0)
        just_under = int(3.9 * GB)
        self.assertIsNotNone(b.reserve("engine:light", just_under))

    def test_heavy_request_on_battery_is_refused(self) -> None:
        b = make_broker(on_battery=True, battery_heavy_threshold_gb=4.0)
        with self.assertRaises(broker_module.BatteryPowerRefusedError):
            b.reserve("engine:heavy", 4 * GB)

    def test_heavy_request_on_ac_is_not_blocked_by_battery_leg(self) -> None:
        b = make_broker(on_battery=False, battery_heavy_threshold_gb=4.0)
        self.assertIsNotNone(b.reserve("engine:heavy-on-ac", 4 * GB))

    def test_battery_probe_failure_fails_closed(self) -> None:
        def _boom():
            raise RuntimeError("pmset unavailable")

        b = make_broker()
        b._on_battery_probe = _boom  # type: ignore[attr-defined]
        with self.assertRaises(broker_module.BatteryPowerRefusedError):
            b.reserve("engine:probe-fails", 10 * GB)


class MachineWideGateTests(unittest.TestCase):
    def test_low_real_availability_refuses_even_with_generous_bookkeeping(self) -> None:
        b = make_broker(machine_available_gb=5.0, system_margin_gb=16.0)
        with self.assertRaises(broker_module.MachineMemoryRefusedError):
            b.reserve("vision:huge", 10 * GB)

    def test_ample_real_availability_grants(self) -> None:
        b = make_broker(machine_available_gb=200.0)
        self.assertIsNotNone(b.reserve("vision:fits", 10 * GB))

    def test_machine_memory_probe_returning_none_fails_closed(self) -> None:
        b = make_broker(machine_memory_probe=lambda: None)
        with self.assertRaises(broker_module.MachineMemoryRefusedError):
            b.reserve("vision:probe-none", 1 * GB)

    def test_machine_memory_probe_raising_fails_closed(self) -> None:
        def _boom():
            raise RuntimeError("host_statistics64 failed")

        b = make_broker(machine_memory_probe=_boom)
        with self.assertRaises(broker_module.MachineMemoryRefusedError):
            b.reserve("vision:probe-raises", 1 * GB)

    def test_swap_over_threshold_refuses(self) -> None:
        b = make_broker(swap_max_gb=2.0, swap_total_gb=8.0, swap_used_gb=3.0, machine_available_gb=200.0)
        with self.assertRaises(broker_module.SwapPressureRefusedError):
            b.reserve("vision:swap-heavy", 1 * GB)

    def test_swap_under_threshold_does_not_refuse(self) -> None:
        b = make_broker(swap_max_gb=2.0, swap_total_gb=8.0, swap_used_gb=0.1, machine_available_gb=200.0)
        self.assertIsNotNone(b.reserve("vision:swap-light", 1 * GB))

    def test_who_holds_names_heavy_process_and_enriches_from_ledger(self) -> None:
        ledger = Path(tempfile.mkdtemp(prefix="memguard-test-ledger-")) / "leases.json"
        holder = make_broker(machine_available_gb=200.0, ledger_path=ledger, project="alpha")
        lease = holder.reserve("vision:example_model_bf16", 75 * GB)

        heavy = [{"pid": lease.pid, "name": "python3.12", "path": "/usr/bin/python3.12", "footprint_gb": 75.0}]
        seeker = make_broker(machine_available_gb=5.0, heavy_processes=heavy, ledger_path=ledger, project="beta")
        with self.assertRaises(broker_module.MachineMemoryRefusedError) as ctx:
            seeker.reserve("vision:another", 10 * GB)
        msg = str(ctx.exception)
        self.assertIn("alpha:vision:example_model_bf16", msg)


class LedgerAsDecisionInputTests(unittest.TestCase):
    """The deliberate design point: the global ledger enters the headroom calculation of
    **another** process, not just message enrichment. This is what closes the race between two
    processes starting in the same second (see the broker.py docstring)."""

    def _shared_ledger(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="memguard-test-ledger-")) / "leases.json"

    def test_second_process_is_refused_citing_first_project_and_owner(self) -> None:
        ledger = self._shared_ledger()
        # A simulated "other process": a pid that is not this tester's os.getpid().
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            first = make_broker(project="alpha", ledger_path=ledger, machine_available_gb=100.0, system_margin_gb=16.0)
            first.reserve("demo:alpha", 70 * GB, "live proof", pid=proc.pid)

            second = make_broker(project="beta", ledger_path=ledger, machine_available_gb=100.0, system_margin_gb=16.0)
            with self.assertRaises(broker_module.MachineMemoryRefusedError) as ctx:
                second.reserve("demo:beta", 60 * GB, "live proof")
            msg = str(ctx.exception)
            self.assertIn("alpha:demo:alpha", msg)
            self.assertIn("70.00GB", msg)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_after_first_releases_second_is_granted(self) -> None:
        ledger = self._shared_ledger()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            first = make_broker(project="alpha", ledger_path=ledger, machine_available_gb=100.0, system_margin_gb=16.0)
            lease = first.reserve("demo:alpha", 70 * GB, "live proof", pid=proc.pid)
            lease.release()

            second = make_broker(project="beta", ledger_path=ledger, machine_available_gb=100.0, system_margin_gb=16.0)
            self.assertIsNotNone(second.reserve("demo:beta", 60 * GB, "live proof"))
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_own_pid_entries_are_excluded_from_the_subtraction(self) -> None:
        """Two leases **from the same process** (same pid, the default) are not subtracted
        twice: they are already counted in the per-instance headroom (self._leases). Mutation:
        removing the `pid != self_pid` condition would make even a single broker in a single
        process contradict itself."""
        ledger = self._shared_ledger()
        a = make_broker(project="alpha", ledger_path=ledger, machine_available_gb=100.0, system_margin_gb=16.0)
        a.reserve("engine:a", 5 * GB)
        b = make_broker(project="alpha", ledger_path=ledger, machine_available_gb=100.0, system_margin_gb=16.0)
        # b is a separate broker but the **same process** (same pid): it must not count as "other".
        self.assertIsNotNone(b.reserve("engine:b", 5 * GB))


class ExternalLeaseHelpersTests(unittest.TestCase):
    """`record_external_lease` / `remove_external_lease`: the way for a project to report to the
    global ledger without adopting the whole generic broker class."""

    def _ledger(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="memguard-test-ledger-")) / "leases.json"

    def test_record_then_visible_via_cross_process_ledger_entries(self) -> None:
        ledger = self._ledger()
        broker_module.record_external_lease(
            ledger, lease_id="abc123", owner="video_model_a", purpose="video render",
            reserved_bytes=53 * GB, pid=__import__("os").getpid(), project="beta",
        )
        entries = broker_module.cross_process_ledger_entries(ledger)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["owner"], "video_model_a")
        self.assertEqual(entries[0]["project"], "beta")
        self.assertEqual(entries[0]["reserved_bytes"], 53 * GB)

    def test_remove_makes_it_disappear(self) -> None:
        ledger = self._ledger()
        broker_module.record_external_lease(
            ledger, lease_id="abc123", owner="x", purpose="", reserved_bytes=1 * GB,
            pid=__import__("os").getpid(), project="beta",
        )
        broker_module.remove_external_lease(ledger, "abc123")
        self.assertEqual(broker_module.cross_process_ledger_entries(ledger), [])

    def test_remove_of_missing_lease_is_a_noop(self) -> None:
        ledger = self._ledger()
        broker_module.remove_external_lease(ledger, "never-existed")  # must not raise

    def test_generic_broker_and_external_helper_share_the_same_ledger(self) -> None:
        """The generic mechanism (reserve()) and the manual one (record_external_lease) write
        to the same file with the same schema, so a project using `record_external_lease` and
        one using a regular `MemoryBroker.reserve()` see each other."""
        ledger = self._ledger()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            broker_module.record_external_lease(
                ledger, lease_id="beta-lease", owner="video_model_a", purpose="render",
                reserved_bytes=50 * GB, pid=proc.pid, project="beta",
            )
            b = make_broker(project="alpha", ledger_path=ledger, machine_available_gb=100.0, system_margin_gb=16.0)
            with self.assertRaises(broker_module.MachineMemoryRefusedError) as ctx:
                b.reserve("vision:huge", 40 * GB)
            self.assertIn("beta:video_model_a", str(ctx.exception))
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class DeadPidReapTests(unittest.TestCase):
    def test_lease_of_dead_process_is_reaped(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            self.assertTrue(broker_module.pid_alive(proc.pid))
            b = make_broker(total_ram_gb=32, safety_floor_gb=8)
            b.reserve("engine:orphaned", 1 * GB, pid=proc.pid)
            self.assertEqual(len(b.current_reservations()), 1)

            proc.terminate()
            proc.wait(timeout=5)
            self.assertFalse(broker_module.pid_alive(proc.pid))

            self.assertEqual(b.current_reservations(), [])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_lease_of_live_process_is_not_reaped(self) -> None:
        b = make_broker()
        b.reserve("engine:self", 1 * GB, pid=__import__("os").getpid())
        self.assertEqual(len(b.current_reservations()), 1)


class SweepIdleAndThreadTests(unittest.TestCase):
    def test_sweep_idle_evicts_only_stale_evictable_leases(self) -> None:
        b = make_broker()
        stale_evict, fresh_evict = mock.Mock(), mock.Mock()
        b.reserve("engine:stale", 1 * GB, idle_ttl_s=0.05, on_evict=stale_evict)
        b.reserve("engine:fresh", 1 * GB, idle_ttl_s=600.0, on_evict=fresh_evict)
        pinned = b.reserve("engine:pinned", 1 * GB, idle_ttl_s=0.05, on_evict=None)
        time.sleep(0.1)
        evicted = b.sweep_idle()
        self.assertEqual(len(evicted), 1)
        stale_evict.assert_called_once()
        fresh_evict.assert_not_called()
        owners = {r["owner"] for r in b.current_reservations()}
        self.assertEqual(owners, {"engine:fresh", "engine:pinned"})

    def test_background_sweep_thread_evicts_without_a_new_reserve_call(self) -> None:
        """`idle_ttl_s` alone does not release anything: a periodic sweep thread is needed, not
        only cleanup inside the next reserve()."""
        b = make_broker(sweep_interval_s=0.05)
        evict_cb = mock.Mock()
        b.reserve("engine:giant", 1 * GB, idle_ttl_s=0.05, on_evict=evict_cb)
        b.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and not evict_cb.called:
                time.sleep(0.05)
        finally:
            b.stop()
        evict_cb.assert_called_once()


class ReleaseAllNowTests(unittest.TestCase):
    def test_releases_all_evictable_leases_regardless_of_idle_ttl(self) -> None:
        b = make_broker()
        evict_a, evict_b = mock.Mock(), mock.Mock()
        b.reserve("engine:a", 1 * GB, idle_ttl_s=600.0, on_evict=evict_a)
        pinned = b.reserve("engine:pinned", 1 * GB, idle_ttl_s=600.0, on_evict=None)
        b.reserve("engine:b", 1 * GB, idle_ttl_s=600.0, on_evict=evict_b)
        released = b.release_all_now("test")
        self.assertEqual(len(released), 2)
        evict_a.assert_called_once()
        evict_b.assert_called_once()
        owners = {r["owner"] for r in b.current_reservations()}
        self.assertEqual(owners, {"engine:pinned"})

    def test_failing_on_evict_keeps_lease_registered(self) -> None:
        def _boom():
            raise RuntimeError("could not free")

        b = make_broker()
        b.reserve("engine:will-fail-evict", 1 * GB, on_evict=_boom)
        released = b.release_all_now("test")
        self.assertEqual(released, [])
        self.assertEqual(len(b.current_reservations()), 1)


class GenericReclaimForTests(unittest.TestCase):
    def test_runs_steps_in_order_and_records_each(self) -> None:
        order = []
        steps = [
            ("step_a", lambda: order.append("a"), None),
            ("step_b", lambda: order.append("b"), None),
        ]
        report = broker_module.generic_reclaim_for("owner", 1 * GB, steps=steps, sleep=lambda _s: None)
        self.assertEqual(order, ["a", "b"])
        self.assertEqual([s["step"] for s in report["steps"]], ["step_a", "step_b"])
        self.assertTrue(all(s["acted"] for s in report["steps"]))

    def test_a_failing_step_does_not_stop_the_rest(self) -> None:
        order = []

        def _boom():
            raise RuntimeError("nope")

        steps = [("bad", _boom, None), ("good", lambda: order.append("good"), None)]
        report = broker_module.generic_reclaim_for("owner", 1 * GB, steps=steps, sleep=lambda _s: None)
        self.assertEqual(order, ["good"])
        self.assertFalse(report["steps"][0]["acted"])
        self.assertIn("error", report["steps"][0])
        self.assertTrue(report["steps"][1]["acted"])

    def test_success_reflects_measured_availability_after_steps(self) -> None:
        steps = [("noop", lambda: None, None)]
        report = broker_module.generic_reclaim_for(
            "owner", 1 * GB, steps=steps,
            machine_memory_probe=lambda: _fake_machine_memory(200.0),
            system_margin_bytes=16 * GB, sleep=lambda _s: None,
        )
        self.assertTrue(report["success"])

    def test_failure_when_still_insufficient_after_steps(self) -> None:
        steps = [("noop", lambda: None, None)]
        report = broker_module.generic_reclaim_for(
            "owner", 100 * GB, steps=steps,
            machine_memory_probe=lambda: _fake_machine_memory(5.0),
            system_margin_bytes=16 * GB, sleep=lambda _s: None,
        )
        self.assertFalse(report["success"])


if __name__ == "__main__":
    unittest.main()


class EvictCallbackDeadlockTests(unittest.TestCase):
    """Regression tests for a real deadlock: an `on_evict` callback that called `release()`
    from the sweep blocked on the broker's non-reentrant Lock, and every
    `current_reservations()` waited forever. Each test runs in a thread with join(timeout), so
    a deadlock fails as a timeout instead of hanging the suite."""

    def _run_with_timeout(self, fn, timeout_s: float = 3.0):
        import threading
        out: Dict[str, Any] = {}

        def target() -> None:
            out["value"] = fn()

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout_s)
        self.assertFalse(t.is_alive(), "deadlock: the call did not return in time")
        return out.get("value")

    def test_on_evict_that_releases_its_own_lease_does_not_deadlock(self) -> None:
        b = make_broker()
        holder: Dict[str, Any] = {}

        def unload() -> None:
            holder["lease"].release()  # exactly the pattern that used to deadlock before the fix

        holder["lease"] = b.reserve("engine:vault", 1 * GB, idle_ttl_s=0.05, on_evict=unload)
        time.sleep(0.1)
        evicted = self._run_with_timeout(b.sweep_idle)
        self.assertEqual(len(evicted), 1)
        self.assertEqual(self._run_with_timeout(b.current_reservations), [])

    def test_on_evict_taking_an_external_lock_does_not_block_other_readers(self) -> None:
        import threading
        b = make_broker()
        model_lock = threading.Lock()
        entered = threading.Event()

        def unload() -> None:
            entered.set()
            with model_lock:  # the model's lock: the other reader holds it and queries the broker
                pass

        b.reserve("engine:clap", 1 * GB, idle_ttl_s=0.05, on_evict=unload)
        time.sleep(0.1)
        model_lock.acquire()
        sweeper = threading.Thread(target=b.sweep_idle, daemon=True)
        sweeper.start()
        self.assertTrue(entered.wait(2.0))
        # the sweep waits for model_lock; a reader holding it must still get an answer from the broker (no AB-BA)
        self._run_with_timeout(b.current_reservations)
        model_lock.release()
        sweeper.join(3.0)
        self.assertFalse(sweeper.is_alive())
        self.assertEqual(self._run_with_timeout(b.current_reservations), [])

    def test_release_all_now_with_self_releasing_callback(self) -> None:
        b = make_broker()
        holder: Dict[str, Any] = {}
        holder["lease"] = b.reserve("engine:x", 1 * GB, on_evict=lambda: holder["lease"].release())
        released = self._run_with_timeout(b.release_all_now)
        self.assertEqual(len(released), 1)
