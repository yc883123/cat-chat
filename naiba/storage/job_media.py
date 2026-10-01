# -*- coding: utf-8 -*-
"""异步 Job 产物写回：终态时把产物媒体挂回**发起该 Job 的那条助手消息**。

背景（维护说明 §四 媒体链路）：`comfyui_batch(wait=false)` / `run_in_background` 立即返回
job_id，产物要等 Job 跑完才出现——若模型之后不再调 `job_output` 把结果带回，生成好的
图片/视频在会话里就永远看不到（生图/生视频的主路径）。本模块在 Job 终态时：

1. 经 `parent_job_id` 定位发起它的 Run（kind ∈ chat/plan_execute）；
2. 在该会话的消息里按 `metadata.run_id` 找到那条助手消息（优先 Run 的 detail.message_id）；
3. 用与工具产出点**同一套声明式采集**（`MediaCollector` + `JOB_MEDIA_DECLARATIONS`）从
   Job result 提取媒体；
4. 把记录挂到"调用该 Job 的那次工具调用"（按 result 里的 job_id 匹配 tool_runs/activity），
   匹配不到时退化为并入消息级 `attachments`（末尾网格仍可见）；
5. 就地更新消息 metadata 并推进 `conversations.updated_at`——前端既有轮询
   （`loadTasks` → `syncCurrentConversation` 的 snapshot 变化）据此重渲染，**无需新事件通道**。

幂等：按来源去重，重复写回不会产生重复媒体；写回失败只记录，绝不影响 Job 终态。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from naiba.core.attachments import union_run_media
from naiba.core.media_types import job_media_declaration
from naiba.core.messages import MetadataKeys
from naiba.storage.media_collect import MediaCollector

logger = logging.getLogger("naiba.storage.job_media")

# 允许写回的父 Run 类型（只有对话轮次才有"消息"可挂）
_PARENT_RUN_KINDS = {"chat", "plan_execute"}
# 允许写回的 Job 终态：completed 是常态；cancelled/failed 也可能已有部分产物
# （ComfyUI 批量里前几段已完成），"生成过就要让用户看到"。
_WRITABLE_STATUSES = {"completed", "cancelled", "failed"}


class JobMediaWriter:
    """把异步 Job 的产物媒体写回发起它的助手消息（幂等、失败不抛给调用方）。"""

    def __init__(self, storage: Any, config: Any, paths: Any = None, collector: Any = None) -> None:
        self._storage = storage
        self._config = config
        self._paths = paths
        self._collector = collector or MediaCollector(config, paths)
        # 读→去重合并→写回必须**串行化**：两个 Job 同时终态时，后写者若拿着"读到的旧数组"
        # 覆盖先写者刚落的媒体，就是同键陈旧数组覆盖——键级合并只隔离不同键，挡不住同键。
        # Job 在**同进程**后台线程里跑，进程内一把锁即可（写回低频，全局串行无实际代价）。
        self._write_lock = threading.Lock()

    def write_back(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """写回一次；返回 ``{conversation_id, message_id, added}``，无需写回时返回 None。"""
        kind = str((job or {}).get("kind") or "")
        declaration = job_media_declaration(kind)
        if declaration["extract"] == "none" or declaration["policy"] == "never":
            return None
        if str((job or {}).get("status") or "") not in _WRITABLE_STATUSES:
            return None
        result = (job or {}).get("result")
        if not isinstance(result, dict) or not result:
            return None
        parent_id = str((job or {}).get("parent_job_id") or "")
        if not parent_id:
            return None
        parent = self._storage.get_background_task(parent_id)
        if not parent or str(parent.get("kind") or "") not in _PARENT_RUN_KINDS:
            return None
        conversation_id = str(parent.get("conversation_id") or (job or {}).get("conversation_id") or "")
        if not conversation_id:
            return None
        job_id = str((job or {}).get("id") or "")
        with self._write_lock:
            # 读也在锁内：并发的第二个写回必须读到第一个已提交的结果，否则会拿旧数组覆盖。
            message = self._find_assistant_message(conversation_id, parent_id, parent)
            if message is None:
                logger.info("Job 产物无处挂载（未找到对应助手消息）：job=%s run=%s", job_id, parent_id)
                return None
            collected = self._collector.collect(
                {
                    "tool": f"job:{kind}",
                    "result": json.dumps(result, ensure_ascii=False),
                    "success": True,
                },
                declaration,
            )
            media = collected.get("media") or []
            if not media:
                return None
            metadata = dict(message.get("metadata") or {})
            added = self._attach(metadata, job_id, media, collected.get("truncated"))
            if not added:
                return None
            message_id = str(message.get("id") or "")
            # **只 patch 自己拥有的那几个键**，不整块写回。同一条助手消息上还有别的写入方
            # （用户可以给 AI 回复点「新会话」→ `set_session_start` 就写在这一行），而 Job
            # 可能在几十分钟后才写回产物：整块写回会拿着"点之前"的旧快照把 `session_start`
            # 抹掉 ⇒ `build_model_history` 清空整段上下文（用户视角＝模型突然失忆）。
            # 只带上"本来就有"的键，避免凭空给消息加空数组。
            patch: dict[str, Any] = {}
            for key in (MetadataKeys.TOOL_RUNS, "activity", MetadataKeys.ATTACHMENTS):
                if key in metadata:
                    patch[key] = metadata[key]
            # 产物不再截断时要清掉旧标记（`json_remove`；删不存在的键是静默无操作）。
            remove: tuple[str, ...] = ()
            if metadata.get(MetadataKeys.ATTACHMENTS_TRUNCATED):
                patch[MetadataKeys.ATTACHMENTS_TRUNCATED] = metadata[MetadataKeys.ATTACHMENTS_TRUNCATED]
            else:
                remove = (MetadataKeys.ATTACHMENTS_TRUNCATED,)
            if not self._storage.merge_message_metadata(
                conversation_id, message_id, patch, remove=remove
            ):
                logger.warning("Job 产物写回失败（消息已不存在）：message=%s", message_id)
                return None
            return {"conversation_id": conversation_id, "message_id": message_id, "added": added}

    # ---- 内部 ----
    def _find_assistant_message(
        self, conversation_id: str, run_id: str, parent: dict[str, Any]
    ) -> dict[str, Any] | None:
        """定位该 Run 落库的助手消息（优先 run detail.message_id，回退 metadata.run_id）。"""
        conversation = self._storage.get_conversation(conversation_id)
        messages = (conversation or {}).get("messages") or []
        detail = parent.get("detail") if isinstance(parent.get("detail"), dict) else {}
        message_id = str(detail.get("message_id") or "")
        if message_id:
            for message in messages:
                if str(message.get("id") or "") == message_id:
                    return message
        for message in reversed(messages):
            if str(message.get("role") or "") != "assistant":
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            if str(metadata.get("run_id") or "") == run_id:
                return message
        return None

    def _attach(
        self,
        metadata: dict[str, Any],
        job_id: str,
        media: list[dict[str, Any]],
        truncated: dict[str, Any] | None,
    ) -> int:
        """把新产物挂到发起该 Job 的那次工具调用上，并重算消息级派生汇总。"""
        runs: list[dict[str, Any]] = [
            run for run in (metadata.get("tool_runs") or []) if isinstance(run, dict)
        ]
        targets = [run for run in runs if job_id and job_id in str(run.get("result") or "")]
        for item in metadata.get("activity") or []:
            run = item.get("run") if isinstance(item, dict) else None
            if isinstance(run, dict) and job_id and job_id in str(run.get("result") or ""):
                if run not in targets:
                    targets.append(run)
        added = 0
        for run in targets:
            existing = {
                str(item.get("source") or "")
                for item in (run.get("media") or [])
                if isinstance(item, dict)
            }
            fresh = [item for item in media if str(item.get("source") or "") not in existing]
            if not fresh:
                continue
            run["media"] = list(run.get("media") or []) + fresh
            if truncated and not run.get("media_truncated"):
                run["media_truncated"] = truncated
            added += len(fresh)
        # 消息级派生汇总与 chat.py 同口径：从各 run 重算；匹配不到工具调用时并入新产物。
        union, union_truncated = union_run_media(runs)
        if not targets:
            existing = {
                str(item.get("source") or "")
                for item in (metadata.get(MetadataKeys.ATTACHMENTS) or [])
                if isinstance(item, dict)
            }
            fresh = [item for item in media if str(item.get("source") or "") not in existing]
            if not fresh:
                return 0
            union, union_truncated = union_run_media([{"media": union + fresh}])
            added = len(fresh)
        metadata[MetadataKeys.ATTACHMENTS] = union
        if union_truncated:
            metadata[MetadataKeys.ATTACHMENTS_TRUNCATED] = union_truncated
        else:
            metadata.pop(MetadataKeys.ATTACHMENTS_TRUNCATED, None)
        return added
