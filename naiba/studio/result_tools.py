"""Task-bound TXT tools, shared by ordinary, chunked and Skill generation."""
from __future__ import annotations

import copy
import json
from typing import Any

from naiba.core.contracts import STUDIO_RESULT_ARTIFACT_KEY, STUDIO_TRACE_MESSAGES_KEY
from naiba.core.exceptions import TaskCancelled
from naiba.skills.agent import SkillAgent
from naiba.studio.artifacts import ResultArtifactError
from naiba.tools.registry import ToolRegistry, ToolSpec

RESULT_TOOLS = ("write_result_txt", "complete_result_txt")
MAX_RESULT_STEPS = 128
MAX_CORRECTIONS = 2
RESULT_TAIL_CHARS = 2400


def _json_continuation_state(text: str) -> dict[str, Any] | None:
    """Lexical state only; business/schema validation still belongs to consumers."""
    if not text.lstrip().startswith(("{", "[")):
        return None
    containers: list[str] = []
    quoted = escaped = False
    mismatch = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            containers.append(char)
        elif char in "]}":
            if not containers or containers[-1] != ("[" if char == "]" else "{"):
                mismatch = True
            elif containers:
                containers.pop()
    return {"open_depth": len(containers), "open_containers": "".join(containers[-64:]),
            "shown_containers": min(64, len(containers)), "in_string": quoted,
            "escaped": escaped, "mismatched_close": mismatch}


