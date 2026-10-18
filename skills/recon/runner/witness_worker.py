"""Witness worker: one stage, one model, one process.

Started only by ``witness_guard.py`` (which owns the memory lease). Network access is denied for the
whole process. Model paths come from the runner config and nowhere else: a manifest may repeat a
model path, but only if it is the configured one.

Stages: ``ocr`` (RapidOCR), ``asr_w1`` (Qwen3-ASR + forced aligner via mlx-audio), ``asr_w2``
(whisper.cpp CLI), ``vlm`` (mlx-vlm). Each stage imports its heavy library inside its own function.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import wave
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner_config import (  # noqa: E402
    RunnerRefused, atomic_write_json, load_config, manifest_inputs, require_inside,
    require_input_file, resolve_root, sha256_file, stage_config,
)

DEFAULT_VLM_PROMPT = (
    "Read all visible UI text first. Compare these chronological source frames; state observed "
    "control, action and visible result separately. Do not infer latency, implementation, host APIs "
    "or invisible actions. Report uncertainty. Source timestamps: {timestamps}"
)
FORBIDDEN_WHISPER_FLAGS = {"-nt", "--no-timestamps"}


def deny_network(event: str, _args: tuple[Any, ...]) -> None:
    """Audit hook: any outbound socket use raises."""
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
        raise RuntimeError("Witness workers cannot access network or download models")


def check_listed(label: str, requested: str | None, listed: str | None) -> str:
    """Return the configured model path; refuse a manifest path that is not the configured one."""
    if not listed:
        raise RunnerRefused(f"Model '{label}' is not listed in the runner config")
    configured = Path(listed).expanduser().resolve(strict=True)
    if requested is not None and Path(requested).expanduser().resolve(strict=True) != configured:
        raise RunnerRefused(f"Model path for '{label}' is not the one listed in the runner config")
    return str(configured)


def resolve_models(stage: str, data: dict[str, Any], cfg: dict[str, Any]) -> dict[str, str]:
    """Map every model/executable the stage needs to its configured path (checked against the manifest)."""
    listed = cfg.get("models", {})
    if stage == "ocr":
        requested = data.get("model_params", {}) or {}
        for key in requested:
            if key not in listed:
                raise RunnerRefused(f"Model parameter '{key}' is not listed in the runner config")
        return {key: check_listed(key, requested.get(key), path) for key, path in listed.items()}
    wanted = {"asr_w1": ["asr_model_path", "aligner_model_path"], "asr_w2": ["binary", "model_path"],
              "vlm": ["model_path"]}[stage]
    return {key: check_listed(key, data.get(key), listed.get(key)) for key in wanted}


def whisper_command(models: dict[str, str], audio: str, prefix: Path, params: dict[str, Any]) -> list[str]:
    """whisper.cpp argv. Never ask for ``-nt``: timing is evidence and the decoder must emit it."""
    extra = [str(flag) for flag in params.get("extra_flags", [])]
    if FORBIDDEN_WHISPER_FLAGS & set(extra):
        raise RunnerRefused("whisper extra_flags must not disable timestamps (-nt / --no-timestamps)")
    return [models["binary"], "-m", models["model_path"], "-f", audio,
            "-l", str(params.get("language", "en")), "-ojf", "-of", str(prefix), *extra]


def whisper_segments(raw: dict[str, Any], duration_s: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep decoded offsets as they are; flag, never clip, impossible intervals."""
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise RuntimeError("W2 source audio duration is invalid")
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    previous_start = -1.0
    for ordinal, row in enumerate(raw["transcription"]):
        start, end = float(row["offsets"]["from"]) / 1000, float(row["offsets"]["to"]) / 1000
        rows.append({"text": row["text"], "t_start": start, "t_end": end})
        reasons = []
        if not math.isfinite(start) or not math.isfinite(end):
            reasons.append("nonfinite_timestamp")
        else:
            if start < 0 or end > duration_s or start > duration_s:
                reasons.append("outside_source_audio")
            if end <= start:
                reasons.append("zero_or_negative_duration")
            if start < previous_start:
                reasons.append("backward_timestamp")
        if reasons:
            issues.append({"segment_index": ordinal, "issues": reasons,
                           "t_start": start, "t_end": end, "audio_duration_s": duration_s})
        previous_start = start
    return rows, issues


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _missing(stage: str, package: str, exc: ImportError) -> RunnerRefused:
    return RunnerRefused(f"Stage {stage} needs the '{package}' library in its configured interpreter: {exc}")


