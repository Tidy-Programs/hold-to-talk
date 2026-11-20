/* Hold to talk - app window. Plain JavaScript, no build step, nothing
   loaded from the internet. All user text goes into the page through
   textContent (never innerHTML). Talks to api.py: X-HTT-Key on every call.

   Rules this file keeps:
   - After any answer from the app (success or failure) every control shows
     the app's real state; a failed change keeps what the user typed.
   - Messages are plain sentences; technical detail stays in the log. */
'use strict';

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'style') el.style.cssText = v;   // CSSOM: allowed by the CSP
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'value') el.value = v;
      else if (k === 'checked') el.checked = !!v;
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

// replaceChildren without the "null" text a null argument would become.
function put(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter(k => k !== null && k !== undefined && k !== false));
}

const ICONS = {
  home: 'M4 11.5 12 5l8 6.5V20a1 1 0 0 1-1 1h-5v-6h-4v6H5a1 1 0 0 1-1-1z',
  history: 'M12 7v5l3 2M3.5 12a8.5 8.5 0 1 0 2.5-6M3 4v4h4',
  book: 'M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5zM5 19.5A1.5 1.5 0 0 0 6.5 21H19M9 7h6',
  words: 'M4 18 8.5 6h1L14 18M5.6 14h6.8M15 13.5c0-1.7 1.2-2.5 2.6-2.5 1.5 0 2.4.9 2.4 2.3V18m0-3.2c-.6-.5-1.5-.8-2.4-.8-1.4 0-2.6.7-2.6 2 0 1.2 1 2 2.3 2 1.3 0 2.2-.6 2.7-1.4',
  snippet: 'M8 6 3 12l5 6M16 6l5 6-5 6M13.5 4l-3 16',
  gear: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19.4 13.5l1.6 1.2-2 3.4-1.9-.7a7 7 0 0 1-1.7 1L15 20.5h-4l-.4-2.1a7 7 0 0 1-1.7-1l-1.9.7-2-3.4 1.6-1.2a7 7 0 0 1 0-2l-1.6-1.2 2-3.4 1.9.7a7 7 0 0 1 1.7-1L11 3.5h4l.4 2.1a7 7 0 0 1 1.7 1l1.9-.7 2 3.4-1.6 1.2a7 7 0 0 1 0 2z',
  copy: 'M9 9h10v11H9zM5 15V4h10',
  edit: 'M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4',
  trash: 'M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13',
  chev: 'M9 6l6 6-6 6',
  arrow: 'M4 12h15M13 6l6 6-6 6',
  search: 'M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM20 20l-4.8-4.8',
  warn: 'M12 4 2.5 20h19zM12 10v4.5M12 17.2v.1',
  info: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 11v6M12 7.6v.1',
  // a four-pointed sparkle (not a ring of rays, which reads as "loading")
  spark: 'M10 3.5c.6 3.6 2.4 5.4 6 6-3.6.6-5.4 2.4-6 6-.6-3.6-2.4-5.4-6-6 3.6-.6 5.4-2.4 6-6zM18 14.5c.3 1.6 1 2.4 2.5 2.6-1.5.3-2.2 1-2.5 2.6-.3-1.6-1-2.3-2.5-2.6 1.5-.2 2.2-1 2.5-2.6z',
  chip: 'M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4',
  wave: 'M3 12h2M7 8v8M11 5v14M15 9v6M19 11v2',
  plus: 'M12 5v14M5 12h14',
};

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', name === 'chev' ? 'ic chev' : 'ic');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICONS[name]);
  svg.append(p);
  return svg;
}

function iconButton(name, label, onclick, extra = '') {
  return h('button', { class: 'icon-btn ' + extra, type: 'button', title: label,
                       'aria-label': label, onclick }, icon(name));
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

const q = s => '“' + s + '”';

function isTextField(el) {
  return !!el && (el.isContentEditable || el.tagName === 'TEXTAREA' ||
    (el.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'submit', 'range', 'color'].includes(el.type)));
}

const fmt = {
  num: n => (n || 0).toLocaleString(),
  secs(s) {
    if (s === null || s === undefined) return '';
    if (s < 1) return Math.round(s * 1000) + ' ms';
    return (s < 10 ? s.toFixed(1) : Math.round(s)) + ' s';
  },
  minutes(s) {
    s = s || 0;
    if (s < 60) return Math.round(s) + ' s';
    if (s < 3600) return Math.round(s / 60) + ' min';
    return (s / 3600).toFixed(1) + ' h';
  },
  when(ts) {
    const d = new Date(ts * 1000), now = new Date();
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const diff = (day - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 864e5;
    if (diff === 0) return 'Today ' + time;
    if (diff === 1) return 'Yesterday ' + time;
    return d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' }) + ' ' + time;
  },
  plural: (n, one, many) => fmt.num(n) + ' ' + (n === 1 ? one : many),
};

// Word-level diff for the stage trace (what each step changed).
function diffWords(a, b) {
  const A = a.split(/(\s+)/).filter(Boolean), B = b.split(/(\s+)/).filter(Boolean);
  if (A.length * B.length > 250000) return [{ t: b, k: 'same' }];
  const n = A.length, m = B.length;
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  const push = (t, k) => {
    const last = out[out.length - 1];
    if (last && last.k === k) last.t += t; else out.push({ t, k });
  };
  while (i < n && j < m) {
    if (A[i] === B[j]) { push(A[i], 'same'); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) { push(A[i], 'del'); i++; }
    else { push(B[j], 'add'); j++; }
  }
  while (i < n) push(A[i++], 'del');
  while (j < m) push(B[j++], 'add');
  return out;
}

function diffNode(a, b) {
  const p = h('p', { class: 'step-text' });
  for (const part of diffWords(a, b)) {
    if (part.k === 'same') p.append(part.t);
    else if (!part.t.trim()) { if (part.k === 'add') p.append(part.t); }
    else p.append(h(part.k === 'add' ? 'ins' : 'del', { text: part.t }));
  }
  return p;
}

// The first changed stretch between what was pasted and the correction, as
// a dictionary entry suggestion ("heard" -> "wanted"), or null.
function changedPair(before, after) {
  const parts = diffWords(before, after);
  let heard = '', wanted = '', started = false;
  for (const p of parts) {
    if (p.k === 'same') { if (started && p.t.trim()) break; if (started) { heard += p.t; wanted += p.t; } continue; }
    started = true;
    if (p.k === 'del') heard += p.t; else wanted += p.t;
  }
  const strip = s => s.trim().replace(/^[^\p{L}\p{N}]+/u, '').replace(/[.,!?;:]+$/u, '').trim();
  heard = strip(heard); wanted = strip(wanted);
  return heard && wanted && heard !== wanted ? { heard, wanted } : null;
}

// ---------------------------------------------------------------- API
const KEY_STORE = 'htt.key';
const api = {
  key: null,
  async call(method, path, body, opts = {}) {
    let res;
    try {
      res = await fetch(path, {
        method, cache: 'no-store', keepalive: !!opts.keepalive,
        headers: Object.assign({ 'X-HTT-Key': this.key || '' },
                               body ? { 'Content-Type': 'application/json' } : {}),
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      connection.lost();
      throw Object.assign(new Error('The app is not answering. Check that it is still running.'), { status: 0 });
    }
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (res.status === 401) { gate('signedout'); }
    if (!res.ok) throw Object.assign(new Error(data.error || 'Something went wrong. Details are in the log.'), { status: res.status, data });
    return data;
  },
  get(path) { return this.call('GET', path); },
  post(path, body, opts) { return this.call('POST', path, body || {}, opts); },
};

// A failed change still tells us the app's real state: show that.
function applyTruth(e) {
  const d = e && e.data;
  if (!d || !S.config) return false;
  if (d.config) Object.assign(S.config, d.config);
  if (d.settings) S.config.settings = d.settings;
  return !!(d.config || d.settings);
}

// ---------------------------------------------------------------- state
const S = {
  state: null, live: null, health: null, config: null, feed: null,
  level: 0, view: 'home', capture: null, captureFor: 'hotkey', micLevel: 0,
};

const VIEWS = [
  ['home', 'Home', 'home'],
  ['history', 'History', 'history'],
  ['dictionary', 'Dictionary', 'book'],
  ['vocabulary', 'Vocabulary', 'words'],
  ['snippets', 'Snippets', 'snippet'],
  ['settings', 'Settings', 'gear'],
];

// ---------------------------------------------------------------- toasts, dialogs
const UNDO_MS = 10000;
const undoable = [];          // open toasts with an Undo, newest last

function toast(text, opts = {}) {
  const el = h('div', { class: 'toast' + (opts.error ? ' err' : ''), role: opts.error ? 'alert' : 'status' }, h('span', { text }));
  let timer = null, closed = false;
  const entry = {};
  const close = reason => {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    const hadFocus = el.contains(document.activeElement);
    el.remove();
    const i = undoable.indexOf(entry);
    if (i >= 0) undoable.splice(i, 1);
    if (reason !== 'undo' && opts.onexpire) opts.onexpire();
    if (hadFocus && opts.returnFocus?.isConnected) opts.returnFocus.focus();
  };
  if (opts.action) {
    entry.undo = () => { close('undo'); opts.onaction(); };
    el.append(h('button', { class: 'link', type: 'button', text: opts.action, 'aria-keyshortcuts': 'Control+Z',
      title: opts.action + ' (Ctrl+Z)', onclick: entry.undo }),
      h('span', { class: 'hint', 'aria-hidden': 'true', text: 'Ctrl+Z' }));
    entry.close = close;
    undoable.push(entry);
  }
  const ms = opts.ms || (opts.action ? UNDO_MS : opts.error ? 7000 : 3500);
  const arm = () => { clearTimeout(timer); timer = setTimeout(() => close('time'), ms); };
  el.addEventListener('mouseenter', () => clearTimeout(timer));
  el.addEventListener('mouseleave', arm);
  el.addEventListener('focusin', () => clearTimeout(timer));
  el.addEventListener('focusout', arm);
  $('#toasts').append(el);
  arm();
  return { close, el };
}

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'z'
      && !isTextField(document.activeElement) && !$('#dialog').open) {
    const last = undoable[undoable.length - 1];
    if (last) { e.preventDefault(); last.undo(); }
  }
});

function fail(e) { toast(e.message || String(e), { error: true }); }

function dialog(build, wide = false) {
  const dlg = $('#dialog');
  const back = document.activeElement;
  dlg.className = wide ? 'wide' : '';
  dlg.replaceChildren();
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (done) return; done = true; resolve(v); if (back?.isConnected) back.focus(); };
    const close = v => { finish(v); dlg.close(); };
    dlg.append(...build(close));
    dlg.onclose = () => finish(undefined);
    dlg.showModal();
  });
}

function confirmBox(title, text, okLabel, danger = true) {
  return dialog(close => [
    h('div', { class: 'dlg-body' }, h('h2', { text: title }), h('p', { text })),
    h('div', { class: 'dlg-foot' },
      h('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: () => close(false), autofocus: true }),
      h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), type: 'button', text: okLabel, onclick: () => close(true) })),
  ]);
}

// ---------------------------------------------------------------- gate (signed out / not running)
function closeWindow(note) {
  window.close();
  setTimeout(() => { note.textContent = 'Close it with the × in its top-right corner.'; }, 400);
}

function gate(kind) {
  const name = S.state?.info?.name || document.title;
  const words = {
    signin: ['Open this window from the app',
      'This window has to be opened from the app: click its icon in the notification area of the taskbar (near the clock).'],
    signedout: ['This window is out of date',
      `${name} was restarted, so this window is no longer connected to it. Click its icon in the notification area to open a fresh window, then close this one.`],
    offline: [`${name} isn’t answering`,
      `${name} was closed or restarted. If it is running, click its icon in the notification area for a fresh window. Otherwise start it again. This window keeps trying to reconnect.`],
  }[kind];
  $('#gate-title').textContent = words[0];
  $('#gate-text').textContent = words[1];
  const note = h('p', { class: 'small muted', 'aria-live': 'polite' });
  const acts = [];
  if (kind === 'offline') acts.push(h('button', { class: 'btn primary', type: 'button', text: 'Try again now', onclick: () => connection.retryNow() }));
  acts.push(h('button', { class: 'btn', type: 'button', text: 'Close this window', onclick: () => closeWindow(note) }));
  put($('#gate-actions'), acts, note);
  $('#gate').hidden = false;
  $('#gate').dataset.kind = kind;
  $('#shell').hidden = true;
  closeDialog();
}

function closeDialog() { const d = $('#dialog'); if (d.open) d.close(); }

// ---------------------------------------------------------------- the Terms of Use
// One screen, two uses: 'gate' (the terms are not accepted yet: it covers
// the window, with "I agree" and "Quit") and 'view' (Settings > About: the
// same text, with "Close"). The text is TERMS.md, fetched with the key.
const termsScreen = {
  mode: null, busy: false, shown: 0,
  inline(text) {                      // **bold** only; everything else is text
    return text.split('**').map((t, i) => i % 2 ? h('b', { text: t }) : t);
  },
  render(markdown) {                  // a small, safe reader for TERMS.md
    const out = [];
    let list = null;
    for (const block of markdown.split(/\n\s*\n/)) {
      const lines = block.split('\n').map(l => l.trimEnd()).filter(l => l.trim());
      if (!lines.length) continue;
      if (lines.every(l => l.trim() === '---')) { out.push(h('hr')); continue; }
      if (lines[0].startsWith('# ') || /^Version \S+ /.test(lines[0])) continue;   // shown in the card's header
      if (lines[0].startsWith('## ')) {
        out.push(h('h2', { text: lines[0].slice(3) }));
        lines.shift();
        if (!lines.length) continue;
      }
      if (lines.every(l => l.startsWith('- '))) {
        list = h('ul'); lines.forEach(l => list.append(h('li', {}, this.inline(l.slice(2)))));
        out.push(list); continue;
      }
      out.push(h('p', {}, this.inline(lines.join(' '))));
    }
    return out;
  },
  hide() {
    const el = $('#terms');
    const was = this.mode;
    this.mode = null; this.shown++;
    el.hidden = true;
    if (was === 'gate' && $('#gate').hidden && connection.alive) $('#shell').hidden = false;
  },
  async show(mode) {
    if (this.mode === mode && !$('#terms').hidden) return;
    const el = $('#terms'), actions = $('#terms-actions');
    const token = ++this.shown;
    this.mode = mode; this.busy = false;
    el.dataset.mode = mode;
    el.hidden = false;
    if (mode === 'gate') { $('#shell').hidden = true; closeDialog(); }
    $('#terms-version').textContent = '';
    put($('#terms-body'), h('p', { text: 'Loading the terms…' }));
    const say = h('span', { class: 'small muted grow', 'aria-live': 'polite' });
    const quit = h('button', { class: 'btn', type: 'button', text: 'Quit', onclick: () => this.quit(say) });
    const close = h('button', { class: 'btn primary', type: 'button', text: 'Close', onclick: () => this.hide() });
    const agree = h('button', { class: 'btn primary', type: 'button', text: 'I agree to the Terms of Use', disabled: true });
    const note = h('p', { class: 'small agree-note', text: 'By agreeing you accept the whole terms. This includes the sections on no warranty, limits of liability, assumption of risk, indemnity and disputes (sections 9 to 14).' });
    put(actions, ...(mode === 'gate' ? [note, say, quit, agree] : [say, close]));
    if (mode === 'gate') say.textContent = 'Hold to talk will not dictate until you agree. Read the terms, then choose I agree to the Terms of Use, or Quit.';
    $('#terms-title').focus({ preventScroll: true });
    let data;
    try { data = await api.get('/api/terms'); }
    catch (e) {
      if (token !== this.shown) return;
      put($('#terms-body'), h('p', { text: e.message }));
      if (mode === 'gate') say.textContent = 'The terms could not be shown, so they cannot be accepted. You can quit and install the app again.';
      return;
    }
    if (token !== this.shown) return;                  // closed, or accepted elsewhere, meanwhile
    const first = data.text.split('\n', 1)[0].trim();
    $('#terms-version').textContent = first;
    put($('#terms-body'), this.render(data.text));
    $('#terms-body').scrollTop = 0;
    if (mode === 'gate') {
      agree.disabled = false;
      agree.addEventListener('click', () => this.agree(data.version, agree, say));
      // Focus the text, not "I agree": a stray Enter or Space (the window opens
      // while the owner may be typing) must not accept terms nobody has read,
      // and the arrow keys / Page Down scroll the text at once.
      $('#terms-body').focus({ preventScroll: true });
    } else {
      close.focus();
    }
  },
  async agree(version, button, say) {
    if (this.busy) return;
    this.busy = true; button.disabled = true; say.textContent = 'Saving…';
    try {
      await api.post('/api/terms/accept', { version });
    } catch (e) {
      this.busy = false; button.disabled = false; say.textContent = e.message;
      return;
    }
    this.acceptedAt = Date.now();
    this.hide();
    toast('Thank you. Dictation is on: hold ' + hotkeyWords() + ' and speak.');
    refreshState();
    if (S.live) S.live.terms = Object.assign({}, S.live.terms, { pending: false });
    renderMini(); views.home.live();
  },
  async quit(say) {
    say.textContent = 'Closing Hold to talk…';
    try { await api.post('/api/quit', {}); } catch (e) { say.textContent = e.message; return; }
    say.textContent = 'Hold to talk is closing. You can close this window.';
    setTimeout(() => window.close(), 600);
  },
  // Called whenever the app's live state arrives.
  sync() {
    const pending = !!S.live?.terms?.pending;
    // (a live update queued before the acceptance may still say "pending")
    if (pending && Date.now() - (this.acceptedAt || 0) < 3000) return;
    if (pending) this.show('gate');
    else if (this.mode === 'gate') this.hide();
  },
};
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && termsScreen.mode === 'view') { e.preventDefault(); termsScreen.hide(); }
});

