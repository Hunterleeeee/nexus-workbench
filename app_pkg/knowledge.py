"""知识库 + Obsidian 领域。

拆自 app.py（2026-08-14 第十六批）。包含:
- 知识库核心: knowledge_files/search、语义向量(embedding/vectors)、混合检索
- Obsidian: 索引/搜索/related/MOC、冲突检测与解决、retrieval-evaluation
- 笔记 CRUD 与知识库路由、draft 同步与回放
仍在 app.py 的领域函数（artifacts/work-items/notifications/inbox 路由等）经 _app_call 运行时转发。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from typing import Any

from fastapi import File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .core import (
    clip,
    decode_json_column,
    log,
    now_iso,
)
from .db import db_connection
from .inbox import get_inbox_record, list_inbox
from .instance import app
from .notifications import create_notification_record


def _KNOWLEDGE_DIR() -> Path:
    """运行时读 app.KNOWLEDGE_DIR——测试 patch app.KNOWLEDGE_DIR 时生效。"""
    import app as _app

    return _app.KNOWLEDGE_DIR


def _OBSIDIAN_VAULT_DIR() -> Path:
    """运行时读 app.OBSIDIAN_VAULT_DIR——测试 patch 时生效。"""
    import app as _app

    return _app.OBSIDIAN_VAULT_DIR


def _OUTPUTS_DIR() -> Path:
    """运行时读 app.OUTPUTS_DIR——测试 patch 时生效。"""
    import app as _app

    return _app.OUTPUTS_DIR


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


_knowledge_files_cache: dict[str, Any] = {"signature": None, "files": []}


def _knowledge_dir_signature() -> tuple[Any, ...]:
    """Cheap fingerprint of the vault: every directory's mtime.

    Creating, renaming or deleting a note bumps its parent directory's mtime, so
    this catches the changes that alter the *file list*.  Editing a note's
    contents does not change the list, which is all this cache holds.
    """
    try:
        stamps = [_KNOWLEDGE_DIR().stat().st_mtime_ns]
    except OSError:
        return ()
    for directory in _KNOWLEDGE_DIR().rglob("*"):
        try:
            if directory.is_dir():
                stamps.append(directory.stat().st_mtime_ns)
        except OSError:
            continue
    return tuple(stamps)


def knowledge_files() -> list[Path]:
    """List vault notes, newest first, without re-walking the tree every call.

    The home page calls this on every render.  A full ``rglob`` plus a ``stat``
    per note is wasted work when nothing was added or removed since last time.
    """
    signature = _app_call('_knowledge_dir_signature', )
    if _knowledge_files_cache["signature"] == signature:
        return list(_knowledge_files_cache["files"])
    files = [
        path
        for path in sorted(_KNOWLEDGE_DIR().rglob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
        if ".trash" not in path.parts
    ]
    _knowledge_files_cache["signature"] = signature
    _knowledge_files_cache["files"] = files
    return list(files)


def knowledge_search(query: str = "") -> list[dict[str, Any]]:
    results = []
    terms = [term for term in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", query.lower())]
    for path in _app_call('knowledge_files', ):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        haystack = f"{path.stem}\n{content}".lower()
        score = sum(haystack.count(term) for term in terms) if terms else 0
        if terms and not score:
            continue
        first_line = next((line.lstrip("# ").strip() for line in content.splitlines() if line.strip()), path.stem)
        results.append({
            "name": path.stem,
            "path": str(path.relative_to(_KNOWLEDGE_DIR())),
            "title": first_line,
            "preview": clip(content, 420),
            "chars": len(content),
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            "score": score,
        })
    results.sort(key=lambda item: (item["score"], item["updated_at"]), reverse=True)
    return results[:50]


# ---------- 语义向量检索（embedding 服务经本机反向隧道调用） ----------
EMBEDDING_URL = os.getenv("WORKBENCH_EMBEDDING_URL", "").strip()
_embedding_health: dict[str, Any] = {"ok": False, "checked_at": 0.0, "last_error": ""}


def embedding_available() -> bool:
    """embedding 服务是否可用（30 秒内缓存探测结果）。"""
    if not EMBEDDING_URL:
        return False
    now = time.time()
    if now - float(_embedding_health.get("checked_at", 0.0)) < 30:
        return bool(_embedding_health.get("ok"))
    try:
        with httpx.Client(timeout=5) as client:
            response = client.get(f"{EMBEDDING_URL}/health")
        _embedding_health["ok"] = response.status_code == 200
    except Exception as exc:  # noqa: BLE001
        _embedding_health["ok"] = False
        _embedding_health["last_error"] = str(exc)
    _embedding_health["checked_at"] = now
    return bool(_embedding_health.get("ok"))


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """调用本机 embedding 服务；失败返回 None（调用方降级关键词）。"""
    if not texts:
        return []
    try:
        with httpx.Client(timeout=40) as client:
            response = client.post(f"{EMBEDDING_URL}/embed", json={"texts": texts})
        if response.status_code != 200:
            return None
        return response.json().get("vectors")
    except Exception:  # noqa: BLE001
        return None


def _ensure_knowledge_vectors_table() -> None:
    connection = db_connection()
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS knowledge_vectors (
                path TEXT PRIMARY KEY,
                dim INTEGER NOT NULL DEFAULT 512,
                vector BLOB NOT NULL,
                text_hash TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )"""
        )
        connection.commit()
    finally:
        connection.close()


def _embedding_text_for_note(path: Path, content: str) -> str:
    title = next((line.lstrip("# ").strip() for line in content.splitlines() if line.strip()), path.stem)
    body = re.sub(r"\s+", " ", content)[:1600]
    return f"{path.stem}\n{title}\n{body}"


def ensure_knowledge_vectors(force: bool = False) -> dict[str, Any]:
    """为缺少向量（或内容变化）的知识文件生成向量，批量调 embedding 服务。"""
    if not _app_call('embedding_available', ):
        return {"ok": False, "indexed": 0, "reason": "embedding 服务不可用"}
    _app_call('_ensure_knowledge_vectors_table', )
    paths = _app_call('knowledge_files', )
    connection = db_connection()
    indexed = skipped = 0
    try:
        for path in paths:
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            text = _app_call('_embedding_text_for_note', path, content)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            row = connection.execute("SELECT text_hash FROM knowledge_vectors WHERE path = ?", (str(path),)).fetchone()
            if not force and row and row["text_hash"] == digest:
                skipped += 1
                continue
            vectors = _app_call('embed_texts', [text]) or []
            if not vectors:
                break  # embedding 服务暂不可用，停止本次索引
            vector_bytes = json.dumps(vectors[0]).encode("utf-8")
            connection.execute(
                """INSERT INTO knowledge_vectors(path, dim, vector, text_hash, updated_at)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET vector=excluded.vector, text_hash=excluded.text_hash, updated_at=excluded.updated_at""",
                (str(path), len(vectors[0]), vector_bytes, digest, now_iso()),
            )
            indexed += 1
        connection.commit()
    finally:
        connection.close()
    return {"ok": True, "indexed": indexed, "skipped": skipped, "total": len(paths)}


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def knowledge_semantic_scores(query: str) -> dict[str, float]:
    """返回 {绝对路径: 余弦相似度}。query 语义向量 vs 库内全部向量。"""
    if not _app_call('embedding_available', ):
        return {}
    query_vectors = _app_call('embed_texts', [query])
    if not query_vectors:
        return {}
    query_vector = query_vectors[0]
    _app_call('_ensure_knowledge_vectors_table', )
    connection = db_connection()
    scores: dict[str, float] = {}
    try:
        rows = connection.execute("SELECT path, vector FROM knowledge_vectors").fetchall()
        for row in rows:
            try:
                stored = json.loads(row["vector"])
            except (json.JSONDecodeError, TypeError):
                continue
            if len(stored) != len(query_vector):
                continue
            scores[str(row["path"])] = _app_call('_cosine_similarity', query_vector, stored)
    finally:
        connection.close()
    return scores


def knowledge_hybrid_search(query: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """关键词 + 语义向量混合检索；embedding 不可用时退化为纯关键词。"""
    keyword = _app_call('knowledge_search', query)
    if not query.strip() or not _app_call('embedding_available', ):
        return keyword[:limit]
    _app_call('ensure_knowledge_vectors', )
    semantic = _app_call('knowledge_semantic_scores', query)
    if not semantic:
        return keyword[:limit]
    max_sim = max(semantic.values()) or 1.0
    merged: dict[str, dict[str, Any]] = {}
    for note in keyword:
        absolute = str(_KNOWLEDGE_DIR() / note["path"])
        merged[absolute] = {**note, "__semantic": semantic.get(absolute, 0.0)}
    for path_str, sim in semantic.items():
        if path_str in merged:
            continue
        path = Path(path_str)
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        title = next((line.lstrip("# ").strip() for line in content.splitlines() if line.strip()), path.stem)
        merged[path_str] = {
            "name": path.stem,
            "path": str(path.relative_to(_KNOWLEDGE_DIR())),
            "title": title,
            "preview": clip(content, 420),
            "chars": len(content),
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            "score": 0,
            "__semantic": sim,
        }
    for item in merged.values():
        item["score"] = item.get("score", 0) + 5.0 * item.get("__semantic", 0.0) / max_sim
        item.pop("__semantic", None)
    ranked = sorted(merged.values(), key=lambda item: (item["score"], item["updated_at"]), reverse=True)
    return ranked[:limit]


def parse_obsidian_markdown(text: str, path: Path) -> dict[str, Any]:
    frontmatter: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, flags=re.DOTALL)
        if match:
            for line in match.group(1).splitlines():
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                key = key.strip()
                value = value.strip()
                if value.startswith("[") and value.endswith("]"):
                    value = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
                elif value.lower() in {"true", "false"}:
                    value = value.lower() == "true"
                else:
                    value = value.strip("'\"")
                frontmatter[key] = value
            body = text[match.end():]
    title_match = re.search(r"^#\s+(.+?)\s*$", body, flags=re.MULTILINE)
    title = (title_match.group(1).strip() if title_match else path.stem).strip()
    links = []
    seen_links: set[str] = set()
    for raw in re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", body):
        target = raw.strip()
        if target and target not in seen_links:
            seen_links.add(target)
            links.append(target)
    tags = []
    frontmatter_tags = frontmatter.get("tags", [])
    if isinstance(frontmatter_tags, str):
        frontmatter_tags = [frontmatter_tags]
    for tag in [*(frontmatter_tags if isinstance(frontmatter_tags, list) else []), *re.findall(r"(?<![\w])#([\w\u4e00-\u9fff/-]+)", body)]:
        normalized = str(tag).strip().lstrip("#")
        if normalized and normalized not in tags:
            tags.append(normalized)
    preview = re.sub(r"\s+", " ", body).strip()
    return {
        "title": title[:240],
        "frontmatter": frontmatter,
        "links": links[:80],
        "tags": tags[:80],
        "preview": clip(preview, 420),
    }


def obsidian_note_paths() -> list[Path]:
    if not _OBSIDIAN_VAULT_DIR().is_dir():
        return []
    paths = []
    for path in _OBSIDIAN_VAULT_DIR().rglob("*.md"):
        if ".obsidian" in path.parts:
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(_OBSIDIAN_VAULT_DIR().resolve())
        except (OSError, ValueError):
            continue
        paths.append(path)
    return sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True)


