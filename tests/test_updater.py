# -*- coding: utf-8 -*-
"""updater.py 更新流程的标准库单元测试（无网络，全部通过桩函数拦截请求）。

覆盖：磁盘缓存读回/回退、latest 静态直连兜底、合成 latest 条目、
错误提示分级、清单校验失败直连失败不降级，以及正常 API 路径与源码模式回归。
"""

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

from naiba.updater import EXECUTABLE_ASSET, LATEST_TAG, MANIFEST_ASSET, REPOSITORY, UpdateManager  # noqa: E402


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
        def dispatch(url):
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
        # 基础文案保持不变，仅在末尾附带当前实际生效的外部请求模式（用于定位代理问题）。
        self.assertTrue(status["error"].startswith("无法连接更新服务器，请检查网络后重试"))
        self.assertIn("外部请求：", status["error"])

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

    # ---------- 安装入口（latest 合成条目） ----------

    @unittest.skipUnless(os.name == "nt", "一键安装路径仅适用于 Windows")
    def test_start_install_latest_tag_uses_static_download(self):
        self.set_build("2.1.6-beta")
        self.manager.releases = [
            {"tag": LATEST_TAG, "version": "2.1.7-beta", "published_at": "",
             "release_url": f"https://github.com/{REPOSITORY}/releases/latest",
             "release_notes": ["说明"], "installable": True, "current": False}
        ]
        self.manager._request_json = self.stub.route(
            latest=manifest_payload(), tag=lambda url: manifest_payload()
        )
        self.manager._download = lambda latest: self.data_dir / "naiba-chat.exe"
        self.manager._launch_replacer = lambda downloaded: None
        self.manager.start_install(LATEST_TAG)
        deadline = time.time() + 5
        while self.manager.phase != "restarting" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.manager.phase, "restarting")
        self.assertTrue(any(url.startswith(f"{LATEST_DL_BASE}/") for url in self.stub.calls))
        self.assertFalse(any(url.startswith(API_RELEASES_URL) for url in self.stub.calls))


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
