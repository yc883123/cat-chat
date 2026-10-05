# -*- coding: utf-8 -*-
"""只读探针：原生工具调用修复**不改变出站请求字节**的逐轮 SHA-256 对照（2026-10-05 乙篇 §5.2）。

跑法：``.venv\\Scripts\\python.exe verify/_probe_toolcall_prose_bytes.py``
（探针不进测试套件；结论进计划文档与测试注释。）

背景：2026-10-05 修复删掉了 ``naiba/llm/stream.py`` 工具调用分支里的
``guard.detected = True``（它让守卫在首个 tool_call 后永久闭嘴，界面只剩半句话）。
修复改的是「入站响应 → 界面」这一段；本探针证明**出站请求字节逐字节不变**：

1. 出站入口 ``naiba.net.open`` 打桩：捕获每个请求体（``Request.data`` 原字节），
   按脚本回放模型 SSE 流（全程不触网；脚本用尽后抛**非网络类**异常立刻终止，
   避免 NetIO 的重试语义把对照搅乱）；
2. 同一段「多轮原生工具调用」对话（3 个请求：2 次工具调用 + 1 次最终答复）跑两遍：
   修后 = 现行代码；修前 = 现行代码 + 「守卫打桩回旧语义」（``finish()`` 之后
   ``detected = True``，即旧代码被删掉的那一行——字节层面与真·修前等价；delta 序列
   与真·修前只差「同 chunk 并存的那段正文」，不影响请求体比对）；
3. 逐轮请求体算 SHA-256 并列比，另加两条直证：任何请求体都**不得出现**流里外发的
   正文（正文只进界面、不进历史），且工具轮的 assistant 消息（``tool_calls`` 载荷）
   必须**在**请求体里（防止「比对双方都空」的假绿）。

3 轮脚本覆盖修复的三个回归位：
- 轮1：tool_call 前后都有正文 + **同 chunk 正文与 tool_calls 并存**（A 方案）；
- 轮2：正文**全部**在 tool_call 之后（「一个字都不显示」的原始缺陷位）；
- 轮3：最终答复（不含工具调用）。

`uuid4` 打桩为确定性递增（工具调用 id 的随机后缀是两次运行唯一的天然字节差）；
`run_context`/工作区在两次运行里同进程同值，其余字节理论同源。
"""
from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import naiba.llm.stream as stream_mod  # noqa: E402
import naiba.net as net_mod  # noqa: E402
from naiba.llm.runtime import ModelRuntime  # noqa: E402
from naiba.skills import agent as agent_mod  # noqa: E402
from naiba.skills.agent import SkillAgent  # noqa: E402

TOOL = "ls_probe"
PROFILE = {
    "kind": "online",
    "name": "探针-中转",
    "base_url": "https://probe.relay.invalid",
    "model": "probe-tool-model",
    "request_format": "openai_chat",
    "api_key": "sk-probe",
}

PROSE_R1_BEFORE = "我先看看工作目录，顺便确认下有没有隐藏文件。"
PROSE_R1_SAME_CHUNK = "目录名有点长，我念一下。"
PROSE_R1_AFTER = "稍等，我把结果读出来再回答。"
PROSE_R2_AFTER_A = "再补一次目录扫描，看看子目录。"
PROSE_R2_AFTER_B = "这个子目录也正常。"
PROSE_R3_FINAL = "两个目录都查完了，一切正常。"
TOOLCALL_PROSE = (PROSE_R1_BEFORE, PROSE_R1_SAME_CHUNK, PROSE_R1_AFTER,
                  PROSE_R2_AFTER_A, PROSE_R2_AFTER_B, PROSE_R3_FINAL)
POST_CALL_PROSE = (PROSE_R1_AFTER, PROSE_R2_AFTER_A, PROSE_R2_AFTER_B)


def sse(chunk: dict) -> bytes:
    return ("data: " + json.dumps(chunk, ensure_ascii=False)).encode("utf-8")


