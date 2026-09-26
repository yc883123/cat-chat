# -*- coding: utf-8 -*-
"""教程文档守门：图片必须落地、交叉链接必须存在、知识库镜像必须与正文同步。

**为什么要有这个文件**：2026-09-25 那一批「半成品入库」是三种缺陷同时发生，而它们
一条都不触发任何既有单测——

① 7 篇 README 的 `![...](images/…)` 全是坏图（截图压根没拍，文字先推上公开仓库）；
② `docs/tutorial-02-comfyui/` 整篇没入库，可另外 7 篇都在链它 → 点过去 404；
③ 一版截图目录里的真文件名（`tut-01-main.png`）与 README 引用（`01-home.png`）对不上。

三条都是「文档与仓库实际内容对不上」，用户点开公开仓库就是一堆破图。所以这里把
**引用 → 文件**、**文件 → 引用**、**链接 → 目标**、**正文 → 知识库镜像** 四个方向都钉住：

- 前三条只比对文件系统的存在性，不需要跑任何产品代码；
- 第四条把 `scripts/backfill_guide_references.py`（教程正文 → 教程助手知识库）的
  转换函数直接拿来重跑一遍并与 `references/` 里的文件逐字节比对——**忘了回填就变红**，
  而不是等用户问教程助手发现它按旧版正文回答。
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SCRIPTS = ROOT / "scripts"
REFS = ROOT / "skills" / "cat-chat-guide" / "references"

# `![](images/…)`：只取圆括号里的目标，允许 `!` 与 `[` 之间没有文字。
IMAGE_REF = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
# `[文字](目标)`：排除图片语法（前面紧邻 `!`），否则图片会被当成链接重复检查。
LINK_REF = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")

EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#", "tel:")


def tutorial_dirs() -> list[Path]:
    return sorted(p for p in DOCS.glob("tutorial-*") if p.is_dir())


def read_text(path: Path) -> str:
    """按 UTF-8 读文本。

    仓库同时存在 CRLF（Windows 检出）与 LF（脚本产出）两套行尾，
    凡是要做**内容比对**的地方都必须先归一到 LF，否则「本地绿 CI 红」。
    """
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def load_backfill():
    """把 `scripts/backfill_guide_references.py` 当模块加载。

    `scripts/` 不是包（没有 `__init__.py`），也不在 `sys.path` 上，所以只能按路径加载——
    但**必须加载真文件**：把转换规则在测试里抄一份就会两边漂移，
    那样这个守门就只剩「镜像不是空的」这种没用的判据了。
    """
    spec = importlib.util.spec_from_file_location(
        "_backfill_guide_references_for_test",
        SCRIPTS / "backfill_guide_references.py",
    )
    assert spec and spec.loader, "找不到 scripts/backfill_guide_references.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TutorialLayoutTests(unittest.TestCase):
    """目录与正文的自洽性。"""

    def test_every_tutorial_dir_has_readme(self) -> None:
        dirs = tutorial_dirs()
        self.assertTrue(dirs, "docs/ 下找不到任何 tutorial-* 目录")
        for directory in dirs:
            self.assertTrue(
                (directory / "README.md").is_file(),
                f"{directory.name} 没有 README.md —— 空目录不该入库",
            )

    def test_image_references_resolve(self) -> None:
        """README 里引用的每一张图都必须在磁盘上。

        **这是本项目踩过的坑**：坏图引用不会让任何构建失败，但用户点开仓库就看到破图。
        """
        missing: list[str] = []
        for directory in tutorial_dirs():
            text = read_text(directory / "README.md")
            for target in IMAGE_REF.findall(text):
                if target.startswith(EXTERNAL_PREFIXES):
                    continue
                if not (directory / target).is_file():
                    missing.append(f"{directory.name}/README.md → {target}")
        self.assertEqual(
            missing, [],
            "以下图片引用找不到文件（要么补拍截图，要么把引用从正文里摘掉）：\n  "
            + "\n  ".join(missing),
        )

    def test_no_orphan_image_files(self) -> None:
        """反向：`images/` 里的每个文件都必须被 README 引用到。

        防的正是「半成品」的典型形态——截图目录里躺着一批文件名与引用对不上的图。
        """
        orphans: list[str] = []
        for directory in tutorial_dirs():
            images = directory / "images"
            if not images.is_dir():
                continue
            text = read_text(directory / "README.md")
            referenced = {
                Path(target).name
                for target in IMAGE_REF.findall(text)
                if not target.startswith(EXTERNAL_PREFIXES)
                and Path(target).parent.name == "images"
            }
            for item in sorted(images.iterdir()):
                if item.is_file() and item.name not in referenced:
                    orphans.append(f"{directory.name}/images/{item.name}")
        self.assertEqual(
            orphans, [],
            "以下图片没有任何 README 引用它（删掉，或在正文里补上引用）：\n  "
            + "\n  ".join(orphans),
        )

    def test_relative_links_resolve(self) -> None:
        """篇与篇之间的相对链接必须指向真实存在的文件。

        教程 8 的速查表一口气链了另外 7 篇——**只要有一篇没入库，就是 7 处 404**，
        所以这条必须逐个链接地查，而不是抽查。
        """
        broken: list[str] = []
        for directory in tutorial_dirs():
            text = read_text(directory / "README.md")
            for target in LINK_REF.findall(text):
                if target.startswith(EXTERNAL_PREFIXES):
                    continue
                path_part = target.split("#", 1)[0].strip()
                if not path_part:
                    continue
                if not (directory / path_part).exists():
                    broken.append(f"{directory.name}/README.md → {target}")
        self.assertEqual(
            broken, [],
            "以下相对链接指向不存在的路径：\n  " + "\n  ".join(broken),
        )


class GuideMirrorTests(unittest.TestCase):
    """教程正文 ↔ 教程助手知识库镜像。"""

    def setUp(self) -> None:
        self.module = load_backfill()

    def test_every_tutorial_is_registered_in_guide_plan(self) -> None:
        """新增一篇教程却没登记进 PLAN ⇒ 教程助手那一篇答不出来。"""
        planned = {folder for _, folder, _, _ in self.module.PLAN}
        actual = {p.name for p in tutorial_dirs()}
        self.assertEqual(
            sorted(actual - planned), [],
            "以下教程目录没登记进 backfill 的 PLAN（教程助手会答不出这一篇）",
        )
        self.assertEqual(
            sorted(planned - actual), [],
            "PLAN 里登记了 docs/ 下不存在的教程目录",
        )

    def test_mirror_targets_exist_and_are_flat(self) -> None:
        """镜像文件名必须是 `NN-….md` 平铺在 references/ 下。"""
        for _, _, target, _ in self.module.PLAN:
            self.assertTrue(
                (REFS / target).is_file(), f"知识库镜像缺失：{target}"
            )
        for item in REFS.glob("*.md"):
            self.assertRegex(
                item.name, r"^\d\d-.*\.md$",
                f"{item.name} 不符合 `NN-….md` 命名（教程助手按序索引它们）",
            )

    def test_mirror_matches_tutorial_readme(self) -> None:
        """镜像必须**逐字节**等于「用当前正文重跑一遍转换」的结果。

        判据取「重跑转换函数」，不取「镜像里没有 images/ 字样」——
        后者在正文改了措辞（比如步骤从 5 条改成 7 条）时照样全绿，
        而那正是教程助手会照着答错步骤的场景。
        """
        stale: list[str] = []
        for index, folder, target, title in self.module.PLAN:
            source = DOCS / folder / "README.md"
            head = (
                f"> 来源：仓库图文教程 `docs/{folder}/README.md`"
                "（图文版带截图；本文件是**文本版**，供教程助手引用）。\n"
                f"> 「3 分钟」系列第 {index} 篇「{title}」，共 8 篇。\n\n---\n\n"
            )
            expected = head + self.module.convert(read_text(source))
            actual = read_text(REFS / target)
            if actual != expected:
                stale.append(
                    f"{target}（正文 {folder}/README.md 改过了）"
                )
        self.assertEqual(
            stale, [],
            "以下知识库镜像与教程正文不同步——改完正文要重跑 "
            "`python scripts/backfill_guide_references.py`：\n  " + "\n  ".join(stale),
        )


if __name__ == "__main__":
    unittest.main()
