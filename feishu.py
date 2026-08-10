"""飞书机器人接入：消息事件 → 主 Agent 调度 → 结果回发 + 主动推送。

凭据从环境变量读取（服务器 .env，不进代码仓库）：
  WORKBENCH_FEISHU_APP_ID     飞书自建应用 App ID
  WORKBENCH_FEISHU_APP_SECRET 飞书自建应用 App Secret
  WORKBENCH_FEISHU_VERIFY_TOKEN  事件订阅校验 token（可选，用于 URL 验证）
  WORKBENCH_FEISHU_ENCRYPT_KEY   事件订阅加密 key（可选，配置后需解密事件体）

安全边界：
- 事件订阅 URL 由飞书回调，进入服务器时在 Nginx 层免 Basic Auth。
- 消息解析后只调用 Workbench 主 Agent 的 dispatch 流程，写动作仍走
  fail-closed + 审批确认，不在飞书侧放权。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

import httpx

def _env(primary: str, alias: str = "") -> str:
    """Read the namespaced setting, with a backwards-compatible alias."""
    value = os.getenv(primary, "").strip()
    return value or (os.getenv(alias, "").strip() if alias else "")


APP_ID = _env("WORKBENCH_FEISHU_APP_ID", "FEISHU_APP_ID")
APP_SECRET = _env("WORKBENCH_FEISHU_APP_SECRET", "FEISHU_APP_SECRET")
VERIFY_TOKEN = _env("WORKBENCH_FEISHU_VERIFY_TOKEN", "FEISHU_VERIFY_TOKEN")
ENCRYPT_KEY = _env("WORKBENCH_FEISHU_ENCRYPT_KEY", "FEISHU_ENCRYPT_KEY")

_TOKEN: dict[str, Any] = {"token": "", "expires_at": 0.0}
_token_lock = asyncio.Lock()

BASE = "https://open.feishu.cn/open-apis"
SIGNATURE_MAX_SKEW_SECONDS = 300


def configured() -> bool:
    return bool(APP_ID and APP_SECRET)


async def tenant_access_token() -> str:
    """获取并缓存 tenant_access_token（有效期 2 小时，提前 10 分钟刷新）。"""
    global _TOKEN
    if _TOKEN["token"] and _TOKEN["expires_at"] > time.time() + 600:
        return _TOKEN["token"]
    async with _token_lock:
        if _TOKEN["token"] and _TOKEN["expires_at"] > time.time() + 600:
            return _TOKEN["token"]
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": APP_ID, "app_secret": APP_SECRET},
            )
            response.raise_for_status()
            body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"飞书获取访问令牌失败：{body.get('msg') or body.get('code')}")
        _TOKEN = {"token": str(body.get("tenant_access_token") or ""), "expires_at": time.time() + int(body.get("expire", 7200)) - 300}
        return _TOKEN["token"]


async def send_message(chat_id: str, text: str, message_type: str = "text") -> dict[str, Any]:
    """向指定会话发送文本/富文本消息。"""
    token = await tenant_access_token()
    content = json.dumps({"text": text}, ensure_ascii=False) if message_type == "text" else text
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{BASE}/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": chat_id, "msg_type": message_type, "content": content},
        )
        response.raise_for_status()
        body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"飞书发送消息失败：{body.get('msg') or body.get('code')}")
    return body


def build_action_card(*, title: str, body: str, buttons: list[dict[str, Any]], status: str = "info") -> str:
    """构建飞书交互卡片 JSON（schema 2.0）。

    buttons: [{"text": "查看详情", "value": {"action": "open", "href": "/automation"}}, ...]
    回调事件 card.action.trigger 会把 button.value 原样带回。
    """
    header_color = {"info": "blue", "success": "green", "warning": "orange", "error": "red"}.get(status, "blue")
    elements: list[dict[str, Any]] = []
    if body:
        elements.append({"tag": "markdown", "content": body})
    if buttons:
        actions: list[dict[str, Any]] = []
        for button in buttons:
            value = dict(button.get("value") or {})
            action = {
                "tag": "button",
                "text": {"tag": "plain_text", "content": str(button.get("text") or "按钮")},
                "type": "default",
                "value": value,
            }
            if button.get("primary"):
                action["type"] = "primary"
            elif button.get("danger"):
                action["type"] = "danger"
            actions.append(action)
        elements.append({"tag": "action", "actions": actions})
    return json.dumps(
        {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": header_color,
                "title": {"tag": "plain_text", "content": str(title)[:120]},
            },
            "elements": elements,
        },
        ensure_ascii=False,
    )


async def send_card(chat_id: str, *, title: str, body: str = "", buttons: list[dict[str, Any]] | None = None, status: str = "info") -> dict[str, Any]:
    """向指定会话发送交互卡片。"""
    card_json = build_action_card(title=title, body=body, buttons=buttons or [], status=status)
    return await send_message(chat_id, card_json, message_type="interactive")


async def push_to_user(user_id: str, text: str, message_type: str = "text") -> dict[str, Any]:
    """向指定用户（open_id）单聊推送（用于主动通知）。"""
    token = await tenant_access_token()
    content = json.dumps({"text": text}, ensure_ascii=False) if message_type == "text" else text
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{BASE}/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": user_id, "msg_type": message_type, "content": content},
        )
        response.raise_for_status()
        body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"飞书推送失败：{body.get('msg') or body.get('code')}")
    return body


def verify_signature(timestamp: str, nonce: str, signature: str, raw_body: bytes) -> bool:
    """事件订阅签名校验：sha256(ts + nonce + encrypt_key + body)。"""
    if not ENCRYPT_KEY:
        # 没有 encrypt_key 时由调用方改用 VERIFY_TOKEN 校验；不要把
        # 缺少认证配置误认为“校验通过”。
        return False
    if not signature:
        return False
    string_to_sign = timestamp + nonce + ENCRYPT_KEY + raw_body.decode("utf-8", "replace")
    digest = hashlib.sha256(string_to_sign.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, signature)


def signature_timestamp_is_fresh(timestamp: str, *, now: float | None = None, max_skew: int = SIGNATURE_MAX_SKEW_SECONDS) -> bool:
    """Reject signed callbacks that are too old or too far in the future.

    Feishu's signature includes a timestamp, so checking freshness closes the
    replay window that remains after the seven-day event-receipt dedupe period.
    VERIFY_TOKEN-only callbacks intentionally do not use this rule.
    """
    try:
        received_at = int(str(timestamp or "").strip())
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else float(now)
    return abs(current - received_at) <= max(1, int(max_skew))


def authentication_configured() -> bool:
    """Return whether the public callback has at least one verifier."""
    return bool(ENCRYPT_KEY or VERIFY_TOKEN)


def verify_event_token(payload: dict[str, Any]) -> bool:
    """Verify Feishu's URL/event token when encrypt-key signatures are absent."""
    if not VERIFY_TOKEN:
        return False
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    candidates = (payload.get("token"), header.get("token"), event.get("token"))
    return any(hmac.compare_digest(VERIFY_TOKEN, str(candidate or "")) for candidate in candidates)


