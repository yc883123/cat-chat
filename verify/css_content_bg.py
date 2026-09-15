# -*- coding: utf-8 -*-
"""对话区内容「实底」守门：背景图只铺对话区，内容块必须自己挡住它。

背景（见 `.chat-backdrop` 的注释）：背景图是对话区里的**独立图层**，不做蒙层压暗，
可读性完全靠"内容一律实底"。所以任何内容块漏了底色（或顺手写了 `background:
transparent`），背景图就会透到文字上——浅色图上还能勉强看，深色图直接读不清，
而且是"上线很久才被用户投诉"的那类问题（本次就实测到 `.starter-grid .starter-add`：
它是虚线「自定义指令」卡，只有它写的是 transparent，其它入口卡都是实底）。

判据（对 REQUIRED_OPAQUE 里每个选择器逐条查 CSS 规则）：
1. 至少要有一条规则给它声明实底（`background` / `background-color`，且不是
   transparent / none / 半透明取值）；
2. 任何一条规则把它写成透明都要报错——覆盖掉实底同样是漏洞（`.starter-add` 就是这么漏的）；
3. 选择器匹配用**整串相等**（按逗号拆组、压缩空白）：`:hover`、`[hidden]`、后代组合
   等衍生规则不参与判定（那些是交互态，不是内容块的底色来源）。同理
   `.message-row.user.is-editing .message-body` 的 `background: transparent` 是
   "编辑态去气泡"的有意设计，与 `.message-row.user .message-body` 是两条不同规则，不会误报。

确需透明的放 TRANSPARENT_ALLOWED 白名单（每条都要写理由），脚本会把豁免项与
"允许半透明但必须知情的风险项"一起打印出来，避免悄悄绕过检查。

用法：python verify/css_content_bg.py [styles.css 路径]（默认 public/styles.css）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_CSS = Path(__file__).resolve().parents[1] / "public" / "styles.css"

# 对话区里必须实底的内容块（值 = 说明，出错时一并打印，省得回头找是哪个控件）。
REQUIRED_OPAQUE: dict[str, str] = {
    ".message-card": "AI 回复卡片",
    ".message-row.user .message-body": "用户气泡",
    ".starter-grid button": "开始页入口卡（三张 starter 卡的底色来源）",
    ".starter-grid .starter-add": "开始页「自定义指令」虚线卡（单独覆盖过 background，必须盯住）",
    ".plan-card-inner": "计划卡",
    ".permission-grid > label": "权限 / 工具选择卡",
    ".tool-confirm": "工具确认框",
    ".composer-wrap": "输入区外框",
    ".composer": "输入框主体",
    ".turn-tip": "轮次悬停概要（fixed，浮在对话区上）",
    ".context-usage-popover": "上下文用量弹层（fixed，浮在对话区上）",
    ".conversation-menu": "会话「⋯」菜单（fixed，浮在对话区上）",
}

# 有意透明 / 不参与"遮住背景图"的东西：每条都必须写清楚为什么。
TRANSPARENT_ALLOWED: dict[str, str] = {
    ".messages": "对话区滚动容器：背景图就在它背后，它自己必须透明（且不能直接涂背景图，见 CSS 注释）",
    ".chat-backdrop": "背景图层本身",
    ".empty-state": "开始页容器：按方案只有三张卡片要实底，标题与图标不加底",
    ".message-body": "基础样式：AI 侧由 .message-card 供底，用户侧由 .message-row.user 覆盖",
    ".message-card .message-body": "AI 卡内部正文：底色由外层 .message-card 提供（这里再刷一层会盖住卡片底色）",
    ".turn-rail": "轮次刻度轨（容器，无视觉）",
    ".turn-tick": "刻度轨命中块：视觉上只是一根细线",
    ".turn-tick-line": "刻度线本体",
    ".message-avatar": "头像圆点：自带渐变实底，不需要再刷一层",
}

# 允许半透明、但每次运行都要报一声的风险项（不能默默脱敏）。
WARN_SEMI_TRANSPARENT: dict[str, str] = {
    ".topbar": "顶栏仍是 94% 半透明：本次只铺对话区、不受影响；将来若改成全屏铺图，这里必须改实底",
}

RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
BG_RE = re.compile(r"background(?:-color)?\s*:\s*([^;]+)", re.I)
_TRANSPARENT_WORDS = ("transparent", "none", "initial", "unset", "revert")


def normalized_selectors(selector_text: str) -> list[str]:
    """选择器组 → 规范化后的单个选择器列表（逗号拆组 + 空白压缩）。"""
    return [re.sub(r"\s+", " ", part).strip() for part in selector_text.split(",") if part.strip()]


def background_declared(declarations: str) -> tuple[bool, list[str]]:
    """返回 (是否有实底, 命中到的透明/半透明取值列表)。"""
    values = [match.group(1).strip() for match in BG_RE.finditer(declarations)]
    if not values:
        return False, []
    transparent = [value for value in values if _is_transparent(value)]
    return len(transparent) < len(values), transparent


def _is_transparent(value: str) -> bool:
    lowered = value.lower()
    if any(lowered == word or lowered.startswith(f"{word} ") for word in _TRANSPARENT_WORDS):
        return True
    # rgba(0,0,0,0) / color-mix(..., transparent) 这类"写了颜色但没遮住"的取值：
    # 只要出现 transparent 关键字，一律按透明处理（半透明也算没挡住背景图）。
    return "transparent" in lowered or bool(re.search(r"rgba?\([^)]*,\s*0(?:\.0+)?\s*\)", lowered))


def collect_rules(css: str) -> list[tuple[str, str]]:
    """CSS 文本 → [(选择器, 声明块)]；@media 里的嵌套规则同样会被取出。

    注释必须先剥掉：本项目大量使用"规则上方一行中文注释"的写法，而注释里没有大括号，
    会被正则并进选择器文本里（`.message-card` 曾因此判成"找不到规则"）。
    """
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    rules: list[tuple[str, str]] = []
    for match in RULE_RE.finditer(css):
        selector_text = match.group(1).strip()
        if not selector_text or selector_text.startswith("@") or selector_text.endswith("{"):
            continue
        for selector in normalized_selectors(selector_text):
            rules.append((selector, match.group(2)))
    return rules


def main() -> int:
    css_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSS
    css = css_path.read_text(encoding="utf-8")
    rules = collect_rules(css)
    problems: list[str] = []

    for selector, reason in REQUIRED_OPAQUE.items():
        matched = [decls for sel, decls in rules if sel == selector]
        if not matched:
            problems.append(f"{selector}（{reason}）：styles.css 里找不到这条规则，守门清单已过期")
            continue
        opaque = False
        transparent_hit = False
        for declarations in matched:
            has_background, transparent_values = background_declared(declarations)
            if transparent_values:
                transparent_hit = True
                problems.append(
                    f"{selector}（{reason}）：底色被写成 {' / '.join(transparent_values)}，"
                    "背景图会透到内容上"
                )
            opaque = opaque or has_background
        # 已经报了"被写成透明"就不再补一句"缺底色"（同一个根因报两遍只会让人分心）。
        if not opaque and not transparent_hit:
            problems.append(f"{selector}（{reason}）：缺 background 实底声明")

    for selector in TRANSPARENT_ALLOWED:
        if selector in REQUIRED_OPAQUE:
            problems.append(f"{selector}：同时出现在必须实底清单与豁免清单里，两处冲突必须去掉一处")

    print(f"对话区实底守门：{css_path}")
    print(f"  必须实底 {len(REQUIRED_OPAQUE)} 项 · 豁免 {len(TRANSPARENT_ALLOWED)} 项")
    for selector, reason in TRANSPARENT_ALLOWED.items():
        print(f"  豁免：{selector} —— {reason}")

    if WARN_SEMI_TRANSPARENT:
        print("  风险登记（不判失败）：")
        for selector, reason in WARN_SEMI_TRANSPARENT.items():
            declarations = " ".join(decls for sel, decls in rules if sel == selector)
            semi = any(_is_transparent(value) for value in BG_RE.findall(declarations))
            state = "仍为半透明" if semi else "已是实底（可更新风险登记）"
            print(f"    {selector} —— {state}；{reason}")

    if problems:
        print("\n发现问题：")
        for problem in problems:
            print("  " + problem)
        return 1
    print("\n对话区内容实底校验通过（无缺底、无透明覆盖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
