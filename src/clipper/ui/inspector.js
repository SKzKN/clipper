// The right-hand panels. Each one reads the current EDL payload and calls edit operations.
import { h, fmt, toast, runJob, get, post, mediaUrl, frameUrl } from './api.js';
import { S, ctx } from './state.js';

const TABS = ['AI', 'Tighten', 'Captions', 'Zoom', 'Graphics', 'Audio', 'Transitions', 'Style', 'Output'];

export function renderInspector(root) {
  const scroll = root.scrollTop;
  const tabs = h('div.tabs', TABS.map((t) => h('button' + (t === S.tab ? '.on' : ''), { onclick: () => { S.tab = t; renderInspector(root); } }, t)));
  const body = h('div.tabbody', PANELS[S.tab]());
  root.replaceChildren(tabs, body);
  root.scrollTop = scroll;
}

// ---- small form helpers ---------------------------------------------------

function slider(label, { min, max, step, value, show = (v) => v, onchange }) {
  const val = h('span.val', show(value));
  const input = h('input', { type: 'range', min, max, step, value,
    oninput: () => { val.textContent = show(+input.value); },
    onchange: () => onchange(+input.value) });
  return h('label.field', h('span', label), input, val);
}

function field(label, control) { return h('label.field', h('span', label), control); }

function selectEl(options, value, onchange) {
  return h('select', { onchange: (e) => onchange(e.target.value) },
    options.map((o) => { const [v, text] = Array.isArray(o) ? o : [o, o]; return h('option', { value: v, selected: v === value }, text); }));
}

function fileRow(kind, placeholder, onpick) {
  const input = h('input.grow', { type: 'text', placeholder, spellcheck: false,
    onkeydown: (e) => { if (e.key === 'Enter' && input.value.trim()) onpick(input.value.trim()); } });
  return h('div.row',
    S.cfg.can_pick ? h('button.btn', { onclick: async () => { const p = await ctx.pickFile(kind); if (p) { input.value = p; onpick(p); } } }, 'Choose…') : null,
    input, h('button.btn', { onclick: () => input.value.trim() && onpick(input.value.trim()) }, 'Use'));
}

function itemRow(kind, item, label, tab) {
  const on = S.selectedItem && S.selectedItem.kind === kind && S.selectedItem.id === item.id;
  return h('div.item' + (on ? '.on' : ''), { onclick: () => {
    S.selectedItem = { kind, id: item.id };
    ctx.seek(item.tl_start ?? item.tl_at);
    ctx.renderTimeline();
    S.tab = tab;
    renderInspector(document.querySelector('.inspector'));
  } },
    h('span.t', fmt(item.tl_start ?? item.tl_at)), h('span.grow', label),
    h('button.btn.sm.ghost.danger.x', { title: 'Remove', onclick: (e) => { e.stopPropagation(); ctx.op('remove_item', { id: item.id }); } }, '✕'));
}

const selected = (kind) => (S.selectedItem && S.selectedItem.kind === kind
  ? S.edl[kind].find((i) => i.id === S.selectedItem.id) : null);
const basename = (p) => (p || '').split('/').pop();

// ---- panels -----------------------------------------------------------------

