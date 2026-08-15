"""飞书事件处理领域。

拆自 app.py（2026-08-14 第二十一批）。包含: 卡片回调/快捷命令/摘要推送/事件入口。
仍在 app.py 的领域函数经 _app_call 运行时转发。
"""
from __future__ import annotations

import cloud_dev
import asyncio
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any

import feishu as feishu_bot
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from .agent_engine import dispatch_agent_task
from .agent_platform import AgentDispatchRequest, dispatch_agent_task as _dispatch_task
from .agent_runs import get_agent_session
from .aihot import load_aihot_snapshot, select_aihot_items
from .artifacts import list_work_items
from .automations import execute_automation_rule
from .core import DATA_DIR, clip, log, now_iso
from .db import db_connection
from .notifications import create_notification_record, mark_notification_read
from .server import evaluate_server_monitor, read_server_monitor
from .sub2api import analyze_sub2api_snapshot, list_sub2api_history, load_sub2api_snapshot, sub2api_prediction
from .instance import app


def _FEISHU_BOT() -> Any:
    """运行时读 app.feishu_bot（外部 SDK 模块）——测试 patch app.feishu_bot 时生效。"""
    import app as _app

    return _app.feishu_bot


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


async def handle_feishu_card_action(event: dict[str, Any]) -> dict[str, Any]:
    """飞书卡片按钮回调：解析 value 并执行对应动作。

    card.action.trigger 事件里 action.value 是按钮创建时的 value 原样。
    """
    inner = event.get("event") or {}
    action = inner.get("action") or {}
    value = action.get("value") if isinstance(action.get("value"), dict) else {}
    operator = inner.get("operator") or {}
    operator_id = str(((operator.get("operator_id") or {}).get("open_id")) or "")
    chat = inner.get("chat") or {}
    chat_id = str(chat.get("chat_id") or "")

    async def reply(text: str) -> None:
        if chat_id:
            try:
                await _FEISHU_BOT().send_message(chat_id, clip(text, 1800))
            except Exception:
                log.debug("忽略异常（reply）", exc_info=True)

    action_name = str(value.get("action") or "")
    if action_name == "open":
        href = str(value.get("href") or "")
        await reply(f"🔗 {href}\n（在浏览器打开对应页面处理）")
    elif action_name == "dismiss":
        # 真正把对应的应用内通知标记为已读，而不是只回一句话。
        try:
            notification_id = int(value.get("notification_id") or 0)
        except (TypeError, ValueError):
            notification_id = 0
        if notification_id:
            try:
                mark_notification_read(notification_id)
            except Exception:
                log.debug("忽略异常（handle_feishu_card_action）", exc_info=True)
        await reply("✅ 已标记为已读。")
    elif action_name == "retry_automation":
        try:
            rule_id = int(value.get("rule_id") or 0)
        except (TypeError, ValueError):
            rule_id = 0
        if rule_id:
            await reply("🔄 正在重试这条自动化规则…")
            try:
                result = await execute_automation_rule(rule_id, trigger=f"feishu-card-{operator_id[:8]}")
                await reply(f"✅ 重试完成：{clip(str(result.get('result') or result.get('run') or 'ok'), 400)}")
            except Exception as exc:
                await reply(f"⚠️ 重试失败：{clip(str(exc), 300)}")
        else:
            await reply("⚠️ 缺少规则编号，无法重试。")
    else:
        await reply(f"收到卡片操作：{action_name or '未知'}")
    return {"code": 0, "msg": "ok"}


