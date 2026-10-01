"""工具引擎（ToolExecutor，单轨形态 Phase 5）。

职责仅剩：
- 权限确认状态机（confirm/auto/full/deny + NEED_CONFIRM 协议：pending/confirm/reject/wait/异步）；
- def 级 policy 评估（经注入的 def 解析器）与执行分发（经 def.execute）；
- 路径解析公开助手（resolve_tool_path / path_within，供 ReadOnly/Craft 包装器使用）。

工具实现一律在 ``naiba/tools/providers/`` 各域模块（def.execute 绑定），
本模块不持有任何工具实现（``_tool_*`` 方法、TOOL_ALIASES、DANGEROUS_TOOLS 已于 Phase 5 废除）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from naiba.core.diagnostics import _permission_debug_enabled
from naiba.core.exceptions import TaskCancelled
from naiba.core.paths import path_within
from naiba.mcp import MCPRegistry
from naiba.tools.providers.core import ToolContext, _resolve_tool_path, run_workspace

logger = logging.getLogger("naiba.tools.executor")


class ToolExecutor:
    VALID_PERMISSION_MODES = {"confirm", "auto", "full", "deny"}

    def __init__(
        self,
        workspace: Path,
        python_executable: str,
        command_timeout: int,
        mcp_registry: MCPRegistry,
        permission_mode: str = "confirm",
        mcp_register: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.workspace = workspace.resolve()
        self.python_executable = python_executable
        self.command_timeout = command_timeout
        self.mcp_registry = mcp_registry
        self.mcp_register = mcp_register
        self.permission_mode = "confirm"
        self.set_permission_mode(permission_mode)
        self.pending_confirmation: dict[str, dict[str, Any]] = {}
        self.confirmation_results: dict[str, tuple[bool, str]] = {}
        self._confirmation_lock = threading.RLock()
        self._def_resolver: Callable[[str], Any] | None = None
        self._alias_resolver: Callable[[str], str] | None = None
        # 诊断标签（如 "run:<id>" / "app"）：仅在权限诊断开启时写日志，判定逻辑不读它
        self.debug_label: str = ""
        # Run 级「围栏来源动作」一次授权（计划 2026-10-01 §3.4）：
        # 只在当前 executor（= 当前 Run 的隔离实例）内有效，随 Run 结束/切换工作区/
        # 工具集合变化而失效；不写全局配置、不跨 Run 复用。
        self._fenced_approval: dict[str, Any] | None = None

    # ---- 围栏来源动作的 Run 级授权（不改变既有权限策略） ----
    def grant_fenced_approval(self, allowed_tools: Any, workspace: Any = "") -> None:
        """记录「允许本轮继续执行后续操作」：绑定当前 Run 的允许工具集合与工作区。"""
        self._fenced_approval = {
            "tools": sorted(str(item) for item in (allowed_tools or [])),
            "workspace": str(workspace or ""),
        }

    def fenced_approval_active(self, allowed_tools: Any, workspace: Any = "") -> bool:
        """当前 Run 是否已获得围栏动作授权（工具集合或工作区变化即失效）。"""
        approval = self._fenced_approval
        if not approval:
            return False
        return (
            approval.get("tools") == sorted(str(item) for item in (allowed_tools or []))
            and str(approval.get("workspace") or "") == str(workspace or "")
        )

    # ---- 注入 ----
    def set_def_resolver(self, resolver: Callable[[str], Any] | None) -> None:
        """注入工具定义解析器（ToolRegistry.get 或等价物）：def 级 policy / execute 查源。"""
        self._def_resolver = resolver

    def set_alias_resolver(self, resolver: Callable[[str], str] | None) -> None:
        """注入别名解析器（ToolRegistry.resolve）：查询层归一，引擎不再持有别名表。"""
        self._alias_resolver = resolver

    def _resolve_tool_name(self, tool: str) -> str:
        if self._alias_resolver is not None:
            return self._alias_resolver(tool)
        return tool

    def _tool_context(self) -> ToolContext:
        return ToolContext(
            workspace=self.workspace,
            python_executable=self.python_executable,
            command_timeout=self.command_timeout,
            mcp_registry=self.mcp_registry,
            mcp_register=self.mcp_register,
        )

    # ---- 公开路径助手（供 ReadOnly/Craft 包装器使用） ----
    def resolve_tool_path(
        self, raw: Any, default_workspace: bool = False, workspace: Path | None = None
    ) -> Path:
        return _resolve_tool_path(self._tool_context(), raw, default_workspace, workspace)

    def workspace_for_run(self, run_context: dict[str, Any] | None = None) -> Path:
        """当前运行（会话级）工作区：run_context 携带值优先，其次本 executor 的工作区。

        包装器（Craft/ReadOnly）与引擎判定必须共用这一个来源，避免判定与执行分叉。
        """
        return run_workspace(run_context) or Path(self.workspace)

    def path_within(self, path: Path, root: Path) -> bool:
        return path_within(path, root)

    def set_permission_mode(self, mode: str) -> None:
        normalized = str(mode or "confirm").strip().lower()
        self.permission_mode = normalized if normalized in self.VALID_PERMISSION_MODES else "confirm"

    def clone_for_permission(self, mode: str) -> "ToolExecutor":
        """Create an isolated executor for one Run while sharing external services."""
        clone = ToolExecutor(
            self.workspace,
            self.python_executable,
            self.command_timeout,
            self.mcp_registry,
            permission_mode=mode,
            mcp_register=self.mcp_register,
        )
        clone._def_resolver = self._def_resolver
        clone._alias_resolver = self._alias_resolver
        clone.debug_label = self.debug_label
        return clone

    # ---- 权限确认 ----
    def _confirmation_reason(
        self,
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> str:
        """确认理由评估入口：只做诊断日志出口，判定逻辑见 _evaluate_confirmation_reason。"""
        reason = self._evaluate_confirmation_reason(tool, arguments, active_skills, run_context)
        if _permission_debug_enabled():
            # warning 级：仓库不配置 logging，info 级不会输出（诊断开关打开时才记录）
            logger.warning(
                "[PERM] executor=%s mode=%s tool=%s workspace=%s reason=%s",
                self.debug_label or "<app>",
                self.permission_mode,
                self._resolve_tool_name(tool),
                self.workspace,
                reason or "<免确认>",
            )
        return reason

    def _evaluate_confirmation_reason(
        self,
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> str:
        tool = self._resolve_tool_name(tool)
        if self.permission_mode == "full":
            return ""
        spec = self._def_resolver(tool) if self._def_resolver is not None else None
        if spec is not None and getattr(spec, "policy", None) is not None:
            try:
                return str(
                    spec.policy(
                        tool, arguments, active_skills, self.permission_mode, run_context, self.workspace,
                    ) or ""
                )
            except Exception as exc:
                # 策略异常按需确认处理（fail-closed，不静默放行）
                return f"权限策略评估失败：{type(exc).__name__}: {exc}"
        if spec is None:
            return ""  # 未登记工具：交由执行分发返回"未知工具"
        if not spec.side_effect:
            return ""
        if self.permission_mode == "auto":
            return ""
        return f"执行工具需要确认：{tool}"

    def _enqueue_confirmation(
        self,
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        reason: str,
        run_context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        recheck: Callable[[str, dict[str, Any]], str] | None = None,
    ) -> tuple[bool, str]:
        """登记一次待确认并返回 ``NEED_CONFIRM`` 协议串（唯一的协议构造点）。

        ``recheck``（计划 2026-10-01 §2.2）是**确认后的复核入口**：包装执行器
        （ReadOnly 等）把「本包装器自己的策略判定」随待确认项一起存下来，
        批准时先复核一遍再落底层 ``execute_unchecked``——确认后的执行因此
        不会绕过包装层（例如只读模式下的 POST / run_workflow）。
        """
        tool = self._resolve_tool_name(tool)
        if self.permission_mode == "deny":
            return False, f"权限被拒绝：{reason}（工具：{tool}）"
        confirm_id = str(uuid.uuid4())
        pending = {
            "tool": tool,
            "arguments": arguments,
            "active_skills": active_skills,
            "run_context": run_context,
            "processing": False,
        }
        if callable(recheck):
            pending["recheck"] = recheck
        if extra:
            pending.update(extra)
        with self._confirmation_lock:
            # run_context 一并暂存：批准执行时必须复用原 Run 的工作区/取消信号/权限模式，
            # 以及依赖 run_context 的工具行为（产物目录、job 归属、reset_context 等）。
            self.pending_confirmation[confirm_id] = pending
        # NEED_CONFIRM 协议以半角冒号分三段解析（agent.py split(":", 3)）；确认理由中
        # 的 Windows 盘符（C:\…）含半角冒号会截断描述文本，故仅对确认理由做全角化。
        reason_safe = str(reason or "").replace(":", "：")
        return False, (
            f"NEED_CONFIRM:{confirm_id}:{reason_safe}:"
            f"{json.dumps(arguments, ensure_ascii=False)[:500]}"
        )

    def request_confirmation(
        self,
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        reason: str,
        run_context: dict[str, Any] | None = None,
        fenced_scope: dict[str, Any] | None = None,
        recheck: Callable[[str, dict[str, Any]], str] | None = None,
    ) -> tuple[bool, str]:
        """显式要求一次确认（**不经过权限策略**）：供「围栏来源动作一律先确认」使用。

        与 ``execute`` 的差别只有一处：判定来源不是 ``permission_mode`` 而是调用方。
        ``fenced_scope`` 非空时，批准会顺带授予本次 Run 的围栏动作一次授权
        （见 ``grant_fenced_approval``）。``recheck`` 见 ``_enqueue_confirmation``。
        """
        return self._enqueue_confirmation(
            tool, arguments, active_skills, reason, run_context,
            extra={"fenced_scope": dict(fenced_scope)} if fenced_scope else None,
            recheck=recheck,
        )

    def execute(
        self,
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
        recheck: Callable[[str, dict[str, Any]], str] | None = None,
    ) -> tuple[bool, str]:
        try:
            reason = self._confirmation_reason(tool, arguments, active_skills, run_context)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if reason:
            return self._enqueue_confirmation(
                tool, arguments, active_skills, reason, run_context, recheck=recheck
            )
        return self.execute_unchecked(tool, arguments, active_skills, run_context)

    def execute_unchecked(
        self,
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """受控直调：跳过权限确认，仅限已自行完成授权的调用方（ReadOnly/Craft 包装器、确认后执行）。

        分发：别名归一 → MCP（mcp__ 前缀 / server.tool）→ def.execute，全部经单一通道。
        """
        try:
            tool = self._resolve_tool_name(tool)
            if tool.startswith("mcp__"):
                parts = tool.split("__", 2)
                if len(parts) == 3 and self.mcp_registry.connection(parts[1]) is not None:
                    return self.mcp_registry.call(parts[1], parts[2], arguments)
                return False, f"未知工具：{tool}"
            if "." in tool:
                server_id, mcp_tool = tool.split(".", 1)
                if self.mcp_registry.connection(server_id) is not None:
                    return self.mcp_registry.call(server_id, mcp_tool, arguments)
                return False, f"未知工具：{tool}"
            spec = self._def_resolver(tool) if self._def_resolver is not None else None
            if spec is not None and getattr(spec, "execute", None) is not None:
                return spec.execute(arguments, active_skills, run_context)
            return False, f"未知工具：{tool}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _post_check_denial(pending: dict[str, Any]) -> str:
        """批准时的复核：返回非空即表示「确认通过了，但策略仍不允许」，禁止执行。

        兜底原则与 ``_evaluate_confirmation_reason`` 一致：复核**抛异常也拒绝**
        （fail-closed），不因为包装层出错而放行一个本该被拦的动作。
        """
        check = pending.get("recheck")
        if not callable(check):
            return ""
        try:
            return str(check(pending["tool"], pending.get("arguments") or {}) or "")
        except Exception as exc:
            return f"确认后复核失败（已拒绝执行）：{type(exc).__name__}: {exc}"

    def confirm_execute(self, confirm_id: str, allow_run: bool | None = None) -> tuple[bool, str]:
        """确认并执行待确认的工具调用"""
        with self._confirmation_lock:
            pending = self.pending_confirmation.get(confirm_id)
            if not pending:
                return False, "确认ID无效或已过期"
            if pending.get("processing"):
                return False, "该操作正在执行"
            pending["processing"] = True
            denial = self._post_check_denial(pending)
            if denial:
                result = (False, denial)
                self.pending_confirmation.pop(confirm_id, None)
                self.confirmation_results[confirm_id] = result
                return result
            self._apply_fenced_scope(pending, allow_run)
        result = self.execute_unchecked(
            pending["tool"], pending["arguments"], pending["active_skills"], pending.get("run_context")
        )
        with self._confirmation_lock:
            self.pending_confirmation.pop(confirm_id, None)
            self.confirmation_results[confirm_id] = result
        return result

    def confirm_execute_async(self, confirm_id: str, allow_run: bool | None = None) -> tuple[bool, str]:
        """Approve immediately and execute the potentially long tool off-request."""
        with self._confirmation_lock:
            pending = self.pending_confirmation.get(confirm_id)
            if not pending:
                return False, "确认ID无效或已过期"
            if pending.get("processing"):
                return True, "工具已在执行"
            pending["processing"] = True
            denial = self._post_check_denial(pending)
            if denial:
                # 复核不通过：同样不能执行，但必须把结论写回 confirmation_results——
                # 正在等待的 Run 靠它收尾，否则会一直等到 30 分钟超时。
                result = (False, denial)
                self.pending_confirmation.pop(confirm_id, None)
                self.confirmation_results[confirm_id] = result
                return result
            self._apply_fenced_scope(pending, allow_run)

        def worker() -> None:
            result = self.execute_unchecked(
                pending["tool"], pending["arguments"], pending["active_skills"], pending.get("run_context")
            )
            with self._confirmation_lock:
                self.pending_confirmation.pop(confirm_id, None)
                self.confirmation_results[confirm_id] = result

        threading.Thread(
            target=worker,
            name=f"tool-confirm-{confirm_id[:8]}",
            daemon=True,
        ).start()
        return True, "已确认，工具正在后台执行"

    def _apply_fenced_scope(self, pending: dict[str, Any], allow_run: bool | None) -> None:
        """围栏来源确认被批准时授予本 Run 的一次授权（``allow_run=False`` 可只批这一次）。

        授权绑定当轮的允许工具集合与工作区：换工作区、改工具集合或 Run 结束即失效。
        """
        scope = pending.get("fenced_scope")
        if not isinstance(scope, dict):
            return
        if allow_run is False:
            return
        self.grant_fenced_approval(scope.get("allowed_tools"), scope.get("workspace"))

    def reject_execute(self, confirm_id: str) -> tuple[bool, str]:
        """拒绝待确认的工具调用"""
        with self._confirmation_lock:
            pending = self.pending_confirmation.get(confirm_id)
            if not pending:
                return False, "确认ID无效或已过期"
            if pending.get("processing"):
                return False, "操作已经开始，无法拒绝"
            self.pending_confirmation.pop(confirm_id, None)
            result = (False, f"用户拒绝执行：{pending['tool']}")
            self.confirmation_results[confirm_id] = result
        return result

    def wait_for_confirmation(
        self,
        confirm_id: str,
        timeout: float = 1800,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                with self._confirmation_lock:
                    pending = self.pending_confirmation.get(confirm_id)
                    if pending and not pending.get("processing"):
                        self.pending_confirmation.pop(confirm_id, None)
                    self.confirmation_results.pop(confirm_id, None)
                raise TaskCancelled("任务已取消")
            with self._confirmation_lock:
                result = self.confirmation_results.pop(confirm_id, None)
                pending = confirm_id in self.pending_confirmation
            if result is not None:
                return result
            if not pending:
                return False, "确认请求已失效"
            time.sleep(0.1)
        with self._confirmation_lock:
            pending = self.pending_confirmation.get(confirm_id)
            if pending and not pending.get("processing"):
                self.pending_confirmation.pop(confirm_id, None)
        minutes = max(1, round(timeout / 60))
        return False, f"用户未在{minutes}分钟内确认，已自动拒绝"

    def mcp_tool_guide(self) -> str:
        return self.mcp_registry.tool_guide()
