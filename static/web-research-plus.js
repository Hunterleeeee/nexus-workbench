/* AI-browser add-ons for the research page.
 *
 * Loaded after web-research.js and reuses its globals (state, activeContext,
 * copilotAsk) rather than forking them, so tab handling stays in one place.
 * Adds: a vertical auto-grouped tab rail, @ context mentions, screenshot-into-
 * chat, and the goal-driven research agent.
 */
(function initResearchPlus() {
  const q = (selector) => document.querySelector(selector);
  const esc = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

  const plus = { mentions: [], popupOpen: false, agentPoll: null, mode: "auto" };

  /* ── 真实网页操作：AI 只产出结构化步骤，桌面壳逐步执行 ───── */
  const actionIntent = /(点击|点开|输入|填写|选择|滚动|向下|向上|后退|前进|刷新|搜索(?:一下)?|打开(?:这个|该|链接)|帮我(?:点|填|选|找)|click|fill|type|select|scroll|go back|go forward|reload)/i;
  function showActionStatus(message, kind = "working") {
    const host = q("#browser-action-status");
    if (!host) return;
    host.hidden = !message;
    host.dataset.kind = kind;
    host.textContent = message || "";
  }
  async function runBrowserCommand(instruction) {
    const dock = window.desktopShell?.browserDock;
    const context = typeof activeContext === "function" ? activeContext() : null;
    if (!dock || !context?.url) return false;
    const input = q("#chat-input");
    if (input) input.value = "";
    if (typeof appendMessage === "function") appendMessage("user", instruction, { kind: "browser_action", runId: context.runId || "" });
    showActionStatus("正在读取当前真实网页并规划操作…");
    const submit = q("#chat-submit");
    if (submit) submit.disabled = true;
    try {
      const snapshotResult = await dock.snapshot(context.id);
      if (!snapshotResult?.ok || !snapshotResult.snapshot) throw new Error(snapshotResult?.message || "当前网页还没有准备好");
      const snapshot = snapshotResult.snapshot;
      const plan = await window.requestJson("/api/web-research/browser-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          instruction,
          page_title: snapshot.title || "",
          page_url: snapshot.url || context.url,
          page_text: String(snapshot.text || "").slice(0, 12000),
          elements: (snapshot.elements || []).slice(0, 160),
        }),
        timeoutMs: 120000,
      });
      const actions = Array.isArray(plan.actions) ? plan.actions.slice(0, 5) : [];
      if (!actions.length) {
        showActionStatus(plan.summary || "没有找到安全、明确的可执行步骤。", "error");
        if (typeof appendMessage === "function") appendMessage("assistant", plan.summary || "我没有找到与指令匹配的可操作控件。", { kind: "browser_action" });
        return true;
      }
      const completed = [];
      for (let index = 0; index < actions.length; index += 1) {
        const action = { ...actions[index] };
        showActionStatus(`正在执行第 ${index + 1}/${actions.length} 步：${action.reason || action.type}`);
        let result = await dock.perform(context.id, action);
        if (result?.requiresConfirmation) {
          const confirmed = window.confirm(`AI 准备执行“${result.confirmationLabel || "敏感操作"}”。这可能提交、发送或修改外部数据，确认继续吗？`);
          if (!confirmed) {
            completed.push("已按你的选择停止敏感操作");
            break;
          }
          result = await dock.perform(context.id, { ...action, confirmed: true });
        }
        if (!result?.ok) throw new Error(result?.message || `第 ${index + 1} 步执行失败`);
        completed.push(result.message || action.reason || action.type);
        if (["click", "navigate", "back", "forward", "reload"].includes(action.type)) await new Promise((resolve) => setTimeout(resolve, 720));
      }
      const answer = `${plan.summary || "操作已完成。"}\n${completed.map((item) => `· ${item}`).join("\n")}`.trim();
      if (typeof appendMessage === "function") appendMessage("assistant", answer, { kind: "browser_action" });
      showActionStatus("操作完成，正在重新同步页面给 AI…", "success");
      await new Promise((resolve) => setTimeout(resolve, 450));
      if (typeof refreshNativeSnapshot === "function") await refreshNativeSnapshot({ force: true });
      window.setTimeout(() => showActionStatus("", "success"), 2200);
      return true;
    } catch (error) {
      const message = `网页操作没有完成：${error?.message || "请稍后重试"}`;
      showActionStatus(message, "error");
      if (typeof appendMessage === "function") appendMessage("assistant", message, { kind: "error" });
      return true;
    } finally {
      if (typeof enableCopilot === "function") enableCopilot();
      else if (submit) submit.disabled = !context.url;
    }
  }

  const browserActionForm = q("#chat-form");
  if (browserActionForm) {
    browserActionForm.addEventListener("submit", (event) => {
      const instruction = q("#chat-input")?.value.trim() || "";
      const shouldAct = plus.mode === "act" || (plus.mode === "auto" && actionIntent.test(instruction));
      if (!instruction || !shouldAct || !window.desktopShell?.browserDock) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      void runBrowserCommand(instruction);
    }, true);
  }

  function setAssistantMode(mode) {
    if (!["auto", "ask", "act"].includes(mode)) return;
    plus.mode = mode;
    document.documentElement.dataset.assistantMode = mode;
    q("#assistant-mode")?.querySelectorAll("[data-assistant-mode]").forEach((button) => {
      const active = button.dataset.assistantMode === mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const input = q("#chat-input");
    if (input) input.placeholder = mode === "ask"
      ? "问当前页面的内容、风险或含义…"
      : mode === "act"
        ? "告诉 AI 要在网页上完成什么…"
        : "问页面，或说“点击/输入/滚动”…";
    if (typeof syncCopilotAvailability === "function") syncCopilotAvailability();
  }

  q("#assistant-mode")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-assistant-mode]");
    if (button) setAssistantMode(button.dataset.assistantMode);
  });

  q("#copilot-quick")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-browser-command]");
    if (!button || button.disabled) return;
    const dock = window.desktopShell?.browserDock;
    const context = typeof activeContext === "function" ? activeContext() : null;
    if (!dock || !context?.id) return;
    const command = button.dataset.browserCommand || "";
    button.disabled = true;
    showActionStatus(command === "回到页面顶部" ? "正在回到页面顶部…" : "正在滚动真实网页…");
    try {
      const action = command === "回到页面顶部"
        ? { type: "scroll", edge: "top", amount: -1600 }
        : { type: "scroll", amount: Math.max(520, Math.round(window.innerHeight * 0.72)) };
      const result = await dock.perform(context.id, action);
      showActionStatus(result?.message || "滚动完成", result?.ok ? "success" : "error");
      if (result?.ok && typeof refreshNativeSnapshot === "function") await refreshNativeSnapshot({ analyze: false });
    } catch (error) {
      showActionStatus(`滚动失败：${error?.message || "请稍后重试"}`, "error");
    } finally {
      if (typeof enableCopilot === "function") enableCopilot();
      else button.disabled = !activeContext()?.url;
      window.setTimeout(() => showActionStatus("", "success"), 1800);
    }
  });


  /* ── @ 引用上下文 ──────────────────────────────────────────── */
  function renderChips() {
    const host = q("#mention-chips");
    if (!host) return;
    host.hidden = plus.mentions.length === 0;
    host.innerHTML = plus.mentions
      .map(
        (mention, index) =>
          `<span class="mention-chip" data-type="${esc(mention.type)}">@${esc(mention.label)}<button type="button" data-drop-mention="${index}" aria-label="移除引用">×</button></span>`
      )
      .join("");
  }

  async function openMentionPopup(query = "") {
    const popup = q("#mention-popup");
    if (!popup) return;
    popup.hidden = false;
    plus.popupOpen = true;
    popup.innerHTML = `<div class="mention-loading">搜索中…</div>`;
    let items = [];
    try {
      const data = await window.requestJson(`/api/web-research/mentionables?q=${encodeURIComponent(query)}`);
      items = data.items || [];
    } catch (_) {
      items = [];
    }
    const tabs = (typeof state !== "undefined" ? state.contexts || [] : [])
      .filter((tab) => tab.url && (!query || (tab.title || "").toLowerCase().includes(query.toLowerCase())))
      .map((tab) => ({ type: "tab", id: tab.id, label: tab.title || tab.url, hint: "已打开的标签页" }));
    const all = [...tabs, ...items].slice(0, 24);
    popup.innerHTML = all.length
      ? all
          .map(
            (item, index) =>
              `<button type="button" class="mention-option" data-mention-index="${index}" role="option">
                 <strong>${esc(item.label)}</strong><small>${esc(item.hint || item.type)}</small>
               </button>`
          )
          .join("")
      : `<div class="mention-loading">没有匹配的内容</div>`;
    popup.dataset.payload = JSON.stringify(all);
  }

  function closeMentionPopup() {
    const popup = q("#mention-popup");
    if (popup) popup.hidden = true;
    plus.popupOpen = false;
  }

  function addMention(item) {
    if (plus.mentions.length >= 8) return;
    if (plus.mentions.some((mention) => mention.type === item.type && mention.id === item.id)) return;
    const entry = { type: item.type, id: item.id, label: item.label };
    if (item.type === "tab" && typeof state !== "undefined") {
      const tab = (state.contexts || []).find((candidate) => candidate.id === item.id);
      // Send the already-extracted reader text; the server never re-fetches.
      entry.text = tab
        ? `${tab.title || ""}\n${tab.url || ""}\n${(tab.readerText || "").slice(0, 2_400)}`
        : "";
    }
    plus.mentions.push(entry);
    renderChips();
    closeMentionPopup();
    q("#chat-input")?.focus();
  }

  document.addEventListener("click", (event) => {
    const drop = event.target.closest("[data-drop-mention]");
    if (drop) {
      plus.mentions.splice(Number(drop.dataset.dropMention), 1);
      renderChips();
      return;
    }
    const option = event.target.closest("[data-mention-index]");
    if (option) {
      const payload = JSON.parse(q("#mention-popup").dataset.payload || "[]");
      const item = payload[Number(option.dataset.mentionIndex)];
      if (item) addMention(item);
      return;
    }
    if (event.target.id === "mention-open") {
      void openMentionPopup("");
      return;
    }
    if (plus.popupOpen && !event.target.closest("#mention-popup")) closeMentionPopup();
  });

  const chatInput = q("#chat-input");
  if (chatInput) {
    chatInput.addEventListener("input", () => {
      const value = chatInput.value;
      const match = value.slice(0, chatInput.selectionStart).match(/@([^\s@]*)$/);
      if (match) void openMentionPopup(match[1]);
      else if (plus.popupOpen) closeMentionPopup();
    });
  }

  async function buildMentionPreamble() {
    if (!plus.mentions.length) return "";
    let resolved = [];
    try {
      const data = await window.requestJson("/api/web-research/mentions/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mentions: plus.mentions }),
      });
      resolved = data.resolved || [];
    } catch (_) {
      return "";
    }
    if (!resolved.length) return "";
    const blocks = resolved.map((item) => `【引用：${item.label}】\n${item.text}`).join("\n\n");
    return `以下是我额外引用的上下文，请结合它们回答：\n\n${blocks}\n\n---\n\n`;
  }

  /* Capture phase so the original submit handler does not also fire. */
  const chatForm = q("#chat-form");
  if (chatForm) {
    chatForm.addEventListener(
      "submit",
      (event) => {
        if (!plus.mentions.length) return; // no mentions: let the original flow run
        event.preventDefault();
        event.stopImmediatePropagation();
        const text = chatInput.value.trim();
        if (!text) return;
        void (async () => {
          const preamble = await buildMentionPreamble();
          plus.mentions = [];
          renderChips();
          if (typeof copilotAsk === "function") await copilotAsk(preamble + text);
        })();
      },
      true
    );
  }

  /* ── 截图入对话 ───────────────────────────────────────────── */
  const shotButton = q("#shot-to-chat");
  function syncShotButton() {
    if (!shotButton) return;
    const context = typeof activeContext === "function" ? activeContext() : null;
    shotButton.disabled = !context?.url || !context?.runId;
  }
  setInterval(syncShotButton, 1200);

  if (shotButton) {
    shotButton.addEventListener("click", async () => {
      const context = typeof activeContext === "function" ? activeContext() : null;
      if (!context?.url) return;
      shotButton.disabled = true;
      const previous = shotButton.textContent;
      shotButton.textContent = "截图中…";
      try {
        const data = await window.requestJson("/api/browser/render", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: context.url }),
          timeoutMs: 60000,
        });
        if (!data.ok) throw new Error(data.message || "截图失败");
        const host = q("#copilot-messages");
        if (host) {
          const card = document.createElement("div");
          card.className = "chat-shot";
          card.innerHTML = `<a href="${esc(data.url)}" target="_blank" rel="noopener"><img src="${esc(data.url)}" alt="页面截图" loading="lazy" /></a>
            <small>截图已保存，供你核对当前页面版面。AI 当前读取的是页面正文，不是图片像素。</small>`;
          host.appendChild(card);
          host.scrollTop = host.scrollHeight;
        }
      } catch (error) {
        const host = q("#copilot-messages");
        if (host) {
          const note = document.createElement("div");
          note.className = "chat-hint";
          note.textContent = `截图失败：${error?.message || "请稍后重试"}`;
          host.appendChild(note);
        }
      } finally {
        shotButton.textContent = previous;
        syncShotButton();
      }
    });
  }

  /* ── Agent 自动执行 ───────────────────────────────────────── */
  function renderAgentRun(run) {
    const host = q("#agent-result");
    const status = q("#agent-status");
    if (!host) return;
    const result = run.result || {};
    if (run.status === "succeeded" && result.answer) {
      const sources = (result.sources || [])
        .map((source, index) => `<li>[来源 ${index + 1}] <a href="${esc(source.url)}" target="_blank" rel="noopener">${esc(source.title || source.url)}</a></li>`)
        .join("");
      host.innerHTML = `<div class="agent-answer"><p>${esc(result.answer).replaceAll("\n", "<br />")}</p></div>
        <div class="agent-sources"><h4>读了 ${result.pages_read || 0} 个页面</h4><ol>${sources}</ol></div>`;
      if (status) status.textContent = "完成";
      return;
    }
    if (run.status === "failed") {
      host.innerHTML = `<div class="empty-result compact"><strong>没有得到结论</strong><p>${esc(run.error || "请换一个起始页面再试")}</p></div>`;
      if (status) status.textContent = "失败";
      return;
    }
    const events = (run.events || []).slice(-4).map((item) => `<li>${esc(item.message || "")}</li>`).join("");
    host.innerHTML = `<div class="agent-progress"><strong>正在读页面…</strong><ul>${events}</ul></div>`;
    if (status) status.textContent = run.status === "running" ? "读取中" : "排队中";
  }

  async function pollAgent(runId) {
    try {
      const data = await window.requestJson(`/api/web-research/agent/${encodeURIComponent(runId)}`);
      const run = data.run || {};
      renderAgentRun(run);
      if (["queued", "running"].includes(run.status)) {
        plus.agentPoll = window.setTimeout(() => void pollAgent(runId), 2500);
      }
    } catch (error) {
      const status = q("#agent-status");
      if (status) status.textContent = error?.message || "读取失败";
    }
  }

  const agentForm = q("#agent-form");
  if (agentForm) {
    agentForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const status = q("#agent-status");
      const button = q("#agent-start-button");
      if (plus.agentPoll) window.clearTimeout(plus.agentPoll);
      if (button) button.disabled = true;
      if (status) status.textContent = "提交中…";
      try {
        const data = await window.requestJson("/api/web-research/agent", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            goal: q("#agent-goal").value.trim(),
            start_url: q("#agent-start").value.trim(),
            max_pages: Number(q("#agent-max").value) || 6,
            render_js: q("#agent-render").checked,
          }),
          timeoutMs: 30000,
        });
        if (status) status.textContent = "已开始";
        void pollAgent(data.run_id);
      } catch (error) {
        if (status) status.textContent = error?.message || "提交失败";
      } finally {
        if (button) button.disabled = false;
      }
    });
  }

  /* ── 从别的页面带着目标进来（例如量化候选股「查最近消息」） ── */
  (function applyIncomingAgentGoal() {
    const params = new URLSearchParams(window.location.search);
    const goal = String(params.get("agent_goal") || "").trim().slice(0, 2000);
    if (!goal) return;
    let startUrl = String(params.get("agent_start") || "").trim().slice(0, 2000);
    // 兜底：调用方忘了带起始页面时，用目标里的关键词凑一个搜索页，而不是留空。
    // 留空的后果是「问题填好了，但不知道从哪开始查」——这正是这个入口要解决的事。
    let startIsGuess = false;
    if (!startUrl) {
      const keywords = goal.replace(/[（(）)：:，,。.、；;？?]/g, " ").split(/\s+/).filter((word) => word.length > 1).slice(0, 6).join(" ");
      if (keywords) {
        startUrl = `https://www.bing.com/search?q=${encodeURIComponent(keywords)}`;
        startIsGuess = true;
      }
    }
    const drawer = q("#agent-drawer");
    const goalInput = q("#agent-goal");
    const startInput = q("#agent-start");
    if (goalInput) goalInput.value = goal;
    if (startInput && startUrl) startInput.value = startUrl;
    if (drawer) {
      drawer.open = true;
      drawer.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    const status = q("#agent-status");
    // Deliberately not auto-submitted: the user picks the starting page and
    // confirms before the agent spends time crawling.
    if (status) {
      status.textContent = !startUrl
        ? "已填好目标，填一个起始页面（例如该股在财经站的页面）后点「开始」。"
        : startIsGuess
          ? "已填好目标；起始页面是按关键词猜的一个搜索页，可以改成更合适的来源再点「开始」。"
          : "已填好目标和起始页面，确认后点「开始」。";
    }
  })();

  setAssistantMode("auto");
  syncShotButton();
})();
