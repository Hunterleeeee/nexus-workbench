# Workbench

个人 AI 工作台：FastAPI 后端 + 多项目入口 + Agent 调度 + Electron 桌面壳。把「收件箱、知识库、文档工厂、研究、云开发、产品管理」等日常任务聚合成一个可部署的工作台，项目可插拔（`projects.json` 控制启用/禁用）。

> 开源版默认关闭爬虫入口（`projects.open-source.json` 里 crawl4ai 为 `enabled: false`）。部署时把该文件复制为 `projects.json` 即可获得开源默认配置，也可按需编辑 `enabled` 字段。

## 功能一览

- `/`：项目入口首页（「现在要处理」待办、`⌘K` 命令面板、推送订阅）
- `/projects/inbox`：快速收件箱（7 类分类、合并建议、批量整理）
- `/projects/knowledge`：本地 Markdown 知识库（关键词 + 语义向量混合检索、Obsidian 只读索引）
- `/projects/doc-factory`：PDF/Word/Excel/PPTX/HTML/Markdown → 结构化产物（可选 MarkItDown 增强、DOCX/PDF 交付审批）
- `/projects/web-research`：轻量 AI 网页研究工作区（多上下文、来源证据、追问、Artifact/WorkItem 交接）
- `/projects/cloud-dev`：受控云开发入口（白名单工作区、固定状态/测试配方、构建审批；不接受任意 shell）
- `/projects/market`：量化选股（可解释因子、观察任务、日报/周报、回测；不自动下单）
- `/projects/server`：服务器只读监控（SSH/本机只读探测、阈值、历史快照、健康评分）
- `/projects/aihot`：AI 热点研究（多数据源、洞察、Web Push 摘要推送）
- `/projects/ai-learning`：AI 转型学习教练（每日知识、练习自测、定时 Push）
- `/projects/idea-analysis`：想法分析（结构化验证、证据/指标回填、继续/暂停/转向）
- `/projects/product-manager`：产品作战室（反馈证据、需求池、RICE 优先级、决策记录、PRD 生成）
- `/automation`：自动化中心（规则目录、多步骤计划、重试与人工接管）

首页「总调度 Agent」是父 Agent，各项目入口声明为子 Agent；调度结果写入 `data/workbench.db` 的 `work_items`，跨项目交接通过 `/api/handoffs` 记录。

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

可选增强（文档解析、浏览器渲染等）见 `requirements-optional.txt`。

### 2. 配置

复制 `.env.example` 为 `.env` 并填写：

```env
# 必填：LLM（OpenAI 兼容接口）
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini

# 可选：飞书机器人（不配则飞书相关功能禁用）
# WORKBENCH_FEISHU_APP_ID=cli_xxx
# WORKBENCH_FEISHU_APP_SECRET=your_app_secret
# WORKBENCH_FEISHU_VERIFY_TOKEN=your_verify_token

# 可选：服务器监控目标（不配则服务器监控页为空）
# WORKBENCH_SERVER=root@your-server.example.com
# WORKBENCH_SERVER_SSH_KEY=~/.ssh/your_key
```

所有配置项见 `.env.example`（含注释说明）。

### 3. 启动

```bash
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765` 即可。完整生产部署（Nginx + systemd + 备份）可参考 `deploy/` 目录中的脚本与配置模板。

## 项目插拔

`projects.json` 控制工作台展示哪些项目。每个条目支持 `enabled` 字段（缺省启用）：

```json
{ "id": "crawl4ai", "enabled": false, "note": "不引导爬虫入口" }
```

禁用后：首页入口、子 Agent 工具、显式/自动调度路由都会统一过滤（页面与业务 API 返回 404）。`projects.open-source.json` 是开源默认模板。

## 目录职责

- `app.py`：FastAPI 主应用（已按领域拆分为 `app_pkg/` 模块）。
- `app_pkg/`：35 个领域模块（projects / inbox / knowledge / agent_engine / market / server / feishu …）。
- `static/`：工作台与各项目页面资源。
- `projects/`：项目页面模板（`projects.json` 的 `source_path` 相对此目录）。
- `data/`：SQLite、LLM 本地配置、快照；不提交版本库。
- `knowledge-base/`：知识库 Markdown 资产。
- `outputs/`：交付产物（草稿、DOCX/PDF）。
- `deploy/`：Nginx / systemd / 健康检查 / 备份 / 一键部署脚本（模板）。
- `desktop/`：Electron 桌面壳。
- `backup.py`：数据库备份/恢复 CLI。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

## 安全边界

- `.env`、`data/*.db`、`data/llm_settings.json` 均不提交版本库（见 `.gitignore`）。
- 云开发只接受白名单工作区 + 固定配方命令，使用 `shell=False`，不支持任意 shell。
- 浏览器项目只能访问公开 URL；工作台自身（`WORKBENCH_PUBLIC_URL` 配置的源）永远不能成为目标。
- 服务器监控为只读探测；日志读取和重启必须服务器侧人工确认。

## License

[MIT](LICENSE)
