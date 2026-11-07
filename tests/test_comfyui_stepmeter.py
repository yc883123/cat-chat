"""护栏：提取 ComfyUI 进度条（顶栏全局单条）。

口径（2026-11-07 用户拍板）：
- 数据源 = ComfyUI 官方内部端点 /internal/logs/raw 的 tqdm 采样进度行；
  HTTP 侧（/history /queue）执行期没有步级字段，纯轮询推算不可行。
- 归属由队列运行 ID、首次运行日志游标与 checkpoint.prompt_ids 共同约束；
- 新鲜期外 / 形状非法 / 端点缺失一律不带 step 字段，前端整块不渲染；
- 功能名只叫「提取 ComfyUI 进度条」，不提任何来源。
"""

from __future__ import annotations

import json
import io
import runpy
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from naiba.comfyui import client as comfyui_client

from naiba.comfyui import jobs as comfyui_jobs
from naiba.comfyui import stepmeter
from naiba.comfyui.runner import ComfyUIRunner


def _tqdm_line(value: int, max_steps: int, rate: str = "6.94it/s") -> str:
    head = f"{round(value * 100 / max_steps)}%" if value else "  0%"
    body = "█" * max(1, value)
    tail = f" [00:0{max(1, max_steps - value)}<00:1{max_steps - value}, {rate}]" if rate else " [00:01<00:02]"
    return f"{head}|{body:<24}| {value}/{max_steps}{tail}"


class StepMeterParseTests(unittest.TestCase):
    def test_standard_tqdm_row(self):
        parsed = stepmeter.parse_progress(_tqdm_line(2, 8))
        self.assertEqual(parsed, {"value": 2, "max": 8, "rate": "6.94it/s"})

    def test_slow_rate_form(self):
        parsed = stepmeter.parse_progress(f" 25%|██| 2/8 [00:05<00:14, 3.22s/it]")
        self.assertEqual(parsed, {"value": 2, "max": 8, "rate": "3.22s/it"})

    def test_missing_rate(self):
        parsed = stepmeter.parse_progress(" 25%|██| 2/8 [00:05<00:14]")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["rate"], "")

    def test_zero_percent_row(self):
        parsed = stepmeter.parse_progress("  0%|          | 0/8 [00:00<?, ?it/s]")
        self.assertIsNotNone(parsed)
        self.assertEqual((parsed["value"], parsed["max"]), (0, 8))
        self.assertEqual(parsed["rate"], "")

    def test_last_match_wins_in_multiline(self):
        text = "\n".join([_tqdm_line(1, 8), _tqdm_line(5, 8), "some warning line"])
        parsed = stepmeter.parse_progress(text)
        self.assertEqual(parsed["value"], 5)

    def test_plain_counter_not_matched(self):
        # 没有 tqdm 竖条+计时块形态的普通 "1/3" 计数不能误报。
        self.assertIsNone(stepmeter.parse_progress("已处理 1/3 张图片"))
        self.assertIsNone(stepmeter.parse_progress(""))

    def test_invalid_shapes_rejected(self):
        self.assertIsNone(stepmeter.parse_progress("| 8/0 [00:01<00:01, 1it/s]"))
        self.assertIsNone(stepmeter.parse_progress("| 9/8 [00:01<00:01, 1it/s]"))


