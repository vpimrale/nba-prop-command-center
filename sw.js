// Minimal offline-shell service worker.
//
// Scope and honesty note (see README for the full explanation): this only caches the
// app's own files (HTML/CSS/JS/icons) so the app SHELL still opens when offline or when
// a network request fails. It does NOT cache live ESPN scoreboard/box-score data, and it
// does NOT provide any cross-device sync — localStorage is still per-browser, per-device.
// Service workers only register over http/https (not file://), so this has no effect at
// all when the app is opened directly as a file — it only matters if you run it through
// `python -m http.server` (or deploy it to a real host) and open it that way.
// Cache name bumped (shell-1 -> shell-2) specifically for the move to being
// served over a real HTTPS host for iPhone installs — guarantees the very
// first load there fetches fresh files rather than serving anything that
// might already be cached under the old name from an earlier local-server
// session on the same browser.
// shell-3 (v6.1): + game-binding.js and the vendored, pinned tesseract.js bundle.
const CACHE_NAME = 'nba-prop-cc-v6-shell-3';
const SHELL_FILES = [
  './', './index.html', './style.css', './app.js', './teams.js', './parse-slip.js',
  './line-rules.js', './prop-status.js', './settlement.js', './game-binding.js', './ladder.js', './archive.js',
  './vendor/tesseract.js-5.1.1/tesseract.min.js',
  './manifest.json', './icon-180.png', './icon-192.png', './icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)).catch(() => {
      // If any one file 404s (e.g. optional files), don't block install entirely.
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // Only handle same-origin GET requests for our own shell files — everything else
  // (ESPN's scoreboard/summary/roster APIs) passes straight through to the network,
  // untouched, so live data behavior is never masked or staled by this worker.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy)).catch(() => {});
        return resp;
      }).catch(() => cached);
    })
  );
});
