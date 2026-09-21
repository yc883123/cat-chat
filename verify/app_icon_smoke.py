# -*- coding: utf-8 -*-
"""「应用内更换图标」端到端冒烟（自编排：隔离源码实例 + 真 HTTP + 真浏览器 + 进程级重启解析）。

为什么必须真后端（§九.92）：上传要真落盘、状态要真持久、预览图要服务端归一化后的真图；
静态 public/ 服务下 POST/DELETE 一律 404，而弹窗与预览看起来照样能画——断言会全绿却什么都没验。

三层断言：
A. HTTP 层（本文件，urllib 真发请求）：状态/上传/归一化尺寸/三种非法输入被拒且不落盘/恢复默认；
B. 浏览器层（app_icon_smoke.cjs）：入口位置、弹窗状态流转、真 `setInputFiles` 上传、预览图
   是服务端出的方形图（`naturalWidth`）、关掉重开仍在、恢复默认回内置、零页面错误；
C. 进程级「重启生效」：**另一个进程**（本文件的进程 ≠ 服务进程）调 `launcher._resolve_app_icon`
   解析托盘/窗口图标——自定义优先、删掉后回内置 icon.ico。桌面壳的真实托盘/窗口外观
   需要人工看（GUI 不在自动化范围内），这里证明的是"重启后会被加载到的那份图标"。

用法：.venv\\Scripts\\python.exe verify\\app_icon_smoke.py
（收尾要删隔离目录，若被沙箱批量删除护栏拦下：前面加 CODEBUDDY_SAFE_DELETE_ENABLED=0）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # C 层要 import launcher / server（脚本在 verify/ 下运行）
PORT = int(os.environ.get("NAIBA_APP_ICON_PORT", "8808"))
BASE = f"http://127.0.0.1:{PORT}"
TMP_ROOT = ROOT / "verify" / "_tmp_app_icon"
SERVER_LOG = ROOT / "verify" / "_app_icon_smoke_server.log"
FIXTURE = TMP_ROOT / "fixture_icon.png"
# 夹具是非方形（300×600）：归一化后长边 600 ⇒ 用来验证"贴方形"真的生效。
FIXTURE_SIZE = (300, 600)
EXPECTED_SQUARE = 600

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok or not detail else f"  -> {detail}"))
    if not ok:
        failures.append(label)


def wait_until(predicate, timeout: float = 45.0, interval: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是预期分支
            pass
        time.sleep(interval)
    return False


def png(size: tuple[int, int], left=(200, 20, 20), right=(20, 20, 200)) -> bytes:
    """左红右蓝的 PNG：既能验尺寸，也能验"两侧内容都在"。"""
    from PIL import Image

    img = Image.new("RGB", size, left)
    for x in range(size[0] // 2, size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), right)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def request(method: str, path: str, raw: bytes | None = None, filename: str = "icon.png"):
    """返回 (status, bytes)；4xx/5xx 也当正常返回（断言看 status）。"""
    headers = {}
    if raw is not None:
        boundary = "----naiba-app-icon-smoke"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            raw,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        body = None
    req = urllib.request.Request(f"{BASE}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310 - 固定本机地址
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        return 0, str(exc).encode("utf-8")


def json_of(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def current_icon_bytes() -> tuple[bytes, bytes]:
    png_path = TMP_ROOT / "custom-icon.png"
    ico_path = TMP_ROOT / "custom-icon.ico"
    return (
        png_path.read_bytes() if png_path.is_file() else b"",
        ico_path.read_bytes() if ico_path.is_file() else b"",
    )


def http_layer() -> None:
    from PIL import Image

    print("---- A. HTTP 层 ----")
    status, raw = request("GET", "/api/app-icon")
    check("GET /api/app-icon 默认态 = custom:false", status == 200 and json_of(raw) == {"custom": False},
          f"{status} {raw[:120]!r}")

    status, raw = request("GET", "/api/app-icon/image")
    builtin_ok = status == 200 and raw[:8] == b"\x89PNG\r\n\x1a\n"
    builtin_size = Image.open(io.BytesIO(raw)).size if builtin_ok else (0, 0)
    check("GET /api/app-icon/image 默认回内置图标的 PNG", builtin_ok and builtin_size[0] == builtin_size[1],
          f"{status} size={builtin_size}")

    # 真上传
    status, raw = request("POST", "/api/app-icon", png(FIXTURE_SIZE))
    check("POST /api/app-icon 合法图片 → 200 + custom:true",
          status == 200 and json_of(raw).get("custom") is True, f"{status} {raw[:160]!r}")

    png_bytes, ico_bytes = current_icon_bytes()
    check("两份文件都落到 app_dir（custom-icon.png / custom-icon.ico）", bool(png_bytes) and bool(ico_bytes),
          f"png={len(png_bytes)} ico={len(ico_bytes)}")
    ico_sizes = sorted(size[0] for size in Image.open(io.BytesIO(ico_bytes)).ico.sizes()) if ico_bytes else []
    check("ICO 齐 7 档尺寸（16…256）", ico_sizes == [16, 24, 32, 48, 64, 128, 256], str(ico_sizes))

    preview = Image.open(io.BytesIO(png_bytes))
    check(f"归一化：非方形 {FIXTURE_SIZE[0]}×{FIXTURE_SIZE[1]} → 方形 {EXPECTED_SQUARE}×{EXPECTED_SQUARE}",
          preview.size == (EXPECTED_SQUARE, EXPECTED_SQUARE), str(preview.size))
    # 竖图 → 左右留白：内容区居中，两侧透明；左侧红、右侧蓝 ⇒ 两侧内容都没被裁掉。
    pad = (EXPECTED_SQUARE - FIXTURE_SIZE[0]) // 2
    middle = EXPECTED_SQUARE // 2
    check("归一化：留白透明、内容不裁切（左红右蓝两侧都在）",
          preview.getpixel((pad + 1, middle))[:3] == (200, 20, 20)
          and preview.getpixel((EXPECTED_SQUARE - pad - 2, middle))[:3] == (20, 20, 200)
          and preview.getpixel((1, middle))[3] == 0
          and preview.getpixel((EXPECTED_SQUARE - 2, middle))[3] == 0
          and preview.getpixel((1, 1))[3] == 0
          and preview.getpixel((EXPECTED_SQUARE - 2, EXPECTED_SQUARE - 2))[3] == 0,
          f"pad={pad} left={preview.getpixel((pad + 1, middle))} right={preview.getpixel((EXPECTED_SQUARE - pad - 2, middle))}")

    status, raw = request("GET", "/api/app-icon/image")
    served = Image.open(io.BytesIO(raw)).size if status == 200 else (0, 0)
    check("预览接口返回的是自定义图（不是内置）", served == (EXPECTED_SQUARE, EXPECTED_SQUARE), f"{status} {served}")

    # 非法输入：必须被拒且不动已有图标
    status, raw = request("POST", "/api/app-icon", b"this is not an image")
    check("非图片被拒（400 + 中文文案）", status == 400 and "图片" in json_of(raw).get("error", ""),
          f"{status} {json_of(raw)}")
    status, raw = request("POST", "/api/app-icon", png((30, 30)))
    check("小于 64px 被拒（400 + 写明下限）",
          status == 400 and "64" in json_of(raw).get("error", ""), f"{status} {json_of(raw)}")
    status, raw = request("POST", "/api/app-icon", b"x" * (5 * 1024 * 1024 + 1))
    check("超过 5MB 被拒（413 或 400）", status in (400, 413), f"{status} {json_of(raw)}")
    check("被拒的上传没有破坏已有图标", current_icon_bytes() == (png_bytes, ico_bytes))

    status, raw = request("GET", "/api/app-icon")
    check("状态仍是 custom:true", status == 200 and json_of(raw).get("custom") is True, f"{status} {raw[:80]!r}")


def restart_layer() -> None:
    """C. 进程级「重启生效」：本进程 ≠ 服务进程，调启动期真正用的解析器。"""
    print("---- C. 进程级重启解析 ----")
    from types import SimpleNamespace

    import launcher
    from PIL import Image

    status, raw = request("POST", "/api/app-icon", png(FIXTURE_SIZE))
    check("（准备）重新上传一份自定义图标", status == 200 and json_of(raw).get("custom") is True, f"{status}")

    launcher.srv.APP = SimpleNamespace(paths=SimpleNamespace(app_dir=TMP_ROOT))
    image, ico_path = launcher._resolve_app_icon()
    check("重启后解析到自定义图标（托盘用图 = 归一化方形图）",
          image.size == (EXPECTED_SQUARE, EXPECTED_SQUARE), str(image.size))
    check("窗口图标路径指向自定义 .ico",
          ico_path is not None and Path(ico_path) == TMP_ROOT / "custom-icon.ico", str(ico_path))

    status, raw = request("DELETE", "/api/app-icon")
    check("DELETE /api/app-icon → 200 + custom:false",
          status == 200 and json_of(raw).get("custom") is False, f"{status} {json_of(raw)}")
    check("恢复默认后两份自定义文件都被删掉", current_icon_bytes() == (b"", b""))
    builtin_ico = ROOT / "icon.ico"
    check("恢复默认不碰仓库内置 icon.ico", builtin_ico.is_file() and builtin_ico.stat().st_size > 0)

    image, ico_path = launcher._resolve_app_icon()
    check("再重启解析回内置图标", ico_path is not None and Path(ico_path) == builtin_ico, str(ico_path))
    check("内置图标可解码", image.size[0] > 0, str(image.size))

    status, _raw = request("DELETE", "/api/app-icon")
    check("再次恢复默认仍 200（幂等）", status == 200, str(status))
    status, raw = request("GET", "/api/app-icon")
    check("状态回到 custom:false", status == 200 and json_of(raw).get("custom") is False, f"{status}")


def browser_layer() -> int:
    print("---- B. 浏览器层 ----")
    env = dict(os.environ)
    env.update({
        "NAIBA_SMOKE_BASE": BASE,
        "NAIBA_ICON_FIXTURE": str(FIXTURE),
        "NODE_PATH": str(ROOT / "node_modules"),
    })
    try:
        proc = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "app_icon_smoke.cjs")],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("找不到 node：本机 node 装在 D:\\Apps\\NodeJS，需先加进 PATH")
        return 1
    print(proc.stdout or "", end="")
    if proc.stderr.strip():
        print("--- node stderr ---")
        print(proc.stderr)
    return proc.returncode


def main() -> int:
    # 每轮全新隔离根：启动前删才是最可靠的"干净"（跑完再删可能被沙箱批量删除护栏拦下）。
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes(png(FIXTURE_SIZE))
    # 隔离实例的 resource_dir 就是它的根目录（`PathContext.local`）——把仓库自带的 icon.ico
    # 放一份进去，等于复现"内置图标可用"的正常形态（冻结版里它就躺在 exe 旁的 _MEIPASS 下）。
    # 不放的话默认态预览必然 404，那是夹具缺件，不是产品问题。
    shutil.copyfile(ROOT / "icon.ico", TMP_ROOT / "icon.ico")

    log = SERVER_LOG.open("w", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "NAIBA_TMP_ROOT": str(TMP_ROOT),
        "NAIBA_TMP_PORT": str(PORT),
    })
    server = subprocess.Popen(  # noqa: S603 - 固定 argv
        [sys.executable, str(ROOT / "verify" / "_serve_tmp.py")],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        if not wait_until(lambda: request("GET", "/api/health")[0] == 200):
            print(f"隔离实例未就绪（日志见 {SERVER_LOG}）")
            return 1
        print(f"隔离实例就绪 {BASE}（app_dir={TMP_ROOT}）")

        http_layer()
        # 浏览器层要从「默认态」起步（它第一条断言就是默认态文案）：A 层结束在自定义态，
        # 这里显式恢复默认。
        request("DELETE", "/api/app-icon")
        node_code = browser_layer()
        if node_code != 0:
            failures.append("浏览器层断言未全绿")
        restart_layer()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        log.close()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
