/* Storm — app shell cache + always-fresh day data */
const CACHE = "storm-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest",
               "./icons/icon-192.png", "./icons/icon-512.png",
               "./icons/icon-maskable-512.png",
               "./icons/apple-touch-icon.png", "./icons/favicon-32.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  if (e.request.method !== "GET") return;
  if (e.request.headers.has("range")) return;

  // Cache by pathname only, so the ?ts=/?sw= cache-busters do not grow
  // the cache with a new entry on every pull.
  const key = new Request(url.origin + url.pathname);
  const busted = new URL(url);
  busted.searchParams.set("sw", Date.now());

  e.respondWith(
    fetch(busted.toString(), { cache: "no-store" })
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(key, copy));
        }
        return res;
      })
      .catch(() => caches.match(key).then((hit) =>
        hit || (e.request.mode === "navigate"
          ? caches.match("./index.html")
          : caches.match("./index.html"))
      ))
  );
});
