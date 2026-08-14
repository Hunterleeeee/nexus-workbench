"""Workbench 自动化与执行计划：规则/运行记录、总调度执行器、执行计划。

从 app.py 拆出的平台执行层（为开源准备）。总调度 execute_automation_rule 调
用各领域函数——已拆模块（inbox/server/sub2api/push/git/llm/agent_platform）
直接导入；仍在 app.py 的领域函数（market/aihot/knowledge/ai-learning/idea/
cid/evidence/projects）走 _app_call 运行时转发，保证测试 patch app.X 生效。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .agent_platform import AgentDispatchRequest, dispatch_agent_task
from .core import clip, log, now_iso
from .db import db_connection
from .git import git_inventory
from .inbox import list_inbox, triage_inbox_record
from .instance import app
from .llm import _llm_error_kind, _llm_error_retryable
from .notifications import create_notification_record
from .push import _push_to_all_subscriptions
from .server import evaluate_server_monitor
from .sub2api import evaluate_sub2api_alerts


def _app_call(name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, name)(*args, **kwargs)


def _decode_json(value: Any, fallback: Any) -> Any:
    """转发 app.platform_decode_json（通用 JSON 解码工具仍在 app.py）。"""
    import app as _app

    return _app.platform_decode_json(value, fallback)


def _audit_datetime(value: Any) -> datetime | None:
    """转发 app._audit_datetime（仍在 app.py）。"""
    import app as _app

    return _app._audit_datetime(value)


def _stale_seconds() -> int:
    """运行时读 app.WORKBENCH_AUTOMATION_STALE_SECONDS——测试 patch app.X 时生效。"""
    import app as _app

    return int(getattr(_app, "WORKBENCH_AUTOMATION_STALE_SECONDS", WORKBENCH_AUTOMATION_STALE_SECONDS))


WORKBENCH_AUTOMATION_STALE_SECONDS = max(300, int(os.getenv("WORKBENCH_AUTOMATION_STALE_SECONDS", "900")))


def automation_rule_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result.get("enabled"))
    result["config"] = _decode_json(result.pop("config_json", "{}"), {})
    return result


def automation_run_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["result"] = _decode_json(result.pop("result_json", "{}"), {})
    return result


def plan_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["input"] = _decode_json(result.pop("input_json", "{}"), {})
    result["result"] = _decode_json(result.pop("result_json", "{}"), {})
    return result


def plan_step_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["dependencies"] = _decode_json(result.pop("dependencies_json", "[]"), [])
    result["input"] = _decode_json(result.pop("input_json", "{}"), {})
    result["result"] = _decode_json(result.pop("result_json", "{}"), {})
    return result



def automation_rules() -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT * FROM automation_rules ORDER BY enabled DESC, updated_at DESC, id DESC").fetchall()
        return [automation_rule_row(row) for row in rows]
    finally:
        connection.close()


def recover_stale_automation_runs() -> dict[str, Any]:
    """Close orphaned queued automation runs after an API/worker restart.

    ``execute_automation_rule`` records a run before doing its work.  There is
    no legitimate long-running state in which that record should stay queued:
    the request either starts it immediately or disappears.  Older versions
    therefore left misleading queued rows forever, while the UI showed the
    rule as healthy because a later run had succeeded.  Recover only queued
    rows (never an active running row), keep the original record, and expose
    the IDs to the caller so the UI can show a truthful, retryable state.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_stale_seconds())
    recovered: list[dict[str, Any]] = []
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT id, rule_id, created_at FROM automation_runs WHERE status = 'queued' ORDER BY created_at ASC"
        ).fetchall()
        timestamp = now_iso()
        for row in rows:
            created_at = _audit_datetime(row["created_at"])
            if not created_at or created_at >= cutoff:
                continue
            error = "服务重启后未被领取，已自动标记为失败；可从自动化中心重试。"
            cursor = connection.execute(
                """UPDATE automation_runs
                   SET status = 'failed', error = ?, started_at = COALESCE(NULLIF(started_at, ''), ?), finished_at = ?
                   WHERE id = ? AND status = 'queued'""",
                (error, str(row["created_at"] or timestamp), timestamp, str(row["id"])),
            )
            if cursor.rowcount:
                recovered.append({"id": str(row["id"]), "rule_id": int(row["rule_id"] or 0), "error": error})
        connection.commit()
    finally:
        connection.close()
    return {
        "recovered_count": len(recovered),
        "recovered_runs": recovered,
        "threshold_seconds": _stale_seconds(),
        "policy": "仅恢复超时 queued 记录；running 记录由实际执行边界和 Worker lease 负责，不在读取接口中强制结束。",
    }


