"""Bounded, explicitly confirmed Studio -> ComfyUI recipe orchestration.

The SQLite manifest is authority, native managers own execution. In particular an
unanswered submit is never translated into permission to create another task.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import re
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from naiba.core.contracts import (
    RECIPE_ACTIONS, RECIPE_EXECUTOR_TYPE, RECIPE_HANDOFF_PROVENANCE,
    RECIPE_MAX_IMAGES_PER_ITEM, RECIPE_MAX_ITEMS, RECIPE_MAX_PROMPT_CHARS,
    RECIPE_MAX_TOTAL_BYTES, RECIPE_SCHEMA_VERSION, RECIPE_INPUT_MODES,
    RECIPE_MEDIA_LIMITS, RECIPE_MEDIA_REFERENCE_NAMES, RECIPE_ITEM_EDIT_FIELDS,
)
from naiba.comfyui.params import media_kind_of_field, scan_params
from naiba.super_agent.recipe_store import RecipeStore, canonical, digest

_ACTIVE = {"queued", "running", "stopping", "submitting", "unknown"}
_NATIVE_ACTIVE = {"queued", "running", "stopping"}
_FAILED = {"failed", "cancelled", "interrupted", "needs_review", "partial", "collection_failed"}


_MEDIA_PROBE_CODE = """import av, io, json, sys
kind=sys.argv[1]
data=sys.stdin.buffer.read()
metadata={}
with av.open(io.BytesIO(data), options={"protocol_whitelist": "pipe,data", "format_whitelist": "mov,matroska,webm,avi,wav,mp3,flac,ogg,aac", "probesize": "5000000", "analyzeduration": "5000000"}) as container:
    import math
    streams = list(container.streams.video if kind == "video" else container.streams.audio)
    if not streams:
        raise ValueError("素材没有所需媒体轨道")
    stream = streams[0]
    seconds = float(stream.duration * stream.time_base) if stream.duration is not None and stream.time_base else float(container.duration or 0) / av.time_base
    if not math.isfinite(seconds) or not 0 < seconds <= 300:
        raise ValueError("音视频时长必须为 0–300 秒的有限正数")
    metadata["duration_seconds"] = seconds
    if kind == "video":
        width, height = stream.codec_context.width, stream.codec_context.height
        if not width or not height or width * height > 16_777_216:
            raise ValueError("视频像素预算超限")
        fps = float(stream.average_rate or stream.base_rate or 0)
        if not math.isfinite(fps) or not 0 < fps <= 120:
            raise ValueError("视频帧率无效或超限")
        metadata.update(width=width, height=height, fps=fps)
    if next(container.decode(stream), None) is None:
        raise ValueError("素材轨道无法解码")
    metadata["has_audio"] = bool(container.streams.audio)
    metadata["verification"] = "metadata_and_first_decoded_frame"
