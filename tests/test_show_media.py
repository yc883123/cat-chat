# -*- coding: utf-8 -*-
"""护栏：媒体「指名出卡」（show_media）——意图门、目标授权门、体积预检与链路一致性。

两条硬边界（计划 2026-10-20 §2.1），任一失败都不产生媒体记录、不出半张卡：

1. **用户意图门** `core/attachments.show_media_intent`：本轮用户原文里确实有「明确要求
   展示某个已有媒体」。否定、假设、引用/转述、仅提及媒体一律不放行；
2. **目标授权门** `core/attachments.resolve_show_media_target`：目标只能是
   (a) 当前会话已记录的媒体（`metadata.attachments` / `tool_runs[].media`），或
   (b) 本轮用户原文里明确给出的工作区内路径/文件名。不扫目录、不查其他会话、
   不凭 basename 猜路径，解析后必须仍在工作区内（符号链接按真实目标判定）。

本文件同时钉住"执行层不靠系统提示"这条承诺：即使模型误调工具、参数完全正确，
缺 RunContext 授权时也只回 `intent_required`，不产生 `run.media`。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.core.attachments import (  # noqa: E402
    SHOW_MEDIA_REASONS,
    build_show_media_candidates,
    extract_media_targets_from_text,
    media_candidates_from_messages,
    resolve_show_media_target,
    show_media_intent,
)
from naiba.core.media_types import MEDIA_SHOW_MAX_BYTES  # noqa: E402
from naiba.core.tool_results import model_visible_result  # noqa: E402
from naiba.plans import ReadOnlyToolExecutor  # noqa: E402
from naiba.subagent import SUBAGENT_BLOCKED_TOOLS  # noqa: E402


def _make_file(root: Path, rel: str, size: int = 32) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _windows_short_path(path: Path) -> Path:
    """取真实 8.3 路径；卷未启用短名时明确跳过，不能拿字符串替换冒充别名。"""
    if os.name != "nt":
        raise unittest.SkipTest("Windows 8.3 路径回归仅适用于 Windows")
    import ctypes
    from ctypes import wintypes

    get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_short_path.restype = wintypes.DWORD
    length = get_short_path(str(path), None, 0)
    if not length:
        raise unittest.SkipTest("当前卷无法查询 8.3 短路径")
    buffer = ctypes.create_unicode_buffer(length)
    if not get_short_path(str(path), buffer, length):
        raise unittest.SkipTest("当前卷无法取得 8.3 短路径")
    short = Path(buffer.value)
    if os.path.normcase(str(short)) == os.path.normcase(str(path)):
        raise unittest.SkipTest("当前卷未给临时媒体生成 8.3 别名")
    return short


class ShowMediaIntentTests(unittest.TestCase):
    """意图门：正例放行、反例一律不放行（规则表驱动，不用单一包含词当承诺）。"""

    POSITIVES = (
        "显示这个文件",
        "把刚生成的唯一视频放出来",
        "播放刚才那段音频",
        "把刚才那张图展示出来",
        "请把那个视频，展示出来",
        "把「刚才那张图」展示出来",
        "把 'just-created.png' 展示出来",
        "把 `just-created.png` 展示出来",
        "刚才那个视频给我看看",
        "看看 01-09.mp4",
        "看一下成品.png",
        "不要删掉这张图，把它展示出来",
        "不用再问我，请播放这个视频",
        "不要展示视频的大小，把视频展示出来",
    )
    NEGATIVES = (
        "这个视频不错",
        "这个视频讲了什么？",
        "看看 01-09.mp4 讲了什么",
        "看看 01-09.mp4 的内容",
        "询问一下视频的内容",
        "不要播放视频",
        "不要看看 01-09.mp4",
        "别把那张图放出来",
        "如果把视频放出来会怎样",
        "假如展示了那张图会怎么样",
        "他说“播放视频”",
        "他说播放视频",
        "听说你展示过那张图",
        "显示这个文件的路径",
        "显示视频的大小",
        "不要把刚才生成在那一轮会话中的视频展示出来",
        "不要把本轮任务生成的那个唯一的视频给我看",
        "不要把那张图，展示出来",
        "别把那个视频，给我播放出来",
        "不播放视频",
        "不能播放视频",
        "显示视频大小",
        "请显示这张图片的文件名",
        "显示视频的文件名",
        "显示图片 路径",
        "请解释 `播放视频` 的作用",
        "请解释 '播放视频' 的作用",
        "请解释“播放视频。”，并显示文件路径",
        "展示“视频”的路径",
        "显示“图片”的大小",
        "",
        "   ",
    )

    def test_positive_requests_pass(self) -> None:
        for text in self.POSITIVES:
            with self.subTest(text=text):
                self.assertTrue(show_media_intent(text), "明确的正向展示请求必须放行")

    def test_negative_requests_blocked(self) -> None:
        for text in self.NEGATIVES:
            with self.subTest(text=text):
                self.assertFalse(show_media_intent(text), "否定/假设/引用/仅提及不得放行")

    def test_quoted_action_does_not_count_but_quoted_target_does(self) -> None:
        """引号内的**动作词**不算请求；引号内的**路径**仍是合法目标。"""
        self.assertFalse(show_media_intent("他说“播放视频”"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = _make_file(root, "a.png")
            text = f"把「{target}」展示出来"
            self.assertTrue(show_media_intent(text), "引号只掩码动作、不掩码目标")
            targets = extract_media_targets_from_text(text, root)
            self.assertEqual([item["source"] for item in targets], [str(target.resolve())])


class ShowMediaTargetTests(unittest.TestCase):
    """目标门：候选来源、路径边界、唯一性与拒绝原因。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _candidates(self, *sources: str) -> list[dict]:
        return build_show_media_candidates(
            [{"role": "assistant", "metadata": {"attachments": [
                {"source": source, "name": Path(source).name} for source in sources
            ]}}],
            "",
            self.root,
        )

    # ---- 参数形态：先完成全部校验，不做部分展示 ----

    def test_argument_shapes_are_rejected_whole(self) -> None:
        cases = {
            "not-a-list": "not_found",
            "": "not_found",
        }
        for value, reason in cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    resolve_show_media_target(value, workspace=self.root)["reason"], reason
                )
        self.assertEqual(
            resolve_show_media_target(None, workspace=self.root)["reason"], "not_found"
        )
        self.assertEqual(
            resolve_show_media_target([123], workspace=self.root)["reason"], "not_found"
        )
        self.assertEqual(
            resolve_show_media_target(["a.png", "b.png"], workspace=self.root)["reason"],
            "over_limit",
        )
        # 重复项同样是「多目标」语义：拒绝，而不是去重后悄悄放行。
        self.assertEqual(
            resolve_show_media_target(["a.png", "a.png"], workspace=self.root)["reason"],
            "over_limit",
        )

    # ---- 会话已记录媒体（不需要原文提到路径） ----

    def test_session_recorded_media_is_authorized(self) -> None:
        image = _make_file(self.root, "out/a.png")
        result = resolve_show_media_target(
            [str(image)], workspace=self.root, candidates=self._candidates(str(image))
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "shown")
        self.assertEqual(result["kind"], "image")
        self.assertEqual(result["name"], "a.png")
        self.assertEqual(result["source"], str(image.resolve()))

    def test_candidate_deduped_across_attachments_and_tool_runs(self) -> None:
        image = _make_file(self.root, "a.png")
        pool = build_show_media_candidates(
            [{"role": "assistant", "metadata": {
                "attachments": [{"source": str(image), "name": "a.png"}],
                "tool_runs": [
                    {"media": [{"source": str(image), "name": "a.png"}]},
                    {"media": [{"source": str(image).upper(), "name": "A.PNG"}]},
                ],
            }}],
            "",
            self.root,
        )
        self.assertEqual(len(pool), 1, "同一来源（大小写不同写法）只保留一份")

    def test_recorded_windows_short_path_matches_canonical_source(self) -> None:
        image = _make_file(self.root, "long media directory/long-source-image-name.png")
        short = _windows_short_path(image)
        self.assertEqual(short.resolve(), image.resolve())
        for recorded, requested in ((short, image), (image, short), (short, short)):
            with self.subTest(recorded=recorded, requested=requested):
                result = resolve_show_media_target(
                    [str(requested)], workspace=self.root,
                    candidates=self._candidates(str(recorded)),
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["source"], str(image.resolve()))

    def test_windows_short_path_candidates_are_deduped(self) -> None:
        image = _make_file(self.root, "long-source-image-name.png")
        short = _windows_short_path(image)
        pool = self._candidates(str(short), str(image))
        self.assertEqual(len(pool), 1, "同一媒体的 8.3 别名不能变成两个歧义候选")
        result = resolve_show_media_target(
            ["刚才那张图"], workspace=self.root, candidates=pool,
        )
        self.assertTrue(result["ok"], result)

    def test_user_uploaded_media_counts_as_candidate(self) -> None:
        """用户上传的附件也是「会话已记录的媒体」——用户说「看刚才上传那张图」应当出卡。"""
        image = _make_file(self.root, "uploads/photo.png")
        pool = media_candidates_from_messages(
            [{"role": "user", "metadata": {"attachments": [{"source": str(image), "name": "photo.png"}]}}]
        )
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["kind"], "image")

    # ---- 用户原文里明确给出的路径 ----

    def test_explicit_workspace_paths_pass(self) -> None:
        image = _make_file(self.root, "sub/图1.png")
        root_image = _make_file(self.root, "根图.png")
        for text, value in (
            (f"把 {image} 展示出来", str(image)),
            ("把 sub/图1.png 展示出来", "sub/图1.png"),
            # 与中文粘在一起也必须切得出来（把<文件名>展示出来）
            (f"把{root_image.name}展示出来", root_image.name),
        ):
            with self.subTest(text=text):
                pool = build_show_media_candidates([], text, self.root)
                self.assertTrue(pool, "原文明确给出的工作区路径应进候选")
                result = resolve_show_media_target([value], workspace=self.root, candidates=pool)
                self.assertTrue(result["ok"], result)

    def test_authorization_requires_candidate_or_declared_path(self) -> None:
        image = _make_file(self.root, "a.png")
        # 文件真实存在，但既不是会话记录、也没在本轮原文里出现 → 拒绝（fail closed）。
        result = resolve_show_media_target(
            [str(image)], workspace=self.root, message_text="随便说点别的", candidates=[]
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_found")

    def test_declared_path_does_not_authorize_another_file_with_same_name(self) -> None:
        """用户指定的目录不存在或越界时，不得剥目录去授权工作区内的同名文件。"""
        _make_file(self.root, "secret.png")
        _make_file(self.root, "sub/secret.png")
        with tempfile.TemporaryDirectory() as other:
            outside = _make_file(Path(other), "secret.png")
            for raw in (
                "missing/secret.png",
                "missing/sub/secret.png",
                "../secret.png",
                str(outside),
                str(Path(other) / "missing" / "secret.png"),
            ):
                for original in (f"显示 {raw}", f"把{raw}展示出来"):
                    with self.subTest(original=original):
                        pool = build_show_media_candidates([], original, self.root)
                        self.assertEqual(pool, [], "完整路径不能退成 basename 或目录尾段")
                        for requested in ("secret.png", "sub/secret.png"):
                            result = resolve_show_media_target(
                                [requested], workspace=self.root, candidates=pool,
                            )
                            self.assertFalse(result["ok"], result)

    def test_cjk_prefix_preserves_relative_directory(self) -> None:
        image = _make_file(self.root, "中文目录/图1.png")
        pool = build_show_media_candidates([], "把中文目录/图1.png展示出来", self.root)
        self.assertEqual([item["source"] for item in pool], [str(image.resolve())])

    def test_unrecorded_outside_workspace_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            outside = _make_file(Path(other), "secret.png")
            result = resolve_show_media_target(
                [str(outside)], workspace=self.root,
                message_text=f"显示 {outside}",
                candidates=[],
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "outside_root")

    def test_recorded_outside_workspace_media_can_be_reused_by_exact_path(self) -> None:
        """会话可信记录可指向宿主 uploads/generated；完整路径仍需存在且通过大小预检。"""
        with tempfile.TemporaryDirectory() as other:
            outside = _make_file(Path(other), "uploaded.png")
            candidate = {"kind": "image", "name": "uploaded.png", "source": str(outside)}
            result = resolve_show_media_target(
                [str(outside)], workspace=self.root, candidates=[candidate],
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["source"], str(outside.resolve()))

            oversized = _make_file(Path(other), "huge.png", MEDIA_SHOW_MAX_BYTES + 1)
            result = resolve_show_media_target(
                [str(oversized)], workspace=self.root,
                candidates=[{"kind": "image", "name": oversized.name, "source": str(oversized)}],
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "too_large")

    def test_parent_traversal_rejected(self) -> None:
        """相对路径带 ``..`` 越界：解析后必须仍在工作区内。"""
        with tempfile.TemporaryDirectory() as other:
            sibling = _make_file(Path(other), "sneak.png")
            raw = f"../{Path(other).name}/sneak.png"
            result = resolve_show_media_target(
                [raw], workspace=self.root, message_text=f"显示 {raw}",
                candidates=[{"kind": "image", "name": "sneak.png", "source": str(sibling)}],
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "outside_root")

    def test_recorded_outside_short_path_matches_canonical_source(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            image = _make_file(Path(other), "long-source-image-name.png")
            short = _windows_short_path(image)
            for recorded, requested in ((short, image), (image, short)):
                with self.subTest(recorded=recorded, requested=requested):
                    result = resolve_show_media_target(
                        [str(requested)], workspace=self.root,
                        candidates=self._candidates(str(recorded)),
                    )
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["source"], str(image.resolve()))

    def test_recorded_outside_relative_path_rejected_when_cwd_is_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            outside = _make_file(Path(other), "recorded.png")
            raw = os.path.relpath(outside, self.root)
            previous = Path.cwd()
            try:
                os.chdir(self.root)
                result = resolve_show_media_target(
                    [raw], workspace=self.root, candidates=self._candidates(str(outside)),
                )
            finally:
                os.chdir(previous)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "outside_root")

    def test_symlink_escape_rejected(self) -> None:
        """越界链接按**真实目标**判定（符号链接 / 目录联接走的是同一条 ``resolve()`` 路径）。

        Windows 上文件符号链接需要开发者模式或管理员权限，普通环境建不出来；目录联接
        （junction）不需要提权，且 ``Path.resolve()`` 同样会跟踪它——用它覆盖同一条规则，
        建不出来时明确跳过而不是假装通过。
        """
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        _make_file(Path(outside_dir.name), "target.png")
        link = self.root / "linked"
        try:
            os.symlink(outside_dir.name, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            if os.name != "nt":
                self.skipTest("当前环境不支持创建符号链接")
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), outside_dir.name],
                capture_output=True, text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"当前环境无法创建目录联接：{created.stderr.strip()[:80]}")
            self.addCleanup(lambda: subprocess.run(
                ["cmd", "/c", "rmdir", str(link)], capture_output=True, text=True
            ))
        result = resolve_show_media_target(
            ["linked/target.png"], workspace=self.root,
            message_text="显示 linked/target.png",
            candidates=[],
        )
        self.assertFalse(result["ok"], "越界链接必须按真实目标判定为工作区外")
        self.assertEqual(result["reason"], "outside_root")

    def test_missing_directory_and_fake_extension(self) -> None:
        cases = {
            "missing.png": "not_found",
        }
        for value, reason in cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    resolve_show_media_target([value], workspace=self.root)["reason"], reason
                )
        (self.root / "folder.png").mkdir()
        self.assertEqual(
            resolve_show_media_target(["folder.png"], workspace=self.root)["reason"], "not_media"
        )
        fake = self.root / "a.png.exe"
        fake.write_bytes(b"x")
        self.assertEqual(
            resolve_show_media_target(["a.png.exe"], workspace=self.root)["reason"], "not_media"
        )
        text_file = self.root / "a.txt"
        text_file.write_bytes(b"x")
        self.assertEqual(
            resolve_show_media_target(["a.txt"], workspace=self.root)["reason"], "not_media"
        )

    # ---- 体积预检：只 stat，不读全文、不复制 ----

    def test_oversize_file_is_rejected_by_stat_only(self) -> None:
        big = self.root / "big.mp4"
        with big.open("wb") as handle:
            handle.seek(MEDIA_SHOW_MAX_BYTES)  # 稀疏文件：不实际写 80MB
            handle.write(b"\0")
        opened: list[str] = []
        original = Path.open

        def _spy(path_self, *args, **kwargs):
            opened.append(str(path_self))
            return original(path_self, *args, **kwargs)

        Path.open = _spy  # type: ignore[assignment]
        try:
            result = resolve_show_media_target(
                [str(big)], workspace=self.root,
                message_text=f"显示 {big}",
                candidates=[],
            )
        finally:
            Path.open = original  # type: ignore[assignment]
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "too_large")
        self.assertEqual(opened, [], "超限文件只能 stat，绝不能打开读取")

    def test_under_limit_passes(self) -> None:
        image = _make_file(self.root, "ok.png")
        result = resolve_show_media_target(
            [str(image)], workspace=self.root, candidates=self._candidates(str(image))
        )
        self.assertTrue(result["ok"], result)

    # ---- 代词/模糊指代：恰好一个才放行 ----

    def test_pronoun_unique_candidate_passes(self) -> None:
        video = _make_file(self.root, "v.mp4")
        pool = self._candidates(str(video))
        result = resolve_show_media_target(
            ["刚才那个视频"], workspace=self.root, message_text="把刚才那个视频放出来",
            candidates=pool,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "video")

    def test_pronoun_zero_candidates_not_found(self) -> None:
        result = resolve_show_media_target(
            ["刚才那段音频"], workspace=self.root, message_text="播放刚才那段音频", candidates=[]
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_found")

    def test_pronoun_multiple_candidates_ambiguous(self) -> None:
        first = _make_file(self.root, "v1.mp4")
        second = _make_file(self.root, "v2.mp4")
        pool = self._candidates(str(first), str(second))
        result = resolve_show_media_target(
            ["刚才那个视频"], workspace=self.root, message_text="把刚才那个视频放出来",
            candidates=pool,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "ambiguous")
        self.assertEqual(sorted(result["choices"]), ["v1.mp4", "v2.mp4"],
                         "歧义要回安全的候选展示名（供模型追问），不挑最新也不挑第一个")

    def test_same_basename_multiple_paths_ambiguous(self) -> None:
        one = _make_file(self.root, "a/x.png")
        two = _make_file(self.root, "b/x.png")
        pool = self._candidates(str(one), str(two))
        result = resolve_show_media_target(
            ["x.png"], workspace=self.root, message_text="显示 x.png", candidates=pool
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "ambiguous")

    def test_kind_filter_narrows_pronoun(self) -> None:
        image = _make_file(self.root, "a.png")
        video = _make_file(self.root, "b.mp4")
        pool = self._candidates(str(image), str(video))
        result = resolve_show_media_target(
            ["刚才那个视频"], workspace=self.root, message_text="播放刚才那个视频", candidates=pool
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["name"], "b.mp4")

    # ---- 模型把「用户那句话」当 paths 传进来（实测常见，2026-10-21 用户报障） ----

    def test_empty_paths_resolves_pronoun_from_user_text(self) -> None:
        """`[]` + 原文 → 按媒体类型与唯一性判定（模型说不出文件名时的正确用法）。"""
        image = _make_file(self.root, "a.png")
        pool = self._candidates(str(image))
        result = resolve_show_media_target(
            [], workspace=self.root, message_text="把刚才那张图展示出来", candidates=pool
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["name"], "a.png")

    def test_empty_paths_prefers_mentioned_media_type(self) -> None:
        image = _make_file(self.root, "a.png")
        video = _make_file(self.root, "b.mp4")
        pool = self._candidates(str(image), str(video))
        result = resolve_show_media_target(
            [], workspace=self.root, message_text="播放刚才那个视频", candidates=pool
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "video")

    def test_bare_pronoun_word_is_recognised_as_image(self) -> None:
        """裸「图」必须算图片词（「把刚才那张图展示出来」是最常见的说法）。"""
        image = _make_file(self.root, "a.png")
        video = _make_file(self.root, "b.mp4")
        pool = self._candidates(str(image), str(video))
        result = resolve_show_media_target(
            [], workspace=self.root, message_text="那张图放出来看看", candidates=pool
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "image")

    def test_sentence_in_paths_falls_back_to_text_resolution(self) -> None:
        """整句请求塞进 paths：按句子解析，而不是直接 not_found。"""
        image = _make_file(self.root, "out/a.png")
        pool = self._candidates(str(image))
        sentence = "把 out/a.png 展示出来"
        self.assertTrue(
            resolve_show_media_target(
                [sentence], workspace=self.root, message_text=sentence, candidates=pool
            )["ok"],
            "句子里写出的路径（已在候选中）应当能出卡",
        )
        pronoun = "把刚才那张图展示出来"
        self.assertTrue(
            resolve_show_media_target(
                [pronoun], workspace=self.root, message_text=pronoun, candidates=pool
            )["ok"],
            "整句代词请求也应退到候选判定",
        )

    def test_sentence_in_paths_does_not_widen_authorization(self) -> None:
        """句子里的路径若**不在候选里**，仍不得出卡（句子不构成新的授权来源）。"""
        _make_file(self.root, "out/a.png")
        with tempfile.TemporaryDirectory() as other:
            outsider = _make_file(Path(other), "secret.png")
            sentence = f"把 {outsider} 展示出来"
            result = resolve_show_media_target(
                [sentence], workspace=self.root, message_text=sentence, candidates=[]
            )
        self.assertFalse(result["ok"])
        self.assertIn(result["reason"], {"not_found", "ambiguous"})
        self.assertNotIn(str(outsider), json.dumps(result, ensure_ascii=False))

    # ---- URL 形态：只认会话里已记录的同串来源 ----

    def test_url_candidate_must_be_recorded(self) -> None:
        url = "http://127.0.0.1:8188/view?filename=a.png&type=output"
        record = {"kind": "image", "name": "a.png", "source": url, "thumb_path": "C:\\t.webp"}
        ok = resolve_show_media_target([url], workspace=self.root, candidates=[record])
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["thumb_path"], "C:\\t.webp")
        self.assertFalse(
            resolve_show_media_target([url], workspace=self.root, candidates=[])["ok"],
            "没记录过的 URL 不得出卡（不联网、不猜地址）",
        )

    def test_reason_enum_is_closed(self) -> None:
        """所有拒绝原因必须落在稳定枚举里（前端/提示词按它分支）。"""
        for value in ([], [""], ["a", "b"], [123], None, "x"):
            with self.subTest(value=value):
                payload = resolve_show_media_target(value, workspace=self.root)
                self.assertEqual(payload["status"], "skipped")
                self.assertIn(payload["reason"], SHOW_MEDIA_REASONS)


