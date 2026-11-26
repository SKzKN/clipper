// The public pages: the landing page and sign up / sign in. Everything else needs an account.
// Section order follows a typical AI video-editor home page: announcement, nav with a product menu, hero with an
// open bar, preview with feature tabs, facts strip, models, automation, editor, teams, what's next, questions,
// closing call to action, footer.
import { post, h, toast } from './api.js';
import { S, ctx } from './state.js';

// The product line-up. `to` is the landing section that shows it; "Soon" ones are not built yet.
const PRODUCTS = [
  { name: 'Auto-polish', tag: 'New', text: 'A raw take in, a finished video out', to: 'editor' },
  { name: 'Animated captions', text: 'Word-timed captions in your style', to: 'preview' },
  { name: 'Reframe', text: 'Any shape, with the speaker kept in frame', to: 'models' },
  { name: 'Clip finder', text: 'Shorts pulled from long recordings', to: 'models' },
  { name: 'Brand templates', text: 'Your captions, graphics and reference styles', to: 'teams' },
  { name: 'B-roll', text: 'Your own clips and images over the talk', to: 'preview' },
  { name: 'Editor', text: 'Edit the video by editing its text', to: 'editor' },
  { name: 'AI editor', tag: 'New', text: 'Say what to change in plain words', to: 'editor' },
  { name: 'Command line', text: 'Every stage as one command', to: 'auto' },
  { name: 'MCP', text: 'Editing tools any AI agent can call', to: 'auto' },
  { name: 'Social scheduler', tag: 'Soon', text: 'Post to every platform on a schedule', to: 'next' },
  { name: 'Export to XML', tag: 'Soon', text: 'Finish in Premiere Pro or DaVinci Resolve', to: 'next' },
  { name: 'Team workspace', tag: 'Soon', text: 'Shared projects and templates', to: 'next' },
  { name: 'Thumbnails', tag: 'Soon', text: 'A cover frame for every video', to: 'next' },
  { name: 'Speech enhance', tag: 'Soon', text: 'A cleaner voice without new equipment', to: 'next' },
  { name: 'Dubbing', tag: 'Soon', text: 'The same video in another language', to: 'next' },
];

const FEATURES = [
  { id: 'clip', label: 'AI clipping', text: 'Finds the stretches of a long recording that stand on their own and makes each one a short.' },
  { id: 'captions', label: 'AI captions', text: 'Speech recognition times every word, and the captions follow in the template you pick.' },
  { id: 'reframe', label: 'Reframe', text: "Crops to vertical, square or wide around the speaker's face." },
  { id: 'broll', label: 'B-roll', text: 'Lays your own clips and images over the talk while the voice keeps playing.' },
  { id: 'zoom', label: 'Punch-in zooms', text: 'Zooms land on the most emphatic sentences, or wherever you put them.' },
  { id: 'music', label: 'Music', text: 'A music bed that ducks under the voice, with sound effects on the cuts.' },
];

const FAQ = [
  ['How does clipper work?', 'You open a video. clipper checks it, transcribes it, and builds an edit: fillers and long pauses cut, captions on, zooms on the emphatic sentences. You then change anything by editing the transcript or the panels, and export.'],
  ['What kinds of video can I use?', 'One person talking to one camera, up to 1080p. Footage with several cameras, several speakers or a larger picture is refused with the reason, and you can override the check if it is wrong.'],
  ['Which languages are supported?', 'Transcription uses Whisper, which covers many languages. Filler-word removal knows English fillers only so far.'],
  ['Can I add captions?', 'Yes. Captions are timed to each word and come from templates you can adjust: size, position, colours, words per line, capitals. You can also copy the caption style of a short you like.'],
  ['Is it free?', 'For now, yes. The pricing page shows the plans, but paid plans are not open and nothing is charged; every account has everything.'],
  ['What does the AI do, and what leaves my computer?', 'DeepSeek chooses the clips and plans the edit: what to cut, where to zoom, what the title says. To do that, clipper sends it the transcript text and a short description of the edit. The video and audio never leave this computer, and transcription runs locally.'],
  ['Why do I need an account?', 'It keeps your videos separate from anyone else who uses this computer, and it is the base for shared workspaces later. The account is stored on this computer; no email is sent.'],
];

