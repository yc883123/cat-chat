# -*- coding: utf-8 -*-
"""API Token 用量统计 + 费用统计（三档单价）的端到端守门。

覆盖（计划《API用量与费用统计》Step 7）：
- 台账记录：record_run_usage 四个接线点（chat 收尾 / subagent 收口 / 中断恢复 /
  空用量不落行）+ run_id 幂等（重记不翻倍）；
- Σ 口径：tokens_from_summary 按 requests_detail 逐次求和（区别于上下文圆环的
  「最后一次请求」口径；旧数据无明细退化顶层）；
- 聚合：usage_stats 的时间窗 / by_model / by_day(localtime) / model_key 过滤；
  端点 by_provider 维度（同供应商多模型合并 / 孤儿归「已删除的 API」/ 两份并存）；
- 单价：_provider_pricing 校验（非法显式报错、全空清除、public_providers 带出）；
- 计费合并：cost_for 三档口径、未定价 None、端点 costs 多币种不混加、unpriced_turns；
- 迁移 v21 幂等 + 端点契约（days 夹紧、响应键齐全）。

费用不落价格快照：查询时按当前单价重算（改价历史金额随新价重算）——
这是有意语义（维护说明 §五.6），测试里以「先定价记录、后改价重查」表达。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.core.usage_stats import cost_for, tokens_from_summary
from naiba.storage.store import CURRENT_SCHEMA_VERSION, ChatStorage


def _summary(*details: dict) -> dict:
    """构造带 requests_detail 的 usage 汇总（顶层给「末次口径」值模拟真实事件）。"""
    last = details[-1]
    return {
        "input_tokens": last["input_tokens"],
        "output_tokens": last["output_tokens"],
        "total_tokens": last.get("total_tokens", last["input_tokens"] + last["output_tokens"]),
        "cached_tokens": last.get("cached_tokens", 0),
        "requests": len(details),
        "requests_detail": [
            {**detail, "index": index + 1} for index, detail in enumerate(details)
        ],
    }


class UsageStatsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.config = ConfigStore(Path(self.tmp.name) / "config.json")
        self.app = NaibaChatApp.__new__(NaibaChatApp)
        self.app.storage = self.storage
        self.app.config = self.config
        self.conversation = self.storage.create_conversation(title="用量测试")


class RecordTests(UsageStatsBase):
    """记录点接线（Step 3）：四条路径共用 record_run_usage。"""

    def _make_run(self, kind: str = "chat", snapshot: dict | None = None) -> str:
        run = self.storage.create_run(
            self.conversation["id"],
            "消息",
            {"id": "", "name": "Agent", "system_prompt": "", "skill_ids": []},
            snapshot or {},
            kind=kind,
        )
        self.storage.update_background_task(str(run["id"]), status="completed", finished=True)
        return str(run["id"])

    def test_records_last_usage_event_and_is_idempotent(self) -> None:
        """从 run_events 最后一条 usage 事件落账；同 run 重记不翻倍。"""
        run_id = self._make_run(snapshot={"model_key": "online:x", "model_name": "mm"})
        self.storage.append_run_event(run_id, {"type": "usage", "usage": _summary(
            {"input_tokens": 100, "output_tokens": 10, "cached_tokens": 40},
            {"input_tokens": 200, "output_tokens": 20, "cached_tokens": 150},
        )})
        self.app.record_run_usage(run_id)
        stats = self.storage.usage_stats(0)
        # Σ 全请求（100+200 / 10+20），不是末次口径的 200/20。
        self.assertEqual(stats["totals"]["input_tokens"], 300)
        self.assertEqual(stats["totals"]["output_tokens"], 30)
        self.assertEqual(stats["totals"]["cached_tokens"], 190)
        self.assertEqual(stats["totals"]["requests"], 2)
        # 幂等：再记一次仍是同一条。
        self.app.record_run_usage(run_id)
        self.assertEqual(self.storage.usage_stats(0)["totals"]["turns"], 1)

    def test_dimensions_from_snapshot_and_task_row(self) -> None:
        run_id = self._make_run(snapshot={"model_key": "online:x", "model_name": "mm"})
        self.storage.append_run_event(run_id, {"type": "usage", "usage": _summary(
            {"input_tokens": 10, "output_tokens": 1},
        )})
        self.app.record_run_usage(run_id, message_id="m-1")
        row = self.storage.usage_stats(0)["by_model"][0]
        self.assertEqual(row["model_key"], "online:x")
        self.assertEqual(row["model_name"], "mm")

    def test_cancelled_and_failed_paths_share_the_same_recorder(self) -> None:
        """取消/失败路径的 usage 事件都已 flush 落库 → 同一函数收口，各记一条。"""
        for status in ("cancelled", "failed"):
            run_id = self._make_run(snapshot={"model_key": "online:x"})
            self.storage.append_run_event(run_id, {"type": "usage", "usage": _summary(
                {"input_tokens": 50, "output_tokens": 5},
            )})
            self.storage.update_background_task(run_id, status=status, finished=True)
            self.app.record_run_usage(run_id)
        stats = self.storage.usage_stats(0)
        self.assertEqual(stats["totals"]["turns"], 2)
        self.assertEqual(stats["totals"]["input_tokens"], 100)

    def test_run_without_usage_events_records_nothing(self) -> None:
        """首请求前即取消/失败：没有 usage 事件，不落空行。"""
        run_id = self._make_run()
        self.app.record_run_usage(run_id)
        self.assertEqual(self.storage.usage_stats(0)["totals"]["turns"], 0)

    def test_subagent_falls_back_to_conversation_model(self) -> None:
        """subagent 的 run 快照是 job_spec（无 model 字段）→ 从会话行回退维度。"""
        import sqlite3
        with self.storage._connect() as db:
            db.execute(
                "UPDATE conversations SET model_key = ?, model_name = ? WHERE id = ?",
                ("online:sub", "sub-mm", self.conversation["id"]),
            )
        job_id = self._make_run(kind="subagent", snapshot={"job_spec": {"kind": "subagent"}, "params": {}})
        self.storage.append_run_event(job_id, {"type": "usage", "usage": _summary(
            {"input_tokens": 30, "output_tokens": 5},
            {"input_tokens": 20, "output_tokens": 5},
        )})
        self.app.record_run_usage(job_id)
        stats = self.storage.usage_stats(0, model_key="online:sub")
        self.assertEqual(stats["totals"]["turns"], 1)
        self.assertEqual(stats["totals"]["requests"], 2)
        self.assertEqual(stats["totals"]["input_tokens"], 50)

    def test_interrupted_recovery_rerecord_is_idempotent(self) -> None:
        """中断恢复补记走同一函数：事件还在 run_events 里，幂等。"""
        run_id = self._make_run(snapshot={"model_key": "online:x"})
        self.storage.append_run_event(run_id, {"type": "usage", "usage": _summary(
            {"input_tokens": 70, "output_tokens": 7},
        )})
        self.app.record_run_usage(run_id, message_id="m-9")
        self.app.record_run_usage(run_id, message_id="m-9")
        stats = self.storage.usage_stats(0)
        self.assertEqual(stats["totals"]["turns"], 1)
        self.assertEqual(stats["totals"]["input_tokens"], 70)


class PureFunctionTests(unittest.TestCase):
    """Σ 与计费纯函数（Step 2）。"""

    def test_tokens_from_summary_sums_all_requests(self) -> None:
        summary = _summary(
            {"input_tokens": 500, "output_tokens": 30, "cached_tokens": 400},
            {"input_tokens": 300, "output_tokens": 40, "cached_tokens": 300},
            {"input_tokens": 200, "output_tokens": 50, "cached_tokens": 100},
        )
        tokens = tokens_from_summary(summary)
        self.assertEqual(tokens["input_tokens"], 1000)
        self.assertEqual(tokens["output_tokens"], 120)
        self.assertEqual(tokens["cached_tokens"], 800)
        self.assertEqual(tokens["uncached_tokens"], 200)
        self.assertEqual(tokens["requests"], 3)

    def test_tokens_without_details_fall_back_to_top_level(self) -> None:
        """旧数据无明细：退化把顶层当单次（口径注释写明仅供兼容）。"""
        tokens = tokens_from_summary({"input_tokens": 900, "output_tokens": 100, "cached_tokens": 800, "requests": 5})
        self.assertEqual(tokens["input_tokens"], 900)
        self.assertEqual(tokens["requests"], 5)
        self.assertEqual(tokens["uncached_tokens"], 100)
        self.assertEqual(tokens_from_summary(None)["input_tokens"], 0)

    def test_cost_for_three_tier_pricing(self) -> None:
        pricing = {"input_per_million": 2, "cached_input_per_million": 0.4, "output_per_million": 8, "currency": "¥"}
        tokens = {"input_tokens": 1_000_000, "cached_tokens": 500_000, "output_tokens": 100_000}
        # 0.5M 未命中×2 + 0.5M 命中×0.4 + 0.1M 输出×8 = 1.0 + 0.2 + 0.8
        self.assertEqual(cost_for(tokens, pricing), {"amount": 2.0, "currency": "¥"})

    def test_cost_for_unpriced_returns_none(self) -> None:
        tokens = {"input_tokens": 1000, "cached_tokens": 0, "output_tokens": 10}
        self.assertIsNone(cost_for(tokens, None))
        self.assertIsNone(cost_for(tokens, {}))
        self.assertIsNone(cost_for(tokens, {"currency": "¥"}))  # 三档全空 = 未定价

    def test_cost_for_clamps_cached_over_input(self) -> None:
        cost = cost_for(
            {"input_tokens": 100, "cached_tokens": 500},
            {"cached_input_per_million": 1},
        )
        self.assertEqual(cost["amount"], 0.0001)  # cached 夹紧到 input


class PricingConfigTests(unittest.TestCase):
    """provider.pricing 配置层（Step 4）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = ConfigStore(Path(self.tmp.name) / "config.json")
        self.base = {
            "name": "DS", "base_url": "https://api.example.com", "model": "m1",
            "api_key": "sk-1", "kind": "online", "request_format": "openai_chat",
        }

    def test_save_read_clear_cycle(self) -> None:
        saved = self.config.upsert_model_profile({
            **self.base,
            "pricing": {"input_per_million": "2", "cached_input_per_million": 0.4, "output_per_million": 8},
        })
        self.assertEqual(
            saved["pricing"],
            {"input_per_million": 2.0, "cached_input_per_million": 0.4, "output_per_million": 8.0, "currency": "¥"},
        )
        # 不带 pricing 键的更新保留旧值（旧客户端兼容）。
        self.config.upsert_model_profile({**self.base, "id": saved["id"], "name": "DS2"})
        provider = [p for p in self.config.public_providers() if p["id"] == saved["id"]][0]
        self.assertEqual(provider["pricing"]["input_per_million"], 2.0)
        # 带 pricing 键且三档全空 → 清除。
        self.config.upsert_model_profile({**self.base, "id": saved["id"], "pricing": {}})
        provider = [p for p in self.config.public_providers() if p["id"] == saved["id"]][0]
        self.assertNotIn("pricing", provider)

    def test_invalid_pricing_raises(self) -> None:
        bad_values = (
            {"input_per_million": "abc"}, {"input_per_million": -1},
            {"input_per_million": 1_000_001}, {"input_per_million": True},
            {"output_per_million": float("nan") if False else "x"},
        )
        for bad in bad_values:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.config.upsert_model_profile({**self.base, "pricing": bad})
        with self.assertRaises(ValueError):
            self.config.upsert_model_profile({**self.base, "pricing": {"currency": "123456789"}})

    def test_currency_defaults_and_survives_restart(self) -> None:
        saved = self.config.upsert_model_profile({
            **self.base, "pricing": {"output_per_million": 3, "currency": " USD "},
        })
        self.assertEqual(saved["pricing"]["currency"], "USD")
        reopened = ConfigStore(Path(self.tmp.name) / "config.json")
        provider = reopened.public_providers()[0]
        self.assertEqual(provider["pricing"], {"output_per_million": 3.0, "currency": "USD"})