print(json.dumps(metadata))
"""

def probe_media_main(kind: str) -> int:
    """Trusted frozen-executable child mode; stdin bytes, stdout JSON only."""
    import sys
    if kind not in {"video", "audio"}:
        return 2
    try:
        if sys.stdin is None or sys.stdout is None:
            return 2
        # Same constrained decoder as source builds; no user program is executed.
        namespace = {"__name__": "__recipe_media_probe__"}
        old_argv = sys.argv
        try:
            sys.argv = ["recipe-media-probe", kind]
            exec(_MEDIA_PROBE_CODE, namespace)
        finally:
            sys.argv = old_argv
        return 0
    except Exception:
        return 1


def _now() -> int:
    return int(time.time() * 1000)


def _key(body: dict[str, Any]) -> str:
    key = str(body.get("request_id") or body.get("request_key") or "").strip()
    if not key or len(key) > 256:
        raise ValueError("需要有效 request_id")
    return key


def _relative(raw: Any) -> str:
    text = str(raw or "").replace("\\", "/")
    win = PureWindowsPath(text)
    path = PurePosixPath(text)
    if not text or win.drive or path.is_absolute() or any(p in {"", ".", ".."} for p in text.split("/")) or ":" in text or "\x00" in text:
        raise ValueError("source_path 必须是安全相对路径")
    return text


def _slot(raw: dict[str, Any]) -> tuple[str, str]:
    return str(raw.get("node_id") or ""), str(raw.get("field_name") or "")


def _integer(raw: Any, label: str, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw or not low <= raw <= high:
        raise ValueError(f"{label} 必须为 {low}-{high} 的整数")
    return int(raw)


class RecipeCoordinator:
    def __init__(self, app: Any, service: Any):
        self.app = app
        self.service = service
        self.data_dir = Path(app.paths.data_dir).resolve()
        self.store = RecipeStore(self.data_dir)
        self._lock = self.store.lock
        self._last_artifact_check = 0.0

    def _builtins(self) -> list[dict[str, Any]]:
        from naiba.super_agent.recipe_catalog import builtins
        return copy.deepcopy(builtins())

    def catalog(self, owner: str = "") -> dict[str, Any]:
        recipes = [self._install_builtin(recipe) for recipe in self._builtins()]
        if owner:
            recipes += self.store.definitions(owner)
        return {"recipes": recipes, "limits": {"items": RECIPE_MAX_ITEMS, "images_per_item": RECIPE_MAX_IMAGES_PER_ITEM, "videos_per_item": 3, "audios_per_item": 3, "total_bytes": RECIPE_MAX_TOTAL_BYTES}}

    def workflow_referenced(self, workflow_id: str) -> bool:
        try:
            return self.store.workflow_referenced(workflow_id)
        except Exception:
            logging.getLogger(__name__).exception("Cannot verify recipe workflow references")
            return True

    @staticmethod
    def _input_mode(body: dict[str, Any], default: str = "script") -> str:
        mode = body.get("input_mode", default)
        if mode not in RECIPE_INPUT_MODES:
            raise ValueError("input_mode 必须为 prompt 或 script")
        return mode

    @staticmethod
    def _media_counts(refs: list[dict[str, Any]]) -> dict[str, int]:
        return {kind: sum(m.get("media", "image") == kind for m in refs) for kind in RECIPE_MEDIA_LIMITS}

    def _fingerprint(self, graph: dict[str, Any]) -> str:
        from naiba.super_agent.recipe_catalog import workflow_fingerprint
        return workflow_fingerprint(graph)

    def _install_builtin(self, recipe: dict[str, Any]) -> dict[str, Any]:
        graph = recipe["workflow"]
        fingerprint = self._fingerprint(graph)
        if recipe.get("workflow_fingerprint") != fingerprint:
            raise ValueError("内置工作流指纹错误")
        workflow_id = "cwf-recipe-" + digest({"id": recipe["id"], "graph": fingerprint})[:24]
        store = self.app.comfyui.store
        # Never silently overwrite an edited library entry under a trusted ID.
        with store._lock:
            existing = store.get_workflow(workflow_id)
            if existing is not None:
                if self._fingerprint(existing.get("prompt") or {}) != fingerprint:
                    raise ValueError("内置工作流已变更，请恢复原版本")
            else:
                store.save_workflow({"id": workflow_id, "name": recipe["name"], "prompt": graph,
                                     "params": scan_params(graph), "created_at": _now(),
                                     "updated_at": _now(), "recipe_immutable": True})
        recipe["workflow_id"] = workflow_id
        return recipe

    def _recipe(self, owner: str, recipe_id: str) -> dict[str, Any]:
        for recipe in self._builtins():
            if recipe["id"] == recipe_id:
                return self._install_builtin(recipe)
        for recipe in self.store.definitions(owner):
            if recipe["id"] == recipe_id:
                self._verify_workflow(recipe)
                return recipe
        raise LookupError("配方不存在或无权访问")

    def _verify_workflow(self, recipe: dict[str, Any]) -> None:
        entry = self.app.comfyui.store.get_workflow(recipe["workflow_id"])
        if entry is None or self._fingerprint(entry.get("prompt") or {}) != recipe["workflow_fingerprint"]:
            raise ValueError("工作流已变更或删除，配方快照不能继续执行")

    def _validate_binding(self, graph: dict[str, Any], binding: Any) -> None:
        if not isinstance(binding, dict):
            raise ValueError("需要明确 prompt_slot")
        node_id, field = _slot(binding)
        node = graph.get(node_id) or {}
        inputs = node.get("inputs") or {}
        # The workflow is selected from the ComfyUI library and fingerprint-pinned.
        # Mapping a prompt only needs a real editable text field; its downstream
        # routing belongs to the workflow, not to a second recipe-specific graph judge.
        slot = next((param for param in scan_params(graph) if _slot(param) == (node_id, field)), None)
        if (not slot or slot["field_type"] != "text"
                or not isinstance(inputs.get(field), str)):
            raise ValueError("提示词写入位置不是当前工作流的文本字段，请重新选择")

    def _template(self, body: dict[str, Any]) -> dict[str, Any]:
        raw = body.get("template") or {}
        if not isinstance(raw, dict):
            raise ValueError("template 必须是对象")
        if body.get("template_id") and raw.get("id") and str(body["template_id"]) != str(raw["id"]):
            raise ValueError("template_id 与 template.id 冲突")
        if "body" in raw:
            text = raw["body"]
            if not isinstance(text, str) or not text.strip() or len(text) > 100000:
                raise ValueError("提词规则正文为空或过长")
            rule = raw.get("result_rule", body.get("result_rule", {"type": "text"}))
            builtin_rule = next((r["template"]["result_rule"] for r in self._builtins() if r["template"]["id"] == raw.get("id")), None)
            if isinstance(rule, str):
                legacy_rule = raw.get("id") == "recipe_minimax_h3_r2v_v1" and rule == "h3_ref2va_single_v1"
                if rule != builtin_rule and not legacy_rule:
                    raise ValueError("未知的内置结果规则")
            elif not isinstance(rule, dict) or rule.get("type") not in {"text", "json_field", "sections"}:
                raise ValueError("不支持的单结果选择规则")
            elif rule["type"] == "json_field" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(rule.get("field") or "")):
                raise ValueError("JSON 结果字段无效")
            return {"id": str(raw.get("id") or "recipe-rule-" + digest(text)[:20]), "body": text, "result_rule": copy.deepcopy(rule)}
        template_id = str(body.get("template_id") or raw.get("id") or "")
        for recipe in self._builtins():
            if recipe["template"]["id"] == template_id:
                return copy.deepcopy(recipe["template"])
        if template_id == "recipe_minimax_h3_r2v_v1":
            legacy = Path(__file__).resolve().with_name("recipe_assets") / "h3_r2v.v1.prompt.txt"
            return {"id": template_id, "body": legacy.read_text(encoding="utf-8"), "result_rule": "h3_ref2va_single_v1"}
        if not template_id:
            raise ValueError("请选择提词库模板")
        custom_id = str(body.get("custom_id") or raw.get("custom_id") or "")
        if custom_id or template_id.startswith("custom:"):
            custom_id = custom_id or template_id.split(":", 1)[1]
            row = self.app.studio.custom.get(custom_id)
            text = row.get("body") or ""
        else:
            text = self.app.studio.catalog.get_body(template_id)
        rule = body.get("result_rule") or raw.get("result_rule") or {"type": "text"}
        if not isinstance(rule, dict) or rule.get("type") not in {"text", "json_field", "sections"}:
            raise ValueError("不支持的单结果选择规则")
        if rule["type"] == "json_field" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(rule.get("field") or "")):
            raise ValueError("JSON 结果字段无效")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("提词规则正文为空")
        return {"id": template_id, "body": text, "result_rule": rule, "custom_id": custom_id}

    def save_recipe(self, owner: str, body: dict[str, Any]) -> dict[str, Any]:
        if not owner or not isinstance(body, dict):
            raise ValueError("配方请求无效")
        if set(body) & {"api_key", "base_url", "target_url", "workflow", "workflow_snapshot", "submission_policy"}:
            raise ValueError("配方不接受凭据、任意目标或内联工作流")
        source_id = str(body.get("source_recipe_id") or "")
        source = self._recipe(owner, source_id) if source_id else None
        if source is not None:
            inherited = {k: copy.deepcopy(source[k]) for k in ("workflow_id", "workflow_fingerprint", "prompt_slot", "media_slots", "duration_slot", "template", "overrides", "input_mode") if k in source}
            if ("template_id" in body or "custom_id" in body) and "template" not in body:
                inherited.pop("template", None)
            body = {**inherited, **body}
        input_mode = self._input_mode(body)
        workflow_id = str(body.get("workflow_id") or "")
        entry = self.app.comfyui.store.get_workflow(workflow_id)
        if not entry:
            raise LookupError("工作流不存在")
        graph = copy.deepcopy(entry.get("prompt") or {})
        expected_fingerprint = body.get("workflow_fingerprint")
        if not isinstance(expected_fingerprint, str) or not expected_fingerprint.strip():
            raise ValueError("需要当前工作流指纹，请刷新工作流后重新保存配方")
        if expected_fingerprint != self._fingerprint(graph):
            raise ValueError("stale workflow_fingerprint：工作流已变化，请刷新并核对当前参数后保存")
        self._validate_binding(graph, body.get("prompt_slot"))
        slots = body.get("media_slots") or []
        if not isinstance(slots, list):
            raise ValueError("media_slots 必须是数组")
        roles, addresses = set(), {_slot(body["prompt_slot"])}
        normalized = []
        for raw in slots:
            if not isinstance(raw, dict):
                raise ValueError("媒体映射格式无效")
            node_id, field = _slot(raw)
            role = str(raw.get("role") or "")
            node = graph.get(node_id) or {}
            if not role or role in roles or (node_id, field) in addresses or raw.get("scope") not in {"item", "shared"}:
                raise ValueError("媒体角色/槽位重复或范围未明确")
            kind = media_kind_of_field(node.get("class_type"), field)
            if kind not in RECIPE_MEDIA_LIMITS or raw.get("media", kind) != kind or not isinstance((node.get("inputs") or {}).get(field), str):
                raise ValueError("只支持已验证的图片/视频/音频媒体槽位")
            roles.add(role)
            addresses.add((node_id, field))
            normalized.append({"role": role, "label": str(raw.get("label") or role), "node_id": node_id,
                               "field_name": field, "media": kind, "scope": raw["scope"], "required": raw.get("required") is True})
        # Builtin derivation is valid only for its pinned graph and bindings.
        # An unrelated workflow or changed mapping is a custom graph, not H3.
        derived_mode = source.get("mode", "custom") if source else "custom"
        if source and (source["workflow_fingerprint"] != self._fingerprint(graph) or source["prompt_slot"] != body["prompt_slot"] or source["media_slots"] != normalized):
            derived_mode = "custom"
        recipe = {"name": str(body.get("name") or entry.get("name") or "自定义配方"),
                  "description": str(body.get("description") or ""), "mode": derived_mode,
                  "version": 2, "input_mode": input_mode, "source_recipe_id": source_id,
                  "source_recipe_version": source.get("version", 1) if source else None,
                  "workflow_id": workflow_id, "workflow": graph, "workflow_fingerprint": self._fingerprint(graph),
                  "prompt_slot": {"node_id": _slot(body["prompt_slot"])[0], "field_name": _slot(body["prompt_slot"])[1]},
                  "media_slots": normalized, "template": (self._template(body) if input_mode == "script" else None), "provenance": {"source": "user"}}
        if body.get("duration_slot"):
            address = _slot(body["duration_slot"])
            param = next((s for s in scan_params(graph) if _slot(s) == address), None)
            if not param or param["field_type"] != "number" or address in addresses:
                raise ValueError("时长槽位无效")
            recipe["duration_slot"] = {"node_id": address[0], "field_name": address[1]}
        recipe["overrides"] = self._overrides(recipe, body.get("overrides"))
        recipe["id"] = "recipe-custom-" + digest(recipe)[:24]
        with self._lock:
            self.store.save_definition(owner, recipe)
        return {"recipe": recipe}

    # ---- 工作流直建（批量执行助手 2026-11-03 计划 §5.1/§5.2）----
    # 造梦机交接 / 总管新建不再要求先注册「方案」：服务端按参数扫描元数据自动
    # 派生映射并落为不可变定义；用途有歧义时明确报错，不猜。

    @staticmethod
    def _auto_prompt_slot(graph: dict[str, Any], params: list[dict[str, Any]]) -> dict[str, Any]:
        """自动识别提示词写入位：正向连线锚点 → 唯一多行文本槽；歧义时报错。"""
        positive = [slot for slot in params if slot.get("role") == "positive_prompt"]
        if len(positive) == 1:
            return {"node_id": str(positive[0]["node_id"]), "field_name": str(positive[0]["field_name"])}
        multiline = [slot for slot in params if slot.get("field_type") == "text" and slot.get("control") == "multiline"]
        if len(multiline) == 1:
            return {"node_id": str(multiline[0]["node_id"]), "field_name": str(multiline[0]["field_name"])}
        candidates = [f"{slot['node_id']}/{slot['field_name']}" for slot in multiline or positive]
        raise ValueError(
            "无法唯一识别提示词入口（候选：" + ("、".join(candidates) if candidates else "无多行文本槽") +
            "）；请在交接时明确选择提示词写入位，不自动猜测")

    def _auto_media_slots(self, graph: dict[str, Any], params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """自动派生媒体槽位：显式映射的图片/音频/视频入口全部纳入，逐项作用域。"""
        slots, roles = [], set()
        for slot in params:
            kind = slot.get("field_type")
            if kind not in RECIPE_MEDIA_LIMITS:
                continue
            node_id, field = str(slot["node_id"]), str(slot["field_name"])
            node = graph.get(node_id) or {}
            if not isinstance((node.get("inputs") or {}).get(field), str):
                continue
            role = f"m{node_id}"
            if role in roles:
                role = f"m{node_id}_{field}"
            if role in roles:
                raise ValueError(f"工作流媒体槽位地址重复：{node_id}/{field}，请先在造梦机整理该工作流")
            roles.add(role)
            optional = slot.get("media_optional")
            slots.append({"role": role, "label": str(slot.get("label") or f"{node_id} · {field}"),
                          "node_id": node_id, "field_name": field, "media": kind,
                          "scope": "item", "required": optional is False})
        return slots

    @staticmethod
    def _auto_duration_slot(params: list[dict[str, Any]]) -> dict[str, Any] | None:
        """时长槽识别：唯一含「时长 / duration」标题的数值槽；识别不到则不映射。"""
        markers = ("时长", "duration")
        hits = [slot for slot in params if slot.get("field_type") == "number"
                and any(marker in f"{slot.get('node_title', '')} {slot.get('label', '')}".lower() for marker in markers)]
        if len(hits) != 1:
            return None
        return {"node_id": str(hits[0]["node_id"]), "field_name": str(hits[0]["field_name"])}

    def _derive_recipe_from_workflow(self, entry: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        graph = copy.deepcopy(entry.get("prompt") or {})
        fingerprint = self._fingerprint(graph)
        params = scan_params(graph)
        prompt_slot = None
        if body.get("prompt_slot"):
            # 前端在歧义时明确选择过写入位：仍按同一规则验证地址真实可写。
            self._validate_binding(graph, body["prompt_slot"])
            node_id, field = _slot(body["prompt_slot"])
            prompt_slot = {"node_id": node_id, "field_name": field}
        else:
            prompt_slot = self._auto_prompt_slot(graph, params)
        media_slots = self._auto_media_slots(graph, params)
        duration_slot = self._auto_duration_slot(params)
        if duration_slot:
            address = (duration_slot["node_id"], duration_slot["field_name"])
            if address in {(prompt_slot["node_id"], prompt_slot["field_name"])} | {(s["node_id"], s["field_name"]) for s in media_slots}:
                duration_slot = None
        recipe = {"name": str(entry.get("name") or "交接工作流"),
                  "description": "由造梦机「交给任务总管」自动生成的批量方案；映射来自工作流参数扫描，可继续复用。",
                  "mode": "custom", "version": 2, "input_mode": "prompt",
                  "source_recipe_id": "", "source_recipe_version": None,
                  "workflow_id": str(entry.get("id") or ""), "workflow": graph, "workflow_fingerprint": fingerprint,
                  "prompt_slot": prompt_slot, "media_slots": media_slots, "template": None,
                  "provenance": {"source": RECIPE_HANDOFF_PROVENANCE, "workflow_fingerprint": fingerprint,
                                  "source_mode": "current" if body.get("source_mode") == "current" else "saved"}}
        if duration_slot:
            recipe["duration_slot"] = duration_slot
        recipe["overrides"] = []
        recipe["id"] = "recipe-custom-" + digest(recipe)[:24]
        return recipe

    def create_from_workflow(self, owner: str, body: dict[str, Any]) -> dict[str, Any]:
        """交接 / 新建：工作流 + 当前值 → 自动映射派生定义 → 复用既有批次链路。"""
        workflow_id = str(body.get("workflow_id") or "")
        entry = self.app.comfyui.store.get_workflow(workflow_id)
        if not entry:
            raise LookupError("工作流不存在")
        graph = copy.deepcopy(entry.get("prompt") or {})
        fingerprint = body.get("workflow_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("需要当前工作流指纹，请在造梦机刷新后重新交接")
        if fingerprint != self._fingerprint(graph):
            raise ValueError("stale workflow_fingerprint：工作流已变化，请在造梦机刷新后重新交接")
        recipe = self._derive_recipe_from_workflow(entry, body)
        duration_slot = recipe.get("duration_slot")
        overrides_in = [value for value in (body.get("overrides") or []) if isinstance(value, dict)]
        duration_seconds = body.get("duration_seconds", body.get("duration"))
        if duration_slot:
            address = (duration_slot["node_id"], duration_slot["field_name"])
            kept = []
            for value in overrides_in:
                if _slot(value) == address:
                    # 时长是配方管理槽位：把当前调整升格为批次时长，不落成固定参数覆盖。
                    if duration_seconds is None:
                        duration_seconds = value.get("value")
                    continue
                kept.append(value)
            overrides_in = kept
            if duration_seconds is None:
                raw = (graph.get(duration_slot["node_id"]) or {}).get("inputs", {}).get(duration_slot["field_name"])
                duration_seconds = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 15
        if duration_seconds is None:
            duration_seconds = 15
        duration_seconds = _integer(duration_seconds, "时长", 1, 300)
        normalized_overrides = self._overrides(recipe, overrides_in)
        role_by_address = {(slot["node_id"], slot["field_name"]): slot["role"] for slot in recipe.get("media_slots", [])}
        item_media = []
        for ref in body.get("media_inputs") or []:
            if not isinstance(ref, dict):
                raise ValueError("素材引用格式无效")
            role = role_by_address.get((str(ref.get("node_id") or ""), str(ref.get("field_name") or "")))
            if not role:
                raise ValueError(f"素材入口不属于该工作流的媒体槽位：{ref.get('node_id')}/{ref.get('field_name')}")
            item_media.append({"role": role, "path": ref.get("path"), "name": ref.get("name")})
        prompt = body.get("prompt")
        if prompt is None:
            prompt = (graph.get(recipe["prompt_slot"]["node_id"]) or {}).get("inputs", {}).get(recipe["prompt_slot"]["field_name"]) or ""
        prompt = str(prompt)
        if not prompt.strip() or len(prompt) > RECIPE_MAX_PROMPT_CHARS:
            raise ValueError("该工作流没有可用提示词（或超过长度上限）：请在造梦机填写后交接")
        with self._lock:
            self.store.save_definition(owner, recipe)
        payload = {"request_id": body.get("request_id"), "recipe_id": recipe["id"],
                   "title": str(body.get("title") or recipe["name"]), "input_mode": "prompt",
                   "shots": body.get("shots", 1), "overrides": normalized_overrides,
                   "duration_seconds": duration_seconds,
                   "items": [{"name": "组 01", "source_path": "handoff/group-01",
                              "prompt": prompt, "media": item_media, "requirements": ""}]}
        return self.create(owner, payload)


    def _model(self, body: dict[str, Any], *, needs_images: bool = False) -> dict[str, Any]:
        key = str(body.get("model_key") or "").strip()
        if not key:
            raise ValueError("请选择明确的反推模型")
        profile = dict(self.app.config.profile(key))
        name = str(body.get("model_name") or profile.get("model") or "").strip()
        if not name or (needs_images and profile.get("supports_images_explicit") is False):
            raise ValueError("请选择支持图片的反推模型")
        return {"model_key": key, "model_name": name,
                "provider_id": str(profile.get("provider_id") or profile.get("provider") or ""),
                "kind": str(profile.get("kind") or ""),
                "base_url_hash": digest(str(profile.get("base_url") or ""))}

    def _target(self) -> dict[str, Any]:
        from naiba.comfyui.client import normalize_base_url
        url = normalize_base_url(self.app.config.data.get("comfyui_url"))
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("生成目标不能含凭据、查询参数或片段")
        return {"url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), "fingerprint": digest(url)}

    def _media(self, raw: Any, batch_id: str, allowed: set[str], slots: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        if raw is None:
            return []
        if isinstance(raw, dict):
            raw = [{"role": role, "path": path} for role, path in raw.items()]
        if not isinstance(raw, list) or len(raw) > sum(RECIPE_MEDIA_LIMITS.values()):
            raise ValueError("每项最多 9 张图片、3 段视频、3 段音频")
        refs, roles = [], set()
        from PIL import Image
        from naiba.studio.images import ALLOWED_IMAGE_EXTS, MAX_IMAGE_BYTES
        extensions = {"image": ALLOWED_IMAGE_EXTS,
                      "video": {".mp4", ".mov", ".webm", ".mkv", ".avi"},
                      "audio": {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}}
        slot_map = {slot["role"]: slot for slot in (slots or [])}
        counts = {kind: 0 for kind in RECIPE_MEDIA_LIMITS}
        uploads = (self.data_dir / "uploads").resolve()
        for ref in raw:
            if not isinstance(ref, dict):
                raise ValueError("媒体必须显式指定 role/path")
            role = str(ref.get("role") or "")
            if not role or role in roles or role not in allowed:
                raise ValueError("媒体角色重复、未知或未明确")
            roles.add(role)
            kind = slot_map.get(role, {}).get("media", "image")
            if ref.get("media", kind) != kind:
                raise ValueError("媒体类型与已验证槽位不符")
            counts[kind] += 1
            if counts[kind] > RECIPE_MEDIA_LIMITS[kind]:
                raise ValueError("媒体数量超过类型上限")
            text = str(ref.get("path") or "")
            path = Path(text)
            if not path.is_absolute():
                safe = _relative(text)
                path = self.data_dir / safe if safe.startswith("uploads/") else uploads / safe
            source = path.resolve()
            if not source.is_relative_to(uploads) or not source.is_file() or source.suffix.lower() not in extensions[kind]:
                raise ValueError("素材必须是受管 uploads 中对应类型的原件")
            size = source.stat().st_size
            if not 0 < size <= (MAX_IMAGE_BYTES if kind == "image" else 80 * 1024 * 1024):
                raise ValueError("素材原件超限或为空（图片 20MB，音视频 80MB）")
            data = source.read_bytes()
            if len(data) != size:
                raise ValueError("素材读取过程中发生变化")
            metadata = {}
            try:
                if kind == "image":
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width * image.height > 40_000_000:
                            raise ValueError("图片像素超过 4000 万")
                        image.verify()
                    with Image.open(io.BytesIO(data)) as image:
                        image.load()
                else:
                    import subprocess
                    import sys
                    code = _MEDIA_PROBE_CODE
                    # Container probing and one-frame decoding occur in an isolated,
                    # killable child; malformed files cannot hang the request worker.
                    command = [sys.executable, "--probe-recipe-media", kind] if getattr(sys, "frozen", False) else [sys.executable, "-I", "-c", code, kind]
                    completed = subprocess.run(command,
                                               input=data, capture_output=True, timeout=12,
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if completed.returncode:
                        raise ValueError("音视频元信息或首帧解码验证失败")
                    metadata = json.loads(completed.stdout)
            except Exception as exc:
                raise ValueError("素材损坏或无法解码：" + kind) from exc
            content_hash = hashlib.sha256(data).hexdigest()
            target = uploads / "recipes" / batch_id / (content_hash + source.suffix.lower())
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(data)
            elif hashlib.sha256(target.read_bytes()).hexdigest() != content_hash:
                raise ValueError("受管素材快照损坏")
            refs.append({"id": "media-" + digest({"role": role, "source": str(source), "hash": content_hash})[:24],
                         "role": role, "media": kind, "path": str(target), "original_path": str(source),
                         "description": str(ref.get("description") or ""),
                         "name": str(ref.get("name") or source.name), "sha256": content_hash, "size": size,
                         **metadata})
        return refs

    def _overrides(self, recipe: dict[str, Any], raw: Any) -> list[dict[str, Any]]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError("overrides 必须是数组")
        params = {_slot(s): s for s in scan_params(recipe["workflow"])}
        prohibited = {_slot(recipe["prompt_slot"])} | {_slot(s) for s in recipe.get("media_slots", [])}
        if recipe.get("duration_slot"):
            prohibited.add(_slot(recipe["duration_slot"]))
        result, seen = [], set()
        for value in raw:
            if not isinstance(value, dict):
                raise ValueError("参数格式无效")
            address = _slot(value)
            if address in seen or address in prohibited or address not in params:
                raise ValueError("参数重复或与配方管理槽位冲突")
            param = params[address]
            if param["field_type"] not in {"number", "seed", "bool"}:
                raise ValueError(f"固定参数 {address[0]}/{address[1]} 是当前不支持的文本/枚举覆盖，不能静默使用工作流原值；请先在造梦机保存该参数到工作流并刷新，再重新制作配方。首期额外覆盖只支持数值/布尔槽，不接受路径或代码")
            val = value.get("value")
            if param["field_type"] == "bool":
                if not isinstance(val, bool):
                    raise ValueError("参数必须为布尔值")
            elif isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError("参数必须为数字")
            digest(val)  # reject NaN and infinity, never silently coerce
            original = recipe["workflow"][address[0]]["inputs"][address[1]]
            integer_slot = param["field_type"] == "seed" or (isinstance(original, int) and not isinstance(original, bool))
            if integer_slot:
                if not isinstance(val, int) or isinstance(val, bool):
                    raise ValueError(f"整数参数 {address[0]}/{address[1]} 必须是精确 JSON 整数，不能截断或浮点转换")
                if abs(val) > 2 ** 53 - 1:
                    raise ValueError(f"整数参数 {address[0]}/{address[1]} 超过可精确确认范围")
                if param["field_type"] == "seed" and val < 0:
                    raise ValueError("配方固定 seed 不能为负数；随机值请不提供覆盖")
            seen.add(address)
            result.append({"node_id": address[0], "field_name": address[1], "value": val})
        return result

    def _merged_overrides(self, recipe: dict[str, Any], defaults: Any, raw: Any) -> list[dict[str, Any]]:
        values = {_slot(v): v for v in self._overrides(recipe, defaults)}
        values.update({_slot(v): v for v in self._overrides(recipe, raw)})
        return list(values.values())

    def _items(self, recipe: dict[str, Any], batch_id: str, raw: Any, shared: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= RECIPE_MAX_ITEMS:
            raise ValueError("每批需要 1-50 项素材")
        slots = recipe.get("media_slots") or []
        allowed = {s["role"] for s in slots if s.get("scope") == "item"}
        result, ids, sources = [], set(), set()
        total_bytes = sum(m["size"] for m in shared)
        for index, row in enumerate(raw):
            if not isinstance(row, dict):
                raise ValueError("素材项目必须是对象")
            source = _relative(row.get("source_path") or row.get("source_relpath") or row.get("name") or f"item-{index + 1}")
            # Windows clients cannot reliably disambiguate case-only paths.
            if source.casefold() in sources:
                raise ValueError("来源相对路径重复或有歧义")
            sources.add(source.casefold())
            item_id = str(row.get("id") or "item-" + digest({"batch": batch_id, "source": source})[:20])
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item_id) or item_id in ids:
                raise ValueError("项目 ID 重复或无效")
            ids.add(item_id)
            media = self._media(row.get("media"), batch_id, allowed, slots)
            total_bytes += sum(m["size"] for m in media)
            if total_bytes > RECIPE_MAX_TOTAL_BYTES:
                raise ValueError("批次素材总大小超过 300MB")
            mapping = {m["role"]: m for m in media + shared}
            if len(mapping) != len(media) + len(shared):
                raise ValueError("公共/逐项媒体角色冲突")
            for slot in slots:
                if slot.get("required") and slot["role"] not in mapping:
                    raise ValueError(f"缺少素材角色 {slot['role']}")
            prompt = str(row.get("prompt") or "")
            if len(prompt) > RECIPE_MAX_PROMPT_CHARS:
                raise ValueError("提示词超过长度上限")
            included = row.get("included", True)
            if not isinstance(included, bool):
                raise ValueError("included 必须是布尔值")
            result.append({"id": item_id, "name": str(row.get("name") or source), "source_path": source,
                           "source_item_id": str(row.get("source_item_id") or "")[:100],
                           "media": media, "requirements": str(row.get("requirements") or ""),
                           "included": included, "prompt": prompt,
                           "overrides": self._overrides(recipe, row.get("overrides")),
                           "reverse": {"status": "provided" if prompt else "pending", "attempts": []},
                           "generate": {"status": "pending", "attempts": []}, "outputs": []})
        total = sum(m["size"] for item in result for m in item["media"]) + sum(m["size"] for m in shared)
        if total > RECIPE_MAX_TOTAL_BYTES:
            raise ValueError("批次素材总大小超过 300MB")
        return result

    def create(self, owner: str, body: dict[str, Any]) -> dict[str, Any]:
        if not owner or not isinstance(body, dict):
            raise ValueError("配方创建请求无效")
        if set(body) & {"api_key", "base_url", "target_url", "workflow", "workflow_snapshot", "submission_policy", "model_snapshot"}:
            raise ValueError("配方创建不接受凭据、任意目标或工作流覆盖")
        key, payload_hash = _key(body), digest(body)
        with self._lock:
            existing = self.store.creation(owner, key, payload_hash)
            if existing:
                self._link(existing)
                return {"batch": self._public(existing), "thread_id": existing["thread_id"]}
            if str(body.get("source") or "") == "workflow":
                # 工作流直建（造梦机交接 / 总管新建）：派生映射后回到统一批次链路。
                return self.create_from_workflow(owner, body)
            recipe = self._recipe(owner, str(body.get("recipe_id") or ""))
            batch_id = "recipe-" + digest({"owner": owner, "request": key})[:32]
            input_mode = self._input_mode(body, recipe.get("input_mode", "script"))
            shared = self._media(body.get("shared_media"), batch_id, {s["role"] for s in recipe.get("media_slots", []) if s.get("scope") == "shared"}, recipe.get("media_slots", []))
            if shared and not any(s.get("scope") == "shared" for s in recipe.get("media_slots", [])):
                raise ValueError("该配方没有公共媒体槽位")
            batch = {"id": batch_id, "batch_id": batch_id, "owner_id": owner, "thread_id": "", "node_id": "",
                     "schema_version": RECIPE_SCHEMA_VERSION, "title": str(body.get("title") or recipe["name"]),
                     "source_thread_id": str(body.get("source_thread_id") or "")[:100],
                     "recipe_id": recipe["id"], "recipe": recipe, "recipe_hash": digest(recipe),
                     "input_mode": input_mode, "template": copy.deepcopy(recipe.get("template")) if input_mode == "script" else None,
                     "model": None, "target": self._target(),
                     "requirements": str(body.get("requirements") or ""), "shared_media": shared,
                     "overrides": self._merged_overrides(recipe, recipe.get("overrides"), body.get("overrides")),
                     "duration_seconds": _integer(body.get("duration_seconds", body.get("duration", 15)), "时长", 1, 300),
                     "shots": _integer(body.get("shots", 1), "次数", 1, 200),
                     "items": self._items(recipe, batch_id, body.get("items"), shared),
                     "status": "draft", "revision": 1, "confirmations": {}, "error": "",
                     "created_at": _now(), "updated_at": _now()}
            if input_mode == "script":
                if not batch["template"] or not str(batch["template"].get("body") or "").strip():
                    raise ValueError("代写模式需要非空提词规则")
                batch["model"] = self._model(body, needs_images=any(self._media_counts(i["media"] + shared)["image"] for i in batch["items"]))
            elif any(not i["prompt"].strip() for i in batch["items"] if i["included"]):
                raise ValueError("直接模式需要每项完整提示词")
            self._validate_manifest(batch)
            self._refresh_hash(batch)
            # This insert precedes creating the independently stored thread. A
            # crash here is repaired by replay/recover with the same chain ID.
            self.store.insert(batch, key, payload_hash)
            self._link(batch)
            return {"batch": self._public(batch), "thread_id": batch["thread_id"]}

    def _link(self, batch: dict[str, Any]) -> None:
        if not batch.get("thread_id"):
            thread, nodes = self.service.create_thread_chain(
                batch["owner_id"], batch["title"], "按图片分组生成视频",
                chain_instance_id=batch["id"], chain_version=1,
                nodes=[{"kind": RECIPE_EXECUTOR_TYPE, "stage_key": RECIPE_EXECUTOR_TYPE,
                        "sequence": 0, "executor_type": RECIPE_EXECUTOR_TYPE, "executor_id": batch["id"],
                        "execution_snapshot": {"batch_id": batch["id"], "recipe_id": batch["recipe_id"]}}],
                execution_snapshot={"recipe_batch_id": batch["id"], "batch_id": batch["id"], "recipe_id": batch["recipe_id"]})
            batch["thread_id"], batch["node_id"] = thread.thread_id, nodes[0].node_id
            self.store.save(batch)
        self._project(batch)

    def _refresh_hash(self, batch: dict[str, Any]) -> None:
        snapshot = {k: batch[k] for k in ("recipe_hash", "template", "model", "target", "requirements", "shared_media", "overrides", "duration_seconds", "shots")}
        snapshot["input_mode"] = batch.get("input_mode", "script")
        snapshot["items"] = [{k: item[k] for k in ("id", "name", "source_path", "media", "requirements", "included", "prompt", "overrides")} for item in batch["items"]]
        batch["revision_hash"] = digest(snapshot)
        batch["confirmation_hash"] = batch["revision_hash"]

    def _public(self, batch: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(batch)
        # Native intents contain prepared text exactly once in the DB, never in
        # events/checkpoints or UI summary fields.
        for item in result["items"]:
            for stage in ("reverse", "generate"):
                for attempt in item[stage]["attempts"]:
                    attempt.pop("payload", None)
                    attempt.pop("policy", None)
            payload = self._generation_payload(batch, item)
            graph = self._effective_graph(batch, item)
            item["effective_params"] = payload["overrides"] if item["prompt"] else []
            item["effective_workflow_id"] = payload["workflow_id"]
            item["effective_workflow_fingerprint"] = self._fingerprint(graph)
            item["effective_workflow"] = {"id": payload["workflow_id"], "fingerprint": self._fingerprint(graph), "prompt": graph}
            item["effective_media_inputs"] = payload["media_inputs"]
            item["seed_semantics"] = "未显式固定的种子按原生执行器随机策略解析；不承诺预览时已知随机值"
            editable = batch["status"] in {"draft", "review", "partial", "needs_review"} and not any(a["state"] in _ACTIVE for i in batch["items"] for p in ("reverse", "generate") for a in i[p]["attempts"])
            mutable_input = editable and not item["generate"]["attempts"]
            item["edit_capabilities"] = {field: (editable if field in {"included", "name"} else mutable_input) for field in RECIPE_ITEM_EDIT_FIELDS}
            item["capabilities"] = []
            if batch["status"] in {"draft", "review", "partial", "needs_review"}:
                if item["reverse"]["status"] in _FAILED:
                    item["capabilities"].append("retry_reverse")
                if item["generate"]["status"] == "collection_failed":
                    item["capabilities"].append("retry_collect")
                elif item["generate"]["status"] in _FAILED:
                    item["capabilities"].append("retry_generate")
        result["counts"] = {"total": len(batch["items"]), "included": sum(i["included"] for i in batch["items"]),
                            "reversed": sum(i["reverse"]["status"] in {"done", "provided"} for i in batch["items"]),
                            "completed": sum(i["generate"]["status"] == "done" for i in batch["items"]),
                            "generation_requests": sum(i["included"] for i in batch["items"]) * batch["shots"]}
        status = batch["status"]
        capabilities = ["cancel"] if status not in {"completed", "cancelled"} else []
        if status in {"draft", "review", "partial", "needs_review"}:
            capabilities += ["edit", "confirm_reverse", "confirm_generate", "retry_reverse", "retry_generate", "retry_collect"]
        if status in {"reverse_running", "generation_running"}:
            capabilities += ["pause"]
        if status in {"paused", "interrupted"}:
            capabilities += ["continue"]
        result["cancel_pending"] = batch["status"] == "cancelled" and any(a["state"] in _ACTIVE for i in batch["items"] for p in ("reverse", "generate") for a in i[p]["attempts"])
        if batch.get("input_mode") == "prompt":
            capabilities = [c for c in capabilities if c not in {"confirm_reverse", "retry_reverse"}]
        result["capabilities"] = capabilities
        result["clone_draft_available"] = True
        result["duration_editable"] = bool(batch["recipe"].get("duration_slot"))
        return result

    def clone_draft(self, owner: str, thread_id: str) -> dict[str, Any]:
        """Read-only allowlisted projection: never clone authority or attempts."""
        with self._lock:
            batch = self.store.get(owner, thread_id)
            draft = {k: copy.deepcopy(batch[k]) for k in ("recipe_id", "title", "requirements", "overrides", "duration_seconds", "shots")}
            draft["input_mode"] = batch.get("input_mode", "script")
            draft["source_thread_id"] = thread_id
            recipe = batch.get("recipe") or {}
            if (recipe.get("provenance") or {}).get("source") == RECIPE_HANDOFF_PROVENANCE:
                # 工作流直建批次的复制：带出工作流与映射，供「选工作流」创建表单重建。
                draft["workflow"] = {"id": recipe.get("workflow_id"), "workflow_fingerprint": recipe.get("workflow_fingerprint"),
                                     "prompt_slot": recipe.get("prompt_slot"), "media_slots": recipe.get("media_slots")}
            if draft["input_mode"] == "script":
                draft.update({k: batch["model"][k] for k in ("model_key", "model_name")})
            def media_projection(refs):
                return [{k: m[k] for k in ("role", "path", "name", "media", "description") if k in m} for m in refs]
            draft["shared_media"] = media_projection(batch["shared_media"])
            draft["items"] = [{**{k: copy.deepcopy(item[k]) for k in ("name", "source_path", "requirements", "included", "prompt", "overrides")},
                               "media": media_projection(item["media"]), "source_item_id": item["id"]} for item in batch["items"]]
            return {"draft": draft}

    def detail(self, owner: str, thread_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self.store.get(owner, thread_id)
            if self._check_artifacts(batch):
                self.store.save(batch)
            return {"batch": self._public(batch)}

    def _check_artifacts(self, batch: dict[str, Any]) -> bool:
        changed = False
        for item in batch["items"]:
            if item["generate"]["status"] != "done" or not item["generate"]["attempts"]:
                continue
            intent = item["generate"]["attempts"][-1]
            outputs = item.get("outputs") or []
            invalid = [ref for ref in outputs if not self._artifact_valid(Path(str(ref.get("path") or "")))]
            if not outputs or invalid:
                item["generate"]["status"] = "collection_failed"
                intent["state"] = "collection_failed"
                intent["error"] = "已收集产物丢失或损坏，仅允许重新收集原任务产物"
                for ref in invalid:
                    ref["exists"] = False
                changed = True
        if changed:
            if batch["status"] != "cancelled":
                batch["status"] = "partial"
            batch["error"] = "产物丢失或损坏，需要仅补收集；不会重新生成"
            batch["revision"] += 1
            self._refresh_hash(batch)
        return changed

    def _artifact_valid(self, path: Path) -> bool:
        try:
            if not path.resolve().is_relative_to(self.data_dir) or not path.is_file() or path.stat().st_size <= 0:
                return False
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
                from PIL import Image
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    image.load()
            return True
        except (OSError, ValueError):
            return False

    def _scope(self, batch: dict[str, Any], body: dict[str, Any]) -> list[dict[str, Any]]:
        ids = body.get("item_ids")
        if ids is None:
            selected = [i for i in batch["items"] if i["included"]]
        else:
            if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
                raise ValueError("item_ids 必须是明确且不重复的项目 ID 数组")
            known = {i["id"]: i for i in batch["items"]}
            if any(i not in known or not known[i]["included"] for i in ids):
                raise ValueError("补跑范围包含未知或未纳入项目")
            selected = [known[i] for i in ids]
        if not selected:
            raise ValueError("没有纳入的项目")
        return selected

    def action(self, owner: str, thread_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict) or body.get("action") not in RECIPE_ACTIONS:
            raise ValueError("不支持的配方操作")
        key, payload_hash = _key(body), digest(body)
        with self._lock:
            batch = self.store.get(owner, thread_id)
            expected_data = canonical(batch)
            replay = self.store.command(owner, batch["id"], key, payload_hash)
            if replay is not None:
                return replay
            if body.get("revision") is None and not body.get("revision_hash"):
                raise ValueError("需要当前 revision")
            if body.get("revision") is not None and body["revision"] != batch["revision"]:
                raise ValueError("stale revision：配方已变化，请刷新后确认")
            supplied_hash = body.get("revision_hash") or body.get("confirmation_hash")
            if supplied_hash and supplied_hash != batch["revision_hash"]:
                raise ValueError("stale confirmation：确认快照已变化")
            action = body["action"]
            if batch["status"] in {"completed", "cancelled"}:
                raise ValueError("终态批次不能再次执行")
            if action == "edit":
                self._edit(batch, body)
            elif action in {"confirm_reverse", "confirm_generate", "retry_reverse", "retry_generate", "retry_collect"}:
                if batch["status"] in {"paused", "interrupted"}:
                    raise ValueError("请先明确继续批次")
                self._authorize(batch, body, action, key)
            elif action == "pause":
                batch["resume_status"] = batch["status"]
                batch["status"] = "paused"
            elif action == "continue":
                if batch["status"] not in {"paused", "interrupted"}:
                    raise ValueError("批次当前不需要继续")
                batch["status"] = batch.pop("resume_status", "review")
                if batch["status"] not in {"reverse_running", "generation_running", "draft", "review", "needs_review", "partial"}:
                    batch["status"] = "review"
            elif action == "cancel":
                batch["status"] = "cancelled"
                batch["cancel_requested_at"] = _now()
                for item in batch["items"]:
                    for phase in ("reverse", "generate"):
                        for attempt in item[phase]["attempts"]:
                            if attempt["state"] in _ACTIVE or (phase == "generate" and attempt.get("task_id") and attempt["state"] == "interrupted"):
                                attempt["cancel_requested"] = True
                                if attempt["state"] == "interrupted":
                                    attempt["state"] = "unknown"
                                    item[phase]["status"] = "unknown"
            batch["updated_at"] = _now()
            self._refresh_hash(batch)
            response = {"batch": self._public(batch)}
            # Command and authority update are one SQLite transaction. All
            # native side effects (including cancellation) happen afterwards.
            committed = self.store.commit_command(batch, key, payload_hash, response, expected_data=expected_data)
            if committed != response:
                return committed
            if action == "retry_collect":
                thread = self.service.get_thread(owner, thread_id)
                if thread.status == "completed":
                    self.service.store.reopen_recipe_collection(owner_id=owner, thread_id=thread_id, node_id=batch["node_id"], batch_id=batch["id"])
            self._project(batch)
            if action == "cancel":
                self._cancel_native(batch)
            return response

    def _edit(self, batch: dict[str, Any], body: dict[str, Any]) -> None:
        if any(a["state"] in _ACTIVE for i in batch["items"] for p in ("reverse", "generate") for a in i[p]["attempts"]):
            raise ValueError("有原生阶段执行或提交结果未知，暂不能编辑")
        patches = body.get("items") or body.get("patches") or {}
        if isinstance(patches, list):
            if any(not isinstance(p, dict) or not p.get("id") for p in patches) or len({p["id"] for p in patches}) != len(patches):
                raise ValueError("编辑需要唯一稳定项目 ID")
            patches = {p["id"]: p for p in patches}
        if not isinstance(patches, dict):
            raise ValueError("编辑 items 必须按项目 ID 映射")
        by_id = {i["id"]: i for i in batch["items"]}
        if set(patches) - set(by_id):
            raise ValueError("编辑引用未知项目")
        reverse_global = any(k in body for k in ("requirements", "model_key", "model_name", "template_id", "template"))
        if "requirements" in body:
            batch["requirements"] = str(body["requirements"])
        if "model_key" in body or "model_name" in body:
            if batch.get("input_mode", "script") == "script":
                batch["model"] = self._model({**(batch["model"] or {}), **body}, needs_images=any(self._media_counts(i["media"] + batch["shared_media"])["image"] for i in batch["items"]))
        if "template_id" in body or "template" in body:
            if batch.get("input_mode", "script") == "script":
                batch["template"] = self._template(body)
        if "duration" in body or "duration_seconds" in body:
            batch["duration_seconds"] = _integer(body.get("duration_seconds", body.get("duration")), "时长", 1, 300)
            reverse_global = True
        if "shots" in body:
            batch["shots"] = _integer(body["shots"], "次数", 1, 200)
        if "overrides" in body:
            batch["overrides"] = self._merged_overrides(batch["recipe"], batch["recipe"].get("overrides"), body["overrides"])
        if "shared_media" in body:
            batch["shared_media"] = self._media(body["shared_media"], batch["id"], {s["role"] for s in batch["recipe"].get("media_slots", []) if s.get("scope") == "shared"}, batch["recipe"].get("media_slots", []))
            reverse_global = True
        for item in batch["items"]:
            for phase in ("reverse", "generate"):
                for intent in item[phase]["attempts"]:
                    if intent["state"] == "authorized":
                        intent["state"] = "invalidated"
            patch = patches.get(item["id"], {})
            if not isinstance(patch, dict) or set(patch) - {"id", "prompt", "requirements", "media", "included", "overrides", "name"}:
                raise ValueError("项目编辑字段不支持")
            if item["generate"]["status"] == "done" and (set(patch) - {"id", "included", "name"} or reverse_global or any(k in body for k in ("shots", "overrides"))):
                raise ValueError("成功生成项不可改写，请新建批次保留成功事实")
            changed_reverse = reverse_global or any(k in patch for k in ("media", "requirements"))
            if "media" in patch:
                item["media"] = self._media(patch["media"], batch["id"], {s["role"] for s in batch["recipe"].get("media_slots", []) if s.get("scope") == "item"}, batch["recipe"].get("media_slots", []))
            for key in ("requirements", "name"):
                if key in patch:
                    item[key] = str(patch[key])
            if "included" in patch:
                if not isinstance(patch["included"], bool):
                    raise ValueError("included 必须是布尔值")
                item["included"] = patch["included"]
            if "overrides" in patch:
                item["overrides"] = self._overrides(batch["recipe"], patch["overrides"])
            if changed_reverse:
                item["previous_prompt"] = item["prompt"]
                if batch.get("input_mode", "script") == "script" or "media" in patch or "shared_media" in body:
                    item["reverse"]["status"] = "pending"
                    item["prompt"] = ""
            if "prompt" in patch:
                prompt = str(patch["prompt"])
                if not prompt.strip() or len(prompt) > RECIPE_MAX_PROMPT_CHARS:
                    raise ValueError("人工提示词为空或超过长度上限")
                item["prompt"] = prompt
                item["reverse"]["status"] = "provided"
            if item["generate"]["status"] != "done":
                if item["generate"]["attempts"] and (set(patch) - {"id", "included", "name"} or reverse_global or any(k in body for k in ("shots", "overrides"))):
                    raise ValueError("这条视频已经提交过，不能改稿后重新生成；请新建批次保留原有结果")
                if not item["generate"]["attempts"]:
                    item["generate"]["status"] = "pending"
        self._validate_manifest(batch)
        batch["confirmations"] = {}
        batch["revision"] += 1
        batch["status"] = "review" if all(i["prompt"] for i in batch["items"] if i["included"]) else "draft"

    def _validate_manifest(self, batch: dict[str, Any]) -> None:
        total = sum(m["size"] for m in batch["shared_media"])
        for item in batch["items"]:
            refs = batch["shared_media"] + item["media"]
            mapping = {m["role"]: m for m in refs}
            mode = batch["recipe"].get("mode")
            if mode == "i2v" and set(mapping) != {"first_frame"}:
                raise ValueError("首帧配方必须恰好提供一张首帧图片")
            counts = self._media_counts(refs)
            if mode == "r2v":
                expected = {f"{prefix}_{n + 1}" for kind, prefix in (("image", "picture"), ("video", "video"), ("audio", "audio")) for n in range(counts[kind])}
                if set(mapping) != expected:
                    raise ValueError("参考媒体各类型必须连续映射，不允许稀疏暗配")
                if batch["recipe"].get("version", 1) < 2 and (counts["image"] < 1 or counts["image"] > 6 or counts["video"] or counts["audio"]):
                    raise ValueError("v1 配方仅支持 1–6 张图片")
            for kind, count in counts.items():
                if count > RECIPE_MEDIA_LIMITS[kind]:
                    raise ValueError("媒体类型数量超限")
            # Number from frozen slot order, never from filenames or UI list order.
            indices = {kind: 0 for kind in RECIPE_MEDIA_LIMITS}
            for slot in batch["recipe"].get("media_slots", []):
                if slot["role"] in mapping:
                    kind = mapping[slot["role"]].get("media", "image")
                    indices[kind] += 1
                    mapping[slot["role"]]["reference"] = f"<{RECIPE_MEDIA_REFERENCE_NAMES[kind]} {indices[kind]}>"
            if mode in {"i2v", "r2v"} and not 4 <= batch["duration_seconds"] <= 15:
                raise ValueError("H3 内置配方时长必须为 4-15 秒")
            if len(mapping) != len(refs):
                raise ValueError("媒体角色重复")
            for slot in batch["recipe"].get("media_slots", []):
                if slot.get("required") and slot["role"] not in mapping:
                    raise ValueError("缺少必需媒体角色")
            total += sum(m["size"] for m in item["media"])
        if total > RECIPE_MAX_TOTAL_BYTES:
            raise ValueError("批次素材超过 300MB")

    def _assert_baseline(self, batch: dict[str, Any], *, check_model: bool = True) -> None:
        self._verify_workflow(batch["recipe"])
        if check_model and batch.get("input_mode", "script") == "script":
            needs_images = any(self._media_counts(i["media"] + batch["shared_media"])["image"] for i in batch["items"])
            if self._model(batch["model"], needs_images=needs_images) != batch["model"]:
                raise ValueError("反推模型档案已变化，需要新建或编辑重新确认")
        if self._target() != batch["target"]:
            raise ValueError("生成目标已变化，需要重新建立确认快照")
        for item in batch["items"]:
            for media in item["media"] + batch["shared_media"]:
                path = Path(media["path"])
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != media["sha256"]:
                    raise ValueError("素材快照已丢失或损坏")

    def _probe_target(self, batch: dict[str, Any]) -> None:
        """生成前的冻结目标可达性检查（计划 §5.3）；只探测本批次冻结地址。"""
        from naiba.comfyui import client as comfy_client
        url = (batch.get("target") or {}).get("url") or ""
        if comfy_client.reachable(url, timeout=3) is None:
            raise ValueError(f"生成目标不可达：{url or '（未配置）'}；请启动 ComfyUI 或核对地址后重新检测")

    def _prompt_reference_errors(self, batch: dict[str, Any], item: dict[str, Any]) -> list[str]:
        """提示词媒体引用检查；返回问题列表（第一条即可作为拒绝理由）。"""
        counts = self._media_counts(item["media"] + batch["shared_media"])
        limits = {"Picture": counts["image"], "Video": counts["video"], "Audio": counts["audio"]}
        errors = []
        for match in re.finditer(r"<\s*(Picture|Video|Audio)\b[^>]*>", str(item.get("prompt") or ""), flags=re.I):
            canonical_label = re.fullmatch(r"<(Picture|Video|Audio) ([1-9]\d*)>", match[0])
            if canonical_label is None or int(canonical_label[2]) > limits.get(canonical_label[1], 0):
                errors.append("提示词引用了不存在或编号无效的媒体：" + match[0])
        return errors

    def precheck(self, owner: str, thread_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """生成前预检（只读）：冻结目标可达 / 工作流快照 / 媒体有效 / 提示词引用。"""
        if not isinstance(body, dict):
            raise ValueError("预检请求无效")
        with self._lock:
            batch = self.store.get(owner, thread_id)
            selected = self._scope(batch, body)
            checks = []
            target_url = (batch.get("target") or {}).get("url") or ""
            try:
                self._probe_target(batch)
                checks.append({"key": "target", "ok": True, "label": "实际目标可达", "detail": ""})
                target_ok = True
            except ValueError as exc:
                checks.append({"key": "target", "ok": False, "label": "实际目标可达", "detail": str(exc)})
                target_ok = False
            try:
                self._verify_workflow(batch["recipe"])
                checks.append({"key": "workflow", "ok": True, "label": "工作流与输入就绪", "detail": ""})
            except (ValueError, LookupError) as exc:
                checks.append({"key": "workflow", "ok": False, "label": "工作流与输入就绪", "detail": str(exc)})
            missing = []
            for item in selected:
                label = item.get("name") or item["id"]
                for media in item["media"] + batch["shared_media"]:
                    path = Path(media["path"])
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != media.get("sha256"):
                        missing.append(f"{label} · {media.get('name') or media['role']}")
            checks.append({"key": "media", "ok": not missing, "label": "媒体文件有效",
                           "detail": "；".join(missing[:5]) + ("等" if len(missing) > 5 else "") if missing else ""})
            prompt_issues = []
            for item in selected:
                label = item.get("name") or item["id"]
                if not str(item.get("prompt") or "").strip():
                    prompt_issues.append(f"{label} 还没有提示词")
                    continue
                for issue in self._prompt_reference_errors(batch, item):
                    if issue not in prompt_issues:
                        prompt_issues.append(issue)
            checks.append({"key": "prompt", "ok": not prompt_issues, "label": "提示词引用存在",
                           "detail": "；".join(prompt_issues[:5])})
            return {"checked_at": _now(), "target": {"url": target_url, "ok": target_ok},
                    "checks": checks, "ok": all(check["ok"] for check in checks)}

    def _authorize(self, batch: dict[str, Any], body: dict[str, Any], action: str, key: str) -> None:
        reverse = action in {"confirm_reverse", "retry_reverse"}
        if reverse and batch.get("input_mode", "script") == "prompt":
            raise ValueError("直接提示词模式不执行代写阶段")
        self._assert_baseline(batch, check_model=reverse)
        if not reverse:
            # 生成阶段的授权以「本批冻结目标可达」为前提（计划 §5.3）；离线在
            # 确认时就被拦下，不进入已运行状态。
            self._probe_target(batch)
        if any(a["state"] in _ACTIVE | {"authorized"} for i in batch["items"] for p in ("reverse", "generate") for a in i[p]["attempts"]):
            raise ValueError("仍有已确认阶段或原生执行未结束或结果未知")
        selected = self._scope(batch, body)
        reverse = action in {"confirm_reverse", "retry_reverse"}
        phase = "reverse" if reverse else "generate"
        targets = []
        for item in selected:
            state = item[phase]["status"]
            if state in {"done", "provided"}:
                continue
            if action in {"confirm_reverse", "confirm_generate"} and state in _FAILED:
                raise ValueError("失败阶段必须使用明确的补跑操作，不可重建原生执行")
            if action.startswith("retry_") and state not in _FAILED:
                raise ValueError("只能补跑明确失败阶段")
            if action == "retry_generate" and state == "collection_failed":
                raise ValueError("该项仅产物收集失败，请使用 retry_collect，不可重新生成")
            if reverse and item["generate"]["attempts"]:
                raise ValueError("已有生成执行记录，不能改变反推输入后续跑旧生成；请新建批次")
            if not reverse and not item["prompt"].strip():
                raise ValueError("纳入项目尚无可用提示词，请编辑或明确排除失败项")
            if not reverse and action != "retry_collect":
                errors = self._prompt_reference_errors(batch, item)
                if errors:
                    raise ValueError(errors[0])
            targets.append(item)
        if not targets:
            raise ValueError("所选范围没有需要执行的阶段")
        for item in targets:
            old = item[phase]["attempts"][-1] if item[phase]["attempts"] else None
            payload = self._reverse_payload(batch, item) if reverse else self._generation_payload(batch, item, install=True)
            operation = "submit"
            native_id = ""
            if action in {"retry_generate", "retry_collect"}:
                if not old or not old.get("task_id"):
                    raise ValueError("缺少可恢复原生任务，不能盲目重建生成")
                native_id = old["task_id"]
                operation = "retry_collect" if action == "retry_collect" else "retry"
            intent = {"attempt": len(item[phase]["attempts"]) + 1, "state": "authorized", "operation": operation,
                      "request_key": "recipe-stage-" + digest({"batch": batch["id"], "item": item["id"], "phase": phase, "command": key}),
                      "payload": payload, "payload_hash": digest(payload), "task_id": "", "source_task_id": native_id or (old.get("task_id", "") if old else ""),
                      "revision_hash": batch["revision_hash"], "created_at": _now(),
                      "policy": {"mode": "recipe", "target_url": batch["target"]["url"],
                                 "workflow_fingerprint": self._fingerprint((self.app.comfyui.store.get_workflow(payload["workflow_id"]) or {}).get("prompt") or {}) if not reverse else "",
                                 "model_identity": batch["model"]}, "error": ""}
            item[phase]["attempts"].append(intent)
            item[phase]["status"] = "authorized"
        batch["confirmations"][phase] = {"revision": batch["revision"], "hash": batch["revision_hash"],
                                               "item_ids": [i["id"] for i in targets], "action": action,
                                               "payload_hashes": [i[phase]["attempts"][-1]["payload_hash"] for i in targets], "confirmed_at": _now()}
        batch["status"] = "reverse_running" if reverse else "generation_running"
        batch["error"] = ""

    def _reverse_payload(self, batch: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        from naiba.studio.service import fill_vars
        requirements = "\n".join(t for t in (batch["requirements"], item["requirements"]) if t)
        counts = self._media_counts(item["media"] + batch["shared_media"])
        variables = {"duration": str(batch["duration_seconds"]), "duration_seconds": str(batch["duration_seconds"]),
                     "requirements": requirements, "image_count": str(counts["image"]),
                     "video_count": str(counts["video"]), "audio_count": str(counts["audio"])}
        content, unresolved = fill_vars(batch["template"]["body"], requirements, variables)
        # Templates without variables still receive the user's requirements and
        # duration; literal text is data, never interpreted as orchestration.
        content += f"\n\n用户补充要求：\n{requirements}\n目标时长：{batch['duration_seconds']} 秒。"
        mapping = {m["role"]: m for m in batch["shared_media"] + item["media"]}
        ordered = [mapping[s["role"]] for s in batch["recipe"].get("media_slots", []) if s["role"] in mapping]
        av_descriptions = [{"reference": m.get("reference"), "description": m.get("description", "")} for m in ordered if m.get("media", "image") != "image"]
        if av_descriptions:
            content += "\n\n音视频仅提供以下用户文字描述，提词模型没有观看或听取原件，不得声称已分析原件。原件只交给 H3：\n" + json.dumps(av_descriptions, ensure_ascii=False)
        refs = [{"path": m["path"], "name": m["name"], "thumb_path": ""} for m in ordered if m.get("media", "image") == "image"]
        request = {"user_request": content, "model_key": batch["model"]["model_key"], "model_name": batch["model"]["model_name"], "images": refs}
        prepared = self.app.studio.prepare_generation(request, recipe=True) if len(refs) > 6 else self.app.studio.prepare_generation(request)
        if isinstance(prepared, tuple):
            prepared, status = prepared
            if status >= 400:
                raise ValueError(str(prepared.get("error") or "反推输入校验失败"))
        if not isinstance(prepared, dict):
            raise ValueError("提词库准备接口未返回有效输入")
        # The frozen template is already composed above; never re-read mutable
        # catalog text at execution time.
        prepared["source_body"] = batch["template"]["body"]
        prepared["recipe_template_id"] = batch["template"]["id"]
        return prepared

    def _effective_graph(self, batch: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        recipe = batch["recipe"]
        if recipe.get("mode") == "r2v":
            from naiba.super_agent.recipe_catalog import prepare_workflow
            return prepare_workflow(recipe["id"], recipe_snapshot=recipe,
                                    **{kind + "_count": count for kind, count in self._media_counts(batch["shared_media"] + item["media"]).items()})
        return copy.deepcopy(recipe["workflow"])

    def _generation_payload(self, batch: dict[str, Any], item: dict[str, Any], *, install: bool = False) -> dict[str, Any]:
        recipe = batch["recipe"]
        overrides = {_slot(s): copy.deepcopy(s) for s in batch["overrides"]}
        overrides.update({_slot(s): copy.deepcopy(s) for s in item["overrides"]})
        slot = recipe["prompt_slot"]
        overrides[_slot(slot)] = {**slot, "value": item["prompt"]}
        if recipe.get("duration_slot"):
            slot = recipe["duration_slot"]
            overrides[_slot(slot)] = {**slot, "value": batch["duration_seconds"]}
        refs = {m["role"]: m for m in batch["shared_media"] + item["media"]}
        media = []
        for slot in recipe.get("media_slots", []):
            if slot["role"] in refs:
                media.append({"node_id": slot["node_id"], "field_name": slot["field_name"], "path": refs[slot["role"]]["path"], "media": refs[slot["role"]].get("media", "image")})
        workflow_id = recipe["workflow_id"]
        if recipe.get("mode") == "r2v":
            from naiba.super_agent.recipe_catalog import prepare_workflow
            graph = self._effective_graph(batch, item)
            if isinstance(graph.get("workflow"), dict):
                graph = graph["workflow"]
            fingerprint = self._fingerprint(graph)
            workflow_id = "cwf-recipe-variant-" + digest({"source": recipe["workflow_fingerprint"], "derived": fingerprint})[:24]
            store = self.app.comfyui.store
            with store._lock:
                entry = store.get_workflow(workflow_id)
                if entry is None and install:
                    store.save_workflow({"id": workflow_id, "name": recipe["name"], "prompt": graph,
                                         "params": scan_params(graph), "recipe_immutable": True,
                                         "source_fingerprint": recipe["workflow_fingerprint"],
                                         "workflow_fingerprint": fingerprint, "created_at": _now(), "updated_at": _now()})
                elif install and entry is not None and self._fingerprint(entry.get("prompt") or {}) != fingerprint:
                    raise ValueError("派生工作流快照已变更")
        return {"workflow_id": workflow_id, "overrides": list(overrides.values()), "media_inputs": media, "shots": batch["shots"]}

    def _jobs(self, phase: str):
        return self.app.studio.jobs if phase == "reverse" else self.app.comfyui.jobs

    def _lookup(self, batch: dict[str, Any], phase: str, intent: dict[str, Any]):
        jobs = self._jobs(phase)
        task = jobs.lookup_request(intent["request_key"], owner_id=batch["owner_id"])
        if task:
            return task
        if intent.get("task_id"):
            # Retry receipts may be absent in old stores; a known exact native
            # ID is still an observation, never grounds for another submit.
            return jobs.store.get_task(intent["task_id"])
        return None

    def _submit(self, batch: dict[str, Any], item: dict[str, Any], phase: str, intent: dict[str, Any]) -> None:
        jobs = self._jobs(phase)
        intent["state"] = "submitting"
        item[phase]["status"] = "submitting"
        self.store.save(batch)  # durable intent BEFORE crossing storage boundary
        try:
            kwargs = {"request_key": intent["request_key"], "owner_id": batch["owner_id"],
                      "source_task_id": intent["source_task_id"], "submission_policy": intent["policy"]}
            if intent["operation"] == "submit":
                task = jobs.submit(copy.deepcopy(intent["payload"]), **kwargs)
            else:
                kwargs.pop("source_task_id")
                task = getattr(jobs, intent["operation"])(intent["source_task_id"], **kwargs)
            intent["task_id"] = str(task["id"])
            intent["state"] = str(task.get("status") or "queued")
            item[phase]["status"] = intent["state"]
        except Exception as exc:
            # A missing response does not establish that the native store or
            # remote endpoint did not accept the work. Reconcile by key only.
            intent["state"] = "unknown"
            intent["error"] = str(exc)
            item[phase]["status"] = "unknown"
            batch["resume_status"] = batch["status"]
            batch["status"] = "needs_review"
            batch["error"] = "原生提交结果未知；只按持久请求键对账，不自动重提"
        self.store.save(batch)

    def _validate_output(self, batch: dict[str, Any], item: dict[str, Any], raw: str) -> dict[str, Any]:
        if batch["recipe"].get("mode") != "custom" and batch["template"] == batch["recipe"]["template"]:
            from naiba.super_agent.recipe_catalog import validate_output
            return validate_output(batch["recipe_id"], raw, recipe_snapshot=batch["recipe"],
                                   **{kind + "_count": n for kind, n in self._media_counts(item["media"] + batch["shared_media"]).items()}, duration_seconds=batch["duration_seconds"])
        rule = batch["template"]["result_rule"]
        if isinstance(rule, str):
            from naiba.super_agent.recipe_catalog import validate_output
            for builtin in self._builtins():
                if builtin["template"]["id"] == batch["template"]["id"] or (rule == "h3_ref2va_single_v1" and builtin["mode"] == "r2v"):
                    validation_recipe = copy.deepcopy(builtin)
                    if rule.endswith("_v1") and validation_recipe["mode"] == "r2v":
                        validation_recipe["version"] = 1
                    return validate_output(builtin["id"], raw, recipe_snapshot=validation_recipe,
                                           **{kind + "_count": n for kind, n in self._media_counts(item["media"] + batch["shared_media"]).items()}, duration_seconds=batch["duration_seconds"])
            return {"valid": False, "raw": raw, "text": "", "errors": ["未知的内置结果规则，需要人工编辑"]}
        errors, text = [], raw
        if rule["type"] == "json_field":
            try:
                def unique_fields(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("JSON 包含重复字段，结果有歧义")
                        result[key] = value
                    return result
                value = json.loads(raw, object_pairs_hook=unique_fields)
                if not isinstance(value, dict) or not isinstance(value.get(rule["field"]), str):
                    raise ValueError("需要单个 JSON 字符串字段")
                text = value[rule["field"]]
            except (ValueError, KeyError) as exc:
                errors.append(str(exc))
                text = ""
        elif rule["type"] == "sections":
            errors.append("自定义多节输出需人工编辑为单条提示词")
            text = ""
        elif raw.lstrip().startswith(("[", "{")) or re.search(r"(?i)</?(?:think|analysis|reasoning)\b", raw) or re.search(r"(?im)^\s*(?:```|~~~|#{1,6}\s|(?:prompt|提示词|方案)\s*(?:\d+|[一二三四五六七八九十百]+)[：:])", raw):
            errors.append("输出含 JSON 结构或编号候选，需人工核对，不能猜选结果")
        if not text.strip() or len(text) > RECIPE_MAX_PROMPT_CHARS:
            errors.append("结果为空或超过长度上限")
        return {"valid": not errors, "raw": raw, "text": text if not errors else "", "errors": errors}

    def _outputs(self, batch: dict[str, Any], task: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        result = task.get("result") or {}
        outputs = result.get("outputs") or []
        if not isinstance(outputs, list):
            return [], False
        verified = []
        for raw in outputs:
            ref = raw if isinstance(raw, dict) else {"path": str(raw)}
            path = Path(str(ref.get("path") or ref.get("source") or ""))
            if not path.is_absolute():
                path = self.app.comfyui.store.task_output_dir(str(task["id"])) / path
            resolved = path.resolve()
            if self._artifact_valid(resolved):
                verified.append({**ref, "path": str(resolved), "exists": True})
        progress = task.get("progress") or {}
        completed = result.get("completed") or (task.get("checkpoint") or {}).get("completed") or []
        done_count = max(int(progress.get("done") or 0), len(completed))
        complete = bool(outputs) and len(verified) == len(outputs) and done_count >= batch["shots"]
        return verified, complete

    def _reconcile(self, batch: dict[str, Any]) -> bool:
        changed = False
        for item in batch["items"]:
            for phase in ("reverse", "generate"):
                for intent in item[phase]["attempts"]:
                    if intent["state"] not in _ACTIVE:
                        continue
                    before_intent = digest(intent)
                    task = self._lookup(batch, phase, intent)
                    if not task:
                        if intent["state"] != "unknown":
                            intent["state"] = "unknown"
                            item[phase]["status"] = "unknown"
                            batch["error"] = "原生请求记录尚不可确认，不会重新提交"
                            if batch["status"] not in {"cancelled", "paused", "interrupted"}:
                                batch["status"] = "needs_review"
                            changed = True
                        continue
                    native_status = str(task.get("status") or "unknown")
                    intent["task_id"] = str(task["id"])
                    cancellation = (task.get("checkpoint") or {}).get("cancellation") or {}
                    if cancellation:
                        intent["cancellation"] = copy.deepcopy(cancellation)
                    if cancellation.get("state") == "pending_remote":
                        intent["state"] = "stopping"
                        item[phase]["status"] = "stopping"
                        changed = changed or digest(intent) != before_intent
                        continue
                    if native_status in _NATIVE_ACTIVE:
                        if intent["state"] != native_status:
                            intent["state"] = native_status
                            item[phase]["status"] = native_status
                            changed = True
                        continue
                    intent["native_status"] = native_status
                    intent["error"] = str(task.get("error") or task.get("detail") or "")
                    intent["finished_at"] = _now()
                    if phase == "reverse":
                        raw_result = task.get("raw_result") if "raw_result" in task else task.get("result")
                        raw = raw_result if isinstance(raw_result, str) else str((raw_result or {}).get("text") or (raw_result or {}).get("content") or task.get("result_partial") or "")
                        intent["raw"] = raw  # complete untruncated provider response
                        if native_status == "done":
                            if task.get("raw_result_source") != "runtime_callback" or "raw_result" not in task:
                                validation = {"valid": False, "raw": raw, "text": "", "errors": ["缺少原生运行时原始结果证明，需人工核对，不接受清理后文本代替原文"]}
                            else:
                                validation = self._validate_output(batch, item, raw)
                            if len(str(validation.get("text") or "")) > RECIPE_MAX_PROMPT_CHARS:
                                validation = {**validation, "valid": False, "text": "", "errors": [*(validation.get("errors") or []), "提示词超过配方长度上限，原文已保留"]}
                            intent["validation"] = validation
                            intent["state"] = "done" if validation.get("valid") else "needs_review"
                            if validation.get("valid") and batch["status"] != "cancelled":
                                item["prompt"] = str(validation["text"])
                        else:
                            intent["state"] = native_status if native_status in _FAILED else "needs_review"
                    else:
                        outputs, complete = self._outputs(batch, task)
                        intent["outputs"] = outputs
                        item["outputs"] = outputs
                        intent["state"] = "done" if native_status in {"done", "completed"} and complete else "partial" if outputs else native_status if native_status in _FAILED else "needs_review"
                        submissions = (task.get("checkpoint") or {}).get("submissions") or {}
                        intent["native_submissions"] = [{k: value for k, value in row.items() if k in {"state", "prompt_id", "error"}} for row in submissions.values() if isinstance(row, dict)]
                        if any(row.get("state") == "collection_failed" for row in submissions.values() if isinstance(row, dict)):
                            intent["state"] = "collection_failed"
                        elif (task.get("checkpoint") or {}).get("pending_generation"):
                            intent["state"] = "interrupted"
                            intent["pending_generation"] = True
                            intent["error"] = "既有产物已收集，仍有尚未提交的生成段；需要明确补生成"
                        if native_status in {"done", "completed"} and not complete:
                            intent["error"] = "原生任务结束但产物缺失或仅部分完成"
                    item[phase]["status"] = intent["state"]
                    changed = True
        if changed:
            batch["revision"] += 1
            self._refresh_hash(batch)
        return changed

    def _settle(self, batch: dict[str, Any]) -> None:
        if batch["status"] == "needs_review" and batch.get("resume_status") in {"reverse_running", "generation_running"}:
            if not any(a["state"] == "unknown" for i in batch["items"] for p in ("reverse", "generate") for a in i[p]["attempts"]):
                # Reconciliation is not permission to claim the remaining items.
                batch["status"] = "interrupted"
                batch["error"] = "已找回原生请求；请明确继续未执行项"
        if batch["status"] not in {"reverse_running", "generation_running"}:
            return
        phase = "reverse" if batch["status"] == "reverse_running" else "generate"
        scoped = set((batch["confirmations"].get(phase) or {}).get("item_ids") or [])
        items = [i for i in batch["items"] if i["id"] in scoped]
        if any(i[phase]["status"] in _ACTIVE | {"authorized"} for i in items):
            return
        if phase == "reverse":
            batch["status"] = "review" if all(i["prompt"].strip() for i in batch["items"] if i["included"]) else "needs_review"
        else:
            included = [i for i in batch["items"] if i["included"]]
            batch["status"] = "completed" if included and all(i["generate"]["status"] == "done" for i in included) else "partial"

    def _cancel_native(self, batch: dict[str, Any]) -> None:
        for item in batch["items"]:
            for phase in ("reverse", "generate"):
                for intent in item[phase]["attempts"]:
                    if not intent.get("cancel_requested") or intent.get("cancel_sent"):
                        continue
                    task = self._lookup(batch, phase, intent)
                    checkpoint = (task or {}).get("checkpoint") or {}
                    remote_outstanding = bool(checkpoint.get("prompt_ids")) or any(row.get("state") in {"submitted", "unknown", "pending_remote", "submitting"} for row in (checkpoint.get("submissions") or {}).values() if isinstance(row, dict)) or (checkpoint.get("cancellation") or {}).get("state") == "pending_remote"
                    if task and (str(task.get("status")) in _NATIVE_ACTIVE or remote_outstanding):
                        try:
                            intent["cancel_supported"] = bool(self._jobs(phase).cancel(str(task["id"])))
                            intent["cancel_sent"] = True
                        except Exception as exc:
                            intent["cancel_error"] = str(exc)
                    elif task:
                        intent["cancel_sent"] = True
        self.store.save(batch)

    def tick(self) -> None:
        with self._lock:
            batches = self.store.batches()
            check_artifacts = time.monotonic() - self._last_artifact_check >= 30
            if check_artifacts:
                self._last_artifact_check = time.monotonic()
            for batch in batches:
                artifact_changed = self._check_artifacts(batch) if check_artifacts else False
                if artifact_changed:
                    self.store.save(batch)
                before = digest(batch)
                active = any(a["state"] in _ACTIVE for i in batch["items"] for p in ("reverse", "generate") for a in i[p]["attempts"])
                if batch["status"] in {"completed", "cancelled"} and not active:
                    continue
                try:
                    if not batch.get("thread_id"):
                        self._link(batch)
                    self._reconcile(batch)
                    if batch["status"] == "cancelled":
                        self._cancel_native(batch)
                    self._settle(batch)
                    if digest(batch) != before:
                        self.store.save(batch)
                        self._project(batch)
                except Exception:
                    logging.getLogger(__name__).exception("Recipe batch reconciliation failed: %s", batch["id"])
                    # Leave its last durable intent unchanged. Other batches can
                    # still reconcile; unknown work remains a serial claim gate.
                    continue
            # Bounded serial execution, even between two recipe batches. Paused
            # or unknown native requests remain in-flight and block new claims.
            if any(a["state"] in _ACTIVE for b in batches for i in b["items"] for p in ("reverse", "generate") for a in i[p]["attempts"]):
                return
            for batch in batches:
                if batch["status"] not in {"reverse_running", "generation_running"}:
                    continue
                phase = "reverse" if batch["status"] == "reverse_running" else "generate"
                for item in batch["items"]:
                    if item[phase]["status"] != "authorized":
                        continue
                    intent = item[phase]["attempts"][-1]
                    try:
                        self._assert_baseline(batch, check_model=phase == "reverse")
                        if phase == "generate":
                            # 排队后目标可能已变化：提交前再探一次冻结目标。
                            self._probe_target(batch)
                    except (ValueError, LookupError, OSError) as exc:
                        batch["status"], batch["error"] = "needs_review", str(exc)
                        self.store.save(batch)
                        self._project(batch)
                        return
                    self._submit(batch, item, phase, intent)
                    self._project(batch)
                    return

    def recover(self) -> int:
        recovered = 0
        with self._lock:
            for batch in self.store.batches():
                self._link(batch)
                self._reconcile(batch)
                self._check_artifacts(batch)
                if batch["status"] not in {"completed", "cancelled", "draft", "review", "needs_review", "partial"}:
                    batch["resume_status"] = batch.get("resume_status") or batch["status"]
                    batch["status"] = "interrupted"
                    recovered += 1
                self.store.save(batch)
                self._project(batch)
        return recovered

    def referenced_upload_paths(self) -> set[str]:
        from naiba.core.paths import normalized_path_key
        refs = set()
        for batch in self.store.batches():
            # Historical manifests intentionally remain reproducible, including
            # cancelled batches reviewed for late results.
            for media in batch["shared_media"] + [m for i in batch["items"] for m in i["media"]]:
                refs.add(normalized_path_key(Path(media["path"])))
                refs.add(normalized_path_key(Path(media["original_path"])))
        return refs

    def _project(self, batch: dict[str, Any]) -> None:
        if not batch.get("thread_id"):
            return
        owner, thread_id, node_id = batch["owner_id"], batch["thread_id"], batch["node_id"]
        with self.service._lock:
            node = self.service.get_node(owner, node_id)
            thread = self.service.get_thread(owner, thread_id)
            status = batch["status"]
            if node.status == "succeeded" and thread.status == "completed" and status != "completed":
                # Repair a crash after committing explicit collection authority
                # but before reopening the independently stored projection.
                confirmation = batch["confirmations"].get("generate") or {}
                if status == "generation_running" and confirmation.get("action") == "retry_collect":
                    self.service.store.reopen_recipe_collection(owner_id=owner, thread_id=thread_id, node_id=node_id, batch_id=batch["id"])
                    node = self.service.get_node(owner, node_id)
                    thread = self.service.get_thread(owner, thread_id)
                else:
                    # Historical completion stays until explicit collection
                    # authority; detail still reports the missing output facts.
                    return
            cancel_pending = status == "cancelled" and any(a["state"] in _ACTIVE for i in batch["items"] for p in ("reverse", "generate") for a in i[p]["attempts"])
            desired_node = "stopping" if cancel_pending else "succeeded" if status == "completed" else "cancelled" if status == "cancelled" else "running" if status in {"reverse_running", "generation_running"} else "waiting_user"
            desired_thread = "stopping" if cancel_pending else "completed" if status == "completed" else "cancelled" if status == "cancelled" else "paused" if status == "paused" else "needs_recovery" if status == "interrupted" else "active" if desired_node == "running" else "waiting_user"
            if node.status == "interrupted" and desired_node not in {"cancelled", "ready"}:
                node = self.service.store.update_node(node_id, owner_id=owner, status="ready")
            if node.status == "ready" and desired_node == "succeeded":
                node = self.service.store.update_node(node_id, owner_id=owner, status="running")
            if node.status != desired_node:
                self.service.store.update_node(node_id, owner_id=owner, status=desired_node,
                                               checkpoint={"batch_id": batch["id"], "revision": batch["revision"], "status": status},
                                               error=batch.get("error", ""), cancel_requested=status == "cancelled")
            from naiba.super_agent.models import THREAD_TRANSITIONS
            if thread.status != desired_thread and desired_thread not in THREAD_TRANSITIONS.get(thread.status, set()) and "active" in THREAD_TRANSITIONS.get(thread.status, set()):
                thread = self.service.store.update_thread(thread_id, owner_id=owner, status="active")
            if thread.status != desired_thread:
                self.service.store.update_thread(thread_id, owner_id=owner, status=desired_thread,
                                                 summary=f"配方批次：{status}")
            self.service.store.append_event(owner_id=owner, thread_id=thread_id, node_id=node_id,
                                            event_type="user_message", source="recipe",
                                            payload={"batch_id": batch["id"], "status": status, "revision": batch["revision"]},
                                            idempotency_key=f"recipe:{batch['id']}:{batch['revision']}:{status}")
