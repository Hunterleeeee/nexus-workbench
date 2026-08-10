const $ = (selector) => document.querySelector(selector);

const state = {
  runId: null,
  run: null,
  activeDoc: 0,
  polling: false,
};

const requestJson = window.WorkbenchUX?.requestJson || (async (url, options = {}) => {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求未完成（${response.status}）`);
  return body;
});

const els = {
  form: $("#crawl-form"),
  task: $("#task"),
  urls: $("#urls"),
  renderJs: $("#render-js"),
  refresh: $("#refresh"),
  maxDepth: $("#max-depth"),
  maxPages: $("#max-pages"),
  crawlButton: $("#crawl-button"),
  systemStatus: $("#system-status"),
  configNote: $("#config-note"),
  runBadge: $("#run-badge"),
  runStatus: $("#run-status"),
  metricPages: $("#metric-pages"),
  metricPageNote: $("#metric-page-note"),
  metricTime: $("#metric-time"),
  metricEngine: $("#metric-engine"),
  metricModel: $("#metric-model"),
  historyList: $("#crawl-history-list"),
  historySummary: $("#crawl-history-summary"),
  queueSummary: $("#crawl-queue-summary"),
  queueList: $("#crawl-queue-list"),
  refreshResearchOps: $("#refresh-research-ops"),
  researchPlanForm: $("#research-plan-form"),
  researchPlanTitle: $("#research-plan-title"),
  researchPlanQuery: $("#research-plan-query"),
  researchPlanUrls: $("#research-plan-urls"),
  researchPlanMessage: $("#research-plan-message"),
  researchPlanList: $("#research-plan-list"),
  observabilitySummary: $("#crawl-observability-summary"),
  observabilityDetails: $("#crawl-observability-details"),
  refreshObservability: $("#refresh-crawl-observability"),
  evidenceArtifactIds: $("#crawl-evidence-artifact-ids"),
  evidenceQuestion: $("#crawl-evidence-question"),
  evidenceOutput: $("#crawl-evidence-output"),
  evidenceTarget: $("#crawl-handoff-target"),
  sourceList: $("#source-list"),
  documentView: $("#document-view"),
  activityLog: $("#activity-log"),
  copyButton: $("#copy-button"),
  chatMessages: $("#chat-messages"),
  chatForm: $("#chat-form"),
  chatInput: $("#chat-input"),
  chatButton: $("#chat-button"),
  chatModelLabel: $("#chat-model-label"),
  keyState: $("#llm-key-state"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function timeOnly(iso) {
  if (!iso) return "--:--:--";
  return new Date(iso).toLocaleTimeString("zh-CN", { hour12: false });
}

function setBusy(busy) {
  els.crawlButton.disabled = busy;
  els.crawlButton.querySelector("span:nth-child(2)").textContent = busy ? "正在工作…" : "开始爬取";
}

function applyLlmSettings(llm, hasPageKey = false) {
  const configured = Boolean(llm?.configured);
  if (els.keyState) els.keyState.textContent = llm?.primary_configured ? "主配置已保存" : hasPageKey ? "Fallback 已配置" : configured ? "环境变量已配置" : "未配置";
  els.metricModel.textContent = configured ? llm.model : "—";
  els.chatModelLabel.textContent = configured ? llm.model : "LLM 未配置";
}


function setStatus(status, label) {
  els.runBadge.className = `run-badge ${status || ""}`;
  const dotClass = status === "running" ? "running" : status === "failed" ? "error" : status === "completed" ? "" : "muted";
  els.runBadge.querySelector(".status-dot").className = `status-dot ${dotClass}`;
  els.runStatus.textContent = label || ({ queued: "排队中", running: "抓取中", completed: "已完成", failed: "失败" }[status] || "等待任务");
}

function renderLogs(logs = []) {
  if (!logs.length) {
    els.activityLog.innerHTML = '<div class="log-placeholder">任务日志会在这里实时出现</div>';
    return;
  }
  els.activityLog.innerHTML = logs.map((log) => `
    <div class="log-item ${escapeHtml(log.level)}"><span class="log-time">${timeOnly(log.at)}</span><span class="log-message">${escapeHtml(log.message)}</span></div>
  `).join("");
  els.activityLog.scrollTop = els.activityLog.scrollHeight;
}

function renderDocuments(documents = []) {
  if (!documents.length) {
    els.sourceList.className = "source-list empty-state";
    els.sourceList.innerHTML = '<div class="empty-mark">01</div><strong>还没有研究材料</strong><p>填入地址后，抓取到的页面会出现在这里。</p>';
    els.documentView.classList.add("hidden");
    els.copyButton.disabled = true;
    return;
  }
  els.sourceList.className = "source-list";
  els.sourceList.innerHTML = documents.map((doc, index) => `
    <button type="button" class="source-item ${index === state.activeDoc ? "active" : ""}" data-doc-index="${index}">
      <div class="source-item-top"><span class="source-index">${String(index + 1).padStart(2, "0")}</span><span class="source-title">${escapeHtml(doc.title || "未命名页面")}</span></div>
      <div class="source-meta">${doc.success ? "✓" : "×"} ${escapeHtml(doc.url)} · ${Number(doc.markdown_chars || 0).toLocaleString()} chars${doc.source_quality?.label ? ` · 质量 ${escapeHtml(doc.source_quality.label)}` : ""}</div>
    </button>
  `).join("");
  renderDocument(documents[state.activeDoc]);
}

function renderDocument(doc) {
  if (!doc) return;
  els.documentView.classList.remove("hidden");
  const locator = doc.source_locator || {};
  const headings = (locator.headings || []).slice(0, 12);
  const quality = doc.source_quality || {};
  const locatorText = `${Number(locator.line_count || 0).toLocaleString()} 行${headings.length ? ` · ${headings.length} 个标题` : ""}`;
  els.documentView.innerHTML = `
    <div class="document-head"><h4>${escapeHtml(doc.title || "未命名页面")}</h4><a href="${escapeHtml(doc.url)}" target="_blank" rel="noreferrer">${escapeHtml(doc.url)}</a></div>
    <div class="source-evidence-meta"><span>来源质量：${escapeHtml(quality.label || "未评估")}</span><span>正文定位：${escapeHtml(locatorText)}</span>${doc.status_code ? `<span>HTTP ${escapeHtml(doc.status_code)}</span>` : ""}</div>
    ${headings.length ? `<details class="source-locator"><summary>查看标题定位</summary><ol>${headings.map((item) => `<li>第 ${escapeHtml(item.line)} 行 · ${escapeHtml(item.text)}</li>`).join("")}</ol></details>` : ""}
    <div class="document-body">${escapeHtml(doc.markdown || doc.error_message || "页面没有可显示内容")}</div>
  `;
  els.copyButton.disabled = !doc.markdown;
}

function renderResultContract(contract = {}) {
  const sections = contract.sections || {};
  const labels = { facts: "事实", judgement: "判断", evidence: "证据", risks: "风险", actions: "动作", next_steps: "下一步" };
  const entries = Object.entries(labels).filter(([key]) => Array.isArray(sections[key]) && sections[key].length);
  if (!contract.summary && !entries.length) return null;
  const node = document.createElement("details");
  node.className = "research-result-contract";
  node.open = true;
  const body = entries.map(([key, label]) => `<div><strong>${label}</strong><ul>${sections[key].slice(0, 8).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`).join("");
  const citations = (contract.citations || []).slice(0, 8).map((item) => item.type === "url" ? `<a href="${escapeHtml(item.value)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.label || item.value)}</a>` : `<span>${escapeHtml(item.value)}</span>`).join(" · ");
  const refs = (contract.source_refs || []).slice(0, 8).map((item) => { const label = `${item.label || item.id || "未命名来源"}${item.data_as_of ? ` · ${item.data_as_of}` : ""}`; return String(item.locator || "").startsWith("http") ? `<a href="${escapeHtml(item.locator)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>` : `<span>${escapeHtml(label)}</span>`; }).join(" · ");
  const coverage = contract.source_coverage || {};
  const coverageText = coverage.total ? `引用覆盖：${coverage.with_locator || 0}/${coverage.total} 可定位 · ${coverage.with_data_time || 0}/${coverage.total} 有数据时间` : "";
  const trace = [contract.data_as_of ? `数据时间：${escapeHtml(contract.data_as_of)}` : "", refs ? `来源：${refs}` : "", coverageText, contract.artifact_ids?.length ? `Artifact ${contract.artifact_ids.length} 份` : "", contract.work_item_ids?.length ? `WorkItem ${contract.work_item_ids.length} 条` : "", contract.relation_ids?.length ? `Relation ${contract.relation_ids.length} 条` : "", contract.replay?.href ? `<a href="${escapeHtml(contract.replay.href)}" target="_blank" rel="noopener noreferrer">查看 Run 回放</a>` : ""].filter(Boolean).join(" · ");
  node.innerHTML = `<summary>结构化结果 · ${escapeHtml(contract.summary || "查看结论与证据")}</summary>${body || `<p>${escapeHtml(contract.summary || "暂无结构化摘要")}</p>`}${citations ? `<div class="research-citations"><strong>可回溯来源</strong><p>${citations}</p></div>` : ""}${trace ? `<div class="research-citations"><strong>审计链</strong><p>${trace}</p></div>` : ""}`;
  return node;
}

function renderRun(run) {
  state.run = run;
  if (els.evidenceArtifactIds && run.artifact_id && !els.evidenceArtifactIds.value.trim()) els.evidenceArtifactIds.value = String(run.artifact_id);
  setStatus(run.status);
  renderLogs(run.logs);
  renderDocuments(run.documents);
  els.metricPages.textContent = run.documents.length ? `${run.documents.length}` : "—";
  els.metricPageNote.textContent = run.documents.length ? `${run.documents.filter((doc) => doc.success).length} 个成功` : "尚未开始";
  els.metricTime.textContent = formatTime(run.elapsed_ms);
  els.metricEngine.textContent = run.render_js ? "Browser" : "HTTP";
  els.metricModel.textContent = run.analysis_status || "等待分析";
  const changes = (run.change_detection || []).filter((item) => item.state && item.state !== "unchanged");
  if (changes.length && run.status === "completed") {
    els.configNote.innerHTML = `<span class="note-icon">i</span><span>本次有 ${changes.length} 个来源是新增或内容有变化，建议先核对来源再引用。</span>`;
  }
  if (run.status === "failed") {
    els.documentView.classList.remove("hidden");
    els.documentView.innerHTML = `<div class="document-body" style="color:var(--danger)">${escapeHtml(run.error || "任务失败")}</div>`;
  }
  if (run.initial_analysis && !run.initialAnalysisShown) {
    appendMessage("assistant", run.initial_analysis, "首轮分析");
    const contractNode = renderResultContract(run.initial_result_contract || {});
    if (contractNode) els.chatMessages.appendChild(contractNode);
    run.initialAnalysisShown = true;
  }
  const complete = run.status === "completed";
  els.chatInput.disabled = !complete;
  els.chatButton.disabled = !complete;
  setBusy(run.status === "queued" || run.status === "running");
}

function evidenceIds() {
  return (els.evidenceArtifactIds?.value || "").split(/[,，\s]+/).map((value) => Number(value)).filter((value) => Number.isInteger(value) && value > 0);
}

els.evidenceOutput?.addEventListener("click", (event) => {
  const link = event.target.closest("[data-clear-evidence]");
  if (link && els.evidenceArtifactIds) els.evidenceArtifactIds.value = "";
});

document.querySelector("#crawl-compare-evidence")?.addEventListener("click", async (event) => {
  const button = event.currentTarget; const ids = evidenceIds();
  if (ids.length < 2) { if (els.evidenceOutput) els.evidenceOutput.textContent = "比较至少需要两个 Artifact ID；当前研究结果可先作为一个来源，再补充另一份来源。"; return; }
  button.disabled = true; if (els.evidenceOutput) els.evidenceOutput.textContent = "正在建立证据比较记录…";
  try { const body = await requestJson("/api/evidence/compare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ artifact_ids: ids, question: els.evidenceQuestion?.value.trim() || "", project_id: "crawl4ai" }) }); if (els.evidenceOutput) els.evidenceOutput.textContent = `已保存证据比较 Artifact #${body.artifact?.id || "—"} · 可用来源 ${body.bundle?.coverage?.available || 0} 份。`; } catch (error) { if (els.evidenceOutput) els.evidenceOutput.textContent = error.message; } finally { button.disabled = false; }
});

document.querySelector("#crawl-handoff-evidence")?.addEventListener("click", async (event) => {
  const button = event.currentTarget; const ids = evidenceIds();
  if (ids.length < 1) { if (els.evidenceOutput) els.evidenceOutput.textContent = "先填写 Artifact ID，或完成一次研究任务后再交接。"; return; }
  if (!window.confirm(`确认把 ${ids.length} 份 Artifact 交给${els.evidenceTarget?.selectedOptions?.[0]?.textContent || "目标 Agent"}？`)) return;
  button.disabled = true; if (els.evidenceOutput) els.evidenceOutput.textContent = "正在建立跨项目交接…";
  try { const body = await requestJson("/api/evidence/handoff", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ artifact_ids: ids, target_project: els.evidenceTarget?.value || "knowledge", title: "Crawl4AI 证据包交接", instruction: els.evidenceQuestion?.value.trim() || "请基于证据包继续分析，并保留来源与数据时间。", confirmed: true }) }); if (els.evidenceOutput) els.evidenceOutput.textContent = `${body.message || "证据包已交接"} WorkItem #${body.item?.id || "—"}。`; } catch (error) { if (els.evidenceOutput) els.evidenceOutput.textContent = error.message; } finally { button.disabled = false; }
});

function renderCrawlHistory(items = [], summary = {}) {
  if (!els.historyList) return;
  const failed = Number(summary.failed || 0);
  const active = Number(summary.active || 0);
  els.historySummary.textContent = active ? `${active} 个进行中${failed ? ` · ${failed} 个失败` : ""}` : failed ? `${failed} 个失败，可重试` : `${items.length} 条记录`;
  if (!items.length) {
    els.historyList.innerHTML = '<div class="history-empty">完成或失败的研究任务会保存在这里。</div>';
    return;
  }
  const statusNames = { queued: "排队中", running: "运行中", succeeded: "已完成", partial: "部分完成", failed: "失败" };
  els.historyList.innerHTML = items.slice(0, 8).map((run) => `<article class="crawl-history-item ${escapeHtml(run.status || "queued")}"><button class="history-open" type="button" data-history-open="${escapeHtml(run.id)}"><span class="history-state"><i></i>${escapeHtml(run.status_label || statusNames[run.status] || run.status)}</span><strong>${escapeHtml(run.title || "网页研究")}</strong><small>${escapeHtml(timeOnly(run.updated_at || run.created_at))} · 第 ${escapeHtml(run.attempt || 1)}/${escapeHtml(run.max_attempts || 1)} 次</small></button><div class="history-actions">${run.retryable ? `<button class="history-retry" type="button" data-history-retry="${escapeHtml(run.id)}">重试</button>` : ""}</div></article>`).join("");
}

function renderResearchQueue(body = {}) {
  if (!els.queueList) return;
  const active = Number(body.active || 0); const running = Number((body.running || []).length); const queued = Number((body.queued || []).length);
  if (els.queueSummary) els.queueSummary.textContent = `${active}/${body.limit || 2} 活跃 · ${running} 执行中 · ${queued} 排队 · 可用 ${body.available ?? 0}`;
  const items = [...(body.running || []), ...(body.queued || [])];
  els.queueList.innerHTML = items.length ? items.map((run) => `<div class="research-queue-row"><div><strong>${escapeHtml(run.title || "网页研究")}</strong><small>${escapeHtml(run.status === "running" ? "正在抓取" : "等待执行")} · ${escapeHtml(run.id || "")}</small></div><button type="button" data-crawl-cancel="${escapeHtml(run.id)}">取消</button></div>`).join("") : '<div class="history-empty">当前没有排队或执行中的抓取任务。</div>';
}

function renderResearchPlans(plans = []) {
  if (!els.researchPlanList) return;
  const statusNames = { draft: "草稿", queued: "排队中", running: "运行中", succeeded: "已完成", failed: "失败", cancelled: "已取消" };
  els.researchPlanList.innerHTML = plans.length ? plans.slice(0, 6).map((plan) => `<div class="research-plan-row"><div><strong>${escapeHtml(plan.title || "未命名计划")}</strong><small>${escapeHtml(statusNames[plan.status] || plan.status || "草稿")} · ${escapeHtml((plan.urls || []).length)} 个来源 · ${escapeHtml(timeOnly(plan.updated_at || plan.created_at))}</small></div><button type="button" data-plan-run="${escapeHtml(plan.id)}" ${["queued", "running"].includes(plan.status) ? "disabled" : ""}>${plan.status === "succeeded" ? "再次运行" : "运行计划"}</button></div>`).join("") : '<div class="history-empty">还没有研究计划。</div>';
}

function renderCrawlObservability(body = {}) {
  if (!els.observabilityDetails) return;
  const counts = body.status_counts || {};
  const duration = body.duration_ms || {};
  const queue = body.queue || {};
  const worker = body.worker || {};
  const quality = Object.entries(body.source_quality?.distribution || {}).slice(0, 5).map(([label, count]) => `${escapeHtml(label)} ${escapeHtml(count)}`).join(" · ") || "暂无来源质量样本";
  const sampleLabel = body.sample_status_label || "观察窗口已读取";
  if (els.observabilitySummary) els.observabilitySummary.innerHTML = `<span>${escapeHtml(sampleLabel)} · ${escapeHtml(body.run_count || 0)} 次抓取</span><small>${escapeHtml(worker.status || "Worker 状态未知")} · 心跳 ${escapeHtml(worker.last_heartbeat ? timeOnly(worker.last_heartbeat) : "—")}</small>`;
  els.observabilityDetails.innerHTML = `<div class="observability-grid"><span><b>${escapeHtml(counts.succeeded || 0)}</b> 成功</span><span><b>${escapeHtml(counts.partial || 0)}</b> 部分完成</span><span><b>${escapeHtml(counts.failed || 0)}</b> 失败</span><span><b>${escapeHtml(body.retryable_failures || 0)}</b> 可重试</span><span><b>${duration.average == null ? "—" : `${escapeHtml(duration.average)}ms`}</b> 平均耗时</span><span><b>${escapeHtml(body.content_hash_changes || 0)}</b> 次内容变化</span></div><p class="observability-note">队列 ${escapeHtml(queue.running || 0)} 执行中 · ${escapeHtml(queue.queued || 0)} 排队 · 来源质量：${quality}</p><small class="observability-policy">${escapeHtml(body.policy || "")}</small>`;
}

async function loadCrawlQueue() {
  if (!els.queueList) return;
  try {
    renderResearchQueue(await requestJson("/api/crawl/queue"));
  } catch (error) {
    if (els.queueSummary) els.queueSummary.textContent = "队列读取失败";
    els.queueList.innerHTML = `<div class="history-empty">${escapeHtml(error.message)} · 可点击刷新重试</div>`;
  }
}

async function loadCrawlPlans() {
  if (!els.researchPlanList) return;
  try {
    const body = await requestJson("/api/crawl/plans?limit=12");
    renderResearchPlans(body.plans || []);
  } catch (error) {
    els.researchPlanList.innerHTML = `<div class="history-empty">研究计划读取失败：${escapeHtml(error.message)} · 可点击刷新重试</div>`;
  }
}

async function loadCrawlObservability() {
  if (!els.observabilityDetails) return;
  try {
    renderCrawlObservability(await requestJson("/api/crawl/observability?days=7"));
  } catch (error) {
    if (els.observabilitySummary) els.observabilitySummary.innerHTML = "<span>观察暂时不可用</span><small>队列与研究计划仍可单独使用</small>";
    els.observabilityDetails.innerHTML = `<div class="history-empty">${escapeHtml(error.message)} · 可点击“刷新观察”重试</div>`;
  }
}

async function loadResearchOps() {
  if (!els.queueList && !els.researchPlanList && !els.observabilityDetails) return;
  // Each panel owns its request and failure state. A missing observation
  // window must not hide an otherwise healthy queue or saved plan.
  await Promise.all([loadCrawlQueue(), loadCrawlPlans(), loadCrawlObservability()]);
}

async function loadCrawlHistory() {
  if (!els.historyList) return;
  try {
    const body = await requestJson("/api/agent/crawl4ai/runs?limit=8");
    renderCrawlHistory(body.runs || [], body.summary || {});
  } catch (error) {
    els.historySummary.textContent = "读取失败";
    els.historyList.innerHTML = `<div class="history-empty">${escapeHtml(error.message)}</div>`;
  }
}

function appendMessage(role, text, label) {
  const node = document.createElement("div");
  node.className = `chat-message ${role === "user" ? "user-message" : "assistant-message"}`;
  node.innerHTML = `<div class="message-label">${escapeHtml(label || (role === "user" ? "YOU" : "COPILOT"))}</div><p>${escapeHtml(text)}</p>`;
  els.chatMessages.appendChild(node);
  els.chatMessages.scrollTop = els.chatMessages.scrollHeight;
  return node;
}

async function pollRun() {
  if (!state.runId || state.polling) return;
  state.polling = true;
  try {
    const run = await requestJson(`/api/runs/${state.runId}`);
    renderRun(run);
    if (run.status === "queued" || run.status === "running") {
      state.polling = false;
      setTimeout(pollRun, 800);
    } else {
      state.polling = false;
      loadCrawlHistory();
    }
  } catch (error) {
    state.polling = false;
    els.configNote.innerHTML = `<span class="note-icon">!</span><span>${escapeHtml(error.message)}</span>`;
  }
}

async function startCrawl(event) {
  event.preventDefault();
  const urls = els.urls.value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
  if (!urls.length) {
    els.urls.focus();
    els.configNote.innerHTML = '<span class="note-icon">!</span><span>请先填写至少一个 http/https 地址。</span>';
    return;
  }
  setBusy(true);
  els.configNote.innerHTML = '<span class="note-icon pulse-icon">•</span><span>任务已提交，右侧会实时更新进度。</span>';
  els.chatMessages.innerHTML = '<div class="chat-message assistant-message"><div class="message-label">COPILOT</div><p>我先去读取这些页面。完成后可以继续问我。</p></div>';
  try {
    const body = await requestJson("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        urls,
        task: els.task.value.trim(),
        render_js: els.renderJs.checked,
        refresh: els.refresh.checked,
        max_depth: Number(els.maxDepth.value),
        max_pages: Number(els.maxPages.value),
      }),
    });
    state.runId = body.run_id;
    state.activeDoc = 0;
    els.copyButton.disabled = true;
    setStatus("queued");
    await pollRun();
    await loadCrawlHistory();
  } catch (error) {
    setBusy(false);
    els.configNote.innerHTML = `<span class="note-icon">!</span><span>${escapeHtml(error.message)}</span>`;
  }
}

