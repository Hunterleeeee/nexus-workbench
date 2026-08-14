"""AI 主动学习领域。

拆自 app.py（2026-08-14 第十七批）。包含: 学习画像/今日一课/课程与练习/探索推荐/
练习批改等。仍在 app.py 的领域函数经 _app_call 运行时转发。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

from .core import clip, clip_for_llm, decode_json_value, extract_json_block, log, now_iso
from .automations import automation_rule_row, save_automation_rule
from .db import db_connection
from .knowledge import write_knowledge_note
from .instance import app
from .llm import call_llm, llm_settings
from .notifications import create_notification_record


AI_LEARNING_CURRICULUM: list[dict[str, Any]] = [
    {
        "module": "建立 AI 认知",
        "title": "先画出你的 AI 转型地图",
        "objective": "分清会用 AI、会设计 AI 工作流和能交付 AI 项目的差别。",
        "knowledge": [
            "AI 转型不是记住更多工具名，而是把工作拆成可被模型辅助、可验证、可复用的步骤。",
            "第一层是个人提效，第二层是重做团队流程，第三层才是把能力做成产品或岗位资产。",
            "优先选择高频、耗时、输入输出清楚的任务，它们最容易形成第一个可量化成果。",
        ],
        "case": {"situation": "一位产品经理每周要花 3 小时整理访谈记录。", "approach": "先让模型按固定字段提取痛点、原话和证据，再由人复核并汇总。", "result": "交付物从一份摘要升级为可追溯的需求证据表。", "lesson": "转型价值来自流程重构，不是单次问答写得更快。"},
        "practice": {"task": "列出一个你每周重复至少两次的任务。", "steps": ["写清任务输入", "写清理想输出", "标出必须由你判断的环节"], "deliverable": "一张 3 列的 AI 改造机会卡。"},
        "quiz": {"question": "最适合作为第一个 AI 转型练习的任务是？", "options": ["一年一次且规则模糊的战略会", "高频、耗时且输入输出清楚的任务", "完全不能复核结果的任务", "只为了展示新技术的任务"], "correct_index": 1, "explanation": "高频且边界清楚的任务最容易验证收益，也最容易持续迭代。"},
        "takeaway": "先改造一个真实工作流，再扩展工具栈。",
    },
    {
        "module": "建立 AI 认知",
        "title": "理解大模型真正擅长什么",
        "objective": "用“预测下一个 token”的视角理解模型能力与边界。",
        "knowledge": [
            "大模型擅长从大量语言模式中生成、改写、分类、抽取和推理候选，但不天然保证事实正确。",
            "模型表现取决于上下文质量：它知道什么、要做什么、哪些限制不能越过。",
            "高风险结论必须接数据源、检索或人工复核，流畅不等于可靠。",
        ],
        "case": {"situation": "运营让模型直接生成竞品月活和收入数据。", "approach": "改为提供公开报告链接，只让模型提取带出处的数据并标记缺失。", "result": "答案不再追求填满表格，而是把已知、未知和来源分开。", "lesson": "让模型处理证据，比让模型凭记忆报数可靠。"},
        "practice": {"task": "拿一个你常问 AI 的问题，标出其中的事实、判断和建议。", "steps": ["圈出必须准确的事实", "给事实补来源", "给建议补验收标准"], "deliverable": "一份带风险标记的问题改写。"},
        "quiz": {"question": "为什么大模型语气很肯定时仍可能出错？", "options": ["它只支持英文", "它在生成符合模式的文本，不天然执行事实校验", "它不能读取任何上下文", "它每次都会随机拒答"], "correct_index": 1, "explanation": "语言连贯度和事实可靠性是两件事，需要证据或工具来校验。"},
        "takeaway": "把 AI 当成强大的生成与推理候选器，而不是自动正确的数据库。",
    },
    {
        "module": "高质量协作",
        "title": "用四要素写出可执行提示词",
        "objective": "掌握“背景—目标—约束—输出”的稳定提示结构。",
        "knowledge": [
            "背景告诉模型你在什么场景工作；目标描述这次要完成的任务，而不是泛泛地说‘优化一下’。",
            "约束写明不能编造、篇幅、语气、受众和必须使用的材料。",
            "输出格式决定结果是否能直接进入下一个流程，例如表格、JSON、清单或邮件草稿。",
        ],
        "case": {"situation": "销售只输入‘帮我写跟进邮件’，结果套话很多。", "approach": "补充客户阶段、会议事实、禁用承诺、邮件长度和下一步 CTA。", "result": "邮件可直接复核，且每句话都有业务依据。", "lesson": "好提示词的本质是清楚的任务委托。"},
        "practice": {"task": "把一个模糊请求改写成四要素提示词。", "steps": ["补 2 句背景", "写一个可验收目标", "列 3 条约束", "指定输出格式"], "deliverable": "一条可重复使用的提示词模板。"},
        "quiz": {"question": "提示词中最容易让结果进入后续自动化的部分是？", "options": ["角色称呼", "礼貌用语", "结构化输出格式", "感叹号数量"], "correct_index": 2, "explanation": "稳定的结构化输出更容易被程序、表格或下一个 Agent 继续处理。"},
        "takeaway": "提示词不是咒语，而是一份清楚、可验收的任务说明。",
    },
    {
        "module": "高质量协作",
        "title": "用示例让输出稳定下来",
        "objective": "理解 few-shot 示例如何约束风格、分类和结构。",
        "knowledge": [
            "当规则难以用一句话描述时，给 2–3 个好示例通常比继续堆形容词有效。",
            "示例要覆盖正常情况和一个边界情况，避免模型只学到最表面的格式。",
            "示例中的错误也会被模仿，所以必须先人工校验。",
        ],
        "case": {"situation": "客服工单分类总把‘无法开票’分到支付失败。", "approach": "补充开票、退款、支付失败三类真实脱敏示例，并说明边界。", "result": "分类规则更稳定，人工只处理低置信度项。", "lesson": "示例相当于给模型看一小份现场操作手册。"},
        "practice": {"task": "为你的一个分类或写作任务准备 3 个示例。", "steps": ["选一个典型例", "选一个易混淆例", "写出为什么这样判断"], "deliverable": "可粘贴进提示词的示例区。"},
        "quiz": {"question": "few-shot 示例最重要的质量要求是？", "options": ["数量越多越好", "必须全部很长", "正确且覆盖典型与边界情况", "只能使用模型生成的示例"], "correct_index": 2, "explanation": "少量高质量且覆盖边界的示例，通常比大量重复样本更有效。"},
        "takeaway": "规则说不清时，用经过校验的示例教模型。",
    },
    {
        "module": "可信使用",
        "title": "给 AI 结果加上证据链",
        "objective": "学会区分事实、推断和建议，并保留来源。",
        "knowledge": [
            "事实应能回到原文、数据或系统记录；推断要说明依据；建议要说明适用条件。",
            "要求模型引用来源时，还要检查引用是否真的支持对应结论。",
            "好的 AI 交付不是看起来完整，而是关键结论可以被快速复核。",
        ],
        "case": {"situation": "研究报告列出五个市场趋势，却没有来源时间。", "approach": "为每条结论增加来源链接、发布时间、原文摘录和置信度。", "result": "过期信息和模型推断被明显区分。", "lesson": "证据链让 AI 输出从草稿变成可决策材料。"},
        "practice": {"task": "检查最近一段 AI 输出。", "steps": ["标出 3 个事实句", "为每句补来源", "把无来源内容改成待验证假设"], "deliverable": "一段带证据状态的结论。"},
        "quiz": {"question": "一条引用链接存在，是否就代表结论可靠？", "options": ["是，只要能打开", "是，只要来源知名", "否，还要检查来源时间与原文是否支持结论", "否，因为所有网页都不可信"], "correct_index": 2, "explanation": "引用可能过期、断章取义或与结论无关，必须核对支持关系。"},
        "takeaway": "让每个关键结论都能回答：根据什么、截至何时、哪里能复核。",
    },
    {
        "module": "重做工作流",
        "title": "把复杂任务拆成 AI 工作流",
        "objective": "把一次大提示拆成输入、处理、校验和交付四段。",
        "knowledge": [
            "复杂任务一次性生成，错误会在长链路中隐藏；拆步后每一段都能校验和重试。",
            "先确定每一步的输入输出契约，再决定由人、模型还是普通程序执行。",
            "关键判断点保留人工门槛，重复转换和整理可以自动化。",
        ],
        "case": {"situation": "团队让模型一次生成完整 PRD，内容经常脱离用户证据。", "approach": "拆成反馈提取、问题聚类、证据检查、需求草稿和评审五步。", "result": "每个需求都能回到反馈，缺口在生成前暴露。", "lesson": "工作流质量比单次回答的惊艳程度更重要。"},
        "practice": {"task": "把一个 30 分钟以上的任务拆成 4 步。", "steps": ["写每步输入", "写每步输出", "标出校验点和失败后的回退"], "deliverable": "一条可执行的 AI 工作流。"},
        "quiz": {"question": "复杂 AI 工作流中最应该保留人工确认的是？", "options": ["重复改格式", "高风险且不可逆的关键判断", "复制字段", "统一文件名"], "correct_index": 1, "explanation": "高风险、不可逆或难以自动验证的决策应有人类确认。"},
        "takeaway": "先设计可校验的步骤，再选择模型和工具。",
    },
    {
        "module": "重做工作流",
        "title": "分清自动化、Copilot 和 Agent",
        "objective": "为不同任务选择合适的自主程度。",
        "knowledge": [
            "规则稳定、输入结构化的任务优先普通自动化，不必强行使用大模型。",
            "需要人持续判断的创作和决策任务适合 Copilot；需要多步调用工具并能观察结果的任务才适合 Agent。",
            "自主程度越高，越需要权限边界、审计记录、超时与人工接管。",
        ],
        "case": {"situation": "每天把固定报表转成 PDF，却准备做一个自主 Agent。", "approach": "改用确定性脚本处理格式，只让模型总结异常变化。", "result": "成本更低，结果也更稳定。", "lesson": "不用 AI 的步骤往往也是好 AI 系统的一部分。"},
        "practice": {"task": "给你工作中的 3 个任务选择自动化、Copilot 或 Agent。", "steps": ["判断规则是否稳定", "判断是否要调用多个工具", "判断风险与人工确认点"], "deliverable": "一张任务—模式选择表。"},
        "quiz": {"question": "什么任务最适合普通自动化？", "options": ["规则稳定且输入结构化", "目标经常变化且无法校验", "需要跨系统自主决策", "需要创意讨论"], "correct_index": 0, "explanation": "确定性规则能解决的问题，用普通程序通常更便宜、更快、更可靠。"},
        "takeaway": "选择最低但足够的智能水平，系统会更稳。",
    },
    {
        "module": "知识与数据",
        "title": "用 RAG 让 AI 基于你的资料回答",
        "objective": "理解检索增强生成的基本链路与适用场景。",
        "knowledge": [
            "RAG 先从资料库检索相关片段，再把片段交给模型生成答案。",
            "它能减少凭空回答并提供来源，但检索不到、资料过期或切片不当仍会导致错误。",
            "首版应先做好资料范围、更新时间和引用回放，再追求复杂向量方案。",
        ],
        "case": {"situation": "新人不断询问公司报销规则，通用模型回答不一致。", "approach": "只检索最新制度文档，答案附章节和更新时间，找不到时明确转人工。", "result": "回答边界清楚，制度更新也可追踪。", "lesson": "RAG 的核心是把答案约束在可管理的知识范围内。"},
        "practice": {"task": "挑一个适合做个人知识助手的资料集合。", "steps": ["限定资料范围", "定义更新频率", "写出回答必须附带的引用字段"], "deliverable": "一份最小 RAG 数据清单。"},
        "quiz": {"question": "RAG 最直接解决的是什么问题？", "options": ["让模型永远不会出错", "让模型基于指定资料并能回溯来源", "自动训练一个新基础模型", "替代所有数据库"], "correct_index": 1, "explanation": "RAG 提供受控上下文和来源，不等于消除所有错误。"},
        "takeaway": "知识助手先管好资料与引用，再谈更复杂的模型。",
    },
    {
        "module": "知识与数据",
        "title": "让非结构化材料变成可用数据",
        "objective": "掌握 AI 抽取、标准化和人工复核的组合方式。",
        "knowledge": [
            "会议纪要、邮件、合同和聊天记录可以先抽取成固定字段，再进入统计或工作流。",
            "字段要定义类型、是否必填、允许值和缺失处理，不能只给一张空表。",
            "高价值字段用抽样准确率评估，低置信度进入人工队列。",
        ],
        "case": {"situation": "采购团队手工从报价单抄供应商、单价和交期。", "approach": "模型输出固定 JSON，程序校验金额和日期，异常行交给人复核。", "result": "大部分录入自动完成，同时保留原文件定位。", "lesson": "模型负责理解文本，程序负责确定性校验。"},
        "practice": {"task": "为一类常见材料设计 5 个抽取字段。", "steps": ["定义字段类型", "写缺失值规则", "选 2 个必须人工复核的字段"], "deliverable": "一个最小结构化数据契约。"},
        "quiz": {"question": "模型抽取金额后，最稳妥的下一步是？", "options": ["直接付款", "用程序校验格式和范围，异常转人工", "让模型再说一次", "删掉原文件"], "correct_index": 1, "explanation": "确定性校验和原文回溯能显著降低抽取错误的业务风险。"},
        "takeaway": "用模型理解材料，用规则校验关键字段。",
    },
    {
        "module": "构建与评估",
        "title": "第一次调用 AI API 要关注什么",
        "objective": "理解模型、消息、令牌、超时和错误处理这五个基础概念。",
        "knowledge": [
            "API 调用至少包含模型名、消息和输出上限；密钥只放在服务端安全配置中。",
            "生产调用必须设置超时、重试和失败降级，不能假设上游永远可用。",
            "记录延迟、token、错误类型和任务结果，才能判断成本与质量。",
        ],
        "case": {"situation": "内部摘要工具偶尔卡住，页面一直显示加载中。", "approach": "增加 30 秒超时、可重试错误分类和一份规则摘要降级。", "result": "上游故障时用户仍能完成任务，并看到明确原因。", "lesson": "可用的 AI 产品必须把失败当成正常分支。"},
        "practice": {"task": "为一个 AI API 功能写失败处理清单。", "steps": ["列出超时和限流", "设计重试次数", "写无模型时的降级输出"], "deliverable": "一份 API 调用验收清单。"},
        "quiz": {"question": "API Key 最不应该放在哪里？", "options": ["服务端环境变量", "权限受控的密钥服务", "公开前端 JavaScript", "仅服务端可读配置"], "correct_index": 2, "explanation": "前端代码会被浏览器用户读取，不能保存秘密密钥。"},
        "takeaway": "先设计失败、成本和观测，再把 AI 调用接进业务。",
    },
    {
        "module": "构建与评估",
        "title": "用小型评测集判断 AI 是否真的变好",
        "objective": "从‘感觉不错’升级为可重复的质量评估。",
        "knowledge": [
            "评测集应来自真实任务，覆盖常见输入、难例和不能犯的错误。",
            "评价维度可以包括事实正确、格式合规、完整性、风险和人工修改时间。",
            "每次改提示词、模型或资料库都跑同一批样本，才能比较版本。",
        ],
        "case": {"situation": "团队争论两个模型谁更适合写商品标题。", "approach": "选 30 个真实商品，用同一规则盲评准确、合规和修改时间。", "result": "最终选择不是最会写的模型，而是综合成本最低的方案。", "lesson": "评测目标应服务业务结果，而不是模型排行榜。"},
        "practice": {"task": "为你的 AI 场景准备 5 条最小评测样本。", "steps": ["选 3 条常见输入", "选 1 条边界输入", "选 1 条高风险输入"], "deliverable": "一份带通过标准的小型评测集。"},
        "quiz": {"question": "为什么修改提示词后要重复跑同一评测集？", "options": ["让输出更长", "比较版本并发现回退", "增加 token 消耗", "避免保存结果"], "correct_index": 1, "explanation": "固定样本提供可比基线，能看到改进和意外退化。"},
        "takeaway": "没有评测集，就很难证明 AI 系统真的进步。",
    },
    {
        "module": "安全与治理",
        "title": "建立 AI 使用的数据边界",
        "objective": "识别敏感数据、外部发送和不可逆动作的风险。",
        "knowledge": [
            "输入模型前先判断是否含个人信息、商业秘密、凭证或受监管数据。",
            "最小化数据、脱敏和限定保留范围，比在提示词里说‘请保密’更有效。",
            "删除、付款、发布和向外部联系人发送内容，应保留明确人工确认。",
        ],
        "case": {"situation": "HR 想把完整候选人简历上传到未知在线工具做筛选。", "approach": "先评估供应商条款，去掉联系方式，用编号关联，并禁止模型自动拒绝候选人。", "result": "效率提升没有越过隐私和公平性边界。", "lesson": "AI 治理要落在数据流和动作权限上。"},
        "practice": {"task": "给一个 AI 工作流做数据与动作风险检查。", "steps": ["标出敏感字段", "写脱敏方式", "圈出必须人工确认的动作"], "deliverable": "一张 AI 安全边界卡。"},
        "quiz": {"question": "下列哪个动作最需要人工确认？", "options": ["把日期统一格式", "给内部草稿分段", "向客户自动发送承诺邮件", "统计词频"], "correct_index": 2, "explanation": "外部发送且包含承诺，影响真实关系并难以撤回，必须保留确认。"},
        "takeaway": "把敏感数据和高风险动作挡在明确边界之外。",
    },
    {
        "module": "转型落地",
        "title": "用 ROI 选择值得做的 AI 项目",
        "objective": "从节省时间、质量改善和风险成本评估项目价值。",
        "knowledge": [
            "价值不只有节省工时，也包括响应速度、错误率、转化率和知识复用。",
            "成本要计算模型费用、集成维护、人工复核和失败处理，不只看 API 单价。",
            "首个项目应在 2–4 周内产生可观察指标，并允许快速停止。",
        ],
        "case": {"situation": "团队计划建设一个覆盖所有部门的 AI 平台。", "approach": "先选客服知识问答，在一个产品线对比响应时间、解决率和人工接管率。", "result": "用小范围指标决定是否扩张，而不是先投入大平台。", "lesson": "最小可测的业务闭环比宏大愿景更能推动转型。"},
        "practice": {"task": "为你的第一个 AI 项目写一张 ROI 假设。", "steps": ["选 1 个主指标", "估算当前基线", "列出全成本", "写停止条件"], "deliverable": "一页项目价值假设。"},
        "quiz": {"question": "评估 AI 项目成本时最容易漏掉的是？", "options": ["模型名称", "人工复核和维护成本", "按钮颜色", "项目口号"], "correct_index": 1, "explanation": "人工复核、集成和维护往往比单次 token 费用更影响总成本。"},
        "takeaway": "用可测指标启动，用全成本判断是否扩张。",
    },
    {
        "module": "转型落地",
        "title": "把学习变成可展示的作品证据",
        "objective": "设计一个能证明你会发现问题、构建方案和评估结果的小项目。",
        "knowledge": [
            "作品集要展示问题、用户、原流程、AI 方案、评测方法、结果和反思。",
            "一个真实可运行的小工作流，比十张工具证书更能证明转型能力。",
            "公开展示前要清除公司数据和敏感信息，并明确哪些结果来自模拟样本。",
        ],
        "case": {"situation": "求职者简历只写‘熟练使用多种 AI 工具’。", "approach": "改成展示一个客服工单分类助手：数据契约、提示词、评测集、失败案例和改进记录。", "result": "能力从自我描述变成面试官可检查的证据。", "lesson": "转型最终要落到可验证的交付物。"},
        "practice": {"task": "确定一个 7 天内能完成的 AI 作品。", "steps": ["选真实问题", "限定最小输入输出", "准备 5 条评测样本", "定义演示方式"], "deliverable": "一份 7 天作品计划。"},
        "quiz": {"question": "最能证明 AI 转型能力的作品内容是？", "options": ["工具名称列表", "只有漂亮截图", "问题、工作流、评测和反思的完整证据", "模型生成的宣传文案"], "correct_index": 2, "explanation": "完整证据能证明你不仅会调用工具，还会设计、验证和改进系统。"},
        "takeaway": "把每周学习收束成一个能运行、能评测、能讲清楚的作品。",
    },
]


AI_LEARNING_PHASES = [
    {"id": "foundation", "title": "建立认知", "description": "看懂模型能力、边界与转型机会", "days": "第 1–2 天"},
    {"id": "collaboration", "title": "高质量协作", "description": "提示、示例、证据与可靠输出", "days": "第 3–5 天"},
    {"id": "workflow", "title": "重做工作流", "description": "任务拆解、Agent、知识与数据", "days": "第 6–9 天"},
    {"id": "delivery", "title": "构建并交付", "description": "API、评测、安全、ROI 与作品", "days": "第 10–14 天"},
]


class AILearningProfileRequest(BaseModel):
    current_role: str = Field(default="", max_length=120)
    target_role: str = Field(default="", max_length=120)
    experience: str = Field(default="beginner", pattern="^(beginner|practical|technical)$")
    focus: str = Field(default="work-efficiency", pattern="^(work-efficiency|product|technical|business|management)$")
    goal: str = Field(default="", max_length=1_000)
    daily_minutes: int = Field(default=25, ge=10, le=120)
    push_time: str = Field(default="08:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    daily_push_enabled: bool = True


class AILearningGenerateRequest(BaseModel):
    refresh: bool = False


class AILearningCompleteRequest(BaseModel):
    quiz_answer: int = Field(ge=0, le=3)
    practice_output: str = Field(default="", max_length=8_000)
    reflection: str = Field(default="", max_length=4_000)
    confidence: int = Field(default=3, ge=1, le=5)


class AILearningProgressRequest(BaseModel):
    practice_output: str = Field(default="", max_length=8_000)
    reflection: str = Field(default="", max_length=4_000)
    confidence: int = Field(default=3, ge=1, le=5)


EMBODIED_PHASES = [
    {"id": "perception", "title": "感知与表征", "description": "机器人怎么把世界变成可计算的状态", "days": "第 1–2 课"},
    {"id": "control", "title": "控制与策略", "description": "从状态到动作，以及它为什么比预测一句话难", "days": "第 3–4 课"},
    {"id": "learning", "title": "学习范式", "description": "模仿学习、强化学习与 VLA 大模型", "days": "第 5–6 课"},
    {"id": "reality", "title": "落到真机", "description": "仿真到现实的鸿沟、评测与安全", "days": "第 7–8 课"},
]

# 具身智能课程。与 AI 转型课程共用同一套课程/自测/练习/批改机制，
# 因此每节课的字段结构必须与 AI_LEARNING_CURRICULUM 完全一致。
EMBODIED_CURRICULUM: list[dict[str, Any]] = [
    {
        "module": "感知与表征",
        "title": "具身智能到底比聊天模型多了什么",
        "objective": "说清「有身体」带来的三个新约束：实时、闭环、不可撤销。",
        "knowledge": [
            "语言模型输出错了可以重说一遍；机械臂把杯子打翻没有撤销键——动作不可逆，所以安全必须前置，而不是事后过滤。",
            "机器人在闭环里工作：它的动作会改变下一步观察到的世界，误差会累积（compounding error）；而聊天是开环的单轮生成。",
            "控制频率是硬约束。50Hz 的控制回路只有 20 毫秒走完感知到动作，这排除了大量「想清楚再说」的做法，于是普遍采用慢思考（规划）加快执行（控制器）的分层。",
        ],
        "case": {
            "situation": "团队把一个多模态大模型直接接上机械臂，让它每一步都输出关节角度。",
            "approach": "改成分层：大模型只输出「把红色杯子放到托盘上」这类子目标，底层由传统控制器高频闭环执行。",
            "result": "推理延迟从阻塞控制回路变成只在子目标切换时发生，抓取成功率和安全性同时上升。",
            "lesson": "把「要做什么」和「怎么动」分开，是具身系统的基本结构。",
        },
        "practice": {
            "task": "挑一个你熟悉的物理任务（哪怕是家里的扫地机），拆出感知—决策—执行三层。",
            "steps": ["写出每一层的输入和输出", "标出哪一层有实时性要求（多少 Hz）", "标出哪个动作是不可逆的"],
            "deliverable": "一张三层分解表，含频率要求与不可逆动作标记。",
        },
        "quiz": {
            "question": "为什么具身智能不能简单照搬「让大模型每步都输出动作」的做法？",
            "options": [
                "因为大模型不支持中文指令",
                "因为控制回路有毫秒级实时约束，且动作不可逆、误差会在闭环中累积",
                "因为机器人没有摄像头",
                "因为强化学习已经解决了所有问题",
            ],
            "correct_index": 1,
            "explanation": "实时性、不可逆性和闭环误差累积，是有身体带来的三个新约束，它们共同决定了分层结构。",
        },
        "takeaway": "先问这个任务的控制频率和不可逆动作，再谈用什么模型。",
    },
    {
        "module": "感知与表征",
        "title": "状态表征：机器人眼里的世界长什么样",
        "objective": "理解为什么表征选择决定了后面所有方法的上限。",
        "knowledge": [
            "同一个任务可以用关节角度、物体位姿、点云或原始像素来表示状态；选择不同，需要的数据量和泛化能力天差地别。",
            "低维结构化表征（比如物体位姿）样本效率高，但需要可靠的感知前端；端到端像素输入不需要标注，但要海量数据。",
            "常见折中是用预训练视觉编码器把图像压成特征，再在特征上学策略——既不用手工标物体，也不用从像素从头学。",
        ],
        "case": {
            "situation": "一个抓取策略在实验室桌面训练得很好，换到另一张桌子就失效。",
            "approach": "排查发现策略把桌面纹理当成了位置线索。改为输入物体相对夹爪的位姿，而不是整幅图像。",
            "result": "换桌面、换光照后仍然可用。",
            "lesson": "策略学到的是你喂给它的表征里的相关性，不是你以为的因果。",
        },
        "practice": {
            "task": "为上一课拆出的任务设计两种状态表征方案。",
            "steps": ["方案 A 用结构化低维量", "方案 B 用原始传感器输入", "各写一条它最可能学到的错误相关性"],
            "deliverable": "两种表征方案的对比，含各自的失效假设。",
        },
        "quiz": {
            "question": "端到端像素输入相比结构化位姿输入，主要代价是什么？",
            "options": [
                "完全无法训练",
                "需要多得多的数据，且更容易学到与任务无关的表面相关性",
                "只能用于仿真环境",
                "推理速度一定更快",
            ],
            "correct_index": 1,
            "explanation": "省掉了感知前端的工程量，代价转移到数据量和泛化风险上。",
        },
        "takeaway": "表征选择是在工程量和数据量之间做交换。",
    },
    {
        "module": "控制与策略",
        "title": "为什么控制比你想的难",
        "objective": "理解连续动作空间、信用分配和分布偏移这三个坑。",
        "knowledge": [
            "动作是连续的高维向量且有物理约束（力矩上限、关节限位），不能像分类那样枚举候选。",
            "任务往往是长时序的：抓取失败发生在第 3 步，惩罚可能到第 30 步才出现，这就是信用分配问题。",
            "训练时策略看到的是专家轨迹上的状态，执行时一旦偏离就进入没见过的状态，误差自我放大——这是模仿学习的分布偏移问题。",
        ],
        "case": {
            "situation": "一个用人类遥操作数据训练的策略，前几步很稳，之后越来越歪直到失败。",
            "approach": "引入 DAgger 式的数据聚合：让策略自己跑，在它跑歪的状态上补专家标注。",
            "result": "策略在偏离状态下也知道怎么修正，长时序成功率明显提升。",
            "lesson": "只在完美轨迹上训练，就学不会从错误中恢复。",
        },
        "practice": {
            "task": "描述你的任务里一次「小偏差滚成大失败」的场景。",
            "steps": ["写出偏差发生的那一步", "写出不纠正会怎样累积", "设计一条能覆盖该状态的数据采集方式"],
            "deliverable": "一份分布偏移分析，含数据补采方案。",
        },
        "quiz": {
            "question": "模仿学习中的分布偏移指的是什么？",
            "options": [
                "训练数据里的图像分辨率和测试时不同",
                "策略执行时偏离专家轨迹，进入训练中没见过的状态，误差自我放大",
                "机器人电池电压不稳定",
                "奖励函数写错了",
            ],
            "correct_index": 1,
            "explanation": "训练分布由专家决定，测试分布由策略自己决定，两者不一致且会自我强化。",
        },
        "takeaway": "要让策略学会跑歪之后怎么回来，就必须让它见过跑歪的状态。",
    },
    {
        "module": "控制与策略",
        "title": "分层：慢思考配快执行",
        "objective": "能为一个任务划出合理的层级边界。",
        "knowledge": [
            "典型三层：任务规划（秒级，大模型擅长）、运动规划与技能选择（百毫秒级）、底层控制（毫秒级，传统控制理论仍然最可靠）。",
            "层级边界应该切在接口稳定的地方：上层给下层的指令语义要清晰且可验证，比如「移动到位姿 X」而不是「往那边一点」。",
            "分层的代价是上层可能给出下层做不到的指令，所以下层必须能拒绝并上报，而不是硬执行。",
        ],
        "case": {
            "situation": "上层规划器要求机械臂穿过一个它够不到的位置。",
            "approach": "底层增加可行性检查，返回不可达并附上最近可行位姿，让上层重新规划。",
            "result": "从撞上去或卡死，变成一次重规划。",
            "lesson": "分层系统里，向上反馈失败原因和向下发指令同样重要。",
        },
        "practice": {
            "task": "给你的任务画一张分层图。",
            "steps": ["定义每层的时间尺度", "写出层间接口的指令格式", "写出下层拒绝指令时上报什么"],
            "deliverable": "一张含接口定义和失败上报机制的分层图。",
        },
        "quiz": {
            "question": "分层控制中，层间接口应该切在哪里？",
            "options": [
                "切在代码文件最多的地方",
                "切在指令语义清晰、且下层能判断可行性的地方",
                "任意位置，只要能跑通",
                "必须让上层直接输出关节力矩",
            ],
            "correct_index": 1,
            "explanation": "接口的价值在于上层不必关心执行细节，而下层能判断这条指令做不做得到。",
        },
        "takeaway": "好的层级边界让上层能被替换，下层能说不。",
    },
    {
        "module": "学习范式",
        "title": "模仿学习：从演示里学技能",
        "objective": "分清行为克隆、DAgger 和逆强化学习各自解决什么。",
        "knowledge": [
            "行为克隆最简单：把（状态, 动作）当监督学习。数据便宜时很有效，但直接暴露在分布偏移问题下。",
            "DAgger 让策略自己执行、专家在它到达的新状态上补标注，用交互成本换鲁棒性。",
            "逆强化学习不学动作，而是从演示里反推奖励函数；好处是能泛化到新场景，代价是计算量大且奖励不唯一。",
        ],
        "case": {
            "situation": "只有 50 条遥操作演示，行为克隆效果不稳。",
            "approach": "改为动作分块（一次预测未来一小段动作序列）并做时序集成，减少高频抖动和累积误差。",
            "result": "同样的数据量下成功率显著提升。",
            "lesson": "数据受限时，改动作表示往往比堆数据更有效。",
        },
        "practice": {
            "task": "为你的任务估算一次演示的采集成本。",
            "steps": ["写出采一条演示要多久", "估算行为克隆大概需要多少条", "判断该选行为克隆还是 DAgger"],
            "deliverable": "一份数据预算与方法选择说明。",
        },
        "quiz": {
            "question": "DAgger 相比朴素行为克隆的核心改进是什么？",
            "options": [
                "使用了更大的神经网络",
                "在策略自己到达的状态上追加专家标注，直面分布偏移",
                "不再需要任何演示数据",
                "把连续动作离散化",
            ],
            "correct_index": 1,
            "explanation": "它把训练分布逐步拉向策略自己的执行分布。",
        },
        "takeaway": "数据在哪些状态上采集，比采了多少条更关键。",
    },
    {
        "module": "学习范式",
        "title": "VLA 与通用机器人策略",
        "objective": "理解视觉-语言-动作模型带来了什么、还没解决什么。",
        "knowledge": [
            "VLA（Vision-Language-Action）把预训练视觉语言模型的常识迁移到机器人上，让「把桌上的橘子放进碗里」这类自然语言指令可执行。",
            "它的优势是语义泛化：没见过的物体名称也可能奏效，因为语言侧见过。但物理泛化（新的力学、新的接触）仍然很弱。",
            "真机数据远比网络图文稀缺，所以跨本体的数据共享和统一动作空间是当前主要方向，也是主要难点。",
        ],
        "case": {
            "situation": "一个 VLA 模型能听懂「拿起那个蓝色的东西」，但换一种夹爪就完全失效。",
            "approach": "把动作输出改为与本体无关的末端位姿增量，再由各本体自己的控制器转换。",
            "result": "同一策略可在两种夹爪上使用，语义能力得以保留。",
            "lesson": "语义泛化和本体泛化是两个独立问题，要分别设计。",
        },
        "practice": {
            "task": "判断你的任务更依赖语义泛化还是物理泛化。",
            "steps": ["列出任务中会变化的因素", "分类为语义变化还是物理变化", "据此判断 VLA 是否合适"],
            "deliverable": "一份变化因素分类表与方法适配结论。",
        },
        "quiz": {
            "question": "VLA 模型目前最不擅长的是哪一类泛化？",
            "options": [
                "没见过的物体名称",
                "换一种说法的自然语言指令",
                "新的接触力学与本体差异",
                "识别常见家居物品",
            ],
            "correct_index": 2,
            "explanation": "语言和视觉的先验来自海量网络数据，物理交互的先验没有对应的大规模来源。",
        },
        "takeaway": "VLA 买来的是语义常识，物理能力仍要靠真机数据和结构设计。",
    },
    {
        "module": "落到真机",
        "title": "Sim2Real：仿真里能跑不等于真机能用",
        "objective": "能列出现实差距的具体来源并给出应对手段。",
        "knowledge": [
            "差距主要有三类：动力学不准（摩擦、接触、延迟）、感知不同（噪声、光照、标定误差）、以及仿真里根本没建模的现象。",
            "域随机化是主流手段：在仿真中随机化质量、摩擦、光照、延迟，逼策略学习对参数不敏感的行为。",
            "系统辨识是另一条路：先测量真机参数把仿真调准。两者常组合使用——先辨识缩小范围，再随机化覆盖残差。",
        ],
        "case": {
            "situation": "仿真中 98% 成功的策略，上真机只有 30%。",
            "approach": "测量真机控制延迟并加入仿真，同时随机化摩擦系数范围。",
            "result": "真机成功率提升到可用区间，换一批物料后也不需重训。",
            "lesson": "先找出最大的那一项差距，而不是笼统地多加点随机。",
        },
        "practice": {
            "task": "为你的任务列出三条最可能的 sim2real 差距。",
            "steps": ["每条写清它如何影响结果", "标注该辨识还是该随机化", "排出优先级"],
            "deliverable": "一份带优先级的现实差距清单。",
        },
        "quiz": {
            "question": "域随机化的作用机理是什么？",
            "options": [
                "让仿真运行得更快",
                "在训练中暴露参数变化，逼策略学习对这些参数不敏感的行为",
                "自动修正真机的机械误差",
                "替代所有真机测试",
            ],
            "correct_index": 1,
            "explanation": "它把「真实参数未知」变成「训练分布已覆盖」，代价是策略趋于保守。",
        },
        "takeaway": "先量出差距在哪，再决定是调准仿真还是拓宽分布。",
    },
    {
        "module": "落到真机",
        "title": "评测与安全：怎么判断它真的可用",
        "objective": "建立一套不靠「演示视频看起来不错」的验收标准。",
        "knowledge": [
            "成功率必须带条件说明：多少次试验、哪些初始条件、失败模式如何分布。单一数字没有意义。",
            "要区分任务失败和不安全失败。打不开瓶盖是前者，甩飞物体是后者，后者的容忍度应该是零。",
            "安全不能只靠策略学会：力矩限幅、工作空间围栏、急停这些硬约束必须在策略之外独立生效。",
        ],
        "case": {
            "situation": "汇报里写抓取成功率 90%，落地后频繁出问题。",
            "approach": "改为报告 50 次试验、5 种初始位姿，并单列失败模式；碰撞类失败单独统计。",
            "result": "发现 90% 是在单一初始位姿下测的，换位姿后降到 60%，且有 2 次碰撞。",
            "lesson": "不分条件的成功率会掩盖最该关注的失败。",
        },
        "practice": {
            "task": "为你的任务写一份验收标准。",
            "steps": ["定义试验次数与初始条件覆盖", "列出失败模式分类", "写出哪些失败零容忍，用什么硬约束兜底"],
            "deliverable": "一份可执行的验收标准，含安全兜底措施。",
        },
        "quiz": {
            "question": "为什么单独报告一个成功率数字不够？",
            "options": [
                "因为百分比不好理解",
                "因为它掩盖了试验条件覆盖度和失败模式的严重性差异",
                "因为成功率总是被高估",
                "因为应该只看仿真结果",
            ],
            "correct_index": 1,
            "explanation": "同样是 90%，在单一初始位姿下和在多样化条件下含义完全不同；碰撞类失败也不能和普通失败等价计数。",
        },
        "takeaway": "验收标准要写清条件、失败分类和零容忍项，否则数字不可信。",
    },
]


LEARNING_TRACKS: dict[str, dict[str, Any]] = {
    "ai-transformation": {
        "id": "ai-transformation",
        "title": "AI 转型学习",
        "subtitle": "把真实工作改造成可被模型辅助、可验证、可复用的流程",
        "curriculum": AI_LEARNING_CURRICULUM,
        "phases": AI_LEARNING_PHASES,
        "note_tag": "AI 转型",
    },
    "embodied": {
        "id": "embodied",
        "title": "具身智能学习",
        "subtitle": "从感知表征到真机落地，理解有身体的智能为什么不同",
        "curriculum": EMBODIED_CURRICULUM,
        "phases": EMBODIED_PHASES,
        "note_tag": "具身智能",
    },
}

DEFAULT_LEARNING_TRACK = "ai-transformation"
def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def learning_track(track: str = "") -> dict[str, Any]:
    """按 id 取学习轨道；未知 id 一律回落到默认轨道而不是报错。"""
    return LEARNING_TRACKS.get(str(track or "").strip() or DEFAULT_LEARNING_TRACK, LEARNING_TRACKS[DEFAULT_LEARNING_TRACK])


def learning_track_id(track: str = "") -> str:
    return str(_app_call('learning_track', track)["id"])


def ai_learning_today() -> str:
    return datetime.now().astimezone().date().isoformat()


def ai_learning_profile_row(row: sqlite3.Row) -> dict[str, Any]:
    profile = dict(row)
    profile["daily_push_enabled"] = bool(profile.get("daily_push_enabled"))
    return profile


def get_ai_learning_profile(track: str = "") -> dict[str, Any]:
    """每个学习轨道有独立的学习档案（目标岗位、每日时长、推送时间都可能不同）。"""
    track_id = _app_call('learning_track_id', track)
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM ai_learning_profiles WHERE track = ?", (track_id,)).fetchone()
        if not row:
            timestamp = now_iso()
            connection.execute(
                """INSERT INTO ai_learning_profiles
                (track, current_role, target_role, experience, focus, goal, daily_minutes, push_time, daily_push_enabled, created_at, updated_at)
                VALUES (?, '', '', 'beginner', 'work-efficiency', '', 25, '08:30', 1, ?, ?)""",
                (track_id, timestamp, timestamp),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM ai_learning_profiles WHERE track = ?", (track_id,)).fetchone()
        return _app_call('ai_learning_profile_row', row)
    finally:
        connection.close()


def save_ai_learning_profile(request: AILearningProfileRequest, track: str = "") -> dict[str, Any]:
    track_id = _app_call('learning_track_id', track)
    timestamp = now_iso()
    _app_call('get_ai_learning_profile', track_id)  # 保证该轨道的档案行存在
    connection = db_connection()
    try:
        connection.execute(
            """UPDATE ai_learning_profiles SET current_role = ?, target_role = ?, experience = ?, focus = ?,
            goal = ?, daily_minutes = ?, push_time = ?, daily_push_enabled = ?, updated_at = ?
            WHERE track = ?""",
            (
                request.current_role.strip(), request.target_role.strip(), request.experience, request.focus,
                request.goal.strip(), request.daily_minutes, request.push_time, int(request.daily_push_enabled),
                timestamp, track_id,
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM ai_learning_profiles WHERE track = ?", (track_id,)).fetchone()
        return _app_call('ai_learning_profile_row', row)
    finally:
        connection.close()


def ai_learning_lesson_row(row: sqlite3.Row) -> dict[str, Any]:
    lesson = dict(row)
    lesson["content"] = decode_json_value(lesson.pop("content_json", "{}"), {}) or {}
    lesson["quiz_correct"] = bool(lesson.get("quiz_correct"))
    lesson["completed"] = lesson.get("status") == "completed"
    lesson["feedback"] = decode_json_value(lesson.pop("feedback_json", "{}"), {}) or {}
    return lesson


def get_ai_learning_lesson(lesson_id: int = 0, lesson_date: str = "", track: str = "") -> dict[str, Any] | None:
    connection = db_connection()
    try:
        if lesson_id:
            # id 全局唯一，按 id 取时不需要再限定轨道。
            row = connection.execute("SELECT * FROM ai_learning_lessons WHERE id = ?", (lesson_id,)).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM ai_learning_lessons WHERE track = ? AND lesson_date = ?",
                (_app_call('learning_track_id', track), lesson_date or _app_call('ai_learning_today', )),
            ).fetchone()
        return _app_call('ai_learning_lesson_row', row) if row else None
    finally:
        connection.close()


def list_ai_learning_lessons(limit: int = 30, track: str = "") -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM ai_learning_lessons WHERE track = ? ORDER BY lesson_date DESC, id DESC LIMIT ?",
            (_app_call('learning_track_id', track), max(1, min(120, limit))),
        ).fetchall()
        return [_app_call('ai_learning_lesson_row', row) for row in rows]
    finally:
        connection.close()


def fallback_ai_learning_content(day_index: int, profile: dict[str, Any], track: str = "") -> dict[str, Any]:
    curriculum = _app_call('learning_track', track)["curriculum"]
    template = json.loads(json.dumps(curriculum[(max(1, day_index) - 1) % len(curriculum)], ensure_ascii=False))
    template["personalization"] = {
        "current_role": profile.get("current_role") or "当前工作",
        "target_role": profile.get("target_role") or "AI 相关岗位",
        "focus": profile.get("focus") or "work-efficiency",
        "daily_minutes": int(profile.get("daily_minutes") or 25),
    }
    return template


def parse_llm_json_object(answer: str) -> dict[str, Any] | None:
    """从模型回复里抠出一个 JSON 对象。

    模型时不时会把 JSON 包在 ```json 里，或者在前面加一句「好的，以下是」。
    这个提取逻辑本来只长在 parse_ai_learning_content 里，新增的几个接口
    如果各写一份，迟早会在某一处漏掉围栏这种情况。
    """
    candidate = str(answer or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    json_text = fenced.group(1) if fenced else candidate
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            json_text = candidate[start : end + 1]
    try:
        raw = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return None
    return raw if isinstance(raw, dict) else None


def parse_ai_learning_content(answer: str, fallback: dict[str, Any]) -> dict[str, Any]:
    candidate = str(answer or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    json_text = fenced.group(1) if fenced else candidate
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            json_text = candidate[start : end + 1]
    try:
        raw = json.loads(json_text)
        raw = raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return fallback
    content = dict(fallback)
    for key, limit in (("module", 80), ("title", 160), ("objective", 500), ("takeaway", 500)):
        if str(raw.get(key) or "").strip():
            content[key] = clip(str(raw[key]).strip(), limit)
    knowledge = raw.get("knowledge")
    if isinstance(knowledge, list):
        normalized = [clip(str(item).strip(), 600) for item in knowledge[:4] if str(item).strip()]
        if len(normalized) >= 2:
            content["knowledge"] = normalized
    for key, fields in {
        # answer 是新增的一格：案例只讲「他怎么做的」，读的人无从判断自己想的
        # 对不对；把「这个情境下正确的做法和理由」显式写出来，案例才有答案。
        "case": ("situation", "approach", "result", "lesson", "answer"),
        "practice": ("task", "deliverable"),
    }.items():
        value = raw.get(key)
        if not isinstance(value, dict):
            continue
        normalized = dict(content.get(key) or {})
        for field in fields:
            if str(value.get(field) or "").strip():
                normalized[field] = clip(str(value[field]).strip(), 800)
        if key == "practice" and isinstance(value.get("steps"), list):
            steps = [clip(str(item).strip(), 300) for item in value["steps"][:5] if str(item).strip()]
            if steps:
                normalized["steps"] = steps
        content[key] = normalized
    quiz = raw.get("quiz")
    if isinstance(quiz, dict) and isinstance(quiz.get("options"), list) and len(quiz["options"]) == 4:
        try:
            correct_index = int(quiz.get("correct_index"))
        except (TypeError, ValueError):
            correct_index = -1
        if 0 <= correct_index <= 3 and str(quiz.get("question") or "").strip():
            content["quiz"] = {
                "question": clip(str(quiz["question"]).strip(), 500),
                "options": [clip(str(item).strip(), 300) for item in quiz["options"]],
                "correct_index": correct_index,
                "explanation": clip(str(quiz.get("explanation") or "").strip(), 800),
            }
    return content


async def generate_ai_learning_lesson(*, lesson_date: str = "", force: bool = False, use_llm: bool = True, track: str = "") -> dict[str, Any]:
    track_id = _app_call('learning_track_id', track)
    target_date = lesson_date or _app_call('ai_learning_today', )
    existing = _app_call('get_ai_learning_lesson', lesson_date=target_date, track=track_id)
    if existing and (not force or existing.get("completed")):
        return existing
    profile = _app_call('get_ai_learning_profile', track_id)
    connection = db_connection()
    try:
        if existing:
            day_index = int(existing.get("day_index") or 1)
        else:
            row = connection.execute("SELECT COUNT(*) AS count FROM ai_learning_lessons WHERE track = ?", (track_id,)).fetchone()
            day_index = int(row["count"] or 0) + 1
        recent_rows = connection.execute(
            "SELECT title FROM ai_learning_lessons WHERE track = ? ORDER BY lesson_date DESC LIMIT 14", (track_id,)
        ).fetchall()
        recent_titles = [str(row["title"]) for row in recent_rows]
    finally:
        connection.close()
    fallback = _app_call('fallback_ai_learning_content', day_index, profile, track_id)
    content = fallback
    source = "curriculum"
    generation_warning = ""
    if use_llm and _app_call('llm_settings', ).get("configured"):
        prompt = {
            "current_role": profile.get("current_role") or "未填写",
            "target_role": profile.get("target_role") or "AI 相关岗位",
            "experience": profile.get("experience"),
            "focus": profile.get("focus"),
            "goal": profile.get("goal") or "提升 AI 实战能力",
            "daily_minutes": profile.get("daily_minutes"),
            "day_index": day_index,
            "avoid_titles": recent_titles,
            "fallback_topic": fallback.get("title"),
        }
        try:
            answer = await _app_call('call_llm', 
                [
                    {"role": "system", "content": "你是务实的中文 AI 转型教练。为用户生成一节能在指定时间内完成的小课。只输出 JSON。字段：module, title, objective, knowledge(2-4条), case{situation,approach,result,lesson,answer}, practice{task,steps(2-5条),deliverable}, quiz{question,options(恰好4项),correct_index(0-3),explanation}, takeaway。案例要求：situation 必须具体到能直接判断——写清楚是谁、手上有什么、要交付什么、卡在哪，不要停留在「某公司想用 AI 提效」这种空壳；数字和公司名写成明显的示例，不得冒充真实公司、真实研究或真实收益；answer 写「在这个情境下正确的做法是什么、为什么」，并指出一个常见的错误做法错在哪。"},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                max_tokens=1_800,
                temperature=0.45,
                purpose="ai_learning_lesson",
            )
            personalized = _app_call('parse_ai_learning_content', answer, fallback)
            if personalized != fallback:
                content = personalized
                source = "personalized"
        except Exception as exc:
            generation_warning = f"个性化生成暂时不可用，已使用内置课程：{clip(str(exc), 180)}"
    timestamp = now_iso()
    connection = db_connection()
    try:
        if existing:
            connection.execute(
                """UPDATE ai_learning_lessons SET module = ?, title = ?, content_json = ?, source = ?,
                status = 'ready', quiz_answer = -1, quiz_correct = 0, practice_output = '', reflection = '', confidence = 0,
                started_at = '', completed_at = '', updated_at = ? WHERE id = ?""",
                (content.get("module", ""), content.get("title", "今日课程"), json.dumps(content, ensure_ascii=False), source, timestamp, existing["id"]),
            )
            lesson_id = int(existing["id"])
        else:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO ai_learning_lessons
                (track, lesson_date, day_index, module, title, content_json, source, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)""",
                (track_id, target_date, day_index, content.get("module", ""), content.get("title", "今日课程"), json.dumps(content, ensure_ascii=False), source, timestamp, timestamp),
            )
            lesson_id = int(cursor.lastrowid or 0)
        connection.commit()
        if not lesson_id:
            row = connection.execute("SELECT id FROM ai_learning_lessons WHERE track = ? AND lesson_date = ?", (track_id, target_date)).fetchone()
            lesson_id = int(row["id"])
    finally:
        connection.close()
    lesson = _app_call('get_ai_learning_lesson', lesson_id=lesson_id) or {}
    if generation_warning:
        lesson["generation_warning"] = generation_warning
    return lesson


def ai_learning_stats(lessons: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    lessons = lessons if lessons is not None else _app_call('list_ai_learning_lessons', 120)
    completed = [item for item in lessons if item.get("completed")]
    completed_dates = {datetime.fromisoformat(str(item["lesson_date"])).date() for item in completed if item.get("lesson_date")}
    cursor = datetime.now().astimezone().date()
    if cursor not in completed_dates:
        cursor -= timedelta(days=1)
    streak = 0
    while cursor in completed_dates:
        streak += 1
        cursor -= timedelta(days=1)
    correct = sum(1 for item in completed if item.get("quiz_correct"))
    last_seven = datetime.now().astimezone().date() - timedelta(days=6)
    weekly = sum(1 for item in completed if datetime.fromisoformat(str(item["lesson_date"])).date() >= last_seven)
    return {
        "completed": len(completed),
        "streak": streak,
        "quiz_accuracy": round(correct / len(completed) * 100) if completed else 0,
        "weekly_completed": weekly,
        "weekly_goal": 5,
        "notes": sum(1 for item in lessons if int(item.get("note_artifact_id") or 0) > 0),
    }


def ai_learning_automation_rule() -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute(
            "SELECT * FROM automation_rules WHERE kind = 'ai_learning_daily' AND project_id = 'ai-learning' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return automation_rule_row(row) if row else None
    finally:
        connection.close()


def sync_ai_learning_automation(profile: dict[str, Any]) -> dict[str, Any] | None:
    existing = _app_call('ai_learning_automation_rule', )
    enabled = bool(profile.get("daily_push_enabled"))
    if not existing and not enabled:
        return None
    schedule = f"daily:{profile.get('push_time') or '08:30'}"
    config = {"push_time": profile.get("push_time") or "08:30", "delivery": ["application", "web_push"], "timezone": "local"}
    if existing:
        if existing.get("schedule") == schedule and bool(existing.get("enabled")) == enabled and existing.get("config") == config:
            return existing
        return save_automation_rule(
            name="AI 转型学习 · 每日课程",
            kind="ai_learning_daily",
            project_id="ai-learning",
            schedule=schedule,
            enabled=enabled,
            config=config,
            rule_id=int(existing["id"]),
        )
    return save_automation_rule(
        name="AI 转型学习 · 每日课程",
        kind="ai_learning_daily",
        project_id="ai-learning",
        schedule=schedule,
        enabled=True,
        config=config,
    )


def complete_ai_learning_lesson(lesson_id: int, request: AILearningCompleteRequest) -> dict[str, Any]:
    lesson = _app_call('get_ai_learning_lesson', lesson_id=lesson_id)
    if not lesson:
        raise HTTPException(404, "学习课程不存在")
    practice_output = request.practice_output.strip()
    quiz = lesson.get("content", {}).get("quiz") or {}
    try:
        correct_index = int(quiz.get("correct_index"))
    except (TypeError, ValueError):
        correct_index = -1
    if lesson.get("completed"):
        return {
            "lesson": lesson,
            "quiz": {"correct": bool(lesson.get("quiz_correct")), "correct_index": correct_index, "explanation": quiz.get("explanation", "")},
            "stats": _app_call('ai_learning_stats', ),
        }
    correct = request.quiz_answer == correct_index
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            """UPDATE ai_learning_lessons SET status = 'completed', quiz_answer = ?, quiz_correct = ?,
            practice_output = ?, reflection = ?, confidence = ?, started_at = COALESCE(NULLIF(started_at, ''), ?), completed_at = ?, updated_at = ?
            WHERE id = ?""",
            (request.quiz_answer, int(correct), practice_output, request.reflection.strip(), request.confidence, timestamp, timestamp, timestamp, lesson_id),
        )
        connection.commit()
    finally:
        connection.close()
    updated = _app_call('get_ai_learning_lesson', lesson_id=lesson_id) or lesson
    create_notification_record(
        title=f"今日 AI 学习已完成 · {updated.get('title')}",
        body=f"自测{'答对了' if correct else '已完成'} · 连续学习 {_app_call('ai_learning_stats', ).get('streak', 0)} 天",
        project_id="ai-learning", kind="learning", level="success", href="/projects/ai-learning",
        event_key=f"ai-learning-complete:{lesson_id}", dedupe_seconds=0,
    )
    return {"lesson": updated, "quiz": {"correct": correct, "correct_index": correct_index, "explanation": quiz.get("explanation", "")}, "stats": _app_call('ai_learning_stats', )}


def save_ai_learning_progress(lesson_id: int, request: AILearningProgressRequest) -> dict[str, Any]:
    lesson = _app_call('get_ai_learning_lesson', lesson_id=lesson_id)
    if not lesson:
        raise HTTPException(404, "学习课程不存在")
    if lesson.get("completed"):
        return {"saved": False, "lesson": lesson}
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            """UPDATE ai_learning_lessons SET status = 'in_progress', practice_output = ?, reflection = ?, confidence = ?,
            started_at = COALESCE(NULLIF(started_at, ''), ?), updated_at = ?
            WHERE id = ? AND status != 'completed'""",
            (request.practice_output.strip(), request.reflection.strip(), request.confidence, timestamp, timestamp, lesson_id),
        )
        connection.commit()
    finally:
        connection.close()
    return {"saved": True, "lesson": _app_call('get_ai_learning_lesson', lesson_id=lesson_id) or lesson}


AI_LEARNING_FEEDBACK_SCHEMA = {
    "verdict": "达标 / 基本达标 / 未达标 三选一",
    "score": "0-100 的整数",
    "met": ["做到了哪些点，逐条对应交付物要求"],
    "gaps": ["差在哪，必须具体到这份产出里的原话或缺失项"],
    "rewrite": "把学员产出改写成一份达标版本（保留他自己的业务场景，不要换成通用例子）",
    "misconception": "如果自测答错，指出他选的那个选项背后的具体误解；答对则留空",
    "next_question": "一个能推进他下一步的追问",
}


def _ai_learning_work_context(limit: int = 4) -> str:
    """取几条用户真实的待办，让反馈落在他自己的工作上而不是泛泛而谈。

    这门课的全部前提就是"把你真实的工作 AI 化"。如果批改只对着课程模板讲，
    学员拿到的仍然是通用建议——那和直接问一个通用模型没有区别。
    """
    try:
        rows = [item for item in _app_call('list_work_items', ) if str(item.get("title") or "").strip()]
    except Exception:
        log.debug("读取工作项失败，跳过真实工作上下文", exc_info=True)
        return ""
    picked = [f"- {clip(str(item.get('title')), 60)}（{item.get('source_project') or 'workbench'}）" for item in rows[:limit]]
    return "\n".join(picked)


def _parse_ai_learning_feedback(raw: str) -> dict[str, Any]:
    """把模型返回解析成固定结构；解析失败时降级为纯文本，不丢内容。"""
    parsed = decode_json_value(extract_json_block(raw), {}) or {}
    if not isinstance(parsed, dict) or not parsed.get("verdict"):
        return {"verdict": "已生成", "score": 0, "met": [], "gaps": [], "rewrite": clip(raw.strip(), 4000),
                "misconception": "", "next_question": "", "raw_only": True}
    def as_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [clip(str(item), 400) for item in value if str(item).strip()][:6]
        text = str(value or "").strip()
        return [clip(text, 400)] if text else []
    try:
        score = max(0, min(100, int(parsed.get("score") or 0)))
    except (TypeError, ValueError):
        score = 0
    return {
        "verdict": clip(str(parsed.get("verdict") or "已生成"), 40),
        "score": score,
        "met": as_list(parsed.get("met")),
        "gaps": as_list(parsed.get("gaps")),
        "rewrite": clip(str(parsed.get("rewrite") or ""), 4000),
        "misconception": clip(str(parsed.get("misconception") or ""), 800),
        "next_question": clip(str(parsed.get("next_question") or ""), 400),
        "raw_only": False,
    }


async def generate_ai_learning_feedback(lesson_id: int) -> dict[str, Any]:
    """对学员的练习产出和自测选择做一次有依据的批改。

    此前 practice_output 只写不读：学员写完练习什么反馈都没有，自测也只是按
    correct_index 对答案 + 一段所有人都一样的固定解释。这个函数补上真正的
    学习闭环——按课程自己声明的交付物要求逐条对照，指出差距并给出改写版本。
    """
    lesson = _app_call('get_ai_learning_lesson', lesson_id=lesson_id)
    if not lesson:
        raise HTTPException(404, "学习课程不存在")
    practice_output = str(lesson.get("practice_output") or "").strip()
    answered_exercises = _app_call('list_answered_exercises_for_lesson', lesson_id)
    if not practice_output and not answered_exercises:
        raise HTTPException(400, "先做一道题或写下本节整体产出，AI 才有东西可点评。")

    content = lesson.get("content") or {}
    practice = content.get("practice") or {}
    quiz = content.get("quiz") or {}
    options = list(quiz.get("options") or [])
    answer_index = int(lesson.get("quiz_answer") if lesson.get("quiz_answer") is not None else -1)
    correct_index = int(quiz.get("correct_index", -1))
    chosen = options[answer_index] if 0 <= answer_index < len(options) else ""
    correct = options[correct_index] if 0 <= correct_index < len(options) else ""

    quiz_block = "学员未作答。"
    if chosen:
        quiz_block = (
            f"题目：{quiz.get('question', '')}\n"
            f"学员选择：{chosen}\n"
            f"正确答案：{correct}\n"
            f"结论：{'答对' if answer_index == correct_index else '答错'}"
        )

    exercises_block = ""
    if answered_exercises:
        parts = []
        for index, ex in enumerate(answered_exercises, 1):
            ex_fb = ex.get("feedback") or {}
            ex_score = int(ex.get("score") or -1)
            score_text = f"{ex_score} 分" if ex_score >= 0 else "已评判"
            parts.append(
                f"{index}. 题目：{str(ex.get('question') or '')}\n"
                f"   学员答案：{clip(str(ex.get('user_answer') or ''), 1200)}\n"
                f"   该题评判：{score_text} · {clip(str(ex_fb.get('verdict') or '（无评语）'), 200)}"
            )
        exercises_block = "\n".join(parts)
    else:
        exercises_block = "未作答任何练习题。"

    output_block = practice_output or "（未填写本节整体产出，仅依据练习题作答与自测情况点评）"

    work_context = _app_call('_ai_learning_work_context', )
    work_block = f"\n\n学员当前真实的工作项（请让建议落在这些事情上）：\n{work_context}" if work_context else ""

    messages = [
        {"role": "system", "content": (
            "你是一位严格但有建设性的 AI 转型教练，正在批改一名学员的练习。\n"
            "硬性要求：\n"
            "1. 只依据学员实际写下的内容判断，不要脑补他没写的东西。\n"
            "2. 指出差距时必须引用他产出里的原话或明确指出缺了哪一项，禁止说空泛的'可以更具体'。\n"
            "3. 改写版本要保留他自己的业务场景，不要替换成通用示例。\n"
            "4. 如果他确实写得好，就直接给高分，不要为了显得严格而挑刺。\n"
            f"只返回一个 JSON 对象，字段含义如下：{json.dumps(AI_LEARNING_FEEDBACK_SCHEMA, ensure_ascii=False)}"
        )},
        {"role": "user", "content": (
            f"课程：{lesson.get('title')}（模块：{lesson.get('module')}）\n"
            f"学习目标：{content.get('objective', '')}\n"
            f"练习任务：{practice.get('task', '')}\n"
            f"要求步骤：{'；'.join(str(item) for item in (practice.get('steps') or []))}\n"
            f"交付物标准：{practice.get('deliverable', '')}\n\n"
            f"自测情况：\n{quiz_block}\n\n"
            f"本节整体产出：\n{clip_for_llm(output_block, 6000)}\n\n"
            f"练习题作答（每题已单独评判，本次综合复核）：\n{clip_for_llm(exercises_block, 3000)}\n\n"
            f"学员的复盘：{clip(str(lesson.get('reflection') or '（未填写）'), 1000)}\n"
            f"学员自评信心：{lesson.get('confidence', 0)}/100{work_block}"
        )},
    ]

    raw = await _app_call('call_llm', messages, max_tokens=2000, temperature=0.3, purpose="ai_learning_review")
    feedback = _app_call('_parse_ai_learning_feedback', raw)
    feedback["reviewed_at"] = now_iso()
    feedback["policy"] = "批改依据是课程声明的交付物标准与你写下的原文；模型可能出错，结论请自行复核。"

    connection = db_connection()
    try:
        connection.execute(
            "UPDATE ai_learning_lessons SET feedback_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(feedback, ensure_ascii=False), now_iso(), lesson_id),
        )
        connection.commit()
    finally:
        connection.close()
    return {"feedback": feedback, "lesson": _app_call('get_ai_learning_lesson', lesson_id=lesson_id) or lesson}


def save_ai_learning_note(lesson_id: int) -> dict[str, Any]:
    lesson = _app_call('get_ai_learning_lesson', lesson_id=lesson_id)
    if not lesson:
        raise HTTPException(404, "学习课程不存在")
    if not lesson.get("completed"):
        raise HTTPException(409, "请先完成课程")
    if int(lesson.get("note_artifact_id") or 0):
        artifact = _app_call('get_artifact_record', int(lesson["note_artifact_id"]))
        if artifact:
            return {"ok": True, "created": False, "artifact": artifact, "lesson": lesson}
    content = lesson.get("content") or {}
    case = content.get("case") or {}
    practice = content.get("practice") or {}
    lines = [
        f"# {lesson.get('title')}", "", f"> 学习日期：{lesson.get('lesson_date')} · 第 {lesson.get('day_index')} 课 · {lesson.get('module')}", "",
        "## 今日目标", "", str(content.get("objective") or ""), "", "## 核心知识", "",
        *[f"- {item}" for item in content.get("knowledge", [])], "", "## 工作案例", "",
        f"- 场景：{case.get('situation', '')}", f"- 做法：{case.get('approach', '')}", f"- 结果：{case.get('result', '')}", f"- 启发：{case.get('lesson', '')}", "",
        "## 动手练习", "", str(practice.get("task") or ""), "", *[f"{index}. {step}" for index, step in enumerate(practice.get("steps", []), 1)], "",
        f"交付物：{practice.get('deliverable', '')}", "", "## 我的练习成果", "", str(lesson.get("practice_output") or "尚未填写"), "",
        "## 我的复盘", "", str(lesson.get("reflection") or "尚未填写"), "", "## 本课要点", "", str(content.get("takeaway") or ""),
    ]
    note = write_knowledge_note(
        f"AI 转型学习-{lesson.get('lesson_date')}-{lesson.get('title')}",
        "\n".join(lines),
        metadata={"source": "ai-learning", "lesson_id": lesson_id, "lesson_date": lesson.get("lesson_date")},
        artifact_kind="ai_learning_note",
    )
    artifact = note.get("artifact") or {}
    if artifact.get("id"):
        connection = db_connection()
        try:
            connection.execute("UPDATE ai_learning_lessons SET note_artifact_id = ?, updated_at = ? WHERE id = ?", (int(artifact["id"]), now_iso(), lesson_id))
            connection.commit()
        finally:
            connection.close()
        _app_call('create_relation_record', from_type="ai_learning_lesson", from_id=str(lesson_id), to_type="artifact", to_id=str(artifact["id"]), relation_type="learning_to_note", metadata={"lesson_date": lesson.get("lesson_date")})
    return {"ok": True, "created": True, "note": note, "artifact": artifact, "lesson": _app_call('get_ai_learning_lesson', lesson_id=lesson_id)}


@app.get("/api/ai-learning/dashboard")
async def get_ai_learning_dashboard(track: str = DEFAULT_LEARNING_TRACK) -> dict[str, Any]:
    """学习看板。track 决定课程体系；缺省是 AI 转型轨道，保持原有行为不变。"""
    meta = _app_call('learning_track', track)
    track_id = str(meta["id"])
    profile = await asyncio.to_thread(_app_call, 'get_ai_learning_profile', track_id)
    automation = await asyncio.to_thread(_app_call, 'sync_ai_learning_automation', profile)
    today = await _app_call('generate_ai_learning_lesson', use_llm=False, track=track_id)
    history = await asyncio.to_thread(_app_call, 'list_ai_learning_lessons', 30, track_id)
    push = await asyncio.to_thread(_app_call, 'get_push_subscriptions')
    return {
        "profile": profile,
        "today": today,
        "history": history[:60],
        "stats": _app_call('ai_learning_stats', history),
        "phases": meta["phases"],
        "track": {"id": track_id, "title": meta["title"], "subtitle": meta["subtitle"], "lesson_count": len(meta["curriculum"])},
        "tracks": [{"id": item["id"], "title": item["title"]} for item in LEARNING_TRACKS.values()],
        "automation": automation,
        "push": {"subscriptions": len(push.get("subscriptions") or []), "ready": bool(push.get("subscriptions"))},
        "llm": {"configured": bool(_app_call('llm_settings', ).get("configured"))},
    }


@app.put("/api/ai-learning/profile")
def update_ai_learning_profile(request: AILearningProfileRequest, track: str = DEFAULT_LEARNING_TRACK) -> dict[str, Any]:
    profile = _app_call('save_ai_learning_profile', request, track)
    return {"ok": True, "profile": profile, "automation": _app_call('sync_ai_learning_automation', profile)}


@app.get("/api/ai-learning/lessons/{lesson_id}")
def get_ai_learning_lesson_detail(lesson_id: int) -> dict[str, Any]:
    """打开一节历史课程：包含当时写的练习、自测选择和 AI 批改。

    学习记录此前只是一行标题，点不开——学过什么、当时怎么答的、批改说了什么，
    全都看不到，等于没有留下记录。
    """
    lesson = _app_call('get_ai_learning_lesson', lesson_id=lesson_id)
    if not lesson:
        raise HTTPException(404, "学习课程不存在")
    return {"lesson": lesson}


@app.post("/api/ai-learning/lessons/today/generate")
async def post_ai_learning_lesson(request: AILearningGenerateRequest, track: str = DEFAULT_LEARNING_TRACK) -> dict[str, Any]:
    track_id = _app_call('learning_track_id', track)
    lesson = await _app_call('generate_ai_learning_lesson', force=request.refresh, use_llm=True, track=track_id)
    return {"ok": True, "lesson": lesson, "stats": _app_call('ai_learning_stats', _app_call('list_ai_learning_lessons', 30, track_id)), "llm": {"configured": bool(_app_call('llm_settings', ).get("configured"))}}


@app.post("/api/ai-learning/lessons/{lesson_id}/complete")
def post_ai_learning_complete(lesson_id: int, request: AILearningCompleteRequest) -> dict[str, Any]:
    return {"ok": True, **_app_call('complete_ai_learning_lesson', lesson_id, request)}


@app.patch("/api/ai-learning/lessons/{lesson_id}/progress")
def patch_ai_learning_progress(lesson_id: int, request: AILearningProgressRequest) -> dict[str, Any]:
    return {"ok": True, **_app_call('save_ai_learning_progress', lesson_id, request)}


@app.post("/api/ai-learning/lessons/{lesson_id}/reset-practice")
def post_ai_learning_reset_practice(lesson_id: int) -> dict[str, Any]:
    """清空某一节课的练习产出、复盘和 AI 批改。

    需要这个接口，是因为上一个 bug 留下的烂摊子：在历史课页面上写的练习和
    点的批改，实际都被写到了「今天那节」的行上。修好之后新的写入不会再串，
    但已经串进去的内容还躺在库里——打开今天的课，看到的是自己在第一课写的
    东西，而且没有任何办法清掉。

    只清这一节的作答痕迹，不动课程内容本身（题目、案例、知识点都保留），
    所以清完这一节还能正常做。
    """
    lesson = _app_call('get_ai_learning_lesson', lesson_id=lesson_id)
    if not lesson:
        raise HTTPException(404, "学习课程不存在")
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            """UPDATE ai_learning_lessons
            SET practice_output = '', reflection = '', confidence = 0, quiz_answer = -1,
                quiz_correct = 0, feedback_json = '{}', status = 'ready', completed_at = '', updated_at = ?
            WHERE id = ?""",
            (timestamp, lesson_id),
        )
        connection.commit()
    finally:
        connection.close()
    return {"ok": True, "lesson": _app_call('get_ai_learning_lesson', lesson_id=lesson_id)}


@app.post("/api/ai-learning/lessons/{lesson_id}/review")
async def review_ai_learning_practice(lesson_id: int) -> dict[str, Any]:
    """让 AI 批改这节课的练习产出与自测选择。

    这是「自测环节应该给我 AI 反馈」缺的那一环：此前 practice_output 只入库、
    没有任何东西读它，自测也只是对 correct_index 并回一段人人相同的固定解释。
    """
    if not _app_call('llm_settings', ).get("configured"):
        raise HTTPException(503, "请先配置全局 LLM，才能让 AI 批改练习。")
    return await _app_call('generate_ai_learning_feedback', lesson_id)


@app.post("/api/ai-learning/lessons/{lesson_id}/note")
def post_ai_learning_note(lesson_id: int) -> dict[str, Any]:
    return _app_call('save_ai_learning_note', lesson_id)


# ---------------------------------------------------------------------------
# 主动学习
#
# 在这之前，学习只有一条被动通道：每天推一节课，学完为止。想临时搞懂一个名词、
# 想知道这周 AI 圈发生了什么值得学的、想把某个理论一次性弄透——都没有入口，
# 只能去等哪天课程刚好讲到。这三个 kind 补的就是「我现在就想学这个」。
# ---------------------------------------------------------------------------
EXPLORATION_KINDS: dict[str, dict[str, str]] = {
    "term": {
        "label": "名词",
        "ask": "解释这个名词",
        "shape": "字段：title(凝练标题，10-14 字，必须是对概念的一句话概括，不要重复或照抄用户的问题原文), definition(一句话说清楚), why_it_matters, misconceptions(2-3条常见误解，每条写清楚「很多人以为…其实…」), in_your_work(结合用户 current_role 的具体工作动作——写清楚在什么任务里怎么用，落到可执行的动作，禁止「提升效率」这种空话), boundary(什么情况下这个概念不适用), check(一个能自查是否真懂的问题)",
    },
    "hotspot": {
        "label": "热点",
        "ask": "从最近的真实热点里挑出值得学的部分",
        "shape": "字段：title(凝练标题，10-14 字，概括这次热点，不要重复用户的问题原文), whats_new(发生了什么，只从 real_items 里挑与用户问题最相关的条目，并注明来源；如果 real_items 里没有与用户问题匹配的条目，明确写「没有找到相关热点」，绝不拿训练记忆里的旧闻凑数), why_it_matters, what_to_learn(2-3条从这件事里真正值得学的能力或概念), skeptic(这件事被高估的地方是什么), in_your_work(结合用户 current_role 的具体工作动作，落到可执行的动作), check",
    },
    "theory": {
        "label": "理论",
        "ask": "把这个理论讲透",
        "shape": "字段：title(凝练标题，10-14 字，概括理论核心，不要重复用户的问题原文), core_idea, mechanism(它为什么成立，讲清楚推理链), evidence(它的支持依据，不确定就写不确定), boundary(它在什么条件下失效), in_your_work(结合用户 current_role 的具体工作动作，落到可执行的动作), check",
    },
    "method": {
        "label": "方法",
        "ask": "教我怎么设计/实现/用——给一份能直接照做的操作指南，而不是抽象原理",
        "shape": "字段：title(凝练标题，10-14 字，概括这套方法，不要重复用户的问题原文), steps(3-5 个步骤，每步写清楚具体做什么、产出什么、怎么判断做对了，必须是能直接照做的动作), key_choices(2-3 个关键取舍点，每个说清选项和各自适用场景), common_mistakes(2-3 个常见坑，每个说清错在哪、怎么避开), in_your_work(结合用户 current_role，把这套方法落到他的一项具体工作里，写清第一步先做什么), check(2-3 条自查清单，做完能判断自己有没有做对)",
    },
}


def exploration_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["content"] = decode_json_value(item.pop("content_json", "{}"), {}) or {}
    return item


def list_ai_learning_explorations(track: str = "", limit: int = 20) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM ai_learning_explorations WHERE track = ? ORDER BY id DESC LIMIT ?",
            (_app_call('learning_track_id', track), max(1, min(60, limit))),
        ).fetchall()
        return [_app_call('exploration_row', row) for row in rows]
    finally:
        connection.close()


# 主动学习「推荐问题」：非专业人士不知道问什么，空白输入框就是拦路虎。
# 每个 track × kind 预置一批高质量问题（贴合课程内容、面向普通用户），
# 前端展示 4 条 + 「换一换」轮换；已探索过的问题自动排除。
EXPLORE_RECOMMENDATIONS: dict[str, dict[str, list[dict[str, str]]]] = {
    "ai-transformation": {
        "term": [
            {"topic": "RAG 检索增强到底解决了什么", "why": "课程里反复提到检索，先弄清它解决什么问题"},
            {"topic": "什么是模型幻觉，为什么语气越肯定越危险", "why": "建立对 AI 输出可靠性的第一认知"},
            {"topic": "提示词里的「四要素」是哪四样", "why": "课程核心方法的第一步"},
            {"topic": "什么是 Agent（智能体），和聊天机器人差在哪", "why": "判断哪些任务能交给 Agent 的前提"},
            {"topic": "什么是「预测下一个 token」", "why": "从底层理解模型为什么这样回答"},
            {"topic": "什么是工作流，和单个问答差在哪", "why": "AI 转型的第一步是流程思维"},
            {"topic": "什么是结构化输出，为什么它更容易被程序处理", "why": "让 AI 产出直接可用的前提"},
            {"topic": "什么是上下文，为什么给太多反而容易出错", "why": "理解模型的输入限制"},
        ],
        "theory": [
            {"topic": "为什么大模型能生成流畅文本却可能出错", "why": "语言连贯不等于事实正确"},
            {"topic": "为什么 AI 转型的价值来自流程重构而不是单次问答", "why": "别只把 AI 当打字更快"},
            {"topic": "为什么好的提示词本质是清楚的任务委托", "why": "提示词不是咒语，是说明书"},
            {"topic": "为什么工作拆成输入输出清楚的步骤才容易被 AI 辅助", "why": "可验证才有可复用"},
            {"topic": "为什么 AI 的判断需要证据和来源支撑", "why": "结论要能回溯"},
            {"topic": "为什么模型表现取决于上下文质量而不是模型本身", "why": "同样模型不同结果的原因"},
            {"topic": "为什么流程改造要一次只重做一条线", "why": "避免改造太多失控"},
            {"topic": "为什么高风险输出必须由人来复核", "why": "流畅不等于可靠"},
        ],
        "method": [
            {"topic": "怎么用四要素写出可执行提示词", "why": "课程第二课的实操核心"},
            {"topic": "怎么把一个重复任务改造成 AI 工作流", "why": "选对第一个改造对象"},
            {"topic": "怎么给 AI 的回答做事实校验和验收", "why": "把复核变成流程一环"},
            {"topic": "怎么设计 Agent 的工具（让模型真正执行而不是只读）", "why": "从「问答」到「干活」"},
            {"topic": "怎么把一次访谈记录整理成可追溯的需求证据表", "why": "结合你的实际工作"},
            {"topic": "怎么让 AI 输出 JSON 或表格供下一个流程使用", "why": "结构化输出实操"},
            {"topic": "怎么用 AI 提取竞品资料并标注来源缺失", "why": "带出处地提取，不凭记忆"},
            {"topic": "怎么建立自己的提示词模板库", "why": "把好提示词沉淀复用"},
        ],
        "hotspot": [
            {"topic": "从最近的 AI 热点里挑 2-3 件对产品经理最值得关注的", "why": "用热点雷达筛出真正重要的"},
            {"topic": "最近有哪些新的 Agent 产品形态值得研究", "why": "追踪产品机会"},
            {"topic": "从最近的模型发布看能力边界在怎么变", "why": "能力变化影响产品判断"},
            {"topic": "最近 AI 热点里哪些只是噱头哪些是真变化", "why": "训练判断力"},
            {"topic": "从热点新闻里找适合个人开发者验证的机会", "why": "把热点变成行动"},
            {"topic": "最近哪些公司或产品的 AI 策略值得拆解学习", "why": "学习真实打法"},
            {"topic": "从最近的热点看 AI 转型的学习重点应该放哪", "why": "学习跟着趋势走"},
            {"topic": "最近 AI 圈有什么新工具值得先上手体验", "why": "保持工具雷达"},
        ],
    },
    "embodied": {
        "term": [
            {"topic": "什么是具身智能，和聊天模型差在哪", "why": "课程第一课的核心"},
            {"topic": "什么是感知—决策—执行三层结构", "why": "拆解具身系统的基本框架"},
            {"topic": "什么是闭环误差累积（compounding error）", "why": "有身体的关键约束之一"},
            {"topic": "什么是慢思考加快执行的分层架构", "why": "具身系统的普遍结构"},
            {"topic": "什么是运动规划（Motion Planning）", "why": "让机器人决定怎么动"},
            {"topic": "什么是 sim2real 仿真到真机迁移", "why": "实验室到真机的关键差距"},
            {"topic": "什么是多模态感知（视觉、触觉、力觉）", "why": "身体感知从哪来"},
            {"topic": "什么是位姿与刚体变换", "why": "机器人眼里的空间关系"},
        ],
        "theory": [
            {"topic": "为什么具身系统要把「要做什么」和「怎么动」分开", "why": "大模型不直接输出关节角度"},
            {"topic": "为什么控制回路有毫秒级实时约束", "why": "50Hz 意味着什么"},
            {"topic": "为什么动作不可逆要求安全前置", "why": "打翻杯子没有撤销键"},
            {"topic": "为什么误差会在闭环中累积", "why": "和聊天开环的本质区别"},
            {"topic": "为什么「想清楚再说」在具身系统里行不通", "why": "实时性排除了哪些做法"},
            {"topic": "为什么仿真里能跑通不等于真机也能跑", "why": "sim2real gap 从哪来"},
            {"topic": "为什么数据采集是具身智能的瓶颈", "why": "机器人没有互联网级语料"},
            {"topic": "为什么具身智能需要世界模型", "why": "预测下一步观察"},
        ],
        "method": [
            {"topic": "怎么把一个物理任务拆成感知—决策—执行三层", "why": "课程第一课的练习"},
            {"topic": "怎么评估一个机器人方案是不是真的「具身」", "why": "判断真伪具身"},
            {"topic": "怎么从扫地机或机械臂这类设备开始理解具身系统", "why": "低门槛上手"},
            {"topic": "怎么给具身项目设计安全边界", "why": "安全前置的实操"},
            {"topic": "怎么理解一个机器人系统里的频率要求", "why": "看控制回路约束"},
            {"topic": "怎么判断一个具身产品离落地还有多远", "why": "用三层结构做体检"},
            {"topic": "怎么给机械臂抓取任务设计子目标分解", "why": "把任务交给控制器执行"},
            {"topic": "怎么阅读一篇机器人论文并抓住要点", "why": "高效吸收研究内容"},
        ],
        "hotspot": [
            {"topic": "从最近的具身智能热点看落地进展", "why": "热点雷达里的具身信号"},
            {"topic": "最近哪些机器人公司或产品的进展值得关注", "why": "追踪主要玩家"},
            {"topic": "从热点看人形机器人离普及还有多远", "why": "管理预期"},
            {"topic": "最近具身智能领域有什么新的技术突破", "why": "保持技术雷达"},
            {"topic": "从热点新闻里找具身智能的学习素材", "why": "把新闻变成课程"},
            {"topic": "最近哪些大模型厂商入了具身智能的局", "why": "关注生态变化"},
            {"topic": "从热点看具身智能的落地场景优先在哪", "why": "场景判断"},
            {"topic": "最近具身智能有哪些被高估的宣传", "why": "训练判断力"},
        ],
    },
}


# 个性化推荐缓存：结合档案/课程进度调 LLM 生成一次，1 小时内换分类、
# 换一批都不必反复调用（精选池做兜底，任何时候都可用）。
_EXPLORE_RECOMMEND_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_EXPLORE_RECOMMEND_CACHE_TTL = 60 * 60


def _explore_topic_similar(a: str, b: str) -> bool:
    """推荐排除用轻量相似度：LLM 换措辞（如「什么是 RAG」vs「RAG 到底解决了
    什么」）也能认出来，避免换一批又换回问过的问题。"""
    norm = lambda s: re.sub(r"[\s，。！？、,.;:：—·「」『』()（）\"'“”]", "", s).lower()
    x, y = norm(a), norm(b)
    if not x or not y:
        return False
    if x in y or y in x:
        return True
    try:
        import difflib

        return difflib.SequenceMatcher(None, x, y).ratio() >= 0.62
    except ImportError:
        return False


def _filter_asked_topics(items: list[dict[str, str]], asked: set[str]) -> list[dict[str, str]]:
    return [
        item
        for item in items
        if not any(_app_call('_explore_topic_similar', str(item.get("topic") or ""), topic) for topic in asked)
    ]


async def recommend_ai_learning_explorations(track: str = "", kind: str = "term") -> list[dict[str, str]]:
    """按 track + kind 返回推荐问题（最多 8 条，前端分批轮换）。

    LLM 可用时结合用户档案、课程进度、已问历史现场生成更贴合的推荐；
    未配置 LLM 或生成失败一律回落到精选池（EXPLORE_RECOMMENDATIONS）。
    已探索过的 topic 自动排除，避免「换一换」换回问过的问题。
    """
    track_id = _app_call('learning_track_id', track)
    spec = EXPLORATION_KINDS.get(kind)
    if not spec:
        return []
    asked = {str(item.get("topic") or "").strip() for item in _app_call('list_ai_learning_explorations', track_id, 80)}
    fallback = EXPLORE_RECOMMENDATIONS.get(track_id, {}).get(kind, [])
    fallback_items = _app_call('_filter_asked_topics', fallback, asked) or fallback
    cache_key = f"{track_id}:{kind}"
    now = time.time()
    cached = _EXPLORE_RECOMMEND_CACHE.get(cache_key)
    if cached and now - cached[0] < _EXPLORE_RECOMMEND_CACHE_TTL:
        return cached[1][:8]
    if not _app_call('llm_settings', ).get("configured"):
        return fallback_items[:8]
    items: list[dict[str, str]] = []
    try:
        profile = _app_call('get_ai_learning_profile', track_id)
        lessons = _app_call('list_ai_learning_lessons', 10, track_id)
        curriculum = _app_call('learning_track', track_id)["curriculum"][:5]
        payload = {
            "kind_label": spec["label"],
            "current_role": profile.get("current_role") or "未填写",
            "target_role": profile.get("target_role") or "未填写",
            "experience": profile.get("experience") or "未填写",
            "focus": profile.get("focus") or "",
            "goal": profile.get("goal") or "",
            "curriculum": [f"{c.get('module')}｜{c.get('title')}：{c.get('objective')}" for c in curriculum],
            "recent_lessons": [str(lesson.get("title") or "") for lesson in lessons[:5]],
            "asked_before": sorted(asked)[:10],
        }
        answer = await _app_call('call_llm', 
            [
                {"role": "system", "content": (
                    f"你是给非专业人士做 AI 学习规划的中文教练。用户不知道现在该问什么，请你结合他的岗位、目标和课程进度，"
                    f"推荐 4 个最适合他现在学的「{spec['label']}」类问题。"
                    '只输出 JSON 对象，不要 markdown 代码块，格式：{"items":[{"topic":"问题（一句话、口语化、具体）","why":"为什么值得现在学（一句话）"}]}。'
                    "要求：贴近用户角色和课程进度；topic 要具体可问，不能空泛；不要推荐用户问过的；每条 why 写清楚和用户现状的关系。"
                )},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            max_tokens=1_200,
            temperature=0.7,
            purpose="ai_learning_recommend",
        )
        content = _app_call('parse_llm_json_object', answer) or {}
        raw_items = content.get("items") if isinstance(content.get("items"), list) else []
        items = [
            {"topic": clip(str(item.get("topic") or ""), 120), "why": clip(str(item.get("why") or ""), 200)}
            for item in raw_items
            if isinstance(item, dict) and str(item.get("topic") or "").strip()
        ][:8]
        items = _app_call('_filter_asked_topics', items, asked)
    except Exception as exc:  # noqa: BLE001
        log.warning("个性化推荐失败，回落精选池（%s/%s）：%s", track_id, kind, exc)
        items = []
    if not items:
        items = fallback_items[:8]
    _EXPLORE_RECOMMEND_CACHE[cache_key] = (now, items)
    return items[:8]


@app.get("/api/ai-learning/explorations/recommend")
async def get_ai_learning_exploration_recommendations(track: str = DEFAULT_LEARNING_TRACK, kind: str = "term") -> dict[str, Any]:
    """按 track + kind 返回推荐问题（最多 8 条，前端本地分批轮换）。"""
    track_id = _app_call('learning_track_id', track)
    recommendations = await _app_call('recommend_ai_learning_explorations', track_id, kind)
    return {
        "track": track_id,
        "kind": kind,
        "recommendations": recommendations,
    }


def get_ai_learning_exploration(exploration_id: int) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM ai_learning_explorations WHERE id = ?", (exploration_id,)).fetchone()
        return _app_call('exploration_row', row) if row else None
    finally:
        connection.close()


async def create_ai_learning_exploration(kind: str, topic: str, track: str = "") -> dict[str, Any]:
    """按需生成一份小专题并存下来。"""
    spec = EXPLORATION_KINDS.get(kind)
    if not spec:
        raise HTTPException(400, f"不支持的主动学习类型：{kind}")
    if not _app_call('llm_settings', ).get("configured"):
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM。")
    track_id = _app_call('learning_track_id', track)
    profile = _app_call('get_ai_learning_profile', track_id)
    topic = clip(str(topic or "").strip(), 120)

    grounding: list[dict[str, Any]] = []
    if kind == "hotspot":
        # 热点必须落在真实条目上。让模型自由发挥「最近的 AI 热点」，它只会
        # 复述训练数据里的旧闻，还会说得像刚发生一样——这比不给更糟。
        try:
            snapshot = _app_call('load_aihot_snapshot', )
            grounding = [
                {"title": clip(str(item.get("title") or ""), 140), "source": item.get("source"), "url": item.get("url"), "importance": item.get("importance")}
                for item in _app_call('select_aihot_items', snapshot, mode="useful", limit=12)
            ][:12]
        except Exception as exc:  # noqa: BLE001
            log.warning("读取热点条目失败：%s", exc)
        if not grounding:
            raise HTTPException(
                409,
                "还没有抓到任何 AI 热点条目，没法基于真实内容出专题。先到「AI 热点」项目里刷新一次，再回来。",
            )
    if not topic and kind != "hotspot":
        raise HTTPException(400, f"请先写清楚想学的{spec['label']}。")

    payload = {
        "kind": kind,
        "topic": topic or "（由你从下面的真实热点里挑）",
        "current_role": profile.get("current_role") or "未填写",
        "target_role": profile.get("target_role") or "AI 相关岗位",
        "experience": profile.get("experience"),
        "goal": profile.get("goal") or "提升 AI 实战能力",
        "real_items": grounding,
    }
    answer = await _app_call('call_llm', 
        [
            {"role": "system", "content": (
                f"你是务实的中文技术教练。用户{spec['ask']}。只输出 JSON，不要 markdown 代码块。{spec['shape']}。"
                "写作要求：讲清楚推理过程而不是只给结论；不确定的地方明确写「不确定」；"
                "绝对不要编造公司名、论文、数据、日期或收益数字；"
                "title 必须是凝练的标题，不要照抄用户的 topic 原文；"
                "in_your_work 必须结合用户的 current_role，写清楚在具体工作场景里的一个可执行动作，不要写空话；"
                "如果我给了 real_items，所有事实只能来自 real_items，且按用户的问题（topic）从 real_items 里筛最相关的，没有匹配就明说没有，不要用训练记忆凑数。"
            )},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        max_tokens=2_400,
        temperature=0.4,
        purpose="ai_learning_exploration",
    )
    content = _app_call('parse_llm_json_object', answer)
    if not isinstance(content, dict) or not content:
        # 模型偶发输出不完整 JSON（method 结构复杂时更常见）：重试一次换采样。
        retry = await _app_call('call_llm', 
            [
                {"role": "system", "content": "刚才的输出不是完整 JSON。请严格只输出一个合法的 JSON 对象，不要任何前后缀文字。"},
                {"role": "user", "content": f"原任务：{spec['ask']}。输出字段与要求：{spec['shape']}。\n用户输入：{json.dumps(payload, ensure_ascii=False)}"},
            ],
            max_tokens=2_400,
            temperature=0.5,
            purpose="ai_learning_exploration",
        )
        content = _app_call('parse_llm_json_object', retry)
    if not isinstance(content, dict) or not content:
        raise HTTPException(502, "生成结果不是可解析的结构化内容，请再试一次。")
    title = clip(str(content.get("title") or topic or spec["label"]), 120)
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            "INSERT INTO ai_learning_explorations (track, kind, topic, title, content_json, source, created_at) VALUES (?,?,?,?,?,?,?)",
            (track_id, kind, topic, title, json.dumps(content, ensure_ascii=False), "llm", timestamp),
        )
        connection.commit()
        exploration_id = int(cursor.lastrowid or 0)
    finally:
        connection.close()
    return _app_call('get_ai_learning_exploration', exploration_id) or {}


class AILearningExploreRequest(BaseModel):
    kind: str = Field(default="term", max_length=20)
    topic: str = Field(default="", max_length=120)


@app.get("/api/ai-learning/explorations")
def get_ai_learning_explorations(track: str = DEFAULT_LEARNING_TRACK, limit: int = 20) -> dict[str, Any]:
    return {
        "kinds": [{"id": key, "label": value["label"]} for key, value in EXPLORATION_KINDS.items()],
        "explorations": _app_call('list_ai_learning_explorations', track, limit),
    }


@app.post("/api/ai-learning/explorations")
async def post_ai_learning_exploration(request: AILearningExploreRequest, track: str = DEFAULT_LEARNING_TRACK) -> dict[str, Any]:
    return {"exploration": await _app_call('create_ai_learning_exploration', request.kind, request.topic, track)}


# ---------------------------------------------------------------------------
# AI 出题 → 我作答 → AI 评判
#
# 原来的练习假设「你手上正好有一个真实场景可以拿来练」。大多数时候没有——
# 于是练习框空着，AI 批改也就无从批起。出题走的是另一条路：题目和背景由 AI
# 给全，你只需要思考和回答；参考答案先藏起来，答完再对照。
# ---------------------------------------------------------------------------
def exercise_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["criteria"] = decode_json_value(item.pop("criteria_json", "[]"), []) or []
    item["feedback"] = decode_json_value(item.pop("feedback_json", "{}"), {}) or {}
    item["answered"] = bool(str(item.get("user_answer") or "").strip())
    return item


def list_answered_exercises_for_lesson(lesson_id: int) -> list[dict[str, Any]]:
    """按课程取已作答的练习题（整节点评时一并参考，最多 6 道，避免 prompt 过长）。"""
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM ai_learning_exercises WHERE lesson_id = ? AND user_answer != '' ORDER BY id ASC LIMIT 6",
            (lesson_id,),
        ).fetchall()
        return [_app_call('exercise_row', row) for row in rows]
    finally:
        connection.close()


def get_ai_learning_exercise(exercise_id: int) -> dict[str, Any] | None:
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM ai_learning_exercises WHERE id = ?", (exercise_id,)).fetchone()
        return _app_call('exercise_row', row) if row else None
    finally:
        connection.close()


def list_ai_learning_exercises(track: str = "", limit: int = 20) -> list[dict[str, Any]]:
    connection = db_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM ai_learning_exercises WHERE track = ? ORDER BY id DESC LIMIT ?",
            (_app_call('learning_track_id', track), max(1, min(60, limit))),
        ).fetchall()
        return [_app_call('exercise_row', row) for row in rows]
    finally:
        connection.close()


def public_exercise(exercise: dict[str, Any]) -> dict[str, Any]:
    """没作答之前不返回参考答案。

    参考答案跟着题目一起发到前端，就算界面上藏起来，打开开发者工具也能看到；
    真想抄的人一定会抄，而抄完这道题就废了。答完之后再给。
    """
    item = dict(exercise)
    if not item.get("answered"):
        item["reference_answer"] = ""
    return item


def _fallback_exercise_question(subject: str, current_role: str) -> dict[str, Any]:
    """LLM 出题连续失败时的内置兜底题：不依赖任何外部调用，保证用户不会空手而归。

    出题要写「题干 + 情境 + 评分要点 + 参考答案」，LLM 生成内容较长，
    max_tokens 给足也会偶发截断或不可解析。兜底题是通用结构 + 题目方向拼接，
    虽不如模型出题贴题，但作为最后一道防线比直接报错强得多。
    """
    topic = clip(subject or "一个具体的技术方向", 60)
    role = clip(current_role or "AI 相关岗位", 40)
    return {
        "question": (
            f"围绕「{topic}」，先明确这个目标要解决的核心问题是什么、对「{role}」来说为什么重要；"
            "再给出一个能落地的实现思路（关键步骤），并说明你会怎么验证它确实有效。"
        ),
        "context": (
            f"假设你是{role}，leader 把「{topic}」这个方向交给你，要求一周内给出可执行的技术方案。"
            "没有现成资料，需要你自己拆解目标、确定优先级、规划验证方式。"
            "产出是一份 3 页以内的方案说明。"
        ),
        "criteria": [
            "是否把目标拆解成了具体的核心问题，而不是停留在概念层面",
            "实现思路是否有可执行的步骤，而不是只给结论",
            "是否说明了验证方式（数据、指标或小规模实验）",
            "是否结合了自己的角色和日常工作场景",
        ],
        "reference_answer": (
            f"合格答案应包含三部分：① 目标拆解——把「{topic}」拆成 2-3 个核心问题，"
            "说清最关键的瓶颈在哪里；② 实现路径——给出 3 步左右的具体动作"
            "（先做什么、用什么方法、产出什么）；③ 验证——用一个可量化的指标或小实验确认方向正确。"
            "常见错误答法：只重复概念定义、给大而全但没有取舍的方案、跳过验证直接给结论。"
        ),
    }


async def create_ai_learning_exercise(*, track: str = "", lesson_id: int = 0, topic: str = "") -> dict[str, Any]:
    if not _app_call('llm_settings', ).get("configured"):
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM，才能出题。")
    track_id = _app_call('learning_track_id', track)
    profile = _app_call('get_ai_learning_profile', track_id)
    lesson = _app_call('get_ai_learning_lesson', lesson_id=lesson_id) if lesson_id else None
    if lesson_id and not lesson:
        raise HTTPException(404, "学习课程不存在")
    subject = clip(str(topic or "").strip(), 120) or str((lesson or {}).get("title") or "")
    if not subject:
        raise HTTPException(400, "请给一个题目方向，或者从某一节课出题。")
    recent = [item.get("question", "") for item in _app_call('list_ai_learning_exercises', track_id, 8)]
    payload = {
        "subject": subject,
        "lesson_objective": ((lesson or {}).get("content") or {}).get("objective", ""),
        "lesson_knowledge": ((lesson or {}).get("content") or {}).get("knowledge", []),
        "current_role": profile.get("current_role") or "未填写",
        "target_role": profile.get("target_role") or "AI 相关岗位",
        "experience": profile.get("experience"),
        "avoid_questions": recent,
    }
    # 出题要写题干+情境+评分要点+参考答案，四段都长。输出预算给足
    # （1500 曾导致 JSON 稳定截断 → 解析失败 → 连续 502），再配重试和兜底。
    max_output = 3_000
    try:
        answer = await _app_call('call_llm', 
            [
                {"role": "system", "content": (
                    "你是务实的中文教练，负责出一道能靠思考回答的题，不需要用户手上有现成的工作材料。只输出 JSON，不要 markdown 代码块。"
                    "题型要求：可以从「判断+理由 / 方案权衡 / 排序优先级 / 找反例 / 纠错 / 场景决策」中选，必须与 avoid_questions 里最近的题目不同题型、不同角度，避免连续几道题都是同一个套路；"
                    "字段：question(题干，给出一个需要判断/权衡/决策的任务并说明理由，不是填空也不是选择), "
                    "context(题目需要的全部背景，写成一个具体到能直接思考的情境：具体角色、具体输入、具体产出要求；情境尽量贴近用户的 current_role 日常会做的事。背景里的公司、人名、数字都写成明显的示例，不要冒充真实公司或真实数据), "
                    "criteria(3-4条评分要点，每条是一个能判断答没答到的具体标准), "
                    "reference_answer(一份合格答案，把推理过程写出来，并指出常见的错误答法错在哪)。"
                    "题目难度对准用户的当前水平，别出只能靠背概念回答的题。"
                )},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            max_tokens=max_output,
            temperature=0.5,
            purpose="ai_learning_exercise",
        )
        data = _app_call('parse_llm_json_object', answer)
        if not isinstance(data, dict) or not str(data.get("question") or "").strip():
            # 模型偶发输出不完整 JSON：重试一次换采样。
            retry = await _app_call('call_llm', 
                [
                    {"role": "system", "content": "刚才的输出不是完整 JSON。请严格只输出一个合法的 JSON 对象，不要任何前后缀文字。"},
                    {"role": "user", "content": f"原任务：出一道能靠思考回答的题。输出字段：question/context/criteria/reference_answer。\n用户输入：{json.dumps(payload, ensure_ascii=False)}"},
                ],
                max_tokens=max_output,
                temperature=0.5,
                purpose="ai_learning_exercise",
            )
            data = _app_call('parse_llm_json_object', retry)
    except Exception as exc:  # noqa: BLE001
        log.warning("出题 LLM 调用失败：%s", exc)
        data = None
    if not isinstance(data, dict) or not str(data.get("question") or "").strip():
        # 连续两次生成失败（或调用异常）：内置模板题兜底，绝不 502。
        log.warning("出题连续失败，使用内置模板兜底（subject=%s）", subject)
        data = _app_call('_fallback_exercise_question', subject, profile.get("current_role") or "")
    timestamp = now_iso()
    connection = db_connection()
    try:
        cursor = connection.execute(
            """INSERT INTO ai_learning_exercises
            (track, lesson_id, topic, question, context, reference_answer, criteria_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                track_id, int(lesson_id or 0), subject,
                clip(str(data.get("question") or ""), 1200),
                clip(str(data.get("context") or ""), 2500),
                clip(str(data.get("reference_answer") or ""), 4000),
                json.dumps([str(item) for item in (data.get("criteria") or [])][:6], ensure_ascii=False),
                timestamp, timestamp,
            ),
        )
        connection.commit()
        exercise_id = int(cursor.lastrowid or 0)
    finally:
        connection.close()
    return _app_call('public_exercise', _app_call('get_ai_learning_exercise', exercise_id) or {})


async def grade_ai_learning_exercise(exercise_id: int, user_answer: str) -> dict[str, Any]:
    exercise = _app_call('get_ai_learning_exercise', exercise_id)
    if not exercise:
        raise HTTPException(404, "题目不存在")
    text = str(user_answer or "").strip()
    if not text:
        raise HTTPException(400, "先写下你的答案，再让 AI 评判。")
    if not _app_call('llm_settings', ).get("configured"):
        raise HTTPException(503, "请先在工作台顶部配置全局 LLM，才能评判。")
    payload = {
        "question": exercise.get("question"),
        "context": exercise.get("context"),
        "criteria": exercise.get("criteria"),
        "reference_answer": exercise.get("reference_answer"),
        "user_answer": clip(text, 4000),
    }
    try:
        answer = await _app_call('call_llm', 
            [
                {"role": "system", "content": (
                    "你是严格但讲道理的中文教练。对照评分要点批改用户的答案。只输出 JSON，不要 markdown 代码块。"
                    "字段：score(0-100 的整数), verdict(一句话结论), "
                    "hits(答到的要点，逐条引用用户的原话), "
                    "misses(没答到或答错的要点，每条说清楚差在哪、正确的想法是什么), "
                    "rewrite(把用户的答案改写成一份合格答案，保留他自己的思路和用词习惯), "
                    "next_step(下一步该练什么，一个具体动作)。"
                    "给分规则：按评分要点逐条核计，答到几个要点就给对应档位的分；方向正确只是不完整，给中间分并说明差在哪一项，不要一票否决；不因为答案短、用词口语、或与参考答案表述不同而扣分——只看是否答到要点。"
                )},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            max_tokens=2_400,
            temperature=0.25,
            purpose="ai_learning_exercise_review",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("评判 LLM 调用失败：%s", exc)
        raise HTTPException(502, "AI 评判暂时不可用，请稍后再试。") from exc
    feedback = _app_call('parse_llm_json_object', answer)
    if not isinstance(feedback, dict):
        feedback = {"verdict": clip(answer, 1200), "score": -1}
    try:
        score = int(feedback.get("score", -1))
    except (TypeError, ValueError):
        score = -1
    timestamp = now_iso()
    connection = db_connection()
    try:
        connection.execute(
            "UPDATE ai_learning_exercises SET user_answer = ?, feedback_json = ?, score = ?, updated_at = ? WHERE id = ?",
            (clip(text, 4000), json.dumps(feedback, ensure_ascii=False), max(-1, min(100, score)), timestamp, exercise_id),
        )
        connection.commit()
    finally:
        connection.close()
    # 交卷之后才把参考答案一起返回，这时对照才有意义。
    return _app_call('get_ai_learning_exercise', exercise_id) or {}


class AILearningExerciseRequest(BaseModel):
    lesson_id: int = 0
    topic: str = Field(default="", max_length=120)


class AILearningExerciseAnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


@app.get("/api/ai-learning/exercises")
def get_ai_learning_exercises(track: str = DEFAULT_LEARNING_TRACK, limit: int = 20) -> dict[str, Any]:
    return {"exercises": [_app_call('public_exercise', item) for item in _app_call('list_ai_learning_exercises', track, limit)]}


@app.post("/api/ai-learning/exercises")
async def post_ai_learning_exercise(request: AILearningExerciseRequest, track: str = DEFAULT_LEARNING_TRACK) -> dict[str, Any]:
    return {"exercise": await _app_call('create_ai_learning_exercise', track=track, lesson_id=request.lesson_id, topic=request.topic)}


@app.post("/api/ai-learning/exercises/{exercise_id}/answer")
async def post_ai_learning_exercise_answer(exercise_id: int, request: AILearningExerciseAnswerRequest) -> dict[str, Any]:
    return {"exercise": await _app_call('grade_ai_learning_exercise', exercise_id, request.answer)}


__all__ = [
    "learning_track",
    "learning_track_id",
    "ai_learning_today",
    "ai_learning_profile_row",
    "get_ai_learning_profile",
    "save_ai_learning_profile",
    "ai_learning_lesson_row",
    "get_ai_learning_lesson",
    "list_ai_learning_lessons",
    "fallback_ai_learning_content",
    "parse_llm_json_object",
    "parse_ai_learning_content",
    "generate_ai_learning_lesson",
    "ai_learning_stats",
    "ai_learning_automation_rule",
    "sync_ai_learning_automation",
    "complete_ai_learning_lesson",
    "save_ai_learning_progress",
    "AI_LEARNING_FEEDBACK_SCHEMA",
    "_ai_learning_work_context",
    "_parse_ai_learning_feedback",
    "generate_ai_learning_feedback",
    "AI_LEARNING_CURRICULUM",
    "AI_LEARNING_PHASES",
    "AILearningProfileRequest",
    "AILearningGenerateRequest",
    "AILearningCompleteRequest",
    "AILearningProgressRequest",
    "EMBODIED_PHASES",
    "EMBODIED_CURRICULUM",
    "LEARNING_TRACKS",
    "DEFAULT_LEARNING_TRACK",
    "save_ai_learning_note",
    "get_ai_learning_dashboard",
    "update_ai_learning_profile",
    "get_ai_learning_lesson_detail",
    "post_ai_learning_lesson",
    "post_ai_learning_complete",
    "patch_ai_learning_progress",
    "post_ai_learning_reset_practice",
    "review_ai_learning_practice",
    "post_ai_learning_note",
    "EXPLORATION_KINDS",
    "exploration_row",
    "list_ai_learning_explorations",
    "EXPLORE_RECOMMENDATIONS",
    "_EXPLORE_RECOMMEND_CACHE",
    "_EXPLORE_RECOMMEND_CACHE_TTL",
    "_explore_topic_similar",
    "_filter_asked_topics",
    "recommend_ai_learning_explorations",
    "get_ai_learning_exploration_recommendations",
    "get_ai_learning_exploration",
    "create_ai_learning_exploration",
    "AILearningExploreRequest",
    "get_ai_learning_explorations",
    "post_ai_learning_exploration",
    "exercise_row",
    "list_answered_exercises_for_lesson",
    "get_ai_learning_exercise",
    "list_ai_learning_exercises",
    "public_exercise",
    "_fallback_exercise_question",
    "create_ai_learning_exercise",
    "grade_ai_learning_exercise",
    "AILearningExerciseRequest",
    "AILearningExerciseAnswerRequest",
    "get_ai_learning_exercises",
    "post_ai_learning_exercise",
    "post_ai_learning_exercise_answer",
]
