"""工具结果对模型可见性的单一事实源（前后端所见一致）。

工具执行返回统一是 ``(success, result_text)``，随后在三层数据间流动：

- **原始 run**（内存，仅宿主收尾用）：``{tool, arguments, result(原文), success, reason}``；
- **模型可见 model_run**（模型上下文）：``{tool, success, result}``——``arguments``/``reason``
  是模型自产自销（它自己刚发的入参与理由），不进模型上下文；``result`` 剥离宿主机器字段
  （存储路径/缩略图/尺寸/SHA-256；vision_image_ops 的产物路径 path/heatmap 例外保留——
  它是模型引用产物的凭据）并统一追加截断标记；采集出卡时在 result 末尾附一条
  「已附媒体卡」短注（见 ``media_attached_note``，事实只来自本轮实际采集记录）；
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

# ---- 「已附媒体卡」短注（模型可见的附卡事实） ---------------------------------
# 采集与出卡都是宿主侧行为，模型上下文剥离掉 media 记录本身——扫描命令实际出了卡，
# 模型却不知道，会重复调用 show_media 或凭空声称"已展示"。短注以**本轮真实采集结果**
# 为唯一事实来源，附在模型可见结果的末尾。
#
# 字节确定性契约（前缀缓存）：同样的 media 记录 → 同样的短注字节，无时间戳、无随机量。
# 活轮与历史回放共用同一入口：
#   · 活轮 ``skills/agent`` 把 model_visible_run 的产物写进 ``role: tool`` 消息，
#     随 trace 落库，后续轮次**原样重放**（字节已冻结）；
#   · 无 trace 的历史兜底重建（``core/history``）从已保存的展示 run 里读出 media，
#     经同一函数再生另一份——同一条 media ⇒ 同一串注，逐字节一致。

# 短注里最多列出的展示名；超出明确说明数量，避免一条注无限增长。
MEDIA_NOTE_MAX_NAMES = 8

# 媒体类型在注里的中文标签（未知类型回落"媒体"）。
_MEDIA_NOTE_KIND_LABELS = {"image": "图片", "video": "视频", "audio": "音频"}


def media_attached_note(media: Any, truncated: Any = None) -> str:
    """把采集到的媒体记录序列化成一条固定形态的短注；无记录返回空串。

    形态（排序、字段顺序、截断说明固定）::

        [媒体附卡] 本轮已附媒体卡 1 张：图片 a.png
        [媒体附卡] 本轮已附媒体卡 3 张：图片 a.png、图片 b.png、视频 c.mp4

    采集端分桶截断时（``media_truncated``：共 N 张仅附前 M 张）显式说明总量::

        [媒体附卡] 本轮已附媒体卡 20 张（共 96 张由结果触发，仅附前 20 张）：…

    名称超过 ``MEDIA_NOTE_MAX_NAMES`` 只列前 8 个并说明余量::

        [媒体附卡] 本轮已附媒体卡 12 张：…（仅列前 8 个名称，其余 4 张也已附卡）

    只包含类型与展示名；``source``/``thumb_path`` 等宿主字段一律不进注。
    **只认实际记录**：media 为空（采集失败/拒绝/intent 未放行/范围外）返回空串，
    绝不声称"已附卡"；没有展示名的记录也不进注——注的价值是"模型可按名区分
    目标"，一个叫不出名字的"已附卡"既帮不了模型也说不清是哪张卡
    （采集器对无名候选会回落"生成结果"，真实记录都带名）。
    """
    records = [
        record
        for record in (media or [])
        if isinstance(record, dict) and str(record.get("name") or "").strip()
    ]
    if not records:
        return ""
    total_shown = len(records)
    head = f"[媒体附卡] 本轮已附媒体卡 {total_shown} 张"
    trunc = truncated if isinstance(truncated, dict) else None
    if trunc:
        try:
            total = max(0, int(trunc.get("total") or 0))
        except (TypeError, ValueError):
            total = 0
        if total > total_shown:
            head += f"（共 {total} 张由结果触发，仅附前 {total_shown} 张）"
    entries: list[str] = []
    for record in records:
        label = _MEDIA_NOTE_KIND_LABELS.get(str(record.get("kind") or ""), "媒体")
        name = str(record.get("name") or "").strip()
        entries.append(f"{label} {name}" if name else label)
    note = head + "：" + "、".join(entries[:MEDIA_NOTE_MAX_NAMES])
    if len(entries) > MEDIA_NOTE_MAX_NAMES:
        note += (
            f"（仅列前 {MEDIA_NOTE_MAX_NAMES} 个名称，"
            f"其余 {len(entries) - MEDIA_NOTE_MAX_NAMES} 张也已附卡）"
        )
    return note


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


def _show_media_summary(result: str) -> str:
    """show_media：只留状态/原因/类型/展示名（剥掉 source / thumb_path / 绝对路径）。

    为什么必须剥：``show_media`` 的原始结果里 source 是**工作区内的真实绝对路径**，
    那是宿主出卡用的凭据。模型只需要知道"成了没有、是哪一个"，把路径回灌进上下文
    等于每展示一次就多一段本机目录结构，也给了后续轮次"照着猜路径"的诱因。
    候选展示名（choices）只回**文件名**，同样不含目录。
    """
    payload = _json_object(result)
    if payload is None:
        return str(result or "")
    visible: dict[str, Any] = {
        "status": str(payload.get("status") or ""),
        "kind": str(payload.get("kind") or ""),
        "name": str(payload.get("name") or ""),
        "reason": str(payload.get("reason") or ""),
    }
    choices = [
        str(item)
        for item in (payload.get("choices") or [])
        if isinstance(item, str) and item
    ]
    if choices:
        visible["choices"] = choices[:8]
    if payload.get("message"):
        visible["message"] = str(payload.get("message") or "")
    return json.dumps(visible, ensure_ascii=False)


def model_visible_result(tool_name: str, result: str) -> str:
    """把工具原始 result 变为模型可见内容：按工具剥离机器字段 + 统一截断标记。

    ``vision_read_folder`` 为已退役的旧名，仅旧会话落库的 tool_runs 里可能出现；
    保留同一脱敏分支，避免老会话历史重放时把存储路径/尺寸灌回模型上下文。
    """
    if tool_name in {"vision_analyze", "vision_read_folder"}:
        value = _vision_load_summary(result)
    elif tool_name == "vision_image_ops":
        value = _vision_ops_summary(result)
    elif tool_name == "show_media":
        value = _show_media_summary(result)
    else:
        value = str(result or "")
    return truncate(value, MODEL_RESULT_MAX_CHARS)


def _result_with_media_note(
    tool_name: str, result: str, media: Any, truncated: Any, success_flag: bool = True
) -> str:
    """模型可见 result + 附卡短注尾部（唯一拼装入口，活轮与历史回放共用）。

    短注放在截断标记**之后**：无论 result 多长、截断与否，末尾的附卡事实永远完整
    （"截断长度固定"），不会被截断标记吞掉。

    幂等：历史兜底重建会把**已带短注的展示 run** 再过一遍本函数（前端所见与模型
    所见同源），末尾已经是同一串注时不再追加——同一条 media 序列化出的注相同，
    ``endswith`` 判定成立，字节与首次生成完全一致。

    只有 ``run["success"]`` 为真才会附注：失败/被拒的工具即使 record 残留也不得
    声称"已附卡"（正常采集器对失败结果直接返回空记录，这里是最后的硬拦截）。
    """
    if not success_flag:
        return model_visible_result(tool_name, result)
    base = model_visible_result(tool_name, result)
    note = media_attached_note(media, truncated)
    if not note:
        return base
    if base.endswith(note):
        return base
    if base.endswith("\n" + note):
        return base
    return base + "\n" + note


def model_visible_run(run: dict[str, Any]) -> dict[str, Any]:
    """原始 run → 模型可见 run（去 arguments/reason，result 脱敏+截断标记）。

    ``run["media"]``/``run["media_truncated"]`` 是采集器写回的实际附卡记录
    （仅在本轮宿主采集成功且工具成功、为真实产物时非空）：非空时在 result 末尾
    附一条「已附媒体卡」短注，让模型知道卡片已挂在本轮回复上（避免重复出卡或
    凭空声称）；失败/被拒的工具不附注（见 ``_result_with_media_note``）。
    """
    source_run = run or {}
    return {
        "tool": str(source_run.get("tool") or ""),
        "success": bool(source_run.get("success", False)),
        "result": _result_with_media_note(
            str(source_run.get("tool") or ""),
            source_run.get("result") or "",
            source_run.get("media"),
            source_run.get("media_truncated"),
            bool(source_run.get("success", False)),
        ),
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
