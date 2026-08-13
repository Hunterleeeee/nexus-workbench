# 迭代记录

## v0.3.179 · 2026-08-13

- **主动学习推荐升级为个性化**：LLM 结合用户档案（岗位/目标岗位/经验/关注方向/目标）、课程进度（已完成课程 + 课程大纲）、已问历史，现场生成贴合个人的推荐问题；1 小时缓存避免反复调用；未配置 LLM 或生成失败自动回落内置精选池。已探索排除升级为**模糊相似度**（去掉标点归一化 + 包含/相似度匹配）——LLM 换措辞（"什么是 RAG" vs "RAG 到底解决了什么"）也能识别，不会换一批又换回问过的。
- **修复使用统计趋势图时区错位**：`daily_runs` 原来按 UTC 日期分组（created_at 存 UTC），本地 0:00-8:00 的活动被算进前一天、趋势图每天错位 8 小时。改为按服务器本地时区（CST/UTC+8）分组。
- 全量 507 通过。

## v0.3.178 · 2026-08-13

- **主动学习「推荐问题」**：名词/热点/理论/方法四类各预置 8 条贴合课程的问题（面向非专业人士，每条带「为什么值得问」），输入框下方直接展示 4 条可点，「换一换」轮换下一批；点击推荐直接发起探索。已探索过的问题自动排除，不会换回问过的。具身智能与 AI 转型学习各自独立推荐（8 个组合共 64 条）。
- 新增回归测试（两个 track × 四个 kind 都必须有带 topic/why 的推荐），全量 507 通过。

## v0.3.177 · 2026-08-13

- **首页加载优化**：`/api/projects` 热路径上的 `import crawl4ai` 改为 `importlib.util.find_spec` 只查安装不执行导入——crawl4ai 光导入要 800ms+（async_webcrawler/async_database 一堆依赖），服务重启后的首个首页请求实测从 ~880ms 降到 ~60ms。
- **使用统计口径修正**：`agent_runs` 统计排除内部记录 `dispatch_child`（子调用双计）/`evidence_acceptance`（联动验收基线）/`manual_takeover`（人工接管）/`approval_decision`（审批动作）——曾出现 30 天 282 条 run 里近一半是这类水分；趋势图同口径。一句话统计「真正在用的」排序改为 runs 主导（之前按 运行+工作项+产物 混合排序，后台产生大量工作项的项目压过用户天天对话的项目）。
- **产品作战室按项目经营**：页面顶部新增全局项目选择器（全部/各产品项目），反馈/需求/原型/决策全部 tab 随项目切换；后端 `overview?project_id=` 过滤补齐决策/原型（之前只过滤反馈/需求，决策/原型 tab 切项目后仍显示别家数据）。
- 新增 2 项回归测试（统计口径排除内部 kind、作战室项目过滤包含决策/原型），全量 506 通过。

## v0.3.176 · 2026-08-13

- **修复应用通知点击双重跳转**：桌面壳 `setWindowOpenHandler` 对 `window.open` 返回 deny（返回 null）但已开新标签，前端原逻辑误判为"打不开"走 `location.href` 兜底 → 「新页面 + 原页面」一起跳。统一改为 `WorkbenchUX.openTarget`：壳环境走 `desktopShell.openTab`，浏览器环境 `window.open`，仅非壳且弹窗被拦截才跳当前页（通知面板两处 + request.js 全局入口）。
- **修复 AI 热点「商业/综合」筛选恒为 0**：映射表有 36kr→商业但源列表没有 36kr；「综合」只是未知域名兜底、所有源都被映射命中所以永不出现。新增源：钛媒体 `tmtpost.com/rss`→商业（实测解析 19 条）、中国新闻网 `chinanews.com.cn/rss/scroll-news.xml`→综合（实测解析 30 条今日新闻；曾先用新浪滚动新闻但它是 2018 年死 feed，实测 15 条全是旧闻后替换）。
- **热点「阅读原文」提为显式按钮**：从「更多」菜单移出到卡片操作区，点击统一走 `openWorkbenchTarget`（壳内新标签），不再依赖 details 菜单；筛选无结果时状态栏提示"该领域暂无订阅源"。
- 新增回归测试（默认源必须覆盖商业/综合标签），全量 504 通过。

## v0.3.175 · 2026-08-13

- **本地知识库阅读闭环（P0）**：笔记卡片新增「查看 / 编辑 / 删除」操作。查看弹窗渲染 Markdown 全文（复用 markdown.js，代码块可复制）；编辑弹窗改标题与正文（标题默认保持原标题，改为一级标题写回）；删除移入知识库 `.trash` 回收站（不物理删除，`knowledge_files` 检索排除 `.trash`）。
- **后端新接口**：`PUT /api/knowledge/note`（编辑）、`DELETE /api/knowledge/note`（删除）；路径校验抽成 `_resolve_knowledge_path`（读/写/删三处共用同一越界拦截）。
- 新增 4 项回归测试（编辑正文+标题行、不传 title 保留原标题、删除进回收站且不在检索、越界路径读/写/删均 404），全量 503 通过。

## v0.3.174 · 2026-08-13

- **收件箱快速记录面板头部换行**：`.panel-head` 允许 `flex-wrap`，窄面板下"实时预览"开关和"全屏编辑"按钮各自从竖排/换行恢复到正常显示；按钮加 `white-space: nowrap` 兜底；实时预览 toggle 加 title 解释用法。

## v0.3.173 · 2026-08-13

- **修复：收件箱转给想法分析的任务在想法分析页看不见**。根因是展示过滤只认 `kind == "opportunity"`，而收件箱路由创建的工作项 kind 是 `idea_review`——交接本身执行正常（会话、结果、后续任务都生成了），只是被列表过滤藏掉。过滤放宽为 `opportunity / idea_review / idea_followup`；已完成且无会话的交接新增「查看分析结果」弹窗（Markdown 渲染完整初判）。

## v0.3.172 · 2026-08-13

- **收件箱「实时预览」开关样式统一**：浏览器默认 checkbox 蓝色实心样式太突兀，自定义为应用内圆角开关（紫色轨道 + 白色滑块），并从工具栏移到面板头部（与「全屏编辑」按钮同行），视觉与全站统一。

## v0.3.171 · 2026-08-13

- **收件箱写入升级**：快速记录卡片新增格式工具栏（加粗/斜体/行内代码/标题/列表/引用/链接）和「实时预览」开关，输入时下方即时渲染 Markdown；新增「全屏编辑」弹窗——左侧编辑（同款工具栏）、右侧实时预览、类型/优先级/来源/标签字段齐全，⌘↵ 保存、Esc 关闭，小屏自动上下分栏。保存走与快速记录同一接口，保存后收件箱 Agent 照常自动分类。

## v0.3.170 · 2026-08-13

- **运行时工具策略按「只读 readonly / 可逆 auto / 高危 confirm」收口**：`cloud_dev_generate`（生成云端产物，版本化且不覆盖用户文件，可逆）由 confirm 降为 auto；唯一保留确认的是 `cloud_dev_test`（在服务器上真实执行固定命令，高危）。现在 19 readonly / 7 auto / 1 confirm，Agent 日常动作几乎全部自动执行，只在真高危处停下。

## v0.3.169 · 2026-08-13

- **notify（发送通知）降为 auto 档**：通知本身无害（只写应用内通知中心，浏览器 Push 仍受独立订阅约束），每条都要人工确认会打断 Agent 流程。确认模式工具仅剩 cloud_dev_generate / cloud_dev_test。

## v0.3.168 · 2026-08-13

- **修复确认门执行端**：确认模式工具（notify / cloud_dev_generate / cloud_dev_test）创建的动作用运行时工具名，确认后执行的分发却只认旧点号命名（market.watchlist.add 等），两套名字零交集——点「确认」必然报「工具尚未接入执行器」。现在确认执行直接回调 `execute_react_tool(..., confirmed=True)` 真正执行，旧点号分支保留兼容。
- **流式截断续写**：`stream_llm_text` 被 max_tokens 截断（finish_reason=length）时自动续写，最多续 `LLM_MAX_CONTINUATIONS` 段；续写期间的 finish 扣住不外发（前端不会以为答案完了）；续满上限在正文明说「还有内容没写完」并把 reason 标成 `length_capped`；ReAct 循环不再把 finish reason 硬写 "stop"，截断信号真实透传。
- **运行中的任务可取消**：`cancel_agent_task` 对 running 任务也标 cancelled；ReAct 循环在每轮工具之间（与插入消息同一位置）查取消标志，取消后停止后续工具调用，不再只能干等（最坏 4 轮 × 60 秒）。
- **Agent 输出去掉横向滑动 + 代码块复制**：代码块长行改为换行显示（不再左右滑动）、表格单元格换行；代码块右上角新增「复制」按钮（navigator.clipboard + 非安全上下文 textarea fallback），项目 Agent 面板与文档工厂预览都生效。
- 验证：新增流式续写 ×2、运行中取消 ×2 专项测试，更新旧取消语义测试；全量 497 项 + 前端 SSE 2 项通过。

## v0.3.167 · 2026-08-13

- **Agent 回答统一 Markdown 渲染（static/markdown.js）**：此前模型输出的表格/列表/代码块/加粗全以原始符号显示，而 Agent 回答最爱用表格和列表。新增安全渲染库（先整段转义再还原已知结构，任何未识别内容留在转义态，杜绝 HTML 注入），支持表格、围栏代码块、标题、列表、引用、行内样式；doc-factory / ai-learning / aihot 等页面统一接入。服务端同步 `_is_markdown_table_divider`，与前端表格识别规则保持一致。
- **新增通用流式对话接口 `/api/chat-stream`**：SSE 逐块返回 LLM 增量（delta / finish / error / reset），前端 fetch + ReadableStream 消费。
- **前端 SSE 可执行回归测试**：`tests/request_stream.test.mjs`（node --test 运行，覆盖 error+[DONE] 误判、reset 丢弃半段），desktop `test:frontend` 脚本接入。
- 验证：全量 493 项通过 + 前端 SSE 2 项通过。

## v0.3.166 · 2026-08-13

- **项目 Agent 流式过程反馈**：Agent 回答期间（尤其多轮联网调研的长任务），工具执行事件实时推给前端——气泡上方显示「搜索公网完成 → 抓取网页完成 → 正在生成回答…」，不再像卡死。后端 ReAct 循环工具执行完 yield `event` chunk，前端 `fetchStream` 消费并展示。
- **文档工厂产物可点击查看**：生成结果与历史产物列表支持点开预览（markdown 渲染），生成/重新生成的正文也从纯文本改为 markdown 排版。
- 验证：新增过程事件回归测试，全量 441 项通过。

## v0.3.165 · 2026-08-13

- **每个项目 Agent 都获得公网调研能力（web_search + web_fetch）**：此前只有总调度主 Agent 能上网，项目 Agent（尤其 doc-factory 文档工厂）写"深度分析"类文档时没有上网工具，只能声称"交接给网页研究 Agent"，而交接不落地（actions 为空）——用户干等。现在 `SUBAGENT_TOOL_MAP` 给全部 15 个项目配齐 `web_search`（360 搜索，只读），没有 `crawl_fetch` 的项目再补 `web_fetch`（抓正文）；`AGENT_TOOL_POLICIES` 与 `agent_detail` 能力声明同步，总调度工具边界、前端能力列表、项目页 ReAct 执行三处一致。
- 实测：web_search 搜「Pi Coding Agent」返回 5 条真实结果（含评测文），doc-factory 可直接搜索+抓取成稿，不再依赖交接链路。
- 验证：新增防回归测试（每个项目 Agent 都声明并配齐 web 工具），全量 440 项通过。

## v0.3.164 · 2026-08-13

- 修复流式请求把 `error` + `[DONE]` 误判成成功的问题，并在主 Provider 已输出部分内容后切换备用 Provider 时清空旧内容，避免答案拼接污染。
- 流式调用现在同步记录 Provider 健康状态，限流与冷却策略对普通请求和流式请求一致。
- AI 学习“本节整体产出”改为真正选填；量化研究卡在远程行情不可用时恢复使用本地历史快照。
- 全部产品页统一获得跳到主要内容、可见键盘焦点、状态/错误播报、减少动态效果和移动端防溢出支持。
- 新增浏览器端 SSE 可执行回归测试及服务端专项回归覆盖。

## v0.3.163 · 2026-08-12

- **修复「项目 Agent」气泡空（前端 SSE 解析丢字）**：服务端项目 Agent 工具轮流式产出 `delta_text` chunk、收敛轮产生 `delta` chunk；前端 `fetchStream` 旧版本只识别 `delta`，把所有工具轮文本都丢掉了——所以后端实际返回了长文本（nginx 记录 78593 字节），前端气泡却空空如也。`fetchStream` 现在同时识别 `delta` 和 `delta_text`，渲染恢复。

## v0.3.162 · 2026-08-12

- 量化研究卡/基金修复的界面展示同步（数据源徽标、样本点数、基金净值日），随 0.3.161 后端一起上线。

## v0.3.161 · 2026-08-12

- **修复「AI 出题」连续失败（502）**：根因是出题内容（题干+情境+评分要点+参考答案）超出 `max_tokens=1500` 上限，输出被稳定截断成不完整 JSON，`parse` 失败后直接 502。出题输出预算提升到 3000，并补齐三层兜底：解析失败自动重试一次（换采样）→ 仍失败或 LLM 调用异常时落到内置模板题，**不再 502**；模板题保留用户给的题目方向，可正常作答与评判。
- **修复项目 Agent「LLM 未返回内容」**：ReAct 工具轮用尽后的收敛回答流式文本 chunk 类型是 `delta`，收敛循环误判为 `delta_text`，导致文本只显示不收集——工具轮越多（如量化研究多步调用）越容易触发「未返回内容」。同时修掉流式工具轮 failover 被提前截断的问题（单个 Provider 失败不再中断整条链，会继续尝试 fallback）。
- **量化个股研究卡接入真实历史数据**：回测/样本外不再只依赖本地快照积累——股票/场内 ETF 用腾讯历史日 K（上市以来真实价格），场外基金用东财历史净值（日频），新加自选当天就能算出结果；界面标注数据来源与样本点数。
- **基金研究不再"没有历史数据"**：新增东财基金历史净值拉取，场外基金（腾讯接口查不到、`sz` 前缀还会被误判为可查）自动兜底到净值序列，基金也能立刻出研究卡。
- 「AI 评判」与「主动学习」同类截断预防：评判 `max_tokens` 1600→2400，调用异常转明确错误提示；主动学习非方法类 1600→2400。
- 验证：新增 6 项专项测试（出题模板兜底 ×2、研究卡腾讯 K 线、基金净值兜底、流式收敛轮 delta 收集、failover 不截断），全量 432 项通过。

## v0.3.153 · 2026-08-11

- **新增「AI 转型学习」项目**：每天一节“知识 → 工作案例 → 小练习 → 自测复盘”课程；支持岗位、目标、基础、方向和每日时长个性化，全局 LLM 不可用时自动降级到 14 天内置课程。
- **学习闭环与推送**：SQLite 记录课程、连续学习、自测正确率、掌握程度和知识库笔记；Sync Worker 新增 `daily:HH:MM` 本地时间调度，按设定时间生成课程并发送工作台通知与浏览器 Web Push。
- 验证：AI 学习专项 3 项、全量 215 项通过；1440px / 375px 深浅色真实浏览器检查无横向溢出。
- **全项目默认浅色**：工作台、平台工具、AI 浏览器、量化选股、Sub2API、Crawl4AI 和独立开发者看板首次打开统一使用浅色；只有用户主动切换后才保存深色偏好，不再自动跟随系统变黑。
- **主题状态统一**：所有项目共用 `workbench-theme`，已打开的多个窗口会同步切换；按钮同步文字、提示和无障碍状态，页面解析初期即可应用已保存主题，减少闪白或闪黑。
- **深浅色可读性修复**：补齐深色画布、侧栏、卡片、表单、状态色和项目 Agent 变量，并为原先锁死深色的 Sub2API、Crawl4AI 与看板外壳补上完整浅色表面。
- 验证：主题资源纳入离线缓存，新增全页面浅色默认与共享主题回归检查。

## v0.3.152 · 2026-08-11

- **AI 浏览器真多标签**：每个内部标签拥有独立 `WebContentsView`，切换只改变可见性，不再重载网页；输入内容、登录表单、滚动位置和页面历史都会保留，支持 `Ctrl/⌘+Tab`、数字快捷键和网页新窗口自动新建内部标签。
- **书签管理**：新增当前页收藏、搜索、打开、删除和收藏状态反馈；书签保存在桌面应用本地资料目录，外部浏览器 Bookmarklet 改名避免与真正书签混淆。
- **系统密码保险箱**：网站密码使用 Electron `safeStorage` 接入 macOS 系统加密，私有文件权限为 `0600`；支持保存网页中已输入的登录信息、手动添加、按账号填入和删除。明文密码不返回页面、不进入 localStorage 或 AI 快照，只允许在完全相同的网站 Origin 填入，且不会自动登录。
- **会话与 AI 稳定性**：恢复上次选中的标签；页面切换期间后台阅读结果会正确归属原标签；同一页面读取失败不再重复启动和刷出多条错误消息。
- **响应式资料抽屉**：标签、书签、密码统一为左侧资料区，小屏改为可开合抽屉；768px 无横向溢出，工具栏会自动换行。
- 验证：桌面真实页面多标签输入/滚动保持、书签增删、系统加密保存/填入、跨 Origin 拒绝和 AI 去重回归通过；完整测试、桌面校验、语法与补丁检查通过。

## v0.3.151 · 2026-08-11

- **记忆上下文瘦身**：单次 Prompt 最多注入 5 条 / 1200 字，拆成最多 2 条置顶核心偏好和 3 条当前问题相关记忆；无匹配的普通全局记忆不再注入。
- **按 Agent 独立检索**：总调度不再把同一份记忆复制给所有子 Agent；每个子 Agent 按自己的项目取小窗口，最终汇总只保留置顶核心偏好。
- **成本可见**：结构化结果新增记忆上下文字符数和调用次数；明确的“记住/以后”规则自动置顶，仍受核心条数上限约束。

## v0.3.150 · 2026-08-11

- **跨会话长期记忆**：新增全局/项目两级记忆、候选确认、置顶、编辑、忽略、彻底删除、置信度和使用记录；只有已确认记忆会进入 Agent 上下文。
- **可控学习与隐私边界**：明确的“记住/以后”会直接保存，偏好表达先进入待确认；不会从助手回复学习，也不会保存凭据或敏感字段，Workbuddy 旧偏好只能预览后手动导入。
- **Agent 与会话接入**：项目 Agent、总调度 Agent 和飞书复用统一记忆检索；总调度网页会话可恢复，结构化结果会标出本轮使用和发现的记忆数量。
- **记忆中心**：工作台新增“我的记忆”，支持桌面与手机入口、键盘焦点管理、响应式卡片和删除确认；SQLite 升级至 schema v7，并新增专项回归测试。
- **Workbench 直连 Cowart**：产品作战室新增“原型”工作区，需求卡可直接创建/打开 Cowart 无限画布；每个原型使用服务器分配的隔离目录，画布自动保存，发布时冻结快照与 HTML 草稿并登记 Artifact、`requirement_to_prototype` 和 `version_of` 关系。
- **Cowart 安全与部署边界**：固定 Cowart 0.1.25 前端资源，移除默认 Google Analytics 标识并限制画布外联；公开 tldraw 生产许可提醒，不依赖服务器上的 Codex 插件目录。

## v0.3.149 · 2026-08-11

