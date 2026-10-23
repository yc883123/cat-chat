# -*- coding: utf-8 -*-
"""「更新产物」展示守门（设置 → 软件更新：文件夹在哪、占多大）。

背景（2026-10-23）：用户能看见下载进度，却无法确认更新包落在哪个目录、失败/取消/放弃后
还有没有残留文件。本文件钉住三条契约：

1. 目录与文件信息**只来自** `/api/update` 的 ``artifacts``，前端不得自己拼路径；
2. 四个状态各有用户视角文案，清理失败必须显示残留而不是「已清理」，且接口缺字段时
   保留上一次的有效信息（不能把已知信息清空成「没有保留」）；
3. 路径用 ``textContent`` 写入、可换行、可选中；不提供打开目录的能力。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _function(source: str, header: str) -> str:
    start = source.index(header)
    body = source[start:]
    return body[: body.index("\n}\n") + 3]


class UpdateArtifactsUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = _read("public/index.html")
        self.source = _read("public/js/07-models-agents.js")
        self.bind = _read("public/js/15-bind-events.js")
        self.css = _read("public/styles.css")

    def test_markup_exposes_directory_and_file_row(self) -> None:
        self.assertIn('id="updateArtifactsDir"', self.index)
        self.assertIn('id="updateArtifactsFile"', self.index)
        self.assertIn('id="copyUpdateArtifactsDir"', self.index)
        self.assertIn('aria-label="复制更新文件夹路径"', self.index, "图标按钮必须有可访问名称")
        self.assertIn("更新文件夹", self.index)
        # 不承诺「打开目录」：面板里不出现资源管理器入口。
        self.assertNotIn("explorer.exe", self.index)

    def test_render_status_feeds_artifacts_from_backend_only(self) -> None:
        render = _function(self.source, "export function renderUpdateStatus(status) {")
        self.assertIn("renderUpdateArtifacts(status.artifacts);", render)
        artifacts = _function(self.source, "function renderUpdateArtifacts(artifacts) {")
        self.assertIn("artifacts.directory", artifacts, "目录必须用后端字段")
        self.assertNotIn("data_dir", artifacts)
        self.assertNotIn("naiba-chat-", artifacts, "前端不得自己拼安装包文件名")
        self.assertIn("if (!artifacts || typeof artifacts !== 'object') return;", artifacts,
                      "缺字段时保留上一次有效信息，不误报已清理")
        self.assertIn("textContent", artifacts, "路径与文件名要用 textContent 写入")
        self.assertNotIn("innerHTML", artifacts)

    def test_states_have_user_facing_copy(self) -> None:
        artifacts = _function(self.source, "function renderUpdateArtifacts(artifacts) {")
        for state in ("downloading", "ready", "cleanup_failed"):
            self.assertIn(f"state === '{state}'", artifacts, f"缺少 {state} 状态分支")
        self.assertIn("正在下载", artifacts)
        self.assertIn("重启后生效", artifacts)
        self.assertIn("未能清理", artifacts, "清理失败必须如实显示残留")
        self.assertIn("当前没有保留的更新包", artifacts)
        self.assertIn("update-artifacts-warn", artifacts)

    def test_copy_button_copies_the_shown_path(self) -> None:
        self.assertIn("copyUpdateArtifactsDir", self.bind)
        self.assertIn("await copyText(path);", self.bind)
        self.assertIn("复制失败，请手动选择路径", self.bind, "剪贴板被拒时要给出普通文本路径")

    def test_long_path_wraps_and_is_selectable(self) -> None:
        self.assertIn(".settings-dialog .update-status > div.update-artifacts { display: block;", self.css,
                      "flex 会让长路径没有换行空间")
        rule = self.css[self.css.index(".update-artifacts-row code {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("overflow-wrap: anywhere", rule)
        self.assertIn("user-select: text", rule)


if __name__ == "__main__":
    unittest.main()
