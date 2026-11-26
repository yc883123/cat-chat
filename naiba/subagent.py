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

from naiba.core.contracts import (AppContext, JOB_WAIT_OUTCOME_KEY, JOB_WAIT_SHORT_WAIT_SECONDS,
                                  RUN_ORIGIN_USER, RunContext, SUPER_AGENT_BINDING_KEY,
                                  SUPER_AGENT_DEPENDENCY_KEY, SUBAGENT_OUTCOME_KEY)

import copy
import traceback
import time
from pathlib import Path
from typing import Any, Callable

from naiba.jobs import JOB_ACTIVE, JobRegistry, JobSpec
from naiba.core.history import build_model_history, local_brain
from naiba.core.tool_results import model_visible_run
from naiba.core.messages import dependency_message_content

MAX_SUBAGENT_DEPTH = 2
MAX_CHILDREN_PER_PARENT = 4

# ``run_in_background`` 接受的 Job 类型（与 ``JobRegistry._workers`` 的实际注册表对齐）。
# 旧 schema 把子 Agent 类型展示成 `agent`，但注册表里只有 `subagent` worker——这里给出
# 别名归一与创建前的类型白名单，避免"先报已提交、再被未知 worker 打成失败"。
JOB_KIND_ALIASES: dict[str, str] = {"agent": "subagent"}
JOB_SUPPORTED_KINDS: frozenset[str] = frozenset({
    "shell", "check", "http_poll", "comfyui", "subagent",
})

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


