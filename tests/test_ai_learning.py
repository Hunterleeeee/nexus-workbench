import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

import app
import sync_worker


class AILearningTests(unittest.TestCase):
    def test_daily_schedule_runs_once_after_local_target_time(self):
        local = timezone(timedelta(hours=8))
        before = datetime(2026, 8, 11, 8, 29, tzinfo=local)
        after = datetime(2026, 8, 11, 8, 31, tzinfo=local)
        rule = {"schedule": "daily:08:30", "last_run_at": ""}
        self.assertFalse(sync_worker.due(rule, before))
        self.assertTrue(sync_worker.due(rule, after))

        rule["last_run_at"] = datetime(2026, 8, 11, 0, 30, tzinfo=timezone.utc).isoformat()
        self.assertFalse(sync_worker.due(rule, after))
        rule["last_run_at"] = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc).isoformat()
        self.assertTrue(sync_worker.due(rule, after))

    def test_learning_loop_is_persistent_idempotent_and_auditable(self):
        async def request(root: Path):
            database_file = root / "workbench.db"
            knowledge_dir = root / "knowledge"
            knowledge_dir.mkdir()
            with (
                patch.object(app, "DATABASE_FILE", database_file),
                patch.object(app, "KNOWLEDGE_DIR", knowledge_dir),
                patch.object(app, "SETTINGS_FILE", root / "llm-settings.json"),
                patch.object(app, "_DB_SCHEMA_READY", False),
                patch.object(app, "llm_settings", return_value={"configured": False}),
            ):
                transport = httpx.ASGITransport(app=app.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    first = await client.get("/api/ai-learning/dashboard")
                    second = await client.get("/api/ai-learning/dashboard")
                    lesson = first.json()["today"]
                    profile = await client.put("/api/ai-learning/profile", json={
                        "current_role": "产品经理",
                        "target_role": "AI 产品经理",
                        "experience": "practical",
                        "focus": "product",
                        "goal": "做出一个可展示的 AI 产品",
                        "daily_minutes": 25,
                        "push_time": "09:10",
                        "daily_push_enabled": True,
                    })
                    progress = await client.patch(
                        f"/api/ai-learning/lessons/{lesson['id']}/progress",
                        json={"practice_output": "完成了一份需求评审提示词", "reflection": "先用真实需求测试", "confidence": 4},
                    )
                    restored = await client.get("/api/ai-learning/dashboard")
                    repeated_progress = await client.patch(
                        f"/api/ai-learning/lessons/{lesson['id']}/progress",
                        json={"practice_output": "完成了一份需求评审提示词", "reflection": "先用真实需求测试", "confidence": 4},
                    )
                    early_note = await client.post(f"/api/ai-learning/lessons/{lesson['id']}/note")
                    complete = await client.post(
                        f"/api/ai-learning/lessons/{lesson['id']}/complete",
                        json={"quiz_answer": lesson["content"]["quiz"]["correct_index"], "practice_output": "完成了一份需求评审提示词", "reflection": "把方法用到需求评审", "confidence": 4},
                    )
                    completed_progress = await client.patch(
                        f"/api/ai-learning/lessons/{lesson['id']}/progress",
                        json={"practice_output": "不应覆盖", "reflection": "不应覆盖", "confidence": 1},
                    )
                    note = await client.post(f"/api/ai-learning/lessons/{lesson['id']}/note")
                    duplicate_note = await client.post(f"/api/ai-learning/lessons/{lesson['id']}/note")
                    final = await client.get("/api/ai-learning/dashboard")
                    return first, second, profile, progress, restored, repeated_progress, early_note, complete, completed_progress, note, duplicate_note, final, knowledge_dir

        with tempfile.TemporaryDirectory() as temp_dir:
            first, second, profile, progress, restored, repeated_progress, early_note, complete, completed_progress, note, duplicate_note, final, knowledge_dir = asyncio.run(request(Path(temp_dir)))
            files = list(knowledge_dir.glob("*.md"))
            note_text = files[0].read_text(encoding="utf-8") if files else ""

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["today"]["id"], second.json()["today"]["id"])
        self.assertEqual(first.json()["automation"]["schedule"], "daily:08:30")
        self.assertEqual(profile.json()["automation"]["schedule"], "daily:09:10")
        self.assertEqual(progress.json()["lesson"]["status"], "in_progress")
        self.assertEqual(restored.json()["today"]["practice_output"], "完成了一份需求评审提示词")
        self.assertEqual(restored.json()["today"]["reflection"], "先用真实需求测试")
        self.assertTrue(repeated_progress.json()["saved"])
        self.assertEqual(early_note.status_code, 409)
        self.assertTrue(complete.json()["quiz"]["correct"])
        self.assertEqual(complete.json()["stats"]["streak"], 1)
        self.assertFalse(completed_progress.json()["saved"])
        self.assertEqual(completed_progress.json()["lesson"]["practice_output"], "完成了一份需求评审提示词")
        self.assertTrue(note.json()["created"])
        self.assertFalse(duplicate_note.json()["created"])
        self.assertEqual(len(files), 1)
        self.assertIn("## 我的练习成果", note_text)
        self.assertIn("完成了一份需求评审提示词", note_text)
        self.assertEqual(final.json()["stats"]["completed"], 1)
        self.assertEqual(final.json()["stats"]["notes"], 1)

    def test_optional_overall_output_allows_lesson_completion(self):
        """界面写“选填”时，API 也必须接受空产出；自测本身就是有效完成证据。"""
        async def request(root: Path):
            with (
                patch.object(app, "DATA_DIR", root),
                patch.object(app, "DATABASE_FILE", root / "workbench.db"),
                patch.object(app, "_DB_SCHEMA_READY", False),
                patch.object(app, "llm_settings", return_value={"configured": False}),
            ):
                transport = httpx.ASGITransport(app=app.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    lesson = (await client.get("/api/ai-learning/dashboard")).json()["today"]
                    response = await client.post(
                        f"/api/ai-learning/lessons/{lesson['id']}/complete",
                        json={"quiz_answer": lesson["content"]["quiz"]["correct_index"], "practice_output": "", "reflection": "", "confidence": 3},
                    )
                    return response

        with tempfile.TemporaryDirectory() as temp_dir:
            response = asyncio.run(request(Path(temp_dir)))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["lesson"]["completed"])

    def test_existing_learning_table_is_migrated_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            connection = sqlite3.connect(database_file)
            connection.execute(
                """CREATE TABLE ai_learning_lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_date TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready'
                )"""
            )
            connection.execute("INSERT INTO ai_learning_lessons (lesson_date, title) VALUES ('2026-08-10', '旧课程')")
            connection.execute("PRAGMA user_version = 9")
            connection.commit()
            connection.close()
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                migrated = app.db_connection()
                columns = {row[1] for row in migrated.execute("PRAGMA table_info(ai_learning_lessons)").fetchall()}
                row = migrated.execute("SELECT title, practice_output FROM ai_learning_lessons WHERE lesson_date = '2026-08-10'").fetchone()
                version = migrated.execute("PRAGMA user_version").fetchone()[0]
                migrated.close()
        self.assertIn("practice_output", columns)
        self.assertEqual(row["title"], "旧课程")
        self.assertEqual(row["practice_output"], "")
        self.assertEqual(version, 10)

    def test_project_page_agent_and_accessible_learning_controls_are_registered(self):
        async def request(database_file: Path):
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                transport = httpx.ASGITransport(app=app.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.get("/projects/ai-learning"), await client.get("/api/projects")

        with tempfile.TemporaryDirectory() as temp_dir:
            page, projects = asyncio.run(request(Path(temp_dir) / "workbench.db"))
        self.assertEqual(page.status_code, 200)
        self.assertIn("AI 转型学习", page.text)
        project = next(item for item in projects.json()["projects"] if item["id"] == "ai-learning")
        self.assertEqual(project["agent_name"], "AI 转型学习教练")
        self.assertEqual(project["primary_action"]["href"], "/projects/ai-learning")
        self.assertIn("ai-learning", app.AGENT_REGISTRY["workbench"]["children"])
        self.assertIn("global_llm", app.AGENT_REGISTRY["ai-learning"]["tools"])

        root = Path(__file__).resolve().parents[1]
        html = (root / "static" / "ai-learning.html").read_text(encoding="utf-8")
        script = (root / "static" / "ai-learning.js").read_text(encoding="utf-8")
        stylesheet = (root / "static" / "ai-learning.css").read_text(encoding="utf-8")
        self.assertIn('aria-labelledby="lesson-title"', html)
        self.assertIn('role="switch"', html)
        self.assertIn('id="lesson-complete-form"', script)
        self.assertIn('id="practice-output"', script)
        self.assertIn("/progress", script)
        self.assertIn("/api/ai-learning/dashboard", script)
        self.assertIn("@media (max-width: 380px)", stylesheet)
        self.assertIn("prefers-reduced-motion", stylesheet)


if __name__ == "__main__":
    unittest.main()
