"""Offline, authoritative-schema configuration save-as gates."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from naiba.comfyui.params import scan_params
from naiba.comfyui.schema import NodeSchemaProvider, parse_input_decl
from naiba.comfyui.service import ComfyUIService
from naiba.comfyui.store import ComfyUIStore


class SafeSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ComfyUIService.__new__(ComfyUIService)
        self.service.app = SimpleNamespace(config=SimpleNamespace(data={}))
        self.service.store = ComfyUIStore(Path(self.temp.name))
        self.service.jobs = mock.Mock()
        self.graph = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "old.safetensors"}},
                      "2": {"class_type": "LoraLoader", "inputs": {"lora_name": "old.safetensors", "strength_model": 0.5}},
                      "3": {"class_type": "KSampler", "inputs": {"seed": 17, "steps": 20, "sampler_name": "euler", "enabled": True,
                                                                    "code": "print(1)", "url": "http://old", "positive": ["1", 0]}}}
        self.source = {"id": "cw-source", "name": "source", "prompt": self.graph, "params": scan_params(self.graph),
                       "visible_params": [{"node_id": "3", "field_name": "steps"}], "param_order": [{"node_id": "3", "field_name": "steps"}]}
        self.service.store.save_workflow(self.source)
        self.body = {"name": "new", "workflow_fingerprint": ComfyUIStore.payload_hash(self.graph), "overrides": []}
        self.schemas = {
            "CheckpointLoaderSimple": {"ckpt_name": {"kind": "combo", "candidates": ["old.safetensors", "new.safetensors"]}},
            "LoraLoader": {"lora_name": {"kind": "combo", "candidates": ["old.safetensors", "new.safetensors"]},
                           "strength_model": {"kind": "number", "number_type": "FLOAT", "min": -2, "max": 2}},
            "KSampler": {"seed": {"kind": "number", "number_type": "INT", "min": 0, "max": 2**64 - 1},
                         "steps": {"kind": "number", "number_type": "INT", "min": 1, "max": 100},
                         "sampler_name": {"kind": "combo", "candidates": ["euler", "dpmpp_2m"]},
                         "enabled": {"kind": "bool"}, "code": {"kind": "text"}, "url": {"kind": "text"}}}
        self.patch = mock.patch.object(NodeSchemaProvider, "node_inputs", side_effect=lambda cls: self.schemas.get(cls))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.network = mock.patch("naiba.comfyui.client.object_info_node", side_effect=AssertionError("live calls forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def save(self, *overrides):
        body = copy.deepcopy(self.body)
        body["overrides"] = [{"node_id": node, "field_name": field, "value": value} for node, field, value in overrides]
        return self.service.api_workflow_save_as("cw-source", body)

    def test_valid_model_lora_sampler_bool_number_and_exact_large_seed(self):
        result, status = self.save(("1", "ckpt_name", "new.safetensors"), ("2", "lora_name", "new.safetensors"),
                                   ("3", "sampler_name", "dpmpp_2m"), ("2", "strength_model", -1.25),
                                   ("3", "enabled", False), ("3", "seed", 2**64 - 1))
        self.assertEqual(status, 201, result)
        saved = result["workflow"]
        self.assertNotEqual(saved["id"], "cw-source")
        self.assertEqual(saved["prompt"]["3"]["inputs"]["seed"], 2**64 - 1)
        self.assertEqual(saved["workflow_fingerprint"], ComfyUIStore.payload_hash(saved["prompt"]))
        self.assertEqual(saved["visible_params"], self.source["visible_params"])
        self.assertEqual(saved["param_order"], self.source["param_order"])
        self.assertEqual(self.service.store.get_workflow("cw-source"), self.source)
        self.service.jobs.assert_not_called()

    def test_untouched_seeds_never_randomized_and_empty_save_forks(self):
        self.graph["3"]["inputs"]["seed"] = -1
        self.source["prompt"] = self.graph
        self.service.store.save_workflow(self.source)
        self.body["workflow_fingerprint"] = ComfyUIStore.payload_hash(self.graph)
        with mock.patch("naiba.comfyui.client.random_seed", side_effect=AssertionError("must not randomize")):
            result, status = self.save()
        self.assertEqual(status, 201)
        self.assertEqual(result["workflow"]["prompt"]["3"]["inputs"]["seed"], -1)

    def test_invalid_values_fail_without_writes(self):
        cases = [("1", "ckpt_name", "missing.safetensors"), ("2", "lora_name", "http://evil"),
                 ("3", "sampler_name", "other"), ("3", "code", "exec('x')"), ("3", "url", "http://evil"),
                 ("3", "steps", "30"), ("3", "steps", 2.5), ("3", "steps", True), ("3", "steps", 101),
                 ("3", "steps", float("nan")), ("3", "steps", float("inf")), ("3", "seed", -1),
                 ("3", "seed", 3.0), ("3", "enabled", "false"), ("3", "enabled", 0), ("3", "positive", "link"),
                 ("3", "unknown", 1)]
        for case in cases:
            with self.subTest(case=case):
                result, status = self.save(case)
                self.assertEqual(status, 400, result)
                self.assertEqual(len(self.service.store.list_workflows()), 1)

    def test_schema_missing_unknown_or_unbounded_is_unavailable_not_cached_fallback(self):
        for declaration in (None, {"kind": "unknown"}, {"kind": "number", "number_type": "INT"},
                            {"kind": "number", "number_type": "INT", "min": 0, "max": float("inf")}):
            with self.subTest(declaration=declaration):
                self.schemas["KSampler"] = {"steps": declaration}
                result, status = self.save(("3", "steps", 30))
                self.assertEqual(status, 503, result)
        self.assertEqual(len(self.service.store.list_workflows()), 1)

    def test_stale_missing_unknown_keys_duplicate_and_extra_override_keys(self):
        stale = {**self.body, "workflow_fingerprint": "0" * 64}
        self.assertEqual(self.service.api_workflow_save_as("cw-source", stale)[1], 409)
        self.assertEqual(self.service.api_workflow_save_as("cw-missing", self.body)[1], 404)
        for key in ("workflow", "target", "credentials", "prompt_slot"):
            self.assertEqual(self.service.api_workflow_save_as("cw-source", {**self.body, key: {}})[1], 400)
        self.assertEqual(self.save(("3", "steps", 20), ("3", "steps", 30))[1], 400)
        body = {**self.body, "overrides": [{"node_id": "3", "field_name": "steps", "value": 30, "code": "x"}]}
        self.assertEqual(self.service.api_workflow_save_as("cw-source", body)[1], 400)

    def test_params_endpoint_still_only_saves_display_order(self):
        result, status = self.service.api_workflow_params_set("cw-source", {
            "visible": [{"node_id": "1", "field_name": "ckpt_name"}],
            "order": [{"node_id": "1", "field_name": "ckpt_name"}], "overrides": [{"value": "not saved"}]})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.service.store.get_workflow("cw-source")["prompt"], self.graph)

    def test_atomic_commit_refuses_source_deleted_after_validation(self):
        original_commit = self.service.store.save_workflow_as
        def concurrent_delete(source_id, fingerprint, entry):
            self.service.store.delete_workflow(source_id)
            return original_commit(source_id, fingerprint, entry)
        with mock.patch.object(self.service.store, "save_workflow_as", side_effect=concurrent_delete):
            result, status = self.save(("3", "steps", 30))
        self.assertEqual(status, 409, result)
        self.assertEqual(self.service.store.list_workflows(), [])

    def test_schema_parser_retains_numeric_contract_and_rejects_partial_enums(self):
        declaration = parse_input_decl(["INT", {"min": 0, "max": 2**64 - 1}])
        self.assertEqual(declaration["number_type"], "INT")
        self.assertEqual(declaration["max"], 2**64 - 1)
        self.assertIsNone(parse_input_decl(["COMBO", {"options": ["safe", 123]}]))


if __name__ == "__main__":
    unittest.main()
