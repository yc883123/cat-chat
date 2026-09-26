# -*- coding: utf-8 -*-
r"""把教程 `README.md` 渲染成带样式的 HTML，供 Edge 无头打印成 PDF。

用法：
    .venv\Scripts\python.exe scripts\make_tutorial_html.py <教程目录> [<教程目录> ...]
    .venv\Scripts\python.exe scripts\make_tutorial_html.py docs\tutorial-01-first-chat

不带参数时处理 docs\ 下所有 `tutorial-*` 目录。

产出：每个目录下 `_tutorial.html`（`_` 前缀 + 已在 .gitignore 里，属临时中间物）。
打印 PDF（Edge 无头，中文用系统字体，不需要额外依赖）：
    msedge --headless --disable-gpu --no-pdf-header-footer ^
           --print-to-pdf="<目录>\<标题>.pdf" "file:///<目录>/_tutorial.html"
"""
from __future__ import annotations

import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  @page {{ margin: 16mm 14mm; }}
  body {{
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    max-width: 860px; margin: 0 auto; padding: 24px 32px;
    color: #24292f; line-height: 1.75; font-size: 14px;
  }}
  h1 {{ font-size: 24px; border-bottom: 3px solid #d6336c; padding-bottom: 10px; }}
  h2 {{ font-size: 18px; margin-top: 28px; border-left: 5px solid #d6336c; padding-left: 10px; }}
  h3 {{ font-size: 15.5px; margin-top: 22px; }}
  img {{ max-width: 100%; border: 1px solid #e5e5e5; border-radius: 8px; margin: 10px 0; }}
  code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 4px; font-size: 13px; }}
  pre {{ background: #f6f8fa; padding: 12px 16px; border-radius: 8px; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{
    margin: 12px 0; padding: 10px 16px; color: #555;
    border-left: 4px solid #d0d7de; background: #f6f8fa; border-radius: 0 8px 8px 0;
  }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th, td {{ border: 1px solid #d0d7de; padding: 8px 12px; text-align: left; }}
  th {{ background: #f6f8fa; }}
  hr {{ border: none; border-top: 1px solid #d0d7de; margin: 24px 0; }}
  strong {{ color: #b51d5e; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def tutorial_dirs(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a).resolve() for a in argv]
    return sorted(p for p in (ROOT / "docs").glob("tutorial-*") if p.is_dir())


def first_h1(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def render(directory: Path) -> Path | None:
    source = directory / "README.md"
    if not source.is_file():
        print("跳过（没有 README.md）：", directory, file=sys.stderr)
        return None
    text = source.read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    out = directory / "_tutorial.html"
    out.write_text(
        TEMPLATE.format(title=first_h1(text, directory.name), body=body), encoding="utf-8"
    )
    print("wrote", out, file=sys.stderr)
    return out


def main() -> int:
    dirs = tutorial_dirs(sys.argv[1:])
    if not dirs:
        print("没有找到任何教程目录", file=sys.stderr)
        return 1
    for directory in dirs:
        render(directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
