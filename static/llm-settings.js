/* Shared Workbench LLM settings surface.
 * All pages use the same provider contract, routing summary and recovery copy.
 * Credentials never leave the browser except in the explicit save/test request.
 */
(function () {
  "use strict";

  const query = (selector, root = document) => root.querySelector(selector);
  const queryAll = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  function locate() {
    const modal = query("#settings-modal, #global-settings-modal");
    const button = query("#llm-settings-button, #global-settings-button");
    const form = query("#llm-settings-form, #global-settings-form");
    const providers = query("#global-providers");
    if (!modal || !button || !form || !providers) return null;
    return {
      modal,
      button,
      form,
      providers,
      add: query("#global-add-provider", modal),
      message: query("#settings-message, #global-settings-message", modal),
      summary: query("#llm-config-summary, #global-llm-summary", modal),
      save: query("button[type=submit]", form),
      close: queryAll("#close-settings, #cancel-settings, [data-close-global-settings]", modal),
      isCrawl: Boolean(query("#settings-modal")),
    };
  }

  function healthText(provider) {
    const health = provider?.health || {};
    if (!provider?.has_key) return provider?.disabled_reason || "未保存 API Key";
    if (!provider?.usable) return provider?.disabled_reason || "配置不完整，暂不调用";
    if (health.status === "cooling") return "冷却中：下一次调用会跳过";
    if (health.status === "error") return `最近失败：${health.last_error_kind || "调用失败"}`;
    if (health.status === "healthy") return "最近调用成功";
    return "已保存 · 可调用 · 尚无正式调用记录（测试不计入正式健康）";
  }

  function providerMarkup(provider = {}) {
    const providerId = escapeHtml(provider.id || "");
    const name = escapeHtml(provider.name || "");
    const baseUrl = escapeHtml(provider.base_url || "");
    const model = escapeHtml(provider.model || "");
    const role = provider.role === "primary" ? "primary" : "fallback";
    const hasKey = Boolean(provider.has_key);
    const health = provider.health || {};
    const statusClass = health.status === "healthy" ? "ok" : health.status === "error" || health.status === "cooling" ? "error" : "";
    const incomplete = provider.usable === false ? " is-incomplete" : "";
    return `<div class="llm-provider${incomplete}" data-role="${role}" data-provider-id="${providerId}" data-has-key="${hasKey ? "true" : "false"}">
      <div class="llm-provider-top">
        <label class="llm-provider-name-field"><span>名称</span><input class="llm-provider-name" value="${name}" placeholder="例如：主模型 / 备用模型" autocomplete="off" /></label>
        <label class="llm-provider-role-field"><span>角色</span><select class="llm-provider-role"><option value="primary"${role === "primary" ? " selected" : ""}>主配置</option><option value="fallback"${role === "fallback" ? " selected" : ""}>备用</option></select></label>
        <button class="llm-provider-test" type="button">测试连通</button>
        <button class="llm-provider-remove" type="button" title="删除该条目" aria-label="删除该条目">×</button>
      </div>
      <label><span>API 地址</span><input class="llm-provider-base-url" value="${baseUrl}" placeholder="https://api.openai.com/v1 或 …/chat/completions" autocomplete="off" /><small class="llm-provider-help">只填基地址或完整 Chat Completions 地址；不要带查询参数、凭据或 Key。</small></label>
      <label><span>模型名</span><input class="llm-provider-model" value="${model}" placeholder="gpt-4o-mini" autocomplete="off" /></label>
      <label><span>API Key</span><div class="llm-provider-key-row"><input class="llm-provider-key" type="password" placeholder="${hasKey ? "已保存，留空保持不变" : "保存后需填写"}" autocomplete="off" /><button class="llm-provider-clear" type="button" ${hasKey ? "" : "disabled"}>清除</button></div></label>
      <p class="llm-provider-status ${statusClass}" role="status" aria-live="polite">${escapeHtml(healthText(provider))}</p>
    </div>`;
  }

  function collect(root) {
    return queryAll(".llm-provider", root).map((item) => ({
      id: item.dataset.providerId || "",
      provider_id: item.dataset.providerId || "",
      name: query(".llm-provider-name", item)?.value.trim() || "",
      role: query(".llm-provider-role", item)?.value || "fallback",
      base_url: query(".llm-provider-base-url", item)?.value.trim() || "",
      model: query(".llm-provider-model", item)?.value.trim() || "",
      api_key: query(".llm-provider-key", item)?.value.trim() || "",
      preserve_api_key: item.dataset.hasKey === "true" && item.dataset.clearApiKey !== "true",
      clear_api_key: item.dataset.clearApiKey === "true",
    }));
  }

  function setMessage(view, text, error = false) {
    if (!view.message) return;
    view.message.textContent = text;
    view.message.classList.toggle("error", error);
    view.message.setAttribute("role", error ? "alert" : "status");
  }

  function updateLabel(view, llm) {
    const label = query(".global-label", view.button) || query("#global-label", view.button);
    const chip = query("#llm-status-chip", view.button);
    const routeLabel = llm?.active_route === "cooling" ? "LLM 冷却中" : llm?.active_route === "primary" ? "主 LLM" : llm?.active_route === "fallback" ? "备用 LLM" : "未配置";
    const active = llm?.active_provider_name || llm?.source || llm?.model || "—";
    if (label) label.textContent = llm?.configured ? `${routeLabel} · ${active}` : routeLabel;
    if (chip) chip.textContent = llm?.configured ? routeLabel : "未配置";
    view.button.classList.toggle("unconfigured", !llm?.configured);
    view.button.classList.toggle("configured", Boolean(llm?.configured));
    view.button.classList.toggle("has-primary", Boolean(llm?.primary_configured));
  }

  function updateSummary(view, llm) {
    if (!view.summary) return;
    const providers = llm?.providers || [];
    const primaryCount = providers.filter((item) => item.role === "primary").length;
    const fallbackCount = Number.isFinite(Number(llm?.fallback_count)) ? Number(llm.fallback_count) : providers.length - primaryCount;
    const active = llm?.active_provider_name || llm?.source || "未生效";
    const order = (llm?.candidate_order || []).map((item, index) => `${index + 1}. ${item.name || "未命名"}${item.source === "environment" ? "（环境变量）" : ""}`).join(" → ");
    const reason = llm?.active_selection_reason ? ` · ${llm.active_selection_reason}` : "";
    const health = llm?.active_status === "cooling" ? " · 当前冷却中" : llm?.active_status === "error" ? " · 最近调用失败" : "";
    const routable = Number.isFinite(Number(llm?.routable_count)) ? Number(llm.routable_count) : providers.filter((item) => item.usable).length;
    const formalSuccess = Number.isFinite(Number(llm?.formal_success_count)) ? Number(llm.formal_success_count) : providers.filter((item) => item.health?.status === "healthy").length;
    view.summary.textContent = providers.length || llm?.configured
      ? `已保存 ${providers.length} 个条目 · 可调用 ${routable} · 正式成功 ${formalSuccess} · 主 ${primaryCount} · fallback ${fallbackCount}${llm?.configured ? ` · 当前生效：${active} / ${llm.model || "—"}${health}${reason}` : " · 暂无可调用候选"}${order ? ` · 调用顺序：${order}` : ""}`
      : "还没有配置任何 LLM 条目。添加后需同时填写地址、模型名和 API Key 才会进入调用候选。";
    view.summary.dataset.tone = llm?.configured ? "success" : "warning";
  }

  function ensureMetrics(view) {
    if (!view.summary) return null;
    let panel = query("#llm-metrics-panel", view.modal);
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "llm-metrics-panel";
      panel.className = "llm-metrics-panel";
      panel.setAttribute("aria-live", "polite");
      view.summary.insertAdjacentElement("afterend", panel);
    }
    return panel;
  }

  function renderMetrics(view, body = {}) {
    const panel = ensureMetrics(view);
    if (!panel) return;
    const summary = body.summary || {};
    const providers = body.by_provider || [];
    const errors = (body.error_kinds || []).slice(0, 3).map((item) => `${escapeHtml(item.kind)} ${escapeHtml(item.count)}`).join(" · ");
    panel.innerHTML = `<div class="llm-metrics-head"><strong>近 ${escapeHtml(body.window_hours || 24)} 小时运行指标</strong><small>${summary.calls ? `${escapeHtml(summary.succeeded)}/${escapeHtml(summary.calls)} 成功` : "暂无正式调用记录"}</small></div><div class="llm-metrics-grid"><span><b>${summary.calls ? `${Math.round(Number(summary.success_rate || 0) * 100)}%` : "—"}</b><small>成功率</small></span><span><b>${escapeHtml(summary.avg_latency_ms || 0)}ms</b><small>平均延迟</small></span><span><b>${escapeHtml(summary.total_tokens || 0)}</b><small>Token</small></span><span><b>${escapeHtml(summary.cost_usd || 0)}</b><small>估算成本 USD</small></span></div><p>${providers.length ? providers.map((item) => `${escapeHtml(item.provider_name)} · ${escapeHtml(item.calls)} 次 · ${Math.round(Number(item.success_rate || 0) * 100)}%`).join("<br>") : "指标只记录模型服务、模型、状态、Token 和耗时，不保存请求或响应正文。"}${errors ? `<br>失败类型：${errors}` : ""}</p>`;
  }

  const sharedRequestJson = window.WorkbenchUX?.requestJson;
  async function requestJson(url, options = {}) {
    if (sharedRequestJson) return sharedRequestJson(url, options);
    const response = await fetch(url, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `请求未完成（${response.status}）`);
    return body;
  }

  function bindProvider(view, item) {
    query(".llm-provider-test", item)?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const status = query(".llm-provider-status", item);
      const entries = collect(view.providers);
      const index = queryAll(".llm-provider", view.providers).indexOf(item);
      const data = entries.find((entry) => entry.id && entry.id === item.dataset.providerId) || entries[index] || {};
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      if (status) { status.hidden = false; status.textContent = "正在验证当前填写内容…"; status.className = "llm-provider-status"; }
      try {
        const body = await requestJson("/api/settings/llm/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
        if (status) { status.textContent = `连接成功 · ${body.model}${body.latency_ms != null ? ` · ${body.latency_ms}ms` : ""} · 测试不会改变正式健康状态`; status.className = "llm-provider-status ok"; }
      } catch (error) {
        if (status) { status.textContent = error.message; status.className = "llm-provider-status error"; }
      } finally { button.disabled = false; button.removeAttribute("aria-busy"); }
    });

    query(".llm-provider-remove", item)?.addEventListener("click", (event) => {
      const button = event.currentTarget;
      if (button.dataset.deletePending !== "true") {
        button.dataset.deletePending = "true";
        button.textContent = "确认删除";
        button.setAttribute("aria-label", "再次点击确认删除");
        const status = query(".llm-provider-status", item);
        if (status) status.textContent = "再次点击“确认删除”移除；保存后才会生效。";
        window.setTimeout(() => { if (button.isConnected) { delete button.dataset.deletePending; button.textContent = "×"; button.setAttribute("aria-label", "删除该条目"); } }, 4500);
        return;
      }
      item.remove();
    });

    query(".llm-provider-clear", item)?.addEventListener("click", () => {
      const input = query(".llm-provider-key", item);
      const clear = query(".llm-provider-clear", item);
      if (input) input.value = "";
      item.dataset.clearApiKey = "true";
      item.dataset.hasKey = "false";
      if (input) input.placeholder = "已清除，保存后需重新填写";
      if (clear) clear.disabled = true;
    });

    query(".llm-provider-key", item)?.addEventListener("input", (event) => {
      if (!event.currentTarget.value.trim()) return;
      item.dataset.clearApiKey = "false";
      item.dataset.hasKey = "true";
      const clear = query(".llm-provider-clear", item);
      if (clear) clear.disabled = false;
    });

    query(".llm-provider-role", item)?.addEventListener("change", (event) => {
      if (event.currentTarget.value !== "primary") return;
      queryAll(".llm-provider-role", view.providers).forEach((select) => { if (select !== event.currentTarget && select.value === "primary") select.value = "fallback"; });
    });
  }

  function renderProviders(view, providers = []) {
    view.providers.innerHTML = providers.length ? providers.map(providerMarkup).join("") : `<div class="llm-provider-empty">还没有 Provider。添加后填写完整配置，或依赖环境变量作为最后 fallback。</div>`;
    queryAll(".llm-provider", view.providers).forEach((item) => bindProvider(view, item));
  }

  function close(view) {
    view.modal.classList.add("hidden");
    const previous = query(`#${CSS.escape(view.modal.dataset.prevFocus || "")}`);
    (previous || view.button).focus?.();
  }

  function open(view) {
    view.modal.classList.remove("hidden");
    view.modal.dataset.prevFocus = document.activeElement?.id || "";
    setMessage(view, "正在读取当前配置…");
    bindCompanion();
    requestJson("/api/settings/llm").then(async (body) => {
      renderProviders(view, body.llm?.providers || []);
      updateLabel(view, body.llm || {});
      updateSummary(view, body.llm || {});
      const panel = ensureMetrics(view);
      if (panel) panel.innerHTML = '<div class="llm-metrics-head"><strong>近 24 小时运行指标</strong><small>读取中…</small></div>';
      try { renderMetrics(view, (await requestJson("/api/agents/metrics?hours=24")).llm || {}); } catch (error) { if (panel) panel.innerHTML = `<div class="llm-metrics-head"><strong>运行指标</strong><small>${escapeHtml(error.message)}</small></div>`; }
      setMessage(view, "API Key 只保存在工作台服务端，不会回显；留空表示保留已保存的 Key。测试只验证连通性，不改变正式健康状态。");
    }).catch((error) => setMessage(view, error.message, true));
  }

  function init() {
    const view = locate();
    if (!view || view.modal.dataset.llmSharedInitialized === "true") return Boolean(view);
    view.modal.dataset.llmSharedInitialized = "true";
    view.button.addEventListener("click", () => open(view));
    view.close.forEach((button) => button.addEventListener("click", () => close(view)));
    view.modal.addEventListener("click", (event) => { if (event.target === view.modal) close(view); });
    view.modal.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); close(view); return; }
      if (event.key !== "Tab") return;
      const focusables = queryAll("button, input, select, textarea, [tabindex]:not([tabindex='-1'])", view.modal).filter((item) => !item.disabled && !item.hidden);
      if (!focusables.length) return;
      const first = focusables[0], last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    view.add?.addEventListener("click", () => {
      const wrapper = document.createElement("div");
      wrapper.innerHTML = providerMarkup({ role: "fallback" });
      const item = wrapper.firstElementChild;
      view.providers.appendChild(item);
      bindProvider(view, item);
      query(".llm-provider-name", item)?.focus();
    });
    view.form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (view.save) { view.save.disabled = true; view.save.setAttribute("aria-busy", "true"); }
      setMessage(view, "正在保存…");
      try {
        const body = await requestJson("/api/settings/llm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ providers: collect(view.providers) }) });
        updateLabel(view, body.llm || {});
        updateSummary(view, body.llm || {});
        setMessage(view, `已保存 ${body.llm?.providers?.length || 0} 个配置条目。`);
        window.setTimeout(() => close(view), 500);
      } catch (error) { setMessage(view, error.message, true); }
      finally { if (view.save) { view.save.disabled = false; view.save.removeAttribute("aria-busy"); } }
    });
    // Load the compact status without opening the modal.
    requestJson("/api/settings/llm").then((body) => { updateLabel(view, body.llm || {}); updateSummary(view, body.llm || {}); }).catch(() => {});
    return true;
  }

  // ── 本机浏览器助手（可选，外部组件）设置 ────────────────────────────
  const companionBase = "http://127.0.0.1:8766";
  async function companionRequest(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.method === "POST") headers["X-Workbench-Companion"] = "1";
    const timeoutMs = Math.max(1000, Math.min(30000, Number(options.timeoutMs) || 8000));
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    const requestOptions = { ...options, headers, mode: "cors" };
    delete requestOptions.timeoutMs;
    if (controller) requestOptions.signal = controller.signal;
    try {
      const response = await fetch(`${companionBase}${path}`, requestOptions);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.message || `Companion 请求失败（${response.status}）`);
      return body;
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("本机 Companion 请求超时，请确认 Companion 正在运行。");
      throw error;
    } finally { if (timer) clearTimeout(timer); }
  }
  function renderCompanionStatus(body = {}) {
    const running = body.status === "running" || body.isRunning === true;
    const button = query("#companion-toggle");
    const state = query("#companion-state");
    const detail = query("#companion-detail");
    if (!button || !state) return;
    state.textContent = running ? "本机助手运行中" : body.status === "not_configured" ? "未配置助手脚本" : "本机助手未运行";
    state.classList.toggle("ok", running);
    state.classList.toggle("error", body.ok === false);
    if (detail) detail.textContent = body.message || (running ? "本机 Google / Gemini 域名桥接已开启。" : "只在需要时启动；启动/停止会请求 macOS 管理员授权。");
    button.disabled = false;
    button.textContent = running ? "停止本机助手" : "启动本机助手";
    button.dataset.running = running ? "1" : "0";
  }
  async function loadCompanionStatus() {
    const button = query("#companion-toggle");
    const state = query("#companion-state");
    if (!button || !state) return;
    try { renderCompanionStatus(await companionRequest("/gemini/status")); }
    catch (error) {
      button.disabled = true; button.textContent = "启动 Companion 后可用";
      state.textContent = "本机 Companion 未连接"; state.className = "companion-state error";
      const detail = query("#companion-detail");
      if (detail) detail.textContent = "请在本机运行 companion/workbench_companion.py；服务器不会替代本机管理员桥。";
    }
  }
  async function toggleCompanion() {
    const button = query("#companion-toggle");
    if (!button) return;
    const running = button.dataset.running === "1";
    const action = running ? "停止" : "启动";
    if (!window.confirm(`确认${action}本机助手？这会调用外部助手脚本，并可能弹出系统授权。`)) return;
    button.disabled = true; button.textContent = `${action}中…`;
    try { renderCompanionStatus(await companionRequest(running ? "/gemini/stop" : "/gemini/start", { method: "POST" })); }
    catch (error) { const detail = query("#companion-detail"); if (detail) detail.textContent = error.message; button.disabled = false; }
  }
  function bindCompanion() {
    const toggle = query("#companion-toggle");
    if (!toggle || toggle.dataset.companionBound === "true") return;
    toggle.dataset.companionBound = "true";
    toggle.addEventListener("click", () => void toggleCompanion());
    void loadCompanionStatus();
  }

  window.WorkbenchLLMSettings = { init, isInitialized: () => Boolean(query("[data-llm-shared-initialized='true']")) };
  init();
})();
