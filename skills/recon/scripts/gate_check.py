#!/usr/bin/env python3
"""Mechanical handoff gate for a video-analysis RUN-RECORD. Exit 0 = the claimed scope status holds;
exit 1 = it does not (reasons printed). Run it before writing complete_scope anywhere.

Iron rule (protocol 2026-10-13.2; UI screenshots/windows and VLM calibration since 2026-10-14.1): when there is any motion evidence, the motion_source_rate channel
must be done, with tracks and with every flagged event opened in the source. A prompt instruction is
not a gate; this script is.

Motion evidence = any of: motion_cues.windows non-empty; motion words in the transcript files given
with --transcript; requirements.motion_source_rate.required true.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# The last three alternatives match Hebrew motion words (motion, animation, moved, drag).
MOTION_WORDS = re.compile(
    r"\b(animat\w*|bounc\w*|elastic|stagger\w*|zoom\w*|rotat\w*|blur\w*|fade\w*|slid\w*|drag\w*|"
    r"mov(e|es|ed|ing)|swip\w*|transition\w*|pop(s|ped)?|explod\w*|shatter\w*|spin\w*|scal(e|es|ed|ing)|"
    r"keyframe\w*|eas(e|ing)|motion|תנוע\w*|אנימצי\w*|זז\w*|גרי?ר\w*)\b", re.I)

REQUIRED_DONE = ("visual_sequence", "human_review", "artifact_audit")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_record")
    ap.add_argument("--transcript", action="append", default=[], help="text/JSON transcript files to scan for motion words")
    a = ap.parse_args()
    r = json.loads(Path(a.run_record).read_text(encoding="utf-8"))
    status = (r.get("result") or {}).get("scope_status")
    ch = r.get("channels", {})
    req = r.get("requirements", {})
    problems: list[str] = []
    warnings: list[str] = []

    hits = 0
    for f in a.transcript:
        hits += len(MOTION_WORDS.findall(Path(f).read_text(encoding="utf-8", errors="ignore")))
    cue_windows = (r.get("motion_cues") or {}).get("windows") or []
    motion_required = bool(cue_windows) or hits > 0 or bool((req.get("motion_source_rate") or {}).get("required"))

    if motion_required:
        m = ch.get("motion_source_rate") or {}
        if m.get("status") != "done":
            problems.append(f"motion evidence present (cue windows={len(cue_windows)}, transcript motion words={hits}) "
                            f"but channels.motion_source_rate.status={m.get('status')!r}")
        if not r.get("motion_tracks"):
            problems.append("motion_tracks is empty: no element trajectories (from where to where) recorded")
        unopened = [e for e in r.get("motion_tracks", []) if isinstance(e, dict) and e.get("flagged") and not e.get("opened_in_source")]
        if unopened:
            problems.append(f"{len(unopened)} flagged motion events were not opened in the source frames")

    # Questions ledger (skill Step 2.5): every required question must end answered.
    ledger = r.get("questions_ledger")
    if ledger is None:
        warnings.append("no questions_ledger in the RUN-RECORD (skill Step 2.5)")
    else:
        for q in ledger:
            if q.get("required", True) and q.get("status") != "answered":
                problems.append(f"required question not answered ({q.get('status')!r}): {q.get('question')}")

    # UI track (protocol 2026-10-14.1): named screenshots with an index, and an explicit review of windows
    # outside the main panel (protocol 2026-10-14.1). An empty windows list is allowed — a missing one is not.
    new_rules = str(r.get("protocol_version") or "") >= "2026-10-14.1"  # no retroactive change to older runs
    ui_required = new_rules and bool((req.get("control_inventory") or {}).get("required")) or bool(r.get("control_inventory", {}).get("required"))
    if ui_required:
        root = Path(a.run_record).resolve().parents[2]
        idx = (r.get("screenshots") or {}).get("index")
        if not idx or not (root / idx).is_file() or (root / idx).read_text(encoding="utf-8").count("|") < 6:
            problems.append("UI track without screenshots.index (named screenshots + INDEX.md with rows)")
        if "windows" not in r:
            problems.append("UI track without a 'windows' review (windows/dialogs outside the main panel; [] if none)")
    # VLM witness (protocol 2026-10-14.1): window length measured on ground truth before a long run.
    if new_rules and (ch.get("vlm") or {}).get("status") == "done" and not r.get("vlm_calibration"):
        problems.append("vlm channel done without vlm_calibration (window length measured against ground truth)")

    for name in REQUIRED_DONE:
        if (req.get(name) or {}).get("required") and (ch.get(name) or {}).get("status") != "done":
            problems.append(f"required channel {name} is {(ch.get(name) or {}).get('status')!r}")
    for name, rq in req.items():
        if (rq or {}).get("required") and (ch.get(name) or {}).get("status") in (None, "planned", "blocked"):
            problems.append(f"required channel {name} is {(ch.get(name) or {}).get('status')!r}")

    ok = not problems if status == "complete_scope" else True
    print(json.dumps({"claimed": status, "motion_required": motion_required, "transcript_motion_words": hits,
                      "cue_windows": len(cue_windows), "problems": sorted(set(problems)), "warnings": warnings,
                      "verdict": "pass" if ok else "fail: complete_scope not allowed"}, ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