// ---------------------------------------------------------------- live connection
const connection = {
  alive: false, retry: 0, ctrl: null, wake: null, stopped: false,
  lost() {
    if (!this.alive) return;
    this.alive = false;
    renderMini();
  },
  retryNow() { this.retry = Math.min(this.retry, 1); if (this.wake) this.wake(); },
  async run() {
    while (!this.stopped) {
      try {
        this.ctrl = new AbortController();
        const res = await fetch('/api/events', { headers: { 'X-HTT-Key': api.key },
                                                 cache: 'no-store', signal: this.ctrl.signal });
        if (res.status === 401) { this.stopped = true; gate('signedout'); return; }
        if (!res.ok || !res.body) throw new Error('stream');
        if (!this.alive) {
          const reconnect = this.retry > 0;
          this.alive = true; this.retry = 0;
          if (reconnect) { await loadAll(); rerender(); }
          if ($('#gate').dataset.kind === 'offline') $('#gate').hidden = true;
          if ($('#gate').hidden && !(termsScreen.mode === 'gate')) $('#shell').hidden = false;
          renderMini();
        }
        const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
        let buf = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += value;
          let i;
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
            let ev = 'message', data = '';
            for (const line of chunk.split('\n')) {
              if (line.startsWith('event: ')) ev = line.slice(7);
              else if (line.startsWith('data: ')) data += line.slice(6);
            }
            if (data) { try { onEvent(ev, JSON.parse(data)); } catch (e) { console.error(e); } }
          }
        }
      } catch (e) { /* fall through to retry */ }
      this.lost();
      this.retry++;
      if (this.retry > 6) gate('offline');
      await new Promise(r => { this.wake = r; setTimeout(r, Math.min(5000, 400 * this.retry)); });
      this.wake = null;
    }
  },
};

function onEvent(ev, data) {
  switch (ev) {
    case 'live': {
      const before = S.live;
      S.live = data;
      if (S.state) S.state.live = data;
      if (!before || before.status !== data.status || before.paused !== data.paused
          || before.terms?.pending !== data.terms?.pending) refreshState();
      termsScreen.sync();
      renderMini();
      views.home.live();
      views.settings.live();
      break;
    }
    case 'level': S.level = data.v; break;
    case 'health': {
      // A microphone plugged in or out: the Settings list must follow.
      if (S.health && JSON.stringify(S.health.mic) !== JSON.stringify(data.mic)) refreshConfig();
      S.health = data;
      if (S.config && data.autostart && JSON.stringify(S.config.autostart) !== JSON.stringify(data.autostart)) {
        S.config.autostart = data.autostart;
        views.settings.autostart();
      }
      refreshState();
      views.home.live(); views.settings.live();
      break;
    }
    case 'dictation':
      if (S.feed) S.feed.unshift(data);
      views.home.dictation(data);
      refreshState();
      if (S.view === 'history') views.history.load();
      break;
    case 'history':
      if (data.cleared) S.feed = [];
      else if (S.feed) S.feed = S.feed.filter(i => i.history_id !== data.deleted && Math.abs(i.ts - data.ts) > 1e-6);
      views.home.renderFeed();
      refreshState();
      break;
    case 'notice':
      if (S.state) S.state.notices.unshift(data);
      if (data.kind === 'mic') refreshState();      // the problem may be over
      views.home.live();
      break;
    case 'config': refreshConfig(); refreshState(); break;
    case 'capture': S.capture = data; views.settings.capture(); break;
    case 'miclevel': S.micLevel = data.v; views.settings.micLevel(); break;
    case 'mictest': views.settings.micDone(data); break;
  }
}

const refreshState = debounce(async () => {
  try {
    S.state = await api.get('/api/state'); S.live = S.state.live; S.health = S.state.health;
    renderMini(); views.home.live(); views.home.stats();
  } catch (e) { /* the stream reports connection problems */ }
}, 150);

const refreshConfig = debounce(async () => {
  try {
    const old = S.config;
    S.config = await api.get('/api/config');
    renderNav();
    const same = k => old && JSON.stringify(old[k]) === JSON.stringify(S.config[k]);
    // Settings saves update S.config.settings themselves; re-rendering the
    // page for them would only drop focus and the "Saved" notes.
    if (S.view === 'settings' && same('settings') && same('devices') && same('autostart')) return;
    views[S.view].config?.();
  } catch (e) { /* the stream reports connection problems */ }
}, 120);

async function loadAll() {
  const [state, config, feed] = await Promise.all([
    api.get('/api/state'), api.get('/api/config'), api.get('/api/feed')]);
  S.state = state; S.live = state.live; S.health = state.health;
  S.config = config; S.feed = feed.items;
}

// Show freshly loaded data on the current page (after a reconnect), without
// disturbing an editor that is open.
function rerender() {
  renderNav(); renderMini();
  if (S.view === 'home') { views.home.live(); views.home.stats(); views.home.renderFeed(); }
  else if (S.view === 'history') views.history.load();
  else views[S.view].config?.();
}

// Usage numbers and "Running on this computer" go stale otherwise (a window
// left open overnight would still show yesterday as "Today").
window.addEventListener('focus', () => { if (connection.alive) refreshState(); });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && connection.alive) refreshState();
  if (document.visibilityState === 'hidden') views.history.flush();
});
window.addEventListener('pagehide', () => views.history.flush());
setInterval(() => { if (connection.alive && document.visibilityState === 'visible') refreshState(); }, 60000);

// ---------------------------------------------------------------- words for states
function hotkeyWords() { return S.live?.hotkey?.plain || 'the hotkey'; }

function speechFailure(detail) {
  const d = String(detail || '').toLowerCase();
  if (/cuda|cudnn|cublas|gpu|out of memory/.test(d))
    return 'The graphics card could not run the speech model. Try again; if it keeps happening, the log says why.';
  if (/connect|resolve|network|internet|timed out|timeout|huggingface|download|http|ssl|certificate|proxy|getaddrinfo|offline/.test(d))
    return 'The speech model could not be downloaded. Check your internet connection, then try again. It is downloaded only once; after that it works offline.';
  if (/no space|disk|permission|access is denied|errno 13|errno 28|read-only/.test(d))
    return 'The speech model could not be stored on this computer: the disk may be full or a folder not accessible. Free up some space, then try again.';
  return 'The speech model could not be loaded. Try again, or open the log to see what went wrong.';
}

function stateWords() {
  const l = S.live;
  if (!l) return { key: 'loading', title: 'Connecting…', sub: '', short: 'Connecting…' };
  if (l.terms?.pending) return { key: 'paused', title: 'Off until you accept the terms', short: 'Terms not accepted',
    sub: 'Dictation stays off, and your hold-to-talk key does nothing, until you read and accept the Terms of Use.' };
  if (l.status === 'loading' && l.download) {
    const pct = Number.isInteger(l.download.percent) ? ' ' + l.download.percent + '%' : '';
    return { key: 'loading', title: 'Downloading the speech model' + pct, short: 'Downloading…' + pct,
      sub: 'Downloading the speech model' + (l.download.size ? ' (' + l.download.size + ')' : '') + '… this can take several minutes. It is downloaded only once; after that it works offline.' };
  }
  if (l.status === 'loading') return { key: 'loading', title: 'Getting ready', short: 'Getting ready…',
    sub: 'Loading the speech model. This takes a few seconds, longer the very first time.' };
  if (l.status === 'failed' && l.failure) return { key: 'failed', title: 'Can’t dictate right now', short: 'Can’t dictate',
    // The plain reason and what to do; the technical detail is in the log.
    sub: (l.detail || 'The speech model could not be downloaded.') + ' ' + (l.failure.advice || '') + (l.failure.auto_retry
      ? ' Hold to talk will try again by itself, or choose Try again.' : ' Choose Try again.') };
  if (l.status === 'failed') return { key: 'failed', title: 'Can’t dictate right now', short: 'Can’t dictate',
    sub: speechFailure(l.detail) };
  if (l.capturing) return { key: 'working', title: 'Waiting for a key', short: 'Waiting for a key',
    sub: 'Press the key or mouse button you want to use (Settings).' };
  if (l.paused) return { key: 'paused', title: 'Paused', short: 'Paused',
    sub: 'Your hotkey does nothing until you resume.' };
  if (l.activity === 'listening') return { key: 'listening', title: 'Listening', short: 'Listening',
    sub: 'Let go of ' + hotkeyWords() + ' to paste.' };
  if (l.activity === 'working') return { key: 'working', title: 'Writing it down', short: 'Writing it down',
    sub: 'Turning your speech into text…' };
  return { key: 'ready', title: 'Ready', sub: null, short: 'Ready' };
}

const CLEANUP_STATES = {
  off: ['Off', ''], starting: ['Starting…', 'working'], unreachable: ['Ollama isn’t running', 'bad'],
  missing: ['Model not installed', 'bad'], idle: ['Installed, loads on next use', ''],
  loaded: ['Loaded and ready', 'ready'],
};
// cl: the cleanup health ({state, blocked, listener_blocked, listener, ...}).
// A refused listener is checked before "blocked": it is a different problem.
function cleanupWords(state, blocked, cl) {
  if (cl?.listener_blocked) return [cl.listener === 'unknown' ? 'Can’t tell which Windows account answers' : 'Another Windows account answers', 'bad'];
  if (blocked) return ['Blocked: its address is not on this computer', 'bad'];
  if (state && !CLEANUP_STATES[state]) return ['Unavailable', 'bad'];
  return CLEANUP_STATES[state] || ['Checking…', ''];
}

function cleanupStatusNote(status) {
  if (!status) return null;
  if (status === 'cleaned') return 'tidied by the AI model';
  if (status === 'unchanged') return 'nothing to change';
  if (status === 'disabled') return 'off';
  if (status.startsWith('short')) return 'skipped, too short to need it';
  if (status.startsWith('long')) return 'skipped, too long';
  if (status === 'timeout') return 'skipped, the model took too long';
  if (status === 'empty input') return 'skipped, nothing to tidy';
  if (status.startsWith('untrusted listener')) return 'skipped, another Windows account is answering on the AI cleanup port, so nothing was sent';
  if (status.startsWith('not ready')) return 'skipped, still checking whether Ollama is running';
  if (status.startsWith('Ollama unreachable')) return 'skipped, Ollama isn’t running' + (/checking again/.test(status) ? ' (the app keeps checking in the background)' : '');
  if (status.startsWith('blocked')) return 'skipped, its address is not on this computer, so nothing was sent';
  if (status.startsWith('rejected')) return 'the AI’s rewrite was not used; your own words were pasted';
  if (/^model not found/i.test(status)) return 'skipped, the AI model isn’t installed in Ollama (the app keeps checking in the background)';
  if (/^HTTP 404/.test(status) || /not found/i.test(status)) return 'skipped, the AI model isn’t installed in Ollama';
  if (/^HTTP 5/.test(status)) return 'skipped, the AI model stopped with an error (details in the log)';
  if (/^HTTP/.test(status)) return 'skipped, Ollama refused the request (details in the log)';
  if (status.startsWith('bad response')) return 'skipped, the AI model’s answer could not be used';
  return 'skipped because of a problem (details in the log)';
}

// ---------------------------------------------------------------- sidebar
function renderNav() {
  const nav = $('#nav');
  nav.replaceChildren(...VIEWS.map(([id, label, ic]) => {
    let count = null;
    if (S.config && id === 'dictionary') count = S.config.dictionary.length;
    if (S.config && id === 'vocabulary') count = S.config.vocabulary.length;
    if (S.config && id === 'snippets') count = S.config.snippets.length;
    // aria-label: the text is hidden in very narrow windows (icons only).
    return h('a', { href: '#' + id, 'aria-current': S.view === id ? 'page' : null, 'aria-label': count ? `${label} (${count})` : label, title: label },
      icon(ic), h('span', { class: 'label', text: label }), count ? h('span', { class: 'count', text: count, 'aria-hidden': 'true' }) : null);
  }));
}

function browserNote() {
  // Shown only when the window is a tab in the ordinary browser (uiwin.py).
  const el = $('#browser-note');
  if (el) el.hidden = S.state?.window !== 'browser';
}

function renderMini() {
  browserNote();
  const el = $('#mini-status');
  if (!connection.alive && S.live) {
    put(el, h('span', { class: 'dot failed' }), h('div', { class: 'grow' },
      h('b', { text: 'Reconnecting…' }), h('span', { class: 'sub', text: 'The app is not answering' })));
    return;
  }
  const w = stateWords();
  const btn = S.live && S.live.status === 'ready' && !S.live.terms?.pending
    ? h('button', { class: 'btn small', type: 'button', text: S.live.paused ? 'Resume' : 'Pause',
        onclick: () => setPaused(!S.live.paused) })
    : null;
  put(el, h('span', { class: 'dot ' + w.key }),
    h('div', { class: 'grow' }, h('b', { text: w.short || w.title }), h('span', { class: 'sub', text: 'Hold ' + hotkeyWords() })), btn);
}

async function setPaused(paused) {
  try { await api.post('/api/pause', { paused }); } catch (e) { fail(e); }
}

