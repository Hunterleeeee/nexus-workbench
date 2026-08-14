"""Workbench Agent 数据层：运行记录/会话/动作（run/session/action）。

从 app.py 拆出的 agent 记录层（为开源准备）。模块内互调全部走 _app_call 运行时
转发（测试 patch app.X 生效）；agent_display_name/_audit_datetime 从 projects 直连；
decode_json_column 从 core 直连；USAGE_EXCLUDED_RUN_KINDS 从 usage 直连。
引擎（stream/run_project_agent）与 agent 路由随后续批次并入。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .agent_platform import AGENT_RUN_STATUS_LABELS
from .core import clip, decode_json_column, log, now_iso
from .db import db_connection
from .notifications import create_notification_record
from .projects import _audit_datetime, agent_display_name
from .usage import USAGE_EXCLUDED_RUN_KINDS


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用模块函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def agent_action_row(row: sqlite3.Row) -> dict[str, Any]:
    action = {key: row[key] for key in row.keys()}
    action["agent_name"] = agent_display_name(action.get("project_id", "workbench"))
    action["requires_confirmation"] = bool(action.get("requires_confirmation"))
    action["arguments"] = decode_json_column(action.pop("arguments_json", "{}"))
    action["result"] = decode_json_column(action.pop("result_json", "{}"))
    return action


def agent_run_row(row: sqlite3.Row) -> dict[str, Any]:
    run = {key: row[key] for key in row.keys()}
    run["request"] = decode_json_column(run.pop("request_json", "{}"))
    run["result"] = decode_json_column(run.pop("result_json", "{}"))
    run["status_label"] = AGENT_RUN_STATUS_LABELS.get(run.get("status"), run.get("status", "未知"))
    run["agent_name"] = agent_display_name(run.get("project_id", "workbench"))
    run["attempt"] = int(run.get("attempt") or 1)
    run["max_attempts"] = int(run.get("max_attempts") or 1)
    run["retryable"] = run.get("status") == "failed" and run["attempt"] < run["max_attempts"]
    return run


def create_agent_run_record(
    *,
    project_id: str,
    kind: str,
    title: str,
    request: dict[str, Any] | None = None,
    session_id: str = "",
    parent_run_id: str = "",
    max_attempts: int = 2,
    attempt: int = 1,
    status: str = "queued",
) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:16]
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            """INSERT INTO agent_runs
            (id, project_id, session_id, parent_run_id, kind, status, attempt, max_attempts, title,
             request_json, result_json, error, created_at, started_at, finished_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '', ?, '', '', ?)""",
            (
                run_id,
                project_id,
                session_id,
                parent_run_id,
                kind,
                status,
                max(1, int(attempt)),
                max(1, int(max_attempts)),
                clip(title.strip() or f"{kind} 运行", 240),
                json.dumps(request or {}, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        run = _app_call("agent_run_row", row)
    finally:
        connection.close()
    _app_call("add_agent_run_event", run_id, "queued", "运行已登记，等待 Agent 执行。")
    return _app_call("get_agent_run", run_id) or run


def get_agent_run(run_id: str) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        return _app_call("agent_run_row", row) if row else None
    finally:
        connection.close()


def list_agent_runs(project_id: str, *, session_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        if session_id:
            rows = connection.execute(
                "SELECT * FROM agent_runs WHERE project_id = ? AND session_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, session_id, max(1, min(limit, 100))),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM agent_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, max(1, min(limit, 100))),
            ).fetchall()
        return [_app_call("agent_run_row", row) for row in rows]
    finally:
        connection.close()


def update_agent_run_record(
    run_id: str,
    *,
    status: str | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    attempt: int | None = None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    timestamp = now_iso()
    updates: list[tuple[str, Any]] = [("updated_at", timestamp)]
    if status is not None:
        updates.append(("status", status))
        if status == "running":
            updates.append(("started_at", timestamp))
        if status in {"succeeded", "partial", "failed", "cancelled"}:
            updates.append(("finished_at", timestamp))
    if result is not None:
        updates.append(("result_json", json.dumps(result, ensure_ascii=False)))
    if error is not None:
        updates.append(("error", clip(error, 2_000)))
    if attempt is not None:
        updates.append(("attempt", max(1, int(attempt))))
    if request is not None:
        updates.append(("request_json", json.dumps(request, ensure_ascii=False)))
    connection = db_connection()
    try:
        assignments = ", ".join(f"{key} = ?" for key, _ in updates)
        cursor = connection.execute(
            f"UPDATE agent_runs SET {assignments} WHERE id = ?",
            [value for _, value in updates] + [run_id],
        )
        connection.commit()
        if cursor.rowcount == 0:
            return None
        row = connection.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        return _app_call("agent_run_row", row) if row else None
    finally:
        connection.close()


def add_agent_run_event(
    run_id: str,
    event_type: str,
    message: str = "",
    *,
    level: str = "info",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            """INSERT INTO agent_run_events
            (run_id, event_type, level, message, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, event_type, level, clip(message, 2_000), json.dumps(metadata or {}, ensure_ascii=False), timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM agent_run_events WHERE id = ?", (cursor.lastrowid,)).fetchone()
        event = {key: row[key] for key in row.keys()}
        event["metadata"] = decode_json_column(event.pop("metadata_json", "{}"))
        return event
    finally:
        connection.close()


def list_agent_run_events(run_id: str, limit: int = 100) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM agent_run_events WHERE run_id = ? ORDER BY id ASC LIMIT ?",
            (run_id, max(1, min(limit, 200))),
        ).fetchall()
        events = []
        for row in rows:
            event = {key: row[key] for key in row.keys()}
            event["metadata"] = decode_json_column(event.pop("metadata_json", "{}"))
            events.append(event)
        return events
    finally:
        connection.close()


