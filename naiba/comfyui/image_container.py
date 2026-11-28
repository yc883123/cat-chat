"""ImageContainer's selected_json adapter; other nodes and pool_json are opaque.

Native references name files on ComfyUI, never files on this host. Only the
explicit host-private media key may cause a managed upload to be read.
"""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterator

from naiba.comfyui import MEDIA_INPUT_MAX_BYTES
from naiba.core.contracts import (
    COMFYUI_IMAGE_CONTAINER_MAX_IMAGES,
    COMFYUI_IMAGE_CONTAINER_MEDIA_KEY,
)
from naiba.core.media_types import media_kind_of
from naiba.storage.media import is_uploads_path

# Exact class/field pairs only; JSON-looking strings elsewhere stay untouched.
IMAGE_CONTAINER_FIELDS = {"ImageContainer": frozenset({"selected_json"})}


def is_image_container_field(node: Any, field_name: str) -> bool:
    return isinstance(node, dict) and field_name in IMAGE_CONTAINER_FIELDS.get(
        node.get("class_type") if isinstance(node.get("class_type"), str) else "", ()
    )


def container_inputs(prompt: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any], str]]:
    for node_id, node in prompt.items():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for field_name in inputs:
            if is_image_container_field(node, field_name):
                yield str(node_id), inputs, field_name


def _relative_name(value: Any, *, empty: bool = False) -> bool:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return False
    if not value:
        return empty
    normalized = value.replace("\\", "/")
    return not (
        PureWindowsPath(value).drive or normalized.startswith("/") or ":" in value
        or any(part in {".", ".."} for part in normalized.split("/"))
    )


def parse_selected(value: Any) -> list[dict[str, Any]]:
    """Parse without coercion or I/O, retaining native entries and unknown keys."""
    if not isinstance(value, str):
        raise ValueError("ImageContainer selected_json 必须是 JSON 字符串")
    try:
        entries = json.loads(value) if value else []
    except (ValueError, RecursionError) as exc:
        raise ValueError("ImageContainer selected_json 不是合法 JSON") from exc
    if not isinstance(entries, list):
        raise ValueError("ImageContainer selected_json 必须是数组")
    if len(entries) > COMFYUI_IMAGE_CONTAINER_MAX_IMAGES:
        raise ValueError(f"ImageContainer 最多选择 {COMFYUI_IMAGE_CONTAINER_MAX_IMAGES} 张图片")
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"ImageContainer 第 {index} 张图片必须是对象")
        subfolder = entry.get("subfolder")
        if not _relative_name(entry.get("name")) or not _relative_name(
            "" if subfolder is None else subfolder, empty=True
        ):
            raise ValueError(f"ImageContainer 第 {index} 张图片的 name/subfolder 不是合法相对路径")
        # The plugin uses `entry.get("type") or "input"` and treats a null
        # subfolder as empty. Preserve those native defaults in the original JSON.
        if entry.get("type") not in (None, "", "input", "output", "temp"):
            raise ValueError(f"ImageContainer 第 {index} 张图片的 type 不合法")
        if COMFYUI_IMAGE_CONTAINER_MEDIA_KEY in entry:
            media = entry[COMFYUI_IMAGE_CONTAINER_MEDIA_KEY]
            if not isinstance(media, dict) or not isinstance(media.get("path"), str) \
                    or not media["path"].strip():
                raise ValueError(f"ImageContainer 第 {index} 张图片缺少受管引用 path")
            for key in ("name", "thumb_path"):
                if key in media and not isinstance(media[key], str):
                    raise ValueError(f"ImageContainer 第 {index} 张图片的 {key} 必须是字符串")
    return entries


def validate_selected(value: Any, data_dir: Path, *,
                      max_bytes: int = MEDIA_INPUT_MAX_BYTES) -> list[dict[str, Any]]:
    entries = parse_selected(value)
    for index, entry in enumerate(entries, 1):
        media = entry.get(COMFYUI_IMAGE_CONTAINER_MEDIA_KEY)
        if media is None:
            continue
        # Both fields reach a local preview URL. An otherwise valid main image
        # must not authorize an arbitrary thumbnail path or external URL.
        for key, label in (("path", "图片"), ("thumb_path", "缩略图")):
            path = media.get(key)
            if key == "thumb_path" and not path:
                continue
            if not is_uploads_path(data_dir, path):
                raise ValueError(f"ImageContainer 第 {index} 张{label}必须是现存的应用上传文件（受管引用）")
            try:
                target = Path(path).expanduser().resolve()
                if media_kind_of(str(target)) != "image":
                    raise ValueError(f"ImageContainer 第 {index} 张{label}的文件类型必须是 image")
                if target.stat().st_size > max_bytes:
                    raise ValueError(f"ImageContainer 第 {index} 张{label}超过单文件上限 {max_bytes // (1024 * 1024)} MB")
            except OSError as exc:
                raise ValueError(f"ImageContainer 第 {index} 张{label}无法读取：{exc}") from exc
    return entries


def prepare_selected(value: Any, data_dir: Path, upload: Callable[[str], str]) -> str:
    """Convert managed picks only. The parsed copy is private to this submission."""
    entries = validate_selected(value, data_dir)
    changed = False
    for entry in entries:
        media = entry.get(COMFYUI_IMAGE_CONTAINER_MEDIA_KEY)
        if media is None:
            continue
        reference = upload(str(Path(media["path"]).expanduser().resolve()))
        if not _relative_name(reference):
            raise ValueError("ComfyUI 上传响应不是合法的图片相对路径")
        subfolder, _, name = reference.replace("\\", "/").rpartition("/")
        if not name:
            raise ValueError("ComfyUI 上传响应缺少图片 name")
        entry.update(name=name, subfolder=subfolder, type="input")
        del entry[COMFYUI_IMAGE_CONTAINER_MEDIA_KEY]
        changed = True
    return json.dumps(entries, ensure_ascii=False) if changed else value


def referenced_media_paths(prompt: dict[str, Any], overrides: Any) -> Iterator[str]:
    """Protect both frozen defaults and picks in overrides, without walking JSON."""
    values = {(node_id, field): inputs[field]
              for node_id, inputs, field in container_inputs(prompt)}
    candidates = list(values.values())
    for item in overrides if isinstance(overrides, list) else []:
        if isinstance(item, dict) and (
            str(item.get("node_id") or ""), str(item.get("field_name") or "")
        ) in values:
            candidates.append(item.get("value"))
    for value in candidates:
        try:
            entries = parse_selected(value)
        except ValueError:
            continue
        for entry in entries:
            media = entry.get(COMFYUI_IMAGE_CONTAINER_MEDIA_KEY)
            if media:
                for key in ("path", "thumb_path"):
                    if media.get(key):
                        yield media[key]
