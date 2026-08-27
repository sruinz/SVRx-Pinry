/* eslint-env serviceworker */
/* eslint-disable no-restricted-globals */

const NETWORK_ONLY_VERSION = 'network-only-v1';
const LEGACY_CACHE_PREFIX = 'pinry-spa-';

self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const cacheNames = await caches.keys();
    await Promise.all(cacheNames
      .filter(name => name.startsWith(LEGACY_CACHE_PREFIX))
      .map(name => caches.delete(name)));
    await self.clients.claim();
    const windows = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    });
    windows.forEach((client) => {
      client.navigate(client.url).catch(() => undefined);
    });
  })());
});

self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') return;

  event.respondWith(fetch(new Request(event.request, { cache: 'no-store' })));
});

self.addEventListener('message', (event) => {
  if (!event.data || event.data.type !== 'SVRX_PINRY_NETWORK_ONLY_PING') return;
  if (!event.source || typeof event.source.postMessage !== 'function') return;

  event.source.postMessage({
    type: 'SVRX_PINRY_NETWORK_ONLY_ACK',
    version: NETWORK_ONLY_VERSION,
  });
});
