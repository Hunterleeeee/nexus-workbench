"""Workbench DB 层：连接管理 + 一次性的 schema 初始化/迁移。

从 app.py 拆出的第二个内核模块（为开源准备）。只依赖 app_pkg.core
（DATABASE_FILE / log / now_iso），不依赖任何业务领域模块。

兼容说明：`_DB_SCHEMA_READY` 标志保留在 app 模块（测试大量 patch 它），
`_ensure_db_schema()` 通过函数级延迟 import 读取/写入 app._DB_SCHEMA_READY，
避免 app ↔ db 循环导入，也让既有测试零改动。
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from typing import Any

from .core import log, now_iso


def _database_file():
    """运行时读取数据库路径。

    兼容拆分：测试通过 `patch app.DATABASE_FILE` 指向临时库（80+ 处），
    这里若用导入时的绑定值（core.DATABASE_FILE）就绕过了 patch，测试会
    污染真实数据库。运行时读 app 命名空间保证 patch 依然生效。
    """
    import app  # noqa: F401

    return app.DATABASE_FILE


DB_SCHEMA_VERSION = 10
_DB_SCHEMA_READY = False
_DB_SCHEMA_LOCK = threading.Lock()


def _initialize_database_schema() -> sqlite3.Connection:
    """Create/migrate the local schema once per process.

    This used to run on every read request. The homepage can make several
    reads during its first paint, so repeating all CREATE TABLE/PRAGMA checks
    there made the database look like the network bottleneck.
    """
    connection = sqlite3.connect(_database_file())
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'note',
            tags TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'inbox',
            priority TEXT NOT NULL DEFAULT 'normal',
            due_at TEXT NOT NULL DEFAULT '',
            classification TEXT NOT NULL DEFAULT '',
            classification_confidence REAL NOT NULL DEFAULT 0,
            duplicate_of INTEGER NOT NULL DEFAULT 0,
            analysis_json TEXT NOT NULL DEFAULT '{}',
            analyzed_at TEXT NOT NULL DEFAULT '',
            route_status TEXT NOT NULL DEFAULT 'none',
            triage_run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    inbox_columns = {row[1] for row in connection.execute("PRAGMA table_info(inbox)").fetchall()}
    inbox_migrations = {
        "priority": "TEXT NOT NULL DEFAULT 'normal'",
        "due_at": "TEXT NOT NULL DEFAULT ''",
        "classification": "TEXT NOT NULL DEFAULT ''",
        "classification_confidence": "REAL NOT NULL DEFAULT 0",
        "duplicate_of": "INTEGER NOT NULL DEFAULT 0",
        "analysis_json": "TEXT NOT NULL DEFAULT '{}'",
        "analyzed_at": "TEXT NOT NULL DEFAULT ''",
        "route_status": "TEXT NOT NULL DEFAULT 'none'",
        "triage_run_id": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT ''",
    }
    for column, declaration in inbox_migrations.items():
        if column not in inbox_columns:
            connection.execute(f"ALTER TABLE inbox ADD COLUMN {column} {declaration}")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS inbox_route_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_id INTEGER NOT NULL,
            target_project TEXT NOT NULL,
            route_kind TEXT NOT NULL DEFAULT 'handoff',
            reason TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'suggested',
            work_item_id INTEGER NOT NULL DEFAULT 0,
            relation_id INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_inbox_route_inbox ON inbox_route_candidates(inbox_id, status, updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS obsidian_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '',
            folder TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0,
            preview TEXT NOT NULL DEFAULT '',
            search_text TEXT NOT NULL DEFAULT '',
            frontmatter_json TEXT NOT NULL DEFAULT '{}',
            links_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            content_hash TEXT NOT NULL DEFAULT '',
            indexed_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    obsidian_columns = {row[1] for row in connection.execute("PRAGMA table_info(obsidian_notes)").fetchall()}
    if "search_text" not in obsidian_columns:
        connection.execute("ALTER TABLE obsidian_notes ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS obsidian_index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_obsidian_notes_mtime ON obsidian_notes(mtime DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS knowledge_conflict_resolutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conflict_key TEXT NOT NULL UNIQUE,
            action TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            left_path TEXT NOT NULL DEFAULT '',
            right_path TEXT NOT NULL DEFAULT '',
            left_hash TEXT NOT NULL DEFAULT '',
            right_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_conflicts_updated ON knowledge_conflict_resolutions(updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS knowledge_conflict_paragraph_resolutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conflict_key TEXT NOT NULL,
            paragraph_key TEXT NOT NULL,
            action TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            left_path TEXT NOT NULL DEFAULT '',
            right_path TEXT NOT NULL DEFAULT '',
            left_line_start INTEGER NOT NULL DEFAULT 0,
            left_line_end INTEGER NOT NULL DEFAULT 0,
            right_line_start INTEGER NOT NULL DEFAULT 0,
            right_line_end INTEGER NOT NULL DEFAULT 0,
            left_text TEXT NOT NULL DEFAULT '',
            right_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(conflict_key, paragraph_key)
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_paragraph_conflicts_updated ON knowledge_conflict_paragraph_resolutions(updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS work_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'task',
            status TEXT NOT NULL DEFAULT 'open',
            priority TEXT NOT NULL DEFAULT 'normal',
            source_project TEXT NOT NULL DEFAULT 'workbench',
            target_project TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    work_item_columns = {row[1] for row in connection.execute("PRAGMA table_info(work_items)").fetchall()}
    work_item_migrations = {
        "claimed_at": "TEXT NOT NULL DEFAULT ''",
        "claimed_run_id": "TEXT NOT NULL DEFAULT ''",
        "result_json": "TEXT NOT NULL DEFAULT '{}'",
        "completed_at": "TEXT NOT NULL DEFAULT ''",
        "last_error": "TEXT NOT NULL DEFAULT ''",
    }
    for column, declaration in work_item_migrations.items():
        if column not in work_item_columns:
            connection.execute(f"ALTER TABLE work_items ADD COLUMN {column} {declaration}")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            path TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'file',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_type TEXT NOT NULL,
            from_id TEXT NOT NULL,
            to_type TEXT NOT NULL,
            to_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agent_actions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            tool TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            risk TEXT NOT NULL DEFAULT 'low',
            requires_confirmation INTEGER NOT NULL DEFAULT 0,
            arguments_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    action_columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_actions)").fetchall()}
    if "run_id" not in action_columns:
        connection.execute("ALTER TABLE agent_actions ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_actions_run ON agent_actions(run_id, updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS idea_sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '未命名想法',
            status TEXT NOT NULL DEFAULT 'active',
            summary_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS idea_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS idea_hypotheses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            plan_version INTEGER NOT NULL DEFAULT 0,
            statement TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '需求',
            priority TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'unverified',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            success_metric TEXT NOT NULL DEFAULT '',
            stop_condition TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    idea_hypothesis_columns = {row[1] for row in connection.execute("PRAGMA table_info(idea_hypotheses)").fetchall()}
    if "plan_version" not in idea_hypothesis_columns:
        connection.execute("ALTER TABLE idea_hypotheses ADD COLUMN plan_version INTEGER NOT NULL DEFAULT 0")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_idea_hypotheses_session ON idea_hypotheses(session_id, updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS idea_validation_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            hypothesis_id INTEGER NOT NULL DEFAULT 0,
            plan_version INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            task_type TEXT NOT NULL DEFAULT 'interview',
            status TEXT NOT NULL DEFAULT 'open',
            due_at TEXT NOT NULL DEFAULT '',
            success_metric TEXT NOT NULL DEFAULT '',
            acceptance TEXT NOT NULL DEFAULT '',
            work_item_id INTEGER NOT NULL DEFAULT 0,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    idea_task_columns = {row[1] for row in connection.execute("PRAGMA table_info(idea_validation_tasks)").fetchall()}
    if "plan_version" not in idea_task_columns:
        connection.execute("ALTER TABLE idea_validation_tasks ADD COLUMN plan_version INTEGER NOT NULL DEFAULT 0")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_idea_validation_tasks_session ON idea_validation_tasks(session_id, updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS idea_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            verdict TEXT NOT NULL DEFAULT '先验证',
            rationale TEXT NOT NULL DEFAULT '',
            continue_if TEXT NOT NULL DEFAULT '',
            stop_if TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_idea_decisions_session ON idea_decisions(session_id, version DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agent_sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '未命名 Agent 会话',
            status TEXT NOT NULL DEFAULT 'active',
            summary_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_project ON agent_sessions(project_id, updated_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id, id ASC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS memory_items (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL DEFAULT 'default',
            scope TEXT NOT NULL DEFAULT 'global',
            project_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'preference',
            memory_key TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            value_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'candidate',
            confidence REAL NOT NULL DEFAULT 0.5,
            sensitivity TEXT NOT NULL DEFAULT 'normal',
            pinned INTEGER NOT NULL DEFAULT 0,
            source_type TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            evidence_text TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            last_used_at TEXT NOT NULL DEFAULT '',
            use_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(owner_id, scope, project_id, status, updated_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_key ON memory_items(owner_id, memory_key, status)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_events_item ON memory_events(memory_id, id DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL DEFAULT 'workbench',
            session_id TEXT NOT NULL DEFAULT '',
            parent_run_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'chat',
            status TEXT NOT NULL DEFAULT 'queued',
            attempt INTEGER NOT NULL DEFAULT 1,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            title TEXT NOT NULL DEFAULT '',
            request_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agent_run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            message TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_project ON agent_runs(project_id, created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id, created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_run_id, created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_run_events_run ON agent_run_events(run_id, id ASC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT 'workbench',
            kind TEXT NOT NULL DEFAULT 'info',
            level TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            href TEXT NOT NULL DEFAULT '',
            read_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(read_at, created_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS feishu_bindings (
            chat_id TEXT PRIMARY KEY,
            user_open_id TEXT NOT NULL DEFAULT '',
            user_name TEXT NOT NULL DEFAULT '',
            bound_at TEXT NOT NULL,
            last_active_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS feishu_event_receipts (
            event_key TEXT PRIMARY KEY,
            event_type TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_feishu_event_receipts_received_at ON feishu_event_receipts(received_at)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS sub2api_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unknown',
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_sub2api_snapshots_checked_at ON sub2api_snapshots(checked_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ok',
            watchlist_json TEXT NOT NULL DEFAULT '[]',
            quotes_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_market_snapshots_checked_at ON market_snapshots(checked_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS server_monitor_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unknown',
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_server_monitor_snapshots_checked_at ON server_monitor_snapshots(checked_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS aihot_feedback (
            item_id TEXT PRIMARY KEY,
            vote TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_aihot_feedback_updated_at ON aihot_feedback(updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS cid_dashboard_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL DEFAULT '',
            project_count INTEGER NOT NULL DEFAULT 0,
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_cid_dashboard_snapshots_repo ON cid_dashboard_snapshots(repo, created_at DESC)")
    connection.commit()
    return connection
def _initialize_extended_schema(connection: sqlite3.Connection) -> None:
    """Create the durable platform tables used by the next workbench round.

    The original schema intentionally focused on project data.  These tables
    keep orchestration, automation, approvals, push subscriptions and audit
    evidence separate so a feature can be retried or removed without touching
    existing Inbox, knowledge or project artifacts.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS automation_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'workbench',
            schedule TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            config_json TEXT NOT NULL DEFAULT '{}',
            last_run_at TEXT NOT NULL DEFAULT '',
            next_run_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'idle',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_automation_rules_enabled ON automation_rules(enabled, next_run_at)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS automation_runs (
            id TEXT PRIMARY KEY,
            rule_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            trigger TEXT NOT NULL DEFAULT 'manual',
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_automation_runs_rule ON automation_runs(rule_id, created_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS execution_plans (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source_project TEXT NOT NULL DEFAULT 'workbench',
            status TEXT NOT NULL DEFAULT 'draft',
            input_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS execution_plan_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL,
            step_key TEXT NOT NULL,
            title TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'workbench',
            kind TEXT NOT NULL DEFAULT 'agent',
            dependencies_json TEXT NOT NULL DEFAULT '[]',
            input_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            attempt INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            run_id TEXT NOT NULL DEFAULT '',
            work_item_id INTEGER NOT NULL DEFAULT 0,
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(plan_id, step_key)
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON execution_plan_steps(plan_id, id)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL DEFAULT '',
            auth TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            quiet_start TEXT NOT NULL DEFAULT '22:00',
            quiet_end TEXT NOT NULL DEFAULT '08:00',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    push_columns = {row[1] for row in connection.execute("PRAGMA table_info(push_subscriptions)").fetchall()}
    for column, declaration in {
        "failure_count": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "last_sent_at": "TEXT NOT NULL DEFAULT ''",
        "last_failed_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in push_columns:
            connection.execute(f"ALTER TABLE push_subscriptions ADD COLUMN {column} {declaration}")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS push_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            event_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            href TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            sent_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_push_deliveries_status ON push_deliveries(status, updated_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_push_deliveries_subscription ON push_deliveries(subscription_id, created_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS approval_requests (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            reviewer_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_approval_requests_status ON approval_requests(status, updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS approval_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id TEXT NOT NULL,
            from_status TEXT NOT NULL DEFAULT '',
            to_status TEXT NOT NULL DEFAULT '',
            reviewer_note TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_approval_events_approval ON approval_events(approval_id, created_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS server_action_executions (
            id TEXT PRIMARY KEY,
            approval_id TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            rollback_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            rolled_back_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    server_execution_columns = {row[1] for row in connection.execute("PRAGMA table_info(server_action_executions)").fetchall()}
    if "updated_at" not in server_execution_columns:
        connection.execute("ALTER TABLE server_action_executions ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_server_action_executions_approval ON server_action_executions(approval_id, created_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS evidence_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            edge_key TEXT NOT NULL,
            scenario TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            run_id TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_checks_edge ON evidence_checks(edge_key, created_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS llm_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id TEXT NOT NULL DEFAULT '',
            provider_name TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'succeeded',
            error_kind TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            run_id TEXT NOT NULL DEFAULT '',
            purpose TEXT NOT NULL DEFAULT 'agent',
            created_at TEXT NOT NULL
        )"""
    )
    llm_usage_columns = {row[1] for row in connection.execute("PRAGMA table_info(llm_usage_events)").fetchall()}
    if "purpose" not in llm_usage_columns:
        connection.execute("ALTER TABLE llm_usage_events ADD COLUMN purpose TEXT NOT NULL DEFAULT 'agent'")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage_events(created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_provider ON llm_usage_events(provider_id, created_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS llm_provider_health (
            provider_id TEXT PRIMARY KEY,
            last_success_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            last_error_kind TEXT NOT NULL DEFAULT '',
            last_error_at TEXT NOT NULL DEFAULT '',
            cooldown_until REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS worker_leases (
            worker_id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',
            lease_until TEXT NOT NULL DEFAULT '',
            last_heartbeat TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_worker_leases_heartbeat ON worker_leases(last_heartbeat DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS research_plans (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source_project TEXT NOT NULL DEFAULT 'crawl4ai',
            query TEXT NOT NULL DEFAULT '',
            urls_json TEXT NOT NULL DEFAULT '[]',
            steps_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'draft',
            current_run_id TEXT NOT NULL DEFAULT '',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_research_plans_updated ON research_plans(updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS inbox_classification_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_id INTEGER NOT NULL DEFAULT 0,
            predicted TEXT NOT NULL DEFAULT '',
            accepted TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_inbox_feedback_label ON inbox_classification_feedback(accepted, created_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS product_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            persona TEXT NOT NULL DEFAULT '',
            importance TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'new',
            linked_requirement_id INTEGER NOT NULL DEFAULT 0,
            artifact_id INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_product_feedback_status ON product_feedback(status, updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS product_requirements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            problem TEXT NOT NULL DEFAULT '',
            target_user TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'discovering',
            reach REAL NOT NULL DEFAULT 1,
            impact REAL NOT NULL DEFAULT 1,
            confidence REAL NOT NULL DEFAULT 50,
            effort REAL NOT NULL DEFAULT 1,
            score REAL NOT NULL DEFAULT 0,
            work_item_id INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_product_requirements_status ON product_requirements(status, score DESC, updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS product_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requirement_id INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            decision TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            alternatives TEXT NOT NULL DEFAULT '',
            revisit_trigger TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'decided',
            artifact_id INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )

    # 产品项目由用户自己定义，与工作台内置的 15 个项目无关——
    # 你在这里管的是"我的产品"，不是"工作台有哪些功能模块"。
    connection.execute(
        """CREATE TABLE IF NOT EXISTS product_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            color TEXT NOT NULL DEFAULT '',
            archived_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(name)
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_product_projects_status ON product_projects(status, updated_at DESC)")

    # 产品作战室原本只有一个扁平的需求池：没有项目维度，也没有缺陷这个概念。
    # 一个人同时维护十几个项目时，所有需求混在一张列表里等于没有优先级。
    requirement_columns = {row[1] for row in connection.execute("PRAGMA table_info(product_requirements)").fetchall()}
    for column, declaration in (
        ("project_id", "TEXT NOT NULL DEFAULT ''"),
        ("item_type", "TEXT NOT NULL DEFAULT 'requirement'"),
        ("severity", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in requirement_columns:
            connection.execute(f"ALTER TABLE product_requirements ADD COLUMN {column} {declaration}")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_product_requirements_project ON product_requirements(project_id, item_type, updated_at DESC)")

    feedback_columns = {row[1] for row in connection.execute("PRAGMA table_info(product_feedback)").fetchall()}
    if "project_id" not in feedback_columns:
        connection.execute("ALTER TABLE product_feedback ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_product_feedback_project ON product_feedback(project_id, updated_at DESC)")

    connection.execute("CREATE INDEX IF NOT EXISTS idx_product_decisions_requirement ON product_decisions(requirement_id, updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS product_prototypes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requirement_id INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            provider TEXT NOT NULL DEFAULT 'cowart',
            canvas_dir TEXT NOT NULL DEFAULT '',
            latest_version INTEGER NOT NULL DEFAULT 0,
            latest_artifact_id INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_product_prototypes_requirement ON product_prototypes(requirement_id, updated_at DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS product_prototype_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prototype_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            snapshot_path TEXT NOT NULL DEFAULT '',
            html_path TEXT NOT NULL DEFAULT '',
            preview_path TEXT NOT NULL DEFAULT '',
            artifact_id INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(prototype_id, version)
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_product_prototype_versions_prototype ON product_prototype_versions(prototype_id, version DESC)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ai_learning_profiles (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_role TEXT NOT NULL DEFAULT '',
            target_role TEXT NOT NULL DEFAULT '',
            experience TEXT NOT NULL DEFAULT 'beginner',
            focus TEXT NOT NULL DEFAULT 'work-efficiency',
            goal TEXT NOT NULL DEFAULT '',
            daily_minutes INTEGER NOT NULL DEFAULT 25,
            push_time TEXT NOT NULL DEFAULT '08:30',
            daily_push_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ai_learning_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_date TEXT NOT NULL UNIQUE,
            day_index INTEGER NOT NULL DEFAULT 1,
            module TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            content_json TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT 'curriculum',
            status TEXT NOT NULL DEFAULT 'ready',
            quiz_answer INTEGER NOT NULL DEFAULT -1,
            quiz_correct INTEGER NOT NULL DEFAULT 0,
            practice_output TEXT NOT NULL DEFAULT '',
            reflection TEXT NOT NULL DEFAULT '',
            confidence INTEGER NOT NULL DEFAULT 0,
            note_artifact_id INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    ai_learning_lesson_columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_learning_lessons)").fetchall()}
    if "practice_output" not in ai_learning_lesson_columns:
        connection.execute("ALTER TABLE ai_learning_lessons ADD COLUMN practice_output TEXT NOT NULL DEFAULT ''")
    # 练习成果此前只写不读——用户交了作业没人批，这正是「没达到学习目的」的核心。
    # feedback_json 保存 AI 批改结果，让反馈可回看、可追溯。
    if "feedback_json" not in ai_learning_lesson_columns:
        connection.execute("ALTER TABLE ai_learning_lessons ADD COLUMN feedback_json TEXT NOT NULL DEFAULT '{}'")
    # 多学习轨道：原表把 lesson_date 声明为全局 UNIQUE，两个轨道在同一天各上一课
    # 就会撞主键。SQLite 改不了已有约束，只能重建表。旧数据全部归到 AI 转型轨道。
    if "track" not in ai_learning_lesson_columns:
        connection.execute("ALTER TABLE ai_learning_lessons RENAME TO ai_learning_lessons_pre_track")
        connection.execute(
            """CREATE TABLE ai_learning_lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track TEXT NOT NULL DEFAULT 'ai-transformation',
                lesson_date TEXT NOT NULL,
                day_index INTEGER NOT NULL DEFAULT 1,
                module TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                content_json TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL DEFAULT 'curriculum',
                status TEXT NOT NULL DEFAULT 'ready',
                quiz_answer INTEGER NOT NULL DEFAULT -1,
                quiz_correct INTEGER NOT NULL DEFAULT 0,
                practice_output TEXT NOT NULL DEFAULT '',
                reflection TEXT NOT NULL DEFAULT '',
                confidence INTEGER NOT NULL DEFAULT 0,
                note_artifact_id INTEGER NOT NULL DEFAULT 0,
                feedback_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(track, lesson_date)
            )"""
        )
        # 旧表可能缺任意列（早期版本的结构更简单），所以逐列决定取值：
        # 有就搬过来，没有就用新表的默认值填，避免 NOT NULL 约束在迁移时炸掉。
        migration_stamp = now_iso()
        target_defaults: dict[str, str] = {
            "id": "NULL", "lesson_date": "''", "day_index": "1", "module": "''", "title": "'历史课程'",
            "content_json": "'{}'", "source": "'curriculum'", "status": "'ready'", "quiz_answer": "-1",
            "quiz_correct": "0", "practice_output": "''", "reflection": "''", "confidence": "0",
            "note_artifact_id": "0", "feedback_json": "'{}'",
            "started_at": "''", "completed_at": "''",
            "created_at": f"'{migration_stamp}'", "updated_at": f"'{migration_stamp}'",
        }
        columns = ", ".join(target_defaults)
        selects = ", ".join(
            name if name in ai_learning_lesson_columns else default
            for name, default in target_defaults.items()
        )
        connection.execute(
            f"INSERT INTO ai_learning_lessons (track, {columns}) SELECT 'ai-transformation', {selects} FROM ai_learning_lessons_pre_track"
        )
        connection.execute("DROP TABLE ai_learning_lessons_pre_track")
        log.info("ai_learning_lessons 已迁移到多轨道结构")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_ai_learning_lessons_track ON ai_learning_lessons(track, lesson_date DESC)")

    # 档案表原本带 CHECK (id = 1) 的单行约束，多轨道下每条轨道要有自己的档案
    # （目标岗位、每日时长、推送时间都可能不同），同样只能重建。
    profile_columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_learning_profiles)").fetchall()}
    # 判据看约束本身而不是列是否存在：单纯 ALTER TABLE 加列并不会去掉
    # CHECK (id = 1)，那样列有了、插第二行照样失败。
    profile_sql = str((connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ai_learning_profiles'"
    ).fetchone() or [""])[0] or "")
    if "track" not in profile_columns or "CHECK (id = 1)" in profile_sql.replace("check", "CHECK"):
        connection.execute("ALTER TABLE ai_learning_profiles RENAME TO ai_learning_profiles_pre_track")
        connection.execute(
            """CREATE TABLE ai_learning_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track TEXT NOT NULL DEFAULT 'ai-transformation' UNIQUE,
                current_role TEXT NOT NULL DEFAULT '',
                target_role TEXT NOT NULL DEFAULT '',
                experience TEXT NOT NULL DEFAULT 'beginner',
                focus TEXT NOT NULL DEFAULT 'work-efficiency',
                goal TEXT NOT NULL DEFAULT '',
                daily_minutes INTEGER NOT NULL DEFAULT 25,
                push_time TEXT NOT NULL DEFAULT '08:30',
                daily_push_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        profile_stamp = now_iso()
        profile_defaults: dict[str, str] = {
            "current_role": "''", "target_role": "''", "experience": "'beginner'",
            "focus": "'work-efficiency'", "goal": "''", "daily_minutes": "25",
            "push_time": "'08:30'", "daily_push_enabled": "1",
            "created_at": f"'{profile_stamp}'", "updated_at": f"'{profile_stamp}'",
        }
        profile_names = ", ".join(profile_defaults)
        profile_selects = ", ".join(
            name if name in profile_columns else default for name, default in profile_defaults.items()
        )
        connection.execute(
            f"INSERT INTO ai_learning_profiles (track, {profile_names}) "
            f"SELECT 'ai-transformation', {profile_selects} FROM ai_learning_profiles_pre_track LIMIT 1"
        )
        connection.execute("DROP TABLE ai_learning_profiles_pre_track")
        log.info("ai_learning_profiles 已迁移到多轨道结构")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_ai_learning_lessons_status ON ai_learning_lessons(status, lesson_date DESC)")

    # ------------------------------------------------------------------
    # Agent 消息队列
    #
    # 在这之前，每一次 Agent 调用都在 HTTP 请求里同步跑完：一次总调度最坏
    # 是几十次 LLM 调用、几百次工具调用，浏览器那端只能干等；请求断了任务
    # 也就没了下文；进程一重启，正在跑的 run 永远停在 running。
    #
    # 队列把「提交」和「执行」拆开：提交只写一行、立刻返回，执行由 worker
    # 按顺序取。带来的三件事都是原来做不到的——
    #   · 排队中的任务能看见、能取消；
    #   · 进程重启后没跑完的任务还在队列里，会被重新领走；
    #   · 任务跑到一半可以往它的队列里插一条消息（「顺便也看看 X」），
    #     下一轮循环就会读到，而不用等它跑完再重新发一遍。
    # ------------------------------------------------------------------
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agent_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue TEXT NOT NULL DEFAULT 'default',
            project_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'chat',
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            priority INTEGER NOT NULL DEFAULT 100,
            attempt INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at TEXT NOT NULL DEFAULT '',
            claimed_by TEXT NOT NULL DEFAULT '',
            claimed_at TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            result_json TEXT NOT NULL DEFAULT '{}',
            dedupe_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    # 取任务的那条查询就是按这三列排的，没有索引会随队列增长线性劣化。
    connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_queue_pick ON agent_queue(status, priority, id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_queue_run ON agent_queue(run_id, id)")
    # 去重键防的是「同一件事被连点两下提交两遍」，只对还没做完的行生效——
    # 做完之后同样的请求应该能再提一次。
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_queue_dedupe ON agent_queue(dedupe_key) "
        "WHERE dedupe_key <> '' AND status IN ('queued', 'running')"
    )

    # 插入消息：往一个「正在跑」的任务里追加指令。
    # 单独一张表而不是塞进 payload：payload 是提交时就固定下来的，
    # 而插入的消息是任务跑起来之后才产生的，两者的生命周期不一样。
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agent_queue_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_id INTEGER NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            consumed_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_agent_queue_messages ON agent_queue_messages(queue_id, consumed_at, id)")

    # 主动学习：不属于 14 天路线，所以不能塞进 ai_learning_lessons——那张表有
    # UNIQUE(track, lesson_date)，一天只能有一节。想查个名词就把今天的课冲掉，
    # 显然不行。单独一张表，随时可以查、可以查很多次。
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ai_learning_explorations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track TEXT NOT NULL DEFAULT 'ai-transformation',
            kind TEXT NOT NULL DEFAULT 'term',
            topic TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            content_json TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT 'llm',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_ai_learning_explorations ON ai_learning_explorations(track, id DESC)")

    # AI 出题：一道题一行，带参考答案和评分标准，用户作答后写回 feedback。
    # 和「练习成果」分开存，因为练习是「你把工作里的产出贴进来」，出题是
    # 「你手上没有场景，我给你一个」——后者可以反复做，前者一节课只有一次。
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ai_learning_exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track TEXT NOT NULL DEFAULT 'ai-transformation',
            lesson_id INTEGER NOT NULL DEFAULT 0,
            exploration_id INTEGER NOT NULL DEFAULT 0,
            topic TEXT NOT NULL DEFAULT '',
            question TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '',
            reference_answer TEXT NOT NULL DEFAULT '',
            criteria_json TEXT NOT NULL DEFAULT '[]',
            user_answer TEXT NOT NULL DEFAULT '',
            feedback_json TEXT NOT NULL DEFAULT '{}',
            score INTEGER NOT NULL DEFAULT -1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_ai_learning_exercises ON ai_learning_exercises(track, id DESC)")

    # ------------------------------------------------------------------
    # Hot-path indexes for the five core tables that had none.
    #
    # inbox / work_items / relations / artifacts are read on almost every
    # page load (首页待办、联动矩阵、交接记录、产物列表).  Without these
    # SQLite full-scans the table and then sorts the whole result set just to
    # return the newest 200 rows, which is the dominant cost of "/" and
    # "/api/work-items" once the tables pass a few thousand rows.
    # ------------------------------------------------------------------
    for index_sql in (
        # inbox: WHERE status = ? + ORDER BY priority/due_at/created_at, plus daily counters.
        "CREATE INDEX IF NOT EXISTS idx_inbox_status_id ON inbox(status, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_inbox_created_at ON inbox(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_inbox_updated_at ON inbox(updated_at DESC, id DESC)",
        # work_items: the homepage list, per-status queues and per-kind lookups.
        "CREATE INDEX IF NOT EXISTS idx_work_items_updated_at ON work_items(updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_work_items_kind ON work_items(kind, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_work_items_created_at ON work_items(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_work_items_target_project ON work_items(target_project, updated_at DESC)",
        # relations: `WHERE from_id = ? OR to_id = ?` needs one index per side so
        # SQLite can run its OR-optimization instead of scanning the table twice.
        "CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_relations_created_at ON relations(created_at DESC)",
        # artifacts: global newest-first list and the per-project variant.
        "CREATE INDEX IF NOT EXISTS idx_artifacts_created_at ON artifacts(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id, created_at DESC)",
    ):
        try:
            connection.execute(index_sql)
        except sqlite3.Error as exc:  # A partially migrated table must not block startup.
            log.warning("跳过索引创建：%s（%s）", index_sql, exc)

    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    current_schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
    timestamp = now_iso()
    target_schema_version = max(current_schema_version, DB_SCHEMA_VERSION)
    for version in range(current_schema_version + 1, target_schema_version + 1):
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, timestamp))
    connection.execute(f"PRAGMA user_version = {target_schema_version}")
    connection.commit()


def _open_db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(_database_file(), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        # journal_mode 是持久化在数据库文件头里的，_ensure_db_schema() 设过一次
        # 之后所有连接都自动是 WAL，这里再设一遍纯属白跑（首页一次要开几十个
        # 连接，实测占到连接建立开销的两成）。synchronous 才是每连接生效的。
        connection.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.Error:
        # A read-only or exotic filesystem may refuse the pragma; keep going.
        pass
    return connection


class _SharedConnection:
    """Proxy whose ``close()`` is a no-op, so a shared connection survives it.

    Every helper in this module ends with ``finally: connection.close()``.  That
    is correct when each helper owns its connection, but it makes connection
    reuse impossible.  Handing out this proxy inside a ``db_scope()`` lets the
    existing code stay exactly as it is while the real connection is opened once.
    """

    __slots__ = ("_real",)

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def close(self) -> None:  # noqa: D401 - deliberately does nothing
        return None


_db_scope_state = threading.local()


@contextlib.contextmanager
def db_scope() -> Any:
    """Reuse one SQLite connection for everything inside the block.

    Only wrap **read-only** aggregations in this.  Rendering the home page used
    to open 66 connections (15 project cards x several helpers each); at
    ~0.32 ms per open that was ~21 ms of pure setup before a single row was read.

    Re-entrant: nested ``db_scope()`` calls share the outermost connection.
    """
    depth = getattr(_db_scope_state, "depth", 0)
    if depth == 0:
        _ensure_db_schema()
        _db_scope_state.connection = _open_db_connection()
    _db_scope_state.depth = depth + 1
    try:
        yield
    finally:
        _db_scope_state.depth -= 1
        if _db_scope_state.depth == 0:
            shared = getattr(_db_scope_state, "connection", None)
            _db_scope_state.connection = None
            if shared is not None:
                try:
                    shared.close()
                except sqlite3.Error:
                    log.warning("关闭共享数据库连接失败", exc_info=True)


def db_connection() -> sqlite3.Connection:
    """Open a lightweight connection after one-time schema initialization."""
    _ensure_db_schema()
    if getattr(_db_scope_state, "depth", 0) > 0:
        shared = getattr(_db_scope_state, "connection", None)
        if shared is not None:
            return _SharedConnection(shared)  # type: ignore[return-value]
    return _open_db_connection()



def _ensure_db_schema() -> None:
    # 兼容拆分：_DB_SCHEMA_READY 标志保留在 app 模块（测试大量 patch 它），
    # 这里延迟 import 避免 app ↔ db 循环导入。
    import app  # noqa: F401

    if app._DB_SCHEMA_READY:
        return
    with _DB_SCHEMA_LOCK:
        if app._DB_SCHEMA_READY:
            return
        connection = _initialize_database_schema()
        _initialize_extended_schema(connection)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            log.warning("无法启用 WAL，将使用默认 journal 模式", exc_info=True)
        connection.close()
        app._DB_SCHEMA_READY = True


__all__ = ["db_connection", "db_scope"]
