# -*- coding: utf-8 -*-
"""护栏：造梦机（docs/plans/2026-10-23-ComfyUI自动化工作台模块计划.md）。

覆盖六条主线（全部不联网，ComfyUI 用假 client）：
1. 参数扫描：槽位类型、连线跳过、seed/image 标记、原值类型保真应用；
2. 导入契约：API 格式放行、UI 格式带导出步骤拒绝、wrapper 兼容；
3. 存储：工作流/任务 CRUD、checkpoint 白名单、事件游标、产物目录删除；
4. 执行：两段任务逐段推进、提交失败带服务端原因、历史错误、丢失重提交预算、
   取消两步语义、恢复收养（已完成段不重复提交）、启动恢复；
5. 任务管理：overrides 校验、重试续跑、批量删除跳过执行中、删除工作流保护；
6. 门面：导入/详情/文件路径安全。
"""

import hashlib
import inspect
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.comfyui import client as comfyui_client  # noqa: E402
from naiba.comfyui import PARAMS_SCHEMA_VERSION  # noqa: E402
from naiba.comfyui.jobs import ComfyUIJobManager, ComfyUITaskError  # noqa: E402
from naiba.comfyui.params import apply_overrides, enrich_slots, media_kind_of_field, scan_params  # noqa: E402
from naiba.comfyui.runner import ComfyUIRunner  # noqa: E402
from naiba.comfyui.schema import (  # noqa: E402
    NodeSchemaProvider,
    input_is_optional,
    parse_autogrow_decl,
    parse_input_decl,
)
from naiba.comfyui.service import (  # noqa: E402
    ComfyUIService,
    UI_FORMAT_ERROR,
    normalize_api_workflow,
)
from naiba.comfyui.store import ComfyUIStore  # noqa: E402


def _sample_prompt() -> dict:
    return {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "model.safetensors"}, "_meta": {"title": "模型"}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "一只猫", "clip": ["4", 1]}, "_meta": {"title": "正向提示词"}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": 123, "steps": 20, "cfg": 7.5,
                         "sampler_name": "euler", "scheduler": "normal",
                         "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["5", 0]}, "_meta": {"title": "采样"}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "8": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "out", "images": ["9", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "bad", "clip": ["4", 1]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "10": {"class_type": "LoraLoader",
               "inputs": {"lora_name": "my.safetensors", "strength_model": 1.0,
                          "strength_clip": 1.0, "model": ["4", 0], "clip": ["4", 1]},
               "_meta": {"title": "LoRA"}},
    }


def _autogrow_decl(prefix: str, minimum: int = 0, maximum: int = 9,
                   template_required: bool = True) -> dict:
    """测试用 Autogrow 组声明（与真实 /object_info 形状一致）。"""
    return {
        "kind": "autogrow",
        "optional": True,
        "autogrow": {
            "prefix": prefix,
            "names": [],
            "min": minimum,
            "max": maximum,
            "template_required": template_required,
        },
    }


class _StubPaths:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)


class _StubApp:
    def __init__(self, data_dir: Path, comfyui_url: str = "http://127.0.0.1:9"):
        self.paths = _StubPaths(data_dir)
        self.config = type("C", (), {
            "data": {"comfyui_url": comfyui_url},
            "save": lambda self: None,
        })()


class _FakeResponse:
    """context manager 形状的假 HTTP 响应（供 _DIRECT_OPENER 打桩）。"""

    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CheckpointNamesTests(unittest.TestCase):
    """模型候选提取：解析 /object_info 单节点响应；失败一律空列表。"""

    def test_parses_enum_choices_deduped_and_sorted(self):
        payload = {"CheckpointLoaderSimple": {"input": {"required": {
            "ckpt_name": [["b.safetensors", "a.safetensors", "a.safetensors"], {}]}}}}
        with mock.patch.object(comfyui_client, "_DIRECT_OPENER") as opener:
            opener.open.return_value = _FakeResponse(payload)
            names = comfyui_client.checkpoint_names("http://127.0.0.1:1")
        self.assertEqual(names, ["a.safetensors", "b.safetensors"])

    def test_failure_returns_empty(self):
        with mock.patch.object(comfyui_client, "_DIRECT_OPENER") as opener:
            opener.open.side_effect = OSError("down")
            self.assertEqual(comfyui_client.checkpoint_names("http://127.0.0.1:1"), [])
            self.assertEqual(comfyui_client.lora_names("http://127.0.0.1:1"), [])

    def test_malformed_payload_returns_empty(self):
        payloads = ([], {"CheckpointLoaderSimple": {}},
                    {"CheckpointLoaderSimple": {"input": {}}},
                    {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": []}}}})
        for payload in payloads:
            with mock.patch.object(comfyui_client, "_DIRECT_OPENER") as opener:
                opener.open.return_value = _FakeResponse(payload)
                self.assertEqual(comfyui_client.checkpoint_names("http://127.0.0.1:1"), [],
                                 payload)

    def test_lora_names_merge_both_loaders(self):
        # 两个标准 LoRA 加载器的 lora_name 枚举合并去重（同目录内容一致，
        # 工作流用的是第三方加载器时也能给对候选）。
        payloads = iter([
            {"LoraLoader": {"input": {"required": {
                "lora_name": [["b.safetensors", "a.safetensors"], {}]}}}},
            {"LoraLoaderModelOnly": {"input": {"required": {
                "lora_name": [["c.safetensors", "a.safetensors"], {}]}}}},
        ])
        with mock.patch.object(comfyui_client, "_DIRECT_OPENER") as opener:
            opener.open.side_effect = lambda url, timeout=10: _FakeResponse(next(payloads))
            names = comfyui_client.lora_names("http://127.0.0.1:1")
        self.assertEqual(names, ["a.safetensors", "b.safetensors", "c.safetensors"])


class ParamsTests(unittest.TestCase):
    def test_client_uses_no_proxy_opener(self):
        # 用户拍板（2026-10-24）：连 ComfyUI 一律直连，不吃任何代理——
        # 运行设置代理、系统代理、环境变量都不影响，与地址形状无关。
        # 空代理表的 ProxyHandler 没有 *_open 方法，build_opener 会把它丢出
        # 处理链——最终效果就是链上没有代理处理器，任何请求都直连。
        proxy_handlers = [h for h in comfyui_client._DIRECT_OPENER.handlers
                          if isinstance(h, urllib.request.ProxyHandler)]
        for handler in proxy_handlers:
            self.assertEqual(handler.proxies, {})
        # 回归警戒线：不许退回 urlopen（默认 opener 会读环境/系统代理）或 net_io 代理策略。
        source = inspect.getsource(comfyui_client)
        self.assertNotIn("urlopen", source)
        self.assertNotIn("net_io.open", source)

    def test_scan_types_and_connection_skip(self):
        slots = scan_params(_sample_prompt())
        kinds = {(s["node_id"], s["field_name"]): s["field_type"] for s in slots}
        self.assertEqual(kinds[("3", "seed")], "seed")
        self.assertEqual(kinds[("3", "cfg")], "number")
        self.assertEqual(kinds[("6", "text")], "text")
        self.assertEqual(kinds[("5", "batch_size")], "number")
        # 连线值（数组）不是控件槽位。
        for connection in (("6", "clip"), ("8", "images"), ("3", "model")):
            self.assertNotIn(connection, kinds)
        # 槽位标签带节点标题（按节点定位：两个节点都有 text 字段，不能按字段名折叠）。
        labels = {(s["node_id"], s["field_name"]): s["label"] for s in slots}
        self.assertIn("正向提示词", labels[("6", "text")])
        self.assertIn("采样", labels[("3", "seed")])

    def test_prompt_roles_traced_from_sampler_connections(self):
        # KSampler 的 positive/negative 连线回溯：节点 6 = 正向提示词，节点 7 = 负向。
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(_sample_prompt())}
        self.assertEqual(roles[("6", "text")], "positive_prompt")
        self.assertEqual(roles[("7", "text")], "negative_prompt")
        self.assertEqual(roles[("3", "seed")], "", "非提示词槽位没有角色")

    def test_scan_roles_sampler_scheduler_checkpoint(self):
        # 无专用元数据的常用字段按「明确字段名（模型类再叠加节点类名）」识别。
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(_sample_prompt())}
        self.assertEqual(roles[("3", "sampler_name")], "sampler")
        self.assertEqual(roles[("3", "scheduler")], "scheduler")
        self.assertEqual(roles[("4", "ckpt_name")], "checkpoint")
        self.assertEqual(roles[("10", "lora_name")], "lora")
        # 同名文本框但没有对应类名：不误标。
        prompt = _sample_prompt()
        prompt["11"] = {"class_type": "OtherNode", "inputs": {"ckpt_name": "x", "lora_name": "y"}}
        roles2 = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(prompt)}
        self.assertEqual(roles2.get(("11", "ckpt_name")), "")
        self.assertEqual(roles2.get(("11", "lora_name")), "")

    def test_scan_roles_resolution_width_height(self):
        # 出图分辨率也属于常用参数：宽/高标成 resolution，主表单默认显示。
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(_sample_prompt())}
        self.assertEqual(roles[("5", "width")], "resolution")
        self.assertEqual(roles[("5", "height")], "resolution")
        self.assertEqual(roles.get(("5", "batch_size")), "", "批次大小不是分辨率")

    def test_prompt_roles_title_fallback(self):
        prompt = {
            "10": {"class_type": "CLIPTextEncode",
                   "inputs": {"text": "a cat", "clip": ["2", 0]},
                   "_meta": {"title": "负向提示词"}},
            "11": {"class_type": "SomeSamplerNode",
                   "inputs": {"model": ["2", 0], "latent": ["2", 1]}},
            "2": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},
        }
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(prompt)}
        self.assertEqual(roles[("10", "text")], "negative_prompt")

    def test_prompt_roles_no_false_positive(self):
        prompt = {"5": {"class_type": "CLIPTextEncode", "inputs": {"text": "孤立的提示", "clip": ["4", 0]}}}
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(prompt)}
        self.assertEqual(roles[("5", "text")], "", "没有采样器锚点也没有标题线索时不乱标")

    def test_scan_marks_loadimage_filename(self):
        prompt = _sample_prompt()
        prompt["10"] = {"class_type": "LoadImage",
                        "inputs": {"filename": "in.png", "image_upload": True}}
        kinds = {(s["node_id"], s["field_name"]): s["field_type"] for s in scan_params(prompt)}
        self.assertEqual(kinds[("10", "filename")], "image")
        self.assertEqual(kinds[("10", "image_upload")], "bool")

    def test_media_slots_explicit_mapping_only(self):
        # 显式映射（目标环境 2026-10-24 逐一确认）：LoadImage/LoadAudio/LoadVideo/
        # VHS_LoadVideo 是媒体槽；映射外的字段（含同名 image/audio/video、
        # VHS_LoadAudio 的路径字符串）一律保持普通文本，不凭字段名猜上传协议。
        prompt = {
            "10": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "11": {"class_type": "LoadAudio", "inputs": {"audio": "a.wav"}},
            "12": {"class_type": "LoadVideo", "inputs": {"file": "a.mp4"}},
            "13": {"class_type": "VHS_LoadVideo", "inputs": {"video": "a.mp4"}},
            "14": {"class_type": "CustomNode", "inputs": {"image": "x.png", "audio": "x.wav"}},
            "15": {"class_type": "VHS_LoadAudio", "inputs": {"audio_file": "input/a.wav"}},
        }
        kinds = {(s["node_id"], s["field_name"]): s["field_type"] for s in scan_params(prompt)}
        self.assertEqual(kinds[("10", "image")], "image")
        self.assertEqual(kinds[("11", "audio")], "audio")
        self.assertEqual(kinds[("12", "file")], "video")
        self.assertEqual(kinds[("13", "video")], "video")
        self.assertEqual(kinds[("14", "image")], "text")
        self.assertEqual(kinds[("14", "audio")], "text")
        self.assertEqual(kinds[("15", "audio_file")], "text")
        self.assertEqual(media_kind_of_field("LoadImage", "image"), "image")
        self.assertIsNone(media_kind_of_field("LoadImage", "other"))

    def test_media_slot_labels_show_source_and_port(self):
        prompt = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "None"}},
            "2": {"class_type": "LoadAudio", "inputs": {"audio": "None"}},
            "3": {"class_type": "VHS_LoadVideo", "inputs": {"video": ""}},
            "4": {"class_type": "LoadAudio", "inputs": {"audio": "None"}},
            "20": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
                "ref_images.ref_image_3": ["1", 0],
                "ref_audios.ref_audio_1": ["2", 0],
                "ref_videos.ref_video_0": ["3", 0],
                "ref_video_audios.ref_video_audio_2": ["4", 0],
            }},
        }
        slots = {(slot["node_id"], slot["field_name"]): slot
                 for slot in scan_params(prompt)}
        self.assertEqual(slots[("1", "image")]["label"],
                         "1 · LoadImage · image → ref_images.ref_image_3")
        self.assertEqual(slots[("2", "audio")]["label"],
                         "2 · LoadAudio · audio → ref_audios.ref_audio_1")
        self.assertEqual(slots[("3", "video")]["label"],
                         "3 · VHS_LoadVideo · video → ref_videos.ref_video_0")
        self.assertEqual(slots[("4", "audio")]["label"],
                         "4 · LoadAudio · audio → ref_video_audios.ref_video_audio_2")
        # 结构化连接保留目标节点与输出索引，显示层不重复目标节点名。
        connection = slots[("1", "image")]["connections"][0]
        self.assertEqual(connection["target_node_id"], "20")
        self.assertEqual(connection["target_class_type"], "MiniMaxH3ReferenceToVideo")
        self.assertEqual(connection["target_field"], "ref_images.ref_image_3")
        self.assertEqual(connection["output_index"], 0)

    def test_slot_label_lists_all_connections_and_output_indexes(self):
        prompt = {
            "10": {"class_type": "LoadImage", "inputs": {"image": "a.png"},
                   "_meta": {"title": "載入圖片"}},
            "20": {"class_type": "H3", "inputs": {
                "ref_images.ref_image_0": ["10", 0],
                "ref_images.ref_image_1": ["10", 0],
            }},
            "21": {"class_type": "MaskA", "inputs": {"mask": ["10", 0]}},
            "22": {"class_type": "MaskB", "inputs": {"mask": ["10", 0]}},
        }
        slot = next(s for s in scan_params(prompt) if s["node_id"] == "10")
        self.assertEqual(
            slot["label"],
            "10 · 載入圖片 · image → ref_images.ref_image_0、ref_images.ref_image_1"
            "、mask（节点 21）、mask（节点 22）",
        )
        self.assertEqual(len(slot["connections"]), 4)

        split = {
            "30": {"class_type": "SplitNode", "inputs": {"x": 1}},
            "40": {"class_type": "A", "inputs": {"left": ["30", 0]}},
            "41": {"class_type": "B", "inputs": {"right": ["30", 1]}},
        }
        split_slot = next(s for s in scan_params(split) if s["node_id"] == "30")
        self.assertEqual(split_slot["label"],
                         "30 · SplitNode · x\n输出 0 → left\n输出 1 → right")

    def test_non_media_slot_also_gets_downstream_ports(self):
        slot = next(s for s in scan_params(_sample_prompt())
                    if s["node_id"] == "3" and s["field_name"] == "seed")
        self.assertIn("· 采样 · seed → samples", slot["label"])

    def test_scan_model_loader_roles_offline_fallback(self):
        # UNET/CLIP/VAE/ControlNet 加载字段：字段名 + 类名双重判定（离线回退路径）。
        prompt = {
            "20": {"class_type": "UNETLoader", "inputs": {"unet_name": "m.safetensors"}},
            "21": {"class_type": "CLIPLoader", "inputs": {"clip_name": "t.safetensors"}},
            "22": {"class_type": "VAELoader", "inputs": {"vae_name": "v.safetensors"}},
            "23": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "c.safetensors"}},
        }
        roles = {(s["node_id"], s["field_name"]): s.get("role") for s in scan_params(prompt)}
        self.assertEqual(roles[("20", "unet_name")], "unet")
        self.assertEqual(roles[("21", "clip_name")], "clip")
        self.assertEqual(roles[("22", "vae_name")], "vae")
        self.assertEqual(roles[("23", "control_net_name")], "controlnet")

    def test_apply_overrides_preserves_types_and_original(self):
        prompt = _sample_prompt()
        workflow = apply_overrides(prompt, [
            {"node_id": "6", "field_name": "text", "value": "一只狗"},
            {"node_id": "3", "field_name": "steps", "value": "8"},
            {"node_id": "3", "field_name": "cfg", "value": "6.5"},
        ], randomize_seeds=True)
        self.assertEqual(workflow["6"]["inputs"]["text"], "一只狗")
        self.assertEqual(workflow["3"]["inputs"]["steps"], 8)
        self.assertEqual(workflow["3"]["inputs"]["cfg"], 6.5)
        # seed 未覆盖且开启随机：换成了 0..2^50 内的新值；原 JSON 不被改动。
        self.assertNotEqual(workflow["3"]["inputs"]["seed"], 123)
        self.assertLessEqual(workflow["3"]["inputs"]["seed"], 2 ** 50)
        self.assertEqual(prompt["3"]["inputs"]["seed"], 123)
        self.assertEqual(prompt["6"]["inputs"]["text"], "一只猫")
        # 连线与未覆盖槽位原样。
        self.assertEqual(workflow["6"]["inputs"]["clip"], ["4", 1])
        self.assertEqual(workflow["5"]["inputs"]["width"], 512)

    def test_fixed_seed_and_negative_seed(self):
        prompt = _sample_prompt()
        fixed = apply_overrides(prompt, [{"node_id": "3", "field_name": "seed", "value": 42}],
                                randomize_seeds=True)
        self.assertEqual(fixed["3"]["inputs"]["seed"], 42)
        negative = apply_overrides(prompt, [{"node_id": "3", "field_name": "seed", "value": -1}],
                                   randomize_seeds=False)
        self.assertNotEqual(negative["3"]["inputs"]["seed"], -1)