async function sendChat(event) {
  event.preventDefault();
  const message = els.chatInput.value.trim();
  if (!message || !state.runId || !state.run || state.run.status !== "completed") return;
  appendMessage("user", message, "YOU");
  els.chatInput.value = "";
  els.chatInput.disabled = true;
  els.chatButton.disabled = true;
  const typing = document.createElement("div");
  typing.className = "typing";
  typing.textContent = "Agent 正在检索相关证据…";
  els.chatMessages.appendChild(typing);
  els.chatMessages.scrollTop = els.chatMessages.scrollHeight;
  try {
    const body = await requestJson("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: state.runId, message }),
    });
    typing.remove();
    const sourceCount = body.agent?.sources ?? 0;
    appendMessage("assistant", body.answer, `AGENT · 检索 ${sourceCount} 页`);
  } catch (error) {
    typing.remove();
    appendMessage("assistant", `分析失败：${error.message}`, "SYSTEM");
  } finally {
    els.chatInput.disabled = false;
    els.chatButton.disabled = false;
    els.chatInput.focus();
  }
}

async function loadHealth() {
  try {
    const [body, settingsBody] = await Promise.all([requestJson("/api/health"), requestJson("/api/settings/llm")]);
    els.systemStatus.textContent = body.crawl4ai_available ? "Crawl4AI 已连接" : "等待安装 Crawl4AI";
    applyLlmSettings(settingsBody.llm || body.llm, settingsBody.has_global_key);
    if (body.llm.configured) {
      els.configNote.innerHTML = '<span class="note-icon">✓</span><span>Crawl4AI 和 LLM 都已就绪，可以开始。</span>';
    } else {
      els.configNote.innerHTML = '<span class="note-icon">i</span><span>爬取不需要 LLM；若要对话分析，请配置全局 LLM。</span>';
    }
  } catch {
    els.systemStatus.textContent = "后端未连接";
  }
}

