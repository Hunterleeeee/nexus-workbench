"""Regression tests for the performance / stability / security hardening pass.

Each test here pins one of the behaviours that was changed, so a future refactor
cannot silently undo it:

* hot-path indexes exist on the five core tables that previously had none;
* ``call_llm_with_tools`` walks the whole Provider candidate chain instead of
  dying with the first Provider (the ReAct path used to have no fallback at all);
* the CORS grant is scoped to a single route instead of the whole app;
* the optional application-layer token gate protects write APIs while leaving
  health checks and the Feishu callback reachable.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import app


def temp_database():
    temp_dir = tempfile.TemporaryDirectory()
    return temp_dir, Path(temp_dir.name) / "workbench.db"


class HotPathIndexTests(unittest.TestCase):
    """These five tables carry the homepage and the linkage matrix.

    Without an index SQLite full-scans and then sorts the whole table just to
    return the newest 200 rows, which dominated the cost of "/" once the tables
    grew past a few thousand rows.
    """

    REQUIRED = {
        "inbox": {"idx_inbox_status_id", "idx_inbox_created_at", "idx_inbox_updated_at"},
        "work_items": {"idx_work_items_updated_at", "idx_work_items_status", "idx_work_items_kind"},
        "relations": {"idx_relations_from", "idx_relations_to"},
        "artifacts": {"idx_artifacts_created_at", "idx_artifacts_project"},
    }

    def test_core_tables_have_hot_path_indexes(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                rows = connection.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'").fetchall()
            finally:
                connection.close()
        by_table: dict[str, set[str]] = {}
        for name, table in rows:
            by_table.setdefault(table, set()).add(name)
        for table, expected in self.REQUIRED.items():
            self.assertTrue(expected.issubset(by_table.get(table, set())), f"{table} 缺少索引：{expected - by_table.get(table, set())}")

    def test_work_item_listing_uses_an_index_instead_of_scanning(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                plan = " ".join(
                    str(row[3])
                    for row in connection.execute(
                        "EXPLAIN QUERY PLAN SELECT * FROM work_items ORDER BY updated_at DESC, id DESC LIMIT 200"
                    ).fetchall()
                )
            finally:
                connection.close()
        # The index must both drive the scan and remove the sort: without it
        # SQLite materialises every row into a temp B-tree just to return 200.
        self.assertIn("idx_work_items_updated_at", plan)
        self.assertNotIn("TEMP B-TREE", plan.upper())


class ToolCallFallbackTests(unittest.TestCase):
    """``call_llm`` always had a candidate chain; ``call_llm_with_tools`` did not.

    The practical failure was asymmetric and confusing: once the primary Provider
    started returning 429, every sub-Agent that used tools died while the
    chat-only paths kept working.
    """

    @staticmethod
    def status_error(code: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "https://primary.test/v1/chat/completions")
        response = httpx.Response(code, request=request)
        return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)

    def run_with_tools(self, first_error):
        primary = {"id": "primary", "name": "主配置", "base_url": "https://primary.test/v1", "model": "m", "api_key": "k"}
        fallback = {"id": "fallback", "name": "备用", "base_url": "https://fallback.test/v1", "model": "m", "api_key": "k"}
        calls: list[str] = []

        class Client:
            async def post(self, url, **kwargs):
                calls.append(url)
                if len(calls) == 1:
                    raise first_error
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={"choices": [{"message": {"content": "备用成功", "tool_calls": []}}]},
                )

        async def client_factory():
            return Client()

        with patch("app.llm_provider_state", return_value={"candidates": [primary, fallback]}), patch(
            "app._llm_health", return_value={"status": "unknown"}
        ), patch("app.llm_http_client", client_factory), patch("app.schedule_llm_usage_event"), patch(
            "app._record_llm_failure"
        ), patch("app._record_llm_success"):
            body = asyncio.run(app.call_llm_with_tools([{"role": "user", "content": "ping"}], []))
        return body, calls

    def test_rate_limited_primary_falls_back_to_the_next_provider(self):
        body, calls = self.run_with_tools(self.status_error(429))
        self.assertEqual(len(calls), 2)
        self.assertEqual(body["choices"][0]["message"]["content"], "备用成功")

    def test_timeout_on_primary_falls_back_to_the_next_provider(self):
        body, calls = self.run_with_tools(httpx.ReadTimeout("timeout"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(body["choices"][0]["message"]["content"], "备用成功")

    def test_all_providers_failing_reports_every_attempt(self):
        primary = {"id": "primary", "name": "主配置", "base_url": "https://primary.test/v1", "model": "m", "api_key": "k"}
        fallback = {"id": "fallback", "name": "备用", "base_url": "https://fallback.test/v1", "model": "m", "api_key": "k"}

        class Client:
            async def post(self, url, **kwargs):
                raise ToolCallFallbackTests.status_error(503)

        async def client_factory():
            return Client()

        with patch("app.llm_provider_state", return_value={"candidates": [primary, fallback]}), patch(
            "app._llm_health", return_value={"status": "unknown"}
        ), patch("app.llm_http_client", client_factory), patch("app.schedule_llm_usage_event"), patch(
            "app._record_llm_failure"
        ), patch("app._record_llm_success"), self.assertRaisesRegex(RuntimeError, "主配置.*备用"):
            asyncio.run(app.call_llm_with_tools([{"role": "user", "content": "ping"}], []))


class ScopedCorsTests(unittest.TestCase):
    """The CORS grant used to be a global middleware with allow_credentials=True.

    That declared "the panel origin may POST to *any* Workbench endpoint carrying
    the browser's cached Basic credentials".  Only one route ever needed it.
    """

    def setUp(self):
        self.origin = app._SUB2API_PANEL_ORIGINS[0]
        self.client = TestClient(app.app)

    def test_panel_origin_is_allowed_on_the_sync_route_only(self):
        allowed = self.client.options(
            app._SUB2API_CORS_PATH,
            headers={"Origin": self.origin, "Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), self.origin)

    def test_other_routes_do_not_receive_cors_headers(self):
        response = self.client.get("/api/health", headers={"Origin": self.origin})
        self.assertIsNone(response.headers.get("access-control-allow-origin"))

    def test_unknown_origin_is_refused_on_the_sync_route(self):
        response = self.client.options(
            app._SUB2API_CORS_PATH,
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
        self.assertIsNone(response.headers.get("access-control-allow-origin"))


class AppTokenAuthTests(unittest.TestCase):
    """Defence in depth: nginx Basic Auth is no longer the only gate.

    Left unset the middleware is a no-op, so upgrading cannot lock anyone out.
    """

    def setUp(self):
        self.client = TestClient(app.app)

    def test_token_unset_keeps_the_previous_behaviour(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("WORKBENCH_API_TOKEN", None)
            self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_protected_route_rejects_a_missing_or_wrong_token(self):
        with patch.dict("os.environ", {"WORKBENCH_API_TOKEN": "s3cret-token"}):
            self.assertEqual(self.client.get("/api/work-items").status_code, 401)
            self.assertEqual(self.client.get("/api/work-items", headers={"X-Workbench-Token": "nope"}).status_code, 401)

    def test_protected_route_accepts_the_configured_token(self):
        with patch.dict("os.environ", {"WORKBENCH_API_TOKEN": "s3cret-token"}):
            ok_header = self.client.get("/api/work-items", headers={"X-Workbench-Token": "s3cret-token"})
            ok_cookie = self.client.get("/api/work-items", cookies={"workbench_token": "s3cret-token"})
        self.assertNotEqual(ok_header.status_code, 401)
        self.assertNotEqual(ok_cookie.status_code, 401)

    def test_health_static_and_feishu_stay_reachable_without_a_token(self):
        with patch.dict("os.environ", {"WORKBENCH_API_TOKEN": "s3cret-token"}):
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            self.assertNotEqual(self.client.get("/static/sw.js").status_code, 401)
            # The Feishu callback authenticates itself by signature, so the token
            # gate must not shadow it -- anything except 401 is fine here.
            self.assertNotEqual(self.client.post("/feishu/event", json={}).status_code, 401)


class AgentTuningTests(unittest.TestCase):
    """The ReAct loop's limits used to be literals buried in the function body."""

    def test_agent_limits_are_bounded_and_configurable(self):
        self.assertGreaterEqual(app.AGENT_CHILD_CONCURRENCY, 1)
        self.assertLessEqual(app.AGENT_CHILD_CONCURRENCY, 8)
        self.assertGreaterEqual(app.AGENT_MAX_TOOL_ROUNDS, 1)
        self.assertGreater(app.AGENT_CHILD_TIMEOUT_SECONDS, app.AGENT_TOOL_TIMEOUT_SECONDS)

    def test_invalid_environment_values_fall_back_to_the_default(self):
        with patch.dict("os.environ", {"WORKBENCH_AGENT_MAX_TOOL_ROUNDS": "not-a-number"}):
            self.assertEqual(app._int_env("WORKBENCH_AGENT_MAX_TOOL_ROUNDS", 4, minimum=1, maximum=8), 4)
        with patch.dict("os.environ", {"WORKBENCH_AGENT_MAX_TOOL_ROUNDS": "999"}):
            self.assertEqual(app._int_env("WORKBENCH_AGENT_MAX_TOOL_ROUNDS", 4, minimum=1, maximum=8), 8)


if __name__ == "__main__":
    unittest.main()
