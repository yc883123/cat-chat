# -*- coding: utf-8 -*-
r"""一次性探针：核对「改过的前端文件 / 新增的内置 Skill 是否真被打进 exe」。

存在理由：手机访问的是**冻结版内置静态资源**（`sys._MEIPASS/public/…`），源码模式读的是
工作树里的 `public/`——两处不一致时，源码怎么跑都对、手机永远看不到变化（§九.128 同因）。
所以「编译完了」必须再确认一遍「打进去的那份 == 我改的那份」。Skill 同理：
`skills/` 随包同步进 `_MEIPASS/skills`，**教程助手的知识库（cat-chat-guide）漏进包就等于
出厂绑定指向空气**。

用法（对刚编出来的 exe 跑，不初始化 GUI/HTTP/锁）：
    dist\naiba-chat.exe --run-skill-script verify\_probe_packed_markers.py

本轮（2.9.2-beta：老三样退役 + 教程助手知识库扩成 8 篇教程 + storage 写路径重试）改过 / 新增的文件：
    naiba/config.py（出厂 agents 清空 + _migrate_retired_factory_agents）,
    naiba/storage/store.py（_write_with_retry 包住 append_run_event / update_job：WAL 瞬时故障重试。
      属后端代码，**不进下面 CHANGED 的静态资源比对**，此处只作改动索引）,
    public/index.html（顶栏说明弹层里老三样那句改成「已退役」口径）,
    public/js/09-settings.js（默认 agent 兜底 id general → master）,
    skills/cat-chat-guide/SKILL.md（references 索引扩成 8 篇）,
    skills/cat-chat-guide/references/00-手册要点.md（新增 §7.1 六个内置 Agent 分工）,
    skills/cat-chat-guide/references/{10,20,30,40,50,60,70,80}-*.md（**本轮新增 8 篇**，
      其中 10-教程-3分钟用ComfyUI出图.md 已被 20-教程2-3分钟用ComfyUI出图.md 取代）

下面 CHANGED 里那 6 个前端文件是 2.9.0 轮次的重点比对项，仍逐字节核对 + 指纹，留作回归
——它们本轮没动，但**不该在包里走样**。
"""
from __future__ import annotations

import hashlib
import sys
import traceback
from pathlib import Path

# 本轮改过、且会被前端消费的文件（逐个比对 + 指纹）。
CHANGED = [
    "index.html",
    "styles.css",
    "js/01-core.js",
    "js/04-messages.js",
    "js/09-settings.js",
    "js/15-bind-events.js",
]

# 本轮新增 / 必须存在的内置 Skill（**必须在包里**，否则出厂绑定失效）。
REQUIRED_SKILLS = [
    "copywriting/SKILL.md",
    "cat-chat-guide/SKILL.md",
    "cat-chat-guide/references/00-手册要点.md",
    "cat-chat-guide/references/10-教程1-3分钟第一次对话.md",
    "cat-chat-guide/references/20-教程2-3分钟用ComfyUI出图.md",
    "cat-chat-guide/references/30-教程3-3分钟用RunningHub云端出图.md",
    "cat-chat-guide/references/40-教程4-3分钟出一集短剧.md",
    "cat-chat-guide/references/50-教程5-3分钟改出一条好提示词.md",
    "cat-chat-guide/references/60-教程6-3分钟让AI写个小工具.md",
    "cat-chat-guide/references/70-教程7-3分钟给CatChat装个新技能.md",
    "cat-chat-guide/references/80-教程8-3分钟认识教程助手.md",
]

