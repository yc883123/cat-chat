# -*- coding: utf-8 -*-
"""单模型独立参数覆盖（上下文、最大输出、温度、思考等级、视觉能力）回归测试。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from naiba.app import NaibaChatApp
from naiba.config import ConfigStore, apply_model_override
from naiba.http import AppHTTPServer, RequestHandler
from naiba.llm.profiles import resolve_profile_with_override
from naiba.paths import PathContext


def _make_config(root: Path) -> ConfigStore:
    paths = PathContext.local(root, root / "config.json")
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    cfg = ConfigStore(paths.config_path, paths=paths)
    cfg.upsert_model_profile({
        "id": "relay-1",
        "kind": "online",
        "name": "中转站",
        "base_url": "https://relay.example/v1",
        "api_key": "sk-secret-keep",
        "request_format": "openai_chat",
        "model": "default-model",
        "context_window": 128000,
        "max_output_tokens": 4096,
        "temperature": 0.5,
        "reasoning_effort": "low",
        "supports_images": False,
    })
    return cfg


class ModelOverrideConfigAndResolutionTests(unittest.TestCase):
    def test_upsert_and_resolve_per_model_overrides_without_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_config(Path(tmp))
            saved = cfg.upsert_model_profile({
                "id": "relay-1",
                "kind": "online",
                "name": "中转站",
                "base_url": "https://relay.example/v1",
                "api_key": "",
                "request_format": "openai_chat",
                "model": "default-model",
                "context_window": 128000,
                "max_output_tokens": 4096,
                "temperature": 0.5,
                "reasoning_effort": "low",
                "supports_images": False,
                "model_overrides": {
                    "deepseek-v4.1-flash": {
                        "context_window": 65536,
                        "max_output_tokens": 8192,
                        "temperature": 0.9,
                        "reasoning_effort": "high",
                        "supports_images": True,
                    },
                    "vision-auto-model": {
                        "supports_images": "auto",
                    },
                    "empty-entry": {
                        "reasoning_effort": "inherit",
                        "supports_images": None,
                    },
                },
            })
            # 空覆盖条目自动裁剪；其他模型不串味
            self.assertNotIn("empty-entry", saved["model_overrides"])
            self.assertIn("deepseek-v4.1-flash", saved["model_overrides"])

            # 1) 命中覆盖的模型全部按独立参数生效
            resolved_a = resolve_profile_with_override(cfg, "online:relay-1", "deepseek-v4.1-flash")
            self.assertEqual(resolved_a["model"], "deepseek-v4.1-flash")
            self.assertEqual(resolved_a["context_window"], 65536)
            self.assertEqual(resolved_a["context_size"], 65536)
            self.assertEqual(resolved_a["context_window_source"], "provider_config")
            self.assertEqual(resolved_a["max_output_tokens"], 8192)
            self.assertEqual(resolved_a["temperature"], 0.9)
            self.assertEqual(resolved_a["reasoning_effort"], "high")
            self.assertIs(resolved_a["supports_images_explicit"], True)
            self.assertIs(resolved_a["supports_images"], True)

            # 2) 未覆盖的其他模型完整继承供应商默认设置
            resolved_b = resolve_profile_with_override(cfg, "online:relay-1", "other-model")
            self.assertEqual(resolved_b["model"], "other-model")
            self.assertEqual(resolved_b["context_window"], 128000)
            self.assertEqual(resolved_b["max_output_tokens"], 4096)
            self.assertEqual(resolved_b["temperature"], 0.5)
            self.assertEqual(resolved_b["reasoning_effort"], "low")
            self.assertIs(resolved_b["supports_images_explicit"], False)
            self.assertIs(resolved_b["supports_images"], False)

            # 3) 单模型 supports_images="auto" 清除供应商强制 false，改走自动推断
            resolved_auto = resolve_profile_with_override(cfg, "online:relay-1", "gemini-2.5-pro")
            # 先设置 gemini-2.5-pro 为 auto，验证可自动识别为支持图片
            cfg.set_provider_model_override("relay-1", "online", "gemini-2.5-pro", {"supports_images": "auto"})
            resolved_auto = resolve_profile_with_override(cfg, "online:relay-1", "gemini-2.5-pro")
            self.assertIsNone(resolved_auto["supports_images_explicit"])
            self.assertIs(resolved_auto["supports_images"], True)

    def test_upsert_without_model_overrides_key_preserves_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_config(Path(tmp))
            cfg.set_provider_model_override("relay-1", "online", "m1", {"context_window": 32768, "reasoning_effort": "off"})
            # 旧客户端/部分表单不带 model_overrides 键时不得误清已有覆盖
            updated = cfg.upsert_model_profile({
                "id": "relay-1",
                "kind": "online",
                "name": "中转站改名",
                "base_url": "https://relay.example/v1",
                "api_key": "",
                "request_format": "openai_chat",
                "model": "default-model",
            })
            self.assertEqual(updated["model_overrides"], {"m1": {"context_window": 32768, "reasoning_effort": "off"}})
            # 显式保存并保留原 API Key
            raw_profile = cfg.profile("online:relay-1")
            self.assertEqual(raw_profile["api_key"], "sk-secret-keep")

    def test_set_provider_model_override_updates_and_resets_without_touching_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_config(Path(tmp))
            res = cfg.set_provider_model_override(
                "relay-1", "online", "qwen3.8-max",
                {"context_window": 262144, "reasoning_effort": "medium", "supports_images": False},
            )
            self.assertEqual(res["model_key"], "online:relay-1")
            self.assertEqual(res["override"]["context_window"], 262144)
            self.assertEqual(cfg.profile("online:relay-1")["api_key"], "sk-secret-keep")

            # 清空/恢复默认
            cleared = cfg.set_provider_model_override("relay-1", "online", "qwen3.8-max", None)
            self.assertEqual(cleared["override"], {})
            self.assertEqual(cleared["model_overrides"], {})
            self.assertEqual(cfg.profile("online:relay-1")["api_key"], "sk-secret-keep")

    def test_validation_rejects_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_config(Path(tmp))
            with self.assertRaises(ValueError):
                cfg.set_provider_model_override("relay-1", "online", "m1", {"context_window": -10})
            with self.assertRaises(ValueError):
                cfg.set_provider_model_override("relay-1", "online", "m1", {"temperature": 3.5})
            with self.assertRaises(ValueError):
                cfg.set_provider_model_override("relay-1", "online", "m1", {"reasoning_effort": "ultra"})
            with self.assertRaises(ValueError):
                cfg.set_provider_model_override("relay-1", "online", "m1", {"supports_images": "maybe"})
            with self.assertRaises(LookupError):
                cfg.set_provider_model_override("no-such-id", "online", "m1", {"context_window": 8192})

    def test_local_per_model_context_window_skips_local_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = PathContext.local(root, root / "config.json")
            (root / "data").mkdir(parents=True, exist_ok=True)
            cfg = ConfigStore(paths.config_path, paths=paths)
            cfg.upsert_model_profile({
                "id": "ollama-1",
                "kind": "local",
                "name": "本地 Ollama",
                "base_url": "http://127.0.0.1:11434",
                "request_format": "ollama",
                "model": "qwen3",
                "model_overrides": {
                    "qwen3-custom": {"context_window": 49152},
                },
            })
            with patch("naiba.llm.profiles.probe_local_context_window", side_effect=AssertionError("不应探测")) as mock_probe:
                resolved = resolve_profile_with_override(cfg, "local:ollama-1", "qwen3-custom")
                self.assertEqual(resolved["context_window"], 49152)
                self.assertEqual(resolved["context_window_source"], "local_config")
                mock_probe.assert_not_called()

    def test_apply_model_override_does_not_mutate_input(self) -> None:
        base = {
            "kind": "online",
            "model": "m0",
            "context_window": 64000,
            "reasoning_effort": "auto",
            "supports_images_explicit": None,
            "model_overrides": {"m1": {"context_window": 131072, "reasoning_effort": "high"}},
        }
        out = apply_model_override(base, "m1")
        self.assertEqual(out["context_window"], 131072)
        self.assertEqual(out["reasoning_effort"], "high")
        self.assertEqual(base["context_window"], 64000)
        self.assertEqual(base["reasoning_effort"], "auto")


class ModelOverrideHttpTests(unittest.TestCase):
    def test_http_endpoint_saves_and_clears_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = PathContext.local(root, root / "config.json")
            (root / "data").mkdir(parents=True, exist_ok=True)
            (root / "workspace").mkdir(parents=True, exist_ok=True)
            cfg = ConfigStore(paths.config_path, paths=paths)
            cfg.data.update({
                "host": "127.0.0.1",
                "port": 0,
                "access_token": "",
                "data_dir": str(root / "data"),
                "workspace_dir": str(root / "workspace"),
                "skills_dirs": [],
                "mcp_servers": [],
            })
            cfg.upsert_model_profile({
                "id": "p-http",
                "kind": "online",
                "name": "HTTP 测试",
                "base_url": "https://example.invalid/v1",
                "api_key": "sk-orig",
                "request_format": "openai_chat",
                "model": "m-default",
            })
            app = NaibaChatApp(paths=paths)
            server = AppHTTPServer(("127.0.0.1", 0), RequestHandler, app)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                def post(body: dict) -> tuple[int, dict]:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/providers/model-override",
                        data=json.dumps(body).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            return resp.status, json.loads(resp.read().decode("utf-8"))
                    except urllib.error.HTTPError as err:
                        return err.code, json.loads(err.read().decode("utf-8"))

                status, data = post({
                    "id": "p-http",
                    "kind": "online",
                    "model_name": "m-special",
                    "override": {
                        "context_window": 131072,
                        "max_output_tokens": 16384,
                        "temperature": 0.4,
                        "reasoning_effort": "high",
                        "supports_images": True,
                    },
                })
                self.assertEqual(status, 200)
                self.assertEqual(data["override"]["context_window"], 131072)
                self.assertEqual(data["override"]["reasoning_effort"], "high")
                self.assertIs(data["override"]["supports_images"], True)

                # 非法参数返回 400
                bad_status, _ = post({
                    "id": "p-http",
                    "kind": "online",
                    "model_name": "m-special",
                    "override": {"reasoning_effort": "invalid"},
                })
                self.assertEqual(bad_status, 400)

                # 清空返回 200 且恢复默认
                clear_status, cleared = post({
                    "id": "p-http",
                    "kind": "online",
                    "model_name": "m-special",
                    "override": None,
                })
                self.assertEqual(clear_status, 200)
                self.assertEqual(cleared["model_overrides"], {})
            finally:
                server.shutdown()
                server.server_close()
                app.stop()


if __name__ == "__main__":
    unittest.main()
