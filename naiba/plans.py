"""交互模式（普通 / Plan）与 Plan 模式生命周期管理。

模式语义：
- craft：内部兼容名称，代表普通模式，可用全部已启用工具。
- plan：准备阶段只读；用户确认执行后，执行器按普通模式能力逐步执行。

Plan 生命周期：prepare → ready → building → finished / failed / cancelled。
SQLite 保存权威状态，同时把计划归档到配置工作区的 `.naiba-chat/plans/`。
"""
from __future__ import annotations

from naiba.core.contracts import AppContext
from naiba.core.tool_results import display_tool_run

import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from naiba.skills.agent import SkillAgent
from naiba.core.exceptions import TaskCancelled
from naiba.tools.executor import ToolExecutor
from naiba.core.history import build_model_history

INTERACTION_MODES = ("craft", "plan")


def normalize_interaction_mode(value: Any) -> str:
    """Normalize persisted/UI values to the two supported interaction modes."""
    return "plan" if str(value or "").strip().lower() == "plan" else "craft"

ALL_TOOLS = (
    "read_file",
    "write_file",
    "list_directory",
    "search_files",
    "run_skill_script",
    "http_request",
    "edit_file",
    "pwsh",
)
READONLY_TOOLS = {"read_file", "list_directory", "search_files", "http_request"}

PLAN_PREPARE_PROMPT = (
    "## Plan 计划模式\n"
    "当前处于 Plan 模式：目标是产出一份经用户确认后再执行的实施方案。"
    "准备阶段你只能只读探索（读取/搜索文件、列目录、GET/HEAD 请求），禁止任何写入或副作用操作。\n"
    "工作流程：\n"
    "1. 澄清需求：需求不明确或缺少关键信息时，先用中文提出最关键的 1~3 个问题。"
    "可以用「请选择……」加编号选项的格式向用户收集选择（每组编号从 1 重新开始）。信息不足时不要输出方案。\n"
    "2. 只读探索：必要时用只读工具了解代码库或相关资料。\n"
    "3. 输出方案：信息足够时，输出完整实施方案，并用 <plan> 标签严格包裹，结构如下：\n\n"
    "<plan>\n"
    "# 计划标题\n"
    "## 方案概述\n"
    "（整体思路、关键决策、影响范围）\n"
    "## 实施步骤\n"
    "1. 步骤标题：具体做什么，涉及哪些文件/模块\n"
    "2. ……\n"
    "</plan>\n\n"
    "要求：步骤 3~8 步、可执行、有先后顺序；<plan> 标签外不要输出多余内容；"
    "信息仍不足时继续提问，不要输出 <plan>。"
)

PLAN_BLOCK_RE = re.compile(r"<plan>(.*?)</plan>", re.S | re.I)
STEP_LINE_RE = re.compile(r"^(?:\d{1,2}[.、)．]|- \[[ xX]\])\s*(.+)$")
STEPS_HEADING_RE = re.compile(r"步骤|实施|流程|安排")


def resolve_mode_tools(
    mode: str, agent_tools: list[str], readonly_mcp_tools: list[str] | None = None
) -> list[str]:
    """按交互模式解析可用工具：与全局 agent_tools 取交集，Plan 再过滤为只读。

    Plan 额外允许标注为只读的 MCP 工具（如 ComfyUI 的环境/模型/工作流查询），
    但 ``run_workflow`` 等有副作用工具始终排除。
    """
    configured = [tool for tool in (agent_tools or []) if tool in ALL_TOOLS]
    # Keep legacy persisted Ask runs read-only while the UI/API no longer expose Ask.
    if str(mode or "").strip().lower() in {"ask", "plan"}:
        result = [tool for tool in configured if tool in READONLY_TOOLS]
        for mcp_tool in readonly_mcp_tools or []:
            if mcp_tool and mcp_tool not in result and not mcp_tool.endswith("__run_workflow"):
                result.append(mcp_tool)
        return result
    return configured


