const $ = (selector) => document.querySelector(selector);
const workbenchRequestJson = window.WorkbenchUX?.requestJson || (async (url, options = {}) => {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求未完成（${response.status}）`);
  return body;
});
function setupThemeToggle() {
  if (!document.querySelector("link[data-workbench-theme]")) { const link = document.createElement("link"); link.rel = "stylesheet"; link.href = "/static/theme.css?v=0.3.187"; link.dataset.workbenchTheme = "true"; document.head.append(link); }
  const topbar = document.querySelector(".topbar-right, .top-actions");
  if (!topbar || topbar.querySelector("[data-theme-toggle]")) return;
  const theme = window.WorkbenchTheme;
  if (!theme) document.documentElement.dataset.theme = localStorage.getItem("workbench-theme") === "dark" ? "dark" : "light";
  const button = document.createElement("button");
  button.type = "button"; button.className = "theme-toggle";
  topbar.append(button);
  if (theme) theme.bindToggle(button, { text: true });
  else {
    const render = () => { const dark = document.documentElement.dataset.theme === "dark"; button.textContent = dark ? "浅色" : "深色"; button.setAttribute("aria-label", dark ? "切换到浅色模式" : "切换到深色模式"); button.setAttribute("aria-pressed", String(dark)); };
    button.addEventListener("click", () => { const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; localStorage.setItem("workbench-theme", next); render(); }); render();
  }
}
setupThemeToggle();
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations()
    .then((registrations) => Promise.all(registrations.filter((registration) => registration.scope === `${location.origin}/static/`).map((registration) => registration.unregister())))
    .then(() => navigator.serviceWorker.register("/static/sw.js?v=0.3.187", { scope: "/" }))
    .catch(() => {});
}
const icons = {
  inbox: '<svg viewBox="0 0 24 24" fill="none"><path d="M5 7.5h14v10H5z" stroke="currentColor" stroke-width="1.5"/><path d="M8 11h8M8 14h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  book: '<svg viewBox="0 0 24 24" fill="none"><path d="M6 5.5h9a3 3 0 0 1 3 3v10H9a3 3 0 0 1-3-3v-10Z" stroke="currentColor" stroke-width="1.5"/><path d="M9 5.5v13M10.5 9h5M10.5 12h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  document: '<svg viewBox="0 0 24 24" fill="none"><path d="M7 4.5h7l3 3v12H7z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M14 4.5v4h3M10 13h4M10 16h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  crawler: '<svg viewBox="0 0 24 24" fill="none"><path d="M6 9.5 12 6l6 3.5v6L12 19l-6-3.5v-6Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="m6.5 10 5.5 3 5.5-3M12 13v6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="18.3" cy="5.3" r="1.7" fill="currentColor"/></svg>',
  chart: '<svg viewBox="0 0 24 24" fill="none"><path d="M5 19V5M5 19h14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="m8 15 3-4 2.5 2 4-5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="none"><path d="m12 3 1.8 6.2L20 11l-6.2 1.8L12 19l-1.8-6.2L4 11l6.2-1.8L12 3Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7L19 16Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  wallet: '<svg viewBox="0 0 24 24" fill="none"><path d="M4.5 7.5h14v11h-14zM4.5 7.5V5.8a1.3 1.3 0 0 1 1.3-1.3h10.7a2 2 0 0 1 2 2v1" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M14.5 12h4M16.5 12v2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  server: '<svg viewBox="0 0 24 24" fill="none"><rect x="4.5" y="5" width="15" height="5.5" rx="1.3" stroke="currentColor" stroke-width="1.5"/><rect x="4.5" y="13.5" width="15" height="5.5" rx="1.3" stroke="currentColor" stroke-width="1.5"/><path d="M7.5 7.7h.01M7.5 16.2h.01M10.5 7.7h6M10.5 16.2h6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  robot: '<svg viewBox="0 0 24 24" fill="none"><rect x="5" y="8" width="14" height="9" rx="2" stroke="currentColor" stroke-width="1.5"/><circle cx="9.2" cy="12.3" r="1.1" fill="currentColor"/><circle cx="14.8" cy="12.3" r="1.1" fill="currentColor"/><path d="M12 8V5M9 5h6M12 17v2.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
};
function escapeHtml(value) { return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;"); }
function escapeAny(value) { if (value == null) return ""; if (typeof value !== "object") return escapeHtml(value); const text = value.label || value.text || value.content || value.title || value.description || value.value || ""; return text ? escapeHtml(text) : escapeHtml(JSON.stringify(value)); }
function setNotificationFeedback(message = "", tone = "") { const feedback = $("#notification-feedback"); if (!feedback) return; feedback.textContent = message; feedback.className = `notification-feedback${tone ? ` ${tone}` : ""}`; }
let projects = []; let hiddenProjectIds = new Set(); let activeGroup = "all"; let collaborationLoaded = false; let draggedProjectId = ""; let preferenceSaveSeq = 0;
function projectAgentHref(project) { const base = project.href || "/"; return `${base}${base.includes("?") ? "&" : "?"}focus=agent`; }
function projectActivityMarkup(project) {
  const activity = project.activity || {};
  if (!activity.signal) return "";
  const latest = activity.latest_run || {};
  const detail = latest.title ? `最近：${latest.title}` : "点击进入负责项目继续处理";
  return `<a class="project-activity ${escapeHtml(activity.tone || "online")}" target="_blank" rel="noopener" href="${escapeHtml(projectAgentHref(project))}" title="${escapeHtml(detail)}"><i aria-hidden="true"></i><span>${escapeHtml(activity.label || "有待处理事项")}</span><small>${escapeHtml(detail)}</small><b aria-hidden="true">↗</b></a>`;
}
function projectCard(project) {
  const summary = project.summary || {};
  const action = project.primary_action || { label: "打开项目", href: project.href };
  const state = project.state || { tone: project.status === "ready" ? "online" : "offline", label: project.status || "unknown" };
  const stateLabel = state.label || "项目状态";
  const agentStatus = project.agent_status || "planned";
  const agentStatusLabel = project.agent_status_label || "规划中";
  const favoriteLabel = project.favorite ? "移出常用项目" : "加入常用项目";
  const health = project.health || {};
  const healthTone = health.tone === "danger" ? "danger" : "good";
  const healthLabel = health.label || project.freshness?.label || "状态正常";
  const healthDetail = health.detail || project.freshness?.source || "暂无数据来源";
  const healthFacts = [health.open_work_items ? `${health.open_work_items} 项待处理` : "", health.blocked_work_items ? `${health.blocked_work_items} 项待确认` : "", health.failed_work_items ? `${health.failed_work_items} 项失败` : "", health.active_runs ? `${health.active_runs} 个运行中` : ""].filter(Boolean).join(" · ");
  const healthMeta = [healthFacts, health.source || project.freshness?.source || "", health.data_as_of || project.freshness?.checked_at ? `数据 ${formatNotificationTime(health.data_as_of || project.freshness?.checked_at)}` : ""].filter(Boolean).join(" · ");
  const hideLabel = `隐藏${project.title || "项目"}`;
  return `<article class="project-card ${escapeHtml(project.accent || "blue")}" data-project-id="${escapeHtml(project.id || "")}" draggable="true"><div class="project-card-head"><span class="project-icon">${icons[project.icon] || icons.chart}</span><span class="project-title-wrap"><span class="project-top"><h3>${escapeHtml(project.title)}</h3><span class="project-agent-badge status-${escapeHtml(agentStatus)}" title="${escapeHtml(agentStatusLabel)}">${escapeHtml(agentStatusLabel)}</span></span><span class="project-meta" title="${escapeHtml(project.meta || "")}">${escapeHtml(project.meta || "")}</span></span><span class="project-card-tools"><button class="project-favorite ${project.favorite ? "active" : ""}" data-project-favorite="${escapeHtml(project.id || "")}" type="button" aria-pressed="${project.favorite ? "true" : "false"}" title="${favoriteLabel}" aria-label="${favoriteLabel}">★</button><button class="project-hide" data-project-hide="${escapeHtml(project.id || "")}" type="button" title="${hideLabel}" aria-label="${hideLabel}">×</button><button class="project-drag-handle" type="button" draggable="true" title="拖动排序" aria-label="拖动排序">⋮⋮</button><span class="project-state ${escapeHtml(state.tone || "offline")}" data-label="${escapeHtml(stateLabel)}" title="${escapeHtml(stateLabel)}" role="img" aria-label="${escapeHtml(stateLabel)}" tabindex="0"><i aria-hidden="true"></i></span></span></div><p class="project-description">${escapeHtml(project.description)}</p><div class="project-summary"><strong>${escapeHtml(summary.value ?? "—")}</strong><span>${escapeHtml(summary.label || "")}</span><small>${escapeHtml(summary.detail || "")}</small></div><div class="project-health ${healthTone}" title="${escapeHtml([healthDetail, healthMeta].filter(Boolean).join(" · "))}"><i aria-hidden="true"></i><span><b>${escapeHtml(healthLabel)}</b><small>${escapeHtml(healthDetail)}${healthMeta ? ` · ${escapeHtml(healthMeta)}` : ""}</small></span></div>${projectActivityMarkup(project)}<div class="project-actions"><a class="project-primary" target="_blank" rel="noopener" href="${escapeHtml(action.href)}">${escapeHtml(action.label)} <span>↗</span></a></div></article>`;
}
function openAddProjectModal() {
  const modal = $("#add-project-modal");
  if (!modal) { window.location.hash = "projects"; return; }
  modal.classList.remove("hidden");
  const message = $("#add-project-message");
  if (message) message.textContent = "";
  const close = () => { modal.classList.add("hidden"); };
  modal.querySelectorAll("[data-close-add-project]").forEach((btn) => btn.addEventListener("click", close));
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
  const form = $("#add-project-form");
  if (form && !form.dataset.bound) {
    form.dataset.bound = "true";
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const submit = form.querySelector("button[type=submit]");
      submit.disabled = true;
      if (message) message.textContent = "保存中…";
      try {
        const body = await workbenchRequestJson("/api/projects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
          id: $("#add-project-id").value.trim(),
          title: $("#add-project-title").value.trim(),
          description: $("#add-project-desc").value.trim(),
          group: $("#add-project-group").value,
        }) });
        if (message) { message.textContent = "已添加入口。"; message.dataset.tone = "success"; }
        $("#add-project-id").value = ""; $("#add-project-title").value = ""; $("#add-project-desc").value = "";
        projects = body.projects || projects;
        renderProjects();
        window.setTimeout(close, 600);
      } catch (error) {
        if (message) { message.textContent = error.message || "添加失败"; message.dataset.tone = "error"; }
      } finally {
        submit.disabled = false;
      }
    });
  }
  window.setTimeout(() => $("#add-project-id")?.focus(), 0);
}

function renderProjects() { const query = $("#project-search").value.trim().toLowerCase(); const matchesGroup = (project) => activeGroup === "all" || (activeGroup === "favorite" ? project.favorite === true : project.group === activeGroup); const visible = projects.filter((project) => matchesGroup(project) && (!query || `${project.title} ${project.description} ${project.meta}`.toLowerCase().includes(query))); $("#project-count").textContent = visible.length; $("#project-note").textContent = `${visible.length} 个入口 · ${activeGroup === "favorite" ? "常用视图" : "紧凑视图"}`; $("#project-grid").innerHTML = visible.length ? visible.map(projectCard).join("") : '<div class="empty-projects">没有匹配的项目。换个关键词，或在其他分组里看看。</div>'; ["favorite", "all", "organize", "produce", "discover", "monitor"].forEach((group) => { const target = $(`#${group}-count`); if (target) target.textContent = group === "all" ? projects.length : group === "favorite" ? projects.filter((item) => item.favorite === true).length : projects.filter((item) => item.group === group).length; }); }
function projectPreferencesPayload() { return { order: projects.map((project) => project.id), favorite_ids: projects.filter((project) => project.favorite).map((project) => project.id), groups: Object.fromEntries(projects.filter((project) => project.group).map((project) => [project.id, project.group])), hidden_ids: [...hiddenProjectIds] }; }
async function saveProjectPreferences() {
  const seq = ++preferenceSaveSeq;
  await new Promise((resolve) => { window.setTimeout(resolve, 160); });
  if (seq !== preferenceSaveSeq) return;
  await workbenchRequestJson("/api/projects/preferences", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(projectPreferencesPayload()) });
  if (seq === preferenceSaveSeq) $("#project-note").textContent = "排序与常用设置已保存";
}
function clearProjectDragState() { draggedProjectId = ""; document.querySelectorAll(".project-card.is-dragging, .project-card.drop-before, .project-card.drop-after, .project-card.drop-hidden").forEach((card) => card.classList.remove("is-dragging", "drop-before", "drop-after", "drop-hidden")); }
function moveProjectBefore(sourceId, targetId, before) {
  const sourceIndex = projects.findIndex((project) => project.id === sourceId);
  const targetIndex = projects.findIndex((project) => project.id === targetId);
  if (sourceIndex < 0 || targetIndex < 0 || sourceId === targetId) return false;
  const [moved] = projects.splice(sourceIndex, 1);
  const adjustedTargetIndex = projects.findIndex((project) => project.id === targetId);
  projects.splice(adjustedTargetIndex + (before ? 0 : 1), 0, moved);
  return true;
}
function setupProjectInteractions() {
  const grid = $("#project-grid");
  if (!grid) return;
  document.addEventListener("keydown", (event) => {
    if (!((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") && event.key !== "/") return;
    const target = event.target;
    const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
    if (typing) return;
    const search = $("#project-search");
    if (!search) return;
    event.preventDefault();
    search.focus();
    search.select();
  });
  grid.addEventListener("click", (event) => {
    const favorite = event.target.closest("[data-project-favorite]");
    if (!favorite) return;
    event.preventDefault();
    event.stopPropagation();
    const project = projects.find((item) => item.id === favorite.dataset.projectFavorite);
    if (!project) return;
    project.favorite = !project.favorite;
    renderProjects();
    saveProjectPreferences().catch((error) => { const note = $("#project-note"); note.textContent = error.message; note.classList.add("error"); setTimeout(() => note.classList.remove("error"), 4000); });
  });
  grid.addEventListener("click", (event) => {
    const hide = event.target.closest("[data-project-hide]");
    if (!hide) return;
    event.preventDefault();
    event.stopPropagation();
    const project = projects.find((item) => item.id === hide.dataset.projectHide);
    if (!project) return;
    hiddenProjectIds.add(project.id);
    projects = projects.filter((item) => item.id !== project.id);
    renderProjects();
    saveProjectPreferences().catch((error) => { const note = $("#project-note"); note.textContent = error.message; note.classList.add("error"); setTimeout(() => note.classList.remove("error"), 4000); });
  });
  grid.addEventListener("click", (event) => {
    if (event.target.closest("a, button, input, select, details, .project-card-tools")) return;
    const card = event.target.closest(".project-card");
    const project = projects.find((item) => item.id === card?.dataset.projectId);
    if (!project) return;
    window.open(project.primary_action?.href || project.href || "/", "_blank", "noopener");
  });
  grid.addEventListener("keydown", (event) => {
    const handle = event.target.closest(".project-drag-handle");
    if (!handle || !["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    const card = handle.closest(".project-card");
    const projectIndex = projects.findIndex((project) => project.id === card?.dataset.projectId);
    const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? projects.length - 1 : projectIndex + (event.key === "ArrowUp" ? -1 : 1);
    if (projectIndex < 0 || nextIndex < 0 || nextIndex >= projects.length) return;
    event.preventDefault();
    const [project] = projects.splice(projectIndex, 1);
    projects.splice(nextIndex, 0, project);
    renderProjects();
    saveProjectPreferences().catch((error) => { const note = $("#project-note"); note.textContent = error.message; note.classList.add("error"); setTimeout(() => note.classList.remove("error"), 4000); });
    window.setTimeout(() => $( `.project-card[data-project-id="${CSS.escape(project.id)}"] .project-drag-handle` )?.focus(), 0);
  });
  grid.addEventListener("dragstart", (event) => {
    const card = event.target.closest(".project-card");
    if (!card) { event.preventDefault(); return; }
    const interactive = event.target.closest("a, button, input, .project-actions");
    if (interactive && !event.target.closest(".project-drag-handle")) { event.preventDefault(); return; }
    draggedProjectId = card.dataset.projectId || "";
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", draggedProjectId);
    card.classList.add("is-dragging");
  });
  grid.addEventListener("dragover", (event) => {
    if (!draggedProjectId) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const cards = [...grid.querySelectorAll(".project-card")];
    const dragging = grid.querySelector(".project-card.is-dragging");
    if (dragging) dragging.classList.add("drop-hidden");
    cards.forEach((item) => item.classList.remove("drop-before", "drop-after"));
    const card = event.target.closest(".project-card");
    let targetCard = card;
    let before = true;
    if (!targetCard) {
      const last = cards.filter((item) => item.dataset.projectId !== draggedProjectId).pop();
      if (!last) return;
      const lastRect = last.getBoundingClientRect();
      targetCard = last;
      before = event.clientY < lastRect.top + lastRect.height / 2;
      if (!before) {
        targetCard = last;
        before = false;
      }
    } else {
      if (targetCard.dataset.projectId === draggedProjectId) return;
      const rect = targetCard.getBoundingClientRect();
      before = event.clientY < rect.top + rect.height / 2;
    }
    if (targetCard) targetCard.classList.add(before ? "drop-before" : "drop-after");
    const edge = 72;
    const viewport = window.innerHeight;
    if (event.clientY < edge) window.scrollBy(0, -12);
    else if (event.clientY > viewport - edge) window.scrollBy(0, 12);
  });
  grid.addEventListener("drop", (event) => {
    const card = event.target.closest(".project-card");
    if (!draggedProjectId) return;
    event.preventDefault();
    let targetCard = card;
    let before = true;
    if (!targetCard) {
      const last = [...grid.querySelectorAll(".project-card")].filter((item) => item.dataset.projectId !== draggedProjectId).pop();
      if (last) { targetCard = last; before = false; }
    } else {
      if (targetCard.dataset.projectId === draggedProjectId) { clearProjectDragState(); return; }
      const rect = targetCard.getBoundingClientRect();
      before = event.clientY < rect.top + rect.height / 2;
    }
    if (targetCard && moveProjectBefore(draggedProjectId, targetCard.dataset.projectId, before)) {
      renderProjects();
      saveProjectPreferences().catch((error) => { const note = $("#project-note"); note.textContent = error.message; note.classList.add("error"); setTimeout(() => note.classList.remove("error"), 4000); });
    }
    clearProjectDragState();
  });
  grid.addEventListener("dragend", clearProjectDragState);
}
async function loadSummary() { try { const [inbox, knowledge] = await Promise.all([workbenchRequestJson("/api/inbox?status=inbox"), workbenchRequestJson("/api/knowledge")]); $("#inbox-count").textContent = inbox.items?.length ?? 0; $("#note-count").textContent = knowledge.notes?.length ?? 0; } catch { $("#inbox-count").textContent = "—"; $("#note-count").textContent = "—"; } }
function workItemHref(item){
  // 工具试用类：跳到 GitHub 工具目录对应工具，不要落回首页待办（会造成死循环）。
  const kind = String(item.kind || "");
  if (kind === "github_tool_trial") {
    const toolId = ((item.metadata || {}).tool || {}).id || "";
    return toolId ? `/github-tools?tool=${encodeURIComponent(toolId)}` : "/github-tools";
  }
  // alert 类工作项指向产生告警的项目（source_project，如 server/sub2api），
  // 让用户去真实数据源处理；普通交接类指向目标项目。
  const isAlert = kind === "alert";
  const raw = isAlert
    ? (item.source_project || item.target_project || "inbox")
    : (item.target_project || item.source_project || "inbox");
  const target = raw.split(",")[0];
  const project=projects.find((entry)=>entry.id===target);
  const base=project?.href || (target==="workbench"?"/":target==="crawl4ai"?"/crawl4ai":"/projects/"+target);
  const anchors={inbox:"#inbox-list", knowledge:"#knowledge-inbox-title", "doc-factory":"#factory-form"};
  return anchors[target] ? `${base}${anchors[target]}` : target === "workbench" ? "/#activity" : `${base}${base.includes("?") ? "&" : "?"}focus=agent`;
}
let pendingWorkItems = [];
let allWorkItems = [];
let workItemView = "active"; // "active" | "ignored"
function isWorkItemIgnored(item){ return Boolean((item.metadata||{}).ignored_at); }
function workItemSourceMarkup(item){
  const source = item.source_context;
  if (!source) return "";
  const updated = source.source_updated_at ? ` · 更新 ${escapeHtml(formatNotificationTime(source.source_updated_at))}` : "";
  const href = source.source_url && /^https?:\/\//i.test(source.source_url)
    ? `<a class="work-item-source-link" href="${escapeHtml(source.source_url)}" target="_blank" rel="noopener noreferrer">原始来源 ↗</a>`
    : "";
  return `<span class="work-item-source"><span>${escapeHtml(source.kind_label)} · ${escapeHtml(source.source_label)}${updated}</span>${href}<small>下一步：${escapeHtml(source.next_step || "在目标 Agent 中继续处理")}</small></span>`;
}
function workItemQualityMarkup(item){
  const quality = item.next_step_quality || {};
  if (!quality.label) return "";
  const tone = quality.status === "ready" ? "ready" : quality.status === "review" ? "review" : "missing";
  return `<span class="work-item-quality ${tone}" title="${escapeHtml(quality.next_action || "")}"><i aria-hidden="true"></i>${escapeHtml(quality.label)}</span>`;
}
function workItemCard(item, {ignoredView = false} = {}){
  const target=(item.target_project||item.source_project||"workbench").split(",")[0];
  const sourceLabel=item.source_agent_name||item.source_project||"工作台总调度 Agent";
  const targetLabel=item.target_agent_label||item.target_agent_names?.join("、")||target;
  const statusNames={open:"待处理",running:"处理中",blocked:"待确认",failed:"执行失败"};
  const kindNames={task:"待办",handoff:"项目交接",agent_dispatch:"Agent 调度",alert:"告警",research:"研究",research_observation:"行情观察"};
  const action = ignoredView
    ? `<button class="work-item-restore" data-restore="${escapeHtml(item.id)}" type="button" title="恢复为待处理">恢复</button>`
    : `<button class="work-item-ignore" data-ignore="${escapeHtml(item.id)}" type="button" title="忽略这项并打开下一条待办">忽略并下一条</button>`;
  // 人话流转：来自哪、要交给谁；同项目内任务则直接说是什么事。
  const routeText = sourceLabel === targetLabel
    ? `${escapeHtml(targetLabel)} 里的${escapeHtml(kindNames[item.kind]||item.kind||"事项")}`
    : `来自 ${escapeHtml(sourceLabel)}，转给 ${escapeHtml(targetLabel)}`;
  return `<article class="work-item status-${escapeHtml(item.status||"open")}${ignoredView ? " ignored" : ""}"><a class="work-item-link" target="_blank" rel="noopener" href="${escapeHtml(workItemHref(item))}"><div class="work-item-top"><span class="work-item-kind">${escapeHtml(kindNames[item.kind]||item.kind||"事项")}</span><span class="work-item-priority">${escapeHtml(statusNames[item.status]||item.status||"待处理")}</span></div><h3>${escapeHtml(item.title||"未命名工作项")}</h3><p>${escapeHtml(item.description||"没有描述")}</p></a><div class="work-item-foot"><div class="work-item-foot-info"><span class="work-item-meta">${routeText}</span>${workItemQualityMarkup(item)}${workItemSourceMarkup(item)}</div>${action}</div></article>`;
}
function renderWorkItems(items){
  allWorkItems = items || [];
  const active = allWorkItems.filter((item)=>["open","running","blocked","failed"].includes(item.status) && !isWorkItemIgnored(item));
  const ignored = allWorkItems.filter((item)=>["open","running","blocked","failed"].includes(item.status) && isWorkItemIgnored(item));
  pendingWorkItems = active;
  const statusNames={open:"待处理",running:"处理中",blocked:"待确认",failed:"执行失败"};
  const section=$("#activity");
  const listEl = $("#work-item-list");
  if(section) section.hidden = false;
  const toggle = $("#toggle-ignored");
  if(toggle) toggle.hidden = !ignored.length;
  if(toggle && ignored.length) toggle.textContent = workItemView === "ignored" ? `待办 ${active.length} 项` : `已忽略 ${ignored.length} 项`;
  if(workItemView === "ignored"){
    $("#activity-note").textContent = ignored.length ? `已忽略 ${ignored.length} 项 · 点击恢复后回到待办` : "没有已忽略的待办";
    updateOneClickButton();
    if(listEl) listEl.innerHTML = ignored.length ? ignored.slice(0,12).map((item)=>workItemCard(item,{ignoredView:true})).join("") : '<div class="work-item-empty">没有已忽略的待办。</div>';
    return;
  }
  $("#activity-note").textContent=active.length?active.length+" 个需要关注":"暂无待处理事项";
  updateOneClickButton();
  if(listEl) listEl.innerHTML = active.length ? active.slice(0,6).map((item)=>workItemCard(item)).join("") : '<div class="work-item-empty">没有待处理的事项。有新事情进来，或告诉 Agent 帮你安排，就会出现在这里。</div>';
  const _ = statusNames;
}
function updateOneClickButton(){
  const button=$("#one-click-process");
  if(!button) return;
  const remaining=pendingWorkItems.length;
  if(!remaining || workItemView === "ignored"){ button.hidden=true; return; }
  button.hidden=false;
  button.textContent = `一键处理 · 剩余 ${remaining}`;
  button.title = `每次打开一条待办；回到工作台后继续下一条（共 ${remaining} 项）`;
}
function processNextWorkItem(){
  const next=pendingWorkItems[0];
  if(!next) return;
  pendingWorkItems.shift();
  updateOneClickButton();
  const href=workItemHref(next);
  window.open(href,"_blank","noopener");
  const note=$("#activity-note");
  if(note) note.textContent=`已打开：「${String(next.title||"").slice(0,20)}」 · 完成或忽略后回到工作台继续下一条 · 剩余 ${pendingWorkItems.length} 项`;
}
async function ignoreWorkItem(id){
  const item = allWorkItems.find((entry)=>String(entry.id)===String(id)) || null;
  const metadata = {...((item?.metadata)||{}), ignored_at: new Date().toISOString()};
  const body = await workbenchRequestJson(`/api/work-items/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ metadata }) });
  if(!body.item) throw new Error("忽略失败");
  await loadWorkItems();
  if(workItemView === "active" && pendingWorkItems.length){
    processNextWorkItem();
  } else if(!pendingWorkItems.length){
    const note = $("#activity-note");
    if(note) note.textContent = "已忽略；当前没有下一条待办";
  }
}
async function restoreWorkItem(id){
  const item = allWorkItems.find((entry)=>String(entry.id)===String(id)) || null;
  const metadata = {...((item?.metadata)||{})};
  delete metadata.ignored_at;
  const body = await workbenchRequestJson(`/api/work-items/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ metadata }) });
  if(!body.item) throw new Error("恢复失败");
  workItemView = "active"; // 恢复后回到待办视图，避免停在空的已忽略视图
  await loadWorkItems();
}
async function loadWorkItems(){ try { const body=await workbenchRequestJson("/api/work-items?status=all"); renderWorkItems(body.items||[]); } catch(error){ $("#activity-note").textContent="读取失败"; $("#work-item-list").innerHTML='<div class="work-item-empty">联动任务读取失败：'+escapeHtml(error.message)+'</div>'; } }
function renderTraceItems(items = [], containerSelector = "#trace-list") {
  const list = $(containerSelector); if (!list) return;
  const labels = { artifact: "产物", work_item: "待办", run: "Agent" };
  const projectNames = Object.fromEntries((projects || []).map((p) => [p.id, p.title || p.id]));
  if (!items.length) { list.innerHTML = '<div class="trace-empty">还没有活动记录。完成一次 Agent 任务、保存一份产物后，这里会按时间汇总。</div>'; return; }
  // 去重：相邻的同类同标题记录合并显示（例如同一任务多次运行）
  const deduped = [];
  for (const item of items) {
    const key = `${item.type}|${item.title}`;
    const last = deduped[deduped.length - 1];
    if (last && last.__key === key) { last.__count = (last.__count || 1) + 1; continue; }
    const copy = { ...item }; copy.__key = key; copy.__count = 1; deduped.push(copy);
  }
  list.innerHTML = deduped.map((item) => {
    const count = item.__count > 1 ? ` <b class="trace-count">×${item.__count}</b>` : "";
    const projectName = item.project_label || projectNames[item.project_id] || item.project_id || "工作台";
    return `<article class="trace-row trace-${escapeHtml(item.type || "record")}"><div class="trace-row-main"><div><span class="trace-kind">${escapeHtml(labels[item.type] || "动态")}</span><strong>${escapeHtml(item.title || "未命名记录")}${count}</strong></div><small>${escapeHtml(projectName)} · ${escapeHtml(item.status || "")} · ${escapeHtml(formatNotificationTime(item.updated_at))}</small>${item.detail ? `<p>${escapeHtml(item.detail)}</p>` : ""}</div>${item.href ? `<a class="trace-open" href="${escapeHtml(item.href)}" target="_blank" rel="noopener">${item.type === "run" ? "查看详情 ↗" : "打开 ↗"}</a>` : ""}</article>`;
  }).join("");
}
let traceLoaded = false;
async function loadTraceCenter() {
  const count = $("#trace-modal-count");
  try {
    const body = await workbenchRequestJson("/api/trace/recent?limit=40");
    renderTraceItems(body.items || [], "#trace-modal-list");
    if (count) count.textContent = `${(body.items || []).length} 条最近记录`;
  } catch (error) { if (count) count.textContent = "读取失败"; $("#trace-modal-list").innerHTML = `<div class="trace-empty">${escapeHtml(error.message)} · 点击刷新重试</div>`; }
}
function setupTraceCenter() {
  const modal = $("#trace-modal");
  const openButton = $("#trace-open");
  const closeButtons = document.querySelectorAll("[data-close-trace]");
  if (!modal || !openButton) return;
  openButton.addEventListener("click", async () => {
    modal.classList.remove("hidden");
    if (!traceLoaded) { traceLoaded = true; await loadTraceCenter(); }
  });
  closeButtons.forEach((button) => button.addEventListener("click", () => modal.classList.add("hidden")));
  modal.addEventListener("click", (event) => { if (event.target === modal) modal.classList.add("hidden"); });
  $("#refresh-trace-modal")?.addEventListener("click", (event) => { const button = event.currentTarget; button.disabled = true; button.textContent = "刷新中…"; loadTraceCenter().finally(() => { button.disabled = false; button.textContent = "刷新"; }); });
}
function setupWorkItems(){
  $("#refresh-work-items")?.addEventListener("click", (event) => { const button = event.currentTarget; button.disabled = true; button.textContent = "刷新中…"; loadWorkItems().finally(() => { button.disabled = false; button.textContent = "刷新"; }); });
  $("#one-click-process")?.addEventListener("click", processNextWorkItem);
  $("#toggle-ignored")?.addEventListener("click", () => { workItemView = workItemView === "ignored" ? "active" : "ignored"; void loadWorkItems(); });
  $("#work-item-list")?.addEventListener("click", async (event) => {
    const ignore = event.target.closest("[data-ignore]");
    const restore = event.target.closest("[data-restore]");
    if(ignore){ event.preventDefault(); ignore.disabled = true; try { await ignoreWorkItem(ignore.dataset.ignore); } catch(e){ $("#activity-note").textContent = e.message; } }
    else if(restore){ event.preventDefault(); restore.disabled = true; try { await restoreWorkItem(restore.dataset.restore); } catch(e){ $("#activity-note").textContent = e.message; } }
  });
}
function renderProjectLinks(links = []) { const list = $("#project-link-list"); const note = $("#project-links-note"); if (!list) return; if (note) note.textContent = links.length ? String(links.length) : "—"; if (!links.length) { list.innerHTML = '<div class="project-link-empty">项目之间还没有协作记录。让某个项目的 Agent 完成一次分析后，就可以转交给其他项目了。</div>'; return; } const statusNames = { verified: "真实链路已验证", synthetic: "内部测试通过", legacy: "历史记录", partial: "证据不完整", configured: "已配置待验证" }; list.innerHTML = links.map((link) => { const evidence = link.evidence || {}; const auditEvidence = link.evidence_summary || {}; const score = Number(link.score || 0); const status = link.status || "configured"; const businessText = auditEvidence.business_status === "verified" ? "真实链路已验证" : auditEvidence.business_status === "synthetic_only" ? `内部测试 ${auditEvidence.synthetic_verified || 0} 条 · 真实链路 0 条` : auditEvidence.business_status === "legacy_unclassified" ? `历史记录 ${auditEvidence.legacy_unclassified_verified || 0} 条 · 真实证据待补` : "真实证据待补"; return `<article class="project-link-card link-${escapeHtml(status)}"><div class="project-link-route"><a href="${escapeHtml(link.from_href || "/")}" target="_blank" rel="noopener" title="打开${escapeHtml(link.from_name || link.from)}">${escapeHtml(link.from_name || link.from)}</a><span aria-hidden="true">→</span><a href="${escapeHtml(link.to_href || "/")}" target="_blank" rel="noopener" title="打开${escapeHtml(link.to_name || link.to)}">${escapeHtml(link.to_name || link.to)}</a></div><strong>${escapeHtml(link.label || "项目交接")} · ${escapeHtml(statusNames[status] || status)}</strong><small>证据 ${score}/4：事项 ${escapeHtml(evidence.work_items || 0)} 条 · 关联 ${escapeHtml(evidence.relations || 0)} 条 · 执行记录 ${escapeHtml(evidence.target_runs || 0)} 条 · 通知 ${escapeHtml(evidence.notifications || 0)} 条</small><small class="project-link-evidence-note">${escapeHtml(businessText)} · ${escapeHtml(auditEvidence.policy || "以真实运行记录为准")}</small></article>`; }).join(""); }
async function loadProjectLinks(){ try { const body = await workbenchRequestJson("/api/project-audit"); renderProjectLinks(body.links || []); } catch (error) { $("#project-links-note").textContent = "读取失败"; $("#project-link-list").innerHTML = `<div class="project-link-empty">项目联动关系读取失败：${escapeHtml(error.message)}</div>`; } }
function auditStatusClass(status) { return String(status || "configured").replace(/[^a-z_-]/g, ""); }
function auditMetric(label, value) { return `<div class="project-audit-metric"><strong>${escapeHtml(value ?? 0)}</strong><small>${escapeHtml(label)}</small></div>`; }
function renderProjectAudit(payload = {}) {
  const list = $("#project-audit-list");
  const summary = payload.summary || {};
  if (!list) return;
  const agents = payload.agents || [];
  const links = payload.links || [];
  const verified = Number(summary.verified_links || 0);
  const linkTotal = links.length;
  const synthetic = Number(summary.synthetic_links || 0);
  const legacy = Number(summary.legacy_links || 0);
  $("#project-audit-summary").textContent = `${summary.agents || agents.length} 个项目 Agent · ${summary.observed || 0} 个有运行记录 · 联动 ${verified}/${linkTotal} 条真实链路已验证${synthetic ? ` · ${synthetic} 条内部测试` : ""}${legacy ? ` · ${legacy} 条历史记录` : ""}`;
  $("#project-audit-generated").textContent = payload.generated_at ? `审计时间 ${formatNotificationTime(payload.generated_at)} · v${payload.version || "—"}` : "—";
  $("#project-links-note").textContent = linkTotal ? String(linkTotal) : "—";
  if (!agents.length) { list.innerHTML = '<div class="project-audit-empty">暂时没有可审计的项目 Agent。</div>'; return; }
  list.innerHTML = agents.map((agent) => {
    const auditStatus = auditStatusClass(agent.audit_status);
    const runSummary = agent.run_summary || {};
    const toolChecks = agent.tool_checks || [];
    const readyTools = toolChecks.filter((tool) => tool.enabled).length;
    const latest = agent.latest_run || {};
    const inbound = agent.inbound_links || [];
    const outbound = agent.outbound_links || [];
    const verifiedLinks = [...inbound, ...outbound].filter((link) => link.status === "verified").length;
    const partialLinks = [...inbound, ...outbound].filter((link) => ["partial", "synthetic", "legacy"].includes(link.status)).length;
    const freshness = agent.freshness || {};
    const implementation = agent.implementation || {};
    const quality = agent.quality || {};
    const latestText = latest.status === "failed" ? `最近失败：${latest.error || latest.title || "未记录原因"}` : latest.title || "尚未运行";
    const qualityRate = quality.total ? `${Math.round(Number(quality.success_rate || 0) * 100)}%` : "—";
    const sourceRate = quality.total ? `${Math.round(Number(quality.source_completeness_rate || 0) * 100)}%` : "—";
    return `<article class="project-audit-card audit-${escapeHtml(auditStatus)}"><div class="project-audit-head"><div class="project-audit-title"><strong>${escapeHtml(agent.name || agent.project_id)}</strong><small>${escapeHtml(agent.project_id || "")}</small></div><span class="audit-status ${escapeHtml(auditStatus)}">${escapeHtml(agent.audit_status_label || "仅配置未验证")}</span></div><div class="project-audit-metrics">${auditMetric("工具可用", `${readyTools}/${toolChecks.length}`)}${auditMetric("Agent 运行", runSummary.total || 0)}${auditMetric("24h 成功率", qualityRate)}${auditMetric("来源完整度", sourceRate)}${auditMetric("联动证据", `${verifiedLinks} / ${partialLinks}`)}</div><div class="project-audit-freshness ${escapeHtml(freshness.status || "missing")}" title="${escapeHtml(freshness.source || "")}"><i aria-hidden="true"></i><span>${escapeHtml(freshness.label || "没有可用数据时间")} · ${escapeHtml(freshness.detail || freshness.source || "暂无来源")}</span></div><details class="project-audit-details"><summary>查看真实边界</summary><p><b>最近运行：</b>${escapeHtml(latestText)}</p><p><b>质量：</b>${escapeHtml(`${quality.total || 0} 次运行 · ${qualityRate} 成功 · ${sourceRate} 有来源`)}</p><p><b>已具备：</b>${escapeHtml((implementation.implemented || []).slice(0, 4).join("、") || "暂无登记")}</p><p><b>下一轮：</b>${escapeHtml((implementation.gaps || []).slice(0, 4).join("、") || "暂无登记")}</p><p><b>联动：</b>${escapeHtml(`${inbound.length} 条入站 · ${outbound.length} 条出站`)}</p></details></article>`;
  }).join("");
}
async function loadProjectAudit() {
  const list = $("#project-audit-list");
  try {
    const body = await workbenchRequestJson("/api/project-audit");
    renderProjectAudit(body);
  } catch (error) {
    $("#project-audit-summary").textContent = "能力审计读取失败";
    $("#project-audit-generated").textContent = error.message;
    if (list) list.innerHTML = `<div class="project-audit-empty">${escapeHtml(error.message)} · 点击“刷新审计”重试</div>`;
  }
}
async function loadCollaborationDetails() {
  if (collaborationLoaded) return;
  collaborationLoaded = true;
  await Promise.all([loadProjectLinks(), loadProjectAudit()]);
}
function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; ++i) output[i] = raw.charCodeAt(i);
  return output;
}
function setupCommandPalette() {
  const palette = $("#command-palette");
  const input = $("#command-palette-input");
  const results = $("#command-palette-results");
  if (!palette || !input || !results) return;
  let workItems = [];
  let activeIndex = -1;
  let previousFocus = null;
  function buildSources() {
    const projectSources = (projects || []).map((p) => ({ kind: "项目", title: p.title || p.id, href: p.href || "#", hint: p.meta || p.group || "项目入口" }));
    const toolSources = [
      { kind: "工具", title: "自动化中心", href: "/automation", hint: "定时任务与规则" },
      { kind: "工具", title: "Git 项目中心", href: "/git", hint: "本机仓库扫描" },
      { kind: "工具", title: "GitHub 工具目录", href: "/github-tools", hint: "效率工具调研" },
      { kind: "工具", title: "审批与交付", href: "/approvals", hint: "审批 / Web Push" },
      { kind: "工具", title: "总调度 Agent", href: "#agent", hint: "发起跨项目调度" },
      { kind: "工具", title: "全局 LLM 配置", href: "#llm", hint: "模型 Provider 管理" },
    ];
    const itemSources = (workItems || []).map((item) => ({ kind: "待办", title: item.title || "工作项", href: workItemHref(item), hint: `${item.status || ""} · ${item.source_project || ""} → ${item.target_project || ""}` }));
    return [...projectSources, ...toolSources, ...itemSources];
  }
  function openPalette() {
    workItems = [...pendingWorkItems];
    previousFocus = document.activeElement;
    activeIndex = -1;
    palette.classList.remove("hidden");
    palette.setAttribute("aria-hidden", "false");
    input.setAttribute("aria-controls", "command-palette-results");
    input.value = "";
    results.innerHTML = '<div class="command-palette-empty">输入关键词，快速跳转</div>';
    input.focus();
  }
  function closePalette() {
    palette.classList.add("hidden");
    palette.setAttribute("aria-hidden", "true");
    activeIndex = -1;
    const focusTarget = previousFocus && typeof previousFocus.focus === "function" ? previousFocus : $("#project-search");
    previousFocus = null;
    focusTarget?.focus();
  }
  function setActive(index) {
    const items = [...results.querySelectorAll(".command-palette-item")];
    activeIndex = items.length ? (index + items.length) % items.length : -1;
    items.forEach((item, itemIndex) => {
      const active = itemIndex === activeIndex;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", active ? "true" : "false");
    });
    const active = items[activeIndex];
    input.setAttribute("aria-activedescendant", active?.id || "");
    if (active) active.scrollIntoView({ block: "nearest" });
  }
  function render(query) {
    const q = query.trim().toLowerCase();
    activeIndex = -1;
    if (!q) { results.innerHTML = '<div class="command-palette-empty">输入关键词，快速跳转到项目、待办或工具</div>'; return; }
    const matches = buildSources().filter((s) => `${s.title} ${s.hint}`.toLowerCase().includes(q)).slice(0, 12);
    if (!matches.length) { results.innerHTML = '<div class="command-palette-empty">没有匹配项</div>'; return; }
    results.innerHTML = matches.map((s, index) => `<button id="command-palette-option-${index}" class="command-palette-item" data-index="${index}" data-href="${escapeHtml(s.href)}" type="button" role="option" aria-selected="false"><span class="command-palette-kind">${escapeHtml(s.kind)}</span><span class="command-palette-title">${escapeHtml(s.title)}</span><small>${escapeHtml(s.hint)}</small><b>↵</b></button>`).join("");
    results.querySelectorAll(".command-palette-item").forEach((el) => el.addEventListener("click", () => { const href = el.dataset.href; if (href === "#agent") { closePalette(); $("#global-agent-button")?.click(); } else if (href === "#llm") { closePalette(); $("#global-settings-button")?.click(); } else { window.open(href, "_blank", "noopener"); closePalette(); } }));
  }
  document.addEventListener("keydown", (event) => {
    const isCmdK = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k";
    if (isCmdK) { event.preventDefault(); palette.classList.contains("hidden") ? openPalette() : closePalette(); }
    if (event.key === "Escape" && !palette.classList.contains("hidden")) { closePalette(); input.blur(); }
  });
  palette.addEventListener("click", (event) => { if (event.target === palette) closePalette(); });
  input.addEventListener("input", () => render(input.value));
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const items = [...results.querySelectorAll(".command-palette-item")];
      if (!items.length) return;
      setActive(activeIndex + (event.key === "ArrowDown" ? 1 : -1));
    } else if (event.key === "Enter") {
      const active = results.querySelector(".command-palette-item.active") || results.querySelector(".command-palette-item");
      if (active) active.click();
    }
  });
  palette.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const focusables = [...palette.querySelectorAll("button, input")].filter((item) => !item.disabled && !item.hidden);
    if (!focusables.length) return;
    const first = focusables[0]; const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  void render("");
}
function setupPwaInstall() {
  const bar = $("#pwa-install-bar");
  if (!bar) return;
  if (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) return;
  if (localStorage.getItem("pwa-install-dismissed")) return;
  let deferredPrompt = null;
  const setVisible = (visible) => {
    bar.hidden = !visible;
    bar.classList.toggle("hidden", !visible);
  };
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredPrompt = event;
    setVisible(true);
  });
  $("#pwa-install-confirm")?.addEventListener("click", async () => {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    const choice = await deferredPrompt.userChoice.catch(() => null);
    deferredPrompt = null;
    setVisible(false);
    if (choice?.outcome === "accepted") localStorage.setItem("pwa-install-dismissed", "1");
  });
  $("#pwa-install-dismiss")?.addEventListener("click", () => {
    setVisible(false);
    localStorage.setItem("pwa-install-dismissed", "1");
  });
  window.addEventListener("appinstalled", () => {
    setVisible(false);
    localStorage.setItem("pwa-install-dismissed", "1");
  });
}
function setupPushPanel() {
  const modal = $("#push-modal");
  const openButton = $("#push-open");
  const closeButton = $("[data-close-push]");
  if (!modal || !openButton) return;
  const stateEl = $("#push-state");
  const configEl = $("#push-config-state");
  const badge = $("#push-nav-badge");
  const quietStart = $("#push-quiet-start");
  const quietEnd = $("#push-quiet-end");
  const deliveryList = $("#push-delivery-list");
  async function refresh() {
    try {
      const [config, subscriptions] = await Promise.all([workbenchRequestJson("/api/push/config"), workbenchRequestJson("/api/push/subscriptions")]);
      const items = subscriptions.subscriptions || [];
      if (stateEl) stateEl.textContent = items.length ? `已订阅 ${items.length} 个浏览器` : "尚未订阅浏览器推送";
      if (configEl) configEl.textContent = config.configured ? `VAPID 已配置 · 可推送` : `VAPID 未配置 · ${config.public_key ? "公钥有" : "公钥缺失"}`;
      if (badge) { badge.textContent = items.length ? String(items.length) : ""; badge.dataset.zero = String(!items.length); }
      if (items[0]) { quietStart.value = items[0].quiet_start || "22:00"; quietEnd.value = items[0].quiet_end || "08:00"; }
    } catch (e) { if (stateEl) stateEl.textContent = "状态读取失败"; }
  }
  async function loadDeliveries() {
    try {
      const body = await workbenchRequestJson("/api/push/deliveries?limit=5");
      const deliveries = body.deliveries || [];
      if (deliveryList) deliveryList.innerHTML = deliveries.length ? deliveries.map((d) => `<div class="push-delivery-row"><span>${escapeHtml(d.title || d.event_key || "推送")}</span><small>${escapeHtml(d.status || "—")} · ${escapeHtml(formatNotificationTime(d.created_at))}</small>${d.error ? `<em>${escapeHtml(String(d.error).slice(0, 60))}</em>` : ""}</div>`).join("") : '<div class="work-item-empty">还没有推送记录。</div>';
    } catch (e) { if (deliveryList) deliveryList.innerHTML = '<div class="work-item-empty">推送记录读取失败。</div>'; }
  }
  openButton.addEventListener("click", () => { modal.classList.remove("hidden"); document.body.style.overflow = "hidden"; void refresh(); void loadDeliveries(); });
  closeButton?.addEventListener("click", () => { modal.classList.add("hidden"); document.body.style.overflow = ""; });
  modal.addEventListener("click", (event) => { if (event.target === modal) { modal.classList.add("hidden"); document.body.style.overflow = ""; } });
  $("#push-subscribe")?.addEventListener("click", async (event) => {
    const button = event.currentTarget; button.disabled = true; button.textContent = "订阅中…";
    try {
      const config = await workbenchRequestJson("/api/push/config");
      if (!config.public_key) throw new Error("服务器未提供 VAPID 公钥");
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) throw new Error("当前浏览器不支持 Web Push");
      const registration = await navigator.serviceWorker.ready;
      // 若已存在旧公钥订阅，先取消再订阅（更换 applicationServerKey 必须 unsubscribe 后重订阅）
      const existing = await registration.pushManager.getSubscription();
      if (existing) {
        await existing.unsubscribe();
      }
      const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(config.public_key) });
      const json = subscription.toJSON();
      await workbenchRequestJson("/api/push/subscriptions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys || {}, user_agent: navigator.userAgent, quiet_start: quietStart.value || "22:00", quiet_end: quietEnd.value || "08:00", enabled: true }) });
      if (stateEl) stateEl.textContent = "订阅成功";
      await refresh();
    } catch (error) { if (stateEl) stateEl.textContent = error.message; }
    finally { button.disabled = false; button.textContent = "订阅浏览器推送"; }
  });
  $("#push-test")?.addEventListener("click", async (event) => {
    const button = event.currentTarget; button.disabled = true;
    try {
      const body = await workbenchRequestJson("/api/push/test", { method: "POST" });
      if (stateEl) stateEl.textContent = body.message || `已发送 ${body.sent || 0} 条`;
      await loadDeliveries();
    } catch (error) { if (stateEl) stateEl.textContent = error.message; }
    finally { button.disabled = false; }
  });
  $("#push-save-window")?.addEventListener("click", async (event) => {
    const button = event.currentTarget; button.disabled = true;
    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.getSubscription();
      if (!subscription) throw new Error("当前没有浏览器订阅，请先订阅");
      const json = subscription.toJSON();
      await workbenchRequestJson("/api/push/subscriptions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys || {}, user_agent: navigator.userAgent, quiet_start: quietStart.value || "22:00", quiet_end: quietEnd.value || "08:00", enabled: true }) });
      if (stateEl) stateEl.textContent = "静默时段已保存";
      await refresh();
    } catch (error) { if (stateEl) stateEl.textContent = error.message; }
    finally { button.disabled = false; }
  });
  void refresh();
}
async function loadApprovalQueue() {
  const badge = $("#approvals-badge");
  if (!badge) return;
  try {
    const body = await workbenchRequestJson("/api/approval-queue");
    const total = body.total || 0;
    if (total > 0) { badge.hidden = false; badge.textContent = total; badge.title = `${total} 项待确认（审批 / 待确认工作项 / 待确认动作）`; }
    else { badge.hidden = true; badge.title = "没有待确认事项"; }
  } catch { badge.hidden = true; }
}
async function loadAppVersion() {
  const el = $("#app-version");
  if (!el) return;
  try {
    const body = await workbenchRequestJson("/api/meta");
    if (body && body.version) el.textContent = `v${body.version}`;
  } catch (e) { /* 保留静态版本号 */ }
}
function setupProjectAudit() {
  const modal = $("#collaboration-modal");
  const openButton = $("#collaboration-open");
  $("#refresh-project-audit")?.addEventListener("click", (event) => { const button = event.currentTarget; button.disabled = true; button.textContent = "刷新中…"; collaborationLoaded = true; void loadProjectAudit().finally(() => { button.disabled = false; button.textContent = "刷新审计"; }); });
  $("#prepare-collaboration")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const operation = $("#collaboration-operation");
    const confirmed = window.confirm("生成并确认主动协作计划？确认后计划会进入 Agent Worker 队列，仍会在每一步保留运行记录。");
    if (!confirmed) return;
    button.disabled = true; button.textContent = "生成中…";
    if (operation) operation.textContent = "正在汇总待办、失败运行和 Agent 状态…";
    try {
      const body = await workbenchRequestJson("/api/workbench/collaboration/prepare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmed: true, limit: 8 }) });
      if (operation) operation.textContent = body.plan ? `${body.message} 计划 ${String(body.plan.id).slice(0, 8)}…` : body.message;
      await loadWorkItems();
    } catch (error) { if (operation) operation.textContent = error.message; }
    finally { button.disabled = false; button.textContent = "生成主动协作计划"; }
  });
  if (!modal || !openButton) return;
  openButton.addEventListener("click", () => {
    modal.classList.remove("hidden");
    modal.dataset.prevFocus = document.activeElement?.id || "";
    void loadCollaborationDetails();
    const firstFocus = modal.querySelector("button");
    if (firstFocus) firstFocus.focus();
  });
  document.querySelectorAll("[data-close-collaboration]").forEach((item) => item.addEventListener("click", () => { modal.classList.add("hidden"); const prev = document.getElementById(modal.dataset.prevFocus || ""); (prev || openButton).focus(); }));
  modal.addEventListener("click", (event) => { if (event.target === modal) { modal.classList.add("hidden"); const prev = document.getElementById(modal.dataset.prevFocus || ""); (prev || openButton).focus(); } });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !modal.classList.contains("hidden")) { modal.classList.add("hidden"); const prev = document.getElementById(modal.dataset.prevFocus || ""); (prev || openButton).focus(); } });
  modal.addEventListener("keydown", (event) => { if (event.key === "Tab") { const focusables = [...modal.querySelectorAll("button, a, input, select, textarea, [tabindex]:not([tabindex='-1'])")].filter((el) => !el.disabled && !el.hidden); if (!focusables.length) return; const first = focusables[0], last = focusables[focusables.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } } });
}

