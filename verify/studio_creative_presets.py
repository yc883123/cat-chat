"""创作资料预设：只读取已审核的正文与逐文件处理清单。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SOURCE = "creative-prompt-kit"
COLLECTION = "创作模板资料"


def _require(ok, message):
    if not ok:
        raise SystemExit(message)


def family(build, source_dir=None):
    root = Path(source_dir) if source_dir is not None else Path(__file__).resolve().parents[1] / "presets" / SOURCE
    manifest = build.read_json(root / "source.json")
    _require(isinstance(manifest, dict) and manifest.get("collection") == COLLECTION,
             "创作模板清单格式或名称不符")
    presets = manifest.get("entries")
    sources = manifest.get("sources")
    _require(isinstance(presets, list) and presets, "创作模板清单缺少条目")
    _require(isinstance(sources, list) and len(sources) == 41, "创作模板必须逐份记录41份源文件的处理结果")
    _require({row["number"] for row in sources} == set(range(1, 42)), "创作模板源文件编号重复或缺失")
    items, bodies, audits, descriptive = [], {}, [], []
    paths = set()
    source_by_number = {row["number"]: row for row in sources}
    for preset in presets:
        item_id, relative = preset["id"], preset["path"]
        _require(relative.startswith("bodies/") and "\\" not in relative
                 and not any(part in {"", ".", ".."} for part in relative.split("/")),
                 f"创作模板路径非法：{relative}")
        _require(item_id not in bodies and relative not in paths, f"创作模板条目重复：{item_id}")
        paths.add(relative)
        path = root / relative
        _require(path.is_file() and path.resolve().is_relative_to(root.resolve()), f"创作模板正文缺失或越界：{relative}")
        raw = path.read_bytes()
        _require(len(raw) == preset["bytes"] and hashlib.sha256(raw).hexdigest() == preset["sha256"],
                 f"创作模板正文指纹不符：{relative}")
        body = raw.decode("utf-8")
        _require(body.strip() and "\r" not in body, f"创作模板正文为空或行尾不符：{item_id}")
        _require("```python" not in body, f"创作模板残留导出脚本：{item_id}")
        refs = preset.get("source_numbers") or []
        _require(refs and all(number in source_by_number for number in refs), f"创作模板来源缺失：{item_id}")
        _require(preset["module"] in build.MODULES_ALLOWED and preset["type"] in build.TYPES_ALLOWED,
                 f"创作模板分类非法：{item_id}")
        hits, handled = build.match_sensitive(body, item_id)
        reviewed = preset.get("descriptive_mentions", {})
        _require(isinstance(reviewed, dict) and all(isinstance(reason, str) and reason.strip()
                                                   for reason in reviewed.values()),
                 f"创作模板复核理由缺失：{item_id}")
        _require(not (set(hits) - set(reviewed)), f"创作模板待复核内容：{item_id}: {hits}")
        if hits or handled:
            descriptive.append({"id": item_id, "source": SOURCE, "markers": sorted(set(hits) | set(handled)),
                                "reasons": reviewed})
        item = build.new_item(
            item_id=item_id, title=preset["title"], module=preset["module"], item_type=preset["type"],
            summary=preset["summary"], tags=preset["tags"], body=body,
            source=" / ".join(source_by_number[number]["name"] for number in refs),
            genre=preset["genre"], cleaned=preset.get("transformations", []),
        )
        item["has_vars"] = bool(item["vars"])
        item["order"] = len(items) + 1
        items.append(item)
        bodies[item_id] = body
        audits.append({"id": item_id, "source_numbers": refs, "sha256": preset["sha256"],
                       "output_sha1": item["source_sha1"], "rules": preset.get("transformations", [])})
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} - {"source.json"}
    _require(actual == paths, "创作模板目录存在未登记或缺失的正文")
    _require(len(set(bodies.values())) == len(bodies), "创作模板存在完全重复正文")
    known_ids = set(bodies)
    for row in sources:
        _require(row.get("decision") in {"included", "excluded", "duplicate"} and row.get("reason"),
                 f"源文件处理结果不完整：{row['number']}")
        linked = row.get("entry_ids", [])
        _require(all(item_id in known_ids for item_id in linked), f"源文件引用未知条目：{row['number']}")
        _require(row["decision"] != "included" or bool(linked), f"已提取文件缺少对应模板：{row['number']}")
        if row["decision"] == "included":
            actual_ids = {preset["id"] for preset in presets if row["number"] in preset["source_numbers"]}
            _require(set(linked) == actual_ids, f"源文件与模板的双向关联不一致：{row['number']}")
    stats = {
        "collection": COLLECTION, "input": len(sources), "output": len(items), "expected": len(items),
        "files": len(paths), "license": manifest.get("license", ""),
        "decisions": sources, "audit": audits,
    }
    return items, bodies, [], stats, descriptive


def append(build, out_dir, source_dir=None):
    """按模块写入对应分片；全量验证完成后才改写目录，保留其他条目。"""
    items, bodies, rejects, stats, descriptive = family(build, source_dir)
    index = build.read_json(out_dir / "catalog_index.json")
    report = build.read_json(out_dir / "conversion_report.json")
    family_ids = set(bodies)
    kept = [row for row in index["items"] if row["id"] not in family_ids]
    _require(not any("-creative-" in row["id"] for row in kept), "未知创作模板ID，需先复核旧条目")
    orders = {module: max((row["order"] for row in kept if row["module"] == module), default=0)
              for module in {row["module"] for row in items}}
    for row in items:
        orders[row["module"]] += 1
        row["order"] = orders[row["module"]]
    shards = {tag: build.read_json(out_dir / f"bodies_{tag}.json") for tag in ("prompt", "script", "game", "style")}
    touched = set()
    for row in items:
        tag = build.BODIES_TAG_BY_MODULE[row["module"]]
        touched.add(tag)
        shards[tag][row["id"]] = bodies[row["id"]]
    all_items = kept + items
    all_bodies = {key: body for shard in shards.values() for key, body in shard.items()}
    problems = build.cross_check(all_items, all_bodies)
    _require(not problems, f"创作模板增补后契约不符：{problems}")
    calculated = build.build_index(all_items)
    for key in ("ok", "count", "chars", "modules", "types", "items"):
        index[key] = calculated[key]
    previous = report.get("sources", {}).get(SOURCE, {})
    index.setdefault("sources", {})[SOURCE] = stats
    report.setdefault("sources", {})[SOURCE] = stats
    counts = report.setdefault("counts", {})
    for key, field in (("expected_plan", "expected"), ("input", "input")):
        counts[key] = counts.get(key, len(kept)) - previous.get(field, 0) + stats[field]
    counts["output_index"] = len(all_items)
    report["creative_conversion"] = stats
    report["catalog_integrity"] = {"index_body_ids_equal": True, "shards": 4, "count": len(all_items)}
    report["catalog_cleanup"] = build.catalog_cleanup_report(all_items)
    report["descriptive_mentions"] = [row for row in report.get("descriptive_mentions", [])
                                      if row.get("source") != SOURCE] + descriptive
    outputs = {"catalog_index.json": index, **{f"bodies_{tag}.json": shards[tag] for tag in sorted(touched)}}
    products = report.setdefault("products", {})
    for name, payload in outputs.items():
        products[name] = {**products.get(name, {}), "items": len(all_items) if name == "catalog_index.json" else len(payload),
                          "bytes": len(json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))}
    outputs["conversion_report.json"] = report
    for name, payload in outputs.items():
        build.write_json(out_dir / name, payload)
    return index