class EndpointTests(UsageStatsBase):
    """统计端点（Step 5）：计费合并 / 夹紧 / 契约。"""

    def _seed(self) -> str:
        priced = self.config.upsert_model_profile({
            "name": "DS", "base_url": "https://a", "model": "m1", "api_key": "k",
            "kind": "online", "request_format": "openai_chat",
            "pricing": {"input_per_million": 2, "cached_input_per_million": 0.4, "output_per_million": 8},
        })
        self.config.upsert_model_profile({
            "name": "Mimo", "base_url": "https://b", "model": "m2", "api_key": "k",
            "kind": "online", "request_format": "openai_chat",
        })
        now = int(time.time() * 1000)
        self.storage.record_usage({
            "run_id": "r-priced", "conversation_id": self.conversation["id"], "kind": "chat",
            "model_key": priced["model_key"], "model_name": "m1",
            "requests": 2, "input_tokens": 1_000_000, "cached_tokens": 500_000,
            "output_tokens": 100_000, "total_tokens": 1_100_000, "created_at": now,
        })
        self.storage.record_usage({
            "run_id": "r-free", "conversation_id": self.conversation["id"], "kind": "chat",
            "model_key": "online:none", "model_name": "m2",
            "requests": 1, "input_tokens": 10_000, "cached_tokens": 0,
            "output_tokens": 1_000, "total_tokens": 11_000, "created_at": now,
        })
        return priced["model_key"]

    def test_endpoint_contract_and_mixed_pricing(self) -> None:
        priced_key = self._seed()
        payload, _ = self.app.api_usage_stats({"days": ["30"]})
        for key in ("days", "since", "totals", "today", "by_model", "by_day", "first_record_at"):
            self.assertIn(key, payload)
        self.assertEqual(payload["days"], 30)
        totals = payload["totals"]
        self.assertEqual(totals["turns"], 2)
        self.assertEqual(totals["input_tokens"], 1_010_000)
        # 已定价 2.0（¥）+ 未定价 0 → costs 只有一行 ¥，unpriced_turns=1。
        self.assertEqual(totals["costs"], [{"currency": "¥", "amount": 2.0}])
        self.assertEqual(totals["unpriced_turns"], 1)
        rows = {row["model_key"]: row for row in payload["by_model"]}
        self.assertEqual(rows[priced_key]["cost"]["amount"], 2.0)
        self.assertTrue(rows[priced_key]["priced"])
        self.assertIsNone(rows["online:none"]["cost"])
        self.assertFalse(rows["online:none"]["priced"])

    def test_multi_currency_costs_are_not_merged(self) -> None:
        """两个供应商分属两币种：costs 按币种分行，绝不混加。"""
        priced_key = self._seed()
        mimo = next(
            (p for p in self.config.public_providers() if p["name"] == "Mimo"), None
        )
        self.assertIsNotNone(mimo)
        self.config.upsert_model_profile({
            "id": mimo["id"], "name": "Mimo", "base_url": "https://b", "model": "m2",
            "api_key": "k", "kind": "online", "request_format": "openai_chat",
            "pricing": {"input_per_million": 5, "currency": "$"},
        })
        now = int(time.time() * 1000)
        self.storage.record_usage({
            "run_id": "r-mimo", "conversation_id": self.conversation["id"], "kind": "chat",
            "model_key": f"online:{mimo['id']}", "model_name": "m2",
            "requests": 1, "input_tokens": 100_000, "cached_tokens": 0,
            "output_tokens": 10_000, "total_tokens": 110_000, "created_at": now,
        })
        payload, _ = self.app.api_usage_stats({})
        # DS（¥）：0.5M 未命中×2 + 0.5M 命中×0.4 + 0.1M 输出×8 = 2.0；
        # Mimo（$）：0.1M 未命中×5 = 0.5（未设档按 0 计）。
        self.assertEqual(
            payload["totals"]["costs"],
            [{"currency": "$", "amount": 0.5}, {"currency": "¥", "amount": 2.0}],
        )
        # r-free 仍指向孤儿 key online:none（provider 列表里无此 id）→ 1 轮未定价。
        # 这同时验证「供应商配置被改 id/删除后，历史用量自动落入未定价」的语义。
        self.assertEqual(payload["totals"]["unpriced_turns"], 1)

    def test_by_provider_merges_models_of_same_provider(self) -> None:
        """同一供应商的两个具体模型：by_model 两行，by_provider 合并成一行。"""
        priced_key = self._seed()  # DS（已定价 ¥）：m1 记录在 r-priced，费用 ¥2.0
        now = int(time.time() * 1000)
        # 同一 DS 供应商下再记一个具体模型 m3：0.5M 未命中×2 + 0.05M 输出×8 = ¥1.4。
        self.storage.record_usage({
            "run_id": "r-ds-m3", "conversation_id": self.conversation["id"], "kind": "chat",
            "model_key": priced_key, "model_name": "m3",
            "requests": 1, "input_tokens": 500_000, "cached_tokens": 0,
            "output_tokens": 50_000, "total_tokens": 550_000, "created_at": now,
        })
        payload, _ = self.app.api_usage_stats({})
        self.assertEqual(len(payload["by_model"]), 3)  # DS/m1、DS/m3、孤儿 m2
        self.assertEqual(len(payload["by_provider"]), 2)
        ds = next(r for r in payload["by_provider"] if r["provider_name"] == "DS")
        self.assertEqual(ds["provider_id"], priced_key.split(":", 1)[1])
        self.assertEqual(ds["turns"], 2)
        self.assertEqual(ds["requests"], 3)
        self.assertEqual(ds["input_tokens"], 1_500_000)
        self.assertEqual(ds["total_tokens"], 1_650_000)
        # 同币种费用累加：2.0（m1）+ 1.4（m3）。
        self.assertEqual(ds["costs"], [{"currency": "¥", "amount": 3.4}])
        self.assertTrue(ds["priced"])

    def test_by_provider_orphan_key_lands_in_deleted_bucket(self) -> None:
        """供应商已删/改 id 的孤儿 model_key：by_provider 归「已删除的 API」，未定价。"""
        self._seed()
        payload, _ = self.app.api_usage_stats({})
        orphan = next(
            (r for r in payload["by_provider"] if r["provider_name"] == "已删除的 API"), None
        )
        self.assertIsNotNone(orphan)
        self.assertEqual(orphan["provider_id"], "none")  # online:none 的 provider 段
        self.assertEqual(orphan["turns"], 1)
        self.assertEqual(orphan["input_tokens"], 10_000)
        self.assertFalse(orphan["priced"])
        self.assertEqual(orphan["costs"], [])

    def test_by_model_and_by_provider_coexist(self) -> None:
        """响应恒带两份聚合：形状互不影响，model_key 过滤对两份同时收窄。"""
        priced_key = self._seed()
        payload, _ = self.app.api_usage_stats({})
        self.assertEqual(len(payload["by_model"]), 2)
        self.assertEqual(len(payload["by_provider"]), 2)
        # by_provider 行：聚合字段 + costs 数组 + priced；没有 by_model 特有的单值 cost 键。
        provider_row = payload["by_provider"][0]
        for key in ("provider_id", "provider_name", "turns", "requests", "input_tokens",
                    "cached_tokens", "output_tokens", "total_tokens", "uncached_tokens",
                    "costs", "priced"):
            self.assertIn(key, provider_row)
        self.assertNotIn("cost", provider_row)
        self.assertIn("cost", payload["by_model"][0])
        # 排序：按 total_tokens 倒序（DS 1.1M > 孤儿 11k）。
        self.assertEqual(
            [r["provider_name"] for r in payload["by_provider"]], ["DS", "已删除的 API"]
        )
        filtered, _ = self.app.api_usage_stats({"model_key": [priced_key]})
        self.assertEqual([r["provider_name"] for r in filtered["by_provider"]], ["DS"])

    def test_days_clamped_and_error(self) -> None:
        self._seed()
        self.assertEqual(self.app.api_usage_stats({"days": ["99999"]})[0]["days"], 365)
        self.assertEqual(self.app.api_usage_stats({"days": ["0"]})[0]["days"], 1)
        self.assertEqual(self.app.api_usage_stats({})[0]["days"], 30)
        self.assertEqual(self.app.api_usage_stats({"days": ["abc"]})[0], {"error": "days 必须是整数"})

    def test_model_key_filter_and_today_bucket(self) -> None:
        priced_key = self._seed()
        payload, _ = self.app.api_usage_stats({"days": ["30"], "model_key": [priced_key]})
        self.assertEqual(payload["totals"]["turns"], 1)
        self.assertEqual(payload["totals"]["input_tokens"], 1_000_000)
        self.assertIn("turns", payload["today"])
        self.assertIn("costs", payload["today"])

    def test_cost_uses_current_pricing_not_a_snapshot(self) -> None:
        """费用恒按当前单价重算：先按 2/百万 记账，改价 4/百万 后历史金额翻倍。"""
        priced_key = self._seed()
        before, _ = self.app.api_usage_stats({})
        self.assertEqual(before["totals"]["costs"][0]["amount"], 2.0)
        self.config.upsert_model_profile({
            "id": priced_key.split(":", 1)[1], "name": "DS", "base_url": "https://a", "model": "m1",
            "api_key": "k", "kind": "online", "request_format": "openai_chat",
            "pricing": {"input_per_million": 4, "cached_input_per_million": 0.8, "output_per_million": 16},
        })
        after, _ = self.app.api_usage_stats({})
        self.assertEqual(after["totals"]["costs"][0]["amount"], 4.0)


