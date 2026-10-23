"""打标任务管理：提交 / 查询 / 取消 / 继续未完成 / 启动恢复。

提交契约（服务端权威）：
- ``image_refs`` 只接受受管 uploads 里经校验的真实图片（见 ``labeler/images.py``）；
- ``config`` 走白名单，未知字段直接 400；不接受客户端 ``provider_info`` / API Key /
  任意 ``base_url``；
- 提交时校验模型配置可解析（供应商存在、模型名非空），把非秘密配置快照写进任务 JSON；
  云端与本地 provider 都允许（本地后端由 runtime 的全局执行锁串行化）；
  重试沿用同一快照，profile 被删/凭证失效时明确失败，不静默换模型。

恢复契约：
- 启动时活跃任务 → ``interrupted``；DB 里 ``running`` 的逐图项 → ``uncertain``
  （请求已发出但结果没落库，可能已计费）。
- 「继续未完成」总是**新建任务**并记录 ``source_task_id``；成功项不重复调用。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from naiba.llm.profiles import resolve_profile_with_override
from naiba.labeler import LABELER_ACTIVE_STATUSES, LABELER_RESUMABLE_STATUSES
from naiba.labeler.engines import DEFAULT_ENGINE, ENGINE_IDS
from naiba.labeler.images import stable_item_id, validate_image_refs
from naiba.labeler.presets import DEFAULT_PRESET, PRESET_IDS
from naiba.labeler.runner import DEFAULT_CONCURRENCY, VALIDATED_CONCURRENCY

VALID_OUTPUT_MODES = ("keywords", "phrases", "description", "both")
# 输出语言：mixed = 标签英文 + 描述中文（历史默认）；zh / en = 标签与描述统一语言。
VALID_LANGUAGES = ("mixed", "zh", "en")
ALLOWED_CONFIG_KEYS = frozenset({
    "engine", "output_mode", "language", "preset", "model_key", "model_name",
    "confidence_threshold", "max_tags", "custom_prompt", "subject_filter",
    "concurrency", "retry_limit", "timeout_seconds", "prompt_version",
})
# 只进指纹的「影响结果」字段：换并发/重试次数/超时不该改变结果身份。
FINGERPRINT_KEYS = (
    "engine", "output_mode", "language", "preset", "model_key", "model_name",
    "confidence_threshold", "max_tags", "custom_prompt", "subject_filter", "prompt_version",
)
VALID_STRENGTHS = ("low", "medium", "high")
MAX_CUSTOM_PROMPT = 2000


class LabelerTaskError(ValueError):
    """任务操作非法（参数/状态）。"""


def _normalize_subject_filter(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"enabled": False, "keywords": [], "strength": "medium"}
    if not isinstance(raw, dict):
        raise LabelerTaskError("subject_filter 必须是对象")
    unknown = set(raw) - {"enabled", "keywords", "strength"}
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise LabelerTaskError(f"subject_filter 包含不支持的字段：{names}")
    enabled = bool(raw.get("enabled"))
    keywords_raw = raw.get("keywords") or []
    if not isinstance(keywords_raw, list):
        raise LabelerTaskError("subject_filter.keywords 必须是数组")
    keywords = [str(word).strip()[:80] for word in keywords_raw if str(word).strip()][:200]
    strength = str(raw.get("strength") or "medium").strip()
    if strength not in VALID_STRENGTHS:
        raise LabelerTaskError(f"subject_filter.strength 非法：{strength}")
    return {"enabled": enabled, "keywords": keywords, "strength": strength}


def normalize_config(app: Any, raw: Any) -> dict[str, Any]:
    """白名单校验并归一化 config；顺带校验模型 profile（不落任何密钥）。"""
    if not isinstance(raw, dict):
        raise LabelerTaskError("config 必须是对象")
    unknown = set(raw) - ALLOWED_CONFIG_KEYS
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise LabelerTaskError(f"config 包含不支持的字段：{names}")
    engine = str(raw.get("engine") or DEFAULT_ENGINE).strip() or DEFAULT_ENGINE
    if engine not in ENGINE_IDS:
        raise LabelerTaskError(f"未知的打标引擎：{engine}")
    output_mode = str(raw.get("output_mode") or "keywords").strip()
    if output_mode not in VALID_OUTPUT_MODES:
        raise LabelerTaskError("output_mode 必须是 keywords / phrases / description / both")
    language = str(raw.get("language") or "mixed").strip().lower()
    if language not in VALID_LANGUAGES:
        raise LabelerTaskError("language 必须是 mixed / zh / en")
    preset = str(raw.get("preset") or DEFAULT_PRESET).strip() or DEFAULT_PRESET
    if preset not in PRESET_IDS:
        raise LabelerTaskError(f"未知的打标侧重：{preset}")
    model_key = str(raw.get("model_key") or "").strip()
    model_name = str(raw.get("model_name") or "").strip()[:256]
    if not model_key or not model_name:
        raise LabelerTaskError("请先选择模型配置与模型")
    try:
        # 只校验配置能解析（供应商存在、模型名非空）；云端与本地后端都允许，
        # 本地后端由 runtime 自己的全局执行锁串行化。
        resolve_profile_with_override(app.config, model_key, model_name)
    except (ValueError, KeyError) as exc:
        raise LabelerTaskError(f"模型配置不可用：{exc}") from exc
    try:
        confidence = float(raw.get("confidence_threshold", 0.35))
    except (TypeError, ValueError):
        raise LabelerTaskError("confidence_threshold 必须是数字") from None
    if not 0.0 <= confidence <= 1.0:
        raise LabelerTaskError("confidence_threshold 必须在 0 到 1 之间")
    try:
        max_tags = int(raw.get("max_tags", 24))
        retry_limit = int(raw.get("retry_limit", 2))
        timeout_seconds = int(raw.get("timeout_seconds", 90))
        concurrency = int(raw.get("concurrency", DEFAULT_CONCURRENCY))
        prompt_version = int(raw.get("prompt_version", 2))
    except (TypeError, ValueError):
        raise LabelerTaskError("max_tags / retry_limit / timeout_seconds / concurrency 必须是整数") from None
    if not 1 <= max_tags <= 64:
        raise LabelerTaskError("max_tags 必须在 1 到 64 之间")
    if not 0 <= retry_limit <= 5:
        raise LabelerTaskError("retry_limit 必须在 0 到 5 之间")
    if not 15 <= timeout_seconds <= 600:
        raise LabelerTaskError("timeout_seconds 必须在 15 到 600 之间")
    if concurrency not in VALIDATED_CONCURRENCY:
        raise LabelerTaskError(
            "当前只开放同时处理 1 张图片（多张并发尚未通过供应商验证）"
        )
    custom_prompt = str(raw.get("custom_prompt") or "").strip()[:MAX_CUSTOM_PROMPT]
    config = {
        "engine": engine,
        "output_mode": output_mode,
        "language": language,
        "preset": preset,
        "model_key": model_key,
        "model_name": model_name,
        "confidence_threshold": round(confidence, 3),
        "max_tags": max_tags,
        "custom_prompt": custom_prompt,
        "subject_filter": _normalize_subject_filter(raw.get("subject_filter")),
        "concurrency": concurrency,
        "retry_limit": retry_limit,
        "timeout_seconds": timeout_seconds,
        "prompt_version": prompt_version,
    }
    config["config_fingerprint"] = config_fingerprint(config)
    return config


def config_fingerprint(config: dict[str, Any]) -> str:
    """结果身份：只含影响标注内容的字段，不含并发/重试/超时/密钥。"""
    payload = {key: config.get(key) for key in FINGERPRINT_KEYS}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "cfg-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


class LabelerJobManager:
    def __init__(self, app: Any, store: Any, db: Any, runner: Any):
        self.app = app
        self.store = store
        self.db = db
        self.runner = runner

    # ---- 启动恢复 ----

    def recover_interrupted(self) -> int:
        recovered = 0
        for task in self.store.list_tasks(active_only=True, limit=5000):
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            self.runner.cancel(task_id, reason="服务重启前任务中断")
            # running 的逐图项：请求已发出、结果未落库 → uncertain（可能已计费）。
            self.db.mark_running_as_uncertain(task_id)
            self.db.cancel_pending(task_id)
            counts = self.db.counts(task_id)
            self.store.update_task(
                task_id,
                status="interrupted",
                detail="服务重启前任务中断。继续时只会处理未完成项；"
                       "「结果不确定」的图片可能已经计费，需要你确认后才会重试。",
                progress=self._progress(counts),
                result={"annotation_ids": self.db.annotation_ids_for_task(task_id)},
            )
            try:
                self.store.append_event(task_id, {"type": "error", "message": "服务重启前任务中断"})
            except Exception:  # noqa: BLE001 - 事件日志失败不影响恢复
                pass
            recovered += 1
        # 没有任务记录的僵尸逐图行（极端情况）：一并收敛，避免永远停在 running。
        self.db.mark_running_as_uncertain()
        return recovered

    # ---- 提交 ----

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise LabelerTaskError("请求体必须是对象")
        refs = validate_image_refs(self.app.paths.data_dir, body.get("image_refs"))
        config = normalize_config(self.app, body.get("config"))
        input_data = {"image_refs": refs, "config": config}
        return self._create_task(input_data, source_task_id="")

    def _create_task(self, input_data: dict[str, Any], *, source_task_id: str) -> dict[str, Any]:
        config = input_data.get("config") or {}
        refs = input_data.get("image_refs") or []
        if not refs:
            raise LabelerTaskError("没有可打标的图片")
        task = self.store.create_task(input_data, source_task_id=source_task_id)
        fingerprint = str(config.get("config_fingerprint") or "")
        items = []
        for ref in refs:
            items.append({
                **ref,
                "item_id": stable_item_id(task["id"], ref["path_key"], ref["content_version"]),
                "state": "queued",
            })
        self.db.add_items(task["id"], items, fingerprint)
        self.runner.submit(task["id"])
        return task

    # ---- 查询 ----

    def tasks(self, active_only: bool = False, limit: int = 60) -> list[dict[str, Any]]:
        return [self._slim(task) for task in self.store.list_tasks(active_only=active_only, limit=limit)]

    def task(self, task_id: str) -> dict[str, Any] | None:
        task = self.store.get_task(task_id)
        if task is None:
            return None
        # 进度/计数以数据库为准（事件流只是通知）。
        counts = self.db.counts(task_id)
        task["progress"] = self._progress(counts)
        task["counts"] = counts
        task["items"] = self._public_items(task_id)
        task["can_continue"] = self._can_continue(task, counts)
        task["uncertain_count"] = int(counts.get("uncertain") or 0)
        return task

    def events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.store.read_events(task_id, after=after)

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        """恢复候选：中断 / 部分失败 / 结果不确定 / 近期活动。"""
        rows: list[dict[str, Any]] = []
        for task in self.store.list_tasks(limit=60):
            counts = self.db.counts(str(task.get("id") or ""))
            interesting = (
                str(task.get("status") or "") in LABELER_RESUMABLE_STATUSES
                or int(counts.get("failed") or 0) > 0
                or int(counts.get("uncertain") or 0) > 0
            )
            if not interesting:
                continue
            slim = self._slim(task)
            slim["progress"] = self._progress(counts)
            slim["counts"] = counts
            slim["can_continue"] = self._can_continue(task, counts)
            slim["uncertain_count"] = int(counts.get("uncertain") or 0)
            rows.append(slim)
            if len(rows) >= max(1, min(int(limit), 20)):
                break
        return rows

    # ---- 取消 / 继续 ----

    def cancel(self, task_id: str) -> bool:
        task = self.store.get_task(task_id)
        if task is None:
            return False
        if str(task.get("status") or "") in {"done", "failed", "cancelled", "interrupted"}:
            return False
        return self.runner.cancel(task_id, reason="用户取消")

    def continue_task(self, task_id: str, *, mode: str = "unfinished",
                      confirm_uncertain: bool = False) -> dict[str, Any]:
        """「继续未完成」：新建关联任务，只复制未完成/失败/（已确认的）不确定项。"""
        source = self.store.get_task(task_id)
        if source is None:
            raise LookupError("任务不存在")
        if mode not in {"unfinished", "failed_only"}:
            raise LabelerTaskError("mode 必须是 unfinished 或 failed_only")
        if str(source.get("status") or "") in LABELER_ACTIVE_STATUSES:
            raise LabelerTaskError("任务还在执行，请先取消或等待结束")
        source_input = source.get("input") or {}
        refs = [ref for ref in (source_input.get("image_refs") or []) if isinstance(ref, dict)]
        if not refs:
            raise LabelerTaskError("源任务没有可继续的图片")
        items = self.db.list_items(task_id, limit=5000)
        by_identity = {
            (str(item.get("path_key") or ""), str(item.get("content_version") or "")): item
            for item in items
        }
        wanted: list[dict[str, Any]] = []
        skipped_succeeded = 0
        skipped_uncertain = 0
        for ref in refs:
            item = by_identity.get((ref.get("path_key"), ref.get("content_version")))
            if item is None:
                continue
            state = str(item.get("state") or "")
            if state == "succeeded":
                skipped_succeeded += 1
                continue
            if state == "uncertain" and not confirm_uncertain:
                skipped_uncertain += 1
                continue
            if mode == "failed_only" and state not in {"failed", "uncertain"}:
                continue
            if state == "running":
                # 理论上恢复后不会残留；保守当作不确定，需要确认。
                if not confirm_uncertain:
                    skipped_uncertain += 1
                    continue
            wanted.append(ref)
        if not wanted:
            reason = "没有需要继续的图片"
            if skipped_uncertain:
                reason = (f"只剩 {skipped_uncertain} 张「结果不确定」的图片；"
                          "它们可能已经计费，需要明确确认后才会重试")
            raise LabelerTaskError(reason)
        # 重新校验受管引用：原图可能已被缓存清理（引用保护失效的极端情况）。
        config = dict(source_input.get("config") or {})
        # 重试沿用原快照；模型配置若已失效，提交时解析会明确报错。
        config.pop("config_fingerprint", None)
        config = normalize_config(self.app, config)
        refs = validate_image_refs(self.app.paths.data_dir, wanted)
        task = self._create_task(
            {"image_refs": refs, "config": config}, source_task_id=task_id,
        )
        task["skipped_succeeded"] = skipped_succeeded
        task["skipped_uncertain"] = skipped_uncertain
        return task

    # ---- 内部 ----

    @staticmethod
    def _progress(counts: dict[str, int]) -> dict[str, int]:
        return {
            "done": int(counts.get("done") or 0),
            "total": int(counts.get("total") or 0),
            "failed": int(counts.get("failed") or 0),
            "uncertain": int(counts.get("uncertain") or 0),
            "cancelled": int(counts.get("cancelled") or 0),
        }

    @staticmethod
    def _can_continue(task: dict[str, Any], counts: dict[str, int]) -> bool:
        if str(task.get("status") or "") in LABELER_ACTIVE_STATUSES:
            return False
        return bool(
            int(counts.get("failed") or 0)
            or int(counts.get("uncertain") or 0)
            or int(counts.get("queued") or 0)
            or int(counts.get("running") or 0)
            or str(task.get("status") or "") in LABELER_RESUMABLE_STATUSES
        )

    @staticmethod
    def _slim(task: dict[str, Any]) -> dict[str, Any]:
        """列表用精简记录：不带整批 image_refs（500 张的清单不该出现在列表接口里）。"""
        input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
        config = input_data.get("config") if isinstance(input_data.get("config"), dict) else {}
        refs = input_data.get("image_refs") or []
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
            "result": task.get("result") or {"annotation_ids": []},
            "error": task.get("error") or "",
            "usage": task.get("usage") or {},
            "source_task_id": task.get("source_task_id") or "",
            "image_count": len(refs),
            "label": LabelerJobManager._label_for(task, refs),
            "preset": config.get("preset") or "",
            "model_name": config.get("model_name") or "",
            "output_mode": config.get("output_mode") or "",
        }

    @staticmethod
    def _label_for(task: dict[str, Any], refs: list[Any]) -> str:
        """批次标题：优先用共同的顶层文件夹名，其次第一张图的文件名 + 张数。"""
        roots: set[str] = set()
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            relative = str(ref.get("relative_path") or "").replace("\\", "/")
            head = relative.split("/", 1)[0] if "/" in relative else ""
            if head:
                roots.add(head)
        count = len(refs)
        if len(roots) == 1:
            return f"{next(iter(roots))} · {count} 张"
        first = ""
        for ref in refs:
            if isinstance(ref, dict) and ref.get("name"):
                first = str(ref.get("name"))
                break
        if first:
            return f"{first} 等 {count} 张"
        return f"{count} 张图片"

    def _public_items(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.list_items(task_id, limit=2000)
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append({
                "item_id": row["item_id"],
                "name": row.get("name") or "",
                "relative_path": row.get("relative_path") or "",
                "state": row.get("state") or "",
                "error": row.get("error") or "",
                "error_category": row.get("error_category") or "",
                "attempt_count": row.get("attempt_count") or 0,
                "annotation_id": row.get("annotation_id"),
                "usage": row.get("usage") or {},
            })
        return out
