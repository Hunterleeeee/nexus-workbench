(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const request = window.requestJson;
  const state = { feedback: [], requirements: [], decisions: [], prototypes: [], cowart: {}, summary: {}, attention: {}, projectFilter: "", projects: {}, feedbackFilter: "active", requirementFilter: "active", activePrototypeId: 0 };

  const feedbackStatusLabels = { new: "新反馈", reviewing: "归纳中", linked: "已关联需求", archived: "已归档" };
  const importanceLabels = { low: "低", normal: "一般", high: "重要", urgent: "紧急" };
  const requirementStatusLabels = { discovering: "收集中", review: "待评审", planned: "已规划", building: "开发中", shipped: "已上线", paused: "已暂停" };
  const prototypeStatusLabels = { draft: "草稿", review: "待评审", approved: "已确认", archived: "已归档" };
  const decisionStatusLabels = { proposed: "待确认", decided: "已决定", revisiting: "重评中", superseded: "已替代" };

  function escape(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }

  function formatDate(value) {
    if (!value) return "刚刚";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function setStatus(message = "", tone = "", href = "") {
    const node = $("#product-status");
    node.textContent = message;
    node.dataset.tone = tone;
    if (href) {
      const link = document.createElement("a");
      link.href = href;
      link.textContent = "打开查看 ↗";
      link.style.marginLeft = "8px";
      node.appendChild(link);
    }
  }

  function setBusy(button, busy, label = "处理中…") {
    if (!button) return;
    if (busy) {
      button.dataset.idleLabel = button.textContent.trim();
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = label;
    } else {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = button.dataset.idleLabel || button.textContent;
      delete button.dataset.idleLabel;
    }
  }

  function emptyMarkup(title, copy, action = "", tab = "") {
    return `<div class="product-empty"><strong>${escape(title)}</strong><p>${escape(copy)}</p>${action ? `<button class="secondary-button" type="button" data-switch-tab="${escape(tab)}">${escape(action)}</button>` : ""}</div>`;
  }

  function switchTab(tab, focus = false) {
    const target = $( `[data-product-tab="${tab}"]` );
    if (!target) return;
    $$("[data-product-tab]").forEach((button) => {
      const active = button === target;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    $$("[data-product-panel]").forEach((panel) => { panel.hidden = panel.dataset.productPanel !== tab; });
    if (focus) target.focus();
    if (window.location.hash !== `#${tab}`) history.replaceState(null, "", `#${tab}`);
  }

  function renderMetrics() {
    const summary = state.summary || {};
    $("#metric-feedback").textContent = summary.new_feedback ?? 0;
    $("#metric-active").textContent = summary.active_requirements ?? 0;
    $("#metric-review").textContent = summary.review_pending ?? 0;
    $("#metric-evidence").textContent = summary.needs_evidence ?? 0;
    $("#tab-feedback-count").textContent = summary.feedback_total ?? 0;
    $("#tab-requirement-count").textContent = summary.requirements_total ?? 0;
    $("#tab-prototype-count").textContent = summary.prototypes_total ?? 0;
    $("#tab-decision-count").textContent = summary.decisions_total ?? 0;
    const needsAttention = Number(summary.new_feedback || 0) + Number(summary.needs_evidence || 0) + Number(summary.review_pending || 0);
    const health = $(".product-health");
    health.classList.toggle("warning", needsAttention > 0);
    $("#product-health-label").textContent = needsAttention ? `${needsAttention} 项需要产品判断` : "产品现场已清空";
    $("#product-health-detail").textContent = needsAttention ? `${summary.new_feedback || 0} 条新反馈 · ${summary.needs_evidence || 0} 个需求缺证据` : "没有待处理反馈或评审事项";
  }

  function renderToday() {
    const focus = [];
    (state.attention.review || []).forEach((item) => focus.push({ kind: "评审", title: item.title, detail: item.problem || "确认范围、证据与验收标准", tab: "requirements", action: "去评审" }));
    (state.attention.needs_evidence || []).forEach((item) => focus.push({ kind: "证据", title: item.title, detail: "当前没有关联用户反馈，建议先补充事实来源", tab: "requirements", action: "看需求" }));
    (state.attention.new_feedback || []).forEach((item) => focus.push({ kind: "反馈", title: item.persona || item.source || "一条新反馈", detail: item.content, tab: "feedback", action: "去归纳" }));
    $("#today-focus-list").innerHTML = focus.length ? focus.slice(0, 6).map((item, index) => `<article class="focus-item"><span class="focus-index">${String(index + 1).padStart(2, "0")}</span><div class="focus-copy"><strong>${escape(item.kind)} · ${escape(item.title)}</strong><p>${escape(item.detail)}</p><small>需要产品经理确认</small></div><button type="button" data-switch-tab="${escape(item.tab)}">${escape(item.action)}</button></article>`).join("") : emptyMarkup("今天没有阻塞项", "可以记录一条新反馈，或让产品经理 Agent 复盘现有需求。", "记录反馈", "feedback");

    const priority = state.attention.top_priority || [];
    $("#today-priority-list").innerHTML = priority.length ? priority.map((item) => `<article class="priority-row"><div><strong>${escape(item.title)}</strong><small>${escape(requirementStatusLabels[item.status] || item.status)} · ${item.evidence_count || 0} 条证据</small></div><span class="rice-score">${Number(item.score || 0).toFixed(2)}</span></article>`).join("") : emptyMarkup("还没有可排序需求", "建立需求并填写 RICE 参数后，这里会显示优先级。", "新建需求", "requirements");

    const decisions = state.decisions.slice(0, 4);
    $("#today-decision-list").innerHTML = decisions.length ? decisions.map((item) => `<article class="decision-preview-item"><strong>${escape(item.title)}</strong><p>${escape(item.decision)} · ${escape(formatDate(item.created_at))}</p></article>`).join("") : emptyMarkup("还没有决策记录", "把重要取舍写下来，后续才能知道为什么改变。", "记录决策", "decisions");
  }

  function renderFeedback() {
    const items = state.feedback.filter((item) => state.feedbackFilter === "all" || item.status !== "archived");
    $("#feedback-list").innerHTML = items.length ? items.map((item) => `<article class="feedback-item"><div class="item-heading"><div><strong>${escape(item.persona || item.source || `反馈 #${item.id}`)}</strong><small>${escape(item.source || "手动记录")} · ${escape(formatDate(item.created_at))}</small></div><span class="importance-pill importance-${escape(item.importance)}">${escape(importanceLabels[item.importance] || item.importance)}</span></div><p>${escape(item.content)}</p><div class="item-footer"><span>${escape(feedbackStatusLabels[item.status] || item.status)}${item.linked_requirement_id ? ` · 需求 #${item.linked_requirement_id}` : ""}</span><div class="item-actions-row">${item.status !== "linked" && item.status !== "archived" ? `<button type="button" data-feedback-to-requirement="${item.id}">转成需求</button>` : ""}${item.status === "new" ? `<button type="button" data-feedback-status="reviewing" data-feedback-id="${item.id}">开始归纳</button>` : ""}${item.status !== "archived" ? `<button type="button" data-feedback-status="archived" data-feedback-id="${item.id}">归档</button>` : `<button type="button" data-feedback-status="new" data-feedback-id="${item.id}">恢复</button>`}</div></div></article>`).join("") : emptyMarkup("反馈池还是空的", "记录用户原话、来源和角色，先积累事实再讨论方案。", "记录第一条反馈", "feedback");
  }

  function requirementStatusOptions(current) {
    return Object.entries(requirementStatusLabels).map(([value, label]) => `<option value="${value}"${value === current ? " selected" : ""}>${label}</option>`).join("");
  }

  function renderRequirementOptions() {
    const select = $("#decision-requirement");
    const selected = select.value;
    select.innerHTML = '<option value="0">不关联具体需求</option>' + state.requirements.map((item) => `<option value="${item.id}">${escape(item.title)}</option>`).join("");
    if ($(`option[value="${CSS.escape(selected)}"]`, select)) select.value = selected;
  }

  function renderRequirements() {
    const filter = state.requirementFilter;
    const items = state.requirements.filter((item) => filter === "all" || filter === "active" ? (filter === "all" || !["shipped", "paused"].includes(item.status)) : item.status === filter);
    $("#requirement-list").innerHTML = items.length ? items.map((item) => {
      const prototype = state.prototypes.find((row) => Number(row.requirement_id) === Number(item.id) && row.status !== "archived");
      const cowartDisabled = state.cowart.available === false;
      return `<article class="requirement-item"><div class="requirement-topline"><span class="status-pill">${escape(requirementStatusLabels[item.status] || item.status)}</span><span class="status-pill ${item.evidence_count ? "" : "evidence-warning"}">${item.evidence_count || 0} 条证据</span><strong class="requirement-score">${Number(item.score || 0).toFixed(2)}</strong></div><p><strong>${escape(item.title)}</strong><br>${escape(item.problem || "还没有补充用户问题")}</p><div class="requirement-meta"><span>Reach ${Number(item.reach || 0)}</span><span>Impact ${Number(item.impact || 0)}</span><span>信心 ${Number(item.confidence || 0)}%</span><span>Effort ${Number(item.effort || 0)}</span></div><div class="item-footer"><span>${escape(item.target_user || "目标用户待补充")} · 更新 ${escape(formatDate(item.updated_at))}</span><div class="item-actions-row"><select data-requirement-status="${item.id}" aria-label="更新 ${escape(item.title)} 的状态">${requirementStatusOptions(item.status)}</select><button class="cowart-action" type="button" data-requirement-prototype="${item.id}"${cowartDisabled ? " disabled" : ""}>${prototype ? "打开 Cowart 原型" : "用 Cowart 做原型"}</button><button type="button" data-requirement-prd="${item.id}">生成 PRD</button><button type="button" data-requirement-decision="${item.id}">记决策</button></div></div></article>`;
    }).join("") : emptyMarkup("当前筛选下没有需求", "从真实反馈创建需求，RICE 会自动计算排序。", "新建需求", "requirements");
    renderRequirementOptions();
  }

  function renderCowartStatus() {
    const node = $("#cowart-integration-state");
    const available = state.cowart.available !== false;
    node.classList.toggle("unavailable", !available);
    $("strong", node).textContent = available ? `Cowart ${state.cowart.version || ""} 已接入` : "Cowart 资源不可用";
    $("small", node).textContent = available ? "统计上报已关闭 · Workbench 隔离存储" : "请重新安装 Workbench 的 Cowart 前端资源";
  }

  function renderPrototypes() {
    const items = state.prototypes || [];
    $("#prototype-list-summary").textContent = `${items.length} 个原型 · ${items.reduce((total, item) => total + Number(item.version_count || 0), 0)} 个已发布版本`;
    $("#prototype-list").innerHTML = items.length ? items.map((item) => `<article class="prototype-item"><div class="prototype-item-main"><div class="prototype-item-heading"><span class="status-pill prototype-${escape(item.status)}">${escape(prototypeStatusLabels[item.status] || item.status)}</span><span class="prototype-provider">Cowart ${escape(state.cowart.version || "")}</span></div><h3>${escape(item.title)}</h3><p>${escape(item.requirement_title || "未关联需求")}</p><div class="prototype-version-strip"><strong>${item.latest_version ? `v${item.latest_version}` : "尚未发布"}</strong><span>${item.version_count || 0} 个版本 · 更新 ${escape(formatDate(item.updated_at))}</span></div></div><div class="prototype-item-actions"><button class="primary-button" type="button" data-open-prototype="${item.id}">打开画布</button><a class="secondary-button" href="${escape(item.canvas_url)}" target="_blank" rel="noopener">全屏</a></div></article>`).join("") : emptyMarkup("还没有原型", "先去需求池选择一条需求，再点击“用 Cowart 做原型”。", "去需求池", "requirements");
    renderCowartStatus();
  }

  function renderDecisions() {
    $("#decision-list").innerHTML = state.decisions.length ? state.decisions.map((item) => `<article class="decision-item"><h3>${escape(item.title)}</h3><div class="decision-meta">${escape(decisionStatusLabels[item.status] || item.status)} · ${escape(item.requirement_title || "独立决策")} · ${escape(formatDate(item.created_at))}</div><p>${escape(item.decision)}</p>${item.rationale || item.alternatives || item.revisit_trigger ? `<div class="decision-detail">${item.rationale ? `<span><strong>理由：</strong>${escape(item.rationale)}</span>` : ""}${item.alternatives ? `<span><strong>未选方案：</strong>${escape(item.alternatives)}</span>` : ""}${item.revisit_trigger ? `<span><strong>重评条件：</strong>${escape(item.revisit_trigger)}</span>` : ""}</div>` : ""}</article>`).join("") : emptyMarkup("还没有产品决策", "记录决定、理由、未选方案和重评条件。", "记录第一条决策", "decisions");
  }

  function renderAll() {
    renderMetrics();
    renderToday();
    renderFeedback();
    renderRequirements();
    renderPrototypes();
    renderDecisions();
  }

  async function loadOverview(showSuccess = false) {
    try {
      const query = state.projectFilter ? `?project_id=${encodeURIComponent(state.projectFilter)}` : "";
      const body = await request(`/api/product-manager/overview${query}`);
      state.projects = body.projects || {};
      renderProjectOptions(state.projects);
      renderProjectRollup(state.projects);
      state.feedback = body.feedback || [];
      state.requirements = body.requirements || [];
      state.decisions = body.decisions || [];
      state.prototypes = body.prototypes || [];
      state.cowart = body.cowart || {};
      state.summary = body.summary || {};
      state.attention = body.attention || {};
      renderAll();
      if (showSuccess) setStatus("产品现场已刷新。", "success");
    } catch (error) {
      setStatus(`读取失败：${error.message}`, "error");
      ["#today-focus-list", "#today-priority-list", "#today-decision-list", "#feedback-list", "#requirement-list", "#prototype-list", "#decision-list"].forEach((selector) => { $(selector).innerHTML = emptyMarkup("暂时无法读取", error.message); });
    }
  }

  function ricePreview() {
    const reach = Number($("#requirement-reach").value || 0);
    const impact = Number($("#requirement-impact").value || 0);
    const confidence = Number($("#requirement-confidence").value || 0) / 100;
    const effort = Math.max(Number($("#requirement-effort").value || 0), 0.01);
    $("#rice-preview").textContent = (reach * impact * confidence / effort).toFixed(2);
  }

  function prepareRequirementFromFeedback(feedbackId) {
    const item = state.feedback.find((row) => Number(row.id) === Number(feedbackId));
    if (!item) return;
    $("#requirement-feedback-id").value = item.id;
    $("#requirement-title").value = item.content.replace(/\s+/g, " ").slice(0, 60);
    $("#requirement-problem").value = item.content;
    $("#requirement-user").value = item.persona || "";
    $("#linked-feedback-preview").hidden = false;
    $("#linked-feedback-preview").innerHTML = `<strong>已关联反馈 #${item.id}</strong>${escape(item.content)}`;
    $("#clear-feedback-link").hidden = false;
    $("#requirement-form-title").textContent = "把反馈转成需求";
    switchTab("requirements");
    $("#requirement-title").focus();
  }

  function clearFeedbackLink() {
    $("#requirement-feedback-id").value = "";
    $("#linked-feedback-preview").hidden = true;
    $("#linked-feedback-preview").textContent = "";
    $("#clear-feedback-link").hidden = true;
    $("#requirement-form-title").textContent = "新建产品需求";
  }

  async function updateFeedback(button) {
    setBusy(button, true, "保存中…");
    try {
      await request(`/api/product-manager/feedback/${encodeURIComponent(button.dataset.feedbackId)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: button.dataset.feedbackStatus }) });
      setStatus("反馈状态已更新。", "success");
      await loadOverview();
    } catch (error) { setStatus(`更新失败：${error.message}`, "error"); }
    finally { setBusy(button, false); }
  }

  async function updateRequirementStatus(select) {
    select.disabled = true;
    try {
      await request(`/api/product-manager/requirements/${encodeURIComponent(select.dataset.requirementStatus)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: select.value }) });
      setStatus("需求状态已更新，并同步到工作项。", "success");
      await loadOverview();
    } catch (error) { setStatus(`更新失败：${error.message}`, "error"); }
    finally { select.disabled = false; }
  }

  async function generatePrd(button) {
    setBusy(button, true, "生成中…");
    setStatus("文档工厂正在生成一页式 PRD，并保留需求与反馈证据关系…");
    try {
      const body = await request(`/api/product-manager/requirements/${encodeURIComponent(button.dataset.requirementPrd)}/prd`, { method: "POST" });
      setStatus(`PRD 已生成：${body.filename || "新版本文档"}`, "success", "/projects/doc-factory");
    } catch (error) { setStatus(`PRD 生成失败：${error.message}`, "error"); }
    finally { setBusy(button, false); }
  }

  function openPrototype(prototypeOrId) {
    const prototype = typeof prototypeOrId === "object" ? prototypeOrId : state.prototypes.find((item) => Number(item.id) === Number(prototypeOrId));
    if (!prototype) return;
    state.activePrototypeId = Number(prototype.id);
    switchTab("prototypes");
    $("#prototype-browser").hidden = true;
    $("#prototype-canvas-workspace").hidden = false;
    $("#prototype-canvas-title").textContent = prototype.title || "Cowart 原型";
    $("#prototype-canvas-status").textContent = `${prototype.requirement_title || "产品需求"} · 画布改动自动保存`;
    $("#prototype-fullscreen-link").href = prototype.canvas_url;
    $("#prototype-publish-button").dataset.prototypePublish = prototype.id;
    const frame = $("#prototype-canvas-frame");
    frame.src = prototype.canvas_url;
    frame.focus();
  }

  function closePrototype() {
    state.activePrototypeId = 0;
    $("#prototype-canvas-workspace").hidden = true;
    $("#prototype-browser").hidden = false;
    $("#prototype-canvas-frame").src = "about:blank";
  }

  async function createOrOpenPrototype(button) {
    setBusy(button, true, "正在准备画布…");
    try {
      const body = await request(`/api/product-manager/requirements/${encodeURIComponent(button.dataset.requirementPrototype)}/prototypes`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      await loadOverview();
      const prototype = state.prototypes.find((item) => Number(item.id) === Number(body.prototype?.id)) || body.prototype;
      setStatus(body.prototype?.created === false ? "已打开这个需求现有的 Cowart 原型。" : "Cowart 原型已创建，画布会自动保存。", "success");
      openPrototype(prototype);
    } catch (error) { setStatus(`Cowart 原型创建失败：${error.message}`, "error"); }
    finally { setBusy(button, false); }
  }

  async function publishPrototype(button) {
    const prototype = state.prototypes.find((item) => Number(item.id) === Number(button.dataset.prototypePublish));
    if (!prototype) return;
    const nextVersion = Number(prototype.latest_version || 0) + 1;
    if (!window.confirm(`确认发布「${prototype.title}」v${nextVersion}？\n\n当前画布会冻结为只读 Artifact，后续修改仍会保留在工作画布中。`)) return;
    setBusy(button, true, "正在发布…");
    $("#prototype-canvas-status").textContent = "正在冻结画布快照并建立需求关系…";
    try {
      const body = await request(`/api/product-manager/prototypes/${encodeURIComponent(prototype.id)}/publish`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ summary: `原型评审版本 v${nextVersion}`, confirmed: true }) });
      await loadOverview();
      $("#prototype-canvas-status").textContent = `v${body.version?.version || nextVersion} 已发布 · Artifact #${body.artifact?.id || "—"}`;
      setStatus(`Cowart 原型 v${body.version?.version || nextVersion} 已发布并登记 Artifact。`, "success");
    } catch (error) {
      $("#prototype-canvas-status").textContent = `发布失败：${error.message}`;
      setStatus(`原型发布失败：${error.message}`, "error");
    } finally { setBusy(button, false); }
  }

  $("#feedback-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter || $("#feedback-form button[type=submit]");
    const content = $("#feedback-content").value.trim();
    if (!content) return;
    setBusy(button, true, "保存中…");
    try {
      await request("/api/product-manager/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content, source: $("#feedback-source").value.trim(), persona: $("#feedback-persona").value.trim(), importance: $("#feedback-importance").value }) });
      event.currentTarget.reset();
      setStatus("反馈已保存，并登记为可追溯证据。", "success");
      await loadOverview();
    } catch (error) { setStatus(`保存失败：${error.message}`, "error"); }
    finally { setBusy(button, false); }
  });

function syncRequirementTypeFields() {
  const isDefect = ($("#requirement-type")?.value || "requirement") === "defect";
  // 缺陷不做 RICE：拿"触达 x 影响 / 成本"给 bug 排序没有意义，优先级来自严重级别。
  const rice = $("#rice-details");
  if (rice) {
    rice.hidden = isDefect;
    if (isDefect) rice.open = false;
    // 隐藏还不够：被 display:none 的控件仍然参与表单校验，一旦它校验不通过，
    // 浏览器既无法聚焦它、也不会提示，submit 就静默失败了
    // （控制台只留一句 "An invalid form control ... is not focusable"）。
    // 所以隐藏的同时必须 disabled，让它彻底退出校验。
    rice.querySelectorAll("input, select").forEach((field) => { field.disabled = isDefect; });
  }
  const severity = $("#requirement-severity-field");
  if (severity) severity.hidden = !isDefect;
  const titleField = $("#requirement-title-field");
  if (titleField) titleField.childNodes[0].nodeValue = isDefect ? "缺陷标题" : "需求名称";
  const title = $("#requirement-title");
  if (title) title.placeholder = isDefect ? "一句话说明什么坏了" : "一句话描述要解决的问题";
  const problem = $("#requirement-problem");
  if (problem) problem.placeholder = isDefect ? "复现步骤、期望结果、实际结果、影响范围" : "谁在什么场景遇到了什么问题？现有替代方案为什么不够？";
  const heading = $("#requirement-form-title");
  if (heading && !Number($("#requirement-feedback-id")?.value || 0)) heading.textContent = isDefect ? "登记缺陷" : "新建产品需求";
}

function renderProjectOptions(projects = {}) {
  const select = $("#requirement-project");
  if (!select) return;
  const previous = select.value;
  select.innerHTML = '<option value="">未归属</option>' + (projects.options || []).map((item) => `<option value="${escape(item.id)}">${escape(item.title)}</option>`).join("");
  // 正在看某个项目时，新建的条目默认就归到它——否则每次都要再选一次，
  // 而且很容易忘了选，结果东西全落进「未归属」。
  const preferred = projects.selected || previous;
  if (preferred && select.querySelector(`option[value="${CSS.escape(preferred)}"]`)) select.value = preferred;
}

function renderProjectRollup(projects = {}) {
  const host = $("#project-rollup");
  if (!host) return;
  const rows = projects.rollup || [];
  const selected = projects.selected || "";
  // 「全部」在最前，然后是各项目；有阻塞缺陷的高亮，一眼看出哪个在冒烟。
  const chips = [
    `<button type="button" class="project-chip ${selected ? "" : "active"}" data-project-filter="">全部</button>`,
    ...rows.map((row) => `<button type="button" class="project-chip ${selected === row.project_id ? "active" : ""} ${row.blockers ? "smoking" : ""}" data-project-filter="${escape(row.project_id)}" title="${escape(row.summary || row.project_title)}">
        <span>${escape(row.project_title)}</span>
        <em>${(row.requirements || 0) + (row.defects || 0)}</em>
        ${row.blockers ? `<b title="${row.blockers} 个阻塞缺陷">!</b>` : ""}
      </button>`),
  ].join("");
  const current = rows.find((row) => row.project_id === selected);
  host.innerHTML = `
    <div class="project-bar">
      <div class="project-chips">${chips}</div>
      <button type="button" id="add-project" class="project-add" title="新建产品项目">+ 新建项目</button>
    </div>
    ${current ? `<div class="project-context"><strong>${escape(current.project_title)}</strong>${current.summary ? `<span>${escape(current.summary)}</span>` : ""}<small>${current.requirements || 0} 需求 · ${current.defects || 0} 缺陷${current.blockers ? ` · <b>${current.blockers} 阻塞</b>` : ""}${current.needs_evidence ? ` · ${current.needs_evidence} 条缺证据` : ""}</small></div>` : ""}
    <form id="project-form" class="project-form" hidden>
      <input id="project-name" maxlength="120" placeholder="项目名称，例如：助理人 Workbench" required />
      <input id="project-summary" maxlength="2000" placeholder="一句话说明这个项目是什么（可选）" />
      <button class="primary-button" type="submit">创建</button>
      <button type="button" id="project-cancel" class="text-button">取消</button>
    </form>`;
}

  $("#requirement-type")?.addEventListener("change", syncRequirementTypeFields);
  syncRequirementTypeFields();   // 初始渲染也要对齐，别只依赖 HTML 里的 hidden
  $("#project-rollup")?.addEventListener("click", async (event) => {
    if (event.target.closest("#add-project")) {
      const form = $("#project-form");
      if (form) { form.hidden = false; $("#project-name")?.focus(); }
      return;
    }
    if (event.target.closest("#project-cancel")) { const form = $("#project-form"); if (form) form.hidden = true; return; }
    const chip = event.target.closest("[data-project-filter]");
    if (!chip) return;
    state.projectFilter = chip.dataset.projectFilter || "";
    await loadOverview();
  });
  $("#project-rollup")?.addEventListener("submit", async (event) => {
    if (!event.target.closest("#project-form")) return;
    event.preventDefault();
    const name = $("#project-name")?.value.trim();
    if (!name) return;
    try {
      const body = await request("/api/product-manager/projects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, summary: $("#project-summary")?.value.trim() || "" }) });
      // 建完直接切到这个项目，省得再点一次。
      state.projectFilter = String(body.project?.id || "");
      setStatus(`项目「${name}」已创建。`, "success");
      await loadOverview();
    } catch (error) { setStatus(`创建项目失败：${error.message}`, "error"); }
  });
  $("#requirement-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter || $("#requirement-form button[type=submit]");
    setBusy(button, true, "创建中…");
    const feedbackId = Number($("#requirement-feedback-id").value || 0);
    try {
      const itemType = $("#requirement-type")?.value || "requirement";
      await request("/api/product-manager/requirements", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: $("#requirement-title").value.trim(), project_id: $("#requirement-project")?.value || "", item_type: itemType, severity: itemType === "defect" ? ($("#requirement-severity")?.value || "major") : "", problem: $("#requirement-problem").value.trim(), target_user: $("#requirement-user").value.trim(), outcome: $("#requirement-outcome").value.trim(), reach: Number($("#requirement-reach").value || 0), impact: Number($("#requirement-impact").value || 0), confidence: Number($("#requirement-confidence").value || 0), effort: Number($("#requirement-effort").value || 1), feedback_ids: feedbackId ? [feedbackId] : [] }) });
      event.currentTarget.reset();
      $("#requirement-reach").value = "1"; $("#requirement-impact").value = "1"; $("#requirement-confidence").value = "50"; $("#requirement-effort").value = "1";
      clearFeedbackLink(); ricePreview(); syncRequirementTypeFields();
      setStatus(itemType === "defect" ? "缺陷已登记，并按严重级别同步为待办。" : "需求已加入需求池，并同步为工作项。", "success");
      await loadOverview();
    } catch (error) { setStatus(`创建失败：${error.message}`, "error"); }
    finally { setBusy(button, false); }
  });

  $("#decision-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter || $("#decision-form button[type=submit]");
    setBusy(button, true, "保存中…");
    try {
      await request("/api/product-manager/decisions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ requirement_id: Number($("#decision-requirement").value || 0), title: $("#decision-title").value.trim(), decision: $("#decision-content").value.trim(), rationale: $("#decision-rationale").value.trim(), alternatives: $("#decision-alternatives").value.trim(), revisit_trigger: $("#decision-trigger").value.trim() }) });
      event.currentTarget.reset();
      setStatus("决策已保存，并登记为可回溯 Artifact。", "success");
      await loadOverview();
    } catch (error) { setStatus(`保存失败：${error.message}`, "error"); }
    finally { setBusy(button, false); }
  });

  document.addEventListener("click", (event) => {
    const tabButton = event.target.closest("[data-product-tab], [data-switch-tab]");
    if (tabButton) switchTab(tabButton.dataset.productTab || tabButton.dataset.switchTab, Boolean(tabButton.dataset.productTab));
    const toRequirement = event.target.closest("[data-feedback-to-requirement]");
    if (toRequirement) prepareRequirementFromFeedback(toRequirement.dataset.feedbackToRequirement);
    const feedbackStatus = event.target.closest("[data-feedback-status]");
    if (feedbackStatus) updateFeedback(feedbackStatus);
    const prd = event.target.closest("[data-requirement-prd]");
    if (prd) generatePrd(prd);
    const prototypeRequirement = event.target.closest("[data-requirement-prototype]");
    if (prototypeRequirement) createOrOpenPrototype(prototypeRequirement);
    const openPrototypeButton = event.target.closest("[data-open-prototype]");
    if (openPrototypeButton) openPrototype(openPrototypeButton.dataset.openPrototype);
    const decision = event.target.closest("[data-requirement-decision]");
    if (decision) {
      $("#decision-requirement").value = decision.dataset.requirementDecision;
      const requirement = state.requirements.find((item) => Number(item.id) === Number(decision.dataset.requirementDecision));
      $("#decision-title").value = requirement ? `${requirement.title} · 产品决策` : "";
      switchTab("decisions"); $("#decision-content").focus();
    }
    const agent = event.target.closest("[data-open-agent]");
    if (agent) $(".project-agent-launcher")?.click();
  });

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-requirement-status]")) updateRequirementStatus(event.target);
  });

  $$("[data-feedback-filter]").forEach((button) => button.addEventListener("click", () => { state.feedbackFilter = button.dataset.feedbackFilter; $$("[data-feedback-filter]").forEach((item) => item.classList.toggle("active", item === button)); renderFeedback(); }));
  $("#requirement-filter").addEventListener("change", (event) => { state.requirementFilter = event.target.value; renderRequirements(); });
  $("#clear-feedback-link").addEventListener("click", clearFeedbackLink);
  $("#prototype-publish-button").addEventListener("click", (event) => publishPrototype(event.currentTarget));
  $("#prototype-close-button").addEventListener("click", closePrototype);
  $("#prototype-canvas-frame").addEventListener("load", () => {
    if (!state.activePrototypeId) return;
    $("#prototype-canvas-status").textContent = "Cowart 已打开 · 改动会自动保存到 Workbench";
  });
  ["#requirement-reach", "#requirement-impact", "#requirement-confidence", "#requirement-effort"].forEach((selector) => $(selector).addEventListener("input", ricePreview));
  $$("[data-product-tab]").forEach((button) => button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const tabs = $$("[data-product-tab]");
    const current = tabs.indexOf(button);
    const next = event.key === "ArrowRight" ? (current + 1) % tabs.length : (current - 1 + tabs.length) % tabs.length;
    switchTab(tabs[next].dataset.productTab, true);
  }));

  const initialTab = window.location.hash.replace("#", "");
  switchTab(["today", "feedback", "requirements", "prototypes", "decisions"].includes(initialTab) ? initialTab : "today");
  ricePreview();
  loadOverview();
})();
