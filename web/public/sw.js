// rebas_daily Service Worker：网络优先 + 缓存兜底。
// 日刊内容每天更新——在线永远拿最新，离线回退到最近读过的版本。
// 页面渲染本身零 JS，这里只是可安装性与离线的渐进增强层。
const CACHE = "rebas-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then(
          (hit) =>
            hit ||
            new Response("离线，且该页尚未缓存。", {
              status: 503,
              headers: { "Content-Type": "text/plain; charset=utf-8" },
            }),
        ),
      ),
  );
});
