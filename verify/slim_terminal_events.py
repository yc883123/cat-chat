# -*- coding: utf-8 -*-
"""数据工具：按迁移的全库口径收缩**存量**终态事件的完整消息副本（默认 dry-run）。

为什么要它：`slim_terminal_run` 只收拾"刚刚结束的那个 run"；迁移 v14 只覆盖 done/cancelled，
之后又落了新的整份 done 消息（本机实测 201 行 / 36.0MB，占 run_events 全部载荷的 82%）。
v24 迁移已带全库口径，但那是**下次启动**才跑；已经跑着老版本、又不想重启时，用本工具按同一
口径（`storage.store._slim_terminal_event_payloads`）先收一次。

用法：
    .venv\\Scripts\\python.exe verify\\slim_terminal_events.py                  # dry-run，只报数
    .venv\\Scripts\\python.exe verify\\slim_terminal_events.py --apply          # 备份 → 收缩 → VACUUM
    .venv\\Scripts\\python.exe verify\\slim_terminal_events.py --db <chat.db> --apply
    .venv\\Scripts\\python.exe verify\\slim_terminal_events.py --apply --no-vacuum

安全边界（都踩过或想清楚了才写下来）：
- 默认库 = 应用自己解析的 `data_dir/chat.db`（`naiba.paths.default_path_context`）；打包版在
  别的数据目录时用 `--db` 指过去——本文件不写死任何盘符/用户名。
- **不构造 `ChatStorage`**：它的 `__init__` 会跑「重启清理」（把 in-flight 任务标成 interrupted）
  与全部待执行迁移，对着**正在运行**的实例的库做这些事会直接打断用户当前那一轮。本工具只用
  裸连接 + 那个纯函数口径。
- `--apply` 先用 SQLite 官方在线备份 API 做一致性整库快照（文件拷贝在别的进程写入时可能撕裂），
  备份失败即中止。
- 只改 `run_events.payload` 里 done/cancelled 的 `message`/`aborted_message` 与 error 的
  `partial_message`（`error.message` 是错误文案，**不在**收缩列）；messages 表与 schema 版本
  一律不动。
- VACUUM 失败（应用在跑、库被占用）只告警：内容收缩已经提交，物理回收可在设置页点「压缩数据库」。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.paths import default_path_context  # noqa: E402
from naiba.storage import store as storage_module  # noqa: E402

# 收缩判据与 `_SLIM_TERMINAL_PAYLOAD_KEYS` 同口径：error 的 `message` 是错误文案，保留。
FAT_SQL = (
    "SELECT COUNT(*) FROM run_events WHERE "
    "(event_type IN ('done','cancelled') "
    " AND (payload LIKE '%\"message\"%' OR payload LIKE '%\"aborted_message\"%')) "
    "OR (event_type = 'error' AND payload LIKE '%\"partial_message\"%')"
)


def default_db_path() -> Path:
    return Path(default_path_context().data_dir) / "chat.db"


def stats(db_path: Path) -> dict:
    with closing(sqlite3.connect(db_path, timeout=30)) as db:
        fat = db.execute(FAT_SQL).fetchone()[0]
        payload_bytes = db.execute(
            "SELECT COALESCE(SUM(LENGTH(payload)),0) FROM run_events"
        ).fetchone()[0]
        events = db.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
        messages = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
    return {
        "fat_rows": int(fat),
        "event_payload_bytes": int(payload_bytes),
        "events": int(events),
        "messages": int(messages),
        "user_version": int(version),
        "db_bytes": db_path.stat().st_size if db_path.exists() else 0,
    }


def describe(label: str, item: dict) -> None:
    print(
        f"{label}：待收缩行={item['fat_rows']} 事件载荷={item['event_payload_bytes']/1048576:.1f}MB "
        f"事件数={item['events']} 消息数={item['messages']} schema=v{item['user_version']} "
        f"库文件={item['db_bytes']/1048576:.1f}MB"
    )


def online_backup(db_path: Path, backup_dir: Path) -> Path:
    """用 SQLite 在线备份 API 做一致性快照（别的进程正在写也安全）。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / (db_path.name + "." + time.strftime("%Y%m%d-%H%M%S") + ".bak")
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        destination = sqlite3.connect(target)
        try:
            with destination:
                source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="收缩存量终态事件的完整消息副本（默认 dry-run）")
    parser.add_argument("--db", default="", help="目标 chat.db（默认：应用解析出的 data_dir/chat.db）")
    parser.add_argument("--apply", action="store_true", help="真的改写（默认只报数）")
    parser.add_argument("--no-vacuum", action="store_true", help="跳过 VACUUM（只做内容收缩）")
    parser.add_argument("--backup-dir", default="", help="备份目录（默认：库同级的 backups/）")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve() if args.db else default_db_path()
    if not db_path.exists():
        print(f"库不存在：{db_path}")
        return 2
    print(f"目标库：{db_path}")

    before = stats(db_path)
    describe("收缩前", before)
    if before["fat_rows"] == 0:
        print("没有需要收缩的存量终态副本，收工（dry-run 与 --apply 都不会改动任何行）")
        return 0

    if not args.apply:
        print("\n这是 dry-run：加 --apply 才会改写（会先做一致性整库备份）。")
        return 0

    backup_dir = Path(args.backup_dir) if args.backup_dir else db_path.parent / "backups"
    try:
        backup = online_backup(db_path, backup_dir)
    except Exception as exc:  # noqa: BLE001 - 备份失败必须中止，不能带着风险改写
        print(f"备份失败，已中止：{type(exc).__name__}: {exc}")
        return 2
    print(f"备份完成：{backup}（{backup.stat().st_size/1048576:.1f}MB）")

    with closing(sqlite3.connect(db_path, timeout=30)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        updated = storage_module._slim_terminal_event_payloads(db)  # noqa: SLF001 - 迁移同口径
        db.commit()
        print(f"内容收缩完成：{updated} 行已改写")
        if not args.no_vacuum:
            try:
                db.execute("VACUUM")
                print("VACUUM 完成")
            except sqlite3.OperationalError as exc:
                print(f"警告：VACUUM 未执行（{exc}）——内容已提交；可在设置页点「压缩数据库」回收空间")

    after = stats(db_path)
    describe("收缩后", after)
    if after["fat_rows"] != 0:
        print(f"仍剩 {after['fat_rows']} 行未收缩（可能是并发写入的新事件），可再跑一次")
    if after["messages"] != before["messages"]:
        print("⚠️ messages 行数变化，请核对！")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
