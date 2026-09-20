"""会话快照与工具集决议（原 async_tasks.py 的会话引导区，阶段 2 拆包第一步）。

职责：给一轮 Run 冻结"会话级"决策——可用工具集（模式 × Agent 范围 × 固化集 ×
图像能力 × Job 依赖闭包）、会话工具固化（bake/enable）、生成选项读取、附件图片判断、
工具路由消息（短跟进消息保留任务上下文）。全部为无状态函数（app / config 显式传入）。
"""

from __future__ import annotations

from naiba.core.contracts import AppContext

import re
from typing import Any

from naiba.plans import normalize_interaction_mode, resolve_mode_tools
from naiba.tools.registry import RETIRED_TOOL_MAP
from naiba.vision.runtime import IMAGE_SUFFIXES


# 系统工具（除 9 个基础 agent_tools 外，按模式追加到 allowed_tools）。
# - Craft 模式：作业/子 Agent/视觉/搜索工具全部可用；
# - Ask/Plan 模式：仅只读分析与搜索工具（vision_image_ops 这类写文件工具排除）。
JOB_TOOLS = (
    "run_in_background", "job_output", "job_status", "job_wait", "job_kill",
    "subagent", "subagent_spawn", "todo_write",
)
# Harness 兼容别名（read/write/edit/grep）只存在于查询层归一（执行兼容），
# 不再注入 allowed_tools/模型可见集；只保留规范名，避免别名与规范名重复披露。
HARNESS_TOOLS = ("read_file", "write_file", "edit_file", "list_directory", "search_files", "pwsh")
CAPABILITY_TOOLS = ("install_skill", "unpack_skill_archive", "inspect_installed_skill")
# 视觉工具单入口：vision_analyze（识图/装载，只读）与 vision_image_ops（PIL 本地计算，会写产物文件）。
VISION_READONLY_TOOLS = ("vision_analyze",)
VISION_WRITING_TOOLS = ("vision_image_ops",)
SYSTEM_TOOLS_CRAFT = HARNESS_TOOLS + JOB_TOOLS + CAPABILITY_TOOLS + VISION_READONLY_TOOLS + VISION_WRITING_TOOLS + ("web_search", "comfyui_prepare_workflow", "comfyui_batch")
SYSTEM_TOOLS_READONLY = VISION_READONLY_TOOLS + ("web_search",)

# Cross-tool dependency closure: a Job CREATOR tool is useless without the QUERY
# tools that its own description tells the model to use afterwards. If a creator
# is enabled but its query tools are filtered out (agent tool_scope / baked
# enabled_tool_ids / plan mode), the model would be told to query with tools that
# are not actually declared — the exact "disabled tool still referenced" trap.
# So a creator always drags its required query tools along.
JOB_CREATOR_TOOL_DEPS = {
    "run_in_background": ("job_output", "job_status", "job_wait", "job_kill"),
    "subagent": ("job_output",),
    "subagent_spawn": ("job_output",),
    "comfyui_batch": ("job_output", "job_status", "job_wait"),
}

# ---- 工具互斥组（同一 Agent 工具集里只能勾一个，模式选择权完全归用户）----
# 子代理的两种上下文模式做成**两个互斥工具**（subagent=fork 继承父历史 /
# subagent_spawn=spawn 干净上下文）：模型只看到被启用的那个，不需要知道"模式"这个概念；
# 用户在一次设置里选定，之后长期生效（与「ComfyUI 联动」等同款工具集心智）。
# 组内**排前者**是保留方向：fork 选错只是费钱、结果仍对；反过来让子代理丢上下文
# 是既费钱又出质量事故的方向（§九.116）。
# 前端镜像 `AGENT_TOOL_MUTEX_GROUPS`（public/js/09-settings.js）——改一边必须改另一边。
MUTUALLY_EXCLUSIVE_TOOL_GROUPS: tuple[tuple[str, ...], ...] = (("subagent", "subagent_spawn"),)


def normalize_tool_mutex(tool_ids: Any) -> list[str]:
    """互斥归一：同组多个同时出现 → 只保留组内排前者，其余丢弃（保持原顺序）。

    存量配置本不该同时含两个（工具页是全选/互斥联动的），这里只是确定性兜底——
    手攒的工具集、直接打 API 的写入、将来手工编辑的 config.json 都从这里收敛，
    保证运行时**绝不相同组双开**。只含单个工具时是恒等操作。
    """
    result = list(tool_ids or [])
    for group in MUTUALLY_EXCLUSIVE_TOOL_GROUPS:
        present = [name for name in group if name in result]
        for dropped in present[1:]:
            result = [item for item in result if item != dropped]
    return result


