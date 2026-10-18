# Witnesses through a guarded runner — ASR, OCR, VLM

Read this at Step 4 (capability check), before planning ASR / OCR / VLM. It is the contract; how it
is met on this machine is in the local notes the preflight lists (`local.notes`, usually
`~/.claude/recon-local/witness-runner.md`) and in the `witness_runner` config key. No configured
runner ⇒ the model channels are `partial_scope`, never improvised.

## What a guarded runner must do

A model load can freeze the machine (two large models at once, swap). So witnesses never load a model
from the skill's own process. A guarded runner:

1. runs memguard's standalone preflight (what the daemon and `ollama ps` hold, machine headroom, swap);
2. takes a lease in the machine-wide memguard ledger for the stage's budget;
3. starts the witness worker in the stage's fixed runtime and watches memory/power/swap while it runs;
4. releases the lease only after the worker and its children are confirmed dead;
5. writes one JSON report (lease, preflight, safety samples, exit, output sha256) per call.

| Stage | Witness | Typical budget |
|---|---|---|
| `asr_w1` | ASR with forced alignment (segments + words) | ~12 GiB |
| `asr_w2` | a second, independent ASR (e.g. whisper.cpp) | ~4 GiB |
| `ocr` | per-frame OCR with boxes and confidence | ~1 GiB |
| `vlm` | a vision-language model over chronological frame batches | the largest; one at a time |

Use the models the project already approved and measured; the skill does not choose others.

## Rules that held on real runs

- **Outputs and inputs live under the target.** Point the runner's output root at the **target
  root**, not at `runs/<date>/`: workers can be stricter than the guard and refuse inputs (audio,
  frames) outside the output root ("witness input outside run output"). With the target root every
  artifact stays in the target and nothing lands in the runner's own state.
- **Read the runner's request validation before a run** — budgets, runtimes and fields change; the
  code is the source of truth, not these notes.
- **Manifests** (one per stage, inside the run folder): ASR `{audio_path, model paths}` with a WAV;
  OCR `{frames: [{path, t, sha256}], model_params}`; VLM `{batches: [{id, frames: [{path, t, sha256}]}],
  model_path}`. A finished target's `*-manifest.json` + `*-guard.json` are the best examples
  (`examples.witness_manifests` in the config).
- A timeout per call (≤ 1 h) and stages one after another: the VLM is one huge model.
- A whole-pipeline engine API writes into its own state. Check where a CLI really writes before
  using it (protocol "Safety and use of existing tools").
- Downloads run an existing `yt_dlp` install and write only into the target; browser cookies only
  after the user approves, never on the skill's initiative.

## Limits to know before promising

- **A VLM prompt written for UI demos is the wrong question for a tutorial or an edit.** Check the
  worker's prompt. Record a mismatch as a limit, or describe frames by review instead; changing the
  prompt is a change to the runner (its own task), not a skill side effect.

## Capture without models

`ffmpeg` / `ffprobe` (config keys `ffmpeg`, `ffprobe`, else PATH) cover the 1fps sequence,
quintuples, action clips and WAV extraction. Record actual PTS and hashes; keep outputs in the target.

## Lessons from real runs (2026-10-13)

- **whisper.cpp large-v3 loops on music.** It repeated a sentence 9–22 times at zero duration in
  music-heavy windows and once invented a sentence. Refuse the timing (`partial`), keep the text,
  resolve each window against W1, source captions and the screen, and record it in a
  `DISCREPANCY-REVIEW.md`. `whisper-cli -nt` disables timing altogether.
- **Contact sheets with `tile`:** `-frames:v N` limits *output* tiles, not input frames, and
  `-update 1` keeps only the last tile. One sheet of frames s..e:
  `-start_number s+1 -i f-%05d.png -vf "trim=end_frame=e-s+1,…,tile=CxR" -frames:v 1`.
- **Actual PTS:** `select='isnan(prev_selected_t)+gte(t-prev_selected_t,1)',showinfo` with
  `-fps_mode passthrough` gives real timestamps for each kept frame (parse `pts_time`).
- **A UI-prompt VLM still helps on a UI demo,** but it misread tool actions as user actions twice;
  log every rejected interpretation in the RUN-RECORD.
