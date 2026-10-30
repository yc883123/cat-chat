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

import threading
import time
import traceback
from typing import Any

from naiba.writer import prompts
from naiba.writer.store import (
    PIPELINE_STATUSES,
    WriterConflictError,
    WriterStoreError,
    new_version_id,
)

# 流水线 kind：整套设定 / 单步设定 / 大纲 / 整章 / 分段 / 续写 / 重写。
PIPELINE_KINDS = (
    "setup_all", "setup_step", "outline", "outline_text", "split",
    "chapter", "chapter_segs", "chapter_continue", "chapter_rewrite",
)
# 各 kind 的步骤序列（segs → draft 两步；draft 复用 Studio chunked 顺序生成各段）。
KIND_STEPS = {
    "setup_all": tuple(prompts.SETUP_STEPS),
    "outline": ("outline",),
    # 3.9.13：整篇大纲（按部）与拆章各是一条单步流水线，互不耦合。
    "outline_text": ("outline_text",),
    "split": ("split",),
    "chapter": ("draft",),
    "chapter_segs": ("segs", "draft"),
    "chapter_continue": ("draft",),
    "chapter_rewrite": ("draft",),
}
SETUP_STEP_KINDS = tuple(prompts.SETUP_STEPS)
TERMINAL_STATUSES = {"done", "failed", "cancelled", "interrupted", "conflict"}