def agent_run_timeline(run_id: str) -> dict[str, Any] | None:
    """Return one replayable, privacy-safe view of a Run and its side effects."""
    run = _app_call("get_agent_run", run_id)
    if not run:
        return None
    connection = db_connection()
    try:
        action_rows = connection.execute(
            "SELECT * FROM agent_actions WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
        ).fetchall()
        relation_rows = connection.execute(
            "SELECT * FROM relations WHERE from_id = ? OR to_id = ? ORDER BY created_at ASC",
            (run_id, run_id),
        ).fetchall()
    finally:
        connection.close()
    return {
        "run": run,
        "events": _app_call("list_agent_run_events", run_id, limit=200),
        "actions": [_app_call("agent_action_row", row) for row in action_rows],
        "relations": [relation_row(row) for row in relation_rows],
        "result_contract": (run.get("result") or {}).get("result_contract", {}) if isinstance(run.get("result"), dict) else {},
    }


def agent_run_summary(project_id: str, batch: dict[str, Any] | None = None) -> dict[str, Any]:
    if batch is not None:
        counts = dict(batch.get("runs", {}).get(project_id, {}))
        return {
            "total": sum(counts.values()),
            "counts": counts,
            "active": sum(counts.get(status, 0) for status in ("queued", "running")),
            "failed": counts.get("failed", 0),
            "latest": batch.get("latest", {}).get(project_id),
        }
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM agent_runs WHERE project_id = ? AND kind NOT IN (?, ?, ?, ?) GROUP BY status",
            (project_id, *USAGE_EXCLUDED_RUN_KINDS),
        ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        latest_row = connection.execute(
            "SELECT * FROM agent_runs WHERE project_id = ? AND kind NOT IN (?, ?, ?, ?) ORDER BY created_at DESC LIMIT 1",
            (project_id, *USAGE_EXCLUDED_RUN_KINDS),
        ).fetchone()
        return {
            "total": sum(counts.values()),
            "counts": counts,
            "active": sum(counts.get(status, 0) for status in ("queued", "running")),
            "failed": counts.get("failed", 0),
            "latest": _app_call("agent_run_row", latest_row) if latest_row else None,
        }
    finally:
        connection.close()


