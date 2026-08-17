"""产品作战室领域。

拆自 app.py（2026-08-14 第十七批）。包含: 项目/需求/反馈/决策/原型(prototype)/
PRD 生成/Cowart 画布。仍在 app.py 的领域函数（run_document_factory 等）经 _app_call 转发。
"""
from __future__ import annotations

import asyncio
import base64
import html as html_lib
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import urllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .core import (
    COWART_SCRIPT_NAME,
    COWART_STYLE_NAME,
    COWART_VENDOR_DIR,
    COWART_VERSION,
    DATA_DIR,
    STATIC_DIR,
    clip,
    decode_json_column,
    log,
    now_iso,
    save_json_atomic,
)
from .db import db_connection
from .instance import app
from .knowledge import write_knowledge_note
from .llm import call_llm, llm_settings
from .notifications import create_notification_record


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def _PRODUCT_PROTOTYPES_DIR() -> Path:
    """运行时读 app.PRODUCT_PROTOTYPES_DIR——测试 patch app.PRODUCT_PROTOTYPES_DIR 时生效。"""
    import app as _app

    return _app.PRODUCT_PROTOTYPES_DIR


def _OUTPUTS_DIR() -> Path:
    """运行时读 app.OUTPUTS_DIR——测试 patch app.OUTPUTS_DIR 时生效。"""
    import app as _app

    return _app.OUTPUTS_DIR


class ProductFeedbackRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    project_id: str = Field(default="", max_length=80)
    source: str = Field(default="", max_length=500)
    persona: str = Field(default="", max_length=240)
    importance: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProductFeedbackUpdateRequest(BaseModel):
    status: str | None = Field(default=None, pattern="^(new|reviewing|linked|archived)$")
    importance: str | None = Field(default=None, pattern="^(low|normal|high|urgent)$")
    linked_requirement_id: int | None = Field(default=None, ge=0)


PRODUCT_ITEM_TYPES = ("requirement", "defect")
PRODUCT_SEVERITIES = ("blocker", "major", "minor", "trivial")


class ProductRequirementRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    # 项目维度：为空表示"未归属"，保持旧数据可用而不是强制迁移。
    project_id: str = Field(default="", max_length=80)
    # 缺陷与需求走同一张表：状态机、证据关联、工作项联动完全一样，
    # 区别只在 RICE 打分（需求）和严重级别 + 复现步骤（缺陷）。
    item_type: str = Field(default="requirement", pattern="^(requirement|defect)$")
    severity: str = Field(default="", max_length=20)
    problem: str = Field(default="", max_length=20_000)
    target_user: str = Field(default="", max_length=500)
    outcome: str = Field(default="", max_length=2_000)
    scope: str = Field(default="", max_length=10_000)
    status: str = Field(default="discovering", pattern="^(discovering|review|planned|building|shipped|paused)$")
    reach: float = Field(default=1, ge=0, le=1_000_000)
    impact: float = Field(default=1, ge=0, le=5)
    confidence: float = Field(default=50, ge=0, le=100)
    effort: float = Field(default=1, gt=0, le=10_000)
    feedback_ids: list[int] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProductRequirementUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    problem: str | None = Field(default=None, max_length=20_000)
    target_user: str | None = Field(default=None, max_length=500)
    outcome: str | None = Field(default=None, max_length=2_000)
    scope: str | None = Field(default=None, max_length=10_000)
    status: str | None = Field(default=None, pattern="^(discovering|review|planned|building|shipped|paused)$")
    reach: float | None = Field(default=None, ge=0, le=1_000_000)
    impact: float | None = Field(default=None, ge=0, le=5)
    confidence: float | None = Field(default=None, ge=0, le=100)
    effort: float | None = Field(default=None, gt=0, le=10_000)


class ProductDecisionRequest(BaseModel):
    requirement_id: int = Field(default=0, ge=0)
    title: str = Field(min_length=1, max_length=240)
    decision: str = Field(min_length=1, max_length=20_000)
    rationale: str = Field(default="", max_length=20_000)
    alternatives: str = Field(default="", max_length=10_000)
    revisit_trigger: str = Field(default="", max_length=2_000)
    status: str = Field(default="decided", pattern="^(proposed|decided|revisiting|superseded)$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProductPrototypeRequest(BaseModel):
    title: str = Field(default="", max_length=240)
    force_new: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProductPrototypePublishRequest(BaseModel):
    summary: str = Field(default="发布 Cowart 画布版本", min_length=1, max_length=2_000)
    confirmed: bool = False


def _product_defect_priority(severity: str) -> str:
    """缺陷的优先级来自严重级别，而不是 RICE 分数。"""
    return {"blocker": "urgent", "major": "high", "minor": "normal", "trivial": "low"}.get(str(severity or ""), "normal")


def product_rice_score(reach: float, impact: float, confidence: float, effort: float) -> float:
    """Return an explainable RICE score; confidence is expressed as 0-100."""
    safe_effort = max(float(effort or 0), 0.01)
    return round(max(float(reach or 0), 0) * max(float(impact or 0), 0) * max(min(float(confidence or 0), 100), 0) / 100 / safe_effort, 2)


def _product_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _product_feedback_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    item["metadata"] = _app_call('_product_metadata', item.pop("metadata_json", "{}"))
    return item


def _product_requirement_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    item["metadata"] = _app_call('_product_metadata', item.pop("metadata_json", "{}"))
    item["score"] = round(float(item.get("score") or 0), 2)
    item["evidence_count"] = int(item.get("evidence_count") or 0)
    return item


def _product_decision_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    item["metadata"] = _app_call('_product_metadata', item.pop("metadata_json", "{}"))
    return item


def _product_prototype_version_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    item["metadata"] = _app_call('_product_metadata', item.pop("metadata_json", "{}"))
    return item


def _product_prototype_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    item["metadata"] = _app_call('_product_metadata', item.pop("metadata_json", "{}"))
    item["canvas_url"] = f"/projects/product-manager/prototypes/{item['id']}/cowart/"
    item["version_count"] = int(item.get("version_count") or 0)
    return item


def _product_prototype_root(prototype_id: int) -> Path:
    """Return a server-owned prototype root; browser input never selects a path."""
    return _PRODUCT_PROTOTYPES_DIR() / str(int(prototype_id))


def _product_canvas_file(prototype_id: int) -> Path:
    return _app_call('_product_prototype_root', prototype_id) / "canvas" / "cowart-canvas.json"


def _product_selection_file(prototype_id: int) -> Path:
    return _app_call('_product_prototype_root', prototype_id) / "canvas" / "cowart-selection.json"


def _product_view_state_file(prototype_id: int) -> Path:
    return _app_call('_product_prototype_root', prototype_id) / "canvas" / "cowart-view-state.json"


def list_product_prototype_versions(prototype_id: int, limit: int = 100) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM product_prototype_versions WHERE prototype_id = ? ORDER BY version DESC LIMIT ?",
            (int(prototype_id), max(1, min(int(limit), 500))),
        ).fetchall()
        return [_app_call('_product_prototype_version_row', row) for row in rows]
    finally:
        connection.close()


