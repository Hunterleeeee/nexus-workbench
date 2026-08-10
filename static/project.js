const qs = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function formatDate(value) {
  if (!value) return "刚刚";
  return new Date(value).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function setupThemeToggle() {
  if (!document.querySelector("link[data-workbench-theme]")) { const link = document.createElement("link"); link.rel = "stylesheet"; link.href = "/static/theme.css?v=0.3.140"; link.dataset.workbenchTheme = "true"; document.head.append(link); }
  const actions = document.querySelector(".page-actions, .actions");
  if (!actions || actions.querySelector("[data-theme-toggle]")) return;
  // 页面已显式声明 data-theme（如深色设计页）时尊重页面，不覆盖；
  // 否则沿用用户偏好（默认浅色）。
  if (!document.documentElement.hasAttribute("data-theme")) {
    const saved = localStorage.getItem("workbench-theme") || "dark";
    document.documentElement.dataset.theme = saved;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "theme-toggle";
  button.dataset.themeToggle = "true";
  button.title = "切换浅色/深色主题";
  const render = () => { const dark = document.documentElement.dataset.theme === "dark"; button.textContent = dark ? "浅色" : "深色"; button.setAttribute("aria-label", dark ? "切换浅色主题" : "切换深色主题"); };
  button.addEventListener("click", () => { const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; localStorage.setItem("workbench-theme", next); render(); });
  actions.append(button);
  render();
}

const PROJECT_AGENT_QUICK_ACTIONS = {
  inbox: ["整理当前待处理", "找出今天最重要的一条", "把最近一条交给合适项目"],
  knowledge: ["搜索与当前工作相关的笔记", "找出今天更新的 Obsidian 笔记", "整理收件箱中的沉淀候选", "检查 Obsidian 是否有相关内容"],
  "doc-factory": ["检查当前材料是否完整", "推荐最合适的文档模板", "生成一份结论优先的文档"],
  sub2api: ["解释本周额度和剩余时间", "检查账户数据是否过期", "列出需要我确认的账户动作"],
  market: ["解释自选股今日涨跌", "检查行情数据是否新鲜", "基于当前自选给出研究观察"],
  server: ["做一次只读健康判断", "解释当前是否需要处理", "按风险列出排查顺序"],
  crawl4ai: ["总结最近一次研究的结论", "标出证据最薄弱的地方", "把结果整理成笔记提纲"],
  aihot: ["筛出今天最值得继续研究的热点", "解释这条热点为什么值得关注", "把机会线索交给想法分析"],
  "idea-analysis": ["梳理当前想法的关键假设", "生成 7 天最小验证计划", "根据已有证据比较继续、暂停还是转向"],
  "cid-dashboard": ["比较最近看板中的项目机会", "找出最值得继续研究的项目", "把当前机会整理成验证任务"],
};

// requestJson 统一由 /static/request.js 暴露为 window.requestJson，
// 本文件不再顶层声明，避免与 app.js（/crawl4ai 页面同载）重复声明冲突。
// 页面内联脚本调用 requestJson(...) 时解析到 window.requestJson。

// Shared page feedback for the domain pages.  A failed read should leave the
// user with a recoverable action, not a dead loading placeholder.
function wbSetBusy(button, busy, busyLabel = "处理中…") {
  if (!button) return;
  if (busy) {
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent.trim();
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = busyLabel;
    return;
  }
  button.disabled = false;
  button.removeAttribute("aria-busy");
  if (button.dataset.idleLabel) {
    button.textContent = button.dataset.idleLabel;
    delete button.dataset.idleLabel;
  }
}

function wbRetryMarkup(message, retryLabel = "重新加载") {
  return `<div class="wb-retry-state" role="alert"><strong>暂时无法读取</strong><p>${escapeHtml(message || "服务暂时不可用")}</p><button type="button" class="secondary-button wb-retry-button" data-wb-retry>${escapeHtml(retryLabel)}</button></div>`;
}

function wbShowRetry(host, message, retryLabel = "重新加载") {
  if (!host) return;
  host.innerHTML = wbRetryMarkup(message, retryLabel);
}

window.WorkbenchUX = { requestJson, wbSetBusy, wbRetryMarkup, wbShowRetry };

function ensureGlobalSettingsEntry() {
  if (document.querySelector("#global-settings-button, [data-global-settings-link], .global-button")) return;
  const actions = document.querySelector(".page-actions, .actions");
  if (!actions) return;
  const link = document.createElement("a");
  link.href = "/?settings=llm";
  link.target = "_blank";
  link.rel = "noopener";
  link.dataset.globalSettingsLink = "true";
  link.className = actions.matches(".actions") ? "button" : "global-button";
  link.innerHTML = actions.matches(".actions") ? "全局 LLM ↗" : '<span class="dot"></span>全局 LLM ↗';
  actions.append(link);
}

function setupGlobalSettings() {
  window.WorkbenchLLMSettings?.init?.();
}

function projectIdFromPage() {
  const explicit = document.body?.dataset.projectId;
  if (explicit) return explicit;
  const path = window.location.pathname;
  const match = path.match(/^\/projects\/([^/]+)/);
  if (match) return match[1];
  if (path === "/crawl4ai") return "crawl4ai";
  return "";
}

function agentActionMarkup(actions = []) {
  return actions.map((action) => {
    const label = action.name || action.tool || "Agent 动作";
    const status = action.status === "executed" ? "已执行" : action.status === "pending" ? "待确认" : "失败";
    return `<span class="project-agent-action ${escapeHtml(action.status || "failed")}"><b>${status}</b>${escapeHtml(label)}</span>`;
  }).join("");
}

function agentExecutionPlanTrace(plan = {}) {
  if (!plan || !plan.kind) return "";
  const statusLabels = { queued: "排队中", running: "执行中", completed: "已完成", succeeded: "已完成", partial: "部分完成", failed: "失败" };
  const targets = Array.isArray(plan.targets) ? plan.targets.join("、") : plan.target || "自动目标";
  const tools = Array.isArray(plan.requested_tools) ? plan.requested_tools.join("、") : "";
  const children = Array.isArray(plan.child_run_ids) ? plan.child_run_ids.length : 0;
  const steps = Array.isArray(plan.steps) ? plan.steps.filter((step) => step?.status === "completed").length : 0;
  const flags = [statusLabels[plan.status] || plan.status || "已记录", plan.needs_confirmation ? "需要你确认交给谁" : "自动处理", children ? `子任务 ${children} 个` : "", steps ? `步骤 ${steps}/${plan.steps.length}` : ""].filter(Boolean).join(" · ");
  const intent = plan.intent ? `意图：${escapeHtml(plan.intent)} · ` : "";
  return `${intent}执行计划：${escapeHtml(targets)} · ${escapeHtml(flags)} · 路由 ${escapeHtml(plan.route_mode || "自动")} · 置信度 ${Math.round(Number(plan.route_confidence || 0) * 100)}%${tools ? ` · 工具约束 ${escapeHtml(tools)}` : ""}`;
}
function agentResultContractMarkup(contract = {}) {
  const sections = contract?.sections || {};
  const labels = { facts: "事实", judgement: "判断", evidence: "证据", risks: "风险", actions: "动作", next_steps: "下一步" };
  const entries = Object.entries(labels).filter(([key]) => Array.isArray(sections[key]) && sections[key].length);
  if (!contract?.summary && !entries.length) return "";
  const body = entries.map(([key, label]) => `<div><strong>${label}</strong><ul>${sections[key].slice(0, 8).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`).join("");
  const citations = (contract.citations || []).slice(0, 8).map((item) => item.type === "url" ? `<a href="${escapeHtml(item.value)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.label || item.value)}</a>` : `<span>${escapeHtml(item.value)}</span>`).join(" · ");
  const refs = (contract.source_refs || []).slice(0, 8).map((item) => { const label = `${item.label || item.id || "未命名来源"}${item.data_as_of ? ` · ${item.data_as_of}` : ""}`; return String(item.locator || "").startsWith("http") ? `<a href="${escapeHtml(item.locator)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>` : `<span>${escapeHtml(label)}</span>`; }).join(" · ");
  const review = contract.needs_review ? `<span class="agent-contract-review">需复核：${escapeHtml((contract.review_reasons || []).join("、") || "证据不足")}</span>` : "";
  const coverage = contract.source_coverage || {};
  const coverageText = coverage.total ? `引用覆盖：${coverage.with_locator || 0}/${coverage.total} 可定位 · ${coverage.with_data_time || 0}/${coverage.total} 有数据时间` : "";
  const plan = contract.execution_plan || {};
  const planTrace = agentExecutionPlanTrace(plan);
  const trace = [contract.data_as_of ? `数据时间：${escapeHtml(contract.data_as_of)}` : "", refs ? `来源：${refs}` : "", coverageText, planTrace, contract.artifact_ids?.length ? `Artifact ${contract.artifact_ids.length} 份` : "", contract.work_item_ids?.length ? `WorkItem ${contract.work_item_ids.length} 条` : "", contract.relation_ids?.length ? `Relation ${contract.relation_ids.length} 条` : "", review, contract.replay?.href ? `<a href="${escapeHtml(contract.replay.href)}" target="_blank" rel="noopener noreferrer">查看 Run 回放</a>` : ""].filter(Boolean).join(" · ");
  return `<details class="project-agent-result-contract"><summary>结构化结果 · ${escapeHtml(contract.summary || "查看结论与证据")}</summary>${body || `<p>${escapeHtml(contract.summary || "暂无结构化摘要")}</p>`}${citations ? `<div class="project-agent-citations"><strong>可回溯来源</strong><p>${citations}</p></div>` : ""}${trace ? `<div class="project-agent-citations"><strong>审计链</strong><p>${trace}</p></div>` : ""}</details>`;
}

function setupIncomingHandoffQueue(projectId, host) {
  if (!host || host.querySelector("[data-incoming-work-items]")) return;
  const section = document.createElement("section");
  section.className = "project-agent-incoming";
  section.dataset.incomingWorkItems = "true";
  section.innerHTML = '<div class="project-agent-incoming-head"><strong>待我处理</strong><span data-incoming-summary>读取中…</span></div><div data-incoming-list><div class="project-agent-runs-empty">正在读取其他项目交给我的事项…</div></div>';
  host.appendChild(section);
  const list = section.querySelector("[data-incoming-list]");
  const summary = section.querySelector("[data-incoming-summary]");
  const render = (items = []) => {
    const actionable = items.filter((item) => ["open", "blocked", "failed"].includes(item.status));
    section.classList.toggle("is-empty", !actionable.length);
    summary.textContent = actionable.length ? `${actionable.length} 条待接收` : "暂无待我处理";
    if (!actionable.length) { list.innerHTML = '<div class="project-agent-runs-empty">没有需要这个 Agent 处理的交接。</div>'; return; }
    list.innerHTML = actionable.slice(0, 4).map((item) => `<article class="project-agent-incoming-item"><div><strong>${escapeHtml(item.title || "未命名工作项")}</strong><p>${escapeHtml(item.description || "没有描述")}</p><small>${escapeHtml(item.source_agent_name || item.source_project || "工作台")} · ${escapeHtml(item.status === "failed" ? "上次失败" : item.status === "blocked" ? "等待处理" : "待接收")}</small></div><button type="button" data-incoming-run="${escapeHtml(item.id)}">${item.status === "failed" ? "重试" : "交给 Agent"}</button></article>`).join("");
  };
  const load = async () => { const body = await requestJson(`/api/agent/${encodeURIComponent(projectId)}/work-items?status=all&limit=12`); render(body.items || []); };
  list.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-incoming-run]");
    if (!button) return;
    button.disabled = true; button.setAttribute("aria-busy", "true"); button.textContent = "处理中…";
    try {
      const body = await requestJson(`/api/agent/${encodeURIComponent(projectId)}/work-items/${encodeURIComponent(button.dataset.incomingRun)}/run`, { method: "POST" });
      const answer = body.message?.content || body.answer || "交接已处理。";
      const chat = document.querySelector(projectId === "idea-analysis" ? "#idea-chat-log" : "#chat-log");
      if (chat) { chat.insertAdjacentHTML("beforeend", `<div class="${projectId === "idea-analysis" ? "idea-message" : "chat-message"} assistant"><strong>${escapeHtml(body.agent?.name || "项目 Agent")}</strong><p>${escapeHtml(answer)}</p></div>`); chat.scrollTop = chat.scrollHeight; }
      await load();
    } catch (error) { button.disabled = false; button.removeAttribute("aria-busy"); button.textContent = "重试"; summary.textContent = error.message; }
    button.removeAttribute("aria-busy");
  });
  load().catch((error) => { summary.textContent = "读取失败"; list.innerHTML = `<div class="project-agent-runs-empty">${escapeHtml(error.message)}</div>`; });
}

