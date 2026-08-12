/* 量化选股：条件筛选 + 可解释打分。
   刻意不使用「推荐/买入」措辞——展示的是候选池、入选原因和反面信号。 */
(function initMarketScreen() {
  const request = window.requestJson || (window.WorkbenchUX || {}).requestJson;
  const ux = window.WorkbenchUX || {};
  if (!request) return;

  const el = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  const num = (id, fallback) => {
    const value = Number(el(id)?.value);
    return Number.isFinite(value) ? value : fallback;
  };
  const show = (value, suffix = "") => (value === null || value === undefined ? "—" : `${value}${suffix}`);

  const PRESETS = {
    steady: { capMin: 100, capMax: 5000, peMax: 40, pbMax: 6, turnover: 0.5, amount: 2, mom: 25, val: 35, sta: 40 },
    growth: { capMin: 50, capMax: 3000, peMax: 100, pbMax: 12, turnover: 1, amount: 2, mom: 55, val: 20, sta: 25 },
    pullback: { capMin: 50, capMax: 3000, peMax: 60, pbMax: 8, turnover: 1, amount: 1, mom: 25, val: 35, sta: 40 },
  };

  function setValue(id, value) {
    const input = el(id);
    if (input && value !== undefined) input.value = value;
  }

  function selectPreset(name) {
    document.querySelectorAll("[data-screen-preset]").forEach((button) => button.classList.toggle("active", button.dataset.screenPreset === name));
    if (name === "custom") {
      const config = el("screen-config");
      if (config) config.open = true;
      return;
    }
    const preset = PRESETS[name];
    if (!preset) return;
    setValue("sc-cap-min", preset.capMin);
    setValue("sc-cap-max", preset.capMax);
    setValue("sc-pe-max", preset.peMax);
    setValue("sc-pb-max", preset.pbMax);
    setValue("sc-turnover", preset.turnover);
    setValue("sc-amount", preset.amount);
    ["mom", "val", "sta"].forEach((key) => {
      setValue(`sc-w-${key}`, preset[key]);
      const label = el(`sc-w-${key}-v`);
      if (label) label.textContent = String(preset[key]);
    });
  }

  ["mom", "val", "sta"].forEach((key) => {
    const slider = el(`sc-w-${key}`);
    const label = el(`sc-w-${key}-v`);
    if (slider && label) slider.addEventListener("input", () => { label.textContent = slider.value; });
  });

  function criteria() {
    return {
      min_market_cap: num("sc-cap-min", 50),
      max_market_cap: num("sc-cap-max", 3000),
      max_pe: num("sc-pe-max", 60),
      max_pb: num("sc-pb-max", 8),
      min_turnover: num("sc-turnover", 1),
      min_amount: num("sc-amount", 1),
      exclude_st: Boolean(el("sc-st")?.checked),
      weight_momentum: num("sc-w-mom", 40),
      weight_value: num("sc-w-val", 30),
      weight_stability: num("sc-w-sta", 30),
      limit: num("sc-limit", 15),
    };
  }

  function renderFunnel(data) {
    const host = el("screen-funnel");
    const dropped = Object.entries(data.dropped_by_rule || {}).filter(([, count]) => count > 0);
    host.hidden = false;
    const chips = dropped
      .map(([rule, count]) => `<span class="screen-chip">${esc(rule)} <b>-${count}</b></span>`)
      .join("");
    host.innerHTML = `<div class="screen-funnel-line">
        全市场 <b>${data.universe_size}</b> 只 → 通过条件 <b>${data.passed_filters}</b> 只 → 深度分析 <b>${data.deep_analyzed}</b> 只 → 展示前 <b>${(data.candidates || []).length}</b> 只
      </div>${chips ? `<div class="screen-chips">被哪条刷掉的：${chips}</div>` : ""}`;
  }

  function bar(label, value) {
    const width = Math.max(0, Math.min(100, Number(value) || 0));
    return `<div class="screen-bar"><span>${esc(label)}</span><i><b style="width:${width}%"></b></i><em>${width}</em></div>`;
  }

  function newsUrl(row) {
    // 起始页面必须一起带过去。只传目标的话，跳过去是一个填好了问题、
    // 但不知道从哪开始查的表单——而「去哪查」恰恰是这个按钮本来要替你解决的。
    const query = `${row.name} ${row.symbol} 最新公告 减持 商誉 诉讼 ST`;
    const start = `https://www.bing.com/search?q=${encodeURIComponent(query)}`;
    return `/projects/web-research?agent_goal=${encodeURIComponent(newsGoal(row))}&agent_start=${encodeURIComponent(start)}`;
  }

  function newsGoal(row) {
    // 消息面只用于人工排除，不参与打分：让研究 Agent 去查「它凭什么涨」，
    // 查到减持/重组传闻/商誉/诉讼/ST 风险，就自己把它从候选里划掉。
    return `查一下 ${row.name}（${row.symbol}）最近三个月的公告和新闻：`
      + `有没有大股东减持、重组或借壳传闻、商誉减值、诉讼或监管问询、ST 风险；`
      + `近期股价异动是由什么消息驱动的。逐条标注来源和日期，没查到就明确说没查到，不要推测。`;
  }

  function renderResult(data) {
    const host = el("screen-result");
    const rows = data.candidates || [];
    if (!rows.length) {
      host.innerHTML = `<div class="today-empty">没有股票同时满足这些条件。看上面「被哪条刷掉的」，把最狠的那条放宽一点再试。</div>`;
      return;
    }
    host.innerHTML = rows
      .map((row, index) => {
        const warnings = (row.warnings || []).map((text) => `<li>${esc(text)}</li>`).join("");
        const scores = row.scores || {};
        return `<article class="screen-item">
          <div class="screen-item-head">
            <span class="screen-rank">${index + 1}</span>
            <div class="screen-item-name">
              <strong>${esc(row.name)}</strong><span class="screen-code">${esc(row.symbol)}</span>
            </div>
            <div class="screen-total"><b>${show(scores.total)}</b><small>综合分</small></div>
          </div>
          <div class="screen-facts">
            <span>现价 ${show(row.price)}</span>
            <span>今日 ${show(row.change_pct, "%")}</span>
            <span>20 日 ${show(row.momentum_20d, "%")}</span>
            <span>60 日 ${show(row.momentum_60d, "%")}</span>
            <span>市盈率 ${show(row.pe)}</span>
            <span>市净率 ${show(row.pb)}</span>
            <span>市值 ${show(row.market_cap_yi, " 亿")}</span>
            <span>年化波动 ${show(row.volatility, "%")}</span>
            <span>距 60 日高点 ${show(row.drawdown_from_high, "%")}</span>
          </div>
          <details class="screen-why">
            <summary>为什么排在这里</summary>
            <div class="screen-bars">
              ${bar("涨得好", scores.momentum)}
              ${bar("买得便宜", scores.value)}
              ${bar("波动小", scores.stability)}
            </div>
            <p class="screen-relative">这三个分数是<strong>在本次候选池内部</strong>的排名，不是跨市场的绝对好坏。</p>
          </details>
          ${warnings ? `<details class="screen-risk" open><summary>哪里可能错（${(row.warnings || []).length}）</summary><ul>${warnings}</ul></details>` : ""}
          <div class="screen-item-actions">
            <button type="button" class="screen-plan-button" data-plan-symbol="${esc(row.symbol)}" data-plan-name="${esc(row.name)}">加入自选并设计划</button>
            <a class="screen-news" href="${newsUrl(row)}" target="_blank" rel="noopener">查最近消息 ↗</a>
            <span class="screen-news-note">消息只用来排除，不进分数</span>
          </div>
        </article>`;
      })
      .join("");
  }

  function renderLimitations(list) {
    const host = el("screen-limitations");
    if (host) host.innerHTML = (list || []).map((text) => `<li>${esc(text)}</li>`).join("");
  }

  async function runScreen(button) {
    const status = el("screen-status");
    if (ux.wbSetBusy) ux.wbSetBusy(button, true, "筛选中…");
    status.textContent = "正在拉全市场行情并计算日线因子，大约 20–60 秒…";
    try {
      const data = await request("/api/market/screen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(criteria()),
        timeoutMs: 180000,
      });
      renderFunnel(data);
      renderResult(data);
      renderLimitations(data.limitations);
      status.textContent = `${new Date(data.generated_at).toLocaleString("zh-CN")} · ${data.policy}`;
    } catch (error) {
      status.textContent = error?.message || "筛选失败";
      el("screen-result").innerHTML = `<div class="today-empty">${esc(error?.message || "筛选失败")}<br />可以先点「数据源自检」看看行情接口通不通。</div>`;
    } finally {
      if (ux.wbSetBusy) ux.wbSetBusy(button, false);
    }
  }

  async function selftest(button) {
    const status = el("screen-status");
    if (ux.wbSetBusy) ux.wbSetBusy(button, true, "自检中…");
    status.textContent = "正在检查行情数据源…";
    try {
      const data = await request("/api/market/screen/selftest", { timeoutMs: 60000 });
      const universe = data.universe || {};
      const kline = data.kline || {};
      status.innerHTML = data.ok
        ? `✅ 数据源正常：全市场 ${universe.rows} 只，日线 ${kline.points} 个点，${esc(universe.verdict || "")}`
        : `⚠️ 数据源有问题：${esc(universe.error || universe.verdict || "")} ${esc(kline.error || kline.verdict || "")}`;
    } catch (error) {
      status.textContent = `自检失败：${error?.message || "请稍后重试"}`;
    } finally {
      if (ux.wbSetBusy) ux.wbSetBusy(button, false);
    }
  }

  el("screen-run")?.addEventListener("click", (event) => void runScreen(event.currentTarget));
  el("screen-selftest")?.addEventListener("click", (event) => void selftest(event.currentTarget));
  document.querySelectorAll("[data-screen-preset]").forEach((button) => button.addEventListener("click", () => selectPreset(button.dataset.screenPreset)));
  selectPreset("steady");
})();
