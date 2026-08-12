import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import app


class WorkbenchMemoryTests(unittest.TestCase):
    def memory_database(self):
        temp_dir = tempfile.TemporaryDirectory()
        database_file = Path(temp_dir.name) / "workbench.db"
        return temp_dir, database_file

    def test_schema_creates_memory_tables_at_version_seven(self):
        temp_dir, database_file = self.memory_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            connection = app.db_connection()
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            finally:
                connection.close()
        self.assertIn("memory_items", tables)
        self.assertIn("memory_events", tables)
        self.assertGreaterEqual(version, 7)

    def test_explicit_memory_is_confirmed_and_injected(self):
        temp_dir, database_file = self.memory_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            learned = app.learn_memories_from_message(
                "记住：回答默认用中文，先说结论",
                project_id="knowledge",
                source_type="agent_message",
                source_id="42",
            )
            context = app.memory_context_for_llm("market", "请给我今天的结论")
        self.assertEqual(len(learned), 1)
        self.assertEqual(learned[0]["status"], "confirmed")
        self.assertEqual(learned[0]["scope"], "global")
        self.assertTrue(learned[0]["pinned"])
        self.assertIn("回答默认用中文", context["text"])
        self.assertEqual(context["refs"][0]["id"], learned[0]["id"])

    def test_retrieval_uses_a_small_relevant_prompt_window(self):
        temp_dir, database_file = self.memory_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            for index in range(3):
                app.create_memory_item(content=f"置顶核心偏好 {index}：回答保持简洁", status="confirmed", pinned=True)
            for index in range(6):
                app.create_memory_item(content=f"量化研究策略 {index}：优先展示风险和证据", status="confirmed", project_id="market", scope="project")
            for index in range(4):
                app.create_memory_item(content=f"文档排版习惯 {index}：使用宽松行距", status="confirmed")
            context = app.memory_context_for_llm("market", "分析量化研究策略")
            core = app.memory_context_for_llm("market", "完全无关的话题", core_only=True)
        self.assertLessEqual(len(context["items"]), app.MAX_MEMORY_CONTEXT_ITEMS)
        self.assertLessEqual(context["stats"]["pinned"], app.MAX_MEMORY_PINNED_ITEMS)
        self.assertLessEqual(context["stats"]["matched"], app.MAX_MEMORY_MATCHED_ITEMS)
        self.assertLessEqual(len(context["text"]), app.MAX_MEMORY_CONTEXT_CHARS)
        self.assertNotIn("文档排版习惯", context["text"])
        self.assertTrue(core["items"])
        self.assertTrue(all(item["pinned"] for item in core["items"]))

    def test_inferred_preference_waits_for_confirmation(self):
        temp_dir, database_file = self.memory_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            learned = app.learn_memories_from_message(
                "我偏好简洁的表格",
                project_id="doc-factory",
                source_type="agent_message",
                source_id="7",
            )
            before = app.memory_context_for_llm("doc-factory", "整理成表格")
            confirmed = app.set_memory_status(learned[0]["id"], "confirmed")
            after = app.memory_context_for_llm("doc-factory", "整理成表格")
        self.assertEqual(learned[0]["status"], "candidate")
        self.assertFalse(before["refs"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertIn(learned[0]["id"], {item["id"] for item in after["refs"]})

    def test_secret_like_content_is_never_saved(self):
        temp_dir, database_file = self.memory_database()
        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
            learned = app.learn_memories_from_message(
                "记住：API Key sk-abcdefghijklmnop",
                project_id="workbench",
                source_type="agent_message",
                source_id="9",
            )
            with self.assertRaises(ValueError):
                app.create_memory_item(content="密码是 12345678", status="confirmed")
            summary = app.memory_summary()
        self.assertEqual(learned, [])
        self.assertEqual(summary["confirmed"], 0)

    def test_memory_api_supports_create_edit_confirm_reject_and_delete(self):
        temp_dir, database_file = self.memory_database()

        async def request_flow():
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                created = await client.post("/api/memories", json={"content": "输出默认使用中文", "status": "candidate", "confidence": 0.7})
                memory_id = created.json()["item"]["id"]
                confirmed = await client.post(f"/api/memories/{memory_id}/confirm")
                updated = await client.patch(f"/api/memories/{memory_id}", json={"content": "输出默认使用简洁中文", "pinned": True})
                listed = await client.get("/api/memories?status=confirmed")
                deleted = await client.delete(f"/api/memories/{memory_id}")
                missing = await client.patch(f"/api/memories/{memory_id}", json={"content": "不存在"})
                return created, confirmed, updated, listed, deleted, missing

        with temp_dir, patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False), patch.object(app, "load_cid_preferences", return_value={}):
            created, confirmed, updated, listed, deleted, missing = asyncio.run(request_flow())
        self.assertEqual(created.status_code, 200)
        self.assertEqual(confirmed.json()["item"]["status"], "confirmed")
        self.assertTrue(updated.json()["item"]["pinned"])
        self.assertEqual(updated.json()["item"]["content"], "输出默认使用简洁中文")
        self.assertEqual(len(listed.json()["items"]), 1)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(missing.status_code, 404)

    def test_workbuddy_preview_only_reads_user_preferences(self):
        """这条用例原来直接读开发机上真实的 .workbuddy/memory/MEMORY.md，
        断言里还写死了「中文交流」这几个字。后果有两个：文件不在的机器上
        （容器、CI、别人的检出）它必然失败，而只要用户改一下自己的备忘录，
        发布前的测试闸门就会莫名其妙卡住——测的是别人的文件内容，不是代码。

        改成自建 fixture，保留它真正要守的东西：预览只取「用户偏好」这一节，
        并且带凭据、IP 这类内容的行不进预览。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / ".workbuddy" / "memory"
            memory.mkdir(parents=True)
            (memory / "MEMORY.md").write_text(
                "\n".join([
                    "# 备忘",
                    "## 用户偏好",
                    "- 默认用中文交流，先说结论",
                    "- 服务器 124.223.1.2 的 root 密码是 hunter2",
                    "- 回答里不要用「赋能」这类词",
                    "## 项目笔记",
                    "- 这一节不该出现在预览里",
                ]),
                encoding="utf-8",
            )
            with patch.object(app, "ROOT", root):
                preview = app.workbuddy_memory_preview()
        combined = "\n".join(item["content"] for item in preview)
        self.assertIn("中文交流", combined)
        self.assertIn("赋能", combined)
        self.assertNotIn("这一节不该出现", combined, "只该读「用户偏好」这一节")
        self.assertNotIn("hunter2", combined, "带凭据的行不能进预览")
        self.assertNotIn("124.223", combined)

    def test_result_contract_and_frontend_expose_memory_trace(self):
        contract = app.agent_result_contract(
            "knowledge",
            "结论：继续整理",
            memory_refs=[{"id": "mem-1", "content": "使用中文", "scope": "global", "kind": "preference", "confidence": 1}],
            memory_updates=[{"id": "mem-2", "content": "偏好表格", "status": "candidate"}],
            memory_context_stats={"items": 1, "chars": 86, "pinned": 1, "matched": 0, "calls": 1, "max_items": 5, "max_chars": 1200},
        )
        root = Path(__file__).resolve().parents[1]
        home = (root / "static" / "workbench.js").read_text(encoding="utf-8")
        stylesheet = (root / "static" / "workbench.css").read_text(encoding="utf-8")
        project = (root / "static" / "project.js").read_text(encoding="utf-8")
        self.assertEqual(contract["memory_refs"][0]["id"], "mem-1")
        self.assertEqual(contract["memory_updates"][0]["status"], "candidate")
        self.assertEqual(contract["memory_context"]["chars"], 86)
        self.assertIn("setupMemoryCenter", home)
        self.assertIn("memory-mobile-open", home)
        self.assertIn(".memory-top-button", stylesheet)
        self.assertIn("/api/memories", home)
        self.assertIn("session_id: sessionId", home)
        self.assertIn("使用了 ${contract.memory_refs.length} 条已确认记忆", home)
        self.assertIn("记忆上下文 ${Number(memoryContext.chars)} 字", home)
        self.assertIn("使用了 ${contract.memory_refs.length} 条已确认记忆", project)
        self.assertIn("记忆上下文 ${Number(memoryContext.chars)} 字", project)


if __name__ == "__main__":
    unittest.main()
