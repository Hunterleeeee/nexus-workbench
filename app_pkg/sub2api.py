"""Workbench Sub2API 领域：面板快照同步、用量分析、告警。

从 app.py 拆出的领域模块（为开源准备）。依赖 core（快照文件/工具）与 db；
路由仍留 app.py，经 import * 拿到本模块的服务函数。call_llm 仍在 app.py，
这里用延迟转发包装（explain_sub2api_change 用它）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .core import DATA_DIR, SUB2API_SNAPSHOT_FILE, clip, load_json_file, log, now_iso, save_json_atomic
from .db import db_connection


def _snapshot_file():
    """运行时读取快照路径（兼容测试 patch app.SUB2API_SNAPSHOT_FILE）。"""
    import app as _app

    return _app.SUB2API_SNAPSHOT_FILE


def _panel_settings_file():
    """运行时读取面板设置路径（兼容测试 patch app.SUB2API_PANEL_SETTINGS_FILE）。"""
    import app as _app

    return _app.SUB2API_PANEL_SETTINGS_FILE


async def call_llm(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.call_llm（仍在 app.py）。"""
    import app as _app

    return await _app.call_llm(*args, **kwargs)


def register_artifact_safely(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.register_artifact_safely（仍在 app.py）。"""
    import app as _app

    return _app.register_artifact_safely(*args, **kwargs)


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


def worker_lease(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.worker_lease（仍在 app.py）。"""
    import app as _app

    return _app.worker_lease(*args, **kwargs)


def create_work_item_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_work_item_record（work-items 领域仍在 app.py）。"""
    import app as _app

    return _app.create_work_item_record(*args, **kwargs)


def create_relation_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_relation_record（仍在 app.py）。"""
    import app as _app

    return _app.create_relation_record(*args, **kwargs)


def load_market_snapshot() -> dict[str, Any]:
    """延迟转发 app.load_market_snapshot（market 领域仍在 app.py）。"""
    import app as _app

    return _app.load_market_snapshot()


def load_sub2api_snapshot() -> dict[str, Any]:
    try:
        values = json.loads(_snapshot_file().read_text(encoding="utf-8"))
        return values if isinstance(values, dict) else {"logged_in": False, "keys": []}
    except (OSError, json.JSONDecodeError):
        return {"logged_in": False, "keys": []}


def save_sub2api_snapshot(values: dict[str, Any]) -> None:
    save_json_atomic(_snapshot_file(), values, 0o600)


def _sub2api_text(value: Any, limit: int = 240) -> str:
    return clip(str(value).strip(), limit) if value is not None else ""


def _sub2api_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _sub2api_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for pattern in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(timezone.utc)


def _sub2api_quota(value: Any) -> dict[str, Any]:
    raw = _sub2api_text(value, 80)
    numbers = re.findall(r"-?\d[\d,]*(?:\.\d+)?", raw)
    if len(numbers) < 2:
        return {"raw": raw, "used": None, "limit": None, "remaining": None, "used_pct": None, "remaining_pct": None}
    used = float(numbers[0].replace(",", ""))
    limit = float(numbers[1].replace(",", ""))
    remaining = max(0.0, limit - used)
    used_pct = used / limit if limit > 0 else None
    return {
        "raw": raw,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "used_pct": used_pct,
        "remaining_pct": max(0.0, 1 - used_pct) if used_pct is not None else None,
    }


def _sub2api_key_value(value: Any) -> str:
    text = _sub2api_text(value, 48)
    if text and "..." not in text and "*" not in text and len(text) > 16:
        return "[已隐藏]"
    return text


def sanitize_sub2api_client_snapshot_id(value: Any) -> str:
    """Keep only a non-secret retry identifier generated by the browser."""
    text = str(value or "").strip()[:100]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,99}", text):
        return ""
    return text


def sanitize_sub2api_snapshot(values: dict[str, Any]) -> dict[str, Any]:
    """Keep the browser-sync contract deliberately small and never persist full credentials."""
    source = values if isinstance(values, dict) else {}
    safe: dict[str, Any] = {}
    for key in ("source_url", "dashboard_url", "subscription_url", "checked_at", "fetched_at", "error", "source"):
        if key in source:
            safe[key] = _sub2api_text(source.get(key), 500)
    client_snapshot_id = sanitize_sub2api_client_snapshot_id(source.get("client_snapshot_id"))
    if client_snapshot_id:
        safe["client_snapshot_id"] = client_snapshot_id
    safe["logged_in"] = bool(source.get("logged_in"))
    safe["balance"] = _sub2api_text(source.get("balance"), 80)
    for section in ("today", "total"):
        raw = source.get(section) if isinstance(source.get(section), dict) else {}
        safe[section] = {key: _sub2api_text(raw.get(key), 80) for key in ("requests", "cost", "tokens") if key in raw}
    raw_api_keys = source.get("api_keys") if isinstance(source.get("api_keys"), dict) else {}
    safe["api_keys"] = {key: int(raw_api_keys.get(key) or 0) for key in ("total", "active") if str(raw_api_keys.get(key) or "").strip()}
    raw_subscription = source.get("subscription") if isinstance(source.get("subscription"), dict) else {}
    safe["subscription"] = {
        key: _sub2api_text(raw_subscription.get(key), 160)
        for key in ("name", "provider", "status", "expires_at", "remaining", "weekly_usage", "weekly_reset", "monthly_usage", "monthly_reset")
        if raw_subscription.get(key) is not None
    }
    safe_keys: list[dict[str, Any]] = []
    for raw_key in source.get("keys") or []:
        if not isinstance(raw_key, dict):
            continue
        safe_keys.append(
            {
                "name": _sub2api_text(raw_key.get("name"), 80),
                "masked": _sub2api_key_value(raw_key.get("masked") or raw_key.get("key")),
                "group": _sub2api_text(raw_key.get("group"), 100),
                "concurrency": _sub2api_text(raw_key.get("concurrency"), 40),
                "today_cost": _sub2api_text(raw_key.get("today_cost"), 80),
                "month_cost": _sub2api_text(raw_key.get("month_cost"), 80),
                "expires": _sub2api_text(raw_key.get("expires"), 100),
                "status": _sub2api_text(raw_key.get("status"), 40),
            }
        )
    safe["keys"] = safe_keys[:100]
    return safe


