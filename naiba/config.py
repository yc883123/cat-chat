# -*- coding: utf-8 -*-
"""配置层：ConfigStore 与 Agent/工具目录/供应商能力推断（层级 2，仅依赖 core 与 paths）。

自 server.py 整片迁入（2026-09-06，3.4.1-B）；路径经 PathContext 注入，
本模块不 import server（哲学② DAG 红线）。
"""

from __future__ import annotations

import json, logging, os, re, secrets, threading, time, urllib.parse, uuid
from pathlib import Path
from typing import Any

from naiba.core.paths import path_within
from naiba.paths import PathContext

logger = logging.getLogger("naiba.config")

# Appearance preferences are intentionally small, stable enums.  Keeping the
# values in one place lets both config migration and runtime updates apply the
# same validation rules.
APPEARANCE_THEMES = frozenset({"system", "light", "dark"})
APPEARANCE_SKINS = frozenset({"violet", "ocean", "rose", "forest"})
# 会话区（消息气泡/正文）字体：字号是像素整数，字体族只存枚举键，
# 具体的 font-family 栈由前端常量映射（后端不碰 CSS，避免两端各写一份真相）。
# 结构三档：跟随界面 / 衬线 / 圆润。
APPEARANCE_CHAT_FONT_STRUCTURAL = ("system", "serif", "rounded")
# 预置字体（前端「指定字体」列表里逐项点选，替掉"手敲 CSS 字体名"）：
# 键名是稳定契约，加一款字体 = 这里加一个键 + 前端补一条栈与标签；
# **键不进 UI 文案**，中文名由前端 CHAT_FONT_PICKS 提供（后端不管显示）。
APPEARANCE_CHAT_FONT_PRESETS = (
    "yahei", "pingfang", "simsun", "kaiti", "simhei", "fangsong",
    "noto_sans", "noto_serif", "lxgw", "smiley", "harmonyos",
)
APPEARANCE_CHAT_FONT_FAMILIES = frozenset(
    (*APPEARANCE_CHAT_FONT_STRUCTURAL, *APPEARANCE_CHAT_FONT_PRESETS, "custom")
)
CHAT_FONT_SIZE_MIN = 13
CHAT_FONT_SIZE_MAX = 18
CHAT_FONT_SIZE_DEFAULT = 15
# 自定义字体串只作为 font-family 片段注入 CSS 变量，截断长度是纵深防御。
CHAT_FONT_FAMILY_CUSTOM_MAX = 100

# ---- 聊天背景图（只铺对话区）----
# 透明度滑杆范围：0.05 是「还能看见」的下限，1 = 完全不透明。
CHAT_BACKGROUND_MIN_OPACITY = 0.05
# 滑杆默认值（方案定稿值：待实机看效果再定）。
CHAT_BACKGROUND_DEFAULT_OPACITY = 0.35
# 允许当背景的图片格式：按**读出来的 format**（不是扩展名）判定，白名单之外一律拒绝。
# 候选是「WebView2/Chromium 能解码 + /api/file 能给出正确 MIME」的位图：
# TIFF / HEIC 这类虽然能上传，但浏览器解不出来——放过去就是"设置保存成功、背景一片空白"
# 的静默失败（本项目最忌的失败形态）。SVG 也不收：矢量无法按内容校验，
# 且不在 core/media_types.py 的图片清单里（口径统一）。
CHAT_BACKGROUND_IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP", "AVIF")
# 位置 / 缩放：**旧模型的遗留输入**。旧模型把取景框比例锁死成对话区比例，只有
# 「缩放 + 位置」两个自由度，于是「填满」「完整显示」两个预置态必然有一个方向自由度
# 恰好为 0（用户报障："取景框只能横向切割，不能竖向切割"——就是它）。现在权威字段是
# `crop`（自由裁剪矩形）；旧值只在 crop 缺失时用来换算等价区域（升级观感零变化），
# 前端不再写回，保留只为兼容旧客户端 / 旧配置文件。
CHAT_BACKGROUND_DEFAULT_POSITION = 50.0
CHAT_BACKGROUND_MIN_ZOOM = 0.05
CHAT_BACKGROUND_MAX_ZOOM = 4.0
CHAT_BACKGROUND_DEFAULT_ZOOM = 1.0
# 裁剪区域（crop）：图片内的相对矩形 {x, y, w, h}（x/y = 左上角，w/h = 宽高，都是图片比例）。
# 缺失 = 自动（由前端按对话区比例取最大区域居中，即旧模型 zoom=1 的观感）。
# 最小边长 2%：再小就是一条缝，拖拽/滑杆都无法操作，界面上也没有意义。
CHAT_BACKGROUND_MIN_CROP = 0.02


def normalize_chat_background_opacity(value: Any) -> float:
    """背景图透明度归一化：非法值回落默认，越界 clamp 到 [0.05, 1]。"""
    try:
        opacity = float(value)
    except (TypeError, ValueError):
        return CHAT_BACKGROUND_DEFAULT_OPACITY
    if opacity != opacity:  # NaN：NaN 参与比较恒为 False，会被 min/max 静默放过
        return CHAT_BACKGROUND_DEFAULT_OPACITY
    return min(1.0, max(CHAT_BACKGROUND_MIN_OPACITY, opacity))


def normalize_chat_background_position(value: Any) -> float:
    """背景图位置（锚点百分比）：非法值回落 50（居中），越界 clamp 到 [0, 100]。"""
    try:
        position = float(value)
    except (TypeError, ValueError):
        return CHAT_BACKGROUND_DEFAULT_POSITION
    if position != position:
        return CHAT_BACKGROUND_DEFAULT_POSITION
    return round(min(100.0, max(0.0, position)), 1)


def normalize_chat_background_zoom(value: Any) -> float:
    """背景图缩放倍率：非法值回落 1（= 填满），越界 clamp 到 [0.05, 4]。"""
    try:
        zoom = float(value)
    except (TypeError, ValueError):
        return CHAT_BACKGROUND_DEFAULT_ZOOM
    if zoom != zoom:
        return CHAT_BACKGROUND_DEFAULT_ZOOM
    return round(min(CHAT_BACKGROUND_MAX_ZOOM, max(CHAT_BACKGROUND_MIN_ZOOM, zoom)), 3)


def normalize_chat_background_crop(value: Any) -> dict[str, Any] | None:
    """裁剪区域归一化；返回 None = 未设置（= 自动按对话区比例取最大区域）。

    加载期容错：手改配置、旧配置、前端都可能给出"形状不对"的值（字段缺失、宽高非正、
    越界）。这里一律**静默收敛**（越界 clamp、结构不可用回落 None）；写入路径走
    `update_settings` 的严格校验，两边分工与旧字段一致。
    """
    if not isinstance(value, dict):
        return None
    numbers: dict[str, float] = {}
    for field in ("x", "y", "w", "h"):
        try:
            number = float(value[field])
        except (KeyError, TypeError, ValueError):
            return None
        if number != number or abs(number) == float("inf"):  # NaN / ±inf
            return None
        numbers[field] = number
    # 宽高非正 = "没有取景"（手改成 0 或负数只可能是想表达"别用取景"）→ 回落自动。
    if numbers["w"] <= 0 or numbers["h"] <= 0:
        return None
    width = min(1.0, max(CHAT_BACKGROUND_MIN_CROP, numbers["w"]))
    height = min(1.0, max(CHAT_BACKGROUND_MIN_CROP, numbers["h"]))
    return {
        "x": round(min(1.0 - width, max(0.0, numbers["x"])), 4),
        "y": round(min(1.0 - height, max(0.0, numbers["y"])), 4),
        "w": round(width, 4),
        "h": round(height, 4),
    }


def normalize_chat_background(value: Any) -> dict[str, Any]:
    """把任意（手改 / 旧配置 / 前端）取值归一化成完整的 chat_background 对象。

    单点实现：默认值、加载期合并、`update_settings` 三处共用同一套
    「非法回落默认、越界 clamp」规则——写三遍迟早漂移。注意它**不做**格式/存在性校验
    （那是 `_validated_chat_background_image` 的事），也不报错（枚举类错误留给
    `update_settings` 显式抛，避免"点保存没反应"）。
    """
    raw = value if isinstance(value, dict) else {}
    return {
        "image": str(raw.get("image") or "").strip(),
        "opacity": normalize_chat_background_opacity(raw.get("opacity")),
        # crop 缺失 → None（自动）。前端拿到 None 会自己算「按对话区比例取最大区域」。
        "crop": normalize_chat_background_crop(raw.get("crop")),
        "position_x": normalize_chat_background_position(raw.get("position_x")),
        "position_y": normalize_chat_background_position(raw.get("position_y")),
        "zoom": normalize_chat_background_zoom(raw.get("zoom")),
    }


def default_chat_background() -> dict[str, Any]:
    """默认背景设置（= 归一化后的空值，加载期与校验分支都复用）。"""
    return normalize_chat_background({})


# ---- 外观设置（主题/皮肤/会话字体）----
def clamp_chat_font_size(value: Any) -> int:
    """会话字号：能转成数值就夹回 13-18，转不动一律回默认值。

    越界不报错（它只是显示偏好，静默夹回比让整份设置保存失败更合理）；
    真正的"非法输入"由更新路径的 `_validated_chat_font_size` 显式拦住。
    """
    try:
        size = int(float(value))
    except (TypeError, ValueError):
        return CHAT_FONT_SIZE_DEFAULT
    return max(CHAT_FONT_SIZE_MIN, min(CHAT_FONT_SIZE_MAX, size))


def clean_chat_font_family_custom(value: Any) -> str:
    """自定义字体串：去空白 + 截断。它只作为 font-family 栈片段注入 CSS 变量。"""
    return str(value or "").strip()[:CHAT_FONT_FAMILY_CUSTOM_MAX]


def normalize_appearance(appearance: Any) -> dict[str, Any]:
    """归一化外观设置，供配置加载与运行时更新**共用**（与 chat_background 同款约定）。

    公开 settings 载荷里永远不出现非法枚举、越界字号或超长字体串；
    返回的键集固定，前端可以放心整体替换 state.appearance。
    """
    merged = dict(appearance) if isinstance(appearance, dict) else {}
    theme = str(merged.get("theme") or "").strip().lower()
    skin = str(merged.get("skin") or "").strip().lower()
    family = str(merged.get("chat_font_family") or "").strip().lower()
    return {
        "theme": theme if theme in APPEARANCE_THEMES else "system",
        "skin": skin if skin in APPEARANCE_SKINS else "violet",
        "chat_font_size": clamp_chat_font_size(merged.get("chat_font_size", CHAT_FONT_SIZE_DEFAULT)),
        "chat_font_family": family if family in APPEARANCE_CHAT_FONT_FAMILIES else "system",
        # 切换回非 custom 时保留用户输入过的串，避免"改一下又切回来"要重打一遍。
        "chat_font_family_custom": clean_chat_font_family_custom(merged.get("chat_font_family_custom")),
    }


def default_appearance() -> dict[str, Any]:
    """默认外观设置（= 归一化后的空值，默认配置与加载期兜底都复用）。"""
    return normalize_appearance({})


def validate_skills_dir(resolved: Path, *, app_dir: Path, public_dir: Path, data_dir: Path) -> None:
    """限制 Skill 目录范围，防止把高危目录暴露给扫描、解压和文件读取。"""
    resolved = resolved.resolve()
    if resolved.parent == resolved:
        raise ValueError("不能把磁盘根目录作为 Skill 目录")
    system_roots = [Path(os.environ.get("SystemRoot", r"C:\Windows"))]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(env_name)
        if value:
            system_roots.append(Path(value))
    for root in system_roots:
        root = root.resolve()
        if resolved == root or path_within(resolved, root):
            raise ValueError(f"不允许使用系统目录作为 Skill 目录：{root}")
    forbidden_exact = {Path.home().resolve(), app_dir, public_dir.resolve(), data_dir.resolve()}
    if resolved in forbidden_exact:
        raise ValueError("不能把用户主目录或程序自身目录作为 Skill 目录，请使用其子目录")


def default_config() -> dict[str, Any]:
    return {
        "host": "0.0.0.0",
        "port": 8765,
        "access_token": f"{secrets.randbelow(1000000):06d}",
        "skills_dirs": ["skills"],
        "hidden_skill_ids": [],
        "workspace_dir": "workspace",
        "data_dir": "data",
        "workspaces": [],
        # UI appearance preferences.  These are persisted server-side so all
        # clients connected to the same Naiba Chat instance share the choice.
        "appearance": {
            "theme": "system",
            "skin": "violet",
            # 会话区字号/字体族：只影响 .message-body，不动全局 UI 与等宽字体。
            "chat_font_size": CHAT_FONT_SIZE_DEFAULT,
            "chat_font_family": "system",
            "chat_font_family_custom": "",
        },
        # 对话区自定义背景图：image = data_dir/uploads 内的绝对路径（"" = 不启用），
        # opacity = 该图层的透明度，position_x/y + zoom = 编辑器里调出来的取景
        # （zoom 是相对「填满」的倍率；< 1 会四周留白，这正是"看全整张图"的手段）。
        # 图片以外的内容一律实底，所以不需要蒙层。
        "chat_background": default_chat_background(),
        # Per-user reusable system prompts for conversation settings.  These
        # live in config.json instead of the conversation database by design.
        "conversation_prompt_presets": [],
        # 用户自定义工具集（「我的工具集」）：必须落在 config.json 里，
        # 因为冻结版 pywebview 默认 private_mode=True，localStorage 每次退出都会被清空。
        "tool_sets": [],
        # 「新会话」种子消息模板（模型调用 reset_context 后填进输入框，由用户决定是否发送）。
        # 占位符：{handoff_path} / {task_count} / {task_list}；含占位符的行在任务数为 0 时整行去掉。
        "context_reset_seed_template": (
            "上一段会话已交接，交接文档：{handoff_path}\n"
            "请先读取该交接文档再继续。\n"
            "[后台任务] 当前仍有 {task_count} 个任务在运行：\n"
            "{task_list}"
        ),
        "provider_id": "",
        # Deprecated compatibility fields. They are retained for old config
        # files but are never used to build model requests.
        "temperature": 0.7,
        "max_tokens": 8192,
        "context_size": 8192,
        "agent_system_prompt": "",
        "permission_mode": "confirm",
        "agent_tools": [
            "read_file",
            "write_file",
            "list_directory",
            "search_files",
            "pwsh",
            "run_skill_script",
            "http_request",
        ],
        "command_timeout": 120,
        # 上下文用量提醒阈值（%）：圆环达到该百分比时前端弹窗提醒一次；0=关闭提醒。
        "context_warning_percent": 80,
        # 本地模型「首字节超时」（秒）：本地后端在 prefill 期间一个字节都不吐，
        # 「正在 prefill」和「永远做不完」在界面上无法区分。本地请求总超时是 1800 秒，
        # 不加这道闸用户要干等半小时才知道这一轮没戏（客户机实测整条会话看似永久卡死）。
        # 0 = 关闭本层。默认值与 llm.runtime.LOCAL_FIRST_BYTE_TIMEOUT_SECONDS 必须一致。
        "local_first_byte_timeout_seconds": 120,
        # 单轮 Agent 的模型调用次数上限（0=不限制）。Agent 循环是 while True，
        # 模型「一直调工具但拿不到结论」时以前可以无限烧下去（并一直占着本地锁）。
        # 默认值与 skills.agent.DEFAULT_MAX_STEPS 必须一致。
        # 键名故意**不叫** max_agent_steps：那是老版本遗留、且在启动时被显式丢弃的键
        # （见下方 self.data.pop("max_agent_steps")），沿用同名会让老配置里的残值突然生效、
        # 把用户的步数卡死在一个随手填过的小数字上。
        "agent_step_limit": 200,
        # 图片缓存：image_upload_original=True 按原尺寸存；False 则超过 image_max_pixels
        # 时用 Lanczos 压缩。缩略图始终从保存后的主图按 thumbnail_max_pixels 生成 WebP（_thumb.webp）。
        "imaging": {
            "image_upload_original": False,
            "image_max_pixels": 2000000,
            "thumbnail_max_pixels": 500000,
            # 图片缓存自动清理阈值（MB）：上传后总大小超限时自动删除最旧且未被
            # 消息/快照引用的缓存（引用中的文件永不自动删除）；0=关闭自动清理。
            "auto_clean_limit_mb": 256,
        },
        "providers": [],
        # MCP 服务默认不注册；只有用户显式配置并授权时才可连接。
        "mcp_servers": [],
        # 多 Agent 定义：每个 Agent 有独立的预设/规则（system_prompt）与固定 Skill（skill_ids）。
        "agents": [
            {
                "id": "general",
                "name": "通用 Agent",
                "system_prompt": "",
                # Domain Skills are routed from the current request or
                # explicitly selected by the user; do not inject them into
                # every general-agent turn.
                "skill_ids": [],
            },
            {
                "id": "coding",
                "name": "编程 Agent",
                "system_prompt": "你是资深编程助手。先理解需求，再给出可直接运行、结构清晰的代码；涉及文件操作时先说明改动范围。",
                "skill_ids": [],
            },
            {
                "id": "drama",
                "name": "短剧 Agent",
                "system_prompt": "你是短剧创作助手。遵循所选短剧类 Skill 的交互收集流程，逐步确认主题、角色、分镜与风格后再产出内容。",
                "skill_ids": [],
            },
        ],
        "default_agent_id": "general",
        # 视觉（Phase 0-3）：provider 缺省时使用内置 OVH 免费匿名视觉链兜底。
        # 视觉调用统一由模型驱动（vision_analyze 工具），无自动路由开关。
        "vision": {
            "provider_model_key": "",
            "fallback_models": [],
            "brain_supports_image": False,
            "timeout_ms": 180000,
            "cache": True,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 200,
            "max_images": 4,
        },
        # 联网搜索（PLAN4 §联网搜索）：完全可选；endpoint/Key/模型/启用状态由用户配置。
        "search": {
            "provider_id": "",
            "profiles": [],
            "endpoint": "",
            "api_key": "",
            "model": "",
            "max_results": 5,
        },
    }