TICK_SECONDS = 0.5


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
        """启动收敛：running 的流水线标 interrupted（不自动续跑，实施计划 §7.3）。"""
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
                       char_count: int = 4, chapter_count: int = 10,
                       setup_version_id: str = "", outline_version_id: str = "",
                       extra_note: str = "", allow_drop: bool = False,
                       part_no: int = 0, part_title: str = "", target_chars: int = 0,
                       want_count: int = 0, from_no: int = 0,
                       base_revision: Any = None, chapter_revision: Any = None) -> dict[str, Any]:
        """创建并启动一条流水线：快照固化全部输入，重试不因页面当前选择变化而换料。"""
        if kind not in PIPELINE_KINDS:
            raise WriterPipelineError(f"未知的流水线类型：{kind}")
        if kind == "setup_step":
            if step not in SETUP_STEP_KINDS:
                raise WriterPipelineError(f"未知的设定步骤：{step or '（空）'}")
            steps: tuple[str, ...] = (step,)
        else:
            steps = KIND_STEPS[kind]
        if not str(model_name or "").strip():
            raise WriterPipelineError("请选择模型；可用模型来自设置页的模型检查结果")
        manifest = self.store._load_manifest(book_id)
        if base_revision is not None and base_revision != "":
            from naiba.writer.store import WriterStore
            WriterStore._check_revision(manifest, base_revision)
        current = manifest.get("active_pipeline")
        if isinstance(current, dict) and str(current.get("status") or "") == "running":
            raise WriterPipelineError("这部作品已有正在进行的生成，请先取消或等它结束")
        setup_version_id = str(setup_version_id or "").strip() or str(
            manifest.get("current_setup_version_id") or "")
        setup = None
        if setup_version_id:
            try:
                setup = self.store.load_setup(book_id, manifest=manifest, version_id=setup_version_id)
            except (LookupError, WriterStoreError):
                setup = None
        if steps[0] in ("segs", "draft", "outline", "outline_text", "split") and not setup:
            raise WriterPipelineError("请先生成或保存设定，再生成大纲与正文")
        # 设定链的起点是创意：已保存设定里没有创意时，模型只能编出「未提供创意/未指定」
        # 这类占位结果，白烧 token 还落一版垃圾（真实反馈）。在排队前如实拒绝。
        if kind in ("setup_all", "setup_step") \
                and not str((setup or {}).get("idea") or "").strip():
            raise WriterPipelineError(
                "请先在「设定 → 故事创意」里写一句话（或从模板套用后再生成）；"
                "还没有创意时，模型只能给出占位结果"
            )
        if steps[0] == "draft":
            outline_version_id = str(outline_version_id or "").strip() or str(
                manifest.get("current_outline_version_id") or "")
            if not outline_version_id:
                raise WriterPipelineError("请先生成大纲，再写正文")
            if chapter_no not in {int(n) for n in manifest.get("chapter_ids") or []}:
                raise WriterPipelineError(f"作品里没有第 {chapter_no} 章；请先同步章节")
            chapter = self.store._load_chapter_index(self.store._book_dir(book_id), chapter_no)
            chapter_revision = chapter_revision if chapter_revision is not None and chapter_revision != "" \
                else int(chapter.get("revision") or 0)
        else:
            outline_version_id = str(outline_version_id or "").strip()
            chapter_revision = None
        char_count = self._clamp(int(char_count or 4), prompts.CHARS_MIN, prompts.CHARS_MAX, 4)
        chapter_count = self._clamp(int(chapter_count or 10), prompts.OUTLINE_MIN, prompts.OUTLINE_MAX, 10)
        # 拆分参数：只在 outline_text / split 有意义，其余 kind 归一成 0。
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
        book_title = str(manifest.get("title") or "未命名作品")
        snapshot = {
            "kind": kind,
            "steps": list(steps),
            "step": step or "",
            "chapter_no": int(chapter_no or 0),
            "model_key": str(model_key or ""),
            "model_name": str(model_name or "").strip(),
            "char_count": char_count,
            "chapter_count": chapter_count,
            "part_no": part_no,
            "part_title": str(part_title or "")[:80],
            "target_chars": target_chars,
            "want_count": want_count,
            "from_no": from_no,
            "setup_version_id": setup_version_id,
            "outline_version_id": outline_version_id,
            "extra_note": str(extra_note or "")[:500],
            "allow_drop": bool(allow_drop),
            "book_title": book_title,
        }
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
            "created_at": int(time.time() * 1000),
            "updated_at": int(time.time() * 1000),
        }
        self.store._save_manifest(self.store._book_dir(book_id), {**manifest, "active_pipeline": pipeline})
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

    # ---- 取消 / 重试 ----

    def cancel_pipeline(self, book_id: str) -> dict[str, Any]:
        """取消：先把流水线标 cancelled，再取消当前 Studio 任务（实施计划 §7.3）。

        迟到的任务结果由 tick 的 running 判定挡住：流水线已非 running，不写回、
        不提交下一步。
        """
        manifest = self.store._load_manifest(book_id)
        pipeline = manifest.get("active_pipeline")
        if not isinstance(pipeline, dict):
            raise WriterPipelineError("这部作品没有进行中的生成")
        if str(pipeline.get("status") or "") != "running":
            raise WriterPipelineError("当前生成已结束，无需取消")
        task_id = str(pipeline.get("current_task_id") or "")

        def _cancel(record: dict[str, Any]) -> None:
            record["status"] = "cancelled"
            record["error"] = "用户取消"

        updated = self.store.update_pipeline(book_id, str(pipeline["id"]), _cancel,
                                             expected_attempt=int(pipeline.get("attempt") or 0))
        if updated is None:
            raise WriterPipelineError("流水线状态已变化，请刷新后重试")
        if task_id:
            try:
                self.app.studio.jobs.cancel(task_id)
            except Exception:  # noqa: BLE001 - 任务取消失败不影响流水线终态
                traceback.print_exc()
        return updated

    def retry_pipeline(self, book_id: str, *, rebase: bool = False) -> dict[str, Any]:
        """失败步重试：attempt+1 重提当前步；conflict 状态需 `rebase=True` 显式换基。"""
        manifest = self.store._load_manifest(book_id)
        pipeline = manifest.get("active_pipeline")
        if not isinstance(pipeline, dict):
            raise WriterPipelineError("这部作品没有可重试的生成")
        status = str(pipeline.get("status") or "")
        if status == "running":
            raise WriterPipelineError("当前生成还在进行中")
        if status == "conflict" and not rebase:
            raise WriterPipelineError(
                "作品或章节已被修改（版本冲突）；请重新读取作品后，勾选「基于当前版本重新生成」再试"
            )
        if status not in ("failed", "conflict", "interrupted", "cancelled"):
            raise WriterPipelineError("只有失败、冲突、中断或取消的生成可以重试")
        attempt = int(pipeline.get("attempt") or 0) + 1

        def _retry(record: dict[str, Any]) -> None:
            record["attempt"] = attempt
            record["status"] = "running"
            record["error"] = ""
            record["current_task_id"] = ""
            record["base_revision"] = int(manifest.get("revision") or 0)
            if record.get("chapter_revision") is not None:
                snapshot = record.get("request_snapshot") or {}
                chapter_no = int(snapshot.get("chapter_no") or 0)
                if chapter_no and chapter_no in {int(n) for n in manifest.get("chapter_ids") or []}:
                    try:
                        chapter = self.store._load_chapter_index(
                            self.store._book_dir(book_id), chapter_no)
                        record["chapter_revision"] = int(chapter.get("revision") or 0)
                    except (LookupError, WriterStoreError):
                        record["chapter_revision"] = None

        updated = self.store.update_pipeline(book_id, str(pipeline["id"]), _retry)
        if updated is None:
            raise WriterPipelineError("流水线状态已变化，请刷新后重试")
        self._submit_step(book_id, updated)
        return updated

    # ---- 查询 ----

    def get_pipeline(self, book_id: str) -> dict[str, Any]:
        manifest = self.store._load_manifest(book_id)
        pipeline = manifest.get("active_pipeline")
        if not isinstance(pipeline, dict):
            return {"pipeline": None}
        payload = dict(pipeline)
        task_id = str(payload.get("current_task_id") or "")
        if task_id:
            task = self._task(task_id)
            if task is not None:
                payload["task_status"] = str(task.get("status") or "")
        return {"pipeline": payload}

    # ---- 推进核心 ----

    def tick(self) -> None:
        """扫全部活动流水线：任务终态则推进；一次 tick 处理完当前所有可推进项。"""
        for row in self.store.iter_active_pipelines():
            book_id = str(row.get("book_id") or "")
            pipeline = dict(row.get("pipeline") or {})
            if not book_id or str(pipeline.get("status") or "") != "running":
                continue
            try:
                self._advance(book_id, pipeline)
            except WriterConflictError as exc:
                self._fail(book_id, pipeline, str(exc), override_status="conflict")
            except (WriterStoreError, WriterPipelineError, prompts.PromptError) as exc:
                self._fail(book_id, pipeline, str(exc))
            except Exception as exc:  # noqa: BLE001 - 单本异常不拖垮其它作品
                traceback.print_exc()
                self._fail(book_id, pipeline, f"推进失败：{exc}")

    def _advance(self, book_id: str, pipeline: dict[str, Any]) -> None:
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
            or int(writer_input.get("chapter_no") or 0) != int(snapshot.get("chapter_no") or 0)
        )
        if mismatch:
            self._fail(book_id, pipeline, "当前任务与流水线不一致（可能来自旧的尝试），已停止推进")
            return
        if status != "done":
            # 条件 1：非 done 一律停链；cancelled/failed/interrupted 各有真实状态。
            self._fail(
                book_id, pipeline,
                str(task.get("error") or "") or f"任务状态为 {status}，流水线停止",
                override_status="cancelled" if status == "cancelled" else "failed",
            )
            return
        # 幂等：同一任务已写回过就不再写（崩溃恢复 / 重复回调安全）。
        if task_id in [str(t) for t in (pipeline.get("completed_task_ids") or [])]:
            self._finish_step(book_id, pipeline, task_id, output=None)
            return
        # 条件 3：输出非空 + 结构化解析 + 截断判定（解析失败 → 停链，保留任务原始结果）。
        result = str(task.get("result") or "")
        finish_reason = str(task.get("finish_reason") or "")
        output = self._parse_step_output(
            step, result, pipeline=pipeline, finish_reason=finish_reason,
        )
        self._finish_step(book_id, pipeline, task_id, output=output)

    def _writer_input(self, task: dict[str, Any]) -> dict[str, Any]:
        input_data = task.get("input") if isinstance(task.get("input"), dict) else {}
        writer = input_data.get("writer")
        return writer if isinstance(writer, dict) else {}

    def _task(self, task_id: str) -> dict[str, Any] | None:
        try:
            return self.app.studio.jobs.task(task_id)
        except Exception:  # noqa: BLE001 - 任务存储异常按「查不到」处理
            return None

    def _fail(self, book_id: str, pipeline: dict[str, Any], error: str,
              *, override_status: str = "failed") -> None:
        status = override_status if override_status in ("failed", "cancelled", "conflict") else "failed"

        def _mutate(record: dict[str, Any]) -> None:
            if str(record.get("status") or "") != "running":
                return
            record["status"] = status
            record["error"] = str(error or "")[:500]

        self.store.update_pipeline(
            book_id, str(pipeline.get("id") or ""), _mutate,
            expected_attempt=int(pipeline.get("attempt") or 0),
        )

    # ---- 步骤输出解析（条件 3） ----

    def _accumulated_setup(self, book_id: str, pipeline: dict[str, Any]) -> dict[str, Any]:
        """链内已积累的设定内容：起点 = 快照里的设定版本，之后逐步合并 step_outputs。"""
        snapshot = pipeline.get("request_snapshot") if isinstance(pipeline.get("request_snapshot"), dict) else {}
        setup_version_id = str(snapshot.get("setup_version_id") or "")
        try:
            setup = self.store.load_setup(book_id, version_id=setup_version_id)
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
            }
        return merged

    def _parse_step_output(self, step: str, result: str, *, pipeline: dict[str, Any],
                           finish_reason: str) -> Any:
        snapshot = pipeline.get("request_snapshot") if isinstance(pipeline.get("request_snapshot"), dict) else {}
        if step == "genre":
            return prompts.parse_genre(result)
        if step == "title":
            return prompts.parse_title_candidates(result)
        if step == "blurb":
            return prompts.parse_blurb_candidates(result)
        if step == "world":
            return prompts.parse_world(result)
        if step == "chars":
            return prompts.parse_characters(result, wanted=int(snapshot.get("char_count") or 4))
        if step == "outline":
            return prompts.parse_outline(result, wanted=int(snapshot.get("chapter_count") or 10))
        if step == "outline_text":
            return prompts.parse_outline_text(result)
        if step == "split":
            return prompts.parse_split(
                result,
                from_no=int(snapshot.get("from_no") or 1),
                want_count=int(snapshot.get("want_count") or 10),
            )
        if step == "segs":
            return prompts.parse_segs(result)
        if step == "draft":
            return prompts.parse_draft(result, finish_reason=finish_reason)
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
        pipeline_id = str(pipeline.get("id") or "")
        attempt = int(pipeline.get("attempt") or 0)
        snapshot = pipeline.get("request_snapshot") if isinstance(pipeline.get("request_snapshot"), dict) else {}
        steps = [str(s) for s in pipeline.get("steps") or []]
        step_index = int(pipeline.get("step_index") or 0)
        step = steps[step_index] if 0 <= step_index < len(steps) else ""
        completed = [str(t) for t in (pipeline.get("completed_task_ids") or [])]
        if task_id and task_id not in completed:
            completed.append(task_id)
        next_index = step_index + 1

        def _mutate(record: dict[str, Any]) -> None:
            # ---- 条件 4：锁内确认仍是本 attempt 的活动流水线，且来源版本未漂移 ----
            if str(record.get("status") or "") != "running":
                raise WriterPipelineError("流水线已不在进行中（可能刚被取消），结果不写回")
            if int(record.get("attempt") or 0) != attempt:
                raise WriterPipelineError("流水线 attempt 已变化，旧尝试的结果不能写回")
            manifest = self.store._load_manifest(book_id)
            if int(record.get("base_revision") or 0) != int(manifest.get("revision") or 0):
                raise WriterConflictError(
                    "生成期间作品被修改过（版本冲突）；请重读作品后选择「基于当前版本重新生成」"
                )
            chapter_no = int(snapshot.get("chapter_no") or 0)
            if step in ("segs", "draft") and record.get("chapter_revision") is not None:
                chapter = self.store._load_chapter_index(self.store._book_dir(book_id), chapter_no)
                if int(chapter.get("revision") or 0) != int(record.get("chapter_revision") or 0):
                    raise WriterConflictError(
                        f"生成期间第 {chapter_no} 章被修改过（版本冲突）；"
                        "请重读章节后选择「基于当前版本重新生成」"
                    )
            # ---- 条件 5：写回本步成果（每步成功才落盘；失败保留前序成果） ----
            if output is not None:
                if step in ("genre", "title", "blurb", "world", "chars", "outline",
                            "outline_text", "split"):
                    self._apply_setup_or_outline_step(book_id, record, step, output, task_id)
                elif step == "segs":
                    outputs = dict(record.get("step_outputs") or {})
                    outputs["segs"] = [str(p) for p in output]
                    record["step_outputs"] = outputs
                elif step == "draft":
                    self._apply_draft_step(book_id, record, output, task_id)
            # ---- 记录完成并前进 ----
            record["completed_task_ids"] = completed
            record["current_task_id"] = ""
            if next_index >= len(record.get("steps") or []):
                record["status"] = "done"
                record["step_index"] = len(record.get("steps") or [])
            else:
                record["step_index"] = next_index
            # 刷新基准：本次写回推进了作品/章节 revision，下游步骤按新基准核对。
            refreshed = self.store._load_manifest(book_id)
            record["base_revision"] = int(refreshed.get("revision") or 0)
            if step in ("segs", "draft") and record.get("chapter_revision") is not None:
                chapter = self.store._load_chapter_index(self.store._book_dir(book_id), chapter_no)
                record["chapter_revision"] = int(chapter.get("revision") or 0)

        updated = self.store.update_pipeline(book_id, pipeline_id, _mutate, expected_attempt=attempt)
        if updated is None:
            return
        if str(updated.get("status") or "") == "running":
            self._submit_step(book_id, updated)

    def _apply_setup_or_outline_step(self, book_id: str, record: dict[str, Any],
                                     step: str, output: Any, task_id: str) -> None:
        """设定六步的写回（锁内调用）：每步生成一份新设定版本；大纲步另落大纲版本并同步章节。"""
        snapshot = record.get("request_snapshot") if isinstance(record.get("request_snapshot"), dict) else {}
        if step == "outline":
            source = record.get("source_version_ids") if isinstance(record.get("source_version_ids"), dict) else {}
            self.store.save_outline(
                book_id, chapters=output, kind="generated", task_id=task_id,
                source_setup_version_id=str(source.get("setup") or ""),
                sync=True, allow_drop=bool(snapshot.get("allow_drop")),
            )
            return
        if step == "outline_text":
            # 只替换这一部，其它部原样保留（多部作品一部一部补）。
            source = record.get("source_version_ids") if isinstance(record.get("source_version_ids"), dict) else {}
            part_no = int(snapshot.get("part_no") or 1)
            current = self.store.load_outline(book_id)
            by_no: dict[int, dict[str, Any]] = {}
            for row in current.get("parts") or []:
                if isinstance(row, dict) and str(row.get("text") or "").strip():
                    by_no[int(row.get("no") or 0)] = {
                        "no": int(row.get("no") or 0),
                        "title": str(row.get("title") or ""),
                        "text": str(row.get("text") or ""),
                    }
            title = str(snapshot.get("part_title") or "").strip() or (
                by_no.get(part_no, {}).get("title") or f"第 {part_no} 部")
            by_no[part_no] = {"no": part_no, "title": title, "text": str(output)}
            # 补齐中间空部（先生成第 2 部时第 1 部留空占位，保证部号连续）。
            filled: list[dict[str, Any]] = []
            for no in range(1, max(by_no) + 1):
                filled.append(by_no.get(no) or {"no": no, "title": f"第 {no} 部", "text": ""})
            self.store.save_outline(
                book_id, parts=filled, kind="generated", task_id=task_id,
                source_setup_version_id=str(source.get("setup") or ""),
            )
            return
        if step == "split":
            self.store.append_chapters(
                book_id, output, part_no=int(snapshot.get("part_no") or 0),
            )
            return
        merged = self._accumulated_setup(book_id, record)
        if step == "genre":
            genre = dict(merged.get("genre") if isinstance(merged.get("genre"), dict) else {})
            merged["genre"] = {
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
            if candidates and not str(merged.get("title") or "").strip():
                merged["title"] = candidates[0]
        elif step == "blurb":
            candidates = [str(c) for c in output]
            merged["blurb_candidates"] = candidates
            if candidates and not str(merged.get("blurb") or "").strip():
                merged["blurb"] = candidates[0]
        elif step == "world":
            merged["worldview"] = str(output)
        elif step == "chars":
            merged["characters"] = [dict(row) for row in output]
        outputs = dict(record.get("step_outputs") or {})
        outputs["setup"] = merged
        record["step_outputs"] = outputs
        self.store.save_setup(book_id, content=merged, kind="generated", task_id=task_id)

    def _apply_draft_step(self, book_id: str, record: dict[str, Any],
                          output: str, task_id: str) -> None:
        snapshot = record.get("request_snapshot") if isinstance(record.get("request_snapshot"), dict) else {}
        chapter_no = int(snapshot.get("chapter_no") or 0)
        source = record.get("source_version_ids") if isinstance(record.get("source_version_ids"), dict) else {}
        self.store.save_chapter_version(
            book_id, chapter_no, content=str(output), kind="generated", task_id=task_id,
            source_setup_version_id=str(source.get("setup") or ""),
            source_outline_version_id=str(source.get("outline") or ""),
            expected_revision=record.get("chapter_revision"),
        )

    # ---- 任务提交 ----

    def _submit_step(self, book_id: str, pipeline: dict[str, Any]) -> str:
        """为当前步提交一个 Studio 任务；任务 input.writer 固化归属（实施计划 §7.1）。"""
        snapshot = pipeline.get("request_snapshot") if isinstance(pipeline.get("request_snapshot"), dict) else {}
        steps = [str(s) for s in pipeline.get("steps") or []]
        step_index = int(pipeline.get("step_index") or 0)
        step = steps[step_index] if 0 <= step_index < len(steps) else ""
        if not step:
            raise WriterPipelineError("流水线没有待执行的步骤")
        book_title = str(snapshot.get("book_title") or "未命名作品")
        extra_note = str(snapshot.get("extra_note") or "")
        chapter_no = int(snapshot.get("chapter_no") or 0)
        system_text = prompts.step_system(step)
        chunked: dict[str, Any] | None = None
        if step == "outline_text":
            setup = self._accumulated_setup(book_id, pipeline)
            part_no = int(snapshot.get("part_no") or 1)
            content = prompts.build_outline_text_user(
                setup,
                part_no=part_no,
                part_title=str(snapshot.get("part_title") or ""),
                target_chars=int(snapshot.get("target_chars") or 8000),
                extra_note=extra_note,
            )
            task_title = f"《{book_title}》· 第 {part_no} 部大纲"
        elif step == "split":
            setup = self.store.load_setup(
                book_id, version_id=str(snapshot.get("setup_version_id") or ""))
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
            from_no = int(snapshot.get("from_no") or 1)
            want_count = int(snapshot.get("want_count") or 10)
            content = prompts.build_split_user(
                setup, part_text=part_text, want_count=want_count, from_no=from_no,
                part_label=f"第 {part_no} 部" if part_no else "",
                extra_note=extra_note,
            )
            task_title = f"《{book_title}》· 拆章（第 {from_no} 章起 {want_count} 章）"
        elif step in ("genre", "title", "blurb", "world", "chars", "outline"):
            setup = self._accumulated_setup(book_id, pipeline)
            content = prompts.build_step_user(
                step, setup,
                char_count=int(snapshot.get("char_count") or 4),
                chapter_count=int(snapshot.get("chapter_count") or 10),
                extra_note=extra_note if step == "genre" else "",
            )
            task_title = f"《{book_title}》· {prompts.STEP_LABELS[step]}"
        else:
            chapter = self.store._load_chapter_index(self.store._book_dir(book_id), chapter_no)
            chapter_view = {
                "no": chapter_no,
                "title": str(chapter.get("title") or ""),
                "beat": str(chapter.get("beat") or ""),
                "hook": str(chapter.get("hook") or ""),
            }
            setup = self.store.load_setup(
                book_id, version_id=str(snapshot.get("setup_version_id") or ""))
            kind = str(pipeline.get("kind") or "")
            mode = "continue" if kind == "chapter_continue" else "whole"
            draft_tail = ""
            if mode == "continue":
                current = self.store.get_chapter(book_id, chapter_no)
                draft_tail = str(current.get("content") or "")[-prompts.CONTINUE_TAIL_CHARS:]
            if kind == "chapter_segs" and step == "draft":
                # 分段正文：复用 Studio chunked 多轮契约（实施计划 §6.3）。
                points = [str(p) for p in (dict(pipeline.get("step_outputs") or {}).get("segs") or [])]
                if not points:
                    raise WriterPipelineError("缺少段落要点，无法分段写作")
                prefix_build = prompts.build_draft_context(
                    setup, chapter_view, mode="segment", seg_points=[],
                    extra_note=extra_note,
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
                    extra_note=extra_note,
                )
                content = built["content"]
                label = prompts.STEP_LABELS["draft"]
                suffix = {"chapter_continue": "续写", "chapter_rewrite": "重写"}.get(kind, "")
                task_title = f"《{book_title}》· 第{chapter_no}章{suffix or ''}{label}".replace("  ", " ")
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
                "chapter_no": chapter_no,
                "base_revision": int(pipeline.get("base_revision") or 0),
                "chapter_revision": pipeline.get("chapter_revision"),
                "source_version_ids": dict(pipeline.get("source_version_ids") or {}),
            },
        }
        if chunked is not None:
            input_data["chunked"] = chunked
        task = self.app.studio.jobs.submit(input_data)
        self.store.update_pipeline(
            book_id, str(pipeline.get("id") or ""),
            lambda record: record.__setitem__("current_task_id", str(task["id"])),
            expected_attempt=int(pipeline.get("attempt") or 0),
        )
        return str(task["id"])
