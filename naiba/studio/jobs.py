"""Studio 生成任务管理：提交/查询/查询事件/取消/重试/启动恢复。

- 任务走 Studio 自己的生命周期（`data_dir/studio/tasks/`），不创建普通会话，不产生
  chat Run，不进 run_events/任务面板的会话语义。
- 提交时输入_ABSOLUTELY 落盘（含最终发给模型的 user 内容），重启后仍可重试。
"""

from __future__ import annotations

from typing import Any

import copy
import hashlib

from naiba.core.contracts import (STUDIO_THREAD_ID_KEY, STUDIO_PARENT_TASK_ID_KEY,
                                  STUDIO_HISTORY_MESSAGES_KEY, STUDIO_SKILL_SNAPSHOT_KEY,
                                  STUDIO_RESULT_ARTIFACT_KEY, STUDIO_LOGICAL_GENERATION_ID_KEY,
                                  STUDIO_RESUMED_FROM_KEY, STUDIO_RESUMED_INTO_KEY)
from naiba.studio.artifacts import has_result_contract, ResultArtifactError
from naiba.studio.conversation import completed_history
from naiba.studio.content import ContentSnapshots
from naiba.studio.model_identity import configured_identity, check_resume_identity
from naiba.core.exceptions import TaskCancelled
from naiba.studio.runner import StudioRunner
from naiba.studio.store import StudioStore, now_ms


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
        for task in self.store.list_tasks(active_only=True, limit=100000):
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            self.runner.cancel(task_id, reason="服务重启前任务中断")  # 排队清理/停止在跑 worker
            if has_result_contract(task):
                try:
                    partial = self.store.artifacts.read(task, require_final=False)[0]
                except ResultArtifactError:
                    partial = ""
            else:
                partial = self.store.partial_from_events(task_id) or str(task.get("result_partial") or "")
            # 崩溃的精确时刻不可知；记录本次收敛为终态的时间，不能留下无完成时间的终态。
            finished_at = now_ms()
            updated = self.store.update_task(
                task_id,
                status="interrupted",
                result_partial=partial,
                detail="服务重启前任务中断，结果保留为已产出部分",
                finished_at=finished_at,
            )
            if updated is None and self.store.get_task(task_id) is not None:
                self.store.update_task(
                    task_id, status="interrupted", result_partial=partial,
                    finished_at=finished_at,
                )
            if self.store.has_terminal_event(task_id) is False:
                self.store.append_event(task_id, {"type": "error", "message": "服务重启前任务中断"})
            recovered += 1
        return recovered

    # ---- 查询 ----

    def tasks(self, active_only: bool = False, limit: int = 60,
              module: str = "", exclude_module: str = "",
              exclude_modules: str = "") -> list[dict[str, Any]]:
        rows = self.store.list_tasks(
            active_only=active_only, limit=limit,
            module=module, exclude_module=exclude_module,
            exclude_modules=exclude_modules,
        )
        for task in rows:
            if has_result_contract(task):
                task["result"] = task["result_partial"] = ""
                try:
                    self.store.artifacts.manifest(task)
                    if not isinstance(task.get(STUDIO_RESULT_ARTIFACT_KEY), dict):
                        raise ResultArtifactError("TXT公开摘要缺失")
                except ResultArtifactError as exc:
                    task[STUDIO_RESULT_ARTIFACT_KEY] = {"status": "invalid", "error": str(exc)}
        return rows

    def task(self, task_id: str) -> dict[str, Any] | None:
        task = self.store.get_task(task_id)
        if task and has_result_contract(task):
            task["result"] = task["result_partial"] = ""
            try:
                text, artifact = self.store.artifacts.read(task, require_final=False)
                task[STUDIO_RESULT_ARTIFACT_KEY] = artifact
                task["result" if artifact["status"] == "final" else "result_partial"] = text
            except ResultArtifactError as exc:
                task[STUDIO_RESULT_ARTIFACT_KEY] = {"schema_version": 1, "status": "invalid", "error": str(exc)}
        return task

    def result_text(self, task_id: str, require_final: bool = True) -> str:
        task = self.store.get_task(task_id)
        if task is None:
            raise LookupError("任务不存在")
        if has_result_contract(task):
            return self.store.artifacts.read(task, require_final=require_final)[0]
        return str(task.get("result") or (task.get("result_partial") if not require_final else "") or "")

    def result_artifact(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise LookupError("任务不存在")
        if not has_result_contract(task):
            raise StudioTaskError("此历史任务没有TXT产物，请使用原结果")
        text, artifact = self.store.artifacts.read(task, require_final=False)
        return {"task_id": task_id, "text": text, "artifact": artifact, "truncated": False}

    def events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        """增量事件轮询：当前 seq 与终端事件是否已送达随 payload 返回。"""
        rows = self.store.read_events(task_id, after=after)
        task = self.store.get_task(task_id)
        if task is None:
            return rows
        return rows

    # ---- 提交 / 取消 / 重试 ----

    def lookup_request(self, request_key: str, *, owner_id: str = "") -> dict[str, Any] | None:
        return self.store.lookup_request(request_key, owner_id=owner_id)

    def submit(self, input_data: dict[str, Any], *, request_key: str = "",
               owner_id: str = "", source_task_id: str = "",
               submission_policy: dict[str, Any] | None = None,
               thread_id: str = "", parent_task_id: str = "") -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise StudioTaskError("生成输入非法")
        content = str(input_data.get("content") or "")
        if not content.strip():
            raise StudioTaskError("生成内容不能为空")
        if not str(input_data.get("model_name") or "").strip():
            raise StudioTaskError("请选择模型；可用模型来自设置页的模型检查结果")
        if (input_data.get("module") in {"novel", "writer"} and not source_task_id
                and "model_snapshot" not in input_data):
            input_data = copy.deepcopy(input_data)
            profile = self.app.config.profile(str(input_data.get("model_key") or ""))
            input_data["model_snapshot"] = configured_identity(input_data, profile)
        task = self.store.create_task(
            input_data, request_key=request_key, owner_id=owner_id,
            source_task_id=source_task_id, submission_policy=submission_policy,
            thread_id=thread_id, parent_task_id=parent_task_id,
        )
        self.runner.submit(task["id"])
        return task

    def followup(self, task_id: str, content: str, images: list[dict[str, Any]], *,
                 request_key: str) -> dict[str, Any]:
        source = self.store.get_task(task_id)
        if source is None:
            raise LookupError("任务不存在")
        input_data = copy.deepcopy(source.get("input") or {})
        if input_data.get("module") in {"novel", "writer"} or source.get("owner_id"):
            raise StudioTaskError("此任务不支持提词库追问")
        if input_data.get("chunked"):
            raise StudioTaskError("分块任务不能在提词库追问")
        history = completed_history(source)
        if input_data.get(STUDIO_SKILL_SNAPSHOT_KEY):
            ContentSnapshots(self.app.paths.data_dir).normalize_and_freeze(input_data, None)
        input_data[STUDIO_HISTORY_MESSAGES_KEY] = history
        input_data["content"] = content
        input_data["user_request"] = content
        input_data["images"] = copy.deepcopy(images)
        return self.submit(
            input_data, request_key=request_key,
            thread_id=str(source.get(STUDIO_THREAD_ID_KEY) or task_id), parent_task_id=task_id,
            submission_policy=source.get("submission_policy") or {},
        )

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
            with self.store._lock:
                task = self.store.get_task(task_id)
                if task is None:
                    skipped.append({"id": task_id, "reason": "not_found"})
                    continue
                if str(task.get("status") or "") in {"queued", "running"}:
                    skipped.append({"id": task_id, "reason": "active"})
                    continue
                # Resume recognition and deletion share this same lock. A
                # frozen descendant or a prepared child keeps its source alive.
                referenced = any(other["id"] != task_id and (
                    other.get(STUDIO_RESUMED_FROM_KEY) == task_id
                    or (has_result_contract(task) and other.get("source_task_id") == task_id))
                    for other in self.store.list_tasks(limit=100000))
                if referenced:
                    skipped.append({"id": task_id, "reason": "referenced"})
                    continue
                if self.store.delete_task(task_id):
                    if has_result_contract(task):
                        self.store.artifacts.remove(task_id)
                    deleted.append(task_id)
                else:
                    skipped.append({"id": task_id, "reason": "not_found"})
        return {"deleted": deleted, "skipped": skipped}

    def prepare_resume(self, task_id: str, *, request_key: str = "", owner_id: str = "",
                       allow_writer: bool = False) -> dict[str, Any]:
        """Copy confirmed bytes into an unscheduled execution of the same generation."""
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200:
            raise StudioTaskError("继续未完成内容需要有效request_key")
        registry_lock = getattr(self.runner, "_registry_lock", self.store._lock)
        with registry_lock, self.store._lock:
            source = self.store.get_task(task_id)
            if source is None:
                raise LookupError("任务不存在")
            if (source.get("input") or {}).get("module") == "writer" and not allow_writer:
                raise StudioTaskError("织书台任务必须通过原流水线继续")
            if owner_id and source.get("owner_id", "") != owner_id:
                raise StudioTaskError("恢复任务不属于当前owner")
            if source.get("status") not in {"failed", "cancelled", "interrupted"}:
                raise StudioTaskError("只能继续已停止的未完成任务")
            if task_id in getattr(self.runner, "_cancel_events", {}):
                raise StudioTaskError("原执行仍在停止，请稍后继续")
            if not has_result_contract(source):
                raise StudioTaskError("此旧任务没有可恢复的TXT片段，请重新生成")
            data = source.get("input") or {}
            profile = self.app.config.profile(str(data.get("model_key") or ""))
            check_resume_identity(data, profile)
            self.store.artifacts.read(source, require_final=False)
            seed = request_key
            while source.get(STUDIO_RESUMED_INTO_KEY):
                existing_id = source[STUDIO_RESUMED_INTO_KEY]
                existing = self.store.get_task(existing_id)
                if existing:
                    expected_key = hashlib.sha256(f"result-resume:{seed}:{task_id}".encode()).hexdigest()
                    if existing.get("request_key") != expected_key:
                        raise StudioTaskError("原任务已经创建接续执行，请查看该任务")
                    if existing.get("status") in {"queued", "running", "done"}:
                        return existing
                    # A prepared child can become interrupted on restart. Copy
                    # its confirmed state into a fresh queued descendant.
                    if existing["id"] in getattr(self.runner, "_cancel_events", {}):
                        raise StudioTaskError("接续执行仍在停止，请稍后继续")
                    self.store.artifacts.read(existing, require_final=False)
                    check_resume_identity(existing.get("input") or {}, profile)
                    source, task_id = existing, existing["id"]
                else:
                    raise StudioTaskError("已登记的TXT接续任务缺失，不能盲目新建")
            request_key = hashlib.sha256(f"result-resume:{seed}:{task_id}".encode()).hexdigest()
            existing = self.store.lookup_request(request_key, owner_id=str(source.get("owner_id") or ""))
            if existing and existing.get("source_task_id") != task_id:
                raise StudioTaskError("恢复request_key已绑定其他任务")
            child = self.store.create_task(
                copy.deepcopy(source.get("input") or {}), request_key=request_key,
                owner_id=str(source.get("owner_id") or ""), source_task_id=task_id,
                submission_policy=source.get("submission_policy") or {},
                thread_id=str(source.get(STUDIO_THREAD_ID_KEY) or task_id),
                parent_task_id=str(source.get(STUDIO_PARENT_TASK_ID_KEY) or ""))
            child = self.store.update_task(child["id"], **{
                STUDIO_LOGICAL_GENERATION_ID_KEY: source.get(STUDIO_LOGICAL_GENERATION_ID_KEY) or task_id,
                STUDIO_RESUMED_FROM_KEY: task_id}) or child
            artifact = self.store.artifacts.clone(source, child)
            self.store.update_task(child["id"], **{STUDIO_RESULT_ARTIFACT_KEY: artifact})
            self.store.update_task(task_id, **{STUDIO_RESUMED_INTO_KEY: child["id"]})
            return self.store.get_task(child["id"]) or child

    def resume_result(self, task_id: str, *, request_key: str = "") -> dict[str, Any]:
        task = self.prepare_resume(task_id, request_key=request_key)
        self.runner.submit(task["id"])
        return self.task(task["id"]) or task

    def retry(self, task_id: str, *, request_key: str = "", owner_id: str = "",
              submission_policy: dict[str, Any] | None = None) -> dict[str, Any]:
        """失败/取消/中断任务的幂等重试：以原输入重建排队任务（原记录保留）。"""
        source = self.store.get_task(task_id)
        if not source:
            raise LookupError("任务不存在")
        if source.get("status") not in {"failed", "cancelled", "interrupted"}:
            raise StudioTaskError("仅失败、取消或中断的任务会重试")
        input_data = dict(source.get("input") or {})
        source_task_id = task_id
        owner_id = owner_id or str(source.get("owner_id") or "")
        if source.get("owner_id") and owner_id != source.get("owner_id"):
            raise StudioTaskError("任务不属于当前 owner")
        frozen_policy = source.get("submission_policy") or {}
        if submission_policy is not None and submission_policy != frozen_policy:
            raise StudioTaskError("重试不能改变原任务的提交策略")
        submission_policy = frozen_policy
        if input_data.get(STUDIO_SKILL_SNAPSHOT_KEY):
            ContentSnapshots(self.app.paths.data_dir).normalize_and_freeze(input_data, None)
        task = self.store.create_task(
            input_data, request_key=request_key, owner_id=owner_id,
            source_task_id=source_task_id, submission_policy=submission_policy,
            thread_id=str(source.get(STUDIO_THREAD_ID_KEY) or task_id),
            parent_task_id=str(source.get(STUDIO_PARENT_TASK_ID_KEY) or ""),
        )
        self.runner.submit(task["id"])
        return task

    def shutdown(self, timeout: float = 5.0) -> None:
        self.runner.shutdown(timeout=timeout)

    # 防御性引用：保证 TaskCancelled 导入不出历史包袱（runner 内部集中处理）。
    _cancelled_type = TaskCancelled
