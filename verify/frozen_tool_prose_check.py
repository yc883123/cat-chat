# -*- coding: utf-8 -*-
"""冻结版自检：确认「工具调用前的进度播报不再堆积」真的进了 exe。

用法：`dist\\naiba-chat.exe --run-skill-script <本文件绝对路径>`

存在理由：这条修复横跨**两层形态**，源码模式看不出打包缺失——
① 展示层是 Python 代码（打包进 PYZ，盘上没有可读的 `.py`），只能用**运行期真调用**验证；
② 前端 `activityMarkup` 的兜底是打包的静态资源（`public/js/03-media.js`），
   而**用户跑的正是 exe**（用户报障时明确说"当前生成的 EXE 仍会出现这类废话"）——
   不重编译换 exe，改一行也到不了用户手上。
逻辑守门在 `tests/test_activity_timeline.py`，真后端冒烟在 `verify/tool_prefix_prose_smoke.py`，
前端渲染真执行断言在 `verify/media_markup_check.mjs`。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 冻结版没打 unittest（`import unittest.mock` 会 ModuleNotFoundError），这里手写最小替身。

ok = True

PROGRESS = ["我已核对接口和调用链。", "根因已经锁定。", "现在补协议。"]
FINAL = "修好了，共改 2 个文件。"


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition or not detail else f'  -> {detail}'}")
    if not condition:
        ok = False


def _event(kind: str, **extra):
    return {"type": kind, **extra}


# ---- ① 运行期：活动时间线只留最后一段正文 --------------------------------------
try:
    from naiba.run.stream import _build_activity_timeline

    events = []
    for index, text in enumerate(PROGRESS):
        events.append(_event("delta", content=text, created_at=100 + index * 100))
        events.append(_event("tool_result", created_at=150 + index * 100))
    events.append(_event("delta", content=FINAL, created_at=999))
    runs = [{"tool": f"tool_{i}", "success": True, "result": "ok"} for i in range(len(PROGRESS))]
    activity = _build_activity_timeline(events, [], runs)
    prose = [item for item in activity if item.get("type") == "prose"]
    tools = [item for item in activity if item.get("type") == "tool"]
    check("运行期：三句过程播报只留一段正文", len(prose) == 1, f"prose={len(prose)}")
    check("运行期：留下的就是最终答复", bool(prose) and prose[0].get("text") == FINAL,
          str(prose[0].get("text") if prose else None))
    check("运行期：工具条目一个不少", len(tools) == len(PROGRESS), f"tools={len(tools)}")
    check("运行期：最终答复排在时间线末尾", bool(activity) and activity[-1].get("type") == "prose")
except Exception as exc:  # noqa: BLE001
    check("运行期：可导入活动时间线构建器", False, repr(exc))

# ---- ② 运行期：取消/失败重建的正文同口径 ---------------------------------------
try:
    from naiba.run.chat import ConversationRunMixin

    rebuilt = [
        _event("delta", content="我已定位根因，", created_at=100),
        _event("tool_result", tool="edit_file", success=True, result="ok", created_at=200),
        _event("delta", content=FINAL, created_at=300),
    ]
    _reasoning, _runs, content, _activity = ConversationRunMixin._rebuild_partial_run("r", rebuilt)
    check("运行期：重建正文只取工具调用之后那段", content == FINAL, repr(content))
    stopped = [
        _event("delta", content="正在验证", created_at=100),
        _event("tool_result", tool="pwsh", success=True, result="out", created_at=200),
    ]
    _r2, _t2, content2, _a2 = ConversationRunMixin._rebuild_partial_run("r2", stopped)
    check("运行期：停在工具步时不把播报当答复", content2 == "", repr(content2))
except Exception as exc:  # noqa: BLE001
    check("运行期：可导入部分运行重建", False, repr(exc))

# ---- ③ 打包资源：前端渲染层兜底已进 exe ---------------------------------------
try:
    from naiba.paths import default_path_context

    media = (Path(default_path_context().public_dir) / "js" / "03-media.js").read_text(
        encoding="utf-8"
    )
    check("打包资源：activityMarkup 只渲染最后一段正文", "lastProseIndex" in media)
    check("打包资源：过滤真的生效（有 filter）", ".filter((item, index) =>" in media)
    check(
        "打包资源：旧的无脑遍历写法已消失",
        "activity.forEach((item, index) => {" not in media,
    )
except Exception as exc:  # noqa: BLE001
    check("打包资源：可读到打包内的 03-media.js", False, repr(exc))

print("\n全部通过" if ok else "\n存在未通过项")
sys.exit(0 if ok else 1)
