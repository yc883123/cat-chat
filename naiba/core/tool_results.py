"""工具结果对模型可见性的单一事实源（前后端所见一致）。

工具执行返回统一是 ``(success, result_text)``，随后在三层数据间流动：

- **原始 run**（内存，仅宿主收尾用）：``{tool, arguments, result(原文), success, reason}``；
- **模型可见 model_run**（模型上下文）：``{tool, success, result}``——``arguments``/``reason``
  是模型自产自销（它自己刚发的入参与理由），不进模型上下文；``result`` 剥离宿主机器字段
  （存储路径/缩略图/尺寸/SHA-256；vision_image_ops 的产物路径 path/heatmap 例外保留——
  它是模型引用产物的凭据）并统一追加截断标记；
- **展示 run display_run**（stream 事件与 ``metadata.tool_runs``）：``{tool, arguments, result(脱敏+标记), success, reason}``
  ——与 model_run 唯一差异是 ``arguments``/``reason``（仅前端展示供用户核对，不影响模型）。

模型上下文的三条通道（native ``role: tool`` 消息、兼容 ``<untrusted_tool_result>``、
历史兜底注入 ``_content_read_tool_outputs``）现在全部以 model_run 为准。
"""

from __future__ import annotations

import json
from typing import Any

MODEL_RESULT_MAX_CHARS = 30000
MODEL_JSON_MAX_CHARS = 60000

_TRUNCATE_MARK = "…（已截断：原文 {total} 字符，仅显示前 {limit} 字符）"


def truncate(text: str, limit: int) -> str:
    """超长统一截断并加明确标记，避免模型误把截断点当成全部内容。"""
    value = str(text or "")
    if len(value) <= limit:
        return value
    marker = _TRUNCATE_MARK.format(total=len(value), limit=limit)
    return value[:limit].rstrip() + "\n" + marker


def truncate_json_text(text: str, limit: int = MODEL_JSON_MAX_CHARS) -> str:
    return truncate(text, limit)


def _json_object(result: str) -> dict[str, Any] | None:
    """尝试把 result 解析为 JSON 对象；任何失败返回 None（调用方应原样保留）。"""
    try:
        payload = json.loads(str(result or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _vision_load_summary(result: str) -> str:
    """视觉「装载」形态（vision_analyze 多模态分流）：只留 note 与图片名。

    原始 result 含存储路径/缩略图/尺寸——宿主（extract_attachments、图片注入）用，
    模型只需要名字以便引用具体图片。
    """
    payload = _json_object(result)
    if payload is None:
        return str(result or "")
    if "images" not in payload and "note" not in payload:
        return str(result or "")
    names = [
        str(img.get("name") or "")
        for img in payload.get("images") or []
        if isinstance(img, dict) and img.get("name")
    ]
    return json.dumps({"note": str(payload.get("note") or ""), "images": names}, ensure_ascii=False)


def _vision_ops_summary(result: str) -> str:
    """vision_image_ops：原样保留（含产物路径 path/heatmap）。

    裁剪图/热力图是模型后续操作的对象（保存到工作区、复制、再处理），产物路径
    必须回传给模型才能引用——剥离路径会导致模型找不到刚落盘的产物（实测：多轮
    搜索 + 绕道 PowerShell 重做）。与 vision_analyze 装载形态不同：那是宿主注入
    用，模型只需按名引用；工具产物路径是模型自产自销的引用凭据。
    """
    return str(result or "")


def model_visible_result(tool_name: str, result: str) -> str:
    """把工具原始 result 变为模型可见内容：按工具剥离机器字段 + 统一截断标记。

    ``vision_read_folder`` 为已退役的旧名，仅旧会话落库的 tool_runs 里可能出现；
    保留同一脱敏分支，避免老会话历史重放时把存储路径/尺寸灌回模型上下文。
    """
    if tool_name in {"vision_analyze", "vision_read_folder"}:
        value = _vision_load_summary(result)
    elif tool_name == "vision_image_ops":
        value = _vision_ops_summary(result)
    else:
        value = str(result or "")
    return truncate(value, MODEL_RESULT_MAX_CHARS)


def model_visible_run(run: dict[str, Any]) -> dict[str, Any]:
    """原始 run → 模型可见 run（去 arguments/reason，result 脱敏+截断标记）。"""
    return {
        "tool": str((run or {}).get("tool") or ""),
        "success": bool((run or {}).get("success", False)),
        "result": model_visible_result(str((run or {}).get("tool") or ""), (run or {}).get("result") or ""),
    }


def display_tool_run(run: dict[str, Any]) -> dict[str, Any]:
    """原始 run → 展示 run（web 事件 / metadata.tool_runs）：可见 result + 展示用 arguments/reason。

    ``media``/``media_truncated`` 是**宿主**在工具产出时按声明提取的托管记录
    （路径/缩略图/截断自述），随展示通道下发给前端就地渲染；它不进模型上下文
    （``model_visible_run`` 不含，模型无从看到宿主缓存路径）。
    """
    visible = model_visible_run(run)
    visible["arguments"] = (run or {}).get("arguments") if isinstance((run or {}).get("arguments"), dict) else {}
    if (run or {}).get("reason"):
        visible["reason"] = str((run or {}).get("reason") or "")
    # 动作来源（native / bare_protocol_tail / fenced）：只作可观测与前端确认文案用，
    # **不进模型上下文**（model_visible_run 不含），也不携带命令正文。
    if (run or {}).get("source"):
        visible["action_source"] = str((run or {}).get("source") or "")
    media = (run or {}).get("media")
    if isinstance(media, list) and media:
        visible["media"] = media
    truncated = (run or {}).get("media_truncated")
    if isinstance(truncated, dict) and truncated:
        visible["media_truncated"] = truncated
    return visible
