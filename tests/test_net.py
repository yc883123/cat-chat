# -*- coding: utf-8 -*-
"""net.py 代理策略 / opener 缓存 / 受控重试的标准库单元测试（无真实网络）。

背景（2026-09-12 实测教训）：设置了手动代理后，更新检查仍偶发直连失败——
根因是 opener 被缓存成“配置变更前”的实例，重试时又复用了同一个失败对象。
本文件守住三条不变量：

1. ``configure()`` 必须让三种 opener 全部失效（含捕获了旧环境变量的 system/direct）；
2. 重试必须重建“当前生效策略”对应的 opener，且**不得**清洗成用户未选择的模式；
3. 只对幂等方法（GET/HEAD/OPTIONS）重试一次，POST 等有副作用请求原样上抛。
"""
from __future__ import annotations

import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.net import NetIO, _is_local_host, _normalize_url  # noqa: E402


class FakeOpener:
    """记录调用次数、可编排“第 N 次抛错”的 opener 替身。"""

    def __init__(self, failures=0, error=None):
        self.failures = failures
        self.error = error or urllib.error.URLError(OSError("connection reset"))
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return f"ok:{getattr(request, 'full_url', request)}"


def url_error():
    return urllib.error.URLError(OSError("connection reset"))


class NormalizeUrlTests(unittest.TestCase):
    def test_accepts_http_https_and_bare_host(self):
        self.assertEqual(_normalize_url("http://127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertEqual(_normalize_url("https://proxy.local:1080"), "https://proxy.local:1080")
        # 省略协议时补 http://
        self.assertEqual(_normalize_url("127.0.0.1:7890"), "http://127.0.0.1:7890")

    def test_rejects_non_http_schemes_and_empty_host(self):
        for raw in ("socks5://127.0.0.1:1080", "ftp://proxy", "http://", "http:///path"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    _normalize_url(raw)

    def test_blank_is_allowed(self):
        self.assertEqual(_normalize_url("   "), "")


class LocalHostTests(unittest.TestCase):
    def test_loopback_private_and_suffixes_are_local(self):
        for host in ("localhost", "127.0.0.1", "127.1.2.3", "::1", "10.0.0.5",
                     "192.168.1.10", "169.254.1.1", "ollama.localhost", "box.lan"):
            with self.subTest(host=host):
                self.assertTrue(_is_local_host(host))

    def test_public_hosts_are_not_local(self):
        for host in ("api.github.com", "8.8.8.8", "1.1.1.1", ""):
            with self.subTest(host=host):
                self.assertFalse(_is_local_host(host))


class ConfigureInvalidationTests(unittest.TestCase):
    """配置变更必须让所有 opener 缓存失效（否则重试仍是旧代理/旧环境）。"""

    def setUp(self):
        self.io = NetIO()

    def test_configure_clears_every_opener_cache(self):
        self.io._manual_opener = object()
        self.io._direct_opener = object()
        self.io._system_opener = object()
        self.io.configure({"enabled": True, "url": "http://127.0.0.1:7890"})
        # 手动代理被重建为新实例，system/direct 被清空待惰性重建
        self.assertIsNotNone(self.io._manual_opener)
        self.assertIsNone(self.io._direct_opener)
        self.assertIsNone(self.io._system_opener)

    def test_configure_rebuilds_manual_opener_instance(self):
        self.io.configure({"enabled": True, "url": "http://127.0.0.1:7890"})
        first = self.io._manual_opener
        self.io.configure({"enabled": True, "url": "http://127.0.0.1:7890"})
        self.assertIsNot(first, self.io._manual_opener, "配置变更必须换成新 opener 实例")

    def test_invalid_proxy_url_falls_back_to_direct_with_error_state(self):
        self.io.configure({"enabled": True, "url": "socks5://127.0.0.1:1080"})
        state = self.io.proxy_state()
        self.assertEqual(state["source"], "direct")
        self.assertIn("error", state)
        self.assertIsNone(self.io._manual_opener)

    def test_configure_none_keeps_legacy_system_behaviour(self):
        self.io.configure(None)
        self.assertEqual(self.io.proxy_state()["source"], "system")


class ProxyStateTests(unittest.TestCase):
    def setUp(self):
        self.io = NetIO()

    def test_state_matrix(self):
        cases = [
            ({"enabled": False, "url": "http://p:1"}, "direct"),
            ({"enabled": True, "url": "http://p:1"}, "manual"),
            ({"enabled": True, "url": "", "use_system_fallback": True}, "system"),
            ({"enabled": True, "url": "", "use_system_fallback": False}, "direct"),
        ]
        for settings, expected in cases:
            with self.subTest(settings=settings):
                self.io.configure(settings)
                self.assertEqual(self.io.proxy_state()["source"], expected)


class ManualProxyNeverDegradesTests(unittest.TestCase):
    """手动代理生效时，任何路径都不得静默退回直连（本次修复的核心不变量）。"""

    def setUp(self):
        self.io = NetIO()
        self.io.configure({"enabled": True, "url": "http://127.0.0.1:7890"})

    def test_opener_for_rebuilds_proxy_opener_when_cache_cleared(self):
        self.io._manual_opener = None
        opener = self.io._opener_for("api.github.com", False)
        self.assertIsNotNone(opener)
        # 重建出的 opener 必须仍带手动代理 handler，而不是空 handler（=直连）
        proxy_handlers = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertIn("http", proxy_handlers[0].proxies)

    def test_retry_after_refresh_still_uses_proxy(self):
        """重试用的 opener 必须是“手动代理”实例，不能是直连实例。"""
        self.io._manual_opener = None
        refreshed = self.io._refresh_opener("api.github.com", False)
        direct = self.io._opener_for("127.0.0.1", True)
        self.assertIsNot(refreshed, direct)
        proxy_handlers = [h for h in refreshed.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertIn("http", proxy_handlers[0].proxies)


class RefreshOpenerTests(unittest.TestCase):
    """_refresh_opener 的分支必须与 _opener_for 的最终选择逐一对齐。

    注意断言口径：refresh 是「先丢弃、再按当前策略惰性重建」，所以不能断言
    字段为 None（重建后必然非 None），而应断言**实例已被替换**。
    """

    def setUp(self):
        self.io = NetIO()
        self.stale = object()

    def assertRefreshed(self, attr):
        """被清空并重建：字段非空且不再是注入的旧实例。"""
        value = getattr(self.io, attr)
        self.assertIsNotNone(value)
        self.assertIsNot(value, self.stale, f"{attr} 应被重建，而不是复用旧实例")

    def assertUntouched(self, attr):
        """本次重试不使用该 opener：旧实例原样保留。"""
        self.assertIs(getattr(self.io, attr), self.stale)

    def test_direct_when_proxy_disabled(self):
        self.io.configure({"enabled": False})
        self.io._direct_opener = self.stale
        self.io._refresh_opener("api.github.com", False)
        self.assertRefreshed("_direct_opener")

    def test_direct_when_local_host_regardless_of_proxy(self):
        self.io.configure({"enabled": True, "url": "http://127.0.0.1:7890"})
        self.io._direct_opener = self.stale
        self.io._refresh_opener("ollama.localhost", True)
        self.assertRefreshed("_direct_opener")

    def test_system_when_unconfigured(self):
        self.io.configure(None)
        self.io._system_opener = self.stale
        self.io._refresh_opener("api.github.com", False)
        self.assertRefreshed("_system_opener")

    def test_manual_when_url_present(self):
        self.io.configure({"enabled": True, "url": "http://127.0.0.1:7890"})
        first = self.io._manual_opener
        self.io._refresh_opener("api.github.com", False)
        self.assertIsNotNone(self.io._manual_opener)
        self.assertIsNot(first, self.io._manual_opener)

    def test_system_when_enabled_with_system_fallback(self):
        self.io.configure({"enabled": True, "url": "", "use_system_fallback": True})
        self.io._system_opener = self.stale
        self.io._refresh_opener("api.github.com", False)
        self.assertRefreshed("_system_opener")

    def test_direct_when_enabled_without_url_and_without_fallback(self):
        """回归：该策略旧写法不清任何 opener，重试复用同一个失败实例（空转）。"""
        self.io.configure({"enabled": True, "url": "", "use_system_fallback": False})
        self.io._direct_opener = self.stale
        self.io._refresh_opener("api.github.com", False)
        self.assertRefreshed("_direct_opener")

    def test_non_selected_caches_are_not_disturbed(self):
        """只重建当前策略的 opener，不顺手清掉其它模式的缓存。"""
        self.io.configure({"enabled": True, "url": "http://127.0.0.1:7890"})
        self.io._manual_opener = self.stale
        self.io._direct_opener = self.stale
        self.io._system_opener = self.stale
        self.io._refresh_opener("api.github.com", False)
        self.assertRefreshed("_manual_opener")
        self.assertUntouched("_direct_opener")
        self.assertUntouched("_system_opener")


class OpenRetryTests(unittest.TestCase):
    """open() 的受控重试语义：仅幂等方法、仅一次、不切换代理模式。"""

    def setUp(self):
        self.io = NetIO()

    def _drive(self, method, failures=1):
        """注入两个替身 opener，返回 (首次, 重试, 结果/异常)。"""
        first, second = FakeOpener(failures=failures), FakeOpener(failures=0)
        with mock.patch.object(self.io, "_opener_for", return_value=first), \
             mock.patch.object(self.io, "_refresh_opener", return_value=second):
            req = urllib.request.Request("https://api.github.com/x", method=method)
            try:
                return first, second, self.io.open(req, timeout=5)
            except BaseException as exc:  # noqa: BLE001 - 用例需检视异常类型
                return first, second, exc

    def test_get_retries_once_after_refresh(self):
        first, second, result = self._drive("GET")
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)
        self.assertTrue(str(result).startswith("ok:"))

    def test_head_and_options_also_retry(self):
        for method in ("HEAD", "OPTIONS"):
            with self.subTest(method=method):
                first, second, _ = self._drive(method)
                self.assertEqual(second.calls, 1)

    def test_post_does_not_retry(self):
        first, second, result = self._drive("POST")
        self.assertIsInstance(result, urllib.error.URLError)
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 0, "有副作用请求不得重放")

    def test_retries_only_once_even_if_second_fails(self):
        first, second = FakeOpener(failures=1), FakeOpener(failures=1)
        with mock.patch.object(self.io, "_opener_for", return_value=first), \
             mock.patch.object(self.io, "_refresh_opener", return_value=second):
            req = urllib.request.Request("https://api.github.com/x", method="GET")
            with self.assertRaises(urllib.error.URLError):
                self.io.open(req, timeout=5)
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1, "重试必须严格一次，不得递归")

    def test_http_error_is_not_retried(self):
        """服务端已给出明确状态码时不重试，交上层按状态分类。"""
        error = urllib.error.HTTPError("https://api.github.com/x", 403, "forbidden", {}, None)
        first = FakeOpener(failures=1, error=error)
        second = FakeOpener(failures=0)
        with mock.patch.object(self.io, "_opener_for", return_value=first), \
             mock.patch.object(self.io, "_refresh_opener", return_value=second):
            req = urllib.request.Request("https://api.github.com/x", method="GET")
            with self.assertRaises(urllib.error.HTTPError):
                self.io.open(req, timeout=5)
        self.assertEqual(second.calls, 0)

    def test_str_url_form_uses_supplied_method_and_headers(self):
        captured = {}

        class CaptureOpener:
            def open(self, request, timeout=None):
                captured["method"] = request.get_method()
                captured["headers"] = dict(request.headers)
                captured["data"] = request.data
                return "ok"

        with mock.patch.object(self.io, "_opener_for", return_value=CaptureOpener()):
            self.io.open("https://api.github.com/x", method="POST", data=b"{}",
                         headers={"X-Test": "1"}, timeout=3)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["data"], b"{}")
        self.assertEqual(captured["headers"].get("X-test"), "1")

    def test_local_host_bypasses_proxy_configured_for_external(self):
        """本地服务永远直连：即使配了手动代理也不走代理 opener。"""
        self.io.configure({"enabled": True, "url": "http://127.0.0.1:7890"})
        opener = FakeOpener(failures=0)
        with mock.patch.object(self.io, "_opener_for", return_value=opener) as spy:
            self.io.open("http://127.0.0.1:11434/api/tags", timeout=1)
        host, local = spy.call_args[0]
        self.assertTrue(local)
        self.assertEqual(host, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
