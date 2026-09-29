# -*- coding: utf-8 -*-
"""缓存清理 scope 化 + 批量引用判定 + 异步化（2026-09-29 计划 S / A–F）。

守门目标（都是"静默出错"的高危面）：

- **uploads / generated 各自独立**：一个目录超限不得删另一个目录里的文件；
  合并成单池正是"generated 撑爆额度 → 挤删最旧的用户上传图"的成因。
- **批量引用判定与逐文件口径一致**：``referenced_cache_paths`` 一次扫表的结果，
  与 ``upload_path_referenced`` 逐文件 LIKE 的结论必须逐条对齐；漏判方向只能是"多留"。
- **unreachable / referenced_bytes 必须来自完整扫描**：不得用"连续 K 组不可删就 break"
  的近似规则——那会漏掉后面的可删组，还会把部分结果当完整结果上报。
- **自动清理异步化**：触发不得阻塞调用方；占锁时如实返回 busy，不假装清理完成。
- **存量配置的 generated 阈值继承旧 auto_clean_limit_mb**（含 0=关闭）。
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba import app as app_module  # noqa: E402
from naiba.app import NaibaChatApp  # noqa: E402
from naiba.config import ConfigStore  # noqa: E402
from naiba.core.paths import normalized_path_key  # noqa: E402
from naiba.paths import PathContext  # noqa: E402
from naiba.storage.media import (  # noqa: E402
    CACHE_SCOPES,
    SCOPE_GENERATED,
    SCOPE_UPLOADS,
    _clean_uploads_cache,
    cache_scope_bytes,
    cache_scope_dir,
)
from naiba.storage.media_collect import MediaCollector  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


def _png_bytes(color=(10, 120, 200), size=(24, 16)) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _age(path: Path, days: int = 2) -> None:
    stamp = (datetime.now() - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def _blob(size: int, seed: bytes = b"x") -> bytes:
    return (seed * ((size // len(seed)) + 1))[:size]


class _FakeConfig:
    """MediaCollector 只用到 resolve_data_dir() 与 data['imaging']。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self.data: dict = {"imaging": {}}

    def resolve_data_dir(self) -> Path:
        return self._data_dir


