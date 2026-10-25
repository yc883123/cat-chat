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
import re
from typing import Any

from naiba.llm.profiles import resolve_profile_with_override
from naiba.labeler import LABELER_ACTIVE_STATUSES, LABELER_RESUMABLE_STATUSES
from naiba.labeler.engines import DEFAULT_ENGINE, ENGINE_IDS
from naiba.labeler.images import stable_item_id, validate_image_refs
from naiba.labeler.presets import DEFAULT_PRESET, PRESET_IDS, canonical_preset_id
from naiba.labeler.runner import DEFAULT_CONCURRENCY, VALIDATED_CONCURRENCY

VALID_OUTPUT_MODES = ("keywords", "phrases", "description", "both")
# 输出语言：mixed = 标签英文 + 描述中文（历史默认）；zh / en = 标签与描述统一语言。
VALID_LANGUAGES = ("mixed", "zh", "en")
ALLOWED_CONFIG_KEYS = frozenset({
    "engine", "output_mode", "language", "preset", "model_key", "model_name",
    "confidence_threshold", "max_tags", "custom_prompt", "subject_filter",
    "concurrency", "retry_limit", "timeout_seconds", "prompt_version",
    "trigger_word",
})
# 只进指纹的「影响结果」字段：换并发/重试次数/超时不该改变结果身份。
# trigger_word 进指纹，但「关闭」时按缺省处理（config_fingerprint 里归一化），
# 这样默认关闭的配置与旧任务指纹一致，已有标注照常复用、不重复计费。
FINGERPRINT_KEYS = (
    "engine", "output_mode", "language", "preset", "model_key", "model_name",
    "confidence_threshold", "max_tags", "custom_prompt", "subject_filter", "prompt_version",
    "trigger_word",
)
VALID_STRENGTHS = ("low", "medium", "high")
MAX_CUSTOM_PROMPT = 2000
MAX_TRIGGER_WORD = 200


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


def _normalize_trigger_word(raw: Any) -> dict[str, Any]:
    """触发词前缀：用户明确指定的字符串，不进模型提示词、只加在结果最前。

    空白折叠成单空格、换行全部抹掉：逐图 caption 是一行文本，触发词带换行会
    破坏「图片同名 txt 一图一文件」的约定。启用但没填内容直接拒（前端同样拦截）。
    """
    if raw is None:
        return {"enabled": False, "value": ""}
    if not isinstance(raw, dict):
        raise LabelerTaskError("trigger_word 必须是对象")
    unknown = set(raw) - {"enabled", "value"}
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise LabelerTaskError(f"trigger_word 包含不支持的字段：{names}")
    enabled = bool(raw.get("enabled"))
    value = re.sub(r"\s+", " ", str(raw.get("value") or "")).strip()[:MAX_TRIGGER_WORD]
    if enabled and not value:
        raise LabelerTaskError("启用触发词后需要填写触发词内容")
    return {"enabled": enabled, "value": value}


def normalize_config(app: Any, raw: Any, *, allow_legacy_language: bool = False) -> dict[str, Any]:
    """白名单校验并归一化 config；顺带校验模型 profile（不落任何密钥）。

    ``allow_legacy_language``：继续/恢复旧任务时放行「英文标签 + 中文描述（mixed）+
    非标签加自然语言」的历史组合——新任务不允许提交这种组合，但老配置必须原样执行。
    """
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
    if language == "mixed" and output_mode != "both" and not allow_legacy_language:
        raise LabelerTaskError(
            "输出语言与输出形式不匹配：「英文标签 + 中文描述」只在「标签 + 自然语言」下可选"
        )
    # 旧任务里的 concept_lora 先映射成 action_lora 再校验，续跑不会因改名被挡。
    preset = canonical_preset_id(raw.get("preset")) or DEFAULT_PRESET
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
        "trigger_word": _normalize_trigger_word(raw.get("trigger_word")),
        "concurrency": concurrency,
        "retry_limit": retry_limit,
        "timeout_seconds": timeout_seconds,
        "prompt_version": prompt_version,
    }
    config["config_fingerprint"] = config_fingerprint(config)
    return config


def config_fingerprint(config: dict[str, Any]) -> str:
    """结果身份：只含影响标注内容的字段，不含并发/重试/超时/密钥。

    触发词「关闭」时按缺省处理（不含触发词键）：默认关闭的新配置与
    旧任务指纹一致，已有标注照常复用；启用或关闭都会在开启时区分。
    """
    payload = {key: config.get(key) for key in FINGERPRINT_KEYS}
    trigger = payload.get("trigger_word")
    if not (isinstance(trigger, dict) and trigger.get("enabled")
            and str(trigger.get("value") or "").strip()):
        payload.pop("trigger_word", None)
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

    # ---- 删除 / 清理（2026-10-26 计划）----

    def delete_tasks(self, task_ids: list[str] | None) -> dict[str, Any]:
        """删除已结束任务的记录、事件与逐图行。

        - 指定 id：只删已结束的；排队/执行中的跳过并回报（打标按图计费，
          取消必须显式，不做「删除时自动取消」）；
        - 不指定 id：清理全部已结束任务（「清理已完成」语义）；
        - 标注库（annotations）不动：标注按内容 + 配置指纹全局复用，删除任务后
          仍可查询、导出；原图留存保护由剩余标注行承担。
        """
        if task_ids is not None:
            targets = [str(item).strip() for item in task_ids[:200] if str(item).strip()]
        else:
            targets = [str(task.get("id") or "") for task in self.store.list_tasks(limit=5000)]
        deleted = 0
        skipped: list[str] = []
        for task_id in targets:
            task = self.store.get_task(task_id)
            if task is None:
                continue
            if str(task.get("status") or "") in LABELER_ACTIVE_STATUSES:
                skipped.append(task_id)
                continue
            if self.store.delete_task(task_id):
                self.db.delete_items(task_id)
                deleted += 1
        return {"deleted": deleted, "skipped": skipped}

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
        # 历史快照可能带「mixed + 非标签加自然语言」的旧默认组合：继续时原样放行。
        config.pop("config_fingerprint", None)
        config = normalize_config(self.app, config, allow_legacy_language=True)
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
            # 批次历史按现行档位显示（旧任务存的 concept_lora 也要显示成「动作」）。
            "preset": canonical_preset_id(config.get("preset")),
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