def obsidian_note_row(row: sqlite3.Row) -> dict[str, Any]:
    note = {key: row[key] for key in row.keys()}
    note.pop("search_text", None)
    note["frontmatter"] = decode_json_column(note.pop("frontmatter_json", "{}"))
    note["links"] = json.loads(note.pop("links_json", "[]") or "[]")
    note["tags"] = json.loads(note.pop("tags_json", "[]") or "[]")
    note["updated_at"] = datetime.fromtimestamp(float(note.get("mtime") or 0), tz=timezone.utc).isoformat() if note.get("mtime") else ""
    note["vault_path"] = str(_OBSIDIAN_VAULT_DIR())
    return note


def obsidian_backlink_count(path: str, title: str, rows: list[sqlite3.Row]) -> int:
    targets = {str(path), str(Path(path).with_suffix("")), str(Path(path).stem), str(title)}
    count = 0
    for row in rows:
        try:
            links = json.loads(row["links_json"] or "[]")
        except json.JSONDecodeError:
            links = []
        if any(str(link).strip() in targets or Path(str(link).strip()).stem in targets for link in links):
            count += 1
    return count


def obsidian_index_vault() -> dict[str, Any]:
    started = now_iso()
    paths = _app_call('obsidian_note_paths', )
    if not _OBSIDIAN_VAULT_DIR().is_dir():
        return {"ok": False, "vault_path": str(_OBSIDIAN_VAULT_DIR()), "error": "Obsidian Vault 不存在或不可读取", "indexed": 0}
    indexed_paths: set[str] = set()
    changed = 0
    connection = db_connection()
    try:
        for path in paths:
            relative = str(path.relative_to(_OBSIDIAN_VAULT_DIR()))
            indexed_paths.add(relative)
            try:
                stat = path.stat()
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parsed = _app_call('parse_obsidian_markdown', text, path)
            content_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
            previous = connection.execute("SELECT content_hash FROM obsidian_notes WHERE path = ?", (relative,)).fetchone()
            if not previous or previous["content_hash"] != content_hash:
                changed += 1
            connection.execute(
                """INSERT INTO obsidian_notes
                (path, title, folder, size, mtime, preview, search_text, frontmatter_json, links_json, tags_json, content_hash, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET title=excluded.title, folder=excluded.folder, size=excluded.size,
                mtime=excluded.mtime, preview=excluded.preview, search_text=excluded.search_text, frontmatter_json=excluded.frontmatter_json,
                links_json=excluded.links_json, tags_json=excluded.tags_json, content_hash=excluded.content_hash,
                indexed_at=excluded.indexed_at""",
                (relative, parsed["title"], str(Path(relative).parent) if Path(relative).parent != Path(".") else "", stat.st_size, stat.st_mtime, parsed["preview"], clip(text, 80_000), json.dumps(parsed["frontmatter"], ensure_ascii=False), json.dumps(parsed["links"], ensure_ascii=False), json.dumps(parsed["tags"], ensure_ascii=False), content_hash, started),
            )
        existing = {row["path"] for row in connection.execute("SELECT path FROM obsidian_notes").fetchall()}
        removed = existing - indexed_paths
        if removed:
            connection.executemany("DELETE FROM obsidian_notes WHERE path = ?", [(path,) for path in removed])
        summary = {"indexed": len(indexed_paths), "changed": changed, "removed": len(removed), "scanned_at": started}
        connection.execute("INSERT INTO obsidian_index_meta(key, value) VALUES('last_scan', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(summary, ensure_ascii=False),))
        connection.commit()
    finally:
        connection.close()
    return {"ok": True, "vault_path": str(_OBSIDIAN_VAULT_DIR()), **summary}


def obsidian_status() -> dict[str, Any]:
    connection = db_connection()
    try:
        count = int(connection.execute("SELECT COUNT(*) FROM obsidian_notes").fetchone()[0])
        latest = connection.execute("SELECT MAX(indexed_at) FROM obsidian_notes").fetchone()[0] or ""
        local_today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today_count = int(connection.execute("SELECT COUNT(*) FROM obsidian_notes WHERE mtime >= ?", (local_today,)).fetchone()[0])
        moc_count = int(connection.execute("SELECT COUNT(*) FROM obsidian_notes WHERE folder LIKE '03 MOC%' ").fetchone()[0])
        meta = connection.execute("SELECT value FROM obsidian_index_meta WHERE key = 'last_scan'").fetchone()
        last_scan = decode_json_column(meta[0]) if meta else {}
        return {"available": _OBSIDIAN_VAULT_DIR().is_dir(), "vault_path": str(_OBSIDIAN_VAULT_DIR()), "note_count": count, "today_note_count": today_count, "moc_count": moc_count, "last_indexed_at": latest, "last_scan": last_scan}
    finally:
        connection.close()


def obsidian_index_rows() -> list[sqlite3.Row]:
    connection = db_connection()
    try:
        return connection.execute("SELECT * FROM obsidian_notes ORDER BY mtime DESC LIMIT 2000").fetchall()
    finally:
        connection.close()


def obsidian_search(query: str = "", limit: int = 40, since_timestamp: float = 0) -> list[dict[str, Any]]:
    rows = [row for row in _app_call('obsidian_index_rows', ) if not since_timestamp or float(row["mtime"] or 0) >= since_timestamp]
    terms = [term for term in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", (query or "").lower())]
    ranked: list[tuple[int, float, sqlite3.Row]] = []
    for row in rows:
        haystack = f"{row['title']}\n{row['path']}\n{row['preview']}\n{row['search_text']}\n{row['tags_json']}\n{row['links_json']}".lower()
        score = sum(haystack.count(term) for term in terms) if terms else 0
        if terms and not score:
            continue
        ranked.append((score, float(row["mtime"] or 0), row))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    all_rows = rows
    results = []
    for score, _, row in ranked[: max(1, min(limit, 100))]:
        note = _app_call('obsidian_note_row', row)
        note["score"] = score
        note["backlinks"] = _app_call('obsidian_backlink_count', note["path"], note["title"], all_rows)
        results.append(note)
    return results


def obsidian_related(path: str, limit: int = 8) -> list[dict[str, Any]]:
    rows = _app_call('obsidian_index_rows', )
    target_row = next((row for row in rows if str(row["path"]) == str(path)), None)
    if not target_row:
        return []
    target = _app_call('obsidian_note_row', target_row)
    target_terms = set(re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", f"{target['title']} {target['preview']}".lower()))
    target_terms = {term for term in target_terms if len(term) >= 2}
    target_tags = {str(tag).lower() for tag in target.get("tags", [])}
    target_links = {str(link).strip() for link in target.get("links", [])}
    related: list[dict[str, Any]] = []
    for row in rows:
        if str(row["path"]) == str(path):
            continue
        note = _app_call('obsidian_note_row', row)
        note_terms = set(re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", f"{note['title']} {note['preview']}".lower()))
        shared_tags = target_tags & {str(tag).lower() for tag in note.get("tags", [])}
        shared_terms = target_terms & note_terms
        link_match = any(
            str(link).strip() in {str(target["path"]), str(Path(target["path"]).with_suffix("")), str(Path(target["path"]).stem), str(target["title"])}
            for link in note.get("links", [])
        ) or any(str(link).strip() in {str(note["path"]), str(Path(note["path"]).with_suffix("")), str(Path(note["path"]).stem), str(note["title"])} for link in target_links)
        score = len(shared_tags) * 5 + min(len(shared_terms), 5) + (8 if link_match else 0)
        if score <= 0:
            continue
        reasons = []
        if link_match:
            reasons.append("已有双链")
        if shared_tags:
            reasons.append(f"共享标签 {', '.join(sorted(shared_tags)[:2])}")
        if shared_terms:
            reasons.append("主题词相近")
        note["relation_score"] = score
        note["relation_reason"] = " · ".join(reasons)
        note["backlinks"] = _app_call('obsidian_backlink_count', note["path"], note["title"], rows)
        related.append(note)
    related.sort(key=lambda item: (item.get("relation_score", 0), item.get("backlinks", 0)), reverse=True)
    return related[: max(1, min(limit, 20))]


def obsidian_moc_suggestions() -> dict[str, Any]:
    rows = _app_call('obsidian_index_rows', )
    moc_notes = [row for row in rows if str(row["folder"] or "").startswith("03 MOC")]
    orphan_notes = []
    tag_counts: dict[str, int] = {}
    for row in rows:
        note = _app_call('obsidian_note_row', row)
        for tag in note.get("tags", []):
            tag_counts[str(tag)] = tag_counts.get(str(tag), 0) + 1
        if not str(row["folder"] or "").startswith("03 MOC") and _app_call('obsidian_backlink_count', note["path"], note["title"], rows) == 0:
            orphan_notes.append({"path": note["path"], "title": note["title"], "folder": note.get("folder", ""), "reason": "没有反向链接，可考虑补充到 MOC"})
    orphan_notes.sort(key=lambda item: item["title"])
    return {
        "moc_count": len(moc_notes),
        "orphan_count": len(orphan_notes),
        "orphan_notes": orphan_notes[:12],
        "top_tags": [{"tag": tag, "count": count} for tag, count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))[:12]],
    }


def knowledge_inbox_candidates() -> list[dict[str, Any]]:
    candidates = []
    allowed = {"note", "idea", "research", "document", "link"}
    for item in list_inbox("inbox"):
        classification = item.get("classification") or item.get("kind") or "note"
        if classification not in allowed and item.get("route_status") != "accepted":
            continue
        first_line = next((line.strip() for line in str(item.get("content", "")).splitlines() if line.strip()), "未命名内容")
        title = clip(first_line, 80)
        existing = _app_call('knowledge_search', title)[:2] if len(title) >= 2 else []
        candidates.append({
            "id": item["id"],
            "content": item.get("content", ""),
            "title": title,
            "classification": classification,
            "priority": item.get("priority", "normal"),
            "priority_label": item.get("priority_label", "普通"),
            "created_at": item.get("created_at", ""),
            "due_at": item.get("due_at", ""),
            "is_overdue": item.get("is_overdue", False),
            "route_status": item.get("route_status", "none"),
            "existing_notes": [{"title": note.get("title"), "path": note.get("path")} for note in existing],
        })
    return candidates[:30]


def sync_inbox_to_obsidian(item_id: int, *, content: str = "", title_override: str = "") -> dict[str, Any]:
    item = get_inbox_record(item_id)
    if not item:
        raise HTTPException(404, "收件箱条目不存在")
    if not _OBSIDIAN_VAULT_DIR().is_dir():
        raise HTTPException(409, "Obsidian Vault 不可用")
    for artifact in _app_call('list_artifacts', "knowledge"):
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        if artifact.get("kind") == "obsidian_inbox_note" and str(metadata.get("source_inbox_id")) == str(item_id):
            return {"synced": True, "already_exists": True, "artifact": artifact, "path": artifact.get("path", ""), "message": "这条收件箱内容已经写入 Obsidian Inbox。"}
    inbox_dir = _OBSIDIAN_VAULT_DIR() / "00 Inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    selected_content = str(content or item.get("content", "")).strip()
    if not selected_content:
        raise HTTPException(400, "写入内容不能为空")
    title = title_override.strip() or next((line.strip() for line in selected_content.splitlines() if line.strip()), f"NEXUS 收件箱 {item_id}")
    title = clip(title, 70)
    filename = _app_call('safe_filename', f"{title}-{item_id}", f"workbench-inbox-{item_id}") + ".md"
    path = inbox_dir / filename
    suffix = 2
    while path.exists():
        path = inbox_dir / f"{_app_call('safe_filename', title, f'workbench-inbox-{item_id}')}-{item_id}-{suffix}.md"
        suffix += 1
    tags = [tag.strip().lstrip("#") for tag in re.split(r"[,，\s]+", item.get("tags", "")) if tag.strip()]
    tag_slugs = [re.sub(r"[^\w\u4e00-\u9fff/-]+", "-", tag).strip("-") for tag in ["workbench/inbox", *tags]]
    tag_lines = "\n".join(f"  - {slug}" for slug in tag_slugs)
    body = (
        "---\n"
        "source: workbench\n"
        f"source_inbox_id: {item_id}\n"
        f"captured_at: {item.get('created_at', now_iso())}\n"
        "tags:\n"
        f"{tag_lines}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"> 来源：NEXUS 快速收件箱 #{item_id}\n\n"
        f"{selected_content}\n"
    )
    path.write_text(body, encoding="utf-8")
    artifact = _app_call('register_artifact_safely', 
        project_id="knowledge",
        name=path.name,
        path=str(path),
        kind="obsidian_inbox_note",
        metadata={"title": title, "source_inbox_id": item_id, "confirmed_at": now_iso(), "vault_relative_path": str(path.relative_to(_OBSIDIAN_VAULT_DIR()))},
    )
    relation = _app_call('create_relation_record', 
        from_type="inbox",
        from_id=str(item_id),
        to_type="artifact",
        to_id=str(artifact.get("id")) if artifact else "",
        relation_type="synced_to_obsidian",
        metadata={"vault_path": str(path), "source_inbox_id": item_id},
    ) if artifact else None
    try:
        create_notification_record(
            title="已写入 Obsidian Inbox",
            body=f"收件箱 #{item_id} 已写入 {path.relative_to(_OBSIDIAN_VAULT_DIR())}，原收件箱内容保留。",
            project_id="knowledge",
            kind="knowledge_sync",
            level="info",
            href="/projects/knowledge",
            event_key=f"obsidian-sync:{item_id}",
            dedupe_seconds=0,
        )
    except Exception:
        log.debug("忽略异常（sync_inbox_to_obsidian）", exc_info=True)
    return {"synced": True, "already_exists": False, "path": str(path), "vault_relative_path": str(path.relative_to(_OBSIDIAN_VAULT_DIR())), "artifact": artifact, "relation": relation, "message": "已写入 Obsidian Inbox，原收件箱内容仍保留。"}


def knowledge_draft_source_check(draft: dict[str, Any]) -> dict[str, Any]:
    """Verify that every recorded source is still readable and unchanged.

    Drafts remain safe to review when a source disappears, but writing them to
    the Vault must stop until the user can inspect the changed source.  Older
    drafts without hashes still receive the readability check and remain
    backwards compatible.
    """
    metadata = draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {}
    expected_hashes = metadata.get("source_content_hashes") if isinstance(metadata.get("source_content_hashes"), dict) else {}
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    source_ids = [int(value) for value in metadata.get("source_artifact_ids", []) if str(value).isdigit()]
    for source_id in source_ids[:30]:
        source = _app_call('get_artifact_record', source_id)
        if not source:
            checks.append({"type": "artifact", "artifact_id": source_id, "readable": False, "status": "missing", "message": "来源 Artifact 不存在"})
            failures.append(f"Artifact #{source_id} 不存在")
            continue
        source_text, source_error = _app_call('read_artifact_source', source)
        current_hash = hashlib.sha256(source_text.encode("utf-8", errors="ignore")).hexdigest() if not source_error else ""
        expected = str(expected_hashes.get(str(source_id)) or expected_hashes.get(source_id) or "")
        changed = bool(expected and current_hash and expected != current_hash)
        status = "changed" if changed else "ok" if not source_error else "unreadable"
        checks.append({"type": "artifact", "artifact_id": source_id, "name": source.get("name", ""), "readable": not bool(source_error), "status": status, "expected_hash": expected, "current_hash": current_hash, "error": source_error})
        if source_error:
            failures.append(f"Artifact #{source_id} 当前不可读")
        elif changed:
            failures.append(f"Artifact #{source_id} 内容已变化")
    expected_note_hashes = metadata.get("source_note_hashes") if isinstance(metadata.get("source_note_hashes"), dict) else {}
    for source_note in [item for item in metadata.get("source_notes", []) if isinstance(item, dict)][:30]:
        relative = str(source_note.get("path") or "").strip()
        if not relative:
            continue
        note_path = (_OBSIDIAN_VAULT_DIR() / relative).resolve()
        try:
            note_path.relative_to(_OBSIDIAN_VAULT_DIR().resolve())
        except ValueError:
            checks.append({"type": "obsidian_note", "path": relative, "readable": False, "status": "unsafe_path"})
            failures.append(f"来源笔记路径不安全：{relative}")
            continue
        try:
            note_text = note_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            checks.append({"type": "obsidian_note", "path": relative, "readable": False, "status": "unreadable", "error": str(exc)[:200]})
            failures.append(f"来源笔记不可读：{relative}")
            continue
        current_hash = hashlib.sha256(note_text.encode("utf-8", errors="ignore")).hexdigest()
        expected = str(expected_note_hashes.get(relative) or source_note.get("content_hash") or "")
        changed = bool(expected and expected != current_hash)
        checks.append({"type": "obsidian_note", "path": relative, "readable": True, "status": "changed" if changed else "ok", "expected_hash": expected, "current_hash": current_hash})
        if changed:
            failures.append(f"来源笔记内容已变化：{relative}")
    return {"blocked": bool(failures), "status": "blocked" if failures else "verified", "message": "；".join(failures) if failures else "所有已登记来源仍可读取且内容未变化", "checks": checks, "checked_at": now_iso()}


def sync_knowledge_draft_to_obsidian(artifact_id: int, *, conflict_action: str = "new") -> dict[str, Any]:
    """Copy an explicitly reviewed Workbench draft into Obsidian Inbox.

    Draft generation is intentionally separate from Vault writing.  This
    helper is the final, auditable boundary: it only accepts a registered
    knowledge Artifact, creates a backup before overwrite, and records the
    source/destination relation.  It never edits an existing Vault note by
    default.
    """
    artifact = _app_call('get_artifact_record', artifact_id)
    if not artifact or artifact.get("project_id") != "knowledge":
        raise HTTPException(404, "知识库草稿 Artifact 不存在")
    allowed_kinds = {"source_review_draft", "paragraph_selection_draft", "knowledge_note", "inbox_handoff_note", "knowledge_conflict_merge_draft", "knowledge_conflict_paragraph_resolution"}
    if artifact.get("kind") not in allowed_kinds:
        raise HTTPException(409, "这份 Artifact 不是可写入 Obsidian 的知识库草稿")
    content, error = _app_call('read_artifact_source', artifact)
    if error or not content.strip():
        raise HTTPException(409, error or "草稿没有可写入的正文")
    source_check = _app_call('knowledge_draft_source_check', artifact)
    if source_check["blocked"]:
        raise HTTPException(409, f"写入前来源校验未通过：{source_check['message']}")
    if not _OBSIDIAN_VAULT_DIR().is_dir():
        raise HTTPException(409, "Obsidian Vault 不可用")
    inbox_dir = _OBSIDIAN_VAULT_DIR() / "00 Inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
    title = str(metadata.get("title") or Path(str(artifact.get("name") or "Workbench 草稿")).stem or "Workbench 草稿").strip()
    base = _app_call('safe_filename', title, f"workbench-draft-{artifact_id}")
    path = inbox_dir / f"{base}.md"
    backup = ""
    if path.exists():
        if conflict_action == "skip":
            return {"synced": False, "skipped": True, "path": str(path), "message": "目标笔记已存在，按选择跳过写入。", "source_artifact": artifact}
        if conflict_action == "overwrite":
            backup_path = path.with_suffix(path.suffix + f".workbench-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.bak")
            backup_index = 2
            while backup_path.exists():
                backup_path = path.with_suffix(path.suffix + f".workbench-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{backup_index}.bak")
                backup_index += 1
            shutil.copy2(path, backup_path)
            backup = str(backup_path)
        else:
            suffix = 2
            while path.exists():
                path = inbox_dir / f"{base}-{suffix}.md"
                suffix += 1
    provenance = f"> 来源：Workbench 知识库草稿 Artifact #{artifact_id} · 原文件：{artifact.get('name', '未命名')}\n\n"
    body = content.strip()
    if provenance not in body:
        body = f"{provenance}{body}"
    path.write_text(body.rstrip() + "\n", encoding="utf-8")
    confirmed_at = now_iso()
    destination = _app_call('register_artifact_safely', 
        project_id="knowledge",
        name=path.name,
        path=str(path),
        kind="obsidian_reviewed_draft",
        metadata={"source_artifact_id": artifact_id, "confirmed_at": confirmed_at, "conflict_action": conflict_action, "backup": backup, "vault_relative_path": str(path.relative_to(_OBSIDIAN_VAULT_DIR())), "source_check": source_check},
    )
    relation = _app_call('create_relation_record', 
        from_type="artifact", from_id=str(artifact_id), to_type="artifact", to_id=str(destination.get("id")) if destination else "", relation_type="draft_to_obsidian", metadata={"backup": backup, "conflict_action": conflict_action}
    ) if destination else None
    _app_call('update_artifact_metadata', artifact_id, {"obsidian_sync": {"artifact_id": destination.get("id") if destination else None, "path": str(path), "confirmed_at": confirmed_at, "backup": backup, "source_check": source_check}})
    return {"synced": True, "skipped": False, "path": str(path), "vault_relative_path": str(path.relative_to(_OBSIDIAN_VAULT_DIR())), "backup": backup, "artifact": destination, "relation": relation, "source_check": source_check, "confirmed_at": confirmed_at, "message": "草稿已写入 Obsidian Inbox，来源 Artifact、校验结果和备份已保留。"}


def knowledge_draft_replay(artifact_id: int) -> dict[str, Any]:
    """Return bounded source excerpts for a draft's citation replay UI."""
    draft = _app_call('get_artifact_record', artifact_id)
    if not draft or draft.get("project_id") != "knowledge":
        raise HTTPException(404, "知识库草稿 Artifact 不存在")
    metadata = draft.get("metadata") if isinstance(draft.get("metadata"), dict) else {}
    source_ids = [int(value) for value in metadata.get("source_artifact_ids", []) if str(value).isdigit()]
    locators = [item for item in metadata.get("source_locators", []) if isinstance(item, dict)]
    sources: list[dict[str, Any]] = []
    for source_id in source_ids[:20]:
        source = _app_call('get_artifact_record', source_id)
        if not source:
            sources.append({"id": source_id, "name": "来源 Artifact 不存在", "error": "来源已被移除或不可用"})
            continue
        content, error = _app_call('read_artifact_source', source)
        if error:
            sources.append({"id": source_id, "name": source.get("name", "未命名来源"), "path": source.get("path", ""), "error": error})
            continue
        lines = content.splitlines()
        source_locators = [item for item in locators if int(item.get("artifact_id") or source_id) == source_id]
        ranges = []
        excerpts = []
        if source_locators:
            for locator in source_locators[:8]:
                start = max(1, int(locator.get("line_start") or 1))
                end = min(len(lines), max(start, int(locator.get("line_end") or start)))
                if lines:
                    ranges.append({"line_start": start, "line_end": end})
                    excerpts.append({"line_start": start, "line_end": end, "text": clip("\n".join(lines[start - 1:end]), 2_000)})
        else:
            end = min(len(lines), 24)
            excerpts.append({"line_start": 1, "line_end": end, "text": clip("\n".join(lines[:end]), 3_000)})
        source_metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        sources.append({
            "id": source_id,
            "name": source.get("name", "未命名来源"),
            "path": source.get("path", ""),
            "data_as_of": source_metadata.get("data_as_of") or source_metadata.get("checked_at") or source.get("created_at", ""),
            "ranges": ranges,
            "excerpts": excerpts,
        })
    for source_note in [item for item in metadata.get("source_notes", []) if isinstance(item, dict)][:20]:
        relative = str(source_note.get("path") or "").strip()
        note_path = (_OBSIDIAN_VAULT_DIR() / relative).resolve()
        try:
            note_path.relative_to(_OBSIDIAN_VAULT_DIR().resolve())
        except ValueError:
            continue
        if not note_path.is_file():
            sources.append({"name": relative or "Obsidian 来源", "path": relative, "error": "来源笔记当前不可读取"})
            continue
        try:
            note_lines = note_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError as exc:
            sources.append({"name": relative or "Obsidian 来源", "path": relative, "error": str(exc)[:200]})
            continue
        start = max(1, int(source_note.get("line_start") or 1))
        end = min(len(note_lines), max(start, int(source_note.get("line_end") or start)))
        sources.append({
            "name": relative or "Obsidian 来源",
            "path": relative,
            "excerpts": [{"line_start": start, "line_end": end, "text": clip("\n".join(note_lines[start - 1:end]), 2_000)}] if note_lines else [],
            "data_as_of": "",
        })
    return {
        "draft": {"id": artifact_id, "name": draft.get("name", ""), "kind": draft.get("kind", ""), "metadata": metadata},
        "sources": sources,
        "source_check": _app_call('knowledge_draft_source_check', draft),
        "policy": "回放只读取已登记来源的有限片段和行号；不会改写来源，也不会把完整材料重新发送给外部服务。",
    }


async def extract_upload_text(upload: UploadFile) -> tuple[str, str]:
    filename = upload.filename or "未命名文件"
    raw = await upload.read()
    try:
        # MinerU can run for minutes on a scanned PDF; never on the event loop.
        text = await asyncio.to_thread(_app_call, 'extract_document_bytes', raw, filename)
        return text, filename
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def write_knowledge_note(title: str, content: str, *, metadata: dict[str, Any] | None = None, artifact_kind: str = "knowledge_note") -> dict[str, Any]:
    filename = _app_call('safe_filename', title, "未命名笔记") + ".md"
    path = _KNOWLEDGE_DIR() / filename
    if path.exists():
        filename = f"{_app_call('safe_filename', title, '未命名笔记')}-{datetime.now().strftime('%H%M%S')}.md"
        path = _KNOWLEDGE_DIR() / filename
    body = content.strip()
    if not body.startswith("#"):
        body = f"# {title.strip() or '未命名笔记'}\n\n{body}"
    path.write_text(body.rstrip() + "\n", encoding="utf-8")
    artifact = _app_call('register_artifact_safely', 
        project_id="knowledge",
        name=path.name,
        path=str(path),
        kind=artifact_kind,
        metadata={"title": title.strip() or "未命名笔记", **(metadata or {})},
    )
    return {"name": path.stem, "path": str(path.relative_to(_KNOWLEDGE_DIR())), "title": title, "artifact": artifact}

class KnowledgeNoteUpdateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(min_length=1, max_length=100_000)
    title: str = Field(default="", max_length=200)

class ObsidianInboxSyncRequest(BaseModel):
    confirmed: bool = False
    content: str = Field(default="", max_length=20_000)
    title: str = Field(default="", max_length=240)


class ObsidianInboxBatchSyncRequest(BaseModel):
    item_ids: list[int] = Field(default_factory=list, min_length=1, max_length=30)
    confirmed: bool = False

class KnowledgeDraftApplyRequest(BaseModel):
    confirmed: bool = False
    conflict_action: str = Field(default="new", pattern="^(new|skip|overwrite)$")

@app.get("/api/knowledge")
async def get_knowledge(q: str = "", vector: int = 0) -> dict[str, Any]:
    if int(vector or 0) and _app_call('embedding_available', ):
        notes = await asyncio.to_thread(_app_call, 'knowledge_hybrid_search', q)
        return {"root": str(_KNOWLEDGE_DIR()), "notes": notes, "mode": "hybrid-vector"}
    return {"root": str(_KNOWLEDGE_DIR()), "notes": _app_call('knowledge_search', q), "mode": "keyword"}


def _resolve_knowledge_path(relative_path: str) -> Path:
    """把用户给的相对路径安全地解析到知识库目录内。

    知识库目录内是按用户给的路径读写文件的唯一入口，不做限制就是
    任意文件读写。越界（..、绝对路径、软链逃逸）一律按不存在处理。
    """
    candidate = str(relative_path or "").strip().lstrip("/")
    if not candidate:
        raise HTTPException(400, "缺少笔记路径")
    root = _KNOWLEDGE_DIR().resolve()
    target = (root / candidate).resolve()
    if not str(target).startswith(str(root)) or not target.is_file() or target.suffix.lower() != ".md":
        log.warning("拒绝越界的知识库路径：%s", candidate)
        raise HTTPException(404, "笔记不存在")
    return target


def read_knowledge_note(relative_path: str) -> dict[str, Any]:
    """读取一篇笔记的全文。路径必须落在知识库目录内。"""
    target = _app_call('_resolve_knowledge_path', relative_path)
    root = _KNOWLEDGE_DIR().resolve()
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HTTPException(500, f"读取失败：{clip(str(exc), 120)}") from exc
    return {
        "path": str(target.relative_to(root)),
        "name": target.stem,
        "title": next((line.lstrip("# ").strip() for line in content.splitlines() if line.strip()), target.stem),
        "content": content,
        "chars": len(content),
        "updated_at": datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc).isoformat(),
    }


def update_knowledge_note(path: str, content: str, title: str = "") -> dict[str, Any]:
    """编辑一篇笔记：正文全文替换；可选 title 生成/替换首行标题。"""
    target = _app_call('_resolve_knowledge_path', path)
    body = content.strip()
    title = title.strip()
    original = target.read_text(encoding="utf-8")
    original_title = next((line.lstrip("# ").strip() for line in original.splitlines() if line.strip()), target.stem)
    effective_title = title or original_title
    lines = body.split("\n")
    if lines and lines[0].lstrip().startswith("#"):
        first = lines[0]
        indent = first[: len(first) - len(first.lstrip())]
        lines[0] = f"{indent}# {effective_title}"
        body = "\n".join(lines)
    else:
        body = f"# {effective_title}\n\n{body}"
    target.write_text(body.rstrip() + "\n", encoding="utf-8")
    new_title = title or next((line.lstrip("# ").strip() for line in body.splitlines() if line.strip()), target.stem)
    return {
        "name": target.stem,
        "path": str(target.relative_to(_KNOWLEDGE_DIR().resolve())),
        "title": new_title,
        "chars": len(body),
        "updated_at": datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc).isoformat(),
    }


