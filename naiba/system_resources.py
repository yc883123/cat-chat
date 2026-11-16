# -*- coding: utf-8 -*-
"""Host RAM/VRAM snapshots, sampled on request without background workers."""

from __future__ import annotations

import copy
import csv
import ctypes
import io
import os
import subprocess
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_CACHE_SECONDS = 3.0
_GPU_TIMEOUT_SECONDS = 2.0
_MIB = 1024 * 1024


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _read_memory() -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("unsupported_platform")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel.GlobalMemoryStatusEx
    query.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
    query.restype = ctypes.c_int
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not query(ctypes.byref(status)):
        raise OSError("memory_query_failed")
    total, available = int(status.ullTotalPhys), int(status.ullAvailPhys)
    if total <= 0 or not 0 <= available <= total:
        raise ValueError("invalid_memory_data")
    used = total - available
    return {
        "available": True,
        "total_bytes": total,
        "used_bytes": used,
        "percent": round(used * 100 / total, 1),
    }


def _find_nvidia_smi() -> str | None:
    """Resolve known driver locations and absolute PATH entries; never search cwd."""
    filename = "nvidia-smi.exe" if os.name == "nt" else "nvidia-smi"
    directories: list[Path] = []
    if os.name == "nt":
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            query = kernel.GetSystemDirectoryW
            query.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            query.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32768)
            length = query(buffer, len(buffer))
            if 0 < length < len(buffer):
                directories.append(Path(buffer.value))
        except (OSError, AttributeError):
            pass
        for key in ("SystemRoot", "WINDIR"):
            root = os.environ.get(key)
            if root:
                directories.append(Path(root) / "System32")
        for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(key)
            if root:
                directories.append(Path(root) / "NVIDIA Corporation" / "NVSMI")
    directories.extend(
        Path(entry.strip('"'))
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry
    )
    for directory in directories:
        if not directory.is_absolute():
            continue
        candidate = directory / filename
        try:
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate.resolve(strict=True))
        except OSError:
            continue
    return None


def _parse_gpus(text: str) -> list[dict[str, Any]]:
    gpus: list[dict[str, Any]] = []
    indices: set[int] = set()
    for row in csv.reader(io.StringIO(text), skipinitialspace=True, strict=True):
        if not row or all(not value.strip() for value in row):
            continue
        if len(row) != 4:
            raise ValueError("invalid_gpu_data")
        index_text, name, total_text, used_text = (value.strip() for value in row)
        if not index_text.isascii() or not index_text.isdecimal() or len(index_text) > 6:
            raise ValueError("invalid_gpu_index")
        index = int(index_text)
        if index in indices or not name or len(name) > 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
            raise ValueError("invalid_gpu_identity")
        try:
            total_mib, used_mib = Decimal(total_text), Decimal(used_text)
            if (
                not total_mib.is_finite() or not used_mib.is_finite()
                or not 0 < total_mib <= 2**40 or not 0 <= used_mib <= total_mib
            ):
                raise ValueError("invalid_gpu_memory")
            total, used = int(total_mib * _MIB), int(used_mib * _MIB)
            if total <= 0:
                raise ValueError("invalid_gpu_memory")
        except InvalidOperation as exc:
            raise ValueError("invalid_gpu_memory") from exc
        indices.add(index)
        gpus.append({
            "index": index,
            "name": name,
            "total_bytes": total,
            "used_bytes": used,
            "percent": round(used * 100 / total, 1),
        })
    return gpus


def _read_gpus() -> tuple[list[dict[str, Any]], str, str]:
    executable = _find_nvidia_smi()
    if executable is None:
        return [], "", "gpu_not_found"
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [executable, "--query-gpu=index,name,memory.total,memory.used", "--format=csv,noheader,nounits"],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=_GPU_TIMEOUT_SECONDS,
            check=False,
            **options,
        )
    except subprocess.TimeoutExpired:
        return [], "gpu_timeout", ""
    except (OSError, subprocess.SubprocessError):
        return [], "gpu_query_failed", ""
    if result.returncode != 0:
        return [], "gpu_query_failed", ""
    try:
        gpus = _parse_gpus(result.stdout)
    except (ValueError, csv.Error):
        return [], "gpu_data_invalid", ""
    return gpus, "", "" if gpus else "gpu_no_devices"


def _sample() -> dict[str, Any]:
    errors: list[str] = []
    memory: dict[str, Any] = {
        "available": False, "total_bytes": None, "used_bytes": None, "percent": None,
    }
    try:
        memory = _read_memory()
    except Exception:  # Windows API failure must not hide usable GPU statistics.
        errors.append("memory_unavailable")
    try:
        gpus, gpu_error, reason = _read_gpus()
    except Exception:  # Discovery errors are also cached and never expose paths.
        gpus, gpu_error, reason = [], "gpu_query_failed", ""
    if gpu_error:
        errors.append(gpu_error)
    return {
        "source": "host",
        "sampled_at": int(time.time() * 1000),
        "memory": memory,
        "gpus": gpus,
        "gpu_available": bool(gpus),
        "error": ";".join(errors),
        "reason": reason,
    }


class SystemResources:
    """One collector per app; concurrent cold requests share a bounded GPU query.

    Refresh runs in its caller. Other callers return the previous snapshot while
    refreshing; with no snapshot yet they wait for that same first sample.
    Failures use the same three-second TTL as successful samples.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._snapshot: dict[str, Any] | None = None
        self._expires_at = 0.0
        self._sampling = False

    def get(self) -> dict[str, Any]:
        with self._condition:
            while self._sampling and self._snapshot is None:
                self._condition.wait()
            if self._snapshot is not None and (self._sampling or time.monotonic() < self._expires_at):
                return copy.deepcopy(self._snapshot)
            self._sampling = True
        snapshot = None
        try:
            snapshot = _sample()
        finally:
            with self._condition:
                # Always release first-sample waiters, even if interrupted.
                if snapshot is not None:
                    self._snapshot = snapshot
                    self._expires_at = time.monotonic() + _CACHE_SECONDS
                self._sampling = False
                self._condition.notify_all()
        return copy.deepcopy(snapshot)
