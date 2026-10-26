# -*- coding: utf-8 -*-
"""章节稿件（原文 / 升级前正文 / 人工改编稿 / AI 改编稿）的数据契约。

一份稿件是**独立正文记录**，不覆盖其它稿件：

| 字段 | 含义 |
|---|---|
| `id` | 稳定记录 ID（`ms-` + 12 位十六进制），删除后永不复用 |
| `chapter_no` | 所属章节号 |
| `kind` | `original` / `legacy` / `manual` / `ai` |
| `no` | 改编稿的显示编号（1、2、3…），只增不重排；原文与升级前正文为 0 |
| `label` | 显示名（原文 / 升级前保存的正文 / 改编稿 1） |
| `content` | 正文 |
| `source_manuscript_id` | 改编稿基于哪份稿件；原文与升级前正文为空 |
| `task_id` | 产出这份稿件的 Studio 任务（人工编辑为空） |
| `user_request` | AI 改编时用户写的要求（人工编辑为空） |

`legacy` 是升级前的既有正文：不能伪称它是「未经修改的原文」（计划 §4.3）。
它照常可以阅读、编辑和用作生成素材，但界面必须如实说明原文来源缺失。
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

KIND_ORIGINAL = "original"
KIND_LEGACY = "legacy"
KIND_MANUAL = "manual"
KIND_AI = "ai"
KINDS = (KIND_ORIGINAL, KIND_LEGACY, KIND_MANUAL, KIND_AI)
# 只有这两种是「改编稿」：它们是编辑/润色的产物，显示编号，且必须记录来源稿件。
ADAPTED_KINDS = (KIND_MANUAL, KIND_AI)

KIND_LABELS = {
    KIND_ORIGINAL: "原文",
    KIND_LEGACY: "升级前保存的正文",
    KIND_MANUAL: "人工改编稿",
    KIND_AI: "AI 改编稿",
}

MANUSCRIPT_ID_PATTERN = re.compile(r"^ms-[0-9a-f]{12}$")

# 显示名的前缀：原文与升级前正文用 kind 的名字，改编稿用「改编稿 N」。
ADAPTED_LABEL_PREFIX = "改编稿"

SCHEMA_VERSION = 2


def new_id() -> str:
    """新的稿件记录 ID（删除后不复用，也不按数组位置推断）。"""
    return f"ms-{uuid.uuid4().hex[:12]}"


def display_label(kind: str, no: int) -> str:
    """稿件显示名：原文类用固定名，改编稿用「改编稿 N」。"""
    if kind in ADAPTED_KINDS:
        number = int(no or 0)
        return f"{ADAPTED_LABEL_PREFIX} {number}" if number > 0 else ADAPTED_LABEL_PREFIX
    return KIND_LABELS.get(str(kind) or "", "稿件")


def normalize(payload: Any, *, chapter_no: int) -> dict[str, Any]:
    """把落盘记录收敛成完整结构；损坏记录抛错而不是补空正文。"""
    if not isinstance(payload, dict):
        raise ValueError("稿件记录不是对象")
    manuscript_id = str(payload.get("id") or "").strip()
    if not MANUSCRIPT_ID_PATTERN.match(manuscript_id):
        raise ValueError("稿件 ID 非法")
    kind = str(payload.get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError(f"稿件类型非法：{kind or '（空）'}")
    if int(payload.get("chapter_no") or 0) != int(chapter_no):
        raise ValueError("稿件所属章节不一致")
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError("稿件缺少正文")
    no = int(payload.get("no") or 0) if kind in ADAPTED_KINDS else 0
    return {
        "schema_version": SCHEMA_VERSION,
        "id": manuscript_id,
        "chapter_no": int(chapter_no),
        "kind": kind,
        "no": max(0, no),
        "label": str(payload.get("label") or "").strip() or display_label(kind, no),
        "content": content,
        "len": len(content),
        "source_manuscript_id": str(payload.get("source_manuscript_id") or ""),
        "created_at": int(payload.get("created_at") or 0),
        "task_id": str(payload.get("task_id") or ""),
        "user_request": str(payload.get("user_request") or ""),
    }


def build(*, chapter_no: int, kind: str, content: str, no: int = 0,
          source_manuscript_id: str = "", task_id: str = "",
          user_request: str = "", created_at: int | None = None,
          label: str = "") -> dict[str, Any]:
    """构造一份新稿件记录（调用方负责先做输入校验）。"""
    if kind not in KINDS:
        raise ValueError(f"稿件类型非法：{kind or '（空）'}")
    stamp = int(created_at if created_at is not None else time.time() * 1000)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": new_id(),
        "chapter_no": int(chapter_no),
        "kind": kind,
        "no": max(0, int(no or 0)) if kind in ADAPTED_KINDS else 0,
        "label": str(label or "").strip() or display_label(kind, no),
        "content": str(content),
        "len": len(str(content)),
        "source_manuscript_id": str(source_manuscript_id or ""),
        "created_at": stamp,
        "task_id": str(task_id or ""),
        "user_request": str(user_request or ""),
    }


def public(row: dict[str, Any], *, with_content: bool = True) -> dict[str, Any]:
    """对外视图：`chapter_no` 只在章节接口内部有意义，不重复下发。"""
    view = {
        "id": str(row.get("id") or ""),
        "kind": str(row.get("kind") or ""),
        "kind_label": KIND_LABELS.get(str(row.get("kind") or ""), ""),
        "no": int(row.get("no") or 0),
        "label": str(row.get("label") or ""),
        "len": int(row.get("len") or 0),
        "created_at": int(row.get("created_at") or 0),
        "source_manuscript_id": str(row.get("source_manuscript_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "user_request": str(row.get("user_request") or ""),
    }
    if with_content:
        view["content"] = str(row.get("content") or "")
    return view
