# -*- coding: utf-8 -*-
"""发布工作流 `release.yml` 的结构与版本串守门。

**为什么要有这条护栏（2026-09-23 的真事故）**：本次发版把更新说明**逐行文本替换**写进
`release.yml` 的 `body: |` 块标量，其中一行把 12 空格缩进写丢了（落到第 0 列）。
YAML 的块标量遇到缩进更小的行就**提前结束**，于是那行 `- 边界与安全…` 被当作文档级的序列项，
整份工作流变成**非法 YAML**——GitHub 不会为它创建任何运行，也就永远打不出 tag、发不出 Release。
表现是「`git push` 成功、远端 master 也更新了，但十分钟过去什么都没发生」，而**本地没有任何东西会报警**：
单测全绿、两份 JSON 都合法、`git diff` 看着也对——**因为这些检查都不解析那份 YAML**。

所以这里钉住三组判据（都不依赖额外三方库，pyyaml 在则由它做完整解析）：

1. **根级只能是映射键**：这些工作流的根是映射（name/on/permissions/env/concurrency/jobs），
   根级出现 `- `（序列项）必然非法。这是「缩进丢到第 0 列」的直接签名。
2. **`|` 块标量的边界必须落在合法缩进**：块内容一律比键行深；块的结束行（下一条非空行）
   缩进只能是 `键行缩进`（兄弟键）或 `键行缩进 - 2`（同级列表项），否则就是被误缩进截断了。
   块内非空行的缩进还必须 >= 首行内容缩进（YAML 的一致缩进规则）。
3. **版本串各处一致**：`env.RELEASE_VERSION` 是唯一源头，tag、两个包名、Release 标题、
   步骤名、README 标题、清单 version、更新说明头条都必须由它派生。

反方向同样钉住：把缩进改坏、把任一处版本串改岔，这里必须变红（变异核对见维护说明 §六）。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
MANIFEST = ROOT / "naiba-chat-update.json"
RELEASE_NOTES = ROOT / "release_notes.json"
README = ROOT / "README.md"

# `key: |` / `key: |-` / `key: |+`（字面块标量）
_BLOCK_SCALAR = re.compile(r"^(\s*)([^\s#][^:]*):[ \t]*\|[-+]?[ \t]*$")
# 根级合法行：顶层映射键 / 注释 / 文档标记
_ROOT_KEY = re.compile(r"^[A-Za-z_][\w.\- ]*:(\s|$)")
_ROOT_MARKER = re.compile(r"^(#|---|\.\.\.|%)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def scan_structure(text: str) -> list[str]:
    """返回结构性问题的中文说明列表；空列表 = 通过。"""
    lines = text.split("\n")
    problems: list[str] = []

    # 判据 1：根级不得出现序列项。
    for number, line in enumerate(lines, 1):
        if not line.strip() or _indent(line) != 0:
            continue
        if line.startswith("- ") or line.strip() == "-":
            problems.append(
                f"第 {number} 行在根级写了序列项（根是映射，非法；多半是块标量里的缩进丢了）："
                f"{line[:60]!r}"
            )
        elif not (_ROOT_KEY.match(line) or _ROOT_MARKER.match(line)):
            problems.append(f"第 {number} 行在根级既不是映射键也不是注释：{line[:60]!r}")

    # 判据 2：块标量的边界与内部缩进。
    for index, line in enumerate(lines):
        match = _BLOCK_SCALAR.match(line)
        if match is None:
            continue
        base = len(match.group(1))
        content_indent: int | None = None
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor]
            if not candidate.strip():
                cursor += 1
                continue
            depth = _indent(candidate)
            if depth <= base:
                break
            if content_indent is None:
                content_indent = depth
            elif depth < content_indent:
                problems.append(
                    f"第 {index + 1} 行的 `|` 块里，第 {cursor + 1} 行缩进 {depth} "
                    f"浅于块首行的 {content_indent}（YAML 要求一致缩进）：{candidate[:60]!r}"
                )
            cursor += 1
        if cursor >= len(lines):
            continue  # 块一直延伸到文件末尾，没有结束行需要检查
        terminator = lines[cursor]
        depth = _indent(terminator)
        if depth not in (base, base - 2):
            problems.append(
                f"第 {index + 1} 行的 `|` 块被第 {cursor + 1} 行（缩进 {depth}）截断——"
                f"结束行只能是兄弟键（缩进 {base}）或同级列表项（缩进 {base - 2}）；"
                f"多半是块内某行缩进写丢了：{terminator[:60]!r}"
            )
    return problems


def workflow_facts() -> tuple[str, dict]:
    """返回 (原始文本, 解析后的判据来源)。不依赖 pyyaml，用正则取需要的字段。"""
    text = WORKFLOW.read_text(encoding="utf-8", newline="")
    env = {}
    for key in ("RELEASE_VERSION", "RELEASE_TAG", "PACKAGE_NAME", "LEGACY_PACKAGE_NAME"):
        found = re.search(rf"^\s*{key}:[ \t]*(\S+)[ \t]*$", text, re.MULTILINE)
        env[key] = found.group(1) if found else ""
    return text, env


class WorkflowStructureTests(unittest.TestCase):
    """工作流文件本身必须是合法 YAML——不合法则 GitHub 直接不运行（本次事故）。"""

    def test_release_yml_has_no_structural_problem(self) -> None:
        problems = scan_structure(WORKFLOW.read_text(encoding="utf-8", newline=""))
        self.assertEqual([], problems, "release.yml 结构有问题：\n" + "\n".join(problems))

    def test_other_workflow_files_have_no_structural_problem(self) -> None:
        """同一条纪律适用于 `.github/workflows/` 下所有文件，不只 release.yml。"""
        for path in sorted(WORKFLOW.parent.glob("*.y*ml")):
            problems = scan_structure(path.read_text(encoding="utf-8", newline=""))
            self.assertEqual([], problems, f"{path.name} 结构有问题：\n" + "\n".join(problems))

    def test_workflow_parses_with_pyyaml_when_available(self) -> None:
        """最强的一条：真解析。pyyaml 不在时由上面两条免依赖判据兜底。"""
        try:
            import yaml
        except ImportError:  # pragma: no cover - 环境相关
            self.skipTest("pyyaml 不可用，已由免依赖的结构判据覆盖")
        for path in sorted(WORKFLOW.parent.glob("*.y*ml")):
            with self.subTest(path=path.name):
                try:
                    yaml.safe_load(path.read_text(encoding="utf-8", newline=""))
                except yaml.YAMLError as error:
                    self.fail(f"{path.name} 不是合法 YAML（GitHub 不会运行它）：{error}")


class ReleaseVersionConsistencyTests(unittest.TestCase):
    """`env.RELEASE_VERSION` 是版本串的唯一源头，其余各处必须由它派生。"""

    def setUp(self) -> None:
        self.text, self.env = workflow_facts()
        self.version = self.env["RELEASE_VERSION"]

    def test_version_is_well_formed(self) -> None:
        self.assertRegex(self.version, r"^\d+\.\d+\.\d+(-beta)?$", "版本号形状不对")

    def test_tag_and_package_names_derive_from_version(self) -> None:
        self.assertEqual(f"v{self.version}", self.env["RELEASE_TAG"])
        self.assertEqual(f"cat-chat-{self.version}-windows-x64", self.env["PACKAGE_NAME"])
        self.assertEqual(f"naiba-chat-{self.version}-windows-x64", self.env["LEGACY_PACKAGE_NAME"])
        # 发布步骤的 name / Release 标题必须同步（漏改会发出标题写着旧版本的 Release）。
        display = self.version.replace("-beta", "") + " Beta"
        self.assertIn(f"Publish {display} release", self.text)
        self.assertIn(f"name: Cat Chat {display}", self.text)
        # 「过时的上一版版本串」不该还留在文件里。
        self.assertNotIn("2.8.6", self.text, "release.yml 里仍残留上一版版本号")

    def test_manifest_agree_with_workflow(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(self.version, manifest["version"])
        # §零 协议常量：逐字不可改（已发布客户端逐字校验）。
        self.assertEqual("yc883123/naiba-chat", manifest["repository"])
        self.assertEqual("naiba-chat.exe", manifest["asset"])

    def test_readme_title_and_release_notes_head_agree(self) -> None:
        display = self.version.replace("-beta", "") + " Beta"
        first_line = README.read_text(encoding="utf-8").split("\n", 1)[0].strip()
        self.assertEqual(f"# Cat Chat {display}", first_line)
        head = json.loads(RELEASE_NOTES.read_text(encoding="utf-8"))[0]
        self.assertTrue(
            head.startswith(f"Cat Chat {display}"), f"更新说明头条没跟上版本：{head[:60]!r}"
        )

    def test_release_body_mentions_the_current_version(self) -> None:
        """`body` 是手写正文，最容易漏改；至少要带上本版标题。"""
        display = self.version.replace("-beta", "") + " Beta"
        self.assertIn(f"Cat Chat {display} Windows build", self.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
