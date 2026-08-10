"""安全的云开发入口。

该模块故意不接受 shell 字符串。飞书只负责创建结构化请求；真正执行时，
工作区必须来自显式配置，命令也只能从固定配方中选择。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SAFE_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHELL_META = re.compile(r"[;&|<>`$(){}\[\]\\]|\x00")
BEARER_OUTPUT = re.compile(r"(?i)(bearer\s+)[^\s,;{}\[\]\"']+")
SECRET_OUTPUT = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|cookie|secret|authorization)[\"']?\s*(?:=|:)\s*[\"']?)[^\s,;}\]\"']+"
)
MAX_OUTPUT_CHARS = 12_000
ACTION_ALIASES = {
    "status": "status",
    "状态": "status",
    "查看状态": "status",
    "test": "test",
    "测试": "test",
    "运行测试": "test",
    "build": "build",
    "构建": "build",
    "generate": "generate",
    "生成": "generate",
    "做一个": "generate",
    "帮我做": "generate",
    "帮我生成": "generate",
    "写一份": "generate",
    "写一个": "generate",
    "新建": "generate",
    "创建": "generate",
    "开发一个": "generate",
    "做个": "generate",
}

# 生成产物的类型识别：网页原型 / 文档 / 脚本
GENERATE_KIND_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("webpage", ("网页", "页面", "原型", "h5", "落地页", "登录页", "主页", "网站", "浏览器", "dashboard", "看板")),
    ("doc", ("文档", "报告", "方案", "说明", "readme", "简报", "计划书", "白皮书", "总结")),
    ("script", ("脚本", "工具", "爬虫", "命令行", "cli", "程序", "函数")),
)
GENERATE_DEFAULT_KIND = "webpage"
GENERATE_KIND_LABELS = {"webpage": "网页原型", "doc": "文档", "script": "脚本"}
GENERATE_TRIGGERS = ("生成", "做一个", "帮我做", "帮我生成", "写一份", "写一个", "新建", "创建", "开发一个", "做个")

# 云端自动改（patch）意图触发词：对 workbench 自身代码库做受控修改，需审批。
PATCH_TRIGGERS = ("改一下", "改一改", "帮我改", "修改", "调整一下", "优化一下", "升级一下", "加一个功能", "加个功能", "增加一个", "实现一个", "能不能改", "把", "给我改")



def _clip(value: Any, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n…输出已截断。"


def redact_output(value: Any) -> str:
    text = _clip(value)
    text = BEARER_OUTPUT.sub(lambda match: f"{match.group(1)}[已隐藏]", text)
    return SECRET_OUTPUT.sub(lambda match: f"{match.group(1)}[已隐藏]", text)


def workspace_map(environ: dict[str, str] | None = None) -> dict[str, Path]:
    """Read explicit workspace aliases from JSON or ``alias=/absolute/path`` pairs."""
    env = environ or os.environ
    raw = str(env.get("WORKBENCH_CLOUD_WORKSPACES", "")).strip()
    if not raw:
        return {}
    parsed: dict[str, str] = {}
    if raw.startswith("{"):
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(candidate, dict):
            parsed = {str(key): str(value) for key, value in candidate.items()}
    else:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                alias, value = item.split("=", 1)
            else:
                value = item
                alias = Path(value).name
            parsed[alias.strip()] = value.strip()
    result: dict[str, Path] = {}
    for alias, raw_path in parsed.items():
        if not SAFE_ALIAS.fullmatch(alias):
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            continue
        # Reject symlinked workspace paths before resolving them.  Checking
        # only the resolved path would make a symlink disappear and could
        # silently redirect execution outside the explicitly configured root.
        if path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            continue
        if resolved.is_dir():
            result[alias] = resolved
    return result


def parse_cloud_dev_command(text: str) -> dict[str, Any]:
    """Parse a deliberately small Chinese command grammar."""
    raw = str(text or "").strip()
    if not raw:
        return {"ok": False, "message": "云开发命令为空。"}
    if SHELL_META.search(raw):
        return {"ok": False, "message": "云开发命令包含不允许的 shell 字符，未执行。"}
    match = re.match(r"^云开发(?:\s+|：|:)?(.*)$", raw, flags=re.IGNORECASE)
    if not match:
        return {"ok": False, "message": "请使用：云开发 <项目> 查看状态 / 运行测试 / 构建。"}
    body = re.sub(r"\s+", " ", match.group(1).strip())
    if not body:
        return {"ok": False, "message": "请补充项目和动作，例如：云开发 workbench 运行测试。"}
    tokens = body.split(" ")
    action = None
    action_size = 0

    def resolve_action(value: str) -> str | None:
        direct = ACTION_ALIASES.get(value.lower(), ACTION_ALIASES.get(value))
        if direct:
            return direct
        compact = re.sub(r"\s+", "", value)
        return ACTION_ALIASES.get(compact.lower(), ACTION_ALIASES.get(compact))

    # Match the two-word Chinese aliases first.  Otherwise a natural message
    # such as “云开发 workbench 查看 状态” would match “状态” as a one-word
    # action and incorrectly leave “workbench 查看” as the project name.
    for size in (2, 1):
        if len(tokens) < size:
            continue
        action_token = " ".join(tokens[-size:])
        action = resolve_action(action_token)
        if action:
            action_size = size
            break
    if action_size:
        tokens = tokens[:-action_size]
    if action is None:
        # 云端自动改意图：例如「云开发 帮我改一下 AI 伴读的样式」
        if "改" in body or "优化" in body or "升级" in body or "实现一个" in body or "加个" in body or "加一个" in body:
            return {
                "ok": True,
                "action": "patch",
                "project": "workbench",
                "requirement": body,
                "requires_approval": False,
                "raw": raw,
            }
        # 自然语言生成意图：例如「云开发 帮我做一个理财记账网页」「云开发 写一份竞品分析报告」
        for trigger in GENERATE_TRIGGERS:
            if trigger in body:
                return {
                    "ok": True,
                    "action": "generate",
                    "project": "cloudgen",
                    "kind": _detect_generate_kind(body),
                    "requirement": re.sub(rf"^{re.escape(trigger)}", "", body).strip() or body,
                    "requires_approval": False,
                    "raw": raw,
                }
        return {"ok": False, "message": "当前支持：① 云开发 <项目> 查看状态 / 运行测试 / 构建；② 云开发 帮我做一个 <网页/文档/脚本>；③ 云开发 帮我改一下 <需求>（需审批）。"}
    project = "workbench"
    if tokens:
        if len(tokens) != 1 or not SAFE_ALIAS.fullmatch(tokens[0]):
            return {"ok": False, "message": "项目名只能包含字母、数字、点、下划线和短横线。"}
        project = tokens[0]
    return {
        "ok": True,
        "project": project,
        "action": action,
        "requires_approval": action == "build",
        "raw": raw,
    }


def _detect_generate_kind(text: str) -> str:
    lowered = text.lower()
    for kind, keywords in GENERATE_KIND_PATTERNS:
        if any(keyword in lowered for keyword in keywords):
            return kind
    return GENERATE_DEFAULT_KIND


def _recipe(root: Path, action: str) -> list[str] | None:
    def npm_script_exists(name: str) -> bool:
        manifest = root / "package.json"
        if not manifest.is_file():
            return False
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        return isinstance(scripts, dict) and isinstance(scripts.get(name), str) and bool(scripts[name].strip())

    if action == "status":
        return None
    if action == "test":
        pytest = root / ".venv" / "bin" / "pytest"
        if pytest.is_file() and (root / "pytest.ini").exists():
            return [str(pytest), "-q"]
        if (root / "pytest.ini").exists() or (root / "pyproject.toml").exists() or (root / "tests").is_dir():
            return [sys.executable, "-m", "pytest", "-q"]
        if npm_script_exists("test"):
            return ["npm", "test"]
        return None
    if action == "build":
        if npm_script_exists("build"):
            return ["npm", "run", "build"]
        if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
            return [sys.executable, "-m", "compileall", "-q", "."]
        # Workbench and similar small Python services may intentionally keep
        # only requirements.txt + app.py.  The fixed compile recipe validates
        # the application and its fixed service modules without installing
        # dependencies or deploying it.
        if (root / "requirements.txt").is_file() and (root / "app.py").is_file():
            python_files = [
                name
                for name in (
                    "app.py",
                    "cloud_dev.py",
                    "feishu.py",
                    "agent_worker.py",
                    "crawl_worker.py",
                    "monitor_worker.py",
                    "sync_worker.py",
                    "backup.py",
                )
                if (root / name).is_file()
            ]
            return [sys.executable, "-m", "compileall", "-q", *python_files]
    return None


def _safe_env() -> dict[str, str]:
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "CI", "VIRTUAL_ENV", "NODE_ENV"}
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result.setdefault("LANG", "C.UTF-8")
    result["CI"] = "1"
    result["GIT_TERMINAL_PROMPT"] = "0"
    return result


def _workspace_status(project: str, root: Path) -> dict[str, Any]:
    markers = [name for name in ("pyproject.toml", "pytest.ini", "package.json", "requirements.txt") if (root / name).exists()]
    recipe_names = [name for name in ("status", "test", "build") if name == "status" or _recipe(root, name)]
    try:
        file_count = sum(1 for path in root.rglob("*") if path.is_file() and ".git" not in path.parts and "node_modules" not in path.parts and ".venv" not in path.parts)
    except OSError:
        file_count = 0
    return {"project": project, "workspace": str(root), "exists": True, "markers": markers, "file_count": file_count, "available_actions": recipe_names}


def run_cloud_dev(request: dict[str, Any], *, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Execute one parsed request, or return a structured refusal."""
    if not request.get("ok"):
        return {"status": "rejected", "message": request.get("message") or "无效云开发请求。"}
    project = str(request.get("project") or "workbench")
    action = str(request.get("action") or "")
    workspaces = workspace_map(environ)
    root = workspaces.get(project)
    if root is None:
        return {"status": "not_configured", "project": project, "action": action, "message": "服务器未配置该项目的显式云开发工作区，未执行任何命令。"}
    if action == "status":
        return {"status": "ok", "action": action, **_workspace_status(project, root)}
    command = _recipe(root, action)
    if not command:
        return {"status": "unsupported", "project": project, "action": action, "message": "该工作区没有已识别的固定命令配方，未执行。"}
    raw_timeout = str((environ or os.environ).get("WORKBENCH_CLOUD_COMMAND_TIMEOUT_SECONDS", "120")).strip()
    try:
        configured_timeout = int(raw_timeout)
    except (TypeError, ValueError):
        configured_timeout = 120
    timeout = max(5, min(300, configured_timeout))
    try:
        completed = subprocess.run(command, cwd=root, env=_safe_env(), shell=False, capture_output=True, text=True, timeout=timeout, check=False)
        output = redact_output((completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else ""))
        return {"status": "ok" if completed.returncode == 0 else "failed", "project": project, "action": action, "command": command, "exit_code": completed.returncode, "output": output, "timeout_seconds": timeout}
    except subprocess.TimeoutExpired as exc:
        output = redact_output((exc.stdout or "") + ("\n" + exc.stderr if exc.stderr else ""))
        return {"status": "timeout", "project": project, "action": action, "command": command, "output": output, "timeout_seconds": timeout}
    except OSError as exc:
        return {"status": "failed", "project": project, "action": action, "command": command, "message": _clip(exc, 500)}


def cloud_dev_policy() -> dict[str, Any]:
    return {
        "workspace_source": "WORKBENCH_CLOUD_WORKSPACES（仅绝对路径，alias=path 或 JSON）",
        "allowed_actions": ["status", "test", "build", "generate"],
        "automatic_actions": ["status", "test", "generate"],
        "approval_actions": ["build"],
        "generate_policy": "自然语言生成产物由 LLM 生成后写入 outputs/cloudgen/，仅作交付物保存与查看，不在服务器执行、不部署；链接经认证访问。",
        "shell": False,
        "network": "不额外启动常驻服务；命令继承最小化环境，超时 300 秒",
        "secret_policy": "不把 API Key、Cookie、OAuth 或完整环境变量写入结果；输出截断并脱敏边界由调用方继续处理。",
    }
