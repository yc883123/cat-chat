# -*- coding: utf-8 -*-
"""媒体采集器：在**工具产出的那一刻**按工具声明提取媒体并托管缓存。

设计要点（与维护说明 §3.2 分层、§九.9/§九.17 教训一致）：

- **唯一生产点**：工具原始 result 只在 ``skills/agent.py`` 构造 run 时可见
  （``display_tool_run`` 已按工具脱敏——`vision_analyze` 装载形态的 path/thumb 会被剥掉）。
  因此采集必须在这里发生，而不是等轮末扫事件/扫文本；取消与失败路径也从事件里
  带回同一份记录，不再依赖"重新扫一遍原始结果"（那两条路径拿不到原始结果）。
- **声明驱动**：是否提取、怎么提取、是否显示由 ``ToolSpec.metadata["media"]``
  （``core/media_types.py`` 契约、``tools/registry.py::MEDIA_DECLARATIONS`` 表）决定；
  不再按工具名硬编码集合、不再无条件正则扫描。
- **先截断再落盘**：候选按类型分桶（图 20 / 视频 8 / 音频 8）预截断后才复制/下载
  并生成缩略图——否则"列一次目录"会把几百张图拷进 data 目录并生成几百张缩略图卡住
  （超限部分向前端标注"共 N 张，仅显示前 M 张"，不静默）。
- **只对成功结果提取**：失败/待确认（``NEED_CONFIRM:``）的结果里出现的路径不是产物
  （错误信息常含路径），旧实现会把它们渲染成指向不存在文件的破图卡片。
- 不捕获可变配置：data_dir/imaging 每次调用经 config 实时解析（不闭包捕获装配期值）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from naiba import net as net_io
from naiba.core.media_types import (
    MEDIA_BUCKET_LIMITS,
    media_kind_of,
    truncate_by_kind,
)
from naiba.core.paths import path_within
from naiba.storage.media import _ensure_webp_thumb

logger = logging.getLogger("naiba.storage.media_collect")

# 结构化媒体记录里存放"真实路径/URL"的键（识别到即产出单个附件，并携带其
# thumb/name 元数据，随后停止递归，避免把 name/thumb_path 当成独立附件再扫一遍）。
_SOURCE_KEYS = ("path", "source", "url", "view_url", "file")
_THUMB_KEYS = ("thumb_path", "thumbnail", "thumb_url")
_NAME_KEYS = ("name", "filename")
# 纯文本结果里的路径兜底（盘符路径与 URL）；截断尾部标点。
_TEXT_PATH_RE = re.compile(r"(?:[A-Za-z]:\\[^\r\n\"']+|https?://[^\s\"']+)")
# 路径在中文/全角散文里会黏上后缀（write_file 返回「已写入 C:\a.png（12 字符）」，
# 旧实现在这里判失败 → 写出的图片既无"修改文件"chip、也无媒体卡，产物彻底不可见）。
# 截断点取首个散文终止符；**不含** `:`（盘符/URL 用）与空格（路径可含空格）。
_PROSE_TERMINATORS = "（(，,。；;！!？?、）)】]}\"'“”‘’<>"
# CJK 字符在 URL 里必然被百分号编码，出现即说明 URL 结束（"见 https://x/a.png，谢谢"）。
_CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")

EMPTY_RESULT: dict[str, Any] = {"media": [], "truncated": None}


class MediaCollector:
    """按声明提取工具产物媒体（宿主托管缓存），返回可内嵌进会话记录的小型记录。"""

    def __init__(self, config: Any, paths: Any = None, on_cached: Any = None) -> None:
        # config 只作为"取数入口"保存：data_dir / imaging 每次调用实时解析，
        # 避免闭包捕获装配期可变状态（教训 9/17）。
        self._config = config
        self._paths = paths
        # on_cached：**产物真正落进 data/generated 之后**调用（宿主据此触发 generated
        # 目录自己的后台清理）。generated 此前没有独立触发点，只能蹭"上传"便车——
        # 只生成不上传的场景（纯 Job / 工具出图）再大也永不清。回调由宿主注入，
        # 这里只负责"写盘成功就通知一次"，不关心它做什么；回调异常不得打断采集。
        self._on_cached = on_cached

    # ---- 对外唯一入口 ----
    def collect(
        self,
        run: dict[str, Any],
        declaration: dict[str, str],
        *,
        intent: bool = False,
    ) -> dict[str, Any]:
        """从一次工具调用的原始 run 提取媒体。

        返回 ``{"media": [record, ...], "truncated": {...} | None}``；
        record = ``{kind, name, source, thumb_path}``（仅路径/元数据，不含字节）。
        """
        policy = str((declaration or {}).get("policy") or "inline")
        extract = str((declaration or {}).get("extract") or "scan")
        if extract == "none" or policy == "never":
            return dict(EMPTY_RESULT)
        if not bool((run or {}).get("success")):
            return dict(EMPTY_RESULT)
        text = str((run or {}).get("result") or "")
        if not text or text.startswith("NEED_CONFIRM:"):
            return dict(EMPTY_RESULT)
        if policy == "intent_gated" and not intent:
            return dict(EMPTY_RESULT)

        candidates = _candidates_from_result(text, extract, tool=str((run or {}).get("tool") or ""))
        if not candidates:
            return dict(EMPTY_RESULT)
        deduped = _dedupe_candidates(candidates)
        kept, truncated = truncate_by_kind(deduped)
        data_dir = Path(self._config.resolve_data_dir()).resolve()
        imaging = dict((self._config.data or {}).get("imaging") or {})
        records = [
            record
            for candidate in kept
            if (record := self._materialize(candidate, data_dir, imaging)) is not None
        ]
        return {"media": _dedupe_records(records), "truncated": truncated}

    # ---- 落盘：把候选变成"可服务、可展示"的记录 ----
    def _materialize(
        self, candidate: dict[str, str], data_dir: Path, imaging: dict[str, Any]
    ) -> dict[str, Any] | None:
        source = str(candidate.get("source") or "")
        kind = media_kind_of(source) or media_kind_of(candidate.get("name"))
        if not kind:
            return None
        parsed = urllib.parse.urlparse(source)
        query = urllib.parse.parse_qs(parsed.query)
        local_path = Path(source).expanduser()
        try:
            is_local_file = local_path.is_file()
        except OSError as exc:  # 路径非法（超长/权限）：当作非本地文件，保留 URL 形态
            logger.warning("媒体候选路径无法访问：source=%s error=%s", source, exc)
            is_local_file = False
        name = (
            str(candidate.get("name") or "")
            or (local_path.name if is_local_file else Path((query.get("filename") or [parsed.path])[0]).name)
            or "生成结果"
        )
        thumb_path = str(candidate.get("thumb_path") or "")
        is_local_comfy = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port == 8188
        )
        # 宿主（而非模型）负责把产物收进缓存：预览持久、且任意输出路径不必进入
        # /api/file 的允许根（工作区外路径仍不可读）。
        if is_local_comfy or is_local_file:
            uploads_dir = (data_dir / "uploads").resolve()
            try:
                already_cached = is_local_file and path_within(local_path.resolve(), uploads_dir)
            except OSError as exc:
                logger.warning("媒体候选路径解析失败：source=%s error=%s", source, exc)
                already_cached = False
            if not already_cached:
                try:
                    generated_dir = (data_dir / "generated").resolve()
                    generated_dir.mkdir(parents=True, exist_ok=True)
                    destination = _cache_by_content(source, local_path, is_local_comfy, generated_dir, name)
                    source = str(destination)
                    # 写盘成功即通知宿主（generated 目录自己的后台清理触发点）。
                    self._notify_cached()
                    if not thumb_path:
                        thumb_path = _ensure_webp_thumb(destination, imaging)
                        if not thumb_path:
                            # 缩略图不可得（GIF/视频/音频/损坏图片）时退化为主图，保证可显示。
                            thumb_path = source
                except (OSError, urllib.error.URLError, ValueError) as exc:
                    # 缓存失败不静默：记日志并保留原来源（ComfyUI /view 由 /api/file 代理显示）。
                    logger.warning("媒体缓存失败，保留原来源：source=%s error=%s", source, exc)
        # 落地校验：本地路径必须真实存在，否则**丢弃**该记录。
        # 否则会给消息挂上一张必然 403/404 的破图（真实事故：脚本 stdout 中文乱码 →
        # 路径不存在 → 前端渲染出破损图像）；URL 仍保留（ComfyUI /view 由 /api/file 代理）。
        if parsed.scheme not in {"http", "https"}:
            try:
                if not Path(source).expanduser().is_file():
                    logger.info("媒体候选落地失败（本地文件不存在），丢弃：source=%s", source)
                    return None
            except OSError as exc:
                logger.warning("媒体候选落地校验失败，丢弃：source=%s error=%s", source, exc)
                return None
        return {"kind": kind, "name": name, "source": source, "thumb_path": thumb_path}

    def _notify_cached(self) -> None:
        """通知宿主"刚往 generated 落了一份产物"；回调异常不得影响采集链路。"""
        callback = self._on_cached
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - 采集的副作用绝不打断工具执行
            logger.warning("产物落盘回调失败：%s", exc)


def _cache_by_content(
    source: str,
    local_path: Path,
    is_local_comfy: bool,
    generated_dir: Path,
    name: str,
) -> Path:
    """把产物按**内容哈希**收进 generated 缓存，返回落盘路径。

    缓存键必须是**内容**而不是来源：ComfyUI 每次生成都复用同一个文件名
    （`lumine_cute_00002_.png` 会被覆盖），若按 URL 命中旧缓存就会显示上一次的图
    （用户实测："Job 刚跑完，显示的却是旧图"）。这里流式读取来源、边算 sha256 边写
    临时文件，再原子改名为 `<内容哈希16>_<name>`；同内容已存在时复用（去重），
    内容变化则自然落到新文件——顺带让 `/api/file` 的浏览器缓存不会命中旧内容。

    失败（网络/磁盘/权限）由调用方兜底（保留原来源并记日志）。
    """
    temp = generated_dir / f".{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    try:
        reader = net_io.open(source, timeout=120) if is_local_comfy else local_path.open("rb")
        with reader as stream, temp.open("wb") as writer:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                writer.write(chunk)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    destination = generated_dir / f"{digest.hexdigest()[:16]}_{name}"
    if destination.is_file() and destination.stat().st_size > 0:
        temp.unlink(missing_ok=True)
    else:
        temp.replace(destination)
    return destination


def _candidates_from_result(result: str, extract: str, *, tool: str = "") -> list[dict[str, str]]:
    """按提取策略从工具结果里取出媒体候选（保持出现顺序）。"""
    text = str(result or "")
    if extract == "structured":
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            # 声明为结构化却给出散文：合法情形（vision_analyze 的"分析形态"返回文字描述，
            # 同一工具另一形态才返回媒体记录），按"本次无媒体"处理并留 debug 痕迹；
            # 严格不回退文本扫描，避免把散文里提到的路径当成本轮产物。
            logger.debug("工具结果非 JSON，按无媒体处理：tool=%s head=%s", tool, text[:120])
            return []
        candidates: list[dict[str, str]] = []
        _visit(payload, candidates)
        return candidates
    # scan：结构化优先，失败回退文本路径扫描（与旧 extract_attachments 口径一致）。
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return [
            {"source": source, "name": "", "thumb_path": ""}
            for source in _text_media_sources(text)
        ]
    candidates = []
    _visit(payload, candidates)
    return candidates


def _text_media_sources(text: str) -> list[str]:
    """纯文本里可识别的媒体路径/URL（按出现顺序，已去掉黏在路径后的散文后缀）。

    两类来源的边界处理**必须分开**（实测教训）：
    - URL（http/https）：只去掉**尾部**标点，**绝不按内部标点截断**——ComfyUI 产物 URL 形如
      `http://127.0.0.1:8188/view?filename=a.png&subfolder=&type=output`，按 `?` 截断会得到
      `.../view` 从而判定"不是媒体"（Job 完成后图片不显示的真实根因）；
    - Windows 路径：在散文终止符处截断（`已写入 C:\\a.png（12 字符）` 的中文后缀会黏住路径），
      终止符不含 `:`（盘符）与空格（路径可含空格）。
    """
    sources: list[str] = []
    for raw in _TEXT_PATH_RE.findall(str(text or "")):
        if raw.lower().startswith(("http://", "https://")):
            cjk = _CJK_RE.search(raw)
            if cjk:
                raw = raw[: cjk.start()]
            candidate = raw.rstrip(" .,;:!?)]}>）】」，。；、！？")
        else:
            cut = len(raw)
            for marker in _PROSE_TERMINATORS:
                position = raw.find(marker)
                if 0 <= position < cut:
                    cut = position
            candidate = raw[:cut].rstrip(" .,;:、，。；：")
        if media_kind_of(candidate):
            sources.append(candidate)
    return sources


def _visit(value: Any, out: list[dict[str, str]]) -> None:
    """递归扫描结果，收集媒体来源（结构化记录优先，命中即不再深入该节点）。"""
    if isinstance(value, str):
        if media_kind_of(value):
            out.append({"source": value, "name": "", "thumb_path": ""})
        return
    if isinstance(value, list):
        for item in value:
            _visit(item, out)
        return
    if not isinstance(value, dict):
        return
    media_source = next(
        (
            str(value[key])
            for key in _SOURCE_KEYS
            if isinstance(value.get(key), str) and media_kind_of(str(value[key]))
        ),
        "",
    )
    if media_source:
        thumb = next(
            (str(value[key]) for key in _THUMB_KEYS if isinstance(value.get(key), str)), ""
        )
        name = next((str(value[key]) for key in _NAME_KEYS if isinstance(value.get(key), str)), "")
        out.append({"source": media_source, "name": name, "thumb_path": thumb})
        return
    for item in value.values():
        _visit(item, out)


def _dedupe_candidates(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    """候选级去重：同来源只留一份；同名（不同来源）优先保留带缩略图的版本。

    同一张图常同时被工具路径（无缩略图）与 vision_analyze（缓存+缩略图）各记一次，
    按名去重可避免"同图双份、其中一份破图"，也避免重复占用分桶配额。
    """
    by_source: dict[str, dict[str, str]] = {}
    order: list[str] = []
    by_name: dict[str, str] = {}
    for candidate in candidates:
        source = str(candidate.get("source") or "")
        if not source or source in by_source:
            continue
        name_key = str(candidate.get("name") or "").strip().lower()
        if name_key:
            existing_source = by_name.get(name_key)
            if existing_source is not None:
                existing = by_source[existing_source]
                if candidate.get("thumb_path") and not existing.get("thumb_path"):
                    by_source[existing_source] = candidate
                continue
            by_name[name_key] = source
        by_source[source] = candidate
        order.append(source)
    return [by_source[source] for source in order]


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """内容级去重：同字节的文件只保留第一份（ComfyUI URL 与本地副本常指向同一张图）。"""
    final: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        source = str(record.get("source") or "")
        try:
            path = Path(source).expanduser()
            if path.is_file() and path.stat().st_size > 0:
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
                key = "file:" + digest.hexdigest()
            else:
                key = "url:" + source
        except OSError as exc:
            logger.warning("媒体去重读取失败，按来源去重：source=%s error=%s", source, exc)
            key = "url:" + source
        if key in seen:
            continue
        seen.add(key)
        final.append(record)
    return final


def bucket_limits() -> dict[str, int]:
    """分桶上限（供测试与文档引用，唯一定义在 core/media_types.py）。"""
    return dict(MEDIA_BUCKET_LIMITS)