async def feishu_quick_command(text: str, chat_id: str) -> bool:
    """飞书快捷命令（I）：/help /今天 /服务器 /额度 /新机会 直接回摘要。

    返回 True 表示已处理（不应继续走总调度）。
    """
    command = str(text or "").strip().lower()
    mapping = {
        "/help": ("可用命令", "可用命令：\n/今天 今日待办与动态\n/服务器 服务器健康\n/额度 Sub2API 额度\n/新机会 最近项目机会\n/热点 最新 AI 热点\n直接发任务也行，我会自己判断。"),
        "/今天": "today",
        "/today": "today",
        "/服务器": "server",
        "/server": "server",
        "/额度": "sub2api",
        "/quota": "sub2api",
        "/新机会": "opportunities",
        "/热点": "aihot",
        "/aihot": "aihot",
    }
    if command not in mapping:
        return False
    target = mapping[command]
    if target == "today":
        reply_text = await feishu_summary_today()
    elif target == "server":
        reply_text = await feishu_summary_server()
    elif target == "sub2api":
        reply_text = await feishu_summary_sub2api()
    elif target == "opportunities":
        reply_text = await feishu_summary_opportunities()
    elif target == "aihot":
        reply_text = await feishu_summary_aihot()
    else:
        reply_text = str(mapping[command])
    try:
        await _FEISHU_BOT().send_message(chat_id, clip(reply_text, 1800))
    except Exception:
        log.debug("忽略异常（feishu_quick_command）", exc_info=True)
    return True


async def feishu_cloud_dev_command(text: str, chat_id: str) -> bool:
    """Handle the explicit ``云开发`` grammar before general Agent routing."""
    raw = str(text or "").strip()
    if not raw.lower().startswith("云开发"):
        return False
    parsed = cloud_dev.parse_cloud_dev_command(raw)
    if not parsed.get("ok"):
        await _FEISHU_BOT().send_message(chat_id, f"⚠️ {parsed.get('message') or '云开发命令无效'}")
        return True
    if parsed.get("requires_approval"):
        created = _app_call('create_cloud_dev_approval', parsed, source="workbench")
        await _FEISHU_BOT().send_message(chat_id, f"🛡️ 已进入审批：云开发 {parsed.get('project')} 构建。\n审批编号：{created['approval']['id']}\n审批前不会执行命令；请在 NEXUS 审批中心确认。")
        return True

    await _FEISHU_BOT().send_message(chat_id, f"收到，正在执行固定云开发动作：{parsed.get('project')} · {parsed.get('action')}。")

    async def run() -> None:
        try:
            body = await _app_call('execute_cloud_dev_request', parsed, source="workbench", chat_id=chat_id)
            result = body.get("result") or {}
            status = str(result.get("status") or "unknown")
            if status == "approval_required":
                label = "已生成编辑计划"
                message = f"☁️ 云开发{label}\n动作：修改代码\n摘要：{clip(str(result.get('summary') or ''), 200)}\n涉及 {result.get('edits_count')} 处编辑 · {len(result.get('files') or [])} 个文件\n审批编号：{result.get('approval_id')}\n审批通过前不会改动任何代码，请在 NEXUS 审批中心确认。"
            else:
                label = "完成" if status == "ok" else "未执行/失败"
                message = f"☁️ 云开发{label}\n项目：{parsed.get('project')}\n动作：{parsed.get('action')}\n状态：{status}"
            if result.get("message"):
                message += f"\n{clip(result.get('message'), 500)}"
            if result.get("output"):
                message += f"\n\n输出：\n{clip(result.get('output'), 1200)}"
            await _FEISHU_BOT().send_message(chat_id, clip(message, 3600))
        except Exception as exc:
            await _FEISHU_BOT().send_message(chat_id, f"⚠️ 云开发任务异常：{clip(str(exc), 500)}")

    asyncio.create_task(run(), name=f"feishu-cloud-dev:{chat_id}")
    return True


async def feishu_summary_today() -> str:
    """/今天：待处理工作项 + 最近动态。"""
    items = list_work_items("all", "") or []
    active = [item for item in items if item.get("status") in {"open", "running", "blocked", "failed"}]
    status_names = {"open": "待处理", "running": "处理中", "blocked": "待确认", "failed": "执行失败"}
    if active:
        lines = [f"· {clip(str(item.get('title') or '未命名'), 60)} [{status_names.get(item.get('status'), item.get('status'))}]" for item in active[:6]]
        todo = "今天有 {} 项待处理：\n{}".format(len(active), "\n".join(lines))
    else:
        todo = "今天没有待处理事项 🎉"
    return f"📋 {todo}\n（发 /服务器 /额度 /新机会 查看更多，或直接说要做的事）"


