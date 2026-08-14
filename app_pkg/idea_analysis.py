"""想法分析领域。

拆自 app.py（2026-08-14 第十七批）。包含: 想法会话/假设与验证/访谈/决策对比/
证据包/自动跟进提醒。仍在 app.py 的领域函数经 _app_call 运行时转发。
"""
import asyncio
import httpx
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .agent_platform import agent_result_contract
from .agent_runs import (
    add_agent_run_event,
    create_agent_run_record,
    get_agent_run,
    update_agent_run_record,
)
from .core import OUTPUTS_DIR, clip, decode_json_column, log, now_iso
from .db import db_connection
from .knowledge import write_knowledge_note
from .projects import agent_display_name
from .evidence import evidence_bundle_payload
from .instance import app
from .llm import call_llm, llm_settings
from .notifications import create_notification_record


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


class IdeaAnalysisChatRequest(BaseModel):
    session_id: str = Field(default="", max_length=80)
    message: str = Field(min_length=1, max_length=8_000)
    stream: bool = Field(default=False, description="true 时返回 SSE 流式输出")

def idea_session_row(row: sqlite3.Row) -> dict[str, Any]:
    session = {key: row[key] for key in row.keys()}
    session["summary"] = decode_json_column(session.pop("summary_json", "{}"))
    return session


def create_idea_session(title: str = "未命名想法") -> dict[str, Any]:
    session_id = uuid.uuid4().hex[:12]
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            "INSERT INTO idea_sessions (id, title, status, summary_json, created_at, updated_at) VALUES (?, ?, 'active', '{}', ?, ?)",
            (session_id, clip(title.strip() or "未命名想法", 120), timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM idea_sessions WHERE id = ?", (session_id,)).fetchone()
        return _app_call('idea_session_row', row)
    finally:
        connection.close()


def get_idea_session(session_id: str) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM idea_sessions WHERE id = ?", (session_id,)).fetchone()
        return _app_call('idea_session_row', row) if row else None
    finally:
        connection.close()


def list_idea_sessions(limit: int = 20) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM idea_sessions ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 100)),)
        ).fetchall()
        return [_app_call('idea_session_row', row) for row in rows]
    finally:
        connection.close()


def list_idea_messages(session_id: str, limit: int = 20) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT id, session_id, role, content, created_at FROM idea_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, max(1, min(limit, 100))),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]
    finally:
        connection.close()


