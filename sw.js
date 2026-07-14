'use strict';

// Bump this version to force the shell to refresh after you edit app files.
const VERSION = 'dispatches-v2';
const SHELL = [
  './',
  'index.html',
  'styles.css',
  'app.js',
  'manifest.webmanifest',
  'icon.svg',
  'icon-maskable.svg'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  const file = url.pathname.split('/').pop();

  // index.json: network-first so new issues show up; fall back to cache offline.
  if (file === 'index.json') {
    event.respondWith(
      fetch(req).then(res => {
        const copy = res.clone();
        caches.open(VERSION).then(c => c.put('index.json', copy));
        return res;
      }).catch(() => caches.match('index.json'))
    );
    return;
  }

  // Issue HTML: stale-while-revalidate (read offline once visited).
  if (file && file.endsWith('.html') && file !== 'index.html') {
    event.respondWith(
      caches.open(VERSION).then(async cache => {
        const cached = await cache.match(req);
        const network = fetch(req).then(res => { cache.put(req, res.clone()); return res; }).catch(() => cached);
        return cached || network;
      })
    );
    return;
  }

  // Everything else (shell): cache-first.
  event.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      const copy = res.clone();
      caches.open(VERSION).then(c => c.put(req, copy));
      return res;
    }))
  );
});
