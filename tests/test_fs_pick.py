# -*- coding: utf-8 -*-
"""原生选择器（KVMem 安装目录 / 模型文件）：只验参数与返回约定，不真弹窗。

tkinter 在无桌面/CI 上不可用，这里用桩替身接管 `tkinter` 与 `tkinter.filedialog`，
把对话框「选中了什么」变成可控变量，验证：
- 文件模式不按扩展名过滤（本地模型有 .gguf，也有 .ninfer / .safetensors 分片）；
- 目录模式与文件模式走不同的对话框，标题/初始目录按调用方传；
- 取消返回空串，不抛异常（前端据此什么都不改）。
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from naiba.app import NaibaChatApp


class _FakeRoot:
    def withdraw(self) -> None:
        pass

    def attributes(self, *_args) -> None:
        pass

    def destroy(self) -> None:
        pass


def _install_tk_stub(selected: str) -> tuple:
    """装上 tkinter 桩；返回 (tk 模块, filedialog 模块, askdirectory 收到的参数, askopenfilename 收到的参数)。"""
    directory_calls: dict = {}
    file_calls: dict = {}

    def askdirectory(**kwargs):
        directory_calls.update(kwargs)
        return selected

    def askopenfilename(**kwargs):
        file_calls.update(kwargs)
        return selected

    fake_tk = types.ModuleType("tkinter")
    fake_tk.Tk = _FakeRoot
    fake_dialog = types.ModuleType("tkinter.filedialog")
    fake_dialog.askdirectory = askdirectory
    fake_dialog.askopenfilename = askopenfilename
    fake_tk.filedialog = fake_dialog
    return fake_tk, fake_dialog, directory_calls, file_calls


class PickFilesystemPathTest(unittest.TestCase):
    def _app(self, exe_dir: Path) -> NaibaChatApp:
        app = NaibaChatApp.__new__(NaibaChatApp)
        app._paths = SimpleNamespace(exe_dir=exe_dir)
        return app

    def _call(self, app: NaibaChatApp, selected: str, **kwargs):
        fake_tk, fake_dialog, directory_calls, file_calls = _install_tk_stub(selected)
        with mock.patch.dict(sys.modules, {"tkinter": fake_tk, "tkinter.filedialog": fake_dialog}):
            result = app.pick_filesystem_path(**kwargs)
        return result, directory_calls, file_calls

    def test_file_mode_keeps_every_extension(self) -> None:
        app = self._app(Path.cwd())
        result, directory_calls, file_calls = self._call(
            app, "D:\\models\\Qwen3-32B.ninfer", mode="file", title="选择模型文件",
        )
        self.assertEqual("D:\\models\\Qwen3-32B.ninfer", result["path"])
        self.assertFalse(result["cancelled"])
        self.assertEqual("file", result["mode"])
        self.assertEqual({}, directory_calls)
        self.assertEqual("选择模型文件", file_calls.get("title"))
        # 不过滤扩展名：只有一条「所有文件」，不出现 *.gguf 之类的白名单。
        self.assertEqual([("所有文件", "*.*")], file_calls.get("filetypes"))

    def test_dir_mode_returns_native_backslash_path(self) -> None:
        app = self._app(Path.cwd())
        result, directory_calls, file_calls = self._call(
            app, "D:\\kvmem-v0.19.1-windows-x64-nvidia-50-unified", mode="dir",
        )
        self.assertEqual("D:\\kvmem-v0.19.1-windows-x64-nvidia-50-unified", result["path"])
        self.assertEqual("dir", result["mode"])
        self.assertEqual({}, file_calls)
        self.assertTrue(directory_calls.get("mustexist"))

    def test_cancel_returns_empty_without_raising(self) -> None:
        app = self._app(Path.cwd())
        result, _dir_calls, _file_calls = self._call(app, "", mode="file")
        self.assertEqual({"cancelled": True, "path": ""}, result)

    def test_initial_directory_uses_parent_of_existing_file(self) -> None:
        app = self._app(Path.cwd())
        target = Path.cwd() / "README.md"
        result, _dir_calls, file_calls = self._call(app, "", mode="file", initial=str(target))
        self.assertTrue(result["cancelled"])
        self.assertEqual(str(target.parent), file_calls.get("initialdir"))


if __name__ == "__main__":
    unittest.main()
