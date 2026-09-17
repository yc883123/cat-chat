"""会话收尾清理：只保留「维护说明 §六」点名的可复用验证资产，其余一次性脚本/产物/临时目录删除。

用法：
    .venv\\Scripts\\python.exe verify\\cleanup_verify.py            # dry-run，只打印计划
    .venv\\Scripts\\python.exe verify\\cleanup_verify.py --apply    # 先打包备份，再删除

配套规则见维护说明 §8.7（用户表示结束会话时先问、再按此清理）。安全边界：
- 删除前把「所有将被删除的脚本」压进 verify/_archive/verify_scripts_<日期>.zip；
- 同时写 _archive/deleted_manifest.txt 记录被删文件与体积；
- **凡受版本控制的文件一律不删**（`git ls-files verify` 兜底，见下面 TRACKED）——
  只动 verify 下的未跟踪产物，不碰仓库任何已跟踪文件；_archive/ 永不清。
"""
from __future__ import annotations

import datetime
import pathlib
import shutil
import subprocess
import sys
import zipfile

TMP = pathlib.Path("verify")
ARCHIVE = TMP / "_archive"
APPLY = "--apply" in sys.argv

# ---- 保留清单：维护说明 §六 点名的验证资产 + 本脚本自身 ----
KEEP = {
    # 静态检查器 / 无浏览器校验
    "scan_undef_all.py", "scan_unused_pkg.py", "esm_cross_assign.py",
    "esm_unused_imports.py", "esm_graph_check.py", "tdz_check.py",
    "dup_ids.py", "sitecustomize.py", "media_markup_check.mjs",
    "skill_prompt_check.mjs", "verify_doc_encoding.py",
    # 浏览器冒烟
    "browser_smoke.cjs", "topbar_smoke.cjs", "pending_files_smoke.cjs",
    "send_button_smoke.cjs", "send_state_smoke.cjs", "file_ref_smoke.cjs",
    "lightbox_smoke.cjs", "usage_rate_smoke.cjs", "media_inline_smoke.cjs",
    "p4_display_smoke.cjs", "p5_writeback_smoke.cjs", "p6_comfy_render_smoke.cjs",
    "provider_cards_smoke.cjs", "agent_cards_smoke.cjs", "agent_avatar_smoke.cjs",
    "context_menu_smoke.cjs", "_check_turn_rail.cjs", "_check_tool_cards_compact.cjs",
    "sidebar_favorites_smoke.cjs", "agent_prompt_smoke.cjs", "quick_msg_smoke.cjs",
    "starter_reasoning_smoke.cjs", "mobile_shell_smoke.cjs", "mobile_ui_regression.cjs",
    # Agent 说明弹层 + 工作区路径/未注册分组提示冒烟（§六 已登记；Python 自编排 + Node 检查）
    "agent_help_popover.py", "agent_help_popover.cjs",
    # 以下 6 个写在 .gitignore 白名单、也登记在 §六，却一直没进本名单（2026-09-17 补）：
    # 靠 TRACKED 兜底才没被删，但「名单漂移」本身就该修——见 §九.91 与 tests/test_verify_assets.py。
    "frozen_interrupt_check.py", "frozen_q1_check.py",
    "q1_context_smoke.py", "q1_context_smoke.cjs",
    "stream_rerender_smoke.py", "stream_rerender_smoke.cjs",
    # 冻结版前端资源自检（§六 已登记）
    "frozen_mobile_ui_check.py",
    # 播种器
    "seed_usage_message.py", "seed_media_message.py", "seed_favorites.py",
    "seed_turn_rail_chat.py", "seed_agent_avatar_chat.py",
    "seed_legacy_builtin_agent.py", "make_test_card.py",
    # HTTP / 传输校验
    "p3_data_check.py", "p4_transport_check.py", "p6_comfy_extract_check.py",
    "p5_writeback_apply.py",
    # 数据维护 / 独立冒烟
    "backfill_media.py", "record_golden.py", "upload_smoke.py",
    "attachment_only_smoke.py",
    # 上下文圆环/提醒冒烟（§六 已登记）
    "ring_usage_smoke.py", "ring_usage_smoke.cjs",
    # 工具分类改版冒烟（§六 已登记；Python 自编排 + Node 检查）
    "tool_groups_smoke.py", "tool_groups_smoke.cjs",
    # 分支对话继承首轮上下文冒烟（§六 已登记；Python 自编排 + Node 检查）
    "branch_first_turn_smoke.py", "branch_first_turn_smoke.cjs",
    # 新会话边界 / 种子消息冒烟（§六 已登记；Python 自编排 + Node 检查）
    "session_start_smoke.py", "session_start_smoke.cjs",
    # 消息列表懒加载冒烟（§六 已登记；Python 自编排 + Node 检查）
    "lazy_messages_smoke.py", "lazy_messages_smoke.cjs",
    # 会话模型下拉冒烟（§六 已登记；Python 自编排 + Node 检查）
    "composer_model_smoke.py", "composer_model_smoke.cjs",
    # 发布清单同步自检（只读）
    "_release_check.py",
    # 「重新生成 / 编辑」冒烟（§六 已登记；Python 自编排 + Node 检查）
    "regenerate_smoke.py", "regenerate_smoke.cjs",
    # 第一批三项（全文搜索 / 分支导航 / 删除单条消息）联动冒烟（§六 已登记）
    "batch1_smoke.py", "batch1_smoke.cjs",
    # 第一批三项的冻结版实跑自检（§六 已登记）
    "frozen_batch1_check.py",
    # 软件更新「目标版本」下拉冒烟（§六 已登记；Python 自编排 + Node 检查）
    "update_version_smoke.py", "update_version_smoke.cjs",
    # 发版跟踪与 Release 资产核对（本机无 gh：走匿名 GitHub API）
    "release_watch.py",
    # 任务面板重做冒烟（§六 已登记；Python 自编排 + Node 检查 + 无浏览器渲染兜底）
    "tasks_panel_smoke.py", "tasks_panel_smoke.cjs", "tasks_panel_render_check.mjs",
    # 隔离实例的公共 harness：维护说明「归档说明」点名要求留在 verify/（被 tasks_panel_smoke.py 依赖）
    "_serve_tmp.py",
    # 本脚本自身
    "cleanup_verify.py",
}


