"""memguard.memprobe: memory measurement with ctypes only, **zero fork**.

**Why this module exists:** the classic way to measure memory is to run a subprocess
(`vm_stat`, `sysctl`, `ps`) while the machine is running out of memory. Under pressure the
kernel's `fork()` must duplicate the parent's page tables and can stall on page-out, exactly
when the measurement is most needed. Every measurement here goes through **ctypes calls into
system libraries**, with no fork on the normal path:
  `proc_pid_rusage`    - libproc (macOS): per-process phys_footprint (includes GPU/Metal)
  `host_statistics64`  - libSystem/libc: machine-wide page counters (free/inactive/wired/...)
  `sysctlbyname`       - libSystem/libc: vm.swapusage, hw.memsize, hw.pagesize

**Platform:** these are macOS interfaces. If the libraries cannot be loaded, the functions
fall back to subprocess parsing (`vm_stat`, `sysctl`), and return `None` when that fails
too; callers treat `None` as "could not measure" (fail-closed), never as a made-up value.

**What is deliberately not in this generic module:** project-specific policy layers built on
top of raw measurement (multi-way ceilings, Metal wired-limit tuning, AC-power checks that
fail open, hard-coded page sizes). `on_ac_power`-style helpers fail **open** while
`sensors.on_battery_power` (generic, in `memguard.sensors`) fails **closed**; merging them
would silently change the failure behaviour of one side.

**Page size is measured, not hard-coded:** `sysctlbyname("hw.pagesize")` is another cheap
ctypes call with no fork. It is 16384 on Apple Silicon today and will not silently break on
a different architecture.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import struct
import subprocess
from typing import Dict, Optional, Tuple

_GB = 1024.0**3
_BYTES_PER_GB = 1024**3

# Libraries: loaded once; `None` if not macOS or on failure (every function below then
# falls back to subprocess parsing instead of raising at import).
# -----------------------------------------------------------------------------

_libproc: Optional[ctypes.CDLL] = None
_libsys: Optional[ctypes.CDLL] = None


def _load_libs() -> None:
    global _libproc, _libsys
    if _libproc is not None or _libsys is not None:
        return
    try:
        path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        _libproc = ctypes.CDLL(path, use_errno=True)
    except OSError:
        _libproc = None
    try:
        path = ctypes.util.find_library("c") or "/usr/lib/libSystem.B.dylib"
        _libsys = ctypes.CDLL(path, use_errno=True)
    except OSError:
        _libsys = None


_load_libs()


# -----------------------------------------------------------------------------
# Page size: measured (see module docstring), not hard-coded. Cached for the process lifetime.
# -----------------------------------------------------------------------------

_PAGE_SIZE_FALLBACK = 16_384  # Apple Silicon default, used if both ctypes and subprocess fail
_page_size_cache: Optional[int] = None


def page_size_bytes() -> int:
    global _page_size_cache
    if _page_size_cache is not None:
        return _page_size_cache
    if _libsys is not None:
        try:
            val = ctypes.c_uint32(0)
            size = ctypes.c_size_t(4)
            rc = _libsys.sysctlbyname(b"hw.pagesize", ctypes.byref(val), ctypes.byref(size), None, 0)
            if rc == 0 and val.value > 0:
                _page_size_cache = int(val.value)
                return _page_size_cache
        except Exception:
            pass
    try:
        out = subprocess.run(["sysctl", "-n", "hw.pagesize"], capture_output=True, text=True, timeout=5.0)
        if out.returncode == 0 and (out.stdout or "").strip().isdigit():
            _page_size_cache = int(out.stdout.strip())
            return _page_size_cache
    except Exception:
        pass
    _page_size_cache = _PAGE_SIZE_FALLBACK
    return _page_size_cache


# -----------------------------------------------------------------------------
# Per-process phys_footprint: proc_pid_rusage, RUSAGE_INFO_V0 (enough for the field we need)
# -----------------------------------------------------------------------------
#
# Layout of rusage_info_v0 (libproc.h): offset 72 = ri_phys_footprint (uint64, the eighth field):
#   uint8_t  ri_uuid[16]          offset 0
#   uint64_t ri_user_time         offset 16
#   uint64_t ri_system_time       offset 24
#   uint64_t ri_pkg_idle_wkups    offset 32
#   uint64_t ri_interrupt_wkups   offset 40
#   uint64_t ri_pageins           offset 48
#   uint64_t ri_wired_size        offset 56
#   uint64_t ri_resident_size     offset 64
#   uint64_t ri_phys_footprint    offset 72  <- the field we read
_RUSAGE_INFO_V0 = 0
_PHYS_FOOTPRINT_OFFSET = 72
_RUSAGE_BUF_SIZE = 512  # far larger than the actual structure (V6 <= 224 bytes)


def process_footprint_gb(pid: int) -> Optional[float]:
    """phys_footprint of `pid` (includes Metal/GPU), in GB. **This is the metric Activity
    Monitor and jetsam actually use.** `None` when libproc is unavailable; `0.0` when the
    pid does not exist (rc != 0: the process is gone and holds nothing; not an error)."""
    if _libproc is None:
        return None
    buf = ctypes.create_string_buffer(_RUSAGE_BUF_SIZE)
    try:
        rc = _libproc.proc_pid_rusage(ctypes.c_int(int(pid)), ctypes.c_int(_RUSAGE_INFO_V0), ctypes.byref(buf))
    except Exception:
        return None
    if rc != 0:
        return 0.0
    raw = buf.raw[_PHYS_FOOTPRINT_OFFSET : _PHYS_FOOTPRINT_OFFSET + 8]
    return round(int.from_bytes(raw, "little") / _GB, 3)


# -----------------------------------------------------------------------------
# Whole-machine memory: host_statistics64 (replaces a `vm_stat` subprocess on the gate path)
# -----------------------------------------------------------------------------

_HOST_VM_INFO64 = 4  # HOST_VM_INFO64 flavor
_HOST_VM_INFO64_COUNT = 38  # sizeof(vm_statistics64_data_t) / sizeof(integer_t)


class _VmStatistics64(ctypes.Structure):
    """vm_statistics64_data_t (`<mach/vm_statistics.h>`) as an explicit ctypes.Structure
    (not manual offsets into a flat array): uint64 fields occupy two 32-bit slots, and an
    ordering mistake would silently read the wrong bytes into
    purgeable_count / speculative_count / compressor_page_count."""

    _fields_ = [
        ("free_count", ctypes.c_uint32),
        ("active_count", ctypes.c_uint32),
        ("inactive_count", ctypes.c_uint32),
        ("wire_count", ctypes.c_uint32),
        ("zero_fill_count", ctypes.c_uint64),
        ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64),
        ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64),
        ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64),
        ("purgeable_count", ctypes.c_uint32),
        ("speculative_count", ctypes.c_uint32),
        ("decompressions", ctypes.c_uint64),
        ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64),
        ("swapouts", ctypes.c_uint64),
        ("compressor_page_count", ctypes.c_uint32),
        ("throttled_count", ctypes.c_uint32),
        ("external_page_count", ctypes.c_uint32),
        ("internal_page_count", ctypes.c_uint32),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


class MachineMemory:
    """Snapshot of whole-machine memory, in GB. `None` in a field means it was not measured."""

    __slots__ = ("free", "inactive", "purgeable", "speculative", "wired", "compressor", "reclaimable")

    def __init__(
        self,
        *,
        free: Optional[float],
        inactive: Optional[float],
        purgeable: Optional[float],
        speculative: Optional[float],
        wired: Optional[float],
        compressor: Optional[float],
    ) -> None:
        self.free = free
        self.inactive = inactive
        self.purgeable = purgeable
        self.speculative = speculative
        self.wired = wired
        self.compressor = compressor
        # reclaimable = free + inactive + purgeable + speculative: everything the kernel
        # can hand back without I/O.
        if free is not None and inactive is not None and purgeable is not None:
            spec = speculative or 0.0
            self.reclaimable: Optional[float] = round(free + inactive + purgeable + spec, 3)
        else:
            self.reclaimable = None

    def __repr__(self) -> str:  # pragma: no cover (debugging convenience only)
        return (
            f"MachineMemory(free={self.free}, inactive={self.inactive}, purgeable={self.purgeable}, "
            f"speculative={self.speculative}, reclaimable={self.reclaimable}, wired={self.wired}, "
            f"compressor={self.compressor})"
        )


def machine_memory() -> Optional[MachineMemory]:
    """Reads `vm_statistics64` through `host_statistics64`: no subprocess, no fork.
    Falls back to `vm_stat` parsing if the ctypes call fails; `None` only if that fails too."""
    if _libsys is None:
        return _machine_memory_fallback()
    try:
        host_self = _libsys.mach_host_self()
        vm_stat = _VmStatistics64()
        count = ctypes.c_uint32(_HOST_VM_INFO64_COUNT)
        rc = _libsys.host_statistics64(
            ctypes.c_uint(host_self), ctypes.c_int(_HOST_VM_INFO64), ctypes.byref(vm_stat), ctypes.byref(count)
        )
    except Exception:
        return _machine_memory_fallback()
    if rc != 0:
        return _machine_memory_fallback()

    ps = page_size_bytes()

    def _gb(pages: int) -> float:
        return round(pages * ps / _GB, 3)

    return MachineMemory(
        free=_gb(vm_stat.free_count),
        inactive=_gb(vm_stat.inactive_count),
        purgeable=_gb(vm_stat.purgeable_count),
        speculative=_gb(vm_stat.speculative_count),
        wired=_gb(vm_stat.wire_count),
        compressor=_gb(vm_stat.compressor_page_count),
    )


def _machine_memory_fallback() -> Optional[MachineMemory]:
    """Last resort: parse the text output of `vm_stat` (a subprocess), **only** if the ctypes path failed."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5.0)
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    text = out.stdout
    m = re.search(r"page size of (\d+) bytes", text)
    if not m:
        return None
    ps = int(m.group(1))
    pages: Dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        value = value.strip().rstrip(".")
        if value.isdigit():
            pages[label.strip().strip('"')] = int(value)

    def _p(key: str) -> Optional[float]:
        v = pages.get(key)
        return round(v * ps / _GB, 3) if v is not None else None

    return MachineMemory(
        free=_p("Pages free"),
        inactive=_p("Pages inactive"),
        purgeable=_p("Pages purgeable"),
        speculative=_p("Pages speculative"),
        wired=_p("Pages wired down"),
        compressor=_p("Pages occupied by compressor"),
    )


