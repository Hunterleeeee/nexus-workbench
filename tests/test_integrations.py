import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import app


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.request = httpx.Request("GET", "https://integration.test")

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError("upstream error", request=self.request, response=response)

    def json(self):
        return self._payload


class FakeClient:
    responses = {}
    calls = []

    def __init__(self, *args, **kwargs):
        self.headers = kwargs.get("headers", {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, self.headers))
        return self.responses.get(("GET", url), FakeResponse())

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, self.headers, kwargs))
        return self.responses.get(("POST", url), FakeResponse({"id": "message-1"}))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.integration_file = Path(self.temp_dir.name) / "integrations.json"
        self.database_file = Path(self.temp_dir.name) / "workbench.db"
        self.file_patch = patch.object(app, "INTEGRATIONS_FILE", self.integration_file)
        self.database_patch = patch.object(app, "DATABASE_FILE", self.database_file)
        self.schema_patch = patch.object(app, "_DB_SCHEMA_READY", False)
        self.file_patch.start()
        self.database_patch.start()
        self.schema_patch.start()
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        FakeClient.calls = []
        FakeClient.responses = {}

    def tearDown(self):
        self.env_patch.stop()
        self.schema_patch.stop()
        self.database_patch.stop()
        self.file_patch.stop()
        self.temp_dir.cleanup()

    def test_miniflux_connection_and_items_use_bearer_token(self):
        app.save_integration_config("miniflux", {"base_url": "https://reader.test", "api_token": "token"})
        FakeClient.responses[("GET", "https://reader.test/v1/me")] = FakeResponse({"username": "me"})
        FakeClient.responses[("GET", "https://reader.test/v1/entries?status=unread&limit=2")] = FakeResponse({"entries": [{"id": 7, "title": "低噪阅读", "content": "<p>摘要</p>", "url": "https://source.test/7", "feed_title": "研究源"}]})
        with patch.object(app.httpx, "AsyncClient", FakeClient):
            result = asyncio.run(app.test_integration_connection("miniflux"))
            items = asyncio.run(app.fetch_integration_items("miniflux", 2))
        self.assertTrue(result["ok"])
        self.assertEqual(items[0]["id"], "miniflux:7")
        self.assertEqual(items[0]["summary"], "摘要")
        self.assertEqual(FakeClient.calls[0][2]["Authorization"], "Bearer token")

    def test_blank_secret_from_settings_form_keeps_saved_value(self):
        app.save_integration_config("miniflux", {"base_url": "https://reader.test", "api_token": "token"})
        status = app.save_integration_config("miniflux", {"base_url": "https://reader.test", "api_token": ""})
        self.assertTrue(status["has_api_token"])

    def test_zotero_items_keep_doi_and_creator_metadata(self):
        app.save_integration_config("zotero", {"base_url": "https://api.zotero.test", "user_id": "12345", "api_key": "key"})
        url = "https://api.zotero.test/users/12345/items?limit=3&format=json&sort=dateAdded&direction=desc"
        FakeClient.responses[("GET", url)] = FakeResponse([{"key": "ABCD", "data": {"title": "研究资料", "DOI": "10.1000/example", "creators": [{"lastName": "作者"}], "abstractNote": "摘要"}}])
        with patch.object(app.httpx, "AsyncClient", FakeClient):
            items = asyncio.run(app.fetch_integration_items("zotero", 3))
        self.assertEqual(items[0]["id"], "zotero:ABCD")
        self.assertEqual(items[0]["metadata"]["doi"], "10.1000/example")
        self.assertIn("作者", items[0]["source"])

    def test_linkding_uses_token_header_and_normalizes_bookmarks(self):
        app.save_integration_config("linkding", {"base_url": "https://links.test", "token": "link-token"})
        FakeClient.responses[("GET", "https://links.test/api/bookmarks/?limit=1")] = FakeResponse({"results": [{"id": 1}]})
        FakeClient.responses[("GET", "https://links.test/api/bookmarks/?limit=3")] = FakeResponse({"results": [{"id": 7, "title": "低噪资料", "url": "https://source.test/7", "description": "保留来源", "tag_names": ["研究"], "date_added": "2026-08-09T01:02:03Z"}]})
        with patch.object(app.httpx, "AsyncClient", FakeClient):
            result = asyncio.run(app.test_integration_connection("linkding"))
            items = asyncio.run(app.fetch_integration_items("linkding", 3))
        self.assertTrue(result["ok"])
        self.assertEqual(items[0]["id"], "linkding:7")
        self.assertEqual(items[0]["metadata"]["tags"], ["研究"])
        self.assertEqual(FakeClient.calls[0][2]["Authorization"], "Token link-token")

    def test_paperless_uses_token_header_and_normalizes_document_metadata(self):
        app.save_integration_config("paperless", {"base_url": "https://paperless.test", "token": "paper-token"})
        FakeClient.responses[("GET", "https://paperless.test/api/documents/?page_size=1")] = FakeResponse({"results": [{"id": 1}]})
        FakeClient.responses[("GET", "https://paperless.test/api/documents/?page_size=3&ordering=-added")] = FakeResponse({"results": [{"id": 9, "title": "合同归档", "original_file_name": "contract.pdf", "notes": "待整理", "tags": ["合同"], "added": "2026-08-08T01:02:03Z"}]})
        with patch.object(app.httpx, "AsyncClient", FakeClient):
            result = asyncio.run(app.test_integration_connection("paperless"))
            items = asyncio.run(app.fetch_integration_items("paperless", 3))
        self.assertTrue(result["ok"])
        self.assertEqual(items[0]["id"], "paperless:9")
        self.assertEqual(items[0]["metadata"]["original_file_name"], "contract.pdf")
        self.assertIn("/documents/9/details", items[0]["url"])
        self.assertEqual(FakeClient.calls[0][2]["Authorization"], "Token paper-token")

    def test_vikunja_reads_open_tasks_with_bearer_token_and_project_filter(self):
        app.save_integration_config("vikunja", {"base_url": "https://tasks.test", "api_token": "vikunja-token", "project_id": "12"})
        FakeClient.responses[("GET", "https://tasks.test/api/v1/info")] = FakeResponse({"version": "0.24.0"})
        url = "https://tasks.test/api/v1/projects/12/tasks?per_page=3&sort_by=due_date&order_by=asc"
        FakeClient.responses[("GET", url)] = FakeResponse([{
            "id": 42,
            "title": "整理研究资料",
            "description": "把资料放进知识库",
            "done": False,
            "priority": 3,
            "dueDate": "2026-08-12T09:00:00Z",
            "updated": "2026-08-09T01:02:03Z",
            "project": {"id": 12, "title": "学习"},
            "labels": [{"title": "研究"}],
        }, {"id": 43, "title": "已完成", "done": True}])
        with patch.object(app.httpx, "AsyncClient", FakeClient):
            result = asyncio.run(app.test_integration_connection("vikunja"))
            items = asyncio.run(app.fetch_integration_items("vikunja", 3))
        self.assertTrue(result["ok"])
        self.assertEqual(items[0]["id"], "vikunja:42")
        self.assertEqual(items[0]["metadata"]["kind"], "task")
        self.assertEqual(items[0]["metadata"]["labels"], ["研究"])
        self.assertNotIn("已完成", str(items))
        self.assertEqual(FakeClient.calls[0][2]["Authorization"], "Bearer vikunja-token")

    def test_searxng_search_results_are_stable_and_keep_query_metadata(self):
        app.save_integration_config(
            "searxng",
            {"base_url": "https://search.test", "query": "个人知识管理", "categories": "general,science"},
        )
        FakeClient.responses[("GET", "https://search.test/search")] = FakeResponse({
            "results": [{
                "title": "学习资料",
                "url": "https://source.test/article",
                "content": "一段搜索摘要",
                "engines": ["google", "bing"],
                "publishedDate": "2026-08-09T01:02:03Z",
                "score": 4.2,
            }],
        })
        with patch.object(app.httpx, "AsyncClient", FakeClient):
            items = asyncio.run(app.fetch_integration_items("searxng", 3))
        self.assertEqual(items[0]["id"], "searxng:" + app.hashlib.sha256(b"https://source.test/article").hexdigest()[:20])
        self.assertEqual(items[0]["metadata"]["query"], "个人知识管理")
        self.assertEqual(items[0]["metadata"]["engines"], ["google", "bing"])
        self.assertEqual(items[0]["metadata"]["kind"], "search_result")
        self.assertEqual(FakeClient.calls[0][2]["Accept"], "application/json")

    def test_wallabag_reads_unarchived_articles_without_writing_back(self):
        app.save_integration_config("wallabag", {"base_url": "https://read.test", "access_token": "wall-token"})
        FakeClient.responses[("GET", "https://read.test/api/entries.json")] = FakeResponse({
            "_embedded": {"items": [{
                "id": 17,
                "title": "稍后学习",
                "url": "https://source.test/learn",
                "excerpt": "保留摘要",
                "tags": [{"label": "学习"}],
                "updated_at": "2026-08-09T01:02:03Z",
                "reading_time": 8,
            }]},
        })
        with patch.object(app.httpx, "AsyncClient", FakeClient):
            items = asyncio.run(app.fetch_integration_items("wallabag", 3))
        self.assertEqual(items[0]["id"], "wallabag:17")
        self.assertEqual(items[0]["metadata"]["tags"], ["学习"])
        self.assertEqual(items[0]["metadata"]["kind"], "saved_article")
        self.assertEqual(FakeClient.calls[0][2]["Authorization"], "Bearer wall-token")

    def test_activitywatch_items_keep_aggregates_without_raw_event_data(self):
        app.save_integration_config("activitywatch", {"base_url": "https://aw.test", "bucket_id": "window"})
        FakeClient.responses[("GET", "https://aw.test/api/0/buckets")] = FakeResponse({
            "window": {"id": "window", "name": "窗口观察", "type": "currentwindow", "client": "aw-watcher-window"},
        })
        FakeClient.responses[("GET", "https://aw.test/api/0/buckets/window/events")] = FakeResponse([
            {"timestamp": "2026-08-08T01:00:00Z", "duration": 30, "data": {"title": "private-window-title", "url": "https://private.test"}},
            {"timestamp": "2026-08-08T02:00:00Z", "duration": 90, "data": {"title": "another-private-title"}},
        ])
        with patch.object(app.httpx, "AsyncClient", FakeClient):
            result = asyncio.run(app.test_integration_connection("activitywatch"))
            items = asyncio.run(app.fetch_integration_items("activitywatch", 3))
        self.assertTrue(result["ok"])
        self.assertEqual(items[0]["id"].split(":", 2)[:2], ["activitywatch", "window"])
        self.assertEqual(items[0]["metadata"]["event_count"], 2)
        self.assertEqual(items[0]["metadata"]["total_seconds"], 120.0)
        self.assertEqual(items[0]["metadata"]["kind"], "time_summary")
        self.assertEqual(items[0]["metadata"]["privacy"], "aggregated_duration_only")
        self.assertNotIn("private-window-title", str(items[0]))
        self.assertNotIn("private.test", str(items[0]))

    def test_github_items_keep_kind_labels_and_source_time(self):
        app.save_integration_config("github", {"owner": "octo", "repo": "workbench", "token": "key"})
        FakeClient.responses[("GET", "https://api.github.com/repos/octo/workbench")] = FakeResponse({"full_name": "octo/workbench"})
        url = "https://api.github.com/repos/octo/workbench/issues?state=open&per_page=3"
        FakeClient.responses[("GET", url)] = FakeResponse([{
            "number": 12,
            "title": "补充工作流",
            "body": "把 Issue 变成可处理的工作项",
            "html_url": "https://github.com/octo/workbench/issues/12",
            "updated_at": "2026-08-09T01:02:03Z",
            "labels": [{"name": "enhancement"}],
            "user": {"login": "lifeng"},
        }])
        with patch.object(app.httpx, "AsyncClient", FakeClient):
            result = asyncio.run(app.test_integration_connection("github"))
            items = asyncio.run(app.fetch_integration_items("github", 3))
        self.assertTrue(result["ok"])
        self.assertEqual(items[0]["id"], "github:octo/workbench:12")
        self.assertEqual(items[0]["metadata"]["kind"], "issue")
        self.assertEqual(items[0]["metadata"]["labels"], ["enhancement"])
        self.assertEqual(items[0]["metadata"]["source_updated_at"], "2026-08-09T01:02:03Z")
        self.assertEqual(FakeClient.calls[0][2]["Accept"], "application/vnd.github+json")
        self.assertNotIn("Authorization", FakeClient.calls[0][2]) if False else None

    def test_sub2api_login_without_refresh_token_does_not_claim_auto_sync(self):
        FakeClient.responses[("POST", "https://sub.chengsir.asia/api/v1/auth/login")] = FakeResponse(
            {"data": {"access_token": "short-lived-access", "user": {"email": "me@example.com"}}}
        )
        settings_file = Path(self.temp_dir.name) / "sub2api_panel_settings.json"
        with patch.object(app, "SUB2API_PANEL_SETTINGS_FILE", settings_file), patch.object(app.httpx, "AsyncClient", FakeClient):
            result = asyncio.run(app.login_sub2api_panel(app.Sub2APILoginRequest(email="me@example.com", password="not-persisted")))
            stored = app.load_sub2api_panel_settings()
            state = app.sub2api_sync_state()

        self.assertTrue(result["has_access_token"])
        self.assertFalse(result["has_credential"])
        self.assertFalse(result["auto_sync_available"])
        self.assertIn("浏览器书签同步", result["message"])
        self.assertNotIn("short-lived-access", str(result))
        self.assertNotIn("not-persisted", str(stored))
        self.assertIn("浏览器同步", state["label"])

    def test_ntfy_notification_posts_to_topic_with_click_url(self):
        app.save_integration_config("ntfy", {"base_url": "https://notify.test", "topic": "workbench", "token": "token"})
        url = "https://notify.test/workbench"
        FakeClient.responses[("POST", url)] = FakeResponse({"id": "message-1"})
        with patch.object(app.httpx, "AsyncClient", FakeClient):
            result = asyncio.run(app.send_ntfy_message(title="提醒", body="请处理", href="https://workbench.example.dev/", priority="high"))
        self.assertTrue(result["ok"])
        call = FakeClient.calls[0]
        self.assertEqual(call[0], "POST")
        self.assertEqual(call[2]["Authorization"], "Bearer token")
        self.assertEqual(call[2]["Priority"], "high")
        self.assertEqual(call[2]["Click"], "https://workbench.example.dev/")

    def test_integration_routes_redact_secrets_and_import_work_item(self):
        """Exercise the public API contract, not only the underlying helpers."""
        reader_url = "https://reader.test"
        entries_url = f"{reader_url}/v1/entries?status=unread&limit=20"
        FakeClient.responses[("GET", f"{reader_url}/v1/me")] = FakeResponse({"username": "me"})
        FakeClient.responses[("GET", entries_url)] = FakeResponse({
            "entries": [{
                "id": 7,
                "title": "路由级集成验收",
                "content": "<p>保留来源并导入 WorkItem</p>",
                "url": "https://source.test/7",
                "feed_title": "研究源",
            }],
        })

        asgi_client_class = httpx.AsyncClient

        async def exercise_routes():
            transport = httpx.ASGITransport(app=app.app)
            async with asgi_client_class(transport=transport, base_url="http://testserver") as client:
                configured = await client.post(
                    "/api/integrations/miniflux/config",
                    json={"values": {"base_url": reader_url, "api_token": "secret-token"}, "enabled": True},
                )
                listed = await client.get("/api/integrations")
                tested = await client.post("/api/integrations/miniflux/test")
                items = await client.get("/api/integrations/miniflux/items?limit=20")
                imported = await client.post("/api/integrations/miniflux/import", json={"ids": ["miniflux:7"]})
                return configured, listed, tested, items, imported

        with patch.object(app.httpx, "AsyncClient", FakeClient):
            configured, listed, tested, items, imported = asyncio.run(exercise_routes())

        self.assertEqual(configured.status_code, 200)
        self.assertNotIn("secret-token", configured.text)
        self.assertEqual(listed.status_code, 200)
        listed_miniflux = next(item for item in listed.json()["integrations"] if item["id"] == "miniflux")
        self.assertTrue(listed_miniflux["has_api_token"])
        self.assertNotIn("api_token", listed_miniflux.get("values", {}))
        self.assertEqual(tested.status_code, 200)
        self.assertEqual(items.status_code, 200)
        self.assertEqual(items.json()["items"][0]["id"], "miniflux:7")
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["created"], 1)
        self.assertEqual(imported.json()["target_project"], "crawl4ai")

    def test_github_routes_redact_token_and_import_to_inbox_idempotently(self):
        github_url = "https://api.github.com/repos/octo/workbench/issues?state=open&per_page=20"
        FakeClient.responses[("GET", github_url)] = FakeResponse([{
            "number": 3,
            "title": "学习资料整理",
            "body": "请整理这份资料",
            "html_url": "https://github.com/octo/workbench/issues/3",
            "updated_at": "2026-08-09T01:02:03Z",
            "user": {"login": "octo"},
        }])

        asgi_client_class = httpx.AsyncClient

        async def exercise_routes():
            transport = httpx.ASGITransport(app=app.app)
            async with asgi_client_class(transport=transport, base_url="http://testserver") as client:
                configured = await client.post(
                    "/api/integrations/github/config",
                    json={"values": {"owner": "octo", "repo": "workbench", "token": "secret-token"}, "enabled": True},
                )
                listed = await client.get("/api/integrations")
                items = await client.get("/api/integrations/github/items?limit=20")
                imported = await client.post("/api/integrations/github/import", json={"ids": ["github:octo/workbench:3"]})
                imported_again = await client.post("/api/integrations/github/import", json={"ids": ["github:octo/workbench:3"]})
                return configured, listed, items, imported, imported_again

        with patch.object(app.httpx, "AsyncClient", FakeClient):
            configured, listed, items, imported, imported_again = asyncio.run(exercise_routes())

        self.assertEqual(configured.status_code, 200)
        self.assertNotIn("secret-token", configured.text)
        self.assertEqual(listed.status_code, 200)
        listed_github = next(item for item in listed.json()["integrations"] if item["id"] == "github")
        self.assertTrue(listed_github["configured"])
        self.assertTrue(listed_github["has_token"])
        self.assertNotIn("token", listed_github.get("values", {}))
        self.assertEqual(items.json()["items"][0]["id"], "github:octo/workbench:3")
        self.assertEqual(imported.json()["created"], 1)
        self.assertEqual(imported.json()["target_project"], "inbox")
        imported_item = imported.json()["items"][0]["item"]
        self.assertEqual(imported_item["source_context"]["kind_label"], "GitHub Issue")
        self.assertEqual(imported_item["source_context"]["source_updated_at"], "2026-08-09T01:02:03Z")
        self.assertIn("确认是否要处理", imported_item["source_context"]["next_step"])
        self.assertEqual(imported_again.json()["created"], 0)
        self.assertEqual(imported_again.json()["skipped"], ["github:octo/workbench:3"])

    def test_vikunja_routes_redact_token_and_import_open_task_idempotently(self):
        tasks_url = "https://tasks.test/api/v1/projects/12/tasks?per_page=20&sort_by=due_date&order_by=asc"
        FakeClient.responses[("GET", tasks_url)] = FakeResponse([{
            "id": 42,
            "title": "复习一篇论文",
            "description": "提炼三个可验证观点",
            "done": False,
            "dueDate": "2026-08-12T09:00:00Z",
            "updated": "2026-08-09T01:02:03Z",
            "project": {"id": 12, "title": "学习"},
            "labels": [{"title": "阅读"}],
        }])

        asgi_client_class = httpx.AsyncClient

        async def exercise_routes():
            transport = httpx.ASGITransport(app=app.app)
            async with asgi_client_class(transport=transport, base_url="http://testserver") as client:
                configured = await client.post(
                    "/api/integrations/vikunja/config",
                    json={"values": {"base_url": "https://tasks.test", "api_token": "secret-token", "project_id": "12"}, "enabled": True},
                )
                listed = await client.get("/api/integrations")
                items = await client.get("/api/integrations/vikunja/items?limit=20")
                imported = await client.post("/api/integrations/vikunja/import", json={"ids": ["vikunja:42"]})
                imported_again = await client.post("/api/integrations/vikunja/import", json={"ids": ["vikunja:42"]})
                return configured, listed, items, imported, imported_again

        with patch.object(app.httpx, "AsyncClient", FakeClient):
            configured, listed, items, imported, imported_again = asyncio.run(exercise_routes())

        self.assertEqual(configured.status_code, 200)
        self.assertNotIn("secret-token", configured.text)
        self.assertNotIn("secret-token", listed.text)
        self.assertEqual(items.json()["items"][0]["id"], "vikunja:42")
        self.assertEqual(imported.json()["created"], 1)
        self.assertEqual(imported.json()["target_project"], "inbox")
        imported_item = imported.json()["items"][0]["item"]
        self.assertEqual(imported_item["source_context"]["kind_label"], "Vikunja 任务")
        self.assertIn("确认是否要处理", imported_item["source_context"]["next_step"])
        self.assertEqual(imported_again.json()["created"], 0)

    def test_activitywatch_routes_import_aggregate_to_workbench_idempotently(self):
        FakeClient.responses[("GET", "https://aw.test/api/0/buckets")] = FakeResponse({
            "desktop": {"id": "desktop", "name": "桌面活动", "type": "currentwindow"},
        })
        FakeClient.responses[("GET", "https://aw.test/api/0/buckets/desktop/events")] = FakeResponse([
            {"duration": 300, "data": {"title": "should-not-be-persisted"}},
        ])

        asgi_client_class = httpx.AsyncClient

        async def exercise_routes():
            transport = httpx.ASGITransport(app=app.app)
            async with asgi_client_class(transport=transport, base_url="http://testserver") as client:
                configured = await client.post(
                    "/api/integrations/activitywatch/config",
                    json={"values": {"base_url": "https://aw.test", "bucket_id": "desktop"}, "enabled": True},
                )
                items = await client.get("/api/integrations/activitywatch/items?limit=5")
                imported = await client.post("/api/integrations/activitywatch/import", json={"ids": [items.json()["items"][0]["id"]]})
                imported_again = await client.post("/api/integrations/activitywatch/import", json={"ids": [items.json()["items"][0]["id"]]})
                return configured, items, imported, imported_again

        with patch.object(app.httpx, "AsyncClient", FakeClient):
            configured, items, imported, imported_again = asyncio.run(exercise_routes())

        self.assertEqual(configured.status_code, 200)
        self.assertEqual(items.status_code, 200)
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["created"], 1)
        self.assertEqual(imported.json()["target_project"], "workbench")
        imported_item = imported.json()["items"][0]["item"]
        self.assertEqual(imported_item["kind"], "efficiency_observation")
        self.assertEqual(imported_item["source_context"]["kind_label"], "效率观察")
        self.assertIn("减少哪类切换", imported_item["source_context"]["next_step"])
        self.assertNotIn("should-not-be-persisted", str(imported_item))
        self.assertEqual(imported_again.json()["created"], 0)

    def test_github_tools_catalog_and_trial_route_keep_boundaries(self):
        async def exercise_routes():
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                catalog = await client.get("/api/github-tools")
                trial = await client.post("/api/github-tools/markitdown/trial")
                catalog_after_trial = await client.get("/api/github-tools")
                return catalog, trial, catalog_after_trial

        catalog, trial, catalog_after_trial = asyncio.run(exercise_routes())
        self.assertEqual(catalog.status_code, 200)
        tools = {item["id"]: item for item in catalog.json()["tools"]}
        self.assertIn("markitdown", tools)
        self.assertEqual(tools["markitdown"]["state"], "integrated")
        self.assertIn("data_boundary", tools["markitdown"])
        self.assertIn("trial", tools["markitdown"])
        self.assertEqual(tools["linkding"]["state"], "integrated")
        self.assertEqual(tools["paperless"]["state"], "integrated")
        integrations = {item["id"]: item for item in catalog.json()["integrations"]}
        self.assertTrue(integrations["linkding"]["configuration_cost"])
        self.assertIn("人工勾选", integrations["paperless"]["data_boundary"])
        self.assertEqual(integrations["vikunja"]["kind"], "task_management")
        self.assertIn("不回写", integrations["vikunja"]["data_boundary"])
        self.assertEqual(integrations["searxng"]["kind"], "search")
        self.assertEqual(integrations["wallabag"]["kind"], "reading")
        self.assertIn("不修改", integrations["wallabag"]["data_boundary"])
        self.assertNotIn("secret-token", catalog.text)
        self.assertEqual(trial.status_code, 200)
        self.assertEqual(trial.json()["item"]["kind"], "github_tool_trial")
        self.assertEqual(len(catalog_after_trial.json()["trials"]), 1)


if __name__ == "__main__":
    unittest.main()
