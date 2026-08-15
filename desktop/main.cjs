"use strict";

const { app, BrowserWindow, Menu, shell, ipcMain, dialog, WebContentsView, safeStorage } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { randomUUID } = require("node:crypto");

const DEFAULT_WORKBENCH_URL = "https://workbench.example.dev/";
const WORKBENCH_URL = (process.env.WORKBENCH_URL || DEFAULT_WORKBENCH_URL).trim();
const TAB_BAR_HEIGHT = 42;
const BROWSER_DOCK_PARTITION = "persist:workbench-ai-browser";

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
    const host = authInfo && authInfo.host ? authInfo.host : "NEXUS";
    const html = `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
    <title>登录 NEXUS</title>
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
      <h1>登录 NEXUS</h1>
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
      title: "登录 NEXUS",
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
  window.webContents.on("login", (event, request, authInfo, callback) => {
    event.preventDefault();
    // 只处理 Workbench 同源的 Basic Auth；外部站点（如热点「阅读原文」）的
    // 401/403 挑战一律放行给站点自己处理，绝不用 Workbench 凭据去试或弹登录窗。
    let requestUrl = "";
    try {
      requestUrl = String((request && request.url) || "").trim();
    } catch {
      requestUrl = "";
    }
    const isWorkbenchRequest =
      workbenchUrl &&
      (requestUrl.startsWith(workbenchUrl.origin) ||
        requestUrl === "" ||
        requestUrl === "about:blank");
    if (!isWorkbenchRequest) {
      callback(); // 不提供凭据，交给页面自身处理
      return;
    }
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

function parseBrowserDockUrl(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    return null;
  }
  if (url.username || url.password || !url.hostname) return null;
  return ["http:", "https:"].includes(url.protocol) ? url : null;
}

function browserBookmarksFilePath() {
  return path.join(app.getPath("userData"), "browser_bookmarks.json");
}

function browserCredentialsFilePath() {
  return path.join(app.getPath("userData"), "browser_credentials.json");
}

function readPrivateJsonArray(filePath) {
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writePrivateJsonArray(filePath, rows) {
  const temporaryPath = `${filePath}.tmp`;
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(temporaryPath, JSON.stringify(rows, null, 2), { encoding: "utf8", mode: 0o600 });
  fs.renameSync(temporaryPath, filePath);
  try { fs.chmodSync(filePath, 0o600); } catch { /* Best effort on non-POSIX filesystems. */ }
}

function normalizeBrowserTabKey(rawKey) {
  const value = String(rawKey || "").trim();
  return /^[a-zA-Z0-9._:-]{1,120}$/.test(value) ? value : "";
}

function credentialOriginFromUrl(rawUrl) {
  const parsed = parseBrowserDockUrl(String(rawUrl || ""));
  return parsed ? parsed.origin : "";
}

function listBrowserBookmarks() {
  return readPrivateJsonArray(browserBookmarksFilePath())
    .map((item) => {
      const url = parseBrowserDockUrl(String(item?.url || ""));
      if (!url || !item?.id) return null;
      return {
        id: String(item.id).slice(0, 120),
        title: String(item.title || url.hostname).trim().slice(0, 160),
        url: url.href,
        createdAt: String(item.createdAt || ""),
        updatedAt: String(item.updatedAt || item.createdAt || ""),
      };
    })
    .filter(Boolean)
    .slice(0, 500);
}

function saveBrowserBookmark(rawBookmark) {
  const target = parseBrowserDockUrl(String(rawBookmark?.url || ""));
  if (!target) return { ok: false, message: "这个网址不能保存为书签" };
  const now = new Date().toISOString();
  const bookmarks = listBrowserBookmarks();
  const existing = bookmarks.find((item) => item.url === target.href);
  const record = {
    id: existing?.id || randomUUID(),
    title: String(rawBookmark?.title || existing?.title || target.hostname).trim().slice(0, 160) || target.hostname,
    url: target.href,
    createdAt: existing?.createdAt || now,
    updatedAt: now,
  };
  const next = [record, ...bookmarks.filter((item) => item.id !== record.id)].slice(0, 500);
  writePrivateJsonArray(browserBookmarksFilePath(), next);
  return { ok: true, bookmark: record };
}

function listCredentialRecords() {
  return readPrivateJsonArray(browserCredentialsFilePath())
    .filter((item) => item && item.id && item.origin && item.username && item.passwordEncrypted)
    .slice(0, 200);
}

function publicCredential(record) {
  return {
    id: String(record.id),
    origin: String(record.origin),
    username: String(record.username),
    hasPassword: true,
    createdAt: String(record.createdAt || ""),
    updatedAt: String(record.updatedAt || record.createdAt || ""),
  };
}

function saveCredentialRecord(origin, username, password) {
  const normalizedUsername = String(username || "").trim().slice(0, 320);
  const normalizedPassword = String(password || "").slice(0, 4096);
  if (!origin || !normalizedUsername || !normalizedPassword) return { ok: false, message: "账号和密码都不能为空" };
  if (!safeStorage.isEncryptionAvailable()) return { ok: false, message: "当前系统的安全加密服务不可用，未保存密码" };
  const records = listCredentialRecords();
  const now = new Date().toISOString();
  const existing = records.find((item) => item.origin === origin && item.username === normalizedUsername);
  const record = {
    id: existing?.id || randomUUID(),
    origin,
    username: normalizedUsername,
    passwordEncrypted: safeStorage.encryptString(normalizedPassword).toString("base64"),
    createdAt: existing?.createdAt || now,
    updatedAt: now,
  };
  const next = [record, ...records.filter((item) => item.id !== record.id)].slice(0, 200);
  writePrivateJsonArray(browserCredentialsFilePath(), next);
  return { ok: true, credential: publicCredential(record) };
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
    <title>NEXUS</title>
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
const browserDocks = new Map();
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
  for (const workspace of browserDocks.values()) {
    for (const dock of workspace.docks.values()) layoutBrowserDock(dock);
  }
}

function activateTab(tabId) {
  for (const tab of tabs) {
    const visible = tab.id === tabId;
    if (tab.view && !tab.view.webContents.isDestroyed()) tab.view.setVisible(visible);
  }
  activeTabId = tabId;
  for (const workspace of browserDocks.values()) {
    for (const dock of workspace.docks.values()) syncBrowserDockVisibility(dock);
  }
  sendTabsChanged();
}

function destroyBrowserDockView(dock) {
  if (!dock) return;
  if (dock.fitTimer) clearTimeout(dock.fitTimer);
  if (dock.lateFitTimer) clearTimeout(dock.lateFitTimer);
  if (dock.view && !dock.view.webContents.isDestroyed()) {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.contentView.removeChildView(dock.view);
    dock.view.webContents.close();
  }
}

function destroyBrowserDock(ownerWebContentsId, rawBrowserTabId = "") {
  const workspace = browserDocks.get(ownerWebContentsId);
  if (!workspace) return;
  const browserTabId = normalizeBrowserTabKey(rawBrowserTabId);
  if (browserTabId) {
    const dock = workspace.docks.get(browserTabId);
    if (!dock) return;
    workspace.docks.delete(browserTabId);
    destroyBrowserDockView(dock);
    if (workspace.activeDockId === browserTabId) workspace.activeDockId = "";
    if (!workspace.docks.size) browserDocks.delete(ownerWebContentsId);
    return;
  }
  browserDocks.delete(ownerWebContentsId);
  for (const dock of workspace.docks.values()) destroyBrowserDockView(dock);
  workspace.docks.clear();
}

function closeTab(tabId) {
  const index = tabs.findIndex((tab) => tab.id === tabId);
  if (index === -1) return;
  const tab = tabs[index];
  if (tab.isHome) return; // 首页标签不可关闭
  destroyBrowserDock(tab.view.webContents.id);
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

function canonicalTabUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    parsed.searchParams.delete("_wb");
    return parsed.href;
  } catch {
    return "";
  }
}

function findTabByUrl(url) {
  const target = canonicalTabUrl(url);
  if (!target) return null;
  return tabs.find((tab) => canonicalTabUrl(tab.url) === target) || null;
}

// 给 URL 追加一次性时间戳，绕过 Chromium 磁盘缓存（桌面壳必须每次拿最新页面）。
function withCacheBuster(url) {
  try {
    const parsed = new URL(url);
    parsed.searchParams.set("_wb", String(Date.now()));
    return parsed.href;
  } catch {
    return url;
  }
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
    tab.url = canonicalTabUrl(navigatedUrl) || navigatedUrl;
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
    if (workbenchUrl && safeTarget.origin === workbenchUrl.origin) return; // 同源留在当前标签
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
    if (workbenchUrl && safeTarget.origin === workbenchUrl.origin) return;
    event.preventDefault();
    openTabInWorkbench(safeTarget.href);
  });
  view.webContents.on("will-attach-webview", (event) => event.preventDefault());
  view.webContents.on("did-fail-load", (_event, errorCode, errorDescription, _validatedURL, isMainFrame) => {
    if (!isMainFrame || errorCode === -3 || state.showingErrorPage) return;
    showErrorPage(view, state, "无法连接 NEXUS", errorDescription || "页面加载失败，请检查服务器和网络后重试。");
  });

  // 桌面壳里禁用 Service Worker：避免 SW 缓存导致页面永远显示旧版本。
  // 桌面壳本身就是网络应用，离线缓存没有意义，反而会造成"改版看不到"。
  const unregisterServiceWorkers = () => {
    void view.webContents.executeJavaScript(
      `(async()=>{try{if("serviceWorker" in navigator){const regs=await navigator.serviceWorker.getRegistrations();for(const r of regs){await r.unregister();}}}catch(_){}})();`,
      true
    ).catch(() => {});
  };
  view.webContents.on("did-start-loading", unregisterServiceWorkers);
  view.webContents.on("did-finish-load", unregisterServiceWorkers);

  void view.webContents.loadURL(withCacheBuster(safeUrl.href)).catch(() => {});
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

function ownerTabForSender(sender) {
  if (!sender || sender.isDestroyed()) return null;
  return tabs.find((tab) => tab.view && tab.view.webContents.id === sender.id) || null;
}

function normalizedDockBounds(rawBounds) {
  const bounds = rawBounds && typeof rawBounds === "object" ? rawBounds : {};
  const number = (value) => Number.isFinite(Number(value)) ? Math.round(Number(value)) : 0;
  return {
    x: Math.max(0, number(bounds.x)),
    y: Math.max(0, number(bounds.y)),
    width: Math.max(0, number(bounds.width)),
    height: Math.max(0, number(bounds.height)),
  };
}

function applyBrowserDockFit(dock) {
  if (!dock || dock.view.webContents.isDestroyed()) return;
  const width = normalizedDockBounds(dock.workspace?.bounds).width;
  const contentWidth = Number(dock.naturalContentWidth || 0);
  const zoomFactor = contentWidth > width + 16
    ? Math.max(0.72, Math.min(1, (width - 4) / contentWidth))
    : 1;
  if (Math.abs(Number(dock.zoomFactor || 1) - zoomFactor) < 0.01) return;
  dock.zoomFactor = zoomFactor;
  dock.view.webContents.setZoomFactor(zoomFactor);
  sendBrowserDockState(dock, { zoomFactor, autoFitted: zoomFactor < 0.995 });
}

async function measureBrowserDockFit(dock) {
  if (!dock || dock.view.webContents.isDestroyed() || dock.view.webContents.isLoading()) return;
  const contents = dock.view.webContents;
  try {
    // Measure at 100% so fixed-width desktop sites can be fitted into the
    // middle pane. Responsive sites keep 100% and remain fully readable.
    if (Math.abs(Number(dock.zoomFactor || 1) - 1) >= 0.01) contents.setZoomFactor(1);
    dock.zoomFactor = 1;
    const metrics = await contents.executeJavaScript(`(() => {
      const root = document.documentElement;
      const body = document.body;
      const viewportWidth = Math.max(1, window.innerWidth || root?.clientWidth || 1);
      const contentWidth = Math.max(viewportWidth, root?.scrollWidth || 0, body?.scrollWidth || 0);
      return { viewportWidth, contentWidth };
    })()`, true);
    const viewportWidth = Math.max(1, Number(metrics?.viewportWidth || 0));
    const contentWidth = Math.max(viewportWidth, Number(metrics?.contentWidth || 0));
    dock.naturalContentWidth = contentWidth > viewportWidth + 16 ? contentWidth : 0;
    applyBrowserDockFit(dock);
    sendBrowserDockState(dock, { zoomFactor: dock.zoomFactor || 1, autoFitted: Number(dock.zoomFactor || 1) < 0.995 });
  } catch {
    dock.naturalContentWidth = 0;
    dock.zoomFactor = 1;
    if (!contents.isDestroyed()) contents.setZoomFactor(1);
  }
}

function scheduleBrowserDockFit(dock, remeasure = false) {
  if (!dock || dock.view.webContents.isDestroyed()) return;
  if (remeasure) dock.fitNeedsMeasure = true;
  if (dock.fitTimer) clearTimeout(dock.fitTimer);
  dock.fitTimer = setTimeout(() => {
    dock.fitTimer = null;
    const shouldMeasure = Boolean(dock.fitNeedsMeasure);
    dock.fitNeedsMeasure = false;
    if (shouldMeasure || !dock.naturalContentWidth) void measureBrowserDockFit(dock);
    else applyBrowserDockFit(dock);
  }, 90);
}

function layoutBrowserDock(dock) {
  if (!dock || !mainWindow || mainWindow.isDestroyed() || dock.view.webContents.isDestroyed()) return;
  const [windowWidth, windowHeight] = mainWindow.getContentSize();
  const relative = normalizedDockBounds(dock.workspace?.bounds);
  const x = Math.min(relative.x, windowWidth);
  const y = Math.min(TAB_BAR_HEIGHT + relative.y, windowHeight);
  const width = Math.max(0, Math.min(relative.width, windowWidth - x));
  const height = Math.max(0, Math.min(relative.height, windowHeight - y));
  const widthChanged = Number(dock.appliedWidth || 0) !== width;
  dock.appliedWidth = width;
  dock.view.setBounds({ x, y, width, height });
  if (widthChanged && width > 0) scheduleBrowserDockFit(dock, !dock.naturalContentWidth);
  syncBrowserDockVisibility(dock);
}

function syncBrowserDockVisibility(dock) {
  if (!dock || dock.view.webContents.isDestroyed()) return;
  const workspace = dock.workspace;
  const bounds = normalizedDockBounds(workspace?.bounds);
  dock.view.setVisible(Boolean(
    workspace?.requestedVisible
    && workspace.activeDockId === dock.browserTabId
    && dock.ownerTabId === activeTabId
    && bounds.width > 0
    && bounds.height > 0
  ));
}

function sendBrowserDockState(dock, extra = {}) {
  const owner = tabs.find((tab) => tab.id === dock.ownerTabId);
  if (!owner || owner.view.webContents.isDestroyed()) return;
  const contents = dock.view.webContents;
  const url = parseBrowserDockUrl(contents.getURL());
  void owner.view.webContents.send("browser-dock-state", {
    browserTabId: dock.browserTabId,
    active: dock.workspace?.activeDockId === dock.browserTabId,
    url: url ? url.href : "",
    title: contents.getTitle() || "",
    loading: contents.isLoading(),
    canGoBack: contents.navigationHistory.canGoBack(),
    canGoForward: contents.navigationHistory.canGoForward(),
    zoomFactor: Number(dock.zoomFactor || 1),
    autoFitted: Number(dock.zoomFactor || 1) < 0.995,
    ...extra,
  });
}

function configureBrowserDockSession(dockSession) {
  if (dockSession.__workbenchBrowserConfigured) return;
  dockSession.__workbenchBrowserConfigured = true;
  dockSession.setPermissionCheckHandler(() => false);
  dockSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  dockSession.on("will-download", (event) => event.preventDefault());
}

function browserDockWorkspaceForEvent(event, create = false) {
  const ownerTab = ownerTabForSender(event.sender);
  if (!ownerTab) return null;
  let workspace = browserDocks.get(event.sender.id);
  if (!workspace && create) {
    workspace = {
      ownerTabId: ownerTab.id,
      ownerWebContentsId: ownerTab.view.webContents.id,
      activeDockId: "",
      bounds: { x: 0, y: 0, width: 0, height: 0 },
      requestedVisible: true,
      docks: new Map(),
    };
    browserDocks.set(workspace.ownerWebContentsId, workspace);
  }
  return workspace || null;
}

function createBrowserDock(workspace, browserTabId) {
  const ownerTab = tabs.find((tab) => tab.id === workspace.ownerTabId);
  if (!ownerTab) return null;
  const view = new WebContentsView({
    webPreferences: {
      partition: BROWSER_DOCK_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      devTools: !app.isPackaged,
    },
  });
  const dock = {
    ownerTabId: ownerTab.id,
    ownerWebContentsId: ownerTab.view.webContents.id,
    browserTabId,
    workspace,
    view,
    naturalContentWidth: 0,
    zoomFactor: 1,
    fitTimer: null,
    lateFitTimer: null,
  };
  workspace.docks.set(browserTabId, dock);
  configureBrowserDockSession(view.webContents.session);
  mainWindow.contentView.addChildView(view);

  const notify = (extra = {}) => sendBrowserDockState(dock, extra);
  view.webContents.on("did-start-loading", () => {
    dock.naturalContentWidth = 0;
    dock.zoomFactor = 1;
    view.webContents.setZoomFactor(1);
    notify({ loading: true, zoomFactor: 1, autoFitted: false });
  });
  view.webContents.on("did-stop-loading", () => {
    notify({ loading: false });
    scheduleBrowserDockFit(dock, true);
    if (dock.lateFitTimer) clearTimeout(dock.lateFitTimer);
    dock.lateFitTimer = setTimeout(() => scheduleBrowserDockFit(dock, true), 700);
  });
  view.webContents.on("page-title-updated", () => notify());
  view.webContents.on("did-navigate", () => notify());
  view.webContents.on("did-navigate-in-page", () => notify());
  view.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    if (isMainFrame && errorCode !== -3) notify({ loading: false, error: errorDescription || "页面加载失败", failedUrl: validatedURL || "" });
  });
  view.webContents.on("will-frame-navigate", (event, targetUrl) => {
    if (!parseBrowserDockUrl(targetUrl)) event.preventDefault();
  });
  view.webContents.on("will-redirect", (event, targetUrl) => {
    if (!parseBrowserDockUrl(targetUrl)) event.preventDefault();
  });
  view.webContents.on("will-attach-webview", (event) => event.preventDefault());
  view.webContents.setWindowOpenHandler(({ url }) => {
    const target = parseBrowserDockUrl(url);
    if (target && !ownerTab.view.webContents.isDestroyed()) {
      void ownerTab.view.webContents.send("browser-dock-open-tab-request", { browserTabId, url: target.href });
    }
    return { action: "deny" };
  });
  layoutBrowserDock(dock);
  return dock;
}

function browserDockForEvent(event, rawBrowserTabId, create = false) {
  const browserTabId = normalizeBrowserTabKey(rawBrowserTabId);
  if (!browserTabId) return null;
  const workspace = browserDockWorkspaceForEvent(event, create);
  if (!workspace) return null;
  return workspace.docks.get(browserTabId) || (create ? createBrowserDock(workspace, browserTabId) : null);
}

function activateBrowserDock(workspace, browserTabId) {
  if (!workspace || !workspace.docks.has(browserTabId)) return false;
  workspace.activeDockId = browserTabId;
  workspace.requestedVisible = true;
  for (const dock of workspace.docks.values()) syncBrowserDockVisibility(dock);
  return true;
}

const BROWSER_DOCK_SNAPSHOT_SCRIPT = `(() => {
  const clean = (value, limit = 500) => String(value || "").replace(/\\s+/g, " ").trim().slice(0, limit);
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity || 1) > 0 && rect.width > 1 && rect.height > 1;
  };
  document.querySelectorAll("[data-workbench-ai-id]").forEach((element) => element.removeAttribute("data-workbench-ai-id"));
  const elements = [];
  const candidates = document.querySelectorAll('a[href],button,input:not([type="hidden"]),textarea,select,[role="button"],[contenteditable="true"]');
  for (const element of candidates) {
    if (elements.length >= 120 || !visible(element)) continue;
    const tag = element.tagName.toLowerCase();
    const inputType = tag === "input" ? String(element.type || "text").toLowerCase() : "";
    const id = "wb-" + (elements.length + 1);
    element.setAttribute("data-workbench-ai-id", id);
    const label = clean(element.getAttribute("aria-label") || element.innerText || element.placeholder || element.title || element.name || element.alt || "", 180);
    const item = { id, tag, role: clean(element.getAttribute("role") || "", 40), inputType, label, disabled: Boolean(element.disabled) };
    if (tag === "a") item.href = clean(element.href, 500);
    if (tag === "select") item.options = Array.from(element.options).slice(0, 30).map((option) => ({ value: clean(option.value, 120), label: clean(option.textContent, 120), selected: option.selected }));
    elements.push(item);
  }
  return {
    title: clean(document.title, 300),
    url: location.href,
    text: clean(document.body ? document.body.innerText : "", 12000),
    selection: clean(getSelection ? getSelection().toString() : "", 3000),
    elements,
  };
})()`;

const BROWSER_CREDENTIAL_CAPTURE_SCRIPT = `(() => {
  const passwordInput = Array.from(document.querySelectorAll('input[type="password"]')).find((input) => !input.disabled && String(input.value || "")) || null;
  if (!passwordInput) return { ok: false, message: "请先在网页的登录框里输入账号和密码" };
  const scope = passwordInput.form || passwordInput.closest('form') || document;
  const candidates = Array.from(scope.querySelectorAll('input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"])'));
  const usernameInput = candidates.find((input) => /username|email/i.test(String(input.autocomplete || "") + " " + String(input.name || "") + " " + String(input.type || "")))
    || candidates.filter((input) => input.compareDocumentPosition(passwordInput) & Node.DOCUMENT_POSITION_FOLLOWING).at(-1)
    || candidates.find((input) => String(input.value || ""));
  const username = String(usernameInput?.value || "").trim();
  const password = String(passwordInput.value || "");
  if (!username || !password) return { ok: false, message: "没有识别到完整的账号和密码" };
  return { ok: true, username: username.slice(0, 320), password: password.slice(0, 4096) };
})()`;

function browserCredentialFillScript(username, password) {
  return `(() => {
    const username = ${JSON.stringify(String(username || ""))};
    const password = ${JSON.stringify(String(password || ""))};
    const passwordInput = Array.from(document.querySelectorAll('input[type="password"]')).find((input) => !input.disabled) || null;
    if (!passwordInput) return { ok: false, message: "当前页面没有找到密码输入框" };
    const scope = passwordInput.form || passwordInput.closest('form') || document;
    const candidates = Array.from(scope.querySelectorAll('input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"])'));
    const usernameInput = candidates.find((input) => /username|email/i.test(String(input.autocomplete || "") + " " + String(input.name || "") + " " + String(input.type || "")))
      || candidates.filter((input) => input.compareDocumentPosition(passwordInput) & Node.DOCUMENT_POSITION_FOLLOWING).at(-1)
      || candidates[0]
      || null;
    const setValue = (input, value) => {
      if (!input) return;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      if (setter) setter.call(input, value); else input.value = value;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    };
    setValue(usernameInput, username);
    setValue(passwordInput, password);
    passwordInput.focus();
    return { ok: true, message: usernameInput ? "账号和密码已填入，请你确认后登录" : "密码已填入，请你补充账号并确认登录" };
  })()`;
}

function browserDockActionScript(action) {
  return `(() => {
    const action = ${JSON.stringify(action)};
    const id = String(action.element_id || "");
    const element = id && /^wb-\\d+$/.test(id) ? document.querySelector('[data-workbench-ai-id="' + id + '"]') : null;
    const result = (ok, message, extra = {}) => ({ ok, message, ...extra });
    if (action.type === "scroll") {
      const edge = ["top", "bottom"].includes(String(action.edge || "")) ? String(action.edge) : "";
      const amount = Math.max(-1600, Math.min(1600, Number(action.amount || (edge === "top" ? -620 : 620))));
      const canScroll = (node) => {
        if (!node || node === document.body || node === document.documentElement) return false;
        const style = getComputedStyle(node);
        return /(auto|scroll|overlay)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 4;
      };
      const nearestScrollable = (node) => {
        for (let current = node; current && current !== document.body; current = current.parentElement) {
          if (canScroll(current)) return current;
        }
        return null;
      };
      const focused = nearestScrollable(document.activeElement);
      const center = nearestScrollable(document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2));
      const visibleScrollers = Array.from(document.querySelectorAll("body *"))
        .filter(canScroll)
        .map((node) => ({ node, rect: node.getBoundingClientRect() }))
        .filter((item) => item.rect.width > 8 && item.rect.height > 8 && item.rect.bottom > 0 && item.rect.top < window.innerHeight)
        .sort((left, right) => (right.rect.width * right.rect.height) - (left.rect.width * left.rect.height));
      const target = edge
        ? (document.scrollingElement || document.documentElement)
        : (focused || center || visibleScrollers[0]?.node || document.scrollingElement || document.documentElement);
      const before = Number(target.scrollTop || 0);
      const maximum = Math.max(0, Number(target.scrollHeight || 0) - Number(target.clientHeight || 0));
      const next = edge === "top" ? 0 : edge === "bottom" ? maximum : Math.max(0, Math.min(maximum, before + amount));
      target.scrollTo({ top: next, behavior: "auto" });
      const moved = Math.abs(next - before);
      if (moved < 1) return result(false, edge === "top" || amount < 0 ? "已经到页面顶部" : "已经到页面底部");
      if (edge === "top") return result(true, "已回到页面顶部");
      if (edge === "bottom") return result(true, "已滚动到页面底部");
      return result(true, amount >= 0 ? "已向下滚动 " + Math.round(moved) + " 像素" : "已向上滚动 " + Math.round(moved) + " 像素");
    }
    if (!element) return result(false, "页面已变化，找不到目标控件，请重新读取页面");
    if (element.disabled) return result(false, "目标控件当前不可用");
    element.scrollIntoView({ block: "center", inline: "center" });
    if (action.type === "click") {
      const label = String(element.getAttribute("aria-label") || element.innerText || element.value || element.title || "").trim();
      const sensitive = /购买|付款|支付|下单|提交订单|确认订单|删除|移除|注销|退出账号|发送|发布|登录|注册|授权|同意协议|buy|pay|purchase|delete|remove|send|publish|login|sign in|sign up|authorize/i.test(label);
      if (sensitive && action.confirmed !== true) return result(false, "这个操作可能提交、发送或修改外部数据，需要你确认", { requiresConfirmation: true, confirmationLabel: label || "执行这个操作" });
      element.click();
      return result(true, label ? "已点击“" + label.slice(0, 80) + "”" : "已点击目标控件");
    }
    if (action.type === "fill") {
      const tag = element.tagName.toLowerCase();
      const inputType = tag === "input" ? String(element.type || "text").toLowerCase() : "";
      if (!["input", "textarea"].includes(tag) && !element.isContentEditable) return result(false, "目标不是可填写控件");
      if (["password", "file", "hidden"].includes(inputType)) return result(false, "为保护隐私，AI 不能填写密码或文件控件");
      const value = String(action.value || "").slice(0, 4000);
      if (element.isContentEditable) element.textContent = value;
      else {
        const prototype = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
        if (setter) setter.call(element, value); else element.value = value;
      }
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
      element.focus();
      return result(true, "已填写目标输入框");
    }
    if (action.type === "select") {
      if (element.tagName.toLowerCase() !== "select") return result(false, "目标不是下拉选择框");
      const value = String(action.value || "");
      const option = Array.from(element.options).find((item) => item.value === value || item.textContent.trim() === value);
      if (!option) return result(false, "下拉框里没有这个选项");
      element.value = option.value;
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
      return result(true, "已选择“" + option.textContent.trim().slice(0, 80) + "”");
    }
    return result(false, "不支持的页面操作");
  })()`;
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
  ipcMain.handle("browser-dock-open", async (event, rawBrowserTabId, rawUrl, rawBounds) => {
    const browserTabId = normalizeBrowserTabKey(rawBrowserTabId);
    const target = parseBrowserDockUrl(String(rawUrl || ""));
    const dock = target && browserTabId ? browserDockForEvent(event, browserTabId, true) : null;
    if (!target || !dock) return { ok: false, message: "只支持安全的 http/https 网页地址" };
    dock.workspace.bounds = normalizedDockBounds(rawBounds);
    activateBrowserDock(dock.workspace, browserTabId);
    for (const item of dock.workspace.docks.values()) layoutBrowserDock(item);
    if (dock.view.webContents.getURL() !== target.href) {
      await dock.view.webContents.loadURL(target.href).catch((error) => {
        sendBrowserDockState(dock, { loading: false, error: error.message || "页面加载失败" });
      });
    }
    return { ok: true, browserTabId };
  });
  ipcMain.handle("browser-dock-activate", (event, rawBrowserTabId, rawBounds) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    if (!dock) return false;
    if (rawBounds) dock.workspace.bounds = normalizedDockBounds(rawBounds);
    activateBrowserDock(dock.workspace, dock.browserTabId);
    for (const item of dock.workspace.docks.values()) layoutBrowserDock(item);
    sendBrowserDockState(dock);
    return true;
  });
  ipcMain.handle("browser-dock-bounds", (event, rawBrowserTabId, rawBounds) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    if (!dock) return false;
    dock.workspace.bounds = normalizedDockBounds(rawBounds);
    for (const item of dock.workspace.docks.values()) layoutBrowserDock(item);
    return true;
  });
  ipcMain.handle("browser-dock-visible", (event, rawBrowserTabId, visible) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    if (!dock) return false;
    dock.workspace.requestedVisible = Boolean(visible);
    if (visible) dock.workspace.activeDockId = dock.browserTabId;
    for (const item of dock.workspace.docks.values()) syncBrowserDockVisibility(item);
    return true;
  });
  ipcMain.handle("browser-dock-navigate", async (event, rawBrowserTabId, command, rawUrl) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    if (!dock) return false;
    const history = dock.view.webContents.navigationHistory;
    if (command === "back" && history.canGoBack()) history.goBack();
    else if (command === "forward" && history.canGoForward()) history.goForward();
    else if (command === "reload") dock.view.webContents.reload();
    else if (command === "navigate") {
      const target = parseBrowserDockUrl(String(rawUrl || ""));
      if (!target) return false;
      await dock.view.webContents.loadURL(target.href).catch(() => {});
    } else return false;
    return true;
  });
  ipcMain.handle("browser-dock-snapshot", async (event, rawBrowserTabId) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    if (!dock || dock.view.webContents.isLoading()) return { ok: false, message: "网页仍在加载" };
    try {
      const snapshot = await dock.view.webContents.executeJavaScript(BROWSER_DOCK_SNAPSHOT_SCRIPT, true);
      return { ok: true, snapshot };
    } catch (error) {
      return { ok: false, message: error.message || "无法读取当前网页" };
    }
  });
  ipcMain.handle("browser-dock-perform", async (event, rawBrowserTabId, rawAction) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    if (!dock) return { ok: false, message: "真实网页尚未打开" };
    const action = rawAction && typeof rawAction === "object" ? { ...rawAction } : {};
    const allowed = new Set(["click", "fill", "select", "scroll", "back", "forward", "reload", "navigate"]);
    if (!allowed.has(action.type)) return { ok: false, message: "不支持的网页操作" };
    if (["back", "forward", "reload", "navigate"].includes(action.type)) {
      const history = dock.view.webContents.navigationHistory;
      if (action.type === "back" && history.canGoBack()) history.goBack();
      else if (action.type === "forward" && history.canGoForward()) history.goForward();
      else if (action.type === "reload") dock.view.webContents.reload();
      else if (action.type === "navigate") {
        const target = parseBrowserDockUrl(String(action.url || action.value || ""));
        if (!target) return { ok: false, message: "网址不安全或格式不正确" };
        await dock.view.webContents.loadURL(target.href).catch(() => {});
      } else return { ok: false, message: "当前没有可用的前进/后退页面" };
      return { ok: true, message: "已执行浏览器导航" };
    }
    try {
      return await dock.view.webContents.executeJavaScript(browserDockActionScript(action), true);
    } catch (error) {
      return { ok: false, message: error.message || "页面操作失败" };
    }
  });
  ipcMain.handle("browser-bookmarks-list", () => ({ ok: true, bookmarks: listBrowserBookmarks() }));
  ipcMain.handle("browser-bookmarks-save", (_event, rawBookmark) => {
    try { return saveBrowserBookmark(rawBookmark); }
    catch (error) { return { ok: false, message: error.message || "书签保存失败" }; }
  });
  ipcMain.handle("browser-bookmarks-remove", (_event, rawId) => {
    try {
      const id = String(rawId || "");
      const bookmarks = listBrowserBookmarks();
      const next = bookmarks.filter((item) => item.id !== id);
      if (next.length === bookmarks.length) return { ok: false, message: "没有找到这个书签" };
      writePrivateJsonArray(browserBookmarksFilePath(), next);
      return { ok: true };
    } catch (error) {
      return { ok: false, message: error.message || "书签删除失败" };
    }
  });
  ipcMain.handle("browser-credentials-list", (event, rawBrowserTabId) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    const origin = credentialOriginFromUrl(dock?.view.webContents.getURL());
    if (!origin) return { ok: false, message: "请先打开要登录的网站", credentials: [] };
    const credentials = listCredentialRecords().filter((item) => item.origin === origin).map(publicCredential);
    return { ok: true, origin, credentials, encryptionAvailable: safeStorage.isEncryptionAvailable() };
  });
  ipcMain.handle("browser-credentials-save", (event, rawBrowserTabId, rawCredential) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    const origin = credentialOriginFromUrl(dock?.view.webContents.getURL());
    const username = String(rawCredential?.username || "").trim().slice(0, 320);
    const password = String(rawCredential?.password || "").slice(0, 4096);
    if (!origin) return { ok: false, message: "请先打开要登录的网站" };
    try { return saveCredentialRecord(origin, username, password); }
    catch (error) { return { ok: false, message: error.message || "密码保存失败" }; }
  });
  ipcMain.handle("browser-credentials-capture", async (event, rawBrowserTabId) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    const originBefore = credentialOriginFromUrl(dock?.view.webContents.getURL());
    if (!dock || !originBefore) return { ok: false, message: "请先打开要登录的网站" };
    try {
      const captured = await dock.view.webContents.executeJavaScript(BROWSER_CREDENTIAL_CAPTURE_SCRIPT, true);
      const originAfter = credentialOriginFromUrl(dock.view.webContents.getURL());
      if (originBefore !== originAfter) return { ok: false, message: "网页地址已变化，未保存密码" };
      if (!captured?.ok) return { ok: false, message: captured?.message || "没有识别到账号密码" };
      return saveCredentialRecord(originBefore, String(captured.username || "").trim(), String(captured.password || ""));
    } catch (error) {
      return { ok: false, message: error.message || "无法读取当前登录框" };
    }
  });
  ipcMain.handle("browser-credentials-fill", async (event, rawBrowserTabId, rawCredentialId) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    const origin = credentialOriginFromUrl(dock?.view.webContents.getURL());
    const record = listCredentialRecords().find((item) => item.id === String(rawCredentialId || "") && item.origin === origin);
    if (!dock || !origin || !record) return { ok: false, message: "这个账号不属于当前网站" };
    if (!safeStorage.isEncryptionAvailable()) return { ok: false, message: "当前系统的安全解密服务不可用" };
    try {
      const password = safeStorage.decryptString(Buffer.from(record.passwordEncrypted, "base64"));
      const result = await dock.view.webContents.executeJavaScript(browserCredentialFillScript(record.username, password), true);
      if (credentialOriginFromUrl(dock.view.webContents.getURL()) !== origin) return { ok: false, message: "网页地址已变化，已停止填充" };
      return result?.ok ? result : { ok: false, message: result?.message || "没有找到登录框" };
    } catch (error) {
      return { ok: false, message: error.message || "账号密码填充失败" };
    }
  });
  ipcMain.handle("browser-credentials-remove", (event, rawBrowserTabId, rawCredentialId) => {
    const dock = browserDockForEvent(event, rawBrowserTabId);
    const origin = credentialOriginFromUrl(dock?.view.webContents.getURL());
    const id = String(rawCredentialId || "");
    if (!origin) return { ok: false, message: "请先打开对应网站" };
    try {
      const records = listCredentialRecords();
      const next = records.filter((item) => !(item.id === id && item.origin === origin));
      if (next.length === records.length) return { ok: false, message: "没有找到这个账号" };
      writePrivateJsonArray(browserCredentialsFilePath(), next);
      return { ok: true };
    } catch (error) {
      return { ok: false, message: error.message || "账号删除失败" };
    }
  });
  ipcMain.on("browser-dock-close", (event, rawBrowserTabId) => destroyBrowserDock(event.sender.id, rawBrowserTabId));
  ipcMain.on("browser-dock-destroy", (event) => destroyBrowserDock(event.sender.id));
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
    title: "NEXUS",
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
    for (const ownerId of [...browserDocks.keys()]) destroyBrowserDock(ownerId);
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

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);
    registerIpcHandlers();
    registerAuthIpc();
    // 清空并禁用 HTTP/SW 缓存：桌面壳是联网应用，任何磁盘缓存都会让用户看不到新版本。
    try {
      const { session } = require("electron");
      const defaultSession = session.defaultSession;
      // 注意：不再 clearCache()——静态资源 URL 带 ?v=版本号，磁盘缓存不会挡更新，
      // 反而能大幅加速页面加载。真正会挡更新的是 Service Worker，这里只清它。
      await defaultSession.clearStorageData({ storages: ["serviceworkers", "cachestorage"] });
    } catch (_) {
      // 清缓存失败不阻塞启动
    }
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
