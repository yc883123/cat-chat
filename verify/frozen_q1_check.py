# -*- coding: utf-8 -*-
"""冻结版实跑自检：确认 Q1（本地模型上下文溢出的用户可见报错）真的进了 exe。

用法：`dist\\naiba-chat.exe --run-skill-script <本文件绝对路径>`
只断言**运行期可达**的东西（冻结版把源码打进 PYZ，盘上没有可读 `.py`，不能读源码文本）——
源码级断言在 `tests/test_context_overflow.py`，真链路在 `verify/q1_context_smoke.py`。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

# 冻结版没打 unittest（`import unittest.mock` 会 ModuleNotFoundError），这里手写最小替身。

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition or not detail else f'  -> {detail}'}")
    if not condition:
        ok = False


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

# ---- 判定层与文案 ----------------------------------------------------------
check("溢出特征串命中 llama.cpp 错误体", is_context_overflow(LLAMA_CPP_400))
check("普通错误不误判", not is_context_overflow("Invalid API key"))
check("能从错误体解析后端自报窗口",
      _window_from_error_detail(LLAMA_CPP_400) == 4096,
      str(_window_from_error_detail(LLAMA_CPP_400)))
text = context_overflow_message("冻结探针本地模型", 4096, is_local=True)
check("文案含窗口与下一步", "4096 tokens" in text and "新会话" in text, text[:120])

# ---- 流内错误不再被吞 ------------------------------------------------------
try:
    StreamMixins._read_ollama_stream(
        [json.dumps({"error": "the input length exceeds the context length (n_ctx: 2048)"}).encode("utf-8")],
        None,
    )
    check("Ollama 流内错误转 ContextOverflowError", False, "没有抛异常")
except ContextOverflowError:
    check("Ollama 流内错误转 ContextOverflowError", True)
except Exception as exc:  # noqa: BLE001
    check("Ollama 流内错误转 ContextOverflowError", False, repr(exc))

try:
    StreamMixins._read_sse_response(
        [("data: " + json.dumps({"error": json.loads(LLAMA_CPP_400)["error"]})).encode("utf-8")],
        "llama_cpp",
        None,
    )
    check("SSE 内嵌错误转 ContextOverflowError", False, "没有抛异常")
except ContextOverflowError:
    check("SSE 内嵌错误转 ContextOverflowError", True)
except Exception as exc:  # noqa: BLE001
    check("SSE 内嵌错误转 ContextOverflowError", False, repr(exc))


# ---- 真链路：ModelRuntime.complete 撞 400 ----------------------------------
def _fake_open(request, timeout, cancel_event=None, opener=None):
    raise urllib.error.HTTPError(
        "http://127.0.0.1:8080/v1/chat/completions", 400, "Bad Request",
        {"Content-Type": "application/json"}, io.BytesIO(LLAMA_CPP_400.encode("utf-8")),
    )


profile = {
    "kind": "local", "name": "冻结探针本地模型",
    "base_url": "http://127.0.0.1:8080", "model": "qwen3-8b",
    "request_format": "llama_cpp", "context_window": 32768,
}
with tempfile.TemporaryDirectory(prefix="frozen_q1_") as tmp:
    original_open = ModelRuntime._urlopen_cancelable
    previous_dump = os.environ.get("NAIBA_ERROR_DUMP_DIR")
    ModelRuntime._urlopen_cancelable = staticmethod(_fake_open)
    os.environ["NAIBA_ERROR_DUMP_DIR"] = tmp
    try:
        ModelRuntime().complete(profile, [{"role": "user", "content": "ping"}], {"stream": False})
        check("HTTP 400 溢出走 ContextOverflowError", False, "没有抛异常")
    except ContextOverflowError as exc:
        message = str(exc)
        check("HTTP 400 溢出走 ContextOverflowError", True)
        check("文案用后端自报窗口（4096，不是配置的 32768）", "4096 tokens" in message, message[:120])
        check("文案含行动指引", "新会话" in message and "设置 → 模型" in message, message[:120])
        check("文案不再甩原始 JSON", "exceed_context_size_error" not in message, message[:120])
    except Exception as exc:  # noqa: BLE001
        check("HTTP 400 溢出走 ContextOverflowError", False, repr(exc))
    finally:
        ModelRuntime._urlopen_cancelable = original_open
        if previous_dump is None:
            os.environ.pop("NAIBA_ERROR_DUMP_DIR", None)
        else:
            os.environ["NAIBA_ERROR_DUMP_DIR"] = previous_dump

# ---- Agent 侧：截断成因区分 -------------------------------------------------
try:
    from naiba.skills.agent import _truncation_info  # noqa: E402

    hit = _truncation_info("length", "正文", usage={"total_tokens": 4000}, limit=4096, local=True)
    miss = _truncation_info("length", "正文", usage={"total_tokens": 4000}, limit=4096, local=False)
    check("本地贴窗口判 context", hit.get("cause") == "context", json.dumps(hit, ensure_ascii=False))
    check("在线不判 context（口径不变）", miss.get("cause") == "output", json.dumps(miss, ensure_ascii=False))
except Exception as exc:  # noqa: BLE001
    check("可 import naiba.skills.agent", False, repr(exc))

# ---- 打包资源：前端分文案的代码必须在包内 -----------------------------------
try:
    from naiba.paths import default_path_context  # noqa: E402

    paths = default_path_context()
    media_js = (Path(paths.public_dir) / "js" / "03-media.js").read_text(encoding="utf-8")
    check("打包资源含 cause 分支", "truncated.cause" in media_js and "cause === 'context'" in media_js)
    check("打包资源含新文案", "上下文窗口已耗尽" in media_js)
except Exception as exc:  # noqa: BLE001
    check("可读到打包内的 03-media.js", False, repr(exc))

print("\n全部通过" if ok else "\n存在未通过项")
sys.exit(0 if ok else 1)
