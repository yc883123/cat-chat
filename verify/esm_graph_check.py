# -*- coding: utf-8 -*-
"""ESM 图一致性校验：
1) 每个 import 的名字必须在源文件中被 export（声明确认）；
2) 每个文件引用的"模块级名"若已定义于其它文件，必须出现在本文件 import 中（防漏）；
3) 输出各文件 import/export 统计。

误报防护（与维护说明 §九 13 条对应）：词边界扫描前先剥离非代码文本——
行/块注释、字符串、模板字面量（${} 内保留代码）、正则字面量（除法不误判）、
属性键冒号容忍仍在。用法：python verify/esm_graph_check.py [js_dir]
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
JS_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "public" / "js"
DECL_RE = re.compile(r"^(?:export\s+)?(?:async\s+function\s+|function\s+|const\s+|let\s+|var\s+|class\s+)([A-Za-z_$][\w$]*)")
IMP_RE = re.compile(r'^import\s*\{([^}]*)\}\s*from\s*"\./([^"]+)"')

# 后续是正则字面量的关键字（return /x/ 等）：前一个"代码字符"是标识符尾时，靠关键字判定。
_REGEX_KEYWORDS = {
    "return", "case", "throw", "if", "while", "for", "do", "else", "typeof",
    "instanceof", "in", "of", "new", "delete", "void", "yield", "await",
}


def strip_non_code(text: str) -> str:
    """剥离注释/字符串/模板正文/正则字面量，保留模板 ${} 内的真代码。

    返回与原文等长的文本（非代码内容替换为空格），供词边界扫描使用。
    """
    n = len(text)
    out: list[str] = []
    i = 0
    # 栈元素：("tpl",) 模板正文（遇 `${` 压入 expr）；("expr", depth) 插值表达式（回到正文）
    stack: list[tuple[str, int]] = []
    # 最近一个非空白"代码字符"，用于区分正则字面量与除法；字符串内不更新。
    prev_code = ""

    def last_identifier() -> str:
        m = re.search(r"([A-Za-z_$][\w$]*)[ \t]*$", "".join(out))
        return m.group(1) if m else ""

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if stack and stack[-1][0] == "tpl":
            if c == "\\":
                out.append("  ")
                i += 2
                continue
            if c == "`":
                out.append(" ")
                stack.pop()
                i += 1
                continue
            if c == "$" and nxt == "{":
                out.append("  ")
                stack.append(("expr", 1))
                i += 2
                continue
            out.append(" ")
            i += 1
            continue
        if stack and stack[-1][0] == "expr":
            depth = stack[-1][1]
            if c == "{":
                stack[-1] = ("expr", depth + 1)
                out.append(c)
                prev_code = c
                i += 1
                continue
            if c == "}":
                if depth == 1:
                    out.append(" ")
                    stack.pop()
                    i += 1
                    continue
                stack[-1] = ("expr", depth - 1)
                out.append(c)
                prev_code = c
                i += 1
                continue

        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        if c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                out.append(" " * (n - i))
                i = n
            else:
                out.append(" " * (j + 2 - i))
                i = j + 2
            continue
        if c in "\"'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c:
                    break
                j += 1
            end = n if j >= n else j + 1
            out.append(" " * (end - i))
            i = end
            continue
        if c == "`":
            out.append(" ")
            stack.append(("tpl", 0))
            i += 1
            continue
        if c == "/" and (prev_code in "=(:,;!&|?{}[]+-*%^~<>" or (
                prev_code.isalpha() and last_identifier() in _REGEX_KEYWORDS)):
            # 正则字面量：找到未转义的闭合斜杠，消费可选 flags
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "/":
                    break
                j += 1
            if j < n:
                j += 1
                while j < n and text[j].isalpha():
                    j += 1
                out.append(" " * (j - i))
                i = j
                continue
            # 没找到闭合斜杠：按除法处理，原样保留
        out.append(c)
        if not c.isspace():
            prev_code = c
        i += 1
    return "".join(out)


files = sorted(JS_DIR.glob("*.js"))

decl: dict[str, list[str]] = {}
for path in files:
    for text in path.read_text(encoding="utf-8").splitlines():
        m = DECL_RE.match(text)
        if m:
            decl.setdefault(m.group(1), []).append(path.name)

problems = []
for path in files:
    lines = path.read_text(encoding="utf-8").splitlines()
    declared_here = {m.group(1) for text in lines if (m := DECL_RE.match(text))}
    imported: dict[str, str] = {}
    for text in lines:
        m = IMP_RE.match(text)
        if m:
            for name in m.group(1).split(","):
                name = name.strip()
                if name:
                    imported[name] = m.group(2)
    # 1) import 名字必须存在于源文件导出
    for name, source in imported.items():
        owners = decl.get(name, [])
        if source not in owners:
            problems.append(f"{path.name}: import {name} 未在 {source} 中定义（{owners or '无处定义'}）")
    # 2) 引用其它文件的模块名而未 import → 漏（在剥离注释/字符串/模板/正则后的代码上做词边界扫描）
    stripped = strip_non_code("\n".join(lines))
    code_lines = stripped.splitlines()
    for name, owners in decl.items():
        if name in declared_here or name in imported:
            continue
        if len(owners) == 1 and owners[0] != path.name:
            pattern = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
            hits = [m for m in pattern.finditer(stripped)]
            # 属性键（`name:`）与成员访问（`obj.name` / `obj?.name`）容忍：前者是对象字面量
            # 的键，后者是**别的对象**上的同名属性（如 `btn.dataset.usageDim`），两者都不是
            # 对本模块绑定的引用。不排除成员访问会把它误报成"引用未 import"（实测
            # 15-bind-events.js 的 `dataset.usageDim` 就是这么被当成 09-settings.js 的
            # 模块级 `usageDim` 的）。
            real = [
                h for h in hits
                if stripped[h.end():h.end() + 1] != ":"
                and stripped[h.start() - 1:h.start()] != "."
            ]
            if real:
                problems.append(f"{path.name}: 引用 {name}（{owners[0]}）但未 import")

for path in files:
    lines = path.read_text(encoding="utf-8").splitlines()
    n_export = sum(1 for t in lines if t.startswith("export "))
    n_import = sum(1 for t in lines if t.startswith("import "))
    print(f"{path.name}: export {n_export} 行, import {n_import} 行")

if problems:
    print("\n校验发现问题：")
    for p in problems:
        print("  " + p)
    raise SystemExit(1)
print("\n导入/导出一致性校验通过（无缺失、无遗漏候选）")
