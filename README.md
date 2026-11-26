# clipper

Edits single-speaker talking-head video. Two modes, one EDL format, one renderer:

- **polish** – one raw talking-head video in, one finished video out.
- **clip** – one long video in, several shorts out.

Every stage and every edit operation is also an MCP tool, so an agent (Claude Code first) can do the
planning: read the transcript, decide what to cut, where to punch in, what the title says. The built-in
`select` stage remains for runs with no agent.

## Setup

```bash
./setup.sh
```

This creates `.venv` with Python 3.12 and installs everything, including a bundled ffmpeg and the Chromium
used to draw overlays. Run it again if you move the folder. `clipper doctor` shows what is installed.

The first transcription downloads a Whisper model (about 480 MB for the default `small`), and the first
footage check downloads a 28 MB speaker model. Both are cached.

## The AI editor (DeepSeek)

DeepSeek is the model that does the planning: it chooses the clips, and it edits by calling the same edit
operations the page and the MCP tools use (cut, zoom, captions, graphics, transitions, sounds).

```bash
clipper deepseek set-key       # asks for the key with hidden input; or set DEEPSEEK_API_KEY
clipper deepseek test          # one small call to confirm the key works
clipper deepseek status
clipper direct talk.clipper polish "cut the tangent about pricing and add a title"
```

Once a key is set, `clipper polish`, `clipper clip` and the editor's **Create** use DeepSeek by default
(`--no-ai` or the switch on the page turns it off), and the editor gets an **AI** panel: type what to change,
or press **Finish it for me**. One Undo takes a whole AI pass back. If DeepSeek refuses (no balance, no
network), clipper says so and uses the built-in recipe instead.

- **What is sent:** the transcript text and a short description of the edit. Video and audio files are never sent.
- **What it cannot do:** see or hear the footage. Transcription stays local (Whisper), because clipper needs a
  time for every word and DeepSeek's speech-to-text returns text only, 120 seconds at a time. The footage
  check and face framing are local too.
- **Model:** `deepseek-flash` by default; set `CLIPPER_DEEPSEEK_MODEL=deepseek-v4-pro` for the larger one.
  The key is kept in `~/.clipper/deepseek.key`, readable only by you, and is never sent to the page.
- **Not tested against the real API.** `tests/test_ai.py` runs it against a stand-in server that follows
  DeepSeek's documented request and reply format. Run `clipper deepseek test` after setting the key.

## Pricing

The pricing page (Monthly / Yearly, four plans, a comparison table) reads `src/clipper/plans.py`. The prices,
minutes and the split of features between plans in that file are **placeholders**: edit them there. Nothing
is charged, because payments are not connected. Paid plans show "Tell me when it opens", which only records
the interest on the account. Minutes of video opened per month are counted per account and shown, but not
enforced, so every account can use everything.

`PROMO` in the same file is a dated offer, currently **Black Friday: 40% off Starter and Pro, 23 to 30 November
2026** (also placeholders). Inside its dates the landing page's top strip and the pricing page show it, with
the old price struck through; after the end date it disappears by itself. Set `PROMO = None` for no offer.
A plan request made during the offer is stored with the offer's id, so you can honour the price later.

## The editor (front end)

```bash
clipper ui                 # opens http://127.0.0.1:8765 in your browser
clipper ui talk.mp4        # opens straight into that video
```

A local page served from your own machine; nothing is uploaded. Signed out, it shows the landing page; the
editor itself needs an account. The flow is: sign up → choose a video → footage check → transcribe →
**Polish the whole video** or **Find clips** → the editor.

**Accounts** live on this computer only (`~/.clipper/accounts.json`, or `$CLIPPER_HOME`): no email is sent and
there is no server to sign up with. Passwords are stored as salted scrypt hashes and sessions last 30 days.
Each account has its own list of videos. There is no reset email, so passwords are reset from a terminal:

```bash
clipper account list
clipper account reset you@example.com      # asks for the new password
clipper account remove you@example.com
```

The landing page lists the product line-up. The ones marked **Soon** (social scheduler, export to XML, team
workspace, thumbnails, speech enhance, dubbing) are not built.

- **Live preview** plays the source through the edit without rendering: cuts, crop, punch-in zooms,
  captions, graphics and b-roll, all using the renderer's own numbers. Transitions, music and sound
  effects are in **Render preview** (a fast half-size render) and the export.
- **The transcript is the edit.** Select words and press Delete to cut them; struck-through words are out,
  select them to restore. The selection toolbar also adds a zoom or a keyword callout on those words.
- **Panels:** AI (tell DeepSeek what to change), Tighten (fillers, pauses, trim), Captions, Zoom, Graphics (overlays and b-roll), Audio (music,
  ducking, sound effects), Transitions, Style (analyse a reference, apply, read the report), Output (shape,
  framing, exact frame check, export).
