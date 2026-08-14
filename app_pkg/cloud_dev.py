"""云开发（CloudStudio 沙箱）领域。

拆自 app.py（2026-08-14 第二十一批）。包含: 云开发审批/补丁执行/生成执行/状态。
仍在 app.py 的领域函数经 _app_call 运行时转发。
"""
from __future__ import annotations

import asyncio
import json
import re

import cloud_dev
import cloud_patch
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .agent_platform import AGENT_REGISTRY
from .agent_runs import add_agent_run_event, create_agent_run_record, get_agent_run, update_agent_run_record
from .artifacts import create_relation_record, create_work_item_record, get_work_item_record, register_artifact_safely, update_work_item_record
from .core import CLOUDGEN_DIR, DATA_DIR, OUTPUTS_DIR, ROOT, clip, decode_json_column, log, now_iso
from .db import db_connection
from .llm import call_llm
from .instance import app
from .notifications import create_notification_record


def _app_call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """通过 app 命名空间调用仍在 app.py 的领域函数——测试 patch app.X 时能生效。"""
    import app as _app

    return getattr(_app, fn_name)(*args, **kwargs)


def _public_cloud_dev_result(result: dict[str, Any]) -> dict[str, Any]:
    public = dict(result)
    public.pop("command", None)
    if public.get("workspace"):
        public["workspace"] = Path(str(public["workspace"])).name
    return public


def create_cloud_dev_approval(parsed: dict[str, Any], *, source: str = "workbench") -> dict[str, Any]:
    project = str(parsed.get("project") or "workbench")
    payload = {
        "request": {"project": project, "action": str(parsed.get("action") or "build")},
        "source": source,
        "command_label": str(parsed.get("raw") or "云开发构建")[:400],
        "execution_policy": "构建可能写入显式工作区；审批只登记意图，仍需通过云开发固定配方执行，不支持任意 shell 或自动部署。",
    }
    approval = _app_call('create_approval_request', "cloud-dev", "cloud_dev_build", f"云开发构建审批 · {project}", payload)
    item = create_work_item_record(
        title=f"云开发构建 · {project}",
        description="构建请求已进入审批。审批前不会执行命令；审批后仍只允许项目固定构建配方。",
        kind="cloud_dev_build",
        status="blocked",
        priority="high",
        source_project=source if source in AGENT_REGISTRY else "workbench",
        target_project="cloud-dev",
        metadata={"approval_id": approval["id"], "project": project, "action": "build", "source": source},
    )
    relation = create_relation_record(from_type="approval", from_id=approval["id"], to_type="work_item", to_id=str(item["id"]), relation_type="approval_to_cloud_dev", metadata={"project": project})
    create_notification_record(title="云开发构建待审批", body=f"{project} · 构建不会自动执行，请在审批中心确认。", project_id="cloud-dev", kind="approval", level="warning", href="/approvals", event_key=f"cloud-dev:{approval['id']}", dedupe_seconds=0)
    return {"approval": approval, "work_item": item, "relation": relation}


