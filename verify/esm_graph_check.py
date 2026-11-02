# -*- coding: utf-8 -*-
"""Check actual ESM exports and lexical bindings, without running app code.

Node's bundled Acorn supplies syntax/scopes; SourceTextModule.link validates
export contracts with the engine. No npm install, network, or browser required.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def analyze_modules(directory: Path) -> dict:
    script = Path(__file__).with_name("js_module_analysis.cjs")
    result = subprocess.run(
        ["node", "--expose-internals", "--experimental-vm-modules", str(script), str(directory.resolve())],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"JavaScript 语法分析失败（不是通过）：{result.stderr.strip()}")
    return json.loads(result.stdout)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "public" / "js"
    try:
        report = analyze_modules(directory)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(error)
        return 2
    for item in report["files"]:
        print(f'{item["file"]}: export {item["exports"]} 个, import {item["imports"]} 个绑定')
    for issue in report["esm"]:
        print(f'{issue["file"]}:{issue["line"]}: {issue["message"]}')
    if report["esm"]:
        return 1
    print("导入/导出及词法绑定校验通过（引擎链接通过，未执行应用代码）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