const go = (id) => () => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });

// Every call to action leads to sign-up; a typed path is opened right after.
function start(path) {
  S.pendingPath = (path || '').trim() || null;
  showAuth('signup');
}

function openBar(label = 'Get clips') {
  const input = h('input', { type: 'text', placeholder: 'Paste the path to a video on this computer', spellcheck: false,
    'aria-label': 'Path to a video', onkeydown: (e) => { if (e.key === 'Enter') start(input.value); } });
  const bar = h('div.search', input, h('button.btn.primary', { onclick: () => start(input.value) }, label));
  bar.input = input;
  return bar;
}

// ---- drawn stand-ins for the product (no footage ships with the page) ------

const waveBars = (n, seed = 0.9) => Array.from({ length: n }, (_, i) =>
  h('i', { style: { height: 18 + Math.round(70 * Math.abs(Math.sin(i * seed) * Math.cos(i * 0.31))) + '%' } }));

const figure = () => h('div.demo-shot', h('i.head'), h('i.body'));

function transcriptLines() {
  const words = 'So um the biggest mistake people make is uh never cutting the pauses.'.split(' ');
  const cut = new Set([1, 8]);
  return h('p', words.map((w, i) => [h('span' + (cut.has(i) ? '.cut' : i === 3 || i === 4 ? '.hot' : ''), w), ' ']));
}

function demo() {
  return h('div.demo', { 'data-f': FEATURES[0].id, 'aria-hidden': 'true' },
    h('div.demo-card.d-text', h('span.demo-label', 'Transcript'), transcriptLines()),
    h('div.demo-phone', figure(),
      h('div.demo-broll', h('span', 'your b-roll')),
      h('div.demo-cap', 'THE ', h('b', 'BIGGEST'), ' MISTAKE')),
    h('div.demo-card.d-tracks', h('span.demo-label', 'Timeline'),
      h('div.demo-wave', waveBars(44)),
      h('div.demo-row.r-clips', h('i', { style: { left: '6%', width: '22%' } }), h('i', { style: { left: '38%', width: '17%' } }),
        h('i', { style: { left: '66%', width: '26%' } })),
      h('div.demo-row.r-zoom', h('i', { style: { left: '22%', width: '26%' } })),
      h('div.demo-row.r-broll', h('i', { style: { left: '30%', width: '18%' } })),
      h('div.demo-row.r-music', h('i', { style: { left: 0, width: '100%' } }))));
}

const clipViz = () => h('div.viz.viz-clip', { 'aria-hidden': 'true' },
  h('div.viz-long', h('div.demo-wave', waveBars(64, 0.7)),
    h('i.pick', { style: { left: '8%', width: '18%' } }), h('i.pick', { style: { left: '41%', width: '14%' } }),
    h('i.pick', { style: { left: '70%', width: '20%' } })),
  h('div.viz-shorts', ['0:42', '0:31', '0:58'].map((t) => h('div.mini', figure(), h('span', t)))));

const reframeViz = () => h('div.viz.viz-reframe', { 'aria-hidden': 'true' },
  h('div.wide', figure(), h('i.crop.c916', h('span', '9:16')), h('i.crop.c11', h('span', '1:1'))));

const terminal = () => h('div.term', { 'aria-hidden': 'true' },
  h('div.term-bar', h('i'), h('i'), h('i')),
  h('pre', [
    ['clipper polish talk.mp4', 'one finished video'],
    ['clipper clip long.mp4 -n 5', 'five shorts'],
    ['clipper style reference.mp4', "measure a short's look"],
    ['claude mcp add clipper -- clipper mcp', 'hand the tools to an agent'],
  ].map(([cmd, note]) => h('span.line', h('b', '$ '), cmd, h('em', '   # ' + note), '\n'))));

