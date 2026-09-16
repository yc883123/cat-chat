"""消息 metadata 契约常量（自 naiba.core.contracts 迁出；收官线 ③）。

写入方（run/chat、run/manager、plans）与重放方（core/history）共用同一键契约——
新增 metadata 键必须先在这里登记再使用（哲学指引 §0.6 第②条"边显式化"）。
``naiba.core.contracts`` 保留 re-export 兼容，既有导入零改动。
"""
from __future__ import annotations

# 全部 metadata 键的权威清单（新增键同步更新本表与 MetadataKeys）。
MESSAGE_METADATA_KEYS: tuple[str, ...] = (
    "attachments",
    "attachments_truncated",
    "reasoning",
    "tool_runs",
    "trace",
    "usage",
    "files",
    "plan_id",
    "plan_step",
    "plan_step_title",
    "aborted",
    "partial",
    "error",
    # 本轮答复截断自述（{"finish_reason","truncated","continued"}）：非空即表示
    # 模型输出可能不完整，前端据此在正文末尾渲染提示行。
    "truncated",
    "run_id",
    "agent_id",
    "display_content",
    # 交互选项组（AI 要求用户在面板里选择时的候选数据）：结构见
    # naiba.core.contracts.CHOICE_GROUP_KEYS；choices 是 choice_groups[0] 的旧版扁平副本。
    "choices",
    "choice_groups",
    # 「新会话开始」边界标记（role=session 的标记行）：重放时从此清空此前历史。
    "session_start",
)


class MetadataKeys:
    """消息 metadata JSON 键（写入方 async_tasks / 重放方 core.history 共用契约）。"""

    ATTACHMENTS = "attachments"
    # 消息级媒体分桶截断的自述信息（{"total","shown","kinds"}）：超出上限时不静默，
    # 前端在媒体网格下方渲染"共 N 张，仅显示前 M 张"。
    ATTACHMENTS_TRUNCATED = "attachments_truncated"
    REASONING = "reasoning"
    TOOL_RUNS = "tool_runs"
    TRACE = "trace"
    USAGE = "usage"
    FILES = "files"
    PLAN_ID = "plan_id"
    PLAN_STEP = "plan_step"
    PLAN_STEP_TITLE = "plan_step_title"
    ABORTED = "aborted"
    PARTIAL = "partial"
    ERROR = "error"
    # 本轮答复截断自述（{"finish_reason","truncated","continued"}）：模型输出被
    # 输出上限截断、或流在没有终止原因的情况下带着"明显未说完"的正文结束时写入。
    TRUNCATED = "truncated"
    RUN_ID = "run_id"
    AGENT_ID = "agent_id"
    DISPLAY_CONTENT = "display_content"
    # 交互选项组（前端选择面板的数据源；组结构见 contracts.CHOICE_GROUP_KEYS）。
    # CHOICES 是 CHOICE_GROUPS[0] 的选项原文副本，保留给旧前端/旧历史读取路径。
    CHOICES = "choices"
    CHOICE_GROUPS = "choice_groups"
    # 新会话边界（写在 role=session 的标记行上）：build_model_history 遇到它即清空
    # 此前的历史；聊天记录本身不删，前端在该位置渲染分隔条。
    SESSION_START = "session_start"
