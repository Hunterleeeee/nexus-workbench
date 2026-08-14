"""Workbench 内核：无业务依赖的基础设施。

这是拆分 app.py 的第一块地基。规则：
1. 本模块不 import 任何业务领域模块（aihot/market/learning...），不 import fastapi。
2. 只放「任何领域都要用」的常量与工具：路径、日志、时间、通用编码。
3. 新增符号时优先放这里；领域私有符号放对应领域模块。

拆分进度：
- [x] 路径常量 / 日志 / 版本 / 限额（从 app.py 头部抽出）
- [ ] DB 连接与 schema（app_pkg/db.py，待抽）
- [ ] 通用工具 now_iso / clip / save_json_atomic ...（待抽）
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Logging.  Until now every failure path was a bare `except Exception: pass`,
# which meant production incidents left no trace at all.  systemd captures
# stdout/stderr into the journal, so a plain StreamHandler is enough:
#   journalctl -u workbench -f
# Set WORKBENCH_LOG_LEVEL=DEBUG to raise verbosity without a code change.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, os.getenv("WORKBENCH_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("workbench")

# Release marker: keep the development server's reload watcher aligned with VERSION.
STATIC_DIR = ROOT / "static"
PROJECTS_FILE = ROOT / "projects.json"
VERSION_FILE = ROOT / "VERSION"
load_dotenv(ROOT / ".env")


def configured_path(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    return Path(value).expanduser() if value else default


DATA_DIR = configured_path("WORKBENCH_DATA_DIR", ROOT / "data")
OUTPUTS_DIR = configured_path("WORKBENCH_OUTPUTS_DIR", ROOT / "outputs")
CLOUDGEN_DIR = OUTPUTS_DIR / "cloudgen"
KNOWLEDGE_DIR = configured_path("WORKBENCH_KNOWLEDGE_DIR", ROOT / "knowledge-base")
SETTINGS_FILE = DATA_DIR / "llm_settings.json"
PROJECT_PREFERENCES_FILE = DATA_DIR / "project_preferences.json"
DATABASE_FILE = DATA_DIR / "workbench.db"
PRODUCT_PROTOTYPES_DIR = DATA_DIR / "product-prototypes"
COWART_VENDOR_DIR = STATIC_DIR / "vendor" / "cowart"
COWART_VERSION = "0.1.25"
COWART_SCRIPT_NAME = "index-pR7Yavzt.js"
COWART_STYLE_NAME = "style-D82LwrRu.css"
SUB2API_SNAPSHOT_FILE = DATA_DIR / "sub2api_snapshot.json"
MARKET_WATCHLIST_FILE = DATA_DIR / "market_watchlist.json"
MARKET_SNAPSHOT_FILE = DATA_DIR / "market_snapshot.json"
SERVER_MONITOR_SNAPSHOT_FILE = DATA_DIR / "server_monitor_snapshot.json"
SERVER_MONITOR_THRESHOLDS_FILE = DATA_DIR / "server_monitor_thresholds.json"
AIHOT_SNAPSHOT_FILE = DATA_DIR / "aihot_snapshot.json"
INTEGRATIONS_FILE = DATA_DIR / "integrations.json"
VAPID_PRIVATE_KEY_FILE = DATA_DIR / "vapid_private.pem"
# The Obsidian vault is a per-machine location.  The default points inside the
# repo so a fresh checkout (or the server) never silently reads a path that
# only exists on one laptop; set WORKBENCH_OBSIDIAN_VAULT_DIR to the real vault.
OBSIDIAN_VAULT_DIR = configured_path(
    "WORKBENCH_OBSIDIAN_VAULT_DIR",
    KNOWLEDGE_DIR / "obsidian",
)
AIHOT_FEED_URL = os.getenv("WORKBENCH_AIHOT_URL", "https://aihot.today/ai-news").strip()
WORKBENCH_PUBLIC_URL = os.getenv("WORKBENCH_PUBLIC_URL", "https://workbench.example.dev:8765").strip().rstrip("/")

WORKBENCH_VERSION = ""
try:
    # VERSION is the release source of truth. An old shell-level override must
    # not make the API report a version different from the files being served.
    WORKBENCH_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip()
except OSError:
    WORKBENCH_VERSION = os.getenv("WORKBENCH_VERSION", "").strip() or "0.3.104"
MAX_LLM_CONTEXT_CHARS = 36_000
MAX_DOCUMENT_CONTEXT_CHARS = 12_000
MAX_CONVERSATION_CHARS = 12_000
MAX_CONVERSATION_MESSAGES = 8
MAX_AGENT_NEW_PAGES = 3
MAX_AGENT_TOTAL_PAGES = 6
MEMORY_OWNER_ID = "default"
MAX_MEMORY_CONTEXT_ITEMS = 5


def now_iso() -> str:
    """当前 UTC 时间戳（ISO 8601）。数据库所有 created_at/updated_at 的规范格式。"""
    return datetime.now(timezone.utc).isoformat()


def load_json_file(path: Path, fallback: Any) -> Any:
    """读取 JSON 文件，失败回退 fallback。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def save_json_atomic(path: Path, values: Any, mode: int | None = None) -> None:
    """原子写 JSON：先写临时文件再替换，避免半截文件。"""
    DATA_DIR.mkdir(exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if mode is not None:
        try:
            temporary.chmod(mode)
        except OSError:
            pass
    os.replace(temporary, path)


def clip(value: str | None, limit: int) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    return value[:limit] + "\n\n[…内容已截断…]"


def clip_for_llm(value: str | None, limit: int) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    head = int(limit * 0.72)
    tail = limit - head
    return value[:head] + "\n\n[…中间内容已压缩…]\n\n" + value[-tail:]

__all__ = [
    "ROOT",
    "STATIC_DIR",
    "PROJECTS_FILE",
    "VERSION_FILE",
    "log",
    "configured_path",
    "DATA_DIR",
    "OUTPUTS_DIR",
    "CLOUDGEN_DIR",
    "KNOWLEDGE_DIR",
    "SETTINGS_FILE",
    "PROJECT_PREFERENCES_FILE",
    "DATABASE_FILE",
    "PRODUCT_PROTOTYPES_DIR",
    "COWART_VENDOR_DIR",
    "COWART_VERSION",
    "COWART_SCRIPT_NAME",
    "COWART_STYLE_NAME",
    "SUB2API_SNAPSHOT_FILE",
    "MARKET_WATCHLIST_FILE",
    "MARKET_SNAPSHOT_FILE",
    "SERVER_MONITOR_SNAPSHOT_FILE",
    "SERVER_MONITOR_THRESHOLDS_FILE",
    "AIHOT_SNAPSHOT_FILE",
    "INTEGRATIONS_FILE",
    "VAPID_PRIVATE_KEY_FILE",
    "OBSIDIAN_VAULT_DIR",
    "AIHOT_FEED_URL",
    "WORKBENCH_PUBLIC_URL",
    "WORKBENCH_VERSION",
    "MAX_LLM_CONTEXT_CHARS",
    "MAX_DOCUMENT_CONTEXT_CHARS",
    "MAX_CONVERSATION_CHARS",
    "MAX_CONVERSATION_MESSAGES",
    "MAX_AGENT_NEW_PAGES",
    "MAX_AGENT_TOTAL_PAGES",
    "MEMORY_OWNER_ID",
    "MAX_MEMORY_CONTEXT_ITEMS",
    "now_iso",
    "clip",
    "clip_for_llm",
    "load_json_file",
    "save_json_atomic",
]
