"""Normalized effective configuration identity, shared by dispatch and tools."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any


class PreflightRejected(RuntimeError):
    """A new dispatch was certainly rejected before executor submission."""


def execution_fingerprint(app: Any, cid: str, *, execution_model: dict[str, str] | None = None,
                          conversation: dict[str, Any] | None = None) -> dict[str, Any]:
    if conversation is None:
        conversation = app.storage.get_conversation(cid, include_trace=False, include_messages=False)
    if not conversation:
        raise PreflightRejected("关联会话不存在，未派发")
    agent_getter = getattr(app.config, "get_agent", None)
    agent = agent_getter(str(conversation.get("agent_id") or "")) if callable(agent_getter) else {}
    if not agent and callable(agent_getter):
        default = getattr(app.config, "default_agent_id", None)
        agent = agent_getter(default()) if callable(default) else {}
    agent_digest = hashlib.sha256(json.dumps(agent or {}, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    workspace = str(app.config.resolve_workspace_dir(str(conversation.get("workspace_dir") or "").strip() or None))
    from naiba.run.session import resolve_session_tool_ids
    tools = resolve_session_tool_ids(app, conversation, agent or {})
    policy = dict(conversation.get("skill_policy") or {"mode": "auto", "skill_ids": []})
    policy["skill_ids"] = sorted(set(policy.get("skill_ids") or []))
    model = execution_model or {key: str(conversation.get(key) or "") for key in ("model_key", "provider_id", "model_name")}
    profile = app.config.profile(str(model.get("model_key") or ""))
    profile_digest = hashlib.sha256(json.dumps(profile or {}, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return {"workspace_dir": os.path.normcase(os.path.realpath(workspace)) if workspace else "",
            "permission_mode": str(conversation.get("permission_mode") or "confirm"),
            "enabled_tool_ids": sorted(set(tools)),
            "skill_policy": policy,
            "execution_model": model, "profile_digest": profile_digest,
            "agent_id": str(conversation.get("agent_id") or ""), "agent_digest": agent_digest,
            **{key: str(conversation.get(key) or "") for key in ("model_key", "provider_id", "model_name")}}


def validate_execution_baseline(app: Any, cid: str, frozen: dict[str, Any], *,
                                conversation: dict[str, Any] | None = None) -> str:
    try:
        current = execution_fingerprint(app, cid, execution_model=frozen.get("execution_model"), conversation=conversation)
    except PreflightRejected as exc:
        return str(exc)
    except (LookupError, ValueError, TypeError, OSError):
        return "当前执行配置无法验证，未派发；请检查模型、工具及工作区后重新创建任务"
    if current != frozen:
        return "会话权限、工作区、工具、Skill、Agent或模型已改变；请重新确认或创建任务，未派发新执行"
    return ""