class ShowMediaRunStateTests(unittest.TestCase):
    """run/chat 装配：只有普通主对话 Run 才置位两枚键。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = _make_file(self.root, "a.png")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _messages(self) -> list[dict]:
        return [{"id": "m1", "role": "assistant", "metadata": {
            "attachments": [{"source": str(self.image), "name": "a.png"}],
        }}]

    def test_main_conversation_gets_authorization(self) -> None:
        from naiba.run.chat import show_media_run_state

        enabled, candidates = show_media_run_state(
            message="把刚才那张图展示出来", mode="craft", conv_kind="main",
            messages=self._messages(), workspace=str(self.root),
        )
        self.assertTrue(enabled)
        self.assertEqual([item["source"] for item in candidates], [str(self.image)])

    def test_aux_plan_and_unstated_turns_are_false(self) -> None:
        from naiba.run.chat import show_media_run_state

        for kwargs in (
            {"message": "把刚才那张图展示出来", "mode": "craft", "conv_kind": "aux"},
            {"message": "把刚才那张图展示出来", "mode": "plan", "conv_kind": "main"},
            {"message": "今天天气不错", "mode": "craft", "conv_kind": "main"},
        ):
            with self.subTest(**kwargs):
                enabled, candidates = show_media_run_state(
                    messages=self._messages(), workspace=str(self.root), **kwargs
                )
                self.assertFalse(enabled)
                self.assertEqual(candidates, [])


class ShowMediaToolTests(unittest.TestCase):
    """执行层：两道门都在 execute 内，失败安全（不靠系统提示劝模型别用）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = _make_file(self.root, "a.png")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _execute(self, arguments, run_context):
        from naiba.tools.providers.core import CoreToolProvider, ToolContext

        registry = __import__("naiba.tools.registry", fromlist=["build_tool_registry"]).build_tool_registry()
        registry.register_provider(
            CoreToolProvider(
                ToolContext(
                    workspace=self.root,
                    python_executable=sys.executable,
                    command_timeout=60,
                    mcp_registry=None,
                )
            )
        )
        spec = registry.get("show_media")
        return spec.execute(arguments, [], run_context)

    def test_missing_intent_returns_intent_required(self) -> None:
        ok, result = self._execute(
            {"paths": [str(self.image)]},
            {"show_media_intent": False, "show_media_candidates": [], "workspace_dir": str(self.root)},
        )
        self.assertFalse(ok)
        payload = json.loads(result)
        self.assertEqual(payload["reason"], "intent_required")
        self.assertNotIn("source", payload)

    def test_authorized_call_returns_structured_record(self) -> None:
        ok, result = self._execute(
            {"paths": [str(self.image)]},
            {
                "show_media_intent": True,
                "workspace_dir": str(self.root),
                "show_media_candidates": [
                    {"kind": "image", "name": "a.png", "source": str(self.image), "thumb_path": ""}
                ],
            },
        )
        self.assertTrue(ok)
        payload = json.loads(result)
        self.assertEqual(payload["status"], "shown")
        self.assertEqual(payload["source"], str(self.image.resolve()))
        self.assertEqual(payload["thumb_path"], "")

    def test_policy_never_asks_for_confirmation(self) -> None:
        from naiba.tools.providers.core import _make_core_policy, ToolContext

        policy = _make_core_policy(
            ToolContext(workspace=self.root, python_executable=sys.executable,
                        command_timeout=60, mcp_registry=None),
            "show_media",
        )
        self.assertEqual(policy("show_media", {}, [], "confirm", {}, self.root), "",
                         "def 级策略必须免确认：真正的门在 execute 内")

    def test_registry_declares_structured_media(self) -> None:
        from naiba.tools.registry import MEDIA_DECLARATIONS, build_tool_registry

        registry = build_tool_registry()
        self.assertEqual(
            registry.media_declaration("show_media"),
            {"policy": "inline", "extract": "structured"},
        )
        self.assertEqual(MEDIA_DECLARATIONS["show_media"]["extract"], "structured",
                         "show_media 的 source 是宿主凭据，不能走文本扫描兜底")
        self.assertFalse(registry.side_effect("show_media") is False)
        self.assertEqual(registry.permission("show_media"), "auto")
        self.assertFalse(registry.retryable("show_media"))


