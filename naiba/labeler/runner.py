"""打标执行器：串行逐图调用，逐图事务落库。

并发口径（P0-a 结论）：
- 业务初始并发固定 1；``config.concurrency`` 只接受已验证档位（当前 ``{1}``）。
- **用量与结果必须在调用线程内一起读**：``complete()`` 之后立刻读 ``last_usage`` /
  ``last_finish_reason``，绝不交给管理线程推断某张图的用量。
- 逐图流程：查既有结果（同图同内容同配置）→ 命中直接标记成功 → 否则 ``mark_running``
  提交 → 调模型 → 一次事务写「标注 + 成功状态 + 用量」。
- 崩溃语义：``mark_running`` 在请求前提交，进程死在返回与提交之间时该行留 ``running``，
  启动恢复标 ``uncertain``（可能已计费），不承诺绝对不重复计费。
"""

from __future__ import annotations

import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from naiba.core.exceptions import TaskCancelled
from naiba.core.contracts import (LABELER_APPROVED_TXT_KEY, LABELER_UNKNOWN_WORKER_SLOT,
                                  LABELER_SUPERVISION_RULES_KEY)
from naiba.llm.profiles import resolve_profile_with_override
from naiba.labeler.engines import get_engine
from naiba.labeler.engines.provider import build_prompt
from naiba.labeler.export import caption_text
from naiba.labeler.gate import (
    DEFAULT_PROVIDER_CONCURRENCY,
    SUPERVISOR_MAX_CONCURRENCY,
    ProviderGate,
)
from naiba.labeler.presets import filter_tags, get_preset, normalize_tag
from naiba.labeler.store import now_ms
from naiba.labeler.supervisor import (
    SupervisionFormatError,
    build_supervision_messages,
    build_supervision_prompt,
    parse_verdict,
    revise_note,
    freeze_supervision_rules,
)
from naiba.labeler.txt_output import TXT_TOOL_NAME, TxtToolContext, build_txt_targets

# 已验证可用的并发档位。P0-a 只通过了离线契约；在线小样本未执行前不开放 >1。
# 多并发走「自定义打标 → 并发打标」开关（按打标员槽位定并发），不受这里限制。
VALIDATED_CONCURRENCY = (1,)
DEFAULT_CONCURRENCY = 1
PROGRESS_WRITE_INTERVAL = 0.3
MAX_RETRY_BACKOFF = 8.0
# 首版固定的返工上限：初次 + 最多 2 次重做 = 每张最多 3 次打标尝试。
MAX_REVISIONS = 2
# 监督调用的有限退避重试次数（429/5xx 等瞬时错误；认证/参数错误不重试）。
SUPERVISOR_RETRY_LIMIT = 1


def classify_error(exc: BaseException) -> str:
    """把异常分成 ``cancelled`` / ``auth`` / ``bad_request`` / ``image`` / ``transient`` / ``permanent``。"""
    if isinstance(exc, TaskCancelled):
        return "cancelled"
    raw = str(exc or "")
    text = raw.lower()
    if "任务已取消" in raw or "cancelled" in text or "canceled" in text:
        return "cancelled"
    if "图片无法读取" in raw or "cannot identify image" in text or "not a valid image" in text:
        return "image"
    auth_markers = ("401", "403", "unauthorized", "invalid api key", "invalid_api_key",
                    "authentication", "鉴权", "apikey", "api key")
    if any(marker in text for marker in auth_markers):
        return "auth"
    transient_markers = ("429", "rate limit", "too many requests", "500", "502", "503", "504",
                         "timeout", "timed out", "超时", "temporarily", "connection reset",
                         "connection aborted", "remotedisconnected", "empty stream", "空流",
                         "overloaded", "server error", "service unavailable")
    if any(marker in text for marker in transient_markers):
        return "transient"
    if any(marker in text for marker in ("400", "invalid request", "参数", "bad request")):
        return "bad_request"
    return "permanent"


