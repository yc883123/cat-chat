# -*- coding: utf-8 -*-
"""Q1 守门：本地模型「上下文溢出」必须是**用户能看懂且能行动**的报错。

改动前三个真实缺口（见 `计划-Q1-本地模型上下文溢出报错.md`）：

1. Ollama 把错误整块塞在流里的 `{"error": ...}` 字段**从未被读取**——后端说「超出上下文
   长度」，界面上只剩「流式响应中没有文本内容」；
2. llama.cpp / Unsloth 的 400 `exceed_context_size_error` 原文透传，用户看到的是
   `请求失败：HTTP 400: {原始 JSON}`；
3. `finish_reason="length"` 一律按「撞输出上限」处理（自动续写一次），无法区分
   「撞输出上限」与「窗口耗尽」——后者续写只会让请求更大。

四层各自守一段：流解析（B）→ 错误识别与文案（A）→ 循环内复查（C）→ 成因区分（D）。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.llm.runtime import (  # noqa: E402
    ModelRuntime,
    _window_from_error_detail,
    context_overflow_message,
)
from naiba.llm.stream import (  # noqa: E402
    ContextOverflowError,
    StreamMixins,
    is_context_overflow,
)
from naiba.skills.agent import SkillAgent  # noqa: E402
from naiba.skills import agent as agent_module  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# 各后端的真实报错原文（照抄实测响应体）。
LLAMA_CPP_400 = json.dumps({
    "error": {
        "code": 400,
        "message": (
            "the request exceeds the available context size. "
            "try increasing the n_ctx option or use a larger context size model"
        ),
        "type": "exceed_context_size_error",
        "n_prompt_tokens": 5120,
        "n_ctx": 4096,
    },
})
LM_STUDIO_STREAM_ERROR = {
    "type": "error",
    "error": {
        "message": (
            "The number of tokens to keep from the initial prompt is greater than "
            "the context length (n_keep: 0, n_ctx: 8192)"
        ),
    },
}
OLLAMA_CONTEXT_ERROR = {
    "error": "the input length exceeds the context length (n_ctx: 2048)",
}


def sse(chunk: dict) -> bytes:
    return ("data: " + json.dumps(chunk, ensure_ascii=False)).encode("utf-8")


def http_error(code: int, body: str = "", reason: str = "Bad Request") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://127.0.0.1:1234/v1/chat/completions",
        code,
        reason,
        {"Content-Type": "application/json"},
        io.BytesIO(body.encode("utf-8")),
    )


class FakeResponse:
    """非流式路径替身（只用到 read()/headers）。"""

    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        return None

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class FakeStreamResponse:
    """流式路径替身（按行迭代 bytes）。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self.headers = {"Content-Type": "text/event-stream"}

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        return None

    def __enter__(self) -> "FakeStreamResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


# ---------------------------------------------------------------------------
# B：流里内嵌的错误不再被吞
# ---------------------------------------------------------------------------


class StreamErrorCaptureTests(unittest.TestCase):
    def test_ollama_error_chunk_is_surfaced(self) -> None:
        """Ollama 的 error 字段此前整块被吞，用户只看到「没有文本内容」。"""
        response = [json.dumps({"error": "model 'qwen' not found"}).encode("utf-8")]
        with self.assertRaises(RuntimeError) as ctx:
            StreamMixins._read_ollama_stream(response, None)
        self.assertIn("Ollama 流式错误", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, ContextOverflowError)

    def test_ollama_context_error_is_overflow_typed(self) -> None:
        response = [json.dumps(OLLAMA_CONTEXT_ERROR).encode("utf-8")]
        with self.assertRaises(ContextOverflowError) as ctx:
            StreamMixins._read_ollama_stream(response, None)
        self.assertIn("context length", ctx.exception.detail)

    def test_ollama_normal_stream_unaffected(self) -> None:
        response = [
            json.dumps({"message": {"content": "你好"}, "error": None}).encode("utf-8"),
            json.dumps({"message": {"content": "。"}}).encode("utf-8"),
        ]
        result = StreamMixins._read_ollama_stream(response, None)
        self.assertEqual(result["content"], "你好。")

    def test_sse_embedded_error_is_typed(self) -> None:
        """llama.cpp / Unsloth 有些版本把错误嵌在流里，而不是返回 4xx。"""
        response = [sse({"error": json.loads(LLAMA_CPP_400)["error"]})]
        with self.assertRaises(ContextOverflowError) as ctx:
            StreamMixins._read_sse_response(response, "llama_cpp", None)
        self.assertEqual(ctx.exception.backend, "llama.cpp")

    def test_sse_normal_events_not_mistaken_for_errors(self) -> None:
        """`error: null` / 无 error 键的正常事件不得被当成错误（codex 事件照旧）。"""
        response = [
            sse({"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1"}}),
            sse({"type": "response.output_text.delta", "delta": "回答", "error": None}),
            sse({"type": "response.completed", "response": {"output": []}}),
        ]
        result = StreamMixins._read_sse_response(response, "codex_responses", None)
        self.assertEqual(result["content"], "回答")

    def test_sse_choices_with_null_error_unaffected(self) -> None:
        response = [sse({"choices": [{"delta": {"content": "答复"}}], "error": None})]
        result = StreamMixins._read_sse_response(response, "openai_chat", None)
        self.assertEqual(result["content"], "答复")

    def test_lm_studio_error_event_is_typed(self) -> None:
        response = [sse(LM_STUDIO_STREAM_ERROR)]
        with self.assertRaises(ContextOverflowError) as ctx:
            StreamMixins._read_lm_studio_stream(response, None)
        self.assertEqual(ctx.exception.backend, "LM Studio")

    def test_lm_studio_plain_error_keeps_readable_text(self) -> None:
        response = [sse({"type": "error", "error": {"message": "model unloaded"}})]
        with self.assertRaises(RuntimeError) as ctx:
            StreamMixins._read_lm_studio_stream(response, None)
        self.assertIn("model unloaded", str(ctx.exception))