class ShowMediaModelVisibilityTests(unittest.TestCase):
    """模型可见结果脱敏：只留状态/原因/类型/展示名，剥掉 source / thumb_path / 绝对路径。"""

    def test_model_result_strips_paths(self) -> None:
        raw = json.dumps({
            "ok": True, "status": "shown", "kind": "image",
            "name": "a.png", "source": "C:\\work\\a.png", "thumb_path": "C:\\cache\\a.webp",
        }, ensure_ascii=False)
        visible = json.loads(model_visible_result("show_media", raw))
        self.assertEqual(visible["status"], "shown")
        self.assertEqual(visible["name"], "a.png")
        self.assertEqual(visible["kind"], "image")
        self.assertNotIn("source", visible)
        self.assertNotIn("thumb_path", visible)
        self.assertNotIn("C:\\", model_visible_result("show_media", raw))

    def test_model_result_keeps_safe_choices(self) -> None:
        raw = json.dumps({
            "ok": False, "status": "skipped", "reason": "ambiguous", "name": "",
            "choices": ["v1.mp4", "v2.mp4"],
        }, ensure_ascii=False)
        visible = json.loads(model_visible_result("show_media", raw))
        self.assertEqual(visible["reason"], "ambiguous")
        self.assertEqual(visible["choices"], ["v1.mp4", "v2.mp4"])

    def test_non_json_result_is_kept(self) -> None:
        self.assertEqual(model_visible_result("show_media", "纯文本"), "纯文本")


