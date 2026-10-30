"""Studio 生成任务管理：提交/查询/查询事件/取消/重试/启动恢复。

- 任务走 Studio 自己的生命周期（`data_dir/studio/tasks/`），不创建普通会话，不产生
  chat Run，不进 run_events/任务面板的会话语义。
- 提交时输入_ABSOLUTELY 落盘（含最终发给模型的 user 内容），重启后仍可重试。
"""

from __future__ import annotations

from typing import Any

from naiba.core.exceptions import TaskCancelled
from naiba.studio.runner import StudioRunner
from naiba.studio.store import StudioStore


class StudioTaskError(ValueError):
    """任务操作非法。"""


class StudioJobManager:
    def __init__(self, app: Any, store: StudioStore, runner: StudioRunner):
        self.app = app
        self.store = store
        self.runner = runner

    # ---- 生命周期 ----

    def recover_interrupted(self) -> int:
        """启动收敛：遗留 running/queued 无人看管 → interrupted（保留 partial，可重试）。"""
        recovered = 0
        for task in self.store.list_tasks(active_only=True):
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            self.runner.cancel(task_id, reason="服务重启前任务中断")  # 排队清理/停止在跑 worker
            partial = self.store.partial_from_events(task_id)
            updated = self.store.update_task(
                task_id,
                status="interrupted",
                result_partial=partial,
                detail="服务重启前任务中断，结果保留为已产出部分",
                finished_at=None,
            )
            if updated is None and self.store.get_task(task_id) is not None:
                self.store.update_task(task_id, status="interrupted", result_partial=partial)
            if self.store.has_terminal_event(task_id) is False:
                self.store.append_event(task_id, {"type": "error", "message": "服务重启前任务中断"})
            recovered += 1
        return recovered

    # ---- 查询 ----

    def tasks(self, active_only: bool = False, limit: int = 60,
              module: str = "", exclude_module: str = "",
              exclude_modules: str = "") -> list[dict[str, Any]]:
        return self.store.list_tasks(
            active_only=active_only, limit=limit,
            module=module, exclude_module=exclude_module,
            exclude_modules=exclude_modules,
        )

    def task(self, task_id: str) -> dict[str, Any] | None:
        return self.store.get_task(task_id)

    def events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        """增量事件轮询：当前 seq 与终端事件是否已送达随 payload 返回。"""
        rows = self.store.read_events(task_id, after=after)
        task = self.store.get_task(task_id)
        if task is None:
            return rows
        return rows

    # ---- 提交 / 取消 / 重试 ----

    def submit(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise StudioTaskError("生成输入非法")
        content = str(input_data.get("content") or "")
        if not content.strip():
            raise StudioTaskError("生成内容不能为空")
        if not str(input_data.get("model_name") or "").strip():
            raise StudioTaskError("请选择模型；可用模型来自设置页的模型检查结果")
        task = self.store.create_task(input_data)
        self.runner.submit(task["id"])
        return task

    def cancel(self, task_id: str) -> bool:
        return self.runner.cancel(task_id, reason="用户取消")

    def delete(self, task_ids: list[str]) -> dict[str, Any]:
        """批量删除**已结束**的任务记录（编辑模式多选删除的后端口径）。

        进行中（queued/running）的任务一律跳过：runner 还在写同一份记录，
        「先取消再删」是用户的显式动作，不能被多选删除顺手做掉。
        """
        deleted: list[str] = []
        skipped: list[dict[str, str]] = []
        for raw in task_ids or []:
            task_id = str(raw or "").strip()
            if not task_id:
                continue
            task = self.store.get_task(task_id)
            if task is None:
                skipped.append({"id": task_id, "reason": "not_found"})
                continue
            if str(task.get("status") or "") in {"queued", "running"}:
                skipped.append({"id": task_id, "reason": "active"})
                continue
            if self.store.delete_task(task_id):
                deleted.append(task_id)
            else:
                skipped.append({"id": task_id, "reason": "not_found"})
        return {"deleted": deleted, "skipped": skipped}

    def retry(self, task_id: str) -> dict[str, Any]:
        """失败/取消/中断任务的幂等重试：以原输入重建排队任务（原记录保留）。"""
        source = self.store.get_task(task_id)
        if not source:
            raise LookupError("任务不存在")
        if source.get("status") not in {"failed", "cancelled", "interrupted"}:
            raise StudioTaskError("仅失败、取消或中断的任务会重试")
        input_data = dict(source.get("input") or {})
        task = self.store.create_task(input_data)
        self.runner.submit(task["id"])
        return task

    def shutdown(self, timeout: float = 5.0) -> None:
        self.runner.shutdown(timeout=timeout)

    # 防御性引用：保证 TaskCancelled 导入不出历史包袱（runner 内部集中处理）。
    _cancelled_type = TaskCancelled
