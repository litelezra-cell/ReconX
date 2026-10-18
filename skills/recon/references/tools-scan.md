# Tools scan — keeping the pipeline's models and tools current

Models and tools for every channel of this skill (ASR, OCR, VLM, optical flow, beats, capture) change
weekly. A choice that was best a month ago can be behind today: whisper large-v3 (2023) kept the
second-witness slot while a measured, more accurate ASR sat on the same disk. This procedure finds what
is new; **the user decides** what to measure and adopt.

## When

`preflight.py` reports `tools_scan`: the last scan date, its age and `stale` (older than
`tools_scan.max_age_days` in the local config, default 7; scans live in `tools_scan.dir`).
When `stale` is true, add one question to the Step 2 intake message (it counts toward the four):
"The last tools scan is N days old — run an update scan now (research only, no downloads)?"
with options *Yes, before the analysis* · *After the analysis* · *Not this time*. **Never run it without a yes.** A "no"
is recorded in the RUN-RECORD (`intake.answers_verbatim`) and the analysis continues with the
current tools. A scheduled weekly scan, if the user set one up, writes to the same folder and counts.

## Rules

- **Research only.** No download, install, pull, model load or switch. Model and quantisation choices
  belong to the user: bring candidates with explanations; the user picks what to measure. Before any
  later download: the machine's duplicate check; every model load goes through the memory gate.
- Research subagents on the research model the user chose (`research_model`; default: one tier below the main model — cheap, but not the smallest); record requested and
  effective model.
- Every claim (what it does, benchmark, licence, release date, Apple-Silicon/MLX/GGUF availability) is
  verified at the primary source (model card, repo, LICENSE file, release notes) before it enters the
  report. Unverified claims go to a separate list. A leaderboard is not evidence for our material.
- First read what exists: the machine inventory and the model registry (current best per task, the
  measurement behind it, rejected candidates) and the previous scan — report only what is new.

## Roles

1. ASR with word timestamps or forced alignment (both witnesses), English and Hebrew; VAD.
2. OCR of UI and screen recordings (Hebrew/Arabic capability is a known gap).
3. Local vision-language models for frames and short clips (known issue: invented numbers).
4. Optical flow, frame interpolation, motion estimation, retime tooling.
5. Beat / downbeat / onset detection (measurable against the editor GT harness when present).
6. Audio separation and audio embeddings.
7. Host applications in use (e.g. DaVinci Resolve): release notes, scripting API, relevant plugins.
8. Claude Code: skills, MCP servers, plugins useful for capture, analysis, orchestration.
9. Apple-Silicon runtimes (MLX, mlx-vlm, mlx-audio, whisper.cpp, llama.cpp, Ollama).

## Output

`<tools_scan.dir>/<YYYY-MM-DD>.md`, in the user's language: 3–5 opening lines (or "a quiet week");
a table per role (name · what's new · date · licence · runs on Apple Silicon and approx. memory · on
disk already? · which registry line it would challenge · what to measure against which GT · source
URL); a "not verified" list; at most 3 "worth measuring" candidates with the exact measurement and
download size — and the question which to measure. Adoption only after measurement, recorded in the
registry's decision log.
