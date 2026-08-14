"""Workbench 行情领域：行情数据层/今日卡片/决策中心/选股/风格/采样/快照刷新。

从 app.py 拆出的 market 模块（为开源准备）。数据层（watchlist/snapshot/symbol
工具/腾讯行情抓取）与主区路由（suggest/state/today/decision-center/watchlist
rules/styles/screen/sampling/report/refresh/observations）。automations 总调度经
_app_call("refresh_market_quotes") 调用。策略区/附加区路由（research/backtest/
valuation 等）随后续批次并入。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

from .automations import automation_rules, list_automation_runs, save_automation_rule
from .core import (
    MARKET_SNAPSHOT_FILE,
    MARKET_WATCHLIST_FILE,
    OUTPUTS_DIR,
    clip,
    load_json_file,
    log,
    now_iso,
    save_json_atomic,
)
from .db import db_connection
from .instance import app
from .llm import call_llm, llm_settings
from .notifications import create_notification_record
from .sub2api import _sub2api_timestamp



def _OUTPUTS_DIR():
    """运行时读 app.OUTPUTS_DIR——测试 patch app.OUTPUTS_DIR 时生效。"""
    import app as _app

    return _app.OUTPUTS_DIR


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def load_market_watchlist() -> list[dict[str, str]]:
    values = _app_call("load_json_file", MARKET_WATCHLIST_FILE, [])
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict) and item.get("symbol")]


# Historical sampling is deliberately opt-in and bounded to a small, known
# set of intervals.  It reuses the existing market_refresh automation handler
# so the API-owned scheduler and the external sync worker share one execution
# path, while the source marker keeps this rule distinguishable from any
# manually-created market refresh rule.
MARKET_SAMPLING_INTERVALS: dict[int, str] = {
    300: "每 5 分钟",
    1800: "每 30 分钟",
    3600: "每 1 小时",
    86400: "每天",
}
MARKET_SAMPLING_SOURCE = "market_sampling"
MARKET_SAMPLING_RULE_NAME = "量化历史样本采集"


def market_sampling_rule() -> dict[str, Any] | None:
    """Return the one rule owned by the explicit market sampling control."""
    candidates = []
    for rule in automation_rules():
        config = rule.get("config") if isinstance(rule.get("config"), dict) else {}
        if (
            str(rule.get("kind") or "") == "market_refresh"
            and str(rule.get("project_id") or "") == "market"
            and str(config.get("source") or "") == MARKET_SAMPLING_SOURCE
        ):
            candidates.append(rule)
    return max(candidates, key=lambda item: int(item.get("id") or 0), default=None)


def market_history_count() -> int:
    """Read the full persisted sample count without the UI's display limit."""
    connection = db_connection()
    try:
        row = connection.execute("SELECT COUNT(*) AS count FROM market_snapshots").fetchone()
        return int(row["count"] or 0) if row else 0
    finally:
        connection.close()


def load_market_snapshot() -> dict[str, Any]:
    values = _app_call("load_json_file", MARKET_SNAPSHOT_FILE, {})
    if not isinstance(values, dict):
        values = {}
    # The editable watchlist is the source of truth.  A quote snapshot may be
    # older than the latest edit, so never let removed symbols leak back into
    # today's cards, AI summaries, or portfolio counts.
    watchlist = _app_call("load_market_watchlist", )
    allowed_symbols = {
        _app_call("normalize_market_symbol", str(item.get("symbol") or ""))
        for item in watchlist
        if isinstance(item, dict)
    }
    allowed_symbols.discard("")
    quotes = [
        item for item in (values.get("quotes") or [])
        if isinstance(item, dict)
        and _app_call("normalize_market_symbol", str(item.get("symbol") or "")) in allowed_symbols
    ]
    values["watchlist"] = watchlist
    values["quotes"] = quotes
    if not watchlist:
        values.update({"checked_at": "", "status": "empty", "missing_symbols": []})
    return values

def save_market_watchlist(values: list[dict[str, str]]) -> None:
    save_json_atomic(MARKET_WATCHLIST_FILE, values, 0o600)


def save_market_snapshot(values: dict[str, Any]) -> None:
    save_json_atomic(MARKET_SNAPSHOT_FILE, values, 0o600)



def market_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    def decode_list(value: str) -> list[Any]:
        try:
            parsed = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    return {
        "id": row["id"],
        "checked_at": row["checked_at"],
        "source": row["source"],
        "status": row["status"],
        "watchlist": decode_list(row["watchlist_json"]),
        "quotes": decode_list(row["quotes_json"]),
        "created_at": row["created_at"],
    }


def market_timestamp_key(value: Any) -> str:
    """Return a stable UTC key for equivalent ISO timestamps.

    Providers may spell the same instant with ``Z`` or an explicit offset.
    Keeping the display value while using a canonical key prevents duplicate
    research points from entering the history series.
    """
    parsed = _sub2api_timestamp(value)
    return parsed.isoformat() if parsed else str(value or "").strip()


