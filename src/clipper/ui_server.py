"""Local web UI: `clipper ui`.

A small HTTP server on 127.0.0.1 that serves the editor page and a JSON API
over the same functions the CLI and the MCP server use. Nothing leaves the
machine. Requests need the per-run token embedded in the page, so other web
pages open in the browser cannot call the API or read files through it.

The landing page is public. Everything else needs an account: sign-up and
sign-in set a session cookie, and every other endpoint refuses without one.
"""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import accounts
from . import audio as audio_mod
from . import deepseek, director, ffmpeg, ops, pipeline, plans
from . import templates as templates_mod
from .captions import caption_groups, timeline_words
from .checks import FootageRejected
from .edl import ASPECTS, TRANSITIONS, Timeline, resolve, summary
from .ffmpeg import ClipperError
from .media import read_audio
from .optable import op_table
from .overlays import _PREPARE_JS, _SEEK_JS
from .project import Project, default_project_dir, safe_name
from .select import select_clips
from .style_apply import load_style

UI_DIR = Path(__file__).parent / "ui"
TOKEN = secrets.token_urlsafe(24)

_jobs: dict[str, dict] = {}
_history: dict[tuple[str, str], dict] = {}   # (project, edl) -> {"undo": [...], "redo": [...]}
_lock = threading.Lock()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _cookie_name() -> str:
    # one cookie per data folder, so two clipper servers on different ports do not sign each other out
    return "clipper_session_" + hashlib.sha256(str(accounts.data_dir()).encode()).hexdigest()[:8]


