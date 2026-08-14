"""Workbench 收件箱领域：条目 CRUD、分类/截止/交接候选、Agent 整理。

从 app.py 拆出的领域模块（为开源准备）。依赖 core（decode_json_column/now_iso/
clip）与 db；create_agent_run_record/update_agent_run_record/add_agent_run_event/
knowledge_tokens 仍留 app.py（agent 运行基础设施与知识库领域），这里用延迟转发。

obsidian 耦合函数（knowledge_inbox_candidates / sync_inbox_to_obsidian）与
React 工具（_react_inbox_read）仍留 app.py，待 knowledge/obsidian 领域拆时一起搬。
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from .core import clip, decode_json_column, decode_json_value, now_iso
from .db import db_connection


def create_agent_run_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_agent_run_record（仍在 app.py）。"""
    import app as _app

    return _app.create_agent_run_record(*args, **kwargs)


def update_agent_run_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.update_agent_run_record（仍在 app.py）。"""
    import app as _app

    return _app.update_agent_run_record(*args, **kwargs)


def add_agent_run_event(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.add_agent_run_event（仍在 app.py）。"""
    import app as _app

    return _app.add_agent_run_event(*args, **kwargs)


def knowledge_tokens(value: str) -> set[str]:
    """延迟转发 app.knowledge_tokens（仍在 app.py，知识库领域）。"""
    import app as _app

    return _app.knowledge_tokens(value)


