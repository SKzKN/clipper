"""The local UI server's API, driven the way the page drives it. Needs the synthetic footage."""
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from clipper import accounts, pipeline, ui_server

FIX = Path(__file__).parent / "fixture"
pytestmark = pytest.mark.skipif(not (FIX / "talk.mp4").exists(), reason="run tests/make_fixture.py first")
starlette_testclient = pytest.importorskip("starlette.testclient")


ACCOUNT = json.loads((Path(__file__).parent / "dev_account.json").read_text())
SIGNUP = {k: ACCOUNT[k] for k in ("name", "email", "password")}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """A signed-in client. Accounts and video lists go to a temporary data folder, not the real one."""
    old = os.environ.get("CLIPPER_HOME")
    os.environ["CLIPPER_HOME"] = str(tmp_path_factory.mktemp("home"))
    key = os.environ.pop("DEEPSEEK_API_KEY", None)   # these tests run with the AI editor not connected
    c = starlette_testclient.TestClient(ui_server.create_app(), base_url="http://127.0.0.1")
    assert c.post("/api/auth/signup", headers={"X-Clipper-Token": ui_server.TOKEN}, json=SIGNUP).status_code == 200
    yield c
    if key:
        os.environ["DEEPSEEK_API_KEY"] = key
    if old is None:
        del os.environ["CLIPPER_HOME"]
    else:
        os.environ["CLIPPER_HOME"] = old


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    work = tmp_path_factory.mktemp("ui")
    video = work / "talk.mp4"
    shutil.copy(FIX / "talk.mp4", video)
    proj = pipeline.ingest(str(video))
    pipeline.ensure_transcript(proj, model="base")
    return str(proj.dir)


AUTH = {"X-Clipper-Token": ui_server.TOKEN}


def wait(client, started):
    if "job" not in started:
        return started
    for _ in range(600):
        job = client.get(f"/api/job/{started['job']}", headers=AUTH).json()
        if job["state"] != "running":
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_page_is_served_and_api_needs_the_token(client):
    page = client.get("/")
    assert page.status_code == 200 and ui_server.TOKEN in page.text
    assert client.get("/api/state").status_code == 403
    assert client.get("/api/state", headers={"X-Clipper-Token": "nope"}).status_code == 403
    assert client.get("/api/state", headers={**AUTH, "host": "evil.example"}).status_code == 403
    state = client.get("/api/state", headers=AUTH).json()
    assert len(state["overlays"]) >= 6 and "prepare" in state["overlay_js"]
    assert state["user"]["email"] == ACCOUNT["email"]
    assert client.get("/static/app.js").status_code == 200


def test_everything_but_the_landing_page_needs_an_account(client, project):
    anon = starlette_testclient.TestClient(ui_server.create_app(), base_url="http://127.0.0.1")
    state = anon.get("/api/state", headers=AUTH).json()
    assert state["user"] is None and state["recent"] == [] and state["has_accounts"]
    assert anon.get("/api/project", headers=AUTH, params={"project": project}).status_code == 401
    assert anon.post("/api/open", headers=AUTH, json={"path": str(FIX / "talk.mp4")}).status_code == 401
    assert anon.get("/api/media", params={"project": project, "t": ui_server.TOKEN}).status_code == 401
    assert anon.get("/tpl/title_card/plain-top/template.html", params={"t": ui_server.TOKEN}).status_code == 401

    # sign-up rules, then sign in, out and in again
    post = lambda path, **body: anon.post(path, headers=AUTH, json=body)
    assert "already" in post("/api/auth/signup", **SIGNUP).json()["error"]
    assert post("/api/auth/signup", name="A", email="a@clipper.test", password="short").status_code == 400
    assert post("/api/auth/signup", name="A", email="not-an-email", password="long-enough-1").status_code == 400
    wrong = post("/api/auth/signin", email=ACCOUNT["email"], password="not-the-password")
    unknown = post("/api/auth/signin", email="nobody@clipper.test", password="not-the-password")
    assert wrong.status_code == unknown.status_code == 400 and wrong.json() == unknown.json()
    assert post("/api/auth/signin", email=ACCOUNT["email"].upper(), password=ACCOUNT["password"]).status_code == 200
    assert anon.get("/api/project", headers=AUTH, params={"project": project}).status_code == 200
    assert post("/api/auth/signout").json() == {"user": None}
    assert anon.get("/api/project", headers=AUTH, params={"project": project}).status_code == 401

    # a second account has its own, empty list of videos; nothing secret is written to disk
    assert post("/api/auth/signup", name="Second", email="second@clipper.test", password="another-test-pass").status_code == 200
    assert anon.get("/api/state", headers=AUTH).json()["recent"] == []
    stored = (accounts.data_dir() / "accounts.json").read_text()
    assert ACCOUNT["password"] not in stored and "another-test-pass" not in stored
    assert all(cookie.value not in stored for cookie in anon.cookies.jar)


def test_rejected_footage_reports_reasons(client):
    job = wait(client, client.post("/api/open", headers=AUTH, json={"path": str(FIX / "uhd.mp4")}).json())
    assert job["state"] == "rejected" and "1080p" in job["result"]["reasons"][0]
    assert client.post("/api/open", headers=AUTH, json={"path": "/nope/missing.mp4"}).status_code == 400


