"""memguard.state: the machine-wide state path, and how to override it for tests/sandboxes.

**Contract:** the `MEMGUARD_STATE_DIR` environment variable overrides
`~/.local/state/memguard/`. It **must** be exported to a temporary directory for every test
run (memguard's own and any project wired to it); otherwise tests read and write the
machine's **real** ledger. A sandbox that depends on someone remembering to set it is not a
sandbox, so enforcement should be mechanical (see `tests/test_sandbox_tripwire.py`).

Environment variables are not read at import time by other modules: every function here
reads `os.environ` **at call time**, so a test that changes the variable mid-run
(`monkeypatch` / `os.environ[...] = ...`) is seen immediately, independent of import order.
This is also what lets `tests/__init__.py` override it safely.
"""

from __future__ import annotations

import os
from pathlib import Path

_STATE_DIR_ENV_VAR = "MEMGUARD_STATE_DIR"
_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "memguard"

LEASES_FILENAME = "leases.json"
DAEMONS_SUBDIR = "daemons"


def memguard_state_dir() -> Path:
    """The global state directory. `MEMGUARD_STATE_DIR` overrides; the default is
    `~/.local/state/memguard`. It does **not create** the directory: callers run
    `.mkdir(parents=True, exist_ok=True)` at the point of writing (`broker.py` /
    `preflight.py`), so that a read-only call (for example a test that merely inspects the
    path) does not create directories as a side effect."""
    raw = os.environ.get(_STATE_DIR_ENV_VAR)
    return Path(raw).expanduser() if raw else _DEFAULT_STATE_DIR


def default_ledger_path() -> Path:
    """Path of the machine-wide ledger: `<state-dir>/leases.json`. See `broker.py` for the structure."""
    return memguard_state_dir() / LEASES_FILENAME


def daemons_dir() -> Path:
    """Daemon registry directory: `<state-dir>/daemons/<name>.json` per live daemon. See `preflight.py`."""
    return memguard_state_dir() / DAEMONS_SUBDIR


def is_real_state_dir() -> bool:
    """`True` when `MEMGUARD_STATE_DIR` is **not** overridden, i.e. reads/writes currently
    go to the machine's real state. Used by test tripwires (see
    tests/test_sandbox_tripwire.py here, and the equivalents in consuming projects)."""
    return not bool(os.environ.get(_STATE_DIR_ENV_VAR))
