# -*- coding: utf-8 -*-
"""图片缓存重复写入审计（只读）。

用途：量化宿主图片缓存（`data/generated`、`data/uploads`）里"同一份内容被写了两遍"
的规模，并验证内容级去重的查找（`naiba.storage.media.existing_content_path`）在真实
数据上既不漏命中也不误判。

用法（相对路径 = 相对本脚本所在目录；数据目录默认 `%LOCALAPPDATA%\\NaibaChat\\data`）：

    项目根\\.venv\\Scripts\\python.exe verify\\_dedup_write_audit.py [数据目录]

判据（先写在前面，命中即判定）：
- **重复组** = 同一 sha256 下、主图（非 `_thumb.webp`）多于一份的组；
- **冗余字节** = 重复组内除第一份以外的全部字节（这就是"重复写入"的量级）；
- **命中** = 组内第 2..N 份用 `existing_content_path(第一份的前 1MiB)` 能查回第一份；
- **误判** = 查回的文件与来源内容不一致（会导致显示错图，必须为 0）。

本脚本只读文件，不写入、不删除、不建目录。
"""
from __future__ import annotations

import hashlib
import os
import sys
from collections import defaultdict
from pathlib import Path

# 相对路径 = 相对本脚本所在目录（verify/），仓库根是它的上一级。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.storage.media import existing_content_path  # noqa: E402

HEAD_BYTES = 1024 * 1024
CHUNK = 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def default_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SystemExit("未设置 LOCALAPPDATA，请把数据目录作为参数传入")
    return Path(local) / "NaibaChat" / "data"


def audit_scope(scope_dir: Path) -> dict[str, int]:
    """审计单个缓存目录，返回统计；不存在的目录返回空统计。"""
    stats = {"files": 0, "bytes": 0, "mains": 0, "groups": 0, "redundant_bytes": 0,
             "hit": 0, "miss": 0, "wrong": 0, "false_positive": 0}
    if not scope_dir.is_dir():
        print(f"[{scope_dir.name}] 目录不存在，跳过：{scope_dir}")
        return stats
    files = [p for p in scope_dir.rglob("*") if p.is_file()]
    mains = [p for p in files if not p.name.endswith("_thumb.webp")]
    stats["files"] = len(files)
    stats["bytes"] = sum(p.stat().st_size for p in files)
    stats["mains"] = len(mains)

    by_digest: dict[str, list[Path]] = defaultdict(list)
    for path in mains:
        by_digest[sha256_of(path)].append(path)
    groups = {k: v for k, v in by_digest.items() if len(v) > 1}
    stats["groups"] = len(groups)
    stats["redundant_bytes"] = sum(
        p.stat().st_size for members in groups.values() for p in members[1:]
    )

    for members in groups.values():
        primary = members[0]
        head = primary.open("rb").read(HEAD_BYTES)
        for other in members[1:]:
            found = existing_content_path(scope_dir, head)
            if found is None:
                stats["miss"] += 1
                print(f"  未命中：{other.name}")
            elif sha256_of(found) == sha256_of(primary):
                stats["hit"] += 1
            else:
                stats["wrong"] += 1
                print(f"  误判：{other.name} -> {found.name}")

    # 全量误判扫描：每个主图都查一次，查回的内容必须与自身一致。
    for path in mains:
        found = existing_content_path(scope_dir, path.open("rb").read(HEAD_BYTES))
        if found is not None and sha256_of(found) != sha256_of(path):
            stats["false_positive"] += 1
            print(f"  ! 误命中：{path.name} -> {found.name}")

    print(
        f"[{scope_dir.name}] {stats['files']} 个文件 / {stats['bytes'] / 1048576:.1f} MB，"
        f"主图 {stats['mains']} 个；重复组 {stats['groups']} 组，"
        f"冗余 {stats['redundant_bytes'] / 1048576:.1f} MB；"
        f"命中 {stats['hit']} / 未命中 {stats['miss']} / 误判 {stats['wrong']} / "
        f"全量误命中 {stats['false_positive']}"
    )
    return stats


def main(argv: list[str]) -> int:
    data_dir = Path(argv[1]).expanduser() if len(argv) > 1 else default_data_dir()
    print(f"数据目录：{data_dir}")
    total = {"groups": 0, "redundant_bytes": 0, "miss": 0, "wrong": 0, "false_positive": 0}
    for scope in ("generated", "uploads"):
        stats = audit_scope((data_dir / scope).resolve())
        for key in total:
            total[key] += stats[key]
    clean = total["miss"] == 0 and total["wrong"] == 0 and total["false_positive"] == 0
    print(
        f"合计：重复组 {total['groups']}，冗余 {total['redundant_bytes'] / 1048576:.1f} MB；"
        "结论：" + ("去重查找全部命中且无误判" if clean else "存在未命中或误判，需复核")
    )
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