def list_product_prototypes(limit: int = 200) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            """SELECT product_prototypes.*, product_requirements.title AS requirement_title,
                (SELECT COUNT(*) FROM product_prototype_versions
                 WHERE product_prototype_versions.prototype_id = product_prototypes.id) AS version_count
            FROM product_prototypes
            LEFT JOIN product_requirements ON product_requirements.id = product_prototypes.requirement_id
            ORDER BY product_prototypes.updated_at DESC, product_prototypes.id DESC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [_app_call('_product_prototype_row', row) for row in rows]
    finally:
        connection.close()


def get_product_prototype(prototype_id: int, *, include_versions: bool = True) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute(
            """SELECT product_prototypes.*, product_requirements.title AS requirement_title,
                (SELECT COUNT(*) FROM product_prototype_versions
                 WHERE product_prototype_versions.prototype_id = product_prototypes.id) AS version_count
            FROM product_prototypes
            LEFT JOIN product_requirements ON product_requirements.id = product_prototypes.requirement_id
            WHERE product_prototypes.id = ?""",
            (int(prototype_id),),
        ).fetchone()
    finally:
        connection.close()
    item = _app_call('_product_prototype_row', row)
    if item is not None and include_versions:
        item["versions"] = _app_call('list_product_prototype_versions', int(prototype_id), 20)
    return item


def list_product_feedback(limit: int = 200) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM product_feedback ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [_app_call('_product_feedback_row', row) for row in rows]
    finally:
        connection.close()


def get_product_feedback(feedback_id: int) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM product_feedback WHERE id = ?", (int(feedback_id),)).fetchone()
        return _app_call('_product_feedback_row', row)
    finally:
        connection.close()


def list_product_requirements(limit: int = 200, project_id: str = "", item_type: str = "") -> list[dict[str, Any]]:
    """需求与缺陷共用一张表；project_id / item_type 为空表示不过滤。

    缺陷不参与 RICE 排序（它们的 score 恒为 0），所以先按严重级别再按更新时间排，
    否则所有缺陷会被需求压到列表最底下。
    """
    clauses: list[str] = []
    values: list[Any] = []
    if project_id:
        clauses.append("product_requirements.project_id = ?")
        values.append(project_id)
    if item_type in PRODUCT_ITEM_TYPES:
        clauses.append("product_requirements.item_type = ?")
        values.append(item_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(max(1, min(int(limit), 500)))
    connection = db_connection()
    try:
        rows = connection.execute(
            f"""SELECT product_requirements.*,
                (SELECT COUNT(*) FROM product_feedback WHERE linked_requirement_id = product_requirements.id) AS evidence_count
            FROM product_requirements {where}
            ORDER BY CASE status WHEN 'review' THEN 0 WHEN 'planned' THEN 1 WHEN 'building' THEN 2 WHEN 'discovering' THEN 3 ELSE 4 END,
                CASE severity WHEN 'blocker' THEN 0 WHEN 'major' THEN 1 WHEN 'minor' THEN 2 WHEN 'trivial' THEN 3 ELSE 4 END,
                score DESC, updated_at DESC, id DESC LIMIT ?""",
            values,
        ).fetchall()
        return [_app_call('_product_requirement_row', row) for row in rows]
    finally:
        connection.close()


def get_product_requirement(requirement_id: int) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute(
            """SELECT product_requirements.*,
                (SELECT COUNT(*) FROM product_feedback WHERE linked_requirement_id = product_requirements.id) AS evidence_count
            FROM product_requirements WHERE id = ?""",
            (int(requirement_id),),
        ).fetchone()
        return _app_call('_product_requirement_row', row)
    finally:
        connection.close()


def list_product_decisions(limit: int = 200) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            """SELECT product_decisions.*, product_requirements.title AS requirement_title
            FROM product_decisions
            LEFT JOIN product_requirements ON product_requirements.id = product_decisions.requirement_id
            ORDER BY product_decisions.updated_at DESC, product_decisions.id DESC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [_app_call('_product_decision_row', row) for row in rows]
    finally:
        connection.close()


def product_manager_summary() -> dict[str, int]:
    connection = db_connection()
    try:
        feedback = connection.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END) AS new_count FROM product_feedback"
        ).fetchone()
        requirements = connection.execute(
            """SELECT COUNT(*) AS total,
                SUM(CASE WHEN status NOT IN ('shipped', 'paused') THEN 1 ELSE 0 END) AS active_count,
                SUM(CASE WHEN status = 'review' THEN 1 ELSE 0 END) AS review_count,
                SUM(CASE WHEN status NOT IN ('shipped', 'paused') AND NOT EXISTS (
                    SELECT 1 FROM product_feedback WHERE linked_requirement_id = product_requirements.id
                ) THEN 1 ELSE 0 END) AS needs_evidence_count
            FROM product_requirements"""
        ).fetchone()
        decisions = connection.execute("SELECT COUNT(*) AS total FROM product_decisions").fetchone()
        prototypes = connection.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) AS draft_count FROM product_prototypes"
        ).fetchone()
    finally:
        connection.close()
    return {
        "feedback_total": int(feedback["total"] or 0),
        "new_feedback": int(feedback["new_count"] or 0),
        "requirements_total": int(requirements["total"] or 0),
        "active_requirements": int(requirements["active_count"] or 0),
        "needs_evidence": int(requirements["needs_evidence_count"] or 0),
        "review_pending": int(requirements["review_count"] or 0),
        "decisions_total": int(decisions["total"] or 0),
        "prototypes_total": int(prototypes["total"] or 0),
        "prototype_drafts": int(prototypes["draft_count"] or 0),
    }


def product_manager_overview(limit: int = 200, project_id: str = "") -> dict[str, Any]:
    """project_id 为空时看全部；给定时只看该项目，用于按项目维度经营。"""
    project_filter = _app_call('valid_product_project_id', project_id) if project_id else ""
    feedback = _app_call('list_product_feedback', limit)
    if project_filter:
        feedback = [item for item in feedback if str(item.get("project_id") or "") == project_filter]
    requirements = _app_call('list_product_requirements', limit, project_filter)
    decisions = _app_call('list_product_decisions', limit)
    prototypes = _app_call('list_product_prototypes', limit)
    if project_filter:
        # 决策/原型本身没有 project_id，通过所属需求归属项目；未关联需求的
        # （requirement_id=0，独立记录）在按项目过滤时不显示。
        requirement_ids = {int(item["id"]) for item in requirements}
        decisions = [item for item in decisions if int(item.get("requirement_id") or 0) in requirement_ids]
        prototypes = [item for item in prototypes if int(item.get("requirement_id") or 0) in requirement_ids]
    active = [item for item in requirements if item.get("status") not in {"shipped", "paused"}]
    needs_evidence = [item for item in active if int(item.get("evidence_count") or 0) == 0]
    review_items = [item for item in requirements if item.get("status") == "review"]
    open_defects = [item for item in active if item.get("item_type") == "defect"]
    summary = _app_call('product_manager_summary', )

    # 按项目汇总：同时维护多个产品时，最需要先看"哪个项目在冒烟"。
    projects = _app_call('list_product_projects', )
    project_titles = {str(item["id"]): str(item["name"]) for item in projects}
    by_project: dict[str, dict[str, Any]] = {
        str(item["id"]): {
            "project_id": str(item["id"]), "project_title": str(item["name"]),
            "summary": str(item.get("summary") or ""),
            "requirements": 0, "defects": 0, "blockers": 0, "needs_evidence": 0,
        }
        for item in projects
    }
    for item in _app_call('list_product_requirements', limit):
        raw_key = str(item.get("project_id") or "")
        # 已删除或从未存在的项目 id 全部并到同一个「未归属」桶，
        # 否则每个失效 id 都会自成一行、显示成一堆同名的「未归属」。
        key = raw_key if raw_key in project_titles else ""
        bucket = by_project.setdefault(key, {
            "project_id": key,
            "project_title": project_titles.get(key, "未归属"),
            "summary": "",
            "requirements": 0, "defects": 0, "blockers": 0, "needs_evidence": 0,
        })
        if item.get("item_type") == "defect":
            bucket["defects"] += 1
            if item.get("severity") == "blocker" and item.get("status") not in {"shipped", "paused"}:
                bucket["blockers"] += 1
        else:
            bucket["requirements"] += 1
        if int(item.get("evidence_count") or 0) == 0 and item.get("status") not in {"shipped", "paused"}:
            bucket["needs_evidence"] += 1

    return {
        "summary": summary,
        "feedback": feedback,
        "requirements": requirements,
        "decisions": decisions,
        "prototypes": prototypes,
        "cowart": _app_call('product_cowart_status', ),
        "projects": {
            "selected": project_filter,
            "options": [{"id": str(item["id"]), "title": str(item["name"]), "summary": str(item.get("summary") or "")} for item in projects],
            "rollup": sorted(by_project.values(), key=lambda item: (-item["blockers"], -item["defects"], -item["requirements"])),
        },
        "item_types": [
            {"id": "requirement", "label": "需求", "scoring": "RICE"},
            {"id": "defect", "label": "缺陷", "scoring": "severity"},
        ],
        "severities": [
            {"id": "blocker", "label": "阻塞"}, {"id": "major", "label": "严重"},
            {"id": "minor", "label": "一般"}, {"id": "trivial", "label": "轻微"},
        ],
        "attention": {
            "new_feedback": [item for item in feedback if item.get("status") == "new"][:6],
            "needs_evidence": needs_evidence[:6],
            "review": review_items[:6],
            "open_defects": sorted(open_defects, key=lambda item: PRODUCT_SEVERITIES.index(item.get("severity") or "minor") if (item.get("severity") in PRODUCT_SEVERITIES) else 9)[:6],
            "top_priority": sorted([item for item in active if item.get("item_type") != "defect"], key=lambda item: float(item.get("score") or 0), reverse=True)[:6],
        },
        "scoring": {
            "name": "RICE",
            "formula": "reach × impact × confidence / effort",
            "note": "分数用于排序建议，不代替产品经理决策。",
        },
    }


