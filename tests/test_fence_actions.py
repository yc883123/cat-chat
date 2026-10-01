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

from naiba.app import NaibaChatApp  # noqa: E402
from naiba.core.text_fences import (  # noqa: E402
    fence_mask,
    is_fence_noise,
    unwrap_whole_response_fence,
)
from naiba.llm.stream import StreamMixins, _FenceGuard  # noqa: E402
from naiba.plans import ReadOnlyToolExecutor, plan_detail_for_event  # noqa: E402
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
    """最小注册表：只暴露给定工具，用来观察它们究竟有没有被执行。"""

    def __init__(self, *specs):
        super().__init__()
        self.calls = []
        self._specs = {spec.name: spec for spec in specs}

    def get(self, name):
        return self._specs.get(name)

    def side_effect(self, name):
        return False

    def retryable(self, name):
        return False

    def media_declaration(self, name):
        return {"extract": "none", "policy": "never"}

    def execute(self, tool, arguments, active, run_context=None):
        self.calls.append(tool)
        return self._specs[tool].execute(arguments, active, run_context)


class AgentFenceTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a.txt").write_text("hello", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _spec(self, name="read_file"):
        from naiba.mcp import MCPRegistry
        from naiba.tools.providers.core import CoreToolProvider, ToolContext

        provider = CoreToolProvider(ToolContext(
            workspace=self.root,
            python_executable=sys.executable,
            command_timeout=30,
            mcp_registry=MCPRegistry([]),
            mcp_register=None,
        ))
        return next(item for item in provider.tools() if item.name == name)

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