class BatchReferenceScanTests(unittest.TestCase):
    """一次扫表拿到的引用集合，必须与逐文件 LIKE 的结论一致。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_scope_ref_")).resolve()
        self.storage = ChatStorage(self.tmp / "chat.db")
        conv = self.storage.create_conversation("ref", "引用测试")
        self.conv_id = str(conv.get("id") or "")
        self.uploads = self.tmp / "uploads" / "2026-01-01"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.generated = self.tmp / "generated" / "2026-01-01"
        self.generated.mkdir(parents=True, exist_ok=True)

    def _reference(self, path: Path, *, stored: str = "") -> None:
        """建一条引用该文件的 run（metadata 走 json.dumps(ensure_ascii=False)）。

        每次引用换一个新会话：同一会话同时只允许一个 in-flight run
        （``create_chat_run`` 会以 ACTIVE_RUN 拒绝第二个）。
        """
        entry = {"name": path.name, "path": stored or str(path), "size": 10, "thumb_path": ""}
        conv = self.storage.create_conversation(f"ref-{path.stem}", f"引用 {path.name}")
        self.storage.create_chat_run(
            str(conv.get("id") or ""),
            "总结",
            [entry],
            {"id": "general", "name": "通用 Agent", "system_prompt": "", "skill_ids": []},
            {"attachments": [entry]},
            "craft",
        )

    def test_batch_scan_agrees_with_single_file_probe(self) -> None:
        referenced = self.uploads / "used.png"
        referenced.write_bytes(_png_bytes())
        idle = self.uploads / "idle.png"
        idle.write_bytes(_png_bytes((1, 2, 3)))
        gen = self.generated / "out.png"
        gen.write_bytes(_png_bytes((4, 5, 6)))
        self._reference(referenced)
        self._reference(gen)

        found = self.storage.referenced_cache_paths([self.tmp / "uploads", self.tmp / "generated"])

        for path in (referenced, idle, gen):
            single = self.storage.upload_path_referenced(path)
            batched = normalized_path_key(path) in found
            self.assertEqual(
                batched,
                single,
                f"批量判定与逐文件判定必须一致：{path.name} batch={batched} single={single}",
            )
        # 前提断言：夹具真的造出了"一个被引用、一个没被引用"，否则上面是空转。
        self.assertTrue(self.storage.upload_path_referenced(referenced))
        self.assertFalse(self.storage.upload_path_referenced(idle))

    def test_batch_scan_finds_forward_slash_records(self) -> None:
        """少数记录里的路径以正斜杠落库（跨平台复制/手改过 metadata）。"""
        target = self.uploads / "slashed.png"
        target.write_bytes(_png_bytes())
        self._reference(target, stored=str(target).replace("\\", "/"))

        found = self.storage.referenced_cache_paths([self.tmp / "uploads"])

        self.assertIn(
            normalized_path_key(target),
            found,
            "正斜杠形态的历史路径也必须被批量判定认出来（否则删除前复核才拦得住，代价是每文件一次全表扫）",
        )

    def test_batch_scan_ignores_paths_outside_roots(self) -> None:
        outside = self.tmp / "workspace" / "note.png"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(_png_bytes())
        self._reference(outside)

        found = self.storage.referenced_cache_paths([self.tmp / "uploads", self.tmp / "generated"])

        self.assertNotIn(normalized_path_key(outside), found, "只找缓存树内的路径")

    def test_batch_scan_miss_is_caught_by_pre_delete_recheck(self) -> None:
        """批量提取抽不到、但 LIKE 复核能命中的记录：删除前复核必须兜住（红线：不得误删）。

        构造方式：把目录部分改成小写落库。批量判定用 C 级 ``str.find`` 按**转义后的
        目录前缀**匹配，大小写敏感 ⇒ 抽不到；而 SQLite 的 LIKE 对 ASCII 大小写不敏感
        ⇒ 单文件复核能命中。这正是"双保险"存在的理由。
        """
        target = self.uploads / "MixedCase.png"
        target.write_bytes(_png_bytes())
        lowered = str(target).lower()
        if lowered == str(target):  # 前提不成立就别假装验到了
            self.skipTest("临时目录路径本身全小写，无法构造大小写差异")
        self._reference(target, stored=lowered)

        batch = self.storage.referenced_cache_paths([self.tmp / "uploads"])
        self.assertNotIn(
            normalized_path_key(target), batch,
            "前提：批量提取应当抽不到（否则这条用例验的不是复核兜底）",
        )
        self.assertTrue(
            self.storage.upload_path_referenced(target),
            "前提：单文件 LIKE 复核应当能命中",
        )

        result = _clean_uploads_cache(
            limit=1,
            data_dir=self.tmp,
            referenced_checker=self.storage.upload_path_referenced,
            grace_seconds=0,
            scope=SCOPE_UPLOADS,
            referenced_keys=batch,
        )
        self.assertTrue(target.is_file(), "批量漏判时删除前复核必须把引用文件拦下来")
        self.assertGreaterEqual(result["skipped_referenced"], 1)

    def test_batch_scan_survives_broken_json_row(self) -> None:
        """某行 metadata 损坏不得让整批判定失败（坏行只是进不了集合，删除前还有复核兜底）。"""
        referenced = self.uploads / "ok.png"
        referenced.write_bytes(_png_bytes())
        self._reference(referenced)
        with self.storage._connect() as db:  # noqa: SLF001 - 故意写坏一行 JSON
            db.execute("INSERT INTO messages (conversation_id, role, content, metadata, created_at) "
                       "VALUES (?, 'user', 'broken', '{not json', ?)",
                       (self.conv_id, int(time.time())))
            db.commit()

        found = self.storage.referenced_cache_paths([self.tmp / "uploads"])

        self.assertIn(normalized_path_key(referenced), found)


class ScopeIsolationTests(unittest.TestCase):
    """两个目录各删各的：一个超限不得牵连另一个。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_scope_iso_")).resolve()
        self.data_dir = self.tmp / "data"
        self.uploads = self.data_dir / "uploads" / "2026-01-01"
        self.generated = self.data_dir / "generated" / "2026-01-01"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.generated.mkdir(parents=True, exist_ok=True)
        self.up_old = self.uploads / "old_upload.bin"
        self.up_old.write_bytes(_blob(4096, b"a"))
        self.gen_old = self.generated / "old_product.bin"
        self.gen_old.write_bytes(_blob(4096, b"b"))
        for path in (self.up_old, self.gen_old):
            _age(path)

    def _clean(self, scope: str) -> dict:
        return _clean_uploads_cache(
            limit=1,
            data_dir=self.data_dir,
            referenced_checker=lambda path: False,
            grace_seconds=0,
            scope=scope,
        )

    def test_uploads_clean_leaves_generated_alone(self) -> None:
        result = self._clean(SCOPE_UPLOADS)
        self.assertGreaterEqual(result["removed"], 1, "前提：uploads 真的清掉了东西")
        self.assertFalse(self.up_old.exists())
        self.assertTrue(self.gen_old.is_file(), "清 uploads 不得动 generated")

    def test_generated_clean_leaves_uploads_alone(self) -> None:
        result = self._clean(SCOPE_GENERATED)
        self.assertGreaterEqual(result["removed"], 1, "前提：generated 真的清掉了东西")
        self.assertFalse(self.gen_old.exists())
        self.assertTrue(self.up_old.is_file(), "清 generated 不得动 uploads")

    def test_scope_none_cleans_both_independently(self) -> None:
        result = _clean_uploads_cache(
            limit=1,
            data_dir=self.data_dir,
            referenced_checker=lambda path: False,
            grace_seconds=0,
        )
        self.assertEqual(set(result["scopes"]), set(CACHE_SCOPES), "必须给出分目录明细")
        self.assertFalse(self.up_old.exists())
        self.assertFalse(self.gen_old.exists())
        self.assertEqual(result["size"], 0)

    def test_thumbnail_travels_with_its_main_file(self) -> None:
        """主图与其缩略图是一组：删除同生，保留同死。"""
        main = self.uploads / "shot.png"
        main.write_bytes(_png_bytes())
        thumb = main.with_name(main.stem + "_thumb.webp")
        thumb.write_bytes(b"thumb-bytes")
        _age(main)
        _age(thumb)

        result = _clean_uploads_cache(
            limit=1,
            data_dir=self.data_dir,
            referenced_checker=lambda path: False,
            grace_seconds=0,
            scope=SCOPE_UPLOADS,
        )
        self.assertFalse(main.exists())
        self.assertFalse(thumb.exists(), "缩略图必须随主图一起删，否则留下孤儿文件")
        # setUp 里还有一个 old_upload.bin 也超限，故至少删掉"主图 + 缩略图"这两个文件。
        self.assertGreaterEqual(result["removed"], 2)

    def test_same_relative_path_in_both_scopes_is_not_confused(self) -> None:
        """两 scope 下同相对路径（同一天同名）不得被当成同一组。"""
        up_same = self.uploads / "same.bin"
        gen_same = self.generated / "same.bin"
        up_same.write_bytes(_blob(4096, b"u"))
        gen_same.write_bytes(_blob(4096, b"g"))
        _age(up_same)
        _age(gen_same)

        result = _clean_uploads_cache(
            limit=1,
            data_dir=self.data_dir,
            referenced_checker=lambda path: False,
            grace_seconds=0,
            scope=SCOPE_GENERATED,
        )
        self.assertFalse(gen_same.exists())
        self.assertTrue(up_same.is_file(), "同名不同 scope 的文件不得被牵连")
        self.assertEqual(result["scope"], SCOPE_GENERATED)

    def test_cache_scope_bytes_and_dir_are_per_scope(self) -> None:
        self.assertEqual(cache_scope_dir(self.data_dir, SCOPE_UPLOADS), self.uploads.parent.resolve())
        self.assertNotEqual(
            cache_scope_dir(self.data_dir, SCOPE_UPLOADS),
            cache_scope_dir(self.data_dir, SCOPE_GENERATED),
        )
        self.assertGreater(cache_scope_bytes(self.data_dir, SCOPE_UPLOADS), 0)
        self.assertGreater(cache_scope_bytes(self.data_dir, SCOPE_GENERATED), 0)
        with self.assertRaises(ValueError):
            cache_scope_dir(self.data_dir, "nope")


