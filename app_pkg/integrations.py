"""Workbench 集成领域：外部服务接入（config/test/import）。

从 app.py 拆出的领域模块（为开源准备）。依赖 core（INTEGRATIONS_FILE/工具）、
db 与 fastapi HTTPException；INTEGRATION_DEFINITIONS 常量仍留 app.py（延迟读）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

from .core import (
    DATA_DIR,
    INTEGRATIONS_FILE,
    KNOWLEDGE_DIR,
    OUTPUTS_DIR,
    WORKBENCH_PUBLIC_URL,
    WORKBENCH_VERSION,
    clip,
    load_json_file,
    log,
    now_iso,
    save_json_atomic,
)
from .db import db_connection
from .instance import app




def create_relation_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_relation_record（仍在 app.py）。"""
    import app as _app

    return _app.create_relation_record(*args, **kwargs)


def create_work_item_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_work_item_record（仍在 app.py）。"""
    import app as _app

    return _app.create_work_item_record(*args, **kwargs)


def decode_json_column(value: str | None) -> dict[str, Any]:
    """延迟转发 app.decode_json_column（仍在 app.py）。"""
    import app as _app

    return _app.decode_json_column(value)


def valid_http_url(value: str) -> bool:
    """延迟转发 app.valid_http_url（仍在 app.py）。"""
    import app as _app

    return _app.valid_http_url(value)


def schedule_ntfy_notification(notification: dict[str, Any]) -> None:
    """延迟转发 app.schedule_ntfy_notification（仍在 app.py）。"""
    import app as _app

    _app.schedule_ntfy_notification(notification)


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
        "description": "读取自托管任务系统中的未完成任务，人工选择后进入收件箱；NEXUS 仍保留来源和审计主线。",
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




def load_integrations() -> dict[str, dict[str, Any]]:
    values = load_json_file(INTEGRATIONS_FILE, {})
    return values if isinstance(values, dict) else {}


def _integration_values(integration_id: str) -> tuple[dict[str, str], str, dict[str, Any]]:
    definition = INTEGRATION_DEFINITIONS.get(integration_id)
    if not definition:
        raise HTTPException(404, "集成不存在")
    saved = load_integrations().get(integration_id)
    saved = saved if isinstance(saved, dict) else {}
    saved_values = saved.get("values") if isinstance(saved.get("values"), dict) else {}
    values: dict[str, str] = {}
    source = ""
    env_keys = INTEGRATION_ENV_KEYS.get(integration_id, {})
    for field in definition["fields"]:
        env_name = env_keys.get(field, "")
        env_value = os.getenv(env_name, "").strip() if env_name else ""
        saved_value = str(saved_values.get(field) or "").strip()
        if env_value:
            values[field] = env_value
            source = "env"
        elif saved_value:
            values[field] = saved_value
            source = source or "saved"
        else:
            values[field] = str((definition.get("defaults") or {}).get(field) or "")
    return values, source, saved


def github_repository_parts(values: dict[str, str]) -> tuple[str, str]:
    """Validate the repository path before putting user input into a URL."""
    owner = str(values.get("owner") or "").strip()
    repo = str(values.get("repo") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", owner) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repo):
        raise HTTPException(400, "GitHub：用户名/组织和仓库名只能包含字母、数字、点、下划线或短横线")
    return owner, repo


def integration_status(integration_id: str) -> dict[str, Any]:
    definition = INTEGRATION_DEFINITIONS.get(integration_id)
    if not definition:
        raise HTTPException(404, "集成不存在")
    values, source, saved = _integration_values(integration_id)
    required = definition.get("required") or list(definition["fields"])
    configured = all(str(values.get(field) or "").strip() for field in required)
    public_values = {field: value for field, value in values.items() if field not in definition["secret_fields"]}
    secret_state = {f"has_{field}": bool(values.get(field)) for field in definition["secret_fields"]}
    return {
        "id": integration_id,
        "name": definition["name"],
        "repo": definition["repo"],
        "kind": definition["kind"],
        "description": definition["description"],
        "configuration_cost": definition.get("configuration_cost") or "按需配置服务地址；密钥由用户自行提供",
        "data_boundary": definition.get("data_boundary") or "只读读取；导入前需要人工勾选，不自动写回第三方服务",
        "next_step": definition.get("next_step") or "配置后测试连接，再读取最新条目并人工选择导入",
        "fields": definition["fields"],
        "values": public_values,
        **secret_state,
        "configured": configured,
        "enabled": bool(saved.get("enabled", True)) if source != "env" else True,
        "source": source or "未配置",
        "last_test_at": str(saved.get("last_test_at") or ""),
        "last_test_status": str(saved.get("last_test_status") or ""),
        "last_error": str(saved.get("last_error") or ""),
    }


def save_integration_config(integration_id: str, values: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    definition = INTEGRATION_DEFINITIONS.get(integration_id)
    if not definition:
        raise HTTPException(404, "集成不存在")
    all_values = load_integrations()
    previous = all_values.get(integration_id) if isinstance(all_values.get(integration_id), dict) else {}
    previous_values = previous.get("values") if isinstance(previous.get("values"), dict) else {}
    normalized: dict[str, str] = {}
    for field in definition["fields"]:
        supplied = values.get(field)
        # Secret inputs are intentionally blank when the UI already has a
        # saved value.  Blank means “keep it”, while an explicit clear can be
        # added later without ever echoing the secret back to the browser.
        if field in definition["secret_fields"] and (supplied is None or not str(supplied).strip()) and previous_values.get(field):
            supplied = previous_values.get(field)
        normalized[field] = str(supplied if supplied is not None else "").strip()
    if normalized.get("base_url") and not valid_http_url(normalized["base_url"]):
        raise HTTPException(400, "集成地址必须是 http/https URL")
    all_values[integration_id] = {**previous, "enabled": bool(enabled), "values": normalized, "updated_at": now_iso()}
    save_json_atomic(INTEGRATIONS_FILE, all_values, 0o600)
    return integration_status(integration_id)


def update_integration_test(integration_id: str, status: str, error: str = "") -> None:
    all_values = load_integrations()
    saved = all_values.get(integration_id) if isinstance(all_values.get(integration_id), dict) else {}
    all_values[integration_id] = {**saved, "last_test_at": now_iso(), "last_test_status": status, "last_error": clip(error, 500)}
    save_json_atomic(INTEGRATIONS_FILE, all_values, 0o600)


def integration_url(integration_id: str, path: str = "") -> str:
    values, _source, _saved = _integration_values(integration_id)
    base_url = str(values.get("base_url") or "").strip().rstrip("/")
    if not base_url or not valid_http_url(base_url):
        raise HTTPException(400, f"{INTEGRATION_DEFINITIONS[integration_id]['name']}：请先填写合法的服务地址")
    return f"{base_url}/{path.lstrip('/')}" if path else base_url


def integration_headers(integration_id: str, values: dict[str, str] | None = None) -> dict[str, str]:
    values = values or _integration_values(integration_id)[0]
    headers = {"User-Agent": f"Workbench/{WORKBENCH_VERSION or '0.3'}"}
    if integration_id == "github":
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        if values.get("token"):
            headers["Authorization"] = f"Bearer {values['token']}"
    elif integration_id == "miniflux" and values.get("api_token"):
        headers["Authorization"] = f"Bearer {values['api_token']}"
    elif integration_id == "zotero" and values.get("api_key"):
        headers["Zotero-API-Key"] = values["api_key"]
        headers["Zotero-API-Version"] = "3"
    elif integration_id == "ntfy" and values.get("token"):
        headers["Authorization"] = f"Bearer {values['token']}"
    elif integration_id in {"linkding", "paperless"} and values.get("token"):
        headers["Authorization"] = f"Token {values['token']}"
    elif integration_id == "vikunja" and values.get("api_token"):
        headers["Authorization"] = f"Bearer {values['api_token']}"
    elif integration_id == "wallabag" and values.get("access_token"):
        headers["Authorization"] = f"Bearer {values['access_token']}"
    if integration_id == "searxng":
        headers["Accept"] = "application/json"
    return headers


def integration_http_error(integration_id: str, exc: Exception) -> HTTPException:
    definition = INTEGRATION_DEFINITIONS.get(integration_id, {})
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in {401, 403}:
        message = "凭据无效或没有访问权限"
    elif status_code == 404:
        message = "服务地址或 API 路径不存在"
    elif status_code == 429:
        message = "第三方服务限流，请稍后重试"
    elif isinstance(exc, httpx.TimeoutException):
        message = "连接超时，请确认线上服务可从服务器访问"
    elif isinstance(exc, httpx.RequestError):
        message = "网络不可达，请确认服务地址、DNS 或代理配置"
    else:
        message = "第三方服务返回了无法处理的响应"
    return HTTPException(502, f"{definition.get('name', integration_id)}：{message}")


async def test_integration_connection(integration_id: str) -> dict[str, Any]:
    values, _source, _saved = _integration_values(integration_id)
    definition = INTEGRATION_DEFINITIONS[integration_id]
    required = definition.get("required") or list(definition["fields"])
    missing = [field for field in required if not values.get(field)]
    if missing:
        raise HTTPException(400, f"{definition['name']}：还缺少 {', '.join(definition['fields'].get(field, field) for field in missing)}")
    if integration_id == "github":
        owner, repo = github_repository_parts(values)
        url = integration_url(integration_id, f"/repos/{owner}/{repo}")
    elif integration_id == "ntfy":
        url = integration_url(integration_id, "/v1/health")
    elif integration_id == "miniflux":
        url = integration_url(integration_id, "/v1/me")
    elif integration_id == "activitywatch":
        url = integration_url(integration_id, "/api/0/buckets")
    elif integration_id == "linkding":
        url = integration_url(integration_id, "/api/bookmarks/?limit=1")
    elif integration_id == "paperless":
        url = integration_url(integration_id, "/api/documents/?page_size=1")
    elif integration_id == "vikunja":
        url = integration_url(integration_id, "/api/v1/info")
    elif integration_id == "searxng":
        url = integration_url(integration_id, "/search")
        params = {"q": "workbench", "format": "json", "categories": values.get("categories") or "general"}
    elif integration_id == "wallabag":
        url = integration_url(integration_id, "/api/entries.json")
        params = {"archive": 0, "perPage": 1}
    else:
        user_id = re.sub(r"[^A-Za-z0-9_-]", "", values.get("user_id", ""))
        if not user_id:
            raise HTTPException(400, "Zotero：用户 ID 格式无效")
        url = integration_url(integration_id, f"/users/{user_id}/items?limit=1&format=json")
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=True, follow_redirects=True, headers=integration_headers(integration_id, values)) as client:
            response = await client.get(url, params=params if "params" in locals() else None)
            response.raise_for_status()
            if integration_id in {"searxng", "wallabag"} and not isinstance(response.json(), (dict, list)):
                raise ValueError("响应不是 JSON 对象或数组")
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        error = integration_http_error(integration_id, exc)
        update_integration_test(integration_id, "failed", str(error.detail))
        raise error from exc
    latency_ms = round((time.perf_counter() - started) * 1000)
    update_integration_test(integration_id, "succeeded")
    return {"ok": True, "status_code": response.status_code, "latency_ms": latency_ms}


class IntegrationConfigRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict, max_length=20)
    enabled: bool = True


class IntegrationImportRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, min_length=1, max_length=50)


class IntegrationNotificationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(default="", max_length=2_000)
    href: str = Field(default="/", max_length=2_000)
    priority: str = Field(default="default", pattern="^(min|low|default|high|max)$")


def require_integration(integration_id: str) -> dict[str, Any]:
    definition = INTEGRATION_DEFINITIONS.get(integration_id)
    if not definition:
        raise HTTPException(404, "集成不存在")
    return definition


@app.get("/api/integrations")
async def get_integrations() -> dict[str, Any]:
    return {"integrations": [integration_status(integration_id) for integration_id in INTEGRATION_DEFINITIONS], "generated_at": now_iso()}


@app.post("/api/integrations/{integration_id}/config")
async def configure_integration(integration_id: str, request: IntegrationConfigRequest) -> dict[str, Any]:
    require_integration(integration_id)
    return {"ok": True, "integration": save_integration_config(integration_id, request.values, request.enabled)}


@app.post("/api/integrations/{integration_id}/test")
async def test_integration(integration_id: str) -> dict[str, Any]:
    require_integration(integration_id)
    result = await test_integration_connection(integration_id)
    return {**result, "integration": integration_status(integration_id), "message": f"{INTEGRATION_DEFINITIONS[integration_id]['name']} 连接成功"}


@app.get("/api/integrations/{integration_id}/items")
async def get_integration_items(integration_id: str, limit: int = 20) -> dict[str, Any]:
    require_integration(integration_id)
    return {"integration": integration_status(integration_id), "items": await fetch_integration_items(integration_id, limit)}


@app.post("/api/integrations/{integration_id}/import")
async def import_integration_items(integration_id: str, request: IntegrationImportRequest) -> dict[str, Any]:
    require_integration(integration_id)
    items = await fetch_integration_items(integration_id, max(len(request.ids), 20))
    selected = {str(item_id) for item_id in request.ids}
    selected_items = [item for item in items if str(item.get("id") or "") in selected]
    if not selected_items:
        raise HTTPException(404, "没有找到可导入的远端条目；请先重新读取最新条目")

    known_ids: set[str] = set()
    connection = db_connection()
    try:
        rows = connection.execute("SELECT metadata_json FROM work_items WHERE kind IN ('integration_item', 'efficiency_observation') ORDER BY id DESC LIMIT 500").fetchall()
        for row in rows:
            metadata = decode_json_column(row["metadata_json"])
            if metadata.get("integration_id") == integration_id and metadata.get("integration_item_id"):
                known_ids.add(str(metadata["integration_item_id"]))
    finally:
        connection.close()

    target_project = (
        "knowledge" if integration_id in {"zotero", "paperless"}
        else "inbox" if integration_id == "github"
        else "inbox" if integration_id == "vikunja"
        else "workbench" if integration_id == "activitywatch"
        else "crawl4ai"
    )
    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    for remote_item in selected_items:
        remote_id = str(remote_item.get("id") or "")
        if remote_id in known_ids:
            skipped.append(remote_id)
            continue
        source = str(remote_item.get("source") or INTEGRATION_DEFINITIONS[integration_id]["name"])
        description = "\n".join(filter(None, [
            str(remote_item.get("summary") or "").strip(),
            f"来源：{source}",
            f"链接：{remote_item.get('url')}" if remote_item.get("url") else "",
            "请在目标 Agent 中继续处理，并保留来源和下一步。",
        ]))
        item = await asyncio.to_thread(create_work_item_record, 
            title=f"{INTEGRATION_DEFINITIONS[integration_id]['name']}：{str(remote_item.get('title') or '未命名条目')[:220]}",
            description=description,
            kind="efficiency_observation" if integration_id == "activitywatch" else "integration_item",
            source_project="workbench",
            target_project=target_project,
            metadata={
                "integration_id": integration_id,
                "integration_item_id": remote_id,
                "source_id": remote_id,
                "source": source,
                "url": str(remote_item.get("url") or ""),
                "published_at": str(remote_item.get("published_at") or ""),
                "source_updated_at": str((remote_item.get("metadata") or {}).get("source_updated_at") or remote_item.get("published_at") or ""),
                "remote_metadata": remote_item.get("metadata") if isinstance(remote_item.get("metadata"), dict) else {},
                "target_project": target_project,
            },
        )
        relation = await asyncio.to_thread(create_relation_record, 
            from_type="integration_item",
            from_id=remote_id,
            to_type="work_item",
            to_id=str(item["id"]),
            relation_type="imported_to_work_item",
            metadata={
                "integration_id": integration_id,
                "target_project": target_project,
                "source_id": remote_id,
                "source_updated_at": str((remote_item.get("metadata") or {}).get("source_updated_at") or remote_item.get("published_at") or ""),
            },
        )
        created.append({"item": item, "relation": relation})
        known_ids.add(remote_id)
    return {"ok": True, "created": len(created), "skipped": skipped, "items": created, "target_project": target_project}


@app.post("/api/integrations/ntfy/notify")
async def notify_via_ntfy(request: IntegrationNotificationRequest) -> dict[str, Any]:
    require_integration("ntfy")
    try:
        result = await send_ntfy_message(title=request.title, body=request.body, href=request.href, priority=request.priority)
    except HTTPException as exc:
        update_integration_test("ntfy", "notify_failed", str(exc.detail))
        raise
    update_integration_test("ntfy", "notification_sent")
    return {"ok": True, "delivery": result, "message": "ntfy 通知已发送"}


async def fetch_integration_items(integration_id: str, limit: int = 20) -> list[dict[str, Any]]:
    values, _source, _saved = _integration_values(integration_id)
    definition = INTEGRATION_DEFINITIONS[integration_id]
    required = definition.get("required") or list(definition["fields"])
    missing = [field for field in required if not values.get(field)]
    if missing:
        raise HTTPException(400, f"{definition['name']}：请先完成配置")
    limit = max(1, min(int(limit), 50))
    if integration_id == "github":
        owner, repo = github_repository_parts(values)
        url = integration_url(integration_id, f"/repos/{owner}/{repo}/issues?state=open&per_page={limit}")
    elif integration_id == "ntfy":
        raise HTTPException(400, "ntfy 是通知出口，不提供可导入条目")
    elif integration_id == "miniflux":
        url = integration_url(integration_id, f"/v1/entries?status=unread&limit={limit}")
    elif integration_id == "activitywatch":
        url = integration_url(integration_id, "/api/0/buckets")
    elif integration_id == "linkding":
        url = integration_url(integration_id, f"/api/bookmarks/?limit={limit}")
    elif integration_id == "paperless":
        url = integration_url(integration_id, f"/api/documents/?page_size={limit}&ordering=-added")
    elif integration_id == "vikunja":
        project_id = str(values.get("project_id") or "").strip()
        if project_id:
            url = integration_url(integration_id, f"/api/v1/projects/{quote(project_id, safe='')}/tasks?per_page={limit}&sort_by=due_date&order_by=asc")
        else:
            url = integration_url(integration_id, f"/api/v1/tasks?per_page={limit}&sort_by=due_date&order_by=asc")
    elif integration_id == "searxng":
        url = integration_url(integration_id, "/search")
        params = {
            "q": str(values.get("query") or "").strip(),
            "format": "json",
            "categories": str(values.get("categories") or "general").strip() or "general",
            "pageno": 1,
        }
    elif integration_id == "wallabag":
        url = integration_url(integration_id, "/api/entries.json")
        params = {"archive": 0, "perPage": limit}
    else:
        user_id = re.sub(r"[^A-Za-z0-9_-]", "", values.get("user_id", ""))
        if not user_id:
            raise HTTPException(400, "Zotero：用户 ID 格式无效")
        url = integration_url(integration_id, f"/users/{user_id}/items?limit={limit}&format=json&sort=dateAdded&direction=desc")
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=True, follow_redirects=True, headers=integration_headers(integration_id, values)) as client:
            response = await client.get(url, params=params if "params" in locals() else None)
            response.raise_for_status()
            payload = response.json()
            if integration_id == "activitywatch":
                buckets = _activitywatch_buckets(payload)
                configured_bucket = str(values.get("bucket_id") or "").strip()
                if configured_bucket:
                    buckets = [bucket for bucket in buckets if bucket["id"] == configured_bucket]
                window_end = datetime.now(timezone.utc)
                window_start = window_end - timedelta(days=7)
                window_start_text = window_start.isoformat()
                window_end_text = window_end.isoformat()
                bucket_limit = min(limit, 10)
                activity_items: list[dict[str, Any]] = []
                for bucket in buckets[:bucket_limit]:
                    bucket_url = integration_url("activitywatch", f"/api/0/buckets/{quote(bucket['id'], safe='')}/events")
                    event_response = await client.get(bucket_url, params={"start": window_start_text, "end": window_end_text})
                    event_response.raise_for_status()
                    events = _activitywatch_events(event_response.json())
                    total_seconds = _activitywatch_duration(events)
                    hours = round(total_seconds / 3600, 1)
                    activity_items.append({
                        "id": f"activitywatch:{bucket['id']}:{window_start.date().isoformat()}",
                        "title": f"{bucket['name']} · 近 7 天效率观察",
                        "summary": f"近 7 天记录 {len(events)} 个事件，共 {hours} 小时。仅保留聚合时长，不保存窗口标题或网页 URL。",
                        "url": integration_url("activitywatch"),
                        "published_at": window_end_text,
                        "source": f"ActivityWatch · {bucket['name']}",
                        "metadata": {
                            "remote_id": bucket["id"],
                            "kind": "time_summary",
                            "bucket_id": bucket["id"],
                            "bucket_type": bucket.get("type", ""),
                            "client": bucket.get("client", ""),
                            "event_count": len(events),
                            "total_seconds": total_seconds,
                            "range_start": window_start_text,
                            "range_end": window_end_text,
                            "source_updated_at": window_end_text,
                            "privacy": "aggregated_duration_only",
                        },
                    })
                return sorted(activity_items, key=lambda item: item["metadata"].get("total_seconds", 0), reverse=True)[:limit]
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise integration_http_error(integration_id, exc) from exc

    normalized: list[dict[str, Any]] = []
    if integration_id == "github":
        owner, repo = github_repository_parts(values)
        raw_items = payload if isinstance(payload, list) else []
        for raw in raw_items:
            if not isinstance(raw, dict) or not raw.get("number"):
                continue
            number = str(raw.get("number"))
            is_pr = isinstance(raw.get("pull_request"), dict)
            kind = "pull_request" if is_pr else "issue"
            labels = [str(item.get("name") or "").strip() for item in (raw.get("labels") or []) if isinstance(item, dict)]
            user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
            normalized.append({
                "id": f"github:{owner}/{repo}:{number}",
                "title": str(raw.get("title") or f"未命名 {kind}")[:240],
                "summary": _plain_external_text(raw.get("body") or "", 700),
                "url": str(raw.get("html_url") or "")[:2_000],
                "published_at": str(raw.get("updated_at") or raw.get("created_at") or ""),
                "source": f"GitHub · {owner}/{repo}",
                "metadata": {
                    "remote_id": number,
                    "owner": owner,
                    "repo": repo,
                    "kind": kind,
                    "state": str(raw.get("state") or "open"),
                    "labels": labels[:20],
                    "author": str(user.get("login") or ""),
                    "source_updated_at": str(raw.get("updated_at") or ""),
                },
            })
    elif integration_id == "miniflux":
        raw_items = payload.get("entries", []) if isinstance(payload, dict) else []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("id") or raw.get("hash") or "").strip()
            if not item_id:
                continue
            normalized.append({
                "id": f"miniflux:{item_id}",
                "title": str(raw.get("title") or "未命名订阅条目")[:240],
                "summary": _plain_external_text(raw.get("content") or raw.get("description"), 700),
                "url": str(raw.get("url") or raw.get("external_url") or "")[:2_000],
                "published_at": str(raw.get("published_at") or raw.get("created_at") or ""),
                "source": str(raw.get("feed_title") or raw.get("feed_id") or "Miniflux"),
                "metadata": {
                    "remote_id": item_id,
                    "kind": "article",
                    "status": raw.get("status", "unread"),
                    "source_updated_at": str(raw.get("published_at") or raw.get("created_at") or ""),
                },
            })
    elif integration_id == "zotero":
        raw_items = payload if isinstance(payload, list) else []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
            item_id = str(raw.get("key") or raw.get("id") or "").strip()
            title = str(data.get("title") or data.get("shortTitle") or "未命名研究条目").strip()
            if not item_id or (not title and not data.get("DOI")):
                continue
            creators = data.get("creators") if isinstance(data.get("creators"), list) else []
            creator_names = [str(item.get("lastName") or item.get("name") or "").strip() for item in creators if isinstance(item, dict)]
            author = ", ".join(name for name in creator_names if name)[:240]
            doi = str(data.get("DOI") or "").strip()
            url = str(data.get("url") or (f"https://doi.org/{doi}" if doi else "")).strip()
            normalized.append({
                "id": f"zotero:{item_id}",
                "title": title[:240],
                "summary": _plain_external_text(data.get("abstractNote") or "", 700),
                "url": url[:2_000],
                "published_at": str(data.get("date") or data.get("dateAdded") or ""),
                "source": author or "Zotero",
                "metadata": {
                    "remote_id": item_id,
                    "kind": str(data.get("itemType") or "item"),
                    "doi": doi,
                    "item_type": data.get("itemType", ""),
                    "source_updated_at": str(data.get("dateAdded") or data.get("date") or ""),
                },
            })
    elif integration_id == "linkding":
        raw_items = payload.get("results", []) if isinstance(payload, dict) else []
        for raw in raw_items:
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            item_id = str(raw.get("id"))
            normalized.append({
                "id": f"linkding:{item_id}",
                "title": str(raw.get("title") or raw.get("url") or "未命名书签")[:240],
                "summary": _plain_external_text(raw.get("description") or "", 700),
                "url": str(raw.get("url") or "")[:2_000],
                "published_at": str(raw.get("date_added") or raw.get("date_modified") or ""),
                "source": "Linkding",
                "metadata": {
                    "remote_id": item_id,
                    "kind": "bookmark",
                    "tags": [str(tag).strip() for tag in (raw.get("tag_names") or raw.get("tags") or []) if str(tag).strip()][:20],
                    "unread": bool(raw.get("unread")),
                    "archived": bool(raw.get("is_archived") or raw.get("archived")),
                    "source_updated_at": str(raw.get("date_modified") or raw.get("date_added") or ""),
                },
            })
    elif integration_id == "paperless":
        raw_items = payload.get("results", []) if isinstance(payload, dict) else []
        base_url = str(values.get("base_url") or "").rstrip("/")
        for raw in raw_items:
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            item_id = str(raw.get("id"))
            updated_at = str(raw.get("modified") or raw.get("added") or "")
            normalized.append({
                "id": f"paperless:{item_id}",
                "title": str(raw.get("title") or raw.get("original_file_name") or "未命名文档")[:240],
                "summary": _plain_external_text(raw.get("notes") or raw.get("content") or "", 700),
                "url": f"{base_url}/documents/{item_id}/details",
                "published_at": updated_at,
                "source": "Paperless-ngx",
                "metadata": {
                    "remote_id": item_id,
                    "kind": "archived_document",
                    "original_file_name": str(raw.get("original_file_name") or "")[:240],
                    "tags": [str(tag).strip() for tag in (raw.get("tags") or []) if str(tag).strip()][:20],
                    "correspondent": str(raw.get("correspondent") or "")[:160],
                    "document_type": str(raw.get("document_type") or "")[:160],
                    "source_updated_at": updated_at,
                },
            })
    elif integration_id == "vikunja":
        raw_items = payload.get("tasks", []) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
        base_url = str(values.get("base_url") or "").rstrip("/")
        for raw in raw_items:
            if not isinstance(raw, dict) or raw.get("id") is None or bool(raw.get("done")):
                continue
            item_id = str(raw.get("id"))
            project = raw.get("project") if isinstance(raw.get("project"), dict) else {}
            labels = raw.get("labels") if isinstance(raw.get("labels"), list) else []
            label_names = [str(label.get("title") or label.get("name") or "").strip() for label in labels if isinstance(label, dict)]
            updated_at = str(raw.get("updated") or raw.get("created") or "")
            due_date = str(raw.get("dueDate") or raw.get("due_date") or "")
            normalized.append({
                "id": f"vikunja:{item_id}",
                "title": str(raw.get("title") or "未命名任务")[:240],
                "summary": _plain_external_text(raw.get("description") or "", 700),
                "url": f"{base_url}/tasks/{quote(item_id, safe='')}",
                "published_at": updated_at,
                "source": f"Vikunja · {str(project.get('title') or '任务')[:120]}",
                "metadata": {
                    "remote_id": item_id,
                    "kind": "task",
                    "status": "open",
                    "priority": raw.get("priority", 0),
                    "due_date": due_date,
                    "project_id": str(project.get("id") or raw.get("project_id") or ""),
                    "project_title": str(project.get("title") or "")[:160],
                    "labels": [name for name in label_names if name][:20],
                    "percent_done": raw.get("percentDone", raw.get("percent_done", 0)),
                    "source_updated_at": updated_at,
                    "next_step": "确认是否要处理；必要时拆分或归档；NEXUS 不会自动改写 Vikunja",
                    },
                })
    elif integration_id == "searxng":
        raw_items = payload.get("results", []) if isinstance(payload, dict) else []
        query = str(values.get("query") or "").strip()
        fetched_at = now_iso()
        for raw in raw_items:
            if not isinstance(raw, dict) or not str(raw.get("url") or "").strip():
                continue
            result_url = str(raw.get("url") or "").strip()[:2_000]
            stable_id = hashlib.sha256(result_url.encode("utf-8")).hexdigest()[:20]
            engines = [str(item).strip() for item in (raw.get("engines") or []) if str(item).strip()]
            published_at = str(raw.get("publishedDate") or raw.get("published_date") or fetched_at)
            normalized.append({
                "id": f"searxng:{stable_id}",
                "title": str(raw.get("title") or result_url)[:240],
                "summary": _plain_external_text(raw.get("content") or raw.get("snippet") or "", 700),
                "url": result_url,
                "published_at": published_at,
                "source": f"SearXNG · {', '.join(engines[:3]) or '聚合搜索'}",
                "metadata": {
                    "remote_id": stable_id,
                    "kind": "search_result",
                    "query": query[:240],
                    "engines": engines[:20],
                    "score": raw.get("score"),
                    "source_updated_at": published_at,
                    "fetched_at": fetched_at,
                },
            })
    elif integration_id == "wallabag":
        embedded = payload.get("_embedded") if isinstance(payload, dict) else {}
        raw_items = embedded.get("items", []) if isinstance(embedded, dict) else []
        if not raw_items and isinstance(payload, dict):
            raw_items = payload.get("items", [])
        for raw in raw_items:
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            item_id = str(raw.get("id"))
            tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
            tag_names = [str(tag.get("label") or tag.get("slug") or tag).strip() for tag in tags if str(tag).strip()]
            updated_at = str(raw.get("updated_at") or raw.get("created_at") or raw.get("published_at") or "")
            normalized.append({
                "id": f"wallabag:{item_id}",
                "title": str(raw.get("title") or "未命名稍后读文章")[:240],
                "summary": _plain_external_text(raw.get("content") or raw.get("excerpt") or "", 700),
                "url": str(raw.get("url") or "")[:2_000],
                "published_at": updated_at,
                "source": "Wallabag",
                "metadata": {
                    "remote_id": item_id,
                    "kind": "saved_article",
                    "tags": [name for name in tag_names if name][:20],
                    "language": str(raw.get("language") or "")[:40],
                    "reading_time": raw.get("reading_time", 0),
                    "source_updated_at": updated_at,
                },
            })
    return normalized[:limit]


async def send_ntfy_message(*, title: str, body: str, href: str = "/", priority: str = "default") -> dict[str, Any]:
    values, _source, saved = _integration_values("ntfy")
    if not all(values.get(field) for field in ("base_url", "topic")):
        raise HTTPException(400, "ntfy：请先配置服务地址和主题")
    if isinstance(saved, dict) and saved.get("enabled") is False:
        raise HTTPException(409, "ntfy 集成已停用")
    url = integration_url("ntfy", f"{values['topic'].strip('/')}")
    headers = integration_headers("ntfy", values) | {
        "Title": clip(title, 240),
        "Priority": priority,
        "Tags": "workbench",
    }
    click_url = href if valid_http_url(href) else f"{WORKBENCH_PUBLIC_URL}/{str(href or '/').lstrip('/')}"
    if valid_http_url(click_url):
        headers["Click"] = click_url
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=True, follow_redirects=True, headers=headers) as client:
            response = await client.post(url, content=clip(body, 2_000).encode("utf-8"))
            response.raise_for_status()
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                payload = {}
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise integration_http_error("ntfy", exc) from exc
    return {"ok": True, "status_code": response.status_code, "message_id": str(payload.get("id") or "") if isinstance(payload, dict) else ""}




__all__ = ["load_integrations", "_integration_values", "github_repository_parts", "integration_status", "save_integration_config", "update_integration_test", "integration_url", "integration_headers", "integration_http_error", "test_integration_connection", "require_integration", "get_integrations", "configure_integration", "test_integration", "get_integration_items", "import_integration_items", "notify_via_ntfy", "fetch_integration_items", "send_ntfy_message", "now_iso", "IntegrationConfigRequest", "IntegrationImportRequest", "IntegrationNotificationRequest", "INTEGRATION_DEFINITIONS"]


def _activitywatch_events(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        return [event for event in payload["events"] if isinstance(event, dict)]
    return []



def _activitywatch_duration(events: list[dict[str, Any]]) -> float:
    total = 0.0
    for event in events:
        try:
            duration = float(event.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if math.isfinite(duration) and duration > 0:
            total += duration
    return round(total, 3)



def _activitywatch_buckets(payload: Any) -> list[dict[str, Any]]:
    """Normalize ActivityWatch's map/list bucket responses without retaining raw events."""
    candidates: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            bucket = dict(value)
            bucket.setdefault("id", key)
            candidates.append(bucket)
    elif isinstance(payload, list):
        candidates = [dict(value) for value in payload if isinstance(value, dict)]
    normalized = []
    for bucket in candidates:
        bucket_id = str(bucket.get("id") or "").strip()
        if not bucket_id:
            continue
        normalized.append({
            "id": bucket_id,
            "name": str(bucket.get("name") or bucket.get("type") or bucket_id).strip()[:160],
            "type": str(bucket.get("type") or "").strip()[:80],
            "client": str(bucket.get("client") or "").strip()[:80],
        })
    return normalized



def _plain_external_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return clip(text, limit)


