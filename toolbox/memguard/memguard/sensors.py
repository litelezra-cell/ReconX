"""memguard.sensors: generic memory/system sensors, ctypes-based where possible.

Every function here **measures a fact about the machine**, with no assumption about who
calls it, so any project can share them. Project-specific sensors (for example the RSS of a
specific named host application) belong in the consuming project's own layer, which imports
these generic ones and adds its own.

Core principle: functions here only **measure**. No risk interpretation ("dangerous",
"close to the ceiling") lives here; that is policy for a layer above (`broker.py`,
`pressure.py`, or a consuming project's own policy layer).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional

# -----------------------------------------------------------------------------
# libproc / proc_pid_rusage: RSS and process scanning
# -----------------------------------------------------------------------------

PROC_ALL_PIDS = 1  # sys/proc_info.h
RUSAGE_INFO_V2 = 2  # sys/resource.h; the version verified against `ps -o rss`


class _RusageInfoV2(ctypes.Structure):
    """Exact C structure from `sys/resource.h` (RUSAGE_INFO_V2). Field order is critical:
    ctypes reads raw memory in exactly this order, and a mistake does not raise; it silently
    reads the wrong bytes. 19 fields; `ri_resident_size` is the eighth (byte offset 64)."""

    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


@lru_cache(maxsize=1)
def _libc() -> ctypes.CDLL:
    """libSystem (which also contains libproc); loaded once for the process lifetime."""
    lib = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_RusageInfoV2)]
    lib.proc_pid_rusage.restype = ctypes.c_int
    lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    lib.proc_listpids.restype = ctypes.c_int
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    lib.proc_pidpath.restype = ctypes.c_int
    return lib


def proc_rss_bytes(pid: int) -> Optional[int]:
    """Raw RSS (bytes) of any `pid` via proc_pid_rusage. `None` if the process does not
    exist or permission is denied; **does not raise**."""
    libc = _libc()
    buf = _RusageInfoV2()
    rc = libc.proc_pid_rusage(int(pid), RUSAGE_INFO_V2, ctypes.byref(buf))
    if rc != 0:
        return None
    return int(buf.ri_resident_size)


def list_pids() -> List[int]:
    """All currently live pids (`proc_listpids` / `PROC_ALL_PIDS`), ctypes only, no fork."""
    libc = _libc()
    needed = libc.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
    if needed <= 0:
        return []
    count = needed // ctypes.sizeof(ctypes.c_int32)
    arr = (ctypes.c_int32 * count)()
    written = libc.proc_listpids(PROC_ALL_PIDS, 0, ctypes.byref(arr), needed)
    if written <= 0:
        return []
    got = written // ctypes.sizeof(ctypes.c_int32)
    return [int(arr[i]) for i in range(got) if int(arr[i]) > 0]


def pid_executable_path(pid: int) -> Optional[str]:
    """Executable path of `pid` via `proc_pidpath`. `None` (does not raise) if the process
    died between the scan and the call, or permission is denied (another user's process)."""
    libc = _libc()
    buf = ctypes.create_string_buffer(4096)
    rc = libc.proc_pidpath(int(pid), buf, 4096)
    if rc <= 0:
        return None
    return buf.value.decode("utf-8", "replace")


def find_pid_by_path_suffix(path_suffix: str) -> Optional[int]:
    """Returns the first pid (from `list_pids()`) whose executable path ends with
    `path_suffix`. `None` if not found; does not raise ("process not running" is a normal
    state). Generic on purpose: a consuming project that wants the RSS of a specific
    external process calls this locally with its own suffix."""
    for pid in list_pids():
        path = pid_executable_path(pid)
        if path is not None and path.endswith(path_suffix):
            return pid
    return None


# Default threshold for "who is holding memory" in a refusal message: processes above 5 GB.
HEAVY_PROCESS_THRESHOLD_BYTES = 5 * 1024**3


def heavy_processes(threshold_bytes: int = HEAVY_PROCESS_THRESHOLD_BYTES) -> List[Dict[str, object]]:
    """Every process whose phys_footprint (memprobe.process_footprint_gb, GPU/Metal included)
    exceeds `threshold_bytes`. **ctypes only**: no `ps` subprocess, no psutil (no new
    dependency). Sorted by footprint, descending. **Observation only**: the decision itself
    relies on `memprobe.available_memory_gb()`, not on this list."""
    from memguard import memprobe

    threshold_gb = threshold_bytes / (1024**3)
    results: List[Dict[str, object]] = []
    for pid in list_pids():
        fp_gb = memprobe.process_footprint_gb(pid)
        if fp_gb is None or fp_gb < threshold_gb:
            continue
        path = pid_executable_path(pid) or f"pid:{pid}"
        name = path.rsplit("/", 1)[-1]
        results.append({"pid": pid, "name": name, "path": path, "footprint_gb": fp_gb})
    results.sort(key=lambda p: p["footprint_gb"], reverse=True)
    return results


# -----------------------------------------------------------------------------
# vm_stat / sysctl: system state
# -----------------------------------------------------------------------------


class SensorUnavailableError(RuntimeError):
    """Raised when real system state could not be measured (missing command, unexpected
    output, timeout).

    Intent: the caller (broker.py) **must** catch this and fail closed (refuse the
    allocation) rather than assume everything is fine. "Could not measure, so refuse", not
    "could not measure, so warn and continue".
    """


_VM_STAT_PAGESIZE_RE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_LINE_RE = re.compile(r'^"?([A-Za-z][A-Za-z0-9 _\-]*?)"?:\s+(\d+)\.?\s*$')
_SWAPUSAGE_FIELD_RE = re.compile(r"(total|used|free)\s*=\s*([\d.]+)([KMGT]?)")

_SIZE_SUFFIX_MULTIPLIER = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _run_text(cmd: list) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SensorUnavailableError(f"running {cmd!r} failed: {exc}") from exc
    return proc.stdout


def _parse_vm_stat(text: str) -> Dict[str, int]:
    page_size_match = _VM_STAT_PAGESIZE_RE.search(text)
    if not page_size_match:
        raise SensorUnavailableError(
            f"vm_stat: 'page size of N bytes' not found in the header (unexpected format):\n{text[:200]!r}"
        )
    fields: Dict[str, int] = {"__page_size_bytes__": int(page_size_match.group(1))}
    for line in text.splitlines():
        m = _VM_STAT_LINE_RE.match(line.strip())
        if m:
            label, value = m.group(1).strip(), int(m.group(2))
            fields[label] = value
    return fields


def _parse_swapusage(text: str) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for key, num, suffix in _SWAPUSAGE_FIELD_RE.findall(text):
        multiplier = _SIZE_SUFFIX_MULTIPLIER.get(suffix.upper(), 1)
        result[key] = int(round(float(num) * multiplier))
    if "used" not in result or "total" not in result:
        raise SensorUnavailableError(f"sysctl vm.swapusage: unexpected format:\n{text!r}")
    return result


@lru_cache(maxsize=1)
def total_physical_memory_bytes() -> int:
    """Total physical memory (bytes) via `sysctl hw.memsize`. Constant for the process lifetime, hence cached."""
    text = _run_text(["sysctl", "-n", "hw.memsize"])
    try:
        return int(text.strip())
    except ValueError as exc:
        raise SensorUnavailableError(f"sysctl hw.memsize: not a number: {text!r}") from exc


@dataclass(frozen=True)
class SystemMemoryState:
    """Raw snapshot of system state: facts only, no risk interpretation."""

    page_size_bytes: int
    pages_free: int
    pages_active: int
    pages_inactive: int
    pages_speculative: int
    pages_wired: int
    pages_purgeable: int
    total_bytes: int
    free_bytes: int
    swap_used_bytes: int
    swap_total_bytes: int


def system_memory_state() -> SystemMemoryState:
    """Measures vm_stat + sysctl (swapusage + hw.memsize). Raises `SensorUnavailableError`
    (not a bare KeyError/ValueError) if parsing fails."""
    vm = _parse_vm_stat(_run_text(["vm_stat"]))
    swap = _parse_swapusage(_run_text(["sysctl", "vm.swapusage"]))
    page_size = vm["__page_size_bytes__"]

    required = ("Pages free", "Pages active", "Pages inactive", "Pages speculative",
                "Pages wired down", "Pages purgeable")
    missing = [k for k in required if k not in vm]
    if missing:
        raise SensorUnavailableError(f"vm_stat: missing fields {missing} (unexpected format)")

    pages_free = vm["Pages free"]
    return SystemMemoryState(
        page_size_bytes=page_size,
        pages_free=pages_free,
        pages_active=vm["Pages active"],
        pages_inactive=vm["Pages inactive"],
        pages_speculative=vm["Pages speculative"],
        pages_wired=vm["Pages wired down"],
        pages_purgeable=vm["Pages purgeable"],
        total_bytes=total_physical_memory_bytes(),
        free_bytes=pages_free * page_size,
        swap_used_bytes=swap["used"],
        swap_total_bytes=swap["total"],
    )


# -----------------------------------------------------------------------------
# Power source: battery vs AC
# -----------------------------------------------------------------------------

_PMSET_POWER_SOURCE_RE = re.compile(r"Now drawing from '([^']+)'")
_PMSET_ON_BATTERY = "battery power"
_PMSET_ON_AC = "ac power"


def on_battery_power() -> bool:
    """`True` when the machine is currently drawing power from the battery, `False` when it
    is on AC.

    **Raises `SensorUnavailableError` rather than guessing** if `pmset` fails, is missing or
    returns an unrecognised format: fail-**closed**. A consuming project may deliberately
    choose the opposite (fail-**open**) in its own policy layer; the two are kept separate
    on purpose (see the memprobe.py docstring)."""
    text = _run_text(["pmset", "-g", "batt"])
    match = _PMSET_POWER_SOURCE_RE.search(text)
    if not match:
        raise SensorUnavailableError(
            f"pmset -g batt: no \"Now drawing from '...'\" line found (unexpected format):\n{text[:200]!r}"
        )
    source = match.group(1).strip().lower()
    if source == _PMSET_ON_BATTERY:
        return True
    if source == _PMSET_ON_AC:
        return False
    raise SensorUnavailableError(
        f"pmset -g batt: unrecognised power source {match.group(1)!r}; not silently interpreting it as AC."
    )
