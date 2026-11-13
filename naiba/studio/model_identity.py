"""Credential-free identity of the configured model used by a generation."""
from __future__ import annotations

import hashlib
import json
from typing import Any

MODEL_IDENTITY_KEYS = frozenset({
    "model_key", "model_name", "provider_id", "kind", "base_url_hash", "request_format",
})


def configured_identity(input_data: dict[str, Any], profile: dict[str, Any]) -> dict[str, str]:
    return {
        "model_key": str(input_data.get("model_key") or ""),
        "model_name": str(input_data.get("model_name") or ""),
        "provider_id": str(profile.get("provider_id") or profile.get("provider") or ""),
        "kind": str(profile.get("kind") or ""),
        "request_format": str(profile.get("request_format") or "openai_chat").strip().lower(),
        "base_url_hash": hashlib.sha256(json.dumps(str(profile.get("base_url") or ""),
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
    }


def check_resume_identity(input_data: dict[str, Any], profile: dict[str, Any]) -> None:
    snapshot = input_data.get("model_snapshot")
    if (not isinstance(snapshot, dict) or not MODEL_IDENTITY_KEYS.issubset(snapshot)
            or any(not isinstance(snapshot[key], str) for key in MODEL_IDENTITY_KEYS)):
        raise ValueError("此旧任务未冻结完整模型身份，无法核验TXT接续；请重新生成")
    current = configured_identity(input_data, profile)
    if any(snapshot[key] != current[key] for key in MODEL_IDENTITY_KEYS):
        raise ValueError("模型配置已改变，不能把新模型接到旧TXT；请基于当前版本重新生成")
