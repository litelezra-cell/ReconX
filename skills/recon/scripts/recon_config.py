"""Machine-local settings for Recon.

The skill and the protocol carry no machine paths: where ffmpeg lives, which venv has opencv or
Playwright, where the guarded witness runner is, which past analyses are good examples — all of that
is in one local JSON file that is not published with the skill.

Lookup: $RECON_CONFIG, else ~/.claude/recon-local/config.json. Keys are documented in
config.example.json next to this file. A missing file or key means "not configured": callers fall
back to PATH lookups and report the gap; they never guess a path.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

DEFAULT_PATH = Path.home() / ".claude/recon-local/config.json"


def config_path() -> Path:
    return Path(os.environ.get("RECON_CONFIG") or DEFAULT_PATH).expanduser()


def load() -> dict:
    p = config_path()
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def expand(value):
    """Expand ~ and $VARS in every string of a config value (str, list or dict)."""
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    return value


def get(key: str, default=None):
    cfg = load()
    return expand(cfg[key]) if key in cfg else default


def tool(name: str) -> str | None:
    """Executable path: env RECON_<NAME>, then the config key <name>, then PATH."""
    env = os.environ.get(f"RECON_{name.upper()}")
    if env:
        return os.path.expanduser(env)
    configured = get(name)
    if configured and os.access(configured, os.X_OK):
        return configured
    return shutil.which(name)
