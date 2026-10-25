# -*- coding: utf-8 -*-
"""拆书匠命令行脚本：把一本 TXT 小说按章拆成文件，**全程零 AI**。

为什么单独做这条入口（《小说改编接入工作台实施计划》§三）：
只想把一本书拆成文件时，「起服务 → 建会话 → 模型判断 → 调工具」太重，而且模型在长
会话里可能自作主张改参数。脚本路径是确定性的：同一本书、同一参数，结果永远一致，
不烧 token，也不需要应用在运行。

零 AI 边界（守门测试 `tests/test_novel_script.py` 用 AST 钉死）：
只 import 标准库 + `naiba.core.novel_split`（纯算法）+ `naiba.tools.providers.novel`
（编码嗅探）。**禁止** import `naiba.app` / `naiba.llm.*` / `naiba.run.*` / `naiba.jobs`
以及任何会话、Run、模型模块——否则「不启动服务就能跑」这条保证就没了。

两种跑法：
    ./.venv/Scripts/python.exe scripts/split_novel.py 书.txt --out ./out
    dist\\naiba-chat.exe --run-skill-script scripts\\split_novel.py 书.txt --out ./out

输出编码：冻结版父进程按 UTF-8 解码子进程输出（`launcher.py`），所以这里必须显式把
stdout 设成 UTF-8，否则中文摘要行在控制台是乱码。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from naiba.core import novel_split  # noqa: E402
from naiba.tools.providers.novel import NovelError, read_text_auto  # noqa: E402

# 退出码：调用方（脚本、CI、用户）据此判断失败原因，不靠解析中文。
EXIT_OK = 0
EXIT_BAD_FILE = 2
EXIT_BAD_ENCODING = 3
EXIT_EMPTY = 4
EXIT_REFUSED = 5

# 拆分层级的中文说明：降级到符号分节/等分时，必须让用户知道这不是常规识别。
LEVEL_LABELS = {
    "heading": "按章节标题识别",
    "sep": "按符号分节（没有识别到章节标题）",
    "single": "只识别到 1 个标题，仍按它切分",
    "len": "没有识别到任何标题，按字数等分",
}

# Windows 文件名非法字符 + 控制字符；标题要落成文件名，必须先洗干净。
_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]')
_MAX_FILENAME_TITLE = 40

# 支持显式指定的编码（关掉自动嗅探）。顺序与自动嗅探一致，便于对照。
MANUAL_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5")


def _ensure_utf8_stdio() -> None:
    """冻结版父进程按 UTF-8 解码子进程输出；不设就会在控制台出乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _safe_title(title: str) -> str:
    """标题转文件名：非法字符换下划线、压空白、限长；空标题回「未命名」。"""
    cleaned = _BAD_FILENAME.sub("_", str(title or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return "未命名"
    return cleaned[:_MAX_FILENAME_TITLE].strip(" .") or "未命名"


def _detect_level(text: str, max_len: int) -> str:
    """拆分层级交给算法层判定：与工作台共用同一份，避免两边口径漂移。"""
    return novel_split.detect_level(text, max_len)


def _read(path: Path, encoding: str) -> tuple[str, str]:
    """按 auto 或指定编码读盘。auto 走工具层同一份嗅探，保证与工作台结果一致。"""
    if encoding == "auto":
        return read_text_auto(path)
    data = path.read_bytes()
    return data.decode(encoding), encoding


def _write_chapters(chapters: list[dict[str, object]], out_dir: Path, fmt: str) -> list[Path]:
    """按格式落盘，返回写出的文件列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if fmt == "json":
        payload = {
            "total": len(chapters),
            "chars": sum(len(str(row["body"])) for row in chapters),
            "chapters": [
                {"i": index, "title": str(row["title"]), "len": len(str(row["body"]))}
                for index, row in enumerate(chapters)
            ],
        }
        path = out_dir / "index.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return [path]
    if fmt == "md":
        parts = []
        for index, row in enumerate(chapters, start=1):
            parts.append(f"# {row['title']}\n\n{row['body']}\n")
        path = out_dir / "book.md"
        path.write_text("\n".join(parts), encoding="utf-8")
        return [path]
    for index, row in enumerate(chapters, start=1):
        name = f"{index:04d}_{_safe_title(str(row['title']))}.txt"
        path = out_dir / name
        path.write_text(str(row["body"]), encoding="utf-8")
        written.append(path)
    return written


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="split_novel.py",
        description="把一本 TXT 小说按章拆分（零 AI：不联网、不调模型、不需要应用在运行）。",
    )
    parser.add_argument("file", help="要拆分的 TXT 文件路径")
    parser.add_argument("--out", default="", help="输出目录（默认：<文件名>_chapters/）")
    parser.add_argument("--format", choices=("txt", "md", "json"), default="txt",
                        help="txt = 每章一个文件；md = 合并成一个 md；json = 只写索引")
    parser.add_argument("--max-len", type=int, default=novel_split.DEFAULT_MAX_LEN,
                        help=f"单章字符上限，超长章会再切块（默认 {novel_split.DEFAULT_MAX_LEN}）")
    parser.add_argument("--encoding", default="auto",
                        help="auto（默认，自动嗅探）或显式指定：" + " / ".join(MANUAL_ENCODINGS))
    parser.add_argument("--dry-run", action="store_true", help="只打印章节索引，不写盘")
    parser.add_argument("--force", action="store_true", help="输出目录已存在且非空时仍覆盖")
    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    args = _parser().parse_args(argv)

    src = Path(args.file).expanduser()
    if not src.is_file():
        print(f"文件不存在或不是普通文件：{src}", file=sys.stderr)
        return EXIT_BAD_FILE
    if args.max_len < 200 or args.max_len > 200000:
        print(f"--max-len 需要在 200–200000 之间（收到 {args.max_len}）", file=sys.stderr)
        return EXIT_BAD_FILE
    if args.encoding != "auto" and args.encoding not in MANUAL_ENCODINGS:
        print(f"--encoding 只支持 auto 或 {' / '.join(MANUAL_ENCODINGS)}", file=sys.stderr)
        return EXIT_BAD_ENCODING

    try:
        text, encoding = _read(src, args.encoding)
    except NovelError as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return EXIT_BAD_FILE
    except (OSError, UnicodeDecodeError) as exc:
        # 显式指定编码解不开时也归到「编码无法识别」，让调用方按同一码分支处理。
        print(f"用 {args.encoding} 读取失败：{exc}", file=sys.stderr)
        return EXIT_BAD_ENCODING

    if not text.strip():
        print("文件里没有可拆的内容", file=sys.stderr)
        return EXIT_EMPTY

    chapters = novel_split.split_chapters(text, args.max_len)
    if not chapters:
        print("没有拆出任何章节（文件可能只有空白或不支持的格式）", file=sys.stderr)
        return EXIT_EMPTY

    level = _detect_level(text, args.max_len)
    total_chars = sum(len(str(row["body"])) for row in chapters)

    # 摘要行：书名 / 章数 / 总字数 / 编码 / 拆分层级。编码口径照算法层注释——这是
    # 「实际选中的解码方式」，不是对源编码的确定性判定（gb18030 能解很多其它编码）。
    print(f"文件：{src.name}")
    print(f"章数：{len(chapters)}")
    print(f"总字数：{total_chars}")
    print(f"编码：{encoding}（实际选中的解码方式，不代表源文件就是这个编码）")
    print(f"拆分层级：{level} —— {LEVEL_LABELS[level]}")

    if args.dry_run:
        print("\n章节索引（--dry-run，未写盘）：")
        for index, row in enumerate(chapters, start=1):
            print(f"  {index:>4}. {row['title']}（{len(str(row['body']))} 字）")
        return EXIT_OK

    out_dir = Path(args.out).expanduser() if args.out else src.parent / f"{src.stem}_chapters"
    if out_dir.exists() and any(out_dir.iterdir()) and not args.force:
        print(
            f"\n输出目录已存在且不为空：{out_dir}\n"
            "为避免覆盖已有文件，本次没有写入。确认要覆盖请加 --force，或换一个 --out。",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    written = _write_chapters(chapters, out_dir, args.format)
    print(f"\n已写入 {len(written)} 个文件到：{out_dir}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
