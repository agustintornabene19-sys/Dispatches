'use strict';

const INDEX_URL = 'index.json';

const els = {
  listView: document.getElementById('list-view'),
  readerView: document.getElementById('reader-view'),
  list: document.getElementById('issue-list'),
  status: document.getElementById('status-line'),
  tabs: document.getElementById('tabs'),
  refresh: document.getElementById('refresh-btn'),
  back: document.getElementById('back-btn'),
  openExt: document.getElementById('open-ext-btn'),
  frame: document.getElementById('reader-frame'),
  readerTitle: document.getElementById('reader-title'),
};

let issues = [];
let filter = 'ALL';
let currentFile = null;

function fmtDate(iso) {
  const d = new Date(iso + 'T00:00:00');
  if (isNaN(d)) return iso;
  return d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
}

function typeOf(it) {
  return (it.type || '').toUpperCase();
}

function render() {
  const items = issues
    .filter(it => filter === 'ALL' || typeOf(it) === filter)
    .slice()
    .sort((a, b) => (b.date || '').localeCompare(a.date || ''));

  els.list.innerHTML = '';
  if (!items.length) {
    els.status.textContent = issues.length ? 'No issues in this section yet.' : 'No issues found. Tap refresh once a digest has been published.';
    return;
  }
  els.status.textContent = '';

  for (const it of items) {
    const isDef = typeOf(it) === 'DEFILADE';
    const card = document.createElement('article');
    card.className = 'card' + (isDef ? ' defilade' : '');
    card.innerHTML =
      '<div class="card-kicker">' + (typeOf(it) || 'DIGEST') + '</div>' +
      '<h2 class="card-title"></h2>' +
      '<div class="card-date"></div>';
    card.querySelector('.card-title').textContent = it.title || (isDef ? 'Weekend Reading' : 'Daily Brief');
    card.querySelector('.card-date').textContent = fmtDate(it.date);
    card.addEventListener('click', () => openIssue(it));
    els.list.appendChild(card);
  }
}

function openIssue(it) {
  currentFile = it.file;
  els.frame.src = it.file;
  els.readerTitle.textContent = (typeOf(it) || 'Digest') + ' · ' + fmtDate(it.date);
  els.listView.classList.add('hidden');
  els.readerView.classList.remove('hidden');
  if (location.hash !== '#reader') history.pushState({ view: 'reader' }, '', '#reader');
  els.frame.scrollIntoView();
}

function showList() {
  els.readerView.classList.add('hidden');
  els.listView.classList.remove('hidden');
  els.frame.src = 'about:blank';
  currentFile = null;
}

// Issues are email-first (fixed 660px table layout). Inside the in-app reader
// they load in an iframe, which (a) tries to open links inside the frame — and
// many sites refuse to be framed, so the link looks broken — and (b) can't
// shrink the 660px layout, causing side-to-side scrolling on phones. Since the
// issue HTML is served from this same origin, we can reach into it on load and
// fix both: force links to open in a new tab, and force the content to fit.
function styleFrame() {
  try {
    const doc = els.frame.contentDocument;
    if (!doc || !doc.head || els.frame.src === 'about:blank') return;
    if (!doc.querySelector('base[data-dispatches]')) {
      const base = doc.createElement('base');
      base.target = '_blank';
      base.setAttribute('data-dispatches', '');
      doc.head.insertBefore(base, doc.head.firstChild);
    }
    if (!doc.querySelector('style[data-dispatches]')) {
      const st = doc.createElement('style');
      st.setAttribute('data-dispatches', '');
      st.textContent =
        'html,body{overflow-x:hidden!important;margin:0!important}' +
        'body *{max-width:100%!important}' +
        'table{width:100%!important}' +
        'img,svg,video{height:auto!important}';
      doc.head.appendChild(st);
    }
  } catch (e) {
    // If the frame is blank or ever cross-origin, leave it untouched.
  }
}
els.frame.addEventListener('load', styleFrame);

async function loadIndex(force) {
  const spinning = els.refresh;
  spinning.classList.add('spin');
  els.status.textContent = 'Loading…';
  try {
    const url = INDEX_URL + (force ? ('?t=' + Date.now()) : '');
    const res = await fetch(url, { cache: force ? 'reload' : 'default' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    issues = Array.isArray(data) ? data : (data.issues || []);
    render();
  } catch (e) {
    if (!issues.length) {
      els.status.textContent = navigator.onLine
        ? 'Could not load the issue list.'
        : 'Offline — no saved issues yet.';
    } else {
      render();
    }
  } finally {
    spinning.classList.remove('spin');
  }
}

// Events
els.tabs.addEventListener('click', e => {
  const btn = e.target.closest('.tab');
  if (!btn) return;
  filter = btn.dataset.filter;
  [...els.tabs.children].forEach(t => t.classList.toggle('active', t === btn));
  render();
});
els.refresh.addEventListener('click', () => loadIndex(true));
els.back.addEventListener('click', () => history.back());
els.openExt.addEventListener('click', () => { if (currentFile) window.open(currentFile, '_blank'); });

window.addEventListener('popstate', () => {
  if (location.hash !== '#reader') showList();
});

// Service worker
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('sw.js').catch(() => {}));
}

loadIndex(false);
