# -*- coding: utf-8 -*-
"""守门：三条 2026-09-16 用户实测缺陷的回归。

起因（用户三条反馈，前两条是 bug、第三条是设计确认）：

1. **紧急**：后台任务跑着的时候点任务面板里的某一行，界面会跳到会话并把消息区整体重渲染，
   而那一刻正在流式输出的助手气泡（正文还只在 DOM 里，run 结束才落库）被
   ``container.replaceChildren()`` 连根拔掉、重渲染后也没人把它挂回来 —— 表现为
   「AI 回复消失，只剩下前一条用户侧消息」，并且此后一直不回来。用户等到重启，
   重启又把这条 in-flight 的 Run 判成 interrupted，事件流里的正文也没人重建落库，
   于是「重启、刷新都不管用」（实测库里就真的只剩一条用户消息，助手消息永远缺位）。
   两处各自修：前端保住活动流的气泡；启动时把中断轮次已产出的内容重建为 partial 消息。

2. 检查更新后程序自动重启，新进程活着、托盘图标在，但主窗口不出来，必须到托盘双击
   「打开窗口」才唤回界面 —— 更新脚本用 ``-WindowStyle Hidden`` 启动新进程，
   而这个程序本来就是窗口化打包（``console=False``，没有控制台要藏）。

3. 「还没点检查更新就查到新版本」——启动 4 秒的后台元数据检查是当时的设计（只查不装），
   但用户视为问题；按用户决定改为**只在点「检查更新」时才发起**（见
   ``UpdateCheckIsManualTests``）。

全量验证口径见 `项目维护说明（修改代码前必读）.md` §六。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.run.manager import ConversationRunManager  # noqa: E402
from naiba.storage.store import (  # noqa: E402
    INTERRUPTED_TASK_REASON,
    ChatStorage,
)


def _read_js(name: str) -> str:
    return (ROOT / "public" / "js" / name).read_text(encoding="utf-8")


def _function_body(source: str, signature: str) -> str:
    """取函数体（到下一个顶层 function/export function 之前）。"""
    import re

    start = source.index(signature)
    rest = source[start:]
    match = re.search(r"\n(?:export )?function ", rest[1:])
    return rest if match is None else rest[: match.start() + 1]


class _AppStub:
    def __init__(self, storage: ChatStorage) -> None:
        self.storage = storage


class _LightRunManager(ConversationRunManager):
    """只装「中断恢复」需要的依赖，避开需要完整 app 的 __init__。"""

    def __init__(self, app: _AppStub) -> None:
        self.app = app
        self._sinks: dict[str, object] = {}
        self._sinks_lock = threading.Lock()


class InterruptedRunRecoveryTests(unittest.TestCase):
    """重启中断的对话 Run：已产出的正文必须被重建落库，而不是永久消失。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "chat.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_run(
        self,
        *,
        kind_events: list[dict] | None = None,
        parent_job_id: str = "",
    ) -> tuple[ChatStorage, str, str]:
        storage = ChatStorage(self.db_path)
        conversation = storage.create_conversation(title="中断恢复")
        conversation_id = str(conversation["id"])
        run, _history = storage.create_chat_run(
            conversation_id,
            "是",
            [],
            {"id": "a1", "name": "Agent"},
            {},
            "craft",
            parent_job_id=parent_job_id,
        )
        run_id = str(run["id"])
        storage.append_run_event(run_id, {"type": "run_started"})
        for payload in kind_events if kind_events is not None else [
            {"type": "delta", "content": "先说结论："},
            {"type": "delta", "content": "这三张图都缺尺度锚定。"},
        ]:
            storage.append_run_event(run_id, payload)
        return storage, conversation_id, run_id

    def _restart(self) -> ChatStorage:
        """重新构造 storage = 服务重启（_initialize 里做启动清理）。"""
        return ChatStorage(self.db_path)

    def test_startup_records_conversation_and_kind_for_interrupted_tasks(self) -> None:
        _storage, conversation_id, run_id = self._seed_run()

        restarted = self._restart()

        self.assertEqual(
            [{"id": run_id, "conversation_id": conversation_id, "kind": "chat"}],
            restarted.interrupted_tasks,
            "启动清理必须把「这次被判为中断的是谁」交出来，否则运行层无从重建正文",
        )

    def test_recovery_persists_partial_assistant_message(self) -> None:
        _storage, conversation_id, run_id = self._seed_run()
        restarted = self._restart()

        recovered = _LightRunManager(_AppStub(restarted)).recover_interrupted_runs()

        self.assertEqual(recovered, 1)
        messages = restarted.get_conversation(conversation_id)["messages"]
        assistant = messages[-1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertIn("先说结论", assistant["content"], "事件流里已产出的正文必须落库")
        self.assertIn("尺度锚定", assistant["content"])
        self.assertTrue(assistant["metadata"].get("partial"), "前端靠它显示「未完成」标记")
        self.assertEqual(assistant["metadata"].get("run_id"), run_id)
        self.assertEqual(
            assistant["metadata"].get("error"),
            INTERRUPTED_TASK_REASON,
            "要写清中断原因，用户才知道这条为什么停在半路",
        )

    def test_recovery_keeps_tool_activity(self) -> None:
        _storage, conversation_id, _run_id = self._seed_run(
            kind_events=[
                {"type": "tool_start", "tool": "comfyui_batch", "args": {}},
                {"type": "tool_result", "tool": "comfyui_batch", "ok": True, "summary": "完成 3/6"},
            ]
        )
        restarted = self._restart()

        _LightRunManager(_AppStub(restarted)).recover_interrupted_runs()

        metadata = restarted.get_conversation(conversation_id)["messages"][-1]["metadata"]
        self.assertTrue(metadata.get("partial"))
        self.assertTrue(metadata.get("activity"), "工具活动时间线要保留（用户至少能看到跑到哪一步）")
        content = restarted.get_conversation(conversation_id)["messages"][-1]["content"]
        self.assertIn(
            INTERRUPTED_TASK_REASON,
            content,
            "一个字都没产出时，占位正文要写清中断原因，用户不必猜这条为什么停在这里",
        )

    def test_recovery_skips_child_jobs_and_empty_runs(self) -> None:
        _storage, child_conversation, _child_run = self._seed_run(parent_job_id="parent-run")

        def _empty_events() -> list[dict]:
            return []

        _storage2, empty_conversation, _empty_run = self._seed_run(kind_events=_empty_events())
        restarted = self._restart()

        recovered = _LightRunManager(_AppStub(restarted)).recover_interrupted_runs()

        self.assertEqual(
            recovered,
            0,
            "子 Job（parent_job_id 非空）与一个字都没产出的轮次都不该生成空壳消息",
        )
        for conversation_id in (child_conversation, empty_conversation):
            roles = [m["role"] for m in restarted.get_conversation(conversation_id)["messages"]]
            self.assertEqual(roles, ["user"], f"{conversation_id} 不该多出助手消息")

    def test_recovery_is_idempotent(self) -> None:
        _storage, conversation_id, _run_id = self._seed_run()
        restarted = self._restart()
        manager = _LightRunManager(_AppStub(restarted))

        self.assertEqual(manager.recover_interrupted_runs(), 1)
        self.assertEqual(manager.recover_interrupted_runs(), 0, "重复恢复不得写入第二条")
        self.assertEqual(
            len(restarted.get_conversation(conversation_id)["messages"]),
            2,
            "一条用户消息 + 一条重建的 partial 助手消息",
        )

    def test_startup_wires_recovery(self) -> None:
        source = (ROOT / "naiba" / "app.py").read_text(encoding="utf-8")
        self.assertIn(
            "self.runs.recover_interrupted_runs()",
            source,
            "恢复必须挂在启动流程上，否则重启后依旧看不到中断轮次的内容",
        )


class StreamingRowSurvivesRerenderTests(unittest.TestCase):
    """重渲染不得拆掉正在流式输出的那条助手气泡（点击任务面板那一行的直接后果）。"""

    def _body(self) -> str:
        return _function_body(_read_js("04-messages.js"), "export function renderMessages")

    def test_live_run_row_is_reattached_after_replace_children(self) -> None:
        body = self._body()
        self.assertIn("liveRunRow", body)
        self.assertIn(
            "container.append(liveRunRow)",
            body,
            "重渲染后必须把活动流的气泡挂回消息区，否则正在写的回复从屏幕上凭空消失",
        )
        self.assertLess(
            body.index("container.replaceChildren()"),
            body.index("container.append(liveRunRow)"),
            "复挂必须发生在清空之后（顺序颠倒等于没挂）",
        )

    def test_reattach_conditions_are_scoped(self) -> None:
        body = self._body()
        self.assertIn("state.abortController", body, "只在确有活动流时复挂")
        self.assertIn("state.runRow?.isConnected", body, "只搬还在消息区里的那一行")
        self.assertIn("state.runConversationId", body, "只搬属于当前会话的那一行")
        self.assertIn("run_id", body, "库里已有本轮终稿时不得再多出一条重复气泡")


class UpdateCheckIsManualTests(unittest.TestCase):
    """更新检查只在用户点「检查更新」时发起：启动不再自动查（2026-09-16 用户拍板）。"""

    def test_launcher_does_not_start_an_update_check(self) -> None:
        source = (ROOT / "launcher.py").read_text(encoding="utf-8")
        # 只看活的代码行：解释「为什么删掉」的注释里会提到那个调用。
        live = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn(
            "start_check",
            live,
            "启动自动查更新会让用户「没点检查更新就看到新版本」；检查只由 "
            "POST /api/update/check 触发",
        )

    def test_idle_copy_points_at_the_manual_entry(self) -> None:
        for name in ("index.html", "js/07-models-agents.js"):
            body = (ROOT / "public" / name).read_text(encoding="utf-8")
            self.assertIn("点「检查更新」才会去查新版本", body, f"{name} 的默认文案要说明是手动触发")

    def test_manual_route_still_checks(self) -> None:
        source = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")
        self.assertIn(
            "self.app.updater.start_check(force=True)",
            source,
            "/api/update/check 仍是唯一的检查入口（强制查，不吃缓存）",
        )


class UpdateRestartShowsWindowTests(unittest.TestCase):
    """自动更新后的重启必须把主窗口带出来，而不是只在托盘留一个图标。"""

    def test_update_script_does_not_hide_the_window(self) -> None:
        # 断言运行期常量（不是源码文本）：冻结版把源码打进 PYZ，只有常量还能在真机读到。
        from naiba.updater import APPLY_UPDATE_SCRIPT

        start_lines = [line for line in APPLY_UPDATE_SCRIPT.splitlines() if "Start-Process" in line]
        self.assertEqual(len(start_lines), 2, "重启脚本有首启 + 兜底重试两条启动命令")
        for line in start_lines:
            self.assertNotIn(
                "-WindowStyle Hidden",
                line,
                "本程序窗口化打包（console=False，没有控制台要藏），Hidden 会连主窗口"
                "一起藏起来：进程活着、托盘图标在，但界面不出来，用户只能到托盘双击唤回",
            )

    def test_launcher_shows_window_when_loaded(self) -> None:
        source = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn("self.window.events.loaded += self._on_window_loaded", source)
        self.assertIn("def _on_window_loaded", source)


if __name__ == "__main__":
    unittest.main()
