"""Offline fixed recipes derived from pinned, MIT-licensed official H3 UI graphs.

The assets are not execution approval. Native workflow validation, media upload,
explicit confirmation and the existing task runners remain the caller's job.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata
from typing import Any

_ASSETS = Path(__file__).resolve().with_name("recipe_assets")
_I2V_FIELDS = ("integrated_multimodal_description", "overall_soundscape", "non_diegetic_music")
_REF_FIELDS = ("subject_definitions", "summary", "retention_analysis", "detailed_description",
               "overall_soundscape", "non_diegetic_music")
_I2V_INSTRUCTION = ("For the target video, at 0.00 seconds into the target video, "
                    "<Picture 1> (from [Shot 1]) is fully referenced.")
_REF_LOADERS = ("137", "139", "recipe_ref_image_3", "recipe_ref_image_4",
                "recipe_ref_image_5", "recipe_ref_image_6", "recipe_ref_image_7",
                "recipe_ref_image_8", "recipe_ref_image_9")
_MAX_OUTPUT_CHARS = 50000


def workflow_fingerprint(graph: dict[str, Any]) -> str:
    """SHA-256 of the entire API graph, canonical UTF-8 JSON (no NaN/default=str)."""
    if not isinstance(graph, dict):
        raise TypeError("workflow must be an API graph object")
    return hashlib.sha256(json.dumps(graph, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _load_catalog() -> list[dict[str, Any]]:
    source = json.loads((_ASSETS / "provenance.json").read_text(encoding="utf-8"))
    recipes = []
    for mode in ("i2v", "r2v"):
        graph = json.loads((_ASSETS / f"h3_{mode}.api.json").read_text(encoding="utf-8"))
        provenance = {k: v for k, v in source.items() if k != "templates"}
        provenance.update(source["templates"][mode])
        fingerprint = workflow_fingerprint(graph)
        if fingerprint != provenance["workflow_fingerprint"]:
            raise ValueError("Fixed recipe asset fingerprint mismatch: " + mode)
        prompt_bytes = (_ASSETS / f"h3_{mode}.prompt.txt").read_bytes()
        if hashlib.sha256(prompt_bytes).hexdigest() != provenance["prompt_template_sha256"]:
            raise ValueError("Fixed recipe prompt asset fingerprint mismatch: " + mode)
        refs = ["skills/h3-prompt-writing/references/base-en.txt"]
        if mode == "r2v":
            refs.extend(["skills/h3-prompt-writing/references/ref-en.txt",
                         "skills/comfyui-shortdramav2/SKILL.md (tag mapping only; not scheduler)"])
        provenance["prompt_references"] = refs
        provenance["license_asset"] = "recipe_assets/LICENSE"
        provenance["verification_scope"] = "Offline source/manifest/graph verification; not server/model execution."
        slots = ([{"role": "first_frame", "label": "首帧 / <Picture 1>", "node_id": "114",
                   "field_name": "image", "media": "image", "scope": "item", "required": True}]
                 if mode == "i2v" else
                 [{"role": f"picture_{i + 1}", "label": f"参考图 {i + 1} / <Picture {i + 1}>",
                   "node_id": node, "field_name": "image", "media": "image", "scope": "item", "required": False}
                  for i, node in enumerate(_REF_LOADERS)] +
                 [{"role": f"{kind}_{i}", "label": f"<{label} {i}>",
                   "node_id": f"recipe_ref_{kind}_{i}", "field_name": field,
                   "media": kind, "scope": "item", "required": False}
                  for kind, label, field in (("video", "Video", "file"), ("audio", "Audio", "audio"))
                  for i in range(1, 4)])
        recipes.append({
            "id": f"minimax_h3_{mode}",
            "name": "MiniMax H3 首帧生视频（官方派生）" if mode == "i2v" else "MiniMax H3 多媒体参考生视频（官方派生）",
            "version": 2, "input_mode": "script",
            "description": ("每张图片生成一条视频。先用图片和剧本生成可编辑的提示词，确认后再制作视频；保留原语言台词。"
                            if mode == "i2v" else
                            "每组可选 0–9 张图片、0–3 段视频、0–3 段音频；音视频仅以用户描述交给提词模型，原件交给 H3。不自动拆分剧本。")
                           + "请求时长 4–15 秒，官方帧数取整可能使实际视频略长；需本机模型和节点，未做真实生成验收。",
            "mode": mode, "workflow": graph, "workflow_fingerprint": fingerprint,
            "provenance": provenance,
            "prompt_slot": {"node_id": "105_104" if mode == "i2v" else "138",
                            "field_name": "prompt" if mode == "i2v" else "value"},
            "media_slots": slots,
            "duration_slot": {"node_id": "105_111" if mode == "i2v" else "132", "field_name": "value"},
            "template": {"id": f"recipe_minimax_h3_{mode}_v1" if mode == "i2v" else "recipe_minimax_h3_r2v_v2",
                         "body": (_ASSETS / f"h3_{mode}.prompt.txt").read_text(encoding="utf-8"),
                         "result_rule": "h3_i2v_single_v1" if mode == "i2v" else "h3_ref2va_single_v2"},
        })
    return recipes


_CATALOG = _load_catalog()


def builtins() -> list[dict[str, Any]]:
    """Return independent mutable recipe snapshots; never leak module state."""
    return deepcopy(_CATALOG)


def get_recipe(recipe_id: str) -> dict[str, Any]:
    for recipe in _CATALOG:
        if recipe["id"] == recipe_id:
            return deepcopy(recipe)
    raise ValueError("Unknown fixed recipe: " + str(recipe_id))


def _valid_image_count(recipe: dict[str, Any], image_count: Any) -> bool:
    maximum = 9 if recipe.get("version", 1) >= 2 else 6
    minimum = 0 if recipe.get("mode") == "r2v" and recipe.get("version", 1) >= 2 else 1
    return (isinstance(image_count, int) and not isinstance(image_count, bool)
            and minimum <= image_count <= (maximum if recipe.get("mode") == "r2v" else 1))


def prepare_workflow(recipe_id: str, *, image_count: int, video_count: int = 0,
                     audio_count: int = 0, recipe_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Derive a deterministic per-count API graph, deleting unused reference tails.

    Count alone cannot verify an actual media list. The caller MUST reject sparse
    supplied roles (e.g. picture_1 + picture_3), never compact/relabel user assets.
    Freeze both the fixed recipe fingerprint and this variant's fingerprint.
    """
    recipe = deepcopy(recipe_snapshot) if recipe_snapshot is not None else get_recipe(recipe_id)
    if not _valid_image_count(recipe, image_count):
        raise ValueError("Invalid image count for the frozen recipe version")
    if any(isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 3 for n in (video_count, audio_count)):
        raise ValueError("Invalid video/audio count")
    graph = recipe["workflow"]
    if recipe["mode"] == "r2v":
        counts = {"image": image_count, "video": video_count, "audio": audio_count}
        seen = {k: 0 for k in counts}
        for slot in recipe["media_slots"]:
            kind = slot.get("media", "image")
            seen[kind] += 1
            if seen[kind] <= counts[kind]:
                continue
            node_id = slot["node_id"]
            removed = {node_id, node_id + "_components"}
            for removed_id in removed:
                graph.pop(removed_id, None)
            for node in graph.values():
                inputs = node.get("inputs") or {}
                for field, connection in list(inputs.items()):
                    if isinstance(connection, list) and len(connection) == 2 and str(connection[0]) in removed:
                        inputs.pop(field)
    return graph


