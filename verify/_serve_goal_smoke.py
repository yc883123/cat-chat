"""GOAL browser fixture. All data and model responses stay in a temporary root."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from naiba import net as net_io
from naiba.config import ConfigStore
from naiba.http import main_entry
from naiba.paths import PathContext


class Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def read(self, *args):
        result, self.body = self.body, b""
        return result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def close(self):
        pass


def fake_open(request, **kwargs):
    if request.full_url == "http://127.0.0.1:1/v1/models":
        return Response({"data": [{"id": "goal-fixture", "context_window": 32000}]})
    if request.full_url != "http://127.0.0.1:1/v1/chat/completions":
        raise AssertionError("Fixture refuses network access")
    body = json.loads(request.data)
    content = "隔离目标已交付，请核对结果。\nGOAL: completed" if "GOAL 模式" in str(body["messages"][0]) else "普通聊天回复。"
    return Response({"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 120, "completion_tokens": 20}})


root = Path(os.environ["NAIBA_SMOKE_ROOT"]).resolve()
root.mkdir(parents=True, exist_ok=True)
paths = PathContext.local(root, root / "config.json")
paths.public_dir = REPO / "public"
paths.resource_dir = REPO
config = ConfigStore(paths.config_path, paths=paths)
workspace = root / "workspace"
workspace.mkdir(exist_ok=True)
config.data.update({"workspace_dir": str(workspace), "mcp_servers": [],
    "new_conversation_model_key": "online:goal-fixture", "new_conversation_model_name": "goal-fixture",
    "default_model_key": "online:goal-fixture", "auto_compaction": {"mode": "off"},
    "providers": [{"id": "goal-fixture", "kind": "online", "name": "隔离假模型", "api_key": "",
        "base_url": "http://127.0.0.1:1/v1", "request_format": "openai_chat", "model": "goal-fixture",
        "models": ["goal-fixture"], "context_window": 32000, "max_output_tokens": 2048}]})
config.save()


def ready(app, _paths):
    cid = app.storage.create_conversation("GOAL 隔离验收", model_key="online:goal-fixture", model_name="goal-fixture",
        permission_mode="full", workspace_dir=str(workspace), reasoning_effort="off")["id"]
    app.storage.set_enabled_tool_ids(cid, ["read_file", "write_file", "edit_file"])
    app.storage.set_conversation_chat_supports_images(cid, False)
    app.storage.update_conversation_settings(cid, stream_enabled=False)
    print(f"GOAL fixture ready: {cid}", flush=True)


with patch.object(net_io, "open", side_effect=fake_open):
    main_entry(paths, on_app=ready)
