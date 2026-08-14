const qs = (selector, root = document) => root.querySelector(selector);
const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const fmt = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
};
const platformErrorMessage = (status, detail = "") => status === 401 ? "线上入口需要认证，请先完成登录后再试。" : status === 403 ? "当前操作需要额外权限，请检查登录状态或确认权限。" : status === 429 ? "请求过于频繁，请稍后再试。" : status >= 500 ? "服务暂时不可用，请稍后重试。" : detail || `请求未完成（${status || "网络"}）`;
const api = async (url, options = {}) => {
  if (window.WorkbenchUX?.requestJson) return window.WorkbenchUX.requestJson(url, options);
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller ? window.setTimeout(() => controller.abort(), 15000) : null;
  let response;
  try { response = await fetch(url, controller ? { ...options, signal: controller.signal } : options); }
  catch (error) { throw new Error(error?.name === "AbortError" ? "请求超时，请稍后重试。" : "网络连接失败，请检查线上入口后重试。"); }
  finally { if (timer !== null) window.clearTimeout(timer); }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(platformErrorMessage(response.status, body.detail || body.message || ""));
  return body;
};
const jsonOptions = (body, method = "POST") => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
function setupThemeToggle() {
  if (!document.querySelector("link[data-workbench-theme]")) { const link = document.createElement("link"); link.rel = "stylesheet"; link.href = "/static/theme.css?v=0.3.197"; link.dataset.workbenchTheme = "true"; document.head.append(link); }
  const topbar = qs(".platform-topbar");
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
function status(id, message, tone = "") {
  const node = qs(`#${id}`);
  if (!node) return;
  node.textContent = message;
  node.className = `platform-status${tone ? ` ${tone}` : ""}`;
  node.setAttribute("role", tone === "error" ? "alert" : "status");
  node.setAttribute("aria-live", tone === "error" ? "assertive" : "polite");
}
function pageNotice(message, tone = "") { status("page-status", message, tone); }
function renderLoadError(selectors, message) {
  const content = `<div class="platform-list-empty platform-load-error" role="alert"><strong>读取失败</strong><br>${esc(message)}<br><button class="platform-button-secondary" type="button" data-retry-page>重新加载</button></div>`;
  selectors.forEach((selector) => { const node = qs(selector); if (node) node.innerHTML = content; });
}
function badge(label, tone = "") { return `<span class="platform-badge ${esc(tone)}">${esc(label)}</span>`; }
function setBusy(button, busy, busyLabel = "处理中…") {
  if (!button) return;
  if (busy) {
    button.dataset.originalLabel = button.textContent;
    button.textContent = busyLabel;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  } else {
    button.textContent = button.dataset.originalLabel || button.textContent;
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

function renderCapabilityGraph(nodes = []) {
  const grid = qs("#capability-grid");
  if (!grid) return;
  grid.innerHTML = nodes.length ? nodes.map((node) => {
    const freshness = node.freshness || {};
    const load = node.load || {};
    const quality = node.quality || {};
    const gap = (node.gaps || [])[0] || "暂无登记缺口";
    const qualityTone = quality.state === "verified" ? "good" : quality.state === "needs_repair" || quality.state === "stale" || quality.state === "historical_failed" ? "danger" : "warning";
    const qualityText = quality.state_label || "仅配置未运行";
    const qualityMeta = quality.total
      ? `近 ${esc(quality.window_hours || 24)}h：成功 ${Math.round(Number(quality.success_rate || 0) * 100)}% · 来源 ${Math.round(Number(quality.source_completeness_rate || 0) * 100)}% · 数据时间 ${Math.round(Number(quality.data_time_completeness_rate || 0) * 100)}% · 计划 ${Math.round(Number(quality.plan_completeness_rate || 0) * 100)}%`
      : quality.historical_total
        ? `近 ${esc(quality.window_hours || 24)}h 无运行 · 历史 ${esc(quality.historical_total)} 次（失败 ${esc(quality.historical_failed || 0)}） · 最近 ${esc(fmt(quality.last_run_at))}`
        : "尚无可评价运行";
    const historyError = !quality.total && quality.last_error ? `<small class="capability-quality capability-quality-error">最近失败：${esc(quality.last_error)}</small>` : "";
    const hrefs = { inbox: "/projects/inbox", knowledge: "/projects/knowledge", "doc-factory": "/projects/doc-factory", sub2api: "/projects/sub2api", market: "/projects/market", server: "/projects/server", crawl4ai: "/crawl4ai", aihot: "/projects/aihot", "idea-analysis": "/projects/idea-analysis", "cid-dashboard": "/projects/cid-dashboard" };
    const action = quality.historical_total || quality.total ? `<a class="capability-card-action" href="${hrefs[node.id] || "/"}">查看运行与来源</a>` : "";
    return `<article class="capability-card"><div class="capability-card-head"><div><strong>${esc(node.name || node.id)}</strong><small>${esc(node.id || "")}</small></div>${badge(qualityText, qualityTone)}</div><p>${esc(node.mission || "围绕项目上下文给出可追踪结果")}</p><div class="capability-meta"><span>运行中 ${esc(load.active ?? 0)} · 失败 ${esc(load.failed ?? 0)}</span><span title="${esc(gap)}">缺口 ${esc(gap.slice(0, 18))}</span></div><small class="capability-quality">${qualityMeta}</small>${historyError}${action}</article>`;
  }).join("") : '<div class="platform-list-empty">暂时没有子 Agent 能力数据。</div>';
}
function renderWorkerStatus(workers = [], leaseSeconds = 0) {
  const list = qs("#worker-status-list"); if (!list) return;
  const active = workers.filter((worker) => worker.claimed && !worker.stale).length;
  const summary = qs("#worker-runtime-summary"); if (summary) summary.textContent = `${active}/${workers.length} 个有有效心跳 · 租约 ${leaseSeconds || "—"}s`;
  list.innerHTML = workers.length ? workers.map((worker) => {
    const tone = worker.stale ? "danger" : worker.claimed ? "good" : "warning";
    const label = worker.stale ? "心跳过期" : worker.claimed ? worker.status || "运行中" : "未认领";
    const heartbeat = worker.heartbeat_age_seconds != null ? `心跳 ${worker.heartbeat_age_seconds}s 前` : "暂无心跳";
    const owner = worker.instance_id ? `实例 ${worker.instance_id}` : "没有持有实例";
    const queue = `队列 ${worker.queue_depth || 0} · 执行中 ${worker.running_count || 0}`;
    const error = worker.last_error ? ` · ${worker.last_error_state === "recovered" ? "历史失败（已恢复）" : "最近失败"}：${worker.last_error}` : "";
    const success = worker.last_success_at ? ` · 最近成功 ${fmt(worker.last_success_at)}` : " · 暂无成功记录";
    const lease = worker.lease_until ? `租约至 ${esc(fmt(worker.lease_until))}` : "暂无有效租约";
    const replay = worker.last_run_id && worker.id === "crawl-worker" ? `<a class="worker-replay-link" href="/api/agent/crawl4ai/runs/${encodeURIComponent(worker.last_run_id)}" target="_blank" rel="noopener">查看最近 Run</a>` : "";
    return `<div class="worker-status-row"><div><strong>${esc(worker.label || worker.id)}</strong><small>${esc(worker.scope || worker.id)} · ${esc(owner)} · ${lease}</small><small>${esc(queue)} · ${esc(heartbeat)}${esc(success)}${esc(error)}</small>${replay}</div>${badge(label, tone)}</div>`;
  }).join("") : '<div class="platform-list-empty">没有 Worker 定义。</div>';
}
function renderRuntimeMetrics(body = {}) {
  const root = qs("#llm-runtime-metrics"); if (!root) return;
  const summary = body.summary || {}; const providers = body.by_provider || []; const errors = (body.error_kinds || []).slice(0, 3);
  root.innerHTML = `<div class="runtime-metric-grid"><span><strong>${esc(summary.calls || 0)}</strong><small>调用</small></span><span><strong>${summary.calls ? `${Math.round(Number(summary.success_rate || 0) * 100)}%` : "—"}</strong><small>成功率</small></span><span><strong>${esc(summary.avg_latency_ms || 0)}ms</strong><small>平均延迟</small></span><span><strong>${esc(summary.total_tokens || 0)}</strong><small>Token</small></span></div><div class="runtime-provider-list">${providers.length ? providers.map((item) => `<div><strong>${esc(item.provider_name || item.provider_id)}</strong><span>${esc(item.calls)} 次 · ${Math.round(Number(item.success_rate || 0) * 100)}% · ${esc(item.avg_latency_ms || 0)}ms</span></div>`).join("") : '<div class="platform-list-empty">暂无调用记录。</div>'}</div>${errors.length ? `<p class="runtime-error-note">失败类型：${errors.map((item) => `${esc(item.kind)} ${esc(item.count)}`).join(" · ")}</p>` : ""}`;
}
async function loadCapabilityGraph() {
  const body = await api("/api/agent/capability-graph");
  renderCapabilityGraph(body.nodes || []);
  return body;
}

const automationState = { kinds: [], rules: [], plans: [], projects: [] };
let automationEditingId = null;
function automationRulePayload(rule, overrides = {}) { return { name: overrides.name ?? rule.name, kind: overrides.kind ?? rule.kind, project_id: overrides.project_id ?? rule.project_id, schedule: overrides.schedule ?? rule.schedule ?? "", enabled: overrides.enabled ?? Boolean(rule.enabled), config: overrides.config ?? rule.config ?? {} }; }
function fillAutomationForm(rule = {}) {
  const config = rule.config || {};
  const set = (id, value) => { const el = qs(id); if (el) el.value = value ?? ""; };
  // select 里没有匹配 option 时动态补一个，避免 required 校验拦截提交（如项目不在能力图节点中）
  const ensureOption = (selector, value) => {
    const el = qs(selector);
    if (!el || !value) return;
    if ([...el.options].some((option) => option.value === value)) return;
    const option = document.createElement("option");
    option.value = value; option.textContent = value; el.append(option);
  };
  ensureOption("#automation-kind", rule.kind);
  ensureOption("#automation-project", rule.project_id);
  set("#automation-name", rule.name);
  set("#automation-kind", rule.kind);
  set("#automation-project", rule.project_id);
  set("#automation-schedule", rule.schedule || "");
  set("#automation-notice-title", config.title);
  set("#automation-notice-body", config.body);
  set("#automation-notice-href", config.href);
  const enabled = qs("#automation-enabled"); if (enabled) enabled.checked = Boolean(rule.enabled);
}
function setAutomationEditMode(ruleId = null) {
  automationEditingId = ruleId;
  const title = qs("#automation-form-title"); if (title) title.textContent = ruleId ? "编辑自动化规则" : "创建自动化规则";
  const submit = qs("#automation-form button[type=submit]"); if (submit) submit.textContent = ruleId ? "保存修改" : "保存规则";
  const cancel = qs("#automation-cancel-edit"); if (cancel) cancel.classList.toggle("hidden", !ruleId);
}
function renderAutomationRules() {
  const list = qs("#automation-rules");
  if (!list) return;
  if (!automationState.rules.length) { list.innerHTML = '<div class="platform-list-empty">还没有自动化规则。先创建一条低风险规则，手动运行一次确认结果。</div>'; return; }
  list.innerHTML = automationState.rules.map((rule) => {
    const kind = automationState.kinds.find((item) => item.kind === rule.kind);
    const summary = rule.run_summary || {};
    const failedCount = Number(summary.failed || 0);
    const stateTone = rule.status === "failed" || failedCount ? "failed" : rule.status === "running" ? "running" : rule.enabled ? "enabled" : "";
    const last = rule.last_run_at ? `最近 ${fmt(rule.last_run_at)}` : "尚未运行";
    const next = rule.enabled && rule.next_run_at ? ` · 下次 ${fmt(rule.next_run_at)}` : "";
    const error = rule.last_error ? ` · ${rule.last_error}` : "";
    const recent = (rule.recent_runs || []).slice(0, 3).map((run) => `${run.status === "succeeded" ? "✓" : run.status === "failed" ? "!" : "·"} ${fmt(run.created_at)} ${run.status === "succeeded" ? "成功" : run.status === "failed" ? `失败：${run.error || "未记录原因"}` : run.status}`).join("；");
    const statusLabel = rule.status === "failed" ? "失败待修复" : failedCount ? "有失败记录" : rule.status || "未运行";
    return `<article class="platform-row"><div class="platform-row-main"><div class="platform-row-title"><strong>${esc(rule.name)}</strong>${badge(rule.enabled ? "已启用" : "已停用", stateTone)}${badge(kind?.label || rule.kind)}</div><p>${esc(kind?.label || rule.kind)} · ${esc(rule.project_id || "workbench")} · ${esc(rule.schedule || "仅手动")}</p><div class="platform-row-meta"><span>${esc(last)}${esc(next)}${esc(error)}</span><span>运行状态：${esc(statusLabel)} · 近期开跑 ${esc(summary.total || 0)} · 成功 ${esc(summary.succeeded || 0)} · 失败 ${esc(summary.failed || 0)}</span></div>${recent ? `<small class="automation-run-history">${esc(recent)}</small>` : `<small class="automation-run-history">尚无运行记录；先手动运行一次确认真实链路。</small>`}</div><div class="platform-row-actions"><button type="button" data-edit-rule="${esc(rule.id)}">编辑</button><button type="button" data-toggle-rule="${esc(rule.id)}">${rule.enabled ? "停用" : "启用"}</button><button type="button" data-run-rule="${esc(rule.id)}">${rule.status === "failed" || failedCount ? "重试" : "立即运行"}</button><button class="danger" type="button" data-delete-rule="${esc(rule.id)}">删除</button></div></article>`;
  }).join("");
}
function renderPlanSteps(steps = []) {
  const list = qs("#plan-list");
  if (!list) return;
  if (!steps.length) { list.innerHTML = '<div class="platform-list-empty">还没有执行计划。计划会按依赖顺序运行，失败达到重试次数后暂停。</div>'; return; }
  const statusNames = { draft: "草稿", running: "运行中", succeeded: "已完成", blocked: "已暂停", failed: "失败" };
  list.innerHTML = steps.map((plan) => `<article class="platform-row"><div class="platform-row-main"><div class="platform-row-title"><strong>${esc(plan.title)}</strong>${badge(statusNames[plan.status] || plan.status || "草稿", plan.status === "succeeded" ? "succeeded" : plan.status === "blocked" ? "blocked" : plan.status === "running" ? "running" : "")}</div><p>${esc(plan.source_project || "workbench")} · ${esc((plan.steps || []).length)} 个步骤 · 更新 ${esc(fmt(plan.updated_at))}</p><div class="platform-row-meta"><span>${plan.error ? `暂停原因：${esc(plan.error)}` : `步骤 ${esc((plan.steps || []).filter((step) => step.status === "succeeded").length)}/${esc((plan.steps || []).length)} 已完成`}</span><span>${esc(plan.id || "")}</span></div></div><div class="platform-row-actions">${["draft", "blocked"].includes(plan.status) ? `<button type="button" data-run-plan="${esc(plan.id)}">运行计划</button>` : ""}${plan.status === "blocked" ? `<button type="button" data-takeover-plan="${esc(plan.id)}">人工接管</button>` : ""}<button type="button" data-view-plan="${esc(plan.id)}">查看步骤</button></div></article>`).join("");
}
function addPlanStep(defaults = {}) {
  const list = qs("#plan-steps");
  if (!list) return;
  const index = list.children.length + 1;
  const row = document.createElement("div");
  row.className = "platform-step-row";
  row.innerHTML = `<span class="platform-step-label">步骤 ${index}</span><input data-step-key value="${esc(defaults.key || `step-${index}`)}" placeholder="key" aria-label="步骤 key" /><input data-step-title value="${esc(defaults.title || "")}" placeholder="步骤名称" aria-label="步骤名称" /><select data-step-kind aria-label="步骤类型"><option value="agent">Agent</option><option value="local">本地动作</option><option value="automation">自动化规则</option></select><select data-step-project aria-label="负责项目"><option value="workbench">工作台</option><option value="inbox">收件箱</option><option value="knowledge">知识库</option><option value="crawl4ai">Crawl4AI</option><option value="market">量化</option><option value="aihot">AI 热点</option><option value="idea-analysis">想法分析</option><option value="doc-factory">文档工厂</option></select><input data-step-deps value="${esc((defaults.dependencies || []).join(","))}" placeholder="依赖 key" aria-label="依赖步骤" /><input data-step-input value="${esc(defaults.input_value || "")}" placeholder="动作 / 规则 ID" aria-label="步骤输入" /><button type="button" data-remove-step title="删除步骤" aria-label="删除步骤">×</button>`;
  qs("[data-step-project]", row).value = defaults.project_id || "workbench";
  qs("[data-step-kind]", row).value = defaults.kind || "agent";
  list.appendChild(row);
}
function readPlanSteps() {
  return qsa(".platform-step-row", qs("#plan-steps")).map((row) => { const kind = qs("[data-step-kind]", row).value; const title = qs("[data-step-title]", row).value.trim(); const rawInput = qs("[data-step-input]", row).value.trim(); const input = kind === "local" ? { action: rawInput || "backup" } : kind === "automation" ? { rule_id: Number(rawInput || 0) } : { message: title || "执行计划步骤" }; return { key: qs("[data-step-key]", row).value.trim(), title, project_id: qs("[data-step-project]", row).value, kind, dependencies: qs("[data-step-deps]", row).value.split(",").map((item) => item.trim()).filter(Boolean), input, max_attempts: 2 }; }).filter((step) => step.key && step.title);
}
async function loadAutomations() {
  const [automation, plans, graph, workers, agentMetrics] = await Promise.all([api("/api/automations"), api("/api/plans"), api("/api/agent/capability-graph"), api("/api/workers"), api("/api/agents/metrics?hours=24")]);
  automationState.kinds = automation.kinds || []; automationState.rules = automation.rules || []; automationState.plans = plans.plans || [];
  const kindSelect = qs("#automation-kind");
  if (kindSelect) kindSelect.innerHTML = automationState.kinds.map((kind) => `<option value="${esc(kind.kind)}" data-project="${esc(kind.project_id)}">${esc(kind.label)}</option>`).join("");
  const projectSelect = qs("#automation-project");
  if (projectSelect) projectSelect.innerHTML = graph.nodes.map((node) => `<option value="${esc(node.id)}">${esc(node.name)}</option>`).join("");
  renderAutomationRules(); renderPlanSteps(automationState.plans); renderCapabilityGraph(graph.nodes || []);
  renderWorkerStatus(workers.workers || [], workers.lease_seconds); renderRuntimeMetrics(agentMetrics.llm || {});
  const ruleCount = qs("#rule-count"); const planCount = qs("#plan-count"); const recoveryCount = qs("#recovery-count"); if (ruleCount) ruleCount.textContent = automationState.rules.length; if (planCount) planCount.textContent = automationState.plans.length; if (recoveryCount) recoveryCount.textContent = automation.summary?.failed_runs ?? automationState.rules.reduce((total, rule) => total + Number(rule.run_summary?.failed || 0), 0);
  return { automation, plans, graph };
}
async function runAutomationRule(ruleId, button) { setBusy(button, true, "运行中…"); try { const body = await api(`/api/automations/${encodeURIComponent(ruleId)}/run`, { method: "POST" }); pageNotice(`规则已完成：${body.result?.message || body.rule?.name || "已写入运行记录"}`, "success"); await loadAutomations(); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } }
async function setupAutomationPage() {
  await loadAutomations();
  pageNotice(`已读取 ${automationState.rules.length} 条规则、${automationState.plans.length} 个计划`, "success");
  qs("#automation-kind")?.addEventListener("change", (event) => { const selected = event.target.selectedOptions[0]; const project = qs("#automation-project"); if (project && selected?.dataset.project) project.value = selected.dataset.project; });
  qs("#automation-form")?.addEventListener("submit", async (event) => { event.preventDefault(); const submit = qs("button[type=submit]", event.currentTarget); setBusy(submit, true, "保存中…"); try { const wasEditing = automationEditingId; const payload = { name: qs("#automation-name").value.trim(), kind: qs("#automation-kind").value, project_id: qs("#automation-project").value, schedule: qs("#automation-schedule").value, enabled: qs("#automation-enabled").checked, config: { title: qs("#automation-notice-title").value.trim(), body: qs("#automation-notice-body").value.trim(), href: qs("#automation-notice-href").value.trim() } }; const body = wasEditing ? await api(`/api/automations/${wasEditing}`, jsonOptions(payload, "PATCH")) : await api("/api/automations", jsonOptions(payload)); event.currentTarget.reset(); qs("#automation-enabled").checked = true; setAutomationEditMode(null); pageNotice(wasEditing ? `已更新规则：${body.rule?.name || "自动化规则"}` : `已创建规则：${body.rule?.name || "自动化规则"}`, "success"); await loadAutomations(); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(submit, false); } });
  qs("#automation-cancel-edit")?.addEventListener("click", () => { const form = qs("#automation-form"); form?.reset(); const enabled = qs("#automation-enabled"); if (enabled) enabled.checked = true; setAutomationEditMode(null); });
  qs("#automation-rules")?.addEventListener("click", async (event) => { const edit = event.target.closest("[data-edit-rule]"); const run = event.target.closest("[data-run-rule]"); const toggle = event.target.closest("[data-toggle-rule]"); const remove = event.target.closest("[data-delete-rule]"); if (edit) { const rule = automationState.rules.find((item) => String(item.id) === String(edit.dataset.editRule)); if (!rule) return; fillAutomationForm(rule); setAutomationEditMode(rule.id); qs("#automation-form")?.scrollIntoView({ block: "start", behavior: "smooth" }); qs("#automation-name")?.focus(); return; } if (run) return runAutomationRule(run.dataset.runRule, run); if (toggle) { const rule = automationState.rules.find((item) => String(item.id) === String(toggle.dataset.toggleRule)); if (!rule) return; setBusy(toggle, true, "保存…"); try { await api(`/api/automations/${rule.id}`, jsonOptions(automationRulePayload(rule, { enabled: !rule.enabled }), "PATCH")); await loadAutomations(); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(toggle, false); } return; } if (remove) { if (remove.dataset.deletePending !== "true") { remove.dataset.deletePending = "true"; remove.textContent = "再次点击删除"; window.setTimeout(() => { if (remove.isConnected) { delete remove.dataset.deletePending; remove.textContent = "删除"; } }, 4500); return; } try { await api(`/api/automations/${remove.dataset.deleteRule}`, { method: "DELETE" }); await loadAutomations(); pageNotice("规则已删除，历史运行记录仍保留", "success"); } catch (error) { pageNotice(error.message, "error"); } } });
  qs("#add-plan-step")?.addEventListener("click", () => addPlanStep());
  qs("#plan-steps")?.addEventListener("click", (event) => { if (event.target.closest("[data-remove-step]")) { event.target.closest(".platform-step-row")?.remove(); qsa(".platform-step-row", qs("#plan-steps")).forEach((row, index) => { const label = qs(".platform-step-label", row); if (label) label.textContent = `步骤 ${index + 1}`; }); } });
  if (!qsa(".platform-step-row", qs("#plan-steps")).length) { addPlanStep({ key: "scan", title: "扫描本机 Git 项目", kind: "local", input_value: "git_scan" }); addPlanStep({ key: "backup", title: "创建本地数据库备份", kind: "local", input_value: "backup", dependencies: ["scan"] }); }
  qs("#plan-form")?.addEventListener("submit", async (event) => { event.preventDefault(); const steps = readPlanSteps(); if (!steps.length) { pageNotice("计划至少需要一个完整步骤", "warning"); return; } const submit = qs("button[type=submit]", event.currentTarget); setBusy(submit, true, "创建中…"); try { const body = await api("/api/plans", jsonOptions({ title: qs("#plan-title").value.trim(), source_project: qs("#plan-source").value, steps, input: { created_from: "automation_center" } })); pageNotice(`已创建执行计划：${body.plan?.title || body.plan?.id}`, "success"); await loadAutomations(); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(submit, false); } });
  qs("#plan-list")?.addEventListener("click", async (event) => { const run = event.target.closest("[data-run-plan]"); const takeover = event.target.closest("[data-takeover-plan]"); const view = event.target.closest("[data-view-plan]"); const id = run?.dataset.runPlan || takeover?.dataset.takeoverPlan || view?.dataset.viewPlan; if (!id) return; if (view) { const plan = automationState.plans.find((item) => item.id === id); const detail = qs("#plan-detail"); if (plan && detail) { detail.hidden = false; detail.innerHTML = `<strong>${esc(plan.title || "执行计划")} · 步骤明细</strong><ol>${(plan.steps || []).map((step, index) => `<li><b>${esc(step.title || step.key || `步骤 ${index + 1}`)}</b> · ${esc(step.status || "待运行")}${step.dependencies?.length ? ` · 依赖 ${esc(step.dependencies.join(", "))}` : ""}</li>`).join("") || "<li>没有步骤</li>"}</ol>`; detail.scrollIntoView({ block: "nearest", behavior: "smooth" }); } return; } const button = run || takeover; setBusy(button, true, takeover ? "接管中…" : "运行中…"); try { const body = await api(`/api/plans/${encodeURIComponent(id)}/${takeover ? "takeover" : "run"}`, { method: "POST" }); pageNotice(takeover ? "已人工接管，可重新运行计划" : `计划${body.plan?.status === "succeeded" ? "已完成" : "已暂停"}`, body.plan?.status === "succeeded" ? "success" : "warning"); await loadAutomations(); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } });
}

function renderRepositories(repositories = []) {
  const grid = qs("#repo-grid"); if (!grid) return;
  if (!repositories.length) { grid.innerHTML = '<div class="platform-list-empty">没有扫描到配置范围内的 Git 仓库。扫描范围包含工作区、Documents/troe_projects 和 Documents/trae_projects 的直接子目录。</div>'; return; }
  grid.innerHTML = repositories.map((repo) => `<article class="repo-card"><div class="repo-head"><div><h3>${esc(repo.name)}${repo.source ? ` ${badge(repo.source, "good")}` : ""}</h3><span class="repo-path" title="${esc(repo.path)}">${esc(repo.path)}</span></div>${badge(repo.dirty ? "有未提交修改" : "工作区干净", repo.dirty ? "warning" : "good")}</div><div class="repo-facts"><div class="repo-fact"><strong>${esc(repo.branch || "—")}</strong><small>当前分支</small></div><div class="repo-fact"><strong>${esc((repo.commits || []).length)}</strong><small>最近提交</small></div><div class="repo-fact"><strong>${esc((repo.related_work_items || []).length)}</strong><small>关联工作项</small></div></div>${repo.status_lines?.length ? `<div class="repo-status">${esc(repo.status_lines.join("\n"))}</div>` : `<div class="repo-status">当前没有未提交修改。</div>`}<div class="repo-commits">${(repo.commits || []).slice(0, 4).map((commit) => `<div class="repo-commit"><code>${esc(commit.hash)}</code><span title="${esc(commit.subject)}">${esc(commit.subject)}</span></div>`).join("")}</div></article>`).join("");
}
async function setupGitPage() { const load = async (scan = false) => { const button = qs("#scan-git"); if (scan) setBusy(button, true, "扫描中…"); try { const body = await api(scan ? "/api/git/scan" : "/api/git/repositories", scan ? { method: "POST" } : {}); renderRepositories(body.repositories || []); const count = qs("#repo-count"); if (count) count.textContent = (body.repositories || []).length; const roots = qs("#git-roots"); if (roots) roots.textContent = String((body.scanned_roots || []).length); status("git-status", `扫描于 ${fmt(body.scanned_at)} · ${(body.scanned_roots || []).length} 个根目录`, "success"); } catch (error) { status("git-status", error.message, "error"); renderLoadError(["#repo-grid"], error.message); } finally { if (scan) setBusy(button, false); } }; qs("#scan-git")?.addEventListener("click", () => load(true)); await load(false); }

function toolStateLabel(tool = {}) { return ({ integrated: "已接入", candidate: "候选", optional: "可选" })[tool.state] || "可试用"; }
function toolStateTone(tool = {}) { return tool.state === "integrated" ? "good" : tool.state === "candidate" ? "warning" : ""; }
function renderTools(tools = [], trials = [], integrations = []) {
  const grid = qs("#tool-grid"); const list = qs("#trial-list"); const integrationGrid = qs("#integration-grid");
  if (grid) grid.innerHTML = tools.length ? tools.map((tool) => {
    const trial = trials.find((item) => item.metadata?.tool?.id === tool.id || item.description?.includes(tool.name));
    const install = tool.installed === true ? "本机已发现" : tool.installed === false ? "本机未安装，功能仍可回退" : "按需配置";
    return `<article class="tool-card"><div class="tool-head"><h3>${esc(tool.name)}</h3><span>${badge(toolStateLabel(tool), toolStateTone(tool))}${trial ? ` ${badge("已登记试用", "good")}` : ""}</span></div><a class="tool-url" href="${esc(tool.url)}" target="_blank" rel="noopener noreferrer">${esc(tool.url)} ↗</a><p>${esc(tool.scenario)}</p><div class="tool-detail"><span><strong>成本：</strong>${esc(tool.cost)}</span><span><strong>适配：</strong>${esc(tool.fit)}</span><span><strong>本机：</strong>${esc(install)}</span><span><strong>隐私边界：</strong>${esc(tool.data_boundary || "只在确认后访问外部服务")}</span><span><strong>试用：</strong>${esc(tool.trial)}</span></div><button class="platform-button-secondary" type="button" data-trial-tool="${esc(tool.id)}">${trial ? "再次登记试用" : "登记一次试用"}</button></article>`;
  }).join("") : '<div class="platform-list-empty">目录为空。</div>';
  if (integrationGrid) integrationGrid.innerHTML = integrations.length ? integrations.map((item) => {
    const testLabels = { succeeded: "连接成功", failed: "上次测试失败", notification_sent: "最近发送成功", notify_failed: "最近发送失败" };
    const statusText = item.configured ? `已配置 · ${testLabels[item.last_test_status] || "尚未测试"}` : "待配置 · 不会访问外部服务";
    const nextStep = item.next_step || (item.id === "activitywatch" ? "导入后先看近 7 天数据，再决定要减少哪类切换" : "配置后读取条目，勾选才会创建工作项");
    const privacy = item.data_boundary || (item.id === "activitywatch" ? "只保留聚合时长，不保存窗口标题/URL" : "密钥只保存于服务端，不回显");
    return `<article class="integration-card"><div class="tool-head"><div><h3>${esc(item.name)}</h3><a class="tool-url" href="${esc(item.repo)}" target="_blank" rel="noopener noreferrer">${esc(item.repo)} ↗</a></div>${badge(item.configured ? "已配置" : "待配置", item.configured ? "good" : "")}</div><p>${esc(item.description)}</p><div class="tool-detail"><span><strong>状态：</strong>${esc(statusText)}</span><span><strong>配置成本：</strong>${esc(item.configuration_cost || "按需配置")}</span><span><strong>下一步：</strong>${esc(nextStep)}</span><span><strong>隐私：</strong>${esc(privacy)}</span></div><button class="platform-button-secondary" type="button" data-open-integration="${esc(item.id)}">${item.configured ? "管理与测试" : "配置集成"}</button></article>`;
  }).join("") : '<div class="platform-list-empty">暂时没有可配置的效率集成。</div>';
  if (list) list.innerHTML = trials.length ? trials.slice(0, 8).map((item) => `<div class="platform-row"><div class="platform-row-main"><div class="platform-row-title"><strong>${esc(item.title)}</strong>${badge(item.status || "open", item.status === "done" ? "good" : "")}</div><p>${esc(item.description || "")}</p><div class="platform-row-meta"><span>工作项 #${esc(item.id)}</span><span>${esc(fmt(item.created_at))}</span></div></div></div>`).join("") : '<div class="platform-list-empty">登记后的试用会进入工作台工作项，保留场景和建议。</div>';
}
function renderIntegrationFields(item = {}) {
  const panel = qs("#integration-panel"); const fields = qs("#integration-fields"); if (!panel || !fields) return;
  panel.classList.remove("hidden");
  const title = qs("#integration-panel-title"); if (title) title.textContent = `${item.name || "集成"} · 配置`;
  const help = qs("#integration-panel-help"); if (help) help.textContent = `${item.description || ""} 密钥只保存于 Workbench 服务端，不会回显到页面。`;
  const placeholders = { base_url: "https://…", owner: "用户名或组织名", repo: "仓库名", token: "公开仓库可留空；留空保留已保存 Token", api_token: "保存后不会回显；留空保留已保存 Token", access_token: "保存后不会回显；留空保留已保存 Token", topic: "例如：workbench", user_id: "例如：12345", bucket_id: "例如：aw-watcher-window（留空读取全部数据桶）", project_id: "例如：12（留空读取可见任务）", query: "例如：个人知识管理方法", categories: "例如：general,science（可选）" };
  // 每个集成敏感字段"去哪申请"的提示；未列出的字段不额外提示。
  const keyHelp = {
    "github.token": "在 GitHub → Settings → Developer settings → Personal access tokens 生成（只读权限即可）",
    "ntfy.token": "ntfy 服务端管理面板生成；公开主题可留空",
    "miniflux.api_token": "Miniflux → Settings → API Keys 生成",
    "zotero.api_key": "Zotero → Settings → API（开发者）页面生成 Key",
    "linkding.token": "Linkding → Settings → API Token 生成",
    "paperless.token": "Paperless-ngx → 设置 → API Token 生成",
    "vikunja.api_token": "Vikunja → 用户设置 → API Token 生成",
    "wallabag.access_token": "Wallabag → Developer 页面 → 创建客户端获取",
  };
  const keyHelpGeneric = "在对应服务的账户设置 / 开发者设置里生成 API Token 后粘贴到这里（工作台只保存它，用于只读读取你的数据）";
  fields.innerHTML = Object.entries(item.fields || {}).map(([key, label]) => {
    const secret = Boolean(item[`has_${key}`]);
    const inputType = ["token", "api_token", "api_key", "access_token"].includes(key) ? "password" : "text";
    const value = secret ? "" : item.values?.[key] || "";
    const placeholder = secret ? (item.configured ? "已保存，留空保持不变" : placeholders[key] || "保存后不会回显") : placeholders[key] || "";
    const helpText = keyHelp[`${item.id}.${key}`] || (["token", "api_token", "api_key", "access_token"].includes(key) ? keyHelpGeneric : "");
    return `<label>${esc(label)}<input data-integration-field="${esc(key)}" type="${inputType}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" />${helpText ? `<small class="integration-field-help">${esc(helpText)}</small>` : ""}</label>`;
  }).join("");
  const itemPanel = qs("#integration-items-panel"); if (itemPanel) itemPanel.classList.toggle("hidden", !item.configured || item.id === "ntfy");
  const ntfyButton = qs("#send-ntfy-test"); if (ntfyButton) ntfyButton.classList.toggle("hidden", item.id !== "ntfy" || !item.configured);
  const statusNode = qs("#integration-status"); if (statusNode) statusNode.textContent = item.last_test_status ? `最近测试：${item.last_test_status}${item.last_error ? ` · ${item.last_error}` : ""}` : "";
}
function renderIntegrationItems(items = []) {
  const list = qs("#integration-items"); if (!list) return;
  list.innerHTML = items.length ? items.map((item) => { const kind = item.metadata?.kind === "pull_request" ? "Pull Request" : item.metadata?.kind === "issue" ? "Issue" : item.metadata?.kind === "time_summary" ? "效率观察" : item.metadata?.kind === "task" ? "待办任务" : item.metadata?.kind === "search_result" ? "搜索结果" : item.metadata?.kind === "saved_article" ? "稍后读" : ""; const time = item.metadata?.source_updated_at || item.published_at || ""; const due = item.metadata?.due_date ? ` · 截止 ${esc(fmt(item.metadata.due_date))}` : ""; const privacy = item.metadata?.privacy === "aggregated_duration_only" ? " · 仅保存聚合时长" : ""; return `<label class="integration-item"><input type="checkbox" data-integration-item="${esc(item.id)}" checked /><span><strong>${kind ? `${esc(kind)} · ` : ""}${esc(item.title || "未命名条目")}</strong><small>${esc(item.summary || "")}${time ? ` · 数据时间 ${esc(fmt(time))}` : ""}${due}${privacy}</small>${item.url ? `<a href="${esc(item.url)}" target="_blank" rel="noopener">查看来源 ↗</a>` : ""}</span></label>`; }).join("") : '<div class="platform-list-empty">没有可导入的未读、开放、搜索或最近条目。</div>';
}
async function setupGithubToolsPage() {
  let currentIntegration = null; let lastItems = []; let integrationCatalog = []; let lastIntegrationTrigger = null;
  const importButton = () => qs("#import-integration-items");
  const updateImportButton = () => {
    const button = importButton();
    const toggle = qs("#toggle-integration-selection");
    if (!button || qs("#integration-items-panel")?.classList.contains("hidden")) return;
    const items = qsa("[data-integration-item]");
    const count = items.filter((input) => input.checked).length;
    button.textContent = count ? `导入选中 ${count} 条` : "先选择内容";
    button.disabled = count === 0;
    if (toggle) toggle.textContent = items.length && count === items.length ? "取消全选" : "全选当前内容";
  };
  const load = async () => { const body = await api("/api/github-tools"); integrationCatalog = body.integrations || []; renderTools(body.tools || [], body.trials || [], integrationCatalog); const tools = body.tools || []; const integrated = tools.filter((tool) => tool.state === "integrated").length; const count = qs("#tool-count"); if (count) count.textContent = `已接入 ${integrated} · 共 ${tools.length}`; return body; };
  const reload = async (message = "") => { const body = await load(); pageNotice(message || `已读取 ${body.tools?.length || 0} 个工具、${body.trials?.length || 0} 条试用记录、${body.integrations?.length || 0} 个效率集成`, "success"); return body; };
  await reload();
  // 从首页待办「试用」卡片跳转过来时，?tool=<id> 定位并高亮对应工具卡片。
  const focusTool = new URLSearchParams(window.location.search).get("tool");
  if (focusTool) {
    window.setTimeout(() => {
      const card = qs(`[data-trial-tool="${CSS.escape(focusTool)}"]`)?.closest(".tool-card");
      if (!card) return;
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("tool-card-focus");
      window.setTimeout(() => card.classList.remove("tool-card-focus"), 2600);
    }, 400);
  }
  qs("#tool-grid")?.addEventListener("click", async (event) => { const button = event.target.closest("[data-trial-tool]"); if (!button) return; setBusy(button, true, "登记中…"); try { await api(`/api/github-tools/${encodeURIComponent(button.dataset.trialTool)}/trial`, { method: "POST" }); await reload("已登记为工作台待办。接下来：①去首页「现在要处理」查看这条试用记录 ②按卡片上的「试用」建议体验 ③觉得有用就告诉我，帮你正式接入"); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } });
  qs("#integration-grid")?.addEventListener("click", (event) => { const button = event.target.closest("[data-open-integration]"); if (!button) return; lastIntegrationTrigger = button; currentIntegration = integrationCatalog.find((item) => item.id === button.dataset.openIntegration) || null; if (currentIntegration) { renderIntegrationFields(currentIntegration); qs("#integration-panel")?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" }); window.setTimeout(() => qs("#integration-fields input")?.focus(), 0); } });
  qs("#close-integration")?.addEventListener("click", () => { qs("#integration-panel")?.classList.add("hidden"); lastIntegrationTrigger?.focus(); });
  qs("#integration-form")?.addEventListener("submit", async (event) => { event.preventDefault(); if (!currentIntegration) return; const values = Object.fromEntries(qsa("[data-integration-field]", event.currentTarget).map((input) => [input.dataset.integrationField, input.value.trim()])); const button = event.currentTarget.querySelector("button[type=submit]"); setBusy(button, true, "保存中…"); try { const body = await api(`/api/integrations/${encodeURIComponent(currentIntegration.id)}/config`, jsonOptions({ values, enabled: true })); currentIntegration = body.integration; renderIntegrationFields(currentIntegration); try { const refreshed = await reload("配置已保存"); currentIntegration = (refreshed.integrations || []).find((item) => item.id === currentIntegration.id) || currentIntegration; renderIntegrationFields(currentIntegration); } catch (refreshError) { pageNotice(`配置已保存，但列表刷新失败：${refreshError.message}。可稍后重新加载。`, "warning"); } } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } });
  qs("#test-integration")?.addEventListener("click", async (event) => { if (!currentIntegration) return; const button = event.currentTarget; setBusy(button, true, "测试中…"); try { const body = await api(`/api/integrations/${encodeURIComponent(currentIntegration.id)}/test`, { method: "POST" }); currentIntegration = body.integration; renderIntegrationFields(currentIntegration); pageNotice(body.message || "连接测试成功", "success"); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } });
  qs("#send-ntfy-test")?.addEventListener("click", async (event) => { if (!currentIntegration || currentIntegration.id !== "ntfy") return; const button = event.currentTarget; setBusy(button, true, "发送中…"); try { const body = await api("/api/integrations/ntfy/notify", jsonOptions({ title: "Workbench 测试通知", body: "ntfy 远程通知已连通。", href: "/" })); pageNotice(body.message || "测试通知已发送", "success"); const refreshed = await reload(); currentIntegration = (refreshed.integrations || []).find((item) => item.id === "ntfy") || currentIntegration; renderIntegrationFields(currentIntegration); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } });
  qs("#load-integration-items")?.addEventListener("click", async (event) => { if (!currentIntegration) return; const button = event.currentTarget; setBusy(button, true, "读取中…"); try { const body = await api(`/api/integrations/${encodeURIComponent(currentIntegration.id)}/items?limit=20`); lastItems = body.items || []; renderIntegrationItems(lastItems); qs("#integration-items-panel")?.classList.remove("hidden"); updateImportButton(); pageNotice(`已读取 ${lastItems.length} 条可导入内容；勾选后才会写入工作台`, "success"); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } });
  qs("#integration-items")?.addEventListener("change", updateImportButton);
  qs("#toggle-integration-selection")?.addEventListener("click", (event) => { const inputs = qsa("[data-integration-item]"); if (!inputs.length) return; const selectAll = inputs.some((input) => !input.checked); inputs.forEach((input) => { input.checked = selectAll; }); updateImportButton(); event.currentTarget.focus(); });
  qs("#import-integration-items")?.addEventListener("click", async (event) => { if (!currentIntegration) return; const ids = qsa("[data-integration-item]:checked").map((input) => input.dataset.integrationItem); if (!ids.length) { pageNotice("先选择至少一条内容", "warning"); return; } const button = event.currentTarget; setBusy(button, true, "导入中…"); try { const body = await api(`/api/integrations/${encodeURIComponent(currentIntegration.id)}/import`, jsonOptions({ ids })); const target = body.target_project === "inbox" ? "收件箱" : body.target_project === "knowledge" ? "知识库" : body.target_project === "crawl4ai" ? "网页研究" : "工作台"; pageNotice(`已导入 ${body.created || 0} 条工作项，已进入${target}${body.skipped?.length ? `；重复 ${body.skipped.length} 条已跳过` : ""}。回到${target}继续处理。`, "success"); try { const refreshed = await api(`/api/integrations/${encodeURIComponent(currentIntegration.id)}/items?limit=20`); lastItems = refreshed.items || []; renderIntegrationItems(lastItems); updateImportButton(); } catch (refreshError) { pageNotice(`已导入到${target}，但列表刷新失败：${refreshError.message}。可点击“读取最新条目”重试。`, "warning"); } } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); updateImportButton(); } });
}

function approvalStatusLabel(status) { return ({ pending: "待审批", approved: "已批准", rejected: "已退回", changes_requested: "要求修改", resubmitted: "已重新提交" })[status] || status || "未知"; }
function renderApprovals(items = []) {
  const list = qs("#approval-list");
  if (!list) return;
  if (!items.length) { list.innerHTML = '<div class="platform-list-empty">没有匹配的审批请求。文档交付或服务器动作审批生成后会在这里等待人工确认。</div>'; return; }
  list.innerHTML = items.map((item) => {
    const payload = item.payload || {};
    const formats = (payload.formats || []).join(" / ").toUpperCase() || item.kind;
    const artifacts = payload.delivery_artifacts || [];
    const decided = item.status === "approved";
    const isServerAction = item.kind === "server_action";
    const execution = item.execution || null;
    const canExecute = isServerAction && decided && (!execution || ["failed", "rolled_back"].includes(execution.status));
    const canRollback = isServerAction && execution?.rollback_available;
    const canDecide = !decided && ["pending", "resubmitted"].includes(item.status);
    const canResubmit = ["changes_requested", "rejected"].includes(item.status);
    const history = (item.history || []).slice(-5).map((event) => `${approvalStatusLabel(event.from_status)} → ${approvalStatusLabel(event.to_status)} · ${fmt(event.created_at)}`).join("；");
    const executionDetail = isServerAction ? `<div class="approval-detail">执行状态：${esc(execution ? execution.status : "尚未执行")}${execution?.error ? ` · ${esc(execution.error)}` : ""}${execution?.result?.message ? ` · ${esc(execution.result.message)}` : ""}</div>` : "";
    const serverActions = isServerAction && decided ? `<div class="approval-actions">${badge(execution ? `执行：${execution.status}` : "审批已通过", execution?.status === "succeeded" ? "approved" : execution?.status === "failed" ? "rejected" : "pending")}${canExecute ? `<button class="platform-button" type="button" data-execute-server="${esc(item.id)}">执行安全动作</button>` : ""}${canRollback ? `<button class="platform-button-secondary" type="button" data-rollback-server="${esc(execution.id)}">回退本地快照</button>` : ""}</div>` : "";
    return `<article class="approval-row"><div class="approval-main"><div class="platform-row-title"><h3>${esc(item.title)}</h3>${badge(approvalStatusLabel(item.status), item.status)}</div><p>${esc(payload.title || payload.action || "交付或人工确认请求")} · ${esc(formats)}</p><div class="approval-meta"><span>项目：${esc(item.project_id || "workbench")}</span><span>产物：${esc(artifacts.length)} 个</span><span>更新：${esc(fmt(item.updated_at || item.created_at))}</span></div>${history ? `<div class="approval-detail">状态历史：${esc(history)}</div>` : ""}${item.reviewer_note ? `<div class="approval-detail">修改/审批意见：${esc(item.reviewer_note)}</div>` : ""}${artifacts.length ? `<div class="approval-detail">Artifact IDs：${artifacts.map((id) => `<code>#${esc(id)}</code>`).join("、")}</div>` : ""}${executionDetail}</div>${serverActions || (decided ? `<div class="approval-actions">${badge("本地状态已完成", "approved")}</div>` : canDecide ? `<div class="approval-actions"><textarea class="approval-note" data-approval-note="${esc(item.id)}" placeholder="审批意见（可选）"></textarea><button class="platform-button" type="button" data-approve="${esc(item.id)}">批准交付</button><button class="platform-button-danger" type="button" data-reject="${esc(item.id)}">退回修改</button></div>` : canResubmit ? `<div class="approval-actions"><p class="approval-detail">确认产物已按意见修改后，再标记为重新提交。</p><button class="platform-button-secondary" type="button" data-resubmit="${esc(item.id)}">标记已重新提交</button></div>` : `<div class="approval-actions">${badge("等待重新提交", "changes_requested")}</div>`)}</article>`;
  }).join("");
}
async function setupApprovalsPage() {
  const filter = qs("#approval-filter");
  const load = async () => { const body = await api(`/api/approvals?status=${encodeURIComponent(filter?.value || "all")}`); renderApprovals(body.approvals || []); const pending = (body.approvals || []).filter((item) => ["pending", "resubmitted"].includes(item.status)).length; const count = qs("#pending-count"); if (count) count.textContent = pending; return body; };
  const initial = await load(); pageNotice(`已读取 ${initial.approvals?.length || 0} 条审批请求`, "success"); filter?.addEventListener("change", () => load().catch((error) => pageNotice(error.message, "error")));
  qs("#approval-list")?.addEventListener("click", async (event) => {
    const approve = event.target.closest("[data-approve]"); const reject = event.target.closest("[data-reject]"); const resubmit = event.target.closest("[data-resubmit]"); const execute = event.target.closest("[data-execute-server]"); const rollback = event.target.closest("[data-rollback-server]");
    if (!approve && !reject && !resubmit && !execute && !rollback) return;
    if (execute || rollback) {
      const button = execute || rollback; setBusy(button, true, execute ? "执行中…" : "回退中…");
      try { const body = await api(execute ? `/api/server/actions/${encodeURIComponent(execute.dataset.executeServer)}/execute` : `/api/server/actions/executions/${encodeURIComponent(rollback.dataset.rollbackServer)}/rollback`, { method: "POST" }); pageNotice(body.message || (execute ? "安全动作已记录" : "已回退本地快照"), body.ok ? "success" : "warning"); await load(); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); }
      return;
    }
    const id = approve?.dataset.approve || reject?.dataset.reject || resubmit?.dataset.resubmit; const note = qs(`[data-approval-note="${CSS.escape(id)}"]`)?.value || ""; const button = approve || reject || resubmit; const nextStatus = approve ? "approved" : reject ? "changes_requested" : "resubmitted"; setBusy(button, true, "保存中…");
    try { await api(`/api/approvals/${encodeURIComponent(id)}`, jsonOptions({ status: nextStatus, reviewer_note: note }, "PATCH")); pageNotice(approve ? "已批准本地状态；服务器动作还需点击执行" : reject ? "已记录修改意见，等待重新提交" : "已标记为重新提交，回到审批队列", "success"); await load(); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); }
  });
  await loadApprovalQueue();
  qs("#refresh-approval-queue")?.addEventListener("click", async (event) => { const button = event.currentTarget; setBusy(button, true, "刷新中…"); try { await loadApprovalQueue(); pageNotice("待确认队列已刷新", "success"); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } });
  await setupPushPanel();
}
function renderApprovalQueue(items = []) {
  const list = qs("#approval-queue-list");
  if (!list) return;
  if (!items.length) { list.innerHTML = '<div class="platform-list-empty">没有待确认的工作项或 Agent 动作。审批请求、blocked 工作项和待确认动作会在这里等待你。</div>'; return; }
  list.innerHTML = items.map((item) => {
    const typeLabel = item.type === "approval" ? "审批" : item.type === "work_item" ? "工作项" : "Agent 动作";
    const project = item.project_id || "workbench";
    const statusLabel = item.status === "blocked" ? "待确认" : item.status === "pending" ? "待确认" : item.status;
    return `<article class="approval-row"><div class="approval-main"><div class="platform-row-title"><h3>${esc(item.title)}</h3>${badge(typeLabel, "pending")}</div><div class="approval-meta"><span>项目：${esc(project)}${item.target_project ? ` → ${esc(item.target_project)}` : ""}</span><span>状态：${esc(statusLabel)}</span><span>更新：${esc(fmt(item.updated_at))}</span></div></div><div class="approval-actions"><a class="platform-button" href="${esc(item.href || "/")}">前往处理</a></div></article>`;
  }).join("");
}
async function loadApprovalQueue() {
  const list = qs("#approval-queue-list");
  if (!list) return;
  try {
    const body = await api("/api/approval-queue");
    renderApprovalQueue(body.items || []);
  } catch (error) {
    list.innerHTML = `<div class="platform-list-empty">待确认队列读取失败：${esc(error.message)}。点击刷新重试。</div>`;
  }
}
function urlBase64ToUint8Array(value) { const padding = "=".repeat((4 - (value.length % 4)) % 4); const raw = atob((value + padding).replaceAll("-", "+").replaceAll("_", "/")); return Uint8Array.from([...raw].map((char) => char.charCodeAt(0))); }
function renderPushDeliveries(items = []) {
  const list = qs("#push-delivery-list");
  if (!list) return;
  if (!items.length) {
    list.innerHTML = '<div class="platform-list-empty">还没有送达记录。发送测试 Push 后会出现在这里。</div>';
    return;
  }
  list.innerHTML = items.slice(0, 30).map((item) => {
    const label = ({ sent: "已送达", failed: "失败", expired: "订阅失效", queued: "排队中" })[item.status] || item.status;
    const tone = item.status === "sent" ? "approved" : item.status === "expired" || item.status === "failed" ? "rejected" : "pending";
    const source = String(item.href || "");
    const sourceLink = source.startsWith("/") || source.startsWith("https://")
      ? `<a href="${esc(source)}" target="_blank" rel="noopener">查看来源 ↗</a>`
      : "";
    const nextStep = item.status === "failed" ? "下一步：检查 Push 配置后重试；仍失败时重新订阅浏览器。" : item.status === "expired" ? "下一步：回到当前浏览器重新订阅。" : "";
    return `<article class="platform-row"><div class="platform-row-main"><div class="platform-row-title"><strong>${esc(item.title || "Push 通知")}</strong>${badge(label, tone)}</div><p>${esc(item.error || (item.status === "sent" ? "浏览器服务已接受送达请求" : "等待处理"))}</p>${nextStep ? `<small class="runtime-error-note">${esc(nextStep)}</small>` : ""}<div class="platform-row-meta"><span>尝试 ${esc(item.attempts || 0)} 次</span><span>${esc(fmt(item.updated_at || item.created_at))}</span>${sourceLink ? `<span>${sourceLink}</span>` : ""}</div></div><div class="platform-row-actions">${item.status === "failed" ? `<button type="button" data-retry-push="${esc(item.id)}">重试</button>` : ""}</div></article>`;
  }).join("");
}
async function loadPushDeliveries() { const body = await api("/api/push/deliveries?limit=60"); renderPushDeliveries(body.deliveries || []); return body; }
async function setupPushPanel() { const state = qs("#push-state"); const quietStart = qs("#push-quiet-start"); const quietEnd = qs("#push-quiet-end"); const refresh = async () => { const [config, subscriptions] = await Promise.all([api("/api/push/config"), api("/api/push/subscriptions")]); const items = subscriptions.subscriptions || []; const latest = items.find((item) => item.last_error) || items[0]; if (state) state.innerHTML = `<strong>${items.length ? `已保存 ${items.length} 个浏览器订阅` : "尚未保存浏览器订阅"}</strong><small>${config.configured ? `VAPID 私钥来源：${esc(config.private_key_source === "file" ? "服务端文件" : "环境变量")}` : "VAPID 私钥缺失：订阅可以保存，但不会送达。"}</small><small>${config.proxy_configured ? "推送网络代理：已配置" : "推送网络代理：未配置（服务器需能直连浏览器 Push 服务）"}</small>${config.public_key ? `<code>公钥已提供 · ${esc(config.public_key.slice(0, 18))}…</code>` : "<small>VAPID 公钥缺失：无法从此页面新建订阅。</small>"}${latest?.last_error ? `<small class="runtime-error-note">最近一次送达失败：${esc(latest.last_error)}</small>` : ""}`; const current = items[0]; if (current) { quietStart.value = current.quiet_start || "22:00"; quietEnd.value = current.quiet_end || "08:00"; } }; await Promise.all([refresh(), loadPushDeliveries()]); qs("#refresh-push-deliveries")?.addEventListener("click", () => loadPushDeliveries().catch((error) => pageNotice(error.message, "error"))); qs("#push-delivery-list")?.addEventListener("click", async (event) => { const button = event.target.closest("[data-retry-push]"); if (!button) return; setBusy(button, true, "重试中…"); try { const body = await api(`/api/push/deliveries/${encodeURIComponent(button.dataset.retryPush)}/retry`, { method: "POST" }); pageNotice(body.ok ? "Push 已送达" : body.delivery?.error || "重试仍未送达", body.ok ? "success" : "warning"); await Promise.all([loadPushDeliveries(), refresh()]); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } }); qs("#subscribe-push")?.addEventListener("click", async (event) => { const button = event.currentTarget; setBusy(button, true, "订阅中…"); try { const config = await api("/api/push/config"); if (!config.public_key) throw new Error("服务器未提供 WORKBENCH_VAPID_PUBLIC_KEY"); if (!("serviceWorker" in navigator) || !("PushManager" in window)) throw new Error("当前浏览器不支持 Web Push"); const registration = await navigator.serviceWorker.ready; const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(config.public_key) }); const json = subscription.toJSON(); await api("/api/push/subscriptions", jsonOptions({ endpoint: json.endpoint, keys: json.keys || {}, user_agent: navigator.userAgent, quiet_start: quietStart.value || "22:00", quiet_end: quietEnd.value || "08:00", enabled: true })); pageNotice("浏览器订阅已保存", "success"); await refresh(); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } }); qs("#save-push-window")?.addEventListener("click", async (event) => { const button = event.currentTarget; setBusy(button, true, "保存中…"); try { const registration = await navigator.serviceWorker.ready; const subscription = await registration.pushManager.getSubscription(); if (!subscription) throw new Error("当前没有浏览器订阅，请先订阅"); const json = subscription.toJSON(); await api("/api/push/subscriptions", jsonOptions({ endpoint: json.endpoint, keys: json.keys || {}, user_agent: navigator.userAgent, quiet_start: quietStart.value || "22:00", quiet_end: quietEnd.value || "08:00", enabled: true })); pageNotice("静默时段已保存", "success"); await refresh(); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } }); qs("#send-push-test")?.addEventListener("click", async (event) => { const button = event.currentTarget; setBusy(button, true, "发送中…"); try { const body = await api("/api/push/test", { method: "POST" }); pageNotice(body.message || `已发送 ${body.sent || 0} 条`, body.ok ? "success" : "warning"); await loadPushDeliveries(); } catch (error) { pageNotice(error.message, "error"); } finally { setBusy(button, false); } }); }

const page = document.body.dataset.page;
setupThemeToggle();
document.addEventListener("click", (event) => { if (event.target.closest("[data-retry-page]")) location.reload(); });
if (page === "automation") setupAutomationPage().catch((error) => { pageNotice(error.message, "error"); renderLoadError(["#automation-rules", "#plan-list", "#capability-grid", "#worker-status-list", "#llm-runtime-metrics"], error.message); });
if (page === "git") setupGitPage().catch((error) => { status("git-status", error.message, "error"); renderLoadError(["#repo-grid"], error.message); });
if (page === "github-tools") setupGithubToolsPage().catch((error) => { pageNotice(error.message, "error"); renderLoadError(["#integration-grid", "#tool-grid", "#trial-list"], error.message); });
if (page === "approvals") setupApprovalsPage().catch((error) => { pageNotice(error.message, "error"); renderLoadError(["#approval-list", "#push-state", "#push-delivery-list"], error.message); });
