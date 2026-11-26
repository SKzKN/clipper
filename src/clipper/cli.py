"""Command line: `clipper <command>`. Results go to stdout as JSON; progress goes to stderr."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ffmpeg, pipeline
from . import templates as templates_mod
from .checks import FootageRejected, check_footage
from .edl import summary
from .ffmpeg import ClipperError
from .project import Project
from .style_apply import load_style


def out(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _finish_args(args) -> dict:
    return {
        "fillers": not args.keep_fillers,
        "pauses": not args.keep_pauses,
        "captions": None if args.captions == "none" else args.captions,
        "zooms_per_minute": args.zooms,
        "style": load_style(args.style) if args.style else None,
        "music": args.music,
    }


def _add_finish_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--project", help="project folder (default: <video name>.clipper next to the video)")
    p.add_argument("--aspect", default="9:16", help="9:16 (default), 16:9, 1:1, 4:5 or source")
    p.add_argument("--captions", default="bold-pop", help="caption template name, or 'none'")
    p.add_argument("--zooms", type=float, default=6.0, help="punch-in zooms per minute (0 = none)")
    p.add_argument("--style", help="style spec JSON from `clipper style` to apply")
    p.add_argument("--music", help="background music file (ducked under speech)")
    p.add_argument("--ai", dest="ai", action="store_true", default=None,
                   help="let DeepSeek do the edit (the default once a key is set; see `clipper deepseek`)")
    p.add_argument("--no-ai", dest="ai", action="store_false", help="use the built-in recipe even if DeepSeek is connected")
    p.add_argument("--brief", help="what the AI editor should do, in plain words (default: a standard finish)")
    p.add_argument("--keep-fillers", action="store_true", help="do not cut um/uh")
    p.add_argument("--keep-pauses", action="store_true", help="do not shorten long pauses")
    p.add_argument("--model", help="whisper model (tiny, base, small, medium, large-v3); default small")
    p.add_argument("--language", help="spoken language code, e.g. en (default: detect)")
    p.add_argument("--preview", action="store_true", help="render a fast half-size preview instead of the final file")
    p.add_argument("--no-render", action="store_true", help="stop after writing the EDL(s)")
    p.add_argument("--force", action="store_true", help="continue even if the footage check fails")


def cmd_doctor(args) -> None:
    caps = ffmpeg.capabilities()
    from .vision import face_backend, ocr_available

    report = {"ffmpeg": caps, "faces": face_backend(), "text_recognition": ocr_available(),
              "caption_templates": [t["name"] for t in templates_mod.list_caption_templates()],
              "overlay_templates": [t["id"] for t in templates_mod.list_overlay_templates()],
              "template_folders": [str(d) for d in templates_mod.template_dirs()]}
    for mod, label in (("faster_whisper", "transcription"), ("sherpa_onnx", "voice_check"),
                       ("playwright", "overlay_renderer")):
        try:
            __import__(mod)
            report[label] = "ok"
        except ImportError:
            report[label] = "not installed"
    problems = []
    if not caps["ass"]:
        problems.append("ffmpeg has no libass: captions cannot be drawn.")
    if not caps["xfade"]:
        problems.append("ffmpeg has no xfade filter: transitions cannot be rendered.")
    from . import deepseek

    report["ai"] = deepseek.status()
    report["problems"] = problems
    out(report)


def cmd_check(args) -> None:
    result = check_footage(args.video)
    out(result)
    if not result["ok"]:
        sys.exit(2)


def cmd_transcribe(args) -> None:
    proj = pipeline.ingest(args.video, args.project, force=args.force)
    t = pipeline.ensure_transcript(proj, args.model, args.language, args.redo)
    out({"project": str(proj.dir), "language": t["language"], "words": len(t["words"]),
         "text": " ".join(w["w"] for w in t["words"])})


def cmd_polish(args) -> None:
    proj = pipeline.ingest(args.video, args.project, force=args.force)
    pipeline.ensure_transcript(proj, args.model, args.language)
    edl, report = pipeline.make_polish(proj, args.name, args.aspect, ai=args.ai, brief=args.brief, **_finish_args(args))
    result = {"project": str(proj.dir), "edl": summary(edl), "report": report}
    if not args.no_render:
        result["render"] = pipeline.render_edl(proj, edl["name"], "preview" if args.preview else "export")
    out(result)


def cmd_clip(args) -> None:
    proj = pipeline.ingest(args.video, args.project, force=args.force)
    pipeline.ensure_transcript(proj, args.model, args.language)
    clips = pipeline.make_clips(proj, args.count, args.min, args.max, True if args.llm else args.ai, args.aspect, **_finish_args(args))
    if not args.no_render:
        for c in clips:
            c["render"] = pipeline.render_edl(proj, c["edl"], "preview" if args.preview else "export")
    out({"project": str(proj.dir), "clips": clips})


def cmd_select(args) -> None:
    proj = pipeline.ingest(args.video, args.project, force=args.force)
    pipeline.ensure_transcript(proj, args.model, args.language)
    from .select import select_clips

    out(select_clips(proj.words(), args.count, args.min, args.max, True if args.llm else None))


def cmd_style(args) -> None:
    spec, path = pipeline.style_from_reference(args.reference, args.out, args.max_seconds)
    out({"style_spec": str(path), **spec})


def cmd_apply_style(args) -> None:
    out(pipeline.apply_style_to(Project.open(args.project), args.edl, args.style, args.music))


def cmd_render(args) -> None:
    proj = Project.open(args.project)
    out(pipeline.render_edl(proj, args.edl, "preview" if args.preview else "export", args.start, args.end,
                            args.frame, args.out))


def cmd_edls(args) -> None:
    proj = Project.open(args.project)
    out({"project": str(proj.dir), "edls": [summary(proj.load_edl(n)) for n in proj.list_edls()]})


def cmd_templates(args) -> None:
    out({"captions": templates_mod.list_caption_templates(), "overlays": templates_mod.list_overlay_templates(),
         "template_folders": [str(d) for d in templates_mod.template_dirs()]})


def cmd_ui(args) -> None:
    from .ui_server import serve

    serve(args.port, open_browser=not args.no_open, video=args.video)


def cmd_deepseek(args) -> None:
    """Connect DeepSeek, the model that chooses clips and plans edits."""
    import getpass

    from . import deepseek

    if args.action == "set-key":
        deepseek.set_key(getpass.getpass("DeepSeek API key (input is hidden): "))
        print(f"Saved to {deepseek.key_file()} (readable only by you).", file=sys.stderr)
    elif args.action == "remove-key":
        deepseek.remove_key()
    elif args.action == "test":
        answer = deepseek.chat([{"role": "user", "content": "Reply with the single word: ready"}], effort="low", max_tokens=2000)
        out({"ok": True, "model": answer.get("_model"), "reply": (answer.get("content") or "").strip()})
        return
    out(deepseek.status())


def cmd_direct(args) -> None:
    """Have the AI editor change an EDL: `clipper direct talk.clipper polish "make it tighter"`."""
    from . import director

    proj = Project.open(args.project)
    pipeline.use_ai(True)
    doc = proj.load_edl(args.edl)
    result = director.direct(proj, doc, " ".join(args.instruction))
    proj.save_edl(doc)
    out({**result, "edl": summary(doc)})


def cmd_account(args) -> None:
    """Manage the editor's accounts from the terminal (there is no email to send a reset link to)."""
    import getpass

    from . import accounts

    try:
        if args.action == "list":
            out(accounts.list_users())
        elif not args.email:
            raise ClipperError(f"Give the account's email: clipper account {args.action} you@example.com")
        elif args.action == "reset":
            password = getpass.getpass("New password: ")
            if getpass.getpass("Again: ") != password:
                raise ClipperError("The two passwords are different. Nothing was changed.")
            out({"password_changed": accounts.set_password(args.email, password)})
        else:
            out({"removed": accounts.remove(args.email)})
    except accounts.AccountError as exc:
        raise ClipperError(str(exc)) from exc