async def feishu_summary_server() -> str:
    """/服务器：只读快照 + 健康评分 + 容量趋势。"""
    try:
        # The probe shells out to ssh with a 25s timeout; keep it off the loop.
        snapshot = await asyncio.to_thread(_app_call, '_app_call', 'read_server_monitor')
        evaluation = await asyncio.to_thread(_app_call, '_app_call', 'evaluate_server_monitor', snapshot, create_records=False)
    except Exception as exc:
        return f"⚠️ 服务器状态读取失败：{clip(str(exc), 200)}"
    metrics = evaluation.get("metrics") or {}
    disk = metrics.get("disk_used_pct")
    memory = metrics.get("memory_used_pct")
    load = metrics.get("load_1m")
    prediction = evaluation.get("prediction") or {}
    disk_pred = (prediction.get("disk") or {})
    mem_pred = (prediction.get("memory") or {})
    lines = [
        f"健康评分：{evaluation.get('health_score')}/100（{evaluation.get('health_score_label')}）",
        f"磁盘使用：{disk}%" if disk is not None else "磁盘：未知",
        f"内存使用：{memory}%" if memory is not None else "内存：未知",
        f"1 分钟负载：{load}" if load is not None else "",
    ]
    if disk_pred.get("status") == "growing":
        lines.append(f"磁盘趋势：约 {disk_pred.get('days_to_warn')} 天到提醒阈值")
    if mem_pred.get("status") == "growing":
        lines.append(f"内存趋势：约 {mem_pred.get('days_to_warn')} 天到提醒阈值")
    alerts = evaluation.get("alerts") or []
    if alerts:
        lines.append(f"⚠️ {len(alerts)} 个关注项：{alerts[0].get('title')}")
    return "🖥 服务器状态\n" + "\n".join(line for line in lines if line)


async def feishu_summary_sub2api() -> str:
    """/额度：Sub2API 额度 + 预测 + 建议。"""
    try:
        snapshot = load_sub2api_snapshot()
        analysis = analyze_sub2api_snapshot(snapshot)
        history = list_sub2api_history(limit=8)
        prediction = sub2api_prediction(history)
    except Exception as exc:
        return f"⚠️ Sub2API 状态读取失败：{clip(str(exc), 200)}"
    sub = snapshot.get("subscription") or {}
    today = snapshot.get("today") or {}
    lines = [
        f"订阅：{sub.get('name') or '未知'}（{sub.get('provider') or '—'}）",
        f"余额：{snapshot.get('balance') or '—'}",
        f"本周用量：{sub.get('weekly_usage') or '—'} / 剩余 {sub.get('remaining') or '—'}",
        f"今日消耗：{today.get('cost') or '—'}（{today.get('requests') or 0} 次请求）",
        f"到期：{sub.get('expires_at') or '—'}",
    ]
    if prediction.get("available"):
        lines.append(f"预测：{prediction.get('note') or ''}")
        for suggestion in (prediction.get("suggestions") or [])[:2]:
            lines.append(f"💡 {suggestion}")
    return "💳 Sub2API 额度\n" + "\n".join(line for line in lines if line)


async def feishu_summary_opportunities() -> str:
    """/新机会：最近登记的项目机会。"""
    try:
        items = list_work_items("all", "cid-dashboard") or []
    except Exception:
        items = []
    opportunities = [item for item in items if (item.get("metadata") or {}).get("opportunity_key")]
    if not opportunities:
        return "📌 还没有登记的项目机会。去独立开发者看板登记一个吧。"
    lines = []
    for item in opportunities[:6]:
        title = str(item.get("title") or "机会")
        status = str(item.get("status") or "")
        status_label = {"open": "待处理", "running": "处理中", "done": "已完成", "blocked": "待确认"}.get(status, status)
        lines.append(f"· {clip(title, 50)} [{status_label}]")
    return "📌 最近登记的项目机会：\n" + "\n".join(lines) + "\n（发「验证 X」让想法分析跟进）"