# 本轮改过的 Skill 文件里的「已生效」指纹（缺一即证改动没进包）。
# 随包 Skill 走整树逐字节比对已能发现「exe 是用旧源码编的」，但**两边同为旧版**时看不出来；
# 指纹钉的是「本轮那几行确实在包里那份文件里」。
# 8 篇教程的指纹各取一句**只有本篇才会有**的话（含本篇专有名词），跨篇重复的套话不算数。
SKILL_FINGERPRINTS = {
    "cat-chat-guide/SKILL.md": [
        "左下角侧栏底部",
        "设置 → Skills 管理",
        "设置 → 连接状态 → MCP 服务",
        "80-教程8-3分钟认识教程助手.md",
    ],
    "cat-chat-guide/references/00-手册要点.md": [
        "左下角侧栏底部的 **⚙ 设置",
        "设置 → Skills 管理",
        "设置 → 连接状态 → MCP 服务",
        "六个内置 Agent 的分工",
    ],
    "cat-chat-guide/references/10-教程1-3分钟第一次对话.md": [
        "顶栏那个下拉是「API」，底部的才是「模型」",
    ],
    "cat-chat-guide/references/20-教程2-3分钟用ComfyUI出图.md": [
        "切到「导演 Agent」",
        "只负责发送指令，不会自动帮你换 Agent",
    ],
    "cat-chat-guide/references/30-教程3-3分钟用RunningHub云端出图.md": [
        "RUNNINGHUB_API_KEY",
    ],
    "cat-chat-guide/references/40-教程4-3分钟出一集短剧.md": [
        "subject_definitions:",
    ],
    "cat-chat-guide/references/50-教程5-3分钟改出一条好提示词.md": [
        "主打一版 + 备选一版",
    ],
    "cat-chat-guide/references/60-教程6-3分钟让AI写个小工具.md": [
        "先给我看清单，别动文件",
    ],
    "cat-chat-guide/references/70-教程7-3分钟给CatChat装个新技能.md": [
        "Agent 是岗位，Skill 是岗位手册",
    ],
    "cat-chat-guide/references/80-教程8-3分钟认识教程助手.md": [
        "全套 8 篇速查表",
    ],
}

# 各文件本轮改动留下的「已生效」指纹（缺一即证改动没进包）。
FINGERPRINTS = {
    # 清理缓存按钮的 title 明确承诺「背景图/应用图标/在用文件会被保留」（§九.140 的告知义务）。
    # 2.9.1 另加了「顶栏说明弹层的 MCP 入口已改成带页面名的写法」这条。
    # 2.9.2 再加上「弹层里老三样那句改成『已退役』口径」。
    "index.html": [
        "set-ico",
        "聊天背景图、应用图标与历史消息里仍在使用的文件会自动保留",
        "设置 → 连接状态 → MCP 服务",
        "早期版本预置的",
    ],
    "styles.css": ["--set-r-card", "agent-card-tag", "settings-card-head"],
    # emoji 头像判定入口（内置 Agent 用 emoji，上传图走 <img>）。
    "js/01-core.js": ["agentAvatarEmoji", "agentAvatarFile"],
    "js/04-messages.js": ["currentAgentAvatarEmoji"],
    # 「内置」徽标 + 隐藏 × 删除；清理结果如实报账的 skipped_referenced。
    # 2.9.2：默认 Agent 兜底 id 由 general 改 master（老三样已退役）。
    "js/09-settings.js": ['agent-card-tag">内置', "skipped_referenced", "data.default_agent_id || 'master'"],
    "js/15-bind-events.js": ["settingsToolbarSub"],
}

