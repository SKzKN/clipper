// Live preview: plays the source video through the EDL without rendering.
//
// Two <video> elements take turns: while one plays a segment, the other is
// already parked at the start of the next, so a cut is a swap rather than a
// seek. Crop, zooms, captions, overlays and b-roll are drawn on top with the
// same maths the renderer uses. Transitions, music and sound effects are only
// in the rendered preview.
import { h, mediaUrl, templateUrl } from './api.js';

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export class LivePlayer {
  constructor(stage, overlayJs) {
    this.stage = stage;
    this.overlayJs = overlayJs;
    this.p = null;            // EDL payload
    this.tl = 0;              // output time, seconds
    this.playing = false;
    this.seg = 0;
    this.active = 0;
    this.onTime = () => {};
    this.onState = () => {};
    this.onError = () => {};
    this.frames = new Map();  // overlay id -> {iframe, seek}
    this.brolls = new Map();  // b-roll id -> element

    this.zoomer = h('div.zoomer');
    this.videos = [0, 1].map(() => {
      const v = h('video', { playsInline: true, preload: 'auto' });
      v.addEventListener('error', () => this.onError(v.error));
      this.zoomer.append(v);
      return v;
    });
    this.brollLayer = h('div.layer');
    this.capLayer = h('div.layer');
    this.cap = h('div.cap');
    this.capLayer.append(this.cap);
    this.ovLayer = h('div.layer');
    stage.append(this.zoomer, this.brollLayer, this.capLayer, this.ovLayer);
    new ResizeObserver(() => this.draw()).observe(stage);
    const loop = () => { this.tick(); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }

  load(project, proxy = false) {
    this.project = project;
    const url = mediaUrl({ project, kind: 'source', ...(proxy ? { proxy: 1 } : {}) });
    for (const v of this.videos) { v.src = url; v.load(); }
  }

  get cur() { return this.videos[this.active]; }
  get other() { return this.videos[1 - this.active]; }

  setEdl(p) {
    const first = !this.p || this.p.name !== p.name;
    this.p = p;
    this.stage.style.aspectRatio = `${p.output.width} / ${p.output.height}`;
    const g = p.geometry;
    for (const v of this.videos) {
      Object.assign(v.style, {
        width: (g.base_w / g.out_w) * 100 + '%', height: (g.base_h / g.out_h) * 100 + '%',
        left: (-g.x0 / g.out_w) * 100 + '%', top: (-g.y0 / g.out_h) * 100 + '%',
      });
    }
    for (const [id, f] of this.frames) { f.iframe.remove(); this.frames.delete(id); }
    for (const [id, el] of this.brolls) { el.remove(); this.brolls.delete(id); }
    this.seek(first ? 0 : Math.min(this.tl, Math.max(0, p.duration - 0.05)));
  }

  segAt(tl) {
    const segs = this.p.segments;
    for (let i = 0; i < segs.length; i++) if (tl < segs[i].tl[1]) return i;
    return segs.length - 1;
  }

  seek(tl) {
    if (!this.p || !this.p.segments.length) return;
    tl = clamp(tl, 0, this.p.duration);
    this.seg = this.segAt(tl);
    const s = this.p.segments[this.seg];
    this.setTime(this.cur, s.src[0] + clamp(tl - s.tl[0], 0, s.src[1] - s.src[0]));
    this.parkNext();
    this.tl = tl;
    this.show();
    this.draw();
    this.onTime(this.tl);
  }

  setTime(v, t) { try { v.currentTime = t; } catch { /* not ready yet */ } }

  parkNext() {
    const next = this.p.segments[this.seg + 1];
    this.other.pause();
    if (next) this.setTime(this.other, next.src[0]);
  }

  show() {
    this.cur.style.visibility = 'visible';
    this.other.style.visibility = 'hidden';
    this.cur.muted = false;
    this.other.muted = true;
  }

  play() {
    if (!this.p) return;
    if (this.tl >= this.p.duration - 0.05) this.seek(0);
    this.playing = true;
    this.cur.play().catch(() => { this.playing = false; this.onState(false); });
    this.onState(true);
  }

  pause() {
    this.playing = false;
    for (const v of this.videos) v.pause();
    this.onState(false);
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  tick() {
    if (!this.p || !this.playing) return;
    const segs = this.p.segments;
    const s = segs[this.seg];
    const t = this.cur.currentTime;
    if (t >= s.src[1] - 0.5 / this.p.fps || this.cur.ended) {
      if (this.seg + 1 < segs.length) {
        const prev = this.cur;
        this.active = 1 - this.active;
        this.seg += 1;
        this.cur.play().catch(() => {});
        prev.pause();
        this.show();
        this.parkNext();
      } else {
        this.pause();
        this.tl = this.p.duration;
        this.draw();
        this.onTime(this.tl);
        return;
      }
    }
    const now = segs[this.seg];
    this.tl = now.tl[0] + clamp(this.cur.currentTime - now.src[0], 0, now.src[1] - now.src[0]);
    this.draw();
    this.onTime(this.tl);
  }

  // ---- drawing --------------------------------------------------------
  draw() {
    if (!this.p) return;
    this.drawZoom();
    this.drawCaptions();
    this.drawOverlays();
    this.drawBroll();
  }

  drawZoom() {
    const g = this.p.geometry, f = this.p.framing, t = this.tl;
    let z = 1, px = f.x * g.base_w - g.x0, py = f.y * g.base_h - g.y0;
    const px0 = px, py0 = py;
    for (const zm of this.p.zooms) {
      let w = 0;
      if (zm.ease > 0) {
        const e = Math.max(1 / this.p.fps, Math.min(zm.ease, (zm.tl_end - zm.tl_start) / 2));
        const q = clamp((t - zm.tl_start) / e, 0, 1) * clamp((zm.tl_end - t) / e, 0, 1);
        w = q * q * (3 - 2 * q);
      } else if (t >= zm.tl_start - 1e-3 && t < zm.tl_end - 1e-3) {
        w = 1;
      }
      z += (zm.scale - 1) * w;
      if (zm.focus) {
        px += (zm.focus[0] * g.base_w - g.x0 - px0) * w;
        py += (zm.focus[1] * g.base_h - g.y0 - py0) * w;
      }
    }
    this.zoomer.style.transformOrigin = `${(clamp(px, 0, g.out_w) / g.out_w) * 100}% ${(clamp(py, 0, g.out_h) / g.out_h) * 100}%`;
    this.zoomer.style.transform = z === 1 ? '' : `scale(${z})`;
  }

  drawCaptions() {
    const c = this.p.captions, t = this.tl, cap = this.cap;
    const group = c.enabled ? c.groups.find((g) => t >= g.start && t < g.end) : null;
    if (!group) { cap.style.display = 'none'; this.capGroup = null; return; }
    const tpl = c.template, H = this.stage.clientHeight;
    const size = (tpl.size * H) / lineRatio(tpl.font, tpl.bold);
    if (this.capGroup !== group) {
      this.capGroup = group;
      cap.replaceChildren(...group.words.flatMap((w, i) => [i ? ' ' : '', h('span', {}, w.text)]));
      const outline = tpl.box ? 0 : tpl.outline * H;
      Object.assign(cap.style, {
        display: 'block', left: tpl.position.x * 100 + '%', top: tpl.position.y * 100 + '%',
        fontFamily: `"${tpl.font}", "Helvetica Neue", Arial, sans-serif`, fontSize: size + 'px',
        fontWeight: tpl.bold ? 800 : 500, fontStyle: tpl.italic ? 'italic' : 'normal',
        letterSpacing: tpl.letter_spacing * size + 'px', color: tpl.color,
        webkitTextStroke: outline ? `${outline * 2}px ${tpl.outline_color}` : '0',
        textShadow: tpl.shadow ? `0 ${tpl.shadow * H}px ${tpl.shadow * H * 2}px ${tpl.shadow_color}` : 'none',
        background: tpl.box ? hexAlpha(tpl.box.color || '#000000', tpl.box.opacity ?? 0.6) : 'none',
        padding: tpl.box ? `${size * 0.12}px ${size * 0.3}px` : '0',
      });
    }
    const spans = cap.querySelectorAll('span');
    let activeIdx = 0;
    group.words.forEach((w, i) => { if (t >= w.start) activeIdx = i; });
    spans.forEach((sp, i) => {
      sp.style.color = tpl.animation === 'highlight' && i === activeIdx ? tpl.highlight_color : '';
      sp.style.visibility = tpl.animation === 'word_reveal' && i > activeIdx ? 'hidden' : '';
    });
    const dt = t - group.start;
    let scale = 1, opacity = 1;
    if (tpl.animation === 'pop') scale = dt < 0.07 ? 0.72 + (0.34 * dt) / 0.07 : dt < 0.13 ? 1.06 - (0.06 * (dt - 0.07)) / 0.06 : 1;
    if (tpl.animation === 'fade') opacity = clamp(Math.min(dt / 0.09, (group.end - t) / 0.07), 0, 1);
    cap.style.transform = `translate(-50%, -50%) scale(${scale})`;
    cap.style.opacity = opacity;
  }

  drawOverlays() {
    const t = this.tl, g = this.p.geometry;
    const scale = this.stage.clientWidth / g.out_w;
    const live = new Set();
    for (const o of this.p.overlays) {
      if (t < o.tl_start - 0.3 || t >= o.tl_end) continue;
      live.add(o.id);
      let f = this.frames.get(o.id);
      if (!f) {
        const iframe = h('iframe.ov', { src: templateUrl(o.template), scrolling: 'no' });
        iframe.style.width = g.out_w + 'px';
        iframe.style.height = g.out_h + 'px';
        f = { iframe, seek: null };
        iframe.addEventListener('load', () => {
          try {
            const win = iframe.contentWindow, root = win.document.documentElement;
            win.CLIPPER = { duration: o.duration, width: g.out_w, height: g.out_h, fps: this.p.fps, params: {} };
            root.style.setProperty('--duration', o.duration + 's');
            root.style.setProperty('--w', g.out_w + 'px');
            root.style.setProperty('--h', g.out_h + 'px');
            const defaults = {};
            for (const [k, v] of Object.entries(o.defaults || {})) defaults[k] = v;
            win.eval('(' + this.overlayJs.prepare + ')')([o.params || {}, defaults]);
            f.seek = win.eval('(' + this.overlayJs.seek + ')');
            this.draw();
          } catch (err) { console.warn('overlay preview failed', err); }
        });
        this.frames.set(o.id, f);
        this.ovLayer.append(iframe);
      }
      f.iframe.style.transform = `scale(${scale})`;
      f.iframe.style.visibility = t >= o.tl_start && f.seek ? 'visible' : 'hidden';
      if (f.seek) { try { f.seek(Math.max(0, t - o.tl_start)); } catch { /* template error */ } }
    }
    for (const [id, f] of this.frames) if (!live.has(id)) { f.iframe.remove(); this.frames.delete(id); }
  }

  drawBroll() {
    const t = this.tl;
    const live = new Set();
    for (const b of this.p.broll) {
      if (t < b.tl_start - 0.5 || t >= b.tl_end) continue;
      live.add(b.id);
      let el = this.brolls.get(b.id);
      if (!el) {
        const url = mediaUrl({ project: this.project, kind: 'broll', edl: this.p.name, id: b.id });
        el = b.still ? h('img.broll', { src: url }) : h('video.broll', { src: url, muted: true, playsInline: true, preload: 'auto' });
        el.style.objectFit = b.fit === 'contain' ? 'contain' : 'cover';
        this.brolls.set(b.id, el);
        this.brollLayer.append(el);
      }
      const on = t >= b.tl_start;
      el.style.visibility = on ? 'visible' : 'hidden';
      if (!b.still) {
        const want = (b.source_in || 0) + Math.max(0, t - b.tl_start);
        if (Math.abs(el.currentTime - want) > 0.25) this.setTime(el, want);
        if (on && this.playing && el.paused) el.play().catch(() => {});
        if ((!on || !this.playing) && !el.paused) el.pause();
      }
    }
    for (const [id, el] of this.brolls) if (!live.has(id)) { el.remove(); this.brolls.delete(id); }
  }
}

// The renderer (libass) sizes a font so that its full line box (ascent + descent) equals the font
// size; CSS sizes it by the em square. This ratio converts one to the other for the font in use.
const ratios = new Map();
function lineRatio(font, bold) {
  const key = font + (bold ? '|b' : '');
  if (!ratios.has(key)) {
    const g = document.createElement('canvas').getContext('2d');
    g.font = `${bold ? 800 : 500} 100px "${font}", "Helvetica Neue", Arial, sans-serif`;
    const m = g.measureText('Hg');
    const r = (m.fontBoundingBoxAscent + m.fontBoundingBoxDescent) / 100;
    ratios.set(key, r > 0.8 && r < 2 ? r : 1.2);
  }
  return ratios.get(key);
}

function hexAlpha(hex, alpha) {
  const n = parseInt(hex.replace('#', ''), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}
