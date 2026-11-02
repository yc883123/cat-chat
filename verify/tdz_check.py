# -*- coding: utf-8 -*-
"""ESM eager-read TDZ gate: syntax-aware imports and initialization order.

Closures do not read captured bindings when constructed. Direct top-level
function calls are followed; browser callbacks are not executed by this scan.
This is not a general proof for arbitrary runtime control flow, dynamic import,
or unknown higher-order callbacks. Real browser startup remains a separate gate.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from esm_graph_check import analyze_modules


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "public" / "js"
    try:
        report = analyze_modules(directory)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(error)
        return 2
    # Broken linking prevents a sound initialization-order verdict.
    issues = report["esm"] + report["tdz"]
    for issue in issues:
        print(f'{issue["file"]}:{issue["line"]}: {issue["message"]}')
    print(f'TDZ 初始化读取风险 {len(report["tdz"])} 处；ESM 绑定问题 {len(report["esm"])} 处')
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