- **量化自选数据一致性**：自选清单成为当前行情、今日待办、AI 整理和组合体检的唯一来源；删除部分自选时同步过滤旧报价，删除全部时清空当前快照、数据时间和旧卡片，历史研究样本仍保留。
- **买卖计划不丢失**：编辑自选时，仍然保留在清单中的股票会继续保留用户写过的买点、卖点、止损和备注。
- **小白首页重构**：首屏明确说明“找候选 → 加自选 → 写计划线”的用途，空自选展示三步引导和双入口；旧版重复今日卡强制隐藏，不再造成固定股票榜的错觉。
- **AI 浏览器体验**：真实网页层独立滚动并自动适配固定宽页面；网页操作与内容问答分开解锁，支持实时页面上下文和安全的受控操作。
- 验证：量化自选增删真实页面回归通过，专项 11 项通过；完整测试、桌面校验与缓存版本同步更新。

## v0.3.142 · 2026-08-10

- **网页研究 Agent 协作**：新增 POST /api/web-research/agent（AI 伴读 Agent 会话）、GET /api/web-research/agent/（会话列表）、POST /api/web-research/mentions/resolve（提及解析）、GET /api/web-research/mentionables（可提及对象）、POST /api/web-research/tab-groups（标签组管理）——研究页与工作台事项/项目双向联动。
- **量化选股规则引擎**：POST /api/market/screen 自定义条件选股（自定义筛选器）、POST /api/market/watchlist/rules（自选规则）、GET /api/market/screen/selftest（自检）、GET /api/market/today（今日行情快照）。
- **用量统计**：GET /api/usage/stats。
- 前端配套：static/web-research-plus.js / market-screen.js / market.css 等同步更新；版本四处同步到 0.3.142（含 desktop 壳）。

## v0.3.140 · 2026-08-10 · 2026-08-10

- **云开发「云端自动改+审批」**（用户「云开发那个 可以直接改造咱们现在做这个项目吗」→ 选「云端自动改+审批」）：
  - 飞书/工作台说「云开发 帮我改一下 X」「云开发 优化一下 X」→ LLM 读取代码库文件清单生成结构化编辑计划（old 精确片段唯一匹配替换，只允许 static/ 与固定小模块），进入审批；审批通过前不改任何代码。
  - 审批通过后执行：备份涉及文件 → 应用编辑 → 运行测试（云开发+工作台测试集）→ 重启 workbench 服务 + 健康检查；任一步失败自动回滚备份并通知。
  - 新模块 cloud_patch.py：code_file_index / plan_patch / validate_edits（路径白名单、唯一匹配、危险模式过滤）/ apply_edits（先备份后应用，失败回滚）/ rollback；app.py 接线 execute_cloud_dev_patch / execute_approved_cloud_dev_patch（subprocess 跑 pytest + systemctl 重启健康检查）；总调度 REACT_TOOLS 新增 cloud_dev_patch 工具；飞书分发对 patch 返回编辑计划摘要+审批编号。
  - 安全边界：不执行任意 shell、不自动部署、不碰 app.py 等核心大文件、编辑片段必须唯一匹配、应用前备份。
  - 线上真实验收：飞书命令「云开发 帮我改一下 AI 伴读面板的发送按钮颜色 改成蓝色」→ LLM 两阶段（先选文件再喂内容生成精确编辑）→ 编辑计划进入审批 → 批准后应用 + pytest + 回滚兜底 → AI 热点页「开始分析」按钮真实变为蓝色 #1677ff。途中修复：①LLM 单阶段生成的 old 片段匹配不上→改两阶段（选文件+读内容）②asyncio.to_thread 传参错误 ③服务器 .venv 缺 pytest→加入 requirements.txt ④服务进程内 systemctl restart 会杀掉当前请求→改 setsid detached 延迟重启，且纯前端改动（static/）无需重启直接生效。
- **可转债数据源修复**（东财 clist 被限流）：改双源——东财数据中心 RPT_BOND_CB_LIST（存续过滤 EXPIRE_DATE>=today，含评级/到期/转股价/赎回状态）+ 腾讯行情 qt.gtimg.cn（GBK 转码批量补现价/涨跌幅/溢价）；溢价率不可用仍诚实降级低价优先。
- **Electron 桌面壳真浏览器**（用户多选「Electron 真浏览器」）：preload 暴露受控 desktopShell.openWebWindow（只传 URL，不暴露 Node/凭据），主进程 parseSafeWebUrl 校验后开独立 BrowserWindow 直接加载目标网页（顶层窗口不受 iframe X-Frame-Options 限制）；网页研究页检测到桌面壳时「新标签页打开」自动改为桌面真窗口。verify.mjs 安全断言同步更新。
- 验证：新增 CloudPatchTests 4 项（意图解析/危险过滤/白名单/应用回滚往返/非唯一拒绝），全量 181 passed；桌面 verify OK；本地实测 patch API 全链路（解析→生成计划→审批，未配 LLM 时优雅失败）。
- 版本四处同步到 0.3.140。

## v0.3.139 · 2026-08-10

- **量化选股完全重做 + 功能全集（量化 2.0）**（用户「推翻完全重做，全做」；融合巴菲特/段永平价值框架 + 幻方等量化机构方法论 + ETF/可转债/仓位调研）：
  - **新布局**：删掉标签页，单页纵向 = AI 一眼看 / 我的自选（编辑弹窗+丰富行情：迷你走势·开盘·量·PE·涨跌幅红涨绿跌）/ 个股研究卡 / 工具区（ETF轮动·可转债·指数估值·组合体检·仓位计算器）/ 高级研究抽屉。
  - **AI 一眼看（可交互）**：POST /api/market/ai-scan 基于自选快照生成人话总结，输入框可追问；没配 LLM 时明确提示。
  - **个股研究卡**：GET /api/market/research-card 输入代码一次生成——行情+估值（PE/PB 自动）+ 动量/均值回归回测 + 样本外验证，结论带人话解读；价值清单提示人工核对。
  - **新工具**：ETF 动量轮动（东财 K 线 20 日动量+绝对动量过滤，四只宽基）、可转债双低/低价筛选（东财 clist，溢价率字段不稳定时降级低价优先）、指数估值百分位（蛋卷接口：沪深300/中证500/创业板/科创50/上证50 的 PE/PB 历史分位+低估/偏高标签）、组合体检（涨跌分布+快照积累提示）、仓位计算器（2% 法则+半凯利，纯前端实时算）。
  - **高级研究保留**：回测/样本外/策略对比+成本敏感性/估值因子/日报周报/历史采样收进抽屉（dynamic 注入 legacy 工具）。
  - 新前端 static/market.js；后端 6 个新 API；数据源尽力而为+诚实降级（转债溢价率不可用、北向已停止实时披露等均如实标注）。
- **网页研究：真实页面（服务器渲染截图）**（用户「服务器渲染真页面」）：POST /api/browser/render 用服务器 Chromium（patchright，已安装）无头渲染目标网页截图（1280x900 PNG），存 outputs/browser-shots/，GET /outputs/browser-shots/{filename} 认证可见；前端新增「真实页面」按钮与截图视图，三态视图（阅读器/真实页面/原网页 iframe）。截图只读不可点击，交互仍靠「新标签页打开」。
- 验证：全量 177 passed；本地实测 ETF轮动/估值百分位/组合体检/研究卡接口通（转债本地受限，服务器验证）；版本四处同步到 0.3.139。

## v0.3.138 · 2026-08-10

- **云开发：新增「生成工坊」**（用户："我在飞书上说句话，比如说要做个什么东西，直接就能开发，不用本地找你"）：
  - 飞书/工作台里说「云开发 帮我做一个 X」「云开发 写一份 X 报告」「云开发 写一个 X 脚本」→ 云端直接生成可交付产物，不再需要本地找 AI。
  - 产物类型：webpage=单文件网页原型（默认）/ doc=Markdown 文档报告 / script=Python 脚本；LLM 生成后写入 outputs/cloudgen/，注册 Artifact，返回认证可见的产物链接（GET /outputs/cloudgen/{filename}，只读不执行、不部署）。
  - 总调度 Agent 新增 ReAct 工具 cloud_dev_generate（自然语言"帮我做一个X"也会被识别）；云开发 Agent 工具表/子 Agent 映射同步登记。
  - parse_cloud_dev_command 支持自然语言生成意图 + 类型识别（网页/文档/脚本关键词），新增策略声明 generate_policy；安全性不变：不开放任意 shell、产物不执行。
  - 验证：新增自然语言解析测试（网页/文档/脚本/拒绝未知），策略断言更新，全量 177 passed；本地实测 POST /api/cloud-dev 全链路（解析→run→生成→LLM 调用→未配 key 时优雅失败）。
- **网页研究 AI 浏览器 v2**（用户："实验了两个都被拒绝了；AI 伴读太丑；AI 功能优化丰富一轮"）：
  - **打开即阅读**：地址栏输入网址回车 = 直接抓取并渲染阅读器 + AI 自动总结，不再先开 iframe（彻底绕开"网站拒绝内嵌"）；「原网页视图」改为手动切换的选项按钮。
  - **AI 伴读重做**：渐变圆形 AI 头像 + 圆角气泡 + 发送按钮图标化；回复等待时显示"三点呼吸"思考动画。
  - **AI 功能丰富**：快捷动作胶囊组——要点 / 找风险 / 翻译 / 行动项（各走预置研究 prompt），另有「问选中内容」划词解释；交接文案改为「沉淀到知识库 / 交给文档工厂 / 交给想法分析」。
  - Bookmarklet 带入网页也直接进入"自动阅读"流程。
  - 验证：JS 语法、元素引用、乱码检查通过，全量测试 177 passed。

## v0.3.137 · 2026-08-10

- **网页研究重做为「AI 浏览器」**（用户："我想做成豆包浏览器/tabbit浏览器那种，不是现在这样的"）：
  - 浏览器式壳：顶部标签条 + 地址栏（输入 http/https 网址回车打开）+「AI 阅读本页」+「新标签页打开」。
  - 双视图内容区：iframe 网页视图（能嵌的站直接浏览，被禁的站提示）+ 阅读器视图（抓取正文渲染成可读排版，**正文可选中**）。
  - 右侧常驻「AI 伴读」面板：打开网页后点「AI 阅读本页」→ 抓取 + 自动总结；在正文里选中一段文字 → 「问选中内容」让 AI 解释并核对；「总结本页」一键重述；下方可追问、可交接 Artifact。
  - 多标签 = 研究上下文（localStorage 保留，可关闭/切换）；批量研究（多 URL 对比分析）与书签工具收进底部抽屉，不再霸屏；Bookmarklet 带入网页时自动开始 AI 阅读。
  - 阅读器用自写 markdownLight 安全转换（只允许标题/段落/列表/链接/代码/粗体，链接限 http/https），正文渲染不引入 XSS。
- **Gemini 本机桥迁入「全局 LLM」设置**（用户："把 Gemini 拿出来放到左下角跟 llm 在一起，做成设置"）：网页研究页侧栏的 Gemini 卡片移除，改为首页左下角「全局 LLM」弹窗（及 /crawl4ai 弹窗）里的独立「本机 Gemini 桥」设置区块（状态 + 启动/停止，逻辑复用迁移到 llm-settings.js，含超时保护）；网页研究顶栏入口文案改为「全局 LLM / 本机 Gemini ↗」。
- 验证：内联 JS 语法、43 个元素引用、HTML 标签配对、本地三页面 200、全量测试 176 passed（更新了 Gemini 迁移后的断言位置）；版本四处同步到 0.3.137。

## v0.3.136 · 2026-08-10

- **量化选股页整体重做**（用户："量化那个太恶心了，还是得重做"；诉求=全功能保留但重新设计布局，解决"乱 / 看不懂 / 操作麻烦 / 结果看不懂"）：
  - **三标签页布局**：行情（自选+今日涨跌+数据状态）/ Agent 观察（信号+研究任务）/ 研究工具（回测·对比·估值·报告）。默认停在「行情」，深研究工具不再霸屏；标签页选择会记住（localStorage + URL hash 支持）。
  - **功能 100% 保留**：自选搜索/示例/刷新、行情 sparkline、健康度/采样、观察信号、事件研究、回测、walk-forward、策略对比、估值、日报周报全部原样可用（65 个 JS 钩子逐一核对无缺失，API 未改动）。
  - **结果加"一句话解读"**：回测/样本外验证/策略对比结果顶部新增人话结论条（绿色=正向结论、黄色=警示），例如"20 个样本点上，动量策略净收益 +3.2%，跑赢买入持有 +1.1%"；细节数字保留在下方等宽明细区，不再丢给用户一堆数字自己看。
  - **操作减负**：回测成本假设（手续费/滑点）与 walk-forward 参数默认折叠成"高级参数"；研究工具页顶部加"不知道用哪个？"四枚人话说明 chip；回测/样本外验证/策略对比各配「一键示例」按钮（填好参数直接跑）。
  - **视觉收口**：删掉四步编号式长页结构，统一卡片化；数据健康度压缩成常驻横条（行情源/数据时间/快照数/可信度）；历史样本采集独立折叠块。
  - 验证：内联 JS 语法检查通过、65 个元素引用无缺失、HTML 标签配对无误、本地服务实测页面 200 + /api/market 与采样接口正常、量化与部署测试 64 passed；全部测试 176 passed。
- 版本同步：VERSION / 静态缓存引用 / 平台页顶栏文案 / desktop/package.json 四处同步到 0.3.136。

## v0.3.135 · 2026-08-10

- **网页研究一键入口**：新增无需安装扩展的 Bookmarklet，可把当前网页 URL、标题和选中文字带回 Workbench；选中文字放在 URL fragment，避免直接进入服务器请求日志，并在首轮分析、追问和 Artifact 来源中标记为未经核验的用户引用。
- **网页研究上下文持久化**：Crawl Run / Worker / 重试链路保留浏览器上下文，追问时也能继续引用当前选中内容；明确提示不要选择密码或 Token。
- **量化输入 fail-closed**：未知策略和无法识别的股票代码不再静默落到动量策略或“样本不足”，直接返回可解释的无效状态/API 400。
- **量化策略对比边界**：策略对比要求至少两个不同策略，重复策略直接返回 400，避免把单策略结果误报为对比研究。
- **飞书配置可见性**：云开发页显示工作区、飞书应用凭据和回调校验三项就绪状态；兼容带/不带 `WORKBENCH_` 前缀的飞书环境变量，避免配置名称不一致导致静默不可用。
- **版本同步**：本地候选版本 bump 到 0.3.135，同步 PWA、桌面壳和静态资源缓存版本。

## v0.3.134 · 2026-08-10

- **量化样本外验证**：新增不重叠 walk-forward API 与量化页入口；每折只在训练段选择回看窗口，再在未参与选择的测试段计算复合收益、买入持有基准、超额、回撤、正收益折比例和折间风险比率。
- **量化样本质量修正**：覆盖度按真实样本间隔计算，避免把 5 分钟快照误按日线覆盖要求判为低质量；继续明确不做未经说明的年化外推。
- **回归覆盖**：新增 walk-forward 参数边界、样本外折边界、API Artifact 和盘中样本质量测试。
- **部署运维**：deploy-workbench.sh 新增备份保留策略（WORKBENCH_KEEP_BACKUPS，默认保留最近 5 份），每次部署成功后自动清理旧备份，根治备份反复堆积撑磁盘的问题；本次部署顺带清掉线上 27 份旧备份中的 23 份（2.4G → 501M），磁盘回落至 22%。

## v0.3.133 · 2026-08-10

- **量化回测风险质量指标**：回测结果新增样本期 Sharpe、Sortino、盈亏比和平均单笔收益；明确不按年化频率外推不规则或分钟级快照，避免把快照研究误读成日频收益。
- **回测页面可读性**：深度研究结果区补充风险比率和盈亏比，继续保留买入持有基准、回撤、暴露、手续费与滑点假设。
- **验证**：新增量化风险指标回归；全量测试和发布门禁待本轮完成。

## v0.3.132 · 2026-08-10

- **量化历史样本采集**：量化页新增显式开启/停止开关，支持每 5 分钟、30 分钟、1 小时和每天四档固定周期；没有自选时拒绝开启，停用只停止后续采集并保留已有历史。
- **采样状态可追踪**：新增 `/api/market/sampling` 状态与控制接口，返回历史快照数、最近样本、最近运行、下次调度和失败原因；复用既有 `market_refresh` 固定自动化配方，不开放任意周期或 shell。
- **量化回测入口补全**：前端将采样状态、数据健康度和回测样本积累放在同一上下文中，并增加固定周期、无自选拒绝、停用保留历史的回归覆盖。

## v0.3.131 · 2026-08-10

- **网页研究 URL 校验拆分**：研究/抓取地址不再误用 LLM endpoint 的无 query 规则，支持搜索页和文章筛选参数；同时拒绝凭据、localhost、回环地址和显式私网 IP，降低公开 Crawl Worker 的内网访问风险。
- **云开发输出脱敏补强**：除普通 `token=` / `Bearer` 文本外，JSON 风格的 `access_token`、`api_key`、`password`、`authorization` 等字段也会隐藏值；脱敏入口对非字符串输出安全转换，并增加回归覆盖。
- **网页研究安全与可靠性收口**：证据卡片只允许 `http`/`https` 来源作为可点击链接，拒绝危险协议和带凭据地址；本机 Gemini Companion 请求增加超时和用户可读的超时提示。
- **云开发异常收口**：固定配方发生未预期异常时，Run、WorkItem 和事件时间线统一落为失败，不再让飞书已报错但后台记录长期停留在运行中。
- **飞书签名重放窗口收口**：签名模式校验 `X-Lark-Request-Timestamp` 的 5 分钟新鲜度；VERIFY_TOKEN 模式保持原有兼容，过期签名在进入解密/业务处理前拒绝。
- **Gemini Companion 边界收紧**：状态接口也校验允许的 Workbench Origin，避免任意本机网页触发来财 helper 的状态检查。
- **Gemini Companion 异常脱敏修复**：helper 配置/执行失败时，异常对象先转为文本再脱敏，避免错误处理路径再次抛出 `TypeError`；新增回归覆盖。
- **Gemini Companion 回退路径回归**：增加来财 Swift 内嵌 helper 提取与落盘权限测试，避免真实 helper 尚未生成时开关静默失效。
- **发布缓存同步**：递增 Workbench、PWA、桌面壳和静态资源版本号，确保本次前端修复不会被旧 Service Worker 或查询参数缓存遮蔽。

## v0.3.130 · 2026-08-09

- **修复所有项目「问AI」面板的滑动 Bug**（用户："滑动有bug"）：根因 = 面板外层与消息区嵌套滚动互相串扰，触屏/滚轮在消息区滚到边界后会连带滚动整块面板、甚至带动页面背景一起跳动。
  - 面板与消息区、最近运行列表、时间线等所有滚动容器加 `overscroll-behavior: contain`，滚动在各自区域内收敛，不再传导到页面背景。
  - 面板打开时锁定背景滚动（`body[data-agent-panel-open]`），关闭时恢复；Escape 键也能关闭面板。
  - 面板打开增加轻量弹出动画（`agent-panel-pop`，尊重 `prefers-reduced-motion`）。
  - AI 热点、想法分析两个内嵌聊天日志同步加滚动隔离。
- **「问AI」交互体验统一优化（所有项目）**：
  - 输入框 Enter 发送、Shift+Enter 换行（浮动面板 + AI 热点 + 想法分析三个入口统一，输入法组词不误触），占位提示同步说明。
  - 等待回复时消息区显示「三点呼吸」思考动画（正在读取项目上下文并执行工具…），成功后随消息重绘消失、失败时移除并显示错误气泡。
  - 消息区由固定 200px 改为弹性 38vh（桌面约 340px，窄屏 34vh），聊天可视区更大。
