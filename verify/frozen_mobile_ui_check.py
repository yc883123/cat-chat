# -*- coding: utf-8 -*-
"""冻结版自检：确认「手机端粘贴通路 + 顶栏收起形态」两处修复真的进了 exe。

用法：`dist\\naiba-chat.exe --run-skill-script <本文件绝对路径>`

存在理由：这两处修复**全是打包进去的静态资源**（`index.html` / `styles.css` / `public/js/*`），
源码模式读 `public/` 永远是新的——只有冻结版能回答「用户手机上跑的那个 exe 里到底是哪一版前端」。
本轮实测的教训：用户报的是 `D:\\naiba-chatexe\\naiba-chat.exe` 的旧前端，源码早就改好了，
**不重编译换 exe，改动一行也到不了手机**（§六 同类：源码模式看不出打包缺失）。
逻辑层断言在 `tests/test_context_menu.py` 与 `tests/test_mobile_topbar.py`，
真浏览器形态断言在 `verify/mobile_ui_regression.cjs`。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 冻结版没打 unittest（`import unittest.mock` 会 ModuleNotFoundError），这里手写最小替身。

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition or not detail else f'  -> {detail}'}")
    if not condition:
        ok = False


try:
    from naiba.paths import default_path_context  # noqa: E402

    public = Path(default_path_context().public_dir)
    core = (public / "js" / "01-core.js").read_text(encoding="utf-8")
    bind = (public / "js" / "15-bind-events.js").read_text(encoding="utf-8")
    css = (public / "styles.css").read_text(encoding="utf-8")
    index = (public / "index.html").read_text(encoding="utf-8")
except Exception as exc:  # noqa: BLE001
    check("可读到打包内的前端资源", False, repr(exc))
    print("\n存在未通过项")
    sys.exit(1)

# ---- ① 手机粘贴：触摸长按让位给系统菜单 + 不可用时给可行动作 --------------------
check("打包资源：记录指针类型（长按判定用）", "lastPointerType = event.pointerType" in bind)
check("打包资源：触摸长按不拦截 contextmenu", "if (isLongPressPointer()) return;" in bind)
check("打包资源：粘贴不可用给可行动作", "PASTE_UNAVAILABLE_HINT" in core and "Ctrl+V" in core)
check("打包资源：旧死文案已消失", "粘贴失败：浏览器未授权" not in core)

# ---- ② 顶栏收起：整条只剩折叠条 + 「展开顶栏」文字入口 --------------------------
check(
    "打包资源：收起隐藏全部子元素",
    ".topbar.compact > *:not(.topbar-collapse-toggle) { display: none; }" in css,
)
check("打包资源：旧「并回一行」规则已删", ".topbar.compact .model-control" not in css)
check("打包资源：展开文字入口在 HTML 里", 'class="topbar-toggle-label"' in index)

print("\n全部通过" if ok else "\n存在未通过项")
sys.exit(0 if ok else 1)
