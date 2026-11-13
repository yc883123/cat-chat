"""Recoverable Writer file transactions and byte-preserving manual drafts.

The pending journal is published before any target is replaced. Readers replay
only recorded before/after hashes; an unrelated edit fails closed. File fsync
does not claim a Windows directory-metadata power-loss guarantee.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

JOURNAL_NAME = ".writeback-pending.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def durable_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def _target(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("写回日志路径非法")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root or path.name == JOURNAL_NAME:
        raise ValueError("写回日志路径越界")
    return path


def recover(root: Path) -> bool:
    root = root.resolve()
    journal = root / JOURNAL_NAME
    if not journal.exists():
        return False
    if journal.is_symlink():
        raise ValueError("写回日志不能是符号链接")
    payload = json.loads(journal.read_bytes().decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("files"), list):
        raise ValueError("写回日志损坏")
    entries = []
    seen = set()
    for row in payload["files"]:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            raise ValueError("写回日志条目损坏")
        path = _target(root, row.get("path"))
        if path in seen:
            raise ValueError("写回日志有重复路径")
        seen.add(path)
        data = row["text"].encode("utf-8")
        if digest(data) != row.get("after"):
            raise ValueError("写回日志校验失败")
        current = digest(path.read_bytes()) if path.exists() else None
        if current not in (row.get("before"), row["after"]):
            raise ValueError("写回期间文件被其他操作修改，已保留日志并停止恢复")
        entries.append((path, data, current == row["after"]))
    # Validate every target before applying any; publish the book/pipeline last.
    entries.sort(key=lambda entry: entry[0] == root / "book.json")
    for path, data, already_written in entries:
        if not already_written:
            durable_write(path, data)
    journal.unlink()
    return True


def commit(root: Path, writes: dict[Path, dict[str, Any]]) -> None:
    root = root.resolve()
    files = []
    for path, payload in writes.items():
        path = path.resolve()
        checked = _target(root, path.relative_to(root).as_posix())
        data = json_bytes(payload)
        files.append({"path": checked.relative_to(root).as_posix(),
                      "before": digest(checked.read_bytes()) if checked.exists() else None,
                      "after": digest(data), "text": data.decode("utf-8")})
    if not files:
        return
    durable_write(root / JOURNAL_NAME, json_bytes({"version": 1, "files": files}))
    recover(root)
