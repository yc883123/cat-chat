"""模型流解析与推理流（自 naiba.llm.runtime 迁出）。

含：SSE/Ollama/LM Studio 流解析、推理 (<think>) 流事件、Agent 工具协议守卫
（判别书面前缀是正文还是 JSON/XML 工具动作）与缓冲收尾。纯解析层：
不触碰网络/锁/状态；ModelRuntime 经继承 StreamMixins（并 ProtocolMixins）复用。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from naiba.llm.protocols import ProtocolMixins

StatusCallback = Callable[[dict[str, Any]], None]


# 「本轮请求超出上下文窗口」的服务端特征串。只用来判定**服务端错误体**（4xx 的 detail、
# 流里内嵌的 error 事件），不碰用户正文，因此没有误伤面。各后端写法不同：
# llama.cpp / Unsloth 回 `exceed_context_size_error` 与 `n_ctx`；LM Studio 回
# `greater than the context length`；Ollama 把错误整块塞在流里；在线供应商多为
# `maximum context length`（DeepSeek/OpenAI）或 `prompt is too long`（Anthropic）。
_CONTEXT_OVERFLOW_MARKERS = (
    "exceed_context_size",
    "exceeds the available context",
    "exceeds the context",
    "context length",
    "context window",
    "context size",
    "maximum context",
    "max context length",
    "prompt is too long",
    "input is too long",
    "reduce the length",
    "n_ctx",
)

# 流内错误事件的来源标注（写进异常文案，让用户知道是哪个后端拒的）。
_STREAM_SOURCE_LABELS = {
    "ollama": "Ollama",
    "lm_studio": "LM Studio",
    "llama_cpp": "llama.cpp",
    "unsloth": "Unsloth",
}


def is_context_overflow(detail: Any) -> bool:
    """服务端错误体是否表示「上下文窗口不足」（大小写无关的纯字符串判定）。"""
    text = str(detail or "").lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)


class ContextOverflowError(RuntimeError):
    """后端明确回报：本轮请求已超出模型的上下文窗口。

    与「网络故障」「供应商限流」分开：溢出是 4xx、重发同一份请求必然再失败，必须给
    用户**可操作的建议**（新会话 / 填写真实上下文窗口）而不是一段原始 JSON。
    用户可见文案由 `naiba.llm.runtime.context_overflow_message` 在「后端名 + 当前窗口」
    都已知时统一拼装（本层只管如实带上后端原文）。
    """

    def __init__(
        self,
        message: str,
        *,
        window: int = 0,
        backend: str = "",
        detail: str = "",
    ) -> None:
        super().__init__(str(message or "上下文窗口不足"))
        try:
            self.window = max(0, int(window or 0))
        except (TypeError, ValueError):
            self.window = 0
        self.backend = str(backend or "")
        self.detail = str(detail or "")


def _stream_error(source: str, error: Any) -> BaseException:
    """把流里内嵌的错误对象/文本转成合适的异常（溢出走专用类型，其余保留原文）。

    ``detail`` 保留错误的**完整原样**（dict 就序列化）：本地后端常把真值放在 message
    的兄弟字段里（llama.cpp 的 ``n_ctx``），只留 message 就再也解析不出真实窗口。
    """
    if isinstance(error, dict):
        detail = str(
            error.get("message") or error.get("detail") or error.get("error") or error
        )
        try:
            raw = json.dumps(error, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(error)
    else:
        detail = str(error or "").strip()
        raw = detail
    detail = detail or "未知错误"
    if is_context_overflow(detail) or is_context_overflow(raw):
        return ContextOverflowError(detail, detail=raw, backend=source)
    return RuntimeError(f"{source} 流式错误：{detail}")


# Patterns used to keep agent tool-call protocols out of the user-facing
# streaming answer. The classifier below decides, before forwarding any
# fragment, whether the leading model output is ordinary prose or an agent
# action (JSON / XML tool protocol) that must only reach the Agent Loop.
_TOOL_OPEN_TAG = re.compile(r"^<(tool_calls|invoke|tool)\b", re.IGNORECASE)
_TOOL_NAMED_ATTR = re.compile(r"\b(?:name|type)\s*=")
# Some compatible endpoints prepend a sentence before emitting their tool
# protocol. Keep a short unflushed tail so a marker split across SSE chunks is
# detected before it can reach the visible answer.
_TOOL_PROTOCOL_ANYWHERE = re.compile(r"<(?:tool_calls|invoke|tool)\b", re.IGNORECASE)
# Kimi K3 (and other Harmony-style compatible endpoints) may serialize tool
# calls as reserved tokens in ordinary output text instead of returning native
# function_call items, for example ``<|open|>tools<|sep|>...``.  Treat this as
# an agent protocol at the stream boundary so it never leaks into the answer.
_HARMONY_TOOL_ANYWHERE = re.compile(r"<\|open\|>(?:tools|call)\b", re.IGNORECASE)
_JSON_TOOL_ANYWHERE = re.compile(
    r'\{(?=[\s\S]{0,96}"(?:type|tool)"\s*:)',
    re.IGNORECASE,
)
# Upper bound (chars) for buffering an ambiguous leading fragment before we
# give up and treat it as plain text, so a malformed stream can never stall.
_AGENT_BUFFER_LIMIT = 1024


class _InlineReasoningParser:
    """Split local-model <think> streams without leaking them into the answer."""

    _OPEN = ("<think>", "<thinking>", "<reasoning>")
    _CLOSE = ("</think>", "</thinking>", "</reasoning>")

    def __init__(self) -> None:
        self.buffer = ""
        self.inside = False

    def feed(self, text: str, final: bool = False) -> tuple[str, str]:
        self.buffer += str(text or "")
        visible: list[str] = []
        reasoning: list[str] = []
        while self.buffer:
            markers = self._CLOSE if self.inside else self._OPEN
            positions = [(self.buffer.lower().find(marker), marker) for marker in markers]
            positions = [(index, marker) for index, marker in positions if index >= 0]
            if positions:
                index, marker = min(positions, key=lambda item: item[0])
                chunk = self.buffer[:index]
                (reasoning if self.inside else visible).append(chunk)
                self.buffer = self.buffer[index + len(marker):]
                self.inside = not self.inside
                continue
            if final:
                (reasoning if self.inside else visible).append(self.buffer)
                self.buffer = ""
                break
            # Retain only a suffix that can actually become a marker in the
            # next SSE chunk.  A fixed tail made every short answer arrive in
            # bursts even when it contained no reasoning tag at all.
            lower = self.buffer.lower()
            keep = 0
            for marker in markers:
                limit = min(len(marker) - 1, len(lower))
                for size in range(1, limit + 1):
                    if marker.startswith(lower[-size:]):
                        keep = max(keep, size)
            if keep:
                if keep == len(self.buffer):
                    # The entire buffer may be the beginning of a marker
                    # (for example ``<thi``). Keep it for the next SSE chunk
                    # and stop this pass instead of looping over unchanged
                    # data forever.
                    break
                chunk, self.buffer = self.buffer[:-keep], self.buffer[-keep:]
            else:
                chunk, self.buffer = self.buffer, ""
            (reasoning if self.inside else visible).append(chunk)
        return "".join(visible), "".join(reasoning)


class _ReasoningStreamer:
    """Stream reasoning deltas live when a provider exposes them incrementally.

    ``feed`` emits ``reasoning_start`` once, then a ``reasoning_delta`` per
    incoming piece. ``finish`` closes with ``reasoning_end`` when streaming was
    possible; otherwise (a model that only returns a lump of thinking at the end,
    or none at all) it falls back to a one-shot ``_emit_buffered_reasoning`` so
    the reasoning is still shown, just not incrementally.
    """

    def __init__(self, status: StatusCallback | None, parts: list[str]):
        self.status = status
        self.parts = parts
        self.started = False

    def feed(self, reasoning: str) -> None:
        if not reasoning:
            return
        self.parts.append(reasoning)
        if self.status is not None:
            if not self.started:
                self.status({"type": "reasoning_start"})
                self.started = True
            self.status({"type": "reasoning_delta", "content": reasoning})

    def finish(self) -> None:
        if self.started:
            if self.status is not None:
                self.status({"type": "reasoning_end"})
        else:
            # 兜底：模型未实时暴露思考（增量解析没有触发），改为结尾一次性输出。
            StreamMixins._emit_buffered_reasoning(self.status, "".join(self.parts))


class StreamMixins:
    @staticmethod
    def _read_ollama_stream(response: Any, status: StatusCallback | None) -> dict[str, Any]:
        chunks: list[dict[str, Any]] = []
        full_content_parts: list[str] = []
        pending = ""
        reasoning_parts: list[str] = []
        reasoning_streamer = _ReasoningStreamer(status, reasoning_parts)
        tool_protocol = False
        inline_parser = _InlineReasoningParser()
        native_tool_calls: dict[int, dict[str, str]] = {}
        for raw_line in response:
            try:
                chunk = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(chunk, dict):
                continue
            # Ollama 把后端错误（含「超出上下文长度」）作为一行 `{"error": "..."}` 直接
            # 塞在流里。此前这个字段整块被吞掉，用户在界面上只能看到「流式响应中没有
            # 文本内容」——真实原因（窗口不够 / 模型未加载 / 参数非法）完全不可见。
            error = chunk.get("error")
            if error not in (None, "", False, {}, []):
                raise _stream_error("Ollama", error)
            chunks.append(chunk)
            message = chunk.get("message") or {}
            for index, raw_call in enumerate(message.get("tool_calls") or [] if isinstance(message, dict) else []):
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function") or {}
                raw_arguments = function.get("arguments", {})
                arguments = raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments, ensure_ascii=False)
                native_tool_calls[index] = {
                    "id": str(raw_call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": arguments,
                }
            text, reasoning = StreamMixins._ollama_stream_delta(chunk)
            text, inline_reasoning = inline_parser.feed(text)
            reasoning = reasoning + inline_reasoning
            if reasoning:
                reasoning_streamer.feed(reasoning)
            if not text:
                continue
            full_content_parts.append(text)
            if not tool_protocol:
                pending += text
                pending, tool_protocol = StreamMixins._forward_guarded_text(pending, status)
        final_text, final_reasoning = inline_parser.feed("", final=True)
        if final_reasoning:
            reasoning_streamer.feed(final_reasoning)
        if final_text:
            full_content_parts.append(final_text)
            pending += final_text
        if not tool_protocol:
            pending, tool_protocol = StreamMixins._forward_guarded_text(pending, status, final=True)
        reasoning_streamer.finish()
        return {
            "content": (
                ProtocolMixins._build_action_from_native_tool_calls(native_tool_calls)
                if native_tool_calls
                else StreamMixins._clean_content("".join(full_content_parts))
            ),
            "reasoning": "".join(reasoning_parts),
            "usage": ProtocolMixins._online_usage("ollama", chunks),
            "finish_reason": ProtocolMixins._online_finish_reason("ollama", chunks),
        }


    @staticmethod
    def _ollama_stream_delta(chunk: dict[str, Any]) -> tuple[str, str]:
        message = chunk.get("message") or {}
        if not isinstance(message, dict):
            return "", ""
        return str(message.get("content") or ""), str(message.get("thinking") or "")


    @staticmethod
    def _read_sse_response(
        response: Any,
        request_format: str,
        status: StatusCallback | None,
    ) -> dict[str, Any]:
        """Collect SSE chunks while forwarding only ordinary prose to the chat client.

        Agent tool protocols (JSON actions, ``<tool_calls>`` / ``<invoke>``,
        DeepSeek ``<tool name="...">`` and native OpenAI ``tool_calls``) are
        buffered but never sent as ``delta`` events—they only reach the Agent
        Loop as the parsed action returned by ``complete``.
        """
        chunks: list[dict[str, Any]] = []
        full_content_parts: list[str] = []
        pending = ""
        reasoning_parts: list[str] = []
        reasoning_ids: list[str] = []
        reasoning_streamer = _ReasoningStreamer(status, reasoning_parts)
        native_tool_calls: dict[int, dict[str, str]] = {}
        tool_protocol = False
        inline_parser = _InlineReasoningParser()
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            # 有些本地后端（llama.cpp / Unsloth 的部分版本、部分兼容网关）不返回 4xx，
            # 而是把错误作为 SSE 事件嵌在流里。正常事件（含 codex_responses 的
            # output_item.* / response.* 与 OpenAI 的 choices delta）都不带顶层 `error`
            # 键，故用真值判定即可，不会误伤正常流。此前这类错误事件被静默跳过，最终
            # 只会以「流式响应中没有文本内容」收场。
            error = chunk.get("error")
            if error not in (None, "", False, {}, []):
                raise _stream_error(
                    _STREAM_SOURCE_LABELS.get(request_format, "模型服务"), error
                )
            chunks.append(chunk)
            event_type = str(chunk.get("type") or "")
            # 捕获 reasoning item 的服务端唯一 id：output_item.added 事件携带 item
            # 对象；delta 事件以 item_id 兜底（OpenAI 规范字段）。回传时需要（官方
            # schema reasoning item 的 id 为必填；缺失在多轮长链实测 400）。
            if request_format == "codex_responses" and event_type == "response.output_item.added":
                item = chunk.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    iid = str(item.get("id") or "")
                    if iid and iid not in reasoning_ids:
                        reasoning_ids.append(iid)
            elif request_format == "codex_responses" and "reasoning" in event_type and event_type.endswith("delta"):
                iid = str(chunk.get("item_id") or "")
                if iid and iid not in reasoning_ids:
                    reasoning_ids.append(iid)
            text, reasoning, tool_calls = ProtocolMixins._stream_delta_full(request_format, chunk)
            text, inline_reasoning = inline_parser.feed(text)
            reasoning = reasoning + inline_reasoning
            if reasoning:
                reasoning_streamer.feed(reasoning)
            if tool_calls:
                # Native OpenAI tool calls must not appear as answer text.
                if not tool_protocol:
                    StreamMixins._forward_guarded_text(pending, status, final=True)
                    pending = ""
                tool_protocol = True
                for call in tool_calls:
                    slot = native_tool_calls.setdefault(
                        call.get("index", 0), {"id": "", "name": "", "arguments": ""}
                    )
                    if call.get("id"):
                        slot["id"] = call["id"]
                    if call.get("name"):
                        slot["name"] += call["name"]
                    if call.get("arguments") is not None:
                        slot["arguments"] += call["arguments"]
                continue
            if not text:
                continue
            full_content_parts.append(text)
            if not tool_protocol:
                pending += text
                pending, tool_protocol = StreamMixins._forward_guarded_text(pending, status)
        final_text, final_reasoning = inline_parser.feed("", final=True)
        if final_reasoning:
            reasoning_streamer.feed(final_reasoning)
        if final_text:
            full_content_parts.append(final_text)
            pending += final_text
        # codex_responses 中继可能只回聚合事件（response.completed /
        # response.output_item.done）而不逐段发 output_text.delta。增量正文为空时
        # 从这里回填正文/思考/reasoning_id/tool action，避免被误判为空流。
        aggregated_action = ""
        if (
            request_format == "codex_responses"
            and not native_tool_calls
            and not tool_protocol
        ):
            agg_text, agg_reasoning, agg_id, aggregated_action = (
                ProtocolMixins._codex_responses_aggregated(chunks)
            )
            if agg_id and agg_id not in reasoning_ids:
                reasoning_ids.append(agg_id)
            if agg_reasoning and not reasoning_parts:
                reasoning_streamer.feed(agg_reasoning)
            if not aggregated_action and agg_text and not "".join(full_content_parts).strip():
                full_content_parts.append(agg_text)
                pending += agg_text
        if not tool_protocol:
            pending, tool_protocol = StreamMixins._forward_guarded_text(pending, status, final=True)
        reasoning_streamer.finish()
        usage = ProtocolMixins._online_usage(request_format, chunks)
        if native_tool_calls:
            # Convert to the internal action structure the Agent Loop consumes.
            content = ProtocolMixins._build_action_from_native_tool_calls(native_tool_calls)
        elif aggregated_action:
            # 仅聚合事件的 function_call：镜像非流式 _online_response 的 action 优先。
            content = aggregated_action
        else:
            content = StreamMixins._clean_content("".join(full_content_parts))
        return {
            "content": content,
            "reasoning": "".join(reasoning_parts),
            "reasoning_id": reasoning_ids[-1] if reasoning_ids else "",
            "usage": usage,
            "finish_reason": ProtocolMixins._online_finish_reason(request_format, chunks),
        }


    @staticmethod
    def _read_lm_studio_stream(response: Any, status: StatusCallback | None) -> dict[str, Any]:
        """按 LM Studio 原生 chat SSE 事件解析（``message.delta`` / ``reasoning.delta`` / ``error`` / ``chat.end``）。

        与 OpenAI 兼容流不同，LM Studio 的 ``type`` 事件直接携带 ``content`` 增量；结构化错误事件
        为 ``{"type":"error","error":...}``，结束事件为 ``{"type":"chat.end"}``。
        """
        chunks: list[dict[str, Any]] = []
        full_content_parts: list[str] = []
        pending = ""
        reasoning_parts: list[str] = []
        reasoning_streamer = _ReasoningStreamer(status, reasoning_parts)
        tool_protocol = False
        inline_parser = _InlineReasoningParser()
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            chunks.append(chunk)
            event_type = str(chunk.get("type") or "")
            if event_type == "error":
                error = chunk.get("error") or "LM Studio 返回未知错误"
                if isinstance(error, dict):
                    error = str(error.get("message") or error.get("details") or error)
                # 统一走 `_stream_error`：溢出（LM Studio 的「greater than the context
                # length (n_keep: …, n_ctx: …)」）转成专用错误，其余保留原文。
                raise _stream_error("LM Studio", error)
            if event_type in ("chat.end", "message.end", "reasoning.end", "message.start", "reasoning.start"):
                continue
            if event_type in ("reasoning.delta", "reasoning.full"):
                reasoning = ProtocolMixins._text_value(chunk.get("content"))
                if reasoning:
                    reasoning_streamer.feed(reasoning)
                continue
            if event_type in ("message.delta", "message.full"):
                text = ProtocolMixins._text_value(chunk.get("content"))
            else:
                # 未知事件但可能带有正文/文本字段（兼容字段命名）。
                text = ProtocolMixins._text_value(chunk.get("content") or chunk.get("text"))
            text, inline_reasoning = inline_parser.feed(text)
            if inline_reasoning:
                reasoning_streamer.feed(inline_reasoning)
            if not text:
                continue
            full_content_parts.append(text)
            if not tool_protocol:
                pending += text
                pending, tool_protocol = StreamMixins._forward_guarded_text(pending, status)
        final_text, final_reasoning = inline_parser.feed("", final=True)
        if final_reasoning:
            reasoning_streamer.feed(final_reasoning)
        if final_text:
            full_content_parts.append(final_text)
            pending += final_text
        if not tool_protocol:
            pending, tool_protocol = StreamMixins._forward_guarded_text(pending, status, final=True)
        reasoning_streamer.finish()
        return {
            "content": StreamMixins._clean_content("".join(full_content_parts)),
            "reasoning": "".join(reasoning_parts),
            "usage": ProtocolMixins._online_usage("lm_studio", chunks) if chunks else {},
            "finish_reason": ProtocolMixins._online_finish_reason("lm_studio", chunks),
        }


    @staticmethod
    def _classify_agent_output(buffer: str) -> str:
        """Classify the leading model output before forwarding it to the chat UI.

        Returns ``"tool"`` for an agent tool-call protocol (kept out of the
        visible answer), ``"text"`` for ordinary prose (safe to stream), or
        ``"pending"`` when the buffer is too short to decide confidently.
        """
        probe = buffer.lstrip()
        if not probe:
            return "pending"
        first = probe[0]
        if first in "{[":
            # JSON object / array agent action is never part of the answer, but
            # only when it actually carries the action-style "type"/"tool" key.
            # A bare "{" or "[" that is simply the tail of streamed prose/code
            # (e.g. Java braces, "[Shot 1]" labels) must be forwarded as text,
            # otherwise multi-code-block answers stall until the stream ends.
            if re.search(r'"\s*(?:type|tool)"\s*:', probe[:200]):
                return "tool"
            return "text"
        if first == "<":
            match = re.match(r"^<([A-Za-z][\w-]*)", probe)
            if not match:
                return "text" if (">" in probe[:64] or len(probe) > 64) else "pending"
            tag = match.group(1).lower()
            if tag in {"tool_calls", "invoke"}:
                return "tool"
            if tag == "tool":
                # DeepSeek named-tool dialect: <tool name="..."> (optionally
                # wrapped in <tool type="tool">). Require a name/type attribute.
                if _TOOL_NAMED_ATTR.search(probe[:200]):
                    return "tool"
                if ">" in probe[:200]:
                    return "text"
                return "pending" if len(probe) <= _AGENT_BUFFER_LIMIT else "text"
            # Another tag (markdown/HTML in prose, <think>, ...): decide once
            # the opening tag closes; otherwise keep buffering briefly.
            if ">" in probe[:200]:
                return "text"
            return "pending" if len(probe) <= _AGENT_BUFFER_LIMIT else "text"
        return "text"


    @staticmethod
    def _tool_protocol_offset(buffer: str) -> int | None:
        """Return the earliest XML/JSON agent protocol marker in ``buffer``."""
        offsets = []
        xml_match = _TOOL_PROTOCOL_ANYWHERE.search(buffer)
        if xml_match:
            offsets.append(xml_match.start())
        harmony_match = _HARMONY_TOOL_ANYWHERE.search(buffer)
        if harmony_match:
            offsets.append(harmony_match.start())
        json_match = _JSON_TOOL_ANYWHERE.search(buffer)
        if json_match:
            offsets.append(json_match.start())
        return min(offsets) if offsets else None


    @staticmethod
    def _possible_protocol_suffix_length(buffer: str) -> int:
        """Return only the ambiguous suffix that must wait for the next chunk.

        Ordinary answer text should be forwarded immediately.  We retain a
        short partial XML marker (for example ``<tool_ca``) or a partial JSON
        first field (for example ``{\"ty``), rather than delaying every stream
        by a fixed number of characters.
        """
        lower = buffer.lower()
        keep = 0
        for token in (
            "<tool_calls", "<invoke", "<tool",
            "<|open|>tools", "<|open|>call",
        ):
            limit = min(len(token) - 1, len(lower))
            for size in range(1, limit + 1):
                if token.startswith(lower[-size:]):
                    keep = max(keep, size)

        brace = buffer.rfind("{")
        if brace >= 0:
            fragment = buffer[brace:]
            rest = fragment[1:].lstrip()
            possible = not rest
            if rest.startswith('"'):
                field = rest[1:]
                if '"' in field:
                    name, tail = field.split('"', 1)
                    possible = name.lower() in {"type", "tool"} and not tail.strip()
                else:
                    possible = any(name.startswith(field.lower()) for name in ("type", "tool"))
            if possible:
                keep = max(keep, len(fragment))
        return keep


    @staticmethod
    def _emit_buffered_reasoning(status: StatusCallback | None, reasoning: str) -> None:
        """Publish reasoning only after the response is known to be user-facing."""
        if not status or not reasoning.strip():
            return
        status({"type": "reasoning_start"})
        status({"type": "reasoning_delta", "content": reasoning})
        status({"type": "reasoning_end"})


    @staticmethod
    def _forward_guarded_text(
        pending: str,
        status: StatusCallback | None,
        final: bool = False,
    ) -> tuple[str, bool]:
        """Forward safe prose while retaining enough tail to catch tool XML/JSON.

        Returns the unflushed tail and whether a tool protocol was detected.
        Once detected, callers suppress the remainder of that model response.
        """
        classification = StreamMixins._classify_agent_output(pending)
        if classification == "tool":
            return "", True
        offset = StreamMixins._tool_protocol_offset(pending)
        if offset is not None:
            visible = pending[:offset]
            if visible and status:
                status({"type": "delta", "content": visible})
            return "", True
        if final:
            if pending and status:
                status({"type": "delta", "content": pending})
            return "", False
        keep = StreamMixins._possible_protocol_suffix_length(pending)
        if keep >= len(pending):
            return pending, False
        visible = pending[:-keep] if keep else pending
        if visible and status:
            status({"type": "delta", "content": visible})
        return pending[-keep:] if keep else "", False


    @staticmethod
    def _clean_content(content: str) -> str:
        text = content.strip()
        if "</think>" in text:
            text = text.rsplit("</think>", 1)[-1]
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
        return text.strip()
