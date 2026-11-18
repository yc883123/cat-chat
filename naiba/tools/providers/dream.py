# -*- coding: utf-8 -*-
"""造梦机对话域工具 Provider（计划 docs/plans/2026-11-17-造梦机对话预设Agent与工具集设计.md）。

七个专用工具（dream_workflows / dream_workflow_details / dream_prepare /
dream_submit / dream_task_status / dream_task_cancel / dream_task_wait），
全部是**普通工具**（非 system）：submit / cancel 的 side_effect=True 让它们走
ToolExecutor 的真实权限链（confirm 弹动作确认卡、auto/full 免确认），
与计划 §7.8 的三档权限表逐条对应——不能用 system=True 绕开这套规则。

边界（计划 §7.3/§7.4）：
- 只读造梦机已导入的工作流；只按用户确认的用途绑定准备参数；只提交有效快照；
  只操作本会话关联任务；不接受原始工作流图、任意地址/代码、伪造 owner。
- 执行归属由宿主从 run_context 解析（当前实现只放行普通主对话顶层 Run）；
  辅助对话、子代理、计划执行、通知续答一律拒绝。总管受管 Run 形态在
  ownership 结构里预留，接入派发闭环前不会从这里放行（计划 §7.7）。
- dream_submit 不进 JOB_CREATOR_TOOL_DEPS：缺依赖时显式报缺哪些工具，
  不隐式补（计划 §7.3）。
"""
from __future__ import annotations

import copy
import dataclasses
import json
from typing import Any, Callable

from naiba.comfyui.dream import (
    DreamPrepareError,
    DreamService,
    DreamSubmitError,
    encode_wait_target,
)
from naiba.core.contracts import JOB_WAIT_OUTCOME_KEY
from naiba.core.contracts import AppContext
from naiba.tools.registry import ToolSpec, _make_dream_side_effect_policy, build_dream_tool_specs

# dream_submit 的必要依赖（计划 §7.4：submit 具备而 prepare/details 等缺失时拒绝提交）。
_DREAM_SUBMIT_REQUIRES = ("dream_prepare", "dream_workflow_details", "dream_task_status")


