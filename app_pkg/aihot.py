"""AI 热点 + 独立开发者看板领域。

拆自 app.py（2026-08-14 第十八批）。包含: 热点快照抓取/去重/选品、AI 热点 Agent、
独立开发者（CID）看板快照/机会/偏好、机会评审。仍在 app.py 的领域函数经 _app_call 转发。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .agent_platform import (
    AGENT_IMPLEMENTATIONS,
    AGENT_PLAYBOOKS,
    AGENT_REGISTRY,
    AGENT_TOOL_POLICIES,
    agent_result_contract,
)
from .agent_runs import (
    agent_run_summary,
    get_agent_run,
    add_agent_message,
    add_agent_run_event,
    create_agent_run_record,
    create_agent_session,
    get_agent_session,
    list_agent_messages,
    update_agent_run_record,
    update_agent_session_summary,
)
from .core import (
    AIHOT_FEED_URL,
    OUTPUTS_DIR,
    AIHOT_SNAPSHOT_FILE,
    STATIC_DIR,
    DATA_DIR,
    MAX_CONVERSATION_MESSAGES,
    clip,
    clip_for_llm,
    decode_json_column,
    load_json_file,
    log,
    now_iso,
    save_json_atomic,
)
from .db import db_connection
from .instance import app
from .sub2api import _SUB2API_CORS_PATH
from .evidence import evidence_quality_descriptor, evidence_quality_summary
from .knowledge import knowledge_tokens
from .llm import call_llm, llm_settings, stream_llm_text
from .memories import sync_cid_preferences_to_memories
from .notifications import create_notification_record
from .projects import _audit_datetime, agent_display_name, agent_status_label, project_link_summary


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


class CIDCompareRequest(BaseModel):
    repo: str = Field(min_length=1, max_length=200)
    project_keys: list[str] = Field(min_length=2, max_length=10)


class AIHotChatRequest(BaseModel):
    session_id: str = Field(default="", max_length=80)
    message: str = Field(min_length=1, max_length=8_000)
    item_ids: list[str] = Field(default_factory=list, max_length=30)
    mode: str = Field(default="useful", max_length=30)
    stream: bool = Field(default=False, description="true 时返回 SSE 流式输出")


class AIHotFeedbackRequest(BaseModel):
    item_id: str = Field(min_length=1, max_length=160)
    vote: str = Field(min_length=1, max_length=30)
    note: str = Field(default="", max_length=1_000)


class AIHotOpportunityRequest(BaseModel):
    item_id: str = Field(min_length=1, max_length=160)


class CIDSnapshotRequest(BaseModel):
    repo: str = Field(min_length=1, max_length=180)
    source_url: str = Field(default="", max_length=1_000)
    fetched_at: str = Field(default="", max_length=80)
    project_count: int = Field(default=0, ge=0, le=100_000)
    snapshot: dict[str, Any] = Field(default_factory=dict)


class CIDOpportunityRequest(BaseModel):
    repo: str = Field(min_length=1, max_length=180)
    project_key: str = Field(min_length=1, max_length=240)
    project: dict[str, Any] = Field(default_factory=dict)

class OpportunityReviewRequest(BaseModel):
    verdict: str = Field(default="", max_length=40)
    rationale: str = Field(default="", max_length=2_000)
    confirmed: bool = False


class CIDPreferenceRequest(BaseModel):
    preferences: dict[str, Any] = Field(default_factory=dict)

def dedupe_aihot_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated stories from multiple sources while keeping the best record."""
    result: list[dict[str, Any]] = []
    key_to_index: dict[str, int] = {}
    for item in items:
        keys = []
        link_key = _app_call('canonical_aihot_link', item.get("link", ""))
        title_key = _app_call('normalized_aihot_title', item.get("title", ""))
        if link_key:
            keys.append(f"link:{link_key}")
        if title_key:
            keys.append(f"title:{title_key}")
        existing_index = next((key_to_index[key] for key in keys if key in key_to_index), None)
        if existing_index is None:
            existing_index = len(result)
            result.append(item)
        else:
            current = result[existing_index]
            current_score = (
                int(current.get("importance") or 0),
                bool(current.get("business_opportunity")),
                len(str(current.get("description") or "")),
                str(current.get("published_at") or ""),
            )
            item_score = (
                int(item.get("importance") or 0),
                bool(item.get("business_opportunity")),
                len(str(item.get("description") or "")),
                str(item.get("published_at") or ""),
            )
            if item_score > current_score:
                result[existing_index] = item
        for key in keys:
            key_to_index[key] = existing_index
    return result


