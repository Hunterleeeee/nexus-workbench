from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import functools
import hashlib
import html as html_lib
import urllib.parse
import ipaddress
import importlib.util
import io
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sqlite3
import statistics
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from app_pkg.instance import app
# 拆分里程碑：路径/日志/版本/限额等内核已迁入 app_pkg.core（为开源准备）。
# `from app_pkg.core import *` 保证下面 3 万行代码里的符号引用全部不变。
from app_pkg.core import *  # noqa: F401,F403
# Agent 运行参数（全部可通过环境变量调，无需改代码）
AGENT_CHILD_CONCURRENCY = _int_env("WORKBENCH_AGENT_CHILD_CONCURRENCY", 3, minimum=1, maximum=8)
AGENT_MAX_TOOL_ROUNDS = _int_env("WORKBENCH_AGENT_MAX_TOOL_ROUNDS", 4, minimum=1, maximum=8)
AGENT_TOOL_TIMEOUT_SECONDS = _int_env("WORKBENCH_AGENT_TOOL_TIMEOUT_SECONDS", 60, minimum=5, maximum=600)
AGENT_CHILD_TIMEOUT_SECONDS = _int_env("WORKBENCH_AGENT_CHILD_TIMEOUT_SECONDS", 420, minimum=30, maximum=1800)
AGENT_MAX_PARALLEL_TOOL_CALLS = _int_env("WORKBENCH_AGENT_MAX_PARALLEL_TOOL_CALLS", 4, minimum=1, maximum=8)

# DB 层已拆到 app_pkg.db；DB_SCHEMA_VERSION/_SharedConnection 是测试引用的
# 兼容别名（测试仍从 app 模块取这两个符号）。
from app_pkg.db import *  # noqa: F401,F403

# 兼容标志：schema 是否已初始化。保留在 app 模块是因为测试大量 patch 它；
# app_pkg.db._ensure_db_schema() 通过延迟 import 读写这里的值。
_DB_SCHEMA_READY = False

from app_pkg.push import *  # noqa: F401,F403
from app_pkg.git import *  # noqa: F401,F403
from app_pkg.sub2api import *  # noqa: F401,F403
from app_pkg.server import *  # noqa: F401,F403
from app_pkg.integrations import *  # noqa: F401,F403
from app_pkg.inbox import *  # noqa: F401,F403
from app_pkg.memories import *  # noqa: F401,F403
from app_pkg.notifications import *  # noqa: F401,F403
from app_pkg.usage import *  # noqa: F401,F403
from app_pkg.llm import *  # noqa: F401,F403
from app_pkg.agent_platform import *  # noqa: F401,F403
from app_pkg.automations import *  # noqa: F401,F403
from app_pkg.evidence import *  # noqa: F401,F403
from app_pkg.projects import *  # noqa: F401,F403
from app_pkg.market import *  # noqa: F401,F403
from app_pkg.agent_runs import *  # noqa: F401,F403
from app_pkg.agent_engine import *  # noqa: F401,F403
from app_pkg.knowledge import *  # noqa: F401,F403
from app_pkg.ai_learning import *  # noqa: F401,F403
from app_pkg.idea_analysis import *  # noqa: F401,F403
from app_pkg.product_manager import *  # noqa: F401,F403
from app_pkg.aihot import *  # noqa: F401,F403
from app_pkg.doc_factory import *  # noqa: F401,F403
from app_pkg.browser import *  # noqa: F401,F403
from app_pkg.artifacts import *  # noqa: F401,F403
from app_pkg.cloud_dev import *  # noqa: F401,F403
from app_pkg.feishu_events import *  # noqa: F401,F403

# Load the environment before importing modules that snapshot their settings at
# import time (notably the Feishu client). systemd EnvironmentFile remains the
# production source of truth, while local .env now behaves consistently too.
import feishu as feishu_bot
import cloud_dev
import cloud_patch
# Multiple sources can be configured via a comma-separated list. Order is preserved;
# sources are fetched in parallel and merged with the existing dedupe rule.
# 默认源 = 国外 AI 聚合 + Hacker News + 国内科技媒体 + 综合财经/商业/时政。
# AI 专属源（aihot.today / hn rss）仍按 AI 关键词过滤；综合源全保留并按源打领域标签，
# 让"热点雷达"覆盖全领域而不是只有 AI（见 _aihot_relevant / _aihot_domain）。


def _strip_html(value: str) -> str:
    from html.parser import HTMLParser as _HTMLParser
    class _Stripper(_HTMLParser):
        def __init__(self):
            super().__init__()
            self._buf: list[str] = []
        def handle_data(self, data):
            self._buf.append(data)
    if not value:
        return ""
    parser = _Stripper()
    try:
        parser.feed(value)
    except Exception:
        return value
    return " ".join("".join(parser._buf).split())




def bind_feishu_chat(chat_id: str, user_open_id: str = "", user_name: str = "") -> None:
    if not chat_id:
        return
    connection = db_connection()
    try:
        connection.execute(
            """INSERT INTO feishu_bindings (chat_id, user_open_id, user_name, bound_at, last_active_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET user_open_id = excluded.user_open_id, user_name = excluded.user_name, last_active_at = excluded.last_active_at""",
            (chat_id, user_open_id, user_name, now_iso(), now_iso()),
        )
        connection.commit()
    finally:
        connection.close()


def claim_feishu_event(payload: dict[str, Any], *, retention_days: int = 7) -> bool:
    """Atomically claim one Feishu event and suppress provider retries.

    Feishu retries callbacks when the first response is slow or unavailable.
    Without a receipt, a single ``云开发 ... 运行测试`` message could enqueue
    the same test more than once. Events without a provider id are allowed
    through because there is no safe stable key to deduplicate.
    """
    event_key = feishu_bot.event_receipt_key(payload)
    if not event_key:
        return True
    received_at = now_iso()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))).isoformat()
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_type = str(header.get("event_type") or payload.get("type") or "")[:120]
    connection = db_connection()
    try:
        connection.execute("DELETE FROM feishu_event_receipts WHERE received_at < ?", (cutoff,))
        cursor = connection.execute(
            "INSERT OR IGNORE INTO feishu_event_receipts (event_key, event_type, received_at) VALUES (?, ?, ?)",
            (event_key, event_type, received_at),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()










class ChatRequest(BaseModel):
    run_id: str
    message: str = Field(min_length=1, max_length=8000)
    live_context: str = Field(default="", max_length=12_000)
    stream: bool = Field(default=False, description="true 时返回 SSE 流式输出")







class WorkbenchDecisionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    decision: str = Field(min_length=1, max_length=2_000)
    rationale: str = Field(default="", max_length=8_000)
    next_steps: list[str] = Field(default_factory=list, max_length=20)
    project_ids: list[str] = Field(default_factory=list, max_length=12)
    confirmed: bool = False


class WorkbenchCollaborationRequest(BaseModel):
    confirmed: bool = False
    include_failed: bool = True
    include_blocked: bool = True
    limit: int = Field(default=8, ge=1, le=20)




class ServerActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=60)
    reason: str = Field(default="", max_length=2_000)
    confirmed: bool = False


class CloudDevRequest(BaseModel):
    command: str = Field(min_length=1, max_length=400)
    confirmed: bool = False


class WorkerHeartbeatRequest(BaseModel):
    status: str = Field(default="ready", max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clip(value: str | None, limit: int) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    return value[:limit] + "\n\n[…内容已截断…]"


def clip_for_llm(value: str | None, limit: int) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    head = int(limit * 0.72)
    tail = limit - head
    return value[:head] + "\n\n[…中间内容已压缩…]\n\n" + value[-tail:]












# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared LLM HTTP client.
#
# Every LLM call used to build its own ``httpx.AsyncClient``, which meant a fresh
# TCP + TLS handshake per request.  A single dispatch fans out to several child
# Agents, each running up to a few ReAct tool rounds, so one user message could
# pay that handshake ten-plus times.  A pooled client keeps the connections warm.
#
# Timeouts are split rather than flat: a dead endpoint now fails over to the next
# provider after ``connect`` seconds instead of holding the run for two minutes,
# while long generations still get the full read budget.
# ---------------------------------------------------------------------------
async def close_llm_http_clients() -> None:
    async with _LLM_HTTP_CLIENT_LOCK:
        for client in list(_LLM_HTTP_CLIENTS.values()):
            try:
                await client.aclose()
            except Exception:
                log.warning("关闭 LLM HTTP 客户端失败", exc_info=True)
        _LLM_HTTP_CLIENTS.clear()




class LLMStreamError(RuntimeError):
    """流式 LLM 调用失败的统一错误（带 provider 名，便于 failover 提示）。"""



AGENT_RESULT_CONTRACT_VERSION = "1.1"






























@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "workbench.html")


@app.get("/crawl4ai")
async def crawl4ai_studio() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/projects/web-research")
async def web_research_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "web-research.html")


@app.get("/projects/cloud-dev")
async def cloud_dev_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "cloud-dev.html")


@app.get("/projects/cid-dashboard")
async def cid_dashboard_shell() -> FileResponse:
    return FileResponse(STATIC_DIR / "project-shell.html")


@app.get("/projects/inbox")
async def inbox_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "inbox.html")


@app.get("/projects/knowledge")
async def knowledge_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "knowledge.html")


@app.get("/projects/doc-factory")
async def document_factory_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "doc-factory.html")


@app.get("/projects/sub2api")
async def sub2api_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "sub2api.html")


@app.get("/projects/market")
async def market_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "market.html")


@app.get("/projects/aihot")
async def aihot_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "aihot.html")


@app.get("/projects/embodied")
async def embodied_page() -> FileResponse:
    """具身智能改为复用学习页模板。

    原页面是 78 行静态 HTML：一份写死的书单加一个笔记框，没有任何后端
    （/api/embodied 从来不存在），也就没有课程、进度、自测和批改——这正是
    「没起到学习作用」的原因。现在它和 AI 转型学习共用同一套课程机制，
    只是换一条 track。
    """
    return FileResponse(STATIC_DIR / "ai-learning.html")


@app.get("/projects/ai-learning")
async def ai_learning_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "ai-learning.html")








@app.get("/projects/idea-analysis")
async def idea_analysis_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "idea-analysis.html")


@app.get("/projects/server")
async def server_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "server.html")


@app.get("/automation")
async def automation_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "automation.html")


@app.get("/git")
async def git_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "git.html")


@app.get("/github-tools")
async def github_tools_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "github-tools.html")


@app.get("/approvals")
async def approvals_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "approvals.html")


@app.get("/projects/cid-dashboard-source")
async def cid_dashboard_source() -> FileResponse:
    project = next((item for item in load_projects() if item.get("id") == "cid-dashboard"), None)
    if not project:
        raise HTTPException(404, "项目配置不存在")
    source_path = os.getenv(project.get("source_env", ""), project.get("source_path", ""))
    source = Path(source_path)
    if not source.is_file():
        # projects.json 里配的是开发机上的绝对路径（/Users/…）。换一台机器
        # ——服务器、另一个 checkout、CI——这个路径都不存在，iframe 直接 404，
        # 而看板是整页的主体内容，页面等于空白。除非有人记得在 .env 里设
        # WORKBENCH_CID_DASHBOARD_FILE，否则没人会想到去查这个。
        # 兜底回落到本仓库内的同名文件；仍然强制解析后必须落在 projects/ 目录
        # 之内，不因为兜底而放宽读文件的边界。
        fallback = (ROOT / "projects" / Path(source_path or "").name).resolve()
        projects_root = (ROOT / "projects").resolve()
        if source_path and fallback.is_file() and fallback.is_relative_to(projects_root):
            source = fallback
        else:
            raise HTTPException(404, f"项目文件不存在：{source}")
    return FileResponse(source, media_type="text/html")