class UnreachableReportingTests(unittest.TestCase):
    """unreachable / referenced_bytes 必须是完整扫描的结果。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_scope_unreach_")).resolve()
        self.data_dir = self.tmp / "data"
        self.uploads = self.data_dir / "uploads" / "2026-01-01"
        self.uploads.mkdir(parents=True, exist_ok=True)

    def _make(self, name: str, size: int, days: int) -> Path:
        path = self.uploads / name
        path.write_bytes(_blob(size, name.encode("utf-8")[:1] or b"z"))
        _age(path, days=days)
        return path

    def test_referenced_bytes_and_unreachable_reported(self) -> None:
        keep = self._make("keep.bin", 4096, 5)
        drop = self._make("drop.bin", 4096, 4)
        referenced = {normalized_path_key(keep)}

        result = _clean_uploads_cache(
            limit=1,
            data_dir=self.data_dir,
            referenced_checker=lambda path: normalized_path_key(path) in referenced,
            grace_seconds=0,
            scope=SCOPE_UPLOADS,
            referenced_keys=referenced,
        )

        self.assertFalse(drop.exists())
        self.assertTrue(keep.is_file(), "被引用文件永不删除")
        self.assertGreaterEqual(result["skipped_referenced"], 1)
        self.assertEqual(result["referenced_bytes"], keep.stat().st_size)
        self.assertTrue(result["unreachable"], "被引用缓存本身超阈值时必须如实上报不可达")

    def test_no_early_break_after_consecutive_referenced_groups(self) -> None:
        """5 个连续被引用组之后仍有一个可删组：必须继续扫到并删掉。

        旧口径用"连续 K 组不可删就 break"做近似——那会漏掉后面的可删组，
        还会把部分结果当完整结果上报（unreachable 假阳性）。
        """
        kept = [self._make(f"keep{i}.bin", 4096, 10 - i) for i in range(5)]
        newest = self._make("free.bin", 4096, 0)
        referenced = {normalized_path_key(path) for path in kept}

        result = _clean_uploads_cache(
            limit=1,
            data_dir=self.data_dir,
            referenced_checker=lambda path: normalized_path_key(path) in referenced,
            grace_seconds=0,
            scope=SCOPE_UPLOADS,
            referenced_keys=referenced,
        )

        self.assertFalse(newest.exists(), "连续 5 个被引用组之后的自由组必须仍被清理")
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["skipped_referenced"], 5)
        self.assertEqual(result["referenced_bytes"], sum(p.stat().st_size for p in kept))
        self.assertTrue(result["unreachable"])

    def test_reachable_when_under_limit_after_clean(self) -> None:
        drop = self._make("drop.bin", 4096, 4)
        newer = self._make("newer.bin", 4096, 1)
        # limit 只容得下一组：删掉最旧那组后即降到阈值以下。
        result = _clean_uploads_cache(
            limit=4097,
            data_dir=self.data_dir,
            referenced_checker=lambda path: False,
            grace_seconds=0,
            scope=SCOPE_UPLOADS,
        )
        self.assertFalse(drop.exists())
        self.assertTrue(newer.is_file(), "降到阈值以下后必须停止删除（不是删光）")
        self.assertEqual(result["removed"], 1)
        self.assertFalse(result["unreachable"], "降到阈值以下就不该报不可达")

    def test_grace_window_and_protect_paths_keep_files(self) -> None:
        fresh = self._make("fresh.bin", 4096, 0)
        old = self._make("old.bin", 4096, 4)
        result = _clean_uploads_cache(
            limit=1,
            data_dir=self.data_dir,
            referenced_checker=lambda path: False,
            protect_paths=[old],
            grace_seconds=15 * 60,
            scope=SCOPE_UPLOADS,
        )
        self.assertTrue(fresh.is_file(), "保护窗口内的组不参与自动删除")
        self.assertTrue(old.is_file(), "protect_paths 里的组无条件保留")
        self.assertEqual(result["removed"], 0)
        self.assertGreaterEqual(result["skipped_recent"], 1)
        self.assertGreaterEqual(result["skipped_protected"], 1)


class AsyncCleanTests(unittest.TestCase):
    """A：自动清理异步化——触发不阻塞调用方，占锁如实报 busy。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_scope_async_")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.app = NaibaChatApp(paths=PathContext.local(root, root / "config.json"))
        self.data_dir = self.app.paths.data_dir
        self.uploads = self.data_dir / "uploads" / "2026-01-01"
        self.uploads.mkdir(parents=True, exist_ok=True)
        # 阈值单位 MB，最小只能设 1 ⇒ 夹具必须 > 1MB，否则触发条件本身就不成立。
        self.app.config.update_settings({"imaging": {"auto_clean_limit_mb": 1}})
        (self.uploads / "big.bin").write_bytes(_blob(2 * 1024 * 1024, b"u"))
        self.addCleanup(self._drain_lock)

    def _drain_lock(self) -> None:
        deadline = time.monotonic() + 5
        while app_module._CACHE_CLEAN_LOCK.locked() and time.monotonic() < deadline:  # noqa: SLF001
            time.sleep(0.02)
        if app_module._CACHE_CLEAN_LOCK.locked():  # noqa: SLF001 - 别把锁泄漏给其它用例
            app_module._CACHE_CLEAN_LOCK.release()  # noqa: SLF001

    def test_trigger_returns_immediately_and_runs_in_background(self) -> None:
        done = threading.Event()
        calls: list[str] = []
        real_once = self.app._run_cache_clean_once  # noqa: SLF001

        def slow_once(scope, protect_paths, trigger):
            calls.append(scope)
            time.sleep(0.4)
            done.set()
            return real_once(scope, protect_paths, trigger)

        self.app._run_cache_clean_once = slow_once  # noqa: SLF001
        started = time.monotonic()
        triggered = self.app._trigger_cache_clean(SCOPE_UPLOADS)
        elapsed = time.monotonic() - started

        self.assertTrue(triggered, "超阈值时必须触发（登记）清理")
        self.assertLess(elapsed, 0.25, "触发必须立刻返回，不得等清理跑完（A：异步）")
        self.assertTrue(done.wait(5), "后台线程必须真的把清理跑起来")
        self.assertEqual(calls, [SCOPE_UPLOADS], "上传只触发 uploads 的清理，不蹭 generated")

    def test_upload_response_returns_before_clean_finishes(self) -> None:
        """真实上传入口：落盘后立刻回响应，响应带 clean_pending。"""
        done = threading.Event()
        real_once = self.app._run_cache_clean_once  # noqa: SLF001

        def slow_once(scope, protect_paths, trigger):
            time.sleep(0.4)
            done.set()
            return real_once(scope, protect_paths, trigger)

        self.app._run_cache_clean_once = slow_once  # noqa: SLF001
        spool = self.data_dir / "spool.part"
        spool.write_bytes(_png_bytes())
        started = time.monotonic()
        payload, status = self.app._upload_spooled(str(spool), "shot.png")  # noqa: SLF001
        elapsed = time.monotonic() - started

        self.assertEqual(status, 200)
        self.assertTrue(payload.get("path"), "上传必须真的落盘成功")
        self.assertTrue(payload.get("clean_pending"), "启动了/登记了后台清理时响应要如实标注")
        self.assertLess(elapsed, 0.25, "上传响应不得等后台清理跑完（A：异步）")
        self.assertTrue(done.wait(5), "后台清理必须真的跑起来")
        self.assertTrue(Path(payload["path"]).is_file(), "刚落盘的附件不得被本轮清理删掉")

    def test_pending_scope_is_re_run_after_current_round(self) -> None:
        """占锁期间登记的另一 scope，必须在当前轮跑完后被补跑（不能丢最后一次触发）。"""
        self.app.config.update_settings({"imaging": {"generated_clean_limit_mb": 1}})
        generated = self.data_dir / "generated" / "2026-01-01"
        generated.mkdir(parents=True, exist_ok=True)
        (generated / "big.bin").write_bytes(_blob(2 * 1024 * 1024, b"g"))

        seen: list[str] = []
        gate = threading.Event()

        def slow_once(scope, protect_paths, trigger):
            seen.append(scope)
            if scope == SCOPE_UPLOADS:
                gate.wait(5)
            return {
                "scope": scope, "trigger": trigger, "removed": 0, "freed": 0, "size": 0,
                "skipped_recent": 0, "skipped_protected": 0, "skipped_referenced": 0,
                "referenced_bytes": 0, "unreachable": False,
            }

        self.app._run_cache_clean_once = slow_once  # noqa: SLF001
        self.assertTrue(self.app._trigger_cache_clean(SCOPE_UPLOADS))  # noqa: SLF001
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(seen, [SCOPE_UPLOADS], "前提：第一轮已进入 uploads 清理")

        # 此刻后台线程正持锁：这次触发只能被登记。
        self.assertTrue(self.app._trigger_cache_clean(SCOPE_GENERATED))  # noqa: SLF001
        gate.set()

        deadline = time.monotonic() + 5
        while SCOPE_GENERATED not in seen and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(SCOPE_GENERATED, seen, "占锁期间登记的另一 scope 必须被补跑")

    def test_trigger_is_noop_when_under_limit(self) -> None:
        self.app.config.update_settings({"imaging": {"auto_clean_limit_mb": 4096}})
        self.assertFalse(self.app._trigger_cache_clean(SCOPE_UPLOADS))  # noqa: SLF001

    def test_trigger_is_noop_when_disabled(self) -> None:
        self.app.config.update_settings({"imaging": {"auto_clean_limit_mb": 0}})
        self.assertFalse(self.app._trigger_cache_clean(SCOPE_UPLOADS))  # noqa: SLF001

    def test_manual_clean_reports_busy_when_lock_held(self) -> None:
        self.assertTrue(app_module._CACHE_CLEAN_LOCK.acquire(blocking=False))  # noqa: SLF001
        try:
            result, status = self.app.api_clean_image_cache()
        finally:
            app_module._CACHE_CLEAN_LOCK.release()  # noqa: SLF001
        self.assertEqual(status, 200)
        self.assertTrue(result.get("busy"), "占锁时返回 busy，而不是假装清理完成")
        self.assertIn("稍后", str(result.get("message") or ""))

    def test_trigger_while_locked_is_registered_not_dropped(self) -> None:
        self.assertTrue(app_module._CACHE_CLEAN_LOCK.acquire(blocking=False))  # noqa: SLF001
        try:
            self.assertTrue(self.app._start_cache_clean(  # noqa: SLF001
                SCOPE_GENERATED, protect_paths=[str(self.uploads / "big.bin")]
            ))
            pending = self.app._cache_clean_pending  # noqa: SLF001
            self.assertIn(SCOPE_GENERATED, pending, "占锁期间的新触发必须按 scope 登记，不能丢弃")
            self.assertTrue(any("big.bin" in item for item in pending[SCOPE_GENERATED]))
        finally:
            app_module._CACHE_CLEAN_LOCK.release()  # noqa: SLF001
            with self.app._cache_clean_state_lock:  # noqa: SLF001
                self.app._cache_clean_pending.clear()  # noqa: SLF001

    def test_manual_clean_scope_validation(self) -> None:
        result, status = self.app.api_clean_image_cache("nope")
        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def test_stats_payload_is_per_scope(self) -> None:
        payload = self.app.api_imaging_stats()
        for key in ("image_cache_bytes", "uploads_cache_bytes", "generated_cache_bytes"):
            self.assertIn(key, payload)
        self.assertEqual(
            payload["image_cache_bytes"],
            payload["uploads_cache_bytes"] + payload["generated_cache_bytes"],
        )
        self.assertIn("cache_clean", payload)
        self.assertGreaterEqual(payload["uploads_cache_bytes"], 2 * 1024 * 1024)