def available_memory_gb() -> Optional[float]:
    """Immediately reclaimable memory in GB: free + inactive + purgeable + speculative
    (see `MachineMemory.reclaimable`). `None` (never a made-up 0.0) when measurement fails."""
    mem = machine_memory()
    return mem.reclaimable if mem is not None else None


def free_memory_gb() -> Optional[float]:
    """"Pages free" only (the conservative, traditional definition of "free"), in GB."""
    mem = machine_memory()
    return mem.free if mem is not None else None


# -----------------------------------------------------------------------------
# swap: sysctlbyname("vm.swapusage") (xsw_usage structure, sys/sysctl.h)
# -----------------------------------------------------------------------------

# xsw_usage: uint64 total, uint64 avail, uint64 used, uint32 pagesize, uint8 encrypted
_XSW_USAGE_FMT = "=QQQIB"
_XSW_USAGE_SIZE = struct.calcsize(_XSW_USAGE_FMT)


def swap_usage() -> Tuple[Optional[float], Optional[float]]:
    """(total_gb, used_gb) via `sysctlbyname`; no subprocess on the normal path."""
    if _libsys is not None:
        try:
            buf = ctypes.create_string_buffer(_XSW_USAGE_SIZE + 8)
            size = ctypes.c_size_t(_XSW_USAGE_SIZE + 8)
            rc = _libsys.sysctlbyname(b"vm.swapusage", buf, ctypes.byref(size), None, 0)
            if rc == 0 and size.value >= _XSW_USAGE_SIZE:
                total, _avail, used = struct.unpack_from(_XSW_USAGE_FMT, buf.raw)[:3]
                return round(total / _GB, 3), round(used / _GB, 3)
        except Exception:
            pass
    return _swap_usage_fallback()


