# -*- coding: utf-8 -*-
"""updater.py 更新流程的标准库单元测试（无网络，全部通过桩函数拦截请求）。

覆盖：磁盘缓存读回/回退、latest 静态直连兜底、合成 latest 条目、
错误提示分级、清单校验失败直连失败不降级，以及正常 API 路径与源码模式回归。
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.updater import (  # noqa: E402
    EXECUTABLE_ASSET,
    LATEST_TAG,
    MANIFEST_ASSET,
    READY_MARKER,
    REPOSITORY,
    UpdateCancelled,
    UpdateManager,
)


API_RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
TAG_DL_BASE = f"https://github.com/{REPOSITORY}/releases/download"
LATEST_DL_BASE = f"https://github.com/{REPOSITORY}/releases/latest/download"
COMMIT_A = "a" * 40
COMMIT_C = "c" * 40
SHA_B = "b" * 64


def manifest_payload(version="2.1.7-beta", commit=COMMIT_A, repository=REPOSITORY, **overrides):
    value = {
        "repository": repository,
        "commit": commit,
        "sha256": SHA_B,
        "asset": EXECUTABLE_ASSET,
        "version": version,
        "release_notes": ["更新说明 A", "更新说明 B"],
    }
    value.update(overrides)
    return value


def api_release_item(tag, installable=True, body="发布说明"):
    assets = [{"name": MANIFEST_ASSET}, {"name": EXECUTABLE_ASSET}] if installable else []
    return {
        "tag_name": tag,
        "published_at": "2026-08-01T00:00:00Z",
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
        "body": body,
        "assets": assets,
    }


def api_releases(*tags, installable=True):
    return [api_release_item(tag, installable=installable) for tag in tags]


def http_error(code=403, body="", headers=None, url="https://api.github.com/x"):
    fp = io.BytesIO(body.encode("utf-8"))
    return urllib.error.HTTPError(url, code, "err", headers or {}, fp)


class RequestStub:
    """按 URL 分发的桩；记录全部被请求的 URL。"""

    def __init__(self):
        self.calls = []

    def route(self, api=None, latest=None, tag=None):
        def dispatch(url, **_kwargs):
            # **_kwargs：updater 现在会带 opener= 走「更新代理」scoped 入口，桩只关心 URL。
            self.calls.append(url)
            if url.startswith(API_RELEASES_URL):
                return self._resolve(api, url)
            if url.startswith(f"{LATEST_DL_BASE}/"):
                return self._resolve(latest, url)
            if url.startswith(f"{TAG_DL_BASE}/"):
                return self._resolve(tag, url)
            raise AssertionError(f"unexpected url: {url}")

        return dispatch

    @staticmethod
    def _resolve(value, url):
        if callable(value):
            return value(url)
        if isinstance(value, BaseException):
            raise value
        return value


def fake_download_response(payload: bytes, *, on_read=None):
    """构造 `_download` 需要的响应对象（只用得到 headers / read / 上下文管理）。"""

    class FakeResponse:
        def __init__(self):
            self._buf = io.BytesIO(payload)
            self.headers = {"Content-Length": str(len(payload))}
            self.reads = 0

        def read(self, size=-1):
            self.reads += 1
            if on_read is not None:
                on_read(self.reads)
            return self._buf.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return FakeResponse()


class ExecutableUpdateTests(unittest.TestCase):
    """以“打包后 EXE”姿态运行：sys.frozen=True，HTTP 层全部打桩。"""

    def setUp(self):
        self._had_frozen = hasattr(sys, "frozen")
        self._old_frozen = getattr(sys, "frozen", None)
        sys.frozen = True
        self._tmp = tempfile.TemporaryDirectory()
        self.app_dir = Path(self._tmp.name) / "app"
        self.data_dir = Path(self._tmp.name) / "data"
        self.app_dir.mkdir()
        self.data_dir.mkdir()
        self.manager = UpdateManager(self.app_dir, self.data_dir)
        self.stub = RequestStub()

    def tearDown(self):
        self._tmp.cleanup()
        if self._had_frozen:
            sys.frozen = self._old_frozen
        else:
            delattr(sys, "frozen")

    def set_build(self, version, commit=None):
        self.manager.build = {"version": version, "commit": commit or COMMIT_C}

    def write_cache(self, entries):
        cache = self.data_dir / "update" / "releases.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return cache

    def cache_entry(self, tag, installable=True, notes=None):
        return {
            "tag": tag,
            "version": tag.lstrip("v"),
            "published_at": "2026-07-01T00:00:00Z",
            "release_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
            "release_notes": notes or ["旧版本说明"],
            "installable": installable,
            "current": False,
        }

    def test_release_normalization_filters_migration_and_dedupes(self):
        entries = [
            self.cache_entry("v1.9.9"),
            self.cache_entry("v2.0.0"),
            self.cache_entry("v2.1.0-beta"),
            self.cache_entry("v2.1.0"),
            self.cache_entry("v2.1.0"),
        ]
        normalized = UpdateManager._normalize_releases(entries)
        self.assertEqual([item["version"] for item in normalized], ["2.1.0", "2.1.0-beta"])

    def test_manual_migration_manifest_is_reported_without_install(self):
        self.set_build("1.9.9", COMMIT_C)
        self.manager._request_json = self.stub.route(
            api=http_error(403), latest=manifest_payload(version="2.0.0", commit=COMMIT_A)
        )
        status = self.manager.check(force=True)
        self.assertEqual(status["phase"], "current")
        self.assertTrue(status["manual_update_available"])
        self.assertEqual(status["manual_release_url"], f"https://github.com/{REPOSITORY}/releases")

    def test_same_version_different_commit_is_available(self):
        self.set_build("2.1.0", COMMIT_C)
        self.manager._request_json = self.stub.route(
            api=http_error(403), latest=manifest_payload(version="2.1.0", commit=COMMIT_A)
        )
        status = self.manager.check(force=True)
        self.assertEqual(status["phase"], "available")
        self.assertTrue(status["update_available"])

    # ---------- 错误提示分级 ----------

    def test_http_error_message_mapping(self):
        cases = [
            (http_error(404), "尚无可用的自动更新版本"),
            (http_error(429, headers={"Retry-After": "60"}), "GitHub 接口访问频率受限，请稍后重试"),
            (http_error(403, headers={"X-RateLimit-Remaining": "0"}), "GitHub 接口访问频率受限，请稍后重试"),
            (http_error(403, body="API rate limit exceeded for 1.2.3.4."), "GitHub 接口访问频率受限，请稍后重试"),
            (http_error(403), "GitHub 拒绝了请求（HTTP 403），请检查网络或代理设置"),
            (http_error(500), "检查更新失败：HTTP 500"),
        ]
        for exc, expected in cases:
            with self.subTest(code=exc.code):
                self.assertEqual(UpdateManager._http_error_message(exc), expected)

    def test_is_rate_limited_only_for_relevant_codes(self):
        self.assertTrue(UpdateManager._is_rate_limited(http_error(429, headers={"Retry-After": "30"})))
        self.assertTrue(UpdateManager._is_rate_limited(http_error(403, body="API rate limit exceeded")))
        self.assertFalse(UpdateManager._is_rate_limited(http_error(403)))
        self.assertFalse(UpdateManager._is_rate_limited(http_error(500, body="rate limit")))

    # ---------- 磁盘缓存读回与回退 ----------

    def test_fresh_disk_cache_reused_without_api(self):
        self.write_cache([self.cache_entry("v2.1.6-beta")])
        self.manager._request_json = self.stub.route(api=AssertionError("API 不应被请求"))
        result = self.manager._fetch_releases()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["version"], "2.1.6-beta")
        self.assertTrue(self.manager._releases_from_cache)
        self.assertEqual(self.stub.calls, [])

    def test_force_bypasses_fresh_cache(self):
        self.write_cache([self.cache_entry("v2.1.6-beta")])
        api_payload = api_releases("v2.1.7-beta", "v2.1.6-beta")
        self.manager._request_json = self.stub.route(api=api_payload)
        result = self.manager._fetch_releases(force=True)
        self.assertEqual([item["tag"] for item in result], ["v2.1.7-beta", "v2.1.6-beta"])
        self.assertFalse(self.manager._releases_from_cache)
        self.assertIn(API_RELEASES_URL, self.stub.calls)

    def test_corrupt_cache_ignored_then_api(self):
        corrupt_payloads = [
            "{not-json",
            "[]",
            '{"x": 1}',
            json.dumps([{"tag": "v1", "version": "1", "installable": "yes"}]),
        ]
        for payload in corrupt_payloads:
            with self.subTest(payload=payload):
                self.write_cache(json.loads(payload)) if payload != "{not-json" else None
                if payload == "{not-json":
                    cache = self.data_dir / "update" / "releases.json"
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(payload, encoding="utf-8")
                api_payload = api_releases("v2.1.7-beta")
                self.manager._request_json = self.stub.route(api=api_payload)
                result = self.manager._fetch_releases()
                self.assertEqual([item["tag"] for item in result], ["v2.1.7-beta"])
                self.assertIn(API_RELEASES_URL, self.stub.calls)
                self.assertFalse(self.manager._releases_from_cache)

    def test_api_failure_falls_back_to_cache_even_when_stale(self):
        cache = self.write_cache([self.cache_entry("v2.1.6-beta")])
        old = time.time() - 10 * 3600
        os.utime(cache, (old, old))
        self.manager._request_json = self.stub.route(api=http_error(403, body="rate limit"))
        result = self.manager._fetch_releases()
        self.assertEqual([item["tag"] for item in result], ["v2.1.6-beta"])
        self.assertTrue(self.manager._releases_from_cache)

    def test_api_failure_without_cache_raises(self):
        self.manager._request_json = self.stub.route(api=http_error(403))
        with self.assertRaises(urllib.error.HTTPError):
            self.manager._fetch_releases()

    # ---------- check() 回退与合成条目 ----------

    def test_check_falls_back_to_latest_when_list_403(self):
        self.set_build("2.1.6-beta")
        self.manager._request_json = self.stub.route(
            api=http_error(403, body="rate limit"), latest=manifest_payload()
        )
        status = self.manager.check(force=True)
        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["latest_version"], "2.1.7-beta")
        top = status["releases"][0]
        self.assertEqual(top["tag"], LATEST_TAG)
        self.assertTrue(top["installable"])
        self.assertFalse(top["current"])
        # API 列表确实被请求并失败（无缓存），随后成功回退到 latest 静态清单
        self.assertTrue(any(url.startswith(API_RELEASES_URL) for url in self.stub.calls))
        latest_calls = [url for url in self.stub.calls if url.startswith(f"{LATEST_DL_BASE}/")]
        self.assertEqual(len(latest_calls), 1)
        self.assertFalse(any(url.startswith(f"{TAG_DL_BASE}/") for url in self.stub.calls))

    def test_check_latest_current_when_already_latest(self):
        self.set_build("2.1.7-beta", COMMIT_A)
        self.manager._request_json = self.stub.route(
            api=http_error(403), latest=manifest_payload(commit=COMMIT_A)
        )
        status = self.manager.check(force=True)
        self.assertEqual(status["phase"], "current")
        self.assertTrue(status["releases"][0]["current"])

    def test_check_falls_back_when_chosen_tag_manifest_network_fails(self):
        self.set_build("2.1.6-beta")
        api_payload = api_releases("v2.1.7-beta", "v2.1.6-beta")

        def tag_handler(url):
            if "v2.1.7-beta" in url:
                raise http_error(500, url=url)
            return manifest_payload(version="2.1.6-beta", commit=COMMIT_A)

        self.manager._request_json = self.stub.route(api=api_payload, tag=tag_handler, latest=manifest_payload())
        status = self.manager.check(force=True)
        self.assertEqual(status["phase"], "available")
        # 权威来源切换为 latest 直连清单
        self.assertEqual(self.manager.latest["tag"], LATEST_TAG)
        self.assertTrue(self.manager.latest["download_url"].startswith(f"{LATEST_DL_BASE}/"))
        failed_tag_url = f"{TAG_DL_BASE}/v2.1.7-beta/{MANIFEST_ASSET}"
        self.assertIn(failed_tag_url, self.stub.calls)
        self.assertTrue(any(url.startswith(f"{LATEST_DL_BASE}/") for url in self.stub.calls))
        # API 列表本身可用时不再合成重复条目（列表中已有同版本真实条目）
        self.assertEqual(status["releases"][0]["tag"], "v2.1.7-beta")

    def test_check_validation_failure_does_not_fallback(self):
        self.set_build("2.1.6-beta")
        api_payload = api_releases("v2.1.7-beta")
        bad = manifest_payload(repository="someone/else")
        self.manager._request_json = self.stub.route(api=api_payload, tag=lambda url: bad)
        status = self.manager.check(force=True)
        self.assertEqual(status["phase"], "error")
        self.assertIn("仓库不匹配", status["error"])
        self.assertFalse(any(url.startswith(f"{LATEST_DL_BASE}/") for url in self.stub.calls))

    def test_check_urlerror_message(self):
        network_error = urllib.error.URLError(OSError("timed out"))
        self.manager._request_json = self.stub.route(api=network_error, latest=network_error)
        status = self.manager.check(force=True)
        self.assertEqual(status["phase"], "error")
        # 基础文案保持不变，仅在末尾附带本次更新实际走的链路（用于定位代理问题）。
        self.assertTrue(status["error"].startswith("无法连接更新服务器，请检查网络后重试"))
        self.assertIn("更新链路：", status["error"])

    def test_check_normal_api_path_regression(self):
        self.set_build("2.1.6-beta")
        api_payload = api_releases("v2.1.7-beta", "v2.1.6-beta")
        self.manager._request_json = self.stub.route(
            api=api_payload, tag=lambda url: manifest_payload(version="2.1.7-beta")
        )
        status = self.manager.check(force=True)
        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["releases"][0]["tag"], "v2.1.7-beta")
        self.assertFalse(any(item["tag"] == LATEST_TAG for item in status["releases"]))
        self.assertEqual(self.manager.latest["tag"], "v2.1.7-beta")
        self.assertIn(f"{TAG_DL_BASE}/v2.1.7-beta/{MANIFEST_ASSET}", self.stub.calls)
        self.assertFalse(any(url.startswith(f"{LATEST_DL_BASE}/") for url in self.stub.calls))

    # ---------- 下载进度 / 取消 / 「已下载待重启」 ----------

    def _payload(self, size=2 * 1024 * 1024):
        payload = b"MZ" + b"\x00" * (size - 2)
        return payload, hashlib.sha256(payload).hexdigest()

    def _latest(self, checksum, commit=COMMIT_A):
        return {
            "download_url": f"{LATEST_DL_BASE}/{EXECUTABLE_ASSET}",
            "commit": commit,
            "sha256": checksum,
            "version": "2.1.7-beta",
        }

    def _downloaded_path(self, commit=COMMIT_A):
        return self.data_dir / "update" / f"naiba-chat-{commit[:12]}.download"

    def test_download_reports_progress_in_monotonic_chunks(self):
        """下载必须按块上报进度（前端进度条/卡死提示的唯一数据源）。"""
        payload, checksum = self._payload()
        response = fake_download_response(payload)
        self.manager._open = lambda request, timeout=None: response
        seen = []
        original = self.manager._advance_download
        self.manager._advance_download = lambda received: (seen.append(received), original(received))[1]

        path = self.manager._download(self._latest(checksum))

        self.assertTrue(path.is_file(), "校验通过后必须保留安装包（等待用户选择重启时机）")
        self.assertEqual(path.read_bytes()[:2], b"MZ")
        self.assertEqual(seen, sorted(seen), "已下载字节必须单调递增")
        self.assertEqual(seen[-1], len(payload), "最后一块必须等于文件总大小")
        snapshot = self.manager._download_snapshot()
        self.assertEqual(snapshot["total"], len(payload))
        self.assertEqual(snapshot["percent"], 100)

    def test_cancel_during_download_removes_partial_file(self):
        """取消必须删掉半成品，绝不能让一个残缺 exe 留在数据目录里。"""
        payload, checksum = self._payload(size=4 * 1024 * 1024)
        response = fake_download_response(
            payload, on_read=lambda reads: self.manager._cancel.set() if reads == 1 else None
        )
        self.manager._open = lambda request, timeout=None: response

        with self.assertRaises(UpdateCancelled):
            self.manager._download(self._latest(checksum))

        self.assertFalse(self._downloaded_path().exists(), "半成品必须被清理")

    def test_cancel_install_requires_downloading_phase(self):
        with self.assertRaises(RuntimeError):
            self.manager.cancel_install()

    def test_download_snapshot_flags_stall_after_silence(self):
        """长时间没有新字节 ⇒ stalled=True（前端据此把进度条标黄）。"""
        self.manager.phase = "downloading"
        self.manager._begin_download(1000)
        self.manager._advance_download(100)
        self.assertFalse(self.manager._download_snapshot()["stalled"])
        self.manager.download["updated_at"] = time.time() - 60
        self.assertTrue(self.manager._download_snapshot()["stalled"])
        self.manager.phase = "idle"
        self.assertFalse(self.manager._download_snapshot()["stalled"], "非下载态不该报卡死")

    @unittest.skipUnless(os.name == "nt", "一键安装路径仅适用于 Windows")
    def test_start_install_downloads_then_waits_for_user_restart(self):
        """下载成功后进入 ready 态：不启动替换脚本、不写 pending 标记，等用户点「立即重启」。"""
        self.set_build("2.1.6-beta")
        self.manager.releases = [
            {"tag": LATEST_TAG, "version": "2.1.7-beta", "published_at": "",
             "release_url": f"https://github.com/{REPOSITORY}/releases/latest",
             "release_notes": ["说明"], "installable": True, "current": False}
        ]
        self.manager._request_json = self.stub.route(
            latest=manifest_payload(), tag=lambda url: manifest_payload()
        )
        staged = self._downloaded_path()
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"MZ" + b"\x00" * 1024)
        launched = []
        self.manager._download = lambda latest: staged
        self.manager._launch_replacer = lambda downloaded: launched.append(downloaded)

        self.manager.start_install(LATEST_TAG)
        deadline = time.time() + 5
        while self.manager.phase not in {"ready", "error"} and time.time() < deadline:
            time.sleep(0.05)

        self.assertEqual(self.manager.phase, "ready")
        self.assertEqual(launched, [], "下载完成不得自动替换 exe（重启时机由用户决定）")
        self.assertFalse((self.data_dir / "update" / "pending-update.json").exists(),
                         "pending 标记只在真正安装那一刻写，否则失败/取消后会误报「上次更新校验失败」")
        self.assertTrue((self.data_dir / "update" / READY_MARKER).is_file())
        status = self.manager.status()
        self.assertTrue(status["can_apply"])
        self.assertEqual(status["ready"]["version"], "2.1.7-beta")
        self.assertTrue(any(url.startswith(f"{LATEST_DL_BASE}/") for url in self.stub.calls))
        self.assertFalse(any(url.startswith(API_RELEASES_URL) for url in self.stub.calls))

    def test_ready_state_survives_restart_and_can_be_applied(self):
        """待重启状态落盘：重启 App 后仍能一键应用，不必重新下载。"""
        payload, checksum = self._payload()
        staged = self._downloaded_path()
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(payload)
        self.manager._mark_ready(self._latest(checksum), staged)

        revived = UpdateManager(self.app_dir, self.data_dir)
        status = revived.status()
        self.assertTrue(status["can_apply"], "重启后必须还认得这份已下载的更新")
        self.assertEqual(status["ready"]["version"], "2.1.7-beta")
        self.assertEqual(status["ready"]["commit"], COMMIT_A)

        launched = []
        revived._launch_replacer = lambda downloaded: launched.append(downloaded)
        result = revived.apply_ready()
        self.assertEqual(revived.phase, "restarting")
        # 落盘标记只存文件名，恢复时用 `self.data_dir.resolve() / "update" / name` 重建路径；
        # 而本次临时根是**未 resolve** 的（CI runner 的 TEMP 带 8.3 短名，如 RUNNER~1），
        # 直接比会「本地全绿 CI 红」⇒ 按维护说明 §六③ 的口径，测试侧也 resolve 后再比。
        # 复现自检：.venv\Scripts\python.exe verify\ci_short_path_check.py tests.test_updater
        self.assertEqual(launched, [staged.resolve()])
        self.assertTrue((self.data_dir / "update" / "pending-update.json").is_file())
        self.assertFalse(result["can_apply"], "重启中不该再提供二次应用入口")

    def test_corrupt_ready_marker_is_discarded_on_startup(self):
        """哈希对不上的「待重启」必须静默丢弃：宁可让用户重下，也不能装一个坏包。"""
        payload, _checksum = self._payload()
        staged = self._downloaded_path()
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(payload)
        (self.data_dir / "update" / READY_MARKER).write_text(
            json.dumps({
                "version": "2.1.7-beta",
                "commit": COMMIT_A,
                "sha256": "f" * 64,
                "file": staged.name,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        revived = UpdateManager(self.app_dir, self.data_dir)
        self.assertFalse(revived.status()["can_apply"])
        self.assertFalse((self.data_dir / "update" / READY_MARKER).exists())
        self.assertFalse(staged.exists())

    def test_discard_ready_removes_marker_and_installer(self):
        payload, checksum = self._payload()
        staged = self._downloaded_path()
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(payload)
        self.manager._mark_ready(self._latest(checksum), staged)

        status = self.manager.discard_ready()

        self.assertFalse(status["can_apply"])
        self.assertFalse(staged.exists())
        self.assertFalse((self.data_dir / "update" / READY_MARKER).exists())


class UpdateProxyOverrideTests(unittest.TestCase):
    """「更新代理」scoped 覆盖项：只影响更新链路，且不改动全局网络策略。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.app_dir = root / "app"
        self.data_dir = root / "data"
        self.app_dir.mkdir()
        self.data_dir.mkdir()
        self.manager = UpdateManager(self.app_dir, self.data_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_is_inherit(self):
        self.assertEqual((self.manager.proxy_override or {}).get("mode"), None)
        self.assertEqual(self.manager.status()["proxy"]["mode"], "inherit")

    def test_configure_proxy_normalizes_unknown_mode_to_inherit(self):
        self.manager.configure_proxy({"mode": "nonsense", "url": "http://127.0.0.1:7890"})
        self.assertEqual(self.manager.proxy_override["mode"], "inherit")

    def test_status_exposes_effective_route(self):
        self.manager.configure_proxy({"mode": "manual", "url": "http://127.0.0.1:7890"})
        proxy = self.manager.status()["proxy"]
        self.assertEqual(proxy["mode"], "manual")
        self.assertIn("127.0.0.1:7890", proxy["note"])
        self.manager.configure_proxy({"mode": "direct"})
        self.assertIn("直连", self.manager.status()["proxy"]["note"])
        # note 是给前端拼「本次更新走：…」用的裸描述，不得自带「更新链路：」前缀，
        # 否则界面会渲染成「本次更新走：更新链路：…」（2026-10-02 截图核对时抓到的文案重复）。
        self.assertFalse(self.manager.status()["proxy"]["note"].startswith("更新链路："))

    def test_error_message_reports_update_route(self):
        self.manager.configure_proxy({"mode": "system"})
        message = self.manager._with_proxy_context("安装更新失败：x")
        self.assertIn("更新链路：系统代理", message)


class ScopedOpenerTests(unittest.TestCase):
    """net_io.open_scoped：inherit 回落全局，其余三态各自独立，manual 空地址退系统代理。"""

    def test_inherit_falls_back_to_global_open(self):
        from naiba import net as net_io

        calls = []
        original = net_io.registry.open
        net_io.registry.open = lambda request, timeout=None: calls.append(timeout) or "sentinel"
        try:
            result = net_io.registry.open_scoped("https://api.github.com/x", override={"mode": "inherit"})
        finally:
            net_io.registry.open = original
        self.assertEqual(result, "sentinel")
        self.assertEqual(calls, [None])

    def test_manual_without_url_falls_back_to_system(self):
        from naiba import net as net_io

        normalized = net_io.registry.normalize_override({"mode": "manual", "url": ""})
        self.assertEqual(normalized["mode"], "system")

    def test_invalid_override_is_treated_as_inherit(self):
        from naiba import net as net_io

        self.assertEqual(net_io.registry.normalize_override(None)["mode"], "inherit")
        self.assertEqual(net_io.registry.normalize_override({"mode": "  "})["mode"], "inherit")
        self.assertEqual(net_io.registry.normalize_override({"mode": "MANUAL", "url": "127.0.0.1:7890"})["mode"], "manual")

    def test_scoped_opener_is_cached_per_mode_and_url(self):
        from naiba import net as net_io

        registry = net_io.NetIO()
        first = registry._scoped_opener("system", "")
        second = registry._scoped_opener("system", "")
        self.assertIs(first, second, "同一策略必须复用同一个 opener（别每次请求都重建）")
        registry.configure({"enabled": False, "url": ""})
        self.assertIsNot(registry._scoped_opener("system", ""), first,
                          "全局配置变更必须让 scoped 缓存整体失效")


class SourceModeUpdateTests(unittest.TestCase):
    """未打包（源码）模式回归：git 调用全部打桩。"""

    def setUp(self):
        self._had_frozen = hasattr(sys, "frozen")
        self._old_frozen = getattr(sys, "frozen", None)
        if self._had_frozen:
            delattr(sys, "frozen")
        self._tmp = tempfile.TemporaryDirectory()
        self.app_dir = Path(self._tmp.name) / "repo"
        self.app_dir.mkdir()
        (self.app_dir / ".git").mkdir()
        self.data_dir = Path(self._tmp.name) / "data"
        self.data_dir.mkdir()
        self.manager = UpdateManager(self.app_dir, self.data_dir)

    def tearDown(self):
        self._tmp.cleanup()
        if self._had_frozen:
            sys.frozen = self._old_frozen

    def test_source_mode_check_regression(self):
        def fake_git(*args, **_kwargs):
            command = args[0] if args else ""
            if command == "remote":
                return f"https://github.com/{REPOSITORY}.git"
            if command == "fetch":
                return ""
            if command == "rev-parse":
                return "e" * 40 if "origin/master" in args else "d" * 40
            if command == "rev-list":
                return "0\t1"
            if command == "status":
                return ""
            return ""

        self.manager._run_git = fake_git
        status = self.manager.check(force=True)
        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["latest_version"], "origin/master")
        self.assertEqual(self.manager.latest["commit"], "e" * 40)

    def test_source_mode_unsupported_directory_error(self):
        manager = UpdateManager(self.data_dir, self.data_dir)
        status = manager.check(force=True)
        self.assertEqual(status["phase"], "error")
        self.assertIn("不是受支持的 naiba-chat Git 仓库", status["error"])

    def test_source_repository_accepts_both_repository_names(self):
        """仓库 2026-09-21 更名为 cat-chat，旧名由 GitHub 301 重定向继续可用。

        remote 认两个名字缺一不可：老克隆仍是旧名、新克隆是新名，只认一个就会让另一半
        用户在源码模式点「检查更新」时被误报成「不是受支持的仓库」。
        """
        accepted = (
            "https://github.com/yc883123/naiba-chat.git",
            "https://github.com/yc883123/cat-chat.git",
            "https://github.com/yc883123/cat-chat",           # 不带 .git 后缀
            "git@github.com:yc883123/cat-chat.git",           # SSH 形态
            "https://github.com/YC883123/CAT-CHAT.git",       # 大小写不敏感
        )
        for remote in accepted:
            with self.subTest(remote=remote):
                self.manager._run_git = lambda *args, _r=remote, **_kwargs: _r
                self.assertTrue(self.manager._source_repository(), remote)

    def test_source_repository_rejects_lookalikes_and_forks(self):
        """判据是**整个路径段**相等，不是「含 naiba-chat 字样」——否则别人 fork 一个同前缀仓库就能冒充。"""
        rejected = (
            "https://github.com/yc883123/naiba-chat-backup.git",
            "https://github.com/yc883123/cat-chat-2.git",
            "https://github.com/someone-else/naiba-chat.git",
            "https://github.com/someone-else/cat-chat.git",
            "https://gitlab.com/yc883123/cat-chat.git",
            "https://github.com/yc883123/other.git",
        )
        for remote in rejected:
            with self.subTest(remote=remote):
                self.manager._run_git = lambda *args, _r=remote, **_kwargs: _r
                self.assertFalse(self.manager._source_repository(), remote)

    def test_source_repository_requires_git_dir_and_source_mode(self):
        """没有 .git（拷来的目录）或已是冻结版时，一律不认作源码仓库。"""
        self.manager._run_git = lambda *_args, **_kwargs: "https://github.com/yc883123/cat-chat.git"
        self.assertTrue(self.manager._source_repository())
        (self.app_dir / ".git").rmdir()
        self.assertFalse(self.manager._source_repository())

        (self.app_dir / ".git").mkdir()
        sys.frozen = True
        try:
            self.assertFalse(self.manager._source_repository())
        finally:
            delattr(sys, "frozen")


if __name__ == "__main__":
    unittest.main()