class GeneratedWriteTriggerTests(unittest.TestCase):
    """generated 自己的触发点：产物落盘后回调宿主（不再蹭上传便车）。"""

    def test_media_collector_notifies_after_generated_write(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="naiba_scope_gen_")).resolve()
        data_dir = tmp / "data"
        source = tmp / "product.png"
        source.write_bytes(_png_bytes())
        fired: list[int] = []

        collector = MediaCollector(
            _FakeConfig(data_dir), None, on_cached=lambda: fired.append(1)
        )
        result = collector.collect(
            {"success": True, "result": json.dumps({"path": str(source)}), "tool": "fake"},
            {"policy": "inline", "extract": "scan"},
        )

        self.assertEqual(len(result["media"]), 1, "前提：产物真的被收进了 generated")
        cached = Path(result["media"][0]["source"])
        self.assertTrue(cached.is_file())
        self.assertTrue(str(cached).startswith(str((data_dir / "generated").resolve())))
        self.assertEqual(len(fired), 1, "写盘成功后必须通知一次宿主")

    def test_callback_failure_does_not_break_collection(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="naiba_scope_gen2_")).resolve()
        data_dir = tmp / "data"
        source = tmp / "product.png"
        source.write_bytes(_png_bytes())

        def boom() -> None:
            raise RuntimeError("宿主回调炸了")

        collector = MediaCollector(_FakeConfig(data_dir), None, on_cached=boom)
        result = collector.collect(
            {"success": True, "result": json.dumps({"path": str(source)}), "tool": "fake"},
            {"policy": "inline", "extract": "scan"},
        )
        self.assertEqual(len(result["media"]), 1, "回调异常不得打断媒体采集")