def _make_submit_summary_lookup(app: AppContext) -> Callable[[str], str]:
    """按快照 ID 反查预检摘要，供 dream_submit 的确认卡展示（§7.8 B9b）。

    只读快照、不产生副作用；摘要缺失 / 快照被删时返回空串（确认链退回仅工具名）。
    条目过多时只列前几条并给完整数量，避免确认卡被一次批量刷屏。
    """
    def lookup(snapshot_id: str) -> str:
        comfy = getattr(app, "comfyui", None)
        dream = getattr(comfy, "dream", None)
        snapshots = getattr(dream, "snapshots", None)
        if snapshots is None:
            return ""
        record = snapshots.get(str(snapshot_id or ""))
        if not isinstance(record, dict):
            return ""
        summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
        workflow = str(summary.get("workflow") or record.get("workflow_name") or "").strip()
        mode = str(summary.get("mode") or record.get("mode") or "single")
        parts: list[str] = []
        if workflow:
            parts.append(f"工作流「{workflow}」")
        items = summary.get("items") if isinstance(summary.get("items"), list) else []
        total = summary.get("total_executions")
        if mode == "batch":
            shown = "、".join(
                f"{str(item.get('name') or '条目')}×{item.get('shots')}"
                for item in items[:4] if isinstance(item, dict)
            )
            if len(items) > 4:
                shown += f" 等 {len(items)} 条"
            count = f"批量 {len(items)} 条" + (f" / 共 {total} 次生成" if total else "")
            parts.append(count + (f"：{shown}" if shown else ""))
        else:
            shots = summary.get("shots")
            parts.append("单次生成" + (f" {shots} 张" if shots else ""))
        size = summary.get("size") if isinstance(summary.get("size"), dict) else {}
        width, height = size.get("width"), size.get("height")
        if isinstance(width, int) and isinstance(height, int):
            parts.append(f"实际尺寸 {width}×{height}")
        return "，".join(part for part in parts if part)

    return lookup


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _ownership(run_context: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    """解析执行归属；只放行普通主对话顶层 Run（计划 §7.3）。

    总管受管 Run 的形态（kind="super_node"，带 thread/node/attempt）在这里预留：
    接入派发闭环时由宿主上下文填充，模型自报的同名字段一律不采信。
    """
    ctx = run_context or {}
    # 归属字段必须由宿主显式给出，缺字段一律拒绝（fail-closed）：不能把「没带
    # conv_kind / run_origin / depth」的来路默认成普通主对话。chat.py 的
    # RunContext 恒定带这三个键；未适配的子代理、总管受管 Run、通知续答等
    # 路径带不齐，正好由这里挡在工具之外（计划 §7.3/§7.4/§7.7）。
    if not ctx or not all(key in ctx for key in ("conv_kind", "run_origin", "depth")):
        return None, (
            "无法确定执行归属：运行上下文缺少对话形态字段，不能使用造梦机对话工具。"
            "请把生成需求发到普通主对话里。"
        )
    conv_kind = str(ctx.get("conv_kind") or "")
    run_origin = str(ctx.get("run_origin") or "")
    try:
        depth = int(ctx.get("depth"))
    except (TypeError, ValueError):
        return None, "无法确定执行归属：运行层级不合法，不能使用造梦机对话工具"
    if conv_kind != "main" or run_origin != "user" or depth != 0:
        return None, (
            "当前运行形态不能使用造梦机对话工具：只有普通主对话里的顶层回复可以"
            "读取目录、预检与提交生成（辅助对话、子代理与结果续答一律不行）。"
            "请把生成需求发到普通对话里。"
        )
    conversation_id = str(ctx.get("conversation_id") or "")
    if not conversation_id:
        return None, "无法确定当前对话，不能使用造梦机对话工具"
    return {
        "kind": "conversation",
        "conversation_id": conversation_id,
        "owner_id": str(ctx.get("owner_session_id") or conversation_id),
        "origin_run_id": str(ctx.get("run_id") or ""),
    }, ""


def _register_notification(
    app: AppContext,
    *,
    ownership: dict[str, Any],
    run_context: dict[str, Any] | None,
    targets: list[str],
) -> tuple[bool, str, str]:
    """提交成功后由宿主立即登记完成通知（计划 §7.5）。

    返回 ``(registered, wait_id, note)``；失败如实回报，绝不承诺未登记的通知。
    """
    coordinator = getattr(app, "job_waits", None)
    if coordinator is None:
        return False, "", "等待登记设施不可用"
    from naiba.subagent import job_wait_execution_snapshot

    getter = getattr(app.storage, "get_run_snapshot", None)
    origin = getter(str(ownership.get("origin_run_id") or "")) if callable(getter) else {}
    snapshot = job_wait_execution_snapshot(origin)
    # 只补 RunContext 已有的真实字段；model_name / Agent 必须来自原 Run，
    # 不能从此刻会话或配置猜测（与 job_wait 的登记同一白名单口径）。
    ctx = run_context or {}
    for key in ("model_key", "allowed_tools", "workspace_dir", "conv_kind", "skill_policy"):
        if key not in snapshot and key in ctx:
            snapshot[key] = copy.deepcopy(ctx[key])
    try:
        record, note = coordinator.register(
            conversation_id=str(ownership.get("conversation_id") or ""),
            origin_run_id=str(ownership.get("origin_run_id") or ""),
            target_job_ids=list(targets),
            snapshot=snapshot,
        )
    except Exception as exc:  # noqa: BLE001 - 登记失败不影响任务本身
        return False, "", f"等待登记失败：{type(exc).__name__}: {exc}"
    if record is None:
        return False, "", note or "未能登记自动通知（会话已有活动等待或超出上限）"
    return True, str(record.get("id") or ""), "（复用既有等待）" if note == "reuse" else ""


def _targets_from_receipt(receipt: dict[str, Any]) -> list[str]:
    kind = str(receipt.get("kind") or "")
    if kind == "comfy-batch":
        batch_id = str(receipt.get("batch_id") or "")
        return [encode_wait_target("comfy-batch", batch_id)] if batch_id else []
    task_id = str(receipt.get("task_id") or "")
    return [encode_wait_target("comfy-task", task_id)] if task_id else []


def _split_target(target: str) -> tuple[str, str]:
    raw = str(target or "").strip()
    if raw.startswith("comfy-batch:"):
        return "comfy-batch", raw[len("comfy-batch:"):]
    if raw.startswith("comfy-task:"):
        return "comfy-task", raw[len("comfy-task:"):]
    if raw.startswith("cfb-"):
        return "comfy-batch", raw
    return "comfy-task", raw


def _status_states(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
    terminal = {"completed", "failed", "cancelled", "interrupted"}
    states: list[dict[str, Any]] = []
    for row in status_payload.get("targets") or []:
        if not isinstance(row, dict):
            continue
        if row.get("error"):
            states.append({"id": row.get("id"), "terminal": True})
            continue
        task = row.get("task") if isinstance(row.get("task"), dict) else None
        batch = row.get("batch") if isinstance(row.get("batch"), dict) else None
        if task is not None:
            states.append({"id": row.get("id"), "terminal": str(task.get("status")) in terminal})
        elif batch is not None:
            item_statuses = [str((item or {}).get("status") or "") for item in batch.get("items") or []]
            active = any(status in ("queued", "running") for status in item_statuses)
            states.append({"id": row.get("id"), "terminal": bool(item_statuses) and not active})
    return states


def _make_handler(app: AppContext, name: str) -> Callable[..., tuple[bool, str]]:
    # DreamService 依赖 app.comfyui（组装根里后于工具注册表创建）：首次调用时惰性
    # 构建，避免装配顺序耦合。
    holder: dict[str, DreamService] = {}

    def _service() -> DreamService:
        service = holder.get("svc")
        if service is None:
            service = DreamService(app)
            holder["svc"] = service
        return service

    def handler(arguments, _active_skills, run_context: dict[str, Any] | None = None):
        args = arguments or {}
        ownership, denial = _ownership(run_context)
        if ownership is None:
            # 归属拒绝不需要任何服务实例：坏形态在这里短路，不触达存储。
            return False, denial
        if name == "dream_workflows":
            service = _service()
            try:
                page = max(1, min(int(args.get("page") or 1), 1000))
            except (TypeError, ValueError):
                return False, "page 必须是正整数"
            try:
                payload = service.list_workflows(page=page)
            except Exception as exc:  # noqa: BLE001 - 只读工具的兜底
                return False, f"读取工作流目录失败：{type(exc).__name__}: {exc}"
            payload["note"] = (
                "编号只在本轮列表内有效：引用工作流请使用 id 与 fingerprint；"
                "列表是实时目录，不要凭记忆假设顺序"
            )
            return True, _json(payload)

        if name == "dream_workflow_details":
            service = _service()
            workflow_id = str(args.get("workflow_id") or "").strip()
            if not workflow_id:
                return False, "缺少 workflow_id（从 dream_workflows 的结果里取）"
            try:
                param_page = max(1, min(int(args.get("param_page") or 1), 100))
            except (TypeError, ValueError):
                param_page = 1
            try:
                payload = service.workflow_details(workflow_id, param_page=param_page)
            except LookupError as exc:
                return False, str(exc)
            except Exception as exc:  # noqa: BLE001
                return False, f"读取工作流详情失败：{type(exc).__name__}: {exc}"
            return True, _json(payload)

        if name == "dream_prepare":
            service = _service()
            workflow_id = str(args.get("workflow_id") or "").strip()
            if not workflow_id:
                return False, "缺少 workflow_id（从 dream_workflows 的结果里取）"
            values = args.get("values") if isinstance(args.get("values"), dict) else {}
            items = args.get("items") if isinstance(args.get("items"), list) else None
            try:
                record = service.prepare(
                    ownership=ownership,
                    workflow_id=workflow_id,
                    mode=str(args.get("mode") or "single").strip(),
                    values=values,
                    items=items,
                    shots=args.get("shots", 1),
                    title=str(args.get("title") or ""),
                    unload_after=bool(args.get("unload_after")),
                    # 完成通知由宿主在提交成功后立即登记，不向模型开放开关（计划 §7.5）。
                )
            except LookupError as exc:
                return False, str(exc)
            except (DreamPrepareError, ValueError) as exc:
                return False, f"预检未通过：{exc}"
            except Exception as exc:  # noqa: BLE001
                return False, f"预检失败：{type(exc).__name__}: {exc}"
            summary = dict(record.get("summary") or {})
            summary["snapshot_id"] = str(record.get("id") or "")
            summary["expires_minutes"] = 30
            if summary.get("missing"):
                summary["instruction"] = (
                    "把缺失项逐条向用户说明并追问；用户明确选择沿用默认时，把该绑定的值填 "
                    "{\"keep_default\": true} 重新预检。缺失项未解决前不要提交"
                )
            else:
                summary["instruction"] = (
                    "预检通过。向用户复述实际参数与总执行次数；用户已明确要求生成时"
                    "按会话授权规则调用 dream_submit 提交"
                )
            return True, _json(summary)

        if name == "dream_submit":
            allowed = {str(item) for item in ((run_context or {}).get("allowed_tools") or [])}
            missing_deps = [tool for tool in _DREAM_SUBMIT_REQUIRES if tool not in allowed]
            if missing_deps:
                return False, (
                    "当前会话的工具集缺少提交所需的配套工具："
                    + "、".join(missing_deps)
                    + "。请新建会话并给这个 Agent 勾选完整的「造梦机对话」工具集，"
                    "不要只开 dream_submit 单件"
                )
            snapshot_id = str(args.get("snapshot_id") or "").strip()
            if not snapshot_id:
                return False, "缺少 snapshot_id（只能提交 dream_prepare 返回的快照，不接受任意参数）"
            service = _service()
            try:
                receipt = service.submit(snapshot_id)
            except DreamSubmitError as exc:
                return False, f"提交被拒绝：{exc}"
            except LookupError as exc:
                return False, str(exc)
            except Exception as exc:  # noqa: BLE001 - 受理未知也要如实回报
                return False, (
                    f"提交结果未知（{type(exc).__name__}: {exc}）：不要重新提交，"
                    "请用 dream_task_status 核对造梦机里的真实任务"
                )
            targets = _targets_from_receipt(receipt)
            notify_registered = False
            notify_note = "未登记（提交回执缺少任务/批次 ID）"
            wait_id = ""
            # 计划 §7.5：登记是宿主动作，紧随提交一起返回，不接受模型用参数关闭；
            # 登记失败也如实回报，绝不承诺一个没登记上的通知。
            if targets:
                notify_registered, wait_id, notify_note = _register_notification(
                    app, ownership=ownership, run_context=run_context, targets=targets,
                )
            payload = {
                **receipt,
                "notify_registered": notify_registered,
                "notify_wait_id": wait_id,
                "notify_note": notify_note,
                "targets": targets,
                "instruction": (
                    "已创建造梦机原生任务；任务卡片在造梦机页面可见。"
                    + ("完成或失败后程序会在这里自动回复一次结果。"
                       if notify_registered else
                       "自动通知没有登记成功，不要向用户承诺完成后的提醒；"
                       "请如实说明可在造梦机结果页查看。")
                ),
            }
            if wait_id:
                service.snapshots.update(
                    str(receipt.get("snapshot_id") or ""),
                    notify_registered=True, notify_wait_id=wait_id,
                )
            return True, _json(payload)

        if name == "dream_task_status":
            service = _service()
            targets = args.get("target_ids") if isinstance(args.get("target_ids"), list) else []
            if not targets:
                single = str(args.get("target_id") or "").strip()
                if single:
                    targets = [single]
            if not targets:
                return False, "缺少 target_ids（提交回执里的任务/批次 ID）"
            try:
                payload = service.task_status(targets, ownership)
            except (DreamPrepareError, ValueError) as exc:
                return False, str(exc)
            except Exception as exc:  # noqa: BLE001
                return False, f"查询失败：{type(exc).__name__}: {exc}"
            return True, _json(payload)

        if name == "dream_task_cancel":
            service = _service()
            target_id = str(args.get("target_id") or "").strip()
            if not target_id:
                return False, "缺少 target_id（只能取消当前对话发起的任务）"
            try:
                payload = service.cancel(target_id, ownership)
            except (LookupError, PermissionError) as exc:
                return False, str(exc)
            except (DreamSubmitError, DreamPrepareError, ValueError) as exc:
                return False, str(exc)
            except Exception as exc:  # noqa: BLE001
                return False, f"取消失败：{type(exc).__name__}: {exc}"
            payload["instruction"] = "取消请求已受理；以 dream_task_status 复核到的终态为准汇报"
            return True, _json(payload)

        if name == "dream_task_wait":
            service = _service()
            ctx = run_context or {}
            slot = ctx.get("tool_call_slot") if isinstance(ctx.get("tool_call_slot"), dict) else {}
            targets = args.get("target_ids") if isinstance(args.get("target_ids"), list) else []
            if not targets:
                single = str(args.get("target_id") or "").strip()
                if single:
                    targets = [single]
            if not targets:
                return False, "缺少 target_ids（提交回执里的任务/批次 ID）"
            typed = [encode_wait_target(*_split_target(str(target))) for target in targets]
            # 已全部终态：当场返回真实状态，不创建无用等待（登记竞态由这里收口）。
            try:
                status_payload = service.task_status(typed, ownership)
            except (DreamPrepareError, ValueError) as exc:
                return False, str(exc)
            states = _status_states(status_payload)
            if states and all(state.get("terminal") for state in states):
                if isinstance(slot, dict):
                    slot[JOB_WAIT_OUTCOME_KEY] = {"kind": "completed", "waited": False}
                return True, _json(status_payload)
            registered, wait_id, note = _register_notification(
                app, ownership=ownership, run_context=run_context, targets=typed,
            )
            if not registered:
                if isinstance(slot, dict):
                    slot[JOB_WAIT_OUTCOME_KEY] = {"kind": "failed", "reason": note}
                return False, f"未能登记完成通知：{note}。可稍后用 dream_task_status 查询状态。"
            if isinstance(slot, dict):
                slot[JOB_WAIT_OUTCOME_KEY] = {
                    "kind": "handoff", "wait_id": wait_id,
                    "message": "已登记，造梦机任务结束后会在这里回复结果。",
                }
                slot["message"] = "已登记，造梦机任务结束后会在这里回复结果。"
            return True, (
                f"已登记完成通知（目标：{'、'.join(typed)}）。"
                "任务结束后程序会在这里自动回复一次结果，本轮不需要继续等待；"
                "不要重复提交同一任务，也不要口头承诺未经登记的通知。"
            )

        return False, f"未实现的 dream 工具：{name}"

    return handler


class DreamToolProvider:
    """造梦机对话域：七个 dream_* 工具的普通工具定义（非 system）。"""

    def __init__(self, app: AppContext) -> None:
        self._handlers = {
            spec.name: _make_handler(app, spec.name)
            for spec in build_dream_tool_specs()
        }
        self._submit_summary = _make_submit_summary_lookup(app)

    def tools(self) -> list[ToolSpec]:
        rows: list[ToolSpec] = []
        for spec in build_dream_tool_specs():
            overrides: dict[str, Any] = {"execute": self._handlers[spec.name]}
            if spec.name == "dream_submit":
                # 确认卡展示工作流 / 条目 / 次数 / 尺寸摘要（§7.8 B9b）。
                overrides["policy"] = _make_dream_side_effect_policy(self._submit_summary)
            rows.append(dataclasses.replace(spec, **overrides))
        return rows