def decrypt_event(payload: dict[str, Any]) -> dict[str, Any]:
    """使用 encrypt_key 解密事件体；未配置时原样返回。"""
    if not ENCRYPT_KEY or "encrypt" not in payload:
        return payload
    # cryptography 只在配置了 encrypt_key 时才需要（惰性导入，避免硬依赖）。
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore[import-not-found]

    key_bytes = hashlib.sha256(ENCRYPT_KEY.encode("utf-8")).digest()
    raw = base64.b64decode(str(payload["encrypt"]))
    iv = raw[:16]
    cipher = raw[16:]
    cipher_obj = Cipher(algorithms.AES(key_bytes), modes.CBC(iv))
    decryptor = cipher_obj.decryptor()
    decrypted = decryptor.update(cipher) + decryptor.finalize()
    pad = decrypted[-1]
    if isinstance(pad, int) and 1 <= pad <= 16:
        decrypted = decrypted[:-pad]
    return json.loads(decrypted.decode("utf-8"))


def extract_message_text(event: dict[str, Any]) -> str:
    """从 im.message.receive_v1 事件里提取用户文本。"""
    message = event.get("message") or {}
    message_type = message.get("message_type") or ""
    content = message.get("content") or ""
    if message_type == "text":
        try:
            return str(json.loads(content).get("text") or "").strip()
        except (TypeError, ValueError, KeyError):
            return str(content).strip()
    if message_type == "post":
        try:
            parts: list[str] = []
            post = json.loads(content).get("post") or {}
            for lang in ("zh_cn", "en_us", "default"):
                body = post.get(lang) or {}
                for node in (body.get("content") or []):
                    for seg in node if isinstance(node, list) else []:
                        if isinstance(seg, dict) and seg.get("tag") == "text":
                            parts.append(str(seg.get("text") or ""))
                if parts:
                    break
            return " ".join(parts).strip()
        except (TypeError, ValueError, KeyError):
            return ""
    return ""


def event_chat_id(event: dict[str, Any]) -> str:
    # 真实 im.message.receive_v1 结构里 chat_id 在 message.chat_id，不在顶层 chat。
    message = event.get("message") or {}
    chat = event.get("chat") or {}
    return str(message.get("chat_id") or chat.get("chat_id") or "")


def event_sender_open_id(event: dict[str, Any]) -> str:
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    return str(sender_id.get("open_id") or sender_id.get("user_id") or sender.get("open_id") or "").strip()


def event_receipt_key(payload: dict[str, Any]) -> str:
    """Return a stable key for Feishu retries without using message content.

    Schema 2.0 supplies ``header.event_id``. Older message callbacks may not,
    so the message id is a safe fallback. Prefixing the source avoids an event
    id and a message id with the same text colliding in the receipt table.
    """
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    inner = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = inner.get("message") if isinstance(inner.get("message"), dict) else {}
    for prefix, value in (
        ("event", header.get("event_id") or payload.get("event_id")),
        ("message", message.get("message_id")),
    ):
        text = str(value or "").strip()
        if text:
            return f"{prefix}:{text}"[:240]
    return ""