def create_product_feedback(request: ProductFeedbackRequest) -> dict[str, Any]:
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            """INSERT INTO product_feedback
            (content, project_id, source, persona, importance, status, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'new', ?, ?, ?)""",
            (
                request.content.strip(),
                _app_call('valid_product_project_id', request.project_id),
                request.source.strip(),
                request.persona.strip(),
                request.importance,
                json.dumps(request.metadata, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        feedback_id = int(cursor.lastrowid)
        connection.commit()
    finally:
        connection.close()
    artifact = _app_call('register_artifact_safely', 
        project_id="product-manager",
        name=f"产品反馈 #{feedback_id}",
        kind="product_feedback",
        metadata={
            "feedback_id": feedback_id,
            "content": clip(request.content.strip(), 20_000),
            "source": request.source.strip(),
            "persona": request.persona.strip(),
            "importance": request.importance,
            "data_as_of": timestamp,
        },
    )
    if artifact:
        connection = db_connection()
        try:
            connection.execute("UPDATE product_feedback SET artifact_id = ? WHERE id = ?", (artifact["id"], feedback_id))
            connection.commit()
        finally:
            connection.close()
    return _app_call('get_product_feedback', feedback_id) or {}


def _product_requirement_priority(score: float) -> str:
    if score >= 20:
        return "high"
    if score < 2:
        return "low"
    return "normal"


def _product_work_item_status(status: str) -> str:
    if status == "shipped":
        return "done"
    if status == "paused":
        return "blocked"
    return "open"


def list_product_projects(include_archived: bool = False) -> list[dict[str, Any]]:
    """用户自定义的产品项目列表。"""
    connection = db_connection()
    try:
        where = "" if include_archived else "WHERE status = 'active'"
        rows = connection.execute(
            f"""SELECT product_projects.*,
                (SELECT COUNT(*) FROM product_requirements
                 WHERE product_requirements.project_id = CAST(product_projects.id AS TEXT)
                   AND item_type = 'requirement') AS requirement_count,
                (SELECT COUNT(*) FROM product_requirements
                 WHERE product_requirements.project_id = CAST(product_projects.id AS TEXT)
                   AND item_type = 'defect' AND status NOT IN ('shipped', 'paused')) AS open_defect_count,
                (SELECT COUNT(*) FROM product_requirements
                 WHERE product_requirements.project_id = CAST(product_projects.id AS TEXT)
                   AND item_type = 'defect' AND severity = 'blocker' AND status NOT IN ('shipped', 'paused')) AS blocker_count
            FROM product_projects {where} ORDER BY updated_at DESC, id DESC"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def create_product_project(name: str, summary: str = "", color: str = "") -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise HTTPException(400, "项目名称不能为空")
    timestamp = now_iso()
    connection = db_connection()
    try:
        existing = connection.execute("SELECT id FROM product_projects WHERE name = ?", (clean_name,)).fetchone()
        if existing:
            raise HTTPException(409, f"已经有一个叫「{clean_name}」的项目")
        cursor = connection.execute(
            "INSERT INTO product_projects(name, summary, color, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (clean_name[:120], str(summary or "").strip()[:2000], str(color or "").strip()[:20], timestamp, timestamp),
        )
        connection.commit()
        project_id = int(cursor.lastrowid)
    finally:
        connection.close()
    return next((item for item in _app_call('list_product_projects', True) if int(item["id"]) == project_id), {"id": project_id, "name": clean_name})


def update_product_project(project_id: int, *, name: str | None = None, summary: str | None = None, status: str | None = None) -> dict[str, Any] | None:
    fields: list[tuple[str, Any]] = []
    if name is not None and str(name).strip():
        fields.append(("name", str(name).strip()[:120]))
    if summary is not None:
        fields.append(("summary", str(summary).strip()[:2000]))
    if status in {"active", "archived"}:
        fields.append(("status", status))
        fields.append(("archived_at", now_iso() if status == "archived" else ""))
    if not fields:
        return None
    fields.append(("updated_at", now_iso()))
    assignments = ", ".join(f"{key} = ?" for key, _ in fields)
    connection = db_connection()
    try:
        connection.execute(f"UPDATE product_projects SET {assignments} WHERE id = ?", [value for _, value in fields] + [int(project_id)])
        connection.commit()
    finally:
        connection.close()
    return next((item for item in _app_call('list_product_projects', True) if int(item["id"]) == int(project_id)), None)


def valid_product_project_id(value: str) -> str:
    """归属必须是用户自己建过的产品项目；未知一律落到未归属，不静默写脏数据。

    这里刻意不复用工作台的 15 个内置项目——那是"工作台有哪些功能模块"，
    而产品作战室要管的是"我在做哪些产品"，两者是不同的东西。
    """
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    connection = db_connection()
    try:
        row = connection.execute("SELECT id FROM product_projects WHERE CAST(id AS TEXT) = ?", (candidate,)).fetchone()
    finally:
        connection.close()
    if row:
        return candidate
    log.warning("忽略未知的产品项目归属：%s", candidate)
    return ""


def create_product_requirement(request: ProductRequirementRequest) -> dict[str, Any]:
    timestamp = now_iso()
    project_id = _app_call('valid_product_project_id', request.project_id)
    item_type = request.item_type if request.item_type in PRODUCT_ITEM_TYPES else "requirement"
    if item_type == "defect":
        # 缺陷不做 RICE：拿"触达 x 影响 / 成本"给 bug 排序没有意义，
        # 它的优先级来自严重级别。score 留 0，排序时走 severity 分支。
        severity = request.severity if request.severity in PRODUCT_SEVERITIES else "major"
        score = 0.0
    else:
        severity = ""
        score = _app_call('product_rice_score', request.reach, request.impact, request.confidence, request.effort)
    connection = db_connection()
    try:
        cursor = connection.execute(
            """INSERT INTO product_requirements
            (title, project_id, item_type, severity, problem, target_user, outcome, scope, status,
             reach, impact, confidence, effort, score, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.title.strip(), project_id, item_type, severity,
                request.problem.strip(), request.target_user.strip(), request.outcome.strip(), request.scope.strip(),
                request.status, request.reach, request.impact, request.confidence, request.effort, score,
                json.dumps(request.metadata, ensure_ascii=False), timestamp, timestamp,
            ),
        )
        requirement_id = int(cursor.lastrowid)
        connection.commit()
    finally:
        connection.close()
    # 工作项归到实际项目上，这样首页卡片和联动矩阵能按项目看到它，
    # 而不是所有需求缺陷都堆在 product-manager 一个桶里。
    item = _app_call('create_work_item_record', 
        title=request.title.strip(),
        description="\n\n".join(part for part in [request.problem.strip(), f"预期结果：{request.outcome.strip()}" if request.outcome.strip() else ""] if part),
        kind="product_defect" if item_type == "defect" else "product_requirement",
        status=_app_call('_product_work_item_status', request.status),
        priority=_app_call('_product_defect_priority', severity) if item_type == "defect" else _app_call('_product_requirement_priority', score),
        source_project="product-manager",
        target_project="product-manager",
        metadata={
            "product_requirement_id": requirement_id, "product_status": request.status,
            "item_type": item_type, "project_id": project_id,
            **({"severity": severity} if item_type == "defect" else {"rice_score": score}),
        },
    )
    connection = db_connection()
    try:
        connection.execute("UPDATE product_requirements SET work_item_id = ? WHERE id = ?", (item["id"], requirement_id))
        feedback_ids = list(dict.fromkeys(int(value) for value in request.feedback_ids if int(value) > 0))
        if feedback_ids:
            placeholders = ",".join("?" for _ in feedback_ids)
            connection.execute(
                f"UPDATE product_feedback SET linked_requirement_id = ?, status = 'linked', updated_at = ? WHERE id IN ({placeholders})",
                [requirement_id, timestamp, *feedback_ids],
            )
        connection.commit()
    finally:
        connection.close()
    for feedback_id in request.feedback_ids:
        feedback = _app_call('get_product_feedback', feedback_id)
        if not feedback or not feedback.get("artifact_id"):
            continue
        _app_call('create_relation_record', 
            from_type="artifact", from_id=str(feedback["artifact_id"]),
            to_type="work_item", to_id=str(item["id"]), relation_type="evidence_for",
            metadata={"project_id": "product-manager", "requirement_id": requirement_id, "feedback_id": feedback_id},
        )
    return _app_call('get_product_requirement', requirement_id) or {}


def update_product_feedback(feedback_id: int, request: ProductFeedbackUpdateRequest) -> dict[str, Any] | None:
    current = _app_call('get_product_feedback', feedback_id)
    if not current:
        return None
    update_values: dict[str, Any] = {}
    if request.status is not None:
        update_values["status"] = request.status
    if request.importance is not None:
        update_values["importance"] = request.importance
    if request.linked_requirement_id is not None:
        if request.linked_requirement_id and not _app_call('get_product_requirement', request.linked_requirement_id):
            raise HTTPException(404, "要关联的产品需求不存在")
        update_values["linked_requirement_id"] = request.linked_requirement_id
        if request.linked_requirement_id:
            update_values["status"] = "linked"
    if not update_values:
        return current
    updates = list(update_values.items())
    updates.append(("updated_at", now_iso()))
    connection = db_connection()
    try:
        assignments = ", ".join(f"{key} = ?" for key, _ in updates)
        connection.execute(f"UPDATE product_feedback SET {assignments} WHERE id = ?", [value for _, value in updates] + [feedback_id])
        connection.commit()
    finally:
        connection.close()
    linked_requirement_id = request.linked_requirement_id or 0
    if linked_requirement_id and current.get("artifact_id"):
        requirement = _app_call('get_product_requirement', linked_requirement_id)
        if requirement and requirement.get("work_item_id"):
            _app_call('create_relation_record', 
                from_type="artifact", from_id=str(current["artifact_id"]), to_type="work_item",
                to_id=str(requirement["work_item_id"]), relation_type="evidence_for",
                metadata={"project_id": "product-manager", "requirement_id": linked_requirement_id, "feedback_id": feedback_id},
            )
    return _app_call('get_product_feedback', feedback_id)


def update_product_requirement(requirement_id: int, request: ProductRequirementUpdateRequest) -> dict[str, Any] | None:
    current = _app_call('get_product_requirement', requirement_id)
    if not current:
        return None
    field_values = {
        "title": request.title, "problem": request.problem, "target_user": request.target_user, "outcome": request.outcome,
        "scope": request.scope, "status": request.status, "reach": request.reach, "impact": request.impact,
        "confidence": request.confidence, "effort": request.effort,
    }
    updates = [(key, value.strip() if isinstance(value, str) else value) for key, value in field_values.items() if value is not None]
    if not updates:
        return current
    scoring = {key: float(current.get(key) or 0) for key in ("reach", "impact", "confidence", "effort")}
    for key, value in updates:
        if key in scoring:
            scoring[key] = float(value)
    score = _app_call('product_rice_score', scoring["reach"], scoring["impact"], scoring["confidence"], scoring["effort"])
    updates.extend([("score", score), ("updated_at", now_iso())])
    connection = db_connection()
    try:
        assignments = ", ".join(f"{key} = ?" for key, _ in updates)
        connection.execute(f"UPDATE product_requirements SET {assignments} WHERE id = ?", [value for _, value in updates] + [requirement_id])
        connection.commit()
    finally:
        connection.close()
    updated = _app_call('get_product_requirement', requirement_id)
    if updated and updated.get("work_item_id"):
        metadata = _app_call('_product_metadata', updated.get("metadata"))
        metadata.update({"product_requirement_id": requirement_id, "product_status": updated["status"], "rice_score": updated["score"]})
        _app_call('update_work_item_record', 
            int(updated["work_item_id"]),
            {
                "status": _app_call('_product_work_item_status', str(updated["status"])),
                "priority": _app_call('_product_requirement_priority', float(updated["score"])),
                "description": "\n\n".join(part for part in [updated.get("problem", ""), f"预期结果：{updated.get('outcome', '')}" if updated.get("outcome") else ""] if part),
                "metadata_json": json.dumps(metadata, ensure_ascii=False),
            },
        )
    return updated


def create_product_decision(request: ProductDecisionRequest) -> dict[str, Any]:
    requirement = _app_call('get_product_requirement', request.requirement_id) if request.requirement_id else None
    if request.requirement_id and not requirement:
        raise HTTPException(404, "关联的产品需求不存在")
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            """INSERT INTO product_decisions
            (requirement_id, title, decision, rationale, alternatives, revisit_trigger, status, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.requirement_id, request.title.strip(), request.decision.strip(), request.rationale.strip(),
                request.alternatives.strip(), request.revisit_trigger.strip(), request.status,
                json.dumps(request.metadata, ensure_ascii=False), timestamp, timestamp,
            ),
        )
        decision_id = int(cursor.lastrowid)
        connection.commit()
    finally:
        connection.close()
    artifact = _app_call('register_artifact_safely', 
        project_id="product-manager", name=f"产品决策 · {request.title.strip()}", kind="product_decision",
        metadata={
            "decision_id": decision_id, "requirement_id": request.requirement_id, "decision": request.decision.strip(),
            "rationale": request.rationale.strip(), "alternatives": request.alternatives.strip(),
            "revisit_trigger": request.revisit_trigger.strip(), "status": request.status, "data_as_of": timestamp,
        },
    )
    if artifact:
        connection = db_connection()
        try:
            connection.execute("UPDATE product_decisions SET artifact_id = ? WHERE id = ?", (artifact["id"], decision_id))
            connection.commit()
        finally:
            connection.close()
        if requirement and requirement.get("work_item_id"):
            _app_call('create_relation_record', 
                from_type="work_item", from_id=str(requirement["work_item_id"]), to_type="artifact", to_id=str(artifact["id"]),
                relation_type="decision_for", metadata={"project_id": "product-manager", "requirement_id": request.requirement_id, "decision_id": decision_id},
            )
    return next((item for item in _app_call('list_product_decisions', ) if item["id"] == decision_id), {})


def product_cowart_status() -> dict[str, Any]:
    available = (COWART_VENDOR_DIR / COWART_SCRIPT_NAME).is_file() and (COWART_VENDOR_DIR / COWART_STYLE_NAME).is_file()
    return {
        "available": available,
        "provider": "cowart",
        "version": COWART_VERSION,
        "analytics": "disabled",
        "storage": "workbench-local",
        "license_notice": "Cowart 为 MIT；tldraw 用于生产环境时需按其许可选择 Hobby、Trial 或商业授权。",
    }


def create_product_prototype(requirement_id: int, request: ProductPrototypeRequest) -> dict[str, Any]:
    requirement = _app_call('get_product_requirement', requirement_id)
    if not requirement:
        raise HTTPException(404, "关联的产品需求不存在")
    cowart = _app_call('product_cowart_status', )
    if not cowart["available"]:
        raise HTTPException(503, "Workbench 的 Cowart 前端资源尚未安装")

    if not request.force_new:
        connection = db_connection()
        try:
            row = connection.execute(
                """SELECT id FROM product_prototypes
                WHERE requirement_id = ? AND provider = 'cowart' AND status != 'archived'
                ORDER BY updated_at DESC, id DESC LIMIT 1""",
                (int(requirement_id),),
            ).fetchone()
        finally:
            connection.close()
        if row:
            existing = _app_call('get_product_prototype', int(row["id"])) or {}
            existing["created"] = False
            return existing

    timestamp = now_iso()
    title = request.title.strip() or f"{requirement['title']} · 交互原型"
    metadata = {
        **request.metadata,
        "cowart_version": COWART_VERSION,
        "analytics": "disabled",
        "storage_policy": "server-owned-project-directory",
    }
    connection = db_connection()
    try:
        cursor = connection.execute(
            """INSERT INTO product_prototypes
            (requirement_id, title, status, provider, canvas_dir, latest_version, latest_artifact_id, metadata_json, created_at, updated_at)
            VALUES (?, ?, 'draft', 'cowart', '', 0, 0, ?, ?, ?)""",
            (int(requirement_id), title, json.dumps(metadata, ensure_ascii=False), timestamp, timestamp),
        )
        prototype_id = int(cursor.lastrowid)
        canvas_dir = f"product-prototypes/{prototype_id}/canvas"
        connection.execute("UPDATE product_prototypes SET canvas_dir = ? WHERE id = ?", (canvas_dir, prototype_id))
        connection.commit()
    finally:
        connection.close()
    _app_call('_product_prototype_root', prototype_id).joinpath("canvas").mkdir(parents=True, exist_ok=True)
    item = _app_call('get_product_prototype', prototype_id) or {}
    item["created"] = True
    return item


def _load_product_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Cowart 画布数据无法读取：{clip(str(exc), 240)}") from exc


def _require_product_prototype(prototype_id: int) -> dict[str, Any]:
    prototype = _app_call('get_product_prototype', prototype_id, include_versions=False)
    if not prototype:
        raise HTTPException(404, "产品原型不存在")
    return prototype


def _touch_product_prototype(prototype_id: int, *, status: str | None = None) -> None:
    connection = db_connection()
    try:
        if status:
            connection.execute(
                "UPDATE product_prototypes SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), int(prototype_id)),
            )
        else:
            connection.execute("UPDATE product_prototypes SET updated_at = ? WHERE id = ?", (now_iso(), int(prototype_id)))
        connection.commit()
    finally:
        connection.close()


def _cowart_canvas_response(prototype_id: int) -> dict[str, Any]:
    _app_call('_require_product_prototype', prototype_id)
    snapshot = _app_call('_load_product_json', _app_call('_product_canvas_file', prototype_id), None)
    return {
        "snapshot": snapshot,
        "storage": "workbench-single-file" if snapshot else "empty",
        "paths": ["canvas/cowart-canvas.json"] if snapshot else [],
    }


def _save_cowart_canvas(prototype_id: int, snapshot: Any) -> dict[str, Any]:
    _app_call('_require_product_prototype', prototype_id)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("store"), dict) or not isinstance(snapshot.get("schema"), dict):
        raise HTTPException(400, "需要有效的 tldraw 画布快照")
    if len(snapshot["store"]) > 100_000:
        raise HTTPException(413, "画布对象过多，暂时无法保存")
    path = _app_call('_product_canvas_file', prototype_id)
    save_json_atomic(path, snapshot, 0o600)
    _app_call('_touch_product_prototype', prototype_id, status="draft")
    return {"ok": True, "storage": "workbench-single-file", "paths": ["canvas/cowart-canvas.json"]}


def _safe_product_asset_path(prototype_id: int, kind: str, asset_path: str) -> Path:
    root = _app_call('_product_prototype_root', prototype_id) / "canvas"
    decoded = urllib.parse.unquote(str(asset_path or ""))
    if kind == "page":
        parts = Path(decoded).parts
        if len(parts) < 2:
            raise HTTPException(404, "Cowart 资源不存在")
        base = root / "pages" / parts[0] / "assets"
        candidate = base.joinpath(*parts[1:]).resolve()
    else:
        base = root / "assets"
        candidate = (base / decoded).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise HTTPException(404, "Cowart 资源不存在") from exc
    return candidate


def _cowart_shape_page_id(store: dict[str, Any], shape: dict[str, Any]) -> str:
    record: Any = shape
    visited: set[str] = set()
    while isinstance(record, dict) and str(record.get("id") or "") not in visited:
        record_id = str(record.get("id") or "")
        visited.add(record_id)
        if record.get("typeName") == "page":
            return record_id
        record = store.get(record.get("parentId"))
    return ""


def _cowart_html_from_data_url(value: str) -> str:
    if not value.startswith("data:text/html") or "," not in value:
        return ""
    header, encoded = value.split(",", 1)
    try:
        raw = base64.b64decode(encoded) if ";base64" in header.lower() else urllib.parse.unquote_to_bytes(encoded)
    except (ValueError, TypeError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _update_cowart_html_draft(prototype_id: int, draft_shape_id: str, html_content: str) -> dict[str, Any]:
    if not draft_shape_id or not html_content.strip():
        raise HTTPException(400, "HTML 草稿标识和内容不能为空")
    if len(html_content.encode("utf-8")) > 5 * 1024 * 1024:
        raise HTTPException(413, "单个 HTML 草稿不能超过 5 MB")
    snapshot = _app_call('_cowart_canvas_response', prototype_id)["snapshot"]
    if not isinstance(snapshot, dict):
        raise HTTPException(404, "Cowart 画布还没有可更新的快照")
    store = snapshot.get("store") if isinstance(snapshot.get("store"), dict) else {}
    shape = store.get(draft_shape_id)
    if not isinstance(shape, dict) or shape.get("typeName") != "shape" or shape.get("type") != "embed" or (shape.get("meta") or {}).get("cowartHtmlDraft") is not True:
        raise HTTPException(404, "没有找到要更新的 Cowart HTML 草稿")
    page_id = _app_call('_cowart_shape_page_id', store, shape)
    if not page_id:
        raise HTTPException(400, "无法确认 HTML 草稿所在画布页面")
    page_dir = re.sub(r"[^a-zA-Z0-9._-]+", "-", page_id.removeprefix("page:")).strip("-") or "page"
    file_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", draft_shape_id).strip("-") or "html-draft"
    file_name = f"{file_name}.html"
    asset_path = _app_call('_product_prototype_root', prototype_id) / "canvas" / "pages" / page_dir / "assets" / file_name
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_text(html_content, encoding="utf-8")
    asset_url = f"/page-assets/{page_dir}/{urllib.parse.quote(file_name)}"
    updated_shape = dict(shape)
    updated_shape["meta"] = {**(shape.get("meta") or {}), "cowartHtmlDraft": True, "cowartHtmlDraftAssetUrl": asset_url}
    updated_shape["props"] = {
        **(shape.get("props") or {}),
        "url": f"data:text/html;base64,{base64.b64encode(html_content.encode('utf-8')).decode('ascii')}",
    }
    store[draft_shape_id] = updated_shape
    _app_call('_save_cowart_canvas', prototype_id, snapshot)
    return {"ok": True, "assetUrl": asset_url, "pageId": page_id, "shapeId": draft_shape_id}


def _extract_cowart_html_versions(prototype_id: int, snapshot: dict[str, Any], version_dir: Path) -> list[Path]:
    html_files: list[Path] = []
    store = snapshot.get("store") if isinstance(snapshot.get("store"), dict) else {}
    for shape in store.values():
        if not isinstance(shape, dict) or (shape.get("meta") or {}).get("cowartHtmlDraft") is not True:
            continue
        html_content = _app_call('_cowart_html_from_data_url', str((shape.get("props") or {}).get("url") or ""))
        if not html_content:
            asset_url = str((shape.get("meta") or {}).get("cowartHtmlDraftAssetUrl") or "")
            if asset_url.startswith("/page-assets/"):
                source = _app_call('_safe_product_asset_path', prototype_id, "page", asset_url.removeprefix("/page-assets/"))
                try:
                    html_content = source.read_text(encoding="utf-8")
                except OSError:
                    html_content = ""
        if not html_content:
            continue
        safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(shape.get("id") or "draft")).strip("-") or "draft"
        output = version_dir / f"{safe_id}.html"
        output.write_text(html_content, encoding="utf-8")
        html_files.append(output)
    return html_files


def publish_product_prototype(prototype_id: int, request: ProductPrototypePublishRequest) -> dict[str, Any]:
    prototype = _app_call('_require_product_prototype', prototype_id)
    if not request.confirmed:
        raise HTTPException(409, "发布原型版本前需要明确确认")
    snapshot = _app_call('_cowart_canvas_response', prototype_id)["snapshot"]
    if not isinstance(snapshot, dict):
        raise HTTPException(409, "画布还没有内容，请先在 Cowart 中保存原型")
    version = int(prototype.get("latest_version") or 0) + 1
    version_dir = _app_call('_product_prototype_root', prototype_id) / "versions" / f"v{version}"
    version_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = version_dir / "cowart-canvas.json"
    save_json_atomic(snapshot_path, snapshot, 0o600)
    html_files = _app_call('_extract_cowart_html_versions', prototype_id, snapshot, version_dir)
    digest = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    manifest = {
        "version": version,
        "provider": "cowart",
        "cowart_version": COWART_VERSION,
        "prototype_id": int(prototype_id),
        "requirement_id": int(prototype.get("requirement_id") or 0),
        "snapshot_sha256": digest,
        "html_files": [path.name for path in html_files],
        "published_at": now_iso(),
    }
    save_json_atomic(version_dir / "manifest.json", manifest, 0o600)
    artifact = _app_call('register_artifact_safely', 
        project_id="product-manager",
        name=f"{prototype['title']} · v{version}",
        path=str(snapshot_path),
        kind="cowart_prototype_version",
        metadata={
            **manifest,
            "summary": request.summary.strip(),
            "html_paths": [str(path) for path in html_files],
            "analytics": "disabled",
        },
    )
    artifact_id = int((artifact or {}).get("id") or 0)
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            """INSERT INTO product_prototype_versions
            (prototype_id, version, summary, snapshot_path, html_path, preview_path, artifact_id, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)""",
            (
                int(prototype_id), version, request.summary.strip(), str(snapshot_path),
                str(html_files[0]) if html_files else "", artifact_id,
                json.dumps(manifest, ensure_ascii=False), timestamp,
            ),
        )
        version_id = int(cursor.lastrowid)
        connection.execute(
            """UPDATE product_prototypes
            SET status = 'review', latest_version = ?, latest_artifact_id = ?, updated_at = ? WHERE id = ?""",
            (version, artifact_id, timestamp, int(prototype_id)),
        )
        connection.commit()
    finally:
        connection.close()
    requirement = _app_call('get_product_requirement', int(prototype.get("requirement_id") or 0))
    if artifact_id and requirement and requirement.get("work_item_id"):
        _app_call('create_relation_record', 
            from_type="work_item", from_id=str(requirement["work_item_id"]), to_type="artifact", to_id=str(artifact_id),
            relation_type="requirement_to_prototype",
            metadata={"project_id": "product-manager", "prototype_id": int(prototype_id), "version": version},
        )
    previous_artifact_id = int(prototype.get("latest_artifact_id") or 0)
    if artifact_id and previous_artifact_id:
        _app_call('create_relation_record', 
            from_type="artifact", from_id=str(previous_artifact_id), to_type="artifact", to_id=str(artifact_id),
            relation_type="version_of", metadata={"project_id": "product-manager", "prototype_id": int(prototype_id), "version": version},
        )
    version_row = next((item for item in _app_call('list_product_prototype_versions', prototype_id) if int(item["id"]) == version_id), {})
    return {"prototype": _app_call('get_product_prototype', prototype_id), "version": version_row, "artifact": artifact}


def _cowart_frame_html(prototype: dict[str, Any]) -> str:
    prototype_id = int(prototype["id"])
    title = html_lib.escape(str(prototype.get("title") or "Cowart 原型"))
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>{title} · Cowart</title>
  <link rel=\"stylesheet\" href=\"/static/vendor/cowart/{COWART_STYLE_NAME}\" />
  <style>html,body,#root{{width:100%;height:100%;margin:0;overflow:hidden}}body{{background:#f8fafc}}</style>
  <script>
    (() => {{
      const base = location.pathname.replace(/\\/$/, '');
      const nativeFetch = window.fetch.bind(window);
      const writeSignatures = new Map();
      const rewrite = (value) => {{
        const parsed = new URL(typeof value === 'string' || value instanceof URL ? value : value.url, location.href);
        if (parsed.origin !== location.origin) return value;
        let nextPath = '';
        if (parsed.pathname.startsWith('/api/')) nextPath = base + parsed.pathname.slice(4);
        else if (parsed.pathname.startsWith('/page-assets/')) nextPath = base + parsed.pathname;
        else if (parsed.pathname.startsWith('/assets/')) nextPath = base + parsed.pathname;
        if (!nextPath) return value;
        const nextUrl = nextPath + parsed.search;
        return value instanceof Request ? new Request(nextUrl, value) : nextUrl;
      }};
      window.fetch = (input, init) => {{
        const rewritten = rewrite(input);
        const requestUrl = new URL(typeof rewritten === 'string' || rewritten instanceof URL ? rewritten : rewritten.url, location.href);
        const method = String(init?.method || (rewritten instanceof Request ? rewritten.method : 'GET')).toUpperCase();
        const body = typeof init?.body === 'string' ? init.body : '';
        if (method === 'PUT' && body && (requestUrl.pathname.endsWith('/view-state') || requestUrl.pathname.endsWith('/selection'))) {{
          try {{
            const value = JSON.parse(body);
            const stable = requestUrl.pathname.endsWith('/view-state')
              ? {{ version: value.version, currentPageId: value.currentPageId, camera: value.camera }}
              : {{ selectedShapes: value.selectedShapes }};
            const signature = JSON.stringify(stable);
            if (writeSignatures.get(requestUrl.pathname) === signature) {{
              return Promise.resolve(new Response(JSON.stringify({{ ok: true, unchanged: true }}), {{ status: 200, headers: {{ 'content-type': 'application/json' }} }}));
            }}
            writeSignatures.set(requestUrl.pathname, signature);
          }} catch (_error) {{}}
        }}
        return nativeFetch(rewritten, init);
      }};
      window.EventSource = class CowartLocalEventSource {{ addEventListener() {{}} close() {{}} }};
      window.__WORKBENCH_COWART__ = {{ prototypeId: {prototype_id}, analytics: false, version: '{COWART_VERSION}' }};
    }})();
  </script>
</head>
<body><div id=\"root\"></div><script type=\"module\" src=\"/static/vendor/cowart/{COWART_SCRIPT_NAME}\"></script></body>
</html>"""

@app.get("/projects/product-manager")
async def product_manager_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "product-manager.html")


@app.get("/api/product-manager/cowart/status")
async def get_product_cowart_status() -> dict[str, Any]:
    return _app_call('product_cowart_status', )


@app.get("/api/product-manager/prototypes")
def get_product_prototypes(limit: int = 200) -> dict[str, Any]:
    return {"prototypes": _app_call('list_product_prototypes', max(1, min(limit, 500))), "cowart": _app_call('product_cowart_status', )}


@app.get("/api/product-manager/prototypes/{prototype_id}")
def get_product_prototype_detail(prototype_id: int) -> dict[str, Any]:
    prototype = _app_call('get_product_prototype', prototype_id)
    if not prototype:
        raise HTTPException(404, "产品原型不存在")
    return {"prototype": prototype, "cowart": _app_call('product_cowart_status', )}


@app.post("/api/product-manager/requirements/{requirement_id}/prototypes")
def post_product_prototype(requirement_id: int, request: ProductPrototypeRequest) -> dict[str, Any]:
    prototype = _app_call('create_product_prototype', requirement_id, request)
    return {"prototype": prototype, "cowart": _app_call('product_cowart_status', )}


@app.post("/api/product-manager/prototypes/{prototype_id}/publish")
def post_product_prototype_version(prototype_id: int, request: ProductPrototypePublishRequest) -> dict[str, Any]:
    return _app_call('publish_product_prototype', prototype_id, request)


@app.get("/projects/product-manager/prototypes/{prototype_id}/cowart/", response_class=HTMLResponse)
def product_cowart_canvas_page(prototype_id: int) -> HTMLResponse:
    prototype = _app_call('_require_product_prototype', prototype_id)
    if not _app_call('product_cowart_status', )["available"]:
        raise HTTPException(503, "Workbench 的 Cowart 前端资源尚未安装")
    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
            "connect-src 'self' data: blob:; frame-src 'self' data: blob:; worker-src 'self' blob:; "
            "object-src 'none'; base-uri 'self'; form-action 'none'; frame-ancestors 'self'"
        ),
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "X-Content-Type-Options": "nosniff",
    }
    return HTMLResponse(_app_call('_cowart_frame_html', prototype), headers=headers)


