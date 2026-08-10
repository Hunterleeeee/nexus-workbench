/* 量化 2.0：自选行情 + AI 一眼看 + 个股研究卡 + 工具（ETF轮动/可转债/估值/组合/仓位）+ 高级研究 */
const $ = (selector) => document.querySelector(selector);
const formatQuoteValue = (value) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) : "—";
function sparkline(points, up) {
  if (!points || points.length < 2) return "";
  const prices = points.map((p) => Number(p.p)).filter((v) => Number.isFinite(v) && v > 0);
  if (prices.length < 2) return "";
  const min = Math.min(...prices), max = Math.max(...prices);
  const range = max - min || 1;
  const w = 96, h = 30;
  const step = w / (prices.length - 1);
  const coords = prices.map((v, i) => [i * step, h - 3 - ((v - min) / range) * (h - 6)]);
  const path = coords.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const stroke = up ? "#c7534f" : "#238b72";
  return `<svg class="quote-spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true"><path d="${path}" fill="none" stroke="${stroke}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/><circle cx="${coords[coords.length-1][0].toFixed(1)}" cy="${coords[coords.length-1][1].toFixed(1)}" r="2.2" fill="${stroke}"/></svg>`;
}
function pctClass(value) { const n = Number(value); return Number.isFinite(n) ? (n >= 0 ? "up" : "down") : ""; }
function pctText(value) { const n = Number(value); if (!Number.isFinite(n)) return "—"; return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`; }
function pctColor(value) { const n = Number(value); if (!Number.isFinite(n)) return ""; return n >= 0 ? "var(--danger)" : "var(--green)"; }

/* ── 自选与行情 ── */
let lastQuotes = [];
function renderQuotes(market) {
  const watchlist = market.watchlist || [];
  const quotes = market.quotes || [];
  lastQuotes = quotes;
  $("#watch-count").textContent = watchlist.length;
  $("#symbols").value = watchlist.map((item) => item.symbol).join("\n");
  $("#checked-at").textContent = market.checked_at ? `最近同步 ${formatDate(market.checked_at)}` : "尚未同步";
  const upCount = quotes.filter((q) => Number(q.change_pct) > 0).length;
  const downCount = quotes.filter((q) => Number(q.change_pct) < 0).length;
  $("#market-kpi-today").innerHTML = `<span class="kpi-up">▲ ${upCount} 涨</span><span class="kpi-down">▼ ${downCount} 跌</span>`;
  const rows = quotes.map((q) => {
    const change = Number(q.change_pct);
    return `<article class="quote-row"><div class="quote-main"><strong>${escapeHtml(q.name || q.symbol)}</strong><small>${escapeHtml(String(q.symbol || "").toUpperCase())} · 开 ${escapeHtml(formatQuoteValue(q.open))} · 量 ${escapeHtml(formatQuoteValue(q.volume))}</small></div><div class="quote-trend">${sparkline(q.trend, change >= 0)}</div><div class="quote-price">${escapeHtml(formatQuoteValue(q.price))}<small>PE ${escapeHtml(q.pe != null ? q.pe : "—")}</small></div><div class="quote-change ${pctClass(change)}">${pctText(change)}</div></article>`;
  }).join("");
  $("#quote-list").innerHTML = rows || '<div class="m2-empty">还没有行情快照。点「刷新行情」获取数据。</div>';
  const quoteSymbols = new Set(quotes.map((q) => String(q.symbol || "").toLowerCase().replace(/^[a-z]+/, "")));
  const missing = (watchlist || []).map((i) => String(i.symbol || "").toLowerCase().replace(/^[a-z]+/, "")).filter((s) => s && !quoteSymbols.has(s));
  const miss = $("#quote-missing");
  if (miss) { miss.hidden = !missing.length; miss.textContent = missing.length ? `⚠ ${missing.slice(0, 6).join("、")}${missing.length > 6 ? ` 等 ${missing.length} 个` : ""} 暂不支持行情（场外基金或代码有误），可移除后重试` : ""; }
}

/* ── 自选编辑弹窗 ── */
function openWatchModal() { $("#watch-modal").classList.remove("hidden"); $("#market-search").focus(); }
function closeWatchModal() { $("#watch-modal").classList.add("hidden"); }
$("#edit-watch").addEventListener("click", openWatchModal);
document.querySelectorAll("[data-close-watch]").forEach((b) => b.addEventListener("click", closeWatchModal));
$("#watch-modal").addEventListener("click", (e) => { if (e.target === $("#watch-modal")) closeWatchModal(); });
$("#use-sample").addEventListener("click", () => { $("#symbols").value = "600519\n000001\n300750"; $("#market-message").textContent = "已填入 3 只示例股票（茅台/平安/宁德），点「保存自选」自动拉取行情。"; });
const searchInput = $("#market-search");
const searchResults = $("#market-search-results");
let searchTimer = null;
searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (q.length < 2) { searchResults.hidden = true; searchResults.innerHTML = ""; return; }
  searchTimer = setTimeout(async () => {
    searchResults.innerHTML = '<div class="market-search-item muted">搜索中…</div>';
    searchResults.hidden = false;
    try {
      const body = await requestJson(`/api/market/suggest?q=${encodeURIComponent(q)}`);
      const items = body.items || [];
      searchResults.innerHTML = items.length ? items.map((i) => `<button type="button" class="market-search-item" data-add-symbol="${escapeHtml(i.prefixed)}" data-add-name="${escapeHtml(i.name)}"><span>${escapeHtml(i.name)}</span><small>${escapeHtml(i.symbol)} · ${escapeHtml(i.kind)}</small></button>`).join("") : '<div class="market-search-item muted">没有匹配，试试输入 6 位代码</div>';
    } catch (error) { searchResults.innerHTML = `<div class="market-search-item muted">搜索失败：${escapeHtml(error.message)}</div>`; }
  }, 320);
});
searchResults.addEventListener("click", (e) => {
  const button = e.target.closest("[data-add-symbol]");
  if (!button) return;
  const current = $("#symbols").value.split(/[\s,，]+/).filter(Boolean);
  if (!current.includes(button.dataset.addSymbol)) current.push(button.dataset.addSymbol);
  $("#symbols").value = current.join("\n");
  searchInput.value = ""; searchResults.hidden = true; searchResults.innerHTML = "";
  $("#market-message").textContent = `已添加 ${button.dataset.addName}（${button.dataset.addSymbol}）。`;
  $("#symbols").focus();
});
$("#watchlist-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#watchlist-form .primary-button");
  WorkbenchUX.wbSetBusy(button, true, "保存中…");
  try {
    const body = await requestJson("/api/market/watchlist", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbols: $("#symbols").value.split(/[\s,，]+/).filter(Boolean) }) });
    renderQuotes(body.market || body);
    $("#market-message").textContent = "自选已保存，正在拉取最新行情…";
    try { const fresh = await requestJson("/api/market/refresh", { method: "POST" }); renderQuotes(fresh.market || fresh); $("#market-message").textContent = fresh.message || "行情已更新。"; } catch (refreshError) { $("#market-message").textContent = `自选已保存，但行情刷新失败：${refreshError.message}`; }
    closeWatchModal(); loadAIScan(); loadPortfolio();
  } catch (error) { $("#market-message").textContent = error.message; }
  finally { WorkbenchUX.wbSetBusy(button, false); }
});
$("#refresh-quotes").addEventListener("click", async () => {
  const button = $("#refresh-quotes");
  WorkbenchUX.wbSetBusy(button, true, "刷新中…");
  try { const body = await requestJson("/api/market/refresh", { method: "POST" }); renderQuotes(body.market || body); loadAIScan(); loadPortfolio(); }
  catch (error) { $("#checked-at").textContent = error.message; }
  finally { WorkbenchUX.wbSetBusy(button, false); }
});

/* ── AI 一眼看 ── */
async function loadAIScan(question) {
  const state = $("#ai-scan-state");
  const answer = $("#ai-scan-answer");
  state.textContent = "生成中…";
  try {
    const body = await requestJson("/api/market/ai-scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: question || "" }) });
    answer.innerHTML = body.ok ? `<p>${escapeHtml(body.answer)}</p>` : `<p>${escapeHtml(body.answer)}</p>`;
    state.textContent = body.ok ? "基于快照数据" : "不可用";
  } catch (error) { answer.innerHTML = `<p>${escapeHtml(error.message)}</p>`; state.textContent = "失败"; }
}
$("#ai-scan-form").addEventListener("submit", (event) => { event.preventDefault(); loadAIScan($("#ai-scan-question").value.trim()); });

/* ── 个股研究卡 ── */
async function loadResearchCard(symbol) {
  const host = $("#research-card-result");
  host.innerHTML = '<div class="m2-empty">正在生成研究卡（行情 + 估值 + 回测样本外）…</div>';
  try {
    const body = await requestJson(`/api/market/research-card?symbol=${encodeURIComponent(symbol)}`);
    if (!body.ok) { host.innerHTML = `<div class="m2-empty">${escapeHtml(body.message || "生成失败")}</div>`; return; }
    const q = body.quote || {};
    const bt = body.backtests || {};
    const wf = body.walkforward || {};
    const quoteLine = q.name ? `<div class="m2-card-head" style="margin-bottom:8px"><div><h3>${escapeHtml(q.name)} · ${escapeHtml(String(q.symbol || "").toUpperCase())}</h3><small>${pctText(q.change_pct)} · 开 ${escapeHtml(formatQuoteValue(q.open))} · 量 ${escapeHtml(formatQuoteValue(q.volume))}</small></div><strong style="color:${pctColor(q.change_pct)};font-size:16px">¥${escapeHtml(formatQuoteValue(q.price))}</strong></div>` : '<div class="m2-empty">没有拿到行情（代码可能有误或不在行情源）。</div>';
    const wfLine = wf.status === "ok" && wf.fold_count ? `${wf.fold_count} 折样本外验证中 ${Math.round((wf.positive_fold_rate || 0) * wf.fold_count)} 折为正，样本外收益 ${wf.out_of_sample_return_pct != null ? wf.out_of_sample_return_pct + "%" : "—"}${(wf.positive_fold_rate || 0) < 0.5 ? " · 提示：多数折为负，可能过拟合" : ""}` : (wf.message || "样本外：样本不足");
    const btLine = (name, key) => { const item = bt[key] || {}; return `${name}：${item.status === "ok" ? `净收益 ${item.net_return_pct}% vs 买入持有 ${item.benchmark_return_pct}% · 回撤 ${item.max_drawdown_pct ?? "—"}%` : (item.message || "样本不足")}`; };
    host.innerHTML = `
      ${quoteLine}
      <div class="m2-research-grid">
        <div class="m2-research-block"><strong>行情与估值</strong><p>PE ${q.pe ?? "—"} · PB ${q.pb ?? "—"}</p></div>
        <div class="m2-research-block"><strong>量化 · 动量回测</strong><p>${escapeHtml(btLine("动量（追涨）", "momentum"))}</p></div>
        <div class="m2-research-block"><strong>量化 · 均值回归</strong><p>${escapeHtml(btLine("均值回归", "mean_reversion"))}</p></div>
        <div class="m2-research-block"><strong>样本外验证</strong><p>${escapeHtml(wfLine)}</p></div>
        <div class="m2-research-block m2-research-wide"><strong>价值清单（巴菲特式检查）</strong><p>${escapeHtml(body.note || "护城河/ROE/负债率/自由现金流需人工核对后填写；量化部分基于本地历史快照。")}</p></div>
      </div>`;
  } catch (error) { host.innerHTML = `<div class="m2-empty">${escapeHtml(error.message)}</div>`; }
}
$("#research-card-form").addEventListener("submit", (event) => { event.preventDefault(); loadResearchCard($("#research-card-symbol").value.trim()); });

/* ── ETF 动量轮动 ── */
async function loadETF() {
  const host = $("#etf-rotation");
  try {
    const body = await requestJson("/api/market/etf-rotation");
    const rows = (body.pool || []).map((i) => `<div class="m2-row"><span>${escapeHtml(i.name)}</span><strong style="color:${pctColor(i.momentum_20d)}">${i.momentum_20d == null ? "—" : pctText(i.momentum_20d)}</strong></div>`).join("");
    host.innerHTML = `${rows}<p class="m2-suggestion">${escapeHtml(body.suggestion || "")}</p><small class="m2-note">${escapeHtml(body.note || "")}</small>`;
  } catch (error) { host.innerHTML = `<div class="m2-empty">${escapeHtml(error.message)}</div>`; }
}
$("#refresh-etf").addEventListener("click", loadETF);

/* ── 可转债 ── */
async function loadCB() {
  const host = $("#cb-list");
  try {
    const body = await requestJson("/api/market/convertible-bonds?limit=12");
    if (!(body.bonds || []).length) { host.innerHTML = `<div class="m2-empty">${escapeHtml(body.note || "暂无数据")}</div>`; return; }
    const rows = body.bonds.map((b) => `<div class="m2-row"><span>${escapeHtml(b.name)}<small>${escapeHtml(b.symbol)} · 双低 ${b.double_low}</small></span><strong>¥${escapeHtml(formatQuoteValue(b.price))} <em style="color:${pctColor(b.change_pct)}">${pctText(b.change_pct)}</em></strong></div>`).join("");
    host.innerHTML = `${rows}<small class="m2-note">${escapeHtml(body.note || "")}</small>`;
  } catch (error) { host.innerHTML = `<div class="m2-empty">${escapeHtml(error.message)}</div>`; }
}
$("#refresh-cb").addEventListener("click", loadCB);

/* ── 指数估值百分位 ── */
async function loadValuation() {
  const host = $("#valuation-percentile");
  try {
    const body = await requestJson("/api/market/valuation-percentile");
    if (!(body.indices || []).length) { host.innerHTML = `<div class="m2-empty">${escapeHtml(body.note || "暂无数据")}</div>`; return; }
    const rows = body.indices.map((i) => {
      const p = i.pe_percentile == null ? 0 : i.pe_percentile;
      const color = p > 0.8 ? "var(--danger)" : p < 0.2 ? "var(--green)" : "var(--muted)";
      return `<div class="m2-row"><span>${escapeHtml(i.name)}<small>PE ${i.pe ?? "—"} · PB ${i.pb ?? "—"} · 分位 ${p}</small></span><strong style="color:${color}">${escapeHtml(i.eva_label || "—")}</strong></div>`;
    }).join("");
    host.innerHTML = `${rows}<small class="m2-note">${escapeHtml(body.note || "")}</small>`;
  } catch (error) { host.innerHTML = `<div class="m2-empty">${escapeHtml(error.message)}</div>`; }
}
$("#refresh-valuation").addEventListener("click", loadValuation);

/* ── 组合体检 ── */
async function loadPortfolio() {
  const host = $("#portfolio-check");
  try {
    const body = await requestJson("/api/market/portfolio-check");
    if (!body.ok) { host.innerHTML = `<div class="m2-empty">${escapeHtml(body.note || "暂无数据")}</div>`; return; }
    const trendCount = Object.keys(body.trend_points || {}).length;
    host.innerHTML = `<div class="m2-row"><span>自选 ${body.count} 只</span><strong><span style="color:var(--danger)">▲ ${body.up_count} 涨</span> · <span style="color:var(--green)">▼ ${body.down_count} 跌</span></strong></div><small class="m2-note">${escapeHtml(body.note || "")}${trendCount ? ` · ${trendCount} 个标的已有历史快照（可继续积累做相关性）。` : ""}</small>`;
  } catch (error) { host.innerHTML = `<div class="m2-empty">${escapeHtml(error.message)}</div>`; }
}
$("#refresh-portfolio").addEventListener("click", loadPortfolio);

/* ── 仓位计算器（前端计算） ── */
function calcPosition() {
  const capital = Number($("#ps-capital").value || 0);
  const risk = Number($("#ps-risk").value || 0);
  const stop = Number($("#ps-stop").value || 0);
  const winrate = Number($("#ps-winrate").value || 0) / 100;
  const ratio = Number($("#ps-ratio").value || 0);
  const out = $("#ps-result");
  if (!capital || !stop) { out.textContent = "请填写总资金与止损距离。"; return; }
  const fixedAmount = capital * risk / 100 / (stop / 100);
  const kelly = ratio > 0 ? (winrate - (1 - winrate) / ratio) : 0;
  const halfKelly = Math.max(0, kelly / 2);
  out.innerHTML = `<strong>2% 法则建议买入金额：¥${Math.round(fixedAmount).toLocaleString("zh-CN")}</strong>（单笔最大亏损 ¥${Math.round(capital * risk / 100).toLocaleString("zh-CN")}）<br>半凯利参考仓位：${(halfKelly * 100).toFixed(1)}% 资金（约 ¥${Math.round(capital * halfKelly).toLocaleString("zh-CN")}）；凯利值为负时不要交易。`;
}
["ps-capital", "ps-risk", "ps-stop", "ps-winrate", "ps-ratio"].forEach((id) => { const el = $("#" + id); if (el) el.addEventListener("input", calcPosition); });
calcPosition();

/* ── 高级研究工具（回测/样本外/对比/估值/报告/采样） ── */
function injectLegacyTools() {
  const host = $("#legacy-tools");
  host.innerHTML = `
    <div class="m2-legacy-grid">
      <section class="m2-legacy-card"><h4>策略回测</h4><div class="m2-form-grid"><label>代码<input id="bt-symbol" placeholder="600519" /></label><label>策略<select id="bt-strategy"><option value="momentum">动量（追涨）</option><option value="mean_reversion">均值回归</option></select></label><label>窗口<select id="bt-window"><option value="10">10 样本点</option><option value="20" selected>20 样本点</option></select></label></div><button id="bt-run" class="m2-button primary" type="button">运行回测</button><div id="bt-result" class="m2-result">结果会显示在这里，并附人话解读。</div></section>
      <section class="m2-legacy-card"><h4>样本外验证（更严格）</h4><button id="wf-run" class="m2-button primary" type="button">运行（用上方代码与策略）</button><div id="wf-result" class="m2-result">用没参与选参的新时间段再验证一次。</div></section>
      <section class="m2-legacy-card"><h4>策略对比 + 成本敏感性</h4><button id="cmp-run" class="m2-button primary" type="button">运行对比</button><div id="cmp-result" class="m2-result">对比追涨 vs 均值回归，看费用翻倍后结论是否稳。</div></section>
      <section class="m2-legacy-card"><h4>估值因子（人工核对）</h4><div class="m2-form-grid"><label>代码<input id="val-symbol" placeholder="600519" /></label><label>PE<input id="val-pe" type="number" step="0.01" /></label><label>PB<input id="val-pb" type="number" step="0.01" /></label><label>ROE%<input id="val-roe" type="number" step="0.01" /></label><label>来源<input id="val-note" placeholder="数据来源/日期" /></label></div><div class="m2-head-actions"><button id="val-fetch" class="m2-button" type="button">自动获取 PE/PB</button><button id="val-save" class="m2-button primary" type="button">保存因子</button></div><div id="val-result" class="m2-result"></div></section>
      <section class="m2-legacy-card"><h4>日报 / 周报</h4><div class="m2-head-actions"><button id="rep-daily" class="m2-button" type="button">生成日报</button><button id="rep-weekly" class="m2-button" type="button">生成周报</button></div><div id="rep-result" class="m2-result">报告基于本地快照生成，保存到 outputs/。</div></section>
      <section class="m2-legacy-card"><h4>历史样本采集</h4><p id="sampling-line" class="m2-result">读取采样状态…</p><div class="m2-head-actions"><select id="sampling-interval"><option value="1800" selected>每 30 分钟</option><option value="3600">每 1 小时</option><option value="86400">每天</option></select><button id="sampling-toggle" class="m2-button" type="button">开启采样</button></div></section>
    </div>`;
  $("#bt-run").addEventListener("click", runBacktest);
  $("#wf-run").addEventListener("click", runWalkForward);
  $("#cmp-run").addEventListener("click", runCompare);
  $("#val-fetch").addEventListener("click", fetchValuation);
  $("#val-save").addEventListener("click", saveValuation);
  $("#rep-daily").addEventListener("click", () => generateReport("daily"));
  $("#rep-weekly").addEventListener("click", () => generateReport("weekly"));
  $("#sampling-toggle").addEventListener("click", toggleSampling);
  loadSampling();
}
async function runBacktest() {
  const symbol = $("#bt-symbol").value.trim();
  const out = $("#bt-result");
  if (!symbol) { out.textContent = "先填股票代码。"; return; }
  out.textContent = "回测中…";
  try {
    const body = await requestJson("/api/market/backtest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol, strategy: $("#bt-strategy").value, window: Number($("#bt-window").value), fee_bps: 10, slippage_bps: 5 }) });
    const bt = body.backtest || {};
    if (bt.status === "ok") {
      const net = Number(bt.net_return_pct), bench = Number(bt.benchmark_return_pct ?? 0);
      const vs = Number.isFinite(bench) ? net - bench : null;
      out.innerHTML = `<strong>${bt.sample_count ?? 0} 个样本点：${$("#bt-strategy").value === "momentum" ? "动量" : "均值回归"}净收益 ${net >= 0 ? "+" : ""}${net.toFixed(2)}%${vs == null ? "" : `，买入持有 ${bench.toFixed(2)}%，${vs >= 0 ? "跑赢" : "跑输"} ${Math.abs(vs).toFixed(2)}%`}；回撤 ${bt.max_drawdown_pct ?? "—"}%。仅作研究参考。</strong><pre>${escapeHtml(JSON.stringify({ 样本: bt.sample_count, 净收益: bt.net_return_pct, 基准: bt.benchmark_return_pct, 回撤: bt.max_drawdown_pct, 胜率: bt.win_rate, 盈亏比: bt.profit_factor, Sharpe: bt.sample_sharpe_ratio }, null, 1))}</pre>`;
    } else { out.textContent = bt.message || "样本不足，未输出收益判断。"; }
  } catch (error) { out.textContent = error.message; }
}
async function runWalkForward() {
  const symbol = $("#bt-symbol").value.trim();
  const out = $("#wf-result");
  if (!symbol) { out.textContent = "先在回测里填股票代码。"; return; }
  out.textContent = "验证中…";
  try {
    const body = await requestJson("/api/market/backtest/walk-forward", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol, strategy: $("#bt-strategy").value, window: Number($("#bt-window").value), fee_bps: 10, slippage_bps: 5, train_size: 30, test_size: 5, step_size: 5, max_folds: 8 }) });
    const wf = body.walk_forward || {};
    if (wf.status === "ok" && wf.fold_count) {
      const pos = Math.round((wf.out_of_sample_positive_fold_rate || 0) * wf.fold_count);
      out.innerHTML = `<strong>${wf.fold_count} 折中 ${pos} 折样本外为正；样本外收益 ${wf.out_of_sample_return_pct ?? "—"}%。${(wf.out_of_sample_positive_fold_rate || 0) < 0.5 ? "多数折为负，可能过拟合，谨慎参考。" : "策略在新时间段上表现相对稳定。"}仅作研究参考。</strong>`;
    } else { out.textContent = wf.message || "样本不足，未形成样本外结论。"; }
  } catch (error) { out.textContent = error.message; }
}
async function runCompare() {
  const symbol = $("#bt-symbol").value.trim();
  const out = $("#cmp-result");
  if (!symbol) { out.textContent = "先在回测里填股票代码。"; return; }
  out.textContent = "对比中…";
  try {
    const strategy = $("#bt-strategy").value;
    const window = Number($("#bt-window").value);
    const comparison = await requestJson("/api/market/strategies/compare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol, strategies: [strategy, strategy === "momentum" ? "mean_reversion" : "momentum"], window, fee_bps: 10, slippage_bps: 5 }) });
    const sensitivity = await requestJson("/api/market/backtest/sensitivity", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol, strategy, window, fee_bps: 10, slippage_bps: 5 }) });
    const comp = comparison.comparison || [];
    const netOf = (i) => i.net_return_pct == null ? null : Number(i.net_return_pct);
    const a = netOf(comp[0]), b = netOf(comp[1]);
    let line = "样本不足，暂无法对比。";
    if (a != null && b != null) {
      const winner = a >= b ? comp[0] : comp[1];
      line = `${winner.strategy === "momentum" ? "动量" : "均值回归"}策略净收益更高（${Math.max(a, b).toFixed(2)}% vs ${Math.min(a, b).toFixed(2)}%）。`;
      const scen = sensitivity.scenarios || [];
      if (scen.length >= 2 && scen[0].return_pct != null && scen[scen.length - 1].return_pct != null) {
        const low = Number(scen[0].return_pct), high = Number(scen[scen.length - 1].return_pct);
        line += `费用翻倍后收益 ${low.toFixed(2)}% → ${high.toFixed(2)}%${Math.abs(high - low) > 0.5 ? "，结论对成本敏感" : "，结论稳定"}。`;
      }
    }
    out.innerHTML = `<strong>${escapeHtml(line)}</strong>`;
  } catch (error) { out.textContent = error.message; }
}
async function fetchValuation() {
  const symbol = $("#val-symbol").value.trim().toLowerCase().replace(/^[a-z]+/, "");
  const q = lastQuotes.find((x) => String(x.symbol || "").toLowerCase().replace(/^[a-z]+/, "") === symbol);
  if (!q) { $("#val-result").textContent = "没有该标的行情；先刷新行情再试。"; return; }
  if (q.pe != null) $("#val-pe").value = q.pe;
  if (q.pb != null) $("#val-pb").value = q.pb;
  $("#val-note").value = `自动获取自行情（PE=${q.pe ?? "—"} / PB=${q.pb ?? "—"}）`;
  $("#val-result").textContent = `已填入 ${q.name || symbol} 的 PE/PB，核对后保存。`;
}
async function saveValuation() {
  const symbol = $("#val-symbol").value.trim();
  const out = $("#val-result");
  if (!symbol) { out.textContent = "先填代码。"; return; }
  try {
    const body = await requestJson("/api/market/valuation", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol, fundamentals: { pe: $("#val-pe").value, pb: $("#val-pb").value, roe: $("#val-roe").value }, note: $("#val-note").value.trim() }) });
    out.textContent = `已保存估值 Artifact #${body.artifact?.id || "—"}（${(body.valuation?.factors || []).map((i) => `${i.name}=${i.value}`).join(" · ") || "无可用因子"}）`;
  } catch (error) { out.textContent = error.message; }
}
async function generateReport(period) {
  const out = $("#rep-result");
  out.textContent = "生成中…";
  try {
    const body = await requestJson("/api/market/report", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ period }) });
    out.textContent = body.answer ? `${body.answer.slice(0, 400)}…` : `已保存：outputs/${body.filename}`;
  } catch (error) { out.textContent = error.message; }
}
let samplingEnabled = false;
async function loadSampling() {
  const line = $("#sampling-line");
  try {
    const body = await requestJson("/api/market/sampling");
    const s = body.sampling || {};
    samplingEnabled = Boolean(s.enabled);
    const toggle = $("#sampling-toggle");
    toggle.textContent = samplingEnabled ? "停止采样" : "开启采样";
    if (s.interval_seconds) $("#sampling-interval").value = String(s.interval_seconds);
    line.textContent = samplingEnabled ? `采集中 · 历史 ${s.history_count || 0} 个快照 · ${s.interval_label || ""}` : `未开启 · 已保留 ${s.history_count || 0} 个历史快照`;
  } catch (error) { line.textContent = `采样状态读取失败：${error.message}`; }
}
async function toggleSampling() {
  const button = $("#sampling-toggle");
  WorkbenchUX.wbSetBusy(button, true, "保存中…");
  try {
    if (!samplingEnabled && !Number($("#watch-count").textContent || 0)) { $("#sampling-line").textContent = "当前没有自选股票，请先保存自选。"; return; }
    if (!samplingEnabled && !confirm("开启后 Workbench 会按周期读取公开行情并保存本地快照，是否开启？")) return;
    const body = await requestJson("/api/market/sampling", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !samplingEnabled, interval_seconds: Number($("#sampling-interval").value || 1800) }) });
    samplingEnabled = Boolean((body.sampling || {}).enabled);
    button.textContent = samplingEnabled ? "停止采样" : "开启采样";
    $("#sampling-line").textContent = samplingEnabled ? "已开启采样。" : "已停止采样（历史保留）。";
  } catch (error) { $("#sampling-line").textContent = error.message; }
  finally { WorkbenchUX.wbSetBusy(button, false); }
}

/* ── 初始化 ── */
async function loadMarket() {
  try {
    const body = await requestJson("/api/market");
    renderQuotes(body.market || body);
    loadAIScan(); loadPortfolio();
  } catch (error) { $("#quote-list").innerHTML = `<div class="m2-empty">${escapeHtml(error.message)}</div>`; }
}
loadMarket();
loadETF();
loadCB();
loadValuation();
injectLegacyTools();