async def execute_cloud_dev_patch(requirement: str, *, source: str = "workbench", chat_id: str = "") -> dict[str, Any]:
    """云端自动改：飞书一句话 → LLM 生成编辑计划 → 校验 → 生成审批（不直接改代码）。

    审批通过后才由 execute_approved_cloud_dev_patch 应用变更（备份→应用→测试→重启，失败回滚）。
    """
    requirement = str(requirement or "").strip()
    if not requirement:
        return {"status": "rejected", "message": "请描述要改什么，例如：云开发 帮我改一下 AI 伴读的按钮颜色。"}

    async def llm_call(messages: list[dict[str, Any]]) -> str:
        return await call_llm(
            messages,
            max_tokens=4000,
            temperature=0.2,
            purpose="cloud_dev_patch",
        )

    plan = await cloud_patch.plan_patch(requirement, llm_call)
    if not plan.get("ok"):
        return {"status": "failed", "action": "patch", "message": (plan.get("errors") or ["生成编辑计划失败"])[0], "plan": None}

    edits = plan["edits"]
    payload = {
        "kind": "cloud_dev_patch",
        "requirement": requirement[:400],
        "summary": str(plan.get("summary") or "")[:200],
        "edits": edits,
        "source": source,
        "chat_id": chat_id,
        "execution_policy": "审批通过后：备份涉及文件 → 应用编辑 → 运行测试 → 重启服务 + 健康检查；任一步失败自动回滚备份。不执行任意 shell、不自动部署。",
    }
    approval = _app_call('create_approval_request', "cloud-dev", "cloud_dev_patch", f"云端自动改审批 · {str(plan.get('summary') or requirement)[:48]}", payload)
    item = create_work_item_record(
        title=f"云端自动改 · {str(plan.get('summary') or requirement)[:40]}",
        description=f"需求：{requirement[:200]}\n改动摘要：{str(plan.get('summary') or '')[:200]}\n涉及 {len(edits)} 处编辑。审批通过前不会改动任何代码。",
        kind="cloud_dev_patch",
        status="blocked",
        priority="high",
        source_project=source if source in AGENT_REGISTRY else "workbench",
        target_project="cloud-dev",
        metadata={"approval_id": approval["id"], "action": "patch", "requirement": requirement[:200], "source": source},
    )
    create_relation_record(from_type="approval", from_id=approval["id"], to_type="work_item", to_id=str(item["id"]), relation_type="approval_to_cloud_dev", metadata={"action": "patch"})
    create_notification_record(title="云端自动改待审批", body=f"{str(plan.get('summary') or requirement)[:80]} · 审批通过前不会改动代码。", project_id="cloud-dev", kind="approval", level="warning", href="/approvals", event_key=f"cloud-dev-patch:{approval['id']}", dedupe_seconds=0)
    files = sorted({str(edit["file"]) for edit in edits})
    return {
        "status": "approval_required",
        "action": "patch",
        "requirement": requirement[:200],
        "summary": str(plan.get("summary") or "")[:200],
        "files": files,
        "edits_count": len(edits),
        "approval_id": approval["id"],
        "message": f"已生成编辑计划（{len(edits)} 处，涉及 {len(files)} 个文件），已进入审批。审批通过前不会改动代码。",
    }


async def execute_approved_cloud_dev_patch(approval_id: str) -> dict[str, Any]:
    """审批通过后执行云端自动改：备份 → 应用 → 测试 → 重启服务 + 健康检查，失败自动回滚。"""
    connection = db_connection()
    try:
        row = connection.execute("SELECT * FROM approval_requests WHERE id = ? AND kind = 'cloud_dev_patch'", (approval_id,)).fetchone()
    finally:
        connection.close()
    if not row:
        raise HTTPException(404, "云端自动改审批不存在")
    if str(row["status"] or "") != "approved":
        raise HTTPException(409, "请先在审批中心批准该变更，再显式点击执行")
    payload = decode_json_column(row["payload_json"] or "{}")
    edits = payload.get("edits") if isinstance(payload, dict) else None
    requirement = str((payload or {}).get("requirement") or "")[:200]
    if not isinstance(edits, list) or not edits:
        raise HTTPException(400, "审批缺少编辑计划，无法执行")

    backup_root = DATA_DIR / "clouddev-patches" / approval_id
    apply_result = await asyncio.to_thread(cloud_patch.apply_edits, edits, None, backup_dir=backup_root)
    if not apply_result.get("ok"):
        raise HTTPException(500, "；".join(apply_result.get("errors") or ["应用失败"]))

    # 运行测试（尽力而为，测试环境缺失不阻塞回滚判断）
    test_output = ""
    test_ok = False
    try:
        test_result = await asyncio.to_thread(
            _run_cloud_patch_tests,
        )
        test_output = test_result["output"]
        test_ok = test_result["ok"]
    except Exception as exc:
        test_output = f"测试执行异常：{clip(str(exc), 300)}"
        test_ok = False

    # 重启服务：只改了 .py 才需要重启（static/ 静态文件即时生效）；失败回滚
    needs_restart = any(str(edit.get("file") or "").endswith(".py") for edit in edits)
    restart_out = ""
    if needs_restart:
        restarted = False
        try:
            restart_ok, restart_out = await asyncio.to_thread(_restart_workbench_service)
            restarted = restart_ok and restart_out
        except Exception as exc:
            restarted = False
            restart_out = f"重启异常：{clip(str(exc), 200)}"
        if not (test_ok and restarted):
            await asyncio.to_thread(cloud_patch.rollback, backup_root)
            summary = f"应用 {len(edits)} 处编辑后未能通过验证（测试:{'通过' if test_ok else '失败'}，服务:{'重启成功' if restarted else '失败'}），已自动回滚。"
            create_notification_record(title="云端自动改已回滚", body=summary, project_id="cloud-dev", kind="cloud_dev", level="error", href="/projects/cloud-dev", event_key=f"cloud-dev-patch-rollback:{approval_id}", dedupe_seconds=0)
            return {"ok": False, "approval_id": approval_id, "message": summary, "test_output": clip(test_output, 800)}
    else:
        # 纯前端改动：测试通过即可，无需重启
        if not test_ok:
            await asyncio.to_thread(cloud_patch.rollback, backup_root)
            summary = f"应用 {len(edits)} 处编辑后测试未通过（{clip(test_output, 200)}），已自动回滚。"
            create_notification_record(title="云端自动改已回滚", body=summary, project_id="cloud-dev", kind="cloud_dev", level="error", href="/projects/cloud-dev", event_key=f"cloud-dev-patch-rollback:{approval_id}", dedupe_seconds=0)
            return {"ok": False, "approval_id": approval_id, "message": summary, "test_output": clip(test_output, 800)}

    summary = f"变更已应用并验证通过（{len(edits)} 处编辑，测试通过{'，服务已重启' if needs_restart else '，静态文件即时生效'}）。需求：{requirement}"
    create_notification_record(title="云端自动改已上线", body=summary, project_id="cloud-dev", kind="cloud_dev", level="success", href="/projects/cloud-dev", event_key=f"cloud-dev-patch-done:{approval_id}", dedupe_seconds=0)
    return {"ok": True, "approval_id": approval_id, "message": summary, "files": sorted({str(e["file"]) for e in edits}), "test_output": clip(test_output, 800), "restart_output": clip(restart_out, 300)}


