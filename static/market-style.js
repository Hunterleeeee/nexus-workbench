/* 按流派选股的前端。
 *
 * 后端（/api/market/styles、/api/market/styles/screen）早就写好了，但页面上
 * 一直没有入口——七个流派、每个流派的适用与失效条件、逐条规则的通过情况，
 * 全都只能通过 curl 看到。这个文件把它接上。
 *
 * 一个刻意的设计：这里不做「推荐买什么」。后端只能拿到你自选池里的标的，
 * 所以它能回答的是「按这套标准，你已经在看的这些里哪些现在符合」，
 * 而不是「全市场哪只好」。把它说成推荐就是在骗人。
 */
(() => {
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const state = { styles: [], scanning: false, openStyle: "" };

  function setStatus(message, tone = "") {
    const host = $("#style-status");
    if (!host) return;
    host.textContent = message || "";
    host.dataset.tone = tone;
  }

  function styleCard(style, verdict) {
    // verdict 只有跑过之后才有；没跑过就只展示这套流派本身在说什么。
    const badge = verdict
      ? `<span class="style-verdict ${verdict.tone}">${esc(verdict.label)}</span>`
      : "";
    return `<article class="style-item ${state.openStyle === style.id ? "open" : ""}" data-style-id="${esc(style.id)}">
      <div class="style-item-head">
        <div><strong>${esc(style.name)}</strong><small>${esc(style.thesis)}</small></div>
        ${badge}
      </div>
      <div class="style-item-meta">
        <span>需要 ${esc(style.min_points || 20)} 个样本点</span>
        <button type="button" class="m2-button" data-style-run="${esc(style.id)}">按这套筛我的自选</button>
      </div>
      <details class="style-rules">
        <summary>它怎么判断，什么时候会亏</summary>
        <div><strong>判断规则</strong><ul>${(style.rules || []).map((rule) => `<li>${esc(rule)}</li>`).join("")}</ul></div>
        <div><strong>什么时候管用</strong><p>${esc(style.works_when || "—")}</p></div>
        <div class="style-fails"><strong>什么时候会亏</strong><p>${esc(style.fails_when || "—")}</p></div>
      </details>
    </article>`;
  }

  function renderStyles(verdicts = {}) {
    const host = $("#style-list");
    if (!host) return;
    host.innerHTML = state.styles.length
      ? state.styles.map((style) => styleCard(style, verdicts[style.id])).join("")
      : '<div class="today-empty">没有读到流派清单。</div>';
  }

  function evaluationRow(item) {
    if (item.status !== "ready") {
      return `<tr class="style-blocked"><td>${esc(item.symbol)}</td><td colspan="2">${esc(item.reason || "数据不足")}</td><td>${esc(item.next_step || "")}</td></tr>`;
    }
    // detail 是这条规则算出来的实际数字。只写「通过 / 不通过」等于让人相信一个
    // 黑盒；把数字摆出来，才能自己判断这条规则是不是踩在边界上。
    const checks = (item.checks || []).map((check) => `<li class="${check.passed ? "pass" : "fail"}">${check.passed ? "✓" : "✗"} ${esc(check.label || "")}<small>${esc(check.detail || "")}</small></li>`).join("");
    return `<tr class="${item.hit ? "style-hit" : ""}">
      <td>${esc(item.symbol)}</td>
      <td>${item.hit ? "全部满足" : "未全部满足"}</td>
      <td>${item.score == null ? "—" : esc(item.score)}</td>
      <td><ul class="style-checks">${checks || "<li>—</li>"}</ul></td>
    </tr>`;
  }

  function renderResult(body) {
    const host = $("#style-result");
    if (!host) return;
    host.hidden = false;
    const style = body.style || {};
    const rows = (body.evaluated || []).map(evaluationRow).join("");
    // 「没有一只命中」和「数据不够所以算不了」是两件完全不同的事，
    // 混在一起说会让人以为市场没机会，其实是样本没攒够。
    const banner = body.data_ready
      ? `<p class="style-summary">${esc(body.summary || "")}</p>`
      : `<p class="style-summary warn">${esc(body.summary || "数据不足")}</p>`;
    host.innerHTML = `<div class="style-result-head"><strong>${esc(style.name || "筛选结果")}</strong><button type="button" id="style-result-close" class="m2-button">收起</button></div>
      ${banner}
      <table class="style-table"><thead><tr><th>标的</th><th>是否命中</th><th>得分</th><th>逐条规则</th></tr></thead><tbody>${rows || '<tr><td colspan="4">自选池是空的</td></tr>'}</tbody></table>`;
    host.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function runStyle(styleId) {
    setStatus("正在按这套流派筛你的自选…");
    try {
      const body = await window.requestJson("/api/market/styles/screen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ style_id: styleId, symbols: [] }),
      });
      state.openStyle = styleId;
      renderResult(body);
      setStatus(body.summary || "");
    } catch (error) {
      setStatus(error.message, "error");
    }
  }

  async function scanAll() {
    if (state.scanning || !state.styles.length) return;
    state.scanning = true;
    const button = $("#style-scan");
    button.disabled = true;
    button.textContent = "正在跑…";
    const verdicts = {};
    let ready = 0;
    try {
      // 串行跑：每个风格都要读一遍历史样本，并发起来只会把本地 SQLite 打满，
      // 而流派一共就七个，串行也就是几百毫秒。
      let precondition = "";
      for (const style of state.styles) {
        try {
          const body = await window.requestJson("/api/market/styles/screen", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ style_id: style.id, symbols: [] }),
          });
          const picks = (body.picks || []).length;
          if (!body.data_ready) verdicts[style.id] = { tone: "blocked", label: "数据不够" };
          else if (picks) { verdicts[style.id] = { tone: "hit", label: `${picks} 只命中` }; ready += picks; }
          else verdicts[style.id] = { tone: "none", label: "暂无命中" };
        } catch (error) {
          // 自选池是空的这类前置条件，七个流派会给出七条一模一样的失败。
          // 报七次「跑不动」既没信息量，还会让人以为是流派本身有问题——
          // 认出来就停下，只说一次真正的原因。
          precondition = error.message;
          break;
        }
      }
      if (precondition) {
        renderStyles();
        setStatus(`${precondition}——按流派筛的是你自己的自选池，先在上面「编辑自选」里加几只。`, "warn");
        return;
      }
      renderStyles(verdicts);
      const blocked = Object.values(verdicts).filter((item) => item.tone === "blocked").length;
      setStatus(
        ready
          ? `${ready} 个命中分布在 ${Object.values(verdicts).filter((v) => v.tone === "hit").length} 种流派里；点任意一种看它逐条怎么判的。`
          : blocked === state.styles.length
            ? "全部流派都因为样本不足跑不出结论——先让行情自动化多采几天样本。"
            : "当前自选池在所有流派下都没有命中。这不代表市场没机会，只代表你现在盯的这些不符合这些标准。",
        ready ? "" : "warn",
      );
    } finally {
      state.scanning = false;
      button.disabled = false;
      button.textContent = "一键跑全部流派";
    }
  }

  async function init() {
    if (!$("#style-list")) return;
    try {
      const body = await window.requestJson("/api/market/styles");
      state.styles = body.styles || [];
      renderStyles();
      setStatus(body.note || "");
    } catch (error) {
      $("#style-list").innerHTML = `<div class="today-empty">读取流派清单失败：${esc(error.message)}</div>`;
    }
    $("#style-scan")?.addEventListener("click", () => void scanAll());
    // 委托绑定：卡片每次重渲染都会重建，绑在卡片上会随之丢失。
    $("#style-list").addEventListener("click", (event) => {
      const run = event.target.closest("[data-style-run]");
      if (run) void runStyle(run.dataset.styleRun);
    });
    $("#style-result").addEventListener("click", (event) => {
      if (event.target.closest("#style-result-close")) {
        $("#style-result").hidden = true;
        state.openStyle = "";
      }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else void init();
})();
