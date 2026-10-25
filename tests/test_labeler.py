# -*- coding: utf-8 -*-
"""护栏：打标器（docs/plans/2026-10-23-打标器接入工作台计划.md）。

覆盖六条主线：
1. 输入安全：只认受管 uploads、扩展名/解码校验、批量上限、重复去重；
2. 配置契约：白名单、非在线拒绝、未验证并发拒绝、config_fingerprint 稳定且排除易变项；
3. 逐图事务：标注 + 状态 + 用量一次提交；running 崩溃 → uncertain；
4. 执行与恢复：复用既有结果不重复调用、瞬时错误重试、永久错误不重试、取消语义、
   启动恢复只把 running 判为 uncertain；
5. 「继续未完成」：新建关联任务、成功项不重复、uncertain 必须显式确认；
6. 导出与句子库：三格式、CSV 公式注入防护、受管落点、degraded 标记。
"""

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core.paths import normalized_path_key  # noqa: E402
from naiba.labeler import LABELER_ITEM_STATES  # noqa: E402
from naiba.labeler.db import LabelerDB, LabelerDBError  # noqa: E402
from naiba.labeler.engines.provider import build_prompt, parse_response  # noqa: E402
from naiba.labeler.export import (  # noqa: E402
    LabelerExportError,
    open_export_dir,
    render,
    render_lora_package,
    write_export,
)
from naiba.labeler.images import (  # noqa: E402
    MAX_BATCH_IMAGES,
    LabelerImageError,
    stable_item_id,
    validate_image_refs,
)
from naiba.labeler.jobs import (  # noqa: E402
    LabelerJobManager,
    LabelerTaskError,
    config_fingerprint,
    normalize_config,
)
from naiba.labeler.originals import archive_original, original_path, prune_unreferenced  # noqa: E402
from naiba.labeler.presets import DEFAULT_PRESET, PRESET_IDS, filter_tags, get_preset, preset_list  # noqa: E402
from naiba.labeler.runner import LabelerRunner, aggregate_usage, classify_error  # noqa: E402
from naiba.labeler.sentences import sentence_stats, split_sentences  # noqa: E402
from naiba.labeler.service import LabelerService  # noqa: E402
from naiba.labeler.store import LabelerStore  # noqa: E402


def _write_png(path: Path, color=(20, 120, 200), size=(16, 16)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


class _StubConfig:
    def __init__(self, kind: str = "online"):
        self.kind = kind

    def profile(self, key):
        return {"kind": self.kind, "name": "stub", "model": "stub-model",
                "base_url": "http://127.0.0.1:9"}

    def runtime_guard_options(self):
        return {}


class _StubModels:
    def __init__(self, behavior=None):
        self.behavior = behavior or (
            lambda profile, messages, options, status: '{"tags":[{"tag":"cat","confidence":0.9}],'
                                                       '"description":"一只猫"}'
        )
        self.calls: list[dict] = []
        self.last_usage: dict = {}
        self.last_reasoning = ""
        self.last_finish_reason = ""

    def complete(self, profile, messages, options, status=None):
        self.calls.append({"messages": messages, "options": options})
        text = self.behavior(profile, messages, options, status)
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        self.last_finish_reason = "stop"
        return text


class _StubPaths:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)


class _StubRuns:
    """模拟「有对话在跑」：前 N 次查询返回活跃，之后返回空。"""

    def __init__(self, active_times: int = 0):
        self.active_times = active_times
        self.calls = 0

    def list_primary(self, conversation_id="", active_only=False):
        self.calls += 1
        if self.active_times > 0:
            self.active_times -= 1
            return [{"id": "run-active"}]
        return []


class _StubApp:
    def __init__(self, data_dir: Path, *, models=None, config=None):
        self.paths = _StubPaths(data_dir)
        self.models = models or _StubModels()
        self.config = config or _StubConfig()
        self.runs = None


def _base_config(**overrides):
    # 默认语言 en：2026-10-26 联动计划起「英文标签 + 中文描述（mixed）」只在
    # 「标签 + 自然语言」下可选，旧默认 mixed + keywords 属历史组合（详见
    # test_language_mixed_only_valid_for_both_mode 与继续兼容用例）。
    config = {
        "engine": "provider",
        "output_mode": "keywords",
        "language": "en",
        "preset": DEFAULT_PRESET,
        "model_key": "online:stub",
        "model_name": "stub-model",
        "confidence_threshold": 0.35,
        "max_tags": 24,
        "custom_prompt": "",
        "subject_filter": {"enabled": False, "keywords": [], "strength": "medium"},
        "concurrency": 1,
        "retry_limit": 1,
        "timeout_seconds": 90,
        "prompt_version": 2,
    }
    config.update(overrides)
    return config


class LabelerImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.uploads = self.data_dir / "uploads"
        self.upload = _write_png(self.uploads / "2026-10-23" / "cover.png")

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_managed_uploads_and_keeps_relative_path(self):
        refs = validate_image_refs(self.data_dir, [{
            "path": str(self.upload), "name": "cover.png",
            "relative_path": "chapter-01/cover.png",
        }])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["relative_path"], "chapter-01/cover.png")
        # 校验端会 resolve()（短路径 TEMP 下会展开 junction），比较必须同口径。
        self.assertEqual(refs[0]["path_key"], normalized_path_key(self.upload.resolve()))
        self.assertIn(":", refs[0]["content_version"])

    def test_rejects_path_outside_uploads(self):
        outside = _write_png(self.data_dir / "elsewhere" / "x.png")
        with self.assertRaises(LabelerImageError):
            validate_image_refs(self.data_dir, [{"path": str(outside)}])

    def test_rejects_non_image_extension_and_bad_bytes(self):
        bad_ext = self.uploads / "note.txt"
        bad_ext.write_text("hi", encoding="utf-8")
        with self.assertRaises(LabelerImageError):
            validate_image_refs(self.data_dir, [{"path": str(bad_ext)}])
        fake = self.uploads / "fake.png"
        fake.write_bytes(b"not an image")
        with self.assertRaises(LabelerImageError):
            validate_image_refs(self.data_dir, [{"path": str(fake)}])

    def test_rejects_too_many_images(self):
        refs = [{"path": str(self.upload)} for _ in range(MAX_BATCH_IMAGES + 1)]
        with self.assertRaises(LabelerImageError):
            validate_image_refs(self.data_dir, refs)

    def test_sanitizes_relative_path_traversal(self):
        refs = validate_image_refs(self.data_dir, [{
            "path": str(self.upload), "relative_path": "..\\..\\windows\\x.png",
        }])
        # 只保留相对名称段，绝不带 .. 或盘符。
        self.assertNotIn("..", refs[0]["relative_path"])
        self.assertNotIn(":", refs[0]["relative_path"])

    def test_dedupes_same_file_in_one_batch(self):
        refs = validate_image_refs(self.data_dir, [
            {"path": str(self.upload)}, {"path": str(self.upload)},
        ])
        self.assertEqual(len(refs), 1)

    def test_stable_item_id_changes_with_content_version(self):
        a = stable_item_id("tg-1", "k", "1:1")
        b = stable_item_id("tg-1", "k", "1:2")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("it-"))


class LabelerConfigTests(unittest.TestCase):
    def setUp(self):
        self.app = _StubApp(Path(tempfile.gettempdir()))

    def test_rejects_unknown_key_and_secret_fields(self):
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(api_key="sk-should-not-be-accepted"))
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(provider_info={"base_url": "http://evil"}))

    def test_accepts_local_profile_but_keeps_concurrency_one(self):
        # 用户口径：有 provider 就行，本地后端也算 provider；但并发仍固定 1。
        app = _StubApp(Path(tempfile.gettempdir()), config=_StubConfig(kind="local"))
        config = normalize_config(app, _base_config())
        self.assertEqual(config["concurrency"], 1)
        self.assertEqual(config["engine"], "provider")

    def test_rejects_unvalidated_concurrency(self):
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(concurrency=4))

    def test_fingerprint_ignores_volatile_fields(self):
        a = normalize_config(self.app, _base_config(retry_limit=0, timeout_seconds=30, concurrency=1))
        b = normalize_config(self.app, _base_config(retry_limit=5, timeout_seconds=600, concurrency=1))
        self.assertEqual(a["config_fingerprint"], b["config_fingerprint"])
        c = normalize_config(self.app, _base_config(preset="style_lora"))
        self.assertNotEqual(a["config_fingerprint"], c["config_fingerprint"])

    def test_preset_ids_are_all_known(self):
        self.assertEqual(len(set(PRESET_IDS)), 7)
        self.assertEqual(len(preset_list()), 7)
        with self.assertRaises(ValueError):
            get_preset("nope")

    def test_rejects_unknown_language_and_phrase_mode_is_valid(self):
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(language="fr"))
        config = normalize_config(self.app, _base_config(output_mode="phrases", language="en"))
        self.assertEqual(config["output_mode"], "phrases")
        self.assertEqual(config["language"], "en")

    def test_language_changes_fingerprint(self):
        zh = normalize_config(self.app, _base_config(language="zh"))
        en = normalize_config(self.app, _base_config(language="en"))
        self.assertNotEqual(zh["config_fingerprint"], en["config_fingerprint"])

    def test_language_mixed_only_valid_for_both_mode(self):
        # 「英文标签 + 中文描述」只在「标签 + 自然语言」下可选；新任务提交
        # 其余组合直接 400（2026-10-26 输出形式与语言联动计划）。
        for mode in ("keywords", "phrases", "description"):
            with self.assertRaises(LabelerTaskError):
                normalize_config(self.app, _base_config(output_mode=mode, language="mixed"))
        both = normalize_config(self.app, _base_config(output_mode="both", language="mixed"))
        self.assertEqual(both["language"], "mixed")
        # 历史任务（mixed + 单一形式的旧默认组合）继续/恢复时原样放行。
        legacy = normalize_config(
            self.app, _base_config(output_mode="keywords", language="mixed"),
            allow_legacy_language=True)
        self.assertEqual(legacy["language"], "mixed")

    def test_trigger_word_normalization_and_validation(self):
        # 默认关闭；启用必须填内容；空白折叠、长度封顶；未知字段拒绝。
        disabled = normalize_config(self.app, _base_config())
        self.assertEqual(disabled["trigger_word"], {"enabled": False, "value": ""})
        enabled = normalize_config(self.app, _base_config(
            trigger_word={"enabled": True, "value": "  my_ohwx \n style "}))
        self.assertEqual(enabled["trigger_word"], {"enabled": True, "value": "my_ohwx style"})
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(trigger_word={"enabled": True, "value": " "}))
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(trigger_word={"enabled": True, "oops": 1}))
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(trigger_word="ohwx"))
        long = normalize_config(self.app, _base_config(
            trigger_word={"enabled": True, "value": "x" * 300}))
        self.assertEqual(len(long["trigger_word"]["value"]), 200)

    def test_trigger_word_fingerprint_rules(self):
        # 关闭 = 缺省：与完全没有该键的旧配置指纹一致（默认关闭不复用不重复计费）；
        # 启用即改变结果身份；关闭但留值不影响身份。
        disabled = normalize_config(self.app, _base_config())
        legacy_payload = {key: disabled.get(key) for key in disabled if key != "trigger_word"}
        self.assertEqual(disabled["config_fingerprint"], config_fingerprint(legacy_payload))
        disabled_with_value = normalize_config(self.app, _base_config(
            trigger_word={"enabled": False, "value": "ohwx"}))
        self.assertEqual(disabled["config_fingerprint"], disabled_with_value["config_fingerprint"])
        enabled = normalize_config(self.app, _base_config(
            trigger_word={"enabled": True, "value": "ohwx"}))
        self.assertNotEqual(disabled["config_fingerprint"], enabled["config_fingerprint"])
        other = normalize_config(self.app, _base_config(
            trigger_word={"enabled": True, "value": "other"}))
        self.assertNotEqual(enabled["config_fingerprint"], other["config_fingerprint"])