def _swap_usage_fallback() -> Tuple[Optional[float], Optional[float]]:
    _UNIT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    try:
        out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5.0)
        if out.returncode != 0 or not out.stdout:
            return None, None
        m_total = re.search(r"\btotal\s*=\s*([\d.]+)\s*([KMGT]?)", out.stdout)
        m_used = re.search(r"\bused\s*=\s*([\d.]+)\s*([KMGT]?)", out.stdout)
        if not m_total or not m_used:
            return None, None
        total = float(m_total.group(1)) * _UNIT.get(m_total.group(2), 1) / _GB
        used = float(m_used.group(1)) * _UNIT.get(m_used.group(2), 1) / _GB
        return round(total, 3), round(used, 3)
    except Exception:
        return None, None


# -----------------------------------------------------------------------------
# Total physical RAM: sysctlbyname("hw.memsize")
# -----------------------------------------------------------------------------


def total_ram_gb() -> Optional[float]:
    """Physical RAM in GB via `sysctlbyname`. `None` (never an invented constant) if the
    fallback fails too; `broker.py` treats `None` as "could not measure, so fail closed",
    the guiding principle of the package. A consuming project that wants a documented
    default for its own machine should apply it in its own layer, not here, so that no
    made-up value is forced on other consumers."""
    if _libsys is not None:
        try:
            val = ctypes.c_uint64(0)
            size = ctypes.c_size_t(8)
            rc = _libsys.sysctlbyname(b"hw.memsize", ctypes.byref(val), ctypes.byref(size), None, 0)
            if rc == 0 and size.value == 8 and val.value > 0:
                return round(val.value / _GB, 3)
        except Exception:
            pass
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5.0)
        if out.returncode == 0 and (out.stdout or "").strip().isdigit():
            return round(int(out.stdout.strip()) / _GB, 3)
    except Exception:
        pass
    return None


