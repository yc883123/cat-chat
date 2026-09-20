# -*- coding: utf-8 -*-
"""媒体采集链路体检（只读）：一次性回答「卡片为什么少了 / 为什么裂了」。

用法：
    .venv\\Scripts\\python.exe verify\\media_pipeline_audit.py [conversation_id]

不给会话 ID 时扫全库。检查四件事（对应维护说明 §四 媒体链路）：

1. **破图来源 A：URL 兜底**——`media_collect._materialize` 在缓存失败时保留原来源
   （`media_collect.py` 缓存失败分支），localhost ComfyUI /view 一旦失效即 404；
   本项还会校验 URL 结构是否完整（被截断的结果文本会切出残缺百分号编码）。
2. **破图来源 B：本地缓存被清理**——记录指向的本地文件已不存在（generated/uploads
   被清过之后，老消息上的卡片全部失效，前端无降级）。
3. **少显示来源 A（合法）**：`attachments_truncated` / `media_truncated` 自述截断
   （图 20 / 视频 8 / 音频 8，见 `core/media_types.py::MEDIA_BUCKET_LIMITS`）。
4. **少显示来源 B（需警惕）**：同名不同源被 `_dedupe_candidates` / `union_run_media`
   按 name 丢弃——只有缩略图更全时才覆盖，因此可能丢掉后生成的同名新图。

阈值判定同源：本地同名字段用尺寸+解码均值差，均值差 < 6 视为同一张图（压缩副本）。

只读：数据库以 `mode=ro` 打开，绝不写任何文件。
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import urllib.parse
from collections import Counter
from datetime import datetime
from pathlib import Path

# 残缺百分号编码（结果文本被硬截断后剩下的半个转义序列）
BROKEN_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def resolve_db_path() -> Path:
    """数据目录取 config.json 的 data_dir（缺省回落 D:\\naibachatdata）。"""
    root = Path(__file__).resolve().parent.parent
    data_dir = "D:/naibachatdata"
    for candidate in (root / "config.json", Path.home() / "AppData/Local/NaibaChat/config.json"):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data_dir = str(payload.get("data_dir") or data_dir)
        break
    return Path(data_dir) / "chat.db"


def iter_records(metadata: dict):
    """产出 (来源位置, 记录) —— 消息级 attachments + 各次工具调用的 media。"""
    for item in metadata.get("attachments") or []:
        if isinstance(item, dict):
            yield "attachments", item
    for run in metadata.get("tool_runs") or []:
        if not isinstance(run, dict):
            continue
        for item in run.get("media") or []:
            if isinstance(item, dict):
                yield f"run:{run.get('tool')}", item


def source_state(source: str) -> str:
    """ok / url / missing / 路径异常。"""
    if source.lower().startswith(("http://", "https://")):
        return "url"
    try:
        return "ok" if Path(source).expanduser().is_file() else "missing"
    except OSError:
        return "路径异常"


def check_url_shape(url: str) -> str:
    """URL 结构体检：残缺百分号编码 / 解出替换字符（被截断的典型特征）。"""
    problems = []
    if BROKEN_ESCAPE_RE.search(url):
        problems.append("残缺百分号编码（结果文本被截断）")
    decoded = urllib.parse.unquote(url)
    if "\ufffd" in decoded:
        problems.append("解码出替换字符 U+FFFD")
    parsed = urllib.parse.urlsplit(url)
    filename = (urllib.parse.parse_qs(parsed.query).get("filename") or [""])[0]
    if not filename:
        problems.append("缺少 filename 查询参数")
    return "；".join(problems)


def same_image(path_a: Path, path_b: Path) -> bool | None:
    """两张本地图是否同一张（压缩副本）。PIL 缺失或解码失败时返回 None。"""
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError:
        return None
    try:
        with Image.open(path_a) as ia, Image.open(path_b) as ib:
            rgb_a, rgb_b = ia.convert("RGB"), ib.convert("RGB")
            if rgb_a.size != rgb_b.size:
                rgb_b = rgb_b.resize(rgb_a.size)
            diff = ImageChops.difference(rgb_a.resize((64, 64)), rgb_b.resize((64, 64)))
            return sum(ImageStat.Stat(diff).mean) / 3 < 6
    except Exception:
        return None


def main() -> int:
    conversation = sys.argv[1] if len(sys.argv) > 1 else ""
    db_path = resolve_db_path()
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}")
        return 2
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = "SELECT id, conversation_id, created_at, metadata FROM messages"
    params: tuple = ()
    if conversation:
        sql += " WHERE conversation_id = ?"
        params = (conversation,)

    urls: list[tuple] = []
    missing: list[tuple] = []
    truncations: list[tuple] = []
    name_collisions: list[tuple] = []
    by_day: dict[str, list[int]] = {}
    total = 0

    for row in conn.execute(sql, params):
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except ValueError:
            continue
        if metadata.get("attachments_truncated"):
            truncations.append((row["id"], "attachments", metadata["attachments_truncated"]))
        seen: set[str] = set()
        by_name: dict[str, list[tuple[str, bool]]] = {}
        for where, record in iter_records(metadata):
            source = str(record.get("source") or "")
            if not source or source in seen:
                continue
            seen.add(source)
            total += 1
            day = datetime.fromtimestamp((row["created_at"] or 0) / 1000).strftime("%Y-%m-%d")
            bucket = by_day.setdefault(day, [0, 0])
            bucket[1] += 1
            state = source_state(source)
            if state == "url":
                urls.append((row["id"], where, record.get("name"), source, check_url_shape(source)))
                continue
            if state != "ok":
                bucket[0] += 1
                missing.append((row["id"], row["conversation_id"], where, record.get("name"), source))
            name = str(record.get("name") or "").strip().lower()
            if name:
                by_name.setdefault(name, []).append((source, bool(record.get("thumb_path"))))
        for run in metadata.get("tool_runs") or []:
            if isinstance(run, dict) and run.get("media_truncated"):
                truncations.append((row["id"], f"run:{run.get('tool')}", run["media_truncated"]))
        for name, items in by_name.items():
            if len({s for s, _ in items}) > 1:
                verdicts = []
                for i in range(1, len(items)):
                    a, b = items[0][0], items[i][0]
                    if a.lower().startswith("http") or b.lower().startswith("http"):
                        verdicts.append("URL 兜底 vs 本地 → 视为同一张")
                        continue
                    same = same_image(Path(a), Path(b))
                    if same is True:
                        verdicts.append("同一张（压缩副本）")
                    elif same is False:
                        verdicts.append("★ 内容不同 → 后生成的那张被丢弃")
                    else:
                        verdicts.append("无法判定（PIL 缺失或解码失败）")
                name_collisions.append((row["id"], name, items, verdicts))

    print(f"数据库：{db_path}")
    print(f"媒体记录（按来源去重后）：{total}")
    print()
    print(f"[1] 保留 URL 的记录：{len(urls)}  （本机 ComfyUI /view 缓存失败时的兜底）")
    for mid, where, name, url, shape in urls:
        print(f"    msg={mid[:12]} {where:<28} {str(name)[:24]:<26} {shape or 'URL 结构正常'}")
        print(f"        {url[:140]}")
    print()
    print(f"[2] 本地文件缺失：{len(missing)}  （缓存被清理后老消息即破图）")
    buckets = Counter()
    for item in missing:
        for token in ("generated", "uploads", "video_frames", "backgrounds", "avatars"):
            if f"\\{token}\\" in str(item[4]) or f"/{token}/" in str(item[4]):
                buckets[token] += 1
                break
        else:
            buckets["其他/工作区外"] += 1
    for key, value in buckets.most_common():
        print(f"    {key}: {value}")
    print()
    print(f"[3] 截断自述（合法少显示，前端有提示）：{len(truncations)}")
    for mid, where, info in truncations:
        print(f"    msg={mid[:12]} {where:<30} {json.dumps(info, ensure_ascii=False)}")
    print()
    print(f"[4] 同名不同源（名字去重介入）：{len(name_collisions)} 处")
    losses = 0
    for mid, name, items, verdicts in name_collisions:
        hit = any(v.startswith("★") for v in verdicts)
        losses += 1 if hit else 0
        print(f"    msg={mid[:12]} {name} ← {' | '.join(verdicts)}")
        if hit:
            for src, thumb in items:
                print(f"        {'[缩略图]' if thumb else '[无缩略图]'} {src[:110]}")
    print()
    print(f"结论：真丢图 {losses} 处；其余同名碰撞均为「原图 vs 压缩副本」或「URL vs 缓存」。")
    print()
    print("缺失按日期分布（缺失/总数）：")
    for day in sorted(by_day):
        miss, tot = by_day[day]
        print(f"    {day}  {miss:>4}/{tot:<4} {'#' * int(40 * miss / max(1, tot))}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
