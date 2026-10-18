"""memguard.broker: a single admission gate before every model load, with **one ledger per machine**.

**The contract is binary:** `reserve()` returns a `Lease` or raises `MemoryReserveError`.
There is no middle state of "warn and continue".

**What is generic here, and what stays in each project:**
  - **Generic (here):** real measurement through `memprobe` / `sensors` (ctypes, no fork),
    the machine-wide ledger (file + lock + reaping), the whole-machine gate, the battery
    gate, idle TTL + sweep thread, LRU eviction, and a generic `reclaim_for` (the list of
    steps is injected).
  - **Project-specific (in the project's own shim layer):** units and parameter names
    (bytes vs GB, a mandatory `kv_ceiling_tokens`, project ceilings or cost ledgers), an
    external RSS sensor for a specific host application, project battery policy, and the
    **actual steps** of `reclaim_for` (which external services to stop is project
    knowledge, not memguard's).

**Deliberate design point: the ledger is a decision input, not just an observation.** A
broker that treats its ledger as "enriched observation, not the source of truth" is correct
when the only thing needing protection is an uncooperative process (a desktop app, Ollama),
because the real measurement (memprobe) sees those without their cooperation. But a machine
shared by several cooperating processes has a different failure: two of them call
`reserve()` **in the same second, before either has actually allocated**, and memprobe,
measured before the real allocation happens, shows "free" to both. Only one ledger per
machine closes this race. In practice, the whole-machine gate subtracts from the real
measurement the sum of bytes that **other** processes (different pid) have already
**declared** in the ledger, before they have necessarily allocated. This can double-count
memory that is both already allocated and declared (memprobe already sees it missing and
the ledger subtracts it again): conservative, not dangerous. Better to be too conservative
than to believe there is room when there is not. This is demonstrated live by
`memguard-demo` (protocol.md): two simulated processes, no real memory touched, the second
refused with the project name of the first.

**"Pinned" leases (without `on_evict`) are never evicted by force.** Without a callback that
actually knows how to release, the broker cannot guarantee the memory is free.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from memguard import memprobe, pressure, sensors
from memguard.state import default_ledger_path as _global_default_ledger_path

logger = logging.getLogger("memguard.broker")

DEFAULT_SAFETY_FLOOR_GB = 28.0
DEFAULT_IDLE_TTL_S = 600.0  # 10 minutes
_SAFETY_FLOOR_ENV_VAR = "MEMGUARD_SAFETY_FLOOR_GB"

_BYTES_PER_GB = 1024**3

_SYSTEM_MARGIN_ENV_VAR = "MEMGUARD_SYSTEM_MARGIN_GB"
DEFAULT_SYSTEM_MARGIN_GB = 16.0

_SWAP_MAX_ENV_VAR = "MEMGUARD_SWAP_MAX_GB"
DEFAULT_SWAP_MAX_GB = 2.0

# Battery gate: generic default. **A consuming project should override it with its own
# number, derived from measurements**; the generic constant here is only a default for
# whoever does not supply a policy of its own.
DEFAULT_BATTERY_HEAVY_THRESHOLD_GB = 4.0
BATTERY_HEAVY_THRESHOLD_GB = DEFAULT_BATTERY_HEAVY_THRESHOLD_GB
BATTERY_HEAVY_THRESHOLD_BYTES = int(BATTERY_HEAVY_THRESHOLD_GB * _BYTES_PER_GB)


def system_margin_bytes_from_config() -> int:
    raw = os.environ.get(_SYSTEM_MARGIN_ENV_VAR)
    gb = float(raw) if raw else DEFAULT_SYSTEM_MARGIN_GB
    return int(gb * _BYTES_PER_GB)


def swap_max_bytes_from_config() -> int:
    raw = os.environ.get(_SWAP_MAX_ENV_VAR)
    gb = float(raw) if raw else DEFAULT_SWAP_MAX_GB
    return int(gb * _BYTES_PER_GB)


def safety_floor_bytes_from_config() -> int:
    raw = os.environ.get(_SAFETY_FLOOR_ENV_VAR)
    gb = float(raw) if raw else DEFAULT_SAFETY_FLOOR_GB
    return int(gb * _BYTES_PER_GB)


def _fmt_gb(num_bytes: float) -> str:
    return f"{num_bytes / _BYTES_PER_GB:.2f}GB"


def pid_alive(pid: int) -> bool:
    """Public on purpose (not `_pid_alive`): `preflight.py` needs it too, for the daemon
    registry (`list_registered_daemons`), not just the broker internals."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but we lack permission to signal it: still "alive"


_pid_alive = pid_alive  # former internal name, kept for compatibility with anyone already importing it