def get_automation_rule(rule_id: int) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM automation_rules WHERE id = ?", (rule_id,)).fetchone()
        return automation_rule_row(row) if row else None
    finally:
        connection.close()


def list_automation_runs(rule_id: int = 0, limit: int = 60) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        query = "SELECT * FROM automation_runs"
        params: list[Any] = []
        if rule_id:
            query += " WHERE rule_id = ?"
            params.append(rule_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(200, int(limit or 60))))
        rows = connection.execute(query, params).fetchall()
        return [automation_run_row(row) for row in rows]
    finally:
        connection.close()


def save_automation_rule(*, name: str, kind: str, project_id: str, schedule: str, enabled: bool, config: dict[str, Any], rule_id: int = 0) -> dict[str, Any]:
    timestamp = now_iso()
    connection = db_connection()
    try:
        if rule_id:
            cursor = connection.execute(
                "UPDATE automation_rules SET name = ?, kind = ?, project_id = ?, schedule = ?, enabled = ?, config_json = ?, updated_at = ? WHERE id = ?",
                (name, kind, project_id, schedule, int(enabled), json.dumps(config, ensure_ascii=False), timestamp, rule_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("自动化规则不存在")
        else:
            cursor = connection.execute(
                "INSERT INTO automation_rules(name, kind, project_id, schedule, enabled, config_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, kind, project_id, schedule, int(enabled), json.dumps(config, ensure_ascii=False), timestamp, timestamp),
            )
            rule_id = int(cursor.lastrowid)
        connection.commit()
        row = connection.execute("SELECT * FROM automation_rules WHERE id = ?", (rule_id,)).fetchone()
        return automation_rule_row(row)
    finally:
        connection.close()


def create_automation_run_record(rule_id: int, trigger: str) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    connection = db_connection()
    try:
        connection.execute("INSERT INTO automation_runs(id, rule_id, trigger, created_at) VALUES (?, ?, ?, ?)", (run_id, rule_id, trigger, now_iso()))
        connection.execute("UPDATE automation_rules SET status = 'running', last_error = '', updated_at = ? WHERE id = ?", (now_iso(), rule_id))
        connection.commit()
    finally:
        connection.close()
    return {"id": run_id, "rule_id": rule_id, "trigger": trigger, "status": "queued"}


def finish_automation_run(run_id: str, rule_id: int, status: str, result: dict[str, Any] | None = None, error: str = "") -> None:
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute("UPDATE automation_runs SET status = ?, result_json = ?, error = ?, started_at = COALESCE(NULLIF(started_at, ''), ?), finished_at = ? WHERE id = ?", (status, json.dumps(result or {}, ensure_ascii=False), error, timestamp, timestamp, run_id))
        connection.execute("UPDATE automation_rules SET status = ?, last_run_at = ?, last_error = ?, updated_at = ? WHERE id = ?", (status, timestamp, error, timestamp, rule_id))
        connection.commit()
    finally:
        connection.close()


async def execute_automation_rule(rule_id: int, trigger: str = "manual") -> dict[str, Any]:
    rule = get_automation_rule(rule_id)
    if not rule:
        raise HTTPException(404, "自动化规则不存在")
    run = create_automation_run_record(rule_id, trigger)
    try:
        kind = str(rule.get("kind") or "").strip()
        if kind == "market_refresh":
            result = await _app_call("refresh_market_quotes")
        elif kind == "aihot_refresh":
            snapshot = await _app_call("fetch_aihot_snapshot", force=True)
            result = {"snapshot": snapshot, "count": len(snapshot.get("items") or [])}
            create_notification_record(title="AI 热点自动同步完成", body=f"本轮获取 {result['count']} 条资讯。", project_id="aihot", kind="automation", level="info", href="/projects/aihot", event_key=f"automation:aihot:{run['id']}", dedupe_seconds=0)
        elif kind == "server_check":
            result = evaluate_server_monitor(create_records=True)
        elif kind == "sub2api_alerts":
            result = evaluate_sub2api_alerts(create_records=True)
        elif kind == "inbox_triage":
            items = list_inbox("inbox")
            results = [triage_inbox_record(int(item["id"])) for item in items[:30]]
            result = {"count": len(results), "items": results}
        elif kind == "knowledge_index":
            result = _app_call("obsidian_index_vault")
        elif kind == "inbox_daily_digest":
            items = list_inbox("inbox")
            overdue = [item for item in items if item.get("is_overdue")]
            today = [item for item in items if str(item.get("created_at") or "")[:10] == datetime.now(timezone.utc).strftime("%Y-%m-%d")]
            result = {"open": len(items), "overdue": len(overdue), "added_today": len(today)}
            create_notification_record(title="收件箱日报", body=f"待处理 {len(items)} 条 · 今日新增 {len(today)} · 已过期 {len(overdue)}", project_id="inbox", kind="automation", level="info" if not overdue else "warning", href="/projects/inbox", event_key=f"automation:inbox-digest:{run['id']}", dedupe_seconds=0)
        elif kind == "knowledge_weekly_digest":
            notes = _app_call("knowledge_files")
            result = {"note_count": len(notes)}
            create_notification_record(title="知识库周报", body=f"本地知识库现有 {len(notes)} 篇笔记。", project_id="knowledge", kind="automation", level="info", href="/projects/knowledge", event_key=f"automation:knowledge-weekly:{run['id']}", dedupe_seconds=0)
        elif kind == "market_daily_report":
            result = await _app_call("refresh_market_quotes")
            create_notification_record(title="行情日报已生成", body="可到量化选股页面生成正式日报。", project_id="market", kind="automation", level="info", href="/projects/market", event_key=f"automation:market-daily:{run['id']}", dedupe_seconds=0)
        elif kind == "server_weekly_report":
            result = evaluate_server_monitor(create_records=True)
            create_notification_record(title="服务器周检完成", body="本轮巡检已完成，异常已生成告警。", project_id="server", kind="automation", level="info", href="/projects/server", event_key=f"automation:server-weekly:{run['id']}", dedupe_seconds=0)
        elif kind == "crawl_retry_failed":
            failed = [r for r in _app_call("list_agent_runs", "crawl4ai", limit=50) if r.get("status") == "failed" and r.get("retryable")]
            result = {"failed_seen": len(failed)}
            if failed:
                create_notification_record(title="爬虫失败待重试", body=f"有 {len(failed)} 个失败抓取任务可重试，进入 Crawl4AI 页面点击重试。", project_id="crawl4ai", kind="automation", level="warning", href="/crawl4ai", event_key=f"automation:crawl-retry:{run['id']}", dedupe_seconds=0)
        elif kind == "aihot_digest_daily":
            summary = await _app_call("create_aihot_summary")
            result = {"artifact": summary.get("artifact"), "path": summary.get("path")}
        elif kind == "ai_learning_daily":
            lesson = await _app_call("generate_ai_learning_lesson", use_llm=True)
            content = lesson.get("content") or {}
            body = clip(f"{content.get('objective') or content.get('takeaway') or '今天用一节小课继续 AI 转型。'} · {_app_call('get_ai_learning_profile').get('daily_minutes', 25)} 分钟", 180)
            event_key = f"ai-learning-daily:{lesson.get('lesson_date')}"
            notification = create_notification_record(
                title=f"今日 AI 转型课 · {lesson.get('title')}", body=body,
                project_id="ai-learning", kind="learning", level="info", href="/projects/ai-learning",
                event_key=event_key, dedupe_seconds=86_400,
            )
            try:
                push_result = await _push_to_all_subscriptions(
                    title=f"今日 AI 转型课 · {lesson.get('title')}", body=body,
                    href="/projects/ai-learning", event_key=event_key,
                )
            except Exception as exc:
                push_result = {"sent": 0, "failed": 0, "error": clip(str(exc), 240)}
            result = {"lesson": lesson, "notification": notification, "push": push_result}
        elif kind == "idea_task_reminder":
            followups = _app_call("idea_followups_payload", 3)
            due_soon = [task for task in followups.get("tasks", []) if task.get("bucket") in {"overdue", "due_soon"}]
            result = {"due_soon": len(due_soon), "summary": followups.get("summary", {})}
            if due_soon:
                names = "、".join(str(t.get("title") or "")[:24] for t in due_soon[:3])
                create_notification_record(title="验证任务需要跟进", body=f"{len(due_soon)} 个验证任务需要跟进：{names}", project_id="idea-analysis", kind="automation", level="warning", href="/projects/idea-analysis", event_key=f"automation:idea-remind:{run['id']}", dedupe_seconds=0)
        elif kind == "cid_snapshot_audit":
            snapshots = _app_call("list_cid_dashboard_snapshots", "", limit=3)
            result = {"snapshots": len(snapshots)}
            create_notification_record(title="看板快照巡检", body=f"已确认 {len(snapshots)} 份看板快照存档。", project_id="cid-dashboard", kind="automation", level="info", href="/projects/cid-dashboard", event_key=f"automation:cid-audit:{run['id']}", dedupe_seconds=0)
        elif kind == "git_scan":
            result = {"repositories": git_inventory()}
        elif kind == "evidence_audit":
            result = _app_call("run_evidence_matrix")
        elif kind == "worker_health_check":
            all_workers = _app_call("worker_status_payload")
            stale = [worker for worker in all_workers if worker.get("stale")]
            result = {"workers": len(all_workers), "stale": len(stale), "ok": not stale}
            if stale:
                names = "、".join(str(worker.get("label") or worker.get("id") or "") for worker in stale[:5])
                _app_call("create_work_item_record", 
                    title=f"Worker 心跳异常：{names}",
                    description=f"{len(stale)} 个 Worker 心跳过期或失联：{names}。请检查对应 systemd 服务（systemctl status workbench-*-worker.service）。",
                    kind="alert", priority="high", source_project="workbench", target_project="server",
                    metadata={"alert_key": f"worker-heartbeat:{now_iso()}", "worker_ids": [worker.get("id") for worker in stale], "source": "worker_health_check"},
                )
                create_notification_record(title="Worker 心跳异常", body=f"{len(stale)} 个 Worker 心跳过期：{names}。", project_id="server", kind="automation", level="warning", href="/projects/server", event_key=f"automation:worker-health:{run['id']}", dedupe_seconds=0)
        elif kind == "notification":
            config = rule.get("config") or {}
            result = {"notification": create_notification_record(title=str(config.get("title") or rule["name"]), body=str(config.get("body") or "自动化规则已执行"), project_id=rule.get("project_id") or "workbench", kind="automation", level="info", href=str(config.get("href") or "/"), event_key=f"automation:{rule_id}:{run['id']}", dedupe_seconds=0)}
        else:
            raise ValueError(f"不支持的自动化类型：{kind}")
        finish_automation_run(run["id"], rule_id, "succeeded", result=result)
        return {"run": {**run, "status": "succeeded", "result": result}, "rule": get_automation_rule(rule_id), "result": result}
    except Exception as exc:
        error = clip(str(exc), 800)
        finish_automation_run(run["id"], rule_id, "failed", error=error)
        # 可恢复错误（LLM 冷却/网络/超时/5xx）自动补跑，最多重试 2 次，
        # 避免一次临时故障让整条自动化规则一直挂在失败上。
        retryable = _llm_error_retryable(_llm_error_kind(exc)) or any(marker in str(exc).lower() for marker in ("cooldown", "rate_limit", "timeout", "connection", "502", "503", "temporarily"))
        result_json = run.get("result") if isinstance(run.get("result"), dict) else {}
        attempts = result_json.get("retry_attempts", []) if isinstance(result_json.get("retry_attempts"), list) else []
        if retryable and len(attempts) < 2 and not os.getenv("WORKBENCH_DISABLE_AUTOMATION_RETRY", "").strip().lower() in {"1", "true", "yes"}:
            delay = 45 * (len(attempts) + 1)
            attempts.append({"at": now_iso(), "error": error})
            connection = db_connection()
            try:
                connection.execute("UPDATE automation_runs SET result_json = ?, error = '' WHERE id = ?", (json.dumps({**result_json, "retry_attempts": attempts, "retry_scheduled_at": now_iso()}, ensure_ascii=False), run["id"]))
                connection.commit()
            finally:
                connection.close()

            async def retry_later():
                await asyncio.sleep(delay)
                try:
                    await execute_automation_rule(rule_id, trigger=f"auto-retry-{len(attempts)}")
                except Exception:
                    log.debug("忽略异常（retry_later）", exc_info=True)
            asyncio.get_event_loop().create_task(retry_later())
            create_notification_record(title=f"自动化失败将重试：{rule.get('name')}", body=f"{clip(error, 200)}；将在 {delay} 秒后自动重试（第 {len(attempts)}/2 次）。", project_id=rule.get("project_id") or "workbench", kind="automation", level="warning", href="/automation", event_key=f"automation-retry:{rule_id}:{run['id']}", dedupe_seconds=0)
        else:
            try:
                create_notification_record(title=f"自动化失败：{rule.get('name')}", body=error, project_id=rule.get("project_id") or "workbench", kind="automation", level="error", href="/automation", event_key=f"automation-failed:{rule_id}:{run['id']}", dedupe_seconds=0)
            except Exception:
                log.debug("忽略异常（execute_automation_rule）", exc_info=True)
        raise HTTPException(502, f"自动化执行失败：{error}") from exc


def create_execution_plan(title: str, source_project: str, steps: list[dict[str, Any]], input_data: dict[str, Any]) -> dict[str, Any]:
    plan_id = uuid.uuid4().hex
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute("INSERT INTO execution_plans(id, title, source_project, input_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (plan_id, title, source_project, json.dumps(input_data, ensure_ascii=False), timestamp, timestamp))
        for index, raw in enumerate(steps, start=1):
            step_key = str(raw.get("key") or f"step-{index}").strip()
            connection.execute(
                "INSERT INTO execution_plan_steps(plan_id, step_key, title, project_id, kind, dependencies_json, input_json, max_attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_id, step_key, str(raw.get("title") or step_key)[:200], str(raw.get("project_id") or "workbench"), str(raw.get("kind") or "agent"), json.dumps(raw.get("dependencies") or [], ensure_ascii=False), json.dumps(raw.get("input") or {}, ensure_ascii=False), max(1, min(3, int(raw.get("max_attempts") or 2))), timestamp, timestamp),
            )
        connection.commit()
        return get_execution_plan(plan_id) or {"id": plan_id}
    finally:
        connection.close()


def get_execution_plan(plan_id: str) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM execution_plans WHERE id = ?", (plan_id,)).fetchone()
        if not row:
            return None
        plan = plan_row(row)
        steps = connection.execute("SELECT * FROM execution_plan_steps WHERE plan_id = ? ORDER BY id", (plan_id,)).fetchall()
        plan["steps"] = [plan_step_row(step) for step in steps]
        return plan
    finally:
        connection.close()


def update_plan_status(plan_id: str, status: str, result: dict[str, Any] | None = None, error: str = "") -> None:
    connection = db_connection()
    try:
        connection.execute("UPDATE execution_plans SET status = ?, result_json = ?, error = ?, updated_at = ? WHERE id = ?", (status, json.dumps(result or {}, ensure_ascii=False), error, now_iso(), plan_id))
        connection.commit()
    finally:
        connection.close()


def claim_next_execution_plan() -> dict[str, Any] | None:
    """Atomically claim one queued plan for the standalone Agent Worker."""
    connection = db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT id FROM execution_plans WHERE status = 'queued' ORDER BY updated_at ASC LIMIT 1").fetchone()
        if not row:
            connection.rollback()
            return None
        plan_id = str(row["id"])
        connection.execute("UPDATE execution_plans SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'", (now_iso(), plan_id))
        connection.commit()
    finally:
        connection.close()
    return get_execution_plan(plan_id)


def update_plan_step(step_id: int, **values: Any) -> None:
    allowed = {"status", "attempt", "run_id", "work_item_id", "result_json", "error", "updated_at"}
    fields = [(key, value) for key, value in values.items() if key in allowed]
    if not fields:
        return
    fields.append(("updated_at", now_iso()))
    assignments = ", ".join(f"{key} = ?" for key, _value in fields)
    connection = db_connection()
    try:
        connection.execute(f"UPDATE execution_plan_steps SET {assignments} WHERE id = ?", [value for _key, value in fields] + [step_id])
        connection.commit()
    finally:
        connection.close()


async def run_execution_plan(plan_id: str) -> dict[str, Any]:
    plan = get_execution_plan(plan_id)
    if not plan:
        raise HTTPException(404, "执行计划不存在")
    update_plan_status(plan_id, "running")
    steps = {step["step_key"]: step for step in plan["steps"]}
    completed: dict[str, dict[str, Any]] = {}
    try:
        while len(completed) < len(steps):
            progressed = False
            for key, step in steps.items():
                if key in completed or step.get("status") in {"succeeded", "skipped"}:
                    completed[key] = step
                    continue
                dependencies = [str(item) for item in step.get("dependencies") or []]
                failed_dependency = next((item for item in dependencies if steps.get(item, {}).get("status") in {"failed", "blocked"}), None)
                if failed_dependency:
                    update_plan_step(step["id"], status="blocked", error=f"依赖步骤 {failed_dependency} 未成功")
                    raise RuntimeError(f"步骤 {step['title']} 被依赖步骤 {failed_dependency} 阻塞")
                if any(item not in completed for item in dependencies):
                    continue
                progressed = True
                attempt = int(step.get("attempt") or 0) + 1
                update_plan_step(step["id"], status="running", attempt=attempt, error="")
                try:
                    if step.get("kind") == "automation":
                        rule_id = int((step.get("input") or {}).get("rule_id") or 0)
                        result = await execute_automation_rule(rule_id, trigger=f"plan:{plan_id}:{key}")
                        run_id = str((result.get("run") or {}).get("id") or "")
                        update_plan_step(step["id"], status="succeeded", run_id=run_id, result_json=json.dumps(result, ensure_ascii=False))
                    elif step.get("kind") == "local":
                        action = str((step.get("input") or {}).get("action") or "")
                        if action == "git_scan":
                            result = {"repositories": git_inventory()}
                        elif action == "backup":
                            result = create_database_backup("plan")
                        else:
                            raise ValueError(f"不支持的本地计划动作：{action}")
                        update_plan_step(step["id"], status="succeeded", result_json=json.dumps(result, ensure_ascii=False))
                    else:
                        input_data = step.get("input") or {}
                        message = str(input_data.get("message") or step.get("title") or "执行项目任务")
                        project_id = str(step.get("project_id") or "workbench")
                        dispatch = AgentDispatchRequest(message=message, project_ids=[project_id] if project_id != "workbench" else [], context={"plan_id": plan_id, "step_key": key, **(input_data.get("context") or {})})
                        result = await dispatch_agent_task(dispatch, parent_run_id="")
                        run_id = str((result.get("run") or {}).get("id") or "")
                        update_plan_step(step["id"], status="succeeded", run_id=run_id, result_json=json.dumps(result, ensure_ascii=False))
                    completed[key] = {**step, "status": "succeeded", "result": result}
                except Exception as exc:
                    error = clip(str(exc), 1_000)
                    if attempt < int(step.get("max_attempts") or 2):
                        update_plan_step(step["id"], status="pending", error=error)
                    else:
                        update_plan_step(step["id"], status="failed", error=error)
                        raise
            if not progressed:
                raise RuntimeError("计划存在循环依赖或未满足的依赖")
            steps = {step["step_key"]: step for step in (get_execution_plan(plan_id) or {}).get("steps", [])}
        result = {"completed": list(completed.keys()), "steps": list(completed.values())}
        update_plan_status(plan_id, "succeeded", result=result)
        create_notification_record(title="执行计划已完成", body=f"{plan.get('title')} · {len(completed)} 个步骤完成。", project_id=plan.get("source_project") or "workbench", kind="plan", level="success", href="/", event_key=f"plan:{plan_id}:succeeded", dedupe_seconds=0)
        return get_execution_plan(plan_id) or {"id": plan_id, "status": "succeeded"}
    except Exception as exc:
        error = clip(str(exc), 1_000)
        update_plan_status(plan_id, "blocked", result={"completed": list(completed.keys())}, error=error)
        create_notification_record(title="执行计划已暂停", body=f"{plan.get('title')} · {error} · 可人工接管后重试。", project_id=plan.get("source_project") or "workbench", kind="plan", level="warning", href="/", event_key=f"plan:{plan_id}:blocked", dedupe_seconds=0)
        raise HTTPException(409, f"计划已暂停：{error}") from exc


class AutomationRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=80)
    project_id: str = Field(default="workbench", max_length=80)
    schedule: str = Field(default="", max_length=80)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlanRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    source_project: str = Field(default="workbench", max_length=80)
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    input: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/automations")
def get_automations() -> dict[str, Any]:
    recovery = recover_stale_automation_runs()
    rules = automation_rules()
    runs = list_automation_runs(limit=80)
    for rule in rules:
        rule["recent_runs"] = [item for item in runs if int(item.get("rule_id") or 0) == int(rule.get("id") or 0)][:5]
        rule["run_summary"] = {
            "total": sum(1 for item in runs if int(item.get("rule_id") or 0) == int(rule.get("id") or 0)),
            "succeeded": sum(1 for item in runs if int(item.get("rule_id") or 0) == int(rule.get("id") or 0) and item.get("status") == "succeeded"),
            "failed": sum(1 for item in runs if int(item.get("rule_id") or 0) == int(rule.get("id") or 0) and item.get("status") == "failed"),
        }
    return {"rules": rules, "runs": runs, "recovery": recovery, "summary": {
        "failed_runs": sum(1 for run in runs if run.get("status") == "failed"),
        "running_runs": sum(1 for run in runs if run.get("status") == "running"),
        "queued_runs": sum(1 for run in runs if run.get("status") == "queued"),
        "succeeded_runs": sum(1 for run in runs if run.get("status") == "succeeded"),
    }, "kinds": [
        {"kind": "market_refresh", "label": "刷新行情", "project_id": "market"},
        {"kind": "aihot_refresh", "label": "同步 AI 热点", "project_id": "aihot"},
        {"kind": "ai_learning_daily", "label": "推送每日 AI 转型课", "project_id": "ai-learning"},
        {"kind": "server_check", "label": "服务器巡检", "project_id": "server"},
        {"kind": "sub2api_alerts", "label": "Sub2API 风险检查", "project_id": "sub2api"},
        {"kind": "inbox_triage", "label": "整理收件箱", "project_id": "inbox"},
        {"kind": "knowledge_index", "label": "索引 Obsidian", "project_id": "knowledge"},
        {"kind": "git_scan", "label": "扫描 Git 项目", "project_id": "workbench"},
        {"kind": "evidence_audit", "label": "联动证据审计", "project_id": "workbench"},
        {"kind": "worker_health_check", "label": "Worker 心跳检查", "project_id": "server"},
        {"kind": "notification", "label": "发送工作台提醒", "project_id": "workbench"},
    ]}


@app.get("/api/automations/runs")
def get_automation_runs(rule_id: int = 0, limit: int = 60) -> dict[str, Any]:
    recovery = recover_stale_automation_runs()
    return {"runs": list_automation_runs(rule_id, limit), "recovery": recovery}


@app.post("/api/automations")
def create_automation(request: AutomationRuleRequest) -> dict[str, Any]:
    return {"rule": save_automation_rule(name=request.name.strip(), kind=request.kind.strip(), project_id=request.project_id.strip() or "workbench", schedule=request.schedule.strip(), enabled=request.enabled, config=request.config)}


@app.patch("/api/automations/{rule_id}")
def update_automation(rule_id: int, request: AutomationRuleRequest) -> dict[str, Any]:
    return {"rule": save_automation_rule(name=request.name.strip(), kind=request.kind.strip(), project_id=request.project_id.strip() or "workbench", schedule=request.schedule.strip(), enabled=request.enabled, config=request.config, rule_id=rule_id)}


@app.delete("/api/automations/{rule_id}")
def delete_automation(rule_id: int) -> dict[str, Any]:
    connection = db_connection()
    try:
        cursor = connection.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
        connection.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "自动化规则不存在")
        return {"ok": True}
    finally:
        connection.close()


@app.post("/api/automations/{rule_id}/run")
async def run_automation(rule_id: int) -> dict[str, Any]:
    return await execute_automation_rule(rule_id)


@app.get("/api/plans")
def list_plans(limit: int = 30) -> dict[str, Any]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT id FROM execution_plans ORDER BY updated_at DESC LIMIT ?", (max(1, min(100, limit)),)).fetchall()
        return {"plans": [get_execution_plan(row["id"]) for row in rows]}
    finally:
        connection.close()


@app.post("/api/plans")
def create_plan(request: ExecutionPlanRequest) -> dict[str, Any]:
    if not request.steps:
        raise HTTPException(400, "计划至少需要一个步骤")
    keys = [str(step.get("key") or f"step-{index}") for index, step in enumerate(request.steps, start=1)]
    if len(keys) != len(set(keys)):
        raise HTTPException(400, "计划步骤 key 不能重复")
    unknown = [str(dep) for step in request.steps for dep in (step.get("dependencies") or []) if str(dep) not in keys]
    if unknown:
        raise HTTPException(400, f"计划依赖不存在：{unknown[0]}")
    return {"plan": create_execution_plan(request.title.strip(), request.source_project.strip() or "workbench", request.steps, request.input)}


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: str) -> dict[str, Any]:
    plan = get_execution_plan(plan_id)
    if not plan:
        raise HTTPException(404, "执行计划不存在")
    return {"plan": plan}


