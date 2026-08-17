"""Workbench 项目领域：项目入口/偏好/首页卡片聚合/联动审计。

从 app.py 拆出的 projects 模块（为开源准备）。包含 PROJECT_LINKS 联动边定义、
项目入口（load_projects/public_projects）、首页卡片聚合（project_activity_batch/
project_activity）、联动审计（project_link_audit/project_audit/project_data_freshness）。
agent_run_summary/agent_detail/agent_quality_metrics/knowledge_files/aihot 快照等
仍在 app.py 的函数走 _app_call 运行时转发；evidence_edge_summary 从 evidence 导入。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import cloud_dev
from fastapi import HTTPException
from pydantic import BaseModel, Field
from urllib.parse import quote

from .agent_platform import AGENT_REGISTRY, AGENT_STATUS_LABELS, AGENT_TOOL_POLICIES
from .core import (
    OUTPUTS_DIR,
    PROJECTS_FILE,
    PROJECT_PREFERENCES_FILE,
    WORKBENCH_VERSION,
    clip,
    log,
    now_iso,
    save_json_atomic,
)
from .db import db_connection, db_scope
from .evidence import evidence_edge_summary
from .inbox import list_inbox
from .instance import app
from .llm import llm_settings
from .notifications import agent_run_row, list_notifications
from .server import analyze_server_snapshot, list_server_monitor_history, load_server_monitor_snapshot
from .sub2api import analyze_sub2api_snapshot, load_market_snapshot, load_sub2api_snapshot
from .usage import USAGE_EXCLUDED_RUN_KINDS


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def load_project_preferences() -> dict[str, Any]:
    try:
        values = json.loads(PROJECT_PREFERENCES_FILE.read_text(encoding="utf-8"))
        return values if isinstance(values, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_project_preferences(values: dict[str, Any]) -> None:
    save_json_atomic(PROJECT_PREFERENCES_FILE, values, 0o600)


def _load_configured_projects() -> list[dict[str, Any]]:
    """读取 projects.json 原始项目列表，不应用任何用户显示偏好。

    首次启动时 projects.json 不存在（开源包默认只带 projects.open-source.json
    模板）：自动回退到开源模板，保证开箱即有项目入口。
    """
    for path in (PROJECTS_FILE, PROJECTS_FILE.parent / "projects.open-source.json"):
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(values, list):
            return [item for item in values if isinstance(item, dict) and item.get("id")]
    return []


def load_projects() -> list[dict[str, Any]]:
    values = _load_configured_projects()
    projects = values
    preferences = load_project_preferences()
    order = [str(item) for item in preferences.get("order", []) if item]
    order_index = {project_id: index for index, project_id in enumerate(order)}
    favorites = {str(item) for item in preferences.get("favorite_ids", []) if item}
    hidden_ids = {str(item) for item in preferences.get("hidden_ids", []) if item}
    groups = preferences.get("groups", {}) if isinstance(preferences.get("groups"), dict) else {}
    for project in projects:
        project_id = str(project.get("id"))
        if "favorite_ids" in preferences:
            project["favorite"] = project_id in favorites
        if project_id in groups and str(groups[project_id]).strip():
            project["group"] = str(groups[project_id]).strip()
    projects = [project for project in projects if str(project.get("id")) not in hidden_ids]
    return sorted(projects, key=lambda item: (order_index.get(str(item.get("id")), len(order_index)), values.index(item)))


# 首页卡片的 N+1：每张卡片各查一次自己的工作项计数、Agent 运行计数和最近一次
# 运行，16 个项目就是 48 次查询。实测 /api/projects 一次请求跑了 242 条 SQL。
# 这三份数据都可以一次 GROUP BY 全部算出来，所以先批量取好，再按项目分发。
#
# 用一个显式传下去的 dict 而不是全局缓存：调用方看得见自己在用批量数据，
# 也不会出现「首页拿到的是三分钟前的快照」这种说不清的新鲜度问题。
def project_activity_batch(project_ids: list[str]) -> dict[str, Any]:
    ids = [str(item) for item in project_ids if str(item)]
    if not ids:
        return {"work_items": {}, "runs": {}, "latest": {}}
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    work_items: dict[str, dict[str, int]] = {}
    runs: dict[str, dict[str, int]] = {}
    latest: dict[str, Any] = {}
    connection = db_connection()
    try:
        # 一个工作项可能既属于来源项目也属于目标项目，所以按两个方向各聚合一次
        # 再合并——这和原来「WHERE source = ? OR target = ?」的口径一致。
        for column in ("source_project", "target_project"):
            for row in connection.execute(
                f"""SELECT {column} AS project, status, COUNT(*) AS count FROM work_items
                WHERE NOT (status = 'failed' AND updated_at < ?)
                GROUP BY {column}, status""",
                (since,),
            ).fetchall():
                project = str(row["project"] or "")
                if not project:
                    continue
                work_items.setdefault(project, {})
                work_items[project][str(row["status"])] = work_items[project].get(str(row["status"]), 0) + int(row["count"])
        for row in connection.execute(
            "SELECT project_id, status, COUNT(*) AS count FROM agent_runs WHERE kind NOT IN (?, ?, ?, ?) GROUP BY project_id, status",
            USAGE_EXCLUDED_RUN_KINDS,
        ).fetchall():
            project = str(row["project_id"] or "")
            if project:
                runs.setdefault(project, {})[str(row["status"])] = int(row["count"])
        # 每个项目最近一次运行：按 (project_id, created_at DESC) 排一遍，取每组第一条。
        seen: set[str] = set()
        for row in connection.execute(
            "SELECT * FROM agent_runs WHERE kind NOT IN (?, ?, ?, ?) ORDER BY project_id, created_at DESC",
            USAGE_EXCLUDED_RUN_KINDS,
        ).fetchall():
            project = str(row["project_id"] or "")
            if project and project not in seen:
                seen.add(project)
                latest[project] = agent_run_row(row)
    finally:
        connection.close()
    return {"work_items": work_items, "runs": runs, "latest": latest}


def project_activity(project_id: str, batch: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the small, actionable status summary used by the home cards.

    The project card must reflect what needs attention now, not just whether a
    route exists. Keep this payload deliberately small: no run request/result
    bodies leave the server, only counts and a safe latest-run title.
    """
    if batch is not None:
        work_items = dict(batch.get("work_items", {}).get(project_id, {}))
    else:
        connection = db_connection()
        try:
            # "failed" 工作项只统计最近 7 天内仍有动作的，避免历史失败永远占据卡片。
            since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            rows = connection.execute(
                """SELECT status, COUNT(*) AS count FROM work_items
                WHERE (source_project = ? OR target_project = ?)
                  AND NOT (status = 'failed' AND updated_at < ?)
                GROUP BY status""",
                (project_id, project_id, since),
            ).fetchall()
            work_items = {str(row["status"]): int(row["count"]) for row in rows}
        finally:
            connection.close()

    runs = _app_call("agent_run_summary", project_id, batch=batch)
    latest = runs.get("latest") or {}
    open_count = work_items.get("open", 0)
    running_count = work_items.get("running", 0)
    blocked_count = work_items.get("blocked", 0)
    failed_count = work_items.get("failed", 0)
    # 只有 24 小时内的失败运行才标记"失败待恢复"，避免历史失败常驻。
    latest_ts = str(latest.get("updated_at") or latest.get("created_at") or "")
    latest_failed = bool(latest and latest.get("status") == "failed")
    if latest_failed and latest_ts:
        try:
            parsed_latest = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
            if parsed_latest.tzinfo is None:
                parsed_latest = parsed_latest.replace(tzinfo=timezone.utc)
            latest_failed = (datetime.now(timezone.utc) - parsed_latest) <= timedelta(hours=24)
        except ValueError:
            latest_failed = False
    if failed_count or latest_failed:
        tone = "warning"
        label = f"{failed_count + (1 if latest_failed else 0)} 个失败待恢复"
    elif blocked_count:
        tone = "warning"
        label = f"{blocked_count} 个待确认"
    elif running_count or runs.get("active", 0):
        tone = "online"
        label = "Agent 运行中"
    elif open_count:
        tone = "online"
        label = f"{open_count} 个待处理"
    else:
        tone = "idle"
        label = "暂无待处理"
    return {
        "tone": tone,
        "label": label,
        "signal": tone != "idle",
        "work_items": {
            "open": open_count,
            "running": running_count,
            "blocked": blocked_count,
            "failed": failed_count,
        },
        "active_runs": int(runs.get("active", 0)),
        "failed_runs": int(runs.get("failed", 0)),
        "latest_run": {
            "status": latest.get("status", "") if latest else "",
            "status_label": latest.get("status_label", "") if latest else "",
            "title": clip(str(latest.get("title") or ""), 100) if latest else "",
            "updated_at": latest.get("updated_at") or latest.get("created_at", "") if latest else "",
        },
    }