- **版本同步**：VERSION 文件此前停在 0.3.128（落后 0.3.129 一版），本次统一 bump 到 0.3.130，并同步刷新所有静态资源的 `?v=` 缓存引用。
- **网页研究示例链接修正**：Tabbit 示例改为官方 `https://www.tabbit.com/`，移除已失效的 `tabbit-ai.com` 地址。
- **量化历史排序修正**：历史快照不再按带时区偏移的时间字符串排序，统一按解析后的 UTC 时间排序，并补充跨偏移回归测试。
- **飞书回调幂等**：新增事件收据表，按 `event_id`（旧事件回退 `message_id`）保留 7 天，避免飞书重试重复执行云开发动作或重复创建审批。
- **量化回测样本补全**：回测、策略对比和成本敏感性分析显式纳入当前快照；同一数据时间由当前快照覆盖历史镜像，不降低样本质量评分。
- **发布与升级边界回归加固**：部署脚本抽出可测试的服务模板渲染函数，回归验证自定义 `--target` 会同步替换 `WorkingDirectory`、`EnvironmentFile`、Worker 路径和 `WORKBENCH_CLOUD_WORKSPACES`；新增旧 SQLite v4 升级测试，确认历史数据保留、飞书事件收据表创建和 schema version 升至 5。全量本地回归 `150 passed, 4 warnings`。
- **PWA 与新增页面移动端验收补齐**：ASGI 直接提供带 `Service-Worker-Allowed: /` 的 `/static/sw.js`，避免本地/非 Nginx 入口注册根作用域时报错；移动端回归覆盖网页研究、云开发和具身智能页面，17 页 × 4 尺寸共 68 组合零溢出、零 JS 错误。新增 Workbench 状态与 Companion HTTP 边界回归后全量为 `152 passed, 4 warnings`。

## v0.3.129 · 2026-08-09

- **线上 Git 项目中心支持查看本机（Mac）项目**（用户："git 那个扫描没扫到"）：根因 = 线上跑在服务器，git 中心扫的是服务器目录（0 个仓库），Mac 项目扫不到。
  - 新增 `/api/git/inventory-push`：Mac 本机扫描 git 清单推送到线上（请求头 `X-Workbench-Token` 匹配服务器 .env `WORKBENCH_GIT_PUSH_TOKEN`，nginx 对该路径免 Basic Auth），存 `data/git-inventory-remote.json`。
  - `get_git_repositories` 合并显示：本机（服务器）扫描 + Mac 推送清单，前端项目卡标注来源徽章（服务器 / Mac）。
  - 新增 `deploy/push-git-inventory.sh`：本机一键扫描并推送（token 存 `.workbuddy/git_push_token`，0600）。
  - 实测：推送 7 个项目（example-miniprogram/harness/example-app/example-voice/NewProject/Ehr/example-career-2.0），线上 `/api/git/repositories` 返回合并清单（remote_machines=[Mac]）。
- **git 扫描递归化**（用户："应该不止这几个"）：`_find_git_repos` 递归 max_depth=3（跳过 node_modules/.venv/dist/build 等依赖目录），覆盖嵌套项目（hotel/example-app、小游戏/example-game/NewProject、AI家政/代码/example-miniprogram），实测本地 4 → 7 个。
- **量化页首屏折叠**（用户："还是整不明白"）：根因 = 自选编辑区 458px 高占满首屏，用户看不到下方最有价值的今日行情 / Agent 观察。改造：编辑自选 / 行情健康度 / 深度研究工具三个区块默认折叠（`.market-collapsible` details，summary 给人话标题 + 提示），首屏聚焦三步引导 + 今日行情 + Agent 观察。
- 已部署线上 v0.3.129，5 服务 active。

## v0.3.127 · 2026-08-09

- **具身智能 Agent 真实性与分类修复**（用户："具身智能的 agent 是假的？项目没加到分类里？"）：
  - Agent 是真实的（5 个真工具：crawl_fetch/knowledge_search/knowledge_write/work_items_read/notify，走总调度 ReAct 机制），问题出在**首页分类**：分组按钮硬编码列表（favorite/all/organize/produce/discover/monitor）没有 research，且 embodied 的 group 设成了不存在的 research → 只在"全部"里。
  - 修复：embodied group → **discover（发现研究）**（与 Crawl4AI/AI热点/想法/CID 同组，实测分组计数 5）；`icons` 加 **robot 图标**（之前 icon=robot 无映射显示空白）。
- **场外基金行情真正接入**（用户："场外基金的问题不能解决了？"）：不再"提示不支持"——新增 `fetch_fund_nav`：腾讯接口查不到的场外开放式基金（如 110022）走**东财基金净值接口**（api.fund.eastmoney.com/f10/lsjz 取最新净值 DWJZ + 日涨幅 JZZZL + 净值日期 + pingzhongdata 取基金名 fS_name），fetch_market_quotes 对缺失代码自动补净值行情（source=fund-nav）；suggest 恢复展示场外基金（现在有数据兜底）。实测：110022 易方达消费 → 净值 2.922 / -0.17% / 2026-08-07。
- **量化估值因子自动获取**（用户："深度研究工具还是得优化"）：腾讯行情 88 字段原生含 PE（fields[39]）/ PB（fields[46]），fetch_market_quotes 一并带回（pe/pb 字段）；估值表单新增「**自动获取 PE/PB**」按钮——从已加载行情自动填入 PE/PB 并记录来源，核对后保存；实测茅台 PE 19.79 / PB 7.03。
- 验证：首页 11 项目含具身智能（robot icon）、发现研究分组计数 5 含 embodied、估值自动获取按钮就位；移动端 56/56 零溢出 0 JS 错。
- 已部署线上 v0.3.127，5 服务 active。

## v0.3.126 · 2026-08-09

- **蓝橙混搭清理**（用户："蓝+橙真丑"）：全站搜出橙色残留——浅橙边框/底色硬编码（#f2cbbb/#f0c6b4/#f0c9b5/#f1cbb7/#fff1e9）、橙色 hover（#c85624）、sub2api 页主色（#ed8159/#3a2925/#5d3b31）、rgba 橙透明度，全部替换为蓝色调（浅蓝边框 #cfe2fb / 深蓝 hover #2f6ed8 / 浅蓝底 #e6f1ff），sub2api 主色也改为蓝；--orange 变量名保留但值为蓝。涉及 platform.css/workbench.css/sub2api.css。
- **基金行情不返回修复**（用户："添加了基金没返回行情"）：根因 = 场内 ETF/指数腾讯接口可查（v_ 前缀），**场外开放式基金（如 110022）查不到**（返回 v_pv_none_match）。修复：①新增 `market_symbol_queryable` 判断（沪 6/5/000 开头、深 0/3/15/16/39 开头、北交所 4/8 可查；11x/12x 等场外基金过滤），suggest 结果不再展示查不到的代码；②行情区对自选里查不到的代码显示"⚠ xx 暂不支持行情（场外基金或代码有误）"提示，不再"加了没反应"。
- **量化深度研究工具引导优化**（用户："深度研究工具玩不明白"）：深度工具区标题下加"什么时候用"导读（回测=验证策略历史赚不赚 / 策略对比=两套规则哪个强 / 估值因子=记录 PE/PB/ROE / 日报周报=自动小结）；回测表单加「一键示例：验证 600519 的追涨策略」按钮（自动填参数并运行，先看到结果长什么样）。
- **Git 项目中心 + GitHub 工具目录使用引导**（用户："git 怎么用 / GitHub 工具目录不好用"）：两个平台页 hero 加"怎么用"引导条（git：打开自动扫描→看分支/未提交/最近提交→处理未提交改动；github-tools：①接现成服务配置集成 ②找新工具登记试用 ③有用帮你接入）。
- 验证：移动端 56/56 零溢出 0 JS 错；首页 accent 计算值 #3b82f6；git/github-tools 引导条、回测示例按钮、行情缺失提示全部生效。
- 已部署线上 v0.3.126，5 服务 active。

## v0.3.125 · 2026-08-09

- **全局主色调改浅蓝**（用户："主色调改浅蓝色"）：`--accent` 从橙（#ee704d/#f97316/#d9653e）改为浅蓝（浅色主题 #3b82f6 / 深色主题 #60a5fa）；accent-soft 底色橙 → 蓝（浅底 #e6f1ff / 深底 #14233c）；全部橙色硬编码（hover #ff9f43、边框 #7a4a27、兜底 #e06c3d/#e56d32 等）统一替换；保留红涨绿跌语义色（#c7534f/#238b72）与 teal/violet/green 辅助色不动。涉及 project.css/theme.css/styles.css/workbench.css/platform.css/project-agent.css/project-shell.html。
- **AI 热点增加国内数据源**（用户："数据源增加一些国内的"）：默认源从 2 个（aihot.today + Hacker News）扩到 5 个，新增 IT之家 RSS（www.ithome.com）、开源中国 RSS（oschina.net）、36氪 feed（36kr.com），全部服务器直连可达；新增 `_aihot_relevant` AI 关键词过滤（人工智能/大模型/智能体/强化学习/机器人/芯片/VLA 等中英文），避免国内科技媒体混入纯硬件/数码新闻；实测 5 源抓取 51 条（IT之家 8、开源中国 17、HN 18 等）。
- **新增「具身智能学习」项目**（用户："加一个具身智能的项目，学习和实践，做还是复用？"）：**方案 = 新建学习 hub 页 + 复用现有基础设施**（不重复造 Agent 轮子）：
  - 新页面 `/projects/embodied`：学习路线（5 阶段：Python+数学 → 机器学习/深度学习 → 强化学习 → 具身核心概念 → 实操框架）、资料清单（Spinning Up/Palm-E/LeRobot/Isaac Lab/RT-1 等）、实践方向（仿真/操作学习/VLA/低成本硬件）、研究入口（POST /api/crawl/plans 交 Crawl4AI）、笔记沉淀（POST /api/knowledge 带"具身智能"标签）、最近沉淀列表。
  - `projects.json` 加入口（id=embodied, accent=blue, icon=robot, group=research）；`/api/projects` 已验证返回。
  - AGENT_REGISTRY 注册「具身智能学习 Agent」（kind=research），`SUBAGENT_TOOL_MAP["embodied"]` = crawl_fetch/knowledge_search/knowledge_write/work_items_read/notify（复用现有工具），总调度 children 加入。
- 验证：具身智能页 200、5 阶段/双表单/10 资料项、无溢出无 JS 错；移动端回归 56/56 零溢出；`/api/projects` 含 embodied。
- 已部署线上 v0.3.125，5 服务 active。

## v0.3.124 · 2026-08-09

- **首页待办"试用"卡片死循环修复**（用户："点试用卡片又打开首页，死循环"）：`github_tool_trial` 工作项的 href 原来是 `/#activity`（回首页待办）→ 死循环。修复：点击跳到 GitHub 工具目录（`/github-tools?tool=<id>`），该页支持 `?tool=` 定位并高亮对应工具卡片（2.6s 高亮），让用户知道"这条待办 → 去看这个工具"。
- **量化页名称模糊搜索**（用户："输入名称模糊匹配，不局限 A 股"）：自选表单新增「搜索添加」输入框，输入名称/代码（如 茅台/新能源/510300）→ debounce 320ms → 结果下拉 → 点击追加到自选。后端新增 `/api/market/suggest`：东财 suggest 优先（A 股/指数/基金，过滤 Classify/SecurityTypeName）+ 新浪 suggest 兜底（A 股名称最准），返回 symbol/prefixed/name/kind。
- **支持基金/ETF**（用户："增加基金"）：`normalize_market_symbol` 修正 5 开头 → sh（沪 ETF，原来会误判为 sz）；腾讯行情接口本身支持场内 ETF（sh510300/sz159915 已验证同格式返回）；基金/ETF 直接输 6 位代码即可查询；搜索标注「基金/ETF」「指数」「A股」类型。
- **数据自动抓取填充**（用户："数据自动抓取填充"）：名称搜索选中即填入代码；保存自选自动刷新行情（v0.3.122 已有）；搜索项点击后提示"点保存自选自动拉取行情"。
- 验证：搜索接口线上实测（茅台→600519、沪深300→指数、新能源→指数+A股、110022→易方达基金）；量化页搜索 UI 生效；移动端回归 56/56 零溢出 0 JS 错误。
- 已部署线上 v0.3.124，5 服务 active。

## v0.3.123 · 2026-08-09

- **全部子 Agent 升级为真 function calling（ReAct 循环）**（用户："所有的子 agent 全部升级"）：
  - 新增 `SUBAGENT_EXTRA_TOOLS`（9 个子 Agent 专属工具）：`inbox_triage`（复用 analyze_inbox_record 确定性分类）/ `knowledge_write`（write_knowledge_note 沉淀）/ `doc_validate`（材料完整性校验）/ `doc_template`（模板列表）/ `crawl_fetch`（httpx 单页抓取+文本提取，15s 超时）/ `market_analyze`（因子分析）/ `idea_read`（想法会话）/ `cid_read`（机会卡）/ `aihot_feedback`（来源分反馈）。
  - 新增 `SUBAGENT_TOOL_MAP`：10 个子 Agent 各自的工具清单（全局工具 + 专属工具，共 41 个 schema）。
  - `call_child` 改造为 ReAct 循环：子 Agent 带自己的工具 → LLM 输出 tool_calls → `asyncio.to_thread(execute_react_tool)` 真执行 → 结果回传 → 直到无 tool_calls 输出最终 6 段总结；每步工具调用 `agent_tool_call` 留痕。
  - 子 Agent 并发限制 `asyncio.Semaphore(2)`：多个子 Agent 同时跑工具循环会放大 LLM 调用次数触发 Provider 429，串行化后更稳；429 仍由既有失败隔离兜住。
- 实测：dispatch"帮我看看服务器"→ server Agent 自己调 server_status（磁盘 16%/50G/趋势 13%→16%）再出报告；dispatch"Sub2API 额度"→ sub2api Agent 调 sub2api_status（余额 $0、本周 $244.24/$300 已用 81.4%、预计 1-2 天耗尽）；工具调用留痕确认（agent_tool_call 事件）。
- 已部署线上 v0.3.123，5 服务 active。至此总调度 + 10 个子 Agent 全部是真 function calling：LLM 决策 → 工具真执行 → 真实数据 → 总结。

## v0.3.122 · 2026-08-09

- **项目中心（GitHub 工具目录）引导优化**（用户："集成还需要 key？什么 key？登记试用干嘛？登记后呢？"）：
  - 集成配置面板每个敏感字段（token/api_key/api_token/access_token）加"去哪申请"提示：GitHub PAT → GitHub Settings → Developer settings；ntfy → 管理面板；Miniflux → Settings → API Keys；Zotero → 开发者页面；Linkding/Paperless/Vikunja/Wallabag 各自的 API Token 位置；未列出的 token 类给通用说明。
  - 登记试用成功提示改为引导：告知"已登记为工作台待办 → 去首页「现在要处理」查看 → 按卡片「试用」建议体验 → 觉得有用告诉我帮你正式接入"。
- **量化选股页交互优化与新手引导**（用户："没明白怎么玩"）：
  - hero 区新增"三步开始"引导条：①填股票代码（或点示例）→ ②保存自选（自动拉行情）→ ③看 Agent 观察信号与观察任务。
  - 自选表单新增「试试示例自选」按钮：一键填入 600519/000001/300750（茅台/平安/宁德）。
  - 保存自选后**自动刷新行情**（原来保存后还要手动点刷新，新手容易卡在这一步）。
  - 信号区标题人话化："观察信号"→"Agent 观察信号"，小字解释 趋势=最近走势 · 波动=价格起伏 · 活跃度=成交热度。
- 验证：量化页引导条/示例按钮/信号标题生效，集成面板 GitHub token 帮助生效；移动端回归 56/56 零溢出 0 JS 错误。
- 已部署线上 v0.3.122，5 服务 active。

## v0.3.121 · 2026-08-09

- **飞书双回复去重**（用户："为什么飞书回复会发两条？"）：dispatch 结果由 `run_dispatch` 回发"✅ 完成"，同时 v0.3.116 把 success 级"总调度已完成"通知也推飞书——两条内容重复。修复：`schedule_feishu_notification` 回退为只推 critical/error/warning 告警级，success 级不再推（飞书发起的结果已由回发覆盖；页面通知保留）。
- **飞书回发截断优化**（用户："第二条内容被截断"）：回发 `clip(result_text, 1800)` → 4000 字符，超出时追加"……内容较长已截断，完整结果可在工作台「最近活动」查看"提示；LLM 侧 v0.3.117/118 已修复 max_tokens 截断（usage 确认无 truncated）。
- **卡片"标记已读"真正生效**（用户："已读点击没反应，干啥的？"）：dismiss 按钮 value 带上 `notification_id`，回调时调 `mark_notification_read` 真把应用内通知标已读，按钮文案"已读"→"标记已读"。注意：回调要生效仍需在飞书开放平台订阅 `card.action.trigger`（卡片回传交互）事件并发布版本（当前回调全部是 im.message.receive_v1，未收到过卡片回调）。
- 已部署线上 v0.3.121，5 服务 active。

## v0.3.120 · 2026-08-09

- **自动化规则编辑入口**（用户："自动化规则那些不能编辑吗？"）：此前列表只有启停/运行/删除，改规则只能删了重建（后端 PATCH API 存在但前端未用）。新增：规则卡片「编辑」按钮 → 复用创建表单填充（标题变"编辑自动化规则"、按钮变"保存修改"、"取消编辑"按钮）；提交走 `PATCH /api/automations/{id}`。修复健壮性坑：`required` 校验会拦截 submit（project select 的 options 来自能力图节点，若规则项目不在节点中 select 值为空 → 表单无法提交），`fillAutomationForm` 自动补缺失 option。
- **GitHub 工具目录"已接入"可见性**（用户："已接入的在哪看？"）：工具卡片右上角一直有"已接入"绿标，但缺汇总。页面计数改为「已接入 X · 共 Y」（hero 区 tool-count），一眼看到已接入数量（实测线上"已接入 8 · 共 10"）。
- **文档同步 v0.3.113-119 七段补记**（用户："文档跟上了吗？"）：CHANGELOG 补 v0.3.113-119（首页布局演进/飞书 A+I/截断修复/模型能力表/ReAct）；v0.3.112 段补磁盘备份瘦身、双隧道、飞书事件解析、PWA 版本号回退、CID 样式修复；ITERATIONS/STATUS-AUDIT/UNFINISHED-CHECKLIST/LLM-CONFIGURATION 等 10 文档同步到 v0.3.120。
- 验证：移动端回归 56/56 零溢出 0 JS 错误；github-tools 统计正常。

## v0.3.119 · 2026-08-09

- **总调度 Agent 升级为真 function calling（ReAct 循环）**：用户点破"现在的 Agent 还是像跟 LLM 对话"（把上下文塞进 prompt 让 LLM 写总结，工具声明只是文字）。一步到位实现：新增 `REACT_TOOLS` 工具注册表（9 个可执行工具：`server_status`/`sub2api_status`/`knowledge_search`/`inbox_read`/`inbox_capture`/`work_items_read`/`aihot_read`/`market_read`/`notify`），每个 = OpenAI function schema + 同步 handler（直接调现有业务函数拿真实数据）。
- **`react_gather_evidence(message, parent_run_id, max_rounds=4)`**：LLM 带 tools 跑循环 → 输出 `tool_calls` → `asyncio.to_thread(execute_react_tool)` 真执行 → 结果回传 → 继续，直到 LLM 无 tool_calls 输出最终证据；`call_llm_with_tools` 用 `llm_provider_state()["candidates"]`（含真实 api_key，注意 `llm_settings()` 的 providers 是脱敏的不含 key）、`tool_choice=auto`、max_tokens 按模型能力表。
- **dispatch 接入**：并发调子 Agent 前先收集 ReAct 工具证据，注入子 Agent prompt 与最终汇总，让子 Agent 基于真实查询结果回答，不再凭快照猜。
- 实测：问"帮我看看服务器怎么样" → LLM 输出 `tool_calls:[server_status]` → 真执行 → 基于真实数据生成证据。
- 已部署线上 v0.3.119，5 服务 active；待飞书实测。

