"""Workbench 领域模块（app.py 拆分）。"""

from __future__ import annotations

import asyncio
import json
import re
import os
import sqlite3
from datetime import datetime, timezone
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from .instance import app
from .core import _int_env, clip, decode_json_value, log, now_iso


AGENT_QUEUE_LEASE_SECONDS = _int_env("WORKBENCH_AGENT_QUEUE_LEASE_SECONDS", 900, minimum=60, maximum=7200)


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def agent_queue_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = decode_json_value(item.pop("payload_json", "{}"), {}) or {}
    item["result"] = decode_json_value(item.pop("result_json", "{}"), {}) or {}
    item["cancellable"] = item.get("status") in {"queued", "running"}
    return item


def agent_queue_task_cancelled(task_id: int) -> bool:
    """运行中的 ReAct 循环在每轮工具之间查这个：任务是否已被用户取消。"""
    connection = _app_call('db_connection', )
    try:
        row = connection.execute("SELECT status FROM agent_queue WHERE id = ?", (task_id,)).fetchone()
        return bool(row and row["status"] == "cancelled")
    finally:
        connection.close()


def enqueue_agent_task(
    *,
    kind: str,
    payload: dict[str, Any],
    project_id: str = "",
    session_id: str = "",
    queue: str = "default",
    priority: int = 100,
    max_attempts: int = 3,
    dedupe_key: str = "",
) -> dict[str, Any]:
    """把一次 Agent 调用放进队列，立刻返回。

    去重键命中还没做完的同一件事时，直接返回既有那条，而不是排两遍——
    「提交」这个动作在网络抖动或用户连点时天然会重复。
    """
    timestamp = now_iso()
    connection = _app_call('db_connection', )
    try:
        if dedupe_key:
            existing = connection.execute(
                "SELECT * FROM agent_queue WHERE dedupe_key = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1",
                (dedupe_key,),
            ).fetchone()
            if existing:
                return {**agent_queue_row(existing), "deduped": True}
        cursor = connection.execute(
            """INSERT INTO agent_queue
            (queue, project_id, session_id, kind, payload_json, status, priority, max_attempts,
             available_at, dedupe_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
            (queue, project_id, session_id, kind, json.dumps(payload or {}, ensure_ascii=False),
             int(priority), int(max_attempts), timestamp, dedupe_key, timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM agent_queue WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return agent_queue_row(row)
    finally:
        connection.close()


def claim_agent_task(worker_id: str, queue: str = "default") -> dict[str, Any] | None:
    """领一个任务。

    用「UPDATE … WHERE id = ? AND status = 'queued'」+ 检查 rowcount 来抢，
    而不是先 SELECT 再 UPDATE：多个 worker 同时取的时候，先查后改会让两个
    worker 领到同一条。租约到期的 running 任务也会被重新放回队列——worker
    进程被杀时不会有人来标记，只能靠租约过期兜底。
    """
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    lease_until = (now + timedelta(seconds=AGENT_QUEUE_LEASE_SECONDS)).isoformat()
    connection = _app_call('db_connection', )
    try:
        # 先把租约过期的 running 放回队列。
        connection.execute(
            """UPDATE agent_queue SET status = 'queued', claimed_by = '', lease_until = '', updated_at = ?
            WHERE status = 'running' AND lease_until <> '' AND lease_until < ?""",
            (timestamp, timestamp),
        )
        connection.commit()
        for _ in range(5):
            row = connection.execute(
                """SELECT id FROM agent_queue
                WHERE status = 'queued' AND queue = ? AND (available_at = '' OR available_at <= ?)
                ORDER BY priority ASC, id ASC LIMIT 1""",
                (queue, timestamp),
            ).fetchone()
            if not row:
                return None
            cursor = connection.execute(
                """UPDATE agent_queue
                SET status = 'running', claimed_by = ?, claimed_at = ?, lease_until = ?,
                    attempt = attempt + 1, updated_at = ?
                WHERE id = ? AND status = 'queued'""",
                (worker_id, timestamp, lease_until, timestamp, row["id"]),
            )
            connection.commit()
            if cursor.rowcount:
                claimed = connection.execute("SELECT * FROM agent_queue WHERE id = ?", (row["id"],)).fetchone()
                return agent_queue_row(claimed)
        return None
    finally:
        connection.close()


def finish_agent_task(task_id: int, *, status: str, result: dict[str, Any] | None = None, error: str = "") -> dict[str, Any] | None:
    """结束一个任务。失败且还有重试次数时放回队列，并按次数退避。"""
    timestamp = now_iso()
    connection = _app_call('db_connection', )
    try:
        row = connection.execute("SELECT * FROM agent_queue WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        if status == "failed" and int(row["attempt"]) < int(row["max_attempts"]):
            # 退避：立刻重试多半会撞上同一个原因（限流、上游抖动）。
            delay = min(300, 15 * (2 ** max(0, int(row["attempt"]) - 1)))
            available_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            connection.execute(
                """UPDATE agent_queue SET status = 'queued', error = ?, available_at = ?,
                claimed_by = '', lease_until = '', updated_at = ? WHERE id = ?""",
                (clip(error, 800), available_at, timestamp, task_id),
            )
        else:
            connection.execute(
                """UPDATE agent_queue SET status = ?, error = ?, result_json = ?, lease_until = '', updated_at = ?
                WHERE id = ?""",
                (status, clip(error, 800), json.dumps(result or {}, ensure_ascii=False), timestamp, task_id),
            )
        connection.commit()
        return agent_queue_row(connection.execute("SELECT * FROM agent_queue WHERE id = ?", (task_id,)).fetchone())
    finally:
        connection.close()


def cancel_agent_task(task_id: int) -> dict[str, Any] | None:
    """取消排队或运行中的任务。

    排队中的直接改为 cancelled；运行中的任务标成 cancelled 后，ReAct 循环
    会在每轮工具之间读到这个状态并停下来（不会杀掉正在进行的单次 LLM/工具
    调用，但不会进入下一轮）。
    """
    timestamp = now_iso()
    connection = _app_call('db_connection', )
    try:
        cursor = connection.execute(
            "UPDATE agent_queue SET status = 'cancelled', updated_at = ? WHERE id = ? AND status IN ('queued', 'running')",
            (timestamp, task_id),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM agent_queue WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        item = agent_queue_row(row)
        item["cancelled"] = bool(cursor.rowcount)
        return item
    finally:
        connection.close()


def list_agent_tasks(status: str = "", queue: str = "", limit: int = 50) -> list[dict[str, Any]]:
    clauses, params = [], []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if queue:
        clauses.append("queue = ?")
        params.append(queue)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = _app_call('db_connection', )
    try:
        rows = connection.execute(
            f"SELECT * FROM agent_queue {where} ORDER BY id DESC LIMIT ?",
            (*params, max(1, min(200, limit))),
        ).fetchall()
        return [agent_queue_row(row) for row in rows]
    finally:
        connection.close()


def insert_agent_queue_message(task_id: int, content: str) -> dict[str, Any]:
    """往一个排队中或正在跑的任务里插一条消息。

    这是队列真正比「同步跑一次」多出来的能力：任务跑到一半你想起还要看一个
    东西，可以直接追加，下一轮循环就会读到——而不是等它跑完再重新发一遍，
    也不是把它取消掉重来。
    """
    text = str(content or "").strip()
    if not text:
        raise HTTPException(400, "插入的消息不能为空")
    timestamp = now_iso()
    connection = _app_call('db_connection', )
    try:
        row = connection.execute("SELECT status, run_id FROM agent_queue WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "队列任务不存在")
        if row["status"] not in {"queued", "running"}:
            raise HTTPException(409, f"任务已经是「{row['status']}」，插入的消息不会被读到")
        cursor = connection.execute(
            "INSERT INTO agent_queue_messages (queue_id, run_id, content, created_at) VALUES (?, ?, ?, ?)",
            (task_id, str(row["run_id"] or ""), clip(text, 4000), timestamp),
        )
        connection.commit()
        return {"id": int(cursor.lastrowid or 0), "queue_id": task_id, "content": clip(text, 4000), "created_at": timestamp}
    finally:
        connection.close()


def consume_agent_queue_messages(task_id: int) -> list[str]:
    """取走并标记这个任务当前累积的插入消息。

    先读后标记，同一条不会被消费两次；标记用的是同一个连接同一个事务，
    中途崩溃的话消息还在，下一轮会重新读到——宁可重复读，不可丢。
    """
    timestamp = now_iso()
    connection = _app_call('db_connection', )
    try:
        rows = connection.execute(
            "SELECT id, content FROM agent_queue_messages WHERE queue_id = ? AND consumed_at = '' ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        if not rows:
            return []
        connection.executemany(
            "UPDATE agent_queue_messages SET consumed_at = ? WHERE id = ?",
            [(timestamp, row["id"]) for row in rows],
        )
        connection.commit()
        return [str(row["content"]) for row in rows]
    finally:
        connection.close()


def recover_stuck_agent_runs() -> int:
    """把「进程重启后再也不会有人推进」的 run 标成失败。

    回收逻辑此前只覆盖 kind='crawl'：dispatch、dispatch_child、chat、action、
    handoff 这些 run 一旦在进程被杀/重启时正处于 running，就永远停在那里。
    更糟的是它们会反过来影响路由——agent_run_summary 把 queued+running 记为
    active，capability_graph_route 按 active 扣分，几个僵尸 run 就能让一个项目
    基本再也不被路由到，而且没有任何地方会提示。

    判断依据是「更新时间早于本进程启动时间」：run 是在内存里推进的，进程一换，
    上一个进程留下的 running 就不可能再有人接手。
    """
    import app as _app
    cutoff = _app._PROCESS_STARTED_AT
    recovered: list[str] = []
    connection = _app_call('db_connection', )
    try:
        rows = connection.execute(
            """SELECT id, project_id, kind FROM agent_runs
            WHERE status IN ('running', 'queued')
              AND kind NOT IN ('crawl')
              AND COALESCE(NULLIF(updated_at, ''), created_at) < ?""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE agent_runs SET status = 'failed', error = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                ("工作台重启时这次运行还没结束，已标记为失败；需要的话可以重试。", now_iso(), now_iso(), row["id"]),
            )
            recovered.append(str(row["id"]))
        connection.commit()
    finally:
        connection.close()
    for run_id in recovered:
        _app_call('add_agent_run_event', run_id, "failed", "工作台重启，这次运行被中断。", level="warning")
    return len(recovered)


class AgentQueueEnqueueRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=8000)
    session_id: str = Field(default="", max_length=80)
    priority: int = Field(default=100, ge=1, le=999)
    dedupe_key: str = Field(default="", max_length=120)


class AgentQueueMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


@app.get("/api/agent/queue")
def get_agent_queue(status: str = "", queue: str = "", limit: int = 50) -> dict[str, Any]:
    tasks = list_agent_tasks(status=status, queue=queue, limit=limit)
    counts: dict[str, int] = {}
    for item in list_agent_tasks(status="all", limit=200):
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"tasks": tasks, "counts": counts, "lease_seconds": AGENT_QUEUE_LEASE_SECONDS}


@app.post("/api/agent/queue")
def post_agent_queue(request: AgentQueueEnqueueRequest) -> dict[str, Any]:
    """提交一次 Agent 调用，立刻返回。

    同步跑完再返回是原来的做法：一次总调度最坏几十次 LLM 调用，浏览器只能
    干等，请求一断任务就没了下文。入队之后提交是一瞬间的事，跑得怎么样去
    队列里看。
    """
    _app_call('require_project_agent', request.project_id)
    task = enqueue_agent_task(
        kind="chat",
        payload={"message": request.message, "session_id": request.session_id},
        project_id=request.project_id,
        session_id=request.session_id,
        priority=request.priority,
        dedupe_key=request.dedupe_key,
    )
    return {"task": task}


