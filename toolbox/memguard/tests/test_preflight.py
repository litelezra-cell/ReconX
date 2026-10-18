"""Tests for `memguard.preflight`: every external dependency is mocked (no real daemon or
Ollama, no network), plus tests for the generic daemon registry."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from memguard import broker as broker_module
from memguard import preflight

GB = 1024**3


def _fake_mem(available_gb: float):
    return broker_module.memprobe.MachineMemory(
        free=available_gb, inactive=0.0, purgeable=0.0, speculative=0.0, wired=1.0, compressor=0.0
    )


def _counting_clock():
    state = {"t": 0.0}

    def _clock():
        state["t"] += 0.01
        return state["t"]

    return _clock


def _abundant_kwargs(**overrides):
    base = dict(
        find_daemon=lambda **_kw: None,
        ollama_ps_probe=lambda: [],
        machine_memory_probe=lambda: _fake_mem(200.0),
        swap_usage_probe=lambda: (8.0, 0.0),
        heavy_processes_probe=lambda _t: [],
        sleep=lambda _s: None,
        clock=_counting_clock(),
    )
    base.update(overrides)
    return base


class DaemonModelLoadedGateTests(unittest.TestCase):
    def test_daemon_reporting_a_huge_model_is_refused(self) -> None:
        find_daemon = lambda **_kw: ("http://x/1", {"models_loaded": [{"provider_id": "vision:huge", "bytes": 73 * GB}]})
        with self.assertRaises(preflight.PreflightRefusedError) as ctx:
            preflight.preflight_standalone_load(10 * GB, **_abundant_kwargs(find_daemon=find_daemon))
        self.assertEqual(ctx.exception.code, "daemon_model_loaded")
        self.assertIn("vision:huge", str(ctx.exception))

    def test_daemon_not_running_does_not_block(self) -> None:
        report = preflight.preflight_standalone_load(10 * GB, **_abundant_kwargs())
        names = [c["name"] for c in report["checks"]]
        self.assertIn("daemon_models_loaded", names)
        self.assertIn("machine_wide_headroom", names)

    def test_daemon_running_with_small_model_does_not_block(self) -> None:
        find_daemon = lambda **_kw: ("http://x/1", {"models_loaded": [{"provider_id": "vision:small", "bytes": 1 * GB}]})
        report = preflight.preflight_standalone_load(1 * GB, **_abundant_kwargs(find_daemon=find_daemon))
        self.assertTrue(any(c["name"] == "machine_wide_headroom" for c in report["checks"]))

    def test_ask_release_calls_rpc_and_waits_for_measured_drop(self) -> None:
        find_daemon = lambda **_kw: ("http://x/1", {"models_loaded": [{"provider_id": "vision:huge", "bytes": 73 * GB}]})
        calls = []

        def _rpc(daemon_ref, method, **_kw):
            calls.append((daemon_ref, method))
            return {"released": ["vision:huge"]}

        report = preflight.preflight_standalone_load(
            10 * GB, ask_daemon_release=True, **_abundant_kwargs(find_daemon=find_daemon, call_rpc=_rpc)
        )
        self.assertEqual(calls, [("http://x/1", "release_models")])
        self.assertTrue(any(c["name"] == "daemon_release_confirmed_by_vm_stat" and c["passed"] for c in report["checks"]))

    def test_ask_release_that_never_frees_enough_is_refused(self) -> None:
        find_daemon = lambda **_kw: ("http://x/1", {"models_loaded": [{"provider_id": "vision:huge", "bytes": 73 * GB}]})
        with self.assertRaises(preflight.PreflightRefusedError) as ctx:
            preflight.preflight_standalone_load(
                10 * GB,
                ask_daemon_release=True,
                **_abundant_kwargs(
                    find_daemon=find_daemon,
                    call_rpc=lambda *_a, **_kw: {"released": []},
                    machine_memory_probe=lambda: _fake_mem(1.0),  # never "releases" enough
                ),
            )
        self.assertEqual(ctx.exception.code, "daemon_release_not_confirmed")


class OllamaGateTests(unittest.TestCase):
    def test_ollama_loaded_refuses(self) -> None:
        with self.assertRaises(preflight.PreflightRefusedError) as ctx:
            preflight.preflight_standalone_load(1 * GB, **_abundant_kwargs(ollama_ps_probe=lambda: ["qwen3:35b   4 minutes from now"]))
        self.assertEqual(ctx.exception.code, "ollama_loaded")

    def test_ollama_empty_does_not_block(self) -> None:
        report = preflight.preflight_standalone_load(1 * GB, **_abundant_kwargs(ollama_ps_probe=lambda: []))
        self.assertTrue(any(c["name"] == "ollama_ps" for c in report["checks"]))

    def test_ollama_ps_real_call_returns_a_list(self) -> None:
        # The content is machine-dependent; only check that it does not raise and the type is right.
        self.assertIsInstance(preflight.ollama_ps(), list)


class MachineWideGateThroughPreflightTests(unittest.TestCase):
    def test_low_availability_refuses(self) -> None:
        with self.assertRaises(broker_module.MachineMemoryRefusedError):
            preflight.preflight_standalone_load(70 * GB, **_abundant_kwargs(machine_memory_probe=lambda: _fake_mem(30.0)))

    def test_swap_pressure_refuses(self) -> None:
        with self.assertRaises(broker_module.SwapPressureRefusedError):
            preflight.preflight_standalone_load(1 * GB, **_abundant_kwargs(swap_usage_probe=lambda: (8.0, 3.0)))

    def test_ample_room_passes_all_three_checks(self) -> None:
        report = preflight.preflight_standalone_load(10 * GB, **_abundant_kwargs())
        names = [c["name"] for c in report["checks"]]
        self.assertEqual(names, ["daemon_models_loaded", "ollama_ps", "machine_wide_headroom"])


class DaemonRegistrationTests(unittest.TestCase):
    def test_register_then_list_round_trips(self) -> None:
        preflight.register_daemon("unit-test-daemon", "http://127.0.0.1:9", pid=os.getpid())
        try:
            names = [d["name"] for d in preflight.list_registered_daemons()]
            self.assertIn("unit-test-daemon", names)
        finally:
            preflight.unregister_daemon("unit-test-daemon")
        self.assertNotIn("unit-test-daemon", [d["name"] for d in preflight.list_registered_daemons()])

    def test_dead_pid_registration_is_reaped_on_list(self) -> None:
        # A pid that is very unlikely to exist: reaping should remove the file silently.
        preflight.register_daemon("dead-daemon", "http://127.0.0.1:9", pid=99_999_999)
        names = [d["name"] for d in preflight.list_registered_daemons()]
        self.assertNotIn("dead-daemon", names)

    def test_unregister_of_missing_daemon_does_not_raise(self) -> None:
        preflight.unregister_daemon("never-registered-xyz")  # must not raise

    def test_find_live_daemon_generic_queries_registered_daemons(self) -> None:
        calls = []

        def _fake_http_get(url, timeout_s):
            calls.append(url)
            return {"status": "ok", "models_loaded": []}

        preflight.register_daemon("http-test-daemon", "http://127.0.0.1:12345", pid=os.getpid())
        try:
            with mock.patch.object(preflight, "_http_get_json", _fake_http_get):
                result = preflight.find_live_daemon()
        finally:
            preflight.unregister_daemon("http-test-daemon")
        self.assertIsNotNone(result)
        url, health = result
        self.assertEqual(url, "http://127.0.0.1:12345")
        self.assertEqual(health["models_loaded"], [])
        self.assertEqual(calls, ["http://127.0.0.1:12345/health"])


if __name__ == "__main__":
    unittest.main()