def public_projects() -> list[dict[str, Any]]:
    # 首页卡片是纯读聚合：15 个项目 x 若干 helper，每个 helper 原本各开一次连接。
    # 套一层 db_scope 后全部复用同一个连接，实测 66 次 open 降到 1 次。
    with db_scope():
        return _public_projects_uncached()


def _public_projects_uncached() -> list[dict[str, Any]]:
    projects = []
    pending_count = len(_app_call("list_inbox", "inbox"))
    note_count = len(_app_call("knowledge_files", ))
    output_files = [path for path in OUTPUTS_DIR.iterdir() if path.is_file() and not path.name.startswith(".")]
    sub2api = _app_call("load_sub2api_snapshot")
    market = _app_call("load_market_snapshot")
    server = _app_call("load_server_monitor_snapshot")
    # 用 find_spec 只查「是否安装」不执行导入——import crawl4ai 光导入就要
    # 800ms+（async_webcrawler/async_database/async_logger 一堆依赖），而首页
    # /api/projects 每次都要走这里，服务重启后的首个请求会白白慢近一秒。
    crawl_available = importlib.util.find_spec("crawl4ai") is not None
    all_projects = _app_call("load_projects")
    # 项目插拔：projects.json 里 enabled=false 的项目不进入首页/导航（部署级开关，
    # 与运行时的隐藏（×）不同——那是个人偏好，这是这份部署要不要这个项目）。
    all_projects = [item for item in all_projects if item.get("enabled") is not False]
    # 一次把所有项目的计数取齐，替代每张卡片各查一遍。
    activity_batch = project_activity_batch([str(item.get("id") or "") for item in all_projects] + ["crawl4ai"])
    for project in all_projects:
        public = {key: value for key, value in project.items() if key not in {"source_path", "source_env"}}
        project_id = project.get("id")
        agent = AGENT_REGISTRY.get(project_id, {})
        public["agent_name"] = agent_display_name(project_id)
        public["agent_status"] = agent.get("status", "planned")
        public["agent_status_label"] = agent_status_label(public["agent_status"])
        activity = _app_call("project_activity", project_id, batch=activity_batch)
        # 网页研究浏览器首版复用同一套 Crawl Worker/SQLite 队列，避免再维护
        # 一份并发、取消、重试和 Artifact 状态；页面层仍作为独立 Workbench 项目呈现。
        if project_id == "web-research":
            activity = _app_call("project_activity", "crawl4ai", batch=activity_batch)
        public["activity"] = activity
        freshness = _app_call("project_data_freshness", project_id)
        if project_id == "web-research":
            freshness = _app_call("project_data_freshness", "crawl4ai")
        public["freshness"] = freshness
        public["health"] = {
            "tone": "danger" if activity["tone"] == "warning" or freshness["status"] in {"stale", "missing"} else "good",
            "label": "需要关注" if activity["tone"] == "warning" else freshness.get("label") or "状态正常",
            "detail": freshness.get("detail") or freshness.get("source") or "暂无数据来源",
            "source": freshness.get("source") or "本地 SQLite",
            "data_as_of": freshness.get("checked_at") or "",
            "open_work_items": int((activity.get("work_items") or {}).get("open", 0) or 0),
            "blocked_work_items": int((activity.get("work_items") or {}).get("blocked", 0) or 0),
            "failed_work_items": int((activity.get("work_items") or {}).get("failed", 0) or 0),
            "active_runs": int(activity.get("active_runs", 0) or 0),
            "failed_runs": int(activity.get("failed_runs", 0) or 0),
        }
        public["open_work_items"] = activity["work_items"]["open"]
        public["state"] = {"tone": "online" if project.get("status", "ready") == "ready" else "offline", "label": "项目可用"}
        if project_id == "inbox":
            public["summary"] = {"value": pending_count, "label": "条待处理", "detail": "随手记录，稍后整理"}
            public["primary_action"] = {"label": "记录一条", "href": "/projects/inbox#inbox-content"}
        elif project_id == "knowledge":
            public["summary"] = {"value": note_count, "label": "篇本地笔记", "detail": "Markdown 持久保存"}
            public["primary_action"] = {"label": "写一篇笔记", "href": "/projects/knowledge#note-title"}
        elif project_id == "doc-factory":
            latest = max(output_files, key=lambda path: path.stat().st_mtime, default=None)
            public["summary"] = {"value": len(output_files), "label": "份已生成产物", "detail": latest.name if latest else "还没有生成记录"}
            public["primary_action"] = {"label": "生成一份", "href": "/projects/doc-factory#factory-form"}
        elif project_id == "crawl4ai":
            crawl_runs = _app_call("agent_run_summary", "crawl4ai")
            active_runs = crawl_runs.get("active", 0)
            latest_run = crawl_runs.get("latest") or {}
            public["summary"] = {"value": active_runs, "label": "个进行中任务", "detail": latest_run.get("title") or ("浏览器引擎已连接" if crawl_available else "等待安装 Crawl4AI")}
            public["primary_action"] = {"label": "开始研究", "href": "/crawl4ai#crawl-form"}
        elif project_id == "web-research":
            crawl_runs = _app_call("agent_run_summary", "crawl4ai")
            active_runs = crawl_runs.get("active", 0)
            latest_run = crawl_runs.get("latest") or {}
            public["summary"] = {"value": active_runs, "label": "个进行中任务", "detail": latest_run.get("title") or ("研究上下文已就绪" if crawl_available else "等待安装 Crawl4AI")}
            public["primary_action"] = {"label": "打开研究浏览器", "href": "/projects/web-research"}
        elif project_id == "cloud-dev":
            workspaces = cloud_dev.workspace_map()
            public["summary"] = {"value": len(workspaces), "label": "个白名单工作区", "detail": "状态/测试自动执行，构建需审批" if workspaces else "尚未配置 WORKBENCH_CLOUD_WORKSPACES"}
            public["state"] = {"tone": "online" if workspaces else "offline", "label": "云开发入口可用" if workspaces else "等待配置工作区"}
            public["primary_action"] = {"label": "打开云开发", "href": "/projects/cloud-dev"}
        elif project_id == "aihot":
            feed = _app_call("load_aihot_snapshot", )
            items = _app_call("dedupe_aihot_items", feed.get("items") or [])
            latest = items[0].get("title") if items else "点击同步 AI 热点"
            public["summary"] = {"value": len(items), "label": "条精选资讯", "detail": latest}
            public["state"] = {"tone": "online" if items else "offline", "label": "AI 热点已同步" if items else "等待同步"}
            public["primary_action"] = {"label": "查看热点", "href": "/projects/aihot"}
        elif project_id == "idea-analysis":
            sessions = _app_call("list_idea_sessions", limit=3)
            public["summary"] = {"value": len(sessions), "label": "个想法记录", "detail": sessions[0].get("title", "从一个奇怪想法开始") if sessions else "随时丢一个想法进来"}
            public["primary_action"] = {"label": "分析一个想法", "href": "/projects/idea-analysis"}
        elif project_id == "product-manager":
            overview = _app_call("product_manager_overview", )
            summary = overview.get("summary", {})
            public["summary"] = {
                "value": summary.get("active_requirements", 0),
                "label": "个进行中需求",
                "detail": f"{summary.get('new_feedback', 0)} 条新反馈 · {summary.get('needs_evidence', 0)} 个需求缺证据",
            }
            public["primary_action"] = {"label": "打开产品作战室", "href": "/projects/product-manager"}
        elif project_id == "cid-dashboard":
            latest_snapshot = _app_call("list_cid_dashboard_snapshots", limit=1)
            latest_snapshot = latest_snapshot[0] if latest_snapshot else None
            public["state"] = {"tone": "online" if latest_snapshot else "offline", "label": "快照已保存" if latest_snapshot else "等待首次加载"}
            public["summary"] = {
                "value": latest_snapshot.get("project_count", "—") if latest_snapshot else "—",
                "label": "个看板项目" if latest_snapshot else "尚未保存快照",
                "detail": f"{latest_snapshot.get('repo', '')} · 数据 {latest_snapshot.get('fetched_at', '')[:16].replace('T', ' ')}" if latest_snapshot else "打开看板后自动保存快照",
            }
            public["primary_action"] = {"label": "打开看板", "href": "/projects/cid-dashboard"}
        elif project_id == "ai-learning":
            learning_history = _app_call("list_ai_learning_lessons", 30)
            learning_stats = _app_call("ai_learning_stats", learning_history)
            latest_lesson = learning_history[0] if learning_history else None
            public["summary"] = {
                "value": learning_stats.get("streak", 0),
                "label": "天连续学习",
                "detail": latest_lesson.get("title", "打开开始第 1 课") if latest_lesson else "打开开始第 1 课",
            }
            public["state"] = {"tone": "online", "label": "今日已完成" if latest_lesson and latest_lesson.get("lesson_date") == _app_call("ai_learning_today", ) and latest_lesson.get("completed") else "今日待学习"}
            public["freshness"] = {
                "status": "fresh" if latest_lesson else "ready",
                "status_label": "今日已更新" if latest_lesson else "课程就绪",
                "label": "今日课程已准备" if latest_lesson else "打开后生成今日课程",
                "checked_at": latest_lesson.get("updated_at", "") if latest_lesson else "",
                "age_seconds": None,
                "source": "学习进度",
                "detail": f"已完成 {learning_stats.get('completed', 0)} 课 · 本周 {learning_stats.get('weekly_completed', 0)}/5",
            }
            public["health"] = {
                "tone": "good",
                "label": "学习计划就绪",
                "detail": f"连续 {learning_stats.get('streak', 0)} 天 · 自测正确率 {learning_stats.get('quiz_accuracy', 0)}%",
                "source": "本地学习记录",
                "data_as_of": latest_lesson.get("updated_at", "") if latest_lesson else "",
                "open_work_items": 0,
                "blocked_work_items": 0,
                "failed_work_items": 0,
                "active_runs": 0,
                "failed_runs": 0,
            }
            public["primary_action"] = {"label": "开始今日学习", "href": "/projects/ai-learning"}
        elif project_id == "market":
            quotes = market.get("quotes") or []
            market_analysis = _app_call("analyze_market_snapshot", market, _app_call("list_market_history", limit=8))
            changes = []
            for quote in quotes[:3]:
                name = quote.get("name") or quote.get("symbol") or "自选"
                change = quote.get("change_pct")
                changes.append(f"{name} {change:+.2f}%" if isinstance(change, (float, int)) else f"{name} —")
            public["summary"] = {
                "value": len(market.get("watchlist") or []),
                "label": "只自选",
                "detail": " · ".join(changes) + (f" · {market_analysis['freshness']['label']}" if changes else market_analysis["freshness"]["label"]),
            }
            public["primary_action"] = {"label": "查看行情", "href": "/projects/market"}
        elif project_id == "server":
            disk = server.get("disk") or {}
            server_analysis = analyze_server_snapshot(server, list_server_monitor_history(limit=8))
            server_status_label = server_analysis.get("status_label", "未检查")
            server_failed = server.get("status") == "error"
            public["state"] = {
                "tone": "offline" if server_failed else ("online" if server_analysis.get("status") == "ok" else "offline"),
                "label": server_status_label,
            }
            public["summary"] = {
                "value": disk.get("used_pct", "—"),
                "label": f"磁盘已用 · {server_analysis.get('freshness', {}).get('label', '无数据')}",
                "detail": f"{server.get('host', 'your-server.example.com')} · 内存 {server.get('memory', {}).get('used_mb', '—')}/{server.get('memory', {}).get('total_mb', '—')} MB · Nginx {server.get('nginx', '—')}",
            }
            public["primary_action"] = {"label": "查看监控", "href": "/projects/server"}
            if server_failed:
                public["health"] = {
                    "tone": "danger",
                    "label": "检查失败",
                    "detail": (server.get("error") or "服务器探测失败，点击刷新重试").splitlines()[0][:120],
                }
        elif project_id == "sub2api":
            subscription = sub2api.get("subscription") or {}
            analysis = analyze_sub2api_snapshot(sub2api)
            weekly_usage = str(subscription.get("weekly_usage", "—"))
            weekly_parts = [part.strip() for part in weekly_usage.split("/", 1)]
            healthy_freshness = analysis["freshness"]["status"] in {"fresh", "aging"}
            public["state"] = {"tone": "online" if sub2api.get("logged_in") and healthy_freshness else "offline", "label": analysis["status_label"] if sub2api.get("logged_in") else "Sub2API 未同步"}
            # 周额度剩余 = 总额度 - 已用；过期时明确提示需要重新同步
            remaining_weekly = "—"
            try:
                if len(weekly_parts) == 2:
                    used_val = float(re.sub(r"[^0-9.]", "", weekly_parts[0]) or 0)
                    total_val = float(re.sub(r"[^0-9.]", "", weekly_parts[1]) or 0)
                    if total_val > 0:
                        remaining_weekly = f"剩余 ${total_val - used_val:.2f}"
            except Exception:
                log.debug("忽略异常（_public_projects_uncached）", exc_info=True)
            freshness_note = analysis["freshness"]["label"]
            if not healthy_freshness:
                freshness_note = f"{freshness_note}，需重新同步"
            public["summary"] = {
                "value": weekly_parts[0] if weekly_parts else "—",
                "label": f"/ {weekly_parts[1]} 周额度" if len(weekly_parts) > 1 else "周额度",
                "detail": f"{remaining_weekly} · {len(sub2api.get('keys', []))} 个 Key · {freshness_note}",
            }
            public["primary_action"] = {"label": "查看账户", "href": "/projects/sub2api"}
        if public["state"].get("tone") == "online" and activity["tone"] == "warning":
            public["state"] = {"tone": "warning", "label": activity["label"]}
        elif public["state"].get("tone") == "online" and activity["tone"] == "online" and activity["active_runs"]:
            public["state"] = {"tone": "online", "label": "Agent 运行中"}
        projects.append(public)
    return projects



