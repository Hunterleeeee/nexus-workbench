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
        temp_dir = tempfile.TemporaryDirectory()
        vault = Path(temp_dir.name)
        (vault / "a.md").write_text("a", encoding="utf-8")
        with temp_dir, patch.object(app, "KNOWLEDGE_DIR", vault), patch.dict(
            app._knowledge_files_cache, {"signature": None, "files": []}
        ):
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
