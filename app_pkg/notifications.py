"""Workbench 通知领域：站内通知 CRUD + ntfy/飞书推送调度。

从 app.py 拆出的领域模块（为开源准备）。依赖 core/db 与 app_pkg.integrations
（send_ntfy_message）；feishu_bot 来自 feishu 模块（app.py 顶部已初始化，重复
import 走缓存）；work_item_row/agent_run_row 仍留 app.py（work-items/agent 领域），
这里用延迟转发。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .core import clip, log, now_iso
from .db import db_connection
from .integrations import send_ntfy_message
import feishu as feishu_bot


def work_item_row(row: sqlite3.Row) -> dict[str, Any]:
    """延迟转发 app.work_item_row（work-items 领域仍在 app.py）。"""
    import app as _app

    return _app.work_item_row(row)


def agent_run_row(row: sqlite3.Row) -> dict[str, Any]:
    """延迟转发 app.agent_run_row（agent 领域仍在 app.py）。"""
    import app as _app

    return _app.agent_run_row(row)


def agent_display_name(project_id: str) -> str:
    """延迟转发 app.agent_display_name（仍在 app.py）。"""
    import app as _app

    return _app.agent_display_name(project_id)


def schedule_ntfy_notification(notification: dict[str, Any]) -> None:
    """Route only actionable in-app alerts to ntfy without blocking the API response."""
    if str(notification.get("level") or "") not in {"critical", "error", "warning"}:
        return
    try:
        status = integration_status("ntfy")
        if not status.get("configured") or not status.get("enabled"):
            return
        loop = asyncio.get_running_loop()
    except (HTTPException, RuntimeError):
        return

    async def deliver() -> None:
        try:
            await send_ntfy_message(title=str(notification.get("title") or "Workbench 提醒"), body=str(notification.get("body") or ""), href=str(notification.get("href") or "/"))
        except Exception as exc:
            update_integration_test("ntfy", "notify_failed", str(exc))

    loop.create_task(deliver(), name=f"ntfy:{notification.get('id', 'notification')}")


def feishu_bindings() -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT * FROM feishu_bindings ORDER BY last_active_at DESC").fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


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


def feishu_notification_rule_id(notification: dict[str, Any]) -> int | None:
    """从通知 event_key 提取 automation rule_id（格式 automation-failed:<rule_id>:<run_id>）。"""
    key = str(notification.get("event_key") or "")
    parts = key.split(":")
    if len(parts) >= 2 and parts[0] in {"automation-failed", "automation-retry"}:
        try:
            return int(parts[1])
        except (TypeError, ValueError):
            return None
    return None


def schedule_feishu_notification(notification: dict[str, Any]) -> None:
    """把应用通知推到已绑定的飞书会话（不阻塞 API 响应）。

    只推 critical/error/warning 告警级；success 级不推——飞书发起的
    dispatch 结果已由 run_dispatch 直接回发，避免"总调度已完成"通知与
    回发内容重复（用户反馈飞书收到两条）。
    """
    if not feishu_bot.configured():
        return
    level = str(notification.get("level") or "")
    if level not in {"critical", "error", "warning"}:
        return
    bound = feishu_bindings()
    if not bound:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def deliver() -> None:
        title = str(notification.get("title") or "Workbench 提醒")
        body = str(notification.get("body") or "")
        href = str(notification.get("href") or "https://workbench.example.dev:8765")
        buttons: list[dict[str, Any]] = []
        if href and href != "/":
            buttons.append({"text": "查看详情", "value": {"action": "open", "href": href}, "primary": True})
        # 自动化失败类通知提供重试按钮
        if "自动化失败" in title:
            rule_id = feishu_notification_rule_id(notification)
            if rule_id:
                buttons.append({"text": "重试", "value": {"action": "retry_automation", "rule_id": rule_id}})
        buttons.append({"text": "标记已读", "value": {"action": "dismiss", "notification_id": notification.get("id")}})
        card_text = body or "点按钮处理。"
        for item in bound:
            try:
                await feishu_bot.send_card(str(item["chat_id"]), title=title, body=card_text, buttons=buttons, status="warning" if "失败" in title or "异常" in title else "info")
            except Exception:
                continue

    loop.create_task(deliver(), name=f"feishu:{notification.get('id', 'notification')}")


def notification_context(event_key: str, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Recover the useful result behind older notification rows.

    Early versions only stored the work-item title in a notification. Keep
    those rows intact, but expose the linked dispatch/action result so the
    application notification center can explain what actually happened.
    """
    if not event_key:
        return {}
    owned_connection = connection is None
    connection = connection or db_connection()
    try:
        if event_key.startswith("work-item:"):
            try:
                item_id = int(event_key.split(":", 1)[1])
            except (TypeError, ValueError):
                return {}
            row = connection.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                return {}
            item = work_item_row(row)
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            children = metadata.get("children") if isinstance(metadata.get("children"), list) else []
            actions = metadata.get("actions") if isinstance(metadata.get("actions"), list) else [action for child in children if isinstance(child, dict) for action in (child.get("actions") or []) if isinstance(action, dict)]
            return {
                "source": "work_item",
                "work_item_id": item_id,
                "status": item.get("status", ""),
                "answer": clip(str(metadata.get("answer") or ""), 2_400),
                "children": [
                    {
                        "project_id": child.get("project_id", ""),
                        "name": child.get("name", ""),
                        "answer": clip(str(child.get("answer", "")), 1_800),
                    }
                    for child in children
                    if isinstance(child, dict)
                ],
                "actions": actions,
                "result": item.get("result") if isinstance(item.get("result"), dict) else {},
            }
        if event_key.startswith("agent-dispatch:"):
            run_id = event_key.split(":", 1)[1]
            row = connection.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return {}
            run = agent_run_row(row)
            result = run.get("result") if isinstance(run.get("result"), dict) else {}
            children = result.get("children") if isinstance(result.get("children"), list) else []
            return {
                "source": "agent_run",
                "run_id": run_id,
                "status": run.get("status", ""),
                "answer": clip(str(result.get("answer", "")), 2_400),
                "children": [
                    {
                        "project_id": child.get("project_id", ""),
                        "name": child.get("name", ""),
                        "answer": clip(str(child.get("answer", "")), 1_800),
                    }
                    for child in children
                    if isinstance(child, dict)
                ],
                "actions": [action for child in children if isinstance(child, dict) for action in (child.get("actions") or []) if isinstance(action, dict)],
            }
    finally:
        if owned_connection:
            connection.close()
    return {}


