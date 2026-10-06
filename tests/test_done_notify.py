# -*- coding: utf-8 -*-
"""完成提醒链路的守门：完成提示音 + 托盘「任务完成」卡片 + 稍后重启悬浮卡修复。

背景（2026-10-06 三件事一起落地）：
1. **完成提示音**：AI 回复结束后前端合成一声「叮咚」（Web Audio，无音频资产），
   开关/音量落在 `appearance.done_sound` / `appearance.done_sound_volume`。
2. **托盘完成卡片**：窗口藏在托盘时，会话完成弹出 frameless 悬浮卡（标题=会话名称），
   `appearance.tray_done_toast` 控制；窗口是否真的隐藏由**后端**（Launcher）说了算。
3. **稍后重启悬浮卡 bug**：`syncUpdateFloat` 的显示条件曾不看 `updateReadyDismissed`，
   点了「稍后重启」→ 关设置 → close 监听触发渲染 → 悬浮卡照样弹出。

不变量（这里全部用关键串静态断言钉住，改动必须过一遍人脑）：
- `syncUpdateFloat` 的 show 表达式必须含 `updateReadyDismissed`（ready 分支）；
- `notifyRunDone` 只在 `event.type === 'done'` 时调用（error/cancelled 不提醒）；
- 弹卡前必须问后端 `naibaWindowHidden`（visibilityState 在 SW_HIDE 下不可信）；
- 卡片标题写 DOM 必须走 `textContent`（会话名任意字符，innerHTML 即 XSS 面）；
- Launcher 维护 `_window_hidden` 真值：hide 置 True、show 置 False；
- 「稍后重启」按版本重置：新版本 ready 必须重新提醒。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class DoneNotifyFrontendTests(unittest.TestCase):
    def _sound(self) -> str:
        return (ROOT / "public/js/20-sound.js").read_text(encoding="utf-8")

    def _run_stream(self) -> str:
        return (ROOT / "public/js/11-run-stream.js").read_text(encoding="utf-8")

    def _models(self) -> str:
        return (ROOT / "public/js/07-models-agents.js").read_text(encoding="utf-8")

    def _core(self) -> str:
        return (ROOT / "public/js/01-core.js").read_text(encoding="utf-8")

    def _settings(self) -> str:
        return (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    # ---- 完成提示音 ----

    def test_sound_module_synth_has_no_asset(self) -> None:
        """音效由 Web Audio 合成：存在 createOscillator，且不许引用任何音频文件资产。"""
        sound = self._sound()
        self.assertIn("createOscillator", sound, "提示音必须是现场合成（无音频资产）")
        self.assertIn("export function playDoneSound", sound)
        self.assertNotIn(".wav", sound.lower(), "不许引入音频文件（守门白名单+打包面都会变大）")
        self.assertNotIn(".mp3", sound.lower())

    def test_run_stream_notifies_only_on_done(self) -> None:
        """提醒只挂在成功结局上：error / cancelled 不许响不许弹。"""
        run_stream = self._run_stream()
        self.assertIn("notifyRunDone", run_stream, "done 分支必须调用 notifyRunDone")
        self.assertIn("import { notifyRunDone }", run_stream)
        self.assertNotIn("notifyRunDone(conversationId, 'error'", run_stream)
        # done 调用点必须紧跟 type === 'done' 判定（两处：流循环 + 收尾缓冲区）。
        self.assertEqual(run_stream.count("event.type === 'done'"), 2, "流循环与缓冲区兜底各一处")

    def test_sound_dedup_guard(self) -> None:
        """500ms 去重必须有：重连重放同一段流事件时不能叮两声。"""
        sound = self._sound()
        self.assertIn("lastPlayedAt", sound)
        self.assertIn("500", sound)

    # ---- 托盘完成卡片 ----

    def test_card_asks_backend_for_window_state(self) -> None:
        """弹卡前必须问后端窗口状态；visibilityState 只能当旧桥的降级兜底。"""
        sound = self._sound()
        self.assertIn("naibaWindowHidden", sound, "窗口是否隐藏以后端为准（SW_HIDE 下 visibilityState 不可信）")
        self.assertIn("naibaNotifyTaskDone", sound)
        self.assertIn('document.visibilityState === "hidden"', sound, "旧桥无 naibaWindowHidden 时的兜底分支要保留")

    def test_card_title_written_via_text_content(self) -> None:
        """会话名要进卡片 DOM：必须 textContent（innerHTML 等于把会话名当 HTML 执行）。"""
        card_page = (ROOT / "public/tray_card.html").read_text(encoding="utf-8")
        self.assertIn("textContent", card_page)
        self.assertNotIn("innerHTML", card_page)

    def test_card_page_exists_and_autocloses(self) -> None:
        card_path = ROOT / "public/tray_card.html"
        self.assertTrue(card_path.is_file(), "卡片窗口加载的静态页必须存在")
        page = card_path.read_text(encoding="utf-8")
        self.assertIn("naibaCardClose", page, "4 秒自毁走桥")
        self.assertIn("naibaCardOpen", page, "点击卡片唤回主窗口走桥")
        self.assertIn("naibaCardData", page, "数据经桥拉取，不走 URL query")

    def test_launcher_maintains_window_hidden_flag(self) -> None:
        """Launcher 必须维护 _window_hidden 真值：hide 置 True、show 置 False。"""
        launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn("self._window_hidden = True", launcher, "window.hide() 后置位")
        self.assertIn("self._window_hidden = False", launcher, "_show_window 唤回后清位")
        self.assertIn("def show_done_card", launcher)
        self.assertIn("def naibaNotifyTaskDone", launcher)
        self.assertIn("def naibaWindowHidden", launcher)
        self.assertIn("js_api=JsApi(srv.APP, launcher=self)", launcher, "主窗口桥必须带 launcher（否则读不到隐藏状态）")
        self.assertIn("latest wins", launcher, "同刻只保留一张卡的约定要写在注释里防误删")

    def test_main_window_dispatches_open_conversation_event(self) -> None:
        bind = self._bind()
        self.assertIn("naiba:open-conversation", bind, "卡片点击 → 主窗口跳会话的事件监听必须在")
        self.assertIn("openConversation(id)", bind)

    def test_settings_ui_wires_all_three_controls(self) -> None:
        """设置页三控件（音开关/音量/卡开关）+ 试听按钮必须齐，且事件都绑上。"""
        index = self._index()
        for dom_id in ("doneSoundToggle", "doneSoundVolume", "trayDoneToastToggle", "previewDoneSound"):
            self.assertIn(f'id="{dom_id}"', index)
        bind = self._bind()
        for dom_id in ("doneSoundToggle", "doneSoundVolume", "trayDoneToastToggle", "previewDoneSound"):
            self.assertIn(f"'#{dom_id}'", bind, f"{dom_id} 的事件没绑")
        settings = self._settings()
        for func in ("saveDoneSound", "saveDoneSoundVolume", "saveTrayDoneToast", "previewDoneSound"):
            self.assertIn(f"export async function {func}", settings) if func.startswith("save") else None
        self.assertIn("export function previewDoneSound", settings)

    def test_appearance_state_carries_new_keys(self) -> None:
        """applyAppearance 是前端外观状态的固定键集：三个新键掉一个，开关就会「存了没生效」。"""
        core = self._core()
        for key in ("done_sound", "done_sound_volume", "tray_done_toast"):
            self.assertIn(key, core)
            self.assertIn(key, (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8"))

    # ---- 稍后重启悬浮卡修复 ----

    def test_update_float_respects_dismissed(self) -> None:
        """show 表达式的 ready 分支必须含 updateReadyDismissed（bug 本体，删了就复发）。"""
        body = self._models()
        start = body.index("export function syncUpdateFloat")
        segment = body[start: start + 1200]
        self.assertIn("!dialogOpen && (downloading || (ready && !updateReadyDismissed))", segment,
                      "syncUpdateFloat 的 show 条件被改动：ready 分支必须继续尊重「稍后重启」已读标记")

    def test_dismissed_resets_on_new_version(self) -> None:
        models = self._models()
        self.assertIn("updateReadyDismissedVersion", models)
        self.assertIn("!== updateReadyDismissedVersion", models,
                      "新版本 ready 必须重置已读（vA 稍后不能压住 vB 的提醒）")
        # discard 后已读态整体回零。
        discard = models[models.index("export async function discardUpdate"):]
        discard = discard[: discard.index("\n}")]
        self.assertIn("updateReadyDismissedVersion = ''", discard)


if __name__ == "__main__":
    unittest.main()