@app.post("/api/agent/queue/{task_id}/messages")
def post_agent_queue_message(task_id: int, request: AgentQueueMessageRequest) -> dict[str, Any]:
    """往排队中或正在跑的任务里插一条消息。"""
    return {"message": insert_agent_queue_message(task_id, request.content)}


@app.post("/api/agent/queue/{task_id}/cancel")
def post_agent_queue_cancel(task_id: int) -> dict[str, Any]:
    task = cancel_agent_task(task_id)
    if not task:
        raise HTTPException(404, "队列任务不存在")
    if not task.get("cancelled"):
        raise HTTPException(409, f"任务已经是「{task['status']}」，取消不了。正在跑的任务停不下来——它可能正在调 LLM 或写库。")
    return {"task": task}


async def run_queued_agent_task(task: dict[str, Any]) -> dict[str, Any]:
    """执行一条队列任务。"""
    payload = task.get("payload") or {}
    project_id = str(task.get("project_id") or "")
    message = str(payload.get("message") or "")
    _app_call('require_project_agent', project_id)
    session = _app_call('get_agent_session', str(payload.get("session_id") or ""), project_id) if payload.get("session_id") else None
    if not session:
        session = await asyncio.to_thread(create_agent_session, project_id, message)
    await asyncio.to_thread(add_agent_message, session["id"], "user", message, {"source": "agent_queue", "queue_id": task["id"]})
    run = await asyncio.to_thread(
        create_agent_run_record,
        project_id=project_id,
        session_id=session["id"],
        kind="chat",
        title=clip(message, 120),
        request={"queue_id": task["id"], "message": message},
        max_attempts=1,
    )
    # 把 run_id 写回队列行：插进来的消息要能顺着它找到这次运行的事件流。
    connection = _app_call('db_connection', )
    try:
        connection.execute("UPDATE agent_queue SET run_id = ?, updated_at = ? WHERE id = ?",
                           (run["id"], now_iso(), task["id"]))
        connection.commit()
    finally:
        connection.close()
    return await _app_call('run_project_agent', 
        project_id=project_id, session=session, run=run, message=message,
        context={"source": "agent_queue"}, queue_task_id=int(task["id"]),
    )


