"""Cat Chat 工作台（Studio）预设资产转换器。

把工作台四套资产（提示词主库 87 = 启元资源 63 + 工作区增补 24、剧本 53、游戏原画 24、
风格库 397）转成统一契约的只读预设 JSON，落盘 `public/studio/`：`catalog_index.json`
（索引）+ `bodies_*.json`（按需加载正文）。同一 sources 目录下重复执行输出逐字节一致。

用法（解释器纪律见《项目维护说明（修改代码前必读）.md》）：
    .venv/Scripts/python.exe verify\\studio_catalog_build.py --sources-dir <目录> [--out-dir public\\studio]

sources 目录约定文件名（`--sources-dir` 由运行者传语义路径，本脚本不内嵌真实路径）：
- qiyuan_prompt_library_index.json / qiyuan_prompt_library_bodies.json   提示词主库 63
- chat_extra_index.json / chat_extra_bodies.json                        提示词增补 24
- qiyuan_novel_templates.json                                           剧本 53
- qiyuan_game_prompt_templates.json                                     游戏原画 24
- qiyuan_gp_style_lib.json / qiyuan_lib_style_lib.json / qiyuan_vs_style_lib.json   风格库 397
- qiyuan_novel_sensitive.json / qiyuan_game_prompt_templates.json 的敏感扫描见标记表

红线：不搬运启元 pyc/代码，仅使用允许纳入的文本/JSON 数据；NSFW 不入库；清理不静默
（拒收/ anomalies 逐条进 docs/plans/2026-10-22-工作台Studio-转换报告.md 与 conversion_report.json）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, OrderedDict
from pathlib import Path

# 模块/类型枚举与《_studio_plan_v2.md》§3 数据契约一致；越界即拒收。
MODULES_ALLOWED = ("vs", "h3", "vi", "ec", "global", "script", "game", "style")
TYPES_ALLOWED = ("template", "rule", "lexicon", "example")
MODULE_ORDER = ("vs", "h3", "vi", "ec", "global", "script", "game", "style")

SNIPPET_N = 700
BUILD_STAMP = "2026-10-22"
CATALOG_VERSION = "2.0.0"
EXPECTED_TOTAL = 561

# NSFW / 不适宜内容硬标记（入库前逐条扫描正文，命中即拒收并进报告；大小写不敏感）。
SENSITIVE_MARKERS = (
    "露点", "全裸", "做爱", "性爱", "淫水", "淫虐", "群交", "肛交", "口交", "乳交", "迷奸", "强奸",
    "嫖娼", "卖淫", "炮友",
    "nsfw", "porn", "erotic", "nude", "naked", "cumshot", "blowjob", "hardcore sex",
)

# 人工复核例外清单：id -> [(命中标记, 保留理由)]。命中清单内标记不算 NSFW，其余标记照常拒收。
# 理由随转换报告落档（清理不静默）。
SENSITIVE_EXEMPT: dict[str, dict[str, str]] = {
    "h3-001-manga4grid": {
        "群交": "「人群交谈」的子串误命中，上下文是派对会场四宫格示例，无性内容",
    },
    "vs-101-k2-engine": {
        "露点": "K2 SFW 版：正文自述「本文件为 SFW 版，不收录露骨内容」，NSFW 仅作为分级判定说明与外置扩展库登记",
        "nsfw": "K2 SFW 版：NSFW 仅是 SFW/NSFW 判定分叉的说明词，正文不含露骨内容",
    },
}


# 正文'模版变量'占位符：{{key}} 字面文本替换（不执行表达式/脚本）。
VAR_PATTERN = re.compile(r"\{\{([^{}]+)\}\}")

# 产物分片：模块 -> 正文分片文件标签（按需懒加载的最小单位）。
BODIES_TAG_BY_MODULE = {
    "vs": "prompt", "h3": "prompt", "vi": "prompt", "ec": "prompt", "global": "prompt",
    "script": "script", "game": "game", "style": "style",
}


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"源文件不可读或非法 JSON：{path.name}：{exc}")


def source_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def match_sensitive(text: str, item_id: str = "") -> tuple[list[str], dict[str, str]]:
    """返回 (待拒收标记, 保留的描述性提及{id: 理由})。"""
    low = text.lower()
    exempt = SENSITIVE_EXEMPT.get(item_id, {})
    reject: list[str] = []
    handled: dict[str, str] = {}
    for marker in SENSITIVE_MARKERS:
        if marker in low:
            if marker in exempt:
                handled[marker] = exempt[marker]
            else:
                reject.append(marker)
    return reject, handled


def vars_from_text(text: str) -> list[dict]:
    """把 {{key}} 占位符整理成变量表（key/label/default）。"""
    keys: list[str] = []
    for token in VAR_PATTERN.findall(text):
        key = token.strip()
        if key and key not in keys:
            keys.append(key)
    return [{"key": key, "label": key, "default": ""} for key in keys]


def new_item(
    *,
    item_id: str,
    title: str,
    module: str,
    item_type: str,
    summary: str,
    tags: list[str],
    body: str,
    model: list[str] | None = None,
    source: str,
    cleaned: list[str] | None = None,
    origin: str = "builtin",
    genre: str = "",
) -> dict:
    return {
        "id": item_id,
        "title": title,
        "module": module,
        "type": item_type,
        "summary": summary,
        "tags": tags,
        "chars": len(body),
        "model": [str(m) for m in (model or []) if str(m).strip()],
        "vars": vars_from_text(body),
        "has_vars": False,  # 由 convert_field_check 统一回填
        "source": source,
        "source_sha1": source_sha1(body),
        "cleaned": list(cleaned or []),
        "origin": origin,
        "order": 0,
        "genre": genre,
        "snippet": body[:SNIPPET_N],
    }


# ---- 家族 1：提示词主库 87（启元 63 + 增补 24，两份索引本身已是契约形态） ----

def prompt_family(source_dir: Path):
    items: list[dict] = []
    bodies: dict[str, str] = {}
    rejects: list[dict] = []
    descriptive: list[dict] = []
    stats: "OrderedDict[str, dict]" = OrderedDict()
    order = 1
    for pack, source_name, expect_n in (
        ("qiyuan_prompt_library", "qiyuan_assets.prompt_library", 63),
        ("chat_extra", "chat_studio_extra", 24),
    ):
        index = read_json(source_dir / f"{pack}_index.json")
        body_map = read_json(source_dir / f"{pack}_bodies.json")
        rows = list(index.get("items") or [])
        dupes: list[str] = []
        empty: list[str] = []
        sensitive: list[str] = []
        output = 0
        seen: set[str] = set()
        for raw in rows:
            iid = str(raw.get("id") or "").strip()
            if not iid:
                rejects.append({"id": "", "source": source_name, "reason": ["空ID"], "sensitive": []})
                continue
            if iid in seen:
                dupes.append(iid)
                rejects.append({"id": iid, "source": source_name, "reason": ["重复ID"], "sensitive": []})
                continue
            seen.add(iid)
            body = str(body_map.get(iid) or "")
            if not body.strip():
                empty.append(iid)
                rejects.append({"id": iid, "source": source_name, "reason": ["空正文"], "sensitive": []})
                continue
            hits, handled = match_sensitive(body, iid)
            if handled:
                descriptive.append({"id": iid, "source": source_name, "markers": sorted(handled)})
            if hits:
                sensitive.append(iid)
                rejects.append({"id": iid, "source": source_name, "reason": [], "sensitive": hits})
                continue
            item = new_item(
                item_id=iid,
                title=str(raw.get("title") or "").strip(),
                module=str(raw.get("module") or "").strip(),
                item_type=str(raw.get("type") or "").strip(),
                summary=str(raw.get("summary") or "").strip(),
                tags=[str(t) for t in (raw.get("tags") or []) if str(t).strip()],
                body=body,
                model=[str(m) for m in (raw.get("model") or []) if str(m).strip()],
                source=str(raw.get("source") or "").strip() or source_name,
                cleaned=[str(c) for c in (raw.get("cleaned") or []) if str(c).strip()],
                origin=str(raw.get("origin") or "") or "builtin",
                genre=str(raw.get("genre") or "").strip(),
            )
            item["vars"] = raw.get("vars") or []
            item["has_vars"] = bool(item["vars"])
            item["order"] = order
            order += 1
            items.append(item)
            bodies[iid] = body
            output += 1
        stats[source_name] = {
            "input": len(rows),
            "output": output,
            "dupes": len(dupes),
            "rejected": len(dupes) + len(empty) + len(sensitive),
            "expected": expect_n,
            "empty_ids": empty[:12],
            "sensitive_ids": sensitive[:12],
            "dupes_ids": dupes[:12],
        }
    return items, bodies, rejects, stats, descriptive


# ---- 家族 2：剧本 53 ----

def script_family(source_dir: Path):
    descriptive: list[dict] = []
    data = read_json(source_dir / "qiyuan_novel_templates.json")
    channels = {str(c.get("key")): c for c in (data.get("channels") or []) if c.get("key")}
    source = "qiyuan_assets.novel_templates"
    rows = list(data.get("templates") or [])
    items, bodies, rejects = [], {}, []
    dupes, seen = [], set()
    order = 1
    for raw in rows:
        orig_id = str(raw.get("id") or "").strip()
        nid = f"script-{orig_id}" if orig_id else ""
        if not nid:
            rejects.append({"id": "", "source": source, "reason": ["空ID"], "sensitive": []})
            continue
        if nid in seen:
            dupes.append(nid)
            rejects.append({"id": nid, "source": source, "reason": ["重复ID"], "sensitive": []})
            continue
        seen.add(nid)
        body = str(raw.get("prompt") or "")
        if not body.strip():
            rejects.append({"id": nid, "source": source, "reason": ["空正文"], "sensitive": []})
            continue
        hits, handled = match_sensitive(body, nid)
        descriptive.extend({"id": nid, "source": source, "markers": sorted(handled)} for _ in [0] if handled)
        if hits:
            rejects.append({"id": nid, "source": source, "reason": [], "sensitive": hits})
            continue
        ch = channels.get(str(raw.get("gender") or "")) or {}
        genre_parts = [part for part in (str(raw.get("sub") or ""), str(ch.get("name") or "")) if part]
        item = new_item(
            item_id=nid,
            title=str(raw.get("name") or "").strip(),
            module="script",
            item_type="template",
            summary=str(raw.get("desc") or "").strip()[:200],
            tags=[str(t) for t in (raw.get("tags") or []) if str(t).strip()],
            body=body,
            source=source,
            genre=" / ".join(genre_parts),
        )
        item["order"] = order
        order += 1
        items.append(item)
        bodies[nid] = body
    stats = {
        "input": len(rows),
        "output": len(items),
        "dupes": len(dupes),
        "rejected": len(rejects),
        "dupes_ids": dupes[:12],
    }
    return items, bodies, rejects, stats, descriptive


# ---- 家族 3：游戏原画 24 ----

def game_family(source_dir: Path):
    descriptive: list[dict] = []
    data = read_json(source_dir / "qiyuan_game_prompt_templates.json")
    cat_names = {str(c.get("id")): str(c.get("name") or "") for c in (data.get("categories") or []) if c.get("id")}
    source = "qiyuan_assets.game_prompt_templates"
    rows = list(data.get("items") or [])
    items, bodies, rejects = [], {}, []
    dupes, seen = [], set()
    game_placeholders: list[str] = []
    order = 1
    for raw in sorted(rows, key=lambda r: int(r.get("order") or 0)):
        orig_id = str(raw.get("id") or "").strip()
        gid = f"game-{orig_id}" if orig_id else ""
        if not gid:
            rejects.append({"id": "", "source": source, "reason": ["空ID"], "sensitive": []})
            continue
        if gid in seen:
            dupes.append(gid)
            rejects.append({"id": gid, "source": source, "reason": ["重复ID"], "sensitive": []})
            continue
        seen.add(gid)
        system_text = str(raw.get("system_prompt") or "").strip()
        template_text = str(raw.get("prompt_template") or "").strip()
        parts = []
        if system_text:
            parts.append(f"## 角色设定\n{system_text}")
        if template_text:
            parts.append(f"## 任务模板\n{template_text}")
        body = "\n\n".join(parts)
        placeholders = vars_from_text(template_text)
        game_placeholders.extend(v["key"] for v in placeholders)
        if not body.strip():
            rejects.append({"id": gid, "source": source, "reason": ["空正文"], "sensitive": []})
            continue
        hits, handled = match_sensitive(body, gid)
        if handled:
            descriptive.append({"id": gid, "source": source, "markers": sorted(handled)})
        if hits:
            rejects.append({"id": gid, "source": source, "reason": [], "sensitive": hits})
            continue
        sub_purpose = str(raw.get("sub_purpose") or "").strip()
        item = new_item(
            item_id=gid,
            title=str(raw.get("name") or "").strip(),
            module="game",
            item_type="template",
            summary=str(raw.get("description") or "").strip()[:240],
            tags=[str(t) for t in (raw.get("tags") or []) if str(t).strip()],
            body=body,
            source=source,
            genre=cat_names.get(str(raw.get("category") or ""), "") or sub_purpose,
        )
        item["vars"] = placeholders
        item["has_vars"] = bool(placeholders)
        item["order"] = order
        order += 1
        items.append(item)
        bodies[gid] = body
    stats = {
        "input": len(rows),
        "output": len(items),
        "dupes": len(dupes),
        "rejected": len(rejects),
        "dupes_ids": dupes[:12],
    }
    return items, bodies, rejects, stats, game_placeholders, descriptive


# ---- 家族 4：风格库 397 ----

STYLE_LIBS = (
    ("qiyuan_gp_style_lib.json", "gp"),
    ("qiyuan_lib_style_lib.json", "lb"),
    ("qiyuan_vs_style_lib.json", "vs"),
)


def style_family(source_dir: Path):
    source = "qiyuan_assets.style_libs"
    descriptive: list[dict] = []
    items, bodies, rejects = [], {}, []
    dupes, seen, empty = [], set(), []
    order = 1
    total_input = 0
    per_lib: dict[str, dict] = {}
    for fname, tag in STYLE_LIBS:
        rows = list(read_json(source_dir / fname))
        total_input += len(rows)
        per_lib[tag] = {"input": len(rows), "output": 0}
        for idx, raw in enumerate(rows, 1):
            sid = f"style-{tag}-{idx:03d}"
            if sid in seen:
                dupes.append(sid)
                rejects.append({"id": sid, "source": source, "reason": ["重复ID"], "sensitive": []})
                continue
            seen.add(sid)
            body = str(raw.get("prompt") or "")
            title = str(raw.get("cn") or "").strip()
            en = str(raw.get("en") or "").strip()
            cat = str(raw.get("cat") or "").strip()
            if not body.strip():
                empty.append(sid)
                rejects.append({"id": sid, "source": source, "reason": ["空正文"], "sensitive": []})
                per_lib[tag]["output"] = per_lib[tag]["output"]  # 保持计数不动
                continue
            hits, handled = match_sensitive(body or "", sid)
            if handled:
                descriptive.append({"id": sid, "source": source, "markers": sorted(handled)})
            if hits:
                rejects.append({"id": sid, "source": source, "reason": [], "sensitive": hits})
                continue
            item = new_item(
                item_id=sid,
                title=title,
                module="style",
                item_type="lexicon",
                summary=en,
                tags=[t for t in (cat, en) if t],
                body=body,
                source=source,
                genre=cat,
            )
            item["order"] = order
            order += 1
            items.append(item)
            bodies[sid] = body
            per_lib[tag]["output"] += 1
    stats = {
        "input": total_input,
        "output": len(items),
        "dupes": len(dupes),
        "rejected": len(rejects),
        "per_lib": per_lib,
        "empty_ids": empty[:12],
    }
    return items, bodies, rejects, stats, descriptive


# ---- 校验与落盘 ----

def cross_check(items: list[dict], bodies: dict[str, str]) -> dict[str, list[str]]:
    """契约一致性（ID 唯一 / 正文齐全 / 片段与字符数一致 / 枚举 / has_vars 一致）。"""
    problems: dict[str, list[str]] = {}
    dupes = [iid for iid, n in Counter(it["id"] for it in items).items() if n > 1]
    if dupes:
        problems["dupe_ids"] = sorted(dupes)
    missing = [it["id"] for it in all_iter(items) if not bodies.get(it, "").strip()]
    if missing:
        problems["missing_bodies"] = sorted(missing)
    bad_module = [it["id"] for it in items if it["module"] not in MODULES_ALLOWED]
    bad_type = [it["id"] for it in items if it["type"] not in TYPES_ALLOWED]
    if bad_module:
        problems["bad_module"] = sorted(bad_module)
    if bad_type:
        problems["bad_type"] = sorted(bad_type)
    snippet_bad = [it["id"] for it in items if bodies.get(it["id"]) and bodies[it["id"]][:SNIPPET_N] != it["snippet"]]
    if snippet_bad:
        problems["snippet_mismatch"] = sorted(snippet_bad)
    chars_bad = [it["id"] for it in items if len(bodies.get(it["id"] or "")) != it["chars"]]
    if chars_bad:
        problems["chars_mismatch"] = sorted(chars_bad)
    vars_bad = [it["id"] for it in items if it["has_vars"] != bool(it["vars"])]
    if vars_bad:
        problems["vars_mismatch"] = sorted(vars_bad)
    empty_title = [it["id"] for it in items if not it["title"].strip()]
    if empty_title:
        problems["empty_title"] = sorted(empty_title)
    return problems


def all_iter(items: list[dict]):
    for it in items:
        yield it["id"]


def build_index(items: list[dict]) -> dict:
    modules = Counter(it["module"] for it in items)
    types = Counter(it["type"] for it in items)
    return {
        "ok": True,
        "version": CATALOG_VERSION,
        "built": BUILD_STAMP,
        "count": len(items),
        "chars": sum(int(it["chars"]) for it in items),
        "snippet_n": SNIPPET_N,
        "modules": {m: modules.get(m, 0) for m in MODULE_ORDER if m in modules},
        "types": {t: types.get(t, 0) for t in TYPES_ALLOWED if t in types},
        "items": items,
    }


def write_json(path: Path, payload: object) -> int:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="")
    return path.stat().st_size


def main() -> None:
    parser = argparse.ArgumentParser(description="工作台预设资产转换器")
    parser.add_argument("--sources-dir", required=True)
    parser.add_argument("--out-dir", default="public/studio")
    parser.add_argument("--report-md", default="docs/plans/2026-10-22-工作台Studio-转换报告.md")
    args = parser.parse_args()

    here = Path(__file__).resolve().parents[1]
    source_dir = Path(args.sources_dir)
    if not source_dir.is_absolute():
        source_dir = here / source_dir
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = here / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    prompt_items, prompt_bodies, prompt_rejects, prompt_stats, prompt_desc = prompt_family(source_dir)
    script_items, script_bodies, script_rejects, script_stats, script_desc = script_family(source_dir)
    game_items, game_bodies, game_rejects, game_stats, game_placeholders, game_desc = game_family(source_dir)
    style_items, style_bodies, style_rejects, style_stats, style_desc = style_family(source_dir)

    items = prompt_items + script_items + game_items + style_items
    bodies = {**prompt_bodies, **script_bodies, **game_bodies, **style_bodies}
    rejects = prompt_rejects + script_rejects + game_rejects + style_rejects

    problems = cross_check(items, bodies)
    if problems:
        raise SystemExit(f"契约校验失败：{json.dumps(problems, ensure_ascii=False)}")

    index = build_index(items)
    descriptive_mentions = prompt_desc + script_desc + game_desc + style_desc
    index["sources"] = {
        "qiyuan_assets.prompt_library": omit_key(prompt_stats["qiyuan_assets.prompt_library"], "source"),
        "chat_studio_extra": omit_key(prompt_stats["chat_studio_extra"], "source"),
        "qiyuan_assets.novel_templates": script_stats,
        "qiyuan_assets.game_prompt_templates": game_stats,
        "qiyuan_assets.style_libs": style_stats,
    }

    files = OrderedDict()
    files["catalog_index.json"] = {"items": len(items), "bytes": write_json(out_dir / "catalog_index.json", index)}
    for tag in ("prompt", "script", "game", "style"):
        part = {it["id"]: bodies[it["id"]] for it in items if BODIES_TAG_BY_MODULE[it["module"]] == tag}
        name = f"bodies_{tag}.json"
        files[name] = {"items": len(part), "bytes": write_json(out_dir / name, part)}

    input_total = sum(
        f"{pack}_index.json" and read_json(source_dir / f"{pack}_index.json").get("count") or (len(read_json(source_dir / f"{pack}_index.json").get("items") or []))
        for pack in ("qiyuan_prompt_library", "chat_extra")
    )
    input_total += script_stats["input"] + game_stats["input"] + style_stats["input"]
    sensitive_rejects = [r for r in rejects if r.get("sensitive")]
    empty_rejects = [r for r in rejects if "空正文" in (r.get("reason") or [])]
    dupe_rejects = [r for r in rejects if "重复ID" in (r.get("reason") or [])]

    report = OrderedDict([
        ("generated_at", BUILD_STAMP),
        ("counts", {
            "expected_plan": EXPECTED_TOTAL,
            "input": input_total,
            "output_index": len(items),
            "rejected": len(rejects),
            "sensitive": len(sensitive_rejects),
            "empty": len(empty_rejects),
            "dupes": len(dupe_rejects),
        }),
        ("sources", index["sources"]),
        ("descriptive_mentions", [
            {"id": row["id"], "source": row["source"], "markers": row["markers"]}
            for row in descriptive_mentions
        ]),
        ("products", {name: meta for name, meta in files.items()}),
        ("anomalies", {
            "note": "主库 4 条（vs-008-qwen50 / vs-027-xh2 / vs-029-guofeng-coll1 / vs-037-daily0621）"
                    "原型截段 chars 与启元清洗后正文不一致，以启元清洗后正文与 chars 为准（cleaned 清单在条目里）",
            "game_vars_placeholder_keys": sorted(set(game_placeholders))[:24],
        }),
        ("rejects", [
            {
                "id": r.get("id") or "",
                "source": r.get("source") or "",
                "reason": r.get("reason") or [],
                "sensitive": r.get("sensitive") or [],
            }
            for r in rejects
        ]),
    ])
    write_json(out_dir / "conversion_report.json", report)

    lines = [
        "# Cat Chat 工作台（Studio）预设资产转换报告",
        "",
        f"> 本文件由 `verify\\\\studio_catalog_build.py` 自动生成（锚定 {BUILD_STAMP}）；数字一律以命令执行结果为准。",
        "",
        "## 汇总",
        "",
        f"- 计划预期 {EXPECTED_TOTAL}；转换输入 {input_total}；索引条目 {len(items)}；"
        f"拒收 {len(rejects)}（敏感 {len(sensitive_rejects)}、空正文 {len(empty_rejects)}、重复 {len(dupe_rejects)}）",
        f"- 最终可检索数（catalog count）：{index['count']}",
        "",
        "## 各来源",
        "",
    ]
    for name, stats in index["sources"].items():
        lines.append(
            f"- {name}：输入 {stats.get('input', 0)}，输出 {stats.get('output', 0)}，"
            f"拒收 {stats.get('rejected', 0)}，重复 {stats.get('dupes', 0)}"
            + (f"，敏感样例 {', '.join(stats['sensitive_ids'])}" if stats.get("sensitive_ids") else "")
            + (f"，空正文样例 {', '.join(stats['empty_ids'])}" if stats.get("empty_ids") else "")
        )
    lines += ["", "## 产物", ""]
    lines += [f"- `{name}`：{meta['items']} 条 / {meta['bytes']} 字节" for name, meta in files.items()]
    lines += [
        "",
        "## 异常与清理说明",
        "",
        f"- {report['anomalies']['note']}",
        *(
            f"- 描述性提及（人工复核保留）：`{row['id']}` {row['source']} 命中 {'、'.join(row['markers'])}"
            for row in descriptive_mentions
        ),
        "- chars/snippet/has_vars/枚举/ID 唯一性等契约一致性由转换器 cross_check 把关，任一不连贯直接失败。",
        "- 模块->正文分片：vs/h3/vi/ec/global->bodies_prompt.json；script->bodies_script.json；"
        "game->bodies_game.json；style->bodies_style.json（按需懒加载）。",
    ]
    md_path = Path(args.report_md)
    if not md_path.is_absolute():
        md_path = here / md_path
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")

    print(f"index {index['count']} 条 -> {out_dir / 'catalog_index.json'}")
    for name, meta in files.items():
        print(f"  {name}: {meta['items']} 条 / {meta['bytes']} 字节")
    print(f"rejected {len(rejects)}（sensitive={len(sensitive_rejects)}, empty={len(empty_rejects)}, dupes={len(dupe_rejects)}）")
    print(f"report -> {md_path}")


def omit_key(d: dict, key: str) -> dict:
    return {k: v for k, v in d.items() if k != key}


if __name__ == "__main__":
    main()
