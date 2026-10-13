"""对拍：core 域 Provider（Phase 5 单轨）——def 一致性 + 引擎端到端路径。

Phase 3 双轨期的新旧实现对拍已完成使命（旧 _tool_* 方法已删），单轨后验证：
- Provider def 与声明表逐字段一致（仅绑定 execute/policy）；
- core 全部 def 携带 def 级 policy；
- 经引擎（wired_executor）执行 read/write/list/glob/search/edit 的端到端结果正确。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from naiba.mcp import MCPRegistry
from naiba.tools.providers import core as core_provider
from naiba.tools.registry import ToolRegistry, build_core_tool_specs, build_harness_alias_specs
from tests.tool_testkit import assembled_registry, wired_executor

ROOT = Path(__file__).resolve().parent.parent


def _make_ctx(workspace: Path) -> core_provider.ToolContext:
    mcp = MCPRegistry([])
    return core_provider.ToolContext(
        workspace=workspace,
        python_executable=sys.executable,
        command_timeout=60,
        mcp_registry=mcp,
        mcp_register=None,
    )


class CoreToolsParityTests(unittest.TestCase):
    """Provider def 一致性 + 引擎端到端行为。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-parity-"))
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.ctx = _make_ctx(self.workspace)
        self.provider = core_provider.CoreToolProvider(self.ctx)
        self.specs = {spec.name: spec for spec in self.provider.tools()}
        self.executor = wired_executor(self.workspace, mode="auto")
        self.reg = assembled_registry(self.workspace)

    def test_provider_covers_exactly_core_declarations(self) -> None:
        declared = {spec.name for spec in build_core_tool_specs()} | {
            spec.name for spec in build_harness_alias_specs()
        }
        self.assertEqual(declared, set(self.specs), "Provider 未覆盖/多出 core/别名声明")

    def test_provider_specs_match_declarations(self) -> None:
        """Provider def 与声明表逐字段一致（仅绑定 execute/policy）。"""
        by_name = {
            spec.name: spec
            for spec in (*build_core_tool_specs(), *build_harness_alias_specs())
        }
        for name, new_spec in self.specs.items():
            with self.subTest(tool=name):
                old_spec = by_name[name]
                self.assertEqual(new_spec.description, old_spec.description)
                self.assertEqual(new_spec.parameters, old_spec.parameters)
                self.assertEqual(new_spec.side_effect, old_spec.side_effect)
                self.assertEqual(new_spec.retryable, old_spec.retryable)
                self.assertEqual(new_spec.timeout, old_spec.timeout)
                self.assertEqual(new_spec.permission, old_spec.permission)
                self.assertEqual(new_spec.annotations, old_spec.annotations)
                self.assertFalse(new_spec.system, f"{name}: core 工具不应标记 system")
                self.assertIsNotNone(new_spec.execute, f"{name}: Provider 未绑定 execute")
                self.assertIsNotNone(new_spec.policy, f"{name}: Provider 未绑定 policy")

    def test_engine_executes_read_and_edit(self) -> None:
        target = self.workspace / "sample.txt"
        target.write_text("AAA\nBBB\nAAA\n", encoding="utf-8")
        ok, out = self.executor.execute("read_file", {"path": str(target)}, [])
        self.assertTrue(ok)
        self.assertIn("AAA", out)
        ok, out = self.executor.execute(
            "edit_file", {"path": str(target), "old_text": "AAA", "new_text": "ZZZ", "all": True}, []
        )
        self.assertTrue(ok)
        self.assertIn("ZZZ", target.read_text(encoding="utf-8"))

    def test_read_file_empty_path_returns_guidance(self) -> None:
        """空/空白 path 必须返回纠错文案（kimi-k3 空参死循环事故的守门）。

        旧行为把空值 resolve 成工作区根本身再抛 PermissionError，对模型零指导价值，
        实测连发 6 个 run 后触发「连续失败且重复」熔断。
        """
        for bad in ({}, {"path": ""}, {"path": "   "}):
            with self.subTest(args=bad):
                ok, out = self.executor.execute("read_file", bad, [])
                self.assertTrue(ok)
                self.assertIn("path", out)
                self.assertIn("list_directory", out)
                self.assertNotIn("PermissionError", out)

    def test_read_file_directory_path_returns_guidance(self) -> None:
        """目录 path 必须返回「请用 list_directory」文案，不得抛 PermissionError。"""
        sub = self.workspace / "sub"
        sub.mkdir()
        ok, out = self.executor.execute("read_file", {"path": str(sub)}, [])
        self.assertTrue(ok)
        self.assertIn("list_directory", out)
        self.assertNotIn("PermissionError", out)

    def test_read_file_file_path_output_unchanged(self) -> None:
        """成功路径字节不变（前缀缓存命中不变的前提）：带 path 读文件原样返回内容。"""
        target = self.workspace / "sample.txt"
        target.write_text("AAA\nBBB\n", encoding="utf-8")
        ok, out = self.executor.execute("read_file", {"path": str(target)}, [])
        self.assertTrue(ok)
        self.assertEqual(out, "AAA\nBBB\n")
        # def 直调与引擎分发同源（单一执行通道）。
        ok2, out2 = self.specs["read_file"].execute({"path": str(target)}, [], None)
        self.assertTrue(ok2)
        self.assertEqual(out2, out)

    def test_engine_search_and_glob(self) -> None:
        (self.workspace / "codes.txt").write_text("alpha\nBETA\n", encoding="utf-8")
        ok, out = self.executor.execute(
            "search_files", {"path": str(self.workspace), "query": "beta", "ignore_case": True}, []
        )
        self.assertTrue(ok)
        self.assertIn("codes.txt", out)
        # glob_files 已并入 list_directory（pattern/files_only 等价语义）。
        ok, out = self.executor.execute(
            "list_directory",
            {"path": str(self.workspace), "pattern": "*.txt", "files_only": True},
            [],
        )
        self.assertTrue(ok)
        self.assertIn("codes.txt", out)
        # 退役名调用必须返回引导而非“未知工具”。
        ok, out = self.reg.execute("glob_files", {}, [])
        self.assertFalse(ok)
        self.assertIn("list_directory", out)

    def test_registry_execute_engine_wired_produces_same_as_def(self) -> None:
        """组装态 registry + 引擎分发与 def.execute 直调结果一致（单一执行通道）。"""
        target = self.workspace / "direct.txt"
        target.write_text("direct path", encoding="utf-8")
        ok, via_engine = self.executor.execute("read_file", {"path": str(target)}, [])
        ok2, via_def = self.specs["read_file"].execute({"path": str(target)}, [], None)
        self.assertTrue(ok)
        self.assertTrue(ok2)
        self.assertEqual(via_engine, via_def)
        ok3, via_reg = self.reg.execute("read_file", {"path": str(target)}, [])
        self.assertTrue(ok3)
        self.assertEqual(via_reg, via_def)


if __name__ == "__main__":
    unittest.main()
