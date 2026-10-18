#!/usr/bin/env python3
"""Create the research-target folder skeleton for one video-analysis target.

Layout follows the "one folder per research target, with a handoff document" convention;
a finished example is `examples.finished_target` in the Recon config.

Never overwrites: an existing file is left untouched and listed as "kept".
Re-running on an existing target only adds what is missing.
Media and frames stay out of git through a .gitignore inside the target folder.

Prints a JSON summary. --dry-run prints the plan and writes nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recon_config  # noqa: E402

TOOLBOX = Path.home() / ".claude/toolbox/video-analysis"
PROTOCOL = TOOLBOX / "README.md"
TEMPLATE = TOOLBOX / "RUN-RECORD.template.json"

GITIGNORE = """\
# recon skill: media, frames and audio are local evidence, never committed.
# JSON / Markdown / text (manifests, hashes, ASR raw, reports) stay tracked.
*.mp4
*.mov
*.mkv
*.webm
*.m4v
*.avi
*.wav
*.mp3
*.m4a
*.aac
*.flac
*.opus
*.png
*.jpg
*.jpeg
*.webp
*.gif
*.heic
*.tif
*.tiff
*.npz
*.npy
# network logs may hold cookies the site set
*.har
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protocol_version() -> str | None:
    m = re.search(r"Version:\s*\*\*([0-9][0-9.\-]+)\*\*", PROTOCOL.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def build(args: argparse.Namespace) -> dict:
    root = Path(args.project).expanduser().resolve()
    tid = args.target_id
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", tid):
        raise SystemExit(f"target-id must be lowercase kebab-case, got {tid!r}")
    if not PROTOCOL.is_file():
        raise SystemExit(f"protocol missing: {PROTOCOL}")

    date = args.date or dt.date.today().isoformat()
    version = protocol_version()
    psha = sha256(PROTOCOL)
    research = root / ".project_docs/research"
    tdir = research / "targets" / tid
    run_dir = tdir / "runs" / date
    abs_t = str(tdir)

    sources = []
    for s in args.source:
        m = re.fullmatch(r"([a-z][a-z _-]{0,30})=(.+)", s)  # optional "role=URL"
        role, url = (m.group(1), m.group(2)) if m else ("source", s)
        sources.append({"role": role, "url": url})
    src_lines = "\n".join(f"| {s['url']} | `evidence/` ⇒ `runs/{date}/` |" for s in sources) or "| (none yet) | |"

    target_json = {
        "target_id": tid,
        "name": args.name,
        "kind": args.kind,
        "sources": sources,
        "requested_by": args.requested_by,
        "requested_at": date,
        "project": root.name,
        "status": "intake",
        "protocol_version": version,
        "protocol_sha256": psha,
        "run_record": f"runs/{date}/RUN-RECORD.json",
        "report": "deliverables/REPORT.md",
        "research_model_requested": (recon_config.get("research_model") or {}).get("value") if isinstance(recon_config.get("research_model"), dict) else recon_config.get("research_model"),
        "research_model_effective": None,
        "created_by": "recon skill",
    }

    readme = f"""# {args.name} — folder map

**Root:** `{abs_t}/`
**Start with:** [HANDOFF.md](HANDOFF.md)
**Status:** intake — the folder was created; capture has not started.

```
{tid}/
├── README.md          <- you are here: the map
├── TARGET.json        target identity: id · name · URLs · requested by · when · status
├── CONTEXT.md         the request verbatim + the intake answers (stage 0)
├── HANDOFF.md         * the handoff document: goal · decisions · state · gaps · next step
├── evidence/
│   ├── site/          site screenshots + INDEX.md (file => URL => date). Images are not in git
│   ├── media/         the source video. Not in git; its SHA256 is recorded in the RUN-RECORD
│   └── frames/        >=1fps sequence, quintuples, action clips, region crops. Not in git
├── runs/{date}/   RUN-RECORD.json (from the template), raw ASR/OCR/VLM outputs. JSON and text in git, media not
└── deliverables/      REPORT.md (and the control map on a UI track)
```

## Source => file
| Source | Where it goes |
|---|---|
{src_lines}

## Files outside this folder that the analysis relies on
| File | Why |
|---|---|
| `{PROTOCOL}` | the binding protocol (`{version}`, sha256 `{psha[:12]}…`) |
| `{TEMPLATE}` | the run-record format |
"""

    context = f"""# {args.name} — context and intent

**Request ({date}), verbatim:**
> (fill in: the exact quote from the chat)

## Round 1 — opening questions (stage 0a of protocol `{version}`)
| Question | Answer, verbatim | Source |
|---|---|---|
| What is it, and why? | | asked / from the request |
| What else can be extracted from it? | | |
| Preliminary first, or straight to deep? | | |
| Anything specific to watch for? | | |

## Preliminary analysis and research (0b)
Findings: `runs/<date>/preliminary-findings.md` (to fill in)

## Round 2 — questions born from the findings (0c)
| Question | Answer, verbatim |
|---|---|
| | |

**Tracks derived from the answers:** (to fill in)

## Context in the project
(Existing project decisions the analysis must serve; do not reopen a closed decision.)
"""

    handoff = f"""# HANDOFF — {args.name}

> Written {date}. All paths are absolute. **Root:** `{abs_t}/`

## 1. Goal
(Fill in from the intake: what to understand and why.)

## 2. Original request (verbatim)
> (fill in)

**Sources:**
{chr(10).join(f"- {s['role']}: {s['url']}" for s in sources) or "- (none yet)"}

## 3. Decisions already made
- Protocol: `{PROTOCOL}` version `{version}`.
- Research agents: the research model from the local config (`research_model`; default one tier below the main model). Every "tool X does Y" claim from a research report is checked against the primary source before it enters a document.
- No installs on the skill's own initiative; model loads only through memguard.

## 4. Actual state
| What | State |
|---|---|
| Target folder | created ({date}) |
| Intake | (to fill in) |
| Capture | not started |

## 5. What was checked / not checked
(Fill in after the run.)

## 6. Gaps and dependencies
(Missing tools, blocked channels, what is waiting on the requester.)

## 7. Next step
(Fill in.)

## 8. Handoff gate
`complete_scope` / `partial_scope` / `blocked` — (fill in, with the reason.)
"""

    run_record = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    template_version = run_record.get("protocol_version")
    run_record["protocol_version"] = version
    run_record["source"]["url"] = sources[0]["url"] if sources else None
    run_record["template_notice"] = (
        f"Initialized by the recon skill on {date} from RUN-RECORD.template.json "
        f"(template version {template_version}). Not run evidence until channels are filled."
    )

    files = {
        tdir / "TARGET.json": json.dumps(target_json, ensure_ascii=False, indent=2) + "\n",
        tdir / "README.md": readme,
        tdir / "CONTEXT.md": context,
        tdir / "HANDOFF.md": handoff,
        tdir / ".gitignore": GITIGNORE,
        run_dir / "RUN-RECORD.json": json.dumps(run_record, ensure_ascii=False, indent=2) + "\n",
    }
    keep_dirs = [tdir / "evidence/site", tdir / "evidence/media", tdir / "evidence/frames", run_dir, tdir / "deliverables"]

    index = research / "README.md"
    index_row = f"| **{args.name}** | video analysis | {date} | intake | `targets/{tid}/HANDOFF.md` |\n"

    result = {
        "target_dir": abs_t,
        "protocol_version": version,
        "template_version": template_version,
        "created": [], "kept": [], "index": None, "dry_run": args.dry_run,
    }

    for d in keep_dirs:
        gk = d / ".gitkeep"
        if gk.exists():
            result["kept"].append(str(gk))
        else:
            result["created"].append(str(gk))
            if not args.dry_run:
                d.mkdir(parents=True, exist_ok=True)
                gk.touch()

    for path, content in files.items():
        if path.exists():
            result["kept"].append(str(path))
            continue
        result["created"].append(str(path))
        if not args.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    if index.exists():
        text = index.read_text(encoding="utf-8")
        if f"targets/{tid}/" in text:
            result["index"] = "already listed"
        else:
            result["index"] = f"row appended to {index}"
            if not args.dry_run:
                index.write_text(text.rstrip("\n") + "\n" + index_row, encoding="utf-8")
    else:
        result["index"] = f"created {index}"
        if not args.dry_run:
            index.parent.mkdir(parents=True, exist_ok=True)
            index.write_text(
                f"# Research index — {root.name}\n\n"
                "| Target / document | What | Date | Status | Entry |\n|---|---|---|---|---|\n" + index_row,
                encoding="utf-8",
            )
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="project root (git toplevel)")
    ap.add_argument("--target-id", required=True, help="stable kebab-case id, e.g. text-flow")
    ap.add_argument("--name", required=True, help="human name of the target")
    ap.add_argument("--kind", default=None, help="short description: product demo, tutorial, reel…")
    ap.add_argument("--source", action="append", default=[],
                    help="URL or local path; optionally role=URL (repeatable)")
    ap.add_argument("--requested-by", default=recon_config.get("requested_by", "the user"))
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(json.dumps(build(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