class ReadOnlyToolExecutor:
    """只读工具执行代理：禁止写类工具与 MCP，http_request 仅允许 GET/HEAD。

    写类工具在 SkillAgent 层已通过 allowed_tools 过滤，本代理作为最后防线。
    """

    BLOCKED_TOOLS = {"write_file", "edit_file", "pwsh", "run_skill_script", "register_mcp"}

    def __init__(self, inner: ToolExecutor):
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(
        self,
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        if tool in self.BLOCKED_TOOLS or "." in tool:
            return False, f"当前为只读模式，已禁止工具：{tool}"
        # 只读模式始终禁止 ComfyUI 的 run_workflow 等具有副作用的 MCP 工具。
        if tool.endswith("__run_workflow"):
            return False, f"当前为只读模式，已禁止工具：{tool}"
        if tool == "http_request":
            method = str((arguments or {}).get("method") or "GET").upper()
            if method not in {"GET", "HEAD"}:
                return False, f"只读模式仅允许 GET/HEAD 请求（收到 {method}）"
        return self._inner.execute(tool, arguments, active_skills, run_context)


class CraftToolExecutor:
    """Craft policy: auto-allow workspace writes, keep other policy checks."""

    def __init__(self, inner: ToolExecutor):
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(
        self,
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        if tool in {"write_file", "edit_file"} and getattr(self._inner, "permission_mode", "confirm") != "deny":
            # 与引擎判定同源：工作区取当前 Run（run_context.workspace_dir → executor.workspace），
            # 包装层不得自行使用装配期工作区，否则同一路径在两层会得到不同结论。
            workspace = self._inner.workspace_for_run(run_context)
            path = self._inner.resolve_tool_path((arguments or {}).get("path"), workspace=workspace)
            if self._inner.path_within(path, workspace):
                return self._inner.execute_unchecked(tool, arguments, active_skills, run_context)
        return self._inner.execute(tool, arguments, active_skills, run_context)


def parse_plan_document(content: str) -> tuple[str, list[dict[str, Any]]]:
    """从 <plan> 内容解析标题与步骤。步骤优先取“实施步骤”类标题下的编号行。"""
    title = ""
    candidates: list[tuple[bool, str]] = []
    in_steps = False
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if not title:
                title = heading
            if STEPS_HEADING_RE.search(heading):
                in_steps = True
            continue
        match = STEP_LINE_RE.match(line)
        if match:
            text = match.group(1).strip()
            if text:
                candidates.append((in_steps, text))
    preferred = [text for flagged, text in candidates if flagged]
    source = preferred or [text for _, text in candidates]
    steps: list[dict[str, Any]] = []
    for index, text in enumerate(source, 1):
        step_title = re.split(r"[：:]", text, maxsplit=1)[0].strip() or text[:30]
        steps.append(
            {
                "id": index,
                "title": step_title[:80],
                "detail": text,
                "status": "pending",
                "summary": "",
            }
        )
    return title[:200], steps


def extract_plan_block(response: str) -> tuple[str | None, str]:
    """提取回复中的 <plan> 块，返回 (计划内容, 去除计划块后的回复)。"""
    match = PLAN_BLOCK_RE.search(response or "")
    if not match:
        return None, response
    content = match.group(1).strip()
    cleaned = (response[: match.start()] + response[match.end():]).strip()
    return content, cleaned


def _merge_steps(old_steps: list[dict[str, Any]], new_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """编辑计划后重新解析步骤：标题一致的步骤保留原进度。"""
    old_by_title = {str(step.get("title") or ""): step for step in old_steps if isinstance(step, dict)}
    for step in new_steps:
        old = old_by_title.get(step["title"])
        if old and old.get("status") in {"done", "failed"}:
            step["status"] = old["status"]
            step["summary"] = str(old.get("summary") or "")
    return new_steps


def render_plan_markdown(plan: dict[str, Any]) -> str:
    def fmt(timestamp: Any) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(timestamp) / 1000))
        except (TypeError, ValueError, OSError):
            return "-"

    status_names = {
        "prepare": "准备中",
        "ready": "待确认",
        "building": "执行中",
        "finished": "已完成",
        "failed": "执行失败",
        "cancelled": "已取消",
    }
    lines = [
        f"# {plan.get('title') or '未命名计划'}",
        "",
        f"- 计划 ID：{plan.get('id')}",
        f"- 对话 ID：{plan.get('conversation_id')}",
        f"- 状态：{status_names.get(plan.get('status'), plan.get('status'))}",
        f"- 创建时间：{fmt(plan.get('created_at'))}",
        f"- 更新时间：{fmt(plan.get('updated_at'))}",
        "",
        "## 需求",
        "",
        str(plan.get("question") or "").strip() or "（无）",
        "",
        "## 方案",
        "",
        str(plan.get("content") or "").strip() or "（尚未生成方案）",
        "",
        "## 步骤进度",
        "",
    ]
    steps = plan.get("steps") or []
    if not steps:
        lines.append("（暂无步骤）")
    for step in steps:
        status = str(step.get("status") or "pending")
        checkbox = "x" if status == "done" else " "
        suffix = f" — {step.get('summary')}" if step.get("summary") else ""
        lines.append(f"- [{checkbox}] {step.get('id')}. {step.get('title')}（{status}）{suffix}")
    if plan.get("error"):
        lines.extend(["", "## 错误", "", str(plan["error"])])
    return "\n".join(lines) + "\n"


class PlanManager:
    ACTIVE_STATUSES = {"prepare", "ready", "building"}
    EXECUTABLE_STATUSES = {"ready", "failed", "cancelled"}
    EDITABLE_STATUSES = {"prepare", "ready", "failed", "cancelled"}

    def __init__(self, app: AppContext, step_runner: Callable[..., str] | None = None):
        self.app = app
        self._lock = threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._step_runner = step_runner or self._run_step_with_agent

    # ---- 查询 ----
    def get(self, plan_id: str) -> dict[str, Any] | None:
        return self.app.storage.get_plan(plan_id)

    def active_plan(self, conversation_id: str) -> dict[str, Any] | None:
        plan = self.app.storage.latest_plan(conversation_id)
        if plan and plan.get("status") in self.ACTIVE_STATUSES:
            return plan
        return None

    # ---- 准备阶段 ----
    def ensure_active_plan(self, conversation_id: str, question: str) -> dict[str, Any]:
        """Plan 模式下用户发消息：复用准备中的计划、就绪计划回到准备阶段，否则创建新计划。"""
        with self._lock:
            plan = self.app.storage.latest_plan(conversation_id)
            if plan:
                status = plan.get("status")
                if status == "building":
                    raise ValueError("当前计划正在执行中，请先取消或等待完成")
                if status == "prepare":
                    updated = self.app.storage.update_plan(plan["id"], question=question)
                    return updated or plan
                if status == "ready":
                    updated = self.app.storage.update_plan(
                        plan["id"], status="prepare", question=question, error="",
                        detail={"message": "正在准备计划", "clarification_round": 0},
                    )
                    return updated or plan
            return self.app.storage.create_plan(conversation_id, question)

    def prepare_prompt(self, plan: dict[str, Any] | None) -> str:
        prompt = PLAN_PREPARE_PROMPT
        if plan and int((plan.get("detail") or {}).get("clarification_round") or 0) >= 1:
            prompt += "\n\n已经完成一轮澄清；即使仍有未知信息，也请明确列出假设并直接输出完整 <plan>，不要继续提问。"
        if plan and str(plan.get("content") or "").strip():
            prompt += (
                "\n\n当前已有一版方案（用户的新消息是对它的修订意见，"
                "请吸收修订意见后重新输出完整的 <plan>）：\n" + str(plan["content"])[:20000]
            )
        return prompt

    @staticmethod
    def needs_plan_compilation(plan: dict[str, Any] | None, response: str) -> bool:
        """Return true when a Plan reply ignored the required plan envelope.

        Genuine clarification questions stay conversational. A direct answer,
        or any reply after one clarification round, is compiled into a plan so
        the UI cannot remain stuck in the prepare state indefinitely.
        """
        if not plan or PLAN_BLOCK_RE.search(response or ""):
            return False
        text = str(response or "").strip()
        clarification_markers = (
            "请选择", "请提供", "请补充", "请确认", "请告诉我", "需要你选择",
        )
        if any(marker in text for marker in clarification_markers):
            return False
        if re.search(r"(?m)^\s*(?:\d+[.、)]|[A-D][.、)])\s*\S+", text):
            return False
        rounds = int((plan.get("detail") or {}).get("clarification_round") or 0)
        if rounds >= 1:
            return True
        return bool(text) and "?" not in text and "？" not in text

    @staticmethod
    def plan_compilation_messages(
        plan: dict[str, Any], response: str
    ) -> list[dict[str, str]]:
        """Build a tool-free repair request that only emits a plan document."""
        question = str(plan.get("question") or "").strip()
        draft = str(response or "").strip()[:20000]
        return [
            {
                "role": "system",
                "content": (
                    "你是实施计划编译器。只输出一个完整的 <plan>...</plan> 块，"
                    "不得调用工具，不得添加标签外说明。计划必须包含 Markdown 一级标题、"
                    "方案概述，以及 3 到 8 条按顺序编号且可执行的实施步骤。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户目标：\n{question}\n\n"
                    f"上一轮模型提供的材料：\n{draft}\n\n"
                    "信息不足之处请列为明确假设，不要继续提问，直接生成实施计划。"
                ),
            },
        ]

    def process_response(self, plan_id: str, response: str) -> tuple[str, dict[str, Any] | None]:
        """处理 Plan 模式的模型回复：检测到 <plan> 块则落库为就绪计划。"""
        plan = self.app.storage.get_plan(plan_id)
        if not plan:
            return response, None
        content, cleaned = extract_plan_block(response)
        if content is None:
            detail = dict(plan.get("detail") or {})
            detail["clarification_round"] = min(1, int(detail.get("clarification_round") or 0) + 1)
            detail["message"] = "等待用户补充信息；下一轮将基于现有信息生成计划"
            updated = self.app.storage.update_plan(plan_id, status="prepare", detail=detail)
            return response, updated or plan
        title, steps = parse_plan_document(content)
        if not steps:
            updated = self.app.storage.update_plan(
                plan_id,
                status="prepare",
                error="计划内容中没有识别到可执行步骤",
                detail={"message": "未识别到有效步骤，请重新生成计划"},
            )
            return response, updated or plan
        if not title:
            title = (plan.get("question") or "").strip()[:30] or "实施计划"
        updated = self.app.storage.update_plan(
            plan_id,
            title=title,
            content=content,
            steps=steps,
            status="ready",
            error="",
            detail={"message": "方案已生成，等待用户确认"},
        )
        if updated:
            self.archive(updated)
        note = "实施方案已生成，请查看下方计划卡片；确认无误后点击「开始执行」，如需调整可直接回复修改意见。"
        return ((cleaned + "\n\n" + note).strip() if cleaned else note), updated

    # ---- 编辑 ----
    def edit_plan(
        self,
        plan_id: str,
        title: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        plan = self.app.storage.get_plan(plan_id)
        if not plan:
            raise LookupError("计划不存在")
        if plan.get("status") not in self.EDITABLE_STATUSES:
            raise ValueError("计划正在执行中，无法编辑")
        if title is not None and not isinstance(title, str):
            raise ValueError("title 必须是文本")
        if content is not None and not isinstance(content, str):
            raise ValueError("content 必须是文本")
        new_title = plan.get("title") if title is None else title.strip()
        if content is None:
            updated = self.app.storage.update_plan(plan_id, title=new_title)
        else:
            parsed_title, parsed_steps = parse_plan_document(content)
            if not new_title:
                new_title = parsed_title
            steps = _merge_steps(plan.get("steps") or [], parsed_steps)
            updated = self.app.storage.update_plan(
                plan_id,
                title=new_title,
                content=content.strip(),
                steps=steps,
                status="ready" if parsed_steps else "prepare",
                error="",
            )
        if not updated:
            raise LookupError("计划不存在")
        self.archive(updated)
        return updated

    def keep_planning(self, plan_id: str) -> dict[str, Any]:
        """Return a reviewed plan to preparation so the next user note is feedback."""
        plan = self.app.storage.get_plan(plan_id)
        if not plan:
            raise LookupError("计划不存在")
        if plan.get("status") not in {"ready", "prepare"}:
            raise ValueError("当前计划不能继续规划")
        detail = dict(plan.get("detail") or {})
        detail.update({"message": "继续规划中，等待用户补充意见", "review": "keep_planning"})
        updated = self.app.storage.update_plan(plan_id, status="prepare", detail=detail, error="")
        if not updated:
            raise LookupError("计划不存在")
        return updated

    # ---- 执行 ----
    def validate_execution(self, plan_id: str) -> dict[str, Any]:
        """Validate that a plan can start without mutating its state."""
        plan = self.app.storage.get_plan(plan_id)
        if not plan:
            raise LookupError("计划不存在")
        status = plan.get("status")
        if status == "building":
            raise ValueError("计划已在执行中")
        if status not in self.EXECUTABLE_STATUSES:
            names = {"prepare": "方案尚未生成", "finished": "计划已完成"}
            raise ValueError(names.get(status, f"当前状态（{status}）不能执行"))
        steps = plan.get("steps") or []
        if not steps:
            raise ValueError("计划没有可执行的步骤")
        if all(step.get("status") == "done" for step in steps):
            raise ValueError("所有步骤已完成")
        return plan

    def prepare_execution(self, plan_id: str, run_id: str = "") -> dict[str, Any]:
        """Move a validated plan into building state for its owning run."""
        with self._lock:
            plan = self.validate_execution(plan_id)
            steps = plan.get("steps") or []
            for step in steps:
                if step.get("status") in {"running", "failed"}:
                    step["status"] = "pending"
                    step["summary"] = ""
            updated = self.app.storage.update_plan(
                plan_id,
                status="building",
                steps=steps,
                error="",
                detail={"message": "准备执行计划", "run_id": run_id},
                started=not plan.get("started_at"),
            )
            if updated:
                self.archive(updated)
            return updated or plan

    def execute(self, plan_id: str) -> dict[str, Any]:
        """Legacy in-process entry used by tests; the server uses ConversationRunManager."""
        with self._lock:
            self.validate_execution(plan_id)
            cancel_event = threading.Event()
            self._cancel_events[plan_id] = cancel_event
            updated = self.prepare_execution(plan_id)
            thread = threading.Thread(
                target=self._execute_loop,
                args=(plan_id, cancel_event),
                name=f"naiba-plan-{plan_id[:8]}",
                daemon=True,
            )
            self._threads[plan_id] = thread
            thread.start()
            return updated

    def run_execution(
        self,
        plan_id: str,
        cancel_event: threading.Event,
        event: Callable[[dict[str, Any]], None],
        snapshot: dict[str, Any] | None = None,
        run_executor: ToolExecutor | None = None,
    ) -> dict[str, Any] | None:
        """Run an already-prepared plan inside its ConversationRunManager thread."""
        self._execute_loop(
            plan_id,
            cancel_event,
            external_event=event,
            manage_registry=False,
            execution_snapshot=snapshot,
            execution_executor=run_executor,
        )
        return self.app.storage.get_plan(plan_id)

    def cancel(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            plan = self.app.storage.get_plan(plan_id)
            if not plan:
                raise LookupError("计划不存在")
            status = plan.get("status")
            if status == "finished":
                raise ValueError("计划已完成，无法取消")
            if status in {"failed", "cancelled"}:
                return plan
            event = self._cancel_events.get(plan_id)
            if event:
                event.set()
            elif status == "building":
                runs = getattr(self.app, "runs", None)
                if runs is not None:
                    runs.cancel_plan(plan_id)
            if status in {"prepare", "ready"}:
                updated = self.app.storage.update_plan(
                    plan_id, status="cancelled", detail={"message": "已取消"}, finished=True
                )
                if updated:
                    self.archive(updated)
                    return updated
                return plan
            # building：执行循环检测到取消事件后自行收尾
            updated = self.app.storage.update_plan(
                plan_id, detail={"message": "正在取消计划执行"}
            )
            return updated or plan

    def _execute_loop(
        self,
        plan_id: str,
        cancel_event: threading.Event,
        external_event: Callable[[dict[str, Any]], None] | None = None,
        manage_registry: bool = True,
        execution_snapshot: dict[str, Any] | None = None,
        execution_executor: ToolExecutor | None = None,
    ) -> None:
        initial = self.app.storage.get_plan(plan_id) or {}
        run_id = str((initial.get("detail") or {}).get("run_id") or "")

        def detail_with_run(detail: dict[str, Any]) -> dict[str, Any]:
            return {**detail, **({"run_id": run_id} if run_id else {})}

        def event(payload: dict[str, Any]) -> None:
            if cancel_event.is_set():
                raise TaskCancelled("计划已取消")
            if external_event is not None:
                external_event(payload)
            kind = str(payload.get("type") or "")
            detail: dict[str, Any] | None = None
            if kind == "status":
                detail = {"message": str(payload.get("message") or "正在执行")}
            elif kind == "tool_start":
                detail = {"message": f"正在执行 {payload.get('tool') or '工具'}", "tool": str(payload.get("tool") or "")}
            elif kind == "tool_confirm":
                detail = {
                    "message": "等待工具确认",
                    "tool": str(payload.get("tool_name") or ""),
                    "tool_desc": str(payload.get("tool_desc") or ""),
                    "arguments": payload.get("arguments") or {},
                    "confirm_id": str(payload.get("confirm_id") or ""),
                }
            elif kind == "tool_result":
                detail = {"message": f"工具 {payload.get('tool') or ''} 执行完毕"}
            if detail is not None:
                try:
                    self.app.storage.update_plan(plan_id, detail=detail_with_run(detail))
                except Exception:
                    pass

        try:
            while True:
                if cancel_event.is_set():
                    raise TaskCancelled("计划已取消")
                plan = self.app.storage.get_plan(plan_id)
                if not plan:
                    return
                steps = plan.get("steps") or []
                next_index = next(
                    (index for index, step in enumerate(steps) if step.get("status") != "done"),
                    None,
                )
                if next_index is None:
                    break
                step = steps[next_index]
                step["status"] = "running"
                self.app.storage.update_plan(
                    plan_id,
                    steps=steps,
                    detail=detail_with_run(
                        {"message": f"正在执行第 {next_index + 1}/{len(steps)} 步：{step.get('title')}"}
                    ),
                )
                if execution_snapshot is not None and self._step_runner == self._run_step_with_agent:
                    summary = self._run_step_with_agent(
                        plan,
                        step,
                        next_index,
                        len(steps),
                        cancel_event,
                        event,
                        execution_snapshot,
                        execution_executor,
                    )
                else:
                    summary = self._step_runner(plan, step, next_index, len(steps), cancel_event, event)
                step["status"] = "done"
                step["summary"] = re.sub(r"\s+", " ", str(summary or "")).strip()[:500]
                self.app.storage.update_plan(plan_id, steps=steps)
                self.archive(self.app.storage.get_plan(plan_id) or plan)
            finished = self.app.storage.update_plan(
                plan_id,
                status="finished",
                detail=detail_with_run({"message": "计划已完成"}),
                finished=True,
            )
            if finished:
                self.archive(finished)
        except TaskCancelled:
            plan = self.app.storage.get_plan(plan_id)
            if plan:
                steps = plan.get("steps") or []
                for step in steps:
                    if step.get("status") == "running":
                        step["status"] = "pending"
                updated = self.app.storage.update_plan(
                    plan_id,
                    status="cancelled",
                    steps=steps,
                    detail=detail_with_run({"message": "已取消"}),
                    finished=True,
                )
                if updated:
                    self.archive(updated)
        except Exception as exc:
            traceback.print_exc()
            plan = self.app.storage.get_plan(plan_id)
            if plan:
                steps = plan.get("steps") or []
                for step in steps:
                    if step.get("status") == "running":
                        step["status"] = "failed"
                updated = self.app.storage.update_plan(
                    plan_id,
                    status="failed",
                    steps=steps,
                    error=str(exc),
                    detail=detail_with_run({"message": f"执行失败：{exc}"}),
                    finished=True,
                )
                if updated:
                    self.archive(updated)
        finally:
            if manage_registry:
                with self._lock:
                    self._cancel_events.pop(plan_id, None)
                    self._threads.pop(plan_id, None)

    def _run_step_with_agent(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        index: int,
        total: int,
        cancel_event: threading.Event,
        event: Callable[[dict[str, Any]], None],
        snapshot: dict[str, Any] | None = None,
        run_executor: ToolExecutor | None = None,
    ) -> str:
        """默认步骤执行器：以 Craft 能力运行 SkillAgent 完成单个步骤。"""
        conversation_id = str(plan.get("conversation_id") or "")
        conversation = self.app.storage.get_conversation(conversation_id)
        if not conversation:
            raise RuntimeError("发起计划的对话已删除")
        frozen = snapshot or {}
        # 与主对话/子代理同参数（plan 模式当前已封存，但签名与口径保持一致，
        # 复活时不会因为只走默认值而让同一会话出现两种回放字节）。
        history = build_model_history(
            frozen.get("conversation_messages") or conversation.get("messages", []),
            **self.app.config.reasoning_replay_options(),
        )
        model_key = str(frozen.get("model_key") or conversation.get("model_key") or "")
        if not model_key:
            provider_id = str(frozen.get("provider_id") or conversation.get("provider_id") or "")
            model_key = f"online:{provider_id}" if provider_id else ""
        profile = self.app.config.profile(model_key)
        if frozen.get("generation_options"):
            options = dict(frozen["generation_options"])
        else:
            getter = self.app.config.generation_options
            try:
                options = dict(getter(model_key))
            except TypeError as first_error:
                try:
                    options = dict(getter())
                except TypeError:
                    raise first_error
        options["stream"] = bool(frozen.get("stream_enabled", conversation.get("stream_enabled", 1)))
        # 本地锁占用者标签：计划执行也是本地锁的潜在占用方，标上角色便于等锁方归因。
        options["lock_label"] = "计划执行"
        agent = frozen.get("agent") or self.app.config.get_agent(str(conversation.get("agent_id") or "")) or {}
        # 系统提示词只有一个来源：Agent（会话级系统提示词已移除）。
        combined_prompt = str(agent.get("system_prompt") or "").strip() or str(
            self.app.config.data.get("agent_system_prompt", "")
        )
        done_steps = [
            item for item in (plan.get("steps") or []) if item.get("status") == "done"
        ]
        progress = "\n".join(
            f"{item.get('id')}. {item.get('title')}：{item.get('summary') or '已完成'}"
            for item in done_steps
        ) or "（尚无已完成步骤）"
        instruction = (
            f"你正在执行已获用户确认的实施计划「{plan.get('title') or '未命名计划'}」。\n\n"
            f"计划方案：\n{str(plan.get('content') or '')[:8000]}\n\n"
            f"已完成步骤：\n{progress}\n\n"
            f"当前步骤（第 {index + 1}/{total} 步）：{step.get('title')}\n"
            f"{step.get('detail') or ''}\n\n"
            "请专注完成当前步骤；完成后用中文简要汇报该步骤结果（不超过 3 句话）。"
        )
        allowed_tools = [str(item) for item in frozen.get("allowed_tools") or resolve_mode_tools(
            "craft", [str(t) for t in self.app.config.data.get("agent_tools", [])]
        )]
        executor = CraftToolExecutor(run_executor or self.app.executor)
        worker = SkillAgent(
            self.app.catalog,
            executor,
            self.app.models.complete,
            getattr(self.app, "media_collector", None),
        )
        skill_policy = frozen.get("skill_policy") or {"mode": "auto", "skill_ids": []}
        content, runs, reasonings, usage = worker.run(
            instruction,
            history,
            profile,
            options,
            skill_policy,
            [],
            combined_prompt,
            allowed_tools,
            event,
            cancel_event,
            tool_registry=self.app.tool_registry,
            run_context={
                "run_id": str((plan.get("detail") or {}).get("run_id") or ""),
                "conversation_id": conversation_id,
                "owner_session_id": conversation_id,
                "depth": 0,
                "allowed_tools": list(allowed_tools),
                "skill_policy": dict(skill_policy),
                "job_registry": getattr(self.app, "jobs", None),
                "executor": executor,
                "cancel_event": cancel_event,
                # 工作区随 Run 快照冻结（与主会话同源：判定/执行/产物目录都用它）
                "workspace_dir": str(frozen.get("workspace_dir") or ""),
                # 计划步骤里的 pwsh / 脚本同样要可见（与主会话同一出口）。
                "event_sink": event,
            },
        )
        self.app.storage.add_message(
            conversation_id,
            "assistant",
            content,
            {
                "plan_id": plan.get("id"),
                "plan_step": step.get("id"),
                "plan_step_title": step.get("title"),
                # 前端/历史展示与模型上下文同源（core.tool_results）。
                "tool_runs": [display_tool_run(run) for run in runs],
                "reasoning": reasonings,
                "usage": usage,
            },
        )
        return content

    # ---- 归档 ----
    def archive(self, plan: dict[str, Any]) -> None:
        """把计划快照写入工作区 .naiba-chat/plans/ 目录（Markdown）。"""
        try:
            workspace = Path(self.app.config.data["workspace_dir"]).expanduser().resolve()
            folder = workspace / ".naiba-chat" / "plans"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{plan['id']}.md"
            path.write_text(render_plan_markdown(plan), encoding="utf-8")
            if plan.get("archive_path") != str(path):
                self.app.storage.update_plan(plan["id"], archive_path=str(path))
        except OSError:
            traceback.print_exc()

    def shutdown(self) -> None:
        with self._lock:
            events = list(self._cancel_events.values())
        for event in events:
            event.set()