## v0.3.118 · 2026-08-09

- **max_tokens 跟随模型能力**（用户："LLM 的输出最大不应该跟着模型来吗？"）：新增 `MODEL_OUTPUT_TOKEN_LIMITS` 模型输出能力表（deepseek 8192 / gpt-5、4o 16384 / claude 64000 / qwen、glm、kimi、doubao 8192-16384 等，前缀包含匹配取最长），`model_output_token_limit(model, requested)` 返回 `min(请求值, 模型上限)`，未命中保守 4096、下限 256；`call_llm` payload 用 `effective_max_tokens`。
- 效果：DeepSeek 能用满 8192 不再白截断；未知模型保守 4096 不爆 API；之前"输出卡在 1801"不再发生。
- 已部署线上 v0.3.118。

## v0.3.117 · 2026-08-09

- **想法分析"失败"排查**：3 条 failed 全是 8-08 联动验收故意制造的 failure 证据（evidence_acceptance），非真实事故；真实业务（idea_chat 26 次成功等）全部正常。
- **Agent 输出截断修复**（用户发现输出可能被截断）：`llm_usage_events` 有 6 条 output_tokens=1801 正好卡 `max_tokens=1800` 上限（finish_reason=length）。修复：`call_llm` 默认 max_tokens 1800→4000；10 处显式调用提升（1800/2000→4000，1400/1500/1600→3000）；`call_llm` 检测 finish_reason=length 时记录 `status='truncated'` / `error_kind='max_tokens_exceeded'` 告警，不再静默。
- 已部署线上 v0.3.117。

## v0.3.116 · 2026-08-09

- **飞书 A：交互卡片**：`feishu.py` 新增 `build_action_card`（schema 2.0：config/header/elements，按钮带 value）+ 卡片发送；通知推飞书改用卡片，带【查看详情】（open href）【重试】（retry_automation，自动化失败时从 event_key 提取 rule_id 调 `execute_automation_rule`）【已读】（dismiss）按钮；`/feishu/event` 新增 `card.action.trigger` 回调分支 `handle_feishu_card_action`。
- **飞书 I：快捷命令**：`feishu_quick_command` 支持 `/help` `/今天` `/服务器` `/额度` `/新机会` `/热点`（+英文别名），摘要函数复用现有读取函数；实测 `/额度` 显示 codex $244.24/$300。
- **首页"最近活动"菜单入口 + 弹窗**（用户澄清："最近活动移到左边=菜单入口，点击弹窗可关闭"）：侧栏「平台工具」区新增「最近活动」按钮（platform-nav-item + green dot），点击打开可关闭弹窗（右上角 × / 点遮罩），渲染 `/api/trace/recent?limit=40`；loadTraceCenter/setupTraceCenter 全部改 modal 版。
- 已部署线上 v0.3.116；待用户配置 `card.action.trigger` 事件 + 发布版本后卡片按钮回调生效。

## v0.3.115 · 2026-08-09

- 首页「最近活动」由侧栏内嵌折叠块改为**侧栏菜单入口 + 可关闭弹窗**（上一版 v0.3.114 的迭代，用户原意是"菜单入口点击弹窗"）；侧栏删除内嵌 details，平台工具区加「最近活动」按钮，弹窗复用 audit-modal 样式，40 条记录按时间倒序。

## v0.3.114 · 2026-08-09

- 首页布局按用户澄清回滚中间双栏：删除 `home-columns` 双栏结构，"最近发生"改为**侧栏底部内嵌折叠块**（sidebar-trace，紧凑列表 8 条，默认展开）；移动端 960px 以下侧栏隐藏时不展示。

## v0.3.113 · 2026-08-09

- 首页「最近发生」从底部单列移入**左侧双栏**（左栏 300px 时间线 sticky，右栏待办 + 项目入口）；移动端 960px 以下回退单列。该布局在 v0.3.114 按用户原意（"移到左边=侧栏菜单"）调整。

## v0.3.112 · 2026-08-09

- **飞书机器人接入（全云端入口）**：新增 `feishu.py` 模块 + `/feishu/event` 事件订阅路由。手机飞书 IM 给机器人发消息 → 服务器复用总调度主 Agent（`dispatch_agent_task`）执行 → 结果回发飞书；支持 challenge URL 验证、可选签名/加密校验；首次对话自动记录 chat 绑定（`feishu_bindings` 表）。
- **通知接飞书**：warning/error 级应用通知自动推送到已绑定飞书会话（`schedule_feishu_notification`），与 ntfy 并列作为通知出口；凭据走服务器 `.env`（`WORKBENCH_FEISHU_APP_ID/SECRET`，0600），不进代码仓库。
- 新增 `/api/feishu` 状态接口（configured / bindings / 校验配置脱敏展示）。
- Nginx 新增 `location /feishu/` 免 Basic Auth 转发（事件回调由飞书服务器调用；真实性靠应用层校验）。
- 已部署线上 v0.3.112：`/feishu/event` 免认证可达、challenge 验证通过、configured=true；5 服务 active。
- 待用户侧：飞书开放平台配置事件订阅 URL（`https://workbench.example.dev:8765/feishu/event`）+ 开通 im 权限 + 发布应用版本。
- **磁盘暴涨修复（v0.3.112 同步）**：`/var/backups/workbench` 78 个部署备份累积 23G——备份脚本 `rsync_excludes` 漏了 `desktop/node_modules`（921M）+ `desktop/dist`，单个备份 1.1G。修复：排除规则加 `desktop/node_modules/ desktop/dist/ desktop/release/ .understand-anything/`；清理旧备份只留最近 3 个。结果磁盘 62%→22%（释放 20G），新备份 1.1G→99M（code 3.1M + persistent 96M）。
- **Web Push 与 embedding 双隧道修复（v0.3.112 同步）**：浏览器推送走本机 SSH 反向隧道（15236）连 Google FCM，隧道断导致全部推送 failed；embedding 隧道（15237）也断（知识库向量检索降级）。手动拉起两条隧道后推送恢复（sent）、embedding 服务健康（bge-small-zh-v1.5）。launchd 自启因 bootstrap I/O error 未生效，需重启 Mac 后 RunAtLoad 恢复。
- **飞书事件解析修复（v0.3.112 内）**：真实回调无顶层 `type` 字段（新版 schema 2.0 只有 `header.event_type`），且 `chat_id` 在 `message.chat_id` 而非顶层 `chat`——两处解析修正后用真实 payload 验证通过；dispatch 回发修复 `request.context` NameError（被外层 except 静默吞导致自动回发丢失）。
- **PWA 版本号回退修复（v0.3.112 内）**：本地 `VERSION` 文件停在 0.3.107（bump 只改 static/ 漏了 VERSION），`/api/meta` 读 VERSION 覆盖静态缓存。修复 `VERSION` → 0.3.112。
- **CID 页面样式修复（v0.3.112 内）**：project-shell.html 全局 `a, button {}` 选择器污染 evidence-panel（12px 字体 + 7px padding 撑坏"机会与进展"标题）；改为 `.bar a/.bar button/.evidence-head button` 局部选择器；iframe 高度 78vh→60vh 避免把证据面板挤出首屏。

## v0.3.111 · 2026-08-09

- **首页通知跳转修复**：点有目标页面的通知卡片 = 直接打开对应项目并自动标已读（原来只展开详情，用户"点了没反应"）；自动化失败通知 href 从 `/`（首页，无意义）改为 `/automation`（自动化中心看失败详情）；无目标页面的纯提醒仍展开详情。
- **各项目 Agent 入口统一**：Sub2API（`.topbar .actions`）和 CID（`.bar .bar-right`）也注入顶部「问这个项目的 AI」按钮，与 7 个标准项目页一致；AI热点/想法页（内嵌 Agent 面板）加顶部锚点按钮，点击滚动聚焦到 Agent 聊天区；不再出现"5 页有顶栏按钮、2 页只有悬浮球、2 页无入口"的不一致。
- **总调度弹窗简化**：删除"这次要达成什么"意图下拉（普通用户看不懂），改为纯自然语言输入 + 可选"想让哪个项目优先处理"；dispatch 请求 intent 置空由 Agent 自动提炼。
- **自动化失败自动重试**：可恢复错误（LLM 冷却/网络/超时/5xx/rate_limit）时自动补跑，最多 2 次、间隔 45/90 秒，失败通知文案改为"失败将重试"；`WORKBENCH_DISABLE_AUTOMATION_RETRY` 可关闭。
- **收件箱低风险自动归档**：分类置信度 ≥0.9 的纯记录（无截止时间、无交接路由、无重复）自动完成归档，metadata 记录 auto_archived 原因。
- **服务器容量趋势预测**：基于最近 5 条历史快照的磁盘/内存使用率线性趋势，估算"按当前增速距提醒/临界阈值还剩几天"；服务器页新增"容量趋势"卡片；历史样本不足时明确提示，不做无依据预测。
- **AI 热点来源分细化**：从"基准3+简单±1"改为按来源历史有用率加权（样本 ≥5 用有用率 1-5 分映射，样本 2-4 用折中公式，样本少回退基准），新增 source_sample_size 字段。
- **量化单日异动提示**：最新价格样本较前一记录涨跌幅 ≥5% 时单独生成"单日异动"观察任务（与区间趋势区分）。
- **Sub2API 额度策略建议**：预测结果新增 suggestions（剩余 ≤2 天/≤5 天/够用时分别给出"暂停低频任务/减少研究/提前备好备用 Provider"等可执行建议），页面新增建议卡片。
- **CID 机会卡自动刷新**：保存新看板快照时，已登记机会的项目状态/名称有变化 → 自动更新机会卡描述 + metadata（signal.status 等）+ 通知；新函数 refresh_cid_opportunity_status。
- **知识库检索命中率评估**：新增 `/api/knowledge/evaluation`，用 6 条内置查询对比关键词 vs 混合检索命中数，报告语义检索带来的"词面零重合"额外命中。
- 移动端回归 56/56 零溢出，Python/JS 语法全绿；已部署线上 v0.3.111。

## v0.3.110 · 2026-08-09

- **导航统一**：project-shell 返回文案统一为「← 返回工作台」；4 个平台页品牌标识英文 PLATFORM CONTROL 中文化为「平台工具」，automation 能力图 small 中文化。
- **项目 Agent 入口显眼化**：有 `.page-actions` 的项目页（inbox/knowledge/doc-factory/market/aihot/idea-analysis/server）在顶部操作区注入「问这个项目的 AI」按钮（原来只有右下角悬浮球，普通用户看不到）；无 page-actions 的页面保留悬浮球兜底。
- **首页「最近发生」默认展开**：`<details open>` + setupTraceCenter 立即加载，不再让用户看到空白折叠态。
- **待办卡片人话化**：「来源 → 目标」改为自然语言（"来自 Sub2API 账户 Agent，转给 收件箱 Agent"；同项目内显示"XX 里的待办"），弱化内部路由感。
- 移动端回归 56/56 零溢出，JS 语法全绿；已部署线上 v0.3.110。

## v0.3.109 · 2026-08-09

- **可用性人话化改造（普通用户视角）**：全站英文 eyebrow 中文化（WORKSPACE→工作台、TODAY/NEXT ACTIONS→今日待办、RECENT ACTIVITY→最近动态、AIHOT/SIGNAL LAYER→AI 热点研究、CAPTURE FIRST→先记下来、OPS/READ ONLY→服务器监控 等 17 处）；术语人话化（WorkItem→事项、Artifact→产物、Relation→关联、Run→执行记录、子 Run→子任务、handoff→交接→转交、Fallback→备用、Provider→模型服务、业务已验证→真实链路已验证、仅合成验收→内部测试通过、历史未分类→历史记录）；移除技术信息外泄（footer 本机路径、run id 前 8 位、projects.json 配置 JSON 示例）；空态引导从黑话改为"第一步做什么"（协作链空态、project-shell 空态、待办空态不再隐藏整块、doc-factory/idea-analysis 空态）；统一主题默认值（project.js 默认 dark 与首页一致）。
- 已部署线上 v0.3.109（5 服务 active）；移动端回归 56/56 零溢出，JS 语法全绿。

## v0.3.109 · 2026-08-09

- **移动端逐页回归（Playwright 320/768/1024/1440 × 14 页面 = 56 组合）**：修复 AI 热点页 320px 横向溢出（`.aihot-layout` / `.feed-list` 的 `1fr` 改为 `minmax(0, 1fr)`，防止 grid 内容撑宽；`.project-agent-handoff` 补 `min-width: 0` 并允许 small 收缩省略），回归后 56/56 零溢出。
- **修复 /crawl4ai 页 JS 崩溃**：`Identifier 'requestJson' has already been declared`——`index.html` 同时加载 app.js（顶层 `const requestJson`）与 project.js（顶层 `function requestJson`）全局重复声明。统一由 request.js 暴露 `window.requestJson`，project.js 不再重复声明。
- **LLM 故障切换实测通过**：临时给 primary（DeepSeek）设置冷却后调用 `call_llm`，fallback（gpt-5.6）成功接管并返回结果，usage 事件确认由 fallback 完成；测试后清理冷却状态恢复正常。
- 已部署线上 v0.3.109（5 服务 active），公开资源与内部健康检查通过。

## v0.3.109 · 2026-08-09

- 修复 Worker 健康检查对 `worker_status_payload()` 返回 **list** 的分支处理（stale 判定与告警创建），避免规则执行时把列表结构误当单对象处理；服务器已配置 worker_health_check 规则（id=7，every 86400s）并实测 4 个 Worker 0 stale。
- 本轮与 v0.3.106 一起上线：真实联动证据 25/25 全覆盖（business_verified 28 条，synthetic 保持 0），evidence 矩阵 success 判定 = work_item(done) + target run + work_item→run relation + notification，逐边通过真实 API + 内部 `run_project_work_item()` 触发，全部拿到 business_execution 证据。

## v0.3.106 · 2026-08-09

- 首页与审批相关入口新增**待审批徽标**：`/api/approval-queue` 驱动，待确认数量（审批 / 待确认工作项 / 待确认动作）显示在侧栏，点击直达审批与交付页。
- AI 热点**变化检测 + 来源评分**：`select_aihot_items` 对比快照 `previous_items` 标记 `change=new`（前端显示「新」徽标）；来源评分 `source_score = 基准3 + 用户反馈(useful +1 / not_useful -1)`，保留 `source_votes`；排序纳入来源分与变化权重（`ranked_key`）。修复循环中 `item=dict(item)` 不写回列表导致字段丢失的 bug。
- **LLM Key 安全加固**：新增 `_llm_key_from_environment(provider_id, name)`，支持 `WORKBENCH_LLM_KEY_<ID>` / `WORKBENCH_LLM_KEY_<NAME>` / `WORKBENCH_LLM_KEY` / `WORKBENCH_LLM_KEYS`(JSON) 四种环境变量注入；`normalize_llm_providers` 在 Key 为空时从环境注入并标记 `key_source=saved/environment`，Key 可不再进入 `llm_settings.json` 明文。
- 新增 `worker_health_check` 自动化类型：检查 Worker 心跳，stale 时创建 alert work_item + Web Push 通知；类型目录同步登记。

## v0.3.105 · 2026-08-09

- Sub2API 告警**恢复闭环**：`evaluate_sub2api_alerts` 由单向创建改为双向比对（active_keys 集合），账户恢复正常后旧的告警 work_item（如 stale、weekly_low）自动 done（`resolved_by=sub2api_health_recovery`），不再长期挂 open 占用待办；返回 `restored` 列表。实测账户恢复后自动恢复 2 条旧告警，待办 4→2。
- LLM 双 Provider 冗余（用户配置第二个 fallback），单 Provider 冷却事故（rate_limit）后具备故障切换冗余。

## v0.3.104 · 2026-08-09

- 线上发布前补齐联动证据历史分类：旧记录明确标为 `legacy_unclassified`，不再被误读为业务已验证；新的真实业务对象链可以覆盖旧历史/合成记录，分类过程幂等。
- Sub2API 自动同步区分“面板凭证被拒绝”和临时网络失败；凭证失效时暂停后台周期重试，保留快照与错误摘要，重新登录后恢复；页面补齐连接状态首次加载和恢复文案。
- Observer 对服务重启窗口的传输中断做一次受限重试，减少把短暂连接切换记录成整组空状态；仍只保存脱敏摘要。
- 首页联动卡片新增“历史未分类”状态，明确显示业务证据待补；新增对应回归覆盖。
- 本轮不启动本地 Workbench 端口；发布后继续以线上认证业务验收和真实联动证据为准。

## v0.3.103 · 2026-08-09

- 联动审计不再把合成验收对象链显示成“已验证”；只有标记为 `business_execution` 的真实业务对象链才显示业务已验证，页面同时展示合成验收与业务证据数量。
- Crawl4AI 稳定性观察、队列和研究计划改为独立加载；观察接口暂时不可用时，不再遮蔽仍可用的研究队列和计划，并提供分区重试入口。
- 新增联动证据、Crawl 观察、AI 热点机会复盘、CID 复盘的回归覆盖；本地全量回归达到 114 条。
- 本版本完成代码与本地验收，发布前仍需线上版本核对、认证业务验收和长周期观察；不启动本地 Workbench 端口。

## v0.3.102 · 2026-08-09

- 自动化超时的 `queued` Run 会在读取接口中保守恢复为可重试的失败；不强制结束真实 `running` Run，并保留恢复原因。
- Agent 能力卡区分“近期无运行 · 历史有失败”和“仅配置未运行”，同时提供最近失败和运行来源入口，避免把历史失败统计误读为当前状态。
- 文档工厂新增“学习笔记/知识卡片”和“决策记录”模板，缺失材料明确标注待补充，不把推测写成事实。
- 测试入口增加项目根路径配置；本地全量回归为 110 条测试通过。
- 本版本只完成代码与本地验收，尚未发布线上；线上继续保持 v0.3.101，且不启动本地 Workbench 端口。

## v0.3.101 · 2026-08-09

- Worker 状态同时显示最近成功时间，并区分“最近失败”和“历史失败（已恢复）”；过长错误信息会截断，避免历史失败掩盖当前已恢复状态。
- 自动化中心的失败统计改为读取真实 Automation Run，移除硬编码的失败恢复数字。
- Sub2API 手动粘贴脱敏快照成功后，同时刷新账户数据和连接状态，避免页面继续显示旧的自动同步失败。
- 修复公开发布验收脚本在 Basic Auth 场景下的控制流，并新增认证拦截回归测试。
- 本地全量回归为 106 条测试；版本、静态资源缓存、PWA Service Worker 和 Electron 壳统一到 v0.3.101。

## v0.3.100 · 2026-08-09

- 修复真实线上 320px 验收发现的移动端横向溢出：GitHub 工具目录卡片标题容器允许收缩，量化研究工具区在窄屏改为单列，观察任务卡不再撑开页面。
- `VERSION`、静态资源缓存、PWA Service Worker、Electron 壳和桌面发布产物统一到 v0.3.100；继续保持线上优先，不启动本地 Workbench 端口。
- 保留线上 v0.3.99 的 Observer、五类 Worker、SearXNG/Wallabag 和首条运行观察证据；本版本发布后重新做线上静态资源和移动端回归。

## v0.3.99 · 2026-08-09

