# Setup — tools, models and what we measured

This page is for the person (or the agent) installing ReconX on a new machine. It says **which tool or
model serves each channel, how to get it, how much memory it needs, and why it was chosen** — with the
measurements behind each choice, so you can adapt to your hardware without repeating the work.

Your agent should read this page, check your machine (`python3 skills/recon/scripts/preflight.py`
reports what is installed), pick the matching tier below, and propose the downloads to you. **Nothing
here is downloaded automatically.** Model choices are yours; the tables say what we measured, not what
you must use.

> Reference machine for all measurements: Apple Silicon (M-series Max), 128 GB unified memory, macOS.
> Measurements were made on real tutorials and UI demos (15–20 minute screen recordings with speech
> and music), not on benchmark datasets. Leaderboard leaders often failed on this material — see the
> lessons at the end.

---

## 1. Channels at a glance

| Channel | What it is for | Our choice | Licence | Memory (approx.) |
|---|---|---|---|---|
| Acquisition | download the video, captions and metadata | `yt-dlp` (no cookies, no bot-detection bypass) | Unlicense | — |
| Frames / audio | decode, extract frames and WAV | `ffmpeg` / `ffprobe` | LGPL/GPL (external binary) | — |
| ASR W1 (timing witness) | transcript with word times | **Qwen3-ASR-1.7B** (`mlx-community/Qwen3-ASR-1.7B-bf16`) + **Qwen3-ForcedAligner-0.6B** (`Qwen/Qwen3-ForcedAligner-0.6B`) via `mlx-audio` | Apache-2.0 | ~4 GB weights, budget 12 GiB |
| ASR W2 (independent witness) | second transcript from a different model family | **whisper.cpp** with `ggml-large-v3.bin` (full large-v3, not turbo) | MIT (whisper.cpp) · model as published by OpenAI | 3.1 GB weights, budget 4 GiB |
| VAD (recommended for W2) | cut long audio into speech chunks before whisper | Silero VAD v5 (`ggml-silero-v5.1.2.bin` for whisper.cpp) | MIT | tiny |
| OCR (authority for numbers and labels) | read every UI label and value, every second | **RapidOCR** with PP-OCRv6 (det/rec small, ONNX, CPU) | Apache-2.0 | ~1 GiB |
| VLM (actions and results, never numbers) | describe what changed in short windows | **Qwen3.6-35B-A3B 8-bit** (`mlx-community/Qwen3.6-35B-A3B-8bit`) via `mlx-vlm` | Apache-2.0 | 35 GB weights, budget 45 GiB |
| Motion | every-frame motion and speed profiles | `motion_profile.py`, `ramp_speed_profile.py` (OpenCV, NumPy) | Apache-2.0 / BSD | small |
| Site capture | official product pages | `site_capture.py` (Playwright + Chromium) | Apache-2.0 | small |
| Memory gate | one model load at a time, machine-wide | `toolbox/memguard` (this repo) | MIT | — |
| Witness runner | runs the four model channels safely | `skills/recon/runner` (this repo) | MIT | — |

Every model load goes through the memory gate. The runner denies network access to model processes,
so weights must be on disk before a run.

---

## 2. Pick a tier for your machine

| Tier | Machine | ASR W1 | ASR W2 | OCR | VLM | What you lose |
|---|---|---|---|---|---|---|
| **A** | Apple Silicon, ≥ 96 GB | Qwen3-ASR-1.7B + aligner | whisper.cpp large-v3 + VAD | RapidOCR | Qwen3.6-35B-A3B 8-bit | nothing — this is the measured stack |
| **B** | Apple Silicon, 32–64 GB | same | same | same | **Qwen3-VL-8B-Instruct 4-bit** (`mlx-community/Qwen3-VL-8B-Instruct-4bit`, ~6 GB) | VLM is weaker: screen-text reading 49.9 % vs 52.1 %, filtering recall drops (16 vs 10 misses on 60 windows). Numbers still come from OCR, so values are unaffected. |
| **C** | Apple Silicon, 16 GB | same (fits) | whisper.cpp **large-v3-turbo** (1.6 GB) | same | none — the agent reads selected frames itself | no independent VLM witness; mark the VLM channel `partial_scope` |
| **D** | Linux / Windows / Intel Mac | an equivalent ASR through `transformers` or CTranslate2 (not measured by us) | whisper.cpp (cross-platform) | RapidOCR (cross-platform) | any local VLM your hardware runs, calibrated first (§4) | MLX is Apple-only; every substitute must be measured on one of your own videos before you trust it |