class NestedSourceOverrideTests(AgentFenceTurnTests):
    """计划 2026-10-01 §2.1：`tools.calls` 里的嵌套 `source` 不得降级确认要求。

    整条响应是一段围栏时，围栏里的多工具 JSON 只要有一个 call 标了 `source=final`
    （或 `native`），旧实现就会把这个 call 当成「免确认来源」——于是 `pwsh` 这类
    危险工具能借着围栏整体合法而绕过强制确认。现在每个 call 一律继承顶层来源。
    """

    def _fenced_multi(self, calls):
        payload = json.dumps({"type": "tools", "calls": calls}, ensure_ascii=False)
        return f"{F}json\n{payload}\n{F}"

    def _run_multi(self, raw, verdict, allow_run=True):
        """跑一轮多工具围栏动作，返回 ``(runs, registry, events, completions, 批准前已执行的工具)``。"""
        executor = wired_executor(self.root, mode="auto")
        registry = _Registry(self._spec("read_file"), self._spec("list_directory"))
        executor.set_def_resolver(registry.get)
        events: list[dict] = []
        completions = {"count": 0}
        executed_before_approval: list[str] = []
        answers = iter([raw, "两个都做完了。"])

        def complete(_profile, _messages, _options, _event):
            completions["count"] += 1
            return next(answers, "两个都做完了。")

        def watcher():
            for _ in range(800):
                pending = list(executor.pending_confirmation.keys())
                if pending:
                    executed_before_approval.extend(registry.calls)
                    if verdict == "approve":
                        executor.confirm_execute(pending[0], allow_run=allow_run)
                    elif verdict == "reject":
                        executor.reject_execute(pending[0])
                    elif verdict == "expire":
                        # 模拟确认失效（实际链路里是超时/已被处理）：直接摘掉待确认项，
                        # wait_for_confirmation 会返回「确认请求已失效」。
                        executor.pending_confirmation.pop(pending[0], None)
                    return
                time.sleep(0.01)

        threading.Thread(target=watcher, daemon=True).start()
        worker = SkillAgent(_Catalog(), executor, complete, None)
        response, runs, _reasonings, _usage = worker.run(
            "两步都做一下",
            [],
            {"kind": "local", "model": "m", "context_window": 32768},
            {"stream": False, "max_tokens": 512, "max_steps": 4},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["read_file", "list_directory"],
            events.append,
            None,
            tool_registry=registry,
            run_context={
                "run_id": "run-nested", "conversation_id": "c", "executor": executor,
                "workspace_dir": str(self.root), "confirmation_ui": True,
            },
        )
        return {
            "response": response, "runs": runs, "registry": registry, "events": events,
            "completions": completions["count"], "before": executed_before_approval,
        }

    def test_nested_source_cannot_downgrade_fenced_confirmation(self):
        raw = self._fenced_multi([
            {"tool": "read_file", "arguments": {"path": str(self.root / "a.txt")},
             "source": "final"},
            {"tool": "list_directory", "arguments": {"path": "."}, "source": "native"},
        ])
        outcome = self._run_multi(raw, "approve", allow_run=True)
        confirms = [e for e in outcome["events"] if e.get("type") == "tool_confirm"]
        self.assertTrue(confirms, "围栏里的多工具动作必须产生确认卡")
        self.assertEqual(
            [e.get("action_source") for e in confirms], ["fenced"],
            "嵌套的 source=final/native 不得把顶层 fenced 降级",
        )
        # 批准前不得有任何执行：确认事件之前不许出现任何工具结果事件
        # （确认的那一次是经 `execute_unchecked` 直接落 def 执行的，不走注册表，
        #  所以只盯 registry.calls 会漏，必须按事件顺序判）。
        kinds = [e.get("type") for e in outcome["events"]]
        first_confirm = kinds.index("tool_confirm")
        self.assertNotIn("tool_result", kinds[:first_confirm], "批准之前任何调用都不许执行")
        self.assertEqual(outcome["before"], [])
        self.assertEqual(
            [(r.get("tool"), r.get("success")) for r in outcome["runs"]],
            [("read_file", True), ("list_directory", True)],
            "批准后两个调用都应执行（第二个复用 Run 级授权）",
        )
        self.assertEqual(
            [r.get("source") for r in outcome["runs"]], ["fenced", "fenced"],
            "记账里的来源也必须全部是 fenced",
        )

    def test_fenced_multi_tool_asks_exactly_once(self):
        raw = self._fenced_multi([
            {"tool": "read_file", "arguments": {"path": str(self.root / "a.txt")}},
            {"tool": "list_directory", "arguments": {"path": "."}},
        ])
        outcome = self._run_multi(raw, "approve", allow_run=True)
        confirms = [e for e in outcome["events"] if e.get("type") == "tool_confirm"]
        self.assertEqual(
            len(confirms), 1,
            "同一批围栏动作只应有一个确认回路（并行会同时挂出多张卡）",
        )

    def test_fenced_multi_tool_reject_stops_the_whole_batch(self):
        raw = self._fenced_multi([
            {"tool": "read_file", "arguments": {"path": str(self.root / "a.txt")}},
            {"tool": "list_directory", "arguments": {"path": "."}},
        ])
        outcome = self._run_multi(raw, "reject")
        self.assertEqual(outcome["registry"].calls, [], "拒绝后整条动作都不许执行")
        self.assertIn("未执行", outcome["response"])
        self.assertIn(F, outcome["response"], "收尾必须保留原始围栏内容")
        self.assertEqual(
            outcome["completions"], 1,
            "拒绝后不得再发起模型请求（否则模型会把同一条命令再发一遍）",
        )

    def test_expired_confirmation_ends_turn_without_refeedback(self):
        raw = self._fenced_multi([
            {"tool": "read_file", "arguments": {"path": str(self.root / "a.txt")}},
        ])
        outcome = self._run_multi(raw, "expire")
        self.assertEqual(outcome["registry"].calls, [], "确认失效后不得执行")
        self.assertIn("未执行", outcome["response"])
        self.assertIn("失效", outcome["response"])
        self.assertEqual(outcome["completions"], 1, "确认失效须与拒绝走同一条收尾")
        self.assertNotIn("FENCED_", outcome["response"], "内部标记不得进入用户可见文案")

    def test_timeout_message_maps_to_fenced_expired(self):
        from naiba.skills.agent import _fenced_wait_failure

        for text in (
            "用户拒绝执行：pwsh",
            "用户未在30分钟内确认，已自动拒绝",
            "确认请求已失效",
            "确认ID无效或已过期",
        ):
            with self.subTest(text=text):
                self.assertTrue(_fenced_wait_failure(text), text)
        self.assertEqual(_fenced_wait_failure("命令返回非零退出码"), "")