def agent_display_name(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.agent_display_name（仍在 app.py）。"""
    import app as _app

    return _app.agent_display_name(*args, **kwargs)


def project_href(project_id: str) -> str:
    """延迟转发 app.project_href（仍在 app.py）。"""
    import app as _app

    return _app.project_href(project_id)


def inbox_row(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    item["analysis"] = decode_json_column(item.pop("analysis_json", "{}"))
    item["duplicate"] = bool(item.get("duplicate_of"))
    item["is_overdue"] = bool(
        item.get("status") == "inbox"
        and item.get("due_at")
        and item.get("due_at") < datetime.now(timezone.utc).date().isoformat()
    )
    item["priority_label"] = {"urgent": "紧急", "high": "高", "normal": "普通", "low": "低"}.get(
        item.get("priority", "normal"), item.get("priority", "普通")
    )
    item["routes"] = list_inbox_route_candidates(int(item["id"])) if item.get("id") else []
    return item


def inbox_route_candidate_row(row: sqlite3.Row) -> dict[str, Any]:
    candidate = {key: row[key] for key in row.keys()}
    candidate["metadata"] = decode_json_column(candidate.pop("metadata_json", "{}"))
    candidate["target_name"] = agent_display_name(candidate.get("target_project", ""))
    candidate["target_href"] = project_href(candidate.get("target_project", ""))
    candidate["confidence_percent"] = round(float(candidate.get("confidence") or 0) * 100)
    return candidate


def list_inbox_route_candidates(inbox_id: int, status: str = "all") -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        if status and status != "all":
            rows = connection.execute(
                "SELECT * FROM inbox_route_candidates WHERE inbox_id = ? AND status = ? ORDER BY confidence DESC, id ASC",
                (inbox_id, status),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM inbox_route_candidates WHERE inbox_id = ? ORDER BY confidence DESC, id ASC",
                (inbox_id,),
            ).fetchall()
        return [inbox_route_candidate_row(row) for row in rows]
    finally:
        connection.close()


def get_inbox_record(item_id: int) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM inbox WHERE id = ?", (item_id,)).fetchone()
        return inbox_row(row) if row else None
    finally:
        connection.close()


def get_inbox_route_candidate(candidate_id: int, inbox_id: int = 0) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        if inbox_id:
            row = connection.execute(
                "SELECT * FROM inbox_route_candidates WHERE id = ? AND inbox_id = ?",
                (candidate_id, inbox_id),
            ).fetchone()
        else:
            row = connection.execute("SELECT * FROM inbox_route_candidates WHERE id = ?", (candidate_id,)).fetchone()
        return inbox_route_candidate_row(row) if row else None
    finally:
        connection.close()


def update_inbox_route_candidate(candidate_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {"status", "work_item_id", "relation_id", "updated_at"}
    updates = [(key, value) for key, value in values.items() if key in allowed]
    if not updates:
        return get_inbox_route_candidate(candidate_id)
    if not any(key == "updated_at" for key, _ in updates):
        updates.append(("updated_at", now_iso()))
    connection = db_connection()
    try:
        assignments = ", ".join(f"{key} = ?" for key, _ in updates)
        cursor = connection.execute(
            f"UPDATE inbox_route_candidates SET {assignments} WHERE id = ?",
            [value for _, value in updates] + [candidate_id],
        )
        connection.commit()
        if cursor.rowcount == 0:
            return None
        row = connection.execute("SELECT * FROM inbox_route_candidates WHERE id = ?", (candidate_id,)).fetchone()
        return inbox_route_candidate_row(row) if row else None
    finally:
        connection.close()


def list_inbox(status: str = "all") -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        order = """
            ORDER BY
              CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
              CASE WHEN status = 'inbox' AND due_at <> '' AND due_at < date('now') THEN 0 ELSE 1 END,
              CASE WHEN due_at = '' THEN 1 ELSE 0 END,
              due_at ASC,
              created_at DESC
        """
        if status == "all":
            rows = connection.execute(f"SELECT * FROM inbox {order}").fetchall()
        else:
            rows = connection.execute(
                f"SELECT * FROM inbox WHERE status = ? {order}", (status,)
            ).fetchall()
        return [inbox_row(row) for row in rows]
    finally:
        connection.close()


def inbox_summary() -> dict[str, int]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT status, COUNT(*) AS count FROM inbox GROUP BY status").fetchall()
        summary = {"inbox": 0, "done": 0, "archived": 0, "all": 0}
        for row in rows:
            summary[str(row["status"])] = int(row["count"])
            summary["all"] += int(row["count"])
        return summary
    finally:
        connection.close()


def create_inbox_record(*, content: str, kind: str = "note", tags: str = "", priority: str = "normal", source: str = "") -> dict[str, Any]:
    priority = priority.strip() or "normal"
    if priority not in {"urgent", "high", "normal", "low"}:
        priority = "normal"
    timestamp = now_iso()
    source = clip(source.strip(), 500)
    connection = db_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO inbox (content, kind, tags, status, priority, source, created_at, updated_at) VALUES (?, ?, ?, 'inbox', ?, ?, ?, ?)",
            (clip(content.strip(), 20_000), kind.strip() or "note", tags.strip(), priority, source, timestamp, timestamp),
        )
        connection.commit()
        item_id = int(cursor.lastrowid)
    finally:
        connection.close()
    try:
        return triage_inbox_record(item_id)
    except Exception:
        # Triage is an enhancement to capture, not a reason to lose the user's input.
        return get_inbox_record(item_id) or {"id": item_id, "content": content, "kind": kind, "tags": tags, "status": "inbox"}


def normalized_inbox_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (value or "").lower())


def extract_inbox_due(content: str) -> dict[str, str]:
    text = str(content or "")
    now = datetime.now(timezone.utc)
    due: datetime | None = None
    label = ""
    if "今天" in text:
        due = now
        label = "今天"
    elif "明天" in text:
        due = now + timedelta(days=1)
        label = "明天"
    elif "后天" in text:
        due = now + timedelta(days=2)
        label = "后天"
    else:
        relative = re.search(r"(\d{1,3})\s*[天日]\s*(?:内|以内|后)", text)
        if relative:
            days = int(relative.group(1))
            due = now + timedelta(days=days)
            label = f"{days} 天内" if "内" in relative.group(0) else f"{days} 天后"
    if due is None:
        full_date = re.search(r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?", text)
        short_date = re.search(r"(?<!\d)(\d{1,2})\s*[月/]\s*(\d{1,2})\s*日?", text)
        try:
            if full_date:
                due = datetime(int(full_date.group(1)), int(full_date.group(2)), int(full_date.group(3)), tzinfo=timezone.utc)
                label = f"{full_date.group(1)}-{int(full_date.group(2)):02d}-{int(full_date.group(3)):02d}"
            elif short_date:
                month, day = int(short_date.group(1)), int(short_date.group(2))
                due = datetime(now.year, month, day, tzinfo=timezone.utc)
                if due.date() < now.date():
                    due = due.replace(year=now.year + 1)
                label = f"{month}月{day}日"
        except ValueError:
            due = None
    if due is None and "本周" in text:
        due = now + timedelta(days=6 - now.weekday())
        label = "本周"
    if due is None and "下周" in text:
        due = now + timedelta(days=7 - now.weekday() + 6)
        label = "下周"
    return {"due_at": due.date().isoformat(), "due_label": label} if due else {"due_at": "", "due_label": ""}


def extract_inbox_next_steps(content: str, classification: str = "note") -> dict[str, Any]:
    """Turn a captured note into small, reviewable next-step suggestions.

    This is deliberately deterministic.  It may split explicit bullets and
    imperative clauses, and otherwise supplies a conservative checklist for
    the detected kind.  It never executes a step or pretends that a guessed
    step came from the user.
    """
    text = str(content or "").strip()
    candidates: list[str] = []
    for raw_line in re.split(r"\r?\n", text):
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", raw_line).strip()
        if not line:
            continue
        for part in re.split(r"[。！？!?；;]+", line):
            part = re.sub(r"^\s*(?:下一步|接下来|然后|再|并且|以及)[:：]?\s*", "", part).strip()
            if 3 <= len(part) <= 160:
                candidates.append(part)

    # A single sentence often contains a useful sequence: “先…再…”。
    if len(candidates) <= 1:
        sequence = re.split(r"\s*(?:先|然后|接着|再|之后)\s*", text)
        for part in sequence:
            part = part.strip(" ，,：:。！？!?；;")
            if 3 <= len(part) <= 160:
                candidates.append(part)

    imperative_leads = ("确认", "补充", "查看", "打开", "记录", "明确", "整理", "交给", "检查", "先", "需要", "请", "跟进", "联系", "验证", "研究", "学习", "阅读", "设计", "写")
    explicit = [item for item in candidates if item.startswith(imperative_leads)]
    steps: list[str] = []
    for item in explicit:
        normalized = re.sub(r"\s+", " ", item).strip()
        if normalized and normalized not in steps:
            steps.append(normalized)
        if len(steps) >= 3:
            break

    if steps:
        return {"steps": steps, "source": "captured_text"}

    defaults = {
        "task": ["确认负责人和截止时间", "完成后回写结果或阻塞原因"],
        "research": ["明确研究问题和时间范围", "补充来源并记录结论与不确定性"],
        "document": ["确认交付对象和格式", "补齐材料并检查来源覆盖"],
        "link": ["打开来源并判断是否值得保留", "记录要点或交给知识库沉淀"],
        "alert": ["确认影响范围和数据时间", "查看来源并记录处理结果"],
        "idea": ["写清关键假设", "选择一个最小验证动作"],
    }
    return {"steps": defaults.get(classification, ["补充这条记录的背景和期望结果"]), "source": "conservative_template"}


def inbox_duplicate_match(item_id: int, content: str) -> dict[str, Any]:
    normalized = normalized_inbox_text(content)
    if len(normalized) < 8:
        return {"id": 0, "score": 0, "reason": ""}
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT id, content FROM inbox WHERE id <> ? AND status = 'inbox' ORDER BY id DESC LIMIT 120",
            (item_id,),
        ).fetchall()
    finally:
        connection.close()
    best: tuple[float, int, str] = (0, 0, "")
    for row in rows:
        other = normalized_inbox_text(row["content"])
        if not other:
            continue
        score = difflib.SequenceMatcher(None, normalized, other).ratio()
        if score > best[0]:
            best = (score, int(row["id"]), str(row["content"]))
    threshold = 0.88 if len(normalized) < 30 else 0.74
    return {"id": best[1] if best[0] >= threshold else 0, "score": round(best[0], 3), "reason": best[2] if best[0] >= threshold else ""}


def inbox_learned_classification(content: str, item_id: int = 0) -> dict[str, Any] | None:
    """Use confirmed local labels as a soft hint; keyword rules remain the fallback."""
    tokens = knowledge_tokens(content)
    if len(tokens) < 2:
        return None
    connection = db_connection()
    try:
        rows = connection.execute(
            """SELECT f.accepted, i.content FROM inbox_classification_feedback f
            JOIN inbox i ON i.id = f.inbox_id WHERE f.inbox_id <> ? ORDER BY f.created_at DESC LIMIT 120""",
            (item_id,),
        ).fetchall()
    finally:
        connection.close()
    best: tuple[float, str] = (0.0, "")
    for row in rows:
        other_tokens = knowledge_tokens(str(row["content"] or ""))
        score = len(tokens.intersection(other_tokens)) / max(1, len(tokens.union(other_tokens)))
        if score > best[0]:
            best = (score, str(row["accepted"] or ""))
    if best[0] < 0.18 or best[1] not in {"note", "task", "link", "idea", "alert", "document", "research"}:
        return None
    return {"classification": best[1], "confidence": round(min(0.92, 0.58 + best[0] * 0.7), 3), "similarity": round(best[0], 3), "source": "confirmed_history"}


def infer_inbox_triage(content: str, kind: str, tags: str, item_id: int) -> dict[str, Any]:
    text = str(content or "").strip()
    lowered = text.lower()
    explicit_kind = kind if kind in {"note", "task", "link", "idea"} else "note"
    kind_rules = [
        ("alert", ("告警", "异常", "报错", "宕机", "服务器", "磁盘", "内存", "nginx", "余额", "额度", "到期")),
        ("document", ("prd", "文档", "报告", "方案", "周报", "会议纪要", "pdf", "word", "ppt")),
        ("research", ("研究", "调研", "查一下", "资料", "竞品", "对比", "爬", "抓取", "来源")),
        ("idea", ("想法", "商机", "创业", "靠谱不靠谱", "做点事", "可行性", "机会")),
        ("task", ("待办", "任务", "截止", "完成", "处理", "跟进", "提醒", "需要")),
        ("link", ("http://", "https://", "www.")),
    ]
    classification = explicit_kind
    confidence = 0.76 if explicit_kind != "note" else 0.46
    evidence: list[str] = []
    if explicit_kind == "note":
        for candidate, words in kind_rules:
            matched = [word for word in words if word.lower() in lowered]
            if matched:
                classification = candidate
                confidence = min(0.94, 0.62 + 0.06 * len(matched))
                evidence = matched[:4]
                break
        learned = inbox_learned_classification(text, item_id)
        if learned and learned["confidence"] > confidence:
            classification = learned["classification"]
            confidence = learned["confidence"]
            evidence = [f"历史确认相似度 {learned['similarity']}"]
    due = extract_inbox_due(text)
    duplicate = inbox_duplicate_match(item_id, text)
    next_steps = extract_inbox_next_steps(text, classification)
    allowed_targets = {edge["to"] for edge in PROJECT_LINKS if edge["from"] == "inbox"}
    route_specs = {
        "knowledge": ("note_capture", "这条内容更适合沉淀为可检索笔记。", ("知识", "笔记", "沉淀", "方法", "总结")),
        "crawl4ai": ("research_task", "这条内容需要外部资料或来源证据。", ("研究", "调研", "资料", "竞品", "爬", "抓取", "网页")),
        "idea-analysis": ("idea_review", "这条内容包含想法或商机，需要做可行性和最小验证。", ("想法", "商机", "创业", "机会", "靠谱不靠谱", "可行性")),
        "doc-factory": ("document_task", "这条内容指向文档、报告或 PRD 交付。", ("prd", "文档", "报告", "方案", "周报", "纪要")),
        "server": ("incident_to_task", "这条内容可能是服务器或服务异常，交给运维 Agent 排查。", ("服务器", "主机", "磁盘", "内存", "nginx", "宕机", "部署")),
        "sub2api": ("quota_alert", "这条内容可能涉及账户额度、余额或到期风险。", ("sub2api", "余额", "额度", "订阅", "key", "到期")),
        "market": ("market_research", "这条内容涉及股票、行情或量化研究。", ("股票", "行情", "量化", "选股", "涨跌", "因子")),
    }
    routes: list[dict[str, Any]] = []
    for target, (route_kind, reason, words) in route_specs.items():
        if target not in allowed_targets:
            continue
        score = 0.0
        if classification == "note" and target == "knowledge": score += 0.58
        if classification == "research" and target == "crawl4ai": score += 0.66
        if classification == "idea" and target == "idea-analysis": score += 0.70
        if classification == "document" and target == "doc-factory": score += 0.70
        if classification == "alert" and target in {"server", "sub2api"}: score += 0.58
        if classification == "task" and target in {"doc-factory", "crawl4ai"}: score += 0.18
        matches = [word for word in words if word.lower() in lowered]
        # A single note can have two downstream stages (for example, research
        # first and document delivery second). Keep both candidates instead of
        # forcing the classifier to throw away the second intent.
        if target == "crawl4ai" and any(word in lowered for word in ("研究", "调研", "竞品", "资料", "网页", "抓取")):
            score += 0.36
        if target == "doc-factory" and any(word in lowered for word in ("报告", "文档", "prd", "方案", "周报", "纪要")):
            score += 0.36
        score += min(0.30, len(matches) * 0.10)
        if score >= 0.28:
            routes.append({"target_project": target, "route_kind": route_kind, "reason": reason, "confidence": round(min(score, 0.98), 3), "matched": matches[:5]})
    routes.sort(key=lambda item: item["confidence"], reverse=True)
    classification_label = {
        "task": "待办",
        "idea": "想法",
        "research": "研究",
        "document": "文档",
        "alert": "告警",
        "link": "链接",
        "note": "笔记",
    }.get(classification, classification)
    return {
        "classification": classification,
        "classification_confidence": round(confidence, 3),
        "classification_label": classification_label,
        "evidence": evidence,
        "due_at": due["due_at"],
        "due_label": due["due_label"],
        "duplicate_of": duplicate["id"],
        "duplicate_score": duplicate["score"],
        "duplicate_preview": clip(duplicate["reason"], 180) if duplicate["id"] else "",
        "next_steps": next_steps["steps"],
        "next_steps_source": next_steps["source"],
        "routes": routes[:3],
        "summary": f"判断为{classification_label}" + (f"，{due['due_label']}前处理" if due["due_label"] else ""),
        "tags": [tag.strip() for tag in re.split(r"[,，\s]+", tags or "") if tag.strip()][:12],
    }


def analyze_inbox_record(item_id: int, *, triage_run_id: str = "") -> dict[str, Any]:
    item = get_inbox_record(item_id)
    if not item:
        raise HTTPException(404, "收件箱条目不存在")
    analysis = infer_inbox_triage(item["content"], item.get("kind", "note"), item.get("tags", ""), item_id)
    now = now_iso()
    connection = db_connection()
    try:
        connection.execute("DELETE FROM inbox_route_candidates WHERE inbox_id = ? AND status = 'suggested'", (item_id,))
        existing = {
            row["target_project"]: row["status"]
            for row in connection.execute(
                "SELECT target_project, status FROM inbox_route_candidates WHERE inbox_id = ?", (item_id,)
            ).fetchall()
        }
        for route in analysis["routes"]:
            if route["target_project"] in existing:
                continue
            connection.execute(
                """INSERT INTO inbox_route_candidates
                (inbox_id, target_project, route_kind, reason, confidence, status, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'suggested', ?, ?, ?)""",
                (item_id, route["target_project"], route["route_kind"], route["reason"], route["confidence"], json.dumps({"matched": route.get("matched", [])}, ensure_ascii=False), now, now),
            )
        accepted = connection.execute(
            "SELECT COUNT(*) FROM inbox_route_candidates WHERE inbox_id = ? AND status = 'accepted'", (item_id,)
        ).fetchone()[0]
        route_status = "accepted" if accepted else "suggested" if analysis["routes"] else "none"
        connection.execute(
            """UPDATE inbox SET due_at = ?, classification = ?, classification_confidence = ?, duplicate_of = ?,
            analysis_json = ?, analyzed_at = ?, route_status = ?, triage_run_id = ?, updated_at = ? WHERE id = ?""",
            (analysis["due_at"], analysis["classification"], analysis["classification_confidence"], analysis["duplicate_of"], json.dumps(analysis, ensure_ascii=False), now, route_status, triage_run_id, now, item_id),
        )
        connection.commit()
    finally:
        connection.close()
    return get_inbox_record(item_id) or item


def triage_inbox_record(item_id: int) -> dict[str, Any]:
    item = get_inbox_record(item_id)
    if not item:
        raise HTTPException(404, "收件箱条目不存在")
    run = create_agent_run_record(
        project_id="inbox",
        kind="triage",
        title=f"整理收件箱：{clip(item['content'], 90)}",
        request={"inbox_id": item_id, "content": item["content"], "kind": item.get("kind", "note")},
        max_attempts=2,
    )
    update_agent_run_record(run["id"], status="running")
    add_agent_run_event(run["id"], "started", "收件箱 Agent 开始分类、提取截止时间、检查重复和生成交接候选。")
    try:
        result = analyze_inbox_record(item_id, triage_run_id=run["id"])
        run_result = {"inbox_id": item_id, "classification": result.get("classification"), "routes": result.get("routes", []), "duplicate_of": result.get("duplicate_of", 0), "due_at": result.get("due_at", ""), "next_steps": result.get("next_steps", []), "next_steps_source": result.get("next_steps_source", "")}
        updated = update_agent_run_record(run["id"], status="succeeded", result=run_result, error="") or run
        add_agent_run_event(run["id"], "succeeded", "收件箱 Agent 已完成整理。", level="success", metadata=run_result)
        # 低风险自动归档：高置信度、纯记录、无截止时间、无交接路由、无重复的
        # 条目直接完成，避免琐碎记录长期占用待办；其余保留待人工处理。
        confidence = float(result.get("classification_confidence") or 0)
        if (
            confidence >= 0.9
            and str(result.get("classification") or "note") == "note"
            and not result.get("due_at")
            and not result.get("routes")
            and not result.get("duplicate_of")
        ):
            connection = db_connection()
            try:
                connection.execute(
                    "UPDATE inbox SET status = 'done', metadata = json_set(COALESCE(metadata, '{}'), '$.auto_archived', json_object('reason', 'high_confidence_note', 'confidence', ?, 'at', ?)) WHERE id = ?",
                    (confidence, now_iso(), item_id),
                )
                connection.commit()
            finally:
                connection.close()
            add_agent_run_event(run["id"], "auto_archived", "高置信度纯记录，已自动完成归档。", level="info", metadata={"confidence": confidence})
            result["auto_archived"] = True
        return result
    except Exception as exc:
        update_agent_run_record(run["id"], status="failed", error=str(exc))
        add_agent_run_event(run["id"], "failed", f"收件箱整理失败：{exc}", level="error")
        raise

__all__ = ["inbox_row", "inbox_route_candidate_row", "list_inbox_route_candidates", "get_inbox_record", "get_inbox_route_candidate", "update_inbox_route_candidate", "list_inbox", "inbox_summary", "create_inbox_record", "normalized_inbox_text", "extract_inbox_due", "extract_inbox_next_steps", "inbox_duplicate_match", "inbox_learned_classification", "infer_inbox_triage", "analyze_inbox_record", "triage_inbox_record"]
