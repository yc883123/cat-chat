# -*- coding: utf-8 -*-
"""冻结版自检：端口冲突弹窗（§九.126）在**打包产物里**真的可用。

为什么必须查打包产物：`_ask_alternate_port` 依赖 tkinter（`simpledialog.askinteger`），
而 tkinter 需要 `_MEIPASS` 下的 tcl/tk 数据目录与 DLL。**漏收这些东西时源码模式一切正常、
单测全绿，只有 exe 会在用户真遇到端口冲突那一刻才发现弹不出来**（与 §六 里
`frozen_appicon_check.py` / `frozen_mobile_ui_check.py` 同因）。
本脚本**不开任何窗口**（沙箱会拦 GUI，结论也不能建在窗口探针上）：只验证
「解释器能建成」+「资源在」+「launcher 里的那套函数拿到了」。

跑法（打包后必跑）：
    dist\\naiba-chat.exe --run-skill-script <本文件绝对路径>

源码模式也能跑（`python launcher.py --run-skill-script <本文件绝对路径>`），
但第 2 段「`_MEIPASS` 下的 tcl/tk 资源」会**显式 skip**——源码模式没有 `_MEIPASS`，
这段判不出结论，别拿它当"打包产物没问题"的证据。

退出码 0 = 全通过；失败会逐条打印 `[FAIL]`。

两条只在这种探针里才会遇到的坑（与 `frozen_appicon_check.py` 同款）：
1. 不能 `import launcher`——`launcher.py` 是 PyInstaller 的**入口脚本**，不注册成可 import 模块；
2. 也不能取 `sys.modules["__main__"]`——`_run_skill_script` 用 `runpy.run_path(script, run_name="__main__")`
   跑本文件、会临时顶掉它。唯一可靠的拿法：沿调用栈找**持有 `_ask_alternate_port` 的那层 frame 的 `f_globals`**。
"""
from __future__ import annotations

import sys
from pathlib import Path

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else '[FAIL]'} {label}" + ("" if ok or not detail else f"  -> {detail}"))
    if not ok:
        FAILURES.append(label)


def skip(label: str, why: str = "") -> None:
    """非冻结版下没有 `_MEIPASS`，这些项无从判定——**明确标 skip，不冒充通过**。"""
    print(f"skip {label}" + (f"  -> {why}" if why else ""))


def main() -> int:
    print(f"冻结版自检：端口冲突弹窗（frozen={bool(getattr(sys, 'frozen', False))}）")

    # ---- 1. tkinter 真的进来了（含子模块） ----
    tk = None
    try:
        import tkinter as tk  # noqa: PLC0415 - 探针就要在这里导入
        from tkinter import simpledialog  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - 导入失败就是结论
        check("import tkinter / simpledialog", False, repr(exc))
        simpledialog = None
    else:
        check("import tkinter / simpledialog", True)
        check("simpledialog.askinteger 可调用", callable(getattr(simpledialog, "askinteger", None)),
              repr(getattr(simpledialog, "askinteger", None)))

    # ---- 2. tcl/tk 数据目录与 DLL 在 _MEIPASS 里 ----
    # 只有冻结版才有 _MEIPASS；源码模式下 tkinter 用系统 Python 的 tcl 目录，这三项无从判定。
    # **不要在这里"顺手"改成查 Lib/tkinter** —— 那是另一个位置，判出来的结论对 exe 毫无意义。
    frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        skip("_MEIPASS 下的 tcl/tk 数据目录与 DLL（源码模式无 _MEIPASS）",
             "改由第 3 段 tk.Tcl() 证明资源可用；要判打包产物请跑 dist\\naiba-chat.exe")
    else:
        meipass = Path(sys._MEIPASS)
        for name in ("_tcl_data", "_tk_data"):
            path = meipass / name
            check(f"_MEIPASS/{name} 存在（tkinter 的数据目录）", path.is_dir(), str(path))
        dlls = sorted(p.name for p in meipass.glob("t*8*t.dll"))
        check("_MEIPASS 下有 tcl/tk 的 DLL", len(dlls) >= 2, str(dlls))

    # ---- 3. 解释器真能建成（不建窗口、不画界面） ----
    # 这是「资源齐不齐」的真判据：Tcl() 会去加载 init.tcl，缺数据目录必抛 TclError。
    if tk is not None:
        try:
            interp = tk.Tcl()
            version = str(interp.eval("info patchlevel"))
        except Exception as exc:  # noqa: BLE001 - TclError 就是结论
            check("tkinter 解释器可创建（资源可用）", False, repr(exc))
        else:
            check("tkinter 解释器可创建（资源可用）", True, f"patchlevel={version}")

    # ---- 4. launcher 里的那套函数在运行期真的可达 ----
    g = None
    frame = sys._getframe()
    while frame is not None:
        if "_ask_alternate_port" in frame.f_globals:
            g = frame.f_globals
            break
        frame = frame.f_back
    check("能沿调用栈定位 launcher 的全局命名空间", g is not None, "frame-walk 未命中")
    if g is None:
        return 1

    ask = g.get("_ask_alternate_port")
    check("launcher._ask_alternate_port 存在且可调用", callable(ask), repr(ask))

    suggest = g.get("suggest_free_port")
    check("launcher 已导入 suggest_free_port", callable(suggest), repr(suggest))
    if callable(suggest):
        picked = suggest(8765, 3)
        check("suggest_free_port 返回合法端口", isinstance(picked, int) and 8766 <= picked <= 8768, repr(picked))

    message = g.get("port_conflict_message")
    text = message(8765, host="0.0.0.0", detail="[WinError 10048]") if callable(message) else ""
    check("取消时仍会给出中文指引（含自查命令与改 port 的出路）",
          "netstat -ano | findstr :8765" in text and '"port"' in text, text.splitlines()[0] if text else "(缺)")

    # ---- 5. tkinter 不可用时必须优雅回落到老路径（绝不把启动弄得更糟） ----
    if callable(ask):
        saved = sys.modules.get("tkinter", "absent")
        try:
            sys.modules["tkinter"] = None  # 强制 `import tkinter` 抛 ImportError
            fallback = ask(8765, "busy")
        except Exception as exc:  # noqa: BLE001 - 抛异常本身就是失败
            check("tkinter 缺失时返回 None 而不抛异常", False, repr(exc))
        else:
            check("tkinter 缺失时返回 None 而不抛异常", fallback is None, repr(fallback))
        finally:
            if saved == "absent":
                sys.modules.pop("tkinter", None)
            else:
                sys.modules["tkinter"] = saved

    print()
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败 -> {FAILURES}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
