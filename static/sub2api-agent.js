const sub2api$ = (selector) => document.querySelector(selector);
const sub2apiEsc = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const sub2apiFmt = (value) => value ? new Date(value).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const sub2apiPct = (value) => typeof value === "number" ? `${Math.round(value * 100)}%` : "—";

function renderSub2ApiSyncState(state = {}) {
  const node = sub2api$("#auto-sync-state");
  if (!node) return;
  const status = state.status || "unknown";
  const tone = status === "failed" ? "error" : status === "succeeded" ? "ok" : status === "not_configured" ? "warn" : "";
  const last = state.last_attempt_at ? ` · 最近尝试 ${sub2apiFmt(state.last_attempt_at)}` : "";
  const error = state.last_error ? ` · ${state.last_error}` : "";
  const pauseHint = state.credential_invalid ? "后台自动重试已暂停" : "";
  node.textContent = `${state.label || "同步状态未知"}${pauseHint ? ` · ${pauseHint}` : ""}${last}${error}。下一步：${state.next_action || "点击立即同步一次"}`;
  node.className = `auto-sync-state ${tone}`;
  node.setAttribute("role", status === "failed" ? "alert" : "status");
}

function renderSub2ApiHealth(body) {
  const analysis = body.analysis || {};
  const freshness = analysis.freshness || {};
  const alerts = analysis.alerts || [];
  const status = analysis.status || "unknown";
  const statusLabel = analysis.status_label || "状态未知";
  const summary = sub2api$("#health-summary");
  const statusNode = sub2api$("#health-status");
  const freshnessNode = sub2api$("#data-freshness");
  const ageNode = sub2api$("#data-age");
  if (statusNode) { statusNode.textContent = statusLabel; statusNode.dataset.status = status; }
  if (summary) summary.textContent = `${statusLabel} · ${freshness.label || "没有同步时间"} · 已检查 ${analysis.fields ? Object.values(analysis.fields).filter(Boolean).length : 0}/5 项关键字段`;
  if (freshnessNode) { freshnessNode.textContent = freshness.status === "fresh" ? "新鲜" : freshness.status === "aging" ? "较旧" : "过期"; freshnessNode.dataset.status = freshness.status || "unknown"; }
  if (ageNode) ageNode.textContent = freshness.age_seconds == null ? "无时间" : freshness.age_seconds < 3600 ? `${Math.max(1, Math.round(freshness.age_seconds / 60))} 分钟前` : `${Math.round(freshness.age_seconds / 3600)} 小时前`;
  const list = sub2api$("#alert-list");
  if (list) list.innerHTML = alerts.length ? alerts.map((alert) => `<div class="alert-row ${sub2apiEsc(alert.level)}"><span class="alert-dot"></span><div><strong>${sub2apiEsc(alert.title)}</strong><p>${sub2apiEsc(alert.message)}</p></div></div>`).join("") : '<div class="alert-empty"><span>✓</span><div><strong>暂时没有风险提醒</strong><p>额度、到期时间和数据新鲜度都在当前阈值内。</p></div></div>';
  const login = sub2api$("#login-status");
  if (login && status === "error") login.classList.add("error");
}

function renderSub2ApiHistory(history) {
  const list = sub2api$("#history-list");
  if (!list) return;
  if (!history?.length) { list.innerHTML = '<div class="history-empty">暂无历史快照；下次从浏览器同步脱敏数据后会在这里形成趋势。</div>'; return; }
  list.innerHTML = history.slice(0, 8).map((item) => `<div class="history-row"><span class="history-time">${sub2apiEsc(sub2apiFmt(item.checked_at || item.created_at))}</span><span>${sub2apiEsc(item.weekly_usage || "—")}</span><span>${sub2apiEsc(item.monthly_usage || "—")}</span><span class="history-status ${sub2apiEsc(item.status || "unknown")}">${sub2apiEsc(item.status === "ok" ? "正常" : item.status === "warning" ? "关注" : item.status === "error" ? "异常" : "未知")}</span></div>`).join("");
}

function renderSub2ApiForecast(body = {}) {
  const root = sub2api$("#quota-forecast");
  if (!root) return;
  const labels = { weekly_remaining_pct: "未来 7 天周额度", monthly_remaining_pct: "未来 7 天月额度" };
  const confidence = { high: "较可靠", medium: "中等参考", low: "低置信度", none: "不可预测" };
  const forecast = body.forecast || {};
  const cards = Object.entries(labels).map(([key, label]) => {
    const item = forecast[key] || {};
    if (item.status !== "available") return `<div class="forecast-card unavailable"><strong>${sub2apiEsc(label)}</strong><span>暂不可预测</span><small>${sub2apiEsc(item.reason || "样本不足")}</small></div>`;
    return `<div class="forecast-card"><strong>${sub2apiEsc(label)}</strong><span>${sub2apiPct(item.predicted)} · 区间 ${sub2apiPct(item.lower)}–${sub2apiPct(item.upper)}</span><small>${sub2apiEsc(confidence[item.confidence] || item.confidence)} · ${sub2apiEsc(item.sample_count)} 个样本 · ${sub2apiEsc(item.reason || "")}</small></div>`;
  });
  root.innerHTML = cards.join("") + `<p class="forecast-policy">预测只用于提前发现额度风险，不解释具体消费原因；数据截至 ${sub2apiEsc(body.data_as_of || "未知")}。</p>`;
}