@app.get("/api/search")
def search_workspace(q: str = "", limit: int = 24) -> dict[str, Any]:
    """Search projects plus the work items and artifacts that need action."""
    query = clip(str(q or "").strip(), 120)
    if len(query) < 2:
        return {"query": query, "results": []}
    needle = f"%{query.lower()}%"
    results: list[dict[str, Any]] = []
    for project in load_projects():
        haystack = " ".join(str(project.get(key) or "") for key in ("id", "title", "description", "meta")).lower()
        if query.lower() in haystack:
            results.append({"type": "project", "type_label": "项目", "title": project.get("title") or project.get("id"), "description": project.get("description", ""), "href": project.get("href") or "/"})
    connection = db_connection()
    try:
        work_items = connection.execute(
            "SELECT id, title, description, status, target_project FROM work_items WHERE lower(title) LIKE ? OR lower(description) LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (needle, needle, max(1, min(limit, 50))),
        ).fetchall()
        artifacts = connection.execute(
            "SELECT id, project_id, name, kind, created_at FROM artifacts WHERE lower(name) LIKE ? OR lower(kind) LIKE ? ORDER BY created_at DESC LIMIT ?",
            (needle, needle, max(1, min(limit, 50))),
        ).fetchall()
    finally:
        connection.close()
    for row in work_items:
        item = dict(row)
        target = str(item.get("target_project") or "").split(",")[0].strip() or "inbox"
        href = project_href(target)
        results.append({"type": "work_item", "type_label": "工作项", "title": item.get("title") or "未命名工作项", "description": item.get("description") or item.get("status") or "", "href": href + ("&" if "?" in href else "?") + "focus=agent"})
    for row in artifacts:
        item = dict(row)
        project_id = str(item.get("project_id") or "workbench")
        results.append({"type": "artifact", "type_label": "产物", "title": item.get("name") or "未命名产物", "description": f"{agent_display_name(project_id)} · {item.get('kind') or 'Artifact'}", "href": project_href(project_id)})
    return {"query": query, "results": results[: max(1, min(limit, 50))]}






@app.post("/api/knowledge")
def create_knowledge_note(request: InboxRequest) -> dict[str, Any]:
    source = request.source.strip()
    source_inbox_id = 0
    match = re.fullmatch(r"inbox:(\d+)", source)
    if match:
        source_inbox_id = int(match.group(1))
        if not get_inbox_record(source_inbox_id):
            raise HTTPException(404, "来源收件箱条目不存在")
    metadata = {"source": source} if source else {}
    if source_inbox_id:
        metadata["source_inbox_id"] = source_inbox_id
    note = write_knowledge_note(request.kind or "未命名笔记", request.content, metadata=metadata, artifact_kind="inbox_handoff_note" if source_inbox_id else "knowledge_note")
    artifact = note.get("artifact") or {}
    if source_inbox_id and artifact.get("id"):
        note["relation"] = create_relation_record(
            from_type="inbox",
            from_id=str(source_inbox_id),
            to_type="artifact",
            to_id=str(artifact["id"]),
            relation_type="captured_as_knowledge",
            metadata={"source": source},
        )
    return {"note": note}


@app.get("/api/notifications")
def get_notifications(unread_only: bool = False, limit: int = 30) -> dict[str, Any]:
    return {"notifications": list_notifications(unread_only=unread_only, limit=limit)}


@app.get("/api/feishu")
def feishu_status() -> dict[str, Any]:
    """飞书接入状态（只返回脱敏信息）。"""
    return {
        "configured": feishu_bot.configured(),
        "bindings": [{"chat_id": item.get("chat_id"), "user_name": item.get("user_name"), "last_active_at": item.get("last_active_at")} for item in feishu_bindings()],
        "verify_token_set": bool(feishu_bot.VERIFY_TOKEN),
        "encrypt_key_set": bool(feishu_bot.ENCRYPT_KEY),
    }







@app.post("/api/notifications/{notification_id}/read")
def read_notification(notification_id: int) -> dict[str, Any]:
    notification = mark_notification_read(notification_id)
    if not notification:
        raise HTTPException(404, "通知不存在")
    return {"notification": notification}


@app.post("/api/notifications/read-all")
def read_all_notifications() -> dict[str, Any]:
    return {"ok": True, "count": mark_all_notifications_read()}


