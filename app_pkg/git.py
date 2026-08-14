"""Workbench Git 领域：仓库清单、远程库存推送。

从 app.py 拆出的领域模块（为开源准备）。依赖 core（save_json_atomic/now_iso）
与 db，路由经 app_pkg.instance 注册；register_artifact_safely 仍留 app.py，
这里用延迟转发包装。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from .core import DATA_DIR, ROOT, now_iso, save_json_atomic
from .db import db_connection
from .instance import app
from .integrations import INTEGRATION_DEFINITIONS, integration_status


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def register_artifact_safely(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.register_artifact_safely（仍在 app.py）。"""
    import app as _app

    return _app.register_artifact_safely(*args, **kwargs)


def git_command(path: Path, args: list[str], timeout: float = 4.0) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _find_git_repos(root: Path, max_depth: int = 3, _depth: int = 0) -> list[Path]:
    """递归查找目录树中的 Git 仓库（含嵌套项目/子仓库），跳过构建与依赖目录。"""
    found: list[Path] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for child in entries:
        if not child.is_dir() or child.name in {".git", "node_modules", ".venv", "__pycache__", ".cache", "dist", "build", "Pods", "DerivedData", "target"}:
            continue
        if (child / ".git").exists():
            found.append(child)
        elif _depth < max_depth:
            found.extend(_find_git_repos(child, max_depth, _depth + 1))
    return found


def git_repository_roots() -> list[Path]:
    # 额外目录通过 WORKBENCH_GIT_ROOTS 配置（逗号分隔，可配多个本机代码目录）。
    extra = [Path(item.strip()).expanduser() for item in os.getenv("WORKBENCH_GIT_ROOTS", "").split(",") if item.strip()]
    configured = [ROOT, ROOT.parent, Path.home() / "Documents" / "troe_projects", Path.home() / "Documents" / "trae_projects", *extra]
    roots: list[Path] = []
    for candidate in configured:
        if not candidate.exists():
            continue
        if (candidate / ".git").exists():
            roots.append(candidate)
            continue
        # 递归扫描：直接子目录 + 嵌套项目（最多 3 层），不漏掉多级目录下的仓库。
        try:
            roots.extend(_find_git_repos(candidate, max_depth=3))
        except OSError:
            continue
    unique: list[Path] = []
    seen: set[str] = set()
    for path in roots:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def git_inventory() -> list[dict[str, Any]]:
    repositories = []
    work_items = _app_call('list_work_items', "all")
    for path in git_repository_roots():
        code, branch, branch_error = git_command(path, ["branch", "--show-current"])
        _status_code, status_text, status_error = git_command(path, ["status", "--short", "--branch"], timeout=6)
        _log_code, log_text, log_error = git_command(path, ["log", "-8", "--date=iso-strict", "--pretty=format:%H%x09%ad%x09%an%x09%s"], timeout=6)
        commits = []
        for line in log_text.splitlines():
            commit, sep1, rest = line.partition("\t")
            timestamp, sep2, rest = rest.partition("\t")
            author, sep3, subject = rest.partition("\t")
            if sep1 and sep2 and sep3:
                commits.append({"hash": commit[:12], "timestamp": timestamp, "author": author, "subject": subject})
        status_lines = [line for line in status_text.splitlines() if line and not line.startswith("##")]
        related = [item for item in work_items if path.name.lower() in f"{item.get('title', '')} {item.get('description', '')}".lower() or str(item.get("metadata", {}).get("repo_path", "")) == str(path)]
        repositories.append({
            "path": str(path),
            "name": path.name,
            "branch": branch if code == 0 else "",
            "branch_error": branch_error,
            "dirty": bool(status_lines),
            "status_lines": status_lines[:80],
            "commits": commits,
            "related_work_items": related[:20],
            "errors": [value for value in (status_error, log_error) if value],
            "scanned_at": now_iso(),
        })
    return repositories


@app.get("/api/git/repositories")
def get_git_repositories() -> dict[str, Any]:
    repositories = git_inventory()
    remote = load_remote_git_inventory()
    if remote:
        # 合并 Mac 推送的本机项目清单，标记来源；本机（服务器）扫描靠前。
        repositories = [dict(item, source="服务器") for item in repositories] + [dict(item, source="Mac") for item in remote]
    return {"repositories": repositories, "scanned_at": now_iso(), "scanned_roots": [str(root) for root in git_repository_roots()], "remote_machines": list({str(item.get("machine") or "Mac") for item in remote}) if remote else []}