PROJECT_LINKS: list[dict[str, str]] = [
    {"from": "inbox", "to": "crawl4ai", "relation": "research_task", "label": "收件箱研究任务"},
    {"from": "inbox", "to": "web-research", "relation": "browser_research_task", "label": "收件箱网页研究"},
    {"from": "inbox", "to": "idea-analysis", "relation": "idea_review", "label": "收件箱想法分析"},
    {"from": "inbox", "to": "product-manager", "relation": "product_signal", "label": "收件箱产品线索"},
    {"from": "inbox", "to": "knowledge", "relation": "note_capture", "label": "收件箱沉淀笔记"},
    {"from": "inbox", "to": "doc-factory", "relation": "document_task", "label": "收件箱文档任务"},
    {"from": "inbox", "to": "market", "relation": "market_research", "label": "收件箱行情研究"},
    {"from": "inbox", "to": "server", "relation": "incident_to_task", "label": "收件箱服务器排查"},
    {"from": "inbox", "to": "sub2api", "relation": "quota_alert", "label": "收件箱额度提醒"},
    {"from": "crawl4ai", "to": "knowledge", "relation": "evidence_to_note", "label": "网页证据沉淀"},
    {"from": "crawl4ai", "to": "doc-factory", "relation": "evidence_to_document", "label": "网页证据成文档"},
    {"from": "crawl4ai", "to": "product-manager", "relation": "research_to_product", "label": "网页证据进入需求"},
    {"from": "web-research", "to": "knowledge", "relation": "browser_evidence_to_note", "label": "研究浏览器沉淀笔记"},
    {"from": "web-research", "to": "doc-factory", "relation": "browser_evidence_to_document", "label": "研究浏览器生成文档"},
    {"from": "web-research", "to": "product-manager", "relation": "browser_evidence_to_product", "label": "研究证据进入需求"},
    {"from": "inbox", "to": "cloud-dev", "relation": "cloud_dev_task", "label": "收件箱云开发任务"},
    {"from": "aihot", "to": "idea-analysis", "relation": "signal_to_idea", "label": "热点转想法验证"},
    {"from": "aihot", "to": "crawl4ai", "relation": "signal_research", "label": "热点深度研究"},
    {"from": "aihot", "to": "knowledge", "relation": "signal_to_note", "label": "热点沉淀笔记"},
    {"from": "aihot", "to": "doc-factory", "relation": "signal_brief", "label": "热点生成简报"},
    {"from": "aihot", "to": "ai-learning", "relation": "signal_to_learning", "label": "热点转学习案例"},
    {"from": "cid-dashboard", "to": "idea-analysis", "relation": "opportunity_to_idea", "label": "看板机会验证"},
    {"from": "cid-dashboard", "to": "crawl4ai", "relation": "project_research", "label": "看板项目研究"},
    {"from": "cid-dashboard", "to": "knowledge", "relation": "opportunity_to_note", "label": "看板机会笔记"},
    {"from": "idea-analysis", "to": "product-manager", "relation": "validated_idea_to_product", "label": "验证想法进入需求池"},
    {"from": "idea-analysis", "to": "inbox", "relation": "validation_to_task", "label": "验证任务进入收件箱"},
    {"from": "product-manager", "to": "doc-factory", "relation": "requirement_to_prd", "label": "产品需求生成 PRD"},
    {"from": "product-manager", "to": "knowledge", "relation": "decision_to_note", "label": "产品决策沉淀"},
    {"from": "product-manager", "to": "inbox", "relation": "product_action", "label": "产品行动进入待办"},
    {"from": "market", "to": "knowledge", "relation": "market_to_note", "label": "行情研究笔记"},
    {"from": "market", "to": "inbox", "relation": "market_alert", "label": "行情观察提醒"},
    {"from": "market", "to": "doc-factory", "relation": "market_report", "label": "行情周报"},
    {"from": "server", "to": "inbox", "relation": "incident_to_task", "label": "服务器异常待办"},
    {"from": "server", "to": "knowledge", "relation": "incident_to_note", "label": "服务器事件沉淀"},
    {"from": "sub2api", "to": "inbox", "relation": "quota_alert", "label": "额度/到期提醒"},
    {"from": "sub2api", "to": "doc-factory", "relation": "usage_report", "label": "用量报告"},
    {"from": "knowledge", "to": "doc-factory", "relation": "knowledge_to_document", "label": "知识生成文档"},
    {"from": "ai-learning", "to": "knowledge", "relation": "learning_to_note", "label": "学习复盘沉淀"},
    {"from": "ai-learning", "to": "crawl4ai", "relation": "learning_research", "label": "学习主题深挖"},
]