// ---------------------------------------------------------------- the pill (in-page twin of overlay.py)
function makePill(extra = '') {
  const pill = h('div', { class: 'pill ' + extra, 'aria-hidden': 'true' });
  for (let i = 0; i < 11; i++) pill.append(h('i'));
  return pill;
}

function setPillColors(pill, colors) {
  if (!colors) return;
  pill.style.setProperty('--pill-fg', colors.color);
  pill.style.setProperty('--pill-bg', colors.background);
}

function pillMode(pill, mode) {
  // mode: 'bars' (levels) | 'dots' (working) | 'idle'
  if (pill.dataset.mode === mode) return;
  pill.dataset.mode = mode;
  pill.classList.toggle('dots', mode === 'dots');
  pill.replaceChildren(...Array.from({ length: mode === 'dots' ? 3 : 11 }, () => h('i')));
  pill.levels = new Array(11).fill(0);
}

function pillFrame(pill, level) {
  // Same mapping as overlay.py: min(1, level * 14) ** 0.6, height ±36%.
  if (pill.dataset.mode !== 'bars' && pill.dataset.mode !== 'idle') return;
  pill.levels.push(Math.pow(Math.min(1, level * 14), 0.6));
  pill.levels.shift();
  const bars = pill.children;
  for (let i = 0; i < bars.length; i++) {
    bars[i].style.height = Math.max(3, pill.levels[i] * 34 * 0.72) + 'px';
  }
}

// One animation loop drives every visible pill.
const pills = new Set();
function animate() {
  const t = performance.now() / 1000;
  for (const pill of pills) {
    if (!pill.isConnected) { pills.delete(pill); continue; }
    let level = 0;
    if (pill.source === 'live') level = S.live?.activity === 'listening' ? S.level : 0;
    else if (pill.source === 'demo') level = 0.004 + 0.012 * Math.abs(Math.sin(t * 4.7) + Math.sin(t * 11.3 + 1) * 0.7);
    else if (pill.source === 'mic') level = S.micLevel;
    pillFrame(pill, level);
  }
  requestAnimationFrame(animate);
}

// ---------------------------------------------------------------- views
const views = {};
function page(title, sub) {
  return h('div', { class: 'page' }, h('header', { class: 'page-head' }, h('h1', { text: title, tabindex: '-1' }),
    sub ? h('p', { text: sub }) : null));
}

function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }

async function turnOffCleanup() {
  const res = await saveSetting({ cleanup_enabled: false });
  if (res) toast('AI cleanup is off. Your words are pasted as heard, with your dictionary and snippets applied.');
}

// Language suggestions the user dismissed (kept for this window's session).
const dismissedHints = new Set();
try { (sessionStorage.getItem('htt.hint') || '').split(',').filter(Boolean).forEach(c => dismissedHints.add(c)); } catch (e) { /* not available */ }

async function checkAgain(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  try { await api.post('/api/health/check', {}); } catch (e) { fail(e); }
  setTimeout(() => { if (btn?.isConnected) { btn.disabled = false; btn.textContent = 'Check again'; } }, 2500);
}

// ---- Home
views.home = {
  el: null,
  mount(main) {
    const p = page('Home');
    p.querySelector('.page-head').classList.add('sr-only');
    this.hero = h('section', { class: 'card hero', 'aria-live': 'polite' });
    this.notices = h('div', { class: 'notices' });
    this.statsEl = h('div', { class: 'stats-wrap' });
    this.engines = h('div', { class: 'card engines' });
    this.feedEl = h('div');
    p.append(this.hero, this.notices, this.statsEl,
      h('div', { class: 'section-title' }, h('h2', { text: 'Running on this computer' })), this.engines,
      h('div', { class: 'section-title' }, h('h2', { text: 'Recent dictations' }),
        h('span', { class: 'aside', text: 'Open one to see each step' })), this.feedEl);
    main.append(p);
    this.el = p;
    this.live(); this.stats(); this.renderFeed();
  },
  live() {
    if (!this.el || !this.el.isConnected) return;
    const w = stateWords(), l = S.live || {};
    const sub = w.sub !== null ? h('p', { class: 'hero-sub', text: w.sub })
      : h('p', { class: 'hero-sub' }, 'Hold ', h('kbd', { text: hotkeyWords() }), ' and speak. Let go to paste.');
    if (!this.pill) { this.pill = makePill('big'); this.pill.source = 'live'; pills.add(this.pill); }
    setPillColors(this.pill, l.overlay);
    pillMode(this.pill, w.key === 'working' ? 'dots' : (w.key === 'listening' ? 'bars' : 'idle'));
    this.pill.classList.toggle('off', ['paused', 'failed', 'loading'].includes(w.key));
    pills.add(this.pill);
    const actions = h('div', { class: 'row actions' });
    if (l.terms?.pending) actions.append(h('button', { class: 'btn primary', type: 'button', text: 'Read and accept the terms', onclick: () => termsScreen.show('gate') }));
    else if (l.paused) actions.append(h('button', { class: 'btn primary', type: 'button', text: 'Resume', onclick: () => setPaused(false) }));
    if (l.status !== 'failed') this.retryMsg = '';
    if (l.status === 'failed') {
      // The answer survives re-renders of the card (live updates).
      const note = h('span', { class: 'small muted retry-note', 'aria-live': 'polite', text: this.retryMsg || '' });
      const say = t => { this.retryMsg = t; const n = this.hero.querySelector('.retry-note'); if (n) n.textContent = t; };
      const retry = h('button', { class: 'btn primary', type: 'button', text: 'Try again', onclick: async () => {
        retry.disabled = true; say('Trying again…');
        try { await api.post('/api/engine/retry', {}); say(''); refreshState(); }
        catch (e) { say(e.message); retry.disabled = false; }
      } });
      actions.append(retry);
      if (l.held_seconds != null) {
        // The kept recording is transcribed by the app as soon as the model loads.
        const again = h('button', { class: 'btn', type: 'button', text: 'Try that recording again', onclick: async () => {
          again.disabled = true; say('Trying again. Your last recording is written down as soon as the speech model has loaded.');
          try { await api.post('/api/engine/retry', {}); refreshState(); }
          catch (e) { say(e.message); again.disabled = false; }
        } });
        actions.append(again);
      }
      actions.append(h('button', { class: 'btn', type: 'button', text: 'Show log', onclick: showLog }), note);
    }
    if (S.state?.info?.dry_run) actions.append(h('span', { class: 'badge warn', text: 'Test mode: nothing is pasted' }));
    const busy = this.hero.contains(document.activeElement) && this.hero.dataset.key === w.key;
    this.hero.className = 'card hero ' + w.key;
    if (!busy) {
      this.hero.dataset.key = w.key;
      put(this.hero, h('div', { class: 'hero-text' }, h('p', { class: 'hero-state', text: w.title }), sub,
        actions.childElementCount ? actions : null), this.pill);
    }
    this.renderNotices();
    this.renderEngines();
  },
  renderNotices() {
    const out = [];
    const add = (kind, title, text, ...actions) => out.push(h('div', { class: 'notice ' + kind },
      icon(kind === 'info' ? 'info' : 'warn'),
      h('div', { class: 'body' }, h('b', { text: title }), text ? (text instanceof Node ? text : h('p', { text })) : null,
        actions.length ? h('div', { class: 'row acts' }, actions) : null)));
    const btn = (label, onclick, primary) => h('button', { class: 'btn small' + (primary ? ' primary' : ''), type: 'button', text: label, onclick });
    const link = (label, hash) => h('a', { class: 'btn small', href: hash, text: label });
    const hl = S.health || {}, l = S.live || {};
    const status = l.status || 'ready';
    if (hl.mic?.state === 'none') {
      const again = btn('Check again', () => checkAgain(again));
      add('bad', 'No microphone found', 'Dictation needs a microphone. Connect one; it is found within a few seconds.', again, link('Microphone settings', '#settings&mic'));
    }
    if (hl.mic?.state === 'missing') add('bad', 'Your microphone isn’t connected', `${q(hl.mic.device)} is not available. Plug it in, or use another microphone.`,
      btn('Use the Windows default', async () => { const r = await saveSetting({ input_device: null }); if (r) toast('Using the Windows default microphone'); }, true),
      link('Choose another', '#settings&mic'));
    const notices = S.state?.notices || [];
    // A problem moving the user's data at start-up: always first, never aged out.
    for (const n of notices.filter(n => n.kind === 'data').slice(0, 1))
      add('bad', 'Your data needs attention', n.message, h('button', { class: 'btn small', type: 'button', text: 'Show log', onclick: showLog }));
    // Shown for as long as the app reports it (not just once).
    const micProblem = l.mic_problem || S.state?.mic_problem;
    if (micProblem && hl.mic?.state !== 'none' && hl.mic?.state !== 'missing')
      add('bad', 'There is a problem with the microphone', micProblem, link('Microphone settings', '#settings&mic'));
    const c = hl.cleanup?.state, blocked = !!hl.cleanup?.blocked, refused = !!hl.cleanup?.listener_blocked;
    // While the speech model is not usable, "dictation still works" would be wrong.
    const after = status === 'ready' ? 'Dictation still works: your words are pasted as heard, with your dictionary and snippets applied.'
      : 'Once dictation is ready, your words will be pasted as heard, with your dictionary and snippets applied.';
    if (status !== 'failed' && refused) {
      // Another account answers on the port (or we cannot tell whose it is):
      // nothing is sent. Checked before "blocked", which means something else.
      const unknown = hl.cleanup.listener === 'unknown';
      const detail = hl.cleanup.listener_detail || (unknown ? 'the Windows account answering on the AI cleanup port could not be determined' : 'another Windows account is answering on the AI cleanup port');
      const sentence = detail.charAt(0).toUpperCase() + detail.slice(1) + '. Nothing is sent to it, so your words are pasted as heard.';
      const trust = S.state?.cleanup?.trust_listener, canTrust = S.config?.settings?.cleanup_trust_available !== false;
      const body = h('div', null, h('p', { text: sentence }),
        unknown ? h('p', { text: 'That usually means Ollama runs as a Windows service. If it is yours, you can allow it: AI cleanup will then send your words to whatever program answers on that port, whichever Windows account it belongs to.' }) : null);
      add('warn', 'AI cleanup is unavailable', body,
        unknown && canTrust && !trust ? btn('Allow this program', async () => { const r = await saveSetting({ cleanup_trust_listener: true }); if (r) { toast('AI cleanup will use it'); refreshState(); checkAgain(); } }, true) : null,
        btn('Turn off AI cleanup', turnOffCleanup), link('AI cleanup settings', '#settings&ai'));
    } else if (status !== 'failed' && blocked) {
      const host = S.state?.cleanup?.host || 'another computer';
      add('warn', 'AI cleanup is blocked', `Its address (${host}) is not on this computer, so nothing is sent there. ${after}`,
        btn('Turn off AI cleanup', turnOffCleanup), link('AI cleanup settings', '#settings&ai'));
    } else if (status !== 'failed' && c === 'unreachable') {
      add('warn', 'AI cleanup is unavailable', 'AI cleanup needs Ollama, a free program that runs AI models on this computer, and it isn’t running. ' + after,
        btn('Turn off AI cleanup', turnOffCleanup), link('What is Ollama?', '#settings&ai'));
    } else if (status !== 'failed' && c && !CLEANUP_STATES[c]) {
      // A state this page does not know (a newer engine): say so plainly.
      const why = hl.cleanup.detail || hl.cleanup.message || hl.cleanup.status || c;
      add('warn', 'AI cleanup is unavailable', `AI cleanup reports: ${why}. ${after}`,
        btn('Turn off AI cleanup', turnOffCleanup), link('AI cleanup settings', '#settings&ai'));
    }
    if (status !== 'failed' && c === 'missing') {
      const m = hl.cleanup.model;
      const again = btn('Check again', () => checkAgain(again));
      add('warn', 'The AI cleanup model isn’t installed',
        h('p', null, `Ollama is running, but the model ${q(m)} isn’t installed in it, so your words are pasted as heard. To install it, open a terminal and run `,
          h('code', { text: 'ollama pull ' + m }), ', or turn AI cleanup off.'),
        btn('Turn off AI cleanup', turnOffCleanup), again);
    }
    // The app kept the last recording that could not be transcribed.
    if (l.held_seconds != null && status === 'ready') {
      const again = btn('Try that recording again', async () => {
        again.disabled = true;
        try { await api.post('/api/recording/retry', {}); toast('Trying that recording again…'); }
        catch (e) { fail(e); again.disabled = false; }
      }, true);
      add('warn', 'Your last recording was not written down', `The app kept it (${fmt.secs(l.held_seconds)} of speech) for a few minutes, in memory only. You can try it again.`, again);
    }
    // Always dictating in one language: suggest saying so (a little faster).
    const hint = l.language_hint;
    if (hint && !S.config?.settings?.language && !dismissedHints.has(hint.code)) {
      const nm = languageName(hint.code, hint.name);
      add('info', `You always dictate in ${nm}`, 'Choosing it makes each dictation a little faster.',
        btn(`Use ${nm}`, async () => { const r = await saveSetting({ language: hint.code }); if (r) toast(`Language set to ${nm}`); }, true),
        btn('Not now', () => { dismissedHints.add(hint.code); try { sessionStorage.setItem('htt.hint', [...dismissedHints].join(',')); } catch (e) { /* not kept */ } this.renderNotices(); }));
    }
    if (l.hook === 'fallback') add('warn', 'Your mouse button isn’t blocked', `${hotkeyWords()} still does its usual job (such as going Back) while you dictate. Pausing and resuming tries again.`);
    // The latest thing that happened to a dictation attempt (a too-brief
    // press is not worth a notice; the microphone has its own one above).
    const recent = notices.filter(n => !['short', 'data'].includes(n.kind) && !(n.kind === 'mic' && micProblem)
      && Date.now() / 1000 - n.ts < 600).slice(0, 1);
    for (const n of recent) {
      if (n.kind === 'paste') add('bad', 'Not pasted · ' + fmt.when(n.ts), n.message + ' You can copy it from Recent dictations below or from History.', link('Open History', '#history'));
      else add(['mic', 'engine', 'error'].includes(n.kind) ? 'bad' : 'info', 'Last attempt · ' + fmt.when(n.ts), n.message);
    }
    const sig = JSON.stringify([hl.mic, hl.cleanup, l.hook, status, l.held_seconds, l.language_hint, [...dismissedHints], S.config?.settings?.language, recent, micProblem, notices.filter(n => n.kind === 'data').length]);
    if (this.notices.contains(document.activeElement) && this.notices.dataset.sig === sig) return;
    this.notices.dataset.sig = sig;
    put(this.notices, out);
  },
  renderEngines() {
    const sp = S.state?.speech || {}, cl = S.state?.cleanup || {}, hl = S.health || {};
    const status = S.live?.status;
    let speechName, speechHow;
    if (sp.name) {
      speechName = sp.name;
      speechHow = sp.fallback
        ? [h('span', { class: 'dot working' }), 'Running on the processor (slower): ' + sp.fallback]
        : [h('span', { class: 'dot ready' }), (sp.device === 'cuda' ? 'Graphics card' : 'Processor') + (sp.compute ? ' · ' + sp.compute : '')];
    } else if (status === 'failed') {
      speechName = 'Not loaded';
      speechHow = [h('span', { class: 'dot failed' }), 'Could not load'];
    } else {
      // Which model is used depends on the hardware found while loading.
      speechName = 'Choosing a model…';
      speechHow = [h('span', { class: 'dot loading' }), S.live?.download ? 'Downloading' : 'Getting ready'];
    }
    const [cw, ck] = cl.enabled ? cleanupWords(hl.cleanup?.state, hl.cleanup?.blocked, hl.cleanup) : ['Off', ''];
    put(this.engines,
      h('div', { class: 'engine' }, icon('wave'), h('div', null,
        h('div', { class: 'what', text: 'Speech recognition (Whisper)' }),
        h('div', { class: 'name', text: speechName }),
        h('div', { class: 'how' }, ...speechHow))),
      h('div', { class: 'engine' }, icon('spark'), h('div', null,
        h('div', { class: 'what', text: 'AI cleanup (Ollama)' }),
        h('div', { class: 'name', text: cl.model || '…' }),
        h('div', { class: 'how' }, h('span', { class: 'dot ' + ck }), cw))));
  },
  stats() {
    if (!this.el || !this.el.isConnected) return;
    const s = S.state?.stats;
    if (!s) { this.statsEl.replaceChildren(); return; }
    const tile = (label, num, sub) => h('div', { class: 'card stat' },
      h('div', { class: 'label', text: label }), h('div', { class: 'num', text: num }), h('div', { class: 'sub', text: sub }));
    const off = S.state.history_enabled === false
      ? h('p', { class: 'small muted stats-note' }, 'Saving history is off, so new dictations are not counted. ',
        h('a', { class: 'link', href: '#settings&history', text: 'Change in Settings' })) : null;
    put(this.statsEl, h('div', { class: 'stats' },
      tile('Today', fmt.num(s.today.words) + ' words', fmt.plural(s.today.dictations, 'dictation', 'dictations')),
      tile('This week', fmt.num(s.week.words) + ' words', fmt.minutes(s.week.seconds) + ' of speech'),
      tile('All time', fmt.num(s.all.words) + ' words', fmt.minutes(s.all.seconds) + ' of speech'),
      tile('Learning', fmt.num(s.learned) + ' learned', fmt.plural(s.corrections, 'correction', 'corrections'))), off);
  },
  dictation() { this.renderFeed(); },
  renderFeed() {
    if (!this.el || !this.el.isConnected) return;
    const items = (S.feed || []).slice(0, 15);
    if (!S.feed) { put(this.feedEl, h('div', { class: 'card pad' }, h('div', { class: 'skeleton' }), h('div', { class: 'skeleton' }))); return; }
    if (!items.length) {
      put(this.feedEl, h('div', { class: 'empty' }, h('b', { text: 'Nothing dictated yet' }),
        h('span', null, 'Click into any text box, hold ', h('kbd', { text: hotkeyWords() }), ', say a sentence and let go.')));
      return;
    }
    const open = new Set([...this.feedEl.querySelectorAll('.item.open')].map(e => e.dataset.ts));
    const focusTs = document.activeElement?.closest?.('.item')?.dataset.ts;
    put(this.feedEl, h('div', { class: 'card list' }, items.map(it => feedItem(it, open.has(String(it.ts))))));
    if (focusTs) this.feedEl.querySelector(`.item[data-ts="${CSS.escape(focusTs)}"] .expander`)?.focus();
  },
};

