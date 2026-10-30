# -*- coding: utf-8 -*-
"""原创小说域（工作台第五个功能页「织书台」，module="writer"）。

职责边界（docs/plans/2026-10-30-写书匠（原创写作）接入实施计划.md §4.1）：

- `store.py`：原创作品、章节、版本、导入暂存和原子持久化。目录在
  `data/studio/writer/<book-id>/`，与拆书匠（`studio/novel/`）**互不混库**。
- `prompts.py`：设定 / 大纲 / 正文提示词、上下文分槽预算、严格 JSON 校验。
- `pipeline.py`：服务端持久化流水线（六步设定、整章 / 分段正文）的推进、取消、
  恢复与重试；前端只发命令与展示，不持有「下一步」。
- `service.py`：`/api/writer/*` 业务门面（返回 `(payload, status)` 二元组）。

共享能力不重复实现：模型调用走 `app.studio.jobs`（任务记录仍在
`data/studio/tasks/`，事件走 `/api/studio/tasks/<id>/events`）；Writer 只保存
作品与流水线状态，不另建模型 API。组合根装配顺序：Studio 之后装配 Writer，
关闭时先停 Writer 协调器，再停 Studio worker。
"""

from __future__ import annotations

__all__ = ["store", "prompts", "pipeline", "service"]