const PANELS = {
  AI() {
    const ready = S.cfg.ai.ready;
    const note = (S.edl.notes || []).find((n) => n.kind === 'ai');
    const st = PANELS.AI.state || (PANELS.AI.state = { text: '', files: [] });
    const redraw = () => renderInspector(document.querySelector('.inspector'));
    const box = h('textarea', { rows: 4, value: st.text, disabled: !ready, 'aria-label': 'What to change',
      placeholder: 'Say what to change. For example: cut the part about pricing, add a title, and put the attached music under it.',
      oninput: (e) => { st.text = e.target.value; } });
    const run = (text) => { const files = st.files; st.text = ''; st.files = []; ctx.direct(text, files); };
    return [
      h('h2', 'AI editor'),
      ready ? null : h('div.notice.warn', 'DeepSeek is not connected yet. In a terminal, run ', h('code', 'clipper deepseek set-key'),
        ' and start clipper again.'),
      h('p.muted.small', 'DeepSeek does the editing: cuts and trims, zooms, captions and how they look, graphics, transitions, sounds, music, b-roll, the shape of the video, and copying the style of a reference short. It works from the words; it cannot see or hear the video.'),
      box,
      h('div.row.wrap', { style: { marginTop: '8px' } },
        S.cfg.can_pick ? h('button.btn.sm', { disabled: !ready, title: 'Music, a b-roll clip or image, or a reference short',
          onclick: async () => { const p = await ctx.pickFile('any'); if (p && !st.files.includes(p)) { st.files.push(p); redraw(); } } }, 'Attach a file…')
          : h('span.hint', 'To use music, b-roll or a reference short, put the file’s full path in your request.'),
        st.files.map((f) => h('span.chip', { title: f }, basename(f), ' ',
          h('button.unchip', { 'aria-label': 'Remove ' + basename(f), onclick: () => { st.files = st.files.filter((x) => x !== f); redraw(); } }, '✕')))),
      h('div.row.wrap', { style: { marginTop: '8px' } },
        h('button.btn.primary', { disabled: !ready, onclick: () => (box.value.trim() ? run(box.value) : toast('Say what to change first, or use "Finish it for me".')) }, 'Do it'),
        h('button.btn', { disabled: !ready, title: 'Tighten, zoom, title and callouts, chosen by the AI', onclick: () => run('') }, 'Finish it for me')),
      h('div.chips', { style: { marginTop: '12px' } },
        ['Make it tighter', 'Add a title card', 'More zooms', 'Bigger captions', 'Make it square', 'Cut the weakest part'].map((text) =>
          h('button.chip.go', { disabled: !ready, onclick: () => run(text) }, text))),
      note ? [h('h2', 'Last pass'), h('p', note.summary), h('p.muted.small', { style: { marginTop: '6px' } }, 'You asked: ' + note.request),
        note.steps.length ? h('ul.ai-steps', note.steps.map((step) => h('li', step))) : null] : null,
    ];
  },

  Tighten() {
    const p = S.edl;
    const fillersLeft = [...S.fillers].filter((i) => p.word_tl[i]).length;
    let maxPause = 0.6, keep = 0.25;
    return [
      h('h2', 'Overview'),
      h('p', `${fmt(p.duration)} kept of ${fmt(S.project.source.duration)} · ${p.cuts.length} cut${p.cuts.length === 1 ? '' : 's'}`),
      h('p.muted.small', { style: { marginTop: '6px' } }, 'Select words in the transcript and press Delete to cut them. Struck-through words are out; select them to restore.'),
      h('h2', 'Fillers'),
      h('div.row', h('span.grow', fillersLeft ? `${fillersLeft} filler word${fillersLeft === 1 ? '' : 's'} (um, uh…) still in` : 'No filler words left'),
        h('button.btn', { disabled: !fillersLeft, onclick: () => ctx.op('remove_fillers', {}, (r) => `Cut ${r.removed_count} fillers (${r.removed_seconds.toFixed(1)}s)`) }, 'Remove')),
      h('h2', 'Pauses'),
      slider('Longer than', { min: 0.3, max: 2, step: 0.05, value: maxPause, show: (v) => v.toFixed(2) + 's', onchange: (v) => { maxPause = v; } }),
      slider('Shorten to', { min: 0.05, max: 0.6, step: 0.05, value: keep, show: (v) => v.toFixed(2) + 's', onchange: (v) => { keep = v; } }),
      h('button.btn', { onclick: () => {
        if (keep >= maxPause) return toast('"Shorten to" has to be less than "Longer than".', 'error');
        ctx.op('remove_pauses', { max_pause: maxPause, keep }, (r) => r.pauses_shortened ? `Shortened ${r.pauses_shortened} pauses (${r.removed_seconds.toFixed(1)}s)` : 'No pauses that long');
      } }, 'Shorten pauses'),
      h('h2', 'Trim'),
      h('div.row',
        h('button.btn', { onclick: () => ctx.op('trim', { start: ctx.now() }) }, 'Start here'),
        h('button.btn', { onclick: () => ctx.op('trim', { end: ctx.now() }) }, 'End here'),
        h('span.muted.small', 'at the playhead')),
    ];
  },

  Captions() {
    const c = S.edl.captions, tpl = c.template;
    const set = (changes) => {
      const args = { template: c.template_name, enabled: c.enabled, overrides: { ...c.overrides }, ...changes };
      if (changes.overrides) args.overrides = { ...c.overrides, ...changes.overrides };
      return ctx.op('set_captions', args);
    };
    const out = [
      h('label.check', h('input', { type: 'checkbox', checked: c.enabled, disabled: !c.template_name,
        onchange: (e) => set({ enabled: e.target.checked }) }), h('span', 'Show captions')),
      h('h2', 'Template'),
      h('div.tpl-grid', S.cfg.captions.map((t) => h('button.tpl' + (t.name === c.template_name ? '.on' : ''),
        { onclick: () => ctx.op('set_captions', { template: t.name, enabled: true, overrides: {} }) },
        h('b', t.name), h('span', t.description)))),
    ];
    if (tpl) {
      out.push(h('h2', 'Adjust'),
        slider('Size', { min: 0.02, max: 0.1, step: 0.002, value: tpl.size, show: (v) => Math.round(v * 1000) / 10 + '%', onchange: (v) => set({ overrides: { size: v } }) }),
        slider('Height', { min: 0.08, max: 0.92, step: 0.01, value: tpl.position.y, show: (v) => Math.round(v * 100) + '%',
          onchange: (v) => set({ overrides: { position: { x: tpl.position.x, y: v } } }) }),
        slider('Words', { min: 1, max: 8, step: 1, value: tpl.max_words, onchange: (v) => set({ overrides: { max_words: v, max_chars: Math.max(tpl.max_chars, v * 7) } }) }),
        field('Text colour', h('input', { type: 'color', value: tpl.color, onchange: (e) => set({ overrides: { color: e.target.value } }) })),
        tpl.animation === 'highlight' ? field('Highlight', h('input', { type: 'color', value: tpl.highlight_color, onchange: (e) => set({ overrides: { highlight_color: e.target.value } }) })) : null,
        h('label.check', h('input', { type: 'checkbox', checked: tpl.uppercase, onchange: (e) => set({ overrides: { uppercase: e.target.checked } }) }), h('span', 'All capitals')),
        Object.keys(c.overrides).length ? h('button.btn.sm', { onclick: () => ctx.op('set_captions', { template: c.template_name, enabled: c.enabled, overrides: {} }) }, 'Reset to template') : null);
    }
    return out;
  },

  Zoom() {
    const p = S.edl, sel = selected('zooms');
    let perMin = 6, scale = 1.15, eased = false;
    const out = [
      h('h2', 'Place automatically'),
      slider('Per minute', { min: 0, max: 14, step: 1, value: perMin, onchange: (v) => { perMin = v; } }),
      slider('Strength', { min: 1.05, max: 1.5, step: 0.01, value: scale, show: (v) => v.toFixed(2) + '×', onchange: (v) => { scale = v; } }),
      h('label.check', h('input', { type: 'checkbox', onchange: (e) => { eased = e.target.checked; } }), h('span', 'Ease in and out (instead of a hard punch-in)')),
      h('div.row',
        h('button.btn', { onclick: () => ctx.op('auto_zoom', { per_minute: perMin, scale, ease: eased ? 0.4 : 0 }, (r) => `${r.zooms_added} zoom${r.zooms_added === 1 ? '' : 's'} placed on emphatic sentences`) }, 'Place zooms'),
        h('button.btn', { onclick: () => {
          const r = ctx.selRange();
          const start = r && r.start != null ? r.start : ctx.now();
          const end = r && r.end != null && r.end > start + 0.5 ? r.end : Math.min(p.duration, start + 2);
          ctx.op('add_zoom', { start, end, scale, ease: eased ? 0.4 : 0 });
        } }, 'Add at playhead')),
      h('h2', `Zooms (${p.zooms.length})`),
      p.zooms.length ? h('div.items', p.zooms.map((z) => itemRow('zooms', z, `${z.scale}× for ${(z.tl_end - z.tl_start).toFixed(1)}s${z.on ? ' · ' + z.on : ''}`, 'Zoom')))
        : h('p.muted.small', 'None yet.'),
    ];
    if (sel) {
      out.push(h('h2', 'Selected zoom'),
        slider('Strength', { min: 1.05, max: 2, step: 0.01, value: sel.scale, show: (v) => v.toFixed(2) + '×', onchange: (v) => ctx.op('update_item', { id: sel.id, fields: { scale: v } }) }),
        slider('Ease', { min: 0, max: 1, step: 0.05, value: sel.ease || 0, show: (v) => v ? v.toFixed(2) + 's' : 'hard cut', onchange: (v) => ctx.op('update_item', { id: sel.id, fields: { ease: v } }) }));
    }
    if (p.zooms.length) out.push(h('button.btn.sm.danger', { style: { marginTop: '12px' }, onclick: () => ctx.op('clear_items', { kind: 'zooms' }) }, 'Remove all zooms'));
    return out;
  },

  Graphics() {
    const p = S.edl, sel = selected('overlays');
    const state = PANELS.Graphics.state || (PANELS.Graphics.state = { template: S.cfg.overlays[0]?.id, params: {} });
    const tpl = S.cfg.overlays.find((t) => t.id === state.template);
    const paramInputs = (t, values, onchange) => Object.entries(t.params).map(([key, spec]) => field(key[0].toUpperCase() + key.slice(1),
      spec.type === 'color'
        ? h('input', { type: 'color', value: values[key] || spec.default || '#ffffff', onchange: (e) => onchange(key, e.target.value) })
        : h('input', { type: 'text', value: values[key] ?? spec.default ?? '', placeholder: spec.description || '', onchange: (e) => onchange(key, e.target.value) })));
    const kinds = { title_card: 'Title card', lower_third: 'Lower third', keyword_callout: 'Keyword callout' };
    const out = [
      h('h2', 'Add a graphic'),
      h('div.tpl-grid', S.cfg.overlays.map((t) => h('button.tpl' + (t.id === state.template ? '.on' : ''),
        { onclick: () => { state.template = t.id; state.params = {}; renderInspector(document.querySelector('.inspector')); } },
        h('b', kinds[t.kind] || t.kind), h('span', t.id.split('/')[1]), h('span', t.description)))),
    ];
    if (tpl) {
      out.push(paramInputs(tpl, state.params, (k, v) => { state.params[k] = v; }),
        h('button.btn', { onclick: () => {
          const params = Object.fromEntries(Object.entries(state.params).filter(([, v]) => v !== ''));
          ctx.op('add_overlay', { template: tpl.id, start: Math.min(ctx.now(), Math.max(0, p.duration - 0.3)), params }, () => 'Added at the playhead');
        } }, 'Add at playhead'));
    }
    out.push(h('h2', `Graphics (${p.overlays.length})`),
      p.overlays.length ? h('div.items', p.overlays.map((o) => itemRow('overlays', o, `${kinds[o.template.split('/')[0]] || o.template} · ${Object.values(o.params)[0] || ''}`, 'Graphics')))
        : h('p.muted.small', 'None yet.'));
    if (sel) {
      const t = S.cfg.overlays.find((x) => x.id === sel.template);
      if (t) out.push(h('h2', 'Selected graphic'),
        paramInputs(t, sel.params, (k, v) => ctx.op('update_item', { id: sel.id, fields: { params: { ...sel.params, [k]: v } } })),
        slider('Duration', { min: 0.5, max: 10, step: 0.1, value: sel.duration, show: (v) => v.toFixed(1) + 's', onchange: (v) => ctx.op('update_item', { id: sel.id, fields: { duration: v } }) }));
    }
    let seconds = 3;
    out.push(h('h2', `B-roll (${p.broll.length})`),
      h('p.muted.small', 'Your own clip or image over the speaker; the voice keeps playing. Covers the selected words, or starts at the playhead.'),
      slider('Length', { min: 0.5, max: 15, step: 0.5, value: seconds, show: (v) => v.toFixed(1) + 's', onchange: (v) => { seconds = v; } }),
      fileRow('media', '/path/to/clip.mp4', (path) => {
        const r = ctx.selRange();
        const start = r && r.start != null && r.kept.length > 1 ? r.start : ctx.now();
        const end = r && r.end != null && r.kept.length > 1 ? r.end : Math.min(p.duration, start + seconds);
        ctx.op('add_broll', { path, start, end }, () => 'B-roll added');
      }),
      p.broll.length ? h('div.items', { style: { marginTop: '8px' } }, p.broll.map((b) => itemRow('broll', b, `${basename(b.path)} · ${(b.tl_end - b.tl_start).toFixed(1)}s`, 'Graphics'))) : null);
    return out;
  },

  Audio() {
    const p = S.edl, m = p.music;
    const setMusic = (changes) => ctx.op('set_music', { path: m.path, gain_db: m.gain_db, duck: m.duck.enabled, duck_amount_db: m.duck.amount_db, offset: m.offset, ...changes });
    let sound = S.cfg.sounds[0], which = 'transitions';
    const out = [h('p.muted.small', 'Music and sound effects are heard in the rendered preview and the export, not in the live preview.'), h('h2', 'Music')];
    if (m) {
      out.push(h('div.row', h('span.grow', basename(m.path)), h('button.btn.sm.danger', { onclick: () => ctx.op('set_music', { path: null }) }, 'Remove')),
        slider('Level', { min: -40, max: -6, step: 1, value: m.gain_db, show: (v) => v + ' dB', onchange: (v) => setMusic({ gain_db: v }) }),
        h('label.check', h('input', { type: 'checkbox', checked: m.duck.enabled, onchange: (e) => setMusic({ duck: e.target.checked }) }), h('span', 'Lower the music while speaking')),
        m.duck.enabled ? slider('Lower by', { min: -24, max: -3, step: 1, value: m.duck.amount_db, show: (v) => v + ' dB', onchange: (v) => setMusic({ duck_amount_db: v }) }) : null);
    } else {
      out.push(fileRow('audio', '/path/to/music.mp3', (path) => ctx.op('set_music', { path }, () => 'Music added')));
    }
    out.push(h('h2', `Sound effects (${p.sfx.length})`),
      field('Sound', selectEl(S.cfg.sounds, sound, (v) => { sound = v; })),
      field('Put on', selectEl([['transitions', 'Cuts with a transition'], ['zooms', 'Every zoom'], ['cuts', 'Every cut'], ['all', 'Cuts and zooms']], which, (v) => { which = v; })),
      h('div.row',
        h('button.btn', { onclick: () => ctx.op('sfx_on_cuts', { sound, which }, (r) => r.sfx_added ? `${r.sfx_added} sounds placed` : 'Nothing to put them on yet') }, 'Place'),
        h('button.btn', { onclick: () => ctx.op('add_sfx', { at: ctx.now(), sound }) }, 'Add at playhead'),
        p.sfx.length ? h('button.btn.sm.danger', { onclick: () => ctx.op('clear_items', { kind: 'sfx' }) }, 'Remove all') : null),
      p.sfx.length ? h('div.items', { style: { marginTop: '8px' } }, p.sfx.slice(0, 40).map((s) => itemRow('sfx', s, basename(s.sound), 'Audio'))) : null);
    return out;
  },

  Transitions() {
    const p = S.edl;
    let type = 'crossfade', perMin = 2;
    const types = S.cfg.transitions.map((t) => [t, t.replace('_', ' ')]);
    const cuts = [...p.cuts].sort((a, b) => Math.abs(b.removed) - Math.abs(a.removed)).slice(0, 60).sort((a, b) => a.time - b.time);
    return [
      h('p.muted.small', 'A transition sits on a cut. They are drawn in the rendered preview and the export, not in the live preview.'),
      h('h2', 'Place automatically'),
      field('Kind', selectEl(types.filter(([t]) => t !== 'cut'), type, (v) => { type = v; })),
      slider('Per minute', { min: 0.5, max: 6, step: 0.5, value: perMin, onchange: (v) => { perMin = v; } }),
      h('div.row',
        h('button.btn', { onclick: () => ctx.op('auto_transitions', { type, per_minute: perMin }, (r) => r.transitions_added ? `${r.transitions_added} placed on the biggest breaks` : 'No cut removed enough footage to count as a break') }, 'Place'),
        h('button.btn.sm.danger', { onclick: () => ctx.op('clear_items', { kind: 'transitions' }) }, 'Remove all')),
      h('h2', `Cuts (${p.cuts.length})`),
      cuts.length ? h('div.items', cuts.map((c) => h('div.item', { onclick: () => ctx.seek(Math.max(0, c.time - 0.8)) },
        h('span.t', fmt(c.time)), h('span.grow.muted.small', `${Math.abs(c.removed).toFixed(1)}s removed`),
        h('span', { onclick: (e) => e.stopPropagation() }, selectEl(types, c.transition, (v) => ctx.op('set_transition', { at: c.time, type: v }))))))
        : h('p.muted.small', 'No cuts yet. Cut something in the transcript, or remove fillers and pauses.'),
      p.cuts.length > cuts.length ? h('p.muted.small', `Showing the ${cuts.length} biggest of ${p.cuts.length}.`) : null,
    ];
  },

  Style() {
    const root = () => document.querySelector('.inspector');
    const st = PANELS.Style.state || (PANELS.Style.state = { current: null, music: '' });
    if (!st.current && S.styles.length) st.current = S.styles[S.styles.length - 1].path;
    const style = S.styles.find((s) => s.path === st.current);
    const analyse = async (reference) => {
      try {
        const res = await runJob('/api/style/analyze', { project: S.project.project, reference });
        S.styles = (await get('/api/styles', { project: S.project.project })).styles;
        st.current = res.path;
        renderInspector(root());
      } catch (err) { toast(err.message, 'error'); }
    };
    const out = [
      h('h2', 'Copy the style of a reference'),
      h('p.muted.small', 'Pick a finished short you like. clipper measures its captions, pacing, zooms, transitions and sound, then applies the closest it has.'),
      fileRow('video', '/path/to/reference.mp4', analyse),
    ];
    if (S.styles.length) out.push(field('Reference', selectEl(S.styles.map((s) => [s.path, s.name]), st.current, (v) => { st.current = v; renderInspector(root()); })));
    if (style) {
      const sp = style.spec, cap = sp.captions || {}, music = (sp.audio || {}).music || {}, sfx = (sp.audio || {}).sfx || {};
      const yn = (v) => (v == null ? 'unclear' : v ? 'yes' : 'no');
      out.push(h('h2', 'What was measured'),
        h('dl.kv',
          h('dt', 'Captions'), h('dd', cap.present ? `${cap.animation}, ${cap.words_per_group} words, ${cap.font.uppercase ? 'capitals' : 'mixed case'}, ${cap.font.weight}` : yn(cap.present)),
          h('dt', 'Shot length'), h('dd', `${sp.pacing.avg_shot_length}s average`),
          h('dt', 'Zooms'), h('dd', `${sp.zoom.punch_ins_per_minute}/min` + (sp.zoom.punch_strength ? ` at ${sp.zoom.punch_strength}×` : '')),
          h('dt', 'Transitions'), h('dd', Object.entries(sp.transitions.counts).map(([k, v]) => `${v} ${k.replace('_', ' ')}`).join(', ') || 'none'),
          h('dt', 'Music'), h('dd', yn(music.present)),
          h('dt', 'Sounds on cuts'), h('dd', yn(sfx.on_cuts))),
        h('img.sheet', { src: mediaUrl({ project: S.project.project, kind: 'style_sheet', name: basename(style.path) }), alt: 'Frames from the reference',
          onerror: (e) => e.target.remove() }),
        h('h2', 'Apply to this edit'),
        music.present && !S.edl.music ? h('div', h('p.muted.small', 'The reference has music. Add your own track to match its level:'),
          fileRow('audio', '/path/to/music.mp3', (path) => { st.music = path; toast('Will use ' + basename(path)); })) : null,
        h('button.btn.primary', { style: { marginTop: '8px' }, onclick: () => ctx.op('apply_style', { style: style.path, music: st.music || null }, () => 'Style applied') }, 'Apply style'));
    }
    const report = (S.edl.notes || []).find((n) => n.kind === 'style');
    if (report) {
      const block = (cls, title, items) => (items.length ? h('div.' + cls, h('h3', title), h('ul', items.map((i) => h('li', i)))) : null);
      out.push(h('h2', 'Result'), h('div.report',
        block('ok', 'Matched', report.matched), block('approx', 'Approximated', report.approximated), block('no', 'Could not match', report.unmatched)));
    }
    return out;
  },

  Output() {
    const p = S.edl;
    const shot = h('div');
    return [
      h('h2', 'Format'),
      field('Shape', selectEl(S.cfg.aspects.map((a) => [a, a === 'source' ? 'Same as source' : a]), p.output.aspect, (v) => ctx.op('set_output', { aspect: v }))),
      h('p.muted.small', `${p.output.width}×${p.output.height} at ${p.output.fps} fps`),
      h('h2', 'Framing'),
      h('p.muted.small', 'The point of the picture that the crop and zooms hold in place. It starts on the speaker\'s face when one is found.'),
      slider('Left–right', { min: 0, max: 1, step: 0.01, value: p.framing.x, show: (v) => Math.round(v * 100) + '%', onchange: (v) => ctx.op('set_framing', { x: v, y: p.framing.y }) }),
      slider('Up–down', { min: 0, max: 1, step: 0.01, value: p.framing.y, show: (v) => Math.round(v * 100) + '%', onchange: (v) => ctx.op('set_framing', { x: p.framing.x, y: v }) }),
      h('h2', 'Check'),
      h('div.row',
        h('button.btn', { onclick: () => {
          shot.replaceChildren(h('p.muted.small', 'Rendering…'));
          const img = h('img.sheet', { alt: 'Exact frame', src: frameUrl({ project: S.project.project, edl: p.name, at: ctx.now().toFixed(3) }),
            onload: () => shot.replaceChildren(img), onerror: () => { shot.replaceChildren(); toast('Could not render that frame.', 'error'); } });
        } }, 'Exact frame at playhead'),
        h('button.btn', { onclick: ctx.renderPreview }, 'Render preview')),
      shot,
      h('h2', 'Export'),
      slider('Loudness', { min: -24, max: -9, step: 1, value: p.output.loudness ?? -14, show: (v) => v + ' LUFS', onchange: (v) => ctx.op('set_output', { loudness: v }) }),
      h('button.btn.primary', { onclick: ctx.exportVideo }, 'Export video'),
      p.preview.export ? h('div', { style: { marginTop: '10px' } }, h('p.muted.small', { style: { wordBreak: 'break-all' } }, 'Last export: ' + p.preview.export),
        h('button.btn.sm', { style: { marginTop: '6px' }, onclick: () => post('/api/reveal', { path: p.preview.export }).catch((e) => toast(e.message, 'error')) }, 'Show in Finder')) : null,
    ];
  },
};
