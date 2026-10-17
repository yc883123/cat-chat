# -*- coding: utf-8 -*-
"""守门：skills 事件的 frozen 字段（固化名单 vs 本轮追加的分组标记）。

保护对象（docs/skill-frozen-appended-badge-plan.md §2.2）：
- ``SkillAgent.run()`` 发出的 ``skills`` 事件按集合归属带 ``frozen`` 标记：
  冻结集成员（policy["skill_ids"]，首轮固化全程有效）为 true，
  本轮 /ref 引用但不在冻结集内的（referenced_ids 追加，尾部指令注入）为 false；
  两组都随 trace 全程留在上下文，frozen 只表达注入位置（用户确认的语义，2026-10-17）；
- ``source`` 字段保留为 "user" 不动（旧前端/既有消费方兼容）；
- 事件结构只加字段不改形态：entries 是带 id/name/source/frozen 的 dict 列表。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.skills.policy import normalize_skill_policy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class SkillPolicyFrozenSplitTests(unittest.TestCase):
    """按 run() 的同口径（normalize 后按 id 归属）断言 frozen 分组。"""

    def _entries(self, skill_ids, referenced_ids):
        catalog = [{"id": item, "name": item, "ref": item} for item in ("A", "B", "C")]
        policy = normalize_skill_policy(
            {"mode": "exclusive", "skill_ids": skill_ids, "referenced_ids": referenced_ids},
            catalog=catalog,
        )
        frozen_set = {str(item) for item in policy["skill_ids"]}
        merged_ids = list(dict.fromkeys([*policy["skill_ids"], *(policy.get("referenced_ids") or [])]))
        return [
            {
                "id": skill_id,
                "source": "user",
                "frozen": str(skill_id) in frozen_set,
            }
            for skill_id in merged_ids
        ]

    def test_frozen_and_appended_are_distinguished(self):
        entries = self._entries(["A", "B"], ["B", "C"])
        by_id = {e["id"]: e for e in entries}
        self.assertTrue(by_id["A"]["frozen"])
        self.assertTrue(by_id["B"]["frozen"])
        self.assertFalse(by_id["C"]["frozen"])
        # 有序合并：冻结集在前，本轮新增在后
        self.assertEqual([e["id"] for e in entries], ["A", "B", "C"])

    def test_reference_overlapping_frozen_stays_frozen(self):
        entries = self._entries(["A"], ["A"])
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["frozen"])

    def test_pure_appended_has_no_frozen(self):
        entries = self._entries([], ["C"])
        self.assertEqual([e["frozen"] for e in entries], [False])

    def test_empty_policy_and_refs_yield_no_entries(self):
        self.assertEqual(self._entries([], []), [])

    def test_source_field_preserved(self):
        entries = self._entries(["A"], ["C"])
        self.assertTrue(all(e["source"] == "user" for e in entries))


if __name__ == "__main__":
    unittest.main()
