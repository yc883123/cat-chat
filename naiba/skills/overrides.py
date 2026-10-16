"""Skill 用户覆盖层：按 skill_id 存 {alias, note}，落盘到数据目录 skill-overrides.json。

设计约束（Skills 管理页改版计划 §1.1 / §4）：
- **id 稳定**：alias/note 任何改动不得改变 scan 的 id/ref，不动目录名与 frontmatter；
- **前缀缓存**：alias/note 不进注入 prompt（``skills/agent.py`` 的 ``<skill name=…>`` 块
  仍用 scan 原始 name，字节不变），覆盖层只服务 UI 展示与搜索；
- 内置 Skill 由 ``_sync_bundled_skills`` 托管、不可写文件，别名/备注只能走本覆盖层。

存储形态：数据目录下独立 ``skill-overrides.json``（``{skill_id: {alias, note}}``），
不进 ``config.json``（避免与设置迁移/公开设置出网口径纠缠）。空串视为清除该字段，
两者皆空则整条删除；``prune_missing`` 在扫描后清理已卸载 Skill 的残留条目。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("naiba.skill_runtime")

ALIAS_MAX_CHARS = 40
NOTE_MAX_CHARS = 500

OVERRIDES_FILENAME = "skill-overrides.json"


def normalize_alias(raw: Any) -> str:
    """规范化别名：去掉首尾空白与内部换行；超限交给调用方校验。"""
    text = str(raw or "").strip()
    return re.sub(r"\s+", " ", text) if text else ""


def validate_alias(raw: Any) -> str:
    """校验并返回合法别名；非法输入抛 ValueError（API 层转 400）。"""
    text = normalize_alias(raw)
    if not text:
        return ""
    if len(text) > ALIAS_MAX_CHARS:
        raise ValueError(f"别名不能超过 {ALIAS_MAX_CHARS} 个字符")
    if "/" in text:
        raise ValueError("别名不能包含 / 字符")
    return text


def validate_note(raw: Any) -> str:
    """校验并返回合法备注；空串表示清除。"""
    text = str(raw or "").replace("\r\n", "\n").strip()
    if len(text) > NOTE_MAX_CHARS:
        raise ValueError(f"备注不能超过 {NOTE_MAX_CHARS} 个字符")
    return text


class SkillOverrides:
    """按 skill_id 持久化 {alias, note}。线程安全；进程内单实例由装配根持有。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, ValueError) as exc:
            logger.warning("读取 Skill 覆盖层失败（忽略，按空处理）：%s", exc)
            return
        if not isinstance(raw, dict):
            return
        data: dict[str, dict[str, str]] = {}
        for key, value in raw.items():
            skill_id = str(key or "").strip()
            if not skill_id or not isinstance(value, dict):
                continue
            alias = str(value.get("alias") or "").strip()
            note = str(value.get("note") or "").strip()
            if alias or note:
                data[skill_id] = {"alias": alias, "note": note}
        self._data = data

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            # 覆盖层是 UI 附属信息：写失败不让设置接口 500，但必须留下可见痕迹。
            logger.warning("保存 Skill 覆盖层失败：%s", exc)

    # ---- 读写 ----

    def get_all(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {skill_id: dict(entry) for skill_id, entry in self._data.items()}

    def set_override(self, skill_id: str, alias: Any = None, note: Any = None) -> dict[str, str]:
        """设置/清除一条覆盖；alias/note 传 None 表示"不动该字段"。

        返回该 skill_id 的最终覆盖条目；两者皆空时整条删除并返回空 dict。
        """
        skill_id = str(skill_id or "").strip()
        if not skill_id:
            raise ValueError("skill_id 不能为空")
        entry: dict[str, str] = {}
        with self._lock:
            existing = dict(self._data.get(skill_id) or {})
            if alias is not None:
                entry["alias"] = validate_alias(alias)
            if note is not None:
                entry["note"] = validate_note(note)
            merged = {**existing, **entry}
            merged = {key: value for key, value in merged.items() if value}
            if merged:
                self._data[skill_id] = merged
            else:
                self._data.pop(skill_id, None)
            self._save()
            return dict(self._data.get(skill_id) or {})

    def remove(self, skill_id: str) -> bool:
        """删除单条覆盖（彻底删除 Skill 后调用）；返回是否真的删了。"""
        skill_id = str(skill_id or "").strip()
        with self._lock:
            if skill_id not in self._data:
                return False
            del self._data[skill_id]
            self._save()
            return True

    def prune_missing(self, known_ids: set[str] | list[str] | tuple[str, ...]) -> list[str]:
        """删除不在 known_ids 里的残留条目（卸载/删除 Skill 后调用），返回被删的 id。"""
        known = {str(item) for item in (known_ids or [])}
        with self._lock:
            stale = [skill_id for skill_id in self._data if skill_id not in known]
            if not stale:
                return []
            for skill_id in stale:
                del self._data[skill_id]
            self._save()
            return stale

    def apply_to_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把覆盖层合并进 scan 条目副本：新增 alias/note 字段（无则空串）。"""
        with self._lock:
            overrides = {skill_id: dict(entry) for skill_id, entry in self._data.items()}
        merged: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            override = overrides.get(str(item.get("id") or ""))
            row = dict(item)
            row["alias"] = str((override or {}).get("alias") or "")
            row["note"] = str((override or {}).get("note") or "")
            merged.append(row)
        return merged
