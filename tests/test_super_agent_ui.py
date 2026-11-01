# -*- coding: utf-8 -*-
"""任务总管入口、控制面、创建弹窗和移动端样式契约。"""

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

    def test_frontend_control_api_contract(self) -> None:
        source = self._read("public/js/28-super-agent.js")
        self.assertIn('api("/api/super-agent/threads")', source)
        self.assertIn("/nodes`", source)
        self.assertIn("/events", source)
        self.assertIn("/artifacts`", source)
        self.assertIn("/approvals`", source)
        self.assertGreaterEqual(source.count('method: "POST"'), 3)
        self.assertIn('body: { action, node_id: nodeId, request_id:', source)
        self.assertIn('body: { status, request_id:', source)
        self.assertIn('start_request: startRequest', source)
        self.assertIn('/api/providers/models', source)
        self.assertIn('provider_id: providerId', source)
        self.assertIn('model_key: providerKey', source)
        self.assertIn('该供应商暂无可用模型', source)
        self.assertNotIn('superAgentCreateModelName', source)
        self.assertNotIn('superAgentCreateModelKey', source)
        self.assertIn('openStudioView();', source)
        self.assertIn('setStudioPage(domain);', source)
        self.assertNotIn('window.prompt(', source)
        html = self._read("public/index.html")
        for marker in ("superAgentCreateDialog", "superAgentCreateBookId", "superAgentCreateStage", "superAgentCreateProvider", "superAgentCreateModel", "superAgentCreateSubmit"):
            self.assertIn(marker, html)
        self.assertNotIn('id="superAgentCreateModelName"', html)
        self.assertNotIn('id="superAgentCreateModelKey"', html)
        self.assertIn('class="writer-dialog-form super-agent-create-form"', html)
        self.assertIn('class="writer-manage-head"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertNotIn("只读查看", html)
        self.assertIn("任务已结束，无需取消", source)
        self.assertIn("操作失败：", source)
        self.assertIn('super-agent-create-dialog', self._read("public/styles.css"))
        self.assertIn('height: 100dvh', self._read("public/styles.css"))
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

    def test_stage_chain_projection_and_cursor_contract(self) -> None:
        source = self._read("public/js/28-super-agent.js")
        html = self._read("public/index.html")
        css = self._read("public/styles.css")
        for field in ("stage_key", "sequence", "chain_version", "current_node_id"):
            self.assertIn(field, source)
        for status in ("blocked", "ready", "running", "waiting_user", "failed"):
            self.assertIn(f"status-{status}", css)
        self.assertIn("superAgentStageChain", html)
        self.assertIn("superAgentChainMeta", html)
        self.assertIn("?after=", source)
        self.assertIn("mergeEvents", source)
        self.assertIn("data-node-id", source)
        self.assertIn("data-super-action", source)
        self.assertIn("data-super-approval", source)
        self.assertIn("data-super-return-domain", source)

    def test_mobile_layout_is_single_column_and_wrap_safe(self) -> None:
        css = self._read("public/styles.css")
        self.assertIn(".super-agent-columns { display: flex; flex-direction: column;", css)
        self.assertIn("min-width: 0", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertRegex(css, r"@media \(max-width: 760px\)[\s\S]*?\.super-agent-side-panel")


if __name__ == "__main__":
    unittest.main()
