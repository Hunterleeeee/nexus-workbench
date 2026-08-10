"use strict";

const { app, BrowserWindow, Menu, shell, ipcMain, dialog, WebContentsView } = require("electron");
const path = require("node:path");
const fs = require("node:fs");

const DEFAULT_WORKBENCH_URL = "https://workbench.example.dev:8765/";
const WORKBENCH_URL = (process.env.WORKBENCH_URL || DEFAULT_WORKBENCH_URL).trim();
const TAB_BAR_HEIGHT = 42;

// ── Basic Auth 凭据 ──
// 优先级：环境变量 WORKBENCH_AUTH_USER/PASS > userData/auth.json > 弹出对话框输入。
// 登录成功后写入 userData/auth.json（仅本机可读），之后自动登录。
function authFilePath() {
  return path.join(app.getPath("userData"), "auth.json");
}

function readSavedAuth() {
  try {
    const raw = fs.readFileSync(authFilePath(), "utf8");
    const parsed = JSON.parse(raw);
    if (typeof parsed.username === "string" && typeof parsed.password === "string") {
      return { username: parsed.username, password: parsed.password };
    }
  } catch {
    // 无保存凭据或读取失败，视为未登录
  }
  return null;
}

function saveAuth(username, password) {
  try {
    fs.writeFileSync(authFilePath(), JSON.stringify({ username, password }, null, 2), {
      encoding: "utf8",
      mode: 0o600,
    });
  } catch (error) {
    console.error("[workbench-desktop] 保存登录凭据失败：", error);
  }
}

function resolveCredentials() {
  const envUser = process.env.WORKBENCH_AUTH_USER;
  const envPass = process.env.WORKBENCH_AUTH_PASS;
  if (envUser && envPass) return { username: envUser, password: envPass };
  const saved = readSavedAuth();
  if (saved) return saved;
  return null;
}

let pendingAuthResolve = null;
let loginWindowRef = null;
let savedAuthRejected = false; // 保存的凭据被服务器 401 拒绝过（密码可能已变）

function promptForCredentials(window, authInfo) {
  return new Promise((resolve) => {
    pendingAuthResolve = resolve;
    const host = authInfo && authInfo.host ? authInfo.host : "Workbench";
    const html = `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
    <title>登录 Workbench</title>
    <style>
      :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      body { box-sizing: border-box; display: grid; min-height: 100vh; margin: 0; padding: 28px; place-items: center; background: #f4f6f8; color: #17202a; }
      main { box-sizing: border-box; width: min(400px, 100%); padding: 28px; border: 1px solid #dce2e8; border-radius: 14px; background: #fff; }
      h1 { margin: 0 0 4px; font-size: 18px; }
      p.host { margin: 0 0 18px; color: #5e6b78; font-size: 12px; overflow-wrap: anywhere; }
      label { display: block; margin: 12px 0 4px; color: #3a4551; font-size: 12px; font-weight: 600; }
      input { box-sizing: border-box; width: 100%; padding: 9px 10px; border: 1px solid #ccd4dc; border-radius: 8px; font: 14px inherit; }
      input:focus { outline: 2px solid #1f6feb55; border-color: #1f6feb; }
      button { width: 100%; margin-top: 20px; padding: 10px; border: 0; border-radius: 8px; background: #1f6feb; color: #fff; font: 600 14px inherit; cursor: pointer; }
      button:hover { background: #1958c0; }
      button:disabled { background: #8ab4f8; cursor: default; }
      .error { display: none; margin-top: 12px; color: #c0392b; font-size: 12px; }
      @media (prefers-color-scheme: dark) { body { background: #1c2128; color: #e6edf3; } main { border-color: #30363d; background: #22272e; } p.host, label { color: #9aa7b4; } input { border-color: #3d444d; background: #272c33; color: #e6edf3; } }
    </style>
  </head>
  <body>
    <main>
      <h1>登录 Workbench</h1>
      <p class="host">${escapeHtml(host)}</p>
      <form id="form">
        <label for="username">账号</label>
        <input id="username" name="username" autocomplete="username" autofocus />
        <label for="password">密码</label>
        <input id="password" name="password" type="password" autocomplete="current-password" />
        <button id="submitBtn" type="submit">登录</button>
        <p class="error" id="error">账号或密码不能为空。</p>
      </form>
    </main>
    <script>
      const form = document.getElementById("form");
      const error = document.getElementById("error");
      const button = document.getElementById("submitBtn");
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const username = document.getElementById("username").value.trim();
        const password = document.getElementById("password").value;
        if (!username || !password) { error.style.display = "block"; return; }
        error.style.display = "none";
        button.disabled = true;
        button.textContent = "正在登录…";
        if (window.auth && typeof window.auth.submit === "function") {
          window.auth.submit({ username, password });
        } else {
          error.textContent = "登录桥不可用，请重启应用重试。";
          error.style.display = "block";
          button.disabled = false;
          button.textContent = "登录";
        }
      });
    </script>
  </body>
</html>`;
    if (loginWindowRef && !loginWindowRef.isDestroyed()) {
      loginWindowRef.destroy();
      loginWindowRef = null;
    }
    const loginWindow = new BrowserWindow({
      width: 460,
      height: 430,
      resizable: false,
      minimizable: false,
      maximizable: false,
      title: "登录 Workbench",
      parent: window,
      modal: Boolean(window && !window.isDestroyed()),
      autoHideMenuBar: true,
      webPreferences: {
        preload: path.join(__dirname, "preload.cjs"),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
      },
    });
    loginWindowRef = loginWindow;
    loginWindow.webContents.on("will-navigate", (event) => event.preventDefault());
    loginWindow.on("closed", () => {
      if (loginWindowRef === loginWindow) loginWindowRef = null;
      if (pendingAuthResolve) {
        pendingAuthResolve = null;
        resolve(null);
      }
    });
    void loginWindow.loadURL(`data:text/html;charset=UTF-8,${encodeURIComponent(html)}`).catch(() => {});
  });
}

