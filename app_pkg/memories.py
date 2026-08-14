"""Workbench 记忆领域：项目记忆 CRUD、检索、卫生、导入。

从 app.py 拆出的领域模块（为开源准备）。依赖 core（decode_json_column/clip/
now_iso）与 db，零业务耦合（无 LLM 调用、无跨域依赖）。路由与 React 工具仍
留 app.py。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from .core import (
    MAX_MEMORY_CONTEXT_ITEMS,
    MEMORY_OWNER_ID,
    _int_env,
    clip,
    decode_json_column,
    log,
    now_iso,
    query_terms,
)
from .db import db_connection
from .instance import app


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


class MemoryCreateRequest(BaseModel):
    content: str = Field(min_length=2, max_length=1_000)
    scope: str = Field(default="global", pattern="^(global|project)$")
    project_id: str = Field(default="", max_length=80)
    kind: str = Field(default="preference", pattern="^(preference|constraint|routine|decision|profile)$")
    memory_key: str = Field(default="", max_length=160)
    value: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="confirmed", pattern="^(candidate|confirmed)$")
    confidence: float = Field(default=1.0, ge=0, le=1)
    pinned: bool = False



class MemoryUpdateRequest(BaseModel):
    content: str | None = Field(default=None, min_length=2, max_length=1_000)
    scope: str | None = Field(default=None, pattern="^(global|project)$")
    project_id: str | None = Field(default=None, max_length=80)
    kind: str | None = Field(default=None, pattern="^(preference|constraint|routine|decision|profile)$")
    value: dict[str, Any] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    pinned: bool | None = None



class MemoryImportRequest(BaseModel):
    confirmed: bool = False










__all__ = [
    "MemoryImportRequest",
    "MemoryUpdateRequest",
    "MemoryCreateRequest",
    "MAX_MEMORY_CONTEXT_CHARS",
    "MAX_MEMORY_ITEM_CONTEXT_CHARS",
    "MAX_MEMORY_MATCHED_ITEMS",
    "MAX_MEMORY_PINNED_ITEMS",
    "MEMORY_KINDS",
    "MEMORY_SCOPES",
    "MEMORY_SECRET_MARKERS",
    "MEMORY_STALE_DAYS",
    "MEMORY_STATUSES",
    "MemoryArchiveRequest",
    "_memory_event",
    "_memory_is_secret_like",
    "_memory_kind_for_text",
    "archive_memory_items",
    "confirm_memory",
    "create_memory",
    "create_memory_item",
    "delete_memory",
    "delete_memory_item",
    "ensure_legacy_cid_memories",
    "get_memories",
    "get_memory_hygiene",
    "get_memory_item",
    "import_workbuddy_memories",
    "import_workbuddy_memory",
    "learn_memories_from_message",
    "list_memory_items",
    "load_cid_preferences",
    "memory_context_for_llm",
    "memory_hygiene",
    "memory_item_row",
    "memory_match_reason",
    "memory_summary",
    "post_memory_archive",
    "preview_workbuddy_memory_import",
    "reject_memory",
    "retrieve_memories",
    "set_memory_status",
    "sync_cid_preferences_to_memories",
    "update_memory",
    "update_memory_item",
    "workbuddy_memory_preview",
]

def load_cid_preferences() -> dict[str, Any]:
    """延迟转发 app.load_cid_preferences（cid 领域仍在 app.py）。"""
    import app as _app

    return _app.load_cid_preferences()



MAX_MEMORY_MATCHED_ITEMS = 3
MAX_MEMORY_PINNED_ITEMS = 2
MAX_MEMORY_ITEM_CONTEXT_CHARS = 280
MAX_MEMORY_CONTEXT_CHARS = 1_200


MEMORY_SCOPES = {"global", "project"}
MEMORY_KINDS = {"preference", "constraint", "routine", "decision", "profile"}
MEMORY_STATUSES = {"candidate", "confirmed", "rejected", "superseded"}
MEMORY_SECRET_MARKERS = (
    "password", "passwd", "api key", "apikey", "access token", "refresh token", "bearer ",
    "cookie", "authorization", "private key", "secret", "密码", "口令", "密钥", "令牌",
    "身份证", "银行卡", "信用卡", "手机号", "家庭住址",
)


def ensure_legacy_cid_memories() -> None:
    connection = db_connection()
    try:
        imported = connection.execute(
            "SELECT 1 FROM memory_events WHERE memory_id = 'system:cid-legacy' AND event_type = 'legacy_import_completed' LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if imported:
        return
    _app_call('sync_cid_preferences_to_memories', _app_call('load_cid_preferences', ))
    connection = db_connection()
    try:
        _app_call('_memory_event', connection, "system:cid-legacy", "legacy_import_completed", source_type="cid_preferences")
        connection.commit()
    finally:
        connection.close()


def memory_item_row(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    item["value"] = decode_json_column(item.pop("value_json", "{}"))
    item["pinned"] = bool(item.get("pinned"))
    item["scope_label"] = "全局" if item.get("scope") == "global" else "项目"
    item["kind_label"] = {
        "preference": "偏好",
        "constraint": "边界",
        "routine": "习惯",
        "decision": "决策",
        "profile": "个人信息",
    }.get(str(item.get("kind") or ""), str(item.get("kind") or "记忆"))
    item["status_label"] = {
        "candidate": "待确认",
        "confirmed": "已确认",
        "rejected": "已忽略",
        "superseded": "已更新",
    }.get(str(item.get("status") or ""), str(item.get("status") or "未知"))
    return item


def _memory_is_secret_like(content: str) -> bool:
    normalized = str(content or "").lower()
    if any(marker in normalized for marker in MEMORY_SECRET_MARKERS):
        return True
    return bool(re.search(r"\b(?:sk|rk|pk)-[a-z0-9_-]{12,}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----", normalized, re.IGNORECASE))


def _memory_event(
    connection: sqlite3.Connection,
    memory_id: str,
    event_type: str,
    *,
    source_type: str = "",
    source_id: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        "INSERT INTO memory_events(memory_id, event_type, source_type, source_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (memory_id, event_type, clip(source_type, 80), clip(source_id, 160), json.dumps(payload or {}, ensure_ascii=False), now_iso()),
    )


def get_memory_item(memory_id: str) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM memory_items WHERE id = ? AND owner_id = ?", (memory_id, MEMORY_OWNER_ID)).fetchone()
        return _app_call('memory_item_row', row) if row else None
    finally:
        connection.close()


def create_memory_item(
    *,
    content: str,
    scope: str = "global",
    project_id: str = "",
    kind: str = "preference",
    memory_key: str = "",
    value: dict[str, Any] | None = None,
    status: str = "candidate",
    confidence: float = 0.5,
    pinned: bool = False,
    source_type: str = "",
    source_id: str = "",
    evidence_text: str = "",
) -> dict[str, Any]:
    clean_content = re.sub(r"\s+", " ", str(content or "")).strip()
    if len(clean_content) < 2:
        raise ValueError("记忆内容太短")
    if _app_call('_memory_is_secret_like', clean_content):
        raise ValueError("这段内容可能包含凭据或敏感个人信息，不会保存为记忆")
    clean_scope = scope if scope in MEMORY_SCOPES else "global"
    clean_project_id = clip(project_id.strip(), 80) if clean_scope == "project" else ""
    if clean_scope == "project" and not clean_project_id:
        clean_project_id = "workbench"
    clean_kind = kind if kind in MEMORY_KINDS else "preference"
    clean_status = status if status in {"candidate", "confirmed"} else "candidate"
    clean_key = clip(memory_key.strip(), 160)
    clean_confidence = max(0.0, min(float(confidence), 1.0))
    timestamp = now_iso()
    connection = db_connection()
    try:
        existing = None
        if clean_key:
            existing = connection.execute(
                """SELECT * FROM memory_items
                WHERE owner_id = ? AND memory_key = ? AND status IN ('candidate', 'confirmed')
                ORDER BY updated_at DESC LIMIT 1""",
                (MEMORY_OWNER_ID, clean_key),
            ).fetchone()
        if not existing:
            existing = connection.execute(
                """SELECT * FROM memory_items
                WHERE owner_id = ? AND scope = ? AND project_id = ? AND kind = ? AND content = ?
                  AND status IN ('candidate', 'confirmed')
                ORDER BY updated_at DESC LIMIT 1""",
                (MEMORY_OWNER_ID, clean_scope, clean_project_id, clean_kind, clean_content),
            ).fetchone()
        if existing and str(existing["content"]) == clean_content:
            memory_id = str(existing["id"])
            next_status = "confirmed" if clean_status == "confirmed" or existing["status"] == "confirmed" else "candidate"
            next_confidence = max(float(existing["confidence"] or 0), clean_confidence)
            connection.execute(
                """UPDATE memory_items SET status = ?, confidence = ?, pinned = ?, updated_at = ?,
                source_type = CASE WHEN source_type = '' THEN ? ELSE source_type END,
                source_id = CASE WHEN source_id = '' THEN ? ELSE source_id END
                WHERE id = ?""",
                (next_status, next_confidence, int(bool(existing["pinned"]) or pinned), timestamp, clip(source_type, 80), clip(source_id, 160), memory_id),
            )
            _app_call('_memory_event', connection, memory_id, "reinforced", source_type=source_type, source_id=source_id, payload={"status": next_status, "confidence": next_confidence})
            connection.commit()
            row = connection.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
            return {**_app_call('memory_item_row', row), "created": False}
        if existing and clean_key and clean_status == "confirmed":
            connection.execute("UPDATE memory_items SET status = 'superseded', updated_at = ? WHERE id = ?", (timestamp, existing["id"]))
            _app_call('_memory_event', connection, str(existing["id"]), "superseded", source_type=source_type, source_id=source_id)
        memory_id = uuid.uuid4().hex[:16]
        connection.execute(
            """INSERT INTO memory_items
            (id, owner_id, scope, project_id, kind, memory_key, content, value_json, status, confidence,
             sensitivity, pinned, source_type, source_id, evidence_text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'normal', ?, ?, ?, ?, ?, ?)""",
            (
                memory_id, MEMORY_OWNER_ID, clean_scope, clean_project_id, clean_kind, clean_key, clip(clean_content, 1_000),
                json.dumps(value or {}, ensure_ascii=False), clean_status, clean_confidence, int(pinned), clip(source_type, 80),
                clip(source_id, 160), clip(evidence_text, 1_000), timestamp, timestamp,
            ),
        )
        _app_call('_memory_event', connection, memory_id, "created", source_type=source_type, source_id=source_id, payload={"status": clean_status})
        connection.commit()
        row = connection.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        return {**_app_call('memory_item_row', row), "created": True}
    finally:
        connection.close()


def list_memory_items(*, status: str = "all", project_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
    clauses = ["owner_id = ?"]
    values: list[Any] = [MEMORY_OWNER_ID]
    if status in MEMORY_STATUSES:
        clauses.append("status = ?")
        values.append(status)
    elif status == "active":
        clauses.append("status IN ('candidate', 'confirmed')")
    if project_id:
        clauses.append("(scope = 'global' OR project_id = ?)")
        values.append(project_id)
    values.append(max(1, min(int(limit), 500)))
    connection = db_connection()
    try:
        rows = connection.execute(
            f"SELECT * FROM memory_items WHERE {' AND '.join(clauses)} ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            values,
        ).fetchall()
        return [_app_call('memory_item_row', row) for row in rows]
    finally:
        connection.close()


def update_memory_item(memory_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    current = _app_call('get_memory_item', memory_id)
    if not current:
        return None
    content = re.sub(r"\s+", " ", str(updates.get("content", current["content"]))).strip()
    if len(content) < 2:
        raise ValueError("记忆内容太短")
    if _app_call('_memory_is_secret_like', content):
        raise ValueError("这段内容可能包含凭据或敏感个人信息，不会保存为记忆")
    scope = updates.get("scope") if updates.get("scope") in MEMORY_SCOPES else current["scope"]
    project_id = str(updates.get("project_id", current["project_id"])).strip() if scope == "project" else ""
    if scope == "project" and not project_id:
        project_id = current.get("project_id") or "workbench"
    kind = updates.get("kind") if updates.get("kind") in MEMORY_KINDS else current["kind"]
    confidence = max(0.0, min(float(updates.get("confidence", current["confidence"])), 1.0))
    pinned = bool(updates.get("pinned", current["pinned"]))
    value = updates.get("value") if isinstance(updates.get("value"), dict) else current.get("value", {})
    changed_fields = [key for key, old, new in (
        ("content", current["content"], content), ("scope", current["scope"], scope),
        ("project_id", current["project_id"], project_id), ("kind", current["kind"], kind),
        ("confidence", current["confidence"], confidence), ("pinned", current["pinned"], pinned),
        ("value", current.get("value", {}), value),
    ) if old != new]
    connection = db_connection()
    try:
        connection.execute(
            """UPDATE memory_items SET content = ?, scope = ?, project_id = ?, kind = ?, value_json = ?,
            confidence = ?, pinned = ?, updated_at = ? WHERE id = ? AND owner_id = ?""",
            (clip(content, 1_000), scope, clip(project_id, 80), kind, json.dumps(value, ensure_ascii=False), confidence, int(pinned), now_iso(), memory_id, MEMORY_OWNER_ID),
        )
        _app_call('_memory_event', connection, memory_id, "corrected", source_type="user", payload={"changed_fields": changed_fields})
        connection.commit()
    finally:
        connection.close()
    return _app_call('get_memory_item', memory_id)


def set_memory_status(memory_id: str, status: str) -> dict[str, Any] | None:
    if status not in {"confirmed", "rejected"}:
        raise ValueError("不支持的记忆状态")
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM memory_items WHERE id = ? AND owner_id = ?", (memory_id, MEMORY_OWNER_ID)).fetchone()
        if not row:
            return None
        confidence = max(float(row["confidence"] or 0), 0.9) if status == "confirmed" else float(row["confidence"] or 0)
        connection.execute("UPDATE memory_items SET status = ?, confidence = ?, updated_at = ? WHERE id = ?", (status, confidence, now_iso(), memory_id))
        _app_call('_memory_event', connection, memory_id, status, source_type="user")
        connection.commit()
    finally:
        connection.close()
    return _app_call('get_memory_item', memory_id)


def delete_memory_item(memory_id: str) -> bool:
    connection = db_connection()
    try:
        row = connection.execute("SELECT id FROM memory_items WHERE id = ? AND owner_id = ?", (memory_id, MEMORY_OWNER_ID)).fetchone()
        if not row:
            return False
        connection.execute("DELETE FROM memory_events WHERE memory_id = ?", (memory_id,))
        connection.execute("DELETE FROM memory_items WHERE id = ? AND owner_id = ?", (memory_id, MEMORY_OWNER_ID))
        connection.commit()
        return True
    finally:
        connection.close()


def memory_summary() -> dict[str, Any]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT status, COUNT(*) AS count FROM memory_items WHERE owner_id = ? GROUP BY status", (MEMORY_OWNER_ID,)).fetchall()
        counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
        global_count = int(connection.execute("SELECT COUNT(*) FROM memory_items WHERE owner_id = ? AND scope = 'global' AND status = 'confirmed'", (MEMORY_OWNER_ID,)).fetchone()[0] or 0)
        project_count = int(connection.execute("SELECT COUNT(*) FROM memory_items WHERE owner_id = ? AND scope = 'project' AND status = 'confirmed'", (MEMORY_OWNER_ID,)).fetchone()[0] or 0)
    finally:
        connection.close()
    return {"confirmed": counts.get("confirmed", 0), "candidate": counts.get("candidate", 0), "rejected": counts.get("rejected", 0), "superseded": counts.get("superseded", 0), "global": global_count, "project": project_count}


MEMORY_STALE_DAYS = _int_env("WORKBENCH_MEMORY_STALE_DAYS", 30, minimum=7, maximum=365)


def memory_hygiene(limit: int = 40) -> dict[str, Any]:
    """指出哪些记忆其实在拖后腿。

    记忆表只增不减：expires_at 字段一直存在，但没有任何代码给它赋值，所以
    确认过的记忆会永久留在检索池里。每轮只有 5 条能进上下文，池子越大、
    真正相关的越容易被挤掉——这时候"记得多"反而让 Agent 更笨。

    这里不自动删任何东西，只把三类值得你过目的挑出来：
      never_used  确认很久却一次都没被检索命中，多半是当初随手确认的
      idle        用过但很久没再用，可能是阶段性信息（比如某个已结束的项目）
      duplicates  内容高度重合，重复占用本就稀缺的 5 个名额
    """
    limit = max(1, min(int(limit or 40), 200))
    stale_before = (datetime.now(timezone.utc) - timedelta(days=MEMORY_STALE_DAYS)).isoformat()
    connection = db_connection()
    try:
        rows = [dict(row) for row in connection.execute(
            """SELECT id, content, memory_key, kind, scope, project_id, confidence, pinned,
                      use_count, last_used_at, created_at, updated_at
            FROM memory_items
            WHERE owner_id = ? AND status = 'confirmed' AND pinned = 0
            ORDER BY updated_at DESC LIMIT 500""",
            (MEMORY_OWNER_ID,),
        ).fetchall()]
    finally:
        connection.close()

    never_used, idle = [], []
    for row in rows:
        used = int(row.get("use_count") or 0)
        last_used = str(row.get("last_used_at") or "")
        created = str(row.get("created_at") or "")
        if used == 0 and created and created < stale_before:
            never_used.append(row)
        elif used > 0 and last_used and last_used < stale_before:
            idle.append(row)

    # 这里原本还有一个"重复记忆"检测，用字符二元组算相似度。实测后删掉了：
    #   「关注 A 股行情」vs「关注美股行情」重叠 0.75，但它们是两条不同的记忆，
    #   归档任何一条都是错的；而「每天早上 8 点推送课程」vs「每日 8:00 推送今天
    #   的课程」确实重复，重叠却只有 0.22。Jaccard 和重叠系数、四档阈值都试过，
    #   没有一档能同时判对——字面相似度分不清"换个说法"和"差一个关键词"。
    # 与其给出会诱导用户删错记忆的建议，不如只保留下面两个基于硬事实
    # （use_count / last_used_at）的信号。真要做去重，需要语义向量而不是字符串。

    return {
        "never_used": never_used[:limit],
        "idle": idle[:limit],
        "stale_days": MEMORY_STALE_DAYS,
        "checked": len(rows),
        "policy": (
            f"只列出已确认、未置顶、且超过 {MEMORY_STALE_DAYS} 天的记忆；置顶记忆永不进入建议。"
            "这里不会自动删除任何东西，归档需要你确认；归档后记忆仍保留在库中，只是不再进入 Agent 上下文。"
        ),
    }


def archive_memory_items(memory_ids: list[str]) -> dict[str, Any]:
    """把记忆移出检索池，但保留记录本身（可追溯，也可恢复）。"""
    wanted = [str(value).strip() for value in memory_ids if str(value).strip()][:200]
    if not wanted:
        return {"archived": 0, "ids": []}
    timestamp = now_iso()
    placeholders = ",".join("?" for _ in wanted)
    connection = db_connection()
    try:
        # 置顶记忆是用户明确要一直生效的，批量归档不能顺手带走。
        cursor = connection.execute(
            f"""UPDATE memory_items SET status = 'superseded', updated_at = ?
            WHERE owner_id = ? AND status = 'confirmed' AND pinned = 0 AND id IN ({placeholders})""",
            [timestamp, MEMORY_OWNER_ID, *wanted],
        )
        archived = int(cursor.rowcount or 0)
        connection.commit()
    finally:
        connection.close()
    if archived:
        log.info("归档了 %d 条记忆", archived)
    return {"archived": archived, "ids": wanted, "summary": _app_call('memory_summary', )}


def _memory_kind_for_text(text: str) -> str:
    if any(word in text for word in ("必须", "不要", "不能", "别再", "不接受", "务必")):
        return "constraint"
    if any(word in text for word in ("每次", "习惯", "通常", "总是")):
        return "routine"
    if any(word in text for word in ("我是", "我在", "我的身份")):
        return "profile"
    return "preference"


def learn_memories_from_message(message: str, *, project_id: str, source_type: str, source_id: str) -> list[dict[str, Any]]:
    """Extract only durable, user-authored signals; assistant text is never learned."""
    text = clip(str(message or "").strip(), 2_000)
    if not text:
        return []
    patterns: tuple[tuple[re.Pattern[str], bool, str], ...] = (
        (re.compile(r"^(?:请)?记住(?:了)?[：,:，\s]*(.+)$"), True, "用户明确要求记住"),
        (re.compile(r"^(?:以后|今后)(?:都|一直|请)?[：,:，\s]*(.+)$"), True, "用户明确指定长期规则"),
        (re.compile(r"^(?:每次|始终|一直)(?:都|请)?[：,:，\s]*(.+)$"), True, "用户明确指定重复习惯"),
        (re.compile(r"^(我(?:更)?(?:喜欢|偏好|习惯|不喜欢|讨厌).+)$"), False, "从用户表达中发现偏好"),
        (re.compile(r"^((?:别再|永远不要|我不接受).+)$"), True, "用户明确指定边界"),
    )
    learned: list[dict[str, Any]] = []
    seen: set[str] = set()
    sentences = [part.strip(" \t\r\n，,：:") for part in re.split(r"[。！？!?;；\n]+", text) if part.strip()]
    for sentence in sentences[:8]:
        for pattern, explicit, reason in patterns:
            match = pattern.match(sentence)
            if not match:
                continue
            content = re.sub(r"\s+", " ", match.group(1).strip(" ，,：:\"'“”"))
            if len(content) < 3 or content in seen or _app_call('_memory_is_secret_like', content):
                break
            seen.add(content)
            project_specific = any(marker in sentence for marker in ("这个项目", "当前项目", "这个页面", "在这里"))
            scope = "project" if project_specific else "global"
            memory_project_id = project_id if scope == "project" else ""
            digest = hashlib.sha256(f"{scope}\n{memory_project_id}\n{content}".encode("utf-8", errors="ignore")).hexdigest()[:20]
            item = _app_call('create_memory_item', 
                content=content,
                scope=scope,
                project_id=memory_project_id,
                kind=_app_call('_memory_kind_for_text', sentence),
                memory_key=f"learned:{digest}",
                value={"learning_reason": reason},
                status="confirmed" if explicit else "candidate",
                confidence=1.0 if explicit else 0.72,
                pinned=explicit,
                source_type=source_type,
                source_id=source_id,
                evidence_text=sentence,
            )
            item["learning_reason"] = reason
            learned.append(item)
            break
    return learned


def sync_cid_preferences_to_memories(preferences: dict[str, Any]) -> None:
    for field, polarity in (("preferred_tags", "preferred"), ("avoid_tags", "avoid")):
        tags = [value.strip() for value in str(preferences.get(field) or "").split(",") if value.strip()]
        for tag in tags[:40]:
            normalized = re.sub(r"\s+", "-", tag.lower())[:80]
            content = f"独立开发机会中{'偏好' if polarity == 'preferred' else '避开'}赛道标签：{tag}"
            try:
                _app_call('create_memory_item', 
                    content=content,
                    scope="project",
                    project_id="cid-dashboard",
                    kind="preference" if polarity == "preferred" else "constraint",
                    memory_key=f"cid-tag:{normalized}",
                    value={"tag": tag, "polarity": polarity},
                    status="confirmed",
                    confidence=1.0,
                    source_type="cid_preferences",
                )
            except ValueError:
                continue


def ensure_legacy_cid_memories() -> None:
    connection = db_connection()
    try:
        imported = connection.execute(
            "SELECT 1 FROM memory_events WHERE memory_id = 'system:cid-legacy' AND event_type = 'legacy_import_completed' LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if imported:
        return
    _app_call('sync_cid_preferences_to_memories', _app_call('load_cid_preferences', ))
    connection = db_connection()
    try:
        _app_call('_memory_event', connection, "system:cid-legacy", "legacy_import_completed", source_type="cid_preferences")
        connection.commit()
    finally:
        connection.close()


def memory_match_reason(hit_terms: list[str], haystack: str = "") -> str:
    """把命中的词整理成一句人能看懂的理由。

    query_terms 会把中文切成二元片段，一句「服务器现在怎么样」能同时命中
    「服务」和「务器」——两条一起摆出来只是噪音，说明不了任何额外的东西。
    所以优先用长词，并且丢掉已被更长的词包含的碎片，最多留两个。
    """
    # 同样长度的片段里，取在原文中出现得最靠前的那个：「服务器磁盘」里
    # 「服务」从第 0 个字开始，「务器」从第 1 个字开始，前者读起来才像话。
    ordered = sorted(hit_terms, key=lambda term: (-len(term), haystack.find(term) if haystack else 0, term))
    kept: list[str] = []
    for term in ordered:
        # 已被更长的词包含，或者和已选的二元片段首尾相接（服务 / 务器），
        # 都不带来新信息。
        if any(term in existing or term[:1] == existing[-1:] or term[-1:] == existing[:1] for existing in kept):
            continue
        kept.append(term)
        if len(kept) == 2:
            break
    return "命中 " + "、".join(f"「{term}」" for term in kept) if kept else ""


def retrieve_memories(
    query: str,
    *,
    project_id: str,
    limit: int = MAX_MEMORY_CONTEXT_ITEMS,
    core_only: bool = False,
) -> list[dict[str, Any]]:
    """Return a small prompt window, never the whole durable memory store."""
    if project_id == "cid-dashboard":
        _app_call('ensure_legacy_cid_memories', )
    connection = db_connection()
    try:
        rows = connection.execute(
            """SELECT * FROM memory_items
            WHERE owner_id = ? AND status = 'confirmed'
              AND sensitivity = 'normal'
              AND (scope = 'global' OR (scope = 'project' AND project_id = ?))
              AND (expires_at = '' OR expires_at > ?)
            ORDER BY pinned DESC, updated_at DESC LIMIT 300""",
            (MEMORY_OWNER_ID, project_id, now_iso()),
        ).fetchall()
        terms = query_terms(query)
        pinned: list[tuple[float, sqlite3.Row, str]] = []
        matched: list[tuple[float, sqlite3.Row, str]] = []
        for row in rows:
            haystack = f"{row['memory_key']} {row['content']} {row['value_json']}".lower()
            hit_terms = [term for term in terms if term in haystack]
            score = (34 if row["scope"] == "project" else 16)
            score += float(row["confidence"] or 0) * 20 + min(int(row["use_count"] or 0), 5) + len(hit_terms) * 12
            # 命中原因要能说给人听。记忆是整个工作台唯一会静默改变回答的东西，
            # 看不见它凭什么被选中，就没法判断一个奇怪的回答是不是它带偏的。
            # query_terms 会把中文切成二元片段，所以这里只展示最长的几个——
            # 「服务」这种碎片作为理由没有意义，「服务器磁盘」才有。
            reason = "置顶记忆，每轮都会带上" if row["pinned"] else _app_call('memory_match_reason', hit_terms, haystack)
            if row["pinned"]:
                pinned.append((score, row, reason))
            elif hit_terms:
                matched.append((score, row, reason))
        sort_key = lambda triple: (triple[0], str(triple[1]["updated_at"]))
        pinned.sort(key=sort_key, reverse=True)
        matched.sort(key=sort_key, reverse=True)
        maximum = max(1, min(int(limit), MAX_MEMORY_CONTEXT_ITEMS))
        selected = list(pinned[: min(MAX_MEMORY_PINNED_ITEMS, maximum)])
        if not core_only:
            matched_limit = min(MAX_MEMORY_MATCHED_ITEMS, maximum - len(selected))
            selected.extend(matched[:matched_limit])
        results = []
        for score, row, reason in selected:
            item = _app_call('memory_item_row', row)
            item["match_reason"] = reason
            item["match_score"] = round(float(score), 1)
            results.append(item)
        return results
    finally:
        connection.close()


def memory_context_for_llm(project_id: str, message: str, *, core_only: bool = False) -> dict[str, Any]:
    candidates = _app_call('retrieve_memories', message, project_id=project_id, core_only=core_only)
    empty_stats = {
        "items": 0,
        "chars": 0,
        "pinned": 0,
        "matched": 0,
        "calls": 0,
        "max_items": MAX_MEMORY_CONTEXT_ITEMS,
        "max_chars": MAX_MEMORY_CONTEXT_CHARS,
        "core_only": bool(core_only),
    }
    if not candidates:
        return {"text": "", "items": [], "refs": [], "stats": empty_stats}
    header = [
        "以下是用户已确认且与本轮相关的少量长期记忆，只用于调整表达、排序和执行偏好；不能代替当前项目数据或外部事实。",
        "本轮明确要求与旧记忆冲突时，以本轮为准。",
    ]
    lines = list(header)
    packed: list[dict[str, Any]] = []
    for item in candidates:
        content = str(item.get("content") or "")
        if len(content) > MAX_MEMORY_ITEM_CONTEXT_CHARS:
            content = content[: MAX_MEMORY_ITEM_CONTEXT_CHARS - 1].rstrip() + "…"
        scope_label = "全局" if item["scope"] == "global" else f"项目:{item['project_id']}"
        line = f"- [{scope_label}/{item['kind_label']}] {content}"
        if len("\n".join([*lines, line])) > MAX_MEMORY_CONTEXT_CHARS:
            continue
        lines.append(line)
        packed.append(item)
    items = packed
    if not items:
        return {"text": "", "items": [], "refs": [], "stats": empty_stats}
    text = "\n".join(lines)
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.executemany(
            "UPDATE memory_items SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
            [(timestamp, item["id"]) for item in items],
        )
        connection.commit()
    finally:
        connection.close()
    refs = [
        {"id": item["id"], "content": str(item["content"])[:MAX_MEMORY_ITEM_CONTEXT_CHARS], "scope": item["scope"], "project_id": item["project_id"], "kind": item["kind"], "kind_label": item.get("kind_label", ""), "confidence": item["confidence"], "pinned": bool(item["pinned"]), "reason": item.get("match_reason", ""), "score": item.get("match_score")}
        for item in items
    ]
    stats = {
        **empty_stats,
        "items": len(items),
        "chars": len(text),
        "pinned": sum(1 for item in items if item.get("pinned")),
        "matched": sum(1 for item in items if not item.get("pinned")),
        "calls": 1,
    }
    return {"text": text, "items": items, "refs": refs, "stats": stats}


def workbuddy_memory_preview() -> list[dict[str, Any]]:
    import app as _app
    path = _app.ROOT / ".workbuddy" / "memory" / "MEMORY.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    in_preferences = False
    candidates: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "## 用户偏好":
            in_preferences = True
            continue
        if in_preferences and stripped.startswith("## "):
            break
        if in_preferences and stripped.startswith("- "):
            content = stripped[2:].strip()
            if content and not _app_call('_memory_is_secret_like', content):
                candidates.append({"content": clip(content, 1_000), "kind": _app_call('_memory_kind_for_text', content), "source": str(path)})
    return candidates[:30]


def import_workbuddy_memories() -> list[dict[str, Any]]:
    imported = []
    for candidate in _app_call('workbuddy_memory_preview', ):
        digest = hashlib.sha256(candidate["content"].encode("utf-8", errors="ignore")).hexdigest()[:20]
        imported.append(_app_call('create_memory_item', 
            content=candidate["content"], scope="global", kind=candidate["kind"], memory_key=f"workbuddy:{digest}",
            status="confirmed", confidence=1.0, source_type="workbuddy", source_id="MEMORY.md",
        ))
    return imported

class MemoryArchiveRequest(BaseModel):
    memory_ids: list[str] = Field(default_factory=list, max_length=200)


@app.get("/api/memories/hygiene")
def get_memory_hygiene(limit: int = 40) -> dict[str, Any]:
    """记忆体检：哪些记忆从没被用过、很久没用、或者互相重复。"""
    return _app_call('memory_hygiene', limit)


@app.post("/api/memories/archive")
def post_memory_archive(request: MemoryArchiveRequest) -> dict[str, Any]:
    if not request.memory_ids:
        raise HTTPException(400, "请至少选择一条记忆")
    return {"ok": True, **_app_call('archive_memory_items', request.memory_ids)}


@app.get("/api/memories")
def get_memories(status: str = "active", project_id: str = "", limit: int = 200) -> dict[str, Any]:
    if status not in {*MEMORY_STATUSES, "all", "active"}:
        raise HTTPException(400, "不支持的记忆状态")
    _app_call('ensure_legacy_cid_memories', )
    return {
        "items": _app_call('list_memory_items', status=status, project_id=project_id.strip(), limit=limit),
        "summary": _app_call('memory_summary', ),
        "policy": "只有已确认记忆会进入 Agent 上下文；候选记忆必须由你确认。凭据和敏感个人信息不会保存。",
    }


@app.post("/api/memories")
def create_memory(request: MemoryCreateRequest) -> dict[str, Any]:
    try:
        item = _app_call('create_memory_item', 
            content=request.content,
            scope=request.scope,
            project_id=request.project_id,
            kind=request.kind,
            memory_key=request.memory_key,
            value=request.value,
            status=request.status,
            confidence=request.confidence,
            pinned=request.pinned,
            source_type="user",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "item": item, "summary": _app_call('memory_summary', )}


@app.patch("/api/memories/{memory_id}")
def update_memory(memory_id: str, request: MemoryUpdateRequest) -> dict[str, Any]:
    updates = request.model_dump(exclude_none=True) if hasattr(request, "model_dump") else request.dict(exclude_none=True)
    try:
        item = _app_call('update_memory_item', memory_id, updates)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not item:
        raise HTTPException(404, "记忆不存在")
    return {"ok": True, "item": item, "summary": _app_call('memory_summary', )}


@app.post("/api/memories/{memory_id}/confirm")
def confirm_memory(memory_id: str) -> dict[str, Any]:
    item = _app_call('set_memory_status', memory_id, "confirmed")
    if not item:
        raise HTTPException(404, "记忆不存在")
    return {"ok": True, "item": item, "summary": _app_call('memory_summary', )}


@app.post("/api/memories/{memory_id}/reject")
def reject_memory(memory_id: str) -> dict[str, Any]:
    item = _app_call('set_memory_status', memory_id, "rejected")
    if not item:
        raise HTTPException(404, "记忆不存在")
    return {"ok": True, "item": item, "summary": _app_call('memory_summary', )}


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str) -> dict[str, Any]:
    if not _app_call('delete_memory_item', memory_id):
        raise HTTPException(404, "记忆不存在")
    return {"ok": True, "deleted": True, "summary": _app_call('memory_summary', )}


@app.get("/api/memories-import/workbuddy")
async def preview_workbuddy_memory_import() -> dict[str, Any]:
    return {
        "items": _app_call('workbuddy_memory_preview', ),
        "policy": "这里只预览 MEMORY.md 的“用户偏好”段落；服务器、部署和环境信息不会导入。",
    }


@app.post("/api/memories-import/workbuddy")
def import_workbuddy_memory(request: MemoryImportRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(409, "请先确认预览内容，再导入已有偏好")
    imported = _app_call('import_workbuddy_memories', )
    return {"ok": True, "items": imported, "summary": _app_call('memory_summary', )}


