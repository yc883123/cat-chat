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

    def tools(self) -> list[ToolSpec]:
        rows: list[ToolSpec] = []
        for spec in build_video_tool_specs():
            impl = _IMPLS[spec.name]
            policy = _make_read_policy(self._context) if spec.name == "probe_video" else _no_confirm_policy
            rows.append(dataclasses.replace(spec, execute=self._make_execute(impl), policy=policy))
        return rows
