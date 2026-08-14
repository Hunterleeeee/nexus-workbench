const CACHE_NAME = "workbench-shell-v0.3.184";
// Keep the shared request/recovery helper available to offline shell pages.
// It is appended below the shell list to avoid duplicating the long manifest line.
const SHELL = ["/", "/crawl4ai", "/projects/inbox", "/projects/knowledge", "/projects/doc-factory", "/projects/sub2api", "/projects/market", "/projects/server", "/projects/aihot", "/projects/ai-learning", "/projects/embodied", "/projects/idea-analysis", "/projects/product-manager", "/projects/cid-dashboard", "/projects/web-research", "/projects/cloud-dev", "/automation", "/git", "/github-tools", "/approvals", "/static/workbench.html", "/static/workbench.css", "/static/workbench.js", "/static/workspace-search.js", "/static/index.html", "/static/styles.css", "/static/app.js", "/static/llm-settings.js", "/static/project.js", "/static/markdown.js", "/static/project.css", "/static/project-agent.css", "/static/platform.css", "/static/platform.js", "/static/automation.html", "/static/git.html", "/static/github-tools.html", "/static/approvals.html", "/static/inbox.html", "/static/knowledge.html", "/static/aihot.html", "/static/aihot.css", "/static/ai-learning.html", "/static/ai-learning.css", "/static/ai-learning.js", "/static/idea-analysis.html", "/static/idea-analysis.css", "/static/product-manager.html", "/static/product-manager.css", "/static/product-manager.js", "/static/vendor/cowart/index-pR7Yavzt.js", "/static/vendor/cowart/style-D82LwrRu.css", "/static/server.html", "/static/doc-factory.html", "/static/sub2api.html", "/static/sub2api.css", "/static/sub2api-agent.css", "/static/sub2api-agent.js", "/static/market.html", "/static/market.css", "/static/web-research.html", "/static/web-research.css", "/static/web-research.js", "/static/cloud-dev.html", "/static/manifest.webmanifest", "/static/icons/workbench-192.svg", "/static/icons/workbench-512.svg", "/static/icons/workbench-192.png", "/static/icons/workbench-512.png", "/usage", "/static/usage.html", "/static/usage.css", "/static/usage.js", "/static/market-today.js", "/static/market-screen.js", "/static/market-style.js", "/static/web-research-plus.js", "/static/web-research-plus.css"];
SHELL.push("/static/request.js", "/static/theme.js", "/static/theme.css");

self.addEventListener("install", (event) => {
  // 逐个 add 而不是 addAll：addAll 是全有全无的，SHELL 里只要有一个路径拼错或
  // 文件被删，整个 install 就 reject，新 Service Worker 永远装不上，用户会一直
  // 被旧壳服务，而且没有任何提示。
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => Promise.all(
    SHELL.map((path) => cache.add(path).catch((error) => console.warn("[sw] 壳资源缓存失败：", path, error)))
  )).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return;
  if (event.request.method !== "GET") return;
  // 跨域请求（CDN、图床）不进我们的壳缓存：opaque 响应放进去只会占空间，
  // 而且离线时拿出来也是不可读的。
  if (url.origin !== self.location.origin) return;
  event.respondWith(fetch(event.request).then((response) => {
    // 只缓存真正成功的响应。原来是来什么存什么：一次 404 或 500 会被写进壳缓存，
    // 之后每次离线回退都拿到那份错误页，而且要等到下个版本换 CACHE_NAME 才会清掉。
    if (response.ok && response.type === "basic") {
      const copy = response.clone();
      caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
    }
    return response;
  }).catch(() => matchCached(event.request)));
});

// 带版本号的静态资源必须精确匹配。原来统一用 ignoreSearch: true，
// 于是 /static/ai-learning.js?v=0.3.184 会命中缓存里 v=0.3.153 那份——
// 整套 ?v= 缓存失效机制在离线回退这条路径上等于没有。
// 页面文档仍然忽略查询串：/projects/x?tab=1 没必要单独缓存一份。
function matchCached(request) {
  const url = new URL(request.url);
  const versioned = url.pathname.startsWith("/static/") && url.searchParams.has("v");
  return caches.match(request, { ignoreSearch: !versioned })
    .then((cached) => cached || (versioned ? caches.match(url.pathname) : null))
    .then((cached) => cached || caches.match("/", { ignoreSearch: true }));
}
