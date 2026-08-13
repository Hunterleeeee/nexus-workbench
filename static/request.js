/* Shared request and recovery primitives for every Workbench page. */
(function initWorkbenchRequest() {
  // 读取类请求：15s 足够，超时基本等于服务端真的不健康。
  const DEFAULT_TIMEOUT_MS = 15000;
  // 写入 / 触发 Agent 类请求：服务端 LLM 读超时是 120s、nginx proxy_read_timeout 是
  // 300s，客户端却统一卡 15s —— 这就是「明明成功了却报失败」的来源。对齐到 120s。
  const MUTATION_TIMEOUT_MS = 120000;
  const MUTATION_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

  class WorkbenchRequestError extends Error {
    constructor(message, options = {}) {
      super(message);
      this.name = "WorkbenchRequestError";
      this.status = options.status ?? 0;
      this.code = options.code || "request_failed";
      this.detail = options.detail || "";
      this.url = options.url || "";
      // 浏览器端 abort 只切断本地这条连接，服务端该写的库、该调的 LLM 一样会跑完。
      // 所以「写入类请求超时」不等于「操作失败」，调用方不该按失败展示，更不该自动重试。
      this.mayHaveSucceeded = Boolean(options.mayHaveSucceeded);
    }
  }

  function detailText(body) {
    if (!body) return "";
    if (typeof body === "string") return body.trim();
    const detail = body.detail ?? body.message ?? body.error;
    if (Array.isArray(detail)) return detail.map((item) => item?.msg || item?.message || String(item)).join("；");
    if (detail && typeof detail === "object") return detail.message || JSON.stringify(detail);
    return detail == null ? "" : String(detail);
  }

  function friendlyErrorMessage(status, detail = "", code = "", mayHaveSucceeded = false) {
    if (code === "timeout" && mayHaveSucceeded) {
      return "等待服务器响应超时。中断浏览器请求并不会取消服务端处理，这次操作可能已经完成——请刷新页面确认后再决定是否重试，直接重试可能会创建重复记录。";
    }
    if (code === "timeout") return "请求超时，请稍后重试。";
    if (code === "network") return "网络连接失败，请检查线上入口后重试。";
    if (status === 401) return "线上入口需要认证，请先完成登录后再试。";
    if (status === 403) return "当前操作需要额外权限，请检查登录状态或确认权限。";
    if (status === 404) return "当前功能在这个线上版本不可用，请先确认部署版本。";
    if (status === 408) return "请求超时，请稍后重试。";
    if (status === 429) return "请求过于频繁，请稍后再试。";
    if (status >= 500) return "服务暂时不可用，请稍后重试；如果持续失败，请查看运行状态。";
    return detail || (status ? `请求未完成（${status}）` : "请求未完成，请稍后重试。");
  }

  function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }

  async function requestJson(url, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const isMutation = MUTATION_METHODS.has(method);
    const { timeoutMs = isMutation ? MUTATION_TIMEOUT_MS : DEFAULT_TIMEOUT_MS, ...fetchOptions } = options;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const upstreamSignal = fetchOptions.signal;
    let timer = null;
    let timedOut = false;
    if (controller) {
      fetchOptions.signal = controller.signal;
      if (upstreamSignal) {
        if (upstreamSignal.aborted) controller.abort();
        else upstreamSignal.addEventListener("abort", () => controller.abort(), { once: true });
      }
      timer = window.setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    }
    let response;
    try {
      response = await fetch(url, fetchOptions);
    } catch (error) {
      const code = timedOut || error?.name === "AbortError" ? "timeout" : "network";
      // 只有"确实发出去了但没等到回应"才可能已经成功；调用方主动 abort 的不算。
      const mayHaveSucceeded = isMutation && timedOut;
      throw new WorkbenchRequestError(friendlyErrorMessage(0, "", code, mayHaveSucceeded), { code, url, mayHaveSucceeded });
    } finally {
      if (timer !== null) window.clearTimeout(timer);
    }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = detailText(body);
      throw new WorkbenchRequestError(friendlyErrorMessage(response.status, detail), { status: response.status, detail, url });
    }
    return body;
  }

  function setBusy(button, busy, busyLabel = "处理中…") {
    if (!button) return;
    if (busy) {
      if (!button.dataset.wbIdleLabel) button.dataset.wbIdleLabel = button.textContent.trim();
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = busyLabel;
      return;
    }
    button.disabled = false;
    button.removeAttribute("aria-busy");
    if (button.dataset.wbIdleLabel) {
      button.textContent = button.dataset.wbIdleLabel;
      delete button.dataset.wbIdleLabel;
    }
  }

  function retryMarkup(message, retryLabel = "重新加载") {
    const safe = String(message || "服务暂时不可用");
    return `<div class="wb-retry-state" role="alert"><strong>暂时无法读取</strong><p>${escapeHtml(safe)}</p><button type="button" class="secondary-button wb-retry-button" data-wb-retry>${escapeHtml(retryLabel)}</button></div>`;
  }

  // 流式对话消费：POST 到 SSE 接口，逐块回调。返回 Promise，resolve 时拿到完整文本。
  // events: { onDelta(text, reasoning), onReset(payload), onFinish(payload), onError(message, payload) }
  async function fetchStream(url, body, events = {}) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* 非 JSON 错误体 */ }
      throw new Error(detail);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const full = [];
    let finishPayload = null;
    let terminalError = "";
    let terminalErrorPayload = null;
    const consumeBlock = (block) => {
      for (const line of block.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") continue;
        let payload;
        try { payload = JSON.parse(data); } catch (_) { continue; }
        if (payload.type === "delta" || payload.type === "delta_text") {
          if (payload.text) full.push(payload.text);
          events.onDelta?.(payload.text || "", payload.reasoning || "");
        } else if (payload.type === "reset") {
          full.length = 0;
          events.onReset?.(payload);
        } else if (payload.type === "event") {
          events.onEvent?.(payload);
        } else if (payload.type === "finish") {
          finishPayload = payload;
          terminalError = "";
          terminalErrorPayload = null;
          events.onFinish?.(payload);
        } else if (payload.type === "error") {
          events.onError?.(payload.message || "流式输出失败", payload);
          if (!payload.recoverable) {
            terminalError = payload.message || "流式输出失败";
            terminalErrorPayload = payload;
          }
        }
      }
    };
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop();
      blocks.forEach(consumeBlock);
    }
    buffer += decoder.decode();
    if (buffer.trim()) consumeBlock(buffer);
    if (!finishPayload) {
      const error = new WorkbenchRequestError(terminalError || "流式连接在完成前中断，请重试。", {
        code: terminalError ? "stream_failed" : "stream_interrupted",
        detail: terminalError,
        url,
      });
      error.payload = terminalErrorPayload;
      throw error;
    }
    return full.join("");
  }

  window.WorkbenchUX = Object.assign(window.WorkbenchUX || {}, {
    DEFAULT_TIMEOUT_MS,
    MUTATION_TIMEOUT_MS,
    WorkbenchRequestError,
    requestJson,
    friendlyErrorMessage,
    wbSetBusy: setBusy,
    wbRetryMarkup: retryMarkup,
    fetchStream,
    wbShowRetry(host, message, retryLabel = "重新加载") {
      if (host) host.innerHTML = retryMarkup(message, retryLabel);
    },
  });

  // 统一全局入口：所有页面（含 /crawl4ai 同时加载 app.js + project.js 的场景）
  // 只从这里取 requestJson，避免多个脚本在全局重复声明同名函数。
  window.requestJson = requestJson;
})();
