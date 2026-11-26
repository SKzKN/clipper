// clipper editor: home, project setup, and the editor shell (player, transcript, timeline).
import { get, post, runJob, Rejected, toast, h, fmt, mediaUrl, onSignedOut } from './api.js';
import { LivePlayer } from './player.js';
import { S, ctx } from './state.js';
import { renderInspector } from './inspector.js';
import { showLanding, showAuth, showPricing } from './landing.js';

const app = document.getElementById('app');
const show = (...nodes) => { app.replaceChildren(...nodes); window.scrollTo(0, 0); };

// ==========================================================================
// Boot and home
// ==========================================================================

async function boot() {
  try {
    S.cfg = await get('/api/state');
  } catch (err) {
    show(h('div.shell.narrow', h('div.notice.bad', 'Could not reach the clipper server: ' + err.message)));
    return;
  }
  route();
}

// Signed out: the landing page (or straight to sign-in when the page was opened for a video).
// Signed in: the video that was asked for, or the list of videos.
function route() {
  const m = location.hash.match(/^#open=(.+)$/);
  if (m) {   // `clipper ui video.mp4` opens the page with #open=<path>
    history.replaceState(null, '', location.pathname);
    S.pendingPath = decodeURIComponent(m[1]);
  }
  if (!S.cfg.user) {
    if (S.pendingPath) showAuth(S.cfg.has_accounts ? 'signin' : 'signup'); else showLanding();
    return;
  }
  const path = S.pendingPath;
  S.pendingPath = null;
  if (path) openPath(path); else showHome();
}
window.addEventListener('hashchange', () => { if (/^#open=/.test(location.hash)) route(); });

ctx.show = show;
ctx.home = () => showHome();
ctx.enter = async () => { S.cfg = await get('/api/state'); route(); };   // after signing up or in
onSignedOut(async () => {   // the session ended while the app was open
  if (!S.cfg?.user) return;
  S.cfg.user = null;   // several requests can fail at once; act on the first
  ctx.player?.pause?.();
  S.cfg = await get('/api/state');
  toast('You were signed out. Sign in to continue.', 'error');
  showAuth('signin');
});

async function signOut() {
  try { await post('/api/auth/signout'); } catch { /* the cookie is gone either way */ }
  S.cfg = await get('/api/state');
  S.project = null;
  showLanding();
}

async function pickFile(kind) {
  if (!S.cfg.can_pick) return null;
  try {
    return (await post('/api/pick', { kind })).path;
  } catch (err) {
    toast(err.message, 'error');
    return null;
  }
}
ctx.pickFile = pickFile;

// Top bar of the signed-in pages.
function nav() {
  const u = S.cfg.user;
  return h('header.nav',
    h('button.brand', { onclick: showHome, title: 'Your videos' }, 'clipper', h('i', '.')),
    h('span.grow'),
    h('button.navlink.always', { onclick: showPricing }, 'Pricing'),
    h('span.account', { title: u.email }, h('i.avatar', u.name.trim()[0].toUpperCase()), h('span.hide-narrow', u.name)),
    h('button.link', { onclick: signOut }, 'Sign out'));
}
ctx.nav = nav;

// "Let DeepSeek do it" switch on the setup cards: on by default once DeepSeek is connected.
function aiSwitch(label) {
  const ready = S.cfg.ai.ready;
  const box = h('input', { type: 'checkbox', checked: ready, disabled: !ready });
  const el = h('div', h('label.check', box, h('span', label)),
    ready ? null : h('p.hint', 'DeepSeek is not connected yet, so the built-in recipe is used. To connect it, run ',
      h('code', 'clipper deepseek set-key'), ' in a terminal and start clipper again.'));
  return { el, on: () => box.checked };
}

function showHome() {
  S.project = null;
  document.title = 'clipper';
  const path = h('input', { type: 'text', placeholder: 'Paste the path to a video', spellcheck: false, 'aria-label': 'Path to a video',
    onkeydown: (e) => { if (e.key === 'Enter') openPath(path.value); } });
  const recent = S.cfg.recent;
  show(h('div.page', nav(), h('main.shell.narrow',
    h('h1', `Welcome${recent.length ? ' back' : ''}, ${S.cfg.user.name.trim().split(/\s+/)[0]}`),
    h('p.lead.left', 'Open a video to polish it, or to pull clips out of it.'),
    S.cfg.problems.length ? h('div.notice.bad', S.cfg.problems.join(' ')) : null,
    h('div.open-row.left',
      h('div.search', path, h('button.btn.primary', { onclick: () => openPath(path.value) }, 'Open video')),
      S.cfg.can_pick ? [h('span.or', 'or'),
        h('button.btn.lg', { onclick: async () => { const p = await pickFile('video'); if (p) openPath(p); } }, 'Choose a file…')] : null),
    h('p.muted.small', 'Single speaker, one camera, up to 1080p. Your video files stay on this computer.'),
    S.cfg.usage ? h('p.usage', h('span.chip.ok', S.cfg.usage.plan + ' plan'),
      ` ${S.cfg.usage.minutes} of ${S.cfg.usage.limit ?? '∞'} minutes of video opened this month `,
      h('button.link', { onclick: showPricing }, S.cfg.pricing.promo ? S.cfg.pricing.promo.headline : 'See plans')) : null,
    h('div.feed', { style: { marginTop: '28px' } }, h('h2.feed-head', 'Your videos'),
      recent.length ? recent.map((r) => h('button.feed-item', { onclick: () => openPath(r.project) },
        h('span.small.muted', 'Opened ' + r.opened),
        h('span.title', r.name),
        h('span.small.muted.path', r.video),
        h('span.chips', h('span.chip', fmt(r.duration)))))
        : h('div.empty', 'No videos yet. Open one above and it will be listed here.')))));
}

async function openPath(path, force = false) {
  path = (path || '').trim();
  if (!path) return;
  try {
    const res = await runJob('/api/open', { path, force });
    S.cfg.recent = (await get('/api/state')).recent;
    showSetup(typeof res.project === 'string' ? res : res.project);  // a finished check returns the project itself
  } catch (err) {
    if (err instanceof Rejected) showRejected(path, err.result);
    else { toast(err.message, 'error'); if (!S.project) showHome(); }
  }
}

function showRejected(path, result) {
  show(h('div.page', nav(), h('main.shell.narrow',
    h('button.link', { onclick: showHome }, '← All videos'),
    h('div.card',
      h('h3', "This footage can't be used as it is"),
      h('p.muted.small', { style: { wordBreak: 'break-all' } }, path),
      h('div.notice.bad', h('ul', result.reasons.map((r) => h('li', r)))),
      result.warnings.length ? h('div.notice.warn', h('ul', result.warnings.map((r) => h('li', r)))) : null,
      h('p.muted.small', { style: { marginTop: '14px' } },
        'These checks are automatic and can be wrong. If this really is one person on one camera, you can continue.'),
      h('div.row', { style: { marginTop: '18px' } },
        h('button.btn.primary', { onclick: showHome }, 'Choose another video'),
        h('button.btn', { onclick: () => openPath(path, true) }, 'Open anyway'))))));
}

// ==========================================================================
// Project setup: transcribe, then choose what to make
// ==========================================================================

function showSetup(proj) {
  S.project = proj;
  S.words = null;
  S.peaks = null;
  document.title = proj.name + ' · clipper';
  const checks = proj.checks || {};
  const head = h('div.proj-head',
    h('button.link', { onclick: showHome }, '← All videos'),
    h('h1', proj.name),
    h('div.chips', h('span.chip', fmt(proj.source.duration)), h('span.chip', `${proj.source.width}×${proj.source.height}`),
      h('span.chip', `${Math.round(proj.source.fps)} fps`), proj.has_transcript ? h('span.chip.ok', 'Transcribed') : null));
  const notes = [];
  if (checks.forced) notes.push(h('div.notice.bad', 'Opened despite failing the footage check: ', (checks.reasons || []).join(' ')));
  if ((checks.warnings || []).length) notes.push(h('div.notice.warn', h('ul', checks.warnings.map((w) => h('li', w)))));

  let body;
  if (!proj.has_transcript) {
    const model = h('select', {}, ['base', 'small', 'medium', 'large-v3'].map((m) => h('option', { value: m, selected: m === 'small' }, m)));
    body = h('div.card',
      h('h3', 'Transcribe'),
      h('p.muted.small', 'clipper cuts and captions from the words, so it needs a transcript first. Larger models are slower and more accurate; the first use of a model downloads it.'),
      h('div.row', { style: { marginTop: '12px' } }, h('span.muted', 'Model'), model,
        h('button.btn.primary', { onclick: async () => {
          try {
            await runJob('/api/transcribe', { project: proj.project, model: model.value });
            showSetup(await get('/api/project', { project: proj.project }));
          } catch (err) { toast(err.message, 'error'); }
        } }, 'Transcribe')));
  } else {
    body = [
      proj.edls.length ? h('div.feed', { style: { marginTop: '28px' } }, h('h2.feed-head', 'Your edits'), proj.edls.map((e) =>
        h('button.feed-item', { onclick: () => openEditor(e.name) }, h('span.title', e.name),
          h('span.chips', h('span.chip.ok', e.mode === 'clip' ? 'Clip' : 'Polish'), h('span.chip', fmt(e.duration)), h('span.chip', e.output.aspect))))) : null,
      h('h2.section-title', proj.edls.length ? 'Start another edit' : 'What do you want to make?'),
      h('div.choices', polishCard(proj), clipsCard(proj)),
    ];
  }
  show(h('div.page', nav(), h('main.shell.narrow', head, notes, body)));
}

function recipeControls() {
  const aspect = h('select', {}, S.cfg.aspects.map((a) => h('option', { value: a }, a === 'source' ? 'Same as source' : a)));
  const captions = h('select', {}, h('option', { value: '' }, 'No captions'),
    S.cfg.captions.map((c) => h('option', { value: c.name, selected: c.name === 'bold-pop' }, c.name)));
  const el = h('div', h('label.field', h('span', 'Format'), aspect), h('label.field', h('span', 'Captions'), captions));
  return { el, values: () => ({ aspect: aspect.value, captions: captions.value || null }) };
}

function polishCard(proj) {
  const rc = recipeControls();
  const ai = aiSwitch('Let DeepSeek plan the edit');
  return h('div.card',
    h('h3', 'Polish the whole video'),
    h('p.muted.small', 'One finished video: fillers and long pauses cut, captions, punch-in zooms.'),
    rc.el, ai.el,
    h('button.btn.primary', { style: { marginTop: '8px' }, onclick: async () => {
      try {
        let name = 'polish', n = 2;
        while (proj.edls.some((e) => e.name === name)) name = `polish-${n++}`;
        const res = await runJob('/api/create', { project: proj.project, kind: 'polish', name, ai: ai.on(), ...rc.values() });
        if (res.note) toast(res.note, 'error');
        S.project = res.project;
        openEditor(res.created[0]);
      } catch (err) { toast(err.message, 'error'); }
    } }, 'Create'));
}

function clipsCard(proj) {
  const rc = recipeControls();
  const count = h('input', { type: 'number', min: 1, max: 20, value: 3, style: { width: '56px' } });
  const min = h('input', { type: 'number', min: 5, max: 300, value: 20, style: { width: '56px' } });
  const max = h('input', { type: 'number', min: 10, max: 600, value: 60, style: { width: '56px' } });
  const ai = aiSwitch('Let DeepSeek choose and finish the clips');
  const list = h('div');
  let proposals = [];
  const create = h('button.btn.primary', { style: { marginTop: '10px', display: 'none' }, onclick: async () => {
    const picked = proposals.filter((p) => p.checked.checked).map((p) => p.clip);
    if (!picked.length) return toast('Tick at least one clip.');
    try {
      const res = await runJob('/api/create', { project: proj.project, kind: 'clips', clips: picked, ai: ai.on(), ...rc.values() });
      if (res.note) toast(res.note, 'error');
      S.project = res.project;
      openEditor(res.created[0]);
    } catch (err) { toast(err.message, 'error'); }
  } }, 'Create selected clips');
  return h('div.card',
    h('h3', 'Find clips'),
    h('p.muted.small', 'Several shorts from one long video. Suggestions are a starting point; you can adjust each clip after.'),
    h('div.row.wrap', { style: { margin: '10px 0' } }, h('span.muted', 'Up to'), count, h('span.muted', 'clips of'), min, h('span.muted', 'to'), max, h('span.muted', 'seconds')),
    rc.el, ai.el,
    h('button.btn', { onclick: async () => {
      try {
        const res = await runJob('/api/select', { project: proj.project, count: +count.value, min: +min.value, max: +max.value, ai: ai.on() });
        if (res.note) toast(res.note);
        proposals = res.clips.map((clip) => ({ clip, checked: h('input', { type: 'checkbox', checked: true }) }));
        list.replaceChildren(...(proposals.length ? proposals.map((p) => h('label.proposal', p.checked,
          h('div', h('b', p.clip.title || 'Clip'), h('span.muted.small', p.clip.reason || '')),
          h('span.muted.small', `${fmt(p.clip.start)} – ${fmt(p.clip.end)}`)))
          : [h('div.notice', 'No stretch of that length made a usable clip. Try a wider range.')]));
        create.style.display = proposals.length ? '' : 'none';
      } catch (err) { toast(err.message, 'error'); }
    } }, 'Suggest clips'),
    list, create);
}

// ==========================================================================
// Editor
// ==========================================================================

let player = null;
let els = {};

async function openEditor(name) {
  const project = S.project.project;
  try {
    if (!S.words) {
      const t = await get('/api/transcript', { project });
      S.words = t.words;
      S.fillers = new Set(t.fillers);
    }
    const payload = await get('/api/edl', { project, edl: name });
    buildEditor();
    player.load(project);
    applyEdl(payload);
    if (!S.peaks) get('/api/peaks', { project }).then((p) => { S.peaks = p; drawWave(); }).catch(() => {});
    get('/api/styles', { project }).then((r) => { S.styles = r.styles; if (S.tab === 'Style') renderInspector(els.inspector); }).catch(() => {});
  } catch (err) {
    toast(err.message, 'error');
  }
}

function buildEditor() {
  S.sel = null;
  S.selectedItem = null;
  S.mode = 'live';
  els = {};
  els.edlSelect = h('select', { title: 'Switch edit', onchange: (e) => openEditor(e.target.value) });
  els.undo = h('button.btn.ghost', { title: 'Undo (⌘Z)', onclick: () => undo(false) }, 'Undo');
  els.redo = h('button.btn.ghost', { title: 'Redo (⇧⌘Z)', onclick: () => undo(true) }, 'Redo');
  els.duration = h('span.muted.small');
  const topbar = h('div.topbar',
    h('button.btn.ghost', { onclick: async () => { player.pause(); showSetup(await get('/api/project', { project: S.project.project })); } }, '← ' + S.project.name),
    h('span.sep'), els.edlSelect, h('span.sep'), els.undo, els.redo,
    h('span.grow'), els.duration,
    h('button.btn', { onclick: renderPreview }, 'Render preview'),
    h('button.btn.primary', { onclick: exportVideo }, 'Export'));

  els.stage = h('div.stage');
  els.stageWrap = h('div.stage-wrap', els.stage);
  els.play = h('button.btn', { onclick: () => player.toggle(), title: 'Play / pause (Space)', style: { minWidth: '64px' } }, 'Play');
  els.time = h('span.time');
  els.badge = h('span.badge');
  els.modeLive = h('button.on', { onclick: () => setMode('live') }, 'Live');
  els.modeRendered = h('button', { onclick: () => setMode('rendered') }, 'Rendered');
  els.hint = h('div.hint', 'Live shows cuts, crop, zooms, captions, graphics and b-roll. Transitions, music and sound effects are in the rendered preview.');
  const playerPane = h('div.pane.player', els.stageWrap,
    h('div.transport', els.play, els.time, h('span.grow'), els.badge, h('div.seg', els.modeLive, els.modeRendered)), els.hint);

  els.transcript = h('div.transcript');
  els.transcriptPane = h('div.pane', els.transcript);
  els.inspector = h('div.pane.inspector');
  els.timeline = h('div.timeline');
  show(h('div.editor', topbar, h('div.main', playerPane, els.transcriptPane, els.inspector), els.timeline));

  player = new LivePlayer(els.stage, S.cfg.overlay_js);
  ctx.player = player;
  player.onTime = onTime;
  player.onState = (playing) => { els.play.textContent = playing ? 'Pause' : 'Play'; };
  let triedProxy = false;
  player.onError = async () => {
    if (triedProxy) return;
    triedProxy = true;
    toast("The browser can't play this video's format directly. Making a playable copy for the preview…");
    try {
      await runJob('/api/proxy', { project: S.project.project });
      player.load(S.project.project, true);
      player.seek(player.tl);
    } catch (err) { toast(err.message, 'error'); }
  };
  new ResizeObserver(fitStage).observe(els.stageWrap);
  new ResizeObserver(() => { drawWave(); }).observe(els.timeline);
  buildTranscript();
}

function fitStage() {
  if (!S.edl) return;
  const { width: W, height: H } = els.stageWrap.getBoundingClientRect();
  const ratio = S.edl.output.width / S.edl.output.height;
  let w = W, hgt = W / ratio;
  if (hgt > H) { hgt = H; w = H * ratio; }
  els.stage.style.width = Math.floor(w) + 'px';
  els.stage.style.height = Math.floor(hgt) + 'px';
}

function applyEdl(p) {
  const defaults = Object.fromEntries(S.cfg.overlays.map((t) => [t.id,
    Object.fromEntries(Object.entries(t.params).map(([k, v]) => [k, v.default ?? '']))]));
  for (const o of p.overlays) o.defaults = defaults[o.template] || {};
  S.edl = p;
  S.kept = [];
  p.word_tl.forEach((tl, i) => { if (tl) S.kept.push(i); });
  els.edlSelect.replaceChildren(...S.project.edls.map((e) => h('option', { value: e.name, selected: e.name === p.name }, e.name)));
  if (!S.project.edls.some((e) => e.name === p.name)) els.edlSelect.append(h('option', { value: p.name, selected: true }, p.name));
  els.undo.disabled = !p.can_undo;
  els.redo.disabled = !p.can_redo;
  els.duration.textContent = `${fmt(p.duration)} of ${fmt(S.project.source.duration)} · ${p.output.aspect}`;
  fitStage();
  player.setEdl(p);
  updatePreviewBadge();
  updateTranscript();
  renderTimeline();
  renderInspector(els.inspector);
  // show each warning once, not after every edit
  S.warned = S.warned || new Set();
  for (const w of p.warnings) if (!S.warned.has(p.name + w)) { S.warned.add(p.name + w); toast(w, 'error'); }
  for (const key of [...S.warned]) if (key.startsWith(p.name) && !p.warnings.includes(key.slice(p.name.length))) S.warned.delete(key);
}
ctx.applyEdl = applyEdl;

function updatePreviewBadge() {
  const pv = S.edl.preview;
  els.badge.className = 'badge' + (pv.exists && pv.stale ? ' stale' : '');
  els.badge.textContent = !pv.exists ? 'not rendered yet' : pv.stale ? 'render is out of date' : 'render is current';
  if (S.mode === 'rendered') setMode('rendered');
}

async function op(name, args = {}, done) {
  try {
    const res = await post('/api/op', { project: S.project.project, edl: S.edl.name, op: name, args });
    if (S.mode === 'rendered') setMode('live');  // the render no longer matches the edit
    applyEdl(res.edl);
    if (done) { const msg = done(res.result); if (msg) toast(msg, 'ok', 3000); }
    return res.result;
  } catch (err) {
    toast(err.message, 'error');
    return null;
  }
}
ctx.op = op;

async function undo(redo) {
  try {
    const res = await post('/api/undo', { project: S.project.project, edl: S.edl.name, redo });
    if (S.mode === 'rendered') setMode('live');
    applyEdl(res.edl);
  } catch (err) { toast(err.message); }
}

// Hand the edit to the AI editor. The page is held while it works so its changes do not collide with yours.
async function direct(instruction) {
  player.pause();
  const hold = h('div.scrim', h('div.dialog', h('h3', 'The AI editor is working'),
    h('p.muted', 'DeepSeek is reading the transcript and making the changes. This can take a minute. One Undo takes it all back.')));
  document.body.append(hold);
  try {
    const res = await runJob('/api/direct', { project: S.project.project, edl: S.edl.name, instruction });
    if (S.mode === 'rendered') setMode('live');
    applyEdl(res.edl);
    toast(res.summary, 'ok', 9000);
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    hold.remove();
  }
}
ctx.direct = direct;

async function renderPreview() {
  player.pause();
  try {
    const res = await runJob('/api/render', { project: S.project.project, edl: S.edl.name, quality: 'preview' });
    S.edl.preview = res.preview;
    for (const w of res.warnings || []) toast(w, 'error');
    setMode('rendered');
    updatePreviewBadge();
  } catch (err) { toast(err.message, 'error'); }
}
ctx.renderPreview = renderPreview;

async function exportVideo() {
  player.pause();
  try {
    const res = await runJob('/api/render', { project: S.project.project, edl: S.edl.name, quality: 'export' });
    S.edl.preview = res.preview;
    renderInspector(els.inspector);
    const el = h('div.scrim', h('div.dialog',
      h('h3', 'Exported'),
      h('p', `${fmt(res.duration)} · ${res.size[0]}×${res.size[1]} · rendered in ${res.render_seconds}s`),
      h('p.muted.small', { style: { marginTop: '6px', wordBreak: 'break-all' } }, res.path),
      (res.warnings || []).length ? h('div.notice.warn', h('ul', res.warnings.map((w) => h('li', w)))) : null,
      h('div.actions',
        h('button.btn', { onclick: () => post('/api/reveal', { path: res.path }).catch((e) => toast(e.message, 'error')) }, 'Show in Finder'),
        h('button.btn.primary', { onclick: () => el.remove() }, 'Done'))));
    document.body.append(el);
  } catch (err) { toast(err.message, 'error'); }
}
ctx.exportVideo = exportVideo;

function setMode(mode) {
  if (mode === 'rendered' && !S.edl.preview.exists) {
    toast('Nothing rendered yet. Use "Render preview" first.');
    mode = 'live';
  }
  S.mode = mode;
  els.modeLive.classList.toggle('on', mode === 'live');
  els.modeRendered.classList.toggle('on', mode === 'rendered');
  els.stage.querySelector('video.rendered')?.remove();
  const live = mode === 'live';
  for (const el of els.stage.children) el.style.display = live ? '' : 'none';
  els.play.disabled = !live;
  if (!live) {
    player.pause();
    els.stage.append(h('video.rendered', { controls: true, autoplay: false,
      src: mediaUrl({ project: S.project.project, kind: 'preview', edl: S.edl.name, v: S.edl.preview.version || 0 }) }));
  }
}

function onTime(t) {
  els.time.textContent = `${fmt(t)} / ${fmt(S.edl.duration)}`;
  if (els.head) els.head.style.left = (S.edl.duration ? (t / S.edl.duration) * 100 : 0) + '%';
  // highlight the word being spoken
  let lo = 0, hi = S.kept.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1, tl = S.edl.word_tl[S.kept[mid]];
    if (t < tl[0]) hi = mid - 1; else if (t >= tl[1]) { found = mid; lo = mid + 1; } else { found = mid; break; }
  }
  const idx = found >= 0 ? S.kept[found] : -1;
  if (idx !== S.nowWord) {
    if (S.nowWord >= 0) S.wordEls[S.nowWord]?.classList.remove('now');
    S.nowWord = idx;
    if (idx >= 0) {
      const el = S.wordEls[idx];
      el.classList.add('now');
      if (player.playing) {
        const pane = els.transcriptPane.getBoundingClientRect(), r = el.getBoundingClientRect();
        if (r.bottom > pane.bottom - 60 || r.top < pane.top + 20) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }
  }
}
ctx.seek = (t) => { if (S.mode !== 'live') setMode('live'); player.seek(t); };
ctx.now = () => player.tl;

// ==========================================================================
// Transcript: the text is the edit
// ==========================================================================

function buildTranscript() {
  S.wordEls = [];
  S.nowWord = -1;
  const root = els.transcript;
  root.replaceChildren();
  if (!S.words.length) { root.append(h('div.empty', 'No transcript.')); return; }
  let para = h('p');
  S.words.forEach(([text, start], i) => {
    if (i) {
      const [ptext, , pend] = S.words[i - 1];
      const gap = start - pend;
      if (gap > 2.5 || (gap > 0.9 && /[.!?]["')]?$/.test(ptext))) { root.append(para); para = h('p'); }
    }
    const el = h('span.w', { 'data-i': i }, text);
    S.wordEls.push(el);
    para.append(el, ' ');
  });
  root.append(para);
  els.seltools = h('div.seltools', { style: { display: 'none' } });
  root.append(els.seltools);

  let anchor = null, dragged = false;
  const wordAt = (e) => { const w = e.target.closest?.('.w'); return w ? +w.dataset.i : null; };
  root.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.seltools')) return;
    const i = wordAt(e);
    if (i == null) { setSelection(null); return; }
    dragged = false;
    if (e.shiftKey && S.sel) { setSelection({ a: S.sel.a, b: i }); anchor = S.sel.a; }
    else { anchor = i; setSelection({ a: i, b: i }, true); }
    root.setPointerCapture?.(e.pointerId);
  });
  root.addEventListener('pointermove', (e) => {
    if (anchor == null) return;
    const el = document.elementFromPoint(e.clientX, e.clientY)?.closest?.('.w');
    if (!el) return;
    const i = +el.dataset.i;
    if (i !== S.sel.b) { dragged = true; setSelection({ a: anchor, b: i }, true); }
  });
  root.addEventListener('pointerup', (e) => {
    if (anchor == null) return;
    const single = !dragged && S.sel && S.sel.a === S.sel.b && !e.shiftKey;
    anchor = null;
    if (single) { const tl = S.edl.word_tl[S.sel.a]; if (tl) ctx.seek(tl[0] + 0.001); }
    setSelection(S.sel);
  });
}

function selRange() {
  if (!S.sel) return null;
  const first = Math.min(S.sel.a, S.sel.b), last = Math.max(S.sel.a, S.sel.b);
  const kept = [];
  for (let i = first; i <= last; i++) if (S.edl.word_tl[i]) kept.push(i);
  return { first, last, kept, anyCut: kept.length < last - first + 1,
    start: kept.length ? S.edl.word_tl[kept[0]][0] : null, end: kept.length ? S.edl.word_tl[kept[kept.length - 1]][1] : null,
    text: S.words.slice(first, last + 1).map((w) => w[0]).join(' ') };
}
ctx.selRange = selRange;

function setSelection(sel, dragging = false) {
  const old = S.sel;
  S.sel = sel;
  if (old) for (let i = Math.min(old.a, old.b); i <= Math.max(old.a, old.b); i++) S.wordEls[i]?.classList.remove('sel');
  const r = selRange();
  if (!r) { els.seltools.style.display = 'none'; return; }
  for (let i = r.first; i <= r.last; i++) S.wordEls[i].classList.add('sel');
  if (dragging) { els.seltools.style.display = 'none'; return; }
  const tools = [];
  if (r.kept.length) {
    tools.push(h('button.btn.sm.danger', { title: 'Delete', onclick: cutSelection }, 'Cut'));
    tools.push(h('button.btn.sm', { onclick: () => op('add_zoom', { start: r.start, end: Math.max(r.end, r.start + 0.8) }, () => 'Zoom added') }, 'Zoom'));
    tools.push(h('button.btn.sm', { onclick: () => {
      const tpl = S.cfg.overlays.find((t) => t.kind === 'keyword_callout');
      if (!tpl) return toast('No keyword callout template installed.', 'error');
      const key = Object.keys(tpl.params).find((k) => tpl.params[k].type === 'text') || 'text';
      op('add_overlay', { template: tpl.id, start: r.start, duration: Math.max(1.2, Math.min(3, r.end - r.start + 0.6)),
        params: { [key]: r.text.replace(/[.,!?;:]+$/g, '') } }, () => 'Callout added');
    } }, 'Callout'));
    tools.push(h('button.btn.sm', { onclick: () => { ctx.seek(r.start); player.play(); } }, 'Play'));
  }
  if (r.anyCut) tools.push(h('button.btn.sm', { onclick: () => { op('restore_words', { first: r.first, last: r.last }, () => 'Restored'); setSelection(null); } }, 'Restore'));
  els.seltools.replaceChildren(...tools);
  const box = els.transcript.getBoundingClientRect(), a = S.wordEls[r.first].getBoundingClientRect();
  els.seltools.style.display = 'flex';
  els.seltools.style.left = Math.max(90, Math.min(box.width - 90, a.left - box.left + 40)) + 'px';
  els.seltools.style.top = a.top - box.top + 'px';
}

function cutSelection() {
  const r = selRange();
  if (!r || !r.kept.length) return;
  op('cut_words', { first: r.kept[0], last: r.kept[r.kept.length - 1] });
  setSelection(null);
}

function updateTranscript() {
  const tl = S.edl.word_tl;
  S.wordEls.forEach((el, i) => {
    el.classList.toggle('cut', !tl[i]);
    el.classList.toggle('filler', S.fillers.has(i));
  });
  if (S.sel) setSelection(S.sel);
}

document.addEventListener('keydown', (e) => {
  if (!S.edl || !document.querySelector('.editor')) return;
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(e.shiftKey); return; }
  if (typing) return;
  if (e.key === ' ') { e.preventDefault(); if (S.mode === 'live') player.toggle(); }
  else if (e.key === 'Backspace' || e.key === 'Delete') { e.preventDefault(); cutSelection(); }
  else if (e.key === 'Escape') setSelection(null);
  else if (e.key === 'ArrowLeft') ctx.seek(Math.max(0, player.tl - (e.shiftKey ? 5 : 1)));
  else if (e.key === 'ArrowRight') ctx.seek(Math.min(S.edl.duration, player.tl + (e.shiftKey ? 5 : 1)));
});

// ==========================================================================
// Timeline
// ==========================================================================

function renderTimeline() {
  const p = S.edl, dur = Math.max(p.duration, 0.001);
  const pct = (t) => (t / dur) * 100 + '%';
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  const step = steps.find((s) => dur / s <= 12) || 900;
  const ruler = h('div.tl-ruler');
  for (let t = 0; t < dur; t += step) ruler.append(h('span', { style: { left: pct(t) } }, fmt(t).replace(/\.\d$/, '')));
  els.wave = h('canvas.tl-wave');
  const select = (kind, item, tab) => (e) => {
    e.stopPropagation();
    S.selectedItem = { kind, id: item.id };
    S.tab = tab;
    ctx.seek(item.tl_start ?? item.tl_at);
    renderTimeline();
    renderInspector(els.inspector);
  };
  const isSel = (kind, id) => S.selectedItem && S.selectedItem.kind === kind && S.selectedItem.id === id;
  const bar = (cls, kind, item, tab, title) => h('div.tl-item.' + cls + (isSel(kind, item.id) ? '.on' : ''), {
    title, style: { left: pct(item.tl_start), width: pct(item.tl_end - item.tl_start) }, onpointerdown: select(kind, item, tab) });
  const row = (label, ...items) => h('div.tl-row', h('span.lab', items.flat().length ? '' : label), ...items);
  const body = h('div.tl-body', ruler, els.wave,
    p.cuts.map((c) => h('div.tl-cut' + (c.transition !== 'cut' ? '.tr' : ''), { style: { left: pct(c.time) } })),
    row('zooms', p.zooms.map((z) => bar('zoom', 'zooms', z, 'Zoom', `Zoom ${z.scale}×`))),
    row('graphics and b-roll',
      p.overlays.map((o) => bar('overlay', 'overlays', o, 'Graphics', o.template)),
      p.broll.map((b) => bar('broll', 'broll', b, 'Graphics', 'B-roll'))),
    row('music and sounds',
      p.music ? h('div.tl-item.music', { style: { left: 0, width: '100%' }, title: 'Music' }) : null,
      p.sfx.map((s) => h('div.tl-item.sfx' + (isSel('sfx', s.id) ? '.on' : ''), { title: s.sound, style: { left: pct(s.tl_at) },
        onpointerdown: select('sfx', s, 'Audio') }))));
  els.head = h('div.tl-head');
  body.append(els.head);
  const seekAt = (e) => {
    const r = body.getBoundingClientRect();
    ctx.seek(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * dur);
  };
  body.addEventListener('pointerdown', (e) => {
    seekAt(e);
    body.setPointerCapture(e.pointerId);
    const move = (ev) => seekAt(ev);
    body.addEventListener('pointermove', move);
    body.addEventListener('pointerup', () => body.removeEventListener('pointermove', move), { once: true });
  });
  els.timeline.replaceChildren(body);
  drawWave();
  onTime(player.tl);
}
ctx.renderTimeline = renderTimeline;

function drawWave() {
  const c = els.wave;
  if (!c || !S.edl) return;
  const dpr = window.devicePixelRatio || 1;
  const W = Math.max(1, Math.floor(c.clientWidth * dpr)), H = Math.floor(c.clientHeight * dpr);
  c.width = W;
  c.height = H;
  const g = c.getContext('2d');
  if (!S.peaks) return;
  const css = getComputedStyle(document.documentElement);
  const waveA = css.getPropertyValue('--wave-a').trim(), waveB = css.getPropertyValue('--wave-b').trim();
  const { peaks, per_second: pps } = S.peaks, segs = S.edl.segments, dur = S.edl.duration;
  let si = 0;
  for (let x = 0; x < W; x++) {
    const t = ((x + 0.5) / W) * dur;
    while (si < segs.length - 1 && t >= segs[si].tl[1]) si++;
    const s = segs[si];
    const a = Math.floor((s.src[0] + (t - s.tl[0])) * pps);
    const b = Math.max(a + 1, Math.floor(a + (dur / W) * pps));
    let peak = 0;
    for (let i = a; i < b && i < peaks.length; i++) if (peaks[i] > peak) peak = peaks[i];
    const y = Math.max(dpr, peak * (H - 4));
    g.fillStyle = si % 2 ? waveB : waveA;
    g.fillRect(x, (H - y) / 2, 1, y);
  }
}

boot();