def resolve_allowed_tools(
    app: AppContext,
    mode: str,
    agent: dict[str, Any],
    web_search_enabled: bool,
    model_key: str = "",
    enabled_tool_ids: list[str] | None = None,
) -> list[str]:
    """Freeze one run's tools after applying mode, Agent scope, and availability.

    若传入 ``enabled_tool_ids``（会话启动时固化的启用工具集），则以它作为硬限制，
    不再叠加 base+system 并集（未固化/无快照时退回旧逻辑，保证迁移/旧会话兼容）。
    """
    if enabled_tool_ids:
        # 旧会话固化的工具集可能含退役名（如 glob_files）：按退役映射归一为新名，
        # 避免"老工具被消失、替代工具也不在固化集里"导致能力静默丢失。
        allowed_tools = list(dict.fromkeys(
            RETIRED_TOOL_MAP.get(str(item), str(item))
            for item in enabled_tool_ids if str(item).strip()
        ))
    else:
        base_tools = resolve_mode_tools(
            mode,
            [str(item) for item in app.config.data.get("agent_tools", [])],
            app.tool_registry.readonly_mcp_tools(),
        )
        system_tools = SYSTEM_TOOLS_READONLY if normalize_interaction_mode(mode) == "plan" else SYSTEM_TOOLS_CRAFT
        allowed_tools = list(dict.fromkeys([*base_tools, *system_tools]))
        # MCP tools are never added implicitly from registry discovery. They
        # are available only when a user explicitly places the tool in scope.
        scope = set(agent.get("tool_scope") or [])
        if scope:
            allowed_tools = [tool for tool in allowed_tools if tool in scope]
    if "web_search" in allowed_tools and not app.web_search.is_available():
        # web_search 只靠"设置固化"：是否声明仅取决于搜索端点是否已配置
        # （会话固化工具集是否含 web_search 决定它是否进入 allowed_tools）。
        # 不再受发送区开关（web_search_enabled）动态控制，避免切换时改变 tools 伤害缓存。
        allowed_tools.remove("web_search")
    # 视觉工具对文本/多模态模型暴露同一工具集（vision_analyze 单入口），
    # 模型能力差异由会话化 def 换形态（RunContext.tool_defs）处理，不再按能力裁剪工具名。
    # Dependency closure: ensure Job creators are always paired with the
    # query tools their descriptions reference, so the model never sees a
    # "use job_output/job_status/job_wait" instruction for a tool that was
    # filtered out of the declaration.
    present = set(allowed_tools)
    for creator, deps in JOB_CREATOR_TOOL_DEPS.items():
        if creator in present:
            for dep in deps:
                if dep not in present:
                    allowed_tools.append(dep)
                    present.add(dep)
    # 互斥归一放在最后：**这一处就保证运行时绝不相同组双开**（无论工具集来自
    # 固化集、Agent scope 还是旧逻辑的兜底并集）。见 MUTUALLY_EXCLUSIVE_TOOL_GROUPS。
    return normalize_tool_mutex(allowed_tools)


def all_tool_names(app: AppContext) -> list[str]:
    """全部可用的工具 id（含别名），用于旧会话/未配置 agent 的"全激活"固化。"""
    schemas = (
        app.tool_registry.schemas()
        if callable(getattr(app.tool_registry, "schemas", None))
        else []
    )
    return [str(spec.get("name") or "") for spec in schemas if isinstance(spec, dict) and spec.get("name")]


def bake_session_tool_ids(
    app: AppContext, conversation: dict[str, Any], agent: dict[str, Any]
) -> list[str]:
    """固化某会话的启用工具集（会话启动时写死，之后不可改）。

    - 已固化 → 直接返回；
    - 旧会话（已有消息、未固化）→ 全部激活（迁移兼容，保持既有行为）；
    - 新会话 → 按当前 Agent 的 tool_scope（空则回退为全量）。
    """
    existing = conversation.get("enabled_tool_ids") or []
    if existing:
        return normalize_tool_mutex([str(item) for item in existing])
    if app.storage.message_count(str(conversation.get("id") or "")) > 0:
        baked = all_tool_names(app)
    else:
        scope = [str(item) for item in (agent.get("tool_scope") or []) if str(item).strip()]
        baked = scope or all_tool_names(app)
    # 落库的就必须是归一后的集合：前端显示与实际放行同源，不给"互斥的两个都开了"留洞。
    baked = normalize_tool_mutex(baked)
    baked = list(dict.fromkeys(str(item) for item in baked if item))
    app.storage.set_enabled_tool_ids(str(conversation.get("id") or ""), baked)
    return baked