def agent_quality_metrics(project_id: str, hours: int = 24) -> dict[str, Any]:
    """Return comparable quality signals for every project Agent."""
    hours = max(1, min(int(hours or 24), 24 * 90))
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM agent_runs WHERE project_id = ? AND julianday(created_at) >= julianday('now', ?) ORDER BY created_at DESC",
            (project_id, f"-{hours} hours"),
        ).fetchall()
        actions = connection.execute(
            "SELECT status, requires_confirmation, run_id FROM agent_actions WHERE project_id = ? AND julianday(created_at) >= julianday('now', ?)",
            (project_id, f"-{hours} hours"),
        ).fetchall()
        historical_counts = connection.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status IN ('succeeded', 'completed') THEN 1 ELSE 0 END) AS succeeded,
                      SUM(CASE WHEN status IN ('failed', 'cancelled') THEN 1 ELSE 0 END) AS failed
                 FROM agent_runs WHERE project_id = ? AND kind NOT IN (?, ?, ?, ?)""",
            (project_id, *USAGE_EXCLUDED_RUN_KINDS),
        ).fetchone()
        historical_latest = connection.execute(
            "SELECT status, created_at, updated_at, error FROM agent_runs WHERE project_id = ? AND kind NOT IN (?, ?, ?, ?) ORDER BY created_at DESC LIMIT 1",
            (project_id, *USAGE_EXCLUDED_RUN_KINDS),
        ).fetchone()
        historical_error = connection.execute(
            """SELECT error, updated_at FROM agent_runs
               WHERE project_id = ? AND error != '' ORDER BY updated_at DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
    finally:
        connection.close()
    runs = [_app_call("agent_run_row", row) for row in rows]
    total = len(runs)
    succeeded = sum(1 for run in runs if run.get("status") in {"succeeded", "completed"})
    failed = sum(1 for run in runs if run.get("status") in {"failed", "cancelled"})
    partial = sum(1 for run in runs if run.get("status") == "partial")
    retried = sum(1 for run in runs if int(run.get("attempt") or 1) > 1 or run.get("parent_run_id"))
    manual = sum(1 for item in actions if bool(item["requires_confirmation"]) or str(item["status"] or "") in {"confirmed", "executed"})
    source_complete = 0
    data_time_complete = 0
    plan_complete = 0
    review_needed = 0
    latest_data_as_of = ""
    for run in runs:
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        contract = result.get("result_contract") if isinstance(result.get("result_contract"), dict) else {}
        source_refs = contract.get("source_refs") if isinstance(contract.get("source_refs"), list) else []
        citations = contract.get("citations") if isinstance(contract.get("citations"), list) else []
        if source_refs or citations:
            source_complete += 1
        if isinstance(contract.get("execution_plan"), dict) and contract.get("execution_plan", {}).get("kind"):
            plan_complete += 1
        if contract.get("needs_review"):
            review_needed += 1
        data_as_of = str(contract.get("data_as_of") or "")
        if data_as_of:
            data_time_complete += 1
            latest_data_as_of = max(latest_data_as_of, data_as_of)
    latest = runs[0] if runs else None
    age_seconds = None
    if latest_data_as_of:
        latest_dt = _audit_datetime(latest_data_as_of)
        if latest_dt:
            age_seconds = max(0, int((datetime.now(timezone.utc) - latest_dt).total_seconds()))
    historical_total = int((historical_counts["total"] if historical_counts else 0) or 0)
    historical_succeeded = int((historical_counts["succeeded"] if historical_counts else 0) or 0)
    historical_failed = int((historical_counts["failed"] if historical_counts else 0) or 0)
    if not total:
        # Do not tell the user an Agent is merely configured when the only
        # reason it has no 24h score is that its last run is older.  This was
        # especially confusing on the platform page: the same card showed
        # "尚无可评价运行" and a non-zero historical failure count beside it.
        if historical_failed:
            state, state_label = "historical_failed", "近期无运行 · 历史有失败"
        elif historical_total:
            state, state_label = "historical", "近期无运行 · 有历史记录"
        else:
            state, state_label = "configured", "仅配置未运行"
    elif latest and latest.get("status") in {"failed", "cancelled"}:
        state, state_label = "needs_repair", "失败待修复"
    elif latest_data_as_of and age_seconds is not None and age_seconds > 24 * 3600:
        state, state_label = "stale", "数据过期"
    elif succeeded:
        state, state_label = "verified", "已验证"
    else:
        state, state_label = "observed", "已有运行记录"
    return {
        "project_id": project_id,
        "window_hours": hours,
        "state": state,
        "state_label": state_label,
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "partial": partial,
        "success_rate": round(succeeded / max(1, total), 4),
        "failure_rate": round(failed / max(1, total), 4),
        "retry_rate": round(retried / max(1, total), 4),
        "manual_takeover_rate": round(manual / max(1, total), 4),
        "source_completeness_rate": round(source_complete / max(1, total), 4),
        "data_time_completeness_rate": round(data_time_complete / max(1, total), 4),
        "plan_completeness_rate": round(plan_complete / max(1, total), 4),
        "review_rate": round(review_needed / max(1, total), 4),
        "action_count": len(actions),
        "action_failure_rate": round(sum(1 for item in actions if str(item["status"] or "") == "failed") / max(1, len(actions)), 4),
        "confirmation_count": sum(1 for item in actions if bool(item["requires_confirmation"])),
        "latest_status": latest.get("status") if latest else "",
        "latest_run_id": latest.get("id") if latest else "",
        "latest_data_as_of": latest_data_as_of,
        "latest_data_age_seconds": age_seconds,
        "historical_total": historical_total,
        "historical_succeeded": historical_succeeded,
        "historical_failed": historical_failed,
        "last_run_at": str((historical_latest["updated_at"] or historical_latest["created_at"]) if historical_latest else ""),
        "last_run_status": str(historical_latest["status"] or "") if historical_latest else "",
        "last_error": clip(str(historical_error["error"] or ""), 300) if historical_error else "",
        "last_error_at": str(historical_error["updated_at"] or "") if historical_error else "",
    }


