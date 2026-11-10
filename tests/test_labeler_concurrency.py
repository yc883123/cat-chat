# -*- coding: utf-8 -*-
"""护栏：贴签匠「自定义打标 → 并发打标（多打标员 + 监督员）」。

覆盖计划 ``docs/plans/2026-11-10-贴签匠自定义打标多并发监督计划.md`` 的硬口径：

1. 配置契约：只在自定义档开放；1–9 个槽位、槽位可重复；监督员必填；
   本地 profile 拒绝并点名槽位；客户端不能注入 Key/端点；关闭开关指纹逐字节不变。
2. 纯监督契约：approve/revise 严格 schema，结构错误抛错、绝不当作通过。
3. 供应商闸门：同一档案（model_key）共享额度；打标与监督共享；取消能打断排队。
4. 执行：多图同时打标、监督队列不被单个监督模型串行化、槽位数就是峰值上限、
   乱序返回仍一一对应、无重复领取、返工/人工检查/失败/取消/恢复语义、用量按线程归属。
5. 前端与服务端契约（DOM id / 接线 / 路由）。
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core.exceptions import TaskCancelled  # noqa: E402
from naiba.labeler import LABELER_ITEM_STATES  # noqa: E402
from naiba.labeler.db import LabelerDB  # noqa: E402
from naiba.labeler.gate import (  # noqa: E402
    DEFAULT_PROVIDER_CONCURRENCY,
    ProviderGate,
    validate_provider_limits,
)
from naiba.labeler.jobs import (  # noqa: E402
    LabelerJobManager,
    LabelerTaskError,
    config_fingerprint,
    normalize_config,
)
from naiba.labeler.runner import LabelerRunner, MAX_REVISIONS  # noqa: E402
from naiba.labeler.service import LabelerService  # noqa: E402
from naiba.labeler.store import LabelerStore  # noqa: E402
from naiba.labeler.supervisor import (  # noqa: E402
    SUPERVISION_PROMPT_VERSION,
    SupervisionFormatError,
    build_supervision_messages,
    build_supervision_prompt,
    parse_verdict,
    revise_note,
    rule_summary,
)


def _write_png(path: Path, color=(20, 120, 200), size=(16, 16)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


class _KeyedConfig:
    """按 model_key 区分的配置桩：未知 key 抛 KeyError，可模拟本地供应商。"""

    def __init__(self, local_keys: tuple[str, ...] = ()):
        self.local_keys = set(local_keys)

    def profile(self, key):
        key = str(key)
        if key == "missing":
            raise KeyError(key)
        kind = "local" if key in self.local_keys else "online"
        return {"kind": kind, "name": key, "model": "stub-model",
                "base_url": "http://127.0.0.1:9", "model_key": key}

    def runtime_guard_options(self):
        return {}


class _StubPaths:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)


class _StubApp:
    def __init__(self, data_dir: Path, *, models=None, config=None):
        self.paths = _StubPaths(data_dir)
        self.models = models
        self.config = config or _KeyedConfig()
        self.runs = None


def _message_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


class _ConcurrencyModels:
    """线程局部 last_usage + 峰值统计 + 可编排行为的模型桩。

    ``label``/``supervisor`` 行为签名：(text, options, call_index) -> str，
    ``kind`` 由提示词判断（监督提示词以「你是图片标注审核员」开头）。
    """

    def __init__(self, *, label=None, supervisor=None):
        self._local = threading.local()
        self.label = label or (lambda text, options, index: '{"tags":[{"tag":"cat","confidence":0.9}],"description":"一只猫"}')
        self.supervisor = supervisor or (lambda text, options, index: '{"decision":"approve","reason":"符合规则"}')
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.inflight_by_key: dict[str, int] = {}
        self.peak_by_key: dict[str, int] = {}
        self.calls: list[dict[str, Any]] = []
        self.usages: list[dict[str, int]] = []
        self.counters = {"label": 0, "supervisor": 0}

    @property
    def last_usage(self) -> dict[str, int]:
        return dict(getattr(self._local, "usage", {}) or {})

    @property
    def last_finish_reason(self) -> str:
        return str(getattr(self._local, "finish_reason", "") or "")

    def complete(self, profile, messages, options, status=None):
        text = _message_text(messages)
        kind = "supervisor" if "你是图片标注审核员" in text else "label"
        key = str(profile.get("model_key") or "")
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.inflight_by_key[key] = self.inflight_by_key.get(key, 0) + 1
            self.peak_by_key[key] = max(self.peak_by_key.get(key, 0),
                                        self.inflight_by_key[key])
            index = self.counters[kind]
            self.counters[kind] += 1
            self.calls.append({"kind": kind, "key": key, "text": text, "index": index})
            usage = {
                "prompt_tokens": index + (1000 if kind == "supervisor" else 0),
                "completion_tokens": 1,
                "total_tokens": index + (1000 if kind == "supervisor" else 0) + 1,
            }
            self._local.usage = usage
            self._local.finish_reason = "stop"
            self.usages.append(usage)
        try:
            behavior = self.supervisor if kind == "supervisor" else self.label
            return behavior(text, options, index)
        finally:
            with self.lock:
                self.inflight -= 1
                self.inflight_by_key[key] -= 1

    def total_tokens(self) -> int:
        with self.lock:
            return sum(int(row.get("total_tokens") or 0) for row in self.usages)

    def calls_of(self, kind: str) -> list[dict[str, Any]]:
        with self.lock:
            return [row for row in self.calls if row["kind"] == kind]


def _image_name(text: str) -> str:
    match = re.search(r"当前图片：([^\n（]+)", text)
    return match.group(1).strip() if match else ""


def _candidate(text: str) -> dict[str, Any]:
    match = re.search(r"候选标注：\n(\{.*?\})\n", text, re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def _base_config(*, workers: list[dict[str, str]], supervisor: dict[str, str],
                 **overrides) -> dict[str, Any]:
    first = workers[0] if workers else {"model_key": "", "model_name": ""}
    config = {
        "engine": "provider",
        "output_mode": "auto",
        "language": "auto",
        "preset": "general",
        "system_prompt": "为图片输出 3-8 个英文标签，只描述画面里真实可见的内容。",
        "model_key": first["model_key"],
        "model_name": first["model_name"],
        "confidence_threshold": 0.0,
        "max_tags": 24,
        "custom_prompt": "",
        "subject_filter": {"enabled": False, "keywords": [], "strength": "medium"},
        "concurrency": 1,
        "retry_limit": 0,
        "timeout_seconds": 30,
        "prompt_version": 2,
        "concurrency_enabled": True,
        "workers": workers,
        "supervisor": supervisor,
    }
    config.update(overrides)
    return config


def _workers(count: int, prefix: str = "online:worker") -> list[dict[str, str]]:
    return [
        {"model_key": f"{prefix}{index}", "model_name": f"worker-model-{index}"}
        for index in range(count)
    ]


class ConcurrencyConfigTests(unittest.TestCase):
    def setUp(self):
        self.app = _StubApp(Path(tempfile.gettempdir()))

    def test_workers_from_1_to_9_accepted(self):
        for count in (1, 2, 9):
            config = normalize_config(self.app, _base_config(
                workers=_workers(count),
                supervisor={"model_key": "online:judge", "model_name": "judge-model"},
            ))
            self.assertTrue(config["concurrency_enabled"])
            self.assertEqual(len(config["workers"]), count)
            self.assertEqual(config["concurrency"], count)
            self.assertEqual(config["supervisor"]["model_name"], "judge-model")
            self.assertEqual(config["supervisor_version"], SUPERVISION_PROMPT_VERSION)

    def test_more_than_9_rejected(self):
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(
                workers=_workers(10),
                supervisor={"model_key": "online:judge", "model_name": "judge-model"},
            ))

    def test_empty_workers_rejected(self):
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(
                workers=[],
                supervisor={"model_key": "online:judge", "model_name": "judge-model"},
            ))

    def test_duplicate_slots_allowed(self):
        same = _workers(1)[0]
        config = normalize_config(self.app, _base_config(
            workers=[dict(same), dict(same)],
            supervisor={"model_key": "online:judge", "model_name": "judge-model"},
        ))
        self.assertEqual(config["workers"][0], config["workers"][1])
        self.assertEqual(config["concurrency"], 2)

    def test_supervisor_required(self):
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(workers=_workers(1), supervisor={}))
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(
                workers=_workers(1),
                supervisor={"model_key": "missing", "model_name": "x"},
            ))

    def test_local_worker_rejected_with_slot_number(self):
        app = _StubApp(Path(tempfile.gettempdir()), config=_KeyedConfig(("online:worker2",)))
        with self.assertRaises(LabelerTaskError) as ctx:
            normalize_config(app, _base_config(
                workers=_workers(3),
                supervisor={"model_key": "online:judge", "model_name": "judge-model"},
            ))
        message = str(ctx.exception)
        self.assertIn("第 3 个打标员槽位", message)
        self.assertIn("本地模型", message)

    def test_local_supervisor_rejected(self):
        app = _StubApp(Path(tempfile.gettempdir()),
                       config=_KeyedConfig(("online:judge",)))
        with self.assertRaises(LabelerTaskError):
            normalize_config(app, _base_config(
                workers=_workers(1),
                supervisor={"model_key": "online:judge", "model_name": "judge-model"},
            ))

    def test_slot_cannot_inject_secret_or_endpoint(self):
        with self.assertRaises(LabelerTaskError) as ctx:
            normalize_config(self.app, _base_config(
                workers=[{"model_key": "online:w0", "model_name": "m", "api_key": "sk-x"}],
                supervisor={"model_key": "online:judge", "model_name": "judge-model"},
            ))
        self.assertIn("不支持的字段", str(ctx.exception))

    def test_workers_without_flag_rejected(self):
        raw = _base_config(workers=_workers(1),
                           supervisor={"model_key": "online:judge", "model_name": "judge-model"})
        raw["concurrency_enabled"] = False
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, raw)

    def test_preset_mode_cannot_enable_concurrency(self):
        raw = _base_config(
            workers=_workers(1),
            supervisor={"model_key": "online:judge", "model_name": "judge-model"},
        )
        raw.pop("system_prompt")
        raw["output_mode"] = "keywords"
        raw["language"] = "en"
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, raw)

    def test_top_level_model_follows_first_worker(self):
        config = normalize_config(self.app, _base_config(
            workers=_workers(2),
            supervisor={"model_key": "online:judge", "model_name": "judge-model"},
            model_key="online:someone-else",
            model_name="other-model",
        ))
        self.assertEqual(config["model_key"], "online:worker0")
        self.assertEqual(config["model_name"], "worker-model-0")

    def test_fingerprint_includes_workers_and_supervisor(self):
        supervisor = {"model_key": "online:judge", "model_name": "judge-model"}
        base = normalize_config(self.app, _base_config(
            workers=_workers(2), supervisor=supervisor))
        other_worker = normalize_config(self.app, _base_config(
            workers=_workers(1), supervisor=supervisor))
        other_judge = normalize_config(self.app, _base_config(
            workers=_workers(2),
            supervisor={"model_key": "online:judge2", "model_name": "judge-model-2"}))
        self.assertNotEqual(base["config_fingerprint"], other_worker["config_fingerprint"])
        self.assertNotEqual(base["config_fingerprint"], other_judge["config_fingerprint"])
        self.assertEqual(base["config_fingerprint"], config_fingerprint(base))

    def test_disabled_snapshot_has_no_concurrency_keys_and_legacy_fingerprint(self):
        raw = {
            "engine": "provider", "output_mode": "keywords", "language": "en",
            "preset": "general", "model_key": "online:stub", "model_name": "stub-model",
            "confidence_threshold": 0.35, "max_tags": 24, "custom_prompt": "",
            "concurrency": 1, "retry_limit": 1, "timeout_seconds": 90,
            "prompt_version": 2,
            "subject_filter": {"enabled": False, "keywords": [], "strength": "medium"},
        }
        config = normalize_config(self.app, raw)
        for key in ("concurrency_enabled", "workers", "supervisor", "supervisor_version"):
            self.assertNotIn(key, config)
        self.assertEqual(config["config_fingerprint"], config_fingerprint(raw))

    def test_continue_requires_current_supervisor_version(self):
        data_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(data_dir, ignore_errors=True))
        store = LabelerStore(data_dir)
        db = LabelerDB(data_dir)
        app = _StubApp(data_dir)
        runner = LabelerRunner(app, store, db)
        self.addCleanup(runner.shutdown)
        jobs = LabelerJobManager(app, store, db, runner)
        image = _write_png(data_dir / "uploads" / "d" / "a.png")
        task = jobs.submit({
            "image_refs": [{"path": str(image), "name": image.name,
                            "relative_path": "d/a.png"}],
            "config": _base_config(
                workers=_workers(1),
                supervisor={"model_key": "online:judge", "model_name": "judge-model"}),
        })
        # 人为改坏快照里的策略版本，模拟老任务换了监督标准。
        record = store.get_task(task["id"])
        record["input"]["config"]["supervisor_version"] = "v0"
        store._write_json(store._record_path(task["id"]), record)
        with self.assertRaises(LabelerTaskError):
            jobs.continue_task(task["id"], mode="unfinished", confirm_uncertain=True)


class SupervisionContractTests(unittest.TestCase):
    def test_approve_parse(self):
        verdict = parse_verdict('{"decision":"approve","reason":"符合要求"}')
        self.assertEqual(verdict["decision"], "approve")
        self.assertEqual(verdict["reason"], "符合要求")
        self.assertEqual(verdict["tags"], [])
        self.assertEqual(verdict["description"], "")

    def test_revise_parse_keeps_revision_fields(self):
        verdict = parse_verdict(
            '```json\n{"decision":"revise","reason":"漏了雨伞",'
            '"tags":["rain","umbrella"],"description":"雨中街道"}\n```'
        )
        self.assertEqual(verdict["decision"], "revise")
        self.assertEqual(verdict["tags"], ["rain", "umbrella"])
        self.assertIn("雨中街道", revise_note(verdict))

    def test_invalid_json_or_unknown_decision_rejected(self):
        for text in ("", "不是 JSON", '{"decision":"maybe"}'):
            with self.assertRaises(SupervisionFormatError):
                parse_verdict(text)

    def test_approve_ignores_revision_payload(self):
        verdict = parse_verdict(
            '{"decision":"approve","reason":"ok","tags":["x"],"description":"y"}')
        self.assertEqual(verdict["tags"], [])
        self.assertEqual(verdict["description"], "")

    def test_rule_summary_from_prompt_and_skill(self):
        self.assertTrue(rule_summary({"system_prompt": "只描述动物。"}).startswith("只描述动物。"))
        self.assertEqual(rule_summary({}), "")
        skill = rule_summary({"skill_id": "anime-tag", "skill_revision": "abc123"})
        self.assertIn("anime-tag", skill)
        self.assertIn("abc123", skill)

    def test_supervision_messages_need_readable_image(self):
        messages, error = build_supervision_messages("", "x")
        self.assertEqual(messages, [])
        self.assertIn("图片无法读取", error)


class ProviderGateTests(unittest.TestCase):
    def test_limit_is_enforced_and_shared_by_key(self):
        gate = ProviderGate({"online:a": 2})
        peak = {"n": 0}
        current = {"n": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=5)

        def worker():
            with gate.slot("online:a"):
                with lock:
                    current["n"] += 1
                    peak["n"] = max(peak["n"], current["n"])
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
                time.sleep(0.05)
                with lock:
                    current["n"] -= 1

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak["n"], 2)
        self.assertEqual(gate.inflight("online:a"), 0)

    def test_different_keys_have_independent_limits(self):
        gate = ProviderGate({"online:a": 1, "online:b": 1})
        self.assertTrue(gate.limit_for("online:a") == 1)
        self.assertTrue(gate.limit_for("online:unknown") == DEFAULT_PROVIDER_CONCURRENCY)
        with gate.slot("online:a"):
            with gate.slot("online:b"):
                self.assertEqual(gate.inflight("online:a"), 1)
                self.assertEqual(gate.inflight("online:b"), 1)

    def test_cancel_breaks_wait(self):
        gate = ProviderGate({"online:a": 1})
        event = threading.Event()
        with gate.slot("online:a"):
            event.set()
            with self.assertRaises(TaskCancelled):
                gate.slot("online:a", event).__enter__()

    def test_limits_validation(self):
        self.assertEqual(validate_provider_limits({"k": "4"}), {"k": 4})
        for raw in ({"k": 0}, {"k": 10}, {"k": True}, {"k": "x"}, []):
            with self.assertRaises(ValueError):
                validate_provider_limits(raw)


class ConcurrentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.uploads = self.data_dir / "uploads"
        self.store = LabelerStore(self.data_dir)
        self.db = LabelerDB(self.data_dir)

    def _jobs(self, app):
        runner = LabelerRunner(app, self.store, self.db)
        self.addCleanup(runner.shutdown)
        return LabelerJobManager(app, self.store, self.db, runner)

    def _images(self, count: int) -> list[Path]:
        return [
            _write_png(self.uploads / "d" / f"a{index}.png",
                       color=(10 * index, 100, 200))
            for index in range(count)
        ]

    def _submit(self, app, images, *, workers=None, supervisor=None, **overrides):
        jobs = self._jobs(app)
        refs = [
            {"path": str(path), "name": path.name, "relative_path": f"d/{path.name}"}
            for path in images
        ]
        workers = workers or _workers(2)
        supervisor = supervisor or {"model_key": "online:judge",
                                    "model_name": "judge-model"}
        config = _base_config(workers=workers, supervisor=supervisor, **overrides)
        task = jobs.submit({"image_refs": refs, "config": config})
        return jobs, task

    def _wait(self, task_id, timeout=20.0):
        deadline = time.time() + timeout
        state = None
        while time.time() < deadline:
            state = self.store.get_task(task_id)
            if state and state.get("status") in {"done", "failed", "cancelled", "interrupted"}:
                return state
            time.sleep(0.02)
        return state

    def test_two_workers_label_concurrently_and_mode_barrier_passes(self):
        barrier = threading.Barrier(2, timeout=8)
        models = _ConcurrencyModels(label=lambda text, options, index: (
            barrier.wait(),
            '{"tags":[{"tag":"cat","confidence":0.9}],"description":"一只猫"}',
        )[1])
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(2), workers=_workers(2))
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        counts = self.db.counts(task["id"])
        self.assertEqual(counts["done"], 2)
        self.assertEqual(len(models.calls_of("label")), 2)
        self.assertEqual(len(models.calls_of("supervisor")), 2)

    def test_nine_slots_peak_equals_slots(self):
        barrier = threading.Barrier(9, timeout=8)
        models = _ConcurrencyModels(label=lambda text, options, index: (
            barrier.wait(),
            '{"tags":[{"tag":"cat","confidence":0.9}],"description":""}',
        )[1])
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(9), workers=_workers(9))
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(self.db.counts(task["id"])["done"], 9)
        self.assertEqual(models.peak, 9)

    def test_single_worker_stays_sequential(self):
        models = _ConcurrencyModels()
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(3), workers=_workers(1))
        self._wait(task["id"])
        self.assertEqual(models.peak, 1)
        self.assertEqual(self.db.counts(task["id"])["done"], 3)

    def test_supervision_is_not_serialized_by_single_supervisor_model(self):
        barrier = threading.Barrier(3, timeout=8)

        def supervise(text, options, index):
            barrier.wait()
            return '{"decision":"approve","reason":"通过"}'

        models = _ConcurrencyModels(
            label=lambda text, options, index: '{"tags":[{"tag":"cat","confidence":0.9}],"description":""}',
            supervisor=supervise,
        )
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(3), workers=_workers(3))
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(len(models.calls_of("supervisor")), 3)

    def test_out_of_order_returns_stay_bound_to_their_image_and_usage(self):
        def label(text, options, index):
            name = _image_name(text)
            # 乱序：后面的图先返回。
            time.sleep(0.02 * ((7 - int(re.search(r"a(\d+)", name).group(1))) % 5))
            return json.dumps(
                {"tags": [{"tag": name.rsplit(".", 1)[0], "confidence": 0.9}],
                 "description": f"desc:{name}"},
                ensure_ascii=False,
            )

        def supervise(text, options, index):
            candidate = _candidate(text)
            time.sleep(0.01)
            return json.dumps(
                {"decision": "approve", "reason": "ok",
                 "description": candidate.get("description", "")},
                ensure_ascii=False,
            )

        models = _ConcurrencyModels(label=label, supervisor=supervise)
        app = _StubApp(self.data_dir, models=models)
        images = self._images(6)
        _, task = self._submit(app, images, workers=_workers(4), write_txt=True)
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        rows = {row["name"]: row for row in self.db.list_items(task["id"])}
        annotations = {row["name"]: row for row in self.db.annotations(task_id=task["id"], limit=100)}
        for image in images:
            row = rows[image.name]
            self.assertEqual(row["state"], "succeeded")
            annotation = annotations[image.name]
            expected_tag = f"d/{image.stem}"
            self.assertEqual(annotation["tags"], [expected_tag])
            self.assertEqual(annotation["description"], f"desc:d/{image.name}")
            # TXT 与标注同源（fallback 写的就是这张图的标注；auto 输出形式按标签出 caption）。
            txt = (self.store.outputs_dir(task["id"]) / row["txt_path"]).read_text(encoding="utf-8")
            self.assertEqual(txt.strip(), expected_tag)
        # 用量按线程归属：任务总量等于所有调用读数之和，没有串线程或漏记。
        self.assertEqual(int(state["usage"].get("total_tokens") or 0), models.total_tokens())
        # 每张图只领取一次：打标尝试次数 1，且没有重复的标注行。
        self.assertTrue(all(row["attempt_count"] == 1 for row in rows.values()))
        self.assertEqual(len(annotations), 6)

    def test_revise_once_then_approve(self):
        def supervise(text, options, index):
            candidate = _candidate(text)
            if "umbrella" in (candidate.get("tags") or []):
                return '{"decision":"approve","reason":"改好了"}'
            return '{"decision":"revise","reason":"缺少雨伞","tags":["umbrella"]}'

        models = _ConcurrencyModels(
            label=lambda text, options, index: (
                '{"tags":[{"tag":"umbrella","confidence":0.9}],"description":"雨中街道"}'
                if "上一轮监督退回意见" in text
                else '{"tags":[{"tag":"street","confidence":0.9}],"description":"街道"}'
            ),
            supervisor=supervise,
        )
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(1), workers=_workers(1))
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        row = self.db.list_items(task["id"])[0]
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["supervisor_decision"], "approve")
        self.assertEqual(row["supervisor_attempts"], 1)
        annotation = self.db.annotations(task_id=task["id"], limit=10)[0]
        self.assertEqual(annotation["tags"], ["umbrella"])
        self.assertEqual(len(models.calls_of("label")), 2)

    def test_second_revision_then_approve(self):
        counter = {"n": 0}

        def supervise(text, options, index):
            counter["n"] += 1
            if counter["n"] <= MAX_REVISIONS:
                return '{"decision":"revise","reason":"再改"}'
            return '{"decision":"approve","reason":"通过"}'

        models = _ConcurrencyModels(supervisor=supervise)
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(1), workers=_workers(1))
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        row = self.db.list_items(task["id"])[0]
        self.assertEqual(row["supervisor_attempts"], MAX_REVISIONS)
        self.assertEqual(len(models.calls_of("label")), MAX_REVISIONS + 1)

    def test_revision_exhausted_goes_human_check_without_annotation(self):
        models = _ConcurrencyModels(
            supervisor=lambda text, options, index: '{"decision":"revise","reason":"一直不合格"}',
        )
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(1), workers=_workers(1))
        state = self._wait(task["id"])
        # 全部未通过监督：任务不能报「完成」。
        self.assertEqual(state["status"], "failed")
        row = self.db.list_items(task["id"])[0]
        self.assertEqual(row["state"], "human_check")
        self.assertIsNone(row["annotation_id"])
        self.assertEqual(row["supervisor_attempts"], MAX_REVISIONS)
        self.assertTrue(row["candidate"].get("tags"))
        self.assertEqual(self.db.annotations(task_id=task["id"], limit=10), [])
        self.assertEqual(self.db.counts(task["id"])["done"], 0)

    def test_unparseable_supervisor_output_goes_human_check(self):
        models = _ConcurrencyModels(
            supervisor=lambda text, options, index: "我觉得还可以",
        )
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(1), workers=_workers(1))
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "failed")
        row = self.db.list_items(task["id"])[0]
        self.assertEqual(row["state"], "human_check")
        self.assertIn("无法解析", row["supervisor_reason"])

    def test_supervisor_auth_failure_marks_item_failed(self):
        def supervise(text, options, index):
            raise RuntimeError("401 unauthorized: invalid api key")

        models = _ConcurrencyModels(supervisor=supervise)
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(1), workers=_workers(1))
        state = self._wait(task["id"])
        row = self.db.list_items(task["id"])[0]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_category"], "supervisor_auth")
        self.assertEqual(self.db.annotations(task_id=task["id"], limit=10), [])

    def test_worker_failure_never_calls_supervisor(self):
        def label(text, options, index):
            raise RuntimeError("400 bad request")

        models = _ConcurrencyModels(label=label)
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(1), workers=_workers(1))
        self._wait(task["id"])
        row = self.db.list_items(task["id"])[0]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(models.calls_of("supervisor"), [])

    def test_provider_limit_caps_label_and_supervisor_combined(self):
        self.store.write_settings({"provider_limits": {"online:worker0": 1}})
        models = _ConcurrencyModels()
        app = _StubApp(self.data_dir, models=models)
        # 两个槽位同一档案，监督员也用同一档案：峰值仍然只能是 1。
        workers = [
            {"model_key": "online:worker0", "model_name": "m0"},
            {"model_key": "online:worker0", "model_name": "m1"},
        ]
        _, task = self._submit(
            app, self._images(4), workers=workers,
            supervisor={"model_key": "online:worker0", "model_name": "judge"},
        )
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "done")
        self.assertEqual(models.peak_by_key.get("online:worker0"), 1)
        self.assertEqual(self.db.counts(task["id"])["done"], 4)

    def test_cancel_stops_dispatch_and_leaves_no_running(self):
        started = threading.Event()
        release = threading.Event()

        def label(text, options, index):
            started.set()
            for _ in range(500):
                if options["cancel_event"].is_set():
                    raise RuntimeError("任务已取消")
                if release.is_set():
                    break
                time.sleep(0.01)
            return '{"tags":[{"tag":"cat","confidence":0.9}],"description":""}'

        models = _ConcurrencyModels(label=label)
        app = _StubApp(self.data_dir, models=models)
        jobs, task = self._submit(app, self._images(10), workers=_workers(2))
        self.assertTrue(started.wait(5))
        self.assertTrue(jobs.cancel(task["id"]))
        state = self._wait(task["id"])
        self.assertEqual(state["status"], "cancelled")
        counts = self.db.counts(task["id"])
        self.assertEqual(counts["cancelled"], 10)
        self.assertEqual(counts["running"], 0)
        self.assertEqual(counts["supervising"], 0)
        self.assertEqual(len(models.calls_of("supervisor")), 0)

    def test_recover_marks_supervising_and_revising_uncertain(self):
        models = _ConcurrencyModels()
        app = _StubApp(self.data_dir, models=models)
        _, task = self._submit(app, self._images(2), workers=_workers(2))
        self._wait(task["id"])
        with self.db._connect() as db:
            db.execute("UPDATE task_items SET state='supervising' WHERE task_id=? AND ordinal=0",
                       (task["id"],))
            db.execute("UPDATE task_items SET state='revising' WHERE task_id=? AND ordinal=1",
                       (task["id"],))
        self.store.update_task(task["id"], status="running")
        jobs = self._jobs(app)
        jobs.recover_interrupted()
        counts = self.db.counts(task["id"])
        self.assertEqual(counts["uncertain"], 2)
        self.assertIn("uncertain", LABELER_ITEM_STATES)

    def test_human_check_is_resumable_via_continue(self):
        models = _ConcurrencyModels(
            supervisor=lambda text, options, index: '{"decision":"revise","reason":"不合格"}',
        )
        app = _StubApp(self.data_dir, models=models)
        jobs, task = self._submit(app, self._images(1), workers=_workers(1))
        self._wait(task["id"])
        self.assertEqual(self.db.list_items(task["id"])[0]["state"], "human_check")
        new_task = jobs.continue_task(task["id"], mode="unfinished", confirm_uncertain=False)
        self.assertEqual(self.db.counts(new_task["id"])["total"], 1)

    def test_public_items_expose_supervisor_fields(self):
        models = _ConcurrencyModels()
        app = _StubApp(self.data_dir, models=models)
        jobs, task = self._submit(app, self._images(1), workers=_workers(1))
        self._wait(task["id"])
        payload = jobs.task_items(task["id"])
        item = payload["items"][0]
        self.assertEqual(item["worker_slot"], 0)
        self.assertEqual(item["worker_model_name"], "worker-model-0")
        self.assertEqual(item["supervisor_decision"], "approve")
        self.assertEqual(item["state"], "succeeded")


class LabelerSettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        config = _KeyedConfig()
        self.app = _StubApp(self.data_dir, config=config)
        self.service = LabelerService(self.app)
        self.addCleanup(self.service.shutdown)

    def test_settings_roundtrip(self):
        payload, status = self.service.api_update_settings(
            {"provider_limits": {"online:a": 2}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["provider_limits"], {"online:a": 2})
        read, status = self.service.api_settings()
        self.assertEqual(status, 200)
        self.assertEqual(read["provider_limits"], {"online:a": 2})
        self.assertGreaterEqual(read["max_workers"], 9)

    def test_unknown_provider_and_bad_value_rejected(self):
        payload, status = self.service.api_update_settings(
            {"provider_limits": {"missing": 2}})
        self.assertEqual(status, 400)
        payload, status = self.service.api_update_settings(
            {"provider_limits": {"online:a": 0}})
        self.assertEqual(status, 400)


class ConcurrencyUiContractTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.html = (self.root / "public/index.html").read_text(encoding="utf-8")
        self.js = (self.root / "public/js/23-labeler.js").read_text(encoding="utf-8")
        self.css = (self.root / "public/styles.css").read_text(encoding="utf-8")
        self.http_py = (self.root / "naiba/http.py").read_text(encoding="utf-8")
        self.service_py = (self.root / "naiba/labeler/service.py").read_text(encoding="utf-8")

    def test_concurrency_controls_in_custom_page(self):
        for element_id in (
            "labelerConcurrencyEnabled", "labelerConcurrencyBody",
            "labelerSupervisorProvider", "labelerSupervisorModel",
            "labelerWorkerSlots", "labelerWorkerAdd", "labelerProviderLimitRows",
            "labelerMainModelRow", "labelerConcurrencyRow",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        # 槽位的删除按钮是动态生成的，靠 data 属性委托。
        self.assertIn("data-worker-remove", self.js)
        self.assertLess(self.html.index('data-labeler-page-body="custom"'),
                        self.html.index('id="labelerConcurrencyBlock"'))

    def test_submit_contract_and_visibility(self):
        self.assertIn("config.concurrency_enabled = true", self.js)
        self.assertIn("config.workers = workers", self.js)
        self.assertIn("config.supervisor = supervisor", self.js)
        self.assertIn("config.concurrency = workers.length || 1", self.js)
        self.assertIn("function collectWorkerRefs()", self.js)
        self.assertIn("function collectSupervisorRef()", self.js)
        self.assertIn("function syncConcurrencyUi()", self.js)
        self.assertIn("mainRow.hidden = enabled", self.js)
        self.assertIn("concurrencyRow.hidden = enabled", self.js)
        self.assertIn("labeler.workers.length >= MAX_WORKERS", self.js)

    def test_review_and_human_check_rendering(self):
        self.assertIn("supervising: \"监督中\"", self.js)
        self.assertIn("revising: \"退回重做\"", self.js)
        self.assertIn("human_check: \"需人工检查\"", self.js)
        self.assertIn("function reviewMetaLine(", self.js)
        self.assertIn("监督通过", self.js)
        self.assertIn("候选（未通过审核，仅供参考）", self.js)
        self.assertIn("需人工检查", self.js)
        self.assertIn("item.candidate", self.js)

    def test_settings_api_and_routes(self):
        self.assertIn('path == "/api/labeler/settings"', self.http_py)
        self.assertIn("def api_settings(", self.service_py)
        self.assertIn("def api_update_settings(", self.service_py)
        self.assertIn("validate_provider_limits", self.service_py)

    def test_hidden_state_css_wins_over_grid(self):
        # display:grid 会压过 UA 的 [hidden]（9.169 同款坑）。
        self.assertIn(".labeler-concurrency-body[hidden] { display: none; }", self.css)
        self.assertIn("#labelerMainModelRow[hidden], #labelerConcurrencyRow[hidden] { display: none; }", self.css)
        self.assertIn(".labeler-worker-slot", self.css)
        self.assertIn(".labeler-anno-candidate", self.css)


if __name__ == "__main__":
    unittest.main()
