"""Workbench LLM 基础设施：模型配置、Provider 健康/回退、用量事件。

从 app.py 拆出的核心基础设施模块（为开源准备）。只依赖 core（路径/日志/时间/
文件工具）与 db；不依赖任何业务领域。call_llm/stream 等调用层与 /api/settings
路由在后续批次并入。
"""

from __future__ import annotations

import asyncio
import httpx
import ipaddress
import json
import math
import os
import re
import sqlite3
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .core import DATA_DIR, KNOWLEDGE_DIR, OUTPUTS_DIR, SETTINGS_FILE, _int_env, clip, log, now_iso, save_json_atomic
from .db import db_connection
from .instance import app

def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用本模块函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def load_saved_llm_settings() -> dict[str, Any]:
    try:
        values = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return values if isinstance(values, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


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





def save_global_llm_settings(values: dict[str, Any]) -> None:
    save_json_atomic(SETTINGS_FILE, values, 0o600)



class LLMSettingsRequest(BaseModel):
    providers: list[dict[str, Any]] = Field(default_factory=list, max_length=20)



class LLMTestRequest(BaseModel):
    provider_id: str = Field(default="", max_length=160)
    name: str = Field(default="", max_length=120)
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=4096)
    model: str = Field(default="", max_length=200)



def _clean_llm_token(value: str) -> str:
    text = value.strip()
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    return text



def _llm_provider_id(value: Any, index: int = 0) -> str:
    """Return a stable, non-secret provider identifier for UI/API merging."""
    candidate = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip()).strip("-").lower()
    return candidate[:120] or f"provider-{index + 1}"



def _llm_provider_usable(provider: dict[str, Any]) -> bool:
    """A provider is callable only when all three routing fields are valid."""
    return bool(
        str(provider.get("api_key") or "").strip()
        and valid_http_url(str(provider.get("base_url") or "").strip())
        and str(provider.get("model") or "").strip()
    )



def _llm_provider_disabled_reason(provider: dict[str, Any]) -> str:
    """Explain why an intentionally saved-but-incomplete entry is skipped."""
    if not str(provider.get("api_key") or "").strip():
        return "未保存 API Key"
    base_url = str(provider.get("base_url") or "").strip()
    if not base_url:
        return "缺少 API 地址"
    if not valid_http_url(base_url):
        return "API 地址必须是 http/https"
    if not str(provider.get("model") or "").strip():
        return "缺少模型名"
    return ""



def _unique_llm_provider_id(value: Any, index: int, used: set[str]) -> str:
    """Keep provider IDs stable while preventing same-name entries colliding."""
    base = _llm_provider_id(value, index)
    candidate = base
    suffix = 2
    while candidate in used:
        tail = f"-{suffix}"
        candidate = f"{base[: max(1, 120 - len(tail))]}{tail}"
        suffix += 1
    used.add(candidate)
    return candidate



def _llm_error_kind(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.RequestError):
        return "network"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return "auth"
        if status == 404:
            return "endpoint"
        if status == 429:
            return "rate_limit"
        if status >= 500:
            return "upstream"
        return f"http_{status}"
    if "返回为空" in str(exc):
        return "empty_response"
    if "未配置" in str(exc):
        return "not_configured"
    return "error"



def _llm_error_retryable(error_kind: str) -> bool:
    """Only retry errors where another Provider has a reasonable chance.

    Authentication, endpoint, model and response-shape errors are configuration
    or contract problems. Falling through every Provider for those errors hides
    the real fix and can spend money repeatedly. Network/timeout, rate limit and
    upstream 5xx errors are the narrow recovery set.
    """
    return error_kind in {"timeout", "network", "rate_limit", "upstream"}



def _llm_health(provider: dict[str, str]) -> dict[str, Any]:
    provider_id = provider.get("id") or _llm_provider_id(provider.get("name"))
    current = LLM_PROVIDER_HEALTH.get(provider_id)
    if current is None:
        current = {}
        try:
            connection = db_connection()
            row = connection.execute("SELECT * FROM llm_provider_health WHERE provider_id = ?", (provider_id,)).fetchone()
            connection.close()
            if row:
                current = dict(row)
        except Exception:
            current = {}
        LLM_PROVIDER_HEALTH[provider_id] = current
    cooldown_until = float(current.get("cooldown_until") or 0)
    if cooldown_until and cooldown_until <= time.time():
        cooldown_until = 0
        current = {**current, "cooldown_until": 0}
        LLM_PROVIDER_HEALTH[provider_id] = current
    if cooldown_until:
        status = "cooling"
    elif current.get("last_error"):
        status = "error"
    elif current.get("last_success_at"):
        status = "healthy"
    else:
        status = "unknown"
    return {
        "status": status,
        "last_error": current.get("last_error", ""),
        "last_error_kind": current.get("last_error_kind", ""),
        "last_error_at": current.get("last_error_at", ""),
        "last_success_at": current.get("last_success_at", ""),
        "cooldown_until": cooldown_until,
    }



def _persist_llm_health(provider_id: str, health: dict[str, Any]) -> None:
    """Persist only operational state; credentials and upstream bodies never enter this table."""
    try:
        connection = db_connection()
        connection.execute(
            """INSERT INTO llm_provider_health
            (provider_id, last_success_at, last_error, last_error_kind, last_error_at, cooldown_until, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id) DO UPDATE SET
              last_success_at=excluded.last_success_at,
              last_error=excluded.last_error,
              last_error_kind=excluded.last_error_kind,
              last_error_at=excluded.last_error_at,
              cooldown_until=excluded.cooldown_until,
              updated_at=excluded.updated_at""",
            (
                provider_id,
                str(health.get("last_success_at") or ""),
                str(health.get("last_error") or ""),
                str(health.get("last_error_kind") or ""),
                str(health.get("last_error_at") or ""),
                float(health.get("cooldown_until") or 0),
                now_iso(),
            ),
        )
        connection.commit()
        connection.close()
    except Exception:
        # A health write must never turn a provider response into a failed call.
        return



def _record_llm_success(provider: dict[str, str]) -> None:
    provider_id = provider.get("id") or _llm_provider_id(provider.get("name"))
    current = LLM_PROVIDER_HEALTH.get(provider_id, {})
    health = {
        **current,
        "last_success_at": now_iso(),
        "last_error": "",
        "last_error_kind": "",
        "last_error_at": "",
        "cooldown_until": 0,
    }
    LLM_PROVIDER_HEALTH[provider_id] = health
    _persist_llm_health(provider_id, health)



def _record_llm_failure(provider: dict[str, str], exc: Exception) -> None:
    provider_id = provider.get("id") or _llm_provider_id(provider.get("name"))
    kind = _llm_error_kind(exc)
    cooldown = time.time() + LLM_PROVIDER_COOLDOWN_SECONDS if kind == "rate_limit" else 0
    health = {
        **LLM_PROVIDER_HEALTH.get(provider_id, {}),
        "last_error": kind,
        "last_error_kind": kind,
        "last_error_at": now_iso(),
        "cooldown_until": cooldown,
    }
    LLM_PROVIDER_HEALTH[provider_id] = health
    _persist_llm_health(provider_id, health)



def record_llm_usage_event(
    provider: dict[str, Any],
    *,
    status: str,
    latency_ms: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_kind: str = "",
    purpose: str = "agent",
) -> None:
    """Persist privacy-safe LLM operational metrics; request/response bodies never enter this table."""
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    total_tokens = input_tokens + output_tokens
    try:
        price = max(0.0, float(os.getenv("WORKBENCH_LLM_COST_PER_1K", "0") or 0))
    except (TypeError, ValueError):
        price = 0.0
    cost_usd = round(total_tokens / 1000 * price, 8)
    connection: sqlite3.Connection | None = None
    try:
        connection = db_connection()
        connection.execute(
            """INSERT INTO llm_usage_events
            (provider_id, provider_name, model, status, error_kind, input_tokens, output_tokens, total_tokens, cost_usd, latency_ms, run_id, purpose, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(provider.get("id") or _llm_provider_id(provider.get("name"))),
                clip(str(provider.get("name") or provider.get("source") or "未命名 Provider"), 120),
                clip(str(provider.get("model") or ""), 160),
                status,
                error_kind,
                input_tokens,
                output_tokens,
                total_tokens,
                cost_usd,
                max(0, int(latency_ms or 0)),
                str(provider.get("run_id") or ""),
                str(purpose or provider.get("purpose") or "agent")[:40],
                now_iso(),
            ),
        )
        connection.commit()
    except Exception:
        # Metrics must never turn a successful Agent call into a failed call.
        log.warning("写入 LLM 用量事件失败（已忽略）", exc_info=True)
        if connection:
            connection.rollback()
    finally:
        if connection:
            connection.close()


# Background tasks need a strong reference, otherwise the event loop may garbage
# collect them mid-flight and the metric silently disappears.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()



def schedule_llm_usage_event(provider: dict[str, Any], **fields: Any) -> None:
    """Record LLM metrics off the event loop.

    ``record_llm_usage_event`` opens a SQLite connection and commits.  It used to
    run inline inside ``call_llm``, so every single LLM request performed a
    blocking write on the event loop -- and with ``busy_timeout = 30000`` a lock
    held by one of the five worker processes could freeze the *entire* API for up
    to 30 seconds.  Observability must never be able to stall the request path.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # Called from a plain worker thread: write inline.
        record_llm_usage_event(provider, **fields)
        return
    task = loop.create_task(asyncio.to_thread(record_llm_usage_event, provider, **fields))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)



