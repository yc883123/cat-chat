# -*- coding: utf-8 -*-
"""护栏：冻结版启动迁移的「EXE 目录 ≠ 数据目录」形态。

保护对象：`naiba/core/migration.py` 的 `migrate_legacy_data` / `_copy_legacy_data`。

覆盖：
- EXE 旁存在旧 `config.json` / `data/` 时启动不再抛异常（2.0.0-beta 起漏传 `paths` 必抛 TypeError）；
- 旧数据完整搬运（配置 / 数据库 / uploads / 普通文件），运行期易变档（`server.json` / `server.lock`）不搬；
- 目标已有 `data/server.json` 时迁移不被中断（WinError 183 事故：后续 uploads 全被静默丢掉）；
- 迁移抛出非 OSError 时只报告、不阻断启动（本次崩溃的放大器）；
- 源码模式（`app_dir == exe_dir`）保持早退语义；
- 装配级：EXE 旁有旧数据的冻结版形态下 `NaibaChatApp` 能构造成功。

存在理由：现有 `test_config_migration` / `test_storage_migration` 只覆盖配置与库结构；而
`PathContext.local()` 会把 `exe_dir` 设成等于 `app_dir`，让这段代码在第一处提前返回就溜走，
所以该分支长期零覆盖——漏传 `paths` 因此躲过了全部单测与 CI。
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core import migration as migration_module  # noqa: E402
from naiba.core.migration import migrate_legacy_data  # noqa: E402
from naiba.paths import PathContext  # noqa: E402


def _create_legacy_db(path: Path, conversations: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT)")
        for index in range(conversations):
            connection.execute(
                "INSERT INTO conversations (id, title) VALUES (?, ?)", (f"c{index}", "旧会话")
            )
        connection.commit()


def _count_conversations(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])


class FrozenStartupMigrationTests(unittest.TestCase):
    """冻结版形态：app_dir（%LOCALAPPDATA%\\NaibaChat）与 exe_dir（便携目录）必然不同。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_legacy_migration_")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.app_dir = (root / "app").resolve()
        self.exe_dir = (root / "portable").resolve()
        self.exe_dir.mkdir(parents=True, exist_ok=True)
        # 直接实例化 PathContext：PathContext.local() 会令 exe_dir == app_dir，正好绕过本文件要守的代码。
        self.paths = PathContext(
            app_dir=self.app_dir,
            exe_dir=self.exe_dir,
            resource_dir=self.app_dir,
            public_dir=(self.app_dir / "public").resolve(),
            config_path=(self.app_dir / "config.json").resolve(),
            data_dir=(self.app_dir / "data").resolve(),
            status_path=(self.app_dir / "data" / "server.json").resolve(),
            lock_path=(self.app_dir / "data" / "server.lock").resolve(),
        )

    def _seed_legacy(
        self,
        *,
        config: bool = True,
        database: bool = True,
        uploads: bool = True,
        status: bool = True,
        lock: bool = True,
        note: bool = False,
    ) -> Path:
        """在 EXE 旁造出 build-35 时代的便携布局（EXE_DIR/config.json + EXE_DIR/data/...）。"""
        if config:
            (self.exe_dir / "config.json").write_text(
                json.dumps({"providers": [{"id": "legacy-provider"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
        legacy_data = self.exe_dir / "data"
        legacy_data.mkdir(parents=True, exist_ok=True)
        if database:
            _create_legacy_db(legacy_data / "chat.db")
        if uploads:
            target = legacy_data / "uploads" / "old.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"legacy-image")
        if status:
            (legacy_data / "server.json").write_text('{"pid": 1, "port": 8765}', encoding="utf-8")
        if lock:
            (legacy_data / "server.lock").write_text("0", encoding="utf-8")
        if note:
            (legacy_data / "custom-note.json").write_text('{"keep": true}', encoding="utf-8")
        return legacy_data

    def test_frozen_startup_migrates_legacy_install(self) -> None:
        self._seed_legacy()
        report = migrate_legacy_data(self.paths)  # 2.0.0-beta 起漏传 paths，这里必抛 TypeError
        self.assertTrue(report["migrated"])
        self.assertTrue(report["config"])
        self.assertTrue(report["data"])
        self.assertEqual(Path(report["source"]).resolve(), self.exe_dir)
        # 配置：内容真的搬过来了
        payload = json.loads(self.paths.config_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["providers"][0]["id"], "legacy-provider")
        # 数据库：连对话一起搬
        self.assertTrue(self.paths.data_dir.joinpath("chat.db").is_file())
        self.assertEqual(_count_conversations(self.paths.data_dir / "chat.db"), 1)
        # 目录树：uploads 递归合并到位
        self.assertTrue((self.paths.data_dir / "uploads" / "old.png").is_file())
        # 运行期易变档不跨目录搬运（状态档每次启动重写、实例锁属于旧进程）
        self.assertFalse((self.paths.data_dir / "server.json").exists())
        self.assertFalse((self.paths.data_dir / "server.lock").exists())

    def test_existing_server_json_does_not_abort_merge(self) -> None:
        # 事故现场：新数据目录里已有上一次启动写下的 server.json（旧目录里的那份是文件）。
        self._seed_legacy()
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        existing = self.paths.data_dir / "server.json"
        existing.write_text('{"pid": 999, "port": 9999}', encoding="utf-8")
        report = migrate_legacy_data(self.paths)
        self.assertTrue(report["data"], "server.json 之后的数据项必须继续迁移（WinError 183 回归）")
        self.assertTrue(
            (self.paths.data_dir / "uploads" / "old.png").is_file(),
            "uploads 排在 server.json 之后，中断时会被静默丢掉",
        )
        self.assertEqual(existing.read_text(encoding="utf-8"), '{"pid": 999, "port": 9999}')

    def test_plain_file_copied_once_and_never_overwritten(self) -> None:
        legacy_data = self._seed_legacy(note=True)
        migrate_legacy_data(self.paths)
        copied = self.paths.data_dir / "custom-note.json"
        self.assertEqual(copied.read_text(encoding="utf-8"), '{"keep": true}')
        # 旧目录内容变化不影响已搬过来、且已被程序使用的那份（目标已有文件优先）
        (legacy_data / "custom-note.json").write_text('{"keep": "legacy-updated"}', encoding="utf-8")
        migrate_legacy_data(self.paths)
        self.assertEqual(copied.read_text(encoding="utf-8"), '{"keep": true}')

    def test_migration_failure_is_reported_instead_of_raised(self) -> None:
        self._seed_legacy()
        with mock.patch.object(
            migration_module,
            "_copy_legacy_data",
            side_effect=TypeError("missing 1 required positional argument: 'paths'"),
        ):
            # 不得抛出：启动路径必须活下来；同时必须留下 error 级日志（§8.4 不吞错）
            with self.assertLogs("naiba.migration", level="ERROR") as captured:
                report = migrate_legacy_data(self.paths)
        self.assertTrue(any("TypeError" in line for line in captured.output))
        self.assertFalse(report["migrated"])
        self.assertIn("TypeError", report["error"])
        warning = self.paths.data_dir / "data-migration-warning.log"
        self.assertTrue(warning.is_file(), "冻结版无控制台，失败必须落到数据目录的日志文件")
        self.assertIn("TypeError", warning.read_text(encoding="utf-8"))

    def test_source_mode_stays_noop(self) -> None:
        legacy_data = self._seed_legacy()
        source_mode = PathContext(
            app_dir=self.exe_dir,
            exe_dir=self.exe_dir,
            resource_dir=self.exe_dir,
            public_dir=(self.exe_dir / "public").resolve(),
            config_path=(self.exe_dir / "config.json").resolve(),
            data_dir=legacy_data,
            status_path=(legacy_data / "server.json").resolve(),
            lock_path=(legacy_data / "server.lock").resolve(),
        )
        report = migrate_legacy_data(source_mode)
        self.assertEqual(report, {"migrated": False, "config": False, "data": False, "source": ""})

    def test_frozen_app_assembly_with_legacy_files_beside_exe(self) -> None:
        self._seed_legacy()
        from naiba.app import NaibaChatApp

        app = NaibaChatApp(paths=self.paths)
        self.addCleanup(app.stop)
        self.assertTrue(app.data_migration["migrated"])
        self.assertNotIn("error", app.data_migration)


if __name__ == "__main__":
    unittest.main()
