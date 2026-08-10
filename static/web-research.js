/* 网页研究 · AI 浏览器
 * 浏览器式壳：地址栏 + 标签页 + iframe 网页视图 + 阅读器（可划词）+ AI 伴读。
 * Gemini 本机桥已迁移到「全局 LLM / 本机 Gemini」设置弹窗（index.html + llm-settings.js）。
 */
const $ = (selector) => document.querySelector(selector);
const storageKey = "workbench-web-research-contexts-v1";
const state = { contexts: [], activeId: "", run: null, polling: null, selectedText: "" };
const statusLabels = { queued: "排队中", running: "抓取中", completed: "已读完", failed: "失败", cancelled: "已取消", cancelling: "正在取消" };

function escapeHtml(value) { return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;"); }
function formatDate(value) { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function safeHttpUrl(value) { const text = String(value ?? "").trim(); if (!text) return ""; try { const parsed = new URL(text); if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password) return ""; return parsed.href; } catch (_) { return ""; } }
function hostOf(url) { try { return new URL(url).hostname.replace(/^www\./, ""); } catch (_) { return url; } }
function openInBrowser(url) {
  // 桌面壳优先：独立真浏览器窗口（不受 iframe 嵌入限制）；否则系统浏览器。
  if (window.desktopShell && typeof window.desktopShell.openWebWindow === "function") {
    void window.desktopShell.openWebWindow(url);
    return;
  }
  window.open(url, "_blank", "noopener");
}
function desktopShellAvailable() {
  return Boolean(window.desktopShell && typeof window.desktopShell.openWebWindow === "function");
}
function setBusy(button, busy, label) { if (!button) return; if (busy) { button.dataset.idle = button.textContent; button.disabled = true; button.setAttribute("aria-busy", "true"); button.textContent = label; } else { button.disabled = false; button.removeAttribute("aria-busy"); if (button.dataset.idle) button.textContent = button.dataset.idle; } }
function showError(host, message) { host.innerHTML = `<div class="empty-result"><strong>暂时无法读取</strong><p>${escapeHtml(message)} · 请重试</p></div>`; }

/* ── 轻量 markdown → 安全 HTML（阅读器正文） ── */
function markdownLight(text) {
  const lines = String(text ?? "").replace(/\r/g, "").split("\n");
  const out = []; let list = null;
  const inline = (line) => line
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) { if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; } continue; }
    const fence = line.match(/^```/);
    if (fence) { if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; } out.push(`<pre>${escapeHtml(line.replace(/^```\w*/, ""))}</pre>`); continue; }
    const heading = line.match(/^(#{2,4})\s+(.*)/);
    if (heading) { if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; } const level = Math.min(4, heading[1].length); out.push(`<h${level}>${inline(heading[2])}</h${level}>`); continue; }
    const item = line.match(/^[-*•]\s+(.*)/);
    if (item) { list = list || []; list.push(`<li>${inline(item[1])}</li>`); continue; }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`<ul>${list.join("")}</ul>`);
  return out.join("");
}

/* ── 研究上下文（浏览器标签） ── */
function loadContexts() { try { const value = JSON.parse(localStorage.getItem(storageKey) || "[]"); state.contexts = Array.isArray(value) ? value.filter((item) => item && item.id).slice(0, 12) : []; } catch (_) { state.contexts = []; } }
function saveContexts() { localStorage.setItem(storageKey, JSON.stringify(state.contexts.slice(0, 12))); }
function activeContext() { return state.contexts.find((item) => item.id === state.activeId) || null; }
function newContext(values = {}) { const context = { id: crypto.randomUUID ? crypto.randomUUID() : `ctx-${Date.now()}`, title: "新标签", url: "", readerHtml: "", readerMeta: "", runId: "", ...values, updatedAt: new Date().toISOString() }; state.contexts.unshift(context); state.contexts = state.contexts.slice(0, 12); state.activeId = context.id; saveContexts(); return context; }
function updateContext(values) {
  const context = activeContext(); if (!context) return;
  const changed = Object.entries(values).some(([key, value]) => context[key] !== value);
  if (!changed) return;
  Object.assign(context, values, { updatedAt: new Date().toISOString() });
  saveContexts(); renderTabs();
}
function renderTabs() {
  const host = $("#context-tabs"); if (!host) return;
  host.innerHTML = state.contexts.map((item) => `<button class="browser-tab ${item.id === state.activeId ? "active" : ""}" data-context-id="${escapeHtml(item.id)}" type="button" role="listitem" title="${escapeHtml(item.url || "空标签")}"><i></i><span>${escapeHtml(item.title || "新标签")}</span><em data-close-context="${escapeHtml(item.id)}" title="关闭标签" aria-label="关闭标签">×</em></button>`).join("");
  host.querySelectorAll("[data-context-id]").forEach((button) => button.addEventListener("click", (event) => { if (event.target.closest("[data-close-context]")) return; selectContext(button.dataset.contextId); }));
  host.querySelectorAll("[data-close-context]").forEach((close) => close.addEventListener("click", (event) => { event.stopPropagation(); closeContext(close.dataset.closeContext); }));
}
function selectContext(id) {
  if (!state.contexts.some((item) => item.id === id)) return;
  state.activeId = id; state.run = null; state.selectedText = "";
  if (state.polling) { clearTimeout(state.polling); state.polling = null; }
  renderTabs();
  const context = activeContext();
  if (!context) return;
  $("#address-input").value = context.url || "";
  $("#selection-action").disabled = true;
  if (context.readerHtml) { showReader(context); enableCopilot(); }
  else if (context.url) {
    // 已打开过但还没抓取完成：切到阅读器等待/继续抓取
    $("#iframe-host").hidden = true; $("#reader-view").hidden = false;
    $("#reader-meta").textContent = "";
    $("#reader-body").innerHTML = '<div class="reader-empty">正在阅读…正文抓取完成后显示在这里。</div>';
    enableCopilot();
  }
  else { $("#iframe-host").hidden = true; $("#reader-view").hidden = false; $("#reader-body").innerHTML = '<div class="reader-empty">新标签页。在地址栏输入网址，回车即可打开并自动阅读。</div>'; $("#reader-meta").textContent = ""; disableCopilot("等待打开网页"); $("#browser-status").textContent = "新标签页"; }
  if (context.runId) void loadRun(context.runId);
}
function closeContext(id) {
  const index = state.contexts.findIndex((item) => item.id === id);
  if (index < 0) return;
  state.contexts.splice(index, 1); saveContexts();
  if (state.activeId === id) { state.activeId = state.contexts[0]?.id || ""; state.run = null; }
  if (!state.contexts.length) { const context = newContext(); state.activeId = context.id; }
  renderTabs(); selectContext(state.activeId);
}

/* ── 视图：阅读器（默认）/ iframe 原网页 ── */
let viewMode = "reader"; // reader | shot | iframe
function setView(mode) {
  viewMode = mode;
  const context = activeContext();
  const toggle = $("#toggle-view");
  if (mode === "shot") {
    if (context?.url) void loadShot(context.url);
    if (toggle) { toggle.textContent = "阅读器视图"; toggle.disabled = false; }
  } else if (mode === "iframe") {
    if (context?.url) showIframe(context.url);
    if (toggle) { toggle.textContent = "阅读器视图"; toggle.disabled = !context?.url; }
  } else {
    if (context?.readerHtml) showReader(context);
    else if (context?.url) { $("#reader-view").hidden = false; $("#iframe-host").hidden = true; $("#reader-body").innerHTML = '<div class="reader-empty">正在阅读…正文抓取完成后显示在这里。</div>'; }
    if (toggle) { toggle.textContent = "原网页视图"; toggle.disabled = !context?.url; }
  }
}
function showIframe(url) {
  const host = $("#iframe-host"); const frame = $("#page-frame"); const reader = $("#reader-view");
  reader.hidden = true; host.hidden = false;
  $("#iframe-note").hidden = true; $("#open-external").hidden = false;
  $("#open-external").onclick = () => { openInBrowser(url); };
  if (frame.src !== url) frame.src = url;
  $("#address-input").value = url;
  const toggle = $("#toggle-view");
  if (toggle) { toggle.textContent = "真实页面"; toggle.disabled = false; }
  $("#browser-status").textContent = `原网页视图：${hostOf(url)}（部分网站会拒绝内嵌，可点「真实页面」用服务器渲染截图）`;
}
function showReader(context) {
  $("#iframe-host").hidden = true; $("#reader-view").hidden = false;
  $("#reader-meta").textContent = context.readerMeta || "";
  $("#reader-body").innerHTML = context.readerHtml || '<div class="reader-empty">没有正文。</div>';
  $("#open-external").hidden = false;
  $("#open-external").onclick = () => { if (context.url) openInBrowser(context.url); };
  const toggle = $("#toggle-view");
  if (toggle) { toggle.textContent = "真实页面"; toggle.disabled = !context?.url; }
  $("#browser-status").textContent = `已读取 ${hostOf(context.url || "")} 的正文 · 可选中文字提问`;
}
async function loadShot(url) {
  const host = $("#shot-view-host"); const img = $("#shot-image");
  $("#iframe-host").hidden = true; $("#reader-view").hidden = true; host.hidden = false;
  img.alt = "正在渲染…";
  img.src = "";
  $("#browser-status").textContent = "服务器正在用无头浏览器渲染真实页面…";
  const shot = $("#shot-view");
  if (shot) shot.disabled = true;
  try {
    const body = await window.requestJson("/api/browser/render", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
    if (!body.ok) { $("#shot-note").textContent = body.message || "渲染失败"; return; }
    img.src = body.url;
    $("#browser-status").textContent = `已渲染 ${hostOf(url)} 的真实页面（服务器截图）`;
  } catch (error) { $("#shot-note").textContent = `渲染失败：${error.message}`; }
  finally { if (shot) shot.disabled = false; }
}
function enableCopilot() {
  const context = activeContext();
  const chatReady = Boolean(context?.runId);
  $("#toggle-view").disabled = !context?.url;
  $("#shot-view").disabled = !context?.url;
  $("#chat-input").disabled = !chatReady; $("#chat-submit").disabled = !chatReady;
  document.querySelectorAll("[data-copilot-quick]").forEach((button) => { button.disabled = !chatReady; });
  $("#selection-action").disabled = true;
  $("#copilot-state").textContent = chatReady ? "可以提问" : "阅读中…完成后即可提问";
}
function disableCopilot(label) {
  $("#toggle-view").disabled = true; $("#shot-view").disabled = true;
  document.querySelectorAll("[data-copilot-quick]").forEach((button) => { button.disabled = true; });
  $("#selection-action").disabled = true;
  $("#chat-input").disabled = true; $("#chat-submit").disabled = true; $("#handoff-evidence").disabled = true;
  $("#copilot-state").textContent = label || "等待打开网页";
}
function appendMessage(role, content) {
  const host = $("#copilot-messages");
  const hint = host.querySelector(".chat-hint"); if (hint) hint.remove();
  const item = document.createElement("div");
  item.className = `chat-message ${role}`;
  const label = document.createElement("span"); label.className = "chat-label"; label.textContent = role === "user" ? "你" : "伴读";
  const body = document.createElement("span"); body.className = "chat-body"; body.textContent = content;
  item.append(label, body); host.append(item); host.scrollTop = host.scrollHeight;
}
function appendThinking() {
  const host = $("#copilot-messages");
  const hint = host.querySelector(".chat-hint"); if (hint) hint.remove();
  const item = document.createElement("div");
  item.className = "chat-message assistant thinking";
  item.innerHTML = '<span class="chat-label">伴读</span><span class="chat-body thinking-dots"><i></i><i></i><i></i>正在思考…</span>';
  host.append(item); host.scrollTop = host.scrollHeight;
  return item;
}

/* ── 地址栏：打开并自动阅读 ── */
function openAddress(raw) {
  const url = safeHttpUrl(raw);
  if (!url) { $("#browser-status").textContent = "请输入完整 http/https 网址（暂不支持搜索词）"; return; }
  let context = activeContext();
  if (!context) { context = newContext({ title: hostOf(url), url }); renderTabs(); }
  else if (!context.url) { updateContext({ title: hostOf(url), url }); }
  context = activeContext();
  if (context.url && context.url !== url) updateContext({ title: hostOf(url), url, readerHtml: "", readerMeta: "", runId: "" });
  state.run = null;
  $("#reader-view").hidden = false; $("#iframe-host").hidden = true;
  $("#reader-meta").textContent = "";
  $("#reader-body").innerHTML = '<div class="reader-empty">正在打开并阅读…正文抓取完成后显示在这里。</div>';
  enableCopilot();
  void aiRead();
}

/* ── AI 阅读：抓取当前页 → 阅读器 + 伴读 ── */
async function aiRead() {
  const context = activeContext();
  if (!context?.url) return;
  $("#copilot-state").textContent = "阅读中…";
  $("#browser-status").textContent = "正在抓取并整理网页内容…";
  appendMessage("user", `请阅读 ${hostOf(context.url)} 这个网页，总结核心内容。`);
  const thinking = appendThinking();
  try {
    const body = await window.requestJson("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ urls: [context.url], task: "总结这个网页的核心内容、关键事实和信息缺口。", render_js: true, refresh: false, max_depth: 1, max_pages: 1 }) });
    updateContext({ runId: body.run_id });
    state.run = null;
    await loadRun(body.run_id);
  } catch (error) {
    thinking.remove();
    $("#copilot-state").textContent = "阅读失败";
    $("#browser-status").textContent = `阅读失败：${error.message}`;
    appendMessage("assistant", `阅读失败：${error.message}`);
  }
}

/* ── 任务轮询 ── */
function schedulePoll(runId) { if (state.polling) clearTimeout(state.polling); state.polling = setTimeout(() => { void loadRun(runId); }, 1500); }
async function loadRun(runId) {
  try {
    const run = await window.requestJson(`/api/runs/${encodeURIComponent(runId)}`);
    if (!activeContext() || activeContext().runId !== runId) return;
    renderRun(run);
    if (["queued", "running", "cancelling"].includes(run.status)) schedulePoll(runId);
  } catch (error) { $("#browser-status").textContent = error.message; }
}
function renderRun(run) {
  state.run = run;
  const context = activeContext();
  const status = run.status || "queued";
  $("#copilot-state").textContent = statusLabels[status] || status;
  $("#browser-status").textContent = run.error ? `任务失败：${run.error}` : status === "completed" ? "已读完 · 正文在左侧，可以选中文字提问" : `正在${statusLabels[status] || status}…`;
  const docs = run.documents || [];
  if (status === "completed" && docs.length) {
    const doc = docs[0];
    const sourceUrl = safeHttpUrl(doc.url);
    const title = doc.title || hostOf(doc.url) || "未命名页面";
    const meta = `${title}${sourceUrl ? ` · ${sourceUrl}` : ""} · ${formatDate(doc.data_as_of || run.finished_at)} · ${Number(doc.markdown_chars || 0).toLocaleString("zh-CN")} 字 · ${doc.source_quality?.label ? `${doc.source_quality.label}质量` : ""}`;
    updateContext({ title: title.slice(0, 24), readerHtml: markdownLight(doc.markdown || doc.error_message || "没有正文"), readerMeta: meta });
    showReader(activeContext());
    enableCopilot();
    const thinking = document.querySelector("#copilot-messages .chat-message.thinking");
    if (thinking) thinking.remove();
    if (run.initial_analysis) appendMessage("assistant", run.initial_analysis);
    $("#handoff-evidence").disabled = !run.artifact_id;
  } else if (status === "failed") {
    const thinking = document.querySelector("#copilot-messages .chat-message.thinking");
    if (thinking) thinking.remove();
    disableCopilot("阅读失败");
    appendMessage("assistant", run.error || "阅读失败，没有拿到正文。");
  } else if (status === "completed" && !docs.length) {
    const thinking = document.querySelector("#copilot-messages .chat-message.thinking");
    if (thinking) thinking.remove();
    disableCopilot("没有正文");
    appendMessage("assistant", "抓取完成但没有拿到正文（可能是动态页面）。可以点「原网页视图」直接浏览，或「新标签页打开」。");
  }
}

/* ── 伴读：快捷动作 / 划词问答 / 追问 ── */
const COPILOT_QUICK_PROMPTS = {
  bullets: "用 3-5 条要点总结当前网页的核心内容，每条一句话，只列要点。",
  risks: "指出当前网页内容里可能误导、过时或缺失的关键信息，逐条说明为什么，并给出核实建议。",
  translate: "把当前网页的核心内容翻译成简体中文，保留关键术语，翻译要自然通顺。",
  actions: "基于当前网页内容，给出 3 条可以立即执行的下一步建议，并说明每条的价值。",
};
async function copilotAsk(message) {
  const context = activeContext();
  if (!message.trim() || !context?.runId) return;
  const input = $("#chat-input"); const submit = $("#chat-submit");
  appendMessage("user", message);
  if (input) input.value = "";
  const thinking = appendThinking();
  setBusy(submit, true, "…");
  try {
    const body = await window.requestJson("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: context.runId, message }) });
    thinking.remove();
    appendMessage("assistant", body.answer || "没有返回内容");
  } catch (error) { thinking.remove(); appendMessage("assistant", `追问失败：${error.message}`); }
  finally { setBusy(submit, false); }
}
function grabSelection() {
  const selection = window.getSelection();
  const text = selection ? selection.toString().trim() : "";
  state.selectedText = text;
  const button = $("#selection-action");
  button.disabled = !text || !activeContext()?.runId;
  if (text) $("#browser-status").textContent = `已选中 ${text.length} 字，可点「问选中内容」`;
}
async function askSelection() {
  if (!state.selectedText || !activeContext()?.runId) return;
  await copilotAsk(`我选中了当前网页正文中的这段内容，请解释它说了什么、是否与全文一致，以及我还需要验证什么：\n\n"${state.selectedText.slice(0, 6000)}"`);
  state.selectedText = ""; $("#selection-action").disabled = true;
}

/* ── 批量研究（抽屉） ── */
function renderBatchRun(run) {
  state.run = run;
  const status = run.status || "queued";
  $("#cancel-research").hidden = !["queued", "running", "cancelling"].includes(status);
  $("#copy-evidence").disabled = !(run.documents || []).length;
  const host = $("#evidence-list");
  if (!(run.documents || []).length) {
    host.innerHTML = ["queued", "running"].includes(status) ? '<div class="empty-result"><span>…</span><strong>正在准备来源</strong><p>浏览器 Worker 返回后，来源卡片会出现在这里。</p></div>' : '<div class="empty-result"><span>01</span><strong>还没有打开网页</strong><p>抓取完成后，来源会按质量和正文长度显示在这里。</p></div>';
  } else {
    host.innerHTML = run.documents.map((doc, index) => { const quality = doc.source_quality || {}; const score = Number(quality.score); const low = Number.isFinite(score) && score < .55; const sourceUrl = safeHttpUrl(doc.url); const sourceMarkup = sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer" title="打开原始网页">${escapeHtml(doc.url || "—")}</a>` : `<span title="非 http/https 来源，已禁用跳转">${escapeHtml(doc.url || "—")}</span>`; return `<article class="evidence-item"><div class="evidence-item-head"><div><h4>${escapeHtml(doc.title || "未命名页面")}</h4>${sourceMarkup}</div><span class="quality-chip ${low ? "low" : ""}">${escapeHtml(quality.label || "未标注")}质量</span></div><div class="evidence-meta"><span>${escapeHtml(formatDate(doc.data_as_of || run.finished_at || run.created_at))}</span><span>${Number(doc.markdown_chars || 0).toLocaleString("zh-CN")} 字</span><span>${doc.success ? "抓取成功" : "抓取失败"}</span></div></article>`; }).join("");
  }
  const logHost = $("#activity-log"); $("#log-count").textContent = String(run.logs?.length || 0);
  logHost.innerHTML = run.logs?.length ? run.logs.map((log) => `<span>${escapeHtml(formatDate(log.at))} · ${escapeHtml(log.message || "")}</span>`).join("") : "<span>等待任务开始。</span>";
  const answer = run.initial_analysis;
  const answerHost = $("#answer-content");
  if (!answer) {
    answerHost.innerHTML = status === "completed" ? '<div class="empty-result compact"><strong>抓取完成，但没有首轮摘要</strong><p>可以直接在右侧伴读追问，或先查看来源。</p></div>' : '<div class="empty-result compact"><strong>回答会出现在这里</strong><p>先输入问题和网页地址；如果证据不足，助手会明确告诉你缺什么。</p></div>';
  } else {
    const meta = run.initial_result_contract?.source_coverage ? `\n\n引用覆盖：${run.initial_result_contract.source_coverage.with_locator || 0}/${run.initial_result_contract.source_coverage.total || 0} 可定位` : "";
    answerHost.innerHTML = `<p class="answer-text">${escapeHtml(answer + meta)}</p>`;
  }
  if (["completed", "failed", "cancelled"].includes(status) && state.polling) { clearTimeout(state.polling); state.polling = null; }
}
async function startBatchResearch(event) {
  event.preventDefault();
  const rawUrls = $("#research-urls").value.split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean);
  const urls = rawUrls.map(safeHttpUrl);
  const question = $("#research-question").value.trim();
  if (!urls.length || !question) return;
  if (urls.some((url) => !url)) { $("#command-status").textContent = "只支持不含账号密码的 http/https 地址"; return; }
  const button = $("#start-research");
  setBusy(button, true, "排队中…"); $("#command-status").textContent = "正在创建安全研究任务…";
  try {
    const body = await window.requestJson("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ urls, task: question, render_js: $("#render-js").checked, refresh: $("#refresh-source").checked, max_depth: 1, max_pages: Math.max(1, Math.min(20, Number($("#max-pages").value) || 5)) }) });
    $("#command-status").textContent = `任务已排队 · ${body.run_id.slice(0, 8)}`;
    state.run = null;
    await pollBatchRun(body.run_id);
  } catch (error) { $("#command-status").textContent = error.message; }
  finally { setBusy(button, false); }
}
async function pollBatchRun(runId) {
  try {
    const run = await window.requestJson(`/api/runs/${encodeURIComponent(runId)}`);
    renderBatchRun(run);
    if (["queued", "running", "cancelling"].includes(run.status)) setTimeout(() => { void pollBatchRun(runId); }, 1500);
  } catch (error) { $("#command-status").textContent = error.message; }
}
async function cancelBatchResearch() { if (!state.run?.id) return; const button = $("#cancel-research"); setBusy(button, true, "取消中…"); try { const body = await window.requestJson(`/api/runs/${encodeURIComponent(state.run.id)}/cancel`, { method: "POST" }); renderBatchRun(body.run || { ...state.run, status: "cancelling" }); } catch (error) { $("#command-status").textContent = error.message; } finally { setBusy(button, false); } }
async function copyEvidence() { const docs = state.run?.documents || []; const text = docs.map((doc, index) => `## 来源 ${index + 1}\n${doc.title || "未命名页面"}\n${doc.url || ""}\n\n${doc.markdown || ""}`).join("\n\n"); try { await navigator.clipboard.writeText(text); $("#command-status").textContent = "证据已复制"; } catch (_) { $("#command-status").textContent = "浏览器拒绝访问剪贴板，请选中正文复制"; } }

/* ── 交接 ── */
async function handoff() {
  const context = activeContext();
  const run = state.run;
  const artifactId = run?.artifact_id;
  if (!artifactId) { $("#handoff-status").textContent = "先完成一次 AI 阅读或批量研究再交接。"; return; }
  const target = $("#handoff-target").value;
  if (!window.confirm(`确认将 Artifact #${artifactId} 交给${target === "knowledge" ? "知识库" : target === "doc-factory" ? "文档工厂" : "想法分析"}？`)) return;
  const button = $("#handoff-evidence");
  setBusy(button, true, "交接中…");
  try {
    const body = await window.requestJson("/api/evidence/handoff", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ artifact_ids: [Number(artifactId)], target_project: target, title: `网页研究：${context?.title || "未命名研究"}`, instruction: "请基于网页研究 Artifact 继续处理，并保留来源、数据时间和不确定性。", confirmed: true }) });
    $("#handoff-status").textContent = `已创建事项 #${body.item?.id || "—"}，来源引用已保留。`;
  } catch (error) { $("#handoff-status").textContent = error.message; }
  finally { setBusy(button, false); }
}

/* ── 书签 / 带入 ── */
function configureBookmarklet() {
  const link = $("#research-bookmarklet"); if (!link) return;
  const target = `${window.location.origin}/projects/web-research`;
  const script = `(()=>{const s=(window.getSelection?window.getSelection().toString():"").slice(0,6000);const q=new URLSearchParams({source_url:location.href,source_title:document.title||""});location.href=${JSON.stringify(target)}+"?"+q.toString()+(s?"#source_selection="+encodeURIComponent(s):"")})()`;
  link.href = `javascript:${script}`;
  const copy = $("#copy-bookmarklet");
  if (copy) copy.addEventListener("click", async () => { try { await navigator.clipboard.writeText(link.href); $("#bookmarklet-status").textContent = "已复制，可粘贴到浏览器书签地址栏"; } catch (_) { $("#bookmarklet-status").textContent = "浏览器拒绝剪贴板，请将按钮拖到书签栏"; } });
}
function applyIncomingContext() {
  const params = new URLSearchParams(window.location.search);
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const url = safeHttpUrl(params.get("source_url"));
  if (!url) return;
  const selection = String(fragment.get("source_selection") || params.get("source_selection") || "").trim().slice(0, 12000);
  const title = String(params.get("source_title") || "").trim().slice(0, 300);
  const existing = state.contexts.find((item) => item.sourceUrl === url && item.sourceContext === selection);
  if (existing) { state.activeId = existing.id; return; }
  const context = newContext({ title: title ? `研究：${title}`.slice(0, 24) : `研究：${hostOf(url)}`, url, sourceUrl: url, sourceContext: selection });
  state.activeId = context.id; renderTabs();
  $("#reader-view").hidden = false; $("#iframe-host").hidden = true;
  $("#reader-meta").textContent = "";
  $("#reader-body").innerHTML = '<div class="reader-empty">正在阅读…正文抓取完成后显示在这里。</div>';
  enableCopilot();
  $("#browser-status").textContent = selection ? "已带入当前网页和选中文字 · 正在自动阅读…" : "已带入当前网页 · 正在自动阅读…";
  $("#address-input").value = url;
  void aiRead();
}

/* ── 主题 ── */
function setupTheme() {
  const saved = localStorage.getItem("workbench-theme") || "dark";
  document.documentElement.dataset.theme = saved;
  const button = $("#theme-toggle");
  if (!button) return;
  const render = () => { const dark = document.documentElement.dataset.theme === "dark"; button.textContent = dark ? "☼" : "☾"; button.title = dark ? "切换浅色主题" : "切换深色主题"; };
  button.addEventListener("click", () => { const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; localStorage.setItem("workbench-theme", next); render(); });
  render();
}

function init() {
  setupTheme();
  loadContexts(); applyIncomingContext();
  if (!state.contexts.length) { const context = newContext(); state.activeId = context.id; }
  renderTabs(); selectContext(state.activeId);

  $("#new-context").addEventListener("click", () => { const context = newContext(); renderTabs(); selectContext(context.id); $("#address-input").focus(); });
  $("#address-form").addEventListener("submit", (event) => { event.preventDefault(); openAddress($("#address-input").value); });
  $("#toggle-view").addEventListener("click", () => setView(viewMode === "reader" ? "shot" : viewMode === "shot" ? "iframe" : "reader"));
  $("#shot-view").addEventListener("click", () => setView("shot"));
  document.querySelector("#copilot-quick").addEventListener("click", (event) => {
    const button = event.target.closest("[data-copilot-quick]");
    if (!button) return;
    const prompt = COPILOT_QUICK_PROMPTS[button.dataset.copilotQuick];
    if (prompt) void copilotAsk(prompt);
  });
  $("#selection-action").addEventListener("click", () => void askSelection());
  $("#chat-form").addEventListener("submit", (event) => { event.preventDefault(); void copilotAsk($("#chat-input").value); });
  $("#handoff-evidence").addEventListener("click", () => void handoff());

  const readerBody = $("#reader-body");
  if (readerBody) readerBody.addEventListener("mouseup", grabSelection);

  $("#research-form").addEventListener("submit", startBatchResearch);
  $("#cancel-research").addEventListener("click", cancelBatchResearch);
  $("#copy-evidence").addEventListener("click", copyEvidence);
  $("#sample-query").addEventListener("click", () => { $("#research-question").value = "比较这些页面的核心观点、关键事实和信息缺口，并给出需要继续验证的 3 个问题。"; $("#research-urls").value = "https://www.tabbit.com/\nhttps://www.doubao.com/browser-extension/landing"; });
  configureBookmarklet();
}
init();