# 打包时被 spec 排除的目录（private_skill_dirs / 产物），比对时同步跳过。
SKIP_PARTS = {
    "H3擦边导演_动作库增强版_v5.4",
    "minnimax-h",
    "output",
    "__pycache__",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compare_tree(packed_root: Path, source_root: Path, label: str, fails: list[str]) -> int:
    """整树逐字节比对（跳过 spec 排除项），返回比对条目数。"""
    if not source_root.is_dir():
        fails.append("%s：工作树目录不存在 %s" % (label, source_root))
        return 0
    if not packed_root.is_dir():
        fails.append("%s：包里没有这个目录 %s" % (label, packed_root))
        print("MISS  %-10s 包里缺失 %s" % (label, packed_root))
        return 0
    checked = 0
    for source in sorted(source_root.rglob("*")):
        if not source.is_file():
            continue
        rel = source.relative_to(source_root)
        if any(part in SKIP_PARTS for part in rel.parts) or source.name == "_err.txt":
            continue
        if source.suffix in {".pyc", ".pyo"}:
            continue
        packed = packed_root / rel
        checked += 1
        if not packed.is_file():
            fails.append("%s/%s：包里没有这个文件" % (label, rel))
            print("MISS  %s/%s" % (label, rel))
            continue
        pb, sb = packed.read_bytes(), source.read_bytes()
        if pb != sb:
            fails.append("%s/%s：包里那份与源码不一致（exe 是用旧源码编的）" % (label, rel))
            print("DIFF  %-40s 包 %8d / 源 %8d" % ("%s/%s" % (label, rel), len(pb), len(sb)))
    print("比对 %-10s 共 %d 个文件" % (label, checked))
    return checked


def main() -> int:
    meipass = getattr(sys, "_MEIPASS", None)
    print("frozen       :", getattr(sys, "frozen", False))
    print("_MEIPASS     :", meipass)
    if not meipass:
        print("拒绝：本探针只对冻结版有意义（源码模式下 _MEIPASS 不存在）")
        return 2

    root = Path(meipass)
    source_root = Path(__file__).resolve().parents[1]
    fails: list[str] = []

    print("=" * 72)
    print("一、本轮改动文件逐字节比对 + 指纹")
    print("=" * 72)
    packed_public = root / "public"
    src_public = source_root / "public"
    for rel in CHANGED:
        packed = packed_public / rel
        source = src_public / rel
        if not packed.is_file():
            fails.append("%s：包里没有这个文件" % rel)
            print("MISS  %-24s 包里缺失" % rel)
            continue
        if not source.is_file():
            fails.append("%s：工作树里没有这个文件（清单写错了？）" % rel)
            print("SKIP  %-24s 工作树找不到，跳过" % rel)
            continue
        pb, sb = packed.read_bytes(), source.read_bytes()
        same = pb == sb
        if not same:
            fails.append("%s：包里那份与源码不一致（exe 是用旧源码编的）" % rel)
        print("%-5s %-24s 包 %8d 字节 / 源 %8d 字节  sha %s%s" % (
            "OK" if same else "DIFF", rel, len(pb), len(sb), sha256(pb)[:12],
            "" if same else "  != " + sha256(sb)[:12],
        ))
        if same:
            text = pb.decode("utf-8", errors="replace")
            for mark in FINGERPRINTS.get(rel, []):
                if mark not in text:
                    fails.append("%s：包里那份不含指纹 %r" % (rel, mark))
                    print("      !! 缺指纹", mark)

    print("=" * 72)
    print("二、整树比对（防止清单写漏）")
    print("=" * 72)
    compare_tree(packed_public, src_public, "public", fails)
    compare_tree(root / "skills", source_root / "skills", "skills", fails)

    print("=" * 72)
    print("三、新增内置 Skill 必须在包里（并核对本轮改动的指纹）")
    print("=" * 72)
    for rel in REQUIRED_SKILLS:
        packed = root / "skills" / rel
        ok = packed.is_file() and packed.stat().st_size > 0
        if not ok:
            fails.append("skills/%s：包里缺失（出厂绑定会指向空气）" % rel)
        print("  %-4s skills/%s%s" % (
            "OK" if ok else "MISS", rel, "" if ok else "  !! 缺失"))
        if not ok:
            continue
        text = packed.read_text(encoding="utf-8", errors="replace")
        for mark in SKILL_FINGERPRINTS.get(rel, []):
            if mark not in text:
                fails.append("skills/%s：包里那份不含指纹 %r" % (rel, mark))
                print("       !! 缺指纹", mark)

    print("=" * 72)
    print("四、launcher JsApi 反射（既有对照项）")
    print("=" * 72)
    jsapi = None
    frame = sys._getframe()
    while frame is not None and jsapi is None:
        jsapi = frame.f_globals.get("JsApi")
        frame = frame.f_back
    if jsapi is None:
        print("  （跳过：沿调用栈找不到 JsApi，与本轮改动无关）")
    else:
        for name in ("naibaOpenFile", "naibaRevealInFolder", "naibaSaveFileAs"):
            ok = callable(getattr(jsapi, name, None))
            if not ok:
                fails.append("JsApi 缺少 %s" % name)
            print("  %-4s JsApi.%s" % ("OK" if ok else "MISS", name))

    print("=" * 72)
    if fails:
        print("结论：不通过，共 %d 项：" % len(fails))
        for item in fails:
            print("  ·", item)
        return 1
    print("结论：全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - 探针要把异常原样打出来
        traceback.print_exc()
        sys.exit(3)
