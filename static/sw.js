const CACHE_NAME = "workbench-shell-v0.3.140";
// Keep the shared request/recovery helper available to offline shell pages.
// It is appended below the shell list to avoid duplicating the long manifest line.
const SHELL = ["/", "/crawl4ai", "/projects/inbox", "/projects/knowledge", "/projects/aihot", "/projects/idea-analysis", "/projects/web-research", "/projects/cloud-dev", "/automation", "/git", "/github-tools", "/approvals", "/static/workbench.html", "/static/workbench.css", "/static/workbench.js", "/static/workspace-search.js", "/static/index.html", "/static/styles.css", "/static/app.js", "/static/llm-settings.js", "/static/project.js", "/static/project.css", "/static/project-agent.css", "/static/platform.css", "/static/platform.js", "/static/automation.html", "/static/git.html", "/static/github-tools.html", "/static/approvals.html", "/static/inbox.html", "/static/knowledge.html", "/static/aihot.html", "/static/aihot.css", "/static/idea-analysis.html", "/static/idea-analysis.css", "/static/server.html", "/static/doc-factory.html", "/static/sub2api.html", "/static/sub2api.css", "/static/sub2api-agent.css", "/static/sub2api-agent.js", "/static/market.html", "/static/market.css", "/static/web-research.html", "/static/web-research.css", "/static/web-research.js", "/static/cloud-dev.html", "/static/manifest.webmanifest", "/static/icons/workbench-192.svg", "/static/icons/workbench-512.svg", "/static/icons/workbench-192.png", "/static/icons/workbench-512.png"];
SHELL.push("/static/request.js");

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return;
  if (event.request.method !== "GET") return;
  event.respondWith(fetch(event.request).then((response) => {
    const copy = response.clone();
    caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
    return response;
  }).catch(() => caches.match(event.request, { ignoreSearch: true }).then((cached) => cached || caches.match("/", { ignoreSearch: true }))));
});