# ---------------------------------------------------------------------------
# A：特征串判定 / 窗口解析 / 文案
# ---------------------------------------------------------------------------


class OverflowClassificationTests(unittest.TestCase):
    def test_markers_hit(self) -> None:
        for detail in (
            LLAMA_CPP_400,
            "The number of tokens ... is greater than the context length (n_ctx: 8192)",
            "This model's maximum context length is 65536 tokens.",
            "prompt is too long: 210000 tokens > 200000 maximum",
            OLLAMA_CONTEXT_ERROR["error"],
        ):
            self.assertTrue(is_context_overflow(detail), detail)

    def test_markers_miss(self) -> None:
        for detail in ("Invalid API key", "model not found", "Bad Request", ""):
            self.assertFalse(is_context_overflow(detail), detail)

    def test_window_is_read_from_backend_body(self) -> None:
        self.assertEqual(_window_from_error_detail(LLAMA_CPP_400), 4096)
        self.assertEqual(_window_from_error_detail(LM_STUDIO_STREAM_ERROR["error"]["message"]), 8192)
        self.assertEqual(
            _window_from_error_detail("This model's maximum context length is 65536 tokens."),
            65536,
        )
        self.assertEqual(_window_from_error_detail("no number here"), 0)

    def test_message_names_backend_window_and_next_step(self) -> None:
        local = context_overflow_message("本地-llama", 4096, is_local=True)
        self.assertIn("本地-llama", local)
        self.assertIn("4096 tokens", local)
        self.assertIn("新会话", local)
        self.assertIn("上下文窗口", local)
        online = context_overflow_message("deepseek", 0, is_local=False)
        self.assertIn("未知", online)
        self.assertIn("新会话", online)


# ---------------------------------------------------------------------------
# A：HTTP 4xx 链路（真走 ModelRuntime.complete）
# ---------------------------------------------------------------------------


