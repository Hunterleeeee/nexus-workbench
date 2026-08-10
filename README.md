# Workbench

个人线上工作台的代码与数据工作区。日常入口只使用线上地址，本目录仅用于代码、配置和产物维护：

`/srv/workbench/`

线上访问：`https://workbench.example.dev:8765/`（当前发布版本以线上页面/API 为准，受保护 API 需认证，日常只使用线上入口；本机不启动 Workbench 端口）

## 入口

- `/`：紧凑型项目入口首页（「现在要处理」待办：一键处理 / 忽略 / 恢复；`⌘K` 命令面板；侧栏推送订阅 + 版本号）
- `/projects/inbox`：快速收件箱（7 类分类、合并建议、批量整理），数据保存到 `data/workbench.db`
- `/projects/knowledge`：本地 Markdown 知识库（**关键词 + 语义向量混合检索**、Obsidian 只读索引、从产物生成草稿），文件保存到 `knowledge-base/`
- `/projects/doc-factory`：把 PDF、Word、Excel、PPTX、HTML、Markdown 或粘贴材料生成 Markdown 产物（可选 MarkItDown 增强转换；DOCX/PDF 交付+审批+按意见重新生成）
- `/projects/sub2api`：查看 Sub2API 余额、订阅、额度趋势与用量快照（服务器自动同步 + 风险评估）
- `/crawl4ai`：Crawl4AI 网页研究入口（队列、取消、证据问答、**支持微信公众号文章抓取**）
- `/projects/web-research`：轻量网页研究浏览器（多页面上下文、来源证据、追问、Artifact/WorkItem 交接；附无需安装扩展的“研究当前网页”书签入口）
- `/projects/cloud-dev`：受控云开发入口（状态、固定测试配方、审批构建；不接受任意 shell）
- `/projects/cid-dashboard`：中国独立开发者看板（机会卡、竞品比较）
- `/projects/market`：量化选股（4 段式研究流程、SVG 走势图、因子、观察任务、显式历史样本采集、日报/周报、回测）
- `/projects/server`：服务器只读监控（可配置阈值、历史展开）
- `/projects/aihot`：AI 热点研究（多数据源、洞察、机会交接、摘要推送 Web Push）
- `/projects/idea-analysis`：想法分析（结构化验证、证据/指标回填、继续/暂停/转向）
- `/automation`：自动化中心（规则类型目录、多步骤计划、重试与人工接管；线上实际规则数量以页面和 API 为准）

首页“总调度 Agent”是父 Agent；各项目入口声明为子 Agent。调度结果会写入 `data/workbench.db` 的 `work_items`，跨项目交接通过 `/api/handoffs` 记录。总调度对子 Agent 并发调用并建立独立 child Run（失败隔离、partial 标记）。

项目联动与 Agent 的现状、未完成项和后续三轮路线见 [`PROJECT-ARCHITECTURE.md`](PROJECT-ARCHITECTURE.md)；待做与优化清单（含文案/交互/功能分级）见 [`PROJECT-TODO-OPTIMIZE.md`](PROJECT-TODO-OPTIMIZE.md)。LLM Provider 规则、环境变量兜底和 Secret 迁移边界见 [`LLM-CONFIGURATION.md`](LLM-CONFIGURATION.md)。

## 目录职责

- `app.py`：FastAPI、Agent、联动与本地数据接口。
- `static/`：工作台和各项目页面资源。
- `data/`：SQLite、脱敏快照、LLM 本地配置与可恢复备份；不提交版本库。
- `knowledge-base/`：工作台生成的 Markdown 知识资产。
- `outputs/`：版本化草稿和正式 DOCX/PDF 交付包。
- `deploy/`：Nginx、systemd、健康检查、备份与一键部署脚本。
- `desktop/`：Electron 桌面壳。
- `companion/`：仅监听本机回环地址的 Gemini Companion；按需调用来财固定 bridge，不要求来财主程序常驻。
- `backup.py`：数据库备份/恢复 CLI（backup / list / restore）。
- `PROJECT-*.md`、`UNFINISHED-CHECKLIST.md`、`ITERATIONS.md`、`PROJECT-TODO-OPTIMIZE.md`：架构、审计、联动、迭代和待做/优化记录。
- `PROJECT-FUTURE-INTEGRATIONS.md`：集成筛选、首版接入边界和后续候选；ntfy / Miniflux / Zotero / GitHub Issues & PR / ActivityWatch / Linkding / Paperless-ngx / Vikunja 已有首版只读接口，MarkItDown 已作为可选文档转换器接入，其余仍是候选。