# 内置 Agent 机制保留（built_in 标记 + 不可删守卫 + 前端「内置」徽标），但**当前清单为空**：
# 原先的四个预设（dsh-standard / dsh-code / dsh-minimal / dsh-cordis）已按用户要求下线。
# 需要重新引入内置 Agent 时，在 `built_in_agents()` 里补回定义即可（tool_scope 留空数组 =
# 不限制，运行时会放行全部工具并在新增工具时自动纳入，不需要再维护一份工具名清单）。


def built_in_agents() -> list[dict[str, Any]]:
    """返回内置 Agent 定义清单（当前为空，机制保留）。

    每次调用返回新副本，防止被外部篡改。清单为空时：
    - `public_agents()` 只返回用户自定义 Agent；
    - `upsert_agent()` 不再给任何 id 打 built_in 标记；
    - `delete_agent()` 的内置守卫不再命中，所有 Agent 都可删除。
    用户配置里遗留的旧内置副本由 `_migrate_agent_builtin_flags()` 去掉 built_in 标记。
    """
    return []


def built_in_agent_ids() -> set[str]:
    return {agent["id"] for agent in built_in_agents()}


# ---- 工具目录（Agent 编辑页的可选工具集）----
# 分类是**单一维度**（作用对象 + 风险），共 7 组：读取 / 写入 / 执行 / 联网 / 视觉 / 任务与扩展 / 长会话。
# 动态注册的 MCP 工具（mcp__<server>__<tool>）统一归入「联网与外部服务」，并按服务器名做
# 二级分组（见 tool_group_entries 的 subgroups）；分类说明与风险徽标见 TOOL_GROUP_INFO。
# 「长会话」= 翻历史（find_conversations / recall_history / read_conversation）+ 重置上下文
# （reset_context）：这一组只在会话层面起作用，成组勾选才有意义（见 TOOL_PRESETS 的 longsession）。
# 改名或合并分类时，TOOL_PRESETS 里引用的工具名要一起核对——守门见
# tests/test_agent_cards.py::ToolGroupCatalogTests。
_ALIAS_MAIN = {
    "read": "read_file", "write": "write_file", "edit": "edit_file",
    "grep": "search_files",
}
_MCP_GROUP = "联网与外部服务"
_LONG_SESSION_GROUP = "长会话"
_TOOL_GROUP = {
    "read_file": "读取与检索", "list_directory": "读取与检索", "search_files": "读取与检索",
    "find_conversations": _LONG_SESSION_GROUP, "recall_history": _LONG_SESSION_GROUP,
    "read_conversation": _LONG_SESSION_GROUP, "reset_context": _LONG_SESSION_GROUP,
    "read_pdf": "读取与检索", "pdf_render_pages": "读取与检索", "pdf_zoom_region": "读取与检索",
    "probe_video": "读取与检索", "extract_frames": "读取与检索",
    "write_file": "文件写入与编辑", "edit_file": "文件写入与编辑",
    "pwsh": "命令与脚本执行", "run_skill_script": "命令与脚本执行",
    "http_request": _MCP_GROUP, "web_search": _MCP_GROUP,
    "comfyui_prepare_workflow": _MCP_GROUP, "comfyui_batch": _MCP_GROUP,
    "register_mcp": _MCP_GROUP,
    "vision_analyze": "视觉与图片", "vision_image_ops": "视觉与图片",
    "run_in_background": "任务与扩展", "job_output": "任务与扩展", "job_status": "任务与扩展",
    "job_wait": "任务与扩展", "job_kill": "任务与扩展", "subagent": "任务与扩展",
    "todo_write": "任务与扩展",
    "install_skill": "任务与扩展", "unpack_skill_archive": "任务与扩展",
    "inspect_installed_skill": "任务与扩展",
}


def _mcp_subgroup(name: str) -> str:
    """mcp__<server>__<tool> → 二级分组名（服务器）；非 MCP 工具返回空串。"""
    if not name.startswith("mcp__"):
        return ""
    server, sep, _tool = name[len("mcp__"):].partition("__")
    return server if sep else ""
# 模型能力映射已随视觉单入口重构移除（vision_analyze 按会话能力换形态，不再按模型裁剪工具集）。
# 新建 Agent 的默认勾选 = 「标准模式」预设的工具集（守门测试钉死两者一致，
# 否则新建 Agent 打开时会显示「当前：自定义」而不是「标准模式」）。
#
# 文档 / 视频五件套（PDF 三件套 read_pdf / pdf_render_pages / pdf_zoom_region +
# 视频抽帧两件套 probe_video / extract_frames）**已并入预设与默认勾选**：
#   standard（13）/ longsession（17）/ comfyui（18）三档全收；
#   readonly 只收纯读取的两件（read_pdf / probe_video，side_effect=False）——
#   会写产物文件的 pdf_render_pages / pdf_zoom_region / extract_frames 不进只读。
# ⚠️ 依赖闭包：extract_frames 抽了帧需要 vision_analyze 才能读，而收它的三档显式预设
#    （standard / longsession / comfyui）本来都已含 vision_analyze（full 走 group:*），
#    故闭包自洽；只读那档不收 extract_frames（守门见 tests/test_agent_cards.py）。
_DEFAULT_SELECTED_TOOLS = frozenset({
    "read_file", "list_directory", "search_files",
    "read_pdf", "pdf_render_pages", "pdf_zoom_region",
    "probe_video", "extract_frames",
    "write_file", "edit_file", "pwsh", "run_skill_script",
    "vision_analyze",
})


def tool_catalog_entries(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 tool_registry schemas 构建 Agent 编辑页的工具目录（不显示 4 个 Harness 别名）。"""
    known = {str(spec.get("name") or ""): spec for spec in schemas if isinstance(spec, dict)}
    entries: list[dict[str, Any]] = []
    for name in known:
        if name in _ALIAS_MAIN:
            continue  # 别名与主工具等价，不单独显示
        description = str(known[name].get("description") or "")
        # 只取描述第一行作为短说明
        first_line = description.splitlines()[0] if description else ""
        entries.append({
            "name": name,
            "description": first_line[:120],
            "group": _TOOL_GROUP.get(name, _MCP_GROUP if name.startswith("mcp__") else "其他"),
            "subgroup": _mcp_subgroup(name),
            "model_target": "any",
            "default_selected": name in _DEFAULT_SELECTED_TOOLS,
            "alias_of": _ALIAS_MAIN.get(name),
        })
    # 端到端顺序：与 TOOL_GROUP_INFO 的分组顺序一致，让每组内工具顺序稳定（避免逐轮随机）
    order = (
        # 读取与检索
        "read_file", "list_directory", "search_files",
        "read_pdf", "pdf_render_pages", "pdf_zoom_region",
        "probe_video", "extract_frames",
        # 文件写入与编辑
        "write_file", "edit_file",
        # 命令与脚本执行
        "pwsh", "run_skill_script",
        # 联网与外部服务
        "http_request", "web_search", "register_mcp",
        "comfyui_prepare_workflow", "comfyui_batch",
        # 视觉与图片
        "vision_analyze", "vision_image_ops",
        # 任务与扩展
        "run_in_background", "job_output", "job_status", "job_wait", "job_kill", "subagent",
        "todo_write",
        "install_skill", "unpack_skill_archive", "inspect_installed_skill",
        # 长会话
        "find_conversations", "recall_history", "read_conversation", "reset_context",
    )
    index = {name: i for i, name in enumerate(order)}
    entries.sort(key=lambda item: (index.get(item["name"], 999), item["name"]))
    return entries


# ---- 工具分类说明（Agent 编辑页：给小白看的分类级解释）----
# 每项：name 分类名 / desc 一句话说明 / badge 风险徽标 / tone 徽标配色（safe|warn|danger|info）。
# 顺序即前端展示顺序；未列出的分组自动追加到末尾。
TOOL_GROUP_INFO: tuple[dict[str, str], ...] = (
    {"name": "读取与检索", "desc": "看文件、搜内容、读 PDF、抽视频帧、翻历史记录。只读，不改动任何东西",
     "badge": "只读", "tone": "safe"},
    {"name": "文件写入与编辑", "desc": "新建、改写、精确替换文件内容。会产生真实改动",
     "badge": "会改文件", "tone": "warn"},
    {"name": "命令与脚本执行", "desc": "在本机运行 PowerShell 与 Skill 脚本。能力最强，也最需要留意",
     "badge": "高风险", "tone": "danger"},
    {"name": "联网与外部服务", "desc": "联网搜索、抓网页、调接口；含 ComfyUI 直连与 MCP 外部能力（按服务器分组）",
     "badge": "联网", "tone": "info"},
    {"name": "视觉与图片", "desc": "图片解读：描述、定位、检测、OCR、取色、裁剪、对比；多模态模型可直接看图",
     "badge": "会写产物", "tone": "warn"},
    {"name": "任务与扩展", "desc": "后台长任务、子 Agent、任务清单；安装与检查 Skill，让 Agent 自己扩展能力",
     "badge": "会改动", "tone": "warn"},
    {"name": "长会话", "desc": "翻历史（列会话 / 检索 / 读原文）与重置上下文：只在会话层面起作用，不改文件",
     "badge": "会话控制", "tone": "info"},
    {"name": "其他", "desc": "未归类工具", "badge": "", "tone": "info"},
)


def tool_group_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 TOOL_GROUP_INFO 的顺序输出分类。

    每组返回：
    - tools：该分类下全部工具名（整组全选与计数用，含二级分组里的）；
    - direct_tools：不属于任何二级分组的工具名（前端先渲染这一批）；
    - subgroups：[{name, tools}]，按工具目录顺序聚合（MCP 动态工具未登记在 order 表里，
      组内即按名字排序；当前用于 MCP 按服务器分组）。
    """
    by_group: dict[str, list[str]] = {}
    subgroup_of: dict[str, str] = {}
    for item in entries:
        name = str(item.get("name") or "")
        by_group.setdefault(str(item.get("group") or "其他"), []).append(name)
        sub = str(item.get("subgroup") or "")
        if sub:
            subgroup_of[name] = sub
    known = {str(info["name"]): info for info in TOOL_GROUP_INFO}

    def _build(name: str, info: dict[str, str]) -> dict[str, Any]:
        tools = by_group[name]
        buckets: dict[str, list[str]] = {}
        for tool in tools:
            sub = subgroup_of.get(tool)
            if sub:
                buckets.setdefault(sub, []).append(tool)
        return {
            "name": name,
            "desc": str(info.get("desc") or ""),
            "badge": str(info.get("badge") or ""),
            "tone": str(info.get("tone") or "info"),
            "tools": tools,
            "direct_tools": [tool for tool in tools if tool not in subgroup_of],
            "subgroups": [{"name": sub, "tools": members} for sub, members in buckets.items()],
        }

    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    for info in TOOL_GROUP_INFO:
        name = str(info["name"])
        if name not in by_group:
            continue
        seen.add(name)
        groups.append(_build(name, info))
    # 兜底：工具目录里出现了未登记的新分组时追加到末尾，不至于丢工具。
    for name in sorted(by_group):
        if name in seen:
            continue
        groups.append(_build(name, known.get(name, {"name": name, "desc": "", "badge": "", "tone": "info"})))
    return groups


# ---- 工具集预设（Agent 编辑页：一键选中一批工具）----
# 5 档：只读模式 6 / 标准模式 13 / 长会话模式 17 / ComfyUI 联动 18 / 全能模式 group:*（33），
# 按能力从小到大排。
# include 支持两种写法：具体工具名，或 "group:分类名"（"group:*" 表示所有分类）。
# exclude 用于从已包含的分类里再剔除个别工具。
# 注意：分类收敛为 7 组后，组的粒度比单个预设的意图更粗（「联网与外部服务」同时含 ComfyUI
# 与 MCP 动态工具、「任务与扩展」含 Skill 管理、「读取与检索」同时含纯读取的 read_pdf /
# probe_video 与会写产物的 pdf_render_pages / pdf_zoom_region / extract_frames），
# 因此**除 group:* 外一律显式列工具名**，保持每个预设的语义精确。
# 写错的组名/工具名由 resolve_tool_preset 告警 + 守门测试兜住。
# 另一个硬约束：预设的工具名必须**已经包含依赖闭包**（如 ComfyUI 预设显式带 job_output/
# job_status/job_wait），否则「下拉显示的个数」与「套用后的实际个数」会不一致。
TOOL_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "readonly",
        "name": "只读模式",
        "tagline": "只读不改",
        "desc": "只能看和搜文件、读 PDF 文本、看视频元信息、看图片。不写文件、不跑命令、不联网，最省心。",
        # 只读边界以 ToolSpec.side_effect 为准：read_pdf / probe_video 是纯读取；
        # pdf_render_pages / pdf_zoom_region / extract_frames 会写产物文件，故不进本档。
        "include": [
            "read_file", "list_directory", "search_files",
            "read_pdf", "probe_video",
            "vision_analyze",
        ],
    },
    {
        "id": "standard",
        "name": "标准模式",
        "tagline": "日常推荐",
        "desc": "读写文件 + 搜索 + 跑命令 + 看图 + 读 PDF / 抽视频帧，覆盖绝大多数本机任务。",
        "include": [
            "read_file", "list_directory", "search_files",
            "read_pdf", "pdf_render_pages", "pdf_zoom_region",
            "probe_video", "extract_frames",
            "write_file", "edit_file", "pwsh", "run_skill_script",
            "vision_analyze",
        ],
    },
    {
        "id": "longsession",
        "name": "长会话模式",
        "tagline": "标准 + 翻历史",
        "desc": "标准模式全部能力，外加翻历史（列会话 / 检索 / 读原文）与重置上下文，适合长会话与跨会话回忆。",
        "include": [
            "read_file", "list_directory", "search_files",
            "read_pdf", "pdf_render_pages", "pdf_zoom_region",
            "probe_video", "extract_frames",
            "write_file", "edit_file", "pwsh", "run_skill_script",
            "vision_analyze",
            "find_conversations", "recall_history", "read_conversation", "reset_context",
        ],
    },
    {
        "id": "comfyui",
        "name": "ComfyUI 联动",
        "tagline": "批量出图",
        "desc": "标准能力 + ComfyUI 工作流与批量出图，并带上任务查询工具。走 HTTP 通道直连本机 ComfyUI，不启用任何 MCP 连接。",
        "include": [
            "read_file", "list_directory", "search_files",
            "read_pdf", "pdf_render_pages", "pdf_zoom_region",
            "probe_video", "extract_frames",
            "write_file", "edit_file", "pwsh", "run_skill_script", "vision_analyze",
            "comfyui_prepare_workflow", "comfyui_batch",
            # comfyui_batch 的依赖闭包（JOB_CREATOR_TOOL_DEPS / 前端 AGENT_TOOL_DEP_RULES）：
            # 必须显式列出，否则下拉显示的个数会小于套用后的实际个数（选中即被闭包补上）。
            "job_output", "job_status", "job_wait",
        ],
        "exclude_mcp": True,
    },
    {
        "id": "full",
        "name": "全能模式",
        "tagline": "全部工具",
        "desc": "开启所有已注册工具（含 MCP、ComfyUI、能力管理、视觉全套）。能力最强，误操作风险也最高。",
        "include": ["group:*"],
    },
)