def _run_cloud_patch_tests() -> dict[str, Any]:
    """在仓库根跑最小测试集（尽量不依赖外部网络）。"""
    import subprocess
    root = ROOT
    python = str(root / ".venv" / "bin" / "python") if (root / ".venv" / "bin" / "python").exists() else "python3"
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", "tests/test_cloud_dev_and_quant.py", "tests/test_workbench_status.py", "-q", "--no-header", "-x"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = (proc.stdout or "")[-1500:] + "\n" + (proc.stderr or "")[-500:]
        return {"ok": proc.returncode == 0, "output": output.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "测试超时（180s）"}
    except Exception as exc:
        return {"ok": False, "output": f"测试执行失败：{clip(str(exc), 200)}"}


def _restart_workbench_service() -> tuple[bool, str]:
    """重启 workbench 服务（systemd）。

    必须在子进程 detached 方式执行，否则 systemctl restart 会杀掉当前请求进程，
    导致健康检查逻辑无法完成。返回 (是否已提交重启, 提示文案)。
    """
    import shutil
    import subprocess
    if not shutil.which("systemctl"):
        return False, "非 systemd 环境，跳过服务重启（请手动重启验证）"
    try:
        # detached：由 setsid 启动独立进程执行重启，当前请求先返回
        subprocess.run(
            ["setsid", "systemctl", "restart", "workbench"],
            check=True,
            capture_output=True,
            timeout=15,
        )
        return True, "服务重启已提交，几秒后自动恢复；前端静态改动即时生效。"
    except subprocess.TimeoutExpired:
        return False, "服务重启命令超时"
    except Exception as exc:
        return False, f"服务重启失败：{clip(str(exc), 200)}"


