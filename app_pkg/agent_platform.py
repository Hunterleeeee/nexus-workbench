"""Workbench Agent 平台层：Agent 注册表/工具策略/结果契约/执行计划。

从 app.py 拆出的平台层模块（为开源准备）。包含 AGENT_* 常量（注册表/工具策略/
路由提示/剧本/实现/状态标签）与平台函数（结果契约解析/子代理能力/执行计划）。
dispatch_agent_task/call_llm_with_tools 执行器随后续批次并入。
"""

from __future__ import annotations

import asyncio
import httpx
import json
import re
import sqlite3
import time
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .core import (
    MAX_CONVERSATION_MESSAGES,
    clip,
    clip_for_llm,
    log,
    now_iso,
)
from .db import db_connection
from .instance import app
from .memories import (
    MAX_MEMORY_CONTEXT_CHARS,
    MAX_MEMORY_CONTEXT_ITEMS,
    learn_memories_from_message,
    memory_context_for_llm,
)
from .notifications import create_notification_record
from .llm import (
    _app_call,
    _contract_id_list,
    _is_markdown_table_divider,
    _llm_error_kind,
    call_llm,
    chat_completions_url,
    llm_settings,
    model_output_token_limit,
)


SUBAGENT_TOOL_MAP: dict[str, list[str]] = {
    "inbox": ["inbox_read", "inbox_triage", "inbox_capture", "work_items_read", "notify", "web_search", "web_fetch"],
    "knowledge": ["knowledge_search", "knowledge_write", "inbox_read", "work_items_read", "notify", "web_search", "web_fetch"],
    "doc-factory": ["doc_validate", "doc_template", "knowledge_search", "work_items_read", "notify", "web_search", "web_fetch"],
    "sub2api": ["sub2api_status", "work_items_read", "notify", "web_search", "web_fetch"],
    "market": ["market_read", "market_analyze", "market_style_screen", "work_items_read", "notify", "web_search", "web_fetch"],
    "server": ["server_status", "work_items_read", "notify", "web_search", "web_fetch"],
    "crawl4ai": ["crawl_fetch", "knowledge_search", "inbox_capture", "work_items_read", "notify", "web_search", "web_fetch"],
    "web-research": ["crawl_fetch", "knowledge_search", "work_items_read", "notify", "web_search", "web_fetch"],
    "aihot": ["aihot_read", "aihot_feedback", "work_items_read", "notify", "web_search", "web_fetch"],
    "idea-analysis": ["idea_read", "inbox_capture", "work_items_read", "notify", "web_search", "web_fetch"],
    "product-manager": ["product_read", "knowledge_search", "inbox_capture", "work_items_read", "notify", "web_search", "web_fetch"],
    "cid-dashboard": ["cid_read", "work_items_read", "notify", "web_search", "web_fetch"],
    "embodied": ["learning_read", "crawl_fetch", "knowledge_search", "knowledge_write", "work_items_read", "notify", "web_search", "web_fetch"],
    "ai-learning": ["learning_read", "crawl_fetch", "knowledge_search", "knowledge_write", "work_items_read", "notify", "web_search", "web_fetch"],
    # cloud_dev_build 按 cloud_dev_policy() 属于需要审批的动作，没有审批链路就
    # 不能做成模型可以直接调的工具，所以这里不登记——留着只会又变成一个查不到
    # handler、被静默丢掉的名字。
    "cloud-dev": ["cloud_dev_generate", "cloud_dev_patch", "cloud_dev_status", "cloud_dev_test", "work_items_read", "notify", "web_search", "web_fetch"],
}



AGENT_RESULT_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "summary": ("结论", "一句话结论", "总体判断", "摘要"),
    "facts": ("事实", "已知事实", "事实与证据", "已知信息"),
    "judgement": ("判断", "判断与假设", "推断", "分析"),
    "evidence": ("证据", "来源与证据", "数据依据", "依据"),
    "risks": ("风险", "不确定性", "缺口", "待验证"),
    "actions": ("动作", "可直接执行的本地动作", "已执行动作", "需要我确认的动作"),
    "next_steps": ("下一步", "后续", "验证计划", "行动计划"),
}



def _react_tools() -> dict[str, dict[str, Any]]:
    """运行时读取 app.REACT_TOOLS / SUBAGENT_EXTRA_TOOLS（工具注册表留在 app.py，
    引用各领域 handler）。"""
    import app as _app

    return _app.REACT_TOOLS


def _subagent_extra_tools() -> dict[str, dict[str, Any]]:
    import app as _app

    return getattr(_app, "SUBAGENT_EXTRA_TOOLS", {})


def agent_display_name(project_id: str) -> str:
    """延迟转发 app.agent_display_name（projects 领域仍在 app.py）。"""
    import app as _app

    return _app.agent_display_name(project_id)


def load_projects() -> list[dict[str, Any]]:
    """延迟转发 app.load_projects（projects 领域仍在 app.py）。"""
    import app as _app

    return _app.load_projects()


def public_project_link(edge: dict[str, str]) -> dict[str, str]:
    """延迟转发 app.public_project_link（projects 领域仍在 app.py）。"""
    import app as _app

    return _app.public_project_link(edge)


def project_link_summary(project_id: str) -> dict[str, list[dict[str, str]]]:
    """延迟转发 app.project_link_summary（projects 领域仍在 app.py）。"""
    import app as _app

    return _app.project_link_summary(project_id)


