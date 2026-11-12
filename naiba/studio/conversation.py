"""Studio conversation snapshots: model history with managed image references."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from naiba.core.contracts import STUDIO_HISTORY_MESSAGES_KEY, STUDIO_TRACE_MESSAGES_KEY
from naiba.core.history import build_model_history, _copy_model_trace_message
from naiba.core.messages import MetadataKeys
from naiba.studio.images import validate_image_refs


def user_message(input_data: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "user", "content": str(input_data.get("content") or "")}
    if input_data.get("images"):
        message["images"] = copy.deepcopy(input_data["images"])
    return message


def completed_history(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Only completed answers become context; core/history owns final-answer dedup."""
    if task.get("status") != "done":
        raise ValueError("只能对已完成的结果追问；失败或取消的内容请先重试")
    data = task.get("input") or {}
    history = copy.deepcopy(data.get(STUDIO_HISTORY_MESSAGES_KEY) or [])
    history.append(user_message(data))
    history.extend(build_model_history([{
        "role": "assistant", "content": str(task.get("result") or ""),
        "metadata": {MetadataKeys.TRACE: task.get(STUDIO_TRACE_MESSAGES_KEY) or [],
                     MetadataKeys.REASONING: task.get("reasoning") or []},
    }], reasoning_replay_max_chars=0, reasoning_replay_turn_chars=0))
    return history


def model_history(input_data: dict[str, Any], profile: dict[str, Any], data_dir: Path | None) -> list[dict[str, Any]]:
    """Encode image bytes only at execution; never put data URLs in task JSON."""
    from naiba.vision.images import encode_image_file

    history = copy.deepcopy(input_data.get(STUDIO_HISTORY_MESSAGES_KEY) or [])
    if not isinstance(history, list):
        raise ValueError("任务历史损坏")
    history.append(user_message(input_data))
    messages = []
    for item in history:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant", "tool"}:
            raise ValueError("任务历史消息非法")
        message = _copy_model_trace_message(item)
        refs = item.get("images") or []
        if refs:
            if item.get("role") != "user":
                raise ValueError("图片只能属于用户消息")
            if data_dir is not None:
                refs = validate_image_refs(data_dir, refs)
            explicit = profile.get("supports_images_explicit")
            if explicit is None and "supports_images_explicit" not in profile:
                inferred = profile.get("supports_images")
                if isinstance(inferred, bool):
                    explicit = inferred
            if explicit is False:
                raise RuntimeError("当前模型不支持图片输入：请换一个支持视觉的模型，或先识别图片后再生成")
            parts: list[dict[str, Any]] = []
            for index, ref in enumerate(refs, 1):
                part = encode_image_file(str(ref.get("path") or ""))
                if part is None:
                    raise ValueError(f"历史图{index}无法读取（文件可能已被清理），请重新上传后新建生成")
                label = str(ref.get("name") or "").strip() or f"图{index}"
                parts.extend([{"type": "text", "text": f"\n\n图{index}（{label}）："}, part])
            parts.append({"type": "text", "text": "\n\n" + str(item.get("content") or "")})
            message["content"] = parts
        messages.append(message)
    return messages


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    """Keep trace in authoritative storage, not in API responses/events."""
    result = copy.deepcopy(task)
    result.pop(STUDIO_TRACE_MESSAGES_KEY, None)
    data = result.get("input")
    if isinstance(data, dict):
        # The readable conversation remains available, but native tools/reasoning
        # are server-only. Do not expose a second copy of reference documents.
        data[STUDIO_HISTORY_MESSAGES_KEY] = [
            {key: copy.deepcopy(item[key]) for key in ("role", "content", "images") if key in item}
            for item in data.get(STUDIO_HISTORY_MESSAGES_KEY, [])
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
            and not item.get("tool_calls")
        ]
    return result
