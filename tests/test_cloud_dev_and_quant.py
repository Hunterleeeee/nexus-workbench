import json
import os
import sqlite3
import tempfile
import threading
import unittest
import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import app
import cloud_dev
import cloud_patch
from companion import workbench_companion


class CloudDevTests(unittest.TestCase):
    def test_explicit_command_parser_rejects_shell_injection(self):
        self.assertEqual(cloud_dev.parse_cloud_dev_command("云开发 workbench 运行测试")["action"], "test")
        self.assertEqual(cloud_dev.parse_cloud_dev_command("云开发 workbench 查看 状态")["action"], "status")
        self.assertEqual(cloud_dev.parse_cloud_dev_command("云开发 workbench 运行 测试")["action"], "test")
        rejected = cloud_dev.parse_cloud_dev_command("云开发 workbench 运行测试; touch /tmp/pwned")
        self.assertFalse(rejected["ok"])
        self.assertIn("shell", rejected["message"])

    def test_natural_language_generate_parsing(self):
        parsed = cloud_dev.parse_cloud_dev_command("云开发 帮我做一个理财记账网页")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["action"], "generate")
        self.assertEqual(parsed["kind"], "webpage")
        self.assertIn("理财记账", parsed["requirement"])
        doc = cloud_dev.parse_cloud_dev_command("云开发 写一份竞品分析报告")
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["action"], "generate")
        self.assertEqual(doc["kind"], "doc")
        script = cloud_dev.parse_cloud_dev_command("云开发 写一个批量重命名文件的脚本")
        self.assertTrue(script["ok"])
        self.assertEqual(script["action"], "generate")
        self.assertEqual(script["kind"], "script")
        unknown = cloud_dev.parse_cloud_dev_command("云开发 今天天气怎么样")
        self.assertFalse(unknown["ok"])
        self.assertIn("支持", unknown["message"])

    def test_missing_workspace_never_executes(self):
        parsed = cloud_dev.parse_cloud_dev_command("云开发 workbench 运行测试")
        with patch.dict(os.environ, {"WORKBENCH_CLOUD_WORKSPACES": ""}, clear=False), patch.object(cloud_dev.subprocess, "run") as run:
            result = cloud_dev.run_cloud_dev(parsed)
        self.assertEqual(result["status"], "not_configured")
        run.assert_not_called()

    def test_test_command_uses_fixed_argv_and_no_shell(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            parsed = cloud_dev.parse_cloud_dev_command("云开发 demo 运行测试")
            completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            with patch.dict(os.environ, {"WORKBENCH_CLOUD_WORKSPACES": f"demo={root}"}, clear=False), patch.object(cloud_dev.subprocess, "run", return_value=completed) as run:
                result = cloud_dev.run_cloud_dev(parsed)
        self.assertEqual(result["status"], "ok")
        call = run.call_args.kwargs
        self.assertFalse(call["shell"])
        self.assertEqual(call["cwd"], root.resolve())
        self.assertNotIn(";", run.call_args.args[0])

    def test_symlinked_workspace_is_rejected_before_resolution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            real_root = base / "real"
            link_root = base / "link"
            real_root.mkdir()
            link_root.symlink_to(real_root, target_is_directory=True)
            with patch.dict(os.environ, {"WORKBENCH_CLOUD_WORKSPACES": f"demo={link_root}"}, clear=False):
                self.assertEqual(cloud_dev.workspace_map(), {})

    def test_public_cloud_status_does_not_expose_workspace_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            with patch.dict(os.environ, {"WORKBENCH_CLOUD_WORKSPACES": f"demo={root}"}, clear=False):
                result = app._public_cloud_dev_result(cloud_dev.run_cloud_dev({"ok": True, "project": "demo", "action": "status"}))
        self.assertEqual(result["workspace"], root.name)
        self.assertNotIn(str(root), json.dumps(result))

    def test_build_is_explicitly_marked_for_approval(self):
        parsed = cloud_dev.parse_cloud_dev_command("云开发 workbench 构建")
        self.assertTrue(parsed["ok"])
        self.assertTrue(parsed["requires_approval"])
        self.assertEqual(cloud_dev.cloud_dev_policy()["automatic_actions"], ["status", "test", "generate"])

    def test_cloud_dev_output_redacts_json_credentials_and_accepts_non_string(self):
        output = cloud_dev.redact_output(
            '{"access_token":"access-secret","api_key": "api-secret", "password": "pw-secret"}'
        )
        self.assertNotIn("access-secret", output)
        self.assertNotIn("api-secret", output)
        self.assertNotIn("pw-secret", output)
        self.assertIn("[已隐藏]", output)
        self.assertEqual(cloud_dev.redact_output(None), "")

    def test_python_service_has_a_fixed_compile_build_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            self.assertEqual(cloud_dev._recipe(root, "build"), [cloud_dev.sys.executable, "-m", "compileall", "-q", "app.py"])

    def test_workbench_build_recipe_covers_fixed_service_modules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
            for name in ("app.py", "cloud_dev.py", "feishu.py", "agent_worker.py"):
                (root / name).write_text("print('ok')\n", encoding="utf-8")
            recipe = cloud_dev._recipe(root, "build")
        self.assertEqual(recipe[-4:], ["app.py", "cloud_dev.py", "feishu.py", "agent_worker.py"])

    def test_package_without_requested_script_is_not_advertised(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text('{"scripts":{"verify":"node verify.mjs"}}', encoding="utf-8")
            self.assertIsNone(cloud_dev._recipe(root, "test"))
            self.assertIsNone(cloud_dev._recipe(root, "build"))

    def test_cloud_dev_execution_exception_closes_run_and_work_item(self):
        async def exercise():
            with patch.object(cloud_dev, "run_cloud_dev", side_effect=RuntimeError("固定配方异常")):
                return await app.execute_cloud_dev_request(
                    {"ok": True, "project": "workbench", "action": "test", "raw": "云开发 workbench 运行测试"},
                    source="feishu",
                    chat_id="chat-exception",
                )

        event_types = []
        with tempfile.TemporaryDirectory() as temp_dir, \
            patch.object(app, "DATABASE_FILE", Path(temp_dir) / "workbench.db"), \
            patch.object(app, "_DB_SCHEMA_READY", False):
            result = asyncio.run(exercise())
            connection = app.db_connection()
            try:
                event_types = [row[0] for row in connection.execute("SELECT event_type FROM agent_run_events ORDER BY id").fetchall()]
            finally:
                connection.close()

        self.assertEqual(result["result"]["status"], "failed")
        self.assertIn("固定配方异常", result["result"]["message"])
        self.assertEqual(result["run"]["status"], "failed")
        self.assertEqual(result["work_item"]["status"], "failed")
        self.assertEqual(event_types, ["queued", "execution_started", "failed"])

    def test_invalid_timeout_configuration_fails_safe_to_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            parsed = cloud_dev.parse_cloud_dev_command("云开发 demo 运行测试")
            completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            with patch.dict(os.environ, {"WORKBENCH_CLOUD_WORKSPACES": f"demo={root}", "WORKBENCH_CLOUD_COMMAND_TIMEOUT_SECONDS": "not-a-number"}, clear=False), patch.object(cloud_dev.subprocess, "run", return_value=completed) as run:
                result = cloud_dev.run_cloud_dev(parsed)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_feishu_cloud_command_uses_fixed_route_and_build_approval(self):
        sent = AsyncMock()
        created_tasks = []

        def capture_task(coro, **kwargs):
            created_tasks.append(kwargs)
            coro.close()
            return SimpleNamespace()

        async def exercise():
            with patch.object(app.feishu_bot, "send_message", sent), patch.object(app.asyncio, "create_task", side_effect=capture_task):
                handled = await app.feishu_cloud_dev_command("云开发 workbench 查看状态", "chat-1")
            self.assertTrue(handled)
            self.assertEqual(len(created_tasks), 1)

            with patch.object(app.feishu_bot, "send_message", new=AsyncMock()) as build_send, patch.object(app, "create_cloud_dev_approval", return_value={"approval": {"id": "approval-1"}}), patch.object(app.asyncio, "create_task", side_effect=capture_task):
                handled_build = await app.feishu_cloud_dev_command("云开发 workbench 构建", "chat-1")
            self.assertTrue(handled_build)
            build_send.assert_awaited_once()

        asyncio.run(exercise())

    def test_feishu_status_exposes_only_readiness_booleans(self):
        async def exercise():
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get("/api/feishu")

        with patch.object(app.feishu_bot, "APP_ID", "app-id"), \
            patch.object(app.feishu_bot, "APP_SECRET", "app-secret"), \
            patch.object(app.feishu_bot, "VERIFY_TOKEN", "verify-token"), \
            patch.object(app.feishu_bot, "ENCRYPT_KEY", ""):
            response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["configured"], True)
        self.assertEqual(body["verify_token_set"], True)
        self.assertEqual(body["encrypt_key_set"], False)
        self.assertNotIn("app-secret", json.dumps(body))

    def test_feishu_environment_aliases_are_supported(self):
        with patch.dict(os.environ, {"WORKBENCH_FEISHU_APP_ID": "", "FEISHU_APP_ID": "legacy-app"}, clear=False):
            self.assertEqual(app.feishu_bot._env("WORKBENCH_FEISHU_APP_ID", "FEISHU_APP_ID"), "legacy-app")

    def test_feishu_callback_rejects_requests_without_verifier_configuration(self):
        async def exercise():
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post("/feishu/event", content=b"{}")

        with patch.object(app.feishu_bot, "ENCRYPT_KEY", ""), patch.object(app.feishu_bot, "VERIFY_TOKEN", ""):
            response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 503)
        self.assertIn("拒绝处理", response.json()["detail"])

    def test_feishu_callback_rejects_stale_signed_requests_before_processing(self):
        async def exercise():
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(
                    "/feishu/event",
                    content=b"{}",
                    headers={
                        "X-Lark-Request-Timestamp": str(int(time.time()) - 301),
                        "X-Lark-Request-Nonce": "fixture-nonce",
                        "X-Lark-Signature": "fixture-signature",
                    },
                )

        with patch.object(app.feishu_bot, "ENCRYPT_KEY", "fixture-key"), patch.object(app.feishu_bot, "verify_signature", return_value=True) as verify:
            response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 401)
        self.assertIn("timestamp", response.json()["detail"])
        verify.assert_not_called()

    def test_feishu_verify_token_supports_schema_header(self):
        with patch.object(app.feishu_bot, "VERIFY_TOKEN", "fixture-token"):
            self.assertTrue(app.feishu_bot.verify_event_token({"header": {"token": "fixture-token"}}))
            self.assertFalse(app.feishu_bot.verify_event_token({"header": {"token": "wrong"}}))
            self.assertFalse(app.feishu_bot.verify_signature("", "", "", b"{}"))

    def test_feishu_signature_timestamp_has_a_bounded_replay_window(self):
        now = 1_700_000_000
        self.assertTrue(app.feishu_bot.signature_timestamp_is_fresh(str(now), now=now))
        self.assertTrue(app.feishu_bot.signature_timestamp_is_fresh(str(now - 300), now=now))
        self.assertFalse(app.feishu_bot.signature_timestamp_is_fresh(str(now - 301), now=now))
        self.assertFalse(app.feishu_bot.signature_timestamp_is_fresh(str(now + 301), now=now))
        self.assertFalse(app.feishu_bot.signature_timestamp_is_fresh("not-a-timestamp", now=now))

    def test_feishu_event_receipt_claim_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            payload = {"header": {"event_id": "event-123", "event_type": "im.message.receive_v1"}}
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                self.assertTrue(app.claim_feishu_event(payload))
                self.assertFalse(app.claim_feishu_event(payload))

    def test_schema_v4_upgrade_preserves_data_and_creates_event_receipts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            connection = sqlite3.connect(database_file)
            try:
                connection.execute(
                    """CREATE TABLE inbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'note',
                        tags TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'inbox',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    "INSERT INTO inbox (content, kind, tags, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("旧数据库记录", "note", "legacy", "inbox", "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
                )
                connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                connection.executemany(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    [(version, "2026-08-01T00:00:00+00:00") for version in range(1, 5)],
                )
                connection.execute("PRAGMA user_version = 4")
                connection.commit()
            finally:
                connection.close()

            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                upgraded = app.db_connection()
                try:
                    record = upgraded.execute("SELECT content, source FROM inbox WHERE id = 1").fetchone()
                    receipt_table = upgraded.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'feishu_event_receipts'"
                    ).fetchone()
                    version = upgraded.execute("PRAGMA user_version").fetchone()[0]
                    migrations = upgraded.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
                finally:
                    upgraded.close()

        self.assertEqual(record["content"], "旧数据库记录")
        self.assertEqual(record["source"], "")
        self.assertIsNotNone(receipt_table)
        self.assertEqual(version, app.DB_SCHEMA_VERSION)
        self.assertEqual([row[0] for row in migrations], [1, 2, 3, 4, 5])

    def test_feishu_callback_does_not_repeat_quick_command_on_retry(self):
        async def exercise():
            payload = {
                "header": {"event_id": "event-retry-1", "event_type": "im.message.receive_v1", "token": "fixture-token"},
                "event": {
                    "message": {"message_type": "text", "content": json.dumps({"text": "/help"}), "chat_id": "chat-1", "message_id": "message-1"},
                    "sender": {"sender_id": {"open_id": "ou-test"}},
                },
            }
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                first = await client.post("/feishu/event", json=payload)
                second = await client.post("/feishu/event", json=payload)
            return first, second

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(app, "DATABASE_FILE", Path(temp_dir) / "workbench.db"), patch.object(app, "_DB_SCHEMA_READY", False), patch.object(app.feishu_bot, "ENCRYPT_KEY", ""), patch.object(app.feishu_bot, "VERIFY_TOKEN", "fixture-token"), patch.object(app.feishu_bot, "send_message", new=AsyncMock()) as send_message:
            first, second = asyncio.run(exercise())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {"code": 0, "msg": "duplicate"})
        self.assertEqual(send_message.await_count, 1)

    def test_feishu_callback_routes_cloud_dev_command_once(self):
        async def exercise():
            payload = {
                "header": {"event_id": "event-cloud-1", "event_type": "im.message.receive_v1", "token": "fixture-token"},
                "event": {
                    "message": {
                        "message_type": "text",
                        "content": json.dumps({"text": "云开发 workbench 查看状态"}),
                        "chat_id": "chat-cloud",
                        "message_id": "message-cloud-1",
                    },
                    "sender": {"sender_id": {"open_id": "ou-cloud"}},
                },
            }
            scheduled = []

            def capture_task(coro, **_kwargs):
                scheduled.append(coro)
                return SimpleNamespace()

            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                with patch.object(app.asyncio, "create_task", side_effect=capture_task):
                    first = await client.post("/feishu/event", json=payload)
                    self.assertEqual(len(scheduled), 1)
                    await scheduled.pop(0)
                    second = await client.post("/feishu/event", json=payload)
            return first, second

        async def fake_execute(parsed, **kwargs):
            return {"result": {"status": "ok", "message": "状态读取完成"}, "run": {}, "work_item": {}}

        with tempfile.TemporaryDirectory() as temp_dir, \
            patch.object(app, "DATABASE_FILE", Path(temp_dir) / "workbench.db"), \
            patch.object(app, "_DB_SCHEMA_READY", False), \
            patch.object(app.feishu_bot, "ENCRYPT_KEY", ""), \
            patch.object(app.feishu_bot, "VERIFY_TOKEN", "fixture-token"), \
            patch.object(app, "execute_cloud_dev_request", new=AsyncMock(side_effect=fake_execute)) as execute, \
            patch.object(app.feishu_bot, "send_message", new=AsyncMock()) as send_message:
            first, second = asyncio.run(exercise())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"code": 0, "msg": "cloud-dev"})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {"code": 0, "msg": "duplicate"})
        execute.assert_awaited_once()
        self.assertEqual(execute.await_args.args[0]["project"], "workbench")
        self.assertEqual(execute.await_args.args[0]["action"], "status")
        self.assertEqual(execute.await_args.kwargs["chat_id"], "chat-cloud")
        self.assertEqual(send_message.await_count, 2)

    def test_feishu_encrypted_callback_reports_missing_crypto_dependency(self):
        async def exercise():
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post("/feishu/event", content=b"{}", headers={"X-Lark-Signature": "fixture", "X-Lark-Request-Timestamp": str(int(time.time()))})

        with patch.object(app.feishu_bot, "ENCRYPT_KEY", "fixture-key"), patch.object(app.feishu_bot, "verify_signature", return_value=True), patch.object(app.feishu_bot, "decrypt_event", side_effect=ImportError("cryptography")):
            response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 503)
        self.assertIn("依赖未安装", response.json()["detail"])


class CloudPatchTests(unittest.TestCase):
    """云端自动改（patch）链路：意图解析 / 校验 / 应用 / 回滚。"""

    def test_parser_detects_patch_intent(self):
        parsed = cloud_dev.parse_cloud_dev_command("云开发 帮我改一下 AI 伴读的按钮颜色")
        self.assertEqual(parsed["action"], "patch")
        self.assertIn("AI 伴读", parsed["requirement"])
        parsed2 = cloud_dev.parse_cloud_dev_command("云开发 优化一下量化页的布局")
        self.assertEqual(parsed2["action"], "patch")
        # 生成意图仍应走 generate
        gen = cloud_dev.parse_cloud_dev_command("云开发 帮我做一个记账网页")
        self.assertEqual(gen["action"], "generate")

    def test_validate_edits_rejects_dangerous_and_unmatched(self):
        root = Path(__file__).resolve().parents[1]
        # 危险模式拒绝
        checked = cloud_patch.validate_edits(
            [{"file": "static/market.js", "old": "import os; os.system('rm -rf /')", "new": "x"}],
            root,
        )
        self.assertFalse(checked["ok"])
        self.assertTrue(any("危险" in e or "找不到" in e or "不存在" in e for e in checked["errors"]))
        # 不存在的文件拒绝
        checked = cloud_patch.validate_edits([{"file": "static/no-such-file.js", "old": "a", "new": "b"}], root)
        self.assertFalse(checked["ok"])
        # 白名单外文件拒绝
        checked = cloud_patch.validate_edits([{"file": "app.py", "old": "a", "new": "b"}], root)
        self.assertFalse(checked["ok"])
        self.assertTrue(any("不在可改范围" in e for e in checked["errors"]))

    def test_apply_and_rollback_roundtrip(self):
        root = Path(__file__).resolve().parents[1]
        target = root / "static" / "cloud-patch-probe.txt"
        target.write_text("line-a\nline-b\nline-c\n", encoding="utf-8")
        edits = [{"file": "static/cloud-patch-probe.txt", "old": "line-b", "new": "line-B", "why": "test"}]
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backup"
            result = cloud_patch.apply_edits(edits, root, backup_dir=backup_dir)
            self.assertTrue(result["ok"], result)
            self.assertIn("static/cloud-patch-probe.txt", result["applied"])
            self.assertEqual(target.read_text(encoding="utf-8"), "line-a\nline-B\nline-c\n")
            # 回滚
            rollback = cloud_patch.rollback(backup_dir, root)
            self.assertTrue(rollback["ok"], rollback)
            self.assertEqual(target.read_text(encoding="utf-8"), "line-a\nline-b\nline-c\n")
        target.unlink(missing_ok=True)

    def test_apply_fails_when_fragment_not_unique(self):
        root = Path(__file__).resolve().parents[1]
        target = root / "static" / "cloud-patch-probe.txt"
        target.write_text("dup\ndup\n", encoding="utf-8")
        edits = [{"file": "static/cloud-patch-probe.txt", "old": "dup", "new": "DUP", "why": "test"}]
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = cloud_patch.apply_edits(edits, root, backup_dir=Path(tmp) / "b")
            self.assertFalse(result["ok"])
            # 未唯一则不应写盘
            self.assertEqual(target.read_text(encoding="utf-8"), "dup\ndup\n")
        target.unlink(missing_ok=True)


class QuantResearchTests(unittest.TestCase):
    def _points(self, count=6):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [{"checked_at": (start + timedelta(days=index)).isoformat(), "price": 100 + index * 2, "volume": 1000 + index * 100, "source": "fixture"} for index in range(count)]

    def test_market_sampling_is_opt_in_bounded_and_keeps_history_when_stopped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            patches = [patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False)]
            for item in patches:
                item.start()
            try:
                with patch.object(app, "load_market_watchlist", return_value=[]):
                    with self.assertRaises(app.HTTPException) as context:
                        asyncio.run(app.update_market_sampling(app.MarketSamplingRequest(enabled=True, interval_seconds=1800)))
                self.assertEqual(context.exception.status_code, 400)

                with patch.object(app, "load_market_watchlist", return_value=[{"symbol": "sh600519"}]):
                    enabled = asyncio.run(app.update_market_sampling(app.MarketSamplingRequest(enabled=True, interval_seconds=1800)))
                    state = enabled["sampling"]
                    self.assertTrue(state["enabled"])
                    self.assertEqual(state["interval_seconds"], 1800)
                    self.assertEqual(state["watchlist_count"], 1)
                    self.assertEqual(state["history_count"], 0)
                    rule = app.market_sampling_rule()
                    self.assertEqual(rule["schedule"], "every:1800")
                    self.assertEqual(rule["config"]["source"], app.MARKET_SAMPLING_SOURCE)

                    stopped = asyncio.run(app.update_market_sampling(app.MarketSamplingRequest(enabled=False, interval_seconds=3600)))
                    self.assertFalse(stopped["sampling"]["enabled"])
                    self.assertEqual(stopped["sampling"]["interval_seconds"], 3600)
                    self.assertEqual(app.market_history_count(), 0)
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_market_sampling_rejects_arbitrary_interval(self):
        with patch.object(app, "load_market_watchlist", return_value=[{"symbol": "sh600519"}]):
            with self.assertRaises(app.HTTPException) as context:
                asyncio.run(app.update_market_sampling(app.MarketSamplingRequest(enabled=True, interval_seconds=600)))
        self.assertEqual(context.exception.status_code, 400)

    def test_factor_thresholds_require_three_and_four_samples(self):
        snapshot = {"checked_at": "2026-01-03T00:00:00+00:00", "source": "fixture", "quotes": [{"symbol": "600519", "price": 104, "previous_close": 102, "volume": 1200}]}
        history = [
            {"checked_at": "2026-01-01T00:00:00+00:00", "source": "fixture", "quotes": [{"symbol": "600519", "price": 100, "previous_close": 99, "volume": 1000}]},
            {"checked_at": "2026-01-02T00:00:00+00:00", "source": "fixture", "quotes": [{"symbol": "600519", "price": 102, "previous_close": 100, "volume": 1100}]},
        ]
        result = app.analyze_market_factors("600519", snapshot, history, app.market_quote_quality(snapshot["quotes"][0]))
        factors = {item["label"]: item for item in result["factors"]}
        self.assertEqual(factors["趋势"]["status"], "ok")
        self.assertEqual(factors["趋势"]["minimum_samples"], 3)
        self.assertEqual(factors["波动"]["status"], "missing")
        self.assertEqual(factors["成交活跃度"]["status"], "ok")

    def test_backtest_contains_benchmark_and_risk_statistics_without_recursion(self):
        points = self._points(12)
        with patch.object(app, "market_backtest_samples", return_value=(points, [])):
            result = app.market_backtest("600519", "momentum", 5, 10, 5)
        for key in ("benchmark_return_pct", "active_return_pct", "max_drawdown_pct", "realized_volatility_pct", "sample_sharpe_ratio", "sample_sortino_ratio", "exposure_pct", "trade_count", "win_rate", "profit_factor", "average_trade_return_pct"):
            self.assertIn(key, result)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sample_quality"]["sample_interval_label"], "约 1.0 天/点")

    def test_backtest_risk_ratios_are_sample_based_not_falsely_annualized(self):
        points = self._points(12)
        with patch.object(app, "market_backtest_samples", return_value=(points, [])):
            result = app.market_backtest("600519", "momentum", 5, 10, 5)
        self.assertIsInstance(result["sample_sharpe_ratio"], float)
        self.assertIn("样本期指标", result["disclaimer"])
        self.assertIn("未按年化", result["disclaimer"])

    def test_backtest_rejects_unknown_strategy_and_symbol_instead_of_silently_using_momentum(self):
        points = self._points(12)
        with patch.object(app, "market_backtest_samples", return_value=(points, [])):
            unknown_strategy = app.market_backtest("600519", "momentum_typo", 5, 10, 5)
            unknown_symbol = app.market_backtest("not-a-symbol", "momentum", 5, 10, 5)
        self.assertEqual(unknown_strategy["status"], "invalid")
        self.assertIn("strategy", unknown_strategy["message"])
        self.assertEqual(unknown_symbol["status"], "invalid")
        self.assertIn("股票代码", unknown_symbol["message"])

    def test_intraday_quality_uses_sample_interval_not_calendar_days(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        points = [{"checked_at": (start + timedelta(minutes=index * 5)).isoformat(), "price": 100 + index, "source": "fixture"} for index in range(12)]
        quality = app.market_backtest_quality(points, [], 5)
        self.assertTrue(quality["coverage_ready"])
        self.assertLess(quality["coverage_required_days"], 1)
        self.assertGreater(quality["coverage_required_intervals"], 1)

    def test_backtest_includes_current_snapshot_when_history_has_not_persisted_it(self):
        snapshot = {"checked_at": "2026-01-04T00:00:00+00:00", "source": "current", "quotes": [{"symbol": "600519", "price": 108, "previous_close": 107, "volume": 1400}]}
        with patch.object(app, "list_market_history", return_value=[]):
            points, rejected = app.market_backtest_samples("600519", snapshot=snapshot)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["checked_at"], snapshot["checked_at"])
        self.assertEqual(rejected, [])

    def test_walk_forward_uses_non_overlapping_test_folds_and_reports_oos_metrics(self):
        points = self._points(42)
        with patch.object(app, "market_backtest_samples", return_value=(points, [])):
            result = app.market_walk_forward("600519", "momentum", 5, 12, 4, 4, 3, 10, 5)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fold_count"], 3)
        self.assertIsNotNone(result["out_of_sample_return_pct"])
        self.assertIsNotNone(result["out_of_sample_benchmark_return_pct"])
        self.assertIn("每折只用训练段", result["policy"])
        folds = result["folds"]
        self.assertEqual([fold["test_from"] for fold in folds], [points[12]["checked_at"], points[16]["checked_at"], points[20]["checked_at"]])
        for index, fold in enumerate(folds):
            self.assertLess(fold["train_to"], fold["test_from"])
            self.assertIn(fold["selected_window"], {3, 5, 7})
            if index:
                self.assertGreaterEqual(fold["test_from"], folds[index - 1]["test_to"])

    def test_walk_forward_rejects_overlapping_test_folds(self):
        points = self._points(20)
        with patch.object(app, "market_backtest_samples", return_value=(points, [])):
            result = app.market_walk_forward("600519", "momentum", 5, 10, 4, 2, 3, 10, 5)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("step_size", result["message"])

    def test_walk_forward_api_returns_research_artifact(self):
        points = self._points(42)
        snapshot = {"checked_at": points[-1]["checked_at"], "source": "fixture", "quotes": []}

        async def exercise():
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(
                    "/api/market/backtest/walk-forward",
                    json={"symbol": "600519", "strategy": "momentum", "window": 5, "train_size": 12, "test_size": 4, "step_size": 4, "max_folds": 3, "fee_bps": 10, "slippage_bps": 5},
                )

        with patch.object(app, "load_market_snapshot", return_value=snapshot), \
            patch.object(app, "record_market_snapshot", return_value=None), \
            patch.object(app, "market_backtest_samples", return_value=(points, [])), \
            patch.object(app, "register_artifact_safely", return_value={"id": 7, "kind": "market_walk_forward"}):
            response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["walk_forward"]["fold_count"], 3)
        self.assertEqual(body["artifact"]["kind"], "market_walk_forward")

    def test_market_backtest_http_rejects_invalid_inputs_before_reading_data(self):
        async def exercise():
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                invalid_strategy = await client.post(
                    "/api/market/backtest",
                    json={"symbol": "600519", "strategy": "momentum_typo", "window": 5, "fee_bps": 10, "slippage_bps": 5},
                )
                invalid_symbol = await client.post(
                    "/api/market/backtest",
                    json={"symbol": "not-a-symbol", "strategy": "momentum", "window": 5, "fee_bps": 10, "slippage_bps": 5},
                )
                duplicate_strategies = await client.post(
                    "/api/market/strategies/compare",
                    json={"symbol": "600519", "strategies": ["momentum", "momentum"], "window": 5, "fee_bps": 10, "slippage_bps": 5},
                )
            return invalid_strategy, invalid_symbol, duplicate_strategies

        with patch.object(app, "load_market_snapshot") as load_snapshot:
            invalid_strategy, invalid_symbol, duplicate_strategies = asyncio.run(exercise())
        self.assertEqual(invalid_strategy.status_code, 400)
        self.assertIn("strategy", invalid_strategy.json()["detail"])
        self.assertEqual(invalid_symbol.status_code, 400)
        self.assertIn("股票代码", invalid_symbol.json()["detail"])
        self.assertEqual(duplicate_strategies.status_code, 400)
        self.assertIn("两个不同策略", duplicate_strategies.json()["detail"])
        load_snapshot.assert_not_called()

    def test_research_confidence_is_exposed_and_does_not_claim_missing_data(self):
        current = {"checked_at": "2026-01-03T00:00:00+00:00", "source": "fixture", "watchlist": [{"symbol": "600519"}], "quotes": [{"symbol": "600519", "price": 104, "previous_close": 102, "volume": 1200}]}
        history = [
            {"checked_at": f"2026-01-0{index}T00:00:00+00:00", "source": "fixture", "quotes": [{"symbol": "600519", "price": 100 + index, "previous_close": 99 + index, "volume": 1000 + index * 100}]} for index in range(1, 4)
        ]
        result = app.analyze_market_snapshot(current, history, now=datetime(2026, 1, 3, 0, 5, tzinfo=timezone.utc))
        confidence = result["research_confidence"]
        self.assertIn(confidence["label"], {"low", "medium", "high"})
        self.assertGreaterEqual(confidence["sample_count"], 3)
        self.assertIn("factor_ready_count", confidence)

    def test_empty_or_invalid_snapshot_is_not_promoted_to_history(self):
        snapshot = {"checked_at": "2026-01-04T00:00:00+00:00", "source": "fixture", "quotes": [{"symbol": "600519", "price": 104}]}
        with patch.object(app, "db_connection") as connection:
            self.assertIsNone(app.record_market_snapshot(snapshot))
        connection.assert_not_called()

    def test_same_timestamp_snapshots_merge_new_valid_quotes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            patches = [patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False)]
            for item in patches:
                item.start()
            try:
                timestamp = "2026-01-04T00:00:00Z"
                first = {"checked_at": timestamp, "source": "source-a", "watchlist": [{"symbol": "600519"}], "quotes": [{"symbol": "600519", "price": 104, "previous_close": 102, "volume": 1200}]}
                second = {"checked_at": "2026-01-04T00:00:00+00:00", "source": "source-b", "watchlist": [{"symbol": "000001"}], "quotes": [{"symbol": "000001", "price": 12, "previous_close": 11.8, "volume": 900}]}
                app.record_market_snapshot(first)
                app.record_market_snapshot(second)
                history = app.list_market_history()
            finally:
                for item in reversed(patches):
                    item.stop()
        self.assertEqual(len(history), 1)
        self.assertEqual({item["symbol"] for item in history[0]["quotes"]}, {"600519", "000001"})
        self.assertEqual(history[0]["source"], "mixed")

    def test_history_orders_snapshots_by_actual_time_across_offsets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            patches = [patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False)]
            for item in patches:
                item.start()
            try:
                # 01:00 +08:00 is 17:00Z on the previous day, so text sorting
                # would incorrectly place it after 00:00Z.
                earlier = {"checked_at": "2026-01-03T01:00:00+08:00", "source": "fixture", "quotes": [{"symbol": "600519", "price": 100, "previous_close": 99, "volume": 1000}]}
                later = {"checked_at": "2026-01-03T00:00:00Z", "source": "fixture", "quotes": [{"symbol": "600519", "price": 101, "previous_close": 100, "volume": 1100}]}
                app.record_market_snapshot(earlier)
                app.record_market_snapshot(later)
                history = app.list_market_history()
            finally:
                for item in reversed(patches):
                    item.stop()
        self.assertEqual([item["checked_at"] for item in history], [later["checked_at"], earlier["checked_at"]])

    def test_history_points_canonicalize_equivalent_timestamps_and_current_wins(self):
        history = [{"checked_at": "2026-01-02T00:00:00Z", "source": "old", "quotes": [{"symbol": "600519", "price": 102, "volume": 1000}]}]
        snapshot = {"checked_at": "2026-01-02T00:00:00+00:00", "source": "current", "quotes": [{"symbol": "600519", "price": 103, "volume": 1100}]}
        points = app._market_history_points("sh600519", snapshot, history)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["price"], 103)

    def test_snapshot_analysis_uses_latest_prior_snapshot_not_input_order(self):
        current = {"checked_at": "2026-01-04T00:00:00+00:00", "source": "fixture", "watchlist": [{"symbol": "600519"}], "quotes": [{"symbol": "600519", "price": 104, "previous_close": 103, "volume": 1300}]}
        history = [
            {"checked_at": "2026-01-01T00:00:00+00:00", "source": "fixture", "quotes": [{"symbol": "600519", "price": 100, "previous_close": 99, "volume": 1000}]},
            {"checked_at": "2026-01-03T00:00:00+00:00", "source": "fixture", "quotes": [{"symbol": "600519", "price": 103, "previous_close": 102, "volume": 1200}]},
        ]
        result = app.analyze_market_snapshot(current, history, now=datetime(2026, 1, 4, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(result["signals"][0]["prior_delta_pct"], 0.97)

    def test_legacy_report_rejects_snapshot_without_valid_quotes(self):
        with patch.object(app, "load_market_snapshot", return_value={"checked_at": "2026-01-04T00:00:00+00:00", "watchlist": [{"symbol": "600519"}], "quotes": []}), patch.object(app, "list_market_history", return_value=[]):
            with self.assertRaises(app.HTTPException) as context:
                app.create_market_report()
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("没有有效报价", str(context.exception.detail))

    def test_confidence_reports_mixed_history_sources(self):
        current = {"checked_at": "2026-01-04T00:00:00+00:00", "source": "new-source", "watchlist": [{"symbol": "600519"}], "quotes": [{"symbol": "600519", "price": 106, "previous_close": 105, "volume": 1300}]}
        history = [
            {"checked_at": f"2026-01-0{index}T00:00:00+00:00", "source": "old-source", "quotes": [{"symbol": "600519", "price": 100 + index * 2, "previous_close": 99 + index * 2, "volume": 1000 + index * 100}]}
            for index in range(1, 3)
        ]
        result = app.analyze_market_snapshot(current, history, now=datetime(2026, 1, 4, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(result["research_confidence"]["source_stability"], "mixed")

    def test_report_prompt_contains_factor_evidence_and_confidence(self):
        current = {"checked_at": "2026-01-04T00:00:00+00:00", "source": "fixture", "watchlist": [{"symbol": "600519"}], "quotes": [{"symbol": "600519", "price": 106, "previous_close": 105, "volume": 1300}]}
        history = [
            {"checked_at": f"2026-01-0{index}T00:00:00+00:00", "source": "fixture", "quotes": [{"symbol": "600519", "price": 100 + index * 2, "previous_close": 99 + index * 2, "volume": 1000 + index * 100}]}
            for index in range(1, 3)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            llm = AsyncMock(return_value="报告")
            with patch.object(app, "load_market_snapshot", return_value=current), patch.object(app, "list_market_history", return_value=history), patch.object(app, "llm_settings", return_value={"configured": True}), patch.object(app, "call_llm", llm), patch.object(app, "OUTPUTS_DIR", Path(temp_dir)), patch.object(app, "register_artifact_safely", return_value={}), patch.object(app, "create_notification_record"):
                asyncio.run(app.generate_market_report(app.MarketReportRequest(period="daily")))
        prompt = llm.await_args.args[0][1]["content"]
        self.assertIn("研究可信度", prompt)
        self.assertIn("趋势=", prompt)
        self.assertIn("样本不足", prompt)


class CompanionTests(unittest.TestCase):
    def test_http_boundary_allows_health_and_pna_but_rejects_invalid_post(self):
        server = workbench_companion.ThreadingHTTPServer(("127.0.0.1", 0), workbench_companion.CompanionHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        origin = "https://workbench.example.dev:8765"
        try:
            with httpx.Client(timeout=5, trust_env=False) as client:
                health = client.get(f"{base}/health", headers={"Origin": origin})
                preflight = client.options(
                    f"{base}/gemini/start",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Private-Network": "true",
                    },
                )
                invalid_status = client.get(f"{base}/gemini/status", headers={"Origin": "https://evil.example"})
                rejected = client.post(
                    f"{base}/gemini/start",
                    headers={"Origin": "https://evil.example", workbench_companion.COMPANION_HEADER: "1"},
                )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["ok"], True)
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers.get("access-control-allow-private-network"), "true")
        self.assertEqual(invalid_status.status_code, 403)
        self.assertEqual(rejected.status_code, 403)

    def test_helper_path_is_fixed_to_expected_filename(self):
        with patch.dict(os.environ, {"WORKBENCH_GEMINI_HELPER_PATH": "/tmp/not-a-helper.py"}, clear=False):
            with self.assertRaises(ValueError):
                workbench_companion.helper_path()

    def test_helper_path_rejects_symlink_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real = root / "real-helper.py"
            real.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            link = root / workbench_companion.HELPER_NAME
            link.symlink_to(real)
            with patch.dict(os.environ, {"WORKBENCH_GEMINI_HELPER_PATH": str(link)}, clear=False):
                with self.assertRaises(ValueError):
                    workbench_companion.helper_path()

    def test_helper_is_materialized_from_reviewed_laicai_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            helper = root / workbench_companion.HELPER_NAME
            source = root / "GeminiOAuthBridgeManager.swift"
            source.write_text(
                'private static let helperScript = #"""\n'
                '#!/usr/bin/env python3\n'
                'print("bridge")\n'
                '"""#\n',
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "WORKBENCH_GEMINI_HELPER_PATH": str(helper),
                    "WORKBENCH_LAICAI_GEMINI_SOURCE": str(source),
                },
                clear=False,
            ):
                materialized = workbench_companion.ensure_helper()
            self.assertEqual(materialized, helper)
            self.assertEqual(materialized.read_text(encoding="utf-8"), "#!/usr/bin/env python3\nprint(\"bridge\")\n")
            self.assertTrue(materialized.stat().st_mode & 0o111)

    def test_output_redaction_hides_common_credentials(self):
        value = workbench_companion.redact("Authorization: Bearer abc123 token=secret-value")
        self.assertIn("[已隐藏]", value)
        self.assertNotIn("abc123", value)
        self.assertNotIn("secret-value", value)

    def test_output_redaction_accepts_exception_values(self):
        value = workbench_companion.redact(RuntimeError("token=secret-value"))
        self.assertIn("[已隐藏]", value)
        self.assertNotIn("secret-value", value)

    def test_status_endpoint_uses_fixed_helper_without_shell(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            helper = Path(temp_dir) / workbench_companion.HELPER_NAME
            helper.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps({'status':'running'}))\n", encoding="utf-8")
            with patch.dict(os.environ, {"WORKBENCH_GEMINI_HELPER_PATH": str(helper)}, clear=False), patch.object(workbench_companion.sys, "platform", "darwin"), patch.object(workbench_companion.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='{"status":"running"}', stderr="")) as run:
                result = workbench_companion.gemini_status()
            self.assertEqual(result["status"], "running")
            self.assertFalse(run.call_args.kwargs["shell"])
            self.assertNotIn("/etc/hosts", json.dumps(result))

    def test_start_and_stop_use_admin_helper_without_shell(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            helper = Path(temp_dir) / workbench_companion.HELPER_NAME
            helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            with patch.dict(os.environ, {"WORKBENCH_GEMINI_HELPER_PATH": str(helper)}, clear=False), \
                patch.object(workbench_companion.sys, "platform", "darwin"), \
                patch.object(workbench_companion.subprocess, "run", return_value=completed) as run:
                started = workbench_companion.gemini_start()
                stopped = workbench_companion.gemini_stop()

        self.assertTrue(started["ok"])
        self.assertTrue(stopped["ok"])
        self.assertEqual(run.call_count, 2)
        start_argv = run.call_args_list[0].args[0]
        stop_argv = run.call_args_list[1].args[0]
        self.assertEqual(start_argv[0], "/usr/bin/osascript")
        self.assertIn("--repair", start_argv[-1])
        self.assertIn("--daemon", start_argv[-1])
        self.assertIn("with administrator privileges", start_argv[-1])
        self.assertIn("--stop", stop_argv[-1])
        self.assertFalse(run.call_args_list[0].kwargs["shell"])
        self.assertFalse(run.call_args_list[1].kwargs["shell"])

    def test_status_does_not_expose_local_process_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            helper = Path(temp_dir) / workbench_companion.HELPER_NAME
            helper.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps({'status':'running','pids':[123],'processes':[{'command':'/secret/path'}],'launchdPlist':'/Library/LaunchDaemons/private.plist','hostsPresent':True}))\n", encoding="utf-8")
            with patch.dict(os.environ, {"WORKBENCH_GEMINI_HELPER_PATH": str(helper)}, clear=False), patch.object(workbench_companion.sys, "platform", "darwin"), patch.object(workbench_companion.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='{"status":"running","pids":[123],"processes":[{"command":"/secret/path"}],"launchdPlist":"/Library/LaunchDaemons/private.plist","hostsPresent":true}', stderr="")):
                result = workbench_companion.gemini_status()
        encoded = json.dumps(result)
        self.assertNotIn("pids", encoded)
        self.assertNotIn("/secret/path", encoded)
        self.assertNotIn("launchdPlist", encoded)
        self.assertTrue(result["hostsPresent"])


if __name__ == "__main__":
    unittest.main()