def delete_knowledge_note(path: str) -> dict[str, Any]:
    """删除一篇笔记：移入知识库 .trash 回收站目录，不物理删除。"""
    target = _app_call('_resolve_knowledge_path', path)
    root = _KNOWLEDGE_DIR().resolve()
    trash_dir = root / ".trash"
    trash_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = trash_dir / f"{stamp}-{target.name}"
    if dest.exists():
        dest = trash_dir / f"{stamp}-{int(time.time() * 1000) % 100000}-{target.name}"
    target.rename(dest)
    return {
        "ok": True,
        "path": str(target.relative_to(root)),
        "trash_path": str(dest.relative_to(root)),
        "message": f"已移入回收站：.trash/{dest.name}",
    }


@app.get("/api/knowledge/note")
def get_knowledge_note(path: str = "") -> dict[str, Any]:
    return {"note": _app_call('read_knowledge_note', path)}


@app.put("/api/knowledge/note")
def update_knowledge_note_api(request: KnowledgeNoteUpdateRequest) -> dict[str, Any]:
    return {"note": _app_call('update_knowledge_note', request.path, request.content, request.title)}


@app.delete("/api/knowledge/note")
def delete_knowledge_note_api(path: str = "") -> dict[str, Any]:
    return _app_call('delete_knowledge_note', path)


