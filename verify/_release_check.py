# -*- coding: utf-8 -*-
"""发布清单同步自检 + 本地绝对路径审计（只读）。

路径审计的判据**不在本文件里**：直接加载 §8.8 守门单测 `tests/test_no_absolute_paths.py`
复用它的正则、占位白名单与扩展名表——同一个判断不写第二遍（两套口径必然漂移，§九.115）。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 产品显示名 2026-09-20 起为 `Cat Chat`（原名 Naiba Chat）；README 标题与 Release 标题
# 必须用当前显示名，历史 `Naiba Chat` 仍被接受（改名前后的发布都能核对），但**不能**放宽成
# 「含 Chat 字样就算过」——品牌必须整个前缀命中。
BRANDS = ("Cat Chat", "Naiba Chat")


def brand_hit(text: str, template: str) -> str:
    """把 `{brand}` 逐品牌代入模板，返回首个命中的品牌名；都没命中返回空串。"""
    for brand in BRANDS:
        if template.format(brand=brand) in text:
            return brand
    return ""

# ---- 1) 两份 release_notes 一致性 ----
notes = json.loads((ROOT / "release_notes.json").read_text(encoding="utf-8"))
update = json.loads((ROOT / "naiba-chat-update.json").read_text(encoding="utf-8"))
version = update["version"]        # 如 2.3.5-beta
series = version.split("-")[0]     # 2.3.5
print(f"[§2/§3] release_notes {len(notes)} 条，与更新清单一致 = {notes == update['release_notes']}，"
      f"version = {version}")

# ---- 2) README 版本串（版本号从更新清单派生；曾写死 2.1.0 而长期失效）----
readme = (ROOT / "README.md").read_text(encoding="utf-8")
title_brand = brand_hit(readme, "# {brand} " + series + " Beta")
print(f"[§4] README 标题 `# <品牌> {series} Beta`：命中品牌 = {title_brand or '无'}")
for token in (f"## {series} Beta 主要能力",
              f"naiba-chat-{version}-windows-x64.zip",
              f'NAIBA_BUILD_VERSION = "{version}"'):
    print(f"[§4] README 含 {token!r}: {token in readme}")

# ---- 3) 工作流 YAML：三处版本串必须与清单一致（无 pyyaml 也照查）----
workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
try:
    import yaml  # type: ignore
    data = yaml.safe_load(workflow)
    print(f"[§1] YAML 解析 OK，env.RELEASE_VERSION = {data['env']['RELEASE_VERSION']}")
except ImportError:
    print("[§1] 未安装 pyyaml，跳过 YAML 解析（改用下面的正则口径）")
except Exception as exc:  # noqa: BLE001
    print(f"[§1] YAML 解析失败：{exc}")

for field, expect in (("RELEASE_VERSION", version),
                      ("RELEASE_TAG", f"v{version}"),
                      ("PACKAGE_NAME", f"naiba-chat-{version}-windows-x64")):
    found = re.search(rf"^\s*{field}:\s*(\S+)\s*$", workflow, re.M)
    actual = found.group(1) if found else "(未找到)"
    suffix = "" if actual == expect else f"  ← 与清单不符（应为 {expect}）"
    print(f"[§1] {field} = {actual}{suffix}")
name_brand = brand_hit(workflow, "name: {brand} " + series + " Beta")
print(f"[§1] 工作流 Release 标题 `name: <品牌> {series} Beta`：命中品牌 = {name_brand or '无'}")
publish_step = f"Publish {series} Beta release"
print(f"[§1] 工作流含 {publish_step!r}: {publish_step in workflow}")

# ---- 4) 本地绝对路径审计（口径复用守门单测；只扫入库的代码/配置类文件）----
# 历史教训（§九.115）：这里曾自己写一套「rglob verify/ 下所有文件」的宽口径，把构建产物也当文本读进来
# （`_rel265/naiba-chat.exe`、`_build.log`、隔离目录里的 `chat.db` / `config.json`…），
# 742 处命中里 656 处是噪音，真正要注意的几处被淹没。判据本来就只有一处：§8.8 守门。
print("\n=== 本地绝对路径审计（口径 = §8.8 守门 tests/test_no_absolute_paths.py）===")


def _load_guard_policy():
    """加载 §8.8 守门模块，复用它的 `CODE_EXTS` / `USER_PATH` / `REPO_PATH` / `PLACEHOLDERS`。"""
    guard = ROOT / "tests" / "test_no_absolute_paths.py"
    if not guard.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_naiba_abs_path_guard", guard)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _audit_absolute_paths() -> None:
    policy = _load_guard_policy()
    if policy is None:
        print("  !! 找不到 tests/test_no_absolute_paths.py，跳过（它才是判据的单一实现）")
        return
    try:
        listed = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"  !! 取不到 `git ls-files`（{exc}），跳过审计")
        return
    scanned = 0
    hits = 0
    for rel in listed.stdout.splitlines():
        # 与守门同一范围：代码/配置类扩展名，`docs/` 下的手册生成物按 §一.5 处理、不参与判定。
        if rel.startswith("docs/") or pathlib.PurePath(rel).suffix.lower() not in policy.CODE_EXTS:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.split("\n"), 1):
            for match in policy.USER_PATH.finditer(line):
                if match.group(1).lower() in policy.PLACEHOLDERS:
                    continue
                hits += 1
                print(f"  {rel}:{line_no}  [用户目录] {match.group(0)}")
            repo_match = policy.REPO_PATH.search(line)
            if repo_match:
                hits += 1
                print(f"  {rel}:{line_no}  [仓库盘符] {repo_match.group(0)}")
    print(f"扫描入库代码/配置 {scanned} 个，命中 {hits} 处"
          f"（测试桩里的假路径也计数，人工按 §8.8 判断）")


_audit_absolute_paths()
