#!/usr/bin/env python3
"""Preflight for the recon skill.

Reads (never writes): the global protocol, the RUN-RECORD template, the
project's own adapter/rules, and which capture/ASR tools exist on disk.
Prints one JSON object. Exit 2 = protocol missing (the skill must stop).

"Found on disk" is not "verified working": the architect still runs each
tool once on the real input before promising a channel.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recon_config  # noqa: E402

HOME = Path.home()
TOOLBOX = HOME / ".claude/toolbox/video-analysis"
PROTOCOL = TOOLBOX / "README.md"
TEMPLATE = TOOLBOX / "RUN-RECORD.template.json"
MEMGUARD = HOME / ".claude/toolbox/memguard"
# The protocol version this skill's runner steps were written against.
# A newer protocol still wins; the skill reports the drift instead of stopping.
SKILL_WRITTEN_FOR = "2026-10-14.1"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_root(start: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def which_any(names: list[str], extra: list[str]) -> list[str]:
    found = []
    for n in names:
        p = shutil.which(n)
        if p:
            found.append(p)
    for pattern in filter(None, extra):
        found.extend(p for p in glob.glob(os.path.expanduser(pattern)) if os.access(p, os.X_OK))
    return sorted(set(found))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.getcwd(), help="project folder (default: cwd)")
    args = ap.parse_args()

    report: dict = {"skill_written_for_protocol": SKILL_WRITTEN_FOR}

    # 1. Protocol — the source of truth. Missing => stop.
    if not PROTOCOL.is_file():
        report["protocol"] = {"status": "missing", "path": str(PROTOCOL)}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    text = PROTOCOL.read_text(encoding="utf-8")
    m = re.search(r"Version:\s*\*\*([0-9][0-9.\-]+)\*\*", text)
    version = m.group(1) if m else None
    report["protocol"] = {
        "status": "ok" if version else "version_unreadable",
        "path": str(PROTOCOL),
        "version": version,
        "sha256": sha256(PROTOCOL),
        "lines": text.count("\n") + 1,
        "differs_from_skill": bool(version and version != SKILL_WRITTEN_FOR),
    }
    tmpl: dict = {"path": str(TEMPLATE), "exists": TEMPLATE.is_file()}
    if TEMPLATE.is_file():
        try:
            tmpl["protocol_version"] = json.loads(TEMPLATE.read_text(encoding="utf-8")).get("protocol_version")
            tmpl["matches_protocol"] = tmpl["protocol_version"] == version
        except json.JSONDecodeError as e:
            tmpl["error"] = f"invalid JSON: {e}"
    report["run_record_template"] = tmpl

    # 2. Project and its own adapter / rules.
    start = Path(args.project).expanduser().resolve()
    root = git_root(start) or start
    report["project"] = {
        "root": str(root),
        "is_git": git_root(start) is not None,
        "is_home_or_toolbox": root in (HOME, HOME / ".claude") or str(root).startswith(str(HOME / ".claude")),
        "rules_files": [str(root / n) for n in ("CLAUDE.md", "AGENTS.md", "ANTIGRAVITY.md") if (root / n).is_file()],
        "adapters": sorted(
            glob.glob(str(root / ".project_docs/protocols/*video*"))
        ),
        "research_index": str(root / ".project_docs/research/README.md")
        if (root / ".project_docs/research/README.md").is_file() else None,
        "existing_targets": sorted(
            p.name for p in (root / ".project_docs/research/targets").glob("*") if p.is_dir()
        ) if (root / ".project_docs/research/targets").is_dir() else [],
    }

    # 3. Machine-local settings (paths live outside the skill; see config.example.json).
    cfg = recon_config.load()
    cfg_path = recon_config.config_path()
    local: dict = {"config": str(cfg_path), "exists": cfg_path.is_file(), "values": {}, "not_configured": []}
    for key in ("ffmpeg", "ffprobe", "python_motion", "python_site_capture", "scraping_protocol",
                "reverse_engineering_dir", "third_party_dir"):
        if key in cfg:
            v = recon_config.expand(cfg[key])
            local["values"][key] = {"path": v, "exists": os.path.exists(v)}
        else:
            local["not_configured"].append(key)
    if "witness_runner" in cfg:
        wr = recon_config.expand(cfg["witness_runner"])
        local["values"]["witness_runner"] = {
            k: {"path": v, "exists": os.path.exists(v if k != "guard_script" else os.path.join(wr.get("project", ""), v))}
            for k, v in wr.items()
        }
    else:
        local["not_configured"].append("witness_runner")
    local["examples"] = recon_config.expand(cfg.get("examples", {}))
    local["notes"] = sorted(str(p) for p in cfg_path.parent.glob("*.md")) if cfg_path.is_file() else []
    report["local"] = local

    venv_globs = recon_config.expand(cfg.get("yt_dlp_venv_globs", ["~/.venvs/*"]))
    yt_dlp_libs: list[str] = []
    for g in venv_globs:
        yt_dlp_libs += glob.glob(os.path.join(g, "lib/python*/site-packages/yt_dlp"))

    # 4. Tools on disk (existence only — never installs anything).
    report["tools"] = {
        "ffmpeg": which_any(["ffmpeg"], [recon_config.tool("ffmpeg") or ""]),
        "ffprobe": which_any(["ffprobe"], [recon_config.tool("ffprobe") or ""]),
        "yt_dlp_cli": which_any(["yt-dlp"], []),
        # Library copies inside existing project venvs. Usable as
        # `<venv>/bin/python -m yt_dlp` — that runs an existing install, it is not a new one.
        "yt_dlp_in_venvs": sorted(str(Path(p).parents[3] / "bin/python") for p in yt_dlp_libs),
        "whisper_cpp": which_any(["whisper-cli"], recon_config.expand(cfg.get("whisper_cli_globs", []))),
        "ollama": which_any(["ollama"], []),
        "deno_js_runtime": which_any(["deno"], []),
    }

    # 5. Memory gate — every model load goes through it (global §3.14).
    report["memguard"] = {
        "package": str(MEMGUARD) if MEMGUARD.is_dir() else None,
        "preflight_cli": shutil.which("memguard-preflight"),
        "readme": str(MEMGUARD / "README.md") if (MEMGUARD / "README.md").is_file() else None,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
