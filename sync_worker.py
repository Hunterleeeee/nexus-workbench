"""Durable worker for scheduled sync and low-risk automation.

The online API can run the same loop for small deployments.  Production may
set ``WORKBENCH_EXTERNAL_SYNC_WORKER=1`` on the API service and run this
process separately so a slow feed, panel sync, or automation rule cannot
consume the API event loop.
"""
from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timedelta, timezone
from typing import Any

from app import (
    auto_sync_sub2api_panel,
    automation_rules,
    execute_automation_rule,
    release_worker_lease,
    worker_lease,
)


WORKER_ID = "sync-worker"
POLL_SECONDS = max(30, int(os.getenv("WORKBENCH_SYNC_POLL_SECONDS", "30")))
STOP = False


def due(rule: dict[str, Any], now: datetime | None = None) -> bool:
    schedule = str(rule.get("schedule") or "")
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.now().astimezone().tzinfo)
    if schedule.startswith("daily:"):
        try:
            hour_text, minute_text = schedule.split(":", 1)[1].split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                return False
        except (TypeError, ValueError):
            return False
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if current < target:
            return False
        last = str(rule.get("last_run_at") or "")
        if not last:
            return True
        try:
            at = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return True
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at.astimezone(current.tzinfo) < target
    if not schedule.startswith("every:"):
        return False
    try:
        interval = max(30, int(schedule.split(":", 1)[1]))
    except (TypeError, ValueError):
        return False
    last = str(rule.get("last_run_at") or "")
    if not last:
        return True
    try:
        at = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) - at.astimezone(timezone.utc) >= timedelta(seconds=interval)


async def worker_loop() -> None:
    global STOP
    last_sub2api_sync = datetime.min.replace(tzinfo=timezone.utc)
    sync_interval = max(300, int(os.getenv("WORKBENCH_SUB2API_SYNC_INTERVAL_SECONDS", "1800")))
    while not STOP:
        lease = worker_lease(WORKER_ID, status="running", metadata={"poll_seconds": POLL_SECONDS})
        if lease.get("status") == "held_by_other_instance":
            await asyncio.sleep(POLL_SECONDS)
            continue
        completed = 0
        failed = 0
        if datetime.now(timezone.utc) - last_sub2api_sync >= timedelta(seconds=sync_interval):
            try:
                await auto_sync_sub2api_panel()
                completed += 1
            except Exception:
                # A missing panel credential is expected on a fresh install;
                # the worker remains healthy and retries on the next cycle.
                failed += 1
            last_sub2api_sync = datetime.now(timezone.utc)
        for rule in automation_rules():
            if STOP or not rule.get("enabled") or not due(rule):
                continue
            try:
                await execute_automation_rule(int(rule["id"]), trigger="sync-worker")
                completed += 1
            except Exception:
                failed += 1
        worker_lease(WORKER_ID, status="idle", metadata={"poll_seconds": POLL_SECONDS, "completed": completed, "failed": failed})
        for _ in range(POLL_SECONDS):
            if STOP:
                break
            await asyncio.sleep(1)
    release_worker_lease(WORKER_ID)


def request_stop(_signal: int, _frame: Any) -> None:
    global STOP
    STOP = True


def main() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
