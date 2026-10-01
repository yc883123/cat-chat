# -*- coding: utf-8 -*-
"""护栏：围栏（```/~~~）动作识别、来源标记、Run 级一次授权与流式正文保全。

对应计划 ``docs/plans/2026-10-01-代码协议误判与围栏动作确认修复计划.md``：

- A–N 矩阵：整文围栏 = ``fenced``（强制确认）；尾部裸协议 = ``bare_protocol_tail``
  （保持现状直接执行）；围栏示例 / 未闭合围栏 / 动作后还有正文 = 正文（不执行、不报错）。
- 权限：``fenced`` 在 ``auto``/``full`` 下也先确认；批准后本 Run 复用授权；换工具集合
  即失效；无确认 UI（子 Agent、后台 Job）按原文收尾且不执行。
- 流式：围栏内不扫描协议标记（示例不被吞、前后正文都能继续产生 delta），裸协议仍被抑制。
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core.text_fences import (  # noqa: E402
    fence_mask,
    is_fence_noise,
    unwrap_whole_response_fence,
)
from naiba.llm.stream import StreamMixins, _FenceGuard  # noqa: E402
from naiba.skills.agent import SkillAgent  # noqa: E402
from naiba.tools.registry import ToolRegistry  # noqa: E402

from tool_testkit import wired_executor  # noqa: E402

F = "`" * 3
T = "~" * 3
TOOL = '{"type":"tool","tool":"shell_exec","arguments":{"command":"Get-Date"}}'
HARMONY = (
    '<|open|>tools<|sep|><|open|>call tool="shell_exec" index="1"<|sep|>'
    '<|open|>argument key="command" type="string"<|sep|>dir'
    '<|close|>argument<|sep|><|close|>call<|sep|><|close|>tools<|sep|>'
)


def sse(chunk):
    return ("data: " + json.dumps(chunk, ensure_ascii=False)).encode("utf-8")


# ---------------------------------------------------------------- 围栏工具


class TextFenceToolTests(unittest.TestCase):
    def test_unwrap_requires_whole_response_block(self):
        self.assertEqual(unwrap_whole_response_fence(f"{F}json\n{{}}\n{F}"), ("{}", "`"))
        self.assertEqual(unwrap_whole_response_fence(f"{T}json\n{{}}\n{T}"), ("{}", "~"))
        self.assertIsNone(unwrap_whole_response_fence(f"前言\n{F}\n{{}}\n{F}"))
        self.assertIsNone(unwrap_whole_response_fence(f"{F}\n{{}}\n{F}\n后文"))
        self.assertIsNone(unwrap_whole_response_fence(f"{F}\n未闭合"))

    def test_mask_is_length_preserving(self):
        raw = f"说明\n{F}json\n{TOOL}\n{F}\n后文"
        masked = fence_mask(raw)
        self.assertEqual(len(masked), len(raw))
        self.assertNotIn("type", masked)
        self.assertIn("后文", masked)

    def test_fence_noise(self):
        self.assertTrue(is_fence_noise("\n" + F + "\n"))
        self.assertTrue(is_fence_noise("   "))
        self.assertFalse(is_fence_noise("\n后文"))


# ---------------------------------------------------------------- A–N 矩阵


class ActionMatrixTests(unittest.TestCase):
    def _parse(self, raw):
        return SkillAgent._parse_action(raw)

    def test_a_to_f_whole_response_fence_is_fenced(self):
        cases = {
            "A json 围栏": f"{F}json\n{TOOL}\n{F}",
            "B ~~~ 围栏": f"{T}json\n{TOOL}\n{T}",
            "C xml invoke": (
                f'{F}xml\n<invoke name="shell_exec">'
                '<parameter name="command">dir</parameter></invoke>\n' + F
            ),
            "D xml named tool": (
                f'{F}xml\n<tool name="shell_exec">'
                '<parameter name="command">dir</parameter></tool>\n' + F
            ),
            "E harmony": f"{F}\n{HARMONY}\n{F}",
            "F 无 info string": f"{F}\n{TOOL}\n{F}",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                action = self._parse(raw)
                self.assertEqual(action.get("type"), "tool", action)
                self.assertEqual(action.get("source"), "fenced")
                self.assertEqual(action.get("tool"), "shell_exec")

    def test_fenced_final_is_plain_text(self):
        action = self._parse(f'{F}json\n{{"type":"final","content":"你好"}}\n{F}')
        self.assertEqual(action.get("type"), "final")
        self.assertEqual(action.get("source"), "final")
        self.assertEqual(action.get("content"), "你好")

    def test_g_to_j_bare_protocol_tail_still_executes(self):
        cases = {
            "G 顶到尾部": TOOL,
            "H 前言+尾部": "收到，开始排查：\n" + TOOL,
            "I 围栏示例+尾部裸协议": f"示例：\n{F}json\n{TOOL}\n{F}\n" + TOOL,
            "J 尾部孤立闭合围栏行": TOOL + f"\n{F}",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                action = self._parse(raw)
                self.assertEqual(action.get("type"), "tool", action)
                self.assertEqual(action.get("source"), "bare_protocol_tail")

    def test_k_to_n_stay_plain_text(self):
        cases = {
            "K 围栏示例+后接正文": f"{F}json\n{TOOL}\n{F}\n以上就是示例，请确认。",
            "L 未闭合围栏": f"{F}json\n{TOOL}",
            "M 动作后接正文": TOOL + "\n以上就是我准备执行的命令。",
            "N 围栏示例夹在正文中间": f"说明如下：\n{F}json\n{TOOL}\n{F}\n以上是示例。",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                action = self._parse(raw)
                self.assertEqual(action.get("type"), "final", action)
                self.assertEqual(action.get("source"), "final")

    def test_fenced_plain_data_is_not_a_tool_nor_parse_error(self):
        raw = f'{F}json\n{{"workflow": {{"type": "api", "nodes": []}}}}\n{F}'
        self.assertEqual(self._parse(raw).get("type"), "final")

    def test_native_function_calling_keeps_native_source(self):
        raw = json.dumps({
            "type": "tool", "tool": "shell_exec",
            "arguments": {"command": "dir"}, "source": "native",
        })
        self.assertEqual(self._parse(raw).get("source"), "native")

    def test_truncated_protocol_at_tail_is_parse_error(self):
        raw = (
            '<|open|>tools<|sep|><|open|>call tool="shell_exec" index="1"<|sep|>'
            '<|open|>argument key="command" type="string"<|sep|>Get-Process'
        )
        self.assertEqual(self._parse(raw), {"type": "parse_error"})

    def test_complete_protocol_followed_by_prose_is_text(self):
        """M 的加强版：完整协议后面还有正文 ⇒ 按正文显示（不论 JSON 还是 XML）。"""
        cases = {
            "json": TOOL + "\n以上是我准备执行的命令。",
            "xml": '<tool name="shell_exec"><parameter name="command">dir</parameter></tool>\n以上是示例。',
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self._parse(raw).get("type"), "final")

    def test_truncated_protocol_is_parse_error_regardless_of_tail(self):
        """不完整协议无法定位结尾：一律按「像协议但解析失败」处理（触发一次规范重发）。"""
        raw = (
            '<|open|>tools<|sep|><|open|>call tool="shell_exec" index="1"<|sep|>'
            '<|open|>argument key="command" type="string"<|sep|>Get-Process'
            "\n以上是示例，请勿执行。"
        )
        self.assertEqual(self._parse(raw), {"type": "parse_error"})


# ---------------------------------------------------------------- 流式护栏


class StreamingFenceGuardTests(unittest.TestCase):
    def _deltas(self, pieces, request_format="openai_chat"):
        events = []
        chunks = [sse({"choices": [{"delta": {"content": piece}}]}) for piece in pieces]
        StreamMixins._read_sse_response(chunks, request_format, events.append)
        return "".join(
            str(event.get("content") or "") for event in events if event.get("type") == "delta"
        )

    def test_fenced_block_is_visible_and_cross_chunk(self):
        pieces = ["说明\n", "```js", "on\n", TOOL, "\n``", "`\n后文"]
        visible = self._deltas(pieces)
        self.assertIn("说明", visible)
        self.assertIn("type", visible)
        self.assertIn("后文", visible)
        self.assertIn(F, visible)

    def test_bare_protocol_at_tail_is_still_suppressed(self):
        pieces = ["收到，开始排查：", TOOL[:20], TOOL[20:]]
        visible = self._deltas(pieces)
        self.assertIn("收到", visible)
        self.assertNotIn('"type"', visible)

    def test_fence_example_mid_prose_then_bare_protocol(self):
        pieces = ["示例：\n```json\n", TOOL, "\n```\n以上是示例。\n", TOOL]
        visible = self._deltas(pieces)
        self.assertIn("示例", visible)
        self.assertIn("以上是示例", visible)

    def test_unclosed_fence_is_forwarded_as_text(self):
        visible = self._deltas(["说明\n```json\n", TOOL])
        self.assertIn('"type"', visible)

    def test_guard_tracks_fence_across_calls(self):
        guard = _FenceGuard()
        StreamMixins._forward_guarded_text("说明\n```json", lambda _e: None, guard=guard)
        regions, _keep = guard.regions("```json\n" + TOOL, False)
        self.assertEqual(regions, [])
        self.assertTrue(guard.in_fence)
        tail = "```json\n{}\n```\n{}"
        regions, _keep = guard.regions(tail, False)
        self.assertTrue(any(end == len(tail) for _start, end in regions))


# ---------------------------------------------------------------- 权限与授权


class FencedPermissionTests(unittest.TestCase):
    """``fenced`` 来源的两段式授权（不改变既有权限策略，只加一层前置确认）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a.txt").write_text("hello", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run_context(self, executor, **extra):
        context = {
            "executor": executor,
            "workspace_dir": str(self.root),
            "confirmation_ui": True,
        }
        context.update(extra)
        return context

    def _call(self, agent, executor, tool, arguments, source, allowed=None, run_context=None):
        return agent._execute_with_retry(
            tool, arguments, [], set(allowed or {tool}), None, None,
            lambda _payload: None, run_context or self._run_context(executor), source,
        )

    def _call_bounded(self, agent, executor, tool, arguments, source, allowed=None,
                      run_context=None, timeout=20):
        """在线程里发起调用并**有界等待**。

        必须这样写的理由：一旦实现回归成「没有确认回路时仍然去等一个永远不会来的点击」，
        同步调用会让**整个测试进程挂满 1800 秒**（变异核对实测：看起来像"脚本坏了"，
        而且会连累同模块后续用例）。有界等待把它变成一条 20 秒内的明确断言。
        """
        outcome: dict = {}

        def caller():
            try:
                outcome["result"] = self._call(
                    agent, executor, tool, arguments, source,
                    allowed=allowed, run_context=run_context,
                )
            except Exception as exc:  # pragma: no cover - 失败要能看见原因
                outcome["error"] = exc

        thread = threading.Thread(target=caller, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        self.assertFalse(thread.is_alive(), "调用迟迟不返回：是不是又去等一个不存在的确认了？")
        self.assertNotIn("error", outcome, outcome.get("error"))
        return outcome.get("result")

    def _call_with_verdict(self, agent, executor, tool, arguments, source, approve, allow_run=True,
                           allowed=None, run_context=None):
        """在线程里发起调用，等确认卡出现即按 ``approve`` 处置。

        返回 ``(是否出现过确认卡, 调用结果)``——「是否出现确认卡」是围栏来源的核心断言。
        """
        outcome = {}
        run_context = run_context or self._run_context(executor)

        def caller():
            try:
                outcome["result"] = self._call(
                    agent, executor, tool, arguments, source,
                    allowed=allowed, run_context=run_context,
                )
            except Exception as exc:  # pragma: no cover - 失败要能看见原因
                outcome["error"] = exc

        thread = threading.Thread(target=caller, daemon=True)
        thread.start()
        saw_pending = False
        for _ in range(600):
            pending = list(executor.pending_confirmation.keys())
            if pending:
                saw_pending = True
                if approve:
                    executor.confirm_execute(pending[0], allow_run=allow_run)
                else:
                    executor.reject_execute(pending[0])
                break
            if not thread.is_alive():
                break
            time.sleep(0.01)
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive(), "确认已给出却仍不返回：收尾路径又有分支漏了")
        self.assertNotIn("error", outcome, outcome.get("error"))
        return saw_pending, outcome.get("result")

    def test_fenced_read_file_requires_confirm_even_in_auto_and_full(self):
        for mode in ("auto", "full"):
            with self.subTest(mode=mode):
                executor = wired_executor(self.root, mode=mode)
                agent = SkillAgent(None, executor, None, None)
                saw_pending, result = self._call_with_verdict(
                    agent, executor, "read_file", {"path": str(self.root / "a.txt")},
                    "fenced", approve=True,
                )
                self.assertTrue(saw_pending, "围栏来源动作任何档位都必须先确认")
                self.assertTrue(result[0], result)
                self.assertIn("hello", result[1])

    def test_bare_and_native_sources_keep_existing_permission(self):
        executor = wired_executor(self.root, mode="auto")
        agent = SkillAgent(None, executor, None, None)
        for source in ("bare_protocol_tail", "native"):
            with self.subTest(source=source):
                ok, out = self._call(
                    agent, executor, "read_file", {"path": str(self.root / "a.txt")}, source
                )
                self.assertTrue(ok, out)
                self.assertIn("hello", out)

    def test_run_level_approval_is_reused_within_run(self):
        executor = wired_executor(self.root, mode="auto")
        agent = SkillAgent(None, executor, None, None)
        run_context = self._run_context(executor)
        saw_pending, result = self._call_with_verdict(
            agent, executor, "read_file", {"path": str(self.root / "a.txt")},
            "fenced", approve=True, allow_run=True, run_context=run_context,
        )
        self.assertTrue(saw_pending)
        self.assertTrue(result[0], result)
        # 同一 Run 的后续围栏动作直接按既有策略执行（auto + 工作区内只读 = 免确认）
        ok, out = self._call(
            agent, executor, "read_file", {"path": str(self.root / "a.txt")},
            "fenced", run_context=run_context,
        )
        self.assertTrue(ok, out)
        self.assertIn("hello", out)

    def test_run_level_approval_expires_when_tool_set_changes(self):
        executor = wired_executor(self.root, mode="auto")
        agent = SkillAgent(None, executor, None, None)
        run_context = self._run_context(executor)
        self._call_with_verdict(
            agent, executor, "read_file", {"path": str(self.root / "a.txt")},
            "fenced", approve=True, allow_run=True, run_context=run_context,
        )
        saw_pending, result = self._call_with_verdict(
            agent, executor, "read_file", {"path": str(self.root / "a.txt")},
            "fenced", approve=True, allowed={"read_file", "list_directory"},
            run_context=run_context,
        )
        self.assertTrue(saw_pending, "工具集合变化后授权必须失效、重新确认")
        self.assertTrue(result[0], result)

    def test_approval_is_scoped_by_workspace(self):
        executor = wired_executor(self.root, mode="auto")
        agent = SkillAgent(None, executor, None, None)
        run_context = self._run_context(executor)
        self._call_with_verdict(
            agent, executor, "read_file", {"path": str(self.root / "a.txt")},
            "fenced", approve=True, allow_run=True, run_context=run_context,
        )
        self.assertTrue(agent._fenced_approval_active(run_context, {"read_file"}))
        other = self._run_context(executor, workspace_dir=str(self.root / "elsewhere"))
        self.assertFalse(agent._fenced_approval_active(other, {"read_file"}))

    def test_allow_once_does_not_grant_run_scope(self):
        executor = wired_executor(self.root, mode="auto")
        agent = SkillAgent(None, executor, None, None)
        run_context = self._run_context(executor)
        self._call_with_verdict(
            agent, executor, "read_file", {"path": str(self.root / "a.txt")},
            "fenced", approve=True, allow_run=False, run_context=run_context,
        )
        saw_pending, result = self._call_with_verdict(
            agent, executor, "read_file", {"path": str(self.root / "a.txt")},
            "fenced", approve=True, allow_run=False, run_context=run_context,
        )
        self.assertTrue(saw_pending, "「仅允许这一次」不应授予 Run 级授权")
        self.assertTrue(result[0], result)

    def test_reject_returns_unexecuted_marker_and_does_not_run(self):
        executor = wired_executor(self.root, mode="auto")
        agent = SkillAgent(None, executor, None, None)
        target = self.root / "never.txt"
        saw_pending, result = self._call_with_verdict(
            agent, executor, "write_file",
            {"path": str(target), "content": "x"}, "fenced", approve=False,
        )
        self.assertTrue(saw_pending)
        self.assertFalse(result[0])
        self.assertTrue(str(result[1]).startswith("FENCED_REJECTED:"), result)
        self.assertFalse(target.exists(), "拒绝后绝不能落盘")

    def test_no_confirmation_ui_skips_without_executing(self):
        executor = wired_executor(self.root, mode="auto")
        agent = SkillAgent(None, executor, None, None)
        run_context = self._run_context(executor, confirmation_ui=False)
        target = self.root / "never.txt"
        ok, out = self._call_bounded(
            agent, executor, "write_file",
            {"path": str(target), "content": "x"}, "fenced", run_context=run_context,
        )
        self.assertFalse(ok)
        self.assertTrue(str(out).startswith("FENCED_SKIPPED:"), out)
        self.assertEqual(executor.pending_confirmation, {})
        self.assertFalse(target.exists())