def resolve_tool_preset(preset: dict[str, Any], entries: list[dict[str, Any]]) -> list[str]:
    """把一个预设展开成具体工具名列表（按工具目录顺序返回）。

    引用了不存在的分类名或工具名时**记录告警**而非静默丢弃——分类改名漏改预设会让
    「一键套用」悄悄少选一批工具，属于难察觉的错配（守门见 tests/test_agent_cards.py）。
    """
    by_group: dict[str, list[str]] = {}
    for item in entries:
        by_group.setdefault(str(item.get("group") or "其他"), []).append(str(item.get("name") or ""))
    known_names = {str(item.get("name") or "") for item in entries}
    selected: set[str] = set()
    unknown: list[str] = []
    for raw in preset.get("include") or []:
        item = str(raw)
        if item.startswith("group:"):
            group = item[len("group:"):]
            if group == "*":
                for names in by_group.values():
                    selected.update(names)
            elif group in by_group:
                selected.update(by_group[group])
            else:
                unknown.append(item)
        elif item in known_names:
            selected.add(item)
        else:
            unknown.append(item)
    for raw in preset.get("exclude") or []:
        item = str(raw)
        if item not in known_names:
            unknown.append(f"exclude:{item}")
        selected.discard(item)
    if unknown:
        logger.warning(
            "工具预设 %s 引用了不存在的分组/工具：%s（当前分组：%s）",
            preset.get("id"), "、".join(unknown), "、".join(sorted(by_group)),
        )
    # exclude_mcp：预设声明“不启用 MCP 通道”时，无论 include 怎么展开，都剔除
    # 动态 MCP 工具（mcp__<server>__<tool>）与 MCP 网关入口（register_mcp），
    # 防止将来新增 MCP 服务/分类后自动污染本预设。
    if preset.get("exclude_mcp"):
        selected = {
            n for n in selected
            if not n.startswith("mcp__") and n not in ("register_mcp",)
        }
    order = {str(item.get("name")): i for i, item in enumerate(entries)}
    return sorted(selected, key=lambda name: order.get(name, 999))


def tool_preset_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """输出给前端的预设清单（工具名已解析好，前端无需再算）。"""
    return [
        {
            "id": str(preset["id"]),
            "name": str(preset["name"]),
            "tagline": str(preset.get("tagline") or ""),
            "desc": str(preset.get("desc") or ""),
            "tools": resolve_tool_preset(preset, entries),
        }
        for preset in TOOL_PRESETS
    ]


# 在线请求协议集合；llama.cpp 提供 OpenAI 兼容接口，但服务进程仍在本机。
ONLINE_REQUEST_FORMATS = {"openai_chat", "codex_responses", "gemini", "claude"}
LOCAL_REQUEST_FORMATS = {"ollama", "lm_studio", "llama_cpp", "unsloth"}
VALID_MODEL_KINDS = {"online", "local"}
VALID_LOCAL_BACKENDS = {"ollama", "lm_studio", "llama_cpp", "unsloth"}


def _infer_kind_for_request_format(request_format: str) -> str:
    """根据请求格式推断模型类别，兼容未携带 kind 的旧配置。"""
    return "local" if request_format in LOCAL_REQUEST_FORMATS else "online"


# 快捷消息排序权重：点击数为主、新鲜度加分防"新条目永远沉底"。
QUICK_MESSAGE_USE_CAP = 50
QUICK_MESSAGE_RECENCY_BONUS = ((7, 6), (30, 3), (90, 1))

# 「我的工具集」上限（与前端 TOOL_SET_MAX 一致）：超出后保留最新的一批。
TOOL_SET_MAX = 30


def _clean_tool_set_tools(tools: Any) -> list[str]:
    """规整工具名列表：只收字符串、去空、去重、保持顺序；非列表一律当空集。"""
    if not isinstance(tools, list):
        return []
    result: list[str] = []
    for raw in tools:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if name and name not in result:
            result.append(name)
    return result


def _quick_message_entries(items: Any) -> list[dict[str, Any]]:
    """规整快捷消息条目：正文 + 使用统计（``index`` 恒为原始插入序号）。

    正文为空的条目跳过（占位不影响 index 定位）；旧数据里的 ``title`` 字段忽略。
    内置预设条目带 ``preset_id``（用户可编辑/删除，升级不覆盖也不复活，见
    ``BUILTIN_QUICK_MESSAGES``）。
    """
    result: list[dict[str, Any]] = []
    for position, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        entry = {
            "index": position,
            "text": text,
            "count": max(0, int(item.get("count") or 0)),
            "added_at": max(0, int(item.get("added_at") or 0)),
            "used_at": max(0, int(item.get("used_at") or 0)),
        }
        preset_id = _quick_preset_id(item)
        if preset_id:
            entry["preset_id"] = preset_id
        result.append(entry)
    return result


def quick_message_score(entry: dict[str, Any], now_ms: int) -> float:
    """快捷消息权重：min(点击次数, 50)×2 + 新鲜度加分（7 天 +6 / 30 天 +3 / 90 天 +1）。"""
    count = max(0, int(entry.get("count") or 0))
    score = min(count, QUICK_MESSAGE_USE_CAP) * 2
    added_at = max(0, int(entry.get("added_at") or 0))
    if added_at:
        age_days = max(0.0, (now_ms - added_at) / 86400000.0)
        for days, bonus in QUICK_MESSAGE_RECENCY_BONUS:
            if age_days <= days:
                score += bonus
                break
    return score


# ---- 开始页「自定义指令」的内置预设 ----
# 这些卡片此前写死在 index.html 里（不可编辑、不可删除）；现在并入 `starter_prompts`，
# 与用户自建条目同权（可编辑/可删除）。每个预设带**稳定 id**：
# - 用户改标题/改正文不影响识别（条目上记 `preset_id`）；
# - 用户删除的 id 记入 `starter_presets_dismissed`，永不自动复活（要恢复走「恢复默认预设」）；
# - 版本新增的预设（id 不在列表里、也没被删过）会在下次启动自动补齐。
# 键：id 稳定标识 / title 标题 / text 指令正文 / desc 副标题 / icon 图标名（前端映射为 SVG）。
BUILTIN_STARTER_PRESETS: tuple[dict[str, str], ...] = (
    {
        "id": "comfy-mcp",
        "title": "通过 MCP 调用 ComfyUI",
        "desc": "连接 ComfyUI MCP 服务",
        "icon": "list",
        "text": (
            "确认 ComfyUI mcp 服务是否正常；若无法连接 mcp 服务，提示用户连接 ComfyUI。"
            "确认接口可用后，等待用户指令，后续只允许通过 mcp 工具调用 ComfyUI 进行生成任务。"
            "将当前工作区目录下的所有工作流 json 文件加上 '_backup' 后缀复制一份，"
            "直接覆盖可能已经存在的带 '_backup' 后缀的同名文件。本次只做复制操作，"
            "不得读取工作流文件内容。忽略文件夹内带 '_backup' 后缀的所有工作流。"
        ),
    },
    {
        "id": "comfy-http",
        "title": "通过 HTTP 调用 ComfyUI",
        "desc": "启动 ComfyUI 后使用",
        "icon": "sparkle",
        "text": (
            "先探测 ComfyUI 是否已启动（GET http://127.0.0.1:8188/system_stats）；"
            "若未启动，提示用户启动 ComfyUI。确认接口可用后，等待用户指令，"
            "后续通过 HTTP API 或 comfy CLI 完成用户要求的生成任务。"
            "将当前工作区目录下的所有工作流 json 文件加上 '_backup' 后缀复制一份，"
            "直接覆盖可能已经存在的带 '_backup' 后缀的同名文件。本次只做复制操作，"
            "不得读取工作流文件内容。忽略文件夹内带 '_backup' 后缀的所有工作流。"
        ),
    },
    {
        "id": "comfy-mcp-setup",
        "title": "设置本地 Comfy MCP",
        "desc": "配置连接与工具",
        "icon": "link",
        "text": (
            "帮我设置本地 Comfy MCP 连接，按照 "
            "https://docs.comfy.org/agent-tools/mcp.md#local-comfy-mcp-connection 的设置指南操作。"
            "优先使用本地 Comfyui 的 python 环境。当发现不止一个的时候，优先寻找正在运行的 Comfyui "
            "对应的环境。当发现没有已运行的 Comfyui 但本地存在多个 Comfyui 环境时，"
            "停止行动并向用户发出询问。安装完成 mcp 服务后，记得提醒用户在 mcp 相关的 agent "
            "设置页面内手动开启由 mcp 服务所引入的新的 comfy mcp tools。"
        ),
    },
    {
        "id": "list-tools",
        "title": "列出可用工具",
        "desc": "查看当前能力",
        "icon": "wrench",
        "text": "列出你当前所有可用工具。",
    },
    {
        "id": "list-files",
        "title": "列出所有文件",
        "desc": "浏览当前目录",
        "icon": "folder",
        "text": "列出当前文件夹下的所有文件。",
    },
    {
        "id": "await-instructions",
        "title": "等待用户指令",
        "desc": "先理解系统指令",
        "icon": "clock",
        "text": "不进行任何操作，先理解你已接收到的系统指令，然后等待后续命令。",
    },
)

# 用户主动删除的内置预设 id 清单：这些 id 不再自动补回（「恢复默认预设」会清空它）。
STARTER_PRESET_DISMISSED_KEY = "starter_presets_dismissed"
# 旧方案（一次性并入布尔标记）的迁移来源：置位时把"当前缺失的内置预设"视为用户已删除。
_LEGACY_STARTER_PRESET_SEED_KEY = "starter_presets_seeded"

# ---- 会话内「快捷消息」的内置预设 ----
# 与开始页预设同款语义：条目带稳定 `preset_id`，用户可编辑/删除；
# - 编辑只改正文，`preset_id` 原样保留 → 升级时不会被"补回"成原版；
# - 删除的 id 记入 `quick_message_presets_dismissed`，永不自动复活；
# - 版本新增的预设（id 未出现、也未被删过）在下次启动自动补齐。
BUILTIN_QUICK_MESSAGES: tuple[dict[str, str], ...] = (
    {
        "id": "handoff-report",
        "text": (
            "现在写一份交接报告，然后调用一次 reset_context 重置上下文。\n"
            "报告写进当前工作区（文件名建议：交接报告_<主题>_<日期>.md），必须包含：\n"
            "1) 任务目标与当前进度（已完成 / 待办，逐条写清）；\n"
            "2) 关键文件与路径（产物、脚本、配置，写绝对路径）；\n"
            "3) 已确认的决定与约束（用户明确要求过的口径，不要遗漏）；\n"
            "4) 仍在运行的后台任务（job id、用途、怎么取结果）；\n"
            "5) 下一步该做什么（给接手者的第一条指令）。\n"
            "写完用 read_file 复核文件非空，再把该文件的绝对路径传给 reset_context 的 handoff_path。"
            "本轮不要再做别的活；若当前工具集没有 reset_context，就只写报告并告诉我。"
        ),
    },
)

# 用户主动删除的内置快捷消息 id 清单：不再自动补回。
QUICK_MESSAGE_PRESET_DISMISSED_KEY = "quick_message_presets_dismissed"


def _quick_preset_id(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("preset_id") or "")


def _starter_preset_id(entry: Any) -> str:
    """条目对应的内置预设 id：优先 `preset_id`，旧条目按标题回退匹配（空串=用户自建）。"""
    if not isinstance(entry, dict):
        return ""
    explicit = str(entry.get("preset_id") or "")
    if explicit:
        return explicit
    title = str(entry.get("title") or "").strip().casefold()
    for preset in BUILTIN_STARTER_PRESETS:
        if preset["title"].casefold() == title:
            return preset["id"]
    return ""


def _starter_preset_entry(preset: dict[str, str]) -> dict[str, str]:
    """内置预设 → 存入 `starter_prompts` 的条目（`id` 改名为 `preset_id`）。"""
    entry = dict(preset)
    entry["preset_id"] = entry.pop("id")
    return entry