class StepMeterNetworkTests(unittest.TestCase):
    def test_queue_snapshot_reuses_one_response_and_preserves_queue_ids_compatibility(self):
        payload = {"queue_running": [[1, "running", {}, {}, []]],
                   "queue_pending": [[2, "pending", {}, {}, []]]}
        with mock.patch.object(comfyui_client._DIRECT_OPENER, "open", side_effect=lambda *args, **kw:
                               io.BytesIO(json.dumps(payload).encode())) as opened:
            all_ids, running_ids = comfyui_client.queue_snapshot("http://127.0.0.1:9")
            self.assertEqual(all_ids, {"running", "pending"})
            self.assertEqual(running_ids, {"running"})
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(comfyui_client.queue_ids("http://127.0.0.1:9"), all_ids)
        with mock.patch.object(comfyui_client._DIRECT_OPENER, "open", side_effect=OSError("offline")):
            self.assertIsNone(comfyui_client.queue_snapshot("http://127.0.0.1:9"))
            self.assertIsNone(comfyui_client.queue_ids("http://127.0.0.1:9"))

    def test_log_request_ignores_environment_and_system_proxies(self):
        payload = {"entries": [{"t": int(time.time() * 1000), "m": _tqdm_line(2, 8)}]}
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                logs = {"entries": []} if paths.count("/internal/logs/raw") == 1 else payload
                body = json.dumps(logs if self.path == "/internal/logs/raw" else {
                    "system": {"comfyui_version": "test"}, "devices": [],
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            # 模拟启动时系统/环境代理存在，且没有给 ComfyUI 地址配置绕过。
            proxies = {"http": "http://127.0.0.1:1", "https": "http://127.0.0.1:1"}
            with mock.patch("urllib.request.getproxies", return_value=proxies), \
                    mock.patch("urllib.request.proxy_bypass", return_value=False):
                isolated = runpy.run_path(str(Path(stepmeter.__file__)))
                self.assertEqual(comfyui_client.reachable(base)["version"], "test")
                isolated["observe"](base + "/", "pid-proxy", is_running=True)
                isolated["observe"](base + "/", "pid-proxy", is_running=True)
                snap = isolated["snapshot_for"](["pid-proxy"])
            self.assertIsNotNone(snap, "生成接口可达时，进度请求也必须绕过代理")
            self.assertEqual((snap["value"], snap["max"]), (2, 8))
            self.assertEqual(paths, ["/system_stats", "/internal/logs/raw", "/internal/logs/raw"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


class StepMeterStateTests(unittest.TestCase):
    def setUp(self):
        stepmeter.reset_for_tests()

    def tearDown(self):
        stepmeter.reset_for_tests()

    def _with_fetch(self, payload, fn):
        original = stepmeter._fetch_raw
        # 首次看到运行只记录游标，后续新到日志才可以展示。
        responses = iter([{"entries": []}, payload])
        stepmeter._fetch_raw = lambda base: next(responses, payload)
        try:
            fn()
            return fn()
        finally:
            stepmeter._fetch_raw = original

    def test_observe_binds_and_snapshots_for_known_prompt(self):
        now_ms = int(time.time() * 1000)
        payload = {"entries": [{"t": now_ms, "m": _tqdm_line(2, 8)}]}
        self._with_fetch(payload, lambda: stepmeter.observe("http://127.0.0.1:9", "pid-1", is_running=True))
        snap = stepmeter.snapshot_for(["pid-1"])
        self.assertIsNotNone(snap)
        self.assertEqual((snap["value"], snap["max"], snap["rate"]), (2, 8, "6.94it/s"))

    def test_unknown_prompt_gets_nothing(self):
        now_ms = int(time.time() * 1000)
        payload = {"entries": [{"t": now_ms, "m": _tqdm_line(2, 8)}]}
        self._with_fetch(payload, lambda: stepmeter.observe("http://127.0.0.1:9", "pid-1", is_running=True))
        self.assertIsNone(stepmeter.snapshot_for(["pid-other"]))
        self.assertIsNone(stepmeter.snapshot_for([]))

    def test_stale_data_not_served(self):
        payload = {"entries": [{"t": int(time.time() * 1000), "m": _tqdm_line(2, 8)}]}
        self._with_fetch(payload, lambda: stepmeter.observe("http://127.0.0.1:9", "pid-1", is_running=True))
        # 把时间戳拨到新鲜期外。
        with stepmeter._LOCK:
            stepmeter._STATE["updated_at"] = int(time.time() * 1000) - stepmeter.FRESH_WINDOW_MS - 1
        self.assertIsNone(stepmeter.snapshot_for(["pid-1"]))

    def test_missing_endpoint_degrades_silently(self):
        # 404 / 网络失败 → _fetch_raw 返回 None → 状态只绑定不更新，快照无数据。
        original = stepmeter._fetch_raw
        stepmeter._fetch_raw = lambda base: None
        try:
            stepmeter.observe("http://127.0.0.1:9", "pid-1", is_running=True)
        finally:
            stepmeter._fetch_raw = original
        self.assertIsNone(stepmeter.snapshot_for(["pid-1"]))

    def test_rebind_clears_old_prompt(self):
        now_ms = int(time.time() * 1000)
        payload = {"entries": [{"t": now_ms, "m": _tqdm_line(2, 8)}]}
        self._with_fetch(payload, lambda: stepmeter.observe("http://127.0.0.1:9", "pid-1", is_running=True))
        self._with_fetch(payload, lambda: stepmeter.observe("http://127.0.0.1:9", "pid-2", is_running=True))
        self.assertIsNone(stepmeter.snapshot_for(["pid-1"]))
        self.assertIsNotNone(stepmeter.snapshot_for(["pid-2"]))

    def test_iso_timestamp_accepted(self):
        # ComfyUI logger 的 t 是 datetime.now().isoformat() 字符串：新鲜期按真实时刻算。
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        payload = {"entries": [{"t": stamp, "m": _tqdm_line(3, 8)}]}
        self._with_fetch(payload, lambda: stepmeter.observe("http://127.0.0.1:9", "pid-1", is_running=True))
        snap = stepmeter.snapshot_for(["pid-1"])
        self.assertIsNotNone(snap)
        self.assertEqual((snap["value"], snap["max"]), (3, 8))

    def test_unparseable_timestamp_row_skipped(self):
        # 时间戳解析不动的行不能进状态：旧行会被误判成永远新鲜，
        # 新任务开局会把上一次生成留在缓冲里的进度当现势数据显示。
        payload = {"entries": [{"t": "not-a-time", "m": _tqdm_line(8, 8)}]}
        self._with_fetch(payload, lambda: stepmeter.observe("http://127.0.0.1:9", "pid-1", is_running=True))
        self.assertIsNone(stepmeter.snapshot_for(["pid-1"]))

    def test_new_prompt_never_inherits_recent_old_log_and_pending_skips_network(self):
        stamp = int(time.time() * 1000) - 1000
        old = {"entries": [{"t": stamp, "m": _tqdm_line(8, 8)}]}
        with mock.patch.object(stepmeter, "_fetch_raw", return_value=old) as fetch:
            stepmeter.observe("http://127.0.0.1:9", "queued", is_running=False)
            self.assertEqual(fetch.call_count, 0)
            self.assertIsNone(stepmeter.snapshot_for(["queued"]))
            stepmeter.observe("http://127.0.0.1:9", "new", is_running=True)
            stepmeter.observe("http://127.0.0.1:9", "new", is_running=True)
            self.assertIsNone(stepmeter.snapshot_for(["new"]))
            fetch.return_value = {"entries": [*old["entries"], {"t": stamp + 500, "m": _tqdm_line(1, 8)}]}
            stepmeter.observe("http://127.0.0.1:9", "new", is_running=True)
            self.assertEqual(stepmeter.snapshot_for(["new"])["value"], 1)
            stepmeter.observe("http://127.0.0.1:9", "pending-after-new", is_running=False)
            self.assertIsNone(stepmeter.snapshot_for(["new"]))

    def test_late_old_observation_cannot_overwrite_new_prompt(self):
        entered, release = threading.Event(), threading.Event()
        stamp = int(time.time() * 1000)
        def fetch(base):
            if base.endswith("old"):
                entered.set()
                self.assertTrue(release.wait(2))
                return {"entries": [{"t": stamp + 1, "m": _tqdm_line(8, 8)}]}
            return {"entries": []}
        with mock.patch.object(stepmeter, "_fetch_raw", side_effect=fetch):
            worker = threading.Thread(target=lambda: stepmeter.observe("http://old", "old", is_running=True))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                stepmeter.observe("http://new", "new", is_running=True)
            finally:
                release.set()
                worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(stepmeter._STATE.get("prompt_id"), "new")
        self.assertIsNone(stepmeter.snapshot_for(["old"]))


class RunnerStepBindingTests(unittest.TestCase):
    def test_wait_loop_only_marks_actual_running_prompt_as_running(self):
        for queue in [({"pid"}, set()), ({"pid"}, {"pid"}), None]:
            with self.subTest(queue=queue):
                cancel = threading.Event()
                with mock.patch.object(comfyui_client, "history_entry", return_value=None), \
                        mock.patch.object(comfyui_client, "queue_snapshot", return_value=queue), \
                        mock.patch.object(stepmeter, "observe") as observe, \
                        mock.patch.object(cancel, "wait", side_effect=lambda timeout: cancel.set()):
                    result = ComfyUIRunner._wait_shot(mock.Mock(), "http://local", "task", 0, "pid",
                                                     cancel, {}, {}, {}, [], [], {})
                self.assertEqual(result, (False, [], "用户取消"))
                observe.assert_called_once_with("http://local", "pid",
                                                is_running=queue is not None and "pid" in queue[1])


class StepExposureTests(unittest.TestCase):
    """jobs._running_step 出口：running + 归属命中才带 step，其余 None。"""

    def setUp(self):
        stepmeter.reset_for_tests()

    def tearDown(self):
        stepmeter.reset_for_tests()

    def _running_task(self, prompt_ids):
        return {
            "id": "ct-test",
            "status": "running",
            "checkpoint": {"prompt_ids": prompt_ids, "completed": [], "errors": []},
        }

    def test_running_task_with_fresh_step(self):
        now_ms = int(time.time() * 1000)
        payload = {"entries": [{"t": now_ms, "m": _tqdm_line(3, 8)}]}
        original = stepmeter._fetch_raw
        responses = iter([{"entries": []}, payload])
        stepmeter._fetch_raw = lambda base: next(responses, payload)
        try:
            stepmeter.observe("http://127.0.0.1:9", "pid-9", is_running=True)
            stepmeter.observe("http://127.0.0.1:9", "pid-9", is_running=True)
        finally:
            stepmeter._fetch_raw = original
        step = comfyui_jobs._running_step(self._running_task(["pid-9"]), self._running_task(["pid-9"])["checkpoint"])
        self.assertIsNotNone(step)
        self.assertEqual(step["value"], 3)

    def test_terminal_status_has_no_step(self):
        task = self._running_task(["pid-9"])
        task["status"] = "success"
        self.assertIsNone(comfyui_jobs._running_step(task, task["checkpoint"]))


class StepMeterUiContractTests(unittest.TestCase):
    """前端契约：顶栏全局单条 + 定点更新 + 不进指纹/签名。"""

    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.html = (self.root / "public/index.html").read_text(encoding="utf-8")
        self.js = (self.root / "public/js/24-comfyui.js").read_text(encoding="utf-8")
        self.css = (self.root / "public/styles.css").read_text(encoding="utf-8")

    def test_meter_is_topbar_direct_child_after_tools(self):
        # 顶栏直接子元素、在 tools 之后 header 闭合之前；tools 是模块库页专属，
        # 条不搭它的车（自带贴右 margin）。
        self.assertIn('id="comfyuiStepMeter"', self.html)
        self.assertIn('class="comfyui-step-meter" id="comfyuiStepMeter" hidden', self.html)
        self.assertLess(self.html.index("</div>", self.html.index('id="studioCustomImport"')),
                        self.html.index('id="comfyuiStepMeter"'))
        self.assertLess(self.html.index('id="comfyuiStepMeter"'), self.html.index("</header>", self.html.index('id="comfyuiStepMeter"')))
        self.assertIn('class="comfyui-step-meter-fill"', self.html)

    def test_meter_has_pct_and_detail_spans(self):
        self.assertIn('class="comfyui-step-meter-pct"', self.html)
        self.assertIn('class="comfyui-step-meter-detail"', self.html)
        self.assertIn('class="comfyui-step-meter-track"', self.html)

    def test_js_single_writer_and_call_sites(self):
        self.assertIn("function updateStepMeter()", self.js)
        self.assertIn("function normalizeStep(step)", self.js)
        self.assertIn("function activeComfyStep()", self.js)
        # 三个刷新点：renderTasks（单次链路）、批量 tick（签名不变也要刷）、renderBatch 尾部。
        self.assertGreaterEqual(self.js.count("updateStepMeter();"), 3)
        # 步级不进签名/指纹：签名函数与指纹函数都不引用 step 字段。
        signature_pos = self.js.index("function batchRunSignature(")
        self.assertNotIn("step", self.js[signature_pos:self.js.index("function startBatchPolling(")])

    def test_shape_validation_and_terminal_hide(self):
        # 形状非法不渲染；无数据整块隐藏（不留 100% 定格）。
        self.assertIn("max <= 0 || value > max", self.js)
        self.assertIn("meter.hidden = true", self.js)
        self.assertIn('meter.hidden = false', self.js)

    def test_css_placement_rules(self):
        # 顶栏直接子元素自带贴右；tools 可见时收回 auto（避免把搜索/导入顶向中间）。
        self.assertIn(".comfyui-step-meter {", self.css)
        self.assertIn(".comfyui-step-meter[hidden] { display: none; }", self.css)
        self.assertIn(".studio-topbar-tools:not([hidden]) ~ .comfyui-step-meter { margin-left: 0; }", self.css)
        self.assertIn("@keyframes comfyui-step-crawl", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce) { .comfyui-step-meter-fill { animation: none; } }", self.css)
        # 手机独占行贴右；窄屏只留条 + 百分比。
        self.assertIn(".comfyui-step-meter { order: 4; margin-left: auto; }", self.css)
        self.assertIn(".comfyui-step-meter-detail { display: none; }", self.css)

    def test_backend_route_not_required_for_contract(self):
        # 后端模块存在且导出口径完整（解析/归属/新鲜期）。
        source = (self.root / "naiba/comfyui/stepmeter.py").read_text(encoding="utf-8")
        self.assertIn("/internal/logs/raw", source)
        self.assertIn("FRESH_WINDOW_MS", source)
        self.assertIn("def snapshot_for", source)


if __name__ == "__main__":
    unittest.main()
