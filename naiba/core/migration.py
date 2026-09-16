# -*- coding: utf-8 -*-
"""旧安装/数据目录迁移辅助（层级 1；路径经 PathContext 显式传入，不读全局）。"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from naiba.paths import PathContext

logger = logging.getLogger("naiba.migration")


def _config_has_providers(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    providers = value.get("providers") if isinstance(value, dict) else None
    return isinstance(providers, list) and any(
        isinstance(provider, dict) and str(provider.get("id") or "").strip()
        for provider in providers
    )


def _database_has_conversations(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute("SELECT COUNT(*) FROM conversations").fetchone()
            return bool(row and int(row[0] or 0))
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False


def _skip_set(skip_relative: Any) -> set[str]:
    """把「相对根路径」集合归一为 POSIX 分隔、去首尾斜杠的形式。"""
    return {
        str(item).replace("\\", "/").strip("/")
        for item in (skip_relative or set())
        if str(item).strip("/")
    }


def _merge_data_tree(source: Path, target: Path, skip_relative: Any = None, _relative: str = "") -> bool:
    """Copy a data tree recursively, keeping files already present at target.

    ``skip_relative`` 里的相对目录会被整枝跳过：用户已删除（隐藏）的 Skill 不能被
    旧目录合并重新复制回来，否则每次启动都会"复活"，表现为删不掉。
    """
    skip = _skip_set(skip_relative)
    changed = False
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        child = f"{_relative}/{item.name}" if _relative else item.name
        if skip and child in skip:
            continue
        destination = target / item.name
        if item.is_dir():
            changed = _merge_data_tree(item, destination, skip, child) or changed
        elif not destination.exists():
            shutil.copy2(item, destination)
            changed = True
    return changed


def _sync_bundled_skills(source: Path, target: Path, skip_relative: Any = None, _relative: str = "") -> bool:
    """Refresh packaged Skill files without deleting older persisted Skills.

    ``skip_relative`` 里的相对目录会被整枝跳过，避免把用户已删除的内置 Skill
    在每次启动时重新写回托管目录。
    """
    changed = False
    skip = _skip_set(skip_relative)
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        child = f"{_relative}/{item.name}" if _relative else item.name
        if skip and child in skip:
            continue
        destination = target / item.name
        if item.is_dir():
            changed = _sync_bundled_skills(item, destination, skip, child) or changed
            continue
        try:
            needs_copy = not destination.is_file() or item.read_bytes() != destination.read_bytes()
        except OSError:
            needs_copy = True
        if needs_copy:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
            changed = True
    return changed


def _copy_legacy_data(source: Path, paths: PathContext, replace_empty_target: bool = True) -> dict[str, bool]:
    """Merge a legacy install into the current data directory, including all subdirectories."""
    report = {"config": False, "data": False, "skills": False}
    legacy_config = source / "config.json"
    legacy_data = source / "data"
    paths.app_dir.mkdir(parents=True, exist_ok=True)

    if legacy_config.is_file() and (
        not paths.config_path.exists() or not _config_has_providers(paths.config_path)
    ):
        shutil.copy2(legacy_config, paths.config_path)
        report["config"] = True

    if not legacy_data.is_dir():
        return report
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    source_db = legacy_data / "chat.db"
    target_db = paths.data_dir / "chat.db"
    replace_db = source_db.is_file() and (
        not target_db.exists()
        or (replace_empty_target and not _database_has_conversations(target_db)
            and _database_has_conversations(source_db))
    )
    if replace_db:
        # A stale WAL/SHM pair from the empty database must not be reused with
        # the restored database. The running server is stopped before startup
        # migration, and the target directory was backed up by the caller.
        for suffix in ("-wal", "-shm"):
            sidecar = target_db.with_name(target_db.name + suffix)
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.copy2(source_db, target_db)
        report["data"] = True
        for suffix in ("-wal", "-shm"):
            sidecar = source_db.with_name(source_db.name + suffix)
            if sidecar.is_file() and sidecar.stat().st_size:
                shutil.copy2(sidecar, target_db.with_name(target_db.name + suffix))

    # 旧数据目录顶层的易变运行期档不搬运：数据库本体由其上方专属分支处理（含 WAL/SHM 配对），
    # server.lock / server.json 是实例锁与状态档（pid/host/port/token），每次启动都会重写。
    volatile = {"chat.db", "chat.db-wal", "chat.db-shm", "server.lock", "server.json"}
    for item in legacy_data.iterdir():
        if item.name in volatile:
            continue
        destination = paths.data_dir / item.name
        if item.is_dir():
            report["data"] = _merge_data_tree(item, destination) or report["data"]
        elif not destination.exists():
            # _merge_data_tree 只接受目录（首句就是 target.mkdir），文件喂进去会先把目标建成
            # 同名目录、再由 source.iterdir() 抛 OSError，让整段迁移静默中断（WinError 183 事故）。
            shutil.copy2(item, destination)
            report["data"] = True

    # 导入旧安装根目录的 Skills（旧版 paths.app_dir/skills 或数据目录同级 skills）到新托管目录。
    managed_skills = (paths.data_dir / "skills").resolve()
    for legacy_skills in (
        (source / "skills").resolve(),
        (source.parent / "skills").resolve(),
        (legacy_data / "skills").resolve(),
    ):
        if legacy_skills.is_dir() and legacy_skills != managed_skills:
            report["skills"] = _merge_data_tree(legacy_skills, managed_skills) or report["skills"]
    return report


def _write_migration_warning(paths: PathContext, exc: BaseException) -> None:
    """把启动迁移失败追加到数据目录（文件通道，沿用 storage/store.py 的既有约定）。

    冻结版（windowed）stdout/stderr 均为 None，``logging`` 的兜底输出无处可去，所以窗口版
    唯一的诊断通道就是数据目录里的日志文件。诊断通道自身失败不改变迁移结果，故只放弃写文件。
    """
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        target = paths.data_dir / "data-migration-warning.log"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [naiba-migration] {type(exc).__name__}: {exc}\n")
    except OSError:
        pass


def migrate_legacy_data(paths: PathContext) -> dict[str, Any]:
    """冻结版每次启动：尝试把 EXE 相邻旧目录的 config.json 与 data/ 合并进数据目录。

    旧文件保留（不删除、不改写源目录）；目标已有的文件一律优先，只补缺失项，
    因此重复启动是幂等的。源码模式（paths.app_dir == paths.exe_dir）直接跳过。
    """
    report: dict[str, Any] = {"migrated": False, "config": False, "data": False, "source": ""}
    if str(paths.app_dir) == str(paths.exe_dir):
        return report
    legacy_config = paths.exe_dir / "config.json"
    legacy_data = paths.exe_dir / "data"
    if not legacy_config.is_file() and not legacy_data.is_dir():
        return report
    try:
        migrated = _copy_legacy_data(paths.exe_dir, paths)
        report.update(migrated)
    except Exception as exc:  # noqa: BLE001 - 迁移失败绝不能阻断启动
        # 启动路径上未被捕获的非 OSError 会让冻结版直接弹 traceback 框退出（EXE 旁有旧目录的
        # 用户必现）。这里放宽到 Exception：记录 + 进 report（bootstrap 诊断页可取）+ 落文件，
        # 迁移退化成"未执行"，但程序照常启动（旧数据仍在 EXE 旁，不会丢）。
        report["error"] = f"{type(exc).__name__}: {exc}"
        logger.exception("迁移旧数据失败（已跳过，不影响启动）：%s", exc)
        _write_migration_warning(paths, exc)
    if report["config"] or report["data"]:
        report["migrated"] = True
        report["source"] = str(paths.exe_dir)
    return report
