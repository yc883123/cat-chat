# -*- coding: utf-8 -*-
"""冻结版自检：确认「插话（运行中的第二输入通道）」与「状态条不再误报重连中」真的进了 exe。

用法：`dist\\naiba-chat.exe --run-skill-script <本文件绝对路径>`

存在理由（与 `frozen_tool_prose_check.py` / `frozen_chat_font_check.py` 同类）：
这两处改动都横跨**两层形态**，源码模式看不出打包缺失——
① 后端部分（存储状态机 / 契约 / manager 方法）打包进 PYZ，盘上没有可读的 `.py`，
   只能用**运行期真调用**验证；
② 前端部分是打包的静态资源（新增 `public/js/18-interjections.js`、`11-run-stream.js`
   的状态机改造、`index.html` 的新控件、`styles.css` 的新样式），而**用户跑的正是 exe**——
   不重编译换 exe，改一行也到不了用户手上（§六 同类：`frozen_mobile_ui_check.py`）。

逻辑守门在 `tests/test_interjections.py`，真后端浏览器冒烟在
`verify/interjection_smoke.py` + `.cjs`，源码级教训见维护说明 §九.100 / §九.101。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 冻结版没打 unittest（`import unittest.mock` 会 ModuleNotFoundError），这里手写最小替身。

ok = True

AGENT = {"id": "", "name": "Chat", "system_prompt": "", "skill_ids": []}


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition or not detail else f'  -> {detail}'}")
    if not condition:
        ok = False


# ---- ① 运行期：契约登记真的进了 exe -------------------------------------------
try:
    from naiba.core.contracts import EVENT_PAYLOAD_KEYS, RUN_CONTEXT_KEYS, EventType
    from naiba.core.messages import MESSAGE_METADATA_KEYS, MetadataKeys

    values = {member.value for member in EventType}
    check("契约：EventType 登记了 user_guidance", "user_guidance" in values)
    check("契约：EventType 登记了 interjection_consumed", "interjection_consumed" in values)
    check(
        "契约：两枚事件的负载键都是 {message_id, message}",
        EVENT_PAYLOAD_KEYS.get("user_guidance") == frozenset({"message_id", "message"})
        and EVENT_PAYLOAD_KEYS.get("interjection_consumed") == frozenset({"message_id", "message"}),
        f"guidance={EVENT_PAYLOAD_KEYS.get('user_guidance')}",
    )
    check(
        "契约：RUN_CONTEXT_KEYS 含 pull_interjections / mark_interjections_consumed",
        {"pull_interjections", "mark_interjections_consumed"} <= set(RUN_CONTEXT_KEYS),
    )
    interjection_keys = {
        MetadataKeys.INTERJECTION,
        MetadataKeys.INTERJECTION_GUIDED,
        MetadataKeys.INTERJECTION_CONSUMED,
        MetadataKeys.INTERJECTION_STOPPED,
    }
    check(
        "契约：插话四枚 metadata 键都在权威清单里",
        interjection_keys <= set(MESSAGE_METADATA_KEYS),
        str(sorted(interjection_keys - set(MESSAGE_METADATA_KEYS))),
    )
except Exception as exc:  # noqa: BLE001
    check("契约：可导入契约层", False, repr(exc))

# ---- ② 运行期：方法存在（打包漏收模块时这里最先红） -----------------------------
try:
    from naiba.run.manager import ConversationRunManager
    from naiba.storage.store import ChatStorage

    for name in ("interject", "guide_interjection", "edit_interjection", "delete_interjection"):
        check(f"运行期：ConversationRunManager.{name} 存在", hasattr(ConversationRunManager, name))
    for name in (
        "add_run_interjection",
        "list_run_interjections",
        "guide_run_interjection",
        "mark_run_interjections_consumed",
        "stop_pending_interjections",
        "edit_run_interjection",
        "delete_run_interjection",
    ):
        check(f"运行期：ChatStorage.{name} 存在", hasattr(ChatStorage, name))
except Exception as exc:  # noqa: BLE001
    check("运行期：可导入 manager / storage", False, repr(exc))

# ---- ③ 运行期：真建库跑通状态机与终态冻结 -------------------------------------
try:
    from naiba.core.history import build_model_history
    from naiba.core.messages import MetadataKeys
    from naiba.storage.store import ChatStorage

    with tempfile.TemporaryDirectory() as tmp:
        storage = ChatStorage(Path(tmp) / "chat.db")
        conversation_id = str(storage.create_conversation()["id"])
        run_id = str(storage.create_run(conversation_id, "回答", AGENT, {}, kind="chat")["id"])

        saved = storage.add_run_interjection(conversation_id, run_id, "改成先看了再删")
        message_id = str(saved["id"])
        check("状态机：入队落库为 role=user 的普通消息", saved["role"] == "user", str(saved["role"]))
        check(
            "状态机：pending 不进 agent 可拉取队列",
            storage.list_run_interjections(run_id) == [],
        )

        storage.guide_run_interjection(conversation_id, run_id, message_id)
        pullable = storage.list_run_interjections(run_id)
        check(
            "状态机：引导后进入可拉取队列且内容一致",
            len(pullable) == 1 and pullable[0]["content"] == "改成先看了再删",
            str(pullable),
        )

        # 未消费前不许进模型上下文——这是「用户没点引导就不发」的底层保证。
        # 历史过滤是纯函数，用合成消息直接考打包内的 build_model_history（与单测同口径）。
        base = {MetadataKeys.INTERJECTION: True, MetadataKeys.RUN_ID: run_id}
        history_src = [
            {"role": "user", "content": "第一问", "metadata": {}},
            {"role": "assistant", "content": "第一答", "metadata": {}},
            {"role": "user", "content": "排队但没引导", "metadata": dict(base)},
            {"role": "user", "content": "取消时被冻结",
             "metadata": {**base, MetadataKeys.INTERJECTION_STOPPED: True}},
        ]
        contents = [str(item.get("content") or "") for item in build_model_history(history_src)]
        check(
            "状态机：未消费 / 被冻结的插话都不进模型历史",
            "排队但没引导" not in contents and "取消时被冻结" not in contents,
            str(contents),
        )
        storage.mark_run_interjections_consumed(run_id, [message_id])
        check(
            "状态机：消费后不再出现在可拉取队列",
            storage.list_run_interjections(run_id) == [],
        )
        consumed_src = history_src + [
            {"role": "user", "content": "已被模型取走",
             "metadata": {**base, MetadataKeys.INTERJECTION_CONSUMED: True}},
        ]
        check(
            "状态机：消费后才进模型历史（此后它就是本轮真实上下文）",
            "已被模型取走" in [str(item.get("content") or "") for item in build_model_history(consumed_src)],
        )

        # 终态冻结：任何终态都要冻结，否则「正常跑完」的残留会停在可引导态。
        second = str(storage.add_run_interjection(conversation_id, run_id, "这条要烂在队列里")["id"])
        storage.guide_run_interjection(conversation_id, run_id, second)
        storage.stop_pending_interjections(run_id)
        check(
            "终态冻结：冻结后 guided 行也不再可拉取",
            storage.list_run_interjections(run_id) == [],
        )

        # §九.100 ④：冻结且已引导的行必须还能删——否则用户排进去的字既发不出也删不掉。
        check(
            "终态冻结：已引导 + 已冻结的行仍可删除",
            storage.delete_run_interjection(conversation_id, run_id, second) is True,
        )
        # 终态之后清理残留：真实时序是「run 结束 → `_finish` 冻结 → 用户删除」，
        # 而删除不该再要求 run 还活着（§九.100 ④ 那个死结）。
        # 先收掉当前 run——顶层 run 互斥（ACTIVE_RUN），不结束就建不了下一个。
        storage.update_background_task(run_id, status="completed", finished=True)
        dead_run = str(storage.create_run(conversation_id, "回答", AGENT, {}, kind="chat")["id"])
        fourth = str(storage.add_run_interjection(conversation_id, dead_run, "取消前一刻写的")["id"])
        storage.guide_run_interjection(conversation_id, dead_run, fourth)
        storage.update_background_task(dead_run, status="cancelled", finished=True)
        storage.stop_pending_interjections(dead_run)
        check(
            "终态冻结：run 结束后删除残留行不需要活动 run（且已引导也能删）",
            storage.delete_run_interjection(conversation_id, dead_run, fourth) is True,
        )
except Exception as exc:  # noqa: BLE001
    check("运行期：真建库跑通插话状态机", False, repr(exc))

# ---- ④ 打包资源：前端两层都必须是新代码 ---------------------------------------
try:
    from naiba.paths import default_path_context

    public = Path(default_path_context().public_dir)

    panel = (public / "js" / "18-interjections.js").read_text(encoding="utf-8")
    check("打包资源：新增的 18-interjections.js 在包内", len(panel) > 1000)
    for symbol in ("sendRunInterjection", "freezeQueuedInterjections", "settlePanel", "rowRunId"):
        check(f"打包资源：18-interjections.js 含 {symbol}", symbol in panel)

    index_html = (public / "index.html").read_text(encoding="utf-8")
    check("打包资源：index.html 含队列面板容器 runGuidanceList", "runGuidanceList" in index_html)
    check("打包资源：index.html 含插话按钮 interjectButton", "interjectButton" in index_html)

    chat_input = (public / "js" / "12-chat-input.js").read_text(encoding="utf-8")
    check("打包资源：12-chat-input.js 注册了 user_guidance handler", "user_guidance" in chat_input)
    check(
        "打包资源：12-chat-input.js 注册了 interjection_consumed handler",
        "interjection_consumed" in chat_input,
    )

    run_stream = (public / "js" / "11-run-stream.js").read_text(encoding="utf-8")
    check(
        "打包资源：11-run-stream.js 用 elapsedReconnectShown 记录横幅（不是 elapsedKind）",
        "elapsedReconnectShown" in run_stream and "elapsedKind" not in run_stream,
    )
    check("打包资源：11-run-stream.js 有 settleReconnectStatus 单点收口", "settleReconnectStatus" in run_stream)

    messages_js = (public / "js" / "04-messages.js").read_text(encoding="utf-8")
    check("打包资源：04-messages.js 把插话滤出消息流", "isQueuedInterjection" in messages_js)

    styles = (public / "styles.css").read_text(encoding="utf-8")
    check("打包资源：styles.css 含队列面板样式", ".run-guidance-list" in styles)
except Exception as exc:  # noqa: BLE001
    check("打包资源：可读到打包内的前端资源", False, repr(exc))

print("\n全部通过" if ok else "\n存在未通过项")
sys.exit(0 if ok else 1)
