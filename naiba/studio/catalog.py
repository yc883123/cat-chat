"""只读预设目录（Studio）：索引校验、筛选、搜索、按 ID 取正文。

预设资产是随应用打包的只读 JSON（`public/studio/`，开发模式与冻结模式共用同一
`paths.public_dir`）：`catalog_index.json` 是索引（含条目元数据），正文按模块分片
（bodies_prompt/script/game/style.json）按需加载，不预读全部长正文。内置资源不可经
API 修改——本模块不提供任何写路径。

字段契约见 `verify/studio_catalog_build.py` 顶部与 docs/plans/2026-10-22-工作台Studio-转换报告.md。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 模块/类型枚举（与转换器/契约一致；越界数据在 catalog 加载时直接判非法）。
MODULES_ALLOWED = frozenset({"vs", "h3", "vi", "ec", "global", "script", "game", "style"})
TYPES_ALLOWED = frozenset({"template", "rule", "lexicon", "example"})
SNIPPET_N = 700

_INDEX_NAME = "catalog_index.json"
_BODIES_NAME = "bodies_{}.json"
# 模块 -> 正文分片标签（与转换器 BODIES_TAG_BY_MODULE 同口径；用规则而非复制，避免漂移）。
_INDEX_KEYS_REQUIRED = ("ok", "version", "built", "count", "snippet_n", "items")
_ITEM_KEYS_REQUIRED = (
    "id", "title", "module", "type", "summary", "tags", "chars", "vars", "has_vars",
    "source", "source_sha1", "cleaned", "origin", "order", "genre", "snippet",
)


class CatalogError(ValueError):
    """目录数据非法（结构/枚举/计数不一致）或请求越界。"""


def bodies_tag_for_module(module: str) -> str:
    """模块 -> 正文分片文件标签。script/game/style 独占分片，其余并入 prompt 分片。"""
    if module == "script":
        return "script"
    if module == "game":
        return "game"
    if module == "style":
        return "style"
    return "prompt"


class StudioCatalog:
    """只读预设目录。构建一次索引缓存（轻量元数据），正文按需从分片文件取。"""

    def __init__(self, public_dir: Path):
        self._public_dir = Path(public_dir)
        self._index: dict[str, Any] | None = None
        self._items_by_id: dict[str, dict[str, Any]] | None = None
        self._bodies_cache: dict[str, dict[str, str]] = {}

    # ---- 加载与校验 ----

    def _load_index(self) -> dict[str, Any]:
        if self._index is not None:
            return self._index
        path = self._public_dir / _INDEX_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CatalogError(f"工作台目录不可读：{exc}") from exc
        if not isinstance(data, dict):
            raise CatalogError("工作台目录结构非法（顶层必须为对象）")
        missing = [key for key in _INDEX_KEYS_REQUIRED if key not in data]
        if missing:
            raise CatalogError(f"工作台目录缺字段：{missing}")
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise CatalogError("工作台目录条目非法（items 必须为非空数组）")
        items_by_id: dict[str, dict[str, Any]] = {}
        for row in items:
            if not isinstance(row, dict):
                raise CatalogError("工作台目录条目非法（须为对象）")
            miss = [key for key in _ITEM_KEYS_REQUIRED if key not in row]
            if miss:
                raise CatalogError(f"条目 {row.get('id') or '?'} 缺字段：{miss}")
            if str(row["module"]) not in MODULES_ALLOWED:
                raise CatalogError(f"条目 {row['id']} 模块越界：{row['module']}")
            if str(row["type"]) not in TYPES_ALLOWED:
                raise CatalogError(f"条目 {row['id']} 类型越界：{row['type']}")
            if int(row["chars"]) != len(str(row["snippet"])) and int(row["chars"]) <= SNIPPET_N:
                # 短正文：chars 应等于整段正文长度，snippet 是全部内容。
                # 长正文（chars>700）无法仅凭 snippet 断言，交给 bodies 加载点核验。
                raise CatalogError(f"条目 {row['id']} chars 与片段不一致")
            if bool(row["has_vars"]) != bool(row.get("vars")):
                raise CatalogError(f"条目 {row['id']} has_vars 与变量表不一致")
            if row["id"] in items_by_id:
                raise CatalogError(f"条目 ID 重复：{row['id']}")
            items_by_id[str(row["id"])] = row
        names = data["modules"] if isinstance(data.get("modules"), dict) else {}
        if sum(int(v) for v in names.values()) != int(data["count"]):
            raise CatalogError("工作台目录 modules 计数与 count 不一致")
        self._index = data
        self._items_by_id = items_by_id
        return self._index

    # ---- 查询面 ----

    def summary(self) -> dict[str, Any]:
        """目录摘要（不含 items 明细的轻量头部 + 计数）。"""
        data = self._load_index()
        return {k: v for k, v in data.items() if k not in ("items",)}

    def items(
        self,
        *,
        module: str = "",
        item_type: str = "",
        query: str = "",
        has_vars: bool | None = None,
    ) -> list[dict[str, Any]]:
        """条目元数据筛选。query 在 title/summary/tags/genre/source 上做子串匹配。"""
        self._load_index()
        rows = list(self._index["items"])
        if module:
            if module not in MODULES_ALLOWED:
                raise CatalogError(f"未知模块：{module}")
            rows = [row for row in rows if row["module"] == module]
        if item_type:
            if item_type not in TYPES_ALLOWED:
                raise CatalogError(f"未知类型：{item_type}")
            rows = [row for row in rows if row["type"] == item_type]
        if has_vars is not None:
            rows = [row for row in rows if bool(row.get("has_vars")) == bool(has_vars)]
        needle = re.sub(r"\s+", "", str(query or "")).strip().lower()
        if needle:
            def match(row: dict[str, Any]) -> bool:
                haystack = " ".join(
                    row.get(field, "") if isinstance(row.get(field), str) else " ".join(str(t) for t in row.get(field) or [])
                    for field in ("title", "summary", "genre", "source", "tags")
                ).lower()
                return needle in re.sub(r"\s+", "", haystack)
            rows = [row for row in rows if match(row)]
        return rows

    def item(self, item_id: str) -> dict[str, Any]:
        """单条元数据。"""
        self._load_index()
        row = self._items_by_id.get(str(item_id or "").strip())
        if not row:
            raise LookupError(f"工作台条目不存在：{item_id}")
        if row["origin"] == "custom":
            raise LookupError("内置目录不含自定义条目，请走自定义库接口")
        return dict(row)

    def get_body(self, item_id: str) -> str:
        """按 ID 取正文（从对应分片懒加载，调用点核验 chars/snippet 契约）。"""
        row = self.item(item_id)
        target_id = str(row["id"])
        expected_chars = int(row["chars"])
        expected_snippet = str(row["snippet"])
        tag = bodies_tag_for_module(str(row["module"]))
        part = self._bodies_cache.get(tag)
        if part is None:
            part_path = self._public_dir / _BODIES_NAME.format(tag)
            try:
                part = json.loads(part_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CatalogError(f"工作台正文分片不可读：{part_path.name}：{exc}") from exc
            if not isinstance(part, dict):
                raise CatalogError(f"工作台正文分片非法：{part_path.name}")
            self._bodies_cache[tag] = part
        body = part.get(target_id)
        if not isinstance(body, str) or not body:
            raise LookupError(f"工作台正文缺失：{target_id}")
        if len(body) != expected_chars:
            raise CatalogError(f"正文长度与索引不一致：{target_id}")
        if body[:SNIPPET_N] != expected_snippet:
            raise CatalogError(f"正文片段与索引不一致：{target_id}")
        return body

    def preload_catalog_bodies(self) -> None:
        """惰性触发全部分片加载（不用于首屏；测试/导出类低频动作用）。"""
        self._load_index()
        for module in MODULES_ALLOWED:
            tag = bodies_tag_for_module(module)
            if tag not in self._bodies_cache:
                self.get_body(next(it for it in self._items_by_id.values() if bodies_tag_for_module(it["module"]) == tag)["id"])
