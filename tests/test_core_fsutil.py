# -*- coding: utf-8 -*-
"""护栏：core.fsutil 原子写 + 短重试（远程实例实测过 WinError 5 拒绝访问）。

场景：任务卡突然整单失败、点重试就好——`os.replace` 撞上正被打开的目标文件
（本服务自己的轮询读、或杀软/索引器握着句柄）。覆盖：
替换撞占用时在窗口内重试成功；占用不消失时按上限失败；Studio 任务记录写路径
在「读者握着旧文件」时不整单失败；写完不残留临时文件。
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.fsutil import atomic_write_json  # noqa: E402
from naiba.studio.store import StudioStore  # noqa: E402

IS_WINDOWS = sys.platform == "win32"


class _HoldFile:
    """在后台线程里握住一个文件的句柄一小段时间（模拟轮询读/杀软占用）。"""

    def __init__(self, path: Path, hold_seconds: float):
        self.path = path
        self.hold_seconds = hold_seconds
        self._opened = threading.Event()
        self._released = threading.Event()

    def __enter__(self):
        def hold():
            with self.path.open("rb"):
                self._opened.set()
                time.sleep(self.hold_seconds)
            self._released.set()

        threading.Thread(target=hold, daemon=True).start()
        self._opened.wait(timeout=5)
        return self

    def __exit__(self, *exc):
        self._released.wait(timeout=5)


class AtomicWriteRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_retry_succeeds_when_lock_released_in_window(self):
        """占用毫秒级消失：第一次替换失败、窗口内重试成功。"""
        path = self.root / "a.json"
        atomic_write_json(path, {"v": 1})
        with _HoldFile(path, 0.1):
            atomic_write_json(path, {"v": 2})  # 默认窗口 8×30ms > 100ms 占用
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"v": 2})
        self.assertEqual([p.name for p in self.root.iterdir()], ["a.json"])

    @unittest.skipUnless(IS_WINDOWS, "POSIX 替换打开中的文件是合法的，占不满重试窗口")
    def test_gives_up_after_attempts_when_lock_persists(self):
        """占用不消失：按上限次数重试后原样抛 PermissionError，不无限等。"""
        path = self.root / "b.json"
        atomic_write_json(path, {"v": 1})
        with self.assertRaises(PermissionError), _HoldFile(path, 0.5):
            atomic_write_json(path, {"v": 2}, attempts=2, delay=0.01)
        # 失败不动已有内容。
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"v": 1})


class StudioStoreWriteRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StudioStore(Path(self.tmp.name))

    def test_update_task_survives_reader_holding_record(self):
        """真档回归：更新任务记录时读者正握着旧文件——不整单失败。"""
        task = self.store.create_task({"content": "x"})
        path = Path(self.tmp.name) / "studio" / "tasks" / f"{task['id']}.json"
        with _HoldFile(path, 0.1):
            updated = self.store.update_task(task["id"], status="running")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["status"], "running")

    def test_write_leaves_no_tmp_files(self):
        self.store.create_task({"content": "x"})
        tasks_dir = Path(self.tmp.name) / "studio" / "tasks"
        leftovers = [p.name for p in tasks_dir.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
