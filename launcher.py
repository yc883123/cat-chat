"""naiba-chat 桌面启动器。

内嵌窗口（pywebview）打开聊天界面，后台运行 HTTP 服务，并提供系统托盘图标。
- 关闭窗口：仅隐藏到托盘，服务继续运行（手机仍可访问）。
- 托盘"退出"：停止服务并退出整个程序。
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

import server as srv


class JsApi:
    """pywebview js_api 桥：供前端调用 Python 完成桌面端能力。

    WebView2 在非 debug 模式下关闭了默认右键菜单（AreDefaultContextMenusEnabled
    仅随 debug 开启），所以前端自绘了菜单；而浏览器剪贴板 API 在 WebView2/局域网
    HTTP 上并不总能拿到权限。这里提供一个写入 Windows CF_DIB 剪贴板的可靠通道：
    前端把图片字节（base64）传进来，用 PIL 归一化成 DIB 后写入剪贴板，任何桌面程序
    （画图/Word/微信等）都能直接粘贴。
    """

    def copy_image_to_clipboard(self, base64_data: str) -> dict:
        import base64
        import io
        import time

        try:
            import win32clipboard
            import win32con
            from PIL import Image
        except Exception as exc:  # pragma: no cover - env dependent
            return {"ok": False, "error": f"缺少图片/剪贴板依赖：{exc}"}

        try:
            raw = base64.b64decode(base64_data or "")
            image = Image.open(io.BytesIO(raw))
            buf = io.BytesIO()
            # 转成 24 位 BMP，去掉 14 字节 BITMAPFILEHEADER 后即 CF_DIB 数据。
            image.convert("RGB").save(buf, "BMP")
            dib = buf.getvalue()[14:]
        except Exception as exc:
            return {"ok": False, "error": f"图片解析失败：{exc}"}

        # 剪贴板可能被其它程序占用，做几次短暂重试。
        for _ in range(10):
            try:
                win32clipboard.OpenClipboard()
                break
            except Exception:
                time.sleep(0.05)
        else:
            return {"ok": False, "error": "无法打开系统剪贴板（可能被其它程序占用）"}

        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
        except Exception as exc:
            return {"ok": False, "error": f"写入剪贴板失败：{exc}"}
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
        return {"ok": True}


class Launcher:
    def __init__(self) -> None:
        self.httpd: ThreadingHTTPServer | None = None
        self.window = None
        self.tray = None
        self.should_quit = False
        self._exit_complete = threading.Event()
        self._exit_watchdog_started = False

    # ---- HTTP 服务（后台线程） ----
    def _run_server(self, host: str, port: int) -> None:
        self.httpd = srv.AppHTTPServer((host, port), srv.RequestHandler, srv.APP)
        self.httpd.daemon_threads = True
        srv.write_status(host, port, str(srv.APP.config.data["access_token"]))
        try:
            self.httpd.serve_forever(poll_interval=0.3)
        except Exception:
            pass

    def _stop_server(self) -> None:
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass

    # ---- 托盘动作 ----
    def _force_exit_if_stuck(self) -> None:
        if self._exit_complete.wait(10):
            return
        try:
            srv.STATUS_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        os._exit(0)

    def _quit(self, icon=None, item=None) -> None:
        self.should_quit = True
        if not self._exit_watchdog_started:
            self._exit_watchdog_started = True
            threading.Thread(target=self._force_exit_if_stuck, name="naiba-exit-watchdog", daemon=True).start()
        self._stop_server()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        # 真正销毁窗口，让 webview.start() 返回
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

    def _show_window(self) -> None:
        if self.window:
            try:
                self.window.show()
                self.window.restore()
            except Exception:
                pass

    def _open_browser(self, url: str):
        def _open():
            webbrowser.open(url)
        return _open

    def _build_tray(self, local_url: str):
        import pystray
        from PIL import Image, ImageDraw

        icon_path = srv.RESOURCE_DIR / "icon.ico"
        try:
            image = Image.open(icon_path).convert("RGBA") if icon_path.is_file() else None
        except (OSError, ValueError):
            image = None
        if image is None:
            image = Image.new("RGB", (64, 64), (18, 100, 64))
            draw = ImageDraw.Draw(image)
            draw.ellipse((14, 14, 50, 50), fill=(255, 255, 255))
            draw.ellipse((22, 22, 42, 42), fill=(18, 100, 64))

        menu = pystray.Menu(
            pystray.MenuItem("打开窗口", lambda: self._show_window(), default=True),
            pystray.MenuItem("在浏览器打开", self._open_browser(local_url)),
            pystray.MenuItem("退出", self._quit),
        )
        return pystray.Icon("naiba-chat", image, "naiba-chat", menu)

    def _on_window_closing(self) -> bool:
        # 用户点关闭：若是要退出（托盘点了退出），放行；否则隐藏到托盘
        if self.should_quit:
            return True
        if self.window:
            try:
                self.window.hide()
            except Exception:
                pass
        return False  # 阻止真正关闭

    def _on_window_loaded(self) -> None:
        """页面就绪后确认主窗口可见。

        更新流程的重启脚本若带上 SW_HIDE（此前 apply-update.ps1 用了
        `-WindowStyle Hidden`），新进程会正常跑起来、托盘图标也在，但主窗口不出来，
        用户只能到托盘双击「打开窗口」才能唤回界面。这里主动 show/restore 一次，
        把这类外部启动方式的隐藏标记抹平。
        """
        self._show_window()

    def run(self) -> None:
        import webview

        if sys.platform == "win32":
            try:
                # 让任务栏使用本进程（EXE）图标，而不是 Python 默认图标
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("naiba.chat")
            except Exception:
                pass

        try:
            instance_lock = srv.acquire_instance_lock()
        except RuntimeError as exc:
            # 已在运行或数据目录/锁文件不可写：简洁中文提示退出，不输出 traceback。
            _notify_startup_error(str(exc))
            raise SystemExit(2) from exc
        srv.APP = srv.NaibaChatApp()
        srv.APP.update_restart_callback = self._quit
        host = str(srv.APP.config.data.get("host", "0.0.0.0"))
        port = int(srv.APP.config.data.get("port", 8765))
        srv.APP.listener_host = host
        token = str(srv.APP.config.data["access_token"])
        local_url = f"http://127.0.0.1:{port}"
        page_url = f"{local_url}/?token={token}"

        server_thread = threading.Thread(target=self._run_server, args=(host, port), daemon=True)
        server_thread.start()
        for _ in range(50):
            try:
                with __import__("urllib.request").request.urlopen(f"{local_url}/api/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.1)

        self.tray = self._build_tray(local_url)
        threading.Thread(target=self.tray.run, daemon=True).start()

        self.window = webview.create_window(
            "naiba-chat",
            page_url,
            js_api=JsApi(),
            width=1280,
            height=860,
            min_size=(900, 600),
            text_select=True,
        )
        self.window.events.closing += self._on_window_closing
        # 窗口就绪即确保可见（见 _on_window_loaded：更新脚本重启时曾把主窗口一起藏起来）。
        self.window.events.loaded += self._on_window_loaded
        # 启动后仅做后台元数据检查（不自动安装），前端通过 30 秒轮询感知结果。
        threading.Timer(4.0, srv.APP.updater.start_check).start()
        icon_path = srv.RESOURCE_DIR / "icon.ico"
        start_kwargs = {}
        if icon_path.is_file():
            start_kwargs["icon"] = str(icon_path)
        # WebView2 持久化 profile：pywebview 的 private_mode 默认 True，会把 profile 放进
        # 临时目录并在进程退出时整个删除 —— 前端存在 localStorage 的偏好（侧栏宽度、
        # 文件面板宽度、顶栏 Skill 勾选、交互模式）因此每次启动都被重置。
        # 目录放在 app_dir 下（冻结版 = %LOCALAPPDATA%\NaibaChat\webview），**不放进 data_dir**：
        # 免得被数据目录迁移/备份当成用户数据一起搬走。
        try:
            storage_dir = srv.APP.paths.app_dir / "webview"
            storage_dir.mkdir(parents=True, exist_ok=True)
            start_kwargs["private_mode"] = False
            start_kwargs["storage_path"] = str(storage_dir)
        except OSError as exc:
            # 目录不可写就退回私有模式：界面偏好会重置，但不影响启动与功能。
            print(f"[launcher] WebView2 持久化目录不可用，回退私有模式：{exc}", file=sys.stderr)
        try:
            webview.start(**start_kwargs)
        finally:
            self._stop_server()
            srv.APP.stop()
            try:
                srv.STATUS_PATH.unlink(missing_ok=True)
            except OSError:
                pass
            instance_lock.close()
            self._exit_complete.set()


def _notify_startup_error(message: str) -> None:
    """桌面端启动失败提示：优先弹系统消息框（窗口/托盘场景下用户看不到 stderr）。"""
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, "naiba-chat 启动失败", 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def _force_utf8_stdio(streams=None) -> None:
    """把子进程 stdout/stderr 强制为 UTF-8（冻结版 runw 下 PYTHONIOENCODING 未必生效）。

    背景：`run_skill_script` 的父进程按 UTF-8 解码子进程输出；若子进程按 locale(GBK)
    输出，脚本打印的中文路径会变成乱码（用户实测：`C:\\...\\临时提示词\\...` 打印成
    `C:\\...\\??ʱ??ʾ??\\...`），而父进程再把这个乱码路径当媒体产物 → 前端破图。
    """
    for stream in (streams if streams is not None else (sys.stdout, sys.stderr)):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            continue


def _run_skill_script(argv: list[str]) -> int:
    """冻结版子进程入口：naiba-chat.exe --run-skill-script <script> [args...]。

    只设置 sys.argv 后以 __main__ 方式执行脚本，不初始化 GUI/HTTP 服务/实例锁。
    返回进程退出码。
    """
    _force_utf8_stdio()
    if not argv:
        print("缺少 --run-skill-script 的脚本路径", file=sys.stderr)
        return 2
    script = argv[0]
    if not os.path.isfile(script):
        print(f"脚本不存在：{script}", file=sys.stderr)
        return 1
    sys.argv = [script, *argv[1:]]
    try:
        import runpy

        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return 0 if code is None else 1
    except BaseException:
        import traceback

        traceback.print_exc()
        return 1
    return 0


def main() -> None:
    # 隐藏入口：冻结版执行 .py Skill 脚本。必须先于任何初始化处理，
    # 否则 naiba-chat.exe --run-skill-script <script> 会走到实例锁，
    # 被误判为"已在运行"而无法执行脚本。
    argv = sys.argv[1:]
    if "--run-skill-script" in argv:
        idx = argv.index("--run-skill-script")
        raise SystemExit(_run_skill_script(argv[idx + 1 :]))
    Launcher().run()


if __name__ == "__main__":
    main()
