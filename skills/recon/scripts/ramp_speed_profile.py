#!/usr/bin/env python3
"""Per-frame apparent speed of the footage — for speed-ramp analysis (protocol iron rule: every source frame).

For every decoded frame (no sampling) it computes dense optical flow (Farneback) against the previous
frame on a downscaled grey image inside a crop, and records the mean flow magnitude (pixels per frame at
the analysis width). A speed ramp shows up as the magnitude rising/falling sharply; a cut as a one-frame
spike with a large frame difference. Output: per-frame series + detected ramp events (segments where the
smoothed magnitude changes by >= --ratio within <= --span seconds, cuts excluded).

This measures how fast the picture moves, not the clip's speed setting: a static shot played at 400% still
moves little. Read it together with the narration and the screen. Every event is a candidate to open in the
source frames, not a finding.

Usage: <python with numpy+opencv> ramp_speed_profile.py VIDEO --out ramp.json [--window a:b ...]
       [--crop x,y,w,h in source px] [--width 320] [--ratio 3] [--span 1.5]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recon_config  # noqa: E402

FF, FP = recon_config.tool("ffmpeg"), recon_config.tool("ffprobe")
if not (FF and FP):
    sys.exit("ffmpeg/ffprobe not found: set them in the Recon config or on PATH")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video"); ap.add_argument("--out", required=True)
    ap.add_argument("--window", action="append", default=[])
    ap.add_argument("--crop", default=None); ap.add_argument("--width", type=int, default=320)
    ap.add_argument("--ratio", type=float, default=3.0); ap.add_argument("--span", type=float, default=1.5)
    a = ap.parse_args()
    st = json.loads(subprocess.run([FP, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                    "stream=width,height,r_frame_rate,avg_frame_rate:format=duration", "-of", "json", a.video],
                                   capture_output=True, text=True).stdout)
    s0 = st["streams"][0]; fps = float(Fraction(s0["r_frame_rate"])); dur = float(st["format"]["duration"])
    windows = [tuple(map(float, w.split(":"))) for w in a.window] or [(0.0, dur)]
    vf = []
    if a.crop:
        x, y, w, h = map(int, a.crop.split(",")); vf.append(f"crop={w}:{h}:{x}:{y}")
    vf.append(f"scale={a.width}:-2"); vf.append("format=gray")
    out = {"video": a.video, "fps": fps, "cfr": s0["r_frame_rate"] == s0["avg_frame_rate"], "crop": a.crop,
           "width": a.width, "windows": []}
    for (t0, t1) in windows:
        raw = subprocess.run([FF, "-v", "error", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}", "-i", a.video, "-vf", ",".join(vf),
                              "-f", "rawvideo", "-"], capture_output=True).stdout
        hdr = subprocess.run([FF, "-v", "error", "-ss", f"{t0:.3f}", "-i", a.video, "-vf", ",".join(vf), "-frames:v", "1",
                              "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
        img = cv2.imdecode(np.frombuffer(hdr, np.uint8), cv2.IMREAD_GRAYSCALE)
        H, W = img.shape
        fr = np.frombuffer(raw, np.uint8)[: (len(raw) // (H * W)) * H * W].reshape(-1, H, W)
        mag, diff = [0.0], [0.0]
        for i in range(1, len(fr)):
            flow = cv2.calcOpticalFlowFarneback(fr[i - 1], fr[i], None, 0.5, 3, 15, 3, 5, 1.2, 0)
            mag.append(float(np.linalg.norm(flow, axis=2).mean()))
            diff.append(float(np.abs(fr[i].astype(int) - fr[i - 1]).mean()))
        mag, diff = np.array(mag), np.array(diff)
        cut = diff > max(25.0, np.median(diff) * 8)
        k = max(1, int(fps * 0.2)); sm = np.convolve(mag, np.ones(k) / k, mode="same")
        events, span = [], int(a.span * fps); i = 0
        while i < len(sm) - span:
            seg = sm[i:i + span]; lo, hi = seg.min(), seg.max()
            if cut[i:i + span].any() or hi < 0.5:
                i += 1; continue
            if hi >= a.ratio * max(lo, 0.05):
                jlo, jhi = int(seg.argmin()), int(seg.argmax())
                kind = "speed_up" if jlo < jhi else "slow_down"
                events.append({"t_start": round(t0 + (i + min(jlo, jhi)) / fps, 3), "t_end": round(t0 + (i + max(jlo, jhi)) / fps, 3),
                               "kind": kind, "mag_low": round(float(lo), 3), "mag_high": round(float(hi), 3),
                               "ratio": round(float(hi / max(lo, 0.05)), 2)})
                i += span
            else:
                i += 1
        out["windows"].append({"start": t0, "end": t1, "frames": int(len(fr)), "expected_frames": int(round((t1 - t0) * fps)),
                               "cuts_t": [round(t0 + j / fps, 3) for j in np.where(cut)[0]],
                               "ramp_events": events,
                               "per_frame": [{"t": round(t0 + j / fps, 3), "mag": round(float(mag[j]), 3), "diff": round(float(diff[j]), 2)}
                                             for j in range(len(fr))]})
        print(f"window {t0:.1f}-{t1:.1f}: {len(fr)} frames, {len(events)} ramp events, {int(cut.sum())} cuts", flush=True)
    Path(a.out).write_text(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
