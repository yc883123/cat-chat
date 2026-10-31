# -*- coding: utf-8 -*-
"""设定集文件夹导入的解析器（第二次反馈 §一/§六）。

用户日常用 Agent 对话攒出「设定集」：一个文件夹、全都是 .md，里面有人物群像、
分卷大纲、章节框架等。织书台把整个文件夹读进来后，按**文件名 + Markdown 结构**
做启发式识别，把能认出来的填进作品：

- 人物档案文件（`人物群像档案_上_1-10.md` 这类）→ 角色（`## 章节标题`一节一个人，
  逐行认「身份：」「性格：」等字段，认不出的整段进「人物介绍」）；
- 分卷大纲文件（`分卷大纲_卷一_至卷四.md` 这类）→ 大纲分部（一部一文件）；
- 章节框架（`第12章 标题` 逐行列表）→ 章节目录（含每章字数 → 单章字数限制）；
- 其余文件 → 世界观素材；创意/简介/投稿简介类文件 → 故事创意。

识别是「尽力而为 + 如实报告」：每个文件认出了什么、什么没认出来都写进 preview，
不猜的部分宁缺毋滥（漏认的文件内容仍会随世界观进设定，不丢字）。
"""

from __future__ import annotations

import re
from typing import Any

from naiba.writer.store import (
    SETUP_TEXT_MAX_CHARS,
    PART_TEXT_MAX,
    WriterStoreError,
    _zh_number,
)

# 单文件与整批的内容上限（防御性）：一个 .md 远小于 30 万字符，超了说明选错目录。
FILE_TEXT_MAX_CHARS = 300_000
MAX_FILES = 80
MAX_TOTAL_CHARS = 3_000_000
ALLOWED_SUFFIXES = (".md", ".markdown", ".txt")

# 文件名分类（按优先级匹配；大小写不敏感）。
CHAR_FILE_PATTERN = re.compile(r"人物|角色|群像|人设", re.IGNORECASE)
PART_FILE_PATTERN = re.compile(r"分卷|分部|卷纲|部纲", re.IGNORECASE)
IDEA_FILE_PATTERNS = (
    re.compile(r"目录摘要|投稿简介", re.IGNORECASE),
    re.compile(r"创意|简介|梗概|前言|书名", re.IGNORECASE),
)
CHAPTER_FILE_PATTERN = re.compile(r"章节|章纲|拆章|目录", re.IGNORECASE)

# 章节框架行：`第12章 标题` / `### 第一章 标题` / `- 第 3 回 标题`。
_CHAPTER_LINE = re.compile(
    r"^\s*[#>*\-\s]*第\s*([0-9]{1,4}|[零一二两三四五六七八九十百千]+)\s*[章回][\s：:·.、—\-]*(.*)$"
)
# 一章框架里标注的字数（"3000字"）：全章出现次数取众数 → 单章字数限制。
_PLAN_CHARS = re.compile(r"([0-9]{3,5})\s*字")
# 人物小节标题：`## 灰烬玫瑰·伊索尔德` / `### 主角：灰烬玫瑰`。
_SECTION_HEAD = re.compile(r"^\s*#{2,4}\s+(.+?)\s*#*\s*$")
# 字段行：`- 身份：守誓流放骑士` / `性格：沉默坚忍`。
_FIELD_LINE = re.compile(r"^\s*[-*>·\s]*([^\s：:]{1,8})\s*[：:]\s*(.+?)\s*$")