def notification_row(row: sqlite3.Row, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    notification = {key: row[key] for key in row.keys()}
    notification["unread"] = not bool(notification.get("read_at"))
    notification["project_name"] = agent_display_name(notification.get("project_id", "workbench"))
    context = notification_context(str(notification.get("event_key", "")), connection)
    if context:
        notification["context"] = context
    return notification


def create_notification_record(
    *,
    title: str,
    body: str = "",
    project_id: str = "workbench",
    kind: str = "info",
    level: str = "info",
    href: str = "",
    event_key: str = "",
    dedupe_seconds: int = 900,
) -> dict[str, Any]:
    """Persist a notification; repeated worker events are collapsed in a short window."""
    timestamp = now_iso()
    connection = db_connection()
    try:
        if event_key:
            previous = connection.execute(
                "SELECT * FROM notifications WHERE event_key = ? AND julianday(created_at) >= julianday(?, ?) ORDER BY id DESC LIMIT 1",
                (event_key, timestamp, f"-{max(0, dedupe_seconds)} seconds"),
            ).fetchone()
            if previous:
                return notification_row(previous, connection)
        cursor = connection.execute(
            """INSERT INTO notifications
            (event_key, project_id, kind, level, title, body, href, read_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)""",
            (event_key, project_id, kind, level, clip(title, 160), clip(body, 1_000), href, timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM notifications WHERE id = ?", (cursor.lastrowid,)).fetchone()
        notification = notification_row(row, connection)
    finally:
        connection.close()
    schedule_ntfy_notification(notification)
    schedule_feishu_notification(notification)
    return notification


def list_notifications(*, unread_only: bool = False, limit: int = 30) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        where = " WHERE read_at = ''" if unread_only else ""
        rows = connection.execute(
            f"SELECT * FROM notifications{where} ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)
        ).fetchall()
        return [notification_row(row, connection) for row in rows]
    finally:
        connection.close()


def mark_notification_read(notification_id: int) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        connection.execute("UPDATE notifications SET read_at = ? WHERE id = ?", (now_iso(), notification_id))
        connection.commit()
        row = connection.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
        return notification_row(row, connection) if row else None
    finally:
        connection.close()


def mark_all_notifications_read() -> int:
    connection = db_connection()
    try:
        cursor = connection.execute("UPDATE notifications SET read_at = ? WHERE read_at = ''", (now_iso(),))
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()

__all__ = ["schedule_ntfy_notification", "feishu_bindings", "bind_feishu_chat", "claim_feishu_event", "feishu_notification_rule_id", "schedule_feishu_notification", "notification_context", "notification_row", "create_notification_record", "list_notifications", "mark_notification_read", "mark_all_notifications_read"]