function attachAuthHandlers(window, state) {
  window.webContents.on("login", (event, _request, authInfo, callback) => {
    event.preventDefault();
    // 先用环境变量/已保存凭据自动尝试；仅当凭据被 401 拒绝过才弹窗（避免密码变更后死循环）
    const credentials = resolveCredentials();
    if (credentials && !savedAuthRejected) {
      savedAuthRejected = true; // 若本次仍 401，下一次 login 事件将走弹窗
      callback(credentials.username, credentials.password);
      return;
    }
    // 无凭据或保存凭据已失效 → 弹登录框（parent 用主窗口）
    promptForCredentials(mainWindow, authInfo).then((credentialsFromUser) => {
      if (credentialsFromUser && credentialsFromUser.username && credentialsFromUser.password) {
        saveAuth(credentialsFromUser.username, credentialsFromUser.password);
        savedAuthRejected = false;
        callback(credentialsFromUser.username, credentialsFromUser.password);
      } else {
        callback(); // 用户取消：不提供凭据
      }
    });
  });
}

// WebContentsView 标签页的登录处理（与主窗口共用同一实现）
function attachAuthHandlersForTab(view, state) {
  attachAuthHandlers(view, state);
}

function registerAuthIpc() {
  ipcMain.on("auth-submit", (_event, credentials) => {
    if (pendingAuthResolve) {
      pendingAuthResolve(credentials || null);
      pendingAuthResolve = null;
    }
    if (loginWindowRef && !loginWindowRef.isDestroyed()) {
      loginWindowRef.close();
    }
  });
}

let workbenchUrl = null;
let workbenchUrlError = null;

function isLoopbackHost(hostname) {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1" || hostname === "[::1]";
}

function parseSafeWebUrl(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    return null;
  }

  if (url.username || url.password) return null;
  if (url.protocol === "https:") return url;
  if (url.protocol === "http:" && isLoopbackHost(url.hostname)) return url;
  return null;
}

function getWorkbenchUrl(rawUrl) {
  const url = parseSafeWebUrl(rawUrl);
  if (!url) {
    throw new Error("WORKBENCH_URL must be an HTTPS URL; HTTP is only allowed for localhost or 127.0.0.1.");
  }
  return url;
}

