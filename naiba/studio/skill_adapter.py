"""Task-local content adapter; the SkillAgent owns the only model/tool loop."""
from __future__ import annotations

from typing import Any

from naiba.core.contracts import STUDIO_SKILL_IDS_KEY, STUDIO_SKILL_SNAPSHOT_KEY, STUDIO_TRACE_MESSAGES_KEY
from naiba.skills.agent import SkillAgent
from naiba.studio.content import ContentSnapshots, READ_TOOLS, _digest


def check_context(profile: dict[str, Any], options: dict[str, Any], system: str,
                  history: list[dict[str, Any]]) -> None:
    """Count the complete system and history; never shorten content to fit."""
    limit, budget = SkillAgent._context_budget(profile, options, system)
    output = int(options.get("max_tokens") or profile.get("max_output_tokens") or 0)
    output = output or min(8192, max(1024, limit // 8))
    fixed = SkillAgent._request_fixed_tokens(options, system)
    used = sum(SkillAgent._replay_footprint_tokens(row) for row in history)
    # SkillAgent's legacy 256-token budget floor must not admit an oversized system.
    if fixed + output + used > limit or used > budget:
        raise ValueError(f"完整系统提示词与历史超出上下文预算（窗口 {limit}，预计输入 {fixed + used}）；未截断规则")


class ContentSkillAdapter:
    def __init__(self, app: Any):
        self.app = app

    def run(self, input_data: dict[str, Any], profile: dict[str, Any],
            history: list[dict[str, Any]], options: dict[str, Any], event: Any,
            usage_sink: dict[str, Any], trace_sink: list[dict[str, Any]]) -> str:
        snapshots = ContentSnapshots(self.app.paths.data_dir)
        snapshot = input_data.get(STUDIO_SKILL_SNAPSHOT_KEY)
        view = snapshots.view(snapshot)
        system = str(input_data.get("system") or "")
        ids = list(input_data.get(STUDIO_SKILL_IDS_KEY) or [])
        if [row["id"] for row in view.scan()] != ids or snapshot.get("system_hash") != _digest(system):
            raise ValueError("Skill 冻结输入损坏，无法继续")
        registry = view.registry()
        check_context(profile, {**options, "tools": registry.schemas()}, system, history)
        failures: list[str] = []

        def bridge(payload: dict[str, Any]) -> None:
            kind = str(payload.get("type") or "")
            if kind in {"delta", "reasoning_delta", "status", "usage"}:
                if kind == "usage" and isinstance(payload.get("usage"), dict):
                    usage_sink.clear()
                    usage_sink.update(payload["usage"])
                event(payload)
            elif kind in {"context_full", "run_failed"}:
                message = str(payload.get("error") or payload.get("message") or "Skill 上下文已满，未截断规则")
                failures.append(message)
                event({"type": "status", "message": message})
            elif kind in {"tool_started", "tool_result", "skill_warning"}:
                message = str(payload.get("message") or "")
                if not message:
                    message = f"参考文档工具 {payload.get('tool') or payload.get('name') or ''} " + ("返回" if kind == "tool_result" else "执行中")
                event({"type": "status", "message": message})

        policy = {"mode": "exclusive", "skill_ids": ids}
        context = {"skill_policy": policy, "allowed_tools": list(READ_TOOLS), "confirmation_ui": False,
                   "cancel_event": options.get("cancel_event"), "executor": view, "model_key": input_data.get("model_key", "")}
        worker = SkillAgent(view, view, self.app.models.complete, None)
        try:
            text, _runs, _reasonings, usage = worker.run(
                "", history, profile, dict(options), policy, [], "", list(READ_TOOLS),
                bridge, options.get("cancel_event"), tool_registry=registry, run_context=context,
                system_prompt_override=system,
            )
            usage_sink.clear()
            usage_sink.update(usage or {})
        finally:
            trace_sink[:] = context.get(STUDIO_TRACE_MESSAGES_KEY) or []
        if failures or context.get("partial"):
            raise ValueError(failures[-1] if failures else "Skill 未完成本次生成")
        if not str(text or "").strip():
            raise ValueError("Skill 没有返回有效正文")
        return str(text)