def _english_errors(text: str) -> bool:
    # Dialogue/lyrics and visibly quoted scene text are the documented exceptions.
    prose = re.sub(r"<d>.*?</d>", "", text, flags=re.S)
    prose = re.sub(r'"[^"\n]*"', "", prose)
    return any(c.isalpha() and "LATIN" not in unicodedata.name(c, "") for c in prose)


def _timeline_errors(body: str, duration: float, errors: list[str]) -> None:
    # Only actual narrative shot declarations count; references in definitions do not.
    declarations = list(re.finditer(r"\[Shot\s+(\d+)\]", body))
    if not declarations or declarations[0].group(1) != "1":
        errors.append("Narrative must contain opening [Shot 1].")
        return
    if [int(m.group(1)) for m in declarations] != list(range(1, len(declarations) + 1)):
        errors.append("Narrative shots must be sequential, without duplicate results or gaps.")
    previous = 0.0
    for index, match in enumerate(declarations):
        tail = body[match.end():]
        timestamp = re.match(r"\s+At\s+(\d{2}):([0-5]\d)\.(\d{3})\b", tail)
        if index == 0:
            if re.match(r"\s+At\s+\d", tail):
                errors.append("Opening [Shot 1] must not have a cut timestamp.")
            continue
        if timestamp is None:
            errors.append("Later shots require At MM:SS.mmm timestamps.")
            continue
        seconds = int(timestamp[1]) * 60 + int(timestamp[2]) + int(timestamp[3]) / 1000
        if not previous < seconds < duration:
            errors.append("Cut timestamps must increase strictly inside requested duration.")
        previous = seconds
    for match in re.finditer(r"(?<![\d:])(\d+):([0-5]\d)(?:\.(\d{1,3}))?(?![\d:])", body):
        seconds = int(match[1]) * 60 + int(match[2]) + float("0." + (match[3] or "0"))
        if seconds > duration:
            errors.append("Timeline exceeds requested duration.")
    for match in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", body, flags=re.I):
        if float(match[1]) > duration:
            errors.append("Explicit seconds exceed requested duration.")