async def _cowart_json_body(request: Request, *, max_bytes: int = 50 * 1024 * 1024) -> Any:
    raw = await request.body()
    if len(raw) > max_bytes:
        raise HTTPException(413, "Cowart 请求内容过大")
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Cowart 请求不是有效 JSON") from exc


@app.get("/projects/product-manager/prototypes/{prototype_id}/cowart/canvas")
def get_product_cowart_canvas(prototype_id: int) -> dict[str, Any]:
    return _app_call('_cowart_canvas_response', prototype_id)


@app.put("/projects/product-manager/prototypes/{prototype_id}/cowart/canvas")
async def put_product_cowart_canvas(prototype_id: int, request: Request) -> dict[str, Any]:
    return await asyncio.to_thread(_app_call, '_save_cowart_canvas', prototype_id, await _app_call('_cowart_json_body', request))


@app.get("/projects/product-manager/prototypes/{prototype_id}/cowart/selection")
def get_product_cowart_selection(prototype_id: int) -> dict[str, Any]:
    _app_call('_require_product_prototype', prototype_id)
    selection = _app_call('_load_product_json', 
        _app_call('_product_selection_file', prototype_id),
        {"selectedShapes": [], "updatedAt": None},
    )
    return {"selection": selection, "storage": "workbench-local"}


