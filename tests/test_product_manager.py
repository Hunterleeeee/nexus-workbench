import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import app


class ProductManagerTests(unittest.TestCase):
    def test_rice_score_is_explainable_and_safe(self):
        self.assertEqual(app.product_rice_score(100, 2, 80, 4), 40.0)
        self.assertEqual(app.product_rice_score(10, 1, 50, 0), 500.0)

    def test_feedback_requirement_and_decision_keep_auditable_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                feedback = app.create_product_feedback(app.ProductFeedbackRequest(
                    content="评审前需要翻多个群找用户反馈",
                    source="用户访谈",
                    persona="产品经理",
                    importance="high",
                ))
                requirement = app.create_product_requirement(app.ProductRequirementRequest(
                    title="统一反馈入口",
                    problem=feedback["content"],
                    target_user="产品经理",
                    outcome="评审前快速找到证据",
                    reach=100,
                    impact=2,
                    confidence=80,
                    effort=4,
                    feedback_ids=[feedback["id"]],
                ))
                decision = app.create_product_decision(app.ProductDecisionRequest(
                    requirement_id=requirement["id"],
                    title="首版只做手动导入",
                    decision="先验证反馈整理闭环",
                    rationale="减少集成成本，尽快获得真实样本",
                ))
                overview = app.product_manager_overview()
                relations = app.list_relations()

                self.assertGreater(feedback["artifact_id"], 0)
                self.assertEqual(requirement["score"], 40.0)
                self.assertEqual(requirement["evidence_count"], 1)
                self.assertGreater(requirement["work_item_id"], 0)
                self.assertGreater(decision["artifact_id"], 0)
                self.assertEqual(overview["summary"]["new_feedback"], 0)
                self.assertEqual(overview["summary"]["needs_evidence"], 0)
                self.assertIn("evidence_for", {item["relation_type"] for item in relations})
                self.assertIn("decision_for", {item["relation_type"] for item in relations})

                updated = app.update_product_requirement(
                    requirement["id"], app.ProductRequirementUpdateRequest(status="shipped")
                )
                work_item = app.get_work_item_record(updated["work_item_id"])
                self.assertEqual(updated["status"], "shipped")
                self.assertEqual(work_item["status"], "done")

    def test_product_manager_page_and_overview_api_are_available(self):
        async def request(database_file: Path):
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                transport = httpx.ASGITransport(app=app.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    page = await client.get("/projects/product-manager")
                    overview = await client.get("/api/product-manager/overview")
                    projects = await client.get("/api/projects")
                    return page, overview, projects

        with tempfile.TemporaryDirectory() as temp_dir:
            page, overview, projects = asyncio.run(request(Path(temp_dir) / "workbench.db"))
        self.assertEqual(page.status_code, 200)
        self.assertIn("产品作战室", page.text)
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["summary"]["feedback_total"], 0)
        product = next(item for item in projects.json()["projects"] if item["id"] == "product-manager")
        self.assertEqual(product["primary_action"]["href"], "/projects/product-manager")
        self.assertEqual(product["agent_name"], "产品经理 Agent")

    def test_cowart_prototype_canvas_and_published_version_are_auditable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_file = root / "workbench.db"
            prototype_dir = root / "product-prototypes"
            with (
                patch.object(app, "DATABASE_FILE", database_file),
                patch.object(app, "PRODUCT_PROTOTYPES_DIR", prototype_dir),
                patch.object(app, "_DB_SCHEMA_READY", False),
            ):
                requirement = app.create_product_requirement(app.ProductRequirementRequest(
                    title="评审原型闭环",
                    problem="需求评审时缺少可以直接标注的交互原型",
                    target_user="产品经理",
                    outcome="在同一工作台完成画布评审",
                ))
                prototype = app.create_product_prototype(
                    requirement["id"], app.ProductPrototypeRequest()
                )
                snapshot = {"schema": {"schemaVersion": 2}, "store": {}}
                saved = app._save_cowart_canvas(prototype["id"], snapshot)
                published = app.publish_product_prototype(
                    prototype["id"],
                    app.ProductPrototypePublishRequest(summary="首轮评审", confirmed=True),
                )
                relations = app.list_relations()

                self.assertTrue(prototype["created"])
                self.assertEqual(saved["storage"], "workbench-single-file")
                self.assertEqual(published["version"]["version"], 1)
                self.assertGreater(published["artifact"]["id"], 0)
                self.assertTrue(Path(published["version"]["snapshot_path"]).is_file())
                self.assertIn("requirement_to_prototype", {item["relation_type"] for item in relations})
                self.assertEqual(app.product_manager_summary()["prototypes_total"], 1)

    def test_cowart_canvas_http_adapter_is_namespaced_and_private(self):
        async def request(database_file: Path, prototype_dir: Path):
            with (
                patch.object(app, "DATABASE_FILE", database_file),
                patch.object(app, "PRODUCT_PROTOTYPES_DIR", prototype_dir),
                patch.object(app, "_DB_SCHEMA_READY", False),
            ):
                requirement = app.create_product_requirement(app.ProductRequirementRequest(title="无限画布"))
                prototype = app.create_product_prototype(requirement["id"], app.ProductPrototypeRequest())
                transport = httpx.ASGITransport(app=app.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    frame = await client.get(prototype["canvas_url"])
                    empty_canvas = await client.get(f"{prototype['canvas_url']}canvas")
                    saved_canvas = await client.put(
                        f"{prototype['canvas_url']}canvas",
                        json={"schema": {"schemaVersion": 2}, "store": {}},
                    )
                    status = await client.get("/api/product-manager/cowart/status")
                    return frame, empty_canvas, saved_canvas, status

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            frame, empty_canvas, saved_canvas, status = asyncio.run(
                request(root / "workbench.db", root / "product-prototypes")
            )
        self.assertEqual(frame.status_code, 200)
        self.assertIn("Content-Security-Policy", frame.headers)
        self.assertNotIn("googletagmanager", frame.text)
        self.assertIsNone(empty_canvas.json()["snapshot"])
        self.assertTrue(saved_canvas.json()["ok"])
        self.assertTrue(status.json()["available"])
        self.assertEqual(status.json()["analytics"], "disabled")

    def test_product_manager_frontend_has_accessible_workflows(self):
        root = Path(__file__).resolve().parents[1]
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        page = (root / "static" / "product-manager.html").read_text(encoding="utf-8")
        script = (root / "static" / "product-manager.js").read_text(encoding="utf-8")
        stylesheet = (root / "static" / "product-manager.css").read_text(encoding="utf-8")
        self.assertIn('role="tablist"', page)
        self.assertIn('data-product-tab="today"', page)
        self.assertIn('id="feedback-form"', page)
        self.assertIn('id="requirement-form"', page)
        self.assertIn('id="decision-form"', page)
        self.assertIn('data-product-tab="prototypes"', page)
        self.assertIn('id="prototype-canvas-frame"', page)
        self.assertIn(f"/static/request.js?v={version}", page)
        self.assertIn("/api/product-manager/overview", script)
        self.assertIn("data-requirement-prd", script)
        self.assertIn("data-requirement-prototype", script)
        self.assertIn("dataset.prototypePublish", script)
        self.assertIn("prefers-reduced-motion", stylesheet)
        self.assertIn("@media (max-width: 520px)", stylesheet)

    def test_vendored_cowart_disables_default_analytics_and_keeps_license_notice(self):
        root = Path(__file__).resolve().parents[1]
        vendor = root / "static" / "vendor" / "cowart"
        bundle = (vendor / app.COWART_SCRIPT_NAME).read_text(encoding="utf-8")
        notice = (vendor / "NOTICE.md").read_text(encoding="utf-8")
        self.assertNotIn("G-SJYHV19YZ9", bundle)
        self.assertIn("Pinned version: `0.1.25`", notice)
        self.assertIn("tldraw", notice)

    def test_overview_project_filter_applies_to_decisions_and_prototypes(self):
        """按项目查看时，决策/原型必须跟着所属需求一起过滤——
        否则「切到某项目，决策和原型 tab 还显示别家数据」就是逻辑不对。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                project = str(app.create_product_project("量化助手")["id"])
                other = str(app.create_product_project("文档工厂")["id"])
                req_a = app.create_product_requirement(app.ProductRequirementRequest(
                    title="量化需求", target_user="投研", problem="信号慢", outcome="更快",
                    reach=10, impact=2, confidence=80, effort=4, project_id=project,
                ))
                req_b = app.create_product_requirement(app.ProductRequirementRequest(
                    title="文档需求", target_user="写作者", problem="排版累", outcome="自动排版",
                    reach=10, impact=2, confidence=80, effort=4, project_id=other,
                ))
                app.create_product_decision(app.ProductDecisionRequest(
                    requirement_id=req_a["id"], title="量化决策", decision="先做回测",
                ))
                app.create_product_decision(app.ProductDecisionRequest(
                    requirement_id=req_b["id"], title="文档决策", decision="先做模板",
                ))
                filtered = app.product_manager_overview(project_id=project)
                everything = app.product_manager_overview()
        self.assertEqual([item["title"] for item in filtered["decisions"]], ["量化决策"],
                         "按项目过滤时决策应只显示该项目的")
        self.assertEqual(len(everything["decisions"]), 2, "不过滤时两边的决策都在")
        self.assertEqual(filtered["projects"]["selected"], project)


if __name__ == "__main__":
    unittest.main()
