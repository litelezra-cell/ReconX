#!/usr/bin/env python3
"""Named screenshot of a moment, picked from every source frame around it (protocol 2026-10-14.1).

For each requested moment (from the transcript: a screen/window that opens, a function shown) it decodes
every source frame in [t-1, t+1], scores sharpness (variance of the Laplacian) and stillness (difference to
the previous frame), and saves the best one as <prefix>_<mmss>_<slug>.png. If even the best frame is soft
(< 60% of the window's sharpest) or mid-transition, it also saves the nearest sharp, still frames before and
after (`..._before.png`, `..._after.png`) — a smeared screenshot is never the only evidence.

Usage: <venv with numpy+opencv>/bin/python sharp_shot.py VIDEO OUTDIR PREFIX mm:ss:slug [mm:ss:slug ...]
Prints one JSON line per moment (asked time, saved time, file, sharpness ratio, before/after files).
After saving: open every image and check that its name says what it shows — a name is a claim.
"""
import json, subprocess, sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recon_config  # noqa: E402

FF, FP = recon_config.tool("ffmpeg"), recon_config.tool("ffprobe")
if not (FF and FP):
    sys.exit("ffmpeg/ffprobe not found: set them in the Recon config or on PATH")


def sec(s: str) -> float:
    if ":" in s:
        m, x = s.split(":")
        return int(m) * 60 + float(x)
    return float(s)


def main() -> int:
    video, outdir, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    st = json.loads(subprocess.run([FP, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                    "stream=width,height,r_frame_rate", "-of", "json", video],
                                   capture_output=True, text=True).stdout)["streams"][0]
    W, H = st["width"], st["height"]
    n, d = map(int, st["r_frame_rate"].split("/")); fps = n / d
    for spec in sys.argv[4:]:
        ts, slug = spec.rsplit(":", 1)
        t = sec(ts); a = max(0.0, t - 1.0)
        raw = subprocess.run([FF, "-v", "error", "-ss", f"{a:.3f}", "-t", "2.0", "-i", video,
                              "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
        fr = np.frombuffer(raw, np.uint8).reshape(-1, H, W, 3)
        g = [cv2.cvtColor(cv2.resize(f, (W // 4, H // 4)), cv2.COLOR_BGR2GRAY) for f in fr]
        sharp = np.array([cv2.Laplacian(x, cv2.CV_64F).var() for x in g])
        diff = np.array([0.0] + [float(np.abs(g[i].astype(int) - g[i - 1]).mean()) for i in range(1, len(g))])
        score = sharp / (sharp.max() + 1e-9) - 0.5 * diff / (diff.max() + 1e-9)
        best = int(score.argmax()); tb = a + best / fps
        name = f"{prefix}_{int(tb // 60):02d}{int(tb % 60):02d}_{slug}.png"
        cv2.imwrite(str(Path(outdir) / name), fr[best])
        entry = {"asked_t": t, "saved_t": round(tb, 3), "file": name, "sharp_ratio": round(float(sharp[best] / sharp.max()), 2)}
        if sharp[best] < 0.6 * sharp.max() or diff[best] > np.median(diff) * 3:
            for tag, rng in (("before", range(best - 1, -1, -1)), ("after", range(best + 1, len(fr)))):
                k = next((i for i in rng if sharp[i] >= 0.8 * sharp.max() and diff[i] <= np.median(diff) * 1.5), None)
                if k is not None:
                    tk = a + k / fps
                    n2 = f"{prefix}_{int(tk // 60):02d}{int(tk % 60):02d}_{slug}_{tag}.png"
                    cv2.imwrite(str(Path(outdir) / n2), fr[k]); entry[tag] = n2
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
