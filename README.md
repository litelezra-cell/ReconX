# ReconX

ReconX is a [Claude Code](https://claude.com/claude-code) skill, together with a written protocol, for analysing videos — tutorials, UI and plugin demos, screen recordings and edits — into evidence-backed reports and control maps.

The protocol is the source of truth. The skill is a runner: it reads the protocol every time, asks the intake questions, builds a research folder for the target, checks which tools actually exist, runs a cheap preliminary pass, comes back with findings and follow-up questions, and then performs the deep analysis the content track requires. It ends at a handoff gate that reports `complete_scope`, `partial_scope` or `blocked`.

## What is inside

```
skills/recon/
  SKILL.md                    the skill (runner for the protocol)
  references/
    artifact-route.md         when the product itself is available: from "what it does" to "how"
    witness-runner.md         contract for a guarded runner of ASR / OCR / VLM witnesses
  scripts/
    preflight.py              read-only check of protocol, project, tools and local config
    scaffold_target.py        creates the per-target research folder (never overwrites)
    gate_check.py             mechanical handoff gate for a RUN-RECORD
    motion_profile.py         per-frame element tracking for motion windows
    ramp_speed_profile.py     optical-flow speed profile for speed ramps / time remaps
    sharp_shot.py             picks a sharp, stable frame for named screenshots
    site_capture.py           captures official pages (HTML, text, screenshots, media list)
    recon_config.py           loads the machine-local config
    config.example.json       template for the local config
toolbox/video-analysis/
  README.md                   the protocol
  CHANGELOG.md                protocol history
  RUN-RECORD.template.json    portable run-record format
  SOURCE.json                 provenance of the protocol versions
scripts/
  check-public.sh             release gate: fails if a tracked file contains a deny-listed string
```

## Requirements

- Claude Code.
- `ffmpeg` and `ffprobe`.
- Python 3 with `numpy`, `opencv-python` and `Pillow` for the motion and screenshot scripts.
- Playwright (with a Chromium build) for `site_capture.py`.
- Optional: local ASR, OCR and VLM models, run through your own guarded runner (see `skills/recon/references/witness-runner.md`). The skill never installs anything and never downloads model weights; a channel whose tool is missing is reported as `partial_scope`.

## Installation

1. Copy `skills/recon` to `~/.claude/skills/recon`.
2. Copy `toolbox/video-analysis` to `~/.claude/toolbox/video-analysis`.
3. Copy `skills/recon/scripts/config.example.json` to `~/.claude/recon-local/config.json` (or point `$RECON_CONFIG` at your own file) and set the paths for your machine. Every key is optional; `preflight.py` reports missing keys as not configured.
4. In a Claude Code session, run `/recon <link or path>`, or ask Claude to analyse a video.

## Key principles

- The protocol is the source of truth; the skill points at its sections and does not copy them.
- Two independent transcripts (ASR engines) when speech is relevant, compared on a timeline.
- Numbers come only from OCR, never from a vision model.
- Motion and creation are measured on every source frame; sampled images are navigation only.
- Named screenshots are checked by eye: a file name is a claim about what the image shows.
- Control maps label every control as `demonstrated`, `visible`, `inferred` or `unknown`, and list what was not spoken about.
- A mechanical handoff gate (`gate_check.py`) refuses `complete_scope` when required evidence is missing.
- No installs and no bot-detection bypass: a blocked source is recorded as blocked.

## Status

Maintained as an English public edition of a private working copy. Protocol version: 2026-10-14.1.

## Naming

The repository is **ReconX**; the skill inside it is `recon`, invoked in Claude Code as `/recon`.

## License

MIT — see [LICENSE](LICENSE). The repository contains only original code and documentation. External tools it calls (ffmpeg, OpenCV, NumPy, Pillow, Playwright, and any models you run through your own runner) are not bundled and remain under their own licenses.
