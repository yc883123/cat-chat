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

# ---- ①b 桌面端「右键粘贴图片/文件」：桥与前端接线都要在 exe 里 --------------------
# 这条功能横跨打包的 Python（launcher.JsApi）与打包的前端资源，两侧都要查：
# 源码模式读 `public/` 永远是新的，读不到「用户那个 exe 里到底有没有这个桥」。
try:
    chat = (public / "js" / "12-chat-input.js").read_text(encoding="utf-8")
except Exception as exc:  # noqa: BLE001
    chat = ""
    check("可读到打包内的 12-chat-input.js", False, repr(exc))
check("打包资源：输入模块调桥读剪贴板", "naibaClipboardPayload" in chat)
check("打包资源：图片走既有上传链路", "uploadFiles([pngFileFromBase64(" in chat)
check("打包资源：文件走路径附件", "state.pendingFiles.push" in chat and "renderPendingFiles()" in chat)
check("打包资源：菜单文案自适应有实现", "粘贴图片并上传" in chat and "labelFor" in chat)
check("打包资源：底层模块有注入点", "export function setClipboardPasteDriver" in core)
check("打包资源：组合根真的注册了驱动", "setClipboardPasteDriver(clipboardPasteDriver)" in bind)
check("打包资源：空串不得替换选区（旧写法已消失）",
      "text !== ''" in core and "if (text != null) ok = insertTextIntoEditable(text)" not in core)

# 运行期真值：桥函数真的在打包的解释器里（读源码文本在冻结版不可用，只能断言可达对象）。
_payload_fn = None
for _name in ("clipboard_payload",):
    _candidate = globals().get(_name)
    if callable(_candidate):
        _payload_fn = _candidate
if _payload_fn is None:
    try:  # 沿调用栈找持有 launcher 全局的那层 frame（不能 import launcher：它是打包入口脚本）
        import inspect

        for _frame in inspect.stack():
            _fn = _frame.frame.f_globals.get("clipboard_payload")
            if callable(_fn):
                _payload_fn = _fn
                break
    except Exception as exc:  # noqa: BLE001
        check("可沿调用栈定位 launcher 的全局命名空间", False, repr(exc))
check("运行期：剪贴板载荷函数可达", callable(_payload_fn))
if callable(_payload_fn):
    try:
        _shape = _payload_fn(read=lambda: {"hdrop": [], "dib": None, "png": None, "text": ""})
        check("运行期：空剪贴板 → kind=empty", _shape == {"ok": True, "kind": "empty"}, str(_shape))
    except Exception as exc:  # noqa: BLE001
        check("运行期：剪贴板载荷可调用", False, repr(exc))

# ---- ② 顶栏收起：整条只剩折叠条 + 「展开顶栏」文字入口 --------------------------
check(
    "打包资源：收起隐藏全部子元素",
    ".topbar.compact > *:not(.topbar-collapse-toggle) { display: none; }" in css,
)
check("打包资源：旧「并回一行」规则已删", ".topbar.compact .model-control" not in css)
check("打包资源：展开文字入口在 HTML 里", 'class="topbar-toggle-label"' in index)

print("\n全部通过" if ok else "\n存在未通过项")
sys.exit(0 if ok else 1)