class RuntimeOverflowTests(unittest.TestCase):
    LOCAL_PROFILE = {
        "kind": "local",
        "name": "本地-llama",
        "base_url": "http://127.0.0.1:8080",
        "model": "qwen3-8b",
        "request_format": "llama_cpp",
        "context_window": 32768,
    }
    ONLINE_PROFILE = {
        "kind": "online",
        "name": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "request_format": "openai_chat",
        "api_key": "sk-test",
    }
    MESSAGES = [{"role": "user", "content": "ping"}]

    def _run(self, outcomes: list, profile: dict, options: dict | None = None) -> tuple[list, BaseException | None]:
        calls: list = []

        def fake_open(request, timeout, cancel_event=None, opener=None):
            calls.append(request)
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, list):
                return FakeStreamResponse(outcome)
            return FakeResponse(outcome)

        with mock.patch.object(ModelRuntime, "_urlopen_cancelable", fake_open), \
                mock.patch("naiba.llm.runtime.time.sleep"), \
                mock.patch.dict(os.environ, {"NAIBA_ERROR_DUMP_DIR": tempfile.mkdtemp(prefix="naiba-of-")}, clear=False):
            try:
                ModelRuntime().complete(profile, self.MESSAGES, options or {"stream": False})
                error: BaseException | None = None
            except RuntimeError as exc:
                error = exc
        return calls, error

    def test_local_400_overflow_reports_backend_window_and_hint(self) -> None:
        calls, error = self._run([http_error(400, LLAMA_CPP_400)], self.LOCAL_PROFILE)
        self.assertIsInstance(error, ContextOverflowError)
        text = str(error)
        self.assertIn("本地-llama", text)
        # 后端自报的 n_ctx 优先于本机配置（配置写的是 32768，真值是 4096）。
        self.assertIn("4096 tokens", text)
        self.assertIn("新会话", text)
        self.assertNotIn("HTTP 400", text, "溢出不该再把原始 JSON 甩给用户")
        self.assertEqual(len(calls), 1, "本地溢出不得重试")

    def test_unknown_window_falls_back_to_profile_value(self) -> None:
        body = json.dumps({"error": {"message": "the input exceeds the context window size"}})
        _calls, error = self._run([http_error(400, body)], self.LOCAL_PROFILE)
        self.assertIsInstance(error, ContextOverflowError)
        self.assertIn("32768 tokens", str(error))

    def test_online_overflow_is_not_retried(self) -> None:
        body = json.dumps({
            "error": {"message": "This model's maximum context length is 65536 tokens."},
        })
        calls, error = self._run([http_error(400, body)], self.ONLINE_PROFILE)
        self.assertIsInstance(error, ContextOverflowError)
        self.assertEqual(len(calls), 1, "溢出是 4xx，重发同一份超长请求必然再失败")
        self.assertIn("deepseek", str(error))

    def test_ordinary_400_keeps_existing_behaviour(self) -> None:
        _calls, error = self._run([http_error(400, "bad request")], self.ONLINE_PROFILE)
        self.assertIsNotNone(error)
        self.assertNotIsInstance(error, ContextOverflowError)
        self.assertIn("HTTP 400", str(error))

    def test_stream_error_inside_response_becomes_overflow(self) -> None:
        """流里内嵌的溢出错误也要走同一条文案出口（本地流式路径）。"""
        # Ollama 的流是 NDJSON（不带 `data:` 前缀），照它的真实形态构造。
        stream = [json.dumps(OLLAMA_CONTEXT_ERROR).encode("utf-8")]
        profile = {**self.LOCAL_PROFILE, "request_format": "ollama", "name": "本地-ollama"}
        _calls, error = self._run([stream], profile, options={"stream": True})
        self.assertIsInstance(error, ContextOverflowError)
        self.assertIn("本地-ollama", str(error))
        self.assertIn("2048 tokens", str(error))
        self.assertIn("新会话", str(error))


# ---------------------------------------------------------------------------
# C：Agent 循环内复查
# ---------------------------------------------------------------------------


class _LoopCatalog:
    def scan(self) -> list:
        return []

    def read_skill_content(self, path: str) -> str:  # pragma: no cover
        return ""


class _EchoRegistry:
    """永远成功、但结果逐次不同（否则先撞「无进展」熔断）。"""

    def __init__(self, payload: str) -> None:
        self.calls = 0
        self.payload = payload

    def schemas(self) -> list:
        return []

    def side_effect(self, name: str) -> bool:
        return False

    def media_declaration(self, name: str) -> dict:
        return {"extract": "none", "policy": "never"}

    def execute(self, tool: str, arguments: dict, active: list, run_context: object):
        self.calls += 1
        return True, f"{self.payload} #{self.calls}"


def _tool_call(_profile, _messages, _options, _event) -> str:
    return '{"type":"tool","tool":"echo","arguments":{"n":1}}'


class LoopContextGuardTests(unittest.TestCase):
    def _run(self, profile: dict, registry, options: dict, events: list) -> tuple:
        worker = SkillAgent(_LoopCatalog(), None, _tool_call, None)
        return worker.run(
            "跑起来",
            [],
            profile,
            options,
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["echo"],
            events.append,
            None,
            tool_registry=registry,
        )

    def test_loop_stops_before_overlong_request(self) -> None:
        """工具结果把请求体撑过窗口时：不再发注定失败的请求，保住已完成的结果。"""
        events: list[dict] = []
        registry = _EchoRegistry("探" * 3000)
        calls: list = []

        def counting_complete(profile, messages, options, event):
            calls.append(len(messages))
            return _tool_call(profile, messages, options, event)

        worker = SkillAgent(_LoopCatalog(), None, counting_complete, None)
        response, runs, _reasonings, _usage = worker.run(
            "跑起来",
            [],
            {"kind": "local", "model": "m", "context_window": 4096},
            {"stream": False, "max_tokens": 512, "max_steps": 10},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["echo"],
            events.append,
            None,
            tool_registry=registry,
        )
        self.assertEqual(len(calls), 1, "越界后不得再发一次注定失败的请求")
        self.assertEqual(len(runs), 1, "已完成的工具结果必须保留")
        self.assertTrue(
            any(item.get("type") == "context_full" for item in events),
            "必须让前端知道窗口满了（前端据此锁输入框）",
        )
        self.assertIn("新会话", response)
        self.assertIn("上下文", response)

    def test_loop_unaffected_when_within_budget(self) -> None:
        """窗口充裕时循环内复查不得提前打断（回归：不能变成「一律只跑一步」）。"""
        events: list[dict] = []
        registry = _EchoRegistry("短的")
        response, runs, _reasonings, _usage = self._run(
            {"kind": "online", "model": "m", "context_window": 128000},
            registry,
            {"stream": False, "max_tokens": 1024, "max_steps": 3},
            events,
        )
        self.assertEqual(registry.calls, 3)
        self.assertEqual(len(runs), 3)
        self.assertFalse(any(item.get("type") == "context_full" for item in events))
        self.assertIn("步数上限", response)