@app.put("/projects/product-manager/prototypes/{prototype_id}/cowart/selection")
async def put_product_cowart_selection(prototype_id: int, request: Request) -> dict[str, Any]:
    await asyncio.to_thread(_app_call, '_require_product_prototype', prototype_id)
    selection = await _app_call('_cowart_json_body', request, max_bytes=2 * 1024 * 1024)
    if not isinstance(selection, dict) or not isinstance(selection.get("selectedShapes"), list):
        raise HTTPException(400, "需要有效的 Cowart 选区状态")
    save_json_atomic(_app_call('_product_selection_file', prototype_id), selection, 0o600)
    return {"ok": True, "storage": "workbench-local"}


@app.get("/projects/product-manager/prototypes/{prototype_id}/cowart/view-state")
def get_product_cowart_view_state(prototype_id: int) -> dict[str, Any]:
    _app_call('_require_product_prototype', prototype_id)
    view_state = _app_call('_load_product_json', 
        _app_call('_product_view_state_file', prototype_id),
        {"version": 1, "currentPageId": None, "camera": {"x": 0, "y": 0, "z": 1}, "updatedAt": None},
    )
    return {"viewState": view_state, "storage": "workbench-local"}


@app.put("/projects/product-manager/prototypes/{prototype_id}/cowart/view-state")
async def put_product_cowart_view_state(prototype_id: int, request: Request) -> dict[str, Any]:
    await asyncio.to_thread(_app_call, '_require_product_prototype', prototype_id)
    view_state = await _app_call('_cowart_json_body', request, max_bytes=2 * 1024 * 1024)
    camera = view_state.get("camera") if isinstance(view_state, dict) else None
    coordinates = [camera.get(key) for key in ("x", "y", "z")] if isinstance(camera, dict) else []
    if (
        not isinstance(view_state, dict)
        or view_state.get("version") != 1
        or view_state.get("currentPageId") is not None and not isinstance(view_state.get("currentPageId"), str)
        or len(coordinates) != 3
        or not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in coordinates)
    ):
        raise HTTPException(400, "需要有效的 Cowart 视图状态")
    save_json_atomic(_app_call('_product_view_state_file', prototype_id), view_state, 0o600)
    return {"ok": True, "storage": "workbench-local"}


