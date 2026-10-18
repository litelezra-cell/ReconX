#!/usr/bin/env python3
"""Capture a public product / documentation page completely, as local evidence.

Why: a fetch-and-summarise tool keeps only what a small model chose to say. The skill needs the page
itself: raw HTML, every visible word verbatim, full-page screenshots, the list of images/videos/links,
the network log, final URL and HTTP status — hashed, so research agents and the report work from a
fixed copy and nothing is lost.

Layout follows the scraping protocol named by `scraping_protocol` in the Recon config (§3), per page:
  <out>/<slug>/html/page.html            raw DOM after load
  <out>/<slug>/data/text.md              innerText verbatim (no summarising)
  <out>/<slug>/data/media.json           images, videos, iframes, links (src/href, alt, size)
  <out>/<slug>/data/page.json            url, final_url, status, title, captured_at, viewport list
  <out>/<slug>/data/network_session.har  network log (git-ignored: may hold cookies set by the site)
  <out>/<slug>/screenshots/{desktop,tablet,mobile}.png   full-page
  <out>/<slug>/manifest.json             every file with sha256
and <out>/INDEX.md (file -> URL -> date).

Limits, on purpose: public pages only. No login, no cookies supplied, no bot-detection or CAPTCHA
bypass. A challenge / login / 4xx-5xx page is recorded as `blocked` with its screenshot — that is the
finding; ask the user for an authorised route instead of working around it.

Run with an interpreter that already has Playwright + Chromium (it installs nothing), e.g.
  <python_site_capture from the Recon config> site_capture.py --out <target>/evidence/site URL [URL ...]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

VIEWPORTS = {"desktop": (1920, 1080), "tablet": (768, 1024), "mobile": (375, 667)}
BLOCK_HINTS = re.compile(r"(verify you are human|checking your browser|cf-challenge|captcha|access denied|sign in to continue|log in to continue)", re.I)
MEDIA_JS = """() => {
  const abs = u => { try { return new URL(u, location.href).href } catch(e) { return u } };
  const imgs = [...document.images].map(i => ({src: abs(i.currentSrc || i.src), alt: i.alt || null, w: i.naturalWidth, h: i.naturalHeight}));
  const vids = [...document.querySelectorAll('video, video source')].map(v => ({src: abs(v.currentSrc || v.src), poster: v.poster ? abs(v.poster) : null}));
  const frames = [...document.querySelectorAll('iframe')].map(f => ({src: abs(f.src), title: f.title || null}));
  const links = [...document.querySelectorAll('a[href]')].map(a => ({href: abs(a.getAttribute('href')), text: (a.innerText || '').trim().slice(0, 200)}));
  const bg = [...document.querySelectorAll('*')].map(e => getComputedStyle(e).backgroundImage).filter(b => b && b.startsWith('url(')).map(b => abs(b.slice(4, -1).replace(/["']/g, '')));
  return {images: imgs, videos: vids, iframes: frames, links, background_images: [...new Set(bg)]};
}"""


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def slug_for(url: str) -> str:
    s = urlsplit(url)
    return re.sub(r"[^a-z0-9]+", "-", (s.netloc + s.path).lower()).strip("-")[:80] or "page"


def capture(pw, url: str, out: Path, wait_ms: int) -> dict:
    d = out / slug_for(url)
    for sub in ("html", "data", "screenshots"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    browser = pw.chromium.launch(headless=True)
    rec = {"url": url, "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "viewports": {}}
    try:
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, record_har_path=str(d / "data/network_session.har"),
                                  record_har_content="omit")
        page = ctx.new_page()
        resp = page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(wait_ms)
        # scroll to the end so lazy content loads, then back to the top
        page.evaluate("async () => { for (let y = 0; y < document.body.scrollHeight; y += 600) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); } window.scrollTo(0, 0); }")
        page.wait_for_timeout(800)
        rec.update({"final_url": page.url, "status": resp.status if resp else None, "title": page.title()})
        (d / "html/page.html").write_text(page.content(), encoding="utf-8")
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        (d / "data/text.md").write_text(f"<!-- {url} | captured {rec['captured_at']} | verbatim innerText -->\n\n{text}\n", encoding="utf-8")
        (d / "data/media.json").write_text(json.dumps(page.evaluate(MEDIA_JS), ensure_ascii=False, indent=1), encoding="utf-8")
        for name, (w, h) in VIEWPORTS.items():
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(500)
            shot = d / f"screenshots/{name}.png"
            page.screenshot(path=str(shot), full_page=True)
            rec["viewports"][name] = [w, h]
        blocked = (rec["status"] or 0) >= 400 or bool(BLOCK_HINTS.search(text[:4000]))
        rec["result"] = "blocked" if blocked else "captured"
        rec["text_chars"] = len(text)
        ctx.close()
    except Exception as e:  # recorded, not swallowed: the page is marked failed with the error
        rec["result"] = "failed"
        rec["error"] = f"{type(e).__name__}: {e}"
    finally:
        browser.close()
    (d / "data/page.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    files = [p for p in sorted(d.rglob("*")) if p.is_file() and p.name != "manifest.json"]
    (d / "manifest.json").write_text(json.dumps({"url": url, "result": rec["result"],
        "files": [{"path": str(p.relative_to(d)), "bytes": p.stat().st_size, "sha256": sha(p)} for p in files]}, indent=1), encoding="utf-8")
    rec["folder"] = str(d)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--out", required=True, help="usually <target>/evidence/site")
    ap.add_argument("--wait-ms", type=int, default=1500)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    rows = []
    with sync_playwright() as pw:
        for u in a.urls:
            r = capture(pw, u, out, a.wait_ms)
            rows.append(r)
            print(json.dumps({k: r.get(k) for k in ("url", "final_url", "status", "result", "title", "text_chars", "error")}, ensure_ascii=False), flush=True)
    idx = out / "INDEX.md"
    lines = idx.read_text(encoding="utf-8").splitlines() if idx.exists() else ["# Site capture index", "", "| Folder | URL | Final URL | Status | Result | Captured |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `{Path(r['folder']).name}/` | {r['url']} | {r.get('final_url')} | {r.get('status')} | {r['result']} | {r['captured_at']} |")
    idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if all(r["result"] == "captured" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