class SchemaAndEnrichTests(unittest.TestCase):
    """节点声明解析与参数扫描增强（计划 §3.1-§3.3）：拿不到声明一律降级。"""

    def test_parse_decl_both_combo_styles(self):
        legacy = parse_input_decl([["a.png", "b.png"], {}])
        self.assertEqual(legacy, {"kind": "combo", "candidates": ["a.png", "b.png"],
                                  "multiline": False})
        modern = parse_input_decl(["COMBO", {"multiselect": False,
                                             "options": ["x.wav", "y.wav"]}])
        self.assertEqual(modern["kind"], "combo")
        self.assertEqual(modern["candidates"], ["x.wav", "y.wav"])

    def test_parse_decl_text_multiline_and_scalars(self):
        self.assertEqual(parse_input_decl(["STRING", {"multiline": True, "default": ""}]),
                         {"kind": "text", "candidates": None, "multiline": True})
        self.assertEqual(parse_input_decl(["STRING", {"default": "x"}]),
                         {"kind": "text", "candidates": None, "multiline": False})
        self.assertEqual(parse_input_decl(["INT", {}])["kind"], "number")
        self.assertEqual(parse_input_decl(["FLOAT", {}])["kind"], "number")
        self.assertEqual(parse_input_decl(["BOOLEAN", {}])["kind"], "bool")

    def test_parse_decl_connections_and_unknown_rejected(self):
        # 连线端口（IMAGE/MODEL/…）与没有 options 的未知自定义类型：不是可编辑控件。
        for spec in (["IMAGE", {}], ["MODEL", {}], ["MY_CUSTOM_TYPE", {}], None, [], "x",
                     [["a", 1]], ["COMBO", {"options": []}]):
            self.assertIsNone(parse_input_decl(spec), spec)

    def test_parse_autogrow_decl_prefix_names_and_bad_shapes(self):
        spec = ["COMFY_AUTOGROW_V3", {"template": {
            "input": {"required": {"ref_image": ["IMAGE", {}]}},
            "prefix": "ref_image_", "min": 0, "max": 9,
        }}]
        parsed = parse_autogrow_decl(spec)
        self.assertEqual(parsed["prefix"], "ref_image_")
        self.assertEqual(parsed["names"], [])
        self.assertEqual(parsed["min"], 0)
        self.assertEqual(parsed["max"], 9)
        self.assertTrue(parsed["template_required"])
        names = parse_autogrow_decl(["COMFY_AUTOGROW_V3", {"template": {
            "input": {"optional": {"ref": ["IMAGE", {}]}},
            "names": ["a", "b"], "min": 1,
        }}])
        self.assertEqual(names["names"], ["a", "b"])
        self.assertEqual(names["max"], 2)
        self.assertFalse(names["template_required"])
        for bad in (["IMAGE", {}], ["COMFY_AUTOGROW_V3", {}],
                    ["COMFY_AUTOGROW_V3", {"template": {}}], None, "x"):
            self.assertIsNone(parse_autogrow_decl(bad), bad)

    def test_input_is_optional_autogrow_range_min_and_prefix(self):
        decls = {"ref_images": _autogrow_decl("ref_image_", minimum=2, maximum=4)}
        # 0/1 是 min 要求的必填槽，不能承诺跳过。
        self.assertFalse(input_is_optional(decls, "ref_images.ref_image_0"))
        self.assertFalse(input_is_optional(decls, "ref_images.ref_image_1"))
        self.assertTrue(input_is_optional(decls, "ref_images.ref_image_2"))
        # 越界、前导零、前缀不符、组名本身都不是可确认的槽位。
        for field in ("ref_images.ref_image_4", "ref_images.ref_image_01",
                      "ref_images.other_2", "ref_images"):
            self.assertIsNone(input_is_optional(decls, field), field)

    def test_input_is_optional_exact_and_unknown_group(self):
        decls = {
            "vae": {"kind": "unknown", "optional": True},
            "image": {"kind": "unknown", "optional": False},
            "ref_images": {"kind": "unknown", "optional": True},
        }
        self.assertTrue(input_is_optional(decls, "vae"))
        self.assertFalse(input_is_optional(decls, "image"))
        # 只有「组在 optional 块」不足以证明动态槽可省略。
        self.assertIsNone(input_is_optional(decls, "ref_images.ref_image_0"))
        self.assertIsNone(input_is_optional(None, "ref_images.ref_image_0"))

    def test_provider_parses_autogrow_group_from_object_info(self):
        provider = NodeSchemaProvider("http://127.0.0.1:1")
        payload = {"input": {"optional": {"ref_images": ["COMFY_AUTOGROW_V3", {
            "template": {"input": {"required": {"ref_image": ["IMAGE", {}]}},
                         "prefix": "ref_image_", "min": 0, "max": 9},
        }]}}}
        with mock.patch.object(comfyui_client, "object_info_node", return_value=payload):
            fields = provider._fetch("MiniMaxH3ReferenceToVideo")
        self.assertEqual(fields["ref_images"]["kind"], "autogrow")
        self.assertTrue(fields["ref_images"]["optional"])
        self.assertEqual(fields["ref_images"]["autogrow"]["min"], 0)
        self.assertTrue(input_is_optional(fields, "ref_images.ref_image_0"))

    def _provider_with(self, payload_or_error):
        provider = NodeSchemaProvider("http://127.0.0.1:1")
        patcher = mock.patch.object(
            NodeSchemaProvider, "_fetch", side_effect=lambda cls: payload_or_error,
        )
        return provider, patcher

    def test_provider_caches_per_class_and_degrades(self):
        fields = {
            "image": {"kind": "combo", "candidates": ["a.png"], "multiline": False,
                      "optional": False},
            "ref_images": {"kind": "unknown", "optional": True},
        }

        def _fetch(class_type):
            return fields if class_type == "LoadImage" else None

        provider = NodeSchemaProvider("http://127.0.0.1:1")
        with mock.patch.object(NodeSchemaProvider, "_fetch", side_effect=_fetch) as fetch:
            self.assertEqual(provider.node_inputs("LoadImage"), fields)
            self.assertEqual(provider.node_inputs("LoadImage"), fields)
            self.assertEqual(fetch.call_count, 1, "同一节点类只取一次（按类缓存）")
        self.assertIsNone(provider.node_inputs("NoSuchNode"), "未安装节点降级为 None")

    def test_provider_preserves_required_and_optional_input_scope(self):
        provider = NodeSchemaProvider("http://127.0.0.1:1")
        payload = {"input": {
            "required": {"image": ["IMAGE", {}]},
            "optional": {"ref_images.ref_image_0": ["IMAGE", {}]},
        }}
        with mock.patch.object(comfyui_client, "object_info_node", return_value=payload):
            fields = provider._fetch("MiniMaxH3ReferenceToVideo")
        self.assertFalse(fields["image"]["optional"])
        self.assertTrue(fields["ref_images.ref_image_0"]["optional"])

    def test_provider_fetch_failure_degrades(self):
        provider, patcher = self._provider_with(None)
        with patcher:
            self.assertIsNone(provider.node_inputs("LoadImage"))

    def test_enrich_slots_combo_and_multiline(self):
        prompt = {
            "1": {"class_type": "MyPromptNode", "inputs": {"text": "p"}},
            "2": {"class_type": "KSampler", "inputs": {"sampler_name": "euler", "seed": 1}},
        }
        slots = scan_params(prompt)
        decls = {
            "MyPromptNode": {"text": {"kind": "text", "candidates": None, "multiline": True}},
            "KSampler": {"sampler_name": {"kind": "combo",
                                          "candidates": ["euler", "dpm++2m"], "multiline": False}},
        }

        class _Provider:
            def node_inputs(self, class_type):
                return decls.get(class_type)

        enriched = enrich_slots(slots, _Provider())
        by_key = {(s["node_id"], s["field_name"]): s for s in enriched}
        self.assertEqual(by_key[("1", "text")]["control"], "multiline")
        self.assertEqual(by_key[("2", "sampler_name")]["control"], "combo")
        self.assertEqual(by_key[("2", "sampler_name")]["candidates"], ["euler", "dpm++2m"])
        # 语义字段不被增强改动（计划 §3.4：override 仍按原值类型转换）。
        self.assertEqual(by_key[("2", "seed")].get("field_type"), "seed")
        self.assertNotIn("control", by_key[("2", "seed")], "声明拿不到的槽位保持基线")

    def test_enrich_slots_media_control_wins_over_schema(self):
        prompt = {"10": {"class_type": "LoadImage", "inputs": {"image": "a.png"}}}
        slots = scan_params(prompt)
        media = enrich_slots(slots, None)
        self.assertEqual(media[0]["control"], "media")
        self.assertEqual(media[0]["media"], "image")
        # 没有下游消费者：未选跳过不会影响任何节点。
        self.assertTrue(media[0]["media_optional"])

    def test_enrich_slots_media_optional_requires_all_consumers_optional(self):
        prompt = {
            "10": {"class_type": "LoadImage", "inputs": {"image": "None"}},
            "20": {"class_type": "OptionalConsumer", "inputs": {
                "ref_images.ref_image_0": ["10", 0]}},
            "21": {"class_type": "RequiredConsumer", "inputs": {
                "ref_images.ref_image_1": ["10", 0]}},
        }
        slots = scan_params(prompt)
        media = next(s for s in slots if s["node_id"] == "10")
        self.assertEqual(len(media["connections"]), 2)

        class _Provider:
            def node_inputs(self, class_type):
                if class_type == "OptionalConsumer":
                    return {"ref_images": _autogrow_decl("ref_image_")}
                if class_type == "RequiredConsumer":
                    return {"ref_images": _autogrow_decl("ref_image_", minimum=2)}
                return None

        enrich_slots(slots, _Provider())
        self.assertFalse(media["media_optional"],
                         "混有必需消费者时不能承诺整份素材会跳过")

        # 两个消费者都 optional 才允许跳过。
        class _AllOptional:
            def node_inputs(self, class_type):
                return {"ref_images": _autogrow_decl("ref_image_")}

        enrich_slots(slots, _AllOptional())
        self.assertTrue(media["media_optional"])

    def test_enrich_slots_media_optional_unknown_when_schema_missing(self):
        prompt = {
            "10": {"class_type": "LoadImage", "inputs": {"image": "None"}},
            "20": {"class_type": "UnknownConsumer", "inputs": {
                "ref_images.ref_image_0": ["10", 0]}},
        }
        slots = scan_params(prompt)
        media = next(s for s in slots if s["node_id"] == "10")

        class _Unknown:
            def node_inputs(self, class_type):
                return None

        enrich_slots(slots, _Unknown())
        self.assertIsNone(media["media_optional"])