def _script() -> list[list[bytes]]:
    """3 个请求的脚本化模型流：每元素是一轮的 SSE 行。"""
    return [
        [  # 轮1：前置正文 → 同 chunk 正文+tool_calls → 参数拆两包 → 后置正文
            sse({"choices": [{"delta": {"content": PROSE_R1_BEFORE}}]}),
            sse({"choices": [{"delta": {
                "content": PROSE_R1_SAME_CHUNK,
                "tool_calls": [{"index": 0, "id": "call_probe_1", "type": "function",
                                "function": {"name": TOOL, "arguments": ""}}],
            }}]}),
            sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "{\"path\": \"工作目录"}}]}}]}),
            sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "\"}"}}]}}]}),
            sse({"choices": [{"delta": {"content": PROSE_R1_AFTER}}]}),
            sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            b"data: [DONE]",
        ],
        [  # 轮2：正文全部在 tool_call 之后（原始缺陷位）
            sse({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_probe_2", "type": "function",
                "function": {"name": TOOL,
                             "arguments": "{\"path\": \"工作目录/子目录\"}"}}]}}]}),
            sse({"choices": [{"delta": {"content": PROSE_R2_AFTER_A}}]}),
            sse({"choices": [{"delta": {"content": PROSE_R2_AFTER_B}}]}),
            sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            b"data: [DONE]",
        ],
        [  # 轮3：最终答复
            sse({"choices": [{"delta": {"content": PROSE_R3_FINAL}}]}),
            sse({"choices": [{"delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 160, "completion_tokens": 24,
                           "total_tokens": 184, "prompt_cache_hit_tokens": 128}}),
            b"data: [DONE]",
        ],
    ]


