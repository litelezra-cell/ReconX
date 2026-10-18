# memguard — a machine-wide memory gate for local model loading

`memguard` is a small, dependency-free Python package (stdlib + `ctypes` only) that decides
whether a process may load a large model *right now*. Every load site calls `reserve()`
before allocating. The broker measures the **whole machine** through `ctypes` (no
subprocess, no fork, which matters because forking can hang when memory is already
exhausted) instead of trusting only what the calling process itself holds. A single ledger
per machine (`~/.local/state/memguard/leases.json`) is shared by every process and every
project, which closes the race in which two processes start loading in the same second,
both see "free", and both proceed.

## Why it exists

Two processes in two different projects each loaded a ~70 GB model. Each project had its
own broker that counted only its own leases, so each was "right" by its own accounting.
Together they exhausted the machine, it swapped heavily and froze. The design that follows
from this:

- **One ledger per machine**, shared across processes and projects.
- **Whole-machine measurement via `ctypes`**, never a subprocess (no fork under memory pressure).
- **Swap in use means refuse** (fail-closed).
- **Large models are released after an idle period**, with a periodic sweep.

## Platform

macOS on Apple Silicon is the supported target. The macOS-specific parts are:

- `memprobe.py` reads memory through `libSystem` via `ctypes`: `host_statistics64`
  (free / inactive / compressed pages), `sysctl` (total RAM, swap usage) and per-process
  footprint calls.
- `sensors.py` reads processes via libproc, memory via `vm_stat`/`sysctl`, and power source via `pmset`.
- `pressure.py` subscribes to `DISPATCH_SOURCE_TYPE_MEMORYPRESSURE` (libdispatch) and falls
  back to polling when the subscription is unavailable.

On other platforms `memprobe.py` cannot load libproc/libSystem symbols, so it falls back
to parsing `vm_stat` / `sysctl` output through a subprocess, and returns `None` when that
fails too. Callers treat `None` as "could not measure" and refuse (fail-closed). The
ledger, lease logic and daemon registry are plain Python (the ledger uses `fcntl` file
locks, so POSIX only), but the package has only been validated on macOS; the process
scanning and battery sensors in `sensors.py` use macOS interfaces (`libproc`, `pmset`) and
raise `SensorUnavailableError` elsewhere.

## Install

```bash
pip install -e path/to/toolbox/memguard
```

This provides the `memguard` package and two console scripts, `memguard-preflight` and
`memguard-demo`. Python 3.9+ is required; there are no third-party dependencies.

## Wiring a project

1. **Local shim.** Do not call `memguard` directly from every load site. Create
   `myproject/memory/broker.py` that subclasses `memguard.broker.MemoryBroker`, injects your
   project defaults (an external RSS sensor if you have one, `project="myproject"`,
   `require_kv_ceiling` if relevant) and exposes a module-level singleton
   (for example `broker_singleton`).
2. **`reserve()` at every load site** (mlx, whisper, CLIP, torch, `ollama pull`, ...), before
   the allocation and without exceptions. A source-level grep gate in your test suite is
   the recommended way to enforce this.

   ```python
   from myproject.memory.broker import broker_singleton

   lease = broker_singleton.reserve("my-model", estimated_bytes, "describe-shot")
   try:
       model = load_model(...)
   finally:
       lease.release()   # or hand the lease to the model object and release on unload
   ```

3. **Standalone scripts** that load a large model in a single process (not through a
   daemon) must run a preflight first, either from Python:

   ```python
   from memguard.preflight import preflight_standalone_load
   preflight_standalone_load(75_000_000_000, context="my-script", ask_daemon_release=True)
   ```

   or from the shell:

   ```bash
   memguard-preflight <bytes> [--ask-release] [--project NAME] [--gb N] [--json]
   ```

   It asks every registered daemon what it holds, checks `ollama ps`, runs the
   whole-machine gate, and exits 0 if the load may proceed or 1 with the refusal message
   on stderr. `--ask-release` asks the daemon that holds models to release them first.
4. **Daemons** register themselves at startup with
   `memguard.preflight.register_daemon("<name>", "<base-url>")`, unregister on exit, report
   `models_loaded` in `/health`, and handle a release request. The full contract is in
   [`protocol.md`](protocol.md).
5. **Tests** must export `MEMGUARD_STATE_DIR` to a temporary directory before importing
   anything, otherwise they read and write the real machine ledger. See
   `tests/test_sandbox_tripwire.py` for a tripwire that fails the suite if the sandbox is
   missing.

## Rules to keep

- Do not add third-party dependencies (stdlib + `ctypes` only; that is the point of the
  package: no fork or subprocess on the time-critical path).
- Do not change the global swap threshold (`MEMGUARD_SWAP_MAX_GB`, default 2 GB) without a
  new measurement. A project that wants a different threshold passes its own
  `swap_max_bytes=` to its broker instead of changing the default.
- The ledger is a **decision input**, not just an observation: bytes already declared by
  other processes are subtracted before a new reservation is granted. See the `broker.py`
  module docstring.
- Gates are fail-closed: a failed measurement results in refusal, never an assumption.

## Layout

```
memguard/
  state.py       - MEMGUARD_STATE_DIR, ledger and daemon-registry paths
  memprobe.py    - raw measurement (ctypes): footprint, machine_memory, swap, total_ram
  sensors.py     - battery / heavy processes / system memory state (generic, macOS)
  pressure.py    - DISPATCH_SOURCE_TYPE_MEMORYPRESSURE subscription + polling fallback
  broker.py      - MemoryBroker: reserve / release / reclaim_for / sweep_idle, global ledger
  preflight.py   - standalone-process gate + daemon registry
  cli.py         - memguard-preflight, memguard-demo
tests/           - self-contained suite (unittest, stdlib only)
protocol.md      - daemon contract (health / release / registration)
run_tests.sh     - the supported way to run the suite
```

## Tests

```bash
./run_tests.sh              # whole suite, verbose
./run_tests.sh 'test_broker.py'   # one file (the argument is a unittest -p pattern)
```

`run_tests.sh` exports `MEMGUARD_STATE_DIR` to a fresh temporary directory (removed on
exit) and runs `python3 -m unittest discover -s tests`. Never run the suite against the
real `~/.local/state/memguard`.

## Live demonstration without models

```bash
memguard-demo --project alpha --gb 70 --hold-seconds 25 &
sleep 2
memguard-demo --project beta  --gb 60 --hold-seconds 5
```

The first process takes a simulated 70 GB lease (written to the ledger, no real memory
touched). The second asks for 60 GB, which together exceed physical RAM on a 128 GB
machine once safety margins are applied, so it is refused with a message naming the first
holder's project and owner. After the first process releases, a retry succeeds.

## License

MIT. See the `LICENSE` file at the repository root.
