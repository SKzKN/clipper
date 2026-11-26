"""Render HTML overlay templates to transparent PNG frames with headless Chromium.

A template is a normal web page. The renderer loads it at the output size,
fills in the parameters, then steps time itself: every CSS animation,
transition and Web Animation on the page is paused and scrubbed to the frame
being captured, so the result is frame-exact and repeatable.

See templates/README.md for the template contract.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .ffmpeg import ClipperError, log
from .templates import load_overlay_template

_PREPARE_JS = """
([params, defaults]) => {
  const values = Object.assign({}, defaults, params);
  const root = document.documentElement;
  for (const [key, value] of Object.entries(values)) {
    root.style.setProperty('--param-' + key, String(value));
  }
  for (const el of document.querySelectorAll('[data-param]')) {
    const v = values[el.dataset.param];
    if (v !== undefined && v !== null) el.textContent = String(v);
  }
  for (const el of document.querySelectorAll('[data-if]')) {
    const v = values[el.dataset.if];
    if (v === undefined || v === null || String(v).trim() === '') el.remove();
  }
  window.CLIPPER.params = values;
  if (typeof window.clipperSetup === 'function') window.clipperSetup(window.CLIPPER);
  return document.fonts.ready.then(() => true);
}
"""

_SEEK_JS = """
(t) => {
  for (const a of document.getAnimations()) { a.pause(); a.currentTime = t * 1000; }
  if (typeof window.clipperFrame === 'function') window.clipperFrame(t, window.CLIPPER.duration);
}
"""


def _launch(playwright):
    errors = []
    for kwargs in ({}, {"channel": "chrome"}):
        try:
            return playwright.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc).splitlines()[0])
    raise ClipperError(
        "Could not start a browser to render overlays. Run `playwright install chromium` "
        f"inside clipper's environment, or install Google Chrome. ({errors[0]})"
    )


def _region_px(region: dict, width: int, height: int) -> dict:
    x = int(max(0, min(1, region.get("x", 0))) * width)
    y = int(max(0, min(1, region.get("y", 0))) * height)
    w = int(min(width - x, round(region.get("w", 1) * width)))
    h = int(min(height - y, round(region.get("h", 1) * height)))
    return {"x": x, "y": y, "width": max(2, w), "height": max(2, h)}


def overlay_key(tpl: dict, params: dict, width: int, height: int, fps: int, duration: float) -> str:
    folder = Path(tpl["path"])
    h = hashlib.sha1()
    for f in sorted(folder.rglob("*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    h.update(json.dumps([params, width, height, fps, round(duration, 3)], sort_keys=True).encode())
    return h.hexdigest()[:16]


def render_overlays(items: list[dict], width: int, height: int, fps: int, cache_dir: Path) -> list[dict]:
    """Render the frames each resolved overlay needs.

    `items` are resolved overlays (tl_start, tl_end, optional phase). Returns,
    per item: frame folder, first frame number, frame count and pixel region.
    Frames already in the cache are reused.
    """
    if not items:
        return []
    jobs = []
    for item in items:
        tpl = load_overlay_template(item["template"])
        duration = float(item["duration"])
        key = overlay_key(tpl, item.get("params", {}), width, height, fps, duration)
        folder = cache_dir / f"{tpl['id'].replace('/', '_')}-{key}"
        folder.mkdir(parents=True, exist_ok=True)
        first = int(round(item.get("phase", 0.0) * fps))
        count = max(1, int(round((item["tl_end"] - item["tl_start"]) * fps)))
        count = min(count, max(1, int(round(duration * fps)) - first))
        missing = [n for n in range(first, first + count) if not (folder / f"{n:05d}.png").exists()]
        jobs.append({"item": item, "tpl": tpl, "folder": folder, "first": first, "count": count,
                     "missing": missing, "duration": duration, "region": _region_px(tpl["region"], width, height)})
    if any(j["missing"] for j in jobs):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = _launch(p)
            try:
                for job in jobs:
                    if not job["missing"]:
                        continue
                    tpl, item = job["tpl"], job["item"]
                    log(f"  overlay {item['id']} ({tpl['id']}): rendering {len(job['missing'])} frames")
                    page = browser.new_page(viewport={"width": width, "height": height})
                    meta = {"duration": job["duration"], "width": width, "height": height, "fps": fps, "params": {}}
                    page.add_init_script(
                        f"window.CLIPPER = {json.dumps(meta)};"
                        "document.addEventListener('DOMContentLoaded', () => {"
                        f"const r = document.documentElement; r.style.setProperty('--duration', '{job['duration']}s');"
                        f"r.style.setProperty('--w', '{width}px'); r.style.setProperty('--h', '{height}px');"
                        "});"
                    )
                    page.goto((Path(tpl["path"]) / "template.html").as_uri())
                    defaults = {k: v.get("default", "") for k, v in tpl["params"].items()}
                    page.evaluate(_PREPARE_JS, [item.get("params", {}), defaults])
                    for n in job["missing"]:
                        page.evaluate(_SEEK_JS, n / fps)
                        page.screenshot(path=str(job["folder"] / f"{n:05d}.png"), omit_background=True,
                                        clip=job["region"], type="png")
                    page.close()
            finally:
                browser.close()
    return [{"id": j["item"]["id"], "folder": j["folder"], "first": j["first"], "count": j["count"],
             "region": j["region"], "tl_start": j["item"]["tl_start"], "tl_end": j["item"]["tl_end"]} for j in jobs]
