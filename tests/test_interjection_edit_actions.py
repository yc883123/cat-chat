# -*- coding: utf-8 -*-
"""护栏：插话队列「行内编辑」必须同时给出保存与**取消**，且取消只还原界面、不发请求。

事故形态（2026-09-23，用户看到真机截图后一句「那肯定要补点完铅笔后的取消啊」）：

`startEditInterjection()` 展开编辑框后，用 `actions.replaceChildren(save)` 把整条动作条
**换成只剩一个「保存」**——编辑 / 删除 / 引导 / 取回 四个按钮同时消失，而「取消」只绑在
`Escape` 上。桌面有 Esc 所以一切正常；**手机没有 Esc**，用户想放弃修改只能去点「保存」
（等于把原文再存一遍，是一次真写库），或者干脆整页刷新。这条路径从 2026-09-19 引入行内
编辑起就存在，只是手机端插话这条线直到本周才被密集使用，所以长期没人撞上。

守住的四条契约（缺一条这条 bug 就会以另一种形态回来）：

1. 编辑态动作条 = **保存 + 取消**（不是「只剩保存」——那是本 bug 的字面签名）；
2. 取消与 Esc 走**同一条路径** `commitInterjectionEdit(id, null)`：只还原界面、**零请求**
   （若哪天有人把它改成调 `api(...)`，那就不是「放弃」而是「提交原文」了）；
3. 取消是**编辑态专属**：常规态的四个图标按钮里不许冒出第五个（防范围蔓延）；
4. 按钮必须有自己的样式（`width: auto`），否则会继承 28/34px 的方形按钮尺寸、两个字被裁掉。

真机形态另有 `verify/_probe_interject_edit.cjs`（真点击 / 真 setBusy / 零请求断言，见 §九.135）。
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


def _media_block(src: str, needle: str) -> str:
    """取包含 `needle` 的那段 `@media (...) { ... }` 正文（按花括号配平，不靠缩进猜）。"""
    at = src.index(needle)
    media = src.rindex("@media", 0, at)
    open_at = src.index("{", media)
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at:i]
    raise AssertionError(f"{needle} 所在的 @media 块没有闭合")


class InterjectionEditActionTests(unittest.TestCase):
    """前端源码守门：编辑态必须有可点的「取消」，且它与 Esc 同一条路径。"""

    def setUp(self) -> None:
        self.js = _read("public", "js", "18-interjections.js")
        self.css = _read("public", "styles.css")
        self.edit_fn = _fn_body(self.js, "function startEditInterjection(")
        click_at = self.js.index("document.addEventListener('click', (event) => {")
        self.delegate = self.js[click_at:self.js.index("\n});", click_at)]
        self.row_fn = _fn_body(self.js, "export function runGuidanceElement(")

    # ---- ① 编辑态同时有保存与取消 ----

    def test_edit_mode_builds_both_save_and_cancel(self) -> None:
        self.assertIn("interjectionSave", self.edit_fn, "编辑态必须仍有「保存」")
        self.assertIn("interjectionCancel", self.edit_fn, "编辑态必须有「取消」")
        self.assertIn("'取消'", self.edit_fn, "取消按钮要带文字——手机上没有可发现的图标惯例")

    def test_edit_actions_are_not_replaced_by_save_alone(self) -> None:
        """负向断言：本 bug 的字面签名就是 `replaceChildren(save)` 单参形态。"""
        self.assertIsNone(
            re.search(r"replaceChildren\(\s*save\s*\)", self.edit_fn),
            "编辑态动作条只剩「保存」= 手机用户没有放弃入口（本次修复的原始缺陷形态）",
        )
        self.assertIn("replaceChildren(save, cancel)", self.edit_fn)

    # ---- ② 取消与 Esc 同路径，且零请求 ----

    def test_cancel_button_reaches_same_path_as_escape(self) -> None:
        at = self.delegate.index("'[data-interjection-cancel]'")
        branch = self.delegate[at:self.delegate.index("return;", at)]
        self.assertIn("commitInterjectionEdit(", branch)
        self.assertIn("null", branch, "null 才是「取消」——非 null 表示带着内容提交")
        self.assertIn("preventDefault", branch)

    def test_cancel_branch_sends_no_request(self) -> None:
        at = self.delegate.index("'[data-interjection-cancel]'")
        branch = self.delegate[at:self.delegate.index("return;", at)]
        self.assertNotIn("api(", branch, "取消必须纯前端还原；改成发请求就不是「放弃修改」了")

    def test_escape_is_still_wired_to_the_same_cancel_path(self) -> None:
        self.assertIn("commitInterjectionEdit(messageId, null)", self.edit_fn)

    # ---- ③ 取消是编辑态专属 ----

    def test_cancel_is_not_offered_on_a_regular_row(self) -> None:
        self.assertNotIn("interjectionCancel", self.row_fn)
        self.assertNotIn("data-interjection-cancel", self.row_fn)

    # ---- ④ 样式与状态标记 ----

    def test_cancel_button_has_its_own_style_with_auto_width(self) -> None:
        rule_at = self.css.index("[data-interjection-cancel]")
        rule = self.css[rule_at:self.css.index("}", rule_at)]
        self.assertIn("width: auto", rule, "不覆盖 width 就会被 28/34px 的方形按钮尺寸裁掉文字")

    def test_editing_class_is_added_and_removed(self) -> None:
        """`is-editing` 必须成对出现：引导是就地改行（不重建节点），漏摘就是残留态。"""
        self.assertIn("classList.add('is-editing')", self.edit_fn)
        self.assertIn("classList.remove('is-editing')", self.js)

    def test_editing_badge_is_hidden_only_on_mobile(self) -> None:
        """编辑态收起「待引导」徽标是**手机专属**的宽度取舍，桌面不许跟着变。"""
        needle = ".run-guidance-card.is-editing .run-guidance-badge"
        media_at = self.css.rindex("@media", 0, self.css.index(needle))
        header = self.css[media_at:self.css.index("{", media_at)]
        self.assertIn("max-width: 760px", header, "收起徽标必须关在手机断点里")
        self.assertIn("display: none", _media_block(self.css, needle))


if __name__ == "__main__":
    unittest.main()