def _tracked_names() -> set[str]:
    """仓库里已跟踪的 verify/ 文件名——它们正是 .gitignore 里 `!verify/*` 白名单放行的复用资产。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "verify"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return set()
    if out.returncode != 0:
        return set()
    return {pathlib.PurePath(line.strip()).name for line in out.stdout.splitlines() if line.strip()}


# KEEP 名单靠人手维护，漏登记一个就会被 --apply 删掉：2026-09-17 实测漏了 7 个受控脚本
# （`_serve_tmp.py`、`q1_context_smoke.py`/`.cjs`、`stream_rerender_smoke.py`/`.cjs`、
# `frozen_q1_check.py`、`frozen_interrupt_check.py`），而它们全都写在 §六 里、还在被别的脚本依赖。
# 所以：**受版本控制即保留**——.gitignore 的 !verify/* 白名单本来就是「§六 已登记的复用资产」的唯一口径，
# 两边自动对齐，不必再各记一份名单（新增脚本只需照 §六 补 .gitignore 白名单，这里同步生效）。
TRACKED = _tracked_names()
EXTRA_KEEP = sorted(n for n in TRACKED if n not in KEEP)
KEEP |= TRACKED

SCRIPT_EXT = {".py", ".cjs", ".mjs", ".js", ".ps1"}

if not TMP.is_dir():
    print(f"没有 {TMP}，无需清理")
    sys.exit(0)

files = [p for p in TMP.iterdir() if p.is_file()]
dirs = [p for p in TMP.iterdir() if p.is_dir() and p.name != "_archive"]
del_files = [p for p in files if p.name not in KEEP]
del_scripts = [p for p in del_files if p.suffix.lower() in SCRIPT_EXT]
del_other = [p for p in del_files if p.suffix.lower() not in SCRIPT_EXT]
keep_count = len(files) - len(del_files)

dir_bytes = sum(f.stat().st_size for d in dirs for f in d.rglob("*") if f.is_file())
total_kb = sum(p.stat().st_size for p in del_files) / 1024

print(f"保留 {keep_count} 个（§六 资产 + 本脚本）")
if not TRACKED:
    print("  ⚠ 未取到版本控制清单（不在仓库根跑？）——仅按 KEEP 名单保留，请勿在此状态下 --apply")
elif EXTRA_KEEP:
    print(f"  其中 {len(EXTRA_KEEP)} 个靠版本控制兜底保留（KEEP 名单漏登记）：{', '.join(EXTRA_KEEP)}")
print(f"将删脚本 {len(del_scripts)} 个 / 产物 {len(del_other)} 个（合计 {total_kb:.0f} KB）"
      f" / 临时目录 {len(dirs)} 个（{dir_bytes/1048576:.1f} MB）")
if del_files:
    print("  示例：", ", ".join(sorted(p.name for p in del_files)[:10]),
          "…" if len(del_files) > 10 else "")

if not APPLY:
    print("\n-- dry-run；确认后加 --apply 执行 --")
    sys.exit(0)

# ---- 1. 打包备份待删脚本 ----
ARCHIVE.mkdir(exist_ok=True)
# 用「日期-时刻」命名：同一天多次清理不会互相覆盖上一次的备份（本轮踩到过）。
stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
zip_path = ARCHIVE / f"verify_scripts_{stamp}.zip"
if del_scripts:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(del_scripts):
            zf.write(path, arcname=path.name)
    print(f"已备份 {len(del_scripts)} 个脚本 -> {zip_path}（{zip_path.stat().st_size/1024:.0f} KB）")

manifest = ARCHIVE / f"deleted_manifest_{stamp}.txt"
lines = [f"# {stamp} 清理 verify 记录", f"保留 {keep_count} 个；备份 {zip_path.name}", ""]
for path in sorted(del_scripts):
    lines.append(f"script  {path.stat().st_size:>8}  {path.name}")
for path in sorted(del_other):
    lines.append(f"artifact{path.stat().st_size:>8}  {path.name}")
for d in sorted(dirs):
    lines.append(f"dir             -  {d.name}/")
manifest.write_text("\n".join(lines), encoding="utf-8")
print(f"已写清单 -> {manifest}")


def _force_rmtree(target: pathlib.Path) -> None:
    """先常规删除；失败则清掉只读属性重试（浏览器临时 profile 有只读文件）。"""
    shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        for child in target.rglob("*"):
            try:
                child.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        raise RuntimeError(f"目录未能删除：{target}")


for path in del_files:
    path.unlink()
for d in dirs:
    _force_rmtree(d)

print(f"已删除 {len(del_files)} 个文件、{len(dirs)} 个目录")
print("verify 剩余：", ", ".join(sorted(p.name for p in TMP.iterdir())))