class ShowMediaScopeTests(unittest.TestCase):
    """可见范围：不进只读白名单、不进 Plan/子 Agent、不进通用预设。"""

    def test_not_in_readonly_sets(self) -> None:
        from naiba.plans import READONLY_TOOLS
        from naiba.run.session import SYSTEM_TOOLS_CRAFT, SYSTEM_TOOLS_READONLY

        self.assertNotIn("show_media", READONLY_TOOLS)
        self.assertNotIn("show_media", SYSTEM_TOOLS_READONLY)
        self.assertNotIn("show_media", SYSTEM_TOOLS_CRAFT)

    def test_plan_execution_filters_show_media(self) -> None:
        source = (ROOT / "naiba/plans.py").read_text(encoding="utf-8")
        self.assertIn('if str(item) != "show_media"', source,
                      "Plan 执行路径必须显式过滤 show_media（不进入 allowed_tools）")

    def test_readonly_executor_blocks_it(self) -> None:
        self.assertIn("show_media", ReadOnlyToolExecutor.BLOCKED_TOOLS,
                      "辅助对话 / Plan 准备的最后防线")

    def test_subagent_blocks_it(self) -> None:
        self.assertIn("show_media", SUBAGENT_BLOCKED_TOOLS,
                      "子 Agent / 后台 Job 的 RunContext 不置位意图，显式挡住")

    def test_plan_mode_resolution_drops_it(self) -> None:
        from types import SimpleNamespace

        from naiba.run.session import resolve_allowed_tools
        from naiba.tools.registry import build_tool_registry

        app = SimpleNamespace(
            config=SimpleNamespace(data={"agent_tools": ["read_file"]}),
            tool_registry=build_tool_registry(),
            web_search=SimpleNamespace(is_available=lambda: False),
        )
        tools = resolve_allowed_tools(
            app, "plan", {}, False, "", ["read_file", "show_media"], "main"
        )
        self.assertNotIn("show_media", tools)
        self.assertIn("read_file", tools)

    def test_tool_description_budget(self) -> None:
        """描述 ≤100 字 / ≤3 句，且不写跨工具编排（编排规则在系统提示）。"""
        from naiba.tools.registry import build_tool_registry

        row = next(r for r in build_tool_registry().schemas() if r["name"] == "show_media")
        description = str(row["description"])
        self.assertLessEqual(len(description), 100, description)
        self.assertLessEqual(len([s for s in description.split("。") if s.strip()]), 3)
        for phrase in ("先用", "随后用", "然后用", "必须先", "应该先"):
            self.assertNotIn(phrase, description)

    def test_run_context_keys_registered(self) -> None:
        from naiba.core.contracts import RUN_CONTEXT_KEYS, default_run_context

        for key in ("show_media_intent", "show_media_candidates", "show_media_message"):
            with self.subTest(key=key):
                self.assertIn(key, RUN_CONTEXT_KEYS)
        defaults = default_run_context()
        self.assertIs(defaults["show_media_intent"], False,
                      "缺省必须拒绝出卡（忘装配退化成不出卡，而不是随便出卡）")
        self.assertEqual(defaults["show_media_candidates"], [])
        self.assertEqual(defaults["show_media_message"], "",
                         "缺省没有用户原文 ⇒ 代词无法解析 ⇒ 只会回 ambiguous/not_found，不会乱出卡")


