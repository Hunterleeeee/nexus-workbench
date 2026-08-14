"""Workbench Agent 路由层：项目 Agent 会话/聊天/工作项/运行/动作/队列路由。

从 app.py 拆出的 agent 路由层（为开源准备）。数据层从 agent_runs 直连；执行引擎
（stream/run_project_agent/ReAct 循环/工具 handler）仍在 app.py，经 _app_call 转发；
CrawlRequest/AgentProxyRequest 等模型随路由拆入（FastAPI 注册时解析注解）。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent_platform import (
    AGENT_REGISTRY,
    AGENT_RESULT_CONTRACT_VERSION,
    AGENT_RUN_STATUS_LABELS,
    AGENT_TOOL_POLICIES,
    AgentDispatchRequest,
    _contract_id_list,
    _contract_source_refs,
    agent_result_contract,
    build_agent_execution_plan,
    call_llm_with_tools,
    dispatch_agent_task,
    runtime_tool_policy,
    subagent_tool_schemas,
    validate_agent_tool_requests,
)
from .agent_runs import (
    add_agent_message,
    add_agent_run_event,
    agent_quality_metrics,
    agent_run_summary,
    create_agent_action_record,
    create_agent_run_record,
    create_agent_session,
    get_agent_action_record,
    get_agent_run,
    get_agent_session,
    list_agent_messages,
    list_agent_runs,
    list_agent_sessions,
    update_agent_action_record,
    update_agent_run_record,
)
from .core import (
    KNOWLEDGE_DIR,
    MARKET_SNAPSHOT_FILE,
    MAX_CONVERSATION_MESSAGES,
    OUTPUTS_DIR,
    SERVER_MONITOR_SNAPSHOT_FILE,
    SUB2API_SNAPSHOT_FILE,
    clip,
    clip_for_llm,
    log,
    now_iso,
)
import functools
import httpx
from .inbox import create_inbox_record, list_inbox
from .instance import app
from .market import (
    add_market_symbol_to_watchlist,
    analyze_market_snapshot,
    evaluate_market_observations,
    list_market_history,
    load_market_snapshot,
    normalize_market_symbol,
    record_market_snapshot,
)
from .notifications import create_notification_record
from .server import (
    DEFAULT_SERVER_THRESHOLDS,
    analyze_server_snapshot,
    list_server_monitor_history,
    load_server_monitor_snapshot,
    save_server_monitor_thresholds,
    server_monitor_config,
)
from .sub2api import analyze_sub2api_snapshot, list_sub2api_history, load_sub2api_snapshot
from .memories import learn_memories_from_message, memory_context_for_llm
from .projects import (
    agent_display_name,
    load_projects,
    project_href,
    project_link_summary,
)



def _REACT_TOOLS() -> dict[str, dict[str, Any]]:
    """运行时读 app.REACT_TOOLS（工具注册表粘合层仍在 app.py）。"""
    import app as _app

    return _app.REACT_TOOLS


def _SUBAGENT_EXTRA_TOOLS() -> dict[str, dict[str, Any]]:
    import app as _app

    return getattr(_app, "_SUBAGENT_EXTRA_TOOLS()", {})


def _AGENT_MAX_TOOL_ROUNDS() -> int:
    import app as _app

    return int(getattr(_app, "_AGENT_MAX_TOOL_ROUNDS()", 4))


def _AGENT_MAX_PARALLEL_TOOL_CALLS() -> int:
    import app as _app

    return int(getattr(_app, "_AGENT_MAX_PARALLEL_TOOL_CALLS()", 4))


def _AGENT_TOOL_TIMEOUT_SECONDS() -> int:
    import app as _app

    return int(getattr(_app, "_AGENT_TOOL_TIMEOUT_SECONDS()", 60))


def _REACT_TOOL_LABELS() -> dict[str, str]:
    import app as _app

    return getattr(_app, "REACT_TOOL_LABELS", {})


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的引擎/领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def _RUNS() -> dict[str, Any]:
    """运行时读 app.runs（内存运行字典仍在 app.py）。"""
    import app as _app

    return _app.runs


class CrawlRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)
    task: str = ""
    # Optional context supplied by the browser bookmarklet.  It is deliberately
    # separate from ``task`` so the UI can show what the user selected and the
    # research Agent can distinguish user-provided context from crawled facts.
    source_title: str = Field(default="", max_length=300)
    source_context: str = Field(default="", max_length=12_000)
    render_js: bool = True
    refresh: bool = False
    max_depth: int = Field(default=1, ge=1, le=3)
    max_pages: int = Field(default=5, ge=1, le=50)


class AgentProxyRequest(BaseModel):
    messages: list[dict[str, str]] = Field(default_factory=list, max_length=30)
    model: str = Field(default="", max_length=200)
    stream: bool = False


class ProjectAgentChatRequest(BaseModel):
    session_id: str = Field(default="", max_length=80)
    message: str = Field(min_length=1, max_length=8_000)
    context: dict[str, Any] = Field(default_factory=dict)
    stream: bool = Field(default=False, description="true 时返回 SSE 流式输出")


class WorkItemTakeoverRequest(BaseModel):
    operator: str = Field(default="本机用户", min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=2_000)


def require_project_agent(project_id: str) -> None:
    if project_id not in AGENT_REGISTRY or project_id == "workbench":
        raise HTTPException(404, "项目 Agent 不存在")


@app.get("/api/agent/{project_id}/sessions")
def get_project_agent_sessions(project_id: str, limit: int = 20) -> dict[str, Any]:
    if project_id != "workbench":
        require_project_agent(project_id)
    sessions = list_agent_sessions(project_id, 100 if project_id == "workbench" else limit)
    if project_id == "workbench":
        sessions = [item for item in sessions if not str(item.get("id") or "").startswith("feishu:")][: max(1, min(limit, 100))]
    return {"sessions": sessions, "agent": _app_call("agent_detail", project_id, llm_ready=bool(_app_call("llm_settings", )["configured"]))}


@app.get("/api/agent/{project_id}/sessions/{session_id}")
def get_project_agent_session(project_id: str, session_id: str) -> dict[str, Any]:
    if project_id != "workbench":
        require_project_agent(project_id)
    session = get_agent_session(session_id, project_id)
    if not session:
        raise HTTPException(404, "项目 Agent 会话不存在")
    return {"session": session, "messages": list_agent_messages(session_id), "agent": _app_call("agent_detail", project_id, llm_ready=bool(_app_call("llm_settings", )["configured"]))}


@app.post("/api/agent/{project_id}/chat")
async def chat_project_agent(project_id: str, request: ProjectAgentChatRequest) -> dict[str, Any]:
    require_project_agent(project_id)
    if not _app_call("llm_settings", )["configured"]:
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM")
    session = get_agent_session(request.session_id, project_id) if request.session_id else None
    if request.session_id and not session:
        raise HTTPException(404, "项目 Agent 会话不存在")
    if not session:
        session = await asyncio.to_thread(create_agent_session, project_id, request.message)
    await asyncio.to_thread(add_agent_message, session["id"], "user", request.message, {"source": "project_agent", "context": _app_call("redact_agent_context", request.context)})
    run = await asyncio.to_thread(create_agent_run_record, 
        project_id=project_id,
        session_id=session["id"],
        kind="chat",
        title=clip(request.message, 120),
        request={"session_id": session["id"], "message": request.message, "context": _app_call("redact_agent_context", request.context)},
        max_attempts=2,
    )
    if request.stream:
        async def event_gen():
            try:
                async for chunk in _app_call("stream_project_agent", 
                    project_id=project_id,
                    session=session,
                    run=run,
                    message=request.message,
                    context=request.context,
                ):
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': clip(str(exc), 300), 'provider': ''}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    return await _app_call("run_project_agent", 
        project_id=project_id,
        session=session,
        run=run,
        message=request.message,
        context=request.context,
    )


@app.get("/api/agent/{project_id}/work-items")
def get_project_agent_work_items(project_id: str, status: str = "open", limit: int = 20) -> dict[str, Any]:
    require_project_agent(project_id)
    if status not in {"all", "open", "running", "blocked", "done", "failed", "archived"}:
        raise HTTPException(400, "不支持的工作项状态")
    items = _app_call("list_work_items", status=status, project_id=project_id)
    items = [item for item in items if project_id in {part.strip() for part in str(item.get("target_project", "")).split(",") if part.strip()}]
    return {"items": items[: max(1, min(limit, 100))], "project_id": project_id, "agent": _app_call("agent_detail", project_id, llm_ready=bool(_app_call("llm_settings", )["configured"]))}


@app.post("/api/agent/{project_id}/work-items/{item_id}/takeover")
def takeover_project_work_item(project_id: str, item_id: int, request: WorkItemTakeoverRequest) -> dict[str, Any]:
    """Pause automation and register an explicit operator takeover object chain."""
    require_project_agent(project_id)
    item = _app_call("get_work_item_record", item_id)
    target_projects = {part.strip() for part in str(item.get("target_project", "")).split(",") if part.strip()} if item else set()
    if not item or project_id not in target_projects:
        raise HTTPException(404, "这个项目没有对应的交接工作项")
    if item.get("status") in {"done", "archived"}:
        raise HTTPException(409, "已完成或已归档的工作项不能接管，请新建后续工作项")

    operator = request.operator.strip()
    note = request.note.strip()
    timestamp = now_iso()
    previous_status = str(item.get("status") or "open")
    previous_run_id = str(item.get("claimed_run_id") or "")
    run = create_agent_run_record(
        project_id=project_id,
        parent_run_id=previous_run_id,
        kind="manual_takeover",
        title=f"人工接管：{clip(item.get('title', '交接工作项'), 180)}",
        request={
            "work_item_id": item_id,
            "source_project": item.get("source_project", ""),
            "operator": operator,
            "note": note,
            "previous_status": previous_status,
            "previous_run_id": previous_run_id,
        },
        max_attempts=1,
    )
    takeover = {
        "operator": operator,
        "note": note,
        "taken_over_at": timestamp,
        "previous_status": previous_status,
        "previous_run_id": previous_run_id,
        "run_id": run["id"],
    }
    metadata = dict(item.get("metadata") or {})
    history = list(metadata.get("manual_takeovers") or []) if isinstance(metadata.get("manual_takeovers"), list) else []
    metadata["manual_takeovers"] = [*history[-19:], takeover]
    result = {"manual_takeover": takeover, "status": "blocked", "next_step": "由接管人处理，完成后再显式更新工作项。"}
    updated_item = _app_call("update_work_item_record", 
        item_id,
        {
            "status": "blocked",
            "claimed_at": timestamp,
            "claimed_run_id": run["id"],
            "result_json": json.dumps(result, ensure_ascii=False),
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
            "completed_at": "",
            "last_error": f"已由 {operator} 人工接管：{note}",
        },
    ) or item
    updated_run = update_agent_run_record(run["id"], status="succeeded", result=result, error="") or run
    _app_call("add_agent_run_event", 
        run["id"],
        "manual_takeover",
        f"{operator} 已人工接管工作项，自动执行暂停。",
        level="warning",
        metadata={"work_item_id": item_id, "previous_status": previous_status, "note": note},
    )
    relation = _app_call("create_relation_record", 
        from_type="work_item",
        from_id=str(item_id),
        to_type="agent_run",
        to_id=run["id"],
        relation_type="manual_takeover",
        metadata={"project_id": project_id, **takeover},
    )
    notification = create_notification_record(
        title=f"{agent_display_name(project_id)}工作项已人工接管",
        body=f"{item.get('title', '交接工作项')} · {operator}：{note}",
        project_id=project_id,
        kind="manual_takeover",
        level="warning",
        href=project_href(project_id),
        event_key=f"work-item-takeover:{item_id}:{run['id']}",
        dedupe_seconds=0,
    )
    return {
        "ok": True,
        "work_item": updated_item,
        "run": updated_run,
        "relation": relation,
        "notification": notification,
        "takeover": takeover,
    }


@app.post("/api/agent/{project_id}/work-items/{item_id}/run")
async def run_project_work_item(project_id: str, item_id: int) -> dict[str, Any]:
    require_project_agent(project_id)
    if not _app_call("llm_settings", )["configured"]:
        raise HTTPException(503, "请先在工作台配置全局 LLM，目标 Agent 才能执行交接任务")
    item = await asyncio.to_thread(get_work_item_record, item_id)
    target_projects = {part.strip() for part in str(item.get("target_project", "")).split(",") if part.strip()} if item else set()
    if not item or project_id not in target_projects:
        raise HTTPException(404, "这个项目没有对应的交接工作项")
    if item.get("status") == "running":
        raise HTTPException(409, "这个工作项已经在运行中")
    if item.get("status") in {"done", "archived"}:
        raise HTTPException(409, "这个工作项已经完成")
    if project_id in {"knowledge", "doc-factory"} and item.get("source_project") == "inbox":
        return await _app_call("run_inbox_handoff_work_item", project_id, item)
    session = create_agent_session(project_id, f"交接：{clip(item.get('title', '未命名工作项'), 90)}")
    handoff_message = (
        "这是一个来自其他项目的已确认交接，请把它当作当前项目 Agent 的正式任务处理。\n\n"
        f"来源 Agent：{item.get('source_agent_name', item.get('source_project', '工作台'))}\n"
        f"工作项：{item.get('title', '未命名工作项')}\n"
        f"任务内容：\n{item.get('description', '')}\n\n"
        "请基于本项目自己的上下文回答：已知事实、执行/分析结果、证据或数据时间、风险、下一步。"
    )
    await asyncio.to_thread(add_agent_message, session["id"], "user", handoff_message, {"source": "work_item", "work_item_id": item_id, "source_project": item.get("source_project", "")})
    run = await asyncio.to_thread(create_agent_run_record, 
        project_id=project_id,
        session_id=session["id"],
        parent_run_id=str(item.get("claimed_run_id") or ""),
        kind="handoff",
        title=clip(item.get("title", "交接工作项"), 240),
        request={"work_item_id": item_id, "source_project": item.get("source_project", ""), "message": handoff_message},
        max_attempts=2,
        attempt=2 if item.get("claimed_run_id") else 1,
    )
    claimed_at = now_iso()
    await asyncio.to_thread(update_work_item_record, item_id, {"status": "running", "claimed_at": claimed_at, "claimed_run_id": run["id"], "last_error": ""})
    try:
        result = await _app_call("run_project_agent", project_id=project_id, session=session, run=run, message=handoff_message, context={"source": "work_item", "work_item_id": item_id})
        actions = result.get("actions") or []
        run_status = result.get("run", {}).get("status")
        has_pending = any(action.get("status") == "pending" for action in actions)
        item_status = "blocked" if has_pending else "done" if run_status == "succeeded" else "failed"
        error = "有动作需要人工确认" if has_pending else "" if item_status == "done" else result.get("run", {}).get("error", "目标 Agent 执行失败")
        updated_item = _app_call("update_work_item_record", 
            item_id,
            {
                "status": item_status,
                "result_json": json.dumps({"agent_run_id": run["id"], "answer": result.get("message", {}).get("content", ""), "actions": actions}, ensure_ascii=False),
                "completed_at": now_iso() if item_status in {"done", "blocked", "failed"} else "",
                "last_error": error,
            },
        ) or item
        relation = await asyncio.to_thread(create_relation_record, 
            from_type="work_item",
            from_id=str(item_id),
            to_type="agent_run",
            to_id=run["id"],
            relation_type="processed_by",
            metadata={"project_id": project_id, "status": item_status},
        )
        try:
            await asyncio.to_thread(create_notification_record, 
                title=f"{agent_display_name(project_id)}已处理交接",
                body=f"{item.get('title', '交接工作项')} · {('等待人工确认' if item_status == 'blocked' else '已完成' if item_status == 'done' else '执行失败')}",
                project_id=project_id,
                kind="handoff",
                level="warning" if item_status in {"blocked", "failed"} else "success",
                href=project_href(project_id),
                event_key=f"work-item-run:{item_id}:{run['id']}",
                dedupe_seconds=0,
            )
        except Exception:
            log.debug("忽略异常（run_project_work_item）", exc_info=True)
        return {**result, "work_item": updated_item, "relation": relation}
    except HTTPException as exc:
        error = str(exc.detail)
        await asyncio.to_thread(update_work_item_record, item_id, {"status": "failed", "completed_at": now_iso(), "last_error": error})
        try:
            await asyncio.to_thread(create_relation_record, 
                from_type="work_item",
                from_id=str(item_id),
                to_type="agent_run",
                to_id=run["id"],
                relation_type="processed_by",
                metadata={"project_id": project_id, "status": "failed", "error": error},
            )
            await asyncio.to_thread(create_notification_record, 
                title=f"{agent_display_name(project_id)}处理交接失败",
                body=f"{item.get('title', '交接工作项')} · {error}",
                project_id=project_id,
                kind="handoff",
                level="error",
                href=project_href(project_id),
                event_key=f"work-item-run:{item_id}:{run['id']}",
                dedupe_seconds=0,
            )
        except Exception:
            log.debug("忽略异常（run_project_work_item）", exc_info=True)
        raise
    except Exception as exc:
        error = str(exc)
        await asyncio.to_thread(update_work_item_record, item_id, {"status": "failed", "completed_at": now_iso(), "last_error": error})
        try:
            await asyncio.to_thread(create_relation_record, 
                from_type="work_item",
                from_id=str(item_id),
                to_type="agent_run",
                to_id=run["id"],
                relation_type="processed_by",
                metadata={"project_id": project_id, "status": "failed", "error": error},
            )
            await asyncio.to_thread(create_notification_record, 
                title=f"{agent_display_name(project_id)}处理交接失败",
                body=f"{item.get('title', '交接工作项')} · {error}",
                project_id=project_id,
                kind="handoff",
                level="error",
                href=project_href(project_id),
                event_key=f"work-item-run:{item_id}:{run['id']}",
                dedupe_seconds=0,
            )
        except Exception:
            log.debug("忽略异常（run_project_work_item）", exc_info=True)
        raise HTTPException(502, f"交接工作项执行失败：{exc}") from exc


@app.post("/api/agent/dispatch")
async def dispatch_agent(request: AgentDispatchRequest) -> dict[str, Any]:
    return await dispatch_agent_task(request)


@app.get("/api/agent/{project_id}/runs")
def get_project_agent_runs(project_id: str, session_id: str = "", limit: int = 20) -> dict[str, Any]:
    if project_id != "workbench":
        require_project_agent(project_id)
    return {"runs": list_agent_runs(project_id, session_id=session_id, limit=limit), "summary": agent_run_summary(project_id)}


@app.get("/api/agent/{project_id}/runs/{run_id}")
def get_project_agent_run(project_id: str, run_id: str) -> dict[str, Any]:
    if project_id != "workbench":
        require_project_agent(project_id)
    run = get_agent_run(run_id)
    if not run or run.get("project_id") != project_id:
        raise HTTPException(404, "Agent 运行记录不存在")
    timeline = agent_run_timeline(run_id) or {"events": [], "actions": [], "relations": [], "result_contract": {}}
    return {"run": run, "events": timeline["events"], "actions": timeline["actions"], "relations": timeline["relations"], "result_contract": timeline["result_contract"], "timeline": timeline}


@app.post("/api/agent/{project_id}/runs/{run_id}/retry")
async def retry_project_agent_run(project_id: str, run_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    if project_id != "workbench":
        require_project_agent(project_id)
    run = await asyncio.to_thread(get_agent_run, run_id)
    if not run or run.get("project_id") != project_id:
        raise HTTPException(404, "Agent 运行记录不存在")
    if run.get("status") != "failed":
        raise HTTPException(409, "只有失败的 Agent 运行可以重试")
    if not run.get("retryable"):
        raise HTTPException(409, "已达到最大重试次数，请检查失败原因后重新发起任务")
    if project_id == "crawl4ai" and run.get("kind") == "crawl":
        request_data = run.get("request") or {}
        try:
            crawl_request = CrawlRequest(**request_data)
        except Exception as exc:
            raise HTTPException(409, f"原始 Crawl4AI 请求已不可用：{exc}") from exc
        durable = await asyncio.to_thread(create_agent_run_record, 
            project_id="crawl4ai",
            parent_run_id=run["id"],
            kind="crawl",
            title=f"重试：{clip(crawl_request.task or (crawl_request.urls[0] if crawl_request.urls else '网页研究'), 100)}",
            request=request_data,
            max_attempts=run.get("max_attempts", 2),
            attempt=run.get("attempt", 1) + 1,
        )
        work_item = await asyncio.to_thread(create_work_item_record, 
            title=f"重试网页研究：{clip(crawl_request.task or (crawl_request.urls[0] if crawl_request.urls else '网页研究'), 100)}",
            description=crawl_request.task.strip() or "重试抓取并整理网页证据",
            kind="research",
            status="running",
            source_project="workbench",
            target_project="crawl4ai",
            metadata={"run_id": durable["id"], "parent_run_id": run["id"]},
        )
        await asyncio.to_thread(create_relation_record, from_type="agent_run", from_id=durable["id"], to_type="work_item", to_id=work_item["id"], relation_type="tracks", metadata={"project_id": "crawl4ai", "parent_run_id": run["id"]})
        # 持久化 work_item_id 到 request_json，独立 Crawl Worker 领取时据此回写工作项状态
        durable["request"]["work_item_id"] = work_item["id"]
        await asyncio.to_thread(update_agent_run_record, durable["id"], request=durable["request"])
        runtime = {
            "id": durable["id"], "status": "queued", "task": crawl_request.task.strip(), "urls": crawl_request.urls,
            "source_title": crawl_request.source_title.strip(), "source_context": crawl_request.source_context.strip(),
            "render_js": crawl_request.render_js, "refresh": crawl_request.refresh, "max_depth": crawl_request.max_depth,
            "max_pages": crawl_request.max_pages, "logs": [], "documents": [], "conversation": [],
            "created_at": now_iso(), "work_item_id": work_item["id"],
        }
        _RUNS()[durable["id"]] = runtime
        return {"ok": True, "run": durable, "run_id": durable["id"], "work_item_id": work_item["id"]}
    if project_id == "workbench" and run.get("kind") == "dispatch":
        request_data = run.get("request") or {}
        if not request_data.get("message"):
            raise HTTPException(409, "原始调度请求已不可用，无法重试")
        # 唯独这个分支漏了 attempt（其他 kind 都传了 attempt+1）。不传的话新 run
        # 恒为 1/2，retryable 永远为 True——重试链没有上限，而每次重试都会重跑
        # 一遍动作推断并生成新的 action id，绕开「已执行就不再执行」的幂等保护。
        return await dispatch_agent_task(
            AgentDispatchRequest(
                message=str(request_data["message"]),
                project_ids=list(request_data.get("project_ids") or []),
                context=request_data.get("context") or {},
            ),
            parent_run_id=run["id"],
            attempt=int(run.get("attempt", 1)) + 1,
            max_attempts=int(run.get("max_attempts", 2)),
        )
    if project_id == "aihot" and run.get("kind") == "chat":
        request_data = run.get("request") or {}
        session = await asyncio.to_thread(get_agent_session, run.get("session_id", ""), "aihot")
        if not session or not request_data.get("message"):
            raise HTTPException(409, "原始 AI 热点会话或用户消息已不可用，无法重试")
        chosen = list(request_data.get("selected_items") or [])
        if not chosen:
            snapshot = await _app_call("fetch_aihot_snapshot", )
            chosen = await asyncio.to_thread(select_aihot_items, snapshot, request_data.get("mode", "useful"), limit=18)
        next_run = await asyncio.to_thread(create_agent_run_record, 
            project_id="aihot",
            session_id=session["id"],
            parent_run_id=run["id"],
            kind="chat",
            title=f"重试：{clip(request_data['message'], 110)}",
            request=request_data,
            max_attempts=run.get("max_attempts", 2),
            attempt=run.get("attempt", 1) + 1,
        )
        return await _app_call("run_aihot_agent_turn", run=next_run, session=session, message=str(request_data["message"]), chosen=chosen)
    if project_id == "crawl4ai" and run.get("kind") == "chat":
        request_data = run.get("request") or {}
        crawl_id = str(request_data.get("crawl_run_id") or run.get("parent_run_id") or "")
        crawl_run = await asyncio.to_thread(load_crawl_runtime, crawl_id)
        if not crawl_run or crawl_run.get("status") != "completed" or not request_data.get("message"):
            raise HTTPException(409, "原始 Crawl4AI 研究结果不可用，无法重试问答")
        next_run = await asyncio.to_thread(create_agent_run_record, 
            project_id="crawl4ai",
            parent_run_id=run["id"],
            kind="chat",
            title=f"重试：{clip(request_data['message'], 110)}",
            request=request_data,
            max_attempts=run.get("max_attempts", 2),
            attempt=run.get("attempt", 1) + 1,
        )
        return await _app_call("run_crawl_chat_turn", durable_run=next_run, crawl_run=crawl_run, message=str(request_data["message"]))
    if project_id == "cid-dashboard" and run.get("kind") == "chat":
        request_data = run.get("request") or {}
        messages = request_data.get("messages") or []
        if not messages:
            raise HTTPException(409, "原始看板 Agent 消息已不可用，无法重试")
        next_run = await asyncio.to_thread(create_agent_run_record, 
            project_id="cid-dashboard",
            parent_run_id=run["id"],
            kind="chat",
            title=f"重试：{clip(str(messages[-1].get('content', '看板分析')), 110)}",
            request={"messages": messages},
            max_attempts=run.get("max_attempts", 2),
            attempt=run.get("attempt", 1) + 1,
        )
        answer, updated = await _app_call("run_cid_agent_turn", run=next_run, messages=messages)
        return {"run": updated, "id": uuid.uuid4().hex, "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]}
    if project_id == "idea-analysis" and run.get("kind") == "idea_plan":
        request_data = run.get("request") or {}
        session_id = str(run.get("session_id") or request_data.get("session_id") or "")
        session = await asyncio.to_thread(get_idea_session, session_id)
        if not session:
            raise HTTPException(409, "原始想法会话已不可用，无法重试验证工作台")
        if not _app_call("list_idea_messages", session_id, limit=1):
            raise HTTPException(409, "原始想法没有对话内容，无法重试验证工作台")
        return await _app_call("generate_idea_validation_plan", 
            session_id,
            trigger="retry",
            parent_run_id=run["id"],
            attempt=run.get("attempt", 1) + 1,
            max_attempts=run.get("max_attempts", 2),
        )
    if project_id == "idea-analysis" and run.get("kind") == "opportunity_validation":
        request_data = run.get("request") or {}
        session = await asyncio.to_thread(get_idea_session, run.get("session_id", ""))
        item = await asyncio.to_thread(get_work_item_record, int(request_data.get("work_item_id") or 0))
        if not session or not item or not request_data.get("message"):
            raise HTTPException(409, "原始机会交接或想法会话已不可用，无法重试")
        next_run = await asyncio.to_thread(create_agent_run_record, 
            project_id="idea-analysis",
            session_id=session["id"],
            parent_run_id=run["id"],
            kind="opportunity_validation",
            title=f"重试：{clip(request_data.get('message', '机会验证'), 110)}",
            request=request_data,
            max_attempts=run.get("max_attempts", 2),
            attempt=run.get("attempt", 1) + 1,
        )
        await asyncio.to_thread(update_work_item_record, item["id"], {"status": "running", "claimed_at": now_iso(), "claimed_run_id": next_run["id"], "last_error": ""})
        try:
            result = await _app_call("run_idea_agent_turn", run=next_run, session=session, message=str(request_data["message"]))
            relation = await asyncio.to_thread(create_relation_record, 
                from_type="work_item", from_id=str(item["id"]), to_type="agent_run", to_id=next_run["id"],
                relation_type="processed_by", metadata={"project_id": "idea-analysis", "kind": "opportunity_validation"},
            )
            updated_item = _app_call("update_work_item_record", 
                item["id"],
                {
                    "status": "done",
                    "result_json": json.dumps({"idea_session_id": session["id"], "agent_run_id": next_run["id"], "answer": result.get("message", {}).get("content", "")}, ensure_ascii=False),
                    "completed_at": now_iso(), "last_error": "",
                },
            ) or item
            notification = await asyncio.to_thread(create_notification_record, 
                title="想法分析重试完成",
                body=f"{clip(item.get('title', '热点机会'), 180)} · 已生成验证计划",
                project_id="idea-analysis", kind="opportunity_result", level="success",
                href="/projects/idea-analysis", event_key=f"idea-opportunity-run:{item['id']}:{next_run['id']}", dedupe_seconds=0,
            )
            return {**result, "ok": True, "work_item": updated_item, "relation": relation, "notification": notification}
        except HTTPException as exc:
            await asyncio.to_thread(update_work_item_record, item["id"], {"status": "failed", "completed_at": now_iso(), "last_error": str(exc.detail)})
            raise
    if project_id == "idea-analysis" and run.get("kind") == "idea_chat":
        request_data = run.get("request") or {}
        session = await asyncio.to_thread(get_idea_session, run.get("session_id", ""))
        if not session or not request_data.get("message"):
            raise HTTPException(409, "原始想法会话或用户消息已不可用，无法重试")
        next_run = await asyncio.to_thread(create_agent_run_record, 
            project_id="idea-analysis",
            session_id=session["id"],
            parent_run_id=run["id"],
            kind="idea_chat",
            title=f"重试：{clip(request_data['message'], 110)}",
            request=request_data,
            max_attempts=run.get("max_attempts", 2),
            attempt=run.get("attempt", 1) + 1,
        )
        return await _app_call("run_idea_agent_turn", run=next_run, session=session, message=str(request_data["message"]))
    if run.get("kind") != "chat":
        raise HTTPException(409, "该运行类型不支持从当前入口重试")
    session = await asyncio.to_thread(get_agent_session, run.get("session_id", ""), project_id)
    request_data = run.get("request") or {}
    if not session or not request_data.get("message"):
        raise HTTPException(409, "原始会话或用户消息已不可用，无法重试")
    next_run = await asyncio.to_thread(create_agent_run_record, 
        project_id=project_id,
        session_id=session["id"],
        parent_run_id=run["id"],
        kind="chat",
        title=f"重试：{clip(request_data['message'], 110)}",
        request=request_data,
        max_attempts=run.get("max_attempts", 2),
        attempt=run.get("attempt", 1) + 1,
    )
    return await _app_call("run_project_agent", 
        project_id=project_id,
        session=session,
        run=next_run,
        message=str(request_data["message"]),
        context=request_data.get("context") or {},
    )


@app.get("/api/agent/actions/{action_id}")
def get_agent_action(action_id: str) -> dict[str, Any]:
    action = get_agent_action_record(action_id)
    if not action:
        raise HTTPException(404, "Agent 动作不存在")
    return {"action": action}


@app.post("/api/agent/actions/{action_id}/confirm")
def confirm_agent_action(action_id: str) -> dict[str, Any]:
    action = execute_agent_action(action_id, force=True)
    return {"ok": action.get("status") == "executed", "action": action}


@app.post("/api/agent/actions/{action_id}/retry")
def retry_agent_action(action_id: str) -> dict[str, Any]:
    action = execute_agent_action(action_id)
    return {"ok": action.get("status") == "executed", "action": action, "run": get_agent_run(action.get("run_id", "")) if action.get("run_id") else None}


@app.post("/api/agent/cid-dashboard/chat/completions")
async def cid_dashboard_agent_proxy(request: AgentProxyRequest) -> Any:
    if not _app_call("llm_settings", )["configured"]:
        raise HTTPException(503, "请先配置工作台全局 LLM")
    messages = [
        {"role": str(item.get("role", "user")), "content": clip(str(item.get("content", "")), 12_000)}
        for item in request.messages
        if item.get("content")
    ]
    if not messages:
        raise HTTPException(400, "Agent 消息不能为空")
    run = await asyncio.to_thread(create_agent_run_record, 
        project_id="cid-dashboard",
        kind="chat",
        title=clip(str(messages[-1].get("content", "看板分析")), 120),
        request={"messages": messages, "model": request.model, "stream": request.stream},
        max_attempts=2,
    )
    answer, updated_run = await _app_call("run_cid_agent_turn", run=run, messages=messages)
    response_id = uuid.uuid4().hex
    if not request.stream:
        return {"id": response_id, "object": "chat.completion", "run": updated_run, "run_id": run["id"], "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]}

    async def stream_response():
        chunk = {"id": response_id, "object": "chat.completion.chunk", "run_id": run["id"], "choices": [{"index": 0, "delta": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")


@app.get("/api/agents")
def list_agents() -> dict[str, Any]:
    configured = bool(_app_call("llm_settings", )["configured"])
    project_ids = [item.get("id") for item in load_projects() if item.get("id")]
    child_ids = [project_id for project_id in project_ids if project_id in AGENT_REGISTRY["workbench"].get("children", [])]
    global_agent = _app_call("agent_detail", "workbench", llm_ready=configured)
    global_agent["id"] = "workbench"
    global_agent["children"] = child_ids
    global_agent["children_detail"] = [_app_call("agent_detail", project_id, llm_ready=configured) for project_id in child_ids]
    return {
        "llm": _app_call("llm_settings", ),
        "global_agent": global_agent,
        "agents": [_app_call("agent_detail", project_id, llm_ready=configured) for project_id in project_ids],
    }


@app.get("/api/agents/metrics")
def get_agent_metrics(hours: int = 24) -> dict[str, Any]:
    project_ids = list(dict.fromkeys(["workbench", *[str(item.get("id")) for item in load_projects() if item.get("id")]]))
    return {
        "llm": _app_call("llm_usage_metrics_payload", hours),
        "agents": {project_id: agent_run_summary(project_id) for project_id in project_ids},
        "quality": {project_id: agent_quality_metrics(project_id, hours) for project_id in project_ids},
        "policy": "质量指标只统计持久化 Run、Action 和结果契约；成功不等于事实正确，来源/数据时间缺失会单独显示。",
    }




def redact_agent_context(value: Any) -> Any:
    """Remove secrets before a project snapshot is included in an Agent prompt."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(word in key_text for word in ("key", "token", "secret", "password", "cookie", "authorization")):
                redacted[key] = "[已隐藏]"
            else:
                redacted[key] = redact_agent_context(item)
        return redacted
    if isinstance(value, list):
        return [redact_agent_context(item) for item in value[:30]]
    return value


