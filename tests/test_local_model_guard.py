# -*- coding: utf-8 -*-
"""守门：本地模型「上下文窗口 / 闸门口径 / 图片总量 / 等锁可取消」四道闸。

保护对象（背景：一台客户机用本地 unsloth 学习 16 小时后，界面永久停在
「等待本地模型资源」，重启后端乃至重启电脑都无效 —— 病根是每轮构造的请求体远超
本地真实窗口，而唯一能拦住它的闸门系统性低估）：

1. `naiba/llm/local_probe.py` —— 本地后端窗口真值探测（llama.cpp /props、
   Ollama /api/show num_ctx、LM Studio /api/v0/models），失败返回 0 不上抛；
2. `naiba/skills/context.py::fallback_context_window` —— 窗口未知时本地用保守值，
   **不能**沿用在线 256k；
3. `SkillAgent._context_budget / _context_fits / _replay_footprint_tokens` ——
   预算必须计入 reasoning_content、tool_calls、role=tool 结果与原生工具 schema；
4. `VisionRouter._cap_local_history_images` —— 本地多模态大脑的每请求图片总量上限；
5. `ModelRuntime._acquire_local_lock` —— 等本地锁可取消、且周期性回报等待时长。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.llm import local_probe  # noqa: E402
from naiba.llm import runtime as llm_runtime  # noqa: E402
from naiba.skills.agent import SkillAgent  # noqa: E402
from naiba.skills.context import (  # noqa: E402
    DEFAULT_CONTEXT_WINDOW,
    LOCAL_DEFAULT_CONTEXT_WINDOW,
    fallback_context_window,
)
from naiba.vision.runtime import VisionRouter  # noqa: E402

LOCAL_PROFILE = {"kind": "local", "model": "qwen3-vl", "request_format": "llama_cpp"}
ONLINE_PROFILE = {"kind": "online", "model": "deepseek-v4-flash", "request_format": "openai_chat"}


class _FakeResponse:
    def __init__(self, payload):
        import json as _json

        self._raw = _json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class LocalProbeTests(unittest.TestCase):
    """本地后端窗口探测：取运行期真值，失败不抛、不缓存失败结果。"""

    def setUp(self) -> None:
        local_probe._clear_cache()

    def test_llama_cpp_props_n_ctx(self) -> None:
        profile = {**LOCAL_PROFILE, "base_url": "http://127.0.0.1:8080/v1"}
        seen: list[str] = []

        def fake_open(request, timeout=None, **_kw):
            seen.append(request.full_url if hasattr(request, "full_url") else str(request))
            return _FakeResponse({"default_generation_settings": {"n_ctx": 32768}})

        with mock.patch.object(local_probe.net_io, "open", fake_open):
            self.assertEqual(local_probe.probe_local_context_window(profile), 32768)
            # 记忆化：第二次不再打网络
            self.assertEqual(local_probe.probe_local_context_window(profile), 32768)
        self.assertEqual(len(seen), 1, "同一 (base_url, model) 只允许探测一次")
        self.assertEqual(seen[0], "http://127.0.0.1:8080/props", "base_url 带 /v1 时 /props 落在根上")

    def test_failure_returns_zero_and_is_not_cached(self) -> None:
        profile = {**LOCAL_PROFILE, "base_url": "http://127.0.0.1:8080"}
        attempts: list[int] = []

        def boom(request, timeout=None, **_kw):
            attempts.append(1)
            raise OSError("connection refused")

        with mock.patch.object(local_probe.net_io, "open", boom):
            self.assertEqual(local_probe.probe_local_context_window(profile), 0)
            self.assertEqual(local_probe.probe_local_context_window(profile), 0)
        self.assertEqual(len(attempts), 2, "失败结果不得记忆化（本地服务起来后要能重新探到）")

    def test_ollama_reads_explicit_num_ctx_only(self) -> None:
        profile = {
            "kind": "local",
            "model": "qwen3:8b",
            "request_format": "ollama",
            "base_url": "http://127.0.0.1:11434",
        }
        with mock.patch.object(
            local_probe.net_io,
            "open",
            lambda request, timeout=None, **_kw: _FakeResponse({"parameters": "num_ctx 8192\n"}),
        ):
            self.assertEqual(local_probe.probe_local_context_window(profile), 8192)
        local_probe._clear_cache()
        with mock.patch.object(
            local_probe.net_io,
            "open",
            lambda request, timeout=None, **_kw: _FakeResponse(
                {"model_info": {"llama.context_length": 131072}}
            ),
        ):
            self.assertEqual(
                local_probe.probe_local_context_window(profile),
                0,
                "不取训练长度（会重新引入「实质不设上限」）",
            )

    def test_ignores_non_local_profiles(self) -> None:
        self.assertEqual(local_probe.probe_local_context_window(ONLINE_PROFILE), 0)


class FallbackWindowTests(unittest.TestCase):
    def test_local_and_online_use_different_fallbacks(self) -> None:
        self.assertEqual(fallback_context_window(LOCAL_PROFILE), LOCAL_DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(fallback_context_window(ONLINE_PROFILE), DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(
            fallback_context_window(None),
            DEFAULT_CONTEXT_WINDOW,
            "拿不到 profile 时保持旧行为",
        )


class ContextGateTests(unittest.TestCase):
    """闸门：预算按 kind 取兜底值；计数覆盖请求体里全部每轮重发的载荷。"""

    def test_local_window_is_not_the_online_default(self) -> None:
        limit, budget = SkillAgent._context_budget(LOCAL_PROFILE, {}, "")
        self.assertEqual(limit, LOCAL_DEFAULT_CONTEXT_WINDOW)
        self.assertLess(budget, DEFAULT_CONTEXT_WINDOW - 200000)
        online_limit, _ = SkillAgent._context_budget(ONLINE_PROFILE, {}, "")
        self.assertEqual(online_limit, DEFAULT_CONTEXT_WINDOW)

    def test_explicit_window_always_wins(self) -> None:
        limit, _ = SkillAgent._context_budget({**LOCAL_PROFILE, "context_window": 65536}, {}, "")
        self.assertEqual(limit, 65536)

    def test_history_oversized_for_local_is_blocked(self) -> None:
        history = [{"role": "user", "content": "a" * 200000}]
        fits, limit, used, _ = SkillAgent._context_fits(history, LOCAL_PROFILE, {}, "")
        self.assertFalse(fits, "20 万字符（约 5 万 token）必须被本地窗口拦住")
        self.assertEqual(limit, LOCAL_DEFAULT_CONTEXT_WINDOW)
        self.assertGreater(used, 40000)
        fits_online, _, _, _ = SkillAgent._context_fits(history, ONLINE_PROFILE, {}, "")
        self.assertTrue(fits_online, "同样的历史对在线模型仍在预算内")

    def test_reasoning_content_is_counted(self) -> None:
        history = [{"role": "assistant", "content": "结论", "reasoning_content": "r" * 400000}]
        fits, _limit, used, _budget = SkillAgent._context_fits(history, LOCAL_PROFILE, {}, "")
        self.assertFalse(fits, "思维链每轮随请求重发，漏算它等于闸门放行超大请求")
        self.assertGreater(used, 90000, f"reasoning 必须计入 used，实际 {used}")

    def test_tool_calls_and_tool_results_are_counted(self) -> None:
        calls = [{"id": "c1", "function": {"name": "read_file", "arguments": "x" * 200000}}]
        history = [
            {"role": "assistant", "content": "", "tool_calls": calls},
            {"role": "tool", "tool_call_id": "c1", "content": "y" * 200000},
        ]
        fits, _limit, used, _budget = SkillAgent._context_fits(history, LOCAL_PROFILE, {}, "")
        self.assertFalse(fits)
        self.assertGreater(used, 90000, f"tool_calls 与 tool 结果都要计入，实际 {used}")

    def test_tool_schema_is_reserved(self) -> None:
        tools = [{"type": "function", "function": {"name": "t", "description": "d" * 300000}}]
        options = {"tools": tools}
        _limit, budget = SkillAgent._context_budget(LOCAL_PROFILE, options, "")
        _limit_plain, budget_plain = SkillAgent._context_budget(LOCAL_PROFILE, {}, "")
        self.assertLess(budget, budget_plain - 20000, "工具 schema 必须从预算里预留掉")
        # schema 挤占的是预算（不是 used）：同一段历史在无工具时仍然装得下，有工具就被拦。
        history = [{"role": "user", "content": "a" * 90000}]
        plain_fits, _, _, _ = SkillAgent._context_fits(history, LOCAL_PROFILE, {}, "")
        tools_fits, _, _, _ = SkillAgent._context_fits(history, LOCAL_PROFILE, options, "")
        self.assertTrue(plain_fits, "无工具 schema 时该历史仍在预算内")
        self.assertFalse(tools_fits, "工具 schema 必须挤占预算，否则闸门仍是低估")


class _ConfigStub:
    def __init__(self, vision: dict):
        self.data = {"vision": vision}


class _AppStub:
    def __init__(self, vision: dict):
        self.config = _ConfigStub(vision)


def _image_part(name: str, payload: str = "YWJj") -> dict:
    return {"type": "image", "media_type": "image/jpeg", "data": payload, "name": name}


def _history_with_images(count: int) -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": f"第 {i} 张"}, _image_part(f"{i}.png")]}
        for i in range(count)
    ]


class ProfileProbeWiringTests(unittest.TestCase):
    """接线：会话解析 profile 时把探测到的本地窗口写进去，显式配置不被覆盖。"""

    def setUp(self) -> None:
        import json
        import tempfile

        local_probe._clear_cache()
        from server import ConfigStore

        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "default_model_key": "local:l1",
                    "providers": [
                        {
                            "id": "l1",
                            "kind": "local",
                            "local_backend": "unsloth",
                            "request_format": "unsloth",
                            "base_url": "http://127.0.0.1:8080/v1",
                            "model": "qwen3-vl",
                            "api_key": "",
                        },
                        {
                            "id": "o1",
                            "kind": "online",
                            "request_format": "openai_chat",
                            "base_url": "https://api.deepseek.com",
                            "model": "deepseek-v4-flash",
                            "api_key": "sk-test",
                            "context_window": 131072,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.config = ConfigStore(path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_local_profile_gets_probed_window(self) -> None:
        from naiba.run.chat import _profile_with_model_override

        with mock.patch.object(
            local_probe.net_io,
            "open",
            lambda request, timeout=None, **_kw: _FakeResponse(
                {"default_generation_settings": {"n_ctx": 24576}}
            ),
        ):
            profile = _profile_with_model_override(self.config, "local:l1", "qwen3-vl")
        self.assertEqual(profile["context_window"], 24576)
        self.assertEqual(profile["context_window_source"], "local_probe")
        self.assertEqual(profile["model"], "qwen3-vl")
        # 闸门与前端圆环随后都读这个字段，必须同源
        self.assertEqual(
            SkillAgent._context_budget(profile, {}, "")[0], 24576
        )

    def test_local_profile_falls_back_conservatively_without_backend(self) -> None:
        from naiba.run.chat import _profile_with_model_override

        def boom(request, timeout=None, **_kw):
            raise OSError("connection refused")

        with mock.patch.object(local_probe.net_io, "open", boom):
            profile = _profile_with_model_override(self.config, "local:l1", "qwen3-vl")
        self.assertFalse(profile.get("context_window"))
        self.assertEqual(
            SkillAgent._context_budget(profile, {}, "")[0], LOCAL_DEFAULT_CONTEXT_WINDOW
        )

    def test_explicit_window_is_never_overwritten(self) -> None:
        from naiba.run.chat import _profile_with_model_override

        calls: list[int] = []

        def spy(request, timeout=None, **_kw):
            calls.append(1)
            return _FakeResponse({"default_generation_settings": {"n_ctx": 4096}})

        with mock.patch.object(local_probe.net_io, "open", spy):
            profile = _profile_with_model_override(self.config, "online:o1", "deepseek-v4-flash")
        self.assertEqual(profile["context_window"], 131072)
        self.assertEqual(calls, [], "显式配置了窗口的 provider 不应触发探测")


class LocalImageCapTests(unittest.TestCase):
    """本地多模态大脑：每请求图片总量上限；在线模型与文本模型路径不受影响。"""

    def setUp(self) -> None:
        self.router = VisionRouter(_AppStub({
            "provider_model_key": "",
            "timeout_ms": 180000,
            "max_images": 4,
            "cache": True,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 200,
        }))

    def test_keeps_most_recent_images_and_placeholder_rest(self) -> None:
        limit = VisionRouter.LOCAL_REQUEST_IMAGE_LIMIT
        history = _history_with_images(limit + 5)
        capped, note = self.router._cap_local_history_images(history)
        remaining = [
            part
            for item in capped
            for part in item["content"]
            if isinstance(part, dict) and part.get("type") == "image"
        ]
        self.assertEqual(len(remaining), limit)
        self.assertIn(f"已省略 5 张", note)
        dropped_names = [
            part.get("name")
            for item in capped
            for part in item["content"]
            if isinstance(part, dict) and part.get("type") == "image"
        ]
        self.assertEqual(dropped_names, [f"{i}.png" for i in range(5, limit + 5)],
                         "保留的必须是按位置最近的那批")
        first_text = "".join(
            str(part.get("text") or "")
            for part in capped[0]["content"]
            if isinstance(part, dict) and part.get("type") == "text"
        )
        self.assertIn("0.png", first_text, "被省略的图片要留下列文件名，便于 vision_analyze 按路径取回")
        self.assertIn("vision_analyze", first_text)

    def test_byte_budget_caps_payload(self) -> None:
        # 每张 3MB（base64 约 4MB 字符），字节上限 8MB ⇒ 最多留 2 张
        big = "A" * (4 * 1024 * 1024)
        history = _history_with_images(4)
        for item in history:
            item["content"][1]["data"] = big
        capped, note = self.router._cap_local_history_images(history)
        remaining = sum(
            1
            for item in capped
            for part in item["content"]
            if isinstance(part, dict) and part.get("type") == "image"
        )
        self.assertEqual(remaining, 2, f"字节上限应把图片压到 2 张，实际 {remaining}")
        self.assertIn("已省略", note)

    def test_small_history_is_untouched(self) -> None:
        history = _history_with_images(2)
        capped, note = self.router._cap_local_history_images(history)
        self.assertEqual(note, "")
        self.assertIs(capped, history)

    def test_prepare_history_applies_cap_only_to_local_multimodal(self) -> None:
        limit = VisionRouter.LOCAL_REQUEST_IMAGE_LIMIT
        history = _history_with_images(limit + 3)
        local_profile = {**LOCAL_PROFILE, "supports_images": True}
        capped, note = self.router.prepare_history(history, local_profile)
        self.assertIn("本地模型单次请求图片上限", note)
        self.assertEqual(
            sum(
                1
                for item in capped
                for part in item["content"]
                if isinstance(part, dict) and part.get("type") == "image"
            ),
            limit,
        )
        online_profile = {**ONLINE_PROFILE, "supports_images": True}
        untouched, online_note = self.router.prepare_history(_history_with_images(limit + 3), online_profile)
        self.assertEqual(online_note, "", "在线模型不动（保住 1.6.0 的前缀缓存契约）")
        self.assertEqual(
            sum(
                1
                for item in untouched
                for part in item["content"]
                if isinstance(part, dict) and part.get("type") == "image"
            ),
            limit + 3,
        )


class LocalLockWaitTests(unittest.TestCase):
    """等本地锁：可取消 + 周期性回报等待时长（「停止」按钮必须真的有效）。"""

    def test_acquires_when_free(self) -> None:
        lock = threading.RLock()
        llm_runtime.ModelRuntime._acquire_local_lock(lock, {}, None)
        lock.release()

    def test_cancel_during_wait_raises(self) -> None:
        lock = threading.RLock()
        started = threading.Event()

        def holder() -> None:
            lock.acquire()
            started.set()
            time.sleep(0.9)
            lock.release()

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(started.wait(2))
        cancel_event = threading.Event()
        cancel_event.set()
        with self.assertRaises(RuntimeError) as ctx:
            llm_runtime.ModelRuntime._acquire_local_lock(lock, {"cancel_event": cancel_event}, None)
        self.assertIn("取消", str(ctx.exception))
        thread.join(2)

    def test_reports_wait_notice_periodically(self) -> None:
        lock = threading.RLock()
        started = threading.Event()

        def holder() -> None:
            lock.acquire()
            started.set()
            time.sleep(1.4)
            lock.release()

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(started.wait(2))
        messages: list[str] = []
        with mock.patch.object(llm_runtime, "LOCAL_LOCK_WAIT_NOTICE_SECONDS", 0.4):
            llm_runtime.ModelRuntime._acquire_local_lock(
                lock, {}, lambda payload: messages.append(str(payload.get("message") or ""))
            )
        lock.release()
        thread.join(2)
        self.assertTrue(messages, "排队等待必须周期性发状态，否则界面无法区分「卡住」与「在排队」")
        self.assertIn("本地模型忙", messages[0])


if __name__ == "__main__":
    unittest.main()
