"""Workbench 领域模块（app.py 拆分）。"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from .instance import app
from .core import now_iso


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


class ApprovalDecisionRequest(BaseModel):
    status: str = Field(pattern="^(approved|rejected|pending|changes_requested|resubmitted)$")
    reviewer_note: str = Field(default="", max_length=4_000)

def create_approval_request(project_id: str, kind: str, title: str, payload: dict[str, Any]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    timestamp = now_iso()
    connection = _app_call('db_connection', )
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
    connection = _app_call('db_connection', )
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
    connection = _app_call('db_connection', )
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
            item["payload"] = _app_call('platform_decode_json', item.pop("payload_json", "{}"), {})
            events = connection.execute("SELECT id, from_status, to_status, reviewer_note, run_id, created_at FROM approval_events WHERE approval_id = ? ORDER BY created_at ASC, id ASC", (item["id"],)).fetchall()
            item["history"] = [dict(event) for event in events]
            execution = connection.execute("SELECT * FROM server_action_executions WHERE approval_id = ? ORDER BY created_at DESC LIMIT 1", (item["id"],)).fetchone()
            item["execution"] = _app_call('_server_action_execution_payload', execution)
            items.append(item)
        return {"approvals": items}
    finally:
        connection.close()


@app.patch("/api/approvals/{approval_id}")
def decide_approval(approval_id: str, request: ApprovalDecisionRequest) -> dict[str, Any]:
    connection = _app_call('db_connection', )
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
        approval_run = _app_call('create_agent_run_record', 
            project_id=str(row["project_id"] or "workbench"),
            kind="approval_decision",
            title=f"审批记录：{row['title']}",
            request={"approval_id": approval_id, "from_status": previous_status, "to_status": request.status, "reviewer_note": request.reviewer_note},
            max_attempts=1,
        )
        _app_call('update_agent_run_record', approval_run["id"], status="running")
        connection.execute("UPDATE approval_requests SET status = ?, reviewer_note = ?, updated_at = ? WHERE id = ?", (request.status, request.reviewer_note, timestamp, approval_id))
        payload = _app_call('platform_decode_json', row["payload_json"], {})
        for artifact_id in payload.get("delivery_artifacts", []) if isinstance(payload, dict) else []:
            artifact_row = connection.execute("SELECT metadata_json FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
            if not artifact_row:
                continue
            metadata = _app_call('platform_decode_json', artifact_row["metadata_json"], {})
            metadata.update({"approval_status": request.status, "reviewer_note": request.reviewer_note, "approval_id": approval_id, "approved_at": timestamp if request.status == "approved" else metadata.get("approved_at", "")})
            connection.execute("UPDATE artifacts SET metadata_json = ? WHERE id = ?", (json.dumps(metadata, ensure_ascii=False), artifact_id))
        connection.execute("INSERT INTO approval_events(approval_id, from_status, to_status, reviewer_note, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", (approval_id, previous_status, request.status, request.reviewer_note, approval_run["id"], timestamp))
        connection.commit()
        _app_call('update_agent_run_record', approval_run["id"], status="succeeded", result={"approval_id": approval_id, "from_status": previous_status, "to_status": request.status}, error="")
        _app_call('create_relation_record', from_type="approval", from_id=approval_id, to_type="agent_run", to_id=approval_run["id"], relation_type="approval_event", metadata={"from_status": previous_status, "to_status": request.status})
        item = dict(row)
        item.update({"status": request.status, "reviewer_note": request.reviewer_note})
        item["payload"] = payload
        item["history"] = [dict(event) for event in connection.execute("SELECT id, from_status, to_status, reviewer_note, run_id, created_at FROM approval_events WHERE approval_id = ? ORDER BY created_at ASC, id ASC", (approval_id,)).fetchall()]
        return {"approval": item, "run": _app_call('get_agent_run', approval_run["id"]), "policy": "批准只更新本地 Artifact 审批状态；服务器写入、删除、交易和外发动作不会因批准自动执行。"}
    finally:
        connection.close()


__all__ = [
    "ApprovalDecisionRequest",
    "create_approval_request",
    "decide_approval",
    "get_approval_queue",
    "get_approvals",
]
