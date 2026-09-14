"""同进程子 Agent（Harness 级，Section 5）。

不创建独立进程。每个子 Agent 具备独立会话上下文、独立 Agent Loop、独立工具作用域，
并记录 parent_job_id / parent_session_id、独立取消信号，在 Job Registry 中可观察。

安全限制：
- 最大委派深度默认 2（主运行 depth=0，子 Agent depth=1，子 Agent 不能再派生子 Agent）。
- 单个父 Agent 最多 4 个子 Agent。
- 子 Agent 继承工作目录但不能扩大权限，默认不能继续创建子 Agent。
- 父 Agent 取消时递归取消所有子任务。
"""
from __future__ import annotations

from naiba.core.contracts import AppContext, RunContext

import traceback
from pathlib import Path
from typing import Any, Callable

from naiba.jobs import JobRegistry, JobSpec
from naiba.core.history import build_model_history
from naiba.core.tool_results import model_visible_run

MAX_SUBAGENT_DEPTH = 2
MAX_CHILDREN_PER_PARENT = 4

# 子 Agent 默认开放的工具（排除任务/子 Agent 管理类工具，防止递归委派与越权作业管理）
SUBAGENT_ALLOWED_TOOLS = [
    "read_file",
    "list_directory",
    "search_files",
    "write_file",
    "edit_file",
    "pwsh",
    "run_skill_script",
    "http_request",
]

SUBAGENT_BLOCKED_TOOLS = {
    "register_mcp",
    "subagent",
    "run_in_background",
    "job_output",
    "job_status",
    "job_wait",
    "job_kill",
}


