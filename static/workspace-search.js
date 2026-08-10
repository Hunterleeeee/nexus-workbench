(function setupWorkspaceSearch() {
  const input = document.querySelector("#project-search");
  const host = input?.closest(".search-box");
  if (!input || !host) return;
  host.style.position = "relative";
  const panel = document.createElement("div");
  panel.id = "workspace-search-results";
  panel.className = "workspace-search-results hidden";
  panel.setAttribute("role", "listbox");
  host.appendChild(panel);
  let timer = 0;
  let requestSeq = 0;
  const close = () => panel.classList.add("hidden");
  const render = (results, query) => {
    if (!results.length) {
      panel.innerHTML = `<div class="workspace-search-empty">没有找到“${escapeHtml(query)}”相关的项目、工作项或产物。</div>`;
      panel.classList.remove("hidden");
      return;
    }
    panel.innerHTML = results.slice(0, 12).map((item) => `<a class="workspace-search-result" role="option" href="${escapeHtml(item.href || "/")}" target="_blank" rel="noopener"><span class="workspace-search-type">${escapeHtml(item.type_label || item.type || "结果")}</span><strong>${escapeHtml(item.title || "未命名")}</strong><small>${escapeHtml(item.description || "")}</small></a>`).join("");
    panel.classList.remove("hidden");
  };
  const lookup = async (query) => {
    const seq = ++requestSeq;
    try {
      const body = await (window.WorkbenchUX?.requestJson ? window.WorkbenchUX.requestJson(`/api/search?q=${encodeURIComponent(query)}&limit=24`) : fetch(`/api/search?q=${encodeURIComponent(query)}&limit=24`).then(async (response) => { const value = await response.json(); if (!response.ok) throw new Error(value.detail || `请求未完成（${response.status}）`); return value; }));
      if (seq === requestSeq) render(body.results || [], query);
    } catch (error) {
      if (seq === requestSeq) { panel.innerHTML = `<div class="workspace-search-empty">搜索暂时不可用：${escapeHtml(error.message)}</div>`; panel.classList.remove("hidden"); }
    }
  };
  input.addEventListener("input", () => {
    if (typeof renderProjects === "function") renderProjects();
    const query = input.value.trim();
    window.clearTimeout(timer);
    if (query.length < 2) { close(); return; }
    panel.innerHTML = "<div class=\"workspace-search-empty\">正在搜索工作项、产物和项目…</div>";
    panel.classList.remove("hidden");
    timer = window.setTimeout(() => lookup(query), 220);
  });
  document.addEventListener("click", (event) => { if (!host.contains(event.target)) close(); });
})();
