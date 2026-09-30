# -*- coding: utf-8 -*-
"""用量统计「分析视图」升级的守门（2026-09-30）。

覆盖：
- 存储层：usage_stats 的 bucket 粒度参数（hour 桶键带 HH:00；by_day 恒天级；
  bucket=day 时 by_bucket 与 by_day 同内容；旧调用签名零破坏）；
- 端点：bucket/since/until 参数（hour 透传「桶×模型」行带 cost/priced/provider_name；
  非法值显式 400；自定义窗口覆盖 days、必须成对且 since<until）；
- 偏好：settings.usage_dash 四键白名单（默认值 / 部分更新合并 / 非法枚举显式报错 /
  重启存活），与 sidebar 同一套策略；
- 前端静态结构：KPI 六卡 / 工具行 / 六个图表容器 / 筛选与偏好弹窗都在，旧 id
  （usageToday / usageModelBody / usageGroupCol 等四卡与旧表结构）不许残留；
  手机端弹层的可视视口监听与 CSS dvh 兜底不许被删。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.app import NaibaChatApp
from naiba.config import ConfigStore
from naiba.storage.store import ChatStorage


class UsageDashBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.config = ConfigStore(Path(self.tmp.name) / "config.json")
        self.app = NaibaChatApp.__new__(NaibaChatApp)
        self.app.storage = self.storage
        self.app.config = self.config

    def seed_two_hours(self) -> tuple[int, int]:
        """相邻两个整点各记一条用量，返回两个桶起点（毫秒）。

        两个 model_key 故意一个可对上供应商、一个是孤儿键：存储层测试只看桶键，
        端点测试用它同时验「已定价 / 未定价」两条路径。子类若在 setUp 里建了
        self.provider，第一条就用它的真实 model_key。
        """
        base = int(time.time() * 1000)
        hour_ms = 3_600_000
        t0 = base - base % hour_ms  # 当前整点
        t1 = t0 - hour_ms           # 上一整点
        priced_key = getattr(self, "provider", {}).get("model_key", "online:a") if isinstance(getattr(self, "provider", None), dict) else "online:a"
        self.storage.record_usage({
            "run_id": "h1", "model_key": priced_key, "model_name": "A",
            "requests": 1, "input_tokens": 10, "cached_tokens": 4,
            "output_tokens": 1, "total_tokens": 11, "created_at": t0,
        })
        self.storage.record_usage({
            "run_id": "h2", "model_key": "online:orphan", "model_name": "B",
            "requests": 2, "input_tokens": 20, "cached_tokens": 0,
            "output_tokens": 2, "total_tokens": 22, "created_at": t1,
        })
        return t1, t0


class StorageBucketTests(UsageDashBase):
    """usage_stats 的 bucket 参数（分析视图的数据底座）。"""

    def test_hour_bucket_keys_have_hour_suffix(self) -> None:
        self.seed_two_hours()
        stats = self.storage.usage_stats(0, bucket="hour")
        # 小时桶键形如 YYYY-MM-DD HH:00；两个整点两行（桶×模型各一行）。
        keys = {row["day"] for row in stats["by_bucket"]}
        self.assertEqual(len(keys), 2)
        for key in keys:
            self.assertRegex(key, r"^\d{4}-\d{2}-\d{2} \d{2}:00$")
        # 行仍是「桶×模型」粒度（不做计价折叠，折叠是 app 层职责）。
        self.assertEqual(len(stats["by_bucket"]), 2)

    def test_by_day_stays_daily_regardless_of_bucket(self) -> None:
        self.seed_two_hours()
        stats = self.storage.usage_stats(0, bucket="hour")
        for row in stats["by_day"]:
            self.assertRegex(row["day"], r"^\d{4}-\d{2}-\d{2}$", "by_day 必须保持天级键")

    def test_day_bucket_reuses_by_day_rows(self) -> None:
        self.seed_two_hours()
        stats = self.storage.usage_stats(0, bucket="day")
        self.assertEqual(
            [(r["day"], r["model_key"], r["input_tokens"]) for r in stats["by_bucket"]],
            [(r["day"], r["model_key"], r["input_tokens"]) for r in stats["by_day"]],
        )

    def test_legacy_signature_still_works(self) -> None:
        """不传 bucket 的旧调用（today 卡等）零破坏：响应键齐全、默认天级。"""
        self.seed_two_hours()
        stats = self.storage.usage_stats(0)
        for key in ("totals", "by_model", "by_day", "by_bucket", "first_record_at"):
            self.assertIn(key, stats)
        self.assertEqual(len(stats["by_bucket"]), len(stats["by_day"]))


class EndpointBucketTests(UsageDashBase):
    """/api/usage/stats 的 bucket / since / until 参数。"""

    def setUp(self) -> None:
        super().setUp()
        self.provider = self.config.upsert_model_profile({
            "name": "DS", "base_url": "https://a", "model": "m1", "api_key": "k",
            "kind": "online", "request_format": "openai_chat",
            "pricing": {"input_per_million": 2, "cached_input_per_million": 0.4, "output_per_million": 8},
        })

    def test_hour_bucket_rows_carry_cost_and_provider(self) -> None:
        self.seed_two_hours()
        payload, _ = self.app.api_usage_stats({"days": ["2"], "bucket": ["hour"]})
        self.assertEqual(payload["bucket"], "hour")
        rows = payload["by_bucket"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("cost", row)
            self.assertIn("priced", row)
            self.assertIn("provider_name", row)
        priced_rows = [row for row in rows if row["model_key"] == self.provider["model_key"]]
        self.assertTrue(priced_rows[0]["priced"])
        # cost_for 内部按 round(amount, 6) 收敛（微小金额在 6 位小数量级）。
        self.assertEqual(priced_rows[0]["cost"], {"amount": round((6 * 2 + 4 * 0.4 + 1 * 8) / 1_000_000, 6), "currency": "¥"})
        unpriced_rows = [row for row in rows if row["model_key"] == "online:orphan"]
        self.assertIsNone(unpriced_rows[0]["cost"])
        self.assertFalse(unpriced_rows[0]["priced"])
        self.assertEqual(unpriced_rows[0]["provider_name"], "已删除的 API")

    def test_bucket_response_keeps_legacy_keys(self) -> None:
        self.seed_two_hours()
        payload, _ = self.app.api_usage_stats({"days": ["1"], "bucket": ["hour"]})
        for key in ("days", "since", "totals", "today", "by_model", "by_provider", "by_day", "first_record_at"):
            self.assertIn(key, payload, f"新契约不许挤掉旧键 {key}")
        self.assertEqual(payload["until"], 0)  # days 窗口 = 无上界
        # by_day 仍是折叠后的天级单行（多币种 costs 数组形态）。
        self.assertIsInstance(payload["by_day"][0]["costs"], list)

    def test_invalid_bucket_rejected(self) -> None:
        self.seed_two_hours()
        payload, status = self.app.api_usage_stats({"bucket": ["week"]})
        self.assertEqual(int(status), 400)
        self.assertEqual(payload, {"error": "bucket 必须是 day 或 hour"})

    def test_custom_window_overrides_days(self) -> None:
        t1, t0 = self.seed_two_hours()
        # 窗口只含最新一个整点：totals 只剩 online:a 那条。
        payload, _ = self.app.api_usage_stats({
            "since": [str(t0)], "until": [str(t0 + 60_000)], "bucket": ["hour"],
        })
        self.assertEqual(payload["totals"]["turns"], 1)
        self.assertEqual(payload["totals"]["input_tokens"], 10)
        self.assertEqual(int(payload["since"]), t0)
        self.assertEqual(int(payload["until"]), t0 + 60_000)
        self.assertEqual(len(payload["by_bucket"]), 1)

    def test_custom_window_validation(self) -> None:
        t1, t0 = self.seed_two_hours()
        cases = (
            {"since": [str(t0)]},                       # 只给 since
            {"until": [str(t0)]},                       # 只给 until
            {"since": [str(t0)], "until": [str(t0)]},   # since >= until
            {"since": ["abc"], "until": [str(t0)]},     # 非整数
            {"since": ["0"], "until": [str(t0)]},       # since <= 0
        )
        for params in cases:
            with self.subTest(params=params):
                payload, status = self.app.api_usage_stats(params)
                self.assertEqual(int(status), 400)
                self.assertIn("error", payload)

    def test_days_still_clamped_for_default_window(self) -> None:
        self.seed_two_hours()
        self.assertEqual(self.app.api_usage_stats({"days": ["99999"]})[0]["days"], 365)


class UsageDashPrefsTests(UsageDashBase):
    """settings.usage_dash 四键白名单（与 sidebar 同款策略）。"""

    def test_defaults_and_partial_merge(self) -> None:
        self.config.update_settings({"usage_dash": {"range": "7"}})
        self.assertEqual(
            self.config.public()["usage_dash"],
            {"range": "7", "gran": "hour", "chart": "bar", "metric": "cost"},
        )
        self.config.update_settings({"usage_dash": {"gran": "day", "chart": "area", "metric": "tokens"}})
        self.assertEqual(
            self.config.public()["usage_dash"],
            {"range": "7", "gran": "day", "chart": "area", "metric": "tokens"},
        )

    def test_invalid_values_raise(self) -> None:
        for bad in (
            {"range": "365"}, {"range": ""}, {"gran": "minute"}, {"chart": "pie"},
            {"metric": "usd"}, {"hacker": True}, "not-a-dict",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.config.update_settings({"usage_dash": bad})

    def test_survives_restart(self) -> None:
        self.config.update_settings({"usage_dash": {"range": "today", "gran": "day"}})
        reopened = ConfigStore(Path(self.tmp.name) / "config.json")
        self.assertEqual(
            reopened.public()["usage_dash"],
            {"range": "today", "gran": "day", "chart": "bar", "metric": "cost"},
        )


class UsageDashFrontendTests(unittest.TestCase):
    """前端静态结构守门：新结构在、旧结构走、手机端弹层适配在。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        cls.settings_js = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        cls.bind_js = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_kpi_cards_and_toolbar_present(self) -> None:
        for element_id in (
            "usageKpiCalls", "usageKpiCost", "usageKpiTokens", "usageKpiRpm",
            "usageKpiTpm", "usageKpiCache",
            "usageRangeChips", "usageDimSeg", "usageGranSel", "usageChartSeg", "usageMetricSeg",
            "usageFilterOpen", "usagePrefsOpen",
        ):
            self.assertIn(f'id="{element_id}"', self.index, f"用量分析缺少 #{element_id}")

    def test_chart_containers_and_dialogs_present(self) -> None:
        for element_id in (
            "usageDistBox", "usageTrendBox", "usageProvBox", "usageCacheBox", "usageSankeyBox",
            "usageDetailHead", "usageDetailBody",
            "usageFilterDialog", "usagePrefsDialog",
        ):
            self.assertIn(f'id="{element_id}"', self.index, f"用量分析缺少 #{element_id}")

    def test_legacy_ids_are_gone(self) -> None:
        # 四卡（今天/7天/30天/累计）与旧表结构被 KPI + 分析图取代：旧 id 不许残留，
        # 否则就是「新旧两套 DOM 并存」，冒烟与真实页面会各说各话。
        for legacy in ("usageToday", "usageWeek", "usageMonth", "usageTotal",
                       "usageModelBody", "usageDayBody", "usageGroupCol", "usageGroupModel"):
            self.assertNotIn(legacy, self.index)
            self.assertNotIn(legacy, self.settings_js)
            self.assertNotIn(legacy, self.bind_js)

    def test_visual_viewport_adaptation_present(self) -> None:
        # 手机端弹层适配（用户点名的重点）：打开弹窗期间监听 vv resize/scroll、
        # 把 max-height 压进可视区；CSS 侧用 dvh 兜底普通小屏。两头都不可删。
        self.assertIn("window.visualViewport", self.settings_js)
        self.assertIn("usageBindDialogViewport", self.settings_js)
        self.assertIn("usageSyncDialogBounds", self.settings_js)
        self.assertIn("usageUnbindDialogViewport", self.bind_js, "原生 close 事件必须摘监听")
        self.assertIn("calc(100dvh", self.styles)

    def test_settings_keys_wired(self) -> None:
        # 偏好走服务端 settings.usage_dash（与 sidebar 同款），前后端键名对齐。
        self.assertIn("usage_dash", self.settings_js)
        self.assertIn("{ usage_dash:", self.settings_js)  # POST body 的键


if __name__ == "__main__":
    unittest.main()
