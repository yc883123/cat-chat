# -*- coding: utf-8 -*-
"""护栏：「已附媒体卡」短注（模型可见的采集附卡事实）与缓存字节稳定性。

计划 2026-10-21（docs/plans/）定的三条硬契约：

1. 短注只按**实际采集记录**生成：media 为空（失败/拒绝/intent 未放行/离范围）
   绝不声称"已附卡"；注里只有类型与展示名，无宿主字段（source/thumb_path 等）；
2. 活轮与历史回放逐字节一致：同一 run 的 media 序列化恒等 ⇒
   role:tool 消息（trace 原样重放）与无 trace 的历史兜底重建（同一函数再生）
   都等于首次发送的尾部注，注只出现一次；
3. 一轮内同一来源只计一张卡：注按单次采集记录去重后的名单生成（采集器已按
   内容哈希去重），跨调用去重由 union_run_media 负责附件与前端卡。
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core.history import build_model_history  # noqa: E402
from naiba.core.tool_results import (  # noqa: E402
    MEDIA_NOTE_MAX_NAMES,
    display_tool_run,
    media_attached_note,
    model_visible_result,
    model_visible_run,
    truncate_json_text,
)

NOTE_MARK = "[媒体附卡]"
NOTE_SINGLE = "[媒体附卡] 本轮已附媒体卡 1 张：视频 01-09.mp4"


def _show_media_raw() -> str:
    return json.dumps({
        "ok": True, "status": "shown", "kind": "video",
        "name": "01-09.mp4", "source": r"C:\abs\01-09.mp4", "thumb_path": r"C:\abs\t.webp",
    }, ensure_ascii=False)


def _record(kind: str, name: str) -> dict:
    return {"kind": kind, "name": name, "source": f"C:\\abs\\{name}", "thumb_path": ""}


def _run(tool: str, raw: str, records: list[dict] | None, *, truncated=None, success=True) -> dict:
    run = {"tool": tool, "arguments": {}, "result": raw, "success": success, "reason": ""}
    if records is not None:
        run["media"] = records
    if truncated is not None:
        run["media_truncated"] = truncated
    return run


class MediaAttachedNoteTests(unittest.TestCase):
    """media_attached_note：形态固定、只含类型与名称、只认实际记录。"""

    def test_single_record_single_line(self) -> None:
        self.assertEqual(
            media_attached_note([_record("video", "01-09.mp4")]),
            "[媒体附卡] 本轮已附媒体卡 1 张：视频 01-09.mp4",
        )

    def test_multi_kind_entries_in_record_order(self) -> None:
        note = media_attached_note([
            _record("image", "a.png"), _record("image", "b.png"), _record("video", "c.mp4"),
        ])
        self.assertEqual(
            note,
            "[媒体附卡] 本轮已附媒体卡 3 张：图片 a.png、图片 b.png、视频 c.mp4",
        )

    def test_truncated_collection_states_total(self) -> None:
        records = [_record("image", f"n{index}.png") for index in range(20)]
        note = media_attached_note(records, {"total": 96, "shown": 20, "kinds": {}})
        self.assertIn("本轮已附媒体卡 20 张（共 96 张由结果触发，仅附前 20 张）", note)

    def test_names_capped_with_explicit_remainder(self) -> None:
        records = [_record("image", f"n{index}.png") for index in range(12)]
        note = media_attached_note(records)
        listed = note.split("：", 1)[1]
        self.assertIn("图片 n0.png", listed)
        self.assertIn(f"图片 n{MEDIA_NOTE_MAX_NAMES - 1}.png", listed)
        self.assertNotIn(f"图片 n{MEDIA_NOTE_MAX_NAMES}.png", listed)
        self.assertIn("仅列前 8 个名称，其余 4 张也已附卡", note)

    def test_empty_media_never_claims_attached(self) -> None:
        # 采集失败 / NEED_CONFIRM / intent 未放行 / 范围外最终都落在这些形态：
        # run 上要么根本没有 media、要么是空表（或全被落地校验丢弃）。
        self.assertEqual(media_attached_note([]), "")
        self.assertEqual(media_attached_note(None), "")
        self.assertEqual(media_attached_note(None, {"total": 5, "shown": 0, "kinds": {}}), "")
        self.assertEqual(media_attached_note([{"kind": "image", "name": "", "source": "C:\\x.png"}]), "")

    def test_note_excludes_host_fields(self) -> None:
        note = media_attached_note([_record("video", "01-09.mp4")])
        self.assertNotIn("C:\\", note)
        self.assertNotIn("thumb", note)

    def test_unknown_kind_label_fallback(self) -> None:
        self.assertIn("媒体 r.pdf", media_attached_note([{"kind": "document", "name": "r.pdf"}]))


class ModelVisibleNoteTests(unittest.TestCase):
    """model_visible_run：短注进 result 字符串末尾；机器字段仍被剥离。"""

    def test_note_appended_to_visible_result(self) -> None:
        run = _run("show_media", _show_media_raw(), [_record("video", "01-09.mp4")])
        raw_visible = model_visible_result("show_media", _show_media_raw())
        self.assertEqual(model_visible_run(run)["result"], raw_visible + "\n" + NOTE_SINGLE)

    def test_run_without_media_keeps_legacy_bytes(self) -> None:
        # 无 media 的 run（旧数据 / 不产媒体工具）：逐字节等于旧实现。
        self.assertEqual(model_visible_run(_run("read_file", "文件内容", None))["result"], "文件内容")

    def test_note_survives_truncation_marker(self) -> None:
        run = _run("pwsh", "x" * 40000, [_record("image", "a.png")])
        visible = model_visible_run(run)["result"]
        self.assertIn("已截断", visible)
        self.assertEqual(visible.rsplit("\n", 1)[1], "[媒体附卡] 本轮已附媒体卡 1 张：图片 a.png")

    def test_note_absent_after_failure_and_machine_fields_stripped(self) -> None:
        visible = model_visible_run(
            _run("show_media", _show_media_raw(), [_record("video", "01-09.mp4")], success=False)
        )["result"]
        self.assertNotIn(NOTE_MARK, visible)
        self.assertNotIn("C:\\abs", visible)
        self.assertNotIn("thumb", visible)

    def test_interface_keys_unchanged(self) -> None:
        self.assertEqual(
            set(model_visible_run(_run("show_media", _show_media_raw(), [_record("video", "01-09.mp4")])).keys()),
            {"tool", "success", "result"},
        )

    def test_deterministic_bytes(self) -> None:
        run = _run("pwsh", "行列清单", [_record("image", "a.png"), _record("image", "b.png")])
        self.assertEqual(
            json.dumps(model_visible_run(run), ensure_ascii=False, sort_keys=True),
            json.dumps(model_visible_run(run), ensure_ascii=False, sort_keys=True),
        )


def _live_role_tool_content(run: dict) -> str:
    """活轮 native 通道的 role:tool 正文（skills/agent 逐字使用的同一表达式）。"""
    return truncate_json_text(json.dumps(model_visible_run(run), ensure_ascii=False))


class NoteReplayStabilityTests(unittest.TestCase):
    """缓存字节契约：活轮与历史回放逐字节一致。"""

    def test_display_run_rebuilds_identical_note(self) -> None:
        # 无 trace 历史兜底路径：metadata.tool_runs 存展示 run（含 media），
        # 重放用它再生一份；活轮用原始 run。两条路必须同一字节。
        original = _run("vision_analyze", "装载结果文本", [_record("image", "a.png")])
        self.assertEqual(
            model_visible_run(display_tool_run(original))["result"],
            model_visible_run(original)["result"],
        )

    def test_rebuild_twice_is_idempotent(self) -> None:
        original = _run("vision_analyze", "装载结果文本", [_record("image", "a.png")])
        once = model_visible_run(display_tool_run(original))["result"]
        twice = model_visible_run(display_tool_run(display_tool_run(original)))["result"]
        self.assertEqual(once, twice)

    def test_content_read_fallback_replays_note_once_and_stable(self) -> None:
        # build_model_history 的无 trace 兜底：注只出现一次；同一会话重复构造逐字节一致。
        run = _run("vision_analyze", "装载结果文本", [_record("image", "a.png")])
        message = {
            "id": "m1", "role": "assistant", "content": "已识图。",
            "metadata": {"tool_runs": [display_tool_run(run)]},
        }
        first = build_model_history([message], image_encode_cache_mb=0)
        second = build_model_history([message], image_encode_cache_mb=0)
        self.assertEqual(first, second)
        blob = json.dumps(first, ensure_ascii=False)
        self.assertEqual(blob.count(NOTE_MARK), 1)
        self.assertIn("本轮已附媒体卡 1 张：图片 a.png", blob)
        self.assertNotIn("C:\\abs\\a.png", blob)

    def test_role_tool_content_matches_replay(self) -> None:
        # native 通道正文（活轮写入 trace 的字节）与无 trace 重建序列化后一致。
        original = _run("vision_analyze", "装载结果文本", [_record("image", "a.png")])
        self.assertEqual(
            _live_role_tool_content(original),
            json.dumps(model_visible_run(display_tool_run(original)), ensure_ascii=False),
        )

    def test_untrusted_channel_serializes_note(self) -> None:
        # 兼容通道：序列化里的注与单 run 形态一致（同一事实源）。
        runs_payload = json.dumps(
            [model_visible_run(_run("show_media", _show_media_raw(), [_record("video", "01-09.mp4")]))],
            ensure_ascii=False,
        )
        self.assertIn("本轮已附媒体卡 1 张：视频 01-09.mp4", runs_payload)
        self.assertNotIn("C:\\abs", runs_payload)


if __name__ == "__main__":
    unittest.main()