def llm_usage_metrics_payload(hours: int = 24) -> dict[str, Any]:
    hours = max(1, min(int(hours or 24), 24 * 90))
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM llm_usage_events WHERE julianday(created_at) >= julianday('now', ?) ORDER BY created_at DESC",
            (f"-{hours} hours",),
        ).fetchall()
    finally:
        connection.close()
    all_events = [dict(row) for row in rows]
    events = [row for row in all_events if str(row.get("purpose") or "agent") != "test"]
    test_events = [row for row in all_events if str(row.get("purpose") or "agent") == "test"]
    total = len(events)
    succeeded = sum(1 for row in events if row.get("status") == "succeeded")
    failed = total - succeeded
    latency_values = sorted(int(row.get("latency_ms") or 0) for row in events)
    p95 = latency_values[min(len(latency_values) - 1, max(0, math.ceil(len(latency_values) * 0.95) - 1))] if latency_values else 0
    providers: dict[str, dict[str, Any]] = {}
    for row in events:
        key = str(row.get("provider_id") or row.get("provider_name") or "unknown")
        item = providers.setdefault(key, {"provider_id": key, "provider_name": row.get("provider_name") or key, "model": row.get("model") or "", "calls": 0, "succeeded": 0, "failed": 0, "total_tokens": 0, "cost_usd": 0.0, "avg_latency_ms": 0})
        item["calls"] += 1
        item["succeeded"] += int(row.get("status") == "succeeded")
        item["failed"] += int(row.get("status") != "succeeded")
        item["total_tokens"] += int(row.get("total_tokens") or 0)
        item["cost_usd"] += float(row.get("cost_usd") or 0)
        item["avg_latency_ms"] += int(row.get("latency_ms") or 0)
    for item in providers.values():
        item["avg_latency_ms"] = round(item["avg_latency_ms"] / max(1, item["calls"]))
        item["cost_usd"] = round(item["cost_usd"], 8)
        item["success_rate"] = round(item["succeeded"] / max(1, item["calls"]), 4)
    error_kinds: dict[str, int] = {}
    for row in events:
        if row.get("status") != "succeeded":
            kind = str(row.get("error_kind") or "error")
            error_kinds[kind] = error_kinds.get(kind, 0) + 1
    return {
        "window_hours": hours,
        "summary": {
            "calls": total,
            "succeeded": succeeded,
            "failed": failed,
            "success_rate": round(succeeded / max(1, total), 4),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in events),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in events),
            "total_tokens": sum(int(row.get("total_tokens") or 0) for row in events),
            "cost_usd": round(sum(float(row.get("cost_usd") or 0) for row in events), 8),
            "avg_latency_ms": round(sum(int(row.get("latency_ms") or 0) for row in events) / max(1, total)),
            "p95_latency_ms": p95,
        },
        "by_provider": sorted(providers.values(), key=lambda item: (-item["calls"], item["provider_name"])),
        "error_kinds": sorted(({"kind": key, "count": value} for key, value in error_kinds.items()), key=lambda item: -item["count"]),
        "recent": events[:30],
        "test_summary": {
            "calls": len(test_events),
            "succeeded": sum(1 for row in test_events if row.get("status") == "succeeded"),
            "failed": sum(1 for row in test_events if row.get("status") != "succeeded"),
            "last_at": test_events[0].get("created_at", "") if test_events else "",
        },
        "policy": "仅记录 Provider、模型、调用状态、Token/耗时/成本等运行指标，不记录请求或响应正文。成本按 WORKBENCH_LLM_COST_PER_1K 估算。",
    }



