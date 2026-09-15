# -*- coding: utf-8 -*-
"""视频抽帧工具守门：probe / 三种抽帧模式 / 联系表 / 幂等 / 两级缓存上限 / 缺依赖降级。

夹具：``tests/fixtures/sample_cut.mp4``（320x240、24 fps、6.0 秒；前 5 秒静止，
第 5.0 秒硬切镜）。**基准值硬编码**——CI 上没有 ffmpeg/ffprobe，测试不能去调 ffprobe；
改夹具必须同步改这里的数字（夹具可重复生成，见 tests/fixtures/make_sample_video.py）。

注意：本文件**不**通过任何"跳过"掩盖缺依赖——缺 av 是真实产品路径（冻结版漏装 →
抽帧报错），第 8 组用例专门守它。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from naiba import video as video_svc
from naiba.core.attachments import compose_user_content, upload_reference_lines
from naiba.mcp import MCPRegistry
from naiba.tools.providers import core as core_provider
from naiba.tools.providers.video import VideoToolProvider
from naiba.tools.registry import MEDIA_DECLARATIONS, build_tool_registry, build_video_tool_specs

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_cut.mp4"
# 夹具基准（本地用 PyAV 量取，硬编码进测试；CI 不依赖 ffprobe）
BASE_DURATION = 6.0
BASE_FPS = 24.0
BASE_WIDTH = 320
BASE_HEIGHT = 240
BASE_CODEC = "h264"
CUT_SECONDS = 5.0


def _ctx(workspace: Path) -> core_provider.ToolContext:
    return core_provider.ToolContext(
        workspace=workspace,
        python_executable=sys.executable,
        command_timeout=60,
        mcp_registry=MCPRegistry([]),
        mcp_register=None,
    )


class VideoFixtureTests(unittest.TestCase):
    """夹具本身的存在性校验：夹具丢了要给"重新生成"的明确指引，而不是无关断言失败。"""

    def test_fixture_present(self) -> None:
        self.assertTrue(FIXTURE.is_file(), f"缺少夹具视频：{FIXTURE}（重新生成见同目录 make_sample_video.py）")
        self.assertGreater(FIXTURE.stat().st_size, 1024)


class VideoServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-video-"))
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---- ① probe ----
    def test_probe_metadata_matches_baseline(self) -> None:
        info = video_svc.probe_video(FIXTURE)
        self.assertAlmostEqual(info["duration"], BASE_DURATION, delta=0.1)
        self.assertAlmostEqual(info["fps"], BASE_FPS, delta=0.01)
        self.assertEqual(info["width"], BASE_WIDTH)
        self.assertEqual(info["height"], BASE_HEIGHT)
        self.assertEqual(info["video_codec"], BASE_CODEC)
        self.assertFalse(info["has_audio"])
        self.assertEqual(info["audio_codec"], "")
        # 总帧数：有声明用声明值（估算标记为 False），无声明才估算并如实标注。
        self.assertGreater(info["frame_count"], 0)
        self.assertIsInstance(info["frame_count_estimated"], bool)
        if not info["frame_count_estimated"]:
            self.assertAlmostEqual(info["frame_count"], BASE_DURATION * BASE_FPS, delta=BASE_FPS)

    def test_probe_frame_count_estimated_when_not_declared(self) -> None:
        """容器没声明总帧数时必须估算并标注 frame_count_estimated=true（不得静默给估算值）。"""
        import types

        class _FakeStream:
            average_rate = 24
            base_rate = 24
            duration = 240          # 240 tick × (1/24) s = 10 秒
            time_base = 1 / 24
            frames = 0              # 很多容器没有 nb_frames

            class codec_context:  # noqa: N801 - 模拟 PyAV 的属性结构
                width, height, name = 320, 240, "h264"

        class _FakeContainer:
            def __init__(self) -> None:
                self.streams = types.SimpleNamespace(video=[_FakeStream()], audio=[])

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        fake_av = types.SimpleNamespace(open=lambda *_a, **_k: _FakeContainer())
        with mock.patch.object(video_svc, "_load_av", lambda: fake_av):
            info = video_svc.probe_video(FIXTURE)
        self.assertTrue(info["frame_count_estimated"])
        self.assertAlmostEqual(info["duration"], 10.0, delta=0.01)
        self.assertEqual(info["frame_count"], 240)  # 10 秒 × 24 fps

    # ---- ② times / interval ----
    def test_times_mode_hits_requested_seconds(self) -> None:
        result = video_svc.extract_frames(FIXTURE, self.data_dir, mode="times", times="0,3,5.5")
        self.assertEqual([frame["t"] for frame in result["frames"]], [0.0, 3.0, 5.5])
        self.assertEqual(result["count"], 3)
        self.assertFalse(result["truncated"])
        for frame in result["frames"]:
            self.assertTrue(Path(frame["path"]).is_file(), frame["path"])
            self.assertGreater(Path(frame["path"]).stat().st_size, 0)
            self.assertEqual((frame["width"], frame["height"]), (BASE_WIDTH, BASE_HEIGHT))
            self.assertIn("video_frames", frame["path"])

    def test_times_within_one_frame_tolerance(self) -> None:
        """时间点误差 ≤ 1 帧：抽出的画面内容必须真的来自该时间点。"""
        result = video_svc.extract_frames(
            FIXTURE, self.data_dir, mode="times", times=str(CUT_SECONDS + 0.5), contact_sheet=False
        )
        frame = result["frames"][0]
        # 切镜后是白底：中心像素应当是亮色（切镜前是深蓝底）。
        from PIL import Image

        image = Image.open(frame["path"]).convert("RGB")
        r, g, b = image.getpixel((BASE_WIDTH // 2, BASE_HEIGHT - 20))
        self.assertGreater(int(r) + int(g) + int(b), 600, f"t={frame['t']} 取到的不是切镜后画面")

    def test_interval_mode_count_and_range(self) -> None:
        result = video_svc.extract_frames(
            FIXTURE, self.data_dir, mode="interval", interval=1.0, contact_sheet=False
        )
        # 6 秒 / 1 秒 → 0..5（末点超出"最后一帧时间"被丢掉，避免标错时间的帧）
        self.assertEqual([frame["t"] for frame in result["frames"]], [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        bounded = video_svc.extract_frames(
            FIXTURE, self.data_dir, mode="interval", interval=2.0, start=1.0, end=4.0,
            contact_sheet=False,
        )
        self.assertEqual([frame["t"] for frame in bounded["frames"]], [1.0, 3.0])

    def test_interval_requires_positive_interval(self) -> None:
        with self.assertRaises(ValueError):
            video_svc.extract_frames(FIXTURE, self.data_dir, mode="interval", interval=0)

    # ---- ③ keyframes ----
    def test_keyframes_lands_on_scene_cut(self) -> None:
        result = video_svc.extract_frames(FIXTURE, self.data_dir, mode="keyframes", contact_sheet=False)
        times = [frame["t"] for frame in result["frames"]]
        self.assertTrue(times, "keyframes 模式没有选到任何帧")
        self.assertAlmostEqual(times[0], 0.0, delta=0.5)
        near_cut = [t for t in times if abs(t - CUT_SECONDS) <= 1.0 / video_svc.VIDEO_KEYFRAME_PROBE_FPS + 0.05]
        self.assertEqual(len(near_cut), 1, f"未恰好落在切点：{times}")
        # 静止段不应产生连发采样（切镜前只有首帧）
        self.assertEqual([t for t in times if t < CUT_SECONDS], [t for t in times if t < CUT_SECONDS][:1])

    def test_keyframes_threshold_one_selects_only_first(self) -> None:
        """threshold 拉到 1.0（最大）时只剩首个基准帧，阈值语义确实生效。"""
        result = video_svc.extract_frames(
            FIXTURE, self.data_dir, mode="keyframes", threshold=1.0, contact_sheet=False
        )
        self.assertEqual([frame["t"] for frame in result["frames"]], [0.0])

    # ---- 联系表 ----
    def test_contact_sheet_generated_within_max_edge(self) -> None:
        from PIL import Image

        result = video_svc.extract_frames(FIXTURE, self.data_dir, mode="interval", interval=1.0)
        sheet = Path(result["sheet"])
        self.assertTrue(sheet.is_file(), result["sheet"])
        self.assertIn("uploads", str(sheet))
        with Image.open(sheet) as image:
            self.assertLessEqual(max(image.size), video_svc.VIDEO_SHEET_MAX_EDGE)
        self.assertFalse(
            video_svc.extract_frames(
                FIXTURE, self.data_dir, mode="interval", interval=1.0, contact_sheet=False,
                start=0.0, end=3.0,
            )["sheet"]
        )

    # ---- ④ 幂等 ----
    def test_cache_idempotent_reuses_paths(self) -> None:
        first = video_svc.extract_frames(FIXTURE, self.data_dir, mode="times", times="1,2")
        second = video_svc.extract_frames(FIXTURE, self.data_dir, mode="times", times="1,2")
        self.assertEqual(
            [frame["path"] for frame in first["frames"]],
            [frame["path"] for frame in second["frames"]],
        )
        self.assertTrue(second.get("cached"), "同键第二次调用必须命中缓存")
        self.assertEqual(first["sheet"], second["sheet"])
        # 帧图带缩略图（与 PDF 页图同口径：产物在 uploads 内，采集器不会补缩略图 → 由服务层出）
        for frame in first["frames"]:
            self.assertTrue(Path(frame["thumb_path"]).is_file(), frame["thumb_path"])

    def test_different_params_get_different_dirs(self) -> None:
        first = video_svc.extract_frames(FIXTURE, self.data_dir, mode="times", times="1,2", contact_sheet=False)
        second = video_svc.extract_frames(FIXTURE, self.data_dir, mode="times", times="1,3", contact_sheet=False)
        self.assertNotEqual(Path(first["frames"][0]["path"]).parent, Path(second["frames"][0]["path"]).parent)

    # ---- ⑤ 错误语义 ----
    def test_missing_and_wrong_type_and_corrupt_files(self) -> None:
        probe = next(spec for spec in self._provider().tools() if spec.name == "probe_video")
        ok, out = probe.execute({"path": str(self.tmp / "nope.mp4")}, [], None)
        self.assertFalse(ok)
        self.assertIn("不存在", out)

        text = self.tmp / "note.txt"
        text.write_text("hello", encoding="utf-8")
        ok, out = probe.execute({"path": str(text)}, [], None)
        self.assertFalse(ok)
        self.assertIn("不是支持的视频文件", out)

        broken = self.tmp / "broken.mp4"
        broken.write_bytes(b"\x00\x01\x02not-a-video" * 40)
        ok, out = probe.execute({"path": str(broken)}, [], None)
        self.assertFalse(ok)
        self.assertIn("无法解析视频", out)
        # 抽帧侧同样给明确文案，而不是抛裸异常打断整轮
        frames = next(spec for spec in self._provider().tools() if spec.name == "extract_frames")
        ok, out = frames.execute({"path": str(broken)}, [], None)
        self.assertFalse(ok)
        self.assertIn("无法解析视频", out)

    def test_times_out_of_range_errors(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            video_svc.extract_frames(FIXTURE, self.data_dir, mode="times", times="999")
        self.assertIn("越界", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            video_svc.extract_frames(FIXTURE, self.data_dir, mode="times", times="")
        self.assertIn("times", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            video_svc.extract_frames(FIXTURE, self.data_dir, mode="nope")
        self.assertIn("mode 非法", str(ctx.exception))

    def test_max_hard_limit_errors(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            video_svc.extract_frames(FIXTURE, self.data_dir, max_count=video_svc.VIDEO_FRAME_HARD_MAX + 1)
        self.assertIn("≤", str(ctx.exception))

    # ---- ⑥ 截断 ----
    def test_truncated_flag_on_even_trim(self) -> None:
        result = video_svc.extract_frames(
            FIXTURE, self.data_dir, mode="interval", interval=0.2, contact_sheet=False
        )
        self.assertEqual(result["count"], video_svc.VIDEO_FRAME_DEFAULT_MAX)
        self.assertTrue(result["truncated"])

    # ---- ⑦ 两级缓存上限 ----
    def test_cache_limit_per_params_dir(self) -> None:
        with mock.patch.object(video_svc, "VIDEO_CACHE_MAX_IMAGES", 2):
            with self.assertRaises(ValueError) as ctx:
                video_svc.extract_frames(
                    FIXTURE, self.data_dir, mode="interval", interval=1.0, contact_sheet=False
                )
        self.assertIn("该视频这组抽帧参数", str(ctx.exception))

    def test_cache_limit_across_param_dirs(self) -> None:
        """参数一变就是新目录：只按目录计数拦不住总膨胀 → 视频级合计上限必须生效。"""
        with mock.patch.object(video_svc, "VIDEO_CACHE_MAX_PER_VIDEO", 4), \
                mock.patch.object(video_svc, "VIDEO_CACHE_MAX_IMAGES", 100):
            video_svc.extract_frames(FIXTURE, self.data_dir, mode="times", times="0,1", contact_sheet=False)
            video_svc.extract_frames(FIXTURE, self.data_dir, mode="times", times="2,3", contact_sheet=False)
            with self.assertRaises(ValueError) as ctx:
                video_svc.extract_frames(FIXTURE, self.data_dir, mode="times", times="4,5", contact_sheet=False)
        self.assertIn("合计", str(ctx.exception))

    # ---- ⑧ 缺解码依赖的降级 ----
    def test_missing_av_dependency_degrades_with_clear_message(self) -> None:
        with mock.patch.dict(sys.modules, {"av": None}):
            with self.assertRaises(ValueError) as ctx:
                video_svc.probe_video(FIXTURE)
            message = str(ctx.exception)
            self.assertIn("视频解码依赖未安装", message)
            self.assertIn("pip install av", message)
            with self.assertRaises(ValueError) as ctx2:
                video_svc.extract_frames(FIXTURE, self.data_dir)
            self.assertIn("视频解码依赖未安装", str(ctx2.exception))

    def test_missing_av_becomes_tool_failure_text(self) -> None:
        """缺依赖在工具边界必须变成"失败 + 明确文案"，而不是抛裸异常打断整轮。"""
        provider = self._provider()
        spec = next(spec for spec in provider.tools() if spec.name == "extract_frames")
        with mock.patch.dict(sys.modules, {"av": None}):
            ok, out = spec.execute({"path": str(FIXTURE)}, [], None)
        self.assertFalse(ok)
        self.assertIn("视频解码依赖未安装", out)

    # ---- 工具层 ----
    def _provider(self) -> VideoToolProvider:
        return VideoToolProvider(_ctx(self.tmp), lambda: self.data_dir)

    def test_provider_returns_json_with_media_paths(self) -> None:
        provider = self._provider()
        probe = next(spec for spec in provider.tools() if spec.name == "probe_video")
        ok, out = probe.execute({"path": str(FIXTURE)}, [], None)
        self.assertTrue(ok, out)
        self.assertAlmostEqual(json.loads(out)["duration"], BASE_DURATION, delta=0.1)

        frames = next(spec for spec in provider.tools() if spec.name == "extract_frames")
        ok, out = frames.execute(
            {"path": str(FIXTURE), "mode": "times", "times": "0,3"}, [], None
        )
        self.assertTrue(ok, out)
        payload = json.loads(out)
        self.assertEqual(len(payload["frames"]), 2)
        self.assertTrue(Path(payload["sheet"]).is_file())

    def test_provider_policy_binding(self) -> None:
        specs = {spec.name: spec for spec in self._provider().tools()}
        self.assertIsNotNone(specs["probe_video"].policy)
        self.assertIsNotNone(specs["extract_frames"].policy)
        # 抽帧只写宿主管控缓存 → 无确认
        self.assertEqual(
            specs["extract_frames"].policy("extract_frames", {}, [], "confirm", None, self.tmp), ""
        )
        # 读外部路径 → 越界必确认；工作区内免确认
        reason = specs["probe_video"].policy(
            "probe_video", {"path": str(FIXTURE)}, [], "confirm", None, self.tmp
        )
        self.assertIn("工作区外", reason)
        inside = self.tmp / "inside.mp4"
        inside.write_bytes(FIXTURE.read_bytes())
        self.assertEqual(
            specs["probe_video"].policy("probe_video", {"path": str(inside)}, [], "auto", None, self.tmp), ""
        )

    def test_extract_frames_retryable_and_timeout(self) -> None:
        spec = next(spec for spec in build_video_tool_specs() if spec.name == "extract_frames")
        self.assertTrue(spec.side_effect)
        self.assertTrue(spec.retryable)
        self.assertEqual(spec.timeout, 180)
        self.assertEqual(spec.permission, "auto")
        probe = next(spec for spec in build_video_tool_specs() if spec.name == "probe_video")
        self.assertFalse(probe.side_effect)
        self.assertEqual(probe.permission, "confirm")

    def test_mode_enum_matches_service(self) -> None:
        """schema 的 mode 枚举必须与 naiba.video.MODES 一致（避免两处名单漂移）。"""
        spec = next(spec for spec in build_video_tool_specs() if spec.name == "extract_frames")
        self.assertEqual(spec.parameters["properties"]["mode"]["enum"], list(video_svc.MODES))
        self.assertEqual(spec.parameters["properties"]["max"]["default"], video_svc.VIDEO_FRAME_DEFAULT_MAX)

    def test_media_declarations_registered(self) -> None:
        self.assertEqual(MEDIA_DECLARATIONS["probe_video"], {"policy": "never", "extract": "none"})
        self.assertEqual(MEDIA_DECLARATIONS["extract_frames"], {"policy": "inline", "extract": "structured"})
        registry = build_tool_registry()
        self.assertEqual(registry.media_declaration("probe_video"), {"policy": "never", "extract": "none"})
        self.assertEqual(
            registry.media_declaration("extract_frames"), {"policy": "inline", "extract": "structured"}
        )

    def test_tool_catalog_placement(self) -> None:
        """"读取与检索"分组、紧邻 PDF 三项之后（不进 order 表会掉到 index=999 的末尾）。"""
        from naiba.config import tool_catalog_entries

        catalog = tool_catalog_entries(build_tool_registry().schemas())
        names = [row["name"] for row in catalog]
        for name in ("probe_video", "extract_frames"):
            row = next(item for item in catalog if item["name"] == name)
            self.assertEqual(row["group"], "读取与检索")
            self.assertTrue(row["default_selected"], "视频工具已并入默认勾选（= 标准模式）")
        pdf_tail = names.index("pdf_zoom_region")
        self.assertEqual(names[pdf_tail + 1: pdf_tail + 3], ["probe_video", "extract_frames"])

    def test_default_selected_and_presets_include_new_tools(self) -> None:
        """视频两件套已并入默认勾选与预设：只读只收纯读取的 probe_video。"""
        from naiba.config import TOOL_PRESETS, _DEFAULT_SELECTED_TOOLS

        self.assertIn("probe_video", _DEFAULT_SELECTED_TOOLS)
        self.assertIn("extract_frames", _DEFAULT_SELECTED_TOOLS)
        includes = {preset["id"]: set(preset.get("include") or []) for preset in TOOL_PRESETS}
        self.assertIn("probe_video", includes["readonly"], "只读模式收纯读取的 probe_video")
        self.assertNotIn("extract_frames", includes["readonly"],
                         "抽帧会写产物文件，不进只读模式")
        for preset_id in ("standard", "longsession", "comfyui"):
            with self.subTest(preset=preset_id):
                self.assertIn("probe_video", includes[preset_id], preset_id)
                self.assertIn("extract_frames", includes[preset_id], preset_id)
        self.assertIn("group:*", includes["full"], "全能模式仍靠 group:* 覆盖全部工具")


class VideoMediaPipelineTests(unittest.TestCase):
    """产物挂回消息媒体区：抽帧结果经**真实 MediaCollector** 采集。

    这是"帧图能自动出现在消息里、可点开灯箱"的接线级守门（等价于 PDF 页图那条链路）：
    数量、类型、来源可服务性、缩略图齐备、不触发分桶截断。
    """

    def test_frames_collected_into_message_media(self) -> None:
        from types import SimpleNamespace

        from naiba.storage.media_collect import MediaCollector

        data_dir = Path(tempfile.mkdtemp(prefix="naiba-video-media-"))
        registry = build_tool_registry()
        provider = VideoToolProvider(_ctx(data_dir), lambda: data_dir)
        spec = next(item for item in provider.tools() if item.name == "extract_frames")
        # 各取切镜前后一帧（画面不同 → 不会被宿主的内容级去重合并）
        ok, out = spec.execute(
            {"path": str(FIXTURE), "mode": "times", "times": "0,5.5"}, [], None
        )
        self.assertTrue(ok, out)

        collector = MediaCollector(
            SimpleNamespace(resolve_data_dir=lambda: str(data_dir), data={"imaging": {}})
        )
        collected = collector.collect(
            {"tool": "extract_frames", "success": True, "result": out},
            registry.media_declaration("extract_frames"),
        )
        records = collected["media"]
        self.assertEqual(len(records), 3, records)  # 2 帧 + 1 张联系表
        self.assertIsNone(collected["truncated"], "默认 8 帧 + 联系表远低于图桶上限 20")
        for record in records:
            self.assertEqual(record["kind"], "image")
            self.assertTrue(Path(record["source"]).is_file(), record["source"])
            self.assertIn("uploads", str(record["source"]))
        # 帧图带显式 thumb_path；联系表是裸路径 → 前端按 <stem>_thumb.webp 推导，
        # 该文件必须真的存在（否则消息区出现破图）
        for record in records:
            thumb = Path(record["thumb_path"]) if record["thumb_path"] else Path(
                str(record["source"]).rsplit(".", 1)[0] + "_thumb.webp"
            )
            self.assertTrue(thumb.is_file(), f"{record['name']} 缺缩略图：{thumb}")

    def test_identical_frames_collapse_by_host_dedupe(self) -> None:
        """宿主按内容去重：画面完全相同的帧只留第一张（不是丢帧，是既有口径）。"""
        from types import SimpleNamespace

        from naiba.storage.media_collect import MediaCollector

        data_dir = Path(tempfile.mkdtemp(prefix="naiba-video-dedupe-"))
        registry = build_tool_registry()
        provider = VideoToolProvider(_ctx(data_dir), lambda: data_dir)
        spec = next(item for item in provider.tools() if item.name == "extract_frames")
        ok, out = spec.execute(
            {"path": str(FIXTURE), "mode": "times", "times": "0,1,2,3"}, [], None
        )
        self.assertTrue(ok, out)
        self.assertEqual(json.loads(out)["count"], 4, "工具结果本身仍返回全部帧路径")
        collector = MediaCollector(
            SimpleNamespace(resolve_data_dir=lambda: str(data_dir), data={"imaging": {}})
        )
        records = collector.collect(
            {"tool": "extract_frames", "success": True, "result": out},
            registry.media_declaration("extract_frames"),
        )["media"]
        frames = [record for record in records if "frame_" in str(record["name"])]
        self.assertLess(len(frames), 4, f"静止画面的重复帧必须被宿主去重合并：{records}")
        self.assertTrue(any("sheet_" in str(record["name"]) for record in records), records)

    def test_probe_result_yields_no_media(self) -> None:
        """probe_video 声明为 never/none：结果里的路径不是产物，不得挂成附件。"""
        from types import SimpleNamespace

        from naiba.storage.media_collect import MediaCollector

        data_dir = Path(tempfile.mkdtemp(prefix="naiba-video-probe-"))
        registry = build_tool_registry()
        provider = VideoToolProvider(_ctx(data_dir), lambda: data_dir)
        spec = next(item for item in provider.tools() if item.name == "probe_video")
        ok, out = spec.execute({"path": str(FIXTURE)}, [], None)
        self.assertTrue(ok, out)
        collector = MediaCollector(
            SimpleNamespace(resolve_data_dir=lambda: str(data_dir), data={"imaging": {}})
        )
        collected = collector.collect(
            {"tool": "probe_video", "success": True, "result": out},
            registry.media_declaration("probe_video"),
        )
        self.assertEqual(collected["media"], [])


class AttachmentReferenceTests(unittest.TestCase):
    """附件引用行：视频指引按工具集条件出现，且 pdf/video 互不串味。"""

    def _upload(self, path: str) -> list[dict[str, str]]:
        return [{"path": path}]

    def test_video_upload_gets_guidance(self) -> None:
        lines = upload_reference_lines(self._upload(r"C:\media\clip.mp4"))
        self.assertEqual(len(lines), 1)
        self.assertIn("probe_video", lines[0])
        self.assertIn("extract_frames", lines[0])
        self.assertIn("vision_analyze", lines[0])

    def test_video_guidance_suppressed_without_tools(self) -> None:
        line = upload_reference_lines(self._upload(r"C:\media\clip.mp4"), video_tools=False)[0]
        self.assertEqual(line, r"[用户上传文件：C:\media\clip.mp4]")

    def test_pdf_line_unchanged(self) -> None:
        with_tools = upload_reference_lines(self._upload(r"C:\doc\a.pdf"))[0]
        without = upload_reference_lines(self._upload(r"C:\doc\a.pdf"), pdf_tools=False)[0]
        self.assertIn("read_pdf", with_tools)
        self.assertEqual(without, r"[用户上传文件：C:\doc\a.pdf]")
        # 视频开关不得影响 PDF 行（逐字节一致契约）
        self.assertEqual(with_tools, upload_reference_lines(self._upload(r"C:\doc\a.pdf"), video_tools=False)[0])

    def test_other_files_plain_and_non_video_kinds_not_gated(self) -> None:
        self.assertEqual(
            upload_reference_lines(self._upload(r"C:\a\b.txt")), [r"[用户上传文件：C:\a\b.txt]"]
        )
        # 音频/图片不是视频：仍走普通行（不出视频指引）
        self.assertEqual(
            upload_reference_lines(self._upload(r"C:\a\b.mp3")), [r"[用户上传文件：C:\a\b.mp3]"]
        )

    def test_compose_user_content_passthrough(self) -> None:
        text = compose_user_content("看看这个", self._upload(r"C:\m\c.webm"))
        self.assertTrue(text.startswith("看看这个\n"))
        self.assertIn("probe_video", text)
        plain = compose_user_content("看看这个", self._upload(r"C:\m\c.webm"), video_tools=False)
        self.assertEqual(plain, "看看这个\n[用户上传文件：C:\\m\\c.webm]")


if __name__ == "__main__":
    unittest.main()
