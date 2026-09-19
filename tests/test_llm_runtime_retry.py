"""守门：在线模型 HTTP 5xx 必须退避重试 + 失败请求体落盘（2026-09-09 实测教训）。

背景：一次 HTTP 500（响应体只有 ``Internal Server Error``，没有结构化错误信息）直接终止
了整轮对话；18 秒后重发同样的请求即成功（供应商侧瞬时故障）。原实现只对 429/502/503/504
重试、且只在「思考回传类 400」时落盘请求体，于是 5xx 既不自愈、也无任何取证。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

from naiba.llm.runtime import ERROR_DUMP_FILENAME, EmptyModelStreamError, ModelRuntime

PROFILE = {
    "kind": "online",
    "name": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
    "request_format": "codex_responses",
    "api_key": "sk-should-never-be-dumped",
}
MESSAGES = [{"role": "user", "content": "ping"}]
PROFILE_OPENAI = {**PROFILE, "request_format": "openai_chat"}
# 非 DeepSeek 画像（GPT）：openai_chat 下 reasoning_effort 在线可控档。
PROFILE_GPT = {
    **PROFILE,
    "name": "gpt",
    "base_url": "https://api.openai.com",
    "model": "gpt-5",
    "request_format": "openai_chat",
}
# Kimi K3 画像（月之暗面官方 openai_chat）：reasoning_effort 只认 low/high/max。
PROFILE_KIMI = {
    **PROFILE,
    "name": "kimi",
    "base_url": "https://api.moonshot.cn",
    "model": "kimi-k3",
    "request_format": "openai_chat",
}


def sse(chunk: dict) -> bytes:
    """把单个 SSE 事件序列化成一行 bytes。"""
    return ("data: " + json.dumps(chunk, ensure_ascii=False)).encode("utf-8")


# 空流：completed 事件但 output 为空 → 正文/思考/action 皆空。
EMPTY_CODEX_STREAM = [sse({"type": "response.completed", "response": {"output": []}})]
DELTA_CODEX_STREAM = [sse({"type": "response.output_text.delta", "delta": "答复"})]
EMPTY_OPENAI_STREAM = [sse({"choices": [{"delta": {}}]})]
DELTA_OPENAI_STREAM = [sse({"choices": [{"delta": {"content": "答复"}}]})]
# 只有思考、没有正文：mimo-v2.5 这类推理模型长思考后可能整轮只回 reasoning_content，
# 正文为空的流对用户等于「气泡里什么都没有」，必须当空流重试而不是直接判失败。
REASONING_ONLY_OPENAI_STREAM = [
    sse({"choices": [{"delta": {"reasoning_content": "先核对一遍参数…"}}]}),
    sse({"choices": [{"delta": {}}]}),
]
# codex_responses 版「有推理零正文」：deepseek-v4.1-flash 高档思考陷入推理循环、
# 正文/工具全空、上游以零计费截断的真实事故形态（2026-09-19 实测，单次约 10 万字符
# 推理、三次重试全部重演）。重试必须沿 high→medium→low 降档打破循环，不能原样重发。
REASONING_ONLY_CODEX_STREAM = [
    sse({"type": "response.reasoning_summary_text.delta", "delta": "再想想……"}),
    sse({"type": "response.completed", "response": {"output": []}}),
]


def http_error(code: int, body: str = "", reason: str = "Internal Server Error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.deepseek.com/responses",
        code,
        reason,
        {"Content-Type": "application/json"},
        io.BytesIO(body.encode("utf-8")),
    )


class FakeResponse:
    """够用的 urllib 响应替身：非流式路径只用到 read()/headers。"""

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
    """可迭代 SSE 响应替身：流式路径按行迭代 bytes（不再用 read()）。"""

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


class OnlineRetryTests(unittest.TestCase):
    def _run(
        self,
        outcomes: list,
        status=None,
        messages: list | None = None,
        dump_dir: str | None = None,
        options: dict | None = None,
        profile: dict | None = None,
    ) -> tuple[list, str, BaseException | None]:
        """outcomes：按调用顺序生效，异常实例→抛出，dict→非流式响应，list[bytes]→流式响应（末项重复）。

        dump_dir 缺省用独立临时目录，避免把取证文件写进仓库工作区。
        options 缺省非流式（{"stream": False}）；profile 缺省 codex_responses。
        """
        calls: list = []
        if dump_dir is None:
            dump_dir = tempfile.mkdtemp(prefix="naiba-retry-test-")
            self.addCleanup(shutil.rmtree, dump_dir, ignore_errors=True)

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
                mock.patch.dict(os.environ, {"NAIBA_ERROR_DUMP_DIR": dump_dir}, clear=False):
            try:
                content = ModelRuntime().complete(
                    profile or PROFILE,
                    messages or MESSAGES,
                    options if options is not None else {"stream": False},
                    status,
                )
                error = None
            except RuntimeError as exc:
                content, error = "", exc
        return calls, content, error

    def test_http_500_is_retried_then_raises(self) -> None:
        events: list[dict] = []
        calls, _content, error = self._run([http_error(500)] * 4, status=events.append)
        # attempts = 4：A0 注入型兜底启用时下限 +1（见 runtime 里 attempts 计算处注释）。
        self.assertEqual(len(calls), 4, "HTTP 500 必须重试到次数上限")
        self.assertIsNotNone(error)
        self.assertIn("HTTP 500", str(error))
        retry_notes = [
            str(event.get("message") or "")
            for event in events
            if event.get("type") == "status" and "重试" in str(event.get("message") or "")
        ]
        self.assertTrue(
            any("HTTP 500" in note for note in retry_notes),
            f"重试状态提示必须带状态码，实际：{retry_notes}",
        )

    def test_http_500_then_success_returns_content(self) -> None:
        calls, content, error = self._run([
            http_error(500),
            {"output_text": "pong", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}},
        ])
        self.assertIsNone(error)
        self.assertEqual(content, "pong", "重试成功后应当正常返回内容")
        self.assertEqual(len(calls), 2)

    def test_http_400_is_not_retried(self) -> None:
        calls, _content, error = self._run([http_error(400, "bad request", "Bad Request")])
        self.assertEqual(len(calls), 1, "普通 400 不得重试")
        self.assertIsNotNone(error)

    def test_5xx_dumps_sanitized_payload(self) -> None:
        long_image = "A" * 200_000
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image", "data": long_image, "media_type": "image/jpeg"},
            ],
        }]
        with tempfile.TemporaryDirectory() as tmp:
            _calls, _content, error = self._run([http_error(500)], messages=messages, dump_dir=tmp)
            self.assertIsNotNone(error)
            path = os.path.join(tmp, ERROR_DUMP_FILENAME)
            self.assertTrue(os.path.exists(path), "5xx 必须落盘请求体")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            parsed = json.loads(text)
            self.assertEqual(parsed["status"], 500)
            self.assertEqual(parsed["reason"], "server-error")
            self.assertIn("payload", parsed)
            self.assertNotIn(long_image[:200], text, "base64 图片必须被截断，不能原样落盘")
            self.assertIn("<str:2000", text, "超长字段应带自述长度的占位符")
            self.assertNotIn(PROFILE["api_key"], text, "API Key 绝不能落盘")

    def test_plain_400_does_not_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._run([http_error(400, "bad request", "Bad Request")], dump_dir=tmp)
            self.assertFalse(os.path.exists(os.path.join(tmp, ERROR_DUMP_FILENAME)))

    def test_reasoning_400_still_dumps(self) -> None:
        body = json.dumps({
            "error": {"message": "The `reasoning_text` in the thinking mode must be passed back to the API."},
        })
        with tempfile.TemporaryDirectory() as tmp:
            _calls, _content, error = self._run([http_error(400, body, "Bad Request")], dump_dir=tmp)
            self.assertIsNotNone(error)
            path = os.path.join(tmp, ERROR_DUMP_FILENAME)
            self.assertTrue(os.path.exists(path), "思考回传类 400 仍要落盘（既有取证能力不得回退）")
            with open(path, encoding="utf-8") as handle:
                parsed = json.loads(handle.read())
            self.assertEqual(parsed["reason"], "reasoning-passback")

    def test_codex_empty_stream_is_retried_then_raises(self) -> None:
        events: list[dict] = []
        calls, _content, error = self._run(
            [EMPTY_CODEX_STREAM], status=events.append, options={"stream": True})
        self.assertEqual(len(calls), 4, "codex 空流必须重试到次数上限")
        self.assertIsInstance(error, EmptyModelStreamError)
        self.assertIn("没有文本内容", str(error))
        retry_notes = [
            str(event.get("message") or "")
            for event in events
            if event.get("type") == "status" and "空响应" in str(event.get("message") or "")
        ]
        self.assertTrue(retry_notes, f"空流重试必须带状态提示，实际：{retry_notes}")

    def test_codex_empty_then_delta_returns_content(self) -> None:
        calls, content, error = self._run(
            [EMPTY_CODEX_STREAM, DELTA_CODEX_STREAM], options={"stream": True})
        self.assertIsNone(error)
        self.assertEqual(content, "答复", "空流重试成功后应返回正文")
        self.assertEqual(len(calls), 2)

    def test_openai_chat_empty_stream_is_not_retried(self) -> None:
        calls, _content, error = self._run(
            [EMPTY_OPENAI_STREAM], options={"stream": True}, profile=PROFILE_OPENAI)
        self.assertEqual(len(calls), 1, "其它在线格式的空流不得纳入重试")
        self.assertIsNotNone(error)
        self.assertNotIsInstance(error, EmptyModelStreamError)

    def test_reasoning_only_stream_is_retried_then_raises(self) -> None:
        """有思考无正文：整轮按空流退避重试，不能把"空白回答"交给上层。"""
        events: list[dict] = []
        calls, _content, error = self._run(
            [REASONING_ONLY_OPENAI_STREAM], status=events.append,
            options={"stream": True}, profile=PROFILE_OPENAI)
        self.assertEqual(len(calls), 4, "有思考无正文的流必须重试到次数上限")
        self.assertIsInstance(error, EmptyModelStreamError)
        self.assertIn("没有文本内容", str(error))
        retry_notes = [
            str(event.get("message") or "")
            for event in events
            if event.get("type") == "status" and "空响应" in str(event.get("message") or "")
        ]
        self.assertTrue(retry_notes, f"空流重试必须带状态提示，实际：{retry_notes}")

    def test_reasoning_only_then_delta_returns_content(self) -> None:
        """重试后正文正常返回：思考不能把这一轮的结果提前吃成空回答。"""
        calls, content, error = self._run(
            [REASONING_ONLY_OPENAI_STREAM, DELTA_OPENAI_STREAM],
            options={"stream": True}, profile=PROFILE_OPENAI)
        self.assertIsNone(error)
        self.assertEqual(content, "答复", "思考轮重试成功后应返回正文")
        self.assertEqual(len(calls), 2)

    def test_reasoning_only_codex_retry_lowers_effort(self) -> None:
        """高档思考空流：重试必须降低思考强度，而不是原样重发同一负载。"""
        events: list[dict] = []
        calls, content, error = self._run(
            [REASONING_ONLY_CODEX_STREAM, DELTA_CODEX_STREAM],
            status=events.append, options={"stream": True},
            profile={**PROFILE, "reasoning_effort": "high"})
        self.assertIsNone(error)
        self.assertEqual(content, "答复")
        self.assertEqual(len(calls), 2)
        # PROFILE 模型名含 deepseek → codex 映射 high→max、medium→high。
        first = json.loads(calls[0].data.decode("utf-8"))
        second = json.loads(calls[1].data.decode("utf-8"))
        self.assertEqual(first["reasoning"]["effort"], "max", "首发必须按会话设置")
        self.assertEqual(second["reasoning"]["effort"], "high", "重试必须降一档")
        notes = [
            str(event.get("message") or "")
            for event in events
            if event.get("type") == "status"
        ]
        self.assertTrue(
            any("降低思考强度" in note for note in notes),
            f"降档重试必须在状态提示里告知用户，实际：{notes}",
        )

    def test_reasoning_only_codex_exhausted_reports_diagnosis(self) -> None:
        """三档全部烧完：报错必须带诊断与可操作建议，不能只有「没有文本内容」。"""
        events: list[dict] = []
        calls, _content, error = self._run(
            [REASONING_ONLY_CODEX_STREAM], status=events.append,
            options={"stream": True}, profile={**PROFILE, "reasoning_effort": "high"})
        self.assertEqual(len(calls), 4)
        self.assertIsNotNone(error)
        self.assertIn("没有文本内容", str(error))
        self.assertIn("降低思考强度", str(error))
        efforts = [
            json.loads(call.data.decode("utf-8"))["reasoning"]["effort"]
            for call in calls
        ]
        self.assertEqual(efforts[:3], ["max", "high", "low"], "重试必须逐级降档")
        self.assertEqual(set(efforts[3:]), {"low"}, "降到最低档后不得再改负载")

    def test_reasoning_only_codex_low_effort_retries_as_is(self) -> None:
        """已是最低档：无档可降，保持原样退避重试（应对上游瞬时抖动）。"""
        calls, _content, error = self._run(
            [REASONING_ONLY_CODEX_STREAM], options={"stream": True},
            profile={**PROFILE, "reasoning_effort": "low"})
        self.assertEqual(len(calls), 4)
        self.assertIsInstance(error, EmptyModelStreamError)
        efforts = [
            json.loads(call.data.decode("utf-8"))["reasoning"]["effort"]
            for call in calls
        ]
        self.assertEqual(set(efforts), {"low"}, "无档可降时不得改负载")

    def test_reasoning_only_codex_auto_effort_retries_as_is(self) -> None:
        """auto 未发任何思考参数：无档可降，重试负载不得新增 reasoning 字段。"""
        calls, _content, error = self._run(
            [REASONING_ONLY_CODEX_STREAM], options={"stream": True}, profile=PROFILE)
        self.assertEqual(len(calls), 4)
        self.assertIsInstance(error, EmptyModelStreamError)
        for call in calls:
            self.assertNotIn("reasoning", json.loads(call.data.decode("utf-8")))

    def test_reasoning_only_openai_deepseek_does_not_lower(self) -> None:
        """openai_chat + DeepSeek 画像不发思考字段（端点拒收）：降档无从谈起。"""
        calls, _content, error = self._run(
            [REASONING_ONLY_OPENAI_STREAM], options={"stream": True},
            profile={**PROFILE_OPENAI, "reasoning_effort": "high"})
        self.assertEqual(len(calls), 4)
        self.assertIsInstance(error, EmptyModelStreamError)
        for call in calls:
            self.assertNotIn("reasoning_effort", json.loads(call.data.decode("utf-8")))

    def test_reasoning_only_openai_gpt_lowers_effort(self) -> None:
        """openai_chat + GPT：reasoning_effort 在线可控，重试降档 high→medium。"""
        calls, content, error = self._run(
            [REASONING_ONLY_OPENAI_STREAM, DELTA_OPENAI_STREAM],
            options={"stream": True}, profile={**PROFILE_GPT, "reasoning_effort": "high"})
        self.assertIsNone(error)
        self.assertEqual(content, "答复")
        efforts = [
            json.loads(call.data.decode("utf-8")).get("reasoning_effort")
            for call in calls
        ]
        self.assertEqual(efforts, ["high", "medium"])

    def test_reasoning_only_kimi_k3_lowers_with_dialect(self) -> None:
        """Kimi K3 降档走自家方言：首发 high→max，重试 medium→high。"""
        calls, content, error = self._run(
            [REASONING_ONLY_OPENAI_STREAM, DELTA_OPENAI_STREAM],
            options={"stream": True}, profile={**PROFILE_KIMI, "reasoning_effort": "high"})
        self.assertIsNone(error)
        self.assertEqual(content, "答复")
        efforts = [
            json.loads(call.data.decode("utf-8")).get("reasoning_effort")
            for call in calls
        ]
        self.assertEqual(efforts, ["max", "high"], "K3 方言映射必须在降档链上生效")

    def test_kimi_k3_off_maps_to_low_on_wire(self) -> None:
        """K3 思考关不掉：off 必须发最低档 low（不发字段 = 默认 max，与「关」相反）。"""
        calls, content, error = self._run(
            [{"choices": [{"message": {"content": "pong"}}],
              "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}],
            profile={**PROFILE_KIMI, "reasoning_effort": "off"})
        self.assertIsNone(error)
        self.assertEqual(content, "pong")
        payload = json.loads(calls[0].data.decode("utf-8"))
        self.assertEqual(payload.get("reasoning_effort"), "low")


# 不支持思考的模型（auto 档必须**不发** max_tokens，否则每请求白打一次 400 往返）。
PROFILE_GPT4O = {
    **PROFILE,
    "name": "openai-official",
    "base_url": "https://api.openai.com",
    "model": "gpt-4o",
    "request_format": "openai_chat",
}
# Anthropic：max_tokens 是协议必填（默认值是 4096 兜底，不是「不发」）。
PROFILE_CLAUDE = {
    **PROFILE,
    "name": "anthropic",
    "base_url": "https://api.anthropic.com",
    "model": "claude-sonnet-4",
    "request_format": "claude",
}
# Gemini：输出上限落在 generationConfig.maxOutputTokens，键名与其它三种协议都不同。
PROFILE_GEMINI = {
    **PROFILE,
    "name": "gemini",
    "base_url": "https://generativelanguage.googleapis.com",
    "model": "gemini-2.5-pro",
    "request_format": "gemini",
}
PLAIN_OPENAI_DONE = {
    "choices": [{"message": {"content": "pong"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class ThinkingOutputBudgetTests(unittest.TestCase):
    """A0：思考时的默认输出上限（max_tokens 是「思考+正文」的总量闸门）。

    事故缺口不是「填得太大」，而是「不填 = 不发字段 = 上游默认值近乎无限」（只有 Claude
    有 4096 硬兜底）。MiMo 会话单条 43 万字符思考就是这么烧出来的。
    """

    def _run(
        self,
        outcomes: list,
        options: dict | None = None,
        profile: dict | None = None,
        messages: list | None = None,
        status=None,
    ) -> tuple[list, str, BaseException | None]:
        calls: list = []
        dump_dir = tempfile.mkdtemp(prefix="naiba-a0-test-")
        self.addCleanup(shutil.rmtree, dump_dir, ignore_errors=True)

        def fake_open(request, timeout, cancel_event=None, opener=None):
            calls.append(request)
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, BaseException):
                raise outcome
            return FakeResponse(outcome)

        with mock.patch.object(ModelRuntime, "_urlopen_cancelable", fake_open), \
                mock.patch("naiba.llm.runtime.time.sleep"), \
                mock.patch.dict(os.environ, {"NAIBA_ERROR_DUMP_DIR": dump_dir}, clear=False):
            try:
                content = ModelRuntime().complete(
                    profile or PROFILE,
                    messages or MESSAGES,
                    options if options is not None else {"stream": False},
                    status,
                )
                error = None
            except RuntimeError as exc:
                content, error = "", exc
        return calls, content, error

    @staticmethod
    def _payload(call) -> dict:
        return json.loads(call.data.decode("utf-8"))

    def test_explicit_effort_fills_default_cap(self) -> None:
        calls, content, error = self._run(
            [PLAIN_OPENAI_DONE], profile={**PROFILE_GPT, "reasoning_effort": "high"})
        self.assertIsNone(error)
        self.assertEqual(content, "pong")
        self.assertEqual(self._payload(calls[0])["max_tokens"], 32768)

    def test_cap_is_clamped_by_preset_ceiling(self) -> None:
        """claude 预设的 ceiling 是 8192：注入值必须是 min(32768, ceiling)。"""
        calls, _content, _error = self._run(
            [{"content": [{"type": "text", "text": "pong"}]}],
            profile={**PROFILE_CLAUDE, "reasoning_effort": "high"})
        self.assertEqual(self._payload(calls[0])["max_tokens"], 8192)

    def test_user_value_wins_over_cap(self) -> None:
        calls, _content, _error = self._run(
            [PLAIN_OPENAI_DONE], options={"stream": False, "max_tokens": 65536},
            profile={**PROFILE_GPT, "reasoning_effort": "high"})
        self.assertEqual(self._payload(calls[0])["max_tokens"], 65536,
                         "用户显式设的值（含故意设很大）永远优先")

    def test_off_never_fills(self) -> None:
        calls, _content, _error = self._run(
            [PLAIN_OPENAI_DONE], profile={**PROFILE_GPT, "reasoning_effort": "off"})
        self.assertNotIn("max_tokens", self._payload(calls[0]))

    def test_auto_without_model_level_evidence_never_fills(self) -> None:
        """粒度洞回归：协议族预设命中**不构成** auto 档的填充依据。

        gpt-4o 不思考，若按协议族预设（openai）填 32768，端点会 400；而剥字段自愈只在
        **单次请求内**重试、不是学习，于是每个请求都完整重演「填 → 400 → 剥 → 重试」。
        """
        calls, _content, _error = self._run([PLAIN_OPENAI_DONE], profile=PROFILE_GPT4O)
        self.assertNotIn("max_tokens", self._payload(calls[0]),
                         "auto 档 + 仅协议族预设命中 ⇒ 不得发字段（与现状一致）")

    def test_auto_with_model_level_evidence_fills(self) -> None:
        """模型级证据成立（DeepSeek 画像命中预设表里的思考模型）⇒ auto 也填。"""
        calls, _content, _error = self._run([PLAIN_OPENAI_DONE], profile=PROFILE_OPENAI)
        self.assertEqual(self._payload(calls[0])["max_tokens"], 32768)

    def test_auto_with_card_declared_thinking_fills(self) -> None:
        """卡片显式配置 thinking ⇒ 视为模型级证据（用户自己声明这台端点会思考）。"""
        calls, _content, _error = self._run(
            [PLAIN_OPENAI_DONE],
            profile={**PROFILE_GPT4O, "thinking": {"max_output_ceiling": 65536}})
        self.assertEqual(self._payload(calls[0])["max_tokens"], 32768,
                         "auto + 卡片声明 ⇒ 填 min(32768, ceiling)")

    def test_claude_fills_even_at_auto(self) -> None:
        """claude 的 max_tokens 是协议必填：A0.b 的「off/auto 不填」对它不适用。"""
        calls, _content, _error = self._run(
            [{"content": [{"type": "text", "text": "pong"}]}],
            profile={**PROFILE_CLAUDE, "reasoning_effort": "off"})
        self.assertEqual(self._payload(calls[0])["max_tokens"], 8192)

    def test_cap_lands_on_each_protocols_own_key(self) -> None:
        """四种协议的**落点键名各不相同**——写错键名会静默不生效（真实请求体复检踩过）。

        真实复测（verify/_probe_a0_wire.py）用记录型上游收下请求体才发现：
        codex_responses 是 `max_output_tokens`、gemini 是 `generationConfig.maxOutputTokens`，
        按 `max_tokens` 去断言只会看到 None，误判成「没注入」。
        """
        cases = [
            ("openai_chat", {**PROFILE_GPT, "reasoning_effort": "high"}, PLAIN_OPENAI_DONE,
             ["max_tokens"], 32768),
            ("codex_responses", {**PROFILE, "reasoning_effort": "high"}, {"output": []},
             ["max_output_tokens"], 32768),
            ("claude", {**PROFILE_CLAUDE, "reasoning_effort": "high"},
             {"content": [{"type": "text", "text": "pong"}]}, ["max_tokens"], 8192),
            ("gemini", {**PROFILE_GEMINI, "reasoning_effort": "high"}, {"candidates": []},
             ["generationConfig", "maxOutputTokens"], 8192),
        ]
        for label, profile, outcome, path, expect in cases:
            with self.subTest(protocol=label):
                calls, _content, _error = self._run([outcome], profile=profile)
                payload = self._payload(calls[0])
                for key in path:
                    self.assertIn(key, payload, f"{label} 的落点键名不得改变")
                    payload = payload[key]
                self.assertEqual(payload, expect, f"{label} 的注入值")

    def test_injected_value_400_drops_field_once(self) -> None:
        """注入值被端点拒 ⇒ 去字段重试一次（用户没要求这个值，不能让兜底打死一轮）。"""
        body = json.dumps({"error": {"message": "max_output_tokens is too large for this model"}})
        events: list[dict] = []
        calls, content, error = self._run(
            [http_error(400, body, "Bad Request"), {"output_text": "pong"}],
            status=events.append)
        self.assertIsNone(error)
        self.assertEqual(content, "pong")
        self.assertEqual(len(calls), 2, "只重试一次，禁循环")
        self.assertIn("max_output_tokens", self._payload(calls[0]))
        self.assertNotIn("max_output_tokens", self._payload(calls[1]),
                         "重试必须去掉我们注入的字段")
        notes = [str(e.get("message") or "") for e in events if e.get("type") == "status"]
        self.assertTrue(any("输出上限" in note for note in notes), f"必须告知用户，实际：{notes}")

    def test_user_value_400_is_not_swallowed(self) -> None:
        """用户显式设的值触发的 400 **不吞错**（照常抛出，暴露真实问题）。"""
        body = json.dumps({"error": {"message": "max_output_tokens is too large"}})
        calls, _content, error = self._run(
            [http_error(400, body, "Bad Request")], options={"stream": False, "max_tokens": 999999})
        self.assertIsNotNone(error)
        self.assertIn("HTTP 400", str(error))
        self.assertEqual(len(calls), 1, "用户显式值不得触发去字段自愈")

    def test_chained_stripping_has_budget(self) -> None:
        """effort=high 打在不支持思考的端点：连锁剥 reasoning_effort + 注入的 max_tokens。

        两次剥字段各消耗一次 attempt，默认 3 会很紧 ⇒ 注入启用时 attempts 下限 +1。
        同时断言：第一次剥掉的字段不能在下一次重建负载时被贴回来（否则永远剥不干净）。
        """
        reasoning_body = json.dumps({"error": {"message": "unknown field: reasoning_effort"}})
        cap_body = json.dumps({"error": {"message": "max_tokens is too large"}})
        calls, content, error = self._run(
            [
                http_error(400, reasoning_body, "Bad Request"),
                http_error(400, cap_body, "Bad Request"),
                PLAIN_OPENAI_DONE,
            ],
            profile={**PROFILE_GPT, "reasoning_effort": "high"},
        )
        self.assertIsNone(error, f"剥两类字段后必须仍有余量成功，实际错误：{error}")
        self.assertEqual(content, "pong")
        self.assertEqual(len(calls), 3)
        self.assertEqual(self._payload(calls[0]).get("reasoning_effort"), "high")
        self.assertEqual(self._payload(calls[1]).get("max_tokens"), 32768,
                         "第二次只剩「去 max_tokens」一件事要做")
        self.assertNotIn("reasoning_effort", self._payload(calls[1]),
                         "已剥掉的思考字段不得在重建负载时被贴回来")
        self.assertNotIn("max_tokens", self._payload(calls[2]))
        self.assertNotIn("reasoning_effort", self._payload(calls[2]))

    def test_attempts_budget_bumped_only_when_injected(self) -> None:
        """注入启用 ⇒ attempts 下限 +1（3 → 4）；未启用（gpt-4o auto）保持 3。"""
        calls_bad, _content, error_bad = self._run(
            [http_error(500)], profile={**PROFILE_GPT, "reasoning_effort": "high"})
        self.assertIsNotNone(error_bad)
        self.assertEqual(len(calls_bad), 4, "注入启用时 attempts = 3 + 1")
        calls_plain, _content, error_plain = self._run([http_error(500)], profile=PROFILE_GPT4O)
        self.assertIsNotNone(error_plain)
        self.assertEqual(len(calls_plain), 3, "未注入时 attempts 保持 3")


# Kimi K2.x：用 thinking 参数，**收到 reasoning_effort 会 400**（根因 6 的真 bug）。
PROFILE_KIMI_K2 = {
    **PROFILE,
    "name": "kimi-k2",
    "base_url": "https://api.moonshot.cn",
    "model": "kimi-k2.6",
    "request_format": "openai_chat",
}


class ThinkingDialectWireTests(unittest.TestCase):
    """思考方言落到请求体的位置与取值（预设数据驱动的端到端证据）。"""

    def _payload_for(self, profile: dict, effort: str) -> dict:
        calls: list = []
        done = (
            {"output_text": "pong", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            if str(profile.get("request_format")) == "codex_responses"
            else PLAIN_OPENAI_DONE
        )

        def fake_open(request, timeout, cancel_event=None, opener=None):
            calls.append(request)
            return FakeResponse(done)

        with mock.patch.object(ModelRuntime, "_urlopen_cancelable", fake_open):
            ModelRuntime().complete(
                {**profile, "reasoning_effort": effort}, MESSAGES, {"stream": False}, None
            )
        return json.loads(calls[0].data.decode("utf-8"))

    def test_kimi_k2_never_receives_reasoning_effort(self) -> None:
        """根因 6：K2.x 收到 reasoning_effort 会 400（改档位必先白打一次）。

        改成数据驱动后，K2 落到 format=none 的预设，**任何档位都不发字段**。
        """
        for effort in ("off", "low", "medium", "high"):
            payload = self._payload_for(PROFILE_KIMI_K2, effort)
            self.assertNotIn("reasoning_effort", payload, f"K2 在 {effort} 档不得发字段")
            self.assertNotIn("reasoning", payload, f"K2 在 {effort} 档不得发任何思考字段")

    def test_kimi_k3_mapping_unchanged(self) -> None:
        """§九.102 三家分治的 K3 映射不变：off→low / low→low / medium→high / high→max。"""
        self.assertEqual(self._payload_for(PROFILE_KIMI, "off").get("reasoning_effort"), "low")
        self.assertEqual(self._payload_for(PROFILE_KIMI, "low").get("reasoning_effort"), "low")
        self.assertEqual(self._payload_for(PROFILE_KIMI, "medium").get("reasoning_effort"), "high")
        self.assertEqual(self._payload_for(PROFILE_KIMI, "high").get("reasoning_effort"), "max")

    def test_auto_sends_no_effort_field(self) -> None:
        payload = self._payload_for(PROFILE_KIMI, "auto")
        self.assertNotIn("reasoning_effort", payload)

    def test_kimi_k3_via_codex_relay_is_not_special_cased(self) -> None:
        """资格约束：kimi-k3 + codex_responses 不得命中 K3 预设（由中继按 OpenAI 方言翻译）。"""
        profile = {**PROFILE_KIMI, "request_format": "codex_responses",
                   "base_url": "https://relay.example.com"}
        payload = self._payload_for(profile, "medium")
        self.assertEqual(payload.get("reasoning"), {"effort": "medium"},
                         "K3 走 codex 中继时必须按 OpenAI Codex 方言发")

    def test_deepseek_codex_mapping_unchanged(self) -> None:
        self.assertEqual(self._payload_for(PROFILE, "off").get("reasoning"), {"effort": "none"})
        self.assertEqual(self._payload_for(PROFILE, "high").get("reasoning"), {"effort": "max"})


if __name__ == "__main__":
    unittest.main()
