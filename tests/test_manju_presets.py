# -*- coding: utf-8 -*-
"""AI 漫剧原版 Skill 与 Studio 派生模板的离线守门。

Golden 指纹来自获批上游 commit；运行时只读仓库，不依赖下载缓存、网络或模型。
CLI 只操作 TemporaryDirectory 的副本，不重建或改写真实 public/studio。
"""

import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.skills.catalog import SkillCatalog  # noqa: E402
from naiba.studio.catalog import StudioCatalog  # noqa: E402
from verify import studio_catalog_build as build  # noqa: E402

SKILLS = ROOT / "skills" / "ai-manju-skills"
PUBLIC = ROOT / "public" / "studio"
REPOSITORY = "https://github.com/zhanghao123vip/ai-manju-skills"
COMMIT = "77fb32a53a00ca7f455cba6a74ec1b05e1a63fb4"
LICENSE_SHA256 = "d1fe23143f5d054dc0d7141b01ae8deae82e44bfe7e501c3fd1c484f7500d3c9"
# slug: (中文题材, 分组, 原版字节数, 原版 SHA-256)。独立于可变 manifest。
EXPECTED = {
    "xuanhuan": ("玄幻修仙", "幻想", 20010, "3520deac5c951adfb80c750d6475e8f9e85c865f31b732ce2f1af3f5758e7a0a"),
    "xihuan": ("西幻魔法", "幻想", 20154, "4649b3d538b0e75d4aeb3b9d7169df09b4c2a153509c9e6ff8d408cfa085735d"),
    "xingji": ("星际机甲", "幻想", 20521, "91f04f9920e3c21e937948f3d7af36ac5e8f5dfec843a2c03e8cc6f25ede5a19"),
    "xitong": ("系统无限流", "幻想", 20494, "6477ba30dfab52a52e3984f30bb61936ab818a248ec53bbae525425d8b5894f5"),
    "moshi": ("末日废土", "幻想", 20732, "16aaee3c1905742a6ae2bdac0a4659afa7bc200da19c399170a77afbe1f99375"),
    "chuanyue": ("穿越重生", "古代", 20400, "2d13a8c63318b1d4c78c9e10ce4fe3999473207eb4c37637a76d784cd5d83c58"),
    "gongdou": ("宫斗", "古代", 20323, "2923dc316eaf812aad455bd3eb1c5aa7891d9a9924562f1ddbb2834157ee01eb"),
    "zhaidou": ("宅斗", "古代", 20099, "de0c994f035464bc4ac993959a7db428506f64c18fb67dbd65a2fa41fb296e94"),
    "quanmou": ("权谋朝堂", "古代", 20090, "ca11a1a7497386af1fc0363f1ee89dcc985ca6eb60c19a9df15e02309b3c37dc"),
    "wuxia": ("武侠江湖", "古代", 19934, "2c0f1a17e9acb9d1a2170633df8c4dd3b1245a9b8ee3432f11760d5df7e00483"),
    "zhongtian": ("种田经商", "古代", 20086, "28ac7be35e45065ef8edb5c02bbfd41cc9379987f3643b3a89b298d112e790b5"),
    "dushi": ("都市逆袭", "现代", 20298, "278a9c0fcdf2f558e15f2667ce6a8d0ed6aa7514edc5a1fcde8eecf7dd349590"),
    "tianchong": ("总裁甜宠", "现代", 20281, "fc0c082da4f7eb6b0dc6c94391e95bbc82d5f98b4f85da47793c77470e6c7aae"),
    "jiating": ("家庭伦理", "现代", 20598, "ab2858ea28da674d37a03c87c852f7bb2dc7831ac766a5d69b1789fce20b465f"),
    "chongsheng": ("重生复仇", "现代", 20701, "00921ddde81efce17494b853d44f9dede9bc10fdecc7d127ea08e26867f1dc67"),
    "shangzhan": ("职场商战", "现代", 20017, "24e67707641071cc1df129c0798063771100e7a9d41c9041554bd7bb0f2a1aae"),
    "niandai": ("年代文", "现代", 20205, "0a158cc0f33a2f07a59063b607c1346cadc7b294b20181aff3cfc2aae36bb697"),
    "yule": ("娱乐圈", "现代", 20079, "509c878cd05b3ad7be440b92c8a3f19ed04c507161f03f065d83d07d0384f373"),
    "xiaoyuan": ("大学校园青春", "现代", 19927, "a0fd16086d64b16ed4c43298c91dd46abfb5b6a77cb1642620604ecd4dd3cba2"),
    "xuanyi": ("悬疑刑侦", "类型片", 20623, "080282384fe82d49d97e86a8025269d3c72a9a3a416cbbd7f3d36ebc4d3cd031"),
    "minsu": ("民俗怪谈", "类型片", 20544, "5c38535b512c6c273323d8fb7451f2045180f3825854717a6577af7ecc704c68"),
}
IDS = {f"script-manju-{slug}" for slug in EXPECTED}
GROUPS = {"幻想", "古代", "现代", "类型片"}


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _snapshot(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes()
            for p in directory.rglob("*") if p.is_file()}


