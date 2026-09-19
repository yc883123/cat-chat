# -*- coding: utf-8 -*-
"""活动时间线（metadata.activity）契约：严格物理序 + 条目时间戳 + 只留最后一段正文。

保护对象：run/stream.py `_build_activity_timeline`——
1. **严格物理序**：保留的条目按事件序列顺序输出，不做任何"语义修正重排"
   （buffered 思考出现在末尾就保持在末尾，与事件发生顺序一致）；
2. **条目 ts**：reasoning/prose/tool 条目附带对应 run_events 事件的 created_at
   毫秒时间戳（前端备用字段，当前仅传递不强制显示）；
3. 无时间戳数据（旧事件/测试桩）时省略 ts 键，不报错；
4. **只留最后一段 prose**（2026-09-19）：工具调用前的过程播报（"我已定位根因"…）
   一律丢弃，只保留最后一段正文（= 真正的最终答复）；工具与思考条目一个不少。

另含 run/chat.py `_rebuild_partial_run` 的正文口径（取消/失败/重启恢复）：与第 4 条同源，
正文只取最后一次工具调用**之后**累积的那段。

以及前端渲染层的同口径兜底（`public/js/03-media.js::activityMarkup`）：历史消息的 activity
是落库数据，后端改口径也重算不了，必须在渲染时再过滤一次。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.run.stream import _build_activity_timeline  # noqa: E402


def ev(kind: str, **extra):
    base = {"type": kind}
    base.update(extra)
    return base


class ActivityTimelineTests(unittest.TestCase):
    def test_strict_physical_order_keeps_trailing_reasoning_last(self):
        # buffered 思考（推理在最后才给出）必须保持在末尾——严格物理序，不允许重排。
        events = [
            ev("delta", content="先说一句", created_at=100),
            ev("reasoning_start", created_at=200),
            ev("reasoning_delta", content="后补的思考", created_at=250),
            ev("reasoning_end", created_at=300),
        ]
        activity = _build_activity_timeline(events, ["后补的思考"], [])
        types = [item["type"] for item in activity]
        # 无工具轮：prose 不内嵌（归到末尾 content），活动仅一条思考且保持在末尾。
        self.assertEqual(types, ["reasoning"])
        self.assertEqual(activity[0]["text"], "后补的思考")

    def test_trailing_reasoning_after_tools_stays_last(self):
        # 工具轮 + 后续正文与 buffered 思考：思考/工具保持物理序，最终答复固定到末尾。
        # 工具调用前的那句"调一下工具"属于过程播报，必须被丢弃（见下一个用例）。
        events = [
            ev("delta", content="调一下工具", created_at=100),
            ev("tool_start", created_at=200),
            ev("tool_result", created_at=300),
            ev("reasoning_start", created_at=400),
            ev("reasoning_delta", content="思考", created_at=500),
            ev("reasoning_end", created_at=600),
            ev("delta", content="完成", created_at=700),
        ]
        runs = [{"tool": "read_file", "success": True, "result": "ok"}]
        activity = _build_activity_timeline(events, ["思考"], runs)
        self.assertEqual(
            [item["type"] for item in activity],
            ["tool", "reasoning", "prose"],
            "工具调用前的过程播报被丢弃；思考/工具保持物理序，最终答复固定到末尾",
        )
        self.assertEqual(activity[-1]["text"], "完成")

    def test_tool_prefix_prose_dropped_keeps_only_final_answer(self):
        # 2026-09-19 用户实测：模型每轮工具调用前都说一句近义进度，十来段摞在一条回复里。
        # 只保留最后一段（后面不再有工具调用的那次 = 真正的最终答复）。
        events = [
            ev("delta", content="我已核对接口", created_at=100),
            ev("tool_result", created_at=200),
            ev("delta", content="根因已经锁定", created_at=300),
            ev("tool_result", created_at=400),
            ev("delta", content="现在补协议", created_at=500),
            ev("tool_result", created_at=600),
            ev("delta", content="修好了，共改 2 个文件。", created_at=700),
        ]
        runs = [
            {"tool": "read_file", "success": True, "result": "1"},
            {"tool": "read_file", "success": True, "result": "2"},
            {"tool": "edit_file", "success": True, "result": "3"},
        ]
        activity = _build_activity_timeline(events, [], runs)
        prose = [item for item in activity if item["type"] == "prose"]
        self.assertEqual(len(prose), 1, "十段重复进度只留一段")
        self.assertEqual(prose[0]["text"], "修好了，共改 2 个文件。")
        self.assertEqual(
            [item["type"] for item in activity],
            ["tool", "tool", "tool", "prose"],
            "工具条目一个不少，最终答复仍在末尾",
        )

    def test_single_prose_segment_is_untouched(self):
        # 只说过一句话（模型没啰嗦）时行为不变——过滤不引入多余改动。
        events = [
            ev("delta", content="先读配置", created_at=100),
            ev("tool_result", created_at=200),
        ]
        runs = [{"tool": "read_file", "success": True, "result": "ok"}]
        activity = _build_activity_timeline(events, [], runs)
        self.assertEqual([item["type"] for item in activity], ["tool", "prose"])
        self.assertEqual(activity[-1]["text"], "先读配置")

    def test_request_index_groups_by_usage_events(self):
        # usage 事件是请求轮次边界：其后到达的活动条目归属下一次请求（request_index+1）。
        events = [
            ev("tool_start", created_at=100),
            ev("tool_result", created_at=200),
            ev("usage", usage={"requests": 1}, created_at=300),
            ev("reasoning_start", created_at=400),
            ev("reasoning_delta", content="第二次请求的思考", created_at=500),
            ev("reasoning_end", created_at=600),
            ev("delta", content="最终答复", created_at=700),
        ]
        runs = [{"tool": "pwsh", "success": True, "result": "out"}]
        activity = _build_activity_timeline(events, ["第二次请求的思考"], runs)
        tool = next(item for item in activity if item["type"] == "tool")
        reasoning = next(item for item in activity if item["type"] == "reasoning")
        prose = next(item for item in activity if item["type"] == "prose")
        self.assertEqual(tool["request_index"], 1, "usage 前的条目归属请求 1")
        self.assertEqual(reasoning["request_index"], 2, "usage 后的条目归属请求 2")
        self.assertEqual(prose["request_index"], 2)

    def test_entries_carry_ts_from_events(self):
        events = [
            ev("delta", content="正文段", created_at=100),
            ev("delta", content="继续", created_at=150),
            ev("tool_start", created_at=200),
            ev("tool_result", created_at=300),
            ev("reasoning_start", created_at=400),
            ev("reasoning_delta", content="思考内容", created_at=450),
            ev("reasoning_end", created_at=500),
        ]
        runs = [{"tool": "pwsh", "success": True, "result": "out"}]
        activity = _build_activity_timeline(events, ["思考内容"], runs)
        prose = next(item for item in activity if item["type"] == "prose")
        tool = next(item for item in activity if item["type"] == "tool")
        reasoning = next(item for item in activity if item["type"] == "reasoning")
        self.assertEqual(prose["ts"], 100, "prose ts 取本段首个 delta 事件")
        self.assertEqual(tool["ts"], 300, "tool ts 取 tool_result 事件")
        self.assertEqual(reasoning["ts"], 500, "reasoning ts 取 reasoning_end 事件")

    def test_no_ts_key_without_created_at(self):
        events = [
            ev("delta", content="正文"),
            ev("tool_start"),
            ev("tool_result"),
        ]
        activity = _build_activity_timeline(events, [], [{"tool": "pwsh", "success": True, "result": "out"}])
        for item in activity:
            self.assertNotIn("ts", item, "无时间戳事件不产出 ts 键")

    def test_rational_reasoning_between_tools_keeps_interleave(self):
        # 工具之间的中途思考保持交错位置（物理序）。
        events = [
            ev("tool_start", created_at=100),
            ev("tool_result", created_at=200),
            ev("reasoning_start", created_at=300),
            ev("reasoning_delta", content="中途思考", created_at=350),
            ev("reasoning_end", created_at=400),
            ev("tool_start", created_at=500),
            ev("tool_result", created_at=600),
        ]
        runs = [
            {"tool": "a", "success": True, "result": "1"},
            {"tool": "b", "success": True, "result": "2"},
        ]
        activity = _build_activity_timeline(events, ["中途思考"], runs)
        self.assertEqual(
            [item["type"] for item in activity],
            ["tool", "reasoning", "tool"],
            "工具之间的思考保持事件物理序",
        )


class PartialRunRebuildTests(unittest.TestCase):
    """取消/失败/重启恢复路径的正文口径：只取最后一次工具调用之后的正文。

    与 activity 的 prose 过滤同源（2026-09-19 用户实测"十句进度话术堆在一条回复里"）：
    此前 `_rebuild_partial_run` 把 run 里所有 delta 拼成正文，中止的消息看起来也像一堆
    重复播报。
    """

    def test_content_keeps_only_last_delta_segment(self):
        from naiba.run.chat import ConversationRunMixin

        events = [
            ev("delta", content="我已定位根因，", created_at=100),
            ev("delta", content="现在补测试。", created_at=150),
            ev("tool_result", tool="edit_file", success=True, result="ok", created_at=200),
            ev("delta", content="已修复并跑通测试。", created_at=300),
        ]
        reasoning, tool_runs, content, activity = ConversationRunMixin._rebuild_partial_run(
            "run-1", events
        )
        self.assertEqual(reasoning, [])
        self.assertEqual(len(tool_runs), 1)
        self.assertEqual(content, "已修复并跑通测试。", "工具调用前的两段播报不得拼进正文")
        self.assertEqual(
            [item["type"] for item in activity],
            ["tool", "prose"],
            "时间线与正文同口径：只剩工具状态 + 最终答复",
        )

    def test_content_empty_when_run_stopped_during_tool_step(self):
        # 取消正好发生在工具执行期间：最后一段是过程播报，不能冒充答复
        # （调用方 `_persist_aborted_message` 会兜底「（已中止）」）。
        from naiba.run.chat import ConversationRunMixin

        events = [
            ev("delta", content="正在验证", created_at=100),
            ev("tool_result", tool="pwsh", success=True, result="out", created_at=200),
        ]
        _reasoning, _runs, content, _activity = ConversationRunMixin._rebuild_partial_run(
            "run-2", events
        )
        self.assertEqual(content, "")


class FrontendProseFilterTests(unittest.TestCase):
    """前端渲染层必须与后端同口径。

    后端出参已过滤，但**历史消息**的 `metadata.activity` 早已带着十来段播报落库了——
    前端 `activityMarkup` 不再兜一次，打开老会话就永远显示旧形态（用户会认为"没修"）。
    """

    def test_activity_markup_renders_only_last_prose(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "public" / "js" / "03-media.js").read_text(encoding="utf-8")
        self.assertIn("export function activityMarkup", source)
        body = source.split("export function activityMarkup", 1)[1].split("\nexport function", 1)[0]
        self.assertIn("lastProseIndex", body, "activityMarkup 必须只渲染最后一段正文")
        self.assertIn(".filter(", body, "必须真的过滤掉更早的 prose 段")
        # 负向：不得退回"逐条无脑渲染"——旧形态（十句进度全渲染）正是它造成的。
        self.assertNotIn(
            "activity.forEach((item, index) => {", body,
            "不得直接遍历未过滤的 activity（会把过程播报全部渲染出来）",
        )


if __name__ == "__main__":
    unittest.main()