# 人物字段标签 → 设定表单字段（同一字段可接受多个叫法）。
_CHAR_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "alias": ("别称", "别名", "称号"),
    "age": ("年龄", "岁数"),
    "origin": ("出身", "来历", "籍贯"),
    "identity": ("身份", "职业", "职位"),
    "costume": ("服装", "着装", "外貌", "形象"),
    "personality": ("性格", "性情", "特质"),
    "voice": ("声音", "说话", "语气", "口癖"),
    "relations": ("关系", "关系网", "人际"),
    "goal": ("目标", "动机", "诉求", "欲望"),
    "arc": ("弧光", "弧线", "成长", "变化"),
    "intro": ("介绍", "简介", "背景", "经历", "秘密"),
}
_LABEL_TO_FIELD = {
    label: field
    for field, labels in _CHAR_FIELD_LABELS.items()
    for label in labels
}
# 正文槽位上限与 store 的 normalize_setup 对齐（这里先截，落盘再统一收敛）。
_CHAR_TEXT_MAX = 200
_CHAR_LONG_MAX = 1_000


def _clean_name(name: str) -> str:
    """小节标题 → 角色名：去掉 Markdown 粗体、序号、括注里的身份描述。"""
    text = str(name or "")
    text = re.sub(r"[*_`#]+", "", text).strip()
    text = re.sub(r"^[0-9]{1,3}[.、)\s]+", "", text)
    text = text.split("（")[0].split("(")[0]
    text = re.sub(r"^(主角|配角|反派)[：:·\-—\s]*", "", text).strip(" ·-—：:")
    return text.strip()


def _file_stem(name: str) -> str:
    stem = re.sub(r"\.(md|markdown|txt)$", "", str(name or ""), flags=re.IGNORECASE)
    # 去掉 "03_" 这类手工排序前缀，剩下的就是人写的标题。
    return re.sub(r"^[0-9]{1,3}[_\-.]+\s*", "", stem).strip()


def _parse_character_file(name: str, content: str) -> list[dict[str, Any]]:
    """人物档案 → 角色数组：`## 标题` 一节一个人，字段行逐个认，认不出进介绍。"""
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_body: list[str] = []
    for line in str(content or "").splitlines():
        head = _SECTION_HEAD.match(line)
        if head:
            if current_title.strip():
                sections.append((current_title, current_body))
            current_title = head.group(1)
            current_body = []
        else:
            current_body.append(line)
    if current_title.strip():
        sections.append((current_title, current_body))
    # 全文没有小节标题：把「名字：」式开头当一节一个（`灰烬玫瑰：守誓流放骑士……`）。
    if not sections:
        return []

    rows: list[dict[str, Any]] = []
    for title, body_lines in sections:
        char_name = _clean_name(title)
        if not char_name or len(char_name) > 60:
            continue
        row: dict[str, Any] = {"name": char_name[:60]}
        head_and_body = "\n".join(body_lines)
        if "主角" in title:
            row["role"] = "主角"
        elif "配角" in title or "反派" in title:
            row["role"] = "配角"
        leftover: list[str] = []
        for line in body_lines:
            field_match = _FIELD_LINE.match(line)
            if not field_match:
                leftover.append(re.sub(r"^\s*[-*>·]+\s*", "", line).strip())
                continue
            label = field_match.group(1).strip()
            value = field_match.group(2).strip()
            field = _LABEL_TO_FIELD.get(label)
            if not field or not value or field in row:
                leftover.append(re.sub(r"^\s*[-*>·]+\s*", "", line).strip())
                continue
            row[field] = value[: (_CHAR_LONG_MAX if field in ("intro", "relations") else _CHAR_TEXT_MAX)]
        if "主角" not in row.get("role", "") and "主角" in head_and_body[:80]:
            row["role"] = "主角"
        intro_text = "\n".join(bit for bit in leftover if bit).strip()
        if intro_text:
            existing = str(row.get("intro") or "").strip()
            merged = f"{existing}\n{intro_text}".strip() if existing else intro_text
            row["intro"] = merged[:_CHAR_LONG_MAX]
        rows.append(row)
    return rows


