"""Standalone Agent Worker for durable execution plans.

The API only validates and queues a plan when
``WORKBENCH_EXTERNAL_AGENT_WORKER=1``.  This process owns the LLM/action
boundary, writes the same Run/Relation/Notification records as the API path,
and can be restarted without losing queued plans.
"""
from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from app import (
    claim_next_execution_plan,
    release_worker_lease,
    run_execution_plan,
    worker_lease,
)


WORKER_ID = "agent-worker"
POLL_SECONDS = max(1, int(os.getenv("WORKBENCH_AGENT_POLL_SECONDS", "2")))
STOP = False


async def process_plan(plan: dict[str, Any]) -> None:
    plan_id = str(plan.get("id") or "")
    if not plan_id:
        return
    try:
        await run_execution_plan(plan_id)
    except Exception:
        # run_execution_plan persists the blocked/error state and notification.
        # Keep the worker alive so later plans can still be processed.
        return


async def worker_loop() -> None:
    global STOP
    while not STOP:
        lease = worker_lease(WORKER_ID, status="running", metadata={"poll_seconds": POLL_SECONDS})
        if lease.get("status") == "held_by_other_instance":
            await asyncio.sleep(POLL_SECONDS)
            continue
        plan = claim_next_execution_plan()
        if not plan:
            worker_lease(WORKER_ID, status="idle", metadata={"poll_seconds": POLL_SECONDS, "queue": "empty"})
            await asyncio.sleep(POLL_SECONDS)
            continue
        worker_lease(WORKER_ID, status="running", metadata={"poll_seconds": POLL_SECONDS, "plan_id": plan.get("id")})
        await process_plan(plan)
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
