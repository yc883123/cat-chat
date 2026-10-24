"""造梦机任务管理：提交 / 查询 / 取消 / 重试续跑 / 启动恢复 / 批量删除。

提交契约（服务端权威）：
- 只接受库里的工作流 id（不接受内联 prompt）——工作流库是唯一入口，
  导入时已做过 API 格式校验与参数扫描；
- overrides 只接受扫描出的槽位，值按槽位类型保真转换（转不动=400）；
- shots ≤ 200（对齐聊天域批量口径）；
- 任务 JSON 保存工作流快照（prompt 副本 + 名称），删除工作流不破坏历史任务。

重试契约：终态任务（failed / cancelled / interrupted）可直接重试——保留
checkpoint（已完成段不重复提交），清掉 errors 后把任务放回队列。
"""

from __future__ import annotations

import time
from typing import Any

from naiba.comfyui import (
    COMFYUI_ACTIVE_STATUSES,
    MAX_SHOTS,
)
from naiba.comfyui.store import ComfyUIStoreError, now_ms


class ComfyUITaskError(ValueError):
    """任务操作非法（参数/状态）。"""


def _format_default(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "…"


class ComfyUIJobManager:
    def __init__(self, app: Any, store: Any, runner: Any):
        self.app = app
        self.store = store
        self.runner = runner

    # ---- 启动恢复 ----

    def recover_interrupted(self) -> int:
        """启动时遗留 queued/running → interrupted；任务详情里说明可重试续跑。"""
        recovered = 0
        for task in self.store.list_tasks(active_only=True, limit=5000):
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            self.runner.cancel(task_id, reason="服务重启前任务中断")
            checkpoint = task.get("checkpoint") or {}
            completed = len(checkpoint.get("completed") or [])
            shots = int((task.get("input") or {}).get("shots") or 0)
            detail = (
                "服务重启前任务中断。已完成 "
                f"{completed}/{shots} 段的产物已保存在本地；"
                "点「重试」会从没完成的段继续，不会重复提交已完成的段。"
            )
            self.store.update_task(
                task_id, status="interrupted", detail=detail,
                finished_at=now_ms(),
                progress={
                    "done": completed,
                    "total": shots,
                    "failed": len(checkpoint.get("errors") or []),
                },
            )
            try:
                self.store.append_event(task_id, {
                    "type": "error", "message": "服务重启前任务中断",
                })
            except ComfyUIStoreError:
                pass
            recovered += 1
        return recovered

    # ---- 提交 ----

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ComfyUITaskError("请求体必须是对象")
        workflow_id = str(body.get("workflow_id") or "").strip()
        if not workflow_id:
            raise ComfyUITaskError("请先选择要执行的工作流")
        entry = self.store.get_workflow(workflow_id)
        if entry is None:
            raise ComfyUITaskError("工作流不存在或已被删除")
        try:
            shots = max(1, min(int(body.get("shots", 1) or 1), MAX_SHOTS))
        except (TypeError, ValueError):
            raise ComfyUITaskError("shots 必须是正整数") from None
        overrides = self._validate_overrides(entry, body.get("overrides"))
        input_data = {
            "workflow_id": workflow_id,
            "workflow_name": str(entry.get("name") or ""),
            # 快照：导入时校验过的 API prompt 原样保存，任务不随后续编辑漂移。
            "prompt": entry.get("prompt") or {},
            "params": entry.get("params") or [],
            "overrides": overrides,
            "shots": shots,
        }
        task = self.store.create_task(input_data)
        self.runner.submit(str(task.get("id") or ""))
        return task

    def _validate_overrides(self, entry: dict[str, Any], raw: Any) -> list[dict[str, Any]]:
        """overrides 只认扫描出的槽位；值按槽位类型保真转换，转不动直接拒绝。"""
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ComfyUITaskError("overrides 必须是数组")
        params = entry.get("params") or []
        slot_map: dict[tuple[str, str], dict[str, Any]] = {
            (str(slot.get("node_id") or ""), str(slot.get("field_name") or "")): slot
            for slot in params if isinstance(slot, dict)
        }
        out: list[dict[str, Any]] = []
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                raise ComfyUITaskError(f"第 {index} 条参数格式非法")
            node_id = str(item.get("node_id") or "")
            field_name = str(item.get("field_name") or "")
            slot = slot_map.get((node_id, field_name))
            if slot is None:
                raise ComfyUITaskError(
                    f"参数 {node_id} · {field_name} 不是这份工作流的可编辑槽位"
                )
            value = item.get("value")
            field_type = str(slot.get("field_type") or "text")
            if field_type == "seed":
                # seed：空值 = 每次随机（不留覆盖）；给了就按数字校验。
                if value is None or str(value).strip() == "":
                    continue
                try:
                    number = int(float(str(value).strip()))
                except (TypeError, ValueError):
                    raise ComfyUITaskError(f"参数 {slot.get('label')} 必须是数字") from None
                if number < 0:
                    continue  # 负数沿用「随机」语义，不覆盖
                out.append({"node_id": node_id, "field_name": field_name, "value": number})
                continue
            if field_type == "number":
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise ComfyUITaskError(f"参数 {slot.get('label')} 必须是数字") from None
                default = slot.get("default")
                out.append({
                    "node_id": node_id, "field_name": field_name,
                    "value": int(number) if isinstance(default, int) else number,
                })
                continue
            if field_type == "bool":
                if isinstance(value, bool):
                    out.append({"node_id": node_id, "field_name": field_name, "value": value})
                else:
                    text = str(value).strip().lower()
                    if text in ("true", "1", "是"):
                        out.append({"node_id": node_id, "field_name": field_name, "value": True})
                    elif text in ("false", "0", "否", ""):
                        out.append({"node_id": node_id, "field_name": field_name, "value": False})
                    else:
                        raise ComfyUITaskError(f"参数 {slot.get('label')} 必须是开/关")
                continue
            # text / image：原样字符串（image 槽本期不上传，用户填已存在的文件名）。
            out.append({
                "node_id": node_id, "field_name": field_name,
                "value": "" if value is None else str(value),
            })
        return out

    # ---- 查询 ----

    def tasks(self, active_only: bool = False, limit: int = 60) -> list[dict[str, Any]]:
        return [self._slim(task) for task in self.store.list_tasks(active_only=active_only,
                                                                   limit=limit)]

    def task(self, task_id: str) -> dict[str, Any] | None:
        task = self.store.get_task(task_id)
        if task is None:
            return None
        task = dict(task)
        task["can_retry"] = str(task.get("status") or "") in {"failed", "cancelled", "interrupted"}
        task["can_cancel"] = str(task.get("status") or "") in COMFYUI_ACTIVE_STATUSES
        return task

    def events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.store.read_events(task_id, after=after)

    # ---- 取消 / 重试 / 删除 ----

    def cancel(self, task_id: str) -> bool:
        return self.runner.cancel(task_id, reason="用户取消")

    def retry(self, task_id: str) -> dict[str, Any]:
        """重试 = 续跑：保留 checkpoint（已完成段不再提交），errors 清掉后重新排队。"""
        task = self.store.get_task(task_id)
        if task is None:
            raise LookupError("任务不存在")
        if str(task.get("status") or "") in COMFYUI_ACTIVE_STATUSES:
            raise ComfyUITaskError("任务还在执行，请先取消或等待结束")
        checkpoint = dict(task.get("checkpoint") or {})
        checkpoint["errors"] = []
        shots = int((task.get("input") or {}).get("shots") or 1)
        done = len(checkpoint.get("completed") or [])
        if done >= shots:
            raise ComfyUITaskError("所有段都已完成，没有需要重试的内容")
        self.store.update_task(
            task_id, status="queued", checkpoint=checkpoint, error="", detail="",
            # 重试是新的一轮运行：清掉上一轮的结束时刻（开始时刻由 runner 重写）。
            finished_at=None,
            progress={"done": done, "total": shots, "failed": 0},
        )
        self.store.append_event(task_id, {
            "type": "log", "message": f"重试：从第 {done + 1} 段继续（已完成的段不会重复提交）",
        })
        self.runner.submit(task_id)
        return self.store.get_task(task_id) or task

    def delete_tasks(self, body: dict[str, Any], *, cancel_timeout: float = 15.0) -> dict[str, Any]:
        """删除任务记录、事件与产物目录。

        - 未指定 id = 清全部：只删已结束的任务，执行中的跳过并回报（「清除已结束」语义）；
        - 指定 id 且 ``cancel_active`` 为真：排队/运行中的任务先取消，等进入终态
          后再删——等待超时或取消失败时如实回报失败原因，任务保留不删。
        """
        ids = body.get("task_ids") if isinstance(body, dict) else None
        cancel_active = bool(isinstance(body, dict) and body.get("cancel_active"))
        if isinstance(ids, list) and ids:
            targets = [str(item).strip() for item in ids[:200] if str(item).strip()]
        else:
            # 未指定 id = 清全部：执行中的在循环里跳过并回报。
            targets = [str(task.get("id") or "")
                       for task in self.store.list_tasks(limit=5000)]
        deleted = 0
        skipped: list[str] = []
        failed: list[dict[str, str]] = []
        for task_id in targets:
            task = self.store.get_task(task_id)
            if task is None:
                continue
            if str(task.get("status") or "") in COMFYUI_ACTIVE_STATUSES:
                if not cancel_active:
                    skipped.append(task_id)
                    continue
                reason = ""
                try:
                    cancelled = self.runner.cancel(task_id, reason="删除任务前自动取消")
                except Exception as exc:  # noqa: BLE001 - 取消失败要如实回报
                    cancelled = False
                    reason = f"取消失败：{exc}"
                if not cancelled:
                    # 返回 False 多半是刚好已终态（竞态）：重查一次，真终态就继续删。
                    task = self.store.get_task(task_id)
                    if task is None:
                        continue
                    if str(task.get("status") or "") in COMFYUI_ACTIVE_STATUSES:
                        failed.append({
                            "id": task_id,
                            "reason": reason or "任务无法取消，已保留这条任务",
                        })
                        continue
                if not self._wait_terminal(task_id, timeout=cancel_timeout):
                    failed.append({
                        "id": task_id,
                        "reason": "已发出取消，但任务迟迟没有停下来，先保留这条任务，稍后再删",
                    })
                    continue
            if self.store.delete_task(task_id):
                deleted += 1
        return {"deleted": deleted, "skipped": skipped, "failed": failed}

    def _wait_terminal(self, task_id: str, *, timeout: float, interval: float = 0.15) -> bool:
        """等任务离开活动状态；超时返回 False（任务保留，由调用方回报）。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            task = self.store.get_task(task_id)
            if task is None or str(task.get("status") or "") not in COMFYUI_ACTIVE_STATUSES:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def delete_workflow(self, workflow_id: str) -> tuple[bool, str]:
        """删除工作流；被排队/执行中的任务引用时拒绝（历史任务用快照，不受影响）。"""
        for task in self.store.list_tasks(active_only=True, limit=5000):
            input_data = task.get("input") or {}
            if str(input_data.get("workflow_id") or "") == str(workflow_id):
                return False, "有正在排队或执行中的任务还在用这份工作流，请先取消它们再删除"
        return self.store.delete_workflow(workflow_id), ""

    # ---- 内部 ----

    def _slim(self, task: dict[str, Any]) -> dict[str, Any]:
        input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
        checkpoint = task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}
        result = task.get("result") if isinstance(task.get("result"), dict) else {"completed": [], "outputs": []}
        return {
            "id": task.get("id"),
            "status": task.get("status"),
            "module": task.get("module"),
            "origin_label": task.get("origin_label"),
            "detail": task.get("detail") or "",
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "progress": task.get("progress") or {},
            "result": result,
            "output_files": self._output_files(task.get("id"), result),
            "error": task.get("error") or "",
            "source_task_id": task.get("source_task_id") or "",
            "workflow_id": input_data.get("workflow_id") or "",
            "workflow_name": input_data.get("workflow_name") or "",
            "shots": input_data.get("shots") or 1,
            "done_segments": len(checkpoint.get("completed") or []),
            "failed_segments": len(checkpoint.get("errors") or []),
        }

    def _output_files(self, task_id: Any, result: dict[str, Any]) -> list[dict[str, str]]:
        """产物清单：文件名 + 本地绝对路径。

        只有「任务 id + 文件名」不够用：前端要拿这条路径去走 `/api/file`——手机/局域网
        靠 URL 上的口令才取得到字节，桌面端靠 `path` 参数才能弹原生「另存为」
        （`<a download>` 在 WebView2 里点了没反应，见 03-media 的 saveLocalFile）。
        这里只拼接、不校验存在性：文件被清理时交给请求侧 404，与图片 404 同一条链。
        """
        tid = str(task_id or "")
        if not tid:
            return []
        base = self.store.task_output_dir(tid)
        return [
            {"name": str(name), "path": str(base / str(name))}
            for name in (result.get("outputs") or [])
            if str(name or "")
        ]
