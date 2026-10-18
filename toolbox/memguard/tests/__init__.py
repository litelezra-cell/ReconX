"""State sandbox for all memguard tests; must run **before** any import of `memguard.state`.

Without this, a test that relies on the default location (no explicit `ledger_path=` and no
`MEMGUARD_STATE_DIR`) would write to the machine's **real** `~/.local/state/memguard/`.
`unittest discover -s tests` does not necessarily execute this file in every invocation
style; see `tests/test_sandbox_tripwire.py` for a mechanical tripwire.
"""
import os as _os
import tempfile as _tempfile

if not _os.environ.get("MEMGUARD_STATE_DIR"):
    _os.environ["MEMGUARD_STATE_DIR"] = _tempfile.mkdtemp(prefix="memguard-tests-state-")