@app.post("/api/plans/{plan_id}/run")
async def start_plan(plan_id: str) -> dict[str, Any]:
    plan = await asyncio.to_thread(get_execution_plan, plan_id)
    if not plan:
        raise HTTPException(404, "执行计划不存在")
    if os.getenv("WORKBENCH_EXTERNAL_AGENT_WORKER", "").strip().lower() in {"1", "true", "yes"}:
        if plan.get("status") in {"queued", "running"}:
            raise HTTPException(409, "这个计划已经在队列或运行中")
        await asyncio.to_thread(update_plan_status, plan_id, "queued", result={"queued_at": now_iso(), "execution_boundary": "agent-worker"}, error="")
        create_notification_record(title="执行计划已进入 Agent Worker 队列", body=f"{plan.get('title')} · 等待独立 Worker 执行。", project_id=plan.get("source_project") or "workbench", kind="plan", level="info", href="/automation", event_key=f"plan:{plan_id}:queued", dedupe_seconds=0)
        return {"queued": True, "plan": get_execution_plan(plan_id), "execution_boundary": "agent-worker"}
    return {"plan": await run_execution_plan(plan_id)}


@app.post("/api/plans/{plan_id}/takeover")
def takeover_plan(plan_id: str) -> dict[str, Any]:
    plan = get_execution_plan(plan_id)
    if not plan:
        raise HTTPException(404, "执行计划不存在")
    update_plan_status(plan_id, "draft", result={"manual_takeover": True, "taken_over_at": now_iso()})
    return {"ok": True, "plan": get_execution_plan(plan_id)}



__all__ = [
    "WORKBENCH_AUTOMATION_STALE_SECONDS",
    "automation_rule_row",
    "automation_run_row",
    "plan_row",
    "plan_step_row",
    "automation_rules",
    "recover_stale_automation_runs",
    "get_automation_rule",
    "list_automation_runs",
    "save_automation_rule",
    "create_automation_run_record",
    "finish_automation_run",
    "execute_automation_rule",
    "create_execution_plan",
    "get_execution_plan",
    "update_plan_status",
    "claim_next_execution_plan",
    "update_plan_step",
    "run_execution_plan",
    "AutomationRuleRequest",
    "ExecutionPlanRequest",
    "get_automations",
    "get_automation_runs",
    "create_automation",
    "update_automation",
    "delete_automation",
    "run_automation",
    "list_plans",
    "create_plan",
    "get_plan",
    "start_plan",
    "takeover_plan",
]
