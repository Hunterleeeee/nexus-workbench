import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app


class DocumentFactoryTests(unittest.TestCase):
    def test_document_factory_templates_include_study_and_decision_workflows(self):
        templates = {item["id"]: item for item in app.document_factory_templates()}
        self.assertEqual(len(templates), 7)
        self.assertIn("study_notes", templates)
        self.assertIn("decision_record", templates)
        self.assertIn("复习题", templates["study_notes"]["instruction"])
        self.assertIn("最终决定", templates["decision_record"]["instruction"])
        self.assertIn("不要编造", templates["study_notes"]["instruction"])
        self.assertIn("待补充", templates["decision_record"]["instruction"])

    def test_citation_coverage_distinguishes_marked_and_unmarked_supported_paragraphs(self):
        materials = {"materials": [{"content": "本项目当前交付周期通常为 7 天，用户反馈主要集中在导出速度和结果回溯。"}]}
        result = app.document_factory_citation_coverage(
            "结论：本项目当前交付周期通常为 7 天，用户反馈主要集中在导出速度。[来源：材料]\n\n用户反馈主要集中在导出速度和结果回溯，需要继续观察。",
            materials,
        )
        self.assertEqual(result["paragraph_count"], 2)
        self.assertEqual(result["marked_supported_paragraphs"], 1)
        self.assertIn("用户反馈", result["unmarked_supported_examples"][0])

    def test_structured_revision_fields_are_bounded_and_preserved_in_prompt_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            fake_artifact = {
                "id": 77,
                "name": "revision.md",
                "metadata": {
                    "title": "修订文档",
                    "version": 2,
                    "source_artifact_ids": [],
                },
            }
            captured = {}

            async def fake_llm(messages, **_kwargs):
                captured["messages"] = messages
                return "# 修订结果\n\n- 结论：保留来源。"

            request = app.DocumentFactoryRequest(
                title="修订文档",
                source_text="这是一段足够长的材料，用于验证结构化修订字段会进入文档工厂提示。" * 2,
                instruction="整理成结论优先的报告",
                revision_focus=["引用覆盖", "结构与逻辑"],
                acceptance_criteria=["关键结论都有来源"],
                revision_from_artifact_id=12,
            )
            with patch.object(app, "OUTPUTS_DIR", output_dir), patch.object(app, "llm_settings", return_value={"configured": True}), patch.object(
                app, "list_artifacts", return_value=[fake_artifact]
            ), patch.object(app, "register_artifact_safely", return_value={"id": 99, "metadata": {}}), patch.object(
                app, "call_llm", new=AsyncMock(side_effect=fake_llm)
            ):
                result = asyncio.run(app.run_document_factory(request))

            prompt = "\n".join(item["content"] for item in captured["messages"])
            self.assertIn("引用覆盖、结构与逻辑", prompt)
            self.assertIn("关键结论都有来源", prompt)
            self.assertEqual(result["artifact"]["id"], 99)
            self.assertEqual(result["validation"]["valid"], True)

    def test_regenerate_request_accepts_review_focus_and_acceptance_criteria(self):
        request = app.DocumentFactoryRegenerateRequest(
            artifact_id=3,
            approval_id="approval-round-1",
            reviewer_note="补充数据时间",
            revision_focus=["事实一致性", "引用覆盖"],
            acceptance_criteria=["每个关键数据都带时间"],
        )
        self.assertEqual(request.revision_focus, ["事实一致性", "引用覆盖"])
        self.assertEqual(request.acceptance_criteria[0], "每个关键数据都带时间")
        self.assertEqual(request.approval_id, "approval-round-1")

    def test_delivery_request_keeps_parent_approval_for_next_round(self):
        request = app.DocumentDeliveryRequest(artifact_id=9, formats=["docx"], title="交付", parent_approval_id="approval-round-1")
        self.assertEqual(request.parent_approval_id, "approval-round-1")


if __name__ == "__main__":
    unittest.main()
