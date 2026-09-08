/* Bili 推流管理 Service Worker — 标准库后端，无构建步骤。
 * 策略：App Shell 缓存优先可离线；/api/* 永远走网络；导航请求网络优先、失败回退缓存。 */
const CACHE = "bili-push-v1";
const SHELL = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // API 与日志永远走网络：状态/推流控制不能读到过期缓存。
  if (url.pathname.startsWith("/api/")) return;
  // 导航：网络优先，离线时回退到缓存的 Shell。
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put("/index.html", copy));
          return res;
        })
        .catch(() => caches.match("/index.html", { ignoreSearch: true })),
    );
    return;
  }
  // 静态资源：缓存优先，命中直接返回；未命中走网络并回填缓存。
  event.respondWith(
    caches.match(request, { ignoreSearch: false }).then(
      (hit) =>
        hit ||
        fetch(request).then((res) => {
          if (res.ok && (url.pathname === "/" || SHELL.includes(url.pathname))) {
            const copy = res.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return res;
        }),
    ),
  );
});