def stage_ocr(data: dict[str, Any], models: dict[str, str], params: dict[str, Any], output: Path) -> dict[str, Any]:
    try:
        from rapidocr import RapidOCR  # type: ignore[import-not-found]
    except ImportError as exc:
        raise _missing("ocr", "rapidocr", exc) from exc
    reader = RapidOCR(params={"Global.log_level": "error", **params.get("rapidocr", {}), **models})
    rows: list[dict[str, Any]] = []
    for frame in data["frames"]:
        found = reader(frame["path"])
        boxes, texts, scores = found.boxes, found.txts, found.scores
        items = [] if boxes is None else [
            {"text": str(text), "confidence": float(score), "bbox": box.tolist()}
            for box, text, score in zip(boxes, texts, scores)]
        rows.append({**frame, "text_items": items})
        atomic_write_json(output, {"stage": "ocr", "frames": rows, "partial": True})
    return {"stage": "ocr", "frames": rows, "model_params": models, "runtime_version": _version("rapidocr")}


def stage_asr_w1(data: dict[str, Any], models: dict[str, str], params: dict[str, Any], output: Path) -> dict[str, Any]:
    try:
        import soundfile as sf  # type: ignore[import-not-found]
        from mlx_audio.stt.utils import load_model  # type: ignore[import-not-found]
    except ImportError as exc:
        raise _missing("asr_w1", "mlx-audio / soundfile", exc) from exc
    chunk_s = int(params.get("chunk_seconds", 280))
    max_tokens = int(params.get("max_tokens", 4096))
    language = str(params.get("language", "English"))
    asr = load_model(models["asr_model_path"])
    aligner = load_model(models["aligner_model_path"])
    audio, sr = sf.read(data["audio_path"], dtype="float32")
    if sr != 16000 or audio.ndim != 1:
        raise RuntimeError("W1 requires source audio PCM mono16k")
    rows: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    scratch = output.parent / (output.stem + "-chunks")
    scratch.mkdir(exist_ok=True)
    try:
        for n, start_sample in enumerate(range(0, len(audio), chunk_s * sr)):
            chunk = scratch / f"chunk-{n}.wav"
            start = start_sample / sr
            part = audio[start_sample:start_sample + chunk_s * sr]
            sf.write(chunk, part, sr, subtype="PCM_16")
            result = asr.generate(str(chunk), max_tokens=max_tokens)
            text = result.text.strip()
            aligned = aligner.generate(audio=str(chunk), text=text, language=language) if text else None
            aligned_words = [{"text": w.text, "t_start": float(w.start_time) + start,
                              "t_end": float(w.end_time) + start} for w in aligned.items] if aligned else []
            words.extend(aligned_words)
            rows.append({"t_start": start, "t_end": start + len(part) / sr, "text": text,
                         "raw_model_segments": getattr(result, "segments", None),
                         "generation_tokens": result.generation_tokens, "max_tokens": max_tokens,
                         "truncated": result.generation_tokens >= max_tokens,
                         "aligned_words": aligned_words})
            atomic_write_json(output, {"stage": "asr_w1", "segments": rows, "words": words, "partial": True})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"stage": "asr_w1", "text": " ".join(row["text"] for row in rows), "segments": rows,
            "words": words, "audio_sha256": sha256_file(data["audio_path"]),
            "chunk_policy": f"contiguous_full_audio_{chunk_s}s_no_VAD_deletions",
            "runtime_version": _version("mlx-audio"), "truncated": any(r["truncated"] for r in rows),
            "asr_model_path": models["asr_model_path"], "aligner_model_path": models["aligner_model_path"]}


def stage_asr_w2(data: dict[str, Any], models: dict[str, str], params: dict[str, Any], output: Path,
                 timeout: float) -> dict[str, Any]:
    prefix = output.parent / (output.stem + "-whisper")
    command = whisper_command(models, data["audio_path"], prefix, params)
    proc = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f"Whisper exited {proc.returncode}")
    raw = json.loads(prefix.with_suffix(".json").read_text(encoding="utf-8"))
    with wave.open(data["audio_path"], "rb") as source_audio:
        duration_s = source_audio.getnframes() / source_audio.getframerate()
    rows, issues = whisper_segments(raw, duration_s)
    result = {"stage": "asr_w2", "text": " ".join(row["text"] for row in rows), "segments": rows,
              "raw": raw, "audio_sha256": sha256_file(data["audio_path"]), "command": command,
              "binary_sha256": sha256_file(models["binary"]), "model_path": models["model_path"],
              "exit_code": proc.returncode, "audio_duration_s": duration_s, "timing_issues": issues,
              "timing_validation": {"bounds": "actual_WAV_frames/sample_rate", "clipped": False,
                                    "duration_tolerance_s": 0.0}}
    if issues:
        result["partial"] = True
        atomic_write_json(output, result)
        raise RuntimeError("W2 timestamp validation failed; raw offsets preserved as partial evidence")
    return result


