"""Workbench 使用统计领域：运行次数/趋势/一句话概况。

从 app.py 拆出的领域模块（为开源准备）。依赖 core（datetime 工具/日志）与 db；
load_projects 仍留 app.py（projects 领域），这里用延迟转发。口径常量
（USAGE_WINDOW_CHOICES / USAGE_EXCLUDED_RUN_KINDS）随领域走。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.responses import FileResponse

from .core import STATIC_DIR, log, now_iso
from .db import db_connection
from .instance import app


def load_projects() -> list[dict[str, Any]]:
    """延迟转发 app.load_projects（projects 领域仍在 app.py）。"""
    import app as _app

    return _app.load_projects()


USAGE_WINDOW_CHOICES = (7, 30, 90)

# 不算「智能体运行」的内部记录：dispatch_child 是总调度派生的子调用（与父 run
# 双计）；evidence_acceptance 是联动验收基线；manual_takeover 是人工接管动作；
# approval_decision 是审批按钮。这些混进 runs 会让统计虚高。口径与
# /api/trace/recent 保持一致，只多排 approval_decision。
USAGE_EXCLUDED_RUN_KINDS = ("dispatch_child", "evidence_acceptance", "manual_takeover", "approval_decision")

def _usage_since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()


def _usage_rate(part: int, whole: int) -> float:
    return round((part / whole) * 100, 1) if whole else 0.0


def collect_usage_stats(days: int = 30) -> dict[str, Any]:
    """Aggregate real activity per project over the trailing window."""
    days = days if days in USAGE_WINDOW_CHOICES else 30
    since = _usage_since(days)
    projects = load_projects()
    known = {str(item.get("id")): item for item in projects if item.get("id")}

    per_project: dict[str, dict[str, Any]] = {
        pid: {
            "project_id": pid,
            "title": str(item.get("title") or pid),
            "href": str(item.get("href") or ""),
            "group": str(item.get("group") or ""),
            "favorite": bool(item.get("favorite")),
            "runs": 0,
            "runs_succeeded": 0,
            "runs_failed": 0,
            "work_items": 0,
            "work_items_done": 0,
            "artifacts": 0,
            "notifications": 0,
            "last_used_at": "",
        }
        for pid, item in known.items()
    }

    def bucket(pid: str) -> dict[str, Any] | None:
        pid = str(pid or "").strip()
        if not pid:
            return None
        if pid not in per_project:
            per_project[pid] = {
                "project_id": pid,
                "title": pid,
                "href": "",
                "group": "",
                "favorite": False,
                "runs": 0, "runs_succeeded": 0, "runs_failed": 0,
                "work_items": 0, "work_items_done": 0,
                "artifacts": 0, "notifications": 0, "last_used_at": "",
            }
        return per_project[pid]

    def touch(entry: dict[str, Any], stamp: Any) -> None:
        text = str(stamp or "")
        if text and text > str(entry.get("last_used_at") or ""):
            entry["last_used_at"] = text

    connection = db_connection()
    try:
        for row in connection.execute(
            "SELECT project_id, status, created_at FROM agent_runs WHERE created_at >= ? AND kind NOT IN (?, ?, ?, ?)",
            (since, *USAGE_EXCLUDED_RUN_KINDS),
        ):
            entry = bucket(row["project_id"])
            if entry is None:
                continue
            entry["runs"] += 1
            if row["status"] == "succeeded":
                entry["runs_succeeded"] += 1
            elif row["status"] in {"failed", "cancelled"}:
                entry["runs_failed"] += 1
            touch(entry, row["created_at"])

        work_totals = {"created": 0, "done": 0, "archived": 0, "failed": 0, "blocked": 0, "open": 0}
        for row in connection.execute(
            "SELECT source_project, target_project, status, created_at, updated_at FROM work_items WHERE created_at >= ?",
            (since,),
        ):
            work_totals["created"] += 1
            status = str(row["status"] or "")
            if status == "done":
                work_totals["done"] += 1
            elif status == "archived":
                work_totals["archived"] += 1
            elif status == "failed":
                work_totals["failed"] += 1
            elif status == "blocked":
                work_totals["blocked"] += 1
            else:
                work_totals["open"] += 1
            for key in ("source_project", "target_project"):
                entry = bucket(row[key])
                if entry is None:
                    continue
                entry["work_items"] += 1
                if status in {"done", "archived"}:
                    entry["work_items_done"] += 1
                touch(entry, row["updated_at"] or row["created_at"])

        for row in connection.execute(
            "SELECT project_id, created_at FROM artifacts WHERE created_at >= ?", (since,)
        ):
            entry = bucket(row["project_id"])
            if entry is None:
                continue
            entry["artifacts"] += 1
            touch(entry, row["created_at"])

        notif_total = notif_read = 0
        for row in connection.execute(
            "SELECT project_id, read_at, created_at FROM notifications WHERE created_at >= ?", (since,)
        ):
            notif_total += 1
            if row["read_at"]:
                notif_read += 1
            entry = bucket(row["project_id"])
            if entry is not None:
                entry["notifications"] += 1

        inbox_row = connection.execute(
            """SELECT COUNT(*) AS captured,
                      SUM(CASE WHEN status IN ('done', 'archived') THEN 1 ELSE 0 END) AS processed,
                      SUM(CASE WHEN status = 'ignored' THEN 1 ELSE 0 END) AS ignored,
                      SUM(CASE WHEN status = 'inbox' THEN 1 ELSE 0 END) AS backlog
               FROM inbox WHERE created_at >= ?""",
            (since,),
        ).fetchone()

        # 两处口径修正：
        # 1) status 写入的是 'succeeded'（见 record_llm_usage_event），这里原本查
        #    'ok'，于是"成功次数"恒为 0，页面上的 LLM 成功率永远是 0%。
        # 2) 排除 purpose='test'：那是"测试连接"按钮产生的探活调用，不是真实用量。
        #    llm_usage_metrics_payload 早就排除了，这里没排，导致两个页面对不上——
        #    实测本机 249 条事件里有 244 条是 test，调用次数被放大了约 50 倍。
        llm_row = connection.execute(
            """SELECT COUNT(*) AS calls,
                      SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS ok,
                      SUM(COALESCE(total_tokens, 0)) AS tokens,
                      SUM(COALESCE(cost_usd, 0)) AS cost,
                      AVG(COALESCE(latency_ms, 0)) AS latency
               FROM llm_usage_events
               WHERE created_at >= ? AND COALESCE(NULLIF(purpose, ''), 'agent') <> 'test'""",
            (since,),
        ).fetchone()

        llm_purposes = [
            {
                "purpose": str(row["purpose"] or "未标注"),
                "calls": int(row["calls"] or 0),
                "tokens": int(row["tokens"] or 0),
            }
            for row in connection.execute(
                """SELECT purpose, COUNT(*) AS calls, SUM(COALESCE(total_tokens, 0)) AS tokens
                   FROM llm_usage_events
                   WHERE created_at >= ? AND COALESCE(NULLIF(purpose, ''), 'agent') <> 'test'
                   GROUP BY purpose ORDER BY calls DESC LIMIT 12""",
                (since,),
            )
        ]

        automation_row = connection.execute(
            """SELECT COUNT(*) AS runs,
                      SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                      SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
               FROM automation_runs WHERE created_at >= ?""",
            (since,),
        ).fetchone()

        # 按「服务器本地时区」分组天数——created_at 存 UTC，直接 substr 会
        # 把本地 0:00-8:00 的活动归进前一天，趋势图每天错位 8 小时。
        daily_counts: dict[str, int] = {}
        for row in connection.execute(
            "SELECT created_at FROM agent_runs WHERE created_at >= ? AND kind NOT IN (?, ?, ?, ?)",
            (since, *USAGE_EXCLUDED_RUN_KINDS),
        ):
            try:
                day = datetime.fromisoformat(str(row["created_at"] or "")).astimezone().strftime("%Y-%m-%d")
            except ValueError:
                day = str(row["created_at"] or "")[:10]
            daily_counts[day] = daily_counts.get(day, 0) + 1
        daily = [{"date": day, "runs": daily_counts[day]} for day in sorted(daily_counts)]
    finally:
        connection.close()

    entries = sorted(
        per_project.values(),
        # runs 优先（用户感知的「在用」= 真的跑过），工作项/产物次之。
        # 之前按 activity 混合排序，后台产生大量工作项的项目会压过
        # 用户天天对话的项目，导致「真正在用的是」与直觉相反。
        key=lambda item: (item["runs"], item["work_items"] + item["artifacts"], item["last_used_at"]),
        reverse=True,
    )
    for entry in entries:
        entry["activity"] = entry["runs"] + entry["work_items"] + entry["artifacts"]
        entry["success_rate"] = _usage_rate(entry["runs_succeeded"], entry["runs"])
        if entry["activity"] == 0:
            entry["verdict"] = "idle"
            entry["verdict_label"] = "这段时间完全没用过"
        elif entry["activity"] < 3:
            entry["verdict"] = "rare"
            entry["verdict_label"] = "几乎没用"
        elif entry["runs"] and entry["success_rate"] < 60:
            entry["verdict"] = "unreliable"
            entry["verdict_label"] = "在用但经常失败"
        else:
            entry["verdict"] = "active"
            entry["verdict_label"] = "在正常使用"

    captured = int(inbox_row["captured"] or 0) if inbox_row else 0
    processed = int(inbox_row["processed"] or 0) if inbox_row else 0
    ignored = int(inbox_row["ignored"] or 0) if inbox_row else 0
    backlog = int(inbox_row["backlog"] or 0) if inbox_row else 0

    idle = [entry for entry in entries if entry["verdict"] == "idle"]
    rare = [entry for entry in entries if entry["verdict"] == "rare"]
    unreliable = [entry for entry in entries if entry["verdict"] == "unreliable"]
    top = [entry for entry in entries if entry["verdict"] == "active"][:3]

    highlights: list[str] = []
    if top:
        highlights.append("这 {} 天真正在用的是：{}。".format(days, "、".join(item["title"] for item in top)))
    if idle:
        highlights.append(
            "{} 个入口一次都没用过（{}），可以考虑下线或合并。".format(
                len(idle), "、".join(item["title"] for item in idle[:5])
            )
        )
    if rare:
        highlights.append("{} 个入口活动少于 3 次，属于「建了但没用起来」。".format(len(rare)))
    if unreliable:
        highlights.append(
            "{} 个入口在用但成功率低于 60%（{}），先修再谈新功能。".format(
                len(unreliable), "、".join(item["title"] for item in unreliable[:3])
            )
        )
    if captured:
        highlights.append(
            "收件箱进了 {} 条，处理 {} 条（{}%），还剩 {} 条堆着。".format(
                captured, processed, _usage_rate(processed, captured), backlog
            )
        )
    if not highlights:
        highlights.append("这段时间几乎没有活动记录，先用起来再回来看这一页。")

    return {
        "days": days,
        "since": since,
        "generated_at": now_iso(),
        "projects": entries,
        "highlights": highlights,
        "totals": {
            "runs": sum(item["runs"] for item in entries),
            "work_items": work_totals["created"],
            "artifacts": sum(item["artifacts"] for item in entries),
            "active_projects": sum(1 for item in entries if item["verdict"] == "active"),
            "idle_projects": len(idle),
            "total_projects": len(entries),
        },
        "work_items": {
            **work_totals,
            "completion_rate": _usage_rate(work_totals["done"], work_totals["created"]),
        },
        "inbox": {
            "captured": captured,
            "processed": processed,
            "ignored": ignored,
            "backlog": backlog,
            "processed_rate": _usage_rate(processed, captured),
        },
        "notifications": {
            "total": notif_total,
            "read": notif_read,
            "read_rate": _usage_rate(notif_read, notif_total),
        },
        "llm": {
            "calls": int(llm_row["calls"] or 0) if llm_row else 0,
            "ok": int(llm_row["ok"] or 0) if llm_row else 0,
            "tokens": int(llm_row["tokens"] or 0) if llm_row else 0,
            "cost_usd": round(float(llm_row["cost"] or 0), 4) if llm_row else 0.0,
            "avg_latency_ms": int(llm_row["latency"] or 0) if llm_row else 0,
            "by_purpose": llm_purposes,
        },
        "automation": {
            "runs": int(automation_row["runs"] or 0) if automation_row else 0,
            "succeeded": int(automation_row["succeeded"] or 0) if automation_row else 0,
            "failed": int(automation_row["failed"] or 0) if automation_row else 0,
        },
        "daily_runs": daily,
    }


@app.get("/usage")
async def usage_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "usage.html")


@app.get("/api/usage/stats")
def get_usage_stats(days: int = 30) -> dict[str, Any]:
    """Read-only usage report. Blocking SQLite work stays off the event loop."""
    return collect_usage_stats(days)

