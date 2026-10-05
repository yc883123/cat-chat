# -*- coding: utf-8 -*-
"""护栏：输入框自增高必须按「内容」量，不能把折行后的 placeholder 算进去（§九.128）。

事故形态（2026-09-22 用户手机截图）：用了插话之后，手机端输入框变成半屏高，一个字符都没打
也降不下来；本轮结束后也不回落。

根因：`resizeTextarea()` 用 `textarea.scrollHeight` 量内容高，而 **Chrome 把折行后的
placeholder 也算成内容高度**。手机 360px 宽下，运行中的占位符「回复进行中…（输入后 Enter
加入插话队列）」有 23 字，再叠加插话键把输入区从 132px 挤到 88px ⇒ 折成 5 行，空输入框被
量成 **155px**（同一只空框、无占位符时只有 39px）。桌面 composer 宽约 880px，同一句一行放得下，
所以这个坑只在手机 + 只在使用插话时出现。

因此有两条互相独立的契约要守住，缺一不可：

1. **量高时必须临时摘掉 placeholder**（摘 → 量 → 放回，同一帧内同步完成）；
2. **凡是「placeholder 或输入区宽度」变化之后，都要重算一次高度**——否则上一次的读数会
   留在屏幕上。当前两个必须重算的调用点是 `updateContextComposerLock()`（placeholder 唯一
   写入点 + 插话键显隐经由 `updateSendButtonState`）与 `fillContextResetSeed()`（程序化写值
   不触发 `input` 事件）。

只做 ①：run 结束时草稿仍停在窄宽度下的高值。
只做 ②：读数本身被占位符污染，重算多少次都是错的。

真机形态另有 `verify/composer_height_smoke.cjs`（13 项断言，手机视口 360×780·DPR3）。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _fn_body(src: str, marker: str) -> str:
    """取某个顶层函数的正文（到列 0 的 `}` 为止）。"""
    start = src.index(marker)
    return src[start:src.index("\n}\n", start)]


class ComposerHeightSourceTests(unittest.TestCase):
    """前端源码守门：摘占位符 + 两处重算调用点。"""

    def setUp(self):
        self.skill_refs_js = _read("public", "js", "13-skill-refs.js")
        self.media_js = _read("public", "js", "03-media.js")
        self.messages_js = _read("public", "js", "04-messages.js")
        self.css = _read("public", "styles.css")

    # ---- ① 量高时不得把 placeholder 算进内容高度 ----

    def test_resize_strips_placeholder_before_measuring(self):
        """量高前必须把 `input.placeholder` 置空，并在 `finally` 里放回。"""
        body = _fn_body(self.skill_refs_js, "export function resizeTextarea() {")
        self.assertIn("const placeholder = input.placeholder;", body,
                      "要先记住原占位符，才能量完放回")
        self.assertIn("input.placeholder = '';", body,
                      "量高前必须摘掉占位符：Chrome 会把折行 placeholder 算进 scrollHeight")
        try_at = body.index("try {")
        measure_at = body.index("scrollHeight")
        finally_at = body.index("} finally {")
        self.assertLess(try_at, measure_at,
                        "摘占位符必须包住量高那一步（量高之前）")
        self.assertLess(measure_at, finally_at,
                        "量高必须写在 try 里，靠 finally 恢复")
        self.assertIn("input.placeholder = placeholder;", body,
                      "finally 里必须把占位符放回，否则用户永远看不到提示语")

    def test_resize_does_not_leave_placeholder_empty_on_throw(self):
        """恢复必须走 `finally`，不能写在 try 末尾的正常路径上。"""
        body = _fn_body(self.skill_refs_js, "export function resizeTextarea() {")
        finally_body = body[body.index("} finally {"):]
        self.assertIn("input.placeholder = placeholder;", finally_body,
                      "只有 finally 里的恢复才能在量高抛错时也生效")

    def test_resize_resets_height_before_measuring(self):
        """量高前先 `height = 'auto'`，否则量到的是上一次的固定高（越量越高的经典 bug）。"""
        body = _fn_body(self.skill_refs_js, "export function resizeTextarea() {")
        auto_at = body.index("input.style.height = 'auto';")
        measure_at = body.index("scrollHeight")
        self.assertLess(auto_at, measure_at, "必须先清成 auto 再量")

    def test_resize_caps_height_and_cap_matches_css(self):
        """封顶值必须与 styles.css 的 `.composer textarea { max-height }` 同值。

        2.10 起多了一档「展开态」上限（视口比例，见 test_update_proxy.py 的
        ComposerExpandGuardTests）：折叠上限仍是 `MAX_COMPOSER_H`，且必须与 CSS 同值；
        量高行统一用 `cap` 变量，避免出现第二条量高路径。
        """
        body = _fn_body(self.skill_refs_js, "export function resizeTextarea() {")
        m = re.search(r"MAX_COMPOSER_H = (\d+);", self.skill_refs_js)
        self.assertIsNotNone(m, "封顶值要提成具名常量，别在量高行里藏魔数")
        cap = int(m.group(1))
        self.assertIn("Math.min(input.scrollHeight, cap)", body,
                      "量高必须封顶，否则长草稿会把输入框顶穿屏幕")
        self.assertIn("composerExpanded ? expandedCap() : MAX_COMPOSER_H", body,
                      "折叠态上限必须仍是 MAX_COMPOSER_H（展开态另有按视口比例的上限）")
        self.assertIn("Math.max(content, expandedMin())", body,
                      "展开态必须有真实下限（S1）：短草稿点展开也得变高（否则「点了没反应」复发）")
        self.assertRegex(self.skill_refs_js, r"MIN_COMPOSER_H_EXPANDED_RATIO = ([0-9.]+);",
                         "展开下限比例要提成具名常量（与 styles.css 的固定值兜底同源）")
        self.assertRegex(
            self.css,
            r"\.composer textarea \{[^}]*max-height: %dpx" % cap,
            f"JS 封顶 {cap}px 与 CSS max-height 必须同值（改一处就得改另一处）",
        )

    def test_resize_guards_missing_input(self):
        """`#messageInput` 不在（例如设置页/开始页局部渲染）时直接返回，不许抛。"""
        body = _fn_body(self.skill_refs_js, "export function resizeTextarea() {")
        self.assertIn("if (!input) return;", body)

    def test_no_other_place_measures_textarea_by_scrollheight(self):
        """负向断言：`#messageInput` 的高度只能由 `resizeTextarea` 一处写入。

        第二处直写 `style.height = …scrollHeight` 就等于绕开了「摘占位符」这条契约，
        而且这种旁路不会报错、只会偶尔量错 —— 必须被这条断言拦下。
        """
        js_dir = ROOT / "public" / "js"
        offenders = []
        for path in sorted(js_dir.glob("*.js")):
            if path.name == "13-skill-refs.js":
                continue
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), 1):
                if "scrollHeight" in line and "style.height" in line:
                    offenders.append(f"{path.name}:{line_no}")
        self.assertEqual([], offenders,
                         "输入框高度只能由 resizeTextarea 单点写入")

    # ---- ② placeholder / 宽度变化之后必须重算 ----

    def test_context_lock_recomputes_height_last(self):
        """`updateContextComposerLock()` 改完 placeholder 与插话键显隐后，末尾必须重算高度。"""
        body = _fn_body(self.media_js, "export function updateContextComposerLock(")
        self.assertIn("input.placeholder = ", body,
                      "这是 placeholder 的唯一写入点（契约见 §九.128 与 03-media 注释）")
        resize_at = body.rindex("resizeTextarea();")
        send_at = body.index("updateSendButtonState();")
        self.assertLess(send_at, resize_at,
                        "重算必须放在 updateSendButtonState 之后：插话键显隐会改输入区宽度，"
                        "先量就量在旧宽度上")

    def test_context_lock_flags_running_row_before_measuring(self):
        """运行态标记 `.composer.is-running` 必须在重算高度**之前**翻转（§九.135 第 6 项）。

        这个类在手机上把输入区拆成两行（输入区宽度 88px → 326px）。写反了就会把旧宽度下
        量出来的高度留在屏幕上——与 §九.128 那个「输入框占半屏、本轮结束也不回落」的 bug
        完全同形，只是触发源换成了版式切换。
        """
        body = _fn_body(self.media_js, "export function updateContextComposerLock(")
        self.assertIn("classList.toggle('is-running'", body,
                      "运行态标记必须由这个方法单点翻转（busy 变化的入口都会经过它）")
        toggle_at = body.index("classList.toggle('is-running'")
        resize_at = body.rindex("resizeTextarea();")
        self.assertLess(toggle_at, resize_at,
                        "拆两行会改输入区宽度，必须先翻类再量高")

    def test_media_module_imports_resize_from_skill_refs(self):
        """`03-media.js` 要真的 import 到同一个 `resizeTextarea`，别自己抄一份。"""
        m = re.search(
            r'import \{([^}]*)\} from "\./13-skill-refs\.js";', self.media_js)
        self.assertIsNotNone(m, "03-media.js 必须从 13-skill-refs.js 取输入管线")
        self.assertIn("resizeTextarea", m.group(1))

    def test_context_reset_seed_refreshes_composer_pipeline(self):
        """`fillContextResetSeed()` 程序化写值后，高度与镜像层都要自己补。"""
        body = _fn_body(self.messages_js, "export function fillContextResetSeed(")
        self.assertIn("resizeTextarea();", body,
                      "多行种子消息塞进一行高的框里 = 正文看不见")
        self.assertIn("renderInputMirror();", body,
                      "镜像层不刷 ⇒ `/ref` 高亮不显示")
        self.assertIn("notifyComposerChanged(input);", body,
                      "发送按钮状态要跟着变")


class ComposerExpandS1S5GuardTests(unittest.TestCase):
    """S1/S2/S4/S5（2026-10-05）：展开态真实下限 + 可视视口参照 + 手机焦点策略 + 按钮新形态。

    背景：展开键上线后点它「完全没反应」——高度永远由内容决定，展开只抬高上限，
    「你好」这种短草稿 scrollHeight=39px ⇒ 上限抬到再高也用不上（桌面/手机同形）。
    S1 给展开态真实下限（可视视口 40%，绝对下限 200px），S4 在 CSS 加固定值兜底，
    S2 让手机点展开不抢焦点（否则软键盘把刚展开的区域压掉一半），
    S5 把按钮换成 18px 迷你键挪进输入框右上角的 margin 排水沟。
    """

    def setUp(self):
        self.skill_refs_js = _read("public", "js", "13-skill-refs.js")
        self.css = _read("public", "styles.css")
        self.html = _read("public", "index.html")

    def test_expanded_min_height_is_applied(self):
        """展开态量高行必须把内容高与展开下限取大，且下限提成具名常量。"""
        body = _fn_body(self.skill_refs_js, "export function resizeTextarea() {")
        self.assertIn("Math.max(content, expandedMin())", body,
                      "没有这一行 = 展开态又变回纯内容驱动，短草稿点了不变大")
        self.assertRegex(self.skill_refs_js, r"const MIN_COMPOSER_H_EXPANDED_RATIO = [0-9.]+;")
        self.assertRegex(self.skill_refs_js, r"const MIN_COMPOSER_H_EXPANDED_PX = \d+;")

    def test_expanded_cap_uses_visual_viewport(self):
        """上限/下限必须读 `visualViewport`；不得直接乘 `window.innerHeight`。

        ⚠️ 只禁「相乘算上限」，不禁「取小作参照」——`visibleHeight()` 里
        `Math.min(vv.height, window.innerHeight)` 是合法用法（软键盘弹起时
        window.innerHeight / CSS dvh 都不缩，只有 visualViewport.height 缩）。
        """
        self.assertIn("window.visualViewport", self.skill_refs_js)
        self.assertNotRegex(self.skill_refs_js, r"window\.innerHeight\s*\*",
                            "键盘弹起时 window.innerHeight 不缩，不能拿它乘比例算上限")
        self.assertNotRegex(self.skill_refs_js, r"innerHeight \* [0-9]",
                            "比例必须乘 visibleHeight()，不能乘 innerHeight")

    def test_expanded_binds_visual_viewport_listener(self):
        """`setComposerExpanded` 必须挂 visualViewport 的 resize/scroll 监听（键盘开合只走它）。"""
        body = _fn_body(self.skill_refs_js, "export function setComposerExpanded(")
        self.assertIn("visualViewport", body)
        self.assertIn("addEventListener('resize'", body)
        self.assertIn("addEventListener('scroll'", body,
                      "键盘推挤时 visualViewport 的 offsetTop 也变，scroll 也要重算")

    def test_coarse_pointer_expand_does_not_autofocus(self):
        """粗指针（手机/平板）展开时不得抢焦点：focus 必弹软键盘，把刚展开的区域压掉一半。"""
        body = _fn_body(self.skill_refs_js, "export function toggleComposerExpanded() {")
        self.assertIn("focus: !isCoarsePointer()", body,
                      "焦点策略必须按指针类型分流（细指针保留点完能打字的原行为）")

    def test_css_expanded_min_height_fallback(self):
        """CSS 兜底必须是固定 px 且与 JS 常量同值；负向：不得用 vh/dvh 写展开态 min-height。

        CSS min-height 优先于内联 style.height，而软键盘弹起时 vh/dvh 都不缩——
        用比例值会把 JS 的「收到键盘上方」又顶回去。
        """
        m = re.search(r"MIN_COMPOSER_H_EXPANDED_PX = (\d+);", self.skill_refs_js)
        self.assertIsNotNone(m, "展开绝对下限要提成具名常量")
        px = int(m.group(1))
        self.assertRegex(
            self.css,
            r"\.composer-wrap\.is-expanded textarea \{[^}]*min-height: %dpx" % px,
            f"CSS 兜底必须写固定 {px}px 并与 MIN_COMPOSER_H_EXPANDED_PX 同值",
        )
        for block in re.findall(r"\.composer-wrap\.is-expanded[^{]*\{([^}]*)\}", self.css):
            self.assertNotRegex(block, r"min-height:\s*[^;]*v[hd]",
                                "展开态 min-height 不得用 vh/dvh 比例值（键盘弹起时它们不缩）")

    def test_expand_button_lives_inside_composer_input(self):
        """S5：按钮在 `.composer-input` 内绝对定位（右上角），HTML 顺序锚在 textarea 之后。"""
        self.assertRegex(self.css, r"\.composer-input \{[^}]*position:\s*relative")
        self.assertRegex(self.css, r"\.composer-input \.expand-button \{[^}]*position:\s*absolute")
        expand_at = self.html.index('id="expandComposer"')
        self.assertGreater(expand_at, self.html.index('id="messageInput"'),
                           "按钮要排在 textarea 之后的同一容器里")
        self.assertLess(expand_at, self.html.index('class="reasoning-wrap"'),
                        "composer-input 之后的第一个兄弟是 reasoning-wrap；排在它前面 ⇒ 确在输入框内部")

    def test_expand_button_uses_margin_gutter_not_padding(self):
        """让位必须用 margin 排水沟（推得开滚动条/镜像层），padding 只推得开文字。"""
        m = re.search(r"\.composer-input #messageInput[^{]*\{([^}]*)\}", self.css)
        self.assertIsNotNone(m, "#messageInput 必须有排水沟规则")
        block = m.group(1)
        self.assertIn("margin-right", block,
                      "排水沟必须落在 #messageInput 的 margin-right 上")
        self.assertNotRegex(block, r"padding-right:\s*([2-9]\d|\d{3,})px",
                            "负向：不得用大 padding-right 让位（推不开滚动条，预览实测暴露）")

    def test_input_mirror_matches_textarea_gutter(self):
        """镜像层与 textarea 的排水沟必须同值，且 textarea 宽度补偿与沟宽一致。"""
        ta = re.search(r"\.composer-input #messageInput[^{]*\{[^}]*margin-right:\s*(\d+)px", self.css)
        mi = re.search(r"\.composer-input #inputMirror[^{]*\{[^}]*margin-right:\s*(\d+)px", self.css)
        self.assertIsNotNone(ta, "textarea 必须有 margin-right 排水沟")
        self.assertIsNotNone(mi, "镜像层必须吃同样的 margin-right，否则高亮与文本换行点不同、整体错位")
        self.assertEqual(ta.group(1), mi.group(1), "两处 margin-right 必须同值")
        self.assertIn("calc(100%% - %spx)" % ta.group(1), self.css,
                      "textarea 原 width:100% 叠 margin 会溢出父盒，必须用 calc 补回沟宽")

    def test_expand_button_mini_diagonal_icon(self):
        """S5 定稿形态：18×18 迷你键、svg 11px、45° 斜向双箭头（防被「顺手放大」改回去）。"""
        block = re.search(r"\.composer-input \.expand-button \{([^}]*)\}", self.css)
        self.assertIsNotNone(block, "展开键样式块必须存在")
        self.assertRegex(block.group(1), r"width:\s*18px")
        self.assertRegex(block.group(1), r"height:\s*18px")
        self.assertRegex(self.css, r"\.composer-input \.expand-button svg \{[^}]*width:\s*11px")
        self.assertIn("rotate(45 12 12)", self.html,
                      "图标必须是 45° 斜向双箭头（现为双山形 SVG 外套 rotate(45 12 12)）")


if __name__ == "__main__":
    unittest.main()