@app.get("/api/knowledge/evaluation")
def knowledge_evaluation() -> dict[str, Any]:
    """检索命中率评估：用一组内置查询对比关键词与语义检索的覆盖。

    只读评估，不修改任何笔记；结果用于判断向量检索是否真的带来了
    “词面零重合也能命中”的价值，而不是展示技术指标。
    """
    queries = [
        {"query": "行情", "note": "行情相关研究"},
        {"query": "股票波动", "note": "量化/波动研究"},
        {"query": "股票波动大适合观察", "note": "纯语义命中测试（词面零重合）"},
        {"query": "AI 热点机会", "note": "热点/机会研究"},
        {"query": "独立开发者", "note": "CID/独立开发"},
        {"query": "服务器部署", "note": "运维/部署"},
    ]
    keyword_counts: list[dict[str, Any]] = []
    semantic_extra: list[dict[str, Any]] = []
    embedding_on = _app_call('embedding_available', )
    for item in queries:
        q = item["query"]
        keyword_hits = _app_call('knowledge_search', q)[:8]
        keyword_titles = {note["path"] for note in keyword_hits}
        hybrid_hits = _app_call('knowledge_hybrid_search', q)[:8] if embedding_on else []
        hybrid_paths = {note["path"] for note in hybrid_hits}
        extra = [note for note in hybrid_hits if note["path"] not in keyword_titles][:5]
        keyword_counts.append({
            "query": q,
            "note": item["note"],
            "keyword_hits": len(keyword_hits),
            "hybrid_hits": len(hybrid_hits),
            "semantic_extra": len(extra),
            "top": [{"title": note.get("title") or note.get("name"), "path": note.get("path")} for note in keyword_hits[:2]],
        })
        if extra:
            semantic_extra.append({"query": q, "extra": [{"title": note.get("title") or note.get("name"), "path": note.get("path")} for note in extra]})
    return {
        "embedding_available": embedding_on,
        "total_keyword_hits": sum(item["keyword_hits"] for item in keyword_counts),
        "total_hybrid_hits": sum(item["hybrid_hits"] for item in keyword_counts),
        "total_semantic_extra": sum(item["semantic_extra"] for item in keyword_counts),
        "queries": keyword_counts,
        "semantic_extra_results": semantic_extra,
        "summary": f"向量检索{'已启用' if embedding_on else '未启用（退化为关键词）'}；6 条内置查询中，语义检索比关键词多命中 {sum(item['semantic_extra'] for item in keyword_counts)} 条词面不重合的结果。",
    }


@app.post("/api/knowledge/reindex-vectors")
async def reindex_knowledge_vectors(force: int = 0) -> dict[str, Any]:
    """手动触发向量索引：force=1 全量重建，否则只补新增/变化文件。"""
    return await asyncio.to_thread(_app_call, 'ensure_knowledge_vectors', bool(int(force or 0)))


@app.get("/api/obsidian")
async def get_obsidian(q: str = "", limit: int = 40, scope: str = "all") -> dict[str, Any]:
    if scope not in {"all", "today"}:
        raise HTTPException(400, "不支持的 Obsidian 时间范围")
    since = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() if scope == "today" else 0
    if q.strip():
        notes = [item for item in await asyncio.to_thread(_app_call, 'obsidian_semantic_results', q, max(1, min(limit * 2, 100))) if not since or float(item.get("mtime") or 0) >= since][: max(1, min(limit, 100))]
        mode = "local-hybrid-bm25+hash-vector"
    else:
        notes = await asyncio.to_thread(_app_call, 'obsidian_search', q, limit=limit, since_timestamp=since)
        mode = "local-index"
    return {"status": _app_call('obsidian_status', ), "scope": scope, "notes": notes, "mode": mode}


