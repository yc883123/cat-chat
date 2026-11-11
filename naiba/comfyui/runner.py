"""造梦机执行器：串行逐段提交 ComfyUI，checkpoint 原子推进，产物字节落本地。

口径（与聊天域 jobs.py 一致，代码独立）：
- 提交失败必须带 ComfyUI 返回的原因（node_errors 优先），整任务失败；
- 「历史里没有 + 队列里也没有」才判该段丢失，丢失后有界重提交（预算 1）；
- 每段 30 分钟轮询超时；取消 = 两步（/interrupt 停当前 + /queue delete 移排队）；
- 段完成后把 ``/view`` 产物字节写进 ``data/comfyui/outputs/<task_id>/``，
  任务只保存相对文件名——ComfyUI 清历史后产物仍可看（计划 D7）；
- checkpoint（prompt_ids/completed/errors/resubmits）每次推进都整体落盘：
  进程崩溃重启或用户重试时，从 checkpoint 续跑——已完成段不重复提交（P4）。
"""

from __future__ import annotations

import threading
import traceback
from typing import Any

from naiba.comfyui import MAX_FILES_PER_SHOT, MAX_RESUBMIT, MAX_SHOTS, SHOT_TIMEOUT_SECONDS
from naiba.comfyui import client
from naiba.comfyui import stepmeter
from naiba.comfyui.params import apply_overrides, media_kind_of_field
from naiba.comfyui.schema import NodeSchemaProvider, input_is_optional
from naiba.comfyui.store import now_ms
from naiba.core.contracts import COMFYUI_SUBMISSIONS_KEY

POLL_INTERVAL_SECONDS = 1.5


