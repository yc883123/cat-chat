# -*- coding: utf-8 -*-
"""验证资产的三份名单必须对得上：`verify/` 目录 ↔ `.gitignore` 白名单 ↔ `cleanup_verify.py` 的 KEEP。

为什么要这条护栏：`cleanup_verify.py --apply` 会删掉"不在 KEEP 里"的一切，而"哪些是复用资产"
另有一份口径写在 `.gitignore` 的 `!verify/*` 白名单里（= §六 登记的那批）。2026-09-17 实测两份
名单漂移出 6 个脚本（`q1_context_smoke.*`、`stream_rerender_smoke.*`、`frozen_q1_check.py`、
`frozen_interrupt_check.py`），再漏一步就是 `_serve_tmp.py` 这种"被别的脚本依赖的 harness"被删掉
（§九.91）。当时是靠 `git ls-files` 兜底救回来的——那条兜底留着，但**漂移本身要被拦在这里**，
而不是等某次清理才发现。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "verify"
CLEANUP = VERIFY / "cleanup_verify.py"
GITIGNORE = ROOT / ".gitignore"


def _keep_names() -> set[str]:
    """从 cleanup_verify.py 源码里取出 KEEP 集合的字面量（该模块 import 就会执行清理逻辑，不能直接导入）。

    只认「带扩展名的文件名」形态：KEEP 块里的中文注释也带引号，宽正则会把注释串当成文件名
    （第一版就是这么把自己考挂的）。
    """
    source = CLEANUP.read_text(encoding="utf-8")
    assert "KEEP = {" in source, "cleanup_verify.py 里找不到 KEEP 定义"
    block = source.split("KEEP = {", 1)[1].split("\n}", 1)[0]
    return set(re.findall(r'"([A-Za-z0-9_.\-]+\.[A-Za-z0-9]+)"', block))


def _allow_names() -> set[str]:
    """`.gitignore` 里 `!verify/xxx` 逐条放行的可复用脚本。"""
    lines = GITIGNORE.read_text(encoding="utf-8").splitlines()
    return {
        line.strip()[len("!verify/"):].strip()
        for line in lines
        if line.strip().startswith("!verify/")
    }


class VerifyAssetRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.keep = _keep_names()
        self.allow = _allow_names()
        self.on_disk = {p.name for p in VERIFY.iterdir() if p.is_file()}

    def test_keep_covers_every_whitelisted_script_that_exists(self) -> None:
        missing = (self.allow & self.on_disk) - self.keep
        self.assertEqual(
            set(), missing,
            f"这些脚本被 .gitignore 放行（= §六 登记的复用资产）却不在 cleanup_verify.KEEP 里，"
            f"清理时会被 --apply 删掉：{sorted(missing)}",
        )

    def test_keep_entries_are_all_whitelisted(self) -> None:
        # KEEP 里却不在白名单 ⇒ 该文件不进版本控制，清掉就真没了。
        stray = self.keep - self.allow
        self.assertEqual(
            set(), stray,
            f"这些名字在 cleanup_verify.KEEP 里、却没在 .gitignore 放行，等于保留了" +
            f"一个不受版本控制的文件：{sorted(stray)}",
        )

    def test_shared_harness_is_kept(self) -> None:
        # 维护说明「归档说明」点名要求留在 verify/：它被 tasks_panel_smoke.py 等直接依赖。
        self.assertIn("_serve_tmp.py", self.keep)


if __name__ == "__main__":
    unittest.main()
