"""Drive the MCP server over stdio the way an agent would.

    python tests/mcp_smoke.py tests/fixture
"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

FIX = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixture").resolve()
VIDEO = str(FIX / "talk.mp4")


async def call(session, tool_name, **args):
    name = tool_name
    res = await session.call_tool(name, args)
    texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
    images = [c for c in res.content if getattr(c, "type", "") == "image"]
    if res.is_error if hasattr(res, "is_error") else res.isError:
        print(f"  {name}: ERROR {texts[0][:300] if texts else ''}")
        return None
    data = None
    if texts:
        try:
            data = json.loads(texts[0])
        except json.JSONDecodeError:
            data = texts[0]
    print(f"  {name}: ok" + (f" (+{len(images)} image, {len(images[0].data) // 1024} KB b64)" if images else ""))
    return data


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "clipper.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print(f"{len(tools)} tools:", ", ".join(t.name for t in tools))

            bad = await call(s, "ingest", video=str(FIX / "two_voices.mp4"))
            assert bad is None, "two-voice footage should be rejected"
            ing = await call(s, "ingest", video=VIDEO)
            proj = ing["project"]
            await call(s, "transcribe", project=proj, model="base")
            src = await call(s, "get_transcript", project=proj)
            print("    sentences:", len(src["sentences"]))

            # clip mode, agent-chosen range
            first, last = src["sentences"][1], src["sentences"][-2]
            await call(s, "create_edl", project=proj, name="agent-clip", mode="clip",
                       ranges=[[first["start"] - 0.1, last["end"] + 0.2]], title="Mistakes")
            await call(s, "remove_fillers", project=proj, edl="agent-clip")
            await call(s, "remove_pauses", project=proj, edl="agent-clip", max_pause=0.5, keep=0.2)
            tl = await call(s, "get_transcript", project=proj, edl="agent-clip", detail="words")
            words = tl["words"]
            await call(s, "set_caption_template", project=proj, edl="agent-clip", template="highlight")
            await call(s, "add_zoom", project=proj, edl="agent-clip", start=words[2]["start"], end=words[8]["end"], scale=1.2)
            await call(s, "auto_zoom", project=proj, edl="agent-clip", per_minute=6)
            info = await call(s, "get_edl", project=proj, edl="agent-clip")
            cut_at = info["cuts"][0]["time"]
            await call(s, "set_transition", project=proj, edl="agent-clip", at=cut_at, type="dip_black", duration=0.4)
            await call(s, "add_overlay", project=proj, edl="agent-clip", template="title_card/plain-center", start=0,
                       duration=1.6, params={"title": "3 editing mistakes", "subtitle": "and how to fix them"})
            await call(s, "add_overlay", project=proj, edl="agent-clip", template="lower_third/plain-bar", start=2.0,
                       params={"name": "Sam Example", "role": "Video editor"})
            await call(s, "add_overlay", project=proj, edl="agent-clip", template="keyword_callout/plain-underline",
                       start=words[10]["start"], params={"text": "captions"})
            await call(s, "add_broll", project=proj, edl="agent-clip", path=str(FIX / "broll.mp4"), start=8.0, end=10.0)
            await call(s, "set_music", project=proj, edl="agent-clip", path=str(FIX / "music.wav"), gain_db=-18)
            await call(s, "add_sfx_on_cuts", project=proj, edl="agent-clip", which="all")
            await call(s, "add_sfx", project=proj, edl="agent-clip", at=1.0, sound="pop")
            err = await call(s, "add_overlay", project=proj, edl="agent-clip", template="nope/none", start=0)
            assert err is None
            await call(s, "render_frame", project=proj, edl="agent-clip", at=0.8)
            prev = await call(s, "render_preview", project=proj, edl="agent-clip")
            print("    preview:", prev["duration"], "s in", prev["render_seconds"], "s;", prev["warnings"])
            exp = await call(s, "export", project=proj, edl="agent-clip")
            print("    export:", exp["path"], exp["render_seconds"], "s")

            # reference style -> second EDL
            style = await call(s, "analyze_style", reference=exp["path"])
            await call(s, "create_edl", project=proj, name="styled", mode="polish")
            await call(s, "remove_fillers", project=proj, edl="styled")
            await call(s, "remove_pauses", project=proj, edl="styled")
            rep = await call(s, "apply_style", project=proj, edl="styled", style=style["style_spec"],
                             music=str(FIX / "music.wav"))
            print(json.dumps(rep["report"], indent=1))
            job = await call(s, "export", project=proj, edl="styled", wait=False)
            for _ in range(120):
                st = await call(s, "job_status", job_id=job["job_id"])
                if st["state"] != "running":
                    break
                await asyncio.sleep(1)
            print("    job:", st["state"], st.get("error", ""))
            await call(s, "list_edls", project=proj)
            await call(s, "list_templates")


asyncio.run(main())
