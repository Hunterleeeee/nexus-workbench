"""Workbench 领域模块（app.py 拆分）。"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from .instance import app
from .core import clip, clip_for_llm, log, now_iso


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


class ChatRequest(BaseModel):
    run_id: str
    message: str = Field(min_length=1, max_length=8000)
    live_context: str = Field(default="", max_length=12_000)
    stream: bool = Field(default=False, description="true 时返回 SSE 流式输出")

class ChatStreamRequest(BaseModel):
    """通用流式对话请求（SSE 输出，逐块返回增量）。"""
    messages: list[dict[str, str]] = Field(min_length=1, max_length=40)
    max_tokens: int = Field(default=4000, ge=16, le=16000)
    temperature: float = Field(default=0.2, ge=0, le=2)
    purpose: str = Field(default="chat", max_length=30)
    reasoning: bool = Field(default=False, description="是否同时流式输出推理过程（reasoning_content）")


@app.post("/api/chat-stream")
async def chat_stream(request: ChatStreamRequest) -> StreamingResponse:
    """真正的流式对话接口：SSE 逐块返回 LLM 增量，而非一次性 JSON。

    事件格式（每行一个 data: JSON，结束以 data: [DONE] 收尾）：
      {"type": "delta", "text": "...", "reasoning": ""}    内容增量
      {"type": "finish", "reason": "stop", "usage": {...}, "provider": "..."}
      {"type": "error", "message": "...", "provider": "..."}（当前 provider 失败会自动换下一个）
    前端用 fetch + ReadableStream 消费，兼容所有浏览器。
    """
    if not _app_call('llm_settings', )["configured"]:
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM")
    messages = [
        {"role": str(item.get("role", "user")), "content": clip(str(item.get("content", "")), 12_000)}
        for item in request.messages
        if str(item.get("content") or "").strip()
    ]
    if not messages:
        raise HTTPException(400, "消息不能为空")

    async def event_gen():
        try:
            async for chunk in _app_call('stream_llm_text', 
                messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                purpose=request.purpose,
                reasoning=request.reasoning,
            ):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': clip(str(exc), 300), 'provider': ''}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat")
async def chat(request: ChatRequest) -> dict[str, Any]:
    run = await asyncio.to_thread(_app_call, 'load_crawl_runtime', request.run_id)
    if not run:
        raise HTTPException(404, "任务不存在")
    if run["status"] != "completed":
        raise HTTPException(409, "请等爬取完成后再分析")
    if not _app_call('llm_settings', )["configured"]:
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM")
    durable = await asyncio.to_thread(_app_call, 'create_agent_run_record', 
        project_id="crawl4ai",
        parent_run_id=request.run_id,
        kind="chat",
        title=clip(request.message, 120),
        request={"crawl_run_id": request.run_id, "message": request.message, "has_live_context": bool(request.live_context.strip())},
        max_attempts=2,
    )
    if request.stream:
        async def event_gen():
            try:
                async for chunk in _app_call('stream_crawl_chat_turn', durable_run=durable, crawl_run=run, message=request.message, live_context=request.live_context):
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': clip(str(exc), 300), 'provider': ''}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    return await _app_call('run_crawl_chat_turn', durable_run=durable, crawl_run=run, message=request.message, live_context=request.live_context)


class CrossTabAskRequest(BaseModel):
    run_ids: list[str] = Field(min_length=2, max_length=6)
    question: str = Field(min_length=1, max_length=2000)


@app.post("/api/research/cross-tab")
async def post_cross_tab_ask(request: CrossTabAskRequest) -> dict[str, Any]:
    """把几个已打开的标签一起问。

    这是「AI 浏览器」真正比「能自动点按钮」值钱的地方：一次读完的几个页面
    放在一起比较、对齐、找矛盾。原来页面上只能一个标签一个标签地问，得到几段
    互不相干的总结，再由人自己在脑子里拼——而拼这一步恰恰是最费劲的。

    刻意的约束：每条结论都必须标出它来自哪个标签，只有一个来源支持的说法要
    标成「仅 X 提到」。不这么要求的话，模型会把几个页面糅成一段听起来很权威、
    但没法追溯的通稿。
    """
    if not _app_call('llm_settings', )["configured"]:
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM")
    ids = list(dict.fromkeys(item.strip() for item in request.run_ids if item.strip()))
    if len(ids) < 2:
        raise HTTPException(400, "至少要选两个标签才谈得上对比")
    sources: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, run_id in enumerate(ids):
        run = await asyncio.to_thread(_app_call, 'load_crawl_runtime', run_id)
        if not run or run.get("status") != "completed":
            missing.append(run_id)
            continue
        documents = run.get("documents") or []
        primary = documents[0] if documents else {}
        sources.append({
            "label": f"标签 {index + 1}",
            "title": clip(str(primary.get("title") or run.get("title") or "未命名页面"), 120),
            "url": str(primary.get("url") or ""),
            "data_as_of": str(primary.get("data_as_of") or run.get("finished_at") or ""),
            "content": clip_for_llm(str(primary.get("markdown") or run.get("initial_analysis") or ""), 9_000),
        })
    if len(sources) < 2:
        raise HTTPException(
            409,
            f"只有 {len(sources)} 个标签读完了内容，凑不成对比。等这些标签的 AI 阅读跑完再试。",
        )
    prompt = "\n\n".join(
        f"【{item['label']}】{item['title']}\n来源：{item['url'] or '（无链接）'}\n数据时间：{item['data_as_of'] or '未知'}\n{item['content']}"
        for item in sources
    )
    answer = await _app_call('call_llm', 
        [
            {"role": "system", "content": (
                "你在同时阅读用户打开的多个网页，回答要建立在这些页面的真实内容上。规则："
                "① 每一条结论后面标出它来自哪几个标签，例如「（标签 1、标签 3）」；"
                "② 只有一个来源支持的说法，标成「仅标签 N 提到」；"
                "③ 各来源互相矛盾时必须单独列出矛盾点，不要挑一个当作事实；"
                "④ 页面里没有的内容就说没有，不要用常识补全；"
                "⑤ 注意数据时间，旧页面的结论不要当成现状。"
                "输出顺序：一句话结论 → 共识 → 分歧与矛盾 → 只有单一来源支持的 → 还缺什么。"
            )},
            {"role": "user", "content": f"我的问题：{request.question}\n\n以下是这些标签的内容：\n\n{prompt}"},
        ],
        max_tokens=2_200,
        temperature=0.2,
        purpose="cross_tab_ask",
    )
    return {
        "answer": answer,
        "sources": [{k: v for k, v in item.items() if k != "content"} for item in sources],
        "skipped": missing,
    }


__all__ = [
    "ChatRequest",
    "ChatStreamRequest",
    "CrossTabAskRequest",
    "chat",
    "chat_stream",
    "post_cross_tab_ask",
]
