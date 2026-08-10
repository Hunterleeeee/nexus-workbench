/* Shared request and recovery primitives for every Workbench page. */
(function initWorkbenchRequest() {
  const DEFAULT_TIMEOUT_MS = 15000;

  class WorkbenchRequestError extends Error {
    constructor(message, options = {}) {
      super(message);
      this.name = "WorkbenchRequestError";
      this.status = options.status ?? 0;
      this.code = options.code || "request_failed";
      this.detail = options.detail || "";
      this.url = options.url || "";
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

  function friendlyErrorMessage(status, detail = "", code = "") {
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
    const { timeoutMs = DEFAULT_TIMEOUT_MS, ...fetchOptions } = options;
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
      throw new WorkbenchRequestError(friendlyErrorMessage(0, "", code), { code, url });
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

  window.WorkbenchUX = Object.assign(window.WorkbenchUX || {}, {
    DEFAULT_TIMEOUT_MS,
    WorkbenchRequestError,
    requestJson,
    friendlyErrorMessage,
    wbSetBusy: setBusy,
    wbRetryMarkup: retryMarkup,
    wbShowRetry(host, message, retryLabel = "重新加载") {
      if (host) host.innerHTML = retryMarkup(message, retryLabel);
    },
  });

  // 统一全局入口：所有页面（含 /crawl4ai 同时加载 app.js + project.js 的场景）
  // 只从这里取 requestJson，避免多个脚本在全局重复声明同名函数。
  window.requestJson = requestJson;
})();