- `v0.3.99` 已正式发布线上：五个核心服务健康检查通过，公网工作台 HTML、GitHub 工具页和 Service Worker 均为 v0.3.99；部署备份、失败回滚和 Chromium 运行文件安装均保留。
- 新增 `workbench-observer.service/.timer` 与 `deploy/observe-workbench.sh`：每 15 分钟只读记录 Worker、Push、Embedding、Sub2API 和 LLM 的脱敏摘要，形成一周稳定性观察证据，不执行重启或外部写入。
- 首条线上观察已成功写入，四个 Worker 均为 idle，Push 已配置且有 1 个订阅，Embedding 混合召回为 0.933；Sub2API 最近一次自动同步失败，已保留为真实恢复待办。
- 新增 SearXNG 和 Wallabag 只读效率集成：搜索结果/稍后读文章可人工选择进入网页研究，保留来源、数据时间和稳定幂等 ID，不复制第二套搜索或文章主库。
- GitHub 工具目录补充真实项目的配置成本、数据边界和下一步；集成表单补齐 Access Token、搜索词、分类和结果类型文案，移动端保持单列可操作。
- 本地全量回归继续通过；Electron ARM64 未签名 app/DMG 已生成，第二个真实 LLM Provider 仍需维护者提供独立凭据后在线上实测，不能用占位配置代替。

## v0.3.98 · 2026-08-09

- 新增 `deploy/verify-public-release.sh` 只读线上版本核对：同时检查公开工作台页面、Service Worker 和 `/api/meta`，明确区分版本不一致与 Basic Auth 拦截，不发送认证信息、不启动本地端口。
- API 与独立 Agent / Crawl / Monitor / Sync Worker 统一读取可选的 `-/www/workbench/.env`，为 LLM Secret 迁移提供一致的进程环境；缺少 `.env` 时继续兼容 `data/llm_settings.json`。
- 新增 `deploy/audit-credentials.sh` 只读 Secret 权限审计；只报告来源和权限状态，不输出或搬运真实 Key。
- GitHub 工具目录集成卡片改为展示“已配置 / 尚未测试 / 连接成功 / 测试失败”等可判断状态，去掉重复的下一步说明；远端条目增加全选/取消全选，减少批量导入操作。
- 同步当前架构、Agent 路线、联动矩阵、待办、部署和 LLM 配置文档；新增部署脚本与前端交互回归覆盖。
- 本地版本、静态资源缓存、桌面壳和 PWA 门禁统一到 v0.3.98；线上仍需发布后用认证入口验收。

## v0.3.97 · 2026-08-09

- 新增 Vikunja 只读效率集成：读取未完成任务、项目、标签和截止时间，人工勾选后导入收件箱；不回写、删除或自动完成第三方任务，保留来源 ID、更新时间和 Relation。
- Agent 结果契约补充来源覆盖摘要：区分来源类型，并展示可定位来源与带数据时间来源的覆盖情况，帮助快速判断是否需要复核。
- 收件箱待办的“忽略”操作会自动打开下一条待办；没有下一条时给出明确反馈，减少重复返回首页的操作。
- GitHub 工具目录从硬编码的效率集成说明改为读取后端数据边界与下一步文案；Vikunja 的配置成本、隐私边界和人工导入路径会直接显示在页面。
- 本地回归、静态资源和桌面壳版本统一到 v0.3.97；线上仍需发布后用认证入口验收。

## v0.3.96 · 2026-08-09

- 工作台 WorkItem 新增保守的“下一步质量”契约：明确区分下一步清楚、需确认范围和需补下一步；主动协作建议与首页待办卡片共享同一判断，不从长描述臆测可执行动作。
- 对齐 Agent 能力注册表与状态矩阵：文档工厂多轮审批、想法分析结构化访谈、CID 跨来源比较和个人偏好学习不再被列为未完成缺口；剩余项改为线上联动验收、评分历史评估和长周期观察。
- 增加桌面/PWA 发布前静态门禁：校验 Electron 安全边界、线上地址、版本一致性、Manifest 图标和 Service Worker 缓存版本；本地回归增至 93 条并全部通过。
- 修正 `VERSION` 文件不可读时的后端版本回退值，避免异常环境误报旧版本。
- 想法分析新增结构化访谈录入：参与者、具体问题、原话/回答、来源和证据状态可单独保存；访谈 Artifact、Relation 和证据包回放保持可追溯。
- 修复想法证据面板按错误字段过滤导致记录不显示的问题；证据包把 `unverified` 正确归入“待验证”，并增加访谈/指标计数。
- 补齐访谈表单的焦点、错误、保存中、成功反馈、Escape 关闭和移动端布局；新增 API、Artifact、Relation、证据包和页面契约回归覆盖。
- 静态资源缓存、桌面壳和状态文档版本统一到 v0.3.96；线上仍需发布后用认证入口验收。

## v0.3.95 · 2026-08-09

- Crawl4AI 结果契约补齐来源回放：保存内容 hash、来源质量、数据时间和命中行号；首轮分析与多轮问答使用同一组可回放来源引用，变化检测随持久化 Run 一起保存。
- 联动证据矩阵区分 `synthetic_acceptance` 与 `business_execution`，同时记录 WorkItem / Run / Relation / Notification 对象链、版本和验收时间，避免历史合成验收记录冒充当前线上业务证据。
- AI 热点、想法分析和 CID 比较统一展示来源可读性、内容 hash、数据新鲜度、来源质量和个人偏好命中；想法指标补齐可回读 Artifact。
- 文档工厂补齐“审批轮次 → 修订 → 新版本 → 新交付审批”闭环，审批 payload 保留 round、当前/上一版本、修订和父审批关系，页面展示下一轮动作。
- 知识库草稿写入 Obsidian 前校验来源仍可读且 hash 未变化；来源变化会阻断写入并提供检查结果，成功写入继续保留确认时间、目标路径、来源和备份。
- 本地回归由 83 条增至 88 条，全部通过；线上仍需认证后发布和验收。

## v0.3.94 · 2026-08-09

- Sub2API 登录状态严格区分可续期凭证、短期访问令牌和浏览器书签同步；没有 `refresh_token` 时不再提示“开始自动同步”，密码仍不落盘。
- Sub2API 页面共享首次账户/趋势读取 Promise，并防止内联脚本与 Agent 脚本重复绑定风险评估按钮；加载失败保留可重试状态。
- 收件箱交接 WorkItem 保留 Agent 生成的下一步建议及其来源；知识库冲突报告增加段落级行号证据和合并草稿定位；量化研究任务保存来源、新鲜度和历史快照质量。
- 知识库新增段落稳定 ID、保留左/右、合并、忽略四种人工处置，生成可审阅 Artifact、来源回放和独立 API；原始 Obsidian 笔记不自动改写。
- Sub2API 浏览器书签同步增加客户端快照 ID、超时、轻量重试和重复请求去重；页面补充登录、点击、等待、失败重试四步兜底说明。
- 新增 Linkding 和 Paperless-ngx 只读集成：Token 请求头、条目标准化、来源时间、人工勾选导入和幂等跳过；GitHub 工具目录补充配置成本、数据边界和下一步。
- 新增相应回归覆盖；本地单元测试当前为 83 条。线上仍需认证后发布和验收，当前公开 Service Worker 仍为 v0.3.83。
- 本轮继续统一 15 个页面的请求超时、认证/限流/服务故障文案和恢复入口；项目 Agent 初始化失败可直接重试，能力矩阵与状态文档同步到知识库段落处置、文档引用覆盖基础检查和 AI 热点推送的真实完成边界。

## v0.3.93 · 2026-08-09

- 行情页统一异步反馈：主行情、研究任务、日报/周报历史和回测历史读取失败会显示原因和重试入口；保存、刷新、研究、回测和策略对比按钮统一显示忙碌状态。
- 收件箱→知识库交接新增临时数据库回归测试，验证 `source_inbox_id`、`inbox_handoff_note` 和 `captured_as_knowledge` Relation 保留来源链。
- 新增 `LLM-CONFIGURATION.md`，说明严格 Provider 候选规则、环境变量最后兜底、systemd `EnvironmentFile` 和 Secret 人工迁移边界；不自动处理真实凭据。
- 本地回归由 70 条增至 71 条；静态 JS、HTML 内联脚本、Python 编译、部署脚本语法和重复 ID 检查通过。线上仍需认证后发布和验收。

## v0.3.92 · 2026-08-09

- 补齐工作台产品闭环：明确决策可落成 Artifact 与后续 WorkItem，主动协作计划只有确认后进入 Agent Worker 队列。
- 知识库草稿写入 Obsidian、行情研究结论沉淀和服务器动作审批增加服务端确认边界；重启/日志读取继续保持人工执行，低风险只读检查保留 Run、执行日志和本地快照回退。
- 收件箱分类统计新增准确率、按类别 Precision/Recall/F1 和 Macro-F1；Sub2API 额度预测与快照差异解释拆分独立状态区域，避免重复 DOM ID 串位。
- 补充 Sub2API 书签同步 Origin 安全边界、量化回测契约、知识草稿确认和服务器高风险动作回归测试；本地单元测试由 59 条增至 70 条。
- 统一版本与静态资源缓存到 v0.3.92。线上仍需认证后发布和验收，不能把本地代码视为线上已发布。

## v0.3.91 · 2026-08-09

- 对齐收件箱、知识库、Sub2API 和量化研究 Agent 的注册状态、已实现能力与剩余缺口，避免页面把已完成能力误报成待做。
- 文档工厂增加可选 Microsoft MarkItDown 适配器：可增强 PDF、DOCX、XLSX、PPTX、HTML 转 Markdown；未安装时继续使用内置解析器，不阻断现有流程。
- GitHub 工具目录明确区分“已接入 / 候选 / 可选依赖”，将 MarkItDown、ActivityWatch、GitHub Issues/PR、Zotero 的实际连接边界、安装状态和隐私边界展示出来。
- 项目 Agent 发送、重试和交接按钮补充 `aria-busy` 状态；新增状态一致性与文档解析回退回归测试。
- 收件箱分类统计接入页面：展示确认样本量、修正数、分类分布和样本不足提示；GitHub 工具目录补充路由级隐私边界与试用 WorkItem 回归测试。
- Sub2API 增加按 Provider/分组的脱敏成本汇总；知识库检索自检显示有效样本是否足够；量化回测显示数据源稳定性提示。
- 本地单元测试当前为 57 条；线上仍需发布后用认证后的 `/api/meta` 和页面硬刷新验收，不能把本地代码视为线上已发布。

## v0.3.90 · 2026-08-09

- Agent 工具边界改为 fail-closed：未知或不属于目标 Agent 能力声明的工具在调用前直接拒绝，不再只记录为 `rejected` 后继续调用；多目标调度按子 Agent 能力裁剪工具计划。
- 收口上一轮尚未登记的 Agent 执行计划、文档工厂结构化修订重点/验收标准、来源与审批重新读取、移动端 Agent 面板和无障碍状态反馈。
- 新增 Agent 工具边界回归测试；本地单元测试当前为 44 条，15 个 HTML 页面中的 9 个内联脚本、全部静态 JS、Python 编译和部署脚本语法检查通过。
- 线上仍保持只读验收边界：当前线上版本仍为 v0.3.83，未将本地代码写成已发布。

## v0.3.89 · 2026-08-09

- 修复只读健康检查脚本的默认等待参数：文档默认值 `--wait 0` 现在可以正常通过参数校验；部署脚本的非零等待行为保持不变。
- 平台页状态消息在错误时使用 `role=alert` 并立即播报；异步按钮补充 `aria-busy`，让失败和处理中状态对键盘/辅助技术更明确。
- 新增部署脚本回归测试；本地单元测试当前为 37 条。

## v0.3.88 · 2026-08-09

- Agent 结构化结果新增执行计划回放信息：计划状态、目标、路由置信度、工具约束、人工确认边界和子 Run 数量在工作台与项目 Agent 中可见；没有执行计划的旧结果仍兼容。
- 首页项目健康卡补充待处理、待确认、失败、运行中数量，以及真实数据来源和数据时间，减少“需要关注”但不知道下一步做什么的问题。
- 新增执行计划契约和首页健康摘要回归测试；本地单元测试当前为 36 条。

## v0.3.87 · 2026-08-09

- 接入 ActivityWatch：支持读取近 7 天数据桶和事件聚合，只保留事件数量、总时长和数据时间，不保存窗口标题、网页 URL 或原始事件内容；可人工选择导入工作台效率观察 WorkItem。
- GitHub 工具目录补充效率集成的下一步、隐私边界和导入后动作；ActivityWatch 作为第五类效率入口，不要求 Workbench 启动本地端口。
- 命令面板补充键盘选中状态、`aria-selected`、焦点恢复和 Tab 焦点边界；平台集成条目补充数据时间和聚合隐私提示。
- 新增 ActivityWatch 聚合回归测试；本地单元测试当前为 34 条。

## v0.3.86 · 2026-08-09

- 外部集成导入的 WorkItem 统一暴露来源类型、原始链接、来源更新时间和下一步；收件箱与目标 Agent 都能直接回到原始来源。
- Miniflux / Zotero 补齐统一来源更新时间和条目类型元数据，GitHub Issue / PR、订阅文章、研究条目沿用同一来源契约。
- 收口 LLM 前端重复实现：`llm-settings.js` 成为唯一配置入口，首页、工作台和项目页不再各自维护 Provider 渲染、测试、保存和指标逻辑。
- 新增来源回溯回归测试；本地单元测试当前为 32 条。

## v0.3.85 · 2026-08-09

- GitHub 工具目录新增 GitHub Issues / Pull Requests 只读集成：支持公开仓库免 Token 读取，也支持可选 Token；保留 Issue/PR 类型、标签、作者、来源 URL、来源更新时间和稳定幂等 ID。
- 支持人工勾选 GitHub Issue / PR 导入收件箱 WorkItem，重复导入自动跳过，并在页面明确显示目标项目和下一步。
- 集成配置页补充仓库字段占位提示、来源更新时间和 Issue/PR 类型；新增 GitHub 集成与路由级脱敏/幂等回归测试。


## v0.3.84 · 2026-08-09

- GitHub 工具目录接入 ntfy、Miniflux、Zotero：支持配置、连接测试、读取条目和人工选择导入 WorkItem；密钥只返回脱敏状态。
- ntfy 接入高优先级工作台告警的异步转发；失败不阻断主业务，并保留本地送达记录和重试入口。
- Push 状态补充私钥来源、公钥、代理和最近失败原因；Crawl Worker 部署统一 Chromium 发布目录，并兼容 Chromium 与 headless-shell 两种可执行文件布局。
- 修复首页 PWA 安装引导重复绑定且无法从 `hidden` CSS 类显示的问题；现在只注册一次安装事件，并在安装/关闭后正确隐藏。
- PWA Shell 补入共享 LLM 脚本，并在离线回退时忽略资源版本查询参数，避免缓存命中旧入口却缺少配置面板。
- 集成 API 增加路由级回归覆盖；本地单元测试当前为 30 条。线上仍需发布 v0.3.84 后再验收。

## v0.3.60 · 2026-08-08

- LLM 配置规则与实际调用链继续统一：页面提示改为“全局 LLM”，所有静态入口和 PWA / Electron 资源缓存版本统一到 v0.3.60。
- 补齐收件箱、AI 热点页面遗漏的静态资源缓存参数，避免线上硬刷新后仍使用旧样式。
- 保持线上验收边界：日常只使用 `https://workbench.example.dev:8765`；公网接口当前仍需认证，未把本地检查写成线上验收。
- 待办文档改为区分“代码完成 / 线上发布 / 线上验收”，并重新筛选后续外部集成候选。

## v0.3.59 · 2026-08-08

- 修正知识库“待确认写入 Obsidian”列表过滤：普通 `knowledge_note` 不再误显示为待审阅草稿，只展示明确的来源/段落/交接草稿或 `review_required` Artifact。
- 统一 `VERSION`、静态资源缓存参数、PWA cache、Electron 壳和状态文档版本。
- 本地验证通过；线上仍需认证后发布验收。

## v0.3.57 · 2026-08-08

- Crawl4AI 执行从 API 进程内临时任务切换为独立 SQLite 队列 + `workbench-crawl-worker.service`，支持原子领取、租约、取消、失败重试和失效租约恢复。
- stale 恢复增加 Worker 实例归属判断，长时间运行的 Crawl 不会仅因超过时间窗口被重复执行；部署前同时编译 API、Worker，并由健康检查验证两个 systemd 服务。
- `/api/system/architecture` 和 Worker 状态改为读取真实租约/心跳，区分 `unclaimed`、`idle`、`running`、`stale`，不再把静态“ready”当成已运行。
- 统一静态资源与 PWA 缓存版本到 v0.3.57；线上日常入口继续使用 `https://workbench.example.dev:8765`，不启动本地端口。

## v0.3.58 · 2026-08-08

- LLM 路由边界修正：环境变量 fallback 进入真实候选链；保存重复名称时生成唯一 Provider ID。
- 连通性测试不再污染正式 Provider 健康状态或触发限流冷却；新增对应单元测试。
- LLM 地址规则统一：接受 API 基地址或完整 `/chat/completions` 地址，拒绝 URL 用户名、密码和片段；主配置、保存顺序中的 fallback、环境变量最后兜底与页面提示保持一致。
- 首页、Crawl4AI 和项目页三套配置入口统一携带 `provider_id`，重复名称不会串测保存条目。
- Agent 运行结果补充来源标记与路由置信度，项目 Agent 面板和首页总调度增加 Run 事件回放入口。
- 新增可复用的证据比较/交接、想法证据包、AI 热点/CID 机会复盘、CID 偏好和行情估值 API；部分入口仍需页面接线。
- 部署与健康检查纳入 `workbench-sync-worker.service`，覆盖编译、systemd、备份、启动、回滚和四服务健康状态。
- 统一静态资源缓存版本、Electron 壳版本和线上入口说明为 v0.3.58；不启动本地工作台端口。

## v0.3.56 · 2026-08-08

- 修复工作台主首页全局 LLM 弹窗未显示运行指标的问题，与 Crawl4AI 配置入口统一显示近 24 小时成功率、延迟、Token、估算成本、Provider 分布和失败类型。
- 统一线上验收口径：日常入口继续使用 `https://workbench.example.dev:8765`，不启动本地工作台端口；版本与当前代码、PWA 缓存和文档同步到 v0.3.56。
- 保留真实证据边界：25 条项目联动边仍需线上逐条产生对象链，未因配置入口或静态关系自动标记完成。

## v0.3.55 · 2026-08-08

- LLM 配置规则收口：Provider 稳定 ID、留空保留 Key、显式清除、主配置 → fallback、健康状态和调用指标（Token / 成本 / 延迟 / 失败类型）统一落库；删除条目增加确认。
- Agent 运行底座增强：Worker SQLite 租约与心跳、多实例防重复执行、统一指标接口、Crawl 持久化队列、研究计划 API、来源质量启发式、同 URL 内容变化检测。
- 工作流补齐：收件箱分类反馈学习、想法验证结论写入知识库、验证任务到期提醒、CID 证据回溯和 AI 热点新增/消失资讯回放。
- UI/交互收口：结构化 Agent 结果可折叠、项目卡片主体点击、收件箱次要操作收进“更多”、一键处理文案明确人工回到工作台继续下一条、拖拽支持 Home/End；静态资源版本统一到 v0.3.55。
- 线上入口保持 `https://workbench.example.dev:8765`；线上功能验收和 25 条联动边真实证据仍需继续完成，未用静态入口冒充完成。

## v0.3.54 · 2026-08-08

- 全局 LLM Provider 规则收口：稳定 ID、留空保留 Key、显式清除 Key、无 Key 条目可见但不参与调用；主配置失败按 fallback 顺序继续，并记录健康/错误类别/429 冷却。
- 三套 LLM 配置入口提交 `provider_id` 和保留/清除语义，显示当前生效 Provider 与健康状态。
- Agent 公共聊天/调度结果增加 `result_contract`，统一结论、事实证据、判断、风险、动作和下一步字段，同时保留原始回答。
- 工作台新增 `/api/search`，顶部搜索覆盖项目、工作项和 Artifact 并可直接跳转。
- 日常入口、桌面壳默认地址和 Sub2API 浏览器同步脚本统一使用 `https://workbench.example.dev:8765`；版本升级为 v0.3.54。

## v0.3.50 · 2026-08-07