def _coerce_fork(value: Any) -> bool:
    """``fork`` 归一化：缺省、空值、无法判读一律回落 ``True``（存量行为不变）。

    对齐 dsh 双模式的 seed 语义——只有显式 ``false`` 才走 spawn。
    ``fork`` **不是模型参数**，而是工具注册时固化的常量（``subagent``=True /
    ``subagent_spawn``=False，见 ``subagent_handler_factory``）：模式选择权在
    Agent 工具集的用户开关上，模型零判断。旧的 Job 记录没有 ``fork`` 字段，
    因此 resume 也走默认 fork，无需迁移。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"false", "0", "no", "off"}:
            return False
        if text in {"true", "1", "yes", "on"}:
            return True
        return True
    if isinstance(value, (int, float)):
        return bool(value)
    return True


SUBAGENT_BLOCKED_TOOLS = {
    "register_mcp",
    # 两个子代理工具同等待遇：子 Agent 不得再派生（深度 ≤2 的既定红线）。
    "subagent",
    "subagent_spawn",
    "run_in_background",
    "job_output",
    "job_status",
    "job_wait",
    "job_kill",
    # 指名出卡只属于「承载用户原始请求的普通主对话 Run」：子 Agent 的 RunContext 不置位
    # show_media_intent，出卡也只能被拒。显式挡住，别让模型看到必失败的工具。
    "show_media",
    # 精确逐帧是"单独授权 + 主对话发起"的工具：子 Agent 的 RunContext 不置位
    # video_frames_intent，且整段视频逐帧落盘代价极高。显式挡住（工具自身也会拒绝）。
    "extract_video_frames_exact",
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
        raise ValueError("缺少 instruction")
    conversation = app.storage.get_conversation(conversation_id)
    if not conversation:
        raise ValueError("对话已删除")
    # fork=true（`subagent` 工具）：回放父会话历史，思考回放限长必须与主对话同参数，
    # 否则同一会话出现两种回放字节（前缀缓存断 + 行为不一致）。
    # fork=false（`subagent_spawn` 工具）：fresh child，不播种父历史（对齐 dsh 的 seed
    # 缺省语义），无历史可回放，子代理只带自身人设 + instruction。
    frozen = params.get("execution_snapshot") if isinstance(params.get("execution_snapshot"), dict) else {}
    model_key = str(frozen.get("model_key") or conversation.get("model_key") or "")
    if not model_key:
        provider_id = str(conversation.get("provider_id") or "")
        model_key = f"online:{provider_id}" if provider_id else ""
    from naiba.config import apply_model_override

    profile = dict(app.config.profile(model_key))
    if frozen.get("model_name"):
        profile = apply_model_override(profile, str(frozen["model_name"]))
        profile["model_name"] = str(frozen["model_name"])
    else:
        profile = apply_model_override(profile, str(profile.get("model") or ""))
    # 工具集先定稿再构建历史：历史是本轮请求前缀的来源，先建历史再定稿工具集会让
    # PDF/视频处理指引按未过滤的默认全集展开，同一会话出现两种回放字节（前缀缓存断）。
    requested = [str(t) for t in (params.get("allowed_tools") or [])]
    if "allowed_tools" in params:
        allowed_tools = [t for t in requested if t not in SUBAGENT_BLOCKED_TOOLS]
    else:
        allowed_tools = list(SUBAGENT_ALLOWED_TOOLS)
    allowed_tools = [t for t in allowed_tools if t not in SUBAGENT_BLOCKED_TOOLS]
    # PDF/视频处理指引按「本次实际可用工具名集合」收窄（与主对话同口径）：
    # 默认工具集不含 read_pdf / extract_frames，指引就不展开。
    allowed_names = {str(t) for t in allowed_tools}
    # fork=true（`subagent` 工具）：回放父会话历史，思考回放限长必须与主对话同参数，
    # 否则同一会话出现两种回放字节（前缀缓存断 + 行为不一致）。
    # fork=false（`subagent_spawn` 工具）：fresh child，不播种父历史（对齐 dsh 的 seed
    # 缺省语义），无历史可回放，子代理只带自身人设 + instruction。
    # `local_image_brain` 同理由：图片降级旗标的回放判据三处调用点必须一致（core.history.local_brain），
    # 否则主对话已落旗标的老消息在子代理里又被装成真图 ⇒ 字节不一致 + 本地请求重新膨胀。
    if _coerce_fork(params.get("fork", True)):
        history = build_model_history(
            conversation.get("messages", []),
            pdf_tools="read_pdf" in allowed_names,
            video_tools="extract_frames" in allowed_names,
            local_image_brain=local_brain(profile),
            **app.config.reasoning_replay_options(),
            **app.config.image_encode_cache_options(),
        )
    else:
        history = []
    dependency = params.get(SUPER_AGENT_DEPENDENCY_KEY)
    if isinstance(dependency, dict) and dependency.get("text"):
        # Separate data from the user's instruction and all user-origin grants.
        history.append({"role": "assistant", "content": dependency_message_content("", dependency)})
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
    if not agent and params.get(SUPER_AGENT_BINDING_KEY):
        agent = app.config.get_agent(app.config.default_agent_id()) or {}
    # 系统提示词只有一个来源：Agent（会话级系统提示词已移除）。
    combined_prompt = str(agent.get("system_prompt") or "").strip() or str(
        app.config.data.get("agent_system_prompt", "")
    )
    skill_policy = params.get("skill_policy") or {"mode": "auto", "skill_ids": []}

    def live_binding_valid() -> bool:
        getter = getattr(app.storage, "get_background_task", None)
        if not callable(getter):
            return False
        job = getter(job_id) or {}
        owner = str(spec.owner_session_id or conversation_id)
        if (job.get("kind") != "subagent" or job.get("conversation_id") != conversation_id
                or str(job.get("owner_session_id") or "") != owner
                or job.get("status") not in {"queued", "running", "waiting"}
                or job.get("cancel_requested") or (cancel is not None and cancel.is_set())):
            return False
        binding = params.get(SUPER_AGENT_BINDING_KEY)
        if not binding:
            return True
        if not isinstance(binding, dict) or not str(binding.get("owner_id") or "").strip():
            return False
        try:
            from naiba.super_agent.runtime import current_execution_claims
            node = app.super_agent.get_node(str(binding["owner_id"]), str(binding["node_id"]))
            thread = app.super_agent.get_thread(str(binding["owner_id"]), str(binding["thread_id"]))
            return (node.thread_id == binding["thread_id"] and node.attempt == binding["attempt"]
                    and node.executor_type == "subagent" and node.executor_id in {"", job_id}
                    and not node.cancel_requested and node.status in {"ready", "running", "waiting_user"}
                    and thread.status in {"draft", "active", "waiting_user", "paused"}
                    and node.checkpoint.get("conversation_id") == conversation_id
                    and any(c.get("correlation") == binding.get("correlation")
                            for c in current_execution_claims(node).values()))
        except (LookupError, KeyError, ValueError, TypeError):
            return False

    def execution_guard() -> str:
        if params.get(SUPER_AGENT_BINDING_KEY) and not live_binding_valid():
            return "执行归属、取消状态或执行轮次已变化"
        fingerprint = params.get("configuration_fingerprint")
        if fingerprint:
            from naiba.super_agent.baseline import validate_execution_baseline
            denial = validate_execution_baseline(app, conversation_id, fingerprint)
            if denial:
                return denial
        return ""

    emit({"type": "job_status", "status": "running", "current_step": "推理中"})
    # 子 Agent 继承"该会话的工作区 + 审批档位"（不得扩大权限）：
    # 直接用 app.executor 会让判定/执行落到启动期默认工作区——会话工作区内的路径被判越界，
    # 且 run_context 缺 executor 时 unpack_skill_archive 等工具取不到工作区（必然失败）。
    try:
        session_workspace = app.config.resolve_workspace_dir(
            str(frozen.get("workspace_dir") or conversation.get("workspace_dir") or "").strip() or None
        )
    except (OSError, ValueError):
        if params.get(SUPER_AGENT_BINDING_KEY):
            raise ValueError("子助手工作区不可用，未执行；请重新确认任务基准")
        session_workspace = app.config.resolve_workspace_dir()
    base_executor = app.executor
    sub_executor = (
        base_executor.clone_for_permission(str(frozen.get("permission_mode") or conversation.get("permission_mode") or "confirm"))
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
        "goal_id": str(spec.goal_id or ""),
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
        # 子 Agent 跑在自己的后台 Job 里，没有前端确认卡：围栏来源动作在此直接按原文
        # 收尾（未执行），不执行、不挂起、不等待 30 分钟（计划 2026-10-01 §3.5）。
        "confirmation_ui": False,
        "execution_guard": execution_guard,
    }
    manager = getattr(app, "runs", None)
    register = getattr(manager, "register_subagent_executor", None)
    unregister = getattr(manager, "unregister_subagent_executor", None)
    registered = bool(params.get(SUPER_AGENT_BINDING_KEY) and callable(register) and callable(unregister)
                      and register(job_id, sub_executor, cancel, live_binding_valid))
    run_context["confirmation_ui"] = registered
    terminal_errors: list[str] = []

    def observed_event(payload: dict[str, Any]) -> None:
        if payload.get("type") == "run_failed":
            terminal_errors.append(str(payload.get("error") or "子助手执行未完成"))
        if "arguments" in payload:
            # The executor keeps originals; persisted/display events get only the
            # same redacted whitelist as the live confirmation projection.
            from naiba.run.manager import ConversationRunManager
            payload = {**payload, "arguments": ConversationRunManager._confirmation_parameters(payload.get("arguments"))}
        emit(payload)

    try:
        denial = execution_guard()
        if denial:
            raise ValueError(denial)
        content, runs, reasonings, usage = worker.run(
            instruction,
            history,
            profile,
            options,
            skill_policy,
            [],
            combined_prompt,
            allowed_tools,
            observed_event,
            cancel,
            tool_registry=app.tool_registry,
            run_context=run_context,
        )
        outcome = run_context.get(SUBAGENT_OUTCOME_KEY) or {
            "version": 1, "status": "failed" if terminal_errors else "completed",
            "reason_code": "agent_failed" if terminal_errors else "completed",
            "summary": terminal_errors[-1] if terminal_errors else "子助手已完成本轮执行",
            "uncompleted_actions": [], "artifact_refs": [],
        }
        app.storage.update_job(
            job_id,
            # 供父模型看到的子 Agent 结果：tool_runs 走模型可见序列化（与主会话一致），
            # usage 是宿主诊断信息，不进入父模型上下文。
            result={"response": content, "tool_runs": [model_visible_run(run) for run in runs],
                    SUBAGENT_OUTCOME_KEY: outcome},
        )
    except TaskCancelled:
        raise
    except Exception as exc:
        traceback.print_exc()
        app.storage.update_job(job_id, result={"error": str(exc)})
        raise
    finally:
        if registered:
            unregister(job_id, sub_executor)


def subagent_handler_factory(
    app: AppContext, fork: bool = True
) -> Callable[..., tuple[bool, str]]:
    """构造子代理系统工具处理器。

    ``fork`` 是**注册时固化的常量**（``subagent``=True / ``subagent_spawn``=False）：
    模式来自用户的工具集二选一，不接受模型传参、也不在运行时判断（维护说明 §九.116）。
    """

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
        # 默认任务名从 instruction 派生（「子任务：前 24 字…」）：面板行标题显示的是
        # 落库 message（label），笼统的「AI 子任务」说不清这行在干什么。
        label = str((arguments or {}).get("label") or "").strip()[:120]
        if not label:
            head = " ".join(instruction.split())[:24]
            label = f"子任务：{head}{'…' if len(' '.join(instruction.split())) > 24 else ''}"
        spec = JobSpec(
            kind="subagent",
            conversation_id=conversation_id,
            params={
                "instruction": instruction,
                "allowed_tools": child_tools,
                # fork 在注册时固化（模型不传、也不可改）：落库的永远是解析后的确定值，
                # resume 时不重算 ⇒ 同一 Job 的上下文模式恒等于创建时用户勾的那个工具。
                "fork": _coerce_fork(fork),
                "skill_policy": dict(ctx.get("skill_policy") or {"mode": "auto", "skill_ids": []}),
            },
            label=label,
            parent_job_id=parent_job_id or None,
            owner_session_id=owner,
            goal_id=str(ctx.get("goal_id") or ""),
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
        # kind 归一 + 支持面校验必须在创建之前：旧 schema 把子 Agent 类型展示成 `agent`，
        # 而 JobRegistry 只注册了 `subagent` worker。原样透传会让 Job 先被登记、再由
        # `_run_unknown` 立刻失败（模型看到的是"已提交"然后一个失败任务）。
        raw_kind = str(spec_dict.get("kind") or "").strip()
        kind = JOB_KIND_ALIASES.get(raw_kind, raw_kind)
        if kind not in JOB_SUPPORTED_KINDS:
            return False, (
                f"不支持的后台任务类型：{raw_kind}。"
                f"可用类型：{'、'.join(sorted(JOB_SUPPORTED_KINDS))}。"
            )
        owner = _owner(ctx)
        raw_params = spec_dict.get("params", {})
        if raw_params is None:
            raw_params = {}
        if not isinstance(raw_params, dict):
            return False, "spec.params 必须是对象"
        params = dict(raw_params)
        if kind == "shell":
            command = params.get("command")
            if not isinstance(command, str) or not command.strip():
                return False, "shell Job 必须提供非空的 params.command"
            params["command"] = command
            # 工作区快照：缺省时把**当前 Run 的会话工作区**冻结进 params.cwd。不能在
            # worker 启动时读全局配置——应用全局工作区后来变动会把已提交任务挪到别处。
            cwd = str(params.get("cwd") or "").strip()
            if not cwd:
                cwd = str(ctx.get("workspace_dir") or "").strip()
            if not cwd:
                try:
                    cwd = str(app.config.resolve_workspace_dir())
                except Exception:  # noqa: BLE001 - 取不到就交给 worker 的兜底
                    cwd = ""
            if cwd:
                params["cwd"] = cwd
        if kind == "comfyui" and not (
            isinstance(params.get("workflow"), dict)
            or (isinstance(params.get("workflows"), list) and params.get("workflows"))
        ):
            return False, "comfyui Job 必须提供 workflow/workflows；已有 API 工作流时请优先调用 comfyui_batch"
        if kind == "subagent":
            # 子 Agent 的必需指令在创建前校验：否则 Job 登记后才在 runner 里因缺 instruction
            # 静默结束（模型只看到一个 failed，没有可自诊的信息）。
            if not str(params.get("instruction") or "").strip():
                return False, "子 Agent Job 必须提供非空的 params.instruction"
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
            kind=kind,
            conversation_id=conversation_id,
            params=params,
            label=str(spec_dict.get("label") or "")[:120],
            parent_job_id=str(ctx.get("run_id") or ctx.get("job_id") or "") or None,
            owner_session_id=owner,
            resumable=bool(spec_dict.get("resumable", False)),
            checkpoint=spec_dict.get("checkpoint"),
            goal_id=str(ctx.get("goal_id") or ""),
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
        # ``notify_on_completion=true``：仅普通主对话顶层 Run，可为本会话拥有的真实 Job
        # 登记「后台完成后自动回复」；普通只读查询行为保持不变。
        args = arguments or {}
        ctx = run_context or {}
        slot = ctx.get("tool_call_slot") if isinstance(ctx.get("tool_call_slot"), dict) else {}
        job_id = str(args.get("job_id") or "")
        if not job_id:
            return False, "缺少 job_id"
        if not jobs.get(job_id):
            return False, _missing_message(job_id)
        timeout, timeout_error = _parse_wait_timeout(args.get("timeout", 600))
        if timeout_error:
            return False, timeout_error
        notify = args.get("notify_on_completion", False)
        if not isinstance(notify, bool):
            return False, "notify_on_completion 必须是 JSON 布尔值（true / false）"
        cancel_event = ctx.get("cancel_event")

        def record(kind: str, *, waited: bool = False, wait_id: str = "", reason: str = "") -> None:
            # 宿主控制信息：只写结构化分类，不从工具正文推断，也不让模型自报字段获得豁免。
            slot[JOB_WAIT_OUTCOME_KEY] = {
                "kind": kind, "waited": bool(waited), "wait_id": wait_id, "reason": reason,
            }

        if notify:
            targets, target_error = _notify_targets(ctx, jobs, job_id)
            if target_error:
                record("failed", reason=target_error)
                return False, target_error
            states = _target_states(jobs, targets)
            if all(state["terminal"] for state in states):
                # 已经全部终态：当场返回真实结果，不创建无用续答。
                record("completed")
                return True, _jobs_result_text(jobs, targets)
            coordinator = getattr(app, "job_waits", None)
            if coordinator is None:
                record("failed", reason="等待登记不可用")
                return False, "当前环境无法登记后台通知；可继续用 job_wait 同步等待。"
            getter = getattr(app.storage, "get_run_snapshot", None)
            origin = getter(str(ctx.get("run_id") or "")) if callable(getter) else {}
            snapshot = job_wait_execution_snapshot(origin)
            # 只补 RunContext 已有的真实字段；model_name / Agent 必须来自原 Run，
            # 不能从此刻会话或配置猜测。白名单不复制 profile、Key 或运行期对象。
            for key in ("model_key", "allowed_tools", "workspace_dir", "conv_kind", "skill_policy"):
                if key not in snapshot and key in ctx:
                    snapshot[key] = copy.deepcopy(ctx[key])
            wait_record, note = coordinator.register(
                conversation_id=str(ctx.get("conversation_id") or ""),
                origin_run_id=str(ctx.get("run_id") or ""),
                target_job_ids=targets,
                snapshot=snapshot,
            )
            if wait_record is None:
                record("failed", reason=note)
                return False, f"未能登记自动回复：{note}。可继续用 job_wait 同步等待。"
            wait_id = str(wait_record.get("id") or "")
            short_wait = min(JOB_WAIT_SHORT_WAIT_SECONDS, timeout)
            try:
                _wait_for_targets(jobs, targets, short_wait, cancel_event)
            except TaskCancelled:
                raise
            states = _target_states(jobs, targets)
            if all(state["terminal"] for state in states):
                # 短等期间就完成了：撤掉刚登记的等待，直接当场回答。
                coordinator.cancel_wait(wait_id, "等待在交接前完成")
                record("completed")
                return True, _jobs_result_text(jobs, targets)
            record("handoff", wait_id=wait_id)
            slot["message"] = "已登记，后台任务结束后会在这里回复。"
            return True, (
                "已登记后台等待：本次先等了 "
                f"{short_wait:.0f} 秒，任务仍在运行。"
                f"目标 Job：{'、'.join(targets)}。"
                "任务结束后程序会在这里自动回复一次结果，本轮不需要继续等待；"
                "不要重复创建同一任务，也不要口头承诺未经登记的通知。"
            )

        waited = timeout > 0
        try:
            job = jobs.wait(job_id, timeout, cancel_event=cancel_event)
        except LookupError:
            return False, _missing_message(job_id)
        except TaskCancelled:
            raise
        if not job:
            return False, _missing_message(job_id)
        status = str(job.get("status") or "")
        progress = float(job.get("progress") or 0)
        step = str(job.get("current_step") or "")
        if status == "completed":
            record("completed", waited=waited)
            return True, f"Job 已完成：{json_dumps(job.get('result', {}))[:2000]}"
        if status in {"queued", "running", "waiting", "stopping", "cancelling"}:
            # 「本次等待到期、任务仍在运行」是**正常快照**，不是失败：旧实现把它和真正的
            # 失败统一成「Job 结束（running）」，模型据此认定任务失败并重复提交付费生成
            # （计划 2026-10-22 §3.3）。这里返回运行中并给出下一步，不取消任务。
            record("pending_timeout", waited=waited)
            return True, (
                f"本次等待到期，Job 仍在运行：status={status}, progress={progress:.0f}%, "
                f"step={step}。可稍后再调用 job_wait / job_status 继续收集；不要重复创建同一任务。"
            )
        if status == "interrupted":
            record("interrupted", waited=waited)
            return False, (
                f"Job 已中断（{job.get('error') or '如服务曾重启'}），不会自动续跑；"
                "可只读查看其输出，需要时由用户决定是否重新发起。"
            )
        if status == "cancelled":
            record("cancelled", waited=waited)
            return False, f"Job 已被取消：{job.get('error') or '用户取消'}"
        # failed：真正的失败，与「等待到期」区分开。
        record("failed", waited=waited)
        return False, f"Job 已失败：{job.get('error') or '原因未记录'}"

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


# ---- job_wait 的宿主侧校验与分类（计划 2026-11-16 §3.1 / §4.1）----
def job_wait_execution_snapshot(value: Any) -> dict[str, Any]:
    """只保留原 Run 已冻结的执行字段，不复制凭证、历史或运行期上下文。"""
    if not isinstance(value, dict):
        return {}
    keys = (
        "model_key", "model_name", "provider_id", "agent", "skill_policy",
        "allowed_tools", "workspace_dir", "conv_kind", "interaction_mode",
        "stream_enabled", "chat_supports_images", "generation_options",
        "web_search_enabled", "deep_reasoning_enabled", "reasoning_effort", "permission_mode",
    )
    return {key: copy.deepcopy(value[key]) for key in keys if key in value}


def _parse_wait_timeout(value: Any) -> tuple[int, str]:
    """严格解析 timeout：拒绝零/负数/非法值，不再用 ``or 600`` 把 0 暗改成 600。"""
    if isinstance(value, bool):
        return 0, "timeout 必须是正整数秒（0 与负数不合法）"
    if isinstance(value, float):
        if not value.is_integer():
            return 0, "timeout 必须是整数秒"
        value = int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return 0, "timeout 必须是正整数秒"
        value = int(text)
    if not isinstance(value, int):
        return 0, "timeout 必须是正整数秒"
    if value <= 0:
        return 0, "timeout 必须大于 0；等待到期用任务自身的状态判断，不要传 0"
    if value > 86400:
        return 0, "timeout 不能超过 86400 秒"
    return value, ""


def _target_states(jobs: JobRegistry, target_job_ids: list[str]) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for job_id in target_job_ids:
        job = jobs.get(job_id)
        status = str((job or {}).get("status") or "")
        # 记录被清理/不存在：无法核实，不再无限等它（与协调器的口径一致）。
        terminal = job is None or status in {"completed", "failed", "cancelled", "interrupted"}
        states.append({"id": job_id, "status": status or "missing", "terminal": terminal, "job": job})
    return states


def _wait_for_targets(
    jobs: JobRegistry, targets: list[str], seconds: float, cancel_event: Any
) -> None:
    """短等一轮：所有目标终态或到期即返回；取消信号命中抛 TaskCancelled。"""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if cancel_event is not None and cancel_event.is_set():
            from naiba.core.exceptions import TaskCancelled

            raise TaskCancelled("等待已停止")
        if all(state["terminal"] for state in _target_states(jobs, targets)):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def _notify_targets(
    ctx: dict[str, Any], jobs: JobRegistry, job_id: str
) -> tuple[list[str], str]:
    """校验能否登记自动回复，并收集完整目标集合（明确等待的 Job + 本 Run 活动子 Job）。"""
    conversation_id = str(ctx.get("conversation_id") or "")
    owner = str(ctx.get("owner_session_id") or conversation_id)
    run_id = str(ctx.get("run_id") or ctx.get("job_id") or "")
    if not conversation_id or not run_id:
        return [], "无法确定当前对话或运行，不能登记自动回复"
    depth = ctx.get("depth")
    if isinstance(depth, int) and depth > 0:
        return [], "子 Agent 不能登记后台自动回复；可继续用 job_wait 同步等待"
    if str(ctx.get("conv_kind") or "main") != "main":
        return [], "辅助对话不能登记后台自动回复；可继续用只读查询查看结果"
    origin = str(ctx.get("run_origin") or RUN_ORIGIN_USER)
    if origin != RUN_ORIGIN_USER:
        # 监督续办/依赖串联/等待续答由各自的调度负责，不能再叠一层普通通知。
        return [], "本轮的后续由程序已有的续办机制负责，不能重复登记自动回复"
    job = jobs.get(job_id)
    if not job:
        return [], "Job 不存在或无法访问"
    stored_owner = str(job.get("owner_session_id") or "")
    if (str(job.get("conversation_id") or "") != conversation_id
            or (stored_owner and stored_owner != owner)):
        return [], "该 Job 不属于当前会话，不能登记自动回复；可继续用只读查询查看结果"
    targets = {job_id}
    for child in jobs.children(run_id, active_only=True):
        if bool(child.get("detached")):
            continue
        child_id = str(child.get("id") or "")
        if child_id and str(child.get("status") or "") in JOB_ACTIVE:
            targets.add(child_id)
    return sorted(targets), ""


def _jobs_result_text(jobs: JobRegistry, targets: list[str]) -> str:
    """同步出口的汇总：逐目标给出真实终态（不要求固定结果字段）。"""
    lines: list[str] = []
    for state in _target_states(jobs, targets):
        job = state["job"]
        if job is None:
            lines.append(f"Job {state['id']}：无法核实（记录已被清理或不存在）")
            continue
        job_id = state["id"]
        status = state["status"]
        if status == "completed":
            lines.append(f"Job {job_id} 已完成：{json_dumps(job.get('result', {}))[:2000]}")
        elif status == "failed":
            lines.append(f"Job {job_id} 已失败：{job.get('error') or '原因未记录'}")
        elif status == "cancelled":
            lines.append(f"Job {job_id} 已取消：{job.get('error') or '用户取消'}")
        elif status == "interrupted":
            lines.append(f"Job {job_id} 已中断：{job.get('error') or '如服务曾重启'}")
        else:
            lines.append(f"Job {job_id}：status={status}")
    return "\n".join(lines)
