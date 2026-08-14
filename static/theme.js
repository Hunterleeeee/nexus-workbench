(() => {
  "use strict";

  if (window.WorkbenchTheme) return;

  // theme.js is present in the <head> of every product page. Load the shared
  // accessibility layer here as well, so standalone pages do not depend on a
  // later project/platform bootstrap just to get focus, motion and state UI.
  if (!document.querySelector("link[data-workbench-theme]")) {
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = "/static/theme.css?v=0.3.197";
    stylesheet.dataset.workbenchTheme = "true";
    document.head.append(stylesheet);
  }

  const STORAGE_KEY = "workbench-theme";
  const DEFAULT_THEME = "light";
  const listeners = new Set();

  const normalize = (value) => value === "dark" ? "dark" : DEFAULT_THEME;
  const readStored = () => {
    try { return normalize(localStorage.getItem(STORAGE_KEY)); }
    catch (_) { return DEFAULT_THEME; }
  };

  const syncToggle = (button) => {
    if (!button) return;
    const dark = document.documentElement.dataset.theme === "dark";
    const nextLabel = dark ? "切换到浅色模式" : "切换到深色模式";
    button.setAttribute("aria-label", nextLabel);
    button.setAttribute("aria-pressed", String(dark));
    button.title = nextLabel;
    if (button.dataset.themeToggleText === "true") button.textContent = dark ? "浅色" : "深色";
  };

  const apply = (value, options = {}) => {
    const theme = normalize(value);
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    if (options.persist) {
      try { localStorage.setItem(STORAGE_KEY, theme); }
      catch (_) { /* Private browsing can deny localStorage writes. */ }
    }
    document.querySelectorAll("[data-theme-toggle]").forEach(syncToggle);
    listeners.forEach((listener) => listener(theme));
    document.dispatchEvent(new CustomEvent("workbench:themechange", { detail: { theme } }));
    return theme;
  };

  const toggle = () => apply(document.documentElement.dataset.theme === "dark" ? "light" : "dark", { persist: true });
  const bindToggle = (button, options = {}) => {
    if (!button) return () => {};
    button.dataset.themeToggle = "true";
    button.dataset.themeToggleText = options.text ? "true" : "false";
    if (button.dataset.themeToggleBound !== "true") {
      button.dataset.themeToggleBound = "true";
      button.addEventListener("click", toggle);
    }
    syncToggle(button);
    return () => { button.removeEventListener("click", toggle); delete button.dataset.themeToggleBound; };
  };
  const subscribe = (listener) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  };

  window.WorkbenchTheme = {
    STORAGE_KEY,
    DEFAULT_THEME,
    get: () => normalize(document.documentElement.dataset.theme),
    apply,
    toggle,
    bindToggle,
    syncToggle,
    subscribe,
  };

  apply(readStored());
  window.addEventListener("storage", (event) => {
    if (event.key === STORAGE_KEY) apply(event.newValue);
  });

  function installSharedUX() {
    const main = document.querySelector("main");
    if (main && !document.querySelector(".skip-link")) {
      if (!main.id) main.id = "wb-main-content";
      if (!main.hasAttribute("tabindex")) main.tabIndex = -1;
      const skip = document.createElement("a");
      skip.className = "skip-link";
      skip.href = `#${main.id}`;
      skip.textContent = "跳到主要内容";
      document.body.prepend(skip);
    }

    const statusSelector = [
      ".platform-status", ".status-message", ".form-note", ".modal-message",
      ".learning-page-status", ".command-status", ".sync-message",
      ".threshold-message", ".settings-message", ".draft-message",
      ".sidebar-inline-status", ".inline-status", ".browser-action-status",
    ].join(",");
    const dialogSelector = ".modal-backdrop, .settings-backdrop";
    const labelSelector = "input:not([type='hidden']), select, textarea";
    const syncLabel = (node) => {
      if (!(node instanceof Element) || !node.matches(labelSelector)) return;
      if (node.getAttribute("aria-label") || node.getAttribute("aria-labelledby") || node.labels?.length) return;
      const fallback = node.getAttribute("placeholder") || node.getAttribute("title") || node.name || node.id;
      if (fallback) node.setAttribute("aria-label", fallback);
    };
    const syncDialog = (node) => {
      if (!(node instanceof Element) || !node.matches(dialogSelector)) return;
      if (!node.hasAttribute("role")) node.setAttribute("role", "dialog");
      if (!node.hasAttribute("aria-modal")) node.setAttribute("aria-modal", "true");
      if (!node.getAttribute("aria-label") && !node.getAttribute("aria-labelledby")) {
        const heading = node.querySelector("h1, h2, h3");
        if (heading) {
          if (!heading.id) heading.id = `${node.id || "wb-dialog"}-title`;
          node.setAttribute("aria-labelledby", heading.id);
        }
      }
    };
    const syncStatus = (node) => {
      if (!(node instanceof Element) || !node.matches(statusSelector)) return;
      const error = node.classList.contains("error") || node.dataset.state === "error";
      node.setAttribute("role", error ? "alert" : "status");
      node.setAttribute("aria-live", error ? "assertive" : "polite");
      node.setAttribute("aria-atomic", "true");
    };
    document.querySelectorAll(statusSelector).forEach(syncStatus);
    document.querySelectorAll(labelSelector).forEach(syncLabel);
    document.querySelectorAll(dialogSelector).forEach(syncDialog);
    if (typeof MutationObserver === "function") {
      new MutationObserver((records) => {
        records.forEach((record) => {
          const target = record.target.nodeType === Node.TEXT_NODE ? record.target.parentElement : record.target;
          syncStatus(target);
          if (record.type === "childList") record.addedNodes.forEach((node) => {
            syncStatus(node);
            syncLabel(node);
            syncDialog(node);
            if (node instanceof Element) {
              node.querySelectorAll(statusSelector).forEach(syncStatus);
              node.querySelectorAll(labelSelector).forEach(syncLabel);
              node.querySelectorAll(dialogSelector).forEach(syncDialog);
            }
          });
        });
      }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["class", "data-state"] });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", installSharedUX, { once: true });
  else installSharedUX();
})();
