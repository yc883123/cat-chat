# -*- coding: utf-8 -*-
"""守门：本地首字节超时 / 本地锁占用者可归因 / Agent 步数上限 / 探测窗口回写。

四件事共用同一条病历主线（客户机本地 unsloth 学 16 小时后界面永久停在
「等待本地模型资源」）。前一轮已经修掉了「请求体过大 / 停止无效 / 界面冻结 / 重启
清不掉僵尸」，这一轮补的是把「卡住」变成「可诊断、可自动收场」的四件事：

1. `naiba/llm/runtime.py::ModelRuntime._iter_stream_lines` —— 本地流式请求加
   **首字节超时**：prefill 做不完与正在 prefill 在界面上无法区分，只靠 1800 秒总超时
   等于让用户干等半小时（`LocalModelFirstByteTimeout`，可配、可关）。
2. `ModelRuntime` 的本地锁占用者标签（`options["lock_label"]` + `_busy_notice`）——
   视觉识别 / 子代理这类不传 status 的调用占住全进程唯一的本地锁时，等锁方必须能
   **指名道姓**，而不是只说「另一处调用仍在使用」。
3. `naiba/skills/agent.py` —— `while True` 加**步数上限**（`max_steps` 参数此前从未
   被读取过），并在预算将尽时先提醒模型收尾。
4. `naiba/config.py::remember_local_context_window` —— 本地探测到的窗口**回写**
   provider 配置（原先只挂在会话 profile 副本上，设置页与其它读取点仍拿 0 → 退回
   在线 256k 兜底）；同时新增两个运行设置与它们的默认值一致性约束。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba import config as config_module  # noqa: E402
from naiba.llm import local_probe  # noqa: E402
from naiba.llm import runtime as llm_runtime  # noqa: E402
from naiba.skills import agent as agent_module  # noqa: E402
from naiba.skills.agent import SkillAgent  # noqa: E402

MAINTENANCE_DOC = ROOT / "项目维护说明（修改代码前必读）.md"


def _read_source(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _assert_has(text: str, needle: str, label: str) -> None:
    """断言包含关系，但**不要**把整份源码 dump 进失败信息（那是几百 KB）。"""
    if needle not in text:
        raise AssertionError(f"{label} 缺少：{needle!r}")


class _BlockingStream:
    """永远不吐字节的流；``close()`` 之后迭代立刻抛错（模拟连接被看门狗关掉）。"""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self.closed_at: float | None = None

    def __iter__(self):
        return self

    def __next__(self):
        self._closed.wait(60)
        raise ValueError("I/O operation on closed file")

    def close(self) -> None:
        self.closed_at = time.perf_counter()
        self._closed.set()


class _KeepaliveOnlyStream:
    """只吐 SSE 保活注释的流（模拟网关/后端在等模型时的保活噪音）。"""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self.closed_at: float | None = None
        self._pending = True

    def __iter__(self):
        return self

    def __next__(self):
        if self._pending:
            self._pending = False
            return b": keepalive\n"
        if self._closed.wait(60):
            raise ValueError("I/O operation on closed file")
        return b"\n"

    def close(self) -> None:
        self.closed_at = time.perf_counter()
        self._closed.set()


class FirstByteTimeoutTests(unittest.TestCase):
    """① 本地首字节超时：卡住的读要被真的拽出来，而不是等满 1800 秒。"""

    def test_blocked_stream_is_aborted_within_the_timeout(self) -> None:
        stream = _BlockingStream()
        started = time.perf_counter()
        with self.assertRaises(llm_runtime.LocalModelFirstByteTimeout) as ctx:
            list(llm_runtime.ModelRuntime._iter_stream_lines(stream, None, 0.4))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 3.0, "首字节超时必须真的把阻塞读打断（而不是继续等总超时）")
        self.assertIsNotNone(stream.closed_at, "超时必须关闭连接，否则读取线程永远不放")
        message = str(ctx.exception)
        self.assertIn("本地模型", message)
        self.assertIn("上下文长度", message, "错误提示要给出可行动的原因，而不是只报超时")

    def test_normal_stream_is_untouched(self) -> None:
        lines = ["data: a\n", "data: b\n"]
        self.assertEqual(
            list(llm_runtime.ModelRuntime._iter_stream_lines(iter(lines), None, 5.0)),
            lines,
        )

    def test_keepalive_comments_do_not_disarm_the_timeout(self) -> None:
        """只发 SSE 保活注释（`: keepalive`）的链路必须仍被判为「没有输出」。

        实测踩坑（2026-09-14）：假端点/网关在等模型时周期性发注释行，若把注释行当成
        「已有输出」，首字节闸门会被静默解除，客户端一直等到对端自己断开（40 秒），
        真实场景就是「prefill 卡死永远不会被发现」。
        """
        stream = _KeepaliveOnlyStream()
        started = time.perf_counter()
        with self.assertRaises(llm_runtime.LocalModelFirstByteTimeout):
            list(llm_runtime.ModelRuntime._iter_stream_lines(stream, None, 0.4))
        self.assertLess(time.perf_counter() - started, 3.0)
        self.assertIsNotNone(stream.closed_at, "保活注释不算输出，超时必须照常断开")
        self.assertFalse(llm_runtime._has_stream_payload(b": keepalive\n"))
        self.assertFalse(llm_runtime._has_stream_payload(b"\n"))
        self.assertTrue(llm_runtime._has_stream_payload(b'data: {"x":1}\n'))

    def test_cancel_wins_over_timeout(self) -> None:
        stream = _BlockingStream()
        cancel_event = threading.Event()
        cancel_event.set()
        with self.assertRaises(RuntimeError) as ctx:
            list(llm_runtime.ModelRuntime._iter_stream_lines(stream, cancel_event, 30.0))
        self.assertIn("取消", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, llm_runtime.LocalModelFirstByteTimeout)

    def test_timeout_only_applies_to_local_profiles(self) -> None:
        source = _read_source("naiba", "llm", "runtime.py")
        block = source[source.index("first_byte_timeout = 0.0"):]
        block = block[: block.index("if diagnostics is not None")]
        self.assertIn("if is_local:", block)
        self.assertIn("LOCAL_FIRST_BYTE_TIMEOUT_SECONDS", block)
        # 在线请求不加这一层：云端排队/长思考合法，且已有 180 秒总超时兜底。
        self.assertIn(
            'override = options.get("first_byte_timeout_seconds")',
            block,
            "必须留一个可配置入口（运行设置会注入它）",
        )

    def test_every_local_stream_path_receives_the_timeout(self) -> None:
        source = _read_source("naiba", "llm", "runtime.py")
        self.assertEqual(
            source.count("_iter_stream_lines(response, cancel_event, first_byte_timeout)"), 3
        )
        self.assertEqual(
            source.count("_iter_stream_lines(retry_response, cancel_event, first_byte_timeout)"), 1
        )


class LockHolderAttributionTests(unittest.TestCase):
    """② 隐形占锁者必须可归因：等待状态里直接说出「谁在用」。"""

    def tearDown(self) -> None:
        llm_runtime.ModelRuntime._set_local_holder("")

    def test_busy_notice_names_the_holder(self) -> None:
        llm_runtime.ModelRuntime._set_local_holder("视觉识别")
        notice = llm_runtime.ModelRuntime._busy_notice(12)
        self.assertIn("视觉识别", notice)
        self.assertIn("12", notice)

    def test_busy_notice_falls_back_when_holder_unknown(self) -> None:
        llm_runtime.ModelRuntime._set_local_holder("")
        self.assertIn("另一处调用", llm_runtime.ModelRuntime._busy_notice(0))

    def test_holder_clearing_only_matches_the_same_label(self) -> None:
        llm_runtime.ModelRuntime._set_local_holder("子代理")
        llm_runtime.ModelRuntime._clear_local_holder("对话回复")
        self.assertEqual(llm_runtime.ModelRuntime._local_holder_name(), "子代理")
        llm_runtime.ModelRuntime._clear_local_holder("子代理")
        self.assertEqual(llm_runtime.ModelRuntime._local_holder_name(), "")

    def test_wait_notice_names_holder_without_waiting_for_the_first_interval(self) -> None:
        lock = threading.RLock()
        started = threading.Event()

        def holder() -> None:
            lock.acquire()
            llm_runtime.ModelRuntime._set_local_holder("视觉识别")
            started.set()
            # 持有时间必须跨过第一次 `acquire(timeout=0.5)`（否则它直接拿到锁、根本不排队）。
            time.sleep(1.0)
            llm_runtime.ModelRuntime._clear_local_holder("视觉识别")
            lock.release()

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(started.wait(2))
        messages: list[str] = []
        # 故意把周期调到 30 秒：第一条状态也必须是「谁在用」，不能先沉默一个周期。
        with mock.patch.object(llm_runtime, "LOCAL_LOCK_WAIT_NOTICE_SECONDS", 30.0):
            llm_runtime.ModelRuntime._acquire_local_lock(
                lock, {}, lambda payload: messages.append(str(payload.get("message") or "")), "对话回复"
            )
        lock.release()
        thread.join(3)
        self.assertTrue(messages, "排队必须立刻回报一次状态（而不是先沉默一个周期）")
        self.assertIn("视觉识别", messages[0])

    def test_invisible_callers_declare_their_label(self) -> None:
        vision = _read_source("naiba", "vision", "runtime.py")
        self.assertEqual(vision.count('"lock_label"'), 2, "视觉识别与视觉能力探测都要声明角色")
        for path, needle in (
            (("naiba", "subagent.py"), 'options["lock_label"] = "子代理"'),
            (("naiba", "plans.py"), 'options["lock_label"] = "计划执行"'),
            (("naiba", "run", "chat.py"), 'compile_options["lock_label"] = "计划整理"'),
            (("naiba", "app.py"), '"lock_label": "连接测试"'),
        ):
            _assert_has(_read_source(*path), needle, "隐形占锁者角色声明")

    def test_holder_is_registered_for_the_duration_of_a_local_call(self) -> None:
        """占用期间必须记名、结束后必须清掉（否则会一直误报上一个占用者）。"""
        seen: list[str] = []

        def complete_one() -> None:
            # 直接通过真实入口跑一次「本地」调用，但把网络层换成假响应。
            runtime = llm_runtime.ModelRuntime()
            profile = {
                "kind": "local",
                "model": "m",
                "request_format": "llama_cpp",
                "base_url": "http://127.0.0.1:9/v1",
                "context_window": 4096,
                "name": "本地",
            }
            with mock.patch.object(llm_runtime, "_NullLock", llm_runtime._NullLock), mock.patch.object(
                llm_runtime.ModelRuntime,
                "_complete_online",
                lambda self, *a, **k: (seen.append(llm_runtime.ModelRuntime._local_holder_name()) or "ok", "", "", {}),
            ):
                runtime.complete(profile, [{"role": "user", "content": "hi"}], {"lock_label": "子代理"})
            seen.append(llm_runtime.ModelRuntime._local_holder_name())

        complete_one()
        self.assertEqual(seen[0], "子代理", "占用期间必须记名")
        self.assertEqual(seen[-1], "", "结束后必须清掉")


class _LoopCatalog:
    def scan(self) -> list:
        return []

    def read_skill_content(self, path: str) -> str:  # pragma: no cover - 无活动技能时不会调用
        return ""


class _LoopRegistry:
    """最小工具注册表：唯一一个永远成功、且结果每次都不同的工具。

    结果必须每次不同，否则会先撞上「连续返回相同结果 = 无进展」那条熔断，
    就测不到步数上限了。
    """

    def __init__(self) -> None:
        self.calls = 0

    def schemas(self) -> list:
        return []

    def side_effect(self, name: str) -> bool:
        return False

    def media_declaration(self, name: str) -> dict:
        return {"extract": "none", "policy": "never"}

    def execute(self, tool: str, arguments: dict, active: list, run_context: object):
        self.calls += 1
        return True, f"结果 {self.calls}"


class StepLimitTests(unittest.TestCase):
    """③ max_steps 真的生效（此前参数存在但从未被读取）。"""

    def test_resolve_priority_and_dirty_values(self) -> None:
        resolve = agent_module._resolve_step_limit
        default = agent_module.DEFAULT_MAX_STEPS
        self.assertEqual(resolve(None, {}), default, "什么都没配时必须用默认上限兜底")
        self.assertEqual(resolve(7, {}), 7)
        self.assertEqual(resolve(None, {"max_steps": 9}), 9)
        self.assertEqual(resolve(7, {"max_steps": 9}), 7, "显式参数优先于 options")
        self.assertEqual(resolve(0, {"max_steps": 9}), 0, "0 = 不限制，且显式参数优先")
        self.assertEqual(resolve(None, {"max_steps": 0}), 0)
        self.assertEqual(resolve("12", {}), 12, "字符串数字要能解析")
        self.assertEqual(resolve("abc", {}), default, "脏值回落到默认上限，不能变成 0=不限制")
        self.assertEqual(resolve(None, {"max_steps": None}), default)

    def test_loop_stops_at_the_limit_and_explains_why(self) -> None:
        events: list[dict] = []
        registry = _LoopRegistry()
        worker = SkillAgent(
            _LoopCatalog(),
            None,
            lambda profile, messages, options, event: '{"type":"tool","tool":"echo","arguments":{"n":1}}',
            None,
        )
        response, runs, reasonings, usage = worker.run(
            "跑起来",
            [],
            {"kind": "online", "model": "m", "context_window": 128000},
            {"max_steps": 4, "stream": False},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["echo"],
            lambda payload: events.append(payload),
            None,
            tool_registry=registry,
        )
        self.assertEqual(registry.calls, 4, "到上限必须停下，不能多调一次模型循环")
        self.assertEqual(len(runs), 4)
        self.assertIn("步数上限", response)
        self.assertTrue(
            any(item.get("type") == "run_failed" for item in events),
            "必须让前端知道「为什么停」，不能静默返回",
        )

    def test_wrapup_hint_is_injected_before_the_hard_stop(self) -> None:
        events: list[dict] = []
        prompts: list[str] = []

        def complete(profile, messages, options, event):
            prompts.append("\n".join(str(item.get("content") or "") for item in messages))
            return '{"type":"tool","tool":"echo","arguments":{"n":1}}'

        worker = SkillAgent(_LoopCatalog(), None, complete, None)
        worker.run(
            "跑起来",
            [],
            {"kind": "online", "model": "m", "context_window": 128000},
            {"max_steps": 4, "stream": False},
            {"mode": "auto", "skill_ids": []},
            [],
            "",
            ["echo"],
            lambda payload: events.append(payload),
            None,
            tool_registry=_LoopRegistry(),
        )
        self.assertTrue(
            any("步数将尽" in item for item in prompts),
            "预算将尽要先提醒模型收尾，避免最后一步被硬切",
        )
        self.assertTrue(any(item.get("type") == "status" and "收尾" in str(item.get("message")) for item in events))

    def test_config_default_matches_the_implementation(self) -> None:
        self.assertEqual(config_module.AGENT_MAX_STEPS_DEFAULT, agent_module.DEFAULT_MAX_STEPS)
        self.assertEqual(
            config_module.LOCAL_FIRST_BYTE_TIMEOUT_DEFAULT,
            llm_runtime.LOCAL_FIRST_BYTE_TIMEOUT_SECONDS,
        )


class GuardSettingsTests(unittest.TestCase):
    """两个新运行设置：默认值、持久化、校验、注入到 options。"""

    def setUp(self) -> None:
        from server import ConfigStore

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.path.write_text(json.dumps({"providers": []}), encoding="utf-8")
        self.store = ConfigStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_are_visible_and_persisted(self) -> None:
        self.assertEqual(self.store.data["local_first_byte_timeout_seconds"], 120)
        self.assertEqual(self.store.data["agent_step_limit"], 200)
        public = self.store.public()
        self.assertEqual(public["local_first_byte_timeout_seconds"], 120)
        self.assertEqual(public["agent_step_limit"], 200)

    def test_zero_is_meaningful_and_kept(self) -> None:
        self.store.update_settings({"local_first_byte_timeout_seconds": 0, "agent_step_limit": 0})
        self.assertEqual(self.store.data["local_first_byte_timeout_seconds"], 0)
        self.assertEqual(self.store.data["agent_step_limit"], 0)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["agent_step_limit"], 0
        )

    def test_blank_falls_back_to_defaults(self) -> None:
        # 留空不能变成 0：那会让首字节闸门静默失效、步数上限静默变成「不限制」。
        self.store.update_settings({"local_first_byte_timeout_seconds": "", "agent_step_limit": ""})
        self.assertEqual(self.store.data["local_first_byte_timeout_seconds"], 120)
        self.assertEqual(self.store.data["agent_step_limit"], 200)

    def test_out_of_range_is_rejected(self) -> None:
        for bad in (-1, 4, 1801, "abc"):
            with self.assertRaises(ValueError):
                self.store.update_settings({"local_first_byte_timeout_seconds": bad})
        for bad in (-1, 1001, "abc"):
            with self.assertRaises(ValueError):
                self.store.update_settings({"agent_step_limit": bad})

    def test_generation_options_carry_both_guards(self) -> None:
        """四处调用方（对话/子代理/计划/计划整理）都从这里取 options，必须带齐。"""
        self.store.data["default_model_key"] = "online:o1"
        self.store.data["providers"] = [
            {"id": "o1", "kind": "online", "request_format": "openai_chat", "model": "m"},
        ]
        options = self.store.generation_options("online:o1")
        self.assertEqual(options["first_byte_timeout_seconds"], 120)
        self.assertEqual(options["max_steps"], 200)
        self.store.update_settings({"local_first_byte_timeout_seconds": 45, "agent_step_limit": 30})
        options = self.store.generation_options("online:o1")
        self.assertEqual(options["first_byte_timeout_seconds"], 45)
        self.assertEqual(options["max_steps"], 30)

    def test_legacy_max_agent_steps_remains_ignored(self) -> None:
        """老键名必须继续被丢弃：沿用同名会让老配置里的残值突然卡死用户的步数。"""
        source = _read_source("naiba", "config.py")
        self.assertIn('self.data.pop("max_agent_steps", None)', source)
        self.assertNotIn('"max_agent_steps":', source, "新设置必须换一个全新键名")


class ProbeWritebackTests(unittest.TestCase):
    """④ 探测到的本地窗口要进 provider 配置，而不是只活在会话 profile 副本里。"""

    def setUp(self) -> None:
        from server import ConfigStore

        local_probe._clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.path.write_text(
            json.dumps(
                {
                    "default_model_key": "local:l1",
                    "providers": [
                        {
                            "id": "l1",
                            "kind": "local",
                            "local_backend": "unsloth",
                            "request_format": "unsloth",
                            "base_url": "http://127.0.0.1:8080/v1",
                            "model": "qwen3-vl",
                            "api_key": "",
                        },
                        {
                            "id": "l2",
                            "kind": "local",
                            "local_backend": "unsloth",
                            "request_format": "unsloth",
                            "base_url": "http://127.0.0.1:8081/v1",
                            "model": "qwen3",
                            "context_window": 8192,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.store = ConfigStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _provider(self, provider_id: str) -> dict:
        return next(item for item in self.store.data["providers"] if item["id"] == provider_id)

    def test_writeback_is_visible_to_every_reader(self) -> None:
        self.assertTrue(self.store.remember_local_context_window("local:l1", 24576))
        self.assertEqual(self._provider("l1")["context_window_probed"], 24576)
        self.assertNotIn("context_window", self._provider("l1"), "回写不得伪装成用户显式配置")
        # 设置页显示 + 其它所有读 provider 的地方都靠这两个出口
        profile = self.store.profile("local:l1")
        self.assertEqual(profile["context_window"], 24576)
        self.assertEqual(profile["context_window_source"], "local_probe")
        public = next(item for item in self.store.public_providers() if item["id"] == "l1")
        self.assertEqual(public["context_window"], 24576)
        self.assertEqual(public["context_window_source"], "local_probe")

    def test_explicit_config_is_never_overwritten(self) -> None:
        self.assertFalse(self.store.remember_local_context_window("local:l2", 24576))
        self.assertNotIn("context_window_probed", self._provider("l2"))
        self.assertEqual(self.store.profile("local:l2")["context_window"], 8192)
        self.assertEqual(self.store.profile("local:l2")["context_window_source"], "local_config")

    def test_writeback_is_idempotent(self) -> None:
        self.assertTrue(self.store.remember_local_context_window("local:l1", 24576))
        self.assertFalse(
            self.store.remember_local_context_window("local:l1", 24576),
            "值没变就不该再写一次 config.json（每轮对话都会走到这里）",
        )
        self.assertTrue(
            self.store.remember_local_context_window("local:l1", 32768),
            "本地后端换了 n_ctx 要能更新",
        )
        self.assertEqual(self._provider("l1")["context_window_probed"], 32768)

    def test_unknown_provider_or_invalid_window_is_ignored(self) -> None:
        self.assertFalse(self.store.remember_local_context_window("local:nope", 4096))
        self.assertFalse(self.store.remember_local_context_window("", 4096))
        self.assertFalse(self.store.remember_local_context_window("local:l1", 0))
        self.assertFalse(self.store.remember_local_context_window("local:l1", "abc"))
        self.assertNotIn("context_window_probed", self._provider("l1"))

    def test_chat_path_actually_writes_back(self) -> None:
        """端到端接线：会话解析 profile 时探测成功 → provider 配置被回写。"""
        from naiba.run.chat import _profile_with_model_override

        with mock.patch.object(
            local_probe.net_io,
            "open",
            lambda request, timeout=None, **_kw: _StubProbeResponse(
                {"default_generation_settings": {"n_ctx": 16384}}
            ),
        ):
            profile = _profile_with_model_override(self.store, "local:l1", "qwen3-vl")
        self.assertEqual(profile["context_window"], 16384)
        self.assertEqual(self._provider("l1")["context_window_probed"], 16384)

    def test_chat_path_keeps_probing_when_the_source_is_only_probed(self) -> None:
        """来源是 local_probe 时必须继续探（本地后端可能这次才起来 / 换了 n_ctx）。"""
        from naiba.run.chat import _profile_with_model_override

        self.store.remember_local_context_window("local:l1", 8192)
        calls: list[int] = []

        def spy(request, timeout=None, **_kw):
            calls.append(1)
            return _StubProbeResponse({"default_generation_settings": {"n_ctx": 32768}})

        with mock.patch.object(local_probe.net_io, "open", spy):
            profile = _profile_with_model_override(self.store, "local:l1", "qwen3-vl")
        self.assertEqual(calls, [1], "local_probe 来源仍要重新探测")
        self.assertEqual(profile["context_window"], 32768)
        self.assertEqual(self._provider("l1")["context_window_probed"], 32768)

    def test_chat_path_never_probes_explicit_config(self) -> None:
        from naiba.run.chat import _profile_with_model_override

        calls: list[int] = []

        def spy(request, timeout=None, **_kw):
            calls.append(1)
            return _StubProbeResponse({"default_generation_settings": {"n_ctx": 4096}})

        with mock.patch.object(local_probe.net_io, "open", spy):
            profile = _profile_with_model_override(self.store, "local:l2", "qwen3")
        self.assertEqual(calls, [], "显式配置了窗口的本地 provider 不得触发探测")
        self.assertEqual(profile["context_window"], 8192)


class _StubProbeResponse:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False


class MaintenanceDocSyncTests(unittest.TestCase):
    """维护说明必须同步这些口径（快照式文档，改代码就改写对应章节）。"""

    def test_doc_mentions_the_new_guards(self) -> None:
        doc = MAINTENANCE_DOC.read_text(encoding="utf-8")
        for token in (
            "LocalModelFirstByteTimeout",
            "local_first_byte_timeout_seconds",
            "lock_label",
            "agent_step_limit",
            "remember_local_context_window",
            "context_window_probed",
            "DEFAULT_MAX_STEPS",
        ):
            _assert_has(doc, token, "维护说明")


if __name__ == "__main__":
    unittest.main()