# ---------------------------------------------------------------- Agent 循环收尾


class _Catalog:
    def scan(self):
        return []

    def read_skill_content(self, path):  # pragma: no cover
        return ""


class _Registry(ToolRegistry):
    """最小注册表：只暴露一个工具，用来观察它究竟有没有被执行。"""

    def __init__(self, spec):
        super().__init__()
        self.calls = []
        self._spec = spec

    def get(self, name):
        return self._spec if name == self._spec.name else None

    def side_effect(self, name):
        return False

    def retryable(self, name):
        return False

    def media_declaration(self, name):
        return {"extract": "none", "policy": "never"}

    def execute(self, tool, arguments, active, run_context=None):
        self.calls.append(tool)
        return self._spec.execute(arguments, active, run_context)


class AgentFenceTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a.txt").write_text("hello", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _spec(self):
        from naiba.mcp import MCPRegistry
        from naiba.tools.providers.core import CoreToolProvider, ToolContext

        provider = CoreToolProvider(ToolContext(
            workspace=self.root,
            python_executable=sys.executable,
            command_timeout=30,
            mcp_registry=MCPRegistry([]),
            mcp_register=None,
        ))
        return next(item for item in provider.tools() if item.name == "read_file")

    def _run(self, replies, run_context_extra=None, registry=None):
        executor = wired_executor(self.root, mode="auto")
        registry = registry or _Registry(self._spec())
        executor.set_def_resolver(registry.get)
        state = {"index": 0}

        def complete(_profile, _messages, _options, _event):
            index = min(state["index"], len(replies) - 1)
            state["index"] += 1
            return replies[index]

        run_context = {
            "run_id": "run-fence",
            "conversation_id": "conv-fence",
            "executor": executor,
            "workspace_dir": str(self.root),
            "confirmation_ui": True,
        }
        run_context.update(run_context_extra or {})
        worker = SkillAgent(_Catalog(), executor, complete, None)
        response, runs, _reasonings, _usage = worker.run(
            "读一下 a.txt",
            [],
            {"kind": "local", "model": "m", "context_window": 32768},
            {"stream": False, "max_tokens": 512, "max_steps": 4},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["read_file"],
            lambda _payload: None,
            None,
            tool_registry=registry,
            run_context=run_context,
        )
        return response, runs, registry, executor

    def _fenced_read(self):
        payload = json.dumps(
            {
                "type": "tool",
                "tool": "read_file",
                "arguments": {"path": str(self.root / "a.txt")},
            },
            ensure_ascii=False,
        )
        return f"{F}json\n{payload}\n{F}"

    def test_fenced_reject_ends_turn_with_original_text(self):
        executor = wired_executor(self.root, mode="auto")
        registry = _Registry(self._spec())
        executor.set_def_resolver(registry.get)
        raw = self._fenced_read()

        def complete(_profile, _messages, _options, _event):
            return raw

        def watcher():
            for _ in range(500):
                pending = list(executor.pending_confirmation.keys())
                if pending:
                    executor.reject_execute(pending[0])
                    return
                time.sleep(0.01)

        threading.Thread(target=watcher, daemon=True).start()
        worker = SkillAgent(_Catalog(), executor, complete, None)
        response, _runs, _reasonings, _usage = worker.run(
            "读一下 a.txt",
            [],
            {"kind": "local", "model": "m", "context_window": 32768},
            {"stream": False, "max_tokens": 512, "max_steps": 4},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["read_file"],
            lambda _payload: None,
            None,
            tool_registry=registry,
            run_context={
                "run_id": "run-reject", "conversation_id": "c", "executor": executor,
                "workspace_dir": str(self.root), "confirmation_ui": True,
            },
        )
        self.assertEqual(registry.calls, [], "围栏动作被拒绝后绝对不能执行")
        self.assertIn("未执行", response)
        self.assertIn(F, response, "拒绝后要保留原始围栏内容供用户判断")

    def test_fenced_approval_runs_once_and_turn_completes(self):
        executor = wired_executor(self.root, mode="auto")
        registry = _Registry(self._spec())
        executor.set_def_resolver(registry.get)
        raw = self._fenced_read()
        answers = iter([raw, "读完了，内容是 hello。"])

        def complete(_profile, _messages, _options, _event):
            return next(answers, "读完了，内容是 hello。")

        def watcher():
            for _ in range(500):
                pending = list(executor.pending_confirmation.keys())
                if pending:
                    executor.confirm_execute(pending[0], allow_run=True)
                    return
                time.sleep(0.01)

        threading.Thread(target=watcher, daemon=True).start()
        worker = SkillAgent(_Catalog(), executor, complete, None)
        response, runs, _reasonings, _usage = worker.run(
            "读一下 a.txt",
            [],
            {"kind": "local", "model": "m", "context_window": 32768},
            {"stream": False, "max_tokens": 512, "max_steps": 4},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["read_file"],
            lambda _payload: None,
            None,
            tool_registry=registry,
            run_context={
                "run_id": "run-approve", "conversation_id": "c", "executor": executor,
                "workspace_dir": str(self.root), "confirmation_ui": True,
            },
        )
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].get("source"), "fenced")
        self.assertTrue(runs[0].get("success"), runs)
        self.assertIn("hello", str(runs[0].get("result") or ""))
        self.assertIn("读完了", response)

    def test_no_ui_run_turns_fenced_action_into_text(self):
        """没有确认回路时必须**不挂起**地收尾——这条用有界等待钉死。

        写成同步调用的话，「仍然去等一个永远不会来的点击」这个缺陷在测试里表现为
        **挂起 1800 秒**（变异核对实测踩过：进程被外部杀掉、看起来像脚本坏了）。
        有界等待把它变成一个 20 秒内的明确断言。
        """
        executor = wired_executor(self.root, mode="auto")
        registry = _Registry(self._spec())
        executor.set_def_resolver(registry.get)
        result: dict = {}

        def worker():
            result["value"] = self._run(
                [self._fenced_read()], run_context_extra={"confirmation_ui": False}
            )

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=20)
        self.assertFalse(
            thread.is_alive(),
            "confirmation_ui=False 的 Run 不得挂起等确认（后台 Job / 子 Agent 没有人能点卡）",
        )
        self.assertEqual({}, executor.pending_confirmation, "没有确认回路时不应留下待确认请求")
        response, _runs, _registry, run_executor = result["value"]
        self.assertIn("未执行", response)
        self.assertEqual(run_executor.pending_confirmation, {})

    def test_parse_error_keeps_last_raw_text(self):
        raw = (
            '<|open|>tools<|sep|><|open|>call tool="read_file" index="1"<|sep|>'
            '<|open|>argument key="path" type="string"<|sep|>D:\\x'
        )
        response, _runs, _registry, _executor = self._run([raw])
        self.assertIn("无法自动纠正", response)
        self.assertIn("read_file", response, "末次解析失败必须保留模型最后一轮原文")


if __name__ == "__main__":
    unittest.main()