def _llm_key_from_environment(provider_id: str, name: str) -> str:
    """从受保护环境变量读取 API Key（Key 不进 llm_settings.json）。

    支持：WORKBENCH_LLM_KEY_<PROVIDER_ID>、WORKBENCH_LLM_KEY_<NAME>、
    WORKBENCH_LLM_KEY（通用），或 WORKBENCH_LLM_KEYS JSON（provider_id/name → key）。
    """
    normalized_id = re.sub(r"[^A-Z0-9]+", "_", provider_id.upper()).strip("_")
    normalized_name = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    for key in (f"WORKBENCH_LLM_KEY_{normalized_id}", f"WORKBENCH_LLM_KEY_{normalized_name}", "WORKBENCH_LLM_KEY"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    raw = os.getenv("WORKBENCH_LLM_KEYS", "").strip()
    if raw:
        try:
            mapping = json.loads(raw)
            if isinstance(mapping, dict):
                value = mapping.get(provider_id) or mapping.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except (json.JSONDecodeError, TypeError):
            pass
    return ""



def normalize_llm_providers(saved: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Return the ordered provider list, migrating the legacy flat format on the fly.

    New structure: {"providers": [{"name", "role", "base_url", "api_key", "model"}]}
    role is "primary" or "fallback"; primary is tried first, fallbacks in order after.
    Legacy format ({api_key, base_url, model, primary_token_json}) is converted once.
    """
    saved = saved or _app_call("load_saved_llm_settings")
    providers_raw = saved.get("providers")
    if isinstance(providers_raw, list) and providers_raw:
        normalized: list[dict[str, str]] = []
        used_ids: set[str] = set()
        for index, provider in enumerate(providers_raw):
            if not isinstance(provider, dict):
                continue
            name = str(provider.get("name") or f"Provider {index + 1}").strip()
            provider_id = _unique_llm_provider_id(provider.get("id") or name, index, used_ids)
            raw_base_url = str(provider.get("base_url") or "").strip()
            entry = {
                "id": provider_id,
                "name": name,
                "role": "primary" if provider.get("role") == "primary" else "fallback",
                # Keep a malformed saved value visible so the settings page
                # can explain the problem instead of silently turning it into
                # an indistinguishable empty field.
                "base_url": normalize_llm_base_url(raw_base_url) or raw_base_url.rstrip("/"),
                "api_key": _clean_llm_token(str(provider.get("api_key") or "")),
                "model": str(provider.get("model") or "").strip(),
                "key_source": "saved",
            }
            # Key 安全加固：JSON 里留空的 Key 从受保护环境变量注入（不进配置文件）
            if not entry["api_key"]:
                env_key = _llm_key_from_environment(provider_id, name)
                if env_key:
                    entry["api_key"] = env_key
                    entry["key_source"] = "environment"
            # Keep incomplete entries so the UI can explain why they are
            # disabled.  _app_call("llm_provider_state", ) separately filters real call
            # candidates to entries with URL, model and Key.
            normalized.append(entry)
        if normalized:
            return normalized

    legacy_key = _clean_llm_token(str(saved.get("api_key") or ""))
    legacy_base_url = normalize_llm_base_url(saved.get("base_url"))
    legacy_model = str(saved.get("model") or "").strip()
    legacy_primary_raw = str(saved.get("primary_token_json") or "").strip()
    migrated: list[dict[str, str]] = []
    if legacy_primary_raw:
        try:
            parsed = json.loads(legacy_primary_raw)
        except json.JSONDecodeError:
            parsed = {"api_key": legacy_primary_raw}
        if isinstance(parsed, str):
            parsed = {"api_key": parsed}
        if isinstance(parsed, (dict, list)):
            primary_key = _clean_llm_token(_nested_config_value(parsed, _PRIMARY_TOKEN_KEYS))
            primary_base_url = _nested_config_value(parsed, _PRIMARY_BASE_URL_KEYS).rstrip("/") or legacy_base_url
            primary_model = _nested_config_value(parsed, _PRIMARY_MODEL_KEYS) or legacy_model
            if primary_key and primary_base_url and primary_model:
                migrated.append({
                    "id": _llm_provider_id("AI Token 主配置"),
                    "name": "AI Token 主配置",
                    "role": "primary",
                    "base_url": primary_base_url,
                    "api_key": primary_key,
                    "model": primary_model,
                })
    if legacy_key and legacy_base_url and legacy_model:
        migrated.append({
            "id": _llm_provider_id("DeepSeek fallback"),
            "name": "DeepSeek fallback",
            "role": "fallback",
            "base_url": legacy_base_url,
            "api_key": legacy_key,
            "model": legacy_model,
        })
    return migrated



def llm_fallback_credentials(saved: dict[str, Any] | None = None) -> dict[str, str]:
    """First fallback provider, or an environment-variable fallback as last resort."""
    providers = normalize_llm_providers(saved)
    for provider in providers:
        if provider["role"] == "fallback" and _llm_provider_usable(provider):
            return {**provider, "source": provider["name"], "provider": "fallback"}
    environment_key = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    if environment_key:
        environment_provider = {
            "id": _llm_provider_id("环境变量 fallback"),
            "name": "环境变量 fallback",
            "role": "fallback",
            "api_key": _clean_llm_token(environment_key),
            "base_url": (os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
            "model": os.getenv("LLM_MODEL") or "gpt-4o-mini",
            "source": "环境变量 fallback",
            "provider": "fallback",
        }
        if _llm_provider_usable(environment_provider):
            return environment_provider
    return {"api_key": "", "base_url": "", "model": "", "source": "未配置", "provider": "fallback"}



def llm_provider_state() -> dict[str, Any]:
    saved = _app_call("load_saved_llm_settings")
    providers = normalize_llm_providers(saved)
    primary = next((provider for provider in providers if provider["role"] == "primary"), None)
    fallbacks = [provider for provider in providers if provider["role"] == "fallback"]
    candidates: list[dict[str, str]] = [
        provider for provider in ([primary] if primary else []) + fallbacks
        if provider and _llm_provider_usable(provider)
    ]
    # Only a fully callable saved fallback can become the selected fallback.
    # Incomplete rows stay visible for editing, but must never shadow a later
    # usable fallback or the final environment-variable fallback.
    fallback = next((provider for provider in fallbacks if _llm_provider_usable(provider)), None) or llm_fallback_credentials(saved)
    if fallback and _llm_provider_usable(fallback):
        # Normalized saved rows intentionally do not carry the legacy
        # ``source`` field.  Add the public label here so the settings API
        # cannot report a working saved fallback as "未配置".
        fallback = {
            **fallback,
            "source": fallback.get("source") or fallback.get("name") or "已保存 fallback",
            "provider": fallback.get("provider") or "fallback",
        }
    # The new provider format may intentionally leave the fallback out of the
    # saved file and rely on LLM_API_KEY/OPENAI_API_KEY for the last resort.
    # It must still participate in the real routing path; otherwise the UI
    # can show an environment fallback while call_llm reports "unconfigured".
    if _llm_provider_usable(fallback) and not any(item.get("id") == fallback.get("id") for item in candidates):
        candidates.append(fallback)
    return {
        "saved": saved,
        "providers": providers,
        "primary": primary,
        "primary_error": "",
        "primary_present": bool(primary),
        "fallback": fallback,
        "candidates": candidates,
    }



def llm_credentials() -> dict[str, str]:
    state = _app_call("llm_provider_state")
    if state["candidates"]:
        return state["candidates"][0]
    return state["fallback"]



def llm_effective_candidate(state: dict[str, Any] | None = None) -> tuple[dict[str, str], str]:
    """Return the provider the next call will actually try first.

    A rate-limited provider is intentionally skipped by ``call_llm``.  The
    settings endpoint used to report the first configured provider anyway,
    which made the UI say "primary" while the next request would use a
    fallback.  Keep this selection rule in one place so the UI and the call
    path describe the same routing decision.
    """
    state = state or _app_call("llm_provider_state")
    candidates = state.get("candidates") or []
    for index, provider in enumerate(candidates):
        if _app_call("_llm_health", provider).get("status") != "cooling":
            return provider, "primary" if index == 0 and provider.get("role") == "primary" else "fallback"
    if candidates:
        provider = candidates[0]
        return provider, "cooling"
    return state.get("fallback") or {}, "unconfigured"



def chat_completions_url(base_url: str) -> str:
    normalized = normalize_llm_base_url(base_url)
    if not normalized:
        return ""
    if urlparse(normalized).path.lower().endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


LLM_ENDPOINT_POLICY = "支持 OpenAI Chat Completions 兼容接口：填写 API 基地址（例如 /v1）或完整 /chat/completions 地址；不支持把 Key 放进 URL。"



def llm_settings() -> dict[str, Any]:
    state = _app_call("llm_provider_state")
    active, active_route = llm_effective_candidate(state)
    primary = state["primary"]
    fallback = state["fallback"]
    providers = [
        {
            "id": provider.get("id", ""),
            "name": provider.get("name", ""),
            "role": provider.get("role", "fallback"),
            "base_url": provider.get("base_url", ""),
            "endpoint": chat_completions_url(provider.get("base_url", "")),
            "model": provider.get("model", ""),
            "has_key": bool(provider.get("api_key")),
            "usable": _llm_provider_usable(provider),
            "disabled_reason": _llm_provider_disabled_reason(provider),
            "health": _app_call("_llm_health", provider),
        }
        for provider in state["providers"]
    ]
    candidate_order = [
        {
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "role": item.get("role", "fallback"),
            "source": item.get("source") or ("environment" if str(item.get("name", "")).startswith("环境变量") else "saved"),
            "endpoint": chat_completions_url(item.get("base_url", "")),
            "model": item.get("model", ""),
            "health": _app_call("_llm_health", item),
        }
        for item in state["candidates"]
    ]
    active_health = _app_call("_llm_health", active) if active else {"status": "unknown"}
    routable_count = sum(1 for provider in providers if provider["usable"])
    formal_success_count = sum(1 for provider in providers if provider["health"].get("status") == "healthy")
    return {
        "configured": bool(state["candidates"]),
        # Keep these separate so the settings UI does not conflate a saved
        # row, a callable route and a provider that has actually succeeded in
        # a production Agent call.
        "saved_count": len(providers),
        "routable_count": routable_count,
        "formal_success_count": formal_success_count,
        "base_url": active.get("base_url", ""),
        "model": active.get("model", ""),
        # Normalized saved providers use ``name``/``role`` rather than the
        # legacy ``source``/``provider`` fields.  Keep the public contract
        # truthful after the provider migration so API consumers do not see
        # "未配置" while a real provider is selected.
        "source": active.get("source") or active.get("name") or "未配置",
        "provider": active.get("provider") or active_route,
        "primary_configured": bool(primary and _llm_provider_usable(primary)),
        "primary_present": state["primary_present"],
        "primary_model": primary.get("model", "") if primary else "",
        "primary_base_url": primary.get("base_url", "") if primary else "",
        "primary_error": state["primary_error"],
        "fallback_configured": bool(_llm_provider_usable(fallback)),
        "fallback_count": sum(1 for provider in state["candidates"] if provider.get("role") == "fallback" and _llm_provider_usable(provider)),
        "fallback_model": fallback.get("model", ""),
        "fallback_base_url": fallback.get("base_url", ""),
        "fallback_source": fallback.get("source", "未配置"),
        "fallback_is_environment": str(fallback.get("source") or "").startswith("环境变量"),
        "active_status": active_health.get("status", "unknown"),
        "active_route": active_route,
        "active_selection_reason": (
            "主配置优先"
            if active_route == "primary"
            else "主配置冷却，已切换到 fallback"
            if active_route == "fallback" and primary and _app_call("_llm_health", primary).get("status") == "cooling"
            else "未配置主配置，使用 fallback"
            if active_route == "fallback"
            else "所有候选均在冷却中"
            if active_route == "cooling"
            else "没有可用 Provider"
        ),
        "active_provider_id": active.get("id", "") if active else "",
        "active_provider_name": active.get("name", "") if active else "",
        "active_provider_health": active_health,
        "providers": providers,
        "candidate_order": candidate_order,
        "routing_policy": "主配置优先；按保存顺序尝试 fallback；环境变量仅作为最后 fallback；无 Key 条目保留但不调用。",
        "endpoint_policy": LLM_ENDPOINT_POLICY,
    }



def valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        # Credentials in an endpoint are too easy to leak through logs, error
        # messages or browser history.  The API key belongs in Authorization.
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False



def valid_research_url(value: str) -> bool:
    """Validate a public web URL used by the research/crawl surface.

    This is intentionally separate from ``valid_http_url``: LLM endpoint
    addresses must not contain query strings or fragments, while real web
    pages commonly do (search results, articles, filters). Research URLs also
    must not point at obvious local/private destinations because the crawl
    worker may run on a public server.

    DNS names are not resolved here; the check is deliberately cheap and
    deterministic at request time. Network-level egress controls remain the
    stronger production SSRF boundary.
    """
    try:
        parsed = urlparse(str(value or "").strip())
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not host:
            return False
        if parsed.username or parsed.password:
            return False
        if host in {"localhost", "localhost.localdomain", "ip6-localhost"} or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return False
        return True
    except (TypeError, ValueError):
        return False



def normalize_llm_base_url(value: Any) -> str:
    """Normalize an accepted LLM URL without changing its endpoint semantics."""
    text = str(value or "").strip()
    if not text or not valid_http_url(text):
        return ""
    return text.rstrip("/")



# 模型输出能力表：max_tokens 上限应跟随模型能力，而不是硬编码。
# key 用模型名前缀匹配（大小写不敏感）；命中顺序取最长匹配。
# 没有命中的模型用保守默认 4096，避免 API 拒绝（部分模型上限 2k-4k）。
MODEL_OUTPUT_TOKEN_LIMITS: tuple[tuple[str, int], ...] = (
    # DeepSeek 系（官方输出上限 8k，新版支持更大）
    ("deepseek", 8192),
    ("deepseek-v4", 8192),
    # GPT 系（OpenAI 输出上限 16k；部分推理模型支持更大）
    ("gpt-5", 16384),
    ("gpt-4o", 16384),
    ("gpt-4", 8192),
    ("gpt", 8192),
    # Claude 系（Anthropic，输出上限 64k）
    ("claude", 64000),
    # 通义/智谱/月之暗面/豆包 等国内模型，常见上限 8k
    ("qwen", 8192),
    ("glm", 8192),
    ("kimi", 16384),
    ("doubao", 8192),
    # Ollama 本地模型保守
    ("llama", 4096),
    ("qwen2", 8192),
)

def model_output_token_limit(model_name: str, requested: int) -> int:
    """按模型能力截断请求的 max_tokens：返回 min(请求值, 模型上限)。

    - 模型命中能力表：上限 = 表值，请求超过上限则 clamp 到上限。
    - 模型未命中：用保守默认 4096（避免传过大值被 API 拒绝）。
    """
    name = str(model_name or "").lower()
    limit = 4096
    best_len = 0
    for prefix, cap in MODEL_OUTPUT_TOKEN_LIMITS:
        if prefix.lower() in name and len(prefix) > best_len:
            limit = cap
            best_len = len(prefix)
    if name and not best_len:
        # 已知模型名但未命中表：保守 4096
        limit = 4096
    return max(256, min(int(requested or 0) or limit, limit))


INTEGRATION_DEFINITIONS: dict[str, dict[str, Any]] = {
    "github": {
        "name": "GitHub Issues / PRs",
        "repo": "https://github.com/features/issues",
        "kind": "code_collaboration",
        "description": "读取指定仓库的开放 Issue 和 Pull Request，人工选择后进入收件箱继续处理。",
        "fields": {"base_url": "API 地址", "owner": "用户名/组织", "repo": "仓库名", "token": "访问 Token"},
        "defaults": {"base_url": "https://api.github.com"},
        "required": ["base_url", "owner", "repo"],
        "secret_fields": {"token"},
    },
    "ntfy": {
        "name": "ntfy",
        "repo": "https://github.com/binwiederhier/ntfy",
        "kind": "notification",
        "description": "把高价值提醒送到手机或桌面，不替换工作台的应用通知。",
        "fields": {"base_url": "服务地址", "topic": "主题", "token": "访问 Token"},
        "required": ["base_url", "topic"],
        "secret_fields": {"token"},
    },
    "miniflux": {
        "name": "Miniflux",
        "repo": "https://github.com/miniflux/v2",
        "kind": "reading",
        "description": "读取低噪 RSS 未读条目，选中后进入网页研究或收件箱。",
        "fields": {"base_url": "服务地址", "api_token": "API Token"},
        "required": ["base_url", "api_token"],
        "secret_fields": {"api_token"},
    },
    "zotero": {
        "name": "Zotero",
        "repo": "https://github.com/zotero/zotero",
        "kind": "research",
        "description": "读取最近研究条目和 DOI 元数据，生成可追踪的学习工作项。",
        "fields": {"base_url": "API 地址", "user_id": "用户 ID", "api_key": "API Key"},
        "required": ["base_url", "user_id", "api_key"],
        "secret_fields": {"api_key"},
    },
    "activitywatch": {
        "name": "ActivityWatch",
        "repo": "https://github.com/ActivityWatch/activitywatch",
        "kind": "productivity",
        "description": "读取近 7 天聚合时长和事件数量，帮助判断时间是否真的花在高价值工作上。不会保存窗口标题或网页 URL。",
        "fields": {"base_url": "服务地址", "bucket_id": "默认数据桶（可选）"},
        "required": ["base_url"],
        "secret_fields": set(),
    },
    "linkding": {
        "name": "Linkding",
        "repo": "https://github.com/sissbruecker/linkding",
        "kind": "reading",
        "description": "读取自托管稍后读书签，人工选择后交给网页研究或知识库继续处理。",
        "fields": {"base_url": "服务地址", "token": "API Token"},
        "required": ["base_url", "token"],
        "secret_fields": {"token"},
        "configuration_cost": "免费·自托管地址 + API Token",
        "data_boundary": "只读书签标题、链接、描述和标签；人工勾选后才进入工作台",
        "next_step": "配置服务地址和 Token，读取书签并人工勾选导入网页研究或知识库",
    },
    "paperless": {
        "name": "Paperless-ngx",
        "repo": "https://github.com/paperless-ngx/paperless-ngx",
        "kind": "documents",
        "description": "读取自托管文档归档的元数据，人工选择后交给知识库或文档工厂。",
        "fields": {"base_url": "服务地址", "token": "API Token"},
        "required": ["base_url", "token"],
        "secret_fields": {"token"},
        "configuration_cost": "免费·自托管地址 + API Token",
        "data_boundary": "只读文档元数据；人工勾选后进入知识库或文档工厂，不自动下载或修改归档文件",
        "next_step": "配置服务地址和 Token，读取文档后人工选择进入知识库或文档工厂",
    },
    "vikunja": {
        "name": "Vikunja",
        "repo": "https://github.com/go-vikunja/vikunja",
        "kind": "task_management",
        "description": "读取自托管任务系统中的未完成任务，人工选择后进入收件箱；Workbench 仍保留来源和审计主线。",
        "fields": {"base_url": "服务地址", "api_token": "API Token", "project_id": "项目 ID（可选）"},
        "required": ["base_url", "api_token"],
        "secret_fields": {"api_token"},
        "configuration_cost": "免费·自托管地址 + API Token；项目 ID 可选",
        "data_boundary": "只读未完成任务的标题、描述、截止时间和标签；不回写、不删除、不自动完成第三方任务",
        "next_step": "配置地址和 Token，读取未完成任务并人工勾选导入收件箱",
    },
    "searxng": {
        "name": "SearXNG",
        "repo": "https://github.com/searxng/searxng",
        "kind": "search",
        "description": "通过自托管的隐私友好搜索聚合器寻找学习资料，人工选择后进入网页研究。",
        "fields": {"base_url": "服务地址", "query": "默认搜索词", "categories": "搜索分类（可选）"},
        "defaults": {"categories": "general"},
        "required": ["base_url", "query"],
        "secret_fields": set(),
        "configuration_cost": "免费·自托管地址；无需 API Key",
        "data_boundary": "只读搜索结果；只在点击读取和人工勾选后进入网页研究，不保存搜索引擎原始日志",
        "next_step": "配置服务地址和默认搜索词，读取结果后人工选择进入网页研究",
    },
    "wallabag": {
        "name": "Wallabag",
        "repo": "https://github.com/wallabag/wallabag",
        "kind": "reading",
        "description": "读取自托管稍后读文章，把真正准备学习或研究的内容送入网页研究。",
        "fields": {"base_url": "服务地址", "access_token": "访问 Token"},
        "required": ["base_url", "access_token"],
        "secret_fields": {"access_token"},
        "configuration_cost": "免费·自托管地址 + Access Token",
        "data_boundary": "只读未归档文章的标题、链接、摘要和标签；不修改 Wallabag 文章状态",
        "next_step": "配置地址和 Access Token，读取稍后读列表后人工选择进入网页研究",
    },
}
INTEGRATION_ENV_KEYS = {
    "github": {"base_url": "WORKBENCH_GITHUB_API_URL", "owner": "WORKBENCH_GITHUB_OWNER", "repo": "WORKBENCH_GITHUB_REPO", "token": "WORKBENCH_GITHUB_TOKEN"},
    "ntfy": {"base_url": "WORKBENCH_NTFY_URL", "topic": "WORKBENCH_NTFY_TOPIC", "token": "WORKBENCH_NTFY_TOKEN"},
    "miniflux": {"base_url": "WORKBENCH_MINIFLUX_URL", "api_token": "WORKBENCH_MINIFLUX_API_TOKEN"},
    "zotero": {"base_url": "WORKBENCH_ZOTERO_URL", "user_id": "WORKBENCH_ZOTERO_USER_ID", "api_key": "WORKBENCH_ZOTERO_API_KEY"},
    "activitywatch": {"base_url": "WORKBENCH_ACTIVITYWATCH_URL", "bucket_id": "WORKBENCH_ACTIVITYWATCH_BUCKET_ID"},
    "linkding": {"base_url": "WORKBENCH_LINKDING_URL", "token": "WORKBENCH_LINKDING_TOKEN"},
    "paperless": {"base_url": "WORKBENCH_PAPERLESS_URL", "token": "WORKBENCH_PAPERLESS_TOKEN"},
    "vikunja": {"base_url": "WORKBENCH_VIKUNJA_URL", "api_token": "WORKBENCH_VIKUNJA_API_TOKEN", "project_id": "WORKBENCH_VIKUNJA_PROJECT_ID"},
    "searxng": {"base_url": "WORKBENCH_SEARXNG_URL", "query": "WORKBENCH_SEARXNG_QUERY", "categories": "WORKBENCH_SEARXNG_CATEGORIES"},
    "wallabag": {"base_url": "WORKBENCH_WALLABAG_URL", "access_token": "WORKBENCH_WALLABAG_ACCESS_TOKEN"},
}

DATA_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)
KNOWLEDGE_DIR.mkdir(exist_ok=True)


# Provider health is intentionally kept separate from credentials.  It gives
# the UI a useful explanation after a failed primary call without persisting
# upstream response bodies or secrets into the shared database.
LLM_PROVIDER_HEALTH: dict[str, dict[str, Any]] = {}
LLM_PROVIDER_COOLDOWN_SECONDS = 60

_PRIMARY_TOKEN_KEYS = (
    "api_key", "apikey", "api-key", "token", "access_token", "access-token",
    "accessToken", "authorization", "bearer_token", "bearerToken", "key", "secret",
)

_PRIMARY_BASE_URL_KEYS = (
    "base_url", "base-url", "baseUrl", "baseURL", "endpoint", "api_url", "api-url",
    "apiUrl", "apiURL", "api_base", "api-base", "apiBase", "url",
)

_PRIMARY_MODEL_KEYS = (
    "model", "model_name", "model-name", "modelName", "model_id", "model-id", "modelId",
)




def runtime_tool_policy(name: str) -> dict[str, Any]:
    """延迟转发 app.runtime_tool_policy（agent 平台仍在 app.py）。"""
    import app as _app

    return _app.runtime_tool_policy(name)


def add_agent_run_event(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.add_agent_run_event（agent 运行基础设施仍在 app.py）。"""
    import app as _app

    return _app.add_agent_run_event(*args, **kwargs)


def update_agent_run_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.update_agent_run_record（仍在 app.py）。"""
    import app as _app

    return _app.update_agent_run_record(*args, **kwargs)


def get_agent_run(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.get_agent_run（仍在 app.py）。"""
    import app as _app

    return _app.get_agent_run(*args, **kwargs)


def create_agent_run_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_agent_run_record（仍在 app.py）。"""
    import app as _app

    return _app.create_agent_run_record(*args, **kwargs)


def project_href(project_id: str) -> str:
    """延迟转发 app.project_href（projects 领域仍在 app.py）。"""
    import app as _app

    return _app.project_href(project_id)


def create_notification_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_notification_record（notifications 领域仍在 app.py）。"""
    import app as _app

    return _app.create_notification_record(*args, **kwargs)


def agent_display_name(project_id: str) -> str:
    """延迟转发 app.agent_display_name（仍在 app.py）。"""
    import app as _app

    return _app.agent_display_name(project_id)


def write_knowledge_note(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.write_knowledge_note（knowledge 领域仍在 app.py）。"""
    import app as _app

    return _app.write_knowledge_note(*args, **kwargs)



# 一次回答最多续写几段。设上限是因为「一直续下去」在模型跑偏时会烧掉大量额度，
# 而 4 段（默认 4000 tokens 一段）已经够写完任何一份正常的分析。
LLM_MAX_CONTINUATIONS = _int_env("WORKBENCH_LLM_MAX_CONTINUATIONS", 4, minimum=0, maximum=10)

def _llm_timeout() -> httpx.Timeout:
    try:
        read = float(os.getenv("WORKBENCH_LLM_READ_TIMEOUT_SECONDS", "120") or 120)
    except (TypeError, ValueError):
        read = 120.0
    try:
        connect = float(os.getenv("WORKBENCH_LLM_CONNECT_TIMEOUT_SECONDS", "10") or 10)
    except (TypeError, ValueError):
        connect = 10.0
    return httpx.Timeout(read, connect=connect, write=30.0, pool=10.0)


_LLM_HTTP_CLIENTS: dict[str, httpx.AsyncClient] = {}
_LLM_HTTP_CLIENT_LOCK = asyncio.Lock()



async def llm_http_client() -> httpx.AsyncClient:
    """Return the pooled client for the current proxy setting."""
    proxy = os.getenv("LLM_PROXY", "").strip()
    async with _LLM_HTTP_CLIENT_LOCK:
        client = _LLM_HTTP_CLIENTS.get(proxy)
        if client is not None and not client.is_closed:
            return client
        options: dict[str, Any] = {
            "timeout": _llm_timeout(),
            "trust_env": False,
            "limits": httpx.Limits(max_keepalive_connections=10, max_connections=20, keepalive_expiry=90.0),
        }
        if proxy:
            options["proxy"] = proxy
        client = httpx.AsyncClient(**options)
        _LLM_HTTP_CLIENTS[proxy] = client
        return client



async def stream_llm_text(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 4000,
    temperature: float = 0.2,
    purpose: str = "agent",
    reasoning: bool = False,
):
    """流式调用 LLM，逐块产出 dict；被 max_tokens 截断时自动续写。

    产出格式（每块一个 dict，由调用方决定如何消费）：
      {"type": "delta", "text": str, "reasoning": str}        内容增量（可能为空串）
      {"type": "finish", "reason": str, "usage": dict|None, "provider": str}
      {"type": "reset", "provider": str}                         已输出的半段内容作废，fallback 将从头回答
      {"type": "error", "message": str, "provider": str,
       "recoverable": bool}                                        当前 provider 失败或整条流最终失败

    语义与 call_llm 一致：主配置失败后依次尝试 fallback 候选，而不是让整条
    链路随第一个 Provider 一起失败。工具轮次（ReAct）仍用非流式 call_llm_with_tools，
    这里只服务"最终文字回答"的流式输出。

    截断续写：finish_reason == "length" 时，把这一段回填成 assistant 消息再要
    下一段，最多续 LLM_MAX_CONTINUATIONS 次；续写期间的 finish 扣住不外发
    （否则前端以为答案完了）。续满上限会在正文末尾明说「还有内容没写完」，
    并把 reason 标成 length_capped。
    """
    state = _app_call("llm_provider_state")
    candidates = state.get("candidates") or []
    if not candidates:
        yield {"type": "error", "message": "未配置可调用的 LLM Provider", "provider": "", "recoverable": False}
        return

    async def _one_stream(provider: dict[str, Any], segment_messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        """单段流式请求；yield 增量文本，返回段内元信息。失败返回 None。"""
        started_at = time.monotonic()
        api_key = str(provider.get("api_key") or "")
        model = str(provider.get("model") or "")
        base_url = str(provider.get("base_url") or "")
        provider_name = str(provider.get("name") or model or "未命名")
        if not api_key or not model or not base_url:
            yield {"type": "meta", "meta": {"error": "配置不完整", "provider": provider_name}}
            return
        payload = {
            "model": model,
            "messages": segment_messages,
            "temperature": temperature,
            "max_tokens": model_output_token_limit(model, max_tokens),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = ""
        usage: dict[str, Any] | None = None
        try:
            client = await _app_call("llm_http_client")
            async with client.stream(
                "POST", chat_completions_url(base_url), headers=headers, json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    choice = ((chunk.get("choices") or [{}])[0]) or {}
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or ""
                    think = delta.get("reasoning_content") or ""
                    if piece:
                        text_parts.append(piece)
                        yield {"type": "delta", "text": piece, "reasoning": ""}
                    if think and reasoning:
                        reasoning_parts.append(think)
                        yield {"type": "delta", "text": "", "reasoning": think}
                    if choice.get("finish_reason"):
                        finish_reason = str(choice.get("finish_reason") or "")
                    if chunk.get("usage"):
                        usage = chunk.get("usage") or None
        except Exception as exc:
            _app_call("schedule_llm_usage_event", 
                provider,
                status="failed",
                error_kind=_llm_error_kind(exc),
                latency_ms=int((time.monotonic() - started_at) * 1000),
                input_tokens=sum(len(str(m.get("content") or "")) for m in segment_messages) // 4,
                purpose=purpose,
            )
            await asyncio.to_thread(_app_call, "_record_llm_failure", provider, exc)
            yield {"type": "meta", "meta": {"error": f"Provider「{provider_name}」失败：{clip(str(exc), 120)}", "provider": provider_name}}
            return
        result = "".join(text_parts).strip() or "".join(reasoning_parts).strip()
        input_tokens = int((usage or {}).get("prompt_tokens") or (sum(len(str(m.get("content") or "")) for m in segment_messages) // 4))
        output_tokens = int((usage or {}).get("completion_tokens") or (len(result) // 4))
        _app_call("schedule_llm_usage_event", 
            provider,
            status="succeeded",
            latency_ms=int((time.monotonic() - started_at) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            purpose=purpose,
        )
        await asyncio.to_thread(_app_call, "_record_llm_success", provider)
        if not result and finish_reason != "length":
            yield {"type": "meta", "meta": {"error": f"Provider「{provider_name}」流式返回为空，尝试下一个…", "provider": provider_name}}
            return
        yield {
            "type": "meta",
            "meta": {
                "text": result,
                "finish_reason": finish_reason or "stop",
                "usage": usage,
                "provider": provider_name,
                "reasoning": "".join(reasoning_parts).strip(),
            },
        }

    errors: list[str] = []
    emitted_any = False
    for provider in candidates:
        if _app_call("_llm_health", provider).get("status") == "cooling":
            errors.append(f"{provider.get('name', '未命名')}:rate_limit_cooling")
            continue
        segment_messages = list(messages)
        final_text = ""
        final_usage = None
        final_reason = "stop"
        segment_index = 0
        truncated = False
        segment_error = None
        while segment_index <= LLM_MAX_CONTINUATIONS:
            meta: dict[str, Any] | None = None
            async for chunk in _one_stream(provider, segment_messages):
                if chunk["type"] == "delta":
                    final_text += chunk["text"]
                    emitted_any = True
                    yield chunk
                elif chunk["type"] == "meta":
                    meta = chunk["meta"]
            if meta is None:
                # 段内异常：未产出任何内容则交给 failover；已产出半段要 reset。
                segment_error = "段内中断"
                if final_text:
                    yield {"type": "reset", "provider": str(provider.get("name") or "")}
                break
            if "error" in meta:
                segment_error = meta["error"]
                if final_text:
                    yield {"type": "reset", "provider": str(provider.get("name") or "")}
                errors.append(segment_error)
                break
            final_usage = meta["usage"]
            final_reason = str(meta["finish_reason"] or "stop")
            if final_reason != "length":
                truncated = False
                break
            # 截断了：回填这一段的 assistant 文本，继续要下一段。
            truncated = True
            segment_text = str(meta.get("text") or "")
            if not segment_text:
                truncated = False
                break
            segment_messages = [*segment_messages, {"role": "assistant", "content": segment_text}]
            segment_index += 1
        if segment_error and not emitted_any:
            # 第一个 provider 就失败且什么都没吐：继续尝试下一个。
            continue
        if truncated:
            # 续满上限仍被截断：明说没写完，并标记 reason。
            tail = f"\n\n（回答已达到 {LLM_MAX_CONTINUATIONS + 1} 段续写上限，后面还有内容没写完，可要求继续。）"
            final_text += tail
            emitted_any = True
            yield {"type": "delta", "text": tail, "reasoning": ""}
            final_reason = "length_capped"
        yield {"type": "finish", "reason": final_reason, "usage": final_usage, "provider": str(meta.get("provider") or "") if meta else ""}
        return
    yield {"type": "error", "message": f"全部 LLM Provider 均失败（{'; '.join(errors)[:300]}）", "provider": "", "recoverable": False}



async def stream_llm_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    purpose: str = "agent_tools",
):
    """流式版带工具调用（OpenAI function calling）。

    流式过程中无法提前知道模型这轮是调工具还是直接回答，所以逐块判断：
      - delta.content 出现 → 文本增量（回答），yield {"type":"delta_text","text":...}
      - delta.tool_calls 出现 → 按 index 累积拼装 arguments，最后 yield 完整 tool_calls
    结束统一 yield {"type":"round_done","mode":"answer"|"tools","content":str,"tool_calls":[...],"usage":...,"provider":...}
    failover 语义与 call_llm_with_tools 一致。产出均为 dict，由 ReAct 循环消费。
    """
    state = _app_call("llm_provider_state")
    candidates = state.get("candidates") or []
    if not candidates:
        yield {"type": "error", "message": "未配置可调用的 LLM Provider", "provider": "", "recoverable": False}
        return

    errors: list[str] = []
    for provider in candidates:
        if _app_call("_llm_health", provider).get("status") == "cooling":
            errors.append(f"{provider.get('name', '未命名')}:rate_limit_cooling")
            continue
        started_at = time.monotonic()
        api_key = str(provider.get("api_key") or "")
        model = str(provider.get("model") or "")
        base_url = str(provider.get("base_url") or "")
        provider_name = str(provider.get("name") or model or "未命名")
        if not api_key or not model or not base_url:
            errors.append(f"{provider_name}:配置不完整")
            continue
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": model_output_token_limit(model, 3000),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            client = await _app_call("llm_http_client")
            content_parts: list[str] = []
            tool_slots: dict[int, dict[str, Any]] = {}
            finish_reason = ""
            usage: dict[str, Any] | None = None
            async with client.stream(
                "POST", chat_completions_url(base_url), headers=headers, json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    choice = ((chunk.get("choices") or [{}])[0]) or {}
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        content_parts.append(piece)
                        yield {"type": "delta_text", "text": piece}
                    # 推理型模型会把思考写在 reasoning_content 里。这里以前直接扔掉，
                    # 于是「Agent 想了 20 秒才开口」这段时间页面上什么都没有。
                    # 只转发、不计入 content：它不是回答的一部分。
                    think = delta.get("reasoning_content") or ""
                    if think:
                        yield {"type": "delta", "text": "", "reasoning": think}
                    for tc in delta.get("tool_calls") or []:
                        index = int(tc.get("index") or 0)
                        slot = tool_slots.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        fn = tc.get("function") or {}
                        if tc.get("id"):
                            slot["id"] = str(tc["id"])
                        if fn.get("name"):
                            slot["function"]["name"] = str(fn["name"])
                        if fn.get("arguments"):
                            slot["function"]["arguments"] = (slot["function"]["arguments"] or "") + str(fn["arguments"])
                    if choice.get("finish_reason"):
                        finish_reason = str(choice.get("finish_reason") or "")
                    if chunk.get("usage"):
                        usage = chunk.get("usage") or None
            content = "".join(content_parts).strip()
            tool_calls = [tool_slots[index] for index in sorted(tool_slots)]
            input_tokens = int((usage or {}).get("prompt_tokens") or (sum(len(str(m.get("content") or "")) for m in messages) // 4))
            output_tokens = int((usage or {}).get("completion_tokens") or ((len(content) + sum(len(str(t.get("function", {}).get("arguments") or "")) for t in tool_calls)) // 4))
            _app_call("schedule_llm_usage_event", 
                provider,
                status="succeeded",
                latency_ms=int((time.monotonic() - started_at) * 1000),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                purpose=purpose,
            )
            await asyncio.to_thread(_record_llm_success, provider)
            mode = "tools" if tool_calls else "answer"
            yield {"type": "round_done", "mode": mode, "content": content, "tool_calls": tool_calls, "finish_reason": finish_reason, "usage": usage, "provider": provider_name}
            return
        except Exception as exc:
            _app_call("schedule_llm_usage_event", 
                provider,
                status="failed",
                error_kind=_llm_error_kind(exc),
                latency_ms=int((time.monotonic() - started_at) * 1000),
                input_tokens=sum(len(str(m.get("content") or "")) for m in messages) // 4,
                purpose=purpose,
            )
            await asyncio.to_thread(_record_llm_failure, provider, exc)
            errors.append(f"{provider_name}:{clip(str(exc), 120)}")
            if content_parts:
                yield {"type": "reset", "provider": provider_name}
            yield {
                "type": "error",
                "message": f"Provider「{provider_name}」失败：{clip(str(exc), 120)}",
                "provider": provider_name,
                "recoverable": True,
            }
            continue
    yield {"type": "error", "message": f"全部 LLM Provider 均失败（{'; '.join(errors)[:300]}）", "provider": "", "recoverable": False}



async def call_llm(
    messages: list[dict[str, str]],
    credentials: dict[str, str] | None = None,
    *,
    max_tokens: int = 4000,
    temperature: float = 0.2,
    purpose: str = "agent",
    track_health: bool = True,
    continue_on_truncation: bool = True,
) -> str:
    """调用 LLM；被 max_tokens 截断时自动续写，直到写完或达到续写上限。

    在这之前，finish_reason == "length" 只是被记了一条 usage 事件，然后把半截
    答案原样返回——用户看到的就是一段写到一半、经常停在句子中间的回答，而且
    没有任何提示说它被截断了。调大 max_tokens 不解决问题：各家 Provider 对
    单次输出都有上限，长任务照样会撞上。所以改成检测到截断就接着写。
    """
    if not continue_on_truncation or LLM_MAX_CONTINUATIONS <= 0:
        return await _app_call("_call_llm_once", messages, credentials, max_tokens=max_tokens, temperature=temperature,
                                    purpose=purpose, track_health=track_health)
    parts: list[str] = []
    working = list(messages)
    for round_index in range(LLM_MAX_CONTINUATIONS + 1):
        text, truncated = await _app_call("_call_llm_once", 
            working, credentials, max_tokens=max_tokens, temperature=temperature,
            purpose=purpose if round_index == 0 else f"{purpose}_continue",
            track_health=track_health, want_truncated=True,
        )
        parts.append(text)
        if not truncated:
            break
        if round_index == LLM_MAX_CONTINUATIONS:
            # 续到上限还没写完：明确告诉用户，而不是让他以为这就是全文。
            parts.append(f"\n\n（回答已达到 {LLM_MAX_CONTINUATIONS + 1} 段续写上限，后面还有内容没写完。"
                         f"可以让我针对其中某一部分单独展开。）")
            break
        working = [
            *working,
            {"role": "assistant", "content": text},
            {"role": "user", "content": "你上一段在这里被长度限制截断了。请紧接着最后一个字继续往下写，"
                                        "不要重复已经写过的内容，也不要重新开头或加过渡语，直接接着写。"},
        ]
    # 续写是接着上一段最后一个字写的，所以直接拼接，中间不加分隔符。
    return "".join(parts).strip()



async def _call_llm_once(
    messages: list[dict[str, str]],
    credentials: dict[str, str] | None = None,
    *,
    max_tokens: int = 4000,
    temperature: float = 0.2,
    purpose: str = "agent",
    track_health: bool = True,
    want_truncated: bool = False,
) -> Any:
    truncated_flag = {"hit": False}

    async def call_once(settings: dict[str, str]) -> str:
        started_at = time.monotonic()
        truncated_flag["hit"] = False
        if not _llm_provider_usable(settings):
            raise RuntimeError(_llm_provider_disabled_reason(settings) or "LLM Provider 配置不完整")
        api_key = settings["api_key"]

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # max_tokens 跟随模型能力：请求值先按模型输出上限 clamp，避免超过模型
        # 实际上限（API 拒绝）或低于需求（长任务被截断）。
        effective_max_tokens = model_output_token_limit(settings.get("model", ""), max_tokens)
        payload = {
            "model": settings["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": effective_max_tokens,
        }
        try:
            client = await _app_call("llm_http_client")
            response = await client.post(
                chat_completions_url(settings["base_url"]),
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            _app_call("schedule_llm_usage_event", 
                settings,
                status="failed",
                error_kind=_llm_error_kind(exc),
                latency_ms=int((time.monotonic() - started_at) * 1000),
                input_tokens=sum(len(str(message.get("content") or "")) for message in messages) // 4,
                purpose=purpose,
            )
            raise
        try:
            message = body["choices"][0]["message"]
            content = message.get("content", "") or ""
            reasoning = message.get("reasoning_content", "") or ""
            finish_reason = str((body.get("choices") or [{}])[0].get("finish_reason") or "")
        except (KeyError, IndexError, TypeError) as exc:
            error = RuntimeError("LLM 返回格式不符合 OpenAI Chat Completions 规范")
            _app_call("schedule_llm_usage_event", 
                settings,
                status="failed",
                error_kind="invalid_response",
                latency_ms=int((time.monotonic() - started_at) * 1000),
                input_tokens=sum(len(str(item.get("content") or "")) for item in messages) // 4,
                purpose=purpose,
            )
            raise error from exc
        # 输出被 max_tokens 截断：finish_reason=length 说明 LLM 还有内容没写完。
        # 记录告警事件，便于排查"回答不完整"。
        if finish_reason == "length":
            truncated_flag["hit"] = True
            try:
                _app_call("schedule_llm_usage_event", 
                    settings,
                    status="truncated",
                    error_kind="max_tokens_exceeded",
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                    input_tokens=sum(len(str(message.get("content") or "")) for message in messages) // 4,
                    output_tokens=len(str(content or reasoning)) // 4,
                    purpose=purpose,
                )
            except Exception:
                log.debug("忽略异常（call_once）", exc_info=True)
        if isinstance(content, list):
            content = "\n".join(str(item.get("text", item)) for item in content)
        result = str(content).strip() or str(reasoning).strip()
        if not result:
            error = RuntimeError("LLM 返回为空")
            _app_call("schedule_llm_usage_event", 
                settings,
                status="failed",
                error_kind=_llm_error_kind(error),
                latency_ms=int((time.monotonic() - started_at) * 1000),
                input_tokens=sum(len(str(message.get("content") or "")) for message in messages) // 4,
                purpose=purpose,
            )
            raise error
        usage = body.get("usage") if isinstance(body, dict) and isinstance(body.get("usage"), dict) else {}
        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or (sum(len(str(message.get("content") or "")) for message in messages) // 4))
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or (len(result) // 4))
        _app_call("schedule_llm_usage_event", 
            settings,
            status="succeeded",
            latency_ms=int((time.monotonic() - started_at) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            purpose=purpose,
        )
        return result

    if credentials is not None:
        try:
            result = await call_once(credentials)
            if track_health:
                _app_call("_record_llm_success", credentials)
            return (result, truncated_flag["hit"]) if want_truncated else result
        except Exception as exc:
            if track_health:
                _app_call("_record_llm_failure", credentials, exc)
            raise

    state = _app_call("llm_provider_state")
    candidates = state["candidates"]
    if not candidates:
        raise RuntimeError("未配置可调用的 LLM Provider")

    first_error: Exception | None = None
    errors: list[str] = []
    for index, candidate in enumerate(candidates):
        health = _app_call("_llm_health", candidate)
        if health.get("status") == "cooling":
            errors.append(f"{candidate.get('name', '未命名')}:rate_limit_cooling")
            continue
        try:
            result = await call_once(candidate)
            if track_health:
                _app_call("_record_llm_success", candidate)
            return (result, truncated_flag["hit"]) if want_truncated else result
        except Exception as exc:
            if track_health:
                _app_call("_record_llm_failure", candidate, exc)
            error_kind = _llm_error_kind(exc)
            errors.append(f"{candidate.get('name', '未命名')}:{error_kind}")
            if first_error is None:
                first_error = exc
            if not _llm_error_retryable(error_kind):
                # Do not hide deterministic configuration/contract failures
                # behind a later fallback. The category contains no upstream
                # response body or credential-bearing request details.
                raise RuntimeError(f"LLM 调用失败：{candidate.get('name', '未命名')}:{error_kind}") from exc
            continue
    if first_error:
        raise RuntimeError(f"LLM 调用失败：{'；'.join(errors) or _llm_error_kind(first_error)}") from first_error
    if candidates and all(_app_call("_llm_health", candidate).get("status") == "cooling" for candidate in candidates):
        raise RuntimeError("所有 LLM Provider 当前处于冷却中，请稍后重试")
    raise RuntimeError("LLM 调用失败")






def _is_markdown_table_divider(line: str) -> bool:
    """|---|:--:|---| 这种表格分隔行。

    前端 static/markdown.js 里有同名判断，两边必须保持一致：一边认成表格、
    另一边认成普通行的话，正文渲染出一张表，「结构化结果」里却是一堆碎条目。
    """
    text = str(line or "").strip()
    return bool(text) and "-" in text and re.fullmatch(r"\|?[\s:|-]*-[\s:|-]*\|?", text) is not None





def _contract_id_list(values: Any) -> list[str]:
    """Normalize trace IDs without assuming every store uses the same type."""
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("id") or value.get("artifact_id") or value.get("work_item_id") or value.get("relation_id")
        if value in (None, ""):
            continue
        normalized = str(value)
        if normalized not in result:
            result.append(normalized)
    return result[:100]



@app.get("/api/settings/llm")
def get_llm_settings() -> dict[str, Any]:
    state = _app_call("llm_provider_state")
    return {
        "llm": llm_settings(),
        "has_global_key": bool(state["candidates"]),
        "has_primary_config": state["primary_present"],
    }


@app.post("/api/settings/llm")

@app.post("/api/settings/llm")
def save_llm_settings(request: LLMSettingsRequest) -> dict[str, Any]:
    providers = request.providers

    saved = load_saved_llm_settings()
    existing = normalize_llm_providers(saved)
    existing_by_id = {str(item.get("id")): item for item in existing}
    existing_by_name = {str(item.get("name")): item for item in existing}
    normalized: list[dict[str, str]] = []
    used_provider_ids: set[str] = set()
    primary_count = 0
    for index, raw in enumerate(providers[:20]):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or f"Provider {index + 1}").strip()[:120]
        provider_id = _unique_llm_provider_id(raw.get("id") or name, index, used_provider_ids)
        role = str(raw.get("role") or "fallback").strip().lower()
        raw_base_url = str(raw.get("base_url") or "").strip()
        base_url = normalize_llm_base_url(raw_base_url)
        api_key = str(raw.get("api_key") or "").strip()
        model = str(raw.get("model") or "").strip()
        previous = existing_by_id.get(provider_id) or existing_by_name.get(name)
        if not api_key and raw.get("preserve_api_key", True) and previous:
            api_key = str(previous.get("api_key") or "")
        # An empty key preserves the existing credential by default.  The UI
        # sends clear_api_key=true only after the user explicitly asks to
        # remove it; the provider entry itself remains visible and reusable.
        if raw.get("clear_api_key"):
            api_key = ""
        if role not in {"primary", "fallback"}:
            role = "fallback"
        # An entry may be saved before it is complete.  This lets the UI keep
        # a named provider in the fallback order while clearly marking it as
        # disabled until its address, model and key are available.  Supplied
        # values are still validated so a typo cannot become a callable route.
        if raw_base_url and not base_url:
            raise HTTPException(400, f"{name}：API 地址必须是 http/https，且不能包含用户名、密码、查询参数或 URL 片段")
        if role == "primary":
            primary_count += 1
        normalized.append({
            "id": provider_id,
            "name": name,
            "role": role,
            "base_url": base_url,
            "api_key": _clean_llm_token(api_key),
            "model": model,
        })
    if primary_count > 1:
        raise HTTPException(400, "主配置只能有一个，其余条目请设为 fallback")

    save_global_llm_settings({"providers": normalized})

    return {
        "ok": True,
        "llm": llm_settings(),
        "has_global_key": bool(_app_call("llm_provider_state", )["candidates"]),
        "has_primary_config": primary_count > 0,
    }


@app.post("/api/settings/llm/test")

@app.post("/api/settings/llm/test")
async def test_llm_settings(request: LLMTestRequest) -> dict[str, Any]:
    raw_base_url = request.base_url.strip()
    # A supplied address is an explicit user choice. Never silently replace an
    # invalid new value with the address from a saved Provider.
    if raw_base_url and not valid_http_url(raw_base_url):
        raise HTTPException(400, "API 地址必须是 http/https，且不能包含用户名、密码、查询参数或 URL 片段")
    base_url = normalize_llm_base_url(raw_base_url)
    model = request.model.strip()
    api_key = request.api_key.strip()
    name = request.name.strip() or "测试条目"
    # 空参数时优先按稳定 provider_id 找回已保存条目，再回退到名称、主配置或当前生效条目。
    state = _app_call("llm_provider_state")
    saved_provider = next((item for item in state["providers"] if request.provider_id and item.get("id") == request.provider_id), None)
    # Name lookup is retained only for old clients that do not know provider_id.
    saved_provider = saved_provider or next((item for item in state["providers"] if not request.provider_id and name and item.get("name") == name), None)
    if not (base_url and model and api_key):
        # A stable id identifies the exact row the user clicked.  Never fall
        # back to the primary provider for a new/unsaved row, otherwise
        # testing an incomplete entry could silently test a different model.
        selected = saved_provider or (
            {}
            if request.provider_id
            else state.get("primary") or (state.get("candidates") or [None])[0] or {}
        )
        base_url = base_url or str(selected.get("base_url") or "")
        model = model or str(selected.get("model") or "")
        api_key = api_key or str(selected.get("api_key") or "")
        name = name or str(selected.get("name") or "已保存配置")
    if not base_url:
        raise HTTPException(400, "API 地址不能为空")
    if not model:
        raise HTTPException(400, "模型名不能为空")
    if not api_key:
        raise HTTPException(400, "API Key 不能为空（未保存可用凭证）")

    started_at = time.monotonic()
    try:
        await call_llm(
            [
                {"role": "system", "content": "你正在进行接口连通性测试。只回复：连接成功。"},
                {"role": "user", "content": "请回复连接成功。"},
            ],
            {**{"id": request.provider_id or _llm_provider_id(name), "name": name, "api_key": _clean_llm_token(api_key), "base_url": base_url, "model": model}, "source": "test", "provider": "test"},
            max_tokens=24,
            temperature=0,
            purpose="test",
            track_health=False,
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            reason = "认证失败：API Key 无效或没有该模型权限"
        elif status == 404:
            reason = "接口路径或模型名不存在"
        elif status == 429:
            reason = "限流：请求过多或模型冷却中"
        else:
            reason = f"接口返回 {status}"
        raise HTTPException(502, reason) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(502, "连接超时：请检查地址可达性或网络") from exc
    except Exception as exc:
        raise HTTPException(502, f"连接失败：{_llm_error_kind(exc)}") from exc
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return {"ok": True, "message": "连接成功", "provider": name, "model": model, "endpoint": chat_completions_url(base_url), "latency_ms": elapsed_ms, "endpoint_policy": LLM_ENDPOINT_POLICY}

__all__ = ["_app_call", "load_saved_llm_settings", "save_global_llm_settings", "_clean_llm_token", "_llm_provider_id", "_llm_provider_usable", "_llm_provider_disabled_reason", "_unique_llm_provider_id", "_llm_error_kind", "_llm_error_retryable", "_llm_health", "_persist_llm_health", "_record_llm_success", "_record_llm_failure", "record_llm_usage_event", "schedule_llm_usage_event", "llm_usage_metrics_payload", "_llm_key_from_environment", "normalize_llm_providers", "llm_fallback_credentials", "llm_provider_state", "llm_credentials", "llm_effective_candidate", "chat_completions_url", "llm_settings", "valid_http_url", "valid_research_url", "normalize_llm_base_url", "model_output_token_limit", "_llm_timeout", "llm_http_client", "stream_llm_text", "stream_llm_with_tools", "call_llm", "_call_llm_once", "_is_markdown_table_divider", "_contract_id_list", "get_llm_settings", "save_llm_settings", "test_llm_settings", "LLMSettingsRequest", "LLMTestRequest", "LLM_PROVIDER_HEALTH", "LLM_PROVIDER_COOLDOWN_SECONDS", "MODEL_OUTPUT_TOKEN_LIMITS", "LLM_MAX_CONTINUATIONS"]