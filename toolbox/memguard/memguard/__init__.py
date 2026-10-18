"""memguard: a machine-wide memory gate for local model loading.

A self-contained Python package (stdlib + ctypes only, no third-party dependencies) with
**one ledger per machine** under `~/.local/state/memguard/` (override: `MEMGUARD_STATE_DIR`).
See `README.md` and `protocol.md` for the full contract. Projects typically build a thin
shim on top (for example `myproject/memory/broker.py`) that injects project defaults.
"""

__version__ = "0.1.0"