function feedItem(it, isOpen) {
  const item = h('div', { class: 'item' + (isOpen ? ' open' : ''), 'data-ts': String(it.ts) });
  const meta = h('div', { class: 'item-meta' }, h('span', { text: fmt.when(it.ts) }),
    it.audio_seconds ? h('span', { text: fmt.secs(it.audio_seconds) + ' of speech' }) : null,
    it.total_seconds ? h('span', { text: 'ready in ' + fmt.secs(it.total_seconds) }) : null,
    it.corrected ? h('span', { class: 'badge brand', text: 'Corrected' }) : null,
    it.paste_error ? h('span', { class: 'badge bad', text: 'Not pasted' }) : null,
    (it.stages || []).some(s => s.failed) ? h('span', { class: 'badge warn', text: 'A step failed' }) : null);
  const exp = h('button', { class: 'expander', type: 'button', 'aria-expanded': isOpen ? 'true' : 'false' },
    h('p', { class: 'item-text clamp', text: it.corrected || it.final }), meta);
  const trace = h('div', { class: 'trace' });
  trace.hidden = !isOpen;
  exp.addEventListener('click', () => {
    const now = !item.classList.contains('open');
    item.classList.toggle('open', now);
    exp.setAttribute('aria-expanded', now ? 'true' : 'false');
    trace.hidden = !now;
    if (now && !trace.childElementCount) trace.append(...traceSteps(it));
  });
  if (isOpen) trace.append(...traceSteps(it));
  const fix = h('button', { class: 'btn small', type: 'button', text: 'Correct', 'aria-label': 'Correct this dictation',
    onclick: () => openCorrection(it) });
  item.append(h('div', { class: 'item-top' }, icon('chev'), h('div', { class: 'item-main' }, exp), h('div', { class: 'item-actions' }, fix)), trace);
  return item;
}

const STAGE_NAMES = {
  'normalize': ['Tidied', 'filler words and spacing'],
  'cleanup.clean': ['AI cleanup', null],
  'textrules.apply': ['Your rules', 'dictionary, snippets, spoken commands'],
};

function pastedNote(it) {
  if (it.paste_error) return 'could not paste it into the window (kept in History; copy it from there)';
  if (it.dry_run || it.paste_method === 'dry-run') return 'test mode, not actually pasted';
  if (it.paste_method === 'typed') return 'typed into the window you were typing in';
  return 'into the window you were typing in';
}

function traceSteps(it) {
  const steps = [];
  const step = (title, note, secs, text, prev, final, failed) => steps.push(h('div', { class: 'step' + (final ? ' final' : '') + (failed ? ' failed' : '') },
    h('div', { class: 'step-head' }, h('b', { text: title }), secs !== null && secs !== undefined ? h('span', { class: 't', text: fmt.secs(secs) }) : null,
      failed ? h('span', { class: 'badge warn', text: 'Failed, skipped' }) : null,
      note ? h('span', { class: 'note', text: '· ' + note }) : null),
    failed ? h('p', { class: 'step-text warn-text', text: 'This step stopped with an error and was skipped, so what it does was not applied to this dictation. Details are in the log.' })
      : prev === null || (final && prev === text) ? h('p', { class: 'step-text', text }) :
      (prev === text ? h('p', { class: 'step-text same', text: 'No change' }) : diffNode(prev, text))));
  step('Heard', it.speech_seconds === null ? 'typed in for a test'
    : 'speech recognition' + (it.language ? ', ' + languageName(it.language) : ''), it.speech_seconds, it.raw, null);
  if (!it.stages) {
    steps.push(h('p', { class: 'small faint', text: 'Step-by-step details are kept only for dictations made since the app last started.' }));
    step('Result', null, null, it.final, it.raw, true);
    return steps;
  }
  let prev = it.raw;
  for (const st of it.stages) {
    const [title, note] = STAGE_NAMES[st.name] || [st.name, null];
    step(title, st.failed ? null : st.name === 'cleanup.clean' ? cleanupStatusNote(it.cleanup) : note, st.seconds, st.text, prev, false, !!st.failed);
    if (!st.failed && typeof st.text === 'string') prev = st.text;
  }
  step('Pasted', pastedNote(it), null, (it.pasted ?? it.final).replace(/ $/, ''), prev, true);
  return steps;
}

async function openCorrection(it) {
  let id = it.history_id;
  if (!id) {
    try { id = (await api.post('/api/history/find', { ts: it.ts })).row.id; }
    catch (e) { return fail(e.status === 404 ? new Error('This dictation isn’t in your history (it was deleted, or saving history is off).') : e); }
  }
  views.history.pendingEdit = id;
  go('#history');
}

// ---- History
views.history = {
  limit: 300,
  pending: new Map(),          // id -> close() of its toast: deleted after the undo period
  mount(main) {
    const p = page('History', 'Everything you dictated, newest first. Correct a dictation and the app offers to learn from your fix, so the same mistake doesn’t happen again.');
    this.limit = 300;
    this.q = h('input', { class: 'input', type: 'search', placeholder: 'Search your dictations', 'aria-label': 'Search your dictations' });
    this.q.addEventListener('input', debounce(() => { this.limit = 300; this.load(); }, 200));
    this.info = h('span', { class: 'small faint', 'aria-live': 'polite' });
    this.clearBtn = h('button', { class: 'btn danger', type: 'button', text: 'Clear history…', onclick: () => this.clear() });
    this.listEl = h('div');
    this.moreEl = h('div');
    p.append(h('div', { class: 'row', style: 'margin-bottom:12px' },
      h('div', { class: 'search' }, icon('search'), this.q), this.info, this.clearBtn), this.listEl, this.moreEl);
    main.append(p);
    this.el = p;
    this.listEl.append(h('div', { class: 'card pad' }, h('div', { class: 'skeleton' }), h('div', { class: 'skeleton' })));
    this.load();
  },
  async load() {
    if (!this.el?.isConnected) return;
    if (this.listEl.querySelector('.editor')) return;   // don't disturb an edit in progress
    let data;
    try { data = await api.get(`/api/history?limit=${this.limit}&q=` + encodeURIComponent(this.q.value)); }
    catch (e) { put(this.listEl, h('div', { class: 'empty' }, h('b', { text: 'History couldn’t be loaded' }), e.message)); return; }
    if (!this.el?.isConnected || this.listEl.querySelector('.editor')) return;
    const hidden = data.items.filter(r => this.pending.has(r.id)).length;
    const items = data.items.filter(r => !this.pending.has(r.id));
    const total = Math.max(0, data.count - this.pending.size), matches = Math.max(0, data.matches - hidden);
    this.info.textContent = this.q.value.trim() ? fmt.plural(matches, 'match', 'matches') : total ? fmt.num(total) + ' saved' : '';
    this.clearBtn.disabled = !total;
    const off = !data.enabled ? h('div', { class: 'notice info', style: 'margin-bottom:12px' }, icon('info'),
      h('div', { class: 'body' }, h('b', { text: 'Saving history is turned off' }), h('p', null, 'New dictations are not kept. ',
        h('a', { class: 'link', href: '#settings&history', text: 'Turn it on in Settings' })))) : null;
    put(this.moreEl);
    if (!items.length) {
      put(this.listEl, off, h('div', { class: 'empty' },
        h('b', { text: this.q.value ? 'No matches' : 'No dictations yet' }),
        this.q.value ? 'Try a different word.' : 'What you dictate appears here.'));
      return;
    }
    put(this.listEl, off, h('div', { class: 'card list' }, items.map(r => this.row(r))));
    if (data.items.length < data.matches) {
      put(this.moreEl, h('div', { class: 'row more' }, h('span', { class: 'small muted', text: `Showing the newest ${fmt.num(items.length)} of ${fmt.num(matches)}.` }),
        h('button', { class: 'btn', type: 'button', text: 'Show more', onclick: () => { this.limit += 300; this.load(); } })));
    }
    if (this.pendingEdit) {
      const id = this.pendingEdit;
      this.pendingEdit = null;
      const el = this.listEl.querySelector(`[data-id="${id}"]`);
      const row = items.find(r => r.id === id);
      if (el && row) { el.scrollIntoView({ block: 'center' }); this.edit(el, row); }
      else toast('That dictation is no longer in your history.', { error: true });
    }
  },
  row(r) {
    const el = h('div', { class: 'item', 'data-id': String(r.id) });
    const meta = h('div', { class: 'item-meta' }, h('span', { text: fmt.when(r.ts) }),
      r.seconds ? h('span', { text: fmt.secs(r.seconds) + ' of speech' }) : null,
      r.corrected !== null ? h('span', { class: 'badge brand', text: 'Corrected' }) : null);
    const main = h('div', { class: 'item-main' }, h('p', { class: 'item-text', text: r.text }), meta);
    if (r.corrected !== null) main.append(h('p', { class: 'small faint', style: 'margin:6px 0 0' }, 'Pasted as: ', r.final));
    el.append(h('div', { class: 'item-top' }, main, h('div', { class: 'item-actions' },
      h('button', { class: 'btn small correct', type: 'button', text: 'Correct', 'aria-label': 'Correct this dictation', onclick: () => this.edit(el, r) }),
      iconButton('copy', 'Copy text', () => this.copy(r.text)),
      iconButton('trash', 'Delete this dictation', () => this.remove(r, el), 'danger'))));
    return el;
  },
  // The app puts the text on the clipboard itself, marked so Windows keeps it
  // out of clipboard history and cloud sync; the page's own clipboard is only
  // the fallback, and the toast says so.
  async copy(text) {
    try { await api.post('/api/clipboard/copy', { text }); toast('Copied (kept out of Windows clipboard history)'); return; }
    catch (e) { /* fall back below */ }
    try { await navigator.clipboard.writeText(text); toast('Copied, but the app could not keep it out of Windows clipboard history'); }
    catch (e) { toast('Couldn’t copy to the clipboard', { error: true }); }
  },
  // Deleting is held back until the undo period ends (or the page is
  // hidden or left), so Undo just shows the row again.
  remove(r, el) {
    const items = [...this.listEl.querySelectorAll('.item:not([hidden])')];
    const next = items[items.indexOf(el) + 1] || items[items.indexOf(el) - 1];
    el.hidden = true;
    const commit = async () => {
      this.pending.delete(r.id);
      try { await api.post('/api/history/delete', { id: r.id }, { keepalive: true }); }
      catch (e) { if (e.status !== 404) { el.hidden = false; fail(e); } }
      if (S.feed) S.feed = S.feed.filter(i => i.history_id !== r.id && Math.abs(i.ts - r.ts) > 1e-6);
      refreshState();
    };
    const heading = this.el.querySelector('h1');
    const t = toast('Dictation deleted', { action: 'Undo', returnFocus: heading, onexpire: commit,
      onaction: () => { this.pending.delete(r.id); el.hidden = false; el.querySelector('.correct')?.focus(); this.load(); } });
    this.pending.set(r.id, t.close);
    (next?.querySelector('.correct') || heading).focus();
    const n = Math.max(0, (parseInt(this.info.textContent.replace(/\D/g, ''), 10) || 1) - 1);
    if (!this.q.value.trim()) this.info.textContent = n ? fmt.num(n) + ' saved' : '';
  },
  flush() { for (const close of [...this.pending.values()]) close('flush'); },
  async clear() {
    const ok = await confirmBox('Clear all history?', 'Every saved dictation is deleted from this computer. This cannot be undone. What you learned (dictionary, vocabulary) is kept.', 'Delete everything');
    if (!ok) return;
    this.flush();
    try {
      await api.post('/api/history/clear', { confirm: true });
      S.feed = []; toast('History cleared'); this.load(); refreshState(); views.home.renderFeed();
    } catch (e) { fail(e); }
  },
  edit(el, r) {
    if (el.querySelector('.editor')) return;
    const ta = h('textarea', { class: 'input', rows: '3', 'aria-label': 'What it should have said' });
    ta.value = r.text;
    const msg = h('p', { class: 'msg', 'aria-live': 'polite' });
    const save = h('button', { class: 'btn primary', type: 'button', text: 'Save correction' });
    const back = () => el.querySelector('.correct')?.focus();
    const cancel = () => { editor.remove(); back(); };
    const editor = h('div', { class: 'editor' },
      h('p', { class: 'was' }, 'Fix the words that came out wrong. Small, exact fixes teach the app best.'),
      ta, h('div', { class: 'row', style: 'margin-top:10px' }, save,
        h('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: cancel }),
        r.corrected !== null ? h('button', { class: 'link', type: 'button', text: 'Go back to what was pasted', onclick: () => this.revert(r) }) : null,
        h('span', { class: 'spacer' }), msg));
    el.append(editor);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    editor.addEventListener('keydown', e => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && e.target === ta) { e.preventDefault(); save.click(); }
      if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        const res = await api.post('/api/history/correct', { id: r.id, text: ta.value });
        editor.remove();
        const fresh = this.row(res.row);
        el.replaceWith(fresh);
        showLearning(fresh, res);
        (fresh.querySelector('.learn button') || fresh.querySelector('.correct'))?.focus();
        refreshState();
      } catch (e) { msg.className = 'msg err'; msg.textContent = e.message; save.disabled = false; }
    });
  },
  async revert(r) {
    try { await api.post('/api/history/revert', { id: r.id }); toast('Back to what was pasted'); this.listEl.querySelector('.editor')?.remove(); this.load(); }
    catch (e) { fail(e); }
  },
};