- **Timeline:** waveform of what is kept, cut and transition markers, zoom / graphic / b-roll / sound tracks;
  click to seek, click an item to select it. Undo and redo cover every edit (⌘Z, ⇧⌘Z; Space plays).

The page talks to a small server (`ui_server.py`) that calls the same functions as the CLI and the MCP
server, so an edit made in the UI is an ordinary EDL file an agent can pick up, and the other way round.
It listens on 127.0.0.1 only; every request needs a per-run token, and everything except the landing page
and sign-up needs a signed-in session.

## Command line

```bash
clipper polish talk.mp4                      # check, transcribe, tighten, caption, zoom, export
clipper polish talk.mp4 --preview            # same, but a fast half-size render
clipper polish talk.mp4 --style ref.style.json --music bed.mp3
clipper clip long.mp4 -n 5 --min 20 --max 60 # several shorts (DeepSeek chooses them once a key is set)
clipper style reference-short.mp4            # writes reference-short.style.json
clipper apply-style talk.clipper polish reference-short.style.json --music bed.mp3
clipper render talk.clipper polish --frame 3.2   # one frame, to check a caption or overlay
clipper check talk.mp4                       # only run the footage check
clipper templates                            # list caption and overlay templates
```

Results are printed as JSON; progress goes to stderr. Everything about a video lives in a project folder
next to it, `<video name>.clipper/` (transcript, EDLs, renders, cache).

## Agent interface (MCP)

```bash
claude mcp add clipper -- /absolute/path/to/clipper/.venv/bin/clipper mcp
```

(or open Claude Code in this folder: `.mcp.json` is already here.) The server tells the agent how to work;
in short: `ingest` → `transcribe` → `get_transcript` → `create_edl` → edit tools → `render_preview` /
`render_frame` (both return images the agent can look at) → `export`.

| Group | Tools |
|---|---|
| Stages | `check_footage`, `ingest`, `transcribe`, `get_transcript`, `correct_transcript`, `select_clips` (fallback), `create_edl`, `auto_polish`, `list_edls`, `get_edl`, `duplicate_edl` |
| Structure | `trim`, `cut`, `set_segments`, `remove_fillers`, `remove_pauses` |
| Look | `set_caption_template`, `add_zoom`, `auto_zoom`, `set_transition`, `add_overlay`, `add_broll`, `set_output`, `set_framing` |
| Sound | `set_music` (add music, with ducking), `add_sfx`, `add_sfx_on_cuts` |
| Housekeeping | `remove_item`, `clear_items`, `list_templates` |
| Style | `analyze_style`, `apply_style` |
| Render | `render_preview`, `render_frame`, `export`, `job_status` |

All edit tools take seconds on the EDL's **current output timeline**, the same times
`get_transcript(edl=...)` returns. Items added with `anchor="content"` are stored against the source
footage, so a zoom on a sentence stays on that sentence when fillers are cut afterwards.

`tests/mcp_smoke.py` drives the whole server the way an agent would.

## The EDL

One JSON file per output video, in `<project>/edls/`. Clip mode writes several; polish mode writes one.

```jsonc
{
  "version": 1, "name": "polish", "mode": "polish",
  "source":   { "path": "...", "duration": 612.4, "width": 1920, "height": 1080, "fps": 30 },
  "output":   { "aspect": "9:16", "width": 1080, "height": 1920, "fps": 30, "loudness": -14 },
  "framing":  { "x": 0.52, "y": 0.38 },          // the speaker's face; crop and zooms keep it in place
  "segments": [                                   // source ranges, played in this order
    { "id": "s001", "start": 3.2, "end": 9.87 },
    { "id": "s002", "start": 10.4, "end": 31.0, "transition_in": { "type": "crossfade", "duration": 0.3 } }
  ],
  "captions": { "enabled": true, "template": "bold-pop", "overrides": { "max_words": 2 } },
  "zooms":    [ { "id": "z1", "start": 12.1, "end": 15.0, "anchor": "source", "scale": 1.15, "ease": 0 } ],
  "overlays": [ { "id": "o1", "template": "title_card/plain-top", "start": 0, "duration": 3,
                  "anchor": "timeline", "params": { "title": "3 editing mistakes" } } ],
  "broll":    [ { "id": "b1", "path": "...", "start": 40.0, "end": 43.5, "anchor": "source", "source_in": 0, "fit": "cover" } ],
  "music":    { "path": "...", "gain_db": -20, "offset": 0, "fade_in": 0.5, "fade_out": 1.5,
                "duck": { "enabled": true, "amount_db": -10, "attack": 0.08, "release": 0.5 } },
  "sfx":      [ { "id": "x1", "sound": "whoosh", "at": 10.4, "anchor": "source", "gain_db": -10 } ]
}
```

