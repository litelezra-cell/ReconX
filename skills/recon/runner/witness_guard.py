#!/usr/bin/env python3
"""Witness guard: runs one witness stage under a machine-wide memory lease.

    python witness_guard.py --request request.json [--config runner.json] [--out-dir TARGET_ROOT]

The guard validates the request, runs memguard's standalone preflight, takes a lease for the stage
budget in the shared ledger, starts the worker in its own process group, samples memory safety while
it runs, stops it when unsafe, releases the lease only after the whole process group is dead, and
writes ``<stage>-guard.json``. It never loads a model itself.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner_config import (  # noqa: E402
    MAX_TIMEOUT_S, RunnerRefused, atomic_write_json, load_config, manifest_inputs, require_inside,
    require_input_file, resolve_root, stage_config,
)

SAMPLE_INTERVAL_S = 0.25
RESULT_CEILING = 16 * 1024**2


class GuardRefused(RunnerRefused):
    pass


def validate_request(request: dict[str, Any], config: dict[str, Any], root: Path) -> dict[str, Any]:
    """Return the request completed from config (runtime, budget); refuse anything outside the target root."""
    cfg = stage_config(config, request.get("stage"))  # type: ignore[arg-type]
    python = cfg.get("python")
    if not python or not Path(python).expanduser().is_file():
        raise GuardRefused("Stage interpreter 'python' is not configured or does not exist")
    for field, configured in (("runtime", python), ("estimated_bytes", cfg["budget_bytes"])):
        if field in request and request[field] != configured:
            raise GuardRefused(f"Request {field} differs from the runner config")
    timeout = request.get("timeout", MAX_TIMEOUT_S)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= MAX_TIMEOUT_S:
        raise GuardRefused(f"Witness timeout must be bounded by {MAX_TIMEOUT_S} seconds")
    parent = request.get("parent_pid", os.getppid())
    if not isinstance(parent, int) or parent <= 1:
        raise GuardRefused("Witness parent PID is invalid")
    output = require_inside("output_path", request.get("output_path", ""), root)
    guard_path = request.get("guard_path") or (
        str(output)[:-len("-raw.json")] + "-guard.json" if str(output).endswith("-raw.json")
        else str(output) + ".guard.json")
    guard = require_inside("guard_path", guard_path, root)
    manifest = require_input_file("manifest_path", request.get("manifest_path", ""), root)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for item in manifest_inputs(data):
        require_input_file("Witness source", item, root)
    return {**request, "runtime": python, "estimated_bytes": cfg["budget_bytes"], "timeout": timeout,
            "parent_pid": parent, "output_path": str(output), "guard_path": str(guard),
            "manifest_path": str(manifest), "root": str(root), "config_path": config["_path"]}


def _parent_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_and_confirm(worker: Any, *, group_alive: Callable[[int], bool] = _group_alive,
                     kill_group: Callable[[int, int], None] = os.killpg,
                     sleep: Callable[[float], None] = time.sleep) -> bool:
    """Kill the whole process group; a dead leader alone does not free the memory."""
    if worker.poll() is None or group_alive(worker.pid):
        try:
            kill_group(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        worker.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    for _ in range(20):
        if worker.poll() is not None and not group_alive(worker.pid):
            return True
        sleep(0.25)
    return False


def run_guard(request: dict[str, Any], *, broker: Any, preflight: Callable[..., Any],
              probe: Callable[[], dict[str, Any]], spawn: Callable[[], Any],
              verify_lease: Callable[[Any], None], stop: threading.Event,
              alive: Callable[[int], bool] = _parent_alive,
              cleanup: Callable[[Any], bool] = stop_and_confirm,
              clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> tuple[dict[str, Any], Any, Any]:
    """Run one stage. Returns (report, lease still held or None, worker). Dependencies are injected."""
    report: dict[str, Any] = {"status": "refused", "stage": request["stage"], "guard_pid": os.getpid(),
                              "reserved_bytes": request["estimated_bytes"],
                              "lease_released": False, "worker_stopped": False}
    lease = None
    worker = None
    try:
        if stop.is_set() or not alive(request["parent_pid"]):
            raise GuardRefused("Parent exited before allocation")
        report["preflight"] = preflight(request["estimated_bytes"], context="witness " + request["stage"],
                                        ask_daemon_release=False)
        report["initial_safety"] = probe()
        lease = broker.reserve("witness:" + request["stage"], request["estimated_bytes"],
                               purpose="offline video witness", kv_ceiling_tokens=8192)
        verify_lease(lease)
        report.update(lease_id=lease.lease_id, global_lease_verified=True)
        request = dict(request, lease_id=lease.lease_id, watchdog_pid=os.getpid())
        if stop.is_set() or not alive(request["parent_pid"]):
            raise GuardRefused("Parent exited before worker start")
        worker = spawn()
        report["request_worker_pid"] = worker.pid
        box: list[tuple[str, str]] = []
        reader = threading.Thread(target=lambda: box.append(worker.communicate(json.dumps(request))),
                                  daemon=True)
        reader.start()
        deadline = clock() + request["timeout"]
        samples = 0
        while worker.poll() is None:
            if stop.is_set() or not alive(request["parent_pid"]):
                raise GuardRefused("Parent exited or cancelled")
            if clock() >= deadline:
                report["status"] = "failed"
                raise GuardRefused("Witness timeout")
            report["last_safety"] = probe()
            samples += 1
            sleep(SAMPLE_INTERVAL_S)
        reader.join(timeout=5)
        report["safety_samples"] = samples
        report["worker_exit_code"] = worker.returncode
        if worker.returncode != 0 or not box:
            report["worker_stderr_tail"] = box[0][1][-8192:] if box else None
            report["status"] = "failed"
            raise GuardRefused("Worker exited without a valid result" + _worker_error(box))
        if len(box[0][0].encode()) > RESULT_CEILING:
            raise GuardRefused("Worker result exceeds 16 MiB metadata ceiling")
        result = json.loads(box[0][0])
        if not isinstance(result, dict) or result.get("status") != "ok":
            report["status"] = "failed"
            raise GuardRefused("Worker reported failure: " + str(
                result.get("error", "unknown") if isinstance(result, dict) else "not an object"))
        report.update(status="ok", result=result)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        report["status"] = "failed" if worker is not None else "refused"
        report["error"] = str(exc)
    finally:
        try:
            stopped = worker is None or cleanup(worker)
        except (OSError, subprocess.SubprocessError):
            stopped = False
        report["worker_stopped"] = stopped
        if lease is not None and stopped:
            lease.release()
            report["lease_released"] = True
        elif lease is not None:
            report.update(status="refused", error="Worker group death unconfirmed; guard retains lease")
    return report, (lease if lease is not None and not report["lease_released"] else None), worker


def _worker_error(box: list[tuple[str, str]]) -> str:
    try:
        return ": " + str(json.loads(box[0][0]).get("error", ""))
    except (ValueError, AttributeError, IndexError):
        return ""


# --- machine sensors (memguard + one ctypes sysctl) -----------------------------------------------

def _sysctl_uint32(name: bytes) -> int:
    lib = ctypes.CDLL(None)
    value = ctypes.c_uint32()
    size = ctypes.c_size_t(4)
    if lib.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
        raise OSError("sysctl unavailable: " + name.decode())
    return int(value.value)


def import_memguard(config: dict[str, Any]) -> None:
    """Make ``memguard`` importable: configured path, then the repository's own toolbox/memguard."""
    candidates = [config.get("memguard_path"), Path(__file__).resolve().parents[3] / "toolbox" / "memguard"]
    for candidate in candidates:
        if candidate and (Path(candidate).expanduser() / "memguard").is_dir():
            sys.path.insert(0, str(Path(candidate).expanduser()))
            break
    importlib.import_module("memguard.broker")