def enable_conversation_tools(
    app: AppContext, conversation_id: str, tool_ids: list[str]
) -> dict[str, Any]:
    """向指定会话"追加/保底注入"若干工具到其固化的启用工具集。

    若会话尚未固化工具集，先按当前 Agent 规则固化成全集，再并集合并；已固化则直接并集。
    只加不删，用于"开始页安装skill"按钮临时/持久启用的能力工具与读写工具。
    """
    conversation = app.storage.get_conversation(conversation_id)
    if not conversation:
        raise LookupError("对话不存在")
    agent = app.config.get_agent(str(conversation.get("agent_id") or ""))
    if not agent:
        agent = app.config.get_agent(app.config.default_agent_id()) or {
            "id": "", "name": "Agent", "system_prompt": "", "skill_ids": []
        }
    existing = conversation.get("enabled_tool_ids") or []
    existing = [str(x) for x in existing if str(x).strip()]
    if not existing:
        existing = bake_session_tool_ids(app, conversation, agent)
        existing = [str(x) for x in existing if str(x).strip()]
    known = set(all_tool_names(app))
    before = list(existing)
    added: list[str] = []
    for t in tool_ids:
        t = str(t or "").strip()
        if t and t in known and t not in existing:
            existing.append(t)
            added.append(t)
    # 互斥归一：只加不删的路径同样过一遍，避免"注入另一个互斥工具"把集合变成双开。
    existing = normalize_tool_mutex(existing)
    if added or existing != before:
        app.storage.set_enabled_tool_ids(conversation_id, existing)
    return {"enabled_tool_ids": existing, "added": added}


def generation_options(config: Any, model_key: str = "") -> dict[str, Any]:
    """Read provider-scoped options while tolerating legacy adapters."""
    getter = config.generation_options
    try:
        return dict(getter(model_key))
    except TypeError as first_error:
        try:
            return dict(getter())
        except TypeError:
            raise first_error


def attachments_have_images(attachments: list[Any]) -> bool:
    for item in attachments:
        if isinstance(item, dict):
            source = item.get("path") or item.get("name") or item.get("source") or ""
        else:
            source = item
        clean = str(source or "").split("?", 1)[0].split("#", 1)[0].lower()
        if any(clean.endswith(suffix) for suffix in IMAGE_SUFFIXES):
            return True
    return False


def routing_message(message: str, history: list[dict[str, Any]]) -> str:
    """Keep tool routing context for short choice/confirmation follow-ups."""
    current = str(message or "").strip()
    compact = re.sub(r"\s+", "", current.lower())
    continuation = bool(
        re.match(r"^(?:选择|选项|确认|继续|就这个|用这个|立即提交|提交并生成|开始生成)", compact)
        or re.match(r"^\d+(?:[：:、.．]|$)", compact)
        or ("推荐" in compact and len(compact) <= 80)
    )
    # Parameter-only replies often start with a model/file name or prompt
    # rather than words such as "继续".  If the preceding assistant turn
    # explicitly requested missing generation/workflow parameters, retain
    # that task context so direct tools remain available.
    if not continuation and len(current) <= 600:
        recent_assistants: list[str] = []
        for item in reversed(history):
            if not isinstance(item, dict) or item.get("role") != "assistant":
                continue
            text = str(item.get("content") or "").strip()
            if text:
                recent_assistants.append(text)
            if len(recent_assistants) >= 4:
                break
        previous_compact = re.sub(r"\s+", "", "\n".join(recent_assistants).lower())
        asks_for_parameters = any(marker in previous_compact for marker in (
            "请补充", "请告诉", "请选择", "至少需要", "正向提示词", "反向提示词",
            "模型编号", "模型名称", "工作流文件", "生成数量", "图片尺寸",
        ))
        workflow_context = any(marker in previous_compact for marker in (
            "comfyui", "生成图片", "生图", "工作流", "checkpoint", "正向提示词",
        ))
        continuation = asks_for_parameters and workflow_context
    if not continuation:
        return current
    context: list[str] = []
    for item in reversed(history):
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text or text == current:
            continue
        context.append(text[:1600])
        if len(context) >= 3:
            break
    if not context:
        return current
    return "\n\n".join([*reversed(context), current])
