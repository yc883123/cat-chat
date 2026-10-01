"""Check documentation encoding and keep the maintenance guide bounded.

Usage::

    .venv\\Scripts\\python.exe verify\\verify_doc_encoding.py

The maintenance guide is a snapshot, not an append-only incident log. This
guard deliberately checks only its stable contract and size policy; it does
not duplicate volatile test counts or historical section numbers.
"""
from __future__ import annotations

import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
MAINTENANCE_DOC = "项目维护说明（修改代码前必读）.md"
ENCODING_FILES = (".gitignore", "README.md", MAINTENANCE_DOC)

# Keep the document small enough that every maintenance pass can read it in
# full. The value is also stated in the guide and checked as an anchor below.
MAX_DOC_BYTES = 120 * 1024
MAX_DOC_LINES = 500

# These are the durable promises that the compressed guide must retain. Do
# not add incident-specific prose or dynamic counts here: those are exactly
# what caused the guide and this check to grow together in the past.
REQUIRED_ANCHORS = {
    "体积与内容边界": "## 0. 体积与内容边界（硬规则）",
    "容量政策": "主文上限：**120 KB / 500 行**",
    "当前版本": "Cat Chat 2.9.14 Beta",
    "事件契约": "naiba/core/contracts.py",
    "Playwright 验收": "Playwright 前端验收（每次改动必做）",
    "路径纪律": "### 8.8 路径纪律：禁止本机绝对路径",
    "消息列表契约": "list_primary",
    "停止状态契约": "stopping",
    "推理回放上限": "MODEL_REASONING_REPLAY_MAX_CHARS",
    "推理回放配置": "reasoning_replay_max_chars",
    "推理回放预算": "_ReasoningReplayBudget",
    "本地首字节超时": "LocalModelFirstByteTimeout",
    "本地首字节配置": "local_first_byte_timeout_seconds",
    "锁标签": "lock_label",
    "Agent 步数上限": "agent_step_limit",
    "本地上下文记忆": "remember_local_context_window",
    "上下文探测": "context_window_probed",
    "默认步数": "DEFAULT_MAX_STEPS",
}


def _check_encoding(path: pathlib.Path) -> bool:
    """Print and validate UTF-8/BOM/replacement-character status for *path*."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"FAIL {path.relative_to(ROOT)}: cannot read: {exc}")
        return False

    has_bom = data.startswith(b"\xef\xbb\xbf")
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"FAIL {path.relative_to(ROOT)}: UTF-8 decode failed: {exc}")
        return False

    replacement_count = decoded.count("\ufffd")
    line_count = len(decoded.splitlines())
    ok = not has_bom and replacement_count == 0
    print(
        f"{'PASS' if ok else 'FAIL'} {path.relative_to(ROOT)}: "
        f"BOM={has_bom} utf-8 OK U+FFFD={replacement_count} "
        f"bytes={len(data)} lines={line_count}"
    )
    if has_bom:
        print("    UTF-8 BOM is not allowed")
    if replacement_count:
        print("    replacement characters (U+FFFD) are not allowed")
    return ok


def main() -> int:
    failed = False
    for name in ENCODING_FILES:
        failed = not _check_encoding(ROOT / name) or failed

    doc_path = ROOT / MAINTENANCE_DOC
    try:
        doc_bytes = doc_path.read_bytes()
        doc = doc_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"FAIL {MAINTENANCE_DOC}: cannot load maintenance guide: {exc}")
        return 1

    doc_lines = len(doc.splitlines())
    if len(doc_bytes) > MAX_DOC_BYTES:
        print(
            f"FAIL maintenance guide size: {len(doc_bytes)} bytes "
            f"> {MAX_DOC_BYTES} bytes ({MAX_DOC_BYTES // 1024} KB)"
        )
        failed = True
    else:
        print(
            f"PASS maintenance guide size: {len(doc_bytes)} bytes "
            f"<= {MAX_DOC_BYTES} bytes ({MAX_DOC_BYTES // 1024} KB)"
        )

    if doc_lines > MAX_DOC_LINES:
        print(f"FAIL maintenance guide lines: {doc_lines} > {MAX_DOC_LINES}")
        failed = True
    else:
        print(f"PASS maintenance guide lines: {doc_lines} <= {MAX_DOC_LINES}")

    for label, token in REQUIRED_ANCHORS.items():
        present = token in doc
        print(f"{'PASS' if present else 'FAIL'} anchor {label}: {token}")
        failed = not present or failed

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