function showLearning(el, res) {
  const box = h('div', { class: 'learn' });
  const sugg = res.suggestions || [], auto = res.auto || [];
  for (const rec of auto) {
    box.append(h('div', { class: 'sugg' },
      h('div', { class: 'swap' }, h('span', { class: 'from', text: rec.heard }), icon('arrow'), h('span', { class: 'to', text: rec.wanted }), h('span', { class: 'badge brand', text: 'Learned automatically' })),
      h('p', { text: rec.summary }),
      h('div', { class: 'row acts' }, h('button', { class: 'btn small', type: 'button', text: 'Undo', onclick: async (ev) => {
        const card = ev.target.closest('.sugg'), b = ev.target;
        if (await undoLearned(rec.id)) { card.classList.add('done'); b.disabled = true; } } }))));
  }
  if (res.learn_error) box.append(h('p', { class: 'msg err', text: res.learn_error }));
  if (!sugg.length && !auto.length) {
    const pair = res.row.corrected !== null ? changedPair(res.row.final, res.row.corrected) : null;
    box.append(h('div', { class: 'learn-head' }, icon('info'), h('span', { text: 'Saved.' })),
      h('p', { class: 'small muted', style: 'margin:0', text: res.row.corrected === null ? 'This dictation is back to what was pasted.'
        : pair ? 'Nothing to learn automatically: it looks like an edit rather than a word that was misheard. If it was a misheard word or name, add it to your Dictionary so it is always written your way.'
          : 'Nothing to learn from this one: it looks like an edit rather than a word that was misheard (or the app already knows it).' }));
    if (pair) box.append(h('div', { class: 'row' }, h('button', { class: 'btn small', type: 'button', text: `Add ${q(pair.wanted)} to Dictionary…`,
      onclick: () => { views.dictionary.prefill = pair; go('#dictionary'); } })));
  } else if (sugg.length) {
    box.prepend(h('div', { class: 'learn-head' }, icon('spark'), h('span', { text: 'Learn from this fix?' })));
  }
  for (const s of sugg) box.append(suggestionCard(s, res.row.id));
  if (sugg.length > 1) {
    box.append(h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', text: 'Learn all', onclick: async () => {
      const left = [...box.querySelectorAll('.sugg:not(.done)')].map(c => c.dataset.id).filter(Boolean);
      if (left.length) await acceptSuggestions(res.row.id, left, box);
    } })));
  }
  el.append(box);
}

const ORDINARY_TITLE = 'Replace an ordinary word everywhere?';

function suggestionCard(s, historyId) {
  const card = h('div', { class: 'sugg', 'data-id': s.id });
  const what = [];
  if (s.dictionary) what.push(h('span', { class: 'badge brand', text: 'Dictionary: always write it this way' }));
  if (s.vocabulary) what.push(h('span', { class: 'badge', text: 'Vocabulary: a hint for speech recognition' }));
  card.append(h('div', { class: 'swap' }, h('span', { class: 'from', text: s.dictionary?.heard || s.heard }), icon('arrow'), h('span', { class: 'to', text: s.wanted })),
    h('div', { class: 'row', style: 'margin-top:8px; gap:6px' }, what),
    h('p', { text: s.reason }),
    ...(s.warnings || []).map(w => h('p', { class: 'msg warn', text: w })));
  const acts = h('div', { class: 'row acts' },
    h('button', { class: 'btn primary small', type: 'button', text: 'Learn this', onclick: () => acceptSuggestions(historyId, [s.id], card) }),
    h('button', { class: 'btn small', type: 'button', text: 'Not this', onclick: () => { card.classList.add('done'); acts.replaceChildren(h('span', { class: 'small faint', text: 'Skipped' })); } }));
  if (s.manual_rule) {
    acts.append(h('button', { class: 'link', type: 'button', text: `Also always replace ${q(s.manual_rule.heard)}…`, onclick: async () => {
      const ok = await confirmBox(ORDINARY_TITLE, `Every ${q(s.manual_rule.heard)} in every future dictation will become ${q(s.manual_rule.wanted)}. You can remove it later in Dictionary.`, 'Add it anyway', false);
      if (!ok) return;
      try { const r = await api.post('/api/dictionary/add', s.manual_rule); Object.assign(S.config, r.config); renderNav(); toast('Added to your dictionary'); }
      catch (e) { applyTruth(e); fail(e); }
    } }));
  }
  card.append(acts);
  return card;
}

async function acceptSuggestions(historyId, ids, scope) {
  try {
    const res = await api.post('/api/learn/accept', { history_id: historyId, ids });
    S.config = Object.assign(S.config || {}, res.config);
    for (const id of ids) {
      const card = scope.matches?.('.sugg') ? scope : scope.querySelector(`.sugg[data-id="${id}"]`);
      if (card) { card.classList.add('done'); card.querySelector('.acts').replaceChildren(h('span', { class: 'small', text: 'Learned' })); }
    }
    const rec = res.applied[0];
    if (rec) toast(res.applied.length > 1 ? `Learned ${res.applied.length} things` : 'Learned: ' + rec.summary,
      { action: 'Undo', onaction: async () => { for (const r of res.applied) await undoLearned(r.id); } });
    else toast((res.skipped[0] || {}).reason || 'Nothing new to learn');
    renderNav(); refreshState();
  } catch (e) { applyTruth(e); renderNav(); fail(e); }
}

async function undoLearned(id) {
  try {
    const res = await api.post('/api/learned/undo', { id });
    S.config = Object.assign(S.config || {}, res.config);
    toast(res.warnings?.length ? res.warnings[0] : 'Undone');
    renderNav(); views[S.view].config?.(); refreshState();
    return true;
  } catch (e) { applyTruth(e); views[S.view].config?.(); fail(e); return false; }
}

// "Learned from your corrections", on the page of what each item added.
function learnedList(filter, emptyText) {
  const learned = (S.config.learned || []).filter(filter);
  if (!learned.length) return h('div', { class: 'empty' }, h('b', { text: 'Nothing learned yet' }), h('span', null, emptyText, ' ', h('a', { href: '#history', class: 'link', text: 'Go to History' })));
  return h('div', { class: 'card list' }, learned.map(r => h('div', { class: 'item' },
    h('div', { class: 'item-top' }, h('div', { class: 'item-main' },
      h('div', { class: 'swap' }, h('span', { class: 'from', text: r.heard }), icon('arrow'), h('span', { class: 'to', text: r.wanted }),
        r.added_entry ? h('span', { class: 'badge brand', text: 'Dictionary entry' }) : null,
        r.added_term ? h('span', { class: 'badge', text: 'Vocabulary word' }) : null,
        r.auto ? h('span', { class: 'badge', text: 'automatic' }) : null, !r.active ? h('span', { class: 'badge', text: 'no longer in use' }) : null),
      h('p', { class: 'small muted', style: 'margin:4px 0 0', text: r.summary }),
      h('div', { class: 'item-meta' }, h('span', { text: fmt.when(r.ts) }), r.before ? h('span', { text: `from ${q(r.before)} → ${q(r.after)}` }) : null)),
    h('div', { class: 'item-actions' }, h('button', { class: 'btn small', type: 'button', text: 'Undo', 'aria-label': `Undo learning ${q(r.wanted)}`, onclick: () => undoLearned(r.id) }))))));
}

// After removing a row, focus a neighbour (never the page body).
function focusAfterRemove(container, selector, index, fallback) {
  const all = [...container.querySelectorAll(selector)];
  (all[Math.min(index, all.length - 1)] || fallback)?.focus();
}

// ---- Dictionary
views.dictionary = {
  mount(main) {
    const p = page('Dictionary', 'When speech recognition writes the left side, the app writes the right side instead. This is what guarantees a spelling — use it for names, brands and anything it keeps getting wrong.');
    // The visible labels name the fields (no aria-label that would differ).
    this.heard = h('input', { class: 'input', placeholder: 'e.g. zor vex', autocomplete: 'off' });
    this.wanted = h('input', { class: 'input', placeholder: 'e.g. Zorvex', autocomplete: 'off' });
    this.addBtn = h('button', { class: 'btn primary', type: 'submit' }, icon('plus'), 'Add');
    this.msg = h('div', { 'aria-live': 'polite' });
    this.sample = h('input', { class: 'input', placeholder: 'Type a sentence to try it on', 'aria-label': 'Try it on a sentence' });
    this.out = h('p', { class: 'out' });
    this.preview = h('div', { class: 'preview', hidden: true }, h('div', { class: 'row' }, h('span', { class: 'small muted', text: 'Try it' }), h('span', { class: 'spacer' })), this.sample, this.out);
    const form = h('form', { class: 'card adder' },
      h('div', { class: 'adder-grid' },
        h('label', { class: 'field' }, h('span', { text: 'When it hears' }), this.heard),
        h('span', { class: 'arrow' }, icon('arrow')),
        h('label', { class: 'field' }, h('span', { text: 'Write' }), this.wanted), this.addBtn),
      this.msg, this.preview);
    form.addEventListener('submit', e => { e.preventDefault(); this.add(); });
    const check = debounce(() => this.check(), 250);
    for (const el of [this.heard, this.wanted]) el.addEventListener('input', check);
    this.sample.addEventListener('input', debounce(() => this.runPreview(), 200));
    this.tableEl = h('div');
    this.learnedEl = h('div');
    p.append(form, h('div', { class: 'section-title' }, h('h2', { text: 'Your entries' }), h('span', { class: 'aside', text: 'Entries you accepted from History are marked “learned”.' })), this.tableEl,
      h('div', { class: 'section-title' }, h('h2', { text: 'Learned from your corrections' }), h('span', { class: 'aside', text: 'Undo removes exactly what was added.' })), this.learnedEl);
    main.append(p);
    this.el = p;
    this.config();
    if (this.prefill) {
      this.heard.value = this.prefill.heard; this.wanted.value = this.prefill.wanted; this.prefill = null;
      this.wanted.focus(); this.check();
    }
  },
  showMsg(kind, ...texts) { put(this.msg, texts.map(t => h('p', { class: 'msg ' + kind, style: 'margin-top:10px', text: t }))); },
  async check() {
    const heard = this.heard.value, wanted = this.wanted.value;
    if (!heard.trim() || !wanted.trim()) { this.msg.replaceChildren(); this.preview.hidden = true; return; }
    try {
      const res = await api.post('/api/dictionary/check', { heard, wanted });
      this.showMsg('warn', ...res.warnings);
      this.preview.hidden = false;
      this.runPreview();
    } catch (e) { this.showMsg('err', e.message); this.preview.hidden = true; }
  },
  async runPreview() {
    const heard = this.heard.value, wanted = this.wanted.value;
    if (!heard.trim() || !wanted.trim()) return;
    try {
      const res = await api.post('/api/dictionary/preview', { heard, wanted, sample: this.sample.value });
      if (!this.sample.value) this.sample.placeholder = res.sample;
      this.out.replaceChildren();
      for (const part of diffWords(res.sample, res.full)) {
        if (part.k === 'same') this.out.append(part.t);
        else if (part.k === 'add') this.out.append(h('mark', { text: part.t }));
      }
      this.out.append(h('span', { class: 'small faint', text: res.matches ? `  · ${res.matches} change${res.matches > 1 ? 's' : ''}` : '  · no match in this sentence' }));
    } catch (e) { /* preview is best effort */ }
  },
  // Checked again when submitted (a quick Enter must not skip the warning),
  // with the same confirmation as learning from a correction.
  async add() {
    const heard = this.heard.value, wanted = this.wanted.value.trim();
    let warnings;
    try { warnings = (await api.post('/api/dictionary/check', { heard, wanted })).warnings; }
    catch (e) { this.showMsg('err', e.message); this.heard.focus(); return; }
    if (warnings.length) {
      this.showMsg('warn', ...warnings);
      const ordinary = warnings.some(w => /ordinary/i.test(w));
      const ok = await confirmBox(ordinary ? ORDINARY_TITLE : 'Add this entry?',
        warnings.join(' ') + ' You can remove it later in Dictionary.', 'Add it anyway', false);
      if (!ok) { this.wanted.focus(); return; }
    }
    try {
      const res = await api.post('/api/dictionary/add', { heard, wanted });
      Object.assign(S.config, res.config);
      toast(`Added: ${q(heard.trim())} → ${q(wanted)}`);
      this.heard.value = ''; this.wanted.value = ''; this.msg.replaceChildren(); this.preview.hidden = true;
      this.heard.focus();
      this.config(); renderNav();
    } catch (e) {          // what was typed stays in the fields
      if (applyTruth(e)) { this.config(); renderNav(); }
      this.showMsg('err', e.message);
    }
  },
  config() {
    if (!this.el?.isConnected || !S.config) return;
    if (this.tableEl.querySelector('tr.editing')) return;      // an edit is open
    const rows = S.config.dictionary;
    const legacy = S.config.legacy_replacements ? h('p', { class: 'small faint', text: `Plus ${S.config.legacy_replacements} older replacement(s) from the settings file (“replacements”), which still apply.` }) : null;
    put(this.learnedEl, learnedList(r => !!r.added_entry, 'Correct a dictation in History and accept what it suggests.'));
    if (!rows.length) {
      put(this.tableEl, h('div', { class: 'empty' }, h('b', { text: 'No entries yet' }), 'Add one above, or correct a dictation in History and let the app suggest it.'), legacy);
      return;
    }
    const table = h('table', { class: 'table' }, h('thead', null, h('tr', null, h('th', { text: 'When it hears' }), h('th', null, h('span', { class: 'sr-only', text: 'becomes' })), h('th', { text: 'Write' }), h('th', null, h('span', { class: 'sr-only', text: 'Actions' })))),
      h('tbody', null, rows.map(r => this.row(r))));
    // A re-render (e.g. the app announcing the change) keeps the focus.
    const f = document.activeElement;
    const keep = this.tableEl.contains(f) && f.closest('tr') ? [f.closest('tr').dataset.heard, f.classList.contains('remove') ? '.remove' : '.edit'] : null;
    put(this.tableEl, h('div', { class: 'card' }, table), legacy);
    if (keep) this.focusRow(keep[0], keep[1]);
  },
  focusRow(heard, which) {
    const tr = [...this.tableEl.querySelectorAll('tbody tr')].find(t => t.dataset.heard === heard);
    (tr?.querySelector(which) || this.heard).focus();
  },
  row(r) {
    const tr = h('tr', { 'data-heard': r.heard }, h('td', { text: r.heard }), h('td', { class: 'arrow-cell' }, icon('arrow')),
      h('td', null, h('b', { text: r.wanted }), r.learned ? h('span', { class: 'badge brand', style: 'margin-left:8px', text: 'learned' }) : null),
      h('td', { class: 'acts' }, iconButton('edit', `Edit ${q(r.heard)}`, () => this.edit(tr, r), 'edit'), iconButton('trash', `Remove ${q(r.heard)}`, () => this.remove(r, tr), 'danger remove')));
    return tr;
  },
  edit(tr, r) {
    const heard = h('input', { class: 'input', value: r.heard, 'aria-label': 'When it hears' });
    const wanted = h('input', { class: 'input', value: r.wanted, 'aria-label': 'Write' });
    const msg = h('p', { class: 'msg err', 'aria-live': 'polite' });
    const done = focusHeard => { tr.className = ''; this.config(); this.focusRow(focusHeard, '.edit'); };
    const save = async () => {
      try {
        const res = await api.post('/api/dictionary/edit', { old_heard: r.heard, heard: heard.value, wanted: wanted.value });
        Object.assign(S.config, res.config);
        if (res.warnings?.length) toast(res.warnings[0]); else toast('Saved');
        done(heard.value.trim());
      } catch (e) { applyTruth(e); msg.textContent = e.message; }
    };
    for (const el of [heard, wanted]) el.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); save(); }
      if (e.key === 'Escape') { e.preventDefault(); done(r.heard); }
    });
    tr.className = 'editing';
    tr.replaceChildren(h('td', null, heard), h('td', { class: 'arrow-cell' }, icon('arrow')), h('td', null, wanted, msg),
      h('td', { class: 'acts' }, h('button', { class: 'btn small primary', type: 'button', text: 'Save', onclick: save }), ' ',
        h('button', { class: 'btn small', type: 'button', text: 'Cancel', onclick: () => done(r.heard) })));
    heard.focus();
  },
  async remove(r, tr) {
    const index = [...this.tableEl.querySelectorAll('tbody tr')].indexOf(tr);
    try {
      const res = await api.post('/api/dictionary/remove', { heard: r.heard });
      Object.assign(S.config, res.config); this.config(); renderNav();
      focusAfterRemove(this.tableEl, 'tbody .remove', index, this.heard);
      toast(`Removed ${q(r.heard)}`, { action: 'Undo', returnFocus: this.heard, onaction: async () => {
        try { const back = await api.post('/api/dictionary/add', { heard: r.heard, wanted: r.wanted });
              Object.assign(S.config, back.config); this.config(); renderNav(); this.focusRow(r.heard, '.edit'); }
        catch (e) { if (applyTruth(e)) { this.config(); renderNav(); } fail(e); } } });
    } catch (e) { if (applyTruth(e)) { this.config(); renderNav(); } fail(e); }
  },
};