class ComfyUIRunner:
    """串行执行队列：一个调度线程处理任务，逐段顺序提交。"""

    def __init__(self, app: Any, store: Any):
        self.app = app
        self.store = store
        self._queue: list[str] = []
        self._delivering: set[str] = set()
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._shutdown = False
        self._cancel_events: dict[str, threading.Event] = {}
        self._registry_lock = threading.Lock()

    # ---- 队列 ----

    def submit(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        with self._condition:
            if self._shutdown or task_id in self._delivering or not task or task.get("status") != "queued":
                return
            self._delivering.add(task_id)
            self._queue.append(str(task_id))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop, name="naiba-comfyui-run", daemon=True
                )
                self._worker.start()

    def is_busy(self) -> bool:
        with self._condition:
            return bool(self._queue) or bool(self._cancel_events)

    def cancel(self, task_id: str, *, reason: str = "任务已取消") -> bool:
        """两步取消 + 本地终态化；已终态的任务返回 False。

        运行中的任务只置取消信号并请求 ComfyUI 停/删，worker 循环负责收尾
        （保留已下载的产物）；排队中的任务直接本地终态化。
        """
        task_id = str(task_id or "").strip()
        with self._registry_lock:
            task = self.store.get_task(task_id)
            if task is None or str(task.get("status") or "") in {"completed", "failed", "cancelled", "interrupted"}:
                return False
            cancel_event = self._cancel_events.get(task_id)
            if cancel_event is not None:
                cancel_event.set()
            with self._condition:
                if task_id in self._queue:
                    self._queue.remove(task_id)
                    self._delivering.discard(task_id)
            if cancel_event is None:
                checkpoint = task.get("checkpoint") or {}
                shots = int((task.get("input") or {}).get("shots") or 0)
                counts = self._progress_counts(checkpoint, shots)
                self._finish_terminal(task_id, "cancelled", "任务已取消", counts)
        # 全局 interrupt 只能在队列确认当前运行项属于本任务时调用。
        checkpoint = task.get("checkpoint") or {}
        prompt_ids = [str(pid) for pid in (checkpoint.get("prompt_ids") or []) if pid]
        done_ids = {str(item.get("prompt_id") or "") for item in (checkpoint.get("completed") or [])
                    if isinstance(item, dict)}
        pending = [pid for pid in prompt_ids if pid and pid not in done_ids]
        base = self._task_base(task)
        if pending:
            self._cancel_prompts(base, pending)
        return True

    @staticmethod
    def _cancel_prompts(base: str, prompt_ids: list[str]) -> None:
        # 删除只命中明确的凭证；查询失败时绝不盲目中断其他用户的运行。
        client.delete_queue_items(base, prompt_ids)
        snapshot = client.queue_snapshot(base)
        targets = set(prompt_ids)
        if snapshot is not None and snapshot[1] and snapshot[1].issubset(targets):
            client.interrupt(base)

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._shutdown = True
            remaining = list(self._queue)
            self._queue.clear()
            self._condition.notify_all()
        for event in list(self._cancel_events.values()):
            event.set()
        for task_id in remaining:
            task = self.store.get_task(task_id) or {}
            counts = self._progress_counts(
                task.get("checkpoint") or {}, int((task.get("input") or {}).get("shots") or 0))
            self._finish_terminal(task_id, "cancelled", "服务关闭，任务已取消", counts)
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._shutdown:
                    self._condition.wait(timeout=1.0)
                task_id = self._queue.pop(0) if self._queue else ""
                if self._shutdown:
                    return
            if task_id:
                try:
                    self._run_task(task_id)
                except Exception:  # noqa: BLE001 - 单任务异常不拖垮 worker
                    traceback.print_exc()
                    self._fail_task(task_id, "造梦机任务执行失败（内部错误）")
                finally:
                    with self._condition:
                        self._delivering.discard(task_id)
                    # An explicit retry may race the previous worker's final cleanup.
                    task = self.store.get_task(task_id)
                    if not self._shutdown and task and task.get("status") == "queued":
                        self.submit(task_id)

    # ---- 单任务 ----

    def _base_url(self) -> str:
        return client.normalize_base_url(self.app.config.data.get("comfyui_url"))

    def _task_base(self, _task: dict[str, Any]) -> str:
        """任务执行目标地址（统一走设置里的 ComfyUI 地址）。"""
        return self._base_url()

    def _run_task(self, task_id: str) -> None:
        # 读取排队态、发布运行态与登记取消信号不能被取消操作穿插。
        with self._registry_lock:
            task = self.store.get_task(task_id)
            if not task or task.get("status") != "queued" or self._shutdown:
                return
            cancel_event = threading.Event()
            self._cancel_events[task_id] = cancel_event
            try:
                self.store.update_task(task_id, status="running", started_at=now_ms(), detail="")
            except Exception:
                self._cancel_events.pop(task_id, None)
                raise
        input_data = task.get("input") or {}
        shots = max(1, min(int(input_data.get("shots") or 1), MAX_SHOTS))
        prompt = input_data.get("prompt") if isinstance(input_data.get("prompt"), dict) else {}
        overrides = [item for item in (input_data.get("overrides") or [])
                     if isinstance(item, dict)]
        media_inputs = [item for item in (input_data.get("media_inputs") or [])
                        if isinstance(item, dict)]
        # 用户手动 ⊘ 的媒体槽：独立于 media_inputs，不受 optional 声明限制。
        media_skip = [item for item in (input_data.get("media_skip") or [])
                      if isinstance(item, dict)]
        # 同一任务执行周期内同一资产只上传一次（计划 §5.2）：任务重试/续跑会
        # 重新进入 _run_task，从快照引用重新读取并按需重新上传。
        media_upload_cache: dict[str, str] = {}
        base = self._task_base(task)
        try:
            # checkpoint 从任务现状续起：新任务是空集；重试任务保留 completed，
            # 由 jobs 层清掉 errors 后把任务放回队列。
            checkpoint = dict(task.get("checkpoint") or {})
            prompt_ids: list[str] = [str(pid) for pid in (checkpoint.get("prompt_ids") or [])]
            completed: list[dict[str, Any]] = [
                item for item in (checkpoint.get("completed") or []) if isinstance(item, dict)
            ]
            errors: list[dict[str, Any]] = []
            resubmits: dict[str, int] = dict(checkpoint.get("resubmits") or {})
            checkpoint.update(prompt_ids=prompt_ids, completed=completed,
                              errors=errors, resubmits=resubmits)
            self.store.update_task(task_id, checkpoint=checkpoint)
            self.store.append_event(task_id, {
                "type": "task_started", "message": f"开始生成 {shots} 段", "comfy_url": base,
            })
            if client.reachable(base) is None:
                self._fail_task(
                    task_id,
                    "ComfyUI 服务不可达：请确认 ComfyUI 已启动、地址正确（设置里的 ComfyUI 地址）",
                )
                return
            done_indexes = {int(item.get("index")) for item in completed
                            if str(item.get("index", "")).lstrip("-").isdigit()}
            if checkpoint.get(COMFYUI_SUBMISSIONS_KEY):
                raise RuntimeError("上次生成请求是否已受理尚未确认，已保留记录；请在 ComfyUI 核对后再处理，未重复生成")
            for index in range(shots):
                if cancel_event.is_set():
                    break
                if index in done_indexes:
                    continue
                prompt_id = prompt_ids[index] if index < len(prompt_ids) else ""
                if not prompt_id or not self._resume_submitted(base, prompt_id):
                    workflow = apply_overrides(prompt, overrides, randomize_seeds=True)
                    self._remove_unselected_optional_media(workflow, media_inputs, base, media_skip)
                    media_error = self._apply_media(base, workflow, media_inputs, media_upload_cache)
                    if cancel_event.is_set():
                        break
                    if media_error:
                        raise RuntimeError(f"第 {index + 1} 段媒体输入处理失败：{media_error}")
                    prompt_id, reason = self._submit_shot(base, task_id, index, workflow, checkpoint, cancel_event)
                    if cancel_event.is_set():
                        break
                    if not prompt_id:
                        raise RuntimeError(f"第 {index + 1} 段提交失败：{reason or '工作流或节点错误'}")
                    prompt_ids = checkpoint["prompt_ids"]
                ok, files, fail_reason = self._wait_shot(
                    base, task_id, index, prompt_id, cancel_event, resubmits,
                    checkpoint, prompt, overrides, media_inputs, media_upload_cache,
                    media_skip,
                )
                prompt_id = checkpoint["prompt_ids"][index]
                if cancel_event.is_set():
                    break
                if not ok:
                    errors.append({"index": index, "prompt_id": prompt_id, "error": fail_reason})
                    checkpoint.update(errors=errors)
                    self.store.update_task(
                        task_id, checkpoint=checkpoint,
                        progress=self._progress_counts(checkpoint, shots),
                        detail=fail_reason,
                    )
                    self.store.append_event(task_id, {
                        "type": "shot_failed", "index": index, "error": fail_reason,
                    })
                    self._fail_task(task_id, fail_reason)
                    return
                completed.append({"index": index, "prompt_id": prompt_id, "files": files})
                checkpoint.update(completed=completed)
                counts = self._progress_counts(checkpoint, shots)
                self.store.update_task(
                    task_id, checkpoint=checkpoint, progress=counts,
                    detail=f"完成 {counts['done']}/{shots} 段",
                )
                self.store.append_event(task_id, {
                    "type": "shot_done", "index": index, "files": files,
                })
            checkpoint.update(prompt_ids=prompt_ids, completed=completed,
                              errors=errors, resubmits=resubmits)
            counts = self._progress_counts(checkpoint, shots)
            if cancel_event.is_set():
                detail = f"已取消，完成 {counts['done']}/{shots} 段" if counts["done"] else "任务已取消"
                if checkpoint.get(COMFYUI_SUBMISSIONS_KEY):
                    detail += "；生成请求是否已受理尚未确认，请在 ComfyUI 核对，不能直接重试"
                self._finish_terminal(task_id, "cancelled", detail, counts, checkpoint)
                self.store.append_event(task_id, {"type": "error", "message": "任务已取消"})
                return
            outputs = self._all_output_names(completed)
            status = "failed" if (errors and not completed) else "completed"
            detail = ""
            if errors:
                detail = f"完成 {counts['done']} 段，失败 {counts['failed']} 段（可重试续跑未完成段）"
            self._finish_terminal(
                task_id, status, detail, counts, checkpoint,
                result={"completed": completed, "outputs": outputs},
                error="部分段失败" if errors and completed else "",
            )
            self.store.append_event(task_id, {"type": "task_done", "status": status})
        except Exception as exc:  # noqa: BLE001 - 兜底终态
            traceback.print_exc()
            if cancel_event.is_set():
                current = self.store.get_task(task_id) or {}
                saved = current.get("checkpoint") or {}
                self._finish_terminal(task_id, "cancelled", str(exc) or "任务已取消",
                                      self._progress_counts(saved, shots))
            else:
                self._fail_task(task_id, str(exc) or "造梦机任务执行失败")
        finally:
            cancel_event.set()
            with self._registry_lock:
                self._cancel_events.pop(task_id, None)
            # 逐次卸载（母本 §5.5，收尾口径）：一轮工作流执行结束（完成 / 取消 /
            # 失败都算一个边界）即请求释放；受理与失败只记事件，不改判已落盘产物。
            if input_data.get("unload_after"):
                self._unload_after_run(task_id, base)

    def _unload_after_run(self, task_id: str, base: str) -> None:
        try:
            accepted, message = client.free_models(base)
        except Exception as exc:  # noqa: BLE001 - 卸载失败不改判产物
            accepted, message = False, str(exc)
        try:
            self.store.append_event(task_id, {"type": "unload_requested",
                                              "accepted": bool(accepted), "detail": str(message)})
        except Exception:  # noqa: BLE001 - 事件写失败不影响收尾
            pass

    @staticmethod
    def _media_value_is_empty(value: Any) -> bool:
        """工作流里媒体加载器的原值是否为空。

        只有空值/None 才允许走 optional 自动跳过；非空（如固定音频、默认底图）
        必须沿用原文件，不能因为槽位声明 optional 就删掉。
        """
        if value is None:
            return True
        if isinstance(value, str):
            text = value.strip()
            return not text or text == "None"
        return False

    @staticmethod
    def _remove_unselected_optional_media(
        workflow: dict[str, Any],
        media_inputs: list[dict[str, Any]],
        base: str,
        media_skip: list[dict[str, Any]] | None = None,
    ) -> None:
        """按明确优先级处理提交副本里的媒体连接（2026-11-09 口径）。

        - 已选新文件：保留连接，后续由 ``_apply_media`` 回填；
        - 用户手动 ``⊘``：无论 optional 与否都移除下游连接，ComfyUI 拒绝就报
          它的正常错误（手动跳过是唯一不受 optional 限制的路径）；
        - 没选新文件但工作流原值非空：沿用原文件，即使 optional 也不删；
        - 没选新文件、原值为空且 schema 确认 optional：自动跳过。

        只改本次提交的深拷贝；清理加载节点时仍要确认没有其它连接引用它。
        """
        selected = {
            (str(item.get("node_id") or ""), str(item.get("field_name") or ""))
            for item in media_inputs
            if item.get("path")
        }
        skipped = {
            (str(item.get("node_id") or ""), str(item.get("field_name") or ""))
            for item in (media_skip or [])
            if isinstance(item, dict)
        }
        schemas = NodeSchemaProvider(base)
        removed_sources: set[str] = set()
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            # GetVideoComponents 是视频加载器与下游之间的桥：它的 video 输入
            # 不是「下游消费边」，跳过整节点；下游对它的引用才是要移除的连接。
            # 它本身会在失去所有引用时按下面的清理规则移除。
            if str(node.get("class_type") or "") == "GetVideoComponents":
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            media_connections: list[tuple[str, str, tuple[str, str], Any]] = []
            for field_name, connection in inputs.items():
                if not (isinstance(connection, list) and len(connection) == 2):
                    continue
                source_id = str(connection[0])
                source = workflow.get(source_id)
                if not isinstance(source, dict):
                    continue
                source_inputs = source.get("inputs")
                if not isinstance(source_inputs, dict):
                    continue
                source_class = str(source.get("class_type") or "")
                loader_id = source_id
                if source_class == "GetVideoComponents":
                    video_connection = source_inputs.get("video")
                    if not isinstance(video_connection, list) or len(video_connection) != 2:
                        continue
                    loader_id = str(video_connection[0])
                    loader = workflow.get(loader_id) or {}
                    source_class = str(loader.get("class_type") or "")
                    source_inputs = loader.get("inputs") or {}
                media_fields = [
                    key for key in source_inputs
                    if media_kind_of_field(source_class, str(key))
                ]
                if len(media_fields) == 1:
                    media_connections.append((
                        str(field_name), source_id,
                        (loader_id, media_fields[0]),
                        source_inputs.get(media_fields[0]),
                    ))
            if not media_connections:
                continue
            # schema 只在需要判断「空值能否自动跳过」时才查：手动 ⊘ 与沿用
            # 原文件都不依赖 ComfyUI 声明，离线也应照常提交。
            declarations: dict[str, Any] | None = None
            for field_name, source_id, address, original in media_connections:
                if address in skipped:
                    # 手动 ⊘：独立强制路径，不以 optional 为门槛。
                    inputs.pop(field_name, None)
                    removed_sources.add(source_id)
                    continue
                if address in selected:
                    # 用户选了新文件：保留连接，执行前由 _apply_media 回填。
                    continue
                if not ComfyUIRunner._media_value_is_empty(original):
                    # 沿用工作流原文件：固定素材不能被 optional 自动跳过。
                    continue
                # 与参数提示共用同一套可选性规则：普通 optional 直接放行，
                # Autogrow 必须校验前缀/编号范围/min，模棱两可的不移除。
                if declarations is None:
                    declarations = schemas.node_inputs(str(node.get("class_type") or "")) or {}
                if declarations and input_is_optional(declarations, field_name) is True:
                    inputs.pop(field_name, None)
                    removed_sources.add(source_id)
        # Only prune the exact loader edges removed above; never trim
        # arbitrary disconnected nodes or a loader still used by another branch.
        used = {str(value[0]) for node in workflow.values()
                for value in (node.get("inputs") or {}).values()
                if isinstance(value, list) and len(value) == 2}
        for source_id in removed_sources - used:
            removed = workflow.pop(source_id, None) or {}
            if removed.get("class_type") == "GetVideoComponents":
                connection = (removed.get("inputs") or {}).get("video")
                if isinstance(connection, list) and len(connection) == 2:
                    removed_sources.add(str(connection[0]))
        used = {str(value[0]) for node in workflow.values()
                for value in (node.get("inputs") or {}).values()
                if isinstance(value, list) and len(value) == 2}
        for source_id in removed_sources - used:
            workflow.pop(source_id, None)

    def _apply_media(
        self,
        base: str,
        workflow: dict[str, Any],
        media_inputs: list[dict[str, Any]],
        upload_cache: dict[str, str],
    ) -> str:
        """把媒体输入上传 ComfyUI 并回填进工作流副本（计划 §5.2）。

        - 只改提交用的 prompt 副本，不碰工作流库快照与后续任务；
        - 同一任务执行周期内同一资产只上传一次：``upload_cache`` 按
          受管引用路径复用 ComfyUI 返回的文件名；
        - 上传失败返回带媒体类型/节点/字段的错误说明，调用方按该段失败处理；
          **不回退旧文件名继续提交**。
        """
        for item in media_inputs:
            node_id = str(item.get("node_id") or "")
            field_name = str(item.get("field_name") or "")
            path = str(item.get("path") or "")
            media = str(item.get("media") or "image")
            node = workflow.get(node_id)
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict) \
                    or field_name not in node["inputs"]:
                return (f"{media} 输入（节点 {node_id} · {field_name}）"
                        "不在这份工作流里，无法回填")
            reference = upload_cache.get(path)
            if reference is None:
                reference, reason = client.upload_media(base, path)
                if not reference:
                    return (f"{media} 上传失败（节点 {node_id} · {field_name}）：{reason}")
                upload_cache[path] = reference
            node["inputs"][field_name] = reference
        return ""

    @staticmethod
    def _resume_submitted(base: str, prompt_id: str) -> bool:
        """True 接回旧执行；只有明确失败或两次历史与队列均无记录才可重提。"""
        try:
            entry = client.history_entry(base, prompt_id)
            had_history = entry is not None
            status = client.entry_status(entry) if entry is not None else ""
            if status in {"success", "completed"}:
                return True
            if status in {"error", "failed"}:
                return False
            snapshot = client.queue_snapshot(base)
            if snapshot is None:
                raise RuntimeError("队列读取失败")
            if prompt_id in snapshot[0]:
                return True
            # 任务可能刚从队列移入历史，再读取一次避免误判丢失。
            entry = client.history_entry(base, prompt_id)
            if entry is None:
                if had_history:
                    raise RuntimeError("历史记录未确认终态后消失")
                return False
            status = client.entry_status(entry)
            if status in {"success", "completed"}:
                return True
            if status in {"error", "failed"}:
                return False
            raise RuntimeError("历史尚未报告终态")
        except Exception as exc:
            raise RuntimeError(f"无法确认原生成 {prompt_id} 的状态，已保留凭证且未重复提交：{exc}") from exc

    def _submit_shot(self, base: str, task_id: str, index: int, workflow: dict[str, Any],
                     checkpoint: dict[str, Any], cancel_event: threading.Event) -> tuple[str, str]:
        # 先持久占位再发请求：响应丢失/进程退出不能被后续重试误当成尚未提交。
        with self._registry_lock:
            if cancel_event.is_set():
                return "", "用户取消"
            submissions = checkpoint.setdefault(COMFYUI_SUBMISSIONS_KEY, {})
            submissions[str(index)] = "submitting"
            self.store.update_task(task_id, checkpoint=checkpoint)
        if cancel_event.is_set():
            submissions.pop(str(index), None)
            self.store.update_task(task_id, checkpoint=checkpoint)
            return "", "用户取消"
        try:
            prompt_id, reason = client.submit(base, workflow)
        except Exception as exc:
            prompt_id, reason = "", str(exc) or type(exc).__name__
        if prompt_id:
            prompt_ids = checkpoint.setdefault("prompt_ids", [])
            while len(prompt_ids) <= index:
                prompt_ids.append("")
            prompt_ids[index] = prompt_id
            submissions.pop(str(index), None)
        elif getattr(reason, "definite", False):
            submissions.pop(str(index), None)
        else:
            submissions[str(index)] = "unknown"
            reason = f"{reason or '未收到提交凭证'}；生成请求是否已受理尚未确认，请先在 ComfyUI 核对，不能直接重试"
        try:
            self.store.update_task(task_id, checkpoint=checkpoint)
        except Exception as exc:
            # 占位还在磁盘，后续重试会拒绝；把已知 ID 写进错误，尽力停止该请求。
            if prompt_id:
                self._cancel_prompts(base, [prompt_id])
            raise RuntimeError(f"保存生成凭证失败（prompt_id={prompt_id or '未知'}）；已保留提交占位，不会自动重提：{exc}") from exc
        if prompt_id:
            self.store.append_event(task_id, {"type": "shot_submitted", "index": index, "prompt_id": prompt_id})
            if cancel_event.is_set():
                self._cancel_prompts(base, [prompt_id])
        return prompt_id, str(reason)

    def _wait_shot(
        self,
        base: str,
        task_id: str,
        index: int,
        prompt_id: str,
        cancel_event: threading.Event,
        resubmits: dict[str, int],
        checkpoint: dict[str, Any],
        prompt: dict[str, Any],
        overrides: list[dict[str, Any]],
        media_inputs: list[dict[str, Any]],
        media_upload_cache: dict[str, str],
        media_skip: list[dict[str, Any]],
    ) -> tuple[bool, list[str], str]:
        """轮询一段直到成功 / 失败 / 丢失重提交 / 超时。返回 (ok, 本地文件名, 失败原因)。"""
        deadline = now_ms() + SHOT_TIMEOUT_SECONDS * 1000
        while True:
            if cancel_event.is_set():
                return False, [], "用户取消"
            if now_ms() > deadline:
                return False, [], f"第 {index + 1} 段轮询超时（单段上限 {SHOT_TIMEOUT_SECONDS // 60} 分钟）"
            try:
                entry = client.history_entry(base, prompt_id)
            except Exception as exc:
                return False, [], f"无法读取原生成 {prompt_id} 的历史，已保留凭证且未重复提交：{exc}"
            if entry is not None:
                status = client.entry_status(entry)
                if status in {"error", "failed"}:
                    detail = client.entry_error(entry) or f"ComfyUI 状态 {status}"
                    return False, [], f"第 {index + 1} 段执行失败：{detail}"
                if status in {"success", "completed"} or entry.get("outputs"):
                    items = client.output_items(entry)
                    if not items:
                        return False, [], f"第 {index + 1} 段完成但没有输出文件"
                    try:
                        files = self._save_outputs(task_id, index, base, items)
                    except Exception as exc:  # noqa: BLE001 - 产物落盘失败按段失败处理
                        return False, [], f"第 {index + 1} 段产物保存失败：{exc}"
                    return True, files, ""
            queue_state = client.queue_snapshot(base)
            queue = queue_state[0] if queue_state is not None else None
            if queue is None or prompt_id in queue:
                # 仍在排队/执行中；队列查询失败时保守处理，不判定丢失。
                # 顺带提取一次采样步级进度（失败静默降级，绝不影响主链路）。
                try:
                    stepmeter.observe(base, prompt_id,
                                      is_running=queue_state is not None and prompt_id in queue_state[1])
                except Exception:  # noqa: BLE001 - 提取失败只是没有条
                    pass
                cancel_event.wait(POLL_INTERVAL_SECONDS)
                continue
            # 查询队列期间可能刚结束；再核对历史后才允许有界重提。
            try:
                if self._resume_submitted(base, prompt_id):
                    continue
            except RuntimeError as exc:
                return False, [], str(exc)
            used = int(resubmits.get(str(index), 0) or 0)
            if used < MAX_RESUBMIT:
                workflow = apply_overrides(prompt, overrides, randomize_seeds=True)
                self._remove_unselected_optional_media(workflow, media_inputs, base, media_skip)
                media_error = self._apply_media(
                    base, workflow, media_inputs, media_upload_cache,
                )
                if cancel_event.is_set():
                    return False, [], "用户取消"
                if media_error:
                    return False, [], f"第 {index + 1} 段媒体输入处理失败：{media_error}"
                new_prompt_id, reason = self._submit_shot(base, task_id, index, workflow, checkpoint, cancel_event)
                if cancel_event.is_set():
                    return False, [], "用户取消"
                if new_prompt_id:
                    resubmits[str(index)] = used + 1
                    checkpoint["resubmits"] = resubmits
                    self.store.update_task(task_id, checkpoint=checkpoint)
                    self.store.append_event(task_id, {
                        "type": "shot_lost", "index": index,
                        "message": f"第 {index + 1} 段被 ComfyUI 丢弃，已自动重新提交",
                    })
                    prompt_id = new_prompt_id
                    continue
                return False, [], str(reason)
            return False, [], f"第 {index + 1} 段已被 ComfyUI 丢弃（服务重启或队列被清空）"

    def _save_outputs(self, task_id: str, index: int, base: str,
                      items: list[dict[str, str]]) -> list[str]:
        """把一段的产物字节写入受管目录，返回相对文件名列表。"""
        target_dir = self.store.task_output_dir(task_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for position, item in enumerate(items[:MAX_FILES_PER_SHOT]):
            data = client.download_output(base, item)
            if not data:
                raise ValueError(f"产物 {item.get('filename')} 内容为空")
            original = item["filename"].replace("\\", "/").split("/")[-1] or "output.bin"
            name = f"{index + 1:03d}_{position:02d}_{original}"
            path = target_dir / name
            tmp = target_dir / f".{name}.tmp"
            tmp.write_bytes(data)
            tmp.replace(path)
            saved.append(name)
        return saved

    @staticmethod
    def _all_output_names(completed: list[dict[str, Any]]) -> list[str]:
        names: list[str] = []
        for entry in completed:
            for name in entry.get("files") or []:
                if name and name not in names:
                    names.append(name)
        return names

    @staticmethod
    def _progress_counts(checkpoint: dict[str, Any], shots: int) -> dict[str, int]:
        done = len(checkpoint.get("completed") or [])
        failed = len(checkpoint.get("errors") or [])
        return {"done": done, "total": int(shots), "failed": failed}

    def _finish_terminal(
        self,
        task_id: str,
        status: str,
        detail: str,
        counts: dict[str, int],
        checkpoint: dict[str, Any] | None = None,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        fields: dict[str, Any] = {
            "status": status, "detail": detail, "finished_at": now_ms(),
            "progress": counts,
        }
        if checkpoint is not None:
            fields["checkpoint"] = checkpoint
        if result is not None:
            fields["result"] = result
        if error:
            fields["error"] = error
        self.store.update_task(task_id, **fields)

    def _fail_task(self, task_id: str, message: str) -> None:
        task = self.store.get_task(task_id) or {}
        counts = self._progress_counts(
            task.get("checkpoint") or {}, int((task.get("input") or {}).get("shots") or 0))
        self._finish_terminal(task_id, "failed", message, counts, error=message)
        self.store.append_event(task_id, {"type": "error", "message": message})