function setupDecisionRecorder() {
  const modal = $("#decision-modal");
  const openButton = $("#record-decision");
  const form = $("#decision-form");
  if (!modal || !openButton || !form) return;
  const close = () => { modal.classList.add("hidden"); openButton.focus(); };
  openButton.addEventListener("click", () => { modal.classList.remove("hidden"); $("#decision-name")?.focus(); });
  document.querySelectorAll("[data-close-decision]").forEach((button) => button.addEventListener("click", close));
  modal.addEventListener("click", (event) => { if (event.target === modal) close(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type=submit]");
    const message = $("#decision-message");
    const nextSteps = $("#decision-next-steps").value.split(/\n+/).map((item) => item.trim()).filter(Boolean);
    button.disabled = true; message.textContent = "正在保存决策和后续待办…"; message.className = "modal-message";
    try {
      const projectIds = $("#decision-projects")?.value.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean) || [];
      const body = await workbenchRequestJson("/api/workbench/decisions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: $("#decision-name").value.trim(), decision: $("#decision-text").value.trim(), rationale: $("#decision-rationale").value.trim(), next_steps: nextSteps, project_ids: projectIds, confirmed: $("#decision-confirmed").checked }) });
      message.textContent = body.message || "决策已保存。"; message.className = "modal-message success";
      form.reset(); await loadWorkItems();
      window.setTimeout(close, 450);
    } catch (error) { message.textContent = error.message; message.className = "modal-message error"; }
    finally { button.disabled = false; }
  });
}
const notificationState = { items: [], selectedId: null, filter: "all" };
const notificationKindNames = { info: "信息", agent_dispatch: "Agent 调度", agent_action: "Agent 动作", alert: "告警", task: "待办", handoff: "项目交接", research: "研究", quota: "额度提醒" };
const notificationLevelNames = { info: "通知", success: "已完成", warning: "需要关注", error: "异常", critical: "紧急" };
function formatNotificationTime(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const diff = Date.now() - date.getTime();
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function notificationLevel(item) {
  const actions = item.context?.actions || [];
  if (item.kind === "agent_dispatch" && ["done", "succeeded", "partial", "failed"].includes(item.context?.status)) {
    if (item.context.status === "failed" || actions.some((action) => action.status === "failed")) return "error";
    if (item.context.status === "partial" || actions.some((action) => action.status === "pending")) return "warning";
    return "success";
  }
  return notificationLevelNames[item.level] ? item.level : "info";
}
function compactNotificationText(value, max = 220) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
function plainAgentText(value, max = 360) {
  const text = String(value ?? "")
    .replace(/^#{1,6}\s*/gm, "")
    .replace(/\*\*(.*?)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/^\s*[-*]\s+/gm, "")
    .replace(/^\s*\d+\.\s+/gm, "")
    .replace(/\|/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return compactNotificationText(text, max);
}
function agentConclusionText(value, max = 260) {
  const text = plainAgentText(value, max * 2);
  const stop = text.search(/已知事实与证据|判断、假设与不确定性|可直接执行的本地动作|需要我确认的动作|下一步/);
  return compactNotificationText(stop > 0 ? text.slice(0, stop) : text, max);
}
function actionSymbol(action) {
  return action?.result?.symbol || action?.arguments?.symbol || "";
}
function dedupeAgentActions(actions = []) {
  const result = [];
  const indexes = new Map();
  const priority = { failed: 4, pending: 3, executed: 2, rejected: 1 };
  actions.filter((action) => action && typeof action === "object").forEach((action) => {
    const symbol = actionSymbol(action);
    const key = `${action.tool || action.name || "action"}:${symbol || JSON.stringify(action.arguments || {})}`;
    const index = indexes.get(key);
    if (index === undefined) { indexes.set(key, result.length); result.push(action); return; }
    if ((priority[action.status] || 0) > (priority[result[index].status] || 0)) result[index] = action;
  });
  return result;
}
function actionResultText(action) {
  const symbol = actionSymbol(action);
  if (action?.status === "executed" && action?.tool === "market.watchlist.add") return action.result?.added === false ? `已在自选中 ${symbol.toUpperCase()}` : `已加入自选 ${symbol.toUpperCase()}`;
  if (action?.status === "pending") return `待确认：${action.name || action.tool || "Agent 动作"}${symbol ? ` · ${symbol.toUpperCase()}` : ""}`;
  if (action?.status === "failed") return `执行失败：${action.name || action.tool || "Agent 动作"}${action.result?.error ? ` · ${action.result.error}` : ""}`;
  return `${action.name || action.tool || "Agent 动作"}${symbol ? ` · ${symbol.toUpperCase()}` : ""}`;
}
function notificationActionSummary(item) {
  return dedupeAgentActions(item?.context?.actions || []).map(actionResultText).join("；");
}
function notificationBody(item) {
  const source = item.project_name || item.project_id || "工作台";
  const body = String(item.body || "").trim();
  const actionSummary = notificationActionSummary(item);
  if (item.kind === "agent_dispatch" && item.context?.status && item.context.status !== "running") return `${item.context.status === "failed" ? "调度失败" : "调度已完成"} · 负责 Agent：${source}${actionSummary ? ` · ${actionSummary}` : ""}`;
  if (item.kind === "agent_dispatch" && body && body === String(item.title || "").replace(/^总调度[：:]\s*/, "")) return `已记录调度任务，负责 Agent：${source}；旧记录未保存完整结果。`;
  if (body) return compactNotificationText(body);
  if (item.kind === "agent_dispatch") return `工作台已创建调度任务，负责 Agent：${source}。点击查看处理结果和动作状态。`;
  if (item.kind === "agent_action") return `${source} 有一个需要关注的 Agent 动作，点击进入项目查看。`;
  if (item.kind === "alert") return `工作台检测到一条来自 ${source} 的异常事件，请点击查看详情。`;
  return body || `来自 ${source} 的应用事件，点击进入项目查看详情。`;
}
function notificationDetail(item) {
  const source = item.project_name || item.project_id || "工作台";
  const kind = notificationKindNames[item.kind] || item.kind || "应用事件";
  const rawBody = compactNotificationText(item.body, 520);
  const context = item.context || {};
  const children = (context.children || []).map((child) => `${child.name || child.project_id || "子 Agent"}：${agentConclusionText(child.answer)}`).filter(Boolean);
  const actionSummary = notificationActionSummary(item);
  if (item.kind === "agent_dispatch") {
    const taskOnly = rawBody && rawBody === String(item.title || "").replace(/^总调度[：:]\s*/, "");
    if (children.length || actionSummary) return [`${item.context?.status === "failed" ? "调度失败" : "调度完成"} · ${item.title || "总调度事件"}`, children.length ? `子 Agent 结果：\n${children.join("\n")}` : "", actionSummary ? `动作结果：${actionSummary}` : ""].filter(Boolean).join("\n\n");
    return `${item.title || "总调度事件"}\n\n${taskOnly ? `由 ${source} 负责；这条历史记录没有保存完整执行结果，请打开项目查看。` : rawBody || `由 ${source} 负责的调度事件。`}`;
  }
  if (item.kind === "agent_action") return `${source} 产生了一个 Agent 动作。${rawBody ? `\n\n动作说明：${rawBody}` : ""}`;
  if (item.kind === "alert") return `工作台检测到来自 ${source} 的${kind}。${rawBody ? `\n\n事件内容：${rawBody}` : ""}`;
  return rawBody || `来自 ${source} 的${kind}。`;
}
function notificationDetailMarkup(item) {
  const context = item.context || {};
  const task = String(item.title || item.body || "应用事件").replace(/^总调度[：:]\s*/, "");
  const children = (context.children || [])
    .map((child) => ({ name: child.name || child.project_id || "子 Agent", answer: agentConclusionText(child.answer, 520) }))
    .filter((child) => child.answer);
  const answer = agentConclusionText(context.answer, 620);
  const actionItems = dedupeAgentActions(context.actions || []).map((action) => {
    const state = action.status === "executed" ? "已执行" : action.status === "pending" ? "待确认" : action.status === "failed" ? "失败" : action.status || "已记录";
    return `<li><span class="notification-action-state state-${escapeHtml(action.status || "recorded")}">${escapeHtml(state)}</span><span>${escapeHtml(actionResultText(action))}</span></li>`;
  }).join("");
  const actionSummary = notificationActionSummary(item);
  const resultMarkup = actionSummary
    ? `<p class="notification-result-final">${escapeHtml(actionSummary)}</p>${answer ? `<p class="notification-result-note">Agent 分析记录：${escapeHtml(answer)}</p>` : ""}`
    : answer
    ? `<p>${escapeHtml(answer)}</p>`
    : children.length
      ? children.map((child) => `<div class="notification-agent-result"><strong>${escapeHtml(child.name)}</strong><p>${escapeHtml(child.answer)}</p></div>`).join("")
      : `<p class="notification-muted">这条历史记录没有保存完整结果；打开来源项目可查看详细运行记录。</p>`;
  return `<div class="notification-detail-block"><span class="notification-detail-label">任务</span><p>${escapeHtml(task)}</p></div><div class="notification-detail-block"><span class="notification-detail-label">Agent 结果</span>${resultMarkup}</div>${actionItems ? `<div class="notification-detail-block"><span class="notification-detail-label">动作状态</span><ul class="notification-action-list">${actionItems}</ul></div>` : ""}`;
}
function notificationNextStep(item) {
  const source = item.project_name || item.project_id || "来源项目";
  if (item.kind === "agent_action") return item.level === "warning" ? "打开项目确认或拒绝这个动作。" : "打开项目查看动作运行记录。";
  if (item.kind === "alert") return "打开对应项目检查异常，并在处理后刷新状态。";
  if (item.kind === "agent_dispatch" && item.context?.actions?.some((action) => action.status === "pending")) return "打开来源项目确认待执行动作。";
  if (item.kind === "agent_dispatch" && item.context?.status === "failed") return `打开「${source}」查看失败原因并重试。`;
  if (item.kind === "agent_dispatch") return item.level === "error" || item.level === "critical" ? `打开「${source}」查看失败原因并重试。` : item.level === "warning" ? `打开「${source}」查看待确认动作。` : `动作已完成；需要时打开「${source}」查看完整汇总。`;
  return item.href ? `打开「${source}」查看完整上下文。` : "暂时无需操作。";
}
function notificationFullTime(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function renderNotifications(items = notificationState.items) {
  const list = $("#notification-list");
  const summary = $("#notification-summary");
  if (!list) return;
  const unreadCount = items.filter((item) => item.unread).length;
  const visibleItems = notificationState.filter === "unread" ? items.filter((item) => item.unread) : items;
  if (summary) summary.textContent = unreadCount ? `${unreadCount} 条未读 · 打开中心不会自动清除；点开单条或“全部已读”即可清除红点` : "已全部读完 · 这里显示工作台内的应用事件";
  const readAll = $("#notifications-read-all");
  if (readAll) readAll.disabled = unreadCount === 0;
  const filterCount = $("#notification-unread-filter-count");
  if (filterCount) filterCount.textContent = unreadCount ? (unreadCount > 99 ? "99+" : String(unreadCount)) : "";
  document.querySelectorAll("[data-notification-filter]").forEach((button) => {
    const active = button.dataset.notificationFilter === notificationState.filter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  if (!visibleItems.length) {
    const emptyTitle = notificationState.filter === "unread" ? "没有未读应用通知" : "暂时没有应用通知";
    const emptyBody = notificationState.filter === "unread" ? "红色数字已经清除；新的 Agent 结果、待确认动作或告警会出现在这里。" : "Agent 结果、待确认动作、额度告警和服务器异常会在这里留下记录。";
    list.innerHTML = `<div class="notification-empty"><span class="notification-empty-icon">✓</span><strong>${emptyTitle}</strong><p>${emptyBody}</p></div>`;
    return;
  }
  list.innerHTML = visibleItems.map((item) => {
    const level = notificationLevel(item);
    const kind = notificationKindNames[item.kind] || item.kind || "应用事件";
    const source = item.project_name || item.project_id || "工作台";
    const readLabel = item.unread ? "未读" : "已读";
    const href = item.href || "";
    const selected = String(notificationState.selectedId) === String(item.id);
    const hint = item.href && item.href !== "/" && item.href !== "#" ? (item.unread ? "点击打开 · 自动标记已读" : "点击打开") : (selected ? "收起详情" : "查看详情");
    return `<article class="notification-item ${item.unread ? "unread" : "read"} ${selected ? "selected" : ""} level-${escapeHtml(level)}" data-notification-id="${escapeHtml(item.id)}" role="listitem"><button class="notification-item-trigger" type="button" data-notification-select="${escapeHtml(item.id)}" aria-expanded="${selected ? "true" : "false"}" aria-label="${escapeHtml(`${readLabel}：${item.title}，来自${source}`)}"><span class="notification-item-top"><span class="notification-source"><i aria-hidden="true"></i>${escapeHtml(source)}</span><span class="notification-item-time">${escapeHtml(formatNotificationTime(item.created_at))}</span></span><span class="notification-item-title-row"><strong>${escapeHtml(item.title || "未命名通知")}</strong><span class="notification-level">${escapeHtml(notificationLevelNames[level])}</span></span><span class="notification-item-body">${escapeHtml(notificationBody(item))}</span><span class="notification-item-meta"><span>${escapeHtml(kind)}</span><span class="notification-read-state"><i aria-hidden="true"></i>${escapeHtml(readLabel)}</span><span class="notification-open-hint">${hint}</span></span></button>${selected ? `<div class="notification-detail"><div class="notification-detail-grid"><div><span class="notification-detail-label">来源 Agent</span><strong>${escapeHtml(source)}</strong></div><div><span class="notification-detail-label">事件类型</span><strong>${escapeHtml(kind)}</strong></div><div><span class="notification-detail-label">下一步</span><strong>${escapeHtml(notificationNextStep(item))}</strong></div></div><div class="notification-detail-content">${notificationDetailMarkup(item)}</div><div class="notification-detail-actions"><span>${escapeHtml(notificationFullTime(item.created_at))} · ${item.unread ? "正在标记已读" : "已标记为已读"}</span>${href ? `<a class="notification-open-project" href="${escapeHtml(href)}" target="_blank" rel="noopener" data-notification-open="${escapeHtml(item.id)}">打开${escapeHtml(source)} ↗</a>` : ""}</div></div>` : ""}</article>`;
  }).join("");
}
function updateNotificationBadge(items = notificationState.items) {
  const unreadCount = items.filter((item) => item.unread).length;
  const count = $("#notification-count");
  if (count) { count.textContent = unreadCount > 99 ? "99+" : String(unreadCount); count.dataset.zero = unreadCount ? "false" : "true"; }
  const filterCount = $("#notification-unread-filter-count");
  if (filterCount) filterCount.textContent = unreadCount ? (unreadCount > 99 ? "99+" : String(unreadCount)) : "";
  const button = $("#notifications-button");
  if (button) button.setAttribute("aria-label", unreadCount ? `应用内通知，${unreadCount} 条未读` : "应用内通知，已全部读完");
  renderNotifications(items);
}
async function loadNotifications() {
  try {
    const body = await workbenchRequestJson("/api/notifications?limit=50");
    const items = body.notifications || [];
    notificationState.items = items;
    updateNotificationBadge(items);
  } catch { /* notification delivery is optional and must not disturb the workspace */ }
}
async function markNotificationRead(id) {
  const body = await workbenchRequestJson(`/api/notifications/${encodeURIComponent(id)}/read`, { method: "POST" });
  const updated = body.notification;
  notificationState.items = notificationState.items.map((item) => String(item.id) === String(id) ? { ...item, ...updated, unread: false } : item);
  updateNotificationBadge(notificationState.items);
}
function setupNotifications() {
  const button = $("#notifications-button");
  const panel = $("#notification-panel");
  const list = $("#notification-list");
  const readAll = $("#notifications-read-all");
  const closeButton = $("#notifications-close");
  const backdrop = $("#notification-backdrop");
  if (!button || !panel) return;
  if (!$("#notification-feedback")) { const feedback = document.createElement("div"); feedback.id = "notification-feedback"; feedback.className = "notification-feedback"; feedback.setAttribute("role", "status"); feedback.setAttribute("aria-live", "polite"); panel.querySelector(".notification-panel-head")?.after(feedback); }
  const closePanel = () => { panel.classList.add("hidden"); backdrop?.classList.add("hidden"); button.setAttribute("aria-expanded", "false"); };
  const openPanel = async () => { panel.classList.remove("hidden"); backdrop?.classList.remove("hidden"); button.setAttribute("aria-expanded", "true"); notificationState.selectedId = null; setNotificationFeedback("正在读取应用事件…"); renderNotifications(); await loadNotifications(); setNotificationFeedback(""); };
  button.addEventListener("click", () => panel.classList.contains("hidden") ? openPanel() : closePanel());
  closeButton?.addEventListener("click", closePanel);
  backdrop?.addEventListener("click", closePanel);
  document.querySelectorAll("[data-notification-filter]").forEach((filterButton) => filterButton.addEventListener("click", () => { notificationState.filter = filterButton.dataset.notificationFilter || "all"; notificationState.selectedId = null; renderNotifications(); }));
  readAll?.addEventListener("click", async () => {
    if (!notificationState.items.some((item) => item.unread)) return;
    readAll.disabled = true;
    try {
      const body = await workbenchRequestJson("/api/notifications/read-all", { method: "POST" });
      notificationState.items = notificationState.items.map((item) => ({ ...item, unread: false, read_at: item.read_at || new Date().toISOString() }));
      updateNotificationBadge();
      setNotificationFeedback(body.count ? `已标记 ${body.count} 条应用通知为已读，红点已清除。` : "没有未读应用通知。", "success");
    } catch (error) { setNotificationFeedback(error.message, "error"); readAll.disabled = false; }
  });
  list?.addEventListener("click", async (event) => {
    event.stopPropagation();
    const openLink = event.target.closest("[data-notification-open]");
    if (openLink) {
      event.preventDefault();
      const href = openLink.getAttribute("href") || "/";
      openWorkbenchTarget(href);
      try { await markNotificationRead(openLink.dataset.notificationOpen); } catch (error) { setNotificationFeedback(error.message, "error"); }
      closePanel();
      return;
    }
    const selectButton = event.target.closest("[data-notification-select]");
    if (selectButton) {
      const id = selectButton.dataset.notificationSelect;
      const item = notificationState.items.find((entry) => String(entry.id) === String(id));
      // 有目标页面的通知：点一下直接打开，未读自动标记已读。
      if (item?.href && item.href !== "/" && item.href !== "#") {
        openWorkbenchTarget(item.href);
        try { await markNotificationRead(id); } catch (error) { setNotificationFeedback(error.message, "error"); }
        closePanel();
        return;
      }
      // 没有目标页面的通知（如纯提醒）：展开详情。
      notificationState.selectedId = String(notificationState.selectedId) === String(id) ? null : id;
      renderNotifications();
      if (item?.unread) {
        try { await markNotificationRead(id); setNotificationFeedback("这条应用通知已读，红点已清除。", "success"); } catch (error) { setNotificationFeedback(error.message, "error"); }
      }
    }
  });
  document.addEventListener("click", (event) => { if (!event.target.closest(".notification-menu")) closePanel(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !panel.classList.contains("hidden")) { closePanel(); button.focus(); } });
  loadNotifications();
  window.setInterval(loadNotifications, 30000);
}
async function loadProjects() { try { const [body, preferences] = await Promise.all([workbenchRequestJson("/api/projects"), workbenchRequestJson("/api/projects/preferences")]); projects = body.projects || []; hiddenProjectIds = new Set((preferences.hidden_ids || []).map(String)); $("#inbox-count").textContent = body.summary?.inbox_count ?? "—"; $("#note-count").textContent = body.summary?.note_count ?? "—"; renderProjects(); } catch (error) { $("#project-grid").innerHTML = `<div class="empty-projects">项目配置读取失败：${escapeHtml(error.message)}</div>`; $("#inbox-count").textContent = "—"; $("#note-count").textContent = "—"; } }
function inlineAgentMarkdown(value) {
  return escapeHtml(value)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}
function renderAgentAnswer(value) {
  const lines = String(value || "没有返回结果").replace(/\r/g, "").split("\n");
  const html = [];
  let listType = "";
  const closeList = () => { if (listType) { html.push(`</${listType}>`); listType = ""; } };
  const flushTable = (rows) => {
    if (!rows.length) return;
    const cells = (line) => line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((cell) => inlineAgentMarkdown(cell.trim()));
    const header = cells(rows[0]);
    const body = rows.slice(2).map((row) => `<tr>${cells(row).map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("");
    html.push(`<div class="agent-table-wrap"><table><thead><tr>${header.map((cell) => `<th>${cell}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`);
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].trim();
    if (!line) { closeList(); continue; }
    if (line.startsWith("|") && lines[index + 1]?.trim().startsWith("|") && /-{3,}/.test(lines[index + 1])) {
      const table = [line, lines[index + 1]];
      index += 2;
      while (index < lines.length && lines[index].trim().startsWith("|")) table.push(lines[index++].trim());
      index -= 1; closeList(); flushTable(table); continue;
    }
    const heading = line.match(/^#{1,3}\s+(.+)$/);
    if (heading) { closeList(); html.push(`<h4>${inlineAgentMarkdown(heading[1])}</h4>`); continue; }
    const bullet = line.match(/^[-*]\s+(.+)$/);
    const numbered = line.match(/^\d+[.)]\s+(.+)$/);
    if (bullet || numbered) {
      const nextType = numbered ? "ol" : "ul";
      if (listType !== nextType) { closeList(); listType = nextType; html.push(`<${listType}>`); }
      html.push(`<li>${inlineAgentMarkdown((bullet || numbered)[1])}</li>`); continue;
    }
    closeList(); html.push(`<p>${inlineAgentMarkdown(line)}</p>`);
  }
  closeList();
  return html.join("");
}
function agentActionMarkup(actions){
  return dedupeAgentActions(actions).map((action)=>{
    const label = actionResultText(action);
    if(action.status === "pending") return `<div class="agent-action pending"><span><b class="action-state">待确认</b>${escapeHtml(label)}</span><button class="agent-action-confirm" data-action-id="${escapeHtml(action.id)}" type="button">确认执行</button></div>`;
    if(action.status === "executed") return `<div class="agent-action executed"><b class="action-state">已执行</b><span>${escapeHtml(label)}</span></div>`;
    return `<div class="agent-action failed"><b class="action-state">失败</b><span>${escapeHtml(label)}</span></div>`;
  }).join("");
}
function agentExecutionPlanTrace(plan = {}) {
  if (!plan || !plan.kind) return "";
  const statusLabels = { queued: "排队中", running: "执行中", completed: "已完成", succeeded: "已完成", partial: "部分完成", failed: "失败" };
  const targets = Array.isArray(plan.targets) ? plan.targets.join("、") : plan.target || "自动目标";
  const tools = Array.isArray(plan.requested_tools) ? plan.requested_tools.join("、") : "";
  const children = Array.isArray(plan.child_run_ids) ? plan.child_run_ids.length : 0;
  const steps = Array.isArray(plan.steps) ? plan.steps.filter((step) => step?.status === "completed").length : 0;
  const flags = [statusLabels[plan.status] || plan.status || "已记录", plan.needs_confirmation ? "需人工确认路由" : "无需额外确认", children ? `子任务 ${children} 个` : "", steps ? `步骤 ${steps}/${plan.steps.length}` : ""].filter(Boolean).join(" · ");
  const intent = plan.intent ? `意图：${escapeHtml(plan.intent)} · ` : "";
  return `${intent}执行计划：${escapeHtml(targets)} · ${escapeHtml(flags)} · 路由 ${escapeHtml(plan.route_mode || "自动")} · 置信度 ${Math.round(Number(plan.route_confidence || 0) * 100)}%${tools ? ` · 工具约束 ${escapeHtml(tools)}` : ""}`;
}
function agentResultContractMarkup(contract = {}) {
  const sections = contract?.sections || {};
  const labels = { facts: "事实", judgement: "判断", evidence: "证据", risks: "风险", actions: "动作", next_steps: "下一步" };
  const entries = Object.entries(labels).filter(([key]) => Array.isArray(sections[key]) && sections[key].length);
  if (!contract?.summary && !entries.length) return "";
  const body = entries.map(([key, label]) => `<div><strong>${label}</strong><ul>${sections[key].slice(0, 8).map((item) => `<li>${escapeAny(item)}</li>`).join("")}</ul></div>`).join("");
  const citations = (contract.citations || []).slice(0, 8).map((item) => item.type === "url" ? `<a href="${escapeHtml(item.value)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.label || item.value)}</a>` : `<span>${escapeHtml(item.value)}</span>`).join(" · ");
  const refs = (contract.source_refs || []).slice(0, 8).map((item) => { const label = `${item.label || item.id || "未命名来源"}${item.data_as_of ? ` · ${item.data_as_of}` : ""}`; return String(item.locator || "").startsWith("http") ? `<a href="${escapeHtml(item.locator)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>` : `<span>${escapeHtml(label)}</span>`; }).join(" · ");
  const review = contract.needs_review ? `<span class="agent-contract-review">需复核：${escapeHtml((contract.review_reasons || []).join("、") || "证据不足")}</span>` : "";
  const coverage = contract.source_coverage || {};
  const coverageText = coverage.total ? `引用覆盖：${coverage.with_locator || 0}/${coverage.total} 可定位 · ${coverage.with_data_time || 0}/${coverage.total} 有数据时间` : "";
  const plan = contract.execution_plan || {};
  const planTrace = agentExecutionPlanTrace(plan);
  const memoryTrace = contract.memory_refs?.length ? `使用了 ${contract.memory_refs.length} 条已确认记忆` : "";
  const memoryUpdateTrace = contract.memory_updates?.length ? `本轮发现 ${contract.memory_updates.length} 条记忆` : "";
  const memoryContext = contract.memory_context || {};
  const memoryBudgetTrace = memoryContext.chars ? `记忆上下文 ${Number(memoryContext.chars)} 字${Number(memoryContext.calls) > 1 ? ` / ${Number(memoryContext.calls)} 次调用` : ""}` : "";
  const trace = [contract.data_as_of ? `数据时间：${escapeHtml(contract.data_as_of)}` : "", refs ? `来源：${refs}` : "", coverageText, memoryTrace, memoryBudgetTrace, memoryUpdateTrace, planTrace, contract.artifact_ids?.length ? `产物 ${contract.artifact_ids.length} 份` : "", contract.work_item_ids?.length ? `事项 ${contract.work_item_ids.length} 条` : "", contract.relation_ids?.length ? `关联 ${contract.relation_ids.length} 条` : "", review, contract.replay?.href ? `<a href="${escapeHtml(contract.replay.href)}" target="_blank" rel="noopener noreferrer">查看执行回放</a>` : ""].filter(Boolean).join(" · ");
  return `<details class="agent-result-contract"><summary>结构化结果 · ${escapeHtml(contract.summary || "查看结论与证据")}</summary>${body || `<p>${escapeHtml(contract.summary || "暂无结构化摘要")}</p>`}${citations ? `<div class="agent-result-citations"><strong>可回溯来源</strong><p>${citations}</p></div>` : ""}${trace ? `<div class="agent-result-citations"><strong>审计链</strong><p>${trace}</p></div>` : ""}</details>`;
}
function agentRunReplayMarkup(runId) {
  return runId ? `<details class="agent-run-replay" data-run-replay="${escapeHtml(runId)}"><summary>查看执行回放</summary><div class="agent-run-replay-body">展开后查看执行过程、动作和子 Agent 路径。</div></details>` : "";
}
function setupGlobalAgentV2(){
  const modal = $("#global-agent-modal"), button = $("#global-agent-button"), form = $("#global-agent-form"), input = $("#global-agent-input"), target = $("#global-agent-target"), message = $("#global-agent-message"), result = $("#global-agent-result"), submit = $("#global-agent-submit");
  if (!modal || !button || !form) return;
  let history = [];
  let sessionId = "";
  const formStack = form.querySelector(".form-stack");
  formStack?.insertAdjacentHTML("afterbegin", `<div class="agent-session-toolbar"><label>继续之前的会话<select id="global-agent-sessions" aria-label="选择总调度会话"><option value="">新会话</option></select></label><button id="global-agent-new-session" class="secondary-button" type="button">新会话</button></div>`);
  const sessionSelect = $("#global-agent-sessions"), newSessionButton = $("#global-agent-new-session");
  const historyFromMessages = (items = []) => items.map((item) => item.role === "user" ? { role: "user", content: item.content || "" } : { role: "assistant", content: item.content || "", children: item.metadata?.result_contract?.agent_name || "工作台总调度 Agent", actions: item.metadata?.actions || [], result_contract: item.metadata?.result_contract || {}, run_id: item.metadata?.run_id || "" });
  const renderHistory = () => {
    result.innerHTML = `<div class="agent-thread">${history.map((turn) => turn.role === "user" ? `<div class="agent-bubble user"><span class="agent-bubble-label">你</span><div>${escapeHtml(turn.content)}</div></div>` : `<div class="agent-bubble assistant"><span class="agent-bubble-label">${escapeHtml(turn.children || "工作台总调度 Agent")}</span><div class="agent-answer-copy">${renderAgentAnswer(turn.content)}</div>${agentResultContractMarkup(turn.result_contract)}${turn.actions?.length ? `<div class="agent-action-heading">动作状态</div>${agentActionMarkup(turn.actions)}` : ""}${agentRunReplayMarkup(turn.run_id)}</div>`).join("")}</div>`;
    result.hidden = !history.length;
    result.scrollTop = result.scrollHeight;
  };
  async function loadAgents(){
    const body = await workbenchRequestJson("/api/agents");
    target.innerHTML = '<option value="">自动判断</option>' + (body.global_agent?.children || []).map((id) => { const agent = (body.agents || []).find((item) => item.project_id === id); return `<option value="${escapeHtml(id)}">${escapeHtml(agent?.name || id)} · ${escapeHtml(agent?.status_label || agent?.status || "规划中")}</option>`; }).join("");
    return body;
  }
  async function loadSession(id) {
    if (!id) { sessionId = ""; history = []; renderHistory(); if (sessionSelect) sessionSelect.value = ""; return; }
    const body = await workbenchRequestJson(`/api/agent/workbench/sessions/${encodeURIComponent(id)}`);
    sessionId = id;
    history = historyFromMessages(body.messages || []);
    renderHistory();
    if (sessionSelect) sessionSelect.value = id;
  }
  async function loadSessions(restoreLatest = false) {
    const body = await workbenchRequestJson("/api/agent/workbench/sessions?limit=30");
    const sessions = body.sessions || [];
    if (sessionSelect) {
      sessionSelect.innerHTML = '<option value="">新会话</option>' + sessions.map((session) => `<option value="${escapeHtml(session.id)}">${escapeHtml(session.title)}</option>`).join("");
      sessionSelect.value = sessionId;
    }
    if (restoreLatest && !sessionId && sessions.length) await loadSession(sessions[0].id);
    return sessions;
  }
  button.addEventListener("click", async () => { modal.classList.remove("hidden"); modal.dataset.prevFocus = document.activeElement?.id || ""; message.textContent = "正在读取会话和子 Agent 能力…"; message.classList.remove("error"); try { const [body] = await Promise.all([loadAgents(), loadSessions(true)]); message.textContent = `已接入 ${body.global_agent?.children?.length || 0} 个子 Agent · 会话和记忆保存在本机。`; input.focus(); } catch (error) { message.textContent = error.message; message.classList.add("error"); } });
  sessionSelect?.addEventListener("change", () => loadSession(sessionSelect.value).catch((error) => { message.textContent = error.message; message.classList.add("error"); }));
  newSessionButton?.addEventListener("click", () => { sessionId = ""; history = []; renderHistory(); if (sessionSelect) sessionSelect.value = ""; message.textContent = "已开始新会话；已确认的长期记忆仍会保留。"; input.focus(); });
  document.querySelectorAll("[data-close-global-agent]").forEach((item) => item.addEventListener("click", () => { modal.classList.add("hidden"); const prev = document.getElementById(modal.dataset.prevFocus || ""); (prev || button).focus(); }));
  modal.addEventListener("click", (event) => { if (event.target === modal) { modal.classList.add("hidden"); const prev = document.getElementById(modal.dataset.prevFocus || ""); (prev || button).focus(); } });
  modal.addEventListener("keydown", (event) => { if (event.key === "Tab") { const focusables = [...modal.querySelectorAll("button, input, select, textarea, [tabindex]:not([tabindex='-1'])")].filter((el) => !el.disabled && !el.hidden); if (!focusables.length) return; const first = focusables[0], last = focusables[focusables.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } } });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !modal.classList.contains("hidden")) { modal.classList.add("hidden"); button.focus(); } });
  result.addEventListener("click", async (event) => {
    const actionButton = event.target.closest("[data-action-id]");
    if (!actionButton) return;
    actionButton.disabled = true; actionButton.textContent = "执行中…";
    try {
      const body = await workbenchRequestJson(`/api/agent/actions/${encodeURIComponent(actionButton.dataset.actionId)}/confirm`, { method: "POST" });
      history.forEach((turn) => (turn.actions || []).forEach((action, index, actions) => { if (action.id === actionButton.dataset.actionId) actions[index] = body.action; }));
      renderHistory(); message.textContent = body.action?.status === "executed" ? "动作已执行并记录。" : "动作未执行，请检查状态。"; void loadNotifications();
    } catch (error) { actionButton.disabled = false; actionButton.textContent = "重试确认"; message.textContent = error.message; message.classList.add("error"); }
  });
  result.addEventListener("click", async (event) => {
    const summary = event.target.closest(".agent-run-replay > summary");
    if (!summary) return;
    const replay = summary.parentElement;
    if (replay.dataset.loaded === "true") return;
    const bodyEl = replay.querySelector(".agent-run-replay-body");
    bodyEl.textContent = "正在读取执行回放…";
    try {
      const body = await workbenchRequestJson(`/api/agent/workbench/runs/${encodeURIComponent(replay.dataset.runReplay)}`);
      const events = body.events || body.timeline?.events || [];
      const route = body.run?.request?.route || body.run?.result?.route || {};
      const eventMarkup = events.map((item) => `<li><strong>${escapeHtml(item.message || item.event_type || "运行事件")}</strong><small>${escapeHtml(item.created_at || "")}</small></li>`).join("");
      bodyEl.innerHTML = `<p>${route.confidence != null ? `路由置信度 ${Math.round(Number(route.confidence) * 100)}% · ` : ""}${escapeHtml(route.note || "已保留完整运行事件")}</p><ol>${eventMarkup || "<li>没有更多事件</li>"}</ol>`;
      replay.dataset.loaded = "true";
    } catch (error) { bodyEl.textContent = error.message; }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); const text = input.value.trim(); if (!text || submit.disabled) return;
    history.push({ role: "user", content: text }); renderHistory(); submit.disabled = true; message.classList.remove("error"); message.textContent = "正在检查上下文、调用子 Agent 并汇总…";
    const pending = { role: "assistant", content: "正在读取项目数据并执行工作流…", children: "工作台总调度 Agent" }; history.push(pending); renderHistory();
    try {
      const body = await workbenchRequestJson("/api/agent/dispatch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId, message: text, intent: "", project_ids: target.value ? [target.value] : [], context: { source: "workbench_home", conversation_turns: history.length, intent: "" } }) });
      sessionId = body.session?.id || sessionId;
      history = body.messages?.length ? historyFromMessages(body.messages) : [...history.slice(0, -1), { role: "assistant", content: body.answer || "没有返回结果", children: (body.children || []).map((item) => item.name || item.project_id).join(" · "), actions: (body.children || []).flatMap((item) => item.actions || []), result_contract: body.result_contract, run_id: body.run?.id || "" }];
      renderHistory(); await loadSessions(false); message.textContent = `已完成 · 会话已保存${body.memory_updates?.length ? ` · 本轮发现 ${body.memory_updates.length} 条记忆` : ""}`; void loadNotifications();
    } catch (error) { history.pop(); history.push({ role: "assistant", content: `这次调度没有完成：${error.message}`, children: "工作台总调度 Agent" }); renderHistory(); message.textContent = error.message; message.classList.add("error"); }
    finally { submit.disabled = false; }
  });
}

function setupMemoryCenter() {
  const nav = document.querySelector(".platform-nav");
  if (!nav || document.querySelector("#memory-open")) return;
  const button = document.createElement("button");
  button.id = "memory-open";
  button.className = "platform-nav-item";
  button.type = "button";
  button.innerHTML = '<span class="platform-nav-dot violet"></span>我的记忆<span id="memory-nav-badge" class="platform-nav-badge" data-zero="true">—</span>';
  const usageLink = [...nav.querySelectorAll("a")].find((item) => item.getAttribute("href") === "/usage");
  nav.insertBefore(button, usageLink || null);
  const mobileButton = document.createElement("button");
  mobileButton.id = "memory-mobile-open";
  mobileButton.className = "memory-top-button";
  mobileButton.type = "button";
  mobileButton.setAttribute("aria-haspopup", "dialog");
  mobileButton.setAttribute("aria-controls", "memory-modal");
  mobileButton.innerHTML = '<span aria-hidden="true">◈</span><span>记忆</span><b id="memory-mobile-badge" data-zero="true">—</b>';
  const topActions = document.querySelector(".top-actions");
  topActions?.insertBefore(mobileButton, topActions.querySelector(".notification-menu"));

  const modal = document.createElement("div");
  modal.id = "memory-modal";
  modal.className = "modal-backdrop hidden";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("aria-labelledby", "memory-modal-title");
  modal.innerHTML = `<div class="modal memory-modal"><div class="modal-head"><div><h2 id="memory-modal-title">我的记忆</h2><p>只有“已确认”的内容会影响 Agent。你可以随时修改、忽略或彻底删除。</p></div><button class="icon-button" data-close-memory type="button" aria-label="关闭我的记忆">×</button></div><div id="memory-summary" class="memory-summary" aria-live="polite"><span>正在读取…</span></div><form id="memory-create-form" class="memory-create-form"><label>新增一条明确记忆<textarea id="memory-create-content" rows="2" required maxlength="1000" placeholder="例如：回答默认用中文，先说结论。"></textarea></label><div class="memory-create-grid"><label>范围<select id="memory-create-scope"><option value="global">整个工作台</option><option value="project">指定项目</option></select></label><label>项目 ID（项目记忆才需要）<input id="memory-create-project" maxlength="80" placeholder="例如 market" /></label><label>类型<select id="memory-create-kind"><option value="preference">偏好</option><option value="constraint">边界</option><option value="routine">习惯</option><option value="decision">决策</option><option value="profile">个人信息</option></select></label><button class="primary-button" type="submit">确认并记住</button></div></form><div class="memory-toolbar"><label>查看<select id="memory-filter"><option value="active">有效记忆</option><option value="candidate">待确认</option><option value="confirmed">已确认</option><option value="all">全部</option></select></label><div><button id="memory-import" class="secondary-button" type="button">预览已有偏好</button><button id="memory-refresh" class="secondary-button" type="button">刷新</button></div></div><p id="memory-message" class="modal-message" role="status" aria-live="polite"></p><details id="memory-hygiene" class="memory-hygiene"><summary>记忆体检 <small>哪些记忆在拖后腿</small></summary><div id="memory-hygiene-body"><p class="memory-empty">展开后开始检查。</p></div></details><div id="memory-list" class="memory-list" aria-live="polite"><div class="memory-empty">正在读取记忆…</div></div></div>`;
  document.body.append(modal);
  const summary = modal.querySelector("#memory-summary"), list = modal.querySelector("#memory-list"), status = modal.querySelector("#memory-message"), filter = modal.querySelector("#memory-filter"), badges = [document.querySelector("#memory-nav-badge"), document.querySelector("#memory-mobile-badge")].filter(Boolean);
  const close = () => { modal.classList.add("hidden"); const previous = document.getElementById(modal.dataset.prevFocus || ""); (previous || button).focus(); };
  const setStatus = (text, error = false) => { status.textContent = text; status.classList.toggle("error", error); status.setAttribute("role", error ? "alert" : "status"); };
  const option = (value, current, label) => `<option value="${value}"${value === current ? " selected" : ""}>${label}</option>`;
  const renderItems = (items = []) => {
    list.innerHTML = items.length ? items.map((item) => `<article class="memory-item" data-memory-id="${escapeHtml(item.id)}"><div class="memory-item-head"><div><span class="memory-status status-${escapeHtml(item.status)}">${escapeHtml(item.status_label)}</span><span class="memory-kind">${escapeHtml(item.kind_label)}</span>${item.pinned ? '<span class="memory-pinned">置顶</span>' : ""}</div><small>${escapeHtml(item.scope === "global" ? "整个工作台" : `项目 · ${item.project_id || "未指定"}`)}</small></div><textarea class="memory-item-content" rows="2" maxlength="1000" aria-label="记忆内容">${escapeHtml(item.content)}</textarea><div class="memory-item-fields"><label>范围<select data-memory-scope>${option("global", item.scope, "整个工作台")}${option("project", item.scope, "指定项目")}</select></label><label>项目<input data-memory-project maxlength="80" value="${escapeHtml(item.project_id || "")}" placeholder="项目 ID" /></label><label>类型<select data-memory-kind>${option("preference", item.kind, "偏好")}${option("constraint", item.kind, "边界")}${option("routine", item.kind, "习惯")}${option("decision", item.kind, "决策")}${option("profile", item.kind, "个人信息")}</select></label><label class="memory-pin"><input data-memory-pinned type="checkbox"${item.pinned ? " checked" : ""} /> 置顶</label></div><div class="memory-item-foot"><small>可信度 ${Math.round(Number(item.confidence || 0) * 100)}% · 使用 ${Number(item.use_count || 0)} 次${item.source_type ? ` · 来源 ${escapeHtml(item.source_type)}` : ""}</small><div class="memory-actions"><button class="secondary-button" data-memory-action="save" type="button">保存修改</button>${item.status === "candidate" ? '<button class="primary-button" data-memory-action="confirm" type="button">确认</button><button class="secondary-button" data-memory-action="reject" type="button">忽略</button>' : ""}<button class="memory-delete" data-memory-action="delete" type="button">删除</button></div></div></article>`).join("") : '<div class="memory-empty">当前筛选下没有记忆。你也可以直接说“记住：以后都用中文”。</div>';
  };
  async function loadMemories() {
    setStatus("正在读取记忆…");
    const body = await workbenchRequestJson(`/api/memories?status=${encodeURIComponent(filter.value)}&limit=300`);
    const info = body.summary || {};
    summary.innerHTML = `<span><strong>${info.confirmed || 0}</strong> 已确认</span><span><strong>${info.candidate || 0}</strong> 待确认</span><span><strong>${info.global || 0}</strong> 全局</span><span><strong>${info.project || 0}</strong> 项目</span>`;
    badges.forEach((badge) => { badge.textContent = info.candidate || info.confirmed || 0; badge.dataset.zero = String(!(info.candidate || info.confirmed)); });
    renderItems(body.items || []);
    setStatus(body.policy || "记忆已更新。");
    return body;
  }
  // 记忆体检的接口（/api/memories/hygiene）早就写好了，但整个前端一个调用点
  // 都没有——每轮只有 5 条能进上下文，池子越大真正相关的越容易被挤掉，
  // 而"哪些记忆在白占名额"这件事此前只能 curl 才看得到。
  const hygiene = modal.querySelector("#memory-hygiene");
  const hygieneBody = modal.querySelector("#memory-hygiene-body");
  const hygieneGroup = (title, hint, items) => items.length
    ? `<div class="memory-hygiene-group"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(hint)}</small>${items.map((item) => `<label class="memory-hygiene-item"><input type="checkbox" value="${escapeHtml(item.id)}" /><span>${escapeHtml(item.content)}</span><small>${item.use_count ? `用过 ${escapeHtml(item.use_count)} 次` : "从未被用到"}</small></label>`).join("")}</div>`
    : "";
  async function loadHygiene() {
    hygieneBody.innerHTML = '<p class="memory-empty">正在检查…</p>';
    try {
      const body = await workbenchRequestJson("/api/memories/hygiene?limit=40");
      const groups = hygieneGroup("从没被用过", "确认很久却一次都没被检索命中，多半是当初随手确认的", body.never_used || [])
        + hygieneGroup("很久没用了", `用过但超过 ${body.stale_days || 30} 天没再用，可能是已经结束的阶段性信息`, body.idle || []);
      hygieneBody.innerHTML = groups
        ? `${groups}<div class="memory-hygiene-actions"><button type="button" id="memory-archive" class="secondary-button">归档选中的</button><small>归档不是删除：记忆仍在库里，只是不再进入 Agent 上下文。</small></div>`
        : `<p class="memory-empty">没有需要处理的。${escapeHtml(body.policy || "")}</p>`;
    } catch (error) {
      hygieneBody.innerHTML = `<p class="memory-empty">体检失败：${escapeHtml(error.message)}</p>`;
    }
  }
  hygiene.addEventListener("toggle", () => { if (hygiene.open) void loadHygiene(); });
  hygieneBody.addEventListener("click", async (event) => {
    if (!event.target.closest("#memory-archive")) return;
    const ids = [...hygieneBody.querySelectorAll("input[type=checkbox]:checked")].map((box) => box.value);
    if (!ids.length) { setStatus("先勾选要归档的记忆。", true); return; }
    const archiveButton = event.target.closest("#memory-archive");
    archiveButton.disabled = true;
    try {
      await workbenchRequestJson("/api/memories/archive", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ memory_ids: ids }) });
      // 先刷新，再写提示：loadMemories 结尾会把状态行改成策略说明，
      // 顺序反了的话「已归档 N 条」这句刚出现就被盖掉。
      await Promise.all([loadHygiene(), loadMemories()]);
      setStatus(`已归档 ${ids.length} 条，它们不再进入 Agent 上下文。`);
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      archiveButton.disabled = false;
    }
  });

  const open = async (event) => { modal.classList.remove("hidden"); modal.dataset.prevFocus = document.activeElement?.id || event.currentTarget?.id || "memory-open"; try { await loadMemories(); modal.querySelector("#memory-create-content").focus(); } catch (error) { setStatus(error.message, true); } };
  button.addEventListener("click", open);
  mobileButton.addEventListener("click", open);
  modal.querySelector("[data-close-memory]").addEventListener("click", close);
  modal.addEventListener("click", (event) => { if (event.target === modal) close(); });
  modal.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); if (event.key !== "Tab") return; const focusable = [...modal.querySelectorAll("button, input, select, textarea")].filter((item) => !item.disabled && !item.hidden); if (!focusable.length) return; const first = focusable[0], last = focusable[focusable.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } });
  filter.addEventListener("change", () => loadMemories().catch((error) => setStatus(error.message, true)));
  modal.querySelector("#memory-refresh").addEventListener("click", () => loadMemories().catch((error) => setStatus(error.message, true)));
  modal.querySelector("#memory-create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.submitter, content = modal.querySelector("#memory-create-content"), scope = modal.querySelector("#memory-create-scope"), project = modal.querySelector("#memory-create-project"), kind = modal.querySelector("#memory-create-kind");
    submit.disabled = true; setStatus("正在保存…");
    try {
      await workbenchRequestJson("/api/memories", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: content.value.trim(), scope: scope.value, project_id: project.value.trim(), kind: kind.value, status: "confirmed", confidence: 1 }) });
      content.value = ""; await loadMemories(); setStatus("已经记住，并会在相关对话中使用。");
    } catch (error) { setStatus(error.message, true); } finally { submit.disabled = false; }
  });
  list.addEventListener("click", async (event) => {
    const actionButton = event.target.closest("[data-memory-action]");
    if (!actionButton) return;
    const card = actionButton.closest("[data-memory-id]"), id = card?.dataset.memoryId, action = actionButton.dataset.memoryAction;
    if (!id) return;
    if (action === "delete" && !window.confirm("确定彻底删除这条记忆吗？删除后不会再用于 Agent。")) return;
    actionButton.disabled = true; setStatus("正在更新记忆…");
    try {
      if (action === "save") {
        await workbenchRequestJson(`/api/memories/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: card.querySelector(".memory-item-content").value.trim(), scope: card.querySelector("[data-memory-scope]").value, project_id: card.querySelector("[data-memory-project]").value.trim(), kind: card.querySelector("[data-memory-kind]").value, pinned: card.querySelector("[data-memory-pinned]").checked }) });
      } else if (action === "delete") {
        await workbenchRequestJson(`/api/memories/${encodeURIComponent(id)}`, { method: "DELETE" });
      } else {
        await workbenchRequestJson(`/api/memories/${encodeURIComponent(id)}/${action}`, { method: "POST" });
      }
      await loadMemories(); setStatus(action === "delete" ? "记忆已彻底删除。" : action === "confirm" ? "记忆已确认，之后会用于相关对话。" : action === "reject" ? "已忽略这条候选记忆。" : "修改已保存。");
    } catch (error) { actionButton.disabled = false; setStatus(error.message, true); }
  });
  modal.querySelector("#memory-import").addEventListener("click", async (event) => {
    const importButton = event.currentTarget;
    importButton.disabled = true; setStatus("正在读取已有偏好预览…");
    try {
      const preview = await workbenchRequestJson("/api/memories-import/workbuddy");
      const lines = (preview.items || []).map((item) => `• ${item.content}`).join("\n");
      if (!lines) { setStatus("没有找到可导入的用户偏好。"); return; }
      if (!window.confirm(`只会导入以下“用户偏好”，不会导入服务器或部署信息：\n\n${lines}\n\n确认导入吗？`)) { setStatus("已取消导入，没有写入任何内容。"); return; }
      const body = await workbenchRequestJson("/api/memories-import/workbuddy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmed: true }) });
      await loadMemories(); setStatus(`已导入 ${body.items?.length || 0} 条已有偏好。`);
    } catch (error) { setStatus(error.message, true); } finally { importButton.disabled = false; }
  });
}
function setupGlobalSettings() {
  window.WorkbenchLLMSettings?.init?.();
}
document.querySelectorAll("[data-group]").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll("[data-group]").forEach((item) => item.classList.remove("active")); button.classList.add("active"); activeGroup = button.dataset.group; renderProjects(); })); $("#project-search").addEventListener("input", renderProjects); $("#add-project").addEventListener("click", () => openAddProjectModal()); $("#restore-project-layout")?.addEventListener("click", async () => { const button = $("#restore-project-layout"); button.disabled = true; button.textContent = "恢复中…"; try { const body = await workbenchRequestJson("/api/projects/preferences/reset", { method: "POST" }); hiddenProjectIds = new Set(); projects = body.projects || []; renderProjects(); $("#project-note").textContent = "已恢复默认顺序、分组和可见项目"; } catch (error) { $("#project-note").textContent = error.message; } finally { button.disabled = false; button.textContent = "恢复默认布局"; } }); setupProjectInteractions(); setupGlobalSettings(); setupGlobalAgentV2(); setupMemoryCenter(); setupWorkItems(); setupNotifications(); setupProjectAudit(); setupTraceCenter(); setupPushPanel(); setupCommandPalette(); setupPwaInstall(); loadProjects(); loadWorkItems(); void loadAppVersion(); void loadApprovalQueue();