@app.put("/projects/product-manager/prototypes/{prototype_id}/cowart/html-draft")
async def put_product_cowart_html_draft(prototype_id: int, request: Request) -> dict[str, Any]:
    body = await _app_call('_cowart_json_body', request, max_bytes=6 * 1024 * 1024)
    if not isinstance(body, dict):
        raise HTTPException(400, "需要有效的 Cowart HTML 草稿")
    return await asyncio.to_thread(_app_call, '_update_cowart_html_draft', 
        prototype_id,
        str(body.get("draftShapeId") or ""),
        str(body.get("htmlContent") or ""),
    )


def _cowart_asset_response(prototype_id: int, kind: str, asset_path: str) -> FileResponse:
    _app_call('_require_product_prototype', prototype_id)
    path = _app_call('_safe_product_asset_path', prototype_id, kind, asset_path)
    if not path.is_file():
        raise HTTPException(404, "Cowart 资源不存在")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"}
    if media_type == "text/html":
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(path, media_type=media_type, headers=headers)


@app.get("/projects/product-manager/prototypes/{prototype_id}/cowart/page-assets/{asset_path:path}")
def get_product_cowart_page_asset(prototype_id: int, asset_path: str) -> FileResponse:
    return _app_call('_cowart_asset_response', prototype_id, "page", asset_path)