class ConfigStore:
    def __init__(self, path: Path, paths: PathContext | None = None):
        self.path = path
        self._paths = paths or PathContext.local(Path(path).parent, Path(path))
        self.lock = threading.RLock()
        defaults = default_config()
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    defaults.update(loaded)
            except (OSError, json.JSONDecodeError):
                pass
        # 嵌套默认值合并：用户配置若只写了部分子字段，补齐缺失键。
        for key in ("vision", "search", "appearance", "chat_background"):
            merged = dict(default_config().get(key, {}))
            if isinstance(defaults.get(key), dict):
                merged.update(defaults[key])
            if key == "appearance":
                # Normalize hand-edited/legacy config values.  Invalid enum
                # values should never leak into the public settings payload.
                merged = normalize_appearance(merged)
            elif key == "chat_background":
                # 手改过 / 旧配置（没有 position_x/y·zoom 三个键）都在这里补齐并归一化：
                # 前端拿到什么就画什么，不能让 NaN、越界值或缺失键漏进公开设置。
                merged = normalize_chat_background(merged)
            defaults[key] = merged
        # Build 74 changes the historical 120-second Run-wide vision budget
        # into a 180-second timeout for each individual visual request. Only
        # migrate the old default; preserve explicit custom timeout values.
        vision = defaults.get("vision")
        if isinstance(vision, dict) and vision.get("timeout_ms") == 120000:
            vision["timeout_ms"] = 180000
        # 自动路由已移除（视觉统一由模型按需调用 vision_analyze）：清理旧配置残留键。
        if isinstance(vision, dict):
            vision.pop("auto_route", None)
            defaults["vision"] = vision
        # MCP 配置去重：重复 server id 只保留首个（PLAN4 §MCP）。
        servers = defaults.get("mcp_servers")
        if isinstance(servers, list):
            seen: dict[str, int] = {}
            deduped = []
            for server in servers:
                if not isinstance(server, dict):
                    continue
                sid = str(server.get("id") or "").strip()
                # The legacy custom ComfyUI bridge is retired. Never revive it
                # from a migrated per-user config or an older portable build.
                if sid == "comfyui":
                    continue
                if not sid or sid in seen:
                    if sid:
                        print(f"[config] Ignored duplicate MCP server id: {sid}")
                    continue
                seen[sid] = 1
                deduped.append(server)
            defaults["mcp_servers"] = deduped
        # Remove the retired bundled Skill from migrated skill roots. The
        # official first-party Skill is the only Comfy MCP integration.
        roots = defaults.get("skills_dirs")
        if isinstance(roots, list):
            defaults["skills_dirs"] = [
                item for item in roots
                if "comfyui-mcp" not in str(item).lower()
            ]
        if not path.exists():
            # 全新安装（尚无 config.json）：默认关闭代理（强制直连），
            # 与旧配置升级保持「跟随系统代理」的兼容行为区分开。
            defaults["proxy"] = {
                "enabled": False,
                "url": "",
                "use_system_fallback": False,
            }
        self.data = defaults
        self._migrate_conversation_prompt_presets()
        self._migrate_tool_sets()
        self._sync_starter_presets()
        self._sync_builtin_quick_messages()
        # Legacy builds persisted max_agent_steps; it is intentionally ignored.
        self.data.pop("max_agent_steps", None)
        self._migrate_default_agent_skills()
        self._migrate_legacy_tool_names()
        self._migrate_agent_builtin_flags()
        tools = self.data.get("agent_tools")
        # run_command 已并入 pwsh：历史默认集里保存的是 run_command（而非 pwsh）。
        # 先统一映射死工具名，避免升级后通用 Agent 静默丢失命令执行能力。
        from naiba.tools.registry import RETIRED_TOOL_MAP

        if isinstance(tools, list):
            mapped = [
                "pwsh" if str(item) == "run_command" else RETIRED_TOOL_MAP.get(str(item), item)
                for item in tools
            ]
            # MCP is an explicit external integration, never a default capability.
            # Remove the exact historical default pair while preserving a user's
            # separately selected MCP tools and configured server definitions.
            legacy_default = {
                "read_file", "write_file", "list_directory", "search_files",
                "run_skill_script", "http_request",
                "register_mcp",
            }
            # 旧配置只要等同于「历史默认工具集」（含 run_command 或已为 pwsh 都算）
            # 就移除默认 MCP 入口；定制过的工具集保留原选择，仅做死工具名映射。
            if set(mapped) <= legacy_default | {"pwsh", "call_mcp"}:
                self.data["agent_tools"] = [
                    item for item in mapped if item not in {"register_mcp", "call_mcp"}
                ]
            elif mapped != tools:
                self.data["agent_tools"] = mapped
        # 在线/本地模型配置分层：为旧 providers 补全 kind/local_backend，并生成 default_model_key。
        self._migrate_model_profiles()
        self.save()

    def _migrate_default_agent_skills(self) -> None:
        """Remove historical domain Skills from the general Agent default."""
        legacy = {"0a3afda21c5622e1", "e03778f862d10595"}
        agents = self.data.get("agents")
        if not isinstance(agents, list):
            return
        for agent in agents:
            if not isinstance(agent, dict) or str(agent.get("id") or "") != "general":
                continue
            skills = agent.get("skill_ids")
            if not isinstance(skills, list):
                agent["skill_ids"] = []
                continue
            agent["skill_ids"] = [str(item) for item in skills if str(item) not in legacy]

    def _migrate_legacy_tool_names(self) -> None:
        """run_command 已并入 pwsh、call_mcp 已移除、视觉旧名已并入新入口：清理持久化工具名。"""
        from naiba.tools.registry import RETIRED_TOOL_MAP

        agents = self.data.get("agents")
        if not isinstance(agents, list):
            return
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            scope = agent.get("tool_scope")
            if isinstance(scope, list):
                agent["tool_scope"] = [
                    "pwsh" if str(item) == "run_command" else RETIRED_TOOL_MAP.get(str(item), item)
                    for item in scope
                    if str(item) != "call_mcp"
                ]

    def _migrate_agent_builtin_flags(self) -> None:
        """清掉已下线内置 Agent 遗留的 built_in 标记。

        内置清单现在为空，但用户配置里可能还留着曾经编辑过的旧内置副本
        （例如 dsh-standard，带 built_in=True）。不清掉的话前端会继续显示「内置」并隐藏
        删除按钮，而后端已经允许删除——两边口径不一致，用户会觉得「删不掉」。
        只摘标记，不动名称/提示词/工具集，用户内容不丢。
        """
        agents = self.data.get("agents")
        if not isinstance(agents, list):
            return
        built_in = built_in_agent_ids()
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if agent.get("built_in") and str(agent.get("id") or "") not in built_in:
                agent.pop("built_in", None)

    def _migrate_conversation_prompt_presets(self) -> None:
        """Normalize prompt presets from config files created by older builds."""
        raw_items = self.data.get("conversation_prompt_presets", [])
        if not isinstance(raw_items, list):
            raw_items = []
        normalized: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            prompt = str(raw.get("system_prompt") or raw.get("text") or "").strip()
            if not prompt:
                continue
            preset_id = str(raw.get("id") or "").strip()
            if not preset_id or preset_id in seen_ids:
                preset_id = uuid.uuid4().hex
            seen_ids.add(preset_id)
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            normalized.append({
                "id": preset_id,
                "title": " ".join(str(raw.get("title") or "快捷系统提示词").split())[:80] or "快捷系统提示词",
                "system_prompt": prompt[:20000],
                "source": str(raw.get("source") or "manual")[:40] or "manual",
                "created_at": str(raw.get("created_at") or now),
                "updated_at": str(raw.get("updated_at") or raw.get("created_at") or now),
            })
        self.data["conversation_prompt_presets"] = normalized

    def _migrate_tool_sets(self) -> None:
        """Normalize user tool sets（旧配置/手改 config.json 都收敛成统一结构）。"""
        raw_items = self.data.get("tool_sets", [])
        if not isinstance(raw_items, list):
            raw_items = []
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            tools = _clean_tool_set_tools(raw.get("tools"))
            if not tools:
                continue
            set_id = str(raw.get("id") or "").strip()
            if not set_id or set_id in seen_ids:
                set_id = uuid.uuid4().hex
            seen_ids.add(set_id)
            normalized.append({
                "id": set_id,
                "name": " ".join(str(raw.get("name") or "").split())[:40] or "自定义工具集",
                "tools": tools,
                "created_at": str(raw.get("created_at") or now),
                "updated_at": str(raw.get("updated_at") or raw.get("created_at") or now),
            })
            if len(normalized) >= TOOL_SET_MAX:
                break
        self.data["tool_sets"] = normalized

    def get_tool_sets(self) -> list[dict[str, Any]]:
        with self.lock:
            items = self.data.get("tool_sets", [])
            if not isinstance(items, list):
                return []
            return [dict(item) for item in items if isinstance(item, dict)]

    def upsert_tool_set(self, name: str, tools: Any, set_id: str = "") -> dict[str, Any]:
        """新增或原地更新一套「我的工具集」；set_id 命中时更新，否则插到最前。"""
        cleaned = _clean_tool_set_tools(tools)
        if not cleaned:
            raise ValueError("工具集不能为空")
        label = " ".join(str(name or "").split())[:40]
        target = str(set_id or "").strip()
        with self.lock:
            items = self.data.setdefault("tool_sets", [])
            if not isinstance(items, list):
                items = []
                self.data["tool_sets"] = items
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if target:
                for item in items:
                    if isinstance(item, dict) and str(item.get("id") or "") == target:
                        item["name"] = label or str(item.get("name") or "自定义工具集")
                        item["tools"] = cleaned
                        item["updated_at"] = now
                        self.save()
                        return dict(item)
            item = {
                "id": uuid.uuid4().hex,
                "name": label or "自定义工具集",
                "tools": cleaned,
                "created_at": now,
                "updated_at": now,
            }
            items.insert(0, item)
            del items[TOOL_SET_MAX:]
            self.save()
            return dict(item)

    def delete_tool_set(self, set_id: str) -> bool:
        target = str(set_id or "").strip()
        if not target:
            return False
        with self.lock:
            items = self.data.get("tool_sets", [])
            if not isinstance(items, list):
                return False
            filtered = [item for item in items
                        if not isinstance(item, dict) or str(item.get("id") or "") != target]
            if len(filtered) == len(items):
                return False
            self.data["tool_sets"] = filtered
            self.save()
            return True

    def _sync_starter_presets(self) -> None:
        """按"已删除 id 清单"补齐缺失的内置预设（每次加载都跑，幂等）。

        - 版本新增的内置预设（id 未被删除过）会自动出现；
        - 用户删掉的 id 记在 `starter_presets_dismissed` 里，永不自动复活
          （点「恢复默认预设」才回来）；
        - 旧条目按标题回退识别并补写 `preset_id`：改过标题的条目靠 id 识别，
          不会被当成"缺失"而重复插入。
        """
        prompts = self.data.get("starter_prompts")
        if not isinstance(prompts, list):
            prompts = []
        prompts = [item for item in prompts if isinstance(item, dict)]
        dismissed = self.data.get(STARTER_PRESET_DISMISSED_KEY)
        dismissed_ids = {str(item) for item in dismissed} if isinstance(dismissed, list) else set()
        # 旧方案迁移：曾置位一次性标记 → 当前缺失的内置预设视为"用户删掉的"
        if self.data.pop(_LEGACY_STARTER_PRESET_SEED_KEY, False):
            present = {_starter_preset_id(item) for item in prompts}
            for preset in BUILTIN_STARTER_PRESETS:
                if preset["id"] not in present:
                    dismissed_ids.add(preset["id"])
        # 回写 preset_id（旧条目按标题匹配）
        changed = False
        for item in prompts:
            if not item.get("preset_id"):
                matched = _starter_preset_id(item)
                if matched:
                    item["preset_id"] = matched
                    changed = True
        present_ids = {_starter_preset_id(item) for item in prompts}
        seeded = [
            _starter_preset_entry(preset)
            for preset in BUILTIN_STARTER_PRESETS
            if preset["id"] not in present_ids and preset["id"] not in dismissed_ids
        ]
        if seeded:
            prompts = seeded + prompts
            changed = True
        if changed or self.data.get(STARTER_PRESET_DISMISSED_KEY) != sorted(dismissed_ids):
            self.data["starter_prompts"] = prompts
            self.data[STARTER_PRESET_DISMISSED_KEY] = sorted(dismissed_ids)
            self.save()

    def _sync_builtin_quick_messages(self) -> None:
        """按"已删除 id 清单"补齐缺失的内置快捷消息（每次加载都跑，幂等）。

        与 `_sync_starter_presets` 同款语义：用户改过正文的条目靠 `preset_id` 识别，
        不会被原版覆盖、也不会被重复插入；删掉的 id 记入删除清单，永不自动复活。
        """
        items = self.data.get("quick_messages")
        if not isinstance(items, list):
            items = []
        items = [item for item in items if isinstance(item, dict)]
        dismissed = self.data.get(QUICK_MESSAGE_PRESET_DISMISSED_KEY)
        dismissed_ids = {str(item) for item in dismissed} if isinstance(dismissed, list) else set()
        present_ids = {_quick_preset_id(item) for item in items} - {""}
        missing = [preset for preset in BUILTIN_QUICK_MESSAGES
                   if preset["id"] not in present_ids and preset["id"] not in dismissed_ids]
        if not missing:
            return
        now = int(time.time() * 1000)
        items.extend({
            "text": preset["text"],
            "count": 0,
            "added_at": now,
            "used_at": 0,
            "preset_id": preset["id"],
        } for preset in missing)
        self.data["quick_messages"] = items
        self.save()

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)

    def public(self) -> dict[str, Any]:
        with self.lock:
            result = {
                key: value
                for key, value in self.data.items()
                if key not in {
                    "access_token", "providers", "mcp_servers",
                    "temperature", "max_tokens", "context_size", "conversation_prompt_presets",
                    "tool_sets",
                }
            }
            result["resolved_workspace_dir"] = str(self.resolve_workspace_dir())
            result["resolved_data_dir"] = str(self.resolve_data_dir())
            return result

    def get_skills_dirs(self) -> list[str]:
        with self.lock:
            return list(self.data.get("skills_dirs", []))

    def get_hidden_skill_ids(self) -> list[str]:
        with self.lock:
            values = self.data.get("hidden_skill_ids", [])
            return [str(item) for item in values] if isinstance(values, list) else []

    def hide_skill(self, skill_id: str) -> list[str]:
        skill_id = str(skill_id or "").strip()
        if not skill_id:
            return self.get_hidden_skill_ids()
        with self.lock:
            hidden = self.data.setdefault("hidden_skill_ids", [])
            if skill_id not in hidden:
                hidden.append(skill_id)
                self.save()
            return list(hidden)

    def unhide_skill(self, skill_id: str) -> list[str]:
        """从 hidden_skill_ids 移除该 id 并持久化；与 hide_skill 对称。"""
        skill_id = str(skill_id or "").strip()
        with self.lock:
            hidden = self.data.setdefault("hidden_skill_ids", [])
            if skill_id in hidden:
                hidden.remove(skill_id)
                self.save()
            return list(hidden)

    def add_skills_dir(self, raw: str) -> str:
        raw = (raw or "").strip()
        if not raw:
            raise ValueError("目录路径不能为空")
        resolved = self._resolve_dir(raw)
        validate_skills_dir(resolved, app_dir=self._paths.app_dir, public_dir=self._paths.public_dir, data_dir=self._paths.data_dir)
        with self.lock:
            dirs = self.data.setdefault("skills_dirs", [])
            if raw not in dirs:
                dirs.append(raw)
            self.save()
        return str(resolved)

    def remove_skills_dir(self, raw: str) -> list[str]:
        raw = (raw or "").strip()
        resolved = str(self._resolve_dir(raw)) if raw else ""
        with self.lock:
            dirs = self.data.setdefault("skills_dirs", [])
            self.data["skills_dirs"] = [
                item for item in dirs if item != raw and str(self._resolve_dir(item)) != resolved
            ]
            self.save()
            return list(self.data["skills_dirs"])

    def get_starter_prompts(self) -> list[dict[str, str]]:
        """开始新对话页的「自定义指令」（插入顺序）。

        与「快捷消息」是两份互不干扰的列表：本列表只服务开始页卡片，
        快捷消息面板走 `quick_messages`（带使用统计与权重排序）。
        """
        with self.lock:
            items = self.data.get("starter_prompts", [])
            if isinstance(items, list):
                return [dict(item) for item in items if isinstance(item, dict)]
            return []

    @staticmethod
    def _preset_timestamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _unique_conversation_preset_title(self, requested: str, *, ignore_id: str = "") -> str:
        base = " ".join(str(requested or "").strip().split())[:80] or "快捷系统提示词"
        existing = {
            str(item.get("title") or "").casefold()
            for item in self.data.get("conversation_prompt_presets", [])
            if isinstance(item, dict) and str(item.get("id") or "") != ignore_id
        }
        if base.casefold() not in existing:
            return base
        index = 2
        while f"{base} ({index})".casefold() in existing:
            index += 1
        return f"{base} ({index})"

    def get_conversation_prompt_presets(self) -> list[dict[str, str]]:
        with self.lock:
            items = self.data.get("conversation_prompt_presets", [])
            if not isinstance(items, list):
                return []
            return [dict(item) for item in items if isinstance(item, dict)]

    def add_conversation_prompt_preset(self, title: str, system_prompt: str, source: str = "manual") -> dict[str, str]:
        prompt = str(system_prompt or "").strip()
        if not prompt:
            raise ValueError("系统提示词不能为空")
        with self.lock:
            presets = self.data.setdefault("conversation_prompt_presets", [])
            if not isinstance(presets, list):
                presets = []
                self.data["conversation_prompt_presets"] = presets
            now = self._preset_timestamp()
            item = {
                "id": uuid.uuid4().hex,
                "title": self._unique_conversation_preset_title(title),
                "system_prompt": prompt[:20000],
                "source": str(source or "manual")[:40] or "manual",
                "created_at": now,
                "updated_at": now,
            }
            presets.append(item)
            self.save()
            return dict(item)

    def update_conversation_prompt_preset(self, preset_id: str, title: str, system_prompt: str) -> dict[str, str] | None:
        prompt = str(system_prompt or "").strip()
        if not prompt:
            raise ValueError("系统提示词不能为空")
        with self.lock:
            presets = self.data.get("conversation_prompt_presets", [])
            if not isinstance(presets, list):
                return None
            for item in presets:
                if isinstance(item, dict) and str(item.get("id") or "") == preset_id:
                    item["title"] = self._unique_conversation_preset_title(title, ignore_id=preset_id)
                    item["system_prompt"] = prompt[:20000]
                    item["updated_at"] = self._preset_timestamp()
                    self.save()
                    return dict(item)
            return None

    def delete_conversation_prompt_preset(self, preset_id: str) -> bool:
        with self.lock:
            presets = self.data.get("conversation_prompt_presets", [])
            if not isinstance(presets, list):
                return False
            filtered = [item for item in presets if not isinstance(item, dict) or str(item.get("id") or "") != preset_id]
            if len(filtered) == len(presets):
                return False
            self.data["conversation_prompt_presets"] = filtered
            self.save()
            return True

    def add_starter_prompt(self, title: str, text: str) -> list[dict[str, str]]:
        title = " ".join(str(title or "").strip().split())[:40] or "自定义指令"
        text = str(text or "").strip()
        if not text:
            raise ValueError("指令内容不能为空")
        with self.lock:
            prompts = self.data.setdefault("starter_prompts", [])
            if not isinstance(prompts, list):
                prompts = []
                self.data["starter_prompts"] = prompts
            prompts.append({"title": title, "text": text})
            self.save()
        return self.get_starter_prompts()

    def remove_starter_prompt(self, index: int) -> list[dict[str, str]]:
        with self.lock:
            prompts = self.data.setdefault("starter_prompts", [])
            if isinstance(prompts, list) and 0 <= int(index) < len(prompts):
                removed = prompts.pop(int(index))
                # 删的是内置预设 → 记入"已删除"清单：以后启动不再自动补回
                preset_id = _starter_preset_id(removed)
                if preset_id:
                    dismissed = self.data.get(STARTER_PRESET_DISMISSED_KEY)
                    dismissed_ids = {str(item) for item in dismissed} if isinstance(dismissed, list) else set()
                    dismissed_ids.add(preset_id)
                    self.data[STARTER_PRESET_DISMISSED_KEY] = sorted(dismissed_ids)
                self.save()
        return self.get_starter_prompts()

    def update_starter_prompt(self, index: int, title: str, text: str) -> list[dict[str, str]]:
        title = " ".join(str(title or "").strip().split())[:40] or "自定义指令"
        text = str(text or "").strip()
        if not text:
            raise ValueError("指令内容不能为空")
        with self.lock:
            prompts = self.data.setdefault("starter_prompts", [])
            if isinstance(prompts, list) and 0 <= int(index) < len(prompts):
                # 保留 desc/icon 等附加字段：内置预设的副标题与图标不应因一次编辑而丢失
                # （前端按 entry.desc / entry.icon 渲染卡片）。
                entry = dict(prompts[int(index)]) if isinstance(prompts[int(index)], dict) else {}
                entry.update({"title": title, "text": text})
                prompts[int(index)] = entry
                self.save()
        return self.get_starter_prompts()

    def count_missing_starter_presets(self) -> int:
        """当前列表里缺失的内置预设数量（前端据此显示「恢复默认预设」）。"""
        with self.lock:
            prompts = self.data.get("starter_prompts")
            present = {
                _starter_preset_id(item)
                for item in (prompts if isinstance(prompts, list) else [])
                if isinstance(item, dict)
            }
            return sum(1 for preset in BUILTIN_STARTER_PRESETS if preset["id"] not in present)

    def restore_starter_presets(self) -> list[dict[str, str]]:
        """把缺失的内置开始页预设补回列表头部，并清空"已删除"清单（一键恢复默认）。

        只补缺失 id 的条目：用户改过的同名条目（带 `preset_id`）保持原样、不覆盖。
        """
        with self.lock:
            prompts = self.data.get("starter_prompts")
            if not isinstance(prompts, list):
                prompts = []
            prompts = [item for item in prompts if isinstance(item, dict)]
            present = {_starter_preset_id(item) for item in prompts}
            seeded = [
                _starter_preset_entry(preset) for preset in BUILTIN_STARTER_PRESETS
                if preset["id"] not in present
            ]
            if seeded:
                self.data["starter_prompts"] = seeded + prompts
            # 恢复默认 = 清空"已删除"清单（此后缺失的内置预设又会自动补齐）
            had_dismissed = bool(self.data.get(STARTER_PRESET_DISMISSED_KEY))
            if had_dismissed:
                self.data[STARTER_PRESET_DISMISSED_KEY] = []
            if seeded or had_dismissed:
                self.save()
        return self.get_starter_prompts()

    # ---- 快捷消息（会话内面板专用列表，与开始页「自定义指令」互不干扰）----
    def get_quick_messages(self, sort: str = "") -> list[dict[str, Any]]:
        """快捷消息列表；``sort="usage"`` 按权重降序（并列取新增时间倒序），默认插入顺序。"""
        with self.lock:
            items = self.data.get("quick_messages", [])
            normalized = _quick_message_entries(items)
        if str(sort or "").strip().lower() == "usage":
            now_ms = int(time.time() * 1000)
            normalized.sort(key=lambda item: (
                -quick_message_score(item, now_ms), -int(item.get("added_at") or 0), int(item["index"]),
            ))
        return normalized

    def add_quick_message(self, text: str) -> list[dict[str, Any]]:
        text = str(text or "").strip()
        if not text:
            raise ValueError("快捷消息内容不能为空")
        with self.lock:
            items = self.data.setdefault("quick_messages", [])
            if not isinstance(items, list):
                items = []
                self.data["quick_messages"] = items
            items.append({
                "text": text,
                "count": 0,
                "added_at": int(time.time() * 1000),
                "used_at": 0,
            })
            self.save()
        return self.get_quick_messages()

    def remove_quick_message(self, index: int) -> list[dict[str, Any]]:
        with self.lock:
            items = self.data.setdefault("quick_messages", [])
            if isinstance(items, list) and 0 <= int(index) < len(items):
                removed = items[int(index)]
                # 删的是内置预设 → 记入删除清单，升级时不再自动补回。
                preset_id = _quick_preset_id(removed)
                if preset_id:
                    dismissed = self.data.get(QUICK_MESSAGE_PRESET_DISMISSED_KEY)
                    ids = {str(item) for item in dismissed} if isinstance(dismissed, list) else set()
                    ids.add(preset_id)
                    self.data[QUICK_MESSAGE_PRESET_DISMISSED_KEY] = sorted(ids)
                items.pop(int(index))
                self.save()
        return self.get_quick_messages()

    def update_quick_message(self, index: int, text: str) -> list[dict[str, Any]]:
        text = str(text or "").strip()
        if not text:
            raise ValueError("快捷消息内容不能为空")
        with self.lock:
            items = self.data.setdefault("quick_messages", [])
            if isinstance(items, list) and 0 <= int(index) < len(items):
                current = items[int(index)] if isinstance(items[int(index)], dict) else {}
                # 编辑只改正文：使用次数/新增时间/最近使用时间原样保留；
                # 内置预设的 preset_id 也要保留，否则升级会被当成"缺失"而补回原版。
                entry = {
                    "text": text,
                    "count": max(0, int(current.get("count") or 0)),
                    "added_at": max(0, int(current.get("added_at") or 0)),
                    "used_at": max(0, int(current.get("used_at") or 0)),
                }
                preset_id = _quick_preset_id(current)
                if preset_id:
                    entry["preset_id"] = preset_id
                items[int(index)] = entry
                self.save()
        return self.get_quick_messages()

    def record_quick_message_use(self, index: int) -> list[dict[str, Any]]:
        """记录一次快捷消息使用（点击插入）：累加次数并刷新最近使用时间。"""
        with self.lock:
            items = self.data.setdefault("quick_messages", [])
            if isinstance(items, list) and 0 <= int(index) < len(items):
                entry = items[int(index)]
                if isinstance(entry, dict):
                    entry["count"] = max(0, int(entry.get("count") or 0)) + 1
                    entry["used_at"] = int(time.time() * 1000)
                    self.save()
        return self.get_quick_messages()

    def _resolve_dir(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (self._paths.app_dir / path).resolve()
        return path.resolve()

    def resolve_workspace_dir(self, raw: str | None = None) -> Path:
        """解析工作区目录：相对路径以 EXE 所在目录为基准（不受启动目录影响）。"""
        raw = (raw if raw is not None else self.data.get("workspace_dir", "workspace") or "workspace").strip()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (self._paths.exe_dir / path).resolve()
        return path.resolve()

    def workspace_dir_for_group(self, workspace_group: str) -> str:
        """Return the registered directory for a workspace name.

        A conversation may only claim a non-empty workspace group when that
        name is present in the registered workspace list.  Keeping this lookup
        here makes the server, rather than the browser, the authority for the
        name-to-directory binding.
        """
        name = str(workspace_group or "").strip()
        if not name:
            raise ValueError("工作区名称不能为空")
        workspaces = self.data.get("workspaces", [])
        if not isinstance(workspaces, list):
            workspaces = []
        for workspace in workspaces:
            if not isinstance(workspace, dict):
                continue
            if str(workspace.get("name") or "").strip() != name:
                continue
            directory = str(workspace.get("dir") or "").strip()
            if not directory:
                break
            return directory
        raise ValueError(f"工作区不存在：{name}")

    def workspace_bindings(self) -> dict[str, str]:
        """Return the current registered workspace name-to-directory mapping."""
        workspaces = self.data.get("workspaces", [])
        if not isinstance(workspaces, list):
            return {}
        return {
            str(workspace.get("name") or "").strip(): str(workspace.get("dir") or "").strip()
            for workspace in workspaces
            if isinstance(workspace, dict)
            and str(workspace.get("name") or "").strip()
            and str(workspace.get("dir") or "").strip()
        }

    def resolve_data_dir(self, raw: str | None = None) -> Path:
        """Resolve persistent data storage; relative paths are relative to self._paths.app_dir."""
        value = raw if raw is not None else self.data.get("data_dir", "data")
        path = Path(str(value or "data")).expanduser()
        if not path.is_absolute():
            path = self._paths.app_dir / path
        return path.resolve()

    def resolve_managed_skills_dir(self, raw: str | None = None) -> Path:
        """持久化 Skills 目录（单一事实来源）：位于数据目录内的 ``skills`` 文件夹。

        默认落在 ``resolve_data_dir() / "skills"``，使 Skills 随数据目录离开
        C 盘 self._paths.app_dir，不再写死为 ``self._paths.app_dir/skills``，也不放在数据目录同级。
        """
        return (self.resolve_data_dir(raw) / "skills").resolve()

    def skills_dirs_resolved(self) -> list[Path]:
        """返回解析后的 Skills 扫描目录，旧 ``self._paths.app_dir/skills`` 重定向到托管目录。

        托管目录（managed）始终排在最前作为唯一持久化入口；随后是用户自定义目录。
        过滤去重，跳过解析失败或不安全的项。
        """
        managed = self.resolve_managed_skills_dir()
        legacy_managed = (self._paths.app_dir / "skills").resolve()
        result: list[Path] = [managed]
        for raw in self.data.get("skills_dirs", []):
            try:
                resolved = self._resolve_dir(str(raw))
            except (OSError, ValueError):
                continue
            if resolved == legacy_managed:
                resolved = managed
            if resolved in result:
                continue
            try:
                validate_skills_dir(resolved, app_dir=self._paths.app_dir, public_dir=self._paths.public_dir, data_dir=self._paths.data_dir)
            except ValueError:
                continue
            result.append(resolved)
        return result

    def validate_data_dir(self, resolved: Path) -> None:
        resolved = resolved.resolve()
        if resolved.parent == resolved:
            raise ValueError("不能把磁盘根目录作为数据目录")
        system_roots = [Path(os.environ.get("SystemRoot", r"C:\Windows"))]
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            value = os.environ.get(env_name)
            if value:
                system_roots.append(Path(value))
        for root in system_roots:
            root = root.resolve()
            if resolved == root or path_within(resolved, root):
                raise ValueError(f"不允许使用系统目录作为数据目录：{root}")
        if resolved == self._paths.public_dir.resolve() or resolved == self._paths.exe_dir.resolve():
            raise ValueError("不能把程序目录作为数据目录，请选择独立目录")

    def ensure_data_dir_writable(self, resolved: Path) -> None:
        self.validate_data_dir(resolved)
        resolved.mkdir(parents=True, exist_ok=True)
        probe = resolved / ".naiba_data_write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise ValueError(f"数据目录不可写：{resolved}（{exc}）")

    def validate_workspace_dir(self, resolved: Path) -> None:
        """拒绝磁盘根目录、系统目录、程序数据目录等过宽或危险路径。"""
        resolved = resolved.resolve()
        if resolved.parent == resolved:
            raise ValueError("不能把磁盘根目录作为工作区")
        system_roots = [Path(os.environ.get("SystemRoot", r"C:\Windows"))]
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            value = os.environ.get(env_name)
            if value:
                system_roots.append(Path(value))
        for root in system_roots:
            root = root.resolve()
            if resolved == root or path_within(resolved, root):
                raise ValueError(f"不允许使用系统目录作为工作区：{root}")
        forbidden_exact = {
            Path.home().resolve(),
            self._paths.app_dir,
            self._paths.data_dir.resolve(),
            self._paths.public_dir.resolve(),
        }
        if resolved in forbidden_exact:
            raise ValueError("不能把程序数据目录或用户主目录作为工作区，请使用其子目录")

    def ensure_workspace_writable(self, resolved: Path) -> None:
        """创建工作区目录并验证可读写性；不允许则抛出。"""
        self.validate_workspace_dir(resolved)
        resolved.mkdir(parents=True, exist_ok=True)
        probe = resolved / ".naiba_write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.read_text(encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise ValueError(f"工作区目录不可读写：{resolved}（{exc}）")

    def public_providers(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                {
                    **provider,
                    "api_key": "",
                    "has_api_key": bool(provider.get("api_key")),
                    "context_window": _infer_context_window(provider),
                    "context_window_source": _context_window_source(provider),
                }
                for provider in self.data.get("providers", [])
            ]

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "host",
            "provider_id",
            "temperature",
            "max_tokens",
            "context_size",
            "agent_system_prompt",
            "permission_mode",
            "agent_tools",
            "command_timeout",
            "context_warning_percent",
            "local_first_byte_timeout_seconds",
            "agent_step_limit",
            "context_reset_seed_template",
            "access_token",
            "workspace_dir",
            "data_dir",
            "imaging",
            "vision",
            "search",
            "proxy",
            "workspaces",
            "appearance",
            "chat_background",
        }
        with self.lock:
            for key in allowed:
                if key in values:
                    if key == "host":
                        host = str(values[key] or "").strip()
                        if host not in {"127.0.0.1", "0.0.0.0"}:
                            raise ValueError("host 只能是 127.0.0.1 或 0.0.0.0")
                        self.data[key] = host
                    elif key == "access_token":
                        token = str(values[key]).strip()
                        if not token:
                            raise ValueError("访问口令不能为空")
                        if len(token) < 4:
                            raise ValueError("访问口令至少 4 位")
                        self.data[key] = token
                    elif key == "agent_system_prompt":
                        self.data[key] = str(values[key])[:12000]
                    elif key == "permission_mode":
                        mode = str(values[key] or "confirm").strip().lower()
                        if mode not in {"confirm", "auto", "full"}:
                            raise ValueError("权限模式必须是 confirm、auto 或 full")
                        self.data[key] = mode
                    elif key == "agent_tools":
                        valid_tools = {
                            "read_file", "write_file", "list_directory", "search_files",
                            "pwsh", "run_skill_script", "http_request",
                        }
                        requested = values[key] if isinstance(values[key], list) else []
                        self.data[key] = [tool for tool in requested if tool in valid_tools]
                    elif key == "workspace_dir":
                        raw = str(values[key] or "").strip()
                        if not raw:
                            # 恢复默认：EXE 所在目录下的 workspace。
                            raw = "workspace"
                        resolved = self.resolve_workspace_dir(raw)
                        self.ensure_workspace_writable(resolved)
                        self.data[key] = raw
                    elif key == "data_dir":
                        raw = str(values[key] or "").strip() or "data"
                        resolved = self.resolve_data_dir(raw)
                        self.ensure_data_dir_writable(resolved)
                        self.data[key] = raw
                    elif key == "context_size":
                        self.data[key] = self._positive_context_size(values[key], "context_size")
                    elif key == "appearance":
                        incoming = values[key]
                        if not isinstance(incoming, dict):
                            raise ValueError("appearance 必须是对象")
                        unknown = set(incoming) - {
                            "theme", "skin",
                            "chat_font_size", "chat_font_family", "chat_font_family_custom",
                        }
                        if unknown:
                            names = ", ".join(sorted(map(str, unknown)))
                            raise ValueError(f"appearance 包含不支持的字段：{names}")
                        merged = dict(self.data.get("appearance", {}))
                        if "theme" in incoming:
                            theme = str(incoming["theme"] or "").strip().lower()
                            if theme not in APPEARANCE_THEMES:
                                raise ValueError("主题必须是 system、light 或 dark")
                            merged["theme"] = theme
                        if "skin" in incoming:
                            skin = str(incoming["skin"] or "").strip().lower()
                            if skin not in APPEARANCE_SKINS:
                                raise ValueError("皮肤必须是 violet、ocean、rose 或 forest")
                            merged["skin"] = skin
                        if "chat_font_size" in incoming:
                            merged["chat_font_size"] = self._validated_chat_font_size(
                                incoming["chat_font_size"]
                            )
                        if "chat_font_family" in incoming:
                            family = str(incoming["chat_font_family"] or "").strip().lower()
                            if family not in APPEARANCE_CHAT_FONT_FAMILIES:
                                # 文案从集合动态生成：加一款预置字体不必再改一遍文案
                                # （写死清单的老写法每加一个键都要同步四处，必漏一处）。
                                raise ValueError(
                                    "会话字体必须是以下之一："
                                    + "、".join(sorted(APPEARANCE_CHAT_FONT_FAMILIES))
                                )
                            merged["chat_font_family"] = family
                        if "chat_font_family_custom" in incoming:
                            if not isinstance(incoming["chat_font_family_custom"], str):
                                raise ValueError("自定义字体必须是不超过 100 字符的文本")
                            merged["chat_font_family_custom"] = clean_chat_font_family_custom(
                                incoming["chat_font_family_custom"]
                            )
                        # 整体过一遍归一化：键集固定，前端可放心替换 state.appearance。
                        self.data[key] = normalize_appearance(merged)
                    elif key == "chat_background":
                        incoming = values[key]
                        if not isinstance(incoming, dict):
                            raise ValueError("chat_background 必须是对象")
                        unknown = set(incoming) - {
                            "image", "opacity", "crop", "position_x", "position_y", "zoom",
                        }
                        if unknown:
                            names = ", ".join(sorted(map(str, unknown)))
                            raise ValueError(f"chat_background 包含不支持的字段：{names}")
                        # 先在"当前值 + 本次增量"上归一化：透明度是滑杆量、crop 是拖拽来的
                        # 矩形，越界一律 clamp（normalize_chat_background 是与默认值、加载期
                        # 共用的一套规则）。crop: null 是合法值 = 恢复"自动取最大区域"。
                        merged = normalize_chat_background({
                            **dict(self.data.get("chat_background", {})),
                            **incoming,
                        })
                        if incoming.get("crop") is not None and "crop" in incoming:
                            # 结构类错误显式报错（前端永远给齐四个数值，缺一个就说明契约对不上，
                            # 静默吞掉会变成"拖了没反应"）；越界仍由归一化 clamp。
                            merged["crop"] = self._validated_chat_background_crop(incoming["crop"])
                        # 图片路径要在归一化之后再校验替换：它是**解析后的绝对路径**，
                        # 不能让它被 incoming 里的原始字符串覆盖回去。文件暂缺不清空、
                        # 只保留路径（见 _validated_chat_background_image）。
                        if "image" in incoming:
                            merged["image"] = self._validated_chat_background_image(incoming["image"])
                        self.data[key] = merged
                    elif key in ("vision", "search", "imaging"):
                        incoming = values[key]
                        if not isinstance(incoming, dict):
                            raise ValueError(f"{key} 必须是对象")
                        # 合并到现有子配置，避免丢失其他子字段。
                        merged = dict(self.data.get(key, {}))
                        for sub_key, sub_value in incoming.items():
                            merged[str(sub_key)] = sub_value
                        if key == "imaging":
                            merged["image_upload_original"] = bool(merged.get("image_upload_original", False))
                            for field in ("image_max_pixels", "thumbnail_max_pixels"):
                                try:
                                    merged[field] = max(1, int(merged.get(field) or 0))
                                except (TypeError, ValueError):
                                    raise ValueError(f"{field} 必须是正整数") from None
                            # 缓存自动清理阈值（MB）：0=关闭；1-4096 区间上限防误填。
                            try:
                                auto_mb = int(merged.get("auto_clean_limit_mb", 256) or 0)
                            except (TypeError, ValueError):
                                raise ValueError("auto_clean_limit_mb 必须是整数") from None
                            if auto_mb < 0 or auto_mb > 4096:
                                raise ValueError("缓存自动清理阈值必须在 0-4096 MB 之间")
                            merged["auto_clean_limit_mb"] = auto_mb
                        self.data[key] = merged
                    elif key == "proxy":
                        incoming = values[key]
                        if not isinstance(incoming, dict):
                            raise ValueError("proxy 必须是对象")
                        enabled = bool(incoming.get("enabled", False))
                        url = str(incoming.get("url") or "").strip()
                        if url and "://" not in url:
                            url = f"http://{url}"
                        if url:
                            parts = urllib.parse.urlsplit(url)
                            if parts.scheme not in {"http", "https"} or not parts.hostname:
                                raise ValueError(
                                    "代理地址格式不正确（仅支持 http/https，示例：http://127.0.0.1:7890）"
                                )
                            if not parts.port:
                                raise ValueError(
                                    f"代理地址缺少端口号：{url}（示例：http://127.0.0.1:7890）"
                                )
                        self.data[key] = {
                            "enabled": enabled,
                            "url": url,
                            "use_system_fallback": bool(incoming.get("use_system_fallback", True)),
                        }
                    elif key == "context_warning_percent":
                        # 0 = 关闭提醒；1-100 = 达到该百分比时前端弹窗提醒一次。
                        try:
                            percent = int(values[key] if values[key] not in (None, "") else 0)
                        except (TypeError, ValueError):
                            raise ValueError("上下文提醒阈值必须是 0-100 的整数") from None
                        if percent < 0 or percent > 100:
                            raise ValueError("上下文提醒阈值必须在 0-100 之间")
                        self.data[key] = percent
                    elif key == "local_first_byte_timeout_seconds":
                        # 0 = 关闭；否则 5-1800 秒。留空按默认值处理（避免误清空导致闸门失效）。
                        raw = values[key]
                        if raw in (None, ""):
                            self.data[key] = LOCAL_FIRST_BYTE_TIMEOUT_DEFAULT
                        else:
                            try:
                                seconds = int(raw)
                            except (TypeError, ValueError):
                                raise ValueError("本地首字节超时必须是整数秒") from None
                            if seconds != 0 and not 5 <= seconds <= 1800:
                                raise ValueError("本地首字节超时必须在 5-1800 秒之间（0 = 关闭）")
                            self.data[key] = seconds
                    elif key == "agent_step_limit":
                        # 0 = 不限制；否则 1-1000 步。留空按默认值处理。
                        raw = values[key]
                        if raw in (None, ""):
                            self.data[key] = AGENT_MAX_STEPS_DEFAULT
                        else:
                            try:
                                steps = int(raw)
                            except (TypeError, ValueError):
                                raise ValueError("Agent 最大步数必须是整数") from None
                            if steps != 0 and not 1 <= steps <= 1000:
                                raise ValueError("Agent 最大步数必须在 1-1000 之间（0 = 不限制）")
                            self.data[key] = steps
                    elif key == "context_reset_seed_template":
                        # 种子消息模板：留空 = 用内置默认（前端回退），最长 2000 字符。
                        self.data[key] = str(values[key] or "")[:2000]
                    else:
                        self.data[key] = values[key]
            self.save()
            result = self.public()
            # Keep the legacy response field for older clients that still
            # validate context_size. It is excluded from bootstrap settings
            # and is never used to build model requests.
            if "context_size" in values:
                result["context_size"] = self.data["context_size"]
            return result

    def upsert_mcp_server(self, values: dict[str, Any]) -> dict[str, Any]:
        server_id = str(values.get("id") or "").strip()
        command = str(values.get("command") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", server_id):
            raise ValueError("MCP 服务 ID 只能包含字母、数字、下划线或连字符")
        if not command:
            raise ValueError("MCP command 不能为空")
        args = values.get("args") or []
        env = values.get("env") or {}
        if not isinstance(args, list) or not isinstance(env, dict):
            raise ValueError("MCP args 必须是数组，env 必须是对象")
        payload = {
            "id": server_id,
            "command": command,
            "args": [str(item) for item in args],
            "env": {str(key): str(value) for key, value in env.items()},
            "enabled": bool(values.get("enabled", True)),
        }
        with self.lock:
            servers = self.data.setdefault("mcp_servers", [])
            index = next((i for i, item in enumerate(servers) if item.get("id") == server_id), None)
            if index is None:
                servers.append(payload)
            else:
                servers[index] = payload
            self.save()
        return payload

    def delete_mcp_server(self, server_id: str) -> bool:
        """Remove a registered MCP server from persistent config."""
        server_id = str(server_id or "").strip()
        with self.lock:
            servers = self.data.get("mcp_servers", [])
            before = len(servers)
            self.data["mcp_servers"] = [item for item in servers if item.get("id") != server_id]
            if len(self.data["mcp_servers"]) != before:
                self.save()
                return True
            return False

    def upsert_provider(self, values: dict[str, Any]) -> dict[str, Any]:
        """兼容旧接口，同时尊重显式 online/local 类型。"""
        request_format = str(values.get("request_format") or "openai_chat").strip().lower()
        payload = dict(values)
        kind = str(values.get("kind") or "").strip().lower()
        payload["kind"] = kind if kind in VALID_MODEL_KINDS else _infer_kind_for_request_format(request_format)
        if payload["kind"] == "local":
            payload["local_backend"] = request_format
        return self.upsert_model_profile(payload)

    def delete_provider(self, provider_id: str) -> bool:
        """兼容别名：按 id 删除（不区分 online/local）。"""
        with self.lock:
            providers = self.data.setdefault("providers", [])
            before = len(providers)
            self.data["providers"] = [item for item in providers if item.get("id") != provider_id]
            removed = len(self.data["providers"]) < before
            default = self.data.get("default_model_key") or ""
            if default.endswith(f":{provider_id}"):
                remaining = self.data.get("providers", [])
                self.data["default_model_key"] = (
                    f"{remaining[0].get('kind', 'online')}:{remaining[0].get('id')}" if remaining else ""
                )
            if self.data.get("provider_id") == provider_id:
                self.data["provider_id"] = ""
            vision = self.data.get("vision")
            if isinstance(vision, dict) and str(vision.get("provider_model_key") or "").endswith(f":{provider_id}"):
                vision["provider_model_key"] = ""
            self.save()
            return removed

    def provider_secret(self, provider_id: str) -> str | None:
        with self.lock:
            provider = next(
                (item for item in self.data.get("providers", []) if item.get("id") == provider_id),
                None,
            )
            return str(provider.get("api_key") or "") if provider else None

    def remember_local_context_window(self, model_key: str, window: int) -> bool:
        """把本地后端**探测到**的上下文窗口写回 provider 配置。

        为什么必须回写：探测值原先只挂在 `run.chat._profile_with_model_override` 造的
        profile 副本上，只有会话路径看得到；`public_providers()`（设置页显示）以及其它
        走 `_infer_context_window()` 的读取点仍然拿 0 → 又退回在线 256k 兜底，等于对
        本地模型不设上限（正是客户机「永久卡死」的病根）。

        写进独立字段 `context_window_probed`，**绝不覆盖**用户显式填写的
        `context_window`；取值优先级见 `_infer_context_window`。返回是否真的发生了写入
        （值没变就不落盘，避免每轮对话都重写一次 config.json）。
        """
        try:
            parsed = int(window)
        except (TypeError, ValueError):
            return False
        if parsed <= 0:
            return False
        kind, _, model_id = str(model_key or "").partition(":")
        if not model_id:
            return False
        with self.lock:
            provider = next(
                (
                    item
                    for item in self.data.get("providers", [])
                    if item.get("id") == model_id and (not kind or item.get("kind") == kind)
                ),
                None,
            )
            if provider is None:
                return False
            # 显式配置优先：用户填过的值永远不参与探测覆盖。
            if provider.get("context_window") or provider.get("context_size"):
                return False
            try:
                current = int(provider.get("context_window_probed") or 0)
            except (TypeError, ValueError):
                current = 0
            if current == parsed:
                return False
            provider["context_window_probed"] = parsed
            self.save()
            return True

    # ---- 在线 / 本地模型配置统一层 ----

    def _migrate_model_profiles(self) -> None:
        """启动时把旧 providers 分层为 online/local，并生成 default_model_key。

        不把旧的 local_model / model_mode 伪造成本地 API 配置。
        """
        providers = self.data.setdefault("providers", [])
        for provider in providers:
            if provider.get("kind") not in VALID_MODEL_KINDS:
                request_format = str(provider.get("request_format") or "openai_chat").strip().lower()
                kind = _infer_kind_for_request_format(request_format)
                provider["kind"] = kind
                if kind == "local":
                    provider["local_backend"] = request_format
                else:
                    provider.pop("local_backend", None)
            # 旧配置补全思维强度，默认 auto（不发送协议字段）。
            effort = str(provider.get("reasoning_effort") or "auto").strip().lower()
            if effort not in {"auto", "off", "low", "medium", "high"}:
                effort = "auto"
            provider["reasoning_effort"] = effort
            if provider.get("context_window") in (None, "") and provider.get("context_size") not in (None, ""):
                try:
                    provider["context_window"] = self._positive_context_size(
                        provider.get("context_size"), "context_window"
                    )
                except ValueError:
                    pass
            provider.pop("context_size", None)
        # 计算 default_model_key：旧 provider_id 指向的条目决定前缀。
        default_key = str(self.data.get("default_model_key") or "").strip()
        if not default_key:
            provider_id = str(self.data.get("provider_id") or "").strip()
            if provider_id:
                target = next(
                    (item for item in providers if item.get("id") == provider_id), None
                )
                if target:
                    default_key = f"{target.get('kind', 'online')}:{provider_id}"
        # 规范化 default_model_key，确保指向现存条目。
        if default_key:
            kind, _, model_id = default_key.partition(":")
            if kind not in VALID_MODEL_KINDS or not any(
                item.get("id") == model_id and item.get("kind") == kind for item in providers
            ):
                default_key = ""
        if not default_key and providers:
            first = providers[0]
            default_key = f"{first.get('kind', 'online')}:{first.get('id')}"
        self.data["default_model_key"] = default_key

    def default_model_key(self) -> str:
        with self.lock:
            return str(self.data.get("default_model_key") or "")

    def set_default_model_key(self, model_key: str) -> str:
        with self.lock:
            key = self._normalize_model_key(model_key)
            kind, _, model_id = key.partition(":")
            provider = next(
                (
                    item
                    for item in self.data.get("providers", [])
                    if item.get("id") == model_id and item.get("kind") == kind
                ),
                None,
            )
            if not provider:
                raise ValueError("模型配置不存在")
            self.data["default_model_key"] = key
            self.data["provider_id"] = model_id  # 兼容旧字段
            self.save()
            return key

    @staticmethod
    def _normalize_model_key(model_key: str) -> str:
        model_key = str(model_key or "").strip()
        if not model_key:
            return ""
        if ":" not in model_key:
            return f"online:{model_key}"
        return model_key

    def model_profiles(self, kind: str | None = None) -> list[dict[str, Any]]:
        """返回所有模型配置（脱敏），并附带 model_key 与是否默认。"""
        with self.lock:
            default = self.data.get("default_model_key") or ""
            result = []
            for provider in self.data.get("providers", []):
                entry_kind = provider.get("kind", "online")
                key = f"{entry_kind}:{provider.get('id')}"
                entry = dict(provider)
                entry["model_key"] = key
                entry["is_default"] = key == default
                entry["api_key"] = ""
                entry["has_api_key"] = bool(provider.get("api_key"))
                explicit_images = provider.get("supports_images")
                entry["supports_images_explicit"] = (
                    explicit_images if isinstance(explicit_images, bool) else None
                )
                entry["supports_images"] = _infer_supports_images(provider)
                entry["context_window"] = _infer_context_window(provider)
                entry["context_window_source"] = _context_window_source(provider)
                if provider.get("context_window"):
                    entry["context_size"] = provider.get("context_window")
                result.append(entry)
            if kind:
                result = [item for item in result if item.get("kind") == kind]
            return result

    def upsert_model_profile(self, values: dict[str, Any]) -> dict[str, Any]:
        """统一保存在线 API 或本地模型配置。"""
        model_id = str(values.get("id") or uuid.uuid4().hex[:12]).strip()
        kind = str(values.get("kind") or "online").strip().lower()
        if kind not in VALID_MODEL_KINDS:
            raise ValueError("模型类型必须是 online 或 local")
        if kind == "local":
            local_backend = str(
                values.get("local_backend") or values.get("request_format") or ""
            ).strip().lower()
            if local_backend not in VALID_LOCAL_BACKENDS:
                raise ValueError("本地后端必须是 ollama、LM Studio、llama.cpp 或 Unsloth")
            request_format = local_backend
        else:
            request_format = str(values.get("request_format") or "openai_chat").strip().lower()
            if request_format not in ONLINE_REQUEST_FORMATS:
                raise ValueError("不支持的在线请求格式")
            local_backend = ""
        with self.lock:
            providers = self.data.setdefault("providers", [])
            # 以 id 为主键：更新时就地切换 kind，避免同一 id 跨类别产生重复条目。
            existing = next(
                (item for item in providers if item.get("id") == model_id),
                None,
            )
            payload = {
                "id": model_id,
                "kind": kind,
                "name": str(values.get("name") or ("本地模型" if kind == "local" else "在线模型")).strip(),
                "base_url": str(values.get("base_url") or "").strip().rstrip("/"),
                "model": str(values.get("model") or "").strip(),
                "api_key": str(values.get("api_key") or "").strip(),
                "request_format": request_format,
            }
            raw_effort = str(values.get("reasoning_effort") or "auto").strip().lower()
            if raw_effort not in {"auto", "off", "low", "medium", "high"}:
                raise ValueError("思维强度必须是 auto / off / low / medium / high 之一")
            payload["reasoning_effort"] = raw_effort
            optional_fields = {
                "context_window": self._positive_context_size,
                "max_output_tokens": self._positive_context_size,
            }
            for field, parser in optional_fields.items():
                raw_value = values.get(field)
                if field == "context_window" and raw_value in (None, ""):
                    raw_value = values.get("context_size")
                if raw_value not in (None, ""):
                    payload[field] = parser(
                        raw_value,
                        "context_size" if field == "context_window" and "context_size" in values else field,
                    )
            raw_temperature = values.get("temperature")
            if raw_temperature not in (None, ""):
                if isinstance(raw_temperature, bool):
                    raise ValueError("temperature 必须是 0 到 2 之间的数字")
                try:
                    temperature = float(raw_temperature)
                except (TypeError, ValueError):
                    raise ValueError("temperature 必须是 0 到 2 之间的数字") from None
                if temperature < 0 or temperature > 2:
                    raise ValueError("temperature 必须是 0 到 2 之间的数字")
                payload["temperature"] = temperature
            clear_supports_images = False
            if "supports_images" in values:
                raw_supports_images = values.get("supports_images")
                if raw_supports_images is None:
                    clear_supports_images = True
                elif isinstance(raw_supports_images, bool):
                    payload["supports_images"] = raw_supports_images
                else:
                    raise ValueError("supports_images 必须是布尔值或 null")
            if kind == "local":
                payload["local_backend"] = local_backend
            else:
                payload.pop("local_backend", None)
            if not payload["base_url"] or not payload["model"]:
                raise ValueError("API/服务地址和模型名称不能为空")
            if existing:
                # 空 API Key 表示保留已有 Key（不覆盖、不清除）。
                if not payload["api_key"]:
                    payload["api_key"] = existing.get("api_key", "")
                for field in ("context_window", "max_output_tokens", "temperature"):
                    if field not in payload:
                        existing.pop(field, None)
                if clear_supports_images:
                    existing.pop("supports_images", None)
                existing.update(payload)
                stored = existing
            else:
                providers.append(payload)
                stored = payload
            if not self.data.get("default_model_key"):
                self.data["default_model_key"] = f"{kind}:{model_id}"
            self.save()
        explicit_images = stored.get("supports_images")
        result = {
            **stored,
            "model_key": f"{kind}:{model_id}",
            "api_key": "",
            "has_api_key": bool(stored["api_key"]),
            "is_default": (self.data.get("default_model_key") == f"{kind}:{model_id}"),
            "supports_images_explicit": (
                explicit_images if isinstance(explicit_images, bool) else None
            ),
            "supports_images": _infer_supports_images(stored),
            "context_window": _infer_context_window(stored),
            "context_window_source": _context_window_source(stored),
        }
        if stored.get("context_window"):
            result["context_size"] = stored["context_window"]
        return result

    def delete_model_profile(self, model_key: str) -> bool:
        with self.lock:
            key = self._normalize_model_key(model_key)
            kind, _, model_id = key.partition(":")
            providers = self.data.setdefault("providers", [])
            before = len(providers)
            self.data["providers"] = [
                item
                for item in providers
                if not (item.get("id") == model_id and item.get("kind") == kind)
            ]
            removed = len(self.data["providers"]) < before
            if self.data.get("default_model_key") == key:
                remaining = self.data.get("providers", [])
                self.data["default_model_key"] = (
                    f"{remaining[0].get('kind', 'online')}:{remaining[0].get('id')}" if remaining else ""
                )
            if self.data.get("provider_id") == model_id:
                self.data["provider_id"] = ""
            vision = self.data.get("vision")
            if isinstance(vision, dict) and vision.get("provider_model_key") == key:
                vision["provider_model_key"] = ""
            self.save()
            return removed

    def profile(self, selection: str = "") -> dict[str, Any]:
        """按 model_key 解析完整模型配置（含 api_key）。

        selection 可为 online:<id> / local:<id>；缺省时回退 default_model_key。
        """
        with self.lock:
            key = self._normalize_model_key(selection) or self.data.get("default_model_key") or ""
            if not key:
                raise ValueError("未选择模型配置")
            kind, _, model_id = key.partition(":")
            if kind not in VALID_MODEL_KINDS:
                raise ValueError(f"不支持的模型类型：{kind}")
            provider = next(
                (
                    item
                    for item in self.data.get("providers", [])
                    if item.get("id") == model_id and item.get("kind") == kind
                ),
                None,
            )
            if not provider:
                raise ValueError(f"找不到模型配置：{key}")
            result = {
                "kind": provider.get("kind", kind),
                **provider,
                "supports_images_explicit": (
                    provider.get("supports_images")
                    if isinstance(provider.get("supports_images"), bool)
                    else None
                ),
                "supports_images": _infer_supports_images(provider),
                "context_window": _infer_context_window(provider),
                "context_window_source": _context_window_source(provider),
            }
            if provider.get("context_window"):
                result["context_size"] = provider.get("context_window")
            return result

    def runtime_guard_options(self) -> dict[str, Any]:
        """「别永久卡住」的两道保险，注入每次模型调用的 options。

        - ``first_byte_timeout_seconds``：本地后端首字节超时（0 = 关闭本层）。
        - ``max_steps``：单轮 Agent 的模型调用次数上限（0 = 不限制）。

        放在 generation_options() 里是因为 chat / 子代理 / 计划执行三处都从它取
        options（`run.manager.generation_options` 是它的薄封装），一处注入三处生效；
        它同时随 run 快照持久化，重放时口径一致。
        """
        options: dict[str, Any] = {}
        try:
            timeout = int(self.data.get("local_first_byte_timeout_seconds", LOCAL_FIRST_BYTE_TIMEOUT_DEFAULT))
        except (TypeError, ValueError):
            timeout = LOCAL_FIRST_BYTE_TIMEOUT_DEFAULT
        options["first_byte_timeout_seconds"] = max(0, timeout)
        try:
            steps = int(self.data.get("agent_step_limit", AGENT_MAX_STEPS_DEFAULT))
        except (TypeError, ValueError):
            steps = AGENT_MAX_STEPS_DEFAULT
        options["max_steps"] = max(0, steps)
        return options

    def generation_options(self, selection: str = "") -> dict[str, Any]:
        with self.lock:
            key = self._normalize_model_key(selection) or str(self.data.get("default_model_key") or "")
            if not key:
                return {
                    "context_size": self._positive_context_size(
                        self.data.get("context_size", 8192), "context_size"
                    ),
                    **self.runtime_guard_options(),
                }
            try:
                profile = self.profile(key)
            except ValueError:
                return {}
            options: dict[str, Any] = {}
            if profile.get("temperature") not in (None, ""):
                options["temperature"] = float(profile["temperature"])
            if profile.get("max_output_tokens") not in (None, ""):
                options["max_tokens"] = int(profile["max_output_tokens"])
            options.update(self.runtime_guard_options())
            return options

    def _validated_chat_background_image(self, raw: Any) -> str:
        """校验聊天背景图：必须是 data_dir/uploads（或 backgrounds）内的图片文件。

        三道闸：
        1. 绝对路径 + 落在两个受管目录内——设置里的路径会被前端直接拼进
           `/api/file?path=…`，放宽等于开一条任意文件读取通道（也保证
           `/api/uploads/delete` 能回收它）；
        2. **按内容**（Pillow 读出的 format，不信扩展名）命中白名单——TIFF/HEIC
           之类浏览器解不出来的格式放过去，就是"保存成功、背景空白"的静默失败；
        3. **文件不在磁盘上时不清空设置**：缓存清理、数据目录迁移、换机器都会让旧路径
           失效，旧实现当场抛错（前端探针再兜底清空），用户什么都没做背景就"自己没了"。
           现在先按文件名在当前受管目录里兜底找回；确实找不到就**原样保留路径**，
           由前端标记「文件暂不可用」并引导重新选择——丢文件可以，丢设置不行。
        返回解析后的绝对路径字符串；`""` 表示清除背景。
        """
        value = str(raw or "").strip()
        if not value:
            return ""
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise ValueError("背景图路径必须是绝对路径")
        resolved = candidate.resolve()
        # 只放行两个受管目录：uploads（用户上传）与 backgrounds（内置预设图，见
        # storage/backgrounds.py）。config 层不能 import storage，所以这里按同一口径直接写
        # 目录名——改口径时两处一起改（与 is_uploads_path 的既有约定一致）。
        data_root = self.resolve_data_dir()
        allowed_roots = (
            (data_root / "uploads").resolve(),
            (data_root / "backgrounds").resolve(),
        )
        if not any(path_within(resolved, root) for root in allowed_roots):
            # 旧数据目录 / 旧机器留下的绝对路径：按文件名在当前受管目录里找回同命中文件
            # （**越界边界不放宽**：只有真找回同名文件才会迁移，否则照旧拒绝）。
            recovered = self._same_name_background_image(resolved.name)
            if recovered is None:
                raise ValueError("背景图必须来自本机（data/uploads 或 data/backgrounds 目录内）")
            resolved = recovered
        elif not resolved.is_file():
            resolved = self._same_name_background_image(resolved.name) or resolved
        if not resolved.is_file():
            # 文件确实找不到（缓存清理 / 迁移窗口）：保留原路径。前端探针失败只标记
            # 「暂不可用」，用户重新选一张即可——清空设置才是真正的数据丢失。
            return str(resolved)
        # PIL 是硬依赖（上传管线也在用），缺失时让它按 ImportError 暴露，不吞成"用户选错图"。
        from PIL import Image
        from PIL.Image import DecompressionBombError

        try:
            with Image.open(resolved) as probe:
                image_format = str(probe.format or "").upper()
        except DecompressionBombError:
            raise ValueError("背景图尺寸过大，请先压缩后再选") from None
        except Exception:  # noqa: BLE001 - 坏图/未知格式一律按"无法识别"处理
            raise ValueError("无法识别的图片文件，请重新选择") from None
        if image_format not in CHAT_BACKGROUND_IMAGE_FORMATS:
            allowed = " / ".join(CHAT_BACKGROUND_IMAGE_FORMATS)
            current = image_format or resolved.suffix.lower().lstrip(".") or "未知"
            raise ValueError(f"背景图仅支持 {allowed} 格式（当前识别为 {current}）")
        return str(resolved)

    def _same_name_background_image(self, name: str) -> Path | None:
        """在当前受管目录里按文件名找回同命中背景图（数据目录迁移的兜底）。

        只在 ``uploads``（含 `YYYY-MM-DD` 分日目录）与 ``backgrounds`` 内部找——越界路径
        不会因为"有个同名文件"就被放行成任意路径；Windows 文件名比较不区分大小写
        （`rglob` 语义）。只负责"找"，格式校验仍由调用方照常执行。
        """
        target = str(name or "").strip()
        if not target or target in {".", ".."}:
            return None
        data_root = self.resolve_data_dir()
        for root in ((data_root / "uploads").resolve(), (data_root / "backgrounds").resolve()):
            if not root.is_dir():
                continue
            direct = root / target
            if direct.is_file():
                return direct
            try:
                for candidate in sorted(root.rglob(target)):
                    if candidate.is_file():
                        return candidate
            except OSError:
                continue
        return None

    @staticmethod
    def _validated_chat_background_crop(raw: Any) -> dict[str, Any]:
        """裁剪区域（写入路径）：结构必须齐全，尺寸必须为正；越界 clamp 回图片内。

        为什么结构错误报错而不是静默回落"自动"：crop 是编辑器拖出来的矩形，四个数永远
        一起发；只发一半只可能是前后端版本对不上——静默改回"自动"会让用户的调整凭空
        消失，且看不出原因（正是本项目最忌的静默失败）。
        """
        if not isinstance(raw, dict):
            raise ValueError("crop 必须是对象")
        unknown = set(raw) - {"x", "y", "w", "h"}
        if unknown:
            names = ", ".join(sorted(map(str, unknown)))
            raise ValueError(f"crop 包含不支持的字段：{names}")
        numbers: dict[str, float] = {}
        for field in ("x", "y", "w", "h"):
            if field not in raw:
                raise ValueError("crop 需要 x、y、w、h 四个数值")
            try:
                number = float(raw[field])
            except (TypeError, ValueError):
                raise ValueError(f"crop.{field} 必须是数值") from None
            if number != number or abs(number) == float("inf"):
                raise ValueError(f"crop.{field} 必须是有限数值")
            numbers[field] = number
        if numbers["w"] <= 0 or numbers["h"] <= 0:
            raise ValueError("crop 的宽和高必须大于 0")
        normalized = normalize_chat_background_crop(numbers)
        if normalized is None:
            raise ValueError("crop 无效")
        return normalized

    @staticmethod
    def _validated_chat_font_size(raw: Any) -> int:
        """会话字号（写入路径）：必须是 13-18 的整数。

        越界**报错**而不是夹回：滑块送来的值永远在区间内，越界只可能是客户端算错或
        旧版前端发的——静默夹回会让用户看到"设了没生效"却查不出原因。
        （配置文件被手改的情况由加载路径的 clamp_chat_font_size 兜底。）
        """
        if isinstance(raw, bool):
            raise ValueError("会话字号必须是 13-18 的整数")
        try:
            size = int(raw)
        except (TypeError, ValueError):
            raise ValueError("会话字号必须是 13-18 的整数") from None
        if not (CHAT_FONT_SIZE_MIN <= size <= CHAT_FONT_SIZE_MAX):
            raise ValueError(f"会话字号必须是 {CHAT_FONT_SIZE_MIN}-{CHAT_FONT_SIZE_MAX} 的整数")
        return size

    @staticmethod
    def _positive_context_size(value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field} 必须是正整数")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field} 必须是正整数") from None
        if parsed <= 0 or (isinstance(value, float) and not value.is_integer()):
            raise ValueError(f"{field} 必须是正整数")
        return parsed

    # ---- Agent 管理 ----

    def public_agents(self) -> list[dict[str, Any]]:
        with self.lock:
            custom = [dict(agent) for agent in self.data.get("agents", [])]
            # 内置 Agent 默认全开启，且允许用户自定义；若用户已编辑过某个内置 Agent，
            # 其覆盖定义保存在 self.data['agents']（built_in=True），此时以覆盖版为准，
            # 不再追加默认内置定义，避免同一个 Agent 出现两次。
            overridden_ids = {
                str(agent.get("id") or "") for agent in custom if agent.get("built_in")
            }
            built_in = [
                dict(agent) for agent in built_in_agents()
                if agent.get("id") not in overridden_ids
            ]
            return custom + built_in

    def default_agent_id(self) -> str:
        with self.lock:
            agents = [*self.data.get("agents", []), *built_in_agents()]
            configured = str(self.data.get("default_agent_id") or "").strip()
            if configured and any(agent.get("id") == configured for agent in agents):
                return configured
            if agents:
                return str(agents[0].get("id") or "general")
            return "general"

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        agent_id = str(agent_id or "").strip()
        with self.lock:
            agent = next(
                (item for item in self.data.get("agents", []) if item.get("id") == agent_id),
                None,
            )
            if agent is None:
                agent = next(
                    (item for item in built_in_agents() if item.get("id") == agent_id),
                    None,
                )
            return dict(agent) if agent else None

    def _allocate_agent_id(self) -> str:
        """为新建 Agent 分配持久化唯一 id（用户不再手填）。

        形态 `agent_<12 位 hex>`：只含 `[A-Za-z0-9_-]`，天然满足 upsert 的校验与 URL 安全；
        与既有 Agent、内置清单逐一比对，避免碰撞。
        """
        taken = {str(item.get("id") or "") for item in self.data.get("agents", [])}
        taken |= built_in_agent_ids()
        for _ in range(64):
            candidate = f"agent_{uuid.uuid4().hex[:12]}"
            if candidate not in taken:
                return candidate
        raise ValueError("无法分配 Agent ID，请重试")

    def upsert_agent(self, values: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(values.get("id") or "").strip()
        if agent_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", agent_id):
                raise ValueError("Agent ID 只能包含字母、数字、下划线或连字符")
        else:
            # 新建（前端已不再让用户填 ID）：后台分配一个持久化唯一 id。
            agent_id = self._allocate_agent_id()
        # 内置 Agent 默认“全开启”，且允许用户自定义（如裁剪 tool_scope）。
        # 编辑仍保留 built_in 标记，使其不可删除；未内建的新 ID 视为自定义 Agent。
        is_built_in = agent_id in built_in_agent_ids()
        name = str(values.get("name") or "").strip()
        if not name:
            raise ValueError("Agent 名称不能为空")
        system_prompt = str(values.get("system_prompt") or "")[:12000]
        raw_skills = values.get("skill_ids") or []
        if not isinstance(raw_skills, list):
            raise ValueError("skill_ids 必须是数组")
        skill_ids = list(dict.fromkeys(str(item) for item in raw_skills if str(item).strip()))
        raw_scope = values.get("tool_scope")
        if raw_scope is not None and not isinstance(raw_scope, list):
            raise ValueError("tool_scope 必须是数组")
        tool_scope = (
            list(dict.fromkeys(str(item) for item in raw_scope if str(item).strip()))
            if isinstance(raw_scope, list) else []
        )
        payload = {
            "id": agent_id,
            "name": name[:80],
            "system_prompt": system_prompt,
            "skill_ids": skill_ids,
            "tool_scope": tool_scope,
        }
        if is_built_in:
            payload["built_in"] = True
        with self.lock:
            agents = self.data.setdefault("agents", [])
            index = next((i for i, item in enumerate(agents) if item.get("id") == agent_id), None)
            # 头像：调用方没带 avatar 键时保留已存值（表单保存不带头像，不能顺手清掉）。
            avatar = values.get("avatar")
            if avatar is None:
                avatar = (agents[index].get("avatar") if index is not None else "") or ""
            payload["avatar"] = str(avatar).strip()
            if index is None:
                agents.append(payload)
            else:
                agents[index] = payload
            if not self.get_agent(str(self.data.get("default_agent_id") or "")):
                self.data["default_agent_id"] = agents[0].get("id", "general") if agents else "general"
            self.save()
        return payload

    def delete_agent(self, agent_id: str) -> bool:
        agent_id = str(agent_id or "").strip()
        if agent_id in built_in_agent_ids():
            # 内置 Agent 不可删除：静默视为成功，避免前端报错。
            return False
        with self.lock:
            agents = self.data.setdefault("agents", [])
            before = len(agents)
            self.data["agents"] = [item for item in agents if item.get("id") != agent_id]
            if len(self.data["agents"]) == before:
                return False
            if self.data.get("default_agent_id") == agent_id:
                remaining = self.data["agents"]
                self.data["default_agent_id"] = (
                    next((item.get("id") for item in remaining if item.get("id") == "general"), None)
                    or (remaining[0].get("id") if remaining else "general")
                )
            self.save()
            return True




def _infer_supports_images(provider: dict[str, Any]) -> bool:
    """推断模型是否支持图片输入（supports_images 能力字段）。

    - 配置显式给出布尔值时直接使用；
    - DeepSeek 官方视觉模型 deepseek-v4-flash-vision-exp 明确为 true；
    - DeepSeek 官方其他模型默认 false；
    - 其余按模型名启发式推断（gemini / claude / 含 vl 等关键词）。
    """
    explicit = provider.get("supports_images")
    if isinstance(explicit, bool):
        return explicit
    base_url = str(provider.get("base_url") or "").lower()
    model = str(provider.get("model") or "").strip().lower()
    if "api.deepseek.com" in base_url or "deepseek.com" in base_url:
        deepseek_vision_hints = (
            "deepseek-vl", "vision", "multimodal", "omni", "-vl", "_vl", "vl2",
        )
        return model == "deepseek-v4-flash-vision-exp" or any(
            hint in model for hint in deepseek_vision_hints
        )
    try:
        from naiba.vision.runtime import VisionRouter

        return VisionRouter._brain_supports_vision(provider)
    except Exception:  # noqa: BLE001 - 视觉模块不可用时不阻塞模型解析
        return False


# 运行设置里两道「别永久卡住」保险的默认值。必须与实现侧常量一致：
# - LOCAL_FIRST_BYTE_TIMEOUT_SECONDS（naiba/llm/runtime.py）
# - DEFAULT_MAX_STEPS（naiba/skills/agent.py）
# 一致性由 tests/test_runtime_guards.py 守门（两份定义漂移会让默认值失效）。
LOCAL_FIRST_BYTE_TIMEOUT_DEFAULT = 120
AGENT_MAX_STEPS_DEFAULT = 200


def _infer_context_window(provider: dict[str, Any]) -> int:
    """Return a trustworthy context limit, or 0 when the API does not expose one."""
    try:
        explicit = int(provider.get("context_window") or provider.get("context_size") or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit

    # 本地后端探测到的真实窗口（llm.local_probe 回写，见 remember_local_context_window）：
    # 优先级低于显式配置、高于「未知」。不认这个字段的话，探测值就只在会话路径生效，
    # 设置页与其它读取点仍然拿 0，又退回在线 256k 兜底。
    try:
        probed = int(provider.get("context_window_probed") or 0)
    except (TypeError, ValueError):
        probed = 0
    if probed > 0:
        return probed

    hostname = (urllib.parse.urlparse(str(provider.get("base_url") or "")).hostname or "").lower()
    # Do not infer a provider's advertised context from its hostname.  A
    # gateway may expose a different limit, and the settings UI must not claim
    # a value the API did not provide.  Explicit provider config remains the
    # source of truth.
    return 0


def _context_window_source(provider: dict[str, Any]) -> str:
    if not _infer_context_window(provider):
        return "unknown"
    if provider.get("context_window") or provider.get("context_size"):
        if str(provider.get("kind") or "online").strip().lower() == "local":
            return "local_config"
        return "provider_config"
    if provider.get("context_window_probed"):
        return "local_probe"
    return "model_capability"