def list_aihot_feedback(item_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Read local-only feedback used to personalize the signal list."""
    connection = _app_call('db_connection', )
    try:
        if item_ids:
            normalized = [str(item_id) for item_id in item_ids if str(item_id).strip()]
            if not normalized:
                return {}
            placeholders = ",".join("?" for _ in normalized)
            rows = connection.execute(
                f"SELECT * FROM aihot_feedback WHERE item_id IN ({placeholders})", normalized
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM aihot_feedback").fetchall()
        return {
            str(row["item_id"]): {
                "item_id": str(row["item_id"]),
                "vote": row["vote"],
                "note": row["note"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }
    finally:
        connection.close()


def save_aihot_feedback(item_id: str, vote: str, note: str = "") -> dict[str, Any]:
    timestamp = now_iso()
    connection: sqlite3.Connection | None = None
    try:
        connection = _app_call('db_connection', )
        connection.execute(
            """INSERT INTO aihot_feedback (item_id, vote, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET vote = excluded.vote, note = excluded.note, updated_at = excluded.updated_at""",
            (str(item_id), vote, clip(note.strip(), 1_000), timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM aihot_feedback WHERE item_id = ?", (str(item_id),)).fetchone()
        return {
            "item_id": str(row["item_id"]),
            "vote": row["vote"],
            "note": row["note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    finally:
        connection.close()


def cid_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    snapshot = decode_json_column(row["snapshot_json"])
    return {
        "id": row["id"],
        "repo": row["repo"],
        "source_url": row["source_url"],
        "fetched_at": row["fetched_at"],
        "project_count": row["project_count"],
        "snapshot": snapshot,
        "created_at": row["created_at"],
    }


def refresh_cid_opportunity_status(repo: str, projects: list[dict[str, Any]], fetched_at: str) -> int:
    """登记过的机会卡项目在新快照里状态/描述有变化时，刷新卡片并通知。

    只更新已登记机会（opportunity work_item + artifact），不自动创建新机会。
    返回更新的机会卡数量；没有变化或没有登记时返回 0。
    """
    if not projects:
        return 0
    by_key = {str(item.get("key") or ""): item for item in projects if item.get("key")}
    if not by_key:
        return 0
    updated = 0
    for item in _app_call('list_work_items', "all", "cid-dashboard"):
        meta = item.get("metadata") or {}
        opportunity_key = str(meta.get("opportunity_key") or "")
        if not opportunity_key or not opportunity_key.startswith("cid:"):
            continue
        project_key = str(meta.get("project_key") or "")
        current = by_key.get(project_key)
        if not current:
            continue
        status = str(current.get("status") or "")
        name = str(current.get("name") or "")
        desc = clip(str(current.get("desc") or ""), 1_000)
        signal = meta.get("signal") if isinstance(meta.get("signal"), dict) else {}
        old_status = str(signal.get("status") or meta.get("project_status") or "")
        old_name = str(signal.get("name") or meta.get("project_name") or "")
        if status == old_status and name == old_name:
            continue
        new_signal = {**signal, "status": status, "name": name, "status_changed_from": old_status or "未知", "last_refreshed_at": now_iso()}
        new_meta = {**meta, "signal": new_signal, "project_status": status, "project_name": name, "last_refreshed_at": now_iso(), "status_changed_from": old_status or "未知"}
        _app_call('update_work_item_record', 
            item["id"],
            {
                "description": clip(f"看板项目 {name} 状态更新为 {status or '未知'}（{fetched_at}）。原描述：{desc or '无'}", 1_500),
                "metadata": new_meta,
            },
        )
        try:
            create_notification_record(
                title=f"看板机会更新：{name}",
                body=f"登记过的看板项目状态变化：{old_status or '未知'} → {status or '未知'}（数据时间 {fetched_at}）。",
                project_id="cid-dashboard",
                kind="agent_action",
                level="info",
                href="/projects/cid-dashboard",
                event_key=f"cid-opportunity-update:{item['id']}:{fetched_at}",
                dedupe_seconds=0,
            )
        except Exception:  # noqa: BLE001
            log.debug("忽略异常（refresh_cid_opportunity_status）", exc_info=True)
        updated += 1
    return updated


def save_cid_dashboard_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a bounded, non-sensitive snapshot from the CID dashboard."""
    repo = clip(str(payload.get("repo") or ""), 180).strip()
    if not repo or "/" not in repo:
        raise ValueError("看板数据源必须是 owner/repo")
    source_url = clip(str(payload.get("source_url") or ""), 1_000).strip()
    fetched_at = clip(str(payload.get("fetched_at") or now_iso()), 80).strip()
    raw = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    def safe_count(value: Any) -> int:
        try:
            return max(0, min(int(value or 0), 100_000))
        except (TypeError, ValueError):
            return 0

    raw_status_counts = raw.get("status_counts") if isinstance(raw.get("status_counts"), dict) else {}
    raw_top_tags = raw.get("top_tags") if isinstance(raw.get("top_tags"), list) else []
    snapshot = {
        "status_counts": {clip(str(key), 80): safe_count(value) for key, value in list(raw_status_counts.items())[:20]},
        "top_tags": [[clip(str(pair[0]), 80), safe_count(pair[1])] for pair in raw_top_tags[:20] if isinstance(pair, (list, tuple)) and len(pair) >= 2],
        "projects": [],
    }
    projects = raw.get("projects") if isinstance(raw.get("projects"), list) else []
    safe_projects = []
    for project in projects[:160]:
        if not isinstance(project, dict):
            continue
        safe_projects.append(
            {
                "key": clip(str(project.get("key") or ""), 240),
                "name": clip(str(project.get("name") or ""), 240),
                "url": clip(str(project.get("url") or ""), 1_000),
                "desc": clip(str(project.get("desc") or ""), 1_000),
                "dev": clip(str(project.get("dev") or ""), 160),
                "status": clip(str(project.get("status") or ""), 40),
                "tags": [clip(str(tag), 80) for tag in (project.get("tags") or [])[:8]],
                "group": clip(str(project.get("group") or project.get("groupLabel") or ""), 160),
            }
        )
    snapshot["projects"] = safe_projects
    snapshot["project_count"] = max(0, min(int(payload.get("project_count") or len(safe_projects)), 100_000))
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    connection = _app_call('db_connection', )
    try:
        previous = connection.execute(
            "SELECT * FROM cid_dashboard_snapshots WHERE repo = ? ORDER BY id DESC LIMIT 1", (repo,)
        ).fetchone()
        if previous:
            previous_snapshot = decode_json_column(previous["snapshot_json"])
            previous_snapshot.pop("saved_at", None)
            if previous["source_url"] == source_url and previous_snapshot == snapshot:
                return _app_call('cid_snapshot_row', previous)
        cursor = connection.execute(
            """INSERT INTO cid_dashboard_snapshots
            (repo, source_url, fetched_at, project_count, snapshot_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (repo, source_url, fetched_at, snapshot["project_count"], snapshot_json, now_iso()),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM cid_dashboard_snapshots WHERE id = ?", (cursor.lastrowid,)).fetchone()
        saved = _app_call('cid_snapshot_row', row)
    finally:
        connection.close()

    # 机会卡状态自动刷新：已登记的机会项目在新快照里状态/描述有变化时，
    # 更新机会卡描述并登记一条通知，让登记过的机会保持最新。
    try:
        _app_call('refresh_cid_opportunity_status', repo, safe_projects, fetched_at)
    except Exception:  # noqa: BLE001
        log.debug("忽略异常（save_cid_dashboard_snapshot）", exc_info=True)
    return saved


def list_cid_dashboard_snapshots(repo: str = "", limit: int = 20) -> list[dict[str, Any]]:
    connection = _app_call('db_connection', )
    try:
        if repo:
            rows = connection.execute(
                "SELECT * FROM cid_dashboard_snapshots WHERE repo = ? ORDER BY created_at DESC LIMIT ?",
                (repo, max(1, min(limit, 50))),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM cid_dashboard_snapshots ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 50)),),
            ).fetchall()
        return [_app_call('cid_snapshot_row', row) for row in rows]
    finally:
        connection.close()


def cid_opportunity_for_project(opportunity_key: str) -> dict[str, Any] | None:
    for work_item in _app_call('list_work_items', "all", "cid-dashboard"):
        metadata = work_item.get("metadata") if isinstance(work_item.get("metadata"), dict) else {}
        if metadata.get("opportunity_key") == opportunity_key:
            return work_item
    return None


def aihot_opportunity_for_item(item_id: str) -> dict[str, Any] | None:
    key = f"aihot:{str(item_id)}"
    for work_item in _app_call('list_work_items', "all", "aihot"):
        metadata = work_item.get("metadata") if isinstance(work_item.get("metadata"), dict) else {}
        if metadata.get("opportunity_key") == key:
            return work_item
    return None


def enrich_aihot_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    feedback = _app_call('list_aihot_feedback', [str(item.get("id")) for item in items])
    opportunities = {
        str(item.get("metadata", {}).get("opportunity_key")): item
        for item in _app_call('list_work_items', "all", "aihot")
        if isinstance(item.get("metadata"), dict) and item.get("metadata", {}).get("opportunity_key")
    }
    enriched = []
    for raw_item in items:
        item = dict(raw_item)
        item_feedback = feedback.get(str(item.get("id")))
        opportunity = opportunities.get(f"aihot:{item.get('id')}")
        item["feedback"] = item_feedback or {"item_id": str(item.get("id")), "vote": "", "note": ""}
        item["opportunity_work_item_id"] = opportunity.get("id") if opportunity else None
        item["opportunity_status"] = opportunity.get("status") if opportunity else ""
        item["opportunity_score"] = _app_call('opportunity_score', item, item["feedback"])
        enriched.append(item)
    return enriched


async def fetch_aihot_snapshot(*, force: bool = False) -> dict[str, Any]:
    current = _app_call('load_aihot_snapshot', )
    fetched_at = current.get("fetched_at")
    fresh = False
    if fetched_at and not force:
        try:
            fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)).total_seconds() < 1800
        except ValueError:
            fresh = False
    if fresh and current.get("items"):
        return current
    async def fetch_one(client: httpx.AsyncClient, url: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            response = await client.get(url, headers={"User-Agent": "Workbench/0.2"})
            response.raise_for_status()
            body = response.text
        except Exception as exc:
            return (url, [{"_error": str(exc)}])
        if "aihot.today" in url:
            batch = _app_call('parse_aihot_items', body)
            for entry in batch:
                entry["domain"] = "ai"
            return (url, batch)
        host = _app_call('_hostname', url)
        batch = _app_call('parse_rss_items', body, host)
        domain = _app_call('_aihot_domain', host)
        for entry in batch:
            entry["domain"] = domain
        return (url, batch)
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False, headers={"User-Agent": "Workbench/0.2"}) as client:
            results = await asyncio.gather(*(fetch_one(client, url) for url in AIHOT_SOURCES), return_exceptions=False)
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for _url, batch in results:
            for entry in batch:
                if "_error" in entry:
                    errors.append(str(entry["_error"]))
                elif _app_call('_aihot_relevant', entry):
                    items.append(entry)
        if not items:
            raise RuntimeError(f"AI 热点多源均未读到数据 ({'; '.join(errors)[:200]})")
        snapshot = {
            "source": " + ".join(_app_call('_hostname', url) for url in AIHOT_SOURCES),
            "source_url": AIHOT_SOURCES[0],
            "sources": list(AIHOT_SOURCES),
            "fetched_at": now_iso(),
            "items": items,
            "previous_items": [
                {"id": item.get("id"), "title": item.get("title"), "link": item.get("link"), "published_at": item.get("published_at")}
                for item in _app_call('dedupe_aihot_items', list(current.get("items") or []))[:200]
                if isinstance(item, dict)
            ],
            "stale": False,
            "error": " | ".join(errors)[:500] if errors else "",
        }
        _app_call('save_aihot_snapshot', snapshot)
        _app_call('register_artifact_safely', 
            project_id="aihot",
            name="aihot_snapshot.json",
            path=str(AIHOT_SNAPSHOT_FILE),
            kind="aihot_feed",
            metadata={"item_count": len(items), "source_count": len(AIHOT_SOURCES)},
        )
        return snapshot
    except Exception as exc:
        if current.get("items"):
            current["stale"] = True
            current["error"] = str(exc)
            return current
        raise HTTPException(502, f"AI 热点同步失败：{exc}") from exc


def _hostname(url: str) -> str:
    from urllib.parse import urlparse as _urlparse
    try:
        return _urlparse(url).netloc or url
    except ValueError:
        return url


def select_aihot_items(snapshot: dict[str, Any], mode: str = "useful", query: str = "", limit: int = 40, domain: str = "") -> list[dict[str, Any]]:
    items = _app_call('dedupe_aihot_items', list(snapshot.get("items") or []))
    query = query.strip().lower()
    if query:
        items = [item for item in items if query in json.dumps(item, ensure_ascii=False).lower()]
    if domain and domain != "all":
        items = [item for item in items if (item.get("domain") or "综合") == domain]
    feedback = _app_call('list_aihot_feedback', [str(item.get("id")) for item in items])
    # 变化检测：对比上一次快照（previous_items），标记新出现的热点
    previous = snapshot.get("previous_items") or []
    previous_ids = {str(item.get("id")) for item in previous if isinstance(item, dict)}
    previous_titles = {_app_call('normalized_aihot_title', str(item.get("title") or "")) for item in previous if isinstance(item, dict)}

    def change_label(item: dict[str, Any]) -> str:
        if str(item.get("id") or "") in previous_ids:
            return ""
        return "" if _app_call('normalized_aihot_title', str(item.get("title") or "")) in previous_titles else "new"

    # 来源质量评分：基于该来源下所有条目的用户反馈。
    # 细化：同时统计有用/不相关样本量，按“有用率 + 样本量”加权，
    # 有足够样本的来源分数更可信，样本少时向基准 3 分收缩。
    source_votes: dict[str, int] = {}
    source_used: dict[str, int] = {}
    source_total: dict[str, int] = {}
    for item in items:
        src = str(item.get("source") or "未知来源")
        vote = str(feedback.get(str(item.get("id")), {}).get("vote", ""))
        if vote == "useful":
            source_votes[src] = source_votes.get(src, 0) + 1
            source_used[src] = source_used.get(src, 0) + 1
            source_total[src] = source_total.get(src, 0) + 1
        elif vote == "not_useful":
            source_votes[src] = source_votes.get(src, 0) - 1
            source_total[src] = source_total.get(src, 0) + 1
    for index, raw_item in enumerate(items):
        updated = dict(raw_item)
        updated["change"] = change_label(updated)
        src = str(updated.get("source") or "未知来源")
        votes = source_votes.get(src, 0)
        total = source_total.get(src, 0)
        # 有用率加权：样本越多，越向“有用率”靠；样本少时回到基准 3 分。
        if total >= 5:
            usefulness = source_used.get(src, 0) / total
            score = 1 + usefulness * 4
        elif total >= 2:
            usefulness = source_used.get(src, 0) / total
            score = 3 + (usefulness - 0.5) * 2
        else:
            score = 3 + votes
        updated["source_score"] = max(0, min(5, round(score, 2)))
        updated["source_votes"] = votes
        updated["source_sample_size"] = total
        items[index] = updated

    def preference_score(item: dict[str, Any]) -> int:
        vote = str(feedback.get(str(item.get("id")), {}).get("vote", ""))
        return 5 if vote == "useful" else -8 if vote == "not_useful" else 0

    def ranked_key(item: dict[str, Any]) -> tuple[int, int, str]:
        return (int(item.get("importance") or 0) + preference_score(item) + item.get("source_score", 3) - 3, 1 if item.get("change") == "new" else 0, str(item.get("published_at") or ""))

    if mode == "useful":
        items.sort(key=ranked_key, reverse=True)
    elif mode == "opportunity":
        opportunity_items = [item for item in items if item.get("business_opportunity") or any(tag in {"Product", "Company", "Industry", "Agent"} for tag in item.get("tags", []))]
        items = opportunity_items or items
        items.sort(key=ranked_key, reverse=True)
    else:
        items.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    return _app_call('enrich_aihot_items', items[: max(1, min(limit, 100))])


def github_repository_parts(values: dict[str, str]) -> tuple[str, str]:
    """Validate the repository path before putting user input into a URL."""
    owner = str(values.get("owner") or "").strip()
    repo = str(values.get("repo") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", owner) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repo):
        raise HTTPException(400, "GitHub：用户名/组织和仓库名只能包含字母、数字、点、下划线或短横线")
    return owner, repo


def _plain_external_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return clip(text, limit)


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

async def run_aihot_agent_turn(
    *,
    run: dict[str, Any],
    session: dict[str, Any],
    message: str,
    chosen: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run the AI-hotspot Agent with selected public evidence and persistence."""
    update_agent_run_record(run["id"], status="running", error="")
    add_agent_run_event(run["id"], "started", "AI 热点 Agent 开始整理所选资讯。")
    evidence = "\n\n".join(
        f"[{index}] {item.get('title')}\n来源：{item.get('source')}｜时间：{item.get('published_at')}｜重要度：{item.get('importance')}\n摘要：{item.get('description')}\n原文：{item.get('link')}"
        for index, item in enumerate(chosen, start=1)
    )
    system = (
        "你是工作台中的 AI 热点研究 Agent。只基于下方 aiHot.today 公开资讯回答，不能把新闻标题当成已验证事实。"
        "用户可能想知道哪些消息值得看、对个人效率或创业有什么启发、是否值得继续研究。"
        "请明确区分：已知信息、你的判断、需要验证的地方。给出最多 3 个可执行的下一步。"
        "如果用户要求发现商机，请优先从真实需求、目标用户、付费可能、竞争和 7 天验证切入。使用简体中文。"
    )
    try:
        history = list_agent_messages(session["id"], limit=MAX_CONVERSATION_MESSAGES * 2)
        add_agent_run_event(run["id"], "llm_started", "正在调用全局 LLM 分析热点证据。", metadata={"items": len(chosen)})
        answer = await call_llm(
            [
                {"role": "system", "content": system},
                *({"role": item["role"], "content": item["content"]} for item in history[-MAX_CONVERSATION_MESSAGES:]),
                {"role": "user", "content": f"资讯证据：\n{clip_for_llm(evidence, 18_000)}\n\n用户问题：\n{message}"},
            ],
            max_tokens=4000,
            temperature=0.25,
        )
        add_agent_run_event(run["id"], "llm_succeeded", "热点分析已返回。", level="success")
        evidence_items = [{"id": item.get("id", ""), "type": "aihot_item", "title": item.get("title", ""), "source": item.get("source", ""), "published_at": item.get("published_at", ""), "link": item.get("link", "")} for item in chosen]
        result_contract = agent_result_contract("aihot", answer, evidence=evidence_items, source_refs=evidence_items, run_id=run["id"], session_id=session["id"])
        assistant_message = add_agent_message(session["id"], "assistant", answer, {"run_id": run["id"], "item_ids": [item.get("id") for item in chosen], "result_contract": result_contract})
        session = update_agent_session_summary(
            session["id"],
            {"last_answer": clip(answer, 1200), "last_result_contract": result_contract, "last_run_id": run["id"], "selected_items": [item.get("id") for item in chosen]},
        ) or session
        result = {"answer": answer, "items": chosen, "session_id": session["id"], "message_id": assistant_message.get("id"), "result_contract": result_contract}
        updated_run = update_agent_run_record(run["id"], status="succeeded", result=result, error="") or run
        add_agent_run_event(run["id"], "succeeded", "AI 热点 Agent 本轮完成。", level="success")
        return {
            "run": updated_run,
            "session": session,
            "message": assistant_message,
            "messages": list_agent_messages(session["id"], limit=40),
            "answer": answer,
            "result_contract": result_contract,
            "items": chosen,
            "agent": _app_call('agent_detail', "aihot", llm_ready=True),
        }
    except httpx.HTTPStatusError as exc:
        error = f"上游返回 {exc.response.status_code}：{clip(exc.response.text, 500)}"
        update_agent_run_record(run["id"], status="failed", error=error)
        add_agent_run_event(run["id"], "failed", f"AI 热点 Agent 调用失败：{error}", level="error")
        raise HTTPException(502, f"AI 热点 Agent 调用失败：{error}") from exc
    except Exception as exc:
        error = str(exc)
        update_agent_run_record(run["id"], status="failed", error=error)
        add_agent_run_event(run["id"], "failed", f"AI 热点 Agent 执行失败：{error}", level="error")
        raise HTTPException(502, f"AI 热点 Agent 调用失败：{error}") from exc

async def run_cid_agent_turn(*, run: dict[str, Any], messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    """Persist the CID dashboard's OpenAI-compatible proxy turn."""
    update_agent_run_record(run["id"], status="running", error="")
    add_agent_run_event(run["id"], "started", "独立开发者看板 Agent 开始分析。", metadata={"messages": len(messages)})
    try:
        add_agent_run_event(run["id"], "llm_started", "正在调用工作台全局 LLM。")
        answer = await call_llm(messages, max_tokens=4000, temperature=0.3)
        result_contract = agent_result_contract("cid-dashboard", answer, run_id=run["id"])
        result = {"answer": answer, "message_count": len(messages), "result_contract": result_contract}
        updated = update_agent_run_record(run["id"], status="succeeded", result=result, error="") or run
        add_agent_run_event(run["id"], "succeeded", "独立开发者看板 Agent 分析完成。", level="success")
        return answer, updated
    except httpx.HTTPStatusError as exc:
        error = f"上游返回 {exc.response.status_code}：{clip(exc.response.text, 500)}"
        update_agent_run_record(run["id"], status="failed", error=error)
        add_agent_run_event(run["id"], "failed", f"看板 Agent 调用失败：{error}", level="error")
        raise HTTPException(502, f"全局 LLM 返回 {error}") from exc
    except Exception as exc:
        error = str(exc)
        update_agent_run_record(run["id"], status="failed", error=error)
        add_agent_run_event(run["id"], "failed", f"看板 Agent 执行失败：{error}", level="error")
        raise HTTPException(502, f"Agent 调用失败：{error}") from exc

@app.get("/api/aihot/feed")
async def get_aihot_feed(mode: str = "useful", q: str = "", limit: int = 40, refresh: bool = False, domain: str = "") -> dict[str, Any]:
    if mode not in {"latest", "useful", "opportunity"}:
        raise HTTPException(400, "不支持的 AI 热点筛选模式")
    snapshot = await _app_call('fetch_aihot_snapshot', force=refresh)
    return {
        "feed": {
            **snapshot,
            "items": _app_call('select_aihot_items', snapshot, mode, q, limit, domain),
            "mode": mode,
            "query": q,
            "domain": domain,
        }
    }


@app.post("/api/aihot/digest")
async def generate_aihot_digest() -> dict[str, Any]:
    if not llm_settings()["configured"]:
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM")
    snapshot = await _app_call('fetch_aihot_snapshot', force=False)
    items = await asyncio.to_thread(_app_call, 'select_aihot_items', snapshot, "useful", "", 30)
    if not items:
        raise HTTPException(400, "还没有可用的热点资讯，先同步一次")
    item_lines = "\n".join(
        f"- {item.get('title', '未命名')}（{item.get('source', '未知来源')}）{('：' + str(item.get('summary') or '')[:120]) if item.get('summary') else ''}"
        for item in items[:20]
    )
    prompt = (
        "你是 AI 资讯研究助手。基于以下资讯列表生成一份简洁摘要，并把内容相近的消息归为一类主题。\n\n"
        f"资讯：\n{item_lines}\n\n"
        "输出格式：\n1. 一句话总览\n2. 主题聚类（每类列出主题名和其中的消息标题）\n3. 值得关注的 3 个信号（为什么值得关注）\n"
        "只基于给出的资讯，不编造外部信息。"
    )
    try:
        answer = await call_llm(
            [{"role": "system", "content": "你是本地 AI 资讯研究助手，输出简洁可读的中文摘要。"}, {"role": "user", "content": prompt}],
            max_tokens=3000,
            temperature=0.3,
        )
    except httpx.HTTPStatusError as exc:
        detail = clip(exc.response.text, 500)
        raise HTTPException(502, f"生成失败：上游返回 {exc.response.status_code}：{detail}") from exc
    except Exception as exc:
        raise HTTPException(502, f"生成失败：{exc}") from exc

    output_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-AI热点摘要.md"
    output_path = OUTPUTS_DIR / output_name
    output_path.write_text(f"# AI 热点摘要\n\n> 生成时间：{now_iso()} · 基于 {len(items)} 条有用资讯\n\n{answer.rstrip()}\n", encoding="utf-8")
    artifact = await asyncio.to_thread(_app_call, 'register_artifact_safely', 
        project_id="aihot",
        name=output_name,
        path=str(output_path),
        kind="aihot_digest",
        metadata={"item_count": len(items), "source_items": [item.get("id", "") for item in items[:20]]},
    )
    # 摘要生成后推送远程 Web Push（有订阅才发，不影响摘要本身）
    try:
        await _app_call('_push_to_all_subscriptions', 
            title="AI 热点摘要已生成",
            body=f"基于 {len(items)} 条有用资讯 · {clip(answer, 120)}",
            href="/projects/aihot",
            event_key=f"aihot-digest:{now_iso()}",
        )
    except Exception:
        log.debug("忽略异常（generate_aihot_digest）", exc_info=True)
    return {"ok": True, "answer": answer, "filename": output_name, "path": str(output_path), "artifact": artifact, "item_count": len(items)}


@app.get("/api/cid-dashboard/snapshot")
def get_cid_dashboard_snapshot(repo: str = "", limit: int = 12) -> dict[str, Any]:
    history = _app_call('list_cid_dashboard_snapshots', repo.strip(), limit=limit)
    return {"snapshot": history[0] if history else None, "history": history, "agent": _app_call('agent_detail', "cid-dashboard", llm_ready=bool(llm_settings()["configured"]))}


@app.post("/api/cid-dashboard/snapshot")
def save_cid_dashboard_snapshot_route(request: CIDSnapshotRequest) -> dict[str, Any]:
    try:
        snapshot = _app_call('save_cid_dashboard_snapshot', request.model_dump() if hasattr(request, "model_dump") else request.dict())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "snapshot": snapshot}


@app.get("/api/cid-dashboard/opportunities")
def get_cid_dashboard_opportunities(repo: str = "", project_key: str = "") -> dict[str, Any]:
    key_prefix = f"cid:{repo.strip()}:" if repo.strip() else "cid:"
    items = []
    for work_item in _app_call('list_work_items', "all", "cid-dashboard"):
        metadata = work_item.get("metadata") if isinstance(work_item.get("metadata"), dict) else {}
        opportunity_key = str(metadata.get("opportunity_key") or "")
        if not opportunity_key.startswith(key_prefix):
            continue
        if project_key and opportunity_key != f"cid:{repo.strip()}:{project_key.strip()}":
            continue
        items.append(work_item)
    # 机会排序按个人偏好加权：偏好命中排前，其余按创建时间倒序
    preferences = _app_call('load_cid_preferences', )
    preferred = {str(value).lower() for value in preferences.get("preferred_tags", "").split(",") if str(value).strip()}
    avoid = {str(value).lower() for value in preferences.get("avoid_tags", "").split(",") if str(value).strip()}
    if preferred or avoid:
        def _score(item: dict[str, Any]) -> float:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            signal = metadata.get("signal") if isinstance(metadata.get("signal"), dict) else {}
            tags = {str(tag).lower() for tag in signal.get("tags", [])}
            return 2.0 * len(tags & preferred) - 1.0 * len(tags & avoid)
        items.sort(key=lambda item: (_score(item), str(item.get("created_at") or "")), reverse=True)
    else:
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"items": items[:50], "count": len(items), "sorted_by_preference": bool(preferred or avoid)}


class CIDPreferenceLearnRequest(BaseModel):
    opportunity_id: int | None = None
    repo: str = ""
    project_key: str = ""
    action: str = Field(pattern="^(like|dislike)$")


@app.post("/api/cid-dashboard/preferences/learn")
def learn_cid_preference(payload: CIDPreferenceLearnRequest) -> dict[str, Any]:
    """从机会点赞/点踩行为自动学习偏好：把机会赛道标签并入 preferred/avoid 列表。"""
    request = payload
    tags: list[str] = []
    source_label = "机会"
    if request.opportunity_id:
        item = _app_call('get_work_item_record', request.opportunity_id)
        if not item or item.get("source_project") != "cid-dashboard":
            raise HTTPException(404, "看板机会不存在")
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        signal = metadata.get("signal") if isinstance(metadata.get("signal"), dict) else {}
        tags = [str(tag).strip() for tag in signal.get("tags", []) if str(tag).strip()]
    elif request.repo and request.project_key:
        # 从最近快照的项目列表取赛道标签（未登记机会也能学习）
        snapshots = _app_call('list_cid_dashboard_snapshots', repo=request.repo, limit=1)
        if not snapshots:
            raise HTTPException(404, "没有该数据源的看板快照")
        snapshot_payload = snapshots[0].get("snapshot") or {}
        for project in snapshot_payload.get("projects") or []:
            if str(project.get("key") or "") == request.project_key:
                tags = [str(tag).strip() for tag in project.get("tags", []) if str(tag).strip()]
                source_label = "项目"
                break
        if not tags:
            raise HTTPException(404, "快照中找不到该项目")
    else:
        raise HTTPException(400, "需要 opportunity_id 或 repo+project_key")
    preferences = _app_call('load_cid_preferences', )
    preferred = [str(value).strip() for value in preferences.get("preferred_tags", "").split(",") if str(value).strip()]
    avoid = [str(value).strip() for value in preferences.get("avoid_tags", "").split(",") if str(value).strip()]
    if request.action == "like":
        for tag in tags:
            if tag not in preferred:
                preferred.append(tag)
        avoid = [tag for tag in avoid if tag not in tags]
    else:
        for tag in tags:
            if tag not in avoid:
                avoid.append(tag)
        preferred = [tag for tag in preferred if tag not in tags]
    preferences["preferred_tags"] = ",".join(preferred[:40])
    preferences["avoid_tags"] = ",".join(avoid[:40])
    save_json_atomic(DATA_DIR / "cid_preferences.json", preferences, 0o600)
    sync_cid_preferences_to_memories(preferences)
    return {"ok": True, "action": request.action, "learned_tags": tags, "source": source_label, "preferences": preferences}


@app.post("/api/cid-dashboard/opportunities")
def create_cid_dashboard_opportunity(request: CIDOpportunityRequest) -> dict[str, Any]:
    repo = request.repo.strip()
    project_key = request.project_key.strip()
    project = request.project if isinstance(request.project, dict) else {}
    title = clip(str(project.get("name") or "未命名看板项目"), 180)
    opportunity_key = f"cid:{repo}:{project_key}"
    existing = _app_call('cid_opportunity_for_project', opportunity_key)
    if existing:
        return {"ok": True, "created": False, "item": existing, "message": "这个看板项目已经登记过机会。"}

    safe_project = {
        "key": clip(project_key, 240),
        "name": title,
        "url": clip(str(project.get("url") or ""), 1_000),
        "desc": clip(str(project.get("desc") or "暂无项目描述"), 1_000),
        "dev": clip(str(project.get("dev") or "未知开发者"), 160),
        "status": clip(str(project.get("status") or ""), 40),
        "tags": [clip(str(tag), 80) for tag in (project.get("tags") or [])[:8]],
        "group": clip(str(project.get("group") or project.get("groupLabel") or ""), 160),
    }
    snapshots = _app_call('list_cid_dashboard_snapshots', repo, limit=1)
    snapshot = snapshots[0] if snapshots else None
    artifact = _app_call('register_artifact_safely', 
        project_id="cid-dashboard",
        name=f"看板项目机会 · {title}",
        path=safe_project["url"],
        kind="cid_project_opportunity",
        metadata={
            "opportunity_key": opportunity_key,
            "repo": repo,
            "project_key": project_key,
            "snapshot_id": snapshot.get("id") if snapshot else None,
            "project": safe_project,
        },
    )
    description = (
        "请判断这个独立开发者看板项目是否值得转化为适合我的个人产品机会。\n"
        f"项目：{safe_project['name']}\n"
        f"介绍：{safe_project['desc']}\n"
        f"开发者：{safe_project['dev']}\n"
        f"赛道：{'、'.join(safe_project['tags']) or '未分类'}\n"
        f"项目链接：{safe_project['url'] or '无'}\n"
        f"看板来源：{repo}；数据时间：{snapshot.get('fetched_at') if snapshot else '未知'}\n\n"
        "请输出目标用户、核心痛点、现有替代、可复制性、关键假设、证据缺口、7 天验证动作、成功指标和停止条件。"
    )
    work_item = _app_call('create_work_item_record', 
        title=f"验证看板机会：{title}",
        description=description,
        kind="opportunity",
        source_project="cid-dashboard",
        target_project="idea-analysis",
        metadata={
            "opportunity_key": opportunity_key,
            "snapshot_id": snapshot.get("id") if snapshot else None,
            "signal": {"repo": repo, "project_key": project_key, "snapshot_fetched_at": snapshot.get("fetched_at") if snapshot else "", "source_url": snapshot.get("source_url") if snapshot else "", **safe_project},
            "artifact_id": artifact.get("id") if artifact else None,
        },
    )
    relation = None
    if artifact:
        relation = _app_call('create_relation_record', 
            from_type="artifact",
            from_id=str(artifact["id"]),
            to_type="work_item",
            to_id=str(work_item["id"]),
            relation_type="project_to_opportunity",
            metadata={"source_project": "cid-dashboard", "target_project": "idea-analysis", "opportunity_key": opportunity_key},
        )
    snapshot_relation = None
    if snapshot:
        snapshot_relation = _app_call('create_relation_record', 
            from_type="cid_snapshot",
            from_id=str(snapshot["id"]),
            to_type="work_item",
            to_id=str(work_item["id"]),
            relation_type="snapshot_to_opportunity",
            metadata={"repo": repo, "project_key": project_key},
        )
    notification = create_notification_record(
        title="看板项目已登记为待验证机会",
        body=f"{title} · 已交给想法分析 Agent",
        project_id="idea-analysis",
        kind="opportunity",
        level="info",
        href="/projects/idea-analysis",
        event_key=f"cid-opportunity:{opportunity_key}",
        dedupe_seconds=0,
    )
    return {"ok": True, "created": True, "item": work_item, "artifact": artifact, "relation": relation, "snapshot_relation": snapshot_relation, "notification": notification}


@app.post("/api/aihot/feedback")
def save_aihot_feedback_route(request: AIHotFeedbackRequest) -> dict[str, Any]:
    if request.vote not in {"useful", "not_useful"}:
        raise HTTPException(400, "热点反馈只能是 useful 或 not_useful")
    snapshot = _app_call('load_aihot_snapshot', )
    item = next((candidate for candidate in snapshot.get("items", []) if str(candidate.get("id")) == request.item_id), None)
    if not item:
        raise HTTPException(404, "这条热点不在当前本地快照中")
    feedback = _app_call('save_aihot_feedback', request.item_id, request.vote, request.note)
    return {"ok": True, "feedback": feedback, "item": _app_call('enrich_aihot_items', [item])[0]}


@app.post("/api/aihot/opportunities")
def create_aihot_opportunity_route(request: AIHotOpportunityRequest) -> dict[str, Any]:
    snapshot = _app_call('load_aihot_snapshot', )
    item = next((candidate for candidate in snapshot.get("items", []) if str(candidate.get("id")) == request.item_id), None)
    if not item:
        raise HTTPException(404, "这条热点不在当前本地快照中")
    existing = _app_call('aihot_opportunity_for_item', request.item_id)
    if existing:
        return {"ok": True, "created": False, "item": existing, "message": "这条热点已经交给想法分析 Agent 了"}

    opportunity_key = f"aihot:{request.item_id}"
    artifact = next(
        (
            candidate
            for candidate in _app_call('list_artifacts', "aihot")
            if isinstance(candidate.get("metadata"), dict)
            and candidate.get("metadata", {}).get("opportunity_key") == opportunity_key
        ),
        None,
    )
    if not artifact:
        artifact = _app_call('register_artifact_safely', 
            project_id="aihot",
            name=f"AI 热点信号 · {request.item_id}",
            path=str(item.get("link") or ""),
            kind="aihot_signal",
            metadata={
                "opportunity_key": opportunity_key,
                "item_id": str(item.get("id")),
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "published_at": item.get("published_at", ""),
            },
        )
    description = (
        "请判断这条 AI 热点是否值得做成个人开发者机会。\n"
        f"热点标题：{item.get('title', '未命名热点')}\n"
        f"摘要：{item.get('description', '暂无摘要')}\n"
        f"来源：{item.get('source', '未知来源')}\n"
        f"发布时间：{item.get('published_at', '未知')}\n"
        f"原文：{item.get('link', '无链接')}\n\n"
        "请输出目标用户、真实痛点、现有替代、可验证假设、7 天验证动作、停止条件和证据缺口。"
    )
    work_item = _app_call('create_work_item_record', 
        title=f"验证热点机会：{clip(str(item.get('title') or '未命名热点'), 150)}",
        description=description,
        kind="opportunity",
        source_project="aihot",
        target_project="idea-analysis",
        metadata={
            "opportunity_key": opportunity_key,
            "signal": {
                "item_id": str(item.get("id")),
                "title": item.get("title", ""),
                "description": item.get("description", ""),
                "source": item.get("source", ""),
                "link": item.get("link", ""),
                "published_at": item.get("published_at", ""),
                "tags": item.get("tags", []),
                "business_opportunity": item.get("business_opportunity", ""),
            },
            "artifact_id": artifact.get("id") if artifact else None,
        },
    )
    relation = None
    if artifact:
        relation = _app_call('create_relation_record', 
            from_type="artifact",
            from_id=str(artifact["id"]),
            to_type="work_item",
            to_id=str(work_item["id"]),
            relation_type="signal_to_opportunity",
            metadata={"source_project": "aihot", "target_project": "idea-analysis", "item_id": request.item_id},
        )
    notification = create_notification_record(
        title="AI 热点已交给想法分析",
        body=f"{clip(str(item.get('title') or '未命名热点'), 180)} · 等待想法分析 Agent 领取",
        project_id="idea-analysis",
        kind="opportunity",
        level="info",
        href="/projects/idea-analysis",
        event_key=f"aihot-opportunity:{request.item_id}",
        dedupe_seconds=0,
    )
    return {"ok": True, "created": True, "item": work_item, "artifact": artifact, "relation": relation, "notification": notification}


@app.get("/api/aihot/opportunity-review")
def aihot_opportunity_review() -> dict[str, Any]:
    """AI 热点机会复盘：聚合所有商机线索 work_item 的状态与去向。"""
    items = _app_call('list_work_items', "all", "aihot")
    opportunities = [
        item for item in items
        if item.get("kind") == "opportunity" and str(item.get("source_project") or "") == "aihot"
    ]
    buckets: dict[str, list[dict[str, Any]]] = {
        "open": [], "running": [], "done": [], "archived": [], "blocked": [], "failed": [],
    }
    for item in opportunities:
        status = str(item.get("status") or "open")
        bucket = buckets.get(status)
        if bucket is None:
            bucket = buckets["open"]
        bucket.append({
            "id": item.get("id"),
            "title": str(item.get("title") or ""),
            "created_at": str(item.get("created_at") or ""),
            "target_project": str(item.get("target_project") or ""),
            "opportunity_key": str((item.get("metadata") or {}).get("opportunity_key") or ""),
        })
    active = buckets["open"] + buckets["running"] + buckets["blocked"] + buckets["failed"]
    processed = buckets["done"] + buckets["archived"]
    total = len(opportunities)
    return {
        "ok": True,
        "total": total,
        "active": len(active),
        "processed": len(processed),
        "processed_rate": round(processed / total, 3) if total else 0,
        "buckets": {key: value for key, value in buckets.items()},
        "latest": (opportunities or [None])[0].get("title") if opportunities else "",
        "review_stats": _app_call('aihot_review_stats', ),
    }


@app.post("/api/aihot/chat")
async def chat_aihot(request: AIHotChatRequest) -> dict[str, Any]:
    if not llm_settings()["configured"]:
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM")
    snapshot = await _app_call('fetch_aihot_snapshot', )
    all_items = snapshot.get("items") or []
    chosen = [item for item in all_items if str(item.get("id")) in {str(value) for value in request.item_ids}]
    if not chosen:
        chosen = await asyncio.to_thread(_app_call, 'select_aihot_items', snapshot, request.mode, limit=18)
    session = get_agent_session(request.session_id, "aihot") if request.session_id else None
    if request.session_id and not session:
        raise HTTPException(404, "AI 热点 Agent 会话不存在")
    if not session:
        session = await asyncio.to_thread(_app_call, 'create_agent_session', "aihot", request.message)
    await asyncio.to_thread(_app_call, 'add_agent_message', session["id"], "user", request.message, {"source": "aihot", "item_ids": [item.get("id") for item in chosen], "mode": request.mode})
    run = await asyncio.to_thread(_app_call, 'create_agent_run_record', 
        project_id="aihot",
        session_id=session["id"],
        kind="chat",
        title=clip(request.message, 120),
        request={"session_id": session["id"], "message": request.message, "mode": request.mode, "item_ids": request.item_ids, "selected_items": chosen},
        max_attempts=2,
    )
    if request.stream:
        async def event_gen():
            try:
                async for chunk in _app_call('stream_aihot_agent_turn', run=run, session=session, message=request.message, chosen=chosen):
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': clip(str(exc), 300), 'provider': ''}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    return await _app_call('run_aihot_agent_turn', run=run, session=session, message=request.message, chosen=chosen)

@app.get("/api/aihot/insights")
def get_aihot_insights() -> dict[str, Any]:
    return _app_call('aihot_insights', )


@app.post("/api/aihot/summary")
async def create_aihot_summary() -> dict[str, Any]:
    insights = await asyncio.to_thread(_app_call, 'aihot_insights')
    lines = [f"# AI 热点摘要 · {datetime.now().astimezone().strftime('%Y-%m-%d')}", "", insights.get("summary", ""), "", f"> 数据时间：{insights.get('fetched_at') or '未知'}", "", "## 主题簇", ""]
    for cluster in insights.get("clusters", []):
        lines.append(f"- **{cluster['label']}**：{cluster['count']} 条；" + "；".join(str(item.get("title") or "") for item in cluster.get("items", [])[:3]))
    lines.extend(["", "## 来源质量", ""])
    lines.extend(f"- {item['source']}：{item['quality_score']}（样本 {item['count']}，有用 {item['useful']}）" for item in insights.get("source_scores", [])[:12])
    path = OUTPUTS_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-AI热点摘要.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifact = await asyncio.to_thread(_app_call, 'register_artifact_safely', project_id="aihot", name=path.name, path=str(path), kind="aihot_summary", metadata={"fetched_at": insights.get("fetched_at"), "cluster_count": len(insights.get("clusters", []))})
    create_notification_record(title="AI 热点摘要已生成", body=insights.get("summary", ""), project_id="aihot", kind="summary", level="info", href="/projects/aihot", event_key=f"aihot-summary:{path.name}", dedupe_seconds=0)
    # 每日摘要自动化也推远程 Web Push
    try:
        await _app_call('_push_to_all_subscriptions', 
            title="AI 热点每日摘要",
            body=clip(insights.get("summary", "") or "今日热点摘要已生成", 140),
            href="/projects/aihot",
            event_key=f"aihot-summary:{path.name}",
        )
    except Exception:
        log.debug("忽略异常（create_aihot_summary）", exc_info=True)
    return {"ok": True, "artifact": artifact, "insights": insights, "path": str(path)}

@app.post("/api/cid-dashboard/compare")
async def compare_cid_projects(request: CIDCompareRequest) -> dict[str, Any]:
    snapshots = await asyncio.to_thread(_app_call, 'list_cid_dashboard_snapshots', request.repo.strip(), limit=1)
    snapshot = snapshots[0] if snapshots else None
    projects = (snapshot or {}).get("projects") or (snapshot or {}).get("items") or []
    chosen = [item for item in projects if str(item.get("key") or item.get("id") or item.get("slug") or "") in set(request.project_keys)]
    if len(chosen) < 2:
        raise HTTPException(404, "快照中没有找到至少两个可比较项目")
    fields = ["name", "desc", "dev", "status", "tags", "group", "url"]
    comparison = {field: [{"project": item.get("name") or item.get("key"), "value": item.get(field, "")} for item in chosen] for field in fields}
    snapshot_data = (snapshot or {}).get("snapshot") if isinstance((snapshot or {}).get("snapshot"), dict) else {}
    snapshot_text = json.dumps(snapshot_data, ensure_ascii=False, sort_keys=True)
    snapshot_quality = evidence_quality_descriptor(
        source=request.repo.strip(),
        data_as_of=(snapshot or {}).get("fetched_at", ""),
        content_hash=hashlib.sha256(snapshot_text.encode("utf-8", errors="ignore")).hexdigest() if snapshot else "",
        readable=bool(snapshot),
        read_error="没有找到该仓库的看板快照" if not snapshot else "",
        source_url=(snapshot or {}).get("source_url", ""),
        relation_count=0,
        source_quality={"project_count": (snapshot or {}).get("project_count", 0), "policy": "看板快照完整性/新鲜度，不等于项目质量"},
    )
    preferences = _app_call('load_cid_preferences', )
    preferred = {str(value).strip().lower() for value in str(preferences.get("preferred_tags", "")).split(",") if str(value).strip()}
    avoid = {str(value).strip().lower() for value in str(preferences.get("avoid_tags", "")).split(",") if str(value).strip()}
    preference_matches = []
    for project in chosen:
        tags = {str(tag).strip().lower() for tag in project.get("tags", [])}
        preference_matches.append({"project": project.get("name") or project.get("key"), "preferred_tags": sorted(tags & preferred), "avoid_tags": sorted(tags & avoid), "match_score": round((len(tags & preferred) - len(tags & avoid)) / max(1, len(preferred | avoid)), 3) if preferred or avoid else None})
    artifact = _app_call('register_artifact_safely', project_id="cid-dashboard", name=f"CID竞品比较-{datetime.now().strftime('%Y%m%d%H%M%S')}.json", path="", kind="cid_competitor_comparison", metadata={"repo": request.repo.strip(), "project_keys": request.project_keys, "comparison": comparison, "fetched_at": (snapshot or {}).get("fetched_at"), "source_quality": snapshot_quality, "preference_matches": preference_matches})
    return {"ok": True, "projects": chosen, "comparison": comparison, "artifact": artifact, "evidence_quality": {"sources": [snapshot_quality], "summary": evidence_quality_summary([{"quality": snapshot_quality}])}, "preference_matches": preference_matches, "policy": "对比同时展示看板快照的新鲜度、内容标识和个人偏好命中；偏好只解释排序，不替代项目事实。"}


@app.post("/api/cid-dashboard/research-task")
async def create_cid_research_task(request: CIDCompareRequest) -> dict[str, Any]:
    comparison = await _app_call('compare_cid_projects', request)
    item = _app_call('create_work_item_record', title=f"研究 CID 竞品比较：{', '.join(request.project_keys)}", description="请基于竞品比较结果继续做可复制性分析、个人偏好匹配和网页/知识库证据检索。", kind="cid_research", source_project="cid-dashboard", target_project="crawl4ai,knowledge", metadata={"artifact_id": comparison.get("artifact", {}).get("id"), "repo": request.repo, "project_keys": request.project_keys})
    relation = _app_call('create_relation_record', from_type="artifact", from_id=str(comparison.get("artifact", {}).get("id")), to_type="work_item", to_id=str(item.get("id")), relation_type="comparison_to_research", metadata={"target_project": "crawl4ai,knowledge"}) if comparison.get("artifact") else None
    return {"ok": True, "item": item, "relation": relation, "comparison": comparison}


def cid_review_stats(repo: str = "") -> dict[str, Any]:
    opportunities = []
    for item in _app_call('list_work_items', "all", "cid-dashboard"):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if item.get("kind") != "opportunity" or str(item.get("source_project") or "") != "cid-dashboard":
            continue
        if repo and str(metadata.get("repo") or "") != repo:
            continue
        opportunities.append(item)
    reviews = []
    for artifact in _app_call('list_artifacts', "cid-dashboard"):
        if artifact.get("kind") != "cid_opportunity_review":
            continue
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        review = metadata.get("review") if isinstance(metadata.get("review"), dict) else {}
        if repo and str(metadata.get("preferences", {}).get("repo") or metadata.get("repo") or "") not in {"", repo}:
            # Older reviews may not have repo in metadata; use the linked item below.
            linked_item = _app_call('get_work_item_record', int(metadata.get("work_item_id") or 0)) if str(metadata.get("work_item_id", "")).isdigit() else None
            linked_repo = str((linked_item or {}).get("metadata", {}).get("repo") or "")
            if linked_repo != repo:
                continue
        reviews.append((artifact, review))
    verdicts: dict[str, int] = {}
    matches = []
    confirmed = 0
    latest_review_at = ""
    for artifact, review in reviews:
        verdict = str(review.get("verdict") or "待验证")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if review.get("confirmed") is True:
            confirmed += 1
        if isinstance(review.get("preference_match"), (int, float)):
            matches.append(float(review["preference_match"]))
        latest_review_at = max(latest_review_at, str(review.get("reviewed_at") or artifact.get("created_at") or ""))
    minimum_reviews = 5
    return {
        "opportunities": len(opportunities),
        "reviewed": len(reviews),
        "confirmed": confirmed,
        "verdicts": verdicts,
        "preference_match_rate": round(sum(matches) / len(matches), 3) if matches else None,
        "preference_match_samples": len(matches),
        "latest_review_at": latest_review_at,
        "sample_status": "ready" if len(reviews) >= minimum_reviews else "insufficient",
        "minimum_reviews": minimum_reviews,
        "policy": "偏好命中率只说明已复盘机会与个人偏好标签的重合，不代表项目成功概率。",
    }


@app.get("/api/cid-dashboard/evidence")
def get_cid_evidence(repo: str = "") -> dict[str, Any]:
    kinds = {"cid_competitor_comparison", "cid_project_opportunity", "cid_snapshot", "cid_opportunity_review"}
    artifacts = [item for item in _app_call('list_artifacts', "cid-dashboard") if item.get("kind") in kinds and (not repo or item.get("metadata", {}).get("repo") == repo)][:100]
    for artifact in artifacts:
        artifact["relations"] = _app_call('list_relations', str(artifact.get("id")))[:12]
    snapshots = _app_call('list_cid_dashboard_snapshots', repo.strip(), limit=20) if repo.strip() else []
    return {
        "artifacts": artifacts,
        "snapshots": snapshots,
        "summary": _app_call('cid_review_stats', repo.strip()),
        "replay": {"message": "每张机会卡和比较 Artifact 都保留来源快照、关系和创建时间，可从最新判断回看历史依据。", "count": len(artifacts)},
    }

@app.post("/api/aihot/opportunities/{item_id}/review")
def review_aihot_opportunity(item_id: str, request: OpportunityReviewRequest) -> dict[str, Any]:
    item = _app_call('aihot_opportunity_for_item', item_id)
    if not item:
        raise HTTPException(404, "这条热点还没有登记为机会")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    signal = metadata.get("signal") if isinstance(metadata.get("signal"), dict) else {}
    feedback = _app_call('list_aihot_feedback', [str(item_id)]).get(str(item_id), {})
    signal_text = json.dumps(signal, ensure_ascii=False, sort_keys=True)
    source_quality = evidence_quality_descriptor(source=str(signal.get("source") or "AI 热点"), data_as_of=str(signal.get("published_at") or signal.get("fetched_at") or ""), content_hash=hashlib.sha256(signal_text.encode("utf-8", errors="ignore")).hexdigest(), readable=bool(signal.get("title") or signal.get("description") or signal.get("summary")), source_url=str(signal.get("link") or signal.get("url") or ""), source_quality={"policy": "热点来源字段完整性/新鲜度，不等于机会成立"})
    review = {**_app_call('opportunity_score', signal, feedback), "source_quality": source_quality, "verdict": request.verdict.strip() or "待验证", "rationale": request.rationale.strip(), "reviewed_at": now_iso(), "confirmed": request.confirmed}
    artifact = _app_call('register_artifact_safely', project_id="aihot", name=f"热点机会复盘 · {item_id}", kind="aihot_opportunity_review", metadata={"item_id": str(item_id), "work_item_id": item.get("id"), "review": review, "source_artifact_id": metadata.get("artifact_id"), "source_quality": source_quality})
    if artifact:
        _app_call('create_relation_record', from_type="work_item", from_id=str(item["id"]), to_type="artifact", to_id=str(artifact["id"]), relation_type="opportunity_to_review", metadata={"confirmed": request.confirmed})
    updated = _app_call('update_work_item_record', item["id"], {"metadata_json": json.dumps({**metadata, "opportunity_review": review, "review_artifact_id": artifact.get("id") if artifact else None}, ensure_ascii=False)})
    return {"ok": True, "review": review, "artifact": artifact, "item": updated or item}


def load_cid_preferences() -> dict[str, Any]:
    path = DATA_DIR / "cid_preferences.json"
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        return values if isinstance(values, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@app.get("/api/cid-dashboard/preferences")
async def get_cid_preferences() -> dict[str, Any]:
    return {"preferences": _app_call('load_cid_preferences', ), "policy": "个人偏好只用于解释机会排序，不覆盖看板原始数据。"}


@app.put("/api/cid-dashboard/preferences")
def save_cid_preferences(request: CIDPreferenceRequest) -> dict[str, Any]:
    preferences = {str(key)[:80]: str(value)[:500] for key, value in request.preferences.items() if str(key).strip()}
    save_json_atomic(DATA_DIR / "cid_preferences.json", preferences, 0o600)
    sync_cid_preferences_to_memories(preferences)
    return {"ok": True, "preferences": preferences}


@app.post("/api/cid-dashboard/opportunities/{item_id}/review")
def review_cid_opportunity(item_id: int, request: OpportunityReviewRequest) -> dict[str, Any]:
    item = _app_call('get_work_item_record', item_id)
    if not item or item.get("source_project") != "cid-dashboard":
        raise HTTPException(404, "看板机会不存在")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    signal = metadata.get("signal") if isinstance(metadata.get("signal"), dict) else {}
    preferences = _app_call('load_cid_preferences', )
    tags = {str(tag).lower() for tag in signal.get("tags", [])}
    preferred = {str(value).lower() for value in preferences.get("preferred_tags", "").split(",") if str(value).strip()}
    avoid = {str(value).lower() for value in preferences.get("avoid_tags", "").split(",") if str(value).strip()}
    signal_text = json.dumps(signal, ensure_ascii=False, sort_keys=True)
    source_quality = evidence_quality_descriptor(source=str(signal.get("repo") or "CID 看板"), data_as_of=str(signal.get("snapshot_fetched_at") or signal.get("fetched_at") or ""), content_hash=hashlib.sha256(signal_text.encode("utf-8", errors="ignore")).hexdigest(), readable=bool(signal.get("name") or signal.get("desc")), source_url=str(signal.get("url") or signal.get("source_url") or ""), source_quality={"policy": "看板快照字段完整性/新鲜度，不等于机会成立"})
    review = {"preference_match": round(len(tags & preferred) / max(1, len(preferred)), 3) if preferred else None, "preference_matches": sorted(tags & preferred), "preference_avoids": sorted(tags & avoid), "source_quality": source_quality, "verdict": request.verdict.strip() or "待验证", "rationale": request.rationale.strip(), "reviewed_at": now_iso(), "confirmed": request.confirmed}
    artifact = _app_call('register_artifact_safely', project_id="cid-dashboard", name=f"看板机会复盘 · {item_id}", kind="cid_opportunity_review", metadata={"work_item_id": item_id, "review": review, "preferences": preferences, "source_quality": source_quality})
    if artifact:
        _app_call('create_relation_record', from_type="work_item", from_id=str(item_id), to_type="artifact", to_id=str(artifact["id"]), relation_type="opportunity_to_review", metadata={"confirmed": request.confirmed})
    updated = _app_call('update_work_item_record', item_id, {"metadata_json": json.dumps({**metadata, "opportunity_review": review, "review_artifact_id": artifact.get("id") if artifact else None}, ensure_ascii=False)})
    return {"ok": True, "review": review, "artifact": artifact, "item": updated or item}


_AIHOT_DEFAULT_SOURCES = (
    "https://aihot.today/ai-news,"
    "https://hnrss.org/newest?q=AI+OR+LLM+OR+GPT,"
    "https://www.ithome.com/rss/,"
    "https://www.oschina.net/news/rss,"
    "https://www.geekpark.net/rss,"
    "https://rss.eastmoney.com/rss_partener.xml,"
    "https://dedicated.wallstreetcn.com/rss.xml,"
    "https://www.tmtpost.com/rss,"
    "https://www.chinanews.com.cn/rss/scroll-news.xml"
)


AIHOT_SOURCES = [url.strip() for url in os.getenv("WORKBENCH_AIHOT_SOURCES", _AIHOT_DEFAULT_SOURCES).split(",") if url.strip()] or [AIHOT_FEED_URL]

# 域名 → 领域（综合源用于打标签；AI 专属源保持 ai 领域）
_AIHOT_DOMAIN_BY_HOST = {
    "aihot.today": "ai",
    "hnrss.org": "ai",
    "ithome.com": "科技",
    "oschina.net": "科技",
    "36kr.com": "商业",
    "geekpark.net": "科技",
    "eastmoney.com": "财经",
    "wallstreetcn.com": "财经",
    "tmtpost.com": "商业",
    "chinanews.com.cn": "综合",
}


def _aihot_domain(source_host: str) -> str:
    """按源域名返回领域标签，未知域名按关键词推断，兜底 generic。"""
    for host, domain in _AIHOT_DOMAIN_BY_HOST.items():
        if host in (source_host or ""):
            return domain
    return "综合"


def _aihot_relevant(entry: dict[str, Any]) -> bool:
    """热点相关性过滤：
    - AI 专属源（domain == "ai"）：标题/摘要命中 AI 关键词才保留，避免混入纯硬件/数码。
    - 综合源（科技/商业/财经/时政）：全量保留（已经按领域源订阅，无需再按 AI 关键词砍），
      保证"全领域热点雷达"的广度。
    """
    if entry.get("domain") != "ai":
        return True
    text = f"{entry.get('title') or ''} {entry.get('summary') or ''}"
    low = text.lower()
    zh = ("人工智能", "大模型", "智能体", "机器学习", "深度学习", "神经网络", "多模态", "具身", "自动驾驶", "算力", "机器人", "芯片", "AIGC")
    en = ("llm", "gpt", "claude", "gemini", "deepseek", "openai", "agentic", "transformer", "neural", " ai ", " ai-", " ai_", "\nai", "machine learning", "model context")
    return any(k in text for k in zh) or any(k in low for k in en)










@app.get("/static/sw.js")
async def service_worker_file() -> FileResponse:
    """Serve the PWA worker with permission to control the Workbench root."""
    return FileResponse(
        STATIC_DIR / "sw.js",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_file() -> FileResponse:
    """Serve the existing app icon for browsers that request a legacy favicon path."""
    return FileResponse(
        STATIC_DIR / "icons" / "nexus-192.png",
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


WORKBENCH_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"
WORKER_LEASE_SECONDS = max(60, int(os.getenv("WORKBENCH_WORKER_LEASE_SECONDS", "120")))
# 本进程启动时刻：用来判断哪些 running 的 run 是上一个进程留下的孤儿。
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
WORKBENCH_CRAWL_STALE_SECONDS = max(300, int(os.getenv("WORKBENCH_CRAWL_STALE_SECONDS", "900")))
# Automation runs are created synchronously by the API.  A run that remains
# queued beyond this window was not merely "slow"—the request that created it
# has disappeared (usually after a restart).  Keep the threshold configurable
# for unusually slow installations, but make the recovery policy explicit.
WORKER_DEFINITIONS = {
    "crawl-worker": {"label": "Crawl Worker", "scope": "网页抓取与证据产物"},
    "sync-worker": {"label": "Sync Worker", "scope": "AI 热点、Sub2API、行情自动化"},
    "monitor-worker": {"label": "Monitor Worker", "scope": "服务器巡检与告警"},
    "agent-worker": {"label": "Agent Worker", "scope": "计划、重试、交接与 LLM"},
}

# This is a capability registry, not a claim that every project already has
# an autonomous Agent. The previous implementation reported every project as
# "ready" whenever a global LLM key existed, which hid the real rollout state.
AGENT_RESULT_CONTRACT_VERSION = "1.1"



def agent_detail(project_id: str, *, llm_ready: bool | None = None) -> dict[str, Any]:
    capability = AGENT_REGISTRY.get(project_id, {})
    tools = list(capability.get("tools", []))
    if project_id != "workbench":
        tools = list(dict.fromkeys([*tools, "work_item_read", "work_item_run"]))
    # 所有项目 Agent 统一具备公网只读调研能力（web_search 搜索 / web_fetch 抓正文）。
    # 与 SUBAGENT_TOOL_MAP 保持一致：能力声明、总调度工具边界和前端能力列表
    # 不能各自为政，否则文档 Agent 写调研文档时只能"声称交接"而交接又不落地。
    tools = list(dict.fromkeys([*tools, "web_search", "web_fetch"]))
    detail = {
        "project_id": project_id,
        "name": agent_display_name(project_id),
        "status": capability.get("status", "planned"),
        "status_label": agent_status_label(capability.get("status", "planned")),
        "kind": capability.get("kind", "unknown"),
        "tools": tools,
        "rounds": capability.get("rounds", []),
        "next": capability.get("next", "建立项目 Agent 能力定义"),
    }
    implementation = AGENT_IMPLEMENTATIONS.get(project_id, {})
    detail["implemented_tools"] = implementation.get("implemented", [])
    detail["gaps"] = implementation.get("gaps", [])
    detail["links"] = project_link_summary(project_id)
    playbook = AGENT_PLAYBOOKS.get(project_id, {})
    detail["mission"] = playbook.get("mission", "围绕项目上下文给出可执行结果")
    detail["output_contract"] = playbook.get("output", ["结论", "证据", "下一步", "风险"])
    detail["autonomy"] = playbook.get("autonomy", "只读分析；写入动作按权限确认")
    permissions = []
    for tool in tools:
        policy = AGENT_TOOL_POLICIES.get(tool)
        if policy:
            permissions.append({"tool": tool, **policy})
        else:
            permissions.append(
                {
                    "tool": tool,
                    "label": tool,
                    "mode": "unavailable",
                    "risk": "unknown",
                    "enabled": False,
                    "description": "已声明但尚未接入执行器，Agent 不会假装调用。",
                }
            )
    detail["tool_permissions"] = permissions
    detail["tool_permission_summary"] = {
        "readonly": sum(1 for item in permissions if item["mode"] == "readonly"),
        "auto": sum(1 for item in permissions if item["mode"] == "auto"),
        "restricted": sum(1 for item in permissions if item["mode"] == "restricted"),
        "confirm": sum(1 for item in permissions if item["mode"] == "confirm"),
        "unavailable": sum(1 for item in permissions if not item["enabled"]),
    }
    detail["run_summary"] = agent_run_summary(project_id)
    if llm_ready is not None:
        detail["llm_ready"] = llm_ready
    return detail








def worker_instance_id() -> str:
    return WORKBENCH_INSTANCE_ID


def worker_lease(worker_id: str, *, status: str = "ready", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Claim a short SQLite lease so two Workbench processes do not run one loop twice."""
    worker_id = str(worker_id).strip()
    if worker_id not in WORKER_DEFINITIONS:
        raise ValueError(f"未知 Worker：{worker_id}")
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    lease_until = (now + timedelta(seconds=WORKER_LEASE_SECONDS)).isoformat()
    connection: sqlite3.Connection | None = None
    try:
        connection = _app_call('db_connection', )
        connection.execute(
            """INSERT INTO worker_leases(worker_id, instance_id, status, lease_until, last_heartbeat, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
              instance_id = excluded.instance_id,
              status = excluded.status,
              lease_until = excluded.lease_until,
              last_heartbeat = excluded.last_heartbeat,
              metadata_json = excluded.metadata_json
            WHERE worker_leases.lease_until < ? OR worker_leases.instance_id = ?""",
            (worker_id, _app_call('worker_instance_id', ), status, lease_until, now_text, json.dumps(metadata or {}, ensure_ascii=False), now_text, _app_call('worker_instance_id', )),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM worker_leases WHERE worker_id = ?", (worker_id,)).fetchone()
        if not row or row["instance_id"] != _app_call('worker_instance_id', ):
            return {"worker_id": worker_id, "status": "held_by_other_instance", "instance_id": row["instance_id"] if row else ""}
        result = dict(row)
        result["metadata"] = decode_json_column(result.pop("metadata_json", "{}"))
        result["lease_seconds"] = WORKER_LEASE_SECONDS
        return result
    finally:
        if connection:
            connection.close()


def release_worker_lease(worker_id: str, status: str = "stopped") -> None:
    connection = _app_call('db_connection', )
    try:
        connection.execute("UPDATE worker_leases SET status = ?, lease_until = '', last_heartbeat = ? WHERE worker_id = ? AND instance_id = ?", (status, now_iso(), worker_id, _app_call('worker_instance_id', )))
        connection.commit()
    finally:
        connection.close()


def worker_status_payload() -> list[dict[str, Any]]:
    connection = _app_call('db_connection', )
    try:
        rows = {str(row["worker_id"]): dict(row) for row in connection.execute("SELECT * FROM worker_leases").fetchall()}
        runtime: dict[str, dict[str, Any]] = {}
        for worker_id in WORKER_DEFINITIONS:
            if worker_id == "crawl-worker":
                queue_row = connection.execute(
                    "SELECT COUNT(*) AS queued, SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running FROM agent_runs WHERE project_id = 'crawl4ai' AND kind = 'crawl' AND status IN ('queued', 'running')"
                ).fetchone()
                error_row = connection.execute(
                    "SELECT id AS run_id, error, updated_at FROM agent_runs WHERE project_id = 'crawl4ai' AND kind = 'crawl' AND error != '' ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                success_row = connection.execute(
                    "SELECT id AS run_id, COALESCE(NULLIF(finished_at, ''), updated_at) AS updated_at FROM agent_runs WHERE project_id = 'crawl4ai' AND kind = 'crawl' AND status IN ('succeeded', 'completed', 'partial') AND error = '' ORDER BY COALESCE(NULLIF(finished_at, ''), updated_at) DESC LIMIT 1"
                ).fetchone()
            elif worker_id in {"sync-worker", "monitor-worker"}:
                kinds = ("market_refresh", "aihot_refresh", "sub2api_alerts") if worker_id == "sync-worker" else ("server_check",)
                placeholders = ",".join("?" for _ in kinds)
                queue_row = connection.execute(
                    f"SELECT COUNT(*) AS queued, SUM(CASE WHEN r.status = 'running' THEN 1 ELSE 0 END) AS running FROM automation_runs r JOIN automation_rules a ON a.id = r.rule_id WHERE a.kind IN ({placeholders}) AND r.status IN ('queued', 'running')",
                    kinds,
                ).fetchone()
                error_row = connection.execute(
                    f"SELECT r.id AS run_id, r.error, r.finished_at AS updated_at FROM automation_runs r JOIN automation_rules a ON a.id = r.rule_id WHERE a.kind IN ({placeholders}) AND r.error != '' ORDER BY r.finished_at DESC LIMIT 1",
                    kinds,
                ).fetchone()
                success_row = connection.execute(
                    f"SELECT r.id AS run_id, r.finished_at AS updated_at FROM automation_runs r JOIN automation_rules a ON a.id = r.rule_id WHERE a.kind IN ({placeholders}) AND r.status = 'succeeded' ORDER BY r.finished_at DESC LIMIT 1",
                    kinds,
                ).fetchone()
            else:
                queue_row = connection.execute(
                    "SELECT COUNT(*) AS queued, SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running FROM agent_runs WHERE project_id != 'crawl4ai' AND status IN ('queued', 'running')"
                ).fetchone()
                error_row = connection.execute(
                    "SELECT id AS run_id, error, updated_at FROM agent_runs WHERE project_id != 'crawl4ai' AND error != '' ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                success_row = connection.execute(
                    "SELECT id AS run_id, COALESCE(NULLIF(finished_at, ''), updated_at) AS updated_at FROM agent_runs WHERE project_id != 'crawl4ai' AND status IN ('succeeded', 'completed', 'partial') AND error = '' ORDER BY COALESCE(NULLIF(finished_at, ''), updated_at) DESC LIMIT 1"
                ).fetchone()
            error_at = _audit_datetime(error_row["updated_at"]) if error_row else None
            success_at = _audit_datetime(success_row["updated_at"]) if success_row else None
            error_state = ""
            if error_row:
                error_state = "recovered" if success_at and (not error_at or success_at > error_at) else "recent"
            runtime[worker_id] = {
                "queue_depth": int((queue_row["queued"] if queue_row else 0) or 0),
                "running_count": int((queue_row["running"] if queue_row else 0) or 0),
                "last_error": clip(str(error_row["error"] or ""), 300) if error_row else "",
                "last_error_at": str(error_row["updated_at"] or "") if error_row else "",
                "last_run_id": str(error_row["run_id"] or "") if error_row else "",
                "last_error_state": error_state,
                "last_success_at": str(success_row["updated_at"] or "") if success_row else "",
                "last_success_run_id": str(success_row["run_id"] or "") if success_row else "",
            }
    finally:
        connection.close()
    now = datetime.now(timezone.utc)
    payload = []
    for worker_id, definition in WORKER_DEFINITIONS.items():
        row = rows.get(worker_id)
        lease_dt = _audit_datetime(row.get("lease_until")) if row else None
        heartbeat_dt = _audit_datetime(row.get("last_heartbeat")) if row else None
        claimed = bool(row and lease_dt and lease_dt > now)
        try:
            metadata = json.loads(row.get("metadata_json") or "{}") if row else {}
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        payload.append({
            "id": worker_id,
            **definition,
            "status": row.get("status") if row and claimed else "unclaimed",
            "claimed": claimed,
            "instance_id": row.get("instance_id", "") if row else "",
            "last_heartbeat": row.get("last_heartbeat", "") if row else "",
            "lease_until": row.get("lease_until", "") if row else "",
            "stale": bool(row and not claimed),
            "heartbeat_age_seconds": max(0, round((now - heartbeat_dt).total_seconds())) if heartbeat_dt else None,
            "metadata": metadata if isinstance(metadata, dict) else {},
            **runtime.get(worker_id, {"queue_depth": 0, "running_count": 0, "last_error": "", "last_error_at": "", "last_run_id": "", "last_error_state": "", "last_success_at": "", "last_success_run_id": ""}),
        })
    return payload


def recover_stale_crawl_runs() -> int:
    """Put runs abandoned by a dead crawl worker back into the durable queue."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=WORKBENCH_CRAWL_STALE_SECONDS)
    cutoff_text = cutoff.isoformat()
    now = datetime.now(timezone.utc)
    recovered: list[str] = []
    connection = _app_call('db_connection', )
    try:
        rows = connection.execute(
            """SELECT agent_runs.id, agent_runs.request_json,
                      worker_leases.instance_id AS lease_instance_id,
                      worker_leases.lease_until
            FROM agent_runs
            LEFT JOIN worker_leases ON worker_leases.worker_id = 'crawl-worker'
            WHERE agent_runs.project_id = 'crawl4ai' AND agent_runs.kind = 'crawl'
              AND agent_runs.status = 'running'
              AND agent_runs.started_at != '' AND agent_runs.started_at < ?""",
            (cutoff_text,),
        ).fetchall()
        for row in rows:
            run_id = str(row["id"])
            try:
                request_payload = json.loads(row["request_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                request_payload = {}
            owner_instance = str(request_payload.get("_worker_instance_id") or "") if isinstance(request_payload, dict) else ""
            lease_until = _audit_datetime(row["lease_until"]) if row["lease_until"] else None
            # A long crawl can legitimately run longer than the stale window.
            # Only recover it when the instance that claimed it no longer owns
            # the crawl-worker lease. This prevents duplicate browser work.
            owner_is_alive = bool(
                owner_instance
                and owner_instance == str(row["lease_instance_id"] or "")
                and lease_until
                and lease_until > now
            )
            if owner_is_alive:
                continue
            cursor = connection.execute(
                """UPDATE agent_runs SET status = 'queued', error = ?, started_at = '',
                   finished_at = '', updated_at = ?
                WHERE id = ? AND status = 'running'""",
                ("Crawl Worker lease 失效，已自动重新排队。", now_iso(), run_id),
            )
            if cursor.rowcount:
                recovered.append(run_id)
        connection.commit()
    finally:
        connection.close()
    for run_id in recovered:
        add_agent_run_event(run_id, "requeued", "上一 Crawl Worker 未完成，任务已恢复到队列。", level="warning")
    return len(recovered) + _app_call('flag_orphaned_crawl_runs', )


def flag_orphaned_crawl_runs() -> int:
    """把「没有任何 Worker 会来取」的排队任务标成失败，而不是让它永远转圈。

    原来的回收逻辑只处理 status='running' 的任务，也就是"被某个 Worker 领走后
    它死了"这一种情况。但如果 Crawl Worker 根本没启动，任务会一直停在
    'queued'：页面上显示"排队等待"，用户以为在跑，实际上永远不会有人来取，
    而且这个函数本身也只在 Crawl Worker 内部被调用——worker 不在，连回收都不会发生。

    这里改为：排队时间远超正常等待窗口、且 crawl-worker 的租约已经过期（或从来
    没有过租约）时，把任务标成失败并写明原因，让它在「最近活动」里变成一条可行动
    的线索，而不是一条沉默的假进行中。
    """
    # 给正常排队留足余量：只处理超过 stale 窗口 4 倍时间还没被领走的。
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=WORKBENCH_CRAWL_STALE_SECONDS * 4)).isoformat()
    now = datetime.now(timezone.utc)
    flagged: list[str] = []
    connection = _app_call('db_connection', )
    try:
        lease = connection.execute(
            "SELECT lease_until FROM worker_leases WHERE worker_id = 'crawl-worker'"
        ).fetchone()
        lease_until = _audit_datetime(lease["lease_until"]) if lease and lease["lease_until"] else None
        if lease_until and lease_until > now:
            return 0  # Worker 还活着，排队是正常的，什么都不做。
        rows = connection.execute(
            """SELECT id FROM agent_runs
            WHERE project_id = 'crawl4ai' AND kind = 'crawl' AND status = 'queued'
              AND COALESCE(NULLIF(created_at, ''), updated_at) < ?""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            cursor = connection.execute(
                """UPDATE agent_runs SET status = 'failed', error = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'""",
                (
                    "抓取 Worker 未运行，这个任务一直没有被领取。请检查 workbench-crawl-worker 服务后重新发起。",
                    now_iso(), now_iso(), str(row["id"]),
                ),
            )
            if cursor.rowcount:
                flagged.append(str(row["id"]))
        connection.commit()
    finally:
        connection.close()
    for run_id in flagged:
        add_agent_run_event(run_id, "orphaned", "抓取 Worker 未运行，任务长时间无人领取，已标记为失败。", level="error")
    if flagged:
        log.warning("标记了 %d 个无人领取的抓取任务（Crawl Worker 可能未运行）", len(flagged))
    return len(flagged)


def claim_next_crawl_run() -> dict[str, Any] | None:
    """Atomically claim one queued crawl run for the standalone worker."""
    connection = _app_call('db_connection', )
    run_id = ""
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT id, request_json FROM agent_runs
            WHERE project_id = 'crawl4ai' AND kind = 'crawl' AND status = 'queued'
            ORDER BY created_at ASC LIMIT 1"""
        ).fetchone()
        if not row:
            connection.rollback()
            return None
        run_id = str(row["id"])
        try:
            request_payload = json.loads(row["request_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            request_payload = {}
        if not isinstance(request_payload, dict):
            request_payload = {}
        request_payload["_worker_instance_id"] = _app_call('worker_instance_id', )
        request_payload["_claimed_at"] = now_iso()
        cursor = connection.execute(
            """UPDATE agent_runs SET status = 'running', started_at = ?, finished_at = '',
               error = '', request_json = ?, updated_at = ?
            WHERE id = ? AND status = 'queued'""",
            (now_iso(), json.dumps(request_payload, ensure_ascii=False), now_iso(), run_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        connection.commit()
    finally:
        connection.close()
    add_agent_run_event(run_id, "claimed", "Crawl Worker 已领取任务。", metadata={"worker": _app_call('worker_instance_id', )})
    return get_agent_run(run_id)




def load_aihot_snapshot() -> dict[str, Any]:
    values = _app_call('load_json_file', AIHOT_SNAPSHOT_FILE, {})
    return values if isinstance(values, dict) else {"items": []}


def save_aihot_snapshot(values: dict[str, Any]) -> None:
    save_json_atomic(AIHOT_SNAPSHOT_FILE, values, 0o600)


def parse_aihot_items(html: str) -> list[dict[str, Any]]:
    """Read the public SSR data from aiHot's Next.js page.

    We intentionally consume only the public initialNewsData payload. This
    avoids copying the site's UI and keeps the workbench's wrapper focused on
    filtering, provenance and Agent conversation.
    """
    pattern = r'self\.__next_f\.push\(\[1,"((?:\\.|[^"\\])*)"\]\)'
    chunks = []
    for match in re.finditer(pattern, html):
        try:
            chunks.append(json.loads('"' + match.group(1) + '"'))
        except json.JSONDecodeError:
            continue
    payload = "\n".join(chunks)
    marker = '"initialNewsData":'
    start = payload.find(marker)
    if start < 0:
        return []
    start += len(marker)
    try:
        parsed, _ = json.JSONDecoder().raw_decode(payload[start:].lstrip())
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    items = []
    for raw in parsed:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        published_at = str(raw.get("published_at") or "").removeprefix("$D")
        tags = [tag.strip() for tag in str(raw.get("tag") or "").split(",") if tag.strip()]
        items.append(
            {
                "id": str(raw.get("id")),
                "title": raw.get("title_trans") or raw.get("title") or "未命名资讯",
                "description": raw.get("des_trans") or raw.get("des") or "",
                "source": raw.get("sources") or "未知来源",
                "tags": tags,
                "published_at": published_at,
                "link": raw.get("link") or "",
                "importance": int(raw.get("importance") or 0),
                "business_opportunity": raw.get("business_opportunity") or "",
                "layer": raw.get("layer") or "",
                "value": raw.get("value") or "",
                "impact": raw.get("impact") or "",
            }
        )
    return _app_call('dedupe_aihot_items', items)


def normalized_aihot_title(title: str) -> str:
    """Make translated/source title variants comparable for feed de-duplication."""
    normalized = unicodedata.normalize("NFKC", str(title or "")).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def canonical_aihot_link(link: str) -> str:
    value = str(link or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if not parsed.netloc:
        return value.lower().rstrip("/")
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/').lower()}"


def parse_rss_items(xml_text: str, source_name: str) -> list[dict[str, Any]]:
    """Parse RSS 2.0 / Atom feeds into the same shape as `parse_aihot_items`.

    Uses only the standard library so the workbench has no extra install cost.
    Stories without a title or link are dropped; importance / business_opportunity
    default to 0 / "" so the dedupe pass keeps the richer record when the same
    story shows up on multiple sources.
    """
    from xml.etree import ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[dict[str, Any]] = []
    hash_inputs: list[str] = []
    for entry in root.iter("item"):
        title = (entry.findtext("title") or "").strip()
        link = (entry.findtext("link") or "").strip()
        desc = (entry.findtext("description") or "").strip()
        pub = (entry.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        hash_inputs.append(link)
        items.append({
            "id": f"rss:{hashlib.md5(link.encode('utf-8')).hexdigest()[:12]}",
            "title": title,
            "description": _app_call('_strip_html', desc)[:300],
            "source": source_name,
            "tags": [],
            "published_at": pub,
            "link": link,
            "importance": 0,
            "business_opportunity": "",
            "layer": "",
        })
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.get("href", "") if link_el is not None else ""
            desc = (entry.findtext("atom:summary", default="", namespaces=ns) or entry.findtext("atom:content", default="", namespaces=ns) or "").strip()
            pub = (entry.findtext("atom:updated", default="", namespaces=ns) or entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
            if not title or not link:
                continue
            items.append({
                "id": f"rss:{hashlib.md5(link.encode('utf-8')).hexdigest()[:12]}",
                "title": title,
                "description": _app_call('_strip_html', desc)[:300],
                "source": source_name,
                "tags": [],
                "published_at": pub,
                "link": link,
                "importance": 0,
                "business_opportunity": "",
                "layer": "",
            })
    return items

async def stream_aihot_agent_turn(
    *,
    run: dict[str, Any],
    session: dict[str, Any],
    message: str,
    chosen: list[dict[str, Any]],
):
    """流式版 AI 热点 Agent：边收边产出 SSE 事件，收完后持久化（与 run_aihot_agent_turn 同语义）。"""
    update_agent_run_record(run["id"], status="running", error="")
    add_agent_run_event(run["id"], "started", "AI 热点 Agent 开始整理所选资讯。")
    evidence = "\n\n".join(
        f"[{index}] {item.get('title')}\n来源：{item.get('source')}｜时间：{item.get('published_at')}｜重要度：{item.get('importance')}\n摘要：{item.get('description')}\n原文：{item.get('link')}"
        for index, item in enumerate(chosen, start=1)
    )
    system = (
        "你是工作台中的 AI 热点研究 Agent。只基于下方 aiHot.today 公开资讯回答，不能把新闻标题当成已验证事实。"
        "用户可能想知道哪些消息值得看、对个人效率或创业有什么启发、是否值得继续研究。"
        "请明确区分：已知信息、你的判断、需要验证的地方。给出最多 3 个可执行的下一步。"
        "如果用户要求发现商机，请优先从真实需求、目标用户、付费可能、竞争和 7 天验证切入。使用简体中文。"
    )
    try:
        history = list_agent_messages(session["id"], limit=MAX_CONVERSATION_MESSAGES * 2)
        messages = [
            {"role": "system", "content": system},
            *({"role": item["role"], "content": item["content"]} for item in history[-MAX_CONVERSATION_MESSAGES:]),
            {"role": "user", "content": f"资讯证据：\n{clip_for_llm(evidence, 18_000)}\n\n用户问题：\n{message}"},
        ]
        add_agent_run_event(run["id"], "llm_started", "正在调用全局 LLM 分析热点证据。", metadata={"items": len(chosen)})
        collected: list[str] = []
        provider = ""
        usage = None
        async for chunk in stream_llm_text(messages, max_tokens=4000, temperature=0.25, purpose="aihot"):
            if chunk["type"] == "delta":
                if chunk.get("text"):
                    collected.append(chunk["text"])
                yield chunk
            elif chunk["type"] == "reset":
                collected.clear()
                yield chunk
            elif chunk["type"] == "finish":
                provider = chunk.get("provider", "")
                usage = chunk.get("usage")
            elif chunk["type"] == "error":
                yield chunk
        answer = "".join(collected).strip()
        if not answer:
            update_agent_run_record(run["id"], status="failed", error="LLM 未返回内容")
            add_agent_run_event(run["id"], "failed", "AI 热点 Agent 未返回内容。", level="error")
            yield {"type": "error", "message": "LLM 未返回内容，请稍后重试。", "provider": provider}
            return
        add_agent_run_event(run["id"], "llm_succeeded", "热点分析已返回。", level="success")
        evidence_items = [{"id": item.get("id", ""), "type": "aihot_item", "title": item.get("title", ""), "source": item.get("source", ""), "published_at": item.get("published_at", ""), "link": item.get("link", "")} for item in chosen]
        result_contract = agent_result_contract("aihot", answer, evidence=evidence_items, source_refs=evidence_items, run_id=run["id"], session_id=session["id"])
        assistant_message = add_agent_message(session["id"], "assistant", answer, {"run_id": run["id"], "item_ids": [item.get("id") for item in chosen], "result_contract": result_contract})
        session = update_agent_session_summary(
            session["id"],
            {"last_answer": clip(answer, 1200), "last_result_contract": result_contract, "last_run_id": run["id"], "selected_items": [item.get("id") for item in chosen]},
        ) or session
        result = {"answer": answer, "items": chosen, "session_id": session["id"], "message_id": assistant_message.get("id"), "result_contract": result_contract}
        updated_run = update_agent_run_record(run["id"], status="succeeded", result=result, error="") or run
        add_agent_run_event(run["id"], "succeeded", "AI 热点 Agent 本轮完成。", level="success")
        yield {"type": "finish", "reason": "stop", "usage": usage, "provider": provider, "answer": answer, "session_id": session["id"], "message_id": assistant_message.get("id"), "result_contract": result_contract}
    except Exception as exc:
        update_agent_run_record(run["id"], status="failed", error=clip(str(exc), 500))
        add_agent_run_event(run["id"], "failed", f"AI 热点 Agent 失败：{clip(str(exc), 200)}", level="error")
        yield {"type": "error", "message": clip(str(exc), 300), "provider": ""}

def aihot_review_stats(items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Explain AI-hotspot feedback and opportunity review maturity."""
    current_items = items if items is not None else _app_call('dedupe_aihot_items', (_app_call('load_aihot_snapshot', ).get("items") or []))
    item_sources = {str(item.get("id")): str(item.get("source") or "未知来源") for item in current_items if item.get("id")}
    feedback = _app_call('list_aihot_feedback', )
    useful = sum(1 for item in feedback.values() if item.get("vote") == "useful")
    not_useful = sum(1 for item in feedback.values() if item.get("vote") == "not_useful")
    source_feedback: dict[str, dict[str, Any]] = {}
    for item_id, record in feedback.items():
        source = item_sources.get(str(item_id), "历史/未知来源")
        stats = source_feedback.setdefault(source, {"source": source, "samples": 0, "useful": 0, "not_useful": 0})
        if record.get("vote") not in {"useful", "not_useful"}:
            continue
        stats["samples"] += 1
        stats[str(record.get("vote"))] += 1
    for stats in source_feedback.values():
        samples = stats["samples"]
        stats["useful_rate"] = round(stats["useful"] / samples, 3) if samples else None
    opportunities = [item for item in _app_call('list_work_items', "all", "aihot") if item.get("kind") == "opportunity" and str(item.get("source_project") or "") == "aihot"]
    review_artifacts = [item for item in _app_call('list_artifacts', "aihot") if item.get("kind") == "aihot_opportunity_review"]
    verdicts: dict[str, int] = {}
    confirmed = 0
    latest_review_at = ""
    for artifact in review_artifacts:
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        review = metadata.get("review") if isinstance(metadata.get("review"), dict) else {}
        verdict = str(review.get("verdict") or "待验证")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if review.get("confirmed") is True:
            confirmed += 1
        latest_review_at = max(latest_review_at, str(review.get("reviewed_at") or artifact.get("created_at") or ""))
    minimum_feedback = 10
    return {
        "feedback": {
            "total": useful + not_useful,
            "useful": useful,
            "not_useful": not_useful,
            "useful_rate": round(useful / (useful + not_useful), 3) if useful + not_useful else None,
            "sample_status": "ready" if useful + not_useful >= minimum_feedback else "insufficient",
            "minimum_samples": minimum_feedback,
            "last_feedback_at": max((str(item.get("updated_at") or item.get("created_at") or "") for item in feedback.values()), default=""),
        },
        "source_feedback": sorted(source_feedback.values(), key=lambda item: (-item["samples"], item["source"])),
        "opportunities": {
            "total": len(opportunities),
            "reviewed": len(review_artifacts),
            "confirmed": confirmed,
            "verdicts": verdicts,
            "latest_review_at": latest_review_at,
            "sample_status": "ready" if review_artifacts else "insufficient",
        },
        "policy": "反馈有用率只描述个人筛选偏好；机会复盘不等于商业成功率，样本不足时不下结论。",
    }


def aihot_insights() -> dict[str, Any]:
    snapshot = _app_call('load_aihot_snapshot', )
    items = _app_call('dedupe_aihot_items', snapshot.get("items") or [])
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        tokens = knowledge_tokens(f"{item.get('title', '')} {item.get('description', '')} {' '.join(item.get('tags') or [])}")
        key = next(iter(sorted(tokens)), "未分类") if tokens else "未分类"
        groups.setdefault(key, []).append(item)
    feedback = _app_call('list_aihot_feedback', [str(item.get("id")) for item in items])
    source_stats: dict[str, dict[str, Any]] = {}
    for item in items:
        source = str(item.get("source") or "未知来源")
        stats = source_stats.setdefault(source, {"source": source, "count": 0, "useful": 0, "not_useful": 0})
        stats["count"] += 1
        vote = feedback.get(str(item.get("id")), {}).get("vote")
        if vote in {"useful", "not_useful"}:
            stats[vote] += 1
    source_scores = []
    for stats in source_stats.values():
        stats["quality_score"] = round((stats["useful"] + 1) / (stats["count"] + 2), 3)
        source_scores.append(stats)
    clusters = [{"label": key, "count": len(group), "items": group[:8]} for key, group in sorted(groups.items(), key=lambda pair: -len(pair[1]))[:12]]
    previous = snapshot.get("previous_items") if isinstance(snapshot.get("previous_items"), list) else []
    previous_ids = {str(item.get("id")) for item in previous if isinstance(item, dict) and item.get("id")}
    current_ids = {str(item.get("id")) for item in items if item.get("id")}
    new_items = [item for item in items if str(item.get("id")) not in previous_ids]
    removed_items = [item for item in previous if str(item.get("id")) not in current_ids]
    opportunity_items = [item for item in _app_call('list_work_items', "all", "aihot") if item.get("kind") == "opportunity"]
    opportunity_replay = [{"id": item.get("id"), "title": item.get("title"), "status": item.get("status"), "updated_at": item.get("updated_at"), "next": (item.get("metadata") or {}).get("next_step", "")} for item in opportunity_items[:20]]
    change_detection = {
        "mode": "snapshot-baseline",
        "message": "每次同步会保留上一份标题/链接基线，新增和消失的项目可回看。",
        "new_items": len(new_items),
        "removed_items": len(removed_items),
        "new": [{"title": item.get("title"), "link": item.get("link"), "source": item.get("source")} for item in new_items[:30]],
        "removed": [{"title": item.get("title"), "link": item.get("link")} for item in removed_items[:30]],
    }
    return {"fetched_at": snapshot.get("fetched_at"), "item_count": len(items), "clusters": clusters, "source_scores": sorted(source_scores, key=lambda item: -item["quality_score"]), "change_detection": change_detection, "opportunity_replay": opportunity_replay, "review_stats": _app_call('aihot_review_stats', items), "summary": f"当前 {len(items)} 条资讯，形成 {len(clusters)} 个主题簇；本轮新增 {len(new_items)} 条，来源质量分数基于本地反馈和样本量。"}

def opportunity_score(signal: dict[str, Any], feedback: dict[str, Any] | None = None) -> dict[str, Any]:
    feedback = feedback or {}
    factors = []
    score = 0
    if signal.get("link") or signal.get("url"):
        score += 20
        factors.append("有可回溯来源")
    if str(signal.get("description") or signal.get("desc") or "").strip():
        score += 20
        factors.append("有摘要材料")
    if str(signal.get("business_opportunity") or "").strip():
        score += 30
        factors.append("包含机会线索")
    if feedback.get("vote") == "useful":
        score += 20
        factors.append("个人反馈为有用")
    elif feedback.get("vote") == "not_useful":
        score -= 20
        factors.append("个人反馈为不相关")
    if signal.get("published_at") or signal.get("fetched_at"):
        score += 10
        factors.append("有数据时间")
    score = max(0, min(100, score))
    return {"score": score, "level": "高" if score >= 70 else "中" if score >= 40 else "低", "factors": factors, "policy": "这是结构化筛选分，不是商业成功概率。"}


__all__ = [
    "AIHotChatRequest",
    "AIHotFeedbackRequest",
    "AIHotOpportunityRequest",
    "CIDSnapshotRequest",
    "CIDOpportunityRequest",
    "OpportunityReviewRequest",
    "CIDPreferenceRequest",
    "dedupe_aihot_items",
    "list_aihot_feedback",
    "save_aihot_feedback",
    "cid_snapshot_row",
    "refresh_cid_opportunity_status",
    "save_cid_dashboard_snapshot",
    "list_cid_dashboard_snapshots",
    "cid_opportunity_for_project",
    "aihot_opportunity_for_item",
    "enrich_aihot_items",
    "fetch_aihot_snapshot",
    "_hostname",
    "select_aihot_items",
    "github_repository_parts",
    "_plain_external_text",
    "_activitywatch_buckets",
    "_activitywatch_events",
    "_activitywatch_duration",
    "run_aihot_agent_turn",
    "run_cid_agent_turn",
    "get_aihot_feed",
    "generate_aihot_digest",
    "get_cid_dashboard_snapshot",
    "save_cid_dashboard_snapshot_route",
    "get_cid_dashboard_opportunities",
    "CIDPreferenceLearnRequest",
    "learn_cid_preference",
    "create_cid_dashboard_opportunity",
    "save_aihot_feedback_route",
    "create_aihot_opportunity_route",
    "aihot_opportunity_review",
    "chat_aihot",
    "get_aihot_insights",
    "create_aihot_summary",
    "compare_cid_projects",
    "create_cid_research_task",
    "cid_review_stats",
    "get_cid_evidence",
    "review_aihot_opportunity",
    "load_cid_preferences",
    "get_cid_preferences",
    "save_cid_preferences",
    "review_cid_opportunity",
    "_AIHOT_DEFAULT_SOURCES",
    "AIHOT_SOURCES",
    "_AIHOT_DOMAIN_BY_HOST",
    "_aihot_domain",
    "_aihot_relevant",
    "service_worker_file",
    "favicon_file",
    "WORKBENCH_INSTANCE_ID",
    "WORKER_LEASE_SECONDS",
    "_PROCESS_STARTED_AT",
    "WORKBENCH_CRAWL_STALE_SECONDS",
    "WORKER_DEFINITIONS",
    "AGENT_RESULT_CONTRACT_VERSION",
    "agent_detail",
    "worker_instance_id",
    "worker_lease",
    "release_worker_lease",
    "worker_status_payload",
    "recover_stale_crawl_runs",
    "flag_orphaned_crawl_runs",
    "claim_next_crawl_run",
    "load_aihot_snapshot",
    "save_aihot_snapshot",
    "parse_aihot_items",
    "normalized_aihot_title",
    "canonical_aihot_link",
    "parse_rss_items",
    "stream_aihot_agent_turn",
    "aihot_review_stats",
    "aihot_insights",
    "CIDCompareRequest",
    "opportunity_score",
]