class PlanReadOnlyConfirmTests(unittest.TestCase):
    """计划 2026-10-01 §2.2：只读代理的判定必须在**确认之后**依然生效。

    围栏来源动作走 ``request_confirmation``，批准后直接落 ``execute_unchecked``——
    若只读判定只挂在 ``execute`` 入口，Plan 模式下一条围栏 ``http_request POST``
    批准后就会真的发出去（只读约束被确认链路整个绕开）。
    """

    class _Spec:
        def __init__(self, name, log):
            self.name = name
            self.log = log
            self.side_effect = False
            self.policy = None

        def execute(self, arguments, active_skills, run_context=None):
            self.log.append(self.name)
            return True, f"{self.name} done"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log: list[str] = []
        self.executor = wired_executor(self.root, mode="auto")
        self.specs = {
            name: self._Spec(name, self.log)
            for name in ("http_request", "read_file", "pwsh", "write_file")
        }
        self.executor.set_def_resolver(self.specs.get)
        self.wrapper = ReadOnlyToolExecutor(self.executor)
        self.agent = SkillAgent(None, self.wrapper, None, None)
        self.run_context = {
            "executor": self.wrapper,
            "workspace_dir": str(self.root),
            "confirmation_ui": True,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, tool, arguments, source="fenced", allowed=None):
        return self.agent._execute_with_retry(
            tool, arguments, [], set(allowed or {tool}), None, None,
            lambda _payload: None, self.run_context, source,
        )

    def _call_bounded(self, tool, arguments, source="fenced", allowed=None, timeout=20):
        outcome: dict = {}

        def caller():
            try:
                outcome["result"] = self._call(tool, arguments, source, allowed)
            except Exception as exc:  # pragma: no cover - 失败要看见原因
                outcome["error"] = exc

        thread = threading.Thread(target=caller, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        self.assertFalse(thread.is_alive(), "调用迟迟不返回：是不是又在等一个不存在的确认？")
        self.assertNotIn("error", outcome, outcome.get("error"))
        return outcome["result"]

    def test_fenced_post_is_refused_before_any_confirmation(self):
        ok, out = self._call_bounded(
            "http_request", {"url": "https://example.invalid", "method": "POST"}
        )
        self.assertFalse(ok, out)
        self.assertIn("只读模式仅允许 GET/HEAD", out)
        self.assertEqual(self.executor.pending_confirmation, {}, "注定失败的动作不该弹卡")
        self.assertEqual(self.log, [], "底层实现绝不能被调用")

    def test_fenced_run_workflow_is_refused(self):
        ok, out = self._call_bounded("comfyui__run_workflow", {}, allowed={"comfyui__run_workflow"})
        self.assertFalse(ok, out)
        self.assertIn("只读模式", out)
        self.assertEqual(self.executor.pending_confirmation, {})

    def test_fenced_get_is_confirmed_then_executed(self):
        """只读能力不能被整体误伤：GET 仍应走「确认 → 执行」。"""
        outcome: dict = {}

        def caller():
            outcome["result"] = self._call("http_request", {"url": "https://example.invalid"})

        thread = threading.Thread(target=caller, daemon=True)
        thread.start()
        pending = []
        for _ in range(600):
            pending = list(self.executor.pending_confirmation.keys())
            if pending:
                break
            if not thread.is_alive():
                break
            time.sleep(0.01)
        self.assertTrue(pending, "只读来源的 GET 也必须先确认")
        self.assertEqual(self.log, [], "批准前不得执行")
        self.executor.confirm_execute(pending[0], allow_run=True)
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertTrue(outcome["result"][0], outcome["result"])
        self.assertEqual(self.log, ["http_request"], "批准后只读的 GET 必须真的执行")

    def test_recheck_blocks_execution_when_policy_breaks_after_card(self):
        """确认后的复核入口确实挂在批准路径上，而不是只在入口挡一次。

        构造方式：先让一个**通过**只读判定的调用正常弹卡，然后在批准前把待确认项里的
        参数改成越权形态（``GET`` → ``POST``）。批准时复核必须以**确认时的参数**重新判定。
        """
        outcome: dict = {}

        def caller():
            outcome["result"] = self._call("http_request", {"url": "https://example.invalid"})

        thread = threading.Thread(target=caller, daemon=True)
        thread.start()
        pending = []
        for _ in range(600):
            pending = list(self.executor.pending_confirmation.keys())
            if pending:
                break
            if not thread.is_alive():
                break
            time.sleep(0.01)
        self.assertTrue(pending)
        self.executor.pending_confirmation[pending[0]]["arguments"]["method"] = "POST"
        ok, out = self.executor.confirm_execute(pending[0], allow_run=True)
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertFalse(ok, out)
        self.assertIn("只读模式仅允许 GET/HEAD", out)
        self.assertEqual(self.log, [], "复核不通过时底层实现不能被调用")

    def test_readonly_execute_path_also_carries_recheck(self):
        """普通（非围栏）路径：确认卡也是带复核入口的。"""
        ok, out = self.wrapper.execute("pwsh", {"command": "dir"}, [], self.run_context)
        self.assertFalse(ok, out)
        self.assertIn("已禁止工具", out)
        ok, out = self.wrapper.execute("http_request", {"method": "GET"}, [], self.run_context)
        self.assertTrue(ok, out)
        self.executor.pending_confirmation.clear()
        self.assertEqual(self.log, ["http_request"])


class PlanConfirmContractTests(unittest.TestCase):
    """计划 2026-10-01 §2.6：Plan 的确认详情与普通对话共用同一来源字段与终态语义。"""

    def test_tool_confirm_detail_carries_action_source(self):
        detail = plan_detail_for_event({
            "type": "tool_confirm",
            "confirm_id": "c-1",
            "tool_name": "read_file",
            "tool_desc": "读取文件",
            "arguments": {"path": "a.txt"},
            "action_source": "fenced",
        })
        self.assertEqual(detail["confirm_id"], "c-1")
        self.assertEqual(detail["tool"], "read_file")
        self.assertEqual(detail["tool_desc"], "读取文件")
        self.assertEqual(detail["arguments"], {"path": "a.txt"})
        self.assertEqual(detail["action_source"], "fenced")

    def test_missing_source_degrades_to_plain_confirmation(self):
        detail = plan_detail_for_event({"type": "tool_confirm", "confirm_id": "c-2"})
        self.assertEqual(detail["action_source"], "", "缺来源时按普通确认处理，不得默认 fenced")

    def test_other_events_keep_their_shape(self):
        self.assertEqual(
            plan_detail_for_event({"type": "status", "message": "跑起来了"}),
            {"message": "跑起来了"},
        )
        self.assertEqual(
            plan_detail_for_event({"type": "tool_start", "tool": "read_file"}),
            {"message": "正在执行 read_file", "tool": "read_file"},
        )
        self.assertEqual(
            plan_detail_for_event({"type": "tool_result", "tool": "read_file"}),
            {"message": "工具 read_file 执行完毕"},
        )
        self.assertIsNone(plan_detail_for_event({"type": "delta", "content": "…"}))


class AllowRunProtocolTests(unittest.TestCase):
    """计划 2026-10-01 §2.3：``allow_run`` 只接受真正的布尔值。

    ``bool("false")`` 是 ``True``：旧实现会把字符串 ``"false"`` 当成「允许本轮继续」，
    静默把一次授权放大成整轮授权。类型不对必须 400，不能悄悄转换。
    """

    class _Runs:
        def __init__(self):
            self.seen: list[Any] = []

        def confirm_tool_async(self, run_id, confirm_id, allow_run=None):
            self.seen.append(allow_run)
            return True, "ok"

    class _Stub:
        def __init__(self):
            self.runs = AllowRunProtocolTests._Runs()
            self.replies: list[tuple[Any, int]] = []

        def _reply(self, payload, status=200):
            self.replies.append((payload, status))
            return payload, status

    def _call(self, body):
        stub = self._Stub()
        NaibaChatApp._confirm_tool(stub, body)
        return stub

    def test_non_boolean_values_are_rejected(self):
        for value in ("false", "true", "", 0, 1, [], {}, ["true"]):
            with self.subTest(value=value):
                stub = self._call({"run_id": "r", "confirm_id": "c", "allow_run": value})
                self.assertEqual(stub.runs.seen, [], f"{value!r} 不该被当成授权")
                self.assertEqual(stub.replies[-1][1], 400, stub.replies)
                self.assertIn("布尔值", str(stub.replies[-1][0].get("error") or ""))

    def test_boolean_values_are_forwarded_verbatim(self):
        for value in (True, False):
            with self.subTest(value=value):
                stub = self._call({"run_id": "r", "confirm_id": "c", "allow_run": value})
                self.assertEqual(stub.runs.seen, [value])
                self.assertEqual(stub.replies[-1][1], 200)

    def test_missing_field_keeps_documented_legacy_default(self):
        """旧前端不带该字段：按「围栏来源 ⇒ 允许本轮继续」的既定兼容策略处理（None）。"""
        stub = self._call({"run_id": "r", "confirm_id": "c"})
        self.assertEqual(stub.runs.seen, [None])
        self.assertEqual(stub.replies[-1][1], 200)

    def test_missing_identifiers_are_rejected(self):
        stub = self._call({"run_id": "", "confirm_id": ""})
        self.assertEqual(stub.runs.seen, [])
        self.assertEqual(stub.replies[-1][1], 400)


if __name__ == "__main__":
    unittest.main()
