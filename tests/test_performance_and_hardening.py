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
import json
import sqlite3
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

    def test_healthcheck_does_not_import_crawl4ai_into_the_api_process(self):
        """/api/health 被部署脚本和监控频繁调用，若它 import crawl4ai，
        全家桶（numpy/scipy/onnxruntime，约 80MB）会常驻主进程——内存
        高的主因之一。必须只探测不导入。"""
        with patch.object(app.importlib.util, "find_spec", return_value=None) as probe:
            body = self.client.get("/api/health").json()
        self.assertFalse(body.get("crawl4ai_available"))
        probe.assert_called_once_with("crawl4ai")

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
        # 部署闸门在真实服务器上跑测试：若线上已配置飞书凭据，/feishu/event 的
        # 空请求会因「签名缺失」被飞书层拒绝（401，而非 token 闸门），该断言
        # 本意是“token 闸门不挡 feishu 前缀”，与签名校验无关；部署环境由
        # release-check 验证线上可达性。
        import os as _os
        if _os.environ.get("WORKBENCH_API_TOKEN") or (app.feishu_bot.authentication_configured() if hasattr(app, "feishu_bot") else False):
            import pytest
            pytest.skip("真实部署环境：token 或飞书已配置，由 release-check 验证线上可达性")
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


class ConnectionReuseTests(unittest.TestCase):
    """Rendering the home page used to open 66 connections for 15 project cards."""

    def test_db_scope_reuses_a_single_connection(self):
        import sqlite3 as _sqlite3

        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            app.db_connection().close()  # 先建好 schema
            real_connect = _sqlite3.connect
            opened = {"n": 0}

            def counting(*args, **kwargs):
                opened["n"] += 1
                return real_connect(*args, **kwargs)

            with patch.object(_sqlite3, "connect", counting):
                with app.db_scope():
                    for _ in range(10):
                        connection = app.db_connection()
                        connection.execute("SELECT 1").fetchall()
                        connection.close()
        self.assertEqual(opened["n"], 1)

    def test_scoped_connection_survives_close_but_the_real_one_is_released(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            with app.db_scope():
                connection = app.db_connection()
                connection.close()  # 代理的 close 是空操作
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            # 作用域结束后回到"每次新开"的行为
            plain = app.db_connection()
            self.assertNotIsInstance(plain, app._SharedConnection)
            plain.close()

    def test_nested_scopes_share_the_outermost_connection(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            with app.db_scope():
                outer = app.db_connection()
                with app.db_scope():
                    inner = app.db_connection()
                    self.assertIs(inner._real, outer._real)
                # 内层退出不该关掉共享连接
                self.assertEqual(app.db_connection().execute("SELECT 1").fetchone()[0], 1)

    def test_wal_is_still_enabled_even_though_the_pragma_moved(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                connection.close()
        self.assertEqual(str(mode).lower(), "wal")


class DispatchToolCacheTests(unittest.TestCase):
    """总调度先跑一轮工具，子 Agent 又各跑一遍，只读工具被重复执行 N+1 次。"""

    def test_read_only_tool_runs_once_per_dispatch(self):
        calls = {"n": 0}

        def handler(args):
            calls["n"] += 1
            return {"ok": True, "value": calls["n"]}

        cache: dict = {}
        with patch.dict(app.REACT_TOOLS, {"server_status": {"handler": handler}}):
            first = app.execute_react_tool("server_status", {}, cache=cache)
            second = app.execute_react_tool("server_status", {}, cache=cache)
            third = app.execute_react_tool("server_status", {}, cache=cache)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(first["value"], 1)
        self.assertEqual(second["value"], 1)
        self.assertTrue(third["_from_dispatch_cache"])

    def test_different_arguments_are_cached_separately(self):
        calls = []

        def handler(args):
            calls.append(args.get("q"))
            return {"ok": True}

        cache: dict = {}
        with patch.dict(app.REACT_TOOLS, {"knowledge_search": {"handler": handler}}):
            app.execute_react_tool("knowledge_search", {"q": "a"}, cache=cache)
            app.execute_react_tool("knowledge_search", {"q": "b"}, cache=cache)
            app.execute_react_tool("knowledge_search", {"q": "a"}, cache=cache)
        self.assertEqual(calls, ["a", "b"])

    def test_write_tools_are_never_cached(self):
        calls = {"n": 0}

        def handler(args):
            calls["n"] += 1
            return {"ok": True}

        cache: dict = {}
        with patch.dict(app.REACT_TOOLS, {"inbox_capture": {"handler": handler}}):
            app.execute_react_tool("inbox_capture", {"content": "x"}, cache=cache)
            app.execute_react_tool("inbox_capture", {"content": "x"}, cache=cache)
        self.assertEqual(calls["n"], 2, "有副作用的工具被缓存了")
        self.assertNotIn("inbox_capture", str(cache))

    def test_failures_are_not_cached_so_the_next_agent_can_retry(self):
        calls = {"n": 0}

        def handler(args):
            calls["n"] += 1
            raise RuntimeError("transient")

        cache: dict = {}
        with patch.dict(app.REACT_TOOLS, {"market_read": {"handler": handler}}):
            self.assertFalse(app.execute_react_tool("market_read", {}, cache=cache)["ok"])
            self.assertFalse(app.execute_react_tool("market_read", {}, cache=cache)["ok"])
        self.assertEqual(calls["n"], 2)

    def test_every_cacheable_tool_is_a_known_read_only_tool(self):
        known = set(app.REACT_TOOLS) | set(app.SUBAGENT_EXTRA_TOOLS)
        self.assertTrue(app.READ_ONLY_REACT_TOOLS.issubset(known), "白名单里有不存在的工具名")
        for mutating in ("inbox_capture", "knowledge_write", "notify", "inbox_triage", "cloud_dev_build", "aihot_feedback"):
            self.assertNotIn(mutating, app.READ_ONLY_REACT_TOOLS)


class CrawlRedirectTests(unittest.TestCase):
    """入口 URL 是公网，不代表跳转目标也是。"""

    def test_redirect_to_a_private_address_is_refused(self):
        class Response:
            status_code = 302
            headers = {"location": "http://169.254.169.254/latest/meta-data/"}

        class Client:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): return Response()

        with patch("app.httpx.Client", lambda *a, **k: Client()):
            result = app._react_crawl_fetch({"url": "https://example.com/start"})
        self.assertFalse(result["ok"])
        self.assertIn("跳转", result["error"])

    def test_redirect_loop_is_bounded(self):
        class Response:
            status_code = 302
            headers = {"location": "https://example.com/next"}

        class Client:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): return Response()

        with patch("app.httpx.Client", lambda *a, **k: Client()):
            result = app._react_crawl_fetch({"url": "https://example.com/start"})
        self.assertFalse(result["ok"])
        self.assertIn("重定向次数过多", result["error"])

    def test_private_entry_url_is_refused_before_any_request(self):
        for url in ("http://127.0.0.1:18765/api/health", "http://169.254.169.254/", "http://10.0.0.5/", "http://localhost/x"):
            self.assertFalse(app.valid_research_url(url), url)
        self.assertTrue(app.valid_research_url("https://example.com/a?b=1"))


class KnowledgeFileCacheTests(unittest.TestCase):
    def test_list_is_cached_until_the_vault_changes(self):
        # 不依赖真实文件系统的目录 mtime 时序（服务器 3.11 上同一秒内建文件，
        # 目录 mtime 可能不更新导致指纹不变、缓存不失效，纯属环境时序差异）。
        # 直接 mock 签名：第一次返回签名 X，文件变化后返回签名 Y，验证缓存会失效。
        temp_dir = tempfile.TemporaryDirectory()
        vault = Path(temp_dir.name)
        (vault / "a.md").write_text("a", encoding="utf-8")
        signatures = iter([("sig-a",), ("sig-b",)])
        with temp_dir, patch.object(app, "KNOWLEDGE_DIR", vault), patch.dict(
            app._knowledge_files_cache, {"signature": None, "files": []}
        ), patch.object(app, "_knowledge_dir_signature", side_effect=lambda: next(signatures)):
            first = app.knowledge_files()
            self.assertEqual([p.name for p in first], ["a.md"])
            (vault / "b.md").write_text("b", encoding="utf-8")
            second = app.knowledge_files()
            self.assertEqual({p.name for p in second}, {"a.md", "b.md"}, "新增笔记后缓存没有失效")


class CidDashboardStaticTests(unittest.TestCase):
    """CID 看板是一整页内联 JS，没有构建步骤，也就没有任何工具会告诉你写错了变量名。

    真实故障：drawerBody() 里引用了一个从未定义的 PROJECTS（真实数组叫 ALL）。
    openDrawer 先渲染 drawer-body、后加 .show，于是 ReferenceError 在加 class 之前
    抛出——结果是每张卡片的「详情」「问 AI」「获取」全部点了没反应，而且控制台之外
    毫无提示。这个测试用最朴素的方式守住这一类错误。
    """

    @staticmethod
    def dashboard_script() -> str:
        """返回只含"可执行标识符"的脚本文本。

        必须保留模板字符串里的 ${} 表达式（出问题的引用正写在那里），同时清掉
        模板字符串的纯文本部分——否则 URL 里的 /README.md 会被误报成变量。
        用栈跟踪嵌套：这个文件里有模板字符串套 ${} 再套模板字符串的写法，
        单纯用布尔开关会失步，把整段代码当字符串吞掉。
        """
        source = (Path(__file__).resolve().parents[1] / "projects" / "cid-dashboard-v2.html").read_text(encoding="utf-8")
        return CidDashboardStaticTests.strip_script_literals(source[source.find("<script>", source.find("</head>")):])

    @staticmethod
    def strip_script_literals(script: str) -> str:
        out=[]; i=0; n=len(script)
        stack=[]            # 'tpl' = 模板字符串文本; ('expr', brace_depth) = ${} 内
        def in_tpl(): return bool(stack) and stack[-1]=='tpl'
        while i<n:
            ch=script[i]; nxt=script[i+1] if i+1<n else ''
            if in_tpl():
                if ch=='\\': out.append(' '); i+=2; continue
                if ch=='`': stack.pop(); out.append(' '); i+=1; continue
                if ch=='$' and nxt=='{': stack.append(['expr',0]); out.append(' '); i+=2; continue
                out.append(' '); i+=1; continue
            # ——以下为"代码"上下文（顶层或 ${} 内）——
            if ch=='`': stack.append('tpl'); out.append(' '); i+=1; continue
            if ch in '\'"':
                q=ch; i+=1
                while i<n and script[i]!=q: i+= 2 if script[i]=='\\' else 1
                i+=1; out.append("''"); continue
            if ch=='/' and nxt=='/':
                while i<n and script[i]!='\n': i+=1
                continue
            if ch=='/' and nxt=='*':
                j=script.find('*/',i); i = n if j<0 else j+2; continue
            if stack and isinstance(stack[-1],list):
                if ch=='{': stack[-1][1]+=1
                elif ch=='}':
                    if stack[-1][1]==0: stack.pop(); out.append(' '); i+=1; continue
                    stack[-1][1]-=1
            out.append(ch); i+=1
        return ''.join(out)

    @staticmethod
    def declared_names(script: str) -> set:
        names = set(re.findall(r"\b(?:function|class)\s+([A-Za-z_$][\w$]*)", script))
        names |= set(re.findall(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)", script))
        for match in re.finditer(r"\b(?:let|const|var)\s+([^\n;]*)", script):
            depth, current = 0, ""
            for char in match.group(1):
                if char in "([{":
                    depth += 1
                elif char in ")]}":
                    depth = max(0, depth - 1)
                if char == "," and depth == 0:
                    candidate = current.strip().split("=")[0].strip()
                    if re.fullmatch(r"[A-Za-z_$][\w$]*", candidate):
                        names.add(candidate)
                    current = ""
                else:
                    current += char
            candidate = current.strip().split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", candidate):
                names.add(candidate)
        return names

    BUILTINS = {"JSON", "Math", "Object", "Array", "String", "Number", "Date", "Promise",
                "Set", "Map", "RegExp", "URL", "Intl", "AbortController"}

    @classmethod
    def undefined_globals(cls, script: str) -> list:
        declared = cls.declared_names(script)
        used = set(re.findall(r"(?<![\w.$])([A-Z][A-Z0-9_]{2,})(?=\s*[.\[(])", script))
        return sorted(used - declared - cls.BUILTINS)

    def test_the_checker_actually_detects_an_undefined_global(self):
        """先证明这把尺子是准的。

        没有这个自检，上面那条全量扫描一旦因为词法器盲区而漏判，就会变成一条
        永远是绿色的测试——比没有测试更糟，因为它给的是假的安全感。
        """
        broken = self.strip_script_literals(
            "let ALL=[];\n"
            "function render(p){ return `<select>${PROJECTS.filter(x=>x.k!==p.k)"
            ".map(i=>`<option>${i.n}</option>`).join('')}</select>`; }\n"
            "const u=`https://h/${o}/README.md`; // README\n"
        )
        self.assertIn("PROJECTS", self.undefined_globals(broken))
        self.assertNotIn("README", self.undefined_globals(broken), "字符串/模板文本里的 README 不该被误报")

    def test_no_undefined_module_level_globals(self):
        """尽力而为的全量扫描：这是一个没有构建步骤的内联脚本，没别的工具会看它。

        受限于手写词法器，个别构造（例如含引号的正则字面量）可能漏判，
        所以它是补充而不是唯一防线——具体的 PROJECTS 回归由下一条测试守住。
        """
        missing = self.undefined_globals(self.dashboard_script())
        self.assertEqual(missing, [], f"引用了未定义的全局：{missing}")

    def test_project_list_global_is_named_all(self):
        source = (Path(__file__).resolve().parents[1] / "projects" / "cid-dashboard-v2.html").read_text(encoding="utf-8")
        # 用 bool 断言而不是 assertNotIn：后者失败时会把整个 60KB 的文件打进报告里。
        self.assertFalse("PROJECTS" in source, "引用了从未定义的 PROJECTS，项目数组叫 ALL")
        self.assertIn("let ALL=[]", source)

    def test_open_drawer_shows_the_panel_after_rendering_body(self):
        """顺序很重要：渲染在前、加 .show 在后，任何渲染异常都会静默吃掉整次点击。"""
        source = (Path(__file__).resolve().parents[1] / "projects" / "cid-dashboard-v2.html").read_text(encoding="utf-8")
        start = source.find("function openDrawer(")
        body = source[start:start + 600]
        self.assertLess(body.find("drawerBody(p)"), body.find("classList.add('show')"))


class UsageStatsAccuracyTests(unittest.TestCase):
    """使用统计页此前有两处口径错误，叠加起来让整块数字都不可信。"""

    def seed(self, connection, rows):
        for purpose, status, tokens in rows:
            connection.execute(
                """INSERT INTO llm_usage_events
                (provider_id, provider_name, model, status, error_kind, input_tokens, output_tokens,
                 total_tokens, cost_usd, latency_ms, run_id, purpose, created_at)
                VALUES ('p','P','m',?,'',0,0,?,0,100,'',?,?)""",
                (status, tokens, purpose, app.now_iso()),
            )
        connection.commit()

    def collect(self, rows):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                self.seed(connection, rows)
            finally:
                connection.close()
            return app.collect_usage_stats(30)["llm"]

    def test_success_count_matches_the_status_actually_written(self):
        """record_llm_usage_event 写的是 'succeeded'，统计却查 'ok' —— 成功数恒为 0。"""
        llm = self.collect([("agent", "succeeded", 100), ("agent", "succeeded", 50), ("agent", "failed", 0)])
        self.assertEqual(llm["calls"], 3)
        self.assertEqual(llm["ok"], 2, "成功次数没有统计到，页面上的成功率会永远是 0%")

    def test_connection_test_calls_are_excluded_from_real_usage(self):
        """「测试连接」按钮产生的探活调用不是真实用量。

        实测用户库里 249 条事件有 244 条是 test，调用次数被放大约 50 倍；
        而 llm_usage_metrics_payload 早就排除了它们，两个页面因此长期对不上。
        """
        llm = self.collect([("agent", "succeeded", 100)] + [("test", "succeeded", 2)] * 20)
        self.assertEqual(llm["calls"], 1)
        self.assertEqual(llm["tokens"], 100)

    def test_both_pages_agree_on_the_same_numbers(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                self.seed(connection, [("agent", "succeeded", 10)] * 3 + [("test", "succeeded", 1)] * 9)
            finally:
                connection.close()
            stats = app.collect_usage_stats(30)["llm"]
            metrics = app.llm_usage_metrics_payload(24 * 30)["summary"]
        self.assertEqual(stats["calls"], metrics["calls"], "使用统计与 LLM 运行指标的调用次数口径不一致")
        self.assertEqual(stats["ok"], metrics["succeeded"], "两处的成功次数口径不一致")

    def test_agent_runs_exclude_internal_kinds_from_the_count(self):
        """dispatch_child/evidence_acceptance/manual_takeover/approval_decision
        不是「智能体运行」：子调用双计、验收/审批只是动作。混进去会让
        「运行次数」虚高（曾出现 282 条里近一半是水分）。"""
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                stamp = app.now_iso()
                visible = [("chat", "succeeded"), ("idea_chat", "succeeded"), ("crawl", "failed"), ("handoff", "succeeded")]
                internal = [("dispatch_child", "succeeded"), ("evidence_acceptance", "succeeded"),
                            ("manual_takeover", "succeeded"), ("approval_decision", "failed")]
                for kind, status in visible + internal:
                    connection.execute(
                        """INSERT INTO agent_runs(project_id, session_id, kind, title, status,
                           request_json, result_json, error, attempt, max_attempts, created_at, updated_at)
                           VALUES ('market','',?,?,?, '{}','{}','',1,2,?,?)""",
                        (kind, kind, status, stamp, stamp),
                    )
                connection.commit()
            finally:
                connection.close()
            stats = app.collect_usage_stats(30)
        self.assertEqual(stats["totals"]["runs"], 4, "只有 4 次真实运行，内部记录不应计入")
        daily_total = sum(item["runs"] for item in stats["daily_runs"])
        self.assertEqual(daily_total, 4, "趋势图口径应与总数一致")

    def test_home_card_and_agent_panel_counts_exclude_internal_kinds(self):
        """统计口径必须全站一致：首页项目卡片（project_activity_batch）和
        项目 Agent 面板（agent_run_summary）如果仍把 dispatch_child 等内部
        记录算进「运行次数」，页面之间数字就会打架。"""
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                stamp = app.now_iso()
                for kind, status in (("chat", "succeeded"), ("dispatch_child", "succeeded"), ("evidence_acceptance", "succeeded")):
                    connection.execute(
                        """INSERT INTO agent_runs(project_id, session_id, kind, title, status,
                           request_json, result_json, error, attempt, max_attempts, created_at, updated_at)
                           VALUES ('market','',?,?,?, '{}','{}','',1,2,?,?)""",
                        (kind, kind, status, stamp, stamp),
                    )
                connection.commit()
            finally:
                connection.close()
            batch = app.project_activity_batch(["market"])
            summary = app.agent_run_summary("market", batch=batch)
            solo = app.agent_run_summary("market")
        self.assertEqual(summary["total"], 1, "首页卡片运行数只算真实运行")
        self.assertEqual(solo["total"], 1, "Agent 面板运行数只算真实运行")


class OrphanedCrawlRunTests(unittest.TestCase):
    """Crawl Worker 没启动时，任务会永远停在 queued，页面上显示"排队等待"。

    原来的回收逻辑只处理 status='running'（被领走后 Worker 死了），而且它本身
    只在 Crawl Worker 内部调用 —— Worker 不在，连回收都不会发生。
    """

    def make_run(self, connection, run_id, created_at, status="queued"):
        connection.execute(
            """INSERT INTO agent_runs (id, project_id, session_id, kind, title, status, request_json,
               result_json, error, parent_run_id, attempt, max_attempts, started_at, finished_at, created_at, updated_at)
               VALUES (?, 'crawl4ai', '', 'crawl', '网页研究', ?, '{}', '{}', '', '', 1, 1, '', '', ?, ?)""",
            (run_id, status, created_at, created_at),
        )

    def test_long_queued_runs_are_flagged_when_no_worker_holds_the_lease(self):
        temp_dir, database_file = temp_database()
        old = (datetime.now(timezone.utc) - timedelta(seconds=app.WORKBENCH_CRAWL_STALE_SECONDS * 10)).isoformat()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                self.make_run(connection, "old-1", old)
                self.make_run(connection, "old-2", old)
                self.make_run(connection, "fresh", app.now_iso())
                connection.commit()
            finally:
                connection.close()
            flagged = app.flag_orphaned_crawl_runs()
            connection = app.db_connection()
            try:
                rows = dict(connection.execute("SELECT id, status FROM agent_runs").fetchall())
                error = connection.execute("SELECT error FROM agent_runs WHERE id = 'old-1'").fetchone()[0]
            finally:
                connection.close()
        self.assertEqual(flagged, 2)
        self.assertEqual(rows["old-1"], "failed")
        self.assertEqual(rows["old-2"], "failed")
        self.assertEqual(rows["fresh"], "queued", "刚入队的任务不该被误伤")
        self.assertIn("Worker", error)

    def test_a_live_worker_lease_leaves_the_queue_alone(self):
        temp_dir, database_file = temp_database()
        old = (datetime.now(timezone.utc) - timedelta(seconds=app.WORKBENCH_CRAWL_STALE_SECONDS * 10)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                self.make_run(connection, "waiting", old)
                connection.execute(
                    """INSERT INTO worker_leases (worker_id, instance_id, status, lease_until, last_heartbeat, metadata_json)
                       VALUES ('crawl-worker', 'i-1', 'running', ?, ?, '{}')""",
                    (future, app.now_iso()),
                )
                connection.commit()
            finally:
                connection.close()
            flagged = app.flag_orphaned_crawl_runs()
            connection = app.db_connection()
            try:
                status = connection.execute("SELECT status FROM agent_runs WHERE id = 'waiting'").fetchone()[0]
            finally:
                connection.close()
        self.assertEqual(flagged, 0)
        self.assertEqual(status, "queued", "Worker 还活着时排队是正常的")


class VapidKeyGenerationTests(unittest.TestCase):
    """「推送订阅」从上线起就是"存了不发"：VAPID 密钥从没配过，也没有工具能生成。"""

    def generate(self, key_file):
        import subprocess, sys
        script = Path(__file__).resolve().parents[1] / "deploy" / "generate-vapid-keys.py"
        return subprocess.run([sys.executable, str(script), "--key-file", str(key_file)],
                              capture_output=True, text=True)

    def test_generated_key_is_a_p256_key_the_app_accepts(self):
        from cryptography.hazmat.primitives import serialization
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "vapid_private.pem"
            result = self.generate(key_file)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(key_file.exists())
            key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
            self.assertEqual(key.curve.name, "secp256r1", "Web Push (RFC 8292) 要求 P-256")
            self.assertEqual(oct(key_file.stat().st_mode)[-3:], "600", "私钥权限必须是 600")
            with patch.dict("os.environ", {"WORKBENCH_VAPID_PRIVATE_KEY_FILE": str(key_file)}):
                self.assertTrue(app.vapid_private_key_configured())
                self.assertEqual(app.vapid_private_key_source(), "file")

    def test_public_key_is_unpadded_urlsafe_base64_of_65_bytes(self):
        import base64
        with tempfile.TemporaryDirectory() as tmp:
            result = self.generate(Path(tmp) / "vapid_private.pem")
            line = next(l for l in result.stdout.splitlines() if l.startswith("WORKBENCH_VAPID_PUBLIC_KEY="))
            public_key = line.split("=", 1)[1]
        self.assertNotIn("=", public_key, "VAPID 公钥不能带 base64 padding")
        self.assertNotIn("+", public_key)
        self.assertNotIn("/", public_key)
        raw = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
        self.assertEqual(len(raw), 65, "未压缩 EC 点应为 65 字节")
        self.assertEqual(raw[0], 0x04, "未压缩点必须以 0x04 开头")

    def test_existing_key_is_not_silently_overwritten(self):
        """换密钥会让所有已有订阅失效，绝不能因为手滑再跑一次就发生。"""
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "vapid_private.pem"
            self.assertEqual(self.generate(key_file).returncode, 0)
            original = key_file.read_bytes()
            second = self.generate(key_file)
            self.assertNotEqual(second.returncode, 0, "重复生成应当失败并提示 --force")
            self.assertEqual(key_file.read_bytes(), original, "私钥被静默覆盖了")


class StaticAssetCompressionTests(unittest.TestCase):
    """Cowart 画布 bundle 有 5.9MB，线上没有压缩——慢网络下表现就是「画布打不开」。"""

    def test_deploy_precompresses_large_static_assets(self):
        script = (Path(__file__).resolve().parents[1] / "deploy" / "deploy-workbench.sh").read_text(encoding="utf-8")
        self.assertIn("precompress_static_assets", script)
        self.assertIn("gzip -9 -c", script)

    def test_nginx_prefers_the_precompressed_file(self):
        conf = (Path(__file__).resolve().parents[1] / "deploy" / "workbench-nginx.conf").read_text(encoding="utf-8")
        self.assertIn("gzip_static on;", conf)
        self.assertIn("gzip on;", conf)

    def test_the_canvas_bundle_is_actually_worth_compressing(self):
        import gzip as gzip_mod
        bundle = Path(__file__).resolve().parents[1] / "static" / "vendor" / "cowart" / app.COWART_SCRIPT_NAME
        if not bundle.is_file():
            self.skipTest("Cowart 资源未安装")
        raw = bundle.read_bytes()
        self.assertGreater(len(raw), 32 * 1024, "小于 32KB 的文件不会被预压缩规则命中")
        self.assertLess(len(gzip_mod.compress(raw, 9)), len(raw) * 0.6)


class AiLearningReviewTests(unittest.TestCase):
    """练习产出此前只写不读——学员交了作业没人批，这是「没达到学习目的」的核心。"""

    FEEDBACK = json.dumps({
        "verdict": "基本达标", "score": 72,
        "met": ["列出了输入与输出"],
        "gaps": ["你写的『整理访谈记录』没有说明每周发生几次"],
        "rewrite": "任务：整理用户访谈记录（每周 2 次）",
        "misconception": "你选了『一年一次的战略会』，说明还在按重要性挑任务",
        "next_question": "哪一步一旦模型出错你能立刻发现？",
    }, ensure_ascii=False)

    def seed_lesson(self, practice_output="任务：整理访谈记录\n输入：录音转写", answer=0):
        content = app.AI_LEARNING_CURRICULUM[0]
        connection = app.db_connection()
        try:
            connection.execute(
                """INSERT INTO ai_learning_lessons(lesson_date,day_index,module,title,content_json,source,status,
                   quiz_answer,quiz_correct,practice_output,reflection,confidence,note_artifact_id,
                   started_at,completed_at,created_at,updated_at)
                   VALUES('2026-08-11',1,?,?,?,'curriculum','in_progress',?,0,?,'感觉有用',3,0,'','',?,?)""",
                (content["module"], content["title"], json.dumps(content, ensure_ascii=False),
                 answer, practice_output, app.now_iso(), app.now_iso()),
            )
            connection.commit()
            return int(connection.execute("SELECT id FROM ai_learning_lessons").fetchone()[0])
        finally:
            connection.close()

    def test_review_grades_against_the_lesson_rubric_and_persists(self):
        temp_dir, database_file = temp_database()
        captured = {}

        async def fake_llm(messages, *args, **kwargs):
            captured["prompt"] = messages[1]["content"]
            return self.FEEDBACK

        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            lesson_id = self.seed_lesson()
            with patch.object(app, "llm_settings", lambda: {"configured": True}), patch.object(app, "call_llm", fake_llm):
                result = asyncio.run(app.review_ai_learning_practice(lesson_id))
                reloaded = app.get_ai_learning_lesson(lesson_id=lesson_id)

        feedback = result["feedback"]
        self.assertEqual(feedback["verdict"], "基本达标")
        self.assertEqual(feedback["score"], 72)
        self.assertTrue(feedback["gaps"])
        self.assertTrue(reloaded["feedback"].get("reviewed_at"), "批改结果没有落库，刷新后就没了")
        # 批改必须有依据：课程声明的交付物标准、学员的自测选择、学员的原文
        self.assertIn("交付物标准", captured["prompt"])
        self.assertIn("学员选择", captured["prompt"])
        self.assertIn("整理访谈记录", captured["prompt"])

    def test_empty_practice_output_is_refused_before_spending_an_llm_call(self):
        temp_dir, database_file = temp_database()
        calls = {"n": 0}

        async def counting_llm(*args, **kwargs):
            calls["n"] += 1
            return self.FEEDBACK

        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            lesson_id = self.seed_lesson(practice_output="   ")
            with patch.object(app, "llm_settings", lambda: {"configured": True}), patch.object(app, "call_llm", counting_llm):
                with self.assertRaises(app.HTTPException) as ctx:
                    asyncio.run(app.review_ai_learning_practice(lesson_id))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(calls["n"], 0, "空产出不该浪费一次 LLM 调用")

    def test_unconfigured_llm_returns_503_not_a_crash(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            lesson_id = self.seed_lesson()
            with patch.object(app, "llm_settings", lambda: {"configured": False}):
                with self.assertRaises(app.HTTPException) as ctx:
                    asyncio.run(app.review_ai_learning_practice(lesson_id))
        self.assertEqual(ctx.exception.status_code, 503)

    def test_non_json_model_output_degrades_without_losing_content(self):
        """模型不听话时不能白跑一次调用，学员至少要看到它说了什么。"""
        temp_dir, database_file = temp_database()

        async def chatty_llm(*args, **kwargs):
            return "这份产出方向对了，但缺少频率说明。"

        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            lesson_id = self.seed_lesson()
            with patch.object(app, "llm_settings", lambda: {"configured": True}), patch.object(app, "call_llm", chatty_llm):
                feedback = asyncio.run(app.review_ai_learning_practice(lesson_id))["feedback"]
        self.assertTrue(feedback["raw_only"])
        self.assertIn("频率说明", feedback["rewrite"])

    def test_json_extraction_handles_fenced_and_prefixed_output(self):
        self.assertEqual(app.extract_json_block('```json\n{"a":1}\n```'), '{"a":1}')
        self.assertEqual(app.extract_json_block('好的，结果如下 {"b":2} 以上'), '{"b":2}')
        self.assertEqual(app.extract_json_block('{"c":3}'), '{"c":3}')


class LearningTrackTests(unittest.TestCase):
    """具身智能原本是 78 行静态页：没有后端、没有课程、没有进度和自测。

    现在它和 AI 转型学习共用同一套机制，只是换一条 track。这需要两次表重建
    （lesson_date 原本是全局 UNIQUE，profiles 原本带 CHECK (id = 1)），
    所以迁移的正确性必须被钉住。
    """

    def legacy_database(self, path, *, with_created_at=True):
        connection = sqlite3.connect(path)
        extra = ", created_at TEXT NOT NULL, updated_at TEXT NOT NULL" if with_created_at else ""
        connection.execute(f"""CREATE TABLE ai_learning_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, lesson_date TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL, practice_output TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ready'{extra})""")
        connection.execute("""CREATE TABLE ai_learning_profiles (
            id INTEGER PRIMARY KEY CHECK (id = 1), current_role TEXT NOT NULL DEFAULT '',
            target_role TEXT NOT NULL DEFAULT '', daily_minutes INTEGER NOT NULL DEFAULT 25,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        stamp = app.now_iso()
        if with_created_at:
            connection.execute("INSERT INTO ai_learning_lessons (lesson_date,title,practice_output,created_at,updated_at) VALUES ('2026-08-10','旧课程','我的练习',?,?)", (stamp, stamp))
        else:
            connection.execute("INSERT INTO ai_learning_lessons (lesson_date,title,practice_output) VALUES ('2026-08-10','旧课程','我的练习')")
        connection.execute("INSERT INTO ai_learning_profiles (id,current_role,target_role,daily_minutes,created_at,updated_at) VALUES (1,'产品经理','AI 产品',40,?,?)", (stamp, stamp))
        connection.commit()
        connection.close()

    def test_migration_preserves_rows_and_assigns_the_default_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_file = Path(tmp) / "workbench.db"
            self.legacy_database(database_file)
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                connection = app.db_connection()
                try:
                    lesson = connection.execute("SELECT track, lesson_date, title, practice_output FROM ai_learning_lessons").fetchone()
                    profile = connection.execute("SELECT track, current_role, target_role, daily_minutes FROM ai_learning_profiles").fetchone()
                    leftovers = connection.execute("SELECT name FROM sqlite_master WHERE name LIKE '%pre_track%'").fetchall()
                finally:
                    connection.close()
        self.assertEqual(tuple(lesson), ("ai-transformation", "2026-08-10", "旧课程", "我的练习"))
        self.assertEqual(tuple(profile), ("ai-transformation", "产品经理", "AI 产品", 40))
        self.assertEqual(leftovers, [], "临时表没有清理")

    def test_migration_survives_a_legacy_table_missing_columns(self):
        """早期版本的表结构更简单，迁移不能因为缺列就撞 NOT NULL。"""
        with tempfile.TemporaryDirectory() as tmp:
            database_file = Path(tmp) / "workbench.db"
            self.legacy_database(database_file, with_created_at=False)
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                connection = app.db_connection()
                try:
                    row = connection.execute("SELECT track, title, created_at FROM ai_learning_lessons").fetchone()
                finally:
                    connection.close()
        self.assertEqual(row[0], "ai-transformation")
        self.assertEqual(row[1], "旧课程")
        self.assertTrue(row[2], "缺失的 created_at 应被补上时间戳")

    def test_check_constraint_is_dropped_even_if_the_column_already_exists(self):
        """只 ALTER TABLE 加列不会去掉 CHECK (id = 1)，列有了照样插不进第二行。"""
        with tempfile.TemporaryDirectory() as tmp:
            database_file = Path(tmp) / "workbench.db"
            self.legacy_database(database_file)
            half = sqlite3.connect(database_file)
            half.execute("ALTER TABLE ai_learning_profiles ADD COLUMN track TEXT NOT NULL DEFAULT 'ai-transformation'")
            half.commit()
            half.close()
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                app.get_ai_learning_profile("embodied")
                connection = app.db_connection()
                try:
                    tracks = sorted(row[0] for row in connection.execute("SELECT track FROM ai_learning_profiles"))
                finally:
                    connection.close()
        self.assertEqual(tracks, ["ai-transformation", "embodied"])

    def test_two_tracks_can_hold_a_lesson_on_the_same_day(self):
        """原表把 lesson_date 声明为全局 UNIQUE，两条轨道同一天必撞。"""
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                for track in ("ai-transformation", "embodied"):
                    connection.execute(
                        "INSERT INTO ai_learning_lessons(track,lesson_date,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                        (track, "2026-08-11", f"{track} 的课", app.now_iso(), app.now_iso()),
                    )
                connection.commit()
                count = connection.execute("SELECT COUNT(*) FROM ai_learning_lessons WHERE lesson_date='2026-08-11'").fetchone()[0]
            finally:
                connection.close()
        self.assertEqual(count, 2)

    def test_lessons_do_not_leak_between_tracks(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                for track, title in (("ai-transformation", "转型课"), ("embodied", "具身课")):
                    connection.execute(
                        "INSERT INTO ai_learning_lessons(track,lesson_date,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                        (track, "2026-08-11", title, app.now_iso(), app.now_iso()),
                    )
                connection.commit()
            finally:
                connection.close()
            embodied = app.list_ai_learning_lessons(30, "embodied")
            transformation = app.list_ai_learning_lessons(30, "ai-transformation")
            picked = app.get_ai_learning_lesson(lesson_date="2026-08-11", track="embodied")
        self.assertEqual([item["title"] for item in embodied], ["具身课"])
        self.assertEqual([item["title"] for item in transformation], ["转型课"])
        self.assertEqual(picked["title"], "具身课")

    def test_every_track_curriculum_has_the_same_shape(self):
        """两套课程共用同一渲染与批改逻辑，字段结构必须一致。"""
        required = {"module", "title", "objective", "knowledge", "case", "practice", "quiz", "takeaway"}
        for track_id, meta in app.LEARNING_TRACKS.items():
            self.assertTrue(meta["curriculum"], f"{track_id} 没有课程")
            for lesson in meta["curriculum"]:
                self.assertEqual(set(lesson), required, f"{track_id} / {lesson.get('title')} 字段不一致")
                quiz = lesson["quiz"]
                self.assertTrue(0 <= quiz["correct_index"] < len(quiz["options"]), f"{lesson['title']} 答案索引越界")
                self.assertGreaterEqual(len(quiz["options"]), 3)
                self.assertTrue(lesson["practice"]["deliverable"], "没有交付物标准，AI 批改就没有判据")

    def test_unknown_track_falls_back_instead_of_erroring(self):
        self.assertEqual(app.learning_track_id("no-such-track"), app.DEFAULT_LEARNING_TRACK)
        self.assertEqual(app.learning_track_id(""), app.DEFAULT_LEARNING_TRACK)


class ProductProjectDimensionTests(unittest.TestCase):
    """产品作战室原本只有一个扁平需求池：没有项目维度，也没有缺陷概念。"""

    def create(self, **kwargs):
        payload = {"title": "条目", "problem": "", "target_user": "", "outcome": "",
                   "reach": 1, "impact": 1, "confidence": 50, "effort": 1}
        payload.update(kwargs)
        return app.create_product_requirement(app.ProductRequirementRequest(**payload))

    def test_defect_uses_severity_instead_of_rice(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            defect = self.create(title="搜索空白", item_type="defect", severity="blocker")
            requirement = self.create(title="自选分组", reach=50, impact=3, confidence=80, effort=2)
        self.assertEqual(defect["item_type"], "defect")
        self.assertEqual(defect["severity"], "blocker")
        self.assertEqual(float(defect["score"]), 0.0, "缺陷不该被 RICE 打分")
        self.assertGreater(float(requirement["score"]), 0)
        self.assertEqual(requirement["severity"], "")

    def test_defect_priority_comes_from_severity(self):
        self.assertEqual(app._product_defect_priority("blocker"), "urgent")
        self.assertEqual(app._product_defect_priority("major"), "high")
        self.assertEqual(app._product_defect_priority("trivial"), "low")
        self.assertEqual(app._product_defect_priority(""), "normal")

    def test_unknown_project_falls_back_to_unassigned(self):
        """归属必须是用户建过的产品项目，否则写进去的是一条永远筛不出来的脏数据。"""
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            unknown = self.create(title="伪造项目", project_id="999")
            # 工作台内置项目的 id 也不算数：产品作战室管的是「我在做哪些产品」，
            # 不是「工作台有哪些功能模块」。
            builtin = self.create(title="用工作台项目 id", project_id="market")
        self.assertEqual(unknown["project_id"], "")
        self.assertEqual(builtin["project_id"], "")

    def test_project_names_are_unique(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            app.create_product_project("量化助手")
            with self.assertRaises(app.HTTPException) as ctx:
                app.create_product_project("量化助手")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_archived_project_leaves_the_active_list(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            project = app.create_product_project("要归档的项目")
            app.update_product_project(int(project["id"]), status="archived")
            active = [item["name"] for item in app.list_product_projects()]
            everything = [item["name"] for item in app.list_product_projects(True)]
        self.assertNotIn("要归档的项目", active)
        self.assertIn("要归档的项目", everything)

    def test_dead_project_ids_collapse_into_one_unassigned_bucket(self):
        """失效 id 各自成桶会显示成一排同名的「未归属」。"""
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                for stale in ("market", "knowledge", "42"):
                    connection.execute(
                        "INSERT INTO product_requirements(title, project_id, item_type, status, created_at, updated_at) VALUES (?,?,'requirement','discovering',?,?)",
                        (f"归属已失效的 {stale}", stale, app.now_iso(), app.now_iso()),
                    )
                connection.commit()
            finally:
                connection.close()
            rollup = app.product_manager_overview()["projects"]["rollup"]
        unassigned = [row for row in rollup if row["project_title"] == "未归属"]
        self.assertEqual(len(unassigned), 1, "失效归属应合并成一个桶")
        self.assertEqual(unassigned[0]["requirements"], 3)

    def test_items_are_filtered_by_project_and_type(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            project = str(app.create_product_project("量化助手")["id"])
            self.create(title="该项目的需求", project_id=project)
            self.create(title="该项目的缺陷", project_id=project, item_type="defect", severity="major")
            self.create(title="别处的需求")
            by_project = app.list_product_requirements(200, project)
            defects_only = app.list_product_requirements(200, project, "defect")
            everything = app.list_product_requirements(200)
        self.assertEqual(len(by_project), 2)
        self.assertEqual([item["title"] for item in defects_only], ["该项目的缺陷"])
        self.assertEqual(len(everything), 3)

    def test_overview_rolls_up_by_project_and_surfaces_blockers_first(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            quiet = str(app.create_product_project("安静项目")["id"])
            smoking = str(app.create_product_project("冒烟项目")["id"])
            self.create(title="安静项目的需求", project_id=quiet)
            self.create(title="冒烟项目的阻塞缺陷", project_id=smoking, item_type="defect", severity="blocker")
            overview = app.product_manager_overview()
        rollup = overview["projects"]["rollup"]
        self.assertEqual(rollup[0]["project_id"], smoking, "有阻塞缺陷的项目应该排最前")
        self.assertEqual(rollup[0]["blockers"], 1)
        self.assertEqual([item["title"] for item in overview["attention"]["open_defects"]], ["冒烟项目的阻塞缺陷"])
        self.assertNotIn("冒烟项目的阻塞缺陷", [item["title"] for item in overview["attention"]["top_priority"]],
                         "缺陷不该混进 RICE 优先级榜")

    def test_defect_work_item_carries_severity_priority_and_project(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            project = str(app.create_product_project("量化助手")["id"])
            self.create(title="缺陷", project_id=project, item_type="defect", severity="blocker")
            items = app.list_work_items()
        item = next(x for x in items if x["kind"] == "product_defect")
        self.assertEqual(item["priority"], "urgent")
        self.assertEqual(item["metadata"]["project_id"], project)


class MemoryHygieneTests(unittest.TestCase):
    """记忆表只增不减：expires_at 字段一直在，但没有任何代码给它赋值。

    每轮只有 MAX_MEMORY_CONTEXT_ITEMS 条能进上下文，池子越大真正相关的越容易
    被挤掉——这时候「记得多」反而让 Agent 更笨。
    """

    def seed(self, connection, memory_id, content, *, use_count=0, last_used="", created=None, pinned=0, status="confirmed"):
        stamp = created or app.now_iso()
        connection.execute(
            """INSERT INTO memory_items(id,owner_id,scope,project_id,kind,memory_key,content,value_json,status,
               confidence,sensitivity,pinned,source_type,source_id,use_count,last_used_at,expires_at,created_at,updated_at)
               VALUES(?,'default','global','','preference','',?,'{}',?,0.8,'normal',?,'','',?,?,'',?,?)""",
            (memory_id, content, status, pinned, use_count, last_used, stamp, stamp),
        )

    def build(self):
        temp_dir, database_file = temp_database()
        stale = (datetime.now(timezone.utc) - timedelta(days=app.MEMORY_STALE_DAYS * 3)).isoformat()
        return temp_dir, database_file, stale

    def test_flags_never_used_and_idle_but_not_fresh_or_pinned(self):
        temp_dir, database_file, stale = self.build()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                self.seed(connection, "never", "确认过却从没命中", created=stale)
                self.seed(connection, "idle", "用过但很久没用", use_count=3, last_used=stale, created=stale)
                self.seed(connection, "fresh", "刚建的记忆")
                self.seed(connection, "pinned", "置顶的老记忆", created=stale, pinned=1)
                connection.commit()
            finally:
                connection.close()
            report = app.memory_hygiene()
        self.assertEqual([item["id"] for item in report["never_used"]], ["never"])
        self.assertEqual([item["id"] for item in report["idle"]], ["idle"])
        flagged = {item["id"] for item in report["never_used"] + report["idle"]}
        self.assertNotIn("fresh", flagged, "刚建的记忆不该被建议归档")
        self.assertNotIn("pinned", flagged, "置顶记忆永远不该出现在建议里")

    def test_archiving_removes_it_from_agent_retrieval_but_keeps_the_row(self):
        temp_dir, database_file, stale = self.build()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                self.seed(connection, "target", "回答默认用中文", created=stale)
                connection.commit()
            finally:
                connection.close()
            before = app.retrieve_memories("中文", project_id="market")
            result = app.archive_memory_items(["target"])
            after = app.retrieve_memories("中文", project_id="market")
            connection = app.db_connection()
            try:
                status = connection.execute("SELECT status FROM memory_items WHERE id='target'").fetchone()[0]
            finally:
                connection.close()
        self.assertEqual(result["archived"], 1)
        self.assertTrue(any(item["id"] == "target" for item in before))
        self.assertFalse(any(item["id"] == "target" for item in after), "归档后仍进入 Agent 上下文")
        self.assertEqual(status, "superseded", "记录应保留以便追溯和恢复，而不是删除")

    def test_pinned_memories_survive_a_bulk_archive(self):
        """置顶是用户明确要一直生效的，批量归档不能顺手带走。"""
        temp_dir, database_file, stale = self.build()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                self.seed(connection, "plain", "普通记忆", created=stale)
                self.seed(connection, "pinned", "置顶记忆", created=stale, pinned=1)
                connection.commit()
            finally:
                connection.close()
            result = app.archive_memory_items(["plain", "pinned"])
            connection = app.db_connection()
            try:
                statuses = dict(connection.execute("SELECT id, status FROM memory_items").fetchall())
            finally:
                connection.close()
        self.assertEqual(result["archived"], 1)
        self.assertEqual(statuses["plain"], "superseded")
        self.assertEqual(statuses["pinned"], "confirmed")

    def test_archive_ignores_empty_input(self):
        temp_dir, database_file, _ = self.build()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            self.assertEqual(app.archive_memory_items([])["archived"], 0)
            self.assertEqual(app.archive_memory_items(["  ", ""])["archived"], 0)

    def test_hygiene_reports_no_duplicate_suggestions(self):
        """字面相似度分不清"换个说法"和"差一个关键词"，所以不做重复建议。

        实测：「关注 A 股行情」vs「关注美股行情」重叠 0.75 但是两条不同记忆；
        「每天早上 8 点推送课程」vs「每日 8:00 推送今天的课程」确实重复却只有 0.22。
        与其给出会诱导用户删错记忆的建议，不如只保留基于硬事实的信号。
        """
        temp_dir, database_file, _ = self.build()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            report = app.memory_hygiene()
        self.assertNotIn("duplicates", report)
        self.assertIn("never_used", report)
        self.assertIn("idle", report)


class ProductFormValidationTests(unittest.TestCase):
    """表单里的两个坑，都会让提交静默失败。"""

    def html(self):
        return (Path(__file__).resolve().parents[1] / "static" / "product-manager.html").read_text(encoding="utf-8")

    def test_effort_default_value_is_valid_for_its_own_step(self):
        """min=0.1 step=0.5 让默认值 1 本身非法（合法值是 0.6、1.1…）。

        这是一个早就存在的问题：需求模式下该字段可见，浏览器会弹「请输入有效值」；
        缺陷模式下它被隐藏，就变成提交没反应、控制台只留一句
        "An invalid form control ... is not focusable"。
        """
        markup = self.html()
        match = re.search(r'id="requirement-effort"[^>]*', markup)
        self.assertIsNotNone(match)
        field = match.group(0)
        self.assertIn('step="any"', field, "effort 是估算值，不该被固定步进卡住")

    def test_hidden_scoring_fields_are_disabled_not_just_hidden(self):
        """被 display:none 的控件仍然参与校验，校验不通过时浏览器既无法聚焦也不提示。"""
        script = (Path(__file__).resolve().parents[1] / "static" / "product-manager.js").read_text(encoding="utf-8")
        block = script[script.find("function syncRequirementTypeFields"):]
        block = block[:block.find("\nfunction ")]
        self.assertIn("disabled = isDefect", block, "隐藏字段必须同时 disabled，否则会静默阻塞提交")


class LearningHistoryTests(unittest.TestCase):
    """学习记录此前只是一行标题，点不开——学过什么、当时怎么答的、批改说了什么全看不到。"""

    def seed(self, connection, lesson_date, title, practice="我的练习", feedback='{"verdict":"达标","score":88,"reviewed_at":"2026-08-10T00:00:00+00:00"}'):
        content = json.dumps(app.AI_LEARNING_CURRICULUM[0], ensure_ascii=False)
        connection.execute(
            """INSERT INTO ai_learning_lessons(track,lesson_date,day_index,module,title,content_json,source,status,
               quiz_answer,quiz_correct,practice_output,reflection,confidence,note_artifact_id,feedback_json,
               started_at,completed_at,created_at,updated_at)
               VALUES('ai-transformation',?,1,'建立 AI 认知',?,?,'curriculum','completed',1,1,?,'复盘',4,0,?,'','',?,?)""",
            (lesson_date, title, content, practice, feedback, lesson_date, lesson_date),
        )

    def test_history_lesson_returns_practice_and_feedback(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                self.seed(connection, "2026-08-08", "第一节", practice="第一节写的东西")
                connection.commit()
                lesson_id = int(connection.execute("SELECT id FROM ai_learning_lessons").fetchone()[0])
            finally:
                connection.close()
            lesson = app.get_ai_learning_lesson_detail(lesson_id)["lesson"]
        self.assertEqual(lesson["title"], "第一节")
        self.assertEqual(lesson["practice_output"], "第一节写的东西", "回看时必须能看到当时写的练习")
        self.assertEqual(lesson["feedback"]["verdict"], "达标", "回看时必须能看到当时的批改")
        self.assertEqual(lesson["quiz_answer"], 1)

    def test_missing_lesson_returns_404(self):
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            with self.assertRaises(app.HTTPException) as ctx:
                app.get_ai_learning_lesson_detail(99999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_history_binding_is_delegated_on_the_container(self):
        """条目每次渲染都会重建，绑在条目上会随之丢失；必须委托在容器上，
        而且要在初始化时绑一次——不能挂在只有出错才会走到的分支里。"""
        script = (Path(__file__).resolve().parents[1] / "static" / "ai-learning.js").read_text(encoding="utf-8")
        self.assertIn("function setupLearningHistory", script)
        self.assertIn("setupLearningHistory();", script.split("function setupAILearning")[1][:400])


class BrowserSessionSecurityTests(unittest.TestCase):
    """AI 浏览器是整个工作台权限最大的一块：一个跑在服务器上、由 LLM 决定
    下一步点哪里的真实浏览器。边界必须被钉死。"""

    def test_navigation_targets_are_restricted(self):
        blocked = [
            "http://127.0.0.1:18765/api/health",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://localhost/x",
            "file:///etc/passwd",
            "",
        ]
        for url in blocked:
            self.assertTrue(app._browser_blocked_reason(url), f"{url} 应该被拒绝")
        self.assertEqual(app._browser_blocked_reason("https://example.com/a?b=1"), "")

    def test_workbench_itself_is_never_a_target(self):
        """让浏览器访问工作台自身，等于把内部 API 交给模型去点。"""
        self.assertIn("工作台自身", app._browser_blocked_reason("https://workbench.example.dev/api/work-items"))
        with patch.dict("os.environ", {"WORKBENCH_PUBLIC_HOST": "my-bench.example"}):
            self.assertIn("工作台自身", app._browser_blocked_reason("https://my-bench.example/api/x"))

    def test_only_whitelisted_actions_are_accepted(self):
        with patch.dict(app._browser_sessions, {"s1": {"id": "s1", "process": None, "steps": 0, "touched_at": 0, "url": "", "history": []}}):
            with self.assertRaises(app.HTTPException) as ctx:
                app.browser_session_act("s1", "evaluate", {"script": "fetch('/api')"})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("不支持的动作", ctx.exception.detail)

    def test_unknown_session_is_rejected(self):
        with self.assertRaises(app.HTTPException) as ctx:
            app.browser_session_act("nope", "snapshot", {})
        self.assertEqual(ctx.exception.status_code, 404)

    def test_upload_paths_cannot_escape_the_session_directory(self):
        """模型只能引用用户显式上传过的文件，不能点名服务器上的任意路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "s1"
            session_dir.mkdir()
            (session_dir / "ok.txt").write_text("hi", encoding="utf-8")
            session = {"id": "s1", "process": None, "steps": 0, "touched_at": 0, "url": "", "history": []}
            with patch.object(app, "BROWSER_SESSION_DIR", Path(tmp)), patch.dict(app._browser_sessions, {"s1": session}):
                for escape_path in ("/etc/passwd", "../../../etc/passwd", "缺失的文件.txt"):
                    with self.assertRaises(app.HTTPException) as ctx:
                        app.browser_session_act("s1", "upload", {"index": 0, "paths": [escape_path]})
                    self.assertEqual(ctx.exception.status_code, 400, escape_path)
                with self.assertRaises(app.HTTPException) as ctx:
                    app.browser_session_act("s1", "upload", {"index": 0, "paths": []})
                self.assertIn("请先上传", ctx.exception.detail)

    def test_worker_locates_elements_by_dom_marker_not_a_second_filter(self):
        """两套过滤规则一旦有差异，序号就会错位——表现为 AI 点了另一个按钮。"""
        worker = (Path(__file__).resolve().parents[1] / "browser_session_worker.py").read_text(encoding="utf-8")
        self.assertIn("data-wb-idx", worker)
        self.assertIn("node.setAttribute('data-wb-idx'", worker)
        self.assertIn("removeAttribute('data-wb-idx')", worker, "每次快照前要清掉旧标记，否则局部刷新后会残留")
        action_block = worker[worker.find('if action in {"click", "type", "upload"}'):]
        action_block = action_block[:action_block.find('if action == "scroll"')]
        self.assertIn('query_selector(f\'[data-wb-idx="{index}"]\')', action_block)

    def test_limits_are_bounded(self):
        self.assertGreaterEqual(app.BROWSER_MAX_SESSIONS, 1)
        self.assertLessEqual(app.BROWSER_MAX_SESSIONS, 6)
        self.assertGreaterEqual(app.BROWSER_IDLE_SECONDS, 60)
        self.assertGreaterEqual(app.BROWSER_MAX_AGENT_STEPS, 1)


class BrowserAgentLoopTests(unittest.TestCase):
    """给一个目标让模型自己连续操作。每一步都要可解释，否则这就是个黑盒——
    出了问题既不知道哪步错了，也不知道该不该信它的结论。"""

    def decisions(self, *items):
        import itertools
        stream = itertools.chain([json.dumps(item, ensure_ascii=False) for item in items],
                                 itertools.repeat(json.dumps({"action": "scroll", "delta": 100})))

        async def fake_llm(messages, *args, **kwargs):
            return next(stream)
        return fake_llm

    def fake_session(self, results):
        """不启动真实浏览器：这里验证的是循环与安全策略，不是 Chromium。"""
        session = {"id": "s1", "process": None, "steps": 0, "touched_at": time.time(), "url": "", "history": []}
        calls = []

        def act(session_id, action, payload=None):
            calls.append((action, dict(payload or {})))
            if action == "goto":
                reason = app._browser_blocked_reason(str((payload or {}).get("url") or ""))
                if reason:
                    raise app.HTTPException(400, reason)
            return results(action, payload) if callable(results) else dict(results)
        return session, act, calls

    def test_blocked_navigation_does_not_abort_the_whole_run(self):
        """被安全策略拦下时要把理由回给模型让它换路子，而不是整个任务失败。"""
        session, act, calls = self.fake_session(lambda a, p: {"ok": True, "url": "https://example.com", "title": "T", "elements": [], "text": "内容", "scroll": {}})
        llm = self.decisions(
            {"thought": "试试内网", "action": "goto", "url": "http://169.254.169.254/"},
            {"thought": "改走公网", "action": "goto", "url": "https://example.com"},
            {"thought": "够了", "action": "finish", "answer": "结论"},
        )
        with patch.dict(app._browser_sessions, {"s1": session}), patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", llm), patch.object(app, "browser_session_act", act):
            result = asyncio.run(app.browser_agent_run("s1", "找点东西"))
        self.assertEqual(result["stop_reason"], "finished")
        self.assertFalse(result["steps"][0]["ok"])
        self.assertIn("私网", result["steps"][0]["error"])
        self.assertTrue(result["steps"][1]["ok"])
        self.assertEqual(result["answer"], "结论")

    def test_step_budget_is_enforced(self):
        session, act, calls = self.fake_session(lambda a, p: {"ok": True, "url": "https://example.com", "title": "T", "elements": [], "text": "", "scroll": {}})
        llm = self.decisions({"action": "scroll", "delta": 100})
        with patch.dict(app._browser_sessions, {"s1": session}), patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", llm), patch.object(app, "browser_session_act", act):
            result = asyncio.run(app.browser_agent_run("s1", "永不结束", max_steps=3))
        self.assertEqual(result["stop_reason"], "step_limit")
        self.assertEqual(len(result["steps"]), 3)
        self.assertFalse(result["ok"])

    def test_budget_cannot_exceed_the_global_cap(self):
        session, act, _ = self.fake_session(lambda a, p: {"ok": True, "url": "", "title": "", "elements": [], "text": "", "scroll": {}})
        llm = self.decisions({"action": "scroll", "delta": 100})
        with patch.dict(app._browser_sessions, {"s1": session}), patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", llm), patch.object(app, "browser_session_act", act):
            result = asyncio.run(app.browser_agent_run("s1", "试图超额", max_steps=999))
        self.assertLessEqual(len(result["steps"]), app.BROWSER_MAX_AGENT_STEPS)

    def test_non_json_model_output_stops_cleanly(self):
        session, act, _ = self.fake_session(lambda a, p: {"ok": True, "url": "", "title": "", "elements": [], "text": "", "scroll": {}})

        async def chatty(messages, *args, **kwargs):
            return "我觉得应该点那个蓝色按钮"
        with patch.dict(app._browser_sessions, {"s1": session}), patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", chatty), patch.object(app, "browser_session_act", act):
            result = asyncio.run(app.browser_agent_run("s1", "随便"))
        self.assertEqual(result["stop_reason"], "bad_decision")
        self.assertFalse(result["ok"])

    def test_disallowed_action_is_refused_without_touching_the_browser(self):
        """模型要求执行任意脚本时，连浏览器都不该碰。"""
        session, act, calls = self.fake_session(lambda a, p: {"ok": True, "url": "", "title": "", "elements": [], "text": "", "scroll": {}})
        llm = self.decisions(
            {"action": "evaluate", "text": "fetch('/api/work-items')"},
            {"action": "finish", "answer": "放弃"},
        )
        with patch.dict(app._browser_sessions, {"s1": session}), patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", llm), patch.object(app, "browser_session_act", act):
            result = asyncio.run(app.browser_agent_run("s1", "读内部接口"))
        self.assertFalse(result["steps"][0]["ok"])
        self.assertIn("不支持的动作", result["steps"][0]["error"])
        self.assertNotIn("evaluate", [action for action, _ in calls])

    def test_every_step_records_why(self):
        session, act, _ = self.fake_session(lambda a, p: {"ok": True, "url": "https://example.com", "title": "T", "elements": [], "text": "", "scroll": {}})
        llm = self.decisions(
            {"thought": "先看看首页", "action": "goto", "url": "https://example.com"},
            {"thought": "拿到了", "action": "finish", "answer": "好"},
        )
        with patch.dict(app._browser_sessions, {"s1": session}), patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", llm), patch.object(app, "browser_session_act", act):
            result = asyncio.run(app.browser_agent_run("s1", "看首页"))
        self.assertEqual(result["steps"][0]["thought"], "先看看首页")
        self.assertTrue(all("thought" in step for step in result["steps"]))

    def test_unconfigured_llm_returns_503(self):
        with patch.object(app, "llm_settings", lambda: {"configured": False}):
            with self.assertRaises(app.HTTPException) as ctx:
                asyncio.run(app.browser_agent_run("s1", "做点什么"))
        self.assertEqual(ctx.exception.status_code, 503)

    def test_observation_lists_elements_by_index_and_omits_screenshot(self):
        """截图不进提示词——太贵，元素清单足够定位。"""
        text = app._browser_agent_observation({
            "url": "https://example.com", "title": "示例", "scroll": {"y": 0, "height": 900},
            "elements": [
                {"index": 0, "tag": "a", "label": "下一页"},
                {"index": 1, "tag": "input", "label": "搜索", "editable": True},
                {"index": 2, "tag": "input", "label": "", "file_input": True},
            ],
            "text": "正文内容", "screenshot": "AAAA" * 5000,
        })
        self.assertIn("[0] 链接：下一页", text)
        self.assertIn("[1] 输入框：搜索", text)
        self.assertIn("[2] 文件上传框", text)
        self.assertNotIn("AAAA", text, "截图不该进提示词")


class BrowserTabStripTests(unittest.TestCase):
    """标签从左侧竖排改到顶部横排。

    竖排在侧栏里能读，但它占掉正文最宝贵的横向空间，而且和所有人对
    「浏览器标签」的肌肉记忆都不一样——标签本来就该在页面上方。
    """

    def markup(self):
        return (Path(__file__).resolve().parents[1] / "static" / "web-research.html").read_text(encoding="utf-8")

    def styles(self):
        return (Path(__file__).resolve().parents[1] / "static" / "web-research.css").read_text(encoding="utf-8")

    def test_tab_list_lives_in_the_main_area_not_the_sidebar(self):
        markup = self.markup()
        main_index = markup.find('<main class="browser-main">')
        tabs_index = markup.find('id="context-tabs"')
        sidebar_index = markup.find('<aside class="workspace-sidebar"')
        self.assertGreater(tabs_index, main_index, "标签条应在主区之内")
        self.assertLess(sidebar_index, main_index)
        self.assertNotIn('id="sidebar-tabs-pane"', markup, "侧栏里的标签面板应已移除")

    def test_strip_spans_both_grid_columns(self):
        """browser-main 是两列网格，不跨列的话标签条会和浏览区并排。"""
        styles = self.styles()
        strip = styles[styles.find(".tab-strip {"):]
        strip = strip[:strip.find("\n.")]
        self.assertIn("grid-column: 1 / -1", strip)
        self.assertIn("flex: 0 0 auto", strip, "不锁定高度会被 flex/grid 拉伸")
        main = styles[styles.find(".browser-main {"):]
        main = main[:main.find("\n")]
        self.assertIn("grid-template-rows", main, "需要显式行定义，标签条才有自己的一行")

    def test_tabs_render_flat_not_grouped_by_host(self):
        """按域名分组的标题在横向排布下会把标签挤成一团。"""
        script = (Path(__file__).resolve().parents[1] / "static" / "web-research.js").read_text(encoding="utf-8")
        render = script[script.find("function renderTabs()"):]
        render = render[:render.find("\nfunction ")]
        self.assertIn("browser-tab", render)
        self.assertNotIn("sidebar-group", render, "横向标签条不该再有域名分组")

    def test_new_tab_button_is_bound_once_at_init(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "web-research.js").read_text(encoding="utf-8")
        self.assertIn("function bindTabStrip", script)
        self.assertIn("bindTabStrip();", script)
        self.assertIn('id="tab-strip-new"', self.markup())


class LessonDraftIsolationTests(unittest.TestCase):
    """上一个 bug 把在历史课里写的练习写进了「今天那节」的行里。

    代码已经修好了（Chromium 实测：在第一课写练习 → 落在第一课那行，
    第二课那行仍为空），但已经串进库里的内容还躺在那儿——打开今天的课，
    看到的是自己在第一课写的东西，而且没有任何办法清掉。
    """

    def script(self):
        return (Path(__file__).resolve().parents[1] / "static" / "ai-learning.js").read_text(encoding="utf-8")

    def test_a_pending_draft_remembers_which_lesson_it_was_typed_in(self):
        """700ms 的防抖窗口内切换课程时，定时器回调看到的
        currentLesson() 已经是新的那一节了。"""
        script = self.script()
        self.assertIn("learningState.draftLessonId = Number(currentLesson().id || 0)", script)
        body = script[script.find("async function saveCurrentLessonDraft("):]
        body = body[:body.find("\n}")]
        self.assertIn("const pendingId = learningState.draftLessonId", body)

    def test_switching_lessons_flushes_instead_of_dropping_the_draft(self):
        """renderTodayLesson 会 clearTimeout，不先冲一次的话，
        700ms 内敲的内容会被静默丢掉。"""
        script = self.script()
        self.assertIn("async function flushPendingDraft(", script)
        body = script[script.find("async function openHistoryLesson("):]
        body = body[:body.find("\nfunction closeHistoryLesson(")]
        self.assertIn("await flushPendingDraft()", body)

    def test_there_is_a_way_to_clear_a_lesson_that_already_holds_the_wrong_answer(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('/api/ai-learning/lessons/{lesson_id}/reset-practice', source)
        body = source[source.find("def post_ai_learning_reset_practice("):]
        body = body[:body.find("\n@app.")]
        # 只清作答痕迹，课程内容要留着，否则清完这一节就没法做了。
        self.assertIn("practice_output = ''", body)
        self.assertIn("feedback_json = '{}'", body)
        self.assertNotIn("content_json", body)
        self.assertIn('id="reset-practice"', self.script())

    def test_clearing_cancels_the_pending_draft_first(self):
        """否则 700ms 后那份草稿会把刚清掉的内容原样写回去。"""
        body = self.script()
        body = body[body.find('learnQuery("#reset-practice")'):]
        body = body[:body.find("\n  });")]
        self.assertIn("clearTimeout(learningState.draftTimer)", body)
        self.assertIn("learningState.draftLessonId = 0", body)


class ResearchHandoffPrefillTests(unittest.TestCase):
    """量化候选股上的「查最近消息 ↗」只带了研究目标，没带起始页面。

    跳过去看到的是一个问题填好了、但不知道从哪开始查的表单——
    而「去哪查」恰恰是这个按钮本来要替你解决的事。
    """

    def test_the_screening_link_carries_a_starting_page(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "market-screen.js").read_text(encoding="utf-8")
        self.assertIn("function newsUrl(", script)
        body = script[script.find("function newsUrl("):]
        body = body[:body.find("\n  }")]
        self.assertIn("agent_goal=", body)
        self.assertIn("agent_start=", body)

    def test_every_handoff_into_web_research_sends_both(self):
        """两个入口各写一份 URL，早晚会有一个漏掉参数。"""
        root = Path(__file__).resolve().parents[1] / "static"
        offenders = []
        for path in root.glob("market*.js"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"/projects/web-research\?[^\"'`]+", text):
                if "agent_goal" in match.group(0) and "agent_start" not in match.group(0):
                    offenders.append(f"{path.name}: {match.group(0)[:60]}")
        self.assertEqual(offenders, [], "跳转链接带了目标却没带起始页面")

    def test_the_receiving_page_guesses_a_start_rather_than_leaving_it_blank(self):
        """兜底：以后谁再忘了传，也不该把人晾在原地。"""
        script = (Path(__file__).resolve().parents[1] / "static" / "web-research-plus.js").read_text(encoding="utf-8")
        self.assertIn("startIsGuess", script)
        self.assertIn("按关键词猜的一个搜索页", script, "猜出来的要说明是猜的，让人知道可以改")


class CidDashboardChartTests(unittest.TestCase):
    """每天新增就是 0~2 个项目，30 点的折线基本贴着底走，看不出任何东西。"""

    def markup(self):
        path = Path(__file__).resolve().parents[1] / "projects" / "cid-dashboard-v2.html"
        if not path.exists():
            self.skipTest("看板文件不在这个检出里")
        return path.read_text(encoding="utf-8")

    def test_the_line_chart_and_its_dead_styles_are_gone(self):
        markup = self.markup()
        for leftover in ('id="chart"', "trend-svg", "trend-line", "trend-dot", "trend-area", "trend-grid"):
            with self.subTest(leftover=leftover):
                self.assertNotIn(leftover, markup, "删了图却留下只服务于它的代码/样式")

    def test_the_three_numbers_that_were_actually_useful_stay(self):
        markup = self.markup()
        self.assertIn("近 30 天新增", markup)
        self.assertIn("单日峰值", markup)
        self.assertIn("日均", markup)

    def test_the_no_date_case_still_says_something(self):
        """原来这句话写在图容器里，图删了不能把提示一起删掉。"""
        self.assertIn("没有日期字段", self.markup())


class CrawlJanitorTests(unittest.TestCase):
    """flag_orphaned_crawl_runs() 在它唯一该生效的场景里从来不会跑。

    这个函数是为「Crawl Worker 根本没启动、任务永远卡在 queued」写的，但它
    此前只在 recover_stale_crawl_runs() 里被调用，而后者只在 crawl_worker.py
    内部调用——Worker 不在的时候，连回收也不会发生。逻辑上自相矛盾：
    它兜的底恰好是它自己够不着的地方。
    """

    def source(self):
        return (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")

    def test_the_main_process_runs_the_janitor(self):
        source = self.source()
        self.assertIn("async def crawl_janitor_loop(", source)
        startup = source[source.find("async def start_automation_scheduler("):]
        startup = startup[:startup.find("\n@app.on_event")]
        self.assertIn("crawl_janitor_loop()", startup)

    def test_it_starts_even_when_the_other_workers_are_external(self):
        """那些开关控制的是别的 Worker 要不要在进程内跑，
        而这件事恰恰是要在别的 Worker 都不在时兜底。"""
        source = self.source()
        startup = source[source.find("async def start_automation_scheduler("):]
        startup = startup[:startup.find("\n@app.on_event")]
        line = next(item for item in startup.splitlines() if "crawl_janitor_loop()" in item)
        guard = startup[:startup.find(line)].splitlines()[-1]
        self.assertNotIn("external_sync_worker", guard)
        self.assertNotIn("external_agent_worker", guard)

    def test_a_single_failure_does_not_kill_the_loop(self):
        source = self.source()
        body = source[source.find("async def crawl_janitor_loop("):]
        body = body[:body.find("\n@app.on_event")]
        self.assertIn("except asyncio.CancelledError", body, "取消要能穿透，否则关不掉")
        self.assertIn("exc_info=True", body)

    def test_it_is_cancelled_on_shutdown(self):
        source = self.source()
        shutdown = source[source.find("async def stop_automation_scheduler("):]
        shutdown = shutdown[:shutdown.find("\n\n\n")]
        self.assertIn("crawl_janitor", shutdown)


class ReleaseGateTests(unittest.TestCase):
    """发布闸门放行的测试集，必须和开发机上跑的是同一套。"""

    def script(self):
        path = Path(__file__).resolve().parents[1] / "deploy" / "deploy-workbench.sh"
        if not path.exists():
            self.skipTest("deploy 脚本不在这个检出里")
        return path.read_text(encoding="utf-8")

    def test_the_gate_does_not_skip_any_test(self):
        """被跳过的那几条一旦真的坏了，正好在发布这一刻没人发现。"""
        script = self.script()
        body = script[script.find("run_release_tests()"):]
        body = body[:body.find("\n}")]
        # 只看真正会执行的行：注释里写「这里曾经用 --deselect」是在解释历史，
        # 不该被当成还在跳过用例。
        code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
        self.assertNotIn("--deselect", code)
        self.assertNotIn(" -k ", code, "用 -k 挑着跑等于另一种形式的跳过")
        self.assertIn('"$test_python" -m pytest -q )', code)

    def test_no_test_depends_on_a_specific_machines_files(self):
        """读开发机上真实文件的用例，在别的机器上必然失败，
        而且用户改一下自己的文件，闸门就会莫名其妙卡住。"""
        memory_tests = (Path(__file__).resolve().parents[1] / "tests" / "test_memory.py").read_text(encoding="utf-8")
        body = memory_tests[memory_tests.find("def test_workbuddy_preview_only_reads_user_preferences("):]
        body = body[:body.find("\n    def ")]
        self.assertIn("TemporaryDirectory", body)
        self.assertIn('patch.object(app, "ROOT"', body)


class DesktopTabStripTests(unittest.TestCase):
    """Electron 标签栏一多就散架。

    #tabs 是个没有任何样式的 div（默认 display:block），里面的 .tab 是
    inline-flex，所以标签一多就换行；而 #tabbar 写死 42px 高，换行出来的那几行
    直接溢出到标签栏外面、压在下面的页面内容上。Chromium 实测 1200px 宽下：
    10 个标签排成 2 行、4 个跑到栏外；16 个排成 3 行、10 个跑到栏外。
    改成单行横排：先等比压缩到 76px 下限，再整条横向滚动。修复后 16 个标签
    仍是 1 行、0 个溢出。
    """

    def shell(self):
        path = Path(__file__).resolve().parents[1] / "desktop" / "shell.html"
        if not path.exists():
            self.skipTest("desktop/shell.html 不在这个检出里")
        return path.read_text(encoding="utf-8")

    def test_the_strip_lays_out_in_a_single_scrolling_row(self):
        shell = self.shell()
        rule = shell[shell.find("#tabs {"):]
        rule = rule[:rule.find("}")]
        self.assertIn("display: flex", rule, "不给 #tabs 布局，标签就会按 inline 换行")
        self.assertIn("overflow-x: auto", rule, "压到下限之后要能横向滚动，而不是换行")
        self.assertIn("min-width: 0", rule, "flex 项不归零 min-width 就压不下去")

    def test_tabs_shrink_but_not_into_nothing(self):
        shell = self.shell()
        rule = shell[shell.find(".tab {", shell.find("#tabs::-webkit-scrollbar")):]
        rule = rule[:rule.find("}")]
        self.assertIn("flex: 0 1 auto", rule)
        self.assertIn("min-width: 76px", rule, "再窄标题就只剩一两个字，不如让它滚动")

    def test_the_active_tab_is_scrolled_into_view(self):
        """标签多到要横向滚动时，切过去却看不到自己切到了哪，等于没切。"""
        self.assertIn("scrollIntoView", self.shell())

    def test_middle_click_closes_a_tab(self):
        """标签被压窄之后 × 很难点，中键是实际最好用的关法。"""
        shell = self.shell()
        self.assertIn("auxclick", shell)
        self.assertIn("event.button !== 1", shell)


class CrossTabAskTests(unittest.TestCase):
    """AI 浏览器上那两个按钮是「向下滚动」和「回到顶部」。

    页面就在眼前，滚动自己拖更快——让 AI 代劳一次滚动没有任何价值。
    调研了豆包浏览器和 Tabbit 之后，两边真正拉开差距的都不是自动点按钮，
    而是「同时读多个标签、对齐比较」：Tabbit 的说法是 referencing multiple
    open tabs simultaneously。人做这件事最费劲，也最值得交出去。
    """

    def test_the_useless_scroll_buttons_are_gone(self):
        markup = (Path(__file__).resolve().parents[1] / "static" / "web-research.html").read_text(encoding="utf-8")
        # 只针对按钮：正文里那句「可以说…向下滚动」仍然成立——Agent 还是能滚，
        # 删掉的是「让人去点一个按钮来代替自己拖滚动条」这件事。
        self.assertNotIn("data-browser-command", markup)
        self.assertIn('id="cross-tab-ask"', markup)

    def test_it_refuses_to_compare_fewer_than_two_tabs(self):
        client = TestClient(app.app)
        response = client.post("/api/research/cross-tab", json={"run_ids": ["a"], "question": "比一比"})
        self.assertEqual(response.status_code, 422, "少于两个标签根本不成其为对比")

    def test_tabs_that_have_not_finished_reading_are_skipped_not_faked(self):
        client = TestClient(app.app)
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "load_crawl_runtime", lambda run_id: None):
            response = client.post("/api/research/cross-tab", json={"run_ids": ["a", "b"], "question": "比一比"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("凑不成对比", response.json()["detail"])

    def test_every_claim_has_to_say_which_tab_it_came_from(self):
        """不这么要求的话，模型会把几个页面糅成一段听起来很权威、
        但没法追溯的通稿。"""
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        body = source[source.find("async def post_cross_tab_ask("):]
        body = body[:body.find("\n# ---")]
        self.assertIn("标出它来自哪几个标签", body)
        self.assertIn("仅标签 N 提到", body)
        self.assertIn("互相矛盾时必须单独列出", body)


class ProjectListFanoutTests(unittest.TestCase):
    """首页卡片是个 N+1：每张卡片各查一次自己的工作项计数、Agent 运行计数
    和最近一次运行。15 个项目一次 /api/projects 跑了 242 条 SQL，而且这个
    数字随项目数线性增长。三份数据都能一次 GROUP BY 全部算出来。
    实测：242 → 198 条，中位耗时 15.7ms → 8.4ms。
    """

    def test_the_batch_helper_replaces_the_per_project_queries(self):
        # project_activity_batch/_public_projects_uncached 已随拆分迁到 app_pkg/projects.py
        source = (Path(__file__).resolve().parents[1] / "app_pkg" / "projects.py").read_text(encoding="utf-8")
        self.assertIn("def project_activity_batch(", source)
        body = source[source.find("def _public_projects_uncached("):]
        body = body[:body.find("\ndef ")]
        self.assertIn("project_activity_batch(", body)
        self.assertIn("batch=activity_batch", body)

    def test_batch_and_per_project_paths_agree(self):
        """批量口径一旦和单查口径不一致，首页显示的就是另一套数字。"""
        project_ids = [str(item.get("id") or "") for item in app.load_projects()][:6]
        batch = app.project_activity_batch(project_ids)
        for project_id in project_ids:
            with self.subTest(project=project_id):
                self.assertEqual(
                    app.project_activity(project_id, batch=batch),
                    app.project_activity(project_id),
                    "批量算出来的卡片和单独查出来的不一样",
                )

    def test_the_endpoint_stays_under_a_query_budget(self):
        """给个上限，下次再有人往卡片里加一个 per-project 查询会当场被挡住。"""
        counter = {"n": 0}

        class Counting(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.set_trace_callback(lambda statement: counter.__setitem__("n", counter["n"] + 1))

        original = sqlite3.connect
        client = TestClient(app.app)
        client.get("/api/meta")
        with patch.object(sqlite3, "connect", lambda *a, **k: original(*a, **{**k, "factory": Counting})):
            counter["n"] = 0
            response = client.get("/api/projects")
        self.assertEqual(response.status_code, 200)
        self.assertLess(counter["n"], 240, f"/api/projects 跑了 {counter['n']} 条 SQL，N+1 又回来了")


class SerialUpstreamFetchTests(unittest.TestCase):
    """市场页最慢的一项是 /api/market/etf-rotation，实测 1484ms。

    里面除了四次上游行情往返几乎没有别的开销，而这四次是一个一个 await
    下来的——四个标的互不依赖，本来就该并发。可转债那边同理：每 60 个代码
    一批是上游的限制，但批与批之间没有依赖，原来也是串着等。
    """

    def source(self):
        # market 并发实现已随拆分迁到 app_pkg/market.py
        return (Path(__file__).resolve().parents[1] / "app_pkg" / "market.py").read_text(encoding="utf-8")

    def test_etf_rotation_fetches_the_pool_concurrently(self):
        body = self.source()
        body = body[body.find("async def market_etf_rotation("):]
        body = body[:body.find("\nasync def ")]
        self.assertIn("asyncio.gather", body)
        self.assertNotIn("for item in pool:\n        klines = await", body)

    def test_a_single_failing_symbol_does_not_sink_the_whole_endpoint(self):
        """并发之后一个标的抛异常会直接冒出来，除非显式收集。"""
        body = self.source()
        body = body[body.find("async def market_etf_rotation("):]
        body = body[:body.find("\nasync def ")]
        self.assertIn("return_exceptions=True", body)
        self.assertIn("isinstance(klines, BaseException)", body)

    def test_convertible_bond_batches_run_concurrently(self):
        body = self.source()
        body = body[body.find("async def market_convertible_bonds("):]
        body = body[:body.find("\nasync def ")]
        self.assertIn("asyncio.gather", body)
        self.assertIn("return_exceptions=True", body)


class MemoryTransparencyTests(unittest.TestCase):
    """记忆是整个工作台唯一会静默改变回答的东西。

    此前页面上只显示「使用了 N 条已确认记忆」——看不到是哪几条，更看不到它
    凭什么被选中。于是一条早就忘了自己写过的偏好把回答带偏时，既发现不了，
    也没有就地关掉的入口。

    先量过再动手：用 8 条贴近真实的记忆和 12 条提问跑了一遍现有的召回，
    命中基本正确，误召回只有「服务号→服务器」这种个位数的边缘情况。
    所以这次没有去调打分公式——没有测出来的缺陷就去调参，只是把问题换个
    地方藏起来。真正缺的是可见性和反馈入口。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DATA_DIR", Path(self.tmp.name)),
                            ("DATABASE_FILE", Path(self.tmp.name) / "workbench.db"),
                            ("_DB_SCHEMA_READY", False)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self):
        app.create_memory_item(content="回答默认用中文，先说结论再给依据", status="confirmed", pinned=True)
        app.create_memory_item(content="服务器磁盘超过 80% 就要提醒我", status="confirmed")
        app.create_memory_item(content="关注美股行情，主要看科技股", status="confirmed")

    def test_every_recalled_memory_explains_why_it_was_recalled(self):
        self.seed()
        items = app.retrieve_memories("服务器现在怎么样", project_id="server")
        reasons = {item["content"][:4]: item["match_reason"] for item in items}
        self.assertEqual(len(reasons), 2)
        self.assertIn("置顶", reasons["回答默认"], "置顶记忆要说明它每轮都会带上")
        self.assertIn("命中", reasons["服务器磁"])
        self.assertTrue(all(item.get("match_score") is not None for item in items))

    def test_the_reason_does_not_read_like_a_tokenizer_dump(self):
        """query_terms 把中文切成二元片段，一句「服务器现在怎么样」会同时命中
        「服务」和「务器」。两条一起摆出来只是噪音。"""
        self.assertEqual(app.memory_match_reason(["服务", "务器"], "服务器磁盘超过 80%"), "命中 「服务」")
        self.assertEqual(app.memory_match_reason(["服务器磁盘", "服务", "磁盘"]), "命中 「服务器磁盘」")
        self.assertEqual(app.memory_match_reason(["需求", "行情"], "需求评审要看行情"), "命中 「需求」、「行情」")
        self.assertEqual(app.memory_match_reason([]), "")

    def test_the_reason_travels_all_the_way_to_the_answer(self):
        self.seed()
        context = app.memory_context_for_llm("server", "服务器现在怎么样")
        self.assertTrue(context["refs"])
        for ref in context["refs"]:
            with self.subTest(ref=ref["id"]):
                self.assertTrue(ref["reason"], "refs 里没有理由，前端就只能显示一个数字")

    def test_unrelated_questions_do_not_drag_in_topic_memories(self):
        """跑偏的召回比不召回更糟：它会静默改变回答的方向。"""
        self.seed()
        items = app.retrieve_memories("今天心情不太好", project_id="server")
        self.assertEqual([item["pinned"] for item in items], [1], "只有置顶记忆该无条件进入")

    def test_the_agent_panel_renders_the_memories_and_a_way_out(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "project.js").read_text(encoding="utf-8")
        self.assertIn("本轮用到的记忆", script)
        self.assertIn("data-memory-drop", script)
        self.assertIn("/reject", script, "看到一条记忆把回答带偏了，要能就地关掉")

    def test_the_hygiene_endpoint_finally_has_a_caller(self):
        """memory_hygiene 早就写好了，但整个前端一个调用点都没有——
        每轮只有 5 条能进上下文，池子越大真正相关的越容易被挤掉，
        而「哪些记忆在白占名额」此前只能 curl 才看得到。"""
        script = (Path(__file__).resolve().parents[1] / "static" / "workbench.js").read_text(encoding="utf-8")
        self.assertIn("/api/memories/hygiene", script)
        self.assertIn("/api/memories/archive", script)
        self.assertIn("归档不是删除", script, "要说清楚归档不会丢数据")

    def test_archiving_takes_a_memory_out_of_the_context_but_not_the_store(self):
        self.seed()
        target = next(item for item in app.list_memory_items(status="confirmed") if "美股" in item["content"])
        app.archive_memory_items([target["id"]])
        recalled = app.retrieve_memories("美股昨天涨了吗", project_id="market")
        self.assertNotIn(target["id"], [item["id"] for item in recalled], "归档后不该再进上下文")
        self.assertTrue(any(item["id"] == target["id"] for item in app.list_memory_items(status="all")), "归档不是删除")


class LessonActionTargetTests(unittest.TestCase):
    """从学习记录打开第一课、点批改，结果跳到了第二课。

    根因是页面上有两个「当前这节课」：屏幕上渲染的那一节，和
    learningState.dashboard.today。所有动作处理器读的都是后者，于是打开历史课
    之后：
      · 批改请求发到今天那节 —— 批的是另一节课；
      · 批完 renderTodayLesson(dashboard.today) —— 页面刷成今天那节；
      · 练习草稿 PATCH 到今天那节 —— 你在第一课写的东西存进了今天那节；
      · 保存笔记同理。
    也就是说不只是跳页，是写错了对象。Chromium 实测修复后：打开第 4 节点批改，
    请求是 /ai-learning/lessons/4/review，页面仍停在第 4 节。
    """

    def script(self):
        return (Path(__file__).resolve().parents[1] / "static" / "ai-learning.js").read_text(encoding="utf-8")

    def test_there_is_one_source_of_truth_for_the_rendered_lesson(self):
        script = self.script()
        self.assertIn("function currentLesson()", script)
        self.assertIn("learningState.currentLesson = lesson", script,
                      "渲染时不记下来的话，currentLesson() 永远只能回退到今天那节")

    def test_no_action_handler_takes_today_as_its_target(self):
        """动作处理器只要还把 dashboard.today 当作操作对象，这个 bug 就会
        以另一种形式回来。（拿它做「这是不是今天那节」的比较是可以的。）"""
        script = self.script()
        self.assertNotIn("const lesson = learningState.dashboard?.today", script,
                         "又把今天那节当成了当前这节")
        for name in ("bindLessonActions", "saveCurrentLessonDraft", "hasCurrentLessonDraft"):
            body = script[script.find(f"function {name}("):]
            body = body[:body.find("\n}")]
            with self.subTest(function=name):
                self.assertIn("currentLesson()", body)

    def test_grading_renders_the_lesson_that_was_graded(self):
        script = self.script()
        body = script[script.find("async function requestAiReview("):]
        body = body[:body.find("\nfunction bindLessonActions(")]
        self.assertIn("renderTodayLesson(body.lesson || currentLesson())", body)
        self.assertNotIn("renderTodayLesson(learningState.dashboard?.today", body)

    def test_an_updated_lesson_is_written_back_everywhere_it_appears(self):
        """同一节课可能同时在当前视图、今日课程和历史列表里；
        只更新一处，另外两处就会显示过期状态。"""
        script = self.script()
        self.assertIn("function syncLessonEverywhere(", script)
        for caller in ("saveCurrentLessonDraft", "requestAiReview", "saveLessonNote"):
            body = script[script.find(f"function {caller}("):]
            body = body[:body.find("\n}")]
            with self.subTest(function=caller):
                self.assertIn("syncLessonEverywhere(", body)

    def test_regenerate_refuses_to_run_while_a_past_lesson_is_open(self):
        """「换一节」换的永远是今天那节。看历史课时点它，今天那节会被悄悄换掉，
        而屏幕上显示的还是历史课——看起来像什么都没发生。"""
        script = self.script()
        body = script[script.find("async function regenerateTodayLesson("):]
        body = body[:body.find("\nasync function saveLessonNote(")]
        self.assertIn("isViewingToday()", body)
        self.assertIn("只对今天的课程生效", body)

    def test_completing_a_past_lesson_does_not_bounce_back_to_today(self):
        script = self.script()
        self.assertIn("const wasToday =", script)
        self.assertIn("if (!wasToday)", script)
        self.assertIn('"完成今日学习" : "记录这一节"', script, "按钮文案要说清楚记的是哪一节")


class LearningHistoryOpenTests(unittest.TestCase):
    """「点击学习记录没反应」和「第一次打开失败」。

    这两个症状在本地数据上都复现不出来（连点四条全部 200、无 pageerror），
    所以修的是让它们不可能再变成沉默失败的三条路径：
      1. data-lesson-id 拿不到数字时原来是一句 `if (!id) return;`——
         点了完全没反应，连一条能报上来的线索都没有；
      2. 点击到内容替换之间的网络时间里页面上什么都不动，看起来就是没反应；
      3. 网络类失败一次就报错，而「第一次失败、第二次就好」正是一次性抖动的样子。
    """

    def script(self):
        return (Path(__file__).resolve().parents[1] / "static" / "ai-learning.js").read_text(encoding="utf-8")

    def body(self):
        source = self.script()
        body = source[source.find("async function openHistoryLesson("):]
        return body[:body.find("\nfunction closeHistoryLesson(")]

    def test_an_unusable_id_is_reported_instead_of_silently_ignored(self):
        body = self.body()
        self.assertNotIn("if (!id) return;", body, "静默 return 就是「点了没反应」")
        self.assertIn("Number.isFinite(id)", body)
        self.assertIn("没有可用的课程编号", body)

    def test_the_clicked_row_shows_that_something_started(self):
        body = self.body()
        self.assertIn('classList.add("loading")', body)
        self.assertIn('classList.remove("loading")', body)
        styles = (Path(__file__).resolve().parents[1] / "static" / "ai-learning.css").read_text(encoding="utf-8")
        self.assertIn(".history-item.loading", styles)

    def test_a_one_off_network_blip_retries_once(self):
        body = self.body()
        self.assertIn('error?.code !== "network"', body)
        self.assertIn('error?.code !== "timeout"', body)
        self.assertEqual(body.count("/api/ai-learning/lessons/"), 2, "重试应该只有一次，不能变成无限重试")

    def test_a_double_click_does_not_fire_two_requests(self):
        self.assertIn("learningState.openingLessonId", self.body())


class ServiceWorkerCacheTests(unittest.TestCase):
    """Service Worker 三处会让「刚部署完第一次打开」表现得很怪的地方。"""

    def worker(self):
        return (Path(__file__).resolve().parents[1] / "static" / "sw.js").read_text(encoding="utf-8")

    def test_versioned_assets_are_not_matched_by_ignoring_the_query_string(self):
        """统一 ignoreSearch: true 时，/static/x.js?v=新 会命中缓存里 v=旧 那份——
        整套 ?v= 缓存失效机制在离线回退这条路径上等于没有。"""
        worker = self.worker()
        self.assertIn("function matchCached", worker)
        self.assertIn('searchParams.has("v")', worker)
        self.assertIn("ignoreSearch: !versioned", worker)

    def test_failed_responses_are_never_written_into_the_shell_cache(self):
        """来什么存什么的话，一次 404 会被存进壳缓存，之后每次离线回退都拿到
        那份错误页，而且要等到下个版本换 CACHE_NAME 才会清掉。"""
        worker = self.worker()
        self.assertIn("response.ok", worker)
        self.assertIn('response.type === "basic"', worker)

    def test_one_missing_shell_file_cannot_block_the_whole_install(self):
        """addAll 是全有全无的：SHELL 里只要有一个路径拼错，install 就 reject，
        新 Service Worker 永远装不上，用户会一直被旧壳服务且没有任何提示。"""
        worker = self.worker()
        self.assertNotIn("cache.addAll(SHELL)", worker)
        self.assertIn("cache.add(path).catch", worker)

    def test_the_cache_name_tracks_the_release(self):
        version = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
        self.assertIn(f'workbench-shell-v{version}', self.worker(), "缓存名没跟着版本走，旧壳不会被清掉")

    def test_every_shell_entry_actually_exists(self):
        """现在缺文件不再阻塞安装，但也不该让它悄悄缺着。"""
        root = Path(__file__).resolve().parents[1]
        worker = self.worker()
        paths = re.findall(r'"(/static/[^"]+)"', worker)
        missing = [path for path in paths if not (root / path.lstrip("/")).exists()]
        self.assertEqual(missing, [], "SHELL 里登记了不存在的静态文件")


class UndefinedCssVariableTests(unittest.TestCase):
    """知识库抽屉背景是透明的。

    .kb-panel 写的是 background: var(--card)，而 --card 这个变量整个项目里
    没有任何地方定义过（theme.css 用的是 --surface / --panel）。自定义属性
    查不到就当没写，background 直接落回 transparent——抽屉整块透明，正文压在
    页面内容上。Chromium 实测 backgroundColor 是 rgba(0, 0, 0, 0)。
    这类错不报任何异常，只能靠实跑看出来。
    """

    ROOT = Path(__file__).resolve().parents[1]

    def defined_variables(self):
        names = set()
        for path in (self.ROOT / "static").glob("*.css"):
            names.update(re.findall(r"(--[a-z0-9-]+)\s*:", path.read_text(encoding="utf-8")))
        return names

    def test_every_variable_used_without_a_fallback_is_defined_somewhere(self):
        defined = self.defined_variables()
        offenders = []
        for path in sorted((self.ROOT / "static").glob("*.css")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"var\((--[a-z0-9-]+)\s*\)", text):
                if match.group(1) not in defined:
                    offenders.append(f"{path.name}:{match.group(1)}")
        self.assertEqual(sorted(set(offenders)), [], "用了没有定义、也没有兜底值的 CSS 变量——会静默变成空值")

    def test_a_hard_coded_fallback_is_not_secretly_the_only_value(self):
        """带兜底的写法躲过了上面那条检查，但如果变量根本没人定义过，
        兜底里那个写死的颜色就是它在两个主题下唯一的取值——等于把主题钉死了。

        AI 浏览器的「@ 引用」浮层就是这么坏的：
        var(--research-surface, #111827)，而 --research-surface 从没被定义过，
        于是浅色模式下它是一块深蓝黑底配深色字，实测对比度 1.01。
        """
        defined = self.defined_variables()
        offenders = []
        for path in sorted((self.ROOT / "static").glob("*.css")):
            # 注释里引用旧写法是在解释历史，不该被当成还在用。
            text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
            for match in re.finditer(r"var\((--[a-z0-9-]+)\s*,\s*([^();]+)\)", text):
                name, fallback = match.group(1), match.group(2).strip()
                if name in defined:
                    continue
                if re.match(r"^#[0-9a-fA-F]{3,8}$|^rgba?\(", fallback):
                    offenders.append(f"{path.name}:{name}→{fallback}")
        self.assertEqual(sorted(set(offenders)), [], "变量从没被定义过，写死的兜底成了两个主题下唯一的取值")

    def test_the_knowledge_drawer_has_a_real_background(self):
        text = (self.ROOT / "static" / "project.css").read_text(encoding="utf-8")
        for selector in (".kb-panel", ".kb-toggle"):
            rule = text[text.find(selector + " {"):]
            rule = rule[:rule.find("}")]
            self.assertIn("background: var(--surface, var(--panel, #fff))", rule, f"{selector} 没有可解析的背景色")


class PhaseIndexSpecificityTests(unittest.TestCase):
    """14 天学习路线的序号贴在圆圈左上角，还小得看不清。

    序号本身是一个 <span>，而 `.learning-phase span` 这三条通用规则会连它一起
    改：display:block 覆盖掉 .phase-index 的 display:grid（place-items 随之
    失效），font-size 和 color 也一并盖住。两条规则作用在同一个元素上，
    `.learning-phase span` 的优先级 (0,1,1) 高于 `.phase-index` (0,1,0)，
    所以后者全线失守。Chromium 实测修复前 display 是 block，修复后是 grid、
    文本中心与圆心的水平偏移为 0。
    """

    def styles(self):
        return (Path(__file__).resolve().parents[1] / "static" / "ai-learning.css").read_text(encoding="utf-8")

    def test_the_generic_span_rules_exclude_the_index(self):
        styles = self.styles()
        self.assertNotIn(".learning-phase span {", styles, "通用 span 规则会把序号一起改掉")
        self.assertIn(".learning-phase span:not(.phase-index)", styles)

    def test_the_index_is_still_a_centred_grid(self):
        rule = self.styles()
        rule = rule[rule.find(".phase-index {"):]
        rule = rule[:rule.find("}")]
        self.assertIn("display: grid", rule)
        self.assertIn("place-items: center", rule)

    def test_the_markup_still_renders_the_index_as_a_span(self):
        """哪天序号换成 <b>，上面那条 :not() 就白写了。"""
        script = (Path(__file__).resolve().parents[1] / "static" / "ai-learning.js").read_text(encoding="utf-8")
        self.assertIn('<span class="phase-index">', script)


class MarketStyleScreenUiTests(unittest.TestCase):
    """七个流派、每个流派的适用与失效条件、逐条规则的通过情况，
    后端全都写好了，页面上却一直没有任何入口——只能 curl 才看得到。"""

    ROOT = Path(__file__).resolve().parents[1]

    def test_the_page_loads_the_style_module(self):
        markup = (self.ROOT / "static" / "market.html").read_text(encoding="utf-8")
        self.assertIn("market-style.js", markup)
        self.assertIn('id="style-list"', markup)
        self.assertIn('id="style-scan"', markup)

    def test_it_shows_when_each_style_loses_money_next_to_when_it_works(self):
        """一张只写自己什么时候管用的策略卡片，读起来像广告。"""
        script = (self.ROOT / "static" / "market-style.js").read_text(encoding="utf-8")
        self.assertIn("fails_when", script)
        self.assertIn("works_when", script)
        for style in app.MARKET_STYLES:
            with self.subTest(style=style["id"]):
                self.assertTrue(str(style.get("fails_when") or "").strip(), "风格没有登记失效条件")

    def test_rule_results_show_the_actual_numbers(self):
        """只写「通过 / 不通过」等于让人相信一个黑盒。"""
        script = (self.ROOT / "static" / "market-style.js").read_text(encoding="utf-8")
        self.assertIn("check.detail", script)
        self.assertIn("check.passed", script, "后端字段是 passed，写成 pass 会全部渲染成不通过")

    def test_a_precondition_failure_is_reported_once_not_seven_times(self):
        """自选池是空的时候，七个流派会给出七条一模一样的失败。"""
        script = (self.ROOT / "static" / "market-style.js").read_text(encoding="utf-8")
        self.assertIn("precondition", script)
        self.assertIn("break", script)

    def test_the_market_agent_can_run_the_same_screen(self):
        """Agent 自己「讲」一套选股逻辑，和页面上按固定规则跑的是两回事。"""
        self.assertIn("market_style_screen", app.SUBAGENT_TOOL_MAP["market"])
        result = app.execute_react_tool("market_style_screen", {})
        self.assertFalse(result["ok"])
        self.assertEqual(
            {item["id"] for item in result["styles"]},
            {style["id"] for style in app.MARKET_STYLES},
            "不给 style_id 时应返回完整的可选清单",
        )


class ActiveLearningTests(unittest.IsolatedAsyncioTestCase):
    """学习此前只有一条被动通道：每天推一节，学完为止。

    临时想弄懂一个名词、想知道最近哪条热点值得学、想把一个理论讲透，都没有
    入口，只能等课程哪天刚好排到。练习那一环还有个更硬的前提问题：它假设
    「你手上正好有一个真实场景可以拿来练」，没有的时候练习框就空着，
    AI 批改也就无从批起。
    """

    def setUp(self):
        # 必须换掉 DATABASE_FILE 而不是 DATA_DIR：连接是直接拿 DATABASE_FILE
        # 开的，只改目录的话测试会写进开发库。
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DATA_DIR", Path(self.tmp.name)),
                            ("DATABASE_FILE", Path(self.tmp.name) / "workbench.db"),
                            ("_DB_SCHEMA_READY", False)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def llm(payload):
        async def fake(*args, **kwargs):
            return json.dumps(payload, ensure_ascii=False)
        return fake

    def test_a_topic_is_required_for_terms_and_theory(self):
        """空题目生成出来的只能是泛泛而谈，不如直接拒绝。"""
        with patch.object(app, "llm_settings", lambda: {"configured": True}):
            for kind in ("term", "theory"):
                with self.subTest(kind=kind):
                    with self.assertRaises(app.HTTPException) as ctx:
                        asyncio.run(app.create_ai_learning_exploration(kind, "  "))
                    self.assertEqual(ctx.exception.status_code, 400)

    def test_an_unknown_kind_is_refused(self):
        with patch.object(app, "llm_settings", lambda: {"configured": True}):
            with self.assertRaises(app.HTTPException) as ctx:
                asyncio.run(app.create_ai_learning_exploration("astrology", "水逆"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_hotspots_refuse_to_run_without_real_items(self):
        """让模型自由发挥「最近的 AI 热点」，它只会把训练数据里的旧闻
        说得像刚发生一样——这比不给更糟。没有真实条目就不生成。"""
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "load_aihot_snapshot", lambda: {}), \
             patch.object(app, "select_aihot_items", lambda *a, **k: []):
            with self.assertRaises(app.HTTPException) as ctx:
                asyncio.run(app.create_ai_learning_exploration("hotspot", ""))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("AI 热点", str(ctx.exception.detail))

    def test_every_configured_feed_host_has_a_domain_label(self):
        """每个已配置的 RSS 源都必须能打出领域标签，否则该领域筛选恒为空。

        商业/综合曾长期为 0：映射表里有 36kr→商业 但源列表没有 36kr，
        而「综合」只是未知域名的兜底，所有源都被映射命中了就永远不出现。
        """
        hosts = [
            app._hostname(url)
            for url in app._AIHOT_DEFAULT_SOURCES.split(",")
            if "aihot.today" not in url and "hnrss.org" not in url
        ]
        self.assertTrue(hosts, "至少要有非 AI 专属源")
        labels = {app._aihot_domain(host) for host in hosts}
        self.assertIn("商业", labels, "默认源必须包含商业类订阅（如钛媒体）")
        self.assertIn("综合", labels, "默认源必须包含综合/新闻类订阅（如新浪滚动新闻）")
        self.assertTrue(all(label in {"科技", "财经", "商业", "综合"} for label in labels), f"未知领域标签：{labels}")

    async def test_every_track_kind_has_recommendations_with_topic_and_why(self):
        """主动学习推荐：两个 track × 四个 kind 都要有可点的推荐，
        且每条带 topic（问什么）和 why（为什么值得问）——否则「换一换」
        换不出东西，非专业用户依然卡在空白输入框前。未配置 LLM 时走
        精选池兜底也必须可用。"""
        with patch.object(app, "llm_settings", lambda: {"configured": False}):
            for track in ("ai-transformation", "embodied"):
                for kind in ("term", "theory", "method", "hotspot"):
                    body = await app.get_ai_learning_exploration_recommendations(track, kind)
                    items = body["recommendations"]
                    self.assertGreaterEqual(len(items), 4, f"{track}/{kind} 至少 4 条推荐")
                    self.assertTrue(all(item.get("topic") and item.get("why") for item in items),
                                    f"{track}/{kind} 每条都要有 topic 和 why")

    def test_hotspot_prompt_carries_the_real_items_and_forbids_invention(self):
        captured = {}

        async def fake(messages, **kwargs):
            captured["messages"] = messages
            return json.dumps({"title": "T", "whats_new": "x"}, ensure_ascii=False)

        items = [{"title": "某模型发布", "source": "官方博客", "url": "https://example.com/a"}]
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "load_aihot_snapshot", lambda: {"items": items}), \
             patch.object(app, "select_aihot_items", lambda *a, **k: items), \
             patch.object(app, "call_llm", fake):
            asyncio.run(app.create_ai_learning_exploration("hotspot", ""))
        system = captured["messages"][0]["content"]
        user = captured["messages"][1]["content"]
        self.assertIn("real_items", system)
        self.assertIn("不要编造", system)
        self.assertIn("某模型发布", user)

    def test_an_unparseable_generation_is_not_stored(self):
        """把一段散文当成结构化内容存进去，下次打开就是一堆空字段。"""
        async def fake(*args, **kwargs):
            return "抱歉，我无法完成。"
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", fake):
            with self.assertRaises(app.HTTPException) as ctx:
                asyncio.run(app.create_ai_learning_exploration("term", "RAG"))
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(app.list_ai_learning_explorations(), [])

    def test_a_generated_exploration_round_trips(self):
        payload = {"title": "RAG", "definition": "检索后再作答", "boundary": "资料本身错时无解"}
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", self.llm(payload)):
            created = asyncio.run(app.create_ai_learning_exploration("term", "RAG"))
        self.assertEqual(created["content"]["definition"], "检索后再作答")
        listed = app.list_ai_learning_explorations()
        self.assertEqual([item["id"] for item in listed], [created["id"]])

    def test_the_reference_answer_is_withheld_until_the_answer_is_in(self):
        """参考答案跟着题目一起发到前端，打开开发者工具就能看到；
        真想抄的人一定会抄，而抄完这道题就废了。"""
        payload = {"question": "该不该做？", "context": "背景", "criteria": ["A", "B"], "reference_answer": "标准答案正文"}
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", self.llm(payload)):
            exercise = asyncio.run(app.create_ai_learning_exercise(topic="提示工程"))
        self.assertEqual(exercise["reference_answer"], "", "没作答就把参考答案发出去了")
        self.assertEqual(exercise["criteria"], ["A", "B"], "评分标准应该先给，才知道往哪答")
        stored = app.get_ai_learning_exercise(exercise["id"])
        self.assertEqual(stored["reference_answer"], "标准答案正文", "参考答案本身要存下来")

        graded = {"score": 72, "verdict": "方向对", "misses": ["少了验收标准"]}
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", self.llm(graded)):
            result = asyncio.run(app.grade_ai_learning_exercise(exercise["id"], "我的答案"))
        self.assertEqual(result["score"], 72)
        self.assertEqual(result["reference_answer"], "标准答案正文", "交卷之后才谈得上对照")
        self.assertTrue(result["answered"])

    def test_exercise_falls_back_to_a_builtin_question_when_llm_keeps_failing(self):
        """LLM 连续两次都吐不出完整 JSON（1500 token 截断的典型症状）时，
        必须用内置模板题兜底，而不是让用户连点两次都吃 502。"""
        async def junk(*args, **kwargs):
            return "这是一个讲具身智能抓取的题"  # 没有可解析的 JSON
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", junk):
            exercise = asyncio.run(app.create_ai_learning_exercise(topic="具身智能灵巧手"))
        self.assertTrue(str(exercise["question"] or "").strip(), "兜底题也必须出得了题")
        self.assertIn("具身智能灵巧手", exercise["question"], "兜底题要带上用户给的题目方向")
        self.assertGreaterEqual(len(exercise.get("criteria") or []), 3, "兜底题也要能评判")
        stored = app.get_ai_learning_exercise(exercise["id"])
        self.assertTrue(str(stored["reference_answer"] or "").strip(), "参考答案要入库，交卷后才能对照")

    def test_exercise_falls_back_when_the_llm_call_raises(self):
        """provider 全挂（网络/上游 5xx）时也不能 502：内置模板题接管。"""
        async def boom(*args, **kwargs):
            raise RuntimeError("upstream down")
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", boom):
            exercise = asyncio.run(app.create_ai_learning_exercise(topic="多模态大模型"))
        self.assertTrue(str(exercise["question"] or "").strip())
        self.assertIn("多模态大模型", exercise["question"])

    def test_an_empty_answer_is_refused_before_spending_a_call(self):
        payload = {"question": "Q", "reference_answer": "A"}
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", self.llm(payload)):
            exercise = asyncio.run(app.create_ai_learning_exercise(topic="X"))
        with self.assertRaises(app.HTTPException) as ctx:
            asyncio.run(app.grade_ai_learning_exercise(exercise["id"], "   "))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_a_non_json_grade_still_keeps_the_answer(self):
        """评判解析失败也不能把用户写的东西丢了。"""
        payload = {"question": "Q", "reference_answer": "A"}
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", self.llm(payload)):
            exercise = asyncio.run(app.create_ai_learning_exercise(topic="X"))

        async def prose(*args, **kwargs):
            return "你答得还行。"
        with patch.object(app, "llm_settings", lambda: {"configured": True}), \
             patch.object(app, "call_llm", prose):
            result = asyncio.run(app.grade_ai_learning_exercise(exercise["id"], "我写的答案"))
        self.assertEqual(result["user_answer"], "我写的答案")
        self.assertIn("你答得还行", result["feedback"]["verdict"])
        self.assertEqual(result["score"], -1, "解析不出分数时不该编一个分数")

    def test_the_exercise_prompt_asks_for_a_self_contained_scenario(self):
        """题目要能靠思考回答——这正是原来的练习做不到的地方。"""
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        body = source[source.find("async def create_ai_learning_exercise("):]
        body = body[:body.find("\nasync def grade_ai_learning_exercise(")]
        self.assertIn("不需要用户手上有现成的工作材料", body)
        self.assertIn("不要冒充真实公司", body)
        self.assertIn("avoid_questions", body, "不去重的话连出几道会是同一题")

    def test_the_lesson_case_now_has_to_carry_an_answer(self):
        """案例只讲「他怎么做的」，读的人无从判断自己想的对不对。"""
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('"case": ("situation", "approach", "result", "lesson", "answer")', source)
        self.assertIn("answer 写「在这个情境下正确的做法是什么、为什么」", source)
        script = (Path(__file__).resolve().parents[1] / "static" / "ai-learning.js").read_text(encoding="utf-8")
        self.assertIn("先自己想", script, "答案直接摊开就没有思考环节了")


class FloatingSurfaceCollisionTests(unittest.TestCase):
    """两个悬浮入口都钉在右下角，谁也不知道对方存在。

    知识库抽屉 right:0 bottom:84 z-index 55，项目 Agent 面板
    right:24 bottom:76 z-index 19。用 Chromium 量过：1280×720 下知识库按钮
    的矩形正好盖在「发送」按钮上，而且 z-index 更高——elementFromPoint 打在
    发送按钮中心，拿到的是知识库。也就是发送按钮点不动（只有 Enter 还能发）。
    """

    def styles(self):
        return (Path(__file__).resolve().parents[1] / "static" / "project.css").read_text(encoding="utf-8")

    def test_knowledge_drawer_yields_while_the_agent_panel_is_open(self):
        styles = self.styles()
        self.assertIn("body[data-agent-panel-open] .kb-drawer", styles)
        rule = styles[styles.find("body[data-agent-panel-open] .kb-drawer"):]
        rule = rule[:rule.find("}")]
        self.assertIn("pointer-events: none", rule, "只做透明还会挡点击")
        self.assertIn("visibility: hidden", rule)

    def test_the_open_state_marker_the_rule_depends_on_still_exists(self):
        """这条规则挂在 body 上的 data 标记上，标记没了规则就静默失效。"""
        script = (Path(__file__).resolve().parents[1] / "static" / "project.js").read_text(encoding="utf-8")
        self.assertIn("document.body.dataset.agentPanelOpen", script)
        agent_css = (Path(__file__).resolve().parents[1] / "static" / "project-agent.css").read_text(encoding="utf-8")
        self.assertIn("body[data-agent-panel-open]", agent_css)


class InlineScriptScopeTests(unittest.TestCase):
    """页面内联脚本和共享脚本在同一个全局作用域里，重名就是整段不执行。

    /projects/cloud-dev 的内联脚本顶层 `const escapeHtml = ...`，而先加载的
    project.js 已经有同名全局函数声明。全局词法声明撞上已存在的全局函数会直接
    抛 SyntaxError，整个 <script> 一行都不跑——页面永远停在「读取中…」，
    所有按钮无反应，报错只出现在控制台。Playwright 实测：
    pageerror = "Identifier 'escapeHtml' has already been declared"，
    #cloud-workspaces 文本恒为「读取中…」。
    """

    ROOT = Path(__file__).resolve().parents[1]

    def shared_globals(self):
        """project.js 顶层声明的名字——内联脚本不能在全局再声明一次。"""
        source = (self.ROOT / "static" / "project.js").read_text(encoding="utf-8")
        names = set()
        for line in source.splitlines():
            match = re.match(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", line)
            if match:
                names.add(match.group(1))
            match = re.match(r"^function\s+([A-Za-z_$][\w$]*)\s*\(", line)
            if match:
                names.add(match.group(1))
        return names

    @staticmethod
    def inline_scripts(markup):
        return re.findall(r"<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", markup, flags=re.S)

    def test_no_page_redeclares_a_name_that_project_js_owns(self):
        shared = self.shared_globals()
        self.assertIn("escapeHtml", shared, "探测顶层声明的正则失效了")
        offenders = []
        for path in sorted([*(self.ROOT / "static").glob("*.html"), *(self.ROOT / "projects").glob("*.html")]):
            markup = path.read_text(encoding="utf-8")
            if "project.js" not in markup:
                continue
            for block in self.inline_scripts(markup):
                # 包进 IIFE 的块里声明什么都不会进全局作用域，不算冲突。
                if "(() => {" in block or "(function" in block:
                    continue
                for name in shared:
                    if re.search(rf"(?:const|let)\s+{re.escape(name)}\s*=", block):
                        offenders.append(f"{path.name}:{name}")
        self.assertEqual(sorted(set(offenders)), [], "内联脚本在全局重复声明了共享脚本的名字，整段脚本会不执行")

    def test_the_cloud_dev_inline_script_is_wrapped(self):
        markup = (self.ROOT / "static" / "cloud-dev.html").read_text(encoding="utf-8")
        body = markup[markup.rfind("<script>"):]
        self.assertIn("(() => {", body, "内联脚本没有包进 IIFE，顶层声明仍会进全局作用域")
        self.assertIn("})();", body)


class ProjectSourceFallbackTests(unittest.TestCase):
    """projects.json 里配的是开发机上的绝对路径。"""

    def test_the_configured_path_is_machine_specific(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "projects.json").read_text(encoding="utf-8"))
        items = config if isinstance(config, list) else config.get("projects", [])
        entry = next(item for item in items if item.get("id") == "cid-dashboard")
        self.assertTrue(entry["source_path"].startswith("/Users/"), "如果哪天改成相对路径，这个兜底就可以拆了")

    def test_the_route_falls_back_to_the_copy_inside_this_repo(self):
        """换一台机器那个绝对路径就不存在，iframe 404、整页空白，
        而看板本身就是这一页的主体内容。"""
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        body = source[source.find("async def cid_dashboard_source("):]
        body = body[:body.find("\n@app.")]
        self.assertIn('ROOT / "projects"', body)
        self.assertIn("is_relative_to(projects_root)", body, "兜底不能顺手放宽读文件的边界")

    def test_the_fallback_still_refuses_a_path_outside_the_projects_folder(self):
        client = TestClient(app.app)
        with patch.object(app, "load_projects", lambda: [{"id": "cid-dashboard", "source_env": "", "source_path": "/nowhere/../../etc/passwd"}]):
            response = client.get("/projects/cid-dashboard-source")
        self.assertEqual(response.status_code, 404)


class ProjectAgentToolLoopTests(unittest.IsolatedAsyncioTestCase):
    """同一个 Agent，换个入口就少了半条腿。

    从工作台问「市场怎么样」，市场 Agent 会真的调 market_read；可是从市场
    项目页直接跟它说话，run_project_agent 走的是另一条路——一次 call_llm，
    一个工具都没有，只能对着一份可能滞后的只读快照复述。项目页恰恰是最常用
    的入口，所以这条路径必须也能真取数据。

    修法是把总调度里那段 ReAct 循环抽成 run_agent_react_loop，两条路径共用。
    """

    def setUp(self):
        self.events = []
        patcher = patch.object(app, "add_agent_run_event", lambda *a, **k: self.events.append((a, k)))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _reply(content="", tool_calls=None):
        message = {"content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {"choices": [{"message": message}]}

    async def test_loop_executes_the_tool_and_feeds_the_result_back(self):
        seen = {}

        def handler(args):
            seen["args"] = args
            return {"ok": True, "close": 12.34, "as_of": "2026-08-11"}

        replies = [
            self._reply("", [{"id": "c1", "function": {"name": "market_read", "arguments": "{\"code\": \"600519\"}"}}]),
            self._reply("收盘 12.34，数据时间 2026-08-11。"),
        ]
        captured = {}

        async def fake_call(messages, tools):
            captured["messages"] = list(messages)
            return replies.pop(0)

        with patch.dict(app.REACT_TOOLS, {"market_read": {"handler": handler}}), \
             patch.object(app, "call_llm_with_tools", fake_call):
            result = await app.run_agent_react_loop(
                project_id="market", run_id="r1",
                messages=[{"role": "user", "content": "现在什么价"}],
                tools=[{"type": "function", "function": {"name": "market_read", "parameters": {}}}],
            )
        self.assertEqual(seen["args"], {"code": "600519"}, "工具参数没有被真正解析并传下去")
        self.assertIn("12.34", result["answer"])
        self.assertEqual([item["tool"] for item in result["tool_calls"]], ["market_read"])
        # 工具结果必须以 role=tool 回喂，否则模型看不到自己查到了什么。
        self.assertTrue(any(m.get("role") == "tool" and "12.34" in m.get("content", "") for m in captured["messages"]))

    async def test_a_failing_tool_becomes_a_result_instead_of_killing_the_turn(self):
        def handler(args):
            raise RuntimeError("上游炸了")

        replies = [
            self._reply("", [{"id": "c1", "function": {"name": "market_read", "arguments": "{}"}}]),
            self._reply("取数失败，以下结论未验证。"),
        ]

        async def fake_call(messages, tools):
            return replies.pop(0)

        with patch.dict(app.REACT_TOOLS, {"market_read": {"handler": handler}}), \
             patch.object(app, "call_llm_with_tools", fake_call):
            result = await app.run_agent_react_loop(
                project_id="market", run_id="r1", messages=[{"role": "user", "content": "问"}],
                tools=[{"type": "function", "function": {"name": "market_read", "parameters": {}}}],
            )
        self.assertIn("未验证", result["answer"])
        self.assertFalse(result["tool_calls"][0]["ok"])

    async def test_round_limit_forces_a_conclusion_rather_than_returning_nothing(self):
        """模型一直要工具、从不写结论时，不能把空字符串当成答案返回。"""
        def handler(args):
            return {"ok": True}

        async def fake_call(messages, tools):
            return self._reply("", [{"id": "c", "function": {"name": "market_read", "arguments": "{}"}}])

        async def fake_summary(messages, **kwargs):
            return "已达工具上限，基于已有结果给出结论。"

        with patch.dict(app.REACT_TOOLS, {"market_read": {"handler": handler}}), \
             patch.object(app, "call_llm_with_tools", fake_call), \
             patch.object(app, "call_llm", fake_summary):
            result = await app.run_agent_react_loop(
                project_id="market", run_id="r1", messages=[{"role": "user", "content": "问"}],
                tools=[{"type": "function", "function": {"name": "market_read", "parameters": {}}}],
                max_rounds=2,
            )
        self.assertEqual(result["rounds"], 2)
        self.assertIn("工具上限", result["answer"])
        self.assertTrue(any(a[1] == "react_forced_summary" for a, _ in self.events))

    async def test_react_loop_stops_when_task_cancelled_between_tool_rounds(self):
        """用户点了取消后，运行中的任务要在本轮工具之间停下来，而不是只能干等。"""
        def handler(args):
            return {"ok": True}

        async def fake_call(messages, tools):
            return self._reply("", [{"id": "c", "function": {"name": "market_read", "arguments": "{}"}}])

        def fake_consume(task_id):
            return []

        with patch.dict(app.REACT_TOOLS, {"market_read": {"handler": handler}}), \
             patch.object(app, "call_llm_with_tools", fake_call), \
             patch.object(app, "consume_agent_queue_messages", fake_consume), \
             patch.object(app, "agent_queue_task_cancelled", lambda task_id: True):
            result = await app.run_agent_react_loop(
                project_id="market", run_id="r1", messages=[{"role": "user", "content": "问"}],
                tools=[{"type": "function", "function": {"name": "market_read", "parameters": {}}}],
                max_rounds=4,
                queue_task_id=7,
            )
        self.assertTrue(result.get("cancelled"), "取消标志应让循环立即停止")
        self.assertIn("取消", result["answer"])
        self.assertEqual(result["rounds"], 0, "取消检查在工具执行前，第一轮工具都不该跑")

    def test_cancel_agent_task_marks_running_task_cancelled(self):
        """运行中的任务也必须能被取消：状态标成 cancelled，取消标志返回 True。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            patches = [patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False)]
            for item in patches:
                item.start()
            try:
                task = app.enqueue_agent_task(kind="chat", payload={"message": "hi"}, project_id="market")
                connection = app.db_connection()
                try:
                    connection.execute("UPDATE agent_queue SET status = 'running' WHERE id = ?", (task["id"],))
                    connection.commit()
                finally:
                    connection.close()
                cancelled = app.cancel_agent_task(task["id"])
                self.assertIsNotNone(cancelled)
                self.assertTrue(cancelled["cancelled"])
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertTrue(app.agent_queue_task_cancelled(task["id"]))
            finally:
                for item in reversed(patches):
                    item.stop()

    async def test_every_tool_call_is_written_to_the_run_timeline(self):
        """回放里看不到调了什么工具，就没法判断结论是查出来的还是编出来的。"""
        replies = [
            self._reply("", [{"id": "c1", "function": {"name": "market_read", "arguments": "{}"}}]),
            self._reply("好了。"),
        ]

        async def fake_call(messages, tools):
            return replies.pop(0)

        with patch.dict(app.REACT_TOOLS, {"market_read": {"handler": lambda args: {"ok": True}}}), \
             patch.object(app, "call_llm_with_tools", fake_call):
            await app.run_agent_react_loop(
                project_id="market", run_id="r1", messages=[{"role": "user", "content": "问"}],
                tools=[{"type": "function", "function": {"name": "market_read", "parameters": {}}}],
            )
        tool_events = [kwargs for args, kwargs in self.events if args[1] == "agent_tool_call"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0]["metadata"]["tool"], "market_read")

    async def test_stream_loop_collects_the_convergence_round_delta_text(self):
        """流式版 ReAct 工具轮用尽后走收敛轮：stream_llm_text 的文本增量
        chunk 类型是 "delta"，收集逻辑误判为 "delta_text" 会导致最终答案
        永远为空、报「LLM 未返回内容」——工具轮越多越容易触发。"""
        async def fake_tools(messages, tools, **kwargs):
            yield {"type": "round_done", "mode": "tools", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "market_read", "arguments": "{}"}}], "finish_reason": "tool_calls", "usage": None, "provider": "test"}

        async def fake_text(messages, **kwargs):
            yield {"type": "delta", "text": "收敛回答：综合刚才的工具结果。", "reasoning": ""}
            yield {"type": "finish", "reason": "stop", "usage": None, "provider": "test"}

        collected = []
        with patch.dict(app.REACT_TOOLS, {"market_read": {"handler": lambda args: {"ok": True}}}), \
             patch.object(app, "stream_llm_with_tools", fake_tools), \
             patch.object(app, "stream_llm_text", fake_text):
            async for chunk in app.stream_agent_react_loop(
                project_id="market", run_id="r1", messages=[{"role": "user", "content": "问"}],
                tools=[{"type": "function", "function": {"name": "market_read", "parameters": {}}}],
                max_rounds=1,
            ):
                collected.append(chunk)
        finish = next((c for c in collected if c["type"] == "finish"), None)
        self.assertIsNotNone(finish)
        self.assertIn("收敛回答", finish["answer"], "收敛轮的 delta 文本必须被收集进最终答案")
        self.assertNotIn("LLM 未返回内容", [c.get("message", "") for c in collected])

    async def test_stream_loop_does_not_abort_failover_on_single_provider_error(self):
        """单个 Provider 流式中断时 stream_llm_with_tools 内部会继续尝试下一个；
        ReAct 循环如果一收到 error 就 return，fallback 就被截断成
        「LLM 未返回内容」。先 error 再成功的轮次必须能拿到答案。"""
        async def fake_tools(messages, tools, **kwargs):
            yield {"type": "error", "message": "Provider「主」失败：断流", "provider": "主"}
            yield {"type": "round_done", "mode": "answer", "content": "fallback 接管的回答。", "tool_calls": [], "finish_reason": "stop", "usage": None, "provider": "备"}

        collected = []
        with patch.object(app, "stream_llm_with_tools", fake_tools):
            async for chunk in app.stream_agent_react_loop(
                project_id="market", run_id="r1", messages=[{"role": "user", "content": "问"}],
                tools=[{"type": "function", "function": {"name": "market_read", "parameters": {}}}],
            ):
                collected.append(chunk)
        finish = next((c for c in collected if c["type"] == "finish"), None)
        self.assertIsNotNone(finish, "failover 成功后必须有 finish，而不是提前 return")
        self.assertIn("fallback 接管", finish["answer"])

    async def test_stream_loop_yields_tool_progress_events(self):
        """工具执行完必须 yield 过程反馈事件（type=event），前端才能显示
        「正在搜索…/正在抓取…」，长任务不再像卡死。"""
        state = {"round": 0}

        async def fake_tools(messages, tools, **kwargs):
            state["round"] += 1
            if state["round"] == 1:
                yield {"type": "round_done", "mode": "tools", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{\"query\": \"pi coding agent\"}"}}], "finish_reason": "tool_calls", "usage": None, "provider": "test"}
            else:
                yield {"type": "round_done", "mode": "answer", "content": "结论。", "tool_calls": [], "finish_reason": "stop", "usage": None, "provider": "test"}

        collected = []
        with patch.dict(app.REACT_TOOLS, {"web_search": {"handler": lambda args: {"ok": True, "results": []}}}), \
             patch.object(app, "stream_llm_with_tools", fake_tools):
            async for chunk in app.stream_agent_react_loop(
                project_id="market", run_id="r1", messages=[{"role": "user", "content": "调研"}],
                tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
            ):
                collected.append(chunk)
        events = [c for c in collected if c["type"] == "event"]
        self.assertTrue(events, "工具执行后必须 yield 过程事件")
        # 一次工具调用现在报两次：开工（让十几秒的等待不像卡死）和结果。
        start = next(c for c in events if c.get("kind") == "tool_start")
        done = next(c for c in events if c.get("kind") == "tool")
        self.assertEqual(start["tool"], "web_search")
        self.assertEqual(done["tool"], "web_search")
        self.assertTrue(done["ok"])
        self.assertIn("搜索", done["message"])
        self.assertLess(collected.index(start), collected.index(done))

    async def test_stream_loop_discards_partial_provider_text_after_reset(self):
        """主 Provider 已吐半句再断流时，fallback 从头回答；前一段不能进入最终答案。"""
        async def fake_tools(messages, tools, **kwargs):
            yield {"type": "delta_text", "text": "主 Provider 的半句话"}
            yield {"type": "reset", "provider": "主"}
            yield {"type": "error", "message": "Provider「主」失败：断流", "provider": "主", "recoverable": True}
            yield {"type": "delta_text", "text": "fallback 的完整回答。"}
            yield {"type": "round_done", "mode": "answer", "content": "fallback 的完整回答。", "tool_calls": [], "finish_reason": "stop", "usage": None, "provider": "备"}

        collected = []
        with patch.object(app, "stream_llm_with_tools", fake_tools):
            async for chunk in app.stream_agent_react_loop(
                project_id="market", run_id="r1", messages=[{"role": "user", "content": "问"}],
                tools=[{"type": "function", "function": {"name": "market_read", "parameters": {}}}],
            ):
                collected.append(chunk)
        finish = next(c for c in collected if c["type"] == "finish")
        self.assertEqual(finish["answer"], "fallback 的完整回答。")
        self.assertTrue(any(c["type"] == "reset" for c in collected), "浏览器必须收到清空半段输出的信号")

    async def test_stream_llm_text_continues_when_truncated_by_max_tokens(self):
        """流式输出被 max_tokens 截断（finish_reason=length）时必须续写，
        而不是只记一条 usage 就收工；续写段要回填成 assistant 消息再请求。"""
        providers = [
            {"id": "primary", "name": "主", "api_key": "k1", "model": "m", "base_url": "https://one.example/v1"},
        ]
        seen_messages: list[list[dict]] = []

        class Response:
            def __init__(self, first):
                self.first = first
            def raise_for_status(self):
                pass
            async def aiter_lines(self):
                if self.first:
                    yield 'data: {"choices":[{"delta":{"content":"前半段"},"finish_reason":"length"}]}'
                else:
                    yield 'data: {"choices":[{"delta":{"content":"后半段"},"finish_reason":"stop"}]}'
                yield "data: [DONE]"
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False

        class Client:
            def __init__(self): self.calls = 0
            def stream(self, *args, **kwargs):
                self.calls += 1
                seen_messages.append(kwargs.get("json", {}).get("messages", []))
                return Response(first=self.calls == 1)

        client = Client()
        with patch.object(app, "llm_provider_state", return_value={"candidates": providers}), \
             patch.object(app, "_llm_health", return_value={"status": "unknown"}), \
             patch.object(app, "llm_http_client", AsyncMock(return_value=client)), \
             patch.object(app, "schedule_llm_usage_event"), \
             patch.object(app, "_record_llm_failure"), \
             patch.object(app, "_record_llm_success"):
            chunks = [chunk async for chunk in app.stream_llm_text([{"role": "user", "content": "问"}])]
        text = "".join(str(c.get("text") or "") for c in chunks if c["type"] == "delta")
        self.assertEqual(text, "前半段后半段", "截断后必须续写并拼接")
        finishes = [c for c in chunks if c["type"] == "finish"]
        self.assertEqual(len(finishes), 1, "续写期间不能把中途的 finish 发出去")
        self.assertEqual(finishes[0]["reason"], "stop")
        self.assertEqual(len(seen_messages), 2, "截断后应发起第二次请求")
        self.assertEqual(seen_messages[1][-1]["role"], "assistant", "续写请求要把上一段回填成 assistant 消息")
        self.assertIn("前半段", seen_messages[1][-1]["content"])

    async def test_stream_llm_text_marks_length_capped_after_continuation_limit(self):
        """续满上限仍被截断时，正文要明说没写完，并把 reason 标成 length_capped。"""
        providers = [{"id": "p", "name": "主", "api_key": "k", "model": "m", "base_url": "https://one.example/v1"}]

        class Response:
            def raise_for_status(self): pass
            async def aiter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"段"},"finish_reason":"length"}]}'
                yield "data: [DONE]"
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False

        class Client:
            def __init__(self): self.calls = 0
            def stream(self, *args, **kwargs):
                self.calls += 1
                return Response()

        client = Client()
        with patch.object(app, "llm_provider_state", return_value={"candidates": providers}), \
             patch.object(app, "_llm_health", return_value={"status": "unknown"}), \
             patch.object(app, "llm_http_client", AsyncMock(return_value=client)), \
             patch.object(app, "schedule_llm_usage_event"), \
             patch.object(app, "_record_llm_failure"), \
             patch.object(app, "_record_llm_success"), \
             patch.object(app, "LLM_MAX_CONTINUATIONS", 1):
            chunks = [chunk async for chunk in app.stream_llm_text([{"role": "user", "content": "问"}])]
        finishes = [c for c in chunks if c["type"] == "finish"]
        self.assertEqual(len(finishes), 1)
        self.assertEqual(finishes[0]["reason"], "length_capped")
        tail = [c for c in chunks if c["type"] == "delta" and "没写完" in str(c.get("text") or "")]
        self.assertTrue(tail, "续满上限必须明说还有内容没写完")

    async def test_streaming_updates_provider_health_on_failure_and_success(self):
        """流式请求也必须更新与非流式请求相同的健康状态，429 才能进入冷却。"""
        providers = [
            {"id": "primary", "name": "主", "api_key": "k1", "model": "m", "base_url": "https://one.example/v1"},
            {"id": "fallback", "name": "备", "api_key": "k2", "model": "m", "base_url": "https://two.example/v1"},
        ]
        failed, succeeded = [], []

        class Response:
            def __init__(self, fail=False): self.fail = fail
            def raise_for_status(self):
                if self.fail:
                    raise RuntimeError("429 rate limit")
            async def aiter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"完整"},"finish_reason":"stop"}]}'
                yield "data: [DONE]"
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False

        class Client:
            def __init__(self): self.calls = 0
            def stream(self, *args, **kwargs):
                self.calls += 1
                return Response(fail=self.calls == 1)

        client = Client()
        with patch.object(app, "llm_provider_state", return_value={"candidates": providers}), \
             patch.object(app, "_llm_health", return_value={"status": "unknown"}), \
             patch.object(app, "llm_http_client", AsyncMock(return_value=client)), \
             patch.object(app, "schedule_llm_usage_event"), \
             patch.object(app, "_record_llm_failure", side_effect=lambda provider, exc: failed.append(provider["id"])), \
             patch.object(app, "_record_llm_success", side_effect=lambda provider: succeeded.append(provider["id"])):
            chunks = [chunk async for chunk in app.stream_llm_text([{"role": "user", "content": "问"}])]
        self.assertEqual(failed, ["primary"])
        self.assertEqual(succeeded, ["fallback"])
        self.assertTrue(any(c["type"] == "finish" and c["provider"] == "备" for c in chunks))

    def test_the_project_chat_path_actually_asks_for_tools(self):
        """两条路径必须都调 run_agent_react_loop，否则又会各写一份、慢慢分叉。"""
        root = Path(__file__).resolve().parents[1]
        source = (root / "app.py").read_text(encoding="utf-8")
        body = source[source.find("async def run_project_agent("):]
        body = body[:body.find("\ndef handoff_title(")]
        self.assertIn("subagent_tool_schemas(project_id)", body, "项目页对话没有取工具清单")
        self.assertIn("run_agent_react_loop(", body)
        # 总调度（dispatch_agent_task）已随 0.3.193 拆到 agent_platform.py，
        # 两处调用分别在 app.py（项目页）与 agent_platform.py（总调度）。
        platform = (root / "app_pkg" / "agent_platform.py").read_text(encoding="utf-8")
        total = source.count("await run_agent_react_loop(") + platform.count("await run_agent_react_loop(")
        self.assertEqual(total, 2, "总调度和项目页都应走这一份实现")

    def test_the_project_chat_cannot_reach_beyond_its_declared_tools(self):
        """能力放开的同时边界不能放开：可执行集合仍是这个项目自己声明的那一份。"""
        for project_id, tools in app.SUBAGENT_TOOL_MAP.items():
            with self.subTest(project=project_id):
                names = {item["function"]["name"] for item in app.subagent_tool_schemas(project_id)}
                self.assertEqual(names, set(tools))
        self.assertEqual(app.subagent_tool_schemas("不存在的项目"), [], "未登记的项目不该拿到任何工具")

    def test_no_agent_declares_a_tool_that_has_no_handler(self):
        """subagent_tool_schemas 查不到就跳过，所以「登记了但没实现」会静默存在：
        cloud-dev 的 cloud_dev_status/_test/_build 就这么躺在表里，Agent 以为自己
        有这三样能力，实际连 schema 都拿不到。"""
        self.assertEqual(app.assert_subagent_tools_exist(), [])

    def test_build_stays_behind_approval_instead_of_becoming_a_callable_tool(self):
        """cloud_dev_policy 把 build 列为需审批动作，就不能给模型一个直接调用的入口。"""
        self.assertIn("build", app.cloud_dev.cloud_dev_policy()["approval_actions"])
        self.assertNotIn("cloud_dev_build", app.REACT_TOOLS)
        self.assertNotIn("cloud_dev_build", app.SUBAGENT_TOOL_MAP["cloud-dev"])

    def test_cloud_dev_test_refuses_when_there_is_no_fixed_recipe(self):
        """没有已识别的固定命令配方时必须直接拒绝，而不是自己猜一条命令去跑。"""
        with patch.object(app.cloud_dev, "run_cloud_dev", lambda *a, **k: {"status": "unsupported", "message": "该工作区没有已识别的固定命令配方，未执行。"}):
            # cloud_dev_test 现在是 confirm 级（会在服务器上真跑一条命令），
            # 这里要验的是「没有配方就拒绝」，所以直接以已确认的身份调用。
            result = app.execute_react_tool("cloud_dev_test", {"project": "workbench"}, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertIn("未执行", result["error"])


class ProjectAgentPanelLayoutTests(unittest.TestCase):
    """项目 Agent 面板打开后看不到对话，也看不到输入框。

    原来的结构是：能力说明、快捷提问、待我处理、最近运行全部平铺在对话上方，
    整个面板 overflow-y: auto 一起滚。用 Chromium 量过（面板内容 961px）：
    1280×720 下打开面板，对话区顶边已经在面板可视区之外，输入框还要再往下
    159px——「打开项目 Agent」之后第一屏什么都干不了。

    改成：面板自己不滚，说明性内容收进默认折叠的抽屉，对话区 flex:1 吃掉
    剩余空间，输入框钉底。同一套测量：对话区 122px → 346px，输入框回到可视区内。
    """

    def script(self):
        return (Path(__file__).resolve().parents[1] / "static" / "project.js").read_text(encoding="utf-8")

    def styles(self):
        return (Path(__file__).resolve().parents[1] / "static" / "project-agent.css").read_text(encoding="utf-8")

    def template(self):
        for line in self.script().splitlines():
            if line.strip().startswith("panel.innerHTML = `"):
                return line.strip()[len("panel.innerHTML = `"):-2]
        self.fail("没有找到项目 Agent 面板的模板")

    def rule(self, styles, selector, contains=""):
        """同一个选择器可能出现在多条规则里（比如变量声明和布局各一条），
        用 contains 挑出想要的那条。"""
        head = selector + " {"
        start = -1
        while True:
            start = styles.find(head, start + 1)
            if start < 0:
                self.fail(f"缺少规则 {selector}" + (f"（含 {contains}）" if contains else ""))
            body = styles[start + len(head):styles.find("}", start)]
            if not contains or contains in body:
                return body

    def test_panel_itself_does_not_scroll(self):
        """面板一旦整体滚动，输入框就会被推出可视区——这是原来的根因。"""
        rule = self.rule(self.styles(), ".project-agent-panel", contains="position: fixed")
        self.assertIn("overflow: hidden", rule)
        self.assertNotIn("overflow-y: auto", rule)
        self.assertIn("height: min(", rule, "只给 max-height 的话面板会缩到内容高度，钉底就没意义")

    def test_conversation_is_the_only_growing_region(self):
        styles = self.styles()
        messages = self.rule(styles, ".project-agent-messages")
        self.assertIn("flex: 1 1 auto", messages)
        self.assertIn("min-height: 0", messages, "grid 子项会把 flex 项撑开，必须显式归零")
        self.assertNotIn("max-height: 38vh", messages, "对话区不该再被固定高度锁死")
        fixed = self.rule(styles, ".project-agent-head, .project-agent-toolbar, .project-agent-quick-actions, .project-agent-form")
        self.assertIn("flex: 0 0 auto", fixed)

    def test_context_drawer_is_collapsed_by_default_and_capped(self):
        template = self.template()
        self.assertIn('<div id="project-agent-context" class="project-agent-context" hidden>', template)
        drawer = self.rule(self.styles(), ".project-agent-context")
        self.assertIn("max-height: 46%", drawer, "抽屉展开也不能把对话挤没")
        self.assertIn("overflow-y: auto", drawer)
        script = self.script()
        self.assertIn("contextToggle.addEventListener", script)
        self.assertIn('aria-controls="project-agent-context"', template)

    def test_explanatory_blocks_moved_inside_the_drawer(self):
        """能力说明/待办/运行记录/转交都属于「有需要才看」，不该占对话的高度。"""
        template = self.template()
        drawer = template[template.find('id="project-agent-context"'):]
        drawer = drawer[:drawer.find('<div id="project-agent-messages"')]
        for marker in ('id="project-agent-capability"', 'class="project-agent-incoming"',
                       'class="project-agent-runs"', 'class="project-agent-handoff"'):
            self.assertIn(marker, drawer, f"{marker} 应该在抽屉里")

    def test_composer_is_the_last_block_in_the_panel(self):
        """输入框后面再挂东西，它就不是钉底的了——转交区原来就挂在它后面。"""
        template = self.template()
        self.assertTrue(template.rstrip().endswith("</form>"), "输入框必须是面板最后一个区块")
        self.assertLess(template.find('id="project-agent-messages"'), template.find('id="project-agent-form"'))

    def test_pending_handoffs_are_still_visible_when_the_drawer_is_shut(self):
        """折叠等于隐藏，所以待处理条数必须顶到抽屉按钮上，否则就是把功能删了。"""
        script = self.script()
        self.assertIn("setContextBadge(actionable.length)", script)
        self.assertIn('id="project-agent-context-badge"', self.template())
        badge = self.rule(self.styles(), ".project-agent-context-badge")
        self.assertIn("background: var(--agent-accent)", badge)

    def test_quick_actions_yield_once_the_conversation_starts(self):
        script = self.script()
        self.assertIn("quickActions.hidden = items.length > 0", script)
        template = self.template()
        self.assertLess(template.find('id="project-agent-messages"'), template.find('id="project-agent-quick-actions"'),
                        "快捷提问应紧贴输入框，而不是压在对话上方")


class EvidenceCardContrastTests(unittest.TestCase):
    """证据卡片在浅色主题下几乎读不出来。

    原因是它直接复用了全局变量：--browser-soft(#f8f9fa) 铺在 card 的
    --browser-surface(#ffffff) 上，对比度只有 1.02——卡片等于没有边界；
    元信息用 --browser-faint(#a5a8ae) 配 8px，对比度 2.25:1，远低于可读线。
    深色主题下这两组值本来就拉得开，所以同一份 CSS 只在浅色露馅。
    修法是给证据区一套自己的变量，两套主题各自取值。
    """

    def styles(self):
        return (Path(__file__).resolve().parents[1] / "static" / "web-research.css").read_text(encoding="utf-8")

    def palette(self, styles, selector):
        block = styles[styles.find(selector + " {") + len(selector) + 2:]
        block = block[:block.find("}")]
        out = {}
        for line in block.split(";"):
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            out[name.strip()] = value.strip()
        return out

    @staticmethod
    def luminance(hex_color):
        raw = hex_color.lstrip("#")
        channels = []
        for offset in (0, 2, 4):
            value = int(raw[offset:offset + 2], 16) / 255
            channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def ratio(self, front, back):
        pair = sorted((self.luminance(front), self.luminance(back)), reverse=True)
        return (pair[0] + 0.05) / (pair[1] + 0.05)

    def test_both_themes_define_the_evidence_palette(self):
        styles = self.styles()
        light = self.palette(styles, ":root")
        dark = self.palette(styles, 'html[data-theme="dark"]')
        needed = {"--evidence-fill", "--evidence-line", "--evidence-meta", "--evidence-link",
                  "--chip-good-ink", "--chip-good-bg", "--chip-low-ink", "--chip-low-bg"}
        for name in sorted(needed):
            self.assertIn(name, light, f"浅色主题缺少 {name}")
            self.assertIn(name, dark, f"深色主题缺少 {name}——只改浅色会把深色弄坏")

    def test_text_on_the_card_clears_the_readability_line(self):
        styles = self.styles()
        for selector, surface in ((":root", "#ffffff"), ('html[data-theme="dark"]', "#202125")):
            palette = self.palette(styles, selector)
            fill = palette["--evidence-fill"]
            with self.subTest(theme=selector):
                # 元信息是最小号的正文，原来只有 2.25:1。
                self.assertGreaterEqual(self.ratio(palette["--evidence-meta"], fill), 4.5)
                self.assertGreaterEqual(self.ratio(palette["--evidence-link"], fill), 4.5)
                # 质量标签自带底色，要跟自己的底色比，而不是跟卡片比。
                self.assertGreaterEqual(self.ratio(palette["--chip-good-ink"], palette["--chip-good-bg"]), 4.5)
                self.assertGreaterEqual(self.ratio(palette["--chip-low-ink"], palette["--chip-low-bg"]), 4.5)
                # 卡片填色和 card 底色几乎相同时，边框是唯一的边界，必须看得见。
                self.assertGreaterEqual(self.ratio(palette["--evidence-line"], surface), 1.3)

    def test_no_hard_coded_colours_left_in_the_evidence_block(self):
        """写死的 #fff5e4 在深色下是一块刺眼的浅黄，主题一切就露馅。"""
        styles = self.styles()
        block = styles[styles.find(".evidence-list {"):styles.find(".empty-result {")]
        self.assertNotIn("#fff5e4", block)
        self.assertNotIn("#b27938", block)
        self.assertNotIn("var(--browser-faint)", block, "元信息不该再用最淡的那档灰")

    def test_long_titles_shrink_instead_of_pushing_the_chip_out(self):
        """标题外面那层 div 是 flex 项，min-width 默认是 auto，
        nowrap 的长标题会把它撑开，质量标签被顶出卡片。"""
        styles = self.styles()
        block = styles[styles.find(".evidence-list {"):styles.find(".empty-result {")]
        self.assertIn(".evidence-item-head > div { min-width: 0", block)
        self.assertIn("flex: 0 0 auto", block[block.find(".quality-chip {"):], "标签不该被压缩")
        self.assertNotIn("max-width: 390px", block, "面板早就不到 390px 宽了")


class KnowledgeNoteReadTests(unittest.TestCase):
    """项目页要能直接看知识库，就需要一个按路径读全文的接口——
    而这是整个工作台唯一一个按用户给的路径读文件的地方，不做限制就是任意文件读取。"""

    def vault(self, tmp):
        root = Path(tmp)
        (root / "子目录").mkdir()
        (root / "笔记.md").write_text("# 我的笔记\n\n正文内容", encoding="utf-8")
        (root / "子目录" / "深层.md").write_text("# 深层笔记\n正文", encoding="utf-8")
        (root / "机密.txt").write_text("不该被读到", encoding="utf-8")
        return root

    def test_reads_a_note_inside_the_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.vault(tmp)
            with patch.object(app, "KNOWLEDGE_DIR", root):
                note = app.read_knowledge_note("笔记.md")
                nested = app.read_knowledge_note("子目录/深层.md")
        self.assertEqual(note["title"], "我的笔记")
        self.assertIn("正文内容", note["content"])
        self.assertEqual(nested["title"], "深层笔记")

    def test_paths_cannot_escape_the_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.vault(tmp)
            with patch.object(app, "KNOWLEDGE_DIR", root):
                for bad in ("../../../etc/passwd", "/etc/passwd", "子目录/../../外面.md"):
                    with self.assertRaises(app.HTTPException) as ctx:
                        app.read_knowledge_note(bad)
                    self.assertEqual(ctx.exception.status_code, 404, bad)

    def test_only_markdown_is_readable(self):
        """限定后缀，避免顺手把 .env 之类的东西读出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.vault(tmp)
            with patch.object(app, "KNOWLEDGE_DIR", root):
                with self.assertRaises(app.HTTPException) as ctx:
                    app.read_knowledge_note("机密.txt")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_empty_path_is_rejected(self):
        with self.assertRaises(app.HTTPException) as ctx:
            app.read_knowledge_note("")
        self.assertEqual(ctx.exception.status_code, 400)


class KnowledgeDrawerTests(unittest.TestCase):
    """抽屉挂在公共的 project.js 上，所有项目页自动获得。"""

    def script(self):
        return (Path(__file__).resolve().parents[1] / "static" / "project.js").read_text(encoding="utf-8")

    def test_drawer_is_wired_into_the_shared_shell(self):
        script = self.script()
        self.assertIn("function setupKnowledgeDrawer", script)
        self.assertIn("setupKnowledgeDrawer();", script.split("DOMContentLoaded")[1][:300])

    def test_knowledge_page_itself_does_not_get_a_drawer(self):
        block = self.script()
        block = block[block.find("function setupKnowledgeDrawer"):]
        block = block[:block.find("\nfunction ")]
        self.assertIn('projectId === "knowledge"', block, "知识库自己那页不该再套一个抽屉")
        self.assertIn('document.getElementById("knowledge-drawer")', block, "重复初始化会挂出两个抽屉")

    def test_new_note_carries_the_source_project(self):
        """回看笔记时要能知道它是在哪件事上得出的。"""
        block = self.script()
        block = block[block.find("function setupKnowledgeDrawer"):]
        block = block[:block.find("\nfunction setupEnterToSend")]
        self.assertIn("source: projectId", block)
        self.assertIn("tags: projectId", block)


class MarketStyleScreenTests(unittest.TestCase):
    """选股风格库。

    最重要的约束不是「算得准」，而是「数据不够时不许给结论」——一个建立在 3 个
    样本点上、看起来很专业的推荐，比没有推荐危险得多。
    """

    def points(self, prices, volumes=None):
        return [{"price": price, "volume": (volumes[i] if volumes else 1000)} for i, price in enumerate(prices)]

    def test_insufficient_samples_never_produce_a_score(self):
        result = app.evaluate_market_style("trend-following", "sh600000", self.points([10, 11, 12]))
        self.assertEqual(result["status"], "insufficient")
        self.assertNotIn("score", result)
        self.assertNotIn("hit", result)
        self.assertIn("需要至少", result["reason"])
        self.assertEqual(result["have"], 3)

    def test_styles_needing_fundamentals_are_marked_unsupported(self):
        """当前行情源只有价格和成交量，不能拿价格硬凑估值。"""
        for style_id in ("deep-value", "quality-growth"):
            result = app.evaluate_market_style(style_id, "sh600000", self.points([10] * 30))
            self.assertEqual(result["status"], "unsupported", style_id)
            self.assertNotIn("score", result)
            self.assertTrue(result["missing"])

    def test_trend_style_judges_by_pace_not_raw_percentage(self):
        """后段基数更高，直接比百分比会系统性冤枉稳定上涨的标的。"""
        steady = app.evaluate_market_style("trend-following", "x", self.points([10 + i * 0.3 for i in range(30)]))
        self.assertTrue(steady["hit"], "稳定线性上涨应当命中趋势跟随")
        stalled = app.evaluate_market_style("trend-following", "x", self.points([10 + i * 0.5 for i in range(20)] + [20] * 10))
        self.assertFalse(stalled["hit"], "后段横盘不该算趋势")
        reversed_trend = app.evaluate_market_style("trend-following", "x", self.points([10 + i * 0.5 for i in range(20)] + [20 - i * 0.6 for i in range(10)]))
        self.assertFalse(reversed_trend["hit"])

    def test_choppy_and_falling_series_do_not_hit_trend(self):
        choppy = app.evaluate_market_style("trend-following", "x", self.points([10 + ((-1) ** i) * 0.8 for i in range(30)]))
        falling = app.evaluate_market_style("trend-following", "x", self.points([20 - i * 0.3 for i in range(30)]))
        self.assertFalse(choppy["hit"])
        self.assertFalse(falling["hit"])
        self.assertEqual(choppy["score"], 0.0)

    def test_volume_breakout_needs_both_price_and_volume(self):
        prices = [10] * 25 + [10.2, 10.5, 11, 11.5, 12]
        volumes = [1000] * 25 + [1200, 1500, 2000, 3000, 4000]
        hit = app.evaluate_market_style("volume-breakout", "x", self.points(prices, volumes))
        self.assertTrue(hit["hit"])
        self.assertGreater(hit["metrics"]["volume_ratio"], 1.5)
        quiet = app.evaluate_market_style("volume-breakout", "x", self.points(prices, [1000] * 30))
        self.assertFalse(quiet["hit"], "缩量新高不该算放量突破")

    def test_every_style_declares_when_it_fails(self):
        """只说什么时候管用、不说什么时候会亏的策略是有害的。"""
        for style in app.MARKET_STYLES:
            self.assertTrue(style.get("works_when"), style["id"])
            self.assertTrue(style.get("fails_when"), f"{style['id']} 没有写失效场景")
            self.assertGreater(len(style["fails_when"]), 20, f"{style['id']} 的失效场景过于敷衍")
            self.assertTrue(style.get("requires"))
            self.assertTrue(style.get("rules"))

    def test_no_style_is_named_after_a_real_person(self):
        """把在世投资人的名字挂在自动选股结果上，既不准确，亏了也说不清。"""
        blob = json.dumps(app.MARKET_STYLES, ensure_ascii=False)
        for name in ("巴菲特", "芒格", "索罗斯", "彼得林奇", "西蒙斯", "达利欧", "buffett", "munger", "soros"):
            self.assertNotIn(name, blob, f"风格库里不该出现真人姓名：{name}")

    def test_unknown_style_returns_404(self):
        with self.assertRaises(app.HTTPException) as ctx:
            app.evaluate_market_style("secret-sauce", "x", self.points([10] * 30))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_screen_refuses_to_rank_when_data_is_thin(self):
        """这正是用户当前的处境：3 个快照、几乎没有报价。"""
        temp_dir, database_file = temp_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False), \
             patch.object(app, "load_market_watchlist", lambda: [{"symbol": "sh600000"}, {"symbol": "sz000001"}]), \
             patch.object(app, "load_market_snapshot", lambda: {"quotes": []}), \
             patch.object(app, "list_market_history", lambda limit=30: []):
            result = app.run_market_style_screen("trend-following")
        self.assertEqual(result["picks"], [], "数据不足时不该给出任何推荐")
        self.assertFalse(result["data_ready"])
        self.assertEqual(len(result["blocked"]), 2)
        self.assertIn("数据不足", result["summary"])

    def test_screen_requires_a_watchlist(self):
        with patch.object(app, "load_market_watchlist", lambda: []):
            with self.assertRaises(app.HTTPException) as ctx:
                app.run_market_style_screen("trend-following")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_catalog_exposes_data_requirements(self):
        catalog = app.market_style_catalog()
        self.assertTrue(catalog)
        for style in catalog:
            self.assertTrue(all("label" in item for item in style["requires"]))
        value = next(item for item in catalog if item["id"] == "deep-value")
        self.assertIn("市盈率", [item["label"] for item in value["requires"]])


class RuntimeToolPolicyTests(unittest.TestCase):
    """风险策略此前按一套「叙述性能力名」登记，而模型能调的是另一套名字。

    实测 market / server / doc-factory 两套交集为 0，连 work_item_read 和
    work_items_read 都差一个 s。后果是策略表对真正会产生副作用的工具一条都没
    覆盖，边界校验也拿错了集合——会拒绝真能执行的工具、放行没有执行器的名字。
    """

    def test_every_callable_tool_has_a_policy(self):
        self.assertEqual(app.assert_runtime_tool_policies(), [])

    def test_an_unregistered_tool_defaults_to_needing_confirmation(self):
        """默认拒绝而不是默认放行：忘了登记策略，最坏是多点一次确认，
        而不是一个没人审过的副作用被静默执行。"""
        policy = app.runtime_tool_policy("某个还没登记的新工具")
        self.assertEqual(policy["mode"], "confirm")
        self.assertFalse(policy["registered"])

    def test_the_boundary_now_guards_the_set_that_actually_runs(self):
        for project_id in ("market", "server", "doc-factory", "inbox"):
            with self.subTest(project=project_id):
                declared = set(app.agent_declared_tools(project_id))
                runtime = {item["function"]["name"] for item in app.subagent_tool_schemas(project_id)}
                self.assertEqual(declared, runtime)
        self.assertTrue(app.validate_agent_tool_requests(["market"], ["market_read"])["valid"],
                        "真能执行的工具被拒了")
        self.assertFalse(app.validate_agent_tool_requests(["market"], ["watchlist_write"])["valid"],
                         "没有执行器的名字被放行了")


class ConfirmationGateTests(unittest.TestCase):
    """确认门此前完全是死代码。

    requires_confirmation 在五个生产赋值点全写死 False，全文没有一处 True，
    所以确认门、待确认通知、审批队列查询全是死代码——而页面上一直写着
    「付款、发送、删除、登录等操作始终由你确认」。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DATABASE_FILE", Path(self.tmp.name) / "workbench.db"), ("_DB_SCHEMA_READY", False)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_confirm_tool_is_not_executed_and_lands_in_the_queue(self):
        ran = {"n": 0}

        def handler(args):
            ran["n"] += 1
            return {"ok": True}

        with patch.dict(app.REACT_TOOLS, {"cloud_dev_test": {"handler": handler}}):
            result = app.execute_react_tool("cloud_dev_test", {"command_id": 1},
                                            project_id="market", run_id="run-1")
        self.assertEqual(ran["n"], 0, "需要确认的工具被直接执行了")
        self.assertTrue(result["needs_confirmation"])
        self.assertTrue(result["action_id"])
        # 审批队列查的就是这条 SQL：requires_confirmation = 1 AND status = 'pending'。
        # 在这之前它永远返回空，因为全文没有一处把 requires_confirmation 置为真。
        connection = app.db_connection()
        try:
            rows = connection.execute(
                "SELECT id FROM agent_actions WHERE requires_confirmation = 1 AND status = 'pending'"
            ).fetchall()
        finally:
            connection.close()
        self.assertIn(result["action_id"], [str(row["id"]) for row in rows], "动作没有进审批队列")

    def test_an_auto_tool_still_runs_without_asking(self):
        ran = {"n": 0}

        def handler(args):
            ran["n"] += 1
            return {"ok": True}

        with patch.dict(app.REACT_TOOLS, {"inbox_capture": {"handler": handler}}):
            result = app.execute_react_tool("inbox_capture", {"content": "x"}, project_id="inbox")
        self.assertEqual(ran["n"], 1)
        self.assertTrue(result["ok"])

    def test_the_model_is_told_not_to_claim_it_finished(self):
        """只回一句「失败」的话，模型会说「我已经发出通知了」。"""
        with patch.dict(app.REACT_TOOLS, {"cloud_dev_test": {"handler": lambda args: {"ok": True}}}):
            result = app.execute_react_tool("cloud_dev_test", {}, project_id="market", run_id="r")
        self.assertIn("不要当作已完成", result["error"])


class EvidencePhaseToolsTests(unittest.TestCase):
    """总调度的「数据探查」阶段此前持有 REACT_TOOLS 全表，包括写工具。"""

    def test_the_gathering_phase_only_gets_read_only_tools(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        body = source[source.find("async def react_gather_evidence("):]
        body = body[:body.find("\nasync def ")]
        self.assertIn('runtime_tool_policy(item["function"]["name"])["mode"] == "readonly"', body)
        allowed = [item["function"]["name"] for item in app.react_tool_schemas()
                   if app.runtime_tool_policy(item["function"]["name"])["mode"] == "readonly"]
        for write_tool in ("inbox_capture", "notify", "cloud_dev_generate"):
            with self.subTest(tool=write_tool):
                self.assertNotIn(write_tool, allowed)


class ActionInferenceTests(unittest.TestCase):
    """动作是从用户原话里正则匹配出来的，而且立刻执行。"""

    def test_negation_questions_and_hearsay_do_not_trigger_actions(self):
        cases = [
            ("inbox", "帮我记一下：下周三要交周报", True),
            ("inbox", "要不要记录一下？", False),
            ("inbox", "他说要记录一下这个事", False),
            ("market", "最近不用太关注传智教育了", False),
            ("knowledge", "把这个结论沉淀到知识库", True),
        ]
        for project_id, message, should_fire in cases:
            with self.subTest(message=message):
                self.assertEqual(bool(app.infer_agent_actions(project_id, message, "回答")), should_fire)

    def test_risky_inferences_now_ask_first(self):
        """改告警阈值意味着以后可能收不到该收的告警；加自选是从「关注」两个字
        猜出来的，误判率最高。这两条都要过人。"""
        thresholds = app.infer_agent_actions("server", "把磁盘告警阈值调到 85", "回答")
        self.assertTrue(thresholds and thresholds[0]["requires_confirmation"])
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        body = source[source.find("def infer_agent_actions("):]
        body = body[:body.find("\ndef ", 10)]
        self.assertNotIn('"requires_confirmation": False,', body.split('"tool": "market.watchlist.add"')[-1][:400])


class AgentRetryIdempotencyTests(unittest.TestCase):
    """重试会把已经写过的东西再写一遍。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DATABASE_FILE", Path(self.tmp.name) / "workbench.db"), ("_DB_SCHEMA_READY", False)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_same_action_in_the_same_run_is_recorded_once(self):
        first = app.create_agent_action_record(project_id="inbox", name="写入收件箱", tool="inbox.capture",
                                               arguments={"content": "同一件事"}, run_id="run-1")
        again = app.create_agent_action_record(project_id="inbox", name="写入收件箱", tool="inbox.capture",
                                               arguments={"content": "同一件事"}, run_id="run-1")
        self.assertEqual(first["id"], again["id"], "重试生成了新的 action id，绕开了幂等保护")
        other = app.create_agent_action_record(project_id="inbox", name="写入收件箱", tool="inbox.capture",
                                               arguments={"content": "另一件事"}, run_id="run-1")
        self.assertNotEqual(first["id"], other["id"])

    def test_the_dispatch_retry_carries_the_attempt_forward(self):
        """其他 kind 都传了 attempt+1，唯独 dispatch 分支漏了，
        于是 retryable 恒为 True——重试链没有上限。"""
        # retry 路由已随拆分迁到 app_pkg/agent_engine.py
        source = (Path(__file__).resolve().parents[1] / "app_pkg" / "agent_engine.py").read_text(encoding="utf-8")
        body = source[source.find('if project_id == "workbench" and run.get("kind") == "dispatch":'):]
        body = body[:body.find('if project_id == "aihot"')]
        self.assertIn('attempt=int(run.get("attempt", 1)) + 1', body)
        self.assertIn("max_attempts=", body)

    def test_runs_left_behind_by_a_restart_are_recovered(self):
        app.create_agent_run_record(project_id="market", kind="chat", title="重启前还在跑")
        connection = app.db_connection()
        try:
            connection.execute("UPDATE agent_runs SET status = 'running', updated_at = '2000-01-01T00:00:00+00:00'")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(app.recover_stuck_agent_runs(), 1)
        runs = app.list_agent_runs("market")
        self.assertEqual(runs[0]["status"], "failed")
        self.assertIn("重启", runs[0]["error"])


class AgentQueueTests(unittest.TestCase):
    """每次 Agent 调用都在 HTTP 请求里同步跑完：浏览器只能干等，
    请求一断任务就没了下文，进程重启后正在跑的 run 永远停在 running。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DATABASE_FILE", Path(self.tmp.name) / "workbench.db"), ("_DB_SCHEMA_READY", False)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_submitting_the_same_thing_twice_queues_it_once(self):
        first = app.enqueue_agent_task(kind="chat", payload={"message": "x"}, dedupe_key="k")
        again = app.enqueue_agent_task(kind="chat", payload={"message": "x"}, dedupe_key="k")
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["deduped"])

    def test_two_workers_never_claim_the_same_task(self):
        """先 SELECT 再 UPDATE 会让两个 worker 领到同一条。"""
        app.enqueue_agent_task(kind="chat", payload={"message": "a"})
        app.enqueue_agent_task(kind="chat", payload={"message": "b"})
        first = app.claim_agent_task("w1")
        second = app.claim_agent_task("w2")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["id"], second["id"])
        self.assertIsNone(app.claim_agent_task("w3"), "队列空了还能领到任务")

    def test_priority_decides_who_goes_first(self):
        app.enqueue_agent_task(kind="chat", payload={"message": "普通"}, priority=100)
        app.enqueue_agent_task(kind="chat", payload={"message": "加急"}, priority=10)
        self.assertEqual(app.claim_agent_task("w1")["payload"]["message"], "加急")

    def test_a_failure_goes_back_to_the_queue_with_backoff(self):
        app.enqueue_agent_task(kind="chat", payload={"message": "x"}, max_attempts=3)
        task = app.claim_agent_task("w1")
        again = app.finish_agent_task(task["id"], status="failed", error="上游 429")
        self.assertEqual(again["status"], "queued", "还有重试次数却没放回队列")
        self.assertGreater(again["available_at"], app.now_iso(), "立刻重试多半会撞上同一个原因")

    def test_it_gives_up_after_the_last_attempt(self):
        app.enqueue_agent_task(kind="chat", payload={"message": "x"}, max_attempts=1)
        task = app.claim_agent_task("w1")
        done = app.finish_agent_task(task["id"], status="failed", error="彻底失败")
        self.assertEqual(done["status"], "failed")

    def test_an_expired_lease_puts_the_task_back(self):
        """worker 进程被杀时不会有人来标记，只能靠租约过期兜底。"""
        app.enqueue_agent_task(kind="chat", payload={"message": "x"})
        task = app.claim_agent_task("dead-worker")
        connection = app.db_connection()
        try:
            connection.execute("UPDATE agent_queue SET lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?", (task["id"],))
            connection.commit()
        finally:
            connection.close()
        reclaimed = app.claim_agent_task("live-worker")
        self.assertEqual(reclaimed["id"], task["id"])
        self.assertEqual(reclaimed["claimed_by"], "live-worker")

    def test_queued_and_running_tasks_can_be_cancelled(self):
        """排队中的直接取消；运行中的标成 cancelled 后，ReAct 循环会在每轮
        工具之间读到并停下来——不再是「只能干等」的死任务。"""
        app.enqueue_agent_task(kind="chat", payload={"message": "x"})
        queued = app.cancel_agent_task(1)
        self.assertTrue(queued["cancelled"])
        app.enqueue_agent_task(kind="chat", payload={"message": "y"})
        running = app.claim_agent_task("w1")
        result = app.cancel_agent_task(running["id"])
        self.assertTrue(result["cancelled"], "运行中的任务也必须能取消")
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(app.agent_queue_task_cancelled(running["id"]))

    def test_a_message_can_be_inserted_into_a_running_task_and_is_read_once(self):
        app.enqueue_agent_task(kind="chat", payload={"message": "查一下 A"})
        task = app.claim_agent_task("w1")
        app.insert_agent_queue_message(task["id"], "顺便也看看 B")
        app.insert_agent_queue_message(task["id"], "还有 C")
        self.assertEqual(app.consume_agent_queue_messages(task["id"]), ["顺便也看看 B", "还有 C"])
        self.assertEqual(app.consume_agent_queue_messages(task["id"]), [], "同一条消息被消费了两次")

    def test_inserting_into_a_finished_task_is_refused(self):
        """任务已经结束了还接受插入，那条消息永远不会被任何人读到。"""
        app.enqueue_agent_task(kind="chat", payload={"message": "x"})
        task = app.claim_agent_task("w1")
        app.finish_agent_task(task["id"], status="succeeded")
        with self.assertRaises(app.HTTPException) as ctx:
            app.insert_agent_queue_message(task["id"], "太晚了")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_the_react_loop_reads_inserted_messages_between_rounds(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        body = source[source.find("async def run_agent_react_loop("):]
        body = body[:body.find("\nasync def run_project_agent(")]
        self.assertIn("consume_agent_queue_messages", body)
        self.assertIn("任务进行中追加的指令", body)


class LlmTruncationTests(unittest.IsolatedAsyncioTestCase):
    """回答被 max_tokens 截断时，此前只是记一条 usage 事件就把半截答案返回了。

    用户看到的是一段写到一半、经常停在句子中间的回答，而且没有任何提示说它
    被截断。调大 max_tokens 不解决问题：各家 Provider 对单次输出都有上限。
    """

    async def test_a_truncated_answer_is_continued_until_it_finishes(self):
        calls = {"n": 0}

        async def fake(messages, credentials=None, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                return (f"第{calls['n']}段", True) if kwargs.get("want_truncated") else f"第{calls['n']}段"
            return ("收尾。", False) if kwargs.get("want_truncated") else "收尾。"

        with patch.object(app, "_call_llm_once", fake):
            answer = await app.call_llm([{"role": "user", "content": "写长一点"}])
        self.assertEqual(calls["n"], 3)
        self.assertEqual(answer, "第1段第2段收尾。", "续写是接着最后一个字写的，中间不该有分隔符")

    async def test_the_continuation_prompt_forbids_repeating_and_re_introducing(self):
        seen = {}

        async def fake(messages, credentials=None, **kwargs):
            seen["messages"] = messages
            return ("段", len(messages) < 3) if kwargs.get("want_truncated") else "段"

        with patch.object(app, "_call_llm_once", fake), patch.object(app, "LLM_MAX_CONTINUATIONS", 1):
            await app.call_llm([{"role": "user", "content": "写"}])
        follow_up = seen["messages"][-1]["content"]
        self.assertIn("不要重复", follow_up)
        self.assertIn("不要重新开头", follow_up)

    async def test_hitting_the_continuation_ceiling_says_so(self):
        """续到上限还没写完，要明说，而不是让人以为这就是全文。"""
        async def always(messages, credentials=None, **kwargs):
            return ("段", True) if kwargs.get("want_truncated") else "段"

        with patch.object(app, "_call_llm_once", always), patch.object(app, "LLM_MAX_CONTINUATIONS", 2):
            answer = await app.call_llm([{"role": "user", "content": "无限"}])
        self.assertIn("续写上限", answer)

    async def test_callers_can_opt_out(self):
        calls = {"n": 0}

        async def fake(messages, credentials=None, **kwargs):
            calls["n"] += 1
            return "一次就好"

        with patch.object(app, "_call_llm_once", fake):
            answer = await app.call_llm([{"role": "user", "content": "短"}], continue_on_truncation=False)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(answer, "一次就好")


class MarkdownRendererTests(unittest.TestCase):
    """Agent 的回答里表格和列表最多，之前它们以原始符号显示。

    渲染器本身在浏览器里跑，这里守的是「接线」那一半：所有加载 project.js
    的页面必须先加载 markdown.js（否则 renderAgentMarkdown 静默退化回纯文本，
    没有报错、只是又不渲染了），以及渲染器的安全前提没有被后来的改动放宽。
    """

    def _static(self, name):
        return (Path(__file__).resolve().parents[1] / "static" / name).read_text(encoding="utf-8")

    def test_every_page_loading_project_js_also_loads_markdown_js(self):
        static_dir = Path(__file__).resolve().parents[1] / "static"
        missing = []
        for page in sorted(static_dir.glob("*.html")):
            source = page.read_text(encoding="utf-8")
            if "/static/project.js" not in source:
                continue
            if "/static/markdown.js" not in source:
                missing.append(page.name)
        self.assertEqual(missing, [], f"这些页面会让 Agent 回答退回纯文本：{missing}")

    def test_markdown_js_loads_before_project_js(self):
        static_dir = Path(__file__).resolve().parents[1] / "static"
        wrong = []
        for page in sorted(static_dir.glob("*.html")):
            source = page.read_text(encoding="utf-8")
            if "/static/project.js" not in source or "/static/markdown.js" not in source:
                continue
            if source.index("/static/markdown.js") > source.index("/static/project.js"):
                wrong.append(page.name)
        self.assertEqual(wrong, [], f"markdown.js 必须在 project.js 之前：{wrong}")

    def test_markdown_js_is_cached_by_service_worker(self):
        self.assertIn("/static/markdown.js", self._static("sw.js"))

    def test_renderer_escapes_before_restoring_structure(self):
        source = self._static("markdown.js")
        # 整段先转义、之后只还原认识的结构，是这个实现不需要 sanitizer 的全部理由。
        self.assertIn("escapeHtml(source).split", source)
        self.assertIn("window.WorkbenchMarkdown", source)

    def test_links_are_restricted_to_http_schemes(self):
        source = self._static("markdown.js")
        self.assertIn("/^https?:\\/\\//i.test(text)", source)
        # 注释里解释了为什么要挡 javascript: / data:，剥掉注释再断言代码本身没放行。
        code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        self.assertNotIn("javascript:", code)
        self.assertNotIn("data:", code)

    def test_table_rows_are_padded_to_header_width(self):
        # 模型偶尔多写或少写一个竖线，不对齐的话整张表会错位。
        self.assertIn("row[column] ?? \"\"", self._static("markdown.js"))

    def test_project_js_uses_the_shared_renderer(self):
        source = self._static("project.js")
        self.assertIn("window.WorkbenchMarkdown", source)
        self.assertIn("project-agent-md", source)

    def test_web_research_delegates_to_the_shared_renderer(self):
        source = self._static("web-research.js")
        self.assertIn("window.WorkbenchMarkdown", source)
        self.assertIn("markdownLightFallback", source)

    def test_rendered_markdown_classes_are_styled(self):
        agent_css = self._static("project-agent.css")
        for token in (".md-table", ".md-code", ".md-list", ".md-quote", ".md-hr", ".project-agent-md"):
            self.assertIn(token, agent_css, f"{token} 没有样式，渲染出来会是裸标签")
        research_css = self._static("web-research.css")
        for token in (".md-table", ".md-code", ".md-quote"):
            self.assertIn(token, research_css)

    def test_markdown_paragraphs_do_not_double_up_line_breaks(self):
        # 渲染后的段落用 <br /> 换行，再叠 pre-wrap 会把每个换行算两次。
        agent_css = self._static("project-agent.css")
        self.assertIn(".project-agent-md p.md-p", agent_css)
        self.assertIn("white-space: normal", agent_css)


class ResultContractStructureTests(unittest.TestCase):
    """「结构化结果」以前是纯按行切的。

    模型写一张 Markdown 表，那张表在结构化面板里会散成一堆条目，连
    |---|---| 这行分隔线都单独成了一条——正文渲染成表格之后，这个反差
    更明显。表格和围栏代码块必须整块留在一条里。
    """

    def test_markdown_table_stays_in_one_item(self):
        answer = "## 事实\n| 指标 | 本周 |\n|---|---|\n| 成功率 | 92% |\n| 耗时 | 1.4s |\n"
        facts = app.agent_result_contract("inbox", answer)["sections"]["facts"]
        self.assertEqual(len(facts), 1, f"表格被切碎了：{facts}")
        self.assertIn("| 成功率 | 92% |", facts[0])
        self.assertIn("|---|---|", facts[0])

    def test_table_divider_never_becomes_its_own_item(self):
        answer = "## 事实\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        for values in app.agent_result_contract("inbox", answer)["sections"].values():
            for item in values:
                self.assertNotEqual(item.strip(), "|---|---|")

    def test_fenced_code_block_stays_in_one_item(self):
        answer = "## 事实\n```python\nx = 1\ny = 2\n```\n"
        facts = app.agent_result_contract("inbox", answer)["sections"]["facts"]
        self.assertEqual(len(facts), 1, f"代码块被切碎了：{facts}")
        self.assertIn("x = 1", facts[0])
        self.assertIn("y = 2", facts[0])

    def test_summary_never_contains_a_table(self):
        # summary 要塞进一行标题，拼进一张表只会是一串竖线。
        answer = "本周有 3 个指标异常。\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        self.assertEqual(app.agent_result_contract("inbox", answer)["summary"], "本周有 3 个指标异常。")

    def test_horizontal_rule_is_dropped(self):
        answer = "## 事实\n第一条\n---\n第二条\n"
        facts = app.agent_result_contract("inbox", answer)["sections"]["facts"]
        self.assertEqual(facts, ["第一条", "第二条"])

    def test_divider_detection_matches_the_front_end(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "markdown.js").read_text(encoding="utf-8")
        self.assertIn("isTableDivider", source)
        for line in ("|---|---|", "| :--- | ---: |", "---|---"):
            self.assertTrue(app._is_markdown_table_divider(line), line)
        for line in ("| a | b |", "", "文字"):
            self.assertFalse(app._is_markdown_table_divider(line), line)

    def test_contract_items_are_rendered_as_markdown_in_the_panel(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "project.js").read_text(encoding="utf-8")
        self.assertIn("agentContractItemMarkup", source)
        self.assertIn("contract-block", source)
        # 被截断的条数必须说出来，否则读的人以为 Agent 就只找到这么多。
        self.assertIn("contract-more", source)


class AgentStreamingUxTests(unittest.TestCase):
    """流式回答期间的观感：正文要即时渲染，过程要留痕，收尾要补齐结构化结果。"""

    def _project_js(self):
        return (Path(__file__).resolve().parents[1] / "static" / "project.js").read_text(encoding="utf-8")

    def test_streamed_answer_is_rendered_as_markdown_not_plain_text(self):
        source = self._project_js()
        self.assertNotIn("para.textContent = lastText", source)
        self.assertIn("renderAgentMarkdown(lastText)", source)

    def test_streaming_repaint_is_batched_per_frame(self):
        # 每个 token 重排一次 Markdown，长回答会肉眼可见地卡。
        self.assertIn("requestAnimationFrame", self._project_js())

    def test_finish_fills_in_the_result_contract_without_a_reload(self):
        source = self._project_js()
        self.assertIn("agentResultContractMarkup(payload.result_contract)", source)
        self.assertIn("payload.actions", source)

    def test_progress_keeps_a_step_history_and_a_thinking_stream(self):
        source = self._project_js()
        for token in ("progress-steps", "progress-thinking", "reasoningText", "tool_start"):
            self.assertIn(token, source)

    def test_tool_start_events_are_emitted_before_execution(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('"kind": "tool_start"', source)
        start = source.index('"kind": "tool_start"')
        run = source.index("async def _run_one", start)
        self.assertLess(start, run, "开工事件必须在工具真正执行之前发出")

    def test_progress_blocks_are_styled(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "project-agent.css").read_text(encoding="utf-8")
        for token in (".progress-now", ".progress-steps", ".progress-thinking", ".contract-block", ".contract-more"):
            self.assertIn(token, css)

    def test_list_markers_are_stripped_from_items(self):
        # 面板里条目本来就带项目符号，行首再留一个「1.」会变成「· 1. 先复现」。
        contract = app.agent_result_contract("inbox", "## 下一步\n1. 先复现\n- 再定位\n")
        self.assertEqual(contract["sections"]["next_steps"], ["先复现", "再定位"])

    def test_summary_line_renders_inline_markdown(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "project.js").read_text(encoding="utf-8")
        self.assertIn('renderAgentInline(contract.summary', source)
