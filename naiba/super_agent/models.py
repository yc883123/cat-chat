from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

THREAD_STATUSES = (
    "draft",
    "active",
    "waiting_user",
    "paused",
    "stopping",
    "cancelled",
    "completed",
    "failed",
    "needs_recovery",
    "archived",
)
NODE_STATUSES = (
    "pending",
    "ready",
    "running",
    "stopping",
    "waiting_user",
    "succeeded",
    "failed",
    "cancelled",
    "blocked",
    "interrupted",
)
APPROVAL_STATUSES = ("pending", "approved", "rejected", "cancelled")

EVENT_TYPES = (
    "user_message",
    "run_started",
    "run_completed",
    "job_completed",
    "job_failed",
    "artifact_created",
    "approval_requested",
    "approval_received",
    "task_cancelled",
    "run_interrupted",
    "stage_promoted",
    "task_paused",
    "task_resumed",
    # 删除＝软删除（归档）与恢复（§5.1）；事件保留归档前状态供恢复回读。
    "task_archived",
    "task_restored",
    "app_restarted",
    "progress",
)

THREAD_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "archived"}
NODE_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}

NODE_TRANSITIONS = {
    "pending": {"ready", "blocked", "cancelled"},
    "ready": {"running", "waiting_user", "cancelled"},
    "running": {"stopping", "waiting_user", "succeeded", "failed", "cancelled", "interrupted"},
    "stopping": {"cancelled", "interrupted"},
    "waiting_user": {"ready", "running", "stopping", "succeeded", "failed", "cancelled", "interrupted"},
    "failed": {"ready", "cancelled"},
    "cancelled": {"ready"},
    "blocked": {"ready", "cancelled"},
    "interrupted": {"ready", "cancelled"},
}
THREAD_TRANSITIONS = {
    "draft": {"active", "paused", "cancelled", "archived"},
    "active": {"waiting_user", "paused", "stopping", "completed", "failed", "cancelled", "archived", "needs_recovery"},
    "waiting_user": {"active", "paused", "stopping", "completed", "failed", "cancelled", "archived", "needs_recovery"},
    "paused": {"active", "stopping", "cancelled", "archived"},
    "stopping": {"cancelled", "needs_recovery"},
    "failed": {"active", "archived"},
    "needs_recovery": {"active", "archived"},
    "completed": {"archived"},
    "cancelled": {"active", "archived"},
    # 「恢复」（§5.1）：归档可回到归档前的状态；归档源状态由服务层限定
    # （active/stopping 必须先取消），所以这里不含 active/stopping。
    "archived": {"draft", "waiting_user", "paused", "failed", "needs_recovery", "completed", "cancelled"},
}


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


@dataclass(slots=True)
class Thread:
    thread_id: str
    owner_id: str
    title: str
    goal: str
    status: str = "draft"
    workspace_dir: str = ""
    router_agent_id: str = ""
    autonomy_level: str = "confirm"
    summary: str = ""
    chain_version: int = 0
    current_node_id: str = ""
    linked_conversation_ids: list[str] = field(default_factory=list)
    linked_run_ids: list[str] = field(default_factory=list)
    linked_job_ids: list[str] = field(default_factory=list)
    linked_domain_task_ids: list[str] = field(default_factory=list)
    execution_snapshot: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    version: int = 1
    last_event_at: int = 0
    created_at: int = 0
    updated_at: int = 0
    archived_at: int | None = None

    @property
    def id(self) -> str:
        return self.thread_id


@dataclass(slots=True)
class TaskNode:
    node_id: str
    thread_id: str
    owner_id: str
    kind: str
    status: str = "pending"
    stage_key: str = ""
    sequence: int = 0
    promote_to: str = ""
    promotion_policy: dict[str, Any] = field(default_factory=dict)
    chain_instance_id: str = ""
    parent_id: str = ""
    dependencies: list[str] = field(default_factory=list)
    executor_type: str = "fake"
    executor_id: str = ""
    input_refs: list[Any] = field(default_factory=list)
    output_refs: list[Any] = field(default_factory=list)
    approval_required: bool = False
    retry_policy: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0
    idempotency_key: str = ""
    execution_snapshot: dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False
    error: str = ""
    schema_version: int = SCHEMA_VERSION
    version: int = 1
    created_at: int = 0
    updated_at: int = 0

    @property
    def id(self) -> str:
        return self.node_id


@dataclass(slots=True)
class TaskEvent:
    event_id: str
    thread_id: str
    node_id: str
    source: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    source_event_id: str = ""
    idempotency_key: str = ""
    attempt: int = 0
    sequence: int = 0
    created_at: int = 0
    handled_at: int | None = None


@dataclass(slots=True)
class Artifact:
    artifact_id: str
    thread_id: str
    node_id: str
    kind: str
    path_or_ref: str
    title: str = ""
    summary: str = ""
    version: int = 1
    idempotency_key: str = ""
    created_at: int = 0


@dataclass(slots=True)
class Approval:
    approval_id: str
    thread_id: str
    node_id: str
    action: str
    description: str
    risk_level: str = "medium"
    status: str = "pending"
    requested_at: int = 0
    resolved_at: int | None = None
    resolved_by: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """Configuration frozen when a Thread/Node is created."""

    agent_id: str = ""
    provider_id: str = ""
    model_key: str = ""
    workspace_dir: str = ""
    permission_mode: str = "confirm"
    enabled_tool_ids: tuple[str, ...] = ()
    skill_policy: dict[str, Any] = field(default_factory=dict)
    vision_capabilities: dict[str, Any] = field(default_factory=dict)
    cost_policy: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "provider_id": self.provider_id,
            "model_key": self.model_key,
            "workspace_dir": self.workspace_dir,
            "permission_mode": self.permission_mode,
            "enabled_tool_ids": list(self.enabled_tool_ids),
            "skill_policy": dict(self.skill_policy),
            "vision_capabilities": dict(self.vision_capabilities),
            "cost_policy": dict(self.cost_policy),
        }
