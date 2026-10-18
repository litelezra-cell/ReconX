#!/usr/bin/env python3
"""Per-frame motion analysis at the SOURCE frame rate — video analysis, not screenshot analysis.

Iron rule (protocol 2026-10-13.2): claims about motion or creation ("from where to where", "what was
created and in which order", speed, overshoot, smear, scale) rest on every source frame of the window.
Sampled stills are navigation only.

For every decoded frame in each window (no sampling):
  * stage: the largest near-black rectangle (viewer/canvas), found per frame so edited zooms and layout
    changes are followed — or a fixed --crop
  * content mask inside the stage: brighter than --bright, or an HSV colour range (--hsv) for coloured
    text on a non-dark background
  * connected components = elements (letters, words, shapes) with centre, size, area
  * sharpness (variance of the Laplacian on the content) and mean frame difference
Tracking: elements are linked frame to frame (predicted position + size check). Each track reports
birth (t, x, y, w, h) -> end (t, x, y, w, h), displacement, path length, duration, peak speed, overshoot
past the final position, scale change, and the smear frames inside it. Tracks restart at edit cuts
(stage rectangle jumps), so a zoom in the edit is not reported as motion.
Positions are fractions of the stage (0..1, origin top-left); times are start + index / fps (CFR checked).

Every flagged event is a candidate to open in the source frames, not a finding.
Run with an interpreter that already has numpy + opencv (it installs nothing):
  <venv>/bin/python motion_profile.py VIDEO --window 95:110 --out motion.json [--hsv 20,40,80,150]
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

# env RECON_FFMPEG / config key / PATH — no machine path in the skill.
FFMPEG = recon_config.tool("ffmpeg")
FFPROBE = recon_config.tool("ffprobe")
if not (FFMPEG and FFPROBE):
    sys.exit("ffmpeg/ffprobe not found: set them in the Recon config (see config.example.json) or on PATH")


def probe(video: str) -> dict:
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate,avg_frame_rate", "-of", "json", video],
                         capture_output=True, text=True, check=True)
    s = json.loads(out.stdout)["streams"][0]
    return {"width": s["width"], "height": s["height"], "r_frame_rate": s["r_frame_rate"],
            "avg_frame_rate": s["avg_frame_rate"], "fps": float(Fraction(s["r_frame_rate"])),
            "cfr": s["r_frame_rate"] == s["avg_frame_rate"]}


def stage_rect(gray: np.ndarray, dark: int):
    mask = (gray < dark).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        return None
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, area = stats[i]
    if area < 0.04 * gray.size:
        return None
    return int(x) + 2, int(y) + 2, max(int(w) - 4, 1), max(int(h) - 4, 1)  # 2px inset: skip the border


def components(mask: np.ndarray, min_px: int):
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    h, w = mask.shape
    out = []
    for i in range(1, n):
        x, y, bw, bh, a = stats[i]
        if a < min_px:
            continue
        out.append({"cx": float(cents[i][0]) / w, "cy": float(cents[i][1]) / h,
                    "w": bw / w, "h": bh / h, "a": a / (w * h)})
    return out


def analyse(video, start, end, width, info, crop, dark, bright, hsv, min_px):
    sh = int(round(info["height"] * width / info["width"] / 2) * 2)
    cmd = [FFMPEG, "-v", "error", "-ss", str(start), "-to", str(end), "-i", video,
           "-vf", f"scale={width}:{sh}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    fsize, k = width * sh * 3, width / info["width"]
    frames, prev, i = [], None, 0
    while True:
        buf = proc.stdout.read(fsize)
        if len(buf) < fsize:
            break
        bgr = np.frombuffer(buf, np.uint8).reshape(sh, width, 3)
        g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        t = round(start + i / info["fps"], 4)
        rect = tuple(int(v * k) for v in crop) if crop else stage_rect(g, dark)
        fr = {"t": t, "stage": rect, "comps": []}
        if rect:
            x, y, w, h = rect
            roi_g = g[y:y + h, x:x + w]
            if hsv:
                hv = cv2.cvtColor(bgr[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
                m = (hv[..., 0] >= hsv[0]) & (hv[..., 0] <= hsv[1]) & (hv[..., 1] >= hsv[2]) & (hv[..., 2] >= hsv[3])
            else:
                m = roi_g > bright
            fr["area"] = round(float(m.mean()), 5)
            fr["comps"] = components(m, min_px)
            if m.any():
                lap = cv2.Laplacian(roi_g, cv2.CV_64F)
                dil = cv2.dilate(m.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
                fr["sharp"] = round(float(lap[dil].var()), 2)
        if prev is not None:
            fr["diff"] = round(float(np.abs(g.astype(np.int16) - prev.astype(np.int16)).mean()), 3)
        frames.append(fr)
        prev, i = g, i + 1
    proc.wait()
    return frames


def track(frames, fps, max_gap=6, max_dist=0.08, smear_ratio=0.45):
    sharp = [f["sharp"] for f in frames if "sharp" in f]
    med = float(np.median(sharp)) if sharp else None
    tracks, active, cuts = [], [], []
    prev_stage = None
    for idx, f in enumerate(frames):
        st = f.get("stage")
        # an edit cut = the stage jumps by >12% in one frame; a smooth edited zoom/pan is not a cut
        if prev_stage and st and (abs(st[2] - prev_stage[2]) > 0.12 * prev_stage[2] or abs(st[3] - prev_stage[3]) > 0.12 * prev_stage[3]
                                  or abs(st[0] - prev_stage[0]) > 0.12 * prev_stage[2] or abs(st[1] - prev_stage[1]) > 0.12 * prev_stage[3]):
            cuts.append(f["t"])
            tracks.extend(active)
            active = []
        prev_stage = st or prev_stage
        smear = bool(med and f.get("sharp") is not None and f["sharp"] < smear_ratio * med and (f.get("diff") or 0) > 0.5)
        used = set()
        for tr in sorted(active, key=lambda tr: -tr["pts"][-1]["a"]):
            last = tr["pts"][-1]
            vx, vy = (last["cx"] - tr["pts"][-2]["cx"], last["cy"] - tr["pts"][-2]["cy"]) if len(tr["pts"]) > 1 else (0, 0)
            px, py = last["cx"] + vx, last["cy"] + vy
            best, bd = None, max_dist
            for j, c in enumerate(f["comps"]):
                if j in used or not (0.3 <= c["a"] / max(last["a"], 1e-9) <= 3.0):
                    continue
                d = ((c["cx"] - px) ** 2 + (c["cy"] - py) ** 2) ** 0.5
                if d < bd:
                    best, bd = j, d
            if best is not None:
                used.add(best)
                tr["pts"].append({**f["comps"][best], "t": f["t"], "smear": smear})
                tr["miss"] = 0
            else:
                tr["miss"] += 1
        for j, c in enumerate(f["comps"]):
            if j not in used:
                active.append({"pts": [{**c, "t": f["t"], "smear": smear}], "miss": 0})
        done = [tr for tr in active if tr["miss"] > max_gap]
        tracks.extend(done)
        active = [tr for tr in active if tr["miss"] <= max_gap]
    tracks.extend(active)
    out = []
    for tr in tracks:
        p = tr["pts"]
        if len(p) < max(6, int(0.1 * fps)):
            continue
        b, e = p[0], p[-1]
        dx, dy = e["cx"] - b["cx"], e["cy"] - b["cy"]
        disp = (dx * dx + dy * dy) ** 0.5
        path = sum(((q["cx"] - o["cx"]) ** 2 + (q["cy"] - o["cy"]) ** 2) ** 0.5 for o, q in zip(p, p[1:]))
        speeds = [(((q["cx"] - o["cx"]) ** 2 + (q["cy"] - o["cy"]) ** 2) ** 0.5) * fps for o, q in zip(p, p[1:])]
        over = 0.0
        if disp > 0.01:
            ux, uy = dx / disp, dy / disp
            proj = [((q["cx"] - b["cx"]) * ux + (q["cy"] - b["cy"]) * uy) for q in p]
            over = max(0.0, max(proj) - disp) / disp
        moving = [q for q, s in zip(p[1:], speeds) if s > 0.02]
        out.append({
            "birth": {k: round(b[k], 4) for k in ("t", "cx", "cy", "w", "h")},
            "end": {k: round(e[k], 4) for k in ("t", "cx", "cy", "w", "h")},
            "frames": len(p), "duration_s": round(e["t"] - b["t"], 3),
            "displacement": [round(dx, 4), round(dy, 4)], "distance": round(disp, 4), "path": round(path, 4),
            "peak_speed": round(max(speeds), 3) if speeds else 0.0,
            "motion_start_t": moving[0]["t"] if moving else None, "motion_end_t": moving[-1]["t"] if moving else None,
            "overshoot_ratio": round(over, 3), "scale_change": round(e["a"] / max(b["a"], 1e-9), 3),
            "smear_frames": [q["t"] for q in p if q["smear"]],
        })
    out.sort(key=lambda d: -d["path"])
    return {"sharp_median": med, "cuts": cuts, "tracks": out}


def summarize(tr: list[dict]) -> dict:
    moving = [t for t in tr if t["path"] > 0.02]
    if not moving:
        return {"tracks": len(tr), "moving_tracks": 0}
    births = sorted(t["birth"]["t"] for t in moving)
    return {
        "tracks": len(tr), "moving_tracks": len(moving),
        "median_displacement": [round(float(np.median([t["displacement"][0] for t in moving])), 4),
                                round(float(np.median([t["displacement"][1] for t in moving])), 4)],
        "median_motion_s": round(float(np.median([(t["motion_end_t"] or 0) - (t["motion_start_t"] or 0) for t in moving])), 3),
        "births_spread_s": round(births[-1] - births[0], 3), "with_overshoot": sum(t["overshoot_ratio"] > 0.03 for t in moving),
        "median_scale_change": round(float(np.median([t["scale_change"] for t in moving])), 3),
        "tracks_with_smear": sum(bool(t["smear_frames"]) for t in moving),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--window", action="append", required=True, help="start:end seconds (repeatable)")
    ap.add_argument("--width", type=int, default=960)
    ap.add_argument("--crop", help="fixed stage x,y,w,h in SOURCE pixels (default: auto per frame)")
    ap.add_argument("--dark", type=int, default=6, help="stage = largest region darker than this (a pure-black viewer is ~0)")
    ap.add_argument("--bright", type=int, default=110)
    ap.add_argument("--hsv", help="content colour range h_lo,h_hi,s_lo,v_lo (OpenCV hue 0-179)")
    ap.add_argument("--min-px", type=int, default=12)
    ap.add_argument("--keep-frames", action="store_true", help="store per-frame components (large)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    info = probe(a.video)
    crop = tuple(int(v) for v in a.crop.split(",")) if a.crop else None
    hsv = tuple(int(v) for v in a.hsv.split(",")) if a.hsv else None
    res = {"video": a.video, "probe": info, "params": {k: v for k, v in vars(a).items()}, "windows": []}
    for w in a.window:
        s, e = (float(v) for v in w.split(":"))
        frames = analyse(a.video, s, e, a.width, info, crop, a.dark, a.bright, hsv, a.min_px)
        tr = track(frames, info["fps"])
        summ = summarize(tr["tracks"])
        win = {"start": s, "end": e, "frames": len(frames), "expected_frames": int(round((e - s) * info["fps"])),
               "frames_without_stage": sum(1 for f in frames if not f.get("stage")), "summary": summ, **tr}
        win["per_frame"] = [{k: f.get(k) for k in ("t", "stage", "area", "sharp", "diff")} | {"n": len(f["comps"])} for f in frames]
        if a.keep_frames:
            win["components"] = [{"t": f["t"], "comps": f["comps"]} for f in frames]
        res["windows"].append(win)
        print(f"{s:7.2f}-{e:7.2f} frames={len(frames)}/{win['expected_frames']} no_stage={win['frames_without_stage']} "
              f"cuts={len(tr['cuts'])} {json.dumps(summ)}", flush=True)
    with open(a.out, "w") as f:
        json.dump(res, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