class ConfigMigrationTests(unittest.TestCase):
    """存量配置的 generated 阈值继承旧 auto_clean_limit_mb（含 0=关闭）。"""

    def _store(self, payload: dict | None) -> ConfigStore:
        tmp = Path(tempfile.mkdtemp(prefix="naiba_scope_cfg_")).resolve()
        path = tmp / "config.json"
        if payload is not None:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return ConfigStore(path)

    def test_fresh_install_uses_new_default(self) -> None:
        store = self._store(None)
        self.assertEqual(store.data["imaging"]["auto_clean_limit_mb"], 256)
        self.assertEqual(store.data["imaging"]["generated_clean_limit_mb"], 512)

    def test_legacy_config_inherits_auto_limit(self) -> None:
        store = self._store({"imaging": {"auto_clean_limit_mb": 1024}})
        self.assertEqual(store.data["imaging"]["auto_clean_limit_mb"], 1024)
        self.assertEqual(store.data["imaging"]["generated_clean_limit_mb"], 1024)

    def test_legacy_disabled_inherits_zero(self) -> None:
        store = self._store({"imaging": {"auto_clean_limit_mb": 0}})
        self.assertEqual(store.data["imaging"]["generated_clean_limit_mb"], 0,
                         "关掉自动清理的用户升级后不得突然开始删生成产物")

    def test_explicit_generated_value_is_preserved(self) -> None:
        store = self._store({"imaging": {"auto_clean_limit_mb": 256,
                                        "generated_clean_limit_mb": 64}})
        self.assertEqual(store.data["imaging"]["generated_clean_limit_mb"], 64)

    def test_inheritance_is_idempotent_across_reload(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="naiba_scope_cfg2_")).resolve()
        path = tmp / "config.json"
        path.write_text(json.dumps({"imaging": {"auto_clean_limit_mb": 0}}), encoding="utf-8")
        first = ConfigStore(path)
        first.save()
        second = ConfigStore(path)
        self.assertEqual(second.data["imaging"]["generated_clean_limit_mb"], 0)
        self.assertEqual(second.data["imaging"]["auto_clean_limit_mb"], 0)

    def test_update_settings_validates_range(self) -> None:
        store = self._store(None)
        with self.assertRaises(ValueError):
            store.update_settings({"imaging": {"generated_clean_limit_mb": -1}})
        with self.assertRaises(ValueError):
            store.update_settings({"imaging": {"generated_clean_limit_mb": 99999}})
        store.update_settings({"imaging": {"generated_clean_limit_mb": 128}})
        self.assertEqual(store.data["imaging"]["generated_clean_limit_mb"], 128)