class ServiceImportTests(unittest.TestCase):
    def test_api_format_accepted(self):
        normalized = normalize_api_workflow({"prompt": _sample_prompt()})
        self.assertIn("3", normalized)
        self.assertEqual(normalize_api_workflow(_sample_prompt()), _sample_prompt())

    def test_ui_format_rejected_with_export_guidance(self):
        with self.assertRaises(ValueError) as ctx:
            normalize_api_workflow({"nodes": [], "links": [], "version": "0.4"})
        self.assertIn("导出（API）", str(ctx.exception))
        self.assertEqual(str(ctx.exception), UI_FORMAT_ERROR)

    def test_empty_and_invalid_rejected(self):
        with self.assertRaises(ValueError):
            normalize_api_workflow({})
        with self.assertRaises(ValueError):
            normalize_api_workflow({"1": "not-a-node"})
        with self.assertRaises(ValueError):
            normalize_api_workflow("text")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = ComfyUIStore(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_workflow_crud(self):
        entry = {"id": self.store.new_workflow_id(), "name": "测试",
                 "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
                 "created_at": 1, "updated_at": 1}
        self.store.save_workflow(entry)
        got = self.store.get_workflow(entry["id"])
        self.assertEqual(got["name"], "测试")
        rows = self.store.list_workflows()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("prompt", rows[0], "列表不携带 prompt 本体")
        self.assertEqual(rows[0]["node_count"], len(_sample_prompt()))
        self.assertTrue(self.store.delete_workflow(entry["id"]))
        self.assertIsNone(self.store.get_workflow(entry["id"]))

    def test_workflow_id_traversal_rejected(self):
        self.assertIsNone(self.store.get_workflow("../../etc/passwd"))
        self.assertIsNone(self.store.get_workflow(""))

    def test_task_lifecycle_and_events(self):
        task = self.store.create_task({"workflow_id": "cw-1", "shots": 2, "prompt": {}, "overrides": []})
        task_id = task["id"]
        self.assertTrue(task_id.startswith("cf-"))
        self.store.update_task(task_id, status="running", started_at=1,
                               checkpoint={"prompt_ids": ["p1"], "completed": [], "errors": [],
                                           "resubmits": {}})
        got = self.store.get_task(task_id)
        self.assertEqual(got["status"], "running")
        self.assertEqual(got["checkpoint"]["prompt_ids"], ["p1"])
        # 白名单外的字段不落盘。
        self.store.update_task(task_id, input_data={"hack": True})
        self.assertNotIn("hack", self.store.get_task(task_id))
        seq1 = self.store.append_event(task_id, {"type": "task_started", "message": "x"})
        seq2 = self.store.append_event(task_id, {"type": "shot_submitted", "index": 0})
        self.assertEqual((seq1, seq2), (1, 2))
        rows = self.store.read_events(task_id, after=1)
        self.assertEqual([row["type"] for row in rows], ["shot_submitted"])
        with self.assertRaises(ValueError):
            self.store.append_event(task_id, {"type": "bogus"})

    def test_delete_task_removes_outputs(self):
        task = self.store.create_task({"shots": 1, "prompt": {}, "overrides": []})
        out_dir = self.store.task_output_dir(task["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "001_00_a.png").write_bytes(b"x")
        self.assertTrue(self.store.delete_task(task["id"]))
        self.assertIsNone(self.store.get_task(task["id"]))
        self.assertFalse(out_dir.exists())


class _FakeComfy:
    """假 ComfyUI：可控的提交成功/失败、历史状态、队列、下载字节。"""

    def __init__(self, *, submit_fail: str = "", history_status: str = "success",
                 drop_after_submit: bool = False):
        self.submit_fail = submit_fail
        self.history_status = history_status
        self.drop_after_submit = drop_after_submit
        self.submitted: list[dict] = []
        self.interrupted = 0
        self.deleted_queue: list[str] = []
        self.lock = threading.Lock()

    def patch(self):
        return mock.patch.multiple(
            comfyui_client,
            reachable=mock.DEFAULT, submit=mock.DEFAULT, queue_ids=mock.DEFAULT,
            history_entry=mock.DEFAULT, entry_status=mock.DEFAULT, entry_error=mock.DEFAULT,
            output_items=mock.DEFAULT, download_output=mock.DEFAULT,
            interrupt=mock.DEFAULT, delete_queue_items=mock.DEFAULT,
        )

    def wire(self, patcher):
        patcher["reachable"].side_effect = lambda base, timeout=10: {"version": "test"}
        def _submit(base, workflow):
            if self.submit_fail:
                return "", self.submit_fail
            with self.lock:
                self.submitted.append({"workflow": workflow})
            if self.drop_after_submit:
                return f"pid-{len(self.submitted)}", ""
            return f"pid-{len(self.submitted)}", ""
        patcher["submit"].side_effect = _submit
        patcher["queue_ids"].side_effect = lambda base: set()  # 永不在队列
        patcher["history_entry"].side_effect = self._history
        patcher["entry_status"].side_effect = self._status
        patcher["entry_error"].side_effect = lambda entry: "采样失败：value out of range"
        patcher["output_items"].side_effect = lambda entry: [
            {"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}]
        patcher["download_output"].side_effect = lambda base, item, timeout=120: b"PNG-BYTES"
        patcher["interrupt"].side_effect = lambda base: True
        patcher["delete_queue_items"].side_effect = self._delete_queue

    def _history(self, base, prompt_id):
        if self.drop_after_submit:
            return None
        if self.history_status == "pending":
            return None
        if self.history_status == "error":
            return {"prompt_id": prompt_id, "status": {"status_str": "error", "messages": []},
                    "outputs": {}}
        return {"prompt_id": prompt_id, "status": {"status_str": "success", "messages": []},
                "outputs": {"9": {"images": [{"filename": "ComfyUI_00001_.png",
                                              "subfolder": "", "type": "output"}]}}}

    def _status(self, entry):
        return str((entry.get("status") or {}).get("status_str") or "")

    def _delete_queue(self, base, prompt_ids):
        self.deleted_queue.extend(prompt_ids)
        return True


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = _StubApp(self.dir)
        self.store = ComfyUIStore(self.dir)
        self.runner = ComfyUIRunner(self.app, self.store)

    def tearDown(self):
        self.runner.shutdown(timeout=2)
        self.tmp.cleanup()

    def _make_task(self, shots=2, **kwargs):
        task = self.store.create_task({
            "workflow_id": "cw-1", "workflow_name": "测试", "prompt": _sample_prompt(),
            "overrides": [], "shots": shots, **kwargs,
        })
        return task

    def _run_to_terminal(self, task_id, deadline=15.0):
        end = time.time() + deadline
        while time.time() < end:
            task = self.store.get_task(task_id)
            if task and task["status"] not in ("queued", "running"):
                return task
            time.sleep(0.05)
        raise AssertionError("任务未在期限内结束")

    def test_two_shots_complete_and_outputs_saved(self):
        fake = _FakeComfy()
        task = self._make_task(shots=2)
        with fake.patch() as patcher:
            fake.wire(patcher)
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["progress"], {"done": 2, "total": 2, "failed": 0})
        self.assertEqual(len(fake.submitted), 2)
        outputs = done["result"]["outputs"]
        self.assertEqual(len(outputs), 2)
        for name in outputs:
            path = self.store.task_output_dir(task["id"]) / name
            self.assertEqual(path.read_bytes(), b"PNG-BYTES")
        types = [row["type"] for row in self.store.read_events(task["id"])]
        self.assertIn("task_started", types)
        self.assertEqual(types.count("shot_submitted"), 2)
        self.assertEqual(types.count("shot_done"), 2)
        self.assertIn("task_done", types)

    def test_submit_failure_carries_server_reason(self):
        fake = _FakeComfy(submit_fail="节点 3（KSampler）：seed 超上限")
        task = self._make_task(shots=2)
        with fake.patch() as patcher:
            fake.wire(patcher)
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("第 1 段提交失败", done["error"])
        self.assertIn("seed 超上限", done["error"])
        self.assertEqual(fake.submitted, [], "提交失败后不再提交后续段")

    def test_unselected_optional_media_inputs_are_removed_without_reindexing(self):
        prompt = {
            "10": {"class_type": "LoadImage", "inputs": {"image": "None"}},
            "11": {"class_type": "LoadImage", "inputs": {"image": "ref.png"}},
            "12": {"class_type": "LoadAudio", "inputs": {"audio": "None"}},
            "13": {"class_type": "VHS_LoadVideo", "inputs": {"video": ""}},
            "14": {"class_type": "LoadAudio", "inputs": {"audio": "song.wav"}},
            "20": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
                "ref_images.ref_image_0": ["10", 0],
                "ref_images.ref_image_1": ["11", 0],
                "ref_audios.ref_audio_0": ["12", 0],
                "ref_videos.ref_video_0": ["13", 0],
                "ref_video_audios.ref_video_audio_1": ["14", 0],
            }},
        }
        declarations = {
            "MiniMaxH3ReferenceToVideo": {
                "ref_images": _autogrow_decl("ref_image_"),
                "ref_audios": _autogrow_decl("ref_audio_"),
                "ref_videos": _autogrow_decl("ref_video_"),
                "ref_video_audios": _autogrow_decl("ref_video_audio_"),
            }
        }
        class_provider = type("Provider", (), {
            "node_inputs": lambda self, class_type: declarations.get(class_type),
        })
        with mock.patch("naiba.comfyui.runner.NodeSchemaProvider", return_value=class_provider()):
            self.runner._remove_unselected_optional_media(prompt, [
                {"node_id": "11", "field_name": "image", "path": "uploads/ref.png"},
                {"node_id": "14", "field_name": "audio", "path": "uploads/song.wav"},
            ], "http://127.0.0.1:8188")
        h3_inputs = prompt["20"]["inputs"]
        self.assertNotIn("ref_images.ref_image_0", h3_inputs)
        self.assertEqual(h3_inputs["ref_images.ref_image_1"], ["11", 0])
        self.assertNotIn("ref_audios.ref_audio_0", h3_inputs)
        self.assertNotIn("ref_videos.ref_video_0", h3_inputs)
        self.assertEqual(h3_inputs["ref_video_audios.ref_video_audio_1"], ["14", 0])

    def test_unselected_media_cleanup_keeps_required_and_unknown_inputs(self):
        prompt = {
            "10": {"class_type": "LoadImage", "inputs": {"image": "None"}},
            "20": {"class_type": "RequiredConsumer", "inputs": {"image": ["10", 0]}},
            "21": {"class_type": "UnknownConsumer", "inputs": {"image": ["10", 0]}},
        }
        declarations = {
            "RequiredConsumer": {"image": {"optional": False}},
            "UnknownConsumer": None,
        }
        class_provider = type("Provider", (), {
            "node_inputs": lambda self, class_type: declarations.get(class_type),
        })
        with mock.patch("naiba.comfyui.runner.NodeSchemaProvider", return_value=class_provider()):
            self.runner._remove_unselected_optional_media(prompt, [], "http://127.0.0.1:8188")
        self.assertEqual(prompt["20"]["inputs"]["image"], ["10", 0])
        self.assertEqual(prompt["21"]["inputs"]["image"], ["10", 0])

    def test_unselected_media_cleanup_honors_autogrow_min_and_unknown_groups(self):
        prompt = {
            "10": {"class_type": "LoadImage", "inputs": {"image": "None"}},
            "11": {"class_type": "LoadImage", "inputs": {"image": "None"}},
            "20": {"class_type": "AutogrowConsumer", "inputs": {
                "ref_images.ref_image_0": ["10", 0],
                "ref_images.ref_image_2": ["11", 0],
            }},
            "21": {"class_type": "UnknownGroupConsumer", "inputs": {
                "ref_images.ref_image_1": ["10", 0]}},
        }
        declarations = {
            # min=2：0/1 号槽位是 required，即使连接未选也不能移除。
            "AutogrowConsumer": {"ref_images": _autogrow_decl("ref_image_", minimum=2)},
            # 只有 optional 归属、没有可解析的 Autogrow：保守保留。
            "UnknownGroupConsumer": {"ref_images": {"kind": "unknown", "optional": True}},
        }
        class_provider = type("Provider", (), {
            "node_inputs": lambda self, class_type: declarations.get(class_type),
        })
        with mock.patch("naiba.comfyui.runner.NodeSchemaProvider", return_value=class_provider()):
            self.runner._remove_unselected_optional_media(prompt, [], "http://127.0.0.1:8188")
        self.assertIn("ref_images.ref_image_0", prompt["20"]["inputs"])
        self.assertNotIn("ref_images.ref_image_2", prompt["20"]["inputs"])
        self.assertIn("ref_images.ref_image_1", prompt["21"]["inputs"])

    def test_media_inputs_uploaded_once_and_backfilled(self):
        # 计划 §5.2：执行前上传受管资产并回填进提交副本；同一任务同资产只传一次；
        # 工作流库快照不被污染。
        fake = _FakeComfy()
        prompt = dict(_sample_prompt())
        prompt["30"] = {"class_type": "LoadImage", "inputs": {"image": "base.png"}}
        uploads = self.dir / "uploads"
        uploads.mkdir(exist_ok=True)
        ref = uploads / "ref.png"
        ref.write_bytes(b"PNG")
        media = [{"node_id": "30", "field_name": "image", "path": str(ref.resolve()),
                  "media": "image", "name": "ref.png", "size": 3}]
        task = self._make_task(shots=2, prompt=prompt, media_inputs=media)
        uploads_called = []

        def _upload(base, path, timeout=300):
            uploads_called.append(path)
            return "ref.png", ""

        with fake.patch() as patcher:
            fake.wire(patcher)
            with mock.patch.object(comfyui_client, "upload_media", side_effect=_upload):
                self.runner.submit(task["id"])
                done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(len(uploads_called), 1, "两段共用同一资产：只上传一次")
        for record in fake.submitted:
            self.assertEqual(record["workflow"]["30"]["inputs"]["image"], "ref.png")
        self.assertEqual(prompt["30"]["inputs"]["image"], "base.png", "库快照不被污染")

    def test_media_upload_failure_stops_segment_with_reason(self):
        fake = _FakeComfy()
        prompt = dict(_sample_prompt())
        prompt["30"] = {"class_type": "LoadImage", "inputs": {"image": "base.png"}}
        media = [{"node_id": "30", "field_name": "image", "path": "X:/missing.png",
                  "media": "image", "name": "missing.png", "size": 3}]
        task = self._make_task(shots=2, prompt=prompt, media_inputs=media)
        with fake.patch() as patcher:
            fake.wire(patcher)
            with mock.patch.object(comfyui_client, "upload_media",
                                   return_value=("", "HTTP 500 boom")):
                self.runner.submit(task["id"])
                done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("媒体输入处理失败", done["error"])
        self.assertIn("节点 30 · image", done["error"])
        self.assertIn("HTTP 500 boom", done["error"])
        self.assertEqual(fake.submitted, [], "上传失败不提交该段，也不回退旧文件名")

    def test_history_error_status_fails_task(self):
        fake = _FakeComfy(history_status="error")
        task = self._make_task(shots=1)
        with fake.patch() as patcher:
            fake.wire(patcher)
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("第 1 段执行失败", done["error"])

    def test_lost_prompt_resubmits_within_budget_then_fails(self):
        fake = _FakeComfy(drop_after_submit=True)
        task = self._make_task(shots=1)
        with fake.patch() as patcher:
            fake.wire(patcher)
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"], deadline=25.0)
        self.assertEqual(done["status"], "failed")
        self.assertIn("已被 ComfyUI 丢弃", done["error"])
        # 预算 1：初次提交 + 一次重提交 = 2 次。
        self.assertEqual(len(fake.submitted), 2)
        self.assertEqual(done["checkpoint"]["resubmits"].get("0"), 1)

    def test_unreachable_fails_with_actionable_message(self):
        task = self._make_task(shots=1)
        with mock.patch.object(comfyui_client, "reachable", return_value=None):
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("ComfyUI 服务不可达", done["error"])

    def test_cancel_running_calls_interrupt_and_queue_delete(self):
        fake = _FakeComfy(history_status="pending")
        fake.patch()  # 保持 patch 生命周期：手动 enter/exit
        task = self._make_task(shots=1)
        started = threading.Event()
        original_submit = comfyui_client.submit

        def _submit(base, workflow):
            started.set()
            return "pid-1", ""

        with mock.patch.object(comfyui_client, "reachable", return_value={"version": "t"}), \
             mock.patch.object(comfyui_client, "submit", side_effect=_submit), \
             mock.patch.object(comfyui_client, "queue_ids", return_value={"pid-1"}), \
             mock.patch.object(comfyui_client, "history_entry", return_value=None), \
             mock.patch.object(comfyui_client, "interrupt", return_value=True) as mock_int, \
             mock.patch.object(comfyui_client, "delete_queue_items",
                               return_value=True) as mock_del:
            self.runner.submit(task["id"])
            self.assertTrue(started.wait(10))
            # 等 runner 把 prompt_id 写进 checkpoint（取消的两步要对着已登记的段来）。
            deadline = time.time() + 5
            while time.time() < deadline:
                current = self.store.get_task(task["id"]) or {}
                if (current.get("checkpoint") or {}).get("prompt_ids"):
                    break
                time.sleep(0.02)
            self.assertTrue(self.runner.cancel(task["id"], reason="用户取消"))
            done = self._run_to_terminal(task["id"])
            self.assertEqual(done["status"], "cancelled")
            mock_int.assert_called()
            mock_del.assert_called()
        _ = original_submit

    def test_cancel_queued_task_terminals_immediately(self):
        task = self._make_task(shots=1)
        fake = _FakeComfy()
        with fake.patch() as patcher:
            fake.wire(patcher)
            # 不 submit runner：任务停留在 queued，直接取消。
            self.assertTrue(self.runner.cancel(task["id"]))
            done = self.store.get_task(task["id"])
        self.assertEqual(done["status"], "cancelled")

    def test_resume_keeps_completed_segments(self):
        fake = _FakeComfy()
        task = self._make_task(shots=2)
        # 预置 checkpoint：第 0 段已完成（含产物文件），第 1 段待跑。
        out_dir = self.store.task_output_dir(task["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "001_00_old.png").write_bytes(b"old")
        self.store.update_task(task["id"], status="queued", checkpoint={
            "prompt_ids": ["pid-old", ""],
            "completed": [{"index": 0, "prompt_id": "pid-old", "files": ["001_00_old.png"]}],
            "errors": [], "resubmits": {},
        })
        with fake.patch() as patcher:
            fake.wire(patcher)
            self.runner.submit(task["id"])
            done = self._run_to_terminal(task["id"])
        self.assertEqual(done["status"], "completed")
        # 只提交了未完成的第 1 段；第 0 段产物保留。
        self.assertEqual(len(fake.submitted), 1)
        self.assertEqual(done["result"]["outputs"], ["001_00_old.png", "002_00_ComfyUI_00001_.png"])

    def test_recover_interrupted_marks_active_tasks(self):
        task = self._make_task(shots=3)
        self.store.update_task(task["id"], status="running")
        manager = ComfyUIJobManager(self.app, self.store, self.runner)
        recovered = manager.recover_interrupted()
        self.assertEqual(recovered, 1)
        got = self.store.get_task(task["id"])
        self.assertEqual(got["status"], "interrupted")
        self.assertIn("重试", got["detail"])
        # 重启恢复成终态时要补上结束时刻，不能沿用启动前的旧值或缺失。
        self.assertIsNotNone(got["finished_at"])


class _StubComfyRunner:
    """JobsTests 用：任务管理层的桩 runner——不启动任何后台线程。

    真 runner 会在提交后立刻起线程跑任务（reach 不到 ComfyUI 就终态化），
    与测试里手写的 update_task 产生竞态；任务管理层测试只需要 submit 被调用。
    """

    def __init__(self, store: ComfyUIStore | None = None):
        self.submitted: list[str] = []
        self.store = store

    def submit(self, task_id: str) -> None:
        self.submitted.append(str(task_id))

    def cancel(self, task_id: str, *, reason: str = "") -> bool:
        # 模拟真 runner 对**排队**任务的取消：同步本地终态化（运行中任务则由
        # worker 收尾，测试用显式的状态翻转桩另行覆盖）。
        if self.store is not None:
            self.store.update_task(task_id, status="cancelled")
        return True

    def shutdown(self, timeout: float = 0.0) -> None:
        pass


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = _StubApp(self.dir)
        self.store = ComfyUIStore(self.dir)
        self.runner = _StubComfyRunner()
        self.jobs = ComfyUIJobManager(self.app, self.store, self.runner)
        self.entry = self.store.save_workflow({
            "id": self.store.new_workflow_id(), "name": "测试工作流",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "created_at": 1, "updated_at": 1,
        })

    def tearDown(self):
        self.runner.shutdown()
        self.tmp.cleanup()

    def test_submit_requires_known_workflow_and_slots(self):
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({})
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({"workflow_id": "cw-missing"})
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({
                "workflow_id": self.entry["id"],
                "overrides": [{"node_id": "99", "field_name": "nope", "value": 1}],
            })
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({"workflow_id": self.entry["id"], "shots": "abc"})

    def test_submit_validates_and_converts_overrides(self):
        task = self.jobs.submit({
            "workflow_id": self.entry["id"], "shots": "2",
            "overrides": [
                {"node_id": "6", "field_name": "text", "value": "一只狗"},
                {"node_id": "3", "field_name": "steps", "value": "9"},
                {"node_id": "3", "field_name": "seed", "value": ""},
            ],
        })
        self.assertEqual(len(task["input"]["overrides"]), 2, "空 seed = 随机，不进 overrides")
        self.assertEqual(task["input"]["overrides"][1]["value"], 9)
        self.assertEqual(task["input"]["prompt"], _sample_prompt())
        self.assertEqual(task["input"]["shots"], 2)
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({
                "workflow_id": self.entry["id"],
                "overrides": [{"node_id": "3", "field_name": "steps", "value": "abc"}],
            })

    # ---- 媒体输入校验（计划 §5.2/§5.3）----

    def _media_entry(self):
        prompt = dict(_sample_prompt())
        prompt["30"] = {"class_type": "LoadImage", "inputs": {"image": "base.png"}}
        prompt["31"] = {"class_type": "LoadAudio", "inputs": {"audio": "base.wav"}}
        return self.store.save_workflow({
            "id": self.store.new_workflow_id(), "name": "媒体工作流",
            "prompt": prompt, "params": enrich_slots(scan_params(prompt), None),
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": 1, "updated_at": 1,
        })

    def _make_upload(self, name: str, data: bytes = b"PNG") -> Path:
        uploads = self.dir / "uploads"
        uploads.mkdir(exist_ok=True)
        target = uploads / name
        target.write_bytes(data)
        return target

    def test_media_inputs_accepted_into_snapshot(self):
        entry = self._media_entry()
        image = self._make_upload("ref.png")
        audio = self._make_upload("clip.wav", b"WAV")
        task = self.jobs.submit({
            "workflow_id": entry["id"], "shots": 1,
            "media_inputs": [
                {"node_id": "30", "field_name": "image", "path": str(image)},
                {"node_id": "31", "field_name": "audio", "path": str(audio)},
            ],
        })
        media = task["input"]["media_inputs"]
        self.assertEqual(len(media), 2)
        self.assertEqual(media[0]["media"], "image")
        self.assertEqual(media[0]["name"], "ref.png")
        self.assertEqual(media[0]["path"], str(image.resolve()))
        self.assertEqual(media[1]["media"], "audio")

    def test_media_inputs_reject_arbitrary_and_missing_paths(self):
        entry = self._media_entry()
        image = self._make_upload("ref.png")
        outside = self.dir / "outside.png"
        outside.write_bytes(b"PNG")
        cases = [
            # 任意本机路径（不在 uploads 树内）——即使文件真实存在。
            {"node_id": "30", "field_name": "image", "path": str(outside)},
            # 路径穿越形态：文本合法但解析后不在受管树内。
            {"node_id": "30", "field_name": "image", "path": "C:/Windows/explorer.png"},
            # 受管树内但文件不存在。
            {"node_id": "30", "field_name": "image",
             "path": str(self.dir / "uploads" / "missing.png")},
            # 类型不匹配：图片槽给了音频文件。
            {"node_id": "30", "field_name": "image", "path": str(self._make_upload("bad.wav", b"WAV"))},
            # 不是扫描出的媒体槽。
            {"node_id": "6", "field_name": "text", "path": str(image)},
            # 空路径。
            {"node_id": "30", "field_name": "image", "path": ""},
        ]
        for index, media in enumerate(cases):
            with self.assertRaises(ComfyUITaskError, msg=f"case {index}"):
                self.jobs.submit({
                    "workflow_id": entry["id"], "shots": 1, "media_inputs": [media],
                })

    def test_media_inputs_oversize_rejected(self):
        entry = self._media_entry()
        image = self._make_upload("big.png")
        with mock.patch("naiba.comfyui.jobs.MEDIA_INPUT_MAX_BYTES", 2):
            with self.assertRaises(ComfyUITaskError):
                self.jobs.submit({
                    "workflow_id": entry["id"], "shots": 1,
                    "media_inputs": [{"node_id": "30", "field_name": "image",
                                      "path": str(image)}],
                })

    def test_media_slot_override_rejected(self):
        entry = self._media_entry()
        with self.assertRaises(ComfyUITaskError):
            self.jobs.submit({
                "workflow_id": entry["id"], "shots": 1,
                "overrides": [{"node_id": "30", "field_name": "image", "value": "x.png"}],
            })

    def test_retry_keeps_completed_and_clears_errors(self):
        task = self.jobs.submit({"workflow_id": self.entry["id"], "shots": 3})
        task_id = task["id"]
        self.store.update_task(task_id, status="failed", checkpoint={
            "prompt_ids": ["p1", "p2", ""],
            "completed": [{"index": 0, "prompt_id": "p1", "files": ["a.png"]}],
            "errors": [{"index": 1, "prompt_id": "p2", "error": "boom"}],
            "resubmits": {},
        })
        retried = self.jobs.retry(task_id)
        self.assertEqual(retried["status"], "queued")
        checkpoint = retried["checkpoint"]
        self.assertEqual(len(checkpoint["completed"]), 1)
        self.assertEqual(checkpoint["errors"], [])
        # 全部完成后再重试：明确拒绝。
        self.store.update_task(task_id, status="failed", checkpoint={
            "prompt_ids": ["p1", "p2", "p3"],
            "completed": [{"index": i, "prompt_id": f"p{i + 1}", "files": []} for i in range(3)],
            "errors": [], "resubmits": {},
        })
        with self.assertRaises(ComfyUITaskError):
            self.jobs.retry(task_id)

    def test_retry_active_task_rejected(self):
        task = self.jobs.submit({"workflow_id": self.entry["id"]})
        self.store.update_task(task["id"], status="running")
        with self.assertRaises(ComfyUITaskError):
            self.jobs.retry(task["id"])

    def test_retry_clears_finished_at(self):
        # 重试是新的一轮运行：上一轮的结束时刻必须清掉，不能沿用旧值。
        task = self.jobs.submit({"workflow_id": self.entry["id"], "shots": 2})
        self.store.update_task(task["id"], status="failed", finished_at=12345)
        retried = self.jobs.retry(task["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertIsNone(retried["finished_at"])

    def test_delete_tasks_skips_active_and_removes_outputs(self):
        # 直接落任务记录（不经过 runner：真 runner 会把任务推到终态，干扰断言）。
        finished = self.store.create_task({"workflow_id": "cw-x", "shots": 1, "prompt": {}, "overrides": []})
        running = self.store.create_task({"workflow_id": "cw-x", "shots": 1, "prompt": {}, "overrides": []})
        self.store.update_task(finished["id"], status="completed")
        self.store.update_task(running["id"], status="running")
        out_dir = self.store.task_output_dir(finished["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "x.png").write_bytes(b"x")
        result = self.jobs.delete_tasks({})
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["skipped"], [running["id"]])
        self.assertEqual(result["failed"], [])
        self.assertFalse(out_dir.exists())
        self.assertIsNotNone(self.store.get_task(running["id"]))

    def test_delete_tasks_cancels_active_then_deletes(self):
        # 计划 §2：删除排队/运行中的任务 = 先取消、等终态、再删记录与产物。
        task = self.store.create_task({"workflow_id": "cw-x", "shots": 1, "prompt": {}, "overrides": []})
        self.store.update_task(task["id"], status="running")
        out_dir = self.store.task_output_dir(task["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "x.png").write_bytes(b"x")
        runner = _StubComfyRunner(self.store)
        jobs = ComfyUIJobManager(self.app, self.store, runner)
        result = jobs.delete_tasks(
            {"task_ids": [task["id"]], "cancel_active": True}, cancel_timeout=5.0)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["failed"], [])
        self.assertEqual(runner.submitted, [])
        self.assertIsNone(self.store.get_task(task["id"]))
        self.assertFalse(out_dir.exists(), "取消后的删除要清掉产物目录")

    def test_delete_tasks_timeout_keeps_task_and_reports_reason(self):
        # 取消发出了但任务迟迟不停（模拟运行中任务还没收尾）：如实回报失败原因，
        # 任务保留，不伪装成已删除。
        task = self.store.create_task({"workflow_id": "cw-x", "shots": 1, "prompt": {}, "overrides": []})
        self.store.update_task(task["id"], status="running")
        result = self.jobs.delete_tasks(
            {"task_ids": [task["id"]], "cancel_active": True}, cancel_timeout=0.3)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["id"], task["id"])
        self.assertTrue(result["failed"][0]["reason"])
        self.assertIsNotNone(self.store.get_task(task["id"]))

    def test_delete_tasks_cancels_queued_task_immediately(self):
        # 排队中的任务取消是同步终态化：无需等待窗口。
        task = self.store.create_task({"workflow_id": "cw-x", "shots": 1, "prompt": {}, "overrides": []})
        jobs = ComfyUIJobManager(self.app, self.store, _StubComfyRunner(self.store))
        result = jobs.delete_tasks(
            {"task_ids": [task["id"]], "cancel_active": True}, cancel_timeout=5.0)
        self.assertEqual(result["deleted"], 1)
        self.assertIsNone(self.store.get_task(task["id"]))

    def test_delete_workflow_blocked_by_active_task(self):
        task = self.store.create_task({"workflow_id": self.entry["id"], "shots": 1,
                                       "prompt": {}, "overrides": []})
        self.store.update_task(task["id"], status="running")
        ok, reason = self.jobs.delete_workflow(self.entry["id"])
        self.assertFalse(ok)
        self.assertIn("取消", reason)
        # 任务终态后可删。
        self.store.update_task(task["id"], status="completed")
        ok, reason = self.jobs.delete_workflow(self.entry["id"])
        self.assertTrue(ok)


class ServiceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = _StubApp(self.dir)
        self.service = ComfyUIService(self.app)

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def test_import_from_json_string_and_detail(self):
        payload, status = self.service.api_workflow_create({
            "name": "粘贴导入", "workflow": json.dumps({"prompt": _sample_prompt()}),
        })
        self.assertEqual(status, 201)
        workflow_id = payload["workflow"]["id"]
        detail, status2 = self.service.api_workflow(workflow_id)
        self.assertEqual(status2, 200)
        self.assertEqual(detail["workflow"]["prompt"], _sample_prompt())
        self.assertTrue(any(s["field_type"] == "seed" for s in detail["workflow"]["params"]))

    def test_detail_upgrades_legacy_params_with_roles(self):
        # 模拟旧版导入：params 没有 role 字段 → 详情时懒升级补上并落盘。
        entry = {
            "id": self.service.store.new_workflow_id(), "name": "旧导入",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "created_at": 1, "updated_at": 1,
        }
        for slot in entry["params"]:
            slot.pop("role", None)
        self.service.store.save_workflow(entry)
        detail, _status = self.service.api_workflow(entry["id"])
        roles = {(s["node_id"], s["field_name"]): s.get("role")
                 for s in detail["workflow"]["params"]}
        self.assertEqual(roles[("6", "text")], "positive_prompt")
        # 升级结果已落盘：再取一次直接带 role（不再依赖重扫）。
        again = self.service.store.get_workflow(entry["id"])
        self.assertTrue(any(s.get("role") == "positive_prompt" for s in again["params"]))
        self.assertEqual(again.get("params_version"), PARAMS_SCHEMA_VERSION)

    def test_detail_upgrades_v1_params_to_new_roles(self):
        # v1 时代已升级过 prompt role 的工作流：schema 版本推进后仍要重扫一次，
        # 补上后加的 sampler/scheduler/checkpoint 角色，且升级结果落盘。
        entry = {
            "id": self.service.store.new_workflow_id(), "name": "v1 导入",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "created_at": 1, "updated_at": 1,
        }
        for slot in entry["params"]:
            if slot.get("role") in ("sampler", "scheduler", "checkpoint"):
                slot["role"] = ""
        self.service.store.save_workflow(entry)
        detail, _status = self.service.api_workflow(entry["id"])
        roles = {(s["node_id"], s["field_name"]): s.get("role")
                 for s in detail["workflow"]["params"]}
        self.assertEqual(roles[("3", "sampler_name")], "sampler")
        self.assertEqual(roles[("4", "ckpt_name")], "checkpoint")
        again = self.service.store.get_workflow(entry["id"])
        self.assertEqual(again.get("params_version"), PARAMS_SCHEMA_VERSION)

    def test_detail_retries_unconfirmed_media_optionality(self):
        prompt = {
            "10": {"class_type": "LoadImage", "inputs": {"image": "None"}},
            "20": {"class_type": "H3", "inputs": {
                "ref_images.ref_image_0": ["10", 0]}},
        }
        entry = self.service.store.save_workflow({
            "id": self.service.store.new_workflow_id(), "name": "媒体升级",
            "prompt": prompt, "params": scan_params(prompt),
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": 1, "updated_at": 1,
        })
        media_before = next(s for s in entry["params"] if s["node_id"] == "10")
        self.assertNotIn("media_optional", media_before)

        # 升级时 ComfyUI 离线：不查 schema、不落盘假结论。
        with mock.patch("naiba.comfyui.service.client.reachable", return_value=None), \
             mock.patch.object(NodeSchemaProvider, "_fetch") as fetch:
            detail, _status = self.service.api_workflow(entry["id"])
            fetch.assert_not_called()
        media_offline = next(s for s in detail["workflow"]["params"] if s["node_id"] == "10")
        self.assertIsNone(media_offline.get("media_optional"))

        # 下次打开详情且 ComfyUI 可达：只补媒体可选性并落盘。
        fields = {"ref_images": _autogrow_decl("ref_image_")}

        def _fetch(class_type):
            return fields if class_type == "H3" else None

        with mock.patch("naiba.comfyui.service.client.reachable", return_value={"ok": True}), \
             mock.patch.object(NodeSchemaProvider, "_fetch", side_effect=_fetch) as fetch:
            detail, _status = self.service.api_workflow(entry["id"])
            self.assertGreaterEqual(fetch.call_count, 1)
        media_online = next(s for s in detail["workflow"]["params"] if s["node_id"] == "10")
        self.assertTrue(media_online["media_optional"])
        saved = next(s for s in self.service.store.get_workflow(entry["id"])["params"]
                     if s["node_id"] == "10")
        self.assertTrue(saved["media_optional"])

        # 结论已确认后不再反复探测 schema。
        with mock.patch("naiba.comfyui.service.client.reachable",
                        side_effect=AssertionError("已确认的媒体槽不应再探 ComfyUI")):
            self.service.api_workflow(entry["id"])

    def test_workflow_visible_params_saved_and_restored(self):
        entry = self.service.store.save_workflow({
            "id": self.service.store.new_workflow_id(), "name": "n",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": 1, "updated_at": 1,
        })
        visible = [{"node_id": "6", "field_name": "text"},
                   {"node_id": "3", "field_name": "seed"}]
        payload, status = self.service.api_workflow_params_set(entry["id"], {"visible": visible})
        self.assertEqual(status, 200)
        self.assertEqual(payload["visible_params"], visible)
        # 按工作流持久化：重新读取详情能恢复，别的字段不受影响。
        detail, _status = self.service.api_workflow(entry["id"])
        self.assertEqual(detail["workflow"]["visible_params"], visible)

    def test_workflow_visible_params_rejects_invalid(self):
        entry = self.service.store.save_workflow({
            "id": self.service.store.new_workflow_id(), "name": "n",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": 1, "updated_at": 1,
        })
        for bad in ({"visible": "nope"},
                    {"visible": [{"node_id": "99", "field_name": "x"}]},
                    {"visible": ["not-an-object"]}):
            payload, status = self.service.api_workflow_params_set(entry["id"], bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("error", payload)
        self.assertNotIn("visible_params", self.service.store.get_workflow(entry["id"]))

    def test_workflow_param_order_saved_and_restored(self):
        # 用户口径（2026-10-25）：编辑参数清单能拖拽排序，保存后主表单按这个顺序显示。
        entry = self.service.store.save_workflow({
            "id": self.service.store.new_workflow_id(), "name": "n",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": 1, "updated_at": 1,
        })
        order = [{"node_id": "3", "field_name": "steps"},
                 {"node_id": "6", "field_name": "text"},
                 {"node_id": "3", "field_name": "seed"}]
        visible = order[:2]
        payload, status = self.service.api_workflow_params_set(
            entry["id"], {"visible": visible, "order": order})
        self.assertEqual(status, 200)
        self.assertEqual(payload["param_order"], order)
        # 顺序与勾选分开存：没勾选的行也保留在顺序里，下次打开清单还是这个次序。
        detail, _status = self.service.api_workflow(entry["id"])
        self.assertEqual(detail["workflow"]["param_order"], order)
        self.assertEqual(detail["workflow"]["visible_params"], visible)

    def test_workflow_param_order_optional_and_validated(self):
        entry = self.service.store.save_workflow({
            "id": self.service.store.new_workflow_id(), "name": "n",
            "prompt": _sample_prompt(), "params": scan_params(_sample_prompt()),
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": 1, "updated_at": 1,
        })
        order = [{"node_id": "6", "field_name": "text"}]
        self.service.api_workflow_params_set(entry["id"], {"visible": order, "order": order})
        # 不传 order 的旧调用方只改勾选，不把已有顺序清掉。
        payload, status = self.service.api_workflow_params_set(entry["id"], {"visible": []})
        self.assertEqual(status, 200)
        self.assertEqual(payload["param_order"], order)
        self.assertEqual(self.service.store.get_workflow(entry["id"])["param_order"], order)
        # 越权槽位与格式错误一律 400，且不落盘半份配置。
        for bad in ({"visible": [], "order": "nope"},
                    {"visible": [], "order": [{"node_id": "99", "field_name": "x"}]},
                    {"visible": [], "order": ["not-an-object"]}):
            payload, status = self.service.api_workflow_params_set(entry["id"], bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("error", payload)
        self.assertEqual(self.service.store.get_workflow(entry["id"])["param_order"], order)

    def test_api_models_unreachable_returns_empty_with_error(self):
        # ComfyUI 不可达：空候选 + 说明；不阻断工作流编辑与提交（前端有自由输入）。
        with mock.patch.object(comfyui_client, "checkpoint_names", return_value=[]), \
             mock.patch.object(comfyui_client, "lora_names", return_value=[]):
            payload, status = self.service.api_models()
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["models"], [])
        self.assertEqual(payload["loras"], [])
        self.assertIn("手动输入", payload["error"])

    def test_api_models_returns_candidates(self):
        with mock.patch.object(comfyui_client, "checkpoint_names",
                               return_value=["a.safetensors", "b.safetensors"]), \
             mock.patch.object(comfyui_client, "lora_names", return_value=["l.safetensors"]):
            payload, _status = self.service.api_models()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["models"], ["a.safetensors", "b.safetensors"])
        self.assertEqual(payload["loras"], ["l.safetensors"])

    def test_api_models_ok_when_only_loras(self):
        # 有 LoRA 没 checkpoint 也算候选可用（ok 只看两者任一非空）。
        with mock.patch.object(comfyui_client, "checkpoint_names", return_value=[]), \
             mock.patch.object(comfyui_client, "lora_names", return_value=["l.safetensors"]):
            payload, _status = self.service.api_models()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["models"], [])
        self.assertEqual(payload["loras"], ["l.safetensors"])

    def test_tasks_delete_cancels_active_when_requested(self):
        # 门面集成：cancel_active 走真 runner——排队任务直接终态化后删除。
        self.service.store.save_workflow({
            "id": "cw-del", "name": "n", "prompt": _sample_prompt(), "params": [],
            "params_version": PARAMS_SCHEMA_VERSION,
            "created_at": 1, "updated_at": 1,
        })
        task = self.service.store.create_task({"workflow_id": "cw-del", "shots": 1,
                                               "prompt": {}, "overrides": []})
        payload, status = self.service.api_tasks_delete({
            "task_ids": [task["id"]], "cancel_active": True, "cancel_timeout": 5,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["deleted"], 1)
        self.assertIsNone(self.service.store.get_task(task["id"]))

    def test_health_reports_unreachable(self):
        payload, status = self.service.api_health()
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("ComfyUI", payload["error"])

    def test_set_url_saves_and_reports_reachability(self):
        # 可达（打桩）：地址入库并返回 reachable=True。
        with mock.patch.object(comfyui_client, "reachable", return_value={"version": "v1"}):
            payload, status = self.service.api_set_url({"url": "127.0.0.1:8188/"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["reachable"])
        self.assertEqual(payload["url"], "http://127.0.0.1:8188")
        self.assertEqual(self.app.config.data["comfyui_url"], "http://127.0.0.1:8188")
        # 不可达：保存成功但如实告知连不上。
        with mock.patch.object(comfyui_client, "reachable", return_value=None):
            payload2, status2 = self.service.api_set_url({"url": "http://127.0.0.1:9"})
        self.assertEqual(status2, 200)
        self.assertFalse(payload2["reachable"])
        self.assertIn("连不上", payload2["error"])

    def test_set_url_rejects_bad_format(self):
        original = self.app.config.data.get("comfyui_url")
        for bad in ("", "   ", "ftp://x", "http://"):
            payload, status = self.service.api_set_url({"url": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("http", payload["error"])
        self.assertEqual(self.app.config.data.get("comfyui_url"), original,
                         "非法地址不能写进配置")

    def test_set_url_keeps_path_prefix(self):
        with mock.patch.object(comfyui_client, "reachable", return_value=None):
            payload, _status = self.service.api_set_url({"url": "http://192.168.1.5:8188/comfy/"})
        self.assertEqual(payload["url"], "http://192.168.1.5:8188/comfy")
        self.assertEqual(self.app.config.data["comfyui_url"], "http://192.168.1.5:8188/comfy")

    def test_task_file_rejects_traversal_and_missing(self):
        task = self.service.store.create_task({"shots": 1, "prompt": {}, "overrides": []})
        out_dir = self.service.store.task_output_dir(task["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "ok.png").write_bytes(b"x")
        self.assertIsNotNone(self.service.task_file(task["id"], "ok.png"))
        self.assertIsNone(self.service.task_file(task["id"], "../secret.png"))
        self.assertIsNone(self.service.task_file(task["id"], "a/b.png"))
        self.assertIsNone(self.service.task_file(task["id"], ".hidden"))
        self.assertIsNone(self.service.task_file(task["id"], "missing.png"))
        self.assertIsNone(self.service.task_file("cf-none", "ok.png"))

    def test_reveal_outputs_opens_managed_dir(self):
        import naiba.comfyui.service as service_module

        with mock.patch.object(service_module, "_open_folder") as opener:
            payload, status = self.service.api_reveal_outputs()
        self.assertEqual(status, 200)
        expected = self.service.store.outputs_dir.resolve()
        self.assertEqual(payload["path"], str(expected))
        self.assertTrue(expected.is_dir(), "目录不存在时要先建出来")
        opener.assert_called_once_with(expected)

    def test_reveal_outputs_reports_oserror(self):
        import naiba.comfyui.service as service_module

        with mock.patch.object(service_module, "_open_folder", side_effect=OSError("boom")):
            payload, status = self.service.api_reveal_outputs()
        self.assertEqual(status, 500)
        self.assertIn("无法打开产物文件夹", payload["error"])

    def test_referenced_upload_paths_protects_active_and_retryable(self):
        # 计划 §5.3：排队/运行中 + 可重试（interrupted/failed）任务引用的输入媒体
        # 受缓存清理保护；completed/cancelled 不保护（沿用通用清理策略）。
        from naiba.core.paths import normalized_path_key

        ref = {"node_id": "30", "field_name": "image", "path": "C:/data/uploads/a.png",
               "media": "image", "name": "a.png", "size": 3}
        task = self.service.store.create_task({
            "shots": 1, "prompt": {}, "overrides": [], "media_inputs": [ref],
        })
        expected = {normalized_path_key("C:/data/uploads/a.png")}
        for status in ("queued", "failed", "interrupted"):
            self.service.store.update_task(task["id"], status=status)
            self.assertEqual(self.service.referenced_upload_paths(), expected, status)
        for status in ("completed", "cancelled"):
            self.service.store.update_task(task["id"], status=status)
            self.assertEqual(self.service.referenced_upload_paths(), set(), status)

    def test_outputs_folder_button_wired_in_ui(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "public/index.html").read_text(encoding="utf-8")
        js = (root / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        self.assertIn('id="comfyuiOpenOutputs"', html)
        self.assertIn('id="comfyuiFreeModels"', html)
        self.assertIn("/api/comfyui/outputs/open", js)
        self.assertIn("/api/comfyui/free", js)
        self.assertIn("#comfyuiOpenOutputs", js)
        self.assertIn("#comfyuiFreeModels", js)
        # 按钮名按用户拍板用短名；完整含义放在 title 里。
        self.assertIn(">产物</button>", html)
        self.assertIn(">清除</button>", html)
        self.assertIn(">释放模型</button>", html)

    def test_media_controls_wired_in_ui(self):
        # 计划 §4-§6 前端接线：媒体选择上传、多行/下拉控件、提交带 media_inputs、
        # 产物卡走公共 mediaKind（音频可播）。
        root = Path(__file__).resolve().parents[1]
        js = (root / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        self.assertIn('"/api/comfyui/media"', js)
        self.assertIn("media_inputs: collectMediaInputs()", js)
        self.assertIn("attachCandidateCombo", js)
        css = (root / "public/styles.css").read_text(encoding="utf-8")
        menu_rule = css.split(".comfyui-combo-list {", 1)[1].split("}", 1)[0]
        self.assertIn("position: fixed", menu_rule)
        self.assertIn("display: block", menu_rule)
        self.assertIn("overflow-y: auto", menu_rule)
        hidden_rule = css.split(".comfyui-combo-list[hidden] {", 1)[1].split("}", 1)[0]
        self.assertIn("display: none", hidden_rule)
        self.assertIn("document.body.append(list)", js)
        self.assertIn("getBoundingClientRect()", js)
        self.assertIn('slot.control === "multiline"', js)
        self.assertIn("renderMediaPicker", js)
        self.assertIn('slot.control === "combo"', js)
        self.assertIn("mediaKind(", js)
        self.assertIn("attachmentThumbUrl(", js)
        self.assertIn('createElement("audio")', js)
        self.assertIn("slot.media_optional === true", js)
        self.assertIn("未选即跳过", js)
        self.assertIn("未选沿用原文件", js)
        self.assertIn("请选择文件", js)

    def test_free_models_reports_pending_when_tasks_active(self):
        import naiba.comfyui.service as service_module

        with mock.patch.object(service_module.client, "reachable", return_value={}), \
             mock.patch.object(service_module.client, "free_models", return_value=(True, "")) as free:
            payload, status = self.service.api_free_models()
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        free.assert_called_once()
        # 无活动任务：不需要「稍后生效」提示。
        self.assertFalse(payload["pending"])

    def test_free_models_unreachable_keeps_message(self):
        import naiba.comfyui.service as service_module

        with mock.patch.object(service_module.client, "reachable", return_value=None):
            payload, status = self.service.api_free_models()
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("连不上 ComfyUI", payload["error"])

    def test_slim_tasks_hide_checkpoint(self):
        self.service.store.save_workflow({
            "id": "cw-x", "name": "n", "prompt": _sample_prompt(), "params": [],
            "created_at": 1, "updated_at": 1,
        })
        self.service.api_submit({"workflow_id": "cw-x", "shots": 1})
        rows, _ = self.service.api_tasks()
        self.assertEqual(len(rows["tasks"]), 1)
        self.assertNotIn("checkpoint", rows["tasks"][0])
        self.assertIn("workflow_name", rows["tasks"][0])
        # 详情轮询与列表返回同一套精简字段，否则第一张任务卡的标题会在
        # 「任务」和工作流名之间来回闪。
        detail, _ = self.service.api_task(rows["tasks"][0]["id"])
        self.assertNotIn("checkpoint", detail["task"])
        self.assertEqual(detail["task"]["workflow_name"], "n")
        self.assertEqual(detail["task"]["shots"], 1)
        self.assertIn("can_cancel", detail["task"])

    def test_slim_tasks_include_output_file_paths(self):
        # 前端要拿绝对路径去走 /api/file：手机/局域网靠 URL 口令取字节，
        # 桌面靠 path 参数弹原生「另存为」——只回文件名两条链路都断了。
        task = self.service.store.create_task({"shots": 1, "prompt": {}, "overrides": []})
        out_dir = self.service.store.task_output_dir(task["id"])
        out_dir.mkdir(parents=True)
        (out_dir / "a.png").write_bytes(b"x")
        self.service.store.update_task(
            task["id"], status="completed",
            result={"completed": [], "outputs": ["a.png"]},
        )
        rows, _ = self.service.api_tasks()
        row = next(item for item in rows["tasks"] if item["id"] == task["id"])
        self.assertEqual(
            row["output_files"],
            [{"name": "a.png", "path": str(out_dir / "a.png")}],
        )


class MediaGroupCardTests(unittest.TestCase):
    """造梦机媒体输入「按类型分组卡片 + 拖拽」（2026-10-26 计划 §5 自动化）。

    前端渲染层源码断言：分组归堆、块形态与四态文案、拖拽接线、触屏常驻按钮、
    身份键与提交结构不变（后端契约零改动）。
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.js = (root / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        cls.css = (root / "public/styles.css").read_text(encoding="utf-8")

    def _rule(self, marker):
        return self.css.split(marker, 1)[1].split("}", 1)[0]

    def _fn(self, signature):
        return self.js.split(signature, 1)[1].split("\nfunction ", 1)[0]

    def test_media_slots_grouped_by_kind(self):
        # 归堆判定沿用 control/field_type 口径，与节点号、节点名、端口名解耦。
        self.assertIn('const MEDIA_GROUP_ORDER = ["image", "audio", "video"]', self.js)
        self.assertIn('slot.control !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type)', self.js)
        # 三组按图片→音频→视频出卡；没有该类型槽就不出该卡。
        self.assertIn("function renderMediaGroup(kind, slots)", self.js)
        self.assertIn("if (groupSlots.length) form.append(renderMediaGroup(kind, groupSlots));", self.js)
        # 组内保持 orderedSlots() 的原顺序（只按进入顺序归堆，不再排序）。
        self.assertIn("mediaGroups.get(kind).push(slot);", self.js)
        # 非媒体槽照旧逐槽渲染，顺序不变。
        self.assertIn("form.append(renderSlot(item.slot));", self.js)
        # 边缘数据（control=media 但类型不在三集合）走老行渲染，不丢控件。
        picker = self._fn("function renderMediaPicker(slot, key) {")
        self.assertIn("comfyui-media-preview", picker)
        self.assertIn('createElement("audio")', picker)

    def test_media_tile_status_and_port_text(self):
        # 四态文案一字不改；判定仍是 slot.media_optional === true。
        self.assertIn("slot.media_optional === true", self.js)
        self.assertIn("`已选：${picked.name}`", self.js)
        for text in ("未选即跳过", "请选择文件", "未选沿用原文件"):
            self.assertIn(text, self.js)
        status = self._fn("function mediaSlotStatus(slot, picked) {")
        self.assertIn('badge: "已选"', status)
        self.assertIn('badge: "未选即跳过"', status)
        self.assertIn('badge: "请选择文件"', status)
        self.assertIn('badge: "沿用原文件"', status)
        # 块底端口小字带来源节点与目标端口；完整映射保留在块 title。
        self.assertIn("target_field", self.js)
        self.assertIn("mediaPortText(slot)", self.js)
        self.assertIn("tile.title = `${slot.label", self.js)
        # 沿用原文件的槽按实块渲染（不是虚线空块）。
        self.assertIn("const keepOriginal = !picked && status.cls === \"keep\";", self.js)

    def test_media_drag_drop_wiring(self):
        # 块与卡片都挂 dragover/dragleave/drop，dragover 与 drop 都 preventDefault
        # （不声明「这里可以放」就收不到 drop）。
        for signature in ("function wireMediaTileDrop(tile, key, kind) {",
                          "function wireMediaGroupDrop(group, tiles, slots, kind) {"):
            block = self._fn(signature)
            for name in ("dragover", "dragleave", "drop"):
                self.assertIn(f'"{name}"', block, signature)
            self.assertEqual(block.count("event.preventDefault()"), 2, signature)
        # 块的 drop 不冒泡给卡片；卡片按空位顺序填、多的拒绝并提示。
        tile_drop = self._fn("function wireMediaTileDrop(tile, key, kind) {")
        self.assertIn("event.stopPropagation()", tile_drop)
        self.assertIn("const empties = slots.filter", self.js)
        self.assertIn("slotKey(empties[index])", self.js)
        self.assertIn("多出的", self.js)
        self.assertIn("请拖到具体块上替换", self.js)
        # 类型不匹配拒绝并指明应拖到哪张卡；目录拖放直接拒绝。
        self.assertIn("function toastMediaKindMismatch(file, kind) {", self.js)
        # 指路必须是文件自己类型的卡（音频拖到图片卡要说「请拖到音频卡」），
        # 不是当前拖到的目标卡。
        self.assertIn("请拖到${MEDIA_KIND_LABELS[actual]}卡", self.js)
        self.assertIn("不支持拖文件夹", self.js)
        self.assertIn("function dropHasDirectory(event) {", self.js)
        # 多文件串行上传（单槽锁锁整批），全部完成后只重绘一次。
        upload = self.js.split("async function uploadMediaFiles(items) {", 1)[1] \
            .split("\nfunction renderMediaPicker", 1)[0]
        self.assertIn("for (const { key, file } of items) {", upload)
        self.assertEqual(upload.count("rerenderParamForm()"), 1)
        self.assertIn('comfyui.uploadingSlot = items.map((item) => item.key).join("|")', upload)
        # 点选路径共用同一上传实现。
        picked = self._fn("async function onMediaPicked(slot, key, fileInput) {")
        self.assertIn("await uploadMediaFiles([{ key, file }]);", picked)

    def test_touch_devices_keep_tile_actions_visible(self):
        # 触屏没有 hover：替换/清除常驻显示，命中区 ≥22px（§4.4）。
        # styles.css 里有多个 (hover: none) 媒体块，取包含本模块规则的那一块。
        media_blocks = [
            block.split("\n}", 1)[0]
            for block in self.css.split("@media (hover: none) {")[1:]
        ]
        hover = next(block for block in media_blocks if ".comfyui-media-tile .t-actions" in block)
        self.assertIn(".comfyui-media-tile .t-actions { display: flex; }", hover)
        self.assertIn(".comfyui-media-tile .t-actions button { width: 22px; height: 22px; }", hover)

    def test_media_tile_grid_and_states_css(self):
        tiles = self._rule(".comfyui-media-tiles {")
        self.assertIn("repeat(auto-fill, minmax(96px, 1fr))", tiles)
        empty = self._rule(".comfyui-media-tile.empty {")
        self.assertIn("border-style: dashed", empty)
        collapsed = self.css.split(
            ".comfyui-media-group.collapsed .comfyui-media-tiles,", 1)[1].split("}", 1)[0]
        self.assertIn("display: none", collapsed)
        dragover = self._rule(".comfyui-media-tile.dragover {")
        self.assertIn("border-color: var(--accent)", dragover)
        card_drop = self._rule(".comfyui-media-tiles.card-drop {")
        self.assertIn("var(--accent)", card_drop)
        hint = self._rule(".comfyui-media-tiles.card-drop + .comfyui-media-drop-hint {")
        self.assertIn("display: block", hint)
        audio_flat = self._rule(".comfyui-media-tile.empty.audio {")
        self.assertIn("min-height: 58px", audio_flat)

    def test_media_group_head_and_collapse(self):
        group = self._fn("function renderMediaGroup(kind, slots) {")
        self.assertIn("comfyui.mediaGroupsCollapsed[kind] === true", group)
        self.assertIn("aria-expanded", group)
        self.assertIn("`${pickedCount}/${slots.length} 已选`", group)
        self.assertIn("`${slots.length} 个输入`", group)
        # 折叠只是内存态：不落盘、不进 visible_params（saveParamsConfig 不碰它）。
        save = self._fn("async function saveParamsConfig() {")
        self.assertNotIn("mediaGroupsCollapsed", save)

    def test_media_identity_and_submit_path_unchanged(self):
        # 身份键仍是 node_id|field_name；collectMediaInputs 输出结构不变。
        self.assertIn("return `${slot.node_id}|${slot.field_name}`;", self.js)
        collect = self._fn("function collectMediaInputs() {")
        self.assertIn(
            '(slot.control || "") !== "media" && !MEDIA_FIELD_TYPES.has(slot.field_type)',
            collect)
        self.assertIn(
            "items.push({ node_id: slot.node_id, field_name: slot.field_name, path: picked.path })",
            collect)
        self.assertIn("media_inputs: collectMediaInputs()", self.js)
        # 首次导入默认可见：旧格式显示集仍强制合并媒体 key，分组卡无需重新导入；
        # 保存过一次（v2 标记）后按用户实际勾选执行——取消勾选的媒体槽要能消失。
        visible = self._fn("function visibleKeySet() {")
        self.assertIn("if (!visibleSetKnowsMedia()) {", visible)
        self.assertIn("MEDIA_FIELD_TYPES.has(slot.field_type)) set.add(slotKey(slot))", visible)
        save = self._fn("async function saveParamsConfig() {")
        self.assertIn("markVisibleSetKnowsMedia();", save)


class ParamsPickerOrderTests(unittest.TestCase):
    """「编辑参数」清单拖拽排序（2026-10-25 用户需求）。

    源码层断言：行可拖、拖拽落序走 DOM、保存时把整份顺序发给后端、主表单按保存的
    顺序渲染；触屏另给上下移按钮，落序口径与拖拽一致。
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.js = (root / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        cls.css = (root / "public/styles.css").read_text(encoding="utf-8")

    def _fn(self, signature):
        return self.js.split(signature, 1)[1].split("\nfunction ", 1)[0]

    def test_picker_rows_are_draggable(self):
        picker = self._fn("function renderParamsPicker() {")
        self.assertIn("row.draggable = true", picker)
        self.assertIn('row.dataset.paramKey = slotKey(slot)', picker)
        self.assertIn("moveButtons(row)", picker)
        # 拖拽与上下移都改 DOM 顺序，保存读的也是 DOM 顺序，两条路径不会各存一份。
        drag = self._fn("function bindParamsPickerDrag() {")
        self.assertIn('addEventListener("dragstart"', drag)
        self.assertIn('addEventListener("dragover"', drag)
        self.assertIn("picker.insertBefore(dragging,", drag)
        self.assertIn("event.clientY > rect.top + rect.height / 2", drag)
        move = self._fn("function movePickerRow(row, direction) {")
        self.assertIn("picker.insertBefore(row, sibling)", move)
        self.assertIn("picker.insertBefore(sibling, row)", move)

    def test_save_sends_order_and_form_renders_it(self):
        save = self._fn("async function saveParamsConfig() {")
        self.assertIn("order.push(row)", save)
        self.assertIn("body: { visible, order }", save)
        self.assertIn("param_order: payload.param_order || order", save)
        order_fn = self._fn("function orderedSlots(slots) {")
        self.assertIn("savedOrderIndex()", order_fn)
        # 没拖过的槽（换了工作流版本新扫出来的）排在已排序之后，不会被藏掉。
        self.assertIn("if (rankA === undefined) return 1;", order_fn)
        self.assertIn("function savedOrderIndex() {", self.js)

    def test_drag_handle_and_move_buttons_styled(self):
        item_rule = self.css.split(".comfyui-params-item {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius", item_rule)
        handle_rule = self.css.split(".comfyui-params-handle {", 1)[1].split("}", 1)[0]
        self.assertIn("cursor: grab", handle_rule)
        self.assertIn("flex: none", handle_rule)
        move_rule = self.css.split(".comfyui-params-move {", 1)[1].split("}", 1)[0]
        self.assertIn("flex: none", move_rule)
        # 触屏没有 hover：上下移按钮必须常显，否则手机上没法排序。
        touch = self.css.split("@media (hover: none) {", 1)[1]
        self.assertIn(".comfyui-params-move { opacity: 1; }", touch)


class ParamValueDraftTests(unittest.TestCase):
    """主表单参数值的保留与「默认」还原（用户口径 2026-10-29）。

    一条规则拆两半：
    1. 值有草稿——表单一重绘（切页回来、传完媒体、保存显示集）就按草稿复位，
       刷新不会再把手填的提示词打回工作流原值；
    2. 「默认」只管值，不动「编辑」保存的显示项与顺序。
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.js = (root / "public/js/24-comfyui.js").read_text(encoding="utf-8")

    def _fn(self, signature):
        return self.js.split(signature, 1)[1].split("\nfunction ", 1)[0]

    def _bind(self):
        return self.js.split("export function bindComfyuiEvents() {", 1)[1].split("\n}\n", 1)[0]

    def test_slot_values_come_from_draft_first(self):
        self.assertIn("drafts: new Map()", self.js)
        initial = self._fn("function initialFieldValue(slot, key) {")
        self.assertIn("savedDraft(key)", initial)
        self.assertIn('slot.default ?? ""', initial)
        # 草稿键带工作流 id：不同工作流的同名节点端口（3|seed）不能互相串值。
        draft_key = self._fn("function draftKey(key) {")
        self.assertIn('comfyui.detail?.id || ""', draft_key)
        # 每种带值的控件都走同一个取值入口，不能只改一半。
        slot_fn = self._fn("function renderSlot(slot) {")
        self.assertEqual(slot_fn.count("initialFieldValue(slot, key)"), 4, slot_fn)
        self.assertIn("savedDraft(key)", slot_fn)

    def test_typing_records_draft(self):
        bind = self._bind()
        self.assertIn('for (const type of ["input", "change"])', bind)
        self.assertIn("rememberDraft(", bind)
        remember = self._fn("function rememberDraft(key, value) {")
        self.assertIn("drafts.set(draftKey(key)", remember)

    def test_default_button_only_resets_values(self):
        reset = self._fn("function resetParamValues() {")
        # 只清这份工作流的草稿，再按工作流原值重绘。
        self.assertIn("comfyui.drafts.delete(key)", reset)
        self.assertIn("renderParamForm()", reset)
        # 不许碰「编辑」保存的显示项与顺序。
        self.assertNotIn("visible_params", reset)
        self.assertNotIn("param_order", reset)
        self.assertNotIn("/params", reset)
        render = self._fn("function renderParamForm() {")
        # 「编辑」按钮文案收短成「编辑」，「默认」并排放在同一组里。
        self.assertIn('edit.textContent = "编辑"', render)
        self.assertIn('reset.textContent = "默认"', render)
        self.assertIn('dataset.comfyuiResetParams = "1"', render)
        self.assertIn('dataset.comfyuiEditParams = "1"', render)
        self.assertIn("actions.append(reset, edit)", render)

    def test_default_click_wired(self):
        bind = self._bind()
        self.assertIn('"[data-comfyui-reset-params]"', bind)
        self.assertIn("resetParamValues()", bind)


class ComfyuiStatusbarLayoutTests(unittest.TestCase):
    """「改地址 / 测试连接」不许越出卡片（用户 2026-10-29：电脑端与手机端都被挤出边框）。

    根因是三栏网格：左右两栏各自有固定上限，中栏被压到几十像素（1024 视口实测 46px），
    两个按钮的 min-content 只能溢出。修法两条并用：状态栏允许换行兜住极端宽度；
    页面自身宽度不足时改三档分页，中栏拿满宽度。判档口径是**页面宽度**而不是视口。
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.js = (root / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        cls.css = (root / "public/styles.css").read_text(encoding="utf-8")
        cls.html = (root / "public/index.html").read_text(encoding="utf-8")

    def _rule(self, marker):
        return self.css.split(marker, 1)[1].split("}", 1)[0]

    def test_statusbar_wraps_instead_of_overflowing(self):
        bar = self._rule(".comfyui-statusbar {")
        self.assertIn("flex-wrap: wrap", bar)
        text = self._rule(".comfyui-status-text {")
        # 基准必须是小值：换行容器按 max-content 排布，长地址会独占一整行、
        # 把两个按钮挤到第二行（1200px 视口实测过）。
        self.assertIn("flex: 1 1 120px", text)
        self.assertIn("min-width: 0", text)
        self.assertIn("text-overflow: ellipsis", text)
        # 按钮保持自身宽度、参与换行而不是被压扁或顶出边界。
        self.assertIn(".comfyui-statusbar .control-button { flex: none; }", self.css)

    def test_narrow_mode_uses_page_width(self):
        self.assertIn("NARROW_PAGE_WIDTH", self.js)
        watch = self.js.split("function watchComfyuiWidth() {", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("ResizeObserver", watch)
        self.assertIn("getBoundingClientRect().width", watch)
        self.assertIn('root.dataset.comfyuiNarrow = "1"', watch)
        # 判档必须挂在页面宽度上，不许再回到 matchMedia(视口)。
        self.assertNotIn('matchMedia("(max-width: 760px)")', self.js)
        self.assertIn('data-comfyui-narrow="1"', self.css)
        # 窄档下三档条可见、三栏只显示一栏。
        self.assertIn('#studioPageComfyui[data-comfyui-narrow="1"] .comfyui-panes { display: flex; }', self.css)
        self.assertIn('[data-comfyui-narrow="1"][data-comfyui-pane="run"] .comfyui-run-col', self.css)
        # 页面初始就带档位，首帧不会先按三栏渲染再跳。
        self.assertIn('data-comfyui-pane="run"', self.html)


class TaskCardLayoutTests(unittest.TestCase):
    def test_long_workflow_title_shrinks_before_status_badge(self):
        """任务标题行必须允许标题收缩并省略，不能压到状态标签上。

        任务数一多、结果栏出现纵向滚动条时，任务卡可用宽度会再窄一截；flex 子项
        默认 min-width:auto，长的 workflow_name 会溢出自己的盒子，与右侧状态标签、
        相邻卡片边界叠在一起。标题显式 min-width:0 + flex:1 才能省略。
        """
        root = Path(__file__).resolve().parents[1]
        css = (root / "public/styles.css").read_text(encoding="utf-8")
        head_rule = css.split(".comfyui-task-head {", 1)[1].split("}", 1)[0]
        self.assertIn("min-width: 0", head_rule, "标题行本身也不能撑破网格列")
        title_rule = css.split(".comfyui-task-head strong {", 1)[1].split("}", 1)[0]
        self.assertIn("min-width: 0", title_rule, "标题必须能缩到内容宽度以下才会省略")
        self.assertIn("flex: 1 1 auto", title_rule)
        self.assertIn("text-overflow: ellipsis", title_rule)
        self.assertIn("white-space: nowrap", title_rule)
        status_rule = css.split(".comfyui-task-status {", 1)[1].split("}", 1)[0]
        self.assertIn("flex: none", status_rule, "状态标签保持自身宽度，不被标题挤压")


if __name__ == "__main__":
    unittest.main()
