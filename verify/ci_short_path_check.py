# -*- coding: utf-8 -*-
"""CI 环境差异自检：在「短路径 TEMP」下跑测试（GitHub runner 的 TEMP 是 8.3 短名）。

**为什么需要它**：runner 的 TEMP 带 8.3 短名（账户目录被截断成 `RUNNER~1`，整个路径
就是 `%USERPROFILE%` 那种形态的短名版本），而产品侧会把设置里的路径 `resolve()` 之后
再存（背景图 / 附件 / 工作区都是这个口径）。测试侧若拿**未 resolve** 的临时路径去比，
就会「本地全绿、CI 全红」——2.5.0-beta 的发布就是这么红的（7 条断言，全是
`naiba_realtarget\\tempXxx\\...` != `naiba~1\\tempXxx\\...`），而本机账户目录没有短名，
照不出来。见维护说明 §六 第 ③ 条 / §九.72。

做法：在临时目录下建一个名字带 `~1` 的 **junction** 指向真实目录，把 `TEMP`/`TMP` 指过去，
再跑与 CI 完全相同的那条命令（`python -m unittest discover -s tests`）。跑完自动拆掉 junction。

用法：
    .venv\\Scripts\\python.exe verify\\ci_short_path_check.py [测试模块 ...]
    （不给参数 = 全量 discover；给了 = 只跑这几个，如 `tests.test_chat_background`）

退出码：0 = 通过；1 = 短路径下测试失败（就是它要抓的问题）；2 = 环境准备失败。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 名字里必须带短名标记：resolve() 才会把它展开成长路径，从而复现 runner 的条件。
JUNCTION_NAME = "naiba~1"
TARGET_NAME = "naiba_ci_short_path_target"


def remove_junction(junction: Path) -> None:
    """只摘链接本身，绝不动目标目录的内容。

    注意不能用 `shutil.rmtree(junction)`：那会顺着链接把**目标目录**里的东西删掉。
    """
    try:
        os.rmdir(junction)
    except OSError:
        subprocess.run(["cmd", "/c", "rmdir", str(junction)], check=False, capture_output=True)


def build_junction() -> tuple[Path, Path]:
    base = Path(tempfile.gettempdir())
    target = base / TARGET_NAME
    junction = base / JUNCTION_NAME
    target.mkdir(parents=True, exist_ok=True)
    if junction.exists() or junction.is_symlink():
        remove_junction(junction)
    result = subprocess.run(  # noqa: S603 - 固定 argv
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=False, capture_output=True, text=True,
        # mklink 的输出走 OEM 代码页（本机 GBK）：不指定编码时读取线程会 UnicodeDecodeError，
        # 于是 `result.stdout` 变空、失败时的报错信息丢失（脚本本身在打印里只看 returncode）。
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"创建 junction 失败：{(result.stdout or '').strip()} {(result.stderr or '').strip()}")
    return junction, target


def main(argv: list[str]) -> int:
    if sys.platform != "win32":
        print("跳过：8.3 短名是 Windows 现象，本自检只在 Windows 上有意义")
        return 0
    target: Path | None = None
    junction: Path | None = None
    try:
        junction, target = build_junction()
        probe = junction / "probe"
        probe.mkdir(parents=True, exist_ok=True)
        short_path = str(probe)
        resolved_path = str(probe.resolve())
        if short_path == resolved_path:
            print(f"注意：{short_path} 未产生短名差异（系统可能关了 8.3 短名），本次自检覆盖有限")
        else:
            print(f"短路径条件就绪：{short_path}  ->  {resolved_path}")

        env = dict(os.environ, TEMP=str(junction), TMP=str(junction))
        command = [sys.executable, "-m", "unittest", *(argv or ["discover", "-s", "tests"])]
        run = subprocess.run(  # noqa: S603 - 固定 argv
            command, cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        output = (run.stdout or "") + (run.stderr or "")
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith(("FAIL:", "ERROR:", "AssertionError", "Ran ", "FAILED", "OK")):
                print(stripped[:240])
        if run.returncode != 0:
            print("短路径条件下测试失败：多半是测试侧比较了未 resolve 的临时路径（见 §六 第 ③ 条）")
            return 1
        print("短路径条件下通过")
        return 0
    except Exception as exc:  # noqa: BLE001 - 环境准备失败要出声，不要伪装成测试失败
        print(f"环境准备失败：{exc}")
        return 2
    finally:
        if junction is not None:
            remove_junction(junction)
        if target is not None:
            # junction 已摘掉，这里删的是真实目录本身；顺带清掉通过链接写进去的探针文件。
            shutil.rmtree(target, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
