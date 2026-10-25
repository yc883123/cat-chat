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
from typing import Any

from naiba.core.exceptions import TaskCancelled
from naiba.llm.profiles import resolve_profile_with_override
from naiba.labeler.engines import get_engine
from naiba.labeler.engines.provider import build_prompt
from naiba.labeler.presets import filter_tags, get_preset, normalize_tag
from naiba.labeler.store import now_ms

# 已验证可用的并发档位。P0-a 只通过了离线契约；在线小样本未执行前不开放 >1。
VALIDATED_CONCURRENCY = (1,)
DEFAULT_CONCURRENCY = 1
PROGRESS_WRITE_INTERVAL = 0.3
MAX_RETRY_BACKOFF = 8.0


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
        input_data = task.get("input") or {}
        config = dict(input_data.get("config") or {})
        cancel_event = threading.Event()
        with self._registry_lock:
            self._cancel_events[task_id] = cancel_event
        writer = _ProgressWriter(self.store, self.db, task_id)
        try:
            self.store.update_task(task_id, status="running", started_at=now_ms(), detail="")
            self.store.append_event(task_id, {"type": "status", "message": "开始打标"})
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
            preset = get_preset(str(config.get("preset") or ""))
            prompt = build_prompt(config, preset)
            engine_cls = get_engine(str(config.get("engine") or "provider"))
            engine = engine_cls(self.app)
            fingerprint = str(config.get("config_fingerprint") or "")
            retry_limit = max(0, min(5, int(config.get("retry_limit") or 0)))
            timeout_seconds = max(5, min(600, int(config.get("timeout_seconds") or 90)))
            total_usage: dict[str, Any] = dict(task.get("usage") or {})
            # 本地后端共用 runtime 的全局执行锁：批量任务不能把聊天挤在门外。
            # 在线 provider 每次调用独立，无需让路。
            is_local = str(profile.get("kind") or "").lower() == "local"

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
        finally:
            cancel_event.set()
            with self._registry_lock:
                self._cancel_events.pop(task_id, None)

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
    ) -> dict[str, Any]:
        item_id = item["item_id"]
        # 同图同内容同配置已有成功结果：直接复用，不重复调用（也不产生费用）。
        reusable = self.db.find_reusable(item["path_key"], item["content_version"], fingerprint)
        if reusable is not None:
            self.db.complete_item(
                task_id, item_id,
                annotation=self._annotation_from_reusable(reusable, item, config),
                usage=reusable.get("usage") or {},
                config_fingerprint=fingerprint,
            )
            self.store.append_event(task_id, {
                "type": "image_done", "item_id": item_id, "name": item.get("name") or "",
                "reused": True,
            })
            return {}

        options = {
            "stream": True,
            "cancel_event": cancel_event,
            "lock_label": "打标器",
            "stream_total_timeout_seconds": timeout_seconds,
            "request_timeout_seconds": timeout_seconds,
            **(self.app.config.runtime_guard_options() or {}),
        }
        attempt = 0
        while True:
            if cancel_event.is_set():
                self.db.cancel_item(task_id, item_id)
                return {}
            self.db.mark_running(task_id, item_id)
            started = time.monotonic()
            try:
                parsed = engine.run(
                    profile=profile,
                    image_path=item["path"],
                    prompt=prompt,
                    options=options,
                    status=None,
                )
                # 诊断读数必须在**本线程**内立刻取（P0-a）。
                usage = dict(getattr(self.app.models, "last_usage", {}) or {})
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
                    "model_key": str(config.get("model_key") or ""),
                    "model_name": str(config.get("model_name") or ""),
                    "tags": tags,
                    "confidences": confidences,
                    "description": description,
                }
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
                    "tags": len(tags), "finish_reason": finish_reason,
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
        }

    def _finish_from_counts(self, task_id: str, counts: dict[str, int],
                            annotation_ids: list[int], usage: dict[str, Any]) -> None:
        total = int(counts.get("total") or 0)
        done = int(counts.get("done") or 0)
        failed = int(counts.get("failed") or 0) + int(counts.get("uncertain") or 0)
        cancelled = int(counts.get("cancelled") or 0)
        progress = self._progress_from_counts(counts)
        result = {"annotation_ids": annotation_ids}
        if cancelled and done == 0 and failed == 0:
            self._finish_task_terminal(
                task_id, "cancelled", {"type": "cancelled"}, detail="任务已取消",
                progress=progress, result=result, usage=usage,
            )
            return
        if total and done == 0 and failed + cancelled == total:
            self._finish_task_terminal(
                task_id, "failed", {"type": "error", "message": "全部图片打标失败"},
                error="全部图片打标失败", progress=progress, result=result, usage=usage,
            )
            return
        detail = ""
        if cancelled:
            detail = f"已取消，完成 {done} 张"
        elif failed:
            detail = f"完成 {done} 张，失败或结果不确定 {failed} 张"
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

    def set_usage(self, usage: dict[str, Any]) -> None:
        self._usage = dict(usage or {})

    def tick(self) -> None:
        now = time.monotonic()
        if now - self._last < PROGRESS_WRITE_INTERVAL:
            return
        self._last = now
        counts = self._db.counts(self._task_id)
        fields: dict[str, Any] = {"progress": LabelerRunner._progress_from_counts(counts)}
        if self._usage:
            fields["usage"] = dict(self._usage)
        self._store.update_task(self._task_id, **fields)
