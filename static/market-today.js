/* "我该做什么" front page for the market tool.
   Reads /api/market/today and writes user-defined alert lines. It never
   recommends a trade: every line shown is either a fact about the quote or a
   comparison against a threshold the user typed in themselves. */
(function initMarketToday() {
  const request = window.requestJson || (window.WorkbenchUX || {}).requestJson;
  const ux = window.WorkbenchUX || {};
  if (!request) return;

  const el = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

  const LEVEL_LABEL = {
    alert: "要处理",
    reach: "到线了",
    near: "快到了",
    unset: "没设线",
    ok: "不用动",
    unknown: "没数据",
  };
  const SIGNAL_LABEL = {
    buy: "你的买点",
    sell: "你的卖点",
    stop: "止损提醒",
    near: "接近计划线",
    setup: "待设计划",
    hold: "计划内",
    unknown: "没数据",
  };

  const state = { editing: null, loading: false, cards: [] };

  function renderVerdict(today) {
    const card = el("today-card");
    el("today-verdict-text").textContent = today.verdict || "—";
    el("today-verdict-detail").textContent = today.detail || "";
    if (card) card.dataset.tone = today.tone || "calm";
    const counts = today.counts || {};
    ["buy", "sell", "stop", "near"].forEach((key) => {
      const target = el(`today-count-${key}`);
      if (target) target.textContent = String(counts[key] || 0);
    });
    const checkedAt = el("today-checked-at");
    if (checkedAt) {
      const value = today.checked_at || today.data_as_of;
      checkedAt.textContent = value ? `数据时间 ${typeof formatDate === "function" ? formatDate(value) : value}` : "暂无行情时间";
    }
  }

  function ruleSummary(rules) {
    const parts = [];
    if (rules?.buy_below) parts.push(`买 ≤ ${rules.buy_below}`);
    if (rules?.sell_above) parts.push(`卖 ≥ ${rules.sell_above}`);
    if (rules?.stop_below) parts.push(`停 < ${rules.stop_below}`);
    return parts.length ? parts.join(" · ") : "待设计划";
  }

  function signalLabel(card) {
    return SIGNAL_LABEL[card.signal] || LEVEL_LABEL[card.level] || "待看";
  }

  function quoteSummary(card) {
    const price = Number(card.price);
    const change = Number(card.change_pct);
    const parts = [];
    if (Number.isFinite(price)) parts.push(`现价 ${price}`);
    if (Number.isFinite(change)) parts.push(`今日 ${change >= 0 ? "+" : ""}${change.toFixed(2)}%`);
    return parts.join(" · ");
  }

  function renderList(today) {
    const host = el("today-list");
    const cards = Array.isArray(today.cards) ? today.cards : [];
    // Cached so the "设线" modal can prefill without a second request.
    state.cards = cards;
    if (!cards.length) {
      host.innerHTML = `<div class="today-empty">还没有自选行情。点上方「编辑自选」添加几只，再点「刷新行情」。</div>`;
      return;
    }
    host.innerHTML = cards
      .map((card) => {
        const facts = (card.facts || []).map((fact) => `<li>${esc(fact)}</li>`).join("");
        const note = card.rules?.note ? `<p class="today-note">你当初写的：${esc(card.rules.note)}</p>` : "";
        const signal = card.signal || card.level || "unknown";
        return `<article class="today-item" data-level="${esc(card.level)}" data-signal="${esc(signal)}">
          <div class="today-item-main">
            <div class="today-item-head">
              <span class="today-flag">${esc(signalLabel(card))}</span>
              <strong>${esc(card.headline)}</strong>
            </div>
            <p class="today-action">${esc(card.action)}${quoteSummary(card) ? ` <span class="today-quote-summary">${esc(quoteSummary(card))}</span>` : ""}</p>
            ${note}
            <details class="today-why"><summary>为什么这么说</summary><ul>${facts}</ul></details>
          </div>
          <div class="today-item-side">
            <span class="today-rule">${esc(ruleSummary(card.rules))}</span>
            <button type="button" class="m2-button" data-edit-rule="${esc(card.symbol)}" data-name="${esc(card.name)}">设线</button>
          </div>
        </article>`;
      })
      .join("");
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    try {
      const data = await request("/api/market/today");
      const today = data.today || {};
      renderVerdict(today);
      renderList(today);
    } catch (error) {
      el("today-verdict-text").textContent = "读取失败";
      el("today-verdict-detail").textContent = error?.message || "请稍后重试。";
    } finally {
      state.loading = false;
    }
  }

  async function refreshQuotes(button) {
    if (ux.wbSetBusy) ux.wbSetBusy(button, true, "刷新中…");
    try {
      await request("/api/market/refresh", { method: "POST", timeoutMs: 30000 });
    } catch (error) {
      el("today-verdict-detail").textContent = `行情刷新失败：${error?.message || "请稍后重试"}`;
    } finally {
      if (ux.wbSetBusy) ux.wbSetBusy(button, false);
      await load();
    }
  }

  function openRuleModal(symbol, name, current) {
    state.editing = symbol;
    el("rule-modal-title").textContent = `给「${name}」设线`;
    el("rule-modal-sub").textContent = "留空表示不设这条线。到线时首屏会提醒你。";
    el("rule-buy").value = current?.buy_below || "";
    el("rule-sell").value = current?.sell_above || "";
    el("rule-stop").value = current?.stop_below || "";
    el("rule-note").value = current?.note || "";
    el("rule-message").textContent = "";
    el("rule-modal").classList.remove("hidden");
    el("rule-buy").focus();
  }

  function closeRuleModal() {
    state.editing = null;
    el("rule-modal").classList.add("hidden");
  }

  // The quote table is rendered by market.js, but should still be able to
  // open the same plan-line editor even when the daily cards have no data.
  window.marketTodayOpenRule = (symbol, name) => {
    const card = (state.cards || []).find((item) => item.symbol === symbol);
    openRuleModal(symbol, name || symbol, card?.rules);
  };

  document.addEventListener("click", (event) => {
    const editButton = event.target.closest("[data-edit-rule]");
    if (editButton) {
      const symbol = editButton.dataset.editRule;
      const card = (state.cards || []).find((item) => item.symbol === symbol);
      openRuleModal(symbol, editButton.dataset.name || symbol, card?.rules);
      return;
    }
    if (event.target.closest("[data-close-rule]")) closeRuleModal();
    if (event.target.id === "rule-modal") closeRuleModal();
    if (event.target.id === "today-refresh") refreshQuotes(event.target);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el("rule-modal").classList.contains("hidden")) closeRuleModal();
  });

  el("rule-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.editing) return;
    const editingSymbol = state.editing;
    const numberOrNull = (id) => {
      const raw = el(id).value.trim();
      if (!raw) return null;
      const value = Number(raw);
      return Number.isFinite(value) && value > 0 ? value : null;
    };
    const message = el("rule-message");
    message.textContent = "保存中…";
    try {
      await request("/api/market/watchlist/rules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: editingSymbol,
          buy_below: numberOrNull("rule-buy"),
          sell_above: numberOrNull("rule-sell"),
          stop_below: numberOrNull("rule-stop"),
          note: el("rule-note").value.trim(),
        }),
      });
      closeRuleModal();
      document.dispatchEvent(new CustomEvent("market:rules-updated", { detail: { symbol: editingSymbol } }));
      await load();
    } catch (error) {
      message.textContent = error?.message || "保存失败";
    }
  });

  document.addEventListener("market:state-updated", () => window.setTimeout(load, 0));
  load();
})();