async def feishu_summary_aihot() -> str:
    """/热点：最新 AI 热点标题。"""
    try:
        snapshot = load_aihot_snapshot()
        items = select_aihot_items(snapshot, mode="useful", limit=5)
    except Exception:
        return "⚠️ AI 热点读取失败。"
    if not items:
        return "📰 暂时没有 AI 热点。"
    lines = [f"· {clip(str(item.get('title') or '未命名'), 60)}" for item in items[:5]]
    return "📰 最新 AI 热点：\n" + "\n".join(lines) + "\n（发「分析热点 X」深入）"


@app.post("/feishu/event")
async def feishu_event(request: Request) -> dict[str, Any]:
    """飞书事件订阅回调：challenge 验证 + 消息 → 主 Agent 调度 → 回发。

    飞书回调请求由 Nginx 层免 Basic Auth 放行；这里仍做签名/URL 校验。
    """
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError):
        raise HTTPException(400, "invalid json")
    # 调试辅助：最近一次回调的完整原文与请求头写入本地文件，便于排查事件结构。
    if os.getenv("WORKBENCH_FEISHU_DEBUG", "").strip().lower() in {"1", "true", "yes"}:
        try:
            debug_payload = {
                "at": now_iso(),
                "headers": {k: v for k, v in request.headers.items() if k.lower().startswith("x-lark")},
                "body": raw.decode("utf-8", "replace")[:4000],
            }
            with open(DATA_DIR / "feishu_last_event.json", "w", encoding="utf-8") as debug_file:
                json.dump(debug_payload, debug_file, ensure_ascii=False, indent=2)
        except Exception:
            log.debug("忽略异常（feishu_event）", exc_info=True)
    timestamp = str(request.headers.get("x-lark-request-timestamp", ""))
    nonce = str(request.headers.get("x-lark-request-nonce", ""))
    signature = str(request.headers.get("x-lark-signature", ""))
    if not _FEISHU_BOT().authentication_configured():
        raise HTTPException(503, "飞书回调尚未配置 ENCRYPT_KEY 或 VERIFY_TOKEN，拒绝处理未认证请求")
    if _FEISHU_BOT().ENCRYPT_KEY:
        if not _FEISHU_BOT().signature_timestamp_is_fresh(timestamp):
            raise HTTPException(401, "signature timestamp expired")
        if not _FEISHU_BOT().verify_signature(timestamp, nonce, signature, raw):
            raise HTTPException(401, "signature mismatch")
    try:
        event = _FEISHU_BOT().decrypt_event(payload)
    except ImportError as exc:
        raise HTTPException(503, "飞书加密回调依赖未安装，请先安装 requirements.txt") from exc
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "飞书事件解密失败") from exc
    if not _FEISHU_BOT().ENCRYPT_KEY and not _FEISHU_BOT().verify_event_token(event):
        raise HTTPException(401, "invalid verify token")
    # URL 验证（首次配置事件订阅时飞书会发 challenge）
    challenge = event.get("challenge")
    if challenge is not None:
        token = event.get("token") or ""
        if _FEISHU_BOT().VERIFY_TOKEN and token and token != _FEISHU_BOT().VERIFY_TOKEN:
            raise HTTPException(401, "invalid verify token")
        return {"challenge": challenge}
    header = event.get("header") or {}
    event_type = header.get("event_type") or ""
    if not _app_call('claim_feishu_event', event):
        return {"code": 0, "msg": "duplicate"}
    # 兼容两种事件结构：老版顶层 type="event_callback"，新版 schema 2.0 只有 header.event_type。
    legacy_type = str(event.get("type") or "")
    if legacy_type and legacy_type != "event_callback":
        return {"code": 0, "msg": "ignored"}
    # 卡片按钮回调（A：交互卡片按钮点击）
    if event_type == "card.action.trigger":
        return await handle_feishu_card_action(event)
    if event_type != "im.message.receive_v1":
        return {"code": 0, "msg": "ignored"}
    inner = event.get("event") or {}
    text = _FEISHU_BOT().extract_message_text(inner)
    chat_id = _FEISHU_BOT().event_chat_id(inner)
    if not text or not chat_id:
        return {"code": 0, "msg": "empty text"}
    sender_open_id = _FEISHU_BOT().event_sender_open_id(inner)
    await asyncio.to_thread(_app_call, '_app_call', 'bind_feishu_chat', chat_id, sender_open_id, sender_open_id[:40])

    # 快捷命令（I）：/help /今天 /服务器 /额度 /新机会 直接回摘要，不走总调度。
    quick_reply = await feishu_quick_command(text, chat_id)
    if quick_reply:
        return {"code": 0, "msg": "quick"}
    # 明确前缀的云开发命令走固定安全链，不进入自然语言总调度，避免把文本当成 shell。
    cloud_reply = await feishu_cloud_dev_command(text, chat_id)
    if cloud_reply:
        return {"code": 0, "msg": "cloud-dev"}

    # 飞书会话上下文：按 chat_id 维护独立会话，dispatch 时继承最近 5 轮
    # 对话历史，让"你查一下原因"这类指代能关联到上一条消息。
    session_id = f"feishu:{chat_id}"
    if not get_agent_session(session_id):
        try:
            connection = db_connection()
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO agent_sessions (id, project_id, title, status, summary_json, created_at, updated_at) VALUES (?, 'workbench', ?, 'active', '{}', ?, ?)",
                    (session_id, f"飞书对话 · {chat_id[:8]}", now_iso(), now_iso()),
                )
                connection.commit()
            finally:
                connection.close()
        except Exception:
            log.debug("忽略异常（feishu_event）", exc_info=True)

    async def run_dispatch() -> None:
        reply = "收到，正在处理：\n" + clip(text, 200)
        try:
            await _FEISHU_BOT().send_message(chat_id, reply)
        except Exception:
            log.debug("忽略异常（run_dispatch）", exc_info=True)
        try:
            # 飞书入口无法让用户选目标 Agent，route_confirmed=True 让低置信度路由也能继续；
            # 总调度直接读写这条持久 Session，网页端与飞书使用同一套上下文逻辑。
            body = await dispatch_agent_task(
                AgentDispatchRequest(
                    message=text,
                    session_id=session_id,
                    intent="",
                    project_ids=[],
                    context={"source": "feishu", "intent": ""},
                    route_confirmed=True,
                )
            )
            answer = str(body.get("answer") or "处理完成。")
            children = (body.get("children") or [])
            summary = " · ".join(str(item.get("name") or item.get("project_id") or "") for item in children if isinstance(item, dict)) if children else ""
            result_text = f"✅ 完成\n{answer}"
            if summary:
                result_text += f"\n参与：{summary}"
            # 回发上限 4000 字符（飞书文本消息容量充足），超出时给出明确提示，
            # 避免用户看到"话说到一半"的错觉；完整结果留在工作台最近活动/通知。
            result_full = result_text
            result_sent = clip(result_full, 4000)
            if len(result_full) > 4000:
                result_sent += "\n\n……内容较长已截断，完整结果可在工作台「最近活动」查看。"
            await _FEISHU_BOT().send_message(chat_id, result_sent)
        except Exception as exc:
            # 把回发失败原因也留在通知里，便于排查（不再静默吞掉）。
            try:
                await _FEISHU_BOT().send_message(chat_id, f"⚠️ 处理失败：{clip(str(exc), 400)}")
            except Exception as inner_exc:
                try:
                    await asyncio.to_thread(_app_call, '_app_call', 'create_notification_record', 
                        title="飞书回发失败",
                        body=f"dispatch 异常：{clip(str(exc), 300)}；回发失败：{clip(str(inner_exc), 200)}",
                        project_id="workbench",
                        kind="agent_action",
                        level="error",
                        href="/automation",
                        event_key=f"feishu-reply-failed:{chat_id}:{now_iso()}",
                        dedupe_seconds=60,
                    )
                except Exception:
                    log.debug("忽略异常（run_dispatch）", exc_info=True)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop:
        loop.create_task(run_dispatch(), name=f"feishu-dispatch:{chat_id}")
    else:
        asyncio.run(run_dispatch())
    return {"code": 0, "msg": "accepted"}


__all__ = [
    "handle_feishu_card_action",
    "feishu_quick_command",
    "feishu_cloud_dev_command",
    "feishu_summary_today",
    "feishu_summary_server",
    "feishu_summary_sub2api",
    "feishu_summary_opportunities",
    "feishu_summary_aihot",
    "feishu_event",
]