class _FakeStream:
    """流式响应替身：按行迭代 bytes，够运行时读流与看门狗调用。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self.headers = {"Content-Type": "text/event-stream"}

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        return None

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _SeqUUID:
    """确定性 uuid4 替身：两次运行拿到同一批 hex，请求体才能逐字节比对。"""

    def __init__(self, number: int) -> None:
        self._number = number

    @property
    def hex(self) -> str:
        return f"{self._number:032x}"

    def __str__(self) -> str:
        return f"00000000-0000-4000-8000-{self._number:012d}"


class _OldGuard(stream_mod._ProtocolStreamGuard):
    """修前语义（守卫打桩）：工具调用分支 ``finish()`` 之后永久闭嘴。

    等价于旧代码里被删掉的那行 ``guard.detected = True``——那是本修复唯一的语义删除。
    """

    def finish(self, status) -> None:
        super().finish(status)
        self.detected = True


class _ProbeCatalog:
    def scan(self) -> list:
        return []

    def read_skill_content(self, path: str) -> str:  # pragma: no cover
        return ""


class _ProbeTools:
    """探针工具注册表：无副作用、结果逐次不同（否则先撞「无进展」熔断）。"""

    def __init__(self) -> None:
        self.calls = 0

    def schemas(self) -> list:
        return [{
            "name": TOOL,
            "description": "列出目录（探针替身，不做任何真实操作）",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径"}},
                "required": ["path"],
            },
        }]

    def side_effect(self, tool: str) -> bool:
        return False

    def media_declaration(self, tool: str) -> dict:
        return {"extract": "none", "policy": "never"}

    def execute(self, tool: str, arguments: dict, active: list, run_context: object):
        self.calls += 1
        return True, f"探针目录清单 #{self.calls}：{json.dumps(arguments, ensure_ascii=False)}"


def _run_conversation(*, old_semantics: bool) -> dict:
    captured: list[dict] = []
    events: list[dict] = []
    script = _script()

    def fake_net_open(request, timeout=None, **kwargs):
        captured.append({
            "url": getattr(request, "full_url", ""),
            "data": bytes(getattr(request, "data", b"") or b""),
        })
        index = len(captured) - 1
        if index >= len(script):
            # 非网络类异常：不会被 NetIO/运行时当成可重试故障，探针立刻停住暴露问题。
            raise RuntimeError("探针脚本已用尽：出现了计划外的请求")
        return _FakeStream(script[index])

    uuids = itertools.count(1)
    worker = SkillAgent(_ProbeCatalog(), None, ModelRuntime().complete, None)
    patches = [
        mock.patch.object(net_mod, "open", fake_net_open),
        # uuid4 确定性化：工具调用 id 的随机后缀是两次运行唯一的天然字节差。
        mock.patch.object(agent_mod.uuid, "uuid4", lambda: _SeqUUID(next(uuids))),
    ]
    if old_semantics:
        patches.append(mock.patch.object(stream_mod, "_ProtocolStreamGuard", _OldGuard))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        result = worker.run(
            "帮我看看工作目录里有什么。",
            [],
            PROFILE,
            {"stream": True, "max_steps": 8},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            [TOOL],
            events.append,
            None,
            max_steps=8,
            tool_registry=_ProbeTools(),
            run_context={},
        )
    return {"captured": captured, "events": events, "result": result}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _delta_text(run: dict) -> str:
    return "".join(
        str(item.get("content") or "") for item in run["events"] if item.get("type") == "delta"
    )


def main() -> None:
    print("== 原生工具调用修复：出站请求体字节探针（2026-10-05 乙篇 §5.2）==")
    print("场景：3 请求 / 2 次原生工具调用 + 1 次最终答复；出站入口 naiba.net.open 打桩捕获，全程不触网。")
    print()

    after = _run_conversation(old_semantics=False)
    before = _run_conversation(old_semantics=True)
    failures: list[str] = []

    # 前提①：两遍都完整跑完 3 个请求、拿到同一句最终答复
    for label, run in (("修后", after), ("修前", before)):
        if len(run["captured"]) != 3:
            failures.append(f"{label}：请求数 {len(run['captured'])} != 3")
        if str(run["result"][0]) != PROSE_R3_FINAL:
            failures.append(f"{label}：最终答复不符：{run['result'][0]!r}")

    # 前提②：修前模拟必须真的生效——tool_call 之后的正文在修前叫不出 delta
    after_delta, before_delta = _delta_text(after), _delta_text(before)
    missing = [prose for prose in TOOLCALL_PROSE if prose not in after_delta]
    if missing:
        failures.append(f"修后 delta 缺正文（修复没生效？）：{missing}")
    leaked = [prose for prose in POST_CALL_PROSE if prose in before_delta]
    if leaked:
        failures.append(f"「修前」未复现永久闭嘴（打桩没生效）：{leaked}")

    # 主断言：逐轮请求体 SHA-256
    rounds = [
        {"round": index, "before": _sha(old["data"]), "after": _sha(new["data"]),
         "equal": old["data"] == new["data"], "bytes": len(new["data"])}
        for index, (old, new) in enumerate(zip(before["captured"], after["captured"]), start=1)
    ]
    if len(rounds) == 3 and not all(item["equal"] for item in rounds):
        failures.append("请求体存在字节差异（前缀缓存契约被破坏）")

    # 直证：正文不进任何请求体；工具轮 assistant 消息（tool_calls 载荷）必须在请求体里
    for label, run in (("修后", after), ("修前", before)):
        for index, item in enumerate(run["captured"], start=1):
            text = item["data"].decode("utf-8", errors="replace")
            hit = [prose for prose in TOOLCALL_PROSE if prose in text]
            if hit:
                failures.append(f"{label} 请求 #{index} 混入流式正文：{hit}")
        if len(run["captured"]) >= 2:
            body2 = run["captured"][1]["data"].decode("utf-8", errors="replace")
            if '"tool_calls"' not in body2 or f'"{TOOL}"' not in body2:
                failures.append(f"{label} 请求 #2 缺工具轮载荷（tool_calls/{TOOL}）——对照可能双方都空")

    print(f"修后 delta 拼接：{after_delta!r}")
    print(f"修前 delta 拼接：{before_delta!r}")
    print("  （修前把 tool_call 之后到达的正文永久闭嘴吞掉 = 原始缺陷复现；两遍请求体仍须逐字节一致）")
    print()
    for item in rounds:
        verdict = "一致" if item["equal"] else "不一致"
        print(f"请求 #{item['round']}（{item['bytes']} 字节） 修后 {item['after']}")
        print(f"                       修前 {item['before']}  ⇒ {verdict}")
    print()

    if failures:
        print("探针失败：")
        for line in failures:
            print(f"  - {line}")
        raise SystemExit(1)
    print("全部通过：3 个请求体逐字节一致；正文只在界面外发、不进请求体；工具轮载荷在档。")


if __name__ == "__main__":
    main()
