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
from app_pkg.chat import *  # noqa: F401,F403
from app_pkg.approvals import *  # noqa: F401,F403
from app_pkg.agent_queue import *  # noqa: F401,F403
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






class CloudDevRequest(BaseModel):
    command: str = Field(min_length=1, max_length=400)
    confirmed: bool = False




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
    return {"name": "NEXUS", "version": WORKBENCH_VERSION, "data_dir": str(DATA_DIR)}












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







# The browser-facing crawl object remains in memory for fast polling, while
# its request, result, timeline and failure state are persisted in agent_runs.
# A later worker deployment can replace this cache without changing the API.
runs: dict[str, dict[str, Any]] = {}
run_tasks: dict[str, Any] = {}
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ---------------------------------------------------------------------------
# 应用层鉴权（纵深防御）
#
# 线上所有保护都来自 nginx 的 auth_basic。这意味着 nginx 配置改错一行、或者
# 服务器上任何本地进程直连 127.0.0.1:18765，全部 300 多个接口就完全裸奔——包括
# 写库、触发 Agent、提交云开发构建。
#
# 这里加一道独立的应用层校验：设置 WORKBENCH_API_TOKEN 后，非白名单路径必须带
# X-Workbench-Token 头或 workbench_token Cookie。默认不设置时保持原有行为
# （只依赖 nginx），所以升级不会把自己锁在门外；确认前端能正常带 token 之后再
# 在 .env 里打开即可。
# ---------------------------------------------------------------------------
AUTH_EXEMPT_PREFIXES = ("/static/", "/feishu/")
AUTH_EXEMPT_PATHS = {
    "/api/health",              # 健康检查脚本，无凭证
    "/api/git/inventory-push",  # 有自己的 WORKBENCH_GIT_PUSH_TOKEN 常数时间校验
    _SUB2API_CORS_PATH,         # 有自己的 Origin 校验
    "/favicon.ico",
    "/manifest.webmanifest",
}


@app.middleware("http")
async def app_token_auth_middleware(request: Request, call_next: Any) -> Any:
    expected = os.getenv("WORKBENCH_API_TOKEN", "").strip()
    if not expected:
        return await call_next(request)
    path = request.url.path
    if path in AUTH_EXEMPT_PATHS or path.startswith(AUTH_EXEMPT_PREFIXES) or request.method == "OPTIONS":
        return await call_next(request)
    provided = str(request.headers.get("x-workbench-token") or request.cookies.get("workbench_token") or "")
    if not secrets.compare_digest(provided, expected):
        from starlette.responses import JSONResponse as _JSONResponse

        log.warning("拒绝未携带有效 token 的请求：%s %s", request.method, path)
        return _JSONResponse({"detail": "缺少或错误的 Workbench 访问令牌"}, status_code=401)
    return await call_next(request)



# ---------------------------------------------------------------------------
# 项目插拔（页面 / API 层）：enabled=false 的项目，其页面与业务 API 直接 404。
# 与入口层（首页过滤）不同——这里是纵深防御：即使有人直接输 URL 或调 API，
# 禁用项目也拿不到任何东西。中间件按路径前缀映射项目 id；部署级配置是静态
# 快照（改 projects.json 后需重启生效），与工具注册表的行为保持一致。
# ---------------------------------------------------------------------------
def _disabled_project_ids_at_startup() -> frozenset[str]:
    try:
        values = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    return frozenset(str(item.get("id")) for item in values if isinstance(item, dict) and item.get("enabled") is False)


_PROJECT_DISABLED_IDS = _disabled_project_ids_at_startup()

# 路径前缀 → 项目 id；None 表示从路径段解析（/projects/{id}）。
_PROJECT_PATH_RULES: list[tuple[str, str | None]] = [
    ("/crawl4ai", "crawl4ai"),
    ("/projects/", None),
    ("/api/inbox", "inbox"),
    ("/api/knowledge", "knowledge"),
    ("/api/doc-factory", "doc-factory"),
    ("/api/sub2api", "sub2api"),
    ("/api/market", "market"),
    ("/api/server", "server"),
    ("/api/aihot", "aihot"),
    ("/api/cid", "cid-dashboard"),
    ("/api/idea", "idea-analysis"),
    ("/api/product", "product-manager"),
    ("/api/ai-learning", "ai-learning"),
    ("/api/embodied", "embodied"),
    ("/api/crawl", "crawl4ai"),
    ("/api/browser", "crawl4ai"),
    ("/api/research", "web-research"),
    ("/api/cloud-dev", "cloud-dev"),
]


def _path_project_id(path: str) -> str | None:
    for prefix, project_id in _PROJECT_PATH_RULES:
        if path.startswith(prefix):
            if project_id:
                return project_id
            seg = path[len(prefix):].strip("/").split("/")[0]
            return seg or None
    if path.startswith("/api/agent/"):
        return path[len("/api/agent/"):].split("/")[0] or None
    return None


@app.middleware("http")
async def project_enabled_gate(request: Request, call_next: Any) -> Any:
    if _PROJECT_DISABLED_IDS:
        project_id = _path_project_id(request.url.path)
        if project_id and project_id in _PROJECT_DISABLED_IDS:
            from starlette.responses import JSONResponse as _JSONResponse

            return _JSONResponse({"detail": "项目未启用"}, status_code=404)
    return await call_next(request)
