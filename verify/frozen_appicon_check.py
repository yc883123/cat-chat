# -*- coding: utf-8 -*-
"""冻结版自检：应用内更换图标（`naiba/storage/app_icon.py` + `launcher._resolve_app_icon`）。

用法（在**打包好的解释器**里跑，验证新模块真的进了 PYZ）：

    dist\\naiba-chat.exe --run-skill-script verify\\frozen_appicon_check.py

为什么必须单列一支冻结版检查：`app_icon` 是新加的模块，PyInstaller 按 import 图收集，
**测试全绿不代表打进去了**——漏收时只有冻结版一点「更换图标」才会崩（同 §九.53 的 `av` 漏装）。
`icon.ico` 也一并在 `_MEIPASS` 里验：它是回退层的输入，漏收会让「恢复默认」变成通用图标。

两个**只在这种探针里才会遇到**的坑（都已踩过，别改回简单写法）：
1. 不能 `import launcher` —— `launcher.py` 是 PyInstaller 的**入口脚本**，不注册成可 import 模块；
2. 也不能取 `sys.modules["__main__"]` —— `launcher._run_skill_script` 用
   `runpy.run_path(script, run_name="__main__")` 跑本文件，会**临时顶掉** `__main__`。
   唯一可靠的拿法：沿调用栈往上找**持有 `_resolve_app_icon` 的那层 frame 的 `f_globals`**。

退出码 0 = 全通过；失败会逐条打印 `[FAIL]`。

注意：这类探针不要放 `dist/`（那是构建产物目录，`--clean` 重建后不保证留得住）。
"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

fails: list[str] = []
oks: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    (oks if cond else fails).append(f"{name}{(' — ' + str(detail)) if detail != '' else ''}")


# ---- 1. 新模块必须真的被打进 PYZ ----
try:
    from naiba.storage.app_icon import (
        APP_ICON_ICO_SIZES,
        APP_ICON_MAX_BYTES,
        clear_app_icon,
        has_custom_app_icon,
        normalize_app_icon,
        read_app_icon_png,
        store_app_icon,
    )

    check("import naiba.storage.app_icon", True)
    check("APP_ICON_MAX_BYTES == 5MB", APP_ICON_MAX_BYTES == 5 * 1024 * 1024, APP_ICON_MAX_BYTES)
    check("ICO 七尺寸", APP_ICON_ICO_SIZES == (16, 24, 32, 48, 64, 128, 256), APP_ICON_ICO_SIZES)
except Exception as exc:  # noqa: BLE001 - 探针要把任何异常变成一条可读的失败
    check("import naiba.storage.app_icon", False, f"{type(exc).__name__}: {exc}")

# ---- 2. PIL 在冻结版可用 + 归一化真跑一遍（非方形 → 留白贴方形，不裁切） ----
from PIL import Image  # noqa: E402 - 上一段失败时这里也该抛，属于「环境不可用」

buf = io.BytesIO()
Image.new("RGB", (600, 300), (10, 120, 240)).save(buf, format="PNG")
raw = buf.getvalue()
try:
    png_bytes, ico_bytes = normalize_app_icon(raw)
    with Image.open(io.BytesIO(png_bytes)) as im:
        check("归一化输出为方形 600x600", im.size == (600, 600), im.size)
        check("归一化输出为 RGBA", im.mode == "RGBA", im.mode)
        check("留白区透明（没被裁切/拉伸）", im.getpixel((2, 2))[3] == 0, im.getpixel((2, 2)))
    with Image.open(io.BytesIO(ico_bytes)) as im:
        # PIL 的 ICO `info["sizes"]` 在不同版本里是 (w,h) 元组或裸整数，两种都要吃得下
        sizes = getattr(im, "info", {}).get("sizes", []) or []
        got = sorted(int(s[0]) if isinstance(s, (tuple, list)) else int(s) for s in sizes)
        check("ICO 含全部七尺寸", got == [16, 24, 32, 48, 64, 128, 256], got)
except Exception as exc:  # noqa: BLE001
    check("归一化 / ICO 生成", False, f"{type(exc).__name__}: {exc}")


# ---- 3. 存储 + 解析（把 launcher 的 app_dir 指到临时目录） ----
class _Namespace:
    """把 launcher 的 `f_globals` 包成「能读写属性」的门面。

    直接改 `g["_app_dir"]` 也能生效（`_resolve_app_icon` 查的就是这层 globals），
    包一层只是为了继续用 `launcher.xxx` 的写法，读起来和源码模式一致。
    """

    def __init__(self, d: dict) -> None:
        object.__setattr__(self, "_d", d)

    def __getattr__(self, k: str):
        try:
            return object.__getattribute__(self, "_d")[k]
        except KeyError:
            raise AttributeError(k) from None

    def __setattr__(self, k: str, v) -> None:
        object.__getattribute__(self, "_d")[k] = v


tmp = Path(tempfile.mkdtemp(prefix="frozen_appicon_"))
resource_dir: object = "(未定位)"
try:
    store_app_icon(tmp, raw)
    check("两份文件成对落盘", has_custom_app_icon(tmp), sorted(p.name for p in tmp.iterdir()))

    g = None
    frame = sys._getframe()
    while frame is not None:
        if "_resolve_app_icon" in frame.f_globals:
            g = frame.f_globals
            break
        frame = frame.f_back
    check("能沿调用栈定位 launcher 的全局命名空间", g is not None, "frame-walk 未命中")

    launcher = _Namespace(g) if g is not None else None
    # RESOURCE_DIR 不在 launcher 的全局里，它是 `srv` 模块的属性（`srv.RESOURCE_DIR`）
    srv = getattr(launcher, "srv", None) if launcher is not None else None
    resource_dir = getattr(srv, "RESOURCE_DIR", "(缺)")
    check("_MEIPASS 里的内置 icon.ico 存在", Path(str(resource_dir), "icon.ico").is_file(), resource_dir)

    if launcher is not None:
        launcher._app_dir = lambda: str(tmp)  # 冻结版启动早期没有 srv.APP，直接改指向

    image, ico_path = launcher._resolve_app_icon()
    check("解析优先用自定义 .ico", ico_path is not None and Path(ico_path).name == "custom-icon.ico", ico_path)
    check("托盘图像可用（方形）", image is not None and image.size[0] == image.size[1], getattr(image, "size", None))

    with Image.open(io.BytesIO(read_app_icon_png(tmp, resource_dir))) as im:
        check("预览接口口径取到自定义方形", im.size == (600, 600), im.size)

    # 只坏 .ico（PNG 还完好）→ 必须**整体**回退，否则「托盘自定义、窗口通用」半截状态
    (tmp / "custom-icon.ico").write_bytes(b"not an ico")
    _image2, ico2 = launcher._resolve_app_icon()
    check("半坏的一对（PNG 好 / ICO 坏）也整体回退",
          ico2 is not None and Path(ico2).name != "custom-icon.ico", f"ico={ico2}")

    # 恢复默认只删 custom-icon.*，绝不碰内置资产
    clear_app_icon(tmp)
    check("恢复默认后两份都不存在", not has_custom_app_icon(tmp), sorted(p.name for p in tmp.iterdir()))
    _image3, ico3 = launcher._resolve_app_icon()
    check("回退后不再指自定义", ico3 is None or Path(ico3).name != "custom-icon.ico", ico3)
    check("内置 icon.ico 未被删除", Path(str(resource_dir), "icon.ico").is_file(), resource_dir)
except Exception as exc:  # noqa: BLE001
    check("存储 / 解析链路", False, f"{type(exc).__name__}: {exc}")

print("=== 冻结版 应用内更换图标 自检 ===")
print(f"python: {sys.version.split()[0]}  frozen={getattr(sys, 'frozen', False)}")
print(f"RESOURCE_DIR: {resource_dir}")
for line in oks:
    print("  [OK]  " + line)
for line in fails:
    print("  [FAIL] " + line)
print(f"结论：{len(oks)} 通过 / {len(fails)} 失败")
sys.exit(1 if fails else 0)