els.form.addEventListener("submit", startCrawl);
els.chatForm.addEventListener("submit", sendChat);
els.historyList?.addEventListener("click", async (event) => {
  const openButton = event.target.closest("[data-history-open]");
  const retryButton = event.target.closest("[data-history-retry]");
  if (openButton) {
    try {
      const run = await requestJson(`/api/runs/${encodeURIComponent(openButton.dataset.historyOpen)}`);
      state.runId = run.id; state.activeDoc = 0; renderRun(run);
      els.configNote.innerHTML = '<span class="note-icon">✓</span><span>已打开历史研究任务，可以继续查看证据或提问。</span>';
    } catch (error) { els.configNote.innerHTML = `<span class="note-icon">!</span><span>${escapeHtml(error.message)}</span>`; }
    return;
  }
  if (retryButton) {
    retryButton.disabled = true; retryButton.textContent = "重试中…";
    try {
      const body = await requestJson(`/api/agent/crawl4ai/runs/${encodeURIComponent(retryButton.dataset.historyRetry)}/retry`, { method: "POST" });
      state.runId = body.run_id; state.activeDoc = 0; setStatus("queued"); setBusy(true); els.configNote.innerHTML = '<span class="note-icon pulse-icon">•</span><span>重试任务已提交，正在恢复研究。</span>'; await loadCrawlHistory(); await pollRun();
    } catch (error) { retryButton.disabled = false; retryButton.textContent = "重试"; els.configNote.innerHTML = `<span class="note-icon">!</span><span>${escapeHtml(error.message)}</span>`; }
  }
});
els.queueList?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-crawl-cancel]"); if (!button) return;
  button.disabled = true; button.textContent = "取消中…";
  try { await requestJson(`/api/runs/${encodeURIComponent(button.dataset.crawlCancel)}/cancel`, { method: "POST" }); await loadResearchOps(); } catch (error) { button.disabled = false; button.textContent = "取消"; if (els.configNote) els.configNote.innerHTML = `<span class="note-icon">!</span><span>${escapeHtml(error.message)}</span>`; }
});
els.researchPlanList?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-plan-run]"); if (!button || button.disabled) return;
  button.disabled = true; button.textContent = "提交中…";
  try { await requestJson(`/api/crawl/plans/${encodeURIComponent(button.dataset.planRun)}/run`, { method: "POST" }); if (els.researchPlanMessage) els.researchPlanMessage.textContent = "计划已进入抓取队列。"; await loadResearchOps(); await loadCrawlHistory(); } catch (error) { if (els.researchPlanMessage) els.researchPlanMessage.textContent = error.message; button.disabled = false; button.textContent = "运行计划"; }
});
els.researchPlanForm?.addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.currentTarget.querySelector("button[type=submit]"); button.disabled = true; if (els.researchPlanMessage) els.researchPlanMessage.textContent = "保存中…";
  try { await requestJson("/api/crawl/plans", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: els.researchPlanTitle.value.trim(), query: els.researchPlanQuery.value.trim(), urls: els.researchPlanUrls.value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean), render_js: true, refresh: false, max_depth: 1, max_pages: 5 }) }); if (els.researchPlanMessage) els.researchPlanMessage.textContent = "计划已保存，可在下方点击运行。"; els.researchPlanForm.reset(); await loadResearchOps(); } catch (error) { if (els.researchPlanMessage) els.researchPlanMessage.textContent = error.message; } finally { button.disabled = false; }
});
els.refreshResearchOps?.addEventListener("click", async (event) => { event.currentTarget.disabled = true; await loadResearchOps(); event.currentTarget.disabled = false; });
els.refreshObservability?.addEventListener("click", async (event) => { event.currentTarget.disabled = true; await loadCrawlObservability(); event.currentTarget.disabled = false; });
els.sourceList.addEventListener("click", (event) => {
  const item = event.target.closest("[data-doc-index]");
  if (!item || !state.run) return;
  state.activeDoc = Number(item.dataset.docIndex);
  renderDocuments(state.run.documents);
});
els.copyButton.addEventListener("click", async () => {
  const doc = state.run?.documents?.[state.activeDoc];
  if (!doc?.markdown) return;
  await navigator.clipboard.writeText(doc.markdown);
  els.copyButton.innerHTML = "已复制";
  setTimeout(() => { els.copyButton.innerHTML = '<svg viewBox="0 0 24 24" fill="none"><rect x="8" y="8" width="11" height="11" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" stroke="currentColor" stroke-width="1.7"/></svg>复制内容'; }, 1400);
});
document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => { els.task.value = button.dataset.prompt; els.task.focus(); }));
document.querySelectorAll("[data-ask]").forEach((button) => button.addEventListener("click", () => { els.chatInput.value = button.dataset.ask; els.chatInput.focus(); }));
loadCrawlHistory();
loadResearchOps();
els.chatInput.addEventListener("keydown", (event) => {
  if (event.isComposing || event.key !== "Enter" || event.shiftKey) return;
  event.preventDefault();
  if (!els.chatButton.disabled) els.chatForm.requestSubmit();
});
loadHealth();