def stage_vlm(data: dict[str, Any], models: dict[str, str], params: dict[str, Any], output: Path) -> dict[str, Any]:
    try:
        from mlx_vlm import generate, load  # type: ignore[import-not-found]
        from mlx_vlm.prompt_utils import apply_chat_template  # type: ignore[import-not-found]
        from mlx_vlm.utils import load_config  # type: ignore[import-not-found]
    except ImportError as exc:
        raise _missing("vlm", "mlx-vlm", exc) from exc
    max_tokens = int(params.get("max_tokens", 4096))
    template = str(params.get("prompt", DEFAULT_VLM_PROMPT))
    model, processor = load(models["model_path"])
    config = load_config(models["model_path"])
    rows: list[dict[str, Any]] = []
    for batch in data["batches"]:
        images = [frame["path"] for frame in batch["frames"]]
        prompt = template.replace("{timestamps}", str([frame["t"] for frame in batch["frames"]]))
        formatted = apply_chat_template(processor, config, prompt, num_images=len(images),
                                        enable_thinking=bool(params.get("enable_thinking", False)))
        generated = generate(model, processor, formatted, image=images, max_tokens=max_tokens,
                             temperature=float(params.get("temperature", 0)),
                             repetition_penalty=float(params.get("repetition_penalty", 1.2)), verbose=False)
        rows.append({**batch, "prompt": prompt, "text": generated.text,
                     "generation_tokens": generated.generation_tokens,
                     "finish_reason": generated.finish_reason,
                     "truncated": generated.finish_reason == "length", "max_tokens": max_tokens})
        atomic_write_json(output, {"stage": "vlm", "batches": rows, "partial": True})
    return {"stage": "vlm", "batches": rows, "model_path": models["model_path"],
            "runtime_version": _version("mlx-vlm"),
            "coverage": "selective_all_prepared_moments_chronological_batches"}


def check_lease(request: dict[str, Any]) -> None:
    """The lease must exist in the shared ledger, owned by the guard, and cover the stage budget."""
    watchdog = request["watchdog_pid"]
    if os.getppid() != watchdog:
        raise RuntimeError("Witness worker requires its lease-owning guard as parent")
    state = Path(os.environ.get("MEMGUARD_STATE_DIR") or Path.home() / ".local/state/memguard")
    lease = json.loads((state / "leases.json").read_text(encoding="utf-8")).get(request["lease_id"])
    if not lease or lease["pid"] != watchdog or lease["reserved_bytes"] < request["estimated_bytes"]:
        raise RuntimeError("Witness worker reservation is missing or insufficient")


def execute(request: dict[str, Any], *, require_lease: bool = True) -> dict[str, Any]:
    if require_lease:
        check_lease(request)
    config = load_config(request["config_path"])
    stage = request["stage"]
    cfg = stage_config(config, stage)
    root = resolve_root(request["root"])
    output = require_inside("output_path", request["output_path"], root)
    manifest_path = require_input_file("manifest_path", request["manifest_path"], root)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    for path in manifest_inputs(data):
        require_input_file("Witness input", path, root)
    for frame in data.get("frames", []):
        if frame.get("sha256") and sha256_file(frame["path"]) != frame["sha256"]:
            raise RuntimeError("Witness frame hash differs from capture provenance")
    models = resolve_models(stage, data, cfg)
    params = cfg.get("params", {})
    if stage == "ocr":
        result = stage_ocr(data, models, params, output)
    elif stage == "asr_w1":
        result = stage_asr_w1(data, models, params, output)
    elif stage == "asr_w2":
        result = stage_asr_w2(data, models, params, output, float(request.get("timeout", 3600)))
    else:
        result = stage_vlm(data, models, params, output)
    result.update(exit_code=0, partial=False)
    atomic_write_json(output, result)
    return {"status": "ok", "output_path": str(output), "sha256": sha256_file(output), "exit_code": 0}


def main() -> int:
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ[key] = "1"
    sys.addaudithook(deny_network)
    request = json.load(sys.stdin)
    try:
        with redirect_stdout(sys.stderr):
            result = execute(request)
    except (RunnerRefused, RuntimeError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}), flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
