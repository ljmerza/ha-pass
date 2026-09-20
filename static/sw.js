const CACHE_VERSION = 'CACHE_VERSION_PLACEHOLDER';

// Only local assets in install cache — cross-origin fonts are cached at
// runtime via cache-first in the fetch handler. This prevents SW install
// failure on LAN-only deployments where Google Fonts is unreachable.
//
// Plain paths on purpose. Pages request these with a ?v=<build> suffix, but the
// build is stamped into this file's CACHE_VERSION only, so precaching the
// versioned URLs would mean templating the list too. The fetch handler matches
// with ignoreSearch instead, and a new build gets a new cache name, so a stale
// entry can never survive to be matched.
const SHELL_ASSETS = [
  '/static/dist.css',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-maskable-192.png',
  '/static/icons/icon-maskable-512.png',
  '/static/domains.js',
  '/static/theme.js',
  '/static/util.js',
  '/static/qrcode.min.js',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then(cache => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  if (
    url.pathname.includes('/stream') ||
    url.pathname.startsWith('/admin') ||
    url.pathname.includes('/state') ||
    url.pathname.includes('/command') ||
    url.pathname.includes('/manifest.json')
  ) {
    return;
  }

  // stale-while-revalidate
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      // ignoreSearch, or the ?v=<build> on every asset URL would miss the
      // plain paths precached at install and fall through to the network on
      // every single load — a silently disabled cache.
      caches.match(event.request, { ignoreSearch: true }).then(cached => {
        const fetchPromise = fetch(event.request).then(response => {
          const clone = response.clone();
          caches.open(CACHE_VERSION).then(cache => cache.put(event.request, clone));
          return response;
        });
        return cached || fetchPromise;
      })
    );
    return;
  }

  // cache-first
  if (url.hostname === 'fonts.googleapis.com' ||
      url.hostname === 'fonts.gstatic.com') {
    event.respondWith(
      caches.match(event.request).then(cached => cached || fetch(event.request).then(response => {
        const clone = response.clone();
        caches.open(CACHE_VERSION).then(cache => cache.put(event.request, clone));
        return response;
      }))
    );
    return;
  }

  // network-first
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