class AppLimitTests(unittest.TestCase):
    """app 侧两个 scope 的阈值解析（含手动清理的"关自动≠清不了"回落）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_scope_limit_")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.app = NaibaChatApp(paths=PathContext.local(root, root / "config.json"))

    def test_limits_are_independent(self) -> None:
        self.app.config.update_settings(
            {"imaging": {"auto_clean_limit_mb": 10, "generated_clean_limit_mb": 20}}
        )
        self.assertEqual(self.app.cache_clean_limit_mb(SCOPE_UPLOADS), 10)
        self.assertEqual(self.app.cache_clean_limit_mb(SCOPE_GENERATED), 20)
        self.assertEqual(self.app.cache_clean_limit_bytes(SCOPE_GENERATED), 20 * 1024 * 1024)

    def test_manual_clean_falls_back_to_default_when_auto_disabled(self) -> None:
        """阈值 0（=关闭自动）时手动清理回落到该目录默认值，否则"关自动"= "手动也清不了"。"""
        self.app.config.update_settings(
            {"imaging": {"auto_clean_limit_mb": 0, "generated_clean_limit_mb": 0}}
        )
        self.assertEqual(self.app.cache_clean_limit_mb(SCOPE_UPLOADS), 0)
        result, status = self.app.api_clean_image_cache(SCOPE_UPLOADS)
        self.assertEqual(status, 200)
        self.assertFalse(result.get("busy"))
        self.assertEqual(result["scopes"][SCOPE_UPLOADS]["trigger"], "manual")


def _function_body(source: str, signature: str) -> str:
    """取顶层函数体（到下一个顶层 function / async function / export function 之前）。"""
    start = source.index(signature)
    rest = source[start:]
    match = re.search(r"\n(?:export )?(?:async )?function ", rest[1:])
    return rest if match is None else rest[: match.start() + 1]


class FrontendSplitTests(unittest.TestCase):
    """前端拆分与慢提示的静态守门（S 的设置页两行 + E 的上传慢提示）。

    纯源码断言：把「两个阈值输入框 / 两个清理按钮 / 各自传自己的 scope / 慢提示文案」
    钉死——防止后续重构把 scope 丢掉、或把两个按钮接回同一个调用。
    """

    def _read(self, rel: str) -> str:
        return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")

    def test_index_has_two_threshold_inputs_and_two_buttons(self) -> None:
        html = self._read("public/index.html")
        self.assertEqual(html.count('id="autoCleanLimitMb"'), 1)
        self.assertEqual(html.count('id="generatedCleanLimitMb"'), 1,
                         "生成产物缓存必须有自己的阈值输入框")
        self.assertEqual(html.count('id="cleanUploadsCache"'), 1)
        self.assertEqual(html.count('id="cleanGeneratedCache"'), 1,
                         "生成产物缓存必须有自己的清理按钮")
        self.assertEqual(html.count('id="uploadsCacheSize"'), 1)
        self.assertEqual(html.count('id="generatedCacheSize"'), 1)
        self.assertEqual(html.count('id="imageCacheSize"'), 1, "合计行保留，不破坏旧展示")

    def test_buttons_pass_their_own_scope(self) -> None:
        binder = self._read("public/js/15-bind-events.js")
        self.assertIn("cleanImageCache('uploads')", binder)
        self.assertIn("cleanImageCache('generated')", binder,
                      "产物按钮必须传 generated，不能与上传按钮共用一个调用")

    def test_clean_posts_scope_and_does_not_fake_success_when_busy(self) -> None:
        js = self._read("public/js/09-settings.js")
        body = _function_body(js, "export async function cleanImageCache")
        self.assertIn("/api/imaging/clean", body)
        self.assertIn("scope", body, "清理必须带 scope，两个按钮各清各的目录")
        self.assertIn("result.busy", body, "占锁时后端返回 busy，前端不得显示完成提示")

    def test_referenced_hint_uses_threshold_and_gives_a_way_out(self) -> None:
        js = self._read("public/js/09-settings.js")
        body = _function_body(js, "export function renderCacheCleanHints")
        self.assertIn("referenced_bytes", body)
        self.assertIn("auto_clean_limit_mb", body, "提示条件要按该目录阈值判断，不是有引用就提示")
        self.assertIn("generated_clean_limit_mb", body)
        self.assertIn("需删除对应会话才能腾出", body, "提示必须给出可操作的出路")

    def test_save_payload_carries_generated_limit(self) -> None:
        js = self._read("public/js/09-settings.js")
        body = _function_body(js, "export async function saveRuntimeSettings")
        self.assertIn("generated_clean_limit_mb", body,
                      "保存设置必须把新阈值一起提交，否则输入框改了不生效")

    def test_slow_upload_hint_is_wired(self) -> None:
        js = self._read("public/js/10-upload.js")
        self.assertIn("SLOW_UPLOAD_HINT_MS", js)
        status = _function_body(js, "function uploadStatusText")
        self.assertIn("服务器处理中", status,
                      "进度 100% 但响应未回时必须显示处理中，不得假装上传成功")
        self.assertIn("file.slow", status, "慢提示要区分「处理中」与「处理中·久等」两种文案")
        self.assertIn("暂时不要重复上传", status,
                      "超时文案必须劝用户别重复上传（否则会传出一堆重复文件）")
        self.assertIn("SLOW_UPLOAD_TIP", js, "完整话术要挂在 chip 的 title 上")


if __name__ == "__main__":
    unittest.main()
