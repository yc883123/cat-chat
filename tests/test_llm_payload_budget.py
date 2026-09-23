# -*- coding: utf-8 -*-
"""守门：在线请求体过大（HTTP 413）必须被识别、自愈、并且在发送前预检。

背景（用户上报）：拖入 76 张图做审核，主 Agent 拆出的子任务里每调用一次视觉工具就往
messages 尾部追加一批（≤4 张）图片，批次只增不减；审核到第 10 张时中继直接返回
``HTTP 413 openai_error``，原文抛给用户。原实现对 400/404/422/429/5xx 都有识别或自愈链，
**唯独 413 没有任何分支**，落到最后的 ``raise RuntimeError(...返回 HTTP 413: {原始 JSON})``。

本文件钉死三条：

1. 瘦身只动图片、不动文本，且从**最旧**的一张开始；
2. 413 ⇒ 省掉较早图片后重发一次（单次、禁循环）；仍 413 ⇒ 抛用户可读文案，
   **不得回显供应商原始 openai_error JSON**；
3. 发送前预算守卫：估算字节超阈值就先瘦身，省掉一次「上传几十 MB、必然 413」的往返；
   本地模型路径不动（它有自己的图片上限逻辑）。

全量验证口径见 `项目维护说明（修改代码前必读）.md` §六。
"""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from naiba.llm.runtime import (
    IMAGE_OMITTED_PLACEHOLDER,
    ONLINE_PAYLOAD_BUDGET_BYTES,
    ModelRuntime,
    _image_omitted_part,
    _iter_image_slots,
    _json_payload_bytes,
    _slim_payload_images,
    online_payload_budget_bytes,
)

PROFILE_OPENAI = {
    "kind": "online",
    "name": "gelly",
    "base_url": "https://relay.example.com",
    "model": "gpt-4o",
    "request_format": "openai_chat",
    "api_key": "sk-test",
}
PROFILE_LOCAL = {
    "kind": "local",
    "name": "ollama",
    "base_url": "http://127.0.0.1:11434",
    "model": "qwen3-vl",
    "request_format": "ollama",
}

IMAGE_A = "A" * 4000
IMAGE_B = "B" * 4000
IMAGE_C = "C" * 4000


def messages_with_images() -> list[dict]:
    """三张图，按「旧 → 新」排列，每张都带一条文本说明（文本必须原样保留）。"""
    return [
        {"role": "user", "content": [
            {"type": "text", "text": "第一张图"},
            {"type": "image", "data": IMAGE_A, "media_type": "image/jpeg"},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "第二张图"},
            {"type": "image", "data": IMAGE_B, "media_type": "image/jpeg"},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "第三张图"},
            {"type": "image", "data": IMAGE_C, "media_type": "image/jpeg"},
        ]},
    ]


def http_error(code: int, body: str = "", reason: str = "Payload Too Large") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://relay.example.com/v1/chat/completions",
        code,
        reason,
        {"Content-Type": "application/json"},
        io.BytesIO(body.encode("utf-8")),
    )


def openai_reply(text: str = "pong") -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


ERROR_413_BODY = json.dumps(
    {"error": {"message": "openai_error", "type": "invalid_request_error"}}, ensure_ascii=False
)


class FakeResponse:
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


def wire_payload(request) -> dict:
    return json.loads(request.data.decode("utf-8"))


def image_parts(payload: dict) -> list[str]:
    """从发出去的 wire 里数出 image_url 的数量。"""
    found = []
    for message in payload.get("messages") or []:
        for part in message.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "image_url":
                found.append(str(part.get("image_url", {}).get("url") or ""))
    return found