def analyze_sub2api_snapshot(snapshot: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    checked_at = snapshot.get("checked_at") or snapshot.get("fetched_at")
    checked_dt = _sub2api_timestamp(checked_at)
    age_seconds = max(0, int((now - checked_dt).total_seconds())) if checked_dt else None
    if checked_dt is None:
        freshness_status, freshness_label = "unknown", "没有同步时间"
    elif age_seconds is not None and age_seconds <= 6 * 3600:
        freshness_status, freshness_label = "fresh", "数据新鲜"
    elif age_seconds is not None and age_seconds <= 24 * 3600:
        freshness_status, freshness_label = "aging", "数据较旧"
    else:
        freshness_status, freshness_label = "stale", "数据已过期"
    subscription = snapshot.get("subscription") if isinstance(snapshot.get("subscription"), dict) else {}
    weekly = _sub2api_quota(subscription.get("weekly_usage"))
    monthly = _sub2api_quota(subscription.get("monthly_usage"))
    remaining_days = _sub2api_number(subscription.get("remaining"))
    if remaining_days is None:
        expires_dt = _sub2api_timestamp(subscription.get("expires_at"))
        remaining_days = max(0, int((expires_dt - now).total_seconds() // 86400)) if expires_dt else None
    fields = {
        "balance": bool(str(snapshot.get("balance") or "").strip()),
        "weekly_quota": weekly["limit"] is not None,
        "monthly_quota": monthly["limit"] is not None,
        "remaining_time": remaining_days is not None or bool(subscription.get("expires_at")),
        "key_usage": bool(snapshot.get("keys")),
    }
    alerts: list[dict[str, Any]] = []
    if not snapshot.get("logged_in"):
        alerts.append({"key": "not_logged_in", "level": "error", "title": "Sub2API 未确认登录", "message": "无法确认当前浏览器登录状态，请重新打开 API Key 页面同步。"})
    if snapshot.get("error"):
        alerts.append({"key": "sync_error", "level": "error", "title": "Sub2API 同步失败", "message": _sub2api_text(snapshot.get("error"), 240)})
    if freshness_status in {"stale", "unknown"}:
        alerts.append({"key": "stale", "level": "warning", "title": "Sub2API 数据需要重新同步", "message": f"{freshness_label}；上次同步：{checked_at or '未知'}。"})
    for name, quota in (("weekly", weekly), ("monthly", monthly)):
        if quota["remaining_pct"] is not None and quota["remaining_pct"] <= 0.2:
            label = "每周" if name == "weekly" else "每月"
            alerts.append({"key": f"{name}_low", "level": "warning", "title": f"Sub2API {label}额度偏低", "message": f"{quota['raw']}，剩余约 {quota['remaining_pct']:.0%}。"})
    if remaining_days is not None and remaining_days <= 14:
        alerts.append({"key": "expires_soon", "level": "warning", "title": "Sub2API 订阅临近到期", "message": f"预计剩余 {remaining_days} 天，到期时间：{subscription.get('expires_at') or '未知'}。"})
    required_fields = [key for key, present in fields.items() if not present]
    status = "error" if any(item["level"] == "error" for item in alerts) else "warning" if alerts else "ok"
    return {
        "status": status,
        "status_label": {"ok": "账户正常", "warning": "需要关注", "error": "同步异常"}[status],
        "freshness": {"status": freshness_status, "label": freshness_label, "checked_at": checked_at or "", "age_seconds": age_seconds},
        "fields": fields,
        "missing_fields": required_fields,
        "weekly": weekly,
        "monthly": monthly,
        "remaining_days": remaining_days,
        "alerts": alerts,
    }


def sub2api_prediction(history: list[dict[str, Any]]) -> dict[str, Any]:
    """基于历史快照做额度消耗预测（纯计算，无 LLM）。

    取最近几条 weekly_remaining_pct 变化估算日均消耗，外推当前每周额度
    预计剩余天数；样本不足时给出观察提示，不做无依据预测。
    """
    points: list[tuple[float, float]] = []
    for item in history[:8]:
        pct = item.get("weekly_remaining_pct")
        if pct is None:
            continue
        try:
            checked = _sub2api_timestamp(item.get("checked_at") or item.get("created_at"))
        except Exception:  # noqa: BLE001
            continue
        if checked:
            points.append((checked.timestamp(), float(pct)))
    if len(points) < 2:
        return {"available": False, "reason": "历史快照不足（至少需要 2 次带周额度剩余的快照）"}
    points.sort(key=lambda point: point[0])
    first_ts, first_pct = points[0]
    last_ts, last_pct = points[-1]
    span_days = max(0.1, (last_ts - first_ts) / 86400.0)
    consumed = max(0.0, first_pct - last_pct)
    daily_rate = consumed / span_days
    if daily_rate <= 0.001:
        return {"available": True, "trend": "stable", "note": "近期每周额度剩余基本持平，未见明显消耗", "remaining_pct": round(last_pct, 1)}
    days_left = last_pct / daily_rate if last_pct > 0 else 0.0
    label = "fast" if daily_rate >= 5 else "normal" if daily_rate >= 1 else "slow"
    note = f"按近 {span_days:.0f} 天的消耗速度（每日约 {daily_rate:.1f}%），当前每周额度预计约 {days_left:.0f} 天后用完。"
    # 策略建议：按剩余天数和趋势给可执行提示，帮助决定是否调整用量。
    suggestions: list[str] = []
    if days_left <= 2:
        suggestions.append("额度预计两天内用完：建议暂停低频任务，优先保留给 Agent 调度和自动化。")
    elif days_left <= 5:
        suggestions.append("额度偏紧（约 5 天内用完）：建议减少非关键研究任务，或考虑提高备用 Provider 的优先级。")
    else:
        suggestions.append("额度按当前节奏够用；如需长期稳定，可提前准备备用 Provider 的额度。")
    if label == "fast":
        suggestions.append("消耗速度较快：可检查是否有高频自动化或重试任务在持续消耗。")
    elif label == "stable":
        suggestions.append("消耗平稳，按现状使用即可。")
    return {
        "available": True,
        "trend": label,
        "daily_rate_pct": round(daily_rate, 2),
        "days_left": round(days_left, 0),
        "remaining_pct": round(last_pct, 1),
        "span_days": round(span_days, 0),
        "note": note,
        "suggestions": suggestions,
    }


_explain_change_cache: dict[str, Any] = {"key": "", "at": 0.0, "text": ""}


async def explain_sub2api_change(history: list[dict[str, Any]]) -> dict[str, Any]:
    """对比最近两次快照，用 LLM 生成一句话变化解释；10 分钟缓存防重复调用。"""
    if len(history) < 2:
        return {"available": False, "explanation": "历史快照不足，暂时无法解释变化。", "cache": False}
    latest = history[0]
    previous = history[1]
    changes = []
    for field, label in (
        ("weekly_usage", "每周用量"),
        ("monthly_usage", "每月用量"),
        ("remaining", "剩余天数"),
        ("balance", "余额"),
    ):
        old_value = previous.get(field, "")
        new_value = latest.get(field, "")
        if old_value != new_value:
            changes.append(f"{label}：{old_value or '未知'} → {new_value or '未知'}")
    if not changes:
        return {"available": True, "explanation": "最近两次快照各项数值没有变化。", "cache": True}
    cache_key = f"{latest.get('checked_at')}|{latest.get('weekly_usage')}|{latest.get('monthly_usage')}"
    if _explain_change_cache["key"] == cache_key and time.time() - _explain_change_cache["at"] < 600:
        return {"available": True, "explanation": _explain_change_cache["text"], "cache": True}
    changes_text = "；".join(changes)
    try:
        answer = await call_llm(
            [
                {"role": "system", "content": "你是 Sub2API 用量分析师。用一句话（不超过 60 字）解释用户 API 账户快照变化，说明可能原因（如业务增长、自动化任务、额度重置等），不确定就说明是推测。"},
                {"role": "user", "content": f"最近两次快照变化：{changes_text}。请给出通俗解释。"},
            ],
            max_tokens=200,
            temperature=0.3,
        )
        text = str(answer or "").strip()
        if not text or len(text) < 4:
            text = f"快照变化：{changes_text}。"
    except Exception:  # noqa: BLE001
        text = f"快照变化：{changes_text}。"
    _explain_change_cache.update({"key": cache_key, "at": time.time(), "text": text})
    return {"available": True, "explanation": text, "cache": False, "changes": changes}


def _panel_unwrap(data: Any) -> Any:
    """Strip the panel's standard {"code":0,"message":"success","data":{...}} wrapper."""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data


def _panel_money(value: Any) -> str:
    try:
        return f"${float(value or 0):.2f}"
    except (TypeError, ValueError):
        return str(value or "")


def _panel_tokens(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return str(value or "")
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(int(number))


def _panel_remaining_days(expires_at: Any) -> str:
    try:
        parsed = _sub2api_timestamp(str(expires_at))
        if parsed:
            days = (parsed - datetime.now(timezone.utc)).days
            return f"{days} 天" if days >= 0 else "已到期"
    except Exception:
        log.debug("忽略异常（_panel_remaining_days）", exc_info=True)
    return ""


def _panel_usage_aggregate(usage: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Aggregate the panel's per-request usage rows into today / total.

    /usage returns {"code":0,"data":{"items":[{model, input_tokens,
    output_tokens, total_cost, actual_cost, created_at, ...}]}}; every row is
    one request. "Today" is matched against the Beijing calendar day because
    the panel timestamps are in +08:00.
    """
    data = _panel_unwrap(usage)
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}, {}
    try:
        import zoneinfo
        today_marker = datetime.now(zoneinfo.ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except Exception:
        today_marker = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today = {"requests": 0, "cost": 0.0, "tokens": 0}
    total = {"requests": len(rows), "cost": 0.0, "tokens": 0}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            cost = float(row.get("total_cost") or row.get("actual_cost") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            tokens = int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
        except (TypeError, ValueError):
            tokens = 0
        total["cost"] += cost
        total["tokens"] += tokens
        if str(row.get("created_at") or "")[:10] == today_marker:
            today["requests"] += 1
            today["cost"] += cost
            today["tokens"] += tokens
    return (
        {"requests": today["requests"], "cost": _panel_money(today["cost"]), "tokens": _panel_tokens(today["tokens"])},
        {"requests": total["requests"], "cost": _panel_money(total["cost"]), "tokens": _panel_tokens(total["tokens"])},
    )


def _panel_keys_list(keys: Any) -> list[dict[str, Any]]:
    """Normalize panel key rows (data.items) into the snapshot keys shape."""
    data = _panel_unwrap(keys)
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = data if isinstance(data, list) else []
    normalized: list[dict[str, Any]] = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        normalized.append(
            {
                "name": str(row.get("name") or ""),
                "masked": _sub2api_key_value(row.get("key") or row.get("masked") or row.get("sk") or row.get("api_key")),
                "group": str(row.get("group") or row.get("group_name") or ""),
                "concurrency": str(row.get("current_concurrency") or row.get("concurrency") or ""),
                "today_cost": str(row.get("today_cost") or ""),
                "month_cost": str(row.get("month_cost") or ""),
                "expires": str(row.get("expires_at") or row.get("expires") or ""),
                "status": str(row.get("status") or ""),
            }
        )
    return normalized


def sub2api_cost_breakdown(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Aggregate numeric costs by the panel's already-sanitized group label."""
    groups: dict[str, dict[str, Any]] = {}
    unpriced_count = 0
    keys = snapshot.get("keys") if isinstance(snapshot.get("keys"), list) else []
    for raw_key in keys[:100]:
        if not isinstance(raw_key, dict):
            continue
        label = _sub2api_text(raw_key.get("group"), 80) or "未分组"
        entry = groups.setdefault(label, {"group": label, "key_count": 0, "today": 0.0, "month": 0.0, "today_count": 0, "month_count": 0})
        entry["key_count"] += 1
        today = _sub2api_number(raw_key.get("today_cost"))
        month = _sub2api_number(raw_key.get("month_cost"))
        if today is None:
            unpriced_count += 1
        else:
            entry["today"] += today
            entry["today_count"] += 1
        if month is not None:
            entry["month"] += month
            entry["month_count"] += 1
    rows = [
        {
            "group": entry["group"],
            "key_count": entry["key_count"],
            "today_cost": round(entry["today"], 6) if entry["today_count"] else None,
            "month_cost": round(entry["month"], 6) if entry["month_count"] else None,
            "priced_today": entry["today_count"],
            "priced_month": entry["month_count"],
        }
        for entry in groups.values()
    ]
    rows.sort(key=lambda item: (item["month_cost"] if item["month_cost"] is not None else -1, item["group"]), reverse=True)
    return {
        "available": bool(rows),
        "groups": rows,
        "totals": {
            "today_cost": round(sum(item["today_cost"] or 0 for item in rows), 6) if any(item["today_cost"] is not None for item in rows) else None,
            "month_cost": round(sum(item["month_cost"] or 0 for item in rows), 6) if any(item["month_cost"] is not None for item in rows) else None,
            "key_count": sum(item["key_count"] for item in rows),
        },
        "unpriced_count": unpriced_count,
        "policy": "只按已脱敏快照中的 Provider/分组和成本字段聚合；缺失成本不按 0 处理，不保存完整 Key。",
    }


def parse_sub2api_panel_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn the Sub2API panel API payloads into the standard snapshot shape.

    Panel responses are wrapped as {"code":0,"message":"success","data":{...}}.
    Endpoints consumed by the server-side sync:
      /auth/me                → user (balance, email)
      /subscriptions/summary  → data.subscriptions[0] (weekly/monthly usage)
      /keys                   → data.items (API keys, masked on save)
      /usage                  → data.items (per-request rows, aggregated)
    """
    me = _panel_unwrap(raw.get("me"))
    if not isinstance(me, dict):
        me = {}
    subs = _panel_unwrap(raw.get("subscriptions") or raw.get("summary") or raw.get("active"))
    sub_list = subs.get("subscriptions") if isinstance(subs, dict) and isinstance(subs.get("subscriptions"), list) else []
    sub = sub_list[0] if sub_list else (subs if isinstance(subs, dict) and "group_name" in subs else {})

    today, total = _panel_usage_aggregate(raw.get("usage"))
    keys = _panel_keys_list(raw.get("keys"))

    subscription: dict[str, Any] = {}
    if sub:
        expires_raw = str(sub.get("expires_at") or "")
        subscription = {
            "name": str(sub.get("group_name") or sub.get("name") or ""),
            "provider": str(sub.get("provider") or ""),
            "status": str(sub.get("status") or ""),
            "expires_at": expires_raw[:16].replace("T", " ") if expires_raw else "",
            "remaining": _panel_remaining_days(expires_raw),
            "weekly_usage": f"{_panel_money(sub.get('weekly_used_usd'))} / {_panel_money(sub.get('weekly_limit_usd'))}",
            "weekly_reset": "",
            "monthly_usage": f"{_panel_money(sub.get('monthly_used_usd'))} / {_panel_money(sub.get('monthly_limit_usd'))}",
            "monthly_reset": "",
        }

    balance = me.get("balance") if "balance" in me else ""
    snapshot: dict[str, Any] = {
        "logged_in": True,
        "checked_at": now_iso(),
        "balance": _panel_money(balance) if isinstance(balance, (int, float)) else str(balance or ""),
        "api_keys": {"total": len(keys), "active": sum(1 for key in keys if str(key.get("status")) == "active")},
        "today": today,
        "total": total,
        "subscription": subscription,
        "keys": keys,
    }
    for key in ("source_url", "dashboard_url", "subscription_url"):
        if raw.get(key):
            snapshot[key] = str(raw.get(key))[:500]
    return snapshot

SUB2API_PANEL_SETTINGS_FILE = DATA_DIR / "sub2api_panel_settings.json"


def load_sub2api_panel_settings() -> dict[str, Any]:
    try:
        values = json.loads(_panel_settings_file().read_text(encoding="utf-8"))
        return values if isinstance(values, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_sub2api_panel_settings(values: dict[str, Any]) -> None:
    save_json_atomic(_panel_settings_file(), values, 0o600)


def sub2api_sync_state() -> dict[str, Any]:
    """Expose a safe, actionable sync status without returning credentials."""
    settings = load_sub2api_panel_settings()
    has_refresh_token = bool(str(settings.get("refresh_token") or "").strip())
    raw = settings.get("sync_state") if isinstance(settings.get("sync_state"), dict) else {}
    if not raw:
        return {
            "status": "connected" if has_refresh_token or settings.get("access_token") else "not_configured",
            "label": "已连接，等待同步" if has_refresh_token else "已登录，需浏览器同步" if settings.get("access_token") else "尚未连接面板",
            "last_attempt_at": "",
            "last_success_at": "",
            "last_error": "",
            "next_action": "点击立即同步一次" if has_refresh_token else "面板未提供可续期凭证，请使用浏览器书签同步或重新登录" if settings.get("access_token") else "登录并连接面板",
        }
    status = str(raw.get("status") or "unknown")
    credential_invalid = bool(raw.get("credential_invalid")) or (
        status == "failed" and any(marker in str(raw.get("last_error") or "") for marker in ("凭证", "token", "登录", "401", "403"))
    )
    labels = {"succeeded": "最近同步成功", "failed": "最近同步失败", "connected": "已连接，等待同步", "not_configured": "尚未连接面板"}
    if credential_invalid:
        labels["failed"] = "面板凭证已失效"
    if status == "connected" and not has_refresh_token and settings.get("access_token"):
        labels["connected"] = "已登录，需浏览器同步"
    if status == "succeeded" and not has_refresh_token:
        labels["succeeded"] = "最近同步成功（需浏览器续传）"
    return {
        "status": status,
        "label": labels.get(status, "同步状态未知"),
        "credential_invalid": credential_invalid,
        "last_attempt_at": str(raw.get("last_attempt_at") or ""),
        "last_success_at": str(raw.get("last_success_at") or ""),
        "last_error": clip(str(raw.get("last_error") or ""), 500),
        "source": str(raw.get("source") or ""),
        "next_action": str(raw.get("next_action") or "点击立即同步一次"),
    }


def update_sub2api_sync_state(status: str, *, source: str = "", error: str = "") -> dict[str, Any]:
    """Persist only operational sync metadata; access/refresh tokens stay untouched."""
    settings = load_sub2api_panel_settings()
    previous = settings.get("sync_state") if isinstance(settings.get("sync_state"), dict) else {}
    timestamp = now_iso()
    error_text = clip(str(error or ""), 500)
    has_refresh_token = bool(str(settings.get("refresh_token") or "").strip())
    credential_invalid = status == "failed" and any(marker in error_text for marker in ("凭证", "token", "登录", "401", "403"))
    if status == "failed":
        if any(marker in error_text for marker in ("凭证", "token", "登录", "401", "403")):
            next_action = "回到上方重新登录并连接面板"
        else:
            next_action = "点击立即同步一次重试；仍失败时重新登录"
    elif status == "succeeded":
        next_action = "服务器会继续按约 30 分钟周期自动同步" if has_refresh_token else "可继续浏览器同步；需要自动同步请重新登录并确认面板提供续期凭证"
    elif status == "connected":
        next_action = "点击立即同步一次" if has_refresh_token else "面板未提供可续期凭证，请使用浏览器书签同步或重新登录"
    elif status == "not_configured":
        next_action = "登录并连接面板"
    else:
        next_action = "点击立即同步一次"
    state = {
        "status": status,
        "last_attempt_at": timestamp,
        "last_success_at": timestamp if status == "succeeded" else str(previous.get("last_success_at") or ""),
        "last_error": error_text,
        "credential_invalid": credential_invalid,
        "source": clip(source, 80),
        "next_action": next_action,
    }
    settings["sync_state"] = state
    try:
        save_sub2api_panel_settings(settings)
    except Exception:
        # Sync status is helpful diagnostics, never a reason to fail a valid snapshot.
        log.debug("忽略异常（update_sub2api_sync_state）", exc_info=True)
    return sub2api_sync_state()


def sub2api_panel_base_url() -> str:
    return os.getenv("SUB2API_PANEL_BASE_URL", "https://sub.chengsir.asia").strip().rstrip("/")

async def panel_refresh_access_token(force_refresh: bool = False) -> str:
    """Return a valid access token for the panel, refreshing it when needed.

    Uses the panel's refresh_token (obtained once at login) to mint a fresh
    access_token via POST /api/v1/auth/refresh; tokens are stored locally so
    the server can keep syncing automatically.
    """
    settings = load_sub2api_panel_settings()
    refresh_token = str(settings.get("refresh_token") or "").strip()
    if not refresh_token:
        raise RuntimeError("未配置面板登录凭证（refresh_token）")
    access = str(settings.get("access_token") or "").strip()
    if access and not force_refresh:
        return access
    base = sub2api_panel_base_url().rstrip("/") + "/api/v1"
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=True) as client:
            response = await client.post(base + "/auth/refresh", json={"refresh_token": refresh_token})
    except httpx.HTTPError as exc:
        raise RuntimeError(f"面板登录凭证刷新失败：{type(exc).__name__}：{str(exc)[:120]}") from exc
    try:
        body = response.json()
    except Exception:
        body = {}
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    new_access = str(data.get("access_token") or data.get("token") or "").strip()
    new_refresh = str(data.get("refresh_token") or "").strip() or refresh_token
    if response.status_code not in (200, 201) or not new_access:
        raise RuntimeError("面板登录凭证已失效，请在 Sub2API 页面重新登录并连接")
    settings["access_token"] = new_access
    settings["refresh_token"] = new_refresh
    save_sub2api_panel_settings(settings)
    return new_access


async def fetch_sub2api_panel_admin() -> dict[str, Any]:
    """Fetch the panel server-side using the saved login token (no browser needed).

    User endpoints come first (a normal account can read them); admin-only
    endpoints are tried last and their 403s never block a successful sync.
    """
    try:
        access = await panel_refresh_access_token()
    except RuntimeError:
        raise
    base = sub2api_panel_base_url().rstrip("/") + "/api/v1"
    headers = {
        "Authorization": f"Bearer {access}",
        "X-Admin-UI-Request": "true",
        "Accept": "application/json",
    }
    endpoints = [
        "/auth/me",
        "/subscriptions/summary",
        "/subscriptions/active",
        "/keys",
        "/usage",
        "/admin/dashboard/stats",
        "/admin/dashboard/snapshot-v2",
        "/admin/usage/stats",
    ]
    results: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    denied_any = False
    async with httpx.AsyncClient(timeout=20, trust_env=True) as client:
        for attempt in (0, 1):
            for path in endpoints:
                try:
                    response = await client.get(base + path, headers=headers)
                    body_text = response.text[:400]
                    is_denied = response.status_code in (401, 403) or "INVALID_TOKEN" in body_text or "UNAUTHORIZED" in body_text.upper()
                    if is_denied:
                        results[path] = {"status": "denied", "detail": f"HTTP {response.status_code}"}
                        denied_any = True
                        continue
                    if response.status_code == 200 and response.headers.get("content-type", "").startswith("application/json"):
                        try:
                            data = response.json()
                        except Exception:
                            data = None
                        if isinstance(data, dict):
                            payload.setdefault(path.split("/")[-1], data)
                            results[path] = {"status": "ok", "detail": f"{len(data)} 字段"}
                        else:
                            results[path] = {"status": "skip", "detail": "响应非 JSON 对象"}
                    elif response.status_code == 404:
                        results[path] = {"status": "skip", "detail": "端点不存在"}
                    else:
                        results[path] = {"status": "skip", "detail": f"HTTP {response.status_code}"}
                except httpx.HTTPError as exc:
                    results[path] = {"status": "error", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
            if not denied_any or attempt == 1:
                break
            try:
                access = await panel_refresh_access_token(force_refresh=True)
                headers["Authorization"] = f"Bearer {access}"
                denied_any = False
            except RuntimeError:
                break
    if not any(item.get("status") == "ok" for item in results.values()):
        denied = [path for path, info in results.items() if info.get("status") == "denied"]
        detail = "登录凭证被面板拒绝" if denied else "；".join(f"{path}={info.get('status')}" for path, info in results.items())
        raise RuntimeError(f"面板接口未返回有效数据（{detail}）")
    payload["source_url"] = f"{sub2api_panel_base_url()}/keys"
    payload["dashboard_url"] = f"{sub2api_panel_base_url()}/dashboard"
    payload["subscription_url"] = f"{sub2api_panel_base_url()}/subscriptions"
    return {"payload": payload, "endpoints": results}


async def auto_sync_sub2api_panel() -> dict[str, Any]:
    """Server-side automatic sync: fetch panel with the saved token and store."""
    try:
        fetched = await fetch_sub2api_panel_admin()
        snapshot, analysis, artifact = record_sub2api_snapshot(
            parse_sub2api_panel_raw(fetched["payload"]), source="panel_admin_auto"
        )
        return {"ok": True, "snapshot": snapshot, "analysis": analysis, "artifact": artifact, "endpoints": fetched["endpoints"], "sync_state": sub2api_sync_state()}
    except Exception as exc:
        state = update_sub2api_sync_state("failed", source="panel_admin_auto", error=str(exc))
        try:
            previous = load_sub2api_snapshot()
            previous.update({"error": str(exc)[:500], "last_sync_error_at": state.get("last_attempt_at", now_iso())})
            save_sub2api_snapshot(previous)
        except Exception:
            log.debug("忽略异常（auto_sync_sub2api_panel）", exc_info=True)
        raise


async def sub2api_auto_sync_loop() -> None:
    """Periodically sync the panel while the process is alive (best effort).

    When no panel credential is saved yet, the loop idles instead of failing;
    on failure the existing healthy snapshot is preserved (only a separate
    ``error`` field is recorded) so a transient error never marks good data
    as stale/error.
    """
    interval = float(os.getenv("SUB2API_AUTO_SYNC_INTERVAL", "1800"))  # seconds, default 30 min
    if interval <= 0:
        return
    while True:
        lease = worker_lease("sync-worker", status="running", metadata={"loop": "sub2api_auto_sync"})
        if lease.get("status") == "held_by_other_instance":
            await asyncio.sleep(min(interval, 60))
            continue
        settings = load_sub2api_panel_settings()
        sync_state = sub2api_sync_state()
        if settings.get("refresh_token") and not sync_state.get("credential_invalid"):
            try:
                await auto_sync_sub2api_panel()
            except Exception as exc:
                try:
                    previous = load_sub2api_snapshot()
                    previous.update({"error": str(exc)[:500], "last_sync_error_at": now_iso()})
                    save_sub2api_snapshot(previous)
                except Exception:
                    log.debug("忽略异常（sub2api_auto_sync_loop）", exc_info=True)
        await asyncio.sleep(interval)




def record_sub2api_snapshot(values: dict[str, Any], source: str = "browser_session", client_snapshot_id: str = "") -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    snapshot = sanitize_sub2api_snapshot(values)
    snapshot["source"] = _sub2api_text(source, 80) or "browser_session"
    client_snapshot_id = sanitize_sub2api_client_snapshot_id(client_snapshot_id or snapshot.get("client_snapshot_id"))
    if client_snapshot_id:
        snapshot["client_snapshot_id"] = client_snapshot_id
    snapshot.setdefault("checked_at", now_iso())
    snapshot["synced_at"] = now_iso()
    analysis = analyze_sub2api_snapshot(snapshot)
    connection = db_connection()
    try:
        if client_snapshot_id:
            rows = connection.execute("SELECT snapshot_json FROM sub2api_snapshots ORDER BY id DESC LIMIT 100").fetchall()
            for row in rows:
                try:
                    previous = json.loads(row["snapshot_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    previous = {}
                if sanitize_sub2api_client_snapshot_id(previous.get("client_snapshot_id")) == client_snapshot_id:
                    existing = sanitize_sub2api_snapshot(previous)
                    existing["_deduplicated"] = True
                    return existing, analyze_sub2api_snapshot(existing), None
        save_sub2api_snapshot(snapshot)
        update_sub2api_sync_state("succeeded", source=source)
        cursor = connection.execute(
            "INSERT INTO sub2api_snapshots (checked_at, status, snapshot_json, created_at) VALUES (?, ?, ?, ?)",
            (snapshot.get("checked_at", ""), analysis["status"], json.dumps(snapshot, ensure_ascii=False), now_iso()),
        )
        connection.commit()
        history_id = cursor.lastrowid
    finally:
        connection.close()
    artifact = register_artifact_safely(
        project_id="sub2api",
        name="sub2api_snapshot.json",
        path=str(_snapshot_file()),
        kind="sub2api_snapshot",
        metadata={"history_id": history_id, "checked_at": snapshot.get("checked_at"), "status": analysis["status"], "source": snapshot.get("source")},
    )
    return snapshot, analysis, artifact


def list_sub2api_history(limit: int = 30) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT * FROM sub2api_snapshots ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
    finally:
        connection.close()
    history = []
    for row in rows:
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
        except json.JSONDecodeError:
            snapshot = {}
        analysis = analyze_sub2api_snapshot(snapshot)
        subscription = snapshot.get("subscription") or {}
        history.append({"id": row["id"], "checked_at": row["checked_at"], "created_at": row["created_at"], "status": row["status"], "weekly_usage": subscription.get("weekly_usage", ""), "monthly_usage": subscription.get("monthly_usage", ""), "remaining": subscription.get("remaining", ""), "expires_at": subscription.get("expires_at", ""), "weekly_remaining_pct": analysis["weekly"].get("remaining_pct"), "monthly_remaining_pct": analysis["monthly"].get("remaining_pct"), "remaining_days": analysis.get("remaining_days")})
    return history


def evaluate_sub2api_alerts(snapshot: dict[str, Any] | None = None, create_records: bool = False) -> dict[str, Any]:
    snapshot = snapshot or load_sub2api_snapshot()
    analysis = analyze_sub2api_snapshot(snapshot)
    created: list[dict[str, Any]] = []
    existing_items = list_work_items("all", "inbox") if create_records else []
    latest_artifact = list_artifacts("sub2api")[0] if list_artifacts("sub2api") else None
    # 恢复闭环：账户恢复正常后，仍挂着的旧告警自动标 done（不重复打扰）
    active_keys = {f"sub2api:{alert['key']}" for alert in analysis["alerts"]}
    restored: list[dict[str, Any]] = []
    for item in list_work_items("all", "sub2api"):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        alert_key = str(metadata.get("alert_key") or "")
        if alert_key.startswith("sub2api:") and item.get("status") in {"open", "running", "blocked"} and alert_key not in active_keys:
            update_work_item_record(item["id"], {"status": "done", "resolved_at": now_iso(), "resolved_by": "sub2api_health_recovery"})
            restored.append({"alert_key": alert_key, "work_item_id": item["id"]})
    for alert in analysis["alerts"]:
        alert_key = f"sub2api:{alert['key']}"
        existing = next((item for item in existing_items if item.get("metadata", {}).get("alert_key") == alert_key and item.get("status") in {"open", "running", "blocked"}), None)
        if existing:
            created.append({"alert": alert, "work_item": existing, "created": False})
            continue
        if not create_records:
            created.append({"alert": alert, "created": False})
            continue



def evaluate_sub2api_alerts(snapshot: dict[str, Any] | None = None, create_records: bool = False) -> dict[str, Any]:
    snapshot = snapshot or load_sub2api_snapshot()
    analysis = analyze_sub2api_snapshot(snapshot)
    created: list[dict[str, Any]] = []
    existing_items = list_work_items("all", "inbox") if create_records else []
    latest_artifact = list_artifacts("sub2api")[0] if list_artifacts("sub2api") else None
    # 恢复闭环：账户恢复正常后，仍挂着的旧告警自动标 done（不重复打扰）
    active_keys = {f"sub2api:{alert['key']}" for alert in analysis["alerts"]}
    restored: list[dict[str, Any]] = []
    for item in list_work_items("all", "sub2api"):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        alert_key = str(metadata.get("alert_key") or "")
        if alert_key.startswith("sub2api:") and item.get("status") in {"open", "running", "blocked"} and alert_key not in active_keys:
            update_work_item_record(item["id"], {"status": "done", "resolved_at": now_iso(), "resolved_by": "sub2api_health_recovery"})
            restored.append({"alert_key": alert_key, "work_item_id": item["id"]})
    for alert in analysis["alerts"]:
        alert_key = f"sub2api:{alert['key']}"
        existing = next((item for item in existing_items if item.get("metadata", {}).get("alert_key") == alert_key and item.get("status") in {"open", "running", "blocked"}), None)
        if existing:
            created.append({"alert": alert, "work_item": existing, "created": False})
            continue
        if not create_records:
            created.append({"alert": alert, "created": False})
            continue
        priority = "urgent" if alert["level"] == "error" else "high"
        item = create_work_item_record(
            title=alert["title"],
            description=f"{alert['message']} 数据时间：{analysis['freshness'].get('checked_at') or '未知'}。",
            kind="alert",
            priority=priority,
            source_project="sub2api",
            target_project="inbox",
            metadata={"alert_key": alert_key, "notification_project": "sub2api", "checked_at": analysis["freshness"].get("checked_at", ""), "source": "sub2api_agent"},
        )
        relation = None
        if latest_artifact:
            relation = create_relation_record(from_type="artifact", from_id=str(latest_artifact["id"]), to_type="work_item", to_id=str(item["id"]), relation_type="alert_from_snapshot", metadata={"alert_key": alert_key})
        existing_items.append(item)
        created.append({"alert": alert, "work_item": item, "relation": relation, "created": True})
    return {"analysis": analysis, "alerts": analysis["alerts"], "created": created, "restored": restored}

__all__ = ["_snapshot_file", "_panel_settings_file", "load_sub2api_snapshot", "save_sub2api_snapshot", "_sub2api_text", "_sub2api_number", "_sub2api_timestamp", "_sub2api_quota", "_sub2api_key_value", "sanitize_sub2api_client_snapshot_id", "sanitize_sub2api_snapshot", "analyze_sub2api_snapshot", "sub2api_prediction", "explain_sub2api_change", "_panel_unwrap", "_panel_money", "_panel_tokens", "_panel_remaining_days", "_panel_usage_aggregate", "_panel_keys_list", "sub2api_cost_breakdown", "parse_sub2api_panel_raw", "load_sub2api_panel_settings", "save_sub2api_panel_settings", "sub2api_sync_state", "update_sub2api_sync_state", "sub2api_panel_base_url", "panel_refresh_access_token", "fetch_sub2api_panel_admin", "auto_sync_sub2api_panel", "sub2api_auto_sync_loop", "record_sub2api_snapshot", "list_sub2api_history", "evaluate_sub2api_alerts", "SUB2API_PANEL_SETTINGS_FILE", "SUB2API_SNAPSHOT_FILE"]
