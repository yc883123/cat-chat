# -*- coding: utf-8 -*-
"""守门：任务面板只列**后台作业**，不把「每一次回答」当成任务。

``background_tasks`` 是 run 与 job 共用的一张表：``chat`` / ``plan_execute`` 那些行是
「用户每发一句话就插入一次的回答记录」，其余 kind（``shell`` / ``check`` / ``http_poll`` /
``comfyui`` / ``subagent``）才是真正的后台作业。旧面板把两类行平铺混排，于是用户看到的
是"我发过的每一句话都是一个任务"，也因此看不出谁触发了谁。

本文件钉死三件事：
1. ``/api/tasks?jobs_only=1`` 在 SQL 层排除回答记录，并把窗口放到 200
   （默认 limit 是 50，而回答记录占绝大多数，先捞再筛会把较早的作业挤出窗口）；
2. 不带该参数时行为与从前完全一致（向后兼容）；
3. 前端承诺：状态词表含 ``stopping``、面板只渲染作业、组标题由「类型 + 时间」拼成
   而不引用用户消息原文。

全量验证口径见 `项目维护说明（修改代码前必读）.md` §六。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.storage.store import ChatStorage, PRIMARY_RUN_KINDS  # noqa: E402

AGENT = {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []}


def _read(name: str) -> str:
    return (ROOT / "public" / "js" / name).read_text(encoding="utf-8")


def _function_body(source: str, signature: str) -> str:
    """取函数体（到下一个顶层 function/export function 之前）。"""
    import re

    start = source.index(signature)
    rest = source[start:]
    match = re.search(r"\n(?:export )?function ", rest[1:])
    return rest if match is None else rest[: match.start() + 1]


def _css_rule(source: str, selector: str) -> str:
    """取 CSS 规则体（``选择器 { ... }``），用于钉死「这条规则存在且含某个声明」。"""
    start = source.index(selector + " {")
    end = source.index("}", start)
    return source[start:end]


class TasksJobsOnlyFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _reply(self) -> str:
        """一次回答记录（顶层 run）。建完立即置终态，避免占住会话的 ACTIVE_RUN 位。"""
        run = self.storage.create_run(self.conversation["id"], "回答", AGENT, {}, kind="chat")
        self.storage.update_background_task(run["id"], status="completed", finished=True)
        return str(run["id"])

    def _job(self, kind: str = "comfyui", parent: str = "parent-1") -> str:
        run = self.storage.create_run(
            self.conversation["id"], "作业", AGENT, {}, kind=kind, parent_job_id=parent
        )
        self.storage.update_background_task(run["id"], status="failed", finished=True)
        return str(run["id"])

    def test_exclude_kinds_drops_reply_rows_only(self) -> None:
        reply = self._reply()
        job = self._job(parent=reply)
        only_jobs = self.storage.list_background_tasks(exclude_kinds=PRIMARY_RUN_KINDS)
        self.assertEqual([row["id"] for row in only_jobs], [job])
        # 不带过滤时行为不变：旧调用方（如清空已结束任务、启动清理）照旧看到全部记录
        self.assertEqual(
            {row["id"] for row in self.storage.list_background_tasks()}, {reply, job}
        )

    def test_jobs_only_window_reaches_two_hundred(self) -> None:
        for _ in range(60):
            self._job()
        self.assertEqual(len(self.storage.list_background_tasks(limit=50)), 50)
        self.assertEqual(
            len(self.storage.list_background_tasks(limit=200, exclude_kinds=PRIMARY_RUN_KINDS)),
            60,
            "面板要一次拿到全部作业，窗口必须能到 200",
        )

    def test_primary_run_kinds_is_the_single_definition(self) -> None:
        self.assertEqual(set(PRIMARY_RUN_KINDS), {"chat", "plan_execute"})
        from naiba.run.manager import PRIMARY_RUN_KINDS as manager_kinds

        self.assertEqual(set(manager_kinds), set(PRIMARY_RUN_KINDS), "两侧必须是同一份定义")


class TasksRouteContractTests(unittest.TestCase):
    """路由契约（源码级）：参数解析、窗口上限、重复分支已合并。"""

    def setUp(self) -> None:
        self.http = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")

    def test_route_parses_jobs_only(self) -> None:
        self.assertIn('query.get("jobs_only"', self.http)
        self.assertIn("exclude_kinds=_PRIMARY_RUN_KINDS if jobs_only else None", self.http)
        self.assertIn("limit=200 if jobs_only else 50", self.http)

    def test_job_cancel_branch_is_not_duplicated(self) -> None:
        marker = 'path.startswith("/api/jobs/") and path.endswith("/cancel")'
        self.assertEqual(
            self.http.count(marker), 1,
            "重复的 /api/jobs/{id}/cancel 分支返回体不一致，只保留一条",
        )


class TaskPanelFrontendTests(unittest.TestCase):
    """前端承诺：状态词表、只渲染作业、组标题不引用用户消息原文。"""

    def setUp(self) -> None:
        self.tasks_js = _read("06-tasks-plans.js")
        self.conversations_js = _read("08-conversations.js")

    def test_status_vocabulary_covers_stopping(self) -> None:
        self.assertIn("'stopping'", self.tasks_js, "activeTaskStatuses 必须认 stopping")
        self.assertIn("stopping: '停止中'", self.tasks_js)
        self.assertIn("waiting: '等待中'", self.tasks_js)

    def test_panel_requests_jobs_only_and_filters_again(self) -> None:
        self.assertIn("/api/tasks?jobs_only=1", self.tasks_js)
        body = _function_body(self.conversations_js, "export function renderRunTasks(")
        self.assertIn("['chat', 'plan_execute']", body, "前端再兜一层，防旧缓存混进回答记录")

    def test_badge_falls_back_to_total_when_nothing_is_active(self) -> None:
        body = _function_body(self.conversations_js, "export function renderRunTasks(")
        self.assertIn("is-idle", body, "没有活动任务时要显示总数并弱化，而不是归零")

    def test_group_title_uses_kind_and_time_not_user_message(self) -> None:
        body = _function_body(self.conversations_js, "export function taskGroupTitle(")
        self.assertIn("taskKindLabel(", body)
        self.assertIn("formatTaskTime(", body)
        self.assertNotIn("task.message", body, "组标题不得引用用户消息原文")
        self.assertNotIn("agent_name", body)

    def test_row_stop_button_only_for_active_jobs(self) -> None:
        body = _function_body(self.conversations_js, "function taskRowMarkup(")
        self.assertIn("activeTaskStatuses.has(task.status)", body)
        self.assertIn("data-task-cancel", body)

    def test_sync_failure_is_surfaced_with_last_success_time(self) -> None:
        self.assertIn("taskSyncFailed", self.tasks_js)
        body = _function_body(self.conversations_js, "function renderTaskSummary(")
        self.assertIn("最后成功更新", body)

    def test_derived_chain_groups_by_visible_chain_anchor(self) -> None:
        """派生链必须按「可见父链锚点」分组，而不是按单层 parent_job_id。

        事故：chat → 子 Agent → ComfyUI 两层链路里 chat 行被 jobs_only 过滤，
        单层分组会把子 Agent 与它派生的 ComfyUI 拆成两组（用户看到「一个子 Agent
        又变成两个任务」）；但也不能简单地"上溯到最顶层可见行"——那样同一父下
        的兄弟作业（父不在面板里）会各自成组，破坏既有「×N」同批口径。
        """
        body = _function_body(self.conversations_js, "export function renderRunTasks(")
        self.assertIn("const key = rootKeyOf(task);", body)
        self.assertIn("byId.get(", body, "锚点要沿可见父链上溯，必须查得到父行")
        self.assertIn("seen.has(next)", body, "环状父子链必须能收敛（脏数据不得卡死面板）")
        self.assertNotIn(
            "const key = String(task.parent_job_id || '');", body,
            "单层 parent_job_id 分组是本次要修的缺陷，不得回退",
        )

    def test_child_rows_are_marked_nested(self) -> None:
        """子任务行必须有从属标记（缩进 + 连接线），且只有父行在面板里时才加。"""
        row = _function_body(self.conversations_js, "function taskRowMarkup(")
        self.assertIn("nested = false", row, "taskRowMarkup 要显式接嵌套标记（默认不嵌套）")
        self.assertIn(" task-item-nested", row)
        body = _function_body(self.conversations_js, "export function renderRunTasks(")
        self.assertIn("byId.has(String(task.parent_job_id", body, "父行也在面板里才缩进")
        styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".task-item-nested", styles)
        self.assertIn("margin-left", _css_rule(styles, ".task-item-nested"), "子行要缩进")


class TaskPanelResumeDisplayTests(unittest.TestCase):
    """重启恢复在面板上的显示口径（2026-09-20 事故）。

    一次服务重启把 ComfyUI 批量生成中断，重启后自动接续跑完，面板里于是并排出现两条
    共用父回答的行。用户看到的三个毛病：

    1. 汇总行 ``共 3 · 运行中 0 · 失败 0 · 已完成 2`` 里凭空少一条——``interrupted``
       既不算「失败」也不算「已完成」，从汇总行完全读不出「发生过一次中断」；
    2. 恢复出的新 Job 标题显示成 ``完成 8/10``——旧 ``resume()`` 把 ``current_step``
       当 label 写进了 ``message``（写入口已在 ``naiba/jobs.py`` 修掉），显示层再兜一层；
    3. 中断源与接续它的新 Job 是两条对立状态的行，不点明「这条已经有人接手」读不懂。

    渲染结果由 `verify/tasks_panel_render_check.mjs` 真执行校验；这里钉源码口径。
    """

    def setUp(self) -> None:
        self.tasks_js = _read("06-tasks-plans.js")
        self.conversations_js = _read("08-conversations.js")

    def test_summary_has_a_bucket_for_every_terminal_status(self) -> None:
        body = _function_body(self.conversations_js, "function renderTaskSummary(")
        self.assertIn("'interrupted'", body, "中断是终态，汇总行必须给它一个桶，否则「共 N」对不上")
        self.assertIn("'cancelled'", body, "取消同理")
        self.assertIn("最后成功更新", body, "改汇总口径不得丢掉同步失败兜底")

    def test_progress_shaped_message_is_not_used_as_title(self) -> None:
        body = _function_body(self.tasks_js, "export function taskDisplayTitle(")
        self.assertIn("TASK_PROGRESS_MESSAGE", body, "进度形文案不得顶在任务名位")
        # 常量本身必须真的认「数字/数字」——只引用一个名字不算修好
        declaration = self.tasks_js.split("const TASK_PROGRESS_MESSAGE", 1)[1].split("\n", 1)[0]
        self.assertIn(r"\d+\s*\/\s*\d+", declaration, "「完成 8/10」这类进度必须命中")

    def test_superseded_interrupted_row_explains_it_was_replaced(self) -> None:
        row = _function_body(self.conversations_js, "function taskRowMarkup(")
        self.assertIn("result?.resumed_into", row, "标记来自 resume 链路写进 result 的键")
        self.assertIn("已由新任务接续", row)


if __name__ == "__main__":
    unittest.main()
