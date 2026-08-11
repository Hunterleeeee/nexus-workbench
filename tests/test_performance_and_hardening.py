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
import re
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