function editorMock() {
  const para = (text, cuts = []) => h('p', text.split(' ').map((w, i) => [h('span' + (cuts.includes(i) ? '.cut' : ''), w), ' ']));
  return h('div.mock', { 'aria-hidden': 'true' },
    h('div.mock-top', h('b', '← talk.mp4'), h('span.grow'), h('span.fake-btn', 'Render preview'), h('span.fake-btn.primary', 'Export')),
    h('div.mock-main',
      h('div.mock-player', h('div.demo-phone', figure(), h('div.demo-cap', 'NOBODY ', h('b', 'HAS'), ' TIME'))),
      h('div.mock-text.d-text',
        para('So um today I want to show you the three biggest mistakes people make when they start editing videos.', [0, 1]),
        para('The first mistake is uh cutting too slowly, nobody has time for that.', [4]),
        para('The second mistake is ignoring sound, great audio matters more than you think.')),
      h('div.mock-side',
        h('div.mock-tabs', h('span.on', 'Tighten'), h('span', 'Captions'), h('span', 'Zoom'), h('span', 'Graphics')),
        h('span.demo-label', 'Fillers'), h('div.fake-row', h('span', '3 filler words still in'), h('span.fake-btn', 'Remove')),
        h('span.demo-label', 'Pauses'), h('div.fake-slider', h('i', { style: { width: '34%' } })), h('div.fake-slider', h('i', { style: { width: '58%' } })))),
    h('div.mock-tl', h('div.demo-wave', waveBars(90, 0.55)),
      h('div.demo-row.r-zoom', h('i', { style: { left: '12%', width: '14%' } }), h('i', { style: { left: '55%', width: '20%' } })),
      h('div.demo-row.r-music', h('i', { style: { left: 0, width: '100%' } }))));
}

// ---- landing page -----------------------------------------------------------

