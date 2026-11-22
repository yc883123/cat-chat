# -*- coding: utf-8 -*-
"""织书台服务端流水线：六步设定、整章 / 分段正文的推进、取消、恢复与重试。

为什么调度权在服务端（实施计划 §4.2）：前端只发命令、展示流水线与任务事件，
不持有「下一步必须执行」的唯一状态——否则刷新后当前任务完成但下一步永远不再
提交、旧页面回调在新页面上重复生成、取消只停了当前任务而延迟回调又启动下一步。

推进条件（实施计划 §7.2，全部满足才写回并前进）：

1. 当前任务确实 `done`；
2. 任务 `input.writer` 的 book/pipeline/attempt/step/chapter 与活动流水线一致；
3. 输出非空、结构化校验通过、正文没有明确截断；
4. 锁内再次确认流水线仍 running 且 base_revision 与当前作品/章节一致；
5. 原子写入新版本并更新当前指针成功。

任一失败停链并给出真实状态；前序已保存的版本保留。同一任务重复写回是幂等的
（completed_task_ids + 版本层 task_id 去重），崩溃恢复与重复点击安全。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import traceback
from contextlib import nullcontext
from typing import Any

from naiba.core.contracts import (
    WRITER_CHAPTER_BASELINE_KEY,
    WRITER_CHAPTER_STATES_KEY,
    WRITER_PREVIOUS_CHAPTER_KEY,
    WRITER_SETUP_SNAPSHOT_KEY,
    WRITER_PIPELINE_STOPPING,
    WRITER_PIPELINE_NEEDS_USER,
    WRITER_PENDING_USER_KEY,
    WRITER_PENDING_ACCEPTED_KEY,
    WRITER_DECISION_DIGEST_KEY,
    WRITER_DECISION_ACTIONS,
    WRITER_DECISION_REVISION_KEY,
    WRITER_DECISION_HISTORY_KEY,
    WRITER_DECISION_REQUEST_KEY,
    WORK_GLOBAL_SYSTEM_PROMPT_KEY,
    WORK_GLOBAL_SYSTEM_PROMPT_REVISION_KEY,
    WRITER_COMPLETED_GENERATIONS_KEY,
    WRITER_RESULT_RESUME_KEY,
    WRITER_OUTPUT_MODE_KEY,
    STUDIO_RESULT_ISSUE_KEY,
    WriterPendingUser,
)
from naiba.writer import prompts
from naiba.writer.store import (
    SETUP_TEXT_MAX_CHARS,
    LEAD_MAX,
    SIDE_MAX,
    normalize_setup,
    WriterConflictError,
    WriterStoreError,
    new_version_id,
)

# 流水线 kind：整套设定 / 单步设定 / 大纲 / 整章 / 分段 / 续写 / 重写 / 状态重建。
PIPELINE_KINDS = (
    "setup_all", "setup_step", "outline", "outline_text", "outline_all", "split",
    "outline_review",
    "chapter", "chapter_segs", "chapter_continue", "chapter_rewrite",
    "chapter_all",
    "state_rebuild",
)
# 各 kind 的步骤序列（segs → draft 两步；draft 复用 Studio chunked 顺序生成各段）。
# draft 之后统一跟 state（章末状态提取）；已保存的正文不依赖状态提取成功。
KIND_STEPS = {
    "setup_all": tuple(prompts.SETUP_STEPS),
    "outline": ("outline",),
    # 3.9.13：整篇大纲（按部）与拆章各是一条单步流水线，互不耦合。
    "outline_text": ("outline_text",),
    # 第二次反馈 §五：一次性生成全部部的大纲——步骤是 N 个 outline_text，
    # 每步用哪一部由快照里的 part_plan[step_index] 决定（保证多部互相衔接）。
    "outline_all": ("outline_text",),
    # 拆章的实际步数在 start_pipeline 里按 SPLIT_STEP_CHAPTERS 切（此处只是占位校验用）。
    "split": ("split",),
    # 第三次反馈④：多部统一复核梳理——步骤是 N 个 outline_review，每步修订一部。
    "outline_review": ("outline_review",),
    "chapter": ("draft", "state"),
    "chapter_segs": ("segs", "draft", "state"),
    "chapter_continue": ("draft", "state"),
    "chapter_rewrite": ("draft", "state"),
    # 方案A（2026-11-04 拍板）：链的正文步骤写完拆章范围内全部章节——
    # 步骤是 (draft, state) × N 交错：第 N+1 章的 draft 必须能读到第 N 章刚落盘的
    # 章末状态，所以 state 紧跟每章 draft，而不是全部 draft 完再补状态。
    # 实际章数在锁内按拆章结果定（此处只是占位校验用）。
    "chapter_all": ("draft",),
    # 用户触发/后台补建：对指定章号范围按当前正文重新提取章末状态
    # （兼容老作品没有状态记录、或重写后状态失效的场景）。实际步数在锁内定。
    "state_rebuild": ("state",),
}
SETUP_STEP_KINDS = tuple(prompts.SETUP_STEPS)
TERMINAL_STATUSES = {"done", "failed", "cancelled", "interrupted", "conflict"}

TICK_SECONDS = 0.5
# 停止确认上限（毫秒）：取消进入 stopping 后，超过此时长任务仍未终态，就自动把
# 流水线收尾为 cancelled，解除作品占用。「取消」绝不能把作品永久锁死（stopping
# 状态下前端取消/重试/重新生成全部禁用）：任务自身的取消链路（事件置位 → 断流 →
# 终态）实测亚秒级，60 秒足以覆盖一切合法慢路径；超时只可能来自环境级卡死
# （存储读失败、进程内死锁等），此时后台任务继续跑到它自己的超时为止，其产物
# 不会再写回本作品（写回只发生在 status=running 的推进里）。
WRITER_STOP_CONFIRM_BOUND_MS = 60_000
# 同一步的瞬时失败自动重试上限：模型这一趟什么都没写出来（空结果）时先自动重打，
# 用完预算才把「重新生成 / 跳过」的决定交还给用户。口径与贴签匠监督「重打 2 次」
# 一致；再多的失败基本不是抖动而是模型/线路持续不工作，自动重试只会烧钱。
WRITER_STEP_AUTO_RETRIES = 2
DECISION_HISTORY_LIMIT = 128
DECISION_CONTENT_LIMIT = 384_000
DECISION_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,80}")


class WriterPipelineError(ValueError):
    """流水线操作非法（用户可读，API 边界转 4xx）。"""


class WriterPipelineManager:
    """每部作品同一时刻最多一个活动流水线；状态持久化在 book.json。

    推进由后台线程轮询驱动（0.5s），任务终态 → 校验 → 写回 → 提交下一步。
    轮询而不是回调：Studio 任务体系对 Writer 保持零侵入，错过一次唤醒最多
    晚半秒推进，代价可以忽略。
    """

    def __init__(self, app: Any, store: Any):
        self.app = app
        self.store = store
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Guarded by the store lock. Reserve before publishing a running step
        # with no task ID; True means its unique submit caller has claimed it.
        self._submissions: dict[tuple[str, str, int, int], bool] = {}
        # A deleted/replaced book cannot retain current_task_id. The Studio
        # task's immutable input.writer is its durable ownership record; keep
        # retrying cancellation here while this manager remains alive.
        self._pending_stops: dict[str, tuple[str, dict[str, Any]]] = {}

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="naiba-writer-pipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)

    def recover(self) -> int:
        """启动收敛：进行中/停止待确认的流水线标 interrupted，不自动续跑。"""
        return self.store.recover_pipelines()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 轮询循环不因单本作品异常退出
                traceback.print_exc()
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()

    # ---- 创建 ----

    def start_pipeline(self, book_id: str, *, kind: str, step: str = "",
                       chapter_no: int = 0, model_key: str = "", model_name: str = "",
                       char_count: int = 4, side_count: int = 0, chapter_count: int = 10,
                       setup_version_id: str = "", outline_version_id: str = "",
                       extra_note: str = "", allow_drop: bool = False,
                       part_no: int = 0, part_title: str = "", target_chars: int = 0,
                       want_count: int = 0, from_no: int = 0,
                       part_plan: Any = None,
                       output_mode: str = "json",
                       base_revision: Any = None, chapter_revision: Any = None) -> dict[str, Any]:
        """创建并启动一条流水线：快照固化全部输入，重试不因页面当前选择变化而换料。"""
        if kind not in PIPELINE_KINDS:
            raise WriterPipelineError(f"未知的流水线类型：{kind}")
        if kind == "setup_step":
            if step not in SETUP_STEP_KINDS:
                raise WriterPipelineError(f"未知的设定步骤：{step or '（空）'}")
            steps: tuple[str, ...] = (step,)
        elif kind == "outline_all":
            # 一次生成全部部的大纲：每步一部，部清单在快照里按步取用。
            plan_rows = self._clean_part_plan(part_plan)
            if not plan_rows:
                raise WriterPipelineError("请先填好各部的部标题，再一次性生成")
            if not prompts.PARTS_MIN <= len(plan_rows) <= prompts.PARTS_MAX:
                raise WriterPipelineError(
                    f"部数需要在 {prompts.PARTS_MIN}–{prompts.PARTS_MAX} 之间（当前 {len(plan_rows)}）"
                )
            plan_rows = [
                {"no": pos + 1, "title": str(row.get("title") or "").strip()[:80]}
                for pos, row in enumerate(plan_rows)
            ]
            # 快照里存重排后的清单（带部号）：写回时按它铺目录骨架、按步取部。
            part_plan = plan_rows
            steps = tuple(["outline_text"] * len(plan_rows))
        elif kind == "outline_review":
            # 多部统一复核梳理：逐部修订已写好的大纲。部清单用真实部号（不重排），
            # 至少要两部才谈得上「互相对照」。
            plan_rows = self._clean_part_plan(part_plan)
            if len(plan_rows) < 2:
                raise WriterPipelineError(
                    "多部复核梳理至少要两部已写好内容的大纲；先逐部生成或填写，再统一复核"
                )
            if len(plan_rows) > prompts.PARTS_MAX:
                raise WriterPipelineError(f"最多 {prompts.PARTS_MAX} 部（当前 {len(plan_rows)}）")
            cleaned = []
            for row in plan_rows:
                try:
                    no = int(row.get("no") or 0)
                except (TypeError, ValueError):
                    no = 0
                if no < 1:
                    raise WriterPipelineError("部清单缺少部号；请回到大纲页重新提交复核梳理")
                cleaned.append({"no": no, "title": str(row.get("title") or "").strip()[:80]})
            part_plan = cleaned
            steps = tuple(["outline_review"] * len(cleaned))
        else:
            steps = KIND_STEPS[kind]
        if not str(model_name or "").strip():
            raise WriterPipelineError("请选择模型；可用模型来自设置页的模型检查结果")
        with self.store._lock:
            manifest = self.store._load_manifest(book_id)
            if base_revision is not None and base_revision != "":
                from naiba.writer.store import WriterStore
                WriterStore._check_revision(manifest, base_revision)
            current = manifest.get("active_pipeline")
            if isinstance(current, dict):
                if current.get("status") == WRITER_PIPELINE_NEEDS_USER:
                    raise WriterConflictError("这部作品还有待处理的生成，请先处理或结束它")
                if current.get("status") in ("running", WRITER_PIPELINE_STOPPING):
                    raise WriterPipelineError("这部作品已有正在进行的生成，请先取消或等它结束")
            setup_version_id = str(setup_version_id or "").strip() or str(
                manifest.get("current_setup_version_id") or "")
            setup = None
            if setup_version_id or isinstance(manifest.get("continuation"), dict):
                try:
                    setup = self.store.load_setup(book_id, manifest=manifest, version_id=setup_version_id)
                except (LookupError, WriterStoreError):
                    setup = None
            if steps[0] in ("segs", "draft", "outline", "outline_text", "split", "outline_review") and not setup:
                raise WriterPipelineError("请先生成或保存设定，再生成大纲与正文")
            # 设定链的起点是创意：已保存设定里没有创意时，模型只能编出「未提供创意/未指定」
            # 这类占位结果，白烧 token 还落一版垃圾（真实反馈）。在排队前如实拒绝。
            if kind in ("setup_all", "setup_step") \
                    and not str((setup or {}).get("idea") or "").strip():
                raise WriterPipelineError(
                    "请先在「设定 → 故事创意」里写一句话（或从模板套用后再生成）；"
                    "还没有创意时，模型只能给出占位结果"
                )
            chapter_plan_rows: list[dict[str, int]] = []
            if kind == "chapter_all":
                # 方案A：批量正文的章数在锁内按拆章实际落下的章定——模型少拆
                # 不硬造，范围内没有可写章节就如实拒绝，不空转烧调用。
                want_count = self._clamp(want_count or 0, 1, prompts.SPLIT_BATCH_MAX, 0)
                from_no = max(1, from_no or 1)
                if want_count < 1:
                    raise WriterPipelineError("请指定要写完的章数（from_no 起 want_count 章）")
                existing = {int(n) for n in manifest.get("chapter_ids") or []}
                chapter_plan_rows = [{"no": no} for no in sorted(existing)
                                     if from_no <= no < from_no + want_count]
                for row in chapter_plan_rows:
                    self.store._assert_chapter_editable(self.store._load_chapter_index(
                        self.store._book_dir(book_id), row["no"]))
                if not chapter_plan_rows:
                    raise WriterPipelineError(
                        f"拆章范围内（第 {from_no}–{from_no + want_count - 1} 章）"
                        "还没有可写的章节；先拆章，再批量写正文")
                chapter_no = chapter_plan_rows[0]["no"]
                # draft/state 交错：第 N+1 章开写前必须能读到第 N 章刚落盘的章末状态。
                steps = tuple(step for _ in chapter_plan_rows for step in ("draft", "state"))
            if kind == "state_rebuild":
                # 章末状态重建（用户触发入口）：对指定范围按**当前正文**重新提取。
                # 只有已写正文（当前指针有内容）的章才有状态可言，其余章跳过。
                want_count = self._clamp(want_count or 0, 1, prompts.SPLIT_BATCH_MAX, 0)
                from_no = max(1, from_no or 1)
                if want_count < 1:
                    raise WriterPipelineError("请指定要重建章末状态的章数（from_no 起 want_count 章）")
                existing = {int(n) for n in manifest.get("chapter_ids") or []}
                for no in sorted(existing):
                    if not from_no <= no < from_no + want_count:
                        continue
                    try:
                        chapter_view = self.store.get_chapter(book_id, no, manifest=manifest)
                    except (LookupError, WriterStoreError):
                        continue
                    if not str(chapter_view.get("content") or "").strip():
                        continue
                    chapter_plan_rows.append({"no": no})
                if not chapter_plan_rows:
                    raise WriterPipelineError(
                        f"重建范围内（第 {from_no}–{from_no + want_count - 1} 章）"
                        "没有已写正文的章节；先写正文，再重建章末状态")
                steps = tuple(["state"] * len(chapter_plan_rows))
            if steps[0] in ("segs", "draft"):
                outline_version_id = str(outline_version_id or "").strip() or str(
                    manifest.get("current_outline_version_id") or "")
                if not outline_version_id:
                    raise WriterPipelineError("请先生成大纲，再写正文")
                if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
                    raise WriterPipelineError(f"作品里没有第 {chapter_no} 章；请先同步章节")
                chapter = self.store._load_chapter_index(self.store._book_dir(book_id), chapter_no)
                self.store._assert_chapter_editable(chapter)
                self.store._check_revision(chapter, chapter_revision, what=f"第 {chapter_no} 章")
                chapter_revision = int(chapter.get("revision") or 0)
            else:
                outline_version_id = str(outline_version_id or "").strip()
                chapter_revision = None
            char_count = self._clamp(int(char_count or 4), prompts.CHARS_MIN, prompts.CHARS_MAX, 4)
            side_count = self._clamp(int(side_count or 0), 0, prompts.SIDE_COUNT_MAX, 0)
            chapter_count = self._clamp(int(chapter_count or 10), prompts.OUTLINE_MIN, prompts.OUTLINE_MAX, 10)
            # 拆分参数：只在 outline_text / outline_all / split 有意义，其余 kind 归一成 0。
            part_no = int(part_no or 0)
            target_chars = int(target_chars or 0)
            want_count = int(want_count or 0)
            from_no = int(from_no or 0)
            if kind == "outline_text":
                if part_no < 1:
                    raise WriterPipelineError("请指定要生成第几部的大纲")
                target_chars = self._clamp(target_chars or 8000, 1000, 60000, 8000)
            if kind == "split":
                if want_count < 1:
                    raise WriterPipelineError("请指定要拆多少章")
                want_count = self._clamp(want_count, prompts.SPLIT_MIN, prompts.SPLIT_BATCH_MAX,
                                         prompts.SPLIT_BATCH_MAX)
                from_no = max(1, from_no or 1)
                # 第三次反馈⑦：一次让模型拆几百章，它会把整本书在思考里推演一遍，
                # 正文没出就撞「思考异常冗长」熔断。按 SPLIT_STEP_CHAPTERS 切成多步连拆：
                # 每步落一批章、带上已拆前文衔接，失败也只停在本批。
                steps = tuple(["split"] * max(1, -(-want_count // prompts.SPLIT_STEP_CHAPTERS)))
            book_title = str(manifest.get("title") or "未命名作品")
            output_mode = str(manifest.get(WRITER_OUTPUT_MODE_KEY) or "json")
            output_mode = output_mode if output_mode in ("json", "freeform") else "json"
            snapshot = {
                "kind": kind,
                "steps": list(steps),
                "step": step or "",
                "chapter_no": int(chapter_no or 0),
                "model_key": str(model_key or ""),
                "model_name": str(model_name or "").strip(),
                "char_count": char_count,
                "side_count": side_count,
                "chapter_count": chapter_count,
                "part_no": part_no,
                "part_title": str(part_title or "")[:80],
                "target_chars": target_chars,
                "want_count": want_count,
                "from_no": from_no,
                "part_plan": [dict(row) for row in (part_plan if isinstance(part_plan, list) else [])
                              if isinstance(row, dict)][:prompts.PARTS_MAX],
                # chapter_all 的逐章计划（[{no: 章号}…]）；其余 kind 恒为空表。
                "chapter_plan": list(chapter_plan_rows),
                "setup_version_id": setup_version_id,
                "outline_version_id": outline_version_id,
                "extra_note": str(extra_note or "")[:500],
                "allow_drop": bool(allow_drop),
                "book_title": book_title,
                WORK_GLOBAL_SYSTEM_PROMPT_KEY: str(manifest.get(WORK_GLOBAL_SYSTEM_PROMPT_KEY) or ""),
                WORK_GLOBAL_SYSTEM_PROMPT_REVISION_KEY: int(manifest.get(WORK_GLOBAL_SYSTEM_PROMPT_REVISION_KEY) or 0),
                WRITER_OUTPUT_MODE_KEY: output_mode,
            }
            if isinstance(manifest.get("continuation"), dict) and isinstance(setup, dict):
                # 旧设定或空设定的摘要可能来自清单补值，版本号不能冻结这部分内容。
                snapshot[WRITER_SETUP_SNAPSHOT_KEY] = copy.deepcopy(setup)
            if steps[0] in ("segs", "draft"):
                baseline, previous = self.store.chapter_writing_inputs(book_id, chapter_no)
                snapshot[WRITER_CHAPTER_BASELINE_KEY] = baseline
                snapshot[WRITER_PREVIOUS_CHAPTER_KEY] = previous
                # 前序章末状态随快照冻结（单章口径与章节基线一致）；chapter_all
                # 不冻结——每步现读，保证读到上一章刚落盘的状态。
                snapshot[WRITER_CHAPTER_STATES_KEY] = self.store.chapter_context_states(
                    book_id, chapter_no)
                if kind == "chapter_continue":
                    self._continuation_baseline(snapshot)
            pipeline = {
                "id": new_version_id("pl"),
                "kind": kind,
                "attempt": 1,
                "status": "running",
                "steps": list(steps),
                "step_index": 0,
                "current_task_id": "",
                "completed_task_ids": [],
                "step_outputs": {},
                "request_snapshot": snapshot,
                "source_version_ids": {
                    "setup": setup_version_id,
                    "outline": outline_version_id,
                },
                "base_revision": int(manifest.get("revision") or 0),
                "chapter_revision": int(chapter_revision) if chapter_revision is not None else None,
                "error": "",
                WRITER_PENDING_USER_KEY: None,
                WRITER_DECISION_REVISION_KEY: 0,
                WRITER_DECISION_HISTORY_KEY: [],
                "created_at": int(time.time() * 1000),
                "updated_at": int(time.time() * 1000),
            }
            self.store._save_manifest(self.store._book_dir(book_id), {**manifest, "active_pipeline": pipeline})
            self._submissions[self._step_key(book_id, pipeline)] = False
        self._submit_step(book_id, pipeline)
        # 返回落盘后的权威记录（_submit_step 已把 current_task_id 写回）。
        updated = self.store.get_active_pipeline(book_id)
        return updated if updated is not None else dict(pipeline)

    @staticmethod
    def _clamp(value: int, low: int, high: int, default: int) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    @staticmethod
    def _clean_part_plan(part_plan: Any) -> list[dict[str, Any]]:
        """部清单去杂：只要带非空标题的对象行（outline_all / outline_review 共用）。"""
        if not isinstance(part_plan, list):
            return []
        return [row for row in part_plan
                if isinstance(row, dict) and str(row.get("title") or "").strip()]

    @staticmethod
    def _split_slice(snapshot: dict[str, Any], step_index: int) -> tuple[int, int]:
        """拆章第 step_index 步负责的章号区间：from_no 向前切 SPLIT_STEP_CHAPTERS 一批。

        快照里的 from_no/want_count 是整条流水线的总任务；单步只看自己那一批。
        """
        base_from = int(snapshot.get("from_no") or 1)
        base_want = int(snapshot.get("want_count") or 0)
        start = base_from + step_index * prompts.SPLIT_STEP_CHAPTERS
        want = min(prompts.SPLIT_STEP_CHAPTERS, base_want - step_index * prompts.SPLIT_STEP_CHAPTERS)
        return start, max(1, want)

    # ---- 取消 / 重试 ----

    def cancel_pipeline(self, book_id: str, *, identity: dict[str, Any] | None = None) -> dict[str, Any]:
        """同锁读取当前任务并阻止推进，锁外取消，任务终态才确认停止。"""
        with self.store._lock:
            pipeline = self.store.get_active_pipeline(book_id)
            if not isinstance(pipeline, dict):
                raise WriterPipelineError("这部作品没有进行中的生成")
            if identity is not None:
                if pipeline.get("id") != identity.get("pipeline_id"):
                    raise WriterConflictError("流水线已变化，请重新打开作品")
                self._check_decision_identity(pipeline, identity)
                if pipeline.get("status") != WRITER_PIPELINE_NEEDS_USER:
                    raise WriterConflictError("待处理步骤已变化，请重新打开作品")
            if pipeline.get("status") == WRITER_PIPELINE_NEEDS_USER:
                def abandon(record: dict[str, Any]) -> None:
                    self._clear_pending(record)
                    record.update(status="cancelled", error="已结束生成，之前保存的内容保留", current_task_id="")
                updated = self.store.update_pipeline(book_id, pipeline["id"], abandon,
                                                     expected_attempt=pipeline["attempt"])
                return updated or pipeline
            if str(pipeline.get("status") or "") != "running":
                raise WriterPipelineError("当前生成已结束或正在停止，无需重复取消")
            task_id = str(pipeline.get("current_task_id") or "")
            submitting = self._step_key(book_id, pipeline) in self._submissions

            def _cancel(record: dict[str, Any]) -> None:
                if record.get("status") != "running":
                    raise WriterPipelineError("当前生成已结束，无需取消")
                record["status"] = WRITER_PIPELINE_STOPPING if task_id or submitting else "cancelled"
                record["error"] = "停止尚未确认；已阻止后续步骤" if task_id or submitting else "用户取消"
                # 兜底计时起点：tick 按它判断 stopping 是否超过了确认上限。
                record["stop_requested_at"] = int(time.time() * 1000)

            updated = self.store.update_pipeline(book_id, str(pipeline["id"]), _cancel,
                                                 expected_attempt=int(pipeline.get("attempt") or 0))
            if updated is None:
                raise WriterPipelineError("流水线状态已变化，请刷新后重试")
        if task_id:
            self._request_stop(book_id, updated, task_id)
        with self.store._lock:
            fresh = self._matching_pipeline(book_id, updated)
            return fresh if fresh is not None else updated

    def retry_pipeline(self, book_id: str, *, rebase: bool = False) -> dict[str, Any]:
        """失败步重试：attempt+1 重提当前步；conflict 状态需 `rebase=True` 显式换基。"""
        with self.store._lock:
            manifest = self.store._load_manifest(book_id)
            pipeline = manifest.get("active_pipeline")
            if not isinstance(pipeline, dict):
                raise WriterPipelineError("这部作品没有可重试的生成")
            status = str(pipeline.get("status") or "")
            if status in ("running", WRITER_PIPELINE_STOPPING):
                raise WriterPipelineError("当前生成还在进行中或停止尚未确认")
            if status == "conflict" and not rebase:
                raise WriterPipelineError(
                    "作品或章节已被修改（版本冲突）；请重新读取作品后，勾选「基于当前版本重新生成」再试"
                )
            if status not in ("failed", "conflict", "interrupted", "cancelled"):
                raise WriterPipelineError("只有失败、冲突、中断或取消的生成可以重试")
            if not rebase and isinstance(manifest.get("continuation"), dict):
                # 先校验旧任务来源，避免缺少可恢复快照时先改成 running 再报错。
                self._pipeline_setup(book_id, pipeline.get("request_snapshot") or {})
            attempt = int(pipeline.get("attempt") or 0) + 1

            def _retry(record: dict[str, Any]) -> None:
                record["attempt"] = attempt
                self._clear_pending(record)
                record["status"] = "running"
                record["error"] = ""
                record["current_task_id"] = ""
                record["step_auto_retries"] = 0
                if rebase:
                    record["base_revision"] = int(manifest.get("revision") or 0)
                    snapshot = dict(record.get("request_snapshot") or {})
                    snapshot["book_title"] = str(manifest.get("title") or "未命名作品")
                    snapshot[WORK_GLOBAL_SYSTEM_PROMPT_KEY] = str(manifest.get(WORK_GLOBAL_SYSTEM_PROMPT_KEY) or "")
                    snapshot[WORK_GLOBAL_SYSTEM_PROMPT_REVISION_KEY] = int(manifest.get(WORK_GLOBAL_SYSTEM_PROMPT_REVISION_KEY) or 0)
                    for field in ("setup", "outline"):
                        vid = str(manifest.get(f"current_{field}_version_id") or "")
                        snapshot[f"{field}_version_id"] = vid
                        record.setdefault("source_version_ids", {})[field] = vid
                    if isinstance(manifest.get("continuation"), dict):
                        snapshot[WRITER_SETUP_SNAPSHOT_KEY] = copy.deepcopy(self.store.load_setup(
                            book_id, manifest=manifest, version_id=snapshot["setup_version_id"]))
                    chapter_no = int(snapshot.get("chapter_no") or 0)
                    if any(step in ("segs", "draft") for step in record.get("steps") or []):
                        baseline, previous = self.store.chapter_writing_inputs(book_id, chapter_no)
                        snapshot[WRITER_CHAPTER_BASELINE_KEY] = baseline
                        snapshot[WRITER_PREVIOUS_CHAPTER_KEY] = previous
                        snapshot[WRITER_CHAPTER_STATES_KEY] = self.store.chapter_context_states(
                            book_id, chapter_no)
                        record["chapter_revision"] = baseline["revision"]
                    if record.get("kind") == "chapter_continue":
                        self._continuation_baseline(snapshot)
                    record["request_snapshot"] = snapshot
                    # 换基后的设定来源必须真正生效，不能再被旧链内累计副本覆盖。
                    outputs = dict(record.get("step_outputs") or {})
                    outputs.pop("setup", None)
                    record["step_outputs"] = outputs
                elif record.get("kind") == "chapter_continue":
                    self._continuation_baseline(record.get("request_snapshot") or {})

            updated = self.store.update_pipeline(
                book_id, str(pipeline["id"]), _retry,
                expected_attempt=int(pipeline.get("attempt") or 0))
            if updated is None:
                raise WriterPipelineError("流水线状态已变化，请刷新后重试")
            self._submissions[self._step_key(book_id, updated)] = False
        self._submit_step(book_id, updated)
        with self.store._lock:
            fresh = self._matching_pipeline(book_id, updated)
            return fresh if fresh is not None else updated

    def resume_pipeline(self, book_id: str, *, source_task_id: str = "",
                        request_key: str = "") -> dict[str, Any]:
        """Claim a new execution for the same frozen attempt/step, then schedule outside the book lock."""
        with self.store._lock:
            manifest = self.store._load_manifest(book_id)
            pipeline = manifest.get("active_pipeline")
            if not isinstance(pipeline, dict):
                raise WriterPipelineError("这部作品没有可恢复的生成")
            current_id = str(pipeline.get("current_task_id") or "")
            source_task_id = str(source_task_id or current_id)
            source = self._task(source_task_id)
            if not source or not self._artifact_task(source):
                raise WriterPipelineError("此任务没有可恢复的 TXT 产物，请选择重新生成")
            owner = self._writer_input(source)
            if (owner.get("book_id") != book_id
                    or owner.get("pipeline_id") != pipeline.get("id")):
                raise WriterPipelineError("恢复任务不属于当前作品或流水线")
            logical_id = self._logical_id(source)
            if logical_id in (pipeline.get(WRITER_COMPLETED_GENERATIONS_KEY) or []):
                return dict(pipeline)  # The recoverable writeback already committed all effects.
            previous = pipeline.get(WRITER_RESULT_RESUME_KEY) or {}
            if pipeline.get("status") == "running":
                if (previous.get("source_task_id") == source_task_id
                        and current_id in (source_task_id, previous.get("child_task_id"))):
                    return dict(pipeline)
                raise WriterPipelineError("当前生成仍在进行，请等待结束或先停止")
            pending = pipeline.get(WRITER_PENDING_USER_KEY) or {}
            resumable_pending = (pipeline.get("status") == WRITER_PIPELINE_NEEDS_USER
                                 and pending.get("resume_txt") is True)
            if not resumable_pending and pipeline.get("status") not in {"failed", "interrupted", "cancelled"}:
                raise WriterPipelineError("当前状态不能继续 TXT；版本冲突时请显式基于当前版本重新生成")
            step_index = int(pipeline.get("step_index") or 0)
            steps = pipeline.get("steps") or []
            step = steps[step_index] if step_index < len(steps) else ""
            snapshot = pipeline.get("request_snapshot") or {}
            if (source_task_id != current_id or owner.get("attempt") != pipeline.get("attempt")
                    or owner.get("step") != step
                    or owner.get("chapter_no", 0) != snapshot.get("chapter_no", 0)
                    or ("step_index" in owner and owner["step_index"] != step_index)):
                raise WriterPipelineError("当前步骤或尝试已变化，不能恢复旧任务")
            if int(pipeline.get("base_revision") or 0) != int(manifest.get("revision") or 0):
                raise WriterConflictError("生成来源已变化，请基于当前版本重新生成，不能拼接旧 TXT")
            chapter_revision = pipeline.get("chapter_revision")
            if step in {"draft", "segs"} and chapter_revision is not None:
                index = self.store._load_chapter_index(self.store._book_dir(book_id), int(snapshot.get("chapter_no") or 0))
                if int(index.get("revision") or 0) != int(chapter_revision):
                    raise WriterConflictError("章节已变化，请基于当前版本重新生成")
            if str(source.get("status") or "") in {"queued", "running"}:
                raise WriterPipelineError("原任务尚未停止，暂时不能恢复")
            key = self._step_key(book_id, pipeline)
            if key in self._submissions:
                return dict(pipeline)
            request_key = str((previous.get("request_key") if previous.get("source_task_id") == source_task_id else "") or request_key
                              or f"writer-resume:{book_id}:{pipeline['id']}:{pipeline.get('attempt')}:{step_index}:{source_task_id}")
            resume = {"source_task_id": source_task_id, "request_key": request_key,
                      "logical_generation_id": logical_id, "phase": "preparing", "child_task_id": ""}

            def claim(record: dict[str, Any]) -> None:
                record.update(status="running", error="")
                self._clear_pending(record)
                record[WRITER_RESULT_RESUME_KEY] = resume

            pipeline = self.store.update_pipeline(book_id, str(pipeline["id"]), claim,
                                                  expected_attempt=int(pipeline.get("attempt") or 0))
            if pipeline is None:
                raise WriterPipelineError("流水线状态已变化，请刷新后重试")
            self._submissions[key] = True
        child_id = ""
        accepted = False
        try:
            child = self.app.studio.jobs.prepare_resume(
                source_task_id, request_key=request_key,
                owner_id=str(source.get("owner_id") or ""), allow_writer=True,
            )
            child_id = str(child["id"])
            with self.store._lock:
                current = self._matching_pipeline(book_id, pipeline)
                if (current is not None and current.get("status") == "running"
                        and self._step_key(book_id, current) == key
                        and current.get("current_task_id") == source_task_id
                        and (current.get(WRITER_RESULT_RESUME_KEY) or {}).get("request_key") == request_key):
                    def bind(record: dict[str, Any]) -> None:
                        record["current_task_id"] = child_id
                        record[WRITER_RESULT_RESUME_KEY] = {**resume, "phase": "ready", "child_task_id": child_id}
                    updated = self.store.update_pipeline(book_id, str(current["id"]), bind,
                                                         expected_attempt=int(pipeline.get("attempt") or 0))
                    accepted = updated is not None
                    if updated is not None:
                        pipeline = updated
        except Exception as exc:
            self._fail(book_id, pipeline, f"恢复未完成：{exc}")
            raise
        finally:
            with self.store._lock:
                self._submissions.pop(key, None)
            if child_id and not accepted:
                self.app.studio.jobs.cancel(child_id)
            self._settle_aborted_submission(book_id, pipeline)
        if accepted:
            self.app.studio.jobs.runner.submit(child_id)
            self._wake.set()
        return self.store.get_active_pipeline(book_id) or pipeline

    # ---- 查询 ----

    def get_pipeline(self, book_id: str) -> dict[str, Any]:
        manifest = self.store._load_manifest(book_id)
        pipeline = manifest.get("active_pipeline")
        if not isinstance(pipeline, dict):
            return {"pipeline": None}
        payload = self.store.public_pipeline(pipeline)
        task_id = str(payload.get("current_task_id") or "")
        if task_id:
            task = self._task(task_id)
            if task is not None:
                payload["task_status"] = str(task.get("status") or "")
        return {"pipeline": payload}

    # ---- 推进核心 ----

    @staticmethod
    def _step_key(book_id: str, pipeline: dict[str, Any]) -> tuple[str, str, int, int]:
        return (book_id, str(pipeline.get("id") or ""), int(pipeline.get("attempt") or 0),
                int(pipeline.get("step_index") or 0))

    def _matching_pipeline(self, book_id: str, pipeline: dict[str, Any]) -> dict[str, Any] | None:
        """Caller holds the store lock; an obsolete snapshot never owns a newer step."""
        try:
            current = self.store.get_active_pipeline(book_id)
        except LookupError:
            return None
        if current is None or self._step_key(book_id, current) != self._step_key(book_id, pipeline):
            return None
        return current

    def _request_stop(self, book_id: str, pipeline: dict[str, Any], task_id: str) -> None:
        """Cancel outside the store lock and check task state instead of its boolean reply."""
        with self.store._lock:
            self._pending_stops[task_id] = (book_id, dict(pipeline))
        error = ""
        try:
            self.app.studio.jobs.cancel(task_id)
        except Exception as exc:  # noqa: BLE001 - Keep the handle for the next poll.
            error = str(exc)[:300]
        task = self._task(task_id)
        terminal = task is not None and str(task.get("status") or "") in {
            "done", "failed", "cancelled", "interrupted",
        }
        with self.store._lock:
            if terminal:
                self._pending_stops.pop(task_id, None)
            current = self._matching_pipeline(book_id, pipeline)
            if current is None or current.get("status") != WRITER_PIPELINE_STOPPING:
                return
            if str(current.get("current_task_id") or "") != task_id:
                return

            def _settle(record: dict[str, Any]) -> None:
                if terminal and self._step_key(book_id, record) not in self._submissions:
                    record["status"] = "cancelled"
                    record["error"] = "用户取消；当前任务已结束"
                    record["stop_requested_at"] = 0
                else:
                    record["error"] = "停止尚未确认；已阻止后续步骤" + (f"：{error}" if error else "")

            self.store.update_pipeline(book_id, str(current["id"]), _settle,
                                       expected_attempt=int(current.get("attempt") or 0))

    def _settle_aborted_submission(self, book_id: str, pipeline: dict[str, Any]) -> None:
        """Only the submit owner can prove an empty task ID means nothing was submitted."""
        with self.store._lock:
            current = self._matching_pipeline(book_id, pipeline)
            if current is None or current.get("status") != WRITER_PIPELINE_STOPPING:
                return
            task_id = str(current.get("current_task_id") or "")
            if not task_id and self._step_key(book_id, current) not in self._submissions:
                def _settle(record: dict[str, Any]) -> None:
                    record["status"] = "cancelled"
                    record["error"] = "用户取消；未提交新任务"
                self.store.update_pipeline(book_id, str(current["id"]), _settle,
                                           expected_attempt=int(current.get("attempt") or 0))
                return
        if task_id:
            self._request_stop(book_id, current, task_id)

    def _settle_overdue_stop(self, book_id: str, task_id: str) -> None:
        """停止确认有界：stopping 超过上限仍未收尾时强制落 cancelled，解除作品占用。

        常规收尾在 `_request_stop`（任务终态即确认）；本方法只接它接不住的两类残留：
        任务被环境级卡死迟迟不终态，或任务已终态但收尾被迟到的提交句柄挡住。
        老版本遗留的无计时 stopping 记录按 updated_at 兜底计时，升级后也能自愈。
        """
        with self.store._lock:
            current = self.store.get_active_pipeline(book_id)
            if not isinstance(current, dict) or current.get("status") != WRITER_PIPELINE_STOPPING:
                return
            if str(current.get("current_task_id") or "") != task_id:
                return
            requested_at = int(current.get("stop_requested_at") or current.get("updated_at") or 0)
            attempt = int(current.get("attempt") or 0)
            pipeline_id = str(current.get("id") or "")
        if not requested_at or int(time.time() * 1000) - requested_at < WRITER_STOP_CONFIRM_BOUND_MS:
            return

        def _force(record: dict[str, Any]) -> None:
            if record.get("status") != WRITER_PIPELINE_STOPPING:
                return
            record["status"] = "cancelled"
            record["error"] = "取消等待超时，已自动结束本次生成；已落盘的未完成 TXT 保留，可重新生成本章"
            record["stop_requested_at"] = 0

        self.store.update_pipeline(book_id, pipeline_id, _force, expected_attempt=attempt)

    def tick(self) -> None:
        """扫全部活动流水线：任务终态则推进；一次 tick 处理完当前所有可推进项。"""
        with self.store._lock:
            pending = list(self._pending_stops.items())
        for task_id, (book_id, pipeline) in pending:
            try:
                self._request_stop(book_id, pipeline, task_id)
            except Exception:  # noqa: BLE001 - Preserve the handle if writing its status fails.
                traceback.print_exc()
        for row in self.store.iter_active_pipelines():
            book_id = str(row.get("book_id") or "")
            pipeline = dict(row.get("pipeline") or {})
            if not book_id:
                continue
            try:
                if pipeline.get("status") == WRITER_PIPELINE_STOPPING:
                    task_id = str(pipeline.get("current_task_id") or "")
                    if task_id:
                        self._request_stop(book_id, pipeline, task_id)
                        self._settle_overdue_stop(book_id, task_id)
                    continue
                if pipeline.get("status") != "running":
                    continue
                self._advance(book_id, pipeline)
            except WriterConflictError as exc:
                self._fail(book_id, pipeline, str(exc), override_status="conflict")
            except (WriterStoreError, WriterPipelineError, prompts.PromptError) as exc:
                self._fail(book_id, pipeline, str(exc))
            except Exception as exc:  # noqa: BLE001 - 单本异常不拖垮其它作品
                traceback.print_exc()
                self._fail(book_id, pipeline, f"推进失败：{exc}")

    def _advance(self, book_id: str, pipeline: dict[str, Any]) -> None:
        with self.store._lock:
            current = self._matching_pipeline(book_id, pipeline)
            if current is None or current.get("status") != "running":
                return
            pipeline = current
            if self._step_key(book_id, pipeline) in self._submissions:
                return
        task_id = str(pipeline.get("current_task_id") or "")
        if not task_id:
            # 没有 current_task_id 却在 running：提交丢失（如重启竞态），如实失败。
            self._fail(book_id, pipeline, "流水线缺少当前任务，无法推进")
            return
        task = self._task(task_id)
        if task is None:
            self._fail(book_id, pipeline, "当前任务记录已丢失，无法推进")
            return
        status = str(task.get("status") or "")
        if status in ("queued", "running"):
            return
        # 条件 2：任务归属校验（book/pipeline/attempt/step/chapter 与流水线一致）。
        writer_input = self._writer_input(task)
        snapshot = pipeline.get("request_snapshot") if isinstance(pipeline.get("request_snapshot"), dict) else {}
        steps = [str(s) for s in pipeline.get("steps") or []]
        step_index = int(pipeline.get("step_index") or 0)
        step = steps[step_index] if 0 <= step_index < len(steps) else ""
        mismatch = (
            str(writer_input.get("book_id") or "") != book_id
            or str(writer_input.get("pipeline_id") or "") != str(pipeline.get("id") or "")
            or int(writer_input.get("attempt") or -1) != int(pipeline.get("attempt") or 0)
            or str(writer_input.get("step") or "") != step
            or ("step_index" in writer_input and writer_input["step_index"] != step_index)
            or int(writer_input.get("chapter_no") or 0) != int(snapshot.get("chapter_no") or 0)
        )
        if mismatch:
            self._fail(book_id, pipeline, "当前任务与流水线不一致（可能来自旧的尝试），已停止推进")
            return
        if status != "done":
            if status == "failed" and self._artifact_task(task) and task.get(STUDIO_RESULT_ISSUE_KEY) == "incomplete":
                reader = getattr(self.app.studio.jobs, "result_text", None)
                partial = ""
                if callable(reader):
                    try:
                        partial = reader(task_id, require_final=False)
                    except (ValueError, OSError) as exc:
                        self._fail(book_id, pipeline, f"TXT 结果不可用：{exc}")
                        return
                if partial:
                    issue = prompts.ContentIssue(
                        "模型没有确认 TXT 写完，已保留可核对的片段",
                        category="truncated", raw=partial,
                        partial=partial if self._decision_editor(step, snapshot) == "text" else None,
                    )
                    self._pause_for_user(book_id, pipeline, issue, resume_txt=True)
                    return
                # 没有可续写片段 = 模型这一趟什么都没确认落盘。先自动重试（瞬时抖动
                # 不该把用户一次次拽回来手动跳过），用完预算才暂停交给用户处理。
                if self._auto_retry_step(book_id, pipeline):
                    return
                issue = prompts.ContentIssue(
                    "模型没有写入可用内容" + self._retry_suffix(pipeline),
                    category="empty", raw="",
                )
                self._pause_for_user(book_id, pipeline, issue)
                return
            suffix = self._retry_suffix(pipeline) if status == "failed" else ""
            if step == "state" and status != "cancelled":
                # 线路错误仍是流水线失败，遇到状态提取失败不回滚已经写好的正文。
                self._fail_state(book_id, pipeline,
                                 (str(task.get("error") or "") or "章末状态提取未完成") + suffix)
                return
            self._fail(
                book_id, pipeline,
                (str(task.get("error") or "") or f"任务状态为 {status}，流水线停止") + suffix,
                override_status="cancelled" if status == "cancelled" else "failed",
            )
            return
        # 幂等：同一任务已写回过就不再写（崩溃恢复 / 重复回调安全）。
        if task_id in [str(t) for t in (pipeline.get("completed_task_ids") or [])]:
            self._finish_step(book_id, pipeline, task_id, output=None)
            return
        # 条件 3：输出非空 + 结构化解析 + 截断判定（解析失败 → 停链，保留任务原始结果）。
        try:
            reader = getattr(self.app.studio.jobs, "result_text", None)
            if callable(reader):
                result = reader(task_id, require_final=True)
            elif self._artifact_task(task):
                raise WriterPipelineError("TXT 结果读取接口不可用，不能回退旧正文")
            else:
                result = str(task.get("result") or "")
        except (ValueError, OSError) as exc:
            if step == "state":
                self._fail_state(book_id, pipeline, f"状态 TXT 结果不可用：{exc}")
            else:
                self._fail(book_id, pipeline, f"TXT 结果不可用：{exc}")
            return
        try:
            output = self._parse_step_output(step, result, pipeline=pipeline,
                                             finish_reason=str(task.get("finish_reason") or ""))
        except prompts.ContentIssue as exc:
            self._pause_for_user(book_id, pipeline, exc)
            return
        self._finish_step(book_id, pipeline, task_id, output=output)

    def _fail_state(self, book_id: str, pipeline: dict[str, Any], error: str) -> None:
        with self.store._lock:
            current = self._matching_pipeline(book_id, pipeline)
            if current is None or current.get("status") != "running" or current.get("current_task_id") != pipeline.get("current_task_id"):
                return
            self._check_step_source(book_id, current, "state", check_state=True)
            self.store.save_chapter_state(
                book_id, int(current["request_snapshot"]["chapter_no"]), state=None, status="failed",
                error=error, task_id=str(current.get("current_task_id") or ""),
                source_version_id=str(current.get("state_source_version_id") or ""),
            )
            self._fail(book_id, current, f"正文已保存，章末状态提取失败：{error}")

    @staticmethod
    def _retry_suffix(pipeline: dict[str, Any]) -> str:
        used = int(pipeline.get("step_auto_retries") or 0)
        return f"（已自动重试 {used} 次仍未成功）" if used > 0 else ""

    def _auto_retry_step(self, book_id: str, pipeline: dict[str, Any]) -> bool:
        """空结果自动重试：attempt+1 重提当前步（冻结输入不动），预算记在流水线上。

        与用户手动重试同构（attempt+1 使旧任务的迟到写回失效），但不算用户的
        重试次数；步推进或用户决策时预算清零。
        """
        used = int(pipeline.get("step_auto_retries") or 0)
        if used >= WRITER_STEP_AUTO_RETRIES:
            return False
        attempt = int(pipeline.get("attempt") or 0) + 1

        def retry(record: dict[str, Any]) -> None:
            if (record.get("status") != "running"
                    or self._step_key(book_id, record) != self._step_key(book_id, pipeline)):
                return
            record["attempt"] = attempt
            record["current_task_id"] = ""
            record["error"] = ""
            record["step_auto_retries"] = used + 1

        updated = self.store.update_pipeline(book_id, str(pipeline.get("id") or ""), retry,
                                             expected_attempt=int(pipeline.get("attempt") or 0))
        if updated is None:
            return False
        self._submissions[self._step_key(book_id, updated)] = False
        self._submit_step(book_id, updated)
        return True

    def _writer_input(self, task: dict[str, Any]) -> dict[str, Any]:
        input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
        writer = input_data.get("writer")
        return writer if isinstance(writer, dict) else {}

    @staticmethod
    def _artifact_task(task: dict[str, Any]) -> bool:
        from naiba.studio.artifacts import has_result_contract
        return has_result_contract(task)

    @staticmethod
    def _logical_id(task: dict[str, Any]) -> str:
        from naiba.core.contracts import STUDIO_LOGICAL_GENERATION_ID_KEY
        return str(task.get(STUDIO_LOGICAL_GENERATION_ID_KEY)
                   or (task.get("input") or {}).get(STUDIO_LOGICAL_GENERATION_ID_KEY) or task.get("id") or "")

    def _task(self, task_id: str) -> dict[str, Any] | None:
        try:
            return self.app.studio.jobs.task(task_id)
        except Exception:  # noqa: BLE001 - 任务存储异常按「查不到」处理
            return None

    @staticmethod
    def _decision_editor(step: str, snapshot: dict[str, Any]) -> str:
        if snapshot.get(WRITER_OUTPUT_MODE_KEY) == "freeform":
            # 自由文本拆章同样走章节行编辑器：原文要经用户确认成章节才进目录。
            return "chapters" if step == "split" else "text"
        return {"genre": "genre", "title": "list", "blurb": "list", "segs": "list",
                "chars": "characters", "split": "chapters", "outline": "chapters",
                "state": "state"}.get(step, "text")

    @staticmethod
    def _skip_consequence(step: str) -> str:
        return {
            "genre": "保留已保存的题材；没有题材时，后续生成不带题材。",
            "title": "不生成这批书名，已保存的书名不变。",
            "blurb": "不生成这批简介，已保存的简介不变。",
            "segs": "不采用段落要点，下一步直接生成整章正文。",
            "draft": "本章不保存这次生成的正文，也不提取这次的章末状态。已有正文保留。",
            "split": "跳过这批章号，已有章节保留；后续批次仍按原章号生成。",
            "state": "正文已保存；这次不补章末状态，连贯性检查只使用已有的有效状态。",
        }.get(step, "这一步的结果不会写入作品，之前保存的内容保留。")

    def _check_step_source(self, book_id: str, record: dict[str, Any], step: str,
                           *, check_state: bool = False) -> None:
        manifest = self.store._load_manifest(book_id)
        if record.get("base_revision") != manifest.get("revision"):
            raise WriterConflictError("作品已被修改过（版本冲突），请基于当前版本重新生成")
        snapshot = record.get("request_snapshot") or {}
        chapter_no = int(snapshot.get("chapter_no") or 0)
        if ((step in ("segs", "draft") and record.get("chapter_revision") is not None)
                or (step == "state" and check_state)):
            chapter = self.store._load_chapter_index(self.store._book_dir(book_id), chapter_no)
            if step == "state":
                source = str(record.get("state_source_version_id") or "")
                if not source or str(chapter.get("current_version_id") or "") != source:
                    raise WriterConflictError("章末状态对应的正文已变化，请从当前正文重建状态")
            elif int(chapter.get("revision") or 0) != record["chapter_revision"]:
                raise WriterConflictError(f"第 {chapter_no} 章已被修改过（版本冲突），请基于当前版本重新生成")

    def _pause_for_user(self, book_id: str, pipeline: dict[str, Any], issue: prompts.ContentIssue,
                        *, resume_txt: bool = False) -> None:
        index = pipeline["step_index"]
        step = pipeline["steps"][index]
        snapshot = pipeline["request_snapshot"]
        partial = issue.partial
        partial_truncated = issue.partial_truncated
        if partial is not None and not isinstance(partial, str):
            try:
                if len(json.dumps(partial, ensure_ascii=False)) > DECISION_CONTENT_LIMIT:
                    partial, partial_truncated = None, True
            except (TypeError, ValueError, RecursionError):
                partial, partial_truncated = None, True
        accepted = issue.accepted
        if accepted is not None and (
            len(json.dumps(accepted, ensure_ascii=False)) > DECISION_CONTENT_LIMIT
            or (step == "chars" and len(normalize_setup({"characters": accepted})["characters"]) != len(accepted))
        ):
            accepted = None
        pending: WriterPendingUser = {
            "step": step, "step_index": index, "attempt": pipeline["attempt"],
            "issue": str(issue)[:500], "category": issue.category,
            "raw": issue.raw, "raw_truncated": issue.raw_truncated,
            "partial": partial, "partial_truncated": partial_truncated,
            "missing": issue.missing, "counts": issue.counts,
            "available_actions": ["retry", "supplement", *(["accept"] if accepted is not None else []), "skip"],
            "skip_consequence": self._skip_consequence(step),
            "editor": self._decision_editor(step, snapshot),
            "body_saved": step == "state" and bool(pipeline.get("state_source_version_id")),
            "chapter_no": int(snapshot.get("chapter_no") or 0),
            "part_no": self._resolve_part(snapshot, index)[0] if step in ("outline_text", "outline_review", "split") else 0,
            "resume_txt": resume_txt,
        }
        task_id = str(pipeline.get("current_task_id") or "")

        def pause(record: dict[str, Any]) -> None:
            if (record.get("status") != "running"
                    or self._step_key(book_id, record) != self._step_key(book_id, pipeline)
                    or str(record.get("current_task_id") or "") != task_id):
                return
            self._check_step_source(book_id, record, step, check_state=True)
            record.update(status=WRITER_PIPELINE_NEEDS_USER, error=str(issue)[:500])
            record[WRITER_PENDING_USER_KEY] = pending
            record[WRITER_PENDING_ACCEPTED_KEY] = accepted
            record[WRITER_DECISION_REVISION_KEY] = int(record.get(WRITER_DECISION_REVISION_KEY) or 0) + 1

        self.store.update_pipeline(book_id, pipeline["id"], pause, expected_attempt=pipeline["attempt"])

    @staticmethod
    def _clear_pending(record: dict[str, Any]) -> None:
        record[WRITER_PENDING_USER_KEY] = None
        record.pop(WRITER_PENDING_ACCEPTED_KEY, None)

    @staticmethod
    def _check_decision_identity(record: dict[str, Any], body: dict[str, Any]) -> None:
        if any(type(body.get(key)) is not int or body[key] != record.get(key)
               for key in ("step_index", "attempt", WRITER_DECISION_REVISION_KEY)):
            raise WriterConflictError("生成阶段或待处理内容已变化，请重新打开作品后再操作")

    def decide_pipeline(self, book_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Commit the action, version writes and receipt together; submit outside the lock."""
        from naiba.writer import drafts

        action = body.get("action")
        request_key = body.get(WRITER_DECISION_REQUEST_KEY)
        if not isinstance(action, str) or action not in WRITER_DECISION_ACTIONS:
            raise WriterStoreError("未知的阶段处理操作")
        if not isinstance(request_key, str) or not DECISION_KEY_PATTERN.fullmatch(request_key):
            raise WriterStoreError("request_key 必须为 8–80 位字母、数字、下划线或连字符")
        reason = body.get("reason", "")
        if (not isinstance(reason, str) or len(reason) > 300
                or (action == "skip" and (body.get("confirmed") is not True or not reason.strip()))):
            raise WriterStoreError("跳过前须明确确认，原因须为 1–300 字")
        keys = ("action", "pipeline_id", "step", "step_index", "attempt", WRITER_DECISION_REVISION_KEY,
                "content", "confirmed", "reason")
        try:
            encoded = json.dumps({key: body.get(key) for key in keys}, ensure_ascii=True, sort_keys=True,
                                 allow_nan=False, separators=(",", ":"))
        except (ValueError, TypeError, RecursionError) as exc:
            raise WriterStoreError("编辑内容格式不正确") from exc
        if len(encoded) > 1_600_000:
            raise WriterStoreError("编辑内容过大，请删减后再提交")
        digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        cleanup = []
        with self.store._lock:
            pipeline = self.store.get_active_pipeline(book_id)
            if not pipeline or pipeline.get("id") != body.get("pipeline_id"):
                raise WriterConflictError("流水线已变化，请重新打开作品")
            history = list(pipeline.get(WRITER_DECISION_HISTORY_KEY) or [])
            for receipt in history:
                if receipt.get(WRITER_DECISION_REQUEST_KEY) == request_key:
                    if receipt.get(WRITER_DECISION_DIGEST_KEY) != digest:
                        raise WriterConflictError("此请求标识已用于不同的处理操作")
                    return {"pipeline": self.store.public_pipeline(pipeline), "decision": self.store.public_decision(receipt)}
            pending = pipeline.get(WRITER_PENDING_USER_KEY)
            if pipeline.get("status") != WRITER_PIPELINE_NEEDS_USER or not isinstance(pending, dict):
                raise WriterConflictError("当前步骤已不在待处理状态，请重新打开作品")
            self._check_decision_identity(pipeline, body)
            step = pending["step"]
            if body.get("step") != step:
                raise WriterConflictError("当前步骤已变化，请重新打开作品")
            if action not in pending["available_actions"]:
                raise WriterStoreError("没有可直接接受的完整条目，请补充、重试或跳过")
            try:
                self._check_step_source(book_id, pipeline, step, check_state=True)
            except WriterConflictError as exc:
                def conflict(record: dict[str, Any]) -> None:
                    record.update(status="conflict", error=str(exc))
                self.store.update_pipeline(book_id, pipeline["id"], conflict, expected_attempt=pipeline["attempt"])
                raise
            output = None
            if action == "supplement":
                output = self._supplement_output(step, body.get("content"), pipeline)
            elif action == "accept":
                output = pipeline.get(WRITER_PENDING_ACCEPTED_KEY)
                if output is None:
                    raise WriterStoreError("没有可直接接受的部分结果")
            if step == "chars" and isinstance(output, list):
                if len(normalize_setup({"characters": output})["characters"]) != len(output):
                    raise WriterStoreError("角色数量超过主角或配角上限，请调整角色分组或数量")
            receipt = {"action": action, WRITER_DECISION_REQUEST_KEY: request_key, WRITER_DECISION_DIGEST_KEY: digest,
                       "step": step, "step_index": pipeline["step_index"], "attempt": pipeline["attempt"],
                       "at": int(time.time() * 1000), "reason": reason.strip() if action == "skip" else "",
                       "counts": pending["counts"], "missing": pending["missing"],
                       "consequence": pending["skip_consequence"] if action == "skip" else "",
                       "chapter_no": pending["chapter_no"], "part_no": pending["part_no"]}

            def apply(record: dict[str, Any]) -> None:
                record[WRITER_DECISION_HISTORY_KEY] = (history + [receipt])[-DECISION_HISTORY_LIMIT:]
                self._clear_pending(record)
                record.update(status="running", error="", current_task_id="", step_auto_retries=0)
                record.pop(WRITER_RESULT_RESUME_KEY, None)
                if action == "retry":
                    record["attempt"] += 1
                    return
                next_index = record["step_index"] + 1
                write_id = f"writer-decision:{record['id']}:{pending['attempt']}:{pending['step_index']}"
                if (step == "split" and action in ("supplement", "skip")
                        and str((record.get("request_snapshot") or {}).get(WRITER_OUTPUT_MODE_KEY)
                                or "json") == "freeform"
                        and str(pending.get("raw") or "").strip()):
                    # 自由文本拆章的原文先归档进「自由文本输出」，登记/跳过都不丢原文；
                    # 重试不归档（旧稿随重试作废）。此处先行 bump 设定版本，
                    # base_revision 由末尾 _advance_record 统一刷新。
                    self._save_freeform_output(book_id, record, step, str(pending["raw"]).strip(),
                                               write_id, kind="generated")
                if action == "skip":
                    if step == "segs":
                        record.setdefault("step_outputs", {})["segs"] = []
                    elif step == "draft" and record["steps"][next_index:next_index + 1] == ["state"]:
                        next_index += 1
                else:
                    self._apply_step_output(book_id, record, step, output, write_id, kind="manual",
                                            cleanup=cleanup)
                self._advance_record(book_id, record, step, next_index)

            with self.store.writeback_transaction(book_id):
                updated = self.store.update_pipeline(book_id, pipeline["id"], apply,
                                                     expected_attempt=pipeline["attempt"])
            if updated is None:
                raise WriterConflictError("流水线状态已变化，请重新打开作品")
            if updated.get("status") == "running":
                self._submissions[self._step_key(book_id, updated)] = False
        drafts.cleanup(cleanup)
        if updated.get("status") == "running":
            try:
                self._submit_step(book_id, updated)
            except Exception:  # _submit_step records the error; the decision is already committed.
                traceback.print_exc()
        latest = self.store.get_active_pipeline(book_id)
        if latest is not None and latest.get("id") == updated.get("id"):
            updated = latest
        return {"pipeline": self.store.public_pipeline(updated), "decision": self.store.public_decision(receipt)}

    def _supplement_output(self, step: str, content: Any, pipeline: dict[str, Any]) -> Any:
        snapshot = pipeline["request_snapshot"]
        editor = self._decision_editor(step, snapshot)
        if content is None or (editor == "text" and not isinstance(content, str)):
            raise WriterStoreError("请填写当前阶段的内容")
        if editor == "text":
            raw = content
        elif isinstance(content, str) and step in ("title", "blurb"):
            raw = json.dumps([content], ensure_ascii=True)
        else:
            raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if len(raw) > DECISION_CONTENT_LIMIT:
            raise WriterStoreError("编辑内容过大，请删减后再提交")
        if editor == "characters":
            wanted = min(LEAD_MAX + SIDE_MAX,
                         int(snapshot.get("char_count") or 4) + max(0, int(snapshot.get("side_count") or 0)))
            return prompts.parse_characters(raw, wanted=wanted, require_count=False)
        if editor == "chapters" and step == "outline":
            return prompts.parse_outline(raw, wanted=int(snapshot.get("chapter_count") or 10), require_count=False)
        if editor == "chapters" and step == "split":
            start, amount = self._split_slice(snapshot, pipeline["step_index"])
            return prompts.parse_split(raw, from_no=start, want_count=amount)
        if step == "segs" and editor == "list":
            return prompts.parse_segs(raw, min_count=1)
        if step == "draft":
            return raw if raw.strip() else prompts.parse_draft(raw)
        return self._parse_step_output(step, raw, pipeline=pipeline, finish_reason="")

    def _fail(self, book_id: str, pipeline: dict[str, Any], error: str,
              *, override_status: str = "failed") -> None:
        status = override_status if override_status in ("failed", "cancelled", "conflict") else "failed"

        def _mutate(record: dict[str, Any]) -> None:
            if str(record.get("status") or "") != "running":
                return
            if self._step_key(book_id, record) != self._step_key(book_id, pipeline):
                return
            if str(record.get("current_task_id") or "") != str(pipeline.get("current_task_id") or ""):
                return
            record["status"] = status
            record["error"] = str(error or "")[:500]

        self.store.update_pipeline(
            book_id, str(pipeline.get("id") or ""), _mutate,
            expected_attempt=int(pipeline.get("attempt") or 0),
        )

    # ---- 步骤输出解析（条件 3） ----

    def _pipeline_setup(self, book_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """续写使用起点实际设定；旧任务只有可独立恢复的版本才允许原样重试。"""
        frozen = snapshot.get(WRITER_SETUP_SNAPSHOT_KEY)
        if isinstance(frozen, dict):
            return copy.deepcopy(frozen)
        version_id = str(snapshot.get("setup_version_id") or "")
        manifest = self.store._load_manifest(book_id)
        if isinstance(manifest.get("continuation"), dict):
            # 兼容自带摘要的旧版本；缺字段时无法知道任务起点曾补入哪一版摘要。
            if version_id:
                record = self.store._read_json(
                    self.store._setup_path(self.store._book_dir(book_id), version_id), what="设定版本")
                if isinstance(record.get("continuation_summary"), dict):
                    return record
            raise WriterPipelineError("这条旧续写任务未保存完整设定快照，请选择「基于当前版本重新生成」后重试")
        return self.store.load_setup(book_id, manifest=manifest, version_id=version_id)

    def _accumulated_setup(self, book_id: str, pipeline: dict[str, Any]) -> dict[str, Any]:
        """链内已积累的设定内容：起点 = 快照里的设定版本，之后逐步合并 step_outputs。"""
        snapshot = pipeline.get("request_snapshot") if isinstance(pipeline.get("request_snapshot"), dict) else {}
        try:
            setup = self._pipeline_setup(book_id, snapshot)
        except (LookupError, WriterStoreError):
            setup = {}
        outputs = pipeline.get("step_outputs") if isinstance(pipeline.get("step_outputs"), dict) else {}
        accumulated = outputs.get("setup")
        if isinstance(accumulated, dict):
            merged = dict(accumulated)
        else:
            merged = {
                "idea": setup.get("idea"), "genre": setup.get("genre"),
                "genre_tags": setup.get("genre_tags"), "title": setup.get("title"),
                "title_candidates": setup.get("title_candidates"), "blurb": setup.get("blurb"),
                "blurb_candidates": setup.get("blurb_candidates"),
                "worldview": setup.get("worldview"), "characters": setup.get("characters"),
                "style": setup.get("style"), "constraints": setup.get("constraints"),
                "references": setup.get("references"),
                "freeform_outputs": list(setup.get("freeform_outputs") or []),
            }
        if "continuation_summary" in setup:
            merged["continuation_summary"] = copy.deepcopy(setup["continuation_summary"])
        return merged

    def _parse_step_output(self, step: str, result: str, *, pipeline: dict[str, Any],
                           finish_reason: str) -> Any:
        snapshot = pipeline.get("request_snapshot") if isinstance(pipeline.get("request_snapshot"), dict) else {}
        truncated = str(finish_reason or "").strip().lower() in {"length", "max_tokens", "max_output_tokens"}
        try:
            output = self._parse_step_output_inner(
                step, result, snapshot, finish_reason=finish_reason,
                step_index=int(pipeline.get("step_index") or 0),
            )
        except prompts.ContentIssue as exc:
            if truncated and step != "draft":
                raise prompts.ContentIssue("输出在中途被截断，请补充或重试", category="truncated",
                                          raw=result, partial=exc.partial, missing=exc.missing,
                                          counts=exc.counts, accepted=exc.accepted) from exc
            raise
        if truncated:
            raise prompts.ContentIssue("输出在中途被截断，请补充或重试", category="truncated",
                                      raw=result, partial=output)
        return output

    def _parse_step_output_inner(self, step: str, result: str, snapshot: dict[str, Any],
                                 *, finish_reason: str = "", step_index: int = 0) -> Any:
        output_mode = str(snapshot.get(WRITER_OUTPUT_MODE_KEY) or "json")
        if step == "genre":
            return prompts.parse_freeform(result, limit=SETUP_TEXT_MAX_CHARS) if output_mode == "freeform" else prompts.parse_genre(result)
        if step == "title":
            return prompts.parse_freeform(result, limit=SETUP_TEXT_MAX_CHARS) if output_mode == "freeform" else prompts.parse_title_candidates(result)
        if step == "blurb":
            return prompts.parse_freeform(result, limit=SETUP_TEXT_MAX_CHARS) if output_mode == "freeform" else prompts.parse_blurb_candidates(result)
        if step == "world":
            return prompts.parse_world(result)
        if step == "chars":
            if output_mode == "freeform":
                return prompts.parse_freeform(result)
            snapshot_side = int(snapshot.get("side_count") or 0)
            return prompts.parse_characters(
                result,
                wanted=int(snapshot.get("char_count") or 4) + max(0, snapshot_side),
            )
        if step == "outline_text" or step == "outline_review":
            # 纯文本大纲本来就是自由内容，结构化模式也保留原文。
            return prompts.parse_outline_text(result)
        if step == "outline":
            if output_mode == "freeform":
                return prompts.parse_freeform(result)
            return prompts.parse_outline(result, wanted=int(snapshot.get("chapter_count") or 10))
        if step == "split":
            # 每步只拆自己那一批：from_no/want_count 按步切，不是快照里的总任务。
            from_no, want_count = self._split_slice(snapshot, step_index)
            if output_mode == "freeform":
                # 自由文本不直接收尾：原文转待处理，确认成章节行后才落目录。
                raise prompts.confirm_freeform_split(result, from_no=from_no, want_count=want_count)
            return prompts.parse_split(result, from_no=from_no, want_count=want_count, require_count=True)
        if step == "segs":
            if output_mode == "freeform":
                return prompts.parse_freeform(result, limit=SETUP_TEXT_MAX_CHARS)
            return prompts.parse_segs(result)
        if step == "draft":
            return prompts.parse_draft(result, finish_reason=finish_reason)
        if step == "state":
            if output_mode == "freeform":
                return prompts.parse_freeform(result, limit=SETUP_TEXT_MAX_CHARS)
            return prompts.parse_chapter_state(result)
        raise WriterPipelineError(f"未知步骤：{step}")

    # ---- 写回与前进（条件 4/5，单次加锁内原子完成） ----

    def _finish_step(self, book_id: str, pipeline: dict[str, Any], task_id: str,
                     *, output: Any) -> None:
        """锁内完成：条件校验 → 写回本步成果 → 记录完成 → 前进/收尾。

        条件 4（流水线仍活动 + revision 一致）与条件 5（原子写回）必须在同一次
        store 锁内完成，否则校验与写回之间可能插入别人的修改。校验先于一切写回；
        流水线自身的写回会推进作品 revision，因此写完后把 base_revision 刷新为
        写回后的权威值——别人的并发修改在下一次条件校验中被发现。
        """
        task = self._task(task_id) or {}
        logical_id = self._logical_id(task) or task_id
        pipeline_id = str(pipeline.get("id") or "")
        attempt = int(pipeline.get("attempt") or 0)
        steps = [str(s) for s in pipeline.get("steps") or []]
        step_index = int(pipeline.get("step_index") or 0)
        step = steps[step_index] if 0 <= step_index < len(steps) else ""
        completed = [str(t) for t in (pipeline.get("completed_task_ids") or [])]
        if task_id and task_id not in completed:
            completed.append(task_id)
        next_index = step_index + 1
        applied = False

        def _mutate(record: dict[str, Any]) -> None:
            nonlocal applied
            # ---- 条件 4：锁内确认仍是本 attempt 的活动流水线，且来源版本未漂移 ----
            if (str(record.get("status") or "") != "running"
                    or int(record.get("step_index") or 0) != step_index
                    or str(record.get("current_task_id") or "") != task_id):
                return
            self._check_step_source(book_id, record, step)
            if output is not None:
                self._apply_step_output(book_id, record, step, output, logical_id)
            record["completed_task_ids"] = completed
            generations = list(record.get(WRITER_COMPLETED_GENERATIONS_KEY) or [])
            if logical_id and logical_id not in generations:
                generations.append(logical_id)
            record[WRITER_COMPLETED_GENERATIONS_KEY] = generations
            self._advance_record(book_id, record, step, next_index)
            applied = True

        transaction = self.store.writeback_transaction(book_id) if self._artifact_task(task) else nullcontext()
        with self.store._lock:
            with transaction:
                updated = self.store.update_pipeline(book_id, pipeline_id, _mutate, expected_attempt=attempt)
            if updated is None or not applied:
                return
            if str(updated.get("status") or "") == "running":
                self._submissions[self._step_key(book_id, updated)] = False
        if str(updated.get("status") or "") == "running":
            self._submit_step(book_id, updated)

    def _advance_record(self, book_id: str, record: dict[str, Any], step: str, next_index: int) -> None:
        record["current_task_id"] = ""
        record["step_index"] = next_index
        record["step_auto_retries"] = 0
        if next_index >= len(record["steps"]):
            record["status"] = "done"
        refreshed = self.store._load_manifest(book_id)
        record["base_revision"] = int(refreshed.get("revision") or 0)
        if step in ("segs", "draft") and record.get("chapter_revision") is not None:
            chapter_no = int(record["request_snapshot"].get("chapter_no") or 0)
            chapter = self.store._load_chapter_index(self.store._book_dir(book_id), chapter_no)
            record["chapter_revision"] = int(chapter.get("revision") or 0)

    def _apply_step_output(self, book_id: str, record: dict[str, Any], step: str,
                           output: Any, task_id: str, *, kind: str = "generated", cleanup=None) -> None:
        snapshot = record["request_snapshot"]
        freeform = snapshot.get(WRITER_OUTPUT_MODE_KEY) == "freeform"
        if step == "segs":
            record.setdefault("step_outputs", {})["segs"] = [str(output)] if freeform else list(output)
        elif step == "draft":
            self._apply_draft_step(book_id, record, output, task_id, kind=kind, cleanup=cleanup)
        elif step == "state":
            if freeform:
                self._save_freeform_output(book_id, record, step, str(output), task_id, kind=kind)
            else:
                self.store.save_chapter_state(
                    book_id, int(snapshot["chapter_no"]), state=output, task_id=task_id,
                    source_version_id=str(record.get("state_source_version_id") or ""),
                )
        else:
            self._apply_setup_or_outline_step(book_id, record, step, output, task_id, kind=kind)

    def _apply_setup_or_outline_step(self, book_id: str, record: dict[str, Any],
                                     step: str, output: Any, task_id: str, *, kind: str) -> None:
        """设定六步的写回（锁内调用）：每步生成一份新设定版本；大纲步另落大纲版本并同步章节。"""
        snapshot = record.get("request_snapshot") if isinstance(record.get("request_snapshot"), dict) else {}
        if step == "outline":
            if snapshot.get(WRITER_OUTPUT_MODE_KEY) == "freeform":
                self._save_freeform_output(book_id, record, step, str(output), task_id, kind=kind)
                return
            source = record.get("source_version_ids") if isinstance(record.get("source_version_ids"), dict) else {}
            self.store.save_outline(
                book_id, chapters=output, kind=kind, task_id=task_id,
                source_setup_version_id=str(source.get("setup") or ""),
                sync=kind == "generated", allow_drop=kind == "generated" and bool(snapshot.get("allow_drop")),
            )
            if kind == "manual":
                self.store.append_chapters(book_id, output)
            return
        if step in ("outline_text", "outline_review"):
            # 只替换这一部，其它部原样保留（多部作品一部一部补）。
            # 空部也要留：部目录（计划分几部排出来的标题骨架）不含正文，但它是
            # 用户排好的结构——第三次反馈②里「生成一部、其他六部目录消失」就是
            # 这里把没正文的部当成垃圾丢掉造成的。
            # 调用时 record.step_index 仍指向刚完成的步，按步取部即可。
            source = record.get("source_version_ids") if isinstance(record.get("source_version_ids"), dict) else {}
            part_no, part_title = self._resolve_part(snapshot, int(record.get("step_index") or 0))
            current = self.store.load_outline(book_id)
            by_no: dict[int, dict[str, Any]] = {}
            for row in current.get("parts") or []:
                if not isinstance(row, dict):
                    continue
                no = int(row.get("no") or 0)
                if no < 1:
                    continue
                by_no[no] = {"no": no, "title": str(row.get("title") or ""),
                             "text": str(row.get("text") or "")}
            # 部清单（part_plan）是最新鲜的目录骨架：outline_all / outline_review 会带
            # 全部各部的标题，先铺进来，没生成到的部也有名字。
            for row in snapshot.get("part_plan") or []:
                if not isinstance(row, dict):
                    continue
                no = int(row.get("no") or 0)
                if no < 1:
                    continue
                title = str(row.get("title") or "").strip()
                if title or no not in by_no:
                    by_no[no] = {"no": no, "title": title or f"第 {no} 部",
                                 "text": by_no.get(no, {}).get("text", "")}
            title = part_title.strip() or (
                by_no.get(part_no, {}).get("title") or f"第 {part_no} 部")
            by_no[part_no] = {"no": part_no, "title": title, "text": str(output)}
            # 补齐中间空部（先生成第 2 部时第 1 部留空占位，保证部号连续）。
            filled: list[dict[str, Any]] = []
            for no in range(1, max(by_no) + 1):
                filled.append(by_no.get(no) or {"no": no, "title": f"第 {no} 部", "text": ""})
            self.store.save_outline(
                book_id, parts=filled, kind=kind, task_id=task_id,
                source_setup_version_id=str(source.get("setup") or ""),
            )
            return
        if step == "split":
            if (str(snapshot.get(WRITER_OUTPUT_MODE_KEY) or "json") == "freeform"
                    and not isinstance(output, list)):
                # 自由文本原始文字只存档（防御路径：确认链路上生成结果会先转待处理）；
                # 用户确认后的章节行（list）仍要落目录。
                self._save_freeform_output(book_id, record, step, str(output), task_id, kind=kind)
                return
            self.store.append_chapters(
                book_id, output, part_no=int(snapshot.get("part_no") or 0),
            )
            return
        merged = self._accumulated_setup(book_id, record)
        if str(snapshot.get(WRITER_OUTPUT_MODE_KEY) or "json") == "freeform":
            self._save_freeform_output(book_id, record, step, str(output), task_id, kind=kind)
            return
        if step == "genre":
            genre = dict(merged.get("genre") if isinstance(merged.get("genre"), dict) else {})
            merged["genre"] = {
                **genre,
                "channel": output.get("channel") or genre.get("channel") or "",
                "cat": output.get("cat") or genre.get("cat") or "",
                "sub_cat": output.get("sub_cat") or genre.get("sub_cat") or "",
                "tone": output.get("tone") or genre.get("tone") or "",
                "hook": output.get("hook") or genre.get("hook") or "",
            }
            merged["genre_tags"] = list(output.get("tags") or [])
        elif step == "title":
            candidates = [str(c) for c in output]
            merged["title_candidates"] = candidates
            if candidates and (kind == "manual" or not str(merged.get("title") or "").strip()):
                merged["title"] = candidates[0]
        elif step == "blurb":
            candidates = [str(c) for c in output]
            merged["blurb_candidates"] = candidates
            if candidates and (kind == "manual" or not str(merged.get("blurb") or "").strip()):
                merged["blurb"] = candidates[0]
        elif step == "world":
            merged["worldview"] = str(output)
        elif step == "chars":
            merged["characters"] = [dict(row) for row in output]
        outputs = dict(record.get("step_outputs") or {})
        outputs["setup"] = merged
        record["step_outputs"] = outputs
        self.store.save_setup(book_id, content=merged, kind=kind, task_id=task_id)

    @staticmethod
    def _continuation_baseline(snapshot: dict[str, Any]) -> dict[str, Any]:
        baseline = snapshot.get(WRITER_CHAPTER_BASELINE_KEY)
        if (not isinstance(baseline, dict)
                or baseline.get("chapter_no") != snapshot.get("chapter_no")
                or not isinstance(baseline.get("content"), str)
                or not baseline["content"].strip()
                or not isinstance(baseline.get("revision"), int)
                or not baseline.get("current_version_id")):
            raise WriterPipelineError("续写缺少完整正文基线；请显式基于当前版本重新生成")
        return baseline

    def _apply_draft_step(self, book_id: str, record: dict[str, Any],
                          output: str, task_id: str, *, kind: str, cleanup=None) -> None:
        snapshot = record.get("request_snapshot") if isinstance(record.get("request_snapshot"), dict) else {}
        chapter_no = int(snapshot.get("chapter_no") or 0)
        source = record.get("source_version_ids") if isinstance(record.get("source_version_ids"), dict) else {}
        content = str(output)
        if record.get("kind") == "chapter_continue":
            baseline = self._continuation_baseline(snapshot)
            if baseline["revision"] != record.get("chapter_revision"):
                raise WriterConflictError("续写基线 revision 不一致，结果不写回")
            # 保留基线与增量全部字节，只补足段落边界已有换行之外的部分。
            base = baseline["content"]
            tail_breaks = base[len(base.rstrip("\r\n")):]
            head_breaks = content[:len(content) - len(content.lstrip("\r\n"))]
            trailing = len(tail_breaks.replace("\r\n", "\n"))
            leading = len(head_breaks.replace("\r\n", "\n"))
            separator = "\n" * max(0, 2 - trailing - leading)
            content = base + separator + content
        if kind == "manual":
            from naiba.writer.drafts import save as save_manual_draft

            save_manual_draft(self.store, book_id, chapter_no, content=content,
                              expected_revision=record.get("chapter_revision"), cleanup_after_commit=cleanup)
            return
        self.store.save_chapter_version(
            book_id, chapter_no, content=content, kind=kind, task_id=task_id,
            source_setup_version_id=str(source.get("setup") or ""),
            source_outline_version_id=str(source.get("outline") or ""),
            expected_revision=record.get("chapter_revision"),
        )

    def _save_freeform_output(self, book_id: str, record: dict[str, Any],
                              step: str, text: str, task_id: str, *, kind: str) -> None:
        setup = self._accumulated_setup(book_id, record)
        rows = list(setup.get("freeform_outputs") or [])
        rows.append({"step": step, "label": prompts.STEP_LABELS.get(step, step),
                     "text": text, "task_id": task_id, "updated_at": int(time.time() * 1000)})
        setup["freeform_outputs"] = rows[-30:]
        record.setdefault("step_outputs", {})["setup"] = setup
        self.store.save_setup(book_id, content=setup, kind=kind, task_id=task_id)

    # ---- 任务提交 ----

    def _resolve_part(self, snapshot: dict[str, Any], step_index: int) -> tuple[int, str]:
        """当前步要生成哪一部：outline_all 按步取 part_plan；单部流水线用快照里的部号。"""
        plan = snapshot.get("part_plan") if isinstance(snapshot.get("part_plan"), list) else []
        row = plan[step_index] if 0 <= step_index < len(plan) and isinstance(plan[step_index], dict) else {}
        part_no = int(row.get("no") or 0) or int(snapshot.get("part_no") or 1)
        part_title = str(row.get("title") or "").strip() or str(snapshot.get("part_title") or "").strip()
        return part_no, part_title

    @staticmethod
    def _chapter_for_step(snapshot: dict[str, Any], step_index: int, kind: str) -> int:
        """当前步对应哪一章：chapter_all 的步骤是 (draft, state) × N 交错
        （两步同章，按下标折半）；state_rebuild 每步一章。越界兜底按 from_no 顺延。"""
        plan = snapshot.get("chapter_plan") if isinstance(snapshot.get("chapter_plan"), list) else []
        offset = step_index if kind == "state_rebuild" else step_index // 2
        row = plan[offset] if 0 <= offset < len(plan) and isinstance(plan[offset], dict) else {}
        no = int(row.get("no") or 0)
        if no >= 1:
            return no
        return int(snapshot.get("from_no") or 1) + offset

    def _submit_step(self, book_id: str, pipeline: dict[str, Any]) -> str:
        """Claim the reserved step once, and clean up even if preparation or persistence fails."""
        key = self._step_key(book_id, pipeline)
        with self.store._lock:
            current = self._matching_pipeline(book_id, pipeline)
            if current is None or current.get("status") != "running":
                if self._submissions.get(key) is True:
                    return ""  # Another submit caller still owns the in-flight handle.
                self._submissions.pop(key, None)
                skipped = True
            else:
                skipped = False
                if current.get("current_task_id") or self._submissions.get(key) is True:
                    return str(current.get("current_task_id") or "")
                self._submissions[key] = True
                pipeline = current
        if skipped:
            self._settle_aborted_submission(book_id, pipeline)
            return ""
        try:
            return self._submit_step_task(book_id, pipeline)
        except Exception as exc:
            try:
                self._fail(book_id, pipeline, f"提交失败：{exc}")
            except (LookupError, WriterStoreError, OSError):
                pass
            raise
        finally:
            with self.store._lock:
                self._submissions.pop(key, None)
            self._settle_aborted_submission(book_id, pipeline)

    def prepare_step_input(self, book_id: str, pipeline: dict[str, Any]) -> dict[str, Any]:
        """只读组装当前步；正式提交和预览使用完全相同的输入与预算。"""
        snapshot = pipeline.get("request_snapshot") if isinstance(pipeline.get("request_snapshot"), dict) else {}
        steps = [str(s) for s in pipeline.get("steps") or []]
        step_index = int(pipeline.get("step_index") or 0)
        step = steps[step_index] if 0 <= step_index < len(steps) else ""
        if not step:
            raise WriterPipelineError("流水线没有待执行的步骤")
        # chapter_all（方案A）：本步的章号/基线/前章要在提交时同锁落盘，
        # 供 _advance 的归属校验与写回冲突检查按同一章对齐。
        pending_step_update: dict[str, Any] | None = None
        pending_chapter_revision: int | None = None
        # state 步提交时读到的来源版本：写回时固化进状态记录，
        # 章节中途被改也不会把状态错挂到新版本上。
        pending_state_version: str | None = None
        book_title = str(snapshot.get("book_title") or "未命名作品")
        extra_note = str(snapshot.get("extra_note") or "")
        chapter_no = int(snapshot.get("chapter_no") or 0)
        output_mode = str(snapshot.get(WRITER_OUTPUT_MODE_KEY) or "json")
        system_text = prompts.step_system(step, snapshot.get(WORK_GLOBAL_SYSTEM_PROMPT_KEY, ""))
        chunked: dict[str, Any] | None = None
        if step == "outline_text":
            setup = self._accumulated_setup(book_id, pipeline)
            part_no, part_title = self._resolve_part(snapshot, step_index)
            prev_parts: list[dict[str, Any]] = []
            if str(pipeline.get("kind") or "") == "outline_all":
                # 一次生成全部：带上已定稿的前几部（本步之前各步已写回），保证互相衔接。
                try:
                    current = self.store.load_outline(book_id)
                except (LookupError, WriterStoreError):
                    current = {}
                for row in current.get("parts") or []:
                    if not isinstance(row, dict) or not str(row.get("text") or "").strip():
                        continue
                    if int(row.get("no") or 0) < part_no:
                        prev_parts.append({
                            "no": int(row.get("no") or 0),
                            "title": str(row.get("title") or ""),
                            "text": str(row.get("text") or "")[:1200],
                        })
            content = prompts.build_outline_text_user(
                setup,
                part_no=part_no,
                part_title=part_title,
                target_chars=int(snapshot.get("target_chars") or 8000),
                extra_note=extra_note,
                prev_parts=prev_parts,
                output_mode_value=output_mode,
            )
            task_title = f"《{book_title}》· 第 {part_no} 部大纲"
        elif step == "outline_review":
            # 多部复核梳理：本部全文 + 其余各部节选（前后都要看），让模型只修这一部。
            setup = self._accumulated_setup(book_id, pipeline)
            part_no, part_title = self._resolve_part(snapshot, step_index)
            outline = self.store.load_outline(book_id)
            rows = [row for row in (outline.get("parts") or []) if isinstance(row, dict)]
            part = next((row for row in rows if int(row.get("no") or 0) == part_no), None)
            part_text = str((part or {}).get("text") or "").strip()
            if not part_text:
                raise WriterPipelineError(f"第 {part_no} 部还没有大纲内容，先写好再复核梳理")
            prev_parts = [row for row in rows
                          if int(row.get("no") or 0) < part_no
                          and str(row.get("text") or "").strip()]
            next_parts = [row for row in rows
                          if int(row.get("no") or 0) > part_no
                          and str(row.get("text") or "").strip()]
            content = prompts.build_outline_review_user(
                setup,
                part_no=part_no,
                part_title=part_title,
                part_text=part_text,
                prev_parts=[{"no": int(r.get("no") or 0), "title": str(r.get("title") or ""),
                             "text": str(r.get("text") or "")} for r in prev_parts],
                next_parts=[{"no": int(r.get("no") or 0), "title": str(r.get("title") or ""),
                             "text": str(r.get("text") or "")} for r in next_parts],
                extra_note=extra_note,
                output_mode_value=output_mode,
            )
            task_title = f"《{book_title}》· 多部复核梳理（第 {part_no} 部）"
        elif step == "split":
            setup = self._pipeline_setup(book_id, snapshot)
            outline = self.store.load_outline(book_id)
            part_no = int(snapshot.get("part_no") or 1)
            part = next((row for row in (outline.get("parts") or [])
                         if isinstance(row, dict) and int(row.get("no") or 0) == part_no), None)
            part_text = str((part or {}).get("text") or "").strip()
            if not part_text:
                # 老作品（逐章大纲）也能拆：把章节标题+梗概拼成一段文字喂进去。
                legacy = [row for row in (outline.get("chapters") or []) if isinstance(row, dict)]
                part_text = "\n".join(
                    f"第{row.get('no')}章 {row.get('title')}：{row.get('beat')}" for row in legacy
                ).strip()
            if not part_text:
                raise WriterPipelineError("这一部还没有大纲内容，先生成或填写大纲再拆章")
            # 这条流水线会自动分批连拆：本步只拆自己那一批（第三次反馈⑦）。
            from_no, want_count = self._split_slice(snapshot, step_index)
            prev_chapters: list[dict[str, Any]] = []
            if from_no > 1:
                # 接续拆分：把已拆好的最近几章带上，新章才会接着上一章的钩子往下走。
                manifest_now = self.store._load_manifest(book_id)
                book_dir = self.store._book_dir(book_id)
                earlier = [int(n) for n in manifest_now.get("chapter_ids") or [] if int(n) < from_no]
                for no in earlier[-prompts.SPLIT_PREV_MAX_CHAPTERS:]:
                    try:
                        index = self.store._load_chapter_index(book_dir, no)
                    except (LookupError, WriterStoreError):
                        continue
                    prev_chapters.append({
                        "no": no,
                        "title": str(index.get("title") or ""),
                        "beat": str(index.get("beat") or ""),
                        "hook": str(index.get("hook") or ""),
                    })
            content = prompts.build_split_user(
                setup, part_text=part_text, want_count=want_count, from_no=from_no,
                part_label=f"第 {part_no} 部" if part_no else "",
                extra_note=extra_note,
                prev_chapters=prev_chapters,
                output_mode_value=output_mode,
            )
            batch_note = f"· 第 {step_index + 1}/{len(steps)} 批" if len(steps) > 1 else ""
            task_title = f"《{book_title}》· 拆章{batch_note}（第 {from_no} 章起 {want_count} 章）"
        elif step in ("genre", "title", "blurb", "world", "chars", "outline"):
            setup = self._accumulated_setup(book_id, pipeline)
            content = prompts.build_step_user(
                step, setup,
                char_count=int(snapshot.get("char_count") or 4),
                side_count=int(snapshot.get("side_count") or 0),
                chapter_count=int(snapshot.get("chapter_count") or 10),
                extra_note=extra_note if step == "genre" else "",
                output_mode_value=output_mode,
            )
            task_title = f"《{book_title}》· {prompts.STEP_LABELS[step]}"
        elif step == "state":
            kind_now = str(pipeline.get("kind") or "")
            if kind_now in ("chapter_all", "state_rebuild"):
                chapter_no = self._chapter_for_step(snapshot, step_index, kind_now)
                pending_step_update = {"chapter_no": chapter_no}
            chapter_record = self.store.get_chapter(book_id, chapter_no)
            state_content = str(chapter_record.get("content") or "")
            if not state_content.strip():
                raise WriterPipelineError(f"第 {chapter_no} 章没有当前正文，无法提取章末状态")
            # 版本号与正文取自同一次读取：状态记录固化的就是被提取的那份正文。
            pending_state_version = str(chapter_record.get("current_version_id") or "")
            try:
                state_setup = self._pipeline_setup(book_id, snapshot)
            except (LookupError, WriterStoreError):
                state_setup = {}
            names = [str(row.get("name") or "").strip()
                     for row in (state_setup.get("characters") or [])
                     if isinstance(row, dict) and str(row.get("name") or "").strip()]
            content = prompts.build_state_user(
                chapter_no=chapter_no, content=state_content, names=names,
                output_mode_value=output_mode)
            task_title = f"《{book_title}》· 第{chapter_no}章章末状态"
        else:
            chapter = snapshot.get(WRITER_CHAPTER_BASELINE_KEY)
            if not isinstance(chapter, dict):
                chapter = self.store._load_chapter_index(self.store._book_dir(book_id), chapter_no)
            previous = snapshot.get(WRITER_PREVIOUS_CHAPTER_KEY) or {}
            chapter_states = snapshot.get(WRITER_CHAPTER_STATES_KEY) or []
            if str(pipeline.get("kind") or "") == "chapter_all":
                # 逐章批量（方案A）：每步提交时现读这一章的基线、前章衔接与
                # 前序章末状态——前一章多半是上一步刚落盘的，不能用旧快照。
                chapter_no = self._chapter_for_step(snapshot, step_index, "chapter_all")
                try:
                    fresh_baseline, fresh_previous = self.store.chapter_writing_inputs(book_id, chapter_no)
                except LookupError as exc:
                    raise WriterPipelineError(
                        f"第 {chapter_no} 章不存在（可能已被删除或改号），批量正文在本步停止：{exc}") from exc
                chapter = fresh_baseline
                previous = fresh_previous
                chapter_states = self.store.chapter_context_states(book_id, chapter_no)
                pending_step_update = {
                    "chapter_no": chapter_no,
                    WRITER_CHAPTER_BASELINE_KEY: fresh_baseline,
                    WRITER_PREVIOUS_CHAPTER_KEY: fresh_previous,
                    WRITER_CHAPTER_STATES_KEY: chapter_states,
                }
                pending_chapter_revision = int(fresh_baseline.get("revision") or 0)
            chapter_view = {
                "no": chapter_no,
                "title": str(chapter.get("title") or ""),
                "beat": str(chapter.get("beat") or ""),
                "hook": str(chapter.get("hook") or ""),
            }
            setup = self._pipeline_setup(book_id, snapshot)
            kind = str(pipeline.get("kind") or "")
            mode = "continue" if kind == "chapter_continue" else "whole"
            draft_tail = ""
            if mode == "continue":
                baseline = self._continuation_baseline(snapshot)
                draft_tail = baseline["content"][-prompts.CONTINUE_TAIL_CHARS:]
            if (kind == "chapter_segs" and step == "draft"
                    and (pipeline.get("step_outputs") or {}).get("segs") != []):
                # 分段正文：复用 Studio chunked 多轮契约（实施计划 §6.3）。
                points = [str(p) for p in (dict(pipeline.get("step_outputs") or {}).get("segs") or [])]
                if not points:
                    raise WriterPipelineError("缺少段落要点，无法分段写作")
                prefix_build = prompts.build_draft_context(
                    setup, chapter_view, mode="segment", seg_points=[],
                    extra_note=extra_note, previous_chapter=previous,
                    chapter_states=chapter_states, output_mode_value=output_mode,
                )
                # 前缀只含公共上下文（无要点段）；每块的要点单独拼。
                prefix_lines = prefix_build["content"].split("\n\n")
                prefix = "\n\n".join(
                    line for line in prefix_lines
                    if not line.startswith("【段落要点】") and not line.startswith("现在写第")
                )
                blocks = []
                for index, point in enumerate(points):
                    blocks.append(
                        f"【段落要点】（第 {index + 1}/{len(points)} 段）\n▶ {point}\n\n"
                        f"请写出本段正文（1200—1800字），只输出本段正文。"
                    )
                chunked = {
                    "blocks": blocks,
                    "prefix": prefix,
                    "continuity_chars": prompts.CONTINUE_TAIL_CHARS,
                    "continuity_label": "【已写内容·结尾】",
                }
                content = f"{prefix}\n\n" + "\n\n".join(blocks)
                task_title = f"《{book_title}》· 第{chapter_no}章分段正文（{len(points)}段）"
            else:
                built = prompts.build_draft_context(
                    setup, chapter_view, mode=mode, draft_tail=draft_tail,
                    extra_note=extra_note, previous_chapter=previous,
                    chapter_states=chapter_states, output_mode_value=output_mode, step=step,
                )
                content = built["content"]
                label = prompts.STEP_LABELS[step]
                suffix = {"chapter_continue": "续写", "chapter_rewrite": "重写"}.get(kind, "")
                if kind == "chapter_all":
                    suffix = f"（批量 {step_index + 1}/{len(steps)}）"
                task_title = f"《{book_title}》· 第{chapter_no}章{suffix or ''}{label}".replace("  ", " ")
        context_window = 0
        try:
            profile = next((row for row in self.app.config.model_profiles()
                            if str(row.get("model_key") or "") == str(snapshot.get("model_key") or "")), {})
            context_window = int(profile.get("context_window") or 0)
        except (AttributeError, TypeError, ValueError):
            pass
        budget = prompts.budget_report(content=content, system=system_text, slots=[],
                                       context_window=context_window)
        input_data: dict[str, Any] = {
            "content": content,
            "model_key": str(snapshot.get("model_key") or ""),
            "model_name": str(snapshot.get("model_name") or ""),
            "title": task_title,
            "module": "writer",
            "origin_label": "织书台",
            "user_request": extra_note,
            "system": system_text,
            "writer": {
                "book_id": book_id,
                "pipeline_id": str(pipeline.get("id") or ""),
                "attempt": int(pipeline.get("attempt") or 0),
                "step": step,
                "step_index": step_index,
                "chapter_no": chapter_no,
                "base_revision": int(pipeline.get("base_revision") or 0),
                "chapter_revision": pipeline.get("chapter_revision"),
                "source_version_ids": dict(pipeline.get("source_version_ids") or {}),
                WORK_GLOBAL_SYSTEM_PROMPT_REVISION_KEY: int(snapshot.get(WORK_GLOBAL_SYSTEM_PROMPT_REVISION_KEY) or 0),
            },
        }
        if chunked is not None:
            input_data["chunked"] = chunked
        return {"input": input_data, "budget": budget,
                "step_update": pending_step_update, "chapter_revision": pending_chapter_revision,
                "state_version": pending_state_version}

    def _submit_step_task(self, book_id: str, pipeline: dict[str, Any]) -> str:
        prepared = self.prepare_step_input(book_id, pipeline)
        input_data = prepared["input"]
        budget = prepared["budget"]
        if budget["fits"] is False:
            raise WriterPipelineError("本次输入超出模型可用上下文：" + "；".join(budget["suggestions"]))
        pending_step_update = prepared["step_update"]
        pending_chapter_revision = prepared["chapter_revision"]
        pending_state_version = prepared["state_version"]
        # This is the submission admission point. Cancellation before it
        # creates no task; cancellation after it owns the returned task.
        with self.store._lock:
            current = self._matching_pipeline(book_id, pipeline)
            if current is None or current.get("status") != "running" or current.get("current_task_id"):
                return ""
        task = self.app.studio.jobs.submit(input_data)
        task_id = str(task["id"])
        accepted = False

        def _persist(record: dict[str, Any]) -> None:
            nonlocal accepted
            if self._step_key(book_id, record) != self._step_key(book_id, pipeline) or record.get("current_task_id"):
                return
            # Keep late handles even in stopping/failed records; they must be
            # visible and cannot authorize another pipeline step.
            record["current_task_id"] = task_id
            accepted = record.get("status") == "running"
            if not accepted:
                return
            if pending_step_update is not None:
                # 逐章批量：本步写哪章、它的基线与前章衔接，必须和任务归属
                # 同锁落盘，否则 _advance 的归属校验会拿旧章号误判不一致。
                record_snapshot = record.get("request_snapshot")
                if isinstance(record_snapshot, dict):
                    record_snapshot.update(pending_step_update)
                    record["request_snapshot"] = record_snapshot
                record["chapter_revision"] = pending_chapter_revision
            if pending_state_version is not None:
                record["state_source_version_id"] = pending_state_version

        try:
            self.store.update_pipeline(
                book_id, str(pipeline.get("id") or ""),
                _persist,
                expected_attempt=int(pipeline.get("attempt") or 0),
            )
        except Exception:
            # Setting a local flag in mutate is not proof that its disk write
            # succeeded. Cancel the returned task before propagating failure.
            accepted = False
            raise
        finally:
            if not accepted:
                self._request_stop(book_id, pipeline, task_id)
        return task_id