@app.get("/projects/product-manager/prototypes/{prototype_id}/cowart/assets/{asset_path:path}")
def get_product_cowart_global_asset(prototype_id: int, asset_path: str) -> FileResponse:
    return _app_call('_cowart_asset_response', prototype_id, "global", asset_path)


class ProductProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=2_000)
    color: str = Field(default="", max_length=20)


class ProductProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    summary: str | None = Field(default=None, max_length=2_000)
    status: str | None = Field(default=None, pattern="^(active|archived)$")


@app.get("/api/product-manager/projects")
def get_product_projects(include_archived: bool = False) -> dict[str, Any]:
    return {"projects": _app_call('list_product_projects', include_archived)}


@app.post("/api/product-manager/projects")
def post_product_project(request: ProductProjectRequest) -> dict[str, Any]:
    return {"ok": True, "project": _app_call('create_product_project', request.name, request.summary, request.color)}


@app.patch("/api/product-manager/projects/{project_id}")
def patch_product_project(project_id: int, request: ProductProjectUpdateRequest) -> dict[str, Any]:
    project = _app_call('update_product_project', project_id, name=request.name, summary=request.summary, status=request.status)
    if not project:
        raise HTTPException(404, "项目不存在或没有可更新的字段")
    return {"ok": True, "project": project}