def agent_project_context(project_id: str) -> dict[str, Any]:
    """Build a small, read-only snapshot for a child Agent.

    This is deliberately assembled from existing project stores instead of
    asking the model to invent project state. Write actions remain separate
    APIs and are not performed by dispatch_agent_task.
    """
    context: dict[str, Any]
    if project_id == "inbox":
        context = {"source": "SQLite inbox", "pending": list_inbox("inbox")[:12]}
    elif project_id == "knowledge":
        context = {
            "source": str(KNOWLEDGE_DIR),
            "recent_notes": _app_call("knowledge_search", "")[:12],
            "obsidian": _app_call("obsidian_status", ),
            "obsidian_recent_notes": _app_call("obsidian_search", "", limit=12),
            "obsidian_today_notes": _app_call("obsidian_search", "", limit=20, since_timestamp=datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),
            "inbox_candidates": _app_call("knowledge_inbox_candidates", )[:12],
            "moc_suggestions": _app_call("obsidian_moc_suggestions", ),
        }
    elif project_id == "doc-factory":
        files = [path for path in OUTPUTS_DIR.iterdir() if path.is_file() and not path.name.startswith(".")]
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        context = {
            "source": str(OUTPUTS_DIR),
            "recent_outputs": [
                {"name": path.name, "size": path.stat().st_size, "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()}
                for path in files[:12]
            ],
            "available_source_artifacts": _app_call("document_factory_source_descriptors", limit=30),
            "source_policy": "只读工作台管理目录中的已登记 Artifact；生成结果保留来源 Relation，不覆盖旧产物。",
        }
    elif project_id == "sub2api":
        snapshot = load_sub2api_snapshot()
        context = {"source": str(SUB2API_SNAPSHOT_FILE), "snapshot": redact_agent_context(snapshot), "analysis": analyze_sub2api_snapshot(snapshot), "history": list_sub2api_history(limit=8)}
    elif project_id == "market":
        snapshot = load_market_snapshot()
        record_market_snapshot(snapshot)
        history = list_market_history(limit=12)
        analysis = analyze_market_snapshot(snapshot, history)
        context = {
            "source": str(MARKET_SNAPSHOT_FILE),
            "snapshot": redact_agent_context(snapshot),
            "analysis": analysis,
            "history": redact_agent_context(history),
            "open_observation_tasks": [
                item for item in _app_call("list_work_items", "all", "market")
                if item.get("source_project") == "market"
                and item.get("kind") == "research_observation"
                and item.get("status") in {"open", "running", "blocked"}
            ][:20],
        }
    elif project_id == "server":
        snapshot = load_server_monitor_snapshot()
        history = list_server_monitor_history(limit=12)
        context = {
            "source": str(SERVER_MONITOR_SNAPSHOT_FILE),
            "target": server_monitor_config()["server"],
            "snapshot": redact_agent_context(snapshot),
            "analysis": analyze_server_snapshot(snapshot, history),
            "history": redact_agent_context(history),
            "risk_boundary": "只读探测；重启、部署、配置修改和删除必须人工确认。",
        }
    elif project_id in {"crawl4ai", "web-research"}:
        durable_runs = list_agent_runs("crawl4ai", limit=12)
        recent_runs: list[dict[str, Any]] = []
        seen_run_ids: set[str] = set()

        # The in-memory object has the freshest logs while a crawl is active;
        # the SQLite row is the source of truth after a restart. Merge both so
        # the Agent never loses its research context just because the API was
        # restarted between two questions.
        for durable in durable_runs:
            run_id = str(durable.get("id") or "")
            if not run_id or run_id in seen_run_ids:
                continue
            runtime = runs.get(run_id) or _app_call("runtime_crawl_from_agent_run", durable)
            result = durable.get("result") or {}
            documents = runtime.get("documents") or result.get("documents") or []
            recent_runs.append(
                {
                    "id": run_id,
                    "status": runtime.get("status") or durable.get("status"),
                    "status_label": durable.get("status_label"),
                    "task": runtime.get("task") or durable.get("request", {}).get("task", ""),
                    "urls": runtime.get("urls") or durable.get("request", {}).get("urls", []),
                    "documents": [
                        {
                            "url": item.get("url", ""),
                            "title": item.get("title", "未命名页面"),
                            "success": bool(item.get("success")),
                            "markdown_chars": item.get("markdown_chars", 0),
                        }
                        for item in documents[:12]
                    ],
                    "analysis_status": runtime.get("analysis_status") or result.get("analysis_status"),
                    "artifact_id": runtime.get("artifact_id") or result.get("artifact_id"),
                    "work_item_id": runtime.get("work_item_id") or result.get("work_item_id"),
                    "error": runtime.get("error") or durable.get("error", ""),
                    "created_at": durable.get("created_at"),
                    "updated_at": durable.get("updated_at"),
                }
            )
            seen_run_ids.add(run_id)

        # A newly queued run can exist in memory before the first durable
        # result snapshot has been written. Keep that state visible too.
        for runtime in sorted(runs.values(), key=lambda item: item.get("created_at", ""), reverse=True):
            run_id = str(runtime.get("id") or "")
            if not run_id or run_id in seen_run_ids:
                continue
            recent_runs.append(
                {
                    "id": run_id,
                    "status": runtime.get("status", "queued"),
                    "status_label": AGENT_RUN_STATUS_LABELS.get(runtime.get("status"), runtime.get("status", "未知")),
                    "task": runtime.get("task", ""),
                    "urls": runtime.get("urls", []),
                    "documents": [
                        {
                            "url": item.get("url", ""),
                            "title": item.get("title", "未命名页面"),
                            "success": bool(item.get("success")),
                            "markdown_chars": item.get("markdown_chars", 0),
                        }
                        for item in (runtime.get("documents") or [])[:12]
                    ],
                    "analysis_status": runtime.get("analysis_status"),
                    "artifact_id": runtime.get("artifact_id"),
                    "work_item_id": runtime.get("work_item_id"),
                    "error": runtime.get("error", ""),
                    "created_at": runtime.get("created_at"),
                    "updated_at": runtime.get("finished_at") or runtime.get("created_at"),
                }
            )
            seen_run_ids.add(run_id)

        context = {
            "source": "SQLite agent_runs + current crawl runtime",
            "runs": recent_runs[:12],
        }
    elif project_id == "aihot":
        snapshot = _app_call("load_aihot_snapshot", )
        context = {"source": "aihot.today", "fetched_at": snapshot.get("fetched_at"), "items": _app_call("select_aihot_items", snapshot, "useful", limit=12)}
    elif project_id == "idea-analysis":
        context = {
            "source": "SQLite idea_sessions + incoming opportunity WorkItems",
            "recent_sessions": _app_call("list_idea_sessions", limit=8),
            "incoming_opportunities": _app_call("idea_opportunity_work_items", limit=12),
        }
    elif project_id == "product-manager":
        overview = _app_call("product_manager_overview", limit=20)
        context = {
            "source": "SQLite product_feedback + product_requirements + product_decisions",
            "summary": overview.get("summary", {}),
            "feedback": overview.get("feedback", [])[:12],
            "requirements": overview.get("requirements", [])[:12],
            "decisions": overview.get("decisions", [])[:8],
            "priority_policy": "RICE = reach × impact × confidence / effort；分数只用于排序建议，最终优先级必须由产品经理确认。",
        }
    elif project_id == "workbench":
        context = {"source": "SQLite work_items", "open_work_items": _app_call("list_work_items", "open")[:12]}
    elif project_id == "cid-dashboard":
        context = {"source": "browser project context", "note": "看板明细由 CID 页面在调用子 Agent 时提供；后端不读取其浏览器 localStorage。"}
    else:
        context = {"source": "project registry", "note": "当前项目尚未注册可读取的数据源。"}

    links = project_link_summary(project_id)
    return {
        "project_context": context,
        "shared_context": {
            "project_id": project_id,
            "agent_name": agent_display_name(project_id),
            "inbound_links": links["inbound"],
            "outbound_links": links["outbound"],
            "open_work_items": _app_call("list_work_items", "open", project_id)[:12],
            "recent_artifacts": _app_call("list_artifacts", project_id)[:12],
        },
    }


def agent_context_result_metadata(context: dict[str, Any] | None) -> dict[str, Any]:
    """Extract traceable IDs and freshness from the read-only Agent snapshot."""
    if not isinstance(context, dict):
        return {"source_refs": [], "artifact_ids": [], "work_item_ids": [], "relation_ids": [], "data_as_of": ""}
    project_context = context.get("project_context") if isinstance(context.get("project_context"), dict) else context
    shared_context = context.get("shared_context") if isinstance(context.get("shared_context"), dict) else {}
    refs: list[dict[str, Any]] = []
    artifact_ids: list[str] = []
    work_item_ids: list[str] = []
    relation_ids: list[str] = []
    timestamps: list[str] = []

    source = project_context.get("source")
    if source:
        refs.append({"type": "context", "label": source, "locator": source})

    def add_item(item: Any, kind: str) -> None:
        if not isinstance(item, dict):
            return
        item_id = item.get("id") or item.get(f"{kind}_id")
        if kind == "artifact" and item_id is not None:
            artifact_ids.append(str(item_id))
        elif kind == "work_item" and item_id is not None:
            work_item_ids.append(str(item_id))
        elif kind == "relation" and item_id is not None:
            relation_ids.append(str(item_id))
        ref = {"type": kind, "id": item_id, "title": item.get("title") or item.get("name"), "source": item.get("source"), "url": item.get("url") or item.get("link"), "path": item.get("path")}
        for key in ("data_as_of", "data_at", "fetched_at", "checked_at", "published_at", "updated_at", "created_at"):
            if item.get(key):
                ref[key] = item[key]
                timestamps.append(str(item[key]))
                break
        if item_id is not None or ref.get("title") or ref.get("source") or ref.get("url") or ref.get("path"):
            refs.append(ref)

    for item in shared_context.get("recent_artifacts") or []:
        add_item(item, "artifact")
    for item in shared_context.get("open_work_items") or []:
        add_item(item, "work_item")
    request_context = context.get("request_context") if isinstance(context.get("request_context"), dict) else {}
    for key, kind in (("artifact_ids", "artifact"), ("work_item_ids", "work_item"), ("relation_ids", "relation")):
        for item_id in request_context.get(key) or []:
            add_item({"id": item_id}, kind)
    for key in ("data_as_of", "data_at", "fetched_at", "checked_at", "published_at", "updated_at", "created_at"):
        value = project_context.get(key)
        if value:
            timestamps.append(str(value))

    data_as_of = max(set(timestamps)) if timestamps else ""
    return {
        "source_refs": _contract_source_refs(refs),
        "artifact_ids": _contract_id_list(artifact_ids),
        "work_item_ids": _contract_id_list(work_item_ids),
        "relation_ids": _contract_id_list(relation_ids),
        "data_as_of": data_as_of,
    }



def child_agent_system(project_id: str) -> str:
    capability = AGENT_REGISTRY.get(project_id, {})
    project = next((item for item in load_projects() if item.get("id") == project_id), {})
    playbook = AGENT_PLAYBOOKS.get(project_id, {})
    output_contract = "、".join(playbook.get("output", ["结论", "证据", "下一步", "风险"]))
    domain_guardrail = ""
    if project_id == "market":
        domain_guardrail = (
            "量化选股特别规则：股票名称只能使用实时快照中的 name-symbol 对应关系或工作台明确登记的本地映射；"
            "没有可验证代码时要说明缺口，不要猜代码。动作只允许来自用户原始消息中的明确标的，"
            "不能把当前已有自选、你的回答或其他子 Agent 提到的股票变成新动作。"
        )
    return (
        f"你是本地工作台中的「{capability.get('name', project.get('title', project_id))}」（{project_id}）子 Agent。"
        f"你的职责是：{project.get('description', '处理本项目相关任务')}。"
        f"当前能力状态：{capability.get('status', 'planned')}；可用工具声明：{', '.join(capability.get('tools', [])) or '无'}。"
        f"你的核心任务：{playbook.get('mission', '围绕上下文给出可执行结果')}。"
        f"执行流程：{playbook.get('workflow', '读取上下文→分析→给出下一步')}。"
        f"输出必须覆盖：{output_contract}。"
        "请尽量使用以下固定小标题输出：结论、事实与证据、判断与不确定性、风险/缺口、动作、下一步。"
        f"自动化边界：{playbook.get('autonomy', '只读分析；写入动作按权限确认')}。"
        f"{domain_guardrail}"
        "你只应基于传入的上下文回答，不要假装已经调用了未提供的工具；"
        "不要只给泛泛建议：每条结论都要绑定上下文中的证据、时间或缺口；如果数据不足，先指出缺口并给出最小补充动作。"
        "明确区分已知事实、推断、建议和需要用户确认的动作。使用简体中文，输出简洁、可执行的结果。"
    )


def market_symbols_from_text(value: str) -> list[str]:
    """Resolve only codes or locally verifiable stock names from user text.

    The LLM response is deliberately not accepted here. A model may mention
    an existing holding or speculate about a code; neither is user intent.
    """
    candidates = re.findall(
        r"(?<![a-z0-9])(?:sh|sz|bj)\d{6}(?!\d)|(?<!\d)\d{6}(?!\d)",
        (value or "").lower(),
    )
    symbols = []
    for candidate in candidates:
        normalized = normalize_market_symbol(candidate)
        if normalized and normalized not in symbols:
            symbols.append(normalized)
    aliases = {
        "传智教育": "sz003032",
        "传智": "sz003032",
        "长鑫科技": "sh688825",
    }
    for quote in load_market_snapshot().get("quotes", []):
        name = str(quote.get("name") or "").strip()
        symbol = normalize_market_symbol(str(quote.get("symbol") or ""))
        if name and symbol:
            aliases[name] = symbol
    lowered = str(value or "").lower()
    for name, symbol in aliases.items():
        if name.lower() in lowered and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def extract_threshold_values(message: str) -> dict[str, float]:
    """Parse threshold mentions like 磁盘 90 / 内存 95 / 负载 8 into bounded values."""
    values: dict[str, float] = {}
    text = message.lower()
    patterns = [
        ("disk", r"磁盘[^\d]{0,10}(\d{1,3})"),
        ("memory", r"内存[^\d]{0,10}(\d{1,3})"),
        ("load", r"负载[^\d]{0,10}(\d{1,3}(?:\.\d)?)"),
    ]
    for key, pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            number = float(match.group(1))
        except ValueError:
            continue
        if key == "load":
            if 0 <= number <= 1000:
                values[f"load_warn"] = max(0, number - 4)
                values[f"load_critical"] = number
        else:
            if 0 <= number <= 100:
                values[f"{key}_warn"] = max(0, number - 10)
                values[f"{key}_critical"] = number
    return values


# 否定、疑问、转述——这三种句子里出现动作关键词，都不代表用户要执行它。
# 原来的推断只做子串包含：「最近不用太关注传智教育了」会命中「关注」并把
# 传智教育加进自选；「要不要记录一下？」会写收件箱。
NON_IMPERATIVE_PATTERNS = (
    re.compile(r"(不用|不要|别|无需|不必|没必要|不想|不需要)[^，。；\n]{0,8}(记录|记一下|沉淀|关注|添加|写入|保存)"),
    re.compile(r"(要不要|是否需要|需不需要|该不该|能不能|可不可以).{0,12}[?？]?$"),
    re.compile(r"[?？]\s*$"),
    re.compile(r"(他说|她说|别人说|同事说|之前说过|听说)"),
)


def action_intent_is_imperative(message: str) -> bool:
    """判断这句话是不是真的在要求执行，而不是在否定、提问或转述。"""
    text = str(message or "").strip()
    if not text:
        return False
    return not any(pattern.search(text) for pattern in NON_IMPERATIVE_PATTERNS)


def infer_agent_actions(project_id: str, message: str, answer: str) -> list[dict[str, Any]]:
    """Infer only narrowly-scoped, reversible local actions from an Agent turn.

    This is intentionally conservative: only explicit, reversible local
    actions are inferred (capture, note creation, watchlist addition). Server
    changes, trading, deletion and external communication are never inferred here.
    """
    # 关键词命中不等于用户要求执行：否定、疑问、转述都会命中同一批词。
    if not action_intent_is_imperative(message):
        return []
    if project_id == "inbox":
        capture_words = ("记录", "记一下", "放进收件箱", "保存到收件箱", "收进去")
        if any(word in message for word in capture_words) and not any(word in message for word in ("查询", "查看", "有哪些")):
            content = re.sub(r"^(帮我|请|麻烦你)?\s*(记录一下|记一下|记录|放进收件箱|保存到收件箱|收进去)?\s*[:：，,]?\s*", "", message.strip()).strip()
            if content:
                return [{
                    "name": "写入收件箱",
                    "tool": "inbox.capture",
                    "risk": runtime_tool_policy("inbox_capture")["risk"],
                    "requires_confirmation": runtime_tool_policy("inbox_capture")["mode"] == "confirm",
                    "arguments": {"content": content, "kind": "task" if "待办" in message or "任务" in message else "note", "tags": "Agent记录"},
                }]
    if project_id == "knowledge":
        save_words = ("沉淀", "写入知识库", "保存到知识库", "记成笔记", "写一篇笔记")
        if any(word in message for word in save_words):
            title = clip(re.sub(r"\s+", " ", message).strip(), 80) or "Agent 沉淀"
            return [{
                "name": "沉淀为本地知识笔记",
                "tool": "knowledge.note.create",
                "risk": runtime_tool_policy("knowledge_write")["risk"],
                "requires_confirmation": runtime_tool_policy("knowledge_write")["mode"] == "confirm",
                "arguments": {"title": title, "content": answer},
            }]
    if project_id == "server":
        # 阈值调整是本地低风险动作（只写本机 JSON，不碰服务器），可直接执行。
        threshold_words = ("阈值", "告警线", "警告线", "调到", "设置成", "改成")
        if any(word in message for word in ("磁盘", "内存", "负载")) and any(word in message for word in threshold_words):
            thresholds = extract_threshold_values(message)
            if thresholds:
                return [{
                    "name": "调整服务器监控阈值",
                    "tool": "server.thresholds.set",
                    # 策略表里 server_thresholds_set 一直写着 mode: confirm，
                    # 而这里生成的却是「低风险、不用确认」并直接落盘——
                    # 改告警阈值意味着以后可能收不到该收的告警，必须过人。
                    "risk": "medium",
                    "requires_confirmation": True,
                    "arguments": {"thresholds": thresholds},
                }]
        return []
    if project_id != "market":
        return []
    observation_words = ("观察任务", "研究任务", "观察清单", "生成观察", "生成研究任务", "建立观察")
    if any(word in message for word in observation_words):
        return [{
            "name": "生成行情观察任务",
            "tool": "market.observations.evaluate",
            "risk": "low",
            "requires_confirmation": False,
            "arguments": {},
        }]
    intent_words = ("添加", "加入", "放入", "自选", "关注", "监控")
    if not any(word in message for word in intent_words) or any(word in message for word in ("删除", "移除", "卖出", "买入", "下单")):
        return []
    # Never derive a write action from the model's answer. Only explicit codes
    # or a locally verifiable name in the user's message may become an action.
    symbols = market_symbols_from_text(message)
    return [
        {
            "name": "加入量化选股自选",
            "tool": "market.watchlist.add",
            # 这条是从「关注」两个字 + 股票别名表猜出来的，误判率最高的一条：
            # 「不用太关注 X 了」也会命中。加一道确认，猜错了也就是多一次点击。
            "risk": "low",
            "requires_confirmation": True,
            "arguments": {"symbol": symbol},
        }
        for symbol in symbols[:5]
    ]


def execute_agent_action(action_id: str, *, force: bool = False, parent_run_id: str = "") -> dict[str, Any]:
    action = get_agent_action_record(action_id)
    if not action:
        raise HTTPException(404, "Agent 动作不存在")
    if action["status"] == "executed":
        return action
    if action["status"] == "rejected":
        return action
    if action["requires_confirmation"] and not force:
        return action
    run = get_agent_run(action.get("run_id", "")) if action.get("run_id") else None
    if run and run.get("status") == "succeeded":
        return action
    if run and run.get("attempt", 1) >= run.get("max_attempts", 1) and run.get("status") == "failed":
        return action
    if not run:
        run = create_agent_run_record(
            project_id=action["project_id"],
            kind="action",
            title=action.get("name") or action.get("tool") or "Agent 动作",
            request={"action_id": action_id, "tool": action.get("tool"), "arguments": action.get("arguments", {})},
            parent_run_id=parent_run_id,
            max_attempts=3,
        )
        update_agent_action_record(action_id, run_id=run["id"])
        action = get_agent_action_record(action_id) or action
    elif run.get("status") == "failed":
        next_attempt = min(run.get("max_attempts", 3), run.get("attempt", 1) + 1)
        run = update_agent_run_record(run["id"], status="queued", attempt=next_attempt, error="") or run
        _app_call("add_agent_run_event", run["id"], "retry_queued", f"第 {next_attempt} 次尝试已排队。", metadata={"action_id": action_id})
    update_agent_run_record(run["id"], status="running")
    _app_call("add_agent_run_event", run["id"], "started", f"开始执行：{action.get('name') or action.get('tool')}", metadata={"action_id": action_id})
    try:
        # 确认门创建的动作用的是运行时工具名（notify / cloud_dev_generate /
        # cloud_dev_test 等）。此前这里只认旧的点号命名（market.watchlist.add、
        # inbox.capture…），两套名字零交集，确认后必然抛「工具尚未接入执行器」。
        # 运行时工具名直接回调 _app_call("execute_react_tool", ..., confirmed=True) 真正执行；
        # 旧点号分支保留兼容历史动作。
        if action["tool"] in _REACT_TOOLS() or action["tool"] in _SUBAGENT_EXTRA_TOOLS():
            result = _app_call("execute_react_tool", action["tool"], action.get("arguments") or {}, project_id=action["project_id"], run_id=run["id"], confirmed=True)
            if not isinstance(result, dict) or not result.get("ok"):
                raise RuntimeError(str((result or {}).get("error") or "工具执行失败"))
            result = result.get("result") if isinstance(result.get("result"), dict) else result
        elif action["tool"] == "market.watchlist.add":
            result = add_market_symbol_to_watchlist(action["arguments"].get("symbol", ""))
        elif action["tool"] == "market.observations.evaluate":
            result = evaluate_market_observations(create_records=True)
        elif action["tool"] == "inbox.capture":
            result = create_inbox_record(
                content=action["arguments"].get("content", ""),
                kind=action["arguments"].get("kind", "note"),
                tags=action["arguments"].get("tags", ""),
            )
        elif action["tool"] == "knowledge.note.create":
            result = _app_call("write_knowledge_note", 
                action["arguments"].get("title", "Agent 沉淀"),
                action["arguments"].get("content", ""),
            )
        elif action["tool"] == "server.thresholds.set":
            raw = action["arguments"].get("thresholds")
            values = raw if isinstance(raw, dict) else {}
            result = save_server_monitor_thresholds({key: float(value) for key, value in values.items() if key in DEFAULT_SERVER_THRESHOLDS})
        else:
            raise RuntimeError(f"工具尚未接入执行器：{action['tool']}")
        updated = update_agent_action_record(action_id, status="executed", result=result, run_id=run["id"]) or action
        update_agent_run_record(run["id"], status="succeeded", result={"action": updated})
        _app_call("add_agent_run_event", run["id"], "succeeded", "动作执行完成。", level="success", metadata={"action_id": action_id})
        parent = get_agent_run(parent_run_id) if parent_run_id else None
        if not parent or parent.get("project_id") != "workbench":
            try:
                create_notification_record(
                    title=f"Agent 动作已完成：{updated.get('name') or updated.get('tool') or '动作'}",
                    body=agent_action_notice([updated]) or "动作已执行并记录。",
                    project_id=updated.get("project_id") or "workbench",
                    kind="agent_action",
                    level="success",
                    href=project_href(updated.get("project_id") or "workbench"),
                    event_key=f"agent-action-run:{run['id']}",
                    dedupe_seconds=0,
                )
            except Exception:
                log.debug("忽略异常（execute_agent_action）", exc_info=True)
        return updated
    except Exception as exc:
        error = str(exc)
        updated = update_agent_action_record(action_id, status="failed", result={"error": error}, run_id=run["id"]) or action
        update_agent_run_record(run["id"], status="failed", error=error, result={"action": updated})
        _app_call("add_agent_run_event", run["id"], "failed", f"动作执行失败：{error}", level="error", metadata={"action_id": action_id})
        parent = get_agent_run(parent_run_id) if parent_run_id else None
        if not parent or parent.get("project_id") != "workbench":
            try:
                create_notification_record(
                    title=f"Agent 动作失败：{updated.get('name') or updated.get('tool') or '动作'}",
                    body=error,
                    project_id=updated.get("project_id") or "workbench",
                    kind="agent_action",
                    level="error",
                    href=project_href(updated.get("project_id") or "workbench"),
                    event_key=f"agent-action-run:{run['id']}",
                    dedupe_seconds=0,
                )
            except Exception:
                log.debug("忽略异常（execute_agent_action）", exc_info=True)
        return updated


def materialize_agent_actions(project_id: str, message: str, answer: str, *, parent_run_id: str = "") -> list[dict[str, Any]]:
    actions = []
    for definition in infer_agent_actions(project_id, message, answer):
        action = create_agent_action_record(project_id=project_id, **definition)
        if not action["requires_confirmation"]:
            action = execute_agent_action(action["id"], parent_run_id=parent_run_id)
        actions.append(action)
    return actions


def agent_action_notice(actions: list[dict[str, Any]]) -> str:
    notices = []
    for action in actions:
        result = action.get("result") or {}
        if action.get("status") == "executed" and action.get("tool") == "market.watchlist.add":
            symbol = result.get("symbol") or action.get("arguments", {}).get("symbol", "")
            state = "已在自选中" if not result.get("added", True) else "已加入自选"
            notices.append(f"✅ {state}：{str(symbol).upper()}")
        elif action.get("status") == "executed" and action.get("tool") == "market.observations.evaluate":
            result = action.get("result") or {}
            created = [item for item in result.get("created", []) if item.get("created")]
            candidates = result.get("candidates") or []
            notices.append(f"✅ 已生成行情观察任务：新增 {len(created)} 条，识别 {len(candidates)} 条")
        elif action.get("status") == "executed" and action.get("tool") == "inbox.capture":
            notices.append(f"✅ 已写入收件箱：{result.get('content') or action.get('arguments', {}).get('content', '')}")
        elif action.get("status") == "executed" and action.get("tool") == "knowledge.note.create":
            notices.append(f"✅ 已沉淀知识笔记：{result.get('title') or result.get('name') or action.get('arguments', {}).get('title', '')}")
        elif action.get("status") == "pending":
            notices.append(f"⏳ 待确认：{action.get('name', 'Agent 动作')}（动作 ID：{action.get('id')}）")
        elif action.get("status") == "failed":
            notices.append(f"⚠️ 执行失败：{action.get('name', 'Agent 动作')} · {(result.get('error') or '未知错误')}")
    return "\n".join(notices)


async def stream_agent_react_loop(
    *,
    project_id: str,
    run_id: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_cache: dict[str, Any] | None = None,
    max_rounds: int = _AGENT_MAX_TOOL_ROUNDS(),
):
    """流式版 ReAct 循环：工具轮一次性执行，最终回答轮真流式输出。

    与 run_agent_react_loop 同语义（工具执行、事件流、回喂），区别：
    回答轮的文字增量边收边 yield（{"type":"delta_text","text":...}），
    结束后 yield {"type":"finish",...,"answer":完整回答}。
    工具轮如有少量说明文字也会流出（保留在最终 answer 之前的展示区）。
    """
    if not tools:
        async for chunk in _app_call("stream_llm_text", messages, max_tokens=4000, temperature=0.2, purpose="agent"):
            yield chunk
        return
    working = list(messages)
    rounds = 0
    executed: list[dict[str, Any]] = []
    final_answer = ""
    final_usage = None
    final_provider = ""
    final_reason = "stop"
    while rounds < max_rounds:
        content = ""
        tool_calls: list[dict[str, Any]] = []
        provider = ""
        usage = None
        round_errors: list[str] = []
        async for chunk in _app_call("stream_llm_with_tools", working, tools, purpose="agent_tools"):
            if chunk["type"] == "delta_text":
                content += chunk["text"]
                yield {"type": "delta_text", "text": chunk["text"]}
            elif chunk["type"] == "delta" and chunk.get("reasoning"):
                # 思考流原样透传，不进 content：它不是回答的一部分，
                # 但它是「现在到底在干什么」唯一的真实信号。
                yield {"type": "delta", "text": "", "reasoning": chunk["reasoning"]}
            elif chunk["type"] == "reset":
                content = ""
                yield chunk
            elif chunk["type"] == "round_done":
                content = chunk.get("content") or ""
                tool_calls = chunk.get("tool_calls") or []
                provider = chunk.get("provider", "")
                usage = chunk.get("usage")
            elif chunk["type"] == "error":
                # 单个 provider 失败时 stream_llm_with_tools 会继续尝试下一个；
                # 这里不能提前 return，否则 failover 被截断成「LLM 未返回内容」。
                # 记录错误继续等 round_done 或最终 error。
                round_errors.append(str(chunk.get("message") or ""))
        if not tool_calls:
            final_answer = content or final_answer
            final_usage = usage
            final_provider = provider
            if not content and round_errors:
                log.warning("Agent %s 工具轮无内容：%s", project_id, "；".join(round_errors)[:300])
            break
        working.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        # 开工事件：以前只有「完成/失败」，一次要跑十几秒的抓取期间页面上一片空白，
        # 看起来和卡死没区别。先把这一轮准备调的工具报出去。
        for pending_call in tool_calls[:12]:
            pending_name = str((pending_call.get("function") or {}).get("name") or "")
            yield {
                "type": "event",
                "kind": "tool_start",
                "tool": pending_name,
                "message": f"{_REACT_TOOL_LABELS().get(pending_name) or ('调用 ' + pending_name)}…",
            }
        semaphore = asyncio.Semaphore(_AGENT_MAX_PARALLEL_TOOL_CALLS())

        async def _run_one(tool_call: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
            fn = tool_call.get("function") or {}
            name = str(fn.get("name") or "")
            raw = fn.get("arguments")
            try:
                arguments = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
            except (TypeError, ValueError):
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            async with semaphore:
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(functools.partial(_app_call, "execute_react_tool", name, arguments, cache=tool_cache)),
                        timeout=_AGENT_TOOL_TIMEOUT_SECONDS(),
                    )
                except asyncio.TimeoutError:
                    log.warning("Agent %s 工具 %s 超时（%ss）", project_id, name, _AGENT_TOOL_TIMEOUT_SECONDS())
                    result = {"ok": False, "error": f"{name} 超过 {_AGENT_TOOL_TIMEOUT_SECONDS()} 秒未返回，已放弃本次调用，请换一个工具或直接基于已有信息作答"}
                except Exception as exc:
                    log.warning("Agent %s 工具 %s 异常：%s", project_id, name, exc, exc_info=True)
                    result = {"ok": False, "error": f"{name} 执行异常：{clip(str(exc), 300)}"}
            return str(tool_call.get("id") or ""), name, result

        outcomes = await asyncio.gather(*(_run_one(item) for item in tool_calls[:12]))
        for call_id, name, result in outcomes:
            executed.append({"tool": name, "ok": bool(result.get("ok")), "error": clip(str(result.get("error") or ""), 200)})
            _app_call("add_agent_run_event", 
                run_id,
                "agent_tool_call",
                f"{agent_display_name(project_id)} 调用工具 {name}。",
                level="info" if result.get("ok") else "warning",
                metadata={"tool": name, "result_ok": bool(result.get("ok")), "error": clip(str(result.get("error") or ""), 200)},
            )
            working.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)})
            # 过程反馈：工具执行完立刻把结果状态推给前端，回答期间不再像卡死。
            action_label = _REACT_TOOL_LABELS().get(name) or f"调用 {name}"
            yield {
                "type": "event",
                "kind": "tool",
                "tool": name,
                "ok": bool(result.get("ok")),
                "message": f"{action_label}{'完成' if result.get('ok') else '失败，继续'}" + (f" · {clip(str(result.get('error') or ''), 60)}" if not result.get("ok") else ""),
            }
        rounds += 1
    if not final_answer:
        # 工具轮次用尽却还没写出结论：再要一次不带工具的收敛回答（流式）。
        # stream_llm_text 的文本增量 chunk 类型是 "delta"（不是 "delta_text"），
        # 判断错了 final_answer 永远是空——工具轮越多越容易触发
        # 「LLM 未返回内容」。这里必须按 "delta" 收集。
        # 内部 finish 不转发：react loop 最后统一发带完整 answer 的 finish，
        # 否则上层会先收到一个没有 answer 的空 finish。
        async for chunk in _app_call("stream_llm_text", working, max_tokens=4000, temperature=0.2, purpose="agent"):
            if chunk["type"] == "delta":
                final_answer += chunk.get("text") or ""
                yield chunk
            elif chunk["type"] == "reset":
                final_answer = ""
                yield chunk
            elif chunk["type"] == "finish":
                final_usage = chunk.get("usage")
                final_provider = chunk.get("provider", "")
                # 透传真实 reason（length_capped 等），不再硬写 stop——
                # 否则截断连信号都没有，前端看到的是一次正常完成。
                final_reason = str(chunk.get("reason") or "stop")
            elif chunk["type"] == "error":
                yield chunk
    if not final_answer:
        yield {"type": "error", "message": "LLM 未返回内容，请稍后重试。", "provider": final_provider}
        return
    yield {"type": "finish", "reason": final_reason, "usage": final_usage, "provider": final_provider, "answer": final_answer, "tool_calls": executed, "rounds": rounds}


