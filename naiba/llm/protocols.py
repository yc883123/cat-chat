"""模型协议适配层：各家 request_format 的 wire 序列化/解析（自 naiba.llm.runtime 迁出）。

纯函数层（staticmethod 集合，原样搬移，零语义变化）：不触碰网络/锁/状态；
ModelRuntime 通过继承本 Mixin 复用，运行时类名限定引用（self./ClassName.）按 MRO 解析。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
from typing import Any

logger = logging.getLogger("naiba.model_runtime")

# 空 CoT 的工具调用轮必须回传**非空** reasoning_text（DeepSeek Responses + tools + 思考模式，
# 2026-09-10 实测：空串/不回传都 400，占位文本通过）。常量串保证跨轮字节稳定、利于前缀缓存。
NO_REASONING_PLACEHOLDER = "(no reasoning content)"


class ProtocolMixins:
    @staticmethod
    def _content_parts(content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content or "")}]
        return [part for part in content if isinstance(part, dict)]


    @staticmethod
    def _content_text(content: Any) -> str:
        return "\n".join(
            str(part.get("text") or "")
            for part in ProtocolMixins._content_parts(content)
            if part.get("type") == "text" and part.get("text")
        )


    @staticmethod
    def _openai_content(content: Any) -> Any:
        if isinstance(content, str):
            return content
        converted = []
        for part in ProtocolMixins._content_parts(content):
            if part.get("type") == "text":
                converted.append({"type": "text", "text": str(part.get("text") or "")})
            elif part.get("type") == "image" and part.get("data"):
                converted.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{part.get('media_type') or 'image/jpeg'};base64,{part['data']}"
                        },
                    }
                )
        return converted


    @staticmethod
    def _openai_messages(
        messages: list[dict[str, Any]],
        include_reasoning_content: bool = False,
    ) -> list[dict[str, Any]]:
        converted = []
        for item in messages:
            role = str(item.get("role") or "user")
            if role == "tool":
                converted.append({
                    "role": "tool",
                    "tool_call_id": str(item.get("tool_call_id") or ""),
                    "content": ProtocolMixins._content_text(item.get("content")),
                })
                continue
            message = {"role": role, "content": ProtocolMixins._openai_content(item.get("content"))}
            if role == "assistant":
                reasoning_content = item.get("reasoning_content")
                if reasoning_content is None:
                    reasoning_content = item.get("reasoning")
                if reasoning_content is not None or include_reasoning_content:
                    message["reasoning_content"] = (
                        "" if reasoning_content is None else str(reasoning_content)
                    )
            if role == "assistant" and isinstance(item.get("tool_calls"), list):
                message["tool_calls"] = [
                    {
                        "id": str(call.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or ""),
                            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                    for call in item["tool_calls"] if isinstance(call, dict)
                ]
            converted.append(message)
        return converted


    @staticmethod
    def _is_deepseek_profile(profile: dict[str, Any]) -> bool:
        """Return whether an OpenAI-compatible profile speaks DeepSeek's
        thinking-mode dialect, which requires assistant reasoning_content on
        every replayed assistant message (including an empty value)."""
        base_url = str(profile.get("base_url") or "").lower()
        model = str(profile.get("model") or "").lower()
        return "deepseek" in model or "deepseek.com" in base_url

    @staticmethod
    def _is_kimi_k3_profile(profile: dict[str, Any]) -> bool:
        """Return whether the profile targets Kimi K3（月之暗面）.

        判定只看模型名：K3 是月之暗面唯一接受 ``reasoning_effort`` 的模型
        （官方取值 low/high/max、默认 max、思考永远开启）；K2.x 用 ``thinking``
        参数、收到 ``reasoning_effort`` 会报错，绝不能按 K3 方言发。
        """
        model = str(profile.get("model") or "").lower()
        return "kimi-k3" in model


    @staticmethod
    def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted = []
        for item in messages:
            role = str(item.get("role") or "user")
            if role == "tool":
                converted.append({
                    "type": "function_call_output",
                    "call_id": str(item.get("tool_call_id") or ""),
                    "output": ProtocolMixins._content_text(item.get("content")),
                })
                continue
            if role == "assistant":
                # DeepSeek 思考模式：携带 tools 的请求，历史轮次推理必须回传，否则 400
                # "The reasoning_text in the thinking mode must be passed back"。
                # 形态为官方 schema：reasoning item 的 content 为 reasoning_text 内容块列表
                # （"以明文承载思维链内容"；content 传字符串会被 serde 拒 "expected a sequence"），
                # 且 output item 带唯一 id（OpenAI 规范 input 侧 reasoning.id required）。
                # 2026-09-10 实测矩阵（真实 API，4 轮工具链）：
                #   带 tool_calls 的轮 → 不回传 reasoning item            → 400
                #   带 tool_calls 的轮 → 回传 text="" 的 reasoning item   → 400
                #   带 tool_calls 的轮 → 回传 text=" " 或占位文本          → 200
                #   无 tool_calls 的轮 → 不传/空文本都不影响                → 200
                # 结论：**带 tool_calls 的 assistant 消息无条件产出 reasoning item，
                # 且 text 必须非空**（服务端会返回"无 CoT 的工具调用轮"，此时用常量占位）。
                reasoning_text = item.get("reasoning_content")
                if reasoning_text is None:
                    reasoning_text = item.get("reasoning")
                if isinstance(item.get("tool_calls"), list):
                    text = str(reasoning_text) if reasoning_text is not None else ""
                    if not text.strip():
                        text = NO_REASONING_PLACEHOLDER
                    converted.append(ProtocolMixins._reasoning_item(text, item))
                    # reasoning/assistant/function_call 相邻成组；function_call 与
                    # function_call_output 保持相邻配对（中间不可插入 item）。
                    converted.append({
                        "role": "assistant",
                        "content": ProtocolMixins._responses_content(item.get("content"), role),
                    })
                    converted.extend({
                        "type": "function_call",
                        "call_id": str(call.get("id") or ""),
                        "name": str(call.get("name") or ""),
                        "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                    } for call in item["tool_calls"] if isinstance(call, dict))
                    continue
                if reasoning_text is not None and str(reasoning_text).strip():
                    converted.append(ProtocolMixins._reasoning_item(reasoning_text, item))
                converted.append({
                    "role": role,
                    "content": ProtocolMixins._responses_content(item.get("content"), role),
                })
                continue
            converted.append({
                "role": role,
                "content": ProtocolMixins._responses_content(item.get("content"), role),
            })
        return converted


    @staticmethod
    def _tool_schemas(tools: Any, request_format: str) -> list[dict[str, Any]]:
        """Convert ToolRegistry rows to the provider's native function schema."""
        rows = tools if isinstance(tools, list) else []
        converted: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("name") or "").strip():
                continue
            name = str(row["name"])
            description = str(row.get("description") or "")
            parameters = row.get("parameters") or {"type": "object", "properties": {}}
            if request_format == "codex_responses":
                converted.append({
                    "type": "function",
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                    "strict": False,
                })
            elif request_format == "gemini":
                converted.append({
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                })
            elif request_format == "claude":
                converted.append({
                    "name": name,
                    "description": description,
                    "input_schema": parameters,
                })
            else:
                converted.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters,
                    },
                })
        return converted


    @staticmethod
    def _responses_content(content: Any, role: str) -> Any:
        if isinstance(content, str):
            return content
        converted = []
        for part in ProtocolMixins._content_parts(content):
            if part.get("type") == "text":
                converted.append(
                    {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": str(part.get("text") or ""),
                    }
                )
            elif role != "assistant" and part.get("type") == "image" and part.get("data"):
                converted.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{part.get('media_type') or 'image/jpeg'};base64,{part['data']}",
                    }
                )
        return converted


    @staticmethod
    def _ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted = []
        for item in messages:
            content = item.get("content")
            role = str(item.get("role") or "user")
            message = {
                "role": role,
                "content": ProtocolMixins._content_text(content) if not isinstance(content, str) else content,
            }
            if role == "assistant" and isinstance(item.get("tool_calls"), list):
                message["tool_calls"] = [{
                    "function": {
                        "name": str(call.get("name") or ""),
                        "arguments": call.get("arguments") or {},
                    }
                } for call in item["tool_calls"] if isinstance(call, dict)]
            if role == "tool":
                message["tool_name"] = str(item.get("name") or "")
            images = [
                str(part.get("data") or "")
                for part in ProtocolMixins._content_parts(content)
                if part.get("type") == "image" and part.get("data")
            ]
            if images:
                message["images"] = images
            converted.append(message)
        return converted


    @staticmethod
    def _gemini_message(item: dict[str, Any]) -> dict[str, Any]:
        role = str(item.get("role") or "user")
        if role == "tool":
            try:
                response = json.loads(ProtocolMixins._content_text(item.get("content")))
            except (json.JSONDecodeError, TypeError):
                response = {"result": ProtocolMixins._content_text(item.get("content"))}
            return {
                "role": "user",
                "parts": [{"functionResponse": {"name": str(item.get("name") or ""), "response": response}}],
            }
        if role == "assistant" and isinstance(item.get("tool_calls"), list):
            return {
                "role": "model",
                "parts": [{"functionCall": {
                    "name": str(call.get("name") or ""),
                    "args": call.get("arguments") or {},
                }} for call in item["tool_calls"] if isinstance(call, dict)],
            }
        return {
            "role": "model" if role == "assistant" else "user",
            "parts": [
                {"text": str(part.get("text") or "")}
                if part.get("type") == "text"
                else {"inlineData": {"mimeType": part.get("media_type") or "image/jpeg", "data": part.get("data") or ""}}
                for part in ProtocolMixins._content_parts(item.get("content"))
                if part.get("type") == "text" or (part.get("type") == "image" and part.get("data"))
            ],
        }


    @staticmethod
    def _claude_message(item: dict[str, Any]) -> dict[str, Any]:
        role = str(item.get("role") or "user")
        if role == "tool":
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": str(item.get("tool_call_id") or ""),
                    "content": ProtocolMixins._content_text(item.get("content")),
                }],
            }
        if role == "assistant" and isinstance(item.get("tool_calls"), list):
            return {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": str(call.get("id") or ""),
                    "name": str(call.get("name") or ""),
                    "input": call.get("arguments") or {},
                } for call in item["tool_calls"] if isinstance(call, dict)],
            }
        return {
            "role": "assistant" if role == "assistant" else "user",
            "content": [
                {"type": "text", "text": str(part.get("text") or "")}
                if part.get("type") == "text"
                else {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.get("media_type") or "image/jpeg",
                        "data": part.get("data") or "",
                    },
                }
                for part in ProtocolMixins._content_parts(item.get("content"))
                if part.get("type") == "text" or (part.get("type") == "image" and part.get("data"))
            ],
        }


    @staticmethod
    def _lm_studio_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """LM Studio 原生 /api/v1/chat 序列化。

        - system 消息提取到顶层 ``system_prompt``；
        - 其余消息的内容统一展开为结构化部件：文本 ``{"type":"text","content":...}``、
          图片 ``{"type":"image","data_url":"..."}``。不再使用 OpenAI ``messages``/``image_url``。
        """
        system_parts: list[str] = []
        input_parts: list[dict[str, Any]] = []
        for item in messages:
            role = str(item.get("role") or "user")
            content = item.get("content")
            if role == "system":
                system_parts.append(ProtocolMixins._content_text(content))
                continue
            for part in ProtocolMixins._content_parts(content):
                if part.get("type") == "text":
                    text = str(part.get("text") or "")
                    if text:
                        input_parts.append({"type": "text", "content": text})
                elif part.get("type") == "image" and part.get("data"):
                    input_parts.append(
                        {
                            "type": "image",
                            "data_url": f"data:{part.get('media_type') or 'image/jpeg'};base64,{part['data']}",
                        }
                    )
        system_prompt = "\n\n".join(p.strip() for p in system_parts if p.strip())
        return system_prompt, input_parts


    # 「有推理零正文」空流重试时的思考强度降档链：high→medium→low 逐级下探。
    # off 本来就不发思考、auto 未发任何强度参数，二者都「无档可降」。
    _LOWER_REASONING_EFFORT = {"high": "medium", "medium": "low"}

    @staticmethod
    def _lower_reasoning_effort(effort: str) -> str:
        """返回下一档思考强度；不可降（off/low/auto/未知）时返回空串。"""
        return ProtocolMixins._LOWER_REASONING_EFFORT.get((effort or "").strip().lower(), "")

    @staticmethod
    def _reasoning_params(
        request_format: str, effort: str, deepseek: bool = False, kimi_k3: bool = False,
    ) -> dict[str, Any]:
        """把思维强度映射为各供应商协议字段；``auto`` 不发送任何参数。"""
        effort = (effort or "auto").strip().lower()
        if effort not in {"off", "low", "medium", "high"}:
            return {}
        if request_format == "lm_studio":
            # LM Studio 原生 API 支持 off/low/medium/high/on。
            return {"reasoning": effort if effort != "off" else "off"}
        if request_format == "ollama":
            # Ollama 支持布尔值以及 low/medium/high；保留用户选择的强度。
            return {"think": False if effort == "off" else effort}
        if request_format == "openai_chat":
            if kimi_k3:
                # Kimi K3 官方方言：reasoning_effort 只认 low/high/max（默认 max），
                # 且思考永远开启、无法关闭——off 落到最低档 low 是最贴近的语义。
                # 应用四档 off/low/medium/high 据此映射（最高档 high→max）。
                # 注意 K2.x 不接受 reasoning_effort（会 400），此分支仅 K3 可走。
                mapping = {"off": "low", "low": "low", "medium": "high", "high": "max"}
                return {"reasoning_effort": mapping[effort]}
            # OpenAI 仅支持 low/medium/high；off 视为不启用（不发送字段）。
            if effort == "off":
                return {}
            return {"reasoning_effort": effort}
        if request_format == "codex_responses":
            if deepseek:
                # DeepSeek Responses API 的 effort 取值：none/low/high/max。
                # 应用四档 off/low/medium/high 据此映射（最高档 high→max，off→none 真正关思考）。
                mapping = {"off": "none", "low": "low", "medium": "high", "high": "max"}
                return {"reasoning": {"effort": mapping[effort]}}
            # OpenAI Codex Responses：低/中/高三档。
            # （Kimi K3 无 Responses API；经中继走此格式时按 OpenAI 方言透传，
            #  由中继自行翻译，不做 K3 特判。）
            if effort == "off":
                return {}
            return {"reasoning": {"effort": effort}}
        # gemini / claude 首期保持自动，不发送未验证字段。
        return {}


    @staticmethod
    def _stream_delta_full(request_format: str, chunk: dict[str, Any]) -> tuple[str, str, list[dict[str, str]]]:
        """Like ``_stream_delta`` but also extracts native OpenAI ``tool_calls``.

        Returns ``(text, reasoning, tool_calls)`` where ``tool_calls`` is a list
        of ``{"index", "id", "name", "arguments"}`` dicts accumulated by the caller.
        """
        text, reasoning = ProtocolMixins._stream_delta(request_format, chunk)
        tool_calls: list[dict[str, str]] = []
        if request_format in {"openai_chat", "lm_studio"}:
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices and isinstance(choices[0], dict) else {}
            for raw_call in delta.get("tool_calls") or []:
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function") or {}
                tool_calls.append(
                    {
                        "index": int(raw_call.get("index", 0) or 0),
                        "id": str(raw_call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": function.get("arguments") if isinstance(function.get("arguments"), str) else "",
                    }
                )
        elif request_format == "codex_responses":
            event_type = str(chunk.get("type") or "")
            if event_type == "response.output_item.added":
                item = chunk.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "function_call":
                    tool_calls.append({
                        "index": int(chunk.get("output_index", 0) or 0),
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or ""),
                    })
            elif event_type == "response.function_call_arguments.delta":
                tool_calls.append({
                    "index": int(chunk.get("output_index", 0) or 0),
                    "id": str(chunk.get("item_id") or ""),
                    "name": "",
                    "arguments": str(chunk.get("delta") or ""),
                })
        elif request_format == "claude":
            event_type = str(chunk.get("type") or "")
            index = int(chunk.get("index", 0) or 0)
            if event_type == "content_block_start":
                block = chunk.get("content_block") or {}
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    raw_input = block.get("input") or {}
                    tool_calls.append({
                        "index": index,
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(raw_input, ensure_ascii=False) if raw_input else "",
                    })
            elif event_type == "content_block_delta":
                delta = chunk.get("delta") or {}
                if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                    tool_calls.append({
                        "index": index,
                        "id": "",
                        "name": "",
                        "arguments": str(delta.get("partial_json") or ""),
                    })
        return text, reasoning, tool_calls


    @staticmethod
    def _build_action_from_native_tool_calls(native_tool_calls: dict[int, dict[str, str]]) -> str:
        """Reassemble streamed OpenAI ``tool_calls`` into the internal action JSON.

        Raises ``RuntimeError`` on malformed/unsupported calls so the original
        protocol is never forwarded to the chat answer.
        """
        actions = []
        for call in (native_tool_calls[index] for index in sorted(native_tool_calls)):
            name = call["name"].strip()
            if not name:
                logger.warning("工具调用解析失败：缺少工具名")
                raise RuntimeError("工具调用解析失败：缺少工具名")
            raw_args = call["arguments"] or "{}"
            try:
                arguments = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as exc:
                # Large native tool-call arguments (e.g. a full workflow JSON) can
                # be truncated/malformed mid-stream. Do NOT abort the whole run with
                # a fatal error — surface a retryable "parse_error" action so the
                # agent loop asks the model to re-emit a clean tool call.
                logger.warning(
                    "工具调用解析失败：参数不是合法 JSON（len=%d, head=%r…）：%s",
                    len(raw_args), raw_args[:120], exc,
                )
                return json.dumps({"type": "parse_error"}, ensure_ascii=False)
            if not isinstance(arguments, dict):
                logger.warning("工具调用解析失败：参数不是 JSON 对象")
                return json.dumps({"type": "parse_error"}, ensure_ascii=False)
            actions.append({"type": "tool", "tool": name, "arguments": arguments})
        payload = actions[0] if len(actions) == 1 else {"type": "tools", "calls": actions}
        return json.dumps(payload, ensure_ascii=False)


    @staticmethod
    def _stream_delta(request_format: str, chunk: dict[str, Any]) -> tuple[str, str]:
        if request_format in {"openai_chat", "lm_studio"}:
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices and isinstance(choices[0], dict) else {}
            return (
                ProtocolMixins._text_value(delta.get("content")),
                ProtocolMixins._text_value(
                    delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
                ),
            )
        if request_format == "codex_responses":
            event_type = str(chunk.get("type") or "")
            if event_type.endswith("output_text.delta"):
                return str(chunk.get("delta") or ""), ""
            if "reasoning" in event_type and event_type.endswith("delta"):
                return "", str(chunk.get("delta") or "")
            return "", ""
        if request_format == "claude":
            delta = chunk.get("delta") or {}
            if str(chunk.get("type") or "") == "content_block_delta":
                return str(delta.get("text") or ""), str(delta.get("thinking") or "")
        return "", ""


    @staticmethod
    def _online_usage(request_format: str, result: Any) -> dict[str, int]:
        """把各供应商的 token usage 统一为输入、输出、总量和缓存命中数。"""
        chunks = result if isinstance(result, list) else [result]
        usage: dict[str, Any] = {}
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            candidate = chunk.get("usage") or chunk.get("usageMetadata") or chunk.get("stats")
            if request_format == "ollama" and any(
                key in chunk for key in ("prompt_eval_count", "eval_count")
            ):
                candidate = chunk
            # Responses 流式：usage 随最后一条 response.completed / response.incomplete
            # 事件嵌套在 response 对象里，不在顶层 chunk 上。
            if candidate is None and request_format == "codex_responses":
                response_obj = chunk.get("response")
                if isinstance(response_obj, dict):
                    candidate = response_obj.get("usage")
            if isinstance(candidate, dict):
                usage = candidate
        if not usage:
            return {}

        def number(*keys: str) -> int:
            for key in keys:
                value = usage.get(key)
                if value is not None:
                    try:
                        return max(0, int(value))
                    except (TypeError, ValueError):
                        continue
            return 0

        input_tokens = number(
            "prompt_tokens", "input_tokens", "inputTokens", "promptTokenCount", "inputTokenCount", "prompt_eval_count"
        )
        output_tokens = number(
            "completion_tokens", "output_tokens", "outputTokens", "candidatesTokenCount", "outputTokenCount", "eval_count"
        )
        total_tokens = number("total_tokens", "totalTokens", "totalTokenCount") or input_tokens + output_tokens
        cached_tokens = number(
            "cached_tokens", "cache_read_input_tokens", "cachedContentTokenCount",
            # DeepSeek reports the context-cache prefix hit via its own field.
            "prompt_cache_hit_tokens",
        )
        if request_format == "claude":
            # Anthropic separately reports uncached, cache-created and cache-read input tokens.
            input_tokens += cached_tokens + number("cache_creation_input_tokens")
            total_tokens = input_tokens + output_tokens
        for detail_key in ("prompt_tokens_details", "input_tokens_details"):
            details = usage.get(detail_key)
            if isinstance(details, dict):
                try:
                    cached_tokens = max(cached_tokens, int(details.get("cached_tokens") or 0))
                except (TypeError, ValueError):
                    pass
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": min(cached_tokens, input_tokens) if input_tokens else cached_tokens,
        }


    # 各供应商"撞输出上限"的不同写法，统一归一为 "length"（Agent 循环只认这一个截断信号）。
    # responses 的 "incomplete" 绝大多数就是 max_output_tokens，故并入 length 处理。
    _LENGTH_FINISH_REASONS = frozenset(
        {"length", "max_tokens", "max_output_tokens", "token_limit", "incomplete"}
    )

    @staticmethod
    def _online_finish_reason(request_format: str, result: Any) -> str:
        """提取模型响应的终止原因（各供应商字段名归一；取最后一个非空值）。

        为什么必须记录它：「正文中途停住」这件事，在界面上无法区分是模型自己收尾、还是撞上
        输出上限被切——没有这个字段就只能靠猜（实测排查一次截断事故时，全链路没有任何终止
        原因留痕，只能反向推断）。返回 ``""`` 表示供应商没给（流被掐断/中继吞字段），
        这个「没有原因」本身也是值得留档的信号。

        与 ``_online_usage`` 同构：``result`` 既可能是单个响应对象，也可能是流式 chunk 列表。
        """
        chunks = result if isinstance(result, list) else [result]
        finish = ""
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            holders: list[dict[str, Any]] = [chunk]
            if isinstance(chunk.get("response"), dict):
                # Responses 流：status / incomplete_details 嵌在 response 对象里。
                holders.append(chunk["response"])
            for holder in holders:
                for key in ("done_reason", "finish_reason", "stop_reason", "finishReason", "status"):
                    value = holder.get(key)
                    if isinstance(value, dict):
                        value = value.get("reason")
                    if value:
                        finish = str(value)
                choices = holder.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        value = choice.get("finish_reason") or choice.get("stop_reason")
                        if value:
                            finish = str(value)
                candidates = holder.get("candidates")
                if isinstance(candidates, list):
                    for candidate in candidates:
                        if isinstance(candidate, dict) and candidate.get("finishReason"):
                            finish = str(candidate["finishReason"])
        normalized = str(finish or "").strip().lower()
        if normalized in ProtocolMixins._LENGTH_FINISH_REASONS:
            return "length"
        return normalized

    @staticmethod
    def _openai_tool_calls_action(result: Any, request_format: str) -> str | None:
        """Extract native OpenAI ``tool_calls`` from a non-streaming response.

        Returns the internal action JSON string, or ``None`` when the response
        has no tool calls. Raises ``RuntimeError`` on malformed calls.
        """
        if request_format not in {"openai_chat", "lm_studio", "ollama"} or not isinstance(result, dict):
            return None
        if request_format == "ollama":
            message = result.get("message") or {}
        else:
            choices = result.get("choices") or []
            message = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(tool_calls, list) or not tool_calls:
            return None
        calls: list[dict[str, str]] = []
        for raw_call in tool_calls:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function") or {}
            raw_arguments = function.get("arguments", "")
            calls.append(
                {
                    "name": str(function.get("name") or ""),
                    "arguments": (
                        raw_arguments if isinstance(raw_arguments, str)
                        else json.dumps(raw_arguments, ensure_ascii=False)
                    ),
                }
            )
        native = {
            index: {"id": "", "name": call["name"], "arguments": call["arguments"]}
            for index, call in enumerate(calls)
        }
        return ProtocolMixins._build_action_from_native_tool_calls(native)


    @staticmethod
    def _responses_tool_calls_action(result: Any, request_format: str) -> str | None:
        """Extract a native function call from a non-streaming Responses API result."""
        if request_format != "codex_responses" or not isinstance(result, dict):
            return None
        calls = [
            item for item in result.get("output") or []
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        if not calls:
            return None
        native = {
            index: {
                "id": str(call.get("call_id") or call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "arguments": str(call.get("arguments") or ""),
            }
            for index, call in enumerate(calls)
        }
        return ProtocolMixins._build_action_from_native_tool_calls(native)


    @staticmethod
    def _gemini_tool_calls_action(result: Any, request_format: str) -> str | None:
        if request_format != "gemini":
            return None
        chunks = result if isinstance(result, list) else [result]
        calls: list[dict[str, Any]] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            candidates = chunk.get("candidates") or []
            content = (candidates[0].get("content") or {}) if candidates and isinstance(candidates[0], dict) else {}
            for part in content.get("parts") or []:
                call = part.get("functionCall") if isinstance(part, dict) else None
                if isinstance(call, dict):
                    calls.append(call)
        if not calls:
            return None
        native = {
            index: {
                "id": "",
                "name": str(call.get("name") or ""),
                "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
            }
            for index, call in enumerate(calls)
        }
        return ProtocolMixins._build_action_from_native_tool_calls(native)


    @staticmethod
    def _claude_tool_calls_action(result: Any, request_format: str) -> str | None:
        if request_format != "claude" or not isinstance(result, dict):
            return None
        calls = [
            block for block in result.get("content") or []
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if not calls:
            return None
        native = {
            index: {
                "id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "arguments": json.dumps(call.get("input") or {}, ensure_ascii=False),
            }
            for index, call in enumerate(calls)
        }
        return ProtocolMixins._build_action_from_native_tool_calls(native)


    @staticmethod
    def _claude_apply_cache_control(messages: list[dict[str, Any]]) -> None:
        """在最后一条"非 tool_result 的 user 消息"末尾追加 cache_control，启用前缀缓存。

        Anthropic 的 prompt caching 必须显式标记 ``cache_control: {"type": "ephemeral"}``
        才生效。约束：
        - 只能标记在 user 角色消息上（assistant 之后内容会打断缓存）；
        - 绝不能标记在含 tool_result 的 user 消息上（否则 API 400）。
        """
        for message in reversed(messages):
            if str(message.get("role") or "") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                if not content:
                    continue
                message["content"] = [{"type": "text", "text": content}]
                content = message["content"]
            if not isinstance(content, list) or not content:
                continue
            if any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                continue
            last_block = content[-1]
            if isinstance(last_block, dict):
                last_block["cache_control"] = {"type": "ephemeral"}
            return


    @staticmethod
    def _reasoning_action(reasoning: str) -> str:
        """只接受 reasoning_content 中完整、合法的 Agent 动作 JSON。"""
        cleaned = str(reasoning or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            action = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(action, dict):
            return ""
        action_type = action.get("type")
        if action_type == "tool":
            if not str(action.get("tool") or "").strip():
                return ""
            if "arguments" in action and not isinstance(action.get("arguments"), dict):
                return ""
        elif action_type == "final":
            if not str(action.get("content") or "").strip():
                return ""
        else:
            return ""
        return json.dumps(action, ensure_ascii=False)


    @staticmethod
    def _with_endpoint(base_url: str, suffix: str) -> str:
        parsed = urllib.parse.urlsplit(base_url)
        target_path = suffix if suffix.startswith("/") else f"/{suffix}"
        base_path = parsed.path.rstrip("/")
        if base_path.endswith(target_path):
            return base_url
        path = ""
        for marker in ("/api/v1/", "/v1beta/", "/v1/"):
            if not target_path.startswith(marker):
                continue
            marker_root = marker.rstrip("/")
            rest = target_path[len(marker_root):]  # e.g. "/chat/completions"
            if base_path.endswith(marker_root):
                path = base_path + rest
                break
            marker_index = base_path.rfind(marker)
            if marker_index >= 0:
                path = base_path[:marker_index] + target_path
                break
            # base_url already carries an endpoint path (e.g. a full chat URL
            # pasted in) but omitted the API-version marker. Re-root it onto the
            # versioned target instead of doubling the path, which would otherwise
            # produce broken URLs such as .../chat/completions/v1/chat/completions.
            if base_path.endswith(rest):
                path = target_path
                break
        if not path:
            path = base_path + target_path
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


    @staticmethod
    def _local_endpoint(base_url: str, path: str) -> str:
        parsed = urllib.parse.urlsplit(base_url)
        base_path = parsed.path.rstrip("/")
        if base_path.endswith("/v1"):
            base_path = base_path[:-3]
        target_path = f"{base_path}{path}" if base_path else path
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, target_path, parsed.query, parsed.fragment)
        )


    @staticmethod
    def _reasoning_item(reasoning_text: Any, source_item: dict[str, Any]) -> dict[str, Any]:
        """构造回传用 reasoning item：content 为 reasoning_text 内容块列表（明文承载）。

        id 优先服务端真实 id（流式 output_item.added / delta item_id 捕获，非流式 output
        提取）；无 id 时合成确定性 id（rs_h_ + sha1）——key 取推理文本，占位文本除外
        （占位串对所有空 CoT 轮都一样，用它做 key 会撞 id），改用工具调用签名（含 call_id，
        逐轮唯一），跨轮 trace 重放字节稳定。
        """
        reasoning_id = str(source_item.get("reasoning_id") or "")
        text = str(reasoning_text or "")
        if not reasoning_id:
            if text and text != NO_REASONING_PLACEHOLDER:
                digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
            else:
                tools_signature = json.dumps(
                    source_item.get("tool_calls") or [], ensure_ascii=False, sort_keys=True,
                )
                digest = hashlib.sha1(tools_signature.encode("utf-8")).hexdigest()
            reasoning_id = f"rs_h_{digest[:16]}"
        return {
            "type": "reasoning",
            "id": reasoning_id,
            "content": [{"type": "reasoning_text", "text": text}],
        }


    @staticmethod
    def _responses_reasoning_id(request_format: str, result: Any) -> str:
        """从非流式 Responses 响应中提取 reasoning item 的唯一 id（回传用）。"""
        if request_format != "codex_responses" or not isinstance(result, dict):
            return ""
        for item in result.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "reasoning":
                return str(item.get("id") or "")
        return ""


    @staticmethod
    def _online_reasoning(request_format: str, result: Any) -> str:
        """尽力从响应中提取推理/思考内容（reasoning_content / thinking 等），无则返回空。"""
        if not isinstance(result, dict):
            return ""
        if request_format in {"openai_chat", "ollama"}:
            choices = result.get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            for key in ("reasoning_content", "reasoning", "thinking", "thought"):
                value = message.get(key) or result.get(key)
                if value:
                    return ProtocolMixins._text_value(value)
            if request_format == "ollama":
                message = result.get("message") or {}
                if isinstance(message, dict) and message.get("thinking"):
                    return str(message["thinking"])
        if request_format in ("codex_responses", "lm_studio"):
            for item in result.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    return ProtocolMixins._text_value(item.get("content") or item.get("summary"))
        return ""


    @staticmethod
    def _online_content(request_format: str, result: Any) -> str:
        if request_format == "openai_chat":
            choices = result.get("choices") or [] if isinstance(result, dict) else []
            choice = choices[0] if choices else {}
            message = choice.get("message") or {}
            return ProtocolMixins._text_value(message.get("content") or choice.get("text"))

        if request_format == "ollama":
            if isinstance(result, list):
                return "".join(ProtocolMixins._online_content(request_format, item) for item in result)
            message = result.get("message") if isinstance(result, dict) else None
            return ProtocolMixins._text_value(message.get("content") if isinstance(message, dict) else "")

        if request_format == "codex_responses":
            if not isinstance(result, dict):
                return ""
            if result.get("output_text"):
                return str(result["output_text"])
            texts = []
            for output in result.get("output") or []:
                for item in output.get("content") or []:
                    text = item.get("text") if isinstance(item, dict) else ""
                    if text:
                        texts.append(str(text))
            return "\n".join(texts)

        if request_format == "gemini":
            chunks = result if isinstance(result, list) else [result]
            texts = []
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                for candidate in chunk.get("candidates") or []:
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        if isinstance(part, dict) and part.get("text"):
                            texts.append(str(part["text"]))
            return "".join(texts)

        if request_format == "claude":
            return ProtocolMixins._text_value(result.get("content") if isinstance(result, dict) else None)

        if request_format == "lm_studio":
            if not isinstance(result, dict):
                return ""
            if result.get("output_text"):
                return str(result["output_text"])
            output = result.get("output") or result.get("choices") or []
            texts = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                # 推理类部件（type == "reasoning"）不属于正文，单独由 _online_reasoning 提取。
                if str(item.get("type") or "") == "reasoning":
                    continue
                value = item.get("content") or (item.get("message") or {}).get("content") or item.get("text")
                text = ProtocolMixins._text_value(value)
                if text:
                    texts.append(text)
            return "\n".join(texts)
        return ""


    @staticmethod
    def _codex_responses_aggregated(
        chunks: list[dict[str, Any]],
    ) -> tuple[str, str, str, str]:
        """从 ``codex_responses`` 流式聚合事件提取 (正文, 思考, 最后一个 reasoning item id, tool_calls action)。

        中继/代理可能只回聚合事件而不逐段发 ``output_text.delta``，此时逐段解析拿不到
        正文。这里从已缓存的 chunk 里补回：仅认 ``response.completed``
        （``chunk["response"]["output"]``）与 ``response.output_item.done``
        （``chunk["item"]``）；优先采用前者（一次给全），仅当其缺失时回退后者，
        再按 item id 去重，避免两种事件同时出现时正文被拼接两次。
        显式忽略 ``response.incomplete``——被 token 上限/服务端中断截断的回答不得当作
        成功正文返回。只取 ``message`` 正文、``reasoning`` 思考（含服务端 id，供下一轮
        回传）与 ``function_call``（复用 ``_responses_tool_calls_action`` 组装为 Agent action）。
        """
        # ``response.output_item.done`` 可以在 ``response.incomplete`` 之前到达。
        # 后者表示整次 Responses 调用未完成，不能因为已有部分 item 结束就把截断内容
        # 回填为成功结果；但 ``response.completed`` 仍是优先且完整的成功终态。
        incomplete = any(
            isinstance(chunk, dict)
            and str(chunk.get("type") or "") == "response.incomplete"
            for chunk in chunks
        )
        items: list[dict[str, Any]] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            if str(chunk.get("type") or "") != "response.completed":
                continue
            response = chunk.get("response")
            if isinstance(response, dict):
                for item in response.get("output") or []:
                    if isinstance(item, dict):
                        items.append(item)
        if not items and not incomplete:
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                if str(chunk.get("type") or "") != "response.output_item.done":
                    continue
                item = chunk.get("item")
                if isinstance(item, dict):
                    items.append(item)

        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            key = str(item.get("id") or "")
            if not key:
                key = "|".join([
                    str(item.get("type") or ""),
                    str(item.get("call_id") or ""),
                    str(item.get("name") or ""),
                    str(item.get("content") or item.get("arguments") or "")[:128],
                ])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        texts: list[str] = []
        reasonings: list[str] = []
        reasoning_id = ""
        for item in deduped:
            item_type = str(item.get("type") or "")
            if item_type == "message":
                text = ProtocolMixins._text_value(item.get("content"))
                if text:
                    texts.append(text)
            elif item_type == "reasoning":
                text = ProtocolMixins._text_value(item.get("content") or item.get("summary"))
                if text:
                    reasonings.append(text)
                item_id = str(item.get("id") or "")
                if item_id:
                    reasoning_id = item_id
        action = ProtocolMixins._responses_tool_calls_action(
            {"output": deduped}, "codex_responses"
        ) or ""
        return "\n".join(texts), "\n".join(reasonings), reasoning_id, action


    @staticmethod
    def _text_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(
                str(item.get("text") or item.get("content") or "")
                for item in value if isinstance(item, dict) and (item.get("text") or item.get("content"))
            )
        return ""