def _raw(slug):
    return (SKILLS / "references" / f"{slug}-manju.md").read_text(encoding="utf-8")


def _family(source_dir=None):
    # 同现有 script_family 的五元组；不复制转换器实现进测试。
    return build.manju_family(source_dir) if source_dir is not None else build.manju_family()


class ManjuOriginalSkillTests(unittest.TestCase):
    def test_exact_original_files_bytes_and_manifest(self):
        expected_paths = {f"{slug}-manju/SKILL.md" for slug in EXPECTED}
        expected_local_paths = {f"references/{slug}-manju.md" for slug in EXPECTED}
        self.assertEqual({p.relative_to(SKILLS).as_posix() for p in SKILLS.rglob("SKILL.md")}, {"SKILL.md"})
        self.assertEqual({p.relative_to(SKILLS).as_posix() for p in (SKILLS / "references").rglob("*")
                          if p.is_file()}, expected_local_paths)
        self.assertEqual({p.name for p in SKILLS.iterdir() if p.is_dir()}, {"agents", "references"})
        manifest = _json(SKILLS / "source.json")
        self.assertEqual(manifest["repository"], REPOSITORY)
        self.assertEqual(manifest["commit"], COMMIT)
        self.assertEqual(manifest["license"], "MIT")
        files = manifest["files"]
        self.assertEqual(len(files), 21)
        self.assertEqual({row["path"] for row in files}, expected_paths)
        self.assertEqual({row["local_path"] for row in files}, expected_local_paths)
        by_path = {row["path"]: row for row in files}
        for slug, (title, group, byte_count, sha256) in EXPECTED.items():
            with self.subTest(slug=slug):
                path = f"{slug}-manju/SKILL.md"
                local_path = f"references/{slug}-manju.md"
                data = (SKILLS / local_path).read_bytes()
                self.assertEqual(len(data), byte_count)
                self.assertEqual(hashlib.sha256(data).hexdigest(), sha256, "原文必须逐字节保存，含原 frontmatter 与换行")
                row = by_path[path]
                self.assertEqual(row["local_path"], local_path)
                self.assertFalse((SKILLS / path).exists(), "不能保留旧的独立 Skill 入口")
                self.assertEqual((row["title"], row["group"], row["bytes"], row["sha256"]),
                                 (title, group, byte_count, sha256))
        license_data = (SKILLS / "LICENSE").read_bytes()
        self.assertEqual(hashlib.sha256(license_data).hexdigest(), LICENSE_SHA256)
        self.assertIn(b"Copyright (c) 2026 zhanghao123vip", license_data)
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("skills/ai-manju-skills/** -text", attributes.splitlines(),
                      "原字节校验资源不能被 Windows core.autocrlf 改写")

    def test_real_scan_has_exactly_one_correct_frontmatter_skill(self):
        catalog = SkillCatalog([SKILLS], base_dir=ROOT)
        rows = catalog.scan()
        self.assertEqual(len(rows), 1, "21份原文是 references，不是21个独立 Skill")
        row = rows[0]
        text = (SKILLS / "SKILL.md").read_text(encoding="utf-8")
        front = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(front)
        name = re.search(r"(?m)^name:\s*(.+)$", front[1])
        self.assertIsNotNone(name)
        self.assertEqual(name[1].strip().strip("\"'"), "ai-manju-skills")
        self.assertEqual(row["name"], "AI 漫剧（21题材）")
        self.assertEqual(row["ref"], "ai-manju-skills")
        self.assertEqual(row["source"], "builtin")
        self.assertEqual(Path(row["path"]).resolve(), (SKILLS / "SKILL.md").resolve())
        self.assertEqual(Path(row["root"]).resolve(), SKILLS.resolve())
        self.assertEqual(catalog.read_skill_content(Path(row["path"])), text)
        self.assertEqual(row["char_count"], len(text), "扫描只加载入口，不拼接21份原文")
        self.assertNotEqual(row["description"], "未提供描述")
        self.assertEqual(set(catalog.by_id()), {row["id"]})
        metadata = (SKILLS / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertRegex(metadata, r'(?m)^\s*display_name:\s*["\']?AI 漫剧（21题材）["\']?\s*$')

    def test_single_entry_indexes_all_references_and_reads_them_on_demand(self):
        text = (SKILLS / "SKILL.md").read_text(encoding="utf-8")
        paths = re.findall(r"references/[A-Za-z0-9_-]+\.md", text)
        self.assertEqual(set(paths), {f"references/{slug}-manju.md" for slug in EXPECTED})
        for slug, (title, _, _, _) in EXPECTED.items():
            with self.subTest(slug=slug):
                self.assertTrue(any(title in line and f"references/{slug}-manju.md" in line
                                    for line in text.splitlines()), "每个题材须有可达的相对路径索引")
        self.assertIn("read_file", text)
        self.assertRegex(text, r"\broot\b")
        self.assertRegex(text, r"按需|读取选定的一份|只读取一份")
        self.assertRegex(text, r"(?:不要|禁止|不应|不得|无需|不一次)[^\n。]*(?:全部|所有|21)")

    def test_no_builtin_name_ref_or_id_collisions(self):
        rows = SkillCatalog([ROOT / "skills"], base_dir=ROOT).scan()
        manju = [r for r in rows if SKILLS.resolve() in Path(r["path"]).resolve().parents]
        other = [r for r in rows if r not in manju]
        self.assertEqual(len(manju), 1, "整库扫描只能发现根入口，不能把 references 当作独立 Skill")
        self.assertEqual(manju[0]["ref"], "ai-manju-skills")
        self.assertEqual(manju[0]["name"], "AI 漫剧（21题材）")
        self.assertEqual(Path(manju[0]["path"]).resolve(), (SKILLS / "SKILL.md").resolve())
        for key in ("name", "ref", "id"):
            values = [r[key].casefold() for r in manju]
            self.assertEqual(len(values), len(set(values)), key)
            self.assertFalse(set(values) & {r[key].casefold() for r in other}, key)

    def test_freezer_packages_license_manifest_and_every_original_at_same_location(self):
        # 只执行 spec 的纯 Tree 收集函数与 datas 表达式；绝不执行构建、git 或 build_info 写入。
        tree = ast.parse((ROOT / "naiba-chat.spec").read_text(encoding="utf-8"))
        declarations = [node for node in tree.body if
                        (isinstance(node, ast.FunctionDef) and node.name == "Tree") or
                        (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                         t.id == "private_skill_dirs" for t in node.targets))]
        namespace = {"Path": Path, "root": ROOT, "build_info": ROOT / "build_info.json",
                     "icon_path": ROOT / "icon.ico"}
        exec(compile(ast.Module(body=declarations, type_ignores=[]), "<spec-data-only>", "exec"), namespace)
        analysis = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name) and node.func.id == "Analysis")
        datas = next(kw.value for kw in analysis.keywords if kw.arg == "datas")
        pairs = eval(compile(ast.Expression(datas), "<spec-datas-only>", "eval"), namespace)
        packed = {Path(dest).joinpath(Path(source).name).as_posix() for source, dest in pairs}
        expected = {"SKILL.md", "agents/openai.yaml", "LICENSE", "source.json",
                    *(f"references/{s}-manju.md" for s in EXPECTED)}
        manju_packed = {path.removeprefix("skills/ai-manju-skills/") for path in packed
                        if path.startswith("skills/ai-manju-skills/")}
        self.assertEqual(manju_packed, expected, "打包必须包含单入口、metadata、来源许可与21份原文，不残留旧入口")