class _EmptyCatalog:
    """空 Skill 目录桩：SkillAgent 只要求 `scan()` 可调用。"""

    def scan(self):
        return []


class _MediaRegistry:
    """窄注册表桩：只暴露一个 def（execute/sub/声明），媒体走真实声明口径。"""

    def __init__(self, spec, declaration):
        self._spec = spec
        self._declaration = dict(declaration)
        self.calls = []

    def get(self, name):
        return self._spec if name == self._spec.name else None

    def schemas(self):
        return [{
            "name": self._spec.name,
            "description": self._spec.description,
            "parameters": self._spec.parameters,
        }]

    def side_effect(self, name):
        return self._spec.side_effect

    def retryable(self, name):
        return self._spec.retryable

    def media_declaration(self, name):
        return dict(self._declaration)

    def execute(self, tool, arguments, active, run_context=None):
        self.calls.append(tool)
        return self._spec.execute(arguments, active, run_context)


class _RecordingCollector:
    """记录调用的采集器桩：返回**真实结构**的媒体记录（模仿 MediaCollector）。"""

    def __init__(self):
        self.calls = []

    def collect(self, run, declaration, *, intent=False, scope_dirs=None):
        self.calls.append((dict(run), dict(declaration), bool(intent), scope_dirs))
        payload = json.loads(str(run.get("result") or "{}"))
        if not payload.get("ok"):
            return {"media": [], "truncated": None}
        return {"media": [{
            "kind": str(payload.get("kind") or ""),
            "name": str(payload.get("name") or ""),
            "source": str(payload.get("source") or ""),
            "thumb_path": str(payload.get("thumb_path") or ""),
        }], "truncated": None}