export function showLanding() {
  document.title = 'clipper: talking-head video editor';
  const cfg = S.cfg;
  const tag = (p) => (p.tag ? h('span.tag' + (p.tag === 'Soon' ? '.soon' : ''), p.tag) : null);

  // nav with the product menu
  const menu = h('div.menu', PRODUCTS.map((p) => h('button.menu-item', { onclick: () => { closeMenu(); go(p.to)(); } },
    h('b', p.name, tag(p)), h('span', p.text))));
  const featBtn = h('button.navlink', { 'aria-expanded': 'false', onclick: (e) => { e.stopPropagation(); toggleMenu(); } }, 'Features ', h('span.caret'));
  const menuWrap = h('div.menu-wrap', featBtn, menu);
  const toggleMenu = (open = !menuWrap.classList.contains('open')) => {
    menuWrap.classList.toggle('open', open);
    featBtn.setAttribute('aria-expanded', String(open));
  };
  const closeMenu = () => toggleMenu(false);
  const outside = (e) => { if (!menuWrap.isConnected) document.removeEventListener('click', outside); else if (!menuWrap.contains(e.target)) closeMenu(); };
  document.addEventListener('click', outside);
  menuWrap.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeMenu(); featBtn.focus(); } });

  // hero
  const phrases = ['a finished video', 'a week of shorts', 'captioned clips'];
  const pill = h('span.pill', phrases[0]);
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
    let i = 0;
    const timer = setInterval(() => {
      if (!pill.isConnected) return clearInterval(timer);
      pill.classList.add('out');
      setTimeout(() => { i = (i + 1) % phrases.length; pill.textContent = phrases[i]; pill.classList.remove('out'); }, 260);
    }, 2800);
  }
  const heroBar = openBar();
  const stage = demo();
  const featText = h('p.feat-text', FEATURES[0].text);
  const tabs = FEATURES.map((f, i) => h('button' + (i ? '' : '.on'), { onclick: (e) => {
    for (const b of e.currentTarget.parentNode.children) b.classList.toggle('on', b === e.currentTarget);
    stage.dataset.f = f.id;
    featText.textContent = f.text;
  } }, f.label));

  // open bar that follows the page while no other open bar (hero, closing call to action) or the footer is in view
  const sticky = h('div.sticky-bar', openBar());
  const cta = h('div.cta', h('h2', 'Get started with clipper'), h('div.open-row', openBar()),
    h('p.small', 'Free. Single speaker, one camera, up to 1080p.'));
  const inView = new Set([heroBar]);
  const watcher = new IntersectionObserver((entries, obs) => {
    if (!sticky.isConnected) return obs.disconnect();
    for (const e of entries) { if (e.isIntersecting) inView.add(e.target); else inView.delete(e.target); }
    sticky.classList.toggle('show', inView.size === 0);
  });

  const facts = [[cfg.captions.length, 'caption templates'], [cfg.overlays.length, 'graphic templates'],
    [cfg.transitions.length - 1, 'transitions'], [cfg.aspects.length - 1, 'output shapes'], [0, 'videos uploaded']];
  const soon = PRODUCTS.filter((p) => p.tag === 'Soon');
  const shapes = cfg.aspects.filter((a) => a !== 'source').join(', ');

  ctx.show(h('div.page.landing',
    cfg.pricing.promo
      ? h('button.announce.deal', { onclick: showPricing }, h('b', cfg.pricing.promo.headline), ` until ${cfg.pricing.promo.ends_label}. `, h('u', 'See pricing'), ' →')
      : h('button.announce', { onclick: go('editor') }, 'New: an editor with live preview, where the transcript is the edit. ', h('u', 'See it'), ' →'),
    h('header.nav',
      h('button.brand', { onclick: () => window.scrollTo({ top: 0, behavior: 'smooth' }) }, 'clipper', h('i', '.')),
      h('nav.nav-mid', menuWrap,
        h('button.navlink', { onclick: go('auto') }, 'Automation'),
        h('button.navlink', { onclick: go('teams') }, 'For teams'),
        h('button.navlink', { onclick: go('faq') }, 'Questions')),
      h('span.grow'),
      h('button.navlink.always', { onclick: showPricing }, 'Pricing'),
      h('button.navlink.always', { onclick: () => showAuth('signin') }, 'Sign in'),
      h('button.btn.primary', { onclick: () => start() }, 'Sign up, it’s free')),

    h('section.hero',
      h('div.eyebrow', 'AI video editor and clipping tool for talking-head video'),
      h('h1', 'Turn one raw take into ', h('br'), pill, ' by editing text'),
      h('p.lead', 'Paste the path to a raw take. clipper transcribes it, cuts the fillers and pauses, captions it, sizes it, and pulls shorts out of long recordings.'),
      cfg.problems.length ? h('div.notice.bad', cfg.problems.join(' ')) : null,
      h('div.open-row', heroBar, h('span.or', 'or'), h('button.btn.lg', { onclick: () => start(heroBar.input.value) }, 'Auto-polish')),
      h('div', { id: 'preview' }, stage),
      h('div.feat-tabs', tabs), featText),

    h('section.band.center.facts',
      h('p.facts-title', 'Runs on your computer, with everything included'),
      h('div.facts-row', facts.map(([n, label]) => h('div', h('b', String(n)), h('span', label))))),

    h('section.band.center', { id: 'models' },
      h('div.eyebrow', 'Editing models'),
      h('h2', 'It reads the words, the picture and the sound'),
      h('p.sub', 'Speech recognition finds every word, face detection frames the speaker, and loudness shows where the emphasis is.'),
      h('div.duo',
        h('div.pcard', clipViz(), h('h3', 'Clip finder'),
          h('p', 'Scores every stretch of a long recording for a complete thought with a strong opening, and proposes the shorts. With DeepSeek connected, the model reads the whole transcript and chooses them instead. Talking-head footage only, for now.')),
        h('div.pcard', reframeViz(), h('h3', 'Reframe'),
          h('p', `Crops to ${shapes} around the speaker's face and holds that point through every zoom. One speaker and one camera, for now.`)))),

    h('section.band.center', { id: 'auto' },
      h('div.eyebrow', 'Workflow automation'),
      h('h2', 'Hand the whole edit to one command, or one agent'),
      h('p.sub', 'Everything the editor does is also a command and an MCP tool, and all three write the same edit file.'),
      h('div.panel', terminal(),
        h('div.trio',
          h('div', h('h3', 'Auto edit'), h('p', 'One command checks, transcribes, tightens, captions, zooms and exports.')),
          h('div', h('h3', 'Agent tools'), h('p', 'Claude Code reads the transcript, decides the cuts, places the graphics and looks at rendered frames.')),
          h('div', h('h3', 'Auto style'), h('p', 'Point at a short you like. clipper measures its captions, pacing and zooms and applies the closest it has.'))))),

    h('section.band.center', { id: 'editor' },
      h('div.eyebrow', 'AI editor'),
      h('h2', 'Let it make the first pass. Keep the final say.'),
      h('p.sub', 'Tell the AI editor what to change in plain words, or delete words yourself to cut the video. Play it back without rendering.'),
      editorMock()),

    h('section.band.center', { id: 'teams' },
      h('div.eyebrow', 'clipper for teams'),
      h('h2', 'One look across every video, whoever makes it'),
      h('p.sub', 'Templates and styles are plain files, so a whole team can cut to the same look.'),
      h('div.trio.cards',
        h('div.pcard', h('div.viz.viz-chips', cfg.captions.slice(0, 5).map((c) => h('span.chip', c.name))),
          h('h3', 'Brand templates'), h('p', 'Caption and graphic templates in your fonts and colours, or a style measured from a reference short.')),
        h('div.pcard', h('div.viz.viz-people', ['A', 'M', 'J'].map((c) => h('i', c)), h('i.more', '+')),
          h('h3', 'Team workspace', h('span.tag.soon', 'Soon')), h('p', 'Accounts are in. Shared projects and shared templates come next.')),
        h('div.pcard', h('div.viz.viz-chips', ['Command line', 'MCP', 'Edit file (JSON)'].map((c) => h('span.chip.ok', c))),
          h('h3', 'Workflow integration'), h('p', 'Drive clipper from scripts or agents, and pick the result up in the editor.')))),

    h('section.band.center', { id: 'next' },
      h('h2', 'What’s coming next'),
      h('p.sub', 'Not built yet, and listed here so you know what is.'),
      h('div.carousel', soon.map((p) => h('div.pcard.small', h('span.tag.soon', 'Soon'), h('h3', p.name), h('p', p.text))))),

    h('section.band.center', { id: 'faq' },
      h('h2', 'Got questions?'),
      h('div.faq', FAQ.map(([q, a]) => h('details', h('summary', q), h('p', a))))),

    h('section.band', cta),

    h('footer.foot',
      h('div.foot-cols',
        h('div', h('button.brand', { onclick: () => window.scrollTo({ top: 0, behavior: 'smooth' }) }, 'clipper', h('i', '.')),
          h('p.small', 'Your video files stay on this computer.')),
        h('div', h('h3', 'Product'), PRODUCTS.filter((p) => p.tag !== 'Soon').slice(0, 7).map((p) => h('button.link', { onclick: go(p.to) }, p.name))),
        h('div', h('h3', 'Automation'), h('button.link', { onclick: go('auto') }, 'Command line'), h('button.link', { onclick: go('auto') }, 'MCP for agents'),
          h('button.link', { onclick: go('auto') }, 'Reference styles')),
        h('div', h('h3', 'Help'), h('button.link', { onclick: showPricing }, 'Pricing'), h('button.link', { onclick: go('faq') }, 'Questions'), h('button.link', { onclick: go('next') }, 'What’s next'),
          h('button.link', { onclick: () => showAuth('signin') }, 'Sign in')))),
    sticky));
  for (const el of [heroBar, cta, document.querySelector('footer.foot')]) watcher.observe(el);
}