# -----------------------------------------------------------------------------
# swap_starvation: relative thresholds. Available for adoption; not wired in by default.
# -----------------------------------------------------------------------------

SWAP_ABS_LIMIT_GB = 16.0
SWAP_REMAINING_LIMIT_GB = 3.0


def swap_starvation(*, swap_total_gb: Optional[float], swap_used_gb: Optional[float]) -> Tuple[bool, str]:
    """True when the swap state indicates memory starvation, using **relative** thresholds
    (unlike the small absolute swap gate in `broker.py`). Available for a project that finds
    the small absolute threshold too noisy; each consuming project chooses for itself."""
    if swap_used_gb is None or swap_total_gb is None:
        return False, ""
    remaining = swap_total_gb - swap_used_gb
    if swap_used_gb > SWAP_ABS_LIMIT_GB:
        return True, f"swap in use {swap_used_gb:.1f}GB > {SWAP_ABS_LIMIT_GB:.0f}GB"
    if swap_total_gb > 8.0 and remaining < SWAP_REMAINING_LIMIT_GB:
        return True, f"swap total {swap_total_gb:.1f}GB > 8GB and {remaining:.1f}GB remaining < {SWAP_REMAINING_LIMIT_GB:.0f}GB"
    if swap_total_gb > 1.0 and swap_used_gb > 4.0 and (swap_used_gb / swap_total_gb) > 0.80:
        frac = swap_used_gb / swap_total_gb
        return True, f"swap in use {swap_used_gb:.1f}/{swap_total_gb:.1f}GB ({frac * 100:.0f}% > 80%)"
    return False, ""


if __name__ == "__main__":  # pragma: no cover (manual read-only self-check)
    import time

    print("=== memguard.memprobe self-check ===")
    print(f"page_size_bytes: {page_size_bytes()}")
    print(f"total_ram_gb: {total_ram_gb()}")
    t0 = time.perf_counter()
    mem = machine_memory()
    dt_ms = (time.perf_counter() - t0) * 1000
    print(f"machine_memory ({dt_ms:.3f}ms): {mem}")
    print(f"available_memory_gb: {available_memory_gb()}")
    t0 = time.perf_counter()
    fp = process_footprint_gb(os.getpid())
    dt_ms = (time.perf_counter() - t0) * 1000
    print(f"self footprint ({dt_ms:.3f}ms): {fp}GB")
    total_swap, used_swap = swap_usage()
    print(f"swap: used={used_swap} / total={total_swap} GB")