function setupProjectHandoff(projectId) {
  if (document.querySelector("[data-project-handoff]")) return;
  const host = document.querySelector(".project-agent-runs, .agent-panel, .conversation-panel");
  if (!host) return;
  setupIncomingHandoffQueue(projectId, document.querySelector(".agent-panel, .conversation-panel") || host);
  const section = document.createElement("section");
  section.className = "project-agent-handoff project-agent-handoff-embedded";
  section.dataset.projectHandoff = "true";
  section.innerHTML = '<div><strong>转交给其他项目</strong><small data-project-handoff-links>读取中…</small></div><div class="project-agent-handoff-row"><select data-project-handoff-target aria-label="选择要转交的项目"><option value="">选择要转交的项目</option></select><button data-project-handoff-button type="button">转交</button></div><small data-project-handoff-status>转交前会先预览目标，确认后才创建事项。</small>';
  host.insertAdjacentElement("afterend", section);
  const target = section.querySelector("[data-project-handoff-target]");
  const button = section.querySelector("[data-project-handoff-button]");
  const linksNote = section.querySelector("[data-project-handoff-links]");
  const status = section.querySelector("[data-project-handoff-status]");
  let pending = false;
  const reset = () => { pending = false; button.classList.remove("pending"); button.textContent = "转交"; };
  const latestAnswer = () => {
    const nodes = document.querySelectorAll("#chat-log .chat-message.assistant p, #idea-chat-log .idea-message.assistant p");
    return nodes.length ? nodes[nodes.length - 1].textContent.trim() : "";
  };
  requestJson(`/api/agent/${encodeURIComponent(projectId)}/sessions`).then((body) => {
    const outbound = body.agent?.links?.outbound || [];
    linksNote.textContent = outbound.length ? `可交给 ${outbound.map((link) => link.label).join("、")}` : "当前没有配置出站联动";
    target.innerHTML = '<option value="">选择要转交的项目</option>' + outbound.map((link) => `<option value="${escapeHtml(link.to)}">${escapeHtml(link.label)}</option>`).join("");
    button.disabled = !outbound.length;
  }).catch((error) => { linksNote.textContent = `读取失败：${error.message}`; button.disabled = true; });
  target.addEventListener("change", reset);
  button.addEventListener("click", async () => {
    const toProject = target.value;
    const answer = latestAnswer();
    if (!toProject || !answer || /^(你可以问我：|把你的想法丢过来吧)/.test(answer)) { status.textContent = "先完成一次 Agent 分析，再选择要转交的项目。"; return; }
    const targetLabel = target.selectedOptions[0]?.textContent || toProject;
    if (!pending) {
      pending = true; button.classList.add("pending"); button.textContent = "确认交接"; status.textContent = `将把这次分析交给 ${targetLabel}，确认后会创建可追踪工作项。`; return;
    }
    button.disabled = true;
    try {
      await requestJson("/api/handoffs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: `${document.title.replace(" · Workbench", "")} 后续任务`, description: answer, from_project: projectId, to_project: toProject, confirmed: true, metadata: { source: "custom_project_agent" } }) });
      status.textContent = `已交给 ${targetLabel}，工作台已记录联动任务。`;
    } catch (error) { status.textContent = error.message; } finally { button.disabled = false; reset(); }
  });
}

function setupProjectAgent() {
  const projectId = projectIdFromPage();
  if (!projectId || projectId === "workbench") return;
  // These projects already have a domain-specific Agent layout. They still
  // use the same global settings and handoff APIs, but should not show two chat boxes.
  if (document.querySelector("#aihot-chat-form, #idea-chat-form")) {
    setupProjectHandoff(projectId);
    // 顶部统一入口：点击滚动到内嵌 Agent 面板并聚焦输入框，与其他项目页体验一致。
    const topActions = document.querySelector(".page-actions, .topbar .actions, .bar .bar-right");
    const agentHost = document.querySelector("#aihot-chat-form, #idea-chat-form");
    if (topActions && agentHost) {
      const anchor = document.createElement("button");
      anchor.type = "button";
      anchor.className = "project-agent-launcher project-agent-launcher-top";
      anchor.innerHTML = '<span class="project-agent-launcher-dot"></span><span>问这个项目的 AI</span><small>去提问</small>';
      anchor.addEventListener("click", () => {
        agentHost.scrollIntoView({ behavior: "smooth", block: "center" });
        const input = agentHost.querySelector("textarea");
        window.setTimeout(() => input?.focus(), 350);
      });
      topActions.prepend(anchor);
    }
    if (new URLSearchParams(window.location.search).get("focus") === "agent") {
      window.setTimeout(() => {
        const input = document.querySelector("#aihot-chat-input, #idea-chat-input");
        input?.scrollIntoView({ behavior: "smooth", block: "center" });
        input?.focus();
      }, 80);
    }
    return;
  }
  const launcher = document.createElement("button");
  // 覆盖三种项目页顶部结构：标准 page-actions、Sub2API 的 topbar>.actions、CID 的 bar>.bar-right。
  const topActions = document.querySelector(".page-actions, .topbar .actions, .bar .bar-right");
  if (topActions) {
    // 显眼入口：放进项目页顶部操作区，普通用户一进页面就能看到。
    launcher.className = "project-agent-launcher project-agent-launcher-top";
    launcher.setAttribute("aria-expanded", "false");
    launcher.innerHTML = '<span class="project-agent-launcher-dot"></span><span>问这个项目的 AI</span><small>打开</small>';
    topActions.prepend(launcher);
  } else {
    // 兜底：右下角悬浮球。
    launcher.className = "project-agent-launcher";
    launcher.setAttribute("aria-expanded", "false");
    launcher.innerHTML = '<span class="project-agent-launcher-dot"></span><span>项目 Agent</span><small>打开</small>';
    document.body.append(launcher);
  }
  const panel = document.createElement("section");
  panel.className = "project-agent-panel hidden";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "项目 Agent");
  panel.innerHTML = `<header class="project-agent-head"><div><span class="project-agent-kicker">PROJECT AGENT</span><h2>项目 Agent</h2><p id="project-agent-subtitle">正在读取项目能力…</p></div><button class="project-agent-close" type="button" aria-label="关闭项目 Agent">×</button></header><div class="project-agent-toolbar"><select id="project-agent-sessions" aria-label="选择 Agent 会话"><option value="">新会话</option></select><button id="project-agent-new" type="button">新会话</button></div><div id="project-agent-capability" class="project-agent-capability"></div><div id="project-agent-quick-actions" class="project-agent-quick-actions" aria-label="项目快捷提问"></div><section class="project-agent-incoming" aria-label="待我处理"><div class="project-agent-incoming-head"><strong>待我处理</strong><span id="project-agent-incoming-summary">读取中…</span></div><div id="project-agent-incoming-list"><div class="project-agent-runs-empty">打开面板后显示其他项目交给我处理的事项</div></div></section><section class="project-agent-runs" aria-label="最近 Agent 运行"><div class="project-agent-runs-head"><strong>最近运行</strong><span id="project-agent-runs-summary">读取中…</span></div><div id="project-agent-runs-list"><div class="project-agent-runs-empty">打开面板后显示执行记录</div></div><div id="project-agent-run-detail" class="project-agent-run-detail" aria-live="polite" hidden></div></section><div id="project-agent-messages" class="project-agent-messages" role="log" aria-live="polite"><div class="project-agent-empty">这是这个项目自己的 Agent。它会读取本项目上下文，并保留本地会话。</div></div><form id="project-agent-form" class="project-agent-form"><textarea id="project-agent-input" rows="3" placeholder="问这个项目的 Agent…（Enter 发送，Shift+Enter 换行）"></textarea><div class="project-agent-form-foot"><span id="project-agent-message">全局 LLM · 会话保存在本机</span><button type="submit">发送</button></div></form><footer class="project-agent-handoff"><div><strong>转交给其他项目</strong><small id="project-agent-links">读取中…</small></div><div class="project-agent-handoff-row"><select id="project-agent-target" aria-label="选择要转交的项目"><option value="">选择要转交的项目</option></select><button id="project-agent-handoff-button" type="button">转交</button></div></footer>`;
  // 面板固定挂到 body；launcher 已在 page-actions 里（或独立悬浮球）时不再重复挂载。
  document.body.append(panel);

  const close = panel.querySelector(".project-agent-close");
  const sessionSelect = panel.querySelector("#project-agent-sessions");
  const messages = panel.querySelector("#project-agent-messages");
  const form = panel.querySelector("#project-agent-form");
  const input = panel.querySelector("#project-agent-input");
  const status = panel.querySelector("#project-agent-message");
  const capability = panel.querySelector("#project-agent-capability");
  const quickActions = panel.querySelector("#project-agent-quick-actions");
  const incomingList = panel.querySelector("#project-agent-incoming-list");
  const incomingSummary = panel.querySelector("#project-agent-incoming-summary");
  const runsList = panel.querySelector("#project-agent-runs-list");
  const runDetail = panel.querySelector("#project-agent-run-detail");
  const runsSummary = panel.querySelector("#project-agent-runs-summary");
  const targetSelect = panel.querySelector("#project-agent-target");
  const handoffButton = panel.querySelector("#project-agent-handoff-button");
  let agent = null;
  let sessionId = "";
  let currentSession = null;
  let lastAnswer = "";
  let handoffConfirmationPending = false;
  status?.setAttribute("role", "status"); status?.setAttribute("aria-live", "polite");
  if (status && window.MutationObserver) new MutationObserver(() => { const isError = /失败|错误|无法|不可|请求失败/.test(status.textContent || ""); status.setAttribute("role", isError ? "alert" : "status"); status.setAttribute("aria-live", isError ? "assertive" : "polite"); }).observe(status, { childList: true, characterData: true, subtree: true });

  function setOpen(open) {
    panel.classList.toggle("hidden", !open);
    launcher.setAttribute("aria-expanded", String(open));
    if (open) {
      // 打开面板时锁定背景滚动，避免触屏/滚轮把页面背景一起带跑（滑动 Bug 修复）。
      document.body.dataset.agentPanelOpen = "open";
      panel.classList.add("is-open");
      input.focus();
    } else {
      delete document.body.dataset.agentPanelOpen;
      panel.classList.remove("is-open");
    }
  }
  function renderMessages(items = []) {
    messages.innerHTML = items.length ? items.map((item) => `<div class="project-agent-message ${item.role === "user" ? "user" : "assistant"}"><strong>${item.role === "user" ? "你" : escapeHtml(agent?.name || "项目 Agent")}</strong><p>${escapeHtml(item.content)}</p>${item.role !== "user" ? agentResultContractMarkup(item.metadata?.result_contract) : ""}${item.metadata?.actions ? `<div class="project-agent-inline-actions">${agentActionMarkup(item.metadata.actions)}</div>` : ""}</div>`).join("") : '<div class="project-agent-empty">这是这个项目自己的 Agent。它会读取本项目上下文，并保留本地会话。</div>';
    messages.scrollTop = messages.scrollHeight;
    const latest = [...items].reverse().find((item) => item.role === "assistant");
    lastAnswer = latest?.content || "";
  }
  function renderCapability(detail, links) {
    agent = detail || agent;
    const outbound = links?.outbound || [];
    const permission = detail?.tool_permission_summary || {};
    const permissionText = `工具 ${permission.readonly || 0} 只读 · ${permission.auto || 0} 自动 · ${permission.restricted || 0} 受限 · ${permission.confirm || 0} 需确认`;
    const implemented = detail?.implemented_tools || [];
    const gaps = detail?.gaps || [];
    const capabilityCount = implemented.length;
    const gapText = gaps.length ? `${gaps.length} 个缺口` : "暂无登记缺口";
    capability.innerHTML = `<div class="project-agent-capability-main"><span class="project-agent-status">${escapeHtml(detail?.status_label || "已接入")}</span><span class="project-agent-capability-copy">${escapeHtml((detail?.mission || "读取项目上下文并给出可执行结果").slice(0, 100))}</span></div><div class="project-agent-capability-meta"><span>${capabilityCount} 项已具备 · ${escapeHtml(gapText)}</span><span>${escapeHtml(permissionText)}</span></div><details class="project-agent-capability-details"><summary>查看能力边界</summary><div><strong>已具备</strong><p>${escapeHtml(implemented.join("、") || "暂无")}</p></div><div><strong>下一轮</strong><p>${escapeHtml(gaps.join("、") || "继续根据运行记录优化")}</p></div></details>`;
    quickActions.innerHTML = (PROJECT_AGENT_QUICK_ACTIONS[projectId] || []).map((prompt) => `<button type="button" data-agent-quick-prompt="${escapeHtml(prompt)}">${escapeHtml(prompt)}</button>`).join("");
    panel.querySelector("#project-agent-subtitle").textContent = `${detail?.name || "项目 Agent"} · ${detail?.llm_ready === false ? "等待全局 LLM" : "本地会话"}`;
    panel.querySelector("#project-agent-links").textContent = outbound.length ? `可交给 ${outbound.map((link) => link.label).join("、")}` : "当前没有配置出站联动";
    targetSelect.innerHTML = '<option value="">选择要转交的项目</option>' + outbound.map((link) => `<option value="${escapeHtml(link.to)}">${escapeHtml(link.label)}</option>`).join("");
    handoffButton.disabled = !outbound.length;
  }
  function renderRuns(items = [], summary = {}) {
    const section = runsList.closest(".project-agent-runs");
    section?.classList.toggle("is-empty", !items.length);
    const active = Number(summary.active || 0);
    const failed = Number(summary.failed || 0);
    runsSummary.textContent = active ? `${active} 个运行中${failed ? ` · ${failed} 个失败` : ""}` : failed ? `${failed} 个失败，可重试` : "最近 8 条";
    if (!items.length) {
      runsList.innerHTML = '<div class="project-agent-runs-empty">还没有运行记录。发送消息后，这里会显示 Agent 的执行状态。</div>';
      return;
    }
    runsList.innerHTML = items.slice(0, 8).map((run) => {
      const retry = run.retryable ? `<button type="button" class="project-agent-run-retry" data-run-retry="${escapeHtml(run.id)}">重试</button>` : "";
      const detail = run.error ? `<p class="project-agent-run-error">${escapeHtml(run.error)}</p>` : `<p>${escapeHtml(run.title || "Agent 运行")}</p>`;
      return `<article class="project-agent-run ${escapeHtml(run.status || "queued")}" data-run-detail="${escapeHtml(run.id)}"><div class="project-agent-run-top"><span class="project-agent-run-state"><i></i>${escapeHtml(run.status_label || run.status || "排队中")}</span><time>${escapeHtml(formatDate(run.updated_at || run.created_at))}</time></div><div class="project-agent-run-title">${escapeHtml(run.kind === "chat" ? "项目对话" : run.kind === "action" ? "本地动作" : "Agent 调度")} · 第 ${escapeHtml(run.attempt || 1)}/${escapeHtml(run.max_attempts || 1)} 次</div>${detail}<div class="project-agent-run-foot">${retry}<span>点击卡片查看执行回放</span></div></article>`;
    }).join("");
  }
  async function loadRuns() {
    const body = await requestJson(`/api/agent/${encodeURIComponent(projectId)}/runs?limit=8`);
    renderRuns(body.runs || [], body.summary || {});
  }
  function renderRunDetail(body = {}) {
    if (!runDetail) return;
    const run = body.run || {};
    const events = body.events || body.timeline?.events || [];
    const actions = body.actions || body.timeline?.actions || [];
    const contract = body.result_contract || body.timeline?.result_contract || run.result?.result_contract || {};
    const eventMarkup = events.length ? events.map((event) => `<div class="project-agent-timeline-event ${escapeHtml(event.level || "info")}"><i></i><div><strong>${escapeHtml(event.message || event.event_type || "运行事件")}</strong><small>${escapeHtml(formatDate(event.created_at))} · ${escapeHtml(event.event_type || "event")}</small></div></div>`).join("") : '<div class="project-agent-runs-empty">这次运行没有更多事件。</div>';
    const citationMarkup = (contract.citations || []).slice(0, 8).map((item) => item.type === "url" ? `<a href="${escapeHtml(item.value)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.label || item.value)}</a>` : `<span>${escapeHtml(item.value)}</span>`).join(" · ");
    const planTrace = agentExecutionPlanTrace(contract.execution_plan || {});
    runDetail.innerHTML = `<div class="project-agent-run-detail-head"><strong>运行回放 · ${escapeHtml(run.status_label || run.status || "未知")}</strong><button type="button" data-run-detail-close>收起</button></div><div class="project-agent-timeline">${eventMarkup}</div>${planTrace ? `<div class="project-agent-citations"><strong>执行计划</strong><p>${planTrace}</p></div>` : ""}${actions.length ? `<div class="project-agent-run-detail-actions"><strong>动作</strong><div>${agentActionMarkup(actions)}</div></div>` : ""}${citationMarkup ? `<div class="project-agent-citations"><strong>来源</strong><p>${citationMarkup}</p></div>` : ""}${contract.needs_review ? `<div class="project-agent-citations agent-contract-review"><strong>需要复核</strong><p>${escapeHtml((contract.review_reasons || []).join("、") || "证据或数据时间不足")}</p></div>` : ""}`;
    runDetail.hidden = false;
  }
  async function loadRunDetail(runId) {
    if (!runDetail) return;
    runDetail.hidden = false;
    runDetail.innerHTML = '<div class="project-agent-runs-empty">正在读取运行回放…</div>';
    try { renderRunDetail(await requestJson(`/api/agent/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}`)); }
    catch (error) { runDetail.innerHTML = `<div class="project-agent-runs-empty">${escapeHtml(error.message)}</div>`; }
  }
  function renderIncomingWorkItems(items = []) {
    const actionable = items.filter((item) => ["open", "blocked", "failed"].includes(item.status));
    const section = incomingList.closest(".project-agent-incoming");
    section?.classList.toggle("is-empty", !actionable.length);
    incomingSummary.textContent = actionable.length ? `${actionable.length} 条待接收` : "暂无待我处理";
    if (!actionable.length) {
      incomingList.innerHTML = '<div class="project-agent-runs-empty">没有需要这个 Agent 处理的交接。</div>';
      return;
    }
    incomingList.innerHTML = actionable.slice(0, 4).map((item) => {
      const source = item.source_context;
      const sourceLine = source
        ? `${source.kind_label} · ${source.source_label}${source.source_updated_at ? ` · 更新 ${formatDate(source.source_updated_at)}` : ""}`
        : `${item.source_agent_name || item.source_project || "工作台"} · ${item.status === "failed" ? "上次失败" : item.status === "blocked" ? "等待处理" : "待接收"}`;
      const nextStep = source?.next_step ? `<small class="project-agent-incoming-next">下一步：${escapeHtml(source.next_step)}</small>` : "";
      const sourceLink = source?.source_url && /^https?:\/\//i.test(source.source_url)
        ? `<a class="project-agent-incoming-source" href="${escapeHtml(source.source_url)}" target="_blank" rel="noopener noreferrer">查看原始来源 ↗</a>`
        : "";
      return `<article class="project-agent-incoming-item"><div><strong>${escapeHtml(item.title || "未命名工作项")}</strong><p>${escapeHtml(item.description || "没有描述")}</p><small>${escapeHtml(sourceLine)}</small>${nextStep}${sourceLink}</div><button type="button" data-work-item-run="${escapeHtml(item.id)}">${item.status === "failed" ? "重试" : "交给 Agent"}</button></article>`;
    }).join("");
  }
  async function loadIncomingWorkItems() {
    const body = await requestJson(`/api/agent/${encodeURIComponent(projectId)}/work-items?status=all&limit=12`);
    renderIncomingWorkItems(body.items || []);
  }
  async function loadSession(id) {
    if (!id) { sessionId = ""; currentSession = null; renderMessages([]); return; }
    const body = await requestJson(`/api/agent/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(id)}`);
    sessionId = id; currentSession = body.session; renderMessages(body.messages || []); renderCapability(body.agent, body.agent?.links);
  }
  async function loadAgent() {
    const [body] = await Promise.all([
      requestJson(`/api/agent/${encodeURIComponent(projectId)}/sessions`),
      loadRuns(),
      loadIncomingWorkItems(),
    ]);
    renderCapability(body.agent, body.agent?.links);
    const sessions = body.sessions || [];
    sessionSelect.innerHTML = '<option value="">新会话</option>' + sessions.map((session) => `<option value="${escapeHtml(session.id)}">${escapeHtml(session.title)}</option>`).join("");
  }
  function showAgentLoadError(error) {
    const message = error?.message || "项目 Agent 暂时无法读取";
    status.innerHTML = `${escapeHtml(message)} <button type="button" class="project-agent-inline-retry" data-agent-load-retry>重试读取</button>`;
    status.setAttribute("role", "alert");
    status.setAttribute("aria-live", "assertive");
  }
  status.addEventListener("click", async (event) => {
    const retry = event.target.closest("[data-agent-load-retry]");
    if (!retry) return;
    retry.disabled = true;
    retry.setAttribute("aria-busy", "true");
    retry.textContent = "读取中…";
    try { await loadAgent(); status.textContent = "已恢复 · 项目 Agent 上下文已读取"; }
    catch (error) { showAgentLoadError(error); }
  });
  launcher.addEventListener("click", async () => {
    const opening = panel.classList.contains("hidden"); setOpen(opening);
    if (!opening) return;
    status.textContent = "正在读取项目上下文…";
    try { await loadAgent(); status.textContent = "全局 LLM · 会话保存在本机"; } catch (error) { showAgentLoadError(error); }
  });
  close.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !panel.classList.contains("hidden")) setOpen(false); });
  panel.querySelector("#project-agent-new").addEventListener("click", () => { sessionSelect.value = ""; loadSession(""); input.focus(); });
  sessionSelect.addEventListener("change", () => loadSession(sessionSelect.value).catch((error) => { status.textContent = error.message; }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message || form.querySelector("button[type=submit]").disabled) return;
    const send = form.querySelector("button[type=submit]");
    send.disabled = true; send.setAttribute("aria-busy", "true"); input.value = ""; status.textContent = "Agent 正在读取项目上下文…";
    messages.insertAdjacentHTML("beforeend", `<div class="project-agent-message user"><strong>你</strong><p>${escapeHtml(message)}</p></div>`); messages.scrollTop = messages.scrollHeight;
    const thinking = document.createElement("div");
    thinking.className = "project-agent-message assistant project-agent-thinking";
    thinking.innerHTML = "<strong>项目 Agent</strong><p><i></i><i></i><i></i>正在读取项目上下文并执行工具…</p>";
    messages.appendChild(thinking); messages.scrollTop = messages.scrollHeight;
    try {
      const body = await requestJson(`/api/agent/${encodeURIComponent(projectId)}/chat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId, message, context: { source: window.location.pathname } }) });
      sessionId = body.session?.id || sessionId; currentSession = body.session; sessionSelect.innerHTML = '<option value="">新会话</option>' + (await requestJson(`/api/agent/${encodeURIComponent(projectId)}/sessions`)).sessions.map((session) => `<option value="${escapeHtml(session.id)}">${escapeHtml(session.title)}</option>`).join(""); sessionSelect.value = sessionId;
      renderMessages(body.messages || []); renderCapability(body.agent, body.links); await Promise.all([loadRuns(), loadIncomingWorkItems()]); status.textContent = body.run?.status === "partial" ? "已完成 · 有动作失败，请查看最近运行" : "已完成 · 结果和会话已保存";
    } catch (error) { thinking.remove(); messages.insertAdjacentHTML("beforeend", `<div class="project-agent-message assistant error"><strong>系统提示</strong><p>${escapeHtml(error.message)}</p></div>`); await loadRuns().catch(() => {}); status.textContent = error.message; } finally { send.disabled = false; send.removeAttribute("aria-busy"); }
  });
  runsList.addEventListener("click", async (event) => {
    const retryButton = event.target.closest("[data-run-retry]");
    const detailButton = event.target.closest("[data-run-detail]");
    if (!retryButton && detailButton) { await loadRunDetail(detailButton.dataset.runDetail); return; }
    if (!retryButton) return;
    retryButton.disabled = true; retryButton.setAttribute("aria-busy", "true");
    retryButton.textContent = "重试中…";
    status.textContent = "正在重试这次 Agent 运行…";
    try {
      const body = await requestJson(`/api/agent/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(retryButton.dataset.runRetry)}/retry`, { method: "POST" });
      sessionId = body.session?.id || sessionId;
      currentSession = body.session || currentSession;
      renderMessages(body.messages || []);
      renderCapability(body.agent, body.links);
      await Promise.all([loadRuns(), loadIncomingWorkItems()]);
      status.textContent = body.run?.status === "partial" ? "重试完成，但有本地动作失败" : "重试完成，结果已保存";
    } catch (error) { retryButton.disabled = false; retryButton.removeAttribute("aria-busy"); retryButton.textContent = "重试"; status.textContent = error.message; await loadRuns().catch(() => {}); }
    retryButton.removeAttribute("aria-busy");
  });
  runDetail?.addEventListener("click", (event) => { if (event.target.closest("[data-run-detail-close]")) { runDetail.hidden = true; runDetail.innerHTML = ""; } });
  incomingList.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-work-item-run]");
    if (!button) return;
    button.disabled = true; button.setAttribute("aria-busy", "true"); button.textContent = "处理中…"; status.textContent = "目标 Agent 正在读取交接并执行…";
    try {
      const body = await requestJson(`/api/agent/${encodeURIComponent(projectId)}/work-items/${encodeURIComponent(button.dataset.workItemRun)}/run`, { method: "POST" });
      sessionId = body.session?.id || sessionId; currentSession = body.session || currentSession; renderMessages(body.messages || []); renderCapability(body.agent, body.links); await Promise.all([loadRuns(), loadIncomingWorkItems()]);
      status.textContent = body.work_item?.status === "blocked" ? "已分析，但有动作需要人工确认。" : body.work_item?.status === "done" ? "交接已完成，结果已回写工作项。" : "交接执行失败，请查看最近运行。";
    } catch (error) { button.disabled = false; button.removeAttribute("aria-busy"); button.textContent = "重试"; status.textContent = error.message; await loadIncomingWorkItems().catch(() => {}); }
    button.removeAttribute("aria-busy");
  });
  handoffButton.addEventListener("click", async () => {
    const toProject = targetSelect.value;
    if (!toProject || !lastAnswer) { status.textContent = "先完成一次对话，再选择要转交的项目。"; return; }
    const targetLabel = targetSelect.selectedOptions[0]?.textContent || toProject;
    if (!handoffConfirmationPending) {
      handoffConfirmationPending = true;
      handoffButton.textContent = "确认交接";
      handoffButton.classList.add("pending");
      status.textContent = `将把这次结果交给 ${targetLabel}，确认后会创建可追踪工作项。`;
      return;
    }
    handoffButton.disabled = true;
    try {
      const title = currentSession?.title || `${agent?.name || "项目 Agent"} 后续任务`;
      await requestJson("/api/handoffs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, description: lastAnswer, from_project: projectId, to_project: toProject, confirmed: true, metadata: { source_session_id: sessionId } }) });
      status.textContent = `已交给 ${targetLabel}，工作台已记录联动任务。`;
    } catch (error) { status.textContent = error.message; } finally { handoffConfirmationPending = false; handoffButton.classList.remove("pending"); handoffButton.textContent = "转交"; handoffButton.disabled = false; }
  });
  quickActions.addEventListener("click", (event) => {
    const button = event.target.closest("[data-agent-quick-prompt]");
    if (!button) return;
    input.value = button.dataset.agentQuickPrompt || "";
    input.focus();
  });
  targetSelect.addEventListener("change", () => { handoffConfirmationPending = false; handoffButton.classList.remove("pending"); handoffButton.textContent = "转交"; });
  if (new URLSearchParams(window.location.search).get("focus") === "agent") {
    window.setTimeout(async () => {
      setOpen(true);
      status.textContent = "正在读取项目上下文…";
      try { await loadAgent(); status.textContent = "已定位到项目 Agent · 可继续处理"; }
      catch (error) { showAgentLoadError(error); }
    }, 80);
  }
}

function setupEnterToSend() {
  // 所有「问AI」输入框统一：Enter 发送，Shift+Enter 换行，输入法组词中不误触。
  const selectors = ["#project-agent-input", "#aihot-chat-input", "#idea-chat-input"];
  selectors.forEach((selector) => {
    const input = document.querySelector(selector);
    if (!input || input.dataset.enterToSend) return;
    input.dataset.enterToSend = "true";
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        input.form?.requestSubmit();
      }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => { setupThemeToggle(); ensureGlobalSettingsEntry(); setupGlobalSettings(); setupProjectAgent(); setupEnterToSend(); });