def run_subagent_agent(
    app: AppContext,
    job_id: str,
    spec: JobSpec,
    cancel: Any,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    """子 Agent 运行器：以隔离上下文执行一次 Agent Loop，结果写回 Job。"""
    from naiba.skills.agent import SkillAgent
    from naiba.core.exceptions import TaskCancelled
    from naiba.plans import CraftToolExecutor

    conversation_id = str(spec.conversation_id or "")
    params = spec.params or {}
    instruction = str(params.get("instruction") or "")
    if not instruction:
        app.storage.update_job(job_id, result={"error": "缺少 instruction"})
        return
    conversation = app.storage.get_conversation(conversation_id)
    if not conversation:
        app.storage.update_job(job_id, result={"error": "对话已删除"})
        return
    history = build_model_history(conversation.get("messages", []))
    model_key = str(conversation.get("model_key") or "")
    if not model_key:
        provider_id = str(conversation.get("provider_id") or "")
        model_key = f"online:{provider_id}" if provider_id else ""
    profile = app.config.profile(model_key)
    getter = app.config.generation_options
    try:
        options = dict(getter(model_key))
    except TypeError as first_error:
        try:
            options = dict(getter())
        except TypeError:
            raise first_error
    options["stream"] = bool(conversation.get("stream_enabled", 1))
    # 本地锁占用者标签：子代理是本地锁最常见的「隐形占用者」——它跑在自己的后台 Job 里，
    # 占住全进程唯一的本地锁时主对话只能显示「等待本地模型资源」，无从归因。
    options["lock_label"] = "子代理"
    agent = app.config.get_agent(str(conversation.get("agent_id") or "")) or {}
    # 系统提示词只有一个来源：Agent（会话级系统提示词已移除）。
    combined_prompt = str(agent.get("system_prompt") or "").strip() or str(
        app.config.data.get("agent_system_prompt", "")
    )
    requested = [str(t) for t in (params.get("allowed_tools") or [])]
    if "allowed_tools" in params:
        allowed_tools = [t for t in requested if t not in SUBAGENT_BLOCKED_TOOLS]
    else:
        allowed_tools = list(SUBAGENT_ALLOWED_TOOLS)
    allowed_tools = [t for t in allowed_tools if t not in SUBAGENT_BLOCKED_TOOLS]
    skill_policy = params.get("skill_policy") or {"mode": "auto", "skill_ids": []}

    emit({"type": "job_status", "status": "running", "current_step": "子 Agent 推理中"})
    # 子 Agent 继承"该会话的工作区 + 审批档位"（不得扩大权限）：
    # 直接用 app.executor 会让判定/执行落到启动期默认工作区——会话工作区内的路径被判越界，
    # 且 run_context 缺 executor 时 unpack_skill_archive 等工具取不到工作区（必然失败）。
    try:
        session_workspace = app.config.resolve_workspace_dir(
            str(conversation.get("workspace_dir") or "").strip() or None
        )
    except (OSError, ValueError):
        session_workspace = app.config.resolve_workspace_dir()
    base_executor = app.executor
    sub_executor = (
        base_executor.clone_for_permission(str(conversation.get("permission_mode") or "auto"))
        if callable(getattr(base_executor, "clone_for_permission", None))
        else base_executor
    )
    if hasattr(sub_executor, "workspace"):
        sub_executor.workspace = Path(session_workspace).expanduser().resolve()
    if hasattr(sub_executor, "debug_label"):
        sub_executor.debug_label = f"subagent:{job_id}"
    worker = SkillAgent(
        app.catalog,
        CraftToolExecutor(sub_executor),
        app.models.complete,
        getattr(app, "media_collector", None),
    )
    try:
        brain_has_vision = bool(
            app.vision.brain_supports_images(profile)
            if callable(getattr(getattr(app, "vision", None), "brain_supports_images", None))
            else False
        )
    except Exception:
        brain_has_vision = False
    try:
        session_defs = (
            app.vision.session_tool_defs(brain_has_vision)
            if callable(getattr(getattr(app, "vision", None), "session_tool_defs", None))
            else None
        )
    except Exception:
        session_defs = None
    run_context: RunContext = {
        "run_id": job_id,
        "job_id": job_id,
        "conversation_id": conversation_id,
        "owner_session_id": spec.owner_session_id or conversation_id,
        "parent_job_id": spec.parent_job_id or "",
        "depth": 1,
        "allowed_tools": list(allowed_tools),
        "skill_policy": dict(skill_policy),
        "job_registry": app.jobs,
        "model_has_vision": brain_has_vision,
        "tool_defs": session_defs,
        # 工作区/执行器/取消信号随会话传入（与主会话同源）：判定、执行、产物目录一致
        "workspace_dir": str(session_workspace),
        "executor": sub_executor,
        "cancel_event": cancel,
    }
    try:
        content, runs, reasonings, usage = worker.run(
            instruction,
            history,
            profile,
            options,
            skill_policy,
            [],
            combined_prompt,
            allowed_tools,
            emit,
            cancel,
            tool_registry=app.tool_registry,
            run_context=run_context,
        )
        app.storage.update_job(
            job_id,
            # 供父模型看到的子 Agent 结果：tool_runs 走模型可见序列化（与主会话一致），
            # usage 是宿主诊断信息，不进入父模型上下文。
            result={"response": content, "tool_runs": [model_visible_run(run) for run in runs]},
        )
    except TaskCancelled:
        raise
    except Exception as exc:
        traceback.print_exc()
        app.storage.update_job(job_id, result={"error": str(exc)})


def subagent_handler_factory(app: AppContext) -> Callable[..., tuple[bool, str]]:
    """构造 ``subagent`` 系统工具处理器。"""

    def handler(
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: RunContext | None = None,
    ) -> tuple[bool, str]:
        ctx = run_context or {}
        parent_depth = int(ctx.get("depth", 0) if isinstance(ctx.get("depth"), int) else 0)
        if parent_depth >= MAX_SUBAGENT_DEPTH - 1:
            return False, "子 Agent 不能再创建子 Agent（已达到最大委派深度）"
        conversation_id = str(ctx.get("conversation_id") or "")
        if not conversation_id:
            return False, "无法确定子 Agent 所属对话"
        parent_job_id = str(ctx.get("run_id") or ctx.get("job_id") or "")
        owner = str(ctx.get("owner_session_id") or conversation_id)

        # 单父最多 4 个子 Agent
        children = [
            j for j in app.jobs.list(owner=owner)
            if str(j.get("parent_job_id") or "") == parent_job_id
        ]
        if len(children) >= MAX_CHILDREN_PER_PARENT:
            return False, f"单父 Agent 最多 {MAX_CHILDREN_PER_PARENT} 个子 Agent"

        instruction = str((arguments or {}).get("instruction") or "").strip()
        if not instruction:
            return False, "缺少 instruction 参数"
        parent_allowed = [str(tool) for tool in ctx.get("allowed_tools") or []]
        requested_tools = [str(tool) for tool in (arguments or {}).get("allowed_tools", [])]
        child_tools = requested_tools or parent_allowed
        child_tools = [
            tool for tool in child_tools
            if tool in parent_allowed and tool not in SUBAGENT_BLOCKED_TOOLS
        ]
        label = str((arguments or {}).get("label") or "")[:120] or "子 Agent 任务"
        spec = JobSpec(
            kind="subagent",
            conversation_id=conversation_id,
            params={
                "instruction": instruction,
                "allowed_tools": child_tools,
                "skill_policy": dict(ctx.get("skill_policy") or {"mode": "auto", "skill_ids": []}),
            },
            label=label,
            parent_job_id=parent_job_id or None,
            owner_session_id=owner,
        )
        job_id = app.jobs.start(spec, owner=owner)
        return True, f"子 Agent 已启动，Job ID：{job_id}（可用 job_wait 等待结果）"

    return handler


def job_tool_handler_factory(app: AppContext) -> dict[str, Callable[..., tuple[bool, str]]]:
    """构造 run_in_background / job_output / job_status / job_wait / job_kill 处理器。"""
    jobs: JobRegistry = app.jobs

    def _owner(ctx: dict[str, Any] | None) -> str:
        ctx = ctx or {}
        return str(ctx.get("owner_session_id") or ctx.get("conversation_id") or "")

    def _missing_message(job_id: str) -> str:
        """区分「Job 记录已被清理」与「Job ID 从未存在」，避免含糊的“无权访问”。"""
        try:
            cleaned = app.storage.is_job_cleaned(job_id)
        except Exception:  # noqa: BLE001 - 诊断失败不影响主流程
            cleaned = False
        if cleaned:
            return (
                f"Job {job_id} 的记录已被清理（历史任务被清理后无法再读取输出）。"
                "如需恢复，请在新对话中说明意图，重新创建 Job。"
            )
        return (
            f"Job {job_id} 不存在或无从访问：当前实例中没有该 Job 的真实记录。"
            "不得猜测 Job ID，请先调用 run_in_background、comfyui_batch 或 subagent 创建真实 Job。"
        )

    def run_in_background(arguments, active_skills, run_context: RunContext | None = None):
        ctx = run_context or {}
        conversation_id = str(ctx.get("conversation_id") or "")
        if not conversation_id:
            return False, "无法确定 Job 所属对话"
        spec_dict = (arguments or {}).get("spec") or {}
        if not isinstance(spec_dict, dict) or not spec_dict.get("kind"):
            return False, "spec 必须包含 kind"
        owner = _owner(ctx)
        params = dict(spec_dict.get("params", {}))
        if str(spec_dict.get("kind") or "") == "comfyui" and not (
            isinstance(params.get("workflow"), dict)
            or (isinstance(params.get("workflows"), list) and params.get("workflows"))
        ):
            return False, "comfyui Job 必须提供 workflow/workflows；已有 API 工作流时请优先调用 comfyui_batch"
        idempotency_key = str((arguments or {}).get("idempotency_key") or "").strip()[:200]
        if idempotency_key:
            for existing in jobs.list(owner=owner, active_only=True):
                snapshot = app.storage.get_run_snapshot(str(existing.get("id") or "")) or {}
                existing_params = snapshot.get("params") if isinstance(snapshot, dict) else {}
                if isinstance(existing_params, dict) and existing_params.get("_idempotency_key") == idempotency_key:
                    return True, f"Job 已存在，复用 Job ID：{existing['id']}"
            params["_idempotency_key"] = idempotency_key
        params.setdefault("skill_policy", dict(ctx.get("skill_policy") or {"mode": "auto", "skill_ids": []}))
        params.setdefault("allowed_tools", [str(tool) for tool in ctx.get("allowed_tools") or []])
        spec = JobSpec(
            kind=str(spec_dict["kind"]),
            conversation_id=conversation_id,
            params=params,
            label=str(spec_dict.get("label") or "")[:120],
            parent_job_id=str(ctx.get("run_id") or ctx.get("job_id") or "") or None,
            owner_session_id=owner,
            resumable=bool(spec_dict.get("resumable", False)),
            checkpoint=spec_dict.get("checkpoint"),
        )
        job_id = jobs.start(spec, owner=owner)
        return True, f"Job 已提交，Job ID：{job_id}（可用 job_wait 等待结果）"

    def job_output(arguments, active_skills, run_context: RunContext | None = None):
        # 只读操作：允许跨对话/跨会话查询 Job 输出（Job ID 由创建方告知或经 resume 记录）。
        job_id = str((arguments or {}).get("job_id") or "")
        if not job_id:
            return False, "缺少 job_id"
        if not jobs.get(job_id):
            return False, _missing_message(job_id)
        out = jobs.read(job_id, int((arguments or {}).get("cursor", 0) or 0))
        events = out.get("events", [])
        text = "\n".join(
            str(e.get("line") or e.get("message") or e.get("content") or "") for e in events
        )
        base = text or "（暂无增量输出）"
        # 增量游标闭环：告知推进后的 cursor，模型可凭它读取后续增量。
        cursor = out.get("cursor")
        if cursor is not None and events:
            base += f"\n…（游标已推进到 {cursor}；如需后续增量请传 cursor={cursor}）"
        return True, base

    def job_status(arguments, active_skills, run_context: RunContext | None = None):
        # 只读操作：允许跨对话/跨会话查询 Job 状态。
        job_id = str((arguments or {}).get("job_id") or "")
        if not job_id:
            return False, "缺少 job_id"
        job = jobs.get(job_id)
        if not job:
            return False, _missing_message(job_id)
        if job["status"] == "completed":
            note = "Job 已完成，可调用 job_output 读取最终结果"
        elif job["status"] == "interrupted":
            note = "Job 已中断（如服务曾重启），记录仍可查看，但不会自动续跑；可只读查看其输出"
        elif job["status"] in ("failed", "cancelled"):
            note = f"Job 已结束（{job['status']}），无法继续执行；{job.get('error') or ''}".strip()
        elif job["status"] in ("queued", "running", "waiting", "stopping"):
            note = "Job 正在运行，可调用 job_wait 等待完成"
        else:
            note = f"Job 状态：{job['status']}"
        return True, (
            f"Job {job_id}：status={job['status']}, progress={job['progress']:.0f}%, "
            f"step={job['current_step']}, attempt={job['attempt']}；{note}"
        )

    def job_wait(arguments, active_skills, run_context: RunContext | None = None):
        # 只读操作：允许跨对话/跨会话等待 Job 结果。
        job_id = str((arguments or {}).get("job_id") or "")
        if not job_id:
            return False, "缺少 job_id"
        if not jobs.get(job_id):
            return False, _missing_message(job_id)
        timeout = int((arguments or {}).get("timeout", 600) or 600)
        try:
            job = jobs.wait(job_id, timeout)
        except LookupError:
            return False, _missing_message(job_id)
        if not job:
            return False, _missing_message(job_id)
        if job["status"] == "completed":
            return True, f"Job 已完成：{json_dumps(job.get('result', {}))[:2000]}"
        return False, f"Job 结束（{job['status']}）：{job.get('error', '')}"

    def job_kill(arguments, active_skills, run_context: RunContext | None = None):
        # 写操作：保留归属，只能取消发起该 Job 的会话（跨对话不可取消）。
        owner = _owner(run_context)
        job_id = str((arguments or {}).get("job_id") or "")
        if not job_id:
            return False, "缺少 job_id"
        job = jobs.get(job_id)
        if not job:
            return False, "Job 不存在或无权访问"
        stored_owner = str(job.get("owner_session_id") or "")
        if owner and stored_owner and stored_owner != owner:
            return (
                False,
                "无权取消该 Job：它由另一个会话创建。如需获取结果，请改用只读的 "
                "job_output / job_status / job_wait（跨对话可用）；无需取消也不应猜测 Job ID。",
            )
        job = jobs.cancel(job_id, owner=owner, reason=str((arguments or {}).get("reason") or "用户取消"))
        if not job:
            return False, "Job 不存在或无权访问"
        return True, f"已发送取消信号给 Job {job_id}"

    return {
        "run_in_background": run_in_background,
        "job_output": job_output,
        "job_status": job_status,
        "job_wait": job_wait,
        "job_kill": job_kill,
    }


def json_dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
