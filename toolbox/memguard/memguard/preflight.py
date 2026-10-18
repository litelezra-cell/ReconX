"""memguard.preflight: admission gate for a **standalone** process before it loads a large
model, plus the daemon registry.

Three checks, in order:
  (a) Every **registered** daemon (`register_daemon`): a loaded model of at least
      `daemon_loaded_threshold_bytes` (10 GB) means refusal. With `ask_daemon_release=True`
      it calls `call_rpc` (injected per project, because the release protocol is
      project-specific: it may be a WebSocket RPC or an HTTP POST) and waits (by polling
      real measurements, not by assumption) until the memory is **measured** as released.
  (b) `ollama ps`: non-empty means refusal (read-only, fully generic).
  (c) **Exactly the same whole-machine check** as `broker.check_machine_wide_headroom`, not
      a look-alike copy.

**Daemon registry:** a hard-coded list of well-known ports only works while there is a
single project. With several projects, `register_daemon` / `list_registered_daemons` let the
generic CLI (`memguard-preflight`) ask **all** daemons without knowing in advance which
projects exist. Each owning daemon can keep using its own injected `find_daemon` / `call_rpc`
(the generic HTTP versions here are the defaults) inside `preflight_standalone_load`; the
registry is an addition for the diagnostic CLI, not a replacement for a project's internal
preflight flow.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.request
import uuid

from memguard import broker as broker_module
from memguard import memprobe, sensors
from memguard.state import daemons_dir

logger = logging.getLogger("memguard.preflight")

DEFAULT_HOST = "127.0.0.1"
DAEMON_LOADED_MODEL_THRESHOLD_BYTES = 10 * 1024**3
HTTP_TIMEOUT_S = 2.0

DEFAULT_RELEASE_POLL_TIMEOUT_S = 30.0
DEFAULT_RELEASE_POLL_INTERVAL_S = 0.5


class PreflightRefusedError(RuntimeError):
    """Preflight refusal: a question of timing / coordination between processes, not allocation accounting."""

    def __init__(self, message: str, code: str = "preflight_refused", params: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code or "preflight_refused"
        self.params = params or {}


# -----------------------------------------------------------------------------
# Daemon registry: `<state-dir>/daemons/<name>.json`
# -----------------------------------------------------------------------------


def register_daemon(name: str, url: str, *, pid: Optional[int] = None) -> Path:
    """Writes `<state-dir>/daemons/<name>.json` = `{name, url, pid, registered_at}`. Each
    project daemon calls this at startup (lifespan/startup) with the base URL of its own
    `/health`, and removes it (`unregister_daemon`) on clean exit. `pid` defaults to
    `os.getpid()`."""
    d = daemons_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    payload = {"name": name, "url": url, "pid": pid if pid is not None else os.getpid(), "registered_at": time.time()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def unregister_daemon(name: str) -> None:
    """Removes the daemon registration, best effort (does not raise if it no longer exists)."""
    path = daemons_dir() / f"{name}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("unregister_daemon(%s) failed", name, exc_info=True)


def list_registered_daemons() -> List[Dict[str, Any]]:
    """All registered daemons whose pid is still alive (dead records are reaped automatically)."""
    d = daemons_dir()
    if not d.is_dir():
        return []
    live: List[Dict[str, Any]] = []
    for entry in sorted(d.glob("*.json")):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = data.get("pid")
        if isinstance(pid, int) and not broker_module.pid_alive(pid):
            try:
                entry.unlink()
            except OSError:
                pass
            continue
        live.append(data)
    return live


# -----------------------------------------------------------------------------
# (a) Querying daemons: generic HTTP by default; a project may inject its own find_daemon / call_rpc
# -----------------------------------------------------------------------------


def _http_get_json(url: str, timeout_s: float) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 (localhost / locally registered URLs only)
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def find_live_daemon(
    host: str = DEFAULT_HOST, ports: Tuple[int, ...] = (), timeout_s: float = HTTP_TIMEOUT_S, **_kw: Any
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """**Generic** version: issues GET `<url>/health` against **every registered daemon**
    (`list_registered_daemons`) instead of hard-coded ports. Returns `(url, health_json)` of
    the first live one, or `None`. A project with its own daemon protocol (for example known
    ports on a WebSocket bridge) injects its own `find_daemon` into
    `preflight_standalone_load` instead of this one; this is the default for the diagnostic
    CLI and for projects with nothing special."""
    for entry in list_registered_daemons():
        url = entry.get("url")
        if not url:
            continue
        data = _http_get_json(f"{url.rstrip('/')}/health", timeout_s)
        if data is not None:
            return url, data
    return None


def call_daemon_release_http(url: str, method: str = "release_models", **_kw: Any) -> Dict[str, Any]:
    """**Generic** default for a release request: `POST <url>/<method>`. A project with its
    own RPC protocol (for example a WebSocket bridge) injects its own `call_rpc`. `method` is
    the shared generic parameter (see `preflight_standalone_load(release_method=...)`) that
    lets both sides agree on "what is being requested" without making the signature of
    `call_rpc` project-specific."""
    req = urllib.request.Request(f"{url.rstrip('/')}/{method}", data=b"{}", method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise PreflightRefusedError(f"release request to {url} failed: {exc}", code="release_request_failed") from exc


# -----------------------------------------------------------------------------
# (b) Ollama: read-only, fully generic
# -----------------------------------------------------------------------------


def ollama_ps() -> List[str]:
    """Lines of `ollama ps` (without the header), or `[]`, including when Ollama is not installed or not running (a normal state)."""
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0 or not out.stdout:
        return []
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    return lines[1:] if len(lines) > 1 else []


# -----------------------------------------------------------------------------
# Waiting for release (--ask-release): polling with real measurements
# -----------------------------------------------------------------------------


def _wait_for_measured_release(
    *,
    needed_available_bytes: float,
    machine_memory_probe: Callable[[], Optional["memprobe.MachineMemory"]],
    timeout_s: float,
    interval_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> bool:
    deadline = clock() + timeout_s
    while clock() < deadline:
        mem = machine_memory_probe()
        if mem is not None and mem.reclaimable is not None and mem.reclaimable * (1024**3) >= needed_available_bytes:
            return True
        sleep(interval_s)
    return False


# -----------------------------------------------------------------------------
# Main function
# -----------------------------------------------------------------------------


def preflight_standalone_load(
    estimated_bytes: int,
    *,
    context: str = "preflight",
    ask_daemon_release: bool = False,
    release_method: str = "release_models",
    host: str = DEFAULT_HOST,
    ports: Tuple[int, ...] = (),
    daemon_loaded_threshold_bytes: int = DAEMON_LOADED_MODEL_THRESHOLD_BYTES,
    release_poll_timeout_s: float = DEFAULT_RELEASE_POLL_TIMEOUT_S,
    release_poll_interval_s: float = DEFAULT_RELEASE_POLL_INTERVAL_S,
    find_daemon: Callable[..., Optional[Tuple[Any, Dict[str, Any]]]] = find_live_daemon,
    call_rpc: Callable[..., Dict[str, Any]] = None,
    ollama_ps_probe: Callable[[], List[str]] = ollama_ps,
    system_margin_bytes: Optional[int] = None,
    swap_max_bytes: Optional[int] = None,
    machine_memory_probe: Callable[[], Optional["memprobe.MachineMemory"]] = memprobe.machine_memory,
    swap_usage_probe: Callable[[], Tuple[Optional[float], Optional[float]]] = memprobe.swap_usage,
    heavy_processes_probe: Callable[[int], List[Dict[str, Any]]] = sensors.heavy_processes,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Runs the three checks, in order. Returns a report (`checks: [...]`) on success; raises
    `PreflightRefusedError` on refusal (a)/(b), and the `MemoryReserveError` family that
    `reserve()` would also raise on refusal (c), which is **exactly the same call** to
    `broker.check_machine_wide_headroom`. There is no bypass flag for any of the three.

    `find_daemon` / `call_rpc` default to the **generic** implementations (HTTP, daemon
    registry); a project with its own daemon protocol injects its own (see the module
    docstring). `call_rpc` is invoked as `call_rpc(daemon_ref, release_method, host=host)`:
    **two positional arguments**, not just `daemon_ref`, so that both a port-based RPC
    (`call_rpc(port, method, **kw)`) and the generic HTTP one (`call_rpc(url, method, **kw)`)
    share one signature; `release_method` (default `"release_models"`) is chosen by each
    project for itself."""
    if call_rpc is None:
        call_rpc = call_daemon_release_http

    report: Dict[str, Any] = {"context": context, "estimated_bytes": int(estimated_bytes), "checks": []}

    # (a) Daemon
    live = find_daemon(host=host, ports=ports)
    if live is None:
        report["checks"].append({"name": "daemon_models_loaded", "daemon": None, "models_loaded": [], "note": "no live or registered daemon"})
    else:
        daemon_ref, health = live
        models_loaded = health.get("models_loaded") or []
        big = [m for m in models_loaded if int(m.get("bytes") or 0) >= daemon_loaded_threshold_bytes]
        report["checks"].append({"name": "daemon_models_loaded", "daemon": daemon_ref, "models_loaded": models_loaded})

        if big:
            if not ask_daemon_release:
                names = ", ".join(f"{m.get('provider_id')} ({int(m.get('bytes') or 0) / (1024**3):.1f}GB)" for m in big)
                raise PreflightRefusedError(
                    f"{context}: refused. The daemon ({daemon_ref}) already has {names} loaded (>= "
                    f"{daemon_loaded_threshold_bytes / (1024**3):.0f}GB). Loading another model in a "
                    "separate process could crash the machine. Use --ask-release to request a "
                    "release, or wait / stop the daemon.",
                    code="daemon_model_loaded",
                    params={"models_loaded": big, "daemon": daemon_ref},
                )

            rpc_result = call_rpc(daemon_ref, release_method, host=host)
            report["checks"].append({"name": "daemon_release_requested", "result": rpc_result})

            needed_available = estimated_bytes + (
                system_margin_bytes if system_margin_bytes is not None else broker_module.system_margin_bytes_from_config()
            )
            released_in_time = _wait_for_measured_release(
                needed_available_bytes=needed_available,
                machine_memory_probe=machine_memory_probe,
                timeout_s=release_poll_timeout_s,
                interval_s=release_poll_interval_s,
                sleep=sleep,
                clock=clock,
            )
            report["checks"].append({"name": "daemon_release_confirmed_by_vm_stat", "passed": released_in_time})
            if not released_in_time:
                raise PreflightRefusedError(
                    f"{context}: refused. A release was requested from the daemon, but after "
                    f"{release_poll_timeout_s:.0f}s the measurement still did not show enough free memory. "
                    "Not proceeding on an assumption, only on a measurement.",
                    code="daemon_release_not_confirmed",
                    params={"waited_s": release_poll_timeout_s},
                )

    # (b) Ollama
    ollama_lines = ollama_ps_probe()
    report["checks"].append({"name": "ollama_ps", "loaded_lines": ollama_lines})
    if ollama_lines:
        raise PreflightRefusedError(
            f"{context}: refused. Ollama currently has {len(ollama_lines)} model(s) loaded (`ollama ps`). "
            "There is no bypass flag; stop Ollama or wait.",
            code="ollama_loaded",
            params={"loaded_lines": ollama_lines},
        )

    # (c) Exactly the same machine-wide check as the broker
    broker_module.check_machine_wide_headroom(
        int(estimated_bytes),
        context=context,
        system_margin_bytes=(
            system_margin_bytes if system_margin_bytes is not None else broker_module.system_margin_bytes_from_config()
        ),
        swap_max_bytes=(swap_max_bytes if swap_max_bytes is not None else broker_module.swap_max_bytes_from_config()),
        machine_memory_probe=machine_memory_probe,
        swap_usage_probe=swap_usage_probe,
        heavy_processes_probe=heavy_processes_probe,
        ledger_entries_probe=lambda: broker_module.cross_process_ledger_entries(broker_module.default_ledger_path()),
    )
    report["checks"].append({"name": "machine_wide_headroom", "passed": True})
    return report
