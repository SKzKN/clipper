// Talking to the local clipper server.
const TOKEN = document.querySelector('meta[name="clipper-token"]').content;

let signedOut = null;
export const onSignedOut = (fn) => { signedOut = fn; };

async function request(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { 'X-Clipper-Token': TOKEN, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* not json */ }
  if (res.status === 401 && signedOut) signedOut();
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}

export const get = (path, params) => request('GET', path + (params ? '?' + new URLSearchParams(params) : ''));
export const post = (path, body) => request('POST', path, body || {});

// URL of a file the server may serve (the token rides along because media tags cannot send headers).
export const mediaUrl = (params) => '/api/media?' + new URLSearchParams({ ...params, t: TOKEN });
export const frameUrl = (params) => '/api/frame?' + new URLSearchParams({ ...params, t: TOKEN, _: Date.now() });
export const templateUrl = (id) => `/tpl/${id}/template.html?t=${TOKEN}`;

// ---- jobs: long operations report progress while they run ---------------
const jobsEl = () => document.getElementById('jobs');

export class Rejected extends Error {
  constructor(result) { super('rejected'); this.result = result; }
}

// Start a job (the endpoint may also answer at once without one) and wait for it.
export async function runJob(path, body) {
  const started = await post(path, body);
  if (!started.job) return started;
  const card = document.createElement('div');
  card.className = 'job';
  card.innerHTML = '<div class="job-label"></div><div class="job-msg"></div><div class="bar"><i></i></div>';
  jobsEl().append(card);
  try {
    for (;;) {
      const job = await get('/api/job/' + started.job);
      card.querySelector('.job-label').textContent = job.label;
      card.querySelector('.job-msg').textContent = job.message || '';
      const bar = card.querySelector('.bar');
      bar.classList.toggle('indeterminate', job.progress == null);
      bar.firstChild.style.width = job.progress == null ? '' : Math.round(job.progress * 100) + '%';
      if (job.state === 'done') return job.result;
      if (job.state === 'rejected') throw new Rejected(job.result);
      if (job.state === 'failed') throw new Error(job.error);
      await new Promise((r) => setTimeout(r, 400));
    }
  } finally {
    card.remove();
  }
}

export function toast(message, kind = 'info', ms = 4500) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = message;
  document.getElementById('toasts').append(el);
  setTimeout(() => el.remove(), kind === 'error' ? ms * 2 : ms);
}

// Small DOM helper: h('div.cls', {onclick}, child, 'text', ...)
export function h(spec, attrs, ...children) {
  const [tag, ...classes] = spec.split('.');
  const el = document.createElement(tag || 'div');
  if (classes.length) el.className = classes.join(' ');
  if (attrs && (attrs instanceof Node || typeof attrs !== 'object' || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k in el && k !== 'list') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const fmt = (t) => {
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, '0')}`;
};
