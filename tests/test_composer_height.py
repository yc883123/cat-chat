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
        """封顶值必须与 styles.css 的 `.composer textarea { max-height }` 同值。"""
        body = _fn_body(self.skill_refs_js, "export function resizeTextarea() {")
        m = re.search(r"MAX_COMPOSER_H = (\d+);", self.skill_refs_js)
        self.assertIsNotNone(m, "封顶值要提成具名常量，别在量高行里藏魔数")
        cap = int(m.group(1))
        self.assertIn("Math.min(input.scrollHeight, MAX_COMPOSER_H)", body,
                      "量高必须封顶，否则长草稿会把输入框顶穿屏幕")
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


if __name__ == "__main__":
    unittest.main()