class SlimHelperTests(unittest.TestCase):
    """瘦身函数本身：顺序、范围、字节估算、各协议替换件。"""

    def test_payload_conversion_matches_wire(self) -> None:
        """前提断言：无图片的消息在 wire 上是纯文本，图片消息才带 image_url 块。"""
        payload = {"messages": ModelRuntime()._openai_messages([
            {"role": "user", "content": "纯文本"},
            {"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image", "data": IMAGE_A, "media_type": "image/jpeg"},
            ]},
        ])}
        self.assertEqual(payload["messages"][0]["content"], "纯文本")
        self.assertEqual(len(image_parts(payload)), 1, "缺陷前提：图片必须以 image_url 块发送")

    def test_omits_oldest_first_and_keeps_text(self) -> None:
        payload = {"messages": ModelRuntime()._openai_messages(messages_with_images())}
        self.assertEqual(len(image_parts(payload)), 3, "缺陷前提：三张图都在请求体里")
        target = _json_payload_bytes(payload) - 3000  # 一张图（约 4KB）就够省
        removed = _slim_payload_images(payload, "openai_chat", target)
        self.assertEqual(removed, 1, "刚好够省时就只省一张")
        urls = image_parts(payload)
        self.assertEqual(len(urls), 2)
        self.assertIn(IMAGE_B, urls[0], "最旧的图必须被先省略")
        self.assertIn(IMAGE_C, urls[1])
        texts = [
            str(part.get("text") or "")
            for message in payload["messages"]
            for part in message["content"] if isinstance(part, dict)
        ]
        self.assertIn("第一张图", texts, "文本一律不动（对话语义不受影响）")
        self.assertIn("第二张图", texts)
        self.assertIn("第三张图", texts)
        self.assertIn(IMAGE_OMITTED_PLACEHOLDER, texts, "被省略的图必须留下可自述的占位")

    def test_always_omits_at_least_one_even_when_under_target(self) -> None:
        """413 路径的前提：我们自己的估算可能与供应商上限不一致，必须真的缩小请求体。"""
        payload = {"messages": ModelRuntime()._openai_messages(messages_with_images())}
        removed = _slim_payload_images(payload, "openai_chat", 1)
        self.assertEqual(removed, 3, "预算极小时应一路省到没有图片可省")
        self.assertEqual(image_parts(payload), [])
        self.assertEqual(
            list(_iter_image_slots(payload, "openai_chat")), [],
            "省完之后不得再有图片槽位",
        )

    def test_no_images_returns_zero(self) -> None:
        payload = {"messages": ModelRuntime()._openai_messages([{"role": "user", "content": "只有文字"}])}
        self.assertEqual(_slim_payload_images(payload, "openai_chat", 1), 0)

    def test_non_positive_target_is_noop(self) -> None:
        payload = {"messages": ModelRuntime()._openai_messages(messages_with_images())}
        self.assertEqual(_slim_payload_images(payload, "openai_chat", 0), 0)
        self.assertEqual(len(image_parts(payload)), 3)

    def test_placeholder_shape_per_protocol(self) -> None:
        self.assertEqual(_image_omitted_part("openai_chat", "user"), {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER})
        self.assertEqual(_image_omitted_part("claude", "user"), {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER})
        self.assertEqual(_image_omitted_part("gemini", "user"), {"text": IMAGE_OMITTED_PLACEHOLDER})
        self.assertEqual(
            _image_omitted_part("codex_responses", "assistant"),
            {"type": "output_text", "text": IMAGE_OMITTED_PLACEHOLDER},
        )
        self.assertEqual(
            _image_omitted_part("codex_responses", "user"),
            {"type": "input_text", "text": IMAGE_OMITTED_PLACEHOLDER},
        )

    def test_image_slots_cover_all_online_protocols(self) -> None:
        cases = {
            "openai_chat": {"messages": [{"content": [{"type": "image_url", "image_url": {"url": "u"}}]}]},
            "claude": {"messages": [{"content": [{"type": "image", "source": {"data": "x"}}]}]},
            "codex_responses": {"input": [{"content": [{"type": "input_image", "image_url": "u"}]}]},
            "gemini": {"contents": [{"parts": [{"inlineData": {"data": "x"}}]}]},
        }
        for request_format, payload in cases.items():
            with self.subTest(request_format=request_format):
                self.assertEqual(
                    len(list(_iter_image_slots(payload, request_format))), 1,
                    f"{request_format} 的图片槽位必须能被识别",
                )

    def test_budget_override(self) -> None:
        self.assertEqual(online_payload_budget_bytes(None), ONLINE_PAYLOAD_BUDGET_BYTES)
        self.assertEqual(online_payload_budget_bytes({"online_payload_budget_bytes": 1234}), 1234)
        self.assertEqual(online_payload_budget_bytes({"online_payload_budget_bytes": 0}), 0)


