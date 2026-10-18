"""Shared, stdlib-only helpers for the witness guard and the witness worker.

The worker runs under whatever Python interpreter a stage is configured with, so this module
must not import anything outside the standard library.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

GIB = 1024**3
STAGES = ("ocr", "asr_w1", "asr_w2", "vlm")
MAX_TIMEOUT_S = 3600
CONFIG_ENV = "RECON_RUNNER_CONFIG"
ROOT_ENV = "RECON_OUT_DIR"
DEFAULT_CONFIG = Path(__file__).with_name("runner.json")


class RunnerRefused(RuntimeError):
    """A request, path or configuration the runner will not act on."""


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load and validate the runner configuration (``runner.json``)."""
    chosen = Path(path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)
    if not chosen.is_file():
        raise RunnerRefused(f"Runner config not found: {chosen} (copy runner.example.json to runner.json)")
    try:
        config = json.loads(chosen.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RunnerRefused(f"Runner config is not valid JSON: {exc}") from exc
    stages = config.get("stages")
    if not isinstance(stages, dict):
        raise RunnerRefused("Runner config needs a 'stages' object")
    for name, entry in stages.items():
        if name not in STAGES:
            raise RunnerRefused(f"Unknown stage in config: {name}")
        if not isinstance(entry, dict):
            raise RunnerRefused(f"Stage {name} must be an object")
        budget = entry.get("budget_bytes")
        if entry.get("enabled", True) and (not isinstance(budget, int) or budget <= 0):
            raise RunnerRefused(f"Stage {name}: budget_bytes must be a positive integer")
    config["_path"] = str(chosen.resolve())
    return config


def stage_config(config: dict[str, Any], stage: str) -> dict[str, Any]:
    if stage not in STAGES:
        raise RunnerRefused(f"Unknown witness stage: {stage!r}")
    entry = config.get("stages", {}).get(stage)
    if not entry:
        raise RunnerRefused(f"Stage {stage} is not configured")
    if not entry.get("enabled", True):
        raise RunnerRefused(f"Stage {stage} is disabled in the runner config")
    return entry


def resolve_root(out_dir: str | os.PathLike[str] | None = None) -> Path:
    """The target root every manifest, input and output must live under."""
    raw = out_dir or os.environ.get(ROOT_ENV)
    if not raw:
        raise RunnerRefused(f"No target root: pass --out-dir or set {ROOT_ENV}")
    root = Path(raw).resolve()
    if not root.is_dir():
        raise RunnerRefused(f"Target root is not a directory: {root}")
    return root


def is_inside(path: str | os.PathLike[str], root: Path) -> bool:
    resolved = Path(path).resolve()
    return resolved != root and resolved.is_relative_to(root)


def require_inside(field: str, path: str | os.PathLike[str], root: Path) -> Path:
    if not path or not is_inside(path, root):
        raise RunnerRefused(f"{field} is outside the target root")
    return Path(path).resolve()


def require_input_file(field: str, path: str | os.PathLike[str], root: Path) -> Path:
    resolved = require_inside(field, path, root)
    if not resolved.is_file():
        raise RunnerRefused(f"{field} is not a file: {resolved.name}")
    return resolved


def manifest_inputs(data: dict[str, Any]) -> list[str]:
    """Every input file path a manifest refers to (audio, frames, batch frames)."""
    frames = list(data.get("frames", []))
    for batch in data.get("batches", []):
        frames.extend(batch.get("frames", []))
    paths = [data["audio_path"]] if data.get("audio_path") else []
    paths += [f["path"] if isinstance(f, dict) else f for f in frames]
    return paths


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_json(path: str | os.PathLike[str], data: Any) -> None:
    target = Path(path)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)
