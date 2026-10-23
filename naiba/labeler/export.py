"""打标结果导出：只基于数据库记录生成受管产物，不接受客户端写入路径。

落点固定为 ``data_dir/labeler/exports/``，前端用 ``/api/file?path=…&download=1`` 下载。
- JSON：含置信度（原始结构）；
- CSV：扁平一行一图，UTF-8 with BOM（Excel 直接打开不乱码）；
- TXT：一行一图，``相对路径, 标签1, 标签2, …``。

CSV 公式注入防护：任何以 ``= + - @`` 或制表/回车开头的单元格前面补一个单引号，
避免用表格软件打开时被当成公式执行。
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Any

FORMATS = ("json", "csv", "txt")
_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")


class LabelerExportError(ValueError):
    """导出参数非法。"""


def _cell(value: Any) -> str:
    text = str(value if value is not None else "")
    if text[:1] in _INJECTION_PREFIXES:
        return "'" + text
    return text


def _stamp(ms: Any) -> str:
    try:
        seconds = int(ms) / 1000
    except (TypeError, ValueError):
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


def _safe_name(raw: str, fallback: str = "labeler") -> str:
    text = _SAFE_NAME_RE.sub("_", str(raw or "")).strip("_")
    return (text or fallback)[:60]


def render(records: list[dict[str, Any]], fmt: str) -> bytes:
    fmt = str(fmt or "").strip().lower()
    if fmt not in FORMATS:
        raise LabelerExportError("导出格式必须是 json / csv / txt")
    if fmt == "json":
        rows = [
            {
                "name": row.get("name") or "",
                "relative_path": row.get("relative_path") or "",
                "tags": row.get("tags") or [],
                "confidences": row.get("confidences") or [],
                "description": row.get("description") or "",
                "preset": row.get("preset") or "",
                "output_mode": row.get("output_mode") or "",
                "model_name": row.get("model_name") or "",
                "created_at": row.get("created_at") or 0,
            }
            for row in records
        ]
        return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow([
            "relative_path", "name", "tags", "confidences",
            "description", "preset", "output_mode", "model_name", "created_at",
        ])
        for row in records:
            writer.writerow([
                _cell(row.get("relative_path") or ""),
                _cell(row.get("name") or ""),
                _cell(", ".join(str(tag) for tag in (row.get("tags") or []))),
                _cell(",".join(str(conf) for conf in (row.get("confidences") or []))),
                _cell(row.get("description") or ""),
                _cell(row.get("preset") or ""),
                _cell(row.get("output_mode") or ""),
                _cell(row.get("model_name") or ""),
                _cell(_stamp(row.get("created_at"))),
            ])
        # BOM：Excel 双击打开时按 UTF-8 解析，中文不乱码。
        return ("\ufeff" + buffer.getvalue()).encode("utf-8")
    lines: list[str] = []
    for row in records:
        tags = ", ".join(str(tag) for tag in (row.get("tags") or []))
        name = str(row.get("relative_path") or row.get("name") or "")
        lines.append(f"{name}, {tags}" if tags else name)
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def write_export(exports_dir: Path, records: list[dict[str, Any]], fmt: str,
                 label: str = "") -> dict[str, Any]:
    """把记录写成受管导出文件并返回元信息（路径只在本机 data 目录内）。"""
    payload = render(records, fmt)
    fmt = fmt.lower()
    exports_dir = Path(exports_dir)
    exports_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(label, "labeler")
    name = f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.{fmt}"
    path = exports_dir / name
    # 同名（同一秒内重复点导出）：补序号，绝不覆盖上一个产物。
    counter = 1
    while path.exists():
        name = f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}-{counter}.{fmt}"
        path = exports_dir / name
        counter += 1
    tmp = path.with_name(f".{name}.tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)
    return {"name": name, "path": str(path), "format": fmt, "count": len(records),
            "bytes": len(payload)}