// ---- sign up / sign in --------------------------------------------------------

export function showAuth(mode = 'signup') {
  const signup = mode === 'signup';
  document.title = (signup ? 'Sign up' : 'Sign in') + ' · clipper';
  const field = (label, attrs) => {
    const input = h('input', attrs);
    return { input, el: h('label.auth-field', h('span', label), input) };
  };
  const name = field('Name', { type: 'text', autocomplete: 'name', required: true, maxLength: 80 });
  const email = field('Email', { type: 'email', autocomplete: signup ? 'email' : 'username', required: true, spellcheck: false });
  const password = field(signup ? 'Password (8 or more characters)' : 'Password',
    { type: 'password', autocomplete: signup ? 'new-password' : 'current-password', required: true, minLength: signup ? 8 : 1 });
  const error = h('div.notice.bad', { role: 'alert', style: { display: 'none' } });
  const submit = h('button.btn.primary.lg.block', { type: 'submit' }, signup ? 'Create my account' : 'Sign in');
  const form = h('form.auth-form', { onsubmit: async (e) => {
    e.preventDefault();
    submit.disabled = true;
    error.style.display = 'none';
    try {
      await post('/api/auth/' + (signup ? 'signup' : 'signin'),
        { name: name.input.value, email: email.input.value, password: password.input.value });
      await ctx.enter();
    } catch (err) {
      error.textContent = err.message;
      error.style.display = '';
      submit.disabled = false;
    }
  } }, signup ? name.el : null, email.el, password.el, error, submit);

  const pending = S.pendingPath ? h('p.small.muted.after', 'Next: open ', h('b', S.pendingPath.split('/').pop())) : null;
  ctx.show(h('div.page',
    h('header.nav', h('button.brand', { onclick: () => { S.pendingPath = null; showLanding(); }, title: 'Back to the start' }, 'clipper', h('i', '.')),
      h('span.grow'),
      h('span.muted.small.hide-narrow', signup ? 'Already have an account?' : 'New to clipper?'),
      h('button.link', { onclick: () => showAuth(signup ? 'signin' : 'signup') }, signup ? 'Sign in' : 'Sign up')),
    h('main.auth',
      h('div.card.auth-card',
        h('h1', signup ? 'Sign up to start editing' : 'Sign in to clipper'),
        pending, form,
        h('p.small.muted', signup
          ? 'Your account is stored on this computer. No email is sent.'
          : ['Forgot your password? Run ', h('code', 'clipper account reset you@example.com'), ' in a terminal.'])))));
  (signup ? name : email).input.focus();
}