def agent_session_row(row: sqlite3.Row) -> dict[str, Any]:
    session = {key: row[key] for key in row.keys()}
    session["summary"] = decode_json_column(session.pop("summary_json", "{}"))
    session["agent_name"] = agent_display_name(session.get("project_id", "workbench"))
    return session


def agent_message_row(row: sqlite3.Row) -> dict[str, Any]:
    message = {key: row[key] for key in row.keys()}
    message["metadata"] = decode_json_column(message.pop("metadata_json", "{}"))
    return message


def create_agent_session(project_id: str, title: str = "未命名 Agent 会话") -> dict[str, Any]:
    session_id = uuid.uuid4().hex[:12]
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            "INSERT INTO agent_sessions (id, project_id, title, status, summary_json, created_at, updated_at) VALUES (?, ?, ?, 'active', '{}', ?, ?)",
            (session_id, project_id, clip(title.strip() or "未命名 Agent 会话", 120), timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        return _app_call("agent_session_row", row)
    finally:
        connection.close()


def get_agent_session(session_id: str, project_id: str = "") -> dict[str, Any] | None:
    connection = db_connection()
    try:
        if project_id:
            row = connection.execute("SELECT * FROM agent_sessions WHERE id = ? AND project_id = ?", (session_id, project_id)).fetchone()
        else:
            row = connection.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        return _app_call("agent_session_row", row) if row else None
    finally:
        connection.close()


def list_agent_sessions(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM agent_sessions WHERE project_id = ? ORDER BY updated_at DESC LIMIT ?",
            (project_id, max(1, min(limit, 100))),
        ).fetchall()
        return [_app_call("agent_session_row", row) for row in rows]
    finally:
        connection.close()


def list_agent_messages(session_id: str, limit: int = 40) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT id, session_id, role, content, metadata_json, created_at FROM agent_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, max(1, min(limit, 100))),
        ).fetchall()
        return [_app_call("agent_message_row", row) for row in reversed(rows)]
    finally:
        connection.close()


