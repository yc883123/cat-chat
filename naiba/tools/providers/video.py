# -*- coding: utf-8 -*-
"""video 域工具 Provider：probe_video / extract_frames 单一定义。

实现函数签名与 documents 域一致（ctx 构造注入、显式参数）；缓存目录按"当前数据目录"
动态获取（data_dir_getter 注入 app.paths.data_dir，防 rebind 漂移——教训 17 同构：
上下文/路径必须显式传递，不闭包捕获装配期可变状态）。

权限策略：
- probe_video：与 read_file / read_pdf 同构（_make_read_policy：工作区/Skill 根内免确认，
  越界必确认）；
- extract_frames：只写宿主管控缓存目录（服务层 path_within 校验），无确认
  （permission="auto"）；缓冲产物受 /api/imaging/clean 与统计覆盖。

错误语义：业务校验失败抛 ValueError（引擎/调用方转为"失败+明确文本"给模型）；
缺解码依赖（av）也走 ValueError（明确引导文案，见 naiba.video._load_av）。
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Callable

from naiba import video as video_svc
from naiba.tools.providers.core import ToolContext, _make_read_policy, ctx_for_run
from naiba.tools.registry import ToolSpec, build_video_tool_specs


def _no_confirm_policy(
    tool: str,
    arguments: dict[str, Any],
    active_skills: list[dict[str, Any]],
    permission_mode: str,
    run_context: dict[str, Any] | None,
    workspace: Path | None = None,
) -> str:
    """抽帧只写宿主管控缓存目录（服务层 path_within 校验），无需用户确认。"""
    return ""


def _resolve_any_path(ctx: ToolContext, raw: Any, active_skills: list[dict[str, Any]]) -> Path:
    """与 core 同构的路径解析（相对路径按工作区/Skill 根），允许任意绝对路径。"""
    from naiba.tools.providers.core import _resolve_read_path

    return _resolve_read_path(ctx, raw, active_skills)


def _probe_video_impl(
    ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]], data_dir: Path
) -> str:
    path = _resolve_any_path(ctx, args.get("path"), active_skills)
    return json.dumps(video_svc.probe_video(path), ensure_ascii=False)


def _extract_frames_impl(
    ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]], data_dir: Path
) -> str:
    path = _resolve_any_path(ctx, args.get("path"), active_skills)
    result = video_svc.extract_frames(
        path,
        data_dir,
        mode=args.get("mode", "interval"),
        times=args.get("times"),
        interval=args.get("interval", 2),
        threshold=args.get("threshold", video_svc.VIDEO_KEYFRAME_THRESHOLD),
        start=args.get("start"),
        end=args.get("end"),
        max_count=args.get("max", video_svc.VIDEO_FRAME_DEFAULT_MAX),
        max_edge=args.get("max_edge", video_svc.VIDEO_FRAME_MAX_EDGE),
        contact_sheet=args.get("contact_sheet", True),
    )
    return json.dumps(result, ensure_ascii=False)


_IMPLS: dict[str, Callable[..., str]] = {
    "probe_video": _probe_video_impl,
    "extract_frames": _extract_frames_impl,
}

# 精确逐帧的拒绝码：模型看到它就知道"这条路本轮没被授权"，而不是去改参数重试。
EXACT_INTENT_REQUIRED = "video_frames_intent_required"
EXACT_CONTEXT_REQUIRED = "video_frames_context_required"
EXACT_JOB_REQUIRED = "video_frames_job_unavailable"

# 活动态（与 JobRegistry 的"仍可复用"口径一致）：同视频同范围只允许一个活动 Job。
_EXACT_ACTIVE_STATUS = frozenset({"queued", "running", "waiting", "stopping", "cancelling"})


def _exact_error(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error": code, "message": message}, ensure_ascii=False)


def _exact_manifest_api(job_id: str) -> str:
    return f"/api/jobs/{job_id}/manifest"


def _find_reusable_exact_job(
    registry: Any, owner: str, video_key: str, plan: dict[str, Any]
) -> dict[str, Any] | None:
    """同 owner + 同视频指纹 + 同范围的活动逐帧 Job（幂等复用，避免重复抽同一段视频）。

    Job 参数存在创建时的 run snapshot 里（``JobRegistry.start`` 写入），不在
    ``/api/jobs`` 的快照里——逐帧参数含完整范围与路径，没必要出现在列表接口。
    """
    storage = getattr(getattr(registry, "app", None), "storage", None)
    if storage is None:
        return None
    try:
        rows = storage.list_background_tasks("", True)
    except Exception:  # noqa: BLE001 - 查询失败按"没有可复用"处理（新建 Job 更安全）
        return None
    for row in rows:
        if str(row.get("kind") or "") != "video_frames":
            continue
        if str(row.get("owner_session_id") or "") != owner:
            continue
        if str(row.get("status") or "") not in _EXACT_ACTIVE_STATUS:
            continue
        try:
            snapshot = storage.get_run_snapshot(str(row.get("id") or "")) or {}
        except Exception:  # noqa: BLE001
            continue
        params = snapshot.get("params") if isinstance(snapshot.get("params"), dict) else {}
        if str(params.get("video_key") or "") != video_key:
            continue
        if params.get("plan") != plan:
            continue
        return {"id": str(row.get("id") or ""), "status": str(row.get("status") or "")}
    return None


class VideoToolProvider:
    """video 域：2 个抽帧工具单一定义（schema × 实现函数 + def 级策略）。"""

    def __init__(self, context: ToolContext, data_dir_getter: Callable[[], Path]) -> None:
        self._context = context
        self._data_dir = data_dir_getter

    def _make_execute(self, impl: Callable[..., str]) -> Callable[..., tuple[bool, str]]:
        """工厂：为单个工具创建 execute 绑定（循环内直接捕获会共享最后一个 impl）。"""

        def _execute(
            arguments: dict[str, Any],
            active_skills: list[dict[str, Any]],
            run_context: dict[str, Any] | None = None,
        ) -> tuple[bool, str]:
            try:
                # 执行侧与判定侧同源：按当前运行工作区派生 ctx（与 documents 域同构）。
                result = impl(ctx_for_run(self._context, run_context), arguments, active_skills, self._data_dir())
                return True, result
            except ValueError as exc:
                return False, str(exc)
            except OSError as exc:
                return False, f"文件操作失败：{exc}"
            except Exception as exc:  # noqa: BLE001 - 工具边界：不吞，转明确失败文本给模型
                return False, f"视频处理失败：{type(exc).__name__}: {exc}"

        return _execute

    def _make_exact_execute(self) -> Callable[..., tuple[bool, str]]:
        """精确逐帧：本工具**创建分离式 Job**，不直接解码（解码在 Job worker 里跑）。

        四道门缺一不可（计划 2026-10-22 §4.2）：意图门（用户原文明确要求逐帧）→
        上下文门（子 Agent / Plan / 辅助对话拒绝）→ 范围与容量预检（创建前整项拒绝）
        → 幂等复用（同 owner + 同视频指纹 + 同范围的活动 Job 直接返回）。
        返回给模型的只有紧凑摘要 + manifest 接口引用，**不是**几百条帧路径。
        """

        def _execute(
            arguments: dict[str, Any],
            active_skills: list[dict[str, Any]],
            run_context: dict[str, Any] | None = None,
        ) -> tuple[bool, str]:
            ctx = run_context if isinstance(run_context, dict) else {}
            if not bool(ctx.get("video_frames_intent")):
                # fail closed：没有授权门就不创建 Job、不写盘。模型无法自证意图，
                # 也不能靠"换个参数再来一次"绕过——只有用户原话能让下一轮重新置位。
                return False, _exact_error(
                    EXACT_INTENT_REQUIRED,
                    "本轮用户没有明确要求逐帧/连续帧抽取，不能使用本工具。"
                    "需要抽样看几帧时改用 extract_frames；确实要逐帧，请等用户明确说明。",
                )
            try:
                depth = int(ctx.get("depth") or 0)
            except (TypeError, ValueError):
                depth = 0
            if depth > 0:
                return False, _exact_error(
                    EXACT_CONTEXT_REQUIRED, "子 Agent 不能发起逐帧抽取：整段视频逐帧落盘代价很高，须由主对话发起。"
                )
            if str(ctx.get("interaction_mode") or "").strip().lower() == "plan":
                return False, _exact_error(
                    EXACT_CONTEXT_REQUIRED, "计划（Plan）阶段不能发起逐帧抽取：请在实施阶段由主对话发起。"
                )
            registry = ctx.get("job_registry")
            owner = str(ctx.get("owner_session_id") or ctx.get("conversation_id") or "")
            conversation_id = str(ctx.get("conversation_id") or "")
            run_id = str(ctx.get("run_id") or "")
            if registry is None or not owner or not conversation_id or not run_id:
                # 辅助对话/后台 Job 没有 job_registry 或没有来源 Run：直接拒绝，不猜。
                return False, _exact_error(
                    EXACT_JOB_REQUIRED, "当前上下文不能启动逐帧任务（缺少任务通道），请在主对话中发起。"
                )
            try:
                path = _resolve_any_path(
                    ctx_for_run(self._context, ctx), arguments.get("path"), active_skills
                )
            except (OSError, ValueError) as exc:
                return False, f"视频路径无法解析：{exc}"
            data_dir = self._data_dir()
            try:
                plan = video_svc.resolve_exact_range(
                    path,
                    start=arguments.get("start"),
                    end=arguments.get("end"),
                    start_frame=arguments.get("start_frame"),
                    end_frame=arguments.get("end_frame"),
                )
                video_svc.check_exact_capacity(data_dir, path, plan)
            except video_svc.ExactRangeError as exc:
                # 创建前整项拒绝：不建 Job、不写一帧、不截断。
                return False, f"逐帧范围校验失败：{exc}"
            except ValueError as exc:
                return False, f"视频无法读取：{exc}"
            max_edge = arguments.get("max_edge", video_svc.EXACT_DEFAULT_MAX_EDGE)
            try:
                video_key = video_svc.video_fingerprint(path)
            except ValueError as exc:
                return False, f"视频无法读取：{exc}"
            # 幂等：同一 owner 下同视频指纹 + 同范围的**活动** Job 直接复用，
            # 避免模型重试/多轮追问时把同一段视频抽两遍（每遍几百张 PNG）。
            reused = _find_reusable_exact_job(registry, owner, video_key, plan)
            if reused:
                return True, json.dumps(
                    {
                        "ok": True,
                        "reused": True,
                        "job_id": str(reused.get("id") or ""),
                        "status": str(reused.get("status") or ""),
                        "range": plan,
                        "estimated_frames": int(plan.get("estimated_frames") or 0),
                        "manifest_api": _exact_manifest_api(str(reused.get("id") or "")),
                    },
                    ensure_ascii=False,
                )
            from naiba.jobs import JobSpec

            spec = JobSpec(
                kind="video_frames",
                conversation_id=conversation_id,
                params={
                    "path": str(path),
                    "plan": plan,
                    "max_edge": max_edge,
                    "video_key": video_key,
                },
                label=f"逐帧抽取：{path.name}",
                # detached：与主聊天并行，不占会话互斥位、不进 list_primary、聊天收尾不等待。
                # parent_job_id 保持为空（父子关系会带来取消级联，见 plan §4.3）。
                parent_job_id=None,
                owner_session_id=owner,
                detached=True,
                origin_run_id=run_id,
            )
            try:
                job_id = registry.start(spec, owner=owner)
            except Exception as exc:  # noqa: BLE001 - 工具边界：转明确失败文本
                return False, f"逐帧任务创建失败：{type(exc).__name__}: {exc}"
            return True, json.dumps(
                {
                    "ok": True,
                    "reused": False,
                    "job_id": job_id,
                    "status": "queued",
                    "range": plan,
                    "estimated_frames": int(plan.get("estimated_frames") or 0),
                    "manifest_api": _exact_manifest_api(job_id),
                    "note": "任务已提交到后台；抽取完成不等于已逐帧分析或已生成提示词。",
                },
                ensure_ascii=False,
            )

        return _execute

    def tools(self) -> list[ToolSpec]:
        rows: list[ToolSpec] = []
        for spec in build_video_tool_specs():
            if spec.name == "extract_video_frames_exact":
                # system=True：它要直接创建 Job（与 run_in_background 同类），授权由
                # run/chat.py 装配的意图门负责，不经过确认引擎。
                rows.append(
                    dataclasses.replace(
                        spec, execute=self._make_exact_execute(), system=True, policy=None
                    )
                )
                continue
            impl = _IMPLS.get(spec.name)
            if impl is None:
                continue
            policy = _make_read_policy(self._context) if spec.name == "probe_video" else _no_confirm_policy
            rows.append(dataclasses.replace(spec, execute=self._make_execute(impl), policy=policy))
        return rows
