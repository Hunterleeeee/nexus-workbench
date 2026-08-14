"""Workbench Git 领域：仓库清单、远程库存推送。

从 app.py 拆出的领域模块（为开源准备）。依赖 core（save_json_atomic/now_iso）
与 db，路由经 app_pkg.instance 注册；register_artifact_safely 仍留 app.py，
这里用延迟转发包装。
"""

from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path
from typing import Any

from .core import DATA_DIR, now_iso, save_json_atomic
from .db import db_connection
from .instance import app


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
    work_items = list_work_items("all")
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

__all__ = ["register_artifact_safely", "git_command", "_find_git_repos", "git_repository_roots", "git_inventory", "get_git_repositories", "scan_git_repositories", "load_remote_git_inventory", "push_git_inventory"]