# ---------------------------------------------------------------------------
# D：length 成因区分
# ---------------------------------------------------------------------------


class TruncationCauseTests(unittest.TestCase):
    def test_local_saturated_window_is_context(self) -> None:
        info = agent_module._truncation_info(
            "length", "写了一半的正文", usage={"total_tokens": 4000}, limit=4096, local=True,
        )
        self.assertTrue(info["truncated"])
        self.assertEqual(info["cause"], "context")

    def test_local_below_window_is_output(self) -> None:
        info = agent_module._truncation_info(
            "length", "正文", usage={"total_tokens": 1200}, limit=32768, local=True,
        )
        self.assertEqual(info["cause"], "output")

    def test_input_output_split_is_used_when_total_missing(self) -> None:
        info = agent_module._truncation_info(
            "length", "正文",
            usage={"input_tokens": 3900, "output_tokens": 100}, limit=4096, local=True,
        )
        self.assertEqual(info["cause"], "context")

    def test_online_stays_output(self) -> None:
        info = agent_module._truncation_info(
            "length", "正文", usage={"total_tokens": 250000}, limit=256000, local=False,
        )
        self.assertEqual(info["cause"], "output")

    def test_missing_usage_or_window_is_output(self) -> None:
        self.assertEqual(agent_module._truncation_info("length", "正文")["cause"], "output")
        self.assertEqual(
            agent_module._truncation_info("length", "正文", usage={"total_tokens": 1}, limit=0)["cause"],
            "output",
        )

    def test_non_length_truncation_has_no_cause(self) -> None:
        info = agent_module._truncation_info("", "正文停在冒号：")
        self.assertTrue(info["truncated"])
        self.assertEqual(info["cause"], "")

    def test_run_metadata_carries_cause(self) -> None:
        """真跑一轮：撞窗口的 length 必须带 cause=context 落进 run_context。"""

        class _StubRuntime:
            last_reasoning = ""
            last_reasoning_id = ""
            last_usage = {"input_tokens": 3900, "output_tokens": 120, "total_tokens": 4020}
            last_finish_reason = "length"

            def complete(self, profile, messages, options, event):  # noqa: D102
                return "写了一半的正文"

        runtime = _StubRuntime()
        events: list[dict] = []
        run_context: dict = {}
        worker = SkillAgent(_LoopCatalog(), None, runtime.complete, None)
        _content, _runs, _reasonings, _usage = worker.run(
            "写点什么",
            [],
            {"kind": "local", "model": "m", "context_window": 4096},
            {"stream": False, "max_tokens": 512, "max_steps": 4},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["echo"],
            events.append,
            None,
            tool_registry=_EchoRegistry("无关"),
            run_context=run_context,
        )
        truncation = run_context.get("truncation") or {}
        self.assertTrue(truncation.get("truncated"))
        self.assertEqual(truncation.get("cause"), "context")
        self.assertTrue(truncation.get("continued"), "续写一次的既有行为不变")


class TruncationNoticeSourceTests(unittest.TestCase):
    """前端提示行必须按 cause 分文案（源码级；渲染级另有 npm 桩检查）。"""

    def setUp(self) -> None:
        self.source = (REPO_ROOT / "public" / "js" / "03-media.js").read_text(encoding="utf-8")

    def test_notice_branches_on_cause(self) -> None:
        self.assertIn("truncated.cause", self.source, "提示行必须读 cause")
        self.assertIn("cause === 'context'", self.source)
        self.assertIn("上下文窗口已耗尽", self.source)
        self.assertIn("已达模型输出上限", self.source)


if __name__ == "__main__":
    unittest.main()