// ---- Vocabulary
views.vocabulary = {
  mount(main) {
    const p = page('Vocabulary', 'Your own words and names. Speech recognition is told to expect them, which makes it more likely to spell them right. It is a strong hint, not a guarantee: to make sure a word is always written one way, add it to the Dictionary.');
    this.input = h('input', { class: 'input', placeholder: 'A name, product or term, e.g. Zorvex', 'aria-label': 'New word or name', autocomplete: 'off' });
    this.msg = h('div', { 'aria-live': 'polite' });
    const form = h('form', { class: 'card adder' }, h('div', { class: 'row' }, h('div', { style: 'flex:1;min-width:200px' }, this.input),
      h('button', { class: 'btn primary', type: 'submit' }, icon('plus'), 'Add')), this.msg);
    form.addEventListener('submit', e => { e.preventDefault(); this.add(); });
    this.input.addEventListener('input', debounce(() => this.check(), 250));
    this.chipsEl = h('div');
    this.fit = h('span', { class: 'aside' });
    this.fitNote = h('div');
    this.learnedEl = h('div');
    p.append(form, h('div', { class: 'section-title' }, h('h2', { text: 'Your words' }), this.fit), this.fitNote, this.chipsEl,
      h('div', { class: 'section-title' }, h('h2', { text: 'Learned from your corrections' }), h('span', { class: 'aside', text: 'Undo removes exactly what was added.' })), this.learnedEl);
    main.append(p);
    this.el = p;
    this.config();
  },
  showMsg(kind, ...texts) { put(this.msg, texts.map(t => h('p', { class: 'msg ' + kind, style: 'margin-top:10px', text: t }))); },
  async check() {
    const term = this.input.value;
    if (!term.trim()) { this.msg.replaceChildren(); return; }
    try {
      const res = await api.post('/api/vocabulary/check', { term });
      this.showMsg('warn', ...res.warnings);
    } catch (e) { this.showMsg('err', e.message); }
  },
  async add() {
    const term = this.input.value;
    try {
      const res = await api.post('/api/vocabulary/add', { term });
      Object.assign(S.config, res.config);
      toast(`Added ${q(term.trim())}`);
      this.input.value = ''; this.msg.replaceChildren(); this.config(); renderNav();
    } catch (e) { if (applyTruth(e)) { this.config(); renderNav(); } this.showMsg('err', e.message); }
  },
  config() {
    if (!this.el?.isConnected || !S.config) return;
    const words = S.config.vocabulary, g = S.config.glossary;
    const ignored = new Set((g.ignored || []).map(t => t.trim().toLocaleLowerCase()));
    const unused = words.filter(w => ignored.has(w.trim().toLocaleLowerCase()));
    this.fit.textContent = g.total > g.used ? `${g.used} of ${g.total} hints are used` : '';
    put(this.fitNote, unused.length ? h('div', { class: 'notice warn', style: 'margin-bottom:12px' }, icon('warn'), h('div', { class: 'body' },
      h('b', { text: `${fmt.plural(unused.length, 'word is', 'words are')} ignored` }),
      h('p', { text: `Speech recognition can only read a limited number of hints, and the list is full. The words marked “ignored” (the last ones) are not used. Remove words you no longer need to make room, or add a Dictionary entry for a word that must always be spelled one way. Dictionary spellings count as hints too.` }))) : null);
    const focusLabel = this.chipsEl.contains(document.activeElement) ? document.activeElement.getAttribute('aria-label') : null;
    put(this.chipsEl, words.length ? h('ul', { class: 'chips', 'aria-label': 'Your words' }, words.map(w => {
      const off = ignored.has(w.trim().toLocaleLowerCase());
      return h('li', { class: 'chip' + (off ? ' off' : ''), title: off ? 'Ignored: the hint list is full' : null }, h('span', { text: w }),
        off ? h('span', { class: 'chip-note', text: 'ignored' }) : null,
        iconButton('edit', `Edit ${q(w)}`, () => this.edit(w)), iconButton('trash', `Remove ${q(w)}`, () => this.remove(w), 'danger remove'));
    }))
      : h('div', { class: 'empty' }, h('b', { text: 'No words yet' }), 'Add the names and terms you use most.'));
    if (focusLabel) this.chipsEl.querySelector(`[aria-label="${CSS.escape(focusLabel)}"]`)?.focus();
    put(this.learnedEl, learnedList(r => !!r.added_term || !r.added_entry, 'Correct a dictation in History and accept what it suggests.'));
  },
  async edit(w) {
    const input = h('input', { class: 'input', value: w, id: 'vocab-edit', autocomplete: 'off' });
    const v = await dialog(close => {
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); close(input.value); } });
      return [h('div', { class: 'dlg-body' }, h('h2', { text: 'Edit word' }), h('label', { class: 'field', for: 'vocab-edit' }, h('span', { text: 'Word or name' })), input),
        h('div', { class: 'dlg-foot' }, h('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: () => close(undefined) }),
          h('button', { class: 'btn primary', type: 'button', text: 'Save', onclick: () => close(input.value) }))];
    });
    if (v === undefined || v === w) return;
    try { const res = await api.post('/api/vocabulary/edit', { old: w, new: v }); Object.assign(S.config, res.config); this.config(); toast('Saved'); }
    catch (e) { if (applyTruth(e)) this.config(); fail(e); }
  },
  async remove(w) {
    const index = [...this.chipsEl.querySelectorAll('.remove')].findIndex(b => b.getAttribute('aria-label') === `Remove ${q(w)}`);
    try {
      const res = await api.post('/api/vocabulary/remove', { term: w });
      Object.assign(S.config, res.config); this.config(); renderNav();
      focusAfterRemove(this.chipsEl, '.remove', index, this.input);
      toast(`Removed ${q(w)}`, { action: 'Undo', returnFocus: this.input, onaction: async () => {
        try { const b = await api.post('/api/vocabulary/add', { term: w }); Object.assign(S.config, b.config); this.config(); renderNav(); }
        catch (e) { if (applyTruth(e)) { this.config(); renderNav(); } fail(e); } } });
    } catch (e) { if (applyTruth(e)) { this.config(); renderNav(); } fail(e); }
  },
};

// ---- Snippets
views.snippets = {
  mount(main) {
    const p = page('Snippets', 'Say a phrase, get a block of text: your address, a sign-off, a standard reply. Say the phrase as a sentence of its own, for example “My signature.”');
    this.listEl = h('div');
    this.formEl = h('div');
    p.append(this.formEl, h('div', { class: 'section-title' }, h('h2', { text: 'Your snippets' })), this.listEl);
    main.append(p);
    this.el = p;
    this.form(null);
    this.config();
  },
  form(editing) {
    const trigger = h('input', { class: 'input', placeholder: 'e.g. my signature', value: editing?.trigger || '', autocomplete: 'off' });
    const text = h('textarea', { class: 'input', rows: '4', placeholder: 'Best regards,\nSam' });
    text.value = editing?.text || '';
    const msg = h('div', { 'aria-live': 'polite' });
    const check = debounce(async () => {
      if (!trigger.value.trim() || !text.value.trim()) { msg.replaceChildren(); return; }
      try {
        const res = await api.post('/api/snippets/check', { trigger: trigger.value, text: text.value, editing: editing?.trigger });
        put(msg, res.warnings.map(w => h('p', { class: 'msg warn', text: w })));
      } catch (e) { put(msg, h('p', { class: 'msg err', text: e.message })); }
    }, 250);
    trigger.addEventListener('input', check); text.addEventListener('input', check);
    const cancel = () => { this.form(null); if (editing) this.focusSnippet(editing.trigger); };
    const form = h('form', { class: 'card adder stack' },
      h('h2', { text: editing ? 'Edit snippet' : 'New snippet' }),
      h('label', { class: 'field' }, h('span', { text: 'When you say' }), trigger),
      h('label', { class: 'field' }, h('span', { text: 'Insert this text' }), text), msg,
      h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'submit', text: editing ? 'Save' : 'Add snippet' }),
        editing ? h('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: cancel }) : null,
        h('span', { class: 'spacer' }), this.modeSelect()));
    if (editing) form.addEventListener('keydown', e => { if (e.key === 'Escape' && e.target.tagName !== 'SELECT') { e.preventDefault(); cancel(); } });
    form.addEventListener('submit', async e => {
      e.preventDefault();
      try {
        const res = editing
          ? await api.post('/api/snippets/edit', { old_trigger: editing.trigger, trigger: trigger.value, text: text.value })
          : await api.post('/api/snippets/add', { trigger: trigger.value, text: text.value });
        Object.assign(S.config, res.config);
        toast(editing ? 'Saved' : `Added ${q(trigger.value.trim())}`);
        const t = trigger.value.trim();
        this.form(null); this.config(); renderNav();
        if (editing) this.focusSnippet(t); else this.formEl.querySelector('input')?.focus();
      } catch (err) { if (applyTruth(err)) { this.config(); renderNav(); } put(msg, h('p', { class: 'msg err', text: err.message })); }
    });
    this.formEl.replaceChildren(form);
    if (editing) trigger.focus();
  },
  focusSnippet(trigger) {
    const card = [...this.listEl.querySelectorAll('.snip')].find(c => c.dataset.trigger === trigger);
    (card?.querySelector('.edit') || this.formEl.querySelector('input'))?.focus();
  },
  modeSelect() {
    const mode = S.config?.settings?.snippet_mode || 'sentence';
    const status = h('span', { class: 'saved', 'aria-live': 'polite' });
    const sel = h('select', { class: 'input', style: 'width:auto' },
      [['sentence', 'Phrase said as its own sentence'], ['whole', 'Phrase is the whole dictation'], ['anywhere', 'Phrase anywhere in a sentence']]
        .map(([v, t]) => h('option', { value: v, text: t, selected: v === mode })));
    sel.addEventListener('change', async () => {
      await saveSetting({ snippet_mode: sel.value }, status);
      sel.value = S.config.settings.snippet_mode;
    });
    return h('label', { class: 'row small muted mode' }, 'Fires when:', sel, status);
  },
  config() {
    if (!this.el?.isConnected || !S.config) return;
    const list = S.config.snippets;
    const f = document.activeElement;
    const keep = this.listEl.contains(f) && f.closest('.snip') ? [f.closest('.snip').dataset.trigger, f.classList.contains('remove') ? '.remove' : '.edit'] : null;
    if (keep) queueMicrotask(() => { const card = [...this.listEl.querySelectorAll('.snip')].find(x => x.dataset.trigger === keep[0]); (card?.querySelector(keep[1]) || this.listEl.querySelector('.remove') || this.formEl.querySelector('input'))?.focus(); });
    put(this.listEl, list.length ? h('div', { class: 'snips' }, list.map(s => h('div', { class: 'card snip', 'data-trigger': s.trigger },
      h('div', { class: 'row' }, h('span', { class: 'say', text: q(s.trigger) }), h('span', { class: 'spacer' }),
        iconButton('edit', `Edit ${q(s.trigger)}`, () => this.form(s), 'edit'), iconButton('trash', `Remove ${q(s.trigger)}`, () => this.remove(s), 'danger remove')),
      h('pre', { text: s.text }))))
      : h('div', { class: 'empty' }, h('b', { text: 'No snippets yet' }), 'Create one above, then say its phrase while holding your hotkey.'));
  },
  async remove(s) {
    const index = [...this.listEl.querySelectorAll('.snip')].findIndex(c => c.dataset.trigger === s.trigger);
    try {
      const res = await api.post('/api/snippets/remove', { trigger: s.trigger });
      Object.assign(S.config, res.config); this.config(); renderNav();
      focusAfterRemove(this.listEl, '.remove', index, this.formEl.querySelector('input'));
      toast(`Removed ${q(s.trigger)}`, { action: 'Undo', returnFocus: this.formEl.querySelector('input'), onaction: async () => {
        try { const b = await api.post('/api/snippets/add', s); Object.assign(S.config, b.config); this.config(); renderNav(); this.focusSnippet(s.trigger); }
        catch (e) { if (applyTruth(e)) { this.config(); renderNav(); } fail(e); } } });
    } catch (e) { if (applyTruth(e)) { this.config(); renderNav(); } fail(e); }
  },
};

// ---- Settings
// Saves, then shows the app's real state either way. Returns the answer, or
// null when the change was not made (the note says why, in plain words).
async function saveSetting(changes, statusEl) {
  const show = (text, err) => {
    if (!statusEl) { if (err) fail(new Error(text)); return; }
    statusEl.textContent = text; statusEl.className = 'saved show' + (err ? ' err' : '');
    clearTimeout(statusEl.timer);
    if (!err) statusEl.timer = setTimeout(() => statusEl.classList.remove('show'), 1800);
  };
  try {
    const res = await api.post('/api/settings', changes);
    S.config.settings = res.settings;
    if (res.config) Object.assign(S.config, res.config);
    if (res.result.restart_required.length) show('Saved. Restart the app to use it.', false);
    else show('Saved', false);
    return res;
  } catch (e) {
    applyTruth(e);
    show(e.message, true);
    return null;
  }
}

