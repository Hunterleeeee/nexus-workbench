import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopDir = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(desktopDir, "..");
const read = (file) => fs.readFileSync(path.join(rootDir, file), "utf8");
const readDesktop = (file) => fs.readFileSync(path.join(desktopDir, file), "utf8");
const fail = (message) => {
  throw new Error(`[desktop verify] ${message}`);
};

const version = read("VERSION").trim();
const packageJson = JSON.parse(readDesktop("package.json"));
if (!/^\d+\.\d+\.\d+$/.test(version)) fail(`VERSION 格式无效：${version}`);
if (packageJson.version !== version) fail(`桌面壳版本 ${packageJson.version} 与 Workbench ${version} 不一致`);

const main = readDesktop("main.cjs");
for (const required of [
  "https://workbench.example.dev:8765/",
  "contextIsolation: true",
  "nodeIntegration: false",
  "sandbox: true",
  "webSecurity: true",
  "allowRunningInsecureContent: false",
  "app.requestSingleInstanceLock()",
]) {
  if (!main.includes(required)) fail(`main.cjs 缺少安全/运行约束：${required}`);
}
for (const forbidden of ["ignore-certificate-errors", "nodeIntegration: true", "javascript:", "file://"]) {
  if (main.includes(forbidden)) fail(`main.cjs 出现不允许的内容：${forbidden}`);
}
const preload = readDesktop("preload.cjs");
if (!preload.includes("contextBridge") || !preload.includes('exposeInMainWorld("desktopShell"')) {
  fail("preload.cjs 应通过 desktopShell 暴露受控能力");
}
if (!preload.includes('exposeInMainWorld("auth"')) {
  fail("preload.cjs 应通过 auth 暴露登录提交桥");
}
for (const forbidden of ["require(\"fs\")", "require(\"node:fs\")", "process.env", "ipcRenderer.sendSync"]) {
  if (preload.includes(forbidden)) fail(`preload.cjs 出现不允许的内容：${forbidden}`);
}
if (!preload.includes("ipcRenderer.on(\"tabs-changed\"")) fail("preload.cjs 缺少标签变化订阅");
if (!main.includes("WebContentsView")) fail("main.cjs 缺少 WebContentsView 标签系统");
if (!main.includes("open-tab") || !main.includes("tab-switch") || !main.includes("tab-close") || !main.includes("tab-new")) {
  fail("main.cjs 缺少标签 IPC（open-tab/tab-switch/tab-close/tab-new）");
}
if (!readDesktop("shell.html").includes('id="tabbar"')) fail("shell.html 缺少标签栏");
if (!main.includes("open-web-window")) fail("main.cjs 缺少 open-web-window 能力注册");
if (!main.includes("parseSafeWebUrl(url)") && !main.includes("parseSafeWebUrl(")) fail("main.cjs 打开外链前必须校验 URL");
if (!main.includes('webContents.on("login"')) fail("main.cjs 缺少 Basic Auth 登录处理");

const manifest = JSON.parse(read("static/manifest.webmanifest"));
if (manifest.start_url !== "/" || manifest.scope !== "/" || manifest.display !== "standalone") {
  fail("PWA Manifest 的 start_url、scope 或 display 不符合线上入口约束");
}
if (!Array.isArray(manifest.icons) || manifest.icons.length < 2) fail("PWA Manifest 缺少完整图标声明");

const serviceWorker = read("static/sw.js");
if (!serviceWorker.includes(`workbench-shell-v${version}`)) fail("Service Worker 缓存版本未与 VERSION 对齐");
for (const required of ["self.skipWaiting()", "self.clients.claim()", "if (url.pathname.startsWith(\"/api/\")) return"]) {
  if (!serviceWorker.includes(required)) fail(`Service Worker 缺少更新或 API 边界：${required}`);
}

console.log(`[desktop verify] OK · Workbench ${version} · Electron 安全边界、PWA Manifest、Service Worker 已对齐`);