- 子项目全量优化（需求 7「每个子项目能怎么继续优化」全部落地）：
  - Sub2API：「检查风险」死按钮复活（调 alerts/evaluate）；额度趋势 delta 展示。
  - 量化选股：V3 回测研究面板（/api/market/backtest + 历史回测）；事件驱动研究表单（/api/market/research → 交接知识库）。
  - AI 热点：洞察面板（/api/aihot/insights 主题簇 + 来源质量分）。
  - 想法分析：验证工作台加「记录证据 / 回填指标 / 继续暂停转向」三件套（evidence/metrics/decision 端点接线）。
  - 收件箱：合并建议浮层（merge-suggestions，一键合并）；事件驱动研究入口。
  - 文档工厂：生成结果 Markdown 渲染预览；格式 KPI 动态化（读 templates）。
  - 知识库：从产物生成草稿（source-draft，需人工确认）。
  - 服务器监控：历史检查行可展开（磁盘/内存/负载详情）。
  - 首页：移除「仅本地运行」「本地运行」PWA「安装到电脑」提示；协作链移入侧栏入口 + 宽版弹窗；「现在要处理」空状态整体隐藏；blue-soft 加深统一 4 张蓝卡视觉。
- 自动化新增 8 种规则类型：inbox_daily_digest / knowledge_weekly_digest / market_daily_report / server_weekly_report / crawl_retry_failed / aihot_digest_daily / idea_task_reminder / cid_snapshot_audit。
- AI 热点数据源多源化：AIHOT_SOURCES 环境变量（默认 aihot.today + hnrss.org AI 关键词），并发抓取 + RSS/Atom 解析 + 统一去重，实测 117 条来自 13 个原始源。
- Playwright 验证 9 个页面 0 JS 错误；回测/风险评估功能实测通过。

## v0.3.49 · 2026-08-07

- 全量体检问题修复（27 项中修复/处理 22 项，3 项确认为误报，2 项留待产品决策）：
  - P0 崩溃：Sub2API 页面 EXTRACT_SCRIPT 未定义导致 JS 崩溃（删残留代码 + 书签 URL 改动态 location.origin）；Crawl4AI 依赖安装（0.9.2）；CID「测试 AI」空参数必 400（后端空参数回退已存配置）。
  - P1 功能：sub2api 同步循环 settings 为空时跳过、失败保留原快照（不再污染 status=error）；首页「/」快捷键聚焦搜索；响应式断点统一（min-width 1120→900 + 960px 侧栏折叠）；AI 热点徽标动态化（接入 /api/settings/llm）；收件箱分类补全（+研究/文档/告警）。
  - P2 打磨：doc-factory/server 升 implemented、market/cid gaps 刷新、server_thresholds_set 策略补全、project_activity 失败加 7 天窗口、LLM 主配置回退选择器修复、刷新按钮加载态、保存失败 error 样式、未读颜色统一为红、failed 底色统一、--display 死变量清理、sw.js 补 doc-factory 预缓存。
  - P3 文案/无障碍：品牌副标/本地标识/页脚文案统一中文、空状态文案修正、弹窗加 role=dialog+aria-modal+焦点陷阱、#project-note/#activity-note 加 aria-live、--faint 对比度提升。
- 误报澄清：Obsidian 集成实际可用（service 注入 vault 路径，79 篇笔记已索引）；页面标题格式、topbar 类名差异属设计意图。
- Playwright 无头验证：首页/Sub2API/AI 热点/收件箱 4 页 0 JS 错误，Sub2API 真实数据完整渲染（$244.24/$300 · 353 天 · 已登录）。

## v0.3.48 · 2026-08-07

- 首页项目卡片 UI 全面优化(P0 优先级):
  - 标题修复单行截断为 2 行 line-clamp,9 张卡片标题全部完整显示;
  - 健康状态行 span 补 min-width:0 + flex:1,修复 flex 容器内文本溢出卡片;
  - 摘要 label 补 min-width:0 + ellipsis,防止 value 数字挤压 detail;
  - 网格最小列宽从 250px 提到 290px,宽屏(≥1560px)300px,3/4 列自动适配;
  - 卡片 meta 行从 "{meta} · {agent_name}" 精简为只显示 meta,信息不丢(title 保留 tooltip)。
- 10 张卡片在 1360px 窗口(3 列)与 1680px 窗口(4 列)下均无任何溢出,主区呼吸感明显改善。
- 全部静态资源升级到 v0.3.48。

## v0.3.47 · 2026-08-07

- 修复 Sub2API 自动同步的四个问题，现在真实数据完整同步成功：
  1. 面板账号是普通用户（role=user）而非管理员 → 端点改为用户端点优先（auth/me、subscriptions/summary、keys、usage），admin 端点 403 不再阻断同步；
  2. 解析器按面板真实响应结构重写（{code,message,data} 包装、subscriptions[0]、keys.data.items、usage 明细按北京时间聚合今日/累计）；
  3. fetch 函数成功路径漏 return（返回 None 导致 502）已修复；
  4. 订阅数据 key 从 summary/active 兼容读取。
- 实测同步结果：余额 $0.00、周额度 $244.24/$300.00（剩余 $55.76）、今日 20 次请求 $1.26、2 个 API Key（脱敏），首页卡片「数据新鲜」。
- 全部静态资源与 PWA 缓存升级到 v0.3.47。

## v0.3.46 · 2026-08-07

- Sub2API 同步改为「连接面板」：在项目页输入面板账号密码登录一次即可，服务器保存登录态（refresh_token + access_token，密码不落盘）并每 30 分钟自动同步，之后无需任何操作。面板登录接口（/api/v1/auth/login）实测无需 Turnstile 验证。
- 新增 `POST /api/sub2api/panel-login`（email + password 登录面板换取 token）；`/api/sub2api/panel-settings` 返回 has_credential；自动续期逻辑（access_token 过期用 refresh_token 经 /api/v1/auth/refresh 刷新）已接通。
- 前端「连接面板」表单：账号/密码 + 登录并连接；已连接显示状态；立即同步 + 端点结果保留。
- 全部静态资源与 PWA 缓存升级到 v0.3.46。

## v0.3.45 · 2026-08-07

- Sub2API 同步升级为「自动同步」：服务器端用面板 Admin API Key 直接读取数据，无需任何浏览器操作。配置一次 Key 后，后台每 30 分钟自动同步一次（可经 SUB2API_AUTO_SYNC_INTERVAL 调整，0 关闭）。
- 探测确认面板真实 API 前缀为 `/api/v1`（SPA 路由返回 index.html 干扰），端点列表：/admin/dashboard/stats、/admin/dashboard/snapshot-v2、/admin/usage/stats、/subscriptions/summary、/subscriptions/active、/keys、/usage、/auth/me。
- 新增接口：GET/POST `/api/sub2api/panel-settings`（Admin API Key 存储，0o600 权限文件）、POST `/api/sub2api/sync-auto`（立即同步，返回各端点状态与解析结果）；鉴权失败（401/403/INVALID_TOKEN）明确报「Key 被面板拒绝」。
- 前端「自动同步」面板：粘贴 Admin Key 保存、显示配置状态、立即同步按钮 + 端点结果展示；浏览器书签/手动粘贴 JSON 折叠为高级选项。
- 全部静态资源与 PWA 缓存升级到 v0.3.45。

## v0.3.44 · 2026-08-07

- Sub2API 同步改为「书签一键同步」方案：在浏览器新建书签并粘贴提供的代码，之后在面板（sub.chengsir.asia）保持登录时点一下书签，脚本直接调用面板同源 API（/auth/me、/subscriptions/summary、/usage、/keys、/key-usage、/user）读取数据并提交到工作台，无需 F12 / 复制 / 粘贴。
- 新增 `POST /api/sub2api/sync-raw`：接收面板原始 API 响应，后端容错解析（兼容 data/subscriptions/items 等包装结构）为标准脱敏快照；带服务端 Origin 校验，非面板来源返回 403。
- 新增 CORS：仅放行配置的面板来源（SUB2API_PANEL_ORIGINS，默认 https://sub.chengsir.asia）对快照提交接口的 POST/OPTIONS。
- 保留「粘贴 JSON 手动提交」作为高级选项。
- 全部静态资源与 PWA 缓存升级到 v0.3.44。

## v0.3.43 · 2026-08-07

- 服务器监控改为支持「本地探测模式」：部署到服务器后监控目标即本机，不再依赖 SSH key（`server_target_is_local` 自动判定 localhost/本机地址；部署 systemd 默认 `WORKBENCH_SERVER=localhost`），刷新恢复正常。SSH 远程模式保留。
- 首页 Server 卡片修正：快照 `status=error` 时健康状态显示「检查失败」及原因（之前 health 误判 good）；卡片主数字改为磁盘使用率，明细展示内存与 Nginx 状态。
- Sub2API 新增「同步快照」面板：提供浏览器控制台提取脚本 + 粘贴 JSON 提交（POST /api/sub2api/snapshot），解决服务器部署后无法更新账户数据的入口问题。
- 首页 Sub2API 卡片修正：明细改为「周额度剩余金额」（之前误显示订阅剩余天数），数据过期时明确提示「需重新同步」。
- 首页项目卡片 UI：删除冗余的「进入项目」按钮（与主操作按钮同地址），主操作按钮改为通栏醒目样式。
- 全部静态资源与 PWA 缓存升级到 v0.3.43。

## v0.3.42 · 2026-08-07

- 文案优化：首页空状态不再引导编辑 projects.json；AI 热点空状态区分「未同步 / 无匹配 / 筛选无结果」；文档工厂模板描述补使用场景；服务器风险说明拆为「只读边界 / 可配置项」两段。
- 交互优化：量化日报/周报新增历史报告列表（可点击回看）；文档工厂生成后显示「下一步：交付审批」引导条；服务器阈值保存/恢复后自动刷新完整状态；Sub2API 趋势图数据点悬停显示数值与时间。
- 功能补全：收件箱新增「来源（可选）」字段并展示（支持来源回溯）；服务器 Agent 支持对话调整告警阈值（如「把磁盘阈值调到 90」）；全局 LLM 测试返回延迟（latency_ms），失败按认证/超时/限流/路径分类提示。
- 修复：文档工厂生成成功后交付审批按钮未启用（上一轮遗漏）。
- 新增 `/api/outputs/{name}/content` 接口用于读取历史产物（含路径穿越防护）。
- 本机 8765 服务停止，日常访问改走服务器 `https://workbench.example.dev:8765`；v0.3.42 已通过部署脚本推送服务器（健康检查全绿，备份 `deploy-20260807-192543`）。
- 全部静态资源与 PWA 缓存升级到 v0.3.42。

## v0.3.41 · 2026-08-07

- 项目卡片拖拽体验改进：整卡可拖、拖到末尾可放、边缘自动滚动、更清晰的落点指示。
- 收件箱新增重复条目合并：检测到重复后可直接合并到目标条目（内容带来源标记并入、源条目标记归档可恢复），并记录 Agent Run。
- 文档工厂审批闭环：页面新增“交付审批”按钮，生成 DOCX/PDF 交付包并创建审批请求；页面展示审批状态与修改意见，approvals 页面批准/退回会回写 Artifact。
- 量化选股新增日报/周报：基于本地快照和可解释因子用全局 LLM 生成报告，保存到 outputs/ 并登记 Artifact。
- Sub2API 新增额度趋势图（周/月剩余比例折线，纯 SVG 无外部依赖）。
- 服务器监控告警阈值可配置：磁盘/内存/负载的警告与严重阈值可在页面编辑保存，立即影响分析。
- AI 热点新增摘要与主题聚类：一键基于有用资讯生成摘要、聚类和关注信号，保存为 Artifact。
- 新增 `backup.py` CLI：backup / list / restore（恢复前自动创建 before-restore 安全备份并需确认）。
- 首页实时状态（最近 Run/待处理/健康度）复核通过并更新文档标记；修正“备用服务不可达”错误结论。
- 审阅并启用一键部署脚本 `deploy/deploy-workbench.sh`（push/apply、备份、回滚、健康检查、边界防护），已成功部署服务器至 v0.3.41。
- 修复部署脚本三个问题：`backup-workbench.sh` 在 `set -e` 下 `[[ -e ]] && rm` 短路导致备份失败；健康检查在服务重启后立即执行太早（新增 `--wait` 轮询等待端口就绪，默认 30s）；服务器缺 `nc` 时改用 bash TCP 兜底。
- 全部静态资源与 PWA 缓存升级到 v0.3.41。

## v0.3.40 · 2026-08-07

- 全局 LLM 配置改为多条目结构：每个条目独立配置名称、URL、模型和 Key，可设置主配置 / fallback 角色，每个条目可单独测试连通性；去掉旧的「主 Token/JSON 粘贴」和单条 fallback 表单。
- 后端 `llm_settings.json` 迁移为 `providers` 数组（旧格式自动转换，不丢已保存 Key）；`call_llm` 按主配置优先、fallback 按序降级。
- 修复 `call_llm` 对推理模型的兼容：`content` 为空时回退读取 `reasoning_content`，避免 deepseek 等推理模型的回复被误判为「返回为空」。
- 修复首页总调度弹窗「确认执行」按钮失效：事件监听选择器与按钮 `data-action-id` 不匹配，导致待确认动作无法确认。
- 修复项目偏好保存的防抖竞态：清定时器导致旧 Promise 永久挂起、多次 fetch 乱序覆盖，改为序号丢弃过期响应。
- 删除反向 SSH 隧道（`com.lifenghe.workbench-cockpit-tunnel` launchd 服务）：不再把本机 GPT 端口反向暴露到服务器。
- 首页、项目页、Crawl4AI 三处全局 LLM 弹窗统一为多条目界面；PWA 缓存与全部静态资源升级到 v0.3.40。

# v0.3.39 · 2026-08-07

- 新增 Workbench 可重复部署脚本：支持本机推送、服务器应用、发布前备份、失败回滚、健康检查和无联网 dry-run；持久化目录与密钥文件不会被源码覆盖。
- 部署加入酒店项目硬隔离：只允许独立 Workbench 服务、用户、目录和 Nginx 文件，拒绝酒店/PM2 标识及符号链接；Nginx 配置未变化时不 reload。
- 完成 Sub2API 与 Crawl4AI 在 320/768/1024/1440 视口的响应式回归，修复 320px 指标卡最小宽度导致的横向溢出。
- 批量完成 15 个页面入口和 38 个只读 API 的本地烟测；能力图返回 10 个 Agent 节点与 25 条联动边。
- 在隔离 SQLite 中验证执行计划的依赖顺序、失败暂停、按步重试、人工接管和最终汇总。
- 重新生成并渲染验收正式 `FDE模式行业观察与实践-v1.docx/.pdf`，保留审批入口和历史 Artifact。

# v0.3.38 · 2026-08-07

- 项目卡片支持拖拽排序，顺序保存到 `data/project_preferences.json`，不修改项目定义文件。
- 项目卡片支持一键加入/移出“常用项目”，同时提供键盘上下移动作为拖拽替代；保存失败会在首页明确提示。
- 继续保留 Cockpit `gpt-5.5` 主配置和 DeepSeek fallback；未将不存在的 `gpt-5.6 sol` 写入配置。

# v0.3.37 · 2026-08-07

- SQLite schema 创建与迁移改为进程内一次初始化，后续请求只建立轻量连接，降低首页和 Agent API 的首请求开销。
- 首页项目 API 合并待处理/知识笔记计数；开发者审计和联动关系改为展开“项目协作链与能力审计”时再加载，减少首屏请求。
- 页面资源和 PWA 缓存版本升级到 v0.3.37；保留现有配置、数据库、知识库和历史产物。

# v0.3.36 · 2026-08-07

- 全局 LLM 新增主配置入口，支持粘贴裸 Token 或常见 OpenAI 兼容 JSON；密钥只保存于服务端，不回显到页面。
- 普通 Agent 调用按“AI Token 主配置 → 当前 DeepSeek fallback”执行；主配置失败自动降级，测试按钮只测试当前主配置，不静默切换。
- 所有全局 LLM 配置入口统一显示主配置/fallback 状态，支持清除主配置；保留已有 fallback、数据库、知识库和历史产物。
- 升级页面资源和 PWA 缓存到 v0.3.36。

# v0.3.34 · 2026-08-07

- 首页增加“安装到电脑”按钮：仅在浏览器触发 `beforeinstallprompt` 后显示，点击后由浏览器完成 PWA 安装确认。
- 安装完成或用户取消后清理延迟安装事件，不重复弹出，不影响正常网页使用。

# v0.3.33 · 2026-08-07

- 修复旧 `/static/` Service Worker 先于新 Worker 缓存旧 Manifest 的问题：先注销旧注册，再安装根作用域 Worker。
- 确保 PNG 图标 Manifest 和根作用域 Worker 同时生效，避免浏览器继续读取旧 SVG 图标缓存。
- PWA 所需的 Manifest、PNG 图标、Worker 和静态资源改为公开读取；工作台页面、API、全局配置和业务数据仍由 Basic Auth 保护。

# v0.3.32 · 2026-08-07

- 修复 Service Worker 作用域只有 `/static/` 的问题，改为控制工作台根路径 `/`；Nginx 增加 `Service-Worker-Allowed: /`。
- 页面自动清理旧的 `/static/` Worker 注册并注册根作用域 Worker，解决 HTTPS 页面没有触发 PWA 安装入口的问题。
- Workbench 与酒店服务保持独立运行，既有配置、数据库、知识库和产物不变。

# v0.3.31 · 2026-08-07

- 修复桌面 PWA 安装条件：Manifest 改用 Chrome 可识别的 192x192 / 512x512 PNG 图标，保留原 SVG 图标作为网页图标。
- Service Worker Shell 缓存加入 PNG 图标并升级到 v0.3.31；HTTPS 工作台入口现在可以被浏览器识别为可安装应用。
- 不修改现有全局 LLM 配置、服务器 SQLite、知识库、自选股和历史产物。

# v0.3.30 · 2026-08-07

- 工作台部署为服务器独立服务：使用独立 `workbench` 用户、Python 虚拟环境、systemd 单元和 Nginx 入口，不与酒店项目的 PM2/3001 进程共用运行边界。
- 工作台服务器入口为 `https://workbench.example.dev:8765`，增加独立 TLS 和 Basic Auth；部署保留工作台 SQLite、全局 LLM 配置、知识库和历史产物。
- 修复 Python 3.11 不兼容的嵌套 f-string 写法，并让 CID 看板文件路径支持环境变量，保证本机与服务器均可启动。
- 收件箱交接继续保留 WorkItem、Agent Run、Artifact、Relation 和应用通知证据链；未覆盖既有版本产物。

# v0.3.29 · 2026-08-07

- 新增 `/api/project-audit` 项目能力审计：分开显示工具可用性、Agent 运行记录、数据新鲜度、最近失败和联动证据。
- 首页开发者视图新增“项目协作链与能力审计”，联动只有同时具备 WorkItem、Relation、目标 Run、应用通知才标记为已验证；仅配置不再伪装成已完成。
- 升级全部页面资源和 PWA 缓存到 v0.3.29；不修改既有配置、数据库、知识库和历史产物。

# v0.3.28 · 2026-08-07

- 工作台首页项目卡片接入真实活动状态：待处理、运行中、待确认和失败恢复不再只存在于数据库里。
- “现在要处理”纳入失败工作项；点击工作项会直达具体项目区域或自动打开项目 Agent，完成首页 → 项目继续处理链路。
- 项目页支持 `?focus=agent` 自动打开/定位 Agent；量化页补齐项目脚本缓存版本；PWA 与页面资源升级到 v0.3.28。

## v0.3.27 · 2026-08-07

