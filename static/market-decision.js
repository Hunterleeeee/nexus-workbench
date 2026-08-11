/* Quant decision center: user plans first, historical reference zones second. */
(function initMarketDecisionCenter() {
  const request = window.requestJson || (window.WorkbenchUX || {}).requestJson;
  if (!request) return;
  const q = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  const money = (value) => {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number.toLocaleString("zh-CN", { maximumFractionDigits: 3 }) : "—";
  };
  const percent = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${number.toFixed(2)}%` : "—";
  };
  const GROUPS = [
    { key: "must", label: "必须处理", hint: "先核对风险线，再看你自己的买卖计划" },
    { key: "near", label: "快到计划线", hint: "提前准备，不需要追着价格动" },
    { key: "watch", label: "暂时不动", hint: "价格仍在你的计划范围内" },
    { key: "setup", label: "没设计划", hint: "先写下买入、卖出和止损条件" },
    { key: "unknown", label: "数据待核对", hint: "缺行情时不做判断" },
  ];
  let loading = false;

  function ruleRows(card) {
    const rules = card.rules || {};
    const rows = [
      ["想买的位置", rules.buy_below, "buy"],
      ["想卖的位置", rules.sell_above, "sell"],
      ["看错要停的位置", rules.stop_below, "stop"],
    ];
    return rows.map(([label, value, tone]) => `<div class="decision-rule-row ${tone}"><span>${label}</span><strong>${value ? `¥${esc(money(value))}` : "还没写"}</strong></div>`).join("");
  }

  function zoneMarkup(reference) {
    if (!reference?.available || !reference.buy_zone || !reference.sell_zone) {
      return `<div class="decision-zone-empty"><strong>暂不显示参考区间</strong><span>至少积累 5 个有效价格样本；样本越多、覆盖时间越长，参考价值越高。</span></div>`;
    }
    return `<div class="decision-zones">
      <div class="decision-zone buy"><span>历史相对偏低区</span><strong>¥${esc(money(reference.buy_zone.low))} – ¥${esc(money(reference.buy_zone.high))}</strong><small>买前仍要核对公司是否变差</small></div>
      <div class="decision-zone sell"><span>历史相对偏高区</span><strong>¥${esc(money(reference.sell_zone.low))} – ¥${esc(money(reference.sell_zone.high))}</strong><small>只是样本位置，不代表还会涨到这里</small></div>
      <div class="decision-zone risk"><span>风险观察线</span><strong>${reference.risk_observation_line ? `¥${esc(money(reference.risk_observation_line))}` : "—"}</strong><small>用低位样本与波动缓冲计算，不替代你的止损</small></div>
    </div>`;
  }

  function researchUrl(card) {
    const goal = `查清 ${card.name}（${card.symbol}）最近 30 天的重要公告、业绩变化和风险消息。区分事实与观点，并附来源日期。`;
    const start = `https://www.bing.com/search?q=${encodeURIComponent(`${card.name} ${card.symbol} 最新公告 业绩 风险`)}`;
    return `/projects/web-research?agent_goal=${encodeURIComponent(goal)}&agent_start=${encodeURIComponent(start)}`;
  }

  function cardMarkup(card) {
    const reference = card.reference || {};
    const facts = (card.facts || []).map((item) => `<li>${esc(item)}</li>`).join("");
    const risks = (card.risks || []).map((item) => `<li>${esc(item)}</li>`).join("");
    const position = card.position_example || {};
    const note = card.rules?.note ? `<p class="decision-plan-note">你当初写的：${esc(card.rules.note)}</p>` : "";
    return `<article class="decision-card" data-action="${esc(card.action_key || "unknown")}">
      <div class="decision-card-head">
        <div class="decision-card-name"><span class="decision-action-pill">${esc(card.action_label || "待核对")}</span><div><strong>${esc(card.name || card.symbol)}</strong><small>${esc(String(card.symbol || "").toUpperCase())}</small></div></div>
        <div class="decision-price"><strong>${card.price ? `¥${esc(money(card.price))}` : "—"}</strong><span class="${Number(card.change_pct) >= 0 ? "up" : "down"}">${percent(card.change_pct)}</span></div>
      </div>
      <div class="decision-card-summary"><strong>${esc(card.headline || "等待数据")}</strong><p>${esc(card.action || "刷新行情后再看。")}</p></div>
      <div class="decision-card-grid">
        <section class="decision-plan"><div class="decision-block-title"><span>我的买卖计划</span><button type="button" data-edit-rule="${esc(card.symbol)}" data-name="${esc(card.name)}">修改</button></div>${ruleRows(card)}${note}</section>
        <section class="decision-reference"><div class="decision-block-title"><span>量化参考位置</span><em data-quality="${esc(reference.quality || "low")}">${esc(reference.quality_label || "样本不足")} · ${Number(reference.sample_count || 0)} 样本 / ${Number(reference.coverage_days || 0)} 天</em></div>${zoneMarkup(reference)}</section>
      </div>
      <div class="decision-evidence">
        <details><summary>为什么这么说</summary><ul>${facts || "<li>当前没有足够事实。</li>"}</ul><p>${esc(reference.method || "")}</p></details>
        <details><summary>哪里可能错</summary><ul>${risks || "<li>公开快照不包含完整基本面。</li>"}</ul></details>
        <details><summary>这只最多买多少？</summary><p>${esc(position.message || "先设置止损线，才能做风险算术示例。")}</p></details>
      </div>
      <div class="decision-card-actions">
        <button type="button" class="decision-primary" data-edit-rule="${esc(card.symbol)}" data-name="${esc(card.name)}">设置我的买 / 卖 / 止损</button>
        <a href="${esc(researchUrl(card))}" target="_blank" rel="noopener">查最近消息</a>
        <button type="button" data-decision-research="${esc(card.symbol)}">生成研究卡</button>
      </div>
    </article>`;
  }

  function render(decision) {
    q("#decision-verdict").dataset.tone = decision.tone || "calm";
    q("#decision-verdict-title").textContent = decision.verdict || "—";
    q("#decision-verdict-detail").textContent = decision.detail || "";
    const cards = Array.isArray(decision.cards) ? decision.cards : [];
    const checkedAt = decision.checked_at;
    const freshness = decision.freshness || {};
    q("#decision-time").textContent = cards.length && checkedAt ? `数据 ${typeof formatDate === "function" ? formatDate(checkedAt) : checkedAt}` : "添加自选后开始";
    q("#decision-quality").textContent = cards.length ? `${freshness.label || "新鲜度未知"} · 历史 ${decision.history_count || 0} 次` : "当前自选为空";
    q("#decision-disclaimer").textContent = decision.disclaimer || "量化结果仅作研究参考，不构成投资建议。";
    ["must", "near", "watch", "setup"].forEach((key) => {
      const target = q(`#decision-count-${key}`);
      if (target) target.textContent = String(decision.counts?.[key] || 0);
    });
    const host = q("#decision-list");
    const groupNav = q(".decision-groups");
    if (groupNav) groupNav.hidden = !cards.length;
    if (!cards.length) {
      host.innerHTML = `<div class="decision-empty"><span>这页是干什么的？</span><strong>只跟踪你自己留下的股票</strong><p>你没有自选时，这里就应该是空的。先找候选或添加一只自选；设好买点、卖点和止损后，工作台才会按你的计划提醒。</p><div class="decision-empty-steps"><div><b>1</b><strong>找候选</strong><small>按条件缩小范围，不直接推荐买入</small></div><div><b>2</b><strong>加入自选</strong><small>只有你留下的股票会进入今日待办</small></div><div><b>3</b><strong>写计划线</strong><small>到买点 / 卖点 / 止损时提醒</small></div></div><div class="decision-empty-actions"><button type="button" data-open-watch>添加第一只自选</button><a href="#screen-card">先去条件选股</a></div></div>`;
      const button = host.querySelector("[data-open-watch]");
      button?.addEventListener("click", () => q("#edit-watch")?.click());
      return;
    }
    host.innerHTML = GROUPS.map((group) => {
      const groupCards = cards.filter((card) => card.group === group.key);
      if (!groupCards.length) return "";
      return `<section class="decision-group" id="decision-group-${group.key}" data-group="${group.key}"><header><div><strong>${group.label}</strong><span>${group.hint}</span></div><em>${groupCards.length} 只</em></header><div class="decision-group-cards">${groupCards.map(cardMarkup).join("")}</div></section>`;
    }).join("");
  }

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const body = await request("/api/market/decision-center", { timeoutMs: 30000 });
      render(body.decision || {});
    } catch (error) {
      q("#decision-list").innerHTML = `<div class="decision-empty"><strong>决策中心读取失败</strong><p>${esc(error?.message || "请稍后重试")}</p></div>`;
      q("#decision-verdict-title").textContent = "暂时无法判断";
      q("#decision-verdict-detail").textContent = "没有可靠数据时，先不要根据这里做决定。";
    } finally {
      loading = false;
    }
  }

  document.addEventListener("click", (event) => {
    const group = event.target.closest("[data-decision-group]");
    if (group) {
      const target = q(`#decision-group-${group.dataset.decisionGroup}`);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const research = event.target.closest("[data-decision-research]");
    if (research) {
      const symbol = research.dataset.decisionResearch || "";
      const input = q("#research-card-symbol");
      if (input) input.value = symbol;
      q("#research-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
      window.setTimeout(() => q("#research-card-form")?.requestSubmit(), 380);
      return;
    }
    if (event.target.closest("#today-refresh") || event.target.closest("#refresh-quotes")) {
      window.setTimeout(load, 1400);
      window.setTimeout(load, 3500);
    }
  });
  q("#rule-form")?.addEventListener("submit", () => window.setTimeout(load, 650));
  q("#watchlist-form")?.addEventListener("submit", () => window.setTimeout(load, 1800));
  document.addEventListener("market:state-updated", () => window.setTimeout(load, 0));
  document.addEventListener("market:rules-updated", () => window.setTimeout(load, 0));
  load();
})();
