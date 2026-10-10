// Service worker del Digest: rende il sito installabile e leggibile anche senza rete.
// - pagina e dati (digests/*.json): prima la rete, così si vede sempre l'ultimo digest;
//   se la rete manca si usa la copia salvata (i giorni già aperti restano leggibili offline).
// - icone, font e librerie: prima la copia salvata.
// - audio (MP3 e indici): sempre dalla rete, mai salvato.
const VERSION = 'digest-v2';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/favicon-32.png'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request) {
  const cache = await caches.open(VERSION);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch (e) {
    const cached = await cache.match(request, { ignoreSearch: true });
    if (cached) return cached;
    if (request.mode === 'navigate') return cache.match('./');
    throw e;
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(VERSION);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok || response.type === 'opaque') cache.put(request, response.clone());
  return response;
}

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const sameOrigin = url.origin === self.location.origin;
  // l'audio va sempre in rete: gli MP3 sono grandi e il lettore chiede pezzi del file (Range)
  if (req.headers.has('range') || url.pathname.includes('/audio/')) return;
  if (req.mode === 'navigate' || (sameOrigin && /\/(digests\/[^/]+\.json|index\.html)?$/.test(url.pathname))) {
    event.respondWith(networkFirst(req));
  } else if (sameOrigin || /fonts\.(googleapis|gstatic)\.com|cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net/.test(url.host)) {
    event.respondWith(cacheFirst(req));
  }
});