def _parse_chapter_framework(content: str) -> tuple[list[dict[str, Any]], list[int]]:
    """章节框架 → 章节目录：`第N章` 行开新章，其后到下一章前的行都算本章要点。"""
    chapters: list[dict[str, Any]] = []
    char_counts: list[int] = []
    current: dict[str, Any] | None = None
    for line in str(content or "").splitlines():
        match = _CHAPTER_LINE.match(line)
        if match:
            if current is not None:
                chapters.append(current)
            no_raw, title = match.group(1), match.group(2).strip()
            if str(no_raw).isdigit():
                no = int(no_raw)
            else:
                no = _zh_number(str(no_raw))
            current = {
                "no": no,
                "title": (title or "")[:200],
                "beat": "",
                "hook": "",
            }
            if title:
                current["beat"] = title[:2000]
            continue
        if current is None:
            continue
        text = re.sub(r"^\s*[-*>·]+\s*", "", line).strip()
        if not text:
            continue
        beat = str(current.get("beat") or "")
        current["beat"] = (f"{beat}\n{text}" if beat else text)[:2000]
    if current is not None:
        chapters.append(current)
    # 章节号缺失/不连续不硬拒：按出现顺序重排，但保留能认出的号（与启元导入同口径）。
    cleaned: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, row in enumerate(chapters, start=1):
        no = int(row.get("no") or 0)
        if no < 1 or no in seen:
            no = index
        seen.add(no)
        cleaned.append({**row, "no": no})
    nos = [row["no"] for row in cleaned]
    # 只有重复或倒序才重排；缺号（如 1、12）保留原章号——章号本身是内容，别替用户改写。
    if len(set(nos)) != len(nos) or any(b <= a for a, b in zip(nos, nos[1:])):
        cleaned = [{**row, "no": pos} for pos, row in enumerate(cleaned, start=1)]
    for row in cleaned:
        for count in _PLAN_CHARS.findall(str(row.get("beat") or "")):
            value = int(count)
            if 500 <= value <= 20000:
                char_counts.append(value)
    return cleaned, char_counts


def _major_plan_chars(counts: list[int]) -> int:
    """全章字数标注的众数 → 单章字数限制（只认织书台可选的那几档，否则 0=不限）。"""
    if not counts:
        return 0
    counts_set = sorted(set(counts))
    best = max(counts_set, key=lambda value: counts.count(value))
    return best if best in (2000, 3000, 4000, 6000) else 0