try {
  workbenchUrl = getWorkbenchUrl(WORKBENCH_URL);
} catch (error) {
  workbenchUrlError = error.message;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function buildErrorPageUrl(title, details, canRetry) {
  const retryMarkup = canRetry
    ? `<a class="button" href="${escapeHtml(workbenchUrl.href)}">重试</a>`
    : "<p>请检查 WORKBENCH_URL 配置：远程地址必须使用 HTTPS。</p>";
  const safeDetails = escapeHtml(details || "页面暂时无法加载。");
  const html = `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
    <title>Workbench</title>
    <style>
      :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      body { box-sizing: border-box; display: grid; min-height: 100vh; margin: 0; padding: 32px; place-items: center; background: #f4f6f8; color: #17202a; }
      main { box-sizing: border-box; width: min(560px, 100%); padding: 36px; border: 1px solid #dce2e8; border-radius: 16px; background: #fff; box-shadow: 0 12px 32px rgb(23 32 42 / 10%); }
      h1 { margin: 0 0 12px; font-size: 24px; }
      p { line-height: 1.6; }
      .details { overflow-wrap: anywhere; color: #5e6b78; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
      .button { display: inline-block; margin-top: 12px; padding: 10px 16px; border-radius: 8px; background: #1f6feb; color: #fff; text-decoration: none; }
      @media (prefers-color-scheme: dark) { body { background: #1c2128; color: #e6edf3; } main { border-color: #30363d; background: #22272e; box-shadow: none; } .details { color: #8b949e; } }
    </style>
  </head>
  <body>
    <main>
      <h1>${escapeHtml(title)}</h1>
      <p>${safeDetails}</p>
      ${retryMarkup}
    </main>
  </body>
</html>`;
  return `data:text/html;charset=UTF-8,${encodeURIComponent(html)}`;
}

function showErrorPage(view, state, title, details) {
  if (!view || view.webContents.isDestroyed()) return;

  state.showingErrorPage = true;
  state.errorPageUrl = buildErrorPageUrl(title, details, Boolean(workbenchUrl));
  void view.webContents.loadURL(state.errorPageUrl).catch(() => {});
}

function focusMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

// ── 标签系统：主窗口加载 shell.html（顶部标签栏），内容区用 WebContentsView 渲染多个页面 ──
const tabs = [];
let tabSeq = 0;
let activeTabId = null;
let mainWindow = null;

function sendTabsChanged() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const list = tabs.map((tab) => ({
    id: tab.id,
    title: tab.title || "加载中…",
    url: tab.url || "",
    active: tab.id === activeTabId,
    isHome: Boolean(tab.isHome),
  }));
  void mainWindow.webContents.send("tabs-changed", list);
}

function layoutTabs() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const [width, height] = mainWindow.getContentSize();
  const viewBounds = { x: 0, y: TAB_BAR_HEIGHT, width, height: Math.max(0, height - TAB_BAR_HEIGHT) };
  for (const tab of tabs) {
    if (!tab.view.webContents.isDestroyed()) tab.view.setBounds(viewBounds);
  }
}

function activateTab(tabId) {
  for (const tab of tabs) {
    const visible = tab.id === tabId;
    if (tab.view && !tab.view.webContents.isDestroyed()) tab.view.setVisible(visible);
  }
  activeTabId = tabId;
  sendTabsChanged();
}

function closeTab(tabId) {
  const index = tabs.findIndex((tab) => tab.id === tabId);
  if (index === -1) return;
  const tab = tabs[index];
  if (tab.isHome) return; // 首页标签不可关闭
  tabs.splice(index, 1);
  if (!tab.view.webContents.isDestroyed()) {
    mainWindow.contentView.removeChildView(tab.view);
    tab.view.webContents.close();
  }
  if (activeTabId === tabId) {
    const next = tabs[index] || tabs[index - 1];
    if (next) activateTab(next.id);
    else if (tabs[0]) activateTab(tabs[0].id);
  } else {
    sendTabsChanged();
  }
}

function findTabByUrl(url) {
  let target = null;
  try {
    const parsed = new URL(url);
    target = parsed.href;
  } catch {
    return null;
  }
  return tabs.find((tab) => {
    try {
      return new URL(tab.url || "").href === target;
    } catch {
      return false;
    }
  });
}

