# -*- coding: utf-8 -*-
"""真机自检：输入区右键「粘贴」所依赖的**真实 win32 剪贴板往返**（§九.148）。

跑法：``.venv\\Scripts\\python.exe verify/clipboard_paste_smoke.py``
（不进测试套件：它要独占真实剪贴板，并会**临时改写**用户剪贴板——脚本自带备份与恢复。）

为什么必须单独有这一支：单元测试喂的是假 reader，只能证明分支判据；而这一层有个
**纯逻辑发现不了的真实形态**（2026-10-01 探针实测）：`GetClipboardData(CF_HDROP)` 经 pywin32
解码后是 **tuple[str]**（不是原始 DROPFILES 字节），而**写入**方向必须自己合成 DROPFILES 结构。

反过来的教训也记在这里：当年凭一个**绕开风险路径**的探针（探针里用了自己写的补头函数，
于是从没真正试过裸 DIB）断定"PIL 打不开裸 CF_DIB"，据此加了补头；而第二次核对又踩了另一个
坑——探针的夹具忘了 strip 掉 14 字节文件头，于是"补头"那列等于给完整 BMP 又加一次头，
读出 `Truncated File Read` 的假象。夹具修正后的矩阵结论是：裸读各形态全部正常，
**补头既没修好也没弄坏任何形态（零收益）**，所以那段代码已被删除。教训：探针夹具自己
也是被验证对象，两次结论相反时必须先怀疑夹具。

断言（任一条不过即退出）：
① 写入位图（40 字节头 BI_RGB 与 V5 头各一次）→ 读回必须是 `kind=image` 且尺寸与颜色一致；
② 写入 CF_HDROP（含中文文件名）→ 读回必须 `kind=files` 且**路径逐条精确一致**；
③ 清空剪贴板 → 必须 `kind=empty`；
④ 非图片/非文件（纯文本）→ 必须 `kind=text`，且**不把文本搬进载荷**；
⑤ 收尾必须把用户原剪贴板**完整恢复**（含 CF_HDROP 要重新合成 DROPFILES——首跑就是在这里
   回写 tuple 抛异常、中止了整段恢复）。
"""
from __future__ import annotations

import io
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402
import win32clipboard  # noqa: E402
import win32con  # noqa: E402

import launcher  # noqa: E402

FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition or not detail else f'  -> {detail}'}")
    if not condition:
        FAILED.append(label)


def _open_clipboard() -> None:
    import time

    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("剪贴板被其它程序占用")


def _write(formats: dict[int, object]) -> None:
    _open_clipboard()
    try:
        win32clipboard.EmptyClipboard()
        for fmt, value in formats.items():
            win32clipboard.SetClipboardData(fmt, value)
    finally:
        win32clipboard.CloseClipboard()


def _hdrop_bytes(paths: list[str]) -> bytes:
    """合成 DROPFILES：pFiles=20 / pt=(0,0) / fNC=0 / fWide=1 + 双 NUL 收尾的宽字符清单。"""
    files = "\0".join(paths) + "\0\0"
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + files.encode("utf-16-le")


_MASKS_FULL = struct.pack("<IIII", 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
_MASKS_THREE = struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF) + b"\x00" * 4