def agent_detail(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.agent_detail（projects 领域仍在 app.py）。"""
    import app as _app

    return _app.agent_detail(*args, **kwargs)


AGENT_REGISTRY: dict[str, dict[str, Any]] = {
    "workbench": {
        "name": "工作台总调度 Agent",
        "status": "orchestrator",
        "kind": "orchestrator",
        "tools": ["agent_registry_read", "work_item_read", "work_item_write", "work_item_run", "handoff_write", "agent_action_execute", "agent_action_confirm", "global_llm"],
        "children": ["inbox", "knowledge", "doc-factory", "sub2api", "market", "server", "crawl4ai", "web-research", "cloud-dev", "aihot", "idea-analysis", "product-manager", "cid-dashboard", "embodied", "ai-learning"],
        "rounds": ["路由意图", "调用子 Agent", "汇总结果并记录交接"],
        "next": "根据任务自动选择子 Agent，并沉淀跨项目交接",
    },
    "inbox": {
        "name": "收件箱 Agent",
        "status": "implemented",
        "kind": "workflow",
        "tools": ["inbox_read", "inbox_write", "inbox_triage", "work_item_write", "agent_session_read", "agent_session_write", "handoff_write"],
        "rounds": ["读取待处理", "自动分类/提取截止时间/查重", "生成交接候选并确认路由"],
        "next": "继续积累真实反馈样本并观察分类指标；无人值守多步骤编排仍需明确确认边界",
    },
    "knowledge": {
        "name": "知识库 Agent",
        "status": "implemented",
        "kind": "knowledge",
        "tools": ["knowledge_search", "knowledge_write", "inbox_read", "obsidian_index_read", "obsidian_search", "obsidian_relation_read", "obsidian_inbox_write_confirm", "agent_session_read", "agent_session_write", "handoff_write"],
        "rounds": ["检索工作台与 Obsidian", "生成带来源的沉淀候选", "补充双链、反向链接和 MOC 提示", "确认后写入 Obsidian Inbox 并登记审计"],
        "next": "真实语义样本与长期评估；段落处置仍需线上人工确认闭环",
    },
    "doc-factory": {
        "name": "文档工厂 Agent",
        "status": "implemented",
        "kind": "generator",
        "tools": ["global_llm", "document_template_read", "document_validate", "document_review", "outputs_write", "artifact_read", "agent_session_read", "agent_session_write", "handoff_write"],
        "rounds": ["读取材料", "生成初稿", "校验并多轮修订"],
        "next": "模板库扩充与线上审批长周期观察",
    },
    "sub2api": {
        "name": "Sub2API 账户 Agent",
        "status": "implemented",
        "kind": "monitor",
        "tools": ["sub2api_snapshot_read", "sub2api_snapshot_sync", "sub2api_alert_evaluate", "agent_session_read", "agent_session_write", "handoff_write"],
        "rounds": ["读取并校验脱敏快照", "判断数据新鲜度与额度/到期风险", "生成历史记录、提醒工作项和应用通知"],
        "next": "可信 Origin 浏览器同步和跨 Provider 成本统计已接入；继续做线上长周期观察",
    },
    "market": {
        "name": "量化研究 Agent",
        "status": "implemented",
        "kind": "research",
        "tools": ["market_snapshot_read", "market_history_read", "market_analysis_read", "market_observation_write", "watchlist_write", "agent_action_execute", "agent_session_read", "agent_session_write", "handoff_write"],
        "rounds": ["读取自选、行情与数据时间", "计算趋势/波动/成交活跃度并解释变化", "生成去重观察任务并记录结果"],
        "next": "线上真实联动验收和数据源长期稳定性观察；研究结论、样本校验与策略对比已完成",
    },
    "server": {
        "name": "服务器监控 Agent",
        "status": "implemented",
        "kind": "ops",
        "tools": ["server_readonly_probe", "server_history_read", "server_analysis_read", "server_thresholds_set", "agent_session_read", "agent_session_write", "handoff_write"],
        "rounds": ["只读探测", "读取历史并判断新鲜度", "生成告警/恢复记录并等待人工确认"],
        "next": "线上执行日志与快照回退观察；高风险动作仍需服务器侧人工处理",
    },
    "crawl4ai": {
        "name": "网页研究 Agent",
        "status": "implemented",
        "kind": "research",
        "tools": ["crawl", "evidence_search", "global_llm"],
        "rounds": ["抓取网页", "基于证据分析", "交接知识库或文档工厂"],
        "next": "证据比较/跨项目交接页面深化和线上长周期运行观察",
    },
    "web-research": {
        "name": "网页研究浏览器 Agent",
        "status": "implemented",
        "kind": "research",
        "tools": ["crawl", "evidence_search", "artifact_read", "global_llm", "handoff_write"],
        "rounds": ["打开网页并建立研究上下文", "基于来源证据追问", "保存 Artifact 或交接给后续项目"],
        "next": "后续再接入登录接管和受控网页动作；当前只抓取公开网页",
    },
    "cloud-dev": {
        "name": "云开发 Agent",
        "status": "implemented",
        "kind": "ops",
        "tools": ["cloud_dev_generate", "cloud_dev_patch", "cloud_dev_status", "cloud_dev_test", "cloud_dev_build", "work_item_read", "agent_session_read", "agent_session_write"],
        "rounds": ["解析结构化云开发命令或自然语言生成需求", "校验显式工作区和固定命令，或生成可交付产物", "执行只读状态/测试、生成产物或进入构建审批"],
        "next": "按需增加项目级固定配方；不开放任意 shell、远程命令或自动部署",
    },
    "aihot": {
        "name": "AI 热点研究 Agent",
        "status": "implemented",
        "kind": "research",
        "tools": ["aihot_feed_read", "aihot_filter", "aihot_feedback_write", "aihot_opportunity_write", "global_llm"],
        "rounds": ["读取最新资讯", "筛选有用消息并去重", "学习本地反馈", "围绕来源继续对话并发现机会", "把机会交给想法分析验证"],
        "next": "来源评分细化、机会卡深度复盘和线上推送送达稳定性",
    },
    "idea-analysis": {
        "name": "想法分析 Agent",
        "status": "implemented",
        "kind": "venture",
        "tools": ["idea_session_read", "idea_session_write", "idea_opportunity_read", "idea_validation_run", "idea_validation_plan_write", "global_llm"],
        "rounds": ["澄清想法", "接收热点/看板机会", "判断需求与商业可行性", "生成结构化验证任务", "持续复盘"],
        "next": "线上真实联动验收；继续评估证据包复盘质量",
    },
    "product-manager": {
        "name": "产品经理 Agent",
        "status": "implemented",
        "kind": "product",
        "tools": ["global_llm", "artifact_read", "work_item_read", "work_item_write", "document_template_read", "agent_session_read", "agent_session_write", "handoff_write"],
        "rounds": ["读取反馈与需求证据", "识别重复、缺口和优先级风险", "形成可评审需求与决策建议", "生成 PRD 或交接后续工作"],
        "next": "积累真实反馈样本，校准优先级权重与决策复盘质量",
    },
    "cid-dashboard": {
        "name": "独立开发者分析 Agent",
        "status": "proxy_agent",
        "kind": "analyst",
        "tools": ["cid_snapshot_read", "cid_snapshot_write", "cid_opportunity_write", "artifact_read", "global_llm"],
        "rounds": ["读取看板上下文", "保存带时间的数据快照", "登记项目机会卡", "把机会交给想法分析验证"],
        "next": "线上真实联动验收与偏好排序质量观察",
    },
    "embodied": {
        "name": "具身智能学习 Agent",
        "status": "implemented",
        "kind": "research",
        "tools": ["crawl_fetch", "knowledge_search", "knowledge_write", "work_item_read", "notify"],
        "rounds": ["研究具身智能学习主题", "沉淀学习笔记与资料", "跟踪领域动态"],
        "next": "按学习路线积累实践记录",
    },
    "ai-learning": {
        "name": "AI 转型学习教练",
        "status": "implemented",
        "kind": "learning",
        "tools": ["global_llm", "knowledge_search", "knowledge_write", "work_item_read", "notify"],
        "rounds": ["根据目标安排今日课程", "讲解知识并拆解真实案例", "布置练习与自测", "记录进度并复盘"],
        "next": "根据学习记录持续调整难度和转型方向",
    },
}

# Tool declarations are intentionally separate from the Agent registry. The
# registry says what a project wants to use; this catalog says what the
# workbench will actually allow at runtime.

AGENT_TOOL_POLICIES: dict[str, dict[str, Any]] = {
    "agent_registry_read": {"label": "读取 Agent 能力图", "mode": "readonly", "risk": "low", "enabled": True, "description": "只读取项目能力、状态和联动关系。"},
    "work_item_write": {"label": "创建工作项", "mode": "auto", "risk": "low", "enabled": True, "description": "在本地数据库创建可追踪工作项。"},
    "work_item_read": {"label": "读取交接工作项", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取分配给当前项目 Agent 的待接收工作项。"},
    "work_item_run": {"label": "执行交接工作项", "mode": "restricted", "risk": "medium", "enabled": True, "description": "在当前项目上下文中领取并执行已确认交接；高风险动作仍停在人工确认。"},
    "handoff_write": {"label": "建立项目交接", "mode": "confirm", "risk": "medium", "enabled": True, "description": "需要用户点击确认后，才把结果交给另一个项目。"},
    "agent_action_execute": {"label": "执行 Agent 动作", "mode": "restricted", "risk": "medium", "enabled": True, "description": "只执行动作协议中已登记的低风险工具。"},
    "agent_action_confirm": {"label": "确认高风险动作", "mode": "confirm", "risk": "high", "enabled": True, "description": "服务器变更、删除、交易和外部发送必须人工确认。"},
    "global_llm": {"label": "调用全局 LLM", "mode": "auto", "risk": "low", "enabled": True, "description": "使用工作台全局配置，不在项目内保存独立 Key。"},
    "agent_session_read": {"label": "读取项目会话", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取当前项目自己的持久会话。"},
    "agent_session_write": {"label": "写入项目会话", "mode": "auto", "risk": "low", "enabled": True, "description": "保存当前项目 Agent 的消息和摘要。"},
    "inbox_read": {"label": "读取收件箱", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取未处理和历史收件箱条目。"},
    "inbox_write": {"label": "写入收件箱", "mode": "auto", "risk": "low", "enabled": True, "description": "明确记录请求可直接写入本地收件箱。"},
    "inbox_triage": {"label": "整理收件箱", "mode": "auto", "risk": "low", "enabled": True, "description": "提取类型、截止时间、重复关系，并生成可确认的项目交接候选。"},
    "knowledge_search": {"label": "搜索知识库", "mode": "readonly", "risk": "low", "enabled": True, "description": "搜索工作台 Markdown 知识文件。"},
    "knowledge_write": {"label": "创建知识笔记", "mode": "auto", "risk": "low", "enabled": True, "description": "只创建新笔记，不覆盖旧文件。"},
    "obsidian_index_read": {"label": "读取 Obsidian 索引", "mode": "readonly", "risk": "low", "enabled": True, "description": "只读扫描本机 Obsidian Markdown，保留路径、标题、标签和更新时间。"},
    "obsidian_search": {"label": "搜索 Obsidian", "mode": "readonly", "risk": "low", "enabled": True, "description": "在已建立的本机 Obsidian 索引中检索笔记和双链。"},
    "obsidian_relation_read": {"label": "分析 Obsidian 关联", "mode": "readonly", "risk": "low", "enabled": True, "description": "基于既有 WikiLink、标签、标题和正文词生成关联建议，不改写 Vault。"},
    "obsidian_inbox_write_confirm": {"label": "确认写入 Obsidian Inbox", "mode": "confirm", "risk": "medium", "enabled": True, "description": "只有用户明确确认后，才在 Vault 的 00 Inbox 创建新 Markdown；不覆盖原笔记。"},
    "document_template_read": {"label": "读取文档模板", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取文档工厂的结构模板和适用场景。"},
    "document_validate": {"label": "检查材料完整性", "mode": "readonly", "risk": "low", "enabled": True, "description": "生成前检查标题、材料、处理要求和模板，列出缺口与风险。"},
    "document_review": {"label": "二次校验文档", "mode": "auto", "risk": "low", "enabled": True, "description": "重新读取登记来源，检查引用覆盖、事实疑点和常见敏感信息；只生成校验报告，不改写原文。"},
    "outputs_write": {"label": "创建文档产物", "mode": "auto", "risk": "low", "enabled": True, "description": "创建新的版本化产物，不覆盖历史文件。"},
    "artifact_read": {"label": "读取 Artifact", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取跨项目登记的产物元数据。"},
    "sub2api_snapshot_read": {"label": "读取 Sub2API 快照", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取脱敏账户、周额度和到期快照。"},
    "sub2api_snapshot_sync": {"label": "同步 Sub2API 脱敏快照", "mode": "auto", "risk": "low", "enabled": True, "description": "接收浏览器页面提供的脱敏字段；不接收、不保存 Cookie、密码或完整 API Key。"},
    "sub2api_alert_evaluate": {"label": "评估 Sub2API 风险", "mode": "auto", "risk": "low", "enabled": True, "description": "判断数据过期、周/月额度偏低、订阅临期和同步异常，并可创建收件箱工作项。"},
    "market_snapshot_read": {"label": "读取行情快照", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取自选股和公开行情快照。"},
    "market_history_read": {"label": "读取行情历史", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取本地保存的历史行情快照，用于比较变化。"},
    "market_analysis_read": {"label": "读取行情观察", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取数据新鲜度、异常保护和可解释研究线索。"},
    "market_observation_write": {"label": "生成行情观察任务", "mode": "auto", "risk": "low", "enabled": True, "description": "根据有数据时间和样本依据的行情因子生成去重本地研究任务；不生成交易指令。"},
    "watchlist_write": {"label": "写入自选股", "mode": "restricted", "risk": "medium", "enabled": True, "description": "仅限用户明确指定的本地自选操作，不执行买卖。"},
    "server_readonly_probe": {"label": "服务器只读探测", "mode": "readonly", "risk": "medium", "enabled": True, "description": "只读检查主机、资源和服务状态。"},
    "server_history_read": {"label": "读取服务器历史", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取本地保存的服务器快照，用于判断变化和恢复。"},
    "server_analysis_read": {"label": "读取服务器分析", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取新鲜度、资源阈值、必需服务和告警恢复判断。"},
    "server_thresholds_set": {"label": "修改服务器阈值", "mode": "confirm", "risk": "medium", "enabled": True, "description": "调整磁盘/内存/负载告警阈值，只写本地监控配置。"},
    "crawl": {"label": "抓取网页", "mode": "restricted", "risk": "medium", "enabled": True, "description": "按研究任务抓取公开网页，受页面数和深度限制。"},
    "evidence_search": {"label": "检索网页证据", "mode": "readonly", "risk": "low", "enabled": True, "description": "在已有抓取结果中检索并保留来源。"},
    "crawl_fetch": {"label": "读取公开网页", "mode": "restricted", "risk": "medium", "enabled": True, "description": "由 Agent 抓取明确给出的公网地址；限制跳转、页面大小和私网目标。"},
    "cloud_dev_generate": {"label": "生成云开发产物", "mode": "restricted", "risk": "medium", "enabled": True, "description": "按白名单模板生成版本化产物；不部署、不覆盖用户文件。"},
    "cloud_dev_patch": {"label": "生成云端修复方案", "mode": "confirm", "risk": "high", "enabled": True, "description": "只生成补丁计划并进入审批；批准后才应用、测试，失败时回滚。"},
    "cloud_dev_status": {"label": "云开发状态", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取显式配置工作区的结构和固定配方，不执行命令。"},
    "cloud_dev_test": {"label": "云开发测试", "mode": "restricted", "risk": "medium", "enabled": True, "description": "只在显式白名单工作区运行固定测试命令；不接受 shell 参数。"},
    "cloud_dev_build": {"label": "云开发构建", "mode": "confirm", "risk": "high", "enabled": True, "description": "构建可能写入工作区，必须进入审批；不包含自动部署。"},
    "aihot_feed_read": {"label": "读取 AI 热点", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取公开资讯快照及其来源。"},
    "aihot_filter": {"label": "筛选热点", "mode": "auto", "risk": "low", "enabled": True, "description": "按时效、价值和去重规则生成精选信号。"},
    "aihot_feedback_write": {"label": "记录热点反馈", "mode": "auto", "risk": "low", "enabled": True, "description": "只在本机保存有用/不相关反馈，用于调整后续排序。"},
    "aihot_opportunity_write": {"label": "创建热点机会任务", "mode": "auto", "risk": "low", "enabled": True, "description": "将用户明确选择的热点登记为交给想法分析 Agent 的本地验证任务。"},
    "idea_session_read": {"label": "读取想法会话", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取当前想法分析会话和验证上下文。"},
    "idea_session_write": {"label": "写入想法会话", "mode": "auto", "risk": "low", "enabled": True, "description": "保存想法判断、假设和验证计划。"},
    "idea_opportunity_read": {"label": "读取机会交接", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取来自 AI 热点、看板或收件箱的待验证机会 WorkItem。"},
    "idea_validation_run": {"label": "运行机会验证", "mode": "auto", "risk": "low", "enabled": True, "description": "将本地机会交接转换为想法会话，并调用全局 LLM 生成结构化验证计划。"},
    "idea_validation_plan_write": {"label": "生成验证工作台", "mode": "auto", "risk": "low", "enabled": True, "description": "把想法拆成假设、7 天验证任务、成功/停止条件和版本化决策，并将本地任务交给收件箱。"},
    "cid_snapshot_read": {"label": "读取看板快照", "mode": "readonly", "risk": "low", "enabled": True, "description": "读取最近一次由看板页面保存的项目列表、赛道和数据时间。"},
    "cid_snapshot_write": {"label": "保存看板快照", "mode": "auto", "risk": "low", "enabled": True, "description": "保存脱敏的项目摘要和来源时间，不保存登录信息或 API Key。"},
    "cid_opportunity_write": {"label": "登记看板机会", "mode": "auto", "risk": "low", "enabled": True, "description": "将用户明确选择的看板项目登记为机会卡，并创建交给想法分析 Agent 的本地工作项。"},
    "notify": {"label": "创建工作台通知", "mode": "auto", "risk": "low", "enabled": True, "description": "只写入工作台内应用通知；浏览器 Push 是否发送仍受独立订阅和静默时段约束。"},
    "web_search": {"label": "公网搜索", "mode": "readonly", "risk": "low", "enabled": True, "description": "在公网搜索网页标题/摘要/链接（只读，无 API key）；要正文时配合 web_fetch。"},
    "web_fetch": {"label": "抓取公网网页", "mode": "readonly", "risk": "low", "enabled": True, "description": "抓取单个公网网页并提取纯文本（只读，15 秒超时，限制私网/跳转）。"},
}

# ---------------------------------------------------------------------------
# 运行时工具策略：按「模型真正能调到的那个名字」登记风险和执行模式。
#
# 在这之前，风险策略（AGENT_TOOL_POLICIES）用的是一套叙述性的能力名
# （market_snapshot_read、watchlist_write…），而模型实际能调的是另一套
# （market_read、market_style_screen…）。两套名字在 market / server /
# doc-factory 上交集为 0，连 work_item_read 和 work_items_read 都差一个 s。
# 后果是策略表对真正会产生副作用的工具一条都没覆盖，而边界校验
# validate_agent_tool_requests 拿声明侧判定，会拒绝真能执行的工具、
# 放行根本没有执行器的名字。
#
# 这张表按运行时名字登记，是唯一被执行路径读取的策略来源。
#   readonly  只读，随便调
#   auto      有副作用但可逆、影响面小（写本地库），自动执行
#   confirm   有外部影响或不易撤销，必须用户点确认才执行
# ---------------------------------------------------------------------------

RUNTIME_TOOL_POLICIES: dict[str, dict[str, Any]] = {
    # —— 只读 ——
    "server_status": {"mode": "readonly", "risk": "low", "label": "查看服务器状态"},
    "sub2api_status": {"mode": "readonly", "risk": "low", "label": "查看账户额度"},
    "knowledge_search": {"mode": "readonly", "risk": "low", "label": "搜索知识库"},
    "inbox_read": {"mode": "readonly", "risk": "low", "label": "读取收件箱"},
    "work_items_read": {"mode": "readonly", "risk": "low", "label": "读取工作项"},
    "aihot_read": {"mode": "readonly", "risk": "low", "label": "读取 AI 热点"},
    "market_read": {"mode": "readonly", "risk": "low", "label": "读取行情"},
    "market_style_screen": {"mode": "readonly", "risk": "low", "label": "按流派筛自选"},
    "product_read": {"mode": "readonly", "risk": "low", "label": "读取产品需求"},
    "learning_read": {"mode": "readonly", "risk": "low", "label": "读取学习进度"},
    "cloud_dev_status": {"mode": "readonly", "risk": "low", "label": "查看云开发工作区"},
    "doc_validate": {"mode": "readonly", "risk": "low", "label": "校验文档材料"},
    "doc_template": {"mode": "readonly", "risk": "low", "label": "读取文档模板"},
    "idea_read": {"mode": "readonly", "risk": "low", "label": "读取想法会话"},
    "cid_read": {"mode": "readonly", "risk": "low", "label": "读取机会看板"},
    # 公网只读：不改本地任何东西，取回来的内容一律当不可信输入处理。
    "web_search": {"mode": "readonly", "risk": "low", "label": "搜索公网"},
    "web_fetch": {"mode": "readonly", "risk": "low", "label": "抓取网页"},
    "crawl_fetch": {"mode": "readonly", "risk": "medium", "label": "抓取公开网页",
                    "note": "只发出站只读请求，但会访问外部站点。"},
    "market_analyze": {"mode": "readonly", "risk": "low", "label": "运行行情因子分析"},

    # —— 写本地库，可逆，自动执行 ——
    "inbox_capture": {"mode": "auto", "risk": "low", "label": "写入收件箱"},
    "knowledge_write": {"mode": "auto", "risk": "low", "label": "创建知识笔记"},
    "inbox_triage": {"mode": "auto", "risk": "low", "label": "整理收件箱"},
    "aihot_feedback": {"mode": "auto", "risk": "low", "label": "记录热点反馈"},
    # notify 只是往应用内通知中心写一条记录（浏览器 Push 还受独立订阅约束），
    # 本身无害——每条通知都要人工确认会打断 Agent 流程。降为 auto。
    "notify": {"mode": "auto", "risk": "low", "label": "发送通知",
               "note": "写入应用内通知中心；浏览器 Push 是否发送仍受独立订阅和静默时段约束。"},

    # —— 需要确认（真高危：执行命令、不可逆、外部副作用）——
    # cloud_dev_test 在服务器上真实执行固定命令，保留确认。
    "cloud_dev_test": {"mode": "confirm", "risk": "medium", "label": "运行云开发测试",
                       "note": "在服务器上真实执行一条固定命令。"},
    # cloud_dev_generate 只生成版本化产物（不部署、不覆盖用户文件），可逆 → auto。
    "cloud_dev_generate": {"mode": "auto", "risk": "medium", "label": "生成云端产物",
                           "note": "按白名单模板生成版本化产物，不部署、不覆盖用户文件。"},
    # cloud_dev_patch 自己已经走审批链路（只生成编辑计划，批准后才应用），
    # 所以这里标 auto——再加一道确认等于同一件事要点两次。
    "cloud_dev_patch": {"mode": "auto", "risk": "medium", "label": "生成代码编辑计划",
                        "note": "只生成计划，应用前另有审批。"},
}



AGENT_ROUTE_HINTS: dict[str, tuple[str, ...]] = {
    "inbox": ("待办", "收件箱", "记录", "提醒", "任务", "想法"),
    "knowledge": ("知识库", "笔记", "沉淀", "总结", "方法", "obsidian"),
    "doc-factory": ("文档", "报告", "方案", "交付", "pdf", "word"),
    "sub2api": ("sub2api", "余额", "额度", "订阅", "key", "到期"),
    "market": ("股票", "行情", "量化", "选股", "涨跌", "因子"),
    "server": ("服务器", "主机", "磁盘", "内存", "nginx", "部署", "异常"),
    "crawl4ai": ("爬虫", "抓取", "网页", "研究", "资料", "来源"),
    "web-research": ("网页研究浏览器", "研究浏览器", "标签页", "网页", "来源", "引用"),
    "aihot": ("ai热点", "AI 热点", "资讯", "新闻", "热点", "最新消息"),
    "idea-analysis": ("想法", "商机", "创业", "做点事", "靠谱不靠谱", "项目分析", "可行性"),
    "product-manager": ("产品经理", "产品需求", "用户反馈", "需求池", "prd", "优先级", "rice", "产品决策"),
    "cid-dashboard": ("独立开发", "项目看板", "github", "赛道", "产品机会"),
    "ai-learning": ("ai学习", "AI 学习", "ai转型", "AI 转型", "学习计划", "课程", "案例", "练习"),
}


AGENT_PLAYBOOKS: dict[str, dict[str, Any]] = {
    "workbench": {
        "mission": "把复杂请求拆成项目任务，调用真正有数据和工具的子 Agent，再汇总为可执行决策",
        "workflow": "识别目标 → 选择子 Agent → 检查上下文新鲜度 → 调用工具/分析 → 写入工作项 → 汇总与追踪",
        "output": ["任务拆解", "子 Agent 证据", "已执行动作", "待确认事项", "下一步与负责人"],
        "autonomy": "低风险本地动作自动执行；外部、不可逆和有歧义动作必须确认",
    },
    "inbox": {
        "mission": "把输入变成不丢失、可分类、可继续处理的收件箱条目",
        "workflow": "识别内容类型 → 提取任务/截止时间/标签 → 判断是否重复 → 生成目标 Agent 候选 → 用户确认后创建工作项",
        "output": ["类型判断", "原文事实", "建议标签", "可直接执行动作", "后续路由"],
        "autonomy": "记录和本地整理自动执行；跨项目交接先展示候选，确认后才创建 WorkItem/Relation",
    },
    "knowledge": {
        "mission": "把零散信息变成可检索、可引用、可复用的知识资产",
        "workflow": "检索工作台与 Obsidian → 查看今日更新/双链关系 → 对比已有内容 → 提炼原子结论 → 保留来源/双链 → 用户确认后写入 Obsidian Inbox",
        "output": ["相关笔记", "新增事实", "冲突与缺口", "笔记草稿", "链接/标签建议"],
        "autonomy": "Obsidian 只读扫描、今日更新、关联和 MOC 提示可自动执行；写入 00 Inbox 必须用户明确确认，不覆盖 Vault 原文件",
    },
    "doc-factory": {
        "mission": "把材料加工成版本化、可交付、可校验的文档产物",
        "workflow": "识别交付对象 → 检查材料完整性 → 选择模板 → 生成新版本 → 记录来源与版本链 → 列出校验项",
        "output": ["交付目标", "材料缺口", "文档结构", "初稿/改稿建议", "校验清单"],
        "autonomy": "只创建新版本产物；不覆盖既有文件",
    },
    "sub2api": {
        "mission": "解释账户、每周额度、订阅到期和 Key 用量，主动发现风险",
        "workflow": "确认快照时间 → 区分余额/周额度/月额度 → 判断趋势与阈值 → 生成提醒",
        "output": ["数据时间", "核心指标", "变化与风险", "提醒级别", "建议动作"],
        "autonomy": "只读脱敏快照；登录、充值、删除 Key 和外部操作必须确认",
    },
    "market": {
        "mission": "从自选和行情快照中解释涨跌、风险和可验证的选股线索，不做交易",
        "workflow": "读取自选与历史快照 → 检查数据时间/异常 → 解释日涨跌、开盘和成交量 → 给出观察假设 → 写入自选或观察任务",
        "output": ["行情状态", "变化解释", "因子/假设", "风险边界", "观察动作"],
        "autonomy": "添加本地自选可自动执行；买卖、下单和外部发送永远禁止自动执行",
    },
    "server": {
        "mission": "对服务器做只读体检，定位异常并给出可回滚的处理建议",
        "workflow": "检查主机与快照时间 → 对比负载/磁盘/内存/服务 → 判断影响范围 → 创建告警工作项",
        "output": ["体检结果", "异常证据", "影响范围", "排查顺序", "需确认的变更"],
        "autonomy": "只读探测和告警可自动执行；重启、部署、配置修改必须人工确认",
    },
    "crawl4ai": {
        "mission": "围绕研究问题抓取证据、标注来源、比较信息并形成可追溯结论",
        "workflow": "拆研究问题 → 抓取来源 → 提取证据 → 区分事实/推断 → 交接笔记或文档",
        "output": ["研究问题", "来源与证据", "事实/推断", "缺口", "交接产物"],
        "autonomy": "抓取和本地证据整理可执行；对外发布和发送必须确认",
    },
    "web-research": {
        "mission": "把多个网页和研究问题组织成可追问、可引用、可交接的研究上下文",
        "workflow": "建立标签页上下文 → 抓取公开来源 → 区分事实/推断 → 保存 Artifact 或交接",
        "output": ["研究问题", "来源与证据", "事实/推断", "缺口", "交接产物"],
        "autonomy": "只抓取公开网页并保存本地研究记录；登录、表单提交和外部发布必须人工确认",
    },
    "aihot": {
        "mission": "从 AI 热点中筛出最新、有用且可转化的信号，避免重复和噪音",
        "workflow": "去重 → 按价值/时效筛选 → 提取事实 → 判断影响 → 形成机会线索与追问",
        "output": ["精选信号", "来源链接", "为什么重要", "可验证机会", "下一步研究"],
        "autonomy": "读取、筛选和生成研究任务可执行；对外转载或发送必须确认",
    },
    "idea-analysis": {
        "mission": "把模糊想法变成可证伪的需求假设、商业判断和 7 天验证计划",
        "workflow": "澄清用户/场景 → 找替代方案 → 判断痛点与付费 → 估算最小方案 → 安排验证",
        "output": ["一句话判断", "关键假设", "证据与未知", "反证条件", "7 天验证计划"],
        "autonomy": "只保存会话和本地验证任务；对外访谈、投放和收费动作必须确认",
    },
    "product-manager": {
        "mission": "把零散反馈和研究证据转成可排序、可评审、可复盘的产品决策",
        "workflow": "核对反馈来源 → 聚类用户问题 → 检查需求证据 → 评估 RICE 与风险 → 形成 PRD/决策建议 → 跟踪下一步",
        "output": ["今日产品结论", "反馈与来源", "需求优先级", "证据缺口", "评审问题", "下一步动作"],
        "autonomy": "读取、分析和本地草稿可自动执行；调整优先级、确认决策和对外交付必须由产品经理确认",
    },
    "cid-dashboard": {
        "mission": "把看板中的项目和趋势转成可研究、可比较、可验证的机会卡",
        "workflow": "筛选项目 → 识别用户/分发/收入线索 → 对比竞品 → 判断可复制性 → 生成研究任务",
        "output": ["项目摘要", "机会信号", "竞品/替代", "风险", "研究任务"],
        "autonomy": "只读分析和本地任务记录；联系作者或复制外部内容必须确认",
    },
    "ai-learning": {
        "mission": "围绕个人转型目标，用每天一节可完成的小课把 AI 知识转成工作能力和作品证据",
        "workflow": "读取目标与进度 → 选择不重复主题 → 讲清知识 → 拆解工作案例 → 布置练习与自测 → 记录复盘",
        "output": ["今日知识", "案例拆解", "动手练习", "自测解释", "下一步与沉淀建议"],
        "autonomy": "课程生成、进度记录和本地提醒可自动执行；对外发布作品或发送内容必须确认",
    },
}


AGENT_RUN_STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "succeeded": "已完成",
    "partial": "部分完成",
    "failed": "失败，可重试",
    "cancelled": "已取消",
}

# The registry answers "which Agent exists"; this matrix answers "what can it
# actually do today". Keeping both explicit prevents a configured LLM from
# being mistaken for a completed project Agent.

AGENT_IMPLEMENTATIONS: dict[str, dict[str, Any]] = {
    "workbench": {"implemented": ["能力图路由", "子 Agent 汇总", "WorkItem 记录", "交接领取/执行", "动作确认", "统一 Run 审计", "失败重试", "执行计划", "独立 Agent Worker", "自动化规则", "统一结果协议", "LLM 运行指标", "Crawl/Sync/Monitor Worker 租约", "审批历史与重新提交", "主动协作计划", "决策 Artifact 与后续待办"], "gaps": ["跨项目结果汇总的线上真实证据", "25 条联动边逐条验收", "主动协作计划的线上长周期执行观察"]},
    "inbox": {"implemented": ["读取收件箱", "写入收件箱", "自动分类", "截止时间提取", "重复检测", "重复合并建议", "优先级与过期提醒", "批量完成/归档/恢复", "交接候选", "目标 Agent 交接执行", "持久 Agent 会话", "Run/失败记录", "确认历史分类软提示", "分类准确率与 Macro-F1", "来源回溯", "下一步提取"], "gaps": ["更多真实反馈样本与长期评估", "无人值守多步骤编排"]},
    "knowledge": {"implemented": ["Markdown 搜索", "写入新笔记", "Obsidian 只读索引", "标题/标签/双链检索", "今日更新", "双链/主题关联建议", "MOC 孤立笔记提示", "收件箱沉淀候选", "批量确认写入 Obsidian Inbox", "MOC 预览与确认维护", "Artifact/Relation/应用事件审计", "反向链接计数", "本地 Hybrid 检索", "检索自召回评估", "语义召回增益指标", "冲突检测", "引用行号", "来源草稿", "确认后写入草稿", "写入前备份", "段落稳定 ID", "保留左/右、合并、忽略处置", "冲突审阅 Artifact", "引用回放 UI", "来源回放 UI", "持久 Agent 会话", "显式交接"], "gaps": ["真实语义样本与长期评估", "线上人工确认闭环"]},
    "doc-factory": {"implemented": ["材料提取", "模板选择", "生成前材料检查", "Markdown 生成", "Artifact 登记", "版本链", "多 Artifact 来源读取", "来源 Relation 回溯", "事实/引用/敏感信息二次校验", "段落级引用覆盖检查", "结构化修订重点与验收标准", "校验报告 Artifact", "失败 Run 审计", "持久 Agent 会话", "显式交接", "DOCX/PDF 交付", "审批和修改意见回收", "多轮修订审批闭环", "Markdown 预览"], "gaps": ["模板库扩充"]},
    "sub2api": {"implemented": ["脱敏快照", "浏览器同步接收接口", "可信 Origin 校验", "服务器自动同步", "数据新鲜度判断", "余额/周额度/月额度/Key 用量区分", "历史快照", "额度与到期风险评估", "趋势图与变化 delta", "变化解释页面", "额度预测", "按 Provider/分组成本统计", "告警 WorkItem/Notification", "持久 Agent 会话", "显式交接", "自动化规则巡检"], "gaps": ["线上长周期观察与已登录浏览器兜底稳定性"]},
    "market": {"implemented": ["自选读写", "行情快照", "历史快照", "显式历史样本采集", "数据新鲜度", "异常报价保护", "小白决策中心", "用户买入/卖出/止损线", "触线与临近分组", "历史分位参考区间", "样本质量与失效风险", "风险预算仓位示例", "回测样本质量校验", "回测来源稳定性", "盘中样本间隔感知覆盖度", "日涨跌/开盘/成交量观察", "趋势/波动/成交活跃度因子", "去重观察任务", "WorkItem/Relation/应用通知", "加入自选动作", "持久 Agent 会话", "显式交接", "事件驱动研究", "研究结论沉淀到知识库", "策略版本", "回测", "策略对比", "walk-forward 样本外验证", "手续费/滑点净收益展示", "费用敏感性", "估值因子 API", "估值因子页面", "日报/周报"], "gaps": ["数据源长期稳定性观察", "更多历史样本后的区间稳定性评估"]},
    "server": {"implemented": ["SSH 只读探测", "主机资源检查", "服务状态快照", "历史快照", "新鲜度与阈值分析", "告警 WorkItem/Notification", "恢复记录", "可配置阈值", "健康评分", "只读 Runbook", "服务器动作审批登记", "批准后的低风险只读执行", "执行日志", "本地快照回退", "独立 Monitor Worker 基础", "持久 Agent 会话", "显式交接", "自动化周检"], "gaps": ["线上长周期执行观察", "高风险重启/日志读取仍需服务器侧人工处理"]},
    "crawl4ai": {"implemented": ["网页抓取", "证据检索", "首轮分析", "结构化结果", "来源质量与标题行定位", "多轮问答", "持久 Run", "WorkItem/Artifact 关系", "SQLite 队列", "独立 Worker", "原子领取与租约", "取消任务", "失败重试", "研究计划", "同 URL 变化检测", "证据比较 API/UI", "跨项目交接 API/UI"], "gaps": ["线上长周期运行观察"]},
    "web-research": {"implemented": ["桌面真实网页画布", "持久登录会话", "多标签研究上下文", "实时页面/选区/控件读取", "受控点击/填写/选择/滚动", "敏感操作二次确认", "公开网页抓取", "来源质量展示", "首轮总结", "证据追问", "Artifact/WorkItem 交接", "失败可恢复"], "gaps": ["需要人工完成密码、验证码、付款与文件上传", "更多复杂网站的长期兼容性观察"]},
    "cloud-dev": {"implemented": ["飞书一句话解析", "显式工作区白名单", "固定测试配方", "状态读取", "命令输出截断", "Agent Run/WorkItem 审计", "构建审批边界"], "gaps": ["未配置工作区时不执行", "不提供任意 shell、部署或远程 SSH"]},
    "aihot": {"implemented": ["资讯同步", "多数据源", "筛选", "链接/标题去重", "本地有用/不相关反馈", "反馈加权排序", "摘要与主题聚类", "来源质量洞察", "结构化机会评分", "新增/消失变化检测", "变化与机会复盘摘要", "热点机会 WorkItem", "Artifact/Relation/应用通知闭环", "机会复盘 API/UI", "每日摘要自动化", "AI 热点摘要 Web Push", "持久 Agent 会话", "Run/失败重试", "选中资讯对话"], "gaps": ["来源评分细化", "线上推送送达稳定性"]},
    "idea-analysis": {"implemented": ["多轮会话", "判断结论", "7 天验证路径", "热点机会 WorkItem 接收", "机会 → 想法会话", "机会来源 Artifact 关系", "结构化假设库", "证据/指标回填", "结构化访谈", "证据包 API/UI", "验证任务 WorkItem", "成功/停止条件", "版本化决策", "继续/暂停/转向比较", "结论沉淀知识库", "结构化验证 Run", "WorkItem/Relation/应用通知结果", "持久会话", "Run/失败重试", "到期提醒 API/UI", "多轮复盘入口"], "gaps": ["线上真实联动验收"]},
    "product-manager": {"implemented": ["产品今日看板", "反馈证据登记", "反馈状态流转", "反馈转需求", "需求池", "RICE 优先级", "证据数量提示", "需求状态流转", "PRD 生成", "决策记录", "Artifact/WorkItem/Relation 审计", "持久 Agent 会话", "显式交接"], "gaps": ["真实反馈聚类样本评估", "GitHub 交付状态自动回写", "指标与实验复盘"]},
    "cid-dashboard": {"implemented": ["看板页", "全局 LLM 代理", "OpenAI 兼容接口", "持久代理 Run", "失败重试", "带来源时间的看板快照", "项目机会卡 Artifact", "机会 → 想法分析 WorkItem", "竞品比较", "研究任务", "证据回溯 API/UI", "个人偏好 API/UI", "个人偏好学习", "机会复盘 API/UI"], "gaps": ["线上真实联动验收"]},
    "ai-learning": {"implemented": ["个性化学习目标", "每日知识卡", "工作案例拆解", "动手练习", "自测与答案解释", "连续学习统计", "课程笔记沉淀", "工作台通知", "定时浏览器 Push", "LLM 个性化生成", "无 LLM 课程降级"], "gaps": ["更多真实学习反馈后的难度自适应", "长期推送送达观察"]},
}

# Project-to-project handoffs are first-class capabilities, not just links in
# the sidebar. The API exposes this graph and Agent context includes the
# relevant inbound/outbound edges.

AGENT_STATUS_LABELS = {
    "orchestrator": "总调度",
    "context_ready": "上下文就绪",
    "one_shot": "单轮生成",
    "tool_ready": "工具就绪",
    "implemented": "已接入",
    "proxy_agent": "代理已接入",
    "planned": "规划中",
}



def runtime_tool_policy(name: str) -> dict[str, Any]:
    """查不到策略的工具按「需要确认」处理。

    默认拒绝而不是默认放行：新增工具时忘了登记策略，最坏结果是多点一次确认，
    而不是一个没人审过的副作用被静默执行。
    """
    policy = RUNTIME_TOOL_POLICIES.get(name)
    if policy:
        return {**policy, "registered": True}
    return {"mode": "confirm", "risk": "medium", "label": name, "registered": False,
            "note": "这个工具没有登记风险策略，按需要确认处理。"}



def assert_runtime_tool_policies() -> list[str]:
    """每个能被调用的工具都必须登记策略。"""
    missing = [
        name for name in (set(_react_tools()) | set(_subagent_extra_tools()))
        if name not in RUNTIME_TOOL_POLICIES
    ]
    if missing:
        log.error("这些工具没有登记运行时风险策略：%s", "、".join(sorted(missing)))
    return sorted(missing)



def subagent_tool_schemas(project_id: str) -> list[dict[str, Any]]:
    """返回指定子 Agent 可用的工具 schema（全局工具 + 子 Agent 专属工具）。"""
    names = SUBAGENT_TOOL_MAP.get(project_id, [])
    schemas: list[dict[str, Any]] = []
    for name in names:
        entry = _react_tools().get(name) or _subagent_extra_tools().get(name)
        if entry:
            schemas.append({"type": entry.get("type", "function"), "function": entry["function"]})
    return schemas


# 每个子 Agent 的工具清单：总调度调用子 Agent 时，子 Agent 用这些工具真执行。
#
# 所有 Agent 统一配 web_search（公网搜索，只读）；没有 crawl_fetch 的项目再补
# web_fetch（抓正文，handler 与 crawl_fetch 相同，不重复给已有抓取能力的项目）。
# 背景：doc-factory 写"深度分析"类文档时没有上网能力，只能声称"交接给网页研究
# Agent"，而交接不落地（actions 为空）——用户干等。给每个 Agent 上网能力后，
# 调研类问题可以当场搜索+抓取，不用绕交接。

def _contract_source_ref(value: Any) -> dict[str, Any] | None:
    """Keep source references small, stable and safe to render in the UI."""
    if not isinstance(value, dict):
        value = {"label": str(value or "")}
    source_id = value.get("source_id") or value.get("id") or value.get("artifact_id") or value.get("work_item_id") or value.get("relation_id") or value.get("crawl_run_id")
    source_type = value.get("source_type") or value.get("type")
    if not source_type:
        source_type = "artifact" if value.get("artifact_id") else "work_item" if value.get("work_item_id") else "relation" if value.get("relation_id") else "source"
    label = value.get("label") or value.get("title") or value.get("name") or value.get("source") or value.get("url") or value.get("link") or source_id or "未命名来源"
    locator = value.get("locator") or value.get("url") or value.get("link") or value.get("path") or ""
    data_as_of = value.get("data_as_of") or value.get("data_at") or value.get("fetched_at") or value.get("checked_at") or value.get("published_at") or value.get("updated_at") or value.get("created_at") or ""
    ref = {
        "type": str(source_type)[:60],
        "label": clip(str(label), 240),
    }
    if source_id not in (None, ""):
        ref["id"] = str(source_id)
    if locator:
        ref["locator"] = clip(str(locator), 1_000)
    if data_as_of:
        ref["data_as_of"] = clip(str(data_as_of), 80)
    if value.get("content_hash"):
        ref["content_hash"] = clip(str(value.get("content_hash")), 128)
    if value.get("source_quality") or value.get("quality"):
        ref["source_quality"] = value.get("source_quality") or value.get("quality")
    for key in ("line_start", "line_end"):
        if value.get(key) not in (None, "", 0):
            try:
                ref[key] = int(value[key])
            except (TypeError, ValueError):
                pass
    return ref



def _contract_source_refs(values: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for value in values or []:
        ref = _contract_source_ref(value)
        if not ref:
            continue
        key = (str(ref.get("type", "")), str(ref.get("id", "")), str(ref.get("locator", "")))
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs[:50]



def _contract_source_coverage(refs: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize whether an Agent result has locatable, timestamped sources.

    This is intentionally descriptive rather than a truth score: a complete
    locator does not make a claim true, but a missing locator makes review and
    replay needlessly expensive.
    """
    counts: dict[str, int] = {}
    with_locator = 0
    with_data_time = 0
    for ref in refs:
        source_type = str(ref.get("type") or "source")
        counts[source_type] = counts.get(source_type, 0) + 1
        if ref.get("locator") or ref.get("id"):
            with_locator += 1
        if ref.get("data_as_of"):
            with_data_time += 1
    total = len(refs)
    if not total:
        status = "missing"
    elif with_locator == total and with_data_time == total:
        status = "complete"
    else:
        status = "partial"
    return {
        "status": status,
        "total": total,
        "with_locator": with_locator,
        "with_data_time": with_data_time,
        "types": counts,
    }



AGENT_RESULT_CONTRACT_VERSION = "1.1"


def agent_result_contract(
    project_id: str,
    answer: str,
    *,
    actions: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    source_refs: list[dict[str, Any]] | None = None,
    data_as_of: str = "",
    artifact_ids: list[Any] | None = None,
    work_item_ids: list[Any] | None = None,
    relation_ids: list[Any] | None = None,
    run_id: str = "",
    session_id: str = "",
    replay: dict[str, Any] | None = None,
    execution_plan: dict[str, Any] | None = None,
    memory_refs: list[dict[str, Any]] | None = None,
    memory_updates: list[dict[str, Any]] | None = None,
    memory_context_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize free-form Agent Markdown into a stable, auditable envelope.

    Agents still return readable Markdown to users.  This lightweight parser
    makes the same answer machine-readable without trusting a model to emit
    valid JSON, and keeps the original answer intact for display.
    """
    sections: dict[str, list[str]] = {key: [] for key in AGENT_RESULT_SECTION_ALIASES}
    current = "summary"
    # 逐行切条目，但表格和围栏代码块必须整块保留。
    # 之前是纯粹按行切的，于是模型写的一张表在「结构化结果」里会散成一堆条目——
    # 连 |---|---| 这行分隔线都单独成了一条，读起来完全是乱码。
    raw_lines = str(answer or "").splitlines()
    index = 0
    while index < len(raw_lines):
        line = raw_lines[index].strip()
        if not line:
            index += 1
            continue

        if line.startswith("```"):
            block = [raw_lines[index]]
            index += 1
            while index < len(raw_lines) and not raw_lines[index].strip().startswith("```"):
                block.append(raw_lines[index])
                index += 1
            if index < len(raw_lines):
                block.append(raw_lines[index])
                index += 1
            sections[current].append("\n".join(block))
            continue

        if "|" in line and index + 1 < len(raw_lines) and _is_markdown_table_divider(raw_lines[index + 1]):
            block = [raw_lines[index], raw_lines[index + 1]]
            index += 2
            while index < len(raw_lines) and "|" in raw_lines[index] and raw_lines[index].strip():
                block.append(raw_lines[index])
                index += 1
            sections[current].append("\n".join(block))
            continue

        # 落单的分隔线（表格被截断、或模型写了个没表头的分隔行）不该变成条目。
        if _is_markdown_table_divider(line) or re.fullmatch(r"[-*_]{3,}", line):
            index += 1
            continue

        heading = re.sub(r"^[#\d.、)）\-\s]+", "", line).rstrip("：:")
        matched = next((key for key, aliases in AGENT_RESULT_SECTION_ALIASES.items() if heading in aliases), None)
        index += 1
        if matched:
            current = matched
            continue
        # 顺手削掉行首的列表符号：面板里本来就带项目符号，留着会变成「· 1. 先复现」。
        sections[current].append(re.sub(r"^(?:[•*\-]|\d+[.)])\s*", "", line))
    # summary 是要塞进一行标题里的，表格和代码块拼进去只会是一串竖线。
    summary_lines = [item for item in sections["summary"] if "\n" not in item]
    summary = " ".join(summary_lines) or clip(str(answer).strip().splitlines()[0] if str(answer).strip() else "", 500)
    citations: list[dict[str, str]] = []
    seen_citations: set[str] = set()
    for raw_line in str(answer or "").splitlines():
        for url in re.findall(r"https?://[^\s)\]>]+", raw_line):
            clean_url = url.rstrip("。，、；;:：")
            if clean_url and clean_url not in seen_citations:
                seen_citations.add(clean_url)
                citations.append({"type": "url", "value": clean_url, "label": _hostname(clean_url)})
        for marker in re.findall(r"\[来源[：:]\s*([^\]]+)\]", raw_line):
            value = marker.strip()
            key = f"source:{value}"
            if value and key not in seen_citations:
                seen_citations.add(key)
                citations.append({"type": "source", "value": value, "label": "来源标记"})
    action_summary = []
    action_artifact_ids: list[Any] = []
    action_work_item_ids: list[Any] = []
    action_relation_ids: list[Any] = []
    for action in actions or []:
        action_result = action.get("result") if isinstance(action.get("result"), dict) else {}
        action_artifact_ids.extend(action_result.get("artifact_ids") or ([action_result.get("artifact_id")] if action_result.get("artifact_id") else []))
        action_work_item_ids.extend(action_result.get("work_item_ids") or ([action_result.get("work_item_id")] if action_result.get("work_item_id") else []))
        action_relation_ids.extend(action_result.get("relation_ids") or ([action_result.get("relation_id")] if action_result.get("relation_id") else []))
        action_summary.append({
            "id": action.get("id", ""),
            "name": action.get("name") or action.get("tool", "Agent 动作"),
            "tool": action.get("tool", ""),
            "status": action.get("status", "pending"),
            "requires_confirmation": bool(action.get("requires_confirmation")),
        })
    normalized_refs = _contract_source_refs([*(evidence or []), *(source_refs or [])])
    source_coverage = _contract_source_coverage(normalized_refs)
    derived_data_as_of = data_as_of or next((str(item.get("data_as_of")) for item in normalized_refs if item.get("data_as_of")), "")
    review_reasons: list[str] = []
    if not normalized_refs and not citations:
        review_reasons.append("没有绑定可回溯来源")
    if not derived_data_as_of:
        review_reasons.append("没有数据时间")
    freshness = {
        "status": "timestamped" if derived_data_as_of else "missing",
        "data_as_of": derived_data_as_of,
        "needs_review": bool(review_reasons),
        "review_reasons": review_reasons,
    }
    replay_payload = dict(replay or {})
    replay_payload.setdefault("available", bool(run_id))
    replay_payload.setdefault("run_id", run_id)
    replay_payload.setdefault("session_id", session_id)
    if run_id:
        replay_payload.setdefault("href", f"/api/agent/{project_id}/runs/{run_id}")
    return {
        "schema_version": AGENT_RESULT_CONTRACT_VERSION,
        "agent_id": project_id,
        "agent_name": agent_display_name(project_id),
        "intent": clip(str((execution_plan or {}).get("intent") or ""), 160),
        "summary": clip(summary, 1_000),
        "sections": {key: values[:20] for key, values in sections.items()},
        "evidence": (evidence or [])[:30],
        "source_refs": normalized_refs,
        "source_coverage": source_coverage,
        "data_as_of": derived_data_as_of,
        "freshness": freshness,
        "needs_review": bool(review_reasons),
        "review_reasons": review_reasons,
        "artifact_ids": _contract_id_list([*(artifact_ids or []), *action_artifact_ids, *[item.get("id") for item in normalized_refs if item.get("type") == "artifact"]]),
        "work_item_ids": _contract_id_list([*(work_item_ids or []), *action_work_item_ids, *[item.get("id") for item in normalized_refs if item.get("type") == "work_item"]]),
        "relation_ids": _contract_id_list([*(relation_ids or []), *action_relation_ids, *[item.get("id") for item in normalized_refs if item.get("type") == "relation"]]),
        "run_id": str(run_id or ""),
        "session_id": str(session_id or ""),
        "replay": replay_payload,
        "execution_plan": dict(execution_plan) if isinstance(execution_plan, dict) else {},
        "tool_plan": (execution_plan or {}).get("tool_plan", [])[:30] if isinstance((execution_plan or {}).get("tool_plan", []), list) else [],
        "memory_refs": [
            {key: item.get(key) for key in ("id", "content", "scope", "project_id", "kind", "confidence", "pinned") if item.get(key) not in (None, "")}
            for item in (memory_refs or [])[:20]
            if isinstance(item, dict)
        ],
        "memory_context": {
            key: (bool((memory_context_stats or {}).get(key)) if key == "core_only" else max(0, int((memory_context_stats or {}).get(key) or 0)))
            for key in ("items", "chars", "pinned", "matched", "calls", "max_items", "max_chars", "core_only")
        },
        "memory_updates": [
            {key: item.get(key) for key in ("id", "content", "scope", "project_id", "kind", "status", "status_label", "learning_reason") if item.get(key) not in (None, "")}
            for item in (memory_updates or [])[:20]
            if isinstance(item, dict)
        ],
        "citations": citations[:30],
        "actions": action_summary,
        "original_answer": str(answer or ""),
    }



def available_child_agents() -> list[str]:
    return [item.get("id") for item in load_projects() if item.get("id") in AGENT_REGISTRY]



def capability_route_explanation(message: str, requested: list[str], targets: list[str], intent: str = "") -> dict[str, Any]:
    """Expose why a route was chosen without claiming model-level intent certainty."""
    explicit = [item for item in requested if item in targets]
    lowered = str(message or "").lower()
    signals = {
        project_id: [hint for hint in AGENT_ROUTE_HINTS.get(project_id, ()) if hint.lower() in lowered]
        for project_id in targets
    }
    matched = sum(len(items) for items in signals.values())
    confidence = 0.96 if explicit else min(0.88, 0.28 + matched * 0.12)
    if intent.strip() and not explicit:
        confidence = max(confidence, 0.58)
    if not matched and not explicit:
        confidence = 0.2
    return {
        "mode": "explicit" if explicit else "explicit_intent" if intent.strip() else "capability_heuristic",
        "confidence": round(confidence, 2),
        "needs_confirmation": bool(not explicit and confidence < 0.36),
        "signals": {key: value for key, value in signals.items() if value},
        "intent": clip(intent, 120),
        "note": "显式选择项目" if explicit else "已保留用户意图，目标仍由能力、数据新鲜度和当前负载辅助判断" if intent.strip() else "基于关键词、能力声明、数据新鲜度和当前负载；低置信度时建议人工指定目标",
    }



def agent_declared_tools(project_id: str) -> list[str]:
    """返回这个 Agent 真正能执行的工具名。

    原来这里取的是 AGENT_REGISTRY 里那份叙述性的能力清单
    （market_snapshot_read、watchlist_write…），而模型实际能调的是
    SUBAGENT_TOOL_MAP 里的另一套（market_read、market_style_screen…）。
    实测 market / server / doc-factory 三个项目两套名字交集为 0，于是
    validate_agent_tool_requests 会拒绝真能执行的工具、放行没有执行器的名字——
    边界校验保护的是一个与运行时无关的集合。

    现在只认能执行的那一套。叙述性清单仍留在 registry 里供页面展示，
    但不再参与任何校验。
    """
    return [item["function"]["name"] for item in subagent_tool_schemas(project_id)]



def validate_agent_tool_requests(project_ids: list[str], requested_tools: list[str] | None = None) -> dict[str, Any]:
    """Validate requested capability IDs before an Agent is allowed to run.

    A plan may still expose rejected IDs for observability, but the runtime
    boundary must fail closed: an unknown or out-of-scope tool cannot be
    smuggled into an LLM prompt and presented as executable capability.
    """
    targets = list(dict.fromkeys(str(item).strip() for item in (project_ids or []) if str(item).strip()))
    requested = list(dict.fromkeys(str(item).strip() for item in (requested_tools or []) if str(item).strip()))[:20]
    declared: list[str] = []
    for project_id in targets:
        for tool_id in agent_declared_tools(project_id):
            if tool_id not in declared:
                declared.append(tool_id)
    accepted = [tool_id for tool_id in requested if tool_id in declared]
    rejected = [tool_id for tool_id in requested if tool_id not in declared]
    return {
        "targets": targets,
        "requested": requested,
        "declared": declared[:60],
        "accepted": accepted,
        "rejected": rejected,
        "valid": not rejected,
    }



def build_agent_execution_plan(
    project_id: str,
    message: str,
    *,
    intent: str = "",
    requested_tools: list[str] | None = None,
    route: dict[str, Any] | None = None,
    status: str = "planned",
    child_run_ids: list[str] | None = None,
    declared_tools: list[str] | None = None,
) -> dict[str, Any]:
    """Create a small, deterministic intent/tool plan for an Agent result.

    The model may still write readable Markdown, but the workbench records the
    boundary that was actually declared by the capability registry. Unknown
    tool IDs are rejected from the plan instead of being presented as if they
    were executable. This is an observability contract, not a claim that every
    listed tool was invoked.
    """
    declared_tools = list(dict.fromkeys(
        str(item) for item in (declared_tools if declared_tools is not None else agent_declared_tools(project_id)) if str(item)
    ))[:60]
    requested = [str(item).strip() for item in (requested_tools or []) if str(item).strip()]
    requested = list(dict.fromkeys(requested))[:20]
    accepted = [item for item in requested if item in declared_tools]
    rejected = [item for item in requested if item not in declared_tools]
    if not accepted and not requested:
        accepted = declared_tools[:8]
    route = route if isinstance(route, dict) else {}
    intent_text = clip(str(intent or "").strip() or f"处理{agent_display_name(project_id)}任务", 160)
    boundary = str(AGENT_PLAYBOOKS.get(project_id, {}).get("autonomy") or "只读分析；写入动作按权限确认")
    tool_plan = [
        {
            "id": tool_id,
            "status": "declared",
            "requested": tool_id in requested,
            "note": "已声明能力；本轮是否实际调用以动作记录为准",
        }
        for tool_id in accepted
    ]
    tool_plan.extend(
        {
            "id": tool_id,
            "status": "rejected",
            "requested": True,
            "note": "不在该 Agent 的能力声明中",
        }
        for tool_id in rejected
    )
    steps = [
        {"id": "read_context", "label": "读取已登记项目上下文", "mode": "readonly", "status": "completed" if status in {"completed", "succeeded", "partial"} else "planned"},
        {"id": "analyze", "label": "基于来源与数据时间分析", "mode": "llm", "status": "completed" if status in {"completed", "succeeded", "partial"} else "planned"},
        {"id": "execute_or_handoff", "label": "执行低风险动作或提出人工确认", "mode": "guarded", "status": "completed" if status in {"completed", "succeeded", "partial"} else "planned"},
    ]
    return {
        "schema_version": "1.0",
        "kind": "agent_task",
        "intent": intent_text,
        "message_excerpt": clip(message, 240),
        "target": project_id,
        "targets": [project_id],
        "requested_tools": requested,
        "declared_tools": declared_tools,
        "rejected_tools": rejected,
        "tool_plan": tool_plan,
        "steps": steps,
        "tool_constraints": "只允许使用能力声明中的工具；实际调用必须出现在 agent_actions",
        "confirmation_boundary": boundary,
        "route_mode": str(route.get("mode") or "project_explicit"),
        "route_confidence": round(float(route.get("confidence") or 1.0), 2),
        "needs_confirmation": bool(route.get("needs_confirmation")),
        "child_run_ids": [str(item) for item in (child_run_ids or []) if str(item)],
        "plan_source": "capability_registry",
        "status": status,
    }



def capability_graph_payload() -> dict[str, Any]:
    nodes = []
    for project_id in available_child_agents():
        detail = agent_detail(project_id, llm_ready=bool(llm_settings()["configured"]))
        freshness = project_data_freshness(project_id)
        summary = agent_run_summary(project_id)
        quality = agent_quality_metrics(project_id, 24)
        nodes.append({
            "id": project_id,
            "name": detail.get("name") or agent_display_name(project_id),
            "mission": AGENT_PLAYBOOKS.get(project_id, {}).get("mission", ""),
            "tools": detail.get("tools", []),
            "implemented": detail.get("implemented_tools", []),
            "gaps": detail.get("gaps", []),
            "freshness": freshness,
            "load": {"active": summary.get("active", 0), "failed": summary.get("failed", 0)},
            "quality": quality,
            "links": project_link_summary(project_id),
        })
    return {"generated_at": now_iso(), "nodes": nodes, "edges": [public_project_link(edge) for edge in PROJECT_LINKS]}


def add_agent_run_event(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.add_agent_run_event（agent 运行基础设施仍在 app.py）。"""
    import app as _app

    return _app.add_agent_run_event(*args, **kwargs)


def update_agent_run_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.update_agent_run_record（仍在 app.py）。"""
    import app as _app

    return _app.update_agent_run_record(*args, **kwargs)


def create_agent_run_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_agent_run_record（仍在 app.py）。"""
    import app as _app

    return _app.create_agent_run_record(*args, **kwargs)


def update_work_item_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.update_work_item_record（work-items 领域仍在 app.py）。"""
    import app as _app

    return _app.update_work_item_record(*args, **kwargs)


def create_work_item_record(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_work_item_record（work-items 领域仍在 app.py）。"""
    import app as _app

    return _app.create_work_item_record(*args, **kwargs)


def create_agent_session(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.create_agent_session（agent 会话域仍在 app.py）。"""
    import app as _app

    return _app.create_agent_session(*args, **kwargs)


def get_agent_session(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.get_agent_session（仍在 app.py）。"""
    import app as _app

    return _app.get_agent_session(*args, **kwargs)


def list_agent_messages(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.list_agent_messages（仍在 app.py）。"""
    import app as _app

    return _app.list_agent_messages(*args, **kwargs)


def add_agent_message(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.add_agent_message（仍在 app.py）。"""
    import app as _app

    return _app.add_agent_message(*args, **kwargs)


def update_agent_session_summary(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.update_agent_session_summary（仍在 app.py）。"""
    import app as _app

    return _app.update_agent_session_summary(*args, **kwargs)


def run_agent_react_loop(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.run_agent_react_loop（ReAct 主循环仍在 app.py）。"""
    import app as _app

    return _app.run_agent_react_loop(*args, **kwargs)


def add_log(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.add_log（通用工具仍在 app.py）。"""
    import app as _app

    return _app.add_log(*args, **kwargs)


def add_conversation(*args: Any, **kwargs: Any) -> Any:
    """延迟转发 app.add_conversation（通用工具仍在 app.py）。"""
    import app as _app

    return _app.add_conversation(*args, **kwargs)


class AgentDispatchRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12_000)
    session_id: str = Field(default="", max_length=80)
    project_ids: list[str] = Field(default_factory=list, max_length=8)
    intent: str = Field(default="", max_length=120)
    tool_ids: list[str] = Field(default_factory=list, max_length=20)
    context: dict[str, Any] = Field(default_factory=dict)
    route_confirmed: bool = False



async def call_llm_with_tools(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """带工具 schema 调用 LLM（OpenAI function calling），返回完整响应体。

    使用 _app_call("llm_provider_state")["candidates"]（含真实 api_key），与 call_llm 一致。

    与 ``call_llm`` 保持同样的 fallback 语义：主配置失败后依次尝试 fallback
    条目，而不是让整条 ReAct 链路随第一个 Provider 一起失败。

    这是之前 Agent 最大的可用性缺口——``call_llm`` 有完整的候选链，
    ``call_llm_with_tools`` 却只挑一个 Provider，所以主 Provider 一限流，
    所有"会调用工具"的子 Agent 立刻全灭，而"只聊天"的路径却还正常。
    """
    state = _app_call("llm_provider_state")
    candidates = state.get("candidates") or []
    if not candidates:
        raise RuntimeError("未配置可调用的 LLM Provider")

    async def call_once(provider: dict[str, Any]) -> dict[str, Any]:
        started_at = time.monotonic()
        api_key = str(provider.get("api_key") or "")
        model = str(provider.get("model") or "")
        base_url = str(provider.get("base_url") or "")
        if not api_key or not model or not base_url:
            raise RuntimeError("LLM Provider 配置不完整")
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": model_output_token_limit(model, 3000),
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            client = await _app_call("llm_http_client")
            response = await client.post(chat_completions_url(base_url), headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            _app_call("schedule_llm_usage_event", 
                provider,
                status="failed",
                error_kind=_llm_error_kind(exc),
                latency_ms=int((time.monotonic() - started_at) * 1000),
                purpose="agent_tools",
            )
            raise
        usage = body.get("usage") if isinstance(body, dict) and isinstance(body.get("usage"), dict) else {}
        _app_call("schedule_llm_usage_event", 
            provider,
            status="succeeded",
            latency_ms=int((time.monotonic() - started_at) * 1000),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            purpose="agent_tools",
        )
        return body

    errors: list[str] = []
    first_error: Exception | None = None
    for provider in candidates:
        if _app_call("_llm_health", provider).get("status") == "cooling":
            errors.append(f"{provider.get('name', '未命名')}:rate_limit_cooling")
            continue
        try:
            body = await call_once(provider)
            _app_call("_record_llm_success", provider)
            return body
        except Exception as exc:
            _app_call("_record_llm_failure", provider, exc)
            errors.append(f"{provider.get('name', '未命名')}:{_llm_error_kind(exc)}")
            if first_error is None:
                first_error = exc
            log.warning("工具调用 Provider 失败，尝试下一个：%s", errors[-1])

    # 所有候选都在冷却时，仍然拿第一个硬试一次，避免"明明有配置却直接报错"。
    if first_error is None and candidates:
        try:
            body = await call_once(candidates[0])
            _app_call("_record_llm_success", candidates[0])
            return body
        except Exception as exc:
            first_error = exc
            errors.append(f"{candidates[0].get('name', '未命名')}:{_llm_error_kind(exc)}")

    raise RuntimeError(f"所有 LLM Provider 都无法完成工具调用（{'; '.join(errors) or '无可用候选'}）") from first_error



async def dispatch_agent_task(request: AgentDispatchRequest, *, parent_run_id: str = "",
                              attempt: int = 1, max_attempts: int = 2) -> dict[str, Any]:
    if not llm_settings()["configured"]:
        raise HTTPException(503, "请先配置工作台全局 LLM，才能启动总调度 Agent")
    session = get_agent_session(request.session_id, "workbench") if request.session_id else None
    if request.session_id and not session:
        raise HTTPException(404, "总调度会话不存在")
    prior_messages = list_agent_messages(session["id"], limit=10) if session else []
    routing_context = "\n".join(str(item.get("content") or "") for item in prior_messages[-4:])
    routing_message = f"{routing_context}\n当前请求：{request.message}" if routing_context else request.message
    targets = route_child_agents(routing_message, request.project_ids)
    intent = request.intent.strip() or str(request.context.get("intent") or "").strip()
    tool_ids = [str(item).strip() for item in request.tool_ids if str(item).strip()][:20]
    tool_boundary = validate_agent_tool_requests(targets, tool_ids)
    if not tool_boundary["valid"]:
        raise HTTPException(
            400,
            f"请求的工具不在目标 Agent 能力声明中：{'、'.join(tool_boundary['rejected'])}。请刷新能力列表后重试。",
        )
    route = capability_route_explanation(routing_message, request.project_ids, targets, intent)
    if route.get("needs_confirmation") and not request.route_confirmed:
        raise HTTPException(
            409,
            f"自动路由置信度只有 {round(float(route.get('confidence') or 0) * 100)}%，请在“优先调用的子 Agent”中指定目标后再开始调度。",
        )
    if not session:
        session = create_agent_session("workbench", request.message)
    user_message = add_agent_message(session["id"], "user", request.message, {"source": str(request.context.get("source") or "workbench_dispatch")})
    memory_updates = learn_memories_from_message(
        request.message,
        project_id="workbench",
        source_type="agent_message",
        source_id=str(user_message.get("id") or ""),
    )
    summary_memory_context = memory_context_for_llm("workbench", request.message, core_only=True)
    session_history = list_agent_messages(session["id"], limit=MAX_CONVERSATION_MESSAGES * 2)
    prior_conversation = [item for item in session_history[:-1]][-MAX_CONVERSATION_MESSAGES:]
    conversation_text = "\n".join(f"{item['role']}: {clip(str(item.get('content') or ''), 2_000)}" for item in prior_conversation)
    dispatch_plan = build_agent_execution_plan(
        "workbench",
        request.message,
        intent=intent,
        requested_tools=tool_ids,
        route=route,
        status="planned",
        declared_tools=tool_boundary["declared"],
    )
    run = create_agent_run_record(
        project_id="workbench",
        session_id=session["id"],
        kind="dispatch",
        title=f"总调度：{clip(request.message, 80)}",
        request={"session_id": session["id"], "message": request.message, "intent": intent, "tool_ids": tool_ids, "project_ids": targets, "requested_project_ids": request.project_ids, "route": route, "execution_plan": dispatch_plan, "context": request.context},
        parent_run_id=parent_run_id,
        attempt=attempt,
        max_attempts=max_attempts,
    )
    update_agent_run_record(run["id"], status="running")
    add_agent_run_event(run["id"], "started", f"总调度开始，目标：{'、'.join(agent_display_name(item) for item in targets)}。", metadata={"children": targets, "route": route})
    dispatch = create_work_item_record(
        title=f"总调度：{clip(request.message, 80)}",
        description=request.message,
        kind="agent_dispatch",
        status="running",
        source_project="workbench",
        target_project=",".join(targets),
        metadata={"orchestrator": "workbench", "children": targets, "route": route, "intent": intent, "tool_ids": tool_ids, "session_id": session["id"], "context": request.context},
    )
    child_results: list[dict[str, Any]] = []
    # 本次调度共用的只读工具结果缓存：总调度的 ReAct 预执行和随后每个子 Agent 的
    # 工具循环都读写它，相同工具 + 相同参数只会真正执行一次。
    dispatch_tool_cache: dict[str, Any] = {}
    try:
        context_text = json.dumps(request.context, ensure_ascii=False) if request.context else "无额外上下文"
        summary_memory_text = summary_memory_context["text"]
        conversation_block = conversation_text or "这是该会话的第一轮。"

        async def call_child(project_id: str) -> dict[str, Any]:
            """每个子 Agent 独立 child Run；失败隔离，不影响其他子 Agent。"""
            child_declared_tools = agent_declared_tools(project_id)
            child_tool_ids = [tool_id for tool_id in tool_ids if tool_id in child_declared_tools]
            child_run = create_agent_run_record(
                project_id=project_id,
                kind="dispatch_child",
                title=f"总调度子任务：{agent_display_name(project_id)}",
                request={"message": request.message, "intent": intent, "tool_ids": child_tool_ids, "parent_dispatch_run": run["id"], "source_project": "workbench"},
                parent_run_id=run["id"],
                max_attempts=1,
            )
            update_agent_run_record(child_run["id"], status="running")
            add_agent_run_event(child_run["id"], "started", f"总调度调用子 Agent：{agent_display_name(project_id)}。", metadata={"parent_run": run["id"], "project_id": project_id})
            try:
                child_memory_context = memory_context_for_llm(project_id, request.message)
                project_context = agent_project_context(project_id)
                project_context_text = clip_for_llm(json.dumps(redact_agent_context(project_context), ensure_ascii=False), 14_000)
                react_context_text = clip_for_llm(react_evidence, 10_000) if react_evidence and "无工具数据可探查" not in react_evidence else ""
                react_context_block = f"\n\n真实工具数据证据（总调度已实际调用工具取得，可信）：\n{react_context_text}" if react_context_text else ""
                # 子 Agent ReAct 循环：子 Agent 用自己声明的工具真执行，再基于真实结果总结，
                # 不再"读快照让 LLM 猜"。每步工具调用都在 child run 留痕。
                child_tools = subagent_tool_schemas(project_id)
                react_messages: list[dict[str, Any]] = [
                    {"role": "system", "content": f"{child_agent_system(project_id)}\n\n你可以调用工具获取真实数据后再回答，工具结果是回答的事实依据，不要编造。可用工具：{', '.join(t['function']['name'] for t in child_tools) or '无'}"},
                ]
                if child_memory_context["text"]:
                    react_messages.append({"role": "system", "content": f"用户长期记忆：\n{child_memory_context['text']}"})
                react_messages.append({"role": "user", "content": f"总调度任务：\n{request.message}\n\n同一会话最近上下文：\n{conversation_block}\n\n明确意图：\n{intent or '未单独填写，请从任务中提炼并标记为推断'}\n\n用户额外上下文：\n{context_text}\n\n项目实时上下文（只读快照，可能滞后）：\n{project_context_text}{react_context_block}\n\n涉及当前状态、额度、行情、网页内容、收件箱等场景，请先调用对应工具获取最新真实数据，拿到结果后再按以下顺序回答：\n1. 一句话结论\n2. 已知事实与证据（带数据时间/来源）\n3. 判断、假设与不确定性\n4. 可直接执行的本地动作\n5. 需要我确认的动作\n6. 下一步（负责人 + 最小动作）"})
                # 循环本体见 run_agent_react_loop：项目页直接对话走的是同一份实现，
                # 两条路径共用同一套工具边界、并发上限、超时兜底和留痕格式。
                loop_result = await run_agent_react_loop(
                    project_id=project_id,
                    run_id=child_run["id"],
                    messages=react_messages,
                    tools=child_tools,
                    tool_cache=dispatch_tool_cache,
                )
                answer = loop_result["answer"]
                actions = materialize_agent_actions(project_id, request.message, answer, parent_run_id=child_run["id"])
                child_trace = agent_context_result_metadata({"project_context": project_context, "request_context": request.context or {}})
                child_plan = build_agent_execution_plan(project_id, request.message, intent=intent, requested_tools=child_tool_ids, route=route, status="completed")
                child_plan["kind"] = "dispatch_child"
                child_plan["parent_run_id"] = run["id"]
                child_contract = agent_result_contract(
                    project_id,
                    answer,
                    actions=actions,
                    run_id=child_run["id"],
                    execution_plan=child_plan,
                    memory_refs=child_memory_context["refs"],
                    memory_updates=memory_updates,
                    memory_context_stats=child_memory_context["stats"],
                    **child_trace,
                )
                update_agent_run_record(child_run["id"], status="succeeded", result={"answer": answer, "project_id": project_id, "actions": len(actions), "execution_plan": child_plan}, error="")
                add_agent_run_event(child_run["id"], "succeeded", f"{agent_display_name(project_id)} 已返回结果。", level="success", metadata={"project_id": project_id, "actions": len(actions)})
                add_agent_run_event(run["id"], "child_succeeded", f"{agent_display_name(project_id)} 已返回结果。", level="success", metadata={"project_id": project_id, "actions": len(actions), "child_run_id": child_run["id"]})
                return {"project_id": project_id, "name": agent_display_name(project_id), "answer": answer, "actions": actions, "result_contract": child_contract, "child_run_id": child_run["id"], "memory_refs": child_memory_context["refs"], "memory_context": child_memory_context["stats"], "failed": False}
            except Exception as exc:
                error = clip(str(exc), 800)
                update_agent_run_record(child_run["id"], status="failed", error=error)
                add_agent_run_event(child_run["id"], "failed", f"子 Agent 调用失败：{error}", level="error")
                add_agent_run_event(run["id"], "child_failed", f"{agent_display_name(project_id)} 调用失败：{error}", level="error", metadata={"project_id": project_id, "child_run_id": child_run["id"]})
                failed_plan = build_agent_execution_plan(project_id, request.message, intent=intent, requested_tools=child_tool_ids, route=route, status="failed")
                failed_plan["kind"] = "dispatch_child"
                failed_plan["parent_run_id"] = run["id"]
                return {"project_id": project_id, "name": agent_display_name(project_id), "answer": f"（调用失败：{error}）", "actions": [], "result_contract": agent_result_contract(project_id, f"调用失败：{error}", run_id=child_run["id"], execution_plan=failed_plan), "child_run_id": child_run["id"], "failed": True}

        # 并发调用所有子 Agent；单个失败由 call_child 内部隔离，不影响整体结果
        add_agent_run_event(run["id"], "dispatch_parallel", f"并发调度 {len(targets)} 个子 Agent。", metadata={"children": targets})
        # ReAct 预执行：先让总调度用工具收集真实数据证据，子 Agent 基于真实数据回答
        react_evidence = await react_gather_evidence(request.message, parent_run_id=run["id"], tool_cache=dispatch_tool_cache)
        if react_evidence and "无工具数据可探查" not in react_evidence:
            add_agent_run_event(run["id"], "react_evidence", f"ReAct 收集到工具证据（{len(react_evidence)} 字符）。", level="success")
        # 子 Agent 并发上限：多个子 Agent 同时跑 ReAct 工具循环会放大 LLM 调用
        # 次数，容易触发 Provider 429。默认 3，可用
        # WORKBENCH_AGENT_CHILD_CONCURRENCY 调整；限流时 call_llm_with_tools 现在
        # 会自动切到 fallback Provider，所以比原来的写死 2 更安全。
        child_semaphore = asyncio.Semaphore(AGENT_CHILD_CONCURRENCY)

        async def _call_child_limited(project_id: str) -> dict[str, Any]:
            async with child_semaphore:
                try:
                    # 单个子 Agent 卡住不该让整条调度无限等待：超时按失败隔离，
                    # 其他子 Agent 的结果照常返回（partial）。
                    return await asyncio.wait_for(call_child(project_id), timeout=AGENT_CHILD_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    log.warning("子 Agent %s 超过 %ss 未返回，按失败隔离", project_id, AGENT_CHILD_TIMEOUT_SECONDS)
                    add_agent_run_event(
                        run["id"],
                        "child_timeout",
                        f"{agent_display_name(project_id)} 超过 {AGENT_CHILD_TIMEOUT_SECONDS} 秒未返回，已按失败隔离。",
                        level="error",
                        metadata={"project_id": project_id, "timeout_seconds": AGENT_CHILD_TIMEOUT_SECONDS},
                    )
                    return {
                        "project_id": project_id,
                        "name": agent_display_name(project_id),
                        "answer": f"（超时：{AGENT_CHILD_TIMEOUT_SECONDS} 秒内未返回）",
                        "actions": [],
                        "result_contract": agent_result_contract(project_id, f"子 Agent 超时（{AGENT_CHILD_TIMEOUT_SECONDS}s）"),
                        "child_run_id": "",
                        "failed": True,
                    }

        child_results = await asyncio.gather(*(_call_child_limited(project_id) for project_id in targets))
        combined_memory_refs: list[dict[str, Any]] = []
        seen_memory_ids: set[str] = set()
        for ref in [*summary_memory_context["refs"], *[ref for child in child_results for ref in child.get("memory_refs", [])]]:
            memory_id = str(ref.get("id") or "")
            if not memory_id or memory_id in seen_memory_ids:
                continue
            seen_memory_ids.add(memory_id)
            combined_memory_refs.append(ref)
        memory_contexts = [summary_memory_context["stats"], *[child.get("memory_context", {}) for child in child_results]]
        combined_memory_stats = {
            "items": len(combined_memory_refs),
            "chars": sum(int(item.get("chars") or 0) for item in memory_contexts),
            "pinned": sum(1 for item in combined_memory_refs if item.get("pinned")),
            "matched": sum(1 for item in combined_memory_refs if not item.get("pinned")),
            "calls": sum(int(item.get("calls") or 0) for item in memory_contexts),
            "max_items": MAX_MEMORY_CONTEXT_ITEMS,
            "max_chars": MAX_MEMORY_CONTEXT_CHARS,
            "core_only": False,
        }
        evidence = "\n\n".join(f"【{item['name']}（{item['project_id']}）】\n{item['answer']}" for item in child_results)
        actions = [action for item in child_results for action in item.get("actions", [])]
        action_context = agent_action_notice(actions) or "本轮没有识别到可直接执行的项目动作。"
        final_memory_suffix = f"\n\n用户置顶的核心偏好：\n{summary_memory_text}" if summary_memory_text else ""
        final_answer = await call_llm(
            [
                {
                    "role": "system",
                    "content": f"你是本地工作台总调度 Agent。你不是泛泛聊天助手，而是任务编排器。请整合多个子 Agent 的结果，给出可执行的中文决策摘要：先说结论，再按项目标明证据、判断、下一步、负责人和阻塞点。不要编造子 Agent 没有提供的事实；数据缺失时明确写出缺口和补数据动作。对于动作状态中标记为已执行的动作，必须明确说已执行，不要再次要求用户确认。{final_memory_suffix}",
                },
                {"role": "user", "content": f"原始任务：\n{request.message}\n\n同一会话最近上下文：\n{conversation_block}\n\n真实工具数据证据（ReAct 已实际调用工具取得）：\n{clip_for_llm(react_evidence, 12_000) if react_evidence and '无工具数据可探查' not in react_evidence else '（本轮未收集到工具数据）'}\n\n子 Agent 结果：\n{evidence}\n\n动作状态（以此为准）：\n{action_context}"},
            ],
            max_tokens=4000,
            temperature=0.2,
        )
        if action_context:
            final_answer = f"{final_answer}\n\n{action_context}"
        run_status = "partial" if any(action.get("status") == "failed" for action in actions) or any(item.get("failed") for item in child_results) else "succeeded"
        child_contracts = [item.get("result_contract") or {} for item in child_results]
        final_sources = [ref for contract in child_contracts for ref in contract.get("source_refs", []) if isinstance(ref, dict)]
        final_plan = dict(dispatch_plan)
        final_plan.update(
            {
                "kind": "dispatch",
                "target": ",".join(targets),
                "targets": targets,
                "status": run_status,
                "child_run_ids": [item.get("child_run_id", "") for item in child_results if item.get("child_run_id")],
            }
        )
        final_contract = agent_result_contract(
            "workbench",
            final_answer,
            actions=actions,
            evidence=[{"project_id": item["project_id"], "agent": item["name"], "summary": item.get("result_contract", {}).get("summary", "")} for item in child_results],
            source_refs=final_sources,
            artifact_ids=[artifact_id for contract in child_contracts for artifact_id in contract.get("artifact_ids", [])],
            work_item_ids=[dispatch["id"], *[work_item_id for contract in child_contracts for work_item_id in contract.get("work_item_ids", [])]],
            relation_ids=[relation_id for contract in child_contracts for relation_id in contract.get("relation_ids", [])],
            run_id=run["id"],
            session_id=session["id"],
            replay={"child_run_ids": [item.get("child_run_id", "") for item in child_results if item.get("child_run_id")]},
            execution_plan=final_plan,
            memory_refs=combined_memory_refs,
            memory_updates=memory_updates,
            memory_context_stats=combined_memory_stats,
        )
        assistant_message = add_agent_message(
            session["id"],
            "assistant",
            final_answer,
            {"source": "workbench_dispatch", "run_id": run["id"], "actions": actions, "result_contract": final_contract, "memory_refs": combined_memory_refs, "memory_updates": memory_updates, "memory_context": combined_memory_stats},
        )
        session = update_agent_session_summary(
            session["id"],
            {"last_answer": clip(final_answer, 1_200), "last_run_id": run["id"], "last_result_contract": final_contract, "last_memory_ids": [item["id"] for item in combined_memory_refs]},
        ) or session
        run_result = {"dispatch_id": dispatch["id"], "session_id": session["id"], "message_id": assistant_message["id"], "children": child_results, "answer": final_answer, "route": route, "result_contract": final_contract}
        updated_run = update_agent_run_record(run["id"], status=run_status, result=run_result, error="") or run
        add_agent_run_event(run["id"], run_status, "总调度完成。" if run_status == "succeeded" else "总调度完成，但有子动作失败。", level="success" if run_status == "succeeded" else "warning")
        updated = update_work_item_record(
            dispatch["id"],
            {
                "status": "done",
                "metadata_json": json.dumps({"orchestrator": "workbench", "children": child_results, "actions": actions, "answer": final_answer, "session_id": session["id"], "memory_ids": [item["id"] for item in combined_memory_refs], "memory_context": combined_memory_stats, "context": request.context}, ensure_ascii=False),
            },
        )
        try:
            target_label = "、".join(agent_display_name(item) for item in targets)
            pending_actions = any(action.get("status") == "pending" for action in actions)
            create_notification_record(
                title=f"总调度已完成：{clip(request.message, 80)}",
                body=(
                    f"已调用：{target_label}\n\n"
                    f"结果摘要：{clip(final_answer, 620)}\n\n"
                    f"动作状态：{clip(action_context, 520)}"
                ),
                project_id="workbench",
                kind="agent_dispatch",
                level="warning" if pending_actions else "success",
                href="/",
                event_key=f"agent-dispatch:{run['id']}",
                dedupe_seconds=0,
            )
        except Exception:
            # Notification delivery must never make a completed dispatch fail.
            log.debug("忽略异常（dispatch_agent_task）", exc_info=True)
        return {
            "ok": True,
            "run": updated_run,
            "dispatch": updated or dispatch,
            "orchestrator": "workbench",
            "children": child_results,
            "answer": final_answer,
            "route": route,
            "result_contract": final_contract,
            "session": session,
            "message": assistant_message,
            "messages": list_agent_messages(session["id"], limit=40),
            "memory_refs": combined_memory_refs,
            "memory_updates": memory_updates,
            "memory_context": combined_memory_stats,
        }
    except httpx.HTTPStatusError as exc:
        error = f"子 Agent 调用失败：上游返回 {exc.response.status_code}：{clip(exc.response.text, 500)}"
        update_agent_run_record(run["id"], status="failed", error=error)
        add_agent_run_event(run["id"], "failed", error, level="error")
        update_work_item_record(dispatch["id"], {"status": "failed", "metadata_json": json.dumps({"error": clip(exc.response.text, 500)}, ensure_ascii=False)})
        try:
            create_notification_record(
                title=f"总调度失败：{clip(request.message, 80)}",
                body=error,
                project_id="workbench",
                kind="agent_dispatch",
                level="error",
                href="/",
                event_key=f"agent-dispatch:{run['id']}",
                dedupe_seconds=0,
            )
        except Exception:
            log.debug("忽略异常（dispatch_agent_task）", exc_info=True)
        raise HTTPException(502, error) from exc
    except Exception as exc:
        error = str(exc)
        update_agent_run_record(run["id"], status="failed", error=error)
        add_agent_run_event(run["id"], "failed", f"总调度 Agent 执行失败：{error}", level="error")
        update_work_item_record(dispatch["id"], {"status": "failed", "metadata_json": json.dumps({"error": str(exc)}, ensure_ascii=False)})
        try:
            create_notification_record(
                title=f"总调度失败：{clip(request.message, 80)}",
                body=f"总调度 Agent 执行失败：{error}",
                project_id="workbench",
                kind="agent_dispatch",
                level="error",
                href="/",
                event_key=f"agent-dispatch:{run['id']}",
                dedupe_seconds=0,
            )
        except Exception:
            log.debug("忽略异常（dispatch_agent_task）", exc_info=True)
        raise HTTPException(502, f"总调度 Agent 调用失败：{exc}") from exc

def capability_graph_route(message: str, children: list[str] | None = None) -> list[str]:
    """Route by declared tools, freshness and current load, not keywords alone."""
    candidates = children or available_child_agents()
    lowered = str(message or "").lower()
    scored: list[tuple[float, str]] = []
    for project_id in candidates:
        if project_id not in AGENT_REGISTRY or project_id == "workbench":
            continue
        hints = AGENT_ROUTE_HINTS.get(project_id, ())
        playbook = AGENT_PLAYBOOKS.get(project_id, {})
        score = sum(3.0 for hint in hints if hint.lower() in lowered)
        score += sum(0.35 for term in str(playbook.get("mission", "")).lower().split() if term and term in lowered)
        detail = agent_detail(project_id, llm_ready=bool(llm_settings()["configured"]))
        tools = set(detail.get("tools") or detail.get("implemented_tools") or [])
        if any(term in lowered for term in ("最新", "同步", "刷新", "数据时间", "变化")):
            freshness = project_data_freshness(project_id)
            if freshness.get("status") in {"stale", "missing"}:
                score += 2.0
        if tools:
            score += min(1.5, len(tools) * 0.1)
        active = int((agent_run_summary(project_id) or {}).get("active", 0))
        score -= min(1.5, active * 0.25)
        if score > 0:
            scored.append((score, project_id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [project_id for _score, project_id in scored[:3]] or ["inbox"]













@app.get("/api/agent/capability-graph")
def get_capability_graph() -> dict[str, Any]:
    return capability_graph_payload()

@app.get("/api/system/architecture")
def get_system_architecture() -> dict[str, Any]:
    workers = worker_status_payload()
    worker_by_id = {worker["id"]: worker for worker in workers}

    def component_status(worker_id: str) -> str:
        worker = worker_by_id.get(worker_id) or {}
        if worker.get("stale"):
            return "stale"
        return str(worker.get("status") or "unclaimed")

    try:
        crawl4ai_available = importlib.util.find_spec("crawl4ai") is not None
    except (ImportError, ValueError):
        crawl4ai_available = False
    components = [
        {"id": "core-api", "label": "Core API", "status": "online", "scope": "FastAPI + SQLite"},
        {"id": "crawl-worker", "label": "Crawl Worker", "status": component_status("crawl-worker") if crawl4ai_available else "optional", "scope": "网页抓取与证据产物"},
        {"id": "sync-worker", "label": "Sync Worker", "status": component_status("sync-worker"), "scope": "AI 热点、Sub2API、行情自动化"},
        {"id": "monitor-worker", "label": "Monitor Worker", "status": component_status("monitor-worker"), "scope": "服务器巡检与告警"},
        {"id": "agent-worker", "label": "Agent Worker", "status": component_status("agent-worker"), "scope": "计划、重试、交接与 LLM"},
    ]
    return {
        "components": components,
        "workers": workers,
        "isolation": "每类任务拥有独立 Run 和错误边界；状态只以 Worker 心跳/租约为准，不把已注册当成正在运行。",
        "version": WORKBENCH_VERSION,
    }

@app.get("/api/workers")
def get_workers() -> dict[str, Any]:
    import app as _app
    return {"instance_id": _app_call('worker_instance_id'), "workers": _app_call('worker_status_payload'), "lease_seconds": _app.WORKER_LEASE_SECONDS, "policy": "同一 Worker 通过 SQLite 短租约避免多实例重复执行；过期租约可被新实例接管。"}

class WorkerHeartbeatRequest(BaseModel):
    status: str = Field(default="ready", max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)

@app.post("/api/workers/{worker_id}/heartbeat")
def heartbeat_worker(worker_id: str, request: WorkerHeartbeatRequest) -> dict[str, Any]:
    try:
        worker = worker_lease(worker_id, status=request.status.strip() or "ready", metadata=request.metadata)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if worker.get("status") == "held_by_other_instance":
        raise HTTPException(409, f"Worker {worker_id} 当前由其他实例持有")
    return {"ok": True, "worker": worker, "workers": worker_status_payload()}


__all__ = [
    "AGENT_IMPLEMENTATIONS",
    "AGENT_PLAYBOOKS",
    "AGENT_REGISTRY",
    "AGENT_RESULT_CONTRACT_VERSION",
    "AGENT_RESULT_SECTION_ALIASES",
    "AGENT_ROUTE_HINTS",
    "AGENT_RUN_STATUS_LABELS",
    "AGENT_STATUS_LABELS",
    "AGENT_TOOL_POLICIES",
    "AgentDispatchRequest",
    "RUNTIME_TOOL_POLICIES",
    "SUBAGENT_TOOL_MAP",
    "WorkerHeartbeatRequest",
    "_contract_source_coverage",
    "_contract_source_ref",
    "_contract_source_refs",
    "_react_tools",
    "_subagent_extra_tools",
    "add_agent_message",
    "add_agent_run_event",
    "add_conversation",
    "add_log",
    "agent_declared_tools",
    "agent_detail",
    "agent_display_name",
    "agent_result_contract",
    "assert_runtime_tool_policies",
    "available_child_agents",
    "build_agent_execution_plan",
    "call_llm_with_tools",
    "capability_graph_payload",
    "capability_graph_route",
    "capability_route_explanation",
    "create_agent_run_record",
    "create_agent_session",
    "create_work_item_record",
    "dispatch_agent_task",
    "get_agent_session",
    "get_capability_graph",
    "get_system_architecture",
    "get_workers",
    "heartbeat_worker",
    "list_agent_messages",
    "load_projects",
    "project_link_summary",
    "public_project_link",
    "run_agent_react_loop",
    "runtime_tool_policy",
    "subagent_tool_schemas",
    "update_agent_run_record",
    "update_agent_session_summary",
    "update_work_item_record",
    "validate_agent_tool_requests",
]
