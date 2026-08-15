import unittest
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

import app


class WorkbenchStatusTests(unittest.TestCase):
    def test_all_project_pages_default_to_shared_light_theme(self):
        root = Path(__file__).resolve().parents[1]
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        pages = sorted((root / "static").glob("*.html"))
        self.assertGreaterEqual(len(pages), 18)
        for page in pages:
            source = page.read_text(encoding="utf-8")
            self.assertIn('data-theme="light"', source, page.name)
            self.assertIn(f'/static/theme.js?v={version}', source, page.name)

        dashboard = (root / "projects" / "cid-dashboard-v2.html").read_text(encoding="utf-8")
        self.assertIn('data-theme="light"', dashboard)
        self.assertIn("THEME_KEY='workbench-theme'", dashboard)
        self.assertNotIn("prefers-color-scheme:dark", dashboard)

        theme_source = (root / "static" / "theme.js").read_text(encoding="utf-8")
        self.assertIn('const DEFAULT_THEME = "light"', theme_source)
        self.assertIn('window.addEventListener("storage"', theme_source)
        self.assertIn('button.setAttribute("aria-pressed"', theme_source)

    def test_service_worker_allows_root_scope_and_is_not_cached(self):
        root = Path(__file__).resolve().parents[1]
        version = (root / "VERSION").read_text(encoding="utf-8").strip()

        async def request():
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(f"/static/sw.js?v={version}")

        response = asyncio.run(request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("service-worker-allowed"), "/")
        self.assertIn("no-cache", response.headers.get("cache-control", ""))
        self.assertIn(f'CACHE_NAME = "workbench-shell-v{version}"', response.text)
        for route in (
            "/projects/inbox", "/projects/knowledge", "/projects/doc-factory",
            "/projects/sub2api", "/projects/market", "/projects/server",
            "/projects/aihot", "/projects/ai-learning", "/projects/embodied",
            "/projects/idea-analysis", "/projects/product-manager",
            "/projects/cid-dashboard", "/projects/web-research", "/projects/cloud-dev",
        ):
            self.assertIn(f'"{route}"', response.text, route)

    def test_favicon_uses_the_existing_workbench_icon(self):
        async def request():
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get("/favicon.ico")

        response = asyncio.run(request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-type"), "image/png")
        self.assertIn("max-age=86400", response.headers.get("cache-control", ""))
        self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_shared_frontend_request_contract_is_loaded_and_user_friendly(self):
        root = Path(__file__).resolve().parents[1]
        request_source = (root / "static" / "request.js").read_text(encoding="utf-8")
        self.assertIn("线上入口需要认证，请先完成登录后再试。", request_source)
        self.assertIn("请求超时，请稍后重试。", request_source)
        self.assertIn("请求过于频繁，请稍后再试。", request_source)
        self.assertIn("服务暂时不可用，请稍后重试", request_source)
        self.assertIn("data-wb-retry", request_source)
        pages = [
            "index.html", "workbench.html", "project-shell.html", "inbox.html", "knowledge.html",
            "doc-factory.html", "aihot.html", "market.html", "idea-analysis.html", "server.html", "web-research.html", "cloud-dev.html",
            "sub2api.html", "automation.html", "git.html", "github-tools.html", "approvals.html",
        ]
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        for page in pages:
            source = (root / "static" / page).read_text(encoding="utf-8")
            self.assertIn(f"/static/request.js?v={version}", source, page)

        web_research = (root / "static" / "web-research.js").read_text(encoding="utf-8")
        self.assertIn("https://www.doubao.com/browser-extension/landing", web_research)
        self.assertIn("https://www.tabbit.com/", web_research)
        self.assertNotIn("tabbit-ai.com", web_research)
        self.assertIn("function safeHttpUrl", web_research)
        self.assertIn('["http:", "https:"]', web_research)
        self.assertIn("const sourceUrl = safeHttpUrl(doc.url)", web_research)
        self.assertIn("function configureBookmarklet", web_research)
        self.assertIn("source_selection", web_research)
        # 本机 Gemini 桥已迁入全局 LLM 设置：超时保护在 llm-settings.js，设置区块在首页/Crawl4AI 弹窗。
        llm_settings = (root / "static" / "llm-settings.js").read_text(encoding="utf-8")
        self.assertIn("new AbortController()", llm_settings)
        self.assertIn("本机 Companion 请求超时", llm_settings)
        self.assertIn("companion-toggle", llm_settings)
        for page in ("workbench.html", "index.html"):
            source = (root / "static" / page).read_text(encoding="utf-8")
            self.assertIn("本机 Gemini 桥", source, page)
            self.assertIn("companion-toggle", source, page)
        web_research_page = (root / "static" / "web-research.html").read_text(encoding="utf-8")
        self.assertIn("address-form", web_research_page)
        self.assertIn("随时问我", web_research_page)
        self.assertNotIn("gemini-toggle", web_research_page)
        self.assertIn("applyIncomingContext", web_research)
        self.assertIn('id="research-bookmarklet"', (root / "static" / "web-research.html").read_text(encoding="utf-8"))
        self.assertIn('id="copy-bookmarklet"', (root / "static" / "web-research.html").read_text(encoding="utf-8"))
        market = (root / "static" / "market.html").read_text(encoding="utf-8")
        market_js = (root / "static" / "market.js").read_text(encoding="utf-8")
        self.assertIn("/static/market.js", market)
        self.assertIn('id="research-card-form"', market)
        self.assertIn('id="ai-scan-form"', market)
        self.assertIn('id="quote-list"', market)
        self.assertIn("/api/market/research-card", market_js)
        self.assertIn("/api/market/etf-rotation", market_js)
        self.assertIn("/api/market/convertible-bonds", market_js)
        self.assertIn("/api/market/valuation-percentile", market_js)
        self.assertIn("/api/market/ai-scan", market_js)
        sync_dependents = market_js.split("function syncMarketDependents(market) {", 1)[1].split("\n}\nfunction renderQuotes", 1)[0]
        self.assertNotIn("loadAIScan(", sync_dependents)
        self.assertIn('$("#ai-scan-form").addEventListener("submit", async (event) => {', market_js)
        self.assertIn('await loadAIScan($("#ai-scan-question").value.trim())', market_js)
        self.assertIn("不会在打开页面时自动调用 AI", market)
        self.assertIn("/api/market/backtest/walk-forward", market_js)
        self.assertIn("/api/market/sampling", market_js)
        self.assertIn("legacy-tools", market)
        cloud_dev = (root / "static" / "cloud-dev.html").read_text(encoding="utf-8")
        self.assertIn('event.submitter || $("#cloud-form button[type=submit]")', cloud_dev)
        self.assertIn('id="cloud-readiness"', cloud_dev)
        self.assertIn("WORKBENCH_FEISHU_APP_ID/SECRET", cloud_dev)
        self.assertIn("WORKBENCH_FEISHU_VERIFY_TOKEN/ENCRYPT_KEY", cloud_dev)

    def test_every_product_page_gets_shared_accessibility_primitives(self):
        root = Path(__file__).resolve().parents[1]
        theme_js = (root / "static" / "theme.js").read_text(encoding="utf-8")
        theme_css = (root / "static" / "theme.css").read_text(encoding="utf-8")
        self.assertIn('stylesheet.dataset.workbenchTheme = "true"', theme_js)
        self.assertIn('skip.textContent = "跳到主要内容"', theme_js)
        self.assertIn('node.setAttribute("aria-live", error ? "assertive" : "polite")', theme_js)
        self.assertIn('node.setAttribute("aria-label", fallback)', theme_js)
        self.assertIn('node.setAttribute("aria-modal", "true")', theme_js)
        self.assertIn("@media (prefers-reduced-motion: reduce)", theme_css)
        self.assertIn(".skip-link:focus", theme_css)
        for page in sorted((root / "static").glob("*.html")):
            source = page.read_text(encoding="utf-8")
            self.assertIn("/static/theme.js", source, page.name)

    def test_inbox_write_has_fullscreen_compose_and_markdown_preview(self):
        """收件箱写入优化：全屏编辑弹窗 + 工具栏 + 实时 Markdown 预览。"""
        root = Path(__file__).resolve().parents[1]
        html = (root / "static" / "inbox.html").read_text(encoding="utf-8")
        self.assertIn('id="inbox-compose-full"', html, "缺少全屏编辑入口")
        self.assertIn('id="inbox-compose-modal"', html, "缺少全屏编辑弹窗")
        self.assertIn('id="inbox-compose-text"', html)
        self.assertIn('id="inbox-compose-preview"', html, "弹窗缺实时预览区")
        self.assertIn('data-md-wrap', html, "缺格式工具栏（加粗/斜体/代码）")
        self.assertIn('data-md-line', html, "缺行级格式（标题/列表/引用）")
        self.assertIn('/static/markdown.js', html, "收件箱必须加载 markdown 渲染库")
        self.assertIn('markdownPreviewOf', html, "缺预览渲染逻辑")
        css = (root / "static" / "project.css").read_text(encoding="utf-8")
        self.assertIn(".inbox-compose-modal", css)
        self.assertIn(".inbox-compose-grid", css)

    def test_frontend_project_and_platform_recovery_hooks_are_present(self):
        root = Path(__file__).resolve().parents[1]
        project_source = (root / "static" / "project.js").read_text(encoding="utf-8")
        platform_source = (root / "static" / "platform.js").read_text(encoding="utf-8")
        home_source = (root / "static" / "workbench.js").read_text(encoding="utf-8")
        self.assertIn("data-agent-load-retry", project_source)
        self.assertIn("data-retry-page", platform_source)
        self.assertIn("window.WorkbenchUX?.requestJson", platform_source)
        self.assertIn("workbenchRequestJson", home_source)
        self.assertIn("/api/agent/dispatch", home_source)
        self.assertIn("忽略这项并打开下一条待办", home_source)
        self.assertIn("source_coverage", home_source)
        self.assertIn("source_coverage", project_source)
        self.assertIn("capability-card-action", platform_source)
        self.assertIn("historical_failed", platform_source)
        self.assertIn("loadCrawlObservability", (root / "static" / "app.js").read_text(encoding="utf-8"))
        self.assertIn("队列与研究计划仍可单独使用", (root / "static" / "app.js").read_text(encoding="utf-8"))
        self.assertIn('synthetic: "内部测试通过"', home_source)
        self.assertIn("真实链路已验证", home_source)
        self.assertIn("project-link-card.link-synthetic", (root / "static" / "workbench.css").read_text(encoding="utf-8"))
        tools_page = (root / "static" / "github-tools.html").read_text(encoding="utf-8")
        self.assertIn("toggle-integration-selection", tools_page)
        self.assertIn("全选当前内容", platform_source)
        self.assertIn("已配置 ·", platform_source)
        self.assertIn("列表刷新失败", platform_source)
        automation_source = (root / "static" / "automation.html").read_text(encoding="utf-8")
        self.assertIn('id="recovery-count"', automation_source)
        self.assertNotIn("<strong>4</strong>", automation_source)
        self.assertIn('automation.summary?.failed_runs', platform_source)
        llm_source = (root / "static" / "llm-settings.js").read_text(encoding="utf-8")
        self.assertIn('button.setAttribute("aria-busy", "true")', llm_source)
        self.assertIn('view.save.removeAttribute("aria-busy")', llm_source)

    def test_document_factory_page_advertises_current_template_count(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "static" / "doc-factory.html").read_text(encoding="utf-8")
        self.assertIn('id="factory-format-count">7', source)
        self.assertIn("body.templates?.length || 7", source)

    def test_desktop_and_pwa_release_guard_is_present(self):
        root = Path(__file__).resolve().parents[1]
        package = (root / "desktop" / "package.json").read_text(encoding="utf-8")
        verifier = (root / "desktop" / "verify.mjs").read_text(encoding="utf-8")
        manifest = (root / "static" / "manifest.webmanifest").read_text(encoding="utf-8")
        service_worker = (root / "static" / "sw.js").read_text(encoding="utf-8")
        self.assertIn('"verify":', package)
        self.assertIn("desktop verify", verifier)
        self.assertIn('"display": "standalone"', manifest)
        self.assertIn("self.skipWaiting()", service_worker)

    def test_idea_interview_form_and_evidence_replay_hooks_are_present(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "static" / "idea-analysis.html").read_text(encoding="utf-8")
        self.assertIn("interview-overlay", source)
        self.assertIn("interview-participant", source)
        self.assertIn("interview-question", source)
        self.assertIn("interview-answer", source)
        self.assertIn("/interviews", source)
        self.assertIn("installInterviewAction", source)
        self.assertIn("metadata.session_id", source)
        workbench_source = (root / "static" / "workbench.js").read_text(encoding="utf-8")
        workbench_css = (root / "static" / "workbench.css").read_text(encoding="utf-8")
        self.assertIn("workItemQualityMarkup", workbench_source)
        self.assertIn("work-item-quality", workbench_css)

    def test_project_audit_exposes_quality_metrics_for_homepage(self):
        project = {"id": "inbox", "title": "快速收件箱", "href": "/projects/inbox"}
        detail = {
            "name": "收件箱 Agent",
            "status": "implemented",
            "status_label": "已接入",
            "tools": ["inbox_read"],
            "implemented_tools": ["读取收件箱"],
            "gaps": ["继续积累样本"],
            "run_summary": {"total": 1, "latest": None},
        }
        quality = {"total": 4, "success_rate": 0.75, "source_completeness_rate": 0.5}
        with patch.object(app, "load_projects", return_value=[project]), \
             patch.object(app, "llm_settings", return_value={"configured": False}), \
             patch.object(app, "agent_detail", return_value=detail), \
             patch.object(app, "agent_quality_metrics", return_value=quality), \
             patch.object(app, "project_link_audit", side_effect=lambda edge: {**edge, "status": "configured", "score": 0}), \
             patch.object(app, "project_activity", return_value={}), \
             patch.object(app, "project_data_freshness", return_value={}):
            result = app.project_audit("inbox")
        self.assertEqual(result["agents"][0]["quality"]["total"], 4)
        self.assertEqual(result["agents"][0]["quality"]["source_completeness_rate"], 0.5)

    def test_every_declared_agent_tool_has_an_auditable_policy(self):
        missing = {
            project_id: sorted(set(detail.get("tools", [])) - set(app.AGENT_TOOL_POLICIES))
            for project_id, detail in app.AGENT_REGISTRY.items()
        }
        self.assertEqual({key: value for key, value in missing.items() if value}, {})

    def test_every_project_agent_can_do_web_research(self):
        """每个项目 Agent 都要能上网调研：web_search（搜索）+ web_fetch（抓正文）。
        文档 Agent 写深度分析类文档时没有上网能力，只能声称"交接给网页研究 Agent"，
        而交接不落地（actions 为空）——用户干等。这是防回归测试。"""
        for project_id in app.AGENT_REGISTRY:
            if project_id == "workbench":
                continue
            schema_names = [item["function"]["name"] for item in app.subagent_tool_schemas(project_id)]
            self.assertIn("web_search", schema_names, f"{project_id} 缺少 web_search")
            declared = app.agent_declared_tools(project_id)
            self.assertIn("web_search", declared, f"{project_id} 能力声明缺少 web_search")
            self.assertIn("web_fetch", declared, f"{project_id} 能力声明缺少 web_fetch")
            self.assertIn("web_search", app.AGENT_TOOL_POLICIES)
            self.assertTrue(app.AGENT_TOOL_POLICIES["web_search"]["enabled"])
            self.assertTrue(app.AGENT_TOOL_POLICIES["web_fetch"]["enabled"])

    def test_creating_project_preserves_projects_hidden_by_user_preference(self):
        import app_pkg.projects as projects_module

        with tempfile.TemporaryDirectory() as temp_dir:
            projects_file = Path(temp_dir) / "projects.json"
            preferences_file = Path(temp_dir) / "project_preferences.json"
            configured = [
                {"id": "visible", "title": "可见项目"},
                {"id": "hidden", "title": "隐藏项目"},
            ]
            projects_file.write_text(json.dumps(configured, ensure_ascii=False), encoding="utf-8")
            preferences_file.write_text(json.dumps({"hidden_ids": ["hidden"]}), encoding="utf-8")

            with patch.object(projects_module, "PROJECTS_FILE", projects_file), \
                 patch.object(projects_module, "PROJECT_PREFERENCES_FILE", preferences_file), \
                 patch.object(projects_module, "public_projects", return_value=[]):
                projects_module.create_project(
                    projects_module.ProjectCreateRequest(id="new-project", title="新项目")
                )

            saved_ids = [item["id"] for item in json.loads(projects_file.read_text(encoding="utf-8"))]

        self.assertEqual(saved_ids, ["visible", "hidden", "new-project"])

    def test_public_project_health_exposes_counts_source_and_data_time(self):
        activity = {
            "tone": "warning",
            "label": "1 个失败待恢复",
            "work_items": {"open": 3, "blocked": 1, "failed": 1},
            "active_runs": 2,
            "failed_runs": 1,
            "latest_run": {},
        }
        freshness = {
            "status": "fresh",
            "label": "数据新鲜",
            "source": "本地收件箱",
            "detail": "3 条待处理",
            "checked_at": "2026-08-09T02:00:00+00:00",
        }
        project = {
            "id": "inbox",
            "title": "快速收件箱",
            "description": "收集待办",
            "status": "ready",
            "href": "/projects/inbox",
        }
        with patch.object(app, "load_projects", return_value=[project]), \
             patch.object(app, "list_inbox", return_value=[{"id": 1}, {"id": 2}, {"id": 3}]), \
             patch.object(app, "knowledge_files", return_value=[]), \
             patch.object(app, "load_sub2api_snapshot", return_value={}), \
             patch.object(app, "load_market_snapshot", return_value={}), \
             patch.object(app, "load_server_monitor_snapshot", return_value={}), \
             patch.object(app, "project_activity", return_value=activity), \
             patch.object(app, "project_data_freshness", return_value=freshness):
            result = app.public_projects()

        health = result[0]["health"]
        self.assertEqual(health["open_work_items"], 3)
        self.assertEqual(health["blocked_work_items"], 1)
        self.assertEqual(health["failed_work_items"], 1)
        self.assertEqual(health["active_runs"], 2)
        self.assertEqual(health["source"], "本地收件箱")
        self.assertEqual(health["data_as_of"], "2026-08-09T02:00:00+00:00")
        self.assertEqual(health["tone"], "danger")

    def test_idea_opportunity_list_includes_inbox_idea_review_handoffs(self):
        """收件箱路由到想法分析的工作项 kind 是 idea_review（aihot/cid 才是
        opportunity）。只认 opportunity 会把收件箱的交接全藏掉——用户从收件箱
        转给想法分析的任务，在想法分析页永远看不见。"""
        items = [
            {"id": 1, "source_project": "inbox", "kind": "idea_review", "target_project": "idea-analysis", "title": "收件箱想法"},
            {"id": 2, "source_project": "aihot", "kind": "opportunity", "target_project": "idea-analysis", "title": "热点机会"},
            {"id": 3, "source_project": "inbox", "kind": "opportunity", "target_project": "idea-analysis", "title": "收件箱机会"},
            {"id": 4, "source_project": "inbox", "kind": "task", "target_project": "idea-analysis", "title": "不该出现"},
            {"id": 5, "source_project": "inbox", "kind": "idea_review", "target_project": "market", "title": "目标不对"},
        ]
        with patch.object(app, "list_work_items", return_value=items):
            result = app.idea_opportunity_work_items()
        ids = [item["id"] for item in result]
        self.assertIn(1, ids, "收件箱 idea_review 必须出现在想法分析机会列表")
        self.assertIn(2, ids)
        self.assertIn(3, ids)
        self.assertNotIn(4, ids, "非机会 kind 不该出现")
        self.assertNotIn(5, ids, "目标不是 idea-analysis 的不该出现")


class KnowledgeNoteCrudTests(unittest.TestCase):
    """本地知识库阅读闭环：查看全文 / 编辑 / 删除（回收站）三条路径。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_dir = app.KNOWLEDGE_DIR
        self._old_cache = dict(app._knowledge_files_cache)
        app.KNOWLEDGE_DIR = self.tmp
        app._knowledge_files_cache = {"signature": None, "files": []}

    def tearDown(self):
        app.KNOWLEDGE_DIR = self._old_dir
        app._knowledge_files_cache = self._old_cache

    def test_edit_updates_body_and_title_line(self):
        note = app.write_knowledge_note("原始标题", "第一段正文")
        rel = note["path"]
        updated = app.update_knowledge_note(rel, "改后的正文", "新标题")
        self.assertEqual(updated["title"], "新标题")
        content = app.read_knowledge_note(rel)["content"]
        self.assertTrue(content.startswith("# 新标题"), content[:30])
        self.assertIn("改后的正文", content)

    def test_edit_without_title_keeps_existing_heading(self):
        note = app.write_knowledge_note("保持标题", "正文")
        rel = note["path"]
        app.update_knowledge_note(rel, "只有正文变化")
        content = app.read_knowledge_note(rel)["content"]
        self.assertTrue(content.startswith("# 保持标题"), "不传 title 不应丢失原标题")

    def test_delete_moves_note_into_trash_and_hides_from_search(self):
        note = app.write_knowledge_note("要删的笔记", "内容")
        rel = note["path"]
        result = app.delete_knowledge_note(rel)
        self.assertTrue(result["ok"])
        self.assertIn(".trash", result["trash_path"])
        self.assertTrue((self.tmp / ".trash").exists())
        files = [str(path) for path in app.knowledge_files()]
        self.assertNotIn(rel, files, "删除后不应再出现在检索结果")
        self.assertFalse((self.tmp / rel).exists(), "原文件应被移走")

    def test_escaped_paths_are_rejected_for_read_update_delete(self):
        for method in (app.read_knowledge_note,):
            with self.assertRaises(app.HTTPException) as ctx:
                method("../outside.md")
            self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(app.HTTPException):
            app.update_knowledge_note("../../etc/passwd", "x")
        with self.assertRaises(app.HTTPException):
            app.delete_knowledge_note("/etc/passwd")



    def test_projects_json_missing_falls_back_to_open_source_template(self):
        """projects.json 缺失（开源首次启动）时自动回退到 open-source 模板。"""
        import app_pkg.projects as projects_module
        from unittest.mock import patch as _patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects_file = root / "projects.json"
            template = root / "projects.open-source.json"
            template.write_text(json.dumps([{"id": "demo", "title": "演示项目"}]), encoding="utf-8")

            with _patch.object(projects_module, "PROJECTS_FILE", projects_file):
                loaded = projects_module._load_configured_projects()

        self.assertEqual([item["id"] for item in loaded], ["demo"])

if __name__ == "__main__":
    unittest.main()
