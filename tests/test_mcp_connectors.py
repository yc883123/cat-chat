# -*- coding: utf-8 -*-
"""MCP 连接器守门（计划：docs/plans/2026-10-18-MCP连接器分页改造计划.md）。

钉住四件事：

1. ``state()`` 回显配置（``command`` / ``args`` / ``env_keys`` / ``start_timeout_ms``），
   但**不含 env 值**——env 里常放 ``GITHUB_TOKEN`` 一类令牌，列表只读 KEY；
2. ``start_timeout_ms``：缺省 20000（= 引入该字段前的硬编码 20 秒，旧配置零行为变化）、
   写入侧严格校验（越界/非整数 ValueError）、运行期只夹取不抛错；
3. 两个超时互不干涉：``MCP_TOOL_TIMEOUT`` / ``call_timeout_seconds`` 管**单次工具调用**，
   ``start_timeout_ms`` 管**连接建立**；
4. 前端结构：设置里有独立的「MCP 连接器」分页、旧的「局域网访问」页不再含 MCP 元素、
   参数切分与 JSON 解析是导出函数并被接线（真实行为在 ``verify/mcp_connectors_smoke.cjs``）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.core.contracts import (  # noqa: E402
    MCP_START_TIMEOUT_MS_DEFAULT,
    MCP_START_TIMEOUT_MS_MAX,
    MCP_START_TIMEOUT_MS_MIN,
)
from naiba.mcp import (  # noqa: E402
    MCPServerConnection,
    MCPRegistry,
    clamp_start_timeout_ms,
)
from server import ConfigStore  # noqa: E402


def _connection(**overrides):
    params = {
        "server_id": "comfy-mcp",
        "command": "npx",
        "args": ["-y", "@comfyorg/comfy-mcp"],
        "env": {"COMFY_BIN": "D:/ComfyUI", "GITHUB_TOKEN": "ghp_secret"},
    }
    params.update(overrides)
    return MCPServerConnection(**params)


class McpStateEchoTests(unittest.TestCase):
    """列表与编辑表单的数据来源：配置回显，但令牌只出 KEY。"""

    def test_state_echoes_config(self) -> None:
        state = _connection(start_timeout_ms=45000).state()
        self.assertEqual(state["command"], "npx")
        self.assertEqual(state["args"], ["-y", "@comfyorg/comfy-mcp"])
        self.assertEqual(state["env_keys"], ["COMFY_BIN", "GITHUB_TOKEN"])
        self.assertEqual(state["start_timeout_ms"], 45000)

    def test_state_never_leaks_env_values(self) -> None:
        state = _connection().state()
        self.assertNotIn("env", state, "state() 不许回显 env 本体（令牌泄漏）")
        dumped = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("ghp_secret", dumped)
        self.assertNotIn("D:/ComfyUI", dumped)

    def test_lightweight_status_stays_cheap(self) -> None:
        """轮询端点不带配置：``pollMcpStatus`` 只覆盖状态字段，回显值不会被擦掉。"""
        registry = MCPRegistry([
            {"id": "comfy-mcp", "command": "npx", "args": [], "env": {}, "start_timeout_ms": 30000},
        ])
        light = registry.lightweight_status()[0]
        for key in ("command", "args", "env_keys", "start_timeout_ms", "tools"):
            self.assertNotIn(key, light)
        full = registry.states()[0]
        self.assertEqual(full["start_timeout_ms"], 30000)


class StartTimeoutTests(unittest.TestCase):
    """``start_timeout_ms``：缺省不变、越界夹取、写入侧严格。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ConfigStore(self.root / "config.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_is_twenty_seconds(self) -> None:
        connection = _connection()
        self.assertEqual(connection.start_timeout_ms, 20000)
        self.assertEqual(connection.start_timeout_seconds, 20.0)
        self.assertEqual(MCP_START_TIMEOUT_MS_DEFAULT, 20000)

    def test_connection_clamps_out_of_range(self) -> None:
        self.assertEqual(clamp_start_timeout_ms(1), MCP_START_TIMEOUT_MS_MIN)
        self.assertEqual(clamp_start_timeout_ms(10 ** 9), MCP_START_TIMEOUT_MS_MAX)
        self.assertEqual(clamp_start_timeout_ms("abc"), MCP_START_TIMEOUT_MS_DEFAULT)
        self.assertEqual(clamp_start_timeout_ms(None), MCP_START_TIMEOUT_MS_DEFAULT)
        # 运行期不抛错：脏配置也不该让服务连不上。
        self.assertEqual(_connection(start_timeout_ms=1).start_timeout_seconds, 5.0)
        self.assertEqual(_connection(start_timeout_ms=10 ** 9).start_timeout_ms, MCP_START_TIMEOUT_MS_MAX)

    def test_config_persists_and_validates(self) -> None:
        saved = self.store.upsert_mcp_server({
            "id": "rea", "command": "npx", "args": [], "start_timeout_ms": 60000,
        })
        self.assertEqual(saved["start_timeout_ms"], 60000)
        self.assertEqual(MCPRegistry([saved]).states()[0]["start_timeout_ms"], 60000)

    def test_config_rejects_invalid_timeout(self) -> None:
        for bad in (4000, 600001, "abc", 1.5, True, [60000]):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.store.upsert_mcp_server({
                        "id": "bad", "command": "npx", "args": [], "start_timeout_ms": bad,
                    })

    def test_missing_timeout_falls_back_to_default(self) -> None:
        """旧配置（没有这个字段）行为零变化：写入补 20000，读取也按 20000 走。"""
        saved = self.store.upsert_mcp_server({"id": "legacy", "command": "npx", "args": []})
        self.assertEqual(saved["start_timeout_ms"], MCP_START_TIMEOUT_MS_DEFAULT)
        self.assertEqual(MCPRegistry([{"id": "old", "command": "npx"}]).states()[0]["start_timeout_ms"], 20000)

    def test_call_timeout_is_independent(self) -> None:
        """两个超时不许合并：MCP_TOOL_TIMEOUT 管单次调用，start_timeout_ms 管建连。"""
        connection = _connection(env={"MCP_TOOL_TIMEOUT": "900"}, start_timeout_ms=5000)
        self.assertEqual(connection.call_timeout_seconds, 900)
        self.assertEqual(connection.start_timeout_seconds, 5.0)
        # 只改启动超时不影响调用超时，反之亦然。
        self.assertEqual(_connection(start_timeout_ms=120000).call_timeout_seconds, 620)


class ConfigUpsertTests(unittest.TestCase):
    """``upsert_mcp_server``：同 id 覆盖、id 校验、env 的「不传即保留」例外。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConfigStore(Path(self.tmp.name) / "config.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_id_overwrites(self) -> None:
        self.store.upsert_mcp_server({"id": "x1", "command": "a", "args": ["1"]})
        self.store.upsert_mcp_server({"id": "x1", "command": "b", "args": []})
        servers = [item for item in self.store.data["mcp_servers"] if item["id"] == "x1"]
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["command"], "b")

    def test_invalid_id_rejected(self) -> None:
        for bad in ("有 空格", "中文", "", "a" * 65):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.store.upsert_mcp_server({"id": bad, "command": "npx"})

    def test_env_kept_when_omitted_on_edit(self) -> None:
        """编辑表单拿不到令牌原文（列表只回显 KEY），所以「不传 env」必须保留原值。"""
        self.store.upsert_mcp_server({"id": "x1", "command": "a", "env": {"TOKEN": "t"}})
        saved = self.store.upsert_mcp_server({"id": "x1", "command": "b"})
        self.assertEqual(saved["env"], {"TOKEN": "t"})
        # 显式传 env（哪怕是空对象）仍然是整体替换。
        replaced = self.store.upsert_mcp_server({"id": "x1", "command": "c", "env": {}})
        self.assertEqual(replaced["env"], {})

    def test_upsert_through_registry_keeps_timeout(self) -> None:
        registry = MCPRegistry([])
        saved = self.store.upsert_mcp_server({
            "id": "x1", "command": "npx", "args": ["-y", "a"], "start_timeout_ms": 90000,
        })
        state = registry.upsert(saved)
        self.assertEqual(state["start_timeout_ms"], 90000)
        self.assertEqual(state["command"], "npx")
        self.assertEqual(state["args"], ["-y", "a"])

    def test_hidden_tools_survive_edit_without_field(self) -> None:
        """UI 没有「工具屏蔽名单」控件 ⇒ 编辑保存时没传该字段，必须原样保留。

        否则用户点一次保存就把 rea 的 schema 防护抹掉，整个对话会被上游 400。
        """
        self.store.upsert_mcp_server({
            "id": "rea", "command": "npx",
            "hidden_tools": ["compare_web_captures"],
        })
        saved = self.store.upsert_mcp_server({"id": "rea", "command": "npx2"})
        self.assertEqual(saved["hidden_tools"], ["compare_web_captures"])
        # 显式传空数组 = 主动清空（与 env 的整体替换语义一致）。
        cleared = self.store.upsert_mcp_server({"id": "rea", "command": "npx3", "hidden_tools": []})
        self.assertEqual(cleared["hidden_tools"], [])

    def test_hidden_tools_normalized(self) -> None:
        saved = self.store.upsert_mcp_server({
            "id": "x1", "command": "a",
            "hidden_tools": ["  b  ", "b", "", None, "c"],
        })
        self.assertEqual(saved["hidden_tools"], ["b", "c"])
        for bad in (123, {"a": 1}):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.store.upsert_mcp_server({"id": "x2", "command": "a", "hidden_tools": bad})

    def test_hidden_tools_reach_connection(self) -> None:
        """连接层吃到的是配置里的屏蔽名单（与 ``naiba/mcp.py`` 的字段名对齐）。"""
        saved = self.store.upsert_mcp_server({
            "id": "rea", "command": "npx", "hidden_tools": ["compare_web_captures"],
        })
        registry = MCPRegistry([saved])
        self.assertEqual(
            registry.connections["rea"].hidden_tools, {"compare_web_captures"},
        )




class McpFrontendStructureTests(unittest.TestCase):
    """前端静态结构（真实交互由 verify/mcp_connectors_smoke.cjs 打真后端验收）。"""

    def setUp(self) -> None:
        self.index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.settings = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        self.bind = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        self.css = (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def _panel(self, name: str, until: str) -> str:
        start = self.index.index(f'data-settings-panel="{name}"')
        return self.index[start: self.index.index(f'data-settings-panel="{until}"', start)]

    def test_settings_has_dedicated_mcp_tab(self) -> None:
        self.assertIn('data-settings-tab="mcp"', self.index, "导航里没有「MCP 连接器」")
        self.assertIn("MCP 连接器", self.index)
        self.assertIn('data-settings-panel="mcp"', self.index, "缺少独立分页面板")
        self.assertIn('data-settings-search="MCP', self.index, "设置搜索匹配不到新分页")

    def test_mcp_moved_out_of_connections_panel(self) -> None:
        connections = self._panel("connections", "vision")
        self.assertNotIn("mcp", connections.lower(), "「局域网访问」页里还留着 MCP 元素")
        self.assertIn("局域网访问", connections)
        # 旧折叠表单的字段与 id 必须彻底消失（否则又是两处各写一份）。
        for legacy in ('id="mcpAddForm"', 'id="mcpNewId"', 'id="mcpNewComfyBin"', 'id="saveMcpServer"'):
            self.assertNotIn(legacy, self.index, f"旧 MCP 表单残留：{legacy}")
        self.assertEqual(self.index.count('id="mcpList"'), 1, "mcpList 必须唯一")

    def test_mcp_panel_has_search_form_and_modes(self) -> None:
        panel = self._panel("mcp", "connections")
        for element in (
            'id="mcpAddNew"', 'id="mcpSearchInput"', 'id="mcpCount"', 'id="mcpEmpty"',
            'id="mcpFormView"', 'id="mcpFormMode"', 'id="mcpFormId"', 'id="mcpFormTimeout"',
            'id="mcpFormCommand"', 'id="mcpFormArgs"', 'id="mcpFormEnv"',
            'id="mcpFormJsonText"', 'id="mcpFormSave"', 'id="mcpFormCancel"',
        ):
            self.assertIn(element, panel, f"MCP 分页缺少 {element}")
        # 新建表单预填 30000（后端缺省仍是 20000，旧配置零变化）。
        self.assertIn('id="mcpFormTimeout" type="number" value="30000"', panel)
        # 计划明确不做启停开关：工具启停在 Agent → 工具集里管。
        self.assertNotIn("mcpEnable", panel)
        self.assertNotIn("启用开关", panel)

    def test_router_jumps_to_new_tab(self) -> None:
        self.assertIn("switchSettingsTab('mcp')", self.bind, "顶栏 MCP 圆点还跳旧分页")
        self.assertNotIn("switchSettingsTab('connections')", self.bind)
        self.assertIn("if (name === 'mcp') loadMcpServers();", self.bind)
        for binding in (
            "$('#mcpList')?.addEventListener('click', handleMcpListClick)",
            "$('#mcpAddNew')?.addEventListener('click', () => openMcpForm())",
            "$('#mcpSearchInput')?.addEventListener('input'",
        ):
            self.assertIn(binding, self.bind, f"缺少接线：{binding}")

    def test_row_rendering_and_helpers(self) -> None:
        for name in ("export function splitArgs(", "export function parseMcpJson(",
                     "export function openMcpForm(", "export async function submitMcpForm(",
                     "export function renderMcpRows(", "export function toggleMcpRow("):
            self.assertIn(name, self.settings, f"缺少 {name}")
        # 列表只消费 env_keys，绝不读 server.env（值不在接口里）。
        self.assertIn("server.env_keys", self.settings)
        self.assertNotIn("server.env ", self.settings)
        # 副标题单行截断 = 旧版「长错误把按钮挤成一坨」的回归锁。
        rule = self.css[self.css.index(".settings-dialog .mcp-row-sub {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("white-space: nowrap", rule)
        self.assertIn("text-overflow: ellipsis", rule)
        self.assertIn("overflow: hidden", rule)
        # [hidden] 必须显式压回：行头/展开区都是 grid/flex 元素，作者样式会顶掉 UA 规则。
        self.assertIn(".settings-dialog .mcp-row-body[hidden] { display: none; }", self.css)
        self.assertIn(".settings-dialog #mcpFormView[hidden]", self.css)


if __name__ == "__main__":
    unittest.main()