// ---- pricing ----------------------------------------------------------------------

export function showPricing() {
  document.title = 'Pricing · clipper';
  const { plans, compare, currency, promo } = S.cfg.pricing;
  const user = S.cfg.user;
  const money = (n) => (Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`);
  let yearly = true;

  const action = (plan) => {
    if (plan.id === 'free') {
      return user ? h('button.btn.block', { disabled: true }, 'Your plan')
        : h('button.btn.block', { onclick: () => showAuth('signup') }, 'Create an account');
    }
    if (!user) return h('button.btn.primary.block', { onclick: () => showAuth('signup') }, 'Sign up free');
    const asked = user.wants_plan === plan.id;
    return h('button.btn.block' + (asked ? '' : '.primary'), { onclick: async (e) => {
      e.currentTarget.disabled = true;
      try {
        const res = await post('/api/plan/interest', { plan: asked ? null : plan.id });
        S.cfg.user = res.user;
        toast(asked ? 'Removed from the list.' : `Noted: you'll hear when ${plan.name} opens.`
          + (res.with_offer ? ` Your request is marked with the ${res.with_offer} offer.` : ''), 'ok');
        showPricing();
      } catch (err) { toast(err.message, 'error'); }
    } }, asked ? 'On the list ✓' : 'Tell me when it opens');
  };

  // the price to pay now: the dated offer (if this plan is in it) on top of the monthly or yearly price
  const onOffer = (plan) => plan.promo_monthly != null;
  const current = (plan) => (onOffer(plan) ? (yearly ? plan.promo_yearly : plan.promo_monthly) : (yearly ? plan.yearly : plan.monthly));
  const price = (plan) => {
    if (plan.monthly == null) return h('div.price', h('b', 'Custom'));
    const now = current(plan);
    return h('div.price', now < plan.monthly ? h('s', money(plan.monthly)) : null,
      h('b', money(now)), h('span', plan.monthly ? ` ${currency}/mo` : ` ${currency}`));
  };
  const priceNote = (plan) => {
    if (plan.monthly == null) return 'Sized to your team';
    if (!plan.monthly) return 'No card needed';
    if (yearly && plan.yearly < plan.monthly) return `${money(current(plan) * 12)} billed yearly`;
    return yearly && plan.yearly_note ? plan.yearly_note : `${money(current(plan))} billed monthly`;
  };
  const card = (plan) => h('div.plan' + (plan.popular ? '.popular' : '') + (user && user.plan === plan.id ? '.mine' : ''),
    plan.popular ? h('span.tag.best', 'Most chosen') : null,
    h('h3', plan.name, onOffer(plan) ? h('span.tag.deal', `${promo.name} −${promo.percent}%`) : null),
    h('p.muted.small', plan.tagline), price(plan), h('p.muted.small.note', priceNote(plan)),
    action(plan),
    h('ul.ticks', plan.features.map((f) => (typeof f === 'string'
      ? h('li' + (/:$/.test(f) ? '.lead-in' : ''), f) : h('li', f.text, h('span.tag.soon', 'Soon'))))));

  const cell = (v) => h('td', v === true ? h('i.yes', { title: 'Included' }) : v === false ? h('span.no', '–')
    : v === 'soon' ? h('span.tag.soon', 'Soon') : v);
  const table = h('div.compare-wrap', h('table.compare',
    h('thead', h('tr', h('th'), plans.map((p) => h('th', p.name,
      h('span', p.monthly == null ? 'Custom' : p.monthly ? `${money(p.promo_monthly ?? p.monthly)}/mo` : '$0'))))),
    compare.map((g) => h('tbody', h('tr.group', h('th', { colSpan: plans.length + 1 }, g.group)),
      g.rows.map((r) => h('tr', h('th', r.label), r.values.map(cell)))))));

  const grid = h('div.plans');
  const free = h('div');
  const monthlyBtn = h('button', { onclick: () => setYearly(false) }, 'Monthly');
  const yearlyBtn = h('button', { onclick: () => setYearly(true) }, 'Yearly');
  function setYearly(v) {
    yearly = v;
    monthlyBtn.classList.toggle('on', !v);
    yearlyBtn.classList.toggle('on', v);
    grid.replaceChildren(...plans.filter((p) => p.id !== 'free').map(card));
    free.replaceChildren(...plans.filter((p) => p.id === 'free').map((p) => h('div.plan.wide',
      h('div', h('h3', p.name), h('p.muted.small', p.tagline), price(p), action(p)),
      h('ul.ticks', p.features.map((f) => h('li', f))))));
  }
  setYearly(true);

  const top = user ? ctx.nav() : h('header.nav',
    h('button.brand', { onclick: showLanding }, 'clipper', h('i', '.')),
    h('span.grow'),
    h('button.navlink.always', { onclick: () => showAuth('signin') }, 'Sign in'),
    h('button.btn.primary', { onclick: () => showAuth('signup') }, 'Sign up, it’s free'));
  ctx.show(h('div.page', top,
    promo ? h('div.promo-band', h('b', promo.headline), ` on monthly and yearly billing. Ends ${promo.ends_label}.`) : null,
    h('main.pricing',
      h('button.link', { onclick: () => (user ? ctx.home() : showLanding()) }, user ? '← Your videos' : '← Back'),
      h('h1', 'Choose a plan'),
      h('div.bill', h('div.seg', monthlyBtn, yearlyBtn), h('span.muted.small', 'Save up to 50% with yearly billing')),
      h('div.notice', h('b', 'Paid plans are not open yet. '), 'Nothing is charged, and every account can use everything for now. ',
        'The minutes are counted but not enforced.'),
      grid, free,
      h('h2.compare-title', 'Compare all plans'),
      table,
      h('p.muted.small.center', 'A minute is one minute of source video opened in clipper. ',
        'Features marked Soon are not built yet.'))));
}
