# -*- coding: utf-8 -*-
"""「立即更新」按钮状态守门（2026-10-23 事故）。

真机复现：下载到一半断网，更新失败；再点「检查更新」→ 又显示有自己的新版本，
但「立即更新」按下去没反应。同一根因还有第二个表现：安装包下完选择「不要更新」后，
按钮重新出现却同样点不动。

根因是**可见性和可点性走了两套口径**：点「立即更新」时先 ``button.disabled = true``
防重复提交，只有请求本身抛错才恢复；而下载失败/取消/放弃更新都发生在请求返回之后，
``renderUpdateStatus`` 只翻 ``hidden`` 不碰 ``disabled``，于是按钮回来了却永远置灰。

本文件钉死四条：

1. ``renderUpdateStatus`` 必须同时根据 ``canInstall`` 写 ``hidden`` 与 ``disabled``；
2. ``installUpdate`` 的请求在途标记必须在 ``finally`` 里复位，并按最新状态重算可点性；
3. ``checkUpdate`` 视为放弃上一次悬挂的安装请求（断网时请求可能迟迟不返回）；
4. 重开页面/另一客户端发起更新时，慢轮询必须接上快速轮询——否则「busy 就跳过」
   会让界面永远停在旧进度（检查中也一样）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _function(source: str, header: str) -> str:
    """截取一个顶层函数体（以行首 ``}`` 结束），不依赖行号。"""
    start = source.index(header)
    body = source[start:]
    end = body.index("\n}\n")
    return body[: end + 3]


class UpdateInstallButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _read("public/js/07-models-agents.js")
        self.bootstrap = _read("public/js/05-bootstrap.js")

    def test_render_syncs_disabled_with_visibility(self) -> None:
        render = _function(self.source, "export function renderUpdateStatus(status) {")
        self.assertIn("installButton.hidden = !canInstall;", render)
        self.assertIn(
            "installButton.disabled = !canInstall || installRequestPending;",
            render,
            "按钮可见时必须同时把 disabled 复位，否则失败/放弃后永远点不动",
        )

    def test_install_allowed_rule_blocks_ready_and_busy(self) -> None:
        rule = _function(self.source, "function updateInstallAllowed(status, selected) {")
        self.assertIn("!selected.current", rule)
        self.assertIn("!(status.ready && status.ready.version)", rule)
        self.assertIn("!UPDATE_BUSY_PHASES.includes(status.phase)", rule)

    def test_install_request_always_resets_pending_flag(self) -> None:
        install = _function(self.source, "export async function installUpdate() {")
        self.assertIn("installRequestPending = true;", install)
        finally_block = install[install.index("} finally {"):]
        self.assertIn("installRequestPending = false;", finally_block,
                      "在途标记必须无条件复位，否则按钮永久置灰")
        self.assertIn("button.disabled = !updateInstallAllowed(", finally_block,
                      "请求结束后要按最新状态重算可点性（失败=可重试）")

    def test_check_update_abandons_hung_install_request(self) -> None:
        check = _function(self.source, "export async function checkUpdate() {")
        self.assertIn("installRequestPending = false;", check)

    def test_fast_poll_keeps_running_when_status_fetch_fails(self) -> None:
        poll = _function(self.source, "export function startUpdatePoll() {")
        self.assertIn("if (updatePhaseBusy(state.bootstrap.update)) updatePollTimer", poll,
                      "单次取状态失败不能让下载进度/取消键停摆")

    def test_bootstrap_resumes_fast_poll_for_busy_phase(self) -> None:
        poll = _function(self.bootstrap, "export function startUpdatePoll() {")
        self.assertIn("startFastUpdatePoll", poll)
        self.assertIn("if (updateBusy(state.bootstrap.update)) startFastUpdatePoll();", poll)
        self.assertIn("if (updateBusy(next)) startFastUpdatePoll();", poll,
                      "另一个客户端发起更新时，慢轮询发现 busy 也要接上快速轮询")


if __name__ == "__main__":
    unittest.main()
