import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

import app


class AgentOptimizationTests(unittest.TestCase):

    def test_stale_queued_automation_runs_are_recovered_without_touching_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False), patch.object(app, "WORKBENCH_AUTOMATION_STALE_SECONDS", 300):
                connection = app.db_connection()
                try:
                    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
                    fresh = app.now_iso()
                    connection.execute(
                        "INSERT INTO automation_rules (name, kind, project_id, schedule, enabled, config_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("热点摘要", "aihot_refresh", "aihot", "every:86400", 1, "{}", "ready", old, old),
                    )
                    rule_id = connection.execute("SELECT id FROM automation_rules ORDER BY id DESC LIMIT 1").fetchone()[0]
                    connection.executemany(
                        "INSERT INTO automation_runs (id, rule_id, status, trigger, result_json, error, started_at, finished_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            ("stale-queued", rule_id, "queued", "schedule", "{}", "", "", "", old),
                            ("fresh-queued", rule_id, "queued", "manual", "{}", "", "", "", fresh),
                            ("active-running", rule_id, "running", "manual", "{}", "", fresh, "", old),
                        ],
                    )
                    connection.commit()
                finally:
                    connection.close()

                recovery = app.recover_stale_automation_runs()
                self.assertEqual(recovery["recovered_count"], 1)
                self.assertEqual(recovery["recovered_runs"][0]["id"], "stale-queued")
                connection = app.db_connection()
                try:
                    statuses = {row["id"]: row["status"] for row in connection.execute("SELECT id, status FROM automation_runs")}
                    errors = {row["id"]: row["error"] for row in connection.execute("SELECT id, error FROM automation_runs")}
                finally:
                    connection.close()
                self.assertEqual(statuses["stale-queued"], "failed")
                self.assertIn("服务重启后未被领取", errors["stale-queued"])
                self.assertEqual(statuses["fresh-queued"], "queued")
                self.assertEqual(statuses["active-running"], "running")

    def test_agent_quality_distinguishes_historical_failure_from_no_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                connection = app.db_connection()
                try:
                    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
                    connection.execute(
                        "INSERT INTO agent_runs (id, project_id, kind, status, error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("old-failed", "knowledge", "chat", "failed", "历史失败原因", old, old),
                    )
                    connection.commit()
                finally:
                    connection.close()
                historical = app.agent_quality_metrics("knowledge", hours=24)
                configured = app.agent_quality_metrics("doc-factory", hours=24)
        self.assertEqual(historical["state"], "historical_failed")
        self.assertEqual(historical["state_label"], "近期无运行 · 历史有失败")
        self.assertEqual(historical["historical_failed"], 1)
        self.assertEqual(historical["last_error"], "历史失败原因")
        self.assertEqual(configured["state"], "configured")
        self.assertEqual(configured["historical_total"], 0)

    def test_worker_status_marks_old_failure_as_recovered_and_keeps_success_time(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE worker_leases (worker_id TEXT PRIMARY KEY, instance_id TEXT, status TEXT, lease_until TEXT, last_heartbeat TEXT, metadata_json TEXT);
            CREATE TABLE agent_runs (id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, status TEXT, error TEXT, updated_at TEXT, finished_at TEXT);
            CREATE TABLE automation_rules (id INTEGER PRIMARY KEY, kind TEXT);
            CREATE TABLE automation_runs (id TEXT PRIMARY KEY, rule_id INTEGER, status TEXT, error TEXT, finished_at TEXT);
            """
        )
        old_error = "x" * 500
        connection.execute(
            "INSERT INTO agent_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("crawl-failed", "crawl4ai", "crawl", "failed", old_error, "2026-08-09T01:00:00+00:00", "2026-08-09T01:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO agent_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("crawl-ok", "crawl4ai", "crawl", "completed", "", "2026-08-09T02:00:00+00:00", "2026-08-09T02:00:00+00:00"),
        )
        connection.commit()
        with patch.object(app, "db_connection", return_value=connection):
            workers = app.worker_status_payload()

        crawl = next(item for item in workers if item["id"] == "crawl-worker")
        self.assertEqual(crawl["last_error_state"], "recovered")
        self.assertEqual(crawl["last_success_at"], "2026-08-09T02:00:00+00:00")
        self.assertLess(len(crawl["last_error"]), len(old_error))
        self.assertLessEqual(len(crawl["last_error"]), 320)

    def test_evidence_edge_summary_separates_synthetic_business_and_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                connection = app.db_connection()
                try:
                    connection.executemany(
                        "INSERT INTO evidence_checks(edge_key, scenario, status, run_id, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            ("inbox->knowledge", "success", "verified", "synthetic-run", json.dumps({"verification_kind": "synthetic_acceptance", "synthetic": True}), "2026-08-09T01:00:00+00:00"),
                            ("inbox->knowledge", "failure", "verified", "business-run", json.dumps({"verification_kind": "business_execution", "work_item_id": 12, "verified_at": "2026-08-09T02:00:00+00:00"}), "2026-08-09T02:00:00+00:00"),
                            ("inbox->knowledge", "retry", "pending", "", "{}", "2026-08-09T03:00:00+00:00"),
                        ],
                    )
                    connection.commit()
                finally:
                    connection.close()
                summary = app.evidence_edge_summary("inbox->knowledge")
        self.assertEqual(summary["verified"], 2)
        self.assertEqual(summary["synthetic_verified"], 1)
        self.assertEqual(summary["business_verified"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["business_status"], "verified")
        self.assertEqual(summary["latest_business_execution"]["work_item_id"], 12)

    def test_evidence_matrix_persists_legacy_classification_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                connection = app.db_connection()
                try:
                    connection.execute(
                        "INSERT INTO evidence_checks(edge_key, scenario, status, run_id, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        ("inbox->knowledge", "success", "verified", "old-run", json.dumps({"verified_from": "SQLite object chain"}), app.now_iso()),
                    )
                    connection.commit()
                finally:
                    connection.close()

                first = app.run_evidence_matrix()
                connection = app.db_connection()
                try:
                    row = connection.execute(
                        "SELECT detail_json FROM evidence_checks WHERE edge_key = ? AND scenario = ?",
                        ("inbox->knowledge", "success"),
                    ).fetchone()
                    first_detail = app.platform_decode_json(row["detail_json"], {})
                finally:
                    connection.close()
                second = app.run_evidence_matrix()
                connection = app.db_connection()
                try:
                    row = connection.execute(
                        "SELECT detail_json FROM evidence_checks WHERE edge_key = ? AND scenario = ?",
                        ("inbox->knowledge", "success"),
                    ).fetchone()
                    second_detail = app.platform_decode_json(row["detail_json"], {})
                finally:
                    connection.close()

        self.assertEqual(first["summary"]["legacy_unclassified_verified"], 1)
        self.assertEqual(second["summary"]["legacy_unclassified_verified"], 1)
        self.assertEqual(first_detail["verification_kind"], "legacy_unclassified")
        self.assertEqual(first_detail["classification_source"], "legacy_record_reclassification")
        self.assertEqual(first_detail["reclassified_at"], second_detail["reclassified_at"])

    def test_crawl_observability_reports_window_quality_and_hash_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False), patch.object(
                app,
                "worker_status_payload",
                return_value=[{"id": "crawl-worker", "status": "idle", "last_heartbeat": "2026-08-09T03:00:00+00:00", "last_success_at": "2026-08-09T02:00:00+00:00"}],
            ):
                now = datetime.now(timezone.utc)
                success_started = (now - timedelta(hours=1)).isoformat()
                success_finished = (now - timedelta(minutes=59)).isoformat()
                failed_at = (now - timedelta(minutes=30)).isoformat()
                connection = app.db_connection()
                try:
                    connection.execute(
                        """INSERT INTO agent_runs
                        (id, project_id, session_id, parent_run_id, kind, status, attempt, max_attempts, title, request_json, result_json, error, created_at, started_at, finished_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "crawl-success",
                            "crawl4ai",
                            "",
                            "",
                            "crawl",
                            "completed",
                            1,
                            1,
                            "稳定性样本",
                            "{}",
                            json.dumps({"documents": [{"source_quality": {"quality_status": "fresh"}}], "change_detection": [{"state": "changed"}], "elapsed_ms": 120}),
                            "",
                            success_started,
                            success_started,
                            success_finished,
                            success_finished,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO agent_runs
                        (id, project_id, session_id, parent_run_id, kind, status, attempt, max_attempts, title, request_json, result_json, error, created_at, started_at, finished_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "crawl-failed",
                            "crawl4ai",
                            "",
                            "",
                            "crawl",
                            "failed",
                            1,
                            2,
                            "失败样本",
                            "{}",
                            "{}",
                            "浏览器启动失败",
                            failed_at,
                            failed_at,
                            failed_at,
                            failed_at,
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()
                result = app.crawl_observability(days=7)
        self.assertEqual(result["run_count"], 2)
        self.assertEqual(result["status_counts"]["succeeded"], 1)
        self.assertEqual(result["status_counts"]["failed"], 1)
        self.assertEqual(result["retryable_failures"], 1)
        self.assertEqual(result["content_hash_changes"], 1)
        self.assertEqual(result["source_quality"]["distribution"]["fresh"], 1)
        self.assertEqual(result["sample_status"], "insufficient")

    def test_aihot_review_stats_reports_feedback_and_opportunity_maturity(self):
        items = [{"id": "signal-1", "source": "source-a"}, {"id": "signal-2", "source": "source-b"}]
        work_items = [{"id": 7, "kind": "opportunity", "source_project": "aihot"}]
        reviews = [{"kind": "aihot_opportunity_review", "created_at": "2026-08-09T02:00:00+00:00", "metadata": {"review": {"verdict": "继续验证", "confirmed": True, "reviewed_at": "2026-08-09T02:00:00+00:00"}}}]

        def fake_work_items(_status="all", project_id=""):
            return work_items if project_id == "aihot" else []

        with patch.object(app, "load_aihot_snapshot", return_value={"items": items}), patch.object(app, "dedupe_aihot_items", side_effect=lambda value: value), patch.object(
            app, "list_aihot_feedback", return_value={"signal-1": {"vote": "useful", "updated_at": "2026-08-09T01:00:00+00:00"}, "signal-2": {"vote": "not_useful", "updated_at": "2026-08-09T01:30:00+00:00"}}
        ), patch.object(app, "list_work_items", side_effect=fake_work_items), patch.object(app, "list_artifacts", return_value=reviews):
            stats = app.aihot_review_stats()
        self.assertEqual(stats["feedback"]["total"], 2)
        self.assertEqual(stats["feedback"]["useful"], 1)
        self.assertEqual(stats["feedback"]["sample_status"], "insufficient")
        self.assertEqual(stats["opportunities"]["total"], 1)
        self.assertEqual(stats["opportunities"]["reviewed"], 1)
        self.assertEqual(stats["opportunities"]["confirmed"], 1)
        self.assertEqual(stats["opportunities"]["verdicts"], {"继续验证": 1})

    def test_cid_review_stats_reports_repo_filter_and_preference_match(self):
        opportunities = [{"id": 9, "kind": "opportunity", "source_project": "cid-dashboard", "metadata": {"repo": "owner/repo"}}]
        reviews = [{"kind": "cid_opportunity_review", "created_at": "2026-08-09T02:00:00+00:00", "metadata": {"repo": "owner/repo", "review": {"verdict": "保留", "confirmed": True, "preference_match": 0.8, "reviewed_at": "2026-08-09T02:00:00+00:00"}}}]

        def fake_work_items(_status="all", project_id=""):
            return opportunities if project_id == "cid-dashboard" else []

        with patch.object(app, "list_work_items", side_effect=fake_work_items), patch.object(app, "list_artifacts", return_value=reviews):
            stats = app.cid_review_stats("owner/repo")
        self.assertEqual(stats["opportunities"], 1)
        self.assertEqual(stats["reviewed"], 1)
        self.assertEqual(stats["confirmed"], 1)
        self.assertEqual(stats["preference_match_rate"], 0.8)
        self.assertEqual(stats["sample_status"], "insufficient")

    def test_idea_interview_route_persists_artifact_relation_and_replays_in_pack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            outputs_dir = Path(temp_dir) / "outputs"
            outputs_dir.mkdir()
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "OUTPUTS_DIR", outputs_dir), patch.object(app, "_DB_SCHEMA_READY", False):
                session = app.create_idea_session("酒店差评处理")

                async def exercise_route():
                    transport = httpx.ASGITransport(app=app.app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                        invalid = await client.post(
                            f"/api/idea-analysis/sessions/{session['id']}/interviews",
                            json={"participant": "前台负责人", "question": "最近一次差评怎么处理？", "answer": ""},
                        )
                        saved = await client.post(
                            f"/api/idea-analysis/sessions/{session['id']}/interviews",
                            json={
                                "participant": "前台负责人",
                                "question": "最近一次差评怎么处理？",
                                "answer": "我们先人工查订单，再请值班经理确认，通常要花二十分钟。",
                                "source": "2026-08-09 电话访谈",
                                "status": "supported",
                            },
                        )
                        evidence = await client.get(f"/api/idea-analysis/sessions/{session['id']}/evidence")
                        pack = await client.get(f"/api/idea-analysis/sessions/{session['id']}/evidence-pack")
                        return invalid, saved, evidence, pack

                invalid, saved, evidence, pack = asyncio.run(exercise_route())

                self.assertEqual(invalid.status_code, 422)
                self.assertEqual(saved.status_code, 200)
                artifact = saved.json()["artifact"]
                self.assertEqual(artifact["kind"], "idea_interview")
                self.assertEqual(artifact["metadata"]["participant"], "前台负责人")
                output_file = Path(artifact["path"])
                self.assertTrue(output_file.exists())
                self.assertIn("二十分钟", output_file.read_text(encoding="utf-8"))
                self.assertEqual(evidence.status_code, 200)
                self.assertEqual(evidence.json()["evidence"][0]["kind"], "idea_interview")
                self.assertEqual(pack.status_code, 200)
                self.assertEqual(pack.json()["summary"]["supported"], 1)
                self.assertEqual(pack.json()["summary"]["interviews"], 1)
                self.assertEqual(pack.json()["summary"]["evidence_total"], 1)
                self.assertEqual(pack.json()["summary"]["support_rate"], 1.0)
                self.assertEqual(pack.json()["summary"]["sample_status"], "insufficient")
                self.assertEqual(len(pack.json()["artifacts"]), 1)
                relations = app.list_relations(session["id"])
                self.assertTrue(any(item["relation_type"] == "session_to_interview" for item in relations))

    def test_crawl_source_references_keep_hash_quality_and_line_locator(self):
        run = {
            "id": "crawl-1",
            "created_at": "2026-08-09T00:00:00+00:00",
            "finished_at": "2026-08-09T01:00:00+00:00",
            "documents": [{
                "url": "https://example.com/research",
                "title": "研究页面",
                "content_hash": "hash-123",
                "source_quality": {"score": 0.8, "label": "高"},
                "data_as_of": "2026-08-09T01:00:00+00:00",
            }],
        }
        refs = app.crawl_source_references(run, [{"url": "https://example.com/research", "title": "研究页面", "content_hash": "hash-123", "data_as_of": run["finished_at"], "locator": {"line_start": 12, "line_end": 15, "source_quality": {"score": 0.8}}}], artifact_id=42)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["content_hash"], "hash-123")
        self.assertEqual(refs[0]["line_start"], 12)
        self.assertEqual(refs[0]["line_end"], 15)
        self.assertIn("#L12-L15", refs[0]["locator"])
        self.assertEqual(refs[0]["artifact_id"], 42)

    def test_evidence_quality_separates_freshness_from_truth(self):
        fresh = app.evidence_quality_descriptor(source="来源 A", data_as_of=app.now_iso(), readable=True, content_hash="abc")
        stale = app.evidence_quality_descriptor(source="来源 B", data_as_of="2020-01-01T00:00:00+00:00", readable=True, content_hash="def")
        unreadable = app.evidence_quality_descriptor(source="来源 C", data_as_of=app.now_iso(), readable=False, read_error="文件消失")
        self.assertEqual(fresh["freshness"], "fresh")
        self.assertEqual(fresh["quality_status"], "fresh")
        self.assertEqual(stale["freshness"], "stale")
        self.assertEqual(stale["quality_status"], "review")
        self.assertEqual(unreadable["quality_status"], "unreadable")

    def test_agent_result_contract_reports_source_coverage_by_type(self):
        contract = app.agent_result_contract(
            "knowledge",
            "结论\n已完成来源核对。\n事实\n- 有一条可回放记录",
            source_refs=[
                {"type": "artifact", "id": 7, "label": "研究产物", "path": "outputs/report.md", "data_as_of": "2026-08-09T01:00:00+00:00"},
                {"type": "work_item", "id": 9, "label": "后续待办", "data_as_of": "2026-08-09T01:10:00+00:00"},
            ],
        )
        self.assertEqual(contract["source_coverage"]["status"], "complete")
        self.assertEqual(contract["source_coverage"]["types"], {"artifact": 1, "work_item": 1})
        self.assertEqual(contract["source_coverage"]["with_data_time"], 2)

    def test_knowledge_draft_source_check_blocks_changed_artifact(self):
        draft = {"metadata": {"source_artifact_ids": [7], "source_content_hashes": {"7": "expected"}}}
        with patch.object(app, "get_artifact_record", return_value={"id": 7, "name": "来源"}), patch.object(app, "read_artifact_source", return_value=("changed body", "")):
            result = app.knowledge_draft_source_check(draft)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["checks"][0]["status"], "changed")
        self.assertIn("内容已变化", result["message"])

    def test_knowledge_draft_source_check_accepts_same_readable_artifact(self):
        body = "same body"
        digest = app.hashlib.sha256(body.encode("utf-8")).hexdigest()
        draft = {"metadata": {"source_artifact_ids": [7], "source_content_hashes": {"7": digest}}}
        with patch.object(app, "get_artifact_record", return_value={"id": 7, "name": "来源"}), patch.object(app, "read_artifact_source", return_value=(body, "")):
            result = app.knowledge_draft_source_check(draft)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["status"], "verified")

    def test_target_agent_registry_matches_implemented_capabilities(self):
        self.assertEqual(app.AGENT_REGISTRY["sub2api"]["status"], "implemented")
        self.assertIn("来源回溯", app.AGENT_IMPLEMENTATIONS["inbox"]["implemented"])
        self.assertIn("引用回放 UI", app.AGENT_IMPLEMENTATIONS["knowledge"]["implemented"])
        self.assertIn("段落稳定 ID", app.AGENT_IMPLEMENTATIONS["knowledge"]["implemented"])
        self.assertNotIn("段落级精细冲突处置", app.AGENT_IMPLEMENTATIONS["knowledge"]["gaps"])
        self.assertIn("段落级引用覆盖检查", app.AGENT_IMPLEMENTATIONS["doc-factory"]["implemented"])
        self.assertIn("AI 热点摘要 Web Push", app.AGENT_IMPLEMENTATIONS["aihot"]["implemented"])
        self.assertIn("额度预测", app.AGENT_IMPLEMENTATIONS["sub2api"]["implemented"])
        self.assertIn("回测样本质量校验", app.AGENT_IMPLEMENTATIONS["market"]["implemented"])
        self.assertIn("多轮修订审批闭环", app.AGENT_IMPLEMENTATIONS["doc-factory"]["implemented"])
        self.assertNotIn("多轮修订审批闭环", app.AGENT_IMPLEMENTATIONS["doc-factory"]["gaps"])
        self.assertIn("结构化访谈", app.AGENT_IMPLEMENTATIONS["idea-analysis"]["implemented"])
        self.assertIn("个人偏好学习", app.AGENT_IMPLEMENTATIONS["cid-dashboard"]["implemented"])
        self.assertNotIn("跨来源统一比较", app.AGENT_IMPLEMENTATIONS["cid-dashboard"]["gaps"])

    def test_document_extraction_keeps_native_fallback_when_optional_adapter_is_unavailable(self):
        with patch.object(app, "extract_with_markitdown", return_value=""):
            self.assertEqual(app.extract_document_bytes("标题\n内容".encode(), "note.md"), "标题\n内容")
            with self.assertRaisesRegex(ValueError, "MarkItDown"):
                app.extract_document_bytes(b"not-a-slide", "slides.pptx")

    def test_markitdown_status_does_not_expose_credentials(self):
        status = app.markitdown_status()
        self.assertIn(status["label"], {"MarkItDown 可用", "内置解析器"})
        self.assertNotIn("api_key", status)

    def test_inbox_classifier_stats_exposes_samples_and_distribution_without_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                connection = app.db_connection()
                try:
                    timestamp = "2026-08-09T00:00:00+00:00"
                    connection.executemany(
                        "INSERT INTO inbox (content, kind, classification, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        [("一条待办", "task", "task", timestamp, timestamp), ("一条笔记", "note", "note", timestamp, timestamp)],
                    )
                    connection.executemany(
                        "INSERT INTO inbox_classification_feedback (inbox_id, predicted, accepted, confidence, created_at) VALUES (?, ?, ?, ?, ?)",
                        [(1, "task", "task", 0.8, timestamp), (2, "task", "note", 0.4, timestamp)],
                    )
                    connection.commit()
                finally:
                    connection.close()
                stats = app.inbox_classifier_stats()
        self.assertEqual(stats["sample_count"], 2)
        self.assertEqual(stats["correction_count"], 1)
        self.assertEqual(stats["sample_status"], "insufficient")
        self.assertEqual(stats["classified_count"], 2)
        self.assertEqual(stats["accuracy"], 0.5)
        self.assertEqual({item["classification"] for item in stats["per_class"]}, {"note", "task"})
        classes = {item["classification"]: item for item in stats["classes"]}
        self.assertEqual(classes["note"]["label"], "笔记")
        self.assertNotIn("token", str(stats).lower())

    def test_inbox_next_steps_prefers_explicit_capture(self):
        result = app.extract_inbox_next_steps("下一步：确认负责人；然后补充截止时间。", "task")
        self.assertEqual(result["source"], "captured_text")
        self.assertEqual(result["steps"], ["确认负责人", "补充截止时间"])

    def test_inbox_next_steps_is_conservative_when_capture_has_no_action(self):
        result = app.extract_inbox_next_steps("一个值得以后再看的想法", "idea")
        self.assertEqual(result["source"], "conservative_template")
        self.assertIn("关键假设", result["steps"][0])

    def test_inbox_handoff_carries_reviewable_next_steps_into_work_item(self):
        item = {
            "id": 12,
            "content": "整理一份研究报告",
            "classification": "research",
            "due_at": "2026-08-12",
            "priority": "normal",
            "analysis": {"classification_label": "研究", "next_steps": ["确认研究问题", "补充来源"], "next_steps_source": "captured_text"},
        }
        candidate = {"id": 4, "target_project": "knowledge", "target_name": "知识库 Agent", "route_kind": "note_capture", "status": "suggested"}
        work_item = {"id": 99}
        with patch.object(app, "get_inbox_record", return_value=item), \
             patch.object(app, "get_inbox_route_candidate", return_value=candidate), \
             patch.object(app, "create_work_item_record", return_value=work_item) as create_item, \
             patch.object(app, "create_relation_record", side_effect=[{"id": 1}, {"id": 2}]), \
             patch.object(app, "update_inbox_route_candidate"), \
             patch.object(app, "db_connection") as db_connection, \
             patch.object(app, "create_notification_record"):
            connection = db_connection.return_value
            connection.__enter__.return_value = connection
            result = app.accept_inbox_route(12, 4)
        description = create_item.call_args.kwargs["description"]
        metadata = create_item.call_args.kwargs["metadata"]
        self.assertIn("确认研究问题", description)
        self.assertEqual(metadata["next_steps_source"], "captured_text")
        self.assertEqual(result["work_item"]["id"], 99)

    @patch.object(
        app,
        "list_market_history",
        return_value=[
            {"checked_at": "2026-08-01T00:00:00+00:00", "quotes": [{"symbol": "sh600000", "price": "10", "previous_close": "9.9", "change_pct": "1.01"}]},
            {"checked_at": "2026-08-02T00:00:00+00:00", "quotes": [{"symbol": "sh600000", "price": "10.2", "previous_close": "10", "change_pct": "2"}]},
            {"checked_at": "2026-08-03T00:00:00+00:00", "quotes": [{"symbol": "sh600000", "price": "bad", "previous_close": "10.2", "change_pct": "0"}]},
        ],
    )
    def test_market_backtest_reports_sample_quality(self, _history):
        result = app.market_backtest("sh600000", "momentum", 2, 10, 5)
        quality = result["sample_quality"]
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(quality["valid_count"], 2)
        self.assertEqual(quality["rejected_count"], 1)
        self.assertEqual(quality["minimum_required"], 3)
        self.assertEqual(quality["status"], "insufficient")

    def test_sub2api_sync_state_exposes_recovery_action_without_credentials(self):
        store = {}

        def load():
            return dict(store)

        def save(values):
            store.clear()
            store.update(values)

        with patch.object(app, "load_sub2api_panel_settings", side_effect=load), patch.object(app, "save_sub2api_panel_settings", side_effect=save):
            app.update_sub2api_sync_state("failed", source="panel_admin_auto", error="面板登录凭证已失效")
            state = app.sub2api_sync_state()

        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["credential_invalid"])
        self.assertIn("失效", state["label"])
        self.assertIn("重新登录", state["next_action"])
        self.assertNotIn("refresh_token", state)

    def test_sub2api_cost_breakdown_aggregates_groups_without_key_values(self):
        result = app.sub2api_cost_breakdown({"keys": [
            {"group": "OpenAI", "today_cost": "$1.20", "month_cost": "$8.00", "masked": "sk-...111"},
            {"group": "OpenAI", "today_cost": "$0.80", "month_cost": "$2.00", "masked": "sk-...222"},
            {"group": "DeepSeek", "today_cost": "", "month_cost": "$4.00", "masked": "sk-...333"},
        ]})
        groups = {item["group"]: item for item in result["groups"]}
        self.assertEqual(groups["OpenAI"]["today_cost"], 2.0)
        self.assertEqual(groups["OpenAI"]["month_cost"], 10.0)
        self.assertEqual(groups["DeepSeek"]["today_cost"], None)
        self.assertEqual(result["unpriced_count"], 1)
        self.assertNotIn("sk-...111", str(result))

    def test_sub2api_browser_script_uses_versioned_api_and_browser_side_redaction(self):
        result = asyncio.run(app.get_sub2api_browser_sync_script())
        self.assertIn("/api/v1", result["script"])
        self.assertIn("panel_bookmarklet_v2", result["script"])
        self.assertIn("/api/sub2api/sync-raw", result["script"])
        self.assertIn("[已隐藏]", result["script"])
        self.assertNotIn("password", result["script"].lower())

    def test_sub2api_raw_sync_rejects_missing_or_untrusted_panel_origin(self):
        request = app.Sub2APIRawSyncRequest(payload={"me": {"balance": "$0"}}, source="panel_bookmarklet_v2")
        for origin in (None, "https://evil.example"):
            with self.assertRaises(app.HTTPException) as context:
                asyncio.run(app.sync_sub2api_panel_raw(request, origin=origin))
            self.assertEqual(context.exception.status_code, 403)

    def test_sub2api_raw_sync_accepts_configured_panel_origin(self):
        request = app.Sub2APIRawSyncRequest(payload={"me": {"balance": "$0"}}, source="panel_bookmarklet_v2")
        with patch.object(app, "record_sub2api_snapshot", return_value=({"checked_at": "now"}, {}, None)), \
             patch.object(app, "list_sub2api_history", return_value=[]), \
             patch.object(app, "sub2api_sync_state", return_value={"status": "succeeded"}):
            result = asyncio.run(app.sync_sub2api_panel_raw(request, origin="https://sub.chengsir.asia"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["snapshot"]["checked_at"], "now")

    def test_sub2api_route_returns_cost_breakdown_without_raw_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            snapshot_file = Path(temp_dir) / "sub2api_snapshot.json"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "SUB2API_SNAPSHOT_FILE", snapshot_file), patch.object(
                app, "_DB_SCHEMA_READY", False
            ), patch.object(app, "load_sub2api_panel_settings", return_value={}), patch.object(app, "register_artifact_safely", return_value=None):
                app.record_sub2api_snapshot(
                    {
                        "logged_in": True,
                        "checked_at": "2026-08-09T01:00:00+00:00",
                        "subscription": {"weekly_usage": "$1 / $10", "monthly_usage": "$2 / $20"},
                        "keys": [{"group": "Provider A", "today_cost": "$1.00", "month_cost": "$3.00", "key": "sk-live-secret-value"}],
                    },
                    source="test",
                )

                async def exercise_route():
                    transport = httpx.ASGITransport(app=app.app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                        return await client.get("/api/sub2api")

                response = asyncio.run(exercise_route())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["cost_breakdown"]["groups"][0]["month_cost"], 3.0)
        self.assertNotIn("sk-live-secret-value", response.text)

    def test_knowledge_retrieval_evaluation_marks_insufficient_samples(self):
        rows = [{"path": "a.md"}, {"path": "b.md"}, {"path": "empty.md"}]
        notes = {
            "a.md": {"title": "行情研究", "path": "a.md"},
            "b.md": {"title": "阅读计划", "path": "b.md"},
            "empty.md": {"title": "", "path": "empty.md"},
        }
        with patch.object(app, "obsidian_index_rows", return_value=rows), \
             patch.object(app, "obsidian_note_row", side_effect=lambda row: notes[row["path"]]), \
             patch.object(app, "obsidian_search", side_effect=lambda query, limit=5: [{"path": "a.md" if "行情" in query else "b.md"}]), \
             patch.object(app, "obsidian_semantic_results", side_effect=lambda query, limit=5: [{"path": "a.md" if "行情" in query else "b.md"}]):
            result = app.obsidian_retrieval_evaluation(sample_limit=3, top_k=5)
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["sample_status"], "insufficient")
        self.assertEqual(result["minimum_samples"], 10)
        self.assertEqual(result["hybrid_gain"], 0.0)

    def test_knowledge_conflict_report_exposes_paragraph_line_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            (vault / "left.md").write_text("# 方案\n\n这套方案有效，适合当前团队。\n\n截止时间是周五。\n", encoding="utf-8")
            (vault / "right.md").write_text("# 方案\n\n这套方案无效，不适合当前团队。\n\n截止时间是周一。\n", encoding="utf-8")
            left = {"path": "left.md", "title": "方案", "content_hash": "left-hash"}
            right = {"path": "right.md", "title": "方案", "content_hash": "right-hash"}
            with patch.object(app, "OBSIDIAN_VAULT_DIR", vault):
                result = app.obsidian_paragraph_conflicts(left, right, (("有效", "无效"), ("适合", "不适合")))
        self.assertTrue(result)
        self.assertEqual(result[0]["kind"], "confirmed_conflict")
        self.assertEqual(result[0]["left"]["line_start"], 3)
        self.assertEqual(result[0]["right"]["line_start"], 3)
        self.assertIn("有效", result[0]["left"]["text"])
        self.assertEqual(len(result[0]["paragraph_key"]), 24)
        conflict_key = app.obsidian_conflict_key({"path": "left.md", "content_hash": "left-hash"}, {"path": "right.md", "content_hash": "right-hash"})
        self.assertEqual(result[0]["paragraph_key"], app.obsidian_conflict_paragraph_key(conflict_key, result[0]))

    def test_sub2api_browser_script_has_retry_and_client_snapshot_id(self):
        result = asyncio.run(app.get_sub2api_browser_sync_script())
        self.assertIn("AbortController", result["script"])
        self.assertIn("client_snapshot_id", result["script"])
        self.assertIn("deduplicated", result["script"])

    def test_sub2api_duplicate_client_snapshot_is_not_written_twice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            snapshot_file = Path(temp_dir) / "sub2api_snapshot.json"
            settings_file = Path(temp_dir) / "sub2api-settings.json"
            patches = [
                patch.object(app, "DATABASE_FILE", database_file),
                patch.object(app, "SUB2API_SNAPSHOT_FILE", snapshot_file),
                patch.object(app, "SUB2API_PANEL_SETTINGS_FILE", settings_file),
                patch.object(app, "_DB_SCHEMA_READY", False),
                patch.object(app, "register_artifact_safely", return_value=None),
            ]
            for item in patches:
                item.start()
            try:
                first, _, _ = app.record_sub2api_snapshot({"logged_in": True, "balance": "$1", "client_snapshot_id": "wb-client-123456"}, "panel_bookmarklet_v2")
                second, _, _ = app.record_sub2api_snapshot({"logged_in": True, "balance": "$1", "client_snapshot_id": "wb-client-123456"}, "panel_bookmarklet_v2")
                self.assertNotIn("_deduplicated", first)
                self.assertTrue(second.pop("_deduplicated", False))
                self.assertEqual(len(app.list_sub2api_history()), 1)
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_knowledge_paragraph_resolution_creates_review_draft_without_vault_write(self):
        pair = {
            "left": {"path": "left.md", "line_start": 3, "line_end": 3, "text": "左侧有效"},
            "right": {"path": "right.md", "line_start": 3, "line_end": 3, "text": "右侧无效"},
            "kind": "confirmed_conflict",
            "kind_label": "明确矛盾",
            "similarity": 0.6,
        }
        pair["paragraph_key"] = app.obsidian_conflict_paragraph_key("conflict-key", pair)
        conflict = {"conflict_key": "conflict-key", "left": {"path": "left.md"}, "right": {"path": "right.md"}, "paragraph_conflicts": [pair]}
        with patch.object(app, "obsidian_conflict_report", return_value={"possible_conflicts": [conflict], "confirmed_conflicts": []}), \
             patch.object(app, "save_knowledge_conflict_paragraph_resolution", return_value={"action": "merge", "paragraph_key": pair["paragraph_key"]}) as save_resolution, \
             patch.object(app, "register_artifact_safely", return_value={"id": 10, "kind": "knowledge_conflict_paragraph_resolution_record"}) as register_artifact, \
             patch.object(app, "knowledge_conflict_paragraph_draft", return_value={"id": 11, "name": "段落草稿.md"}), \
             patch.object(app, "create_relation_record", return_value={"id": 12}):
            result = asyncio.run(app.resolve_obsidian_conflict_paragraph("conflict-key", pair["paragraph_key"], app.ObsidianConflictParagraphResolutionRequest(action="merge", note="按数据时间合并", confirmed=True)))
        self.assertTrue(result["ok"])
        self.assertEqual(result["draft"]["id"], 11)
        self.assertEqual(save_resolution.call_args.args[2], "merge")
        self.assertEqual(register_artifact.call_args.kwargs["metadata"]["paragraph_key"], pair["paragraph_key"])

    def test_inbox_knowledge_handoff_preserves_source_and_relation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            knowledge_dir = Path(temp_dir) / "knowledge-base"
            knowledge_dir.mkdir()
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "KNOWLEDGE_DIR", knowledge_dir), patch.object(
                app, "_DB_SCHEMA_READY", False
            ):
                connection = app.db_connection()
                try:
                    connection.execute(
                        "INSERT INTO inbox (content, kind, classification, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        ("整理一份行情研究笔记", "research", "research", "2026-08-09T00:00:00+00:00", "2026-08-09T00:00:00+00:00"),
                    )
                    connection.commit()
                finally:
                    connection.close()

                result = asyncio.run(
                    app.create_knowledge_note(
                        app.InboxRequest(content="# 行情研究\n\n保留来源。", kind="研究笔记", source="inbox:1")
                    )
                )

                note = result["note"]
                artifact = note["artifact"]
                relation = note["relation"]

            self.assertEqual(artifact["kind"], "inbox_handoff_note")
            self.assertEqual(artifact["metadata"]["source"], "inbox:1")
            self.assertEqual(artifact["metadata"]["source_inbox_id"], 1)
            self.assertEqual(relation["relation_type"], "captured_as_knowledge")
            self.assertEqual(relation["from_type"], "inbox")
            self.assertEqual(relation["from_id"], "1")
            self.assertEqual(relation["to_type"], "artifact")
            self.assertEqual(relation["to_id"], str(artifact["id"]))

    def test_market_backtest_quality_reports_mixed_source_stability(self):
        result = app.market_backtest_quality(
            [{"checked_at": "2026-08-01T00:00:00+00:00", "source": "source-a"}, {"checked_at": "2026-08-02T00:00:00+00:00", "source": "source-b"}],
            [],
            2,
        )
        self.assertEqual(result["source_stability"], "mixed")
        self.assertEqual(result["source_counts"], {"source-a": 1, "source-b": 1})

    def test_market_backtest_and_strategy_compare_return_auditable_cost_contract(self):
        history = [
            {"checked_at": "2026-08-01T00:00:00+00:00", "source": "source-a", "quotes": [{"symbol": "sh600000", "price": "10", "previous_close": "9.9", "change_pct": "1"}]},
            {"checked_at": "2026-08-02T00:00:00+00:00", "source": "source-a", "quotes": [{"symbol": "sh600000", "price": "10.5", "previous_close": "10", "change_pct": "5"}]},
            {"checked_at": "2026-08-03T00:00:00+00:00", "source": "source-a", "quotes": [{"symbol": "sh600000", "price": "10.2", "previous_close": "10.5", "change_pct": "-2.86"}]},
            {"checked_at": "2026-08-04T00:00:00+00:00", "source": "source-a", "quotes": [{"symbol": "sh600000", "price": "11", "previous_close": "10.2", "change_pct": "7.84"}]},
        ]
        with patch.object(app, "list_market_history", return_value=history), patch.object(app, "register_artifact_safely", return_value={"id": 22, "kind": "market_backtest"}):
            result = asyncio.run(app.run_market_backtest(app.MarketBacktestRequest(symbol="sh600000", strategy="momentum", window=2, fee_bps=10, slippage_bps=5)))
            comparison = asyncio.run(app.compare_market_strategies(app.MarketStrategyCompareRequest(symbol="sh600000", strategies=["momentum", "mean_reversion"], window=2, fee_bps=10, slippage_bps=5)))
        self.assertEqual(result["backtest"]["cost_assumptions"]["fee_bps"], 10)
        self.assertIn("sample_quality", result["backtest"])
        self.assertEqual(len(comparison["comparison"]), 2)
        self.assertEqual(comparison["policy"].split("，")[0], "只比较本地历史快照")

    def test_knowledge_draft_replay_returns_bounded_line_excerpt(self):
        draft = {"id": 8, "project_id": "knowledge", "name": "草稿.md", "kind": "paragraph_selection_draft", "metadata": {"source_artifact_ids": [3], "source_locators": [{"artifact_id": 3, "line_start": 2, "line_end": 2}]}}
        source = {"id": 3, "project_id": "market", "name": "来源.md", "path": "/safe/source.md", "metadata": {"data_as_of": "2026-08-01"}, "created_at": "2026-08-01T00:00:00+00:00"}
        with patch.object(app, "get_artifact_record", side_effect=lambda artifact_id: draft if artifact_id == 8 else source), patch.object(app, "read_artifact_source", return_value=("第一行\n第二行\n第三行", "")):
            result = app.knowledge_draft_replay(8)

        self.assertEqual(result["sources"][0]["ranges"], [{"line_start": 2, "line_end": 2}])
        self.assertEqual(result["sources"][0]["excerpts"][0]["text"], "第二行")

    def test_sub2api_explanation_targets_are_unique(self):
        markup = (app.ROOT / "static/sub2api.html").read_text(encoding="utf-8")
        self.assertNotIn('id="change-explanation"', markup)
        self.assertIn('id="quota-change-explanation"', markup)
        self.assertIn('id="snapshot-change-explanation"', markup)

    def test_sub2api_page_shares_initial_requests_and_guards_risk_handler(self):
        markup = (app.ROOT / "static/sub2api.html").read_text(encoding="utf-8")
        agent = (app.ROOT / "static/sub2api-agent.js").read_text(encoding="utf-8")
        self.assertIn("window.__sub2ApiLoadPromise = load();", markup)
        self.assertIn("window.__sub2ApiTrendPromise = loadTrendDelta();", markup)
        self.assertIn('dataset.sub2apiInlineBound = "true"', markup)
        self.assertIn('dataset.sub2apiInlineBound !== "true"', agent)
        self.assertIn("window.__sub2ApiLoadPromise", agent)
        self.assertIn("window.__sub2ApiTrendPromise", agent)
        self.assertIn("Promise.all([load(), loadPanelSettings()])", markup)

    def test_market_research_work_item_preserves_data_quality(self):
        snapshot = {"checked_at": "2026-08-09T02:00:00+00:00", "source": "公开行情源", "watchlist": [{"symbol": "sh600000"}]}
        analysis = {"freshness": {"status": "fresh", "label": "新鲜"}, "warnings": []}
        with patch.object(app, "load_market_snapshot", return_value=snapshot), \
             patch.object(app, "list_market_history", return_value=[{"checked_at": "2026-08-08T02:00:00+00:00"}]), \
             patch.object(app, "analyze_market_snapshot", return_value=analysis), \
             patch.object(app, "register_artifact_safely", return_value={"id": 7}), \
             patch.object(app, "create_work_item_record", return_value={"id": 8}) as create_item, \
             patch.object(app, "create_relation_record", return_value={"id": 9}):
            result = asyncio.run(app.create_market_research(app.MarketResearchRequest(symbol="sh600000", question="为什么波动增加？")))
        quality = create_item.call_args.kwargs["metadata"]["data_quality"]
        self.assertEqual(quality["freshness_status"], "fresh")
        self.assertEqual(quality["history_count"], 1)
        self.assertIn("数据质量：新鲜", create_item.call_args.kwargs["description"])
        self.assertEqual(result["item"]["id"], 8)

    def test_market_research_conclusion_requires_explicit_confirmation(self):
        request = app.MarketResearchConclusionRequest(conclusion="先观察，不做交易")
        with self.assertRaises(app.HTTPException) as context:
            asyncio.run(app.conclude_market_research(123, request))
        self.assertEqual(context.exception.status_code, 409)

    def test_knowledge_draft_sync_requires_explicit_confirmation(self):
        request = app.KnowledgeDraftApplyRequest(confirmed=False)
        with self.assertRaises(app.HTTPException) as context:
            asyncio.run(app.sync_knowledge_draft(123, request))
        self.assertEqual(context.exception.status_code, 409)

    def test_collaboration_plan_is_not_queued_without_confirmation(self):
        snapshot = {
            "items": [{"work_item": {"id": 7, "title": "处理研究任务", "description": "补充来源"}, "target_project": "knowledge"}],
        }
        with patch.object(app, "workbench_collaboration_snapshot", return_value=snapshot), \
             patch.object(app, "create_execution_plan", return_value={"id": "plan-1"}) as create_plan, \
             patch.object(app, "get_execution_plan", return_value={"id": "plan-1", "status": "draft"}), \
             patch.object(app, "update_plan_status") as update_status:
            result = asyncio.run(app.prepare_workbench_collaboration(app.WorkbenchCollaborationRequest(confirmed=False, limit=1)))
        self.assertTrue(create_plan.called)
        self.assertFalse(update_status.called)
        self.assertIn("确认后", result["message"])

    def test_work_item_next_step_quality_is_conservative_and_explicit(self):
        missing = app.work_item_next_step_quality({"metadata": {}, "target_project": "knowledge"})
        self.assertEqual(missing["status"], "missing")
        review = app.work_item_next_step_quality({"metadata": {"next_steps": ["补充来源"], "next_steps_source": "captured_text"}, "target_project": ""})
        self.assertEqual(review["status"], "review")
        ready = app.work_item_next_step_quality({"metadata": {"next_steps": ["补充来源"], "next_steps_source": "captured_text", "due_at": "2026-08-12"}, "target_project": "knowledge"})
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["steps"], ["补充来源"])

    def test_collaboration_snapshot_explains_missing_next_step(self):
        snapshot = app.workbench_collaboration_snapshot
        with patch.object(app, "list_work_items", return_value=[{"id": 7, "title": "待整理", "description": "只有背景", "status": "open", "priority": "normal", "target_project": "knowledge", "source_project": "inbox", "metadata": {}}]), \
             patch.object(app, "project_audit", return_value={"agents": []}):
            result = snapshot(limit=1)
        self.assertEqual(result["items"][0]["recommendation"], "先补一条最小下一步")
        self.assertEqual(result["next_step_quality"]["counts"]["missing"], 1)

    def test_unconfirmed_workbench_decision_does_not_create_followup(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(app, "OUTPUTS_DIR", Path(temp_dir)), \
             patch.object(app, "register_artifact_safely", return_value={"id": 30}), \
             patch.object(app, "create_relation_record"), \
             patch.object(app, "create_notification_record"), \
             patch.object(app, "create_work_item_record") as create_item:
            result = app.create_workbench_decision(app.WorkbenchDecisionRequest(title="先观察", decision="暂不推进", next_steps=["下周复盘"], confirmed=False))
        self.assertIsNone(result["work_item"])
        self.assertFalse(create_item.called)
        self.assertIn("勾选确认", result["message"])

    def test_server_action_request_keeps_high_risk_execution_manual(self):
        approval = {"id": "approval-1", "kind": "server_action", "status": "pending", "payload": {"action": "restart"}}
        with patch.object(app, "create_approval_request", return_value=approval), \
             patch.object(app, "create_work_item_record", return_value={"id": 8}), \
             patch.object(app, "create_relation_record", return_value={"id": 9}), \
             patch.object(app, "create_notification_record", return_value={"id": 10}):
            result = asyncio.run(app.request_server_action(app.ServerActionRequest(action="restart", reason="服务需要人工重启", confirmed=True)))
        self.assertEqual(result["approval"]["id"], "approval-1")
        self.assertIn("不会直接改动服务器", result["message"])

    def test_approved_restart_is_recorded_but_never_runs_shell(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                approval = app.create_approval_request("server", "server_action", "服务器重启", {"action": "restart"})
                connection = app.db_connection()
                try:
                    connection.execute("UPDATE approval_requests SET status = 'approved' WHERE id = ?", (approval["id"],))
                    connection.commit()
                finally:
                    connection.close()
                result = asyncio.run(app.execute_approved_server_action(approval["id"]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["execution"]["status"], "manual_required")
        self.assertIn("不通过 Workbench 自动运行 shell", result["execution"]["result"]["execution_policy"])


if __name__ == "__main__":
    unittest.main()