function settingRow(title, sub, ctl, id) {
  return h('div', { class: 'setting', id }, h('div', { class: 'what' }, h('b', { text: title }), sub ? (sub instanceof Node ? sub : h('span', { text: sub })) : null),
    h('div', { class: 'ctl' }, ctl));
}

// A switch that always ends up showing the app's real value.
function toggle(key, label, read = s => s[key], write = v => ({ [key]: v }), after = null) {
  const status = h('span', { class: 'saved', 'aria-live': 'polite' });
  const input = h('input', { type: 'checkbox', role: 'switch', 'aria-label': label, checked: !!read(S.config.settings) });
  input.addEventListener('change', async () => {
    input.disabled = true;
    await saveSetting(write(input.checked), status);
    input.checked = !!read(S.config.settings);
    input.disabled = false;
    if (after) after(input.checked);
  });
  return [h('label', { class: 'switch' }, input), status];
}

const PRESETS = [
  ['Default', '#f4f4f5', '#17181c'], ['Pink', '#ff4fa3', '#17181c'], ['Teal', '#3fd1bf', '#17181c'],
  ['Amber', '#ffb23e', '#17181c'], ['Violet', '#b69cff', '#1d1830'], ['Light', '#17181c', '#f4f4f5'],
];

function contrast(a, b) {
  const lum = hex => {
    const c = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map(v => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

let languageNames = null;
function languageName(code, given) {
  if (given && given !== code) return given;
  try { languageNames = languageNames || new Intl.DisplayNames(['en'], { type: 'language' }); return languageNames.of(code) || code; }
  catch (e) { return code; }
}

views.settings = {
  mount(main) {
    const p = page('Settings', 'Changes apply right away, to your next dictation.');
    this.body = h('div');
    p.append(this.body);
    main.append(p);
    this.el = p;
    this.config();
  },
  config() {
    if (!this.el?.isConnected || !S.config) return;
    if (this.body.contains(document.activeElement) && isTextField(document.activeElement)) return;
    const c = S.config, s = c.settings, hl = S.health || {};
    const group = (id, title, sub, ...rows) => h('section', { class: 'card group', id: 'set-' + id, 'aria-labelledby': 'set-' + id + '-h' },
      h('div', { class: 'group-head' }, h('h2', { text: title, id: 'set-' + id + '-h', tabindex: '-1' }), sub ? (sub instanceof Node ? sub : h('p', { text: sub })) : null), ...rows);

    // hotkey
    this.hotkeyBox = h('div', { class: 'hotkey-box' });
    this.captureBox = h('div');
    const hotkey = group('hotkey', 'Hold-to-talk key', 'Hold it while you speak, let go to paste.',
      settingRow('Current key', null, this.hotkeyBox), this.captureBox);

    // microphone
    const micStatus = h('span', { class: 'saved', 'aria-live': 'polite' });
    const devices = c.devices.list;
    const noMic = !devices.length;
    const mic = h('select', { class: 'input', 'aria-label': 'Microphone', disabled: noMic && !c.devices.missing },
      noMic ? h('option', { value: '__none', text: 'No microphone connected', selected: s.input_device === null, disabled: true })
        : h('option', { value: '__default', text: 'Windows default microphone', selected: s.input_device === null }),
      devices.map((d, i) => h('option', { value: String(i), text: d.name + (d.is_default ? ' (Windows default)' : ''), selected: d.selected })),
      c.devices.missing ? h('option', { value: '__missing', text: c.devices.missing + ' (not connected)', selected: true, disabled: true }) : null);
    mic.addEventListener('change', async () => {
      const v = mic.value === '__default' ? null : devices[Number(mic.value)].value;
      const res = await saveSetting({ input_device: v }, micStatus);
      if (!res) this.config();
    });
    this.meter = h('span', { class: 'meter', 'aria-hidden': 'true' }, h('i'));
    this.micMsg = h('span', { class: 'small muted', 'aria-live': 'polite' });
    this.micBtn = h('button', { class: 'btn', type: 'button', text: 'Test', disabled: noMic, onclick: () => this.micTest(mic) });
    const lang = h('select', { class: 'input', 'aria-label': 'Language you speak' },
      h('option', { value: '', text: 'Detect automatically', selected: !s.language }),
      c.languages.map(l => {
        const name = languageName(l.code, l.name);
        return h('option', { value: l.code, text: name === l.code ? l.code : `${name} (${l.code})`, selected: s.language === l.code });
      }));
    const langStatus = h('span', { class: 'saved', 'aria-live': 'polite' });
    lang.addEventListener('change', async () => { await saveSetting({ language: lang.value || null }, langStatus); lang.value = S.config.settings.language || ''; });
    let micNote = null;
    if (noMic || hl.mic?.state === 'none') {
      const again = h('button', { class: 'btn small', type: 'button', text: 'Check again', onclick: () => checkAgain(again) });
      micNote = h('div', { class: 'notice bad inset' }, icon('warn'), h('div', { class: 'body' }, h('b', { text: 'No microphone found' }),
        h('p', { text: 'Dictation needs a microphone. Connect one (a headset, a USB microphone or a webcam with a microphone); it appears here within a few seconds.' }),
        h('div', { class: 'row acts' }, again)));
    } else if (c.devices.missing) {
      micNote = h('div', { class: 'notice bad inset' }, icon('warn'), h('div', { class: 'body' }, h('b', { text: 'Your microphone isn’t connected' }),
        h('p', { text: `${q(c.devices.missing)} is not available. Plug it in, or choose another microphone below.` })));
    }
    const audio = group('mic', 'Microphone and language', null, micNote,
      settingRow('Microphone', null, [mic, micStatus]),
      settingRow('Check the level', 'Speak for three seconds; the bar should move well into the middle.', [this.micMsg, this.meter, this.micBtn]),
      settingRow('Language you speak', 'Choosing it is a little faster and more accurate than detecting it.', [lang, langStatus]));

    // AI cleanup
    this.cleanupState = h('span', { class: 'how small' });
    const model = h('input', { class: 'input', value: s.cleanup_model, 'aria-label': 'AI model name', style: 'width:200px', autocomplete: 'off', spellcheck: 'false' });
    const modelStatus = h('span', { class: 'saved', 'aria-live': 'polite' });
    const saveModel = async () => {
      const v = model.value.trim();
      if (!v) { model.value = S.config.settings.cleanup_model; return; }
      if (v === S.config.settings.cleanup_model) return;
      const res = await saveSetting({ cleanup_model: v }, modelStatus);
      if (!res) { model.value = S.config.settings.cleanup_model; model.select(); }   // keep the saved name, say why
    };
    model.addEventListener('change', saveModel);
    model.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); saveModel(); } });
    const where = s.cleanup_local
      ? 'It runs in Ollama, a free program you install separately from ollama.com. Nothing leaves your computer.'
      : null;
    const aiSub = h('div', null, h('p', { text: 'A small AI language model removes ums, false starts and self-corrections and fixes punctuation. ' + (where || '') }),
      h('p', { text: 'If Ollama isn’t installed or is slow, your words are simply pasted as heard. You can also turn this off: dictation works fine without it.' }));
    const host = s.cleanup_host || 'another computer';
    const remote = s.cleanup_local ? null
      : h('div', { class: 'notice warn inset' }, icon('warn'), h('div', { class: 'body' },
        h('b', { text: `AI cleanup is blocked: ${host} is not this computer` }),
        h('p', { text: 'The settings file points AI cleanup at another computer, so nothing is sent and your words are pasted as heard. If you did not set this up, turn AI cleanup off.' })));
    const lc = hl.cleanup || {};
    const refused = lc.listener_blocked ? h('div', { class: 'notice warn inset' }, icon('warn'), h('div', { class: 'body' },
      h('b', { text: lc.listener === 'unknown' ? 'AI cleanup cannot tell who answers on its port' : 'Another Windows account answers on its port' }),
      h('p', { text: (lc.listener_detail ? lc.listener_detail.charAt(0).toUpperCase() + lc.listener_detail.slice(1) + '. ' : '') + 'Nothing is sent to it, so your words are pasted as heard.' }))) : null;
    const trustRow = s.cleanup_trust_available && (lc.listener_blocked || s.cleanup_trust_listener)
      ? settingRow('Trust whatever answers on the AI cleanup port',
        'Skips the check of which Windows account runs the program on that port, so your words go to it whoever it belongs to. Needed when Ollama runs as a Windows service; leave it off otherwise.',
        toggle('cleanup_trust_listener', 'Trust whatever answers on the AI cleanup port', st => st.cleanup_trust_listener)) : null;
    const ai = group('ai', 'AI cleanup', aiSub, remote, refused,
      settingRow('Use AI cleanup', null, [this.cleanupState, ...toggle('cleanup_enabled', 'Use AI cleanup')]),
      settingRow('Model', h('span', null, 'The Ollama model to use. To install one, open a terminal and run ', h('code', { text: 'ollama pull ' + s.cleanup_model }), '.'), [model, modelStatus]),
      trustRow);

    // text
    const text = group('text', 'Text', null,
      settingRow('Remove filler words', '“um”, “uh”, “erm”. While AI cleanup is on, it removes them too.', toggle('remove_fillers', 'Remove filler words')),
      settingRow('Spoken formatting', 'Say “new line” or “new paragraph” as a phrase of its own.', toggle('formatting_commands', 'Spoken formatting')),
      settingRow('Add a space after each dictation', 'So the next one doesn’t run into it.', toggle('trailing_space', 'Add a space after each dictation')),
      settingRow('Restore the clipboard after pasting', 'Text you copied before dictating is put back.', toggle('restore_clipboard', 'Restore the clipboard after pasting')));

    // learning + history
    const learning = group('history', 'History and learning', null,
      settingRow('Save history', 'Kept only on this computer. Needed to correct dictations and learn from them, and for the usage numbers on Home.', toggle('save_history', 'Save history')),
      settingRow('Learn clear-cut fixes automatically', 'When you only change how a word is written (github → GitHub), learn it without asking. Everything else is still offered for you to accept.', toggle('auto_learn', 'Learn clear-cut fixes automatically')));

    // sounds
    const snd = s.sounds || { enabled: true, volume: 0.5, mute_others: false };
    const volStatus = h('span', { class: 'saved', 'aria-live': 'polite' });
    const vol = h('input', { type: 'range', min: '0', max: '100', step: '5', value: String(Math.round(snd.volume * 100)), 'aria-label': 'Chime volume', class: 'range', disabled: !snd.enabled });
    const volText = h('span', { class: 'small muted vol', text: Math.round(snd.volume * 100) + '%' });
    vol.addEventListener('input', () => { volText.textContent = vol.value + '%'; });
    vol.addEventListener('change', async () => {
      await saveSetting({ sounds: { volume: Number(vol.value) / 100 } }, volStatus);
      vol.value = String(Math.round(S.config.settings.sounds.volume * 100)); volText.textContent = vol.value + '%';
    });
    const playNote = h('span', { class: 'saved', 'aria-live': 'polite' });
    const play = which => async () => {
      try { await api.post('/api/sounds/play', { which }); }
      catch (e) { playNote.textContent = e.message; playNote.className = 'saved show err'; }
    };
    const [soundSwitch, soundStatus] = toggle('sounds', 'Play chimes', st => st.sounds?.enabled, v => ({ sounds: { enabled: v } }),
      on => { vol.disabled = !on; });
    const [muteSwitch, muteStatus] = toggle('mute_others', 'Mute other sound while dictating', st => st.sounds?.mute_others, v => ({ sounds: { mute_others: v } }));
    const sounds = group('sounds', 'Sounds', null,
      settingRow('Start and stop chimes', 'A short sound when dictation starts listening and when it stops.', [soundSwitch, soundStatus]),
      settingRow('Volume', null, [vol, volText, volStatus]),
      settingRow('Hear them', null, [h('button', { class: 'btn', type: 'button', text: 'Play start', onclick: play('start') }),
        h('button', { class: 'btn', type: 'button', text: 'Play stop', onclick: play('stop') }), playNote]),
      settingRow('Mute other sound while dictating', 'While you hold the key, other programs go silent, and come back when you let go. People in a call can still hear you; you will not hear them.', [muteSwitch, muteStatus]));

    // appearance
    const appearance = this.appearance();

    // system
    this.autoStatus = h('span', { class: 'saved', 'aria-live': 'polite' });
    this.autoInput = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Start with Windows', checked: c.autostart.enabled, disabled: !c.autostart.available });
    this.autoInput.addEventListener('change', async () => {
      const st = this.autoStatus;
      try { const r = await api.post('/api/autostart', { enabled: this.autoInput.checked }); S.config.autostart = r.autostart; st.textContent = 'Saved'; st.className = 'saved show'; }
      catch (e) { st.textContent = e.message; st.className = 'saved show err'; }
      this.autoInput.checked = !!S.config.autostart.enabled;
    });
    this.pauseInput = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Pause dictation', checked: !!S.live?.paused });
    this.pauseInput.addEventListener('change', () => setPaused(this.pauseInput.checked));
    this.pauseKeyBox = h('div', { class: 'hotkey-box' });
    this.pauseCaptureBox = h('div');
    // Less graphics memory: only when this version of the app has the setting.
    let gpuRow = null;
    if (s.less_graphics_memory_available) {
      const gpuStatus = h('span', { class: 'saved', 'aria-live': 'polite' });
      const gpu = h('input', { type: 'checkbox', role: 'switch', 'aria-label': 'Use less graphics memory', checked: !!s.less_graphics_memory });
      gpu.addEventListener('change', async () => {
        gpu.disabled = true;
        const res = await saveSetting({ less_graphics_memory: gpu.checked }, gpuStatus);
        gpu.checked = !!S.config.settings.less_graphics_memory; gpu.disabled = false;
        if (res) { clearTimeout(gpuStatus.timer); gpuStatus.textContent = 'Saved. Restart the app for this to take effect.'; gpuStatus.className = 'saved show'; }
      });
      gpuRow = settingRow('Use less graphics memory', 'Loads the speech model in a smaller form: it needs less memory on your graphics card, with a very small loss in accuracy. It takes effect after you restart the app.', [h('label', { class: 'switch' }, gpu), gpuStatus]);
    }
    const system = group('system', 'System', null, gpuRow,
      settingRow('Start with Windows', c.autostart.available ? 'Runs quietly in the notification area after you sign in.' : 'Only available for the main installation.', [h('label', { class: 'switch' }, this.autoInput), this.autoStatus]),
      settingRow('Pause dictation', 'The hotkey does nothing while paused (a mouse button does its usual job again).', h('label', { class: 'switch' }, this.pauseInput)),
      settingRow('Pause shortcut', 'Press it in any app to pause or resume dictation.', this.pauseKeyBox), this.pauseCaptureBox);

    // about
    const info = S.state?.info || {};
    const about = group('about', 'About', null,
      settingRow(`${info.name || ''} ${info.version || ''}`, info.log_path ? 'Log file: ' + info.log_path : 'The log is shown in the console this app was started from.',
        h('button', { class: 'btn', type: 'button', text: 'Show recent log', onclick: showLog })),
      this.termsRow(info.terms),
      h('p', { class: 'small muted', text: 'Free to use; not to be passed on, published or sold. No warranty. See LICENSE.' }));

    const focused = this.body.contains(document.activeElement) ? document.activeElement.getAttribute('aria-label') : null;
    put(this.body, hotkey, audio, ai, text, learning, sounds, appearance, system, about);
    this.live(); this.capture();
    if (focused) this.body.querySelector(`[aria-label="${CSS.escape(focused)}"]`)?.focus();
  },
  termsRow(t) {
    t = t || {};
    let when = '';
    if (t.accepted_at) {
      const d = new Date(t.accepted_at);
      when = isNaN(d) ? t.accepted_at : d.toLocaleDateString([], { day: 'numeric', month: 'long', year: 'numeric' });
    }
    const how = { installer: 'in the installer', app: 'in the app' }[t.how] || '';
    const sub = !t.pending && t.accepted_version && t.accepted_version === t.version
      ? `Version ${t.version}, accepted ${when}${how ? ' ' + how : ''}.`
      : `Version ${t.version || ''}. Not accepted yet.`;
    return settingRow('Terms of Use', sub,
      h('button', { class: 'btn', type: 'button', text: 'View terms', onclick: () => termsScreen.show('view') }));
  },
  autostart() {
    if (!this.autoInput?.isConnected) return;
    this.autoInput.checked = !!S.config.autostart.enabled;
  },
  appearance() {
    const cur = Object.assign({}, S.config.settings.overlay);
    const defaults = S.config.overlay_defaults;
    const pill = makePill(); pill.source = 'demo'; pills.add(pill);
    pillMode(pill, 'bars');
    const dots = makePill(); pillMode(dots, 'dots');
    const status = h('span', { class: 'saved', 'aria-live': 'polite' });
    const warn = h('p', { class: 'msg warn', 'aria-live': 'polite' });
    const fg = h('input', { type: 'color', 'aria-label': 'Bar colour' }), bg = h('input', { type: 'color', 'aria-label': 'Pill colour' });
    const fgText = h('input', { class: 'input', 'aria-label': 'Bar colour (hex)', maxlength: '7', spellcheck: 'false' }), bgText = h('input', { class: 'input', 'aria-label': 'Pill colour (hex)', maxlength: '7', spellcheck: 'false' });
    const swatches = h('div', { class: 'swatches', role: 'group', 'aria-label': 'Presets' });
    const paint = (colors) => {
      fg.value = fgText.value = colors.color; bg.value = bgText.value = colors.background;
      setPillColors(pill, colors); setPillColors(dots, colors);
      const r = contrast(colors.color, colors.background);
      warn.textContent = r < 3 ? `Hard to see: these two colours have a contrast of only ${r.toFixed(1)}:1 (aim for 3:1 or more).` : '';
      for (const b of swatches.children) b.setAttribute('aria-pressed', b.dataset.c === colors.color + colors.background ? 'true' : 'false');
    };
    const save = async (colors) => {
      paint(colors);
      if (colors.color === S.config.settings.overlay.color && colors.background === S.config.settings.overlay.background) return;
      await saveSetting({ overlay: colors }, status);
      Object.assign(cur, S.config.settings.overlay);
      paint(Object.assign({}, cur));
    };
    for (const [name, c1, c2] of PRESETS) {
      swatches.append(h('button', { class: 'swatch', type: 'button', title: name, 'aria-label': name, 'data-c': c1 + c2,
        style: `background:${c2}`, onclick: () => save({ color: c1, background: c2 }) }, h('i', { style: `background:${c1}` })));
    }
    fg.addEventListener('input', () => { cur.color = fg.value; paint(cur); });
    bg.addEventListener('input', () => { cur.background = bg.value; paint(cur); });
    fg.addEventListener('change', () => save(Object.assign({}, cur)));
    bg.addEventListener('change', () => save(Object.assign({}, cur)));
    const fromText = (input, key) => input.addEventListener('change', () => {
      const v = input.value.trim().toLowerCase();
      if (/^#[0-9a-f]{6}$/.test(v)) { cur[key] = v; save(Object.assign({}, cur)); }
      else {             // not kept: put the colour in use back, and say why
        input.value = cur[key];
        status.textContent = `${q(v)} is not a colour. Type one like #ff4fa3.`; status.className = 'saved show err';
      }
    });
    fromText(fgText, 'color'); fromText(bgText, 'background');
    paint(cur);
    const show = h('button', { class: 'btn', type: 'button', text: 'Show it on screen', onclick: async () => {
      try { await api.post('/api/overlay/preview', {}); toast('Look at the bottom of your screen'); } catch (e) { fail(e); } } });
    const reset = h('button', { class: 'link', type: 'button', text: 'Reset to default', onclick: () => save(Object.assign({}, defaults)) });
    return h('section', { class: 'card group', id: 'set-appearance' },
      h('div', { class: 'group-head' }, h('h2', { text: 'Appearance' }), h('p', { text: 'The pill that appears at the bottom of your screen while you dictate.' })),
      h('div', { class: 'appearance' },
        h('div', { class: 'stage', role: 'img', 'aria-label': 'Preview of the pill' }, h('div', { class: 'stack', style: 'display:grid;gap:10px;justify-items:center' }, pill, dots)),
        h('div', { class: 'stack', style: 'flex:1 1 220px;min-width:0' },
          swatches,
          h('div', { class: 'colors' },
            h('label', { class: 'color-field' }, 'Bars', h('span', { class: 'row' }, fg, fgText)),
            h('label', { class: 'color-field' }, 'Pill', h('span', { class: 'row' }, bg, bgText))),
          warn,
          h('div', { class: 'row' }, show, reset, status))));
  },
  live() {
    if (!this.el?.isConnected || !this.hotkeyBox) return;
    const l = S.live || {}, hl = S.health || {}, s = S.config.settings;
    const hookNote = l.hotkey?.mouse ? (l.hook === 'active' ? 'While the app runs, this button no longer does its usual job.'
      : l.hook === 'fallback' ? 'Not blocked: the button still does its usual job too. Pause and resume to retry.' : '') : '';
    const canChange = !l.capturing && l.status === 'ready';
    const keep = this.hotkeyBox.contains(document.activeElement) || this.pauseKeyBox.contains(document.activeElement)
      ? document.activeElement.dataset.k : null;
    put(this.hotkeyBox, h('div', { class: 'hotkey-now-wrap' }, h('div', { class: 'hotkey-now', text: hotkeyWords() }),
      h('div', { class: 'small faint', text: l.hotkey?.technical || '' }), hookNote ? h('div', { class: 'small muted', text: hookNote }) : null),
      h('button', { class: 'btn', type: 'button', text: 'Change…', 'data-k': 'hk', 'aria-label': 'Change the hold-to-talk key', disabled: !canChange, onclick: () => this.startCapture('hotkey') }));
    const pk = s.pause_hotkey;
    put(this.pauseKeyBox, h('div', { class: 'hotkey-now-wrap' }, h('div', { class: pk ? 'hotkey-now small-key' : 'muted', text: pk ? s.pause_hotkey_plain : 'None' })),
      h('button', { class: 'btn', type: 'button', text: pk ? 'Change…' : 'Set…', 'data-k': 'pk', 'aria-label': pk ? 'Change the pause shortcut' : 'Set a pause shortcut', disabled: !canChange, onclick: () => this.startCapture('pause') }),
      pk ? h('button', { class: 'btn', type: 'button', text: 'Clear', 'data-k': 'pc', 'aria-label': 'Clear the pause shortcut', onclick: async () => {
        const res = await saveSetting({ pause_hotkey: null });
        if (res) { toast('Pause shortcut cleared'); this.live(); this.pauseKeyBox.querySelector('button')?.focus(); }
      } }) : null);
    if (keep) (this.hotkeyBox.querySelector(`[data-k="${keep}"]`) || this.pauseKeyBox.querySelector(`[data-k="${keep}"]`) || this.pauseKeyBox.querySelector('button'))?.focus();
    if (this.pauseInput) this.pauseInput.checked = !!l.paused;
    if (this.cleanupState) {
      const [w, k] = S.config.settings.cleanup_enabled ? cleanupWords(hl.cleanup?.state, hl.cleanup?.blocked, hl.cleanup) : ['', ''];
      put(this.cleanupState, w ? h('span', { class: 'row small muted', style: 'gap:6px' }, h('span', { class: 'dot ' + k }), w) : null);
    }
  },
  async startCapture(target) {
    S.captureFor = target;
    S.capture = { state: 'waiting', seconds: 10 };
    this.capture();
    try { await api.post('/api/hotkey/capture', {}); } catch (e) { S.capture = null; this.capture(); fail(e); }
  },
  capture() {
    if (!this.captureBox) return;
    const cap = S.capture;
    const pause = S.captureFor === 'pause';
    const box = pause ? this.pauseCaptureBox : this.captureBox, other = pause ? this.captureBox : this.pauseCaptureBox;
    put(other); other.dataset.shown = '';
    const closeBox = () => { S.capture = null; this.capture(); (pause ? this.pauseKeyBox : this.hotkeyBox).querySelector('button')?.focus(); };
    if (!cap || cap.state === 'cancelled' && box.dataset.shown !== 'waiting' && box.dataset.shown !== 'cancelled') { put(box); box.dataset.shown = ''; return; }
    const was = box.dataset.shown;
    box.dataset.shown = cap.state;
    const secs = Math.round(cap.seconds || 10);
    if (cap.state === 'waiting') {
      put(box, h('div', { class: 'capture', role: 'status' }, h('span', { class: 'ring', 'aria-hidden': 'true' }),
        h('div', { style: 'flex:1;min-width:200px' }, h('b', { text: pause ? 'Press the key or key combination you want for pausing' : 'Press the key, key combination or mouse button you want to hold' }),
          h('div', { class: 'small muted', text: `Press it and let go within ${secs} seconds. ` + (pause ? '' : 'Side and middle mouse buttons work too (they won’t go Back while you do this). ') + 'Esc cancels.' })),
        h('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: () => api.post('/api/hotkey/cancel', {}).catch(fail) })));
      box.querySelector('button').focus();
    } else if (cap.state === 'done') {
      const status = h('span', { class: 'saved', 'aria-live': 'polite' });
      const same = pause && cap.spec && S.live?.hotkey?.spec && cap.plain === S.live.hotkey.plain;
      put(box, h('div', { class: 'capture', role: 'status' },
        h('div', { style: 'flex:1;min-width:200px' }, h('div', { class: 'small muted', text: 'You pressed' }), h('div', { class: 'found', text: cap.plain }),
          h('div', { class: 'small faint', text: cap.technical }),
          same ? h('p', { class: 'msg err', text: 'That is already your hold-to-talk key. Choose a different key for pausing.' }) : null),
        same ? null : h('button', { class: 'btn primary', type: 'button', text: pause ? 'Use for pausing' : 'Use this key', onclick: async () => {
          const res = await saveSetting(pause ? { pause_hotkey: cap.spec } : { hotkey: cap.spec }, status);
          if (res) { closeBox(); toast(pause ? `Press ${cap.plain} to pause or resume` : `Hold ${cap.plain} to dictate`); this.live(); } } }),
        h('button', { class: 'btn', type: 'button', text: 'Try again', onclick: () => this.startCapture(S.captureFor) }),
        h('button', { class: 'link', type: 'button', text: pause ? 'Keep the current shortcut' : 'Keep the current key', onclick: closeBox }), status));
      box.querySelector('.btn')?.focus();
    } else {
      const text = cap.reason === 'user' ? 'Cancelled. Nothing changed.' : `Nothing was pressed within ${secs} seconds, so nothing changed.`;
      put(box, h('div', { class: 'capture', role: 'status' }, h('span', { class: 'muted', style: 'flex:1', text }),
        h('button', { class: 'btn', type: 'button', text: 'Try again', onclick: () => this.startCapture(S.captureFor) }),
        h('button', { class: 'link', type: 'button', text: 'Close', onclick: closeBox })));
      if (was === 'waiting') box.querySelector('.btn')?.focus();
    }
  },
  async micTest(select) {
    const list = S.config.devices.list;
    const device = select.value === '__missing' ? S.config.settings.input_device
      : select.value === '__default' || select.value === '__none' ? null : list[Number(select.value)].value;
    this.micBtn.disabled = true; this.micMsg.textContent = 'Listening…'; S.micLevel = 0;
    try { await api.post('/api/mic/test', { device }); }
    catch (e) { this.micBtn.disabled = false; this.micMsg.textContent = e.message; }
  },
  micLevel() {
    if (!this.meter?.isConnected) return;
    this.meter.firstChild.style.width = Math.min(100, Math.pow(Math.min(1, S.micLevel * 14), 0.6) * 100) + '%';
  },
  micDone(res) {
    if (!this.meter?.isConnected) return;
    this.micBtn.disabled = false; this.meter.firstChild.style.width = '0';
    if (!res.ok) { this.micMsg.textContent = res.error; return; }
    const p = res.peak;
    this.micMsg.textContent = p < 0.003 ? 'Nothing heard. Is it muted?' : p < 0.02 ? 'Very quiet. Move closer or raise its level in Windows.' : 'Sounds good.';
  },
};

async function showLog() {
  let data;
  try { data = await api.get('/api/log'); } catch (e) { return fail(e); }
  const pre = h('div', { class: 'log', tabindex: '0', 'aria-label': 'Recent log lines' });
  for (const line of data.lines) {
    pre.append(h('div', { class: / ERROR /.test(line) ? 'e' : / WARNING /.test(line) ? 'w' : '', text: line }));
  }
  if (!data.lines.length) pre.append('Nothing logged yet.');
  dialog(close => [h('div', { class: 'dlg-body' }, h('h2', { text: 'Recent log' }),
    h('p', { class: 'small', text: data.path ? 'The full log is in ' + data.path : 'Only recent lines are kept in the window.' }), pre),
  h('div', { class: 'dlg-foot' }, h('button', { class: 'btn primary', type: 'button', text: 'Close', onclick: () => close() }))], true);
  pre.scrollTop = pre.scrollHeight;
}

// ---------------------------------------------------------------- router
function route() {
  const [id, anchor] = (location.hash || '#home').slice(1).split('&');
  const view = VIEWS.some(v => v[0] === id) ? id : 'home';
  if (S.view !== view) views.history.flush();       // leaving: deletions happen now
  S.view = view;
  renderNav();
  const main = $('#main');
  main.replaceChildren();
  views.home.pill = null;
  views[view].mount(main);
  main.scrollTop = 0;
  const target = anchor && document.getElementById('set-' + anchor);
  if (target) { target.scrollIntoView({ block: 'start' }); (target.querySelector('h2') || target).focus({ preventScroll: true }); }
}

// ---------------------------------------------------------------- start
async function boot() {
  const params = new URLSearchParams(location.hash.slice(1));
  const ticket = params.get('t');
  let view = params.get('v') || 'home';
  if (ticket) {
    history.replaceState(null, '', '#' + view);         // the ticket never stays in the URL
    try {
      const res = await fetch('/api/hello', { method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ticket }) });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.key) sessionStorage.setItem(KEY_STORE, data.key);
    } catch (e) { /* handled below */ }
  }
  api.key = sessionStorage.getItem(KEY_STORE);
  if (!api.key) { gate('signin'); return; }
  try { await loadAll(); }
  catch (e) { if (e.status !== 401) gate('offline'); return; }
  $('#about-link').textContent = `Version ${S.state.info.version} · Log`;
  $('#about-link').addEventListener('click', showLog);
  $('#gate').hidden = true;
  $('#shell').hidden = false;
  connection.alive = true;
  window.addEventListener('hashchange', route);
  route();
  renderMini();
  termsScreen.sync();
  requestAnimationFrame(animate);
  connection.run();
}

boot();
