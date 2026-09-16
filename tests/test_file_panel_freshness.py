# -*- coding: utf-8 -*-
"""护栏：文件面板「点击即重读」，不得再复用首次读取的缓存。

需求（用户原话）：AI 生成文件后点开预览，随后 AI 又改了该文件，再次点击打开仍显示旧内容，
必须关掉右侧栏浏览再开才能看到新内容。

修复口径（2026-09-16）：
- `loadFileTab(tab, force)`：force=true 忽略已缓存的 tab.info，重新从磁盘读取；
- 新增 `reloadFileTab(tab)`：草稿保护 + 节流，`openFilePanel` / `activateFileTab` /
  `reopenFilePanel` 三个入口统一走它（不再有「仅 !tab.info 才加载」的单条件分支）；
- 强制重读期间保留旧内容渲染（不闪「文件尚未加载」空态），失败保留旧内容并提示；
- 图片 URL 追加 `v=<mtime>-<size>` 版本参数，绕过 `/file/raw` 的浏览器缓存。

不变量：
- `filePanelUsable()` / 视口边界见 tests/test_mobile_parity.py，本文件不重复守门；
- 编辑中且有改动时不得静默覆盖用户草稿。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PANEL_JS = (ROOT / "public/js/14-file-panel.js").read_text(encoding="utf-8")


class FilePanelForceReloadTests(unittest.TestCase):
    def test_load_file_tab_accepts_force_flag(self):
        self.assertIn("export async function loadFileTab(tab, force = false)", PANEL_JS)
        # 有缓存且未强制时短路；force 必须能穿透缓存
        self.assertIn("(tab.info && !force)", PANEL_JS)

    def test_reload_file_tab_exists_and_is_reused(self):
        self.assertIn("export function reloadFileTab(tab)", PANEL_JS)
        # 定义 1 次 + openFilePanel / activateFileTab / reopenFilePanel 各 1 次
        self.assertEqual(PANEL_JS.count("reloadFileTab("), 4)

    def test_open_file_panel_has_no_cache_only_branch(self):
        self.assertNotIn("if (tab && !tab.info && !tab.loading) loadFileTab(tab);", PANEL_JS)
        self.assertNotIn("if (!tab.info && !tab.loading && !tab.error) loadFileTab(tab);", PANEL_JS)

    def test_draft_guard_present(self):
        self.assertIn("tab.editing && tab.draft !== null && tab.draft !== tab.info?.content", PANEL_JS)
        self.assertIn("文件正在编辑中，已保留你的修改", PANEL_JS)

    def test_reload_keeps_old_content_on_failure_and_no_blank_flash(self):
        self.assertIn("重新读取失败，仍显示上次内容", PANEL_JS)
        self.assertIn("if (tab.loading && !info)", PANEL_JS)
        self.assertIn("if (tab.error && !info)", PANEL_JS)

    def test_image_url_carries_mtime_size_version(self):
        self.assertIn("export function fileVersionToken(info)", PANEL_JS)
        self.assertIn("&v=${encodeURIComponent(version)}", PANEL_JS)
        # 图片 <img> 与大图灯箱必须用同一份版本化 URL
        self.assertIn("convFileRawUrl(info.path || tab.raw, fileVersionToken(info))", PANEL_JS)

    def test_reload_is_throttled(self):
        self.assertIn("FILE_RELOAD_THROTTLE_MS", PANEL_JS)
        self.assertIn("tab.refreshedAt", PANEL_JS)

    def test_save_reloads_through_force_path(self):
        self.assertIn("await loadFileTab(tab, true)", PANEL_JS)


if __name__ == "__main__":
    unittest.main()
