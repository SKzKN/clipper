"""Find and load caption and overlay templates.

Template folders are searched in this order (first match wins):

1. folders added at run time (a project's own ``templates/``),
2. ``$CLIPPER_TEMPLATES`` (several folders separated by ``:``),
3. ``./templates`` in the current directory,
4. ``~/.clipper/templates``,
5. the built-in examples shipped with the package.

Layout inside a template folder::

    captions/<name>.json
    overlays/<kind>/<name>/template.json
    overlays/<kind>/<name>/template.html
    fonts/*.ttf|otf            (optional, made available to captions)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .ffmpeg import ClipperError

BUILTIN_DIR = Path(__file__).parent / "templates"
OVERLAY_KINDS = ("title_card", "lower_third", "keyword_callout")
_extra_dirs: list[Path] = []

CAPTION_DEFAULTS = {
    "description": "",
    "font": "Arial",
    "bold": True,
    "italic": False,
    "uppercase": False,
    "strip_punctuation": True,
    "size": 0.045,            # font size as a fraction of output height
    "letter_spacing": 0.0,    # fraction of font size
    "color": "#FFFFFF",
    "outline_color": "#000000",
    "outline": 0.0035,        # outline width as a fraction of output height
    "shadow": 0.0,            # drop-shadow offset as a fraction of output height
    "shadow_color": "#000000",
    "box": None,              # {"color": "#000000", "opacity": 0.6} for a solid background box
    "position": {"x": 0.5, "y": 0.72},  # centre of the caption, fractions of the frame
    "max_words": 3,
    "max_chars": 18,
    "max_gap": 0.6,           # a pause longer than this starts a new caption
    "linger": 0.15,           # seconds a caption stays after its last word
    "animation": "none",      # none | pop | fade | word_reveal | highlight
    "highlight_color": "#FFD400",
}
CAPTION_ANIMATIONS = ("none", "pop", "fade", "word_reveal", "highlight")


def add_template_dir(path: str | Path) -> None:
    p = Path(path)
    if p.is_dir() and p not in _extra_dirs:
        _extra_dirs.insert(0, p)


def template_dirs() -> list[Path]:
    dirs = list(_extra_dirs)
    for part in os.environ.get("CLIPPER_TEMPLATES", "").split(os.pathsep):
        if part.strip():
            dirs.append(Path(part).expanduser())
    dirs += [Path.cwd() / "templates", Path.home() / ".clipper" / "templates", BUILTIN_DIR]
    seen, out = set(), []
    for d in dirs:
        if d.is_dir() and d.resolve() not in seen:
            seen.add(d.resolve())
            out.append(d)
    return out


def font_dirs() -> list[Path]:
    return [d / "fonts" for d in template_dirs() if (d / "fonts").is_dir()]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ClipperError(f"Template file {path} is not valid JSON: {exc}") from exc


def list_caption_templates() -> list[dict]:
    out, seen = [], set()
    for d in template_dirs():
        for f in sorted((d / "captions").glob("*.json")):
            if f.stem in seen:
                continue
            seen.add(f.stem)
            tpl = {**CAPTION_DEFAULTS, **_read_json(f)}
            out.append({"name": f.stem, "description": tpl["description"], "animation": tpl["animation"],
                        "max_words": tpl["max_words"], "uppercase": tpl["uppercase"], "font": tpl["font"],
                        "path": str(f)})
    return out


def load_caption_template(name: str, overrides: dict | None = None) -> dict:
    for d in template_dirs():
        f = d / "captions" / f"{name}.json"
        if f.exists():
            tpl = {**CAPTION_DEFAULTS, **_read_json(f), **(overrides or {})}
            tpl["name"] = name
            if tpl["animation"] not in CAPTION_ANIMATIONS:
                raise ClipperError(f"Caption template {name}: animation must be one of {CAPTION_ANIMATIONS}.")
            return tpl
    names = ", ".join(t["name"] for t in list_caption_templates())
    raise ClipperError(f"No caption template named {name!r}. Available: {names}.")


def list_overlay_templates(kind: str | None = None) -> list[dict]:
    out, seen = [], set()
    for d in template_dirs():
        for f in sorted((d / "overlays").glob("*/*/template.json")):
            tpl_id = f"{f.parent.parent.name}/{f.parent.name}"
            if tpl_id in seen:
                continue
            seen.add(tpl_id)
            meta = _read_json(f)
            if kind and meta.get("kind", f.parent.parent.name) != kind:
                continue
            out.append({"id": tpl_id, "kind": meta.get("kind", f.parent.parent.name),
                        "description": meta.get("description", ""),
                        "default_duration": meta.get("default_duration", 3.0),
                        "params": meta.get("params", {}), "path": str(f.parent)})
    return out


def load_overlay_template(tpl_id: str) -> dict:
    for d in template_dirs():
        folder = d / "overlays" / tpl_id
        if (folder / "template.json").exists():
            meta = _read_json(folder / "template.json")
            if not (folder / "template.html").exists():
                raise ClipperError(f"Overlay template {tpl_id} has no template.html in {folder}.")
            meta.setdefault("kind", folder.parent.name)
            meta.setdefault("params", {})
            meta.setdefault("default_duration", 3.0)
            meta.setdefault("region", {"x": 0, "y": 0, "w": 1, "h": 1})
            meta["id"] = tpl_id
            meta["path"] = str(folder)
            return meta
    ids = ", ".join(t["id"] for t in list_overlay_templates())
    raise ClipperError(f"No overlay template named {tpl_id!r}. Available: {ids}.")
