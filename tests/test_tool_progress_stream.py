"""pwsh / run_skill_script 的实时行级输出（tool_progress）与终态语义。

守门要点（三条都与"改造前后行为等价"有关，缺一即回归）：

1. 终态返回值必须保留 ``exit_code=<n>\\n`` 前缀——``_result_success`` 靠它判定成败，
   丢前缀会让失败命令静默变成成功（工具历史里就会把失败写成成功）；
2. 没有 ``event_sink`` 时行为与改造前一致：不报进度、不抛错、输出逐字节相同；
3. 取消与超时都要在秒级返回。``jobs.py:_run_shell`` 的 ``readline`` 直读写法在子进程
   长时间无输出时会永久阻塞，循环体里的取消/超时检查形同虚设——这两条用例就是钉死
   "不许再退回那种写法"。
4. ``run_skill_script`` 的 ``args.timeout`` 上界（900→7200）与"这个值确实传到墙钟"。
   上限只由 ``core.py`` 一处 clamp 决定，注册表里的 ``spec.timeout`` 是给 Web 看的元数据。
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.tools.providers import core  # noqa: E402

CHILD_PRINTS_THREE_LINES = (
    "import sys\n"
    "for i in range(3):\n"
    "    print('out', i)\n"
    "    sys.stderr.write('err %d\\n' % i)\n"
)


def _run(command, *, timeout=30, sink=None, tool="pwsh", cancel=None):
    run_context: dict[str, object] = {"event_sink": sink}
    if cancel is not None:
        run_context["cancel_event"] = cancel
    return core._run_streaming_command(
        command,
        cwd=Path(ROOT),
        timeout=timeout,
        max_output=50000,
        tool=tool,
        run_context=run_context,
    )


class StreamingCommandTests(unittest.TestCase):
    def test_lines_are_streamed_and_output_is_complete(self):
        events: list[dict] = []
        result = _run([sys.executable, "-u", "-c", CHILD_PRINTS_THREE_LINES], sink=events.append)
        self.assertTrue(result.startswith("exit_code=0\n"), result[:80])
        for index in range(3):
            self.assertIn(f"out {index}", result)
            self.assertIn(f"err {index}", result)
        progressed = [item for item in events if item.get("type") == "tool_progress"]
        self.assertTrue(progressed, "至少要推送一条实时行")
        self.assertTrue(all(item.get("tool") == "pwsh" for item in progressed))
        self.assertTrue(all(isinstance(item.get("line"), str) for item in progressed))

    def test_nonzero_exit_still_counts_as_failure(self):
        result = _run([sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertTrue(result.startswith("exit_code=3\n"), result[:80])
        self.assertFalse(core._result_success("pwsh", result))

    def test_without_sink_behaviour_is_unchanged(self):
        result = _run([sys.executable, "-c", "print('hello')"])
        self.assertEqual(result.strip(), "exit_code=0\nhello")

    def test_timeout_returns_promptly_with_minus_one(self):
        started = time.monotonic()
        result = _run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10, f"超时响应过慢：{elapsed:.1f}s")
        self.assertTrue(result.startswith("exit_code=-1"), result[:80])
        self.assertIn("上限", result)

    def test_cancel_returns_promptly(self):
        cancel = threading.Event()
        box: dict[str, str] = {}

        def worker() -> None:
            box["result"] = _run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=60,
                cancel=cancel,
            )

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        time.sleep(0.6)
        started = time.monotonic()
        cancel.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "取消之后仍在阻塞（说明退回了 readline 直读）")
        self.assertLess(time.monotonic() - started, 6, "取消响应过慢")
        self.assertTrue(box["result"].startswith("exit_code=-1"), box["result"][:80])
        self.assertIn("取消", box["result"])


class StreamBindingTests(unittest.TestCase):
    def test_streaming_is_limited_to_subprocess_tools(self):
        self.assertEqual(set(core._STREAM_TOOL_FNS), {"pwsh", "run_skill_script"})

    def test_stream_execute_forwards_run_context(self):
        captured: dict[str, object] = {}

        def impl(ctx, arguments, active_skills, run_context=None):
            captured["run_context"] = run_context
            return "exit_code=0\nok"

        class _Ctx:
            workspace = str(ROOT)

        def sink(payload):  # pragma: no cover - 仅用于同一性断言
            return None

        execute = core._make_stream_execute(_Ctx(), impl, "pwsh")
        success, result = execute({}, [], {"run_id": "r1", "event_sink": sink})
        self.assertTrue(success)
        self.assertEqual(result, "exit_code=0\nok")
        self.assertIs(captured["run_context"]["event_sink"], sink)


class PwshProviderIntegrationTests(unittest.TestCase):
    """走真实装配链（provider → registry → executor）：验证 run_context 确实一路透传到工具实现。

    单测 ``_run_streaming_command`` 只证明"函数本身会推送"；这一条证明"线上那条链把它接上了"
    （``_make_stream_execute`` 是唯一新增的接线点，接错就只有静态断言能发现）。
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tests"))

    def test_pwsh_progress_reaches_event_sink(self):
        import tempfile

        from tool_testkit import run_context_for, wired_executor

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            executor = wired_executor(root, mode="auto")
            events: list[dict] = []
            run_context = run_context_for(
                root, event_sink=events.append, cancel_event=threading.Event()
            )
            script = (
                "for ($i = 0; $i -lt 3; $i++) { "
                "Write-Output ('line ' + $i); Start-Sleep -Milliseconds 150 }"
            )
            success, result = executor.execute_unchecked(
                "pwsh", {"command": script, "timeout": 60}, [], run_context
            )
            self.assertTrue(result.startswith("exit_code=0"), result[:120])
            self.assertTrue(success, result[:120])
            self.assertIn("line 2", result)
            progressed = [item for item in events if item.get("type") == "tool_progress"]
            self.assertTrue(progressed, "真实装配链上没有任何 tool_progress 事件")
            self.assertTrue(all(item.get("tool") == "pwsh" for item in progressed))
            self.assertTrue(
                any("line" in str(item.get("line") or "") for item in progressed),
                f"进度行内容异常：{progressed[:3]}",
            )