def test_edit_undo_and_payload(client, project):
    opened = client.post("/api/open", headers=AUTH, json={"path": project}).json()
    assert opened["project"]["has_transcript"]
    words = client.get("/api/transcript", headers=AUTH, params={"project": project}).json()
    assert len(words["words"]) > 30 and words["fillers"]

    made = client.post("/api/create", headers=AUTH, json={"project": project, "kind": "polish", "name": "ui"}).json()
    assert made["created"] == ["ui"]
    edl = client.get("/api/edl", headers=AUTH, params={"project": project, "edl": "ui"}).json()
    assert edl["captions"]["enabled"] and edl["captions"]["groups"] and edl["geometry"]["out_w"] == 1080
    assert all(edl["word_tl"][i] is None for i in words["fillers"])          # the recipe cut the fillers
    kept = [i for i, tl in enumerate(edl["word_tl"]) if tl]

    def op(name, args):
        res = client.post("/api/op", headers=AUTH, json={"project": project, "edl": "ui", "op": name, "args": args})
        assert res.status_code == 200, res.text
        return res.json()

    before = edl["duration"]
    after = op("cut_words", {"first": kept[5], "last": kept[7]})["edl"]
    assert after["duration"] < before and after["word_tl"][kept[6]] is None and after["can_undo"]
    restored = op("restore_words", {"first": kept[5], "last": kept[7]})["edl"]
    assert restored["word_tl"][kept[6]] is not None

    op("add_overlay", {"template": "keyword_callout/plain-pill", "start": 2.0, "params": {"text": "hi"}})
    with_overlay = op("add_zoom", {"start": 1.0, "end": 2.5, "scale": 1.3})["edl"]
    assert len(with_overlay["overlays"]) == 1 and any(z["scale"] == 1.3 for z in with_overlay["zooms"])

    undone = client.post("/api/undo", headers=AUTH, json={"project": project, "edl": "ui"}).json()["edl"]
    assert not any(z["scale"] == 1.3 for z in undone["zooms"]) and undone["can_redo"]
    redone = client.post("/api/undo", headers=AUTH, json={"project": project, "edl": "ui", "redo": True}).json()["edl"]
    assert any(z["scale"] == 1.3 for z in redone["zooms"])

    bad = client.post("/api/op", headers=AUTH, json={"project": project, "edl": "ui", "op": "set_transition",
                                                    "args": {"at": 1.0, "type": "sparkle"}})
    assert bad.status_code == 400 and "Unknown transition" in bad.json()["error"]
    assert client.post("/api/op", headers=AUTH, json={"project": project, "edl": "ui", "op": "rm_rf", "args": {}}).status_code == 400


def test_media_render_and_template_files(client, project):
    client.post("/api/create", headers=AUTH, json={"project": project, "kind": "polish", "name": "media"})
    src = client.get("/api/media", params={"project": project, "kind": "source", "t": ui_server.TOKEN},
                     headers={"Range": "bytes=0-99"})
    assert src.status_code == 206 and len(src.content) == 100                 # seeking needs range requests
    assert client.get("/api/media", params={"project": project, "kind": "source"}).status_code == 403

    job = wait(client, client.post("/api/render", headers=AUTH, json={"project": project, "edl": "media", "quality": "preview"}).json())
    assert job["state"] == "done" and job["result"]["preview"]["exists"] and not job["result"]["preview"]["stale"]
    pv = client.get("/api/media", params={"project": project, "kind": "preview", "edl": "media", "t": ui_server.TOKEN})
    assert pv.status_code == 200 and len(pv.content) > 10_000

    frame = client.get("/api/frame", params={"project": project, "edl": "media", "at": 1.0, "t": ui_server.TOKEN})
    assert frame.status_code == 200 and frame.content[:4] == b"\x89PNG"

    tpl = client.get("/tpl/title_card/plain-top/template.html", params={"t": ui_server.TOKEN})
    assert tpl.status_code == 200 and "data-param" in tpl.text
    assert client.get("/tpl/title_card/plain-top/../../../../cli.py", params={"t": ui_server.TOKEN}).status_code in (403, 404)
    assert client.get("/tpl/title_card/plain-top/template.html").status_code == 403


def test_pricing_usage_and_ai_state(client, project, tmp_path):
    state = client.get("/api/state", headers=AUTH).json()
    assert [p["id"] for p in state["pricing"]["plans"]] == ["free", "starter", "pro", "business"]
    assert all(len(r["values"]) == 4 for g in state["pricing"]["compare"] for r in g["rows"])
    assert state["ai"] == {"provider": "DeepSeek", "ready": False, "model": "deepseek-flash"}
    assert state["user"]["plan"] == "free" and state["usage"]["limit"] == 60

    # asking about a paid plan is remembered and changes nothing else
    asked = client.post("/api/plan/interest", headers=AUTH, json={"plan": "pro"}).json()["user"]
    assert asked["wants_plan"] == "pro" and asked["plan"] == "free"
    assert client.post("/api/plan/interest", headers=AUTH, json={"plan": "platinum"}).status_code == 400

    # a video opened for the first time counts towards the month's minutes; reopening it does not
    before = state["usage"]["minutes"]
    video = tmp_path / "fresh.mp4"
    shutil.copy(FIX / "talk.mp4", video)
    for _ in range(2):
        assert wait(client, client.post("/api/open", headers=AUTH, json={"path": str(video)}).json()).get("state") != "failed"
    assert client.get("/api/state", headers=AUTH).json()["usage"]["minutes"] == pytest.approx(before + 0.5, abs=0.11)

    # without DeepSeek the AI editor says so, and creating an edit uses the built-in recipe at once (no job)
    refused = client.post("/api/direct", headers=AUTH, json={"project": project, "edl": "polish", "instruction": "tighter"})
    assert refused.status_code == 400 and "not connected" in refused.json()["error"]
    made = client.post("/api/create", headers=AUTH, json={"project": project, "kind": "polish", "name": "plain"}).json()
    assert made["created"] == ["plain"] and made["note"] is None
