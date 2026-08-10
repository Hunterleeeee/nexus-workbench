"""Durable Crawl4AI worker for the online Workbench deployment.

The Core API only creates queued ``agent_runs``.  This process claims those
runs with a SQLite transaction and a short worker lease, so an API restart does
not lose a research task and a dead worker can be recovered later.
"""
from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from app import (
    CrawlRequest,
    add_agent_run_event,
    claim_next_crawl_run,
    get_agent_run,
    recover_stale_crawl_runs,
    run_crawl,
    runs,
    runtime_crawl_from_agent_run,
    update_agent_run_record,
    worker_lease,
    release_worker_lease,
)


WORKER_ID = "crawl-worker"
CONCURRENCY = max(1, min(4, int(os.getenv("WORKBENCH_CRAWL_CONCURRENCY", "2"))))
POLL_SECONDS = max(1, int(os.getenv("WORKBENCH_CRAWL_POLL_SECONDS", "2")))
STOP = False


def request_from_run(run: dict[str, Any]) -> CrawlRequest:
    payload = dict(run.get("request") or {})
    allowed = {"urls", "task", "source_title", "source_context", "render_js", "refresh", "max_depth", "max_pages"}
    return CrawlRequest(**{key: payload.get(key) for key in allowed if key in payload})


async def process_run(durable: dict[str, Any]) -> None:
    run_id = str(durable.get("id") or "")
    if not run_id:
        return
    runtime = runtime_crawl_from_agent_run(durable)
    runs[run_id] = runtime
    try:
        request = request_from_run(durable)
        await run_crawl(run_id, request)
    except Exception as exc:  # run_crawl normally records failures itself.
        update_agent_run_record(run_id, status="failed", error=str(exc))
        add_agent_run_event(run_id, "failed", f"Crawl Worker 异常退出：{exc}", level="error")
    finally:
        runs.pop(run_id, None)


async def worker_loop() -> None:
    global STOP
    active: set[asyncio.Task[Any]] = set()
    while not STOP:
        lease = worker_lease(WORKER_ID, status="running", metadata={"concurrency": CONCURRENCY, "active": len(active)})
        if lease.get("status") == "held_by_other_instance":
            await asyncio.sleep(POLL_SECONDS)
            continue

        recover_stale_crawl_runs()

        while len(active) < CONCURRENCY:
            durable = claim_next_crawl_run()
            if not durable:
                break
            task = asyncio.create_task(process_run(durable))
            active.add(task)

        if active:
            done, pending = await asyncio.wait(active, timeout=POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            active = set(pending)
            for task in done:
                task.result()
        else:
            worker_lease(WORKER_ID, status="idle", metadata={"concurrency": CONCURRENCY, "active": 0})
            await asyncio.sleep(POLL_SECONDS)

    if active:
        await asyncio.gather(*active, return_exceptions=True)
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