@app.get("/api/obsidian/related")
def get_obsidian_related(path: str, limit: int = 8) -> dict[str, Any]:
    return {"path": path, "notes": _app_call('obsidian_related', path, limit=limit)}


@app.get("/api/obsidian/moc-suggestions")
def get_obsidian_moc_suggestions() -> dict[str, Any]:
    return _app_call('obsidian_moc_suggestions', )


@app.post("/api/obsidian/inbox/{item_id}/sync")
def sync_obsidian_inbox(item_id: int, request: ObsidianInboxSyncRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(400, "写入 Obsidian Inbox 前需要明确确认")
    return _app_call('sync_inbox_to_obsidian', item_id, content=request.content, title_override=request.title)


@app.post("/api/obsidian/inbox/batch-sync")
def batch_sync_obsidian_inbox(request: ObsidianInboxBatchSyncRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(400, "批量写入 Obsidian Inbox 前需要明确确认")
    item_ids = list(dict.fromkeys(int(item_id) for item_id in request.item_ids))
    results: list[dict[str, Any]] = []
    for item_id in item_ids:
        try:
            results.append({"id": item_id, "ok": True, "result": _app_call('sync_inbox_to_obsidian', item_id)})
        except HTTPException as exc:
            results.append({"id": item_id, "ok": False, "error": str(exc.detail)})
        except Exception as exc:
            results.append({"id": item_id, "ok": False, "error": str(exc)})
    succeeded = sum(1 for result in results if result.get("ok"))
    return {
        "ok": succeeded == len(results),
        "results": results,
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "message": f"已处理 {len(results)} 条，成功 {succeeded} 条。",
    }


@app.post("/api/obsidian/index")
def index_obsidian() -> dict[str, Any]:
    return {"status": _app_call('obsidian_index_vault', )}




@app.get("/api/knowledge/inbox-candidates")
def get_knowledge_inbox_candidates() -> dict[str, Any]:
    return {"candidates": _app_call('knowledge_inbox_candidates', ), "policy": "只在用户点击确认后写入 Obsidian Inbox；原收件箱内容保留。"}

def knowledge_tokens(value: str) -> set[str]:
    tokens = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_]{2,}", str(value or "").lower()))
    expanded: set[str] = set(tokens)
    for token in tokens:
        if len(token) >= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", token):
            expanded.update(token[index:index + 2] for index in range(len(token) - 1))
    return expanded


def knowledge_vector_features(value: str) -> set[str]:
    """Build dependency-free local features for a reproducible vector index.

    This is deliberately not marketed as a pretrained semantic model. It is a
    stable feature-hash vector that complements the existing IDF ranking with
    Chinese character n-grams and ASCII terms, so the index remains rebuildable
    offline and does not send Vault content to a remote embedding service.
    """
    text = str(value or "").lower()
    features = {f"t:{token}" for token in _app_call('knowledge_tokens', text)}
    for block in re.findall(r"[\u4e00-\u9fff]+", text):
        features.update(f"c:{block[index:index + 2]}" for index in range(max(0, len(block) - 1)))
    return features


def knowledge_hash_vector(features: set[str], dimension: int = 192) -> list[float]:
    vector = [0.0] * dimension
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8", errors="replace"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimension
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    magnitude = math.sqrt(sum(value * value for value in vector))
    return [value / magnitude for value in vector] if magnitude else vector


def knowledge_vector_similarity(left: list[float], right: list[float]) -> float:
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right))))


def obsidian_semantic_results(query: str, limit: int = 20) -> list[dict[str, Any]]:
    query_tokens = _app_call('knowledge_tokens', query)
    if not query_tokens:
        return _app_call('obsidian_search', "", limit=limit)
    rows = _app_call('obsidian_index_rows', )
    query_vector = _app_call('knowledge_hash_vector', _app_call('knowledge_vector_features', query))
    # 真语义向量：embedding 服务可用时，query 现场 embed、文档向量查
    # knowledge_vectors 表（复用工作区索引），替代词法哈希向量提升召回；
    # 服务不可用或文档未索引时自动回退哈希向量（保持原行为）。
    semantic_query_vector: list[float] | None = None
    semantic_vectors: dict[str, list[float]] = {}
    if _app_call('embedding_available', ):
        query_vectors = _app_call('embed_texts', [query])
        if query_vectors:
            semantic_query_vector = query_vectors[0]
            _app_call('_ensure_knowledge_vectors_table', )
            connection = db_connection()
            try:
                for row in connection.execute("SELECT path, vector FROM knowledge_vectors").fetchall():
                    try:
                        semantic_vectors[str(row["path"])] = json.loads(row["vector"])
                    except (json.JSONDecodeError, TypeError):
                        continue
            finally:
                connection.close()
    # Local hybrid retrieval: BM25-like IDF + field boosts + a deterministic
    # feature-hash vector. It remains fully offline and reproducible; the
    # vector is explicitly a lexical feature index, not a cloud embedding.
    documents = []
    document_frequency: dict[str, int] = {}
    for row in rows:
        note = _app_call('obsidian_note_row', row)
        text = f"{note.get('title', '')} {note.get('preview', '')} {' '.join(note.get('tags', []))} {' '.join(note.get('links', []))} {row['search_text']}".lower()
        tokens = _app_call('knowledge_tokens', text)
        features = _app_call('knowledge_vector_features', text)
        absolute = str(_OBSIDIAN_VAULT_DIR() / str(note.get("path") or ""))
        document_vector = semantic_vectors.get(absolute) if semantic_query_vector else None
        documents.append((row, note, text, tokens, features, _app_call('knowledge_hash_vector', features), document_vector, absolute))
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    total_documents = max(1, len(documents))
    scored = []
    for row, note, text, note_tokens, _features, document_vector, semantic_doc_vector, absolute in documents:
        overlap = query_tokens.intersection(note_tokens)
        if semantic_query_vector and semantic_doc_vector:
            vector_score = _app_call('_cosine_similarity', semantic_query_vector, semantic_doc_vector)
        else:
            vector_score = _app_call('knowledge_vector_similarity', query_vector, document_vector)
        if not overlap and vector_score < 0.08:
            continue
        bm25_score = 0.0
        title_text = str(note.get("title") or "").lower()
        tags_text = " ".join(str(tag) for tag in note.get("tags", [])).lower()
        for token in overlap:
            idf = 1 + math.log((total_documents + 1) / (document_frequency.get(token, 0) + 1))
            tf = min(4, text.count(token))
            boost = 2.4 if token in title_text else 1.5 if token in tags_text else 1.0
            bm25_score += idf * tf * boost
        bm25_score = bm25_score / max(1.0, math.sqrt(len(note_tokens)))
        # Keep the lexical score dominant; vector features only improve recall
        # for related wording and never override a strong exact match.
        semantic_score = bm25_score + vector_score * 1.25
        path = _OBSIDIAN_VAULT_DIR() / str(note.get("path") or "")
        locators = []
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for number, line in enumerate(lines, start=1):
                if any(token in line.lower() for token in query_tokens):
                    locators.append({"line": number, "text": clip(line.strip(), 180)})
                    if len(locators) >= 4:
                        break
        except OSError:
            pass
        scored.append({
            **note,
            "semantic_score": round(semantic_score, 4),
            "bm25_score": round(bm25_score, 4),
            "vector_score": round(vector_score, 4),
            "matched_tokens": sorted(overlap)[:12],
            "citations": locators,
            "retrieval": "local-hybrid-bm25+hash-vector",
        })
    return sorted(scored, key=lambda item: (-item["semantic_score"], -float(item.get("mtime") or 0)))[:limit]


def obsidian_conflict_key(left: dict[str, Any], right: dict[str, Any]) -> str:
    values = sorted(
        [
            f"{left.get('path', '')}|{left.get('content_hash', '')}",
            f"{right.get('path', '')}|{right.get('content_hash', '')}",
        ]
    )
    return hashlib.sha256("\n".join(values).encode("utf-8", errors="replace")).hexdigest()[:24]


