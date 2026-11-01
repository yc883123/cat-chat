# -*- coding: utf-8 -*-
"""P2 第一切片：任务总管入口、独立只读视图和移动端样式契约。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SuperAgentUITests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_sidebar_entry_and_independent_view_exist_once(self) -> None:
        html = self._read("public/index.html")
        self.assertEqual(html.count('id="openSuperAgent"'), 1)
        self.assertEqual(html.count('id="superAgentView"'), 1)
        sidebar = html[html.index('<aside class="sidebar"'):html.index("</aside>")]
        self.assertIn('id="openSuperAgent"', sidebar)
        view = html[html.index('<section class="super-agent-view"'):html.index('<section class="composer-wrap">')]
        for marker in ("superAgentThreadList", "superAgentDetail", "superAgentNodeList", "superAgentEventList", "superAgentApprovalList", "superAgentArtifactList"):
            self.assertIn(f'id="{marker}"', view)

    def test_legacy_tasks_dialog_is_preserved_with_explicit_handoff(self) -> None:
        html = self._read("public/index.html")
        dialog = html[html.index('<dialog id="tasksDialog"'):html.index("</dialog>", html.index('<dialog id="tasksDialog"'))]
        self.assertIn('id="taskList"', dialog)
        self.assertIn('id="openTaskManager"', dialog)
        self.assertNotIn("创建任务", dialog)

    def test_frontend_uses_read_only_api_module(self) -> None:
        source = self._read("public/js/28-super-agent.js")
        self.assertIn('api("/api/super-agent/threads")', source)
        self.assertIn("/nodes`", source)
        self.assertIn("/events`", source)
        self.assertIn("/artifacts`", source)
        self.assertIn("/approvals`", source)
        self.assertNotIn("method: \"POST\"", source)
        self.assertNotIn("/pause", source)
        self.assertNotIn("/cancel", source)
        self.assertNotIn("/retry", source)
        self.assertIn('bindSuperAgentEvents()', self._read("public/js/15-bind-events.js"))

    def test_app_view_contract_and_no_workbench_page_changes(self) -> None:
        core = self._read("public/js/01-core.js")
        self.assertIn("appView: 'chat'", core)
        module = self._read("public/js/28-super-agent.js")
        self.assertIn('setAppView(next)', module)
        self.assertIn('setAppView(view)', core)
        for name in ("23-labeler.js", "24-comfyui.js", "25-novel.js", "27-writer.js"):
            source = self._read(f"public/js/{name}")
            self.assertNotIn("superAgentView", source)
            self.assertNotIn("/api/super-agent", source)
        bind = self._read("public/js/15-bind-events.js")
        self.assertEqual(bind.count("#openChatMode"), 1, "顶层 Chat 入口只应在组合根绑定一次")

    def test_mobile_layout_is_single_column_and_wrap_safe(self) -> None:
        css = self._read("public/styles.css")
        self.assertIn(".super-agent-columns { display: flex; flex-direction: column;", css)
        self.assertIn("min-width: 0", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertRegex(css, r"@media \(max-width: 760px\)[\s\S]*?\.super-agent-side-panel")


if __name__ == "__main__":
    unittest.main()
