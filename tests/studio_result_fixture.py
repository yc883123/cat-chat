"""Offline model helpers for the explicit Studio TXT output protocol."""
from __future__ import annotations

import json
from typing import Any


def latest_result_receipt(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("role") != "tool" or message.get("name") != "write_result_txt":
            continue
        try:
            run = json.loads(message["content"])
            result = run.get("result")
            if isinstance(result, str):
                result = json.loads(result)
            if run.get("success") and isinstance(result, dict) and result.get("receipt_id"):
                return result
        except (KeyError, TypeError, ValueError):
            continue
    return None


def complete_result_reply(messages: list[dict[str, Any]]) -> str | None:
    receipt = latest_result_receipt(messages)
    if receipt is None:
        return None
    return json.dumps({"type": "tool", "tool": "complete_result_txt", "arguments": {
        key: receipt[key] for key in ("receipt_id", "segments", "chars", "sha256")}}, ensure_ascii=False)


def write_result_reply(text: str, *, segment: int = 1) -> str:
    return json.dumps({"type": "tool", "tool": "write_result_txt",
                       "arguments": {"segment": segment, "text": text}}, ensure_ascii=False)


def uses_result_tools(options: dict[str, Any]) -> bool:
    return any(tool.get("name") == "write_result_txt" for tool in options.get("tools") or [])
