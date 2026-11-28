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

import json
import shutil
import subprocess
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


@unittest.skipUnless(shutil.which("node"), "更新状态行为测试需要 Node.js")
class UpdateErrorPriorityTests(unittest.TestCase):
    """执行真实渲染函数，用固定状态验证最终文案与按钮，不依赖网络或时钟。"""

    NODE_DRIVER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor() {
    this.children = [];
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.style = {};
    this.classList = { toggle() {} };
  }
  set textContent(value) { this.children = [String(value)]; }
  get textContent() {
    return this.children.map(child => typeof child === 'string' ? child : child.textContent).join('');
  }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
}
const elements = new Map();
const $ = selector => {
  if (!elements.has(selector)) elements.set(selector, new Element());
  return elements.get(selector);
};
$('#settingsDialog').open = Boolean(input.dialogOpen);
const context = {
  $, state: { bootstrap: { update: input.status } },
  document: { createElement: () => new Element(), createTextNode: text => String(text) },
  UPDATE_BUSY_PHASES: ['checking', 'downloading', 'restarting'],
  UPDATE_RELEASE_URL: 'https://example.invalid/releases',
  updateNotesCache: new Map(),
  updateReadyDismissed: Boolean(input.dismissed),
  updateReadyDismissedVersion: input.dismissed ? String(input.status.ready?.version || '') : '',
  installRequestPending: false,
  initUpdateBrowserControls() {}, renderUpdateSource() {}, renderUpdateVersionList() {},
  renderUpdateNotes() {}, ensureUpdateNotes() {}, renderUpdateArtifacts() {}, renderUpdateProxyState() {},
};
vm.createContext(context);
vm.runInContext(input.functions + '\nrenderUpdateStatus(' + JSON.stringify(input.status) + ');', context);
const result = {};
for (const id of ['updateMessage', 'installUpdate', 'restartNow', 'restartLater',
                  'updateFloat', 'updateFloatTitle', 'updateFloatMeta', 'updateFloatTrack',
                  'updateFloatCancel', 'updateFloatApply', 'updateFloatDiscard']) {
  const element = $('#' + id);
  result[id] = { text: element.textContent, hidden: element.hidden, disabled: element.disabled };
}
result.manualLinks = $('#updateMessage').children.filter(child => child.href).map(child => child.href);
process.stdout.write(JSON.stringify(result));
"""

    def render(self, status: dict, *, dialog_open=False, dismissed=False) -> dict:
        source = _read("public/js/07-models-agents.js")
        functions = "\n".join(
            _function(source, header).removeprefix("export ")
            for header in (
                "export function renderUpdateStatus(status) {",
                "export function syncUpdateFloat(status = state.bootstrap.update || {}) {",
                "function updateInstallAllowed(status, selected) {",
                "function updateVersionAllowed(release) {",
                "function versionLabel(release) {",
                "function formatUpdateSize(bytes) {",
            )
        )
        result = subprocess.run(
            ["node", "-e", self.NODE_DRIVER], cwd=ROOT,
            input=json.dumps({"functions": functions, "status": status,
                              "dialogOpen": dialog_open, "dismissed": dismissed}),
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def status(self, **overrides) -> dict:
        return {
            "supported": True, "mode": "frozen", "phase": "error",
            "current_version": "4.8.1", "latest_version": "4.8.2",
            "releases": [{"tag": "v4.8.2", "version": "4.8.2", "current": False}],
            **overrides,
        }

    def test_error_survives_other_status_messages(self) -> None:
        current = [{"tag": "v4.8.1", "version": "4.8.1", "current": True}]
        cases = {
            "pending_ok": {"pending_verification": {"pending": True, "ok": True, "target_version": "4.8.2"}},
            "pending_failed": {"pending_verification": {"pending": True, "ok": False, "target_version": "4.8.2"}},
            "source": {"mode": "source"},
            "current": {"releases": current},
            "manual_required": {"manual_download_required": True},
            "manual_available": {"manual_update_available": True},
            "manual_phase": {"phase": "manual"},
            "no_installable_release": {"releases": [{"tag": "v2.0.0", "version": "2.0.0"}]},
            "unsupported": {"supported": False},
            "available": {"phase": "available"},
            "combined": {"releases": current, "manual_download_required": True,
                         "pending_verification": {"pending": True, "ok": True, "target_version": "4.8.2"}},
        }
        error = '更新失败：<校验错误> & "请重试"'
        for name, fields in cases.items():
            with self.subTest(name=name):
                result = self.render(self.status(error=error, **fields))
                self.assertEqual(result["updateMessage"]["text"], error)
                self.assertEqual(result["manualLinks"], [])

    def test_normal_status_messages_and_manual_link_remain(self) -> None:
        cases = [
            ({}, "检查更新失败。"),
            ({"phase": "available"}, "将安装 4.8.2，下载完成后可选择立即重启。"),
            ({"pending_verification": {"pending": True, "ok": True, "target_version": "4.8.2"}},
             "上次更新已完成并验证通过（4.8.2）。"),
            ({"pending_verification": {"pending": True, "ok": False, "target_version": "4.8.2"}},
             "上次更新到 4.8.2 后版本校验失败，请重新检查更新或手动安装。"),
            ({"mode": "source"}, "源码模式不支持一键更新，请在终端中执行 git pull --ff-only origin master。"),
            ({"releases": [{"tag": "v4.8.1", "version": "4.8.1", "current": True}]},
             "当前已安装该版本，无需更新。"),
            ({"manual_download_required": True, "manual_release_url": "https://example.invalid/manual"},
             "2.0.0 及更早版本不支持自动更新，请前往 GitHub Release 手动下载。"),
        ]
        for error in (None, ""):
            for fields, expected in cases:
                with self.subTest(error=error, fields=fields):
                    status = self.status(**fields)
                    if error is not None:
                        status["error"] = error
                    result = self.render(status)
                    self.assertEqual(result["updateMessage"]["text"], expected)
                    self.assertEqual(result["manualLinks"],
                                     ["https://example.invalid/manual"] if fields.get("manual_download_required") else [])

    def test_ready_error_is_visible_in_float_without_changing_actions(self) -> None:
        status = self.status(phase="ready", ready={"version": "4.8.2"})
        normal = self.render(status)
        self.assertEqual(normal["updateFloatMeta"]["text"], "4.8.2 已下载，重启后生效")
        failed = self.render({**status, "phase": "error", "error": "替换程序失败，请重试。"})
        self.assertEqual(failed["updateMessage"]["text"], "替换程序失败，请重试。")
        self.assertEqual(failed["updateFloatMeta"]["text"], "替换程序失败，请重试。")
        self.assertFalse(failed["updateFloat"]["hidden"])
        for id in ("installUpdate", "restartNow", "restartLater", "updateFloatTitle",
                   "updateFloatTrack", "updateFloatCancel", "updateFloatApply", "updateFloatDiscard"):
            self.assertEqual(failed[id], normal[id], id)
        self.assertTrue(failed["installUpdate"]["hidden"])
        self.assertFalse(failed["updateFloatApply"]["hidden"])
        self.assertFalse(failed["updateFloatDiscard"]["hidden"])

    def test_ready_error_float_respects_settings_and_later_choice(self) -> None:
        status = self.status(error="重启失败", ready={"version": "4.8.2"})
        for options in ({"dialog_open": True}, {"dismissed": True}):
            with self.subTest(options=options):
                result = self.render(status, **options)
                self.assertTrue(result["updateFloat"]["hidden"])
                self.assertEqual(result["updateMessage"]["text"], "重启失败")


if __name__ == "__main__":
    unittest.main()