class StorageLayerTests(UsageStatsBase):
    """存储层细节：by_day 粒度、窗口过滤、迁移幂等。"""

    def test_by_day_keeps_model_granularity_and_localtime(self) -> None:
        day_start = time.mktime(time.strptime("2026-09-20", "%Y-%m-%d"))
        ms_a = int((day_start + 3 * 3600) * 1000)   # 当天凌晨
        ms_b = int((day_start + 30 * 3600) * 1000)  # 次日
        self.storage.record_usage({
            "run_id": "d1", "model_key": "online:a", "model_name": "A",
            "requests": 1, "input_tokens": 10, "output_tokens": 1, "total_tokens": 11,
            "created_at": ms_a,
        })
        self.storage.record_usage({
            "run_id": "d2", "model_key": "online:b", "model_name": "B",
            "requests": 1, "input_tokens": 20, "output_tokens": 2, "total_tokens": 22,
            "created_at": ms_a,
        })
        self.storage.record_usage({
            "run_id": "d3", "model_key": "online:a", "model_name": "A",
            "requests": 1, "input_tokens": 30, "output_tokens": 3, "total_tokens": 33,
            "created_at": ms_b,
        })
        stats = self.storage.usage_stats(0)
        days = {(row["day"], row["model_key"]): row for row in stats["by_day"]}
        self.assertEqual(len(stats["by_day"]), 3)
        self.assertEqual(days[("2026-09-20", "online:a")]["input_tokens"], 10)
        self.assertEqual(days[("2026-09-20", "online:b")]["input_tokens"], 20)
        self.assertEqual(days[("2026-09-21", "online:a")]["input_tokens"], 30)

    def test_window_filters(self) -> None:
        self.storage.record_usage({
            "run_id": "w1", "requests": 1, "input_tokens": 10, "output_tokens": 1,
            "total_tokens": 11, "created_at": 1000,
        })
        self.storage.record_usage({
            "run_id": "w2", "requests": 1, "input_tokens": 20, "output_tokens": 2,
            "total_tokens": 22, "created_at": 3000,
        })
        stats = self.storage.usage_stats(2000)
        self.assertEqual(stats["totals"]["turns"], 1)
        self.assertEqual(stats["totals"]["input_tokens"], 20)
        stats = self.storage.usage_stats(0, until_ms=2000)
        self.assertEqual(stats["totals"]["input_tokens"], 10)
        stats = self.storage.usage_stats(0, until_ms=99999)
        self.assertEqual(stats["totals"]["turns"], 2)

    def test_migration_v21_idempotent(self) -> None:
        self.assertEqual(CURRENT_SCHEMA_VERSION, 21)
        # 同一库重复打开：迁移幂等，数据保留。
        self.storage.record_usage({
            "run_id": "m1", "requests": 1, "input_tokens": 10, "output_tokens": 1,
            "total_tokens": 11, "created_at": int(time.time() * 1000),
        })
        reopened = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.assertEqual(reopened.usage_stats(0)["totals"]["turns"], 1)


if __name__ == "__main__":
    unittest.main()
