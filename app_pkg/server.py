"""Workbench 服务器监控领域：快照采集、健康评估、阈值管理。

从 app.py 拆出的领域模块（为开源准备）。依赖 core（快照/阈值文件、工具）与
db；路由与 React 工具仍留 app.py。
"""

from __future__ import annotations

import uuid
import asyncio
import json
import os
import re
import socket
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from .core import (
    decode_json_column,    SERVER_MONITOR_SNAPSHOT_FILE,
    SERVER_MONITOR_THRESHOLDS_FILE,
    clip,
    load_json_file,
    log,
    now_iso,
    save_json_atomic,
)
from .db import db_connection
from .instance import app


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


class ServerMonitorRequest(BaseModel):
    refresh: bool = True



class ServerThresholdsRequest(BaseModel):
    thresholds: dict[str, float] = Field(default_factory=dict, max_length=10)




class ServerActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=60)
    reason: str = Field(default="", max_length=2_000)
    confirmed: bool = False

SERVER_SAFE_ACTIONS = {"refresh": {"label": "重新执行只读检查", "risk": "low"}, "inspect_logs": {"label": "记录服务日志检查请求", "risk": "low"}, "restart": {"label": "申请重启 Workbench 服务", "risk": "high"}}


def _server_action_execution_payload(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    item["result"] = _app_call('platform_decode_json', item.pop("result_json", "{}"), {})
    item["rollback"] = _app_call('platform_decode_json', item.pop("rollback_json", "{}"), {})
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
    payload = _app_call('platform_decode_json', row["payload_json"], {})
    action = str(payload.get("action") or "").strip()
    definition = SERVER_SAFE_ACTIONS.get(action)
    if not definition:
        raise HTTPException(400, "审批中的服务器动作已不在安全白名单")
    existing = latest_server_action_execution(approval_id)
    if existing and existing.get("status") in {"succeeded", "manual_required"} and not existing.get("rolled_back_at"):
        return {"ok": existing.get("status") == "succeeded", "execution": existing, "message": "这条审批已经执行过，避免重复执行。"}
    execution = create_server_action_execution(approval_id, action)
    run = _app_call('create_agent_run_record', 
        project_id="server",
        kind="server_action_execution",
        title=f"执行服务器动作：{definition['label']}",
        request={"approval_id": approval_id, "action": action, "risk": definition["risk"]},
        max_attempts=1,
    )
    _app_call('update_agent_run_record', run["id"], status="running")
    _app_call('add_agent_run_event', run["id"], "execution_started", f"已按审批执行：{definition['label']}。", metadata={"approval_id": approval_id, "execution_id": execution["id"]})
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
            _app_call('update_agent_run_record', run["id"], status="succeeded", result={"approval_id": approval_id, "execution_id": execution["id"], "action": action})
            _app_call('add_agent_run_event', run["id"], "execution_succeeded", "只读服务器检查已完成，可按需回退本地快照。", level="success", metadata={"execution_id": execution["id"]})
            return {"ok": True, "execution": finished, "run": _app_call('get_agent_run', run["id"]), "message": "只读服务器检查已完成。"}
        message = "日志读取需要服务器侧人工查看；本次已记录执行边界。" if action == "inspect_logs" else "重启属于高风险动作，仍需服务器侧人工执行；本次仅记录审批和执行边界。"
        finished = finish_server_action_execution(execution["id"], status="manual_required", result={"message": message, "execution_policy": "不通过 Workbench 自动运行 shell 或重启命令。"})
        _app_call('update_agent_run_record', run["id"], status="succeeded", result={"approval_id": approval_id, "execution_id": execution["id"], "action": action, "manual_required": True})
        _app_call('add_agent_run_event', run["id"], "manual_required", message, level="warning", metadata={"execution_id": execution["id"]})
        return {"ok": False, "execution": finished, "run": _app_call('get_agent_run', run["id"]), "message": message}
    except Exception as exc:
        error = clip(str(exc), 800)
        finish_server_action_execution(execution["id"], status="failed", error=error)
        _app_call('update_agent_run_record', run["id"], status="failed", error=error)
        _app_call('add_agent_run_event', run["id"], "execution_failed", error, level="error", metadata={"execution_id": execution["id"]})
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
    approval = _app_call('create_approval_request', "server", "server_action", f"服务器动作审批：{definition['label']}", {"action": action, "reason": request.reason.strip(), "risk": definition["risk"], "execution_policy": "仅允许白名单只读动作；restart 仍需服务器侧人工执行"})
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









def _sub2api_timestamp(*args: Any, **kwargs: Any) -> Any:
    """直接转发 app_pkg.sub2api._sub2api_timestamp（不经过 app 命名空间，避免
    循环覆盖导致递归）。"""
    from app_pkg.sub2api import _sub2api_timestamp as _real

    return _real(*args, **kwargs)


def create_notification_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_notification_record（仍在 app.py）。"""
    import app as _app

    return _app.create_notification_record(*args, **kwargs)


def create_relation_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_relation_record（仍在 app.py）。"""
    import app as _app

    return _app.create_relation_record(*args, **kwargs)


def create_work_item_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_work_item_record（仍在 app.py）。"""
    import app as _app

    return _app.create_work_item_record(*args, **kwargs)


def list_artifacts(project_id: str = "") -> list[dict[str, Any]]:
    """延迟转发 app.list_artifacts（仍在 app.py）。"""
    import app as _app

    return _app.list_artifacts(project_id)


def list_work_items(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """延迟转发 app.list_work_items（仍在 app.py）。"""
    import app as _app

    return _app.list_work_items(*args, **kwargs)


def update_work_item_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.update_work_item_record（仍在 app.py）。"""
    import app as _app

    return _app.update_work_item_record(*args, **kwargs)



def load_server_monitor_snapshot() -> dict[str, Any]:
    values = load_json_file(SERVER_MONITOR_SNAPSHOT_FILE, {})
    return values if isinstance(values, dict) else {}


def save_server_monitor_snapshot(values: dict[str, Any]) -> None:
    save_json_atomic(SERVER_MONITOR_SNAPSHOT_FILE, values, 0o600)


def server_monitor_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    snapshot = _app_call('decode_json_column', row["snapshot_json"])
    return {
        "id": row["id"],
        "checked_at": row["checked_at"],
        "status": row["status"],
        "snapshot": snapshot,
        "created_at": row["created_at"],
    }


def record_server_monitor_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    checked_at = str(snapshot.get("checked_at") or "").strip()
    if not checked_at:
        return None
    connection = db_connection()
    try:
        existing = connection.execute(
            "SELECT * FROM server_monitor_snapshots WHERE checked_at = ? ORDER BY id DESC LIMIT 1",
            (checked_at,),
        ).fetchone()
        if existing:
            return _app_call('server_monitor_snapshot_row', existing)
        cursor = connection.execute(
            """INSERT INTO server_monitor_snapshots (checked_at, status, snapshot_json, created_at)
            VALUES (?, ?, ?, ?)""",
            (checked_at, str(snapshot.get("status") or "unknown"), json.dumps(snapshot, ensure_ascii=False), now_iso()),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM server_monitor_snapshots WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _app_call('server_monitor_snapshot_row', row) if row else None
    finally:
        connection.close()


def list_server_monitor_history(limit: int = 30) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM server_monitor_snapshots ORDER BY checked_at DESC, id DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
        return [_app_call('server_monitor_snapshot_row', row) for row in rows]
    finally:
        connection.close()


def server_number(value: Any) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def analyze_server_snapshot(
    snapshot: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Turn a read-only SSH snapshot into bounded health signals and transitions."""
    now = now or datetime.now(timezone.utc)
    history = history or []
    checked_at = str(snapshot.get("checked_at") or "").strip()
    checked_dt = _app_call('_sub2api_timestamp', checked_at)
    age_seconds = max(0, int((now - checked_dt).total_seconds())) if checked_dt else None
    if checked_dt is None:
        freshness = {"status": "unknown", "label": "没有检查时间"}
    elif age_seconds is not None and age_seconds <= 15 * 60:
        freshness = {"status": "fresh", "label": "数据新鲜"}
    elif age_seconds is not None and age_seconds <= 6 * 3600:
        freshness = {"status": "aging", "label": "数据较旧"}
    else:
        freshness = {"status": "stale", "label": "数据已过期"}
    freshness.update({"checked_at": checked_at, "age_seconds": age_seconds})

    disk = snapshot.get("disk") if isinstance(snapshot.get("disk"), dict) else {}
    memory = snapshot.get("memory") if isinstance(snapshot.get("memory"), dict) else {}
    disk_used_pct = _app_call('server_number', disk.get("used_pct"))
    memory_total_mb = _app_call('server_number', memory.get("total_mb"))
    memory_used_mb = _app_call('server_number', memory.get("used_mb"))
    memory_available_mb = _app_call('server_number', memory.get("available_mb"))
    memory_used_pct = round(memory_used_mb / memory_total_mb * 100, 1) if memory_total_mb and memory_used_mb is not None else None
    load_values = [_app_call('server_number', item) for item in str(snapshot.get("load") or "").split()]
    load_values = [item for item in load_values if item is not None]
    load_1m = load_values[0] if load_values else None

    services = [
        {"key": "nginx", "label": "Nginx", "value": str(snapshot.get("nginx") or "unknown"), "required": True},
        {"key": "app", "label": "App / PM2", "value": str(snapshot.get("app") or "unknown"), "required": bool(snapshot.get("app"))},
        {"key": "workbench", "label": "Workbench", "value": str(snapshot.get("workbench") or "unknown"), "required": False},
    ]
    alerts: list[dict[str, Any]] = []

    def add_alert(key: str, level: str, title: str, message: str) -> None:
        alerts.append({"key": key, "level": level, "title": title, "message": message})

    if snapshot.get("status") == "error" or snapshot.get("error"):
        add_alert("probe_failed", "error", "服务器检查失败", str(snapshot.get("error") or "SSH 只读检查失败。"))
    if freshness["status"] in {"stale", "unknown", "aging"}:
        add_alert("snapshot_stale", "warning", "服务器快照需要刷新", f"{freshness['label']}；上次检查：{checked_at or '未知'}。")
    thresholds = _app_call('load_server_monitor_thresholds', )
    disk_warn, disk_critical = thresholds["disk_warn"], thresholds["disk_critical"]
    memory_warn, memory_critical = thresholds["memory_warn"], thresholds["memory_critical"]
    load_warn, load_critical = thresholds["load_warn"], thresholds["load_critical"]
    if disk_used_pct is not None and disk_used_pct >= disk_critical:
        add_alert("disk_critical", "error", "服务器磁盘空间紧张", f"根分区已使用 {disk_used_pct:.0f}%，超过 {disk_critical:.0f}% 阈值。")
    elif disk_used_pct is not None and disk_used_pct >= disk_warn:
        add_alert("disk_high", "warning", "服务器磁盘使用率偏高", f"根分区已使用 {disk_used_pct:.0f}%，超过 {disk_warn:.0f}% 阈值。")
    if memory_used_pct is not None and memory_used_pct >= memory_critical:
        add_alert("memory_critical", "error", "服务器内存紧张", f"内存已使用 {memory_used_pct:.1f}%，超过 {memory_critical:.0f}% 阈值。")
    elif memory_used_pct is not None and memory_used_pct >= memory_warn:
        add_alert("memory_high", "warning", "服务器内存使用率偏高", f"内存已使用 {memory_used_pct:.1f}%，超过 {memory_warn:.0f}% 阈值。")
    if load_1m is not None and load_1m >= load_critical:
        add_alert("load_critical", "error", "服务器负载过高", f"1 分钟负载为 {load_1m:.2f}，超过 {load_critical:.2f} 阈值。")
    elif load_1m is not None and load_1m >= load_warn:
        add_alert("load_high", "warning", "服务器负载偏高", f"1 分钟负载为 {load_1m:.2f}，超过 {load_warn:.2f} 阈值。")
    for service in services:
        if service["required"] and service["value"] not in {"active", "running"}:
            add_alert(
                f"service_{service['key']}",
                "error",
                f"{service['label']} 不在运行",
                f"当前状态：{service['value']}。需要人工确认服务、部署或主机状态。",
            )

    previous_entry = next((item for item in history if isinstance(item, dict) and str(item.get("checked_at") or "") != checked_at), None)
    previous_snapshot = (previous_entry.get("snapshot") if previous_entry else None) or {}
    previous_alerts: list[dict[str, Any]] = []
    if previous_snapshot and str(previous_snapshot.get("checked_at") or "") != checked_at:
        previous_alerts = _app_call('analyze_server_snapshot', previous_snapshot, [], now).get("alerts", [])
    current_keys = {item["key"] for item in alerts}
    previous_keys = {item["key"] for item in previous_alerts}
    recovered = [item for item in previous_alerts if item["key"] not in current_keys]
    new_alerts = [item for item in alerts if item["key"] not in previous_keys]
    status = "error" if any(item["level"] == "error" for item in alerts) else "warning" if alerts else "ok"
    if not snapshot:
        status = "error"
    if not snapshot:
        summary = "还没有服务器检查快照。"
    elif status == "error":
        summary = f"发现 {len([item for item in alerts if item['level'] == 'error'])} 个需要人工处理的服务器问题。"
    elif status == "warning":
        summary = f"主机可读取，但有 {len(alerts)} 个监控项需要关注。"
    else:
        summary = "主机、关键服务和资源指标均在当前阈值内。"
    score = 100
    score -= min(30, len([item for item in alerts if item["level"] == "error"]) * 18)
    score -= min(20, len([item for item in alerts if item["level"] == "warning"]) * 8)
    if freshness["status"] in {"stale", "unknown"}:
        score -= 15
    elif freshness["status"] == "aging":
        score -= 7
    health_score = max(0, min(100, score))
    runbook = [
        {"key": "refresh", "label": "重新执行只读检查", "risk": "low", "available": True},
        {"key": "inspect_logs", "label": "检查服务日志与最近运行", "risk": "low", "available": True},
        {"key": "restart", "label": "重启服务", "risk": "high", "available": False, "reason": "需要人工审批；当前 Agent 不直接执行服务器变更"},
    ]
    # 容量趋势预测：用最近 5 条历史快照的磁盘/内存使用率做线性趋势，
    # 估算按当前增速距离告警阈值还有多少天（没有足够历史时给出 unknown）。
    def _trend_days(values: list[tuple[str, float | None]], warn: float, critical: float) -> dict[str, Any]:
        points = [(item[0], item[1]) for item in values if item[1] is not None]
        if len(points) < 2:
            return {"status": "unknown", "days_to_warn": None, "days_to_critical": None, "note": "历史样本不足，暂无法预测趋势"}
        try:
            ts = [datetime.fromisoformat(item[0].replace("Z", "+00:00")).timestamp() for item in points]
        except Exception:
            ts = [index for index in range(len(points))]
        latest, latest_at = points[-1][1], ts[-1]
        if latest is None:
            return {"status": "unknown", "days_to_warn": None, "days_to_critical": None, "note": "最近一次没有可用数据"}
        slope = 0.0
        denom = max(ts[-1] - ts[0], 1)
        for index in range(1, len(points)):
            dt = ts[index] - ts[index - 1]
            dv = (points[index][1] or latest) - (points[index - 1][1] or latest)
            if dt > 0:
                slope += dv / dt
        slope = slope / max(1, len(points) - 1) * 86400  # 每日变化（百分点/天）
        if slope <= 0.001:
            return {"status": "stable", "days_to_warn": None, "days_to_critical": None, "note": "使用率趋于平稳，无短期容量压力", "slope_per_day": round(slope, 2)}
        days_warn = (warn - latest) / slope if latest < warn else 0
        days_critical = (critical - latest) / slope if latest < critical else 0
        return {
            "status": "growing",
            "days_to_warn": round(max(0, days_warn), 1),
            "days_to_critical": round(max(0, days_critical), 1),
            "note": f"按当前增速，约 {max(0, days_warn):.0f} 天触及提醒阈值（{warn:.0f}%），{max(0, days_critical):.0f} 天触及临界阈值（{critical:.0f}%）",
            "slope_per_day": round(slope, 2),
        }

    history_pairs: list[tuple[str, float | None]] = []
    memory_pairs: list[tuple[str, float | None]] = []
    for entry in history[:5]:
        snap = entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else {}
        snap_disk = snap.get("disk") if isinstance(snap.get("disk"), dict) else {}
        snap_mem = snap.get("memory") if isinstance(snap.get("memory"), dict) else {}
        checked = str(entry.get("checked_at") or "")
        disk_val = _app_call('server_number', snap_disk.get("used_pct"))
        mem_total = _app_call('server_number', snap_mem.get("total_mb"))
        mem_used = _app_call('server_number', snap_mem.get("used_mb"))
        mem_val = round(mem_used / mem_total * 100, 1) if mem_total and mem_used is not None else None
        history_pairs.append((checked, disk_val))
        memory_pairs.append((checked, mem_val))
    prediction = {
        "disk": _trend_days(history_pairs, disk_warn, disk_critical),
        "memory": _trend_days(memory_pairs, memory_warn, memory_critical),
    }
    return {
        "status": status,
        "status_label": {"ok": "状态正常", "warning": "需要关注", "error": "存在异常"}[status],
        "summary": summary,
        "freshness": freshness,
        "metrics": {
            "disk_used_pct": disk_used_pct,
            "memory_used_pct": memory_used_pct,
            "memory_total_mb": memory_total_mb,
            "memory_used_mb": memory_used_mb,
            "memory_available_mb": memory_available_mb,
            "load_1m": load_1m,
        },
        "prediction": prediction,
        "services": services,
        "alerts": alerts,
        "new_alerts": new_alerts,
        "recovered": recovered,
        "history_count": len(history),
        "health_score": health_score,
        "health_score_label": "健康" if health_score >= 85 else "需要关注" if health_score >= 60 else "高风险",
        "runbook": runbook,
        "risk_note": "只读边界：SSH 探测与阈值提醒不会修改服务器。可配置项：磁盘 / 内存 / 负载的告警阈值；重启、部署、删除和配置变更必须人工确认。",
    }


def evaluate_server_monitor(snapshot: dict[str, Any] | None = None, create_records: bool = False) -> dict[str, Any]:
    snapshot = snapshot or _app_call('load_server_monitor_snapshot', )
    history = _app_call('list_server_monitor_history', limit=12)
    analysis = _app_call('analyze_server_snapshot', snapshot, history)
    created: list[dict[str, Any]] = []
    if not create_records:
        return {"analysis": analysis, "created": created, "history": history}
    active_items = _app_call('list_work_items', "all", "server")
    latest_artifact = next(iter(_app_call('list_artifacts', "server")), None)
    for alert in analysis["new_alerts"] or analysis["alerts"]:
        alert_key = f"server:{alert['key']}"
        existing = next((item for item in active_items if item.get("metadata", {}).get("alert_key") == alert_key and item.get("status") in {"open", "running", "blocked"}), None)
        if existing:
            created.append({"alert": alert, "work_item": existing, "created": False})
            continue
        item = _app_call('create_work_item_record', 
            title=alert["title"],
            description=f"{alert['message']} 数据时间：{analysis['freshness'].get('checked_at') or '未知'}。",
            kind="alert",
            priority="urgent" if alert["level"] == "error" else "high",
            source_project="server",
            target_project="inbox",
            metadata={"alert_key": alert_key, "notification_project": "server", "checked_at": analysis["freshness"].get("checked_at", ""), "source": "server_monitor_agent"},
        )
        relation = _app_call('create_relation_record', 
            from_type="artifact" if latest_artifact else "project",
            from_id=str(latest_artifact["id"] if latest_artifact else "server"),
            to_type="work_item",
            to_id=str(item["id"]),
            relation_type="alert_from_server_snapshot",
            metadata={"alert_key": alert_key, "project_id": "server"},
        )
        active_items.append(item)
        created.append({"alert": alert, "work_item": item, "relation": relation, "created": True})
    for recovery in analysis["recovered"]:
        alert_key = f"server:{recovery['key']}"
        existing = next((item for item in active_items if item.get("metadata", {}).get("alert_key") == alert_key and item.get("status") in {"open", "running", "blocked"}), None)
        if existing:
            _app_call('update_work_item_record', existing["id"], {"status": "done", "completed_at": now_iso(), "result_json": json.dumps({"recovered_at": snapshot.get("checked_at"), "message": "监控项已恢复"}, ensure_ascii=False)})
        _app_call('create_notification_record', 
            title=f"服务器监控已恢复：{recovery['title']}",
            body=f"{recovery['message']} 当前检查已恢复正常。",
            project_id="server",
            kind="alert",
            level="success",
            href="/projects/server",
            event_key=f"server-recovery:{recovery['key']}:{snapshot.get('checked_at', '')}",
            dedupe_seconds=0,
        )
    return {"analysis": analysis, "created": created, "history": history}


def server_monitor_config() -> dict[str, str]:
    server = os.getenv("WORKBENCH_SERVER", "").strip() or "root@your-server.example.com"
    key = os.path.expanduser(os.getenv("WORKBENCH_SERVER_SSH_KEY", "~/.ssh/workbench_deploy").strip())
    known_hosts = os.path.expanduser(os.getenv("WORKBENCH_SERVER_KNOWN_HOSTS", "~/.ssh/known_hosts").strip())
    return {"server": server, "ssh_key": key, "known_hosts": known_hosts}


def server_target_is_local(target: str) -> bool:
    """True when the monitored target is this machine itself.

    The server monitor used to always SSH back to the deployment host. When
    Workbench itself runs on the monitored server (the common deployment),
    that requires an SSH key the service user usually does not have. Detecting
    localhost / 127.0.0.1 / the local hostname / any local interface address
    lets us probe locally instead, keeping the monitor useful after deploy.
    """
    host = target.rsplit("@", 1)[-1].strip().lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if host == socket.gethostname().lower():
        return True
    try:
        local_addresses = {addr[4][0] for addr in socket.getaddrinfo(socket.gethostname(), None) if addr[4] and len(addr[4]) >= 1}
    except Exception:
        local_addresses = set()
    try:
        interface_ips = set()
        probe = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        if probe.returncode == 0:
            interface_ips.update(probe.stdout.split())
    except Exception:
        interface_ips = set()
    try:
        target_ip = socket.gethostbyname(host)
    except Exception:
        target_ip = ""
    return bool(target_ip) and (target_ip in local_addresses or target_ip in interface_ips)


SERVER_MONITOR_PROBE_COMMAND_TEMPLATE = r'''set -e
printf 'HOST|%s\n' "$(hostname)"
printf 'OS|%s\n' "$(uname -srm)"
printf 'UPTIME|%s\n' "$(uptime -p 2>/dev/null || uptime)"
printf 'LOAD|%s\n' "$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || printf '—')"
printf 'DISK|%s\n' "$(df -P / | awk 'NR==2 {print $2 "|" $3 "|" $5}')"
printf 'MEMORY|%s\n' "$(free -m 2>/dev/null | awk 'NR==2 {print $2 "|" $3 "|" $7}' || printf '—')"
printf 'NGINX|%s\n' "$(systemctl is-active nginx 2>/dev/null || printf 'unknown')"
printf 'WORKBENCH|%s\n' "$(systemctl is-active workbench 2>/dev/null || printf 'not-installed')"
{app_probe}'''


def server_probe_command() -> str:
    """探测命令；可额外监控的服务名通过环境变量配置（默认空=不探测该服务）。

    注意：systemctl is-active 对不存在的服务会输出 inactive（退出码 3）而非报错，
    所以必须用重定向 + || 显式兜底，避免把 inactive 当真实状态。
    """
    app_service = os.getenv("WORKBENCH_APP_SERVICE_NAME", "").strip()
    if app_service:
        app_probe = f"printf 'APP|%s\\n' \"$(systemctl is-active {app_service} >/dev/null 2>&1 && printf 'active' || printf 'unknown')\""
    else:
        app_probe = "# APP 服务未配置（WORKBENCH_APP_SERVICE_NAME），跳过探测"
    # 模板含 awk 花括号，不能用 .format，用占位符替换
    return SERVER_MONITOR_PROBE_COMMAND_TEMPLATE.replace("{app_probe}", app_probe)


DEFAULT_SERVER_THRESHOLDS: dict[str, float] = {
    "disk_warn": 85,
    "disk_critical": 95,
    "memory_warn": 85,
    "memory_critical": 95,
    "load_warn": 4,
    "load_critical": 8,
}


def load_server_monitor_thresholds() -> dict[str, float]:
    try:
        values = json.loads(SERVER_MONITOR_THRESHOLDS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        values = {}
    if not isinstance(values, dict):
        values = {}
    normalized: dict[str, float] = {}
    for key, default in DEFAULT_SERVER_THRESHOLDS.items():
        raw = values.get(key)
        try:
            number = float(raw)
        except (TypeError, ValueError):
            number = default
        normalized[key] = max(0.0, min(100.0 if "disk" in key or "memory" in key else 1000.0, number))
    return normalized


def save_server_monitor_thresholds(values: dict[str, float]) -> dict[str, float]:
    normalized = {key: float(value) for key, value in values.items() if key in DEFAULT_SERVER_THRESHOLDS}
    save_json_atomic(SERVER_MONITOR_THRESHOLDS_FILE, normalized, 0o600)
    return _app_call('load_server_monitor_thresholds', )


def read_server_monitor() -> dict[str, Any]:
    config = _app_call('server_monitor_config', )
    command = _app_call('server_probe_command', )
    target = config["server"]
    is_local = _app_call('server_target_is_local', target)
    if is_local:
        # Deployment mode: Workbench runs on the very server it monitors.
        # Probe locally so the monitor keeps working without SSH credentials.
        result = subprocess.run(["bash", "-lc", command], capture_output=True, text=True, timeout=25, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "本地探测失败").strip()
            raise RuntimeError(detail[-500:])
    else:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={config['known_hosts']}" ]
        if config["ssh_key"]:
            args.extend(["-i", config["ssh_key"]])
        args.extend([target, "bash", "-lc", command])
        result = subprocess.run(args, capture_output=True, text=True, timeout=25, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "SSH 检查失败").strip()
            raise RuntimeError(detail[-500:])
    parsed: dict[str, Any] = {"server": target, "checked_at": now_iso(), "status": "ok"}
    if is_local:
        parsed["mode"] = "local"
    for line in result.stdout.splitlines():
        key, _, value = line.partition("|")
        if not _:
            continue
        value = value.strip()
        if key == "HOST": parsed["host"] = value
        elif key == "OS": parsed["os"] = value
        elif key == "UPTIME": parsed["uptime"] = value
        elif key == "LOAD": parsed["load"] = value
        elif key == "DISK":
            disk = value.split("|")
            parsed["disk"] = {"total_kb": disk[0], "used_kb": disk[1], "used_pct": disk[2]} if len(disk) == 3 else {"raw": value}
        elif key == "MEMORY":
            memory = value.split("|")
            parsed["memory"] = {"total_mb": memory[0], "used_mb": memory[1], "available_mb": memory[2]} if len(memory) == 3 else {"raw": value}
        elif key in {"NGINX", "WORKBENCH", "APP"}:
            parsed[key.lower()] = value
    return parsed

@app.get("/api/server")
async def get_server_monitor() -> dict[str, Any]:
    snapshot = _app_call('load_server_monitor_snapshot', )
    safe_config = _app_call('server_monitor_config', )
    history = await asyncio.to_thread(list_server_monitor_history, limit=30)
    return {
        "server": snapshot,
        "analysis": _app_call('analyze_server_snapshot', snapshot, history),
        "history": history,
        "target": safe_config["server"],
        "configured": bool(safe_config["server"]),
        "thresholds": _app_call('load_server_monitor_thresholds', ),
    }


@app.get("/api/server/thresholds")
async def get_server_thresholds() -> dict[str, Any]:
    return {"thresholds": _app_call('load_server_monitor_thresholds', ), "defaults": DEFAULT_SERVER_THRESHOLDS}


@app.put("/api/server/thresholds")
def update_server_thresholds(request: ServerThresholdsRequest) -> dict[str, Any]:
    thresholds = _app_call('save_server_monitor_thresholds', request.thresholds)
    snapshot = _app_call('load_server_monitor_snapshot', )
    history = _app_call('list_server_monitor_history', limit=30)
    return {"ok": True, "thresholds": thresholds, "analysis": _app_call('analyze_server_snapshot', snapshot, history)}


@app.post("/api/server/refresh")
async def refresh_server_monitor(request: ServerMonitorRequest) -> dict[str, Any]:
    if not request.refresh:
        return await _app_call('get_server_monitor', )
    try:
        snapshot = await asyncio.to_thread(read_server_monitor)
    except Exception as exc:
        previous = _app_call('load_server_monitor_snapshot', )
        previous.update({"status": "error", "error": str(exc), "checked_at": now_iso()})
        _app_call('save_server_monitor_snapshot', previous)
        await asyncio.to_thread(record_server_monitor_snapshot, previous)
        artifact = await asyncio.to_thread(_app_call, "register_artifact_safely", 
            project_id="server",
            name="server_monitor_snapshot.json",
            path=str(SERVER_MONITOR_SNAPSHOT_FILE),
            kind="server_snapshot",
            metadata={"status": "error", "error": str(exc)},
        )
        evaluation = await asyncio.to_thread(evaluate_server_monitor, previous, create_records=True)
        raise HTTPException(502, f"服务器检查失败：{exc}") from exc
    _app_call('save_server_monitor_snapshot', snapshot)
    await asyncio.to_thread(record_server_monitor_snapshot, snapshot)
    artifact = await asyncio.to_thread(_app_call, "register_artifact_safely", 
        project_id="server",
        name="server_monitor_snapshot.json",
        path=str(SERVER_MONITOR_SNAPSHOT_FILE),
        kind="server_snapshot",
        metadata={"status": snapshot.get("status"), "checked_at": snapshot.get("checked_at")},
    )
    evaluation = await asyncio.to_thread(evaluate_server_monitor, snapshot, create_records=True)
    return {
        "server": snapshot,
        "analysis": evaluation["analysis"],
        "history": evaluation["history"],
        "evaluation": evaluation,
        "target": _app_call('server_monitor_config', )["server"],
        "configured": True,
        "artifact": artifact,
    }


__all__ = [
    "DEFAULT_SERVER_THRESHOLDS",
    "server_probe_command",
    "SERVER_SAFE_ACTIONS",
    "ServerActionRequest",
    "ServerMonitorRequest",
    "ServerThresholdsRequest",
    "_server_action_execution_payload",
    "_sub2api_timestamp",
    "analyze_server_snapshot",
    "create_notification_record",
    "create_relation_record",
    "create_server_action_execution",
    "create_work_item_record",
    "evaluate_server_monitor",
    "execute_approved_server_action",
    "execute_server_action",
    "finish_server_action_execution",
    "get_server_action_executions",
    "get_server_monitor",
    "get_server_thresholds",
    "latest_server_action_execution",
    "list_artifacts",
    "list_server_action_executions",
    "list_server_monitor_history",
    "list_work_items",
    "load_server_monitor_snapshot",
    "load_server_monitor_thresholds",
    "read_server_monitor",
    "record_server_monitor_snapshot",
    "refresh_server_monitor",
    "request_server_action",
    "rollback_server_action",
    "rollback_server_action_execution",
    "save_server_monitor_snapshot",
    "save_server_monitor_thresholds",
    "server_monitor_config",
    "server_monitor_snapshot_row",
    "server_number",
    "server_target_is_local",
    "update_server_thresholds",
    "update_work_item_record",
]
