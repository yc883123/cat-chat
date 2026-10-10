# -*- coding: utf-8 -*-
"""媒体采集器与消息级媒体汇总的守门（P2：生产点提取 + 分桶预截断 + 事件内嵌）。

冻结以下不变量：
1. **只对成功结果提取**——失败/待确认（NEED_CONFIRM）结果里的路径不是产物
   （错误信息常含路径），旧实现会渲染成指向不存在文件的破图卡片；
2. **声明驱动**——policy never / extract none 一律不提取；intent_gated 仅在
   run_context["media_intent"] 为真时放行；
3. **先截断再落盘**——候选按类型分桶（图 20 / 视频 8 / 音频 8）预截断后才复制/下载，
   否则列一次目录会把几百张图拷进 data 目录并生成几百张缩略图卡住；
   超限部分返回可自述的 truncated 信息（不静默）；
4. **记录可内嵌**——返回 {kind,name,source,thumb_path} 小型记录，随 tool_result 事件
   与 metadata.tool_runs 下发；model_visible_run 不得包含它（模型看不到宿主缓存路径）；
5. data_dir 每次调用实时解析（不闭包捕获装配期配置，教训 9/17）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.core.attachments import _image_intent, union_run_media  # noqa: E402
from naiba.core.contracts import EVENT_PAYLOAD_KEYS, RUN_CONTEXT_KEYS  # noqa: E402
from naiba.core.messages import MESSAGE_METADATA_KEYS, MetadataKeys  # noqa: E402
from naiba.core.media_types import MEDIA_BUCKET_LIMITS  # noqa: E402
from naiba.core.tool_results import display_tool_run, model_visible_run  # noqa: E402
from naiba.storage.media_collect import MediaCollector, _text_media_sources  # noqa: E402

# 1x1 PNG（真实图片，用于缩略图断言）
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f5b0000000049454e44ae426082"
)

INLINE = {"policy": "inline", "extract": "scan"}
STRUCTURED = {"policy": "inline", "extract": "structured"}
GATED = {"policy": "intent_gated", "extract": "scan"}


class _ConfigStub:
    """最小 ConfigView 桩：data_dir 可变（验证"每次调用实时解析"）。"""

    def __init__(self, data_dir: Path) -> None:
        self.data = {"imaging": {}}
        self._data_dir = data_dir

    def resolve_data_dir(self) -> Path:
        return self._data_dir

    def rebind(self, data_dir: Path) -> None:
        self._data_dir = data_dir


def _run(result: str = "", success: bool = True, tool: str = "write_file") -> dict:
    return {"tool": tool, "arguments": {}, "result": result, "success": success, "reason": ""}


class _TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.src = self.root / "src"
        self.src.mkdir(parents=True, exist_ok=True)
        self.config = _ConfigStub(self.data_dir)
        self.collector = MediaCollector(self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_png(self, name: str, payload: bytes = PNG_BYTES) -> Path:
        path = self.src / name
        path.write_bytes(payload)
        return path


class CollectGateTests(_TempCase):
    """提取门禁：成功、声明、意图。"""

    def test_failed_run_produces_no_media(self) -> None:
        target = self._make_png("a.png")
        result = f"FileNotFoundError: [Errno 2] No such file: '{target}'"
        self.assertEqual(self.collector.collect(_run(result, success=False), INLINE)["media"], [])

    def test_need_confirm_result_produces_no_media(self) -> None:
        result = f"NEED_CONFIRM:cid:读取工作区外路径：{self._make_png('b.png')}"
        self.assertEqual(self.collector.collect(_run(result), INLINE)["media"], [])

    def test_policy_never_and_extract_none(self) -> None:
        result = str(self._make_png("c.png"))
        self.assertEqual(self.collector.collect(_run(result), {"policy": "never", "extract": "scan"})["media"], [])
        self.assertEqual(self.collector.collect(_run(result), {"policy": "inline", "extract": "none"})["media"], [])

    def test_intent_gated_requires_intent(self) -> None:
        result = str(self._make_png("d.png"))
        declaration = {"policy": "intent_gated", "extract": "scan"}
        self.assertEqual(self.collector.collect(_run(result, tool="list_directory"), declaration)["media"], [])
        gated = self.collector.collect(_run(result, tool="list_directory"), declaration, intent=True)
        self.assertEqual(len(gated["media"]), 1)


class IntentGatedScopeTests(_TempCase):
    """intent_gated 附图范围（真实事故 2026-10 守门）：

    共享工作区里的历史旧图被目录枚举结果自动附出。声明丢失的根因修复之外，
    范围也要收紧——意图通过后只附**本轮枚举目录内**的媒体，偶然出现在
    结果文本里的其他路径（如工作区根的旧图）不采集。
    """

    GATED = {"policy": "intent_gated", "extract": "scan"}

    def test_scope_keeps_only_media_inside_enumerated_dir(self) -> None:
        inside = self._make_png("inside.png")
        outside_dir = self.root / "elsewhere"
        outside_dir.mkdir(exist_ok=True)
        outside = outside_dir / "outside.png"
        outside.write_bytes(PNG_BYTES + b"1")
        result = f"目录内容：\n{inside}\n{outside}"
        collected = self.collector.collect(
            _run(result, tool="list_directory"),
            self.GATED,
            intent=True,
            scope_dirs=[self.src],
        )
        self.assertEqual([r["name"] for r in collected["media"]], ["inside.png"])

    def test_scope_drops_urls(self) -> None:
        target = self._make_png("local.png")
        result = f"{target}\nhttp://127.0.0.1:8188/view?filename=remote.png&type=output"
        collected = self.collector.collect(
            _run(result, tool="list_directory"),
            self.GATED,
            intent=True,
            scope_dirs=[self.src],
        )
        self.assertEqual([r["name"] for r in collected["media"]], ["local.png"])

    def test_scope_all_outside_yields_no_media(self) -> None:
        outside = self.root / "old.png"
        outside.write_bytes(PNG_BYTES + b"2")
        collected = self.collector.collect(
            _run(f"目录内容：\n{outside}", tool="list_directory"),
            self.GATED,
            intent=True,
            scope_dirs=[self.src],
        )
        self.assertEqual(collected["media"], [], "范围外候选一个也不得附出")

    def test_no_scope_dirs_keeps_previous_behavior(self) -> None:
        """无范围信息（scope_dirs=None）维持原行为（测试/兼容路径）。"""
        target = self._make_png("any.png")
        collected = self.collector.collect(
            _run(str(target), tool="list_directory"), self.GATED, intent=True
        )
        self.assertEqual(len(collected["media"]), 1)

    def test_inline_policy_ignores_scope(self) -> None:
        """范围过滤只对 intent_gated 生效：inline（本轮真产物）不受影响。"""
        inside = self._make_png("in.png")
        outside = self.root / "out.png"
        outside.write_bytes(PNG_BYTES + b"3")
        collected = self.collector.collect(
            _run(f"已写入 {inside}\n另见 {outside}"), INLINE, scope_dirs=[self.src]
        )
        self.assertEqual(sorted(r["name"] for r in collected["media"]), ["in.png", "out.png"])

    def test_structured_rejects_non_json(self) -> None:
        result = f"已写入 {self._make_png('e.png')}（3 字符）"
        self.assertEqual(self.collector.collect(_run(result), STRUCTURED)["media"], [])

    def test_structured_reads_media_records(self) -> None:
        target = self._make_png("f.png")
        result = json.dumps({"note": "看图", "images": [{"name": "f.png", "path": str(target)}]})
        collected = self.collector.collect(_run(result, tool="vision_analyze"), STRUCTURED)
        self.assertEqual(len(collected["media"]), 1)
        record = collected["media"][0]
        self.assertEqual(record["kind"], "image")
        self.assertEqual(record["name"], "f.png")
        self.assertTrue(Path(record["source"]).is_file())
        self.assertTrue(Path(record["thumb_path"]).is_file(), "缓存后应生成缩略图")

    def test_scan_reads_prose_path(self) -> None:
        target = self._make_png("g.png")
        collected = self.collector.collect(_run(f"已写入 {target}（12 字符）"), INLINE)
        self.assertEqual([Path(r["name"]).name for r in collected["media"]], ["g.png"])


class CollectTruncationTests(_TempCase):
    """分桶预截断：先截断再落盘（宿主成本可控），超限可自述。"""

    def _many(self, kind: str, count: int) -> str:
        suffix = {"image": ".png", "video": ".mp4", "audio": ".mp3"}[kind]
        paths = []
        for index in range(count):
            path = self.src / f"{kind}_{index:03d}{suffix}"
            # 内容必须互不相同：内容级去重会合并同字节文件。
            path.write_bytes((PNG_BYTES if kind == "image" else b"x" * 16) + bytes([index]))
            paths.append(str(path))
        return json.dumps(paths)

    def test_images_truncated_before_caching(self) -> None:
        collected = self.collector.collect(_run(self._many("image", 25)), INLINE)
        limit = MEDIA_BUCKET_LIMITS["image"]
        self.assertEqual(len(collected["media"]), limit)
        generated = self.data_dir / "generated"
        written = [p for p in generated.glob("*.png")]
        self.assertEqual(len(written), limit, f"落盘文件数应等于分桶上限（实际 {len(written)}）")
        self.assertEqual(
            collected["truncated"],
            {"total": 25, "shown": limit, "kinds": {"image": {"total": 25, "shown": limit}}},
        )

    def test_video_and_audio_buckets(self) -> None:
        collected = self.collector.collect(_run(self._many("video", 10)), INLINE)
        self.assertEqual(len(collected["media"]), MEDIA_BUCKET_LIMITS["video"])
        collected = self.collector.collect(_run(self._many("audio", 10)), INLINE)
        self.assertEqual(len(collected["media"]), MEDIA_BUCKET_LIMITS["audio"])

    def test_no_truncation_reports_none(self) -> None:
        collected = self.collector.collect(_run(self._many("image", 3)), INLINE)
        self.assertEqual(len(collected["media"]), 3)
        self.assertIsNone(collected["truncated"])

    def test_content_duplicates_collapse(self) -> None:
        first = self._make_png("same_1.png")
        second = self._make_png("same_2.png")
        collected = self.collector.collect(_run(json.dumps([str(first), str(second)])), INLINE)
        self.assertEqual(len(collected["media"]), 1, "同字节文件应只保留一份")

    def test_data_dir_resolved_per_call(self) -> None:
        target = self._make_png("h.png")
        first = self.collector.collect(_run(str(target)), INLINE)["media"][0]
        # 采集器输出的是 resolve() 后的绝对路径；self.data_dir 在 CI runner 上可能是 8.3 短路径，
        # 因此两边都解析后再做 is_relative_to（该判断是纯词法比较）。
        self.assertTrue(
            Path(first["source"]).resolve().is_relative_to((self.data_dir / "generated").resolve())
        )
        other = self.root / "data2"
        other.mkdir(parents=True, exist_ok=True)
        self.config.rebind(other)
        second = self.collector.collect(_run(str(target)), INLINE)["media"][0]
        self.assertTrue(
            Path(second["source"]).resolve().is_relative_to((other / "generated").resolve()),
            "data_dir 必须每次调用实时解析（不得闭包捕获装配期值）",
        )


class GeneratedDedupeWriteGuardTests(_TempCase):
    """重复写入护栏：同一份内容再次采集时**不得**再整份写一遍盘。

    背景（实测）：generated 缓存里 138.8MB 中有 135.6MB 是"同一份内容、两个文件名"。
    旧实现即使内容已命中缓存，也先把来源完整写成 `.part` 临时文件、算完哈希发现重名才
    删掉——同一张图被多条消息引用一次就白写一遍整份字节。
    """

    def _generated_files(self) -> list[Path]:
        generated = self.data_dir / "generated"
        return sorted(p for p in generated.rglob("*") if p.is_file())

    def test_second_collect_of_same_bytes_does_not_write_again(self) -> None:
        first = self._make_png("same_a.png")
        collected = self.collector.collect(_run(str(first)), INLINE)["media"][0]
        cached = Path(collected["source"])
        self.assertTrue(cached.is_file())
        before = self._generated_files()
        before_mtimes = {p: p.stat().st_mtime_ns for p in before}

        # 同名同内容、但换了来源路径（真实场景：Desktop 上带时间戳的副本）再采集一次。
        second = self._make_png("same_b.png")
        again = self.collector.collect(_run(str(second)), INLINE)["media"][0]
        self.assertEqual(
            Path(again["source"]), cached, "同内容必须复用既有缓存文件（不得落第二份）"
        )
        after = self._generated_files()
        self.assertEqual(
            [p for p in after if p.suffix != ".webp"],
            [p for p in before if p.suffix != ".webp"],
            "重复采集不得新增主图文件",
        )
        for path, mtime in before_mtimes.items():
            if path.name.endswith(".webp"):
                continue
            self.assertEqual(
                path.stat().st_mtime_ns, mtime,
                f"同内容重复采集不得重写既有文件：{path.name}",
            )

    def test_no_partial_temp_files_left_behind(self) -> None:
        target = self._make_png("clean.png")
        self.collector.collect(_run(str(target)), INLINE)
        leftovers = [p for p in self._generated_files() if p.name.endswith(".part")]
        self.assertEqual(leftovers, [], "缓存写入必须原子收尾，不留 .part")

    def test_source_already_in_cache_skips_copy(self) -> None:
        """来源本身就在宿主缓存树内（generated/uploads）时，原位复用、不再复制。"""
        target = self._make_png("h.png")
        first = self.collector.collect(_run(str(target)), INLINE)["media"][0]
        cached = Path(first["source"])
        before = self._generated_files()

        again = self.collector.collect(_run(str(cached)), INLINE)["media"][0]
        self.assertEqual(Path(again["source"]), cached)
        self.assertEqual(
            self._generated_files(), before, "来源已在缓存树内时不得再复制一份"
        )
        self.assertTrue(Path(again["thumb_path"]).is_file(), "缩略图仍需可得")


class TextSourceBoundaryTests(unittest.TestCase):
    """纯文本来源的边界处理：URL 与 Windows 路径必须分开（真实事故回归）。

    事故：`job_wait` 返回「Job 已完成。{…"files":["http://127.0.0.1:8188/view?filename=a.png…"]}」，
    按"散文终止符"（含 `?`）截断会得到 `…/view` → 判定不是媒体 → **Job 完成后图片不显示**。
    """

    JOB_WAIT_RESULT = (
        "Job 已完成。{\"prompt_ids\": [\"8e34a71d\"], \"completed\": [{\"index\": 0, "
        "\"prompt_id\": \"8e34a71d\", \"files\": "
        "[\"http://127.0.0.1:8188/view?filename=lumine_cute_00001_.png&subfolder=&type=output\"]}], "
        "\"errors\": []}"
    )

    def test_comfyui_view_url_query_string_survives(self) -> None:
        sources = _text_media_sources(self.JOB_WAIT_RESULT)
        self.assertEqual(
            sources,
            ["http://127.0.0.1:8188/view?filename=lumine_cute_00001_.png&subfolder=&type=output"],
        )

    def test_url_trailing_punctuation_stripped(self) -> None:
        self.assertEqual(
            _text_media_sources("生成完成（见 http://127.0.0.1:8188/view?filename=a.png）"),
            ["http://127.0.0.1:8188/view?filename=a.png"],
        )
        self.assertEqual(
            _text_media_sources("见 https://example.com/a.png，谢谢"),
            ["https://example.com/a.png"],
        )

    def test_windows_path_prose_suffix_trimmed(self) -> None:
        sources = _text_media_sources("已写入 C:\\work\\a.png（12 字符）")
        self.assertEqual(sources, ["C:\\work\\a.png"])

    def test_url_without_media_ext_ignored(self) -> None:
        self.assertEqual(_text_media_sources("见 http://127.0.0.1:8188/view?filename=a.txt"), [])

    def test_collect_scan_keeps_comfyui_url_record(self) -> None:
        """端到端（不触网）：ComfyUI 不可达时保留原 URL 记录，而不是丢掉整张图。"""
        collector = MediaCollector(_ConfigStub(Path(tempfile.mkdtemp())))
        collected = collector.collect(
            {"tool": "job_wait", "result": self.JOB_WAIT_RESULT, "success": True},
            {"policy": "inline", "extract": "scan"},
        )
        self.assertEqual(len(collected["media"]), 1)
        self.assertEqual(collected["media"][0]["kind"], "image")
        self.assertEqual(collected["media"][0]["name"], "lumine_cute_00001_.png")


class ContentAddressedCacheTests(_TempCase):
    """缓存键必须是**内容**而不是来源（真实事故：ComfyUI 复用文件名 → 显示旧图）。"""

    @staticmethod
    def _png_bytes(color: tuple[int, int, int]) -> bytes:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (16, 16), color).save(buf, format="PNG")
        return buf.getvalue()

    def test_same_source_changed_content_is_not_stale(self) -> None:
        target = self.src / "lumine_cute_00002_.png"
        target.write_bytes(self._png_bytes((200, 30, 30)))
        first = self.collector.collect(_run(str(target)), INLINE)["media"][0]
        # ComfyUI 每次生成都复用同一文件名：路径/URL 不变，内容变了
        target.write_bytes(self._png_bytes((30, 30, 200)))
        second = self.collector.collect(_run(str(target)), INLINE)["media"][0]
        self.assertNotEqual(
            first["source"], second["source"],
            "同名同路径但内容变了，不能复用旧缓存（否则显示上一次的图）",
        )
        self.assertEqual(Path(second["source"]).read_bytes(), self._png_bytes((30, 30, 200)))
        self.assertTrue(Path(second["thumb_path"]).is_file())

    def test_repeated_reference_reuses_same_cache_entry(self) -> None:
        target = self.src / "same.png"
        target.write_bytes(self._png_bytes((10, 200, 10)))
        first = self.collector.collect(_run(str(target)), INLINE)["media"][0]
        second = self.collector.collect(_run(str(target)), INLINE)["media"][0]
        self.assertEqual(first["source"], second["source"], "同一来源且内容未变时应复用缓存")

    def test_same_content_different_name_shares_content_hash(self) -> None:
        """内容相同的不同文件名共用同一内容哈希前缀（内容寻址，与来源名无关）。"""
        payload = self._png_bytes((10, 200, 10))
        a = self.src / "a.png"
        b = self.src / "b.png"
        a.write_bytes(payload)
        b.write_bytes(payload)
        first = self.collector.collect(_run(str(a)), INLINE)["media"][0]
        second = self.collector.collect(_run(str(b)), INLINE)["media"][0]
        prefix = lambda record: Path(record["source"]).name.split("_", 1)[0]  # noqa: E731
        self.assertEqual(prefix(first), prefix(second))
        self.assertTrue(Path(first["source"]).is_file() and Path(second["source"]).is_file())


    def test_nonexistent_local_path_is_dropped(self) -> None:
        """本地路径不存在（如脚本 stdout 中文乱码、文件已被移动）→ 不产出记录。

        否则会给消息挂一张必然 403/404 的破图（用户实测：run_skill_script 打印的乱码
        路径被当媒体产物，前端渲染成破损图像）。
        """
        missing = self.src / "不存在的目录" / "ghost.png"
        collected = self.collector.collect(_run(f"moved a.png -> {missing}"), INLINE)
        self.assertEqual(collected["media"], [])

    def test_mojibake_path_is_dropped(self) -> None:
        """GBK 乱码路径（非 UTF-8 子进程输出被按 UTF-8 解码的产物）同样丢弃。"""
        mojibake = "C:\\Users\\user\\Desktop\\\ufffd\ufffd\u02b1\ufffd\\preview_images\\a.png"
        self.assertEqual(self.collector.collect(_run(f"moved a.png -> {mojibake}"), INLINE)["media"], [])


class UnionRunMediaTests(_TempCase):
    """消息级汇总：顺序、去重、分桶截断、旧数据兼容。"""

    def test_union_preserves_order_and_dedupes(self) -> None:
        runs = [
            {"media": [{"kind": "image", "name": "a.png", "source": "C:\\a.png", "thumb_path": ""}]},
            {"media": [
                {"kind": "image", "name": "b.png", "source": "C:\\b.png", "thumb_path": "C:\\b_thumb.webp"},
                {"kind": "image", "name": "a.png", "source": "C:\\a2.png", "thumb_path": "C:\\a2_thumb.webp"},
            ]},
            {"media": [{"kind": "video", "name": "v.mp4", "source": "C:\\v.mp4", "thumb_path": ""}]},
        ]
        media, truncated = union_run_media(runs)
        self.assertIsNone(truncated)
        self.assertEqual([row["name"] for row in media], ["a.png", "b.png", "v.mp4"])
        # 同名优先保留带缩略图的版本（避免"同图双份、其中一份破图"）
        self.assertEqual(media[0]["source"], "C:\\a2.png")

    def test_union_truncates_per_kind(self) -> None:
        runs = [{
            "media": [
                {"kind": "image", "name": f"i{index}.png", "source": f"C:\\i{index}.png", "thumb_path": ""}
                for index in range(30)
            ]
        }]
        media, truncated = union_run_media(runs)
        self.assertEqual(len(media), MEDIA_BUCKET_LIMITS["image"])
        self.assertEqual(truncated["total"], 30)
        self.assertEqual(truncated["shown"], MEDIA_BUCKET_LIMITS["image"])

    def test_legacy_runs_without_media(self) -> None:
        self.assertEqual(union_run_media([{"tool": "read_file", "result": "文本"}]), ([], None))
        self.assertEqual(union_run_media([]), ([], None))


class IntentTermTests(unittest.TestCase):
    """意图词表守门（2026-10-06 出卡修复）：媒体词补视频/音频，双词约束不变。

    修复前 `_IMAGE_MEDIA_TERM_RE` 只有图片词 ⇒ 「看看那个视频」判无意图，
    枚举类工具返回的视频/音频一律不出卡，出不出取决于话术里碰不碰巧带「图」字。
    """

    def test_video_audio_intents_hit(self) -> None:
        for text in (
            "看看那个视频",
            "把生成的 mp3 给我",
            "列出 D:/out 里的 mp4",
            "播放刚才生成的 wav",
            "试听一下这段 mp3",
            "那个 webm 显示一下",
            "看一下生成的图片",  # 图片词路径不得回归
        ):
            with self.subTest(text=text):
                self.assertTrue(_image_intent(text), f"应为媒体意图：{text}")

    def test_mention_without_action_is_not_intent(self) -> None:
        for text in (
            "我听说那个视频",  # 「听说」不含「听听/试听」——单字「听」不入动作词的原因
            "这个视频是谁做的",
            "那个 mp4 文件挺大的吧",
            "remove the old video file",  # 拉丁词必须有边界：mov 不得命中 remove
            "关于图片的说明文档",  # 只是提及，无动作词
        ):
            with self.subTest(text=text):
                self.assertFalse(_image_intent(text), f"不应判为媒体意图：{text}")


class ExtractionWideningTests(_TempCase):
    """提取面放宽守门：正斜杠路径、JSON 散文内嵌路径、result 不可变。"""

    def _make_mp4(self, name: str) -> Path:
        path = self.src / name
        path.write_bytes(b"fake mp4 bytes")
        return path

    def test_forward_slash_path_is_extracted(self) -> None:
        target = self._make_mp4("fwd.mp4")
        forward = str(target).replace("\\", "/")
        collected = self.collector.collect(_run(f"已保存到 {forward}（完成）"), INLINE)
        self.assertEqual(len(collected["media"]), 1, "正斜杠路径不得漏抓")
        self.assertEqual(collected["media"][0]["kind"], "video")

    def test_json_prose_path_extracted_in_scan_mode(self) -> None:
        target = self._make_mp4("prose.mp4")
        result = json.dumps({"msg": f"已保存到 {target}（完成）"}, ensure_ascii=False)
        collected = self.collector.collect(_run(result), INLINE)
        self.assertEqual(len(collected["media"]), 1, "JSON 散文里包的路径不得静默丢失")
        self.assertEqual(collected["media"][0]["kind"], "video")

    def test_structured_mode_still_rejects_prose(self) -> None:
        """structured 模式严格不回退文本扫描的既有契约不得被本次放宽破坏。"""
        target = self._make_mp4("strict.mp4")
        result = json.dumps({"msg": f"已保存到 {target}（完成）"}, ensure_ascii=False)
        self.assertEqual(self.collector.collect(_run(result), STRUCTURED)["media"], [])

    def test_collect_does_not_mutate_result(self) -> None:
        """提取只读不改写工具结果——wire 层字节稳定的根基（缓存命中验证第 1 层）。"""
        target = self._make_png("frozen.png")
        run = _run(f"已写入 {target}（12 字符）")
        snapshot = run["result"]
        self.collector.collect(run, INLINE)
        self.collector.collect(run, STRUCTURED)
        self.collector.collect(run, {"policy": "intent_gated", "extract": "scan"})
        self.assertEqual(run["result"], snapshot)


class AgentCollectorWiringTests(unittest.TestCase):
    """Agent 生产点接线：声明门禁、异常不中断工具结果。"""

    class _Registry:
        def __init__(self, declaration: dict) -> None:
            self.declaration = declaration
            self.calls: list[str] = []

        def media_declaration(self, name: str) -> dict:
            self.calls.append(name)
            return self.declaration

    class _Collector:
        def __init__(self, payload: dict | None = None, boom: bool = False) -> None:
            self.payload = payload or {"media": [], "truncated": None}
            self.boom = boom
            self.calls: list[tuple[str, dict, bool, object]] = []

        def collect(self, run, declaration, *, intent=False, scope_dirs=None):
            self.calls.append(
                (
                    str(run.get("tool")),
                    dict(declaration),
                    bool(intent),
                    [Path(p) for p in scope_dirs] if scope_dirs is not None else None,
                )
            )
            if self.boom:
                raise RuntimeError("采集失败（模拟）")
            return self.payload

    def _agent(self, collector):
        from naiba.skills.agent import SkillAgent

        return SkillAgent(catalog=None, executor=None, model_complete=None, media_collector=collector)

    def test_media_written_into_run_at_production_point(self) -> None:
        payload = {"media": [{"kind": "image", "name": "a.png", "source": "C:\\a.png", "thumb_path": ""}],
                   "truncated": {"total": 25, "shown": 20, "kinds": {}}}
        collector = self._Collector(payload)
        agent = self._agent(collector)
        run = _run("已写入 C:\\a.png（3 字符）")
        agent._collect_media(run, self._Registry(INLINE), {"media_intent": True})
        self.assertEqual(len(run["media"]), 1)
        self.assertEqual(run["media_truncated"]["shown"], 20)
        self.assertEqual(collector.calls[0][2], True, "intent 应取自 run_context.media_intent")

    def test_never_declaration_skips_collector(self) -> None:
        collector = self._Collector()
        agent = self._agent(collector)
        run = _run("C:\\a.png")
        agent._collect_media(run, self._Registry({"policy": "never", "extract": "none"}), {})
        self.assertEqual(collector.calls, [])
        self.assertNotIn("media", run)

    def test_collector_failure_keeps_tool_result(self) -> None:
        agent = self._agent(self._Collector(boom=True))
        run = _run("已写入 C:\\a.png（3 字符）")
        agent._collect_media(run, self._Registry(INLINE), {})
        self.assertNotIn("media", run, "采集异常不得污染/丢弃工具结果")
        self.assertEqual(run["result"], "已写入 C:\\a.png（3 字符）")

    def test_missing_collector_is_noop(self) -> None:
        agent = self._agent(None)
        run = _run("C:\\a.png")
        agent._collect_media(run, self._Registry(INLINE), {})
        self.assertNotIn("media", run)

    # ---- intent_gated 附图范围（真实事故 2026-10）：本轮枚举目录 ----

    def test_intent_gated_scope_from_absolute_path_arg(self) -> None:
        collector = self._Collector()
        agent = self._agent(collector)
        run = _run("nothing", tool="list_directory")
        run["arguments"] = {"path": str(ROOT / "naiba")}
        agent._collect_media(run, self._Registry(GATED), {
            "media_intent": True, "workspace_dir": str(ROOT),
        })
        self.assertEqual(collector.calls[0][3], [(ROOT / "naiba").resolve()])

    def test_intent_gated_scope_from_relative_path_arg(self) -> None:
        collector = self._Collector()
        agent = self._agent(collector)
        run = _run("nothing", tool="search_files")
        run["arguments"] = {"path": "sub/dir"}
        agent._collect_media(run, self._Registry(GATED), {
            "media_intent": True, "workspace_dir": str(ROOT),
        })
        self.assertEqual(collector.calls[0][3], [(ROOT / "sub" / "dir").resolve()])

    def test_intent_gated_scope_defaults_to_workspace(self) -> None:
        """无 path 参数（默认枚举工作区）→ 范围回落会话工作区。"""
        collector = self._Collector()
        agent = self._agent(collector)
        run = _run("nothing", tool="grep")
        run["arguments"] = {"pattern": "x"}
        agent._collect_media(run, self._Registry(GATED), {
            "media_intent": True, "workspace_dir": str(ROOT),
        })
        self.assertEqual(collector.calls[0][3], [ROOT.resolve()])

    def test_intent_gated_without_scope_info_passes_none(self) -> None:
        """无 path 且无工作区 → None（无范围信息，采集侧维持原行为）。"""
        collector = self._Collector()
        agent = self._agent(collector)
        run = _run("nothing", tool="list_directory")
        run["arguments"] = {}
        agent._collect_media(run, self._Registry(GATED), {"media_intent": True})
        self.assertIsNone(collector.calls[0][3])

    def test_inline_declaration_gets_no_scope(self) -> None:
        """inline（本轮真产物）不走范围过滤：scope_dirs 恒为 None。"""
        collector = self._Collector()
        agent = self._agent(collector)
        run = _run("已写入 C:\\a.png（3 字符）", tool="write_file")
        run["arguments"] = {"path": "C:\\a.png"}
        agent._collect_media(run, self._Registry(INLINE), {
            "media_intent": True, "workspace_dir": str(ROOT),
        })
        self.assertIsNone(collector.calls[0][3])


class MediaContractTests(unittest.TestCase):
    """契约登记：事件负载键 / metadata 键 / run_context 键 / 展示与模型可见的分离。"""

    def test_event_payload_keys_registered(self) -> None:
        keys = EVENT_PAYLOAD_KEYS["tool_result"]
        self.assertIsNotNone(keys)
        self.assertIn("media", keys)
        self.assertIn("media_truncated", keys)

    def test_metadata_key_registered(self) -> None:
        self.assertEqual(MetadataKeys.ATTACHMENTS_TRUNCATED, "attachments_truncated")
        self.assertIn("attachments_truncated", MESSAGE_METADATA_KEYS)

    def test_run_context_media_intent_registered(self) -> None:
        self.assertIn("media_intent", RUN_CONTEXT_KEYS)

    def test_display_run_carries_media_but_model_run_does_not(self) -> None:
        run = _run(
            json.dumps({"path": "C:\\a.png"}),
            tool="vision_image_ops",
        )
        run["media"] = [{"kind": "image", "name": "a.png", "source": "C:\\a.png", "thumb_path": ""}]
        run["media_truncated"] = {"total": 21, "shown": 20, "kinds": {}}
        display = display_tool_run(run)
        self.assertIn("media", display)
        self.assertIn("media_truncated", display)
        visible = model_visible_run(run)
        self.assertNotIn("media", visible)
        self.assertNotIn("media_truncated", visible)


class ToolRunEncodingTests(unittest.TestCase):
    """`run_skill_script` 子进程 stdio 必须 UTF-8（否则中文路径乱码 → 乱码路径变破图）。"""

    def test_frozen_entry_forces_utf8_stdio(self) -> None:
        try:
            import launcher
        except ImportError as exc:  # 桌面依赖缺失时跳过（CI/无 GUI 环境）
            self.skipTest(f"launcher 不可导入：{exc}")

        calls = []

        class _Stream:
            def reconfigure(self, **kwargs):
                calls.append(kwargs)

        launcher._force_utf8_stdio((_Stream(), _Stream()))
        self.assertEqual(len(calls), 2)
        for kwargs in calls:
            self.assertEqual(kwargs.get("encoding"), "utf-8")
            self.assertEqual(kwargs.get("errors"), "replace")

    def test_force_utf8_stdio_tolerates_none_stream(self) -> None:
        try:
            import launcher
        except ImportError as exc:
            self.skipTest(f"launcher 不可导入：{exc}")

        launcher._force_utf8_stdio((None,))  # 无控制台的 runw 下 sys.stdout 可能为 None


if __name__ == "__main__":
    unittest.main()
