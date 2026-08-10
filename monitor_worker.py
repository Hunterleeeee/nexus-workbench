"""Durable read-only server monitor worker for the online Workbench.

The API remains responsible for on-demand checks.  This process owns the
periodic check so a slow SSH probe or a temporary API restart does not block
the rest of the platform.  It never restarts services or changes server state.
"""
from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from app import (
    evaluate_server_monitor,
    now_iso,
    read_server_monitor,
    record_server_monitor_snapshot,
    register_artifact_safely,
    save_server_monitor_snapshot,
    worker_lease,
    release_worker_lease,
    SERVER_MONITOR_SNAPSHOT_FILE,
)


WORKER_ID = "monitor-worker"
INTERVAL_SECONDS = max(60, int(os.getenv("WORKBENCH_MONITOR_INTERVAL_SECONDS", "300")))
STOP = False


def run_once() -> dict[str, Any]:
    try:
        snapshot = read_server_monitor()
    except Exception as exc:
        snapshot = {"status": "error", "error": str(exc), "checked_at": now_iso()}
    save_server_monitor_snapshot(snapshot)
    record_server_monitor_snapshot(snapshot)
    artifact = register_artifact_safely(
        project_id="server",
        name="server_monitor_snapshot.json",
        path=str(SERVER_MONITOR_SNAPSHOT_FILE),
        kind="server_snapshot",
        metadata={"status": snapshot.get("status"), "checked_at": snapshot.get("checked_at"), "worker": WORKER_ID},
    )
    evaluation = evaluate_server_monitor(snapshot, create_records=True)
    return {"checked_at": snapshot.get("checked_at"), "status": snapshot.get("status"), "health_score": evaluation.get("analysis", {}).get("health_score"), "alerts": len(evaluation.get("analysis", {}).get("alerts") or []), "artifact_id": artifact.get("id") if artifact else None}


async def worker_loop() -> None:
    global STOP
    first_run = True
    while not STOP:
        lease = worker_lease(WORKER_ID, status="running", metadata={"interval_seconds": INTERVAL_SECONDS, "last_cycle": ""})
        if lease.get("status") == "held_by_other_instance":
            await asyncio.sleep(min(INTERVAL_SECONDS, 60))
            continue
        result = await asyncio.to_thread(run_once)
        worker_lease(WORKER_ID, status="idle", metadata={"interval_seconds": INTERVAL_SECONDS, "last_cycle": result})
        if first_run:
            first_run = False
        for _ in range(INTERVAL_SECONDS):
            if STOP:
                break
            await asyncio.sleep(1)
            if _ % 60 == 59:
                worker_lease(WORKER_ID, status="idle", metadata={"interval_seconds": INTERVAL_SECONDS, "last_cycle": result})
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
