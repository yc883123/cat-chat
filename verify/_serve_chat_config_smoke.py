# -*- coding: utf-8 -*-
"""「对话参数」冒烟专用隔离实例启动器（verify/ 已 gitignore，不入库）。

与 _smoke_serve.py 的差别只有一处：**不改 exe_dir**（保持 app_dir == exe_dir），
让 ``migrate_legacy_data`` 的"源码模式直接跳过"分支生效——否则启动会把仓库根的
真实 ``data/``（用户工作流库）合并进隔离实例，冒烟截图与断言都会看到真实数据。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ["NAIBA_SMOKE_ROOT"]).resolve()
ROOT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(REPO))

from naiba.http import main_entry  # noqa: E402
from naiba.paths import PathContext  # noqa: E402

pc = PathContext.local(ROOT, ROOT / "config.json")
# 静态资源直接用仓库本体（只读），不复制；exe_dir 保持指向隔离根（迁移跳过）。
pc.public_dir = (REPO / "public").resolve()
pc.resource_dir = REPO
main_entry(pc)
