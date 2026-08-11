import unittest
import asyncio
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
        self.assertIn("/api/market/backtest/walk-forward", market_js)
        self.assertIn("/api/market/sampling", market_js)
        self.assertIn("legacy-tools", market)
        cloud_dev = (root / "static" / "cloud-dev.html").read_text(encoding="utf-8")
        self.assertIn('event.submitter || $("#cloud-form button[type=submit]")', cloud_dev)
        self.assertIn('id="cloud-readiness"', cloud_dev)
        self.assertIn("WORKBENCH_FEISHU_APP_ID/SECRET", cloud_dev)
        self.assertIn("WORKBENCH_FEISHU_VERIFY_TOKEN/ENCRYPT_KEY", cloud_dev)

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


if __name__ == "__main__":
    unittest.main()