@app.post("/api/git/scan")
def scan_git_repositories() -> dict[str, Any]:
    result = {"repositories": git_inventory(), "scanned_at": now_iso(), "scanned_roots": [str(root) for root in git_repository_roots()]}
    register_artifact_safely(project_id="workbench", name="git-inventory.json", path=str(DATA_DIR / "git-inventory.json"), kind="git_inventory", metadata={"count": len(result["repositories"]), "scanned_at": result["scanned_at"]})
    save_json_atomic(DATA_DIR / "git-inventory.json", result, 0o600)
    return result


GIT_INVENTORY_REMOTE_FILE = DATA_DIR / "git-inventory-remote.json"


def load_remote_git_inventory() -> list[dict[str, Any]]:
    """读取其他机器（如 Mac）推送的本机 Git 项目清单，用于线上 git 中心合并显示。"""
    try:
        payload = json.loads(GIT_INVENTORY_REMOTE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload.get("repos") or [] if isinstance(payload, dict) else []


@app.post("/api/git/inventory-push")
async def push_git_inventory(request: Request) -> dict[str, Any]:
    """接收本机推送的 Git 项目清单（Mac 等），存到服务器供 git 中心合并展示。

    认证：请求头 X-Workbench-Token 必须匹配服务器 .env 的 WORKBENCH_GIT_PUSH_TOKEN。
    """
    expected = os.getenv("WORKBENCH_GIT_PUSH_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, "服务端未配置 WORKBENCH_GIT_PUSH_TOKEN")
    provided = str(request.headers.get("x-workbench-token") or "")
    # This endpoint is exposed without nginx Basic Auth, so compare in constant
    # time rather than leaking the token prefix through response timing.
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(403, "推送令牌不匹配")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")
    repos = payload.get("repos") if isinstance(payload, dict) else None
    if not isinstance(repos, list):
        raise HTTPException(400, "repos 必须是数组")
    payload["machine"] = str(payload.get("machine") or "Mac")
    payload["pushed_at"] = now_iso()
    payload["repos"] = [repo for repo in repos if isinstance(repo, dict) and repo.get("name")][:200]
    save_json_atomic(GIT_INVENTORY_REMOTE_FILE, payload, 0o600)
    return {"ok": True, "count": len(payload["repos"]), "machine": payload["machine"]}



# ---------------------------------------------------------------------------
# Usage statistics: answer "which parts of the Workbench do I actually use".
# Everything here reads existing tables; no new writes, no new schema.  The
# point is to make retirement decisions with data instead of by feel.
# ---------------------------------------------------------------------------

USAGE_WINDOW_CHOICES = (7, 30, 90)

# 不算「智能体运行」的内部记录：dispatch_child 是总调度派生的子调用（与父 run
# 双计）；evidence_approval 是联动验收基线；manual_takeover 是人工接管动作；
# approval_decision 是审批按钮。这些混进 runs 会让统计虚高——曾出现 30 天
# 282 条 run 里近一半是这三类。口径与 /api/trace/recent 保持一致，只多排
# approval_decision。
USAGE_EXCLUDED_RUN_KINDS = ("dispatch_child", "evidence_acceptance", "manual_takeover", "approval_decision")

@app.get("/api/github-tools")
def get_github_tools() -> dict[str, Any]:
    markitdown = _app_call('markitdown_status', )
    tools = [
        {"id": "markitdown", "name": "Microsoft MarkItDown", "url": "https://github.com/microsoft/markitdown", "scenario": "文档、网页和演示材料转 Markdown", "cost": "免费·可选依赖", "fit": "已接入文档工厂；未安装时回退内置解析器", "trial": "在文档工厂上传一份 PPTX 或复杂表格，检查 Markdown 结构和来源保留", "state": "integrated", "installed": bool(markitdown.get("available")), "data_boundary": "文件只在 Workbench 进程内转换，不自动上传"},
        {"id": "activitywatch", "name": "ActivityWatch", "url": "https://github.com/ActivityWatch/activitywatch", "scenario": "个人时间和效率反馈", "cost": "免费·本地", "fit": "已接入近 7 天聚合观察，不保存窗口标题和 URL", "trial": "配置本机服务后导入一次聚合观察 WorkItem", "state": "integrated", "installed": None, "data_boundary": "只回传聚合时长、事件数量和数据时间"},
        {"id": "github-issues", "name": "GitHub Issues / Pull Requests", "url": "https://github.com/cli/cli", "scenario": "代码项目待办和评审", "cost": "免费·API", "fit": "已接入只读读取与收件箱导入", "trial": "配置一个仓库，读取开放 Issue/PR 并人工勾选导入", "state": "integrated", "installed": None, "data_boundary": "只读仓库条目；写操作仍需人工确认"},
        {"id": "zotero", "name": "Zotero", "url": "https://github.com/zotero/zotero", "scenario": "论文、资料和 DOI 学习入口", "cost": "免费·API", "fit": "已接入研究条目读取并导入知识库", "trial": "读取最近研究条目，人工选择后生成知识库工作项", "state": "integrated", "installed": None, "data_boundary": "只读取用户选定条目的元数据和摘要"},
        {"id": "linkding", "name": "Linkding", "url": "https://github.com/sissbruecker/linkding", "scenario": "低噪书签与稍后读入口", "cost": "免费·自托管", "fit": "已接入只读书签读取和人工勾选导入", "trial": "配置 Linkding 后导入一批待读书签，观察网页研究和知识库的分流质量", "state": "integrated", "installed": None, "data_boundary": "只读标题、链接、描述和标签；不修改书签"},
        {"id": "paperless", "name": "Paperless-ngx", "url": "https://github.com/paperless-ngx/paperless-ngx", "scenario": "个人文档归档与资料再利用", "cost": "免费·自托管", "fit": "已接入文档元数据读取和人工导入知识库", "trial": "配置 Paperless-ngx 后选择几份文档，验证来源和数据时间是否保留", "state": "integrated", "installed": None, "data_boundary": "只读文档元数据；不自动下载或修改归档文件"},
        {"id": "searxng", "name": "SearXNG", "url": "https://github.com/searxng/searxng", "scenario": "隐私友好的学习资料搜索", "cost": "免费·自托管", "fit": "已接入搜索结果读取和人工选择进入网页研究", "trial": "配置一个 SearXNG 实例，搜索一个学习主题并人工选择结果进入网页研究", "state": "integrated", "installed": None, "data_boundary": "只读聚合搜索结果；不保存原始搜索日志，不自动抓取全文"},
        {"id": "wallabag", "name": "Wallabag", "url": "https://github.com/wallabag/wallabag", "scenario": "稍后读文章进入学习流程", "cost": "免费·自托管", "fit": "已接入未归档文章读取和人工选择进入网页研究", "trial": "配置 Access Token，选择一篇稍后读文章进入网页研究，验证来源回溯", "state": "integrated", "installed": None, "data_boundary": "只读文章元数据和摘要；不修改 Wallabag 的归档状态"},
        {"id": "lazygit", "name": "lazygit", "url": "https://github.com/jesseduffield/lazygit", "scenario": "终端 Git 审查与提交", "cost": "免费·本地", "fit": "Workbench 已有只读 Git 项目中心；lazygit 作为本机审查补充", "trial": "安装后扫描一个仓库并完成一次分支审查；Workbench 不自动执行提交或 push", "state": "candidate", "installed": bool(shutil.which("lazygit")), "data_boundary": "本地 Git 元数据；不要把密钥或完整 diff 放入通知"},
        {"id": "super-productivity", "name": "Super Productivity", "url": "https://github.com/super-productivity/super-productivity", "scenario": "任务执行、时间记录和学习计划", "cost": "免费·本地", "fit": "仅作为单向导入候选，不替代 Workbench 收件箱主库", "trial": "先导出一份任务 JSON，验证去重、截止时间和来源映射", "state": "candidate", "installed": None, "data_boundary": "只读导出；不做双向同步，不创建第二套主任务库"},
    ]
    connection = db_connection()
    try:
        rows = connection.execute("SELECT * FROM work_items WHERE kind = 'github_tool_trial' ORDER BY created_at DESC LIMIT 50").fetchall()
        trials = [_app_call('work_item_row', row) for row in rows]
    finally:
        connection.close()
    return {
        "tools": tools,
        "trials": trials,
        "integrations": [integration_status(integration_id) for integration_id in INTEGRATION_DEFINITIONS],
        "generated_at": now_iso(),
    }


@app.post("/api/github-tools/{tool_id}/trial")
async def create_github_tool_trial(tool_id: str) -> dict[str, Any]:
    catalog = await asyncio.to_thread(get_github_tools)
    tool = next((item for item in catalog["tools"] if item["id"] == tool_id), None)
    if not tool:
        raise HTTPException(404, "GitHub 工具不存在")
    item = _app_call('create_work_item_record', title=f"试用 GitHub 工具：{tool['name']}", description=f"场景：{tool['scenario']}\n试用建议：{tool['trial']}\n仓库：{tool['url']}", kind="github_tool_trial", source_project="workbench", target_project="workbench", metadata={"tool": tool, "repo_path": ""})
    return {"ok": True, "item": item}


__all__ = [
    "GIT_INVENTORY_REMOTE_FILE",
    "USAGE_EXCLUDED_RUN_KINDS",
    "USAGE_WINDOW_CHOICES",
    "_find_git_repos",
    "create_github_tool_trial",
    "get_git_repositories",
    "get_github_tools",
    "git_command",
    "git_inventory",
    "git_repository_roots",
    "load_remote_git_inventory",
    "push_git_inventory",
    "register_artifact_safely",
    "scan_git_repositories",
]