async def agent_queue_worker_loop() -> None:
    """队列消费循环。

    只在主进程里跑一份：worker 之间靠 claim_agent_task 的原子 UPDATE 抢任务，
    所以多开几份也不会重复执行，但没必要。
    """
    worker_id = f"queue-{os.getpid()}"
    while True:
        try:
            task = await asyncio.to_thread(claim_agent_task, worker_id)
            if not task:
                await asyncio.sleep(3)
                continue
            log.info("队列任务 %s 开始执行（%s）", task["id"], task.get("project_id"))
            try:
                result = await run_queued_agent_task(task)
                await asyncio.to_thread(finish_agent_task, task["id"], status="succeeded",
                                        result={"run_id": (result.get("run") or {}).get("id", ""),
                                                "session_id": (result.get("session") or {}).get("id", ""),
                                                "answer": clip(str(result.get("message", {}).get("content", "")), 2000)})
            except Exception as exc:  # noqa: BLE001 - 单条任务失败不能终结循环
                log.warning("队列任务 %s 执行失败：%s", task["id"], exc, exc_info=True)
                await asyncio.to_thread(finish_agent_task, task["id"], status="failed", error=clip(str(exc), 800))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("队列循环异常", exc_info=True)
            await asyncio.sleep(5)


__all__ = [
    "AGENT_QUEUE_LEASE_SECONDS",
    "AgentQueueEnqueueRequest",
    "AgentQueueMessageRequest",
    "agent_queue_row",
    "agent_queue_task_cancelled",
    "agent_queue_worker_loop",
    "cancel_agent_task",
    "claim_agent_task",
    "consume_agent_queue_messages",
    "enqueue_agent_task",
    "finish_agent_task",
    "get_agent_queue",
    "insert_agent_queue_message",
    "list_agent_tasks",
    "post_agent_queue",
    "post_agent_queue_cancel",
    "post_agent_queue_message",
    "recover_stuck_agent_runs",
    "run_queued_agent_task",
]
