# Workbench 开源准备清单

> 生成时间：2026-08-15
> 状态：待确认（本清单用于决定开源包内容，确认后按 P0→P2 执行）

---

## 一、现状摸底结论（已自动核查）

### ✅ 已达标（无需处理）
| 项 | 状态 |
|---|---|
| `.env` / `.env.example` | `.env` 已 gitignore；`.env.example` 是占位符（`your_api_key_here`），无真实密钥 |
| `.workbuddy/`（含真实飞书 token 的记忆文件） | 已 gitignore，不入库 |
| `data/*.db` / `knowledge-base/` / `outputs/` | 已 gitignore |
| `_to_delete/`、`desktop/dist/`、`dist/` | 已 gitignore |
| `projects.open-source.json` 模板 | 已存在（crawl4ai `enabled:false` + note） |
| 量化（market 项目） | 确认保留，只屏蔽爬虫入口引导 |

### ⚠️ 敏感点（必须处理）
| # | 位置 | 内容 | 风险 |
|---|---|---|---|
| S1 | git 历史（100 提交） | 全部含 `workbench.example.dev` | 高——直接开源=裸奔 |
| S2 | `projects.json` | 含 `/home/user/...` 绝对路径 | 高 |
| S3 | `tests/` 4 处 | `workbench.example.dev` 硬编码 | 中 |
| S4 | `CHANGELOG.md` / `PROJECT-STATUS-AUDIT.md` / `PROJECT-ARCHITECTURE.md` / `LLM-CONFIGURATION.md` 等 | 线上域名 + 内部事故细节 | 高 |
| S5 | `deploy/README.md`、`deploy/pull-knowledge.sh` | 服务器地址 `root@workbench.example.dev` | 高 |
| S6 | `dist/` | 0.3.142 旧部署包 | 中 |
| S7 | `static/server.html` | "App / PM2" 内部服务名 | 低 |
| S8 | `companion/README.md` | 本机路径 | 低 |
| S9 | `app_pkg/market.py:1012` | eastmoney 搜索 API token（公开接口） | 低-中 |
| S10 | `app_pkg/server.py:591-592` | **硬编码默认值 `root@workbench.example.dev` + `~/.ssh/deploy_key`**（监控页默认 SSH 连你的服务器） | 高 |
| S11 | `companion/` 全套 | 硬编码 `/home/user/.../harness/...` + 来财（Laicai）私有生态（Gemini OAuth 桥，开源用户用不上） | 高 |

---

## 二、开源包目录清单（建议）

### 🟢 进开源包（19 类）
```
app.py                      # 主应用（已拆到 1005 行）
app_pkg/                    # 35 个领域模块（核心资产）
static/                     # 前端资源（脱敏后）
projects/                   # 项目页面模板（脱敏后）
tests/                      # 测试（脱敏后）
companion/                  # ⚠️ 建议移出（来财私有生态，S11）
desktop/                    # Electron 桌面壳（含 package.json）
deploy/deploy-workbench.sh  # 通用部署脚本（保留，脱敏）
*.py（根目录 12 个 worker/工具脚本）
requirements.txt / requirements-optional.txt / requirements.lock
pytest.ini
.gitignore
.env.example
projects.open-source.json   # 开源版项目配置（新默认）
nexus-logo.svg
LICENSE                     # 需新建（见 P0-4）
README.md                   # 需重写（见 P0-5）
docs/OPEN-SOURCE-PLAN.md    # 本清单（可选保留）
CHANGELOG.md                # 需脱敏或精简（见 P1-6）
```

### 🔴 移出仓库（内部专属，不进开源包）
```
companion/                      # 来财私有 Gemini OAuth 桥（S11，含本机路径）
deploy/pull-knowledge.sh        # 含服务器地址
deploy/README.md                # 含线上地址
deploy/deploy-workbench.sh 备份变体（如含服务器默认值）
ITERATIONS.md                   # 内部迭代记录
PROJECT-AGENT-ROADMAP.md        # 内部路线
PROJECT-ARCHITECTURE.md         # 内部架构备忘（含域名）
PROJECT-EFFICIENCY-CATALOG.md   # 内部效率清单
PROJECT-FUTURE-INTEGRATIONS.md  # 内部规划
PROJECT-LINKAGE-MATRIX.md       # 内部联动矩阵
PROJECT-OPTIMIZATION-MATRIX.md  # 内部优化矩阵
PROJECT-STATUS-AUDIT.md         # 内部状态审计（含域名）
PROJECT-TODO-OPTIMIZE.md        # 内部待办
UNFINISHED-CHECKLIST.md         # 内部未完成清单
WORKBENCH-TODO-NON-INTEGRATIONS.md  # 内部非集成待办
LLM-CONFIGURATION.md            # 内部 LLM 配置说明
```

### ⚪ 待决策（3 项）
| 项 | 选项 A | 选项 B |
|---|---|---|
| `CHANGELOG.md` | 保留，但删除含域名/内部细节的条目 | 整个移出仓库 |
| `deploy/deploy-workbench.sh` | 保留（改为读环境变量服务器地址） | 移出，开源只给部署说明 |
| 示例数据 | 提供 `projects.open-source.json` 空模板 | 同时提供 1-2 个演示项目 |

---

## 三、执行步骤（确认后按此执行）

### P0 安全红线（不做不能开源）
1. **清洗 git 历史**：`git filter-repo` 全库替换 `workbench.example.dev` / `203.0.113.1` / `/home/user` / `deploy-host` / `deploy_key` → 占位符（如 `YOUR_DOMAIN.example`）；或新建干净仓库只推快照
2. **projects.json 相对路径化**：`source_path` 改为相对路径（如 `projects/cid-dashboard-v2.html`）；默认启用 `projects.open-source.json`
3. **tests 脱敏**：4 处 `workbench.example.dev` → `example.workbench.dev`
4. **新建 LICENSE**（MIT / Apache-2.0 二选一，需用户确认）
5. **重写 README**：通用安装/架构/快速开始，去本机路径与线上地址
6. **server.py 默认值脱敏**：`WORKBENCH_SERVER` 默认值去掉 `root@workbench.example.dev`（改为空或 `root@your-server`）、`WORKBENCH_SERVER_SSH_KEY` 默认去掉 `deploy_key`（S10）
7. **companion/ 整体移出**（S11）

### P1 内容裁剪（决定开源范围）
6. 按上方"移出仓库"清单执行移动/归档（建议移到 `archive/` 或直接删除）
7. `dist/` 旧包删除
8. `static/server.html` "Hotel" → 通用名；`companion/README.md` 去本机路径
9. `app_pkg/market.py` eastmoney token → 环境变量读取

### P2 工程化（开源体验）
10. `requirements.txt` 拆分：crawl4ai 移到 optional（当前两边都有）
11. `.gitignore` 补漏：`projects.json`（用 open-source 模板代替）、`archive/`
12. 补 CONTRIBUTING / SECURITY / 项目结构说明

---

## 四、需要你拍板的 4 个决策

- [ ] **D1 LICENSE 选型**：MIT（最宽松） / Apache-2.0（带专利授权） / GPL-3.0（传染）
- [ ] **D2 CHANGELOG**：保留脱敏 / 移出
- [ ] **D3 部署脚本**：保留通用化 / 移出
- [ ] **D4 示例数据**：空模板 / 带演示项目
