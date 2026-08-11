/* Usage report: renders /api/usage/stats. Read-only, no writes. */
(function initUsagePage() {
  const ux = window.WorkbenchUX || {};
  const request = window.requestJson || ux.requestJson;
  const state = { days: 30, loading: false };

  const el = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

  function fmt(value) {
    const number = Number(value || 0);
    if (number >= 10000) return `${(number / 10000).toFixed(1)} 万`;
    return String(number);
  }

  function relativeDay(iso) {
    if (!iso) return "从未";
    const then = new Date(iso);
    if (Number.isNaN(then.getTime())) return "从未";
    const days = Math.floor((Date.now() - then.getTime()) / 86400000);
    if (days <= 0) return "今天";
    if (days === 1) return "昨天";
    return `${days} 天前`;
  }

  function renderHighlights(data) {
    const host = el("usage-highlights");
    const items = Array.isArray(data.highlights) ? data.highlights : [];
    host.innerHTML = items.length
      ? items.map((text) => `<li>${esc(text)}</li>`).join("")
      : `<li class="usage-empty">暂无可总结的活动。</li>`;
  }

  function renderKpis(data) {
    const totals = data.totals || {};
    const cards = [
      { value: fmt(totals.runs), label: `Agent 运行（${data.days} 天）` },
      { value: fmt(totals.work_items), label: "新建工作项" },
      { value: `${totals.active_projects || 0}/${totals.total_projects || 0}`, label: "真正在用的入口" },
      { value: fmt(totals.idle_projects), label: "零使用入口" },
      { value: `${(data.work_items || {}).completion_rate || 0}%`, label: "工作项完成率" },
    ];
    el("usage-kpis").innerHTML = cards
      .map((card) => `<div class="usage-kpi"><strong>${esc(card.value)}</strong><span>${esc(card.label)}</span></div>`)
      .join("");
  }

  function renderProjects(data) {
    const host = el("usage-projects");
    const rows = Array.isArray(data.projects) ? data.projects : [];
    if (!rows.length) {
      host.innerHTML = `<div class="usage-empty">这段时间没有任何入口产生活动。</div>`;
      return;
    }
    const max = Math.max(1, ...rows.map((row) => row.activity || 0));
    host.innerHTML = rows
      .map((row) => {
        const width = Math.round(((row.activity || 0) / max) * 100);
        const link = row.href ? `<a href="${esc(row.href)}">${esc(row.title)}</a>` : `<a>${esc(row.title)}</a>`;
        const detail = [
          `${row.runs || 0} 次运行`,
          `${row.work_items || 0} 个工作项`,
          `${row.artifacts || 0} 个产物`,
          `最近 ${relativeDay(row.last_used_at)}`,
        ].join(" · ");
        return `<div class="usage-row">
          <div class="usage-row-name">${link}<span class="usage-row-meta">${esc(detail)}</span></div>
          <div class="usage-bar"><i style="width:${width}%"></i></div>
          <span class="usage-verdict ${esc(row.verdict)}">${esc(row.verdict_label)}</span>
        </div>`;
      })
      .join("");
  }

  function flowRow(label, part, whole, note) {
    const percent = whole ? Math.round((part / whole) * 100) : 0;
    return `<div class="usage-flow-row">
      <header><span>${esc(label)}</span><b>${part} / ${whole}（${percent}%）</b></header>
      <div class="usage-bar"><i style="width:${percent}%"></i></div>
      ${note ? `<span class="usage-flow-note">${esc(note)}</span>` : ""}
    </div>`;
  }

  function renderFlow(data) {
    const inbox = data.inbox || {};
    const work = data.work_items || {};
    const notif = data.notifications || {};
    el("usage-flow").innerHTML = [
      flowRow("收件箱处理率", inbox.processed || 0, inbox.captured || 0, `还有 ${inbox.backlog || 0} 条堆在收件箱`),
      flowRow("工作项完成率", (work.done || 0) + (work.archived || 0), work.created || 0, `失败 ${work.failed || 0} · 阻塞 ${work.blocked || 0}`),
      flowRow("通知已读率", notif.read || 0, notif.total || 0, notif.total ? "未读比例高说明推送没价值" : ""),
    ].join("");
  }

  function renderLlm(data) {
    const llm = data.llm || {};
    const purposes = Array.isArray(llm.by_purpose) ? llm.by_purpose : [];
    const maxCalls = Math.max(1, ...purposes.map((item) => item.calls || 0));
    const totals = `<div class="usage-llm-total">
      <div><strong>${fmt(llm.calls)}</strong><span>调用次数</span></div>
      <div><strong>${fmt(llm.tokens)}</strong><span>Token</span></div>
      <div><strong>$${Number(llm.cost_usd || 0).toFixed(2)}</strong><span>估算成本</span></div>
      <div><strong>${fmt(llm.avg_latency_ms)}ms</strong><span>平均延迟</span></div>
    </div>`;
    const list = purposes.length
      ? `<div class="usage-llm-list">${purposes
          .map((item) => {
            const width = Math.round(((item.calls || 0) / maxCalls) * 100);
            return `<div class="usage-llm-item"><span>${esc(item.purpose)}</span><div class="usage-bar"><i style="width:${width}%"></i></div><span>${item.calls}</span></div>`;
          })
          .join("")}</div>`
      : `<div class="usage-empty">这段时间没有 LLM 调用记录。</div>`;
    el("usage-llm").innerHTML = totals + list;
  }

  function renderTrend(data) {
    const host = el("usage-trend");
    const daily = Array.isArray(data.daily_runs) ? data.daily_runs : [];
    if (!daily.length) {
      host.innerHTML = `<div class="usage-empty">没有运行记录。</div>`;
      return;
    }
    const max = Math.max(1, ...daily.map((item) => item.runs || 0));
    const labelEvery = Math.max(1, Math.ceil(daily.length / 12));
    host.innerHTML = daily
      .map((item, index) => {
        const height = Math.max(2, Math.round(((item.runs || 0) / max) * 100));
        const showLabel = index % labelEvery === 0 || index === daily.length - 1;
        const dateLabel = String(item.date || "").slice(5);
        return `<div class="usage-trend-col" title="${esc(item.date)}：${item.runs} 次"><div class="usage-trend-bar" style="height:${height}%"></div>${showLabel ? `<span class="usage-trend-label">${esc(dateLabel)}</span>` : ""}</div>`;
      })
      .join("");
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    const status = el("usage-state");
    if (status) status.textContent = "读取中…";
    try {
      const data = await request(`/api/usage/stats?days=${state.days}`);
      renderHighlights(data);
      renderKpis(data);
      renderProjects(data);
      renderFlow(data);
      renderLlm(data);
      renderTrend(data);
      if (status) status.textContent = `统计到 ${new Date(data.generated_at).toLocaleString("zh-CN")}`;
    } catch (error) {
      const message = error?.message || "读取失败";
      if (status) status.textContent = "读取失败";
      if (ux.wbShowRetry) ux.wbShowRetry(el("usage-projects"), message);
      else el("usage-projects").innerHTML = `<div class="usage-empty">${esc(message)}</div>`;
    } finally {
      state.loading = false;
    }
  }

  document.querySelectorAll(".usage-window-button").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".usage-window-button").forEach((item) => item.classList.remove("is-active"));
      button.classList.add("is-active");
      state.days = Number(button.dataset.days) || 30;
      load();
    });
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-wb-retry]")) load();
  });

  load();
})();
