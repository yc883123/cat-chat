"""工作流直建批次（批量执行助手）：映射用户定（不检查不猜）、快照执行、时长升格与预检。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from naiba.comfyui.params import scan_params
from naiba.comfyui.store import ComfyUIStore
from naiba.storage.store import ChatStorage
from naiba.super_agent.recipe_store import digest
from naiba.super_agent.recipes import RecipeCoordinator
from naiba.super_agent.service import SuperAgentService

# 带正向连线锚点 + 时长槽 + 两个可选媒体入口的最小工作流。
HANDOFF_GRAPH = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "默认正文"}},
    "2": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "steps": 20}},
    "3": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
    "4": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
    "5": {"class_type": "PrimitiveFloat", "inputs": {"value": 15}, "_meta": {"title": "视频时长（秒）"}},
}
# 无采样器锚点、两个裸文本槽：用途只有用户知道——不再报错，映射由用户指定。
AMBIGUOUS_GRAPH = {
    "1": {"class_type": "NoteNode", "inputs": {"text": "甲"}},
    "2": {"class_type": "NoteNode", "inputs": {"text": "乙"}},
}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.image = self.data / "uploads" / "original.png"
        self.image.parent.mkdir(parents=True)
        Image.new("RGB", (4, 4), "red").save(self.image)
        self.storage = ChatStorage(self.root / "chat.db")
        self.service = SuperAgentService(self.storage)
        self.studio = SimpleNamespace(jobs=SimpleNamespace(lookup_request=lambda *a, **k: None, store=SimpleNamespace(get_task=lambda task_id: None)))
        self.workflow_store = ComfyUIStore(self.data)
        self.workflow_store.save_workflow({"id": "cw-1", "name": "测试工作流", "prompt": copy.deepcopy(HANDOFF_GRAPH),
                                           "params": scan_params(HANDOFF_GRAPH), "created_at": 1, "updated_at": 1})
        self.workflow_store.save_workflow({"id": "cw-ambig", "name": "歧义工作流", "prompt": copy.deepcopy(AMBIGUOUS_GRAPH),
                                           "params": scan_params(AMBIGUOUS_GRAPH), "created_at": 1, "updated_at": 1})
        self.app = SimpleNamespace(paths=SimpleNamespace(data_dir=self.data),
                                   config=SimpleNamespace(data={"comfyui_url": "http://127.0.0.1:8188"}, profile=lambda key: {}),
                                   studio=self.studio,
                                   comfyui=SimpleNamespace(jobs=SimpleNamespace(), store=self.workflow_store))
        self.core = RecipeCoordinator(self.app, self.service)
        self.fp = patch.object(RecipeCoordinator, "_fingerprint", side_effect=digest)
        self.fp.start()
        self.addCleanup(self.fp.stop)

    def handoff(self, *, workflow_id="cw-1", **extra):
        body = {"source": "workflow", "request_id": extra.pop("request_id", "handoff-1"),
                "workflow_id": workflow_id, "shots": 2, **extra}
        return self.core.create("owner", body)

    def test_mapping_from_body_is_taken_verbatim(self):
        mapping = {"prompt_slot": {"node_id": "1", "field_name": "text"},
                   "duration_slot": {"node_id": "5", "field_name": "value"},
                   "media_slots": [{"node_id": "3", "field_name": "image", "media": "image"}]}
        created = self.handoff(**mapping)
        batch = created["batch"]
        recipe = batch["recipe"]
        self.assertEqual(recipe["provenance"]["source"], "workflow_handoff")
        self.assertEqual(recipe["mode"], "custom")
        self.assertEqual(batch["input_mode"], "prompt")
        # 映射原样收：正文默认从映射槽位带入工作流保存值。
        self.assertEqual(recipe["prompt_slot"], {"node_id": "1", "field_name": "text"})
        self.assertEqual(batch["items"][0]["prompt"], "默认正文")
        self.assertEqual(recipe["duration_slot"], {"node_id": "5", "field_name": "value"})
        self.assertEqual(batch["duration_seconds"], 15)
        self.assertEqual([(slot["role"], slot["media"], slot["scope"]) for slot in recipe["media_slots"]],
                         [("m3_image", "image", "item")])
        # 方案快照注册为自己的不可变条目：提交按快照跑，不随库工作流漂移。
        snapshot_id = recipe["workflow_id"]
        self.assertNotEqual(snapshot_id, "cw-1")
        self.assertIsNotNone(self.workflow_store.get_workflow(snapshot_id))
        # 派生定义按内容幂等：换 request_id 再交接，落到同一个不可变定义。
        again = self.handoff(request_id="handoff-again", **mapping)
        self.assertEqual(again["batch"]["recipe"]["id"], batch["recipe"]["id"])

    def test_unmapped_slots_are_simply_not_mapped(self):
        # 没传映射的槽位就不映射、不参与批量填参；不再自动猜测。
        created = self.handoff()
        recipe = created["batch"]["recipe"]
        self.assertIsNone(recipe["prompt_slot"])
        self.assertIsNone(recipe.get("duration_slot"))
        self.assertEqual(recipe["media_slots"], [])
        self.assertEqual(created["batch"]["items"][0]["prompt"], "")

    def test_prompt_without_text_slot_is_rejected_as_inconsistent(self):
        # 自己带了正文却没有文本写入位：正文没地方去，属映射不自洽，明确拒绝。
        with self.assertRaisesRegex(ValueError, "文本写入位"):
            self.handoff(prompt="用户正文")

    def test_ambiguous_text_slots_no_longer_block(self):
        created = self.handoff(workflow_id="cw-ambig", prompt="正文", prompt_slot={"node_id": "2", "field_name": "text"})
        self.assertEqual(created["batch"]["recipe"]["prompt_slot"], {"node_id": "2", "field_name": "text"})
        # 也不因「两个文本槽」报错：不指定就什么都不映射。
        created = self.handoff(workflow_id="cw-ambig", request_id="handoff-quiet")
        self.assertIsNone(created["batch"]["recipe"]["prompt_slot"])

    def test_media_inputs_are_adopted_as_confirmed_mapping(self):
        # 造梦机带过来的素材（用户已明确选了文件）= 已确认映射，自动收编；
        # 不再要求「素材入口必须属于既有媒体槽位」。
        created = self.handoff(media_inputs=[{"node_id": "3", "field_name": "image", "path": str(self.image)},
                                             {"node_id": "9", "field_name": "image", "path": str(self.image)}])
        media = created["batch"]["items"][0]["media"]
        self.assertEqual([row["role"] for row in media], ["m3_image", "m9_image"])
        recipe = created["batch"]["recipe"]
        self.assertEqual({(slot["node_id"], slot["field_name"]) for slot in recipe["media_slots"]},
                         {("3", "image"), ("9", "image")})

    def test_stale_fingerprint_no_longer_blocks(self):
        created = self.handoff(workflow_fingerprint="stale-or-wrong")
        self.assertEqual(created["batch"]["recipe"]["workflow_fingerprint"], digest(HANDOFF_GRAPH))

    def test_duration_override_is_promoted_not_fixed(self):
        created = self.handoff(prompt_slot={"node_id": "1", "field_name": "text"},
                               duration_slot={"node_id": "5", "field_name": "value"},
                               overrides=[{"node_id": "5", "field_name": "value", "value": 5},
                                          {"node_id": "2", "field_name": "steps", "value": 8}])
        batch = created["batch"]
        self.assertEqual(batch["duration_seconds"], 5)
        fixed = {(row["node_id"], row["field_name"]) for row in batch["overrides"]}
        self.assertNotIn(("5", "value"), fixed)
        self.assertIn(("2", "steps"), fixed)

    def test_handoff_script_mode_uses_template_and_skips_model(self):
        # 反推模式：模板由用户选（H3 预设可直用）；反推模型可进批次后再补。
        created = self.handoff(prompt_slot={"node_id": "1", "field_name": "text"},
                               input_mode="script", template_id="recipe_minimax_h3_r2v_v2", requirements="剧本要求")
        batch = created["batch"]
        self.assertEqual(batch["input_mode"], "script")
        self.assertEqual(batch["template"]["id"], "recipe_minimax_h3_r2v_v2")
        self.assertIsNone(batch["model"])
        with self.assertRaisesRegex(ValueError, "反推模型"):
            self.core.action("owner", created["thread_id"],
                             {"action": "confirm_reverse", "request_id": "rev-1", "revision": batch["revision"]})

    def test_precheck_reports_target_workflow_media_prompt(self):
        created = self.handoff(prompt_slot={"node_id": "1", "field_name": "text"},
                               media_inputs=[{"node_id": "3", "field_name": "image", "path": str(self.image)}])
        thread_id = created["thread_id"]
        with patch("naiba.comfyui.client.reachable", return_value=None):
            report = self.core.precheck("owner", thread_id, {})
        self.assertFalse(report["ok"])
        by_key = {check["key"]: check for check in report["checks"]}
        self.assertFalse(by_key["target"]["ok"])
        self.assertTrue(by_key["workflow"]["ok"])
        self.assertTrue(by_key["media"]["ok"])
        self.assertTrue(by_key["prompt"]["ok"])
        with patch("naiba.comfyui.client.reachable", return_value={"version": "v1"}):
            report = self.core.precheck("owner", thread_id, {})
        self.assertTrue(report["ok"])
        # 媒体快照丢失 → 媒体检查失败。
        media_path = Path(self.core.detail("owner", thread_id)["batch"]["items"][0]["media"][0]["path"])
        media_path.unlink()
        report = self.core.precheck("owner", thread_id, {})
        by_key = {check["key"]: check for check in report["checks"]}
        self.assertFalse(by_key["media"]["ok"])

    def test_add_groups_and_media_patch_in_direct_mode(self):
        # 多组批量：交接只建第 1 组，其余组在批次里逐个「添加一组」补齐。
        created = self.handoff(prompt_slot={"node_id": "1", "field_name": "text"},
                               duration_slot={"node_id": "5", "field_name": "value"},
                               media_slots=[{"node_id": "3", "field_name": "image", "media": "image"}])
        tid = created["thread_id"]
        batch = self.core.detail("owner", tid)["batch"]
        self.assertEqual(len(batch["items"]), 1)
        revision, revision_hash = batch["revision"], batch["revision_hash"]
        updated = self.core.action("owner", tid, {
            "action": "edit", "request_id": "add-group-1", "revision": revision, "revision_hash": revision_hash,
            "add_items": [{"source_path": "added/group-2", "prompt": "第二组正文", "media": []}]})["batch"]
        self.assertEqual(len(updated["items"]), 2)
        self.assertEqual(updated["items"][1]["prompt"], "第二组正文")
        self.assertEqual(updated["status"], "review")
        # 直接模式换素材不清正文：媒体引用对不对由用户决定。
        item_id = updated["items"][0]["id"]
        updated = self.core.action("owner", tid, {
            "action": "edit", "request_id": "media-1", "revision": updated["revision"], "revision_hash": updated["revision_hash"],
            "items": {item_id: {"media": [{"role": "m3_image", "path": str(self.image)}]}}})["batch"]
        item = updated["items"][0]
        self.assertEqual([m["role"] for m in item["media"]], ["m3_image"])
        self.assertEqual(item["prompt"], "第二组正文" if item["id"] != updated["items"][0]["id"] else created["batch"]["items"][0]["prompt"])
        self.assertEqual(item["reverse"]["status"], "provided")
        # 编辑接口拒绝引用未知项目的补丁，添加组受 50 组上限约束。
        with self.assertRaisesRegex(ValueError, "50"):
            self.core.action("owner", tid, {
                "action": "edit", "request_id": "add-group-2", "revision": updated["revision"], "revision_hash": updated["revision_hash"],
                "add_items": [{"source_path": f"added/extra-{n}", "prompt": "x", "media": []} for n in range(60)]})

    def test_generate_authorize_probes_frozen_target(self):
        created = self.handoff(prompt_slot={"node_id": "1", "field_name": "text"})
        self.tid = created["thread_id"]
        batch = self.core.detail("owner", self.tid)["batch"]
        with patch.object(RecipeCoordinator, "_probe_target", side_effect=ValueError("生成目标不可达：x")):
            with self.assertRaises(ValueError) as caught:
                self.core.action("owner", self.tid, {"action": "confirm_generate", "request_id": "gen-1",
                                                     "revision": batch["revision"]})
        self.assertIn("生成目标不可达", str(caught.exception))
        after = self.core.detail("owner", self.tid)["batch"]
        self.assertEqual(after["status"], "draft")
        self.assertEqual(after["items"][0]["generate"]["attempts"], [])


if __name__ == "__main__":
    unittest.main()
