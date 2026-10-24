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
from naiba.comfyui.params import apply_overrides, media_kind_of_field
from naiba.comfyui.schema import NodeSchemaProvider
from naiba.comfyui.store import now_ms

POLL_INTERVAL_SECONDS = 1.5


class ComfyUIRunner:
    """串行执行队列：一个调度线程处理任务，逐段顺序提交。"""

    def __init__(self, app: Any, store: Any):
        self.app = app
        self.store = store
        self._queue: list[str] = []
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._shutdown = False
        self._cancel_events: dict[str, threading.Event] = {}
        self._registry_lock = threading.Lock()

    # ---- 队列 ----

    def submit(self, task_id: str) -> None:
        with self._condition:
            if self._shutdown:
                return
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
        task = self.store.get_task(task_id)
        if task is None:
            return False
        if str(task.get("status") or "") in {"completed", "failed", "cancelled", "interrupted"}:
            return False
        cancel_event = None
        with self._registry_lock:
            cancel_event = self._cancel_events.get(task_id)
            if task_id in self._queue:
                with self._condition:
                    self._queue.remove(task_id)
        # 两步取消：先请求中断正在执行的段，再把未完成的 prompt 从 ComfyUI 队列移除。
        checkpoint = task.get("checkpoint") or {}
        prompt_ids = [str(pid) for pid in (checkpoint.get("prompt_ids") or []) if pid]
        done_ids = {str(item.get("prompt_id") or "") for item in (checkpoint.get("completed") or [])
                    if isinstance(item, dict)}
        pending = [pid for pid in prompt_ids if pid and pid not in done_ids]
        base = self._base_url()
        if pending:
            client.interrupt(base)
            client.delete_queue_items(base, pending)
        if cancel_event is not None:
            cancel_event.set()
            return True
        shots = int((task.get("input") or {}).get("shots") or 0)
        counts = self._progress_counts(checkpoint, shots)
        detail = f"已取消，完成 {counts['done']}/{shots} 段" if counts["done"] else "任务已取消"
        self._finish_terminal(task_id, "cancelled", detail, counts)
        return True

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

    # ---- 单任务 ----

    def _base_url(self) -> str:
        return client.normalize_base_url(self.app.config.data.get("comfyui_url"))

    def _run_task(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if not task or task.get("status") != "queued":
            return
        input_data = task.get("input") or {}
        shots = max(1, min(int(input_data.get("shots") or 1), MAX_SHOTS))
        prompt = input_data.get("prompt") if isinstance(input_data.get("prompt"), dict) else {}
        overrides = [item for item in (input_data.get("overrides") or [])
                     if isinstance(item, dict)]
        media_inputs = [item for item in (input_data.get("media_inputs") or [])
                        if isinstance(item, dict)]
        # 同一任务执行周期内同一资产只上传一次（计划 §5.2）：任务重试/续跑会
        # 重新进入 _run_task，从快照引用重新读取并按需重新上传。
        media_upload_cache: dict[str, str] = {}
        base = self._base_url()
        cancel_event = threading.Event()
        with self._registry_lock:
            self._cancel_events[task_id] = cancel_event
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
            self.store.update_task(
                task_id, status="running", started_at=now_ms(), detail="",
                checkpoint=checkpoint,
            )
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
            # 恢复收养：上次已提交但未确认完成的段，先查历史——已完成的直接收产物，
            # 不重复提交（P4：已完成工作不重复提交）。
            for index, prompt_id in enumerate(list(prompt_ids)):
                if not prompt_id or index in done_indexes:
                    continue
                adopted = self._adopt_submitted(
                    base, task_id, index, prompt_id, completed, errors,
                )
                if adopted:
                    done_indexes.add(index)
                    checkpoint.update(prompt_ids=prompt_ids, completed=completed,
                                      errors=errors, resubmits=resubmits)
                    self.store.update_task(
                        task_id, checkpoint=checkpoint,
                        progress=self._progress_counts(checkpoint, shots),
                    )
            for index in range(shots):
                if cancel_event.is_set():
                    break
                if index in done_indexes:
                    continue
                workflow = apply_overrides(prompt, overrides, randomize_seeds=True)
                self._remove_unselected_optional_media(workflow, media_inputs, base)
                media_error = self._apply_media(
                    base, workflow, media_inputs, media_upload_cache,
                )
                if media_error:
                    message = f"第 {index + 1} 段媒体输入处理失败：{media_error}"
                    errors.append({"index": index, "error": message})
                    checkpoint.update(errors=errors)
                    self.store.update_task(
                        task_id, checkpoint=checkpoint,
                        progress=self._progress_counts(checkpoint, shots),
                        detail=message,
                    )
                    self.store.append_event(task_id, {
                        "type": "shot_failed", "index": index, "error": message,
                    })
                    self._fail_task(task_id, message)
                    return
                prompt_id, reason = client.submit(base, workflow)
                if not prompt_id:
                    message = f"第 {index + 1} 段提交失败：{reason}" if reason \
                        else f"第 {index + 1} 段提交失败（工作流或节点错误）"
                    errors.append({"index": index, "error": message})
                    checkpoint.update(errors=errors)
                    self.store.update_task(
                        task_id, checkpoint=checkpoint,
                        progress=self._progress_counts(checkpoint, shots),
                        detail=message,
                    )
                    self.store.append_event(task_id, {
                        "type": "shot_failed", "index": index, "error": message,
                    })
                    self._fail_task(task_id, message)
                    return
                while len(prompt_ids) <= index:
                    prompt_ids.append("")
                prompt_ids[index] = prompt_id
                checkpoint.update(prompt_ids=prompt_ids)
                self.store.update_task(task_id, checkpoint=checkpoint)
                self.store.append_event(task_id, {
                    "type": "shot_submitted", "index": index, "prompt_id": prompt_id,
                })
                ok, files, fail_reason = self._wait_shot(
                    base, task_id, index, prompt_id, cancel_event, resubmits,
                    checkpoint, prompt, overrides, media_inputs, media_upload_cache,
                )
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
            self._fail_task(task_id, str(exc) or "造梦机任务执行失败")
        finally:
            cancel_event.set()
            with self._registry_lock:
                self._cancel_events.pop(task_id, None)

    @staticmethod
    def _remove_unselected_optional_media(
        workflow: dict[str, Any],
        media_inputs: list[dict[str, Any]],
        base: str,
    ) -> None:
        """Omit unselected media connections only where ComfyUI declares them optional."""
        selected = {
            (str(item.get("node_id") or ""), str(item.get("field_name") or ""))
            for item in media_inputs
            if item.get("path")
        }
        schemas = NodeSchemaProvider(base)
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            media_connections: list[tuple[str, str]] = []
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
                media_fields = [
                    key for key in source_inputs
                    if media_kind_of_field(source_class, str(key))
                ]
                if len(media_fields) == 1 and (source_id, media_fields[0]) not in selected:
                    media_connections.append((str(field_name), source_id))
            if not media_connections:
                continue
            declarations = schemas.node_inputs(str(node.get("class_type") or ""))
            if not declarations:
                continue
            for field_name, source_id in media_connections:
                declaration = declarations.get(field_name)
                if declaration is None:
                    declaration = next((value for key, value in declarations.items()
                                         if field_name.startswith(f"{key}.")), None)
                if declaration and declaration.get("optional") is True:
                    inputs.pop(field_name, None)

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

    def _adopt_submitted(
        self,
        base: str,
        task_id: str,
        index: int,
        prompt_id: str,
        completed: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> bool:
        """已提交未确认的段：查历史收养结果；不可收养（还没跑/已丢）返回 False。"""
        try:
            entry = client.history_entry(base, prompt_id)
        except Exception:  # noqa: BLE001 - 查不到按未完成处理，交给后续提交逻辑
            return False
        if entry is None:
            return False
        status = client.entry_status(entry)
        if status in {"error", "failed"}:
            detail = client.entry_error(entry) or f"ComfyUI 状态 {status}"
            errors.append({"index": index, "prompt_id": prompt_id,
                           "error": f"第 {index + 1} 段执行失败：{detail}"})
            return True
        items = client.output_items(entry) if (status in {"success", "completed"}
                                               or entry.get("outputs")) else []
        if not items:
            return False
        try:
            files = self._save_outputs(task_id, index, base, items)
        except Exception as exc:  # noqa: BLE001 - 收养时落盘失败按未完成处理
            traceback.print_exc()
            return False
        completed.append({"index": index, "prompt_id": prompt_id, "files": files,
                          "recovered": True})
        self.store.append_event(task_id, {
            "type": "shot_done", "index": index, "files": files, "recovered": True,
        })
        return True

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
            except Exception:  # noqa: BLE001 - 网络抖动按「暂时查不到」继续轮询
                entry = None
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
            queue = client.queue_ids(base)
            if queue is None or prompt_id in queue:
                # 仍在排队/执行中；队列查询失败时保守处理，不判定丢失。
                cancel_event.wait(POLL_INTERVAL_SECONDS)
                continue
            # 历史无 + 队列无：ComfyUI 重启或清队列导致丢失。有界重提交一次。
            used = int(resubmits.get(str(index), 0) or 0)
            if used < MAX_RESUBMIT:
                workflow = apply_overrides(prompt, overrides, randomize_seeds=True)
                self._remove_unselected_optional_media(workflow, media_inputs, base)
                media_error = self._apply_media(
                    base, workflow, media_inputs, media_upload_cache,
                )
                if media_error:
                    return False, [], f"第 {index + 1} 段媒体输入处理失败：{media_error}"
                new_prompt_id, _reason = client.submit(base, workflow)
                if new_prompt_id:
                    resubmits[str(index)] = used + 1
                    prompt_ids = list(checkpoint.get("prompt_ids") or [])
                    while len(prompt_ids) <= index:
                        prompt_ids.append("")
                    prompt_ids[index] = new_prompt_id
                    checkpoint.update(prompt_ids=prompt_ids)
                    self.store.update_task(task_id, checkpoint=checkpoint)
                    self.store.append_event(task_id, {
                        "type": "shot_lost", "index": index,
                        "message": f"第 {index + 1} 段被 ComfyUI 丢弃，已自动重新提交",
                    })
                    prompt_id = new_prompt_id
                    continue
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
