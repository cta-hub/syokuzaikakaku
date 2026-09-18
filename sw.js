/* 買い時チェッカー Service Worker
   方針：キャッシュ優先（店内の圏外でも必ず表示）。
   VERSION は scripts/update_data.py が index.html と manifest.json の内容から自動計算して書き換えます。
   手で index.html を直してActionsを使わない場合は、VERSION の文字列を何でもいいので変えてください
   （変えないと、スマホに古い版が残り続けます）。 */
const VERSION = '7eff2e7a14';
const CACHE = 'kaidoki-' + VERSION;
const ASSETS = ['./', './index.html', './manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // cache:'reload' = ブラウザのHTTPキャッシュを通さず、必ずサーバーの最新版を取る
      .then((c) => c.addAll(ASSETS.map((u) => new Request(u, { cache: 'reload' }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('kaidoki-') && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  if (new URL(req.url).origin !== self.location.origin) return;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    // ページ遷移は ?utm=... などが付いてもキャッシュに当てる
    const hit = await cache.match(req, { ignoreSearch: req.mode === 'navigate' });
    if (hit) return hit;
    try {
      const res = await fetch(req);
      if (res.ok && res.type === 'basic') cache.put(req, res.clone());
      return res;
    } catch (err) {
      if (req.mode === 'navigate') {
        const fallback = (await cache.match('./index.html')) || (await cache.match('./'));
        if (fallback) return fallback;
      }
      throw err;
    }
  })());
});