def validate_output(recipe_id: str, raw: Any, *, image_count: int,
                    duration_seconds: float, video_count: int = 0, audio_count: int = 0,
                    recipe_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fail closed on ambiguous output; retain raw verbatim, never repair/truncate.

    This deterministic format/timeline/reference gate is not semantic verification
    of visual fidelity, speech speed, dialogue completeness or generated runtime.
    """
    errors: list[str] = []
    result = {"valid": False, "raw": raw, "text": "", "errors": errors}
    try:
        recipe = deepcopy(recipe_snapshot) if recipe_snapshot is not None else get_recipe(recipe_id)
    except ValueError:
        errors.append("Unknown fixed recipe.")
        return result
    if not _valid_image_count(recipe, image_count):
        errors.append("Invalid image count for the frozen recipe version (v2 R2V: 0–9; v1: 1–6; I2V: one).")
    duration_valid = (isinstance(duration_seconds, (int, float)) and not isinstance(duration_seconds, bool)
                      and 4 <= duration_seconds <= 15 and math.isfinite(duration_seconds))
    if not duration_valid:
        errors.append("Requested duration must be a finite number from 4 to 15 seconds.")
    if not isinstance(raw, str) or not raw.strip():
        errors.append("Output must be one nonempty text result.")
        return result
    if len(raw) > _MAX_OUTPUT_CHARS:
        errors.append("Output exceeds the fixed 50000-character limit; no truncation performed.")
        return result
    text = raw.strip()
    if "```" in text or re.search(r"(?m)^\s*~~~", text):
        errors.append("Fenced output is not a single plain-text prompt.")
    if text.startswith(("{", "[\"", "[{")):
        errors.append("JSON/multiple results are not supported.")
    fields = _I2V_FIELDS if recipe["mode"] == "i2v" else _REF_FIELDS
    header = re.compile(r"(?m)^(" + "|".join(fields) + r"):([^\n]*)")
    matches = list(header.finditer(text))
    if tuple(m.group(1) for m in matches) != fields:
        errors.append("Sections must appear exactly once in the required order: " + ", ".join(fields))
        return result
    prefix = text[:matches[0].start()].strip()
    if prefix != (_I2V_INSTRUCTION if recipe["mode"] == "i2v" else ""):
        errors.append("Unexpected preamble or missing exact first-frame instruction.")
    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start() + len(match[1]) + 1:end].strip()
        sections[match[1]] = section
        if not section:
            errors.append("Empty section: " + match[1])
        if re.search(r"(?m)^\s*(?:#{1,6}\s|(?:[A-Za-z_]+):)", section):
            errors.append("Unexpected extra heading/field inside " + match[1])
        if match[1] in ("overall_soundscape", "non_diegetic_music"):
            if re.search(r"\n\s*\n", section):
                errors.append("Sound sections must each be one paragraph; extra result/prose is not accepted.")
            if section.startswith("N/A") and section != "N/A":
                errors.append("N/A must be the entire sound section, without trailing explanation/results.")
    if re.search(r"(?im)^\s*(?:(?:option|alternative|result|prompt|version)\s*[#:]?\s*\d+\b|"
                 r"(?:here (?:is|are)|explanation|analysis|reasoning|notes?|plan)\s*:)", text):
        errors.append("Additional result, explanation or planning markers are not accepted.")
    if _english_errors(text):
        errors.append("Descriptions must be English; original dialogue and quoted visible text are exempt.")
    labels = list(re.finditer(r"<\s*(Picture|Audio|Video|Subject)\b([^>]*)>", text, flags=re.I))
    picture_numbers = set()
    media_numbers = {"Audio": set(), "Video": set()}
    multimodal = recipe.get("version", 1) >= 2 and recipe["mode"] == "r2v"
    counts = {"Audio": audio_count, "Video": video_count}
    for kind, count in counts.items():
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= (3 if multimodal else 0):
            errors.append("Invalid " + kind + " reference count.")
    subjects = set()
    for label in labels:
        canonical = re.fullmatch(r"<(Picture|Audio|Video|Subject) ([1-9]\d*)>", label[0])
        if canonical is None:
            errors.append("Malformed or noncanonical reference label: " + label[0])
            continue
        kind, number = canonical[1], int(canonical[2])
        if kind in ("Audio", "Video"):
            media_numbers[kind].add(number)
            if not multimodal:
                errors.append("Unsupported reference type for this recipe: " + kind)
        elif kind == "Subject" and recipe["mode"] == "i2v":
            errors.append("Unsupported reference type for this recipe: " + kind)
        elif kind == "Picture":
            picture_numbers.add(number)
        elif kind == "Subject":
            subjects.add(number)
    if _valid_image_count(recipe, image_count) and picture_numbers != set(range(1, image_count + 1)):
        errors.append("Picture labels must match all supplied images exactly as contiguous 1..image_count.")
    for kind, numbers in media_numbers.items():
        if isinstance(counts[kind], int) and not isinstance(counts[kind], bool) and numbers != set(range(1, counts[kind] + 1)):
            errors.append(kind + " labels must match supplied references exactly.")
    if recipe["mode"] == "r2v":
        definitions = sections["subject_definitions"]
        defined_subjects = set(map(int, re.findall(r"(?m)^<Subject ([1-9]\d*)>", definitions)))
        if subjects != defined_subjects:
            errors.append("Every Subject label must be defined at the start of its own definition line.")
        def_ids = re.findall(r"(?m)^<(Subject|Picture|Video|Audio) ([1-9]\d*)>" if multimodal else r"(?m)^<(Subject|Picture) ([1-9]\d*)>", definitions)
        if len(def_ids) != len(set(def_ids)):
            errors.append("Duplicate subject/picture definitions.")
        defined_pictures = set(map(int, re.findall(r"<Picture ([1-9]\d*)>", definitions)))
        if _valid_image_count(recipe, image_count) and defined_pictures != set(range(1, image_count + 1)):
            errors.append("Definitions must identify every supplied source picture.")
        prefixes = r"reference generation|keyframe completion|video editing|video continuation|audio reference|audio reuse" if multimodal else r"reference generation|keyframe completion"
        if not re.match(r"\[(?:" + prefixes + r")(?: \+ (?:" + prefixes + r"))*\]", sections["summary"]):
            errors.append("Summary must begin with a supported task prefix.")
        if multimodal:
            if re.search(r"\[(?:[^\]]*\b)?(?:audio reuse|audio reference)\b", sections["summary"]) and audio_count + video_count == 0:
                errors.append("Audio task prefix requires supplied audio or video references.")
            if re.search(r"\[(?:[^\]]*\b)?(?:video editing|video continuation)\b", sections["summary"]) and video_count == 0:
                errors.append("Video task prefix requires a supplied video reference.")
        retention = sections["retention_analysis"]
        if re.search(r"\(S\d+\)", retention):
            errors.append("Retention analysis must not assign speaker IDs.")
        retention_labels = "Subject|Picture|Video|Audio" if multimodal else "Subject|Picture"
        retained = re.findall(r"(?m)^<(" + retention_labels + r") ([1-9]\d*)>[^\n]*:\s*"
                              r"(?:fully_preserved|partially_preserved|attribute_transfer|weak_reference)\s*-\s*\S", retention)
        if multimodal:
            for kind, count in counts.items():
                defined = set(map(int, re.findall(r"<" + kind + r" ([1-9]\d*)>", definitions)))
                if defined != set(range(1, count + 1)):
                    errors.append("Definitions must identify every supplied " + kind + ".")
            if image_count == video_count == audio_count == 0 and (definitions != "N/A" or retention != "N/A"):
                errors.append("Zero-reference definitions and retention must be N/A.")
        if set(retained) != set(def_ids) or len(retained) != len(set(retained)):
            errors.append("Retention analysis must cover each defined label once with a supported relationship marker.")
        if sections["detailed_description"].startswith("[Shot 1]"):
            errors.append("Full-reference narrative needs an English style opening before [Shot 1].")
    narrative_field = "integrated_multimodal_description" if recipe["mode"] == "i2v" else "detailed_description"
    if any("<d>" in content for field, content in sections.items() if field != narrative_field):
        errors.append("Dialogue belongs only in the narrative section, not definitions or sound summaries.")
    dialogue_tags = re.findall(r"</?d>", text)
    dialogue_blocks = re.findall(r"<d>(.*?)</d>", text, flags=re.S)
    dialogue_bad = dialogue_tags != [tag for _ in dialogue_blocks for tag in ("<d>", "</d>")]
    for block in dialogue_blocks:
        content = re.fullmatch(r"\[([^\]\n]+)\]\s+(.+)", block, flags=re.S)
        if content is None or not content[2].strip() or "<d>" in block:
            dialogue_bad = True
    if dialogue_bad:
        errors.append("Dialogue blocks must be balanced with a language tag and nonempty original words.")
    if duration_valid:
        _timeline_errors(sections["integrated_multimodal_description" if recipe["mode"] == "i2v" else "detailed_description"],
                         float(duration_seconds), errors)
        # Duration declarations in summaries/retention must not contradict the item.
        for match in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", text, flags=re.I):
            if float(match[1]) > duration_seconds:
                errors.append("Explicit seconds exceed requested duration.")
    # Stable unique diagnostics without touching the original content.
    errors[:] = list(dict.fromkeys(errors))
    if not errors:
        result.update(valid=True, text=raw)
    return result