function renderSub2ApiCostBreakdown(breakdown = {}) {
  const root = sub2api$("#cost-breakdown");
  if (!root) return;
  const groups = breakdown.groups || [];
  if (!groups.length) { root.innerHTML = '<span class="loading">暂无可按分组统计的成本字段。</span>'; return; }
  const money = (value) => value == null ? "—" : `$${Number(value).toFixed(2)}`;
  root.innerHTML = `<div class="cost-breakdown-head"><strong>按 Provider / 分组看成本</strong><small>今日 ${sub2apiEsc(money(breakdown.totals?.today_cost))} · 近 30 天 ${sub2apiEsc(money(breakdown.totals?.month_cost))}</small></div><div class="cost-breakdown-list">${groups.slice(0, 6).map((item) => `<div><span>${sub2apiEsc(item.group || "未分组")} · ${sub2apiEsc(item.key_count || 0)} Key</span><b>${sub2apiEsc(money(item.month_cost))}</b></div>`).join("")}</div><small class="cost-breakdown-policy">${sub2apiEsc(breakdown.policy || "")} ${breakdown.unpriced_count ? `仍有 ${sub2apiEsc(breakdown.unpriced_count)} 个 Key 缺少成本字段。` : ""}</small>`;
}

function renderSub2ApiRetry(host, message, action) {
  if (!host) return;
  window.WorkbenchUX?.wbShowRetry(host, message, "重试读取");
  const button = host.querySelector("[data-wb-retry]");
  button?.addEventListener("click", async () => {
    window.WorkbenchUX?.wbSetBusy(button, true, "读取中…");
    try { await action(); } finally { if (button.isConnected) window.WorkbenchUX?.wbSetBusy(button, false); }
  }, { once: true });
}

async function loadSub2ApiAgentState() {
  try {
    let body = null;
    if (window.__sub2ApiLoadPromise !== undefined) {
      body = await window.__sub2ApiLoadPromise;
      if (!body) throw new Error("账户状态暂时无法读取");
    } else {
      body = await (window.WorkbenchUX?.requestJson ? window.WorkbenchUX.requestJson("/api/sub2api") : fetch("/api/sub2api").then(async (response) => { const value = await response.json(); if (!response.ok) throw new Error(value.detail || `请求未完成（${response.status}）`); return value; }));
    }
    renderSub2ApiHealth(body);
    renderSub2ApiHistory(body.history || []);
    renderSub2ApiCostBreakdown(body.cost_breakdown || {});
    renderSub2ApiSyncState(body.sync_state || {});
    try {
      let trendBody = null;
      if (window.__sub2ApiTrendPromise !== undefined) {
        trendBody = await window.__sub2ApiTrendPromise;
      } else {
        trendBody = await (window.WorkbenchUX?.requestJson ? window.WorkbenchUX.requestJson("/api/sub2api/trend") : fetch("/api/sub2api/trend").then(async (response) => { const value = await response.json(); if (!response.ok) throw new Error(value.detail || `请求未完成（${response.status}）`); return value; }));
      }
      if (!trendBody) throw new Error("趋势暂时不可用");
      renderSub2ApiForecast(trendBody);
    } catch (error) {
      const forecast = sub2api$("#quota-forecast");
      if (forecast) forecast.innerHTML = `<div class="forecast-card unavailable"><strong>额度预测</strong><span>暂时无法读取</span><small>${sub2apiEsc(error.message)}</small></div>`;
    }
  } catch (error) {
    window.__sub2ApiLoadPromise = undefined;
    const summary = sub2api$("#health-summary");
    renderSub2ApiRetry(summary, `读取健康状态失败：${error.message}`, loadSub2ApiAgentState);
  }
}

const sub2apiEvaluateButton = sub2api$("#evaluate-alerts");
if (sub2apiEvaluateButton && sub2apiEvaluateButton.dataset.sub2apiInlineBound !== "true") sub2apiEvaluateButton.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  window.WorkbenchUX?.wbSetBusy(button, true, "检查中…");
  try {
    const body = await (window.WorkbenchUX?.requestJson ? window.WorkbenchUX.requestJson("/api/sub2api/alerts/evaluate", { method: "POST" }) : fetch("/api/sub2api/alerts/evaluate", { method: "POST" }).then(async (response) => { const value = await response.json(); if (!response.ok) throw new Error(value.detail || `请求未完成（${response.status}）`); return value; }));
    renderSub2ApiHealth(body);
    const created = (body.created || []).filter((item) => item.created).length;
    window.WorkbenchUX?.wbSetBusy(button, false);
    button.textContent = created ? `已生成 ${created} 条提醒` : "已检查";
    window.setTimeout(() => { if (button.isConnected) button.textContent = "检查风险"; }, 2200);
  } catch (error) {
    const message = sub2api$("#sync-message");
    if (message) message.textContent = `风险评估失败：${error.message}`;
    window.WorkbenchUX?.wbSetBusy(button, false);
  }
});

loadSub2ApiAgentState();
