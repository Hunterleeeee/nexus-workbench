"""Workbench Web Push 领域：VAPID 密钥、订阅管理、推送投递。

从 app.py 拆出的领域模块（为开源准备）。依赖只有 app_pkg.core（路径常量/
now_iso/log）与 app_pkg.db（db_connection），零业务耦合。

路由函数仍留在 app.py（需要 app 实例），这里只搬服务函数；
app.py 通过 `from app_pkg.push import *` 拿到这些符号。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from .core import VAPID_PRIVATE_KEY_FILE, WORKBENCH_PUBLIC_URL, clip, log, now_iso
from .instance import app
from .db import db_connection
from .llm import valid_http_url

def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def vapid_private_key_source() -> str:
    configured_file = os.getenv("WORKBENCH_VAPID_PRIVATE_KEY_FILE", "").strip()
    path = Path(configured_file).expanduser() if configured_file else VAPID_PRIVATE_KEY_FILE
    try:
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return "file"
    except OSError:
        pass
    return "env" if os.getenv("WORKBENCH_VAPID_PRIVATE_KEY", "").strip() else ""


def vapid_private_key_configured() -> bool:
    return bool(_app_call('vapid_private_key_source', ))


def _push_error_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _push_delivery_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["attempts"] = int(item.get("attempts") or 0)
    return item


def deliver_push(
    subscription: sqlite3.Row | dict[str, Any],
    *,
    title: str,
    body: str,
    href: str = "/",
    event_key: str = "",
    delivery_id: int = 0,
) -> dict[str, Any]:
    """Send one Web Push and persist the result without leaking credentials."""
    subscription_id = int(subscription["id"])
    timestamp = now_iso()
    connection = db_connection()
    try:
        if delivery_id:
            row = connection.execute("SELECT * FROM push_deliveries WHERE id = ?", (delivery_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Push 送达记录不存在")
            attempts = int(row["attempts"] or 0) + 1
            connection.execute("UPDATE push_deliveries SET status = 'queued', attempts = ?, error = '', updated_at = ? WHERE id = ?", (attempts, timestamp, delivery_id))
        else:
            cursor = connection.execute(
                "INSERT INTO push_deliveries(subscription_id, event_key, title, body, href, status, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', 1, ?, ?)",
                (subscription_id, event_key[:240], title[:240], body[:2_000], href[:2_000], timestamp, timestamp),
            )
            delivery_id = int(cursor.lastrowid)
        connection.commit()
    finally:
        connection.close()

    # 优先读取 data/vapid_private.pem（systemd EnvironmentFile 装载 PEM 多行字符串容易转义出错）；
    # 兼容旧的 WORKBENCH_VAPID_PRIVATE_KEY 环境变量（base64url DER 或 PEM）。
    configured_file = os.getenv("WORKBENCH_VAPID_PRIVATE_KEY_FILE", "").strip()
    pem_path = Path(configured_file).expanduser() if configured_file else VAPID_PRIVATE_KEY_FILE
    private_key = ""
    if pem_path.exists():
        try:
            private_key = pem_path.read_text(encoding="utf-8").strip()
        except Exception as exc:
            error = f"读取 VAPID 私钥文件失败：{exc}"
            _app_call('_finish_push_delivery', delivery_id, subscription_id, "failed", error, disable=False)
            return {"id": delivery_id, "status": "failed", "error": error}
    if not private_key:
        private_key = os.getenv("WORKBENCH_VAPID_PRIVATE_KEY", "").strip()
    subject = os.getenv("WORKBENCH_VAPID_SUBJECT", "mailto:workbench@localhost").strip()
    if not private_key:
        error = "尚未配置 VAPID 私钥"
        _app_call('_finish_push_delivery', delivery_id, subscription_id, "failed", error, disable=False)
        return {"id": delivery_id, "status": "failed", "error": error}
    try:
        from pywebpush import webpush
    except ImportError:
        error = "当前环境未安装 pywebpush"
        _app_call('_finish_push_delivery', delivery_id, subscription_id, "failed", error, disable=False)
        return {"id": delivery_id, "status": "failed", "error": error}
    # 若仍取到的是裸 base64url DER（无头尾），自动包装成 PKCS8 PEM（兼容旧 .env 值）
    if not private_key.startswith("-----BEGIN"):
        try:
            import base64
            padding = "=" * ((4 - len(private_key) % 4) % 4)
            der_bytes = base64.urlsafe_b64decode(private_key + padding)
            pem_lines = ["-----BEGIN PRIVATE KEY-----"]
            for index in range(0, len(der_bytes), 64):
                pem_lines.append(der_bytes[index:index + 64].decode("latin-1"))
            pem_lines.append("-----END PRIVATE KEY-----")
            private_key = "\n".join(pem_lines)
        except Exception as exc:
            error = f"VAPID 私钥格式无法识别：{exc}"
            _app_call('_finish_push_delivery', delivery_id, subscription_id, "failed", error, disable=False)
            return {"id": delivery_id, "status": "failed", "error": error}
    # pywebpush 1.14.1 期望 vapid_private_key 是文件路径或 Vapid 实例（PEM 字符串会被当路径处理）。
    # 每次发送写到临时 PEM 文件，确保并发安全（用 delivery_id 隔离）。
    import tempfile
    try:
        _tmp_pem = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False, encoding="ascii")
        _tmp_pem.write(private_key)
        _tmp_pem.close()
        vapid_key_path = _tmp_pem.name
    except Exception as exc:
        error = f"写入 VAPID 临时文件失败：{exc}"
        _app_call('_finish_push_delivery', delivery_id, subscription_id, "failed", error, disable=False)
        return {"id": delivery_id, "status": "failed", "error": error}
    try:
        push_kwargs: dict[str, Any] = {
            "subscription_info": {"endpoint": subscription["endpoint"], "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]}},
            "data": json.dumps({"title": title, "body": body, "href": href}, ensure_ascii=False),
            "vapid_private_key": vapid_key_path,
            "vapid_claims": {"sub": subject},
        }
        # 可选代理：服务器直连 Google FCM 被墙时，通过 SSH 反向隧道转发到本机代理。
        # 配置 WORKBENCH_PUSH_PROXY=http://127.0.0.1:15236 后走代理发送。
        push_proxy = os.getenv("WORKBENCH_PUSH_PROXY", "").strip()
        if push_proxy:
            import requests
            session = requests.Session()
            session.proxies = {"http": push_proxy, "https": push_proxy}
            push_kwargs["requests_session"] = session
        webpush(**push_kwargs)
        os.unlink(vapid_key_path)
        _app_call('_finish_push_delivery', delivery_id, subscription_id, "sent", "", disable=False)
        return {"id": delivery_id, "status": "sent"}
    except Exception as exc:
        try:
            os.unlink(vapid_key_path)
        except OSError:
            pass
        status_code = _app_call('_push_error_status', exc)
        expired = status_code in {404, 410}
        error = clip(str(exc), 500)
        _app_call('_finish_push_delivery', delivery_id, subscription_id, "expired" if expired else "failed", error, disable=expired)
        return {"id": delivery_id, "status": "expired" if expired else "failed", "error": error, "http_status": status_code}


def _finish_push_delivery(delivery_id: int, subscription_id: int, status: str, error: str, *, disable: bool) -> None:
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            "UPDATE push_deliveries SET status = ?, error = ?, sent_at = ?, updated_at = ? WHERE id = ?",
            (status, error, timestamp if status == "sent" else "", timestamp, delivery_id),
        )
        if status == "sent":
            connection.execute("UPDATE push_subscriptions SET failure_count = 0, last_error = '', last_sent_at = ?, updated_at = ? WHERE id = ?", (timestamp, timestamp, subscription_id))
        else:
            connection.execute(
                "UPDATE push_subscriptions SET enabled = ?, failure_count = failure_count + 1, last_error = ?, last_failed_at = ?, updated_at = ? WHERE id = ?",
                (0 if disable else 1, error, timestamp, timestamp, subscription_id),
            )
        connection.commit()
    finally:
        connection.close()


def list_push_deliveries(limit: int = 80) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT d.*, s.endpoint, s.user_agent FROM push_deliveries d LEFT JOIN push_subscriptions s ON s.id = d.subscription_id ORDER BY d.created_at DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [_app_call('_push_delivery_row', row) for row in rows]
    finally:        connection.close()


async def _push_to_all_subscriptions(title: str, body: str, href: str = "/", event_key: str = "") -> dict[str, Any]:
    """向所有启用的浏览器订阅发送一条 Web Push（线程池内执行，不阻塞事件循环）。"""
    connection = db_connection()
    try:
        rows = connection.execute("SELECT * FROM push_subscriptions WHERE enabled = 1 ORDER BY updated_at DESC").fetchall()
    finally:
        connection.close()
    if not rows:
        return {"ok": False, "sent": 0, "message": "没有启用的浏览器订阅"}
    sent = 0
    errors: list[str] = []
    for row in rows:
        try:
            result = await asyncio.to_thread(deliver_push, row, title=title, body=body, href=href, event_key=event_key)
            if result.get("status") == "sent":
                sent += 1
            elif result.get("error"):
                errors.append(str(result["error"])[:120])
        except Exception as exc:
            errors.append(str(exc)[:120])
    return {"ok": sent > 0 or not errors, "sent": sent, "errors": errors[:3], "message": f"已发送 {sent} 条推送"}



class PushSubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2_000)
    keys: dict[str, str] = Field(default_factory=dict)
    user_agent: str = Field(default="", max_length=500)
    quiet_start: str = Field(default="22:00", max_length=5)
    quiet_end: str = Field(default="08:00", max_length=5)
    enabled: bool = True

@app.get("/api/push/subscriptions")
def get_push_subscriptions() -> dict[str, Any]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT id, endpoint, user_agent, enabled, quiet_start, quiet_end, failure_count, last_error, last_sent_at, last_failed_at, created_at, updated_at FROM push_subscriptions ORDER BY updated_at DESC").fetchall()
        return {"subscriptions": [dict(row) | {"enabled": bool(row["enabled"])} for row in rows], "configured": _app_call('vapid_private_key_configured', ), "private_key_source": _app_call('vapid_private_key_source', ), "proxy_configured": bool(os.getenv("WORKBENCH_PUSH_PROXY", "").strip())}
    finally:
        connection.close()


@app.get("/api/push/config")
async def get_push_config() -> dict[str, Any]:
    """Expose only the public VAPID key and delivery readiness to the browser."""
    return {
        "configured": _app_call('vapid_private_key_configured', ),
        "private_key_source": _app_call('vapid_private_key_source', ),
        "public_key": os.getenv("WORKBENCH_VAPID_PUBLIC_KEY", "").strip(),
        "subject": os.getenv("WORKBENCH_VAPID_SUBJECT", "mailto:workbench@localhost").strip(),
        "proxy_configured": bool(os.getenv("WORKBENCH_PUSH_PROXY", "").strip()),
    }


@app.post("/api/push/subscriptions")
def save_push_subscription(request: PushSubscriptionRequest) -> dict[str, Any]:
    if not valid_http_url(request.endpoint):
        raise HTTPException(400, "Push endpoint 必须是 http/https 地址")
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            "INSERT INTO push_subscriptions(endpoint, p256dh, auth, user_agent, enabled, quiet_start, quiet_end, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, user_agent=excluded.user_agent, enabled=excluded.enabled, quiet_start=excluded.quiet_start, quiet_end=excluded.quiet_end, updated_at=excluded.updated_at",
            (request.endpoint, str(request.keys.get("p256dh") or ""), str(request.keys.get("auth") or ""), request.user_agent, int(request.enabled), request.quiet_start, request.quiet_end, timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT id, endpoint, user_agent, enabled, quiet_start, quiet_end, failure_count, last_error, last_sent_at, last_failed_at, created_at, updated_at FROM push_subscriptions WHERE endpoint = ?", (request.endpoint,)).fetchone()
        return {"subscription": dict(row) | {"enabled": bool(row["enabled"])}, "delivery": "vapid-ready" if _app_call('vapid_private_key_configured', ) else "stored-awaiting-vapid"}
    finally:
        connection.close()


@app.delete("/api/push/subscriptions")
def delete_push_subscription(endpoint: str) -> dict[str, Any]:
    connection = db_connection()
    try:
        connection.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        connection.commit()
        return {"ok": True}
    finally:
        connection.close()



@app.get("/api/push/deliveries")
def get_push_deliveries(limit: int = 80) -> dict[str, Any]:
    return {"deliveries": _app_call('list_push_deliveries', limit), "policy": "仅保存送达状态、失败摘要和订阅状态，不保存 VAPID 私钥。"}


@app.post("/api/push/deliveries/{delivery_id}/retry")
async def retry_push_delivery(delivery_id: int) -> dict[str, Any]:
    connection = db_connection()
    try:
        row = connection.execute("SELECT d.*, s.endpoint, s.p256dh, s.auth, s.enabled FROM push_deliveries d JOIN push_subscriptions s ON s.id = d.subscription_id WHERE d.id = ?", (delivery_id,)).fetchone()
    finally:
        connection.close()
    if not row:
        raise HTTPException(404, "Push 送达记录不存在")
    if not row["endpoint"]:
        raise HTTPException(409, "对应订阅已不存在，无法重试")
    subscription = dict(row)
    result = await asyncio.to_thread(deliver_push, subscription, title=row["title"], body=row["body"], href=row["href"], event_key=row["event_key"], delivery_id=delivery_id)
    return {"ok": result.get("status") == "sent", "delivery": result}


__all__ = [
    "PushSubscriptionRequest",
    "_finish_push_delivery",
    "_push_delivery_row",
    "_push_error_status",
    "_push_to_all_subscriptions",
    "delete_push_subscription",
    "deliver_push",
    "get_push_config",
    "get_push_deliveries",
    "get_push_subscriptions",
    "list_push_deliveries",
    "retry_push_delivery",
    "save_push_subscription",
    "vapid_private_key_configured",
    "vapid_private_key_source",
]
