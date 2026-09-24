# -*- coding: utf-8 -*-
"""把 docs/tutorial-0X/README.md 回填成 skills/cat-chat-guide/references/ 下的文本版。

用法（仓库根执行）：
    .venv\\Scripts\\python.exe scripts\\backfill_guide_references.py

一鱼两吃：对外是图文教程，对内是教程助手的知识库，内容只维护一份。
改完教程正文（或新增第 9 篇）就重跑一次本脚本，别再手工抄一遍——
手抄必然与 `docs/` 那边漂移，而教程助手只会照 `references/` 答，漂移了就是给出错的步骤。
转换规则：
  - 去掉 `![...](images/...)` 图片行（知识库是纯文本，引用不到仓库里的图）；
  - 相对链接 `[文字](../tutorial-0X-.../README.md)` 拆成纯文字（Skill 里点不开）；
  - 顶部加一段「来源 + 第 N 篇 / 共 8 篇」说明；
  - 统一 LF（与 SKILL.md、00-手册要点.md 一致）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REFS = ROOT / "skills" / "cat-chat-guide" / "references"

IMAGE_LINE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
REL_LINK = re.compile(r"\[([^\]]+)\]\(\.\./tutorial-[^)]*\)")

PLAN: list[tuple[int, str, str, str]] = [
    (1, "tutorial-01-first-chat", "10-教程1-3分钟第一次对话.md", "3 分钟第一次对话"),
    (2, "tutorial-02-comfyui", "20-教程2-3分钟用ComfyUI出图.md", "3 分钟用 ComfyUI 出图"),
    (3, "tutorial-03-runninghub", "30-教程3-3分钟用RunningHub云端出图.md", "3 分钟用 RunningHub 云端出图"),
    (4, "tutorial-04-shortdrama", "40-教程4-3分钟出一集短剧.md", "3 分钟出一集短剧"),
    (5, "tutorial-05-prompt", "50-教程5-3分钟改出一条好提示词.md", "3 分钟改出一条好提示词"),
    (6, "tutorial-06-coding", "60-教程6-3分钟让AI写个小工具.md", "3 分钟让 AI 写个小工具"),
    (7, "tutorial-07-skill", "70-教程7-3分钟给CatChat装个新技能.md", "3 分钟给 Cat Chat 装个新技能"),
    (8, "tutorial-08-tutor", "80-教程8-3分钟认识教程助手.md", "3 分钟认识教程助手"),
]


def convert(text: str) -> str:
    lines = text.split("\n")
    # 从第一个 H1 开始
    start = next((i for i, line in enumerate(lines) if line.startswith("# ")), 0)
    body = lines[start:]
    body = [line for line in body if not IMAGE_LINE.match(line)]
    out: list[str] = []
    blank = 0
    for line in body:
        if line.strip() == "":
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    text = "\n".join(out)
    text = REL_LINK.sub(lambda m: m.group(1), text)
    return text.rstrip() + "\n"


def main() -> int:
    failed = False
    planned = {folder for _, folder, _, _ in PLAN}
    actual = {p.name for p in DOCS.glob("tutorial-*") if (p / "README.md").is_file()}
    for orphan in sorted(actual - planned):
        print(f"FAIL {orphan}/README.md 没登记进 PLAN —— 新增教程必须同时回填知识库，"
              f"否则教程助手答不出这一篇")
        failed = True
    for missing in sorted(planned - actual):
        print(f"FAIL PLAN 里的 {missing} 在 docs/ 下不存在")
        failed = True

    for index, folder, target, title in PLAN:
        src = DOCS / folder / "README.md"
        if not src.is_file():
            print(f"FAIL 缺源文件 {src}")
            failed = True
            continue
        head = (
            f"> 来源：仓库图文教程 `docs/{folder}/README.md`（图文版带截图；本文件是**文本版**，供教程助手引用）。\n"
            f"> 「3 分钟」系列第 {index} 篇「{title}」，共 8 篇。\n\n---\n\n"
        )
        text = head + convert(src.read_text(encoding="utf-8"))
        dst = REFS / target
        dst.write_text(text, encoding="utf-8", newline="\n")
        blob = dst.read_bytes()
        images = blob.count(b"images/")
        links = len(re.findall(rb"\]\(\.\./", blob))
        print(f"OK   {target}: {len(blob)} bytes, 残留图片引用={images}, 残留相对链接={links}")
        if images or links:
            failed = True
    legacy = REFS / "10-教程-3分钟用ComfyUI出图.md"
    if legacy.exists():
        legacy.unlink()
        print(f"删   {legacy.name}（已由 20-教程2-… 取代）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
