"""打标结果导出：只基于数据库记录生成受管产物，不接受客户端写入路径。

落点固定为 ``data_dir/labeler/exports/``，前端用 ``/api/file?path=…&download=1`` 下载。
- JSON：含置信度（原始结构）；
- CSV：扁平一行一图，UTF-8 with BOM（Excel 直接打开不乱码）；
- TXT：一行一图，``相对路径, 标签1, 标签2, …``；
- captions：逐图 ``.txt`` 打包（只有 caption，不含图片）；
- lora：**LoRA 训练包**——按留存的原图字节逐张导出 ``000001.png`` + 同主干
  ``000001.txt`` 配对，附 manifest（原始名称/相对路径/导出名/SHA-256 映射）。
  任何记录缺原图、同一样本出现多条、原图字节对不上时**整包拒绝**，不生成半包。

CSV 公式注入防护：任何以 ``= + - @`` 或制表/回车开头的单元格前面补一个单引号，
避免用表格软件打开时被当成公式执行。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from naiba.labeler.originals import normalize_digest, original_path

FORMATS = ("json", "csv", "txt", "captions", "lora")
_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")
_PACKAGE_EXT_RE = re.compile(r"^\.[a-z0-9]{2,8}$")


def _open_folder(target: Path) -> None:
    """用系统文件管理器打开目录（单拆出来便于测试打桩）。"""
    if sys.platform.startswith("win"):
        os.startfile(str(target))  # type: ignore[attr-defined]  # 仅 Windows 有
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def open_export_dir(exports_dir: Path) -> str:
    """打开受管导出目录并返回实际路径；目录不存在时先建出来。

    路径完全由服务端决定（``data_dir/labeler/exports``），不接受客户端传入。
    """
    target = Path(exports_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    _open_folder(target)
    return str(target)


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


def _folder_part(records: list[dict[str, Any]]) -> str:
    """导出包的自动命名：文件夹本来的名字（上传时选中的那个文件夹）。

    打标批次来自一个文件夹，包名直接用它，导出目录里多个包才能一眼
    分清谁是谁的数据集（否则只剩任务号和时间戳）。

    - 全部记录共享同一个首段目录（即浏览器整包选择文件夹时
      ``relative_path`` 的第一段）：就用这个名字，内部子目录不影响；
    - 记录来自多个不同的首段目录（散选）：没有共同文件夹名，返回空串，
      回落到「标注-时间戳」的老命名；
    - 顶层散图（relative_path 没有目录段）同样返回空串。
    """
    first_segments: set[str] = set()
    has_top_level = False
    for row in records:
        raw = str(row.get("relative_path") or row.get("name") or "").replace("\\", "/")
        parts = [part for part in raw.split("/") if part not in ("", ".", "..")]
        parts = [part for part in parts if not re.match(r"^[A-Za-z]:$", part)]
        if len(parts) <= 1:
            has_top_level = True  # 文件直接在所选层，没有文件夹段
        else:
            first_segments.add(parts[0])
    if has_top_level or len(first_segments) != 1:
        return ""
    return _safe_name(next(iter(first_segments)), "")


def _caption_text(row: dict[str, Any]) -> str:
    """逐图 txt 的内容：优先标签（训练常用），没有标签才用描述。"""
    tags = ", ".join(str(tag) for tag in (row.get("tags") or []) if str(tag).strip())
    text = tags or str(row.get("description") or "").strip()
    # 一行一条 caption：换行会破坏「图片同名 txt」的一图一文件约定。
    return re.sub(r"\s*\r?\n\s*", " ", text).strip()


def _caption_member(row: dict[str, Any]) -> str:
    """把图片的相对路径换成同名 .txt（保留子目录，避免清空结构后重名）。"""
    raw = str(row.get("relative_path") or row.get("name") or "").replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in ("", ".", "..")]
    parts = [part for part in parts if not re.match(r"^[A-Za-z]:$", part)]
    if not parts:
        return "image.txt"
    parts[-1] = Path(parts[-1]).stem + ".txt"
    return "/".join(parts)


def _render_captions(records: list[dict[str, Any]]) -> bytes:
    """逐图 .txt 打成 zip：解压后每个 txt 与图片同名同目录，可直接用于 LoRA 训练。"""
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in records:
            member = _caption_member(row)
            if member in used:
                # 同目录同名图片（极少见）：补序号，绝不覆盖前一条 caption。
                stem, suffix = member.rsplit(".", 1)
                counter = 2
                while f"{stem}-{counter}.{suffix}" in used:
                    counter += 1
                member = f"{stem}-{counter}.{suffix}"
            used.add(member)
            archive.writestr(member, _caption_text(row))
    return buffer.getvalue()


# ---- LoRA 训练包（lora）：图片 + 同主干 .txt + manifest，整包成功或整包拒绝 ----


def _record_label(row: dict[str, Any]) -> str:
    text = str(row.get("relative_path") or row.get("name") or "").strip()
    if not text:
        text = f"标注#{row.get('id') or '?'}"
    return text


def _caption_source(row: dict[str, Any]) -> str:
    if any(str(tag or "").strip() for tag in (row.get("tags") or [])):
        return "tags"
    if str(row.get("description") or "").strip():
        return "description"
    return ""


def _package_extension(row: dict[str, Any]) -> str:
    """导出图片的扩展名：跟原图片走（来源相对路径 → 原名 → 服务端校验过的缓存名）。"""
    for key in ("relative_path", "name", "path"):
        suffix = Path(str(row.get(key) or "")).suffix.lower()
        if _PACKAGE_EXT_RE.match(suffix):
            return suffix
    return ".png"


def _resolve_original_bytes(data_dir: Path, row: dict[str, Any]) -> bytes:
    """取这条记录的原图字节：留存池优先；缓存图只有在字节校验一致时才可用。

    缩放/压缩后的缓存图摘要必然对不上记录的原图摘要——那种「看似有图」的
    回退正是训练包要拦的，命中不了就整包拒绝。
    """
    digest = normalize_digest(row.get("original_sha256"))
    if not digest:
        raise LabelerExportError(
            f"选中的标注里，「{_record_label(row)}」没有原图记录（旧版本打标的结果），"
            "无法生成训练包；请用当前版本重新打标后再导出"
        )
    data: bytes | None = None
    archived = original_path(data_dir, digest)
    if archived is not None:
        try:
            data = archived.read_bytes()
        except OSError:
            data = None
    if data is None:
        cached = Path(str(row.get("path") or ""))
        try:
            candidate = cached.read_bytes()
            if hashlib.sha256(candidate).hexdigest() == digest:
                data = candidate
        except OSError:
            data = None
    if data is None or hashlib.sha256(data).hexdigest() != digest:
        raise LabelerExportError(
            f"选中的标注里，「{_record_label(row)}」的原图文件已缺失或对不上"
            "（上传缓存可能被清理或缩放过），无法生成训练包"
        )
    return data


def render_lora_package(records: list[dict[str, Any]], data_dir: Path) -> bytes:
    """把标注记录打包成 LoRA 训练包（图片 + 同主干 .txt + manifest）。

    - 短名：按 ``relative_path, 标注 id`` 固定排序后从 1 递增（``000001.png``），
      同一批输入无论导出几次命名都一致；
    - 配对：图片与 .txt 共用短名主干，caption 优先标签、无标签用描述；
    - manifest：逐条记录原始名称、相对路径、导出名、原图 SHA-256 与标注归属；
    - 拒绝条件：同一样本（path_key + content_version）出现多条、记录没有原图
      摘要、原图字节取不到或校验不一致——任何一条命中都不写任何文件。
    """
    rows = sorted(
        records,
        key=lambda row: (str(row.get("relative_path") or ""), int(row.get("id") or 0)),
    )
    legacy = [row for row in rows if not normalize_digest(row.get("original_sha256"))]
    if legacy:
        raise LabelerExportError(
            f"选中的 {len(rows)} 条标注里有 {len(legacy)} 条没有原图记录"
            f"（例如「{_record_label(legacy[0])}」，旧版本打标的结果），"
            "无法生成训练包；请用当前版本重新打标后再导出"
        )
    seen_samples: set[tuple[str, str]] = set()
    for row in rows:
        sample = (str(row.get("path_key") or ""), str(row.get("content_version") or ""))
        if sample in seen_samples:
            raise LabelerExportError(
                f"选中的标注里，「{_record_label(row)}」对应的同一张图片有多条记录；"
                "训练包要求每个样本只出现一次，请按任务或按记录缩小选择范围后重试"
            )
        seen_samples.add(sample)
    entries: list[dict[str, Any]] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, row in enumerate(rows, 1):
            data = _resolve_original_bytes(Path(data_dir), row)
            stem = f"{index:06d}"
            image_name = f"{stem}{_package_extension(row)}"
            archive.writestr(image_name, data)
            archive.writestr(f"{stem}.txt", _caption_text(row))
            entries.append({
                "index": index,
                "image": image_name,
                "caption": f"{stem}.txt",
                "relative_path": str(row.get("relative_path") or ""),
                "name": str(row.get("name") or ""),
                "annotation_id": int(row.get("id") or 0),
                "source_task_id": str(row.get("source_task_id") or ""),
                "path_key": str(row.get("path_key") or ""),
                "content_version": str(row.get("content_version") or ""),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "caption_source": _caption_source(row),
            })
        manifest = {
            "type": "labeler-lora-package",
            "created_at": int(time.time() * 1000),
            "count": len(entries),
            "entries": entries,
        }
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    return buffer.getvalue()


def render(records: list[dict[str, Any]], fmt: str, data_dir: Path | None = None) -> bytes:
    fmt = str(fmt or "").strip().lower()
    if fmt not in FORMATS:
        raise LabelerExportError("导出格式必须是 json / csv / txt / captions / lora")
    if fmt == "captions":
        return _render_captions(records)
    if fmt == "lora":
        if data_dir is None:
            raise LabelerExportError("训练包导出缺少数据目录（内部参数错误）")
        return render_lora_package(records, Path(data_dir))
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
                 label: str = "", data_dir: Path | None = None,
                 custom_name: str = "") -> dict[str, Any]:
    """把记录写成受管导出文件并返回元信息（路径只在本机 data 目录内）。

    命名优先级：用户填了包名就叫用户的名字（撞名补序号，不覆盖）；没填
    就自动命名——记录来自同一个文件夹时直接用**文件夹本来的名字**，取不到
    文件夹名（散选/顶层散图）才回落到「标注-时间戳」的老命名。lora 格式在
    自动命名时保留 ``lora-`` 前缀，和同文件夹的逐图 .txt 包区分开。
    """
    payload = render(records, fmt, data_dir=data_dir)
    fmt = fmt.lower()
    exports_dir = Path(exports_dir)
    exports_dir.mkdir(parents=True, exist_ok=True)
    # 逐图 txt 与训练包都是 zip 容器；其余格式与扩展名同名。
    extension = "zip" if fmt in ("captions", "lora") else fmt
    # 用户可能顺手把扩展名一起填进来：去掉尾缀，统一由格式决定扩展名。
    stem = _safe_name(re.sub(r"\.[A-Za-z0-9]{2,8}$", "", custom_name or "").strip(), "")
    if stem:
        # 用户命名：就叫这个名字；撞名补序号，绝不覆盖上一个产物。
        name = f"{stem}.{extension}"
        counter = 2  # 从 -2 起：和浏览器「名字 (2)」的重复体验一致
        path = exports_dir / name
        while path.exists():
            name = f"{stem}-{counter}.{extension}"
            path = exports_dir / name
            counter += 1
    else:
        stem = _safe_name(label, "labeler")
        folder_part = _folder_part(records)
        if folder_part:
            # 自动命名 = 文件夹本来的名字；重复导出同一文件夹补序号区分。
            stem = folder_part
        if fmt == "lora":
            # 训练包在导出目录里一眼可辨：固定 lora- 前缀。
            stem = f"lora-{stem}"
        if folder_part:
            name = f"{stem}.{extension}"
            counter = 2
            path = exports_dir / name
            while path.exists():
                name = f"{stem}-{counter}.{extension}"
                path = exports_dir / name
                counter += 1
        else:
            name = f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.{extension}"
            path = exports_dir / name
            # 同名（同一秒内重复点导出）：补序号，绝不覆盖上一个产物。
            counter = 1
            while path.exists():
                name = f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}-{counter}.{extension}"
                path = exports_dir / name
                counter += 1
    tmp = path.with_name(f".{name}.tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)
    return {"name": name, "path": str(path), "format": fmt, "count": len(records),
            "bytes": len(payload)}