async def run_agent_react_loop(
    *,
    project_id: str,
    run_id: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_cache: dict[str, Any] | None = None,
    max_rounds: int = _AGENT_MAX_TOOL_ROUNDS(),
    queue_task_id: int = 0,
) -> dict[str, Any]:
    """跑一轮 ReAct：模型要工具就真执行，结果回喂，直到它给出结论。

    抽成公用函数的理由是一条真实的能力断层：这段循环原本只长在总调度的子
    Agent 分支里。从工作台问「市场怎么样」，市场 Agent 会真的去调 market_read；
    可是从市场项目页直接跟同一个 Agent 说话，走的是另一条代码路径——一次
    call_llm，一个工具都没有，只能对着一份可能已经过期的只读快照猜。同一个
    Agent 换个入口就少了半条腿，而项目页恰恰是最常用的入口。

    返回 {"answer": str, "tool_calls": [...], "rounds": int}；
    每次工具调用都会写进 run 的事件流，回放里能看到它到底查了什么。
    """
    answer = ""
    rounds = 0
    executed: list[dict[str, Any]] = []
    if not tools:
        return {"answer": "", "tool_calls": executed, "rounds": 0}
    working = list(messages)
    while rounds < max_rounds:
        body = await _app_call("call_llm_with_tools", working, tools)
        reply = ((body.get("choices") or [{}])[0]).get("message") or {}
        content = str(reply.get("content") or "").strip()
        tool_calls = reply.get("tool_calls") or []
        if content:
            answer = content
        if not tool_calls:
            break
        working.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        semaphore = asyncio.Semaphore(_AGENT_MAX_PARALLEL_TOOL_CALLS())

        async def _run_one(tool_call: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
            fn = tool_call.get("function") or {}
            name = str(fn.get("name") or "")
            raw = fn.get("arguments")
            try:
                arguments = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
            except (TypeError, ValueError):
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            async with semaphore:
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(functools.partial(_app_call, "execute_react_tool", name, arguments, cache=tool_cache,
                                                            project_id=project_id, run_id=run_id)),
                        timeout=_AGENT_TOOL_TIMEOUT_SECONDS(),
                    )
                except asyncio.TimeoutError:
                    log.warning("Agent %s 工具 %s 超时（%ss）", project_id, name, _AGENT_TOOL_TIMEOUT_SECONDS())
                    result = {"ok": False, "error": f"{name} 超过 {_AGENT_TOOL_TIMEOUT_SECONDS()} 秒未返回，已放弃本次调用，请换一个工具或直接基于已有信息作答"}
                except Exception as exc:  # noqa: BLE001 - 单个工具失败要转成结果回喂，不能拖垮整轮
                    log.warning("Agent %s 工具 %s 异常：%s", project_id, name, exc, exc_info=True)
                    result = {"ok": False, "error": f"{name} 执行异常：{clip(str(exc), 300)}"}
            return str(tool_call.get("id") or ""), name, result

        # 每轮工具跑完之后看一眼有没有人往这个任务里插消息。放在这里是因为
        # 这是循环里唯一一个「上一步已结束、下一步还没定」的位置——插进来的
        # 指令能影响接下来调什么工具，而不是等它跑完才被看到。
        if queue_task_id:
            for extra in await asyncio.to_thread(_app_call, "consume_agent_queue_messages", queue_task_id):
                working.append({"role": "user", "content": f"（任务进行中追加的指令）{extra}"})
                _app_call("add_agent_run_event", run_id, "queue_message", f"任务进行中收到追加指令：{clip(extra, 120)}", level="info")
            # 同一位置查取消：用户在队列页点了取消后，运行中的任务也能在
            # 本轮工具之间停下来，而不是只能干等（最坏 4 轮 × 60 秒）。
            if await asyncio.to_thread(_app_call, "agent_queue_task_cancelled", queue_task_id):
                _app_call("add_agent_run_event", run_id, "cancelled", "任务已被取消，停止后续工具调用。", level="warning")
                return {"answer": "（任务已取消）", "tool_calls": executed, "rounds": rounds, "cancelled": True}
        outcomes = await asyncio.gather(*(_run_one(item) for item in tool_calls[:12]))
        for call_id, name, result in outcomes:
            executed.append({"tool": name, "ok": bool(result.get("ok")), "error": clip(str(result.get("error") or ""), 200)})
            _app_call("add_agent_run_event", 
                run_id,
                "agent_tool_call",
                f"{agent_display_name(project_id)} 调用工具 {name}。",
                level="info" if result.get("ok") else "warning",
                metadata={"tool": name, "result_ok": bool(result.get("ok")), "error": clip(str(result.get("error") or ""), 200)},
            )
            working.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)})
        rounds += 1
    if not answer:
        # 工具轮次用尽却还没写出结论：再要一次不带工具的收敛回答，
        # 而不是把「汇总中」这种半成品当作最终结果返回。
        _app_call("add_agent_run_event", run_id, "react_forced_summary", f"{agent_display_name(project_id)} 用满 {max_rounds} 轮工具仍未给出结论，改为强制收敛作答。", level="warning")
        try:
            answer = await _app_call("call_llm", 
                [*working, {"role": "user", "content": "工具调用已达上限，现在不要再调用工具。请只基于以上已获得的工具结果直接给出结论；证据不足的部分明确标注为“未验证”。"}],
                purpose="agent_forced_summary",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Agent %s 强制收敛作答失败：%s", project_id, exc)
            answer = f"（已完成 {max_rounds} 轮工具探查，但未能形成结论：{clip(str(exc), 200)}）"
    return {"answer": answer, "tool_calls": executed, "rounds": rounds}


async def stream_project_agent(
    *,
    project_id: str,
    session: dict[str, Any],
    run: dict[str, Any],
    message: str,
    context: dict[str, Any] | None = None,
):
    """流式版项目 Agent turn：ReAct 工具轮一次性，最终回答轮流式输出，收完持久化。"""
    update_agent_run_record(run["id"], status="running", error="")
    _app_call("add_agent_run_event", run["id"], "started", f"{agent_display_name(project_id)} 开始读取项目上下文。")
    try:
        requested_tools = [str(item) for item in (context or {}).get("tool_ids", []) if str(item)]
        tool_boundary = validate_agent_tool_requests([project_id], requested_tools)
        if not tool_boundary["valid"]:
            raise HTTPException(400, f"请求的工具不在 {agent_display_name(project_id)} 能力声明中：{'、'.join(tool_boundary['rejected'])}")
        history = list_agent_messages(session["id"], limit=MAX_CONVERSATION_MESSAGES * 2)
        source_message = next((item for item in reversed(history) if item.get("role") == "user" and item.get("content") == message), None)
        memory_updates = learn_memories_from_message(
            message,
            project_id=project_id,
            source_type="agent_message",
            source_id=str((source_message or {}).get("id") or ""),
        )
        memory_context = memory_context_for_llm(project_id, message)
        project_context = agent_project_context(project_id)
        context_text = clip_for_llm(json.dumps(redact_agent_context(project_context), ensure_ascii=False), 16_000)
        messages = [
            {"role": "system", "content": child_agent_system(project_id) + "这是一个可持续的项目 Agent 会话。记住前面对话中的决定，但每次以当前项目上下文为准；如果用户的问题需要另一个项目，明确建议交接而不是假装拥有对方数据。"},
            {"role": "system", "content": f"本轮项目上下文（只读，可能有数据时间）：\n{context_text}"},
        ]
        if memory_context["text"]:
            messages.append({"role": "system", "content": f"用户长期记忆：\n{memory_context['text']}"})
        messages.extend({"role": item["role"], "content": item["content"]} for item in history[-MAX_CONVERSATION_MESSAGES:])
        project_tools = subagent_tool_schemas(project_id)
        tool_calls: list[dict[str, Any]] = []
        if project_tools:
            messages.insert(1, {
                "role": "system",
                "content": (
                    "你可以调用工具获取真实数据后再回答；工具结果是回答的事实依据，不要编造，也不要把快照里的旧数据当成当前值。"
                    f"可用工具：{'、'.join(item['function']['name'] for item in project_tools)}。"
                    "涉及当前状态、额度、行情、网页内容、收件箱、知识库检索等场景，先调工具再下结论。"
                ),
            })
        _app_call("add_agent_run_event", run["id"], "llm_started", "正在调用全局 LLM。", metadata={"model": llm_settings().get("model", ""), "tools": len(project_tools)})
        collected: list[str] = []
        final_answer = ""
        provider = ""
        usage = None
        if project_tools:
            async for chunk in stream_agent_react_loop(
                project_id=project_id,
                run_id=run["id"],
                messages=messages,
                tools=project_tools,
                tool_cache={},
            ):
                if chunk["type"] == "delta_text":
                    collected.append(chunk["text"])
                    yield chunk
                elif chunk["type"] == "delta":
                    yield chunk
                elif chunk["type"] == "reset":
                    collected.clear()
                    final_answer = ""
                    yield chunk
                elif chunk["type"] == "finish":
                    provider = chunk.get("provider", "")
                    usage = chunk.get("usage")
                    tool_calls = chunk.get("tool_calls") or []
                    # 工具轮用尽后的收敛回答只存在于 finish.answer（react loop
                    # 内部已按 "delta" 收集），上层不能只靠 delta_text。
                    final_answer = str(chunk.get("answer") or "")
                elif chunk["type"] == "event":
                    # 工具执行过程反馈：透传给前端显示"正在搜索…/正在抓取…"。
                    yield chunk
                elif chunk["type"] == "error":
                    yield chunk
        else:
            async for chunk in _app_call("stream_llm_text", messages, max_tokens=4000, temperature=0.25, purpose="agent"):
                if chunk["type"] == "delta":
                    if chunk.get("text"):
                        collected.append(chunk["text"])
                    yield chunk
                elif chunk["type"] == "reset":
                    collected.clear()
                    yield chunk
                elif chunk["type"] == "finish":
                    provider = chunk.get("provider", "")
                    usage = chunk.get("usage")
                elif chunk["type"] == "error":
                    yield chunk
        answer = (final_answer or "".join(collected)).strip()
        if not answer:
            update_agent_run_record(run["id"], status="failed", error="LLM 未返回内容")
            _app_call("add_agent_run_event", run["id"], "failed", "项目 Agent 未返回内容。", level="error")
            yield {"type": "error", "message": "LLM 未返回内容，请稍后重试。", "provider": provider}
            return
        _app_call("add_agent_run_event", run["id"], "llm_succeeded", "全局 LLM 已返回结果。", level="success", metadata={"tool_calls": len(tool_calls)})
        actions = materialize_agent_actions(project_id, message, answer, parent_run_id=run["id"])
        trace = agent_context_result_metadata({"project_context": project_context, "request_context": context or {}})
        execution_plan = build_agent_execution_plan(
            project_id,
            message,
            intent=str((context or {}).get("intent") or ""),
            requested_tools=tool_boundary["accepted"],
            status="partial" if any(action.get("status") == "failed" for action in actions) else "succeeded",
        )
        execution_plan["tool_calls"] = tool_calls
        execution_plan["tools_used"] = list(dict.fromkeys(item["tool"] for item in tool_calls))
        result_contract = agent_result_contract(
            project_id,
            answer,
            actions=actions,
            run_id=run["id"],
            session_id=session["id"],
            execution_plan=execution_plan,
            memory_refs=memory_context["refs"],
            memory_updates=memory_updates,
            memory_context_stats=memory_context["stats"],
            **trace,
        )
        assistant_message = add_agent_message(run["session_id"], "assistant", answer, {"actions": actions, "result_contract": result_contract, "run_id": run["id"], "memory_refs": memory_context["refs"], "memory_updates": memory_updates})
        session = update_agent_session_summary(
            session["id"],
            {"last_answer": clip(answer, 1200), "last_actions": actions, "last_result_contract": result_contract, "last_run_id": run["id"], "context_source": project_context.get("project_context", {}).get("source", ""), "last_memory_ids": [item["id"] for item in memory_context["items"]]},
        ) or session
        final_status = "partial" if any(action.get("status") == "failed" for action in actions) else "succeeded"
        result = {"session_id": session["id"], "message_id": assistant_message.get("id"), "answer": answer, "actions": actions, "result_contract": result_contract, "memory_refs": memory_context["refs"], "memory_updates": memory_updates, "memory_context": memory_context["stats"]}
        updated_run = update_agent_run_record(run["id"], status=final_status, result=result, error="") or run
        _app_call("add_agent_run_event", 
            run["id"],
            final_status,
            "对话完成。" if final_status == "succeeded" else "对话完成，但至少一个本地动作失败。",
            level="success" if final_status == "succeeded" else "warning",
            metadata={"actions": len(actions)},
        )
        yield {"type": "finish", "reason": "stop", "usage": usage, "provider": provider, "answer": answer, "session_id": session["id"], "message_id": assistant_message.get("id"), "actions": actions, "result_contract": result_contract, "memory_updates": memory_updates, "agent": _app_call("agent_detail", project_id, llm_ready=True)}
    except Exception as exc:
        update_agent_run_record(run["id"], status="failed", error=clip(str(exc), 500))
        _app_call("add_agent_run_event", run["id"], "failed", f"项目 Agent 失败：{clip(str(exc), 200)}", level="error")
        yield {"type": "error", "message": clip(str(exc), 300), "provider": ""}


async def run_project_agent(
    *,
    project_id: str,
    session: dict[str, Any],
    run: dict[str, Any],
    message: str,
    context: dict[str, Any] | None = None,
    queue_task_id: int = 0,
) -> dict[str, Any]:
    """Execute one persistent project-Agent turn and leave an audit trail."""
    update_agent_run_record(run["id"], status="running", error="")
    _app_call("add_agent_run_event", run["id"], "started", f"{agent_display_name(project_id)} 开始读取项目上下文。")
    try:
        requested_tools = [str(item) for item in (context or {}).get("tool_ids", []) if str(item)]
        tool_boundary = validate_agent_tool_requests([project_id], requested_tools)
        if not tool_boundary["valid"]:
            raise HTTPException(400, f"请求的工具不在 {agent_display_name(project_id)} 能力声明中：{'、'.join(tool_boundary['rejected'])}")
        history = list_agent_messages(session["id"], limit=MAX_CONVERSATION_MESSAGES * 2)
        source_message = next((item for item in reversed(history) if item.get("role") == "user" and item.get("content") == message), None)
        memory_updates = learn_memories_from_message(
            message,
            project_id=project_id,
            source_type="agent_message",
            source_id=str((source_message or {}).get("id") or ""),
        )
        memory_context = memory_context_for_llm(project_id, message)
        project_context = agent_project_context(project_id)
        context_text = clip_for_llm(json.dumps(redact_agent_context(project_context), ensure_ascii=False), 16_000)
        messages = [
            {"role": "system", "content": child_agent_system(project_id) + "这是一个可持续的项目 Agent 会话。记住前面对话中的决定，但每次以当前项目上下文为准；如果用户的问题需要另一个项目，明确建议交接而不是假装拥有对方数据。"},
            {"role": "system", "content": f"本轮项目上下文（只读，可能有数据时间）：\n{context_text}"},
        ]
        if memory_context["text"]:
            messages.append({"role": "system", "content": f"用户长期记忆：\n{memory_context['text']}"})
        messages.extend({"role": item["role"], "content": item["content"]} for item in history[-MAX_CONVERSATION_MESSAGES:])
        # 项目页直接对话也走 ReAct：只读快照会滞后，问「现在怎么样」时必须
        # 让 Agent 真去调工具取当下的数据，而不是照着快照复述。工具边界仍然是
        # 这个项目自己声明的那一份，和总调度路径完全一致。
        project_tools = subagent_tool_schemas(project_id)
        tool_calls: list[dict[str, Any]] = []
        if project_tools:
            messages.insert(1, {
                "role": "system",
                "content": (
                    "你可以调用工具获取真实数据后再回答；工具结果是回答的事实依据，不要编造，也不要把快照里的旧数据当成当前值。"
                    f"可用工具：{'、'.join(item['function']['name'] for item in project_tools)}。"
                    "涉及当前状态、额度、行情、网页内容、收件箱、知识库检索等场景，先调工具再下结论。"
                ),
            })
        _app_call("add_agent_run_event", run["id"], "llm_started", "正在调用全局 LLM。", metadata={"model": llm_settings().get("model", ""), "tools": len(project_tools)})
        if project_tools:
            loop_result = await run_agent_react_loop(
                project_id=project_id,
                run_id=run["id"],
                messages=messages,
                tools=project_tools,
                tool_cache={},
                queue_task_id=queue_task_id,
            )
            answer = loop_result["answer"]
            tool_calls = loop_result["tool_calls"]
        else:
            answer = await _app_call("call_llm", messages, max_tokens=4000, temperature=0.25)
        _app_call("add_agent_run_event", run["id"], "llm_succeeded", "全局 LLM 已返回结果。", level="success", metadata={"tool_calls": len(tool_calls)})
        actions = materialize_agent_actions(project_id, message, answer, parent_run_id=run["id"])
        trace = agent_context_result_metadata({"project_context": project_context, "request_context": context or {}})
        execution_plan = build_agent_execution_plan(
            project_id,
            message,
            intent=str((context or {}).get("intent") or ""),
            requested_tools=tool_boundary["accepted"],
            status="partial" if any(action.get("status") == "failed" for action in actions) else "succeeded",
        )
        # 把「这轮实际调了哪些工具」记进计划里：回放时能看出结论是查出来的
        # 还是模型自己想出来的，这是判断可信度最直接的一个信号。
        execution_plan["tool_calls"] = tool_calls
        execution_plan["tools_used"] = list(dict.fromkeys(item["tool"] for item in tool_calls))
        result_contract = agent_result_contract(
            project_id,
            answer,
            actions=actions,
            run_id=run["id"],
            session_id=session["id"],
            execution_plan=execution_plan,
            memory_refs=memory_context["refs"],
            memory_updates=memory_updates,
            memory_context_stats=memory_context["stats"],
            **trace,
        )
        assistant_message = add_agent_message(run["session_id"], "assistant", answer, {"actions": actions, "result_contract": result_contract, "run_id": run["id"], "memory_refs": memory_context["refs"], "memory_updates": memory_updates})
        session = update_agent_session_summary(
            session["id"],
            {"last_answer": clip(answer, 1200), "last_actions": actions, "last_result_contract": result_contract, "last_run_id": run["id"], "context_source": project_context.get("project_context", {}).get("source", ""), "last_memory_ids": [item["id"] for item in memory_context["items"]]},
        ) or session
        final_status = "partial" if any(action.get("status") == "failed" for action in actions) else "succeeded"
        result = {"session_id": session["id"], "message_id": assistant_message.get("id"), "answer": answer, "actions": actions, "result_contract": result_contract, "memory_refs": memory_context["refs"], "memory_updates": memory_updates, "memory_context": memory_context["stats"]}
        updated_run = update_agent_run_record(run["id"], status=final_status, result=result, error="") or run
        _app_call("add_agent_run_event", 
            run["id"],
            final_status,
            "对话完成。" if final_status == "succeeded" else "对话完成，但至少一个本地动作失败。",
            level="success" if final_status == "succeeded" else "warning",
            metadata={"actions": len(actions)},
        )
        return {
            "run": updated_run,
            "session": session,
            "message": assistant_message,
            "messages": list_agent_messages(session["id"], limit=40),
            "actions": actions,
            "result_contract": result_contract,
            "memory_refs": memory_context["refs"],
            "memory_updates": memory_updates,
            "agent": _app_call("agent_detail", project_id, llm_ready=True),
            "links": project_link_summary(project_id),
        }
    except httpx.HTTPStatusError as exc:
        error = f"上游返回 {exc.response.status_code}：{clip(exc.response.text, 500)}"
        update_agent_run_record(run["id"], status="failed", error=error)
        _app_call("add_agent_run_event", run["id"], "failed", f"{agent_display_name(project_id)} 调用失败：{error}", level="error")
        raise HTTPException(502, f"{agent_display_name(project_id)} 调用失败：{error}") from exc
    except HTTPException as exc:
        error = clip(str(exc.detail or exc), 800)
        update_agent_run_record(run["id"], status="failed", error=error)
        _app_call("add_agent_run_event", run["id"], "failed", f"{agent_display_name(project_id)} 请求被能力边界拦截：{error}", level="error")
        raise
    except Exception as exc:
        error = str(exc)
        update_agent_run_record(run["id"], status="failed", error=error)
        _app_call("add_agent_run_event", run["id"], "failed", f"{agent_display_name(project_id)} 执行失败：{error}", level="error")
        raise HTTPException(502, f"{agent_display_name(project_id)} 调用失败：{error}") from exc
__all__ = [
    "run_project_agent",
    "stream_project_agent",
    "redact_agent_context",
    "run_agent_react_loop",
    "stream_agent_react_loop",
    "agent_action_notice",
    "materialize_agent_actions",
    "execute_agent_action",
    "infer_agent_actions",
    "action_intent_is_imperative",
    "extract_threshold_values",
    "market_symbols_from_text",
    "child_agent_system",
    "agent_context_result_metadata",
    "agent_project_context",
    "CrawlRequest",
    "AgentProxyRequest",
    "ProjectAgentChatRequest",
    "WorkItemTakeoverRequest",
    "require_project_agent",
    "get_project_agent_sessions",
    "get_project_agent_session",
    "chat_project_agent",
    "get_project_agent_work_items",
    "takeover_project_work_item",
    "run_project_work_item",
    "dispatch_agent",
    "get_project_agent_runs",
    "get_project_agent_run",
    "retry_project_agent_run",
    "get_agent_action",
    "confirm_agent_action",
    "retry_agent_action",
    "cid_dashboard_agent_proxy",
    "list_agents",
    "get_agent_metrics",
]
