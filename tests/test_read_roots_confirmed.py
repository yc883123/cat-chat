# -*- coding: utf-8 -*-
"""护栏：会话内「已确认允许」的工作区外目录，读取类工具免逐次确认。

缺陷形态（本文件钉的就是这几条）：
1. 用户拖入工作区外的素材目录、在确认框点过「允许」之后，模型每读一个文件还要再弹一次
   「允许执行」——同一个决定被问了几十遍，目录一大就根本用不了；
2. 反过来，任何"顺手把路径记成可信"的实现都是越权：模型如果能用工具参数把任意路径写进
   免确认集合，等于把整台机器的读权限交出去。

契约（唯一来源 + 一个判定点，见 §九.131）：
- 授权**只有一个来源**：用户在界面上点过「允许」（`app.folder_index(allow_outside=True)`），
  记录挂在会话上（`app._confirmed_folders`）。读取策略只是**读**这份记录，不新增写入通道；
- 只放宽**读**：写策略一个字不改，确认目录内写文件仍按「工作区外」处理；
- 只覆盖**该目录子树**：`..` 逃逸在 resolve 阶段折叠成目录外 ⇒ 仍要确认；
- 不跨会话、不落盘、fail-closed（getter 缺失/会话 id 为空/getter 抛异常 ⇒ 当作没确认过）。

引擎接线与生产等价（def 解析器 + 别名解析器，见 tool_testkit）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.app import NaibaChatApp  # noqa: E402
from naiba.tools.providers.core import ToolContext  # noqa: E402
from naiba.tools.providers.documents import DocumentToolProvider  # noqa: E402
from naiba.tools.providers.video import VideoToolProvider  # noqa: E402

from tool_testkit import assembled_registry, run_context_for, wired_executor  # noqa: E402

# 命中读取策略的 5 个工具（core 域 `_READ_FAMILY` + documents/video 域复用同一策略）
READ_FAMILY = ("read_file", "list_directory", "search_files", "read_pdf", "probe_video")


def _context(workspace: Path, confirmed_read_roots=None) -> ToolContext:
    from naiba.mcp import MCPRegistry

    return ToolContext(
        workspace=workspace,
        python_executable=sys.executable,
        command_timeout=60,
        mcp_registry=MCPRegistry([]),
        mcp_register=None,
        confirmed_read_roots_getter=confirmed_read_roots,
    )


class ConfirmedReadRootsTests(unittest.TestCase):
    """判定层：一个来源（getter）+ 一个判定点（`_make_read_policy`）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_readroots_")
        # resolve()：策略对工作区与目标都 resolve()，夹具不 resolve 的话 CI 的 8.3 短 TEMP
        # 会让「理由里带的路径」与夹具字符串对不上（与 test_folder_index 同因）。
        base = Path(self.tmp.name).resolve()
        self.workspace = base / "ws"
        self.workspace.mkdir()
        (self.workspace / "inside.txt").write_text("INSIDE", encoding="utf-8")

        # 用户在界面上确认过的目录（含子目录）
        self.allowed = base / "allowed"
        (self.allowed / "sub").mkdir(parents=True)
        (self.allowed / "a.txt").write_text("ALLOWED-A", encoding="utf-8")
        (self.allowed / "sub" / "b.txt").write_text("ALLOWED-B", encoding="utf-8")

        # 没确认过的目录，以及「确认目录的上级」
        self.other = base / "other"
        self.other.mkdir()
        (self.other / "c.txt").write_text("OTHER-C", encoding="utf-8")
        (base / "up.txt").write_text("UP", encoding="utf-8")
        self.base = base

        self.addCleanup(self.tmp.cleanup)

    # ---- 夹具 ----

    def _getter(self, conversation_id: str = "conv-a"):
        return lambda cid: [self.allowed] if str(cid) == conversation_id else []

    def _executor(self, mode: str = "confirm", getter=None):
        getter = self._getter() if getter is None else getter
        return wired_executor(self.workspace, mode=mode, confirmed_read_roots=getter)

    def _run(self, executor, tool: str, arguments: dict, conversation_id: str = "conv-a"):
        run_context = run_context_for(
            self.workspace,
            **({} if conversation_id is None else {"conversation_id": conversation_id}),
        )
        return executor.execute(tool, arguments, [], run_context)

    # ---- 1. 基线：没确认过就问 ----

    def test_unconfirmed_outside_still_asks(self) -> None:
        executor = self._executor(getter=lambda cid: [])
        ok, out = self._run(executor, "read_file", {"path": str(self.allowed / "a.txt")})
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        self.assertIn("工作区外", out)

    # ---- 2/3. 已确认：目录内与子目录内都免确认，且真读到内容 ----

    def test_confirmed_dir_reads_without_confirm(self) -> None:
        ok, out = self._run(self._executor(), "read_file", {"path": str(self.allowed / "a.txt")})
        self.assertTrue(ok, out)
        self.assertNotIn("NEED_CONFIRM", out)
        self.assertIn("ALLOWED-A", out)

    def test_confirmed_dir_covers_subtree(self) -> None:
        ok, out = self._run(
            self._executor(), "read_file", {"path": str(self.allowed / "sub" / "b.txt")}
        )
        self.assertTrue(ok, out)
        self.assertIn("ALLOWED-B", out)

    def test_confirmed_dir_listing_without_confirm(self) -> None:
        ok, out = self._run(self._executor(), "list_directory", {"path": str(self.allowed)})
        self.assertTrue(ok, out)
        self.assertIn("a.txt", out)

    def test_confirmed_dir_search_without_confirm(self) -> None:
        ok, out = self._run(
            self._executor(), "search_files", {"path": str(self.allowed), "query": "ALLOWED-A"}
        )
        self.assertTrue(ok, out)
        self.assertIn("a.txt", out)

    # ---- 4. 边界不收成「整个盘」：上级 / 邻居目录仍问 ----

    def test_parent_of_confirmed_dir_still_asks(self) -> None:
        ok, out = self._run(self._executor(), "read_file", {"path": str(self.base / "up.txt")})
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        self.assertIn("工作区外", out)

    def test_unconfirmed_sibling_still_asks(self) -> None:
        ok, out = self._run(self._executor(), "read_file", {"path": str(self.other / "c.txt")})
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)

    def test_dotdot_escape_still_asks(self) -> None:
        escape = str(self.allowed / ".." / "other" / "c.txt")
        ok, out = self._run(self._executor(), "read_file", {"path": escape})
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        self.assertIn("工作区外", out)

    def test_dotdot_escapes_then_returns_inside_still_ok(self) -> None:
        """`..` 折回自己目录内 ⇒ 仍属授权子树（这是 resolve 语义，不是漏洞）。"""
        weird = str(self.allowed / "sub" / ".." / "a.txt")
        ok, out = self._run(self._executor(), "read_file", {"path": weird})
        self.assertTrue(ok, out)
        self.assertIn("ALLOWED-A", out)

    # ---- 6/7. 会话隔离与空会话 ----

    def test_other_conversation_not_inherited(self) -> None:
        ok, out = self._run(
            self._executor(), "read_file", {"path": str(self.allowed / "a.txt")}, "conv-b"
        )
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)

    def test_missing_conversation_id_asks(self) -> None:
        ok, out = self._run(self._executor(), "read_file", {"path": str(self.allowed / "a.txt")}, None)
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)

    def test_blank_conversation_id_asks(self) -> None:
        ok, out = self._run(self._executor(), "read_file", {"path": str(self.allowed / "a.txt")}, "")
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)

    def test_getter_receives_conversation_id(self) -> None:
        seen: list[str] = []

        def getter(cid: str):
            seen.append(cid)
            return []

        self._run(self._executor(getter=getter), "read_file", {"path": str(self.allowed / "a.txt")})
        self.assertEqual(["conv-a"], seen)

    # ---- 8/9. fail-closed：老装配与抛异常 ----

    def test_legacy_context_without_getter_is_byte_identical(self) -> None:
        """不传 getter（老装配）⇒ 与加字段前的行为逐字节一致。"""
        executor = wired_executor(self.workspace, mode="confirm")
        ok, out = self._run(executor, "read_file", {"path": str(self.allowed / "a.txt")})
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        expected = f"读取工作区外路径：{(self.allowed / 'a.txt').resolve()}（当前工作区：{self.workspace}）"
        self.assertIn(expected.replace(":", "："), out)

    def test_tool_context_default_field_is_none(self) -> None:
        self.assertIsNone(_context(self.workspace).confirmed_read_roots_getter)

    def test_getter_exception_is_fail_closed(self) -> None:
        def boom(cid: str):
            raise RuntimeError("授权表读挂了")

        ok, out = self._run(self._executor(getter=boom), "read_file", {"path": str(self.allowed / "a.txt")})
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        self.assertIn("工作区外", out)

    def test_getter_returning_none_is_fail_closed(self) -> None:
        executor = self._executor(getter=lambda cid: None)
        ok, out = self._run(executor, "read_file", {"path": str(self.allowed / "a.txt")})
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)

    def test_garbage_entries_do_not_break_the_policy(self) -> None:
        """坏条目不得拖垮整张策略表：跳过后仍照常判定。"""
        executor = self._executor(getter=lambda cid: [None, "", self.allowed])
        ok, out = self._run(executor, "read_file", {"path": str(self.allowed / "a.txt")})
        self.assertTrue(ok, out)
        self.assertIn("ALLOWED-A", out)

    # ---- 10. 五个工具全覆盖 ----

    def test_all_read_family_tools_share_the_exemption(self) -> None:
        """5 个读取工具拿的必须是**同一个** ToolContext（documents/video 域复用 core 的）。"""
        ctx = _context(self.workspace, self._getter())
        registry = assembled_registry(self.workspace, confirmed_read_roots=self._getter())
        core = {name: registry.get(name) for name in ("read_file", "list_directory", "search_files")}
        extra = {
            spec.name: spec
            for provider in (
                DocumentToolProvider(ctx, lambda: self.base / "cache"),
                VideoToolProvider(ctx, lambda: self.base / "cache"),
            )
            for spec in provider.tools()
        }
        run_context = run_context_for(self.workspace, conversation_id="conv-a")
        for name in READ_FAMILY:
            spec = core.get(name) or extra.get(name)
            self.assertIsNotNone(spec, name)
            reason = spec.policy(name, {"path": str(self.allowed / "a.txt")}, [], "confirm", run_context, self.workspace)
            self.assertEqual("", reason, f"{name} 在已确认目录内应免确认，实际：{reason}")

    def test_all_read_family_tools_still_ask_when_unconfirmed(self) -> None:
        """负向对照：同一个循环在未确认时必须全部要确认（否则上面那条是空断言）。"""
        ctx = _context(self.workspace, lambda cid: [])
        registry = assembled_registry(self.workspace, confirmed_read_roots=lambda cid: [])
        core = {name: registry.get(name) for name in ("read_file", "list_directory", "search_files")}
        extra = {
            spec.name: spec
            for provider in (
                DocumentToolProvider(ctx, lambda: self.base / "cache"),
                VideoToolProvider(ctx, lambda: self.base / "cache"),
            )
            for spec in provider.tools()
        }
        run_context = run_context_for(self.workspace, conversation_id="conv-a")
        for name in READ_FAMILY:
            spec = core.get(name) or extra.get(name)
            reason = spec.policy(name, {"path": str(self.allowed / "a.txt")}, [], "confirm", run_context, self.workspace)
            self.assertIn("工作区外", reason, f"{name} 未确认时必须确认，实际：{reason}")

    # ---- 11. 只放宽读：写策略一个字不改 ----

    def test_write_in_confirmed_dir_still_asks_in_confirm_mode(self) -> None:
        ok, out = self._run(
            self._executor("confirm"),
            "write_file",
            {"path": str(self.allowed / "new.txt"), "content": "x"},
        )
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        self.assertIn("写入工作区外", out)

    def test_write_in_confirmed_dir_still_asks_in_auto_mode(self) -> None:
        ok, out = self._run(
            self._executor("auto"),
            "write_file",
            {"path": str(self.allowed / "new.txt"), "content": "x"},
        )
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)

    def test_write_outside_is_rejected_in_deny_mode(self) -> None:
        ok, out = self._run(
            self._executor("deny"),
            "write_file",
            {"path": str(self.allowed / "new.txt"), "content": "x"},
        )
        self.assertFalse(ok)
        self.assertIn("权限被拒绝", out)

    # ---- 12. 相对路径不被污染 ----

    def test_relative_path_not_resolved_into_confirmed_dir(self) -> None:
        """工作区里没有 a.txt、确认目录里有：相对路径**不得**被解析到确认目录。

        相对路径的解析范围刻意保持原样（工作区 → active Skill 根）：把确认目录也塞进去，
        `read_file("a.txt")` 会悄悄读到工作区外的同名文件，行为变得不可预测。
        模型访问这些目录用索引里给的**绝对路径**（这正是现在的口径）。
        """
        self.assertFalse((self.workspace / "a.txt").exists())
        ok, out = self._run(self._executor(), "read_file", {"path": "a.txt"})
        self.assertFalse(ok, f"相对路径不该有结果：{out}")
        self.assertNotIn("ALLOWED-A", out)
        # 落点仍是工作区内（找不到文件），而不是被改写成确认目录里的同名文件。
        # 报错文本是异常 repr，反斜杠被转义过，先归一再看。
        flat = out.replace("\\\\", "\\")
        self.assertIn(str(self.workspace / "a.txt"), flat)
        self.assertNotIn(str(self.allowed / "a.txt"), flat)

    def test_relative_path_inside_workspace_unaffected(self) -> None:
        ok, out = self._run(self._executor(), "read_file", {"path": "inside.txt"})
        self.assertTrue(ok, out)
        self.assertIn("INSIDE", out)

    # ---- 14. 大小写 / 尾斜杠 / 反斜杠：同一授权 ----

    def test_case_and_separator_variants_are_same_grant(self) -> None:
        variants = [
            str(self.allowed).replace("\\", "/"),
            str(self.allowed) + "/",
            str(self.allowed).upper() + "\\",
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                executor = self._executor(getter=lambda cid, v=variant: [v])
                ok, out = self._run(executor, "read_file", {"path": str(self.allowed / "a.txt")})
                self.assertTrue(ok, f"{variant} 未被当作同一授权：{out}")


class _AppGate:
    """只借 `NaibaChatApp` 上的文件夹闸门逻辑，不构造真 App（避免拉起 MCP / 后台任务）。

    这里刻意用**未绑定方法**而不是复制一份逻辑：测的就是线上那份实现。
    """

    folder_index = NaibaChatApp.folder_index
    # 线上是 staticmethod：取出来已是普通函数，这里必须重新包一层才不会被当成实例方法
    _folder_key = staticmethod(NaibaChatApp._folder_key)
    _confirmed_folder_paths = NaibaChatApp._confirmed_folder_paths
    confirmed_read_roots = NaibaChatApp.confirmed_read_roots

    def __init__(self, workspace: Path, conversations: dict | None = None) -> None:
        self.config = SimpleNamespace(resolve_workspace_dir=lambda raw=None: Path(workspace))
        self.storage = SimpleNamespace(get_conversation=lambda cid: (conversations or {}).get(cid))
        self._confirmed_folders: dict[str, set[str]] = {}


class ConfirmedReadRootsAppTests(unittest.TestCase):
    """app 层：确认记录 → 免确认根，且与 `folder_index` 同源。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_readroots_app_")
        base = Path(self.tmp.name).resolve()
        self.workspace = base / "ws"
        self.workspace.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "q.txt").write_text("OUTSIDE", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)
        self.app = _AppGate(
            self.workspace, {"conv-a": {"id": "conv-a"}, "conv-b": {"id": "conv-b"}}
        )

    def test_no_confirmation_yet_returns_empty(self) -> None:
        self.assertEqual([], self.app.confirmed_read_roots("conv-a"))

    def test_blank_conversation_id_returns_empty(self) -> None:
        self.assertEqual([], self.app.confirmed_read_roots(""))
        self.assertEqual([], self.app.confirmed_read_roots(None))  # type: ignore[arg-type]

    def test_allow_outside_records_root(self) -> None:
        self.app.folder_index(str(self.outside), "conv-a", allow_outside=True)
        roots = self.app.confirmed_read_roots("conv-a")
        self.assertEqual(1, len(roots))
        self.assertEqual(self.outside, Path(roots[0]).resolve())
        # 另一个会话与空会话都拿不到
        self.assertEqual([], self.app.confirmed_read_roots("conv-b"))
        self.assertEqual([], self.app.confirmed_read_roots(""))

    def test_inside_workspace_records_nothing(self) -> None:
        (self.workspace / "in").mkdir()
        self.app.folder_index(str(self.workspace / "in"), "conv-a")
        self.assertEqual([], self.app.confirmed_read_roots("conv-a"))

    def test_app_grant_reaches_read_policy_end_to_end(self) -> None:
        """真接线：确认记录里的**小写归一键**必须能被读取策略认出来（同一事实两处不漂移）。"""
        self.app.folder_index(str(self.outside), "conv-a", allow_outside=True)
        executor = wired_executor(
            self.workspace,
            mode="confirm",
            confirmed_read_roots=self.app.confirmed_read_roots,
        )
        run_context = run_context_for(self.workspace, conversation_id="conv-a")
        ok, out = executor.execute("read_file", {"path": str(self.outside / "q.txt")}, [], run_context)
        self.assertTrue(ok, out)
        self.assertIn("OUTSIDE", out)
        # 换会话（同一个 getter）→ 照旧要确认
        other = run_context_for(self.workspace, conversation_id="conv-b")
        ok2, out2 = executor.execute("read_file", {"path": str(self.outside / "q.txt")}, [], other)
        self.assertFalse(ok2)
        self.assertTrue(out2.startswith("NEED_CONFIRM:"), out2)


class RealAppAssemblyTests(unittest.TestCase):
    """装配级护栏：`app.py` 那一行**真的接了吗**。

    只测 getter 逻辑是抓不到「忘了在装配处接线」的——那种情况下 `ToolContext` 的字段是
    `None`，策略退化成"没确认过"，控制器用例照样全绿，而产品里用户点十次「允许」也没用。
    所以这里构造**真 App**，用真注册表里 `read_file` 的那张 spec 打一次判定。
    """

    def setUp(self) -> None:
        from naiba.paths import PathContext

        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_readroots_asm_")
        root = Path(self.tmp.name).resolve()
        (root / "workspace").mkdir()
        # 工作区外（默认工作区 = exe_dir/workspace，见 config.resolve_workspace_dir）
        self.outside = root / "outside"
        self.outside.mkdir()
        (self.outside / "q.txt").write_text("OUTSIDE", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)
        self.app = NaibaChatApp(paths=PathContext.local(root, root / "config.json"))
        self.workspace = self.app.config.resolve_workspace_dir()

    def test_real_assembly_exempts_confirmed_dir(self) -> None:
        spec = self.app.tool_registry.get("read_file")
        self.assertIsNotNone(spec)
        cid = self.app.storage.create_conversation(title="t")["id"]
        run_context = run_context_for(self.workspace, conversation_id=cid)
        args = {"path": str(self.outside / "q.txt")}

        before = spec.policy("read_file", args, [], "confirm", run_context, self.workspace)
        self.assertIn("工作区外", before)

        self.app.folder_index(str(self.outside), cid, allow_outside=True)
        after = spec.policy("read_file", args, [], "confirm", run_context, self.workspace)
        self.assertEqual("", after, "app 装配处的 confirmed_read_roots_getter 没接上")

        # 同一装配下换会话仍然问（授权按会话隔离，不是全局开关）
        other = self.app.storage.create_conversation(title="t2")["id"]
        run_context_other = run_context_for(self.workspace, conversation_id=other)
        again = spec.policy("read_file", args, [], "confirm", run_context_other, self.workspace)
        self.assertIn("工作区外", again)

    def test_real_assembly_reads_through_executor(self) -> None:
        """再走一遍真引擎：确认之后模型侧真的能读到内容（判定与执行同一根）。"""
        cid = self.app.storage.create_conversation(title="t")["id"]
        self.app.folder_index(str(self.outside), cid, allow_outside=True)
        executor = self.app.executor.clone_for_permission("confirm")
        executor.set_def_resolver(self.app.tool_registry.get)
        executor.set_alias_resolver(self.app.tool_registry.resolve)
        run_context = run_context_for(self.workspace, conversation_id=cid)
        ok, out = executor.execute("read_file", {"path": str(self.outside / "q.txt")}, [], run_context)
        self.assertTrue(ok, out)
        self.assertIn("OUTSIDE", out)


class ConfirmedReadRootsErrorTests(unittest.TestCase):
    """异常型用例单独放：`folder_index` 未确认时抛 `FolderConfirmRequired`。"""

    def setUp(self) -> None:
        from naiba.core.conv_files import FolderConfirmRequired

        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_readroots_err_")
        base = Path(self.tmp.name).resolve()
        self.workspace = base / "ws"
        self.workspace.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        self.addCleanup(self.tmp.cleanup)
        self.app = _AppGate(self.workspace, {"conv-a": {"id": "conv-a"}})
        self.exc_type = FolderConfirmRequired

    def test_unconfirmed_outside_raises_and_records_nothing(self) -> None:
        with self.assertRaises(self.exc_type):
            self.app.folder_index(str(self.outside), "conv-a")
        self.assertEqual({}, self.app._confirmed_folders)
        self.assertEqual([], self.app.confirmed_read_roots("conv-a"))


if __name__ == "__main__":
    unittest.main()