class ShowMediaAgentLoopTests(unittest.TestCase):
    """真实 Agent 循环：模型发出 show_media 调用 → run.media 产生记录 → 消息附件含目标。

    §7.6「仅检查配置/API 返回不算验收」：这里走 `SkillAgent.run` 的真执行链路
    （工具分发 → 执行器 → 采集器 → run["media"] → union_run_media），
    不使用桩替掉 show_media 的执行体。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = _make_file(self.root, "out/成品.png")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _show_media_spec(self):
        from naiba.mcp import MCPRegistry
        from naiba.tools.providers.core import CoreToolProvider, ToolContext

        provider = CoreToolProvider(ToolContext(
            workspace=self.root,
            python_executable=sys.executable,
            command_timeout=30,
            mcp_registry=MCPRegistry([]),
            mcp_register=None,
        ))
        return next(item for item in provider.tools() if item.name == "show_media")

    def _run(self, *, intent: bool, candidates: list[dict]) -> tuple[list[dict], Any]:
        from naiba.skills.agent import SkillAgent
        from naiba.tools.registry import MEDIA_DECLARATIONS
        from tests.tool_testkit import wired_executor

        spec = self._show_media_spec()
        registry = _MediaRegistry(spec, MEDIA_DECLARATIONS["show_media"])
        executor = wired_executor(self.root, mode="auto")
        executor.set_def_resolver(registry.get)
        collector = _RecordingCollector()
        action = json.dumps({
            "type": "tool",
            "tool": "show_media",
            "arguments": {"paths": [str(self.image)]},
        }, ensure_ascii=False)
        replies = [action, "已经展示好了。"]
        state = {"index": 0}

        def complete(_profile, _messages, _options, _event):
            index = min(state["index"], len(replies) - 1)
            state["index"] += 1
            return replies[index]

        worker = SkillAgent(_EmptyCatalog(), executor, complete, collector)
        _response, runs, _reasonings, _usage = worker.run(
            "把刚才那张图展示出来",
            [],
            {"kind": "local", "model": "m", "context_window": 32768},
            {"stream": False, "max_tokens": 512, "max_steps": 3},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["show_media"],
            lambda _payload: None,
            None,
            tool_registry=registry,
            run_context={
                "run_id": "run-show-media",
                "conversation_id": "conv-show-media",
                "executor": executor,
                "workspace_dir": str(self.root),
                "confirmation_ui": True,
                "show_media_intent": intent,
                "show_media_candidates": candidates,
            },
        )
        return runs, collector

    def test_authorized_turn_produces_media_record(self) -> None:
        runs, collector = self._run(
            intent=True,
            candidates=[{"kind": "image", "name": self.image.name,
                         "source": str(self.image), "thumb_path": ""}],
        )
        target_runs = [run for run in runs if run.get("tool") == "show_media"]
        self.assertEqual(len(target_runs), 1, "模型那一次调用要被真的执行到")
        self.assertTrue(target_runs[0]["success"], target_runs[0]["result"])
        self.assertEqual(len(collector.calls), 1, "采集器只在工具产出点被调用一次")
        declaration = collector.calls[0][1]
        self.assertEqual(declaration, {"policy": "inline", "extract": "structured"},
                         "show_media 必须按 inline/structured 声明采集")
        from naiba.core.attachments import union_run_media

        attachments, truncated = union_run_media(runs)
        self.assertIsNone(truncated)
        self.assertEqual([item["source"] for item in attachments], [str(self.image.resolve())],
                         "消息级 attachments 必须含目标（前端据此出卡）")
        self.assertEqual(attachments[0]["kind"], "image")

    def test_without_intent_no_media_record(self) -> None:
        runs, collector = self._run(
            intent=False,
            candidates=[{"kind": "image", "name": self.image.name,
                         "source": str(self.image), "thumb_path": ""}],
        )
        target_runs = [run for run in runs if run.get("tool") == "show_media"]
        self.assertEqual(len(target_runs), 1)
        self.assertFalse(target_runs[0]["success"], "缺意图授权必须失败")
        self.assertEqual(json.loads(target_runs[0]["result"])["reason"], "intent_required")
        from naiba.core.attachments import union_run_media

        attachments, _ = union_run_media(runs)
        self.assertEqual(attachments, [], "误调保护：不得产生任何媒体记录")

    def test_path_mismatch_produces_no_card(self) -> None:
        """构造了完全匹配的路径、但候选集里没有它 ⇒ 仍然不出卡（不做半张卡）。"""
        other_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(other_root, ignore_errors=True))
        outsider = _make_file(other_root, "secret.png")
        from naiba.skills.agent import SkillAgent
        from naiba.tools.registry import MEDIA_DECLARATIONS
        from tests.tool_testkit import wired_executor

        spec = self._show_media_spec()
        registry = _MediaRegistry(spec, MEDIA_DECLARATIONS["show_media"])
        executor = wired_executor(self.root, mode="auto")
        executor.set_def_resolver(registry.get)
        collector = _RecordingCollector()
        action = json.dumps({
            "type": "tool", "tool": "show_media",
            "arguments": {"paths": [str(outsider)]},
        }, ensure_ascii=False)
        replies = [action, "无法展示。"]
        state = {"index": 0}

        def complete(_profile, _messages, _options, _event):
            index = min(state["index"], len(replies) - 1)
            state["index"] += 1
            return replies[index]

        worker = SkillAgent(_EmptyCatalog(), executor, complete, collector)
        _response, runs, _r, _u = worker.run(
            "把刚才那张图展示出来", [],
            {"kind": "local", "model": "m", "context_window": 32768},
            {"stream": False, "max_tokens": 256, "max_steps": 3},
            {"mode": "auto", "skill_ids": []}, [], "", ["show_media"],
            lambda _payload: None, None,
            tool_registry=registry,
            run_context={
                "run_id": "run-show-media-2", "conversation_id": "conv-2",
                "executor": executor, "workspace_dir": str(self.root),
                "confirmation_ui": True,
                "show_media_intent": True,
                "show_media_candidates": [],
            },
        )
        target_runs = [run for run in runs if run.get("tool") == "show_media"]
        self.assertEqual(len(target_runs), 1)
        self.assertFalse(target_runs[0]["success"])
        from naiba.core.attachments import union_run_media

        self.assertEqual(union_run_media(runs)[0], [])
        self.assertEqual(len(collector.calls), 1, "采集器仍被调用（按 run.success 自行丢弃）")
        self.assertEqual(collector.calls[0][0].get("media"), None,
                         "失败调用的 run 上不得出现 media")


if __name__ == "__main__":
    unittest.main()
