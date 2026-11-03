"""Offline fixed-catalog gates; never contact a model or ComfyUI server."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from naiba.super_agent.recipe_catalog import (builtins, get_recipe, prepare_workflow,
                                              validate_output, workflow_fingerprint)

ASSETS = Path(__file__).resolve().parents[1] / "naiba/super_agent/recipe_assets"
I2V = "minimax_h3_i2v"
R2V = "minimax_h3_r2v"
OPENING = ("For the target video, at 0.00 seconds into the target video, "
           "<Picture 1> (from [Shot 1]) is fully referenced.")


def i2v():
    return (OPENING + "\n\nintegrated_multimodal_description: [Shot 1] Live-action. The subject in "
            '<Picture 1> turns toward the doorway and (S1) says, <d>[Chinese] 你好，世界！</d> '
            'A sign reads "欢迎光临". The camera holds a static shot.\n\n'
            "overall_soundscape: Footsteps cross the room.\n\nnon_diegetic_music: N/A")


def r2v(count=2):
    definitions = "\n".join(f"<Subject {i}> is the visible subject in <Picture {i}>." for i in range(1, count + 1))
    retained = "\n".join(f"<Subject {i}>: fully_preserved - identity and clothing are retained." for i in range(1, count + 1))
    return (f"subject_definitions:\n{definitions}\n\nsummary: [reference generation] The subjects meet.\n\n"
            f"retention_analysis:\n{retained}\n\ndetailed_description: The video is live-action. "
            "[Shot 1] The camera holds still as <Subject 1> (S1) says, <d>[Chinese] 你好！</d> "
            "The subject moves toward the doorway.\n\noverall_soundscape: Quiet room tone.\n\n"
            "non_diegetic_music: N/A")


class RecipeCatalogTests(unittest.TestCase):
    def validate(self, text, *, recipe=I2V, count=1, duration=5):
        return validate_output(recipe, text, image_count=count, duration_seconds=duration)

    def reject(self, text, **kwargs):
        result = self.validate(text, **kwargs)
        self.assertFalse(result["valid"], result)
        self.assertEqual(result["raw"], text)
        self.assertEqual(result["text"], "")
        self.assertTrue(result["errors"])
        return result

    def test_catalog_shape_and_deepcopy(self):
        items = builtins()
        self.assertEqual([r["id"] for r in items], [I2V, R2V])
        for recipe in items:
            self.assertEqual(set(recipe), {"id", "name", "description", "mode", "workflow",
                                          "workflow_fingerprint", "provenance", "prompt_slot",
                                          "media_slots", "duration_slot", "template", "version", "input_mode"})
            graph = recipe["workflow"]
            self.assertEqual(workflow_fingerprint(graph), recipe["workflow_fingerprint"])
            for slot in [recipe["prompt_slot"], recipe["duration_slot"], *recipe["media_slots"]]:
                self.assertIn(slot["field_name"], graph[slot["node_id"]]["inputs"])
            self.assertEqual(graph[recipe["prompt_slot"]["node_id"]]["inputs"][recipe["prompt_slot"]["field_name"]], "")
            for slot in recipe["media_slots"]:
                self.assertEqual(slot["scope"], "item")
                self.assertIsInstance(slot["required"], bool)
            recipe["workflow"]["92"]["inputs"]["filename_prefix"] = "mutated"
            recipe["provenance"]["modifications"].clear()
        self.assertNotIn("mutated", json.dumps(builtins()))
        self.assertTrue(get_recipe(I2V)["provenance"]["modifications"])
        with self.assertRaises(ValueError):
            get_recipe("unknown")

    def test_fingerprint_canonical_and_sensitive(self):
        graph = get_recipe(I2V)["workflow"]
        self.assertEqual(workflow_fingerprint(graph), workflow_fingerprint(dict(reversed(list(graph.items())))))
        other = deepcopy(graph)
        other["105_111"]["inputs"]["value"] = 6
        self.assertNotEqual(workflow_fingerprint(graph), workflow_fingerprint(other))
        with self.assertRaises(ValueError):
            workflow_fingerprint({"nan": float("nan")})
        with self.assertRaises(TypeError):
            workflow_fingerprint([])

    def test_official_provenance_and_mit_notice(self):
        license_bytes = (ASSETS / "LICENSE").read_bytes()
        for recipe, source_hash in zip(builtins(), [
                "34ee39544808fd3b0dc8de9df082940d4d41c3771beb80d3988c1ea5531cec0d",
                "afeea99e9fd5a1456df348e0573e289c1243f2f28546fc95c1c963f808d61ddb"]):
            source = recipe["provenance"]
            self.assertEqual(source["version"], "0.1.126")
            self.assertEqual(source["license"], "MIT")
            self.assertEqual(source["source_sha256"], source_hash)
            self.assertEqual(source["license_sha256"], hashlib.sha256(license_bytes).hexdigest())
            self.assertIn({"filename": Path(source["source"]).name, "sha256": source_hash}, source["source_manifest_entry"]["assets"])
            self.assertIn("Comfy Org", license_bytes.decode())
            self.assertTrue(source["prompt_references"])
            prompt = ASSETS / ("h3_" + recipe["mode"] + ".prompt.txt")
            self.assertEqual(source["prompt_template_sha256"], hashlib.sha256(prompt.read_bytes()).hexdigest())
            self.assertEqual(source["prompt_reference_hash_format"], "UTF-8 bytes with CRLF normalized to LF")
            for name, expected in source["prompt_reference_sha256"].items():
                reference_bytes = (ASSETS.parents[2] / name).read_bytes()
                self.assertEqual(hashlib.sha256(reference_bytes.replace(b"\r\n", b"\n")).hexdigest(), expected)
            self.assertNotIn(":/", json.dumps(source))
            self.assertIn("not server/model execution", source["verification_scope"])

    def test_all_graph_links_resolve_and_no_demo_or_ui_widgets(self):
        for recipe in builtins():
            graph = recipe["workflow"]
            self.assertNotIn("nodes", graph)
            for node in graph.values():
                self.assertEqual(set(node), {"class_type", "inputs"})
                self.assertNotIn(node["class_type"], ["MarkdownNote", "Note", "GetImageSize"])
                self.assertFalse({"upload", "fixed", "control_after_generate"} & set(node["inputs"]))
                for value in node["inputs"].values():
                    if isinstance(value, list):
                        self.assertEqual(len(value), 2)
                        self.assertIn(value[0], graph)
                        self.assertIsInstance(value[1], int)
            serialized = json.dumps(graph).lower()
            for demo in ["gaming_mouse", "superboy", "mecha_dragon", "vaporwave", "maker", "audio 1", "teaccel", "editorial"]:
                self.assertNotIn(demo, serialized)

    def test_i2v_flatten_overrides_not_inner_stale_values(self):
        recipe = get_recipe(I2V)
        graph = recipe["workflow"]
        self.assertEqual(graph["92"]["inputs"]["video"], ["105_91", 0])
        self.assertEqual(graph["105_104"]["inputs"]["first_frame"], ["114", 0])
        self.assertNotIn("last_frame", graph["105_104"]["inputs"])
        self.assertEqual(graph["105_104"]["inputs"]["width"], ["115", 0])
        self.assertEqual(graph["105_104"]["inputs"]["height"], ["115", 1])
        self.assertEqual(graph["105_104"]["inputs"]["length"], ["105_107", 1])
        self.assertEqual(graph["105_107"]["inputs"]["values.a"], ["105_111", 0])
        self.assertEqual(graph["105_111"]["inputs"]["value"], 5)
        self.assertEqual(graph["105_15"]["inputs"]["noise_seed"], 757358688076805)
        self.assertEqual(graph["105_125"]["inputs"]["value"], 8)
        self.assertIs(graph["105_126"]["inputs"]["value"], False)
        self.assertNotIn("105", graph)
        self.assertNotIn("119", graph)
        self.assertIn("119.image is unconnected", " ".join(recipe["provenance"]["modifications"]))

    def test_r2v_preserves_scheduler_guider_divergence_and_prompt_slot(self):
        graph = get_recipe(R2V)["workflow"]
        self.assertEqual(graph["136"]["inputs"]["prompt"], ["138", 0])
        self.assertEqual(graph["124"]["inputs"]["model"], ["127", 0])
        self.assertEqual(graph["126"]["inputs"]["model"], ["141", 0])
        self.assertEqual(graph["136"]["inputs"]["ref_images.ref_image_0"], ["137", 0])
        self.assertEqual(graph["136"]["inputs"]["ref_images.ref_image_1"], ["139", 0])
        self.assertEqual(graph["132"]["inputs"]["value"], 5)

    def test_count_variants_remove_both_loaders_and_connections(self):
        fingerprints = set()
        for count in range(0, 10):
            graph = prepare_workflow(R2V, image_count=count)
            keys = {key for key in graph["136"]["inputs"] if key.startswith("ref_images.")}
            self.assertEqual(keys, {f"ref_images.ref_image_{i}" for i in range(count)})
            self.assertEqual(sum(n["class_type"] == "LoadImage" for n in graph.values()), count)
            fingerprints.add(workflow_fingerprint(graph))
            self.assertEqual(graph, prepare_workflow(R2V, image_count=count))
        self.assertEqual(len(fingerprints), 10)
        self.assertEqual(workflow_fingerprint(prepare_workflow(R2V, image_count=9, video_count=3, audio_count=3)), get_recipe(R2V)["workflow_fingerprint"])
        self.assertEqual(prepare_workflow(I2V, image_count=1), get_recipe(I2V)["workflow"])
        for count in [10, -1, True, "2", 2.0, None]:
            with self.subTest(count=count), self.assertRaises(ValueError):
                prepare_workflow(R2V, image_count=count)
        with self.assertRaises(ValueError):
            prepare_workflow(I2V, image_count=2)

    def test_offline_converter_reproduces_v2_extension_and_provenance(self):
        from naiba.super_agent.recipe_assets.convert_official import add_r2v_references
        expected = get_recipe(R2V)
        graph = deepcopy(expected["workflow"])
        # Strip every v2 media extension, retaining only official image 0/1.
        for slot in expected["media_slots"][2:]:
            node_id = slot["node_id"]
            graph.pop(node_id, None)
            graph.pop(node_id + "_components", None)
        graph["136"]["inputs"] = {k: v for k, v in graph["136"]["inputs"].items()
                                  if not k.startswith(("ref_images.", "ref_videos.", "ref_audios.", "ref_video_audios."))}
        graph["136"]["inputs"].update({"ref_images.ref_image_0": ["137", 0], "ref_images.ref_image_1": ["139", 0]})
        changes = add_r2v_references(graph)
        self.assertEqual(graph, expected["workflow"])
        self.assertEqual(workflow_fingerprint(graph), expected["workflow_fingerprint"])
        self.assertEqual(changes, expected["provenance"]["modifications"][3:])
        self.assertFalse(any(k.startswith("ref_video_audios.") for k in graph["136"]["inputs"]))
        first = deepcopy(graph)
        self.assertEqual(add_r2v_references(graph), changes)
        self.assertEqual(graph, first)

    def test_frozen_v1_graph_derives_without_current_catalog(self):
        old = get_recipe(R2V)
        old["workflow"] = prepare_workflow(R2V, image_count=6)
        old["media_slots"] = old["media_slots"][:6]
        old.pop("version")
        old["template"]["result_rule"] = "h3_ref2va_single_v1"
        self.assertEqual(workflow_fingerprint(old["workflow"]), "27fbd151db9fdf404cec844b21e21ca7fa684976ffbb94c8f771f8c642fc151a")
        old["workflow"]["132"]["inputs"]["value"] = 7
        projected = prepare_workflow(R2V, image_count=2, recipe_snapshot=old)
        self.assertEqual(projected["132"]["inputs"]["value"], 7)
        self.assertEqual(sum(n["class_type"] == "LoadImage" for n in projected.values()), 2)
        with self.assertRaises(ValueError):
            prepare_workflow(R2V, image_count=0, recipe_snapshot=old)

    def test_video_component_types_and_optional_zero_projection(self):
        graph = prepare_workflow(R2V, image_count=0, video_count=1, audio_count=1)
        self.assertEqual(graph["136"]["inputs"]["ref_videos.ref_video_0"], ["recipe_ref_video_1_components", 0])
        self.assertEqual(graph["recipe_ref_video_1_components"]["inputs"]["video"], ["recipe_ref_video_1", 0])
        self.assertEqual(graph["136"]["inputs"]["ref_audios.ref_audio_0"], ["recipe_ref_audio_1", 0])
        zero = prepare_workflow(R2V, image_count=0)
        self.assertFalse(any(n["class_type"] in {"LoadImage", "LoadAudio", "LoadVideo", "GetVideoComponents"} for n in zero.values()))
        self.assertFalse(any(k.startswith(("ref_images.", "ref_videos.", "ref_audios.")) for k in zero["136"]["inputs"]))

    def test_prompt_templates_are_self_contained_one_pass(self):
        for recipe in builtins():
            body = recipe["template"]["body"]
            for name in ("requirements", "duration_seconds", "image_count"):
                self.assertIn("{{" + name + "}}", body)
            self.assertIn("one pass", body)
            self.assertIn("English", body)
            self.assertIn("original language", body)
            self.assertIn("17k+5", body)
            self.assertNotIn("D:/", body)
        self.assertEqual(get_recipe(I2V)["template"]["result_rule"], "h3_i2v_single_v1")
        self.assertEqual(get_recipe(R2V)["template"]["result_rule"], "h3_ref2va_single_v2")

    def test_valid_outputs_keep_bytes_and_original_dialogue(self):
        text = " \n" + i2v() + "\n "
        result = self.validate(text)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["text"], text)
        for count in range(1, 10):
            result = self.validate(r2v(count), recipe=R2V, count=count)
            self.assertTrue(result["valid"], result)

    def test_empty_nontext_and_oversize_preserve_raw(self):
        for value in [None, {}, [i2v()], "", " \n", "x" * 50001]:
            with self.subTest(value=str(value)[:30]):
                self.reject(value)

    def test_strict_sections_duplicates_order_empty_and_fences(self):
        text = i2v()
        samples = ["```text\n" + text + "\n```", "~~~\n" + text + "\n~~~", text + "\n\n" + text,
                   "Here is the prompt:\n" + text, text.replace(OPENING, ""),
                   text.replace("overall_soundscape: Footsteps cross the room.", "overall_soundscape:"),
                   text.replace("overall_soundscape:", "soundscape:"),
                   text.replace("overall_soundscape: Footsteps cross the room.\n\nnon_diegetic_music: N/A",
                                "non_diegetic_music: N/A\n\noverall_soundscape: Footsteps."),
                   text + "\nexplanation: This is the best option.", text + "\n# Result 2\nOther option.",
                   text + "\n\nThis was the first option. Another prompt follows.",
                   text + "\nOption 2\nAnother independent prompt.",
                   text + "\nThis is why I chose the prompt.",
                   json.dumps({"results": [text, text]})]
        for sample in samples:
            with self.subTest(sample=sample[-80:]):
                self.reject(sample)
        self.reject(r2v().replace("summary:", "retention_analysis:"), recipe=R2V, count=2)

    def test_image_reference_count_sparse_and_unsupported_types(self):
        for label in ["<Picture 2>", "<Picture 0>", "<Picture 01>", "<picture 1>", "<Picture  1>",
                      "<Audio 1>", "<Video 1>", "<Subject 1>"]:
            self.reject(i2v().replace("<Picture 1>", label))
        self.reject(r2v(2).replace("<Picture 2>", "<Picture 3>"), recipe=R2V, count=2)
        self.reject(r2v(1), recipe=R2V, count=2)
        self.reject(r2v(2) + " <Audio 1>", recipe=R2V, count=2)
        self.reject(r2v(2) + " <Subject 8>", recipe=R2V, count=2)
        self.reject(r2v(2).replace("<Subject 2> is", "<Subject 1> is"), recipe=R2V, count=2)
        self.reject(r2v(2).replace("[reference generation]", "[audio reuse]"), recipe=R2V, count=2)
        self.reject(r2v(2).replace("fully_preserved", "fully_copy"), recipe=R2V, count=2)
        self.reject(r2v(2).replace("<Subject 2>: fully_preserved - identity and clothing are retained.", ""), recipe=R2V, count=2)
        self.reject(r2v(2).replace("The video is live-action. ", ""), recipe=R2V, count=2)
        self.reject(i2v().replace("Footsteps cross the room.", "<d>[Chinese] 你好</d>"))

    def test_duration_and_image_count_parameter_gates(self):
        for duration in [3.9, 15.1, float("inf"), float("nan"), True, "5", None]:
            self.reject(i2v(), duration=duration)
        for count in [0, 2, 7, True, 1.0, "1", None]:
            self.reject(i2v(), count=count)
        for duration in [4, 15, 4.5]:
            self.assertTrue(self.validate(i2v(), duration=duration)["valid"])
        unknown = self.validate(i2v(), recipe="unknown")
        self.assertFalse(unknown["valid"])
        self.assertEqual(unknown["raw"], i2v())

    def test_timeline_bounds_cut_order_and_multiple_results(self):
        valid = i2v().replace("The camera holds a static shot.", "[Shot 2] At 00:03.000, the camera cuts to the doorway.")
        self.assertTrue(self.validate(valid)["valid"])
        for sample in [valid.replace("00:03.000", "00:05.000"), valid.replace("00:03.000", "00:06.000"),
                       valid.replace("[Shot 2]", "[Shot 3]"), valid.replace("At 00:03.000,", "At three seconds,"),
                       valid.replace("[Shot 2]", "[Shot 1]"),
                       valid.replace("00:03.000", "00:00.000"),
                       valid.replace("[Shot 2] At 00:03.000,", "[Shot 2] At 00:03.000, [Shot 3] At 00:02.000,"),
                       i2v().replace("The camera holds a static shot.", "The action lasts 9 seconds."),
                       i2v().replace("[Shot 1]", "[Shot 1] At 00:00.000,")]:
            with self.subTest(sample=sample):
                self.reject(sample)

    def test_language_and_dialogue_markup(self):
        self.reject(i2v().replace("Live-action.", "镜头画面。"))
        self.reject(i2v().replace("[Chinese]", ""))
        self.reject(i2v().replace("</d>", ""))
        self.reject(i2v().replace("[Chinese] 你好，世界！", "[Chinese] "))
        self.assertTrue(self.validate(i2v().replace("Live-action", "Cinéma-style"))["valid"])

    def test_official_frame_grid_is_preserved_not_exact_seconds(self):
        for recipe in builtins():
            graph = recipe["workflow"]
            node = "105_107" if recipe["mode"] == "i2v" else "131"
            self.assertEqual(graph[node]["inputs"]["expression"], recipe["provenance"]["duration"]["expression"])
            for seconds, frames in [(4, 107), (15, 362)]:
                value = max(5, round(seconds * 24))
                self.assertEqual(value + (5 - value % 17) % 17, frames)
                self.assertGreater(frames / 24, seconds)

    def test_converter_rejects_wrong_source_without_contacting_server(self):
        spec = importlib.util.spec_from_file_location("recipe_converter_test", ASSETS / "convert_official.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            with self.assertRaises(FileNotFoundError):
                module.convert(source, Path(tmp) / "out")


if __name__ == "__main__":
    unittest.main()
