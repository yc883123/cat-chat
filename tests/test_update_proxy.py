# -*- coding: utf-8 -*-
"""守门：更新链路三项体验改造（2026-10-02 定稿）。

背景：更新下载此前**没有任何可见进度**（不知道是网卡了还是在跑）、**无法取消**、下载完成
后**强制自动重启**，而且新装默认「强制直连」导致直连 GitHub 长时间卡住时用户无处下手。

四条不可退让的口径：

1. ``settings.update_proxy`` 是**独立于全局 proxy** 的 scoped 覆盖项，四态
   ``inherit``（默认，行为与引入前逐字一致）/ ``system`` / ``direct`` / ``manual``；
   只作用于更新检查与下载，全局网络策略不受影响。地址校验口径与全局 proxy 一致
   （仅 http/https、必须带端口）。
2. 下载过程中必须上报 ``download.{received,total,speed,percent,stalled}``，并在
   ``phase == "downloading"`` 时给出 ``can_cancel``；前端据此画进度条、标卡死、给取消键。
3. 下载+校验成功后进入 ``ready`` 态（**不自动替换 exe、不写 pending 标记**），
   由 ``/api/update/apply`` 在用户点「立即重启」时安装；状态跨重启保留
   （``ready-update.json`` + 安装包落盘），设置页常驻入口。
4. 输入框展开/折叠只改高度上限、不动 DOM 结构：折叠 180px、展开 72dvh，
   编辑模式下 composer 被搬进气泡（可能在窗口顶部）时向下生长，靠滚动容器推进而非裁剪。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _config_app():
    from naiba.app import NaibaChatApp
    from naiba.paths import PathContext

    tmp = tempfile.TemporaryDirectory(prefix="naiba_upx_")
    root = Path(tmp.name)
    return NaibaChatApp(paths=PathContext.local(root, root / "config.json")), tmp


class UpdateProxySettingsTests(unittest.TestCase):
    """偏好键本身：默认跟随全局、可落盘、有校验、能注入 updater。"""

    def setUp(self) -> None:
        self.app, self._tmp = _config_app()
        self.addCleanup(self._tmp.cleanup)

    def test_default_is_inherit_and_published(self) -> None:
        public = self.app.config.public()
        self.assertIn("update_proxy", public, "偏好键没有随 bootstrap 下发")
        self.assertEqual(public["update_proxy"], {"mode": "inherit", "url": ""},
                         "默认必须跟随全局（等价于引入该设置前的行为）")
        saved = json.loads((Path(self._tmp.name) / "config.json").read_text(encoding="utf-8"))
        self.assertIn("update_proxy", saved, "默认值必须真的写进 config.json，不能只活在 public() 兜底里")

    def test_manual_mode_persists_and_reaches_updater(self) -> None:
        self.app.api_update_runtime_settings(
            {"update_proxy": {"mode": "manual", "url": "127.0.0.1:7897"}}
        )
        self.assertEqual(self.app.updater.proxy_override["mode"], "manual")
        self.assertIn("7897", self.app.updater.proxy_override["url"])
        self.assertEqual(self.app.updater.status()["proxy"]["mode"], "manual")
        reloaded = json.loads((Path(self._tmp.name) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(reloaded["update_proxy"]["mode"], "manual")

    def test_invalid_values_are_rejected(self) -> None:
        cases = [
            ({"mode": "nonsense"}, "更新代理模式"),
            ({"mode": "manual", "url": ""}, "未填写代理地址"),
            ({"mode": "manual", "url": "socks5://127.0.0.1:7890"}, "更新代理地址格式不正确"),
            ({"mode": "manual", "url": "http://127.0.0.1"}, "缺少端口号"),
            ("not-a-dict", "update_proxy 必须是对象"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as ctx:
                    self.app.config.update_settings({"update_proxy": payload})
                self.assertIn(expected, str(ctx.exception))
        # 校验失败不得污染已存的值
        self.assertEqual(self.app.config.data["update_proxy"], {"mode": "inherit", "url": ""})

    def test_global_proxy_is_untouched_by_update_proxy(self) -> None:
        """两者解耦：改更新代理不能顺带改全局出站策略（TUN 用户全局可直连、更新单挂代理）。"""
        before = dict(self.app.config.data.get("proxy") or {})
        self.app.api_update_runtime_settings({"update_proxy": {"mode": "direct"}})
        self.assertEqual(dict(self.app.config.data.get("proxy") or {}), before)

    def test_update_proxy_does_not_touch_release_protocol_constants(self) -> None:
        """改名/加设置都不许碰更新协议三常量（旧客户端逐字校验）。"""
        from naiba import updater

        self.assertEqual(updater.REPOSITORY, "yc883123/naiba-chat")
        self.assertEqual(updater.MANIFEST_ASSET, "naiba-chat-update.json")
        self.assertEqual(updater.EXECUTABLE_ASSET, "naiba-chat.exe")


class UpdatePanelMarkupTests(unittest.TestCase):
    """设置页与悬浮卡的静态契约。"""

    def setUp(self) -> None:
        self.html = _read("public", "index.html")
        self.css = _read("public", "styles.css")
        self.panel_js = _read("public", "js", "07-models-agents.js")
        self.bind_js = _read("public", "js", "15-bind-events.js")

    def test_panel_has_progress_cancel_and_restart_choices(self) -> None:
        for element_id in (
            "updateProgress", "updateProgressFill", "updateProgressText", "updateProgressSpeed",
            "cancelUpdate", "restartNow", "restartLater", "updateReady", "readyApply", "readyDiscard",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html, f"缺少更新面板元素 #{element_id}")

    def test_panel_has_update_proxy_switch(self) -> None:
        for mode in ("inherit", "system", "direct", "manual"):
            element_id = f"updateProxy{mode.capitalize()}"
            with self.subTest(mode=mode):
                self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('name="updateProxyMode"', self.html)
        self.assertIn('id="updateProxyUrl"', self.html)
        self.assertIn('id="updateProxyState"', self.html, "必须有「本次更新走：…」的生效说明")

    def test_proxy_state_label_is_prefixed_once(self) -> None:
        """状态行的「本次更新走：」只许拼一次。

        前端渲染是 ``本次更新走：${note}``，因此服务端 ``status().proxy.note`` 必须是
        **裸描述**；两边都带前缀时界面会显示成「本次更新走：更新链路：跟随全局（…）」
        （2026-10-02 截图核对时抓到并已修正）。
        """
        js = _read("public", "js", "07-models-agents.js")
        self.assertIn("`本次更新走：${note}`", js)
        updater = _read("naiba", "updater.py")
        self.assertIn('return f"{message}（更新链路：{note}）"', updater,
                      "「更新链路：」前缀只应出现在错误文案里")
        self.assertNotIn('return f"更新链路：{note}"', updater,
                         "_proxy_note 必须返回裸描述，前缀交给调用方拼")

    def test_proxy_options_are_segmented_cells(self) -> None:
        """四态必须是「等宽分段控件」，原生 radio 只做语义载体。

        真机实测的竖排根因：``.settings-dialog label input { height: var(--set-ctl-h) }``
        （38px）会把原生 radio 连同 1:1 比例一起放大成 39×38 的巨型圆点，并挤得选项文字
        只剩 13px 宽 ⇒ 「跟随全局」被拆成四行竖排（label 79×64）。因此选项必须自带类名、
        等宽分栏，并把 radio 铺满整格 + 视觉隐藏（铺满而非 1px：点击命中点要落在它自己身上）。
        """
        self.assertIn('class="update-proxy-option"', self.html)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", self.css,
                      "四态必须等宽分栏，不能被内容宽度挤成参差")
        block = self.css[self.css.index(".settings-dialog .update-proxy-option input {"):]
        block = block[:block.index("}")]
        self.assertIn("position: absolute", block)
        self.assertIn("opacity: 0", block, "原生圆点必须视觉隐藏，只留选中胶囊")
        self.assertIn("height: 100%", block,
                      "必须铺满整格：留 1px 会让命中点暴露在 span 下，点击/合规校验都容易失手")
        # 全局 label input 规则不能把 radio 撑回来：本块选择器特异度必须高于它 (0,1,1)。
        self.assertNotIn("\n.update-proxy-options label {", self.css,
                         "旧的裸 label 规则必须删掉，否则它会与分段样式打架")

    def test_float_card_exists_with_both_actions(self) -> None:
        for element_id in ("updateFloat", "updateFloatFill", "updateFloatCancel", "updateFloatApply", "updateFloatDiscard"):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)

    def test_float_card_position_rules(self) -> None:
        """宽屏：右下角但抬高到输入框之上；窄屏：退化为顶部卡片（否则必然遮挡全宽输入区）。"""
        block = self.css[self.css.index(".update-float {"):]
        block = block[:block.index("\n}")]
        self.assertIn("position: fixed", block)
        self.assertIn("right: 14px", block)
        self.assertRegex(block, r"bottom: calc\(140px \+ max\(14px", "必须抬起到底部输入框之上")
        mobile = self.css[self.css.index("@media (max-width: 760px) {"):]
        mobile_block = mobile[mobile.index(".update-float {"):]
        mobile_block = mobile_block[:mobile_block.index("\n  }")]
        self.assertIn("top: calc(56px", mobile_block, "窄屏必须改为顶部吸附")
        self.assertIn("bottom: auto", mobile_block)

    def test_hidden_attribute_wins_over_flex_display(self) -> None:
        """作者样式的 display 会顶掉 UA 的 [hidden]，有 flex 的元素必须显式压回。

        真机冒烟抓到过一次：`#updateProgress` 是 `.update-status` 的直接子元素，继承到
        `.update-status > div { display:flex }` ⇒ 没下载时进度条就露在外面。
        """
        self.assertIn(".update-ready[hidden] { display: none; }", self.css)
        self.assertIn(".update-float[hidden] { display: none; }", self.css)
        # 进度块既要在隐藏时压回 hidden，又要在显示时压过父级的 display:flex（否则轨道宽塌成 0）
        self.assertIn(".settings-dialog .update-status > div.update-progress { display: block; }", self.css)
        self.assertIn(".settings-dialog .update-status > div.update-progress[hidden] { display: none; }", self.css)
        # 面板里所有会「藏/露」的容器都必须在 CSS 里有对应的 [hidden] 压回规则
        for element_id in ("updateProgress", "updateReady", "updateFloat"):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)

    def test_download_progress_and_restart_flow_in_js(self) -> None:
        for marker in ("startUpdatePoll", "syncUpdateFloat", "cancelUpdate", "applyUpdate", "discardUpdate", "saveUpdateProxy"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.panel_js)
        self.assertIn("/api/update/cancel", self.panel_js)
        self.assertIn("/api/update/apply", self.panel_js)
        self.assertIn("update_proxy", self.panel_js, "保存必须走 /api/settings 的 update_proxy 单项键")

    def test_bindings_wired(self) -> None:
        for marker in (
            "#cancelUpdate", "#restartNow", "#restartLater", "#readyApply", "#readyDiscard",
            "#updateFloatCancel", "#updateFloatApply", "#expandComposer", "syncUpdateFloat",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.bind_js)


class ComposerExpandGuardTests(unittest.TestCase):
    """输入框展开/折叠：按钮位置、高度契约、编辑态不截断、发送后收回。"""

    def setUp(self) -> None:
        self.html = _read("public", "index.html")
        self.css = _read("public", "styles.css")
        self.skill_refs = _read("public", "js", "13-skill-refs.js")
        self.run_stream = _read("public", "js", "11-run-stream.js")
        self.bind_js = _read("public", "js", "15-bind-events.js")

    def test_toggle_button_sits_in_button_row_before_send(self) -> None:
        html = self.html
        expand_at = html.index('id="expandComposer"')
        send_at = html.index('id="sendButton"')
        composer_at = html.index('<form class="composer" id="composerForm">')
        self.assertGreater(expand_at, composer_at)
        self.assertLess(expand_at, send_at, "双三角键必须在发送键旁的按钮列里（不遮挡文本）")

    def test_expanded_cap_matches_css(self) -> None:
        import re

        m = re.search(r"MAX_COMPOSER_H_EXPANDED_RATIO = ([0-9.]+);", self.skill_refs)
        self.assertIsNotNone(m, "展开上限要提成具名常量")
        ratio = float(m.group(1))
        expected_dvh = round(ratio * 100)
        self.assertIn(f"max-height: {expected_dvh}dvh", self.css,
                      f"JS 比例 {ratio} 与 CSS 上限 {expected_dvh}dvh 必须同值（改一处必改另一处）")
        self.assertIn(".composer-wrap.is-expanded textarea", self.css)

    def test_expanded_state_does_not_clamp_inline_edit(self) -> None:
        """编辑态 wrap 本来就是 max-height:none/overflow:visible，展开规则必须排除它。"""
        self.assertIn(".composer-wrap.is-expanded:not(.is-inline-edit)", self.css)

    def test_toggle_queries_real_wrap_and_reflects_aria(self) -> None:
        body = self.skill_refs[self.skill_refs.index("export function setComposerExpanded("):]
        body = body[:body.index("\n}\n")]
        self.assertIn(".composer-wrap:not(.edit-placeholder)", body,
                      "编辑态下真 composer 前面还有个 hidden 占位锚点，直接查 .composer-wrap 会抓错")
        self.assertIn("aria-pressed", body)
        self.assertIn("is-expanded", body)
        self.assertIn("resizeTextarea()", body, "改完上限必须立刻重量高")

    def test_resize_reads_expanded_state_from_dom(self) -> None:
        self.assertIn("composerExpanded ? expandedCap() : MAX_COMPOSER_H", self.skill_refs,
                      "高度唯一写入点必须区分折叠/展开上限")

    def test_send_collapses_expanded_composer(self) -> None:
        self.assertIn("collapseComposerIfExpanded()", self.run_stream,
                      "发送后内容已清空，应收回折叠态")
        self.assertIn("collapseComposerIfExpanded", self.skill_refs)


if __name__ == "__main__":
    unittest.main()
