# -*- coding: utf-8 -*-
"""冻结版自检：确认创作场「原文/改编稿 + 记录管理」整改真的进了 exe。

用法：`dist\\<exe>.exe --run-skill-script <本文件绝对路径>`

存在理由：这次整改横跨三层——
① 打包的 Python（`naiba/novel/*`：稿件记录、schema 2 迁移、预算预检、批量删除）；
② 打包的前端资源（`public/index.html`、`styles.css`、`public/js/25-novel.js`）；
③ 数据目录解析（右栏显示的保存路径必须来自后端）。

源码模式读仓库 `public/` 与 `naiba/` 永远是新的，只有冻结版能回答
「用户拿到的那个 exe 里到底是哪一版」。纯逻辑断言在
`tests/test_novel_manuscripts.py` / `tests/test_novel_records_api.py`，
真浏览器断言在 `verify/novel_ui_smoke.cjs`；本文件只核对**打包内容与资源**。

CRLF/LF 差异先归一：Windows 打包可能把文本资源的换行改掉，
不归一就会把「换行不同」误判成「漏打包」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 冻结版没打 unittest（`import unittest.mock` 会 ModuleNotFoundError），这里手写最小替身。

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition or not detail else f'  -> {detail}'}")
    if not condition:
        ok = False


def _read(path: Path) -> str:
    """读文本资源并归一换行（CRLF/LF 差异不算漏打包）。"""
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


# ---- ① 打包内的前端资源 ----
try:
    from naiba.paths import default_path_context  # noqa: E402

    public = Path(default_path_context().public_dir)
    index = _read(public / "index.html")
    css = _read(public / "styles.css")
    novel_js = _read(public / "js" / "25-novel.js")
except Exception as exc:  # noqa: BLE001
    check("可读到打包内的前端资源", False, repr(exc))
    print("\n存在未通过项")
    sys.exit(1)

check("前端：阶段栏有「管理记录」入口", 'id="novelManageBtn"' in index)
check("前端：管理面板存在", 'id="novelManageDialog"' in index)
check("前端：原文/改编稿卡片容器", 'id="novelManuscripts"' in index)
check("前端：AI 润色入口", 'id="novelAiEdit"' in index)
check("前端：逐章稿件选择容器", 'id="novelPickManuscripts"' in index)
check("前端：每章字数滑条", 'id="novelBodyChars"' in index and 'min="3000"' in index
      and 'max="15000"' in index)
check("前端：章数上限滑条可到 20", 'id="novelMaxChapters"' in index and 'max="20"' in index)
check("前端：右栏动态说明容器", 'id="novelPlanBody"' in index)
check("前端：保存位置与复制路径", 'id="novelLocationPath"' in index and 'id="novelCopyPath"' in index)
# 守门口径同 tests/test_topbar.py：工作台模块壳里不能声明未实现的 tabs 语义。
# 只截取创作场这一块（到它的收尾 `</div>` 前一层的结束标记），其它既有面板的 tabs 不属于本模块。
_start = index.find('id="studioPageNovel"')
_end = index.find("</section>", index.find('id="novelFileInput"', _start))
novel_section = index[_start:_end] if _start >= 0 and _end > _start else ""
check("前端：创作场区块可定位", bool(novel_section))
check("前端：创作场未声明未实现的 tabs 语义", 'role="tablist"' not in novel_section)

check("JS：保存改编稿走独立入口", "saveManuscriptEdit" in novel_js)
check("JS：读单份稿件正文", "/manuscripts/" in novel_js)
check("JS：预算预检接口", "/api/novel/stage/preview" in novel_js)
check("JS：记录删除接口", "/api/novel/records/delete" in novel_js)
check("JS：任务删除接口", "/api/novel/tasks/delete" in novel_js)
check("JS：不把打开位置暴露给手机", "window.pywebview?.api" in novel_js)
check("JS：展开状态按稿件记录", "expandedTouched" in novel_js)

check("CSS：管理记录入口固定在阶段栏", ".novel-manage-entry" in css)
check("CSS：稿件卡片与正文区", ".novel-manuscript-card" in css and ".novel-manuscript-body" in css)
check("CSS：双滑条", ".novel-limit input[type=\"range\"]" in css)
check("CSS：手机管理面板全屏", ".novel-manage-dialog" in css)
check("CSS：手机分档按钮有焦点反馈", ".novel-pane-btn:focus-visible" in css)

# ---- ② 打包内的 Python 实现 ----
try:
    from naiba.novel import manuscripts, prompts  # noqa: E402
    from naiba.novel.service import SCHEMA_VERSION, NovelStore  # noqa: E402

    check("Python：作品清单已是 schema 2", int(SCHEMA_VERSION) == 2, str(SCHEMA_VERSION))
    check("Python：稿件类型齐全",
          set(manuscripts.KINDS) == {"original", "legacy", "manual", "ai"},
          str(manuscripts.KINDS))
    check("Python：每章字数上限 3000–15000",
          (prompts.BODY_SLOT_MIN, prompts.BODY_SLOT_MAX) == (3000, 15000))
    check("Python：章数上限可到 20", prompts.MAX_CHAPTERS_MAX == 20)
    check("Python：预算报告标为估算", "估算" in prompts.budget_report(
        content="x", chapters=[], warnings=[], body_limit=3000, max_chapters=6,
    )["estimate_note"])
    check("Python：单章改编阶段已注册", prompts.ADAPT_STAGE in prompts.ALL_STAGES)

    from naiba.novel.service import NovelService  # noqa: E402

    for name in ("api_save_manuscript", "api_get_manuscript", "api_records",
                 "api_delete_records", "api_delete_tasks", "api_stage_preview",
                 "api_location", "api_reveal"):
        check(f"Python：NovelService.{name}", hasattr(NovelService, name))
except Exception as exc:  # noqa: BLE001
    check("可读到打包内的小说域实现", False, repr(exc))

# ---- ③ 数据位置由后端解析 ----
try:
    from naiba.paths import default_path_context  # noqa: E402

    paths = default_path_context()
    root = Path(paths.data_dir) / "studio" / "novel"
    check("数据目录：作品保存在 data/studio/novel 下",
          root.parent.name == "studio" and root.name == "novel", str(root))
    check("数据目录：路径是绝对路径", root.is_absolute(), str(root))
    check("数据目录：冻结版 app_dir 与数据目录分离或同级",
          isinstance(paths.app_dir, Path))
except Exception as exc:  # noqa: BLE001
    check("可解析数据目录", False, repr(exc))

print()
if ok:
    print("全部通过")
else:
    print("存在未通过项")
    sys.exit(1)
