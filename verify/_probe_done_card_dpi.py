# 一次性取证：完成卡片在真机上的真实落点 vs 预期落点（只读，不改产品代码）。
#
# 复刻 launcher.show_done_card 的坐标算法（GetSystemMetrics 物理像素直接当逻辑像素传给
# create_window），窗口加载完用 GetWindowRect 取**物理**真实落点，对比物理屏幕/工作区，
# 一次性写 JSON 结果后自毁。屏幕右下角会闪一个 ~368x128 的小窗 1-2 秒，属预期。
import ctypes
import json
import os
import sys

RESULT = os.path.join(os.path.dirname(__file__), "_probe_done_card_dpi.out.json")
CARD_W, CARD_H = 368, 128
TITLE = "naiba-card-dpi-probe"


def collect():
    user32 = ctypes.windll.user32
    phys_w, phys_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    wa = RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)  # SPI_GETWORKAREA

    hwnd = user32.FindWindowW(None, TITLE)
    rect = RECT()
    ok = bool(hwnd) and bool(user32.GetWindowRect(hwnd, ctypes.byref(rect)))
    dpi = user32.GetDpiForWindow(hwnd) if hwnd else 0
    return {
        "physical_screen": {"w": phys_w, "h": phys_h},
        "workarea_physical": {"left": wa.left, "top": wa.top, "right": wa.right, "bottom": wa.bottom},
        "dpi_for_window": dpi,
        "scale": (dpi / 96) if dpi else None,
        "window_rect_physical": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom} if ok else None,
        "found_hwnd": bool(hwnd),
    }


def main():
    import webview

    # —— 修复公式验证：物理像素 ÷ scale = 逻辑像素，下缘贴工作区（不再硬编码 76） ——
    user32 = ctypes.windll.user32
    phys_w, phys_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    wa = RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)  # SPI_GETWORKAREA
    dpi = user32.GetDpiForSystem() or 96
    scale = dpi / 96
    logical_w = phys_w / scale
    logical_bottom = wa.bottom / scale
    x = max(0, round(logical_w - CARD_W - 24))
    y = max(0, round(logical_bottom - CARD_H - 12))
    screen_w, screen_h = phys_w, phys_h
    passed = {"x": x, "y": y}

    window = webview.create_window(
        TITLE, "data:text/html,<body style='background:%237C5CFF'></body>",
        width=CARD_W, height=CARD_H, x=x, y=y,
        resizable=False, frameless=True, on_top=True, shadow=False,
    )

    def on_loaded():
        data = {"passed_to_create_window": passed, **collect()}
        # 判定：右缘/下缘是否越出物理屏幕、是否压进工作区（任务栏条带）
        r = data.get("window_rect_physical")
        if r:
            scr = data["physical_screen"]
            wa = data["workarea_physical"]
            data["verdict"] = {
                "right_overflow_px": r["right"] - scr["w"],
                "bottom_overflow_px": r["bottom"] - scr["h"],
                "left_overflow_px": 0 - r["left"],
                "top_overflow_px": 0 - r["top"],
                "covers_taskbar_band": r["bottom"] > wa["bottom"],
                "fully_offscreen": (r["left"] >= scr["w"] or r["top"] >= scr["h"]
                                    or r["right"] <= 0 or r["bottom"] <= 0),
            }
        with open(RESULT, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        window.destroy()

    window.events.loaded += on_loaded
    webview.start()
    print("result written:", RESULT)


if __name__ == "__main__":
    sys.exit(main())