def _check(request: Request, public: bool = False) -> dict | None:
    """Host and page-token check for every request; a signed-in user for all but the public ones."""
    host = request.headers.get("host", "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        raise ApiError("Bad host.", 403)
    token = request.headers.get("x-clipper-token") or request.query_params.get("t")
    if not token or not secrets.compare_digest(token, TOKEN):
        raise ApiError("Missing or wrong token. Reload the page.", 403)
    user = accounts.user_for(request.cookies.get(_cookie_name()))
    if not user and not public:
        raise ApiError("Sign in to continue.", 401)
    request.state.user = user
    return user


def api(fn, public: bool = False):
    """JSON endpoint wrapper: token and session check, body parsing, errors as {"error": ...}."""
    async def endpoint(request: Request):
        try:
            _check(request, public)
            body = {}
            if request.method == "POST":
                raw = await request.body()
                body = json.loads(raw) if raw else {}
            from starlette.concurrency import run_in_threadpool

            result = await run_in_threadpool(fn, request, body)
            return result if isinstance(result, Response) else JSONResponse(result)
        except ApiError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except (ClipperError, accounts.AccountError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse({"error": f"Bad request: {exc}"}, status_code=400)
    return endpoint


def _project(src) -> Project:
    path = src.get("project") if isinstance(src, dict) else src.query_params.get("project")
    if not path:
        raise ApiError("No project given.")
    return Project.open(path)


def _recent_file(user: dict) -> Path:
    return accounts.user_dir(user) / "recent.json"


def _remember(proj: Project, user: dict) -> None:
    items = _recent(user)
    entry = {"project": str(proj.dir), "video": proj.source["path"], "name": Path(proj.source["path"]).name,
             "duration": proj.source["duration"], "opened": time.strftime("%Y-%m-%d %H:%M")}
    items = [entry] + [i for i in items if i["project"] != entry["project"]]
    path = _recent_file(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items[:12], indent=1))


def _recent(user: dict) -> list[dict]:
    try:
        items = json.loads(_recent_file(user).read_text())
    except (OSError, ValueError):
        return []
    return [i for i in items if Path(i["project"], "project.json").exists()]


# --------------------------------------------------------------------------
# Jobs (anything that takes more than a moment)
# --------------------------------------------------------------------------

def start_job(kind: str, fn, label: str) -> dict:
    job_id = uuid.uuid4().hex[:10]
    job = {"id": job_id, "kind": kind, "label": label, "state": "running", "progress": None, "message": "",
           "started": time.time()}
    _jobs[job_id] = job

    def work():
        ffmpeg.set_sinks(lambda msg: job.update(message=msg.strip().splitlines()[-1][:160], progress=None),
                         lambda frac: job.update(progress=round(frac, 3)))
        try:
            job["result"] = fn()
            job["state"] = "done"
        except FootageRejected as exc:
            job.update(state="rejected", result=exc.result, error=str(exc))
        except ClipperError as exc:
            job.update(state="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - report anything to the page instead of dying silently
            job.update(state="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            job["seconds"] = round(time.time() - job["started"], 1)
            ffmpeg.set_sinks(None, None)

    threading.Thread(target=work, daemon=True, name=f"job-{kind}").start()
    return {"job": job_id}


# --------------------------------------------------------------------------
# What the page needs to draw an EDL
# --------------------------------------------------------------------------

def _preview_state(proj: Project, name: str) -> dict:
    preview, edl_file = proj.render_path(name, "preview"), proj.edl_path(name)
    export = proj.render_path(name, "export")
    out = {"exists": preview.exists(), "stale": False, "export": str(export) if export.exists() else None}
    if preview.exists():
        out["stale"] = preview.stat().st_mtime < edl_file.stat().st_mtime
        out["version"] = int(preview.stat().st_mtime)
    return out


def edl_payload(proj: Project, doc: dict) -> dict:
    tl = Timeline(doc)
    res = resolve(doc, tl)
    words = proj.words() if proj.has_transcript() else []
    word_tl = []
    for w in words:
        ranges = tl.map_range(w["start"], w["end"])
        kept = sum(b - a for a, b in ranges)
        word_tl.append([round(ranges[0][0], 3), round(ranges[-1][1], 3)]
                       if kept >= 0.5 * max(w["end"] - w["start"], 1e-3) else None)
    cap = doc["captions"]
    captions = {"enabled": bool(cap.get("enabled") and cap.get("template")), "template_name": cap.get("template"),
                "overrides": cap.get("overrides") or {}, "template": None, "groups": []}
    if cap.get("template"):
        tpl = templates_mod.load_caption_template(cap["template"], cap.get("overrides"))
        captions["template"] = tpl
        if captions["enabled"] and words:
            captions["groups"] = [
                {"start": round(g["start"], 3), "end": round(g["end"], 3),
                 "words": [{"text": w["text"], "start": round(w["start"], 3)} for w in g["words"]]}
                for g in caption_groups(timeline_words(words, tl), tpl)]
    out_w, out_h = doc["output"]["width"], doc["output"]["height"]
    src_w, src_h = doc["source"]["width"], doc["source"]["height"]
    cover = max(out_w / src_w, out_h / src_h)
    base_w, base_h = src_w * cover, src_h * cover
    ax, ay = doc["framing"]["x"], doc["framing"]["y"]
    x0 = min(max(ax * base_w - out_w / 2, 0), base_w - out_w)
    y0 = min(max(ay * base_h - out_h / 2, 0), base_h - out_h)
    key = (str(proj.dir), doc["name"])
    hist = _history.get(key, {"undo": [], "redo": []})
    place = lambda items: [{**it, "tl_start": round(it["tl_start"], 3), "tl_end": round(it["tl_end"], 3)} for it in items]
    return {
        "name": doc["name"], "mode": doc["mode"], "title": doc.get("title"),
        "duration": round(tl.duration, 3), "fps": tl.fps, "output": doc["output"], "framing": doc["framing"],
        "geometry": {"base_w": base_w, "base_h": base_h, "x0": x0, "y0": y0, "out_w": out_w, "out_h": out_h},
        "segments": [{"id": s.id, "src": [round(s.src_start, 4), round(s.src_end, 4)],
                      "tl": [round(s.tl_start, 4), round(s.tl_end, 4)],
                      "transition": (s.transition_in or {}).get("type")} for s in tl.segs],
        "cuts": [{**c, "time": round(c["time"], 3)} for c in tl.cuts()],
        "word_tl": word_tl,
        "captions": captions,
        "zooms": place(res["zooms"]), "overlays": place(res["overlays"]), "broll": place(res["broll"]),
        "sfx": [{**s, "tl_at": round(s["tl_at"], 3)} for s in res["sfx"]],
        "music": doc.get("music"),
        "notes": doc.get("notes", []),
        "warnings": res["warnings"],
        "can_undo": bool(hist["undo"]), "can_redo": bool(hist["redo"]),
        "preview": _preview_state(proj, doc["name"]),
    }


def project_payload(proj: Project) -> dict:
    return {"project": str(proj.dir), "name": Path(proj.source["path"]).name, "source": proj.source,
            "checks": proj.meta.get("checks"), "has_transcript": proj.has_transcript(),
            "edls": [summary(proj.load_edl(n)) for n in proj.list_edls()]}


# --------------------------------------------------------------------------
# Edit operations exposed to the page
# --------------------------------------------------------------------------

def _push_history(proj: Project, doc: dict) -> None:
    key = (str(proj.dir), doc["name"])
    hist = _history.setdefault(key, {"undo": [], "redo": []})
    hist["undo"].append(copy.deepcopy(doc))
    del hist["undo"][:-60]
    hist["redo"].clear()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

def ep_state(request, body):
    """What the page needs to draw itself. Public: signed out, it carries no videos, only what the landing page shows."""
    caps = ffmpeg.capabilities()
    user = request.state.user
    return {
        "user": user,
        "has_accounts": accounts.has_accounts(),
        "pricing": plans.payload(),
        "usage": _usage(user) if user else None,
        "ai": {k: v for k, v in deepseek.status().items() if k != "key_from"},
        "recent": _recent(user) if user else [],
        "captions": templates_mod.list_caption_templates(),
        "overlays": templates_mod.list_overlay_templates(),
        "transitions": ["cut", *TRANSITIONS],
        "sounds": list(audio_mod.BUILTIN_SFX),
        "aspects": ["9:16", "16:9", "1:1", "4:5", "source"],
        "overlay_js": {"prepare": _PREPARE_JS, "seek": _SEEK_JS},
        "problems": [] if caps["ass"] else ["This ffmpeg cannot draw captions (no libass)."],
        "can_pick": sys.platform == "darwin",
    }


def _usage(user: dict) -> dict:
    limit = plans.plan(user.get("plan"))["minutes"]
    return {"minutes": round(accounts.usage_seconds(user) / 60, 1), "limit": limit, "plan": plans.plan(user.get("plan"))["name"]}


def ep_want_plan(request, body):
    """Remember which paid plan this account wants to hear about. Nothing is charged; payments are not connected."""
    plan_id = body.get("plan")
    if plan_id is not None and plan_id not in [p["id"] for p in plans.PLANS if p["id"] != "free"]:
        raise ApiError("Unknown plan.")
    promo = plans.active_promo()
    offer = promo["id"] if promo and plan_id in promo["plans"] else None
    return {"user": accounts.want_plan(request.state.user, plan_id, offer), "with_offer": promo["name"] if offer else None}


def _signed_in(user: dict) -> JSONResponse:
    response = JSONResponse({"user": user})
    response.set_cookie(_cookie_name(), accounts.new_session(user), max_age=accounts.SESSION_DAYS * 86400,
                        httponly=True, samesite="strict", path="/")
    return response


def ep_signup(request, body):
    return _signed_in(accounts.sign_up(body.get("name"), body.get("email"), body.get("password")))


def ep_signin(request, body):
    return _signed_in(accounts.sign_in(body.get("email"), body.get("password")))


def ep_signout(request, body):
    accounts.end_session(request.cookies.get(_cookie_name()))
    response = JSONResponse({"user": None})
    response.delete_cookie(_cookie_name(), path="/")
    return response


def ep_pick(request, body):
    """Native file dialog (macOS). Returns {"path": ...} or {"path": null} if cancelled."""
    if sys.platform != "darwin":
        raise ApiError("The file dialog is only available on macOS. Type or paste the path instead.")
    kind = body.get("kind", "video")
    types = {"video": '{"public.movie"}', "audio": '{"public.audio"}', "media": '{"public.movie", "public.image"}',
             "json": '{"public.json"}'}.get(kind)
    prompt = {"video": "Choose a video", "audio": "Choose a music or sound file", "media": "Choose a clip or image",
              "json": "Choose a style spec"}.get(kind, "Choose a file")
    script = f'POSIX path of (choose file with prompt "{prompt}"' + (f" of type {types})" if types else ")")
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    return {"path": proc.stdout.strip() or None}


def ep_open(request, body):
    """Open a video (checking it first) or an existing project."""
    path = Path(str(body.get("path", "")).strip().strip('"').strip("'")).expanduser()
    if not path.exists():
        raise ApiError(f"Nothing found at {path}")
    force = bool(body.get("force"))
    user = request.state.user
    if path.is_dir() or (default_project_dir(path) / "project.json").exists() and not force:
        proj = Project.open(path)
        if (proj.meta.get("checks") or {}).get("ok") or (proj.meta.get("checks") or {}).get("forced"):
            _remember(proj, user)
            return {"project": project_payload(proj)}

    def work():
        new = not (default_project_dir(path) / "project.json").exists()
        proj = pipeline.ingest(str(path), force=force)
        if new:
            accounts.add_usage(user, proj.source["duration"])
        _remember(proj, user)
        return project_payload(proj)
    return start_job("open", work, f"Checking {path.name}")


def ep_project(request, body):
    return project_payload(_project(request))


def ep_transcribe(request, body):
    proj = _project(body)

    def work():
        t = pipeline.ensure_transcript(proj, body.get("model"), body.get("language"), bool(body.get("redo")))
        return {"words": len(t["words"]), "language": t["language"]}
    return start_job("transcribe", work, "Transcribing")


def ep_transcript(request, body):
    proj = _project(request)
    if not proj.has_transcript():
        return {"words": [], "fillers": []}
    words = proj.words()
    fillers = [i for i, w in enumerate(words) if ops.norm_word(w["w"]) in ops.DEFAULT_FILLERS]
    return {"words": [[w["w"], w["start"], w["end"]] for w in words], "fillers": fillers}


def ep_peaks(request, body):
    """Waveform of the source, as peak values in equal time slices."""
    proj = _project(request)
    cache = proj.cache / "peaks.json"
    if cache.exists():
        return json.loads(cache.read_text())
    mono = read_audio(proj.source["path"], 8000, 1)
    per_second = 50
    n = max(1, int(len(mono) / 8000 * per_second))
    step = max(1, len(mono) // n)
    peaks = np.abs(mono[: n * step]).reshape(n, step).max(axis=1) if len(mono) >= n * step else np.zeros(1)
    top = float(np.percentile(peaks, 99)) or 1.0
    data = {"per_second": len(peaks) / max(len(mono) / 8000, 1e-3),
            "peaks": [round(min(1.0, float(p) / top), 3) for p in peaks]}
    cache.write_text(json.dumps(data))
    return data


def ep_edl(request, body):
    proj = _project(request)
    return edl_payload(proj, proj.load_edl(request.query_params["edl"]))


def ep_create(request, body):
    """Create EDLs: one polish EDL, clips from proposals, or a clip from a source range.
    With `ai` (the default once DeepSeek is connected) the AI editor finishes each one, as a job."""
    proj = _project(body)
    kind = body.get("kind", "polish")
    aspect = body.get("aspect", "9:16")
    recipe = body.get("recipe", True)
    ai = pipeline.use_ai(None if body.get("ai") is None else bool(body["ai"])) and recipe
    finish = {"captions": body.get("captions", "bold-pop"), "zooms_per_minute": body.get("zooms_per_minute", 6.0)}
    finisher = (lambda doc: pipeline.finish_with_ai(proj, doc, body.get("brief"), **finish)) if ai else \
               (lambda doc: pipeline.finish_edl(proj, doc, **finish))

    def work():
        created, notes = [], []
        if kind == "polish":
            doc = proj.new_edl(body.get("name") or "polish", "polish", None, aspect)
            if recipe:
                notes.append(finisher(doc).get("note"))
            proj.save_edl(doc)
            created.append(doc["name"])
        else:
            used = set(proj.list_edls())
            for clip in body["clips"]:
                n = 1
                while f"clip-{n:02d}" in used:
                    n += 1
                name = f"clip-{n:02d}"
                used.add(name)
                end = min(float(clip["end"]), proj.source["duration"])
                doc = proj.new_edl(name, "clip", [(float(clip["start"]), end)], aspect)
                doc["title"] = clip.get("title")
                if recipe:
                    notes.append(finisher(doc).get("note"))
                proj.save_edl(doc)
                created.append(name)
        return {"created": created, "project": project_payload(proj), "note": next((n for n in notes if n), None)}

    return start_job("create", work, "The AI editor is working") if ai else work()


def ep_delete_edl(request, body):
    proj = _project(body)
    path = proj.edl_path(body["edl"])
    if path.exists():
        path.unlink()
    return {"project": project_payload(proj)}


def ep_select(request, body):
    proj = _project(body)
    ai = pipeline.use_ai(None if body.get("ai") is None else bool(body["ai"]))
    work = lambda: select_clips(proj.words(), int(body.get("count", 3)), float(body.get("min", 20)),
                                float(body.get("max", 60)), ai)
    return start_job("select", work, "Choosing clips") if ai else work()


def ep_direct(request, body):
    """Hand the edit to the AI editor with an instruction. One undo step takes the whole pass back."""
    proj = _project(body)
    name = body["edl"]
    instruction = str(body.get("instruction") or "").strip()[:2000]
    pipeline.use_ai(True)

    def work():
        before = proj.load_edl(name)
        doc = copy.deepcopy(before)
        result = director.direct(proj, doc, instruction)
        with _lock:
            _push_history(proj, before)
            proj.save_edl(doc)
        return {**result, "edl": edl_payload(proj, doc)}
    return start_job("direct", work, "The AI editor is working")


def ep_op(request, body):
    proj = _project(body)
    with _lock:
        doc = proj.load_edl(body["edl"])
        table = op_table(proj)
        if body["op"] not in table:
            raise ApiError(f"Unknown operation {body['op']!r}.")
        before = copy.deepcopy(doc)
        result = table[body["op"]](doc, body.get("args") or {})
        _push_history(proj, before)
        proj.save_edl(doc)
    return {"result": result, "edl": edl_payload(proj, doc)}


def ep_undo(request, body):
    proj = _project(body)
    key = (str(proj.dir), safe_name(body["edl"]))
    hist = _history.get(key, {"undo": [], "redo": []})
    src, dst = ("redo", "undo") if body.get("redo") else ("undo", "redo")
    if not hist[src]:
        raise ApiError("Nothing to redo." if body.get("redo") else "Nothing to undo.")
    with _lock:
        hist[dst].append(proj.load_edl(body["edl"]))
        doc = hist[src].pop()
        proj.save_edl(doc)
    return {"edl": edl_payload(proj, doc)}


def ep_render(request, body):
    proj = _project(body)
    name, quality = body["edl"], body.get("quality", "preview")

    def work():
        result = pipeline.render_edl(proj, name, quality, body.get("start"), body.get("end"))
        return {**result, "preview": _preview_state(proj, name)}
    return start_job("export" if quality == "export" else "preview", work,
                     "Exporting" if quality == "export" else "Rendering preview")


def ep_frame(request, body):
    proj = _project(request)
    result = pipeline.render_edl(proj, request.query_params["edl"], "export",
                                 frame_at=float(request.query_params.get("at", 0)))
    return FileResponse(result["path"], media_type="image/png", headers={"Cache-Control": "no-store"})


def ep_style_analyze(request, body):
    ref = Path(str(body["reference"]).strip()).expanduser()
    if not ref.exists():
        raise ApiError(f"Nothing found at {ref}")
    proj = _project(body)
    out = proj.dir / "styles" / f"{safe_name(ref.stem)}.style.json"

    def work():
        spec, path = pipeline.style_from_reference(str(ref), str(out))
        return {"spec": spec, "path": str(path)}
    return start_job("style", work, f"Analysing {ref.name}")


def ep_styles(request, body):
    proj = _project(request)
    out = []
    for f in sorted((proj.dir / "styles").glob("*.style.json")):
        try:
            out.append({"path": str(f), "name": f.name.replace(".style.json", ""), "spec": json.loads(f.read_text())})
        except ValueError:
            pass
    return {"styles": out}


def ep_job(request, body):
    job = _jobs.get(request.path_params["job_id"])
    if not job:
        raise ApiError("Unknown job.", 404)
    return {k: v for k, v in job.items() if k != "started"}


def ep_reveal(request, body):
    path = Path(body["path"])
    if not path.exists():
        raise ApiError("That file no longer exists.")
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], check=False)
    return {"ok": True}


# -- files -------------------------------------------------------------------

def ep_media(request, body):
    """Serve the files the page plays: the source, renders, b-roll and style sheets of a project."""
    proj = _project(request)
    kind = request.query_params.get("kind", "source")
    if kind == "source":
        proxy = proj.cache / "proxy.mp4"
        path = proxy if request.query_params.get("proxy") and proxy.exists() else Path(proj.source["path"])
    elif kind in ("preview", "export"):
        path = proj.render_path(request.query_params["edl"], kind)
    elif kind == "broll":
        doc = proj.load_edl(request.query_params["edl"])
        item = next((b for b in doc["broll"] if b["id"] == request.query_params.get("id")), None)
        if not item:
            raise ApiError("Unknown b-roll item.", 404)
        path = Path(item["path"])
    elif kind == "style_sheet":
        path = (proj.dir / "styles" / Path(request.query_params["name"]).name).with_suffix("").with_suffix(".style.frames.jpg")
    else:
        raise ApiError("Unknown media kind.")
    if not path.exists():
        raise ApiError("File not found.", 404)
    return FileResponse(path, headers={"Cache-Control": "no-store"})


def ep_proxy(request, body):
    """Make a browser-friendly copy of a source the browser cannot play."""
    proj = _project(body)
    out = proj.cache / "proxy.mp4"

    def work():
        if not out.exists():
            ffmpeg.run(["-i", proj.source["path"], "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset",
                        "veryfast", "-crf", "24", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
                       what="Proxy", total=proj.source["duration"])
        return {"ok": True}
    return start_job("proxy", work, "Preparing a playable copy")


async def ep_template_file(request: Request):
    """Files of an overlay template, so the page can show it live in an iframe."""
    try:
        _check(request)
        tpl = templates_mod.load_overlay_template(f"{request.path_params['kind']}/{request.path_params['name']}")
        folder = Path(tpl["path"]).resolve()
        target = (folder / request.path_params["file"]).resolve()
        if folder not in target.parents or not target.is_file():
            raise ApiError("Not found.", 404)
        return FileResponse(target, headers={"Cache-Control": "no-store"})
    except ApiError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    except ClipperError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


class FreshStatic(StaticFiles):
    """The page's own files, always revalidated: after an update the browser must not run a stale script."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


async def index(request: Request):
    html = (UI_DIR / "index.html").read_text().replace("__TOKEN__", TOKEN)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def create_app() -> Starlette:
    get = lambda path, fn: Route(path, api(fn), methods=["GET"])
    post = lambda path, fn: Route(path, api(fn), methods=["POST"])
    return Starlette(routes=[
        Route("/", index),
        Route("/api/state", api(ep_state, public=True), methods=["GET"]),
        Route("/api/auth/signup", api(ep_signup, public=True), methods=["POST"]),
        Route("/api/auth/signin", api(ep_signin, public=True), methods=["POST"]),
        Route("/api/auth/signout", api(ep_signout, public=True), methods=["POST"]),
        post("/api/pick", ep_pick), post("/api/open", ep_open), post("/api/direct", ep_direct),
        post("/api/plan/interest", ep_want_plan),
        get("/api/project", ep_project), post("/api/transcribe", ep_transcribe), get("/api/transcript", ep_transcript),
        get("/api/peaks", ep_peaks), get("/api/edl", ep_edl), post("/api/create", ep_create),
        post("/api/delete_edl", ep_delete_edl), post("/api/select", ep_select), post("/api/op", ep_op),
        post("/api/undo", ep_undo), post("/api/render", ep_render), get("/api/frame", ep_frame),
        post("/api/style/analyze", ep_style_analyze), get("/api/styles", ep_styles),
        get("/api/job/{job_id}", ep_job), post("/api/reveal", ep_reveal), get("/api/media", ep_media),
        post("/api/proxy", ep_proxy),
        Route("/tpl/{kind}/{name}/{file:path}", ep_template_file),
        Mount("/static", FreshStatic(directory=UI_DIR), name="static"),
    ])


def serve(port: int = 8765, open_browser: bool = True, video: str | None = None) -> None:
    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    if video:
        from urllib.parse import quote

        url += "#open=" + quote(str(Path(video).expanduser().resolve()))
    print(f"clipper UI: {url}", file=sys.stderr)
    if open_browser:
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
