// 桌面壳桥：暴露「打开/切换/关闭标签页」与「提交 Basic Auth 凭据」。
// 不暴露 Node、文件系统、凭据或任意 IPC；所有 URL 校验都在主进程完成。
"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktopShell", {
  /** 请求在主进程的新 BrowserWindow 中打开指定网页（真正的独立窗口，不受 iframe X-Frame-Options 限制）。返回 Promise<boolean>。 */
  openWebWindow: (url) => ipcRenderer.invoke("open-web-window", String(url || "")),
  /** 打开一个标签页（导航到 url，同 url 已存在则切换过去）。 */
  openTab: (url) => ipcRenderer.invoke("open-tab", String(url || "")),
  /** 切换到指定标签。 */
  switchTab: (id) => ipcRenderer.send("tab-switch", id),
  /** 关闭指定标签（首页标签除外）。 */
  closeTab: (id) => ipcRenderer.send("tab-close", id),
  /** 新建一个空白标签。 */
  newTab: () => ipcRenderer.send("tab-new"),
  /** 订阅标签列表变化。 */
  onTabsChanged: (callback) => {
    if (typeof callback !== "function") return;
    ipcRenderer.on("tabs-changed", (_event, tabs) => callback(tabs));
  },
  /** AI 浏览器中间的真实网页画布。外部页面在独立、持久化的安全会话里运行。 */
  browserDock: {
    open: (tabId, url, bounds) => ipcRenderer.invoke("browser-dock-open", String(tabId || ""), String(url || ""), bounds || {}),
    activate: (tabId, bounds) => ipcRenderer.invoke("browser-dock-activate", String(tabId || ""), bounds || {}),
    setBounds: (tabId, bounds) => ipcRenderer.invoke("browser-dock-bounds", String(tabId || ""), bounds || {}),
    setVisible: (tabId, visible) => ipcRenderer.invoke("browser-dock-visible", String(tabId || ""), Boolean(visible)),
    navigate: (tabId, command, url = "") => ipcRenderer.invoke("browser-dock-navigate", String(tabId || ""), String(command || ""), String(url || "")),
    snapshot: (tabId) => ipcRenderer.invoke("browser-dock-snapshot", String(tabId || "")),
    perform: (tabId, action) => ipcRenderer.invoke("browser-dock-perform", String(tabId || ""), action && typeof action === "object" ? action : {}),
    close: (tabId) => ipcRenderer.send("browser-dock-close", String(tabId || "")),
    destroy: () => ipcRenderer.send("browser-dock-destroy"),
    bookmarks: {
      list: () => ipcRenderer.invoke("browser-bookmarks-list"),
      save: (bookmark) => ipcRenderer.invoke("browser-bookmarks-save", bookmark && typeof bookmark === "object" ? bookmark : {}),
      remove: (id) => ipcRenderer.invoke("browser-bookmarks-remove", String(id || "")),
    },
    credentials: {
      list: (tabId) => ipcRenderer.invoke("browser-credentials-list", String(tabId || "")),
      save: (tabId, credential) => ipcRenderer.invoke("browser-credentials-save", String(tabId || ""), credential && typeof credential === "object" ? credential : {}),
      capture: (tabId) => ipcRenderer.invoke("browser-credentials-capture", String(tabId || "")),
      fill: (tabId, credentialId) => ipcRenderer.invoke("browser-credentials-fill", String(tabId || ""), String(credentialId || "")),
      remove: (tabId, credentialId) => ipcRenderer.invoke("browser-credentials-remove", String(tabId || ""), String(credentialId || "")),
    },
    onState: (callback) => {
      if (typeof callback !== "function") return () => {};
      const listener = (_event, browserState) => callback(browserState || {});
      ipcRenderer.on("browser-dock-state", listener);
      return () => ipcRenderer.removeListener("browser-dock-state", listener);
    },
    onOpenTabRequest: (callback) => {
      if (typeof callback !== "function") return () => {};
      const listener = (_event, request) => callback(request || {});
      ipcRenderer.on("browser-dock-open-tab-request", listener);
      return () => ipcRenderer.removeListener("browser-dock-open-tab-request", listener);
    },
  },
});

contextBridge.exposeInMainWorld("auth", {
  /** 登录表单提交：把账号密码交给主进程完成 Basic Auth 回调。 */
  submit: (credentials) => ipcRenderer.send("auth-submit", credentials || null),
});
