"""The DeepSeek-backed AI editor, against a stand-in server that speaks the Chat Completion format.

No real key and no network: these check what clipper sends, how it applies what comes back, and that it
falls back cleanly when the API refuses. They cannot check how good DeepSeek's choices are.
"""
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from clipper import deepseek, director, pipeline
from clipper.edl import Timeline
from clipper.ffmpeg import ClipperError

FIX = Path(__file__).parent / "fixture"
pytestmark = pytest.mark.skipif(not (FIX / "talk.mp4").exists(), reason="run tests/make_fixture.py first")


class FakeDeepSeek:
    """Answers each request with the next scripted reply; remembers what it was sent."""

    def __init__(self):
        self.replies, self.requests = [], []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                status, payload = fake.replies.pop(0) if fake.replies else (500, {"error": {"message": "no reply scripted"}})
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def say(self, content=None, tool_calls=None, status=200, finish="stop"):
        if status != 200:
            self.replies.append((status, {"error": {"message": content}}))
            return
        message = {"role": "assistant", "content": content, "reasoning_content": "thinking it over"}
        if tool_calls:
            message["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                                      "function": {"name": name, "arguments": json.dumps(args)}}
                                     for i, (name, args) in enumerate(tool_calls)]
        self.replies.append((200, {"model": "deepseek-flash", "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else finish}]}))


@pytest.fixture()
def api(monkeypatch, tmp_path):
    fake = FakeDeepSeek()
    monkeypatch.setenv("CLIPPER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEEPSEEK_BASE_URL", fake.url)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.setattr(deepseek.time, "sleep", lambda s: None)
    yield fake
    fake.server.shutdown()


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    work = tmp_path_factory.mktemp("ai")
    shutil.copy(FIX / "talk.mp4", work / "talk.mp4")
    proj = pipeline.ingest(str(work / "talk.mp4"))
    pipeline.ensure_transcript(proj, model="base")
    return proj


def test_key_comes_from_the_environment_or_a_private_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIPPER_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert not deepseek.is_ready() and deepseek.status()["key_from"] is None
    with pytest.raises(ClipperError, match="not connected"):
        deepseek.chat([{"role": "user", "content": "hi"}])
    deepseek.set_key("  file-key-not-real \n")
    assert deepseek.api_key() == "file-key-not-real" and deepseek.status()["key_from"] == "file"
    assert oct(deepseek.key_file().stat().st_mode)[-3:] == "600"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key-not-real")
    assert deepseek.api_key() == "env-key-not-real"


def test_request_follows_the_documented_format_and_errors_are_plain(api):
    api.say("hello")
    assert deepseek.chat([{"role": "user", "content": "hi"}])["content"] == "hello"
    sent = api.requests[0]
    assert sent["path"] == "/chat/completions" and sent["auth"] == "Bearer test-key-not-real"
    assert sent["body"]["model"] == "deepseek-flash" and sent["body"]["thinking"] == {"type": "enabled"}
    assert sent["body"]["stream"] is False and "response_format" not in sent["body"]

    api.say("bad key", status=401)
    with pytest.raises(ClipperError, match="refused the API key"):
        deepseek.chat([{"role": "user", "content": "hi"}])
    api.say("no money", status=402)
    with pytest.raises(ClipperError, match="run out of balance"):
        deepseek.chat([{"role": "user", "content": "hi"}])
    api.say("busy", status=503)
    api.say("recovered")                                   # a busy server is retried
    assert deepseek.chat([{"role": "user", "content": "hi"}])["content"] == "recovered"
    api.say("half an ans", finish="length")
    with pytest.raises(ClipperError, match="cut off"):
        deepseek.chat([{"role": "user", "content": "hi"}])
    api.say("   ")                                          # JSON Output can come back blank; retried once
    api.say('{"ok": true}')
    assert deepseek.chat_json([{"role": "user", "content": "json please"}]) == {"ok": True}
    assert api.requests[-1]["body"]["response_format"] == {"type": "json_object"}


def test_deepseek_chooses_clips_and_bad_picks_are_dropped(api, project):
    from clipper.select import select_clips, sentence_list

    words = project.words()
    sents = sentence_list(words)
    api.say(json.dumps({"clips": [
        {"first_sentence": 1, "last_sentence": len(sents) - 1, "title": "The mistakes", "reason": "Complete thought."},
        {"first_sentence": 0, "last_sentence": 999, "title": "Out of range", "reason": "x"},
        {"first_sentence": 0, "last_sentence": 0, "title": "Far too short", "reason": "x"}]}))
    result = select_clips(words, count=2, min_len=12, max_len=40)
    assert result["method"] == "deepseek" and [c["title"] for c in result["clips"]] == ["The mistakes"]
    assert result["clips"][0]["start"] == pytest.approx(sents[1]["start"] - 0.15, abs=0.01)
    assert "json" in api.requests[-1]["body"]["messages"][0]["content"]

    api.say("no money", status=402)                         # the API refuses: the heuristic takes over and says why
    fallback = select_clips(words, count=1, min_len=5, max_len=25)
    assert fallback["method"] == "heuristic" and "balance" in fallback["note"] and fallback["clips"]


def test_the_ai_editor_edits_through_tools(api, project):
    words = project.words()
    doc = project.new_edl("ai-test", "polish", None, "9:16")
    full = Timeline(doc).duration
    last = len(words) - 1
    api.say(tool_calls=[
        ("remove_fillers", {}),
        ("cut_words", {"first": 20, "last": 23, "reason": "a tangent"}),
        ("zoom", {"first": 5, "last": 9, "scale": 9}),                       # scale is clamped
        ("graphic", {"kind": "title_card", "text": "Three editing mistakes"}),
        ("graphic", {"kind": "keyword_callout", "text": "No time", "at_word": 12}),
        ("zoom", {"first": 21, "last": 22}),                                 # those words were just cut
        ("cut_words", {"first": 5}),                                         # missing argument
        ("launch_rocket", {}),
    ])
    api.say(tool_calls=[("read_edit", {}), ("captions", {"template": "highlight", "max_words": 3})])
    api.say("I tightened the take, punched in on the first point and added a title and a callout.")
    result = director.direct(project, doc, "make it punchy")

    assert result["summary"].startswith("I tightened")
    assert Timeline(doc).duration < full
    assert len(doc["zooms"]) == 1 and doc["zooms"][0]["scale"] == 1.5 and doc["zooms"][0]["anchor"] == "source"
    assert [o["template"].split("/")[0] for o in doc["overlays"]] == ["title_card", "keyword_callout"]
    assert doc["overlays"][0]["params"]["title"] == "Three editing mistakes" and doc["overlays"][0]["start"] == 0
    assert doc["captions"]["template"] == "highlight" and doc["captions"]["overrides"]["max_words"] == 3
    ok = [s["ok"] for s in result["steps"]]
    assert ok == [True, True, True, True, True, False, False, False, True]   # read_edit is not listed as a step
    note = doc["notes"][-1]
    assert note["kind"] == "ai" and note["request"] == "make it punchy" and len(note["steps"]) == 6

    first, second, third = (r["body"] for r in api.requests)
    assert "Request: make it punchy" in first["messages"][1]["content"] and "<transcript>" in first["messages"][1]["content"]
    assert {t["function"]["name"] for t in first["tools"]} >= {"cut_words", "zoom", "graphic", "captions", "read_edit"}
    tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 8 and tool_msgs[0]["tool_call_id"] == "call_0"
    assert "Problem" in tool_msgs[5]["content"] and "Problem" in tool_msgs[6]["content"] and "no tool" in tool_msgs[7]["content"]
    assistant = next(m for m in second["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "thinking it over" and "_model" not in assistant
    assert any("<transcript>" not in m["content"] and "[" in m["content"] for m in third["messages"] if m["role"] == "tool")


def test_polish_uses_the_ai_and_falls_back_to_the_recipe(api, project):
    api.say(tool_calls=[("remove_fillers", {}), ("auto_zoom", {"per_minute": 4})])
    api.say("Done: fillers out, zooms in.")
    doc, report = pipeline.make_polish(project, "ai-polish")
    assert report["ai"]["summary"].startswith("Done") and doc["captions"]["template"] == "bold-pop" and doc["zooms"]

    api.say("no money", status=402)
    doc, report = pipeline.make_polish(project, "ai-fallback")
    assert "balance" in report["note"] and "fillers" in report and doc["zooms"] and not any(n["kind"] == "ai" for n in doc["notes"])

    assert pipeline.make_polish(project, "no-ai", ai=False)[1].get("ai") is None and len(api.requests) == 3


def test_the_editor_page_runs_an_ai_pass_as_one_undo_step(api, project):
    import time

    starlette_testclient = pytest.importorskip("starlette.testclient")
    from clipper import ui_server

    account = json.loads((Path(__file__).parent / "dev_account.json").read_text())
    auth = {"X-Clipper-Token": ui_server.TOKEN}
    client = starlette_testclient.TestClient(ui_server.create_app(), base_url="http://127.0.0.1")
    client.post("/api/auth/signup", headers=auth, json={k: account[k] for k in ("name", "email", "password")})
    assert client.get("/api/state", headers=auth).json()["ai"]["ready"] is True
    proj = str(project.dir)

    def run(path, body):
        started = client.post(path, headers=auth, json=body).json()
        for _ in range(300):
            job = client.get(f"/api/job/{started['job']}", headers=auth).json()
            if job["state"] != "running":
                return job
            time.sleep(0.05)
        raise AssertionError("job did not finish")

    api.say(tool_calls=[("remove_fillers", {})])
    api.say("Fillers are out.")
    made = run("/api/create", {"project": proj, "kind": "polish", "name": "page-ai"})
    assert made["state"] == "done" and made["result"]["created"] == ["page-ai"]

    api.say(tool_calls=[("zoom", {"first": 3, "last": 6}), ("graphic", {"kind": "title_card", "text": "Editing mistakes"})])
    api.say("Added a zoom and a title.")
    job = run("/api/direct", {"project": proj, "edl": "page-ai", "instruction": "add a zoom and a title"})
    assert job["state"] == "done" and job["result"]["summary"] == "Added a zoom and a title."
    edl = job["result"]["edl"]
    assert len(edl["zooms"]) == 1 and len(edl["overlays"]) == 1 and edl["can_undo"]
    assert edl["notes"][-1]["steps"] == ["Zoomed on words 3-6", "Added a title card: Editing mistakes"]
    undone = client.post("/api/undo", headers=auth, json={"project": proj, "edl": "page-ai"}).json()["edl"]
    assert undone["zooms"] == [] and undone["overlays"] == []          # the whole pass is one undo step

    api.say("no money", status=402)                                     # the API refuses mid-session: nothing changes
    failed = run("/api/direct", {"project": proj, "edl": "page-ai", "instruction": "more"})
    assert failed["state"] == "failed" and "balance" in failed["error"]
    assert client.get("/api/edl", headers=auth, params={"project": proj, "edl": "page-ai"}).json()["zooms"] == []


def test_the_ai_editor_can_do_every_kind_of_edit(api, project):
    doc = project.new_edl("ai-all", "polish", None, "9:16")
    music, broll = str(FIX / "music.wav"), str(FIX / "broll.mp4")
    api.say(tool_calls=[
        ("captions", {"template": "highlight"}),
        ("caption_style", {"size": 6, "height": 80, "color": "#ffee00", "uppercase": False}),
        ("cut_words", {"first": 20, "last": 23}),
        ("transition", {"at_word": 24, "type": "crossfade"}),
        ("zoom", {"first": 5, "last": 9, "ease": 0.3}),
        ("sound", {"sound": "pop", "at_word": 5}),
        ("framing", {"x": 0.4, "y": 0.35}),
        ("loudness", {"lufs": -16}),
        ("music", {"path": music, "level_db": -24}),
        ("broll", {"path": broll, "first": 30, "last": 33}),
        ("music", {"path": "/etc/hosts"}),                       # a file the user never gave: refused
        ("graphic", {"kind": "lower_third", "text": "Sam Example", "secondary": "Editor", "at_word": 2}),
    ])
    zoom_id = "z1"
    api.say(tool_calls=[("edit_item", {"id": zoom_id, "scale": 1.3}), ("edit_item", {"id": "o1", "text": "Sam E."}),
                        ("edit_item", {"id": "nope", "scale": 2}), ("trim", {"first": 2, "last": 40})])
    api.say("Everything is in.")
    result = director.direct(project, doc, "do everything", files=[music, broll])

    cap = doc["captions"]
    assert cap["template"] == "highlight" and cap["overrides"]["size"] == 0.06 and cap["overrides"]["position"]["y"] == 0.8
    assert cap["overrides"]["color"] == "#ffee00" and cap["overrides"]["uppercase"] is False
    assert any((s.get("transition_in") or {}).get("type") == "crossfade" for s in doc["segments"])
    assert doc["zooms"][0]["ease"] == 0.3 and doc["zooms"][0]["scale"] == 1.3
    assert [s["sound"] for s in doc["sfx"]] == ["pop"]
    assert doc["framing"] == {"x": 0.4, "y": 0.35} and doc["output"]["loudness"] == -16
    assert doc["music"]["path"].endswith("music.wav") and doc["music"]["gain_db"] == -24 and doc["music"]["duck"]["enabled"]
    assert len(doc["broll"]) == 1 and doc["broll"][0]["path"].endswith("broll.mp4")
    lower = doc["overlays"][0]
    assert lower["template"].startswith("lower_third/") and lower["params"] == {"name": "Sam E.", "role": "Editor"}
    refused = [s for s in result["steps"] if not s["ok"]]
    assert len(refused) == 2 and "not a file the user attached" in refused[0]["result"] and "No zoom or graphic" in refused[1]["result"]
    assert Timeline(doc).duration < project.source["duration"] - 5       # the trim took the ends off
    first = api.requests[0]["body"]["messages"][1]["content"]
    assert "Files the user attached" in first and music in first and broll in first

    # a path typed into the request counts as given; anything else does not
    assert director._named_files(f"put {music} under it, and 'not/a/real/file.mp3' too") == [music]
    assert {t["function"]["name"] for t in director.tool_specs()} >= {
        "trim", "caption_style", "edit_item", "transition", "sound", "framing", "loudness", "music", "broll", "copy_style"}