def add_agent_message(session_id: str, role: str, content: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO agent_messages (session_id, role, content, metadata_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, clip(content, 20_000), json.dumps(metadata or {}, ensure_ascii=False), timestamp),
        )
        connection.execute("UPDATE agent_sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id))
        connection.commit()
        row = connection.execute("SELECT id, session_id, role, content, metadata_json, created_at FROM agent_messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _app_call("agent_message_row", row)
    finally:
        connection.close()


def update_agent_session_summary(session_id: str, summary: dict[str, Any], title: str = "") -> dict[str, Any] | None:
    connection = db_connection()
    try:
        if title.strip():
            connection.execute(
                "UPDATE agent_sessions SET title = ?, summary_json = ?, updated_at = ? WHERE id = ?",
                (clip(title.strip(), 120), json.dumps(summary, ensure_ascii=False), now_iso(), session_id),
            )
        else:
            connection.execute(
                "UPDATE agent_sessions SET summary_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(summary, ensure_ascii=False), now_iso(), session_id),
            )
        connection.commit()
        row = connection.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        return _app_call("agent_session_row", row) if row else None
    finally:
        connection.close()



def create_agent_action_record(
    *,
    project_id: str,
    name: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    risk: str = "low",
    requires_confirmation: bool = False,
    run_id: str = "",
) -> dict[str, Any]:
    timestamp = now_iso()
    # 幂等键：同一个 run + 同一个工具 + 同一份参数，只该有一条动作记录。
    #
    # 原来每次都是新的 uuid，于是重试时「已执行就不再执行」那道保护完全落空——
    # 重跑一遍动作推断就生成一批全新的 id，收件箱多一条、知识库多一个文件、
    # 通知再推一遍。带上这个键之后，重试命中的是同一条记录，直接返回既有结果。
    fingerprint = hashlib.sha1(
        json.dumps({"run": run_id, "project": project_id, "tool": tool, "arguments": arguments or {}},
                   sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:12]
    action_id = fingerprint if run_id else uuid.uuid4().hex[:12]
    connection = db_connection()
    try:
        if run_id:
            existing = connection.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
            if existing:
                return _app_call("agent_action_row", existing)
        connection.execute(
            """INSERT INTO agent_actions
            (id, project_id, name, tool, status, risk, requires_confirmation, arguments_json, result_json, run_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, '{}', ?, ?, ?)""",
            (
                action_id,
                project_id,
                name,
                tool,
                risk,
                int(requires_confirmation),
                json.dumps(arguments or {}, ensure_ascii=False),
                run_id,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        action = _app_call("agent_action_row", row)
    finally:
        connection.close()
    if requires_confirmation:
        try:
            create_notification_record(
                title=f"Agent 待确认：{name}",
                body=f"{agent_display_name(project_id)} 请求执行 {tool}",
                project_id=project_id,
                kind="agent_action",
                level="warning",
                href=f"/projects/{project_id}" if project_id != "workbench" else "/",
                event_key=f"agent-action:{action_id}",
                dedupe_seconds=0,
            )
        except Exception:
            log.debug("忽略异常（create_agent_action_record）", exc_info=True)
    return action


def get_agent_action_record(action_id: str) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        return _app_call("agent_action_row", row) if row else None
    finally:
        connection.close()


def update_agent_action_record(
    action_id: str,
    *,
    status: str | None = None,
    result: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    updates: list[tuple[str, Any]] = [("updated_at", now_iso())]
    if status is not None:
        updates.append(("status", status))
    if result is not None:
        updates.append(("result_json", json.dumps(result, ensure_ascii=False)))
    if run_id is not None:
        updates.append(("run_id", run_id))
    connection = db_connection()
    try:
        assignments = ", ".join(f"{key} = ?" for key, _ in updates)
        cursor = connection.execute(
            f"UPDATE agent_actions SET {assignments} WHERE id = ?",
            [value for _, value in updates] + [action_id],
        )
        connection.commit()
        if cursor.rowcount == 0:
            return None
        row = connection.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        return _app_call("agent_action_row", row)
    finally:
        connection.close()


__all__ = ["agent_action_row", "agent_run_row", "create_agent_run_record", "get_agent_run", "list_agent_runs", "update_agent_run_record", "add_agent_run_event", "list_agent_run_events", "agent_run_timeline", "agent_run_summary", "agent_quality_metrics", "agent_session_row", "agent_message_row", "create_agent_session", "get_agent_session", "list_agent_sessions", "list_agent_messages", "add_agent_message", "update_agent_session_summary", "create_agent_action_record", "get_agent_action_record", "update_agent_action_record"]