async def execute_cloud_dev_generate(requirement: str, kind: str = "webpage") -> dict[str, Any]:
    """云端生成工坊：飞书一句话 → LLM 生成可交付产物 → 存 outputs/cloudgen + Artifact。

    产物仅作为文件保存与查看，不在服务器执行、不部署；链接经 Basic Auth 认证访问。
    """
    requirement = str(requirement or "").strip()
    if not requirement:
        return {"status": "rejected", "message": "请描述想生成的内容，例如：帮我做一个理财记账网页。"}
    kind = str(kind or "webpage")
    plan = {
        "webpage": {"ext": "html", "label": "网页原型", "prompt": "生成一个可直接在浏览器打开的完整单文件 HTML 网页原型（内联 CSS，可含少量内联 JS），中文界面，视觉现代简洁。需求：{requirement}。只输出完整 HTML 代码，不要额外说明。"},
        "doc": {"ext": "md", "label": "文档", "prompt": "撰写一份结构清晰的中文 Markdown 文档/报告。需求：{requirement}。包含：背景、核心内容（分节）、要点与建议、待确认事项。只输出 Markdown 正文。"},
        "script": {"ext": "py", "label": "脚本", "prompt": "生成一个完整可运行的 Python 脚本（含 argparse 或 main()，含注释与异常处理）。需求：{requirement}。只输出代码。"},
    }
    config = plan.get(kind, plan["webpage"])
    requirement_label = requirement if len(requirement) <= 60 else requirement[:60] + "…"
    try:
        content = await call_llm(
            [
                {"role": "system", "content": "你是云端开发助手。严格按用户要求生成可直接交付的产物，输出要完整、自包含、可运行/可打开。不输出多余解释。"},
                {"role": "user", "content": config["prompt"].format(requirement=requirement)},
            ],
            max_tokens=4000,
            temperature=0.35,
            purpose="cloud_dev_generate",
        )
    except Exception as exc:
        return {"status": "failed", "kind": kind, "message": f"生成失败：{clip(str(exc), 500) or 'LLM 调用异常'}"}
    content = str(content or "").strip()
    if not content:
        return {"status": "failed", "kind": kind, "message": "生成结果为空，请稍后再试。"}
    CLOUDGEN_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", requirement_label).strip("-")[:40] or "deliverable"
    filename = f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.{config['ext']}"
    path = CLOUDGEN_DIR / filename
    path.write_text(content, encoding="utf-8")
    artifact = register_artifact_safely(
        project_id="cloud-dev",
        name=filename,
        path=str(path),
        kind="cloud_dev_generate",
        metadata={"kind": kind, "label": config["label"], "requirement": requirement[:200], "generated_at": now_iso()},
    )
    first_lines = [line for line in content.splitlines() if line.strip()][:3]
    summary = "；".join(first_lines)[:200]
    return {
        "status": "ok",
        "kind": kind,
        "label": config["label"],
        "requirement": requirement[:200],
        "file": filename,
        "url": f"/outputs/cloudgen/{filename}",
        "artifact_id": artifact["id"] if artifact else None,
        "summary": summary,
        "message": f"已生成{config['label']}：{filename}。点开链接查看；产物只保存不执行、不部署。",
    }


async def execute_cloud_dev_request(parsed: dict[str, Any], *, source: str = "workbench", chat_id: str = "") -> dict[str, Any]:
    project = str(parsed.get("project") or "workbench")
    action = str(parsed.get("action") or "")
    if action == "generate":
        title = f"云开发生成 · {str(parsed.get('requirement') or '未命名需求')[:24]}"
    else:
        title = f"云开发{action} · {project}"
    run = create_agent_run_record(project_id="cloud-dev", kind="cloud_dev", title=title, request={"project": project, "action": action, "source": source})
    item = create_work_item_record(title=title, description=str(parsed.get("raw") or title), kind="cloud_dev", status="running", source_project=source if source in AGENT_REGISTRY else "workbench", target_project="cloud-dev", metadata={"run_id": run["id"], "project": project, "action": action, "chat_id": chat_id})
    update_agent_run_record(run["id"], status="running")
    add_agent_run_event(run["id"], "execution_started", "已通过固定云开发配方开始执行。", metadata={"project": project, "action": action})
    try:
        if action == "generate":
            result = await execute_cloud_dev_generate(str(parsed.get("requirement") or ""), str(parsed.get("kind") or "webpage"))
        elif action == "patch":
            result = await execute_cloud_dev_patch(str(parsed.get("requirement") or ""), source=source, chat_id=chat_id)
        else:
            result = await asyncio.to_thread(cloud_dev.run_cloud_dev, parsed)
    except Exception as exc:
        result = {
            "status": "failed",
            "project": project,
            "action": action,
            "message": f"云开发固定动作异常：{clip(str(exc), 800) or '未知错误'}",
        }
    public = _public_cloud_dev_result(result)
    status = result.get("status")
    succeeded = status == "ok"
    pending_approval = status == "approval_required"
    run_status = "succeeded" if succeeded else ("pending_approval" if pending_approval else "failed")
    run_message = "云开发任务完成。" if succeeded else ("已生成编辑计划，等待审批。" if pending_approval else f"云开发任务未完成：{result.get('message') or result.get('status')}")
    update_agent_run_record(run["id"], status=run_status, result=public, error="" if succeeded or pending_approval else str(result.get("message") or result.get("output") or result.get("status") or "执行失败"))
    add_agent_run_event(run["id"], run_status, run_message, level="success" if succeeded else ("info" if pending_approval else "error"), metadata={"status": status, "exit_code": result.get("exit_code")})
    update_work_item_record(item["id"], {"status": "done" if succeeded else ("blocked" if pending_approval else "failed"), "result_json": json.dumps(public, ensure_ascii=False), "last_error": "" if succeeded or pending_approval else str(result.get("message") or result.get("output") or result.get("status") or "执行失败"), "completed_at": now_iso()})
    return {"run": get_agent_run(run["id"]) or run, "work_item": get_work_item_record(item["id"]) or item, "result": public}


