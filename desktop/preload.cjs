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
});

contextBridge.exposeInMainWorld("auth", {
  /** 登录表单提交：把账号密码交给主进程完成 Basic Auth 回调。 */
  submit: (credentials) => ipcRenderer.send("auth-submit", credentials || null),
});
