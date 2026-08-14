"""Workbench Agent 路由层：项目 Agent 会话/聊天/工作项/运行/动作/队列路由。

从 app.py 拆出的 agent 路由层（为开源准备）。数据层从 agent_runs 直连；执行引擎
（stream/run_project_agent/ReAct 循环/工具 handler）仍在 app.py，经 _app_call 转发；
CrawlRequest/AgentProxyRequest 等模型随路由拆入（FastAPI 注册时解析注解）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent_platform import AGENT_REGISTRY, AgentDispatchRequest, dispatch_agent_task
from .agent_runs import (
    add_agent_message,
    add_agent_run_event,
    agent_quality_metrics,
    agent_run_summary,
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
from .core import clip, now_iso
from .instance import app
from .projects import agent_display_name, load_projects


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
    add_agent_run_event(
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
            snapshot = await fetch_aihot_snapshot()
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
        return await run_aihot_agent_turn(run=next_run, session=session, message=str(request_data["message"]), chosen=chosen)
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
        return await run_crawl_chat_turn(durable_run=next_run, crawl_run=crawl_run, message=str(request_data["message"]))
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
        answer, updated = await run_cid_agent_turn(run=next_run, messages=messages)
        return {"run": updated, "id": uuid.uuid4().hex, "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]}
    if project_id == "idea-analysis" and run.get("kind") == "idea_plan":
        request_data = run.get("request") or {}
        session_id = str(run.get("session_id") or request_data.get("session_id") or "")
        session = await asyncio.to_thread(get_idea_session, session_id)
        if not session:
            raise HTTPException(409, "原始想法会话已不可用，无法重试验证工作台")
        if not list_idea_messages(session_id, limit=1):
            raise HTTPException(409, "原始想法没有对话内容，无法重试验证工作台")
        return await generate_idea_validation_plan(
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
            result = await run_idea_agent_turn(run=next_run, session=session, message=str(request_data["message"]))
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
        return await run_idea_agent_turn(run=next_run, session=session, message=str(request_data["message"]))
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
    answer, updated_run = await run_cid_agent_turn(run=run, messages=messages)
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
        "llm": llm_usage_metrics_payload(hours),
        "agents": {project_id: agent_run_summary(project_id) for project_id in project_ids},
        "quality": {project_id: agent_quality_metrics(project_id, hours) for project_id in project_ids},
        "policy": "质量指标只统计持久化 Run、Action 和结果契约；成功不等于事实正确，来源/数据时间缺失会单独显示。",
    }


__all__ = [
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