def make_probe(broker_module: Any, memprobe: Any, sensors: Any, pressure: Any) -> Callable[[], dict[str, Any]]:
    def probe() -> dict[str, Any]:
        if sensors.on_battery_power():
            raise GuardRefused("Heavy inference requires AC power")
        _total, swap_used = memprobe.swap_usage()
        if swap_used is None:
            raise GuardRefused("Swap sensor unavailable")
        try:
            level = pressure._flags_to_level(_sysctl_uint32(b"kern.memorystatus_vm_pressure_level"))
        except OSError:
            level = pressure.PressureLevel.UNKNOWN
        if level not in (pressure.PressureLevel.NORMAL, pressure.PressureLevel.UNKNOWN):
            raise GuardRefused("Memory pressure is " + level.value)
        broker_module.check_machine_wide_headroom(
            0, context="witness guard", system_margin_bytes=broker_module.system_margin_bytes_from_config(),
            swap_max_bytes=broker_module.swap_max_bytes_from_config(),
            machine_memory_probe=memprobe.machine_memory, swap_usage_probe=memprobe.swap_usage,
            heavy_processes_probe=sensors.heavy_processes,
            ledger_entries_probe=lambda: broker_module.cross_process_ledger_entries(
                broker_module.default_ledger_path()))
        return {"swap_used_gb": swap_used, "pressure": level.value, "on_ac": True}
    return probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--request", required=True, help="request JSON file")
    parser.add_argument("--config", help="runner config (default: $RECON_RUNNER_CONFIG or runner.json)")
    parser.add_argument("--out-dir", help="target root (default: $RECON_OUT_DIR)")
    args = parser.parse_args(argv)
    guard_path: Path | None = None
    request: dict[str, Any] = {}
    try:
        config = load_config(args.config)
        root = resolve_root(args.out_dir)
        request = validate_request(json.loads(Path(args.request).read_text(encoding="utf-8")), config, root)
        guard_path = Path(request["guard_path"])
        import_memguard(config)
        broker_module = importlib.import_module("memguard.broker")
        sensors = importlib.import_module("memguard.sensors")
        pressure = importlib.import_module("memguard.pressure")
        memprobe = importlib.import_module("memguard.memprobe")
        state_module = importlib.import_module("memguard.state")
        preflight_module = importlib.import_module("memguard.preflight")
    except (RuntimeError, ImportError, OSError, ValueError, KeyError) as exc:
        report = {"status": "refused", "stage": request.get("stage"), "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if guard_path is not None:
            atomic_write_json(guard_path, report)
        return 1
    broker = broker_module.MemoryBroker(project="recon-witness", sweep_interval_s=0, require_kv_ceiling=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def verify_lease(lease: Any) -> None:
        row = json.loads(state_module.default_ledger_path().read_text(encoding="utf-8")).get(lease.lease_id)
        if not row or row.get("pid") != os.getpid() or row.get("reserved_bytes") != lease.reserved_bytes:
            raise GuardRefused("Lease absent from the shared ledger")

    def spawn() -> Any:
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1",
                   HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false", RECON_OUT_DIR=request["root"])
        worker = Path(__file__).with_name("witness_worker.py")
        return subprocess.Popen([request["runtime"], str(worker)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, env=env)

    try:
        report, retained, worker = run_guard(
            request, broker=broker, preflight=preflight_module.preflight_standalone_load,
            probe=make_probe(broker_module, memprobe, sensors, pressure),
            spawn=spawn, verify_lease=verify_lease, stop=stop)
    except preflight_module.PreflightRefusedError as exc:  # raised before any lease
        report, retained, worker = {"status": "refused", "stage": request["stage"], "error": str(exc),
                                    "reserved_bytes": request["estimated_bytes"],
                                    "lease_released": False, "worker_stopped": True}, None, None
    atomic_write_json(guard_path, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    while retained is not None:  # quarantine: keep the lease until the process group is really dead
        if worker is not None and stop_and_confirm(worker):
            retained.release()
            retained = None
        else:
            time.sleep(1)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