class SkillScriptTimeoutCapTests(unittest.TestCase):
    """``run_skill_script`` 的超时上限与「这个值真的传到墙钟」。

    两条用例分工不同，缺一即漏：

    * 上限直钉：用 monkeypatch 截住 ``_run_streaming_command``，读它收到的 ``timeout``——
      真等 900/7200 秒不可行，而只看代码也无法证明 clamp 没被别处二次截断；
    * 真跑守卫：``sleep 3`` 的脚本在 ``timeout: 2`` 必须被判超时、在 ``timeout: 5``
      必须正常结束——证明新上限不是"只改了个没人读的数字"，取消/超时通道确实在
      ``args.timeout`` 这条路上。
    """

    SLEEP_THREE = (
        "import time\n"
        "print('start')\n"
        "time.sleep(3)\n"
        "print('end')\n"
    )

    def _fixture(self, root: Path) -> tuple[core.ToolContext, list[dict]]:
        script = root / "scripts" / "nap.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(self.SLEEP_THREE, encoding="utf-8")
        ctx = core.ToolContext(
            workspace=root,
            python_executable=sys.executable,
            command_timeout=120,
            mcp_registry=None,
        )
        return ctx, [{"id": "nap", "name": "nap", "root": str(root)}]

    def test_timeout_is_clamped_to_7200_and_defaults_to_context(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            ctx, skills = self._fixture(root)
            seen: list[object] = []
            original = core._run_streaming_command

            def spy(_command, **kwargs):
                seen.append(kwargs.get("timeout"))
                return "exit_code=0\n"

            core._run_streaming_command = spy
            try:
                core._tool_run_skill_script(
                    ctx, {"skill": "nap", "script": "scripts/nap.py", "timeout": 99999}, skills,
                )
                core._tool_run_skill_script(ctx, {"skill": "nap", "script": "scripts/nap.py"}, skills)
                core._tool_run_skill_script(
                    ctx, {"skill": "nap", "script": "scripts/nap.py", "timeout": 0}, skills,
                )
            finally:
                core._run_streaming_command = original
            self.assertEqual(seen[0], 7200, f"上界应为 7200：{seen}")
            self.assertEqual(seen[1], 120, "缺省仍取 ctx.command_timeout（120 不抬）")
            self.assertEqual(seen[2], 1, "下界仍是 1 秒")

    def test_real_run_honours_requested_timeout_below_and_above_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            ctx, skills = self._fixture(root)
            started = time.monotonic()
            short = core._tool_run_skill_script(
                ctx, {"skill": "nap", "script": "scripts/nap.py", "timeout": 2}, skills, {},
            )
            self.assertLess(time.monotonic() - started, 10, f"超时判定过慢：{short[:80]}")
            self.assertTrue(short.startswith("exit_code=-1"), short[:80])
            self.assertIn("上限", short)
            self.assertNotIn("end", short, "超时后仍拿到收尾输出 = 墙钟没真正生效")
            long_run = core._tool_run_skill_script(
                ctx, {"skill": "nap", "script": "scripts/nap.py", "timeout": 5}, skills, {},
            )
            self.assertTrue(long_run.startswith("exit_code=0"), long_run[:120])
            self.assertIn("end", long_run)

    def test_registry_metadata_is_not_an_execution_gate(self) -> None:
        """``spec.timeout=120`` 只是 Web 目录元数据，不得反过来夹住执行。"""
        from naiba.tools.registry import build_tool_registry

        registry = build_tool_registry()
        self.assertEqual(registry.timeout("run_skill_script"), 120)
        self.assertEqual(
            [row["timeout"] for row in registry.schemas() if row["name"] == "run_skill_script"],
            [120],
        )


if __name__ == "__main__":
    unittest.main()