def project_link_summary(project_id: str) -> dict[str, list[dict[str, str]]]:
    inbound = [edge for edge in PROJECT_LINKS if edge["to"] == project_id]
    outbound = [edge for edge in PROJECT_LINKS if edge["from"] == project_id]
    return {"inbound": inbound, "outbound": outbound}


def agent_display_name(project_id: str) -> str:
    """Return the user-facing Chinese name while keeping project IDs internal."""
    capability = AGENT_REGISTRY.get(project_id, {})
    if capability.get("name"):
        return str(capability["name"])
    project = next((item for item in load_projects() if item.get("id") == project_id), {})
    return f"{project.get('title', project_id)} Agent"


def agent_status_label(status: str) -> str:
    return AGENT_STATUS_LABELS.get(status, status or "未定义")


def project_href(project_id: str) -> str:
    if project_id == "workbench":
        return "/"
    project = next((item for item in load_projects() if item.get("id") == project_id), {})
    return str(project.get("href") or f"/projects/{project_id}")


def public_project_link(edge: dict[str, str]) -> dict[str, str]:
    """Add user-facing names and URLs without changing the internal edge shape."""
    source = edge.get("from", "")
    target = edge.get("to", "")
    return {
        **edge,
        "from_name": agent_display_name(source),
        "to_name": agent_display_name(target),
        "from_href": project_href(source),
        "to_href": project_href(target),
    }