def record_market_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Persist only usable quote history without changing the JSON snapshot contract."""
    checked_at = str(snapshot.get("checked_at") or "").strip()
    if not checked_at:
        return None
    watchlist = [item for item in snapshot.get("watchlist", []) if isinstance(item, dict) and item.get("symbol")]
    quotes = [item for item in snapshot.get("quotes", []) if isinstance(item, dict) and item.get("symbol")]
    # A failed/empty provider response is still kept in market_snapshot.json
    # for troubleshooting, but it must not become a usable research point.
    usable_quotes = [quote for quote in quotes if _app_call("market_quote_quality", quote).get("valid")]
    if not usable_quotes:
        return None
    connection = db_connection()
    try:
        existing = connection.execute(
            "SELECT * FROM market_snapshots WHERE checked_at = ? ORDER BY id DESC LIMIT 1",
            (checked_at,),
        ).fetchone()
        if not existing:
            # Fall back to a bounded canonical-time lookup for providers that
            # changed from ``Z`` to ``+00:00`` (or vice versa).
            candidates = connection.execute("SELECT * FROM market_snapshots ORDER BY id DESC LIMIT 500").fetchall()
            incoming_key = _app_call("market_timestamp_key", checked_at)
            existing = next((row for row in candidates if _app_call("market_timestamp_key", row["checked_at"]) == incoming_key), None)
        if existing:
            existing_payload = _app_call("market_snapshot_row", existing)
            existing_quotes = {
                _app_call("normalize_market_symbol", item.get("symbol", "")): item
                for item in existing_payload.get("quotes", [])
                if isinstance(item, dict) and _app_call("normalize_market_symbol", item.get("symbol", ""))
            }
            for quote in usable_quotes:
                existing_quotes[_app_call("normalize_market_symbol", quote.get("symbol", ""))] = quote
            merged_quotes = list(existing_quotes.values())
            existing_watchlist = {
                _app_call("normalize_market_symbol", item.get("symbol", "")): item
                for item in existing_payload.get("watchlist", [])
                if isinstance(item, dict) and _app_call("normalize_market_symbol", item.get("symbol", ""))
            }
            for item in watchlist:
                existing_watchlist[_app_call("normalize_market_symbol", item.get("symbol", ""))] = item
            sources = {
                str(existing_payload.get("source") or "unknown"),
                str(snapshot.get("source") or "unknown"),
            }
            sources.discard("unknown")
            source = next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "unknown"
            connection.execute(
                "UPDATE market_snapshots SET source = ?, status = ?, watchlist_json = ?, quotes_json = ? WHERE id = ?",
                (
                    source,
                    "ok" if merged_quotes else str(snapshot.get("status") or existing_payload.get("status") or "ok"),
                    json.dumps(list(existing_watchlist.values()), ensure_ascii=False),
                    json.dumps(merged_quotes, ensure_ascii=False),
                    existing["id"],
                ),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM market_snapshots WHERE id = ?", (existing["id"],)).fetchone()
            return _app_call("market_snapshot_row", row) if row else existing_payload
        cursor = connection.execute(
            """INSERT INTO market_snapshots
            (checked_at, source, status, watchlist_json, quotes_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                checked_at,
                str(snapshot.get("source") or "unknown"),
                str(snapshot.get("status") or "ok"),
                json.dumps(watchlist, ensure_ascii=False),
                json.dumps(usable_quotes, ensure_ascii=False),
                now_iso(),
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM market_snapshots WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _app_call("market_snapshot_row", row) if row else None
    finally:
        connection.close()


def list_market_history(limit: int = 30) -> list[dict[str, Any]]:
    requested = max(1, min(limit, 100))
    connection = db_connection()
    try:
        # checked_at can contain equivalent instants with different offsets
        # (for example ``Z`` and ``+08:00``).  SQL text ordering is not
        # chronological across offsets, so fetch a bounded recent window and
        # sort the decoded rows by the canonical UTC timestamp below.
        rows = connection.execute(
            "SELECT * FROM market_snapshots ORDER BY id DESC LIMIT ?",
            (max(requested, 5000),),
        ).fetchall()
        items = [_app_call("market_snapshot_row", row) for row in rows]
        items.sort(
            key=lambda item: (
                _sub2api_timestamp(item.get("checked_at")) or datetime.min.replace(tzinfo=timezone.utc),
                int(item.get("id") or 0),
            ),
            reverse=True,
        )
        return items[:requested]
    finally:
        connection.close()


def market_sampling_state() -> dict[str, Any]:
    """Build a truthful status payload for the opt-in sampling control."""
    rule = _app_call("market_sampling_rule", )
    config = rule.get("config") if isinstance(rule and rule.get("config"), dict) else {}
    schedule = str(rule.get("schedule") or "") if rule else ""
    schedule_match = re.fullmatch(r"every:(\d+)", schedule)
    interval_seconds = int(schedule_match.group(1)) if schedule_match else int(config.get("interval_seconds") or 1800)
    if interval_seconds not in MARKET_SAMPLING_INTERVALS:
        interval_seconds = 1800

    history = _app_call("list_market_history", limit=1)
    latest = history[0] if history else None
    history_count = _app_call("market_history_count", )
    rule_id = int(rule.get("id") or 0) if rule else 0
    runs = list_automation_runs(rule_id, limit=5) if rule_id else []
    latest_run = runs[0] if runs else None
    last_error = str((rule or {}).get("last_error") or "").strip()
    if latest_run and latest_run.get("status") == "failed":
        last_error = str(latest_run.get("error") or last_error).strip()

    last_run_at = str((rule or {}).get("last_run_at") or "").strip()
    next_run_at = ""
    last_dt = _sub2api_timestamp(last_run_at) if last_run_at else None
    if rule and rule.get("enabled") and last_dt:
        next_run_at = (last_dt + timedelta(seconds=interval_seconds)).isoformat()

    latest_run_summary = None
    if latest_run:
        latest_run_summary = {
            "id": latest_run.get("id"),
            "status": latest_run.get("status"),
            "trigger": latest_run.get("trigger"),
            "created_at": latest_run.get("created_at"),
            "started_at": latest_run.get("started_at"),
            "finished_at": latest_run.get("finished_at"),
            "error": latest_run.get("error") or "",
        }

    latest_snapshot = None
    if latest:
        latest_snapshot = {
            "checked_at": latest.get("checked_at") or "",
            "source": latest.get("source") or "",
            "status": latest.get("status") or "",
            "quote_count": len(latest.get("quotes") or []),
        }

    enabled = bool(rule and rule.get("enabled"))
    return {
        "enabled": enabled,
        "status": "enabled" if enabled else "disabled",
        "interval_seconds": interval_seconds,
        "interval_label": MARKET_SAMPLING_INTERVALS[interval_seconds],
        "allowed_intervals": [
            {"seconds": seconds, "label": label}
            for seconds, label in MARKET_SAMPLING_INTERVALS.items()
        ],
        "watchlist_count": len(_app_call("load_market_watchlist", )),
        "history_count": history_count,
        "latest_snapshot": latest_snapshot,
        "last_run_at": last_run_at,
        "next_run_at": next_run_at,
        "last_run": latest_run_summary,
        "failure_reason": last_error,
        "rule_id": rule_id or None,
        "policy": "仅在用户明确开启后按固定周期读取公开行情并保存本地历史快照；关闭后保留历史，不连接券商、不自动下单。",
    }


def market_quote_quality(quote: dict[str, Any]) -> dict[str, Any]:
    price = _app_call("parse_market_number", quote.get("price"))
    previous = _app_call("parse_market_number", quote.get("previous_close"))
    provided_change = _app_call("parse_market_number", quote.get("change_pct"))
    reasons: list[str] = []
    if price is None or price <= 0:
        reasons.append("现价缺失或无效")
    if previous is None or previous <= 0:
        reasons.append("昨收缺失或无效")
    calculated_change = None
    if price is not None and previous not in (None, 0):
        calculated_change = round((price - previous) / previous * 100, 2)
        if abs(calculated_change) > 35:
            reasons.append("涨跌幅超出常规保护阈值")
        if provided_change is not None and abs(provided_change - calculated_change) > 0.25:
            reasons.append("涨跌幅与价格计算结果不一致")
    return {
        "valid": not reasons,
        "reasons": reasons,
        "price": price,
        "previous_close": previous,
        "change_pct": calculated_change if calculated_change is not None else provided_change,
        "open": _app_call("parse_market_number", quote.get("open")),
        "volume": _app_call("parse_market_number", quote.get("volume")),
    }


def analyze_market_snapshot(
    snapshot: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Turn a quote snapshot into bounded, explainable research observations."""
    now = now or datetime.now(timezone.utc)
    history = history if history is not None else _app_call("list_market_history", limit=30)
    checked_at = str(snapshot.get("checked_at") or "").strip()
    checked_dt = _sub2api_timestamp(checked_at)
    age_seconds = max(0, int((now - checked_dt).total_seconds())) if checked_dt else None
    if checked_dt is None:
        freshness = {"status": "unknown", "label": "没有同步时间"}
    elif age_seconds is not None and age_seconds <= 15 * 60:
        freshness = {"status": "fresh", "label": "数据新鲜"}
    elif age_seconds is not None and age_seconds <= 6 * 3600:
        freshness = {"status": "aging", "label": "数据较旧"}
    else:
        freshness = {"status": "stale", "label": "数据已过期"}
    freshness.update({"checked_at": checked_at, "age_seconds": age_seconds})

    watchlist = [item for item in snapshot.get("watchlist", []) if isinstance(item, dict) and item.get("symbol")]
    quotes = [item for item in snapshot.get("quotes", []) if isinstance(item, dict) and item.get("symbol")]
    quote_map = {_app_call("market_symbol_key", item.get("symbol")): item for item in quotes}
    missing_symbols = [_app_call("market_symbol_key", item.get("symbol")) for item in watchlist if _app_call("market_symbol_key", item.get("symbol")) not in quote_map]
    quality_by_symbol: dict[str, dict[str, Any]] = {}
    anomalies: list[dict[str, Any]] = []
    valid_quotes: list[dict[str, Any]] = []
    for quote in quotes:
        symbol = _app_call("market_symbol_key", quote.get("symbol"))
        quality = _app_call("market_quote_quality", quote)
        quality_by_symbol[symbol] = quality
        if quality["valid"]:
            valid_quotes.append(quote)
        else:
            anomalies.append({"symbol": symbol, "name": quote.get("name") or symbol.upper(), "reasons": quality["reasons"]})

    prior_candidates = []
    for item in history:
        item_checked_at = str(item.get("checked_at") or "").strip()
        if not item_checked_at or _app_call("market_timestamp_key", item_checked_at) == _app_call("market_timestamp_key", checked_at):
            continue
        item_dt = _sub2api_timestamp(item_checked_at)
        if checked_dt and item_dt and item_dt <= checked_dt:
            prior_candidates.append((item_dt, item))
        elif not checked_dt:
            prior_candidates.append((item_dt or datetime.min.replace(tzinfo=timezone.utc), item))
    prior_snapshot = max(prior_candidates, key=lambda pair: pair[0])[1] if prior_candidates else None
    prior_quotes = {
        _app_call("market_symbol_key", item.get("symbol")): item
        for item in (prior_snapshot or {}).get("quotes", [])
        if isinstance(item, dict) and item.get("symbol")
    }
    signals: list[dict[str, Any]] = []
    for quote in valid_quotes:
        symbol = _app_call("market_symbol_key", quote.get("symbol"))
        quality = quality_by_symbol[symbol]
        change_pct = quality.get("change_pct")
        factors: list[str] = []
        if isinstance(change_pct, (int, float)):
            if change_pct <= -3:
                observation = "相对昨收明显走弱"
            elif change_pct >= 3:
                observation = "相对昨收明显走强"
            else:
                observation = "相对昨收变化有限"
            factors.append(f"日涨跌 {change_pct:+.2f}%")
        else:
            observation = "涨跌幅缺失，暂不判断方向"
        open_price = quality.get("open")
        if isinstance(open_price, (int, float)) and open_price > 0 and isinstance(quality.get("price"), (int, float)):
            open_change = round((quality["price"] - open_price) / open_price * 100, 2)
            factors.append(f"相对开盘 {open_change:+.2f}%")
        if quality.get("volume") is None:
            factors.append("成交量缺失")
        prior_delta = None
        prior = prior_quotes.get(symbol)
        prior_price = _app_call("parse_market_number", (prior or {}).get("price"))
        if prior_price and quality.get("price"):
            prior_delta = round((quality["price"] - prior_price) / prior_price * 100, 2)
            factors.append(f"较上一快照 {prior_delta:+.2f}%")
        factor_analysis = analyze_market_factors(symbol, snapshot, history, quality)
        factors.extend(factor_analysis["factor_labels"])
        signals.append({
            "symbol": symbol,
            "name": quote.get("name") or symbol.upper(),
            "observation": observation,
            "change_pct": change_pct,
            "prior_delta_pct": prior_delta,
            "factors": factors,
            "factor_details": factor_analysis["factors"],
            "data_quality": factor_analysis["data_quality"],
            "research_tasks": factor_analysis["research_tasks"],
        })

    warnings: list[str] = []
    if freshness["status"] in {"stale", "unknown", "aging"}:
        warnings.append(f"{freshness['label']}，请刷新后再做判断")
    if missing_symbols:
        warnings.append(f"{len(missing_symbols)} 只自选未返回有效报价")
    if anomalies:
        warnings.append(f"{len(anomalies)} 条报价触发异常保护")
    if not watchlist:
        summary = "还没有自选股票，先添加标的再开始研究。"
    elif not valid_quotes:
        summary = "当前没有可用于研究的有效报价，已保留原有快照。"
    elif warnings:
        summary = f"已读取 {len(valid_quotes)}/{len(watchlist)} 只自选；当前需要关注数据质量或新鲜度。"
    else:
        summary = f"已读取 {len(valid_quotes)} 只自选；可基于日涨跌、趋势、波动和成交活跃度做观察。"
    factor_total = sum(len(signal.get("factor_details") or []) for signal in signals)
    factor_ready = sum(
        1
        for signal in signals
        for factor in (signal.get("factor_details") or [])
        if factor.get("status") == "ok"
    )
    quality_by_signal = [signal.get("data_quality") or {} for signal in signals]
    coverage_days = max((float(item.get("coverage_days") or 0) for item in quality_by_signal), default=0.0)
    source_stabilities = {str(item.get("source_stability") or "unknown") for item in quality_by_signal}
    freshness_score = {"fresh": 1.0, "aging": 0.65, "stale": 0.25, "unknown": 0.0}.get(freshness["status"], 0.0)
    watchlist_symbols = {_app_call("market_symbol_key", item.get("symbol")) for item in watchlist}
    valid_watchlist_quotes = [quote for quote in valid_quotes if _app_call("market_symbol_key", quote.get("symbol")) in watchlist_symbols]
    quote_score = min(1.0, (len(valid_watchlist_quotes) / len(watchlist)) if watchlist else 0.0)
    factor_score = (factor_ready / factor_total) if factor_total else 0.0
    coverage_values = [float(item.get("coverage_days") or 0) for item in quality_by_signal if item.get("sample_count")]
    representative_coverage = statistics.median(coverage_values) if coverage_values else 0.0
    coverage_score = min(1.0, representative_coverage / 20.0) if representative_coverage else 0.0
    confidence_score = round(quote_score * 0.35 + freshness_score * 0.25 + factor_score * 0.25 + coverage_score * 0.15, 3)
    confidence = "high" if confidence_score >= 0.8 and factor_total and factor_ready == factor_total and freshness["status"] == "fresh" else "medium" if confidence_score >= 0.5 else "low"
    research_confidence = {
        "score": confidence_score,
        "label": confidence,
        "sample_count": sum(int(item.get("sample_count") or 0) for item in quality_by_signal),
        "valid_quote_count": len(valid_quotes),
        "rejected_quote_count": len(anomalies) + len(missing_symbols),
        "coverage_days": coverage_days,
        "freshness": freshness["status"],
        "factor_ready_count": factor_ready,
        "factor_total": factor_total,
        "source_stability": "unknown" if not source_stabilities or source_stabilities == {"unknown"} else "mixed" if "mixed" in source_stabilities or len(source_stabilities) > 1 else "stable",
        "explanation": "置信度综合有效报价、数据新鲜度、因子最小样本和时间覆盖；不代表收益概率。",
    }
    status = "warning" if not watchlist else "error" if not valid_quotes else "warning" if warnings else "ok"
    return {
        "status": status,
        "status_label": "暂无自选" if not watchlist else "行情不可用" if not valid_quotes else "需要关注" if warnings else "可研究",
        "summary": summary,
        "freshness": freshness,
        "source": str(snapshot.get("source") or "未知行情源"),
        "watchlist_count": len(watchlist),
        "quote_count": len(quotes),
        "valid_quote_count": len(valid_quotes),
        "missing_symbols": missing_symbols,
        "anomalies": anomalies,
        "warnings": warnings,
        "signals": signals,
        "research_confidence": research_confidence,
        "risk_note": "这些是基于公开快照的研究线索，不构成投资建议，也不会自动下单。",
    }


def _market_history_points(
    symbol: str,
    snapshot: dict[str, Any],
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return de-duplicated chronological quote points for one symbol."""
    by_time: dict[str, dict[str, Any]] = {}
    # The current JSON snapshot wins over a database row with the same time.
    for entry in [*history, snapshot]:
        checked_at = str(entry.get("checked_at") or "").strip()
        if not checked_at:
            continue
        timestamp_key = _app_call("market_timestamp_key", checked_at)
        for quote in entry.get("quotes", []):
            if not isinstance(quote, dict):
                continue
            if _app_call("market_symbol_key", quote.get("symbol")) != _app_call("market_symbol_key", symbol):
                continue
            price = _app_call("parse_market_number", quote.get("price"))
            if price is None or price <= 0:
                continue
            by_time[timestamp_key] = {
                "checked_at": checked_at,
                "price": price,
                "volume": _app_call("parse_market_number", quote.get("volume")),
                "source": str(entry.get("source") or "unknown"),
            }
    return sorted(
        by_time.values(),
        key=lambda item: _sub2api_timestamp(item["checked_at"]) or datetime.min.replace(tzinfo=timezone.utc),
    )


def _market_factor_meta(
    *,
    label: str,
    points: list[dict[str, Any]],
    value: Any = None,
    unit: str = "",
    observation: str = "",
    missing_reason: str = "",
    minimum_samples: int = 0,
) -> dict[str, Any]:
    if not missing_reason and minimum_samples and len(points) < minimum_samples:
        missing_reason = f"至少需要 {minimum_samples} 个不同时间的有效样本，当前只有 {len(points)} 个"
    coverage_days = 0.0
    if len(points) >= 2:
        parsed = [_sub2api_timestamp(item.get("checked_at")) for item in points]
        parsed = [item for item in parsed if item]
        if len(parsed) >= 2:
            coverage_days = round(max(0.0, (parsed[-1] - parsed[0]).total_seconds() / 86400), 2)
    return {
        "key": label,
        "label": label,
        "status": "ok" if not missing_reason else "missing",
        "value": value,
        "unit": unit,
        "observation": observation,
        "sample_count": len(points),
        "minimum_samples": minimum_samples,
        "coverage_days": coverage_days,
        "data_from": points[0]["checked_at"] if points else "",
        "data_to": points[-1]["checked_at"] if points else "",
        "missing_reason": missing_reason,
    }


def analyze_market_factors(
    symbol: str,
    snapshot: dict[str, Any],
    history: list[dict[str, Any]],
    quality: dict[str, Any],
) -> dict[str, Any]:
    """Calculate bounded factors from local snapshots, never from model guesses.

    The public quote source currently provides price and volume only. Valuation,
    fundamentals and technical indicators that need a longer time series remain
    explicitly absent instead of being inferred.
    """
    points = _app_call("_market_history_points", symbol, snapshot, history)
    price_points = [point for point in points if isinstance(point.get("price"), (int, float))]
    volume_points = [point for point in points if isinstance(point.get("volume"), (int, float)) and point["volume"] >= 0]
    factor_labels: list[str] = []
    research_tasks: list[dict[str, Any]] = []

    if len(price_points) >= 2:
        start_price = price_points[0]["price"]
        end_price = price_points[-1]["price"]
        trend_change = round((end_price - start_price) / start_price * 100, 2) if start_price else None
        trend_state = "up" if trend_change is not None and trend_change >= 3 else "down" if trend_change is not None and trend_change <= -3 else "flat"
        trend_text = {
            "up": "样本区间价格上行",
            "down": "样本区间价格下行",
            "flat": "样本区间价格变化有限",
        }[trend_state]
        trend = _market_factor_meta(
            label="趋势",
            points=price_points,
            value=trend_change,
            unit="%",
            observation=trend_text,
            minimum_samples=3,
        )
        trend.update({"state": trend_state, "start_price": start_price, "end_price": end_price})
        if trend.get("status") == "ok" and trend_state in {"up", "down"}:
            factor_labels.append(f"区间趋势 {trend_change:+.2f}%")
            research_tasks.append({"factor": f"trend_{trend_state}", "label": "趋势变化", "message": trend_text, "value": trend_change})
        # 单日异动：最新一个价格点相对前一个样本的涨跌幅超过 ±5% 时单独提示，
        # 与区间趋势区分开（单日异动往往值得当天关注）。
        if trend.get("status") == "ok" and len(price_points) >= 2 and price_points[-2].get("price"):
            last_change = round((price_points[-1]["price"] - price_points[-2]["price"]) / price_points[-2]["price"] * 100, 2)
            if abs(last_change) >= 5:
                day_state = "up" if last_change > 0 else "down"
                day_text = f"最新样本较前一记录 {last_change:+.2f}%，属于明显异动，建议关注当天动态与消息面。"
                factor_labels.append(f"单日异动 {last_change:+.2f}%")
                research_tasks.append({"factor": f"daily_spike_{day_state}", "label": "单日异动", "message": day_text, "value": last_change})
    else:
        trend = _market_factor_meta(label="趋势", points=price_points, missing_reason="至少需要 3 个不同时间的有效价格样本", minimum_samples=3)
        trend["state"] = "unknown"

    returns: list[float] = []
    for previous, current in zip(price_points, price_points[1:]):
        if previous["price"]:
            returns.append((current["price"] - previous["price"]) / previous["price"] * 100)
    if len(price_points) >= 4 and len(returns) >= 3:
        volatility = round(statistics.pstdev(returns), 2)
        volatility_state = "high" if volatility >= 3 else "normal"
        volatility_text = "样本间价格波动偏高" if volatility_state == "high" else "样本间价格波动暂未偏高"
        volatility_factor = _market_factor_meta(label="波动", points=price_points, value=volatility, unit="%", observation=volatility_text, minimum_samples=4)
        volatility_factor.update({"state": volatility_state, "return_samples": len(returns)})
        if volatility_factor.get("status") == "ok" and volatility_state == "high":
            factor_labels.append(f"波动 {volatility:.2f}%")
            research_tasks.append({"factor": "high_volatility", "label": "波动偏高", "message": volatility_text, "value": volatility})
    else:
        volatility_factor = _market_factor_meta(label="波动", points=price_points, missing_reason="至少需要 4 个不同时间的有效价格样本", minimum_samples=4)
        volatility_factor.update({"state": "unknown", "return_samples": len(returns)})

    if len(volume_points) >= 3:
        latest_volume = volume_points[-1]["volume"]
        baseline = [point["volume"] for point in volume_points[:-1]]
        median_volume = statistics.median(baseline) if baseline else None
        volume_ratio = round(latest_volume / median_volume, 2) if median_volume and median_volume > 0 else None
        if volume_ratio is None:
            activity_factor = _market_factor_meta(label="成交活跃度", points=volume_points, missing_reason="历史成交量基准为 0，无法比较")
            activity_factor.update({"state": "unknown"})
        else:
            activity_state = "high" if volume_ratio >= 1.5 else "low" if volume_ratio <= 0.67 else "normal"
            activity_text = {"high": "最新成交量高于历史中位数", "low": "最新成交量低于历史中位数", "normal": "最新成交量接近历史中位数"}[activity_state]
            activity_factor = _market_factor_meta(label="成交活跃度", points=volume_points, value=volume_ratio, unit="x", observation=activity_text, minimum_samples=3)
            activity_factor.update({"state": activity_state, "latest_volume": latest_volume, "baseline_volume": median_volume})
            if activity_factor.get("status") == "ok" and activity_state in {"high", "low"}:
                factor_labels.append(f"成交量 {volume_ratio:.2f}x")
                research_tasks.append({"factor": f"volume_{activity_state}", "label": "成交活跃度变化", "message": activity_text, "value": volume_ratio})
    else:
        activity_factor = _market_factor_meta(label="成交活跃度", points=volume_points, missing_reason="至少需要 3 个不同时间的有效成交量样本", minimum_samples=3)
        activity_factor.update({"state": "unknown"})

    factor_details = [trend, volatility_factor, activity_factor]
    data_quality = {
        "sample_count": len(points),
        "valid_price_count": len(price_points),
        "valid_volume_count": len(volume_points),
        "data_from": points[0]["checked_at"] if points else "",
        "data_to": points[-1]["checked_at"] if points else "",
        "coverage_days": max((float(factor.get("coverage_days") or 0) for factor in factor_details), default=0.0),
        "source": str(snapshot.get("source") or "unknown"),
        "source_stability": "unknown" if not points else "stable" if len({str(point.get("source") or "unknown") for point in points}) == 1 else "mixed",
        "missing_factors": [factor["label"] for factor in factor_details if factor.get("status") == "missing"],
        "note": "公开行情快照目前只提供价格和成交量；因子样本不足时不做方向性判断。",
    }
    return {"factors": factor_details, "factor_labels": factor_labels, "research_tasks": research_tasks, "data_quality": data_quality}


def evaluate_market_observations(
    snapshot: dict[str, Any] | None = None,
    *,
    create_records: bool = False,
) -> dict[str, Any]:
    """Turn material factor signals into deduplicated local research tasks."""
    snapshot = snapshot or _app_call("load_market_snapshot", )
    history = _app_call("list_market_history", limit=60)
    if snapshot.get("checked_at"):
        _app_call("record_market_snapshot", snapshot)
        history = _app_call("list_market_history", limit=60)
    analysis = _app_call("analyze_market_snapshot", snapshot, history)
    candidates: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []
    existing_items = _app_call("list_work_items", "all", "market") if create_records else []
    artifact = _app_call("list_artifacts", "market")[0] if create_records and _app_call("list_artifacts", "market") else None
    for signal in analysis.get("signals", []):
        for task in signal.get("research_tasks", []):
            factor = str(task.get("factor") or "observation")
            observation_key = f"market:{signal.get('symbol')}:{factor}"
            candidate = {
                "observation_key": observation_key,
                "symbol": signal.get("symbol", ""),
                "name": signal.get("name", ""),
                "factor": factor,
                "title": f"观察 {signal.get('name') or signal.get('symbol')} · {task.get('label') or '行情变化'}",
                "message": str(task.get("message") or "基于当前样本形成研究观察。"),
                "value": task.get("value"),
                "checked_at": analysis.get("freshness", {}).get("checked_at", ""),
                "source": analysis.get("source", ""),
            }
            candidates.append(candidate)
            if not create_records:
                continue
            # Keep one task per symbol/factor state even after completion. A
            # refresh must not recreate the same observation endlessly; a
            # changed factor state gets a different key and can be tracked.
            existing = next(
                (item for item in existing_items if item.get("metadata", {}).get("observation_key") == observation_key),
                None,
            )
            if existing:
                created.append({"candidate": candidate, "work_item": existing, "created": False})
                continue
            item = _app_call("create_work_item_record", 
                title=candidate["title"],
                description=f"{candidate['message']} 数据时间：{candidate['checked_at'] or '未知'}。请在后续快照中复核，不构成买卖指令。",
                kind="research_observation",
                status="open",
                priority="normal",
                source_project="market",
                target_project="inbox",
                metadata={
                    "observation_key": observation_key,
                    "factor": factor,
                    "symbol": candidate["symbol"],
                    "checked_at": candidate["checked_at"],
                    "source": candidate["source"],
                    "agent": "quantitative_research_agent",
                },
            )
            relation = None
            if artifact:
                relation = _app_call("create_relation_record", 
                    from_type="artifact",
                    from_id=str(artifact["id"]),
                    to_type="work_item",
                    to_id=str(item["id"]),
                    relation_type="research_observation_from_snapshot",
                    metadata={"observation_key": observation_key, "checked_at": candidate["checked_at"]},
                )
            try:
                _app_call("create_notification_record", 
                    title=candidate["title"],
                    body=f"{candidate['message']} · 数据时间：{candidate['checked_at'] or '未知'}",
                    project_id="market",
                    kind="research_observation",
                    level="info",
                    href="/projects/market#market-observations",
                    event_key=f"market-observation:{observation_key}",
                    dedupe_seconds=0,
                )
            except Exception:
                log.debug("忽略异常（evaluate_market_observations）", exc_info=True)
            existing_items.append(item)
            created.append({"candidate": candidate, "work_item": item, "relation": relation, "created": True})
    return {"analysis": analysis, "candidates": candidates, "created": created}


def add_market_symbol_to_watchlist(symbol: str) -> dict[str, Any]:
    normalized = _app_call("normalize_market_symbol", symbol)
    if not normalized:
        raise ValueError(f"无法识别股票代码：{symbol}")
    values = _app_call("load_market_watchlist", )
    existing = {item.get("symbol") for item in values}
    added = normalized not in existing
    if added:
        values.append({"symbol": normalized})
        _app_call("save_market_watchlist", values)
        snapshot = _app_call("load_market_snapshot", )
        snapshot["watchlist"] = values
        _app_call("save_market_snapshot", snapshot)
    return {"symbol": normalized, "added": added, "watchlist": values}


def normalize_market_symbol(value: str) -> str:
    symbol = re.sub(r"\s+", "", value or "").lower()
    if re.fullmatch(r"\d{6}", symbol):
        if symbol.startswith(("5", "6", "68")):
            return f"sh{symbol}"
        if symbol.startswith(("1",)):
            return f"sz{symbol}"
        if symbol.startswith(("4", "8")):
            return f"bj{symbol}"
        return f"sz{symbol}"
    if re.fullmatch(r"(?:sh|sz|bj|hk|us)\d{5,6}", symbol) or re.fullmatch(r"us[a-z]{1,6}", symbol):
        return symbol
    return ""


def market_symbol_key(value: Any) -> str:
    """Use one comparison key for bare and exchange-prefixed symbols."""
    normalized = _app_call("normalize_market_symbol", str(value or ""))
    return normalized or str(value or "").strip().lower()


def market_symbol_queryable(prefixed: str) -> bool:
    """判断代码是否能在腾讯行情接口查到（A 股、场内 ETF/LOF、指数；场外开放式基金查不到）。"""
    match = re.fullmatch(r"(sh|sz|bj)(\d{6})", prefixed or "")
    if not match:
        return False
    market, code = match.group(1), match.group(2)
    if market == "sh":
        return code.startswith(("6", "5", "000"))  # 沪股 / 沪场内 ETF·LOF / 沪指数
    if market == "sz":
        return code.startswith(("0", "3", "15", "16", "39"))  # 深股 / 深 ETF·LOF / 深指数
    return code.startswith(("4", "8"))  # 北交所


def parse_market_number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_market_quotes(symbols: list[str]) -> list[dict[str, Any]]:
    normalized = list(dict.fromkeys(_app_call("normalize_market_symbol", symbol) for symbol in symbols))
    normalized = [symbol for symbol in normalized if symbol]
    if not normalized:
        return []
    url = f"https://qt.gtimg.cn/q={','.join(normalized)}"
    async with httpx.AsyncClient(timeout=12, trust_env=False, headers={"User-Agent": "Workbench/0.2"}) as client:
        response = await client.get(url)
        response.raise_for_status()
    text = response.content.decode("gb18030", errors="replace")
    quotes = []
    for raw_symbol, raw_fields in re.findall(r'v_([a-z0-9]+)="(.*?)";', text, flags=re.IGNORECASE):
        fields = raw_fields.split("~")
        if len(fields) < 6:
            continue
        price = _app_call("parse_market_number", fields[3])
        previous = _app_call("parse_market_number", fields[4])
        if price is None or previous in (None, 0):
            continue
        change = price - previous
        quotes.append({
            "symbol": raw_symbol.lower(),
            "name": fields[1].strip() or raw_symbol.upper(),
            "price": price,
            "previous_close": previous,
            "change": round(change, 4),
            "change_pct": round(change / previous * 100, 2),
            "open": _app_call("parse_market_number", fields[5]),
            "volume": _app_call("parse_market_number", fields[6]) if len(fields) > 6 else None,
            "pe": _app_call("parse_market_number", fields[39]) if len(fields) > 39 else None,
            "pb": _app_call("parse_market_number", fields[46]) if len(fields) > 46 else None,
            "updated_at": now_iso(),
        })
    # 场外开放式基金：腾讯接口查不到（v_pv_none_match），用东财基金净值接口补充。
    # 返回"最新净值 + 日涨幅 + 净值日期"，保证"加了基金能看到数据"。
    found = {str(quote["symbol"]).replace("sh", "").replace("sz", "").replace("bj", "") for quote in quotes}
    missing_fund = [symbol for symbol in normalized if re.fullmatch(r"[a-z]{2}\d{6}", symbol) and symbol[2:] not in found]
    if missing_fund:
        fund_results = await asyncio.gather(*(_app_call("fetch_fund_nav", symbol[2:]) for symbol in missing_fund[:10]), return_exceptions=True)
        for result in fund_results:
            if isinstance(result, dict) and result:
                quotes.append(result)
    return quotes


async def fetch_fund_nav(symbol6: str) -> dict[str, Any] | None:
    """东财场外基金最新净值（lsjz 接口）+ 基金名称（pingzhongdata）。"""
    symbol6 = re.sub(r"\D", "", symbol6 or "")[-6:]
    if len(symbol6) != 6:
        return None
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"}
        async with httpx.AsyncClient(timeout=10, trust_env=False, headers=headers) as client:
            response = await client.get(f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={symbol6}&pageIndex=1&pageSize=1")
            response.raise_for_status()
            payload = response.json()
        rows = (((payload or {}).get("Data") or {}).get("LSJZList")) or []
        if not rows:
            return None
        row = rows[0]
        price = _app_call("parse_market_number", row.get("DWJZ"))
        if price is None:
            return None
        change_pct = _app_call("parse_market_number", row.get("JZZZL"))
        name = symbol6
        try:
            async with httpx.AsyncClient(timeout=8, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
                name_response = await client.get(f"https://fund.eastmoney.com/pingzhongdata/{symbol6}.js")
                match = re.search(r'fS_name\s*=\s*"([^"]+)"', name_response.text)
                if match:
                    name = match.group(1).strip()
        except Exception:
            log.debug("忽略异常（fetch_fund_nav）", exc_info=True)
        return {
            "symbol": symbol6,
            "name": name,
            "price": price,
            "previous_close": None,
            "change": change_pct if change_pct is not None else None,
            "change_pct": change_pct if change_pct is not None else None,
            "open": None,
            "volume": None,
            "updated_at": now_iso(),
            "source": "fund-nav",
            "nav_date": str(row.get("FSRQ") or ""),
        }
    except Exception:
        return None


async def fetch_fund_nav_history(symbol6: str, days: int = 120) -> list[dict[str, Any]]:
    """东财场外基金历史净值序列（lsjz 分页），返回 [{date, close}] 升序。

    场外基金在腾讯行情/K线接口都查不到，回测样本只能靠本地快照慢慢积累——
    新加的自选当天没有历史，研究卡就永远"样本不足"。这里直接拉东财的
    历史净值（日频，与 A 股 K 线同构：date + 价格），让基金也能立刻研究。
    """
    symbol6 = re.sub(r"\D", "", symbol6 or "")[-6:]
    if len(symbol6) != 6:
        return []
    days = max(10, min(500, int(days or 120)))
    page_size = 20
    pages = max(1, -(-days // page_size))
    rows: list[tuple[str, float]] = []
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"}
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False, headers=headers) as client:
            for page in range(1, pages + 1):
                response = await client.get(
                    f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={symbol6}&pageIndex={page}&pageSize={page_size}"
                )
                response.raise_for_status()
                payload = response.json()
                batch = (((payload or {}).get("Data") or {}).get("LSJZList")) or []
                if not batch:
                    break
                for item in batch:
                    nav = _app_call("parse_market_number", item.get("DWJZ"))
                    if nav and nav > 0:
                        rows.append((str(item.get("FSRQ") or ""), nav))
    except Exception:
        log.warning("拉取基金 %s 历史净值失败", symbol6, exc_info=True)
        return []
    # 接口按时间倒序返回，归一化成升序并去重、按日期截断。
    rows = sorted({date: nav for date, nav in rows}.items())
    return [{"date": date, "close": nav} for date, nav in rows[-days:]]


class MarketWatchlistRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=30)


class MarketSamplingRequest(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=1800, ge=300, le=86400)


class MarketReportRequest(BaseModel):
    period: str = Field(default="daily", pattern="^(daily|weekly)$")




@app.get("/api/market/suggest")
async def market_symbol_suggest(q: str = "", limit: int = 8) -> dict[str, Any]:
    """按名称/代码模糊搜索 A 股、基金/ETF、指数（东财优先，新浪兜底，只读外部请求）。"""
    query = (q or "").strip()
    if len(query) < 1 or len(query) > 20:
        return {"items": []}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _append(code: str, prefixed: str, name: str, kind: str) -> None:
        normalized = _app_call("normalize_market_symbol", prefixed) or _app_call("normalize_market_symbol", code)
        if not normalized or code in seen:
            return
        seen.add(code)
        items.append({"symbol": code, "prefixed": normalized, "name": name, "kind": kind})

    # 东财 suggest：覆盖 A 股、指数、基金；场内 ETF 覆盖有限但比新浪全。
    try:
        em_url = f"https://searchapi.eastmoney.com/api/suggest/get?input={quote(query)}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8"
        async with httpx.AsyncClient(timeout=8, trust_env=False, headers={"User-Agent": "Mozilla/5.0 (Workbench)"}) as client:
            response = await client.get(em_url)
            response.raise_for_status()
            em_body = response.json()
        for entry in (em_body.get("QuotationCodeTable") or {}).get("Data") or []:
            code = str(entry.get("Code") or "")
            name = str(entry.get("Name") or "")
            classify = str(entry.get("Classify") or "")
            security_type = str(entry.get("SecurityTypeName") or "")
            if not code or not name or not re.fullmatch(r"\d{5,6}", code):
                continue
            is_stock = classify in {"AStock", "Index"} or "股" in security_type or "指数" in security_type
            is_fund = classify == "Fund" or "基金" in security_type or "ETF" in security_type.upper()
            if not (is_stock or is_fund):
                continue
            quote_id = str(entry.get("QuoteID") or "")
            market_part = quote_id.split(".")[0] if "." in quote_id else ""
            prefixed = f"sh{code}" if market_part == "1" or "沪" in security_type else f"sz{code}" if market_part == "0" or "深" in security_type else f"bj{code}" if "北" in security_type else code
            _append(code, prefixed, name, "基金/ETF" if is_fund else "指数" if classify == "Index" else "A股")
            if len(items) >= min(max(int(limit), 1), 12):
                return {"items": items}
    except Exception:
        log.debug("忽略异常（market_symbol_suggest）", exc_info=True)

    # 新浪 suggest 兜底：A 股名称搜索最准。
    if len(items) < min(max(int(limit), 1), 12):
        try:
            sina_url = f"https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15&key={quote(query)}"
            async with httpx.AsyncClient(timeout=8, trust_env=False, headers={"User-Agent": "Mozilla/5.0 (Workbench)"}) as client:
                response = await client.get(sina_url)
                response.raise_for_status()
            text = response.content.decode("gb18030", errors="replace")
            for raw in re.findall(r'"(.*?)"', text):
                fields = raw.split(",")
                if len(fields) < 6:
                    continue
                name = fields[0].strip()
                market_type = str(fields[1] or "")
                code = fields[2].strip()
                prefixed = fields[3].strip()
                if not (code and name and market_type in {"11", "12", "13", "14", "15"}):
                    continue
                _append(code, prefixed, name, "基金/ETF" if market_type == "15" else "A股")
                if len(items) >= min(max(int(limit), 1), 12):
                    break
        except Exception:
            log.debug("忽略异常（market_symbol_suggest）", exc_info=True)
    return {"items": items}


@app.get("/api/market")
def get_market_state() -> dict[str, Any]:
    snapshot = _app_call("load_market_snapshot", )
    _app_call("record_market_snapshot", snapshot)
    history = _app_call("list_market_history", limit=30)
    observation_tasks = [
        item for item in _app_call("list_work_items", "all", "market")
        if item.get("source_project") == "market" and item.get("kind") == "research_observation"
    ][:30]
    # 给每只行情附加迷你走势序列（供前端画 SVG 走势图）
    quotes = snapshot.get("quotes") or []
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        symbol = str(quote.get("symbol") or "").lower()
        points = _app_call("_market_history_points", symbol, snapshot, history)
        quote["trend"] = [{"t": p["checked_at"], "p": p["price"]} for p in points[-20:]]
    return {"market": snapshot, "analysis": _app_call("analyze_market_snapshot", snapshot, history), "history": history, "observation_tasks": observation_tasks}



# ---------------------------------------------------------------------------
# "What should I do today" — the plain-language front page of the market tool.
#
# Design constraint: Workbench must not hand out investment advice.  So every
# line here is either (a) a fact about the quote, or (b) a comparison against a
# threshold the *user* set themselves.  The tool never decides what is a good
# buy; it only tells you when your own line was crossed, in words that do not
# require knowing what a factor or a drawdown is.
# ---------------------------------------------------------------------------

class MarketRuleRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    buy_below: float | None = Field(default=None, ge=0, le=1_000_000)
    sell_above: float | None = Field(default=None, ge=0, le=1_000_000)
    stop_below: float | None = Field(default=None, ge=0, le=1_000_000)
    note: str = Field(default="", max_length=200)


def _market_rule_value(raw: Any) -> float | None:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _distance_pct(price: float, line: float) -> float:
    return round((price - line) / line * 100, 2) if line else 0.0


def market_watchlist_rules() -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for item in _app_call("load_market_watchlist", ):
        symbol = _app_call("normalize_market_symbol", str(item.get("symbol") or ""))
        if not symbol:
            continue
        rules[symbol] = {
            "buy_below": _market_rule_value(item.get("buy_below")),
            "sell_above": _market_rule_value(item.get("sell_above")),
            "stop_below": _market_rule_value(item.get("stop_below")),
            "note": clip(str(item.get("note") or ""), 200),
        }
    return rules


def save_market_watchlist_rule(request: MarketRuleRequest) -> dict[str, Any]:
    symbol = _app_call("normalize_market_symbol", request.symbol)
    if not symbol:
        raise ValueError("股票代码无法识别，请使用 6 位代码，例如 600519。")
    watchlist = _app_call("load_market_watchlist", )
    found = False
    for item in watchlist:
        if _app_call("normalize_market_symbol", str(item.get("symbol") or "")) != symbol:
            continue
        found = True
        for key in ("buy_below", "sell_above", "stop_below"):
            value = getattr(request, key)
            if value is None or value <= 0:
                item.pop(key, None)
            else:
                item[key] = round(float(value), 4)
        if request.note.strip():
            item["note"] = request.note.strip()
        else:
            item.pop("note", None)
    if not found:
        raise ValueError("这只股票不在自选里，请先添加到自选。")
    _app_call("save_market_watchlist", watchlist)
    return {"ok": True, "symbol": symbol, "rules": _app_call("market_watchlist_rules", ).get(symbol, {})}


def _market_today_for_quote(quote: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    """Turn one quote plus the user's own lines into one plain-language card."""
    name = str(quote.get("name") or quote.get("symbol") or "")
    price = quote.get("price")
    change_pct = quote.get("change_pct")
    try:
        price = float(price)
    except (TypeError, ValueError):
        return {
            "symbol": quote.get("symbol"),
            "name": name,
            "level": "unknown",
            "signal": "unknown",
            "headline": f"{name} today 没有取到价格",
            "action": "稍后刷新行情再看。",
            "facts": [],
            "rules": rule,
        }
    change_pct = float(change_pct or 0)
    buy = rule.get("buy_below")
    sell = rule.get("sell_above")
    stop = rule.get("stop_below")

    facts = [f"现价 {price}", f"今天{'涨' if change_pct >= 0 else '跌'} {abs(change_pct)}%"]
    if buy:
        facts.append(f"你的买入线 {buy}（现价比它{'高' if price >= buy else '低'} {abs(_distance_pct(price, buy))}%）")
    if sell:
        facts.append(f"你的卖出线 {sell}（现价比它{'高' if price >= sell else '低'} {abs(_distance_pct(price, sell))}%）")
    if stop:
        facts.append(f"你的止损线 {stop}（现价比它{'高' if price >= stop else '低'} {abs(_distance_pct(price, stop))}%）")

    if not (buy or sell or stop):
        return {
            "symbol": quote.get("symbol"), "name": name, "price": price, "change_pct": change_pct,
            "level": "unset", "signal": "setup",
            "headline": f"{name} 还没设线",
            "action": "给它设一个「跌到多少提醒我」，我才能替你盯着。",
            "facts": facts, "rules": rule,
        }

    if stop and price <= stop:
        return {
            "symbol": quote.get("symbol"), "name": name, "price": price, "change_pct": change_pct,
            "level": "alert", "signal": "stop", "headline": f"{name} 跌破了你设的止损线",
            "action": f"现价 {price}，已经低于你写的 {stop}。你当初设这条线是为了在这里停手——现在去看一眼。",
            "facts": facts, "rules": rule,
        }
    if sell and price >= sell:
        return {
            "symbol": quote.get("symbol"), "name": name, "price": price, "change_pct": change_pct,
            "level": "reach", "signal": "sell", "headline": f"{name} 到了你设的卖出线",
            "action": f"现价 {price}，达到你写的 {sell}。要不要走，按你自己的计划来。",
            "facts": facts, "rules": rule,
        }
    if buy and price <= buy:
        return {
            "symbol": quote.get("symbol"), "name": name, "price": price, "change_pct": change_pct,
            "level": "reach", "signal": "buy", "headline": f"{name} 跌到了你想买的价",
            "action": f"现价 {price}，低于你写的 {buy}。看一眼是不是还符合你当初想买的理由。",
            "facts": facts, "rules": rule,
        }

    near = []
    for label, line in (("止损线", stop), ("买入线", buy), ("卖出线", sell)):
        if line and abs(_distance_pct(price, line)) <= 3:
            near.append(label)
    if near:
        return {
            "symbol": quote.get("symbol"), "name": name, "price": price, "change_pct": change_pct,
            "level": "near", "signal": "near", "headline": f"{name} 快到你的{near[0]}了",
            "action": "还差 3% 以内，先知道就行，不用现在动。",
            "facts": facts, "rules": rule,
        }
    return {
        "symbol": quote.get("symbol"), "name": name, "price": price, "change_pct": change_pct,
        "level": "ok", "signal": "hold", "headline": f"{name} 在你设的区间里",
        "action": "不用动。", "facts": facts, "rules": rule,
    }


MARKET_LEVEL_ORDER = {"alert": 0, "reach": 1, "near": 2, "unset": 3, "ok": 4, "unknown": 5}


def build_market_today(snapshot: dict[str, Any]) -> dict[str, Any]:
    """One screen that answers: do I need to do anything today?"""
    rules = _app_call("market_watchlist_rules", )
    watchlist_symbols = {
        _app_call("normalize_market_symbol", str(item.get("symbol") or ""))
        for item in _app_call("load_market_watchlist", )
        if isinstance(item, dict)
    }
    watchlist_symbols.discard("")
    quotes = [
        item for item in (snapshot.get("quotes") or [])
        if isinstance(item, dict)
        and _app_call("normalize_market_symbol", str(item.get("symbol") or "")) in watchlist_symbols
    ]
    cards = [
        _market_today_for_quote(quote, rules.get(_app_call("normalize_market_symbol", str(quote.get("symbol") or "")), {}))
        for quote in quotes
    ]
    cards.sort(key=lambda card: (MARKET_LEVEL_ORDER.get(card["level"], 9), -abs(float(card.get("change_pct") or 0))))

    need_action = [card for card in cards if card["level"] in {"alert", "reach"}]
    heads_up = [card for card in cards if card["level"] == "near"]
    unset = [card for card in cards if card["level"] == "unset"]
    signal_counts = {
        "buy": sum(1 for card in cards if card.get("signal") == "buy"),
        "sell": sum(1 for card in cards if card.get("signal") == "sell"),
        "stop": sum(1 for card in cards if card.get("signal") == "stop"),
        "near": sum(1 for card in cards if card.get("signal") == "near"),
    }

    if not quotes:
        verdict = "还没有行情"
        detail = "先添加自选并刷新行情，这一页才有内容。"
        tone = "empty"
    elif need_action:
        verdict = f"{len(need_action)} 只需要你看一眼"
        detail = "、".join(card["name"] for card in need_action[:4]) + " 触到了你自己设的线。"
        tone = "action"
    elif heads_up:
        verdict = "今天不用动"
        detail = f"但有 {len(heads_up)} 只快到线了，心里有个数就行。"
        tone = "watch"
    elif unset and len(unset) == len(cards):
        verdict = "我还不能替你盯"
        detail = "所有自选都没设线。给每只写一句「跌到多少提醒我」，这页才有用。"
        tone = "setup"
    else:
        verdict = "今天不用动"
        detail = "所有设了线的自选都在区间里。"
        tone = "calm"

    total_change = [float(card.get("change_pct") or 0) for card in cards if card.get("change_pct") is not None]
    average = round(sum(total_change) / len(total_change), 2) if total_change else 0.0

    return {
        "verdict": verdict,
        "detail": detail,
        "tone": tone,
        "checked_at": snapshot.get("checked_at") or "",
        "data_as_of": snapshot.get("checked_at") or "",
        "average_change_pct": average,
        "counts": {
            "total": len(cards),
            "need_action": len(need_action),
            "heads_up": len(heads_up),
            "unset": len(unset),
            **signal_counts,
        },
        "cards": cards,
    }


@app.get("/api/market/today")
def get_market_today() -> dict[str, Any]:
    """Plain-language daily verdict built from the last stored snapshot."""
    snapshot = _app_call("load_market_snapshot", )
    return {"ok": True, "today": _app_call("build_market_today", snapshot)}


def _market_percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0.0, min(1.0, ratio)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _market_reference_zones(points: list[dict[str, Any]], current_price: float, kline: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """历史参考位置：优先用腾讯前复权日K（kline）算分位，快照 points 兜底。

    新加入自选的股票在快照里只有几天数据，而它上市以来的交易早就存在——
    直接拉历史日K让分位区间立刻有据可依，而不是等快照慢慢积累。
    """
    prices = [float(point["price"]) for point in points if isinstance(point.get("price"), (int, float)) and point["price"] > 0]
    if kline:
        kline_prices = [float(item["close"]) for item in kline if isinstance(item.get("close"), (int, float)) and item["close"] > 0]
        # 快照点（今天的最新价）优先保留，历史K线去重后并入
        merged = list(prices)
        for value in kline_prices:
            if abs(value - current_price) > 1e-6 and value not in merged:
                merged.append(value)
        if merged:
            prices = merged
    parsed_times = [_sub2api_timestamp(point.get("checked_at")) for point in points]
    parsed_times = [item for item in parsed_times if item]
    coverage_days = round((parsed_times[-1] - parsed_times[0]).total_seconds() / 86400, 1) if len(parsed_times) >= 2 else 0.0
    if kline and len(kline) >= 2:
        # K线日期范围更真实（新加自选的快照只有几天，历史K线覆盖上市以来）
        coverage_days = max(coverage_days, round((datetime.fromisoformat(str(kline[-1]["date"]).replace("/", "-")) - datetime.fromisoformat(str(kline[0]["date"]).replace("/", "-"))).total_seconds() / 86400, 1))
    source_count = len({str(point.get("source") or "unknown") for point in points})
    if len(prices) >= 20 and coverage_days >= 10 and source_count == 1:
        quality, quality_label = "high", "样本较充分"
    elif len(prices) >= 8 and coverage_days >= 2:
        quality, quality_label = "medium", "可作辅助参考"
    else:
        quality, quality_label = "low", "样本偏少"
    changes = [
        (current - previous) / previous * 100
        for previous, current in zip(prices, prices[1:])
        if previous > 0
    ]
    volatility = round(statistics.pstdev(changes), 2) if len(changes) >= 3 else None
    peak = prices[0] if prices else current_price
    max_drawdown = 0.0
    for price in prices:
        peak = max(peak, price)
        if peak > 0:
            max_drawdown = min(max_drawdown, (price - peak) / peak * 100)
    current_peak = max(prices) if prices else current_price
    current_drawdown = round((current_price - current_peak) / current_peak * 100, 2) if current_peak else None
    trend_change = round((prices[-1] - prices[0]) / prices[0] * 100, 2) if len(prices) >= 3 and prices[0] else None
    if trend_change is None:
        trend_label = "趋势样本不足"
    elif trend_change >= 3:
        trend_label = f"样本区间上行 {trend_change:+.2f}%"
    elif trend_change <= -3:
        trend_label = f"样本区间下行 {trend_change:+.2f}%"
    else:
        trend_label = f"样本区间变化不大 {trend_change:+.2f}%"
    # 样本偏少时所有分位都退化成同一点(等于当前价), 显示出 ¥29.06~¥29.06
    # 这种"宽度=0 的区间"对用户没价值且容易误导. 必须 quality 至少 medium.
    zones_available = len(prices) >= 5 and quality != "low"
    buy_low = _market_percentile(prices, 0.20) if zones_available else None
    buy_high = _market_percentile(prices, 0.40) if zones_available else None
    sell_low = _market_percentile(prices, 0.70) if zones_available else None
    sell_high = _market_percentile(prices, 0.90) if zones_available else None
    risk_buffer = max(0.03, min(0.10, (volatility or 2.0) / 100 * 1.5))
    risk_line = buy_low * (1 - risk_buffer) if buy_low else None
    return {
        "available": zones_available,
        "buy_zone": {"low": round(buy_low, 3), "high": round(buy_high, 3)} if buy_low is not None and buy_high is not None else None,
        "sell_zone": {"low": round(sell_low, 3), "high": round(sell_high, 3)} if sell_low is not None and sell_high is not None else None,
        "risk_observation_line": round(risk_line, 3) if risk_line else None,
        "sample_count": len(prices),
        "coverage_days": coverage_days,
        "quality": quality,
        "quality_label": quality_label,
        "volatility_pct": volatility,
        "trend_change_pct": trend_change,
        "trend_label": trend_label,
        "current_drawdown_pct": current_drawdown,
        "max_drawdown_pct": round(max_drawdown, 2) if prices else None,
        "method": "参考区间来自本地历史价格样本的 20%–40% 与 70%–90% 分位，不预测未来，也不是自动买卖信号。",
    }


def _market_position_example(price: float, stop_line: float | None) -> dict[str, Any]:
    if not stop_line or stop_line <= 0 or price <= stop_line:
        return {"available": False, "message": "先写下止损线，才能按“最多亏多少”反推仓位。"}
    capital = 100_000.0
    risk_budget = 1_000.0
    loss_per_share = price - stop_line
    risk_limited = int(risk_budget / loss_per_share / 100) * 100 if loss_per_share > 0 else 0
    capital_limited = int(capital / price / 100) * 100 if price > 0 else 0
    shares = max(0, min(risk_limited, capital_limited))
    amount = round(shares * price, 2)
    return {
        "available": shares > 0,
        "capital": capital,
        "risk_budget": risk_budget,
        "risk_pct": 1.0,
        "shares": shares,
        "amount": amount,
        "position_pct": round(amount / capital * 100, 1) if capital else 0,
        "stop_distance_pct": round((price - stop_line) / price * 100, 2),
        "message": f"仅作风险算术示例：假设总资金 10 万元、单次最多亏 1%，按现价到止损线的距离，最多约 {shares} 股（约 {amount:.0f} 元）。" if shares else "止损距离过大，按 10 万元、单次最多亏 1% 的示例暂算不出一手。",
    }


MARKET_DECISION_GROUP_ORDER = {"must": 0, "near": 1, "watch": 2, "setup": 3, "unknown": 4}


def build_market_decision_center(snapshot: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    rules = _app_call("market_watchlist_rules", )
    watchlist = [item for item in _app_call("load_market_watchlist", ) if isinstance(item, dict) and item.get("symbol")]
    # 批量拉历史日K（腾讯前复权，进程内缓存）：自选票上市以来的真实价格，
    # 让「历史相对偏高/偏低区」不必等快照慢慢积累（新加自选当天就能算出）。
    _decision_kline: dict[str, list[dict[str, Any]]] = {}
    try:
        tencent_symbols = [_app_call("normalize_market_symbol", str(item.get("symbol") or "")) for item in watchlist]
        tencent_symbols = [sym for sym in tencent_symbols if re.fullmatch(r"(?:sh|sz|bj)\d{6}", sym or "")]
        if tencent_symbols:
            async def _fetch_all_klines(symbols: list[str]) -> list[Any]:
                return await asyncio.gather(*(_app_call("_tencent_kline", sym, 120) for sym in symbols), return_exceptions=True)

            kline_list = asyncio.run(_fetch_all_klines(tencent_symbols))
            for sym, kline in zip(tencent_symbols, kline_list):
                if isinstance(kline, list) and kline:
                    _decision_kline[_app_call("normalize_market_symbol", sym)] = kline
    except Exception:
        log.warning("拉取自选历史K线失败，回退到快照样本", exc_info=True)
    quotes = [item for item in (snapshot.get("quotes") or []) if isinstance(item, dict) and item.get("symbol")]
    quote_map = {_app_call("normalize_market_symbol", str(item.get("symbol") or "")): item for item in quotes}
    analysis = _app_call("analyze_market_snapshot", snapshot, history)
    cards: list[dict[str, Any]] = []
    for watch in watchlist:
        symbol = _app_call("normalize_market_symbol", str(watch.get("symbol") or ""))
        quote = quote_map.get(symbol)
        rule = rules.get(symbol, {})
        if not quote:
            cards.append({
                "symbol": symbol,
                "name": str(watch.get("name") or symbol),
                "group": "unknown",
                "action_key": "unknown",
                "action_label": "先刷新数据",
                "headline": "没有拿到有效行情",
                "price": None,
                "rules": rule,
                "reference": {"available": False, "sample_count": 0, "quality": "low", "quality_label": "没有样本"},
                "facts": ["当前行情源没有返回这只标的的有效价格。"],
                "risks": ["价格缺失时不应计算买卖区间或仓位。"],
                "position_example": {"available": False, "message": "没有现价，暂时不能计算仓位示例。"},
            })
            continue
        today_card = _market_today_for_quote(quote, rule)
        action_key = str(today_card.get("signal") or "unknown")
        group = "must" if action_key in {"stop", "sell", "buy"} else "near" if action_key == "near" else "setup" if action_key == "setup" else "watch" if action_key == "hold" else "unknown"
        price = float(today_card.get("price") or 0)
        points = _app_call("_market_history_points", symbol, snapshot, history)
        reference = _app_call("_market_reference_zones", points, price, kline=_decision_kline.get(symbol) or [])
        valuation = {
            "pe": _app_call("parse_market_number", quote.get("pe")),
            "pb": _app_call("parse_market_number", quote.get("pb")),
        }
        facts = [
            f"现价 {price:g}，今日{'上涨' if float(today_card.get('change_pct') or 0) >= 0 else '下跌'} {abs(float(today_card.get('change_pct') or 0)):.2f}%",
            reference["trend_label"],
        ]
        if reference.get("volatility_pct") is not None:
            facts.append(f"样本间波动约 {reference['volatility_pct']:.2f}%")
        if reference.get("current_drawdown_pct") is not None:
            facts.append(f"现价距样本高点 {reference['current_drawdown_pct']:.2f}%")
        if valuation["pe"] is not None or valuation["pb"] is not None:
            facts.append(f"当前快照估值：PE {valuation['pe'] if valuation['pe'] is not None else '缺失'}，PB {valuation['pb'] if valuation['pb'] is not None else '缺失'}")
        risks = []
        if reference["quality"] == "low":
            risks.append(f"只有 {reference['sample_count']} 个价格样本、覆盖 {reference['coverage_days']} 天，参考区间容易失真。")
        if reference.get("volatility_pct") is not None and reference["volatility_pct"] >= 3:
            risks.append("近期样本波动偏大，价格可能快速穿过参考区间。")
        if valuation["pe"] is None and valuation["pb"] is None:
            risks.append("缺少可核对的估值数据，不能判断公司是否便宜。")
        risks.append("价格历史看不到业绩、行业变化和突发消息，买前仍要核对基本面。")
        cards.append({
            "symbol": symbol,
            "name": str(quote.get("name") or watch.get("name") or symbol),
            "group": group,
            "action_key": action_key,
            "action_label": {"stop": "风险优先", "sell": "到了卖点", "buy": "到了买点", "near": "快到计划线", "hold": "继续观察", "setup": "先设计划"}.get(action_key, "先核对数据"),
            "headline": today_card.get("headline"),
            "action": today_card.get("action"),
            "price": price,
            "change_pct": today_card.get("change_pct"),
            "rules": rule,
            "reference": reference,
            "valuation": valuation,
            "facts": facts,
            "risks": risks,
            "position_example": _market_position_example(price, rule.get("stop_below")),
        })
    cards.sort(key=lambda card: (MARKET_DECISION_GROUP_ORDER.get(card.get("group", "unknown"), 9), -abs(float(card.get("change_pct") or 0))))
    counts = {key: sum(1 for card in cards if card.get("group") == key) for key in MARKET_DECISION_GROUP_ORDER}
    if counts["must"]:
        verdict = f"先处理 {counts['must']} 只触线标的"
        detail = "风险线优先，其次核对你自己的买入或卖出计划；工作台不会自动下单。"
        tone = "action"
    elif counts["near"]:
        verdict = "今天不用急着动"
        detail = f"有 {counts['near']} 只接近计划线，先把买入理由、止损和仓位写清楚。"
        tone = "watch"
    elif cards:
        verdict = "今天以观察和补计划为主"
        detail = "没有触到你的计划线。量化参考区只帮你定位，不替你做决定。"
        tone = "calm"
    else:
        verdict = "先建立第一份自选清单"
        detail = "添加标的、刷新行情，再积累历史样本，决策中心才会开始工作。"
        tone = "empty"
    return {
        "verdict": verdict,
        "detail": detail,
        "tone": tone,
        "checked_at": snapshot.get("checked_at") or "",
        "freshness": analysis.get("freshness") or {},
        "source": snapshot.get("source") or "未知行情源",
        "history_count": len(history),
        "counts": {"total": len(cards), **counts},
        "cards": cards,
        "disclaimer": "量化参考区间来自历史样本，不代表未来涨跌，不构成投资建议；本项目不连接券商、不自动下单。",
    }


@app.get("/api/market/decision-center")
def get_market_decision_center() -> dict[str, Any]:
    snapshot = _app_call("load_market_snapshot", )
    if snapshot.get("checked_at"):
        _app_call("record_market_snapshot", snapshot)
    history = _app_call("list_market_history", limit=100)
    return {"ok": True, "decision": _app_call("build_market_decision_center", snapshot, history)}


@app.post("/api/market/watchlist/rules")
def set_market_watchlist_rule(request: MarketRuleRequest) -> dict[str, Any]:
    try:
        return _app_call("save_market_watchlist_rule", request)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# 量化选股：按你设定的硬性条件筛出候选池，再用可解释的因子打分排序。
#
# 这里刻意不叫「推荐」。工作台能做的是：把全市场按你写下的规则过一遍，把每只
# 入选的原因、原始数据和**反面信号**摊开给你看。它没有基本面、没有行业中性化、
# 没有对手盘信息，所以它排第一不等于它该买 —— 排序只是把你的注意力放到哪几只
# 值得自己去查。每个结果都带 warnings 和 limitations，前端必须一起展示。
# ---------------------------------------------------------------------------

# 正域被上游限流时自动降级到 delay 域名（同接口，数据延迟约 1 分钟），最后腾讯兜底。
MARKET_UNIVERSE_URLS = [
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
]
MARKET_KLINE_URLS = [
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2his-delay.eastmoney.com/api/qt/stock/kline/get",
]
# 腾讯日线（前复权）：param=<market><code>,day,,,<limit>,qfq
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
# 深主板 + 创业板 + 沪主板 + 科创板
MARKET_UNIVERSE_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
MARKET_UNIVERSE_FIELDS = "f2,f3,f5,f6,f8,f9,f12,f13,f14,f20,f21,f23"
MARKET_SCREEN_CACHE_SECONDS = 900
_market_screen_cache: dict[str, Any] = {"at": 0.0, "rows": []}


class MarketScreenRequest(BaseModel):
    """筛选条件。全部有默认值，用户改哪条算哪条。"""

    min_market_cap: float = Field(default=50, ge=0, le=100_000, description="总市值下限（亿元）")
    max_market_cap: float = Field(default=3_000, ge=0, le=1_000_000, description="总市值上限（亿元）")
    min_pe: float = Field(default=0, ge=-1_000, le=10_000)
    max_pe: float = Field(default=60, ge=0, le=100_000)
    max_pb: float = Field(default=8, ge=0, le=10_000)
    min_turnover: float = Field(default=1.0, ge=0, le=100, description="换手率下限 %")
    min_amount: float = Field(default=1.0, ge=0, le=10_000, description="成交额下限（亿元）")
    exclude_st: bool = True
    weight_momentum: int = Field(default=40, ge=0, le=100)
    weight_value: int = Field(default=30, ge=0, le=100)
    weight_stability: int = Field(default=30, ge=0, le=100)
    limit: int = Field(default=15, ge=5, le=40)
    deep_pool: int = Field(default=80, ge=20, le=150, description="进入日线深度分析的候选数量")


def _screen_number(value: Any) -> float | None:
    """东财在停牌/无数据时返回 '-' 或 None，统一成 None。"""
    if value in (None, "", "-", "－"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def normalize_universe_rows(payload: Any) -> list[dict[str, Any]]:
    """把东财 clist 响应转成内部结构，字段缺失时保留 None 而不是编造 0。"""
    rows = (((payload or {}).get("data") or {}).get("diff")) or []
    if isinstance(rows, dict):  # 某些分页形态返回 dict 而不是 list
        rows = list(rows.values())
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("f12") or "").strip()
        name = str(row.get("f14") or "").strip()
        if not code or not name:
            continue
        market = str(row.get("f13") or "")
        result.append({
            "symbol": code,
            "secid": f"{'1' if market == '1' else '0'}.{code}",
            "name": name,
            "price": _screen_number(row.get("f2")),
            "change_pct": _screen_number(row.get("f3")),
            "volume": _screen_number(row.get("f5")),
            "amount": _screen_number(row.get("f6")),
            "turnover": _screen_number(row.get("f8")),
            "pe": _screen_number(row.get("f9")),
            "pb": _screen_number(row.get("f23")),
            "market_cap": _screen_number(row.get("f20")),
            "float_cap": _screen_number(row.get("f21")),
        })
    return result


def apply_screen_filters(rows: list[dict[str, Any]], criteria: MarketScreenRequest) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """硬性条件过滤。返回 (通过的行, 每条规则刷掉多少只)。

    统计每条规则的淘汰数是刻意的：条件设太死导致候选池为空时，用户要能一眼
    看出是哪一条把票都刷光了，而不是只看到一个空列表。
    """
    dropped = {
        "缺少价格或市值": 0, "ST/退市": 0, "市值区间": 0,
        "市盈率区间": 0, "市净率上限": 0, "换手率下限": 0, "成交额下限": 0,
    }
    passed: list[dict[str, Any]] = []
    for row in rows:
        cap_yi = (row["market_cap"] / 1e8) if row.get("market_cap") else None
        amount_yi = (row["amount"] / 1e8) if row.get("amount") else None
        if row.get("price") is None or cap_yi is None:
            dropped["缺少价格或市值"] += 1
            continue
        upper = row["name"].upper()
        if criteria.exclude_st and ("ST" in upper or "退" in row["name"]):
            dropped["ST/退市"] += 1
            continue
        if cap_yi < criteria.min_market_cap or cap_yi > criteria.max_market_cap:
            dropped["市值区间"] += 1
            continue
        pe = row.get("pe")
        # PE 为负（亏损）或缺失时按不满足处理：这个打分模型没有能力给亏损公司估值
        if pe is None or pe <= criteria.min_pe or pe > criteria.max_pe:
            dropped["市盈率区间"] += 1
            continue
        pb = row.get("pb")
        if pb is None or pb <= 0 or pb > criteria.max_pb:
            dropped["市净率上限"] += 1
            continue
        if (row.get("turnover") or 0) < criteria.min_turnover:
            dropped["换手率下限"] += 1
            continue
        if amount_yi is None or amount_yi < criteria.min_amount:
            dropped["成交额下限"] += 1
            continue
        row["market_cap_yi"] = round(cap_yi, 1)
        row["amount_yi"] = round(amount_yi, 2)
        passed.append(row)
    return passed, dropped


def compute_price_factors(closes: list[float]) -> dict[str, Any]:
    """从日收盘序列算动量、波动和回撤。数据不够就返回 None，不外推。"""
    closes = [value for value in closes if isinstance(value, (int, float)) and value > 0]
    if len(closes) < 25:
        return {"momentum_20d": None, "momentum_60d": None, "volatility": None, "drawdown_from_high": None, "points": len(closes)}
    latest = closes[-1]
    momentum_20 = round((latest / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None
    momentum_60 = round((latest / closes[-61] - 1) * 100, 2) if len(closes) >= 61 else None
    window = closes[-60:] if len(closes) >= 60 else closes
    returns = [(window[i] / window[i - 1] - 1) for i in range(1, len(window)) if window[i - 1]]
    volatility = round(statistics.pstdev(returns) * math.sqrt(250) * 100, 2) if len(returns) > 5 else None
    high = max(window)
    drawdown = round((latest / high - 1) * 100, 2) if high else None
    return {
        "momentum_20d": momentum_20,
        "momentum_60d": momentum_60,
        "volatility": volatility,
        "drawdown_from_high": drawdown,
        "points": len(closes),
    }


def _percentile_rank(sorted_values: list[float], value: float, *, higher_is_better: bool) -> float:
    """value 在样本中的百分位（0-100）。样本内排名，不跨市场比较。"""
    if not sorted_values:
        return 50.0
    below = sum(1 for item in sorted_values if item < value)
    equal = sum(1 for item in sorted_values if item == value)
    rank = (below + equal / 2) / len(sorted_values) * 100
    return round(rank if higher_is_better else 100 - rank, 1)


def score_candidates(candidates: list[dict[str, Any]], criteria: MarketScreenRequest) -> list[dict[str, Any]]:
    """三个维度各自在候选池内排百分位，再按权重加权。

    只在候选池内部排名 —— 这意味着分数是相对的：候选池整体都很差时，第一名
    依然是 90 分。前端必须把这句话显示出来。
    """
    usable = [item for item in candidates if item.get("momentum_20d") is not None]
    if not usable:
        return []
    momentum_values = sorted(item["momentum_20d"] for item in usable)
    pe_values = sorted(item["pe"] for item in usable if item.get("pe"))
    vol_values = sorted(item["volatility"] for item in usable if item.get("volatility") is not None)

    total_weight = max(1, criteria.weight_momentum + criteria.weight_value + criteria.weight_stability)
    scored: list[dict[str, Any]] = []
    for item in usable:
        momentum_score = _percentile_rank(momentum_values, item["momentum_20d"], higher_is_better=True)
        value_score = _percentile_rank(pe_values, item["pe"], higher_is_better=False) if item.get("pe") and pe_values else 50.0
        stability_score = (
            _percentile_rank(vol_values, item["volatility"], higher_is_better=False)
            if item.get("volatility") is not None and vol_values else 50.0
        )
        total = (
            momentum_score * criteria.weight_momentum
            + value_score * criteria.weight_value
            + stability_score * criteria.weight_stability
        ) / total_weight
        item = dict(item)
        item["scores"] = {
            "momentum": momentum_score,
            "value": value_score,
            "stability": stability_score,
            "total": round(total, 1),
        }
        item["warnings"] = _app_call("candidate_warnings", item)
        scored.append(item)
    scored.sort(key=lambda entry: entry["scores"]["total"], reverse=True)
    return scored


def candidate_warnings(item: dict[str, Any]) -> list[str]:
    """反面信号。排名越高越要看这里 —— 高分往往正是因为某个维度被拉满了。"""
    warnings: list[str] = []
    momentum = item.get("momentum_20d")
    momentum60 = item.get("momentum_60d")
    if momentum is not None and momentum > 40:
        warnings.append(f"20 日已经涨了 {momentum}%，这个位置买等于追高，回撤风险大。")
    if momentum is not None and momentum60 is not None and momentum > 0 and momentum60 < 0:
        warnings.append("短期在涨但 60 日仍是跌的，可能只是下跌途中的反弹。")
    if (item.get("volatility") or 0) > 60:
        warnings.append(f"年化波动 {item['volatility']}%，属于高波动品种，拿不住就别碰。")
    drawdown = item.get("drawdown_from_high")
    if drawdown is not None and drawdown > -3:
        warnings.append("紧贴 60 日最高点，一旦转头，回撤空间没有缓冲。")
    if (item.get("pe") or 0) > 40:
        warnings.append(f"市盈率 {item['pe']}，估值不便宜，业绩不及预期时杀估值很快。")
    if (item.get("turnover") or 0) > 20:
        warnings.append(f"换手率 {item['turnover']}%，交投过热，短线资金主导。")
    if (item.get("market_cap_yi") or 0) < 100:
        warnings.append("总市值不足 100 亿，流动性和抗风险能力都偏弱。")
    return warnings


MARKET_SCREEN_LIMITATIONS = [
    "只用了公开行情：价格、成交、市盈率、市净率。没有财报质量、没有行业景气、没有公告和舆情。",
    "分数是候选池内部的相对排名。整池都很差时，第一名照样是高分。",
    "动量因子在单边行情里有效，在震荡市会反复打脸；A 股的风格切换比成熟市场更频繁。",
    "市盈率对周期股和亏损公司几乎无意义，这套规则已经把亏损公司整体排除了。",
    "没有做行业中性化，结果可能集中在同一个板块，等于变相押注单一赛道。",
    "回测和实盘之间还隔着手续费、冲击成本和你自己的执行纪律。",
]


async def fetch_market_universe(force: bool = False) -> list[dict[str, Any]]:
    """拉取 A 股全市场快照（分页拉全，接口单页上限 100 条）。

    正域（push2）被限流时自动降级到 delay 域；全部失败返回空列表。
    缓存 15 分钟，避免反复打对方接口。
    """
    now = time.time()
    if not force and _market_screen_cache["rows"] and now - _market_screen_cache["at"] < MARKET_SCREEN_CACHE_SECONDS:
        return _market_screen_cache["rows"]
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    last_error: Exception | None = None
    for base_url in MARKET_UNIVERSE_URLS:
        try:
            rows: list[dict[str, Any]] = []
            page = 1
            page_size = 100
            while True:
                params = {
                    "pn": page, "pz": page_size, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                    "fid": "f6", "fs": MARKET_UNIVERSE_FS, "fields": MARKET_UNIVERSE_FIELDS,
                }
                async with httpx.AsyncClient(timeout=20, trust_env=False, headers=headers) as client:
                    response = await client.get(base_url, params=params)
                    response.raise_for_status()
                    payload = response.json()
                data = (payload or {}).get("data") or {}
                total = int(data.get("total") or 0)
                page_rows = _app_call("normalize_universe_rows", payload)
                rows.extend(page_rows)
                # 单页满 100 且未拉完 → 下一页；否则结束
                if not page_rows or len(page_rows) < page_size or len(rows) >= total:
                    break
                page += 1
                if page > 60:  # 安全上限（6000+ 只），防死循环
                    break
            if rows:
                _market_screen_cache["rows"] = rows
                _market_screen_cache["at"] = now
            return rows
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    return []


async def fetch_daily_closes(secid: str, limit: int = 70) -> list[float]:
    """取单只标的的日线收盘序列（前复权）。

    东财正域限流时降级 delay 域，再降级腾讯 fqkline。返回收盘价序列（可能为空）。
    """
    params = {
        "secid": secid, "klt": 101, "fqt": 1, "end": "20500101", "lmt": limit,
        "fields1": "f1", "fields2": "f51,f53",
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    last_error: Exception | None = None
    for base_url in MARKET_KLINE_URLS:
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, headers=headers) as client:
                response = await client.get(base_url, params=params)
                response.raise_for_status()
                payload = response.json()
            klines = (((payload or {}).get("data") or {}).get("klines")) or []
            closes: list[float] = []
            for line in klines:
                parts = str(line).split(",")
                if len(parts) >= 2:
                    value = _screen_number(parts[1])
                    if value:
                        closes.append(value)
            if closes:
                return closes
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    # 腾讯 fqkline 兜底：secid "1.600519" → sh600519
    try:
        market, code = str(secid).split(".", 1)
        tencent_symbol = ("sh" if market == "1" else "sz") + code
        async with httpx.AsyncClient(
            timeout=15, trust_env=False,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            response = await client.get(
                TENCENT_KLINE_URL,
                params={"param": f"{tencent_symbol},day,,,{limit},qfq"},
            )
            response.raise_for_status()
            payload = response.json()
        data = (((payload or {}).get("data") or {}).get(tencent_symbol)) or {}
        rows = data.get("qfqday") or data.get("day") or []
        closes = [_screen_number(str(row[2])) for row in rows if len(row) >= 3]
        return [value for value in closes if value]
    except Exception as exc:  # noqa: BLE001
        if last_error is not None:
            raise last_error from exc
        raise


async def run_market_screen(criteria: MarketScreenRequest) -> dict[str, Any]:
    universe = await fetch_market_universe()
    if not universe:
        raise RuntimeError("没有取到全市场行情。请点「数据源自检」确认上游接口是否可达。")
    passed, dropped = _app_call("apply_screen_filters", universe, criteria)
    # 按成交额取前 N 只做日线深度分析：日线是一只一个请求，必须有上限。
    passed.sort(key=lambda row: row.get("amount") or 0, reverse=True)
    deep = passed[: criteria.deep_pool]

    semaphore = asyncio.Semaphore(8)

    async def enrich(row: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                closes = await fetch_daily_closes(row["secid"])
            except Exception:
                closes = []
        return {**row, **_app_call("compute_price_factors", closes)}

    enriched = await asyncio.gather(*(enrich(row) for row in deep), return_exceptions=False)
    scored = _app_call("score_candidates", list(enriched), criteria)
    return {
        "generated_at": now_iso(),
        "universe_size": len(universe),
        "passed_filters": len(passed),
        "deep_analyzed": len(deep),
        "dropped_by_rule": dropped,
        "candidates": scored[: criteria.limit],
        "criteria": criteria.model_dump(),
        "limitations": MARKET_SCREEN_LIMITATIONS,
        "policy": "这是按你设定条件排序的候选池，不是买入建议。工作台不连接券商，不会下单。",
    }


# ---------------------------------------------------------------------------
# 选股风格库
#
# 设计前提（很重要，别绕过它）：当前行情源只有价格和成交量，没有 PE、PB、ROE、
# 营收增速这些基本面字段——app.py 里 market_factors 的注释早就写明了这一点，
# 并且明确选择「缺就是缺，不靠推断补」。
#
# 所以这里不做「价值风格选股」这种拿价格算估值的假动作。每个风格显式声明它需要
# 哪些数据；数据不够就不出结论，而是告诉你差什么、去哪补。一个只说什么时候管用、
# 不说什么时候会亏的选股策略是有害的，所以每个风格都必须写失效场景。
#
# 刻意不用真人姓名：公开的投资大师方法本来就没有完整规则，把某个在世投资人的
# 名字挂在自动选股结果上，既不准确，出了亏损也说不清。用风格流派 + 可查证的
# 量化条件更实在。
# ---------------------------------------------------------------------------
MARKET_STYLE_DATA_LABELS = {
    "price_series": "价格时间序列",
    "volume_series": "成交量时间序列",
    "pe": "市盈率",
    "pb": "市净率",
    "dividend_yield": "股息率",
    "revenue_growth": "营收增速",
    "profit_growth": "利润增速",
    "roe": "净资产收益率",
    "debt_ratio": "资产负债率",
    "market_cap": "总市值",
}

MARKET_STYLES: list[dict[str, Any]] = [
    {
        "id": "trend-following",
        "name": "趋势跟随",
        "thesis": "价格的方向比价格的高低更有信息量；上涨中的标的更可能继续上涨，直到趋势被打断。",
        "requires": ["price_series"],
        "min_points": 20,
        "rules": [
            "样本区间累计涨幅为正，且最近 1/3 区间的涨幅不弱于整体",
            "最大回撤小于区间涨幅的一半（涨得动也扛得住）",
            "价格在样本后段位于全区间中位数之上",
        ],
        "works_when": "市场有明确主线、资金持续流入某个方向时最有效。",
        "fails_when": "震荡市里会被反复打脸——趋势策略在横盘中天然亏钱，因为每次信号都是假突破。政策或情绪突变导致的急转弯也躲不掉。",
    },
    {
        "id": "momentum-rotation",
        "name": "相对强度轮动",
        "thesis": "比较同一时间窗口内不同标的的相对表现，持有跑得快的、换掉跑得慢的。",
        "requires": ["price_series"],
        "min_points": 20,
        "rules": [
            "在自选池内按区间涨幅排名，取前 1/3",
            "剔除波动率排名同样靠前的（涨幅来自剧烈波动而非稳定上行）",
            "需要至少 5 只标的才有比较意义",
        ],
        "works_when": "板块分化明显、有明确领涨方向时。",
        "fails_when": "普涨或普跌时排名没有区分度；调仓频率高会被交易成本吃掉大部分超额收益，A 股还要额外考虑印花税。",
    },
    {
        "id": "mean-reversion",
        "name": "超跌回归",
        "thesis": "短期非理性下跌往往过度，价格会向近期均值回归。",
        "requires": ["price_series"],
        "min_points": 20,
        "rules": [
            "当前价格显著低于样本均值（偏离超过一个标准差）",
            "下跌过程中没有出现持续放量（放量下跌通常意味着基本面变化，不是情绪超跌）",
            "样本区间整体不是单边下行",
        ],
        "works_when": "情绪性错杀、无基本面变化的短期回调。",
        "fails_when": "遇到真实利空时这个策略最危险——它会让你在下跌趋势里不断加仓。所谓「接飞刀」说的就是它。必须配合基本面排查，而当前数据源没有基本面。",
    },
    {
        "id": "volume-breakout",
        "name": "放量突破",
        "thesis": "价格创出区间新高且伴随成交量明显放大，说明有增量资金认可这个价格。",
        "requires": ["price_series", "volume_series"],
        "min_points": 20,
        "rules": [
            "最新价格接近或超过样本区间最高价",
            "最近成交量高于区间均量的 1.5 倍",
            "突破前有一段窄幅整理（波动率低于区间中位数）",
        ],
        "works_when": "有明确催化剂（业绩、政策、订单）的启动初期。",
        "fails_when": "放量也可能是出货。单看量价无法区分「资金进场」和「主力派发」，这是这个风格最本质的盲区。缩量新高同样常见，会被这套规则漏掉。",
    },
    {
        "id": "low-volatility",
        "name": "低波动防守",
        "thesis": "长期看，波动更小的组合在同等收益下体验更好，回撤更浅也更容易拿得住。",
        "requires": ["price_series"],
        "min_points": 20,
        "rules": [
            "区间波动率排在自选池后 1/3",
            "最大回撤小于自选池中位数",
            "区间收益不为负",
        ],
        "works_when": "市场不确定性高、你更在意回撤而不是弹性时。",
        "fails_when": "牛市里会显著跑输——低波动的代价就是放弃弹性。另外历史低波动不保证未来低波动，黑天鹅面前所有低波动都会失效。",
    },
    # ---- 以下风格当前数据源支撑不了，显式列出而不是假装能算 ----
    {
        "id": "deep-value",
        "name": "低估值",
        "thesis": "用显著低于内在价值的价格买入，安全边际来自估值本身。",
        "requires": ["pe", "pb", "dividend_yield"],
        "min_points": 0,
        "rules": ["市盈率与市净率处于历史分位低位", "股息率高于市场中位数", "排除盈利为负导致的假低估"],
        "works_when": "市场系统性悲观、优质资产被无差别抛售时。",
        "fails_when": "便宜可能有便宜的道理——价值陷阱是这个风格的主要亏损来源。行业衰退期的低估值往往会更低。",
    },
    {
        "id": "quality-growth",
        "name": "质量成长",
        "thesis": "持续高回报率且能维持增长的生意，时间站在你这边。",
        "requires": ["revenue_growth", "profit_growth", "roe", "debt_ratio"],
        "min_points": 0,
        "rules": ["净资产收益率连续多期高于阈值", "营收与利润同步增长", "负债率不因扩张而失控"],
        "works_when": "经济扩张期、优质公司能持续兑现增长时。",
        "fails_when": "好公司未必是好股票——买得太贵时，业绩兑现了股价照样跌。增速一旦放缓，高估值的杀伤力非常大。",
    },
]


def market_style_catalog() -> list[dict[str, Any]]:
    """风格目录（含数据依赖），前端据此渲染，不在页面里硬编码规则。"""
    return [
        {
            **{key: value for key, value in style.items() if key != "requires"},
            "requires": [{"id": item, "label": MARKET_STYLE_DATA_LABELS.get(item, item)} for item in style["requires"]],
        }
        for style in MARKET_STYLES
    ]


def _style_series(points: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    prices = [float(p["price"]) for p in points if isinstance(p.get("price"), (int, float))]
    volumes = [float(p["volume"]) for p in points if isinstance(p.get("volume"), (int, float)) and p["volume"] >= 0]
    return prices, volumes


def _style_metrics(prices: list[float], volumes: list[float]) -> dict[str, Any]:
    """只算价格和成交量能支撑的指标，一个都不外推。"""
    if len(prices) < 2:
        return {}
    start, end = prices[0], prices[-1]
    peak, trough_after_peak = prices[0], 0.0
    max_drawdown = 0.0
    for price in prices:
        peak = max(peak, price)
        if peak:
            max_drawdown = min(max_drawdown, (price - peak) / peak * 100)
    returns = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices)) if prices[i - 1]]
    volatility = round(statistics.pstdev(returns) * 100, 3) if len(returns) >= 2 else None
    tail = prices[max(0, len(prices) * 2 // 3):]
    tail_return = round((tail[-1] - tail[0]) / tail[0] * 100, 2) if len(tail) >= 2 and tail[0] else None
    return {
        "return_pct": round((end - start) / start * 100, 2) if start else None,
        "tail_return_pct": tail_return,
        "max_drawdown_pct": round(max_drawdown, 2),
        "volatility_pct": volatility,
        "median_price": round(statistics.median(prices), 4),
        "mean_price": round(statistics.fmean(prices), 4),
        "price_stdev": round(statistics.pstdev(prices), 4) if len(prices) >= 2 else None,
        "last_price": round(end, 4),
        "max_price": round(max(prices), 4),
        "volume_ratio": round(volumes[-1] / statistics.fmean(volumes), 2) if len(volumes) >= 3 and statistics.fmean(volumes) else None,
        "points": len(prices),
        "volume_points": len(volumes),
    }


def evaluate_market_style(style_id: str, symbol: str, points: list[dict[str, Any]], peer_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """对单个标的评估一个风格。

    返回里一定包含 status：
      ready        —— 数据够，给出判断
      insufficient —— 数据不够，明确说差多少
      unsupported  —— 当前数据源根本没有这个风格需要的字段
    绝不在 insufficient/unsupported 时给分数或结论——那正是「看着专业实则编造」。
    """
    style = next((item for item in MARKET_STYLES if item["id"] == style_id), None)
    if not style:
        raise HTTPException(404, f"没有这个选股风格：{style_id}")

    missing_fields = [item for item in style["requires"] if item not in {"price_series", "volume_series"}]
    if missing_fields:
        return {
            "style_id": style_id, "style": style["name"], "symbol": symbol, "status": "unsupported",
            "reason": "当前行情源只提供价格和成交量",
            "missing": [{"id": item, "label": MARKET_STYLE_DATA_LABELS.get(item, item)} for item in missing_fields],
            "next_step": "接入含基本面字段的数据源后，这个风格才能给出结论。",
        }

    prices, volumes = _style_series(points)
    needed = int(style["min_points"])
    if len(prices) < needed:
        return {
            "style_id": style_id, "style": style["name"], "symbol": symbol, "status": "insufficient",
            "reason": f"需要至少 {needed} 个价格样本，当前只有 {len(prices)} 个",
            "have": len(prices), "need": needed,
            "next_step": "先让行情自动化按固定间隔采样，攒够样本再看结论。",
        }
    if "volume_series" in style["requires"] and len(volumes) < needed:
        return {
            "style_id": style_id, "style": style["name"], "symbol": symbol, "status": "insufficient",
            "reason": f"需要至少 {needed} 个成交量样本，当前只有 {len(volumes)} 个",
            "have": len(volumes), "need": needed,
            "next_step": "行情源需要同时记录成交量。",
        }

    metrics = _style_metrics(prices, volumes)
    peer = peer_context or {}
    checks: list[dict[str, Any]] = []

    def check(passed: bool, label: str, detail: str) -> None:
        checks.append({"passed": bool(passed), "label": label, "detail": detail})

    if style_id == "trend-following":
        ret, tail, dd = metrics.get("return_pct"), metrics.get("tail_return_pct"), metrics.get("max_drawdown_pct")
        check(ret is not None and ret > 0, "区间上行", f"区间涨幅 {ret}%")
        # 按「每期涨幅」比较，而不是直接拿后段百分比和全段的 1/3 比。
        # 后段基数更高，同样的斜率算出来的百分比天然更小——那样会系统性
        # 冤枉稳定上涨的标的，而这正是趋势跟随最该选中的形态。
        points_total = max(1, int(metrics.get("points") or 1) - 1)
        points_tail = max(1, points_total // 3)
        pace_total = (ret / points_total) if ret is not None else None
        pace_tail = (tail / points_tail) if tail is not None else None
        check(
            pace_tail is not None and pace_total is not None and pace_tail >= pace_total * 0.5,
            "后段未失速",
            f"后段每期 {round(pace_tail, 3) if pace_tail is not None else '—'}% / 全段每期 {round(pace_total, 3) if pace_total is not None else '—'}%",
        )
        check(dd is not None and ret is not None and ret > 0 and abs(dd) < max(ret / 2, 1e-9), "回撤可控", f"最大回撤 {dd}%")
        check(metrics.get("last_price", 0) >= metrics.get("median_price", 0), "价格居于中位数之上", f"最新 {metrics.get('last_price')} / 中位 {metrics.get('median_price')}")
    elif style_id == "momentum-rotation":
        rank, total = peer.get("return_rank"), peer.get("peer_count", 0)
        check(total >= 5, "样本池足够比较", f"自选池 {total} 只（至少 5 只）")
        check(rank is not None and total and rank <= max(1, total // 3), "涨幅排名靠前", f"排名 {rank}/{total}")
        vol_rank = peer.get("volatility_rank")
        check(vol_rank is None or (total and vol_rank > total // 3), "涨幅不是靠剧烈波动", f"波动率排名 {vol_rank}/{total}")
    elif style_id == "mean-reversion":
        last, mean, sd = metrics.get("last_price"), metrics.get("mean_price"), metrics.get("price_stdev")
        deviated = sd is not None and sd > 0 and last is not None and mean is not None and (mean - last) > sd
        check(deviated, "显著低于均值", f"最新 {last} / 均值 {mean} / 标准差 {sd}")
        check((metrics.get("volume_ratio") or 0) < 1.5 if metrics.get("volume_ratio") is not None else False,
              "下跌未持续放量", f"最新量比 {metrics.get('volume_ratio')}")
        check((metrics.get("return_pct") or 0) > -30, "并非单边崩塌", f"区间涨幅 {metrics.get('return_pct')}%")
    elif style_id == "volume-breakout":
        last, top = metrics.get("last_price"), metrics.get("max_price")
        check(last is not None and top and last >= top * 0.98, "接近区间新高", f"最新 {last} / 区间最高 {top}")
        check((metrics.get("volume_ratio") or 0) >= 1.5, "成交显著放大", f"量比 {metrics.get('volume_ratio')}")
        check(metrics.get("volatility_pct") is not None, "有波动率样本", f"波动率 {metrics.get('volatility_pct')}%")
    elif style_id == "low-volatility":
        vol_rank, total = peer.get("volatility_rank"), peer.get("peer_count", 0)
        check(total >= 3, "样本池足够比较", f"自选池 {total} 只")
        check(vol_rank is not None and total and vol_rank > total * 2 // 3, "波动排名靠后", f"波动率排名 {vol_rank}/{total}")
        check((metrics.get("return_pct") or 0) >= 0, "区间收益不为负", f"区间涨幅 {metrics.get('return_pct')}%")

    passed = sum(1 for item in checks if item["passed"])
    return {
        "style_id": style_id, "style": style["name"], "symbol": symbol, "status": "ready",
        "hit": passed == len(checks) and bool(checks),
        "score": round(passed / len(checks), 2) if checks else 0.0,
        "checks": checks, "metrics": metrics,
        "works_when": style["works_when"], "fails_when": style["fails_when"],
    }


def run_market_style_screen(style_id: str, symbols: list[str] | None = None) -> dict[str, Any]:
    """一键按风格筛自选池。

    数据不够时不给排名——宁可返回一份「差什么」的清单，也不返回一个看起来
    很专业、实际建立在 3 个样本点上的推荐。
    """
    style = next((item for item in MARKET_STYLES if item["id"] == style_id), None)
    if not style:
        raise HTTPException(404, f"没有这个选股风格：{style_id}")
    watchlist = [str(item.get("symbol") or "").strip() for item in _app_call("load_market_watchlist", )]
    targets = [item for item in (symbols or watchlist) if item]
    if not targets:
        raise HTTPException(400, "自选池是空的，先添加要观察的标的")

    snapshot = _app_call("load_market_snapshot", )
    history = _app_call("list_market_history", limit=400)

    # 先算全池指标，才能做相对排名（轮动和低波动都依赖同池比较）。
    per_symbol: dict[str, dict[str, Any]] = {}
    for symbol in targets:
        points = _app_call("_market_history_points", symbol, snapshot, history)
        prices, volumes = _style_series(points)
        per_symbol[symbol] = {"points": points, "metrics": _style_metrics(prices, volumes) if len(prices) >= 2 else {}}

    comparable = [item for item in targets if per_symbol[item]["metrics"].get("return_pct") is not None]
    by_return = sorted(comparable, key=lambda item: per_symbol[item]["metrics"]["return_pct"], reverse=True)
    by_volatility = sorted(
        [item for item in comparable if per_symbol[item]["metrics"].get("volatility_pct") is not None],
        key=lambda item: per_symbol[item]["metrics"]["volatility_pct"],
    )

    results = []
    for symbol in targets:
        peer = {
            "peer_count": len(comparable),
            "return_rank": by_return.index(symbol) + 1 if symbol in by_return else None,
            "volatility_rank": by_volatility.index(symbol) + 1 if symbol in by_volatility else None,
        }
        results.append(_app_call("evaluate_market_style", style_id, symbol, per_symbol[symbol]["points"], peer))

    ready = [item for item in results if item["status"] == "ready"]
    picks = sorted([item for item in ready if item.get("hit")], key=lambda item: item.get("score", 0), reverse=True)
    blocked = [item for item in results if item["status"] != "ready"]

    return {
        "style": {key: value for key, value in style.items() if key != "requires"},
        "picks": picks,
        "evaluated": results,
        "blocked": blocked,
        "data_ready": not blocked,
        "summary": (
            f"{len(picks)} 只命中「{style['name']}」全部条件"
            if ready and picks else
            (f"自选池 {len(targets)} 只，没有标的同时满足全部条件" if ready else
             f"数据不足，{len(blocked)} 只标的无法评估——先补齐数据再看结论")
        ),
        "disclaimer": (
            "这是基于本地历史快照的规则匹配，不是投资建议，也不会下单。"
            "每个风格的失效场景写在 fails_when 里，用之前请先读它。"
        ),
    }


@app.get("/api/market/styles")
def get_market_styles() -> dict[str, Any]:
    """选股风格目录：每个风格声明它需要什么数据、什么时候管用、什么时候会亏。"""
    return {
        "styles": _app_call("market_style_catalog", ),
        "available_fields": ["price_series", "volume_series"],
        "note": "当前行情源只提供价格与成交量；依赖基本面的风格会明确标为数据不支持，而不是用价格硬凑。",
    }


class MarketStyleScreenRequest(BaseModel):
    style_id: str = Field(min_length=1, max_length=60)
    symbols: list[str] = Field(default_factory=list, max_length=60)


@app.post("/api/market/styles/screen")
def post_market_style_screen(request: MarketStyleScreenRequest) -> dict[str, Any]:
    return _app_call("run_market_style_screen", request.style_id, [item.strip() for item in request.symbols if item.strip()])


@app.post("/api/market/screen")
async def post_market_screen(request: MarketScreenRequest) -> dict[str, Any]:
    try:
        return {"ok": True, **await run_market_screen(request)}
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"行情数据源不可达：{clip(str(exc), 200)}") from exc


@app.get("/api/market/screen/selftest")
async def get_market_screen_selftest() -> dict[str, Any]:
    """上线后先点这个：确认上游可达、字段齐全，再去用选股。"""
    report: dict[str, Any] = {"ok": False, "universe": {}, "kline": {}}
    try:
        rows = await fetch_market_universe(force=True)
        sample = rows[0] if rows else {}
        missing = [key for key in ("price", "pe", "pb", "market_cap", "turnover", "amount") if sample.get(key) is None]
        report["universe"] = {
            "reachable": True,
            "rows": len(rows),
            "sample": sample,
            "missing_fields": missing,
            "verdict": "字段齐全" if not missing else f"缺字段：{'、'.join(missing)}（可能是该只停牌，可再试）",
        }
    except Exception as exc:
        report["universe"] = {"reachable": False, "error": clip(str(exc), 300)}
        return report
    try:
        closes = await fetch_daily_closes("1.600519")
        report["kline"] = {
            "reachable": True,
            "points": len(closes),
            "factors": _app_call("compute_price_factors", closes),
            "verdict": "可用" if len(closes) >= 25 else "返回点数不足，动量无法计算",
        }
    except Exception as exc:
        report["kline"] = {"reachable": False, "error": clip(str(exc), 300)}
        return report
    report["ok"] = bool(report["universe"].get("rows")) and bool(report["kline"].get("points"))
    return report

@app.get("/api/market/sampling")
def get_market_sampling() -> dict[str, Any]:
    return {"ok": True, "sampling": _app_call("market_sampling_state", )}


@app.put("/api/market/sampling")
def update_market_sampling(request: MarketSamplingRequest) -> dict[str, Any]:
    interval_seconds = int(request.interval_seconds)
    if interval_seconds not in MARKET_SAMPLING_INTERVALS:
        allowed = "、".join(str(item) for item in MARKET_SAMPLING_INTERVALS)
        raise HTTPException(400, f"采样周期不受支持，只能选择：{allowed} 秒。")

    watchlist = _app_call("load_market_watchlist", )
    existing = _app_call("market_sampling_rule", )
    if request.enabled and not watchlist:
        raise HTTPException(400, "当前没有自选股票，不能开启历史样本采集；请先保存自选。")

    config = {
        "source": MARKET_SAMPLING_SOURCE,
        "purpose": "historical_sampling",
        "interval_seconds": interval_seconds,
        "policy": "只读取公开行情并保存本地历史快照，不连接券商、不自动下单。",
    }
    if existing:
        rule = _app_call("save_automation_rule", 
            name=MARKET_SAMPLING_RULE_NAME,
            kind="market_refresh",
            project_id="market",
            schedule=f"every:{interval_seconds}",
            enabled=bool(request.enabled),
            config={**(existing.get("config") or {}), **config},
            rule_id=int(existing["id"]),
        )
    elif request.enabled:
        rule = _app_call("save_automation_rule", 
            name=MARKET_SAMPLING_RULE_NAME,
            kind="market_refresh",
            project_id="market",
            schedule=f"every:{interval_seconds}",
            enabled=True,
            config=config,
        )
    else:
        # Stopping a never-created sampler is an idempotent no-op.  In
        # particular, disabling must not create a rule or mutate history.
        rule = None

    state = _app_call("market_sampling_state", )
    return {"ok": True, "sampling": state, "rule": rule}


@app.post("/api/market/report")
async def generate_market_report(request: MarketReportRequest) -> dict[str, Any]:
    if not _app_call("llm_settings", )["configured"]:
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM")
    snapshot = _app_call("load_market_snapshot", )
    history = await asyncio.to_thread(list_market_history, limit=30)
    analysis = await asyncio.to_thread(analyze_market_snapshot, snapshot, history)
    watchlist = snapshot.get("watchlist", [])
    if not watchlist:
        raise HTTPException(400, "自选股为空，先添加股票并刷新行情")
    quotes = snapshot.get("quotes", [])
    if not quotes:
        raise HTTPException(400, "还没有行情快照，先刷新行情")
    if not analysis.get("valid_quote_count"):
        raise HTTPException(400, "当前快照没有有效报价，暂不生成量化报告")

    period_label = "日报" if request.period == "daily" else "周报"
    def factor_prompt(signal: dict[str, Any]) -> str:
        rendered: list[str] = []
        for factor in signal.get("factor_details") or []:
            label = str(factor.get("label") or "因子")
            if factor.get("status") == "missing":
                rendered.append(f"{label}=样本不足（{factor.get('missing_reason') or '未达到最低样本'}）")
            elif factor.get("value") is None:
                rendered.append(f"{label}=未形成数值（{factor.get('observation') or '仅作状态记录'}）")
            else:
                rendered.append(f"{label}={factor.get('value')}{factor.get('unit') or ''}（{factor.get('observation') or '已计算'}）")
        return "；".join(rendered) or "暂无可用因子"

    signals_text = "\n".join(
        f"- {item.get('name') or item.get('symbol')}（{item.get('symbol')}）：{item.get('observation')}"
        + f"；因子：{factor_prompt(item)}"
        for item in analysis.get("signals", [])
    ) or "（暂无形成观察）"
    warnings_text = "；".join(analysis.get("warnings", [])) or "无"
    freshness_label = (analysis.get("freshness") or {}).get("label", "未知")
    checked_at = str(snapshot.get("checked_at") or "")
    confidence = analysis.get("research_confidence") if isinstance(analysis.get("research_confidence"), dict) else {}
    confidence_text = (
        f"{confidence.get('label', 'unknown')} {round(float(confidence.get('score') or 0) * 100)}%"
        f"；有效报价 {confidence.get('valid_quote_count', 0)}；拒绝/缺失 {confidence.get('rejected_quote_count', 0)}"
        f"；覆盖 {confidence.get('coverage_days', 0)} 天；来源稳定性 {confidence.get('source_stability', 'unknown')}"
    )
    quotes_text = "\n".join(
        f"- {item.get('name') or item.get('symbol')}（{item.get('symbol')}）：{item.get('price')}，涨跌幅 {item.get('change_pct')}%"
        for item in quotes[:20]
    )
    prompt = (
        f"你是量化研究助手，基于以下本机行情快照生成一份简洁的{period_label}。"
        f"数据时间：{checked_at}，数据新鲜度：{freshness_label}，研究可信度：{confidence_text}。\n\n"
        f"自选股当前行情：\n{quotes_text}\n\n"
        f"可解释观察：\n{signals_text}\n\n"
        f"告警/提醒：{warnings_text}\n\n"
        "要求：先给一段总结，再按股票列出关键变化和关注点，最后给出 2-3 条下一步研究建议。"
        "只基于上面给出的数据，不编造；样本不足的因子要明确说明。研究可信度低或中时降低确定性措辞。不要给具体买卖建议。"
    )
    try:
        answer = await _app_call("call_llm", 
            [{"role": "system", "content": "你是本地量化研究助手，输出可直接阅读的中文报告，不构成投资建议。"}, {"role": "user", "content": prompt}],
            max_tokens=3000,
            temperature=0.3,
        )
    except httpx.HTTPStatusError as exc:
        detail = clip(exc.response.text, 500)
        raise HTTPException(502, f"生成失败：上游返回 {exc.response.status_code}：{detail}") from exc
    except Exception as exc:
        raise HTTPException(502, f"生成失败：{exc}") from exc

    output_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-量化{period_label}.md"
    output_path = _OUTPUTS_DIR() / output_name
    output_path.write_text(f"# 量化{period_label}\n\n> 数据时间：{checked_at} · 数据新鲜度：{freshness_label} · 来源：Workbench 量化研究 Agent\n\n{answer.rstrip()}\n", encoding="utf-8")
    artifact = await asyncio.to_thread(_app_call, "register_artifact_safely", 
        project_id="market",
        name=output_name,
        path=str(output_path),
        kind=f"market_{request.period}_report",
        metadata={
            "period": request.period,
            "period_label": period_label,
            "checked_at": checked_at,
            "watch_count": len(watchlist),
            "freshness": freshness_label,
            "research_confidence": confidence,
        },
    )
    await asyncio.to_thread(create_notification_record, 
        title=f"量化{period_label}已生成",
        body=f"{period_label}基于 {checked_at[:16].replace('T', ' ')} 快照生成，保存到 outputs/。",
        project_id="market",
        kind="agent_result",
        level="info",
        href="/projects/market",
        event_key=f"market:{request.period}_report:{output_name}",
        dedupe_seconds=0,
    )
    return {"ok": True, "period": request.period, "period_label": period_label, "answer": answer, "filename": output_name, "path": str(output_path), "artifact": artifact}


@app.put("/api/market/watchlist")
def update_market_watchlist(request: MarketWatchlistRequest) -> dict[str, Any]:
    symbols = []
    for raw in request.symbols:
        normalized = _app_call("normalize_market_symbol", raw)
        if normalized and normalized not in symbols:
            symbols.append(normalized)
    existing_by_symbol = {
        _app_call("normalize_market_symbol", str(item.get("symbol") or "")): item
        for item in _app_call("load_market_watchlist", )
        if isinstance(item, dict) and _app_call("normalize_market_symbol", str(item.get("symbol") or ""))
    }
    # Keep the user's buy/sell/stop lines for symbols that remain selected.
    # Editing the list must not silently erase an existing plan.
    values = [{**existing_by_symbol.get(symbol, {}), "symbol": symbol} for symbol in symbols]
    _app_call("save_market_watchlist", values)
    snapshot = _app_call("load_market_snapshot", )
    snapshot["watchlist"] = values
    allowed_symbols = set(symbols)
    snapshot["quotes"] = [
        item for item in (snapshot.get("quotes") or [])
        if isinstance(item, dict)
        and _app_call("normalize_market_symbol", str(item.get("symbol") or "")) in allowed_symbols
    ]
    snapshot["missing_symbols"] = [
        symbol for symbol in (snapshot.get("missing_symbols") or [])
        if _app_call("normalize_market_symbol", str(symbol or "")) in allowed_symbols
    ]
    if not values:
        snapshot.update({"quotes": [], "checked_at": "", "status": "empty", "missing_symbols": []})
    _app_call("save_market_snapshot", snapshot)
    _app_call("record_market_snapshot", snapshot)
    history = _app_call("list_market_history", limit=30)
    observation_tasks = [
        item for item in _app_call("list_work_items", "all", "market")
        if item.get("source_project") == "market" and item.get("kind") == "research_observation"
    ][:30]
    return {
        "ok": True,
        "watchlist": values,
        "quotes": snapshot.get("quotes", []),
        "market": snapshot,
        "analysis": _app_call("analyze_market_snapshot", snapshot, history),
        "history": history,
        "observation_tasks": observation_tasks,
    }


@app.post("/api/market/refresh")
async def refresh_market_quotes() -> dict[str, Any]:
    watchlist = _app_call("load_market_watchlist", )
    symbols = [item.get("symbol", "") for item in watchlist]
    if not symbols:
        snapshot = {"watchlist": [], "quotes": [], "checked_at": "", "source": "", "status": "empty", "missing_symbols": []}
        _app_call("save_market_snapshot", snapshot)
        return {"ok": True, **snapshot, "market": snapshot, "message": "当前没有自选股票，已清空旧行情展示"}
    try:
        quotes = await _app_call("fetch_market_quotes", symbols)
    except Exception as exc:
        raise HTTPException(502, f"行情源暂时不可用，已保留上次快照：{exc}") from exc
    if not quotes:
        raise HTTPException(502, "行情源未返回有效报价，已保留上次快照；请稍后重试。")
    normalized_symbols = [_app_call("normalize_market_symbol", item.get("symbol", "")) for item in watchlist]
    returned_symbols = {_app_call("normalize_market_symbol", item.get("symbol", "")) for item in quotes}
    missing_symbols = [symbol for symbol in normalized_symbols if symbol and symbol not in returned_symbols]
    snapshot = {
        "watchlist": watchlist,
        "quotes": quotes,
        "checked_at": now_iso(),
        "source": "Tencent quote",
        "status": "partial" if missing_symbols else "ok",
        "missing_symbols": missing_symbols,
    }
    _app_call("save_market_snapshot", snapshot)
    await asyncio.to_thread(record_market_snapshot, snapshot)
    history = await asyncio.to_thread(list_market_history, limit=30)
    artifact = await asyncio.to_thread(_app_call, "register_artifact_safely", 
        project_id="market",
        name="market_snapshot.json",
        path=str(MARKET_SNAPSHOT_FILE),
        kind="market_snapshot",
        metadata={"checked_at": snapshot.get("checked_at"), "quote_count": len(quotes), "missing_symbols": missing_symbols},
    )
    message = f"行情已更新 · 返回 {len(quotes)}/{len(watchlist)} 只自选"
    if missing_symbols:
        message += f" · {len(missing_symbols)} 只未返回"
    observation_tasks = [
        item for item in _app_call("list_work_items", "all", "market")
        if item.get("source_project") == "market" and item.get("kind") == "research_observation"
    ][:30]
    return {"ok": True, "market": snapshot, "analysis": _app_call("analyze_market_snapshot", snapshot, history), "history": history, "observation_tasks": observation_tasks, "message": message, "artifact": artifact}


@app.post("/api/market/observations/evaluate")
def evaluate_market_observations_route() -> dict[str, Any]:
    result = _app_call("evaluate_market_observations", create_records=True)
    observation_tasks = [
        item for item in _app_call("list_work_items", "all", "market")
        if item.get("source_project") == "market" and item.get("kind") == "research_observation"
    ][:30]
    return {"ok": True, **result, "observation_tasks": observation_tasks}




class MarketResearchRequest(BaseModel):
    symbol: str = Field(default="", max_length=20)
    question: str = Field(min_length=1, max_length=2_000)
    event_date: str = Field(default="", max_length=40)


class MarketStrategyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    rules: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=2_000)


class MarketBacktestRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    strategy: str = Field(default="momentum", max_length=40)
    window: int = Field(default=5, ge=2, le=60)
    fee_bps: float = Field(default=10, ge=0, le=500)
    slippage_bps: float = Field(default=5, ge=0, le=500)


class MarketWalkForwardRequest(MarketBacktestRequest):
    train_size: int = Field(default=30, ge=5, le=160)
    test_size: int = Field(default=5, ge=2, le=60)
    step_size: int = Field(default=5, ge=2, le=60)
    max_folds: int = Field(default=8, ge=1, le=20)


class MarketStrategyCompareRequest(MarketBacktestRequest):
    strategies: list[str] = Field(default_factory=lambda: ["momentum", "mean_reversion"], min_length=2, max_length=4)


class MarketSensitivityRequest(MarketBacktestRequest):
    pass


SUPPORTED_MARKET_STRATEGIES = {"momentum", "mean_reversion"}


def normalize_market_strategy(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in SUPPORTED_MARKET_STRATEGIES else ""


def market_backtest_input_error(symbol: Any, strategy: Any) -> str:
    if not _app_call("normalize_market_symbol", symbol):
        return "无法识别股票代码，请填写 6 位 A 股/ETF 代码或 sh/sz 前缀。"
    if not _app_call("normalize_market_strategy", strategy):
        return "strategy 只能是 momentum 或 mean_reversion。"
    return ""


def market_history_series(symbol: str) -> list[dict[str, Any]]:
    normalized = _app_call("normalize_market_symbol", symbol)
    by_timestamp: dict[str, dict[str, Any]] = {}
    for row in _app_call("list_market_history", limit=200):
        checked_at = row.get("checked_at")
        quote = next((item for item in row.get("quotes", []) if _app_call("normalize_market_symbol", item.get("symbol", "")) == normalized), None)
        quality = _app_call("market_quote_quality", quote or {})
        if quote and quality.get("valid") and quality.get("price") is not None:
            by_timestamp[_app_call("market_timestamp_key", checked_at)] = {"checked_at": checked_at, "price": float(quality["price"]), "volume": quality.get("volume")}
    return sorted(
        by_timestamp.values(),
        key=lambda item: _sub2api_timestamp(item["checked_at"]) or datetime.min.replace(tzinfo=timezone.utc),
    )


def market_backtest_samples(symbol: str, snapshot: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return valid samples and an explicit rejection list for backtests."""
    normalized = _app_call("normalize_market_symbol", symbol)
    valid_by_timestamp: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    entries = [(row, False) for row in _app_call("list_market_history", limit=200)]
    if isinstance(snapshot, dict) and snapshot.get("checked_at"):
        # The current JSON snapshot may not have been persisted yet when a
        # caller hits the backtest endpoint directly.  Include it explicitly;
        # if its timestamp already exists in history, the current payload wins.
        entries.append((snapshot, True))
    for row, is_current in entries:
        checked_at = str(row.get("checked_at") or row.get("created_at") or "")
        if not checked_at:
            rejected.append({"checked_at": "", "reason": "缺少数据时间"})
            continue
        quote = next((item for item in row.get("quotes", []) if _app_call("normalize_market_symbol", item.get("symbol", "")) == normalized), None)
        quality = _app_call("market_quote_quality", quote or {})
        if not quote or not quality.get("valid"):
            rejected.append({"checked_at": checked_at, "reason": "；".join(quality.get("reasons") or ["缺少报价"])})
            continue
        timestamp_key = _app_call("market_timestamp_key", checked_at)
        if timestamp_key in valid_by_timestamp:
            if is_current:
                # A normal /api/market request persists the same snapshot in
                # SQLite first.  Do not lower the quality score for that
                # expected mirror; simply let the current payload win.
                valid_by_timestamp[timestamp_key] = {"checked_at": checked_at, "price": float(quality["price"]), "volume": quality.get("volume"), "source": str(row.get("source") or "unknown")}
                continue
            rejected.append({"checked_at": checked_at, "reason": "重复数据时间"})
            continue
        valid_by_timestamp[timestamp_key] = {"checked_at": checked_at, "price": float(quality["price"]), "volume": quality.get("volume"), "source": str(row.get("source") or "unknown")}
    valid = sorted(
        valid_by_timestamp.values(),
        key=lambda item: _sub2api_timestamp(item["checked_at"]) or datetime.min.replace(tzinfo=timezone.utc),
    )
    return valid, rejected


def market_backtest_quality(points: list[dict[str, Any]], rejected: list[dict[str, Any]], window: int) -> dict[str, Any]:
    """Summarize whether a local snapshot series is fit for a research backtest."""
    minimum_required = max(3, int(window) + 1)
    total = len(points) + len(rejected)
    coverage = 0.0
    parsed_times = [_sub2api_timestamp(item.get("checked_at")) for item in points]
    parsed_times = [item for item in parsed_times if item]
    if len(parsed_times) >= 2:
        coverage = round(max(0.0, (parsed_times[-1] - parsed_times[0]).total_seconds() / 86400), 2)
    score = round(len(points) / total, 3) if total else 0.0
    enough = len(points) >= minimum_required
    sample_interval_hours = None
    if len(parsed_times) >= 2:
        intervals = [max(0.0, (current - previous).total_seconds() / 3600) for previous, current in zip(parsed_times, parsed_times[1:])]
        if intervals:
            sample_interval_hours = round(statistics.median(intervals), 2)
    source_counts: dict[str, int] = {}
    for point in points:
        source = str(point.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
    source_stability = "unknown" if not source_counts or set(source_counts) == {"unknown"} else "stable" if len(source_counts) == 1 else "mixed"
    # Compare coverage to the requested number of *sample intervals*, not to
    # a fixed number of calendar days.  A 20-point 5-minute sample should not
    # be rejected as if it were a 20-day daily series.
    interval_days = sample_interval_hours / 24 if sample_interval_hours and sample_interval_hours > 0 else 1.0
    coverage_required_days = round(max(interval_days * max(float(window), 2.0) * 0.75, interval_days * 2.0), 2)
    coverage_ready = coverage >= coverage_required_days
    confidence = (
        "high"
        if enough and len(points) >= 20 and score >= 0.9 and coverage_ready and source_stability == "stable"
        else "medium"
        if enough and score >= 0.7 and coverage >= max(0.5, coverage_required_days * 0.5)
        else "low"
    )
    if sample_interval_hours is None:
        interval_label = "无法估计"
    elif sample_interval_hours >= 24:
        interval_label = f"约 {round(sample_interval_hours / 24, 2)} 天/点"
    else:
        interval_label = f"约 {sample_interval_hours} 小时/点"
    return {
        "valid_count": len(points),
        "rejected_count": len(rejected),
        "minimum_required": minimum_required,
        "usable_window_count": max(0, len(points) - int(window)),
        "coverage_days": coverage,
        "coverage_required_days": coverage_required_days,
        "coverage_required_intervals": round(coverage_required_days / interval_days, 2) if interval_days > 0 else None,
        "coverage_ready": coverage_ready,
        "sample_interval_hours": sample_interval_hours,
        "sample_interval_label": interval_label,
        "quality_score": score,
        "confidence": confidence,
        "status": "ready" if enough else "insufficient",
        "source_counts": source_counts,
        "source_stability": source_stability,
        "rejected": rejected[:20],
        "policy": "仅使用现价、昨收和涨跌幅一致的唯一时间点；异常样本不参与回测；多个行情源混用时降低来源稳定性提示。",
    }


def _simulate_market_backtest(
    points: list[dict[str, Any]],
    strategy: str,
    window: int,
    fee_bps: float,
    slippage_bps: float,
    evaluation_start: int = 0,
) -> dict[str, Any]:
    """Run one deterministic simulation without reading or writing application state."""
    prices = [float(item["price"]) for item in points]
    if not prices:
        return {
            "capital": 1.0,
            "trades": [],
            "closed_returns": [],
            "net_return_pct": 0.0,
            "benchmark_return_pct": None,
            "active_return_pct": None,
            "max_drawdown_pct": 0.0,
            "realized_volatility_pct": None,
            "sample_sharpe_ratio": None,
            "sample_sortino_ratio": None,
            "exposure_pct": 0.0,
            "trade_count": 0,
            "win_rate": None,
            "profit_factor": None,
            "average_trade_return_pct": None,
            "equity_curve": [],
        }
    evaluation_start = max(0, min(int(evaluation_start or 0), len(points) - 1))
    fee_rate = max(0.0, float(fee_bps)) / 10_000
    slippage_rate = max(0.0, float(slippage_bps)) / 10_000
    capital = 1.0
    holdings = 0.0
    entry_cash = None
    trades: list[dict[str, Any]] = []
    closed_returns: list[float] = []
    equity_curve: list[float] = []
    held_samples = 0

    for index, current in enumerate(prices):
        if index >= max(window, evaluation_start):
            average = statistics.mean(prices[index - window:index])
            should_hold = current < average * 0.98 if strategy == "mean_reversion" else current > average * 1.01
            if should_hold and holdings == 0:
                execution_price = current * (1 + slippage_rate)
                entry_cash = capital
                holdings = capital / (execution_price * (1 + fee_rate))
                capital = 0.0
                trades.append({"action": "buy_simulated", "at": points[index]["checked_at"], "price": current, "execution_price": round(execution_price, 6)})
            elif not should_hold and holdings:
                execution_price = current * (1 - slippage_rate)
                exit_cash = holdings * execution_price * (1 - fee_rate)
                trade_return = ((exit_cash / entry_cash) - 1) * 100 if entry_cash else None
                capital = exit_cash
                holdings = 0.0
                if trade_return is not None:
                    closed_returns.append(trade_return)
                trades.append({"action": "sell_simulated", "at": points[index]["checked_at"], "price": current, "execution_price": round(execution_price, 6), "trade_return_pct": round(trade_return, 4) if trade_return is not None else None})
                entry_cash = None
        if index >= evaluation_start:
            if holdings:
                held_samples += 1
            equity_curve.append(capital + holdings * current)

    if holdings:
        current = prices[-1]
        execution_price = current * (1 - slippage_rate)
        exit_cash = holdings * execution_price * (1 - fee_rate)
        trade_return = ((exit_cash / entry_cash) - 1) * 100 if entry_cash else None
        capital = exit_cash
        if trade_return is not None:
            closed_returns.append(trade_return)
        trades.append({"action": "sell_simulated_end", "at": points[-1]["checked_at"], "price": current, "execution_price": round(execution_price, 6), "trade_return_pct": round(trade_return, 4) if trade_return is not None else None})
        equity_curve[-1] = capital

    period_returns = [(current - previous) / previous for previous, current in zip(equity_curve, equity_curve[1:]) if previous > 0]
    sample_sharpe_ratio = None
    sample_sortino_ratio = None
    if len(period_returns) >= 2:
        mean_return = statistics.mean(period_returns)
        return_deviation = statistics.pstdev(period_returns)
        if return_deviation > 0:
            # This is deliberately labelled "sample" rather than annualized:
            # snapshots may be 5 minutes, daily, or irregular and should not
            # be turned into an invented annual frequency.
            sample_sharpe_ratio = round(mean_return / return_deviation * math.sqrt(len(period_returns)), 4)
        downside_returns = [value for value in period_returns if value < 0]
        downside_deviation = statistics.pstdev(downside_returns) if len(downside_returns) >= 2 else None
        if downside_deviation and downside_deviation > 0:
            sample_sortino_ratio = round(mean_return / downside_deviation * math.sqrt(len(period_returns)), 4)
    peak = 0.0
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak:
            max_drawdown = min(max_drawdown, value / peak - 1)
    strategy_return = (capital - 1) * 100
    benchmark_start_price = prices[evaluation_start]
    benchmark_return = ((prices[-1] / benchmark_start_price) - 1) * 100 if benchmark_start_price else None
    gains = sum(value for value in closed_returns if value > 0)
    losses = sum(value for value in closed_returns if value < 0)
    return {
        "capital": capital,
        "trades": trades,
        "closed_returns": closed_returns,
        "net_return_pct": round(strategy_return, 4),
        "benchmark_return_pct": round(benchmark_return, 4) if benchmark_return is not None else None,
        "active_return_pct": round(strategy_return - benchmark_return, 4) if benchmark_return is not None else None,
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "realized_volatility_pct": round(statistics.pstdev(period_returns) * 100, 4) if len(period_returns) >= 2 else None,
        "sample_sharpe_ratio": sample_sharpe_ratio,
        "sample_sortino_ratio": sample_sortino_ratio,
        "exposure_pct": round(held_samples / max(1, len(equity_curve)) * 100, 4) if equity_curve else 0.0,
        "trade_count": len(closed_returns),
        "win_rate": round(sum(1 for value in closed_returns if value > 0) / len(closed_returns), 4) if closed_returns else None,
        "profit_factor": round(gains / abs(losses), 4) if losses < 0 else None,
        "average_trade_return_pct": round(statistics.mean(closed_returns), 4) if closed_returns else None,
        "equity_curve": equity_curve,
    }


def _walk_forward_candidate_windows(window: int, train_size: int) -> list[int]:
    """Return a small, explicit parameter grid that can fit inside a train fold."""
    requested = max(2, min(60, int(window)))
    candidates = sorted({max(2, requested - 2), requested, min(60, requested + 2)})
    return [candidate for candidate in candidates if candidate + 1 <= int(train_size)]


def _select_walk_forward_window(
    train_points: list[dict[str, Any]],
    strategy: str,
    window: int,
    fee_bps: float,
    slippage_bps: float,
) -> dict[str, Any]:
    """Select a window only from the training segment of one walk-forward fold."""
    candidates = _app_call("_walk_forward_candidate_windows", window, len(train_points))
    evaluations = []
    for candidate in candidates:
        result = _app_call("_simulate_market_backtest", train_points, strategy, candidate, fee_bps, slippage_bps)
        net_return = float(result.get("net_return_pct") or 0.0)
        drawdown = float(result.get("max_drawdown_pct") or 0.0)
        # A modest drawdown penalty keeps an in-sample outlier from always
        # winning while keeping the policy deterministic and explainable.
        score = net_return + drawdown * 0.25
        evaluations.append({
            "window": candidate,
            "net_return_pct": result.get("net_return_pct"),
            "max_drawdown_pct": result.get("max_drawdown_pct"),
            "trade_count": result.get("trade_count", 0),
            "score": round(score, 4),
        })
    if not evaluations:
        return {"selected_window": None, "candidates": []}
    selected = max(evaluations, key=lambda item: (item["score"], item["net_return_pct"] or 0.0, -abs(item["max_drawdown_pct"] or 0.0)))
    return {"selected_window": selected["window"], "candidates": evaluations}


def _compound_percent_returns(values: list[float]) -> float:
    capital = 1.0
    for value in values:
        capital *= 1.0 + float(value) / 100.0
    return round((capital - 1.0) * 100.0, 4)


def _fold_max_drawdown(values: list[float]) -> float:
    capital = 1.0
    peak = capital
    max_drawdown = 0.0
    for value in values:
        capital *= 1.0 + float(value) / 100.0
        peak = max(peak, capital)
        if peak:
            max_drawdown = min(max_drawdown, capital / peak - 1.0)
    return round(max_drawdown * 100.0, 4)


def _fold_sample_ratios(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    returns = [float(value) / 100.0 for value in values]
    mean_return = statistics.mean(returns)
    deviation = statistics.pstdev(returns)
    sharpe = round(mean_return / deviation * math.sqrt(len(returns)), 4) if deviation > 0 else None
    downside = [value for value in returns if value < 0]
    downside_deviation = statistics.pstdev(downside) if len(downside) >= 2 else None
    sortino = round(mean_return / downside_deviation * math.sqrt(len(returns)), 4) if downside_deviation and downside_deviation > 0 else None
    return sharpe, sortino


def market_walk_forward(
    symbol: str,
    strategy: str,
    window: int,
    train_size: int,
    test_size: int,
    step_size: int,
    max_folds: int,
    fee_bps: float = 10,
    slippage_bps: float = 5,
    snapshot: dict[str, Any] | None = None,
    external_points: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run deterministic non-overlapping walk-forward out-of-sample tests."""
    normalized_symbol = _app_call("normalize_market_symbol", symbol)
    normalized_strategy = _app_call("normalize_market_strategy", strategy)
    input_error = _app_call("market_backtest_input_error", symbol, strategy)
    points, rejected_samples = (
        (list(external_points), [])
        if external_points is not None
        else _app_call("market_backtest_samples", symbol, snapshot=snapshot)
    )
    quality = _app_call("market_backtest_quality", points, rejected_samples, window)
    base = {
        "symbol": normalized_symbol,
        "strategy": normalized_strategy or str(strategy),
        "window": window,
        "train_size": train_size,
        "test_size": test_size,
        "step_size": step_size,
        "max_folds": max_folds,
        "sample_count": len(points),
        "fold_count": 0,
        "folds": [],
        "out_of_sample_return_pct": None,
        "out_of_sample_benchmark_return_pct": None,
        "out_of_sample_active_return_pct": None,
        "out_of_sample_max_drawdown_pct": None,
        "out_of_sample_mean_return_pct": None,
        "out_of_sample_positive_fold_rate": None,
        "out_of_sample_sharpe_ratio": None,
        "out_of_sample_sortino_ratio": None,
        "cost_assumptions": {"fee_bps": fee_bps, "slippage_bps": slippage_bps},
        "sample_quality": quality,
        "policy": "每折只用训练段选择回看窗口，随后在不重叠测试段计算样本外结果；不连接券商、不自动下单。",
    }
    if input_error:
        return {**base, "status": "invalid", "message": input_error}
    if step_size < test_size:
        return {**base, "status": "invalid", "message": "为避免样本外测试折重叠，step_size 不能小于 test_size。"}
    if train_size <= window or len(points) < train_size + test_size:
        return {**base, "status": "insufficient", "message": f"至少需要 {train_size + test_size} 个有效历史点，且训练段要长于回看窗口；当前只有 {len(points)} 个。"}
    if not _app_call("_walk_forward_candidate_windows", window, train_size):
        return {**base, "status": "insufficient", "message": "训练段不足以容纳所选回看窗口及其候选参数。"}

    folds = []
    start = 0
    while start + train_size + test_size <= len(points) and len(folds) < max_folds:
        train_end = start + train_size
        test_end = train_end + test_size
        train_points = points[start:train_end]
        test_points = points[train_end:test_end]
        selection = _app_call("_select_walk_forward_window", train_points, strategy, window, fee_bps, slippage_bps)
        selected_window = selection.get("selected_window")
        if selected_window is None:
            break
        block = train_points + test_points
        oos = _app_call("_simulate_market_backtest", block, strategy, selected_window, fee_bps, slippage_bps, evaluation_start=len(train_points))
        gross = _app_call("_simulate_market_backtest", block, strategy, selected_window, 0, 0, evaluation_start=len(train_points))
        folds.append({
            "fold": len(folds) + 1,
            "train_count": len(train_points),
            "test_count": len(test_points),
            "train_from": train_points[0]["checked_at"],
            "train_to": train_points[-1]["checked_at"],
            "test_from": test_points[0]["checked_at"],
            "test_to": test_points[-1]["checked_at"],
            "selected_window": selected_window,
            "selection": selection["candidates"],
            "oos_return_pct": oos["net_return_pct"],
            "oos_gross_return_pct": gross["net_return_pct"],
            "oos_benchmark_return_pct": oos["benchmark_return_pct"],
            "oos_active_return_pct": oos["active_return_pct"],
            "oos_max_drawdown_pct": oos["max_drawdown_pct"],
            "oos_sample_sharpe_ratio": oos["sample_sharpe_ratio"],
            "oos_sample_sortino_ratio": oos["sample_sortino_ratio"],
            "trade_count": oos["trade_count"],
            "win_rate": oos["win_rate"],
        })
        start += step_size
    if not folds:
        return {**base, "status": "insufficient", "message": "当前历史点不足以形成一个有效的样本外测试折。"}

    oos_returns = [float(item["oos_return_pct"] or 0.0) for item in folds]
    benchmark_returns = [float(item["oos_benchmark_return_pct"] or 0.0) for item in folds]
    sharpe, sortino = _app_call("_fold_sample_ratios", oos_returns)
    status = "ok" if len(folds) >= 2 else "insufficient"
    message = "样本外折数不足 2，结果仅作试运行，不能据此判断策略稳定性。" if status != "ok" else "已完成不重叠 walk-forward 样本外验证。"
    return {
        **base,
        "status": status,
        "message": message,
        "folds": folds,
        "fold_count": len(folds),
        "out_of_sample_return_pct": _app_call("_compound_percent_returns", oos_returns),
        "out_of_sample_benchmark_return_pct": _app_call("_compound_percent_returns", benchmark_returns),
        "out_of_sample_active_return_pct": round(_app_call("_compound_percent_returns", oos_returns) - _app_call("_compound_percent_returns", benchmark_returns), 4),
        "out_of_sample_max_drawdown_pct": _app_call("_fold_max_drawdown", oos_returns),
        "out_of_sample_mean_return_pct": round(statistics.mean(oos_returns), 4),
        "out_of_sample_positive_fold_rate": round(sum(1 for value in oos_returns if value > 0) / len(oos_returns), 4),
        "out_of_sample_sharpe_ratio": sharpe,
        "out_of_sample_sortino_ratio": sortino,
        "data_from": points[0]["checked_at"],
        "data_to": points[-1]["checked_at"],
        "cost_assumptions": {"fee_bps": round(fee_bps, 4), "slippage_bps": round(slippage_bps, 4), "total_round_trip_bps": round((fee_bps + slippage_bps) * 2, 4)},
        "disclaimer": "这是本地历史快照的 walk-forward 样本外模拟；每折先在训练段选择窗口，再在不重叠测试段计算结果。Sharpe/Sortino 是折间样本指标，未按年化频率外推；不构成投资建议，也不会下单。",
    }


def market_backtest(
    symbol: str,
    strategy: str,
    window: int,
    fee_bps: float = 10,
    slippage_bps: float = 5,
    snapshot: dict[str, Any] | None = None,
    external_points: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_symbol = _app_call("normalize_market_symbol", symbol)
    normalized_strategy = _app_call("normalize_market_strategy", strategy)
    input_error = _app_call("market_backtest_input_error", symbol, strategy)
    points, rejected_samples = (
        (list(external_points), [])
        if external_points is not None
        else _app_call("market_backtest_samples", symbol, snapshot=snapshot)
    )
    quality = _app_call("market_backtest_quality", points, rejected_samples, window)
    base = {
        "symbol": normalized_symbol,
        "strategy": normalized_strategy or str(strategy),
        "window": window,
        "sample_count": len(points),
        "trades": [],
        "return_pct": None,
        "net_return_pct": None,
        "benchmark_return_pct": None,
        "active_return_pct": None,
        "max_drawdown_pct": None,
        "realized_volatility_pct": None,
        "sample_sharpe_ratio": None,
        "sample_sortino_ratio": None,
        "exposure_pct": None,
        "trade_count": 0,
        "win_rate": None,
        "profit_factor": None,
        "average_trade_return_pct": None,
        "cost_assumptions": {"fee_bps": fee_bps, "slippage_bps": slippage_bps},
        "sample_quality": quality,
    }
    if input_error:
        return {**base, "status": "invalid", "message": input_error}
    if len(points) < max(3, window + 1):
        return {**base, "status": "insufficient", "message": f"至少需要 {window + 1} 个有效历史点，当前只有 {len(points)} 个。样本按快照点计算，不等同交易日。"}
    simulation = _app_call("_simulate_market_backtest", points, strategy, window, fee_bps, slippage_bps)
    gross = _app_call("_simulate_market_backtest", points, strategy, window, 0, 0) if fee_bps or slippage_bps else None
    return {
        **base,
        "status": "ok",
        "return_pct": simulation["net_return_pct"],
        "net_return_pct": simulation["net_return_pct"],
        "gross_return_pct": gross["net_return_pct"] if gross else simulation["net_return_pct"],
        "benchmark_return_pct": simulation["benchmark_return_pct"],
        "active_return_pct": simulation["active_return_pct"],
        "max_drawdown_pct": simulation["max_drawdown_pct"],
        "realized_volatility_pct": simulation["realized_volatility_pct"],
        "sample_sharpe_ratio": simulation["sample_sharpe_ratio"],
        "sample_sortino_ratio": simulation["sample_sortino_ratio"],
        "exposure_pct": simulation["exposure_pct"],
        "trade_count": simulation["trade_count"],
        "win_rate": simulation["win_rate"],
        "profit_factor": simulation["profit_factor"],
        "average_trade_return_pct": simulation["average_trade_return_pct"],
        "trades": simulation["trades"],
        "data_from": points[0]["checked_at"],
        "data_to": points[-1]["checked_at"],
        "cost_assumptions": {"fee_bps": round(fee_bps, 4), "slippage_bps": round(slippage_bps, 4), "total_round_trip_bps": round((fee_bps + slippage_bps) * 2, 4)},
        "disclaimer": "这是本地历史快照的模拟研究；已计入假设手续费和滑点，并与买入持有基准比较。Sharpe/Sortino 是样本期指标，未按年化频率外推；不构成投资建议，也不会下单。",
    }


@app.post("/api/market/research")
def create_market_research(request: MarketResearchRequest) -> dict[str, Any]:
    symbol = _app_call("normalize_market_symbol", request.symbol) if request.symbol else ""
    snapshot = _app_call("load_market_snapshot", )
    history = _app_call("list_market_history", limit=30)
    analysis = _app_call("analyze_market_snapshot", snapshot, history)
    freshness = analysis.get("freshness") if isinstance(analysis.get("freshness"), dict) else {}
    data_quality = {
        "source": str(snapshot.get("source") or "本地快照"),
        "checked_at": str(snapshot.get("checked_at") or ""),
        "freshness_status": str(freshness.get("status") or "missing"),
        "freshness_label": str(freshness.get("label") or "未知"),
        "history_count": len(history),
        "watchlist_count": len(snapshot.get("watchlist") or []),
        "warnings": [clip(str(item), 180) for item in (analysis.get("warnings") or [])[:5]],
        "research_confidence": analysis.get("research_confidence") or {},
    }
    artifact = _app_call("register_artifact_safely", project_id="market", name=f"行情研究上下文 · {symbol or '全市场'}", path=str(MARKET_SNAPSHOT_FILE), kind="market_research_context", metadata={"symbol": symbol, "checked_at": snapshot.get("checked_at"), "question": request.question, "data_quality": data_quality})
    quality_note = f"数据质量：{data_quality['freshness_label']} · 来源 {data_quality['source']} · 历史快照 {data_quality['history_count']} 次"
    item = _app_call("create_work_item_record", title=f"行情研究：{clip(request.question, 120)}", description=f"标的：{symbol or '未指定'}\n问题：{request.question}\n事件日期：{request.event_date or '未指定'}\n数据时间：{snapshot.get('checked_at') or '未知'}\n{quality_note}\n要求：请结合历史快照与公开来源完成事件驱动研究，不自动交易。", kind="market_event_research", source_project="market", target_project="knowledge", metadata={"symbol": symbol, "question": request.question, "event_date": request.event_date, "artifact_id": artifact.get("id") if artifact else None, "checked_at": snapshot.get("checked_at"), "data_quality": data_quality})
    relation = _app_call("create_relation_record", from_type="artifact", from_id=str(artifact.get("id")), to_type="work_item", to_id=str(item.get("id")), relation_type="market_context_to_research", metadata={"symbol": symbol, "data_quality": data_quality}) if artifact else None
    return {"ok": True, "item": item, "artifact": artifact, "relation": relation}


@app.get("/api/market/reports")
def get_market_reports(limit: int = 30) -> dict[str, Any]:
    return {"artifacts": [item for item in _app_call("list_artifacts", "market") if item.get("kind") in {"market_report", "market_strategy", "market_backtest", "market_walk_forward"}][:max(1, min(limit, 100))]}


@app.post("/api/market/reports")
def create_market_report() -> dict[str, Any]:
    snapshot = _app_call("load_market_snapshot", )
    history = _app_call("list_market_history", limit=30)
    analysis = _app_call("analyze_market_snapshot", snapshot, history)
    if not analysis.get("valid_quote_count"):
        raise HTTPException(400, "当前快照没有有效报价，暂不生成量化报告")
    confidence = analysis.get("research_confidence") or {}
    lines = [f"# 量化行情日报 · {datetime.now().astimezone().strftime('%Y-%m-%d')}", "", f"> 数据时间：{snapshot.get('checked_at') or '未知'} · 来源：{snapshot.get('source') or '本地快照'}", "", "## 总览", "", analysis.get("summary") or "暂无行情摘要。", "", f"研究可信度：{confidence.get('label') or 'low'} · 分数 {confidence.get('score', 0)} · 有效报价 {confidence.get('valid_quote_count', 0)} / 拒绝 {confidence.get('rejected_quote_count', 0)} · 覆盖 {confidence.get('coverage_days', 0)} 天", "", "## 可解释观察", ""]
    for signal in analysis.get("signals", []):
        lines.append(f"- **{signal.get('name') or signal.get('symbol')}**：{signal.get('observation') or '暂无观察'}；因子：{'、'.join(signal.get('factors') or []) or '样本不足'}")
    lines.extend(["", "## 风险边界", "", "本报告只用于本地研究，不构成投资建议，不执行自动交易。"])
    path = OUTPUTS_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-量化日报.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifact = _app_call("register_artifact_safely", project_id="market", name=path.name, path=str(path), kind="market_report", metadata={"report_type": "daily", "checked_at": snapshot.get("checked_at"), "history_count": len(history)})
    return {"ok": True, "artifact": artifact, "path": str(path), "content": "\n".join(lines)}


@app.post("/api/market/strategies")
def create_market_strategy(request: MarketStrategyRequest) -> dict[str, Any]:
    version = 1 + sum(1 for item in _app_call("list_artifacts", "market") if item.get("kind") == "market_strategy" and item.get("metadata", {}).get("name") == request.name.strip())
    artifact = _app_call("register_artifact_safely", project_id="market", name=f"策略-{_app_call('safe_filename', request.name)}-v{version}.json", path=str(MARKET_SNAPSHOT_FILE), kind="market_strategy", metadata={"name": request.name.strip(), "version": version, "rules": request.rules, "note": request.note, "automated_trading": False})
    return {"ok": True, "artifact": artifact, "version": version, "policy": "策略只用于研究和回测，不连接券商、不自动下单。"}


@app.get("/api/market/strategies")
def get_market_strategies(limit: int = 50) -> dict[str, Any]:
    strategies = [item for item in _app_call("list_artifacts", "market") if item.get("kind") == "market_strategy"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in strategies[: max(1, min(limit, 100))]:
        name = str((item.get("metadata") or {}).get("name") or item.get("name") or "未命名策略")
        grouped.setdefault(name, []).append(item)
    return {"strategies": strategies[: max(1, min(limit, 100))], "versions": grouped, "count": len(strategies)}


@app.post("/api/market/backtest")
def run_market_backtest(request: MarketBacktestRequest) -> dict[str, Any]:
    strategy = _app_call("normalize_market_strategy", request.strategy)
    symbol = _app_call("normalize_market_symbol", request.symbol)
    if not strategy:
        raise HTTPException(400, "strategy 只能是 momentum 或 mean_reversion")
    if not symbol:
        raise HTTPException(400, "无法识别股票代码，请填写 6 位 A 股/ETF 代码或 sh/sz 前缀")
    snapshot = _app_call("load_market_snapshot", )
    _app_call("record_market_snapshot", snapshot)
    result = _app_call("market_backtest", symbol, strategy, request.window, request.fee_bps, request.slippage_bps, snapshot=snapshot)
    artifact = _app_call("register_artifact_safely", project_id="market", name=f"回测-{symbol}-{strategy}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json", path=str(MARKET_SNAPSHOT_FILE), kind="market_backtest", metadata=result)
    return {"ok": True, "backtest": result, "artifact": artifact}


@app.post("/api/market/backtest/walk-forward")
def run_market_walk_forward(request: MarketWalkForwardRequest) -> dict[str, Any]:
    strategy = _app_call("normalize_market_strategy", request.strategy)
    symbol = _app_call("normalize_market_symbol", request.symbol)
    if not strategy:
        raise HTTPException(400, "strategy 只能是 momentum 或 mean_reversion")
    if not symbol:
        raise HTTPException(400, "无法识别股票代码，请填写 6 位 A 股/ETF 代码或 sh/sz 前缀")
    if request.step_size < request.test_size:
        raise HTTPException(400, "为避免样本外测试折重叠，step_size 不能小于 test_size")
    snapshot = _app_call("load_market_snapshot", )
    _app_call("record_market_snapshot", snapshot)
    result = _app_call("market_walk_forward", 
        symbol,
        strategy,
        request.window,
        request.train_size,
        request.test_size,
        request.step_size,
        request.max_folds,
        request.fee_bps,
        request.slippage_bps,
        snapshot=snapshot,
    )
    artifact = _app_call("register_artifact_safely", 
        project_id="market",
        name=f"样本外验证-{symbol}-{strategy}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json",
        path=str(MARKET_SNAPSHOT_FILE),
        kind="market_walk_forward",
        metadata=result,
    )
    return {"ok": True, "walk_forward": result, "artifact": artifact}


@app.post("/api/market/strategies/compare")
def compare_market_strategies(request: MarketStrategyCompareRequest) -> dict[str, Any]:
    strategies = list(dict.fromkeys(request.strategies))
    symbol = _app_call("normalize_market_symbol", request.symbol)
    if len(strategies) < 2:
        raise HTTPException(400, "strategies 至少需要两个不同策略")
    invalid = [item for item in strategies if not _app_call("normalize_market_strategy", item)]
    if invalid:
        raise HTTPException(400, "strategies 只能包含 momentum 或 mean_reversion")
    if not symbol:
        raise HTTPException(400, "无法识别股票代码，请填写 6 位 A 股/ETF 代码或 sh/sz 前缀")
    snapshot = _app_call("load_market_snapshot", )
    _app_call("record_market_snapshot", snapshot)
    comparison = [
        _app_call("market_backtest", symbol, _app_call("normalize_market_strategy", strategy), request.window, request.fee_bps, request.slippage_bps, snapshot=snapshot)
        for strategy in strategies
    ]
    artifact = _app_call("register_artifact_safely", 
        project_id="market",
        name=f"策略对比-{_app_call('normalize_market_symbol', request.symbol)}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json",
        path=str(MARKET_SNAPSHOT_FILE),
        kind="market_strategy_comparison",
        metadata={"symbol": symbol, "strategies": strategies, "window": request.window, "fee_bps": request.fee_bps, "slippage_bps": request.slippage_bps, "comparison": comparison},
    )
    return {"ok": True, "comparison": comparison, "artifact": artifact, "policy": "只比较本地历史快照，不构成投资建议，也不会自动下单。"}


@app.post("/api/market/backtest/sensitivity")
def market_backtest_sensitivity(request: MarketSensitivityRequest) -> dict[str, Any]:
    strategy = _app_call("normalize_market_strategy", request.strategy)
    symbol = _app_call("normalize_market_symbol", request.symbol)
    if not strategy:
        raise HTTPException(400, "strategy 只能是 momentum 或 mean_reversion")
    if not symbol:
        raise HTTPException(400, "无法识别股票代码，请填写 6 位 A 股/ETF 代码或 sh/sz 前缀")
    scenarios = [("无成本", 0.0, 0.0), ("当前假设", request.fee_bps, request.slippage_bps), ("成本翻倍", request.fee_bps * 2, request.slippage_bps * 2)]
    snapshot = _app_call("load_market_snapshot", )
    _app_call("record_market_snapshot", snapshot)
    results = []
    for label, fee_bps, slippage_bps in scenarios:
        result = _app_call("market_backtest", symbol, strategy, request.window, fee_bps, slippage_bps, snapshot=snapshot)
        results.append({"label": label, "fee_bps": fee_bps, "slippage_bps": slippage_bps, "return_pct": result.get("net_return_pct"), "status": result.get("status"), "sample_count": result.get("sample_count"), "sample_quality": result.get("sample_quality", {})})
    return {"ok": True, "symbol": symbol, "strategy": strategy, "scenarios": results, "policy": "敏感性分析只说明成本假设对历史模拟的影响，不代表未来收益。"}



class MarketValuationRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    fundamentals: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=2_000)


class MarketResearchConclusionRequest(BaseModel):
    conclusion: str = Field(min_length=1, max_length=12_000)
    confirmed: bool = False


@app.post("/api/market/valuation")
def create_market_valuation(request: MarketValuationRequest) -> dict[str, Any]:
    symbol = _app_call("normalize_market_symbol", request.symbol)
    if not symbol:
        raise HTTPException(400, "无法识别股票代码")
    numeric = {}
    for key in ("pe", "pb", "ps", "roe", "dividend_yield", "revenue_growth"):
        value = request.fundamentals.get(key)
        try:
            if value is not None and value != "":
                numeric[key] = round(float(value), 6)
        except (TypeError, ValueError):
            continue
    available = bool(numeric)
    factors = [{"name": key, "value": value, "status": "available"} for key, value in numeric.items()]
    result = {"symbol": symbol, "available": available, "factors": factors, "missing": [key for key in ("pe", "pb", "roe") if key not in numeric], "note": request.note.strip(), "data_as_of": _app_call("load_market_snapshot", ).get("checked_at", ""), "policy": "估值因子只接受明确来源的输入；缺数据时不推断估值，也不构成投资建议。"}
    artifact = _app_call("register_artifact_safely", project_id="market", name=f"估值因子 · {symbol}", kind="market_valuation", path=str(MARKET_SNAPSHOT_FILE), metadata=result)
    return {"ok": True, "valuation": result, "artifact": artifact}


@app.post("/api/market/research/{item_id}/conclude")
def conclude_market_research(item_id: int, request: MarketResearchConclusionRequest) -> dict[str, Any]:
    if not request.confirmed:
        raise HTTPException(409, "沉淀行情研究结论前需要明确确认")
    item = _app_call("get_work_item_record", item_id)
    if not item or item.get("source_project") != "market" or item.get("kind") != "market_event_research":
        raise HTTPException(404, "行情研究任务不存在")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    existing_id = metadata.get("conclusion_artifact_id")
    if str(existing_id).isdigit():
        existing_artifact = _app_call("get_artifact_record", int(existing_id))
        if existing_artifact:
            return {"ok": True, "already_concluded": True, "item": item, "note": {"artifact": existing_artifact}, "message": "这条行情研究已经沉淀过结论。"}
    artifact_id = metadata.get("artifact_id")
    source = _app_call("get_artifact_record", int(artifact_id)) if str(artifact_id).isdigit() else None
    title = clip(item.get("title") or "行情研究结论", 120)
    note = _app_call("write_knowledge_note", f"行情研究结论｜{title}", f"> 来源工作项：行情 #{item_id}\n> 数据时间：{metadata.get('checked_at') or (source or {}).get('metadata', {}).get('checked_at') or '未记录'}\n\n## 结论\n\n{request.conclusion.strip()}\n\n## 风险边界\n\n本结论基于本地快照和人工补充，只作研究记录，不构成投资建议。", metadata={"source_market_work_item_id": item_id, "source_artifact_id": source.get("id") if source else None, "confirmed": request.confirmed}, artifact_kind="market_research_conclusion")
    relation = _app_call("create_relation_record", from_type="work_item", from_id=str(item_id), to_type="artifact", to_id=str(note.get("artifact", {}).get("id", "")), relation_type="research_to_knowledge", metadata={"confirmed": request.confirmed}) if note.get("artifact") else None
    conclusion_artifact_id = note.get("artifact", {}).get("id") if note.get("artifact") else None
    updated_metadata = {**metadata, "conclusion_artifact_id": conclusion_artifact_id, "concluded_at": now_iso(), "conclusion": request.conclusion.strip()}
    updated = _app_call("update_work_item_record", item_id, {"status": "done", "completed_at": now_iso(), "metadata_json": json.dumps(updated_metadata, ensure_ascii=False), "result_json": json.dumps({"knowledge_artifact_id": conclusion_artifact_id, "conclusion": request.conclusion.strip()}, ensure_ascii=False), "last_error": ""})
    return {"ok": True, "item": updated, "note": note, "relation": relation, "message": "研究结论已沉淀到工作区知识库。"}


# ═══════════════ 量化 2.0：研究卡 / ETF 轮动 / 可转债 / 估值百分位 / 组合体检 / AI 一眼看 ═══════════════

_TENCENT_KLINE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def _tencent_kline(symbol: str, days: int = 40) -> list[dict[str, Any]]:
    """腾讯日 K 线（前复权）：symbol 如 sh510300 / sz159915；返回 [{date, close}] 升序。

    进程内缓存 10 分钟：量化决策卡片批量拉自选历史时不会每次请求都打上游。
    """
    cache_key = f"{symbol}:{days}"
    cached = _TENCENT_KLINE_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < 600:
        return cached[1]
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days + 10},qfq"
        async with httpx.AsyncClient(timeout=10, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        node = ((payload or {}).get("data") or {}).get(symbol) or {}
        klines = node.get("qfqday") or node.get("day") or []
        rows = []
        for parts in klines:
            if len(parts) >= 3:
                close = _app_call("parse_market_number", parts[2])
                if close and close > 0:
                    rows.append({"date": str(parts[0]), "close": close})
        result = rows[-days:]
        _TENCENT_KLINE_CACHE[cache_key] = (time.monotonic(), result)
        return result
    except Exception:
        return []


async def market_etf_rotation() -> dict[str, Any]:
    """宽基 ETF 动量轮动：候选池 20 日动量排序 + 绝对动量过滤建议。"""
    pool = [
        {"symbol": "sh510300", "name": "沪深300ETF"},
        {"symbol": "sh510500", "name": "中证500ETF"},
        {"symbol": "sz159915", "name": "创业板ETF"},
        {"symbol": "sh588000", "name": "科创50ETF"},
    ]
    # 四个标的的行情互不依赖，原来是一个一个 await 下来的：实测这个接口 1484ms，
    # 是整个市场页最慢的一项，而里面除了四次串行的上游往返几乎没有别的开销。
    # 并发之后耗时取决于最慢的那一次，而不是四次之和。
    klines_list = await asyncio.gather(
        *(_app_call("_tencent_kline", item["symbol"], 30) for item in pool),
        return_exceptions=True,
    )
    results = []
    for item, klines in zip(pool, klines_list):
        if isinstance(klines, BaseException) or len(klines) < 2:
            note = "行情不足" if not isinstance(klines, BaseException) else f"行情读取失败：{clip(str(klines), 80)}"
            results.append({**item, "momentum_20d": None, "latest": None, "ok": False, "note": note})
            continue
        latest = klines[-1]["close"]
        base = klines[0]["close"]
        momentum = round((latest - base) / base * 100, 2)
        results.append({**item, "momentum_20d": momentum, "latest": latest, "ok": True, "note": ""})
    ranked = [r for r in results if r.get("ok") and r.get("momentum_20d") is not None]
    ranked.sort(key=lambda r: r["momentum_20d"], reverse=True)
    top = ranked[0] if ranked else None
    suggestion = ""
    if top:
        if top["momentum_20d"] <= 0:
            suggestion = f"{top['name']} 动量 {top['momentum_20d']}% 仍为负，按绝对动量过滤建议空仓观望（或转债券/货币）。"
        else:
            suggestion = f"建议关注 {top['name']}（20 日动量 {top['momentum_20d']}% 最强）；跌破其 20 日均线或动量转负再换仓。"
    return {
        "pool": results,
        "suggestion": suggestion,
        "note": "动量 = 近 20 个交易日涨幅；只作研究参考，不构成投资建议。",
    }


async def _tencent_convertible_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """腾讯行情批量查询可转债现价/涨跌幅/溢价（GBK 编码需转码）。

    返回 {code: {"price", "change_pct", "premium"}}；失败字段为 None。
    """
    if not codes:
        return {}
    result: dict[str, dict[str, Any]] = {}
    try:
        url = "https://qt.gtimg.cn/q=" + ",".join(codes)
        async with httpx.AsyncClient(timeout=10, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = await client.get(url)
            response.raise_for_status()
            text = response.content.decode("gbk", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            body = line.split("=", 1)[1].strip().strip(";").strip('"')
            fields = body.split("~")
            if len(fields) < 40:
                continue
            code = str(fields[2] or "").strip()
            price = _app_call("parse_market_number", fields[3])
            change_pct = _app_call("parse_market_number", fields[32] if len(fields) > 32 else None)
            # 转股溢价率在部分代码段不返回，尽力而为
            premium = _app_call("parse_market_number", fields[47] if len(fields) > 47 else None)
            result[code] = {
                "price": price,
                "change_pct": change_pct,
                "premium": premium,
            }
    except Exception:
        log.debug("忽略异常（_tencent_convertible_quotes）", exc_info=True)
    return result


async def market_convertible_bonds(limit: int = 30) -> dict[str, Any]:
    """可转债低价/双低筛选：东财数据中心取存续转债静态信息（评级/到期/转股价），腾讯行情补实时价与溢价。

    溢价率字段不稳定时降级为低价优先（诚实标注）。只作研究参考。
    """
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        url = (
            "https://datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPT_BOND_CB_LIST"
            "&columns=ALL&pageSize=300&pageNumber=1&sortColumns=EXPIRE_DATE&sortTypes=1"
            f"&filter=(EXPIRE_DATE%3E%3D%27{today}%27)"
        )
        async with httpx.AsyncClient(timeout=12, trust_env=False, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        rows = (((payload or {}).get("result") or {}).get("data")) or []
        rows = rows[:200]
        # 腾讯行情批量补实时价（一次最多 60 个代码）
        premium_available = False
        bonds: list[dict[str, Any]] = []
        # 每批 60 个代码是上游的限制，但批与批之间没有依赖关系——原来一批一批
        # 串着等，200 只债就是 4 次串行往返。
        chunks = [rows[i : i + 60] for i in range(0, len(rows), 60)]
        chunk_prefixes = [
            {
                str(item.get("SECURITY_CODE") or "").strip(): ("sh" if str(item.get("SECUCODE") or "").endswith(".SH") else "sz")
                for item in chunk
            }
            for chunk in chunks
        ]
        quote_batches = await asyncio.gather(
            *(_app_call("_tencent_convertible_quotes", [prefix + code for code, prefix in mapping.items() if code])
              for mapping in chunk_prefixes),
            return_exceptions=True,
        )
        for chunk, code_prefix, quotes in zip(chunks, chunk_prefixes, quote_batches):
            if isinstance(quotes, BaseException):
                log.warning("可转债实时价批量读取失败：%s", quotes)
                continue
            for item in chunk:
                code = str(item.get("SECURITY_CODE") or "").strip()
                quote = quotes.get(code_prefix.get(code, "") + code) if code else None
                price = (quote or {}).get("price")
                if price is None or price <= 0:
                    continue
                premium = (quote or {}).get("premium")
                if premium is not None:
                    premium_available = True
                double_low = round(price + (premium or 0), 2)
                bonds.append({
                    "symbol": code,
                    "name": str(item.get("SECURITY_NAME_ABBR") or f"{code}转债"),
                    "price": price,
                    "change_pct": (quote or {}).get("change_pct"),
                    "premium": premium,
                    "double_low": double_low,
                    "rating": str(item.get("RATING") or ""),
                    "expire_date": str(item.get("EXPIRE_DATE") or "")[:10],
                    "convert_stock": str(item.get("CONVERT_STOCK_CODE") or ""),
                    "is_redeem": str(item.get("IS_REDEEM") or ""),
                })
        bonds = [b for b in bonds if b["price"] < 130]
        bonds.sort(key=lambda b: (b["premium"] is not None, b["double_low"] if b["premium"] is not None else b["price"]))
        return {
            "bonds": bonds[:limit],
            "premium_available": premium_available,
            "note": "双低 = 价格 + 转股溢价率（越低性价比越高）；溢价率不可用时按低价筛选。评级/到期来自交易所数据。只作研究参考。",
        }
    except Exception as exc:
        return {"bonds": [], "premium_available": False, "note": f"可转债数据读取失败：{clip(str(exc), 200)}"}


async def market_valuation_percentile() -> dict[str, Any]:
    """宽基指数估值百分位（蛋卷公开估值接口）：pe/pb 历史分位 + 估值区间标签。"""
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = await client.get("https://danjuanfunds.com/djapi/index_eva/dj")
            response.raise_for_status()
            payload = response.json()
        items = ((payload or {}).get("data") or {}).get("items") or []
        wanted = ["沪深300", "中证500", "创业板", "科创50", "上证50"]
        result = []
        for item in items:
            name = str(item.get("name") or "")
            if not any(k in name for k in wanted):
                continue
            pe_pct = item.get("pe_percentile")
            pb_pct = item.get("pb_percentile")
            eva = str(item.get("eva_type") or "")
            label = {"low": "低估", "normal": "适中", "high": "偏高"}.get(eva, eva or "未知")
            result.append({
                "name": name,
                "index_code": item.get("index_code"),
                "pe": round(float(item.get("pe") or 0), 2) if item.get("pe") else None,
                "pb": round(float(item.get("pb") or 0), 2) if item.get("pb") else None,
                "pe_percentile": round(float(pe_pct or 0), 3) if pe_pct else None,
                "pb_percentile": round(float(pb_pct or 0), 3) if pb_pct else None,
                "roe": round(float(item.get("roe") or 0), 3) if item.get("roe") else None,
                "eva_label": label,
            })
        return {"indices": result, "note": "百分位 = 当前 PE/PB 处于历史什么位置；>0.8 偏贵、<0.2 偏便宜（用于定投/仓位参考）。"}
    except Exception as exc:
        return {"indices": [], "note": f"指数估值读取失败：{clip(str(exc), 200)}"}


async def market_portfolio_check() -> dict[str, Any]:
    """组合体检：基于自选行情与历史快照，给出集中度/相关性/波动观察。"""
    snapshot = _app_call("load_market_snapshot", )
    watchlist = snapshot.get("watchlist") or []
    quotes = snapshot.get("quotes") or []
    if not watchlist:
        return {"ok": False, "note": "还没有自选股票，先添加自选再看组合体检。"}
    def _pos(value):
        try:
            return isinstance(value, (int, float)) and float(value) > 0
        except (TypeError, ValueError):
            return False
    up = [q for q in quotes if _pos(q.get("change_pct"))]
    down = [q for q in quotes if (lambda v: isinstance(v, (int, float)) and float(v) < 0)(q.get("change_pct"))]
    trends = {str(q.get("symbol") or "").lower(): [p.get("p") for p in (q.get("trend") or []) if isinstance(p.get("p"), (int, float))] for q in quotes}
    return {
        "ok": True,
        "count": len(watchlist),
        "up_count": len(up),
        "down_count": len(down),
        "symbols": [str(q.get("symbol") or "").upper() for q in quotes],
        "trend_points": {k: len(v) for k, v in trends.items() if v},
        "note": "相关性计算需要每个标的至少 5 个历史快照点；样本不足的标的会标注。只作研究参考。",
    }


async def market_research_card(symbol: str) -> dict[str, Any]:
    """个股研究卡（三块合一）：行情 + 估值 + 量化回测/样本外 + 价值清单。

    回测数据源优先级：① 腾讯历史日 K（股票/场内 ETF，上市以来真实价格）；
    ② 东财历史净值（场外基金，日频净值）——两者都不依赖本地快照积累，
    新加的自选当天就能算出结果；③ 本地周期快照（最后的兜底）。
    """
    symbol = str(symbol or "").strip().lower()
    if not symbol:
        return {"ok": False, "message": "请先填写股票代码。"}
    norm = _app_call("normalize_market_symbol", symbol)
    if not norm:
        return {"ok": False, "message": "代码格式无效，例如 600519 或 sh600519。"}
    raw = re.sub(r"\D", "", symbol)[-6:]
    quote = None
    data_source = ""
    external_points: list[dict[str, Any]] = []
    try:
        if _app_call("market_symbol_queryable", norm) or norm.startswith(("sh", "sz", "bj")):
            quotes = await _app_call("fetch_market_quotes", [norm])
            quote = quotes[0] if quotes else None
        # 场外基金在腾讯接口查不到：净值行情 + 历史净值都走东财。
        if not quote and len(raw) == 6:
            fund_quote = await _app_call("fetch_fund_nav", raw)
            if fund_quote:
                quote = fund_quote
    except Exception:
        quote = None
    try:
        if _app_call("market_symbol_queryable", norm):
            klines = await _app_call("_tencent_kline", norm, 120)
            if klines:
                data_source = "tencent-kline"
                external_points = [
                    {"checked_at": f"{item['date']} 15:00:00", "price": float(item["close"]), "volume": None, "source": "tencent-kline"}
                    for item in klines
                ]
        # 腾讯查不到的 6 位代码可能是场外基金（sz 前缀也被 queryable 误判）：
        # 直接退到东财历史净值，让基金当天就能研究，而不是永远"样本不足"。
        if not external_points and len(raw) == 6:
            history = await _app_call("fetch_fund_nav_history", raw, 120)
            if history:
                data_source = "fund-nav-history"
                external_points = [
                    {"checked_at": f"{item['date']} 15:00:00", "price": float(item["close"]), "volume": None, "source": "fund-nav-history"}
                    for item in history
                ]
    except Exception:
        log.warning("研究卡拉取 %s 历史数据失败", norm, exc_info=True)
    if not quote and external_points:
        quote = {
            "symbol": norm, "name": norm.upper(), "price": external_points[-1]["price"],
            "change_pct": None, "open": None, "volume": None, "source": data_source,
        }
    backtests = {}
    walkforward = {}
    for strategy in ("momentum", "mean_reversion"):
        try:
            body = await asyncio.to_thread(
                market_backtest, symbol, strategy, 20, 10, 5, None,
                external_points if external_points else None,
            )
            backtests[strategy] = {
                "status": body.get("status"),
                "net_return_pct": body.get("net_return_pct"),
                "benchmark_return_pct": body.get("benchmark_return_pct"),
                "max_drawdown_pct": body.get("max_drawdown_pct"),
                "sample_count": body.get("sample_count"),
                "message": body.get("message"),
            }
        except Exception:
            backtests[strategy] = {"status": "error", "message": "回测失败"}
    try:
        wf = await asyncio.to_thread(
            market_walk_forward, symbol, "momentum", 20, 30, 5, 5, 8, 10, 5, None,
            external_points if external_points else None,
        )
        walkforward = {
            "status": wf.get("status"),
            "out_of_sample_return_pct": wf.get("out_of_sample_return_pct"),
            "fold_count": wf.get("fold_count"),
            "positive_fold_rate": wf.get("out_of_sample_positive_fold_rate"),
            "sample_count": wf.get("sample_count"),
            "message": wf.get("message"),
        }
    except Exception:
        walkforward = {"status": "error", "message": "样本外验证失败"}
    return {
        "ok": True,
        "symbol": norm,
        "quote": quote,
        "backtests": backtests,
        "walkforward": walkforward,
        "data_source": data_source,
        "warnings": [],
        "note": "价值清单（护城河/ROE/负债率/自由现金流）需要人工核对后填写；量化部分基于历史行情/净值（"
        + ("腾讯日K" if data_source == "tencent-kline" else "东财基金净值" if data_source == "fund-nav-history" else "本地历史快照")
        + "），只作研究参考。",
    }


async def market_ai_scan(question: str = "") -> dict[str, Any]:
    """AI 一眼看：基于自选行情与观察信号生成人话总结；可带追问问题。"""
    snapshot = _app_call("load_market_snapshot", )
    watchlist = snapshot.get("watchlist") or []
    quotes = snapshot.get("quotes") or []
    if not quotes:
        return {"ok": False, "answer": "还没有行情数据。先添加自选并刷新行情，再让我看一眼。"}
    lines = []
    for q in quotes[:12]:
        change = q.get("change_pct")
        change_text = ("+" + str(change) + "%") if isinstance(change, (int, float)) and float(change) > 0 else (str(change) + "%") if isinstance(change, (int, float)) else "—"
        lines.append(f"{q.get('name') or q.get('symbol')}({str(q.get('symbol') or '').upper()}): {q.get('price')} {change_text}")
    summary_text = "；".join(lines)
    question = str(question or "").strip()
    prompt = (
        "你是量化研究助手。下面是用户自选股的最新行情快照（时间、来源以快照为准）：\n"
        f"{summary_text}\n"
    )
    if question:
        prompt += f"用户追问：{question}\n请结合快照数据回答，区分事实与判断，缺数据就明说。"
    else:
        prompt += "请用 3-5 句话总结今天值得注意的点：整体强弱、最值得关注的标的及原因、需要警惕的信号。区分事实与判断，不构成投资建议。"
    try:
        answer = await call_llm(
            [{"role": "system", "content": "你是量化研究助手，说话要有人味、结论要带数据时间和依据，缺数据就明说，不构成投资建议。"}, {"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.3,
            purpose="market_ai_scan",
        )
    except Exception as exc:
        return {"ok": False, "answer": f"AI 一眼看暂时不可用：{clip(str(exc), 300)}"}
    return {"ok": True, "answer": answer}


@app.get("/api/market/research-card")
async def get_market_research_card(symbol: str = "") -> dict[str, Any]:
    return await _app_call("market_research_card", symbol)


@app.get("/api/market/etf-rotation")
async def get_market_etf_rotation() -> dict[str, Any]:
    return await _app_call("market_etf_rotation", )


@app.get("/api/market/convertible-bonds")
async def get_market_convertible_bonds(limit: int = 30) -> dict[str, Any]:
    return await _app_call("market_convertible_bonds", limit)


@app.get("/api/market/valuation-percentile")
async def get_market_valuation_percentile() -> dict[str, Any]:
    return await _app_call("market_valuation_percentile", )


@app.get("/api/market/portfolio-check")
async def get_market_portfolio_check() -> dict[str, Any]:
    return await _app_call("market_portfolio_check", )


@app.post("/api/market/ai-scan")
async def post_market_ai_scan(request: dict[str, Any]) -> dict[str, Any]:
    return await _app_call("market_ai_scan", str((request or {}).get("question") or ""))


# ═══════════════ 网页研究：服务器渲染真实页面截图 ═══════════════

__all__ = [
    "market_ai_scan",
    "_tencent_kline",
    "market_backtest",
    "market_walk_forward",
    "_style_metrics",
    "_style_series",
    "_percentile_rank",
    "_screen_number",
    "_market_position_example",
    "_market_percentile",
    "_market_today_for_quote",
    "_distance_pct",
    "_market_rule_value",
    "_market_factor_meta",
    "post_market_ai_scan",
    "get_market_portfolio_check",
    "get_market_valuation_percentile",
    "get_market_convertible_bonds",
    "get_market_etf_rotation",
    "get_market_research_card",
    "market_research_card",
    "market_portfolio_check",
    "market_valuation_percentile",
    "market_convertible_bonds",
    "_tencent_convertible_quotes",
    "market_etf_rotation",
    "_TENCENT_KLINE_CACHE",
    "conclude_market_research",
    "create_market_valuation",
    "MarketResearchConclusionRequest",
    "MarketValuationRequest",
    "market_backtest_sensitivity",
    "compare_market_strategies",
    "run_market_walk_forward",
    "run_market_backtest",
    "get_market_strategies",
    "create_market_strategy",
    "create_market_report",
    "get_market_reports",
    "create_market_research",
    "_fold_sample_ratios",
    "_fold_max_drawdown",
    "_compound_percent_returns",
    "_select_walk_forward_window",
    "_walk_forward_candidate_windows",
    "_simulate_market_backtest",
    "market_backtest_quality",
    "market_backtest_samples",
    "market_history_series",
    "market_backtest_input_error",
    "normalize_market_strategy",
    "SUPPORTED_MARKET_STRATEGIES",
    "MarketSensitivityRequest",
    "MarketStrategyCompareRequest",
    "MarketWalkForwardRequest",
    "MarketBacktestRequest",
    "MarketStrategyRequest",
    "MarketResearchRequest",
    "save_market_watchlist",
    "save_market_snapshot",
    "_market_history_points",
    "_market_reference_zones",
    "load_market_watchlist", "MARKET_SAMPLING_INTERVALS", "MARKET_SAMPLING_SOURCE", "MARKET_SAMPLING_RULE_NAME", "market_sampling_rule", "market_history_count", "load_market_snapshot", "market_snapshot_row", "market_timestamp_key", "record_market_snapshot", "list_market_history", "market_sampling_state", "market_quote_quality", "analyze_market_snapshot", "analyze_market_factors", "evaluate_market_observations", "add_market_symbol_to_watchlist", "normalize_market_symbol", "market_symbol_key", "market_symbol_queryable", "parse_market_number", "fetch_market_quotes", "fetch_fund_nav", "fetch_fund_nav_history", "MarketWatchlistRequest", "MarketSamplingRequest", "MarketReportRequest", "market_symbol_suggest", "get_market_state", "MarketRuleRequest", "market_watchlist_rules", "save_market_watchlist_rule", "MARKET_LEVEL_ORDER", "build_market_today", "get_market_today", "MARKET_DECISION_GROUP_ORDER", "build_market_decision_center", "get_market_decision_center", "set_market_watchlist_rule", "MARKET_UNIVERSE_URLS", "MARKET_KLINE_URLS", "TENCENT_KLINE_URL", "MARKET_UNIVERSE_FS", "MARKET_UNIVERSE_FIELDS", "MARKET_SCREEN_CACHE_SECONDS", "MarketScreenRequest", "normalize_universe_rows", "apply_screen_filters", "compute_price_factors", "score_candidates", "candidate_warnings", "MARKET_SCREEN_LIMITATIONS", "fetch_market_universe", "fetch_daily_closes", "run_market_screen", "MARKET_STYLE_DATA_LABELS", "MARKET_STYLES", "market_style_catalog", "evaluate_market_style", "run_market_style_screen", "get_market_styles", "MarketStyleScreenRequest", "post_market_style_screen", "post_market_screen", "get_market_screen_selftest", "get_market_sampling", "update_market_sampling", "generate_market_report", "update_market_watchlist", "refresh_market_quotes", "evaluate_market_observations_route"]
