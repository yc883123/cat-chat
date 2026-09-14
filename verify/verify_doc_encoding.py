"""校验文档类改动：UTF-8 无 BOM、无「意外」替换字符、关键表述自洽。

用法：.venv\\Scripts\\python.exe verify\\verify_doc_encoding.py
说明：维护说明 §九.18 故意保留了两处 U+FFFD 作为乱码指纹示例，按行白名单放行；
      其它位置出现 U+FFFD 一律判失败（这是 GBK 固化事故的指纹）。
"""
from __future__ import annotations

import ast
import pathlib
import sys

FILES = [
    ".gitignore",
    "README.md",
    "项目维护说明（修改代码前必读）.md",
]

# 允许出现的 U+FFFD 所在行必须含以下标记之一（§九.18 的乱码示例）
ALLOW_MARKERS = ("run_skill_script 实测", "can't find entry file")

failed = False
for name in FILES:
    data = pathlib.Path(name).read_bytes()
    has_bom = data[:3] == b"\xef\xbb\xbf"
    try:
        text = data.decode("utf-8")
        decode = "utf-8 OK"
    except UnicodeDecodeError as exc:
        text = ""
        decode = f"DECODE FAIL: {exc}"
        failed = True
    unexpected = [
        (idx, line)
        for idx, line in enumerate(text.split("\n"), 1)
        if "\ufffd" in line and not any(m in line for m in ALLOW_MARKERS)
    ]
    if has_bom or decode != "utf-8 OK" or unexpected:
        failed = True
    total = data.count(b"\xef\xbf\xbd")
    print(f"{name}: BOM={has_bom} {decode} U+FFFD={total} 非示例={len(unexpected)} "
          f"lines={text.count(chr(10))}")
    for idx, line in unexpected:
        print(f"    line {idx}: {line[:120].encode('unicode_escape').decode('ascii')}")

# 内容自洽：文档改动的关键表述
doc = pathlib.Path("项目维护说明（修改代码前必读）.md").read_text(encoding="utf-8")

# 这两个数字会随代码与用例增长而变。历史上它们被写死成 69 文件 / 844 用例，文档早已跑到
# 70 / 920，于是守门自己成了长期噪声（FAIL 里还看不出该怎么修）。改为**从实际数据推导**后再比对：
# 文件数口径与 verify/scan_undef_all.py 的 TARGETS 一致（naiba/ 全包 + server.py + launcher.py），
# 用例数用 AST 静态数 tests/ 下的 def test*（与 unittest discover 实测一致，且不导入任何测试模块）。
ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG_FILE_COUNT = len(list(ROOT.joinpath("naiba").rglob("*.py"))) + len(
    [p for p in (ROOT / "server.py", ROOT / "launcher.py") if p.exists()]
)
TEST_CASE_COUNT = 0
for _test_file in ROOT.joinpath("tests").rglob("test_*.py"):
    for _node in ast.walk(ast.parse(_test_file.read_text(encoding="utf-8"))):
        if isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _node.name.startswith("test"):
            TEST_CASE_COUNT += 1

checks = {
    "§3.1 树含 .venv/": "├── .venv/" in doc,
    "§六 解释器纪律": "**解释器纪律（本机唯一正确用法）**" in doc,
    "§六 编译用 venv": "项目根\\.venv\\Scripts\\python.exe -m PyInstaller" in doc,
    "无 518 残留": "518 用例" not in doc,
    f"§六 检查器 {PKG_FILE_COUNT} 文件": f"（{PKG_FILE_COUNT} 文件 0 候选）" in doc,
    "§六 不再提 verify_split_merge": "verify_split_merge" not in doc,
    "§六 补工具卡片检查": "_check_tool_cards_compact.cjs" in doc,
    f"§六 {TEST_CASE_COUNT} 用例": f"（{TEST_CASE_COUNT} 用例，" in doc,
    "§3.4 分区无重复标题": "不再有灰色标题块与固定高度" in doc,
    "§九.50 toast top layer": "底部提示框（`#toast`）是同一个坑" in doc,
    "§3.4 Agent 分区切换": "弹层内改为分区切换" in doc,
    "§六 提示注入口径守门": "test_prompt_gating" in doc,
    "§六 分支首轮冒烟": "branch_first_turn_smoke.py" in doc,
    "§四 分支继承首轮上下文": "分支对话在分支点不是首条消息时继承该列" in doc,
    "§3.3 v17 迁移": "迁移 v1-v17" in doc,
    "§四 快捷提示词页下线": "设置页的「快捷提示词」页已整体下线" in doc,
    "§3.3 视觉文案分流": "按模型视觉能力分「分析/装载」两种文案" in doc,
    "§九.23 条件注入口径": "按会话固化工具集条件注入" in doc,
    "§8.8 路径纪律": "### 8.8 路径纪律：禁止本机绝对路径" in doc,
    "§六 路径守门": "test_no_absolute_paths" in doc,
    "§3.4 发送前判定": "pendingContextWarning" in doc,
    "§3.4 5% 重新提醒": "CONTEXT_WARNING_STEP=5" in doc,
    "§九.52 实时值": "52. **\"实时值\"必须有主" in doc,
    "§六 弹窗几何断言": "高度 ≤240px" in doc,
    "§3.4 圆环唯一写入点": "上下文圆环/弹层的唯一写入点" in doc,
    "§3.4 提醒阈值": "上下文提醒阈值" in doc,
    "§六 圆环守门": "test_context_ring" in doc,
    "§六 圆环冒烟": "ring_usage_smoke.py" in doc,
    "§六 弹窗冒烟": "contextWarningDialog" in doc,
    "§3.3 5xx 退避": "429 + 全部 5xx" in doc,
    "§九.51 5xx 教训": "51. **供应商 5xx 是瞬时故障" in doc,
    "§六 5xx 守门测试": "test_llm_runtime_retry" in doc,
    "§8.1 链含第 9 步": "9. 会话收尾（用户表示结束会话时）" in doc,
    "§8.7 会话收尾清理": "### 8.7 会话收尾清理" in doc,
    "§8.7 指向一键脚本": "verify\\cleanup_verify.py" in doc,
    "§3.3 工具分类 7 组": "7 组单一维度分类" in doc,
    "§3.3 预设 5 档": "**5 档**" in doc,
    "§四 PDF 条件注入": "仅当会话工具集含 `read_pdf` 时注入" in doc,
    "§3.4 风险徽标": "group-badge" in doc,
    "§六 工具分类冒烟": "tool_groups_smoke.py" in doc,
}
readme = pathlib.Path("README.md").read_text(encoding="utf-8")
checks["README 无 public/app.js"] = "public/app.js" not in readme
checks["README js 逐个检查"] = "Get-ChildItem public\\js\\*.js" in readme
ignore = pathlib.Path(".gitignore").read_text(encoding="utf-8")
checks[".gitignore 含 .workbuddy/"] = "\n.workbuddy/\n" in ignore
checks[".gitignore 注释无乱码"] = "验证脚本目录" in ignore
checks[".gitignore 默认忽略 verify 产物"] = "\nverify/*\n" in ignore
checks[".gitignore 放行可复用脚本"] = "\n!verify/scan_undef_all.py\n" in ignore
checks[".gitignore 放行工具分类冒烟"] = "\n!verify/tool_groups_smoke.cjs\n" in ignore
checks["§六 目录分工表"] = "**目录分工（`tests/` vs `verify/`）**" in doc
checks["无 .tmptest 残留"] = ".tmptest" not in doc

for label, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failed = True

sys.exit(1 if failed else 0)
