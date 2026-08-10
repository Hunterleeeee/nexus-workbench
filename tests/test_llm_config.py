import os
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import httpx
from fastapi import HTTPException


class LlmConfigTests(unittest.TestCase):
    def test_duplicate_provider_names_get_distinct_operational_ids(self):
        providers = app.normalize_llm_providers(
            {
                "providers": [
                    {"name": "Same", "role": "fallback", "base_url": "https://a.test/v1", "model": "a", "api_key": "x"},
                    {"name": "Same", "role": "fallback", "base_url": "https://b.test/v1", "model": "b", "api_key": "y"},
                ]
            }
        )
        self.assertEqual([item["id"] for item in providers], ["same", "same-2"])

    def test_environment_fallback_is_used_when_saved_fallback_has_no_key(self):
        saved = {
            "providers": [
                {"name": "Saved", "role": "fallback", "base_url": "https://saved.test/v1", "model": "saved", "api_key": ""}
            ]
        }
        with patch.dict(os.environ, {"LLM_API_KEY": "sentinel", "LLM_BASE_URL": "https://env.test/v1", "LLM_MODEL": "env"}, clear=False):
            result = app.llm_fallback_credentials(saved)
        self.assertEqual(result["source"], "环境变量 fallback")
        self.assertEqual(result["base_url"], "https://env.test/v1")
        self.assertEqual(result["model"], "env")

    def test_environment_fallback_does_not_override_keyed_saved_fallback(self):
        saved = {
            "providers": [
                {"name": "Saved", "role": "fallback", "base_url": "https://saved.test/v1", "model": "saved", "api_key": "saved-key"}
            ]
        }
        with patch.dict(os.environ, {"LLM_API_KEY": "env-key"}, clear=False):
            result = app.llm_fallback_credentials(saved)
        self.assertEqual(result["source"], "Saved")
        self.assertEqual(result["model"], "saved")

    def test_chat_completions_url_accepts_base_or_full_endpoint(self):
        self.assertEqual(app.chat_completions_url("https://example.test/v1"), "https://example.test/v1/chat/completions")
        self.assertEqual(app.chat_completions_url("https://example.test/v1/chat/completions"), "https://example.test/v1/chat/completions")

    def test_llm_url_rejects_embedded_credentials_and_fragments(self):
        self.assertFalse(app.valid_http_url("https://user:pass@example.test/v1"))
        self.assertFalse(app.valid_http_url("https://example.test/v1#secret"))
        self.assertFalse(app.valid_http_url("https://example.test/v1?api_key=secret"))
        self.assertFalse(app.valid_http_url("https://example.test/v1?x=1"))
        self.assertEqual(app.chat_completions_url("https://example.test/v1#secret"), "")
        self.assertEqual(app.chat_completions_url("https://example.test/v1?x=1"), "")

    def test_research_url_allows_query_strings_but_rejects_local_targets(self):
        self.assertTrue(app.valid_research_url("https://example.test/search?q=workbench&page=2"))
        self.assertTrue(app.valid_research_url("https://example.test/article#evidence"))
        self.assertFalse(app.valid_research_url("http://127.0.0.1:8766/gemini/status"))
        self.assertFalse(app.valid_research_url("http://10.0.0.8/private"))
        self.assertFalse(app.valid_research_url("http://[::1]/private"))
        self.assertFalse(app.valid_research_url("https://user:pass@example.test/article?q=1"))

    def test_research_plan_accepts_query_url_and_rejects_private_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_file = Path(temp_dir) / "workbench.db"
            with patch.object(app, "DATABASE_FILE", database_file), patch.object(app, "_DB_SCHEMA_READY", False):
                result = app.create_research_plan(
                        app.ResearchPlanRequest(
                            title="带参数的研究",
                            urls=["https://example.test/search?q=workbench&page=2"],
                        )
                    )
                self.assertEqual(result["plan"]["urls"], ["https://example.test/search?q=workbench&page=2"])
                with self.assertRaises(HTTPException) as context:
                    app.create_research_plan(
                            app.ResearchPlanRequest(title="私网研究", urls=["http://127.0.0.1:8766/status"])
                        )
        self.assertEqual(context.exception.status_code, 400)

    def test_llm_endpoint_does_not_append_path_after_query(self):
        self.assertEqual(app.chat_completions_url("https://example.test/v1?x=1"), "")

    def test_llm_settings_exposes_resolved_endpoint_and_policy(self):
        saved = {
            "providers": [
                {"name": "完整端点", "role": "fallback", "base_url": "https://example.test/v1/chat/completions/", "model": "m", "api_key": "key"}
            ]
        }
        with patch("app.load_saved_llm_settings", return_value=saved):
            settings = app.llm_settings()
        self.assertEqual(settings["providers"][0]["endpoint"], "https://example.test/v1/chat/completions")
        self.assertIn("Chat Completions", settings["endpoint_policy"])

    def test_llm_settings_separates_saved_routable_and_formally_successful(self):
        saved = {
            "providers": [
                {"name": "可调用", "role": "primary", "base_url": "https://example.test/v1", "model": "m", "api_key": "key"},
                {"name": "待补全", "role": "fallback", "base_url": "", "model": "", "api_key": ""},
            ]
        }
        with patch("app.load_saved_llm_settings", return_value=saved), patch("app._llm_health", return_value={"status": "unknown"}):
            settings = app.llm_settings()
        self.assertEqual(settings["saved_count"], 2)
        self.assertEqual(settings["routable_count"], 1)
        self.assertEqual(settings["formal_success_count"], 0)

    def test_environment_fallback_is_a_real_routing_candidate(self):
        saved = {
            "providers": [
                {"name": "Primary", "role": "primary", "base_url": "https://primary.test/v1", "model": "primary", "api_key": "primary-key"}
            ]
        }
        with patch("app.load_saved_llm_settings", return_value=saved), patch.dict(
            os.environ,
            {"LLM_API_KEY": "env-key", "LLM_BASE_URL": "https://env.test/v1", "LLM_MODEL": "env"},
            clear=False,
        ):
            state = app.llm_provider_state()
        self.assertEqual([item["name"] for item in state["candidates"]], ["Primary", "环境变量 fallback"])
        self.assertTrue(state["candidates"][-1]["api_key"])

    def test_incomplete_saved_provider_is_kept_but_not_routable(self):
        saved = {
            "providers": [
                {"name": "待补全", "role": "fallback", "base_url": "", "model": "", "api_key": ""},
                {"name": "可用", "role": "fallback", "base_url": "https://ok.test/v1", "model": "ok", "api_key": "key"},
            ]
        }
        with patch("app.load_saved_llm_settings", return_value=saved):
            state = app.llm_provider_state()
            settings = app.llm_settings()
        self.assertEqual([item["name"] for item in state["providers"]], ["待补全", "可用"])
        self.assertEqual([item["name"] for item in state["candidates"]], ["可用"])
        incomplete = next(item for item in settings["providers"] if item["name"] == "待补全")
        self.assertFalse(incomplete["usable"])
        self.assertEqual(incomplete["disabled_reason"], "未保存 API Key")
        self.assertFalse(settings["primary_configured"])

    def test_invalid_saved_provider_is_not_routable(self):
        saved = {
            "providers": [
                {"name": "坏地址", "role": "primary", "base_url": "not-a-url", "model": "m", "api_key": "key"},
            ]
        }
        with patch("app.load_saved_llm_settings", return_value=saved):
            state = app.llm_provider_state()
        self.assertEqual(state["candidates"], [])

    def test_incomplete_keyed_fallback_does_not_shadow_environment_fallback(self):
        saved = {
            "providers": [
                {"name": "缺地址", "role": "fallback", "base_url": "", "model": "m", "api_key": "saved-key"},
            ]
        }
        with patch("app.load_saved_llm_settings", return_value=saved), patch.dict(
            os.environ,
            {"LLM_API_KEY": "env-key", "LLM_BASE_URL": "https://env.test/v1", "LLM_MODEL": "env"},
            clear=False,
        ):
            state = app.llm_provider_state()
            settings = app.llm_settings()
        self.assertEqual([item["name"] for item in state["candidates"]], ["环境变量 fallback"])
        self.assertEqual(settings["fallback_source"], "环境变量 fallback")
        incomplete = next(item for item in settings["providers"] if item["name"] == "缺地址")
        self.assertFalse(incomplete["usable"])
        self.assertEqual(incomplete["disabled_reason"], "缺少 API 地址")

    def test_invalid_environment_fallback_is_not_routable(self):
        saved = {"providers": []}
        with patch("app.load_saved_llm_settings", return_value=saved), patch.dict(
            os.environ,
            {"LLM_API_KEY": "env-key", "LLM_BASE_URL": "not-a-url", "LLM_MODEL": "env"},
            clear=False,
        ):
            state = app.llm_provider_state()
        self.assertEqual(state["candidates"], [])

    def test_testing_new_provider_id_does_not_test_primary_instead(self):
        saved = {
            "providers": [
                {"name": "主配置", "id": "primary", "role": "primary", "base_url": "https://primary.test/v1", "model": "primary", "api_key": "key"},
            ]
        }
        with patch("app.load_saved_llm_settings", return_value=saved):
            with self.assertRaisesRegex(HTTPException, "API 地址不能为空"):
                asyncio.run(app.test_llm_settings(app.LLMTestRequest(provider_id="new-provider", name="新条目")))

    def test_testing_invalid_new_url_fails_before_saved_provider_fallback(self):
        saved = {
            "providers": [
                {"name": "已保存", "id": "saved", "role": "primary", "base_url": "https://saved.test/v1", "model": "saved", "api_key": "key"},
            ]
        }
        with patch("app.load_saved_llm_settings", return_value=saved):
            with self.assertRaisesRegex(HTTPException, "查询参数"):
                asyncio.run(app.test_llm_settings(app.LLMTestRequest(
                    provider_id="saved", name="已保存", base_url="https://new.test/v1?api_key=oops", model="new", api_key="key"
                )))

    @staticmethod
    def _status_error(status: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "https://example.test/v1/chat/completions")
        response = httpx.Response(status, request=request)
        return httpx.HTTPStatusError("upstream error", request=request, response=response)

    def test_auth_error_does_not_try_fallback(self):
        primary = {"id": "primary", "name": "主配置", "role": "primary", "base_url": "https://primary.test/v1", "model": "m", "api_key": "k"}
        fallback = {"id": "fallback", "name": "备用", "role": "fallback", "base_url": "https://fallback.test/v1", "model": "m", "api_key": "k"}
        calls = []
        class FailingClient:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def post(self, *args, **kwargs):
                calls.append(kwargs["json"]["model"])
                raise LlmConfigTests._status_error(401)
        with patch("app.llm_provider_state", return_value={"candidates": [primary, fallback]}), patch.object(app, "_llm_health", return_value={"status": "unknown"}), patch("app.httpx.AsyncClient", FailingClient), patch("app.record_llm_usage_event"), patch("app._record_llm_failure"), patch("app._record_llm_success"), self.assertRaisesRegex(RuntimeError, "auth"):
            asyncio.run(app.call_llm([{"role": "user", "content": "ping"}], purpose="test", track_health=False))
        self.assertEqual(len(calls), 1)

    def test_timeout_continues_to_fallback(self):
        primary = {"id": "primary", "name": "主配置", "role": "primary", "base_url": "https://primary.test/v1", "model": "m", "api_key": "k"}
        fallback = {"id": "fallback", "name": "备用", "role": "fallback", "base_url": "https://fallback.test/v1", "model": "m", "api_key": "k"}
        calls = []
        class Client:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def post(self, url, **kwargs):
                calls.append(url)
                if len(calls) == 1:
                    raise httpx.ReadTimeout("timeout")
                return type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"choices": [{"message": {"content": "备用成功"}}]}})()
        with patch("app.llm_provider_state", return_value={"candidates": [primary, fallback]}), patch("app._llm_health", return_value={"status": "unknown"}), patch("app.httpx.AsyncClient", Client), patch("app.record_llm_usage_event"), patch("app._record_llm_failure"), patch("app._record_llm_success"):
            self.assertEqual(asyncio.run(app.call_llm([{"role": "user", "content": "ping"}], track_health=False)), "备用成功")
        self.assertEqual(len(calls), 2)

    def test_rate_limit_and_upstream_errors_continue_to_fallback(self):
        for status in (429, 503):
            primary = {"id": "primary", "name": "主配置", "role": "primary", "base_url": "https://primary.test/v1", "model": "m", "api_key": "k"}
            fallback = {"id": "fallback", "name": "备用", "role": "fallback", "base_url": "https://fallback.test/v1", "model": "m", "api_key": "k"}
            calls = []
            class Client:
                def __init__(self, *args, **kwargs): pass
                async def __aenter__(self): return self
                async def __aexit__(self, *args): return False
                async def post(self, url, **kwargs):
                    calls.append(url)
                    if len(calls) == 1:
                        raise LlmConfigTests._status_error(status)
                    return type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"choices": [{"message": {"content": "备用成功"}}]}})()
            with patch("app.llm_provider_state", return_value={"candidates": [primary, fallback]}), patch("app._llm_health", return_value={"status": "unknown"}), patch("app.httpx.AsyncClient", Client), patch("app.record_llm_usage_event"), patch("app._record_llm_failure"), patch("app._record_llm_success"):
                self.assertEqual(asyncio.run(app.call_llm([{"role": "user", "content": "ping"}], track_health=False)), "备用成功")
            self.assertEqual(len(calls), 2)

    def test_connection_test_does_not_change_provider_health(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "连接成功"}}]}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, *args, **kwargs):
                return FakeResponse()

        credentials = {
            "id": "test-provider",
            "name": "测试 Provider",
            "base_url": "https://example.test/v1",
            "model": "test-model",
            "api_key": "test-key",
        }
        with patch("app.httpx.AsyncClient", FakeClient), patch("app._record_llm_success") as success, patch(
            "app._record_llm_failure"
        ) as failure:
            answer = asyncio.run(
                app.call_llm(
                    [{"role": "user", "content": "ping"}],
                    credentials,
                    purpose="test",
                    track_health=False,
                )
            )
        self.assertEqual(answer, "连接成功")
        success.assert_not_called()
        failure.assert_not_called()

    def test_effective_candidate_follows_fallback_when_primary_is_cooling(self):
        state = {
            "candidates": [
                {"id": "primary", "name": "主配置", "role": "primary"},
                {"id": "fallback", "name": "备用配置", "role": "fallback"},
            ],
            "fallback": {},
        }
        def fake_health(provider):
            return {"status": "cooling" if provider["id"] == "primary" else "healthy"}
        with patch("app._llm_health", side_effect=fake_health):
            provider, route = app.llm_effective_candidate(state)
        self.assertEqual(provider["id"], "fallback")
        self.assertEqual(route, "fallback")

    def test_agent_result_contract_contains_traceable_ids_and_replay(self):
        contract = app.agent_result_contract(
            "knowledge",
            "结论：已整理\n\n下一步：人工确认写入",
            source_refs=[{"type": "artifact", "id": 12, "title": "来源笔记", "path": "knowledge-base/source.md", "updated_at": "2026-08-08T01:02:03Z"}],
            artifact_ids=[12],
            work_item_ids=[34],
            relation_ids=[56],
            data_as_of="2026-08-08T01:02:03Z",
            run_id="run-123",
            session_id="session-123",
        )
        self.assertEqual(contract["schema_version"], "1.1")
        self.assertEqual(contract["artifact_ids"], ["12"])
        self.assertEqual(contract["work_item_ids"], ["34"])
        self.assertEqual(contract["relation_ids"], ["56"])
        self.assertEqual(contract["data_as_of"], "2026-08-08T01:02:03Z")
        self.assertEqual(contract["replay"]["href"], "/api/agent/knowledge/runs/run-123")
        self.assertEqual(contract["source_refs"][0]["type"], "artifact")

    def test_agent_result_contract_preserves_execution_plan_trace(self):
        execution_plan = {
            "kind": "dispatch",
            "targets": ["knowledge", "doc-factory"],
            "requested_tools": ["knowledge_search"],
            "route_mode": "capability_graph",
            "route_confidence": 0.82,
            "needs_confirmation": False,
            "child_run_ids": ["child-1", "child-2"],
            "status": "succeeded",
        }
        contract = app.agent_result_contract(
            "workbench",
            "结论：已完成跨项目汇总",
            run_id="parent-1",
            execution_plan=execution_plan,
        )
        self.assertEqual(contract["execution_plan"], execution_plan)
        self.assertEqual(contract["execution_plan"]["child_run_ids"], ["child-1", "child-2"])

        empty_plan_contract = app.agent_result_contract("workbench", "没有计划")
        self.assertEqual(empty_plan_contract["execution_plan"], {})

    def test_agent_execution_plan_rejects_undeclared_tools_and_records_boundary(self):
        with patch.object(app, "agent_detail", return_value={"tools": ["knowledge.search"]}), patch.object(
            app, "llm_settings", return_value={"configured": True}
        ):
            plan = app.build_agent_execution_plan(
                "knowledge",
                "查找行情相关笔记",
                intent="检索并核对已有知识",
                requested_tools=["knowledge.search", "server.restart"],
                route={"mode": "explicit", "confidence": 0.96},
                status="succeeded",
            )
        self.assertEqual(plan["intent"], "检索并核对已有知识")
        self.assertEqual(plan["rejected_tools"], ["server.restart"])
        self.assertEqual(plan["tool_plan"][0]["status"], "declared")
        self.assertEqual(plan["tool_plan"][1]["status"], "rejected")
        self.assertEqual(plan["steps"][0]["status"], "completed")

    def test_agent_tool_boundary_is_fail_closed_across_dispatch_targets(self):
        def fake_detail(project_id, **_kwargs):
            return {"tools": {"knowledge": ["knowledge_search"], "doc-factory": ["document_validate"]}.get(project_id, [])}

        with patch.object(app, "agent_detail", side_effect=fake_detail), patch.object(
            app, "llm_settings", return_value={"configured": True}
        ):
            boundary = app.validate_agent_tool_requests(
                ["knowledge", "doc-factory"],
                ["knowledge_search", "document_validate", "server_restart"],
            )
        self.assertFalse(boundary["valid"])
        self.assertEqual(boundary["accepted"], ["knowledge_search", "document_validate"])
        self.assertEqual(boundary["rejected"], ["server_restart"])

    def test_agent_tool_boundary_accepts_empty_request_without_inventing_tools(self):
        with patch.object(app, "agent_detail", return_value={"tools": ["knowledge_search"]}), patch.object(
            app, "llm_settings", return_value={"configured": True}
        ):
            boundary = app.validate_agent_tool_requests(["knowledge"], [])
        self.assertTrue(boundary["valid"])
        self.assertEqual(boundary["requested"], [])
        self.assertEqual(boundary["rejected"], [])

    def test_agent_result_contract_exposes_intent_and_tool_plan(self):
        plan = {
            "kind": "agent_task",
            "intent": "核对来源",
            "tool_plan": [{"id": "knowledge.search", "status": "declared"}],
            "status": "succeeded",
        }
        contract = app.agent_result_contract("knowledge", "结论：已核对", execution_plan=plan)
        self.assertEqual(contract["intent"], "核对来源")
        self.assertEqual(contract["tool_plan"][0]["id"], "knowledge.search")

    def test_agent_result_contract_marks_unbound_answer_for_review(self):
        contract = app.agent_result_contract("inbox", "结论：需要先补充事实")
        self.assertTrue(contract["needs_review"])
        self.assertEqual(contract["freshness"]["status"], "missing")
        self.assertIn("没有绑定可回溯来源", contract["review_reasons"])

    def test_agent_context_metadata_keeps_project_artifacts_and_work_items(self):
        metadata = app.agent_context_result_metadata(
            {
                "project_context": {"source": "SQLite snapshot", "fetched_at": "2026-08-08T02:00:00Z"},
                "shared_context": {
                    "recent_artifacts": [{"id": 7, "name": "snapshot.json", "updated_at": "2026-08-08T02:00:00Z"}],
                    "open_work_items": [{"id": 8, "title": "需要复核", "updated_at": "2026-08-08T01:00:00Z"}],
                },
            }
        )
        self.assertEqual(metadata["artifact_ids"], ["7"])
        self.assertEqual(metadata["work_item_ids"], ["8"])
        self.assertEqual(metadata["data_as_of"], "2026-08-08T02:00:00Z")

    def test_evidence_bundle_does_not_claim_missing_artifacts_are_sources(self):
        bundle = app.evidence_bundle_payload([999999], "验证来源")
        self.assertEqual(bundle["sources"], [])
        self.assertEqual(bundle["missing"][0]["artifact_id"], 999999)
        self.assertEqual(bundle["coverage"]["available"], 0)

    def test_opportunity_score_is_explainable_and_bounded(self):
        result = app.opportunity_score(
            {"link": "https://example.test", "description": "摘要", "business_opportunity": "机会", "published_at": "2026-08-08"},
            {"vote": "useful"},
        )
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["level"], "高")
        self.assertIn("有可回溯来源", result["factors"])


if __name__ == "__main__":
    unittest.main()