- 想法分析 Agent Round 3：新增结构化假设库、计划版本、成功指标、停止条件、7 天验证任务和版本化决策基础；旧版本不会被覆盖，当前页面只展示最新计划。
- 验证任务自动创建收件箱 WorkItem、Hypothesis/WorkItem Relation 和应用内通知；AI 热点、CID 看板、Crawl4AI、知识库来源保留到想法会话。
- 新增验证工作台 GET/POST API；计划生成失败会留下失败 Agent Run，可从页面重试并保留 parent_run_id / attempt lineage；非 JSON LLM 输出使用保守 fallback。
- 想法分析页面补齐最近会话自动恢复、计划版本/判断/假设/任务/刷新/失败/重试交互；1280px 桌面回归无横向溢出、无控制台错误。
- 使用隔离 SQLite 验证 JSON、fallback、版本 1/2、409/502/503、失败重试、来源 Artifact Relation 和应用通知；PWA 与页面资源升级到 v0.3.27。

## v0.3.26 · 2026-08-07

- 文档工厂 Agent Round 3：生成结果新增“校验这份结果”，执行来源可读性、引用标记、敏感信息和内容完整性检查，并调用全局 LLM 做保守的事实/引用二次审查。
- 校验过程持久化 `agent_runs` / `agent_run_events`，生成版本化校验报告 Artifact，并建立被校验产物、来源和报告之间的 Relation；失败会记录失败 Run。
- 页面显示校验通过、待核实项或风险项；不自动改写原文、不发送外部内容，历史文档和来源保持不变；PWA 与页面资源升级到 v0.3.26。
- 使用隔离 SQLite 验证校验成功、来源丢失、敏感信息标记、报告 Artifact、来源 Relation 和失败 Run 路径。

## v0.3.25 · 2026-08-07

- 文档工厂 Agent Round 2：新增工作区 Artifact 选择器，可组合知识库、热点、行情、服务器和历史文档产物，不再只能粘贴一段文本。
- 生成前读取每份来源的可用性和失败原因；提示来源数量、合并风险和不可读取文件，不把未接入内容伪装成材料。
- 生成结果保存 `source_artifacts` 元数据，并为每份来源创建 `Artifact → Artifact used_as_document_source` Relation；旧文档、配置、数据库、知识库和产物均不覆盖。
- 文档工厂 Agent 上下文补充可引用 Artifact 清单；页面显示来源链、版本和项目 Agent 名称；升级 PWA 与页面资源到 v0.3.25。
- 使用隔离 SQLite 验证多来源成功、缺失来源失败、越界路径阻断、来源 Relation 和重复版本链路径。

## v0.3.24 · 2026-08-07

- CID 独立开发者看板 Agent Round 2：看板加载后将脱敏项目快照（来源仓库、数据时间、项目数量、状态和赛道摘要）持久化到 SQLite。
- 项目抽屉新增“交给想法分析 Agent”：明确选择后创建项目机会 Artifact、CID Snapshot → WorkItem Relation、Artifact → WorkItem Relation 和应用内通知。
- 机会登记按 `cid:<repo>:<project_key>` 去重；已登记查询、登记中、失败可重试状态在看板内可见。
- 更新 Agent 注册表、能力策略、审计清单、联动矩阵和 PWA 页面资源到 v0.3.24；保留既有配置、数据库、知识库和产物。
- 使用隔离 SQLite 验证快照、机会卡、关系、交接任务、通知和重复点击路径；当前真实数据库不写入虚构用户选择。

## v0.3.23 · 2026-08-07

- AI 热点研究 Agent Round 2：新增本地“有用 / 不相关”反馈，反馈只保存在工作台 SQLite，并参与后续信号排序。
- 明确点击“交给想法分析”后创建去重的机会 WorkItem，保存热点标题、摘要、来源、原文链接和发布时间。
- 热点信号登记 Artifact → WorkItem Relation，并写入应用内通知；重复点击复用已有任务，不重复制造通知和产物。
- AI 热点卡片补齐反馈、加入对话、交给想法分析、已执行、加载、失败和重试状态；页面加入 PWA Shell 缓存，资源升级到 v0.3.23。
- 想法分析 Agent Round 2：增加待验证机会收件区；领取热点机会后创建想法会话、生成结构化初判，并回写 WorkItem、Relation、Agent Run 和应用通知；失败可重试。
- 用隔离 SQLite 验证反馈持久化、反馈加权排序、机会去重以及 Artifact / Relation / Notification 闭环；不改动既有配置、数据库、知识库和产物。

## v0.3.22 · 2026-08-07

- 量化研究 Agent Round 2：从本地行情历史计算趋势、波动和成交活跃度因子；每个因子显示样本数、起止数据时间和缺失原因，样本不足时不做方向性判断。
- 增加去重的行情观察任务：因子达到研究阈值后，可生成 `WorkItem`，并登记行情快照 `Artifact → WorkItem` 关系和应用内通知；不生成买卖指令。
- 量化 Agent 对话支持明确“生成观察任务”时直接执行本地低风险动作；自选股、历史数据和原有产物保持不变。
- 量化页面补齐因子明细、研究任务、状态空结果和加载反馈；所有页面资源与 PWA 缓存升级到 v0.3.22。

## v0.3.21 · 2026-08-07

- 知识库 Agent Round 2：增加今日更新、MOC 数量和孤立笔记提示。
- 增加 Obsidian 双链/主题关联建议；只读分析基于本地索引，不修改原始笔记。
- 增加 Workbench Inbox → Obsidian `00 Inbox/` 的显式确认写入；每次写入登记 Artifact、Relation 和应用内事件，原收件箱内容保留。
- 增加知识库页面的沉淀候选区，明确 Agent 只能建议，不能静默写入 Obsidian；PWA / 页面资源升级到 v0.3.21。

## v0.3.20 · 2026-08-07

- 收件箱 Agent Round 3：新增优先级、过期提醒、按优先级/截止时间排序和每条事项的交接状态。
- 收件箱支持批量完成、批量归档、批量恢复和批量设置优先级；批量接口返回逐条成功/失败结果。
- 已归档视图作为可恢复回收站，日常整理不再依赖物理删除；保留原有数据库、配置、知识库、自选股和产物。
- 重新审计项目优化清单，修正量化行情和服务器监控已完成能力的旧标记；PWA / 页面资源升级到 v0.3.20。

## v0.3.19 · 2026-08-07

- 首页收敛为“待处理 + 项目入口”：项目协作链移入默认收起的开发者视图，不再占用日常主视图。
- 普通项目 Agent 增加按项目提供的快捷提问；AI 热点和想法分析增加“从这里开始”操作，减少空白对话。
- 服务器监控 Agent Round 1 完成：历史快照、新鲜度、磁盘/内存/负载阈值、必需服务判断、告警 WorkItem、恢复通知和只读风险边界。
- 服务器项目页改为“结论—数据状态—服务—告警/恢复—历史—主机信息”结构；Workbench inactive 不再作为必需服务故障。
- 保留已有全局配置、数据库、知识库、自选股和产物；PWA 缓存和前端资源升级到 v0.3.19。

## v0.3.18 · 2026-08-07

- 应用通知中心与桌面系统通知彻底分离：通知铃铛只打开工作台内的事件面板，不再出现“桌面推送已启用”旧提示。
- 通知详情改为“任务 / Agent 结果 / 动作状态 / 下一步”结构；单条通知点击后展开并标记已读，“全部已读”清除全部红点。
- 升级前端资源查询串、Service Worker 注册地址和缓存名，清除旧 PWA 页面残留；保留现有配置、数据库、知识库、自选股和产物。
- 量化研究 Agent Round 1 完成：行情历史快照、数据新鲜度、报价质量保护、可解释观察和历史列表已接入；过期、无自选、无有效报价分别显示正确状态。

## v0.3.17 · 2026-08-07

- 修复总调度 Agent 直接返回路径仍显示原始 Markdown 的问题，统一使用结构化结果、去重动作和清晰状态。
- 总调度完成或动作确认后立即刷新应用内通知，不必等待下一次轮询；通知中心明确只记录工作台事件，不弹系统通知。
- 增强总 Agent 弹窗的点击遮罩关闭和 Escape 关闭；升级 PWA 缓存版本，避免旧版“桌面推送已启用”残留。
- 不覆盖既有配置、数据库、知识库、自选股和产物。

## v0.3.16 · 2026-08-07

- Crawl4AI 页面接入统一项目 Agent 面板，保留原有抓取/研究问答界面，并补充能力边界、最近运行、待接收交接、失败重试和项目联动入口。
- Crawl4AI Agent 上下文改为合并 SQLite `agent_runs` 与当前运行态；服务重启后仍可读取最近任务、来源页面、分析状态、Artifact、WorkItem 和失败原因。
- 修复深色 Crawl4AI 页面上的 Agent 面板可读性、能力摘要挤压和底部联动入口被截断问题；空的交接/运行区域自动收缩。
- PWA 缓存和 Crawl4AI 资源升级到 v0.3.16；不覆盖既有配置、数据库、知识库和产物。

## v0.3.15 · 2026-08-07

- 总调度 Agent 结果改为结构化 Markdown 展示，压缩弹窗高度并避免把原始 Markdown 与动作状态重复展示。
- Agent 动作按标的和工具去重，明确显示“已加入 / 已在自选中 / 待确认 / 失败”等可执行状态。
- 应用通知补回旧通知关联的调度结果和动作上下文；通知中心只保留应用内记录，移除旧版底部 Toast。
- 升级工作台前端资源与 PWA 缓存版本，不覆盖既有配置、数据库、知识库和产物。

## v0.3.14 · 2026-08-07

- 应用通知中心收敛为“结果、动作、告警、联动”四类应用内事件，不再使用“桌面推送已启用”这类误导提示。
- 通知详情补充来源 Agent、事件类型、发生内容和下一步；红色数字只代表未读数量，点开单条或“全部已读”才会清除。
- 总调度改为在完成/失败后写入结果通知，避免只记录任务创建而看不到执行结果；动作执行结果也会进入通知中心。
- 升级 HTML、JS、CSS 和 PWA 缓存版本，不覆盖既有配置、数据库、知识库和产物。

## v0.3.13 · 2026-08-07

- Sub2API 账户 Agent 完成 Round 2：接收脱敏浏览器快照，区分余额、周/月额度、Key 用量和订阅剩余时间。
- 增加数据新鲜度、关键字段校验、历史快照和失败降级；页面明确显示数据是否较旧或已过期。
- 增加额度偏低、订阅临期、未登录和同步失败风险评估，可生成收件箱 WorkItem、Relation 和应用通知；充值、删除 Key 等外部动作仍不自动执行。
- 补充项目联动矩阵，区分 24 条“已配置联动边”和当前数据库中已验证的真实链路；不再把页面跳转算作完成。
- 升级版本和 PWA 缓存，不覆盖既有配置、数据库、知识库和产物。

## v0.3.12 · 2026-08-07

- 应用通知中心改为清晰的“全部 / 未读”视图，红点只代表未读条数；点开单条或点击“全部已读”即可清除。
- 通知详情改为 Agent 来源、事件类型、发生内容、时间和项目入口的结构化信息，长文本只显示摘要，不再把原始输出直接铺满页面。
- 增加应用内通知遮罩、关闭按钮、外部点击关闭和明确的“不是桌面推送”说明，移除旧版本提示残留风险。
- 升级 HTML、JS、CSS 和 PWA 缓存版本，不覆盖既有配置、数据库、知识库和产物。

## v0.3.11 · 2026-08-07

- 文档工厂 Agent 增加通用报告、会议纪要、PRD、周报/简报、行动清单 5 种模板。
- 增加生成前材料检查：产物名、材料、处理要求、模板、材料长度和来源文件名都会给出可解释状态/提醒。
- 产物保存为不覆盖历史的 `v1/v2` 文件；同名产物通过 Artifact → Artifact `version_of` 关系形成版本链。
- 文档生成请求记录模板、材料来源、字符数、校验提醒和上一版本 Artifact；保留 DOCX/PDF、多 Artifact 和事实引用校验缺口。
- 升级 PWA 缓存和版本标记，不覆盖既有配置、数据库和产物。

## v0.3.10 · 2026-08-07

- 知识库 Agent 接入本机 Obsidian Vault，只读扫描 Markdown，不修改 `.obsidian/` 和原始笔记。
- 增加 SQLite 增量索引，保存标题、正文检索文本、Frontmatter、标签、WikiLink、更新时间和内容哈希。
- 知识库页面增加 Vault 状态、索引数量、最后同步、索引按钮和 Obsidian 搜索结果；搜索支持正文、标题、标签和双链。
- 知识库 Agent 上下文可读取 Obsidian 最近笔记，保留反向链接数量和后续语义检索/MOC 缺口。
- 升级 PWA 缓存和版本标记，不覆盖既有配置、数据库、知识库、产物；Vault 原文件保持不变。

## v0.3.9 · 2026-08-07

- 通知中心明确收敛为应用内收件箱，不再把旧版“桌面推送已启用”Toast 当成通知内容。
- 通知面板补充关闭按钮、最近事件说明、来源 Agent、事件类型、发生内容、时间和项目直达入口。
- 单条通知显示未读状态；点开详情标记该条已读，“全部已读”清除全部红点并保持按钮状态正确。
- 升级工作台前端资源与 PWA 缓存版本，不修改既有配置、数据库和产物。

## v0.3.8 · 2026-08-07

- 收件箱 Agent 增加自动分类、截止时间提取、重复检测和可解释的整理 Run。
- 收件箱条目会生成目标 Agent 交接候选；确认后才创建 WorkItem、Relation 和应用内通知，拒绝可留痕。
- 目标项目 Agent 增加交接工作项领取/执行，结果回写 WorkItem、目标 Run、Relation 和应用通知；仍保留人工点击启动和高风险动作确认边界。
- 收件箱页面显示 Agent 判断、截止时间、重复提醒和交接候选；统一项目 Agent 面板补充已具备能力与下一轮缺口。
- SQLite 采用增量迁移，保留既有收件箱、配置、会话、数据库和产物；PWA 缓存升级至 v0.3.8。

## v0.3.7 · 2026-08-07

- 应用通知中心改为面板内反馈：通知内容在面板展开，已读状态和去向清晰可见。
- 明确红点规则：点开单条通知清除对应红点，“全部已读”清除全部红点；不再把桌面推送提示混入应用通知。
- 升级工作台 HTML、JS、CSS 和 PWA 缓存版本，避免旧版“桌面推送已启用”提示继续残留。

## v0.3.6 · 2026-08-07

- 首页增加“项目协作链”，展示来源 Agent、目标 Agent、关系名称和可追踪交接说明，支持分别新标签页打开两端项目。
- 项目卡片显示真实 Agent 状态：工具就绪、已接入、代理已接入或规划中。
- 各项目统一补充全局 LLM 入口；无独立配置按钮的项目自动指向工作台全局配置。
- `/api/project-links` 返回用户可直接使用的中文 Agent 名称和项目地址；升级 PWA / Electron 资源缓存。
- 项目交接改为两步确认，后端拒绝未确认请求，确认后才创建 WorkItem 和 Relation。
- AI 热点和想法分析的专用 Agent 页面补齐项目联动区，不再只有聊天而没有交接入口。

## v0.3.5 · 2026-08-07

- 应用通知中心补充“红点 = 未读、点开 = 已读、全部已读清除红点”的可见说明。
- 已读通知改为“点击展开内容”，不再继续提示“标记已读”；调度通知正文明确显示任务内容。
- 明确通知中心只记录工作台应用内事件，不调用桌面系统通知；升级 PWA / Electron 资源缓存，避免旧提示残留。

## v0.3.4 · 2026-08-07

- 通知铃铛彻底收敛为应用内通知，不再显示或触发“桌面推送/系统提醒”入口。
- 通知详情补充来源 Agent、事件类型、发生内容和完整时间；点击条目展开详情并自动标记已读。
- 红点只代表未读应用通知；“全部已读”清除全部红点，没有项目链接的通知不再错误显示“打开项目”。
- 升级工作台前端资源和 PWA 缓存版本，不覆盖既有配置、数据库和产物。

## v0.3.3 · 2026-08-07

- AI 热点、想法分析、Crawl4AI 和 CID 看板补齐独立 Agent 的持久 Run、事件、失败审计和重试。
- Crawl4AI 任务由内存对象升级为 SQLite 可恢复记录；生成研究 WorkItem、结果 Artifact 和 Relation 链路。
- AI 热点保留所选资讯证据与会话；想法分析保留领域会话并记录判断结果；CID 代理保持兼容原有接口。
- 增加 [PROJECT-STATUS-AUDIT.md](PROJECT-STATUS-AUDIT.md)，固化全部项目的当前能力、未完成事项和联动方向。
- PWA 缓存和工作台前端资源升级至 v0.3.3。

## v0.3.2 · 2026-08-07

- 增加统一 Agent Run 审计：项目对话、总调度和本地动作均记录状态、事件、结果、失败原因与尝试次数。
- Agent 详情返回工具权限摘要；未接入工具会明确显示为不可执行。
- 项目 Agent 面板增加最近运行列表、失败原因和可重试操作；对话重试不会重复写入用户消息。
- `agent_actions` 自动迁移 `run_id` 字段，保留既有数据库和历史数据；PWA 缓存升级至 `v0.3.2`。

## v0.3.1 · 2026-08-07

- 将顶部通知按钮收敛为纯应用内通知中心，不再把系统推送权限/提示作为点击结果。
- 通知点击先在面板内展开“通知内容”，自动标记该条已读；只有点击“打开项目”才会新开标签页。
- “全部已读”明确清除未读红点；通知面板显示应用内模式和完整时间，避免把底部 Toast 误认成通知内容。
- 应用通知不再默认触发桌面系统提醒，保留显式开启后的可选能力；升级 PWA 缓存版本。

## v0.3.0 · 2026-08-06

- 增加统一持久 Agent 基础层：项目 Agent 会话和消息写入 SQLite，重载页面后可继续上下文。
- 收件箱、知识库、文档工厂、Sub2API、量化选股和服务器监控增加统一“项目 Agent”入口，使用各自项目上下文和中文 Agent 名称。
- 项目 Agent 支持显式创建跨项目交接：交接会生成 WorkItem 和 Relation，并显示目标项目。
- `/api/agent/{project_id}/chat`、会话查询和交接 API 正式接入；动作仍沿用项目权限和低风险本地自动执行边界。
- 保留既有 data / knowledge-base / outputs，不迁移、不覆盖历史内容；PWA 缓存升级到 v0.3.0。

## v0.2.9 · 2026-08-06

- 通知按钮改为打开应用内通知中心，不再点击即申请桌面推送权限。
- 应用通知展示来源 Agent、事件类型、正文、时间和直达项目；点击单条通知自动标记已读并打开新标签页。
- 增加“全部已读”，红点只统计未读应用通知；桌面推送权限调整为通知中心内的可选设置。
- 增加 `/api/project-links` 项目联动图接口，支持按项目筛选。
- 升级 PWA Service Worker 缓存版本，保留既有 data / knowledge-base / outputs。

## v0.2.8 · 2026-08-06

- 为总 Agent 和 10 个子 Agent 增加领域任务流程、输出契约、证据要求和自动化边界。
- 子 Agent 不再只返回通用问答：明确要求输出结论、事实证据、不确定性、动作、确认项和下一步负责人。
- 收件箱 Agent 支持明确“记录/保存”请求自动写入本地收件箱；知识库 Agent 支持明确请求自动生成新笔记，不覆盖旧产物。
- 工作台首页改为独立主区域滚动、固定顶栏、加载骨架、可读对话结果和更紧凑的状态交互。
- 保留 v0.2.7 的通知、推送与热点去重能力。

## v0.2.7 · 2026-08-06

- 增加 SQLite `notifications` 通知事件表和未读/已读 API；高优先级工作项自动生成通知。
- 工作台增加本机桌面推送入口，支持 PWA Service Worker / Electron 环境，通知点击可回到项目。
- AI 热点按标准化原文链接或标题去重，减少多来源重复资讯。
- 明确当前单进程架构、项目故障边界和后续 Crawl / Sync / Monitor / Agent Worker 隔离路线。
- 保留既有 `data/`、`knowledge-base/`、`outputs/` 和全局 LLM 配置。