function createTabView(url, options = {}) {
  const safeUrl = parseSafeWebUrl(url);
  if (!safeUrl) return false;

  // 同 URL 标签已存在 → 直接切换过去
  const existing = findTabByUrl(safeUrl.href);
  if (existing) {
    activateTab(existing.id);
    return true;
  }

  const isHome = Boolean(options.isHome);
  const id = ++tabSeq;
  const view = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      devTools: !app.isPackaged,
    },
  });
  const tab = { id, view, url: safeUrl.href, title: isHome ? "首页" : "", isHome };
  tabs.push(tab);
  mainWindow.contentView.addChildView(view);
  layoutTabs();

  const state = { errorPageUrl: null, showingErrorPage: false };

  // Basic Auth 登录处理
  attachAuthHandlersForTab(view, state);

  // 标题更新
  view.webContents.on("page-title-updated", (_event, title) => {
    tab.title = title && String(title).trim() ? String(title).slice(0, 40) : (isHome ? "首页" : tab.url || "");
    sendTabsChanged();
  });
  view.webContents.on("did-navigate", (_event, navigatedUrl) => {
    tab.url = navigatedUrl;
    sendTabsChanged();
  });

  // 新窗口请求（window.open / target=_blank）→ 新标签
  view.webContents.setWindowOpenHandler(({ url: targetUrl }) => {
    openTabInWorkbench(targetUrl);
    return { action: "deny" };
  });

  // 主窗口标签页内导航：同源留在当前标签，跨源 http/https 新开标签，非 web 协议拦截
  view.webContents.on("will-frame-navigate", (event, targetUrl) => {
    if (targetUrl === state.errorPageUrl) return;
    const safeTarget = parseSafeWebUrl(targetUrl);
    if (!safeTarget) {
      event.preventDefault();
      return;
    }
    if (safeTarget.origin === workbenchUrl.origin) return; // 同源留在当前标签
    event.preventDefault();
    openTabInWorkbench(safeTarget.href);
  });
  view.webContents.on("will-redirect", (event, targetUrl) => {
    if (targetUrl === state.errorPageUrl) return;
    const safeTarget = parseSafeWebUrl(targetUrl);
    if (!safeTarget) {
      event.preventDefault();
      return;
    }
    if (safeTarget.origin === workbenchUrl.origin) return;
    event.preventDefault();
    openTabInWorkbench(safeTarget.href);
  });
  view.webContents.on("will-attach-webview", (event) => event.preventDefault());
  view.webContents.on("did-fail-load", (_event, errorCode, errorDescription, _validatedURL, isMainFrame) => {
    if (!isMainFrame || errorCode === -3 || state.showingErrorPage) return;
    showErrorPage(view, state, "无法连接 Workbench", errorDescription || "页面加载失败，请检查服务器和网络后重试。");
  });

  void view.webContents.loadURL(safeUrl.href).catch(() => {});
  activateTab(id);
  return true;
}

function openTabInWorkbench(url) {
  return createTabView(url);
}

// ── 旧独立窗口能力（保留 IPC 兼容：open-web-window 也走标签）──
function createWebWindow(url) {
  return createTabView(url);
}

function registerIpcHandlers() {
  ipcMain.handle("open-web-window", (_event, rawUrl) => {
    try {
      return createTabView(String(rawUrl || ""));
    } catch {
      return false;
    }
  });
  ipcMain.handle("open-tab", (_event, rawUrl) => {
    try {
      return createTabView(String(rawUrl || ""));
    } catch {
      return false;
    }
  });
  ipcMain.on("tab-switch", (_event, tabId) => {
    const found = tabs.find((tab) => tab.id === tabId);
    if (found) activateTab(found.id);
  });
  ipcMain.on("tab-close", (_event, tabId) => closeTab(tabId));
  ipcMain.on("tab-new", () => {
    createTabView(workbenchUrl.href, { isHome: false });
  });
}

function createWindow() {
  const state = {
    errorPageUrl: null,
    showingErrorPage: false,
  };
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1120,
    minHeight: 720,
    title: "Workbench",
    backgroundColor: "#f4f6f8",
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      devTools: !app.isPackaged,
    },
  });
  mainWindow = window;

  window.once("ready-to-show", () => {
    if (!window.isDestroyed()) window.show();
  });
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
  });
  window.on("resize", () => layoutTabs());

  // 加载标签栏壳页面
  void window.loadFile(path.join(__dirname, "shell.html")).catch(() => {});
  window.webContents.on("did-finish-load", () => {
    if (!window.isDestroyed() && workbenchUrl) {
      // 首页标签（不可关闭）
      createTabView(workbenchUrl.href, { isHome: true });
    }
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    openTabInWorkbench(url);
    return { action: "deny" };
  });

  return window;
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    focusMainWindow();
  });

  app.whenReady().then(() => {
    Menu.setApplicationMenu(null);
    registerIpcHandlers();
    registerAuthIpc();
    createWindow();
    app.on("activate", () => {
      if (!mainWindow || mainWindow.isDestroyed()) createWindow();
      else focusMainWindow();
    });
  }).catch(() => {
    app.quit();
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
}
