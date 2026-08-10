# Workbench Desktop

这是一个薄 Electron 壳，不内置 Python、模型或 API Key，也不向远程页面暴露 Node、IPC 或文件系统能力。

当前壳版本与 Workbench 对齐为 `0.3.140`。

## 开发

```bash
cd /srv/workbench/desktop
npm install
npm start
```

验证壳代码或生成 macOS 安装包：

```bash
npm run verify        # 静态门禁（版本/安全边界/PWA 对齐）
npm run package       # 打包 Apple Silicon (arm64) 安装包
npm run package:intel # 打包 Intel (x64) 安装包
```

## 安装（普通用户）

打包完成后，安装包在 `desktop/dist/Workbench-<版本>-arm64.dmg`。安装方式：

1. 双击打开 `.dmg` 文件
2. 把弹出的「Workbench」图标拖进「应用程序」文件夹
3. 第一次打开时，如果 macOS 提示「无法验证开发者」，右键点应用 → 打开 → 再点「打开」即可（未签名包需要这一步）

> 也可以直接双击 `desktop/dist/mac-arm64/Workbench.app` 免安装运行（无需拖动）。

本机没有签名证书时，可生成用于结构和启动验收的未签名包：

```bash
CSC_IDENTITY_AUTO_DISCOVERY=false npm run package
```

未签名包不等于可分发安装包；正式发布仍需签名、安装验证和自动更新渠道。

`npm run verify` 不启动应用，也不需要读取线上凭据；它会检查桌面壳版本是否与根目录 `VERSION` 一致、远程地址和 Electron 隔离配置、PWA Manifest 以及 Service Worker 缓存版本。它是发布前静态门禁，不等于已经生成安装包或完成线上发布。

默认打开线上 Workbench：`https://workbench.example.dev:8765/`。如需切换到其他部署地址，必须使用 HTTPS：

```bash
WORKBENCH_URL=https://workbench.example.com npm start
```

壳不会忽略证书错误，也不设置 `ignore-certificate-errors`。远程页面只能在原始 Workbench 同源地址内导航；安全的 HTTPS 外链交给系统浏览器，任意 `file://`、`javascript:`、`data:` 等其他协议会被拒绝（错误页使用的内部 `data:` 页面除外）。窗口启用单实例、隔离上下文、Node 禁用、沙箱和 Web 安全策略；preload 保持空实现。

如果服务器暂时不可用，窗口会显示错误页并提供“重试”。macOS 关闭最后一个窗口后保留应用运行，再次激活或启动会聚焦已有窗口。

壳会在远程页面加载完成后读取同源 `/api/meta`（沿用浏览器登录态）。如果线上版本与壳版本不同，会先提示手动刷新；这不是自动发布或自动下载更新，正式发布仍需要重新构建并分发安装包。

### Basic Auth 登录

线上 Workbench 有 HTTP Basic Auth。桌面壳首次打开遇到 401 时会弹出登录窗口，输入账号密码后自动保存到 `userData/auth.json`（仅本机 0600 可读），之后每次启动自动登录。也可以先用环境变量预置（优先级高于保存的凭据）：

```bash
WORKBENCH_AUTH_USER=你的账号 WORKBENCH_AUTH_PASS=你的密码 npm start
```

本机 Gemini 按需开关由 Workbench 网页研究页调用独立的 `companion/workbench_companion.py`；桌面壳本身不执行任意本机命令，也不向远程页面暴露 Node、IPC 或凭据。使用该开关前需在本机启动 Companion，来财 Gemini bridge 只在用户确认后启动或停止。

生产发布时只更新壳和代码版本，工作台的数据、配置和产物仍由后端的 shared 数据目录保存。
