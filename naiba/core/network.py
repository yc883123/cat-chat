# -*- coding: utf-8 -*-
"""局域网地址探测与网络访问状态（层级 1，标准库）。"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any


def _is_usable_lan_ipv4(address: str) -> bool:
    """Return whether an address can be presented as a LAN entry point."""
    try:
        parsed = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    # Do not offer loopback, APIPA, benchmarking, multicast, or unspecified
    # adapter addresses as phone-access URLs.
    return (
        parsed.is_private
        and not parsed.is_loopback
        and not parsed.is_link_local
        and parsed not in ipaddress.IPv4Network("198.18.0.0/15")
        and not parsed.is_multicast
        and not parsed.is_unspecified
    )


def get_lan_ip() -> str | None:
    """Find the LAN address selected by the current default IPv4 route.

    Connecting a UDP socket does not send traffic, but lets the OS choose the
    outbound interface. This makes the default-route address win over VPNs and
    virtual adapters. Hostname addresses are only a fallback for offline LANs.
    """
    candidates: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return next((address for address in dict.fromkeys(candidates) if _is_usable_lan_ipv4(address)), None)


def suggest_free_port(start: Any, tries: int = 10) -> int:
    """给"端口被占用"弹窗提供一个大概率空闲的建议值（§九.126）。

    从 ``start + 1`` 起逐个端口用**临时裸 socket** 试探：能 ``bind`` 上就说明当前
    没人监听。**只 bind 不 listen、随后立刻 close**，因此不发一个字节、不产生
    TIME_WAIT，也不会干扰任何已有连接（绑不上就是绑不上，探针本身无副作用）。

    全忙时回落到 ``start + 1``：建议值只是弹窗的初始值，最终端口永远由用户显式确认
    （§九.121 的"不做自动换端口"决定不变）。

    刻意**不** import ``naiba.http``（本模块是层级 1）：那里的 ``_port_has_listener``
    要发一次 TCP 连接去验身份，属于 HTTP 层的事；这里只需要"能不能绑上"。
    """
    try:
        base = int(start)
    except (TypeError, ValueError):
        base = 0
    if not 1 <= base <= 65535:
        base = 8765
    fallback = base + 1 if base < 65535 else base
    try:
        attempts = max(1, int(tries))
    except (TypeError, ValueError):
        attempts = 1
    for offset in range(1, attempts + 1):
        candidate = base + offset
        if candidate > 65535:
            break
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", candidate))
        except OSError:
            continue
        finally:
            probe.close()
        return candidate
    return fallback


def port_conflict_message(port: Any, *, host: str = "", detail: str = "") -> str:
    """端口无法绑定时的中文指引（桌面弹窗与 CLI 共用**同一份文案**）。

    为什么要有这个函数：8765 被别的程序占用时，GUI 版曾"静默失败"——绑定在后台线程里
    抛异常、线程无声死亡，托盘照出、窗口开到 `ERR_CONNECTION_REFUSED`，用户只看到
    "软件坏了"。用户能自己解决这件事（关掉占用者或改 config.json 的 port），前提是
    **有人告诉他**；文案里必须给出可复制的自查命令与两种处理办法，缺一不可。

    ``detail`` 透传底层异常摘要（含 WinError 10048 之类），便于对照排查。
    """
    lines = [
        f"Cat Chat 启动失败：端口 {port} 无法绑定（可能已被其它程序占用）。",
    ]
    if host:
        lines.append(f"绑定地址：{host}:{port}")
    if detail:
        lines.append(f"系统返回：{detail}")
    lines += [
        "",
        "先查是谁占着这个端口：",
        f"  netstat -ano | findstr :{port}",
        '  tasklist /FI "PID eq <上一步最后一列的 PID>"',
        "",
        "两种处理办法（任选一种）：",
        "1) 关掉占用该端口的程序（也可能是没退干净的上一份 Cat Chat），再重新启动；",
        '2) 换个端口：编辑 config.json，把 "port" 改成别的值（例如 8766），保存后重启。',
        "   · 安装版：%LOCALAPPDATA%\\NaibaChat\\config.json",
        "   · 源码版：项目目录下 config.json（也可用 python server.py --port 8800）",
        "   注意：换端口后手机/局域网的访问地址与防火墙放行规则里的端口要一起改。",
    ]
    return "\n".join(lines)


def network_access_status(host: str, port: int) -> dict[str, Any]:
    """Describe the active listener and the only LAN URL safe to present."""
    normalized_host = str(host or "").strip()
    local_url = f"http://127.0.0.1:{port}"
    try:
        bound_ip = ipaddress.ip_address(normalized_host)
    except ValueError:
        bound_ip = None

    lan_enabled = normalized_host in {"0.0.0.0", "::"}
    if isinstance(bound_ip, ipaddress.IPv4Address) and normalized_host != "0.0.0.0":
        lan_enabled = _is_usable_lan_ipv4(str(bound_ip))

    if not lan_enabled:
        return {
            "lan_enabled": False,
            "lan_url": "",
            "lan_reason": "当前服务仅允许本机访问。启用手机访问后重启即可使用。",
            "local_url": local_url,
        }

    lan_ip = str(bound_ip) if isinstance(bound_ip, ipaddress.IPv4Address) and _is_usable_lan_ipv4(str(bound_ip)) else get_lan_ip()
    if not lan_ip:
        return {
            "lan_enabled": False,
            "lan_url": "",
            "lan_reason": "未检测到可用于局域网访问的 IPv4 地址。请连接网络后重试。",
            "local_url": local_url,
        }
    return {
        "lan_enabled": True,
        "lan_url": f"http://{lan_ip}:{port}",
        "lan_reason": "",
        "local_url": local_url,
    }


