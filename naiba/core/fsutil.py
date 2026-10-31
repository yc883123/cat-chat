# -*- coding: utf-8 -*-
"""落盘原子写：同目录临时文件 + `os.replace`，替换被占用时短暂重试。

为什么重试：Windows 上 `os.replace` 对**正被打开的目标文件**会抛
`PermissionError [WinError 5]`（本服务自己的轮询读、杀毒软件/索引器都可能
恰好握着句柄），这类占用都是毫秒级的；直接失败会让一次正常的生成任务整单
报错（远程实例实测过）。给定上限内按固定间隔重试，耗尽仍失败才原样抛出。
非 Windows 没有这个失败模式（POSIX 替换打开中的文件是合法的），不重试。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

# 5=拒绝访问（目标被无 FILE_SHARE_DELETE 的句柄打开）、32=共享冲突、33=锁定。
_RETRY_WINERRORS = frozenset({5, 32, 33})
ATOMIC_ATTEMPTS = 8
ATOMIC_DELAY_SECONDS = 0.03


def _replace_with_retry(tmp_path: Path, path: Path, *, attempts: int = ATOMIC_ATTEMPTS,
                        delay: float = ATOMIC_DELAY_SECONDS) -> None:
    for attempt in range(max(1, attempts)):
        try:
            os.replace(tmp_path, path)
            return
        except OSError as exc:
            transient = getattr(exc, "winerror", None) in _RETRY_WINERRORS
            last = attempt + 1 >= max(1, attempts)
            if not transient or last:
                raise
            time.sleep(delay)


def _atomic_write(path: Path, data: str | bytes, *, attempts: int = ATOMIC_ATTEMPTS,
                  delay: float = ATOMIC_DELAY_SECONDS) -> None:
    """先写同目录隐藏临时文件再原子替换；失败不动已有内容，临时文件清理干净。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        if isinstance(data, bytes):
            tmp_path.write_bytes(data)
        else:
            tmp_path.write_text(data, encoding="utf-8")
        _replace_with_retry(tmp_path, path, attempts=attempts, delay=delay)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path, payload: dict[str, Any], *, attempts: int = ATOMIC_ATTEMPTS,
                      delay: float = ATOMIC_DELAY_SECONDS) -> None:
    """JSON 落盘（ensure_ascii=False，indent=1，与各域存储既有格式一致）。"""
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=1), attempts=attempts, delay=delay)


def atomic_write_bytes(path: Path, data: bytes, *, attempts: int = ATOMIC_ATTEMPTS,
                       delay: float = ATOMIC_DELAY_SECONDS) -> None:
    _atomic_write(path, data, attempts=attempts, delay=delay)
