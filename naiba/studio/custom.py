"""自定义库（Studio）：data_dir/studio/custom/<id>.json，每条一个文件。

- ID 由服务端生成（路径安全的 `custom-<时间戳>-<随机>`），禁止用户文件名穿越目录。
- 写入全部走「同目录临时文件 + os.replace 原子替换」；解析失败不覆盖已有记录。
- 支持 .txt / .md（正文导入，标题取文件名）与约定结构 .json 导入；导出=返回该条 JSON。
- 自定义条目不进入内置目录（origin='custom'，目录/正文接口不收）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

MODULES_ALLOWED = frozenset({"vs", "h3", "vi", "ec", "global", "script", "game", "style"})
TYPES_ALLOWED = frozenset({"template", "rule", "lexicon", "example"})
TITLE_MAX = 200
BODY_MAX = 400_000
IMPORT_EXT = (".txt", ".md", ".json")

CUSTOM_ID_PATTERN = re.compile(r"^custom-[0-9]{14}-[0-9a-f]{12}$")


class CustomError(ValueError):
    """自定义条目非法或操作越界。"""


def _stat(item_id: str, body: str, origin: str = "custom") -> dict[str, Any]:
    """目录条目形态（与前述内置条目同字段集；自定义多一个 body 字段以便直接编辑/导出）。"""
    from naiba.studio.catalog import SNIPPET_N

    body = str(body)
    return {
        "id": item_id,
        "title": "",
        "module": "global",
        "type": "template",
        "summary": "",
        "tags": [],
        "chars": len(body),
        "model": [],
        "vars": [],
        "has_vars": False,
        "source": "自定义条目",
        "source_sha1": "",
        "cleaned": [],
        "origin": origin,
        "order": 0,
        "genre": "",
        "snippet": body[:SNIPPET_N],
        "body": body,
    }


class CustomStore:
    """自定义条目库（线程锁串行化写路径；读为每条独立小文件，损坏条目不拖累他人）。"""

    def __init__(self, data_dir: Path):
        self._dir = Path(data_dir) / "studio" / "custom"
        self._lock = threading.Lock()

    # ---- 路径与安全 ----

    def _safe_path(self, item_id: str) -> Path:
        iid = str(item_id or "").strip()
        if not CUSTOM_ID_PATTERN.match(iid):
            raise CustomError("条目 ID 非法")
        path = (self._dir / f"{iid}.json").resolve()
        if not path.is_relative_to(self._dir.resolve()):
            raise CustomError("条目路径越界")
        return path

    def _new_id(self) -> str:
        return f"custom-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:12]}"

    # ---- 基础读写 ----

    @staticmethod
    def _validate_common(record: dict[str, Any]) -> None:
        title = str(record.get("title") or "").strip()
        if not title:
            raise CustomError("标题不能为空")
        if len(title) > TITLE_MAX:
            raise CustomError(f"标题不能超过 {TITLE_MAX} 字符")
        body = record.get("body")
        if not isinstance(body, str) or not body.strip():
            raise CustomError("正文不能为空")
        if len(body) > BODY_MAX:
            raise CustomError(f"正文过长（>{BODY_MAX} 字符）")
        if record.get("module") not in MODULES_ALLOWED:
            raise CustomError(f"模块越界：{record.get('module')}")
        if record.get("type") not in TYPES_ALLOWED:
            raise CustomError(f"类型越界：{record.get('type')}")
        tags = record.get("tags")
        if not isinstance(tags, list) or any(not str(t).strip() for t in tags):
            raise CustomError("tags 必须是字符串数组")
        summary = record.get("summary")
        if not isinstance(summary, str):
            raise CustomError("summary 必须是文本")
        genre = record.get("genre")
        if not isinstance(genre, str):
            raise CustomError("genre 必须是文本")

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        """同目录临时文件 + 原子替换；失败不覆盖已有内容。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp_path, path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ---- 对外 ----

    def list(self) -> dict[str, Any]:
        """列表（按更新时间倒序）。损坏文件不算记录、只报文件名，不影响其他条目。"""
        corrupt: list[str] = []
        items: list[dict[str, Any]] = []
        if self._dir.exists():
            for path in sorted(self._dir.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    corrupt.append(path.name)
                    continue
                if not isinstance(payload, dict) or not CUSTOM_ID_PATTERN.match(str(payload.get("id") or "")):
                    corrupt.append(path.name)
                    continue
                items.append(self._public_view(payload))
        items.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        return {"items": items, "corrupt": corrupt}

    def get(self, item_id: str) -> dict[str, Any]:
        path = self._safe_path(item_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise LookupError("自定义条目不存在") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
            raise CustomError("自定义条目文件损坏") from exc
        if not isinstance(payload, dict):
            raise CustomError("自定义条目文件损坏")
        return self._public_view(payload)

    def create(self, body_text: str, *, title: str = "", module: str = "global",
               item_type: str = "template", tags: list[str] | None = None,
               summary: str = "", genre: str = "") -> dict[str, Any]:
        record = _stat("", body_text)
        record.update({
            "title": str(title or "").strip(),
            "module": module,
            "type": item_type,
            "tags": [str(t) for t in (tags or []) if str(t).strip()],
            "summary": str(summary or ""),
            "genre": str(genre or ""),
        })
        self._validate_common(record)
        with self._lock:
            record["id"] = self._new_id()
            record["created_at"] = record["updated_at"] = int(time.time() * 1000)
            self._write_json(self._dir / f"{record['id']}.json", record)
        return self._public_view(record)

    def update(self, item_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = self.get(item_id)
        with self._lock:
            record = {**current}
            for field in ("title", "module", "type", "tags", "summary", "genre", "body"):
                if field in body and body[field] is not None:
                    record[field] = body[field]
            record["chars"] = len(str(record.get("body") or ""))
            record["updated_at"] = int(time.time() * 1000)
            self._validate_common(record)
            self._write_json(self._dir / f"{item_id}.json", record)
        return self._public_view(record)

    def delete(self, item_id: str) -> bool:
        path = self._safe_path(item_id)
        with self._lock:
            if not path.exists():
                raise LookupError("自定义条目不存在")
            try:
                path.unlink()
            except OSError as exc:
                raise CustomError(f"删除失败：{exc}") from exc
        return True

    def export(self, item_id: str) -> dict[str, Any]:
        return self.get(item_id)

    def import_body(self, filename: str, content: str) -> dict[str, Any]:
        """txt/md/json 导入：txt/md → 正文；json → 校验结构后整条导入。"""
        suffix = Path(str(filename or "").strip()).suffix.lower()
        if suffix not in IMPORT_EXT:
            raise CustomError("只支持 .txt / .md / .json 导入")
        if suffix == ".json":
            try:
                payload = json.loads(content)
            except json.JSONDecodeError as exc:
                raise CustomError("JSON 解析失败，未导入") from exc
            if not isinstance(payload, dict):
                raise CustomError("JSON 结构必须是对象，未导入")
            body_text = payload.get("body")
            if not isinstance(body_text, str) or not body_text.strip():
                raise CustomError("JSON 缺 body 字段，未导入")
            return self.create(
                body_text,
                title=str(payload.get("title") or "").strip(),
                module=str(payload.get("module") or "global"),
                item_type=str(payload.get("type") or "template"),
                tags=[str(t) for t in (payload.get("tags") or []) if str(t).strip()],
                summary=str(payload.get("summary") or ""),
                genre=str(payload.get("genre") or ""),
            )
        stem = Path(str(filename)).stem.strip() or "导入条目"
        return self.create(content, title=stem[:TITLE_MAX])

    # ---- 展示 ----

    @staticmethod
    def _public_view(record: dict[str, Any]) -> dict[str, Any]:
        row = dict(_stat(str(record.get("id") or ""), record.get("body") if isinstance(record.get("body"), str) else ""))
        row.update({
            "title": str(record.get("title") or ""),
            "module": record.get("module") or "global",
            "type": record.get("type") or "template",
            "tags": [str(t) for t in (record.get("tags") or []) if str(t).strip()],
            "summary": str(record.get("summary") or ""),
            "genre": str(record.get("genre") or ""),
            "created_at": int(record.get("created_at") or 0),
            "updated_at": int(record.get("updated_at") or 0),
            "body": record.get("body") if isinstance(record.get("body"), str) else "",
        })
        row["chars"] = len(row["body"])
        return row