class MemoryReserveError(RuntimeError):
    """Allocation refused. The message is meant to be human-readable."""

    def __init__(self, message: str, code: str = "memory_refused", params: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code or "memory_refused"
        self.params = params or {}


class MissingKVCeilingError(MemoryReserveError):
    """`reserve()` was called without a valid `kv_ceiling_tokens` on a broker constructed with
    `require_kv_ceiling=True` (a project policy: "loading without a ceiling is an error, not
    a default"). A generic broker (`require_kv_ceiling` defaults to `False`) does not require
    it at all; the parameter is simply stored on the Lease for documentation."""


class BatteryPowerRefusedError(MemoryReserveError):
    """A heavy request was refused because the machine is on battery (or the power state could not be read at all)."""

    def __init__(self, message: str, code: str = "battery_refused", params: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, code=code or "battery_refused", params=params)


class MachineMemoryRefusedError(MemoryReserveError):
    """Refused because the **whole machine** (memprobe, not just the broker's own accounting)
    does not have enough actually-free memory **after subtracting what other projects have
    already declared in the global ledger**, or because the measurement failed."""

    def __init__(self, message: str, code: str = "machine_memory_refused", params: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, code=code or "machine_memory_refused", params=params)


class SwapPressureRefusedError(MemoryReserveError):
    """`vm.swapusage.used` is above the threshold. Swap is a gate: compression is already in effect."""

    def __init__(self, message: str, code: str = "swap_pressure_refused", params: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, code=code or "swap_pressure_refused", params=params)


# -----------------------------------------------------------------------------
# The machine-wide ledger: `~/.local/state/memguard/leases.json` (or `MEMGUARD_STATE_DIR`)
# -----------------------------------------------------------------------------
#
# The `project` field: every lease carries the name of the project that created it, so that
# a refusal message can say "held by alpha:model_x" and not just "foreign process,
# pid=1234". See `who_holds_memory_summary`.
_LEDGER_FILENAME = "leases.json"


def default_ledger_path() -> Path:
    """Default path: **machine-wide** (`memguard.state.default_ledger_path()`), overridable via
    `MEMGUARD_STATE_DIR`. Deliberately not a per-project file, which would be blind to other
    projects: exactly the problem this package exists to fix."""
    return _global_default_ledger_path()


@contextlib.contextmanager
def _ledger_file_lock(path: Path):
    """File lock (`flock`, between **processes**, including different projects on the same
    file) on an adjacent lock file; writes always go through tmp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _read_ledger(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        logger.warning("shared ledger (%s) unreadable; treating it as empty", path, exc_info=True)
        return {}


def _write_ledger_atomic(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def record_external_lease(
    ledger_path: Path,
    *,
    lease_id: str,
    owner: str,
    purpose: str,
    reserved_bytes: int,
    pid: int,
    project: str,
    granted_at: Optional[float] = None,
) -> None:
    """Writes a lease record into the global ledger **without requiring a full
    `MemoryBroker` instance**: for a project that has its own admission mechanism and only
    wants to **also** report to the shared ledger, so that other projects see it in
    "who holds memory" and in headroom calculations, without handing the whole decision to
    the generic class. A write failure is swallowed into the log (a failed write should not
    bring down a `reserve()` already approved by the caller's own admission mechanism)."""
    try:
        with _ledger_file_lock(ledger_path):
            data = _read_ledger(ledger_path)
            data[lease_id] = {
                "owner": owner,
                "purpose": purpose,
                "reserved_bytes": int(reserved_bytes),
                "pid": int(pid),
                "project": project,
                "granted_at": granted_at if granted_at is not None else time.time(),
            }
            _write_ledger_atomic(ledger_path, data)
    except OSError:
        logger.warning("record_external_lease failed for %s", lease_id, exc_info=True)


def remove_external_lease(ledger_path: Path, lease_id: str) -> None:
    """Deletes an external lease record (see `record_external_lease`). Idempotent: a no-op if it no longer exists."""
    try:
        with _ledger_file_lock(ledger_path):
            data = _read_ledger(ledger_path)
            if lease_id in data:
                del data[lease_id]
                _write_ledger_atomic(ledger_path, data)
    except OSError:
        logger.warning("remove_external_lease failed for %s", lease_id, exc_info=True)


def cross_process_ledger_entries(ledger_path: Path) -> List[Dict[str, Any]]:
    """Reads the shared ledger, removes dead pids **from all projects** (not just the
    caller's), and returns the live records."""
    with _ledger_file_lock(ledger_path):
        data = _read_ledger(ledger_path)
        alive = {lid: entry for lid, entry in data.items() if _pid_alive(entry.get("pid", -1))}
        if len(alive) != len(data):
            _write_ledger_atomic(ledger_path, alive)
        return list(alive.values())


def who_holds_memory_summary(
    heavy_processes_probe: Callable[[int], List[Dict[str, Any]]],
    ledger_entries_probe: Callable[[], List[Dict[str, Any]]],
) -> str:
    """"Who holds memory" for a refusal message: processes above 5 GB (including Ollama or
    any other process), enriched with the real project / owner / purpose from the shared
    ledger when the pid matches a declared lease."""
    try:
        heavy = heavy_processes_probe(sensors.HEAVY_PROCESS_THRESHOLD_BYTES)
    except Exception:
        logger.warning("who_holds_memory_summary: heavy_processes_probe failed", exc_info=True)
        return "Who holds memory: could not scan processes."
    if not heavy:
        return "Who holds memory: no single process above 5GB (usage is spread out, or it is in wired/compressor memory, not RSS/footprint)."

    declared_by_pid: Dict[int, Dict[str, Any]] = {}
    try:
        for entry in ledger_entries_probe():
            pid = entry.get("pid")
            if isinstance(pid, int):
                declared_by_pid[pid] = entry
    except Exception:
        logger.warning("who_holds_memory_summary: ledger_entries_probe failed", exc_info=True)

    parts = []
    for proc in heavy:
        pid = proc.get("pid")
        declared = declared_by_pid.get(pid) if isinstance(pid, int) else None
        if declared is not None:
            project = declared.get("project")
            tag = f"{project}:{declared.get('owner')}" if project else declared.get("owner")
            parts.append(
                f"{tag} ({declared.get('purpose', '')}, {proc.get('footprint_gb'):.1f}GB, pid={pid})"
            )
        else:
            parts.append(f"{proc.get('name')} ({proc.get('footprint_gb'):.1f}GB, pid={pid})")
    return "Who holds memory (>5GB): " + "; ".join(parts)


def _declared_bytes_by_other_processes(
    ledger_entries_probe: Callable[[], List[Dict[str, Any]]], self_pid: int
) -> Tuple[int, List[Dict[str, Any]]]:
    """The sum of bytes that **other** processes (pid different from self_pid) have already
    declared in the global ledger, plus the records themselves (for a readable refusal
    message). See the module docstring ("deliberate design point"): this is what closes the
    race between two processes starting in the same second; it is not just observation."""
    try:
        entries = ledger_entries_probe()
    except Exception:
        logger.warning("_declared_bytes_by_other_processes: ledger_entries_probe failed; treating as empty", exc_info=True)
        return 0, []
    others = [e for e in entries if e.get("pid") != self_pid]
    total = sum(int(e.get("reserved_bytes") or 0) for e in others)
    return total, others


def check_machine_wide_headroom(
    estimated_bytes: int,
    *,
    context: str,
    system_margin_bytes: int,
    swap_max_bytes: int,
    machine_memory_probe: Callable[[], Optional["memprobe.MachineMemory"]],
    swap_usage_probe: Callable[[], Tuple[Optional[float], Optional[float]]],
    heavy_processes_probe: Callable[[int], List[Dict[str, Any]]],
    ledger_entries_probe: Callable[[], List[Dict[str, Any]]],
    self_pid: Optional[int] = None,
) -> None:
    """The shared check: the swap gate, then the machine gate **net of what other projects
    have already declared in the ledger** (see the module docstring). Called from both
    `MemoryBroker._machine_wide_gate` and `preflight.preflight_standalone_load`: **exactly
    the same function**, not a look-alike copy. `context` is only a human-readable
    description of the call (it does not affect the decision). `self_pid` (default
    `os.getpid()`) ensures our own leases (already counted separately in the per-project
    `_headroom_locked`, or not yet actually allocated) are not double-counted as "another
    process".
    """
    if self_pid is None:
        self_pid = os.getpid()

    try:
        swap_total_gb, swap_used_gb = swap_usage_probe()
    except Exception as exc:  # noqa: BLE001 (fail closed, the guiding principle of the module)
        raise MachineMemoryRefusedError(
            f"{context} refused: could not measure swap ({exc}); failing closed, not guessing."
        ) from exc

    if swap_used_gb is not None and swap_used_gb * _BYTES_PER_GB > swap_max_bytes:
        raise SwapPressureRefusedError(
            f"{context} refused: swap in use {swap_used_gb:.2f}GB is above the threshold "
            f"{swap_max_bytes / _BYTES_PER_GB:.1f}GB. Swap is a gate: "
            f"compression is already in effect, so no further model is loaded on top of it. "
            f"{who_holds_memory_summary(heavy_processes_probe, ledger_entries_probe)}"
        )

    try:
        mem = machine_memory_probe()
    except Exception as exc:  # noqa: BLE001
        mem = None
        _probe_exc: Optional[Exception] = exc
    else:
        _probe_exc = None

    if mem is None or mem.reclaimable is None:
        raise MachineMemoryRefusedError(
            f"{context} refused: could not measure whole-machine memory "
            f"({_probe_exc if _probe_exc is not None else 'machine_memory() returned None'}); "
            "failing closed, not assuming there is room."
        )

    available_bytes = mem.reclaimable * _BYTES_PER_GB
    declared_by_others_bytes, other_entries = _declared_bytes_by_other_processes(ledger_entries_probe, self_pid)
    usable_bytes = available_bytes - system_margin_bytes - declared_by_others_bytes
    if usable_bytes < estimated_bytes:
        claimed_note = ""
        if declared_by_others_bytes > 0:
            tags = ", ".join(
                f"{e.get('project') or '?'}:{e.get('owner')} ({_fmt_gb(int(e.get('reserved_bytes') or 0))}, pid={e.get('pid')})"
                for e in other_entries
            )
            claimed_note = f" | already declared in the ledger by other processes {_fmt_gb(declared_by_others_bytes)} [{tags}]"
        raise MachineMemoryRefusedError(
            f"{context} refused: whole-machine check failed. "
            f"Actually free (free+inactive+purgeable+speculative) {_fmt_gb(available_bytes)} | "
            f"system margin {_fmt_gb(system_margin_bytes)}{claimed_note} => usable "
            f"{_fmt_gb(max(usable_bytes, 0))}, required {_fmt_gb(estimated_bytes)}. "
            f"{who_holds_memory_summary(heavy_processes_probe, ledger_entries_probe)}",
            params={
                "requested_bytes": int(estimated_bytes),
                "available_bytes": int(available_bytes),
                "system_margin_bytes": int(system_margin_bytes),
                "declared_by_others_bytes": int(declared_by_others_bytes),
            },
        )


# -----------------------------------------------------------------------------
# generic_reclaim_for: an injected reclaim order
# -----------------------------------------------------------------------------


def generic_reclaim_for(
    owner: str,
    needed_bytes: int,
    *,
    steps: List[Tuple[str, Callable[[], None], Optional[int]]],
    machine_memory_probe: Callable[[], Optional["memprobe.MachineMemory"]] = memprobe.machine_memory,
    system_margin_bytes: Optional[int] = None,
    poll_interval_s: float = 0.25,
    reclaim_timeout_s: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Runs `steps` (a list of `(name, action, pid_or_None)`) in order, and after each step
    waits for the measured footprint to drop (if `pid` is given) before continuing.
    **Generic with respect to the steps themselves**: each project injects its own (for
    example stopping an image-generation server, unloading an Ollama model, clearing a
    framework cache).
    """
    margin = system_margin_bytes if system_margin_bytes is not None else system_margin_bytes_from_config()
    report: Dict[str, Any] = {
        "owner": owner,
        "needed_bytes": int(needed_bytes),
        "steps": [],
        "success": False,
        "available_before": memprobe.available_memory_gb(),
        "available_after": None,
    }

    for name, action, pid in steps:
        before_fp = memprobe.process_footprint_gb(pid) if pid else None
        try:
            action()
            acted = True
            err = None
        except Exception as exc:  # noqa: BLE001 (one failed reclaim step must not stop the rest)
            acted = False
            err = str(exc)

        after_fp = before_fp
        if pid and acted:
            deadline = time.monotonic() + reclaim_timeout_s
            while time.monotonic() < deadline:
                current = memprobe.process_footprint_gb(pid) or 0.0
                if before_fp is not None and current < before_fp * 0.5:
                    after_fp = current
                    break
                sleep(poll_interval_s)
            else:
                after_fp = memprobe.process_footprint_gb(pid)

        step_report: Dict[str, Any] = {"step": name, "acted": acted}
        if err is not None:
            step_report["error"] = err
        if pid:
            step_report["footprint_before_gb"] = before_fp
            step_report["footprint_after_gb"] = after_fp
        report["steps"].append(step_report)

    mem = machine_memory_probe()
    after_avail = mem.reclaimable if mem is not None else None
    report["available_after"] = after_avail
    needed_gb = needed_bytes / _BYTES_PER_GB
    margin_gb = margin / _BYTES_PER_GB
    if after_avail is not None and after_avail >= needed_gb + margin_gb:
        report["success"] = True
    return report


@dataclass
class Lease:
    """An approved memory lease. Call `release()` (or use it as a context manager) when done."""

    lease_id: str
    owner: str
    purpose: str
    reserved_bytes: int
    pid: int
    granted_at: float
    last_used_at: float
    project: str = "default"
    kv_ceiling_tokens: Optional[int] = None
    idle_ttl_s: float = DEFAULT_IDLE_TTL_S
    on_evict: Optional[Callable[[], None]] = field(default=None, repr=False, compare=False)
    _broker: Optional["MemoryBroker"] = field(default=None, repr=False, compare=False)

    def touch(self) -> None:
        """Call on every actual use of the model; it resets the idle-TTL clock."""
        self.last_used_at = time.time()

    def release(self) -> None:
        if self._broker is not None:
            self._broker.release(self)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


class MemoryBroker:
    """A per-process gatekeeper. Thread-safe. **There is deliberately no global singleton in
    this file**: each consuming project holds its own singleton (for example
    `myproject.memory.broker.broker_singleton`), constructed with its own `project=`."""

    def __init__(
        self,
        *,
        project: str = "default",
        safety_floor_bytes: Optional[int] = None,
        pressure_monitor: Optional[pressure.MemoryPressureMonitor] = None,
        total_ram_probe: Callable[[], int] = sensors.total_physical_memory_bytes,
        external_rss_probe: Callable[[], Optional[int]] = lambda: None,
        on_battery_probe: Callable[[], bool] = sensors.on_battery_power,
        system_margin_bytes: Optional[int] = None,
        swap_max_bytes: Optional[int] = None,
        battery_heavy_threshold_bytes: Optional[int] = None,
        machine_memory_probe: Callable[[], Optional[memprobe.MachineMemory]] = memprobe.machine_memory,
        swap_usage_probe: Callable[[], Tuple[Optional[float], Optional[float]]] = memprobe.swap_usage,
        heavy_processes_probe: Callable[[int], List[Dict[str, Any]]] = sensors.heavy_processes,
        ledger_path: Optional[Path] = None,
        sweep_interval_s: float = 30.0,
        require_kv_ceiling: bool = False,
    ) -> None:
        self._project = project
        # RLock, not Lock. An eviction callback (`on_evict`) that called `lease.release()`
        # => `broker.release()` from the same thread would self-deadlock on a plain Lock,
        # and every caller of `current_reservations()` (a daemon's /health) would wait
        # forever. The protection is two-fold: an RLock for the synchronous path (reserve
        # evicts LRU leases under the lock), and `on_evict` on the periodic / explicit paths
        # (sweep, release_all_now, critical pressure) runs **outside** the lock; see
        # `_evict_outside_lock`.
        self._lock = threading.RLock()
        self._sweep_interval_s = float(sweep_interval_s)
        self._sweep_stop = threading.Event()
        self._sweep_thread: Optional[threading.Thread] = None
        self._leases: Dict[str, Lease] = {}
        self._safety_floor_bytes = (
            safety_floor_bytes if safety_floor_bytes is not None else safety_floor_bytes_from_config()
        )
        self._pressure_monitor = pressure_monitor if pressure_monitor is not None else pressure.MemoryPressureMonitor()
        self._pressure_monitor.add_listener(self._on_pressure_change)
        self._total_ram_probe = total_ram_probe
        self._external_rss_probe = external_rss_probe
        self._on_battery_probe = on_battery_probe
        self._system_margin_bytes = (
            system_margin_bytes if system_margin_bytes is not None else system_margin_bytes_from_config()
        )
        self._swap_max_bytes = swap_max_bytes if swap_max_bytes is not None else swap_max_bytes_from_config()
        self._battery_heavy_threshold_bytes = (
            battery_heavy_threshold_bytes if battery_heavy_threshold_bytes is not None else BATTERY_HEAVY_THRESHOLD_BYTES
        )
        self._machine_memory_probe = machine_memory_probe
        self._swap_usage_probe = swap_usage_probe
        self._heavy_processes_probe = heavy_processes_probe
        self._ledger_path_override = ledger_path
        self._require_kv_ceiling = require_kv_ceiling

    # -- lifecycle of the pressure subscription + the sweep thread ----------------

    def start(self) -> None:
        self._pressure_monitor.start()
        if self._sweep_interval_s > 0 and (self._sweep_thread is None or not self._sweep_thread.is_alive()):
            self._sweep_stop.clear()
            self._sweep_thread = threading.Thread(target=self._sweep_loop, name=f"memguard-sweep-{self._project}", daemon=True)
            self._sweep_thread.start()

    def stop(self) -> None:
        self._sweep_stop.set()
        t = self._sweep_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._pressure_monitor.stop()

    def _sweep_loop(self) -> None:
        while not self._sweep_stop.wait(self._sweep_interval_s):
            try:
                evicted = self.sweep_idle()
                if evicted:
                    logger.info("periodic sweep: evicted %d idle leases: %s", len(evicted), evicted)
            except Exception:  # noqa: BLE001
                logger.warning("periodic sweep failed this round", exc_info=True)

    def _on_pressure_change(self, _old: pressure.PressureLevel, new: pressure.PressureLevel) -> None:
        if new == pressure.PressureLevel.CRITICAL:
            with self._lock:
                candidates = [lse for lse in self._leases.values() if lse.on_evict is not None]
            evicted = [lse.lease_id for lse in candidates if self._evict_outside_lock(lse)]
            if evicted:
                logger.warning("CRITICAL memory pressure: evicted %d leases immediately: %s", len(evicted), evicted)
            else:
                logger.warning("CRITICAL memory pressure: no evictable leases (none have on_evict) to release")

    # -- battery gate ------------------------------------------------------------

    def _battery_gate(self, owner: str, estimated_bytes: int, purpose: str) -> None:
        if estimated_bytes < self._battery_heavy_threshold_bytes:
            return
        try:
            on_battery = self._on_battery_probe()
        except Exception as exc:  # noqa: BLE001 (fail closed, in the safe direction)
            logger.warning("battery gate: reading the power state failed; treating it as battery and refusing", exc_info=True)
            raise BatteryPowerRefusedError(
                f"Not loading a model of {_fmt_gb(estimated_bytes)} ({owner}, {purpose or 'model load'}): "
                f"could not read the machine's power state ({exc}), so it is treated as "
                "battery and refused (fail closed, not guessing 'probably on AC'). Connect to power and retry."
            ) from exc

        if on_battery:
            raise BatteryPowerRefusedError(
                f"Not loading a model of {_fmt_gb(estimated_bytes)} on battery; connect to power. "
                f"({owner}, {purpose or 'model load'} | heavy threshold "
                f"{self._battery_heavy_threshold_bytes / _BYTES_PER_GB:.1f}GB, no bypass)"
            )

    # -- whole-machine gate ------------------------------------------------------

    def _machine_wide_gate(self, owner: str, estimated_bytes: int, purpose: str) -> None:
        check_machine_wide_headroom(
            estimated_bytes,
            context=f"reserve({owner!r}, {purpose or 'model load'})",
            system_margin_bytes=self._system_margin_bytes,
            swap_max_bytes=self._swap_max_bytes,
            machine_memory_probe=self._machine_memory_probe,
            swap_usage_probe=self._swap_usage_probe,
            heavy_processes_probe=self._heavy_processes_probe,
            ledger_entries_probe=self._cross_process_ledger_entries,
        )

    # -- ledger shared between processes / projects ------------------------------

    def _ledger_path(self) -> Path:
        return self._ledger_path_override if self._ledger_path_override is not None else default_ledger_path()

    def _cross_process_ledger_entries(self) -> List[Dict[str, Any]]:
        return cross_process_ledger_entries(self._ledger_path())

    def _record_lease_in_ledger(self, lease: "Lease") -> None:
        record_external_lease(
            self._ledger_path(),
            lease_id=lease.lease_id,
            owner=lease.owner,
            purpose=lease.purpose,
            reserved_bytes=lease.reserved_bytes,
            pid=lease.pid,
            project=lease.project,
            granted_at=lease.granted_at,
        )

    def _remove_lease_from_ledger(self, lease_id: str) -> None:
        remove_external_lease(self._ledger_path(), lease_id)

    # -- the central API ---------------------------------------------------------

    def reserve(
        self,
        owner: str,
        estimated_bytes: int,
        purpose: str = "",
        *,
        kv_ceiling_tokens: Optional[int] = None,
        idle_ttl_s: float = DEFAULT_IDLE_TTL_S,
        on_evict: Optional[Callable[[], None]] = None,
        pid: Optional[int] = None,
    ) -> Lease:
        """Tries to allocate `estimated_bytes` (bytes) for `owner`. If `require_kv_ceiling=True`
        (a policy set in the constructor, not here), `kv_ceiling_tokens` becomes mandatory.
        Returns a `Lease` or raises `MemoryReserveError` (or a subclass).

        The battery gate is checked **first** among the substantive gates; the whole-machine
        gate second; both run **before** taking the internal `_lock`."""
        if self._require_kv_ceiling and (kv_ceiling_tokens is None or kv_ceiling_tokens <= 0):
            raise MissingKVCeilingError(
                f"reserve({owner!r}) refused: call without an explicit KV ceiling "
                f"(kv_ceiling_tokens={kv_ceiling_tokens!r}). Loading without a ceiling is an error, not a default."
            )
        if estimated_bytes <= 0:
            raise MemoryReserveError(f"reserve({owner!r}) refused: estimated_bytes={estimated_bytes!r} is not positive.")

        self._battery_gate(owner, estimated_bytes, purpose)
        self._machine_wide_gate(owner, estimated_bytes, purpose)

        with self._lock:
            self._reap_dead_pids_locked()
            self._sweep_idle_locked()

            level = self._pressure_monitor.current_level()
            if level in (pressure.PressureLevel.WARN, pressure.PressureLevel.CRITICAL):
                raise MemoryReserveError(
                    f"reserve({owner!r}, {_fmt_gb(estimated_bytes)}) refused: memory-pressure level "
                    f"{level.value.upper()} (WARN = no new loads, CRITICAL = evict immediately). "
                    "Retry after the condition clears."
                )

            try:
                total_ram = self._total_ram_probe()
            except sensors.SensorUnavailableError as exc:
                raise MemoryReserveError(
                    f"reserve({owner!r}) refused: could not measure total physical memory ({exc}); "
                    "failing closed, not guessing."
                ) from exc

            external_rss = self._external_rss_probe() or 0

            headroom = self._headroom_locked(total_ram, external_rss)
            if headroom < estimated_bytes:
                self._evict_lru_until_locked(estimated_bytes - headroom)
                headroom = self._headroom_locked(total_ram, external_rss)

            if headroom < estimated_bytes:
                reserved_total = self._total_reserved_locked()
                raise MemoryReserveError(
                    f"reserve({owner!r}, {purpose or 'model load'}) refused: not enough memory. "
                    f"Total physical {_fmt_gb(total_ram)} | external (not ours) {_fmt_gb(external_rss)} | "
                    f"reserved by us {_fmt_gb(reserved_total)} | safety floor "
                    f"{_fmt_gb(self._safety_floor_bytes)} => headroom {_fmt_gb(max(headroom, 0))}, "
                    f"required {_fmt_gb(estimated_bytes)}. Active leases: {self._holders_summary_locked()}"
                )

            now = time.time()
            lease = Lease(
                lease_id=uuid.uuid4().hex[:12],
                owner=owner,
                purpose=purpose or "model load",
                reserved_bytes=int(estimated_bytes),
                project=self._project,
                kv_ceiling_tokens=kv_ceiling_tokens,
                pid=pid if pid is not None else os.getpid(),
                granted_at=now,
                last_used_at=now,
                idle_ttl_s=idle_ttl_s,
                on_evict=on_evict,
                _broker=self,
            )
            self._leases[lease.lease_id] = lease
            self._record_lease_in_ledger(lease)
            return lease

    def release(self, lease: "Lease | str") -> None:
        """Idempotent: a second call on the same lease is a no-op."""
        lease_id = lease.lease_id if isinstance(lease, Lease) else lease
        with self._lock:
            self._leases.pop(lease_id, None)
        self._remove_lease_from_ledger(lease_id)

    def release_all_now(self, reason: str = "manual") -> List[Dict[str, Any]]:
        with self._lock:
            candidates = [lse for lse in self._leases.values() if lse.on_evict is not None]
        released: List[Dict[str, Any]] = []
        for lease in candidates:
            info = {
                "lease_id": lease.lease_id,
                "owner": lease.owner,
                "purpose": lease.purpose,
                "reserved_bytes": lease.reserved_bytes,
            }
            if self._evict_outside_lock(lease):
                released.append(info)
        if released:
            logger.info("release_all_now(%s): evicted %d leases: %s", reason, len(released), [r["lease_id"] for r in released])
        return released

    def sweep_idle(self) -> List[str]:
        with self._lock:
            now = time.time()
            due = [
                lse for lse in self._leases.values()
                if lse.on_evict is not None and (now - lse.last_used_at) > lse.idle_ttl_s
            ]
        evicted = [lse.lease_id for lse in due if self._evict_outside_lock(lse)]
        if evicted:
            logger.info("idle-TTL: evicted %d leases: %s", len(evicted), evicted)
        return evicted

    def _evict_outside_lock(self, lease: Lease) -> bool:
        """Runs `on_evict` **without** holding `_lock`, so a callback that takes a lock of its
        own (or calls `release()`) cannot create a deadlock against another caller of the
        broker (AB-BA). If the lease was already released in the meantime by another thread,
        this is a no-op."""
        with self._lock:
            if lease.lease_id not in self._leases:
                return False
        try:
            lease.on_evict()  # type: ignore[misc]
        except Exception:
            logger.warning("on_evict failed for lease=%s owner=%s; it stays registered", lease.lease_id, lease.owner, exc_info=True)
            return False
        with self._lock:
            self._leases.pop(lease.lease_id, None)
        self._remove_lease_from_ledger(lease.lease_id)
        return True

    def current_reservations(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._reap_dead_pids_locked()
            return [
                {
                    "lease_id": lse.lease_id,
                    "owner": lse.owner,
                    "purpose": lse.purpose,
                    "reserved_bytes": lse.reserved_bytes,
                    "kv_ceiling_tokens": lse.kv_ceiling_tokens,
                    "pid": lse.pid,
                    "project": lse.project,
                    "granted_at": lse.granted_at,
                    "age_s": round(time.time() - lse.granted_at, 1),
                    "idle_s": round(time.time() - lse.last_used_at, 1),
                    "evictable": lse.on_evict is not None,
                }
                for lse in self._leases.values()
            ]

    def total_reserved_bytes(self) -> int:
        with self._lock:
            return self._total_reserved_locked()

    def headroom_bytes(self) -> int:
        """Diagnostic: current headroom (may be negative) from a live measurement; does not change state."""
        total_ram = self._total_ram_probe()
        external_rss = self._external_rss_probe() or 0
        with self._lock:
            return self._headroom_locked(total_ram, external_rss)

    def reclaim_for(
        self,
        owner: str,
        needed_bytes: int,
        *,
        steps: List[Tuple[str, Callable[[], None], Optional[int]]],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """A thin instance wrapper around the module-level `generic_reclaim_for` (see there)."""
        return generic_reclaim_for(
            owner,
            needed_bytes,
            steps=steps,
            machine_memory_probe=self._machine_memory_probe,
            system_margin_bytes=self._system_margin_bytes,
            **kwargs,
        )

    @property
    def safety_floor_bytes(self) -> int:
        return self._safety_floor_bytes

    @property
    def pressure_monitor(self) -> pressure.MemoryPressureMonitor:
        return self._pressure_monitor

    @property
    def project(self) -> str:
        return self._project

    # -- internal ----------------------------------------------------------------

    def _headroom_locked(self, total_ram: int, external_rss: int) -> int:
        reserved = self._total_reserved_locked()
        return total_ram - external_rss - reserved - self._safety_floor_bytes

    def _total_reserved_locked(self) -> int:
        return sum(lse.reserved_bytes for lse in self._leases.values())

    def _sweep_idle_locked(self) -> List[str]:
        now = time.time()
        evicted: List[str] = []
        for lease in list(self._leases.values()):
            if lease.on_evict is None:
                continue
            if (now - lease.last_used_at) > lease.idle_ttl_s:
                if self._evict_locked(lease):
                    evicted.append(lease.lease_id)
        if evicted:
            logger.info("idle-TTL: evicted %d leases: %s", len(evicted), evicted)
        return evicted

    def _evict_lru_until_locked(self, need_bytes: int) -> int:
        candidates = sorted(
            (lse for lse in self._leases.values() if lse.on_evict is not None),
            key=lambda lse: lse.last_used_at,
        )
        freed = 0
        for lease in candidates:
            if freed >= need_bytes:
                break
            reserved = lease.reserved_bytes
            if self._evict_locked(lease):
                freed += reserved
        return freed

    def _evict_all_evictable_locked(self) -> List[str]:
        evicted = []
        for lease in list(self._leases.values()):
            if lease.on_evict is not None and self._evict_locked(lease):
                evicted.append(lease.lease_id)
        return evicted

    def _evict_locked(self, lease: Lease) -> bool:
        assert lease.on_evict is not None
        try:
            lease.on_evict()
        except Exception:
            logger.warning("on_evict failed for lease=%s owner=%s; it stays registered", lease.lease_id, lease.owner, exc_info=True)
            return False
        self._leases.pop(lease.lease_id, None)
        self._remove_lease_from_ledger(lease.lease_id)
        return True

    def _reap_dead_pids_locked(self) -> None:
        dead = [lid for lid, lse in self._leases.items() if not _pid_alive(lse.pid)]
        for lid in dead:
            del self._leases[lid]
            self._remove_lease_from_ledger(lid)
        if dead:
            logger.info("reap: removed %d leases of dead processes: %s", len(dead), dead)

    def _holders_summary_locked(self) -> str:
        if not self._leases:
            return "no active leases"
        return "; ".join(
            f"{lse.owner} ({_fmt_gb(lse.reserved_bytes)}, pid={lse.pid}, "
            f"{'evictable' if lse.on_evict else 'pinned'})"
            for lse in self._leases.values()
        )
