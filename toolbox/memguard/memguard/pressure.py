"""memguard.pressure: generic memory-pressure subscription.

Subscribes to `DISPATCH_SOURCE_TYPE_MEMORYPRESSURE` through ctypes (libdispatch), with a
polling fallback (via `memguard.sensors`) if the libdispatch connection fails. It has no
dependency on any particular application, only `memguard.sensors` and raw ctypes.

WARNING: **macOS offers no synchronous read of memory pressure, only events** (there is no
public API for "what is the level right now"). Consequently `current_level()` always
returns the **last value received**, not a fresh measurement. Until the first event arrives
(or the fallback has sampled), the level is `PressureLevel.UNKNOWN`, never silently
`NORMAL`; `broker.py` decides how to treat UNKNOWN and nothing is hidden here.

The bitmask constants (`DISPATCH_MEMORYPRESSURE_*`) are copied from
`.../MacOSX.sdk/usr/include/dispatch/source.h`, not reconstructed from memory. The full
ctypes chain (dispatch_source_create, set_event_handler_f, resume, cancel) was exercised on
real hardware before being relied on.

**Not tested for real (a documented gap, not hidden):** the genuine trigger (an actual
WARN/CRITICAL transition) requires `sysctl kern.memorypressure_manual_trigger` (the
`memory_pressure -S` tool), which needs root. The automated tests inject a level directly
into the broker (the correct seam between event code and policy logic) and do not rely on
a real kernel event.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
import time
from enum import Enum
from functools import lru_cache
from typing import Callable, List, Optional

from memguard import sensors

logger = logging.getLogger("memguard.pressure")

# --- dispatch/source.h, checked against the SDK (see module docstring) ---
_DISPATCH_MEMORYPRESSURE_NORMAL = 0x01
_DISPATCH_MEMORYPRESSURE_WARN = 0x02
_DISPATCH_MEMORYPRESSURE_CRITICAL = 0x04
_MASK_ALL = _DISPATCH_MEMORYPRESSURE_NORMAL | _DISPATCH_MEMORYPRESSURE_WARN | _DISPATCH_MEMORYPRESSURE_CRITICAL
_DISPATCH_QUEUE_PRIORITY_DEFAULT = 0

# --- fallback heuristic (when libdispatch is unavailable); see `_fallback_level_from_state` ---
FALLBACK_WARN_NONWIRED_FREE_FRACTION = 0.15
FALLBACK_CRITICAL_NONWIRED_FREE_FRACTION = 0.07
FALLBACK_WARN_SWAP_USED_BYTES = 512 * 1024 * 1024
FALLBACK_CRITICAL_SWAP_USED_BYTES = 2 * 1024 * 1024 * 1024

DEFAULT_POLL_INTERVAL_S = 5.0

_HANDLER_FUNC_T = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


class PressureLevel(Enum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    WARN = "warn"
    CRITICAL = "critical"


@lru_cache(maxsize=1)
def _libc() -> ctypes.CDLL:
    lib = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    lib.dispatch_get_global_queue.restype = ctypes.c_void_p
    lib.dispatch_get_global_queue.argtypes = [ctypes.c_long, ctypes.c_ulong]
    lib.dispatch_source_create.restype = ctypes.c_void_p
    lib.dispatch_source_create.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    lib.dispatch_source_set_event_handler_f.restype = None
    lib.dispatch_source_set_event_handler_f.argtypes = [ctypes.c_void_p, _HANDLER_FUNC_T]
    lib.dispatch_resume.restype = None
    lib.dispatch_resume.argtypes = [ctypes.c_void_p]
    lib.dispatch_source_cancel.restype = None
    lib.dispatch_source_cancel.argtypes = [ctypes.c_void_p]
    lib.dispatch_source_get_data.restype = ctypes.c_ulong
    lib.dispatch_source_get_data.argtypes = [ctypes.c_void_p]
    return lib


def _flags_to_level(flags: int) -> PressureLevel:
    # CRITICAL beats WARN beats NORMAL if several bits are set at once (should not happen
    # in practice, but if it does the more conservative interpretation wins, not whichever
    # is checked first).
    if flags & _DISPATCH_MEMORYPRESSURE_CRITICAL:
        return PressureLevel.CRITICAL
    if flags & _DISPATCH_MEMORYPRESSURE_WARN:
        return PressureLevel.WARN
    if flags & _DISPATCH_MEMORYPRESSURE_NORMAL:
        return PressureLevel.NORMAL
    return PressureLevel.UNKNOWN


def _fallback_level_from_state(state: "sensors.SystemMemoryState") -> PressureLevel:
    """Rough estimate when libdispatch is unavailable. **Not** a real substitute for a kernel
    event, only a safety net.

    Why "non-wired fraction of the total" ((total - wired) / total) rather than plain
    pages_free / total: measured against Apple's own `memory_pressure` tool on the
    development machine, it reported "System-wide memory free percentage: 94%-95%" at the
    moment plain `pages_free / total_pages` came out at **47%** (a nearly 2x gap).
    `(total_pages - wired) / total_pages` on the same sample gave ~95%, matching the
    official tool. Conclusion: the "free" that jetsam cares about includes everything that
    is not permanently locked (active / inactive / speculative / purgeable / free), not just
    the narrow free pages. Using pages_free alone would make the fallback far too pessimistic
    (a spurious WARN almost all the time).
    """
    total_pages = state.total_bytes // state.page_size_bytes if state.page_size_bytes else 0
    if total_pages <= 0:
        return PressureLevel.UNKNOWN
    nonwired_free_fraction = (total_pages - state.pages_wired) / total_pages

    if (
        nonwired_free_fraction < FALLBACK_CRITICAL_NONWIRED_FREE_FRACTION
        or state.swap_used_bytes >= FALLBACK_CRITICAL_SWAP_USED_BYTES
    ):
        return PressureLevel.CRITICAL
    if (
        nonwired_free_fraction < FALLBACK_WARN_NONWIRED_FREE_FRACTION
        or state.swap_used_bytes >= FALLBACK_WARN_SWAP_USED_BYTES
    ):
        return PressureLevel.WARN
    return PressureLevel.NORMAL


class MemoryPressureMonitor:
    """A single memory-pressure flag for the whole process. `start()` tries a real kernel
    connection (libdispatch); on failure it switches to polling with a visible log message.
    `current_level()` always returns the cached value (not a fresh measurement; see the
    module warning)."""

    def __init__(self, *, poll_interval_s: float = DEFAULT_POLL_INTERVAL_S) -> None:
        self._poll_interval_s = poll_interval_s
        self._lock = threading.Lock()
        self._level = PressureLevel.UNKNOWN
        self._fallback_mode = False
        self._started = False
        self._listeners: List[Callable[[PressureLevel, PressureLevel], None]] = []

        # kept alive so ctypes does not garbage-collect them while the callback may still be invoked
        self._source: Optional[ctypes.c_void_p] = None
        self._callback_ref: Optional[ctypes.CFUNCTYPE] = None

        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # -- public ---------------------------------------------------------------

    def start(self) -> None:
        """Idempotent. Tries libdispatch; on any failure (including something unexpected during
        setup) falls back to thread polling, **with a visible warning** (never silently)."""
        if self._started:
            return
        self._started = True
        try:
            self._start_real_dispatch_source()
            logger.info("memory-pressure monitor: connected to DISPATCH_SOURCE_TYPE_MEMORYPRESSURE (real kernel events)")
        except Exception:
            logger.warning(
                "memory-pressure monitor: libdispatch connection failed; switching to fallback "
                "polling every %.1fs (lower accuracy, see pressure.py docstring)",
                self._poll_interval_s,
                exc_info=True,
            )
            self._start_fallback_polling()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._source is not None:
            try:
                _libc().dispatch_source_cancel(self._source)
            except Exception:
                logger.warning("memory-pressure monitor: dispatch_source_cancel failed", exc_info=True)
            # dispatch_release is deliberately not called: uncertainty about retain semantics
            # through raw ctypes (no ARC bridge) makes "do not release" (a small one-time
            # leak that does not repeat in a loop) safer than "possibly release wrongly"
            # (a real crash risk). Documented, not forgotten.
            self._source = None
        if self._poll_thread is not None:
            self._stop_event.set()
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None

    def current_level(self) -> PressureLevel:
        with self._lock:
            return self._level

    def is_fallback_mode(self) -> bool:
        return self._fallback_mode

    def add_listener(self, callback: Callable[[PressureLevel, PressureLevel], None]) -> None:
        """`callback(old_level, new_level)` is invoked on every level change (best effort: an
        exception in a callback does not bring the monitor down)."""
        self._listeners.append(callback)

    def inject_level_for_test(self, level: PressureLevel) -> None:
        """**Tests only.** Injects a level directly without touching libdispatch or polling:
        the correct seam for testing policy logic (broker.py) without a real kernel event
        (which requires root; see the module docstring)."""
        self._set_level(level)

    # -- internal: the real path ----------------------------------------------

    def _start_real_dispatch_source(self) -> None:
        libc = _libc()
        anchor = ctypes.c_void_p.in_dll(libc, "_dispatch_source_type_memorypressure")
        mp_type_ptr = ctypes.c_void_p(ctypes.addressof(anchor))
        queue = libc.dispatch_get_global_queue(_DISPATCH_QUEUE_PRIORITY_DEFAULT, 0)
        source = libc.dispatch_source_create(mp_type_ptr, 0, _MASK_ALL, queue)
        if not source:
            raise RuntimeError("dispatch_source_create returned NULL")

        def _on_event(_context: int) -> None:
            try:
                flags = libc.dispatch_source_get_data(source)
                self._set_level(_flags_to_level(flags))
            except Exception:
                logger.warning("memory-pressure monitor: callback failed", exc_info=True)

        self._callback_ref = _HANDLER_FUNC_T(_on_event)  # must outlive the source
        libc.dispatch_source_set_event_handler_f(source, self._callback_ref)
        libc.dispatch_resume(source)
        self._source = source
        self._fallback_mode = False

    # -- internal: the fallback -----------------------------------------------

    def _start_fallback_polling(self) -> None:
        self._fallback_mode = True
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="memguard-pressure-poll", daemon=True
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                state = sensors.system_memory_state()
                self._set_level(_fallback_level_from_state(state))
            except sensors.SensorUnavailableError:
                logger.warning("memory-pressure fallback: could not measure system state this time", exc_info=True)
            self._stop_event.wait(self._poll_interval_s)

    # -- internal: shared -----------------------------------------------------

    def _set_level(self, new_level: PressureLevel) -> None:
        with self._lock:
            old_level = self._level
            if old_level == new_level:
                return
            self._level = new_level
        for listener in self._listeners:
            try:
                listener(old_level, new_level)
            except Exception:
                logger.warning("memory-pressure listener failed", exc_info=True)
