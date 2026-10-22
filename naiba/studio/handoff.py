"""Studio → 普通对话 交接 adapter（唯一跨域边界）。

按修订版计划 §「代码归属与依赖边界」：

- `naiba/studio/` 不得依赖 `naiba/run/chat.py` / `ConversationRunManager`；只有这里能跨进
  聊天域。组装根（`naiba/app.py`）注入两个回调：
    * `create_conversation_callback(body) -> dict`：等价 `app.api_create_conversation(body)`；
    * `submit_chat_callback(body) -> dict`：等价 `app.runs.submit_chat(body)`。
- adapter 只做"输入组装 + 顺序调用 + 错误翻译"，不持有模型引用、不知道 Run 细节。
- 交接输入体即计划 P3.4 的"可追溯上下文"：模板正文（若来自条目）、用户填写的变量、
  用户输入、生成结果，以清晰可读的中文块进入首条用户消息；后续轮次完全归普通对话。
"""

from __future__ import annotations

from typing import Any

HANDOFF_HEADER = "【来自 Cat Chat 工作台的交接】"


def compose_handoff_message(record: dict[str, Any], note: str = "") -> str:
    """把工作台生成记录整理成首条用户消息（纯文本块；模板正文长时使用原文）。"""
    parts: list[str] = [HANDOFF_HEADER]
    title = str(record.get("title") or "").strip()
    if record.get("item_id"):
        module = str(record.get("module") or "")
        parts.append(f"- 模板：{title}（模块 {module}，条目 id {record.get('item_id')}，来自{record.get('origin_label') or '预设库'}）")
    else:
        parts.append("- 输入方式：自由输入（未选模板）")
    vars_map = record.get("vars") or {}
    if isinstance(vars_map, dict) and vars_map:
        lines = [f"  - {key}：{value}" for key, value in vars_map.items()]
        parts.append("- 模板变量：")
        parts.extend(lines)
    user_request = str(record.get("user_request") or "").strip()
    if user_request:
        parts.append("- 我的输入：")
        parts.append(user_request)
    body_text = str(record.get("source_body") or "").strip()
    if record.get("item_id") and body_text:
        parts.append("- 模板正文：")
        parts.append("<<<")
        parts.append(body_text)
        parts.append(">>>")
    parts.append("- 工作台生成结果：")
    parts.append("<<<")
    parts.append(str(record.get("result") or "").strip() or "（无结果）")
    parts.append(">>>")
    follow = str(note or "").strip()
    if follow:
        parts.append("- 请基于以上内容继续：")
        parts.append(follow)
    return "\n".join(parts).strip()


class HandoffAdapter:
    """由组装根注入的回调执行：先创建普通会话，再提交首轮聊天。"""

    def __init__(self, create_conversation_callback: Any, submit_chat_callback: Any):
        self._create_conversation = create_conversation_callback
        self._submit_chat = submit_chat_callback

    def handoff(self, record: dict[str, Any], *, agent_id: str, note: str = "",
                model_key: str = "", model_name: str = "") -> dict[str, Any]:
        """创建普通会话（绑定 agent/model）并把 Studio 上下文交给聊天模块。

        任一步失败时：抛错给 service 让它转 4xx/409；普通会话若已创建而聊天提交失败，
        会话仍然保留（用户可在普通对话里直接重发/重写），Studio 侧在返回里带 `conversation_id`
        供前端去重与"返回工作台"路径。
        """
        agent_id = str(agent_id or "").strip()
        if not agent_id:
            raise ValueError("请选择 Agent")
        conv_body: dict[str, Any] = {"agent_id": agent_id}
        title = str(record.get("title") or "").strip() or "工作台生成"
        conv_body["title"] = f"工作台·{title}"[:120]
        if model_key:
            conv_body["model_key"] = str(model_key)
        if model_name:
            conv_body["model_name"] = str(model_name)[:256]
        conversation = self._create_conversation(conv_body)
        conversation_id = str((conversation or {}).get("id") or "")
        if not conversation_id:
            raise RuntimeError("创建会话失败：未返回会话 id")
        message = compose_handoff_message(record, note=note)
        run = self._submit_chat({
            "conversation_id": conversation_id,
            "message": message,
        })
        return {"conversation_id": conversation_id, "run": run or {}, "message": message}