@app.post("/api/handoffs")
def create_handoff(request: HandoffRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(409, "项目交接需要确认后才能创建工作项")
    if request.from_project not in AGENT_REGISTRY:
        raise HTTPException(400, "来源项目 Agent 不存在")
    if request.to_project not in AGENT_REGISTRY or request.to_project == "workbench":
        raise HTTPException(400, "目标项目 Agent 不存在")
    item = create_work_item_record(
        title=request.title,
        description=request.description,
        kind="handoff",
        priority=request.priority,
        source_project=request.from_project,
        target_project=request.to_project,
        metadata=request.metadata,
    )
    relation = create_relation_record(
        from_type="project",
        from_id=request.from_project,
        to_type="work_item",
        to_id=str(item["id"]),
        relation_type="handoff",
        metadata={"to_project": request.to_project},
    )
    return {"item": item, "relation": relation}




@app.get("/api/outputs")
async def list_outputs() -> dict[str, Any]:
    files = []
    for path in sorted(OUTPUTS_DIR.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_file() or path.name.startswith("."):
            continue
        files.append({"name": path.name, "size": path.stat().st_size, "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()})
    return {"outputs": files[:100]}


@app.get("/api/outputs/{name}/content")
def get_output_content(name: str) -> dict[str, Any]:
    safe_name = Path(name).name
    path = OUTPUTS_DIR / safe_name
    if path.parent != OUTPUTS_DIR or not path.is_file():
        raise HTTPException(404, "产物文件不存在")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise HTTPException(409, "无法读取产物文件（可能不是文本文件）")
    return {"name": safe_name, "content": content[:100_000]}












@app.get("/api/meta")
async def get_meta() -> dict[str, Any]:
    return {"name": "Workbench", "version": WORKBENCH_VERSION, "data_dir": str(DATA_DIR)}






@app.get("/api/workers")
def get_workers() -> dict[str, Any]:
    return {"instance_id": worker_instance_id(), "workers": worker_status_payload(), "lease_seconds": WORKER_LEASE_SECONDS, "policy": "同一 Worker 通过 SQLite 短租约避免多实例重复执行；过期租约可被新实例接管。"}


@app.get("/api/trace/recent")
def get_recent_trace(limit: int = 24) -> dict[str, Any]:
    """Provide one compact, body-free activity feed across the core records.

    面向日常使用者输出「人话动态」：每条记录用一句话说明发生了什么，
    过滤掉纯内部对象（关系边、空工作项等）噪声。
    """
    limit = max(4, min(int(limit or 24), 80))
    connection = db_connection()
    try:
        work_items = connection.execute(
            "SELECT id, source_project, target_project, title, kind, status, updated_at, created_at FROM work_items ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        runs = connection.execute(
            "SELECT id, project_id, kind, title, status, error, updated_at, created_at FROM agent_runs ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        artifacts = connection.execute(
            "SELECT id, project_id, name, kind, created_at FROM artifacts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    project_names = {item.get("id"): item.get("title") or item.get("id") for item in load_projects()}
    status_names = {"open": "待处理", "running": "处理中", "blocked": "待确认", "failed": "失败", "done": "已完成", "archived": "已归档", "succeeded": "成功", "partial": "部分完成", "queued": "排队中", "cancelled": "已取消"}

    def project_label(project_id: str) -> str:
        return project_names.get(project_id, project_id or "工作台")

    items: list[dict[str, Any]] = []

    # 工作项 → 人话动态（过滤纯内部证据验收与空标题）
    for row in work_items:
        title = str(row["title"] or "").strip()
        kind = str(row["kind"] or "")
        if not title or kind == "evidence_acceptance":
            continue
        status = str(row["status"] or "open")
        project_id = str(row["target_project"] or row["source_project"] or "workbench")
        action = "新增待办" if status == "open" else status_names.get(status, status)
        items.append({
            "type": "work_item",
            "title": f"{action}：{title}",
            "status": status_names.get(status, status),
            "project_id": project_id,
            "project_label": project_label(project_id),
            "updated_at": str(row["updated_at"] or row["created_at"] or ""),
            "href": "/" if project_id == "workbench" else project_href(project_id),
        })

    # Agent 运行 → 人话动态
    for row in runs:
        kind = str(row["kind"] or "")
        if kind in {"dispatch_child", "evidence_acceptance", "manual_takeover"}:
            continue
        status = str(row["status"] or "queued")
        title = str(row["title"] or "").strip()
        project_id = str(row["project_id"] or "workbench")
        verb = "运行成功" if status == "succeeded" else "运行失败" if status == "failed" else "部分完成" if status == "partial" else "开始运行" if status == "running" else "已取消" if status == "cancelled" else "排队等待"
        items.append({
            "type": "run",
            "title": f"{project_label(project_id)} Agent {verb}：{title or '任务'}",
            "status": status_names.get(status, status),
            "project_id": project_id,
            "project_label": project_label(project_id),
            "updated_at": str(row["updated_at"] or row["created_at"] or ""),
            "detail": str(row["error"] or "") if status == "failed" else "",
            "href": f"/api/agent/{row['project_id']}/runs/{row['id']}",
        })

    # 产物 → 人话动态（过滤定期快照等自动化产物，避免刷屏）
    snapshot_artifacts = {"server_monitor_snapshot.json", "sub2api_snapshot.json", "aihot_snapshot.json", "cid_snapshot.json"}
    for row in artifacts:
        name = str(row["name"] or "").strip()
        kind = str(row["kind"] or "")
        if not name or name.startswith("crawl-result") or name in snapshot_artifacts:
            continue
        project_id = str(row["project_id"] or "workbench")
        items.append({
            "type": "artifact",
            "title": f"保存了产物：{name}",
            "status": kind or "产物",
            "project_id": project_id,
            "project_label": project_label(project_id),
            "updated_at": str(row["created_at"] or ""),
            "href": "/" if project_id == "workbench" else project_href(project_id),
        })

    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return {"items": items[:limit], "generated_at": now_iso(), "policy": "面向日常使用者的人话动态流：说明发生了什么，不含内部请求/响应正文。"}


@app.post("/api/workers/{worker_id}/heartbeat")
def heartbeat_worker(worker_id: str, request: WorkerHeartbeatRequest) -> dict[str, Any]:
    try:
        worker = worker_lease(worker_id, status=request.status.strip() or "ready", metadata=request.metadata)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if worker.get("status") == "held_by_other_instance":
        raise HTTPException(409, f"Worker {worker_id} 当前由其他实例持有")
    return {"ok": True, "worker": worker, "workers": worker_status_payload()}


@app.get("/api/health")
def health() -> dict[str, Any]:
    # 只查「是否安装」不执行导入：import crawl4ai 会连带加载 numpy/scipy/
    # onnxruntime 全家桶（约 80MB），而健康检查被部署脚本/监控频繁调用，
    # 第一次就让它常驻主进程。真实抓取走 run_crawl 里的函数级懒加载。
    try:
        crawl4ai_available = importlib.util.find_spec("crawl4ai") is not None
    except (ImportError, ValueError):
        crawl4ai_available = False
    return {"ok": True, "version": WORKBENCH_VERSION, "crawl4ai_available": crawl4ai_available, "llm": llm_settings()}


def enqueue_crawl_request(request: CrawlRequest, *, research_plan_id: str = "", parent_run_id: str = "") -> dict[str, Any]:
    urls = []
    for raw in request.urls:
        value = raw.strip()
        if value and value not in urls:
            urls.append(value)
    if not urls:
        raise HTTPException(400, "至少填写一个 URL")
    invalid = [url for url in urls if not valid_research_url(url)]
    if invalid:
        raise HTTPException(400, f"只支持 http/https URL：{invalid[0]}")

    payload = crawl_request_payload(request, urls)
    if research_plan_id:
        payload["research_plan_id"] = research_plan_id
    if parent_run_id:
        payload["parent_run_id"] = parent_run_id
    durable = create_agent_run_record(
        project_id="crawl4ai",
        kind="crawl",
        title=f"网页研究：{clip(request.task or urls[0], 100)}",
        request=payload,
        max_attempts=2,
    )
    work_item = create_work_item_record(
        title=f"网页研究：{clip(request.task or urls[0], 100)}",
        description=request.task.strip() or "抓取并整理网页证据",
        kind="research",
        status="running",
        source_project="workbench",
        target_project="crawl4ai",
        metadata={"run_id": durable["id"], "urls": urls, "max_pages": request.max_pages, "research_plan_id": research_plan_id, "parent_run_id": parent_run_id},
    )
    create_relation_record(from_type="agent_run", from_id=durable["id"], to_type="work_item", to_id=work_item["id"], relation_type="tracks", metadata={"project_id": "crawl4ai"})
    # 持久化 work_item_id 到 request_json，独立 Crawl Worker 领取时据此回写工作项状态
    durable["request"]["work_item_id"] = work_item["id"]
    update_agent_run_record(durable["id"], request=durable["request"])
    run_id = durable["id"]
    runs[run_id] = {
        "id": run_id,
        "status": "queued",
        "task": request.task.strip(),
        "urls": urls,
        "source_title": request.source_title.strip(),
        "source_context": request.source_context.strip(),
        "render_js": request.render_js,
        "refresh": request.refresh,
        "max_depth": request.max_depth,
        "max_pages": request.max_pages,
        "logs": [],
        "documents": [],
        "conversation": [],
        "created_at": now_iso(),
        "work_item_id": work_item["id"],
        "research_plan_id": research_plan_id,
    }
    # The API only records a durable queued Run. ``crawl_worker.py`` owns
    # execution so a web request restart cannot lose the task or keep it tied
    # to the Core API process.
    return {"run_id": run_id, "work_item_id": work_item["id"]}


@app.post("/api/runs")
def create_run(request: CrawlRequest) -> dict[str, Any]:
    return enqueue_crawl_request(request)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = load_crawl_runtime(run_id)
    if not run:
        raise HTTPException(404, "任务不存在")
    return public_run(run)



# ---------------------------------------------------------------------------
# Web-research browser: context mentions, tab grouping and a bounded agent.
#
# These endpoints exist to make the research surface behave like an AI-native
# browser: pull anything into the conversation with @, keep many open pages
# organised, and let a goal drive the crawling instead of a URL list.  All of
# them stay read-only against the outside world -- the agent follows links and
# reads, it never submits a form or clicks a destructive control.
# ---------------------------------------------------------------------------




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
    if not llm_settings()["configured"]:
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
            async for chunk in stream_llm_text(
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
    run = await asyncio.to_thread(load_crawl_runtime, request.run_id)
    if not run:
        raise HTTPException(404, "任务不存在")
    if run["status"] != "completed":
        raise HTTPException(409, "请等爬取完成后再分析")
    if not llm_settings()["configured"]:
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM")
    durable = await asyncio.to_thread(create_agent_run_record, 
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
                async for chunk in stream_crawl_chat_turn(durable_run=durable, crawl_run=run, message=request.message, live_context=request.live_context):
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': clip(str(exc), 300), 'provider': ''}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    return await run_crawl_chat_turn(durable_run=durable, crawl_run=run, message=request.message, live_context=request.live_context)


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
    if not llm_settings()["configured"]:
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM")
    ids = list(dict.fromkeys(item.strip() for item in request.run_ids if item.strip()))
    if len(ids) < 2:
        raise HTTPException(400, "至少要选两个标签才谈得上对比")
    sources: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, run_id in enumerate(ids):
        run = await asyncio.to_thread(load_crawl_runtime, run_id)
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
    answer = await call_llm(
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


# ---------------------------------------------------------------------------
# Platform round: orchestration, automation, local Git, backups, delivery
# and verifiable evidence.  These endpoints are intentionally additive: they
# do not mutate the existing project data contracts.
# ---------------------------------------------------------------------------


def platform_decode_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        decoded = json.loads(str(value or ""))
        return decoded if decoded is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def platform_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None



def capability_graph_route(message: str, children: list[str] | None = None) -> list[str]:
    """Route by declared tools, freshness and current load, not keywords alone."""
    candidates = children or available_child_agents()
    lowered = str(message or "").lower()
    scored: list[tuple[float, str]] = []
    for project_id in candidates:
        if project_id not in AGENT_REGISTRY or project_id == "workbench":
            continue
        hints = AGENT_ROUTE_HINTS.get(project_id, ())
        playbook = AGENT_PLAYBOOKS.get(project_id, {})
        score = sum(3.0 for hint in hints if hint.lower() in lowered)
        score += sum(0.35 for term in str(playbook.get("mission", "")).lower().split() if term and term in lowered)
        detail = agent_detail(project_id, llm_ready=bool(llm_settings()["configured"]))
        tools = set(detail.get("tools") or detail.get("implemented_tools") or [])
        if any(term in lowered for term in ("最新", "同步", "刷新", "数据时间", "变化")):
            freshness = project_data_freshness(project_id)
            if freshness.get("status") in {"stale", "missing"}:
                score += 2.0
        if tools:
            score += min(1.5, len(tools) * 0.1)
        active = int((agent_run_summary(project_id) or {}).get("active", 0))
        score -= min(1.5, active * 0.25)
        if score > 0:
            scored.append((score, project_id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [project_id for _score, project_id in scored[:3]] or ["inbox"]












class ApprovalDecisionRequest(BaseModel):
    status: str = Field(pattern="^(approved|rejected|pending|changes_requested|resubmitted)$")
    reviewer_note: str = Field(default="", max_length=4_000)





@app.get("/api/agent/capability-graph")
def get_capability_graph() -> dict[str, Any]:
    return capability_graph_payload()


@app.get("/api/backups")
async def get_backups() -> dict[str, Any]:
    return {"backups": list_database_backups(), "database": str(DATABASE_FILE), "current": database_metadata()}


@app.post("/api/backups")
async def create_backup() -> dict[str, Any]:
    return {"backup": create_database_backup("manual")}


@app.post("/api/backups/{name}/restore")
async def restore_backup(name: str) -> dict[str, Any]:
    try:
        return restore_database_backup(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/system/architecture")
def get_system_architecture() -> dict[str, Any]:
    workers = worker_status_payload()
    worker_by_id = {worker["id"]: worker for worker in workers}

    def component_status(worker_id: str) -> str:
        worker = worker_by_id.get(worker_id) or {}
        if worker.get("stale"):
            return "stale"
        return str(worker.get("status") or "unclaimed")

    try:
        crawl4ai_available = importlib.util.find_spec("crawl4ai") is not None
    except (ImportError, ValueError):
        crawl4ai_available = False
    components = [
        {"id": "core-api", "label": "Core API", "status": "online", "scope": "FastAPI + SQLite"},
        {"id": "crawl-worker", "label": "Crawl Worker", "status": component_status("crawl-worker") if crawl4ai_available else "optional", "scope": "网页抓取与证据产物"},
        {"id": "sync-worker", "label": "Sync Worker", "status": component_status("sync-worker"), "scope": "AI 热点、Sub2API、行情自动化"},
        {"id": "monitor-worker", "label": "Monitor Worker", "status": component_status("monitor-worker"), "scope": "服务器巡检与告警"},
        {"id": "agent-worker", "label": "Agent Worker", "status": component_status("agent-worker"), "scope": "计划、重试、交接与 LLM"},
    ]
    return {
        "components": components,
        "workers": workers,
        "isolation": "每类任务拥有独立 Run 和错误边界；状态只以 Worker 心跳/租约为准，不把已注册当成正在运行。",
        "version": WORKBENCH_VERSION,
    }




def create_approval_request(project_id: str, kind: str, title: str, payload: dict[str, Any]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute("INSERT INTO approval_requests(id, project_id, kind, title, payload_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (request_id, project_id, kind, title, json.dumps(payload, ensure_ascii=False), timestamp, timestamp))
        connection.execute("INSERT INTO approval_events(approval_id, from_status, to_status, reviewer_note, created_at) VALUES (?, '', 'pending', '', ?)", (request_id, timestamp))
        connection.commit()
    finally:
        connection.close()
    return {"id": request_id, "project_id": project_id, "kind": kind, "title": title, "payload": payload, "status": "pending", "created_at": timestamp, "updated_at": timestamp}


@app.get("/api/approval-queue")
def get_approval_queue() -> dict[str, Any]:
    """统一待确认队列：聚合审批请求 + blocked 工作项 + 待确认 Agent 动作。"""
    connection = db_connection()
    items: list[dict[str, Any]] = []
    try:
        approvals = connection.execute(
            "SELECT id, project_id, title, kind, status, updated_at FROM approval_requests WHERE status IN ('pending', 'resubmitted') ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
        for row in approvals:
            items.append({"type": "approval", "id": str(row["id"]), "title": str(row["title"] or "审批请求"), "project_id": str(row["project_id"] or ""), "status": str(row["status"] or "pending"), "updated_at": str(row["updated_at"] or ""), "href": "/approvals"})
        blocked = connection.execute(
            "SELECT id, source_project, target_project, title, status, updated_at FROM work_items WHERE status = 'blocked' ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
        for row in blocked:
            items.append({"type": "work_item", "id": str(row["id"]), "title": str(row["title"] or "待确认工作项"), "project_id": str(row["source_project"] or ""), "target_project": str(row["target_project"] or ""), "status": "blocked", "updated_at": str(row["updated_at"] or ""), "href": "/#activity"})
        actions = connection.execute(
            "SELECT id, project_id, name, tool, status, risk, created_at FROM agent_actions WHERE requires_confirmation = 1 AND status = 'pending' ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        for row in actions:
            items.append({"type": "action", "id": str(row["id"]), "title": str(row["name"] or row["tool"] or "待确认动作"), "project_id": str(row["project_id"] or ""), "tool": str(row["tool"] or ""), "status": "pending", "updated_at": str(row["created_at"] or ""), "href": "/approvals"})
    finally:
        connection.close()
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return {"items": items, "total": len(items)}


@app.get("/api/approvals")
def get_approvals(status: str = "all") -> dict[str, Any]:
    connection = db_connection()
    try:
        query = "SELECT * FROM approval_requests"
        params: list[Any] = []
        if status != "all":
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT 100"
        rows = connection.execute(query, params).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["payload"] = platform_decode_json(item.pop("payload_json", "{}"), {})
            events = connection.execute("SELECT id, from_status, to_status, reviewer_note, run_id, created_at FROM approval_events WHERE approval_id = ? ORDER BY created_at ASC, id ASC", (item["id"],)).fetchall()
            item["history"] = [dict(event) for event in events]
            execution = connection.execute("SELECT * FROM server_action_executions WHERE approval_id = ? ORDER BY created_at DESC LIMIT 1", (item["id"],)).fetchone()
            item["execution"] = _server_action_execution_payload(execution)
            items.append(item)
        return {"approvals": items}
    finally:
        connection.close()


@app.patch("/api/approvals/{approval_id}")
def decide_approval(approval_id: str, request: ApprovalDecisionRequest) -> dict[str, Any]:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM approval_requests WHERE id = ?", (approval_id,)).fetchone()
        if not row:
            raise HTTPException(404, "审批请求不存在")
        previous_status = str(row["status"] or "pending")
        allowed_transitions = {
            "pending": {"approved", "rejected", "changes_requested"},
            "rejected": {"resubmitted", "pending"},
            "changes_requested": {"resubmitted", "pending"},
            "resubmitted": {"approved", "rejected", "changes_requested"},
            "approved": set(),
        }
        if request.status not in allowed_transitions.get(previous_status, set()):
            raise HTTPException(409, f"审批状态不能从“{previous_status}”变为“{request.status}”")
        timestamp = now_iso()
        approval_run = create_agent_run_record(
            project_id=str(row["project_id"] or "workbench"),
            kind="approval_decision",
            title=f"审批记录：{row['title']}",
            request={"approval_id": approval_id, "from_status": previous_status, "to_status": request.status, "reviewer_note": request.reviewer_note},
            max_attempts=1,
        )
        update_agent_run_record(approval_run["id"], status="running")
        connection.execute("UPDATE approval_requests SET status = ?, reviewer_note = ?, updated_at = ? WHERE id = ?", (request.status, request.reviewer_note, timestamp, approval_id))
        payload = platform_decode_json(row["payload_json"], {})
        for artifact_id in payload.get("delivery_artifacts", []) if isinstance(payload, dict) else []:
            artifact_row = connection.execute("SELECT metadata_json FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
            if not artifact_row:
                continue
            metadata = platform_decode_json(artifact_row["metadata_json"], {})
            metadata.update({"approval_status": request.status, "reviewer_note": request.reviewer_note, "approval_id": approval_id, "approved_at": timestamp if request.status == "approved" else metadata.get("approved_at", "")})
            connection.execute("UPDATE artifacts SET metadata_json = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False), artifact_id))
        connection.execute("INSERT INTO approval_events(approval_id, from_status, to_status, reviewer_note, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (approval_id, previous_status, request.status, request.reviewer_note, approval_run["id"], timestamp))
        connection.commit()
        update_agent_run_record(approval_run["id"], status="succeeded", result={"approval_id": approval_id, "from_status": previous_status, "to_status": request.status}, error="")
        create_relation_record(from_type="approval", from_id=approval_id, to_type="agent_run", to_id=approval_run["id"], relation_type="approval_event", metadata={"from_status": previous_status, "to_status": request.status})
        item = dict(row)
        item.update({"status": request.status, "reviewer_note": request.reviewer_note})
        item["payload"] = payload
        item["history"] = [dict(event) for event in connection.execute("SELECT id, from_status, to_status, reviewer_note, run_id, created_at FROM approval_events WHERE approval_id = ? ORDER BY created_at ASC, id ASC", (approval_id,)).fetchall()]
        return {"approval": item, "run": get_agent_run(approval_run["id"]), "policy": "批准只更新本地 Artifact 审批状态；服务器写入、删除、交易和外发动作不会因批准自动执行。"}
    finally:
        connection.close()



@app.get("/api/github-tools")
def get_github_tools() -> dict[str, Any]:
    markitdown = markitdown_status()
    tools = [
        {"id": "markitdown", "name": "Microsoft MarkItDown", "url": "https://github.com/microsoft/markitdown", "scenario": "文档、网页和演示材料转 Markdown", "cost": "免费·可选依赖", "fit": "已接入文档工厂；未安装时回退内置解析器", "trial": "在文档工厂上传一份 PPTX 或复杂表格，检查 Markdown 结构和来源保留", "state": "integrated", "installed": bool(markitdown.get("available")), "data_boundary": "文件只在 Workbench 进程内转换，不自动上传"},
        {"id": "activitywatch", "name": "ActivityWatch", "url": "https://github.com/ActivityWatch/activitywatch", "scenario": "个人时间和效率反馈", "cost": "免费·本地", "fit": "已接入近 7 天聚合观察，不保存窗口标题和 URL", "trial": "配置本机服务后导入一次聚合观察 WorkItem", "state": "integrated", "installed": None, "data_boundary": "只回传聚合时长、事件数量和数据时间"},
        {"id": "github-issues", "name": "GitHub Issues / Pull Requests", "url": "https://github.com/cli/cli", "scenario": "代码项目待办和评审", "cost": "免费·API", "fit": "已接入只读读取与收件箱导入", "trial": "配置一个仓库，读取开放 Issue/PR 并人工勾选导入", "state": "integrated", "installed": None, "data_boundary": "只读仓库条目；写操作仍需人工确认"},
        {"id": "zotero", "name": "Zotero", "url": "https://github.com/zotero/zotero", "scenario": "论文、资料和 DOI 学习入口", "cost": "免费·API", "fit": "已接入研究条目读取并导入知识库", "trial": "读取最近研究条目，人工选择后生成知识库工作项", "state": "integrated", "installed": None, "data_boundary": "只读取用户选定条目的元数据和摘要"},
        {"id": "linkding", "name": "Linkding", "url": "https://github.com/sissbruecker/linkding", "scenario": "低噪书签与稍后读入口", "cost": "免费·自托管", "fit": "已接入只读书签读取和人工勾选导入", "trial": "配置 Linkding 后导入一批待读书签，观察网页研究和知识库的分流质量", "state": "integrated", "installed": None, "data_boundary": "只读标题、链接、描述和标签；不修改书签"},
        {"id": "paperless", "name": "Paperless-ngx", "url": "https://github.com/paperless-ngx/paperless-ngx", "scenario": "个人文档归档与资料再利用", "cost": "免费·自托管", "fit": "已接入文档元数据读取和人工导入知识库", "trial": "配置 Paperless-ngx 后选择几份文档，验证来源和数据时间是否保留", "state": "integrated", "installed": None, "data_boundary": "只读文档元数据；不自动下载或修改归档文件"},
        {"id": "searxng", "name": "SearXNG", "url": "https://github.com/searxng/searxng", "scenario": "隐私友好的学习资料搜索", "cost": "免费·自托管", "fit": "已接入搜索结果读取和人工选择进入网页研究", "trial": "配置一个 SearXNG 实例，搜索一个学习主题并人工选择结果进入网页研究", "state": "integrated", "installed": None, "data_boundary": "只读聚合搜索结果；不保存原始搜索日志，不自动抓取全文"},
        {"id": "wallabag", "name": "Wallabag", "url": "https://github.com/wallabag/wallabag", "scenario": "稍后读文章进入学习流程", "cost": "免费·自托管", "fit": "已接入未归档文章读取和人工选择进入网页研究", "trial": "配置 Access Token，选择一篇稍后读文章进入网页研究，验证来源回溯", "state": "integrated", "installed": None, "data_boundary": "只读文章元数据和摘要；不修改 Wallabag 的归档状态"},
        {"id": "lazygit", "name": "lazygit", "url": "https://github.com/jesseduffield/lazygit", "scenario": "终端 Git 审查与提交", "cost": "免费·本地", "fit": "Workbench 已有只读 Git 项目中心；lazygit 作为本机审查补充", "trial": "安装后扫描一个仓库并完成一次分支审查；Workbench 不自动执行提交或 push", "state": "candidate", "installed": bool(shutil.which("lazygit")), "data_boundary": "本地 Git 元数据；不要把密钥或完整 diff 放入通知"},
        {"id": "super-productivity", "name": "Super Productivity", "url": "https://github.com/super-productivity/super-productivity", "scenario": "任务执行、时间记录和学习计划", "cost": "免费·本地", "fit": "仅作为单向导入候选，不替代 Workbench 收件箱主库", "trial": "先导出一份任务 JSON，验证去重、截止时间和来源映射", "state": "candidate", "installed": None, "data_boundary": "只读导出；不做双向同步，不创建第二套主任务库"},
    ]
    connection = db_connection()
    try:
        rows = connection.execute("SELECT * FROM work_items WHERE kind = 'github_tool_trial' ORDER BY created_at DESC LIMIT 50").fetchall()
        trials = [work_item_row(row) for row in rows]
    finally:
        connection.close()
    return {
        "tools": tools,
        "trials": trials,
        "integrations": [integration_status(integration_id) for integration_id in INTEGRATION_DEFINITIONS],
        "generated_at": now_iso(),
    }


@app.post("/api/github-tools/{tool_id}/trial")
async def create_github_tool_trial(tool_id: str) -> dict[str, Any]:
    catalog = await asyncio.to_thread(get_github_tools)
    tool = next((item for item in catalog["tools"] if item["id"] == tool_id), None)
    if not tool:
        raise HTTPException(404, "GitHub 工具不存在")
    item = create_work_item_record(title=f"试用 GitHub 工具：{tool['name']}", description=f"场景：{tool['scenario']}\n试用建议：{tool['trial']}\n仓库：{tool['url']}", kind="github_tool_trial", source_project="workbench", target_project="workbench", metadata={"tool": tool, "repo_path": ""})
    return {"ok": True, "item": item}


def set_docx_run_font(run: Any, name: str = "Hiragino Sans GB", size: float = 11, color: str = "1F2937", bold: bool | None = None) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:cs"), name)
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        run.bold = bold


def set_docx_cell_shading(cell: Any, fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def build_docx_delivery(title: str, text: str, target: Path) -> None:
    from docx import Document
    from docx.enum.section import WD_SECTION
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    document = Document()
    section = document.sections[0]
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)
    normal = document.styles["Normal"]
    normal.font.name = "Hiragino Sans GB"
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.1
    for name, size, color, before, after in (("Heading 1", 16, "2E74B5", 16, 8), ("Heading 2", 13, "2E74B5", 12, 6), ("Heading 3", 12, "1F4D78", 8, 4)):
        style = document.styles[name]
        style.font.name = "Hiragino Sans GB"
        style._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), "Hiragino Sans GB")
        style._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), "Hiragino Sans GB")
        style._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Hiragino Sans GB")
        style._element.get_or_add_rPr().rFonts.set(qn("w:cs"), "Hiragino Sans GB")
        style.font.size = Pt(size)
        style.font.color.rgb = __import__("docx").shared.RGBColor.from_string(color)
        style.font.bold = True
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.line_spacing = 1.1
    header = section.header.paragraphs[0]
    header.text = "Workbench · 文档交付包"
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in header.runs:
        set_docx_run_font(run, size=9, color="6B7280")
    footer = section.footer.paragraphs[0]
    footer.text = f"{WORKBENCH_VERSION} · 生成于 {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}"
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in footer.runs:
        set_docx_run_font(run, size=8.5, color="6B7280")
    title_paragraph = document.add_paragraph()
    title_paragraph.paragraph_format.space_after = Pt(4)
    title_run = title_paragraph.add_run(title or "未命名文档")
    set_docx_run_font(title_run, size=23, color="0B2545", bold=True)
    meta = document.add_paragraph()
    meta.paragraph_format.space_after = Pt(14)
    meta_run = meta.add_run(f"Workbench 正式交付草稿 · 版本 {WORKBENCH_VERSION}")
    set_docx_run_font(meta_run, size=10, color="6B7280")
    rule = document.add_paragraph()
    rule.paragraph_format.space_after = Pt(12)
    p_pr = rule._p.get_or_add_pPr()
    from docx.oxml import OxmlElement
    p_bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "8")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "D8DEE8")
    p_bdr.append(bottom)
    p_pr.append(p_bdr)

    def add_markdown_runs(paragraph: Any, content: str) -> None:
        """Render the small Markdown subset accepted by the document factory."""
        chunks = re.split(r"(\*\*.+?\*\*)", str(content or ""))
        for chunk in chunks:
            if not chunk:
                continue
            is_bold = chunk.startswith("**") and chunk.endswith("**") and len(chunk) >= 4
            value = chunk[2:-2] if is_bold else chunk
            run = paragraph.add_run(value)
            set_docx_run_font(run, bold=is_bold)

    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            document.add_paragraph().paragraph_format.space_after = Pt(2)
            continue
        if line in {"*", "_"}:
            continue
        if line.startswith("### "):
            document.add_paragraph(line[4:], style="Heading 3")
        elif line.startswith("## "):
            document.add_paragraph(line[3:], style="Heading 2")
        elif line.startswith("# "):
            document.add_paragraph(line[2:], style="Heading 1")
        elif line.startswith(("- ", "* ")):
            paragraph = document.add_paragraph(style="List Bullet")
            paragraph.paragraph_format.left_indent = Inches(0.5)
            paragraph.paragraph_format.first_line_indent = Inches(-0.25)
            paragraph.paragraph_format.space_after = Pt(8)
            add_markdown_runs(paragraph, line[2:])
        elif re.match(r"^\d+[.)] ", line):
            paragraph = document.add_paragraph(style="List Number")
            paragraph.paragraph_format.left_indent = Inches(0.5)
            paragraph.paragraph_format.first_line_indent = Inches(-0.25)
            paragraph.paragraph_format.space_after = Pt(8)
            add_markdown_runs(paragraph, re.sub(r"^\d+[.)] ", "", line))
        else:
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(6)
            paragraph.paragraph_format.line_spacing = 1.1
            add_markdown_runs(paragraph, line)
    document.save(target)


def convert_docx_to_pdf(docx_path: Path, pdf_path: Path) -> tuple[bool, str]:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return False, "未找到 LibreOffice/soffice"
    temp_dir = pdf_path.parent / f".pdf-convert-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        # The bundled headless LibreOffice build does not always inherit the
        # macOS font database, so Chinese TTC fonts can otherwise become
        # missing-glyph boxes.  Give fontconfig an explicit, per-conversion
        # search path.  WORKBENCH_DOCX_FONT_DIR is also supported for Linux
        # deployments where a CJK font package is mounted separately.
        font_dirs: list[Path] = []
        configured_font_dir = os.getenv("WORKBENCH_DOCX_FONT_DIR", "").strip()
        if configured_font_dir:
            font_dirs.append(Path(configured_font_dir).expanduser())
        font_dirs.extend(
            Path(path)
            for path in (
                "/System/Library/Fonts",
                "/System/Library/Fonts/Supplemental",
                "/Library/Fonts",
                str(Path.home() / "Library" / "Fonts"),
                "/usr/share/fonts",
                "/usr/local/share/fonts",
            )
        )
        font_dirs = [path for path in font_dirs if path.exists() and path.is_dir()]
        conversion_env = os.environ.copy()
        if font_dirs:
            fontconfig_path = temp_dir / "fontconfig.conf"
            fontconfig_dirs = "".join(f"    <dir>{str(path)}</dir>\n" for path in font_dirs)
            fontconfig_path.write_text(
                "<?xml version=\"1.0\"?>\n"
                "<!DOCTYPE fontconfig SYSTEM \"urn:fontconfig:fonts.dtd\">\n"
                "<fontconfig>\n"
                f"{fontconfig_dirs}"
                "    <dir prefix=\"xdg\">fonts</dir>\n"
                "    <dir>~/.fonts</dir>\n"
                "</fontconfig>\n",
                encoding="utf-8",
            )
            conversion_env["FONTCONFIG_FILE"] = str(fontconfig_path)
        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(temp_dir), str(docx_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=conversion_env,
        )
        converted = temp_dir / f"{docx_path.stem}.pdf"
        if result.returncode != 0 or not converted.exists():
            return False, clip(result.stderr or result.stdout or "PDF 转换失败", 500)
        shutil.copy2(converted, pdf_path)
        return True, ""
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)






class InboxBatchMergeRequest(BaseModel):
    source_ids: list[int] = Field(default_factory=list, min_length=1, max_length=20)
    confirmed: bool = False




@app.get("/api/inbox/merge-suggestions")
def get_inbox_merge_suggestions(limit: int = 50) -> dict[str, Any]:
    items = list_inbox("inbox")
    suggestions = []
    for index, first in enumerate(items):
        left = knowledge_tokens(first.get("content", ""))
        if len(left) < 2:
            continue
        for second in items[index + 1:]:
            right = knowledge_tokens(second.get("content", ""))
            similarity = len(left.intersection(right)) / max(1, len(left.union(right)))
            if similarity >= 0.45:
                suggestions.append({"source": first, "duplicate": second, "similarity": round(similarity, 3), "reason": "文本关键词高度重叠，请人工确认是否合并。"})
    return {"suggestions": sorted(suggestions, key=lambda item: -item["similarity"])[:limit], "policy": "只建议不自动删除；合并后原条目进入 archived 并保留来源。"}


@app.post("/api/inbox/{item_id}/merge-batch")
def merge_inbox_items(item_id: int, request: InboxBatchMergeRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(409, "合并收件箱条目前需要明确确认")
    source = get_inbox_record(item_id)
    if not source:
        raise HTTPException(404, "主收件箱条目不存在")
    merged = [source.get("content", "")]
    ids = [item_id]
    for source_id in request.source_ids:
        if source_id == item_id:
            continue
        item = get_inbox_record(source_id)
        if item and item.get("status") == "inbox":
            merged.append(item.get("content", ""))
            ids.append(source_id)
    if len(ids) < 2:
        raise HTTPException(400, "至少需要两条待处理条目")
    merged_item = create_inbox_record(content="\n\n--- 合并条目 ---\n\n".join(merged), kind=source.get("kind", "note"), tags=source.get("tags", ""), priority=source.get("priority", "normal"))
    connection = db_connection()
    try:
        connection.executemany("UPDATE inbox SET status = 'archived', updated_at = ? WHERE id = ?", [(now_iso(), value) for value in ids])
        connection.commit()
    finally:
        connection.close()
    relation = create_relation_record(from_type="inbox", from_id=str(item_id), to_type="inbox", to_id=str(merged_item["id"]), relation_type="merged_into", metadata={"source_ids": ids})
    return {"ok": True, "merged": merged_item, "archived_ids": ids, "relation": relation}


@app.get("/api/inbox/classifier-stats")
def inbox_classifier_stats() -> dict[str, Any]:
    minimum_samples = 10
    labels = {
        "note": "笔记",
        "task": "待办",
        "link": "链接",
        "idea": "想法",
        "alert": "告警",
        "document": "文档",
        "research": "研究",
    }
    connection = db_connection()
    try:
        rows = connection.execute("SELECT classification, COUNT(*) AS count FROM inbox WHERE classification != '' GROUP BY classification ORDER BY count DESC").fetchall()
        totals = connection.execute(
            """SELECT COUNT(*) AS total_count,
                SUM(CASE WHEN classification != '' THEN 1 ELSE 0 END) AS classified_count
               FROM inbox"""
        ).fetchone()
        feedback = connection.execute(
            """SELECT COUNT(*) AS sample_count,
                SUM(CASE WHEN predicted = accepted THEN 1 ELSE 0 END) AS confirmed_count,
                SUM(CASE WHEN predicted != accepted THEN 1 ELSE 0 END) AS correction_count
               FROM inbox_classification_feedback"""
        ).fetchone()
        feedback_pairs = connection.execute(
            "SELECT predicted, accepted FROM inbox_classification_feedback WHERE predicted != '' AND accepted != ''"
        ).fetchall()
        sample_count = int(feedback["sample_count"] or 0)
        confirmed_count = int(feedback["confirmed_count"] or 0)
        correction_count = int(feedback["correction_count"] or 0)
        classes = [
            {"classification": str(row["classification"]), "label": labels.get(str(row["classification"]), str(row["classification"])), "count": int(row["count"] or 0)}
            for row in rows
        ]
        feedback_labels = sorted({str(row["predicted"] or "") for row in feedback_pairs} | {str(row["accepted"] or "") for row in feedback_pairs})
        per_class = []
        f1_values = []
        for label in feedback_labels:
            true_positive = sum(1 for row in feedback_pairs if str(row["predicted"]) == label and str(row["accepted"]) == label)
            predicted_count = sum(1 for row in feedback_pairs if str(row["predicted"]) == label)
            actual_count = sum(1 for row in feedback_pairs if str(row["accepted"]) == label)
            precision = true_positive / predicted_count if predicted_count else None
            recall = true_positive / actual_count if actual_count else None
            f1 = (2 * precision * recall / (precision + recall)) if precision is not None and recall is not None and precision + recall else None
            if f1 is not None:
                f1_values.append(f1)
            per_class.append({
                "classification": label,
                "label": labels.get(label, label),
                "precision": round(precision, 3) if precision is not None else None,
                "recall": round(recall, 3) if recall is not None else None,
                "f1": round(f1, 3) if f1 is not None else None,
                "predicted_count": predicted_count,
                "actual_count": actual_count,
            })
        return {
            "classes": classes,
            "total_count": int(totals["total_count"] or 0),
            "classified_count": int(totals["classified_count"] or 0),
            "sample_count": sample_count,
            "confirmed_count": confirmed_count,
            "correction_count": correction_count,
            "confirmation_rate": round(confirmed_count / sample_count, 3) if sample_count else None,
            "accuracy": round(confirmed_count / sample_count, 3) if sample_count else None,
            "macro_f1": round(sum(f1_values) / len(f1_values), 3) if f1_values else None,
            "per_class": per_class,
            "minimum_samples": minimum_samples,
            "sample_status": "ready" if sample_count >= minimum_samples else "insufficient",
            "model": "deterministic triage v1",
            "learning": "用户确认/修正可作为后续分类样本；不会自动改写历史条目，也不会因样本不足宣称模型已学会。",
        }
    finally:
        connection.close()















@app.post("/api/runs/{run_id}/cancel")




@app.post("/api/push/test")
async def send_push_test() -> dict[str, Any]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT * FROM push_subscriptions WHERE enabled = 1 ORDER BY updated_at DESC").fetchall()
    finally:
        connection.close()
    if not rows:
        return {"ok": False, "sent": 0, "message": "还没有启用的 Push 订阅。"}
    sent = 0
    expired = 0
    errors = []
    for row in rows:
        # deliver_push 内含 pywebpush 同步网络请求，放到线程池避免阻塞事件循环
        result = await asyncio.to_thread(deliver_push, row, title="Workbench 测试通知", body="远程 Web Push 已连通。", href="/", event_key=f"push-test:{now_iso()}")
        if result.get("status") == "sent":
            sent += 1
        elif result.get("status") == "expired":
            expired += 1
        elif result.get("error"):
            errors.append(result["error"])
    message = f"已送达 {sent}/{len(rows)} 个订阅"
    if expired:
        message += f"，已停用 {expired} 个失效订阅"
    return {"ok": sent > 0, "sent": sent, "stored": len(rows), "expired": expired, "errors": list(dict.fromkeys(errors))[:10], "message": message}


async def automation_scheduler_loop() -> None:
    while True:
        await asyncio.sleep(30)
        lease = worker_lease("sync-worker", status="running", metadata={"loop": "automation_scheduler"})
        if lease.get("status") == "held_by_other_instance":
            continue
        for rule in automation_rules():
            if not rule.get("enabled"):
                continue
            schedule = str(rule.get("schedule") or "")
            match = re.fullmatch(r"every:(\d+)", schedule)
            if not match:
                continue
            interval = max(30, int(match.group(1)))
            last = rule.get("last_run_at")
            last_dt = _audit_datetime(last) if last else None
            now = datetime.now(timezone.utc)
            if last_dt and (now - last_dt).total_seconds() < interval:
                continue
            try:
                await execute_automation_rule(int(rule["id"]), trigger="scheduler")
            except Exception:
                continue


AGENT_QUEUE_LEASE_SECONDS = _int_env("WORKBENCH_AGENT_QUEUE_LEASE_SECONDS", 900, minimum=60, maximum=7200)


def agent_queue_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = decode_json_value(item.pop("payload_json", "{}"), {}) or {}
    item["result"] = decode_json_value(item.pop("result_json", "{}"), {}) or {}
    item["cancellable"] = item.get("status") in {"queued", "running"}
    return item


def agent_queue_task_cancelled(task_id: int) -> bool:
    """运行中的 ReAct 循环在每轮工具之间查这个：任务是否已被用户取消。"""
    connection = db_connection()
    try:
        row = connection.execute("SELECT status FROM agent_queue WHERE id = ?", (task_id,)).fetchone()
        return bool(row and row["status"] == "cancelled")
    finally:
        connection.close()


def enqueue_agent_task(
    *,
    kind: str,
    payload: dict[str, Any],
    project_id: str = "",
    session_id: str = "",
    queue: str = "default",
    priority: int = 100,
    max_attempts: int = 3,
    dedupe_key: str = "",
) -> dict[str, Any]:
    """把一次 Agent 调用放进队列，立刻返回。

    去重键命中还没做完的同一件事时，直接返回既有那条，而不是排两遍——
    「提交」这个动作在网络抖动或用户连点时天然会重复。
    """
    timestamp = now_iso()
    connection = db_connection()
    try:
        if dedupe_key:
            existing = connection.execute(
                "SELECT * FROM agent_queue WHERE dedupe_key = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1",
                (dedupe_key,),
            ).fetchone()
            if existing:
                return {**agent_queue_row(existing), "deduped": True}
        cursor = connection.execute(
            """INSERT INTO agent_queue
            (queue, project_id, session_id, kind, payload_json, status, priority, max_attempts,
             available_at, dedupe_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
            (queue, project_id, session_id, kind, json.dumps(payload or {}, ensure_ascii=False),
             int(priority), int(max_attempts), timestamp, dedupe_key, timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM agent_queue WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return agent_queue_row(row)
    finally:
        connection.close()


def claim_agent_task(worker_id: str, queue: str = "default") -> dict[str, Any] | None:
    """领一个任务。

    用「UPDATE … WHERE id = ? AND status = 'queued'」+ 检查 rowcount 来抢，
    而不是先 SELECT 再 UPDATE：多个 worker 同时取的时候，先查后改会让两个
    worker 领到同一条。租约到期的 running 任务也会被重新放回队列——worker
    进程被杀时不会有人来标记，只能靠租约过期兜底。
    """
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    lease_until = (now + timedelta(seconds=AGENT_QUEUE_LEASE_SECONDS)).isoformat()
    connection = db_connection()
    try:
        # 先把租约过期的 running 放回队列。
        connection.execute(
            """UPDATE agent_queue SET status = 'queued', claimed_by = '', lease_until = '', updated_at = ?
            WHERE status = 'running' AND lease_until <> '' AND lease_until < ?""",
            (timestamp, timestamp),
        )
        connection.commit()
        for _ in range(5):
            row = connection.execute(
                """SELECT id FROM agent_queue
                WHERE status = 'queued' AND queue = ? AND (available_at = '' OR available_at <= ?)
                ORDER BY priority ASC, id ASC LIMIT 1""",
                (queue, timestamp),
            ).fetchone()
            if not row:
                return None
            cursor = connection.execute(
                """UPDATE agent_queue
                SET status = 'running', claimed_by = ?, claimed_at = ?, lease_until = ?,
                    attempt = attempt + 1, updated_at = ?
                WHERE id = ? AND status = 'queued'""",
                (worker_id, timestamp, lease_until, timestamp, row["id"]),
            )
            connection.commit()
            if cursor.rowcount:
                claimed = connection.execute("SELECT * FROM agent_queue WHERE id = ?", (row["id"],)).fetchone()
                return agent_queue_row(claimed)
        return None
    finally:
        connection.close()


def finish_agent_task(task_id: int, *, status: str, result: dict[str, Any] | None = None, error: str = "") -> dict[str, Any] | None:
    """结束一个任务。失败且还有重试次数时放回队列，并按次数退避。"""
    timestamp = now_iso()
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM agent_queue WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        if status == "failed" and int(row["attempt"]) < int(row["max_attempts"]):
            # 退避：立刻重试多半会撞上同一个原因（限流、上游抖动）。
            delay = min(300, 15 * (2 ** max(0, int(row["attempt"]) - 1)))
            available_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            connection.execute(
                """UPDATE agent_queue SET status = 'queued', error = ?, available_at = ?,
                claimed_by = '', lease_until = '', updated_at = ? WHERE id = ?""",
                (clip(error, 800), available_at, timestamp, task_id),
            )
        else:
            connection.execute(
                """UPDATE agent_queue SET status = ?, error = ?, result_json = ?, lease_until = '', updated_at = ?
                WHERE id = ?""",
                (status, clip(error, 800), json.dumps(result or {}, ensure_ascii=False), timestamp, task_id),
            )
        connection.commit()
        return agent_queue_row(connection.execute("SELECT * FROM agent_queue WHERE id = ?", (task_id,)).fetchone())
    finally:
        connection.close()


def cancel_agent_task(task_id: int) -> dict[str, Any] | None:
    """取消排队或运行中的任务。

    排队中的直接改为 cancelled；运行中的任务标成 cancelled 后，ReAct 循环
    会在每轮工具之间读到这个状态并停下来（不会杀掉正在进行的单次 LLM/工具
    调用，但不会进入下一轮）。
    """
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            "UPDATE agent_queue SET status = 'cancelled', updated_at = ? WHERE id = ? AND status IN ('queued', 'running')",
            (timestamp, task_id),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM agent_queue WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        item = agent_queue_row(row)
        item["cancelled"] = bool(cursor.rowcount)
        return item
    finally:
        connection.close()


def list_agent_tasks(status: str = "", queue: str = "", limit: int = 50) -> list[dict[str, Any]]:
    clauses, params = [], []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if queue:
        clauses.append("queue = ?")
        params.append(queue)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = db_connection()
    try:
        rows = connection.execute(
            f"SELECT * FROM agent_queue {where} ORDER BY id DESC LIMIT ?",
            (*params, max(1, min(200, limit))),
        ).fetchall()
        return [agent_queue_row(row) for row in rows]
    finally:
        connection.close()


def insert_agent_queue_message(task_id: int, content: str) -> dict[str, Any]:
    """往一个排队中或正在跑的任务里插一条消息。

    这是队列真正比「同步跑一次」多出来的能力：任务跑到一半你想起还要看一个
    东西，可以直接追加，下一轮循环就会读到——而不是等它跑完再重新发一遍，
    也不是把它取消掉重来。
    """
    text = str(content or "").strip()
    if not text:
        raise HTTPException(400, "插入的消息不能为空")
    timestamp = now_iso()
    connection = db_connection()
    try:
        row = connection.execute("SELECT status, run_id FROM agent_queue WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "队列任务不存在")
        if row["status"] not in {"queued", "running"}:
            raise HTTPException(409, f"任务已经是「{row['status']}」，插入的消息不会被读到")
        cursor = connection.execute(
            "INSERT INTO agent_queue_messages (queue_id, run_id, content, created_at) VALUES (?, ?, ?, ?)",
            (task_id, str(row["run_id"] or ""), clip(text, 4000), timestamp),
        )
        connection.commit()
        return {"id": int(cursor.lastrowid or 0), "queue_id": task_id, "content": clip(text, 4000), "created_at": timestamp}
    finally:
        connection.close()


def consume_agent_queue_messages(task_id: int) -> list[str]:
    """取走并标记这个任务当前累积的插入消息。

    先读后标记，同一条不会被消费两次；标记用的是同一个连接同一个事务，
    中途崩溃的话消息还在，下一轮会重新读到——宁可重复读，不可丢。
    """
    timestamp = now_iso()
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT id, content FROM agent_queue_messages WHERE queue_id = ? AND consumed_at = '' ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        if not rows:
            return []
        connection.executemany(
            "UPDATE agent_queue_messages SET consumed_at = ? WHERE id = ?",
            [(timestamp, row["id"]) for row in rows],
        )
        connection.commit()
        return [str(row["content"]) for row in rows]
    finally:
        connection.close()


def recover_stuck_agent_runs() -> int:
    """把「进程重启后再也不会有人推进」的 run 标成失败。

    回收逻辑此前只覆盖 kind='crawl'：dispatch、dispatch_child、chat、action、
    handoff 这些 run 一旦在进程被杀/重启时正处于 running，就永远停在那里。
    更糟的是它们会反过来影响路由——agent_run_summary 把 queued+running 记为
    active，capability_graph_route 按 active 扣分，几个僵尸 run 就能让一个项目
    基本再也不被路由到，而且没有任何地方会提示。

    判断依据是「更新时间早于本进程启动时间」：run 是在内存里推进的，进程一换，
    上一个进程留下的 running 就不可能再有人接手。
    """
    cutoff = _PROCESS_STARTED_AT
    recovered: list[str] = []
    connection = db_connection()
    try:
        rows = connection.execute(
            """SELECT id, project_id, kind FROM agent_runs
            WHERE status IN ('running', 'queued')
              AND kind NOT IN ('crawl')
              AND COALESCE(NULLIF(updated_at, ''), created_at) < ?""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE agent_runs SET status = 'failed', error = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                ("工作台重启时这次运行还没结束，已标记为失败；需要的话可以重试。", now_iso(), now_iso(), row["id"]),
            )
            recovered.append(str(row["id"]))
        connection.commit()
    finally:
        connection.close()
    for run_id in recovered:
        add_agent_run_event(run_id, "failed", "工作台重启，这次运行被中断。", level="warning")
    return len(recovered)


class AgentQueueEnqueueRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=8000)
    session_id: str = Field(default="", max_length=80)
    priority: int = Field(default=100, ge=1, le=999)
    dedupe_key: str = Field(default="", max_length=120)


class AgentQueueMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


@app.get("/api/agent/queue")
def get_agent_queue(status: str = "", queue: str = "", limit: int = 50) -> dict[str, Any]:
    tasks = list_agent_tasks(status=status, queue=queue, limit=limit)
    counts: dict[str, int] = {}
    for item in list_agent_tasks(status="all", limit=200):
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"tasks": tasks, "counts": counts, "lease_seconds": AGENT_QUEUE_LEASE_SECONDS}


@app.post("/api/agent/queue")
def post_agent_queue(request: AgentQueueEnqueueRequest) -> dict[str, Any]:
    """提交一次 Agent 调用，立刻返回。

    同步跑完再返回是原来的做法：一次总调度最坏几十次 LLM 调用，浏览器只能
    干等，请求一断任务就没了下文。入队之后提交是一瞬间的事，跑得怎么样去
    队列里看。
    """
    require_project_agent(request.project_id)
    task = enqueue_agent_task(
        kind="chat",
        payload={"message": request.message, "session_id": request.session_id},
        project_id=request.project_id,
        session_id=request.session_id,
        priority=request.priority,
        dedupe_key=request.dedupe_key,
    )
    return {"task": task}


@app.post("/api/agent/queue/{task_id}/messages")
def post_agent_queue_message(task_id: int, request: AgentQueueMessageRequest) -> dict[str, Any]:
    """往排队中或正在跑的任务里插一条消息。"""
    return {"message": insert_agent_queue_message(task_id, request.content)}


@app.post("/api/agent/queue/{task_id}/cancel")
def post_agent_queue_cancel(task_id: int) -> dict[str, Any]:
    task = cancel_agent_task(task_id)
    if not task:
        raise HTTPException(404, "队列任务不存在")
    if not task.get("cancelled"):
        raise HTTPException(409, f"任务已经是「{task['status']}」，取消不了。正在跑的任务停不下来——它可能正在调 LLM 或写库。")
    return {"task": task}


async def run_queued_agent_task(task: dict[str, Any]) -> dict[str, Any]:
    """执行一条队列任务。"""
    payload = task.get("payload") or {}
    project_id = str(task.get("project_id") or "")
    message = str(payload.get("message") or "")
    require_project_agent(project_id)
    session = get_agent_session(str(payload.get("session_id") or ""), project_id) if payload.get("session_id") else None
    if not session:
        session = await asyncio.to_thread(create_agent_session, project_id, message)
    await asyncio.to_thread(add_agent_message, session["id"], "user", message, {"source": "agent_queue", "queue_id": task["id"]})
    run = await asyncio.to_thread(
        create_agent_run_record,
        project_id=project_id,
        session_id=session["id"],
        kind="chat",
        title=clip(message, 120),
        request={"queue_id": task["id"], "message": message},
        max_attempts=1,
    )
    # 把 run_id 写回队列行：插进来的消息要能顺着它找到这次运行的事件流。
    connection = db_connection()
    try:
        connection.execute("UPDATE agent_queue SET run_id = ?, updated_at = ? WHERE id = ?",
                           (run["id"], now_iso(), task["id"]))
        connection.commit()
    finally:
        connection.close()
    return await run_project_agent(
        project_id=project_id, session=session, run=run, message=message,
        context={"source": "agent_queue"}, queue_task_id=int(task["id"]),
    )


async def agent_queue_worker_loop() -> None:
    """队列消费循环。

    只在主进程里跑一份：worker 之间靠 claim_agent_task 的原子 UPDATE 抢任务，
    所以多开几份也不会重复执行，但没必要。
    """
    worker_id = f"queue-{os.getpid()}"
    while True:
        try:
            task = await asyncio.to_thread(claim_agent_task, worker_id)
            if not task:
                await asyncio.sleep(3)
                continue
            log.info("队列任务 %s 开始执行（%s）", task["id"], task.get("project_id"))
            try:
                result = await run_queued_agent_task(task)
                await asyncio.to_thread(finish_agent_task, task["id"], status="succeeded",
                                        result={"run_id": (result.get("run") or {}).get("id", ""),
                                                "session_id": (result.get("session") or {}).get("id", ""),
                                                "answer": clip(str(result.get("message", {}).get("content", "")), 2000)})
            except Exception as exc:  # noqa: BLE001 - 单条任务失败不能终结循环
                log.warning("队列任务 %s 执行失败：%s", task["id"], exc, exc_info=True)
                await asyncio.to_thread(finish_agent_task, task["id"], status="failed", error=clip(str(exc), 800))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("队列循环异常", exc_info=True)
            await asyncio.sleep(5)


async def crawl_janitor_loop() -> None:
    """定期回收「没有 Worker 会来取」的排队任务。

    flag_orphaned_crawl_runs() 是为「Crawl Worker 根本没启动」写的，但它此前
    只在 recover_stale_crawl_runs() 里被调用，而后者只在 crawl_worker.py 内部
    调用——Worker 不在的时候，连回收也不会发生。也就是说这个函数在它唯一
    该生效的场景里从来不会跑，逻辑上自相矛盾。

    主进程是唯一「一定在跑」的进程，所以放在这里，并且不受
    WORKBENCH_EXTERNAL_*_WORKER 开关影响：那些开关控制的是别的 Worker 要不要
    在进程内跑，而这件事恰恰是要在别的 Worker 都不在时兜底。
    """
    # 回收本身要等排队超过 stale 窗口 4 倍才动手，所以检查间隔按 stale 窗口来
    # 就够了，没必要跑得更勤。
    interval = max(60, WORKBENCH_CRAWL_STALE_SECONDS)
    while True:
        await asyncio.sleep(interval)
        try:
            flagged = await asyncio.to_thread(flag_orphaned_crawl_runs)
            if flagged:
                log.warning("回收了 %s 个无人认领的爬取任务（Crawl Worker 未运行）", flagged)
            stuck = await asyncio.to_thread(recover_stuck_agent_runs)
            if stuck:
                log.warning("回收了 %s 个上次进程留下的僵尸 Agent 运行", stuck)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 兜底循环不能被单次失败终结
            log.warning("回收无人认领的爬取任务失败", exc_info=True)


@app.on_event("startup")
async def start_automation_scheduler() -> None:
    external_sync_worker = os.getenv("WORKBENCH_EXTERNAL_SYNC_WORKER", "").strip().lower() in {"1", "true", "yes"}
    external_agent_worker = os.getenv("WORKBENCH_EXTERNAL_AGENT_WORKER", "").strip().lower() in {"1", "true", "yes"}
    if not external_sync_worker:
        await asyncio.to_thread(worker_lease, "sync-worker", status="ready", metadata={"startup": True})
    if not external_agent_worker:
        await asyncio.to_thread(worker_lease, "agent-worker", status="ready", metadata={"startup": True})
    if not external_sync_worker and getattr(app.state, "automation_scheduler", None) is None:
        app.state.automation_scheduler = asyncio.create_task(automation_scheduler_loop())
    if not external_sync_worker and getattr(app.state, "sub2api_auto_sync_task", None) is None:
        app.state.sub2api_auto_sync_task = asyncio.create_task(sub2api_auto_sync_loop())
    # 无条件启动：它兜的就是「别的 Worker 都不在」这种情况。
    if getattr(app.state, "crawl_janitor", None) is None:
        app.state.crawl_janitor = asyncio.create_task(crawl_janitor_loop())
    if getattr(app.state, "agent_queue_worker", None) is None:
        app.state.agent_queue_worker = asyncio.create_task(agent_queue_worker_loop())
    # 启动时立刻回收一次：上一个进程留下的僵尸不该等到第一个巡检周期。
    try:
        stuck = await asyncio.to_thread(recover_stuck_agent_runs)
        if stuck:
            log.warning("启动回收：%s 个上次进程留下的 Agent 运行已标记为失败", stuck)
    except Exception:  # noqa: BLE001
        log.warning("启动回收僵尸 Agent 运行失败", exc_info=True)


@app.on_event("shutdown")
async def stop_automation_scheduler() -> None:
    task = getattr(app.state, "automation_scheduler", None)
    if task:
        task.cancel()
        app.state.automation_scheduler = None
    sync_task = getattr(app.state, "sub2api_auto_sync_task", None)
    if sync_task:
        sync_task.cancel()
        app.state.sub2api_auto_sync_task = None
    janitor = getattr(app.state, "crawl_janitor", None)
    if janitor:
        janitor.cancel()
        app.state.crawl_janitor = None
    queue_worker = getattr(app.state, "agent_queue_worker", None)
    if queue_worker:
        queue_worker.cancel()
        app.state.agent_queue_worker = None
    await close_llm_http_clients()
    external_sync_worker = os.getenv("WORKBENCH_EXTERNAL_SYNC_WORKER", "").strip().lower() in {"1", "true", "yes"}
    external_agent_worker = os.getenv("WORKBENCH_EXTERNAL_AGENT_WORKER", "").strip().lower() in {"1", "true", "yes"}
    owned_workers = []
    if not external_sync_worker:
        owned_workers.append("sync-worker")
    if not external_agent_worker:
        owned_workers.append("agent-worker")
    for worker_id in owned_workers:
        try:
            await asyncio.to_thread(release_worker_lease, worker_id)
        except Exception:
            log.debug("忽略异常（stop_automation_scheduler）", exc_info=True)


# ---------------------------------------------------------------------------
# Evidence and opportunity round: small, reusable primitives shared by the
# Crawl4AI, AI-hotspot, CID and idea Agents.  These endpoints deliberately
# store references and audit relations, not copied upstream bodies.
# ---------------------------------------------------------------------------







def update_artifact_metadata(artifact_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
    artifact = get_artifact_record(artifact_id)
    if not artifact:
        return None
    metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
    metadata.update(values)
    connection = db_connection()
    try:
        connection.execute("UPDATE artifacts SET metadata_json = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False), artifact_id))
        connection.commit()
    finally:
        connection.close()
    return get_artifact_record(artifact_id)












@app.get("/api/sub2api/change-explanation")
async def explain_sub2api_changes(limit: int = 30) -> dict[str, Any]:
    trend = await get_sub2api_trend(limit)
    delta = trend.get("delta") or {}
    changes = []
    for key, label in (("weekly_remaining_pct", "周额度剩余比例"), ("monthly_remaining_pct", "月额度剩余比例")):
        value = delta.get(key)
        if isinstance(value, (int, float)):
            direction = "增加" if value > 0 else "减少" if value < 0 else "没有变化"
            points = trend.get("points") or []
            reset_hint = ""
            if len(points) >= 2:
                previous = points[-2].get(key)
                raw_key = "weekly_raw" if key.startswith("weekly") else "monthly_raw"
                raw_changed = points[-2].get(raw_key) != points[-1].get(raw_key)
                if value > 0.15 and raw_changed:
                    reset_hint = "；可能包含额度周期重置或面板口径变化"
                elif value < -0.15 and not raw_changed:
                    reset_hint = "；快照显示剩余比例下降，但无法仅凭快照区分请求消耗与同步延迟"
            changes.append({"metric": key, "label": label, "delta": value, "direction": direction, "reason": f"基于最近可用脱敏快照的首尾差值{reset_hint}"})
    points = trend.get("points") or []
    expiry_change = None
    if len(points) >= 2 and isinstance(points[-2].get("remaining_days"), (int, float)) and isinstance(points[-1].get("remaining_days"), (int, float)):
        expiry_change = round(float(points[-1]["remaining_days"]) - float(points[-2]["remaining_days"]), 2)
    if expiry_change is not None and expiry_change > 1:
        changes.append({"metric": "remaining_days", "label": "订阅剩余时间", "delta": expiry_change, "direction": "增加", "reason": "快照中的到期倒计时出现回升，可能是续期或面板更新时间变化；请回原页面核对"})
    return {"changes": changes, "forecast": trend.get("forecast", {}), "sample_count": trend.get("sample_count", 0), "data_as_of": (trend.get("points") or [{}])[-1].get("checked_at", "") if trend.get("points") else "", "policy": "只解释快照差异，不推断具体消费原因；需要原因时请回到 Sub2API 原页面核对。"}


# ---------------------------------------------------------------------------
# Final product closure endpoints.  These use the existing Artifact / WorkItem
# / Relation primitives so every new action remains replayable without adding
# another source of truth.
# ---------------------------------------------------------------------------


def list_workbench_decisions(limit: int = 30) -> list[dict[str, Any]]:
    decisions = [item for item in list_artifacts("workbench") if item.get("kind") == "workbench_decision"]
    decisions.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    for item in decisions[: max(1, min(limit, 100))]:
        item["relations"] = list_relations(str(item.get("id")))[:12]
    return decisions[: max(1, min(limit, 100))]


@app.get("/api/workbench/decisions")
def get_workbench_decisions(limit: int = 30) -> dict[str, Any]:
    return {"decisions": list_workbench_decisions(limit), "policy": "决策只记录用户明确确认的结论；Agent 建议会保留为草稿，不会自动替用户改结论。"}


@app.post("/api/workbench/decisions")
def create_workbench_decision(request: WorkbenchDecisionRequest) -> dict[str, Any]:
    project_ids = list(dict.fromkeys(str(item).strip() for item in request.project_ids if str(item).strip()))
    invalid = [item for item in project_ids if item not in AGENT_REGISTRY]
    if invalid:
        raise HTTPException(400, f"不存在的项目 Agent：{invalid[0]}")
    title = request.title.strip()
    decision = request.decision.strip()
    next_steps = [clip(str(item).strip(), 500) for item in request.next_steps if str(item).strip()][:20]
    timestamp = now_iso()
    next_step_lines = [f"- {item}" for item in next_steps] or ["- 暂未记录下一步。"]
    body = [f"# {title}", "", f"## 决策\n\n{decision}", "", "## 判断依据", "", request.rationale.strip() or "未补充判断依据。", "", "## 下一步", "", *next_step_lines, "", f"> 状态：{'已确认' if request.confirmed else '草稿'} · 记录时间：{timestamp}"]
    path = OUTPUTS_DIR / f"decision-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_filename(title, 'workbench')}.md"
    suffix = 2
    while path.exists():
        path = OUTPUTS_DIR / f"decision-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_filename(title, 'workbench')}-{suffix}.md"
        suffix += 1
    path.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
    artifact = register_artifact_safely(project_id="workbench", name=path.name, path=str(path), kind="workbench_decision", metadata={"title": title, "decision": decision, "rationale": request.rationale.strip(), "next_steps": next_steps, "project_ids": project_ids, "confirmed": request.confirmed, "created_at": timestamp})
    if artifact:
        for project_id in project_ids:
            create_relation_record(from_type="project", from_id=project_id, to_type="artifact", to_id=str(artifact["id"]), relation_type="project_to_decision", metadata={"confirmed": request.confirmed})
    item = None
    if next_steps and request.confirmed:
        item = create_work_item_record(title=f"执行决策：{title}", description="\n".join(next_steps), kind="decision_followup", priority="normal", source_project="workbench", target_project=project_ids[0] if len(project_ids) == 1 else "workbench", metadata={"decision_artifact_id": artifact.get("id") if artifact else None, "project_ids": project_ids, "confirmed": request.confirmed})
        if artifact:
            create_relation_record(from_type="artifact", from_id=str(artifact["id"]), to_type="work_item", to_id=str(item["id"]), relation_type="decision_to_followup", metadata={"confirmed": request.confirmed})
    notification = create_notification_record(title=f"已记录工作台决策：{title}", body=clip(decision, 500), project_id="workbench", kind="decision", level="info", href="/#activity", event_key=f"decision:{artifact.get('id') if artifact else timestamp}", dedupe_seconds=0)
    message = "决策已记录；下一步已进入工作台待办。" if item else "决策草稿已记录；勾选确认后才会创建下一步待办。" if next_steps else "决策已记录。"
    return {"ok": True, "artifact": artifact, "work_item": item, "notification": notification, "message": message}


def workbench_collaboration_snapshot(limit: int = 8, include_failed: bool = True, include_blocked: bool = True) -> dict[str, Any]:
    statuses = {"open", "running"}
    if include_failed:
        statuses.add("failed")
    if include_blocked:
        statuses.add("blocked")
    items = [item for item in list_work_items("all") if item.get("status") in statuses and not (item.get("metadata") or {}).get("ignored_at")]
    items.sort(key=lambda item: (0 if item.get("priority") == "urgent" else 1 if item.get("priority") == "high" else 2, item.get("updated_at", "")), reverse=False)
    agents = project_audit().get("agents", [])
    recommendations = []
    for item in items[: max(1, min(limit, 20))]:
        target = str(item.get("target_project") or item.get("source_project") or "workbench").split(",")[0].strip()
        quality = item.get("next_step_quality") or work_item_next_step_quality(item)
        recommendation = (
            "先处理失败并查看运行回放" if item.get("status") == "failed"
            else "补充人工确认" if item.get("status") == "blocked"
            else "继续执行" if item.get("status") == "running"
            else "先补一条最小下一步" if quality.get("status") == "missing"
            else "先确认目标/负责人/截止时间" if quality.get("status") == "review"
            else "领取并执行"
        )
        recommendations.append({"work_item": item, "target_project": target, "recommendation": recommendation, "next_step_quality": quality, "href": project_href(target)})
    quality_counts = {status: sum(1 for entry in recommendations if entry.get("next_step_quality", {}).get("status") == status) for status in ("ready", "review", "missing")}
    return {"items": recommendations, "agent_count": len(agents), "agents": [{"project_id": item.get("project_id"), "name": item.get("name"), "audit_status": item.get("audit_status"), "latest_run": item.get("latest_run")} for item in agents], "next_step_quality": {"counts": quality_counts, "policy": "只统计显式记录的下一步；长描述不会自动当成可执行动作。"}, "generated_at": now_iso(), "policy": "主动协作只生成基于当前 WorkItem/Run 的建议和计划，不代替用户批准高风险动作。"}


@app.get("/api/workbench/collaboration")
def get_workbench_collaboration(limit: int = 8) -> dict[str, Any]:
    return workbench_collaboration_snapshot(limit)


@app.post("/api/workbench/collaboration/prepare")
def prepare_workbench_collaboration(request: WorkbenchCollaborationRequest) -> dict[str, Any]:
    snapshot = workbench_collaboration_snapshot(request.limit, request.include_failed, request.include_blocked)
    selected = snapshot.get("items", [])[: request.limit]
    if not selected:
        return {"ok": True, "snapshot": snapshot, "plan": None, "message": "当前没有需要主动协作的工作项。"}
    steps = []
    for index, entry in enumerate(selected, start=1):
        item = entry["work_item"]
        target = entry["target_project"] if entry["target_project"] in AGENT_REGISTRY and entry["target_project"] != "workbench" else "workbench"
        steps.append({"key": f"item-{item['id']}", "title": f"处理工作项 #{item['id']}：{item.get('title', '未命名')}", "project_id": target, "kind": "agent", "dependencies": [f"item-{selected[index - 2]['work_item']['id']}"] if index > 1 else [], "input": {"message": f"请处理工作项 #{item['id']}：{item.get('title', '')}\n{item.get('description', '')}", "context": {"work_item_id": item["id"], "source": "主动协作"}}, "max_attempts": 2})
    plan = create_execution_plan("工作台主动协作计划", "workbench", steps, {"work_item_ids": [entry["work_item"].get("id") for entry in selected], "confirmed": request.confirmed})
    if request.confirmed:
        update_plan_status(plan["id"], "queued", result={"prepared_at": now_iso(), "confirmed": True})
        create_notification_record(title="主动协作计划已进入队列", body=f"共 {len(steps)} 个步骤，等待 Agent Worker 执行。", project_id="workbench", kind="plan", level="info", href="/automation", event_key=f"collaboration-plan:{plan['id']}", dedupe_seconds=0)
    return {"ok": True, "snapshot": snapshot, "plan": get_execution_plan(plan["id"]), "message": "计划已生成；确认后才会进入执行队列。" if not request.confirmed else "计划已确认并进入 Agent Worker 队列。"}






SERVER_SAFE_ACTIONS = {"refresh": {"label": "重新执行只读检查", "risk": "low"}, "inspect_logs": {"label": "记录服务日志检查请求", "risk": "low"}, "restart": {"label": "申请重启 Workbench 服务", "risk": "high"}}


def _server_action_execution_payload(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    item["result"] = platform_decode_json(item.pop("result_json", "{}"), {})
    item["rollback"] = platform_decode_json(item.pop("rollback_json", "{}"), {})
    item["rollback_available"] = bool(item.get("rollback", {}).get("previous_snapshot")) and not item.get("rolled_back_at")
    return item


def latest_server_action_execution(approval_id: str) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM server_action_executions WHERE approval_id = ? ORDER BY created_at DESC LIMIT 1", (approval_id,)).fetchone()
        return _server_action_execution_payload(row)
    finally:
        connection.close()


def list_server_action_executions(limit: int = 80) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT * FROM server_action_executions ORDER BY created_at DESC LIMIT ?", (max(1, min(200, int(limit or 80))),)).fetchall()
        return [item for row in rows if (item := _server_action_execution_payload(row))]
    finally:
        connection.close()


def create_server_action_execution(approval_id: str, action: str) -> dict[str, Any]:
    execution_id = uuid.uuid4().hex
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            "INSERT INTO server_action_executions(id, approval_id, action, status, created_at) VALUES (?, ?, ?, 'running', ?)",
            (execution_id, approval_id, action, timestamp),
        )
        connection.commit()
    finally:
        connection.close()
    return {"id": execution_id, "approval_id": approval_id, "action": action, "status": "running", "result": {}, "rollback": {}, "rollback_available": False, "created_at": timestamp, "finished_at": "", "rolled_back_at": ""}


def finish_server_action_execution(execution_id: str, *, status: str, result: dict[str, Any] | None = None, error: str = "", rollback: dict[str, Any] | None = None) -> dict[str, Any] | None:
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            "UPDATE server_action_executions SET status = ?, result_json = ?, error = ?, rollback_json = ?, finished_at = ?, updated_at = ? WHERE id = ?",
            (status, json.dumps(result or {}, ensure_ascii=False), error, json.dumps(rollback or {}, ensure_ascii=False), timestamp, timestamp, execution_id),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM server_action_executions WHERE id = ?", (execution_id,)).fetchone()
        return _server_action_execution_payload(row)
    finally:
        connection.close()


async def execute_approved_server_action(approval_id: str) -> dict[str, Any]:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM approval_requests WHERE id = ?", (approval_id,)).fetchone()
    finally:
        connection.close()
    if not row:
        raise HTTPException(404, "审批请求不存在")
    if row["kind"] != "server_action":
        raise HTTPException(409, "这不是服务器动作审批")
    if row["status"] != "approved":
        raise HTTPException(409, "服务器动作必须先审批通过")
    payload = platform_decode_json(row["payload_json"], {})
    action = str(payload.get("action") or "").strip()
    definition = SERVER_SAFE_ACTIONS.get(action)
    if not definition:
        raise HTTPException(400, "审批中的服务器动作已不在安全白名单")
    existing = latest_server_action_execution(approval_id)
    if existing and existing.get("status") in {"succeeded", "manual_required"} and not existing.get("rolled_back_at"):
        return {"ok": existing.get("status") == "succeeded", "execution": existing, "message": "这条审批已经执行过，避免重复执行。"}
    execution = create_server_action_execution(approval_id, action)
    run = create_agent_run_record(
        project_id="server",
        kind="server_action_execution",
        title=f"执行服务器动作：{definition['label']}",
        request={"approval_id": approval_id, "action": action, "risk": definition["risk"]},
        max_attempts=1,
    )
    update_agent_run_record(run["id"], status="running")
    add_agent_run_event(run["id"], "execution_started", f"已按审批执行：{definition['label']}。", metadata={"approval_id": approval_id, "execution_id": execution["id"]})
    try:
        if action == "refresh":
            previous_snapshot = load_server_monitor_snapshot()
            result = await refresh_server_monitor(ServerMonitorRequest(refresh=True))
            finished = finish_server_action_execution(
                execution["id"],
                status="succeeded",
                result={"message": "只读服务器检查已完成", "response": {"status": result.get("server", {}).get("status"), "checked_at": result.get("server", {}).get("checked_at"), "artifact_id": (result.get("artifact") or {}).get("id")}},
                rollback={"previous_snapshot": previous_snapshot, "note": "回退只恢复工作台中的上一份监控快照，不会回退服务器外部状态。"},
            )
            update_agent_run_record(run["id"], status="succeeded", result={"approval_id": approval_id, "execution_id": execution["id"], "action": action})
            add_agent_run_event(run["id"], "execution_succeeded", "只读服务器检查已完成，可按需回退本地快照。", level="success", metadata={"execution_id": execution["id"]})
            return {"ok": True, "execution": finished, "run": get_agent_run(run["id"]), "message": "只读服务器检查已完成。"}
        message = "日志读取需要服务器侧人工查看；本次已记录执行边界。" if action == "inspect_logs" else "重启属于高风险动作，仍需服务器侧人工执行；本次仅记录审批和执行边界。"
        finished = finish_server_action_execution(execution["id"], status="manual_required", result={"message": message, "execution_policy": "不通过 Workbench 自动运行 shell 或重启命令。"})
        update_agent_run_record(run["id"], status="succeeded", result={"approval_id": approval_id, "execution_id": execution["id"], "action": action, "manual_required": True})
        add_agent_run_event(run["id"], "manual_required", message, level="warning", metadata={"execution_id": execution["id"]})
        return {"ok": False, "execution": finished, "run": get_agent_run(run["id"]), "message": message}
    except Exception as exc:
        error = clip(str(exc), 800)
        finish_server_action_execution(execution["id"], status="failed", error=error)
        update_agent_run_record(run["id"], status="failed", error=error)
        add_agent_run_event(run["id"], "execution_failed", error, level="error", metadata={"execution_id": execution["id"]})
        raise HTTPException(502, f"服务器动作执行失败：{error}") from exc


def rollback_server_action_execution(execution_id: str) -> dict[str, Any]:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM server_action_executions WHERE id = ?", (execution_id,)).fetchone()
    finally:
        connection.close()
    execution = _server_action_execution_payload(row)
    if not execution:
        raise HTTPException(404, "服务器动作执行记录不存在")
    if execution.get("action") != "refresh" or execution.get("status") != "succeeded":
        raise HTTPException(409, "只有成功的只读检查可以回退本地监控快照")
    previous_snapshot = execution.get("rollback", {}).get("previous_snapshot")
    if not isinstance(previous_snapshot, dict):
        raise HTTPException(409, "没有可回退的上一份监控快照")
    save_server_monitor_snapshot(previous_snapshot)
    record_server_monitor_snapshot(previous_snapshot)
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute("UPDATE server_action_executions SET status = 'rolled_back', rolled_back_at = ?, updated_at = ? WHERE id = ?", (timestamp, timestamp, execution_id))
        connection.commit()
        row = connection.execute("SELECT * FROM server_action_executions WHERE id = ?", (execution_id,)).fetchone()
    finally:
        connection.close()
    return {"ok": True, "execution": _server_action_execution_payload(row), "message": "已回退工作台中的上一份监控快照；不影响服务器外部状态。"}


@app.post("/api/server/actions/request")
def request_server_action(request: ServerActionRequest) -> dict[str, Any]:
    action = request.action.strip()
    definition = SERVER_SAFE_ACTIONS.get(action)
    if not definition:
        raise HTTPException(400, "不支持的服务器动作")
    if not request.confirmed:
        raise HTTPException(409, "服务器动作需要明确确认")
    approval = create_approval_request("server", "server_action", f"服务器动作审批：{definition['label']}", {"action": action, "reason": request.reason.strip(), "risk": definition["risk"], "execution_policy": "仅允许白名单只读动作；restart 仍需服务器侧人工执行"})
    item = create_work_item_record(title=definition["label"], description=request.reason.strip() or "请在审批后按安全边界处理服务器动作。", kind="server_action", status="blocked", priority="high" if definition["risk"] == "high" else "normal", source_project="server", target_project="server", metadata={"approval_id": approval["id"], "action": action, "risk": definition["risk"]})
    relation = create_relation_record(from_type="approval", from_id=approval["id"], to_type="work_item", to_id=str(item["id"]), relation_type="approval_to_server_action", metadata={"action": action})
    create_notification_record(title="服务器动作已进入审批", body=f"{definition['label']} · 请先在审批中心处理。", project_id="server", kind="approval", level="warning", href="/approvals", event_key=f"server-action:{approval['id']}", dedupe_seconds=0)
    return {"ok": True, "approval": approval, "work_item": item, "relation": relation, "message": "已建立审批和执行日志；不会直接改动服务器。"}


@app.get("/api/server/actions/executions")
def get_server_action_executions(limit: int = 80) -> dict[str, Any]:
    return {"executions": list_server_action_executions(limit), "policy": "只读检查可在批准后执行；日志查看和重启保留服务器侧人工边界。"}


@app.post("/api/server/actions/{approval_id}/execute")
async def execute_server_action(approval_id: str) -> dict[str, Any]:
    return await execute_approved_server_action(approval_id)


@app.post("/api/server/actions/executions/{execution_id}/rollback")
def rollback_server_action(execution_id: str) -> dict[str, Any]:
    return rollback_server_action_execution(execution_id)






# ═══════════════ 产品作战室：反馈 → 需求 → 决策 → PRD ═══════════════

PRODUCT_FEEDBACK_STATUSES = {"new", "reviewing", "linked", "archived"}
PRODUCT_REQUIREMENT_STATUSES = {"discovering", "review", "planned", "building", "shipped", "paused"}
PRODUCT_DECISION_STATUSES = {"proposed", "decided", "revisiting", "superseded"}

# The browser-facing crawl object remains in memory for fast polling, while
# its request, result, timeline and failure state are persisted in agent_runs.
# A later worker deployment can replace this cache without changing the API.
runs: dict[str, dict[str, Any]] = {}
run_tasks: dict[str, Any] = {}
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
