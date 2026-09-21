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
import urllib.error
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

import server as srv
from naiba.core.network import port_conflict_message
from naiba.storage.app_icon import app_icon_paths

# 原生那边把文件路径交给 pywebview 需要一点时间（两条独立的消息通道）。测试会把它改成 0。
FOLDER_DROP_SETTLE_SECONDS = 0.2


def _app_dir() -> Path:
    """当前应用目录（自定义图标就落在它下面）：优先取已装配的 app 实例。

    冻结版 = `%LOCALAPPDATA%\\NaibaChat`，源码版 = 仓库根；`srv.APP` 在 run() 装配前
    还不存在，所以这里必须 getattr 兜底到模块级常量，不能让启动早退。
    """
    paths = getattr(getattr(srv, "APP", None), "paths", None)
    app_dir = getattr(paths, "app_dir", None)
    return Path(app_dir) if app_dir else Path(srv.APP_DIR)


def _drop_debug_log(message: str) -> None:
    """把"拖文件夹"这条链路的实况写一行到数据目录下的 `drop-debug.log`。

    这条路是「前端 JS → WebView2 原生 → Python」三段接力，任何一段断了，用户只会看到
    一句兜底提示，而 Python 侧连异常都看不到（原生那一步的异常抛在页面里）。所以每一段
    都落一行日志，出问题时**直接读文件**就能定位，不用再问用户要现象。
    """
    try:
        paths = getattr(getattr(srv, "APP", None), "paths", None)
        data_dir = getattr(paths, "data_dir", None)
        if not data_dir:
            data_dir = getattr(srv, "DATA_DIR", None)
        if not data_dir:
            return
        target = Path(data_dir) / "drop-debug.log"
        if target.exists() and target.stat().st_size > 256 * 1024:
            target.unlink(missing_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass  # 诊断日志永远不该拦住功能


def _resolve_app_icon():
    """解析应用图标，返回 (托盘用 PIL Image, 窗口用 .ico 路径或 None)。

    优先级：自定义（`app_dir/custom-icon.*`，两张齐备）→ 内置 `RESOURCE_DIR/icon.ico`
    → 兜底现画一个。托盘与 pywebview 窗口图标**共用这一个出口**：各写一份解析就会
    出现「托盘换了、窗口没换」的半截状态（自定义图标是两张文件驱动的，两处读错一张
    就会分叉）。任何一层读不出来都静默回退下一层——图标问题不该拦住启动。
    """
    from PIL import Image, ImageDraw

    custom_png, custom_ico = app_icon_paths(_app_dir())
    if custom_png.is_file() and custom_ico.is_file():
        try:
            with Image.open(custom_png) as img:
                tray_image = img.convert("RGBA")
            # .ico 也要读一次：它只交给 Windows/pywebview 用，坏了不会抛，只会让窗口
            # 悄悄退回通用图标——那正是「托盘换了、窗口没换」的半截状态。两张都能读
            # 才认这一对；`format` 判据顺带挡掉「PNG 改了后缀冒充 .ico」。
            with Image.open(custom_ico) as ico:
                if ico.format != "ICO":
                    raise ValueError("not an ico")
            return tray_image, str(custom_ico)
        except (OSError, ValueError):
            pass  # 文件在但坏了：回退默认，不报错
    default_ico = Path(srv.RESOURCE_DIR) / "icon.ico"
    if default_ico.is_file():
        try:
            with Image.open(default_ico) as img:
                return img.convert("RGBA"), str(default_ico)
        except (OSError, ValueError):
            pass
    image = Image.new("RGB", (64, 64), (18, 100, 64))
    draw = ImageDraw.Draw(image)
    draw.ellipse((14, 14, 50, 50), fill=(255, 255, 255))
    draw.ellipse((22, 22, 42, 42), fill=(18, 100, 64))
    return image, None


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

    # ---- 拖文件夹进输入区：把"拖进来的文件夹在硬盘上的真实路径"交回前端 ----
    def naibaFolderDrop(self, native_error: str = "") -> dict:
        """前端在识别出"拖进来的是目录"时，主动追问：刚才那个目录的绝对路径是啥？

        为什么不继续用 pywebview 的 drop 事件回调（`Launcher._on_composer_drop`）：
        WebView2 的 `chrome.webview.postMessageWithAdditionalObjects` 只要收到不受支持的
        对象就会**抛异常、整条消息作废**（微软官方文档写明），而 pywebview 注入的 drop
        监听正是走这一步。它一抛，紧随其后的 `postMessage` 也不会执行 —— 意味着
        **Python 侧永远等不到那个事件**，页面上却什么错都看不到，前端只能超时弹兜底提示。
        这正是"浏览器里拖文件夹拿不到完整路径"这句提示在桌面客户端里也出现的原因。

        现在的分工（三段各自可控）：
          1. 前端在 drop 事件里**自己**调 `postMessageWithAdditionalObjects`，并把异常抓下来
             （异常文本通过 `native_error` 带进来，兜底时能一眼看出是"原生这一步就废了"）；
          2. WebView2 把文件对象连同真实路径交给 pywebview，pywebview 存进 `_dnd_state['paths']`
             （**前提是有 drop 监听被注册过**，见 `_register_drop_listener`）；
          3. 这里直接读 `_dnd_state['paths']`，只挑**目录**，交回前端去建索引。

        普通文件不在这里管：它们仍走前端原有的上传链路（`uploadFiles`）。
        """
        result: dict = {"dirs": [], "reason": "start", "native_error": str(native_error or "")}
        try:
            from webview.dom import _dnd_state
        except Exception as exc:  # pragma: no cover - 环境相关
            result["reason"] = "import-failed"
            _drop_debug_log(f"folderDrop import failed: {exc!r}")
            return result

        # 原生消息（第 2 段）和这次桥调用是两条独立的路，给它一点落地时间。
        # 200ms 足够：那条消息在 drop 事件里就同步发出去了，桥调用还要一个来回。
        time.sleep(FOLDER_DROP_SETTLE_SECONDS)
        try:
            raw = list(_dnd_state.get("paths") or [])
        except Exception as exc:  # pragma: no cover - 环境相关
            result["reason"] = "state-failed"
            _drop_debug_log(f"folderDrop state failed: {exc!r}")
            return result

        dirs: list[str] = []
        for entry in raw:
            try:
                full = str(entry[1] or "").strip()
            except Exception:
                continue
            if not full or not os.path.isdir(full):
                continue
            if full not in dirs:
                dirs.append(full)

        # 消费过的路径要清掉：这个列表是 pywebview 的全局队列，留着会被**下一次**拖拽
        # 当成"这次的新路径"，也会让 pywebview 的事件回调按文件名错配到别的对象上。
        if raw:
            try:
                _dnd_state["paths"] = []
            except Exception:
                pass

        result["dirs"] = dirs
        result["reason"] = "ok" if dirs else ("empty" if not raw else "no-dir")
        _drop_debug_log(
            "folderDrop raw=%d dirs=%d reason=%s native_error=%r dirs=%r"
            % (len(raw), len(dirs), result["reason"], result["native_error"], dirs)
        )
        return result


class Launcher:
    def __init__(self) -> None:
        self.httpd: ThreadingHTTPServer | None = None
        self.window = None
        self.tray = None
        self.should_quit = False
        self._exit_complete = threading.Event()
        self._exit_watchdog_started = False

    # ---- HTTP 服务（主线程绑定 + 后台线程服务） ----
    def _bind_server(self, host: str, port: int) -> None:
        """**在主线程**完成端口绑定，失败就把 OSError 抛给 run()。

        此前绑定写在后台线程里（`AppHTTPServer(...)` 构造无 try/except），端口被占时
        异常在子线程里无声死亡：冻结版没有控制台，用户只会看到"托盘在、窗口白屏"。
        挪回主线程后，同一个异常就能变成一行中文提示。
        """
        self.httpd = srv.AppHTTPServer((host, port), srv.RequestHandler, srv.APP)
        self.httpd.daemon_threads = True

    def _serve(self) -> None:
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

    def _abort_startup(self, message: str, lock=None) -> SystemExit:
        """启动期硬失败：弹中文提示 → 清现场 → 返回 SystemExit（调用方 ``raise`` 出去）。

        返回异常而不是自己抛，是为了让调用点写成 ``raise self._abort_startup(...) from exc``
        ——静态分析看得出控制流到此为止，不会被误读成"失败后继续往下启动"。
        """
        _notify_startup_error(message)
        self._stop_server()
        app = getattr(srv, "APP", None)
        if app is not None:
            try:
                app.stop()
            except Exception:
                pass
        try:
            srv.STATUS_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        if lock is not None:
            try:
                lock.close()
            except Exception:
                pass
        return SystemExit(2)

    def _wait_healthy(self, local_url: str) -> str:
        """等本机服务就绪，返回 'ok' / 'foreign' / 'down'。

        ``foreign``＝端口上确实有 HTTP 服务在应答，但**不是本应用**（`/api/health` 的
        `app` 标记对不上）。这种情形必须当场失败：否则窗口会开到别人的应用上，
        用户在陌生界面里打字（旧实现只看 status code，5 秒白等后还照常开窗）。

        健康检查**必须绕过一切代理**（`ProxyHandler({})`）：裸 `urllib.request.urlopen`
        会跟随 Windows 系统代理与 `http_proxy` 环境变量，而代理绕过列表里只写
        「localhost」或「<local>」时**不含 127.0.0.1**（`<local>` 只匹配不带点的名字）。
        2.8.2 真实用户案例：开着代理软件的用户从 2.7.6 升上来就起不来、重启无效——
        回环自检被送进代理后 50 次全失败，应用被判 "down" 拒绝启动。回环请求本来
        就不该经过任何代理，这里必须显式断开。
        """
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        last = "down"
        for _ in range(50):
            try:
                with opener.open(f"{local_url}/api/health", timeout=1) as response:
                    status = int(getattr(response, "status", 0) or 0)
                    body = response.read()
            except urllib.error.HTTPError:
                # 有 HTTP 服务，只是这个路径不健康：端口被别人占着。
                return "foreign"
            except Exception:
                last = "down"
                time.sleep(0.1)
                continue
            if status != 200:
                return "foreign"
            try:
                payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
            except ValueError:
                return "foreign"
            if isinstance(payload, dict) and str(payload.get("app") or "") == srv.HEALTH_APP_MARKER:
                return "ok"
            return "foreign"
        return last

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

    def _build_tray(self, local_url: str, image=None):
        import pystray

        if image is None:
            # 兜底：外部若单独调用本方法（不经过 run() 的解析），自己解析一次。
            image, _ico_path = _resolve_app_icon()

        menu = pystray.Menu(
            pystray.MenuItem("打开窗口", lambda: self._show_window(), default=True),
            pystray.MenuItem("在浏览器打开", self._open_browser(local_url)),
            pystray.MenuItem("退出", self._quit),
        )
        return pystray.Icon("naiba-chat", image, "Cat Chat", menu)

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
        """页面就绪后确认主窗口可见，并挂上"拖文件夹进输入区"的监听。

        更新流程的重启脚本若带上 SW_HIDE（此前 apply-update.ps1 用了
        `-WindowStyle Hidden`），新进程会正常跑起来、托盘图标也在，但主窗口不出来，
        用户只能到托盘双击「打开窗口」才能唤回界面。这里主动 show/restore 一次，
        把这类外部启动方式的隐藏标记抹平。
        """
        self._show_window()
        self._register_drop_listener()

    # ---- 拖入文件夹（桌面壳专属：只有这里拿得到真实绝对路径） ----
    def _on_composer_drop(self, event) -> None:
        """pywebview 的 drop 事件：把拖进来的**目录**绝对路径交回前端。

        只有桌面壳能做这件事：WebView2 通过 `postMessageWithAdditionalObjects` 把 File 对象
        连同真实路径一起送过来，pywebview 把它挂在事件的 `pywebviewFullPath` 上；纯浏览器里
        Chromium 出于安全模型不给绝对路径（见计划 B.2），那条路只能降级。

        普通文件不在这里处理——前端原有的上传链路照旧；这里只挑出**目录**，
        并且"是不是目录"以磁盘真实类型为准（不猜 File.type）。
        """
        files = ((event or {}).get("dataTransfer") or {}).get("files") or []
        paths: list[str] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            full = str(item.get("pywebviewFullPath") or "").strip()
            if not full:
                continue
            try:
                if os.path.isdir(full):
                    paths.append(full)
            except OSError:
                continue
        if not paths or self.window is None:
            _drop_debug_log(
                "dropEvent: files=%d dirs=%d（无目录，交给前端原有链路）"
                % (len(files), len(paths))
            )
            return
        script = f"window.naibaHandleDroppedFolders({json.dumps(paths, ensure_ascii=False)});"
        try:
            self.window.evaluate_js(script)
            _drop_debug_log("dropEvent: 已回交前端 %d 个目录" % len(paths))
        except Exception as exc:
            # 页面可能正在刷新：拿不到就算了，前端有超时降级提示
            _drop_debug_log(f"dropEvent: 回交前端失败 {exc!r}")

    def _register_drop_listener(self) -> None:
        """给输入区挂 drop 监听（pywebview 的 DOM 事件 API）。

        **这一步不只是为了那条事件回调，更是"拖文件夹"整条链路的前置开关**：
        WebView2 收到 `postMessageWithAdditionalObjects('FilesDropped', ...)` 时，
        pywebview 会先看 `_dnd_state['num_listeners']`——**为 0 就整条消息丢掉**，
        真实路径根本不落库。也就是说，这里没挂上，前端再怎么调原生接口都拿不到路径，
        而用户只会看到一句兜底提示。所以这里失败必须留痕（见 `_drop_debug_log`）。

        必须等页面 loaded 之后再挂：`get_element` 走 evaluate_js，页面没加载就没有 DOM。
        每次 loaded 先摘上一次（刷新页面会重建 DOM，但 Python 侧的事件表会累积）。
        """
        try:
            from webview.dom import DOMEventHandler

            node = self.window.dom.get_element(".composer-wrap")
            if node is None:
                _drop_debug_log("dropListener: .composer-wrap 未找到（页面结构变了？）")
                return
            try:
                node.events.drop -= self._on_composer_drop
            except Exception:
                pass  # 首次挂载时本来就没有，pywebview 内部会记一条 warning
            node.events.drop += DOMEventHandler(self._on_composer_drop)
            from webview.dom import _dnd_state

            _drop_debug_log(
                "dropListener: 已挂载 num_listeners=%s" % _dnd_state.get("num_listeners")
            )
        except Exception as exc:
            _drop_debug_log(f"dropListener 挂载失败: {exc!r}")

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

        server_thread = threading.Thread(target=self._serve, name="naiba-http", daemon=True)
        # 绑定在主线程完成：端口被占时这里是唯一能"说话"的地方（见 _bind_server）。
        try:
            self._bind_server(host, port)
        except OSError as exc:
            raise self._abort_startup(
                port_conflict_message(port, host=host, detail=str(exc)), instance_lock
            ) from exc
        srv.write_status(host, port, str(srv.APP.config.data["access_token"]))
        server_thread.start()
        health = self._wait_healthy(local_url)
        if health != "ok":
            # 旧实现这里没有失败分支：5 秒白等之后照常开托盘、开窗口，用户看到的是
            # ERR_CONNECTION_REFUSED（或更糟——开到别人的服务上）。现在直接给中文原因。
            if health == "foreign":
                reason = (
                    f"端口 {port} 上另有服务在应答，但它不是 Cat Chat。\n\n"
                    f"{port_conflict_message(port, host=host)}"
                )
            else:
                # "down"（绑定已成功、但自检连不上）绝不能再拼「无法绑定」那段文案——
                # 2.8.2 用户看到的弹窗因此自相矛盾（绑定成功却提示端口被占），把排查
                # 引向了 netstat。这里的真话是：绑定成功、自检不通，方向是代理/安全软件。
                reason = (
                    f"本机服务在 5 秒内没有就绪（端口 {port}）。\n\n"
                    f"端口绑定本身是成功的（{host}:{port}），但启动自检"
                    f" http://127.0.0.1:{port}/api/health 一直连不上。\n"
                    "常见原因：代理软件接管了 127.0.0.1 的请求，或安全软件拦截了本进程的"
                    "回环访问。请在代理软件中放行/绕过 127.0.0.1，或把 Cat Chat 加入"
                    "安全软件白名单后重试。"
                )
            raise self._abort_startup(reason, instance_lock)

        # 图标只解析一次、托盘与窗口共用（见 _resolve_app_icon：各写一份会出现
        # 「托盘换了、窗口没换」的半截状态）。
        icon_image, icon_path = _resolve_app_icon()
        self.tray = self._build_tray(local_url, icon_image)
        threading.Thread(target=self.tray.run, daemon=True).start()

        self.window = webview.create_window(
            "Cat Chat",
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
        # 启动时不自动查更新：检查只在用户点「检查更新」时发起（`POST /api/update/check`）。
        # 此前这里是 `threading.Timer(4.0, updater.start_check)`，于是用户「还没点检查更新就能
        # 查到新版本」，容易被当成 bug（2026-09-16 用户反馈第三条）。只查不装的语义不变，
        # 只是不再由程序主动发起。
        start_kwargs = {}
        if icon_path:
            start_kwargs["icon"] = icon_path
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
            ctypes.windll.user32.MessageBoxW(0, message, "Cat Chat 启动失败", 0x10)  # MB_ICONERROR
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
