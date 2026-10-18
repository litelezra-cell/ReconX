# memguard — daemon protocol

The contract between `memguard` and any project daemon that wants the machine-wide gate to
see it and to be able to ask it to release memory.

## 1. What every daemon must expose

### 1.1 `GET <base-url>/health` returns `models_loaded`

The daemon's normal health response gains one extra key:

```json
{
  "status": "ok",
  "models_loaded": [
    {"provider_id": "vision:example_model_bf16", "bytes": 80530636800, "loaded_at": 1758000000.0, "idle_s": 5.0}
  ]
}
```

`models_loaded` is **always a list** (empty when nothing is loaded), never `null` and never
a missing key. `bytes` is an `int` (bytes, not GB). `provider_id`, `loaded_at` and `idle_s`
are human-readable and are not fed back into any decision logic.

A typical implementation maps the project broker's current reservations
(`current_reservations()`) to this shape. `/health` should return a cached snapshot and must
not take locks or measure memory in the request path.

### 1.2 Release: `release_models`

Some way to ask the daemon to release everything it *can* release (leases that have an
`on_evict` callback) and report what was actually released. The exact transport is
project-specific, because each daemon already has its own RPC/HTTP channel:

- **Generic default (memguard):** `POST <base-url>/release_models`. Request and response
  bodies are free-form; `memguard.preflight.call_daemon_release_http` only checks that the
  call did not fail.
- **Custom transport:** a project that exposes release over another channel (for example a
  WebSocket RPC) injects its own `call_rpc` into `preflight_standalone_load` instead of
  using the default.

Each daemon implements its own side (the mechanism that actually releases is
`broker.release_all_now()`) and exposes or injects the matching call path.

## 2. Daemon registration

So that the generic CLI can find every daemon, each one registers at startup
(lifespan/startup hook):

```python
from memguard.preflight import register_daemon
register_daemon("myproject", "http://127.0.0.1:8765")
```

This writes `~/.local/state/memguard/daemons/<name>.json` containing
`{name, url, pid, registered_at}`. On clean exit call `unregister_daemon("myproject")`.
A record whose pid is no longer alive is removed automatically (`list_registered_daemons`),
so there is no need to clean up after a crash.

Registration is a **diagnostic addition** for `memguard-preflight` (the CLI). It does not
replace a project's own internal preflight flow, which may keep using its own
`find_live_daemon` / `call_daemon_rpc` helpers that know that project's exact ports and
protocol.

## 3. The global ledger: `~/.local/state/memguard/leases.json`

Every successful `reserve()` (from any project, any process) writes a row:
`{owner, purpose, reserved_bytes, pid, project, granted_at}`. A lease that has been
released, or whose `pid` is dead, is removed automatically. The `project` field lets a
refusal message say "held by alpha:model_x" instead of a bare pid.

The ledger is a **decision input**, not just an observation (see the `broker.py` module
docstring): the whole-machine gate subtracts the bytes that *other* processes have already
declared, before they have necessarily allocated them, which closes the race between two
processes that start loading in the same second.

Override the location with the `MEMGUARD_STATE_DIR` environment variable (tests must do this).

## 4. Live demonstration without models

```bash
memguard-demo --project alpha --gb 70 --hold-seconds 25 &
sleep 2
memguard-demo --project beta  --gb 60 --hold-seconds 5
```

The `alpha` process receives a simulated 70 GB lease (written to the ledger, no real memory
touched). The `beta` process, one or two seconds later, asks for 60 GB; 70 + 60 = 130 GB
exceeds 128 GB of physical RAM even with generous margins, so it is refused with a
`MachineMemoryRefusedError` whose message names the **project and owner of the first
holder** ("alpha:demo:alpha ... pid=..."). Once `alpha` releases (at the end of its
`hold-seconds`, or on `kill`), a retry by `beta` is granted.

## 5. What not to change without a new measurement

- The ledger file name and structure (`leases.json`): any schema change breaks
  cross-project compatibility.
- The fail-closed direction of every gate (a failed measurement means refusal, not an
  assumption).
- The small absolute swap threshold (`MEMGUARD_SWAP_MAX_GB`, default 2 GB). A project that
  wants another threshold overrides it through the environment variable or passes an
  explicit `swap_max_bytes=` to its own broker, rather than changing the global default.
