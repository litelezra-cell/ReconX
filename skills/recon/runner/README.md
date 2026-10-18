# Witness runner

A small, config-driven runner for the four model "witness" channels used by ReconX: OCR, two independent
ASR passes (W1 with forced alignment, W2 whisper.cpp) and a vision-language model (VLM). Its job is to
run them **safely on your own machine**: a model load can freeze a laptop, so no model is ever loaded
by the skill's own process.

The runner has no dependencies of its own beyond the Python standard library and the repository's
`toolbox/memguard`. Each stage's model library (`rapidocr`, `mlx-audio`, `mlx-vlm`, `whisper.cpp`) lives in
the interpreter you configure for that stage and is imported only when that stage runs.

## Safety model

- **Network denied.** The worker installs an audit hook that raises on any socket connect or DNS lookup,
  and sets the Hugging Face offline variables. Nothing is downloaded at run time.
- **Memory gate.** Before anything loads, the guard runs memguard's standalone preflight (daemon and
  `ollama ps` models, machine headroom, swap), then takes a lease for the stage budget in the machine-wide
  memguard ledger. The worker refuses to start without that lease being present in the ledger.
- **Watchdog.** While the worker runs, the guard samples swap, kernel memory-pressure level, power source
  and machine headroom every 250 ms and kills the worker's process group when the machine is unsafe or
  the timeout (at most 1 h) expires.
- **Lease released only after death.** The lease is released only once the worker and every child in its
  process group are confirmed dead. If that cannot be confirmed, the guard keeps the lease.
- **One model per process, one stage at a time.** Run stages sequentially; the VLM is one large model.
- **Path checks.** Manifest, outputs and every input file (audio, frames) must resolve (symlinks
  included) to files under the target root. The guard checks first, the worker checks again.
- **Models only from config.** A manifest may name a model path, but only the one listed in the config;
  anything else is refused. Frame hashes in the manifest are verified against the files.
- **Atomic writes.** Outputs and the guard report are written to a temp file and renamed; progress is
  saved as `partial: true` after each frame/chunk/batch and finalised with `partial: false`.

## Configuration

Copy `runner.example.json` to `runner.json` (or point `RECON_RUNNER_CONFIG` / `--config` at a file).

| Key (per stage) | Meaning |
|---|---|
| `enabled` | Disabled or missing stages are refused. |
| `python` | Interpreter that has this stage's libraries installed. |
| `budget_bytes` | Memory reserved in the ledger. Defaults: OCR 1 GiB, W1 12 GiB, W2 4 GiB, VLM 45 GiB. A request may not override it. |
| `models` | The only model paths the stage may load. OCR: RapidOCR keys (`Det.model_path`, ...; empty = library defaults). W1: `asr_model_path`, `aligner_model_path`. W2: `binary`, `model_path`. VLM: `model_path`. |
| `params` | Stage parameters (see below). |

Top-level `memguard_path` (optional) points at a directory containing the `memguard` package; otherwise
the repository's `toolbox/memguard` is used.

Stage parameters: W1 `chunk_seconds` (280), `max_tokens` (4096), `language`; W2 `language` and
`extra_flags` (never `-nt`: timing is evidence; whisper.cpp large-v3 can loop on music, so the worker
flags zero-length, backward and out-of-range segments as `timing_issues` and marks the result partial);
VLM `max_tokens`, `temperature` (0), `repetition_penalty`, `enable_thinking`, `prompt` (with a
`{timestamps}` placeholder). `fps` and `frames_per_batch` in the VLM params are hints for whoever builds
the manifest (the calibrated window is 16 frames at 2 fps); the worker takes batches from the manifest.

The default VLM prompt is written for UI demonstrations. For a tutorial or an edit, set a different
`prompt` and record that choice in the run record.

## Request and manifests

```
python witness_guard.py --request request.json [--config runner.json] [--out-dir TARGET_ROOT]
```

`--out-dir` defaults to `RECON_OUT_DIR`. Use the **target root**, not the dated run folder, so audio and
frames can live in the same target as the outputs.

```json
{"stage": "ocr", "manifest_path": "<root>/runs/r1/v-ocr-manifest.json",
 "output_path": "<root>/runs/r1/v-ocr-raw.json", "timeout": 3600}
```

Optional: `guard_path` (default: `output_path` with `-raw.json` replaced by `-guard.json`), `parent_pid`
(the guard stops the worker if that process disappears; default: the guard's parent).

Manifests (model paths optional, because the config supplies them):

- `ocr`: `{"frames": [{"path", "t", "sha256"}]}`
- `asr_w1`, `asr_w2`: `{"audio_path": "<16 kHz mono WAV>"}`
- `vlm`: `{"batches": [{"id", "frames": [{"path", "t", "sha256"}]}]}`

## Outputs

`<id>-<stage>-raw.json` holds the witness result (OCR text items with boxes and confidence; W1 segments and
aligned words; W2 segments with offsets and the exact command; VLM text per batch with token counts and
finish reason). `<id>-<stage>-guard.json` (also printed to stdout) holds `status`
(`ok` / `refused` / `failed`), `reserved_bytes`, `lease_id`, `lease_released`, `worker_stopped`, the
`preflight` report, `initial_safety`, `last_safety`, `safety_samples`, `worker_exit_code`,
`worker_stderr_tail` and, on success, the output's `sha256`. Exit code is 0 only for `ok`.

## How the skill calls it

The skill's config key `witness_runner` points at `witness_guard.py` and the runner config. For each stage
in turn it writes the manifest and request inside the target, runs the guard with `--out-dir <target
root>`, and reads the guard JSON before the raw output. `refused` or `failed` means that channel is
`partial_scope`. A stage must never be retried by loading the model some other way.

## Testing

```
cd skills/recon/runner && python3 -m unittest discover -s tests
```

The unit tests need no models and no network (stdlib `unittest`; they use a temporary
`MEMGUARD_STATE_DIR` and fake brokers). They cover path validation, the unlisted-model refusal, refused
reservations, failing workers, watchdog stop, unconfirmed worker death and network denial.
For a live check, configure only the `ocr` stage and run it on a few frames in a scratch target.
