"""job/subagent 域工具 Provider：任务工具「schema + 执行函数」单一定义。

处理器来源（整块复用，不做改写型手术）：
- ``naiba.subagent.job_tool_handler_factory``：run_in_background/job_output/job_status/job_wait/job_kill；
- ``naiba.subagent.subagent_handler_factory``：subagent（fork）/ subagent_spawn（spawn）；
- 本模块函数：todo_write（自 app.py 处理器原样抽取，self→app 参数）。

依赖经构造参数注入（AppContext Protocol），不摸全局。
"""
from __future__ import annotations

import dataclasses
import json
from functools import partial
from typing import Any

from naiba.core.contracts import AppContext
from naiba.subagent import job_tool_handler_factory, subagent_handler_factory
from naiba.tools.registry import ToolSpec, build_job_tool_specs


def _todo_write_handler(
    app: AppContext, args: dict[str, Any], _skills: Any, run_context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    run_id = str((run_context or {}).get("run_id") or (run_context or {}).get("job_id") or "")
    if not run_id:
        return False, "无法确定当前运行，不能保存任务清单"
    raw = (args or {}).get("todos")
    if not isinstance(raw, list) or len(raw) > 100:
        return False, "todos 必须是最多 100 项的数组"
    todos: list[dict[str, str]] = []
    active = 0
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return False, f"第 {index} 项不是对象"
        content = str(item.get("content") or "").strip()
        status = str(item.get("status") or "pending")
        if not content or status not in {"pending", "in_progress", "completed"}:
            return False, f"第 {index} 项缺少 content 或 status 无效"
        active += int(status == "in_progress")
        todos.append({"id": str(item.get("id") or index), "content": content[:1000], "status": status})
    if active > 1:
        return False, "同时最多只能有一个 in_progress 任务"
    return True, json.dumps({"saved": True, "todos": todos}, ensure_ascii=False)


class JobToolProvider:
    """job/subagent 域：8 个任务工具（含 subagent/subagent_spawn/todo_write）单一定义。"""

    def __init__(self, app: AppContext) -> None:
        handlers = dict(job_tool_handler_factory(app))
        # 两种上下文模式 = 两个互斥的工具（工具集二选一，模型零判断，见 §九.116）：
        # subagent 继承父会话历史（fork，默认方向），subagent_spawn 走干净上下文。
        handlers["subagent"] = subagent_handler_factory(app, fork=True)
        handlers["subagent_spawn"] = subagent_handler_factory(app, fork=False)
        handlers["todo_write"] = partial(_todo_write_handler, app)
        self._handlers = handlers

    def tools(self) -> list[ToolSpec]:
        rows: list[ToolSpec] = []
        for spec in build_job_tool_specs():
            handler = self._handlers.get(spec.name)
            if handler is None:
                continue
            rows.append(dataclasses.replace(spec, execute=handler, system=True))
        return rows