class PayloadTooLargeRuntimeTests(unittest.TestCase):
    """真跑 ``ModelRuntime.complete``：413 的识别、自愈与文案。"""

    def _run(
        self,
        outcomes: list,
        messages: list | None = None,
        options: dict | None = None,
        profile: dict | None = None,
        status=None,
    ) -> tuple[list, str, BaseException | None]:
        calls: list = []

        def fake_open(request, timeout, cancel_event=None, opener=None):
            calls.append(request)
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, BaseException):
                raise outcome
            return FakeResponse(outcome)

        with mock.patch.object(ModelRuntime, "_urlopen_cancelable", fake_open), \
                mock.patch("naiba.llm.runtime.time.sleep"), \
                mock.patch.dict(os.environ, {"NAIBA_ERROR_DUMP_DIR": "/tmp"}, clear=False):
            try:
                content = ModelRuntime().complete(
                    profile or PROFILE_OPENAI,
                    messages if messages is not None else messages_with_images(),
                    options if options is not None else {"stream": False},
                    status,
                )
                error = None
            except RuntimeError as exc:
                content, error = "", exc
        return calls, content, error

    def test_413_retries_once_after_omitting_oldest_image(self) -> None:
        events: list[dict] = []
        calls, content, error = self._run(
            [http_error(413, ERROR_413_BODY), openai_reply("pong")], status=events.append
        )
        self.assertIsNone(error)
        self.assertEqual(content, "pong", "省图重试成功后应当正常返回内容")
        self.assertEqual(len(calls), 2)
        first, second = (wire_payload(call) for call in calls)
        self.assertEqual(len(image_parts(first)), 3, "第一次请求本来是 3 张图")
        urls = image_parts(second)
        self.assertEqual(len(urls), 2, "重试必须真的省掉一张")
        self.assertIn(IMAGE_B, urls[0], "从最旧的一张开始省")
        self.assertTrue(
            any("请求体过大" in str(event.get("message") or "") for event in events),
            f"必须有可读的状态提示，实际：{events}",
        )

    def test_413_after_slim_raises_readable_error_without_raw_body(self) -> None:
        calls, _content, error = self._run([http_error(413, ERROR_413_BODY)])
        self.assertIsNotNone(error)
        message = str(error)
        self.assertEqual(len(calls), 2, "瘦身重试只允许一次（禁循环）")
        self.assertIn("HTTP 413", message)
        self.assertIn("请求体过大", message)
        self.assertIn("已自动省略较早的图片后仍被拒绝", message)
        self.assertNotIn("openai_error", message, "不得把供应商原始 JSON 塞给用户")
        self.assertNotIn("invalid_request_error", message)

    def test_413_without_images_is_not_retried(self) -> None:
        calls, _content, error = self._run(
            [http_error(413, ERROR_413_BODY)],
            messages=[{"role": "user", "content": "只有文字，没有图片"}],
        )
        self.assertEqual(len(calls), 1, "没有图片可省时不得空跑一次注定失败的请求")
        self.assertIsNotNone(error)
        self.assertIn("HTTP 413", str(error))
        self.assertIn("图片/附件总量超出供应商上限", str(error))

    def test_local_profile_is_never_slimmed(self) -> None:
        """本地模型有自己的图片上限逻辑：这里既不瘦身也不重发。"""
        calls, _content, error = self._run(
            [http_error(413, "too large", "Payload Too Large")],
            profile=PROFILE_LOCAL,
        )
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(error)
        self.assertIn("HTTP 413", str(error))

    def test_slim_only_touches_images(self) -> None:
        """重试请求里除图片外的字节必须逐字不变（只改图片槽位）。"""
        calls, _content, _error = self._run([http_error(413, ERROR_413_BODY), openai_reply()])
        first, second = (wire_payload(call) for call in calls)
        self.assertEqual(first["model"], second["model"])
        self.assertEqual(first["stream"], second["stream"])
        self.assertEqual(len(first["messages"]), len(second["messages"]))
        for before, after in zip(first["messages"], second["messages"]):
            before_parts = before.get("content") or []
            after_parts = after.get("content") or []
            self.assertEqual(len(before_parts), len(after_parts), "只允许替换槽位，不允许增删")
            for part_before, part_after in zip(before_parts, after_parts):
                if part_before.get("type") == "image_url":
                    continue
                self.assertEqual(part_before, part_after, "文本块必须逐字不变")

    def test_budget_guard_slims_before_first_request(self) -> None:
        """B3：超预算时第一发就已经瘦身（不先传一轮必然 413 的请求）。"""
        events: list[dict] = []
        calls, content, error = self._run(
            [openai_reply("pong")],
            options={"stream": False, "online_payload_budget_bytes": 1},
            status=events.append,
        )
        self.assertIsNone(error)
        self.assertEqual(content, "pong")
        self.assertEqual(len(calls), 1, "守卫必须在发送前生效，只应发出一次请求")
        self.assertEqual(image_parts(wire_payload(calls[0])), [], "超预算时图片应全部被省略")
        self.assertTrue(
            any("已省略 3 张较早的图片" in str(event.get("message") or "") for event in events),
            f"守卫必须给出可读提示，实际：{events}",
        )

    def test_budget_guard_off_when_zero(self) -> None:
        calls, _content, _error = self._run(
            [openai_reply()],
            options={"stream": False, "online_payload_budget_bytes": 0},
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(image_parts(wire_payload(calls[0]))), 3, "预算为 0 = 关闭预检查")


if __name__ == "__main__":
    unittest.main()