class ManjuStudioTests(unittest.TestCase):
    def setUp(self):
        self.items, self.bodies, self.rejects, self.stats, self.descriptive = _family()
        self.by_id = {row["id"]: row for row in self.items}

    def test_exact_family_metadata_and_reproducible_conversion(self):
        self.assertEqual(len(self.items), 21)
        self.assertEqual(set(self.by_id), IDS)
        self.assertEqual(set(self.bodies), IDS)
        self.assertEqual(self.rejects, [])
        self.assertEqual((self.items, self.bodies, self.rejects, self.stats, self.descriptive), _family(SKILLS))
        self.assertEqual(build.cross_check(self.items, self.bodies), {})
        for slug, (title, group, _, _) in EXPECTED.items():
            with self.subTest(slug=slug):
                row = self.by_id[f"script-manju-{slug}"]
                self.assertEqual((row["module"], row["type"], row["origin"]), ("script", "template", "builtin"))
                self.assertEqual(row["genre"], title)
                self.assertIn(title, row["title"])
                self.assertTrue({"漫剧", "全流程", group}.issubset(row["tags"]))
                self.assertEqual(set(row["tags"]) & GROUPS, {group})
                self.assertIn(COMMIT, row["source"], "来源必须锚定 commit，不使用可变 main/master")
                self.assertIn("ai-manju-skills", row["source"])
                self.assertIn(f"{slug}-manju/SKILL.md", row["source"])
                self.assertEqual(row["source_sha1"], hashlib.sha1(self.bodies[row["id"]].encode("utf-8")).hexdigest())

    def test_original_genre_rhythm_defaults_and_planning_details_survive(self):
        for slug in EXPECTED:
            with self.subTest(slug=slug):
                original = _raw(slug)
                body = self.bodies[f"script-manju-{slug}"]
                rhythm = next(line for line in original.splitlines() if line.startswith("**本题材的节奏。**"))
                self.assertIn(rhythm, body, "不可用泛化节奏替代该题材的原版长短镜规则")
                for line in original.splitlines():
                    if (line.startswith(("| 集数与时长", "| 单集镜头", "| 画幅", "| 风格", "| 受众"))
                            or re.match(r"^\d+\. ", line) and "制作与发布" not in line
                            and "能写文件" not in line and "文件名" not in line
                            or line.startswith(("本集进度：", "- **全部原创", "**全部原创", "用户输入："))):
                        self.assertIn(line, body, f"丢失原版题材细节：{line}")

    def test_full_five_stages_and_deep_body_not_only_summary(self):
        for slug in EXPECTED:
            with self.subTest(slug=slug):
                original = _raw(slug)
                body = self.bodies[f"script-manju-{slug}"]
                headings = re.findall(r"(?m)^## .+阶段[1-5][：:].+$", original)
                self.assertEqual(len(headings), 5)
                positions = [body.index(heading) for heading in headings]
                self.assertEqual(positions, sorted(positions))
                # 不只存在阶段标题：各阶段的核心正文、视频/声音/连续性与最终发布细则全部可达。
                for token in ("人物卡（编号P01起）", "道具卡（编号D01起）", "场景卡（编号S01起）",
                              "特效卡（编号FX01起）", "本集对白：XXX字", "A段：分镜图提示词",
                              "B段：视频提示词", "参考图使用规则：", "资产图锚定：", "站位与朝向：",
                              "运镜：", "音效：", "连续性检查", "80–120字简介", "标注AI生成内容"):
                    self.assertIn(token, body)
                for line in original.splitlines():
                    if line.startswith(("- 每集4–", "- 每集写完逐句", "- **台词语速", "- **台词**",
                                        "- 时间轴首尾相接", "- 发布文案：", "- 合规提示：")):
                        self.assertIn(line, body)
                self.assertIn("阶段5", body)
                self.assertGreater(len(body), len(original) * 0.85, "防止误把长模板压缩为摘要")

    def test_chat_ready_cleanup_and_reference_images_are_not_variables(self):
        for slug in EXPECTED:
            with self.subTest(slug=slug):
                body = self.bodies[f"script-manju-{slug}"]
                row = self.by_id[f"script-manju-{slug}"]
                self.assertFalse(body.lstrip().startswith("---"))
                for pattern in (r"(?m)^(?:name|description):", r"(?<!不要)(?<!禁止)只回复.{0,50}已就绪", r"通过粘贴看到",
                                r"能写文件的工具", r"WorkBuddy|Claude Code", r"文件名和编号",
                                r"01-企划案\.md|02-资产库\.md|03-分集剧本\.md|第01集\.md", r"\bMixed\b"):
                    self.assertNotRegex(body, pattern)
                for number in (1, 2, 3):
                    self.assertIn(f"［参考图 {number}］", body)
                self.assertEqual(row["vars"], [], "Mixed 是上传顺序引用，不是用户模板变量")
                self.assertFalse(row["has_vars"])
                self.assertNotRegex(body, r"\{\{[^{}]+\}\}")

    def test_committed_studio_rows_and_all_bodies_are_reachable(self):
        catalog = StudioCatalog(PUBLIC)
        all_rows = catalog.items()
        rows = [row for row in all_rows if row["id"].startswith("script-manju-")]
        self.assertEqual(len(rows), 21)
        self.assertEqual({row["id"] for row in rows}, IDS)
        for row in rows:
            with self.subTest(item_id=row["id"]):
                self.assertEqual({k: v for k, v in row.items() if k != "order"},
                                 {k: v for k, v in self.by_id[row["id"]].items() if k != "order"})
                self.assertEqual(catalog.get_body(row["id"]), self.bodies[row["id"]])
        self.assertEqual(len({row["id"] for row in all_rows}), len(all_rows))
        for row in all_rows:
            with self.subTest(reachable=row["id"]):
                self.assertTrue(catalog.get_body(row["id"]).strip())