Without any local model, the skill still runs: source captions, OCR and the agent's own reading of
frames carry the analysis, and the report says which witnesses were missing.

---

## 3. Install (tier A; adapt names for B–D)

```bash
# 1. the skill, the protocol and the memory gate
cp -R skills/recon ~/.claude/skills/recon
mkdir -p ~/.claude/toolbox && cp -R toolbox/video-analysis toolbox/memguard ~/.claude/toolbox/
pip install -e ~/.claude/toolbox/memguard

# 2. local config (paths to your tools and models)
mkdir -p ~/.claude/recon-local
cp skills/recon/scripts/config.example.json ~/.claude/recon-local/config.json
cp ~/.claude/skills/recon/runner/runner.example.json ~/.claude/skills/recon/runner/runner.json   # then edit model paths
# (or keep it elsewhere and set RECON_RUNNER_CONFIG=/path/to/runner.json)

# 3. system tools
brew install ffmpeg yt-dlp

# 4. Python environments (one per family keeps dependency pins apart)
python3 -m venv ~/.venvs/recon-core && ~/.venvs/recon-core/bin/pip install numpy opencv-python Pillow rapidocr onnxruntime
python3 -m venv ~/.venvs/recon-mlx  && ~/.venvs/recon-mlx/bin/pip install mlx-audio mlx-vlm
python3 -m venv ~/.venvs/recon-web  && ~/.venvs/recon-web/bin/pip install playwright && ~/.venvs/recon-web/bin/playwright install chromium

# 5. whisper.cpp (build with Metal) and its models
git clone https://github.com/ggml-org/whisper.cpp && cd whisper.cpp && cmake -B build && cmake --build build -j
sh ./models/download-ggml-model.sh large-v3          # 3.1 GB
# VAD model for long audio: see whisper.cpp's models/ README (silero v5.1.2)

# 6. model weights (check what you already have first; downloads are large)
huggingface-cli download mlx-community/Qwen3-ASR-1.7B-bf16
huggingface-cli download Qwen/Qwen3-ForcedAligner-0.6B
huggingface-cli download mlx-community/Qwen3.6-35B-A3B-8bit                   # ~35 GB, tier A only

# 7. check
python3 ~/.claude/skills/recon/scripts/preflight.py --project "$PWD"
```

RapidOCR downloads nothing at run time: its PP-OCRv6 ONNX files ship inside the pip package; point the
runner config at them.

---

## 4. What we measured, and why these choices

### ASR — two independent witnesses (W1 + W2)
- **Method.** Three items: an 18.5-minute tutorial (no captions), a long tutorial with captions, a short
  clip. Ground truth: the creator's captions, and a leave-one-out majority across all models (the
  same reference set for every model). Gates a model had to pass to replace the incumbent: lower WER
  on the long item against both references, on-screen term recall at least equal, finish both long
  items, real-time factor ≥ 4 on the Mac, timestamps (native or through an aligner).
- **Results (long item, WER vs captions / vs majority):** Qwen3-ASR-1.7B **4.04 % / 4.47 %**;
  whisper large-v3 8.36 % / 4.58 %; Granite-Speech-3.3-8B 5.22 % / 3.76 % but RTFx ≈ 1.3 and no
  timestamps; Canary-Qwen-2.5B chunked to ≤ 40 s 4.94 % / 4.85 % (unchunked it degenerates: 99.95 %);
  Canary-1B-v2 6.70 % / 5.62 % and slow on long audio; Parakeet-TDT-0.6B-v3 16.5 % but very fast.
  Qwen3-ASR runs at RTFx 35–41; the forced aligner adds word times (~1 s per short item).