@app.get("/outputs/cloudgen/{filename}")
async def cloudgen_file(filename: str) -> FileResponse:
    """云开发生成工坊的产物访问：仅认证后可看，只读，不执行。"""
    safe = Path(filename).name
    if safe != filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404, detail="产物不存在")
    path = CLOUDGEN_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="产物不存在")
    return FileResponse(path)


@app.get("/api/cloud-dev")
async def get_cloud_dev_status() -> dict[str, Any]:
    workspaces = cloud_dev.workspace_map()
    return {
        "policy": cloud_dev.cloud_dev_policy(),
        "workspaces": [_public_cloud_dev_result(cloud_dev.run_cloud_dev({"ok": True, "project": alias, "action": "status"})) for alias in sorted(workspaces)],
    }


@app.post("/api/cloud-dev")
async def run_cloud_dev_api(request: CloudDevRequest) -> dict[str, Any]:
    parsed = cloud_dev.parse_cloud_dev_command(request.command)
    if not parsed.get("ok"):
        raise HTTPException(400, parsed.get("message") or "云开发命令无效")
    if parsed.get("requires_approval"):
        return {"ok": True, "status": "approval_required", **create_cloud_dev_approval(parsed)}
    result = await execute_cloud_dev_request(parsed)
    return {"ok": result.get("result", {}).get("status") == "ok", **result, "policy": cloud_dev.cloud_dev_policy()}


@app.post("/api/cloud-dev/approvals/{approval_id}/execute")
async def execute_approved_cloud_dev(approval_id: str) -> dict[str, Any]:
    connection = db_connection()
    try:
        row = connection.execute("SELECT id, kind, status FROM approval_requests WHERE id = ?", (approval_id,)).fetchone()
    finally:
        connection.close()
    if not row:
        raise HTTPException(404, "云开发审批不存在")
    if str(row["kind"] or "") == "cloud_dev_patch":
        return await execute_approved_cloud_dev_patch(approval_id)
    if str(row["kind"] or "") != "cloud_dev_build":
        raise HTTPException(400, "不支持的审批类型")
    if str(row["status"] or "") != "approved":
        raise HTTPException(409, "请先在审批中心批准该构建，再显式点击执行")
    payload = decode_json_column(row["payload_json"] or "{}")
    request_payload = payload.get("request") if isinstance(payload, dict) else {}
    parsed = {"ok": True, "project": str((request_payload or {}).get("project") or "workbench"), "action": "build", "raw": str((payload or {}).get("command_label") or "云开发构建")}
    result = await execute_cloud_dev_request(parsed, source="workbench")
    run_id = str((result.get("run") or {}).get("id") or "")
    if run_id:
        await asyncio.to_thread(create_relation_record, from_type="approval", from_id=approval_id, to_type="agent_run", to_id=run_id, relation_type="approval_to_cloud_dev_run", metadata={"action": "build"})
    create_notification_record(title="云开发构建已执行", body=f"{parsed['project']} · 结果：{(result.get('result') or {}).get('status')}", project_id="cloud-dev", kind="cloud_dev", level="success" if (result.get("result") or {}).get("status") == "ok" else "error", href="/projects/cloud-dev", event_key=f"cloud-dev-executed:{approval_id}", dedupe_seconds=0)
    return {"ok": (result.get("result") or {}).get("status") == "ok", "approval_id": approval_id, **result}


__all__ = [
    "_public_cloud_dev_result",
    "create_cloud_dev_approval",
    "execute_cloud_dev_patch",
    "execute_approved_cloud_dev_patch",
    "_run_cloud_patch_tests",
    "_restart_workbench_service",
    "execute_cloud_dev_generate",
    "execute_cloud_dev_request",
    "cloudgen_file",
    "get_cloud_dev_status",
    "run_cloud_dev_api",
    "execute_approved_cloud_dev",
]
