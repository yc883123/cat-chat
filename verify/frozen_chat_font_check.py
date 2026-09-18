# -*- coding: utf-8 -*-
"""冻结版自检：确认「会话区字体」这条链路真的进了 exe（前后端两侧都查）。

用法：`dist\\naiba-chat.exe --run-skill-script <本文件绝对路径>`

存在理由：这个功能**一半是打包进去的静态资源**（`styles.css` / `index.html` / `public/js/*`），
源码模式读 `public/` 永远是新的——只有冻结版能回答「用户手上那个 exe 里到底是哪一版前端」；
另一半是 `naiba/config.py` 的设置契约，冻结版里源码在 PYZ 内，只能靠**运行期真调用**来证明。

逻辑层断言在 `tests/test_chat_font.py`（源码级护栏）与 `tests/test_appearance.py`（配置契约），
真浏览器形态断言在 `verify/chat_font_smoke.py` + `.cjs`。
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
    settings = (public / "js" / "09-settings.js").read_text(encoding="utf-8")
    bind = (public / "js" / "15-bind-events.js").read_text(encoding="utf-8")
    css = (public / "styles.css").read_text(encoding="utf-8")
    index = (public / "index.html").read_text(encoding="utf-8")
except Exception as exc:  # noqa: BLE001
    check("可读到打包内的前端资源", False, repr(exc))
    print("\n存在未通过项")
    sys.exit(1)

# ---- ① 设置契约（运行期真调用，冻结版源码在 PYZ 里读不到 .py） -------------------
try:
    from naiba.config import (  # noqa: E402
        APPEARANCE_CHAT_FONT_FAMILIES,
        APPEARANCE_CHAT_FONT_PRESETS,
        CHAT_FONT_SIZE_DEFAULT,
        default_config,
        normalize_appearance,
    )

    appearance = default_config()["appearance"]
    check(
        "运行期：默认外观含字体三键且与历史值等价",
        appearance.get("chat_font_size") == CHAT_FONT_SIZE_DEFAULT == 15
        and appearance.get("chat_font_family") == "system"
        and appearance.get("chat_font_family_custom") == "",
        repr(appearance),
    )
    check(
        "运行期：归一化会夹回越界字号并把非法字体族收回 system",
        normalize_appearance({"chat_font_size": 99, "chat_font_family": "comic"})
        == {"theme": "system", "skin": "violet", "chat_font_size": 18,
            "chat_font_family": "system", "chat_font_family_custom": ""},
        repr(normalize_appearance({"chat_font_size": 99, "chat_font_family": "comic"})),
    )
    check(
        "运行期：预设字体键全部在 exe 里可写入可回读（点选列表的键必须被后端认）",
        len(APPEARANCE_CHAT_FONT_PRESETS) == 11
        and all(
            normalize_appearance({"chat_font_family": key})["chat_font_family"] == key
            for key in APPEARANCE_CHAT_FONT_PRESETS
        ),
        repr(sorted(APPEARANCE_CHAT_FONT_PRESETS)),
    )
    check(
        "运行期：白名单 = 结构族 + 预设 + custom（少一个就点不动）",
        set(APPEARANCE_CHAT_FONT_FAMILIES)
        == {*APPEARANCE_CHAT_FONT_PRESETS, "system", "serif", "rounded", "custom"},
        repr(sorted(APPEARANCE_CHAT_FONT_FAMILIES)),
    )
except Exception as exc:  # noqa: BLE001
    check("运行期：可导入外观设置的常量与归一化函数", False, repr(exc))

# ---- ② 样式层：变量默认值 + 只有 .message-body 消费它 --------------------------
flat_css = css.replace(" ", "")
check("打包资源：默认值仍是 15px / 界面字体（老用户视觉零变化）",
      "--msg-font-size:15px" in flat_css and "--msg-font-family:var(--font-sans)" in flat_css)
try:
    start = css.index(".message-body {")
    body_rule = css[start:css.index("}", start)]
except ValueError:
    body_rule = ""
check("打包资源：.message-body 消费字体变量",
      "font-size: var(--msg-font-size)" in body_rule and "font-family: var(--msg-font-family)" in body_rule,
      body_rule[:120])
check("打包资源：正文不再写死 15px",
      "font-size: 15px" not in body_rule)
check("打包资源：字体设置不改写全局 --font-sans / body 字号",
      "root.style.fontSize" not in core and "setProperty('--font-sans'" not in core)

# ---- ③ 应用链路与面板接线 ------------------------------------------------------
check("打包资源：CSS 变量由 applyAppearance 写入", "root.style.setProperty('--msg-font-size'" in core
      and "root.style.setProperty('--msg-font-family'" in core)
check("打包资源：字体族映射与 custom 兜底齐全",
      "MSG_FONT_FAMILIES" in core and "Source Han Serif SC" in core and "}, var(--font-sans)`" in core)
check("打包资源：只传 theme/skin 的调用点不重置字体", "numericOrNull(previous.chat_font_size)" in core
      and "MSG_FONT_FAMILY_KEYS.has(previous.chat_font_family)" in core)

# 点选列表：清单在 JS 常量里（HTML 不写死），安装探测靠 canvas 度量而非 queryLocalFonts。
check("打包资源：点选清单是 JS 常量且 11 个预设 + custom 兜底",
      "export const CHAT_FONT_PICKS" in core
      and core.count("{ key: '") >= 11
      and "{ key: 'custom', label: '手动填写…', probe: '' }" in core,
      str(core.count("{ key: '")))
check("打包资源：安装探测是 canvas 度量 + 结果缓存（手机非安全上下文可用）",
      "export function isFontInstalled" in core and "measureText" in core
      and "fontInstalledCache.set(name, installed)" in core)
check("打包资源：面板控件与回显齐全",
      'id="chatFontSize"' in index and 'name="appearanceChatFont"' in index
      and 'id="chatFontPick"' in index and 'id="chatFontManualRow"' in index
      and "buildChatFontPickOptions" in settings and "appearanceFormValues" in bind)
check("打包资源：radio 用 __pick__ 占位（不是 custom），占位值永不落库",
      'value="__pick__"' in index
      and "CHAT_FONT_PICK_VALUE = '__pick__'" in settings
      and "family.value === CHAT_FONT_PICK_VALUE" in settings)
check("打包资源：两级显隐由 CHAT_FONT_BASE_FAMILIES + custom 决定",
      "CHAT_FONT_BASE_FAMILIES = new Set(['system', 'serif', 'rounded'])" in settings
      and "#chatFontManualRow" in settings)
check("打包资源：滑块与下拉都只做本地预览（不在改动路径落库）",
      "$('#chatFontSize')?.addEventListener('input'" in bind
      and "$('#chatFontPick')?.addEventListener('change'" in bind)

print("\n全部通过" if ok else "\n存在未通过项")
sys.exit(0 if ok else 1)
