# -*- coding: utf-8 -*-
"""护栏：消息全文搜索（`GET /api/search/messages` + `ChatStorage.search_messages`）。

保护对象：
- 空 `q` 必须 400 —— 空查询会白扫一遍全库且结果毫无意义；
- `conversation_id` 指定但**不存在**必须 404 —— 否则前端会把「这个会话搜不到」
  误读成「搜不到」；
- `limit` 非整数 400；越界**夹紧**到 [1, 50] 而不报错（它是 UI 的展示意图，不是契约）；
- `truncated` 必须显式标注截断，绝不静默丢弃：`returned` / `total_hits` / `truncated`
  三者必须自洽（这是「不静默丢结果」的判据）；
- 每条 `snippet` 必须是**原文的连续子串**且含命中词 —— 前端据此高亮，不再回查正文；
- 限定时段的查询只扫该会话，全库查询才跨会话；
- 路由 `GET /api/search/messages` 在 `http.py` 注册。

为什么用 stub app 跑判定层：`api_search_messages` 只依赖 `self.storage`，
所以 `SimpleNamespace(storage=...)` 即可直接覆盖「解析 → 判定 → 返回 (payload, status)」
的分工，不需要起 HTTP 服务（与 `test_first_turn` 的 `_first_turn_info` 同法）。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.app import NaibaChatApp, _query_first  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _stub(storage: ChatStorage) -> SimpleNamespace:
    """app 层判定只用到 storage —— 与 `test_first_turn` 的 stub 同口径。"""
    return SimpleNamespace(storage=storage)


class MessageSearchApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_msgsearch_")
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.app = _stub(self.storage)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, title, pairs):
        conversation = self.storage.create_conversation(title=title)
        for role, content in pairs:
            self.storage.add_message(conversation["id"], role, content)
        return conversation

    def _search(self, **params):
        return NaibaChatApp.api_search_messages(self.app, params)

    # ---- 校验分支 -------------------------------------------------------

    def test_empty_query_is_400(self):
        payload, status = self._search(q="")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_whitespace_only_query_is_400(self):
        payload, status = self._search(q="   ")
        self.assertEqual(status, 400, "空白串与空串同口径，不能漏网")

    def test_limit_must_be_integer(self):
        self._seed("会话", [("user", "前缀缓存机制")])
        payload, status = self._search(q="缓存", limit="abc")
        self.assertEqual(status, 400)
        self.assertIn("limit", payload.get("error", ""))

    def test_unknown_conversation_scope_is_404(self):
        self._seed("会话", [("user", "前缀缓存机制")])
        payload, status = self._search(q="缓存", conversation_id="does-not-exist")
        self.assertEqual(status, 404, "给了不存在的会话要 404，不能静默返回空结果")
        self.assertIn("error", payload)

    def test_missing_conversation_scope_is_global_200(self):
        self._seed("会话", [("user", "前缀缓存机制")])
        payload, status = self._search(q="缓存")
        self.assertEqual(status, 200, "不限定会话 = 全库搜索，不是错误")
        self.assertEqual(payload["conversation_id"], "")

    # ---- 命中与片段 -----------------------------------------------------

    def test_hits_snippet_and_totals_are_consistent(self):
        conversation = self._seed("缓存研究", [
            ("user", "我们来聊聊前缀缓存" + "前" * 80 + "命中词在这里"),
            ("assistant", "好的，前缀缓存的关键是逐字节一致"),
            ("user", "无关内容"),
        ])
        payload, status = self._search(q="缓存")
        self.assertEqual(status, 200)
        self.assertEqual(payload["returned"], len(payload["hits"]))
        self.assertGreaterEqual(payload["total_hits"], payload["returned"])
        self.assertEqual(
            payload["truncated"], payload["total_hits"] > len(payload["hits"]),
            "truncated 必须精确等于「总数超过返回数」，否则就是静默丢弃",
        )
        contents = {
            message["id"]: message["content"]
            for message in self.storage.get_conversation(conversation["id"])["messages"]
        }
        for hit in payload["hits"]:
            with self.subTest(message_id=hit["message_id"]):
                self.assertIn("缓存", hit["snippet"], "片段必须含命中词")
                core = hit["snippet"].strip("…")
                self.assertIn(core, contents[hit["message_id"]],
                              "snippet 必须是原文的连续子串（前端高亮不复查正文）")
                self.assertEqual(hit["conversation_id"], conversation["id"])
                self.assertTrue(hit["conversation_title"])

    def test_snippet_is_clipped_around_hit(self):
        """命中词前后的上下文都被裁掉时，两端要有省略号。"""
        content = "甲" * 200 + "命中锚点" + "乙" * 200
        self._seed("长文", [("user", content)])
        payload, status = self._search(q="命中锚点")
        self.assertEqual(status, 200)
        snippet = payload["hits"][0]["snippet"]
        self.assertTrue(snippet.startswith("…") and snippet.endswith("…"),
                        "两侧都被裁时必须带省略号")
        self.assertIn("命中锚点", snippet)

    def test_match_is_case_insensitive(self):
        self._seed("英文", [("user", "Prefix Cache Matters")])
        payload, status = self._search(q="prefix cache")
        self.assertEqual(status, 200)
        self.assertEqual(payload["returned"], 1, "instr(lower()) 大小写不敏感")

    # ---- 截断 -----------------------------------------------------------

    def test_limit_truncation_is_flagged(self):
        for index in range(5):
            self._seed(f"会话{index}", [("user", f"缓存命中第 {index} 条")])
        payload, status = self._search(q="缓存命中", limit=2)
        self.assertEqual(status, 200)
        self.assertEqual(payload["returned"], 2)
        self.assertEqual(payload["total_hits"], 5)
        self.assertTrue(payload["truncated"], "被截断必须显式标注")

    def test_limit_is_clamped_not_rejected(self):
        self._seed("会话", [("user", "缓存")])
        for raw in ("0", "-3", "9999"):
            with self.subTest(limit=raw):
                payload, status = self._search(q="缓存", limit=raw)
                self.assertEqual(status, 200, "越界 limit 是夹紧而非报错")
                self.assertGreaterEqual(payload["limit"], 1)
                self.assertLessEqual(payload["limit"], 50)

    def test_non_truncated_when_all_fit(self):
        self._seed("会话", [("user", "缓存"), ("assistant", "缓存")])
        payload, status = self._search(q="缓存")
        self.assertEqual(status, 200)
        self.assertFalse(payload["truncated"])

    # ---- 范围 -----------------------------------------------------------

    def test_scoped_search_only_scans_that_conversation(self):
        target = self._seed("目标", [("user", "缓存甲")])
        self._seed("其它", [("user", "缓存乙"), ("assistant", "缓存丙")])
        payload, status = self._search(q="缓存", conversation_id=target["id"])
        self.assertEqual(status, 200)
        self.assertEqual(payload["total_hits"], 1)
        self.assertTrue(all(hit["conversation_id"] == target["id"] for hit in payload["hits"]))

    def test_global_search_spans_conversations(self):
        self._seed("甲", [("user", "缓存甲")])
        self._seed("乙", [("user", "缓存乙")])
        payload, status = self._search(q="缓存")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total_hits"], 2)
        self.assertEqual(len({hit["conversation_id"] for hit in payload["hits"]}), 2)

    def test_hit_carries_ordinal_and_role(self):
        conversation = self._seed("会话", [("user", "第一"), ("assistant", "缓存回复")])
        payload, _ = self._search(q="缓存回复")
        hit = payload["hits"][0]
        self.assertEqual(hit["role"], "assistant")
        self.assertGreaterEqual(hit["ordinal"], 1, "序号供前端展示「第 N 条」")
        # 标题取库里的**当前**值：首条 user 消息会顶掉默认标题（add_message 的既有行为）。
        current = self.storage.get_conversation(conversation["id"], include_messages=False)["title"]
        self.assertEqual(hit["conversation_title"], current)
        self.assertEqual(hit["conversation_title"], "第一")

    def test_scope_counts_present_for_ui_hint(self):
        self._seed("会话", [("user", "缓存")])
        payload, _ = self._search(q="缓存")
        self.assertIn("scope", payload, "全库范围计数供 UI 显示搜索范围提示")

    # ---- 路由接线 -------------------------------------------------------

    def test_route_registered_and_ordered(self):
        http = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/search/messages"', http, "路由必须注册")
        self.assertIn("api_search_messages", http)
        self.assertIn("parse_qs(parsed.query)", http, "query 必须由传输层解析后交给 app 层")

    def test_query_first_handles_parse_qs_shape(self):
        """`_query_first` 是模块级纯函数：既吃 parse_qs 的 list 值，也吃裸串。"""
        self.assertEqual(_query_first({"q": ["缓存"]}, "q"), "缓存")
        self.assertEqual(_query_first({"q": ["缓存", "第二"]}, "q"), "缓存", "只取首个值")
        self.assertEqual(_query_first({"q": []}, "q"), "")
        self.assertEqual(_query_first({}, "q"), "")
        self.assertEqual(_query_first({"q": "裸串"}, "q"), "裸串")


if __name__ == "__main__":
    unittest.main()