验收 WorkItem 完成取证后统一转为 `archived`，保留数据库中的 Run、Relation、Notification 和证据矩阵，不继续占据首页待处理列表。正式交付与真实联动产物不作为临时文件清理。

## 运行入口

日常访问和验收统一使用线上地址：<https://workbench.example.dev:8765/>。本机不再作为工作台运行入口；发布、备份、回滚和健康检查见 [`deploy/README.md`](deploy/README.md)，统一由 `deploy/deploy-workbench.sh` 管理。

## 全局 LLM

点击首页左侧“全局 LLM”，配置一次 API Key、API 地址和模型名（支持多条目主/fallback 角色）。API 地址可填 OpenAI 兼容 API 基地址（例如 `/v1`）或完整 `/chat/completions` 地址；禁止把用户名、密码、Key 或 URL 片段写进地址。只有三项都有效的条目才会调用；缺项条目会保留并说明原因，调用顺序为主配置 → 保存顺序中的 fallback → 环境变量最后兜底。所有项目子 Agent（包括独立开发者看板）都通过工作台后端共用同一份配置，浏览器项目不会再单独发送 API Key。首页和 Crawl4AI 的配置弹窗都会显示当前生效 Provider 与近 24 小时运行指标。

配置保存在 `data/llm_settings.json`，后端会尝试设置为仅当前用户可读；该文件已加入 `.gitignore`。也可以使用 `.env` 作为备用配置：

```env
LLM_API_KEY=你的密钥
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

## 飞书云开发与本机 Gemini

飞书消息使用明确前缀进入受控云开发链路：

```text
云开发 workbench 查看状态
云开发 workbench 运行测试
云开发 workbench 构建
```

服务器 `.env` 推荐使用带 `WORKBENCH_` 前缀的飞书配置（代码也兼容无前缀的旧名称）：

```env
WORKBENCH_FEISHU_APP_ID=飞书自建应用 App ID
WORKBENCH_FEISHU_APP_SECRET=飞书自建应用 App Secret
WORKBENCH_FEISHU_VERIFY_TOKEN=事件订阅校验 token
# 或使用 WORKBENCH_FEISHU_ENCRYPT_KEY 替代 VERIFY_TOKEN
```

服务器服务模板默认把发布目录作为显式云开发工作区（部署脚本会按目标目录替换路径）；也可以在 `.env` 中改为 alias/path 配置。状态/测试只有在该白名单存在时执行，构建始终先进入 Workbench 审批中心。命令由固定配方生成，使用 `shell=False`，不支持任意 shell、远程 SSH 或自动部署。飞书公开回调还必须配置 `WORKBENCH_FEISHU_ENCRYPT_KEY` 或 `WORKBENCH_FEISHU_VERIFY_TOKEN` 至少一项，否则回调会拒绝处理；回调按事件 ID 做 7 天幂等，防止飞书重试重复执行云开发动作。

网页研究页里的 Gemini 开关调用本机 `companion/workbench_companion.py`。Companion 只监听 `127.0.0.1:8766`，启动/停止来财 bridge 前会要求用户确认，并可能弹出 macOS 管理员授权；未运行 Companion 时，服务器不会代替本机执行。

## 迁移说明

原先的 Crawl4AI Studio 已迁入本目录根部，之前的看板页面已复制到 `projects/cid-dashboard-v2.html`。旧目录保留为回退副本，后续以本目录为准。

Sub2API 页面使用 `data/sub2api_snapshot.json` 保存最近一次浏览器同步的脱敏快照；不会读取或保存完整 API Key、密码或浏览器 Cookie。