`anchor: "source"` means the times are source seconds (the item follows the footage); `"timeline"` means
output seconds (a title card at 0).

## Finishing layer

- **Punch-in zooms** – `add_zoom`, or `auto_zoom` which picks the most emphatic sentences (loud words,
  numbers, strong words). `ease: 0` is a hard punch-in cut; above 0 it ramps. Frame-exact.
- **Transitions** – on the join between two segments: crossfade, dip_black, flash_white, slide_*, wipe_*,
  zoom_in, blur. Picture only; the sound stays a clean cut.
- **Music** – one track under the whole video, looped if short, ducked automatically while the speaker talks.
- **SFX** – built-in whoosh, pop, click, thud (synthesised, nothing to license) or your own files;
  `add_sfx_on_cuts` places them on transitions, zooms or every cut.
- **Overlays** – HTML templates rendered to transparent frames: title card, lower third, keyword callout.
  Two plain examples of each are included. See `src/clipper/templates/README.md` for how to design your own.
- **B-roll** – your own clip or image over a time range; the speaker's audio continues. No generation, no
  stock search.

## Reference style

`clipper style <reference-video>` measures a reference short and writes a style spec:

| Measured | How |
|---|---|
| Caption position, size, case, weight, fill / outline / box colours | text recognition on sampled frames |
| Caption animation (highlight, word-by-word, pop, none) and words per caption | how the text changes frame to frame |
| Average shot length, punch-in frequency and strength, slow zooms | cut detection, then matching the picture before and after each cut |
| Transition types (cut, crossfade, dip to black, flash, motion) | how the picture changes across each transition |
| Music bed present and how far under the voice | tonality and beat in the gaps between phrases |
| Sound effects on cuts | noise bursts landing on cuts more often than chance |

`apply_style` maps the spec onto the closest caption template (then sets its position, size, colours and
words per caption), zoom frequency and strength, transitions, music level and sounds, and returns three
lists: **matched**, **approximated**, **unmatched**. The font family is always reported as unmatched (it
is not identified), as are b-roll and music when you have not supplied any.

The analysis is measurement, not understanding. It is reliable on clean references and over-counts
transitions and zooms on references that are heavy with overlays and b-roll. Read the spec before trusting it.

## Footage rules

Single speaker, one camera, 1080p at most. `ingest` checks this and refuses anything else with the reason:

- larger than 1080p;
- the picture cuts to a different shot (several cameras, or cutaways already edited in);
- two or more faces on screen in most sampled frames, or a second voice in the audio;
- no audio track.

Jump cuts and punch-ins on the same camera angle are allowed (with a warning). The checks are heuristics;
if one is wrong for your footage, `--force` (CLI) or `force=true` (MCP) continues anyway.

## Not included

Hosted web app, online accounts (the editor's accounts are local), payments, cloud deployment, YouTube downloading, generated or stock b-roll,
multi-camera. (The editor above is a local page, not a hosted service.)

## Tests

```bash
.venv/bin/python -m pytest tests/test_core.py           # fast, no media
.venv/bin/python tests/make_fixture.py tests/fixture    # synthetic footage (macOS `say` voice)
.venv/bin/python -m pytest                              # everything, about a minute
.venv/bin/python tests/mcp_smoke.py tests/fixture       # the MCP server, end to end
# tests/test_ui.py covers the UI server's API, accounts included; the page itself was checked by hand in a browser
# tests/dev_account.json is a test-only account; `clipper-ui-dev` in .claude/launch.json runs against a throwaway data folder
```

## Layout

```
src/clipper/
  cli.py            command line
  mcp_server.py     MCP tools
  ui_server.py      local web UI server      ui/   landing page and editor (plain HTML, CSS, JS modules)
  accounts.py       sign up, sign in, sessions (stored locally)      plans.py   pricing page data
  deepseek.py       DeepSeek client        director.py     the AI editor (clips, tool-calling edits)
  optable.py        edit operations by name, shared by the page and the AI editor
  pipeline.py       the stages, shared by both
  checks.py         footage rules          transcribe.py   Whisper, word timings
  select.py         fallback clip choice   edl.py          EDL + timeline maths
  ops.py            edit operations        render.py       ffmpeg graph
  audio.py          mix, ducking, SFX      captions.py     ASS captions
  overlays.py       HTML -> frames         vision.py       faces and text (Apple Vision)
  style_analyze.py  reference -> spec      style_apply.py  spec -> EDL
  templates/        captions/*.json, overlays/<kind>/<name>/
```
