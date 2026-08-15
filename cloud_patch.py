"""云开发「云端自动改+审批」：对 workbench 代码库做受控修改。

安全边界：
- 只允许改 static/ 前端文件与顶层小模块（< CORE_FILE_LIMIT 字符），拒绝 app.py 等核心大文件。
- 编辑以「old 片段唯一匹配」方式应用，匹配不上立即报错，绝不模糊替换。
- 应用前先备份原文件；审批执行时先备份再应用，失败自动回滚。
- 不执行任意 shell、不自动部署；审批通过后重启服务 + 健康检查，失败回滚。

文件路径必须相对仓库根，且通过白名单校验。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Awaitable

# 允许修改的目录前缀（相对仓库根）
ALLOWED_PREFIXES = ("static/",)
# 顶层允许修改的小模块（字符数限制内）
ALLOWED_TOP_FILES = ("feishu.py", "cloud_dev.py", "cloud_patch.py", "browser_render_worker.py")
# 单个文件超过该字符数不参与云端自动改（防止大文件模糊编辑）
FILE_CHAR_LIMIT = 60_000
# 单次最多编辑处数
MAX_EDITS = 6
# 单处 old 片段长度上限
OLD_FRAGMENT_LIMIT = 3_000
# LLM 生成的编辑计划里禁止出现的危险模式（粗过滤，应用前还有唯一匹配校验）
DANGEROUS_OLD = re.compile(r"(?i)(rm\s+-rf|shutil\.rmtree|os\.system|subprocess\.(call|run|Popen)|eval\(|exec\(|__import__|pickle\.loads|/etc/passwd)")


def _repo_root() -> Path:
    """仓库根 = 本模块所在目录（/www/workbench 或本地项目根）。"""
    return Path(__file__).resolve().parent


def code_file_index(root: Path | None = None) -> list[dict[str, Any]]:
    """列出可编辑文件清单（路径 + 字符数），供 LLM 选文件。"""
    root = root or _repo_root()
    index: list[dict[str, Any]] = []
    for prefix in ALLOWED_PREFIXES:
        base = root / prefix
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in (".html", ".js", ".css", ".md", ".py", ".json"):
                rel = str(path.relative_to(root))
                if path.stat().st_size <= FILE_CHAR_LIMIT:
                    index.append({"file": rel, "chars": path.stat().st_size})
    for name in ALLOWED_TOP_FILES:
        path = root / name
        if path.is_file() and path.stat().st_size <= FILE_CHAR_LIMIT:
            index.append({"file": name, "chars": path.stat().st_size})
    return sorted(index, key=lambda item: item["file"])


def _read_file(root: Path, rel: str) -> str:
    path = root / rel
    return path.read_text(encoding="utf-8")


def validate_edits(edits: list[dict[str, Any]], root: Path | None = None) -> dict[str, Any]:
    """校验编辑计划：路径白名单 + old 片段唯一匹配 + 危险模式过滤。

    返回 {"ok": bool, "errors": [...], "valid_edits": [...]}
    """
    root = root or _repo_root()
    errors: list[str] = []
    valid: list[dict[str, Any]] = []
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "errors": ["编辑计划为空"], "valid_edits": []}
    if len(edits) > MAX_EDITS:
        return {"ok": False, "errors": [f"单次最多 {MAX_EDITS} 处编辑，当前 {len(edits)} 处"], "valid_edits": []}
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            errors.append(f"第 {i} 处编辑格式错误")
            continue
        rel = str(edit.get("file") or "").strip().lstrip("./")
        old = str(edit.get("old") or "")
        new = str(edit.get("new") or "")
        # 路径白名单
        allowed = rel.startswith(ALLOWED_PREFIXES) or rel in ALLOWED_TOP_FILES
        if not allowed:
            errors.append(f"第 {i} 处：文件 {rel} 不在可改范围（仅 static/ 与固定小模块）")
            continue
        if ".." in rel or "\x00" in rel:
            errors.append(f"第 {i} 处：非法路径 {rel}")
            continue
        if not old or not new:
            errors.append(f"第 {i} 处：old/new 不能为空")
            continue
        if len(old) > OLD_FRAGMENT_LIMIT:
            errors.append(f"第 {i} 处：old 片段过长（>{OLD_FRAGMENT_LIMIT} 字符）")
            continue
        if DANGEROUS_OLD.search(old):
            errors.append(f"第 {i} 处：old 片段含危险模式，拒绝")
            continue
        target = root / rel
        if not target.is_file():
            errors.append(f"第 {i} 处：文件 {rel} 不存在")
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"第 {i} 处：读取 {rel} 失败：{exc}")
            continue
        count = content.count(old)
        if count == 0:
            errors.append(f"第 {i} 处：{rel} 中找不到要替换的片段（已发生变化？）")
            continue
        if count > 1:
            errors.append(f"第 {i} 处：{rel} 中该片段出现 {count} 次，不唯一，请附带更多上下文")
            continue
        valid.append({"file": rel, "old": old, "new": new, "why": str(edit.get("why") or "")[:200]})
    return {"ok": not errors, "errors": errors, "valid_edits": valid}


def apply_edits(edits: list[dict[str, Any]], root: Path | None = None, *, backup_dir: Path | None = None) -> dict[str, Any]:
    """把校验过的编辑应用到真实文件；返回备份目录（可回滚）。

    先整体备份涉及的文件，再逐个应用；任一处失败则回滚已应用的改动并返回错误。
    """
    root = root or _repo_root()
    checked = validate_edits(edits, root)
    if not checked["ok"]:
        return {"ok": False, "errors": checked["errors"], "applied": []}
    touched: dict[str, str] = {}
    applied: list[str] = []
    created_backup = False
    try:
        for edit in checked["valid_edits"]:
            target = root / edit["file"]
            if edit["file"] not in touched:
                content = target.read_text(encoding="utf-8")
                touched[edit["file"]] = content
                if backup_dir is not None:
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    (backup_dir / edit["file"].replace("/", "__")).write_text(content, encoding="utf-8")
                    created_backup = True
            content = touched[edit["file"]]
            if content.count(edit["old"]) != 1:
                raise RuntimeError(f"{edit['file']} 中片段不唯一或已变化，拒绝应用")
            touched[edit["file"]] = content.replace(edit["old"], edit["new"], 1)
            applied.append(edit["file"])
        for rel, content in touched.items():
            (root / rel).write_text(content, encoding="utf-8")
        return {"ok": True, "applied": applied, "backup_dir": str(backup_dir) if created_backup and backup_dir else None}
    except Exception as exc:
        # 回滚已写盘的文件
        for rel, content in touched.items():
            try:
                (root / rel).write_text(content, encoding="utf-8")
            except OSError:
                pass
        return {"ok": False, "errors": [f"应用失败：{exc}"], "applied": []}


def rollback(backup_dir: Path | None, root: Path | None = None) -> dict[str, Any]:
    """从备份目录恢复文件。"""
    root = root or _repo_root()
    if backup_dir is None or not backup_dir.is_dir():
        return {"ok": False, "errors": ["没有可用的备份目录"]}
    restored: list[str] = []
    for backup in backup_dir.iterdir():
        if not backup.is_file():
            continue
        rel = backup.name.replace("__", "/")
        target = root / rel
        try:
            target.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
            restored.append(rel)
        except OSError as exc:
            return {"ok": False, "errors": [f"回滚 {rel} 失败：{exc}"], "restored": restored}
    return {"ok": True, "restored": restored}


async def plan_patch(
    requirement: str,
    llm_call: Callable[[list[dict[str, Any]]], Awaitable[str]],
    root: Path | None = None,
) -> dict[str, Any]:
    """LLM 生成并校验编辑计划。

    llm_call: 传入 app 的 call_llm 封装（messages -> 文本）。
    返回 {"ok": bool, "summary": str, "edits": [...], "errors": [...]}
    """
    root = root or _repo_root()
    index = code_file_index(root)
    if not index:
        return {"ok": False, "errors": ["没有可编辑的文件（仅支持 static/ 与固定小模块）"], "edits": []}
    index_text = "\n".join(f"- {item['file']}（{item['chars']} 字符）" for item in index)

    # 第一轮：LLM 先选出最相关的 1-3 个文件（只看清单，不生成编辑）
    select_system = (
        "你是工作台代码库的受控修改助手。用户会用自然语言提出修改需求（前端页面、文案、样式、小模块逻辑）。"
        "请从给出的可编辑文件清单中选出最可能需要修改的 1-3 个文件，严格只输出 JSON 数组，如 [\"static/web-research.css\"]，不要任何额外文字。"
    )
    try:
        raw_select = str(
            await llm_call(
                [
                    {"role": "system", "content": select_system},
                    {"role": "user", "content": f"可编辑文件清单（路径 + 字符数）：\n{index_text}\n\n用户修改需求：{requirement}\n请选出最可能需要修改的文件（1-3 个）。"},
                ]
            )
            or ""
        ).strip()
    except Exception as exc:
        return {"ok": False, "errors": [f"LLM 选文件失败：{str(exc)[:300]}"], "edits": []}
    match = re.search(r"\[.*\]", raw_select, re.S)
    if not match:
        return {"ok": False, "errors": ["LLM 未正确返回候选文件"], "edits": []}
    try:
        selected = json.loads(match.group(0))
    except json.JSONDecodeError:
        selected = []
    if not isinstance(selected, list) or not selected:
        return {"ok": False, "errors": ["LLM 未选出候选文件"], "edits": []}

    # 读取选中文件内容（截断到合理长度，避免超 token）
    contexts = []
    for rel in selected[:3]:
        rel = str(rel or "").strip().lstrip("./")
        if rel.startswith(ALLOWED_PREFIXES) or rel in ALLOWED_TOP_FILES:
            path = root / rel
            if path.is_file() and path.stat().st_size <= FILE_CHAR_LIMIT:
                try:
                    content = path.read_text(encoding="utf-8")
                    contexts.append(f"### 文件 {rel}（共 {len(content)} 字符，下面展示前 4000 字符）\n{content[:4000]}")
                except OSError:
                    pass

    system = (
        "你是工作台代码库的受控修改助手。用户会用自然语言提出修改需求（前端页面、文案、样式、小模块逻辑）。"
        "下面是相关文件的真实内容。你必须严格输出一个 JSON 对象（不要 markdown 代码块、不要额外文字），格式："
        '{"summary": "一句话说明改动", "edits": [{"file": "相对路径", "old": "要替换的原文精确片段（必须能从上面对应文件内容中逐字找到且唯一）", "new": "替换后的内容", "why": "为什么改"}]}。'
        "要求：edits 最多 6 处；old 片段必须逐字复制自文件内容、包含足够上下文以保证唯一（至少 20 字符）；"
        "new 要完整自包含；只改必要内容，不要重写整个文件；不要改出语法错误。文件内容被截断时，只改前 4000 字符内可见的部分。"
    )
    user = f"{chr(10).join(contexts) if contexts else '（未能读取选中文件内容）'}\n\n用户修改需求：{requirement}\n\n请输出编辑计划 JSON。"
    try:
        raw = str(await llm_call([{"role": "system", "content": system}, {"role": "user", "content": user}]) or "").strip()
    except Exception as exc:
        return {"ok": False, "errors": [f"LLM 生成计划失败：{str(exc)[:300]}"], "edits": []}
    # 提取 JSON（容忍代码块包裹）
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {"ok": False, "errors": ["LLM 输出不是有效 JSON"], "edits": []}
    try:
        plan = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {"ok": False, "errors": [f"LLM 输出 JSON 解析失败：{exc}"], "edits": []}
    edits = plan.get("edits") if isinstance(plan, dict) else None
    checked = validate_edits(edits, root)
    if not checked["ok"]:
        return {"ok": False, "errors": checked["errors"], "edits": []}
    return {"ok": True, "summary": str(plan.get("summary") or "")[:200], "edits": checked["valid_edits"], "errors": []}