## v0.2.6 · 2026-08-06

- 建立统一 Agent 动作协议和 SQLite `agent_actions` 记录，区分自动执行、待确认、失败。
- 量化研究 Agent 接入 `market.watchlist.add`：明确要求添加股票时，直接写入本地自选并在总 Agent 结果中显示执行状态。
- 总 Agent 弹窗增加动作结果和“确认执行”入口，为后续高风险动作保留人工审批路径。
- 增加 `PROJECT-AGENT-ROADMAP.md`，逐个项目列出独立优化轮次和动作权限边界。

## v0.2.5 · 2026-08-06

- 修复总 Agent 输入时被全局快捷键劫持的问题，移除项目字母快捷键和 `/` 搜索抢焦点行为。
- 移除首页项目卡片上已经停用的快捷键提示，避免误导。
- 修正项目入口配置的尾逗号，保留全部项目入口可正常加载。

## v0.2.4 · 2026-08-06

- 为所有子 Agent 增加稳定的中文名称，项目卡片、总调度选择器和联动任务统一显示中文名。
- `/api/agents` 增加 `status_label` 和 `children_detail`，保留英文项目 ID 供程序调用。
- 联动任务 API 增加来源 Agent 与目标 Agent 的中文显示字段，历史工作项无需迁移即可正常展示。

## v0.2.3 · 2026-08-06

- 增加工作台总调度 Agent：工作台作为父 Agent，项目能力作为子 Agent，支持自动路由、子 Agent 汇总和本地工作项记录。
- 增加 SQLite `work_items`、`artifacts`、`relations` 基础表及工作项、交接 API。
- 独立开发者看板改为使用工作台全局 LLM 代理，不再从项目页面发送独立 API Key。
- 补齐服务器监控页面，支持主机、磁盘、内存、负载、Nginx 和服务状态展示；检查失败保留上次快照。
- 工作台首页增加“总调度 Agent”入口，项目继续保持新标签页打开。

## v0.2.2 · 2026-08-06

- 完成历史未完成项审计，新增项目联动与 Agent 架构文档。
- 增加真实 Agent 能力注册表，避免把“全局 LLM 已配置”误报为“所有项目 Agent 已就绪”。
- 明确 Artifact、WorkItem、Run、Relation 四类共享对象和三轮 Agent 演进路线。

## v0.2.1 · 2026-08-06

- 项目卡片移除醒目的 `READY` / “已登录”状态文案。
- 状态改为卡片右上角绿灯/红灯，悬停或聚焦时显示具体状态。
- 增加“常用项目”分组，优先聚合高频入口。

## v0.2.0 · 2026-08-06

- 全局 LLM 配置增加跨项目测试入口，已保存 API Key 不需要反复输入。
- 配置采用原子写入，数据目录支持独立于代码 release。
- Sub2API 首页主指标改为每周额度，账户余额降为辅助快照。
- 增加量化选股基础入口、自选股和公开行情快照。
- 增加 PWA 安装基础文件和 Electron 薄壳骨架。
- 保留现有收件箱、知识库、文档工厂、Crawl4AI 和 Sub2API 数据。

## v0.1.0 · 初始工作台

- 建立本地项目入口、全局 LLM 配置、收件箱、知识库、文档工厂和 Sub2API 快照。

版本升级原则：只替换代码 release，不覆盖 `data/`、`knowledge-base/`、`outputs/` 和服务器 shared 数据。

## v0.3.51 · 2026-08-07

- 「现在要处理」模块加「一键处理」按钮：点击逐个打开待办项对应项目页面（处理完回首页按钮自动指向下一个），按钮显示「一键处理 · 剩余 N」，有新打开动作时顶部提示「已打开：「xxx」 · 剩余 N 项」。Playwright 实测：1 条待办 → 按钮文案剩余 1 → 点击后剩余 0。

## v0.3.52 · 2026-08-07

- 修复「现在要处理」工作项跳转 bug：alert 类工作项（服务器/Sub2API/行情告警）之前按 target_project=inbox 跳进了收件箱，现在改为跳回产生告警的项目（source_project），让用户直接看到真实状态去处理；普通交接类仍跳目标项目。Playwright 实测：alert server → /projects/server、alert sub2api → /projects/sub2api、research → /crawl4ai、task inbox → /projects/inbox。

## v0.3.53 · 2026-08-07

- 「现在要处理」卡片加「忽略」按钮：不想管的待办一键忽略（标记 metadata.ignored_at，不改变状态避免告警重复创建），从待办列表消失；工具栏出现「已忽略 N 项」切换按钮，进入已忽略视图可「恢复」回待办（恢复后自动回到待办视图，避免停在空列表）。Playwright 实测：忽略 → 切换 → 恢复 → 回到待办全流程通过。

## v0.3.61 · 2026-08-08

- 修复 Crawl Worker 独立执行后工作项状态不回写 bug：`create_agent_run_record` 持久化时未携带 work_item_id，worker 从 SQLite 领取任务时 runtime 里 work_item_id 恒为 None，导致爬虫 Run 成功后关联的「网页研究」工作项永远停留在 running。修复：crawl 创建与重试端点把 work_item_id 写入 request_json（update_agent_run_record 支持 request 参数），runtime_crawl_from_agent_run 从 request 恢复。
- 修复服务器 Crawl4AI 浏览器路径：chromium 装在 example-user 用户下，workbench 服务用户找不到 → 爬虫全部失败（BrowserType.launch: Executable doesn't exist）。已为 workbench 用户安装 chromium 并固化 PLAYWRIGHT_BROWSERS_PATH + ProtectHome=false 到 crawl worker service。
- 归档 2 个已被重试成功取代的失败工作项；服务器升级 v0.3.60，5 个服务 active。

## v0.3.61 · 2026-08-08（补记）

- 「最近发生」回放中心优化：从首页常驻大区块改为默认收起的开发者视图（details 折叠，展开时才加载），内容去重（相邻同文件合并显示 ×N）+ 中文友好标签（产物/工作项/运行/关联）+ 项目中文名。
- 证据矩阵补齐：通过 /api/evidence/run 对 25 条联动边批量真实跑通 success/failure/retry/manual_takeover 四场景，100 个证据全部 verified（从 0 → 100），WorkItem+Run+Relation+Notification 完整对象链落库；验收工作项已归档。
- Web Push 服务端配置：生成 VAPID 密钥对写入服务器 /www/workbench/.env（部署不覆盖），WORKBENCH_VAPID_PUBLIC/PRIVATE/SUBJECT 生效，/api/push/config 返回 configured=True；浏览器订阅后即可真实送达。

## v0.3.63 · 2026-08-08

- Crawl4AI 支持抓取微信公众号文章：检测 mp.weixin.qq.com URL 时自动切换微信内置浏览器 UA（MicroMessenger）+ stealth 模式 + 移动端视口，绕过微信 WAF 的“环境异常”验证页；实测公众号正文完整抓取。
- 微信公众号文章强制走浏览器渲染（不走轻量 HTTP 模式，避免 302/验证页）。

## v0.3.65 · 2026-08-08

- 总调度 Agent 改造：子 Agent 调用从串行改为并发（asyncio.gather），每个子 Agent 建立独立 dispatch_child Run（parent 关联总调度 Run），单子 Agent 失败隔离不影响其他，run_status 在任一子 Agent 失败时标 partial。replay.child_run_ids 返回真实 child run id。实测：server+market 并发调度，2 个 child run 均 succeeded。
- 微信公众号抓取修复（v0.3.64 完整链路）：微信 UA + stealth + 移动端视口绕过 WAF 验证页；关闭 robots.txt 检查（微信 robots 拦截导致 403）。实测公众号正文 6671 字符完整抓取。

## v0.3.65 · 2026-08-08（补记）

- 备份/恢复演练：服务器实测 backup-workbench.sh 备份 + manifest.sha256 校验全部 OK；llm_settings.json 权限确认 600（workbench 用户）。
- 证据 synthetic 区分确认：evidence_acceptance kind + created_by=evidence_runner 标记已存在。

## v0.3.66 · 2026-08-08

- 首页侧栏新增「推送订阅」入口 + 弹窗：订阅浏览器推送、静默时段设置、测试发送、最近推送记录、侧栏订阅数量徽标；VAPID 已配置时可直接订阅。
- 首页侧栏底部显示当前部署版本号（读 /api/meta，失败保留静态版本）。

## v0.3.67 · 2026-08-08

- 修复 Web Push 测试失败：.env 存的是裸 base64url DER 私钥字节，pywebpush 不识别（"Could not deserialize key data"）。发送前自动包装成 PKCS8 PEM 格式，兼容旧 .env 值（无需重生成密钥），也直接支持新 PEM 格式。

## v0.3.68 · 2026-08-08

- 修复 Web Push 测试推送失败根因：systemd EnvironmentFile 多行 PEM 字符串易转义出错，VAPID 私钥被错误读到空值，导致 webpush 报"Could not deserialize key data"。
- 改造为优先读取 data/vapid_private.pem（实际 PEM 文件，权限 600），代码向后兼容旧的 base64url DER 环境变量；公钥仍存 .env。
- 部署：workbench.service 加 EnvironmentFile=/www/workbench/.env 让 systemd 加载 .env；data/vapid_private.pem 由 .env 读公钥 + 文件读私钥，避免 .env 存多行 PEM 的转义坑。

## v0.3.69 · 2026-08-08

- 修复推送测试"Could not deserialize key data"：pywebpush 1.14.1 的 vapid_private_key 参数期望文件路径或 Vapid 实例，不是 PEM 字符串。代码改为：每次发送把 PEM 写到临时文件（tempfile.NamedTemporaryFile，按 delivery_id 隔离并发），传文件路径给 webpush，发送后删除。

## v0.3.70 · 2026-08-08

- 修复推送测试导致服务卡死：deliver_push 内含 pywebpush 同步网络请求，在 async 端点里直接调用会阻塞 uvicorn 事件循环（单 worker 下整个服务无响应）。改为 asyncio.to_thread 放入线程池，/api/push/test 和推送重试端点均已修复。

## v0.3.71 · 2026-08-08

- Web Push 支持走代理发送：配置 WORKBENCH_PUSH_PROXY（如 http://127.0.0.1:15236，通过 SSH 反向隧道转发到本机代理）后，webpush 请求经代理发往 FCM，解决国内服务器直连 Google 被墙问题。pywebpush 用 requests_session 注入代理。

## v0.3.72 · 2026-08-08

- 订阅按钮兼容「旧公钥订阅残留」：点击订阅前先检测并取消浏览器里已有的旧 applicationServerKey 订阅（浏览器限制更换 applicationServerKey 必须先 unsubscribe 再 subscribe），避免 "Registration failed - A subscription with a different applicationServerKey already exists"。

## v0.3.73 · 2026-08-08

- 「最近发生」改为「人话活动流」：每条动态用一句话说明发生了什么（如「新增待办：Sub2API 每周额度偏低」「workbench Agent 运行成功：…」「保存了产物：回测-…」），显示项目中文名、状态、相对时间；过滤内部技术记录（关系边、证据验收、crawl-result 快照），不再展示数据库对象流。面板标题也从「开发者回放/对象链」改为「最近活动」。
- 本机代理隧道配置：launchd plist 已放置 ~/Library/LaunchAgents/com.workbench.push-tunnel.plist（开机自动建反向隧道 15236），当前环境无法加载 GUI launchd 域，下次登录生效；临时隧道已恢复保持推送可用。

## v0.3.74 · 2026-08-08

- 「最近发生」活动流过滤自动化噪声：定期快照产物（server/sub2api/aihot/cid snapshot.json）和证据验收/手动接管 run 不再刷屏，只保留用户可见的动态。

## v0.3.75 · 2026-08-08

- 量化选股项目深度优化:
  - **页面信息架构重构**: 按「1 我的自选 → 2 数据状态 → 3 Agent 观察 → 4 深度研究工具」研究流程分组, 每段人话引导, Agent 能力脚注说明;
  - **涨跌颜色修复**: 中国股市惯例(涨红跌绿), 同时配合 ▲▼ 箭头;
  - **Agent 升级**: market 注册表 status: tool_ready → implemented (工具/能力/运行记录均齐);
  - **深度研究工具布局**: 回测为主栏, 策略对比/估值/日报周报合并到右侧栏, 节省纵向空间。

## v0.3.76 · 2026-08-08

- 量化选股行情可视化升级：每只自选股增加 SVG 迷你走势图（基于历史快照价格序列，涨红跌绿，起点/终点圆点标记），顶部 KPI 增加「▲ N 涨 / ▼ N 跌」统计，行情卡 4 列布局（名称/走势/价格/涨跌），移动端自动隐藏走势图。

## v0.3.77 · 2026-08-08

- 修复 Sub2API 页面主题错乱（白底浅字看不清）：theme.css 动态注入默认浅色主题，覆盖了 sub2api 的 `--panel` 为白色但 `--ink` 未变，导致白底浅字。修复 ①project.js 改为「html 已显式声明 data-theme 时尊重页面，不覆盖」；②sub2api.html `<html data-theme="dark">` 显式锁定深色（深色设计页）；③theme.css 的 dark 模式补齐 `--panel/--raised/--panel-soft` 等被 sub2api 直接使用的变量。

## v0.3.78 · 2026-08-08

- 收件箱：忽略交接建议后自动滚动高亮下一条待处理项。
- 首页 Cmd+K 命令面板：快速搜索并跳转项目 / 待办工作项 / 平台工具（自动化/Git/审批/总调度/全局LLM），支持键盘上下选择 + Enter 跳转 + Esc 关闭。
- AI 热点摘要（手动 digest + 每日自动化 summary）生成后触发远程 Web Push（有订阅才发，失败不影响摘要）；新增批量推送辅助 _push_to_all_subscriptions（to_thread 防阻塞）。
- 确认已完成：文档工厂「按意见重新生成」闭环、量化研究结论沉淀知识库链路（实测 create→conclude→knowledge-note 全通）、LLM 前端为共享面板无重复实现。

## v0.3.79 · 2026-08-08

- 知识库向量检索（embedding 服务方案）：Mac 本机运行 fastembed + bge-small-zh-v1.5（512 维）embedding 服务，经 SSH 反向隧道 15237 供服务器调用（服务器 1.9G 内存跑不动本地模型，改用"Mac 起服务 + 隧道"架构，与 Web Push 代理同一模式）。服务器知识库搜索升级为「关键词 + 语义向量」混合检索：/api/knowledge?vector=1 时懒索引缺失向量（knowledge_vectors 表按文件路径存储，内容哈希变化自动重索引），query 向量与库内向量余弦相似度排序，与关键词分融合；embedding 服务不可用时自动降级纯关键词。新增 /api/knowledge/reindex-vectors（force=1 全量重建）。前端搜索框显示检索模式徽标（语义+关键词 / 关键词）。
- 实测：本地「行情」关键词 0 命中 → 混合语义 2 命中；「服务器负载和磁盘空间」纯语义 5.0/4.84 命中（词面零重合）。

## v0.3.80 · 2026-08-08

- Obsidian 检索接真向量：`obsidian_semantic_results` 在 embedding 服务可用时用真语义向量（knowledge_vectors 表 + query 现场 embed）替代词法哈希向量，BM25 保留；服务不可用自动回退哈希向量。相关 async 端点全部改 to_thread 防阻塞。
- Sub2API 额度预测 + 变化解释：`sub2api_prediction`（纯计算：历史周额度剩余线性外推，预测用完天数）；`/api/sub2api/explain-change`（LLM 一句话解释最近两次快照变化，10 分钟缓存）；页���健康面板加「额度消耗预测」卡 + 「解释变化」按钮。
- AI 热点机会复盘：`/api/aihot/opportunity-review` 聚合商机线索状态（待处理/进行中/已处理 + 采纳率）；商机线索视图顶部显示复盘条。
- CID 偏好自动学习：`/api/cid-dashboard/preferences/learn`（like/dislike 把项目赛道标签自动并入 preferred/avoid，支持未登记机会时按 repo+project_key 从快照学习）；机会列表按偏好加权排序；项目详情页加「👍 喜欢此赛道 / 👎 不感兴趣」按钮。
- PWA 安装引导：首页 `beforeinstallprompt` 监听 + 底部安装条（可关闭，已安装/独立模式不显示）；desktop 壳版本同步 v0.3.79 → v0.3.80。
- 失败 Run 重试核实：retry 端点已覆盖 crawl/chat/dispatch/opportunity_validation + 通用 chat fallback，前端统一重试按钮存在；服务器真实业务失败仅 crawl 类（可重试），其余为验收场景故意失败，无缺口。

## v0.3.81 · 2026-08-08

- 修复 CID 偏好学习端点：参数名 request 与 FastAPI 保留参数冲突（被当作 query 参数导致 Field required），改名 payload。

## v0.3.82 · 2026-08-08

- 修复 CID 偏好学习端点二次修复：真正根因是 `from __future__ import annotations` 下函数注解为字符串，而 CIDPreferenceLearnRequest 定义在端点之后，FastAPI 无法解析字符串注解为 body model（当作 query）。把模型类定义移到端点之前。

## v0.3.84 · 2026-08-08

- 修复 CID 偏好学习按快照取标签：projects 嵌套在 cid_snapshot_row 的 snapshot 字段下，learn 端点读取路径修正。
- GitHub 工具目录新增 ntfy、Miniflux、Zotero 三类效率集成：配置、连通性测试、条目读取、选择性导入 WorkItem；密钥只保存在 Workbench 服务端，不回显。
- ntfy 支持主动通知，并异步转发工作台高优先级告警；外部发送失败不会阻断本地通知记录。
- Push 页面增加私钥来源、代理状态、公钥缺失和最近送达失败原因，区分“已保存订阅”和“已送达”。
- Crawl Worker 部署统一 `PLAYWRIGHT_BROWSERS_PATH` 到发布目录 `.cache/ms-playwright`，部署时检测并安装 Chromium，避免服务用户使用错误浏览器路径。
- 新增集成测试；本地 28 条单元测试、Python 编译、静态 JS 和部署脚本语法检查通过。线上发布与第三方凭据验收仍待确认。

## v0.3.105 · 2026-08-09

- Sub2API 告警恢复闭环：evaluate 时对「账户已恢复正常但旧告警仍挂 open」的 work_item 自动标 done（resolved_by=sub2api_health_recovery），不再让已解决的告警长期占用首页待办；返回新增 restored 列表。

## v0.3.106 · 2026-08-09

- 审批队列：新增 /api/approval-queue 聚合三源（审批请求 pending/resubmitted + blocked 工作项 + 待确认 Agent 动作）；首页侧栏「审批与交付」加待确认徽标；/approvals 页新增「待确认工作项与动作」区块。
- LLM Key 安全加固：llm_settings.json 留空的 Key 自动从受保护环境变量注入（WORKBENCH_LLM_KEY_<ID> / _<NAME> / WORKBENCH_LLM_KEY / WORKBENCH_LLM_KEYS JSON），Key 可不进配置文件；normalize 输出新增 key_source（saved/environment）。
- AI 热点来源评分 + 变化检测：每个热点新增 change 标记（对比 previous_items 快照，新出现标「新」）与 source_score（基准 3 分 + 该来源用户反馈加权，有用 +1 / 不相关 -1）；useful/opportunity 排序纳入来源分与新热点微提升；卡片显示「新」徽标 + 来源 X/5 分。
- 自动化新增 worker_health_check 类型：定期检查 Worker 心跳，有 stale worker 时创建告警工作项 + 通知。

## v0.3.109 · 2026-08-09

- 修复 worker_health_check 自动化：worker_status_payload 返回 list（非 dict），分支直接遍历列表。
