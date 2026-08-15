<div align="center">

# NEXUS

**个人 AI 工作台 · FastAPI + 多项目入口 + Agent 调度 + Electron 桌面壳**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](requirements.txt)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white)](requirements.txt)
[![Tests](https://img.shields.io/badge/Tests-494%20passed-brightgreen)](#测试)

把「收件箱、知识库、文档工厂、研究、云开发、产品管理」等日常任务，聚合成一个本地优先、可部署、可插拔的工作台。

</div>

---

## 特性

- 🧩 **项目可插拔**：`projects.json` 控制启用/禁用，首页入口、子 Agent 工具、调度路由统一过滤
- 🤖 **真 Agent 调度**：首页「总调度 Agent」是父 Agent，各项目声明子 Agent；工具执行 + 证据回放
- 📥 **快速收件箱**：7 类自动分类、合并建议、批量整理
- 📚 **本地知识库**：Markdown + 关键词/语义向量混合检索
- 📄 **文档工厂**：PDF/Word/Excel/PPTX/HTML/Markdown → 结构化产物，DOCX/PDF 交付审批
- 🔬 **研究工具**：AI 网页研究、AI 热点追踪、想法结构化验证
- 📈 **量化选股**：可解释因子、观察任务、日报/周报、回测（不自动下单）
- 🖥️ **服务器监控**：SSH/本机只读探测、阈值、历史快照、健康评分
- 🎓 **学习教练**：AI 转型学习（每日知识、练习自测、定时推送）
- ⚡ **自动化中心**：规则目录、多步骤计划、重试与人工接管
- 🖥️ **Electron 桌面壳**：独立窗口、标签页、Basic Auth 自动登录（`desktop/`）

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 可复现安装（钉版）：pip install -r requirements.lock
```

可选功能按需安装（网页抓取、Web Push 推送等，缺失自动降级）：

```bash
pip install -r requirements-optional.txt
```

### 2. 初始化项目配置

```bash
cp projects.open-source.json projects.json
```

`projects.json` 控制工作台展示哪些项目；**缺失时会自动回退到开源模板**，所以这步可跳过，复制后可按需编辑 `enabled` 字段。

### 3. 配置 LLM（OpenAI 兼容接口）

复制 `.env.example` 为 `.env` 并填写：

```env
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

### 4. 启动

```bash
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765> 即可。

### 5. 桌面壳（可选）

```bash
cd desktop
npm install
npm start          # 启动桌面壳
npm run verify     # 静态门禁（版本/安全边界/PWA 对齐）
npm run package    # 打包 macOS 安装包（arm64）
```

> 生产部署模板（Nginx + systemd + 备份）暂未包含在本仓库中，可按需自行配置。

## 项目插拔

每个条目支持 `enabled` 字段（缺省启用）：

```json
{ "id": "crawl4ai", "enabled": false, "note": "不引导爬虫入口" }
```

禁用后：首页入口、子 Agent 工具、显式/自动调度路由统一过滤（页面与业务 API 返回 404）。开源默认模板见 `projects.open-source.json`。

## 架构概览

```
┌─────────────────────────────────────────────┐
│                Electron 桌面壳               │
│         （可选：独立窗口 / 标签 / 自动登录）    │
└──────────────────┬──────────────────────────┘
                   │ HTTPS
┌──────────────────▼──────────────────────────┐
│              FastAPI (app.py)               │
│  ┌──────────────────────────────────────┐   │
│  │       总调度 Agent（父 Agent）        │   │
│  │   inbox│knowledge│doc-factory│market  │   │
│  │   server│ai-learning│cloud-dev│...    │   │
│  └──────────────────────────────────────┘   │
│  app_pkg/ 35 个领域模块 · SQLite · 本地优先   │
└──────────────────┬──────────────────────────┘
                   │
     ┌─────────────┼─────────────┐
     │             │             │
 收件箱/知识库  产物/输出     外部集成
 knowledge-base  outputs/    （飞书/WebPush/SSH 只读）
```

- 总调度与子 Agent 全部走 ReAct function calling
- 跨项目交接通过 `/api/handoffs` 记录（`data/workbench.db` 的 `work_items`）
- 多 Provider LLM failover（主配置优先，失败按保存顺序切换）

## 目录职责

### 后端入口与进程

| 文件 | 职责 |
|---|---|
| `app.py` | FastAPI 主应用（uvicorn 入口，已按领域拆分为 `app_pkg/`） |
| `agent_worker.py` | Agent 调度后台 Worker（独立进程） |
| `crawl_worker.py` | 网页抓取后台 Worker |
| `monitor_worker.py` | 服务器监控后台 Worker |
| `sync_worker.py` | 数据同步后台 Worker |
| `browser_render_worker.py` | 浏览器截图渲染 Worker（按需启动） |
| `browser_session_worker.py` | 浏览器会话 Worker（按需启动） |
| `feishu.py` | 飞书机器人适配层（被主应用 import） |
| `cloud_dev.py` / `cloud_patch.py` | 云开发受控执行与代码补丁模块 |
| `backup.py` | 数据库备份/恢复 CLI |

### 模块与资源

| 路径 | 职责 |
|---|---|
| `app_pkg/` | 35 个领域模块（inbox/knowledge/agent_engine/market/server/…） |
| `static/` | 工作台与各项目页面资源 |
| `desktop/` | Electron 桌面壳 |
| `tests/` | 测试套件（pytest） |
| `scripts/` | 工具脚本（VAPID 密钥生成等） |

### 配置与数据（不提交版本库）

| 路径 | 职责 |
|---|---|
| `projects.json` / `projects.open-source.json` | 项目插拔配置（缺失回退开源模板） |
| `data/` | SQLite、LLM 配置、快照（仅 `.gitkeep` 入库） |
| `knowledge-base/` | 运行时 Markdown 知识库（仅 `README.md` 入库） |
| `outputs/` | 交付产物 |
| `.env` / `.env.example` | 环境变量（模板入库，真实配置不入库） |

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
# 494 passed, 3 skipped, 58 subtests
```

前端与桌面壳门禁：

```bash
cd desktop
npm ci
npm run verify
npm run test:frontend
```

## 安全边界

- `.env`、`data/*.db`、`data/llm_settings.json` 均不提交版本库（见 `.gitignore`）
- 云开发只接受白名单工作区 + 固定配方命令，使用 `shell=False`，不支持任意 shell
- 浏览器项目只能访问公开 URL；工作台自身（`WORKBENCH_PUBLIC_URL`）永远不能成为目标
- 服务器监控为只读探测；日志读取和重启必须服务器侧人工确认
- 代码中的 `workbench.example.dev` 均为示例占位地址，部署时必须通过 `WORKBENCH_PUBLIC_URL`（后端）或 `WORKBENCH_URL`（桌面壳）覆盖为实际地址

## 命名说明

产品名称为 **NEXUS**。部分环境变量、HTTP Header 和前端全局对象（如 `WORKBENCH_*`、`X-Workbench-*`、`window.WorkbenchUX`）仍保留 `WORKBENCH` 前缀，用于兼容已有部署配置和客户端集成；新配置请继续沿用这些约定，不要自行改名，否则会破坏兼容性。

## License

[MIT](LICENSE)
