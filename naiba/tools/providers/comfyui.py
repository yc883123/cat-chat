"""comfyui 域工具 Provider：comfyui_prepare_workflow / comfyui_batch 单一定义。

处理器与工作流规范化助手自 app.py 原样抽取（self→显式依赖：app.jobs/app.storage），
未被他处引用，整体随迁。依赖经构造参数注入（AppContext Protocol）。
"""
from __future__ import annotations

import dataclasses
import json
import secrets
from pathlib import Path
from typing import Any

from naiba.core.contracts import AppContext
from naiba.tools.registry import ToolSpec, build_comfyui_tool_specs


def _normalize_comfyui_workflow(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("工作流 JSON 必须是对象")
    # Accept the common {prompt: {...}} wrapper produced by API clients.
    candidate = value.get("prompt") if isinstance(value.get("prompt"), dict) else value
    # UI exports contain a nodes array and links; they are not POST /prompt payloads.
    if isinstance(candidate.get("nodes"), list) or isinstance(candidate.get("links"), list):
        raise ValueError("检测到 ComfyUI UI JSON，请先导出 API 格式工作流")
    if not candidate:
        raise ValueError("工作流为空")
    invalid = [key for key, node in candidate.items() if not isinstance(node, dict)]
    if invalid:
        raise ValueError(f"API 工作流节点值必须是对象：{', '.join(map(str, invalid[:5]))}")
    return candidate


def _load_comfyui_workflow(raw_path: str) -> dict[str, Any]:
    path_text = str(raw_path or "").strip()
    if not path_text:
        raise ValueError("工作流路径为空")
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() != ".json":
        raise ValueError("工作流文件必须是 .json")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("工作流文件超过 20 MB")
    value = json.loads(path.read_text(encoding="utf-8"))
    return _normalize_comfyui_workflow(value)


# 负 seed 自动随机的保守上限。ComfyUI 核心节点（KSampler 等）seed 范围是 0 ~ 2^64-1，
# 但常见的第三方节点把 seed 声明成 ±2^50（如 rgthree 的「Seed (rgthree)」，-1 表示随机）。
# 早先在这里取 randbelow(2^63)，巨数几乎必超 ±2^50 节点的 max，ComfyUI 整单 400
# （value_bigger_than_max）——2026-09-20 事故：10 张图全部死于「第 1 段提交失败」，
# 而同一文件 MCP 原样提交却能成功。取交集上限 2^50 对两类节点都合法。
_COMFYUI_SEED_RANDOM_MAX = 2 ** 50


def _normalize_comfyui_runtime_workflow(value: Any) -> dict[str, Any]:
    """Normalize an API workflow and replace invalid negative random seeds."""
    workflow = _normalize_comfyui_workflow(value)
    normalized = json.loads(json.dumps(workflow, ensure_ascii=False))
    for node in normalized.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key in ("seed", "noise_seed"):
            raw = inputs.get(key)
            if isinstance(raw, (int, float)) and raw < 0:
                inputs[key] = secrets.randbelow(_COMFYUI_SEED_RANDOM_MAX + 1)
    return normalized


def _comfyui_batch_handler(
    app: AppContext, args: dict[str, Any], _skills: Any, run_context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Submit a batch of API-format workflows through the durable JobRegistry."""
    from naiba.jobs import JobSpec

    ctx = run_context or {}
    conversation_id = str(ctx.get("conversation_id") or "")
    if not conversation_id:
        return False, "无法确定当前对话，不能创建 ComfyUI Job"
    values = args or {}
    # shots 口径：与 workflow / workflow_paths 搭配时，每个工作流重复提交 shots 次。
    # 此前 shots 只在单 workflow 分支生效，workflow_paths + shots=10 会被静默吞成 1 段
    # （2026-09-20 "total=1 — but shots=10" 事故：模型看到返回值才知道参数没生效）。
    try:
        shots = max(1, min(int(values.get("shots", 1)), 200))
    except (TypeError, ValueError):
        return False, "shots 必须是正整数"
    workflows = values.get("workflows")
    if isinstance(workflows, str):
        try:
            workflows = json.loads(workflows)
        except json.JSONDecodeError as exc:
            return False, f"workflows 字符串不是合法 JSON：{exc}"
    workflow_paths = values.get("workflow_paths")
    if isinstance(workflows, list) and workflows:
        if shots > 1:
            return False, (
                "workflows 数组已逐条列出每次提交的内容，shots 不适用；"
                "要重复提交请复制数组元素，或改用 workflow / workflow_paths 搭配 shots"
            )
    elif isinstance(workflow_paths, list) and workflow_paths:
        base: list[dict[str, Any]] = []
        for raw_path in workflow_paths:
            try:
                base.append(_load_comfyui_workflow(str(raw_path)))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return False, f"工作流文件读取失败：{exc}"
        workflows = [item for item in base for _ in range(shots)]
    else:
        one = values.get("workflow")
        if not isinstance(one, dict):
            return False, "需要 workflows 数组，或提供 workflow 对象（可与 shots 搭配重复提交）"
        workflows = [one for _ in range(shots)]
    if not isinstance(workflows, list) or not workflows or not all(isinstance(item, dict) for item in workflows):
        return False, "workflows 必须是非空的 API 工作流对象数组"
    if len(workflows) > 200:
        return False, "单次最多提交 200 个工作流"
    try:
        workflows = [_normalize_comfyui_runtime_workflow(item) for item in workflows]
    except ValueError as exc:
        return False, str(exc)
    owner = str(ctx.get("owner_session_id") or conversation_id)
    spec = JobSpec(
        kind="comfyui",
        conversation_id=conversation_id,
        params={
            "comfyui_url": str(values.get("comfyui_url") or "http://127.0.0.1:8188"),
            "workflows": workflows,
            "wait_timeout": max(1, min(int(values.get("timeout", 7200)), 86400)),
        },
        label="ComfyUI 批量生成",
        parent_job_id=str(ctx.get("run_id") or ctx.get("job_id") or "") or None,
        owner_session_id=owner,
        resumable=True,
    )
    job_id = app.jobs.start(spec, owner=owner)
    if bool(values.get("wait")):
        snapshot = app.jobs.wait(job_id, float(spec.params["wait_timeout"]), owner=owner)
        return True, json.dumps(snapshot or {"id": job_id}, ensure_ascii=False)
    return True, json.dumps({"job_id": job_id, "status": "queued", "total": len(workflows)}, ensure_ascii=False)


def _comfyui_prepare_workflow_handler(
    app: AppContext, args: dict[str, Any], _skills: Any, _run_context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    values = args or {}
    try:
        if isinstance(values.get("workflow"), dict):
            raw = values["workflow"]
        else:
            raw = json.loads(Path(str(values.get("path") or "")).expanduser().resolve().read_text(encoding="utf-8"))
        normalized = _normalize_comfyui_workflow(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        text = json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False)
        return False, text
    nodes = []
    for node_id, node in list(normalized.items())[:2000]:
        inputs = node.get("inputs") if isinstance(node, dict) else {}
        nodes.append({
            "id": str(node_id),
            "class_type": str(node.get("class_type") or ""),
            "input_count": len(inputs) if isinstance(inputs, dict) else 0,
        })
    result: dict[str, Any] = {
        "valid": True,
        "format": "api",
        "node_count": len(normalized),
        "nodes": nodes,
        "has_output_node": any(str(item.get("class_type") or "").lower().startswith(("save", "video", "preview")) for item in nodes),
    }
    if bool(values.get("include_workflow")):
        result["workflow"] = normalized
    return True, json.dumps(result, ensure_ascii=False)


class ComfyUIProvider:
    """comfyui 域：comfyui_prepare_workflow + comfyui_batch 单一定义。"""

    def __init__(self, app: AppContext) -> None:
        self._handlers = {
            "comfyui_batch": lambda args, skills, ctx: _comfyui_batch_handler(app, args, skills, ctx),
            "comfyui_prepare_workflow": lambda args, skills, ctx: _comfyui_prepare_workflow_handler(app, args, skills, ctx),
        }

    def tools(self) -> list[ToolSpec]:
        rows: list[ToolSpec] = []
        for spec in build_comfyui_tool_specs():
            rows.append(dataclasses.replace(spec, execute=self._handlers[spec.name], system=True))
        return rows