AUDIT_STATUS_LABELS = {
    "verified": "已验证",
    "synthetic": "仅合成验收",
    "legacy": "历史未分类",
    "observed": "有运行记录",
    "partial": "部分验证",
    "configured": "仅配置未验证",
    "blocked": "能力被阻断",
    "failed": "失败待修复",
    "missing": "暂无数据",
    "fresh": "数据新鲜",
    "aging": "数据较旧",
    "stale": "数据已过期",
}


def _audit_datetime(value: Any) -> datetime | None:
    """Parse the few timestamp shapes used by local snapshots and SQLite."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for pattern in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def audit_freshness(timestamp: Any, source: str = "", detail: str = "") -> dict[str, Any]:
    checked_at = str(timestamp or "")
    parsed = _audit_datetime(timestamp)
    age_seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds())) if parsed else None
    if parsed is None:
        status = "missing"
        label = "没有可用数据时间"
    elif age_seconds is not None and age_seconds <= 6 * 3600:
        status = "fresh"
        label = "数据新鲜"
    elif age_seconds is not None and age_seconds <= 24 * 3600:
        status = "aging"
        label = "数据较旧"
    else:
        status = "stale"
        label = "数据已过期"
    return {
        "status": status,
        "status_label": AUDIT_STATUS_LABELS.get(status, status),
        "label": label,
        "checked_at": checked_at,
        "age_seconds": age_seconds,
        "source": source,
        "detail": detail,
    }


def project_data_freshness(project_id: str) -> dict[str, Any]:
    """Expose the source timestamp an Agent is actually using right now."""
    timestamp = ""
    source = "本地 SQLite"
    detail = ""
    if project_id == "sub2api":
        snapshot = load_sub2api_snapshot()
        timestamp = snapshot.get("checked_at") or snapshot.get("fetched_at", "")
        source = "Sub2API 脱敏快照"
        detail = (analyze_sub2api_snapshot(snapshot).get("status_label") or "账户快照")
    elif project_id == "market":
        snapshot = load_market_snapshot()
        timestamp = snapshot.get("checked_at", "")
        source = str(snapshot.get("source") or "公开行情快照")
        detail = f"{len(snapshot.get('quotes') or [])} 个报价"
    elif project_id == "server":
        snapshot = load_server_monitor_snapshot()
        timestamp = snapshot.get("checked_at", "")
        source = "服务器只读监控快照"
        detail = str(snapshot.get("status") or "未检查")
    elif project_id == "aihot":
        snapshot = _app_call("load_aihot_snapshot", )
        timestamp = snapshot.get("fetched_at", "")
        source = "aihot.today 资讯快照"
        detail = f"{len(snapshot.get('items') or [])} 条资讯"
    elif project_id == "cid-dashboard":
        snapshot = _app_call("list_cid_dashboard_snapshots", limit=1)
        latest = snapshot[0] if snapshot else {}
        timestamp = latest.get("fetched_at", "")
        source = "独立开发者看板快照"
        detail = f"{latest.get('project_count', 0)} 个项目" if latest else "尚未保存快照"
    elif project_id == "knowledge":
        status = _app_call("obsidian_status", )
        timestamp = status.get("last_indexed_at") or (status.get("last_scan") or {}).get("scanned_at", "")
        source = "Obsidian 本地索引"
        detail = f"{status.get('note_count', 0)} 篇笔记"
    elif project_id == "doc-factory":
        paths = [path for path in OUTPUTS_DIR.iterdir() if path.is_file() and not path.name.startswith(".")]
        latest = max(paths, key=lambda path: path.stat().st_mtime, default=None)
        timestamp = latest.stat().st_mtime if latest else ""
        source = "工作台输出目录"
        detail = latest.name if latest else "尚未生成产物"
    elif project_id == "inbox":
        connection = db_connection()
        try:
            row = connection.execute("SELECT updated_at FROM inbox ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
            timestamp = row["updated_at"] if row else ""
        finally:
            connection.close()
        source = "本地收件箱"
        detail = f"{len(list_inbox('inbox'))} 条待处理"
    elif project_id == "product-manager":
        connection = db_connection()
        try:
            row = connection.execute(
                """SELECT MAX(updated_at) AS updated_at FROM (
                    SELECT updated_at FROM product_feedback
                    UNION ALL SELECT updated_at FROM product_requirements
                    UNION ALL SELECT updated_at FROM product_decisions
                )"""
            ).fetchone()
            timestamp = row["updated_at"] if row and row["updated_at"] else ""
        finally:
            connection.close()
        overview = _app_call("product_manager_overview", )
        source = "产品作战室 SQLite"
        detail = f"{overview['summary']['feedback_total']} 条反馈 · {overview['summary']['requirements_total']} 个需求"
    else:
        latest = (_app_call("agent_run_summary", project_id).get("latest") or {})
        timestamp = latest.get("updated_at") or latest.get("created_at", "")
        source = "Agent Run"
        detail = latest.get("title", "尚未运行")
    return audit_freshness(timestamp, source, detail)


def project_link_audit(edge: dict[str, str]) -> dict[str, Any]:
    """Check whether a configured edge has produced the objects in its contract."""
    source_project = edge.get("from", "")
    target_project = edge.get("to", "")
    work_items = [
        item
        for item in _app_call("list_work_items", project_id=source_project)
        if target_project in {part.strip() for part in str(item.get("target_project", "")).split(",") if part.strip()}
    ]
    work_item_ids = {str(item.get("id")) for item in work_items}
    relations = _app_call("list_relations", )
    matched_relations = []
    for relation in relations:
        metadata = relation.get("metadata") if isinstance(relation.get("metadata"), dict) else {}
        related_ids = {str(relation.get("from_id", "")), str(relation.get("to_id", ""))}
        metadata_projects = {
            str(metadata.get("source_project") or ""),
            str(metadata.get("from_project") or ""),
            str(metadata.get("target_project") or ""),
            str(metadata.get("to_project") or ""),
            str(metadata.get("project_id") or ""),
        }
        if work_item_ids.intersection(related_ids) or target_project in metadata_projects and source_project in metadata_projects:
            matched_relations.append(relation)
        elif relation.get("from_type") == "project" and relation.get("from_id") == source_project and target_project in metadata_projects:
            matched_relations.append(relation)
    relation_ids = {str(relation.get("id")) for relation in matched_relations}
    target_runs = []
    for run in _app_call("list_agent_runs", target_project, limit=100):
        request = run.get("request") if isinstance(run.get("request"), dict) else {}
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        referenced_item = str(request.get("work_item_id") or result.get("work_item_id") or "")
        if referenced_item in work_item_ids:
            target_runs.append(run)
            continue
        if any(str(relation.get("to_id")) == str(run.get("id")) for relation in matched_relations if relation.get("to_type") == "agent_run"):
            target_runs.append(run)
    run_ids = {str(run.get("id")) for run in target_runs}
    notifications = []
    for notification in list_notifications(limit=100):
        event_key = str(notification.get("event_key") or "")
        if notification.get("project_id") == target_project and (
            any(item_id and item_id in event_key for item_id in work_item_ids)
            or any(run_id and run_id in event_key for run_id in run_ids)
        ):
            notifications.append(notification)
    stages = {
        "work_item": bool(work_items),
        "relation": bool(matched_relations),
        "target_run": bool(target_runs),
        "notification": bool(notifications),
    }
    score = sum(stages.values())
    evidence_summary = evidence_edge_summary(f"{source_project}->{target_project}")
    if score == 4 and evidence_summary.get("business_status") == "verified":
        status = "verified"
    elif score == 4 and evidence_summary.get("business_status") == "synthetic_only":
        # A deterministic acceptance run proves that the harness works, not
        # that this edge has carried a real business object recently.
        status = "synthetic"
    elif score == 4 and evidence_summary.get("business_status") == "legacy_unclassified":
        status = "legacy"
    else:
        status = "partial" if score else "configured"
    return {
        **public_project_link(edge),
        "status": status,
        "status_label": AUDIT_STATUS_LABELS[status],
        "score": score,
        "stages": stages,
        "evidence_summary": evidence_summary,
        "evidence": {
            "work_items": len(work_items),
            "relations": len(matched_relations),
            "target_runs": len(target_runs),
            "notifications": len(notifications),
            "work_item_ids": [item.get("id") for item in work_items[:6]],
            "run_ids": [run.get("id") for run in target_runs[:6]],
            "relation_ids": list(relation_ids)[:6],
        },
    }



def project_audit(project_id: str = "") -> dict[str, Any]:
    project_ids = [project_id] if project_id else [item.get("id") for item in load_projects() if item.get("id") in AGENT_REGISTRY]
    configured = bool(llm_settings()["configured"])
    link_rows = [project_link_audit(edge) for edge in PROJECT_LINKS]
    inbound_by_project: dict[str, list[dict[str, Any]]] = {}
    outbound_by_project: dict[str, list[dict[str, Any]]] = {}
    for link in link_rows:
        outbound_by_project.setdefault(link.get("from", ""), []).append(link)
        inbound_by_project.setdefault(link.get("to", ""), []).append(link)
    agents = []
    for current_id in project_ids:
        if current_id not in AGENT_REGISTRY:
            continue
        detail = _app_call("agent_detail", current_id, llm_ready=configured)
        summary = detail.get("run_summary") or {}
        quality = _app_call("agent_quality_metrics", current_id, 24)
        latest = summary.get("latest") or {}
        tool_checks = []
        declared_tools = detail.get("tools") or []
        for tool in declared_tools:
            policy = AGENT_TOOL_POLICIES.get(tool)
            available = bool(policy and policy.get("enabled"))
            reason = "" if available else "工具策略未接入"
            if tool == "global_llm" and not configured:
                available = False
                reason = "尚未配置全局 LLM"
            tool_checks.append({
                "tool": tool,
                "label": policy.get("label", tool) if policy else tool,
                "mode": policy.get("mode", "unavailable") if policy else "unavailable",
                "enabled": available,
                "status": "ready" if available else "blocked",
                "reason": reason,
            })
        if latest.get("status") == "failed":
            run_status = "failed"
        elif summary.get("total", 0):
            run_status = "observed"
        elif any(item["status"] == "blocked" for item in tool_checks):
            run_status = "blocked"
        else:
            run_status = "configured"
        agents.append({
            "project_id": current_id,
            "name": detail.get("name"),
            "status": detail.get("status"),
            "status_label": detail.get("status_label"),
            "audit_status": run_status,
            "audit_status_label": AUDIT_STATUS_LABELS[run_status],
            "implementation": {"implemented": detail.get("implemented_tools", []), "gaps": detail.get("gaps", [])},
            "tool_checks": tool_checks,
            "run_summary": summary,
            "quality": quality,
            "latest_run": latest,
            "activity": project_activity(current_id),
            "freshness": project_data_freshness(current_id),
            "inbound_links": inbound_by_project.get(current_id, []),
            "outbound_links": outbound_by_project.get(current_id, []),
        })
    status_counts: dict[str, int] = {}
    for agent in agents:
        status_counts[agent["audit_status"]] = status_counts.get(agent["audit_status"], 0) + 1
    return {
        "version": WORKBENCH_VERSION,
        "generated_at": now_iso(),
        "summary": {
            "agents": len(agents),
            "configured": status_counts.get("configured", 0),
            "observed": status_counts.get("observed", 0),
            "failed": status_counts.get("failed", 0),
            "blocked": status_counts.get("blocked", 0),
            "verified_links": sum(1 for link in link_rows if link["status"] == "verified"),
            "synthetic_links": sum(1 for link in link_rows if link["status"] == "synthetic"),
            "legacy_links": sum(1 for link in link_rows if link["status"] == "legacy"),
            "partial_links": sum(1 for link in link_rows if link["status"] == "partial"),
            "configured_links": sum(1 for link in link_rows if link["status"] == "configured"),
        },
        "agents": agents,
        "links": link_rows,
    }


class ProjectCreateRequest(BaseModel):
    id: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=2, max_length=60)
    description: str = Field(default="", max_length=300)
    group: str = Field(default="discover", max_length=30)
    icon: str = Field(default="chart", max_length=30)
    accent: str = Field(default="blue", max_length=20)


class ProjectPreferencesRequest(BaseModel):
    order: list[str] = Field(default_factory=list, max_length=100)
    favorite_ids: list[str] = Field(default_factory=list, max_length=100)
    groups: dict[str, str] = Field(default_factory=dict, max_length=100)
    hidden_ids: list[str] = Field(default_factory=list, max_length=100)



@app.get("/api/projects")
def projects() -> dict[str, Any]:
    return {
        "projects": public_projects(),
        # Keep the first paint on one project payload instead of issuing two
        # extra requests just for the small counters in the header.
        "summary": {
            "inbox_count": len(list_inbox("inbox")),
            "note_count": len(_app_call("knowledge_files", )),
        },
    }


@app.post("/api/projects")
def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    """新增一个项目入口（写入 projects.json，前端主页立即可见）。"""
    # 写配置时必须读取未经个人偏好过滤的原始列表。若使用 load_projects()，
    # 用户在首页隐藏的项目会在新增项目时被当成“不存在”并从 projects.json 永久删掉。
    existing = _load_configured_projects()
    if any(str(item.get("id")) == request.id for item in existing):
        raise HTTPException(409, f"项目入口 {request.id} 已存在")
    entry = {
        "id": request.id,
        "title": request.title.strip(),
        "description": request.description.strip() or "自定义项目入口。",
        "meta": "自定义入口",
        "href": f"/projects/{request.id}",
        "accent": request.accent,
        "icon": request.icon,
        "group": request.group,
        "status": "custom",
    }
    existing.append(entry)
    PROJECTS_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "project": entry, "projects": public_projects()}




@app.get("/api/projects/preferences")
async def get_project_preferences() -> dict[str, Any]:
    preferences = load_project_preferences()
    return {
        "order": [str(item) for item in preferences.get("order", []) if item],
        "favorite_ids": [str(item) for item in preferences.get("favorite_ids", []) if item],
        "groups": preferences.get("groups", {}) if isinstance(preferences.get("groups"), dict) else {},
        "hidden_ids": [str(item) for item in preferences.get("hidden_ids", []) if item],
    }


@app.post("/api/projects/preferences")
def update_project_preferences(request: ProjectPreferencesRequest) -> dict[str, Any]:
    try:
        configured = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        configured = []
    project_ids = [str(item.get("id")) for item in configured if isinstance(item, dict) and item.get("id")]
    known = set(project_ids)
    order = list(dict.fromkeys(str(item).strip() for item in request.order if str(item).strip() in known))
    order.extend(project_id for project_id in project_ids if project_id not in order)
    favorites = list(dict.fromkeys(str(item).strip() for item in request.favorite_ids if str(item).strip() in known))
    hidden_ids = list(dict.fromkeys(str(item).strip() for item in request.hidden_ids if str(item).strip() in known))
    allowed_groups = {"organize", "produce", "discover", "monitor"}
    groups = {
        str(project_id): str(group).strip()
        for project_id, group in request.groups.items()
        if str(project_id).strip() in known and str(group).strip() in allowed_groups
    }
    values = {"order": order, "favorite_ids": favorites, "groups": groups, "hidden_ids": hidden_ids, "updated_at": now_iso()}
    save_project_preferences(values)
    return {"ok": True, "preferences": values, "projects": public_projects()}




@app.post("/api/projects/preferences/reset")
def reset_project_preferences() -> dict[str, Any]:
    save_project_preferences({"order": [], "favorite_ids": [], "groups": {}, "hidden_ids": [], "updated_at": now_iso()})
    return {"ok": True, "projects": public_projects(), "preferences": load_project_preferences()}






__all__ = [
    "load_project_preferences",
    "save_project_preferences",
    "load_projects",
    "project_activity_batch",
    "project_activity",
    "public_projects",
    "_public_projects_uncached",
    "PROJECT_LINKS",
    "project_link_summary",
    "agent_display_name",
    "agent_status_label",
    "project_href",
    "public_project_link",
    "AUDIT_STATUS_LABELS",
    "_audit_datetime",
    "audit_freshness",
    "project_data_freshness",
    "project_link_audit",
    "project_audit",
    "ProjectCreateRequest",
    "ProjectPreferencesRequest",
    "projects",
    "create_project",
    "get_project_preferences",
    "update_project_preferences",
    "reset_project_preferences",
]
