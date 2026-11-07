# -*- coding: utf-8 -*-
"""护栏：SkillCatalog 扫描（曾在 bootstrap 路径上因辅助函数缺失导致 NameError→500→前端登录弹窗）。

保护对象：后续 skill_runtime 拆分（阶段 2 剩余）时，扫描/前端元数据解析链路的完整性。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.skills.catalog import SkillCatalog  # noqa: E402


class SkillCatalogScanTests(unittest.TestCase):
    def test_scan_reads_frontmatter_and_display_name(self):
        # 注意：扫描会跳过路径中任何以 "." 开头的目录段（如沙箱 verify），
        # 因此临时目录必须建在非点开头的位置（workspace 根下）。
        import tempfile

        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=str(workspace)) as tmp:
            root = Path(tmp) / "skills"
            skill_dir = root / "demo-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: demo\ndescription: 演示技能\n---\n\n# 演示\n",
                encoding="utf-8",
            )
            catalog = SkillCatalog([root])
            items = catalog.scan()
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item.get("name"), "demo")
            self.assertEqual(item.get("description"), "演示技能")
            self.assertEqual(item.get("id"), catalog.by_id()[item["id"]]["id"])


class SkillCatalogBundleTests(unittest.TestCase):
    """完整目录文本包（贴签匠「载入完整目录」）：SKILL.md 正文 + 文本资产，脚本只报名单。"""

    def _make_skill(self, root: Path) -> SkillCatalog:
        skill_dir = root / "demo-bundle"
        (skill_dir / "references").mkdir(parents=True)
        (skill_dir / "scripts" / "sub").mkdir(parents=True)
        (skill_dir / "assets").mkdir(parents=True)
        (skill_dir / "agents").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-bundle\ndescription: 演示打包\n---\n\n# 规则正文\n按参考文档输出。",
            encoding="utf-8",
        )
        (skill_dir / "references" / "base.txt").write_text("BASE-规则一。\nBASE-规则二。", encoding="utf-8")
        (skill_dir / "references" / "extra.md").write_text("# 补充\nEXTRA-细则。", encoding="utf-8")
        (skill_dir / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
        (skill_dir / "scripts" / "sub" / "helper.py").write_text("x = 1\n", encoding="utf-8")
        (skill_dir / "assets" / "flow.json").write_text('{"a":\x00b}', encoding="utf-8")  # 合法后缀的二进制
        (skill_dir / "agents" / "openai.yaml").write_text("display_name: 演示\n", encoding="utf-8")
        (skill_dir / ".hidden" ).mkdir()
        (skill_dir / ".hidden" / "x.md").write_text("秘密", encoding="utf-8")
        return SkillCatalog([root])

    def _bundle(self, tmp: str) -> tuple[SkillCatalog, dict]:
        catalog = self._make_skill(Path(tmp) / "skills")
        item = catalog.scan()[0]
        return catalog, catalog.skill_bundle(item["id"])

    def test_bundle_packs_skill_body_and_text_assets(self):
        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=str(workspace)) as tmp:
            catalog, payload = self._bundle(tmp)
            bundle = payload["bundle"]
            # SKILL.md 在最前且已去 frontmatter；references 文本资产带路径分隔头收进包。
            self.assertTrue(bundle.startswith("=== SKILL.md ==="))
            self.assertNotIn("name: demo-bundle", bundle)
            self.assertIn("=== references/base.txt ===", bundle)
            self.assertIn("BASE-规则一。", bundle)
            self.assertIn("=== references/extra.md ===", bundle)
            # scripts 正文绝不入包；agents/ 元数据不入包。
            self.assertNotIn("print('hi')", bundle)
            self.assertNotIn("display_name", bundle)
            self.assertNotIn(".hidden", bundle)
            # 名单与跳过清单：脚本全报（含子目录），二进制资产跳过并注明原因。
            self.assertEqual(payload["script_names"], ["scripts/run.py", "scripts/sub/helper.py"])
            skipped = {row["path"]: row["reason"] for row in payload["skipped"]}
            self.assertEqual(skipped.get("assets/flow.json"), "binary")
            files = {row["path"]: row["chars"] for row in payload["files"]}
            self.assertIn("SKILL.md", files)
            self.assertIn("references/base.txt", files)
            self.assertGreater(payload["bundle_chars"], 0)
            self.assertFalse(payload["truncated"])

    def test_bundle_truncates_per_file_and_total(self):
        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=str(workspace)) as tmp:
            catalog = self._make_skill(Path(tmp) / "skills")
            item = catalog.scan()[0]
            per_file = catalog.skill_bundle(item["id"], max_file_chars=10)["files"]
            truncated_files = [row for row in per_file if row["truncated"]]
            self.assertTrue(truncated_files)
            payload = catalog.skill_bundle(item["id"], max_total_chars=40)
            self.assertTrue(payload["truncated"])
            self.assertLessEqual(payload["bundle_chars"], 40 + 40)  # 截断标记本身会占字数

    def test_bundle_rejects_missing_skill(self):
        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=str(workspace)) as tmp:
            catalog = self._make_skill(Path(tmp) / "skills")
            with self.assertRaises(ValueError):
                catalog.skill_bundle("nonexistent")
            with self.assertRaises(ValueError):
                catalog.skill_bundle("")


if __name__ == "__main__":
    unittest.main()
