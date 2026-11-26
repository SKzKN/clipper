# Templates

Two kinds: **caption templates** (JSON) and **overlay templates** (HTML). The ones in this folder are
plain examples; put your own in any of these places and they are picked up by name (first match wins):

1. `<project>.clipper/templates/` – only for that video
2. folders listed in `$CLIPPER_TEMPLATES` (separated by `:`)
3. `./templates/` in the folder you run clipper from
4. `~/.clipper/templates/`
5. this folder (built in)

A template with the same name as a built-in one replaces it.

```
templates/
  captions/<name>.json
  overlays/<kind>/<name>/template.json
  overlays/<kind>/<name>/template.html      (+ any images, fonts, css next to it)
  fonts/*.ttf|otf                            fonts for captions
```

## Caption templates

One JSON file. Every field is optional; these are the defaults:

```jsonc
{
  "description": "",
  "font": "Arial",             // a font installed on the machine, or one in templates/fonts/
  "bold": true, "italic": false,
  "uppercase": false,
  "strip_punctuation": true,   // drop . , ; : and quotes (keeps ? ! and apostrophes)
  "size": 0.045,               // font size as a fraction of the output height
  "letter_spacing": 0.0,       // fraction of the font size
  "color": "#FFFFFF",
  "outline_color": "#000000",
  "outline": 0.0035,           // outline width, fraction of the output height (0 = none)
  "shadow": 0.0,               // drop-shadow offset, fraction of the output height
  "shadow_color": "#000000",
  "box": null,                 // {"color": "#000000", "opacity": 0.6} for a solid background box
  "position": {"x": 0.5, "y": 0.72},   // centre of the caption, fractions of the frame
  "max_words": 3,              // words per caption
  "max_chars": 18,             // a caption also breaks before it gets longer than this
  "max_gap": 0.6,              // a pause longer than this (seconds) starts a new caption
  "linger": 0.15,              // seconds a caption stays after its last word
  "animation": "none",         // none | pop | fade | word_reveal | highlight
  "highlight_color": "#FFD400" // the spoken word's colour when animation is "highlight"
}
```

Sizes are fractions of the frame, so one template works at any output size. Any field can be overridden
per EDL (`set_caption_template` with `overrides`), which is also how a reference style is applied.

## Overlay templates

A folder with two files. `kind` is one of `title_card`, `lower_third`, `keyword_callout`.

### template.json

```jsonc
{
  "kind": "lower_third",
  "description": "One line an agent reads when choosing a template.",
  "default_duration": 4.0,
  "region": {"x": 0, "y": 0.74, "w": 1, "h": 0.16},   // part of the frame the overlay can touch
  "params": {
    "name":   {"type": "text",  "required": true, "description": "Name or main line"},
    "role":   {"type": "text",  "default": "",    "description": "Second line (optional)"},
    "accent": {"type": "color", "default": "#FFD400"}
  }
}
```

`region` is in fractions of the frame. Only that rectangle is captured, so keep it as small as the design
allows: rendering is faster. Leave it out for a full-frame overlay. Anything animating in from outside
the region is simply clipped at its edge.

### template.html

An ordinary web page with a transparent background. It is loaded at the output size (1080×1920 for 9:16,
1920×1080 for 16:9), so use `vw` / `vh` / `vmin` units and the same template works in both.

What the renderer does for you:

| You write | What happens |
|---|---|
| `<h1 data-param="title">` | the element's text becomes the `title` parameter |
| `<p data-if="subtitle">` | the element is removed when `subtitle` is empty |
| `var(--param-accent)` in CSS | every parameter is available as a CSS variable |
| `var(--duration)` in CSS | how long the overlay is on screen, e.g. `3s` |
| `window.CLIPPER` in JS | `{params, duration, width, height, fps}` |
| `window.clipperSetup = (info) => {}` | called once before the first frame (optional) |
| `window.clipperFrame = (t, duration) => {}` | called for every frame with the time in seconds (optional) |

**Animate with normal CSS animations, transitions or the Web Animations API.** The renderer does not play
the page in real time: for each frame it pauses every animation on the page and sets it to that frame's
time, so the result is frame-exact and identical on every render. Two rules follow from that:

- An exit animation is an animation with a delay: `animation: out .3s ease-in calc(var(--duration) - .3s) forwards;`
- Do not use `setTimeout`, `requestAnimationFrame` loops or video elements for timing. If you need
  script-driven motion, do it in `clipperFrame(t)`.

Minimal example:

```html
<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body { margin: 0; width: 100vw; height: 100vh; background: transparent; overflow: hidden; }
  .tag { position: absolute; top: 14vh; left: 50%; translate: -50% 0;
         padding: 1.6vmin 4vmin; border-radius: 99vmin; font: 800 6vmin "Helvetica Neue", Arial, sans-serif;
         background: var(--param-background, #FFD400); color: #111;
         animation: pop .3s cubic-bezier(.2, 1.6, .4, 1) both,
                    out .2s ease-in calc(var(--duration, 2s) - .2s) forwards; }
  @keyframes pop { from { opacity: 0; scale: .5; } to { opacity: 1; scale: 1; } }
  @keyframes out { to { opacity: 0; scale: .8; } }
</style></head>
<body><div class="tag" data-param="text">Keyword</div></body></html>
```

Fonts: use fonts installed on the machine, or ship the font file in the template folder and load it with
`@font-face { src: url("MyFont.woff2"); }`.

To check a template while designing it, add it to any EDL and ask for single frames:

```bash
clipper render talk.clipper polish --frame 0.2
clipper render talk.clipper polish --frame 1.5
```

Rendered overlay frames are cached and re-made whenever a file in the template folder or a parameter changes.
