#!/usr/bin/env python3
"""Workbench 数据库备份与恢复 CLI。

用法：
  python backup.py backup [备注]
  python backup.py list
  python backup.py restore <备份文件名> [--yes]

备份保存到 data/backups/；恢复前会自动创建一次 before-restore 安全备份。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(__file__).resolve().parent / "data"
DATABASE_FILE = DATA_DIR / "workbench.db"
BACKUP_ROOT = DATA_DIR / "backups"


def safe_filename(value: str, fallback: str = "backup") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-")
    return (cleaned or fallback)[:60]


def backup(reason: str = "manual") -> dict:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_ROOT / f"workbench-{stamp}-{safe_filename(reason, 'manual')}.db"
    if not DATABASE_FILE.exists():
        raise SystemExit(f"数据库不存在：{DATABASE_FILE}")
    source = sqlite3.connect(DATABASE_FILE)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    info = {"name": target.name, "path": str(target), "size": target.stat().st_size, "created_at": datetime.now().isoformat(timespec="seconds")}
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return info


def list_backups() -> None:
    if not BACKUP_ROOT.exists():
        print("还没有备份。运行 backup.py backup 创建第一个备份。")
        return
    items = sorted(BACKUP_ROOT.glob("*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not items:
        print("还没有备份。运行 backup.py backup 创建第一个备份。")
        return
    for path in items:
        size_kb = path.stat().st_size / 1024
        updated = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{path.name:<60} {size_kb:>10.1f} KB  {updated}")


def restore(name: str, confirmed: bool = False) -> dict:
    safe_name = Path(name).name
    source = BACKUP_ROOT / safe_name
    if source.parent != BACKUP_ROOT or not source.is_file() or source.suffix != ".db":
        raise SystemExit("备份文件不存在或不在受控备份目录")
    print(f"即将从 {source.name} 恢复数据库。恢复前会先创建一次 before-restore 安全备份。")
    if not confirmed:
        answer = input("输入 RESTORE 确认：")
        if answer.strip() != "RESTORE":
            raise SystemExit("已取消恢复。")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety = BACKUP_ROOT / f"workbench-{stamp}-before-restore.db"
    source_conn = sqlite3.connect(DATABASE_FILE)
    safety_conn = sqlite3.connect(safety)
    try:
        source_conn.backup(safety_conn)
    finally:
        safety_conn.close()
        source_conn.close()
    restored_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(DATABASE_FILE)
    try:
        restored_conn.execute("PRAGMA integrity_check")
        restored_conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        restored_conn.backup(target_conn)
    finally:
        target_conn.close()
        restored_conn.close()
    info = {"ok": True, "restored": safe_name, "safety_backup": safety.name, "restored_at": datetime.now().isoformat(timespec="seconds")}
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description="Workbench 数据库备份与恢复")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup", help="创建备份（可带备注）").add_argument("reason", nargs="?", default="manual")
    sub.add_parser("list", help="列出备份")
    restore_parser = sub.add_parser("restore", help="恢复备份")
    restore_parser.add_argument("name", help="备份文件名（data/backups/ 下的 .db 文件）")
    restore_parser.add_argument("--yes", action="store_true", help="跳过 RESTORE 确认")
    args = parser.parse_args()
    if args.command == "backup":
        backup(args.reason)
    elif args.command == "list":
        list_backups()
    elif args.command == "restore":
        restore(args.name, confirmed=args.yes)


if __name__ == "__main__":
    main()