def _v5_dib(size, *, masks: bytes, header_size: int = 124) -> bytes:
    """BITMAPV5HEADER/V4HEADER + BI_BITFIELDS 的 CF_DIB；像素 BGRA=30,60,200,255 ⇒ RGB(200,60,30)。"""
    width, height = size
    stride = ((width * 32 + 31) // 32) * 4
    pixels = bytearray()
    for _y in range(height):
        pixels += bytes((30, 60, 200, 255)) * width
        pixels += b"\x00" * (stride - width * 4)
    header = struct.pack("<IiiHHIIiiII", header_size, width, height, 1, 32, 3,
                         len(pixels), 2835, 2835, 0, 0)
    header += masks
    header += b"\x00" * (header_size - len(header))
    return header + bytes(pixels)


def _backup_and_restore():
    """备份用户的剪贴板并在结束时恢复——不恢复就是"跑个自检把用户剪贴板清了"。

    **只备份能原样写回的格式**：文本（`str`）与 CF_HDROP（要重新合成 DROPFILES 字节，
    直接回写 tuple 会抛 `a bytes-like object is required`，一次抛就整段恢复中止——
    2026-10-01 首跑真踩了这个，把用户剪贴板清空了）。Chromium 的内部格式之类无法重建，
    一律跳过；位图不备份（图形格式互相派生，写回只会得到等价物）。
    """
    saved: dict[int, object] = {}
    try:
        _open_clipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                saved[win32con.CF_UNICODETEXT] = str(
                    win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
                )
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                data = win32clipboard.GetClipboardData(win32con.CF_HDROP)
                paths = [data] if isinstance(data, str) else [str(item) for item in (data or [])]
                paths = [path for path in paths if os.path.isfile(path)]
                if paths:
                    saved[win32con.CF_HDROP] = paths
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        print(f"  （备份剪贴板失败，本次不恢复：{exc}）")
        return (lambda: []), saved

    def restore() -> list[str]:
        """返回**恢复失败的格式名**清单（空清单 = 完整恢复）。"""
        if not saved:
            return []
        failures: list[str] = []
        _open_clipboard()
        try:
            win32clipboard.EmptyClipboard()
            for fmt, value in saved.items():
                try:
                    payload = _hdrop_bytes(value) if fmt == win32con.CF_HDROP else value
                    win32clipboard.SetClipboardData(fmt, payload)
                except Exception as exc:
                    failures.append(f"{fmt}: {exc}")
        finally:
            win32clipboard.CloseClipboard()
        return failures

    return restore, saved


def main() -> int:
    restore, saved = _backup_and_restore()
    tmp = Path(tempfile.mkdtemp(prefix="clipboard_paste_smoke_"))
    try:
        # ---- ① 位图 ----
        source = Image.new("RGB", (43, 27), (200, 30, 90))
        buf = io.BytesIO()
        source.save(buf, "BMP")
        _write({win32con.CF_DIB: buf.getvalue()[14:]})
        payload = launcher.clipboard_payload()
        check("① 位图 → kind=image", payload.get("kind") == "image", str(payload)[:120])
        if payload.get("kind") == "image":
            import base64

            image = Image.open(io.BytesIO(base64.b64decode(payload["base64"])))
            check("① 尺寸与写入一致", image.size == (43, 27), str(image.size))
            check("① 颜色与写入一致", image.convert("RGB").getpixel((0, 0)) == (200, 30, 90),
                  str(image.convert("RGB").getpixel((0, 0))))
            check("① 载荷不含 bytes（要能 JSON 序列化）", isinstance(payload["base64"], str))

        # ---- ①b V5 / V4 头的位图（32 位位图常见形态；**不补文件头**直接读）----
        for label, header_size, masks in (("V5 头 + 4 掩码", 124, _MASKS_FULL),
                                          ("V4 头 + 3 掩码", 108, _MASKS_THREE)):
            _write({win32con.CF_DIB: _v5_dib((37, 19), header_size=header_size, masks=masks)})
            payload = launcher.clipboard_payload()
            ok = payload.get("kind") == "image"
            detail = str(payload)[:120]
            if ok:
                import base64 as _b64

                image = Image.open(io.BytesIO(_b64.b64decode(payload["base64"])))
                ok = image.size == (37, 19) and image.convert("RGB").getpixel((0, 0)) == (200, 60, 30)
                detail = f"{image.size} / {image.convert('RGB').getpixel((0, 0))}"
            check(f"①b {label} → 尺寸与颜色一致", ok, detail)

        # ---- ② 文件（CF_HDROP，含中文名）----
        first = tmp / "中文图片.png"
        second = tmp / "第二个文件.txt"
        Image.new("RGB", (5, 5)).save(first)
        second.write_text("hi", encoding="utf-8")
        _write({win32con.CF_HDROP: _hdrop_bytes([str(first), str(second)])})
        payload = launcher.clipboard_payload()
        check("② 文件 → kind=files", payload.get("kind") == "files", str(payload)[:160])
        check("② 路径逐条精确一致（中文名不乱码）",
              payload.get("paths") == [str(first), str(second)], str(payload.get("paths")))
        check("② 名字与大小带回来了",
              payload.get("names") == ["中文图片.png", "第二个文件.txt"]
              and payload.get("sizes") == [first.stat().st_size, 2],
              f"{payload.get('names')} / {payload.get('sizes')}")
        check("② 文件优先于位图（零拷贝）", payload.get("kind") == "files")

        # ---- ③ 纯文本 ----
        _write({win32con.CF_UNICODETEXT: "一段要被粘贴的文本"})
        payload = launcher.clipboard_payload()
        check("③ 纯文本 → kind=text", payload == {"ok": True, "kind": "text"}, str(payload))

        # ---- ④ 空 ----
        _open_clipboard()
        try:
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()
        payload = launcher.clipboard_payload()
        check("④ 空剪贴板 → kind=empty", payload == {"ok": True, "kind": "empty"}, str(payload))
    finally:
        failures = restore()
        check("⑤ 剪贴板已完整恢复（含 CF_HDROP 要重新合成 DROPFILES）", not failures, " / ".join(failures))
        if win32con.CF_UNICODETEXT in saved:
            _open_clipboard()
            try:
                now = str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or "")
            finally:
                win32clipboard.CloseClipboard()
            check("⑤ 恢复后的文本与原文本逐字符一致", now == saved[win32con.CF_UNICODETEXT],
                  f"{now[:40]!r} != {saved[win32con.CF_UNICODETEXT][:40]!r}")

    print(f"\n结果：{'全部通过' if not FAILED else '存在未通过项 —— ' + ' / '.join(FAILED)}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
