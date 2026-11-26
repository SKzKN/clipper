"""A project is a folder holding everything clipper knows about one source video.

    <video-name>.clipper/
        project.json      source info and the footage check result
        transcript.json   words with timings
        analysis.json     cached silences, word loudness and face framing
        edls/<name>.json  one EDL per output video
        renders/          previews, frames and exports
        styles/           style specs
        templates/        optional project-only templates
        cache/            overlay frames and temporary render files
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import numpy as np

from . import edl as edl_mod
from .ffmpeg import ClipperError
from .media import probe, read_audio
from .templates import add_template_dir


def default_project_dir(video: str | Path) -> Path:
    video = Path(video).expanduser().resolve()
    return video.with_name(video.stem + ".clipper")


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w\-]+", "-", name.strip()).strip("-").lower()
    if not name:
        raise ClipperError("Name is empty after removing unsupported characters.")
    return name[:60]


class Project:
    def __init__(self, path: str | Path):
        self.dir = Path(path).expanduser().resolve()
        if not (self.dir / "project.json").exists():
            raise ClipperError(f"{self.dir} is not a clipper project (no project.json). Run ingest on a video first.")
        self.meta = json.loads((self.dir / "project.json").read_text())
        add_template_dir(self.dir / "templates")

    # -- creation --------------------------------------------------------
    @classmethod
    def create(cls, video: str | Path, project_dir: str | Path | None = None, checks: dict | None = None) -> "Project":
        video = Path(video).expanduser().resolve()
        info = probe(video)
        pdir = Path(project_dir).expanduser().resolve() if project_dir else default_project_dir(video)
        for sub in ("edls", "renders", "styles", "cache"):
            (pdir / sub).mkdir(parents=True, exist_ok=True)
        meta_path = pdir / "project.json"
        if meta_path.exists():
            old = json.loads(meta_path.read_text())
            if old.get("source", {}).get("path") != info["path"]:
                raise ClipperError(f"{pdir} already belongs to a different video ({old['source']['path']}).")
            if checks is not None:
                old["checks"] = checks
            old["source"] = info
            meta_path.write_text(json.dumps(old, indent=2))
        else:
            meta = {"version": 1, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": info, "checks": checks}
            meta_path.write_text(json.dumps(meta, indent=2))
        return cls(pdir)

    @classmethod
    def open(cls, path: str | Path) -> "Project":
        """Open a project folder, or the project that belongs to a video file."""
        p = Path(path).expanduser()
        if p.is_file():
            p = default_project_dir(p)
        return cls(p)

    # -- basics ----------------------------------------------------------
    @property
    def source(self) -> dict:
        return self.meta["source"]

    @property
    def cache(self) -> Path:
        return self.dir / "cache"

    def save_meta(self) -> None:
        (self.dir / "project.json").write_text(json.dumps(self.meta, indent=2))

    # -- transcript ------------------------------------------------------
    @property
    def transcript_path(self) -> Path:
        return self.dir / "transcript.json"

    def has_transcript(self) -> bool:
        return self.transcript_path.exists()

    def transcript(self) -> dict:
        if not self.has_transcript():
            raise ClipperError("This project has no transcript yet. Run the transcribe stage first.")
        return json.loads(self.transcript_path.read_text())

    def words(self) -> list[dict]:
        return self.transcript()["words"]

    def save_transcript(self, transcript: dict) -> None:
        self.transcript_path.write_text(json.dumps(transcript, ensure_ascii=False))
        self._drop_analysis("word_levels")

    # -- cached analysis -------------------------------------------------
    def _analysis(self) -> dict:
        p = self.dir / "analysis.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def _save_analysis(self, data: dict) -> None:
        (self.dir / "analysis.json").write_text(json.dumps(data))

    def _drop_analysis(self, key: str) -> None:
        data = self._analysis()
        if data.pop(key, None) is not None:
            self._save_analysis(data)

    def silences(self) -> list[tuple[float, float]]:
        data = self._analysis()
        if "silences" not in data:
            from .audio import find_silences

            mono = read_audio(self.source["path"], 16000, 1)
            data["silences"] = find_silences(mono, 16000)
            self._save_analysis(data)
        return [tuple(s) for s in data["silences"]]

    def word_levels(self) -> np.ndarray:
        data = self._analysis()
        if "word_levels" not in data:
            from .audio import word_levels

            mono = read_audio(self.source["path"], 16000, 1)
            data["word_levels"] = [round(float(v), 2) for v in word_levels(mono, 16000, self.words())]
            self._save_analysis(data)
        return np.array(data["word_levels"], dtype=np.float32)

    def framing(self) -> tuple[float, float]:
        """Where the speaker's face is, as fractions of the source frame."""
        checks = self.meta.get("checks") or {}
        face = (checks.get("details") or {}).get("face_center")
        return (face[0], face[1]) if face else (0.5, 0.42)

    # -- EDLs ------------------------------------------------------------
    def edl_path(self, name: str) -> Path:
        return self.dir / "edls" / f"{safe_name(name)}.json"

    def list_edls(self) -> list[str]:
        return sorted(p.stem for p in (self.dir / "edls").glob("*.json"))

    def load_edl(self, name: str) -> dict:
        path = self.edl_path(name)
        if not path.exists():
            have = ", ".join(self.list_edls()) or "none yet"
            raise ClipperError(f"No EDL named {name!r} in this project. Existing: {have}.")
        return edl_mod.load(path)

    def save_edl(self, edl: dict) -> Path:
        path = self.edl_path(edl["name"])
        edl_mod.save(edl, path)
        return path

    def new_edl(self, name: str, mode: str, segments: list[tuple[float, float]] | None = None,
                aspect: str = "9:16") -> dict:
        return edl_mod.new_edl(safe_name(name), mode, self.source, segments, aspect, self.framing())

    def render_path(self, name: str, kind: str) -> Path:
        suffix = {"export": ".mp4", "preview": ".preview.mp4", "frame": ".frame.png"}[kind]
        return self.dir / "renders" / f"{safe_name(name)}{suffix}"