- **Decision.** W1 = Qwen3-ASR + aligner (most accurate and fast, with timing). W2 = whisper
  large-v3: a different model family, so its errors are independent of W1's. Source captions, when
  present, are a third witness.
- **Known failure modes, by witness:** captions drop a leading digit or a decimal; Qwen3-ASR drops
  decimal points ("1.3" → "13"); whisper can drop a technical term and **loops on long audio with
  music** (a sentence repeated 9–22 times at zero duration, which fails timestamp validation). Use VAD
  chunking before whisper; treat a looping W2 as text-only and say so in the report.
- **Model-specific lesson:** a model with a stated training window (e.g. 40 s) must be measured on
  audio chunked to that window; "it degenerates" is only a verdict after the official runtime and
  chunking were tried. Stereo input crashed one runtime — check the input before blaming the model.

### OCR — the authority for labels and numbers
- On 20 frozen 4K stills with hand-checked ground truth (461 items), RapidOCR PP-OCRv6 read **85.9 %**;
  vision-language models read ≤ 45–52 % of the same items. Hence: **every number in a report comes from
  OCR**, never from a VLM.
- Run it every second at source resolution. Read per-second raw OCR before calling a contradiction: a
  button can change its label after a click (e.g. "Match" becomes "Refresh").
- **Gap:** RapidOCR does not read Hebrew or Arabic. Check right-to-left text by eye.

### VLM — calibrate the window length before a long run
- Calibrated on two segments with frame-by-frame ground truth, Qwen3.6-35B-A3B 8-bit, 2 fps, 1280×720,
  a fixed UI prompt, temperature 0:

  | Window | Result |
  |---|---|
  | 2 s | correct but too little context |
  | 4 s | invented a setting change that did not happen |
  | **8 s** | **correct actions and results, no invented values** (both segments) |
  | 16 s | starts to invent ("changed X to a value it already had") |
  | 24 s | invents values and reverses cause and effect |

  → 8-second windows. Over a whole tutorial the VLM still invented numbers in every video (3–52
  conflicts with OCR per video), which is why it is a navigation witness only.
- On a 4K screen-text test the 35B model read 52.1 % vs 49.9 % for the 8B, and in a 60-window filtering
  test it reached 80 % accuracy (precision 0.91, recall 0.67) vs clearly more misses for the 8B.
- It mistakes the screen recording's own zoom/pan for user actions; check motion claims in the frames.

### Motion — measured on every source frame
- `motion_profile.py` tracks elements frame by frame; `ramp_speed_profile.py` measures apparent footage
  speed (dense optical flow) and flags ramps. On a screen-recorded tutorial the whole-frame profile
  mostly measures the narrator, the recording's zoom/pan and viewer playback; use it on full-screen
  result segments or with `--crop` on the viewer, and open every flagged event at source rate before it
  counts.

### Screenshots and witnesses at scale
- Across 17 tutorials (≈ 3.4 hours) the pipeline produced two ASR witnesses, per-second OCR and 8-second
  VLM windows for every video, plus 552 named screenshots; each name was checked against the image and
  about one in twenty (25 of 552) was wrong and renamed. **A screenshot name is a claim; open it.**

---

## 5. Lessons that cost the most

1. A leaderboard is not your material: the top public ASR models failed on 15–20-minute tutorials
   until they were run the way their model cards require (chunking, mono input, official runtime).
2. Measure the production path, not a convenient script: a model that took 2.5 s per item in a test
   script took ~3 minutes per item through the real pipeline (larger frames, more of them).
3. Two huge models loaded at once froze a 128 GB machine. Hence the machine-wide memory gate: every
   load reserves first, swap means refuse, large models are released after idle.
4. "Exit 0" is not a result: read the output. A looping transcript and an empty model reply both
   exited cleanly.
5. Models change every week. The skill offers a tools scan when the last one is older than a week
   (`skills/recon/references/tools-scan.md`), and adopts nothing without a measurement and your yes.
