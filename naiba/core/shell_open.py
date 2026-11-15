# -*- coding: utf-8 -*-
"""在系统文件管理器里打开目录，并尽量让它立即可见。

Windows 上只调用 ``os.startfile`` 是不够的：已经有 explorer.exe 在跑时，
ShellExecute 只是让那个后台进程新建一个窗口，前台锁（foreground lock）不给
新窗口前台权——窗口确实开了，却藏在浏览器/应用后面，任务栏只闪一下，用户得
再点一次才看得见（2026-11-15 实测：没有 Explorer 窗口时能到前台，有窗口时
前景句柄不变化）。

这里的做法：打开前记下现有 Explorer 顶层窗口，打开后等这次新出现的那个
（或在已有窗口里换标签页导致标题变化的那个），用 AttachThreadInput +
SetForegroundWindow 显式激活。整个等待在后台线程里做，不拖慢 HTTP 响应。
非 Windows 保持系统默认行为。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# 资源管理器的两个顶层窗口类名。
_EXPLORER_CLASSES = frozenset({"CabinetWClass", "ExploreWClass"})
_SW_RESTORE = 9
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x0002
_ACTIVATE_TIMEOUT = 2.5
_ACTIVATE_INTERVAL = 0.08


def open_folder(target: Path | str) -> None:
    """用系统文件管理器打开目录；Windows 下打开后把窗口带到前台。"""
    path = Path(target)
    if sys.platform.startswith("win"):
        # 先记下现有窗口，才能从「打开后」的集合里认出这一次新开的那个。
        before = _explorer_windows()
        os.startfile(str(path))  # type: ignore[attr-defined]  # 仅 Windows 有
        threading.Thread(
            target=_activate_opened_explorer,
            args=(before, path.name),
            daemon=True,
            name="shell-open-activate",
        ).start()
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path)])


# ---- Windows 窗口识别与激活（只在 win32 分支调用）----

def _user32():
    import ctypes

    return ctypes.windll.user32


def _explorer_windows() -> dict[int, str]:
    """可见的 Explorer 顶层窗口：句柄 -> 标题。"""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    found: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _lparam):  # noqa: ANN001 - ctypes 回调签名
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        if class_name.value not in _EXPLORER_CLASSES:
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, len(title))
        found[int(hwnd)] = title.value
        return True

    user32.EnumWindows(collect, 0)
    return found


def _wait_for_opened_window(before: dict[int, str], name: str,
                            timeout: float = _ACTIVATE_TIMEOUT) -> int:
    """等这一次打开产生的 Explorer 窗口，返回句柄；认不出就返回 0。"""
    deadline = time.monotonic() + timeout
    current: dict[int, str] = {}
    while True:
        current = _explorer_windows()
        fresh = [hwnd for hwnd in current if hwnd not in before]
        if fresh:
            # EnumWindows 按 Z 序从上到下枚举，新窗口在前；取第一个。
            return fresh[0]
        if time.monotonic() >= deadline:
            break
        time.sleep(_ACTIVATE_INTERVAL)
    if not name:
        return 0
    # 在已有窗口里开新标签页的情况：窗口句柄没变，标题变成新目录。
    changed = [hwnd for hwnd, title in current.items()
               if name in title and before.get(hwnd) != title]
    if changed:
        return changed[0]
    same_title = [hwnd for hwnd, title in current.items() if name in title]
    return same_title[0] if same_title else 0


def _activate_opened_explorer(before: dict[int, str], name: str) -> None:
    try:
        hwnd = _wait_for_opened_window(before, name)
        if hwnd:
            _activate_window(hwnd)
    except Exception:  # noqa: BLE001 - 前台激活只是体验优化，失败不能影响已开的窗口
        return


def _activate_window(hwnd: int) -> bool:
    """把窗口切到前台。

    2026-11-15 在本机 Win11 实测的顺序：
    1. 窗口已经在最前，直接算成功；
    2. AttachThreadInput 借前台线程的输入队列再 SetForegroundWindow ——
       Windows 11 上多数时候仍返回 0（前台是别的进程时失败）；
    3. 注入一次 ALT 键再 SetForegroundWindow —— 「本进程收到过最近输入」
       就能拿到前台权，实测有效；
    4. SwitchToThisWindow 兜底。
    """
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    user32.ShowWindow(hwnd, _SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return True

    foreground = int(user32.GetForegroundWindow() or 0)
    current_tid = int(kernel32.GetCurrentThreadId())
    foreground_tid = 0
    if foreground:
        foreground_tid = int(user32.GetWindowThreadProcessId(foreground, None))
    attached = False
    if foreground_tid and foreground_tid != current_tid:
        attached = bool(user32.AttachThreadInput(foreground_tid, current_tid, True))
    try:
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(foreground_tid, current_tid, False)
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return True

    # ALT 只是「证明本进程有输入」，松手后不给前台应用留下按键状态。
    user32.keybd_event(_VK_MENU, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
    user32.SetForegroundWindow(hwnd)
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return True

    switch = getattr(user32, "SwitchToThisWindow", None)
    if switch is not None:
        switch(hwnd, True)
    return int(user32.GetForegroundWindow() or 0) == hwnd