def add_idea_message(session_id: str, role: str, content: str) -> dict[str, Any]:
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO idea_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, clip(content, 20_000), timestamp),
        )
        connection.execute("UPDATE idea_sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id))
        connection.commit()
        row = connection.execute("SELECT id, session_id, role, content, created_at FROM idea_messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    finally:
        connection.close()


def idea_hypothesis_row(row: sqlite3.Row) -> dict[str, Any]:
    hypothesis = {key: row[key] for key in row.keys()}
    hypothesis["evidence"] = decode_json_column(hypothesis.pop("evidence_json", "{}"))
    return hypothesis


def idea_validation_task_row(row: sqlite3.Row) -> dict[str, Any]:
    task = {key: row[key] for key in row.keys()}
    task["result"] = decode_json_column(task.pop("result_json", "{}"))
    return task


def idea_decision_row(row: sqlite3.Row) -> dict[str, Any]:
    decision = {key: row[key] for key in row.keys()}
    decision["evidence"] = decode_json_column(decision.pop("evidence_json", "{}"))
    return decision


def list_idea_hypotheses(session_id: str, plan_version: int | None = None) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        if plan_version is None:
            rows = connection.execute("SELECT * FROM idea_hypotheses WHERE session_id = ? ORDER BY id ASC", (session_id,)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM idea_hypotheses WHERE session_id = ? AND plan_version = ? ORDER BY id ASC", (session_id, int(plan_version))).fetchall()
        return [_app_call('idea_hypothesis_row', row) for row in rows]
    finally:
        connection.close()


def list_idea_validation_tasks(session_id: str, plan_version: int | None = None) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        if plan_version is None:
            rows = connection.execute("SELECT * FROM idea_validation_tasks WHERE session_id = ? ORDER BY due_at ASC, id ASC", (session_id,)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM idea_validation_tasks WHERE session_id = ? AND plan_version = ? ORDER BY due_at ASC, id ASC", (session_id, int(plan_version))).fetchall()
        return [_app_call('idea_validation_task_row', row) for row in rows]
    finally:
        connection.close()


def list_idea_decisions(session_id: str) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT * FROM idea_decisions WHERE session_id = ? ORDER BY version DESC", (session_id,)).fetchall()
        return [_app_call('idea_decision_row', row) for row in rows]
    finally:
        connection.close()


def idea_validation_plan(session_id: str) -> dict[str, Any]:
    decisions = _app_call('list_idea_decisions', session_id)
    latest_version = int(decisions[0]["version"]) if decisions else 0
    all_hypotheses = _app_call('list_idea_hypotheses', session_id)
    all_tasks = _app_call('list_idea_validation_tasks', session_id)
    latest_hypotheses = _app_call('list_idea_hypotheses', session_id, latest_version) if latest_version else all_hypotheses
    latest_tasks = _app_call('list_idea_validation_tasks', session_id, latest_version) if latest_version else all_tasks
    if latest_version and not latest_hypotheses and all_hypotheses:
        latest_hypotheses = all_hypotheses
    if latest_version and not latest_tasks and all_tasks:
        latest_tasks = all_tasks
    return {
        "session_id": session_id,
        "version": latest_version,
        "hypotheses": latest_hypotheses,
        "tasks": latest_tasks,
        "decisions": decisions,
        "history": {"hypotheses": all_hypotheses, "tasks": all_tasks},
    }


def create_idea_hypothesis_record(
    *, session_id: str, plan_version: int = 0, statement: str, category: str = "需求", priority: str = "normal",
    evidence: dict[str, Any] | None = None, success_metric: str = "", stop_condition: str = ""
) -> dict[str, Any]:
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            """INSERT INTO idea_hypotheses
            (session_id, plan_version, statement, category, priority, status, evidence_json, success_metric, stop_condition, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'unverified', ?, ?, ?, ?, ?)""",
            (session_id, max(0, int(plan_version)), clip(statement, 1_000), clip(category, 80) or "需求", priority if priority in {"urgent", "high", "normal", "low"} else "normal", json.dumps(evidence or {}, ensure_ascii=False), clip(success_metric, 500), clip(stop_condition, 500), timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM idea_hypotheses WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _app_call('idea_hypothesis_row', row)
    finally:
        connection.close()


def create_idea_validation_task_record(
    *, session_id: str, hypothesis_id: int, plan_version: int = 0, title: str, task_type: str = "interview", due_at: str = "",
    success_metric: str = "", acceptance: str = ""
) -> dict[str, Any]:
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            """INSERT INTO idea_validation_tasks
            (session_id, hypothesis_id, plan_version, title, task_type, status, due_at, success_metric, acceptance, work_item_id, result_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, 0, '{}', ?, ?)""",
            (session_id, int(hypothesis_id), max(0, int(plan_version)), clip(title, 240), clip(task_type, 80) or "验证", clip(due_at, 80), clip(success_metric, 500), clip(acceptance, 700), timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM idea_validation_tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _app_call('idea_validation_task_row', row)
    finally:
        connection.close()


def attach_idea_task_work_item(task_id: int, work_item_id: int) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        connection.execute("UPDATE idea_validation_tasks SET work_item_id = ?, updated_at = ? WHERE id = ?", (int(work_item_id), now_iso(), int(task_id)))
        connection.commit()
        row = connection.execute("SELECT * FROM idea_validation_tasks WHERE id = ?", (int(task_id),)).fetchone()
        return _app_call('idea_validation_task_row', row) if row else None
    finally:
        connection.close()


def create_idea_decision_record(
    *, session_id: str, verdict: str, rationale: str, continue_if: str = "", stop_if: str = "", evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    connection = db_connection()
    try:
        version_row = connection.execute("SELECT COALESCE(MAX(version), 0) AS version FROM idea_decisions WHERE session_id = ?", (session_id,)).fetchone()
        version = int(version_row["version"] or 0) + 1
        cursor = connection.execute(
            """INSERT INTO idea_decisions
            (session_id, version, verdict, rationale, continue_if, stop_if, evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, version, clip(verdict, 80) or "先验证", clip(rationale, 1_500), clip(continue_if, 700), clip(stop_if, 700), json.dumps(evidence or {}, ensure_ascii=False), now_iso()),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM idea_decisions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _app_call('idea_decision_row', row)
    finally:
        connection.close()


def update_idea_session_summary(session_id: str, summary: dict[str, Any]) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        connection.execute(
            "UPDATE idea_sessions SET summary_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), now_iso(), session_id),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM idea_sessions WHERE id = ?", (session_id,)).fetchone()
        return _app_call('idea_session_row', row) if row else None
    finally:
        connection.close()


def idea_opportunity_work_items(limit: int = 30) -> list[dict[str, Any]]:
    """Return local opportunity handoffs waiting for the Venture Agent."""
    # kind 不止 opportunity：收件箱路由到想法分析的工作项是 idea_review
    # （aihot/cid 才是 opportunity）。只认 opportunity 会把收件箱来的交接
    # 全部藏掉——用户从收件箱转给想法分析的任务，在想法分析页永远看不见。
    items = [
        item
        for item in _app_call('list_work_items', "all", "idea-analysis")
        if item.get("source_project") in {"aihot", "cid-dashboard", "inbox"}
        and item.get("kind") in {"opportunity", "idea_review", "idea_followup"}
        and "idea-analysis" in {part.strip() for part in str(item.get("target_project", "")).split(",") if part.strip()}
    ]
    return items[: max(1, min(limit, 100))]

async def run_idea_agent_turn(
    *,
    run: dict[str, Any],
    session: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Run the idea-analysis Agent while preserving its domain-specific session tables."""
    update_agent_run_record(run["id"], status="running", error="")
    add_agent_run_event(run["id"], "started", "想法分析 Agent 开始整理假设和验证路径。")
    system = (
        "你是工作台中的想法分析 Agent，负责和用户一起判断一个奇怪想法是否值得做。"
        "你不是一上来就泼冷水，也不是无条件鼓励；必须通过追问和证据逐步收敛。"
        "每轮都尽量围绕：目标用户与痛点、现有替代方案、付费或价值、获客路径、竞争壁垒、实现成本、合规风险。"
        "当信息不足时先提出不超过 3 个关键问题；当已经足够判断时，输出：\n"
        "1. 结论：值得做 / 先验证 / 暂不建议；\n"
        "2. 依据：事实与假设分开；\n"
        "3. 最小验证：7 天内能完成的动作、目标用户、成功指标；\n"
        "4. 风险与下一步。\n"
        "不要假装做过外部市场调研；没有证据就标注待验证。使用简体中文，像一个务实的产品合伙人。"
    )
    try:
        history = _app_call('list_idea_messages', session["id"], limit=12)
        add_agent_run_event(run["id"], "llm_started", "正在调用全局 LLM 做想法分析。")
        answer = await _app_call('call_llm', 
            [{"role": "system", "content": system}] + [{"role": item["role"], "content": item["content"]} for item in history],
            max_tokens=4000,
            temperature=0.3,
        )
        add_agent_run_event(run["id"], "llm_succeeded", "想法分析已返回。", level="success")
        result_contract = agent_result_contract(
            "idea-analysis",
            answer,
            source_refs=[{"type": "idea_session", "id": session["id"], "title": session.get("title", "未命名想法"), "updated_at": session.get("updated_at", "")}],
            data_as_of=session.get("updated_at", ""),
            run_id=run["id"],
            session_id=session["id"],
        )
        assistant_message = _app_call('add_idea_message', session["id"], "assistant", answer)
        verdict_match = re.search(r"(值得做|先验证|暂不建议)", answer)
        summary = {
            **(session.get("summary") if isinstance(session.get("summary"), dict) else {}),
            "verdict": verdict_match.group(1) if verdict_match else "继续澄清",
            "last_answer": clip(answer, 1000),
            "last_result_contract": result_contract,
            "last_run_id": run["id"],
        }
        session = _app_call('update_idea_session_summary', session["id"], summary) or session
        result = {"answer": answer, "session_id": session["id"], "message_id": assistant_message.get("id"), "verdict": summary["verdict"], "result_contract": result_contract}
        updated_run = update_agent_run_record(run["id"], status="succeeded", result=result, error="") or run
        add_agent_run_event(run["id"], "succeeded", "想法分析 Agent 本轮完成。", level="success")
        return {
            "run": updated_run,
            "session": session,
            "message": assistant_message,
            "result_contract": result_contract,
            "messages": _app_call('list_idea_messages', session["id"], limit=100),
            "agent": _app_call('agent_detail', "idea-analysis", llm_ready=True),
        }
    except httpx.HTTPStatusError as exc:
        error = f"上游返回 {exc.response.status_code}：{clip(exc.response.text, 500)}"
        update_agent_run_record(run["id"], status="failed", error=error)
        add_agent_run_event(run["id"], "failed", f"想法分析 Agent 调用失败：{error}", level="error")
        raise HTTPException(502, f"想法分析 Agent 调用失败：{error}") from exc
    except Exception as exc:
        error = str(exc)
        update_agent_run_record(run["id"], status="failed", error=str(exc))
        add_agent_run_event(run["id"], "failed", f"想法分析 Agent 执行失败：{error}", level="error")
        raise HTTPException(502, f"想法分析 Agent 调用失败：{error}") from exc


def parse_idea_validation_plan(answer: str, session: dict[str, Any]) -> dict[str, Any]:
    """Parse a structured plan while keeping a useful fallback for plain LLM output."""
    candidate = str(answer or "").strip()
    parsed: dict[str, Any] = {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    json_text = fenced.group(1) if fenced else candidate
    if not fenced:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            json_text = candidate[start : end + 1]
    try:
        loaded = json.loads(json_text)
        parsed = loaded if isinstance(loaded, dict) else {}
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    raw_hypotheses = parsed.get("hypotheses") if isinstance(parsed.get("hypotheses"), list) else []
    hypotheses = []
    for raw in raw_hypotheses[:8]:
        if not isinstance(raw, dict):
            continue
        statement = str(raw.get("statement") or raw.get("hypothesis") or "").strip()
        if not statement:
            continue
        hypotheses.append(
            {
                "statement": clip(statement, 1_000),
                "category": clip(str(raw.get("category") or "需求"), 80),
                "priority": str(raw.get("priority") or "normal"),
                "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {"status": "待验证", "note": str(raw.get("evidence") or "")},
                "success_metric": clip(str(raw.get("success_metric") or raw.get("metric") or ""), 500),
                "stop_condition": clip(str(raw.get("stop_condition") or raw.get("stop_if") or ""), 500),
            }
        )
    if not hypotheses:
        hypotheses = [
            {
                "statement": clip(f"目标用户愿意为“{session.get('title', '这个想法')}”投入时间或金钱。", 1_000),
                "category": "付费",
                "priority": "high",
                "evidence": {"status": "待验证", "note": "LLM 未返回结构化假设，需先做用户访谈。"},
                "success_metric": "至少访谈 5 位目标用户，其中 2 位明确表达当前痛点并愿意尝试下一步",
                "stop_condition": "连续 5 位目标用户都表示没有痛点或现有替代已足够",
            }
        ]
    raw_tasks = parsed.get("tasks") if isinstance(parsed.get("tasks"), list) else []
    tasks = []
    for raw in raw_tasks[:12]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("action") or "").strip()
        if not title:
            continue
        try:
            due_in_days = max(1, min(int(raw.get("due_in_days") or raw.get("day") or 1), 30))
        except (TypeError, ValueError):
            due_in_days = 1
        try:
            hypothesis_index = max(0, min(int(raw.get("hypothesis_index") or raw.get("hypothesis") or 0), len(hypotheses) - 1))
        except (TypeError, ValueError):
            hypothesis_index = 0
        tasks.append(
            {
                "title": clip(title, 240),
                "task_type": clip(str(raw.get("task_type") or raw.get("type") or "访谈"), 80),
                "due_in_days": due_in_days,
                "hypothesis_index": hypothesis_index,
                "success_metric": clip(str(raw.get("success_metric") or raw.get("metric") or hypotheses[hypothesis_index].get("success_metric") or ""), 500),
                "acceptance": clip(str(raw.get("acceptance") or raw.get("success_criteria") or "完成后记录事实、人数和原话，不用主观感觉代替结果"), 700),
            }
        )
    if not tasks:
        tasks = [
            {
                "title": "访谈 5 位目标用户，记录当前替代方案、痛点频率和付费意愿",
                "task_type": "访谈",
                "due_in_days": 7,
                "hypothesis_index": 0,
                "success_metric": hypotheses[0]["success_metric"],
                "acceptance": "保存受访者类型、原话、当前解决方式和是否愿意下一步尝试",
            }
        ]
    decision = parsed.get("decision") if isinstance(parsed.get("decision"), dict) else {}
    verdict = str(decision.get("verdict") or parsed.get("verdict") or session.get("summary", {}).get("verdict") or "先验证")
    if verdict not in {"值得做", "先验证", "暂不建议"}:
        verdict = "先验证"
    return {
        "title": clip(str(parsed.get("title") or session.get("title") or "未命名想法"), 120),
        "summary": clip(str(parsed.get("summary") or parsed.get("rationale") or "先用低成本验证关键假设，再决定是否投入开发。"), 1_500),
        "hypotheses": hypotheses,
        "tasks": tasks,
        "decision": {
            "verdict": verdict,
            "rationale": clip(str(decision.get("rationale") or parsed.get("rationale") or "关键事实仍不足，先执行最小验证。"), 1_500),
            "continue_if": clip(str(decision.get("continue_if") or parsed.get("continue_if") or "达到至少一个成功指标，并且用户愿意继续参与。"), 700),
            "stop_if": clip(str(decision.get("stop_if") or parsed.get("stop_if") or "连续验证没有需求信号，或成本明显高于可获得价值。"), 700),
        },
        "raw_answer": clip(candidate, 8_000),
    }


async def generate_idea_validation_plan(
    session_id: str,
    *,
    trigger: str = "manual",
    parent_run_id: str = "",
    attempt: int = 1,
    max_attempts: int = 2,
) -> dict[str, Any]:
    if not _app_call('llm_settings', )["configured"]:
        raise HTTPException(503, "请先在工作台配置全局 LLM，才能生成验证工作台")
    session = _app_call('get_idea_session', session_id)
    if not session:
        raise HTTPException(404, "想法会话不存在")
    messages = _app_call('list_idea_messages', session_id, limit=16)
    if not messages:
        raise HTTPException(409, "这个想法还没有对话内容，先描述想法再生成验证工作台")
    run = create_agent_run_record(
        project_id="idea-analysis",
        session_id=session_id,
        parent_run_id=parent_run_id,
        kind="idea_plan",
        title=f"生成验证工作台：{clip(session.get('title', '未命名想法'), 100)}",
        request={"session_id": session_id, "trigger": trigger, "message_count": len(messages)},
        max_attempts=max_attempts,
        attempt=attempt,
    )
    update_agent_run_record(run["id"], status="running", error="")
    add_agent_run_event(run["id"], "started", "想法分析 Agent 开始拆分假设、验证任务和决策条件。")
    conversation = "\n\n".join(f"{item['role']}: {clip(item['content'], 4_000)}" for item in messages)
    system = (
        "你是一个务实的创业验证 Agent。只基于对话内容建立可执行验证工作台，不把推测当事实。"
        "必须返回严格 JSON，不要 Markdown 包裹，字段为：title、summary、verdict（值得做/先验证/暂不建议）、"
        "hypotheses（数组，每项含 statement/category/priority/evidence/success_metric/stop_condition）、"
        "tasks（数组，每项含 title/task_type/due_in_days/hypothesis_index/success_metric/acceptance）、"
        "decision（含 verdict/rationale/continue_if/stop_if）。"
        "最多 8 个假设、12 个任务；任务应能在 7 天内由一个人完成，优先访谈、落地页、竞品核查或小规模手工交付。"
    )
    try:
        add_agent_run_event(run["id"], "llm_started", "正在调用全局 LLM 生成结构化验证计划。")
        answer = await _app_call('call_llm', 
            [{"role": "system", "content": system}, {"role": "user", "content": f"想法会话：\n{conversation}"}],
            max_tokens=2600,
            temperature=0.2,
        )
        plan = _app_call('parse_idea_validation_plan', answer, session)
        decision = _app_call('create_idea_decision_record', 
            session_id=session_id,
            verdict=plan["decision"]["verdict"],
            rationale=plan["decision"]["rationale"],
            continue_if=plan["decision"]["continue_if"],
            stop_if=plan["decision"]["stop_if"],
            evidence={"trigger": trigger, "run_id": run["id"], "raw_summary": plan["summary"]},
        )
        hypotheses = []
        plan_relation_ids: list[str] = []
        plan_work_item_ids: list[str] = []
        for item in plan["hypotheses"]:
            hypothesis = _app_call('create_idea_hypothesis_record', 
                session_id=session_id,
                plan_version=decision["version"],
                statement=item["statement"],
                category=item["category"],
                priority=item["priority"],
                evidence=item["evidence"],
                success_metric=item["success_metric"],
                stop_condition=item["stop_condition"],
            )
            hypotheses.append(hypothesis)
            hypothesis_relation = _app_call('create_relation_record', 
                from_type="idea_session", from_id=session_id, to_type="idea_hypothesis", to_id=str(hypothesis["id"]),
                relation_type="contains_hypothesis", metadata={"plan_version": decision["version"], "run_id": run["id"]},
            )
            plan_relation_ids.append(str(hypothesis_relation.get("id")))
        tasks = []
        for task_data in plan["tasks"]:
            hypothesis_index = max(0, min(int(task_data.get("hypothesis_index", 0)), len(hypotheses) - 1))
            hypothesis = hypotheses[hypothesis_index]
            due_at = (datetime.now(timezone.utc) + timedelta(days=int(task_data.get("due_in_days", 1)))).date().isoformat()
            task = _app_call('create_idea_validation_task_record', 
                session_id=session_id,
                hypothesis_id=hypothesis["id"],
                plan_version=decision["version"],
                title=task_data["title"],
                task_type=task_data["task_type"],
                due_at=due_at,
                success_metric=task_data["success_metric"],
                acceptance=task_data["acceptance"],
            )
            work_item = _app_call('create_work_item_record', 
                title=f"验证任务：{task_data['title']}",
                description=(
                    f"想法：{session.get('title', '未命名想法')}\n"
                    f"假设：{hypothesis['statement']}\n"
                    f"成功指标：{task_data['success_metric'] or '待补充'}\n"
                    f"验收记录：{task_data['acceptance']}\n"
                    f"截止：{due_at}"
                ),
                kind="validation_task",
                status="open",
                priority=hypothesis.get("priority", "normal"),
                source_project="idea-analysis",
                target_project="inbox",
                metadata={"idea_session_id": session_id, "hypothesis_id": hypothesis["id"], "validation_task_id": task["id"], "plan_version": decision["version"], "run_id": run["id"]},
            )
            task = _app_call('attach_idea_task_work_item', task["id"], work_item["id"]) or task
            validation_relation = _app_call('create_relation_record', 
                from_type="idea_hypothesis", from_id=str(hypothesis["id"]), to_type="work_item", to_id=str(work_item["id"]),
                relation_type="hypothesis_to_validation", metadata={"idea_session_id": session_id, "task_id": task["id"], "plan_version": decision["version"]},
            )
            plan_relation_ids.append(str(validation_relation.get("id")))
            plan_work_item_ids.append(str(work_item["id"]))
            tasks.append({**task, "work_item": work_item})
        summary = {
            **(session.get("summary") if isinstance(session.get("summary"), dict) else {}),
            "verdict": decision["verdict"],
            "plan_version": decision["version"],
            "last_plan_run_id": run["id"],
            "hypothesis_count": len(hypotheses),
            "validation_task_count": len(tasks),
        }
        session = _app_call('update_idea_session_summary', session_id, summary) or session
        plan_contract = agent_result_contract(
            "idea-analysis",
            plan["raw_answer"],
            source_refs=[{"type": "idea_session", "id": session_id, "title": session.get("title", "未命名想法"), "updated_at": session.get("updated_at", "")}],
            data_as_of=session.get("updated_at", ""),
            work_item_ids=plan_work_item_ids,
            relation_ids=plan_relation_ids,
            run_id=run["id"],
            session_id=session_id,
        )
        plan_result = {"session": session, "plan": _app_call('idea_validation_plan', session_id), "decision": decision, "tasks": tasks, "answer": plan["raw_answer"], "result_contract": plan_contract}
        updated_run = update_agent_run_record(run["id"], status="succeeded", result={"decision_id": decision["id"], "hypothesis_count": len(hypotheses), "task_count": len(tasks), "plan_version": decision["version"], "result_contract": plan_contract}, error="") or run
        add_agent_run_event(run["id"], "succeeded", f"验证工作台已生成：{len(hypotheses)} 个假设、{len(tasks)} 个任务。", level="success", metadata={"plan_version": decision["version"]})
        notification = create_notification_record(
            title="想法分析已生成验证工作台",
            body=f"{session.get('title', '未命名想法')} · v{decision['version']} · {len(tasks)} 个验证任务已交给收件箱",
            project_id="idea-analysis",
            kind="validation_plan",
            level="info",
            href="/projects/idea-analysis",
            event_key=f"idea-plan:{session_id}:{decision['version']}",
            dedupe_seconds=0,
        )
        return {**plan_result, "run": updated_run, "notification": notification}
    except httpx.HTTPStatusError as exc:
        error = f"上游返回 {exc.response.status_code}：{clip(exc.response.text, 500)}"
        update_agent_run_record(run["id"], status="failed", error=error)
        add_agent_run_event(run["id"], "failed", f"验证工作台生成失败：{error}", level="error")
        raise HTTPException(502, f"验证工作台生成失败：{error}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        error = str(exc)
        update_agent_run_record(run["id"], status="failed", error=error)
        add_agent_run_event(run["id"], "failed", f"验证工作台生成失败：{error}", level="error")
        raise HTTPException(502, f"验证工作台生成失败：{error}") from exc

@app.get("/api/idea-analysis/sessions")
def get_idea_analysis_sessions() -> dict[str, Any]:
    return {"sessions": _app_call('list_idea_sessions', limit=50)}


@app.get("/api/idea-analysis/followups")
def get_idea_followups(days: int = 3) -> dict[str, Any]:
    return _app_call('idea_followups_payload', days)


@app.post("/api/idea-analysis/reminders/run")
def run_idea_followup_reminders(days: int = 3) -> dict[str, Any]:
    payload = _app_call('idea_followups_payload', days)
    actionable = [task for task in payload["tasks"] if task.get("bucket") in {"overdue", "due_soon"}]
    notifications = []
    for task in actionable:
        bucket_label = "已逾期" if task.get("bucket") == "overdue" else "即将到期"
        notifications.append(create_notification_record(
            title=f"验证任务{bucket_label}：{clip(str(task.get('title') or '未命名任务'), 80)}",
            body=f"{task.get('session_title') or '未命名想法'} · 截止 {task.get('due_at') or '未设置'}；请记录证据或调整决策。",
            project_id="idea-analysis",
            kind="idea_followup",
            level="critical" if task.get("bucket") == "overdue" else "warning",
            href="/projects/idea-analysis",
            event_key=f"idea-followup:{task.get('id')}:{task.get('due_at')}",
            dedupe_seconds=86_400,
        ))
    return {"ok": True, "matched": len(actionable), "notifications": notifications, "followups": payload}


@app.get("/api/idea-analysis/opportunities")
def get_idea_analysis_opportunities() -> dict[str, Any]:
    items = _app_call('idea_opportunity_work_items', limit=50)
    return {"items": items, "count": len(items), "agent": _app_call('agent_detail', "idea-analysis", llm_ready=bool(_app_call('llm_settings', )["configured"]))}


@app.post("/api/idea-analysis/opportunities/{item_id}/run")
async def run_idea_analysis_opportunity(item_id: int) -> dict[str, Any]:
    if not _app_call('llm_settings', )["configured"]:
        raise HTTPException(503, "请先在工作台配置全局 LLM，想法分析 Agent 才能执行机会验证")
    item = await asyncio.to_thread(_app_call, 'get_work_item_record', item_id)
    opportunity_ids = {candidate.get("id") for candidate in _app_call('idea_opportunity_work_items', limit=100)}
    if not item or item.get("id") not in opportunity_ids:
        raise HTTPException(404, "想法分析没有找到这条机会交接")
    if item.get("status") == "running":
        raise HTTPException(409, "这条机会正在分析中")
    if item.get("status") in {"done", "archived"}:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        raise HTTPException(409, f"这条机会已经分析完成{('，会话：' + str(result.get('idea_session_id'))) if result.get('idea_session_id') else ''}")

    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    signal = metadata.get("signal") if isinstance(metadata.get("signal"), dict) else {}
    source_label = item.get("source_agent_name") or agent_display_name(item.get("source_project", "aihot"))
    handoff = (
        "这是一个来自其他项目的已确认机会交接，请把它作为想法分析 Agent 的正式输入。\n\n"
        f"来源 Agent：{source_label}\n"
        f"机会任务：{item.get('title', '未命名机会')}\n"
        f"任务要求：{item.get('description', '')}\n\n"
        f"热点/项目标题：{signal.get('title', '')}\n"
        f"来源：{signal.get('source', '')}\n"
        f"原文：{signal.get('link', '')}\n"
        f"摘要：{signal.get('description', '')}\n\n"
        "请先给出结构化初判：目标用户、核心痛点、现有替代、关键假设、证据缺口、7 天验证动作、成功指标、停止条件和下一步。"
    )
    session = _app_call('create_idea_session', f"验证：{clip(str(signal.get('title') or item.get('title') or '未命名机会'), 90)}")
    source_artifact_id = metadata.get("artifact_id")
    source_summary = {
        "source_work_item_id": item_id,
        "source_project": item.get("source_project", ""),
        "source_artifact_id": source_artifact_id,
        "source_title": signal.get("title") or item.get("title", ""),
    }
    session = _app_call('update_idea_session_summary', session["id"], source_summary) or session
    await asyncio.to_thread(_app_call, 'create_relation_record', 
        from_type="work_item", from_id=str(item_id), to_type="idea_session", to_id=session["id"],
        relation_type="opportunity_to_session", metadata={"source_project": item.get("source_project", ""), "source_artifact_id": source_artifact_id or ""},
    )
    if source_artifact_id:
        await asyncio.to_thread(_app_call, 'create_relation_record', 
            from_type="artifact", from_id=str(source_artifact_id), to_type="idea_session", to_id=session["id"],
            relation_type="opportunity_evidence", metadata={"source_project": item.get("source_project", ""), "work_item_id": item_id},
        )
    await asyncio.to_thread(_app_call, 'add_idea_message', session["id"], "user", handoff)
    run = await asyncio.to_thread(_app_call, 'create_agent_run_record', 
        project_id="idea-analysis",
        session_id=session["id"],
        kind="opportunity_validation",
        title=clip(item.get("title", "热点机会验证"), 180),
        request={"work_item_id": item_id, "session_id": session["id"], "source_project": item.get("source_project", ""), "message": handoff},
        max_attempts=2,
    )
    await asyncio.to_thread(_app_call, 'update_work_item_record', item_id, {"status": "running", "claimed_at": now_iso(), "claimed_run_id": run["id"], "last_error": ""})
    try:
        result = await _app_call('run_idea_agent_turn', run=run, session=session, message=handoff)
        relation = await asyncio.to_thread(_app_call, 'create_relation_record', 
            from_type="work_item",
            from_id=str(item_id),
            to_type="idea_session",
            to_id=session["id"],
            relation_type="opportunity_to_validation",
            metadata={"source_project": item.get("source_project", ""), "agent_run_id": run["id"]},
        )
        run_relation = await asyncio.to_thread(_app_call, 'create_relation_record', 
            from_type="work_item",
            from_id=str(item_id),
            to_type="agent_run",
            to_id=run["id"],
            relation_type="processed_by",
            metadata={"project_id": "idea-analysis", "kind": "opportunity_validation"},
        )
        updated_item = _app_call('update_work_item_record', 
            item_id,
            {
                "status": "done",
                "result_json": json.dumps({"idea_session_id": session["id"], "agent_run_id": run["id"], "answer": result.get("message", {}).get("content", "")}, ensure_ascii=False),
                "completed_at": now_iso(),
                "last_error": "",
            },
        ) or item
        notification = await asyncio.to_thread(_app_call, 'create_notification_record', 
            title="想法分析已完成机会初判",
            body=f"{clip(item.get('title', '热点机会'), 180)} · 点击想法分析项目查看验证计划",
            project_id="idea-analysis",
            kind="opportunity_result",
            level="success",
            href="/projects/idea-analysis",
            event_key=f"idea-opportunity-run:{item_id}:{run['id']}",
            dedupe_seconds=0,
        )
        return {**result, "ok": True, "work_item": updated_item, "relation": relation, "run_relation": run_relation, "notification": notification}
    except HTTPException as exc:
        await asyncio.to_thread(_app_call, 'update_work_item_record', item_id, {"status": "failed", "completed_at": now_iso(), "last_error": str(exc.detail)})
        raise
    except Exception as exc:
        await asyncio.to_thread(_app_call, 'update_work_item_record', item_id, {"status": "failed", "completed_at": now_iso(), "last_error": str(exc)})
        raise HTTPException(502, f"机会验证执行失败：{exc}") from exc


@app.get("/api/idea-analysis/sessions/{session_id}")
def get_idea_analysis_session(session_id: str) -> dict[str, Any]:
    session = _app_call('get_idea_session', session_id)
    if not session:
        raise HTTPException(404, "想法会话不存在")
    return {"session": session, "messages": _app_call('list_idea_messages', session_id, limit=100)}


@app.get("/api/idea-analysis/sessions/{session_id}/plan")
def get_idea_analysis_plan(session_id: str) -> dict[str, Any]:
    session = _app_call('get_idea_session', session_id)
    if not session:
        raise HTTPException(404, "想法会话不存在")
    return {"session": session, "plan": _app_call('idea_validation_plan', session_id)}


@app.post("/api/idea-analysis/sessions/{session_id}/plan")
async def create_idea_analysis_plan(session_id: str) -> dict[str, Any]:
    return await _app_call('generate_idea_validation_plan', session_id, trigger="manual")


@app.post("/api/idea-analysis/chat")
async def chat_idea_analysis(request: IdeaAnalysisChatRequest) -> dict[str, Any]:
    if not _app_call('llm_settings', )["configured"]:
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM")
    session = _app_call('get_idea_session', request.session_id) if request.session_id else None
    if request.session_id and not session:
        raise HTTPException(404, "想法会话不存在")
    if not session:
        session = await asyncio.to_thread(_app_call, 'create_idea_session', request.message)
    await asyncio.to_thread(_app_call, 'add_idea_message', session["id"], "user", request.message)
    run = await asyncio.to_thread(_app_call, 'create_agent_run_record', 
        project_id="idea-analysis",
        session_id=session["id"],
        kind="idea_chat",
        title=clip(request.message, 120),
        request={"session_id": session["id"], "message": request.message},
        max_attempts=2,
    )
    if request.stream:
        async def event_gen():
            try:
                async for chunk in _app_call('stream_idea_agent_turn', run=run, session=session, message=request.message):
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': clip(str(exc), 300), 'provider': ''}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    return await _app_call('run_idea_agent_turn', run=run, session=session, message=request.message)

class IdeaEvidenceRequest(BaseModel):
    kind: str = Field(default="market", max_length=40)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=12_000)
    source: str = Field(default="", max_length=2_000)
    status: str = Field(default="unverified", pattern="^(unverified|supported|contradicted|partial)$")


class IdeaInterviewRequest(BaseModel):
    participant: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=2_000)
    answer: str = Field(min_length=1, max_length=12_000)
    source: str = Field(default="", max_length=2_000)
    status: str = Field(default="unverified", pattern="^(unverified|supported|contradicted|partial)$")


class IdeaMetricRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    value: str = Field(default="", max_length=200)
    target: str = Field(default="", max_length=200)
    status: str = Field(default="pending", pattern="^(pending|met|missed|unknown)$")
    note: str = Field(default="", max_length=2_000)


class IdeaDecisionCompareRequest(BaseModel):
    confirmed: bool = False
    verdict: str = Field(default="", max_length=40)
    rationale: str = Field(default="", max_length=2_000)


def idea_followups_payload(days: int = 3) -> dict[str, Any]:
    horizon = max(0, min(int(days or 3), 30))
    today = datetime.now(timezone.utc).date()
    deadline = today + timedelta(days=horizon)
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT t.*, s.title AS session_title FROM idea_validation_tasks t LEFT JOIN idea_sessions s ON s.id = t.session_id WHERE t.status IN ('open', 'running') ORDER BY t.due_at ASC, t.id ASC"
        ).fetchall()
    finally:
        connection.close()
    tasks = []
    for row in rows:
        task = _app_call('idea_validation_task_row', row)
        due_text = str(task.get("due_at") or "")
        try:
            due_date = datetime.fromisoformat(due_text).date()
        except ValueError:
            due_date = None
        if due_date is None:
            bucket = "unscheduled"
        elif due_date < today:
            bucket = "overdue"
        elif due_date <= deadline:
            bucket = "due_soon"
        else:
            bucket = "later"
        task["session_title"] = row["session_title"] or "未命名想法"
        task["bucket"] = bucket
        task["days_remaining"] = (due_date - today).days if due_date else None
        task["work_item"] = _app_call('get_work_item_record', int(task.get("work_item_id") or 0)) if task.get("work_item_id") else None
        tasks.append(task)
    return {
        "today": today.isoformat(),
        "horizon_days": horizon,
        "summary": {bucket: sum(1 for task in tasks if task.get("bucket") == bucket) for bucket in ("overdue", "due_soon", "later", "unscheduled")},
        "tasks": tasks,
        "policy": "只提醒未完成的验证任务，不自动替用户修改结论；逾期任务优先进入人工复盘。",
    }


def idea_session_artifacts(session_id: str, kinds: set[str] | None = None) -> list[dict[str, Any]]:
    result = []
    for artifact in _app_call('list_artifacts', "idea-analysis"):
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        if str(metadata.get("session_id") or "") == session_id and (not kinds or artifact.get("kind") in kinds):
            result.append(artifact)
    return result


@app.get("/api/idea-analysis/sessions/{session_id}/evidence")
def get_idea_evidence(session_id: str) -> dict[str, Any]:
    if not _app_call('get_idea_session', session_id):
        raise HTTPException(404, "想法会话不存在")
    return {"evidence": _app_call('idea_session_artifacts', session_id, {"idea_evidence", "idea_interview", "idea_metric"}), "decisions": _app_call('list_idea_decisions', session_id)}


@app.post("/api/idea-analysis/sessions/{session_id}/evidence")
def add_idea_evidence(session_id: str, request: IdeaEvidenceRequest) -> dict[str, Any]:
    session = _app_call('get_idea_session', session_id)
    if not session:
        raise HTTPException(404, "想法会话不存在")
    artifact = _app_call('register_artifact_safely', project_id="idea-analysis", name=f"{_app_call('safe_filename', request.title)}-{datetime.now().strftime('%Y%m%d%H%M%S')}.md", path="", kind="idea_evidence", metadata={"session_id": session_id, "evidence_kind": request.kind, "source": request.source, "status": request.status, "title": request.title})
    if artifact:
        body = f"# {request.title}\n\n状态：{request.status}\n来源：{request.source or '未提供'}\n\n{request.content}\n"
        path = OUTPUTS_DIR / f"idea-evidence-{artifact['id']}.md"
        path.write_text(body, encoding="utf-8")
        connection = db_connection()
        try:
            connection.execute("UPDATE artifacts SET path = ? WHERE id = ?", (str(path), artifact["id"]))
            connection.commit()
        finally:
            connection.close()
        artifact = _app_call('get_artifact_record', int(artifact["id"])) or artifact
        _app_call('create_relation_record', from_type="idea_session", from_id=session_id, to_type="artifact", to_id=str(artifact["id"]), relation_type="session_to_evidence", metadata={"kind": request.kind, "status": request.status})
    return {"ok": True, "artifact": artifact}


@app.post("/api/idea-analysis/sessions/{session_id}/interviews")
def add_idea_interview(session_id: str, request: IdeaInterviewRequest) -> dict[str, Any]:
    if not _app_call('get_idea_session', session_id):
        raise HTTPException(404, "想法会话不存在")
    timestamp = now_iso()
    artifact = _app_call('register_artifact_safely', 
        project_id="idea-analysis",
        name=f"访谈-{_app_call('safe_filename', request.participant)}-{datetime.now().strftime('%Y%m%d%H%M%S')}.md",
        path="",
        kind="idea_interview",
        metadata={"session_id": session_id, "participant": request.participant, "question": request.question, "source": request.source, "status": request.status, "recorded_at": timestamp},
    )
    if artifact:
        path = OUTPUTS_DIR / f"idea-interview-{artifact['id']}.md"
        path.write_text(f"# 访谈记录：{request.participant}\n\n- 记录时间：{timestamp}\n- 来源：{request.source or '未提供'}\n- 状态：{request.status}\n\n## 问题\n\n{request.question.strip()}\n\n## 原话/回答\n\n{request.answer.strip()}\n", encoding="utf-8")
        connection = db_connection()
        try:
            connection.execute("UPDATE artifacts SET path = ? WHERE id = ?", (str(path), artifact["id"]))
            connection.commit()
        finally:
            connection.close()
        artifact = _app_call('get_artifact_record', int(artifact["id"])) or artifact
        _app_call('create_relation_record', from_type="idea_session", from_id=session_id, to_type="artifact", to_id=str(artifact["id"]), relation_type="session_to_interview", metadata={"status": request.status, "participant": request.participant})
    return {"ok": True, "artifact": artifact, "message": "结构化访谈已保存，可在证据包中回放问题、回答和来源。"}


@app.post("/api/idea-analysis/sessions/{session_id}/metrics")
def add_idea_metric(session_id: str, request: IdeaMetricRequest) -> dict[str, Any]:
    if not _app_call('get_idea_session', session_id):
        raise HTTPException(404, "想法会话不存在")
    artifact = _app_call('register_artifact_safely', project_id="idea-analysis", name=f"指标-{_app_call('safe_filename', request.name)}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json", path="", kind="idea_metric", metadata={"session_id": session_id, "name": request.name, "value": request.value, "target": request.target, "status": request.status, "note": request.note})
    if artifact:
        path = OUTPUTS_DIR / f"idea-metric-{artifact['id']}.json"
        path.write_text(json.dumps({"name": request.name, "value": request.value, "target": request.target, "status": request.status, "note": request.note, "recorded_at": now_iso()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        connection = db_connection()
        try:
            connection.execute("UPDATE artifacts SET path = ? WHERE id = ?", (str(path), artifact["id"]))
            connection.commit()
        finally:
            connection.close()
        artifact = _app_call('get_artifact_record', int(artifact["id"])) or artifact
        _app_call('create_relation_record', from_type="idea_session", from_id=session_id, to_type="artifact", to_id=str(artifact["id"]), relation_type="session_to_metric", metadata={"status": request.status})
    return {"ok": True, "artifact": artifact}


@app.post("/api/idea-analysis/sessions/{session_id}/decision/compare")
def compare_idea_decision(session_id: str, request: IdeaDecisionCompareRequest) -> dict[str, Any]:
    if not _app_call('get_idea_session', session_id):
        raise HTTPException(404, "想法会话不存在")
    evidence = _app_call('idea_session_artifacts', session_id, {"idea_evidence", "idea_interview", "idea_metric"})
    supported = sum(1 for item in evidence if item.get("metadata", {}).get("status") in {"supported", "met"})
    contradicted = sum(1 for item in evidence if item.get("metadata", {}).get("status") in {"contradicted", "missed"})
    unknown = max(0, len(evidence) - supported - contradicted)
    evidence_bundle = evidence_bundle_payload([int(item["id"]) for item in evidence if str(item.get("id", "")).isdigit()], "")
    quality = evidence_bundle.get("coverage", {}).get("quality", {})
    automatic = "继续" if supported > contradicted and supported >= 2 else "暂停" if contradicted > supported else "转向/补证据"
    decision = None
    knowledge_note = None
    if request.confirmed:
        decision = _app_call('create_idea_decision_record', session_id=session_id, verdict=request.verdict.strip() or automatic, rationale=request.rationale.strip() or f"基于 {supported} 条支持证据、{contradicted} 条反证和 {unknown} 条未决证据的版本化比较。", continue_if="支持证据持续增加且关键指标达到目标。", stop_if="反证超过支持证据，或关键指标连续未达标。", evidence={"supported": supported, "contradicted": contradicted, "unknown": unknown, "automatic_suggestion": automatic})
        session = _app_call('get_idea_session', session_id) or {"title": "想法验证"}
        knowledge_note = write_knowledge_note(
            f"想法结论｜{session.get('title') or '未命名想法'}｜v{decision.get('version', 1)}",
            f"## 结论\n\n**{decision.get('verdict', automatic)}**\n\n## 判断依据\n\n{decision.get('rationale', '')}\n\n- 支持证据：{supported}\n- 反证：{contradicted}\n- 未决：{unknown}\n\n## 下一步\n\n继续条件：{decision.get('continue_if', '')}\n\n停止条件：{decision.get('stop_if', '')}\n",
            metadata={"session_id": session_id, "decision_id": decision.get("id"), "verdict": decision.get("verdict")},
            artifact_kind="idea_decision_note",
        )
        if knowledge_note.get("artifact"):
            _app_call('create_relation_record', from_type="idea_decision", from_id=str(decision.get("id")), to_type="artifact", to_id=str(knowledge_note["artifact"].get("id")), relation_type="decision_to_knowledge", metadata={"session_id": session_id})
    return {
        "comparison": {
            "supported": supported,
            "contradicted": contradicted,
            "unknown": unknown,
            "evidence_total": len(evidence),
            "support_rate": round(supported / len(evidence), 3) if evidence else None,
            "contradicted_rate": round(contradicted / len(evidence), 3) if evidence else None,
            "sample_status": "ready" if len(evidence) >= 3 else "insufficient",
            "minimum_samples": 3,
            "suggested_verdict": automatic,
            "evidence_quality": quality,
        },
        "decision": decision,
        "evidence": evidence,
        "evidence_sources": evidence_bundle.get("sources", []),
        "knowledge_note": knowledge_note,
        "policy": "继续/暂停/转向建议只基于人工记录的证据状态；来源质量、数据时间和可读性单独展示，不把数量当成可信度。",
    }

@app.get("/api/idea-analysis/sessions/{session_id}/evidence-pack")
def get_idea_evidence_pack(session_id: str) -> dict[str, Any]:
    session = _app_call('get_idea_session', session_id)
    if not session:
        raise HTTPException(404, "想法会话不存在")
    evidence = _app_call('idea_session_artifacts', session_id, {"idea_evidence", "idea_interview", "idea_metric", "idea_decision_note"})
    decision = _app_call('list_idea_decisions', session_id)
    counts = {"supported": 0, "contradicted": 0, "partial": 0, "pending": 0, "interviews": 0, "metrics": 0}
    for artifact in evidence:
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        status = str(metadata.get("status") or "pending")
        if status == "unverified":
            status = "pending"
        if status in counts:
            counts[status] += 1
        if artifact.get("kind") == "idea_interview":
            counts["interviews"] += 1
        elif artifact.get("kind") == "idea_metric":
            counts["metrics"] += 1
    source_bundle = evidence_bundle_payload([int(item["id"]) for item in evidence if str(item.get("id", "")).isdigit()], "")
    coverage = source_bundle.get("coverage", {}) if isinstance(source_bundle.get("coverage"), dict) else {}
    total = len(evidence)
    latest_decision = decision[0] if decision else None
    decision_evidence = latest_decision.get("evidence") if isinstance(latest_decision, dict) and isinstance(latest_decision.get("evidence"), dict) else {}
    summary = {
        **counts,
        "evidence_total": total,
        "support_rate": round(counts["supported"] / total, 3) if total else None,
        "contradicted_rate": round(counts["contradicted"] / total, 3) if total else None,
        "sample_status": "ready" if total >= 3 else "insufficient",
        "minimum_samples": 3,
        "latest_decision_at": latest_decision.get("created_at", "") if latest_decision else "",
        "decision_count": len(decision),
        "decision_source_coverage": {
            "available": coverage.get("available", 0),
            "requested": coverage.get("requested", 0),
            "readable": coverage.get("readable", 0),
            "quality": coverage.get("quality", {}),
            "recorded_evidence": decision_evidence,
        },
        "evidence_quality": coverage.get("quality", {}),
    }
    return {"session": session, "artifacts": evidence, "decisions": decision, "summary": summary, "evidence_sources": source_bundle.get("sources", []), "policy": "证据包按来源、状态、数据时间和可读性聚合；不会自动替用户改变继续/暂停/转向结论。样本不足时只展示趋势，不替代人工判断。"}


__all__ = [
    "IdeaAnalysisChatRequest",
    "idea_session_row",
    "create_idea_session",
    "get_idea_session",
    "list_idea_sessions",
    "list_idea_messages",
    "add_idea_message",
    "idea_hypothesis_row",
    "idea_validation_task_row",
    "idea_decision_row",
    "list_idea_hypotheses",
    "list_idea_validation_tasks",
    "list_idea_decisions",
    "idea_validation_plan",
    "create_idea_hypothesis_record",
    "create_idea_validation_task_record",
    "attach_idea_task_work_item",
    "create_idea_decision_record",
    "update_idea_session_summary",
    "idea_opportunity_work_items",
    "run_idea_agent_turn",
    "parse_idea_validation_plan",
    "generate_idea_validation_plan",
    "get_idea_analysis_sessions",
    "get_idea_followups",
    "run_idea_followup_reminders",
    "get_idea_analysis_opportunities",
    "run_idea_analysis_opportunity",
    "get_idea_analysis_session",
    "get_idea_analysis_plan",
    "create_idea_analysis_plan",
    "chat_idea_analysis",
    "IdeaEvidenceRequest",
    "IdeaInterviewRequest",
    "IdeaMetricRequest",
    "IdeaDecisionCompareRequest",
    "idea_followups_payload",
    "idea_session_artifacts",
    "get_idea_evidence",
    "add_idea_evidence",
    "add_idea_interview",
    "add_idea_metric",
    "compare_idea_decision",
    "get_idea_evidence_pack",
]