def cmd_mcp(args) -> None:
    from .mcp_server import main as mcp_main

    mcp_main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipper", description="Edit single-speaker talking-head video.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("polish", help="one raw talking-head video in, one finished video out")
    p.add_argument("video")
    p.add_argument("--name", default="polish", help="name of the EDL (default: polish)")
    _add_finish_options(p)
    p.set_defaults(func=cmd_polish)

    p = sub.add_parser("clip", help="one long video in, several shorts out")
    p.add_argument("video")
    p.add_argument("-n", "--count", type=int, default=3, help="number of clips (default 3)")
    p.add_argument("--min", type=float, default=20.0, help="shortest clip in seconds (default 20)")
    p.add_argument("--max", type=float, default=60.0, help="longest clip in seconds (default 60)")
    p.add_argument("--llm", action="store_true", help="same as --ai: let DeepSeek choose and finish the clips")
    _add_finish_options(p)
    p.set_defaults(func=cmd_clip)

    p = sub.add_parser("style", help="analyse a reference short and write a style spec JSON")
    p.add_argument("reference")
    p.add_argument("-o", "--out", help="where to write the spec (default: <reference>.style.json)")
    p.add_argument("--max-seconds", type=float, default=90.0, help="analyse at most this much (default 90)")
    p.set_defaults(func=cmd_style)

    p = sub.add_parser("apply-style", help="apply a style spec to an existing EDL")
    p.add_argument("project")
    p.add_argument("edl")
    p.add_argument("style")
    p.add_argument("--music", help="music track to use if the reference has music")
    p.set_defaults(func=cmd_apply_style)

    p = sub.add_parser("check", help="check that a video is single-speaker, single-camera, 1080p max")
    p.add_argument("video")
    p.set_defaults(func=cmd_check)

    for name, func, help_ in (("transcribe", cmd_transcribe, "stage: transcribe with word timings"),
                              ("select", cmd_select, "stage: propose clips (built-in fallback selection)")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("video")
        p.add_argument("--project")
        p.add_argument("--model")
        p.add_argument("--language")
        p.add_argument("--force", action="store_true")
        if name == "transcribe":
            p.add_argument("--redo", action="store_true")
        else:
            p.add_argument("-n", "--count", type=int, default=3)
            p.add_argument("--min", type=float, default=20.0)
            p.add_argument("--max", type=float, default=60.0)
            p.add_argument("--llm", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("render", help="render an EDL (final file, preview, a range or one frame)")
    p.add_argument("project")
    p.add_argument("edl")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--start", type=float)
    p.add_argument("--end", type=float)
    p.add_argument("--frame", type=float, help="render a single frame at this output time")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("edls", help="list the EDLs in a project")
    p.add_argument("project")
    p.set_defaults(func=cmd_edls)

    p = sub.add_parser("ui", help="open the editor in your browser (runs on this computer only)")
    p.add_argument("video", nargs="?", help="video to open straight away")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true", help="do not open a browser window")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("deepseek", help="connect DeepSeek, the model that chooses clips and plans edits")
    p.add_argument("action", choices=["status", "set-key", "remove-key", "test"], nargs="?", default="status")
    p.set_defaults(func=cmd_deepseek)

    p = sub.add_parser("direct", help="have the AI editor change an EDL from a plain instruction")
    p.add_argument("project")
    p.add_argument("edl")
    p.add_argument("instruction", nargs="*", help="what to change; leave out for the standard finish")
    p.set_defaults(func=cmd_direct)

    p = sub.add_parser("account", help="list the editor's accounts, reset a password or remove an account")
    p.add_argument("action", choices=["list", "reset", "remove"])
    p.add_argument("email", nargs="?")
    p.set_defaults(func=cmd_account)

    sub.add_parser("templates", help="list caption and overlay templates").set_defaults(func=cmd_templates)
    sub.add_parser("doctor", help="check that everything clipper needs is installed").set_defaults(func=cmd_doctor)
    sub.add_parser("mcp", help="run the MCP server on stdio (for Claude Code and other agents)").set_defaults(func=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except FootageRejected as exc:
        print(str(exc), file=sys.stderr)
        print("\nNothing was changed. To continue anyway, add --force.", file=sys.stderr)
        sys.exit(2)
    except ClipperError as exc:
        print(f"clipper: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
