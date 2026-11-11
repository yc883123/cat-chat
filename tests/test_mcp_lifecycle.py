"""Deterministic registry races with controllable connection startup/shutdown."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from naiba.mcp import MCPError, MCPRegistry, MCPServerConnection


class ControlledConnection(MCPServerConnection):
    def __init__(self, config):
        super().__init__(config["id"], config["command"], [], {})
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.starts = 0
        self.stops = 0
        self.keep_running = False

    def start(self, timeout=None):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("startup gate not released")
        self.starts += 1
        self._session = object()
        self._stopped = False
        self._stopping = False
        if self.on_tools_discovered:
            self.on_tools_discovered(self.server_id, [{"name": self.command}])

    def stop(self):
        self.stops += 1
        self._stopping = self.keep_running
        if not self.keep_running:
            self._session = None
            self._stopped = True


class MCPRegistryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.registry = MCPRegistry([])
        self.connections = []
        self.threads = []
        self.errors = []
        self.next_blocked = False
        self.patcher = patch.object(self.registry, "_connection", side_effect=self.factory)
        self.patcher.start()
        self.addCleanup(lambda: self.patcher.stop())
        self.registry.start()

    def tearDown(self):
        for connection in self.connections:
            connection.release.set()
            connection.keep_running = False
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.registry.stop()
        self.assertEqual(self.errors, [])

    def factory(self, config):
        connection = ControlledConnection(config)
        if self.next_blocked:
            connection.release.clear()
            self.next_blocked = False
        self.connections.append(connection)
        return connection

    def spawn(self, action):
        finished = threading.Event()

        def run():
            try:
                action()
            except Exception as exc:
                self.errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return finished

    def blocked_save(self):
        self.next_blocked = True
        constructed = threading.Event()
        original = self.factory

        def factory(config):
            connection = original(config)
            constructed.set()
            return connection

        self.patcher.stop()
        self.patcher = patch.object(self.registry, "_connection", side_effect=factory)
        self.patcher.start()
        self.spawn(lambda: self.registry.upsert({"id": "same", "command": "first"}))
        self.assertTrue(constructed.wait(2))
        connection = self.connections[-1]
        self.assertTrue(connection.entered.wait(2))
        return connection

    def test_two_saves_stop_every_superseded_connection(self):
        first = self.blocked_save()
        second_done = self.spawn(lambda: self.registry.upsert({"id": "same", "command": "second"}))
        self.assertFalse(second_done.wait(0.05))
        first.release.set()
        self.assertTrue(second_done.wait(2))
        current = self.registry.connection("same")
        self.assertEqual(current.command, "second")
        self.assertEqual(first.stops, 1)
        self.assertFalse(first.state()["connected"])
        self.registry.stop()
        self.assertTrue(all(not c.state()["connected"] for c in self.connections))

    def test_remove_waits_for_inflight_start_and_leaves_no_process(self):
        first = self.blocked_save()
        removed = self.spawn(lambda: self.registry.remove("same"))
        first.release.set()
        self.assertTrue(removed.wait(2))
        self.assertIsNone(self.registry.connection("same"))
        self.assertFalse(first.state()["connected"])

    def test_stop_waits_for_inflight_start_and_prevents_late_watchdog_start(self):
        first = self.blocked_save()
        stopped = self.spawn(self.registry.stop)
        first.release.set()
        self.assertTrue(stopped.wait(2))
        self.registry._start_current(first, retry=True)
        self.assertEqual(first.starts, 1)
        self.assertFalse(first.state()["connected"])

    def test_unrelated_save_and_status_are_not_blocked_by_startup(self):
        first = self.blocked_save()
        finished = self.spawn(lambda: self.registry.upsert({"id": "other", "command": "other"}))
        self.assertTrue(finished.wait(2))
        self.assertEqual(len(self.registry.states()), 2)
        self.assertTrue(self.registry.connection("other").state()["connected"])
        first.release.set()

    def test_stale_start_snapshot_and_discovery_cannot_revive_removed_server(self):
        discovered = []
        self.registry.on_tools_discovered = lambda sid, tools: discovered.append((sid, tools))
        self.registry.upsert({"id": "same", "command": "first"})
        first = self.registry.connection("same")
        self.registry.remove("same")
        discovered.clear()
        self.registry._start_current(first)
        first.on_tools_discovered("same", [{"name": "late"}])
        self.assertEqual(first.starts, 1)
        self.assertEqual(discovered, [])

    def test_unconfirmed_stop_preserves_manageable_handle_and_blocks_replacement(self):
        self.registry.upsert({"id": "same", "command": "first"})
        first = self.registry.connection("same")
        first.keep_running = True
        for action in (
            lambda: self.registry.upsert({"id": "same", "command": "replacement"}),
            lambda: self.registry.upsert({"id": "same", "command": "first", "enabled": False}),
            lambda: self.registry.remove("same"),
            lambda: self.registry.reconnect("same"),
        ):
            with self.subTest(action=action), self.assertRaisesRegex(MCPError, "停止尚未确认"):
                action()
            self.assertIs(self.registry.connection("same"), first)
            self.assertEqual(self.registry.states()[0]["status"], "stopping")
            self.assertEqual(len(self.connections), 1)
        self.registry.stop()
        self.assertIs(self.registry.connection("same"), first)
        first.keep_running = False
        self.assertTrue(self.registry.remove("same"))

    def test_disabled_save_cannot_leave_first_connection_running(self):
        first = self.blocked_save()
        disabled = self.spawn(lambda: self.registry.upsert({"id": "same", "command": "first", "enabled": False}))
        first.release.set()
        self.assertTrue(disabled.wait(2))
        self.assertIsNone(self.registry.connection("same"))
        self.assertFalse(first.state()["connected"])

    def test_call_reconnect_finishes_before_remove_without_blocking_other_servers(self):
        self.registry.upsert({"id": "same", "command": "first"})
        first = self.registry.connection("same")
        first.stop()
        entered = threading.Event()
        release = threading.Event()
        original_call = first.call

        def blocked_call(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("call gate not released")
            return original_call(*args)

        with patch.object(first, "call", side_effect=blocked_call):
            try:
                called = self.spawn(lambda: self.registry.call("same", "echo", {}))
                self.assertTrue(entered.wait(2))
                removed = self.spawn(lambda: self.registry.remove("same"))
                self.assertFalse(removed.wait(0.05))
                other_saved = self.spawn(lambda: self.registry.upsert({"id": "other", "command": "other"}))
                self.assertTrue(other_saved.wait(2))
                self.assertEqual(len(self.registry.states()), 2)
            finally:
                release.set()
            self.assertTrue(called.wait(2))
            self.assertTrue(removed.wait(2))
        self.assertEqual(first.starts, 2)
        self.assertFalse(first.state()["connected"])
        self.assertIsNone(self.registry.connection("same"))
        self.assertFalse(self.registry.call("same", "echo", {})[0])
        self.assertEqual(first.starts, 2, "a removed connection must never restart")

    def test_reconnect_timeout_keeps_original_thread_and_does_not_start_another(self):
        connection = MCPServerConnection("slow", "probe", [], {})
        old_thread = Mock()
        old_thread.is_alive.return_value = True
        connection._thread = old_thread
        with patch.object(connection, "start") as start:
            self.assertFalse(connection.reconnect_once())
            start.assert_not_called()
        self.assertIs(connection._thread, old_thread)
        self.assertEqual(connection.state()["status"], "stopping")
        old_thread.join.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
