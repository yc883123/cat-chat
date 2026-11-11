"""MCP 管理入口必须经过注册表的生命周期保护。"""
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, MagicMock, patch

from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.mcp import MCPError, MCPRegistry, MCPStartupError
from tests.test_mcp_lifecycle import ControlledConnection


class MCPAppLifecycleTests(unittest.TestCase):
    def test_delete_keeps_configuration_when_connection_cannot_stop(self):
        config = Mock()
        registry = MagicMock()
        registry.remove.side_effect = RuntimeError("连接仍在停止中")
        app = SimpleNamespace(config=config, mcp=registry)
        with self.assertRaisesRegex(RuntimeError, "停止中"):
            NaibaChatApp.remove_mcp_server(app, "test-server")
        config.delete_mcp_server.assert_not_called()

    def test_delete_stops_connection_before_deleting_configuration(self):
        operations = []
        app = SimpleNamespace(
            config=SimpleNamespace(delete_mcp_server=lambda key: operations.append(("delete", key))),
            mcp=SimpleNamespace(remove=lambda key: operations.append(("stop", key)),
                                server_operation=lambda _key: threading.RLock()),
        )
        result = NaibaChatApp.remove_mcp_server(app, "test-server")
        self.assertEqual(operations, [("stop", "test-server"), ("delete", "test-server")])
        self.assertTrue(result["removed"])

    def test_reconnect_uses_registry_serialization(self):
        registry = Mock()
        registry.reconnect.return_value = {"id": "test-server", "status": "connected"}
        result = NaibaChatApp.reconnect_mcp_server(SimpleNamespace(mcp=registry), "test-server")
        registry.reconnect.assert_called_once_with("test-server")
        registry.connection.assert_not_called()
        self.assertEqual(result["status"], "connected")


class MCPAppConfigurationRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config = ConfigStore(self.config_path)
        self.registry = MCPRegistry([])
        self.app = SimpleNamespace(config=self.config, mcp=self.registry)
        self.connections = []
        self.threads = []
        self.gates = []
        self.errors = []
        self.patcher = patch.object(self.registry, "_connection", side_effect=self._connection)
        self.patcher.start()
        self.registry.start()

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for connection in self.connections:
            connection.keep_running = False
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.registry.stop()
        self.patcher.stop()
        self.tmp.cleanup()
        self.assertEqual(self.errors, [])

    def _connection(self, values):
        connection = ControlledConnection(values)
        self.connections.append(connection)
        return connection

    def _save(self, command, *, server_id="same", **values):
        return NaibaChatApp.register_mcp_server(self.app, {"id": server_id, "command": command, **values})

    def _spawn(self, action):
        entered, finished = threading.Event(), threading.Event()

        def run():
            entered.set()
            try:
                action()
            except Exception as exc:
                self.errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        self.assertTrue(entered.wait(2))
        return finished

    def _persisted(self):
        return {item["id"]: item for item in json.loads(self.config_path.read_text(encoding="utf-8"))["mcp_servers"]}

    def _block_first_save(self):
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        original = self.config.upsert_mcp_server

        def save(values):
            config = original(values)
            if values["command"] == "first":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("save gate not released")
            return config

        patcher = patch.object(self.config, "upsert_mcp_server", side_effect=save)
        patcher.start()
        self.addCleanup(patcher.stop)
        first_done = self._spawn(lambda: self._save("first"))
        self.assertTrue(entered.wait(2))
        return first_done, release

    def test_two_saves_keep_persisted_and_live_configuration_in_same_order(self):
        first_done, release = self._block_first_save()
        second_done = self._spawn(lambda: self._save("second"))
        self.assertFalse(second_done.wait(0.05))
        self.assertEqual(self._persisted()["same"]["command"], "first")
        other_done = self._spawn(lambda: self._save("unrelated", server_id="other"))
        self.assertTrue(other_done.wait(2), "a different ID must not wait for this save")
        release.set()
        self.assertTrue(first_done.wait(2))
        self.assertTrue(second_done.wait(2))
        self.assertEqual(self._persisted()["same"]["command"], "second")
        self.assertEqual(self.registry.connection("same").command, "second")
        self.assertEqual(self._persisted()["other"]["command"], "unrelated")
        first_connection = next(item for item in self.connections if item.command == "first")
        self.assertFalse(first_connection.state()["connected"])

    def test_remove_after_save_leaves_neither_configuration_nor_live_connection(self):
        first_done, release = self._block_first_save()
        removed = self._spawn(lambda: NaibaChatApp.remove_mcp_server(self.app, "same"))
        self.assertFalse(removed.wait(0.05))
        release.set()
        self.assertTrue(first_done.wait(2))
        self.assertTrue(removed.wait(2))
        self.assertNotIn("same", self._persisted())
        self.assertIsNone(self.registry.connection("same"))
        self.assertTrue(all(not item.state()["connected"] for item in self.connections))

    def test_save_after_remove_cannot_have_its_configuration_deleted_by_old_request(self):
        self._save("old")
        old = self.registry.connection("same")
        entered, release = threading.Event(), threading.Event()
        self.gates.append(release)
        original_stop = old.stop

        def stop():
            entered.set()
            if not release.wait(5):
                raise AssertionError("stop gate not released")
            original_stop()

        with patch.object(old, "stop", side_effect=stop):
            removed = self._spawn(lambda: NaibaChatApp.remove_mcp_server(self.app, "same"))
            self.assertTrue(entered.wait(2))
            saved = self._spawn(lambda: self._save("replacement"))
            self.assertFalse(saved.wait(0.05))
            release.set()
            self.assertTrue(removed.wait(2))
            self.assertTrue(saved.wait(2))
        self.assertEqual(self._persisted()["same"]["command"], "replacement")
        self.assertEqual(self.registry.connection("same").command, "replacement")
        self.assertFalse(old.state()["connected"])

    def test_stop_timeout_restores_old_configuration_for_replace_and_disable(self):
        self._save("old", env={"TEST_KEY": "old-value"}, hidden_tools=["hidden"])
        previous = self._persisted()["same"]
        old = self.registry.connection("same")
        old.keep_running = True
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                with self.assertRaisesRegex(MCPError, "停止尚未确认"):
                    self._save("replacement", env={"TEST_KEY": "new-value"}, enabled=enabled)
                self.assertEqual(self._persisted()["same"], previous)
                self.assertIs(self.registry.connection("same"), old)
                self.assertEqual(old.state()["status"], "stopping")

    def test_startup_failure_keeps_new_configuration_for_the_registered_replacement(self):
        self._save("old")
        old = self.registry.connection("same")
        replacement = ControlledConnection({"id": "same", "command": "replacement"})
        self.connections.append(replacement)
        with patch.object(self.registry, "_connection", return_value=replacement), \
                patch.object(replacement, "start", side_effect=MCPStartupError("startup timeout")):
            with self.assertRaisesRegex(MCPStartupError, "startup timeout"):
                self._save("replacement")
        self.assertEqual(self._persisted()["same"]["command"], "replacement")
        self.assertIs(self.registry.connection("same"), replacement)
        self.assertFalse(old.state()["connected"])
