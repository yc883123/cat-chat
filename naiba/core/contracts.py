"""跨模块契约定义（重构哲学指引 §0.6 第②条"边显式化"的载体）。

- ``RunContext``：run/工具/技能/视觉层共享的"上下文袋"契约（原裸字典，16 键 + 扩展键）。
  TypedDict 方案：运行时仍是 dict（行为零变化），但在类型层面把键与消费方钉死；
  阶段 2 切分 async_tasks 时可平滑升级为带默认值/校验的 dataclass。
- ``EventType``：前后端事件流 type 枚举。前端可处置的种类必须 ⊆ 本枚举
  （测试 test_contracts 从 public/app.js 反查校验，防止两端漂移）。
- ``MetadataKeys``：消息 metadata JSON 键常量（写入方与重放方的唯一契约来源）。
- ``CHOICE_GROUP_KEYS`` / ``CHOICE_MODES`` / ``CHOICE_SOURCES``：AI 回复里「交互选项组」的
  结构契约。实时事件（run/chat）、消息 metadata 与历史读取（http）三条路径共用
  ``core.choices.normalize_choice_groups`` 这一个规范化入口，结构本身登记在这里。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, TypedDict, runtime_checkable

import threading  # noqa: F401  (供 run_context 键类型引用；协议成员注解用)

RUN_CONTEXT_KEYS: tuple[str, ...] = (
    "run_id", "job_id", "conversation_id", "owner_session_id", "parent_job_id",
    "depth", "allowed_tools", "skill_policy", "job_registry", "executor",
    "cancel_event", "vision_budget", "interaction_mode", "routing_message",
    "mcp_active", "trace_messages", "plan_exit_content", "plan_step_title",
    "model_has_vision", "tool_defs", "workspace_dir", "media_intent",
    "event_sink", "truncation",
)

# 运行期保持 dict 形态（零行为变化）；"带默认值/校验"经由工厂与校验函数落地，
# 避免把 TypedDict 硬切成 dataclass 的改写型手术风险（哲学⑥ 守则④）。
def default_run_context() -> dict[str, Any]:
    """构造带默认值的 RunContext（缺失键由消费方按需读取，字段按类型入位）。"""
    return {
        "run_id": "",
        "job_id": "",
        "conversation_id": "",
        "owner_session_id": "",
        "parent_job_id": "",
        "depth": 0,
        "allowed_tools": [],
        "skill_policy": {},
        "job_registry": None,
        "executor": None,
        "cancel_event": None,
        "vision_budget": None,
        "interaction_mode": "craft",
        "routing_message": "",
        "mcp_active": False,
        "trace_messages": [],
        "plan_exit_content": "",
        "plan_step_title": "",
        "model_has_vision": False,
        "tool_defs": None,
        "workspace_dir": "",
        "media_intent": False,
        # 事件出口（可调用）：工具实现把「实时进度」交给它，由宿主统一落库 + SSE 广播。
        # 工具层不直接持有 manager，避免把 run 内部结构泄漏到工具实现里。
        "event_sink": None,
        # 本轮答复的截断自述信息（{"finish_reason","truncated","continued"}）；
        # 由 skills/agent.py 写入、run/chat.py 落进消息 metadata。
        "truncation": {},
    }


def validate_run_context(ctx: Any) -> list[str]:
    """校验 RunContext：返回非法键清单（空列表=合法）。

    只做白名单与键名检查；类型错误由各消费方按既有防御式读取兜底（不改变运行行为）。
    """
    if not isinstance(ctx, dict):
        return ["<not-a-dict>"]
    return sorted(set(ctx.keys()) - set(RUN_CONTEXT_KEYS))


# ---- 交互选项组契约（写入方 run/chat，重放方 core/choices + http，消费方前端选择面板）----
# 组结构：{"prompt": 题目标题, "choices": [选项原文...], "mode": "single"|"multi",
#         "source": "explicit"|"natural"}；options 原文不截断（长题目/长选项原样保留）。
# 兼容历史：message.metadata.choices（纯字符串数组，无题目）由规范化升级为单组。
CHOICE_GROUP_KEYS: tuple[str, ...] = ("prompt", "choices", "mode", "source")
CHOICE_MODES: tuple[str, ...] = ("single", "multi")
SOURCE_EXPLICIT = "explicit"   # 来自回复里的 ```naiba-choices 结构化代码块（有效块优先）
SOURCE_NATURAL = "natural"     # 来自自然语言识别的"请选择…" + 编号/字母列表
CHOICE_SOURCES: tuple[str, ...] = (SOURCE_EXPLICIT, SOURCE_NATURAL)
DEFAULT_CHOICE_MODE = "single"
# 回复中显式结构化选项的围栏语言标记（模型输出约定，不是工具，也不进工具目录）。
EXPLICIT_CHOICE_FENCE = "naiba-choices"


@runtime_checkable
class ConfigView(Protocol):
    """运行时对 config 的最小访问面（构建时校验；成员 ⊆ ConfigStore 实际面）。"""

    data: dict[str, Any]
    lock: Any

    def profile(self, model_key: str) -> dict[str, Any]: ...
    def resolve_data_dir(self, raw: str | None = None) -> Any: ...
    def resolve_workspace_dir(self, raw: str | None = None) -> Any: ...
    def resolve_managed_skills_dir(self, raw: str | None = None) -> Any: ...
    def get_agent(self, agent_id: str) -> dict[str, Any] | None: ...
    def default_agent_id(self) -> str: ...
    def workspace_dir_for_group(self, name: str) -> str: ...
    def get_hidden_skill_ids(self) -> list[str]: ...
    def public_agents(self) -> list[dict[str, Any]]: ...
    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def save(self) -> None: ...


@runtime_checkable
class AppContext(Protocol):
    """组装根的最小访问面（NaibaChatApp 实例即满足；运行时模块按此注入而非 Any）。"""

    config: ConfigView
    paths: Any
    storage: Any
    runs: Any
    tasks: Any
    jobs: Any
    plans: Any
    catalog: Any
    vision: Any
    web_search: Any
    models: Any
    executor: Any
    tool_registry: Any
    updater: Any
    capabilities: Any
    mcp: Any
    listener_host: str
    update_restart_callback: Any
    data_migration: dict[str, Any]


class RunContext(TypedDict, total=False):
    """一轮执行（Run/Job/子 Agent）的共享上下文。所有键均可选：不同执行形态按需填充。"""

    run_id: str                      # 本轮 Run id
    job_id: str                      # 本 Job id（子 Agent/后台任务形态）
    conversation_id: str             # 会话 id
    owner_session_id: str            # 归属会话（job_kill 的读写边界）
    parent_job_id: str               # 父 Job id（父子级联取消用）
    depth: int                       # 子 Agent 嵌套深度
    allowed_tools: list[str]         # 本轮可用工具集
    skill_policy: dict[str, Any]     # Skill 策略（冻结集/引用集）
    job_registry: Any                # JobRegistry（后台 Job 工具用）
    executor: Any                    # ToolExecutor（工具执行与权限确认）
    cancel_event: Any                # threading.Event（取消信号）
    vision_budget: Any               # VisionBudget（本轮视觉预算）
    interaction_mode: str            # craft / plan / ask
    routing_message: str             # 触发本轮的用户消息
    # ---- 扩展键（生产方写入、消费方读取，属既有隐式协议，一并显式化）----
    mcp_active: bool                 # 本轮是否激活 MCP（skills 写入）
    trace_messages: list[Any]        # 本轮 trace 原样消息（技能层写入）
    plan_exit_content: str           # Plan 出口内容（async_tasks 读取）
    plan_step_title: str             # 当前计划步骤标题（metadata 用）
    model_has_vision: bool           # 会话大脑模型是否支持图片（vision_analyze 执行分流）
    tool_defs: dict[str, Any] | None # 会话化 def 覆盖（如 vision_analyze 按模型能力换形态）
    workspace_dir: str               # 本轮会话工作区（解析后的绝对路径；产物类工具落盘默认位置）
    media_intent: bool               # 用户本轮是否明确要求看图（枚举类工具的 intent_gated 门禁）
    # ---- 透明化/截断自述（工具实时进度出口 + 本轮答复截断信息）----
    event_sink: Any                  # 工具实时进度出口（callable(dict) -> None；缺省不报进度）
    truncation: dict[str, Any]       # 本轮答复截断信息（finish_reason / truncated / continued）


class EventType(str, Enum):
    """流式事件 type 契约（前端无线变更；新增事件必须先加进这里）。"""

    # ---- 对话流（前端 handleChatEvent 可处置）----
    DELTA = "delta"
    REASONING = "reasoning"
    REASONING_START = "reasoning_start"
    REASONING_DELTA = "reasoning_delta"
    REASONING_END = "reasoning_end"
    STATUS = "status"
    SKILLS = "skills"
    SKILL_WARNING = "skill_warning"
    TOOLS_AVAILABLE = "tools_available"
    TOOL_START = "tool_start"
    TOOL_PROGRESS = "tool_progress"
    TOOL_RESULT = "tool_result"
    TOOL_CONFIRM = "tool_confirm"
    CHOICE = "choice"
    CANCELLED = "cancelled"
    RUN_FAILED = "run_failed"
    RUN_STARTED = "run_started"
    CONTEXT_FULL = "context_full"
    DONE = "done"
    ERROR = "error"
    DEBUG_CACHE = "debug_cache"
    HEARTBEAT = "heartbeat"

    # ---- Agent 循环记录（无消费事件已随阶段 4 清理）----
    # ---- Job 域（字段动态展开，宽松）----
    JOB_STATUS = "job_status"
    JOB_FINISHED = "job_finished"
    JOB_LOG = "job_log"
    JOB_CHECK = "job_check"
    OUTPUT = "output"
    # ---- 实时用量（流式期间每完成一次请求推送，前端尾端框实时更新）----
    USAGE = "usage"


class EventPayload(TypedDict, total=False):
    """流式事件负载契约（各事件 type 字段的并集；wire 版式零变化，仅类型标注）。

    实际负载键与前端消费键的一致性由 tests/test_contracts 以 golden 基线反查校验：
    任何 emit 负载出现新键而本契约未登记，即判定为漂移；新增事件字段必须先加进这里。
    """

    type: str
    message: str
    content: str
    reasoning: str
    text: str
    # 对话流 / 状态
    run_id: str
    sequence: int
    status: str
    attempt: int
    limit: int
    used: int
    budget: int
    reason: str
    followup_run_id: str
    partial_message: dict[str, Any]
    # 工具流
    tool: str
    tool_name: str
    tool_desc: str
    arguments: dict[str, Any]
    confirm_id: str
    success: bool
    # 工具调用实例序号（同一轮并行调用同名工具时，前端据此把事件贴到正确的卡片上）
    seq: int
    # 媒体（宿主在工具产出时提取的托管记录：kind/name/source/thumb_path；
    # media_truncated 为分桶截断的自述信息，前端据此渲染提示块）
    media: list[dict[str, Any]]
    media_truncated: dict[str, Any]
    tools: list[dict[str, Any]]
    skills: list[dict[str, Any]]
    choices: list[str]
    choice_groups: list[dict[str, Any]]
    # choice 事件的来源消息 id：前端以「会话 + 来源消息」为键保存临时选择，
    # 缺了它 choice 与随后的 done 会各算一个来源，面板会在两事件之间被重建（选择被清空）。
    message_id: str
    plan: dict[str, Any]
    aborted_message: dict[str, Any]
    # 诊断
    label: str
    lines: list[str]
    # Job 流
    kind: str
    phase: str
    handle: str
    next_check_in: int
    shot: int
    total: int
    files: list[str]
    index: int
    prompt_id: str
    line: str
    # 子 Agent / 计划
    current_step: str
    error: str
    result: str
    # 实时用量（usage 事件负载；与消息 metadata.usage 同构）
    usage: dict[str, Any]


# 事件 type → 允许的负载键（不含公共键 type/run_id/sequence/created_at；None = 宽松，
# 仅校验 type 存在性——job 域字段经 **fields/dict 动态展开，无法静态钉死）。
# 权威来源：各发射点取证（阶段 2 契约收口），守门 tests/test_events_contract.py 强制。
# 历史消息排序契约：后端唯一决定消息顺序（(created_at, rowid)），前端按 API
# 返回数组顺序渲染、不自行排序——见 tests/test_contracts.py 守门。
MESSAGE_ORDER_KEYS: tuple[str, ...] = ("created_at", "rowid")
EVENT_PAYLOAD_KEYS: dict[str, frozenset[str] | None] = {
    # ---- 对话流（前端 handleChatEvent 可处置）----
    "run_started": frozenset({"run_id"}),
    "status": frozenset({"message"}),
    "skills": frozenset({"skills"}),
    "skill_warning": frozenset({"message"}),
    "tools_available": frozenset({"tools"}),
    "delta": frozenset({"content"}),
    "reasoning_start": frozenset(),
    "reasoning_delta": frozenset({"content"}),
    "reasoning_end": frozenset(),
    "reasoning": frozenset({"content"}),
    "tool_start": frozenset({"tool", "arguments", "reason", "seq"}),
    # 工具实时进度（pwsh / run_skill_script 逐行 stdout+stderr）：line 为一行原文，
    # seq 与 tool_start/tool_result 同值，前端据此把行贴到正确的运行卡片上。
    "tool_progress": frozenset({"tool", "line", "seq"}),
    "tool_result": frozenset({"tool", "success", "result", "arguments", "reason", "media", "media_truncated", "seq"}),
    "tool_confirm": frozenset({"tool_name", "tool_desc", "arguments", "confirm_id"}),
    "choice": frozenset({"choices", "choice_groups", "message_id"}),
    "cancelled": frozenset({"message", "aborted_message"}),
    "run_failed": frozenset({"error"}),
    "context_full": frozenset({"limit", "used", "budget"}),
    "done": frozenset({"message", "followup_run_id", "plan"}),
    "error": frozenset({"message", "partial_message"}),
    "debug_cache": frozenset({"label", "lines"}),
    "heartbeat": frozenset(),
    # ---- Job 域（字段动态展开，宽松）----
    "job_status": None,
    "job_finished": None,
    "job_log": frozenset({"line"}),
    "job_check": None,
    "output": frozenset({"line"}),
    # ---- 实时用量 ----
    "usage": frozenset({"usage"}),
}


# 已迁往 naiba.core.messages（收官线 ③；保留 re-export 兼容，既有导入零改动）。
from naiba.core.messages import MESSAGE_METADATA_KEYS, MetadataKeys  # noqa: E402,F401