class ManjuAppendCliTests(unittest.TestCase):
    def _baseline(self, out):
        shutil.copytree(PUBLIC, out)
        index = _json(out / "catalog_index.json")
        index["items"] = [r for r in index["items"] if r["id"] not in IDS]
        index["count"] = len(index["items"])
        index["chars"] = sum(r["chars"] for r in index["items"])
        index["modules"] = dict(Counter(r["module"] for r in index["items"]))
        index["types"] = dict(Counter(r["type"] for r in index["items"]))
        index["sources"] = {k: v for k, v in index.get("sources", {}).items() if "manju" not in k}
        _write_json(out / "catalog_index.json", index)
        script = _json(out / "bodies_script.json")
        _write_json(out / "bodies_script.json", {k: v for k, v in script.items() if k not in IDS})
        return index

    def _run(self, out, *, source=None):
        command = [sys.executable, "-I", str(ROOT / "verify" / "studio_catalog_build.py"),
                   "--append-manju", "--out-dir", str(out)]
        if source is not None:
            command += ["--manju-dir", str(source)]
        return subprocess.run(command, cwd=out.parent, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)

    def test_append_is_byte_idempotent_and_keeps_old_rows_and_other_shards(self):
        with tempfile.TemporaryDirectory(prefix="manju-cli-") as tmp:
            out = Path(tmp) / "studio"
            before_index = self._baseline(out)
            before = _snapshot(out)
            old_bodies = _json(out / "bodies_script.json")
            first = self._run(out)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            once = _snapshot(out)
            index = _json(out / "catalog_index.json")
            self.assertEqual(index["count"], before_index["count"] + 21)
            self.assertEqual([r for r in index["items"] if r["id"] not in IDS], before_index["items"])
            self.assertEqual({r["id"] for r in index["items"] if r["id"] in IDS}, IDS)
            bodies = _json(out / "bodies_script.json")
            self.assertEqual({k: v for k, v in bodies.items() if k not in IDS}, old_bodies)
            for name, data in before.items():
                if name not in {"catalog_index.json", "bodies_script.json", "conversion_report.json"}:
                    self.assertEqual(once[name], data, f"不应改动其他旧分片：{name}")
            old_report = json.loads(before["conversion_report.json"])
            report = _json(out / "conversion_report.json")
            for key, value in old_report["sources"].items():
                if "manju" not in key:
                    self.assertEqual(report["sources"][key], value)
            for key in ("rejects", "descriptive_mentions"):
                for entry in old_report[key]:
                    self.assertIn(entry, report[key], f"增补不能丢失旧报告 {key}")
            for key, value in old_report["anomalies"].items():
                self.assertEqual(report["anomalies"][key], value)
            for tag in ("prompt", "game", "style"):
                name = f"bodies_{tag}.json"
                self.assertEqual(report["products"][name], old_report["products"][name])
            for key, value in before_index.get("sources", {}).items():
                self.assertEqual(index["sources"][key], value)
            self.assertEqual(index["count"], len(index["items"]))
            self.assertEqual(sum(index["modules"].values()), index["count"])
            self.assertEqual(sum(index["types"].values()), index["count"])
            self.assertEqual(index["chars"], sum(r["chars"] for r in index["items"]))
            catalog = StudioCatalog(out)
            for row in index["items"]:
                self.assertTrue(catalog.get_body(row["id"]))
            second = self._run(out, source=SKILLS)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(_snapshot(out), once, "重复增补必须逐字节幂等，不能重复追加或重排")

    def test_bad_source_fails_closed_before_writing_any_catalog_file(self):
        for damage in ("missing_reference", "changed_bytes", "missing_license", "changed_license",
                       "missing_manifest", "wrong_commit", "wrong_upstream_path", "missing_local_path",
                       "wrong_local_path", "upstream_as_local_path", "absolute_local_path", "traversal_local_path"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory(prefix="manju-invalid-") as tmp:
                tmp = Path(tmp)
                out = tmp / "studio"
                self._baseline(out)
                source = tmp / "originals"
                shutil.copytree(SKILLS, source)
                reference = source / "references" / "xuanhuan-manju.md"
                if damage == "missing_reference":
                    reference.unlink()
                elif damage == "changed_bytes":
                    reference.write_bytes(reference.read_bytes() + b"\n")
                elif damage == "missing_license":
                    (source / "LICENSE").unlink()
                elif damage == "changed_license":
                    (source / "LICENSE").write_bytes(b"not the approved MIT license\n")
                elif damage == "missing_manifest":
                    (source / "source.json").unlink()
                else:
                    manifest = _json(source / "source.json")
                    row = next(r for r in manifest["files"] if r["path"] == "xuanhuan-manju/SKILL.md")
                    if damage == "wrong_commit":
                        manifest["commit"] = "0" * 40
                    elif damage == "wrong_upstream_path":
                        row["path"] = row["local_path"]
                    elif damage == "missing_local_path":
                        del row["local_path"]
                    elif damage == "wrong_local_path":
                        row["local_path"] = "references/xihuan-manju.md"
                    elif damage == "upstream_as_local_path":
                        legacy = source / row["path"]
                        legacy.parent.mkdir(parents=True)
                        shutil.copyfile(reference, legacy)
                        row["local_path"] = row["path"]
                    else:
                        # 路径指向同一份有效字节，防止测试仅因文件不存在而通过。
                        outside = tmp / "outside.md"
                        shutil.copyfile(reference, outside)
                        row["local_path"] = str(outside.resolve()) if damage == "absolute_local_path" else "../outside.md"
                    _write_json(source / "source.json", manifest)
                before = _snapshot(out)
                result = self._run(out, source=source)
                self.assertNotEqual(result.returncode, 0, "损坏或漂移原件不能被静默接收")
                self.assertTrue((result.stdout + result.stderr).strip(), "失败必须提供诊断")
                self.assertEqual(_snapshot(out), before, "校验失败不能部分改写目录")


if __name__ == "__main__":
    unittest.main()