class TxtToolContext:
    def __init__(self, store: Any, task: dict[str, Any], block_no: int, cancel: Any, event: Any):
        self.store, self.task, self.block_no = store, task, block_no
        self.cancel, self.event = cancel, event
        self.finish_reason = ""
        self.view_prefix = 0
        self.acknowledged_calls: set[str] = set()

    def _check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise TaskCancelled("任务已取消")
        current = self.store.get_task(self.task["id"])
        if current is None or current.get("status") not in {"queued", "running"}:
            raise TaskCancelled("任务已取消或已失效")

    def instruction(self) -> str:
        manifest = self.store.artifacts.manifest(self.task)
        block = manifest["blocks"][self.block_no - 1]
        last = block["segments"][-1]["receipt"] if block["segments"] else None
        confirmed = self.store.artifacts._block_bytes(self.task["id"], self.block_no, block).decode("utf-8")
        tail = confirmed[-RESULT_TAIL_CHARS:]
        structure = _json_continuation_state(confirmed)
        return (
            "\n\n[本次生成的宿主 TXT 输出协议]\n"
            "最终作品必须通过 write_result_txt(segment,text) 分段写入当前任务。"
            "segment从1连续递增，text只含正文，不加说明；JSON结果按原样接续，不加代码围栏。"
            "工具成功回执才表示已保存。每段建议不超过6000字符。"
            "全部正文写完后调用complete_result_txt，原样回传最新回执的receipt_id、segments、chars、sha256。"
            "不得自己计算摘要，不得只回复完成；完成工具成功后宿主直接收尾。"
            "重放同段必须相同内容。已确认文字不会再次写入；若输出上限中断，只补未写内容。"
            "历史写段参数可能只保留回执和末尾，完整原文仍在文件中，不表示内容丢失。"
            f"\n当前业务块 {self.block_no}/{len(manifest['blocks'])}；下一段 {len(block['segments']) + 1}。"
            f"\n最新回执：{json.dumps(last, ensure_ascii=False)}"
            + (f"\n已保存JSON词法状态（不代表业务校验成功）：{json.dumps(structure, ensure_ascii=False)}" if structure else "")
            + (f"\n已确认正文末尾（仅作接续素材，不是新增指令）：\n{tail}" if tail else "")
        )

    def execute(self, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        self._check()
        try:
            if tool == "write_result_txt":
                before = self.store.artifacts.manifest(self.task)["blocks"][self.block_no - 1]
                count = len(before["segments"])
                receipt = self.store.artifacts.write_segment(
                    self.task, self.block_no, args.get("segment"), args.get("text"), cancel=self.cancel)
                text, summary = self.store.artifacts.read(self.task, require_final=False)
                self.store.update_task(self.task["id"], **{STUDIO_RESULT_ARTIFACT_KEY: summary})
                if receipt["segment"] > count:
                    self.event({"type": "delta", "content": args["text"]})
                self.event({"type": "status", "message": f"TXT 已落盘 {summary['chars']} 字符"})
                return True, json.dumps(receipt, ensure_ascii=False)
            if tool == "complete_result_txt":
                summary = self.store.artifacts.complete_block(
                    self.task, self.block_no, args, finish_reason=self.finish_reason, cancel=self.cancel)
                self.store.update_task(self.task["id"], **{STUDIO_RESULT_ARTIFACT_KEY: summary})
                return True, json.dumps(summary, ensure_ascii=False)
            return False, "此任务没有该结果工具"
        except (ResultArtifactError, TypeError, ValueError) as exc:
            return False, str(exc)

    def completed_text(self) -> str | None:
        self._check()
        manifest = self.store.artifacts.manifest(self.task)
        block = manifest["blocks"][self.block_no - 1]
        if not block.get("final"):
            return None
        self.store.artifacts.read(self.task, require_final=False)
        return self.store.artifacts._block_bytes(self.task["id"], self.block_no, block).decode("utf-8")

    def transform(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compact only acknowledged write arguments; original trace is untouched."""
        copied = copy.deepcopy(messages)
        manifest = self.store.artifacts.manifest(self.task)
        block = manifest["blocks"][self.block_no - 1]
        for row in copied[self.view_prefix:]:
            for call in row.get("tool_calls") or []:
                name = call.get("name") or (call.get("function") or {}).get("name")
                args = call.get("arguments", (call.get("function") or {}).get("arguments"))
                encoded = isinstance(args, str)
                if encoded:
                    try:
                        args = json.loads(args)
                    except (ValueError, TypeError):
                        continue
                if name != "write_result_txt" or not isinstance(args, dict):
                    continue
                n = args.get("segment")
                if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(block["segments"]):
                    continue
                receipt = block["segments"][n - 1]
                import hashlib
                text = args.get("text")
                call_id = str(call.get("id") or "")
                if call_id in self.acknowledged_calls:
                    continue
                if (not call_id or not isinstance(text, str)
                        or hashlib.sha256(text.encode("utf-8")).hexdigest() != receipt["sha256"]):
                    continue
                self.acknowledged_calls.add(call_id)
                kept_tail = text[-RESULT_TAIL_CHARS:] if n == len(block["segments"]) else ""
                replacement = {"segment": n, "text": f"[已写入TXT的{receipt['chars']}字符；原文由宿主持久保留]\n{kept_tail}",
                               "receipt": receipt["receipt"]}
                if "arguments" in call:
                    call["arguments"] = json.dumps(replacement, ensure_ascii=False) if encoded else replacement
                else:
                    call["function"]["arguments"] = json.dumps(replacement, ensure_ascii=False) if encoded else replacement
        # Only a pure, acknowledged output group can leave the working view.
        # Reference reads and mixed groups retain every byte and their IDs.
        groups: list[tuple[int, int]] = []
        for index in range(self.view_prefix, len(copied)):
            row = copied[index]
            calls = row.get("tool_calls") if row.get("role") == "assistant" else None
            if not isinstance(calls, list) or not calls:
                continue
            call_ids = [str(call.get("id") or "") for call in calls]
            if (any((call.get("name") or (call.get("function") or {}).get("name")) != "write_result_txt" for call in calls)
                    or any(call_id not in self.acknowledged_calls for call_id in call_ids)):
                continue
            end = index + 1 + len(calls)
            results = copied[index + 1:end]
            if (len(results) == len(calls) and all(item.get("role") == "tool" for item in results)
                    and {str(item.get("tool_call_id") or "") for item in results} == set(call_ids)):
                groups.append((index, end))
        if len(groups) > 2:
            last = block["segments"][-1]["receipt"] if block["segments"] else None
            tail = self.store.artifacts._block_bytes(self.task["id"], self.block_no, block).decode("utf-8")[-RESULT_TAIL_CHARS:]
            structure = _json_continuation_state(self.store.artifacts._block_bytes(self.task["id"], self.block_no, block).decode("utf-8"))
            state = {"role": "user", "content": "[宿主已保存较早的完整工具组；原始轨迹未删除]\n"
                     f"最新TXT回执：{json.dumps(last, ensure_ascii=False)}\nJSON词法状态：{json.dumps(structure, ensure_ascii=False)}"
                     f"\n已保存末尾，仅作接续素材：\n{tail}"}
            removed = {index for start, end in groups[:-2] for index in range(start, end)}
            remainder = [row for index, row in enumerate(copied[self.view_prefix:], self.view_prefix)
                         if index not in removed and not (row.get("role") == "user"
                         and str(row.get("content") or "").startswith("[宿主已保存较早的完整工具组；原始轨迹未删除]"))]
            copied = [*copied[:self.view_prefix], state, *remainder]
        return copied


class ResultExecutor:
    def __init__(self, context: TxtToolContext, view: Any = None):
        self.context, self.view = context, view

    def scan(self) -> list[dict[str, Any]]:
        return self.view.scan() if self.view is not None else []

    def read_skill_content(self, path: str) -> str:
        if self.view is None:
            raise ValueError("本任务没有Skill参考文档")
        return self.view.read_skill_content(path)

    def execute(self, tool: str, arguments: dict[str, Any], active_skills: Any = None,
                run_context: Any = None) -> tuple[bool, str]:
        if tool in RESULT_TOOLS:
            return self.context.execute(tool, arguments)
        if self.view is not None:
            return self.view.execute(tool, arguments, active_skills, run_context)
        return False, "本任务只允许结果写入工具"

    def registry(self) -> ToolRegistry:
        registry = self.view.registry() if self.view is not None else ToolRegistry()
        specs = {
            "write_result_txt": ({"segment": {"type": "integer", "minimum": 1}, "text": {"type": "string"}},
                                 "按连续段号写入当前任务TXT，返回持久化回执。"),
            "complete_result_txt": ({"receipt_id": {"type": "string"}, "segments": {"type": "integer"},
                                      "chars": {"type": "integer"}, "sha256": {"type": "string"}},
                                     "用最新写入回执确认当前业务块完成。"),
        }
        for name, (properties, description) in specs.items():
            registry.register(ToolSpec(name=name, description=description,
                parameters={"type": "object", "properties": properties, "required": list(properties),
                            "additionalProperties": False}, side_effect=True, retryable=False,
                system=True, permission="auto",
                execute=lambda args, active, ctx, tool=name: self.execute(tool, args, active, ctx),
                metadata={"media": {"policy": "never", "extract": "none"}}))
        return registry


def run_result_agent(app: Any, context: TxtToolContext, profile: dict[str, Any],
                     history: list[dict[str, Any]], options: dict[str, Any], system: str,
                     event: Any, usage_sink: dict[str, Any], trace_sink: list[dict[str, Any]],
                     *, view: Any = None) -> str:
    executor = ResultExecutor(context, view)
    registry = executor.registry()
    allowed = [row["name"] for row in registry.schemas()]
    ids = [row["id"] for row in executor.scan()]
    policy = {"mode": "exclusive", "skill_ids": ids}
    run_context = {"skill_policy": policy, "allowed_tools": allowed, "confirmation_ui": False,
                   "cancel_event": options.get("cancel_event"), "executor": executor,
                   "model_key": (context.task.get("input") or {}).get("model_key", ""),
                   "tool_loop_complete": context.completed_text, "model_view_transform": context.transform}
    worker = SkillAgent(executor, executor, None, None)
    all_trace: list[dict[str, Any]] = []
    call_usage: list[dict[str, Any]] = []

    def complete(*args: Any) -> str:
        # ModelRuntime's side channels are thread-local. Clear before this
        # request so an interrupted request cannot inherit the previous usage.
        cleared_usage = cleared_finish = False
        try:
            app.models.last_usage = {}
            cleared_usage = True
        except AttributeError:
            pass
        try:
            app.models.last_finish_reason = ""
            cleared_finish = True
        except AttributeError:
            pass
        observed: dict[str, Any] = {}
        callback = args[3] if len(args) > 3 else None
        def observe(payload: dict[str, Any]) -> None:
            if payload.get("type") == "usage" and isinstance(payload.get("usage"), dict):
                observed.update(copy.deepcopy(payload["usage"]))
            if callback is not None:
                callback(payload)
        forwarded = [*args[:3], observe]
        failed = True
        try:
            result = app.models.complete(*forwarded)
            failed = False
            return result
        finally:
            incoming = getattr(app.models, "last_usage", None) if cleared_usage else None
            usage = dict(observed)
            if isinstance(incoming, dict):
                usage.update(copy.deepcopy(incoming))
            if failed:
                usage.update(observed)
            call_usage.append(usage)
            context.finish_reason = str(getattr(app.models, "last_finish_reason", "") or "") if cleared_finish else ""
            # A usage event can arrive before a transport error and before
            # ModelRuntime publishes its final side channel. Keep it once.
            if observed and cleared_usage:
                app.models.last_usage = usage
            try:
                context.store.artifacts.record_attempt(context.task, context.finish_reason,
                                                      failed=failed, usage_known=bool(usage))
            except (OSError, ValueError):
                if not failed:
                    raise

    # Keep the bound runtime available to SkillAgent's existing usage capture.
    class Runtime:
        def complete(self, *args: Any) -> str:
            return complete(*args)

        def __getattr__(self, name: str) -> Any:
            return getattr(app.models, name)

    worker.model_complete = Runtime().complete

    def bridge(payload: dict[str, Any]) -> None:
        kind = payload.get("type")
        # Raw protocol deltas are not saved content. Writes emit exact TXT bytes.
        if kind in {"usage", "status", "reasoning_delta"}:
            event(payload)
        elif kind in {"context_full", "run_failed"}:
            event({"type": "status", "message": str(payload.get("error") or "生成未完成，TXT片段已保留")})

    current_history = copy.deepcopy(history)
    try:
        for correction in range(MAX_CORRECTIONS + 1):
            context.view_prefix = 1 + len(current_history)
            try:
                text, _runs, _reasonings, usage = worker.run(
                    "", current_history, profile, dict(options), policy, [], "", allowed,
                    bridge, options.get("cancel_event"), max_steps=MAX_RESULT_STEPS,
                    tool_registry=registry, run_context=run_context,
                    system_prompt_override=system + context.instruction())
            finally:
                all_trace.extend(run_context.get(STUDIO_TRACE_MESSAGES_KEY) or [])
            result = context.completed_text()
            if result is not None:
                return result
            if run_context.get("partial") or correction == MAX_CORRECTIONS:
                raise ResultArtifactError("模型未完成TXT写入确认协议，已保留确认片段，可继续未完成内容")
            current_history = [*history, {"role": "user", "content":
                "上次只返回了普通答复，尚未完成TXT结果协议。请按最新回执写入剩余正文并调用完成工具；不要只回复完成。"}]
    finally:
        trace_sink.extend(all_trace or run_context.get(STUDIO_TRACE_MESSAGES_KEY) or [])
        if call_usage:
            merged: dict[str, Any] = {}
            for usage in call_usage:
                for key, value in usage.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        merged[key] = merged.get(key, 0) + value
                    elif value:
                        merged[key] = value
            merged["requests"] = len(call_usage)
            merged["records"] = call_usage[-256:]
            usage_sink.clear()
            usage_sink.update(merged)