def parse_settings_folder(files: Any) -> dict[str, Any]:
    """整个设定集文件夹的文本解析（纯函数，不落盘）。

    `files`：`[{"name": 文件名, "content": 正文}, …]`（调用方已过滤扩展名与大小）。
    返回 `{setup, parts, chapters, plan_chars, preview}`；识别不了就返回空并在
    preview.warnings 里如实说明。
    """
    if not isinstance(files, list) or not files:
        raise WriterStoreError("没有读到任何 .md 文件；请选择设定集文件夹")
    if len(files) > MAX_FILES:
        raise WriterStoreError(f"文件太多（{len(files)} 个，上限 {MAX_FILES}）；请精简后重试")

    char_files: list[tuple[str, str]] = []
    part_files: list[tuple[str, str]] = []
    idea_file: tuple[str, str] | None = None
    chapter_files: list[tuple[str, str]] = []
    other_files: list[tuple[str, str]] = []
    warnings: list[str] = []
    total = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        content = str(item.get("content") or "")
        if not name or not content.strip():
            continue
        total += len(content)
        if len(content) > FILE_TEXT_MAX_CHARS:
            raise WriterStoreError(f"「{name}」超过 {FILE_TEXT_MAX_CHARS // 10000} 万字符上限")
        lowered = name.lower()
        # 顺序有讲究：目录摘要/投稿简介是「创意」，必须先于「目录=章节框架」判。
        if PART_FILE_PATTERN.search(lowered):
            part_files.append((name, content))
        elif CHAR_FILE_PATTERN.search(lowered):
            char_files.append((name, content))
        elif idea_file is None and any(p.search(lowered) for p in IDEA_FILE_PATTERNS):
            idea_file = (name, content)
        elif CHAPTER_FILE_PATTERN.search(lowered):
            chapter_files.append((name, content))
        else:
            other_files.append((name, content))
    if total > MAX_TOTAL_CHARS:
        raise WriterStoreError(
            f"设定集总共 {total / 10000:.0f} 万字符，超过 {MAX_TOTAL_CHARS // 10000 // 100}00 万上限；请拆分后重试"
        )

    # ---- 角色 ----
    characters: list[dict[str, Any]] = []
    char_files_ok = 0
    for name, content in char_files:
        rows = _parse_character_file(name, content)
        if rows:
            char_files_ok += 1
            characters.extend(rows)
        else:
            # 认不出小节结构：整份进世界观素材，不丢内容。
            other_files.append((name, content))

    # ---- 大纲分部（一部一文件）----
    parts: list[dict[str, Any]] = []
    used_part_names: set[str] = set()
    for name, content in part_files:
        text = content.strip()[:PART_TEXT_MAX]
        if not text:
            continue
        title = _file_stem(name)[:80]
        parts.append({"no": len(parts) + 1, "title": title, "text": text})
        used_part_names.add(name)

    # ---- 章节框架：优先「章节/目录」类文件，没有再扫分部大纲正文 ----
    chapters: list[dict[str, Any]] = []
    char_counts: list[int] = []
    chapter_source = ""
    for name, content in chapter_files:
        rows, counts = _parse_chapter_framework(content)
        if rows:
            chapter_source = chapter_source or name
            chapters.extend(rows)
            char_counts.extend(counts)
    if not chapters:
        for name, content in part_files:
            rows, counts = _parse_chapter_framework(content)
            if rows:
                chapter_source = chapter_source or name
                chapters.extend(rows)
                char_counts.extend(counts)
    if not chapters:
        for name, content in other_files:
            rows, counts = _parse_chapter_framework(content)
            if rows:
                chapter_source = chapter_source or name
                chapters.extend(rows)
                char_counts.extend(counts)

    # ---- 创意与世界观光 ----
    idea = ""
    idea_source = ""
    if idea_file is not None:
        idea = idea_file[1].strip()[:SETUP_TEXT_MAX_CHARS]
        idea_source = idea_file[0]
    worldview_blocks: list[str] = []
    for name, content in other_files:
        stem = _file_stem(name)
        worldview_blocks.append(f"## {stem}\n\n{content.strip()}")
    worldview = "\n\n".join(worldview_blocks)[:SETUP_TEXT_MAX_CHARS]

    if not any([idea, worldview, characters, parts, chapters]):
        warnings.append("设定集里没有识别出角色、大纲、章节或世界观内容；请检查文件夹是否选对")
    if not parts and not chapters:
        warnings.append("没有识别到分卷大纲或章节框架；导入后停在大纲页，需要你自己生成/粘贴大纲")
    if not characters:
        warnings.append("没有识别出角色档案（需要「## 人物名」小节结构）；人物请在设定页补")
    if characters and not any(str(row.get("role") or "") == "主角" for row in characters):
        warnings.append("角色里没有标记出主角；导入后请把主角的角色字段改成主角")

    setup = {
        "idea": idea,
        "worldview": worldview,
        "characters": characters,
    }
    preview = {
        "file_count": len(char_files) + len(part_files) + len(chapter_files) + len(other_files),
        "character_count": len(characters),
        "parts_count": len(parts),
        "chapter_count": len(chapters),
        "plan_chars": _major_plan_chars(char_counts),
        "idea_source": idea_source,
        "chapter_source": chapter_source,
        "worldview_chars": len(worldview),
        "warnings": warnings,
    }
    return {
        "setup": setup,
        "has_setup": bool(idea or worldview or characters),
        "parts": parts,
        "chapters": chapters,
        "plan_chars": preview["plan_chars"],
        "preview": preview,
    }