@app.get("/api/product-manager/overview")
def get_product_manager_overview(limit: int = 200, project_id: str = "") -> dict[str, Any]:
    return _app_call('product_manager_overview', limit=max(1, min(limit, 500)), project_id=project_id)


@app.post("/api/product-manager/feedback")
def post_product_feedback(request: ProductFeedbackRequest) -> dict[str, Any]:
    return {"feedback": _app_call('create_product_feedback', request), "summary": _app_call('product_manager_overview', )["summary"]}


@app.patch("/api/product-manager/feedback/{feedback_id}")
def patch_product_feedback(feedback_id: int, request: ProductFeedbackUpdateRequest) -> dict[str, Any]:
    feedback = _app_call('update_product_feedback', feedback_id, request)
    if not feedback:
        raise HTTPException(404, "产品反馈不存在")
    return {"feedback": feedback, "summary": _app_call('product_manager_overview', )["summary"]}


@app.post("/api/product-manager/requirements")
def post_product_requirement(request: ProductRequirementRequest) -> dict[str, Any]:
    return {"requirement": _app_call('create_product_requirement', request), "summary": _app_call('product_manager_overview', )["summary"]}


@app.patch("/api/product-manager/requirements/{requirement_id}")
def patch_product_requirement(requirement_id: int, request: ProductRequirementUpdateRequest) -> dict[str, Any]:
    requirement = _app_call('update_product_requirement', requirement_id, request)
    if not requirement:
        raise HTTPException(404, "产品需求不存在")
    return {"requirement": requirement, "summary": _app_call('product_manager_overview', )["summary"]}


@app.post("/api/product-manager/decisions")
def post_product_decision(request: ProductDecisionRequest) -> dict[str, Any]:
    return {"decision": _app_call('create_product_decision', request), "summary": _app_call('product_manager_overview', )["summary"]}


@app.post("/api/product-manager/requirements/{requirement_id}/prd")
async def generate_product_requirement_prd(requirement_id: int) -> dict[str, Any]:
    requirement = await asyncio.to_thread(_app_call, 'get_product_requirement', requirement_id)
    if not requirement:
        raise HTTPException(404, "产品需求不存在")
    feedback = [item for item in _app_call('list_product_feedback', ) if int(item.get("linked_requirement_id") or 0) == requirement_id]
    evidence = "\n".join(
        f"- [{item.get('persona') or '未标注用户'}｜{item.get('source') or '手动记录'}] {item.get('content', '')}"
        for item in feedback[:30]
    ) or "- 当前没有关联反馈证据，PRD 中必须明确标注证据缺口。"
    source_text = (
        f"需求标题：{requirement['title']}\n"
        f"目标用户：{requirement.get('target_user') or '待补充'}\n"
        f"用户问题：{requirement.get('problem') or '待补充'}\n"
        f"预期结果：{requirement.get('outcome') or '待补充'}\n"
        f"范围草案：{requirement.get('scope') or '待补充'}\n"
        f"当前状态：{requirement.get('status')}\n"
        f"RICE：{requirement.get('score')}（Reach {requirement.get('reach')} / Impact {requirement.get('impact')} / Confidence {requirement.get('confidence')}% / Effort {requirement.get('effort')}）\n\n"
        f"关联反馈证据：\n{evidence}"
    )
    result = await _app_call('run_document_factory', _app_call('DocumentFactoryRequest', 
        title=f"{requirement['title']} PRD",
        source_text=source_text,
        instruction="生成一页式、可评审的产品需求文档。先写结论，明确目标与非目标；所有缺失信息标记待补充，不得把推断写成用户事实。",
        template="prd",
        source_name=f"产品作战室需求 #{requirement_id}",
        acceptance_criteria=["目标、非目标和范围清楚", "关键流程与异常场景可评审", "指标和验收标准可验证", "用户事实与产品判断明确区分"],
    ))
    artifact = result.get("artifact") or {}
    if artifact and requirement.get("work_item_id"):
        result["product_relation"] = await asyncio.to_thread(_app_call, 'create_relation_record', 
            from_type="work_item", from_id=str(requirement["work_item_id"]), to_type="artifact", to_id=str(artifact["id"]),
            relation_type="requirement_to_prd", metadata={"source_project": "product-manager", "target_project": "doc-factory", "requirement_id": requirement_id},
        )
    return result


# ═══════════════ 产品作战室：反馈 → 需求 → 决策 → PRD ═══════════════

PRODUCT_FEEDBACK_STATUSES = {"new", "reviewing", "linked", "archived"}
PRODUCT_REQUIREMENT_STATUSES = {"discovering", "review", "planned", "building", "shipped", "paused"}
PRODUCT_DECISION_STATUSES = {"proposed", "decided", "revisiting", "superseded"}


__all__ = [
    "PRODUCT_DECISION_STATUSES",
    "PRODUCT_FEEDBACK_STATUSES",
    "PRODUCT_ITEM_TYPES",
    "PRODUCT_REQUIREMENT_STATUSES",
    "PRODUCT_SEVERITIES",
    "ProductDecisionRequest",
    "ProductFeedbackRequest",
    "ProductFeedbackUpdateRequest",
    "ProductProjectRequest",
    "ProductProjectUpdateRequest",
    "ProductPrototypePublishRequest",
    "ProductPrototypeRequest",
    "ProductRequirementRequest",
    "ProductRequirementUpdateRequest",
    "_OUTPUTS_DIR",
    "_PRODUCT_PROTOTYPES_DIR",
    "_cowart_asset_response",
    "_cowart_canvas_response",
    "_cowart_frame_html",
    "_cowart_html_from_data_url",
    "_cowart_json_body",
    "_cowart_shape_page_id",
    "_extract_cowart_html_versions",
    "_load_product_json",
    "_product_canvas_file",
    "_product_decision_row",
    "_product_defect_priority",
    "_product_feedback_row",
    "_product_metadata",
    "_product_prototype_root",
    "_product_prototype_row",
    "_product_prototype_version_row",
    "_product_requirement_priority",
    "_product_requirement_row",
    "_product_selection_file",
    "_product_view_state_file",
    "_product_work_item_status",
    "_require_product_prototype",
    "_safe_product_asset_path",
    "_save_cowart_canvas",
    "_touch_product_prototype",
    "_update_cowart_html_draft",
    "create_product_decision",
    "create_product_feedback",
    "create_product_project",
    "create_product_prototype",
    "create_product_requirement",
    "generate_product_requirement_prd",
    "get_product_cowart_canvas",
    "get_product_cowart_global_asset",
    "get_product_cowart_page_asset",
    "get_product_cowart_selection",
    "get_product_cowart_status",
    "get_product_cowart_view_state",
    "get_product_feedback",
    "get_product_manager_overview",
    "get_product_projects",
    "get_product_prototype",
    "get_product_prototype_detail",
    "get_product_prototypes",
    "get_product_requirement",
    "list_product_decisions",
    "list_product_feedback",
    "list_product_projects",
    "list_product_prototype_versions",
    "list_product_prototypes",
    "list_product_requirements",
    "patch_product_feedback",
    "patch_product_project",
    "patch_product_requirement",
    "post_product_decision",
    "post_product_feedback",
    "post_product_project",
    "post_product_prototype",
    "post_product_prototype_version",
    "post_product_requirement",
    "product_cowart_canvas_page",
    "product_cowart_status",
    "product_manager_overview",
    "product_manager_page",
    "product_manager_summary",
    "product_rice_score",
    "publish_product_prototype",
    "put_product_cowart_canvas",
    "put_product_cowart_html_draft",
    "put_product_cowart_selection",
    "put_product_cowart_view_state",
    "update_product_feedback",
    "update_product_project",
    "update_product_requirement",
    "valid_product_project_id",
]