def aggregate_usage(current: dict[str, Any], item_usage: dict[str, Any]) -> dict[str, Any]:
    """把单图用量累加进任务总用量（只累加数值型字段）。"""
    merged = dict(current or {})
    for key, value in (item_usage or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = merged.get(key, 0) + value
    return merged


class LabelerRunner:
    """串行执行队列：一个调度线程处理任务，逐图顺序调用。"""

    def __init__(self, app: Any, store: Any, db: Any):
        self.app = app
        self.store = store
        self.db = db
        self._queue: list[str] = []
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._shutdown = False
        self._cancel_events: dict[str, threading.Event] = {}
        self._registry_lock = threading.Lock()
        # 当前任务的监督队列（任务级，结束即 shutdown；测试可观察并发度）。
        self._supervisor_pool: ThreadPoolExecutor | None = None

    # ---- 队列 ----

    def submit(self, task_id: str) -> None:
        with self._condition:
            if self._shutdown:
                return
            self._queue.append(str(task_id))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop, name="naiba-labeler-run", daemon=True
                )
                self._worker.start()

    def is_busy(self) -> bool:
        with self._condition:
            return bool(self._queue) or bool(self._cancel_events)

    def cancel(self, task_id: str, *, reason: str = "任务已取消") -> bool:
        """取消：请求中经 cancel_event 中断；排队中的直接终态化。"""
        task_id = str(task_id or "").strip()
        with self._registry_lock:
            cancel_event = self._cancel_events.get(task_id)
            if cancel_event is not None:
                cancel_event.set()
                return True
        task = self.store.get_task(task_id)
        if not task:
            return False
        if task.get("status") == "queued":
            with self._condition:
                if task_id in self._queue:
                    self._queue.remove(task_id)
            self.db.cancel_pending(task_id)
            counts = self.db.counts(task_id)
            self._finish_task_terminal(
                task_id, "cancelled", {"type": "cancelled"}, detail=reason,
                progress=self._progress_from_counts(counts),
                result={"annotation_ids": self.db.annotation_ids_for_task(task_id)},
            )
            return True
        return False

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._shutdown = True
            remaining = list(self._queue)
            self._queue.clear()
            self._condition.notify_all()
        for event in list(self._cancel_events.values()):
            event.set()
        for task_id in remaining:
            self.db.cancel_pending(task_id)
            self._finish_task_terminal(
                task_id, "cancelled", {"type": "cancelled"}, detail="服务关闭，任务已取消",
                progress=self._progress_from_counts(self.db.counts(task_id)),
            )
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
                    self._fail_task(task_id, "打标任务执行失败（内部错误）")

    # ---- 单任务 ----

    def _run_task(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if not task or task.get("status") != "queued":
            return
        config = dict((task.get("input") or {}).get("config") or {})
        cancel_event = threading.Event()
        with self._registry_lock:
            self._cancel_events[task_id] = cancel_event
        writer = _ProgressWriter(self.store, self.db, task_id)
        try:
            self.store.update_task(task_id, status="running", started_at=now_ms(), detail="")
            self.store.append_event(task_id, {"type": "status", "message": "开始打标"})
            if config.get("concurrency_enabled"):
                self._run_task_concurrent(task_id, task, config, cancel_event, writer)
            else:
                self._run_task_serial(task_id, task, config, cancel_event, writer)
        except Exception as exc:  # noqa: BLE001 - 兜底终态
            traceback.print_exc()
            self._fail_task(task_id, str(exc) or "打标任务执行失败")
        finally:
            cancel_event.set()
            with self._registry_lock:
                self._cancel_events.pop(task_id, None)

    def _run_task_serial(self, task_id: str, task: dict[str, Any],
                         config: dict[str, Any], cancel_event: threading.Event,
                         writer: "_ProgressWriter") -> None:
        """关闭并发时沿用原串行路径：一张一张跑，行为与旧版本逐字节一致。"""
        try:
            try:
                profile = resolve_profile_with_override(
                    self.app.config,
                    str(config.get("model_key") or ""),
                    str(config.get("model_name") or ""),
                )
            except (ValueError, KeyError) as exc:
                self._fail_task(task_id, f"模型配置不可用：{exc}")
                return
            # 云端与本地 provider 都走同一条 complete()；本地后端的串行由 runtime 的
            # 全局执行锁保证（并发档位仍固定 1）。
            # chat 引擎（Skill 打标）：Skill 即规则，不走 build_prompt 的指令拼装；
            # SkillAgent 事件桥接进任务事件流，逐图进度仍由 writer 口径负责。
            is_chat_engine = str(config.get("engine") or "") == "chat"

            def event_bridge(payload: dict[str, Any]) -> None:
                try:
                    self.store.append_event(task_id, payload)
                except Exception:  # noqa: BLE001 - 事件桥故障绝不影响打标主流程
                    pass

            engine_cls = get_engine(str(config.get("engine") or "provider"))
            engine = engine_cls(self.app, config, event_sink=event_bridge)
            # finalize（置信度过滤/标签清洗/触发词）对 chat 产出同样生效；
            # chat 档 preset 固定 general，只有 prompt 分支需要区分。
            preset = get_preset(str(config.get("preset") or ""))
            if is_chat_engine:
                from naiba.labeler.engines.chat import build_chat_prompt

                prompt = build_chat_prompt(config)
            else:
                prompt = build_prompt(config, preset)
            fingerprint = str(config.get("config_fingerprint") or "")
            retry_limit = max(0, min(5, int(config.get("retry_limit") or 0)))
            timeout_seconds = max(5, min(600, int(config.get("timeout_seconds") or 90)))
            total_usage: dict[str, Any] = dict(task.get("usage") or {})
            # 本地后端共用 runtime 的全局执行锁：批量任务不能把聊天挤在门外。
            # 在线 provider 每次调用独立，无需让路。
            is_local = str(profile.get("kind") or "").lower() == "local"
            # 写入本地 TXT：输出目录在提交时已建好；这里把「每张图 → 同名 txt」
            # 的目标一次性算好，同名相对路径在任务内也不会串写。
            write_txt = bool(config.get("write_txt"))
            txt_output: tuple[Any, dict[str, str]] | None = None
            if write_txt:
                output_dir = self.store.outputs_dir(task_id)
                output_dir.mkdir(parents=True, exist_ok=True)
                txt_output = (output_dir, build_txt_targets(self.db.list_items(task_id, limit=10000)))

            for item in self.db.pending_items(task_id):
                if cancel_event.is_set():
                    break
                if is_local:
                    self._yield_to_chat(cancel_event)
                    if cancel_event.is_set():
                        break
                item_usage = self._run_item(
                    task_id=task_id,
                    item=item,
                    config=config,
                    preset=preset,
                    prompt=prompt,
                    engine=engine,
                    profile=profile,
                    fingerprint=fingerprint,
                    retry_limit=retry_limit,
                    timeout_seconds=timeout_seconds,
                    cancel_event=cancel_event,
                    txt_output=txt_output,
                )
                if item_usage:
                    total_usage = aggregate_usage(total_usage, item_usage)
                    writer.set_usage(total_usage)
                writer.tick()
                if is_local:
                    # 短暂让出：等锁的一方（聊天/工作台）有机会在这一张结束后拿到锁。
                    cancel_event.wait(0.2)
            if cancel_event.is_set():
                self.db.cancel_pending(task_id)
            counts = self.db.counts(task_id)
            annotation_ids = self.db.annotation_ids_for_task(task_id)
            self._finish_from_counts(task_id, counts, annotation_ids, total_usage)
        except Exception as exc:  # noqa: BLE001 - 兜底终态
            traceback.print_exc()
            self._fail_task(task_id, str(exc) or "打标任务执行失败")

    def _run_task_concurrent(self, task_id: str, task: dict[str, Any],
                             config: dict[str, Any], cancel_event: threading.Event,
                             writer: "_ProgressWriter") -> None:
        """自定义打标并发监督档：有界打标员池 + 独立有界监督队列。

        - 每个槽位固定一个引擎实例（引擎持有逐次调用的用量读数，必须线程内独占）；
        - 图片由共享游标领取，领取与计数在同一把锁内，不会超领或重复派发；
        - 每张图「打标 → 监督」顺序不变，但不同图片可同时在打标或监督；
        - 同一供应商档案（model_key）的在途请求数由 ``ProviderGate`` 统一限制，
          打标与监督共享额度。
        """
        workers = [dict(row) for row in (config.get("workers") or []) if isinstance(row, dict)]
        supervisor_ref = dict(config.get("supervisor") or {})
        if not workers or not supervisor_ref:
            self._fail_task(task_id, "并发配置不完整：缺少打标员槽位或监督员")
            return
        try:
            worker_profiles = [
                resolve_profile_with_override(
                    self.app.config, str(row.get("model_key") or ""),
                    str(row.get("model_name") or ""),
                )
                for row in workers
            ]
            supervisor_profile = resolve_profile_with_override(
                self.app.config, str(supervisor_ref.get("model_key") or ""),
                str(supervisor_ref.get("model_name") or ""),
            )
        except (ValueError, KeyError) as exc:
            self._fail_task(task_id, f"模型配置不可用：{exc}")
            return

        def event_bridge(payload: dict[str, Any]) -> None:
            try:
                self.store.append_event(task_id, payload)
            except Exception:  # noqa: BLE001 - 事件桥故障绝不影响打标主流程
                pass

        engine_cls = get_engine(str(config.get("engine") or "provider"))
        preset = get_preset(str(config.get("preset") or ""))
        if str(config.get("engine") or "") == "chat":
            from naiba.labeler.engines.chat import build_chat_prompt

            base_prompt = build_chat_prompt(config)
        else:
            base_prompt = build_prompt(config, preset)
        fingerprint = str(config.get("config_fingerprint") or "")
        retry_limit = max(0, min(5, int(config.get("retry_limit") or 0)))
        timeout_seconds = max(5, min(600, int(config.get("timeout_seconds") or 90)))
        txt_output: tuple[Any, dict[str, str]] | None = None
        if bool(config.get("write_txt")):
            output_dir = self.store.outputs_dir(task_id)
            output_dir.mkdir(parents=True, exist_ok=True)
            txt_output = (output_dir, build_txt_targets(self.db.list_items(task_id, limit=10000)))

        settings = self.store.read_settings()
        gate = ProviderGate(
            settings.get("provider_limits") or {}, DEFAULT_PROVIDER_CONCURRENCY,
        )
        items = self.db.pending_items(task_id)
        cursor = {"index": 0}
        cursor_lock = threading.Lock()
        usage_lock = threading.Lock()
        total_usage: dict[str, Any] = dict(task.get("usage") or {})

        def take_item() -> dict[str, Any] | None:
            with cursor_lock:
                if cancel_event.is_set() or cursor["index"] >= len(items):
                    return None
                row = items[cursor["index"]]
                cursor["index"] += 1
                return row

        def lane(slot: int) -> None:
            engine = engine_cls(self.app, config, event_sink=event_bridge)
            while True:
                item = take_item()
                if item is None:
                    return
                try:
                    item_usage = self._run_concurrent_item(
                        task_id=task_id, item=item, config=config, preset=preset,
                        prompt=base_prompt, engine=engine, profile=worker_profiles[slot],
                        supervisor_profile=supervisor_profile,
                        fingerprint=fingerprint, retry_limit=retry_limit,
                        timeout_seconds=timeout_seconds, cancel_event=cancel_event,
                        txt_output=txt_output, gate=gate, worker_slot=slot,
                        worker_ref=workers[slot], supervisor_ref=supervisor_ref,
                    )
                except Exception as exc:  # noqa: BLE001 - 单图异常不拖垮整条泳道
                    traceback.print_exc()
                    item_usage = {}
                    try:
                        self.db.fail_item(
                            task_id, item["item_id"], error=str(exc),
                            category=classify_error(exc),
                            meta={
                                "worker_slot": slot,
                                "worker_model_key": str(workers[slot].get("model_key") or ""),
                                "worker_model_name": str(workers[slot].get("model_name") or ""),
                                "phase": "failed",
                            },
                        )
                    except Exception:  # noqa: BLE001 - 收尾失败也不能停泳道
                        traceback.print_exc()
                if item_usage:
                    with usage_lock:
                        merged = aggregate_usage(total_usage, item_usage)
                        total_usage.clear()
                        total_usage.update(merged)
                    writer.set_usage(total_usage)
                writer.tick()

        pool = ThreadPoolExecutor(
            max_workers=min(SUPERVISOR_MAX_CONCURRENCY, max(1, len(workers))),
            thread_name_prefix="naiba-labeler-supervisor",
        )
        self._supervisor_pool = pool  # 测试可观察；任务结束即 shutdown
        threads = [
            threading.Thread(target=lane, args=(index,),
                             name=f"naiba-labeler-lane-{index}", daemon=True)
            for index in range(len(workers))
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            pool.shutdown(wait=True)
        finally:
            self._supervisor_pool = None
            try:
                pool.shutdown(wait=False)
            except Exception:  # noqa: BLE001 - 已 shutdown 时忽略
                pass
        if cancel_event.is_set():
            self.db.cancel_pending(task_id)
        counts = self.db.counts(task_id)
        annotation_ids = self.db.annotation_ids_for_task(task_id)
        self._finish_from_counts(task_id, counts, annotation_ids, total_usage)

    def _run_concurrent_item(self, *, task_id: str, item: dict[str, Any],
                             config: dict[str, Any], preset: dict[str, Any],
                             prompt: str, engine: Any, profile: dict[str, Any],
                             supervisor_profile: dict[str, Any], fingerprint: str,
                             retry_limit: int, timeout_seconds: int,
                             cancel_event: threading.Event,
                             txt_output: tuple[Any, dict[str, str]] | None,
                             gate: ProviderGate, worker_slot: int,
                             worker_ref: dict[str, str],
                             supervisor_ref: dict[str, str]) -> dict[str, Any]:
        """单图的并发处理：打标 → 监督 → 通过/退回重做 → 人工检查。"""
        item_id = item["item_id"]
        writer = self._build_txt_writer(task_id, item, txt_output, staged=True)
        worker_meta = {
            "worker_slot": worker_slot,
            "worker_model_key": str(worker_ref.get("model_key") or ""),
            "worker_model_name": str(worker_ref.get("model_name") or ""),
        }
        # 同图同配置（含打标员集合/监督模型/监督策略版本）已有通过结果：直接复用。
        reusable = self.db.find_reusable(item["path_key"], item["content_version"], fingerprint)
        if reusable is not None and (writer is None or isinstance(reusable.get(LABELER_APPROVED_TXT_KEY), str)):
            annotation = self._annotation_from_reusable(reusable, item, config)
            original_key = str(annotation.get("model_key") or "")
            original_name = str(annotation.get("model_name") or "")
            matching_slots = [index for index, ref in enumerate(config.get("workers") or [])
                              if ref.get("model_key") == original_key and ref.get("model_name") == original_name]
            worker_meta = {
                "worker_slot": matching_slots[0] if len(matching_slots) == 1 else LABELER_UNKNOWN_WORKER_SLOT,
                "worker_model_key": original_key, "worker_model_name": original_name,
            }
            if writer is not None:
                writer.write_fallback(reusable[LABELER_APPROVED_TXT_KEY])
            if not self._commit_approved(task_id, item_id, annotation, reusable.get("usage") or {},
                                         fingerprint, {**worker_meta, "supervisor_decision": "reused", "phase": "approved"},
                                         writer, cancel_event):
                return {}
            self.store.append_event(task_id, {
                "type": "image_done", "item_id": item_id, "name": item.get("name") or "",
                "reused": True, "txt_state": writer.state if writer else "",
            })
            return {}

        options, label_prompt = self._build_item_options(
            config, item, prompt, timeout_seconds, cancel_event, writer)
        total_usage: dict[str, Any] = {}
        label_usage: dict[str, Any] = {}
        supervisor_usage: dict[str, Any] = {}
        revisions = 0
        attempt = 0
        current_prompt = label_prompt
        while True:
            if cancel_event.is_set():
                if writer is not None:
                    writer.reset()
                self.db.cancel_item(task_id, item_id, usage=total_usage, supervisor_usage=supervisor_usage)
                return total_usage
            attempt_started = False
            try:
                # 闸门等待也会取消；尚未调用本图模型时不能借到上一张图的读数。
                self._reset_usage(self.app.models)
                with gate.slot(str(worker_ref.get("model_key") or ""), cancel_event):
                    attempt_started = True
                    annotation, usage, finish_reason = self._label_once(
                        task_id=task_id, item=item, config=config, preset=preset,
                        prompt=current_prompt, engine=engine, profile=profile,
                        options=options, cancel_event=cancel_event, writer=writer,
                        model_ref=worker_ref, worker_slot=worker_slot,
                    )
            except Exception as exc:  # noqa: BLE001 - 分类后决定重试或记失败
                category = classify_error(exc)
                captured = getattr(exc, "labeler_attempt_usage", None)
                failure_usage = dict(captured) if isinstance(captured, dict) else (
                    {} if not attempt_started or category in {"auth", "bad_request", "image"} else
                    dict(getattr(self.app.models, "last_usage", {}) or {}))
                total_usage = aggregate_usage(total_usage, failure_usage)
                if category == "cancelled":
                    if writer is not None:
                        writer.reset()
                    self.db.cancel_item(task_id, item_id, usage=total_usage, supervisor_usage=supervisor_usage)
                    cancel_event.set()
                    return total_usage
                attempt += 1
                if category == "transient" and attempt <= retry_limit:
                    delay = min(MAX_RETRY_BACKOFF, 0.5 * (2 ** (attempt - 1)))
                    if cancel_event.wait(delay):
                        if writer is not None:
                            writer.reset()
                        self.db.cancel_item(task_id, item_id, usage=total_usage, supervisor_usage=supervisor_usage)
                        return total_usage
                    continue
                merged = total_usage
                self.db.fail_item(
                    task_id, item_id, error=str(exc), category=category, usage=merged,
                    meta={**worker_meta, "phase": "failed"},
                )
                self.store.append_event(task_id, {
                    "type": "image_failed", "item_id": item_id,
                    "name": item.get("name") or "", "error": str(exc)[:500],
                    "category": category,
                })
                if writer is not None:
                    writer.reset()
                return merged
            attempt = 0
            label_usage = aggregate_usage(label_usage, usage)
            total_usage = aggregate_usage(total_usage, usage)

            # ---- 监督：先落阶段，再送审；等待期间取消也按实际结果收敛 ----
            self.db.set_phase(
                task_id, item_id, "supervising", state="supervising",
                supervisor_attempts=revisions, supervisor_usage=supervisor_usage,
            )
            self.store.append_event(task_id, {
                "type": "image_supervising", "item_id": item_id,
                "name": item.get("name") or "",
            })
            try:
                candidate = dict(annotation)
                if writer is not None:
                    candidate["txt_content"] = writer.candidate_text()
                    if not writer.wrote or writer.state == "failed":
                        raise SupervisionFormatError("最终 TXT 未完整生成，不能批准")
                verdict, verdict_usage = self._supervise_with_retry(
                    task_id=task_id, item=item, config=config,
                    annotation=candidate, supervisor_profile=supervisor_profile,
                    supervisor_ref=supervisor_ref, worker_slot=worker_slot,
                    worker_model=str(worker_ref.get("model_name") or ""),
                    cancel_event=cancel_event, gate=gate,
                    timeout_seconds=timeout_seconds,
                )
            except SupervisionFormatError as exc:
                failed_usage = dict(getattr(exc, "supervisor_usage", {}) or {})
                total_usage = aggregate_usage(total_usage, failed_usage)
                supervisor_usage = aggregate_usage(supervisor_usage, failed_usage)
                if writer is not None:
                    writer.reset()
                if not self._settle_unapproved(
                    task_id, item_id, candidate=candidate, cancel_event=cancel_event,
                    reason=f"监督结果无法解析：{exc}", usage=total_usage,
                    meta={**worker_meta, "supervisor_attempts": revisions,
                          "supervisor_usage": supervisor_usage},
                ):
                    return total_usage
                self.store.append_event(task_id, {
                    "type": "image_human_check", "item_id": item_id,
                    "name": item.get("name") or "", "reason": str(exc)[:500],
                })
                return total_usage
            except Exception as exc:  # noqa: BLE001 - 监督调用失败
                category = classify_error(exc)
                failure_usage = dict(getattr(exc, "supervisor_usage", {}) or {})
                total_usage = aggregate_usage(total_usage, failure_usage)
                supervisor_usage = aggregate_usage(supervisor_usage, failure_usage)
                if category == "cancelled":
                    if writer is not None:
                        writer.reset()
                    self.db.cancel_item(task_id, item_id, usage=total_usage, supervisor_usage=supervisor_usage)
                    cancel_event.set()
                    return total_usage
                merged = total_usage
                if writer is not None:
                    writer.reset()
                if not self._settle_unapproved(
                    task_id, item_id, reason=f"监督失败：{exc}", cancel_event=cancel_event,
                    category=f"supervisor_{category}", usage=merged,
                    meta={**worker_meta, "supervisor_attempts": revisions,
                          "supervisor_usage": supervisor_usage},
                    candidate=candidate,
                ):
                    return merged
                self.store.append_event(task_id, {
                    "type": "image_failed", "item_id": item_id,
                    "name": item.get("name") or "",
                    "error": f"监督失败：{str(exc)[:400]}",
                    "category": f"supervisor_{category}",
                })
                return merged
            supervisor_usage = aggregate_usage(supervisor_usage, verdict_usage)
            total_usage = aggregate_usage(total_usage, verdict_usage)
            decision = str(verdict.get("decision") or "")
            reason = str(verdict.get("reason") or "")
            self.db.set_phase(
                task_id, item_id, "supervising", supervisor_decision=decision,
                supervisor_reason=reason, supervisor_attempts=revisions,
                supervisor_usage=supervisor_usage,
            )
            if decision == "approve":
                if writer is not None:
                    annotation[LABELER_APPROVED_TXT_KEY] = writer.candidate_text()
                approved_meta = {
                        **worker_meta, "phase": "approved",
                        "supervisor_decision": "approve",
                        "supervisor_attempts": revisions,
                        "supervisor_reason": reason,
                        "supervisor_usage": supervisor_usage,
                    }
                try:
                    committed = self._commit_approved(task_id, item_id, annotation, total_usage,
                                                      fingerprint, approved_meta, writer, cancel_event)
                except Exception as exc:
                    if not self._settle_unapproved(task_id, item_id, reason=f"批准产物发布失败：{exc}",
                                      category="publish_failed", usage=total_usage,
                                      meta=approved_meta, candidate=candidate, cancel_event=cancel_event):
                        return total_usage
                    self.store.append_event(task_id, {"type": "image_failed", "item_id": item_id,
                                                       "error": "批准产物发布失败", "category": "publish_failed"})
                    return total_usage
                if not committed:
                    return total_usage
                if total_usage:
                    self.store.append_event(task_id, {"type": "usage", "usage": total_usage})
                self.store.append_event(task_id, {
                    "type": "image_done", "item_id": item_id,
                    "name": item.get("name") or "",
                    "tags": len(annotation.get("tags") or []),
                    "finish_reason": finish_reason,
                    "txt_state": writer.state if writer is not None else "",
                })
                return total_usage
            # ---- 退回：超过上限转人工检查；否则原打标员重做 ----
            if revisions >= MAX_REVISIONS:
                if writer is not None:
                    writer.reset()
                note = "监督连续退回，已达重做上限（3 次打标尝试）"
                if reason:
                    note += f"：{reason}"
                if not self._settle_unapproved(
                    task_id, item_id, candidate=candidate, reason=note, cancel_event=cancel_event,
                    usage=total_usage,
                    meta={**worker_meta, "supervisor_attempts": revisions,
                          "supervisor_usage": supervisor_usage},
                ):
                    return total_usage
                self.store.append_event(task_id, {
                    "type": "image_human_check", "item_id": item_id,
                    "name": item.get("name") or "", "reason": note[:500],
                })
                return total_usage
            revisions += 1
            note = revise_note(verdict)
            self.db.set_phase(
                task_id, item_id, "revising", state="revising",
                supervisor_decision="revise", supervisor_reason=reason,
                supervisor_attempts=revisions, supervisor_usage=supervisor_usage,
            )
            self.store.append_event(task_id, {
                "type": "image_revised", "item_id": item_id,
                "name": item.get("name") or "", "attempt": revisions,
                "reason": reason[:500],
            })
            current_prompt = label_prompt
            if note:
                current_prompt = (
                    label_prompt
                    + "\n\n上一轮监督退回意见（请按此重做，仍按原规则输出）：" + note
                )

    def _settle_unapproved(self, task_id: str, item_id: str, *, candidate: dict[str, Any],
                           reason: str, usage: dict[str, Any], meta: dict[str, Any],
                           cancel_event: threading.Event, category: str = "") -> bool:
        # 所有迟到裁定都服从已受理取消，不只保护 approve 分支。
        with self._registry_lock:
            if cancel_event.is_set():
                self.db.cancel_item(task_id, item_id, usage=usage,
                                    supervisor_usage=meta.get("supervisor_usage"))
                return False
            if category:
                self.db.fail_item(task_id, item_id, error=reason, category=category,
                                  usage=usage, meta=meta, candidate=candidate)
            else:
                self.db.human_check_item(task_id, item_id, candidate=candidate,
                                         reason=reason, usage=usage, meta=meta)
        return True

    def _commit_approved(self, task_id: str, item_id: str, annotation: dict[str, Any],
                         usage: dict[str, Any], fingerprint: str, meta: dict[str, Any],
                         writer: Any, cancel_event: threading.Event) -> bool:
        # 与 cancel 共用窄锁：取消已受理后，迟到批准不能发布 TXT 或成功标注。
        with self._registry_lock:
            if cancel_event.is_set():
                if writer is not None:
                    writer.reset()
                self.db.cancel_item(task_id, item_id, usage=usage,
                                    supervisor_usage=meta.get("supervisor_usage"))
                return False
            try:
                if writer is not None:
                    writer.publish()
                self.db.complete_item(task_id, item_id, annotation=annotation, usage=usage,
                                      config_fingerprint=fingerprint, meta=meta)
            except Exception:
                if writer is not None:
                    writer.reset()
                raise
        return True

    def _supervise_with_retry(self, *, task_id: str, item: dict[str, Any],
                              config: dict[str, Any], annotation: dict[str, Any],
                              supervisor_profile: dict[str, Any],
                              supervisor_ref: dict[str, str], worker_slot: int,
                              worker_model: str, cancel_event: threading.Event,
                              gate: ProviderGate,
                              timeout_seconds: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """把监督调用投递到独立有界队列并等待结果；瞬时错误有限退避重试。"""
        pool = getattr(self, "_supervisor_pool", None)
        last_exc: BaseException | None = None
        total_usage: dict[str, Any] = {}
        for attempt in range(SUPERVISOR_RETRY_LIMIT + 1):
            if cancel_event.is_set():
                cancelled = TaskCancelled("任务已取消")
                cancelled.supervisor_usage = dict(total_usage)
                raise cancelled
            request = lambda: self._supervisor_request(  # noqa: E731 - 单次投递
                task_id=task_id, item=item, config=config, annotation=annotation,
                supervisor_profile=supervisor_profile, supervisor_ref=supervisor_ref,
                worker_slot=worker_slot, worker_model=worker_model,
                cancel_event=cancel_event, gate=gate,
                timeout_seconds=timeout_seconds,
            )
            future = pool.submit(request) if pool is not None else None
            try:
                text, usage = future.result() if future is not None else request()
                total_usage = aggregate_usage(total_usage, usage)
                if cancel_event.is_set():
                    raise TaskCancelled("任务已取消")
                try:
                    verdict = parse_verdict(text)
                except SupervisionFormatError as exc:
                    exc.supervisor_usage = total_usage
                    raise
            except BaseException as exc:  # noqa: BLE001 - 分类后决定重试或抛出
                last_exc = exc
                if not isinstance(exc, SupervisionFormatError):
                    total_usage = aggregate_usage(total_usage, getattr(exc, "supervisor_usage", {}) or {})
                exc.supervisor_usage = dict(total_usage)
                if cancel_event.is_set():
                    cancelled = TaskCancelled("任务已取消")
                    cancelled.supervisor_usage = dict(total_usage)
                    raise cancelled from exc
                if classify_error(exc) == "transient" and attempt < SUPERVISOR_RETRY_LIMIT:
                    delay = min(MAX_RETRY_BACKOFF, 0.5 * (2 ** attempt))
                    if cancel_event.wait(delay):
                        cancelled = TaskCancelled("任务已取消")
                        cancelled.supervisor_usage = dict(total_usage)
                        raise cancelled from exc
                    continue
                raise
            return verdict, total_usage
        raise last_exc or RuntimeError("监督调用失败")

    def _supervisor_request(self, *, task_id: str, item: dict[str, Any],
                            config: dict[str, Any], annotation: dict[str, Any],
                            supervisor_profile: dict[str, Any],
                            supervisor_ref: dict[str, str], worker_slot: int,
                            worker_model: str, cancel_event: threading.Event,
                            gate: ProviderGate,
                            timeout_seconds: int) -> tuple[str, dict[str, Any]]:
        """在线程内发起一次监督请求并**当场**读用量（runtime 读数是线程局部的）。"""
        prompt = build_supervision_prompt(
            config, annotation, slot_index=worker_slot, worker_model=worker_model,
        )
        messages, error = build_supervision_messages(item["path"], prompt)
        if error:
            raise ValueError(error)
        options = {
            "stream": True,
            "cancel_event": cancel_event,
            "lock_label": "打标器监督",
            "stream_total_timeout_seconds": timeout_seconds,
            "request_timeout_seconds": timeout_seconds,
            **(self.app.config.runtime_guard_options() or {}),
        }
        with gate.slot(str(supervisor_ref.get("model_key") or ""), cancel_event):
            self._reset_usage(self.app.models)
            try:
                text = str(self.app.models.complete(
                    supervisor_profile, messages, options, None,
                ) or "")
            except BaseException as exc:  # noqa: BLE001 - 把本线程用量随异常带回
                # runtime 的 last_usage 是线程局部的：异常必须在本线程把读数取走，
                # 不能让泳道线程事后跨线程读（那会拿到别人的/默认的读数）。
                try:
                    exc.supervisor_usage = dict(getattr(self.app.models, "last_usage", {}) or {})
                except Exception:  # noqa: BLE001 - 附加失败不影响原始异常
                    pass
                raise
        usage = dict(getattr(self.app.models, "last_usage", {}) or {})
        return text, usage

    def _run_item(
        self,
        *,
        task_id: str,
        item: dict[str, Any],
        config: dict[str, Any],
        preset: dict[str, Any],
        prompt: str,
        engine: Any,
        profile: dict[str, Any],
        fingerprint: str,
        retry_limit: int,
        timeout_seconds: int,
        cancel_event: threading.Event,
        txt_output: tuple[Any, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        item_id = item["item_id"]
        writer = self._build_txt_writer(task_id, item, txt_output)
        # 同图同内容同配置已有成功结果：直接复用，不重复调用（也不产生费用）。
        reusable = self.db.find_reusable(item["path_key"], item["content_version"], fingerprint)
        if reusable is not None:
            annotation = self._annotation_from_reusable(reusable, item, config)
            if writer is not None:
                # 复用的结果也要有同名 TXT：内容就是这条已保存的标注。
                writer.write_fallback(caption_text(annotation))
            self.db.complete_item(
                task_id, item_id,
                annotation=annotation,
                usage=reusable.get("usage") or {},
                config_fingerprint=fingerprint,
            )
            self.store.append_event(task_id, {
                "type": "image_done", "item_id": item_id, "name": item.get("name") or "",
                "reused": True, "txt_state": writer.state if writer else "",
            })
            return {}

        options, prompt = self._build_item_options(
            config, item, prompt, timeout_seconds, cancel_event, writer)
        attempt = 0
        while True:
            if cancel_event.is_set():
                self.db.cancel_item(task_id, item_id)
                return {}
            started = time.monotonic()
            try:
                annotation, usage, finish_reason = self._label_once(
                    task_id=task_id, item=item, config=config, preset=preset,
                    prompt=prompt, engine=engine, profile=profile, options=options,
                    cancel_event=cancel_event, writer=writer,
                )
                self.db.complete_item(
                    task_id, item_id,
                    annotation=annotation,
                    usage=usage,
                    config_fingerprint=fingerprint,
                )
                if usage:
                    self.store.append_event(task_id, {"type": "usage", "usage": usage})
                self.store.append_event(task_id, {
                    "type": "image_done", "item_id": item_id, "name": item.get("name") or "",
                    "tags": len(annotation.get("tags") or []), "finish_reason": finish_reason,
                    "txt_state": writer.state if writer is not None else "",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                })
                return usage
            except Exception as exc:  # noqa: BLE001 - 分类后决定重试或记失败
                category = classify_error(exc)
                if category == "cancelled":
                    self.db.cancel_item(task_id, item_id)
                    cancel_event.set()
                    return {}
                attempt += 1
                if category == "transient" and attempt <= retry_limit:
                    delay = min(MAX_RETRY_BACKOFF, 0.5 * (2 ** (attempt - 1)))
                    if cancel_event.wait(delay):
                        self.db.cancel_item(task_id, item_id)
                        return {}
                    continue
                # 认证/参数错误不重试：重试只会继续失败并可能反复计费。
                failure_usage = {} if category in {"auth", "bad_request", "image"} else (
                    dict(getattr(self.app.models, "last_usage", {}) or {})
                )
                self.db.fail_item(
                    task_id, item_id, error=str(exc), category=category, usage=failure_usage,
                )
                self.store.append_event(task_id, {
                    "type": "image_failed", "item_id": item_id, "name": item.get("name") or "",
                    "error": str(exc)[:500], "category": category,
                })
                return {}

    def _build_item_options(self, config: dict[str, Any], item: dict[str, Any],
                            prompt: str, timeout_seconds: int,
                            cancel_event: threading.Event, writer: Any) -> tuple[dict[str, Any], str]:
        """单图调用选项与（按需追加图片标识的）提示词，串行/并发两条路径共用。"""
        item_id = str(item.get("item_id") or "")
        options = {
            "stream": True,
            "cancel_event": cancel_event,
            "lock_label": "打标器",
            "stream_total_timeout_seconds": timeout_seconds,
            "request_timeout_seconds": timeout_seconds,
            **(self.app.config.runtime_guard_options() or {}),
        }
        if str(config.get("engine") or "") == "chat":
            # chat 引擎的事件桥需要图片上下文（事件带图片名，逐图可读）。
            options["item_id"] = item_id
            options["item_name"] = str(item.get("name") or "")
        if writer is not None:
            # 两类引擎共用同一个写入器：provider 的单轮工具循环与 chat 的
            # SkillAgent 工具循环都从 options["txt_tool"] 取它。
            options["txt_tool"] = writer
            # 模型需要知道这张图的标识，才能把 item 参数填对（否则只能在批量里猜）。
            label = str(item.get("relative_path") or item.get("name") or "")
            if label:
                prompt = (
                    f"{prompt}\n\n当前图片：{label}"
                    f"（调用 {TXT_TOOL_NAME} 时 item 填这个）"
                )
        return options, prompt

    def _label_once(self, *, task_id: str, item: dict[str, Any], config: dict[str, Any],
                    preset: dict[str, Any], prompt: str, engine: Any,
                    profile: dict[str, Any], options: dict[str, Any],
                    cancel_event: threading.Event, writer: Any,
                    model_ref: dict[str, str] | None = None,
                    worker_slot: int = 0) -> tuple[dict[str, Any], dict[str, Any], str]:
        """一次打标尝试：mark_running → 调模型 → 解析/过滤/触发词 → 返回标注草稿。

        不写标注库：串行路径由 ``_run_item`` 直接提交，并发路径先送监督再决定。
        """
        item_id = item["item_id"]
        self._reset_usage(self.app.models)
        self._reset_usage(engine)
        self._validate_live_skill_rules(config)
        if writer is not None:
            # 重试会从零重来：清掉上一次尝试写了一半的 TXT，避免分段重复。
            writer.reset()
        ref = model_ref or {
            "model_key": str(config.get("model_key") or ""),
            "model_name": str(config.get("model_name") or ""),
        }
        self.db.mark_running(
            task_id, item_id, phase="labeling", worker_slot=worker_slot,
            worker_model_key=str(ref.get("model_key") or ""),
            worker_model_name=str(ref.get("model_name") or ""),
        )
        parsed = engine.run(
            profile=profile,
            image_path=item["path"],
            prompt=prompt,
            options=options,
            status=None,
        )
        # 诊断读数必须在**本线程**内立刻取（P0-a）。chat 引擎的多轮循环
        # 用量在 SkillAgent.run 返回值里汇总（engine.last_usage），比
        # last_usage（只反映最后一次调用）更真实；provider 引擎维持原口径。
        engine_usage = getattr(engine, "last_usage", None)
        usage = dict(engine_usage) if isinstance(engine_usage, dict) and engine_usage \
            else dict(getattr(self.app.models, "last_usage", {}) or {})
        try:
            self._validate_live_skill_rules(config)
        except Exception as exc:
            # 规则漂移拒收产物，也不能丢掉 Skill 多轮执行已经产生的用量。
            exc.labeler_attempt_usage = usage
            raise
        finish_reason = str(getattr(self.app.models, "last_finish_reason", "") or "")
        tags, confidences = self._finalize_tags(parsed, config, preset)
        description = self._finalize_description(parsed, config, preset)
        # 触发词前缀在置信度/预设过滤与格式化之后统一应用：不参与过滤，
        # 标签列表放首位（置信度补 -1 保持与标签按位对齐），描述放最前。
        trigger = self._trigger_word(config)
        if trigger:
            if tags:
                tags, confidences = self._apply_trigger_to_tags(trigger, tags, confidences)
            if description:
                description = self._apply_trigger_to_description(trigger, description, config)
        annotation = {
            "path_key": item["path_key"],
            "content_version": item["content_version"],
            "path": item["path"],
            "relative_path": item.get("relative_path") or "",
            "name": item.get("name") or "",
            "original_sha256": str(item.get("original_sha256") or ""),
            "engine": str(config.get("engine") or "provider"),
            "output_mode": str(config.get("output_mode") or "keywords"),
            "preset": str(config.get("preset") or ""),
            "prompt_version": int(config.get("prompt_version") or 1),
            "model_key": str(ref.get("model_key") or ""),
            "model_name": str(ref.get("model_name") or ""),
            "tags": tags,
            "confidences": confidences,
            "description": description,
        }
        if writer is not None:
            # 模型一次工具都没调用（或不支持工具）时回填一份，保证每张成功图片
            # 都有同名 TXT；工具写过的内容优先，不被标注文本覆盖。
            writer.write_fallback(caption_text(annotation))
        return annotation, usage, finish_reason

    def _build_txt_writer(self, task_id: str, item: dict[str, Any],
                          txt_output: tuple[Any, dict[str, str]] | None, *,
                          staged: bool = False) -> TxtToolContext | None:
        """按任务开关构造当前图片的 TXT 写入器；关闭时返回 None（零行为变化）。"""
        if txt_output is None:
            return None
        output_dir, targets = txt_output
        item_id = str(item.get("item_id") or "")

        def record(state: str, path: str, chars: int) -> None:
            self.db.mark_txt(task_id, item_id, path=path, state=state, chars=chars)

        def event(message: str) -> None:
            self.store.append_event(task_id, {"type": "status", "message": message})

        return TxtToolContext(
            task_id=task_id,
            output_dir=output_dir,
            item=item,
            relative=targets.get(item_id, ""),
            on_record=record,
            on_event=event,
            staged=staged,
        )

    @staticmethod
    def _reset_usage(target: Any) -> None:
        # runtime 在线程复用时不会在异常出口清 last_usage，不能继承上一张图读数。
        try:
            target.last_usage = {}
        except (AttributeError, TypeError):
            pass

    def _validate_live_skill_rules(self, config: dict[str, Any]) -> None:
        # SkillAgent 按需读取目录；排队或执行期间发生可观察变更必须阻断，
        # 不让新规则产物交给旧规则监督。串行链路保持原有行为。
        if config.get("concurrency_enabled") and str(config.get("engine") or "") == "chat":
            if freeze_supervision_rules(self.app, config) != config.get(LABELER_SUPERVISION_RULES_KEY):
                raise ValueError("Skill 规则已改变，未继续执行或采纳结果，请按当前规则新建任务")

    @staticmethod
    def _trigger_word(config: dict[str, Any]) -> str:
        """配置里启用的触发词；未启用或没内容返回空串。"""
        trigger = config.get("trigger_word")
        if not isinstance(trigger, dict) or not trigger.get("enabled"):
            return ""
        return str(trigger.get("value") or "").strip()

    @staticmethod
    def _apply_trigger_to_tags(word: str, tags: list[str],
                               confidences: list[float]) -> tuple[list[str], list[float]]:
        """触发词作为一条完整标签放在列表首位；首标签已是它时不重复加。

        置信度补 -1.0（模型没给）：标签与置信度必须按位对齐。
        """
        if tags and normalize_tag(tags[0]) == normalize_tag(word):
            return tags, confidences
        return [word, *tags], [-1.0, *confidences]

    @staticmethod
    def _apply_trigger_to_description(word: str, text: str, config: dict[str, Any]) -> str:
        """触发词放描述最前，分隔符跟输出语言（英文逗号 + 空格 / 中文逗号）。"""
        text = str(text or "").strip()
        if not text or text.casefold().startswith(word.casefold()):
            return text
        separator = ", " if str(config.get("language") or "") == "en" else "，"
        return f"{word}{separator}{text}"

    @staticmethod
    def _finalize_tags(parsed: dict[str, Any], config: dict[str, Any],
                       preset: dict[str, Any], item: dict[str, Any] | None = None) -> tuple[list[str], list[float]]:
        """预设过滤 → 置信度门槛 → 数量截断。``conf == -1`` 表示模型没给置信度。"""
        threshold = float(config.get("confidence_threshold") or 0.0)
        max_tags = max(1, min(64, int(config.get("max_tags") or 24)))
        subject_filter = config.get("subject_filter") or {}
        extra_keywords: list[str] = []
        strength = ""
        if isinstance(subject_filter, dict) and subject_filter.get("enabled"):
            extra_keywords = [str(word) for word in (subject_filter.get("keywords") or [])]
            strength = str(subject_filter.get("strength") or "")
        raw_tags = list(parsed.get("tags") or [])
        raw_conf = list(parsed.get("confidences") or [])
        # 置信度与标签按位置配对；模型少给/多给都不越界。
        paired = list(zip(raw_tags, raw_conf + [-1.0] * max(0, len(raw_tags) - len(raw_conf))))
        kept = [(str(tag), float(conf)) for tag, conf in paired]
        kept = [(tag, conf) for tag, conf in kept if not (0 <= conf < threshold)]
        filtered = filter_tags([tag for tag, _ in kept], preset=preset,
                               strength=strength, extra_keywords=extra_keywords)
        allowed = {tag.lower() for tag in filtered}
        out_tags: list[str] = []
        out_conf: list[float] = []
        seen: set[str] = set()
        for tag, conf in kept:
            key = tag.lower()
            if key not in allowed or key in seen:
                continue
            seen.add(key)
            out_tags.append(tag)
            out_conf.append(conf)
            if len(out_tags) >= max_tags:
                break
        _ = item
        return out_tags, out_conf

    @staticmethod
    def _finalize_description(parsed: dict[str, Any], config: dict[str, Any],
                              preset: dict[str, Any]) -> str:
        """词组短语模式：按生成模式的忽略词逐个丢短语，其余原样保留。

        自然语言描述不做规则拆句过滤（会切坏句子），靠提示词约束；
        词组短语本身就是逗号分隔，能安全地逐项过滤。
        """
        text = str(parsed.get("description") or "").strip()
        if str(config.get("output_mode") or "") != "phrases" or not text:
            return text
        subject_filter = config.get("subject_filter") or {}
        extra_keywords: list[str] = []
        strength = ""
        if isinstance(subject_filter, dict) and subject_filter.get("enabled"):
            extra_keywords = [str(word) for word in (subject_filter.get("keywords") or [])]
            strength = str(subject_filter.get("strength") or "")
        pieces = [piece.strip() for piece in re.split(r"[,，、;；\n]+", text) if piece.strip()]
        if not pieces:
            return ""
        allowed = set(filter_tags(pieces, preset=preset, strength=strength,
                                  extra_keywords=extra_keywords))
        out: list[str] = []
        seen: set[str] = set()
        for piece in pieces:
            key = normalize_tag(piece)
            if key not in allowed or key in seen:
                continue
            seen.add(key)
            out.append(piece)
        # 描述语言决定分隔符：英文短语用英文逗号，中文（含中英混合的默认描述）用中文逗号。
        separator = ", " if str(config.get("language") or "") == "en" else "，"
        return separator.join(out)

    @staticmethod
    def _annotation_from_reusable(reusable: dict[str, Any], item: dict[str, Any],
                                  config: dict[str, Any]) -> dict[str, Any]:
        return {
            "path_key": item["path_key"],
            "content_version": item["content_version"],
            "path": item["path"],
            "relative_path": item.get("relative_path") or "",
            "name": item.get("name") or "",
            "original_sha256": str(item.get("original_sha256") or ""),
            "engine": reusable.get("engine") or str(config.get("engine") or "provider"),
            "output_mode": reusable.get("output_mode") or str(config.get("output_mode") or "keywords"),
            "preset": reusable.get("preset") or str(config.get("preset") or ""),
            "prompt_version": int(reusable.get("prompt_version") or 1),
            "model_key": reusable.get("model_key") or str(config.get("model_key") or ""),
            "model_name": reusable.get("model_name") or str(config.get("model_name") or ""),
            "tags": reusable.get("tags") or [],
            "confidences": reusable.get("confidences") or [],
            "description": reusable.get("description") or "",
            LABELER_APPROVED_TXT_KEY: reusable.get(LABELER_APPROVED_TXT_KEY),
        }

    # ---- 终态 ----

    def _yield_to_chat(self, cancel_event: threading.Event) -> None:
        """本地后端有聊天/工作台在跑时先等它结束：批量打标不能把对话卡在排队里。

        只读查询（``list_primary(active_only=True)``）；查询失败按「没有人在跑」处理，
        绝不让这个礼让逻辑本身挡住打标。取消信号全程有效（0.5s 轮询）。
        """
        runs = getattr(self.app, "runs", None)
        if runs is None or not callable(getattr(runs, "list_primary", None)):
            return
        while not cancel_event.is_set():
            try:
                # 真实签名是 list_primary(conversation_id="", active_only=False)：
                # 不传 conversation_id = 查全部会话的顶层 Run。
                active = runs.list_primary(active_only=True)
            except Exception:  # noqa: BLE001 - 礼让失败不影响打标
                return
            if not active:
                return
            cancel_event.wait(0.5)

    @staticmethod
    def _progress_from_counts(counts: dict[str, int]) -> dict[str, int]:
        return {
            "done": int(counts.get("done") or 0),
            "total": int(counts.get("total") or 0),
            "failed": int(counts.get("failed") or 0),
            "uncertain": int(counts.get("uncertain") or 0),
            "cancelled": int(counts.get("cancelled") or 0),
            "human_check": int(counts.get("human_check") or 0),
        }

    def _finish_from_counts(self, task_id: str, counts: dict[str, int],
                            annotation_ids: list[int], usage: dict[str, Any]) -> None:
        total = int(counts.get("total") or 0)
        done = int(counts.get("done") or 0)
        failed = int(counts.get("failed") or 0) + int(counts.get("uncertain") or 0)
        cancelled = int(counts.get("cancelled") or 0)
        human_check = int(counts.get("human_check") or 0)
        progress = self._progress_from_counts(counts)
        result = {"annotation_ids": annotation_ids}
        if cancelled and done == 0 and failed == 0 and human_check == 0:
            self._finish_task_terminal(
                task_id, "cancelled", {"type": "cancelled"}, detail="任务已取消",
                progress=progress, result=result, usage=usage,
            )
            return
        if total and done == 0 and failed + cancelled + human_check == total:
            # 全部没有通过：监督退回/人工检查不是「失败」，但也不能报完成。
            message = "全部图片未通过监督审核" if human_check and not failed else "全部图片打标失败"
            self._finish_task_terminal(
                task_id, "failed", {"type": "error", "message": message},
                error=message, progress=progress, result=result, usage=usage,
            )
            return
        detail = ""
        if cancelled:
            detail = f"已取消，完成 {done} 张"
        elif failed or human_check:
            bits = [f"完成 {done} 张"]
            if failed:
                bits.append(f"失败或结果不确定 {failed} 张")
            if human_check:
                bits.append(f"需人工检查 {human_check} 张")
            detail = "，".join(bits)
        self._finish_task_terminal(
            task_id, "done", {"type": "done"}, detail=detail,
            progress=progress, result=result, usage=usage,
        )

    def _finish_task_terminal(
        self,
        task_id: str,
        status: str,
        terminal_payload: dict[str, Any],
        *,
        detail: str = "",
        progress: dict[str, int] | None = None,
        result: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            return
        # 终态防降级：已取消/已失败的任务不再被 worker 的收尾写成 done。
        if status == "done" and str(task.get("status") or "") in {"cancelled", "failed", "interrupted"}:
            return
        try:
            self.store.append_event(task_id, terminal_payload)
        except Exception:  # noqa: BLE001 - 事件失败不影响状态落库
            traceback.print_exc()
        fields: dict[str, Any] = {"status": status, "detail": detail, "finished_at": now_ms()}
        if progress is not None:
            fields["progress"] = progress
        if result is not None:
            fields["result"] = result
        if usage is not None:
            fields["usage"] = usage
        if error:
            fields["error"] = error
        self.store.update_task(task_id, **fields)

    def _fail_task(self, task_id: str, message: str) -> None:
        self.db.cancel_pending(task_id)
        self._finish_task_terminal(
            task_id, "failed", {"type": "error", "message": message}, error=message,
            progress=self._progress_from_counts(self.db.counts(task_id)),
            result={"annotation_ids": self.db.annotation_ids_for_task(task_id)},
        )


class _ProgressWriter:
    """进度写盘节流：任务 JSON 含整批 image_refs，每图都重写代价太大。"""

    def __init__(self, store: Any, db: Any, task_id: str):
        self._store = store
        self._db = db
        self._task_id = task_id
        self._usage: dict[str, Any] = {}
        self._last = 0.0
        # 并发档多条泳道共享同一个 writer：用量与节流时间戳都必须按键/按锁更新。
        self._lock = threading.Lock()

    def set_usage(self, usage: dict[str, Any]) -> None:
        with self._lock:
            self._usage = dict(usage or {})

    def tick(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now - self._last < PROGRESS_WRITE_INTERVAL:
                return
            self._last = now
            usage = dict(self._usage)
        counts = self._db.counts(self._task_id)
        fields: dict[str, Any] = {"progress": LabelerRunner._progress_from_counts(counts)}
        if usage:
            fields["usage"] = usage
        self._store.update_task(self._task_id, **fields)
