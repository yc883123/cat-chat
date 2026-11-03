"""Reproduce the fixed graphs offline from the pinned official installed package.

Run with the project venv and -I, passing the site-packages directory. No network,
ComfyUI server, custom workflow, downloaded code or model execution is involved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SOURCE_HASHES = {
    "i2v": "34ee39544808fd3b0dc8de9df082940d4d41c3771beb80d3988c1ea5531cec0d",
    "r2v": "afeea99e9fd5a1456df348e0573e289c1243f2f28546fc95c1c963f808d61ddb",
}
UI_ONLY = {"upload", "control_after_generate", "fixed"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def links_by_id(links):
    return {x["id"] if isinstance(x, dict) else x[0]: x for x in links}


def origin(link):
    return (link["origin_id"], link["origin_slot"]) if isinstance(link, dict) else (link[1], link[2])


def convert_nodes(nodes, links, *, prefix="", boundary=None):
    result = {}
    for node in nodes:
        if node["type"] in {"Note", "MarkdownNote", "GetImageSize", "ImageScaleToTotalPixels"}:
            continue
        if "-" in node["type"]:  # The one subgraph instance is flattened separately.
            continue
        values = {k: v for k, v in (node.get("widgets_values_named") or {}).items() if k not in UI_ONLY}
        for port in node.get("inputs", []):
            if port.get("link") is None:
                continue
            source, slot = origin(links[port["link"]])
            if source == -10:
                value = boundary[slot]
                if value is not None:
                    values[port["name"]] = value
                else:
                    values.pop(port["name"], None)
            else:
                values[port["name"]] = [prefix + str(source), slot]
        result[prefix + str(node["id"]) ] = {"class_type": node["type"], "inputs": values}
    return result


def add_r2v_references(graph):
    """Apply the deterministic v2 media extension to the pinned official graph."""
    image_ids = ["137", "139", *[f"recipe_ref_image_{i}" for i in range(3, 10)]]
    for index, node_id in enumerate(image_ids):
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": f"recipe_input_{index + 1}.png"}}
        graph["136"]["inputs"][f"ref_images.ref_image_{index}"] = [node_id, 0]
    for kind, cls, field, suffix in (("video", "LoadVideo", "file", "mp4"), ("audio", "LoadAudio", "audio", "wav")):
        for index in range(1, 4):
            node_id = f"recipe_ref_{kind}_{index}"
            graph[node_id] = {"class_type": cls, "inputs": {field: f"recipe_input_{index}.{suffix}"}}
            source_id = node_id
            if kind == "video":
                source_id = node_id + "_components"
                graph[source_id] = {"class_type": "GetVideoComponents", "inputs": {"video": [node_id, 0]}}
            graph["136"]["inputs"][f"ref_{kind}s.ref_{kind}_{index - 1}"] = [source_id, 0]
    return [
        "Version 2: up to nine optional images, three optional core LoadVideo references and three optional LoadAudio references. Zero references are structurally supported; no live generation claim.",
        "Execution must remove absent optional loaders AND corresponding H3 reference connections, including unused GetVideoComponents; never sparse labels.",
        "Preserved scheduler124.model->127 and guider126.model->141 exactly (not normalized).",
        "LoadVideo VIDEO output passes through GetVideoComponents.images IMAGE output before H3 ref_videos; video soundtrack is not implicitly bound.",
    ]


def convert(source: Path, target: Path):
    metadata_path = source / "comfyui_workflow_templates_json-0.1.126.dist-info/METADATA"
    metadata = metadata_path.read_text(encoding="utf-8")
    if "Version: 0.1.126\n" not in metadata or "License-Expression: MIT\n" not in metadata:
        raise ValueError("Unexpected official JSON package metadata")
    manifest_path = source / "comfyui_workflow_templates_core/manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("id") in {"video_minimax_h3_i2v", "video_minimax_h3_r2v"}:
                entries.append(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(manifest)
    license_path = source / "comfyui_workflow_templates-0.11.100.dist-info/licenses/LICENSE"
    license_bytes = license_path.read_bytes()
    if b"MIT License" not in license_bytes or b"Copyright (c) 2023-present Comfy Org" not in license_bytes:
        raise ValueError("Unexpected official license")
    target.mkdir(parents=True, exist_ok=True)
    (target / "LICENSE").write_bytes(license_bytes)
    provenance = {"package": "comfyui-workflow-templates-json", "version": "0.1.126",
                  "license": "MIT", "copyright": "Copyright (c) 2023-present Comfy Org",
                  "license_source": license_path.relative_to(source).as_posix(),
                  "license_sha256": digest(license_bytes), "metadata_sha256": digest(metadata_path.read_bytes()),
                  "manifest_source": manifest_path.relative_to(source).as_posix(),
                  "manifest_sha256": digest(manifest_bytes), "templates": {}}
    for mode, expected in SOURCE_HASHES.items():
        name = "video_minimax_h3_" + mode
        raw = (source / "comfyui_workflow_templates_json/templates" / (name + ".json")).read_bytes()
        if digest(raw) != expected:
            raise ValueError("Official source hash mismatch: " + name)
        entry = next(e for e in entries if e["id"] == name)
        if not any(a.get("filename") == name + ".json" and a.get("sha256") == expected for a in entry["assets"]):
            raise ValueError("Official manifest mismatch: " + name)
        ui = json.loads(raw)
        graph = convert_nodes(ui["nodes"], links_by_id(ui["links"]))
        modifications = ["Removed UI notes, layout, properties and upload/seed-control widgets.",
                         "Replaced demonstration prompts and media filenames with empty prompt and recipe_input_N.png placeholders.",
                         "Replaced SaveVideo filename_prefix with video/recipe_h3_" + mode + "."]
        if mode == "i2v":
            parent = next(n for n in ui["nodes"] if n["id"] == 105)
            sub = next(s for s in ui["definitions"]["subgraphs"] if s["id"] == parent["type"])
            root_links = links_by_id(ui["links"])
            overrides = parent["widgets_values_named"]
            connected = {x["name"]: [str(origin(root_links[x["link"]])[0]), origin(root_links[x["link"]])[1]]
                         for x in parent["inputs"] if x.get("link") is not None}
            boundary = [connected.get(x["name"], overrides.get(x["name"])) for x in sub["inputs"]]
            graph.update(convert_nodes(sub["nodes"], links_by_id(sub["links"]), prefix="105_", boundary=boundary))
            graph["92"]["inputs"]["video"] = ["105_91", 0]
            graph["105_104"]["inputs"]["prompt"] = ""
            graph["114"]["inputs"]["image"] = "recipe_input_1.png"
            modifications.extend([
                "Flattened subgraph105 as 105_<inner_id>; resolved all parent promoted overrides and live root connections.",
                "Official link218 connects114 directly to105.first_frame;119.image is unconnected and119->120 is output-disconnected. Omitted119/120; no invented scale edge.",
                "Parent duration5, noise_seed757358688076805, turbo false and turbo_steps8 override inner defaults."])
        else:
            graph["138"]["inputs"]["value"] = ""
            modifications.extend(add_r2v_references(graph))
        graph["92"]["inputs"]["filename_prefix"] = "video/recipe_h3_" + mode
        graph_bytes = (json.dumps(graph, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        (target / ("h3_" + mode + ".api.json")).write_bytes(graph_bytes)
        provenance["templates"][mode] = {
            "source": "comfyui_workflow_templates_json/templates/" + name + ".json",
            "source_sha256": expected, "source_manifest_entry": entry, "ui_workflow_id": ui["id"],
            "workflow_fingerprint": digest(json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")),
            "modifications": modifications,
            "duration": {"requested_seconds": [4, 15], "fps": 24,
                         "expression": "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17",
                         "note": "Official frame snapping may exceed requested duration; 4s=107frames/24,15s=362frames/24. No promise of exact container duration."}}
    repository = Path(__file__).resolve().parents[3]
    for mode, record in provenance["templates"].items():
        references = ["skills/h3-prompt-writing/references/base-en.txt"]
        if mode == "r2v":
            references += ["skills/h3-prompt-writing/references/ref-en.txt", "skills/comfyui-shortdramav2/SKILL.md"]
        # Repository references use Git's LF representation; user checkouts may
        # contain CRLF. Official source/license and frozen templates stay exact.
        record["prompt_reference_sha256"] = {
            name: digest((repository / name).read_bytes().replace(b"\r\n", b"\n"))
            for name in references
        }
        record["prompt_reference_hash_format"] = "UTF-8 bytes with CRLF normalized to LF"
        record["prompt_template_sha256"] = digest((target / ("h3_" + mode + ".prompt.txt")).read_bytes())
    (target / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site_packages", type=Path)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    convert(args.site_packages, args.out)