class LabelerPresetAndParseTests(unittest.TestCase):
    def test_filter_drops_ignored_domain_and_duplicates(self):
        # 选「画风」= 描述忽略画风：模型仍输出风格词时直接丢弃。
        preset = get_preset("style_lora")
        tags = ["Woman", "woman", "red dress", "high quality", "oil painting", "watercolor"]
        out = filter_tags(tags, preset=preset)
        self.assertEqual(out, ["woman", "red dress"])

    def test_face_preset_drops_face_words(self):
        out = filter_tags(["smile", "blue eyes", "red dress", "妆容"],
                          preset=get_preset("face_lora"))
        self.assertEqual(out, ["red dress"])

    def test_subject_filter_extra_keywords(self):
        out = filter_tags(["cat", "dog", "person"], preset=get_preset("object_lora"),
                          extra_keywords=["dog"], strength="medium")
        self.assertNotIn("dog", out)

    def test_prompt_uses_ignore_semantics(self):
        # 口径：选「面部」= 描述忽略面部，而不是重点描述面部。
        preset = get_preset("face_lora")
        prompt = build_prompt(_base_config(preset="face_lora", output_mode="description"), preset)
        self.assertIn("生成模式（以下内容必须从标签和描述里省略）", prompt)
        self.assertIn("忽略面部", prompt)
        self.assertNotIn("重点标出", prompt)

    def test_prompt_general_preset_has_no_ignore_block(self):
        prompt = build_prompt(_base_config(preset="general"), get_preset("general"))
        self.assertNotIn("生成模式（以下内容必须从标签和描述里省略）", prompt)

    def test_prompt_language_and_phrase_mode(self):
        prompt = build_prompt(
            _base_config(preset="general", output_mode="phrases", language="en"),
            get_preset("general"),
        )
        self.assertIn("全部用英文", prompt)
        self.assertIn("词组短语", prompt)
        zh = build_prompt(
            _base_config(preset="general", output_mode="description", language="zh"),
            get_preset("general"),
        )
        self.assertIn("全部用中文", zh)
        # mixed（英文标签 + 中文描述）是与「标签 + 自然语言」配套的合法组合。
        mixed = build_prompt(
            _base_config(preset="general", output_mode="both", language="mixed"),
            get_preset("general"),
        )
        self.assertIn("标签用小写英文名词短语", mixed)
        self.assertIn("description 用中文", mixed)

    def test_parse_json_with_code_fence(self):
        parsed = parse_response('```json\n{"tags":[{"tag":"cat","confidence":0.9}],"description":"d"}\n```')
        self.assertEqual(parsed["tags"], ["cat"])
        self.assertEqual(parsed["confidences"], [0.9])
        self.assertEqual(parsed["description"], "d")
        self.assertEqual(parsed["parsed"], "json")

    def test_parse_fallback_comma_list(self):
        parsed = parse_response("cat, dog, warm light")
        self.assertEqual(parsed["tags"], ["cat", "dog", "warm light"])
        self.assertEqual(parsed["parsed"], "fallback")

    def test_classify_error(self):
        self.assertEqual(classify_error(RuntimeError("HTTP 429 rate limit")), "transient")
        self.assertEqual(classify_error(RuntimeError("401 unauthorized")), "auth")
        self.assertEqual(classify_error(RuntimeError("400 bad request")), "bad_request")
        self.assertEqual(classify_error(RuntimeError("任务已取消")), "cancelled")
        self.assertEqual(classify_error(RuntimeError("weird thing")), "permanent")


    def test_prompt_combinations_consistent_for_valid_languages(self):
        # 4 种输出形式 × 各自可见语言：语言规则与形式规则同现且不矛盾
        # （2026-10-26 联动计划验收）。旧任务 mixed + 标签仍能组出提示词。
        cases = [
            ("keywords", "zh", "全部用中文"),
            ("keywords", "en", "全部用英文"),
            ("phrases", "zh", "中文逗号"),
            ("phrases", "en", "英文逗号"),
            ("description", "zh", "全部用中文"),
            ("description", "en", "全部用英文"),
            ("both", "mixed", "标签用小写英文名词短语"),
            ("both", "zh", "全部用中文。标签用中文名词短语"),
            ("both", "en", "全部用英文。标签用小写英文名词短语"),
        ]
        for mode, language, marker in cases:
            prompt = build_prompt(
                _base_config(output_mode=mode, language=language), get_preset(DEFAULT_PRESET))
            self.assertIn(marker, prompt, f"{mode}/{language}")
        legacy = build_prompt(
            _base_config(output_mode="keywords", language="mixed"), get_preset(DEFAULT_PRESET))
        self.assertIn("输出形式：只要标签", legacy)


class LabelerTriggerAndLanguageUiTests(unittest.TestCase):
    """触发词控件与输出形式/语言联动的前端接线（源码断言）。"""

    def test_trigger_word_controls_wired_in_ui(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "public/index.html").read_text(encoding="utf-8")
        js = (root / "public/js/23-labeler.js").read_text(encoding="utf-8")
        # 控件在配置区（输出语言行与模型配置行之间），默认关闭（disabled）。
        self.assertIn('id="labelerTriggerEnabled"', html)
        self.assertIn('id="labelerTriggerWord"', html)
        self.assertLess(html.index('id="labelerLanguage"'), html.index('id="labelerTriggerEnabled"'))
        self.assertLess(html.index('id="labelerTriggerEnabled"'), html.index('id="labelerProviderSelect"'))
        self.assertIn('disabled aria-label="触发词"', html)
        # 配置收集带 trigger_word；启用未填写不允许开始任务。
        self.assertIn("trigger_word: {", js)
        self.assertIn("启用触发词后，请先填写触发词内容", js)
        self.assertIn("triggerWord.disabled = !triggerEnabled.checked", js)

    def test_language_options_follow_output_mode(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "public/index.html").read_text(encoding="utf-8")
        js = (root / "public/js/23-labeler.js").read_text(encoding="utf-8")
        # mixed 只作为 both 的选项（文案改为「英文标签 + 中文描述」，不再叫中英混合）。
        self.assertNotIn("中英混合", html)
        self.assertIn("英文标签 + 中文描述", html)
        # 联动：选项可见性 + 值映射（标签→英文，短语/自然语言→中文），隐藏值不生效。
        self.assertIn("function syncLanguageOptions()", js)
        self.assertIn('$("#labelerOutputMode")?.addEventListener("change", syncLanguageOptions)', js)
        self.assertIn("option.hidden = !(value in labels)", js)
        self.assertIn('langSelect.value = mode === "keywords" ? "en" : "zh"', js)


class LabelerDBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.db = LabelerDB(self.data_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _item(self, item_id="it-1", key="k1", version="1:1"):
        return {
            "item_id": item_id, "path": "C:/data/uploads/a.png", "path_key": key,
            "content_version": version, "relative_path": "a.png", "name": "a.png",
            "size": 10, "mtime_ns": 1,
        }

    def _annotation(self, key="k1", version="1:1", tags=None):
        return {
            "path_key": key, "content_version": version, "path": "C:/data/uploads/a.png",
            "relative_path": "a.png", "name": "a.png", "engine": "provider",
            "output_mode": "keywords", "preset": "face_lora", "prompt_version": 1,
            "model_key": "online:stub", "model_name": "stub-model",
            "tags": tags or ["cat"], "confidences": [0.9], "description": "",
        }

    def test_complete_item_is_one_transaction_and_counts(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.assertEqual(self.db.counts("tg-1")["queued"], 1)
        self.db.mark_running("tg-1", "it-1")
        self.assertEqual(self.db.counts("tg-1")["running"], 1)
        annotation_id, reused = self.db.complete_item(
            "tg-1", "it-1", annotation=self._annotation(), usage={"total_tokens": 15},
            config_fingerprint="cfg-a",
        )
        self.assertFalse(reused)
        self.assertGreater(annotation_id, 0)
        counts = self.db.counts("tg-1")
        self.assertEqual(counts["done"], 1)
        self.assertEqual(counts["running"], 0)
        self.assertEqual(self.db.annotation_ids_for_task("tg-1"), [annotation_id])
        self.assertEqual(self.db.list_items("tg-1")[0]["usage"], {"total_tokens": 15})

    def test_unique_identity_reuses_row(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        first, _ = self.db.complete_item("tg-1", "it-1", annotation=self._annotation(),
                                        usage={}, config_fingerprint="cfg-a")
        reused_row = self.db.find_reusable("k1", "1:1", "cfg-a")
        self.assertIsNotNone(reused_row)
        self.assertEqual(reused_row["id"], first)
        second, reused = self.db.complete_item(
            "tg-1", "it-1", annotation=self._annotation(tags=["cat", "fur"]),
            usage={}, config_fingerprint="cfg-a",
        )
        self.assertTrue(reused)
        self.assertEqual(first, second)
        self.assertEqual(self.db.find_reusable("k1", "1:1", "cfg-a")["tags"], ["cat", "fur"])

    def test_reuse_keeps_original_task_ownership_but_join_sees_both(self):
        """复用不搬走历史：标注仍归首个任务，后续任务通过 task_items 关联可见。"""
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.db.complete_item("tg-1", "it-1", annotation=self._annotation(),
                              usage={}, config_fingerprint="cfg-a")
        self.db.add_items("tg-2", [self._item()], "cfg-a")
        self.db.complete_item("tg-2", "it-1", annotation=self._annotation(),
                              usage={}, config_fingerprint="cfg-a")
        row = self.db.annotations(limit=10)[0]
        self.assertEqual(row["source_task_id"], "tg-1", "归属不能被重跑改写")
        self.assertEqual(len(self.db.annotations(task_id="tg-1")), 1)
        self.assertEqual(len(self.db.annotations(task_id="tg-2")), 1)

    def test_different_fingerprint_keeps_history(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.db.complete_item("tg-1", "it-1", annotation=self._annotation(),
                             usage={}, config_fingerprint="cfg-a")
        self.db.add_items("tg-2", [self._item()], "cfg-b")
        self.db.complete_item("tg-2", "it-1", annotation=self._annotation(),
                             usage={}, config_fingerprint="cfg-b")
        rows = self.db.annotations(limit=10)
        self.assertEqual(len(rows), 2)

    def test_running_becomes_uncertain_on_recovery(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.db.mark_running("tg-1", "it-1")
        self.assertEqual(self.db.mark_running_as_uncertain(), 1)
        counts = self.db.counts("tg-1")
        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual(counts["running"], 0)

    def test_failed_item_keeps_usage_and_category(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.db.fail_item("tg-1", "it-1", error="HTTP 500", category="transient",
                          usage={"total_tokens": 3})
        item = self.db.list_items("tg-1")[0]
        self.assertEqual(item["state"], "failed")
        self.assertEqual(item["error_category"], "transient")
        self.assertEqual(item["usage"], {"total_tokens": 3})

    def test_all_item_states_are_declared(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.assertIn(self.db.list_items("tg-1")[0]["state"], LABELER_ITEM_STATES)

    def test_referenced_paths_only_for_given_tasks(self):
        self.db.add_items("tg-1", [self._item()], "cfg-a")
        self.assertEqual(self.db.referenced_paths(["tg-1"]), {"C:/data/uploads/a.png"})
        self.assertEqual(self.db.referenced_paths(["tg-9"]), set())


class LabelerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 清理顺序很重要：runner 必须在删临时目录之前停掉，否则 Windows 上
        # 还在跑的 worker 会占着 labeler.db，tearDown 报 PermissionError。
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.uploads = self.data_dir / "uploads"
        self.images = [
            _write_png(self.uploads / "d" / f"a{i}.png", color=(10 * i, 100, 200))
            for i in range(3)
        ]
        self.store = LabelerStore(self.data_dir)
        self.db = LabelerDB(self.data_dir)

    def _make_app(self, models=None):
        return _StubApp(self.data_dir, models=models or _StubModels())

    def _jobs(self, app):
        runner = LabelerRunner(app, self.store, self.db)
        self.addCleanup(runner.shutdown)
        return LabelerJobManager(app, self.store, self.db, runner)

    def _submit(self, app, config=None, images=None):
        jobs = self._jobs(app)
        refs = [
            {"path": str(path), "name": path.name, "relative_path": f"d/{path.name}"}
            for path in (images if images is not None else self.images)
        ]
        task = jobs.submit({"image_refs": refs, "config": config or _base_config()})
        return jobs, task

    def _wait(self, task_id, timeout=10.0):
        deadline = time.time() + timeout
        state = None
        while time.time() < deadline:
            state = self.store.get_task(task_id)
            if state and state.get("status") in {"done", "failed", "cancelled", "interrupted"}:
                return state
            time.sleep(0.02)
        return state

    def test_success_path_records_every_item_and_usage(self):
        app = self._make_app()
        _, task = self._submit(app)
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        counts = self.db.counts(task["id"])
        self.assertEqual(counts["done"], 3)
        self.assertEqual(state["usage"].get("total_tokens"), 45)
        self.assertEqual(len(state["result"]["annotation_ids"]), 3)
        self.assertEqual(len(app.models.calls), 3)
        # 图片以多模态 part 发送（不是把路径拼进提示文本）。
        first_content = app.models.calls[0]["messages"][0]["content"]
        self.assertEqual(first_content[0]["type"], "image")

    def test_reuses_existing_result_without_calling_model(self):
        app = self._make_app()
        _, task = self._submit(app, images=self.images[:1])
        self._wait(task["id"])
        calls_before = len(app.models.calls)
        _, task2 = self._submit(app, images=self.images[:1])
        state = self._wait(task2["id"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(len(app.models.calls), calls_before, "同图同配置不应重复调用模型")
        self.assertEqual(self.db.counts(task2["id"])["done"], 1)

    def test_transient_error_retries_then_succeeds(self):
        remaining = {"n": 1}

        def behavior(profile, messages, options, status):
            if remaining["n"] > 0:
                remaining["n"] -= 1
                raise RuntimeError("HTTP 503 service unavailable")
            return '{"tags":[{"tag":"cat","confidence":0.9}],"description":""}'

        app = self._make_app(_StubModels(behavior))
        _, task = self._submit(app, config=_base_config(retry_limit=1), images=self.images[:1])
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(self.db.counts(task["id"])["done"], 1)
        self.assertEqual(len(app.models.calls), 2)

    def test_permanent_error_not_retried(self):
        def behavior(profile, messages, options, status):
            raise RuntimeError("401 unauthorized: invalid api key")

        app = self._make_app(_StubModels(behavior))
        _, task = self._submit(app, config=_base_config(retry_limit=3), images=self.images[:1])
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(len(app.models.calls), 1)
        item = self.db.list_items(task["id"])[0]
        self.assertEqual(item["state"], "failed")
        self.assertEqual(item["error_category"], "auth")

    def test_cancel_running_task_stops_and_marks_remaining(self):
        started = threading.Event()

        def behavior(profile, messages, options, status):
            started.set()
            for _ in range(300):
                if options["cancel_event"].is_set():
                    raise RuntimeError("任务已取消")
                time.sleep(0.01)
            return '{"tags":[{"tag":"cat","confidence":0.9}],"description":""}'

        app = self._make_app(_StubModels(behavior))
        jobs, task = self._submit(app)
        self.assertTrue(started.wait(5))
        self.assertTrue(jobs.cancel(task["id"]))
        state = self._wait(task["id"], timeout=15)
        self.assertEqual(state["status"], "cancelled")
        counts = self.db.counts(task["id"])
        self.assertEqual(counts["cancelled"], 3)
        self.assertEqual(counts["running"], 0)

    def test_recover_marks_interrupted_and_uncertain(self):
        app = self._make_app()
        _, task = self._submit(app, images=self.images[:2])
        self._wait(task["id"])
        # 人为制造「请求已发出但结果没落库」：把成功项改回 running。
        with self.db._connect() as db:
            db.execute("UPDATE task_items SET state='running' WHERE task_id=? AND ordinal=0", (task["id"],))
        self.store.update_task(task["id"], status="running")
        jobs = self._jobs(app)
        recovered = jobs.recover_interrupted()
        self.assertEqual(recovered, 1)
        state = self.store.get_task(task["id"])
        self.assertEqual(state["status"], "interrupted")
        counts = self.db.counts(task["id"])
        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual(counts["done"], 1)

    def test_continue_unfinished_skips_succeeded_and_requires_uncertain_confirm(self):
        app = self._make_app()
        _, task = self._submit(app, images=self.images[:2])
        self._wait(task["id"])
        # 制造一条 uncertain。
        with self.db._connect() as db:
            db.execute("UPDATE task_items SET state='uncertain' WHERE task_id=? AND ordinal=1",
                       (task["id"],))
        self.store.update_task(task["id"], status="interrupted")
        jobs = self._jobs(app)
        with self.assertRaises(LabelerTaskError):
            jobs.continue_task(task["id"], mode="unfinished", confirm_uncertain=False)
        new_task = jobs.continue_task(task["id"], mode="unfinished", confirm_uncertain=True)
        self.assertEqual(new_task["source_task_id"], task["id"])
        counts = self.db.counts(new_task["id"])
        self.assertEqual(counts["total"], 1, "成功项不应复制到新任务")
        names = [item["name"] for item in self.db.list_items(new_task["id"])]
        self.assertEqual(names, [self.images[1].name], "只复制「结果不确定」的那一张")

    def test_continue_failed_only(self):
        def behavior(profile, messages, options, status):
            raise RuntimeError("500 server error")

        app = self._make_app(_StubModels(behavior))
        _, task = self._submit(app, config=_base_config(retry_limit=0), images=self.images[:2])
        self._wait(task["id"])
        jobs = self._jobs(app)
        new_task = jobs.continue_task(task["id"], mode="failed_only")
        self.assertEqual(self.db.counts(new_task["id"])["total"], 2)

    def test_usage_accumulator_ignores_non_numeric(self):
        merged = aggregate_usage({"total_tokens": 3}, {"total_tokens": 2, "model": "x"})
        self.assertEqual(merged["total_tokens"], 5)
        self.assertNotIn("model", merged)

    def test_local_profile_waits_for_chat_but_still_finishes(self):
        """本地后端：有对话在跑时先让路，对话结束后照样完成打标（不互相卡死）。"""
        app = _StubApp(self.data_dir, config=_StubConfig(kind="local"))
        app.runs = _StubRuns(active_times=2)
        _, task = self._submit(app, images=self.images[:1])
        state = self._wait(task["id"], timeout=15)
        self.assertEqual(state["status"], "done")
        self.assertGreaterEqual(app.runs.calls, 3, "应当先等待对话结束再处理图片")

    def test_online_profile_does_not_poll_chat_runs(self):
        app = _StubApp(self.data_dir, config=_StubConfig(kind="online"))
        app.runs = _StubRuns(active_times=5)
        _, task = self._submit(app, images=self.images[:1])
        state = self._wait(task["id"], timeout=15)
        self.assertEqual(state["status"], "done")
        self.assertEqual(app.runs.calls, 0, "在线 provider 不需要礼让查询")


class LabelerTriggerWordTests(unittest.TestCase):
    """触发词前缀（2026-10-26 计划）：过滤后统一加前缀、复用隔离、导出包含前缀。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.uploads = self.data_dir / "uploads"
        self.images = [
            _write_png(self.uploads / "d" / f"a{i}.png", color=(10 * i, 100, 200))
            for i in range(2)
        ]
        self.store = LabelerStore(self.data_dir)
        self.db = LabelerDB(self.data_dir)
        self.app = _StubApp(self.data_dir, models=_StubModels())

    def _jobs(self):
        runner = LabelerRunner(self.app, self.store, self.db)
        self.addCleanup(runner.shutdown)
        return LabelerJobManager(self.app, self.store, self.db, runner)

    def _submit(self, jobs, config):
        refs = [
            {"path": str(path), "name": path.name, "relative_path": f"d/{path.name}"}
            for path in self.images[:1]
        ]
        return jobs, jobs.submit({"image_refs": refs, "config": config})

    def _wait(self, task_id, timeout=10.0):
        deadline = time.time() + timeout
        state = None
        while time.time() < deadline:
            state = self.store.get_task(task_id)
            if state and state.get("status") in {"done", "failed", "cancelled", "interrupted"}:
                return state
            time.sleep(0.02)
        return state

    def _annotation_of(self, task_id):
        ids = self.db.annotation_ids_for_task(task_id)
        return self.db.annotations_by_ids(ids)[0]

    def test_both_mode_prefixes_tags_and_description(self):
        _, task = self._submit(self._jobs(), _base_config(
            output_mode="both", language="mixed",
            trigger_word={"enabled": True, "value": "my_ohwx"}))
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        annotation = self._annotation_of(task["id"])
        # 标签首位是触发词，置信度补 -1 保持按位对齐；描述最前加前缀（中文逗号）。
        self.assertEqual(annotation["tags"], ["my_ohwx", "cat"])
        self.assertEqual(annotation["confidences"], [-1.0, 0.9])
        self.assertEqual(annotation["description"], "my_ohwx，一只猫")

    def test_english_description_uses_english_separator(self):
        _, task = self._submit(self._jobs(), _base_config(
            output_mode="both", language="en",
            trigger_word={"enabled": True, "value": "ohwx"}))
        self._wait(task["id"])
        annotation = self._annotation_of(task["id"])
        self.assertEqual(annotation["description"], "ohwx, 一只猫")

    def test_no_duplicate_when_already_prefixed(self):
        def behavior(profile, messages, options, status):
            return ('{"tags":[{"tag":"My_Ohwx","confidence":0.9}],'
                    '"description":"my_ohwx, 一只猫"}')

        self.app = _StubApp(self.data_dir, models=_StubModels(behavior))
        _, task = self._submit(self._jobs(), _base_config(
            output_mode="both", language="en",
            trigger_word={"enabled": True, "value": "my_ohwx"}))
        self._wait(task["id"])
        annotation = self._annotation_of(task["id"])
        # 首标签已是触发词（大小写不同也算）、描述已以其开头：都不重复加。
        self.assertEqual(annotation["tags"], ["My_Ohwx"])
        self.assertEqual(annotation["confidences"], [0.9])
        self.assertEqual(annotation["description"], "my_ohwx, 一只猫")

    def test_phrases_mode_prefixes_description_only(self):
        def behavior(profile, messages, options, status):
            return '{"tags":[],"description":"一只橘猫, 阳光, 木地板"}'

        self.app = _StubApp(self.data_dir, models=_StubModels(behavior))
        _, task = self._submit(self._jobs(), _base_config(
            output_mode="phrases", language="zh",
            trigger_word={"enabled": True, "value": "mystyle"}))
        self._wait(task["id"])
        annotation = self._annotation_of(task["id"])
        self.assertEqual(annotation["tags"], [])
        self.assertTrue(annotation["description"].startswith("mystyle，"))
        self.assertIn("一只橘猫", annotation["description"])

    def test_disabled_trigger_keeps_results_and_reuse(self):
        jobs = self._jobs()
        _, task = self._submit(jobs, _base_config(output_mode="both", language="mixed"))
        self._wait(task["id"])
        annotation = self._annotation_of(task["id"])
        self.assertEqual(annotation["tags"], ["cat"])
        self.assertEqual(annotation["description"], "一只猫")
        # 关闭状态下与旧任务指纹一致：同图同配置继续复用，不重复调用模型。
        calls_before = len(self.app.models.calls)
        _, task2 = self._submit(jobs, _base_config(output_mode="both", language="mixed"))
        self._wait(task2["id"])
        self.assertEqual(len(self.app.models.calls), calls_before)

    def test_enabled_trigger_disables_cross_config_reuse(self):
        jobs = self._jobs()
        base = _base_config(output_mode="both", language="mixed")
        _, task = self._submit(jobs, base)
        self._wait(task["id"])
        calls_before = len(self.app.models.calls)
        _, task2 = self._submit(jobs, _base_config(
            output_mode="both", language="mixed",
            trigger_word={"enabled": True, "value": "ohwx"}))
        state = self._wait(task2["id"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(len(self.app.models.calls), calls_before + 1,
                         "不同触发词配置不得复用旧标注")
        annotation = self._annotation_of(task2["id"])
        self.assertEqual(annotation["tags"][0], "ohwx")

    def test_captions_export_carries_prefix(self):
        _, task = self._submit(self._jobs(), _base_config(
            output_mode="both", language="mixed",
            trigger_word={"enabled": True, "value": "my_ohwx"}))
        self._wait(task["id"])
        annotation = self._annotation_of(task["id"])
        records = [dict(annotation)]
        payload = render(records, "captions")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            text = archive.read("d/a0.txt").decode("utf-8")
        # 逐图 caption 一行：触发词在最前（训练包同名 txt 走同一 _caption_text）。
        self.assertTrue(text.startswith("my_ohwx,"))
        self.assertIn("cat", text)

    def test_continue_task_preserves_legacy_mixed_language(self):
        def behavior(profile, messages, options, status):
            raise RuntimeError("401 unauthorized")

        self.app = _StubApp(self.data_dir, models=_StubModels(behavior))
        jobs = self._jobs()
        # 直接落一条历史任务：mixed + 标签（旧默认组合），跳过新任务校验。
        legacy_config = normalize_config(
            self.app, _base_config(output_mode="keywords", language="mixed"),
            allow_legacy_language=True)
        refs = validate_image_refs(self.data_dir, [{
            "path": str(self.images[0]), "name": self.images[0].name,
            "relative_path": f"d/{self.images[0].name}",
        }])
        task = jobs._create_task({"image_refs": refs, "config": legacy_config}, source_task_id="")
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "failed")
        continued = jobs.continue_task(task["id"])
        stored = self.store.get_task(continued["id"])
        self.assertEqual(stored["input"]["config"]["language"], "mixed")
        self.assertEqual(stored["source_task_id"], task["id"])


class LabelerStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LabelerStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_task_carries_module_and_origin(self):
        task = self.store.create_task({"image_refs": [], "config": {}}, source_task_id="tg-src")
        self.assertEqual(task["module"], "labeler")
        self.assertEqual(task["origin_label"], "打标器")
        self.assertEqual(task["source_task_id"], "tg-src")
        reloaded = self.store.get_task(task["id"])
        self.assertEqual(reloaded["module"], "labeler")

    def test_update_rejects_unknown_fields(self):
        task = self.store.create_task({"image_refs": [], "config": {}})
        updated = self.store.update_task(task["id"], status="running", input={"x": 1})
        self.assertEqual(updated["status"], "running")
        self.assertEqual(updated["input"], {"image_refs": [], "config": {}}, "input 不可被 update 改写")

    def test_events_allocate_monotonic_seq_and_reject_unknown(self):
        task = self.store.create_task({"image_refs": [], "config": {}})
        self.assertEqual(self.store.append_event(task["id"], {"type": "status", "message": "x"}), 1)
        self.assertEqual(self.store.append_event(task["id"], {"type": "done"}), 2)
        self.assertEqual([row["seq"] for row in self.store.read_events(task["id"])], [1, 2])
        with self.assertRaises(Exception):
            self.store.append_event(task["id"], {"type": "nope"})

    def test_unknown_status_converges_to_interrupted(self):
        task = self.store.create_task({"image_refs": [], "config": {}})
        path = self.store._record_path(task["id"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "bogus"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.store.get_task(task["id"])["status"], "interrupted")

    def test_referenced_paths_only_for_protected_statuses(self):
        task = self.store.create_task({"image_refs": [{"path": "C:/d/uploads/a.png"}], "config": {}})
        self.assertTrue(self.store.referenced_upload_paths())
        self.store.update_task(task["id"], status="done")
        self.assertEqual(self.store.referenced_upload_paths(), set())


class LabelerExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _records(self):
        return [{
            "name": "a.png", "relative_path": "d/a.png", "tags": ["=cmd", "cat"],
            "confidences": [0.9, 0.5], "description": "一只猫", "preset": "face_lora",
            "output_mode": "both", "model_name": "m", "created_at": 1700000000000,
        }]

    def test_json_contains_confidences(self):
        payload = json.loads(render(self._records(), "json").decode("utf-8"))
        self.assertEqual(payload[0]["confidences"], [0.9, 0.5])

    def test_csv_has_bom_and_formula_guard(self):
        text = render(self._records(), "csv").decode("utf-8-sig")
        self.assertTrue(render(self._records(), "csv").startswith(b"\xef\xbb\xbf"))
        self.assertIn("'=cmd", text)
        self.assertIn("d/a.png", text)

    def test_txt_is_comma_separated(self):
        text = render(self._records(), "txt").decode("utf-8")
        self.assertEqual(text.strip(), "d/a.png, =cmd, cat")

    def test_write_export_stays_in_managed_dir(self):
        artifact = write_export(self.dir / "exports", self._records(), "json", label="../../etc/passwd")
        path = Path(artifact["path"]).resolve()
        self.assertTrue(str(path).startswith(str((self.dir / "exports").resolve())))
        self.assertTrue(path.is_file())

    def test_unknown_format_rejected(self):
        with self.assertRaises(LabelerExportError):
            render(self._records(), "xml")

    def test_captions_zip_matches_image_names(self):
        records = [
            {"relative_path": "dataset/sub/a.png", "name": "a.png",
             "tags": ["cat", "red dress"], "description": ""},
            {"relative_path": "dataset/b.jpg", "name": "b.jpg",
             "tags": [], "description": "一只猫。", "output_mode": "description"},
            {"relative_path": "dataset/sub/a.png", "name": "a.png",
             "tags": ["dog"], "description": ""},
        ]
        payload = render(records, "captions")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            self.assertIn("dataset/sub/a.txt", names)
            self.assertIn("dataset/b.txt", names)
            self.assertIn("dataset/sub/a-2.txt", names)
            self.assertEqual(archive.read("dataset/sub/a.txt").decode("utf-8"), "cat, red dress")
            self.assertEqual(archive.read("dataset/b.txt").decode("utf-8"), "一只猫。")

    def test_captions_export_uses_zip_extension(self):
        artifact = write_export(self.dir / "exports", self._records(), "captions", label="train")
        self.assertTrue(artifact["path"].endswith(".zip"))
        self.assertEqual(artifact["format"], "captions")

    def test_write_export_auto_names_by_folder(self):
        # 自动命名 = 文件夹本来的名字（relative_path 首段，内部子目录不影响）；
        # 重复导出同一文件夹补 -2，绝不覆盖。
        records = [
            {"relative_path": "数据集A/pos/a.png", "name": "a.png", "tags": ["cat"]},
            {"relative_path": "数据集A/neg/b.png", "name": "b.png", "tags": ["dog"]},
        ]
        artifact = write_export(self.dir / "exports", records, "captions", label="全部标注")
        self.assertEqual(Path(artifact["path"]).name, "数据集A.zip")
        artifact2 = write_export(self.dir / "exports", records, "captions", label="全部标注")
        self.assertEqual(Path(artifact2["path"]).name, "数据集A-2.zip")

    def test_write_export_custom_name_wins(self):
        # 用户填了包名就叫用户的名字；顺手把扩展名一起填的会被剥掉，
        # 由格式决定扩展名；撞名补 -2。
        artifact = write_export(self.dir / "exports3", self._records(), "captions",
                                custom_name="我的包.zip")
        self.assertEqual(Path(artifact["path"]).name, "我的包.zip")
        artifact2 = write_export(self.dir / "exports3", self._records(), "captions",
                                 custom_name="我的包")
        self.assertEqual(Path(artifact2["path"]).name, "我的包-2.zip")
        # 包名同样过安全清洗：路径段进不了文件名。
        artifact3 = write_export(self.dir / "exports4", self._records(), "json",
                                 custom_name="../../etc/passwd")
        path = Path(artifact3["path"]).resolve()
        self.assertTrue(str(path).startswith(str((self.dir / "exports4").resolve())))

    def test_write_export_without_folder_falls_back(self):
        # 顶层散图/跨文件夹散选没有共同文件夹名：回落到「标注-时间戳」老命名。
        records = [
            {"relative_path": "a.png", "name": "a.png", "tags": ["cat"]},
            {"relative_path": "其他集/b.png", "name": "b.png", "tags": ["dog"]},
        ]
        artifact = write_export(self.dir / "exports5", records, "captions", label="选中标注")
        name = Path(artifact["path"]).name
        self.assertTrue(name.startswith("选中标注-"), name)
        self.assertTrue(name.endswith(".zip"), name)

    def test_open_export_dir_creates_managed_folder(self):
        from naiba.labeler import export as export_module

        with mock.patch.object(export_module, "_open_folder") as opener:
            path = open_export_dir(self.dir / "exports")
        self.assertTrue(Path(path).is_dir())
        opener.assert_called_once()

    def test_reveal_exports_returns_managed_path(self):
        from naiba.labeler.service import LabelerService

        service = LabelerService.__new__(LabelerService)
        service._exports_dir = self.dir / "exports"
        with mock.patch("naiba.labeler.service.open_export_dir",
                        return_value=str(service._exports_dir)) as opener:
            payload, status = service.api_reveal_exports()
        self.assertEqual(status, 200)
        self.assertEqual(payload["path"], str(service._exports_dir))
        opener.assert_called_once_with(service._exports_dir)

    def test_export_folder_button_wired_in_ui(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "public/index.html").read_text(encoding="utf-8")
        js = (root / "public/js/23-labeler.js").read_text(encoding="utf-8")
        self.assertIn('id="labelerOpenExports"', html)
        self.assertIn('value="captions"', html)
        self.assertIn("/api/labeler/exports/open", js)
        self.assertIn("#labelerOpenExports", js)


class LabelerSentenceTests(unittest.TestCase):
    def test_split_chinese_and_english(self):
        sentences = split_sentences("天空是蓝色的。地面有草！Hello world. 再来一句")
        self.assertIn("天空是蓝色的。", sentences)
        self.assertIn("地面有草！", sentences)
        self.assertTrue(any("Hello world." in item for item in sentences))

    def test_stats_marks_degraded_and_counts(self):
        stats = sentence_stats([{"description": "一只猫。一只猫。天空很蓝。"}])
        self.assertTrue(stats["degraded"])
        items = {row["sentence"]: row["count"] for row in stats["items"]}
        self.assertEqual(items["一只猫。"], 2)

    def test_query_filter(self):
        stats = sentence_stats([{"description": "一只猫。天空很蓝。"}], query="猫")
        self.assertEqual(len(stats["items"]), 1)


def _png_bytes(color=(90, 30, 200), size=(12, 12)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class LabelerOriginalsTests(unittest.TestCase):
    """原图留存：内容寻址、幂等、路径防穿越、生命周期清理。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_archive_is_content_addressed_and_idempotent(self):
        data = _png_bytes()
        first = archive_original(self.dir, data)
        second = archive_original(self.dir, data)
        self.assertEqual(first["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(first["size"], len(data))
        path = Path(first["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.name, first["sha256"][:2])
        self.assertEqual(path.read_bytes(), data)

    def test_original_path_rejects_bad_digest(self):
        data = _png_bytes()
        archive_original(self.dir, data)
        digest = hashlib.sha256(data).hexdigest()
        self.assertIsNotNone(original_path(self.dir, digest))
        self.assertIsNotNone(original_path(self.dir, digest.upper()))
        for bad in ("", "abc", "../etc/passwd", f"{digest}extra", 123):
            self.assertIsNone(original_path(self.dir, bad))

    def test_prune_keeps_referenced_and_young_files(self):
        keep_data, drop_data = _png_bytes((1, 2, 3)), _png_bytes((4, 5, 6))
        keep = archive_original(self.dir, keep_data)
        drop = archive_original(self.dir, drop_data)
        old = time.time() - 25 * 60 * 60
        os.utime(drop["path"], (old, old))
        stats = prune_unreferenced(self.dir, {keep["sha256"]})
        self.assertEqual(stats["removed"], 1)
        self.assertTrue(Path(keep["path"]).is_file())
        self.assertFalse(Path(drop["path"]).is_file())

    def test_prune_spares_unreferenced_but_recent(self):
        data = _png_bytes((9, 9, 9))
        fresh = archive_original(self.dir, data)
        stats = prune_unreferenced(self.dir, set())
        self.assertEqual(stats["removed"], 0)
        self.assertTrue(Path(fresh["path"]).is_file())

    def test_prune_leaves_unrecognized_files_alone(self):
        stray = self.dir / "labeler" / "originals" / "zz" / "not-a-digest.txt"
        stray.parent.mkdir(parents=True)
        stray.write_text("keep me", encoding="utf-8")
        prune_unreferenced(self.dir, set())
        self.assertTrue(stray.is_file())

    def test_service_prune_originals_uses_db_references(self):
        db = LabelerDB(self.dir)
        data = _png_bytes((7, 7, 7))
        archived = archive_original(self.dir, data)
        stale = archive_original(self.dir, _png_bytes((8, 8, 8)))
        os.utime(stale["path"], (time.time() - 25 * 60 * 60,) * 2)
        db.complete_item(
            "t1", "it1",
            annotation={
                "path_key": "k", "content_version": "1:1", "path": "/x.png",
                "relative_path": "d/a.png", "name": "a.png",
                "original_sha256": archived["sha256"],
                "engine": "provider", "output_mode": "keywords",
                "model_key": "m", "model_name": "m",
                "tags": [], "confidences": [], "description": "",
            },
            usage={}, config_fingerprint="cfg",
        )
        service = LabelerService.__new__(LabelerService)
        service.app = _StubApp(self.dir)
        service.db = db
        stats = service._prune_originals()
        self.assertEqual(stats["removed"], 1)
        self.assertTrue(Path(archived["path"]).is_file())
        self.assertEqual(
            service.db.referenced_original_digests(), {archived["sha256"]}
        )


class LabelerLoraPackageTests(unittest.TestCase):
    """LoRA 训练包：短名配对、原字节保真、manifest 可追溯、缺口整包拒绝。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.uploads = self.dir / "uploads"
        self.original = _png_bytes((10, 20, 30))
        self.digest = hashlib.sha256(self.original).hexdigest()
        self.archived = archive_original(self.dir, self.original)

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self, *, relative_path, name, path=None, digest=None,
                path_key=None, content_version="1:1", ann_id=1, tags=None,
                description="", task_id="tg-x"):
        return {
            "id": ann_id,
            "source_task_id": task_id,
            "path": str(path or self.uploads / "cache.bin"),
            "path_key": path_key or f"key-{ann_id}",
            "content_version": content_version,
            "relative_path": relative_path,
            "name": name,
            "original_sha256": self.digest if digest is None else digest,
            "tags": tags if tags is not None else ["cat"],
            "confidences": [0.9] if tags else [],
            "description": description,
        }

    def test_same_content_two_sources_exports_two_pairs(self):
        # 同内容、不同来源：两条样本各自导出图片与 caption（用户拍板的口径）。
        records = [
            self._record(relative_path="folder-one/a.png", name="a.png", ann_id=1),
            self._record(relative_path="folder-two/a.png", name="a.png", ann_id=2),
        ]
        payload = render_lora_package(records, self.dir)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            self.assertIn("000001.png", names)
            self.assertIn("000001.txt", names)
            self.assertIn("000002.png", names)
            self.assertIn("000002.txt", names)
            self.assertIn("manifest.json", names)
            self.assertEqual(archive.read("000001.png"), self.original)
            self.assertEqual(archive.read("000002.png"), self.original)
            self.assertEqual(archive.read("000001.txt"), b"cat")
            self.assertEqual(archive.read("000002.txt"), b"cat")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        self.assertEqual(manifest["count"], 2)
        self.assertEqual([e["image"] for e in manifest["entries"]],
                         ["000001.png", "000002.png"])
        for entry in manifest["entries"]:
            self.assertEqual(entry["sha256"], self.digest)
            self.assertEqual(entry["size"], len(self.original))
            self.assertEqual(entry["caption_source"], "tags")
        self.assertEqual(manifest["entries"][0]["relative_path"], "folder-one/a.png")
        self.assertEqual(manifest["entries"][1]["relative_path"], "folder-two/a.png")

    def test_caption_prefers_tags_then_description(self):
        records = [
            self._record(relative_path="a.png", name="a.png", ann_id=1, tags=["cat", "dog"]),
            self._record(relative_path="b.png", name="b.png", ann_id=2, tags=[],
                         description="一只猫在睡觉。"),
            self._record(relative_path="c.png", name="c.png", ann_id=3, tags=[],
                         description=""),
        ]
        payload = render_lora_package(records, self.dir)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.read("000001.txt").decode("utf-8"), "cat, dog")
            self.assertEqual(archive.read("000002.txt").decode("utf-8"), "一只猫在睡觉。")
            self.assertEqual(archive.read("000003.txt").decode("utf-8"), "")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        sources = [entry["caption_source"] for entry in manifest["entries"]]
        self.assertEqual(sources, ["tags", "description", ""])

    def test_naming_is_stable_and_follows_fixed_order(self):
        records = [
            self._record(relative_path="b.png", name="b.png", ann_id=9),
            self._record(relative_path="a.png", name="a.png", ann_id=2),
        ]
        first = render_lora_package(records, self.dir)
        second = render_lora_package(list(reversed(records)), self.dir)
        # 命名与顺序稳定（ZIP 头/manifest 里的时间戳会随渲染时刻变化，比语义内容）。
        with zipfile.ZipFile(io.BytesIO(first)) as a, zipfile.ZipFile(io.BytesIO(second)) as b:
            self.assertEqual(a.namelist(), b.namelist())
            for name in a.namelist():
                if name == "manifest.json":
                    continue
                self.assertEqual(a.read(name), b.read(name), name)
            manifest_a = json.loads(a.read("manifest.json").decode("utf-8"))
            manifest_b = json.loads(b.read("manifest.json").decode("utf-8"))
            manifest_a.pop("created_at")
            manifest_b.pop("created_at")
            self.assertEqual(manifest_a, manifest_b)
        self.assertEqual([e["relative_path"] for e in manifest_a["entries"]], ["a.png", "b.png"])
        self.assertEqual([e["annotation_id"] for e in manifest_a["entries"]], [2, 9])

    def test_extension_follows_source_image(self):
        original = _png_bytes((3, 4, 5))
        digest = hashlib.sha256(original).hexdigest()
        archive_original(self.dir, original)
        record = self._record(relative_path="a.webp", name="a.webp", digest=digest)
        payload = render_lora_package([record], self.dir)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIn("000001.webp", archive.namelist())
            self.assertIn("000001.txt", archive.namelist())

    def test_legacy_record_without_digest_rejects_whole_package(self):
        records = [
            self._record(relative_path="a.png", name="a.png", ann_id=1),
            self._record(relative_path="b.png", name="b.png", ann_id=2, digest=""),
        ]
        with self.assertRaises(LabelerExportError) as ctx:
            render_lora_package(records, self.dir)
        self.assertIn("没有原图记录", str(ctx.exception))
        self.assertIn("重新打标", str(ctx.exception))

    def test_same_sample_twice_rejects_whole_package(self):
        records = [
            self._record(relative_path="a.png", name="a.png", ann_id=1,
                         path_key="same-key"),
            self._record(relative_path="a.png", name="a.png", ann_id=2,
                         path_key="same-key"),
        ]
        with self.assertRaises(LabelerExportError) as ctx:
            render_lora_package(records, self.dir)
        self.assertIn("多条记录", str(ctx.exception))

    def test_missing_original_everywhere_rejects(self):
        Path(self.archived["path"]).unlink()
        record = self._record(relative_path="a.png", name="a.png",
                              path=self.uploads / "gone.png")
        with self.assertRaises(LabelerExportError) as ctx:
            render_lora_package([record], self.dir)
        self.assertIn("原图文件已缺失", str(ctx.exception))

    def test_scaled_cache_image_cannot_replace_original(self):
        # 留存池里没有原图；缓存图是缩放过的另一份字节：摘要对不上，必须拒绝。
        Path(self.archived["path"]).unlink()
        scaled = _png_bytes((10, 20, 30), size=(6, 6))
        cache = self.uploads / "scaled.png"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(scaled)
        self.assertNotEqual(hashlib.sha256(scaled).hexdigest(), self.digest)
        record = self._record(relative_path="a.png", name="a.png", path=cache)
        with self.assertRaises(LabelerExportError):
            render_lora_package([record], self.dir)

    def test_cache_image_with_matching_bytes_is_accepted(self):
        # 留存池被清但缓存图字节与原图一致（未缩放）：按摘要校验通过后可用。
        cache = self.uploads / "same.png"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(self.original)
        Path(self.archived["path"]).unlink()
        record = self._record(relative_path="a.png", name="a.png", path=cache)
        payload = render_lora_package([record], self.dir)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.read("000001.png"), self.original)

    def test_write_export_lora_names_zip_and_atomic_rejection(self):
        exports = self.dir / "exports"
        artifact = write_export(exports, [self._record(relative_path="a.png", name="a.png")],
                                "lora", label="任务x", data_dir=self.dir)
        self.assertTrue(Path(artifact["path"]).name.startswith("lora-任务x"))
        self.assertTrue(artifact["path"].endswith(".zip"))
        # 整包拒绝：渲染失败时目录里不能留下半个包。
        exports2 = self.dir / "exports2"
        with self.assertRaises(LabelerExportError):
            write_export(exports2, [self._record(relative_path="b.png", name="b.png",
                                                 digest="")], "lora")
        self.assertEqual(list(exports2.glob("*")) if exports2.exists() else [], [])

    def test_write_export_lora_auto_names_by_folder_with_prefix(self):
        # lora 训练包自动命名 = 文件夹本来的名字，保留 lora- 前缀与
        # 同文件夹的逐图 .txt 包区分开。
        artifact = write_export(
            self.dir / "exports-lora-folder",
            [self._record(relative_path="数据集A/sub/a.png", name="a.png")],
            "lora", label="任务x", data_dir=self.dir)
        self.assertEqual(Path(artifact["path"]).name, "lora-数据集A.zip")

    def test_write_export_lora_custom_name_has_no_prefix(self):
        # 用户自己命名的训练包不加 lora- 前缀：填什么就叫什么。
        artifact = write_export(
            self.dir / "exports-lora-custom",
            [self._record(relative_path="数据集A/a.png", name="a.png")],
            "lora", label="任务x", data_dir=self.dir, custom_name="我的训练包")
        self.assertEqual(Path(artifact["path"]).name, "我的训练包.zip")


class LabelerUploadDigestTests(unittest.TestCase):
    """原图摘要贯穿：上传留存 → refs 校验 → 落库 → 标注行 → 导出可用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.uploads = self.dir / "uploads"

    def tearDown(self):
        self.tmp.cleanup()

    def test_validate_refs_carries_digest_and_rejects_bad_format(self):
        data = _png_bytes()
        first = _write_png(self.uploads / "d1" / "a.png")
        second = _write_png(self.uploads / "d2" / "a.png")
        first.write_bytes(data)
        second.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        refs = validate_image_refs(self.dir, [
            {"path": str(first), "relative_path": "f1/a.png", "original_sha256": digest},
            {"path": str(second), "relative_path": "f2/a.png", "original_sha256": digest.upper()},
        ])
        self.assertEqual(len(refs), 2)
        self.assertEqual([ref["original_sha256"] for ref in refs], [digest, digest])
        self.assertNotEqual(refs[0]["path_key"], refs[1]["path_key"])
        with self.assertRaises(LabelerImageError):
            validate_image_refs(self.dir, [
                {"path": str(first), "relative_path": "f1/a.png",
                 "original_sha256": "not-a-digest"},
            ])

    def test_service_upload_archives_original_without_dedup(self):
        service = LabelerService.__new__(LabelerService)
        service.app = _StubApp(self.dir)
        data = _png_bytes((42, 1, 2))
        digest = hashlib.sha256(data).hexdigest()
        spool_a = self.dir / "a.part"
        spool_a.write_bytes(data)
        payload_a, status_a = service.api_upload(str(spool_a), "a.png")
        self.assertEqual(status_a, 200)
        self.assertEqual(payload_a["original_sha256"], digest)
        self.assertEqual(payload_a["original_size"], len(data))
        self.assertFalse(payload_a["deduped"])
        # 同内容第二个来源：缓存副本不去重（两条样本），原图留存复用同一份字节。
        spool_b = self.dir / "b.part"
        spool_b.write_bytes(data)
        payload_b, status_b = service.api_upload(str(spool_b), "b.png")
        self.assertEqual(status_b, 200)
        self.assertNotEqual(payload_a["path"], payload_b["path"])
        self.assertTrue(Path(payload_a["path"]).is_file())
        self.assertTrue(Path(payload_b["path"]).is_file())
        self.assertTrue(Path(payload_a["original_sha256"]) is not None)
        archived = original_path(self.dir, digest)
        self.assertIsNotNone(archived)
        self.assertEqual(archived.read_bytes(), data)
        self.assertFalse(spool_a.exists())
        self.assertFalse(spool_b.exists())

    def test_service_upload_rejects_non_image(self):
        service = LabelerService.__new__(LabelerService)
        service.app = _StubApp(self.dir)
        spool = self.dir / "x.part"
        spool.write_bytes(b"not an image")
        payload, status = service.api_upload(str(spool), "x.png")
        self.assertEqual(status, 400)
        self.assertIn("不是可读取", payload["error"])
        self.assertEqual(list((self.dir / "labeler" / "originals").rglob("*")) or [], [])

    def test_complete_item_persists_digest_and_reuse_fills_blank(self):
        db = LabelerDB(self.dir)
        data = _png_bytes((5, 5, 5))
        digest = hashlib.sha256(data).hexdigest()
        db.add_items("t1", [{
            "item_id": "it1", "path": "/x.png", "path_key": "k1",
            "content_version": "10:20", "relative_path": "d/a.png", "name": "a.png",
            "size": len(data), "mtime_ns": 20, "original_sha256": digest,
        }], "cfg")
        annotation = {
            "path_key": "k1", "content_version": "10:20", "path": "/x.png",
            "relative_path": "d/a.png", "name": "a.png",
            "original_sha256": digest,
            "engine": "provider", "output_mode": "keywords",
            "model_key": "m", "model_name": "m",
            "tags": ["cat"], "confidences": [0.9], "description": "",
        }
        ann_id, reused = db.complete_item("t1", "it1", annotation=annotation,
                                          usage={}, config_fingerprint="cfg")
        self.assertFalse(reused)
        rows = db.annotations(task_id="t1")
        self.assertEqual(rows[0]["original_sha256"], digest)
        items = db.list_items("t1")
        self.assertEqual(items[0]["original_sha256"], digest)
        # 旧版本产出的行（摘要为空）被重跑复用时补齐，不覆盖已有值。
        blank = dict(annotation, original_sha256="")
        ann_id2, reused2 = db.complete_item("t2", "it2", annotation=blank,
                                            usage={}, config_fingerprint="cfg")
        self.assertTrue(reused2)
        self.assertEqual(ann_id2, ann_id)
        self.assertEqual(db.annotations_by_ids([ann_id])[0]["original_sha256"], digest)
        newer = dict(annotation, original_sha256=hashlib.sha256(b"other").hexdigest())
        db.complete_item("t3", "it3", annotation=newer, usage={},
                         config_fingerprint="cfg")
        self.assertEqual(db.annotations_by_ids([ann_id])[0]["original_sha256"], digest)

    def test_legacy_database_gets_digest_columns(self):
        # 模拟旧库：没有 original_sha256 列也能打开并自动补列。
        root = self.dir / "labeler"
        root.mkdir(parents=True)
        connection = sqlite3.connect(root / "labeler.db")
        connection.executescript(
            """
            CREATE TABLE task_items (
                task_id TEXT NOT NULL, item_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL,
                path_key TEXT NOT NULL, content_version TEXT NOT NULL,
                relative_path TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0, mtime_ns INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'queued',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                error_category TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                usage_json TEXT NOT NULL DEFAULT '{}', annotation_id INTEGER,
                config_fingerprint TEXT NOT NULL DEFAULT '',
                started_at INTEGER, finished_at INTEGER,
                PRIMARY KEY (task_id, item_id)
            );
            CREATE TABLE annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL, source_task_id TEXT NOT NULL,
                path_key TEXT NOT NULL, content_version TEXT NOT NULL,
                path TEXT NOT NULL, relative_path TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '', engine TEXT NOT NULL,
                output_mode TEXT NOT NULL, preset TEXT,
                prompt_version INTEGER NOT NULL DEFAULT 1,
                model_key TEXT NOT NULL, model_name TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
                confidences_json TEXT NOT NULL DEFAULT '[]',
                description TEXT NOT NULL DEFAULT '',
                usage_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            """
        )
        connection.commit()
        connection.close()
        db = LabelerDB(self.dir)
        digest = hashlib.sha256(_png_bytes()).hexdigest()
        db.add_items("t1", [{
            "item_id": "it1", "path": "/x", "path_key": "k",
            "content_version": "1:1", "relative_path": "a.png", "name": "a.png",
            "size": 1, "mtime_ns": 1, "original_sha256": digest,
        }], "cfg")
        db.complete_item(
            "t1", "it1",
            annotation={
                "path_key": "k", "content_version": "1:1", "path": "/x",
                "relative_path": "a.png", "name": "a.png",
                "original_sha256": digest,
                "engine": "provider", "output_mode": "keywords",
                "model_key": "m", "model_name": "m",
                "tags": [], "confidences": [], "description": "",
            },
            usage={}, config_fingerprint="cfg",
        )
        self.assertEqual(db.annotations(task_id="t1")[0]["original_sha256"], digest)

    def test_runner_annotation_carries_item_digest(self):
        item = {"path_key": "k", "content_version": "1:1", "path": "/x.png",
                "relative_path": "d/a.png", "name": "a.png",
                "original_sha256": "a" * 64}
        config = {"engine": "provider", "output_mode": "keywords", "preset": "",
                  "prompt_version": 1, "model_key": "m", "model_name": "m"}
        reusable = {"engine": "provider", "output_mode": "keywords", "preset": "",
                    "prompt_version": 1, "model_key": "m", "model_name": "m",
                    "tags": ["cat"], "confidences": [0.9], "description": ""}
        merged = LabelerRunner._annotation_from_reusable(reusable, item, config)
        self.assertEqual(merged["original_sha256"], "a" * 64)

    def test_service_export_lora_end_to_end_from_db(self):
        db = LabelerDB(self.dir)
        data = _png_bytes((11, 22, 33))
        digest = hashlib.sha256(data).hexdigest()
        archived = archive_original(self.dir, data)
        cache = self.uploads / "d" / "a.png"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(data)
        db.add_items("tg-1", [{
            "item_id": "it-1", "path": str(cache), "path_key": "k1",
            "content_version": "1:1", "relative_path": "set/a.png", "name": "a.png",
            "size": len(data), "mtime_ns": 1, "original_sha256": digest,
        }], "cfg")
        db.complete_item(
            "tg-1", "it-1",
            annotation={
                "path_key": "k1", "content_version": "1:1", "path": str(cache),
                "relative_path": "set/a.png", "name": "a.png",
                "original_sha256": digest,
                "engine": "provider", "output_mode": "keywords",
                "model_key": "m", "model_name": "m",
                "tags": ["cat"], "confidences": [0.9], "description": "",
            },
            usage={}, config_fingerprint="cfg",
        )
        service = LabelerService.__new__(LabelerService)
        service.app = _StubApp(self.dir)
        service.db = db
        service._exports_dir = self.dir / "labeler" / "exports"
        rows = db.annotations(task_id="tg-1")
        payload, status = service.api_export(
            {"format": "lora", "annotation_ids": [rows[0]["id"]]}
        )
        self.assertEqual(status, 201)
        self.assertTrue(payload["path"].endswith(".zip"))
        with zipfile.ZipFile(payload["path"]) as archive:
            self.assertEqual(archive.read("000001.png"), data)
            self.assertEqual(archive.read("000001.txt"), b"cat")
        # 历史遗留（无摘要）记录导出训练包：整包拒绝且错误可读。
        db2 = LabelerDB(self.dir / "legacy")
        db2.add_items("tg-2", [{
            "item_id": "it-1", "path": str(cache), "path_key": "k2",
            "content_version": "1:1", "relative_path": "old/a.png", "name": "a.png",
            "size": 1, "mtime_ns": 1, "original_sha256": "",
        }], "cfg")
        db2.complete_item(
            "tg-2", "it-1",
            annotation={
                "path_key": "k2", "content_version": "1:1", "path": str(cache),
                "relative_path": "old/a.png", "name": "a.png",
                "original_sha256": "",
                "engine": "provider", "output_mode": "keywords",
                "model_key": "m", "model_name": "m",
                "tags": ["cat"], "confidences": [0.9], "description": "",
            },
            usage={}, config_fingerprint="cfg",
        )
        service2 = LabelerService.__new__(LabelerService)
        service2.app = _StubApp(self.dir)
        service2.db = db2
        service2._exports_dir = self.dir / "labeler" / "exports"
        rows2 = db2.annotations(task_id="tg-2")
        payload2, status2 = service2.api_export(
            {"format": "lora", "annotation_ids": [rows2[0]["id"]]}
        )
        self.assertEqual(status2, 400)
        self.assertIn("没有原图记录", payload2["error"])

    def test_training_package_ui_wired(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "public/index.html").read_text(encoding="utf-8")
        js = (root / "public/js/23-labeler.js").read_text(encoding="utf-8")
        self.assertIn('value="lora"', html)
        self.assertIn("LoRA 训练包", html)
        self.assertIn("/api/labeler/uploads", js)
        self.assertIn("original_sha256", js)


class LabelerTaskDeleteTests(unittest.TestCase):
    """删除与一键清理（docs/plans/2026-10-26-打标器任务删除与清理计划.md）。

    口径：删任务 JSON + 事件文件 + task_items；annotations 全局复用、保留可导出；
    排队/打标中的跳过并回报（打标按图计费，取消必须显式）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.uploads = self.data_dir / "uploads"
        self.images = [
            _write_png(self.uploads / "d" / f"del{i}.png", color=(30 * i, 90, 210))
            for i in range(2)
        ]
        self.store = LabelerStore(self.data_dir)
        self.db = LabelerDB(self.data_dir)
        app = _StubApp(self.data_dir)
        runner = LabelerRunner(app, self.store, self.db)
        self.addCleanup(runner.shutdown)
        self.jobs = LabelerJobManager(app, self.store, self.db, runner)

    def _submit(self, images=None):
        refs = [
            {"path": str(path), "name": path.name, "relative_path": f"d/{path.name}"}
            for path in (images if images is not None else self.images)
        ]
        return self.jobs.submit({"image_refs": refs, "config": _base_config()})

    def _wait(self, task_id, timeout=10.0):
        deadline = time.time() + timeout
        state = None
        while time.time() < deadline:
            state = self.store.get_task(task_id)
            if state and state.get("status") in {"done", "failed", "cancelled", "interrupted"}:
                return state
            time.sleep(0.02)
        return state

    def test_store_delete_task_removes_record_and_events(self):
        task = self.store.create_task({"image_refs": [], "config": {}})
        self.store.append_event(task["id"], {"type": "status", "message": "queued"})
        self.assertTrue(self.store.delete_task(task["id"]))
        self.assertIsNone(self.store.get_task(task["id"]))
        self.assertFalse((self.data_dir / "labeler" / "tasks" / f"{task['id']}.json").exists())
        self.assertFalse((self.data_dir / "labeler" / "tasks" / f"{task['id']}.events.jsonl").exists())

    def test_store_delete_task_rejects_non_task_ids(self):
        self.assertFalse(self.store.delete_task("../config.json"))
        self.assertFalse(self.store.delete_task(""))
        self.assertFalse(self.store.delete_task("nonexistent-id"))

    def test_delete_finished_task_keeps_annotations(self):
        task = self._submit()
        self._wait(task["id"])
        annotation_ids = self.db.annotation_ids_for_task(task["id"])
        self.assertTrue(annotation_ids)
        result = self.jobs.delete_tasks([task["id"]])
        self.assertEqual(result, {"deleted": 1, "skipped": []})
        self.assertIsNone(self.store.get_task(task["id"]))
        self.assertEqual(self.db.counts(task["id"])["total"], 0)
        # 标注库保留：可按 id 读取，导出与查询不受任务删除影响。
        rows = self.db.annotations_by_ids(annotation_ids)
        self.assertEqual(len(rows), len(annotation_ids))

    def test_delete_active_task_is_skipped_and_intact(self):
        release = threading.Event()

        def behavior(profile, messages, options, status):
            release.wait(timeout=10)
            return '{"tags":[{"tag":"cat","confidence":0.9}],"description":""}'

        app = _StubApp(self.data_dir, models=_StubModels(behavior=behavior))
        runner = LabelerRunner(app, self.store, self.db)
        self.addCleanup(runner.shutdown)
        jobs = LabelerJobManager(app, self.store, self.db, runner)
        refs = [{"path": str(path), "name": path.name} for path in self.images]
        task = jobs.submit({"image_refs": refs, "config": _base_config()})
        result = jobs.delete_tasks([task["id"]])
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["skipped"], [task["id"]])
        self.assertIsNotNone(self.store.get_task(task["id"]))
        self.assertGreater(self.db.counts(task["id"])["total"], 0)
        release.set()
        self._wait(task["id"])

    def test_delete_without_ids_clears_all_finished_only(self):
        done = self._submit(self.images[:1])
        self._wait(done["id"])
        release = threading.Event()

        def behavior(profile, messages, options, status):
            release.wait(timeout=10)
            return '{"tags":[{"tag":"cat","confidence":0.9}],"description":""}'

        app = _StubApp(self.data_dir, models=_StubModels(behavior=behavior))
        runner = LabelerRunner(app, self.store, self.db)
        self.addCleanup(runner.shutdown)
        jobs = LabelerJobManager(app, self.store, self.db, runner)
        refs = [{"path": str(path), "name": path.name} for path in self.images]
        active = jobs.submit({"image_refs": refs, "config": _base_config()})
        result = jobs.delete_tasks(None)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["skipped"], [active["id"]])
        self.assertIsNone(self.store.get_task(done["id"]))
        self.assertIsNotNone(self.store.get_task(active["id"]))
        release.set()
        self._wait(active["id"])

    def test_delete_ignores_unknown_ids(self):
        result = self.jobs.delete_tasks(["tg-does-not-exist"])
        self.assertEqual(result, {"deleted": 0, "skipped": []})

    def test_service_delete_contract(self):
        service = LabelerService.__new__(LabelerService)
        service.app = _StubApp(self.data_dir)
        service.store = self.store
        service.db = self.db
        service.jobs = self.jobs
        task = self._submit(self.images[:1])
        self._wait(task["id"])
        payload, status = service.api_tasks_delete({"task_ids": [task["id"]]})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["deleted"], 1)
        self.assertEqual(payload["skipped"], [])
        # 不带 task_ids = 清理全部已结束。
        task2 = self._submit(self.images[1:])
        self._wait(task2["id"])
        payload, status = service.api_tasks_delete({})
        self.assertEqual(status, 200)
        self.assertEqual(payload["deleted"], 1)
        # 标注库繁忙如实回报 503，不谎报成功。
        with mock.patch.object(self.jobs, "delete_tasks", side_effect=LabelerDBError("busy")):
            payload, status = service.api_tasks_delete({})
        self.assertEqual(status, 503)
        self.assertIn("标注库繁忙", payload["error"])

    def test_delete_ui_wired(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "public/index.html").read_text(encoding="utf-8")
        js = (root / "public/js/23-labeler.js").read_text(encoding="utf-8")
        http = (root / "naiba/http.py").read_text(encoding="utf-8")
        self.assertIn('id="labelerClearFinished"', html)
        self.assertIn("清理已完成", html)
        self.assertIn("labeler-batch-delete", js)
        self.assertIn("TERMINAL_BATCH_STATUSES", js)
        self.assertIn("clearSelectedBatch", js)
        self.assertIn('method: "DELETE"', js)
        delete_section = http.split("def do_DELETE", 1)[1].split("def do_PATCH", 1)[0]             if "def do_PATCH" in http.split("def do_DELETE", 1)[1]             else http.split("def do_DELETE", 1)[1]
        self.assertIn('path == "/api/labeler/tasks"', delete_section)



if __name__ == "__main__":
    unittest.main()