def list_knowledge_conflict_resolutions() -> dict[str, dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute("SELECT * FROM knowledge_conflict_resolutions ORDER BY updated_at DESC").fetchall()
        return {str(row["conflict_key"]): dict(row) for row in rows}
    finally:
        connection.close()


def obsidian_conflict_paragraph_key(conflict_key: str, paragraph: dict[str, Any]) -> str:
    """Return a stable ID for one pair of line-addressable conflict evidence."""
    left = paragraph.get("left") if isinstance(paragraph.get("left"), dict) else {}
    right = paragraph.get("right") if isinstance(paragraph.get("right"), dict) else {}
    material = {
        "conflict_key": str(conflict_key or ""),
        "left_path": str(left.get("path") or ""),
        "left_line_start": int(left.get("line_start") or 0),
        "left_line_end": int(left.get("line_end") or 0),
        "left_text": str(left.get("text") or ""),
        "right_path": str(right.get("path") or ""),
        "right_line_start": int(right.get("line_start") or 0),
        "right_line_end": int(right.get("line_end") or 0),
        "right_text": str(right.get("text") or ""),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()[:24]


def list_knowledge_conflict_paragraph_resolutions(conflict_key: str | None = None) -> dict[str, dict[str, Any]]:
    connection = db_connection()
    try:
        if conflict_key:
            rows = connection.execute(
                "SELECT * FROM knowledge_conflict_paragraph_resolutions WHERE conflict_key = ? ORDER BY updated_at DESC",
                (str(conflict_key),),
            ).fetchall()
            return {str(row["paragraph_key"]): dict(row) for row in rows}
        rows = connection.execute(
            "SELECT * FROM knowledge_conflict_paragraph_resolutions ORDER BY updated_at DESC"
        ).fetchall()
        return {f"{row['conflict_key']}:{row['paragraph_key']}": dict(row) for row in rows}
    finally:
        connection.close()


def save_knowledge_conflict_paragraph_resolution(
    conflict: dict[str, Any], paragraph: dict[str, Any], action: str, note: str = ""
) -> dict[str, Any]:
    """Persist a paragraph decision while keeping both Vault notes untouched."""
    if action not in {"keep_left", "keep_right", "merge", "dismiss"}:
        raise ValueError("不支持的段落处理动作")
    conflict_key = str(conflict.get("conflict_key") or "").strip()
    paragraph_key = str(paragraph.get("paragraph_key") or _app_call('obsidian_conflict_paragraph_key', conflict_key, paragraph)).strip()
    left = paragraph.get("left") if isinstance(paragraph.get("left"), dict) else {}
    right = paragraph.get("right") if isinstance(paragraph.get("right"), dict) else {}
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            """INSERT INTO knowledge_conflict_paragraph_resolutions
            (conflict_key, paragraph_key, action, note, left_path, right_path,
             left_line_start, left_line_end, right_line_start, right_line_end,
             left_text, right_text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conflict_key, paragraph_key) DO UPDATE SET action=excluded.action,
              note=excluded.note, left_path=excluded.left_path, right_path=excluded.right_path,
              left_line_start=excluded.left_line_start, left_line_end=excluded.left_line_end,
              right_line_start=excluded.right_line_start, right_line_end=excluded.right_line_end,
              left_text=excluded.left_text, right_text=excluded.right_text,
              updated_at=excluded.updated_at""",
            (
                conflict_key,
                paragraph_key,
                action,
                clip(note, 2_000),
                str(left.get("path") or ""),
                str(right.get("path") or ""),
                int(left.get("line_start") or 0),
                int(left.get("line_end") or 0),
                int(right.get("line_start") or 0),
                int(right.get("line_end") or 0),
                clip(str(left.get("text") or ""), 2_000),
                clip(str(right.get("text") or ""), 2_000),
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM knowledge_conflict_paragraph_resolutions WHERE conflict_key = ? AND paragraph_key = ?",
            (conflict_key, paragraph_key),
        ).fetchone()
        return dict(row) if row else {"conflict_key": conflict_key, "paragraph_key": paragraph_key, "action": action, "note": note}
    finally:
        connection.close()


def save_knowledge_conflict_resolution(conflict: dict[str, Any], action: str, note: str = "") -> dict[str, Any]:
    """Persist a human decision while keeping the Vault itself untouched."""
    left = conflict.get("left") if isinstance(conflict.get("left"), dict) else {}
    right = conflict.get("right") if isinstance(conflict.get("right"), dict) else {}
    key = str(conflict.get("conflict_key") or "").strip()
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            """INSERT INTO knowledge_conflict_resolutions
            (conflict_key, action, note, left_path, right_path, left_hash, right_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conflict_key) DO UPDATE SET action=excluded.action, note=excluded.note,
              left_path=excluded.left_path, right_path=excluded.right_path, left_hash=excluded.left_hash,
              right_hash=excluded.right_hash, updated_at=excluded.updated_at""",
            (key, action, clip(note, 2_000), str(left.get("path") or ""), str(right.get("path") or ""), str(left.get("content_hash") or ""), str(right.get("content_hash") or ""), timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM knowledge_conflict_resolutions WHERE conflict_key = ?", (key,)).fetchone()
        return dict(row) if row else {"conflict_key": key, "action": action, "note": note}
    finally:
        connection.close()


def knowledge_conflict_draft(conflict: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any] | None:
    """Create a local review draft for merge; never write it into Obsidian."""
    if resolution.get("action") != "merge":
        return None
    left = conflict.get("left") if isinstance(conflict.get("left"), dict) else {}
    right = conflict.get("right") if isinstance(conflict.get("right"), dict) else {}
    left_path = _OBSIDIAN_VAULT_DIR() / str(left.get("path") or "")
    right_path = _OBSIDIAN_VAULT_DIR() / str(right.get("path") or "")
    try:
        left_text = left_path.read_text(encoding="utf-8", errors="ignore")
        right_text = right_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise ValueError(f"无法读取冲突笔记：{exc}") from exc
    title = f"知识冲突合并草稿 · {left.get('title') or left.get('path') or '左侧'}"
    paragraph_lines = []
    for index, pair in enumerate(conflict.get("paragraph_conflicts") or [], start=1):
        left_locator = pair.get("left") if isinstance(pair.get("left"), dict) else {}
        right_locator = pair.get("right") if isinstance(pair.get("right"), dict) else {}
        paragraph_lines.extend([
            f"### 段落冲突 {index} · {pair.get('kind_label') or '需要人工判断'}",
            f"- 左侧第 {left_locator.get('line_start', '?')}–{left_locator.get('line_end', '?')} 行：{left_locator.get('text', '')}",
            f"- 右侧第 {right_locator.get('line_start', '?')}–{right_locator.get('line_end', '?')} 行：{right_locator.get('text', '')}",
            f"- 相似度：{pair.get('similarity', '—')} · 信号：{pair.get('signal') or '主题相近'}",
            "",
        ])
    paragraph_section = "## 段落级冲突定位\n\n" + "\n".join(paragraph_lines) if paragraph_lines else ""
    body = (
        f"# {title}\n\n"
        f"> 冲突 key：{conflict.get('conflict_key')}\n"
        f"> 这是人工审阅草稿，不会自动写回 Obsidian。请核对数据时间和适用范围后再合并。\n\n"
        f"{paragraph_section}"
        f"## 左侧：{left.get('path', '')}\n\n{left_text.strip()}\n\n"
        f"## 右侧：{right.get('path', '')}\n\n{right_text.strip()}\n\n"
        f"## 合并说明\n\n{resolution.get('note') or '请在此处记录保留依据、冲突处理和最终结论。'}\n"
    )
    path = _OUTPUTS_DIR() / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_app_call('safe_filename', title, 'knowledge-conflict')}.md"
    path.write_text(body, encoding="utf-8")
    return _app_call('register_artifact_safely', project_id="knowledge", name=path.name, path=str(path), kind="knowledge_conflict_merge_draft", metadata={"title": title, "review_required": True, "conflict_key": conflict.get("conflict_key"), "left_path": left.get("path", ""), "right_path": right.get("path", ""), "resolution": resolution, "source_notes": [{"path": left.get("path", ""), "line_start": 1, "line_end": min(24, len(left_text.splitlines())), "content_hash": hashlib.sha256(left_text.encode("utf-8", errors="ignore")).hexdigest()}, {"path": right.get("path", ""), "line_start": 1, "line_end": min(24, len(right_text.splitlines())), "content_hash": hashlib.sha256(right_text.encode("utf-8", errors="ignore")).hexdigest()}], "source_note_hashes": {str(left.get("path") or ""): hashlib.sha256(left_text.encode("utf-8", errors="ignore")).hexdigest(), str(right.get("path") or ""): hashlib.sha256(right_text.encode("utf-8", errors="ignore")).hexdigest()}})


def knowledge_conflict_paragraph_draft(conflict: dict[str, Any], paragraph: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any] | None:
    """Create an auditable paragraph decision draft; never write back to Vault."""
    if resolution.get("action") not in {"keep_left", "keep_right", "merge"}:
        return None
    left = paragraph.get("left") if isinstance(paragraph.get("left"), dict) else {}
    right = paragraph.get("right") if isinstance(paragraph.get("right"), dict) else {}
    action = str(resolution.get("action"))
    action_label = {"keep_left": "保留左侧", "keep_right": "保留右侧", "merge": "合并审阅"}[action]
    if action == "keep_left":
        selected = str(left.get("text") or "")
        decision = f"保留左侧：{left.get('path', '')} 第 {left.get('line_start', '?')}–{left.get('line_end', '?')} 行"
    elif action == "keep_right":
        selected = str(right.get("text") or "")
        decision = f"保留右侧：{right.get('path', '')} 第 {right.get('line_start', '?')}–{right.get('line_end', '?')} 行"
    else:
        selected = f"### 左侧候选\n\n{left.get('text', '')}\n\n### 右侧候选\n\n{right.get('text', '')}\n\n### 人工合并区\n\n请在确认写入前整理为一段最终表述。"
        decision = "保留左右证据，等待人工合并"
    title = f"知识冲突段落 · {action_label} · {conflict.get('conflict_key', '')}-{paragraph.get('paragraph_key', '')}"
    body = (
        f"# {title}\n\n"
        f"> 这是段落级人工审阅草稿，不会自动修改 Obsidian。\n"
        f"> 冲突 key：{conflict.get('conflict_key', '')}\n"
        f"> 段落 key：{paragraph.get('paragraph_key', '')}\n\n"
        f"## 处理决定\n\n{decision}\n\n"
        f"## 段落内容\n\n{selected.strip()}\n\n"
        f"## 处理说明\n\n{resolution.get('note') or '请补充适用范围、数据时间和最终取舍依据。'}\n\n"
        f"## 来源定位\n\n"
        f"- 左侧：`{left.get('path', '')}` 第 {left.get('line_start', '?')}–{left.get('line_end', '?')} 行\n"
        f"- 右侧：`{right.get('path', '')}` 第 {right.get('line_start', '?')}–{right.get('line_end', '?')} 行\n"
    )
    path = _OUTPUTS_DIR() / f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{_app_call('safe_filename', title, 'knowledge-conflict-paragraph')}.md"
    path.write_text(body, encoding="utf-8")
    return _app_call('register_artifact_safely', 
        project_id="knowledge",
        name=path.name,
        path=str(path),
        kind="knowledge_conflict_paragraph_resolution",
        metadata={
            "title": title,
            "review_required": True,
            "conflict_key": conflict.get("conflict_key"),
            "paragraph_key": paragraph.get("paragraph_key"),
            "resolution": resolution,
            "source_notes": [
                {"path": left.get("path", ""), "line_start": left.get("line_start", 0), "line_end": left.get("line_end", 0), "content_hash": (conflict.get("left") or {}).get("content_hash", "")},
                {"path": right.get("path", ""), "line_start": right.get("line_start", 0), "line_end": right.get("line_end", 0), "content_hash": (conflict.get("right") or {}).get("content_hash", "")},
            ],
            "source_note_hashes": {str((conflict.get("left") or {}).get("path") or ""): (conflict.get("left") or {}).get("content_hash", ""), str((conflict.get("right") or {}).get("path") or ""): (conflict.get("right") or {}).get("content_hash", "")},
        },
    )


def _obsidian_paragraph_blocks(note: dict[str, Any], limit: int = 24) -> list[dict[str, Any]]:
    """Return bounded, line-addressable paragraphs for human conflict review."""
    path = _OBSIDIAN_VAULT_DIR() / str(note.get("path") or "")
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    blocks: list[dict[str, Any]] = []
    start = 0
    buffer: list[str] = []

    def flush(end_line: int) -> None:
        nonlocal start, buffer
        text = "\n".join(buffer).strip()
        if text:
            blocks.append({"line_start": start, "line_end": max(start, end_line), "text": clip(text, 500)})
        start = 0
        buffer = []

    for line_number, line in enumerate(lines, start=1):
        if line.strip():
            if not start:
                start = line_number
            buffer.append(line)
        elif start:
            flush(line_number - 1)
        if len(blocks) >= limit:
            break
    if start and len(blocks) < limit:
        flush(len(lines))
    return blocks[:limit]


def obsidian_paragraph_conflicts(left: dict[str, Any], right: dict[str, Any], contradiction_pairs: tuple[tuple[str, str], ...], conflict_key: str | None = None) -> list[dict[str, Any]]:
    """Locate bounded paragraph evidence instead of asking users to compare whole notes."""
    left_blocks = _app_call('_obsidian_paragraph_blocks', left)
    right_blocks = _app_call('_obsidian_paragraph_blocks', right)
    matches: list[dict[str, Any]] = []
    for left_block in left_blocks:
        left_tokens = _app_call('knowledge_tokens', left_block["text"])
        if len(left_tokens) < 3:
            continue
        left_text = left_block["text"].lower()
        for right_block in right_blocks:
            right_tokens = _app_call('knowledge_tokens', right_block["text"])
            overlap = len(left_tokens.intersection(right_tokens)) / max(1, len(left_tokens.union(right_tokens)))
            right_text = right_block["text"].lower()
            contradiction = next((pair for pair in contradiction_pairs if (pair[0] in left_text and pair[1] in right_text) or (pair[1] in left_text and pair[0] in right_text)), None)
            if overlap < (0.35 if contradiction else 0.72):
                continue
            match = {
                "left": left_block,
                "right": right_block,
                "similarity": round(overlap, 3),
                "kind": "confirmed_conflict" if contradiction else "possible_conflict",
                "kind_label": "明确矛盾" if contradiction else "段落相近",
                "signal": " / ".join(contradiction) if contradiction else "关键词和语义主题相近",
            }
            match["paragraph_key"] = _app_call('obsidian_conflict_paragraph_key', conflict_key or _app_call('obsidian_conflict_key', left, right), match)
            matches.append(match)
    matches.sort(key=lambda item: (item["kind"] != "confirmed_conflict", -item["similarity"]))
    return matches[:4]


def obsidian_conflict_report() -> dict[str, Any]:
    notes = [_app_call('obsidian_note_row', row) for row in _app_call('obsidian_index_rows', )]
    resolutions = _app_call('list_knowledge_conflict_resolutions', )
    exact: dict[str, list[dict[str, Any]]] = {}
    for note in notes:
        exact.setdefault(str(note.get("content_hash") or ""), []).append(note)
    duplicate_groups = [group for key, group in exact.items() if key and len(group) > 1]
    near_matches = []
    confirmed_conflicts = []
    contradiction_pairs = (("支持", "反对"), ("可以", "不能"), ("适合", "不适合"), ("有效", "无效"), ("should", "should not"), ("yes", "no"))
    for index, first in enumerate(notes):
        left = _app_call('knowledge_tokens', f"{first.get('title')} {first.get('preview')}")
        if len(left) < 3:
            continue
        for second in notes[index + 1:]:
            right = _app_call('knowledge_tokens', f"{second.get('title')} {second.get('preview')}")
            similarity = len(left.intersection(right)) / max(1, len(left.union(right)))
            if similarity >= 0.62 and first.get("content_hash") != second.get("content_hash"):
                left_text = f"{first.get('title', '')} {first.get('preview', '')}".lower()
                right_text = f"{second.get('title', '')} {second.get('preview', '')}".lower()
                contradiction = next((pair for pair in contradiction_pairs if (pair[0] in left_text and pair[1] in right_text) or (pair[1] in left_text and pair[0] in right_text)), None)
                key = _app_call('obsidian_conflict_key', first, second)
                paragraph_conflicts = _app_call('obsidian_paragraph_conflicts', first, second, contradiction_pairs, key)
                paragraph_resolutions = _app_call('list_knowledge_conflict_paragraph_resolutions', key)
                for paragraph in paragraph_conflicts:
                    paragraph["resolution"] = paragraph_resolutions.get(str(paragraph.get("paragraph_key")))
                entry = {"conflict_key": key, "left": first, "right": second, "similarity": round(similarity, 3), "kind": "confirmed_conflict" if contradiction else "possible_conflict", "resolution": resolutions.get(key), "paragraph_conflicts": paragraph_conflicts, "conflict_scope": "paragraph" if paragraph_conflicts else "note"}
                entry["paragraph_resolved_count"] = sum(1 for paragraph in paragraph_conflicts if paragraph.get("resolution"))
                if contradiction:
                    entry["contradiction_signal"] = list(contradiction)
                    confirmed_conflicts.append(entry)
                else:
                    near_matches.append(entry)
    all_conflicts = [*near_matches, *confirmed_conflicts]
    resolved_count = sum(1 for item in all_conflicts if item.get("resolution"))
    paragraph_resolved_count = sum(int(item.get("paragraph_resolved_count") or 0) for item in all_conflicts)
    return {
        "exact_duplicates": duplicate_groups,
        "possible_conflicts": near_matches[:100],
        "confirmed_conflicts": confirmed_conflicts[:100],
        "counts": {"exact": len(duplicate_groups), "possible": len(near_matches), "confirmed": len(confirmed_conflicts), "resolved": resolved_count, "paragraph_resolved": paragraph_resolved_count},
        "policy": "只提示相似或矛盾候选；解决记录只保存人工选择和来源指针，不自动改写 Vault。",
    }


def obsidian_retrieval_evaluation(sample_limit: int = 30, top_k: int = 5) -> dict[str, Any]:
    """Measure whether the hybrid index can retrieve a note from its title.

    This is a lightweight self-recall benchmark, useful for deciding whether
    an external embedding service is actually needed. It is not a relevance
    judgement and is never used as a fact-confidence score.
    """
    rows = _app_call('obsidian_index_rows', )
    samples = []
    baseline_hits = 0
    hybrid_hits = 0
    skipped_count = 0
    requested_count = min(len(rows), max(1, min(sample_limit, 100)))
    for row in rows[: max(1, min(sample_limit, 100))]:
        note = _app_call('obsidian_note_row', row)
        title = str(note.get("title") or "").strip()
        if len(_app_call('knowledge_tokens', title)) < 1:
            skipped_count += 1
            continue
        target = str(note.get("path") or "")
        baseline = _app_call('obsidian_search', title, limit=top_k)
        hybrid = _app_call('obsidian_semantic_results', title, limit=top_k)
        baseline_hit = any(str(item.get("path")) == target for item in baseline)
        hybrid_hit = any(str(item.get("path")) == target for item in hybrid)
        baseline_hits += int(baseline_hit)
        hybrid_hits += int(hybrid_hit)
        samples.append({"query": title, "target": target, "baseline_hit": baseline_hit, "hybrid_hit": hybrid_hit})
    count = len(samples)
    minimum_samples = 10
    baseline_recall = round(baseline_hits / count, 3) if count else None
    hybrid_recall = round(hybrid_hits / count, 3) if count else None
    return {
        "mode": "self-recall",
        "requested_count": requested_count,
        "sample_count": count,
        "skipped_count": skipped_count,
        "top_k": top_k,
        "baseline_recall": baseline_recall,
        "hybrid_recall": hybrid_recall,
        "hybrid_gain": round(hybrid_recall - baseline_recall, 3) if baseline_recall is not None and hybrid_recall is not None else None,
        "minimum_samples": minimum_samples,
        "sample_status": "ready" if count >= minimum_samples else "insufficient",
        "samples": samples[:20],
        "policy": "仅用于评估本地检索是否有明显瓶颈；不把自检召回率当成语义正确率。",
    }


def obsidian_moc_preview() -> dict[str, Any]:
    status = _app_call('obsidian_moc_suggestions', )
    suggestions = status.get("orphan_notes") or []
    return {"moc_count": status.get("moc_count", 0), "suggestions": suggestions, "policy": "预览不会修改 Vault；确认后才写入 MOC。"}


def apply_obsidian_moc(note_paths: list[str]) -> dict[str, Any]:
    candidates = [Path(path).expanduser().resolve() for path in note_paths[:100]]
    safe = [path for path in candidates if _OBSIDIAN_VAULT_DIR().resolve() in path.parents and path.is_file()]
    moc_paths = [path for path in _app_call('obsidian_note_paths', ) if "moc" in path.stem.lower() or "moc" in str(path.parent).lower()]
    if not moc_paths:
        raise ValueError("没有找到可维护的 MOC 文件")
    target = moc_paths[0]
    backup = target.with_suffix(target.suffix + f".workbench-{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak")
    shutil.copy2(target, backup)
    existing = target.read_text(encoding="utf-8")
    additions = []
    for path in safe:
        link = f"- [[{path.stem}]]"
        if link not in existing:
            additions.append(link)
    if additions:
        target.write_text(existing.rstrip() + "\n\n## Workbench 建议链接\n" + "\n".join(additions) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(target), "backup": str(backup), "added": len(additions), "links": additions}


class ObsidianMocApplyRequest(BaseModel):
    confirmed: bool = False
    note_paths: list[str] = Field(default_factory=list, max_length=100)


class KnowledgeSourceDraftRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    artifact_ids: list[int] = Field(default_factory=list, max_length=30)
    instruction: str = Field(default="", max_length=4_000)


class KnowledgeSelectionDraftRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    artifact_id: int
    paragraphs: list[dict[str, Any]] = Field(default_factory=list, min_length=1, max_length=30)
    instruction: str = Field(default="", max_length=4_000)


class ObsidianConflictResolutionRequest(BaseModel):
    action: str = Field(pattern="^(keep_left|keep_right|merge|dismiss)$")
    note: str = Field(default="", max_length=2_000)
    confirmed: bool = False


class ObsidianConflictParagraphResolutionRequest(BaseModel):
    action: str = Field(pattern="^(keep_left|keep_right|merge|dismiss)$")
    note: str = Field(default="", max_length=2_000)
    confirmed: bool = False

@app.get("/api/obsidian/semantic-search")
async def semantic_search_obsidian(q: str = "", limit: int = 20) -> dict[str, Any]:
    notes = await asyncio.to_thread(obsidian_semantic_results, q, max(1, min(limit, 100)))
    return {"query": q, "notes": notes, "mode": "local-hybrid-bm25+vector", "policy": "本地混合检索（关键词 + 语义向量，embedding 服务可用时）；结果带文件和行号引用，不把相似度当作事实。"}


@app.get("/api/obsidian/conflicts")
def get_obsidian_conflicts() -> dict[str, Any]:
    return _app_call('obsidian_conflict_report', )


@app.get("/api/obsidian/retrieval-evaluation")
async def get_obsidian_retrieval_evaluation(sample_limit: int = 30, top_k: int = 5) -> dict[str, Any]:
    return await asyncio.to_thread(obsidian_retrieval_evaluation, max(1, min(sample_limit, 100)), max(1, min(top_k, 20)))


@app.post("/api/obsidian/conflicts/{conflict_key}/resolve")
def resolve_obsidian_conflict(conflict_key: str, request: ObsidianConflictResolutionRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(409, "记录冲突处理前需要明确确认")
    report = _app_call('obsidian_conflict_report', )
    conflict = next((item for item in [*report.get("possible_conflicts", []), *report.get("confirmed_conflicts", [])] if str(item.get("conflict_key")) == conflict_key), None)
    if not conflict:
        raise HTTPException(404, "冲突候选不存在，可能需要先重新索引 Vault")
    resolution = _app_call('save_knowledge_conflict_resolution', conflict, request.action, request.note)
    artifact = _app_call('register_artifact_safely', project_id="knowledge", name=f"知识冲突处理 · {conflict_key}", kind="knowledge_conflict_resolution", metadata={"conflict_key": conflict_key, "action": request.action, "note": request.note, "left_path": conflict.get("left", {}).get("path", ""), "right_path": conflict.get("right", {}).get("path", "")})
    relations = []
    if artifact:
        for side, label in ((conflict.get("left") or {}, "left"), (conflict.get("right") or {}, "right")):
            if side.get("path"):
                relations.append(_app_call('create_relation_record', from_type="artifact", from_id=str(artifact.get("id")), to_type="obsidian_note", to_id=str(side.get("path")), relation_type="conflict_resolution_source", metadata={"side": label, "action": request.action, "conflict_key": conflict_key}))
    draft = _app_call('knowledge_conflict_draft', conflict, resolution)
    if draft and artifact and draft.get("id"):
        relations.append(_app_call('create_relation_record', from_type="artifact", from_id=str(artifact.get("id")), to_type="artifact", to_id=str(draft.get("id")), relation_type="conflict_to_merge_draft", metadata={"conflict_key": conflict_key}))
    return {"ok": True, "resolution": resolution, "artifact": artifact, "draft": draft, "relations": relations, "message": "已记录人工处理决定；原始 Obsidian 笔记未被改写。"}


@app.post("/api/obsidian/conflicts/{conflict_key}/paragraphs/{paragraph_key}/resolve")
def resolve_obsidian_conflict_paragraph(conflict_key: str, paragraph_key: str, request: ObsidianConflictParagraphResolutionRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(409, "记录段落冲突处理前需要明确确认")
    report = _app_call('obsidian_conflict_report', )
    conflict = next((item for item in [*report.get("possible_conflicts", []), *report.get("confirmed_conflicts", [])] if str(item.get("conflict_key")) == conflict_key), None)
    if not conflict:
        raise HTTPException(404, "冲突候选不存在，可能需要先重新索引 Vault")
    paragraph = next((item for item in conflict.get("paragraph_conflicts", []) if str(item.get("paragraph_key")) == paragraph_key), None)
    if not paragraph:
        raise HTTPException(404, "段落冲突证据不存在，可能需要先重新索引 Vault")
    resolution = _app_call('save_knowledge_conflict_paragraph_resolution', conflict, paragraph, request.action, request.note)
    artifact = _app_call('register_artifact_safely', 
        project_id="knowledge",
        name=f"知识冲突段落处理 · {paragraph_key}",
        kind="knowledge_conflict_paragraph_resolution_record",
        metadata={
            "title": f"知识冲突段落处理 · {paragraph_key}",
            "review_required": True,
            "conflict_key": conflict_key,
            "paragraph_key": paragraph_key,
            "action": request.action,
            "note": request.note,
            "source_notes": [
                {"path": (paragraph.get("left") or {}).get("path", ""), "line_start": (paragraph.get("left") or {}).get("line_start", 0), "line_end": (paragraph.get("left") or {}).get("line_end", 0)},
                {"path": (paragraph.get("right") or {}).get("path", ""), "line_start": (paragraph.get("right") or {}).get("line_start", 0), "line_end": (paragraph.get("right") or {}).get("line_end", 0)},
            ],
        },
    )
    draft = _app_call('knowledge_conflict_paragraph_draft', conflict, paragraph, resolution)
    relations = []
    for side, label in ((paragraph.get("left") or {}, "left"), (paragraph.get("right") or {}, "right")):
        if side.get("path") and artifact:
            relations.append(_app_call('create_relation_record', from_type="artifact", from_id=str(artifact.get("id")), to_type="obsidian_note", to_id=str(side.get("path")), relation_type="conflict_paragraph_source", metadata={"side": label, "action": request.action, "conflict_key": conflict_key, "paragraph_key": paragraph_key, "line_start": side.get("line_start"), "line_end": side.get("line_end")}))
    if draft and artifact and draft.get("id"):
        relations.append(_app_call('create_relation_record', from_type="artifact", from_id=str(artifact.get("id")), to_type="artifact", to_id=str(draft.get("id")), relation_type="conflict_paragraph_to_review_draft", metadata={"conflict_key": conflict_key, "paragraph_key": paragraph_key, "action": request.action}))
    return {"ok": True, "resolution": resolution, "artifact": artifact, "draft": draft, "relations": relations, "message": "已记录段落处理决定；原始 Obsidian 笔记未被改写。" if not draft else "已记录段落处理决定并生成可审阅草稿；原始 Obsidian 笔记未被改写。"}


@app.get("/api/obsidian/moc/preview")
def preview_obsidian_moc() -> dict[str, Any]:
    return _app_call('obsidian_moc_preview', )


@app.post("/api/obsidian/moc/apply")
def maintain_obsidian_moc(request: ObsidianMocApplyRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(409, "维护 MOC 前需要明确确认")
    try:
        result = _app_call('apply_obsidian_moc', request.note_paths)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    artifact = _app_call('register_artifact_safely', project_id="knowledge", name=Path(result["path"]).name, path=result["path"], kind="obsidian_moc_update", metadata={"backup": result["backup"], "added": result["added"]})
    return {"result": result, "artifact": artifact}


@app.post("/api/knowledge/source-draft")
def create_knowledge_source_draft(request: KnowledgeSourceDraftRequest) -> dict[str, Any]:
    sources = []
    for artifact_id in request.artifact_ids:
        artifact = _app_call('get_artifact_record', artifact_id)
        if not artifact:
            continue
        content, error = _app_call('read_artifact_source', artifact)
        if not error:
            sources.append({"artifact": artifact, "content": clip(content, 8_000)})
    if not sources:
        raise HTTPException(400, "没有可读取的来源 Artifact")
    body = f"# {request.title.strip()}\n\n> 这是带来源的可审阅草稿，写入 Obsidian 前需要人工确认。\n\n"
    if request.instruction:
        body += f"## 加工要求\n\n{request.instruction.strip()}\n\n"
    body += "## 来源材料\n\n" + "\n\n".join(f"### 来源 Artifact #{item['artifact'].get('id')} · {item['artifact'].get('name')}\n\n{item['content']}" for item in sources)
    note = _app_call('write_knowledge_note', 
        request.title,
        body,
        metadata={
            "source_artifact_ids": [item["artifact"].get("id") for item in sources],
            "source_content_hashes": {str(item["artifact"].get("id")): hashlib.sha256(_app_call('read_artifact_source', item["artifact"])[0].encode("utf-8", errors="ignore")).hexdigest() for item in sources},
            "review_required": True,
        },
        artifact_kind="source_review_draft",
    )
    for item in sources:
        if note.get("artifact"):
            _app_call('create_relation_record', from_type="artifact", from_id=str(item["artifact"].get("id")), to_type="artifact", to_id=str(note["artifact"].get("id")), relation_type="source_to_review_draft", metadata={"review_required": True})
    return {"note": note, "sources": [item["artifact"] for item in sources], "approval_required": True}


@app.post("/api/knowledge/selection-draft")
def create_knowledge_selection_draft(request: KnowledgeSelectionDraftRequest) -> dict[str, Any]:
    artifact = _app_call('get_artifact_record', request.artifact_id)
    if not artifact:
        raise HTTPException(404, "来源 Artifact 不存在")
    content, error = _app_call('read_artifact_source', artifact)
    if error:
        raise HTTPException(400, error)
    source_path, _ = _app_call('artifact_source_path', artifact)
    raw_lines = content.splitlines()
    selections: list[dict[str, Any]] = []
    for index, raw in enumerate(request.paragraphs[:30], start=1):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        try:
            start = max(1, int(raw.get("line_start") or raw.get("start") or 0))
            end = max(start, int(raw.get("line_end") or raw.get("end") or start))
        except (TypeError, ValueError):
            start, end = 0, 0
        if start and end and raw_lines:
            selected = "\n".join(raw_lines[start - 1:min(end, len(raw_lines))]).strip()
            if selected:
                text = selected
        if not text:
            continue
        normalized = re.sub(r"\s+", " ", text).strip()
        source_line = next((line_no for line_no, line in enumerate(raw_lines, start=1) if normalized[:80] and normalized[:80] in re.sub(r"\s+", " ", line).strip()), None)
        if not source_line and normalized not in re.sub(r"\s+", " ", content):
            raise HTTPException(400, f"第 {index} 段无法在来源中定位，请重新选择原文")
        selections.append({
            "index": index,
            "text": clip(text, 8_000),
            "locator": {"path": str(source_path or artifact.get("path") or ""), "line_start": start or source_line or 0, "line_end": end or source_line or 0, "artifact_id": request.artifact_id},
        })
    if not selections:
        raise HTTPException(400, "至少选择一段可回溯的来源文本")
    body = f"# {request.title.strip()}\n\n> 这是按段落选取的可审阅草稿，写入前需要人工确认。\n\n"
    if request.instruction.strip():
        body += f"## 加工要求\n\n{request.instruction.strip()}\n\n"
    body += "## 选中来源\n\n" + "\n\n".join(
        f"### 段落 {item['index']} · Artifact #{request.artifact_id} · 第 {item['locator']['line_start']}–{item['locator']['line_end']} 行\n\n{item['text']}"
        for item in selections
    )
    note = _app_call('write_knowledge_note', 
        request.title,
        body,
        metadata={
            "source_artifact_ids": [request.artifact_id],
            "source_content_hashes": {str(request.artifact_id): hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()},
            "source_locators": [item["locator"] for item in selections],
            "review_required": True,
            "selection_mode": "paragraph",
        },
        artifact_kind="paragraph_selection_draft",
    )
    if note.get("artifact"):
        _app_call('create_relation_record', from_type="artifact", from_id=str(request.artifact_id), to_type="artifact", to_id=str(note["artifact"]["id"]), relation_type="selected_source_to_draft", metadata={"locators": [item["locator"] for item in selections]})
    return {"ok": True, "note": note, "source": artifact, "selections": selections, "approval_required": True, "policy": "草稿保留 Artifact、文件路径和行号；原始来源不被改写。"}

@app.post("/api/obsidian/drafts/{artifact_id}/sync")
def sync_knowledge_draft(artifact_id: int, request: KnowledgeDraftApplyRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(409, "写入 Obsidian 前需要明确确认")
    return _app_call('sync_knowledge_draft_to_obsidian', artifact_id, conflict_action=request.conflict_action)


@app.get("/api/knowledge/drafts/{artifact_id}/replay")
def replay_knowledge_draft(artifact_id: int) -> dict[str, Any]:
    return _app_call('knowledge_draft_replay', artifact_id)


__all__ = [
    "_knowledge_files_cache",
    "_knowledge_dir_signature",
    "knowledge_files",
    "knowledge_search",
    "EMBEDDING_URL",
    "embedding_available",
    "embed_texts",
    "_ensure_knowledge_vectors_table",
    "_embedding_text_for_note",
    "ensure_knowledge_vectors",
    "_cosine_similarity",
    "knowledge_semantic_scores",
    "knowledge_hybrid_search",
    "parse_obsidian_markdown",
    "obsidian_note_paths",
    "obsidian_note_row",
    "obsidian_backlink_count",
    "obsidian_index_vault",
    "obsidian_status",
    "obsidian_index_rows",
    "obsidian_search",
    "obsidian_related",
    "obsidian_moc_suggestions",
    "knowledge_inbox_candidates",
    "sync_inbox_to_obsidian",
    "knowledge_draft_source_check",
    "sync_knowledge_draft_to_obsidian",
    "knowledge_draft_replay",
    "extract_upload_text",
    "write_knowledge_note",
    "KnowledgeNoteUpdateRequest",
    "ObsidianInboxSyncRequest",
    "ObsidianInboxBatchSyncRequest",
    "KnowledgeDraftApplyRequest",
    "get_knowledge",
    "_resolve_knowledge_path",
    "read_knowledge_note",
    "update_knowledge_note",
    "delete_knowledge_note",
    "get_knowledge_note",
    "update_knowledge_note_api",
    "delete_knowledge_note_api",
    "knowledge_evaluation",
    "reindex_knowledge_vectors",
    "get_obsidian",
    "get_obsidian_related",
    "get_obsidian_moc_suggestions",
    "sync_obsidian_inbox",
    "batch_sync_obsidian_inbox",
    "index_obsidian",
    "get_knowledge_inbox_candidates",
    "knowledge_tokens",
    "knowledge_vector_features",
    "knowledge_hash_vector",
    "knowledge_vector_similarity",
    "obsidian_semantic_results",
    "obsidian_conflict_key",
    "list_knowledge_conflict_resolutions",
    "obsidian_conflict_paragraph_key",
    "list_knowledge_conflict_paragraph_resolutions",
    "save_knowledge_conflict_paragraph_resolution",
    "save_knowledge_conflict_resolution",
    "knowledge_conflict_draft",
    "knowledge_conflict_paragraph_draft",
    "_obsidian_paragraph_blocks",
    "obsidian_paragraph_conflicts",
    "obsidian_conflict_report",
    "obsidian_retrieval_evaluation",
    "obsidian_moc_preview",
    "apply_obsidian_moc",
    "ObsidianMocApplyRequest",
    "KnowledgeSourceDraftRequest",
    "KnowledgeSelectionDraftRequest",
    "ObsidianConflictResolutionRequest",
    "ObsidianConflictParagraphResolutionRequest",
    "semantic_search_obsidian",
    "get_obsidian_conflicts",
    "get_obsidian_retrieval_evaluation",
    "resolve_obsidian_conflict",
    "resolve_obsidian_conflict_paragraph",
    "preview_obsidian_moc",
    "maintain_obsidian_moc",
    "create_knowledge_source_draft",
    "create_knowledge_selection_draft",
    "sync_knowledge_draft",
    "replay_knowledge_draft",
]
