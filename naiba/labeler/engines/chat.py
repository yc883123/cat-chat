"""``engine=chat``：自定义打标选中 Skill 后，走对话模块同款 SkillAgent 工具循环。

与 provider 引擎的差别：Skill 即规则——``<skill name root>`` 块（SKILL.md 全文，
不截断）由 SkillAgent 自己注入，参考文件由模型按需 ``read_file`` 读取；引擎不做
任何提示词拼装与截断。产出文本交给既有解析管线（``parse_response``，含非 JSON
兜底），触发词/过滤/落库全部不变——口径就是「得到一个文本，经由打标器处理」。

无会话运行的对象构造照抄 ``naiba/subagent.py`` 的先例：无 run manager、无消息
落库、无确认 UI（``confirmation_ui=False``，需确认动作按「未执行」fail-closed 收尾）。
工具白名单只放免确认的只读三件套：``read_file`` 的信任根含 active skill 根目录
（tools/providers/core.py），Skill 附带的参考文档可直接读；``run_skill_script``
这类需确认/有副作用的工具不进白名单——批量打标无人值守，且模型根本看不到未列入
schema 的工具，不会误调。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from naiba.core.exceptions import TaskCancelled
from naiba.core.history import encode_image_for_model
from naiba.labeler.engines.provider import parse_response

ENGINE_ID = "chat"

CHAT_ALLOWED_TOOLS = ("read_file", "list_directory", "search_files")
# 每张图的循环步数上限：读几个参考文件 + 给出答复绰绰有余，防个别技能失控多轮烧钱。
CHAT_MAX_STEPS = 12


def build_chat_prompt(config: dict[str, Any]) -> str:
    """对话引擎的每图指令：极简，不掺输出格式要求（输出规则由 Skill 决定）。"""
    parts = ["请按上方技能规则处理这张图片。"]
    extra = str(config.get("custom_prompt") or "").strip()
    if extra:
        parts.append("用户补充要求（优先满足，可点名要忽略的元素）：" + extra)
    return "\n".join(parts)


class ChatSkillEngine:
    """``engine=chat``：单图一次 SkillAgent 会话，返回与 provider 引擎同构的解析结果。"""

    id = ENGINE_ID

    def __init__(self, app: Any, config: dict[str, Any] | None = None,
                 event_sink: Any = None):
        self.app = app
        self.config = config or {}
        self._event_sink = event_sink
        # 多轮工具循环的真实用量：SkillAgent.run 返回汇总，runner 优先读这里。
        self.last_usage: dict[str, Any] = {}
        self.last_run_context: dict[str, Any] = {}

    def _build_worker(self, task_label: str) -> tuple[Any, Any]:
        """对话同款的 SkillAgent 装配（subagent.py:241-246 先例），全部无会话落库。"""
        from naiba.plans import CraftToolExecutor
        from naiba.skills.agent import SkillAgent

        base_executor = self.app.executor
        sub_executor = (
            base_executor.clone_for_permission("auto")
            if callable(getattr(base_executor, "clone_for_permission", None))
            else base_executor
        )
        if hasattr(sub_executor, "debug_label"):
            sub_executor.debug_label = task_label
        worker = SkillAgent(
            self.app.catalog,
            CraftToolExecutor(sub_executor),
            self.app.models.complete,
            None,  # media_collector=None：跳过媒体采集（测试/轻量调用口径，见 agent.py）
        )
        return worker, sub_executor

    def _bridge_event(self, image_name: str) -> Any:
        """SkillAgent 事件 → 贴签匠事件流：只转发批量进度有意义的关键事件。"""
        sink = self._event_sink
        if not callable(sink):
            return None

        def emit(payload: dict[str, Any]) -> None:
            kind = str(payload.get("type") or "")
            message = str(payload.get("message") or "")
            try:
                if kind == "status":
                    sink({"type": "status", "message": f"{image_name}: {message}"})
                elif kind == "tool_result":
                    sink({"type": "status",
                          "message": f"{image_name}: 工具 {payload.get('name') or ''} 返回"})
                elif kind in {"context_full", "run_failed", "skill_warning"}:
                    sink({"type": "status", "message": f"{image_name}: {message or kind}"})
                # delta / reasoning_delta / usage 等流式噪声不进打标事件流。
            except Exception:  # noqa: BLE001 - 事件桥故障绝不影响打标主流程
                pass
        return emit

    def run(
        self,
        *,
        profile: dict[str, Any],
        image_path: str,
        prompt: str,
        options: dict[str, Any],
        status: Any = None,
    ) -> dict[str, Any]:
        from naiba.skills.agent import _CONTEXT_FULL_NOTICE, _CONTEXT_LOOP_NOTICE

        skill_id = str(self.config.get("skill_id") or "").strip()
        if not skill_id:
            raise ValueError("对话引擎（Skill 打标）要先选择 Skill")
        part = encode_image_for_model(str(image_path or ""))
        if part is None:
            raise ValueError("图片无法读取或编码（文件可能已被清理）")
        # 图在前文在后（与 provider 引擎同序）；history 末条已是 user 时
        # SkillAgent 不会再追加 user_message（agent.py:1106-1107）。
        history = [{"role": "user", "content": [
            part,
            {"type": "text", "text": "\n" + (str(prompt or "").strip() or "请按技能规则处理这张图片。")},
        ]}]
        cancel_event = options.get("cancel_event")
        image_name = str(options.get("item_name") or "图片")
        worker, sub_executor = self._build_worker(str(options.get("lock_label") or "打标器"))
        run_context = {
            "run_id": f"labeler-{str(options.get('item_id') or 'item')}",
            "conversation_id": "",
            "owner_session_id": "",
            "depth": 1,
            "allowed_tools": list(CHAT_ALLOWED_TOOLS),
            "skill_policy": {"mode": "exclusive", "skill_ids": [skill_id]},
            "model_has_vision": True,
            "tool_defs": None,
            "workspace_dir": str(Path(self.app.config.resolve_workspace_dir()).expanduser().resolve()),
            "executor": sub_executor,
            "cancel_event": cancel_event,
            # 批量无人值守：需确认的动作直接按「未执行」收尾，不挂起等确认。
            "confirmation_ui": False,
        }
        try:
            self.last_run_context = run_context
            text, _runs, _reasonings, usage = worker.run(
                "",
                history,
                profile,
                dict(options),
                {"mode": "exclusive", "skill_ids": [skill_id]},
                [],
                str(self.config.get("system_prompt") or ""),  # Agent system prompt 与 Skill 一同注入
                list(CHAT_ALLOWED_TOOLS),
                self._bridge_event(image_name),
                cancel_event,
                max_steps=CHAT_MAX_STEPS,
                tool_registry=getattr(self.app, "tool_registry", None),
                run_context=run_context,
            )
        except TaskCancelled:
            raise
        self.last_usage = dict(usage or {})
        cleaned = str(text or "").strip()
        # 上下文塞不下时 SkillAgent 把 notice 当正常返回值交回（不抛异常）：
        # 识别出来转成该图失败，避免把提示文案误存成标注。
        if not cleaned or cleaned.startswith(("上下文已达到窗口上限",)) \
                or cleaned in {_CONTEXT_FULL_NOTICE, _CONTEXT_LOOP_NOTICE}:
            raise ValueError("Skill 会话没有产出可用文本（上下文超限或步数用尽）")
        parsed = parse_response(cleaned)
        parsed["raw"] = cleaned
        return parsed
