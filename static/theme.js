(() => {
  "use strict";

  if (window.WorkbenchTheme) return;

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
})();
