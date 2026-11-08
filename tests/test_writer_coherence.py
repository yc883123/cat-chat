# -*- coding: utf-8 -*-
"""护栏：织书台只读连贯性检查（跨章连贯性修复计划 §三.C、§五）。

覆盖：报告 schema 与证据（章节号 + 状态条目 + 正文原文片段）、四类发现的
命中与不命中、confirmed/tentative 区分、报告与正文版本绑定及过期判定、
只运行检查绝不修改章节正文或版本。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.writer import coherence  # noqa: E402
from naiba.writer.coherence import run_continuity_check, report_is_stale  # noqa: E402
from naiba.writer.service import WriterService  # noqa: E402
from naiba.writer.store import WriterStore  # noqa: E402


class _Jobs:
    def __init__(self):
        self.submitted: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.cancelled: list[str] = []

    def submit(self, input_data):
        task = {"id": f"st-{len(self.submitted) + 1:04d}", "status": "queued",
                "input": json.loads(json.dumps(input_data, ensure_ascii=False)),
                "title": input_data.get("title", ""), "result": "", "result_partial": "",
                "error": "", "finish_reason": ""}
        self.submitted.append(task["input"])
        self.tasks[task["id"]] = task
        return task

    def task(self, task_id):
        return self.tasks.get(task_id)

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        task = self.tasks.get(task_id)
        if task is not None and task["status"] in ("queued", "running"):
            task["status"] = "cancelled"
            return True
        return False


class _Config:
    def model_profiles(self):
        return [{"model_key": "online:probe", "model": "probe", "context_window": 100000}]


class _Paths:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _App:
    def __init__(self, data_dir):
        self.paths = _Paths(data_dir)
        self.config = _Config()
        self.studio = type("S", (), {"jobs": None})()
        self.studio.jobs = _Jobs()


class CoherenceTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = WriterStore(self.root)
        self.book_id = self.store.create_book(title="连贯测")["id"]
        self.store.save_setup(self.book_id, content={
            "idea": "创意",
            "characters": [
                {"name": "张三", "role": "lead", "identity": "船匠"},
                {"name": "李四", "role": "side", "alias": "李师傅"},
                {"name": "王五", "role": "side"},
            ],
        }, kind="manual")
        rows = [{"no": no, "title": f"第{no}章", "beat": "梗概", "hook": ""}
                for no in range(1, 5)]
        self.store.save_outline(self.book_id, chapters=rows, kind="generated", sync=True)
        self.contents = {
            1: "第一章：张三在码头遇见李四，两人打了一架。",
            2: "第二章：张三与李四在荒庙结盟，王五把残图交给了他们。",
            3: "第三章：李四独自去了北境，神秘人尾随其后。",
            4: "第四章：王五在北境渡口摆渡。",
        }
        for no, text in self.contents.items():
            self.store.save_chapter_version(self.book_id, no, content=text)

    def put_state(self, no: int, state: dict) -> None:
        index = self.store._load_chapter_index(self.store._book_dir(self.book_id), no)
        self.store.save_chapter_state(
            self.book_id, no, state=state,
            source_version_id=str(index.get("current_version_id") or ""))

    @staticmethod
    def state(*, characters=(), relations=(), knowledge=(), events=(),
              open_threads=(), next_hooks=()):
        return {"characters": list(characters), "relations": list(relations),
                "knowledge": list(knowledge), "events": list(events),
                "open_threads": list(open_threads), "next_hooks": list(next_hooks)}

    def check(self):
        return run_continuity_check(self.store, self.book_id)

    def types(self, report):
        return [finding["type"] for finding in report["findings"]]


class BasicReportTests(CoherenceTestCase):
    def test_no_states_report_notes_and_no_findings(self):
        report = self.check()
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["schema"], coherence.REPORT_SCHEMA)
        self.assertTrue(any("没有有效章末状态" in note for note in report["notes"]))
        self.assertEqual(report["chapter_versions"]["1"], self.store.get_chapter(self.book_id, 1)["current_version_id"])

    def test_report_binds_versions_and_staleness(self):
        self.put_state(1, self.state(characters=[{"name": "张三", "presence": "出场"}]))
        report = self.check()
        self.assertEqual(report["checked_chapters"], [1])
        self.assertFalse(report_is_stale(self.store, self.book_id, report))
        # 重写第 1 章 → 旧报告过期。
        self.store.save_chapter_version(self.book_id, 1, content="第一章重写稿。")
        self.assertTrue(report_is_stale(self.store, self.book_id, report))
        # 重新运行得到新报告，又不过期。
        fresh = self.check()
        self.assertFalse(report_is_stale(self.store, self.book_id, fresh))

    def test_check_never_touches_content_or_versions(self):
        for no in range(1, 5):
            self.put_state(no, self.state(characters=[{"name": "张三", "presence": "出场"}],
                                          events=[f"事件{no}"]))
        before = []
        for no in range(1, 5):
            chapter = self.store.get_chapter(self.book_id, no)
            before.append((no, chapter["current_version_id"], chapter["content"],
                           chapter["revision"], len(chapter["versions"])))
        self.check()
        self.check()
        for no, vid, content, revision, version_count in before:
            chapter = self.store.get_chapter(self.book_id, no)
            self.assertEqual((chapter["current_version_id"], chapter["content"],
                              chapter["revision"], len(chapter["versions"])),
                             (vid, content, revision, version_count))


class FindingRuleTests(CoherenceTestCase):
    def test_unintroduced_character_with_quote(self):
        self.put_state(1, self.state(characters=[
            {"name": "张三", "presence": "出场"}, {"name": "李四", "presence": "出场"}]))
        self.put_state(2, self.state(characters=[{"name": "张三", "presence": "出场"}]))
        self.put_state(3, self.state(characters=[
            {"name": "张三", "presence": "出场"},
            {"name": "神秘人", "presence": "出场"}]))
        report = self.check()
        findings = [f for f in report["findings"] if f["type"] == "character_unintroduced"]
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["chapters"], [3])
        self.assertEqual(finding["names"], ["神秘人"])
        self.assertEqual(finding["severity"], "tentative")
        evidence = finding["evidence"][0]
        self.assertEqual(evidence["chapter_no"], 3)
        self.assertIn("神秘人尾随其后", evidence["quote"])
        # 角色卡里的王五 / 别名李师傅不算无铺垫。
        self.put_state(3, self.state(characters=[
            {"name": "李师傅", "presence": "出场"}, {"name": "王五", "presence": "出场"}]))
        report = self.check()
        self.assertTrue(all(f["chapters"] != [3] or f["names"] != ["李师傅"]
                            for f in report["findings"]))

    def test_vanished_lead_flagged_only_when_gone_to_the_end(self):
        self.put_state(1, self.state(characters=[{"name": "张三", "presence": "出场"}]))
        self.put_state(2, self.state(characters=[{"name": "张三", "presence": "出场"}]))
        self.put_state(3, self.state(characters=[{"name": "张三", "presence": "缺席"}]))
        self.put_state(4, self.state(characters=[{"name": "张三", "presence": "缺席"}]))
        findings = [f for f in self.check()["findings"] if f["type"] == "character_vanished"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["chapters"], [2, 3])
        self.assertEqual(findings[0]["severity"], "tentative")
        # 中途消失、末章回归 → 不算消失。
        self.put_state(4, self.state(characters=[{"name": "张三", "presence": "出场"}]))
        findings = [f for f in self.check()["findings"] if f["type"] == "character_vanished"]
        self.assertEqual(findings, [])

    def test_knowledge_conflict_and_reveal_between(self):
        self.put_state(1, self.state(
            characters=[{"name": "张三", "presence": "出场"}],
            knowledge=[{"name": "李四", "known": [], "unknown": ["密令内容"]}]))
        self.put_state(2, self.state(characters=[{"name": "张三", "presence": "出场"}]))
        self.put_state(3, self.state(
            characters=[{"name": "张三", "presence": "出场"}],
            knowledge=[{"name": "李四", "known": ["密令内容"], "unknown": []}]))
        findings = [f for f in self.check()["findings"] if f["type"] == "knowledge_conflict"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["chapters"], [1, 3])
        self.assertTrue(all("密令内容" in e.get("quote", "") or "密令内容" in e["entry"]
                            for e in findings[0]["evidence"]))
        # 中间章的事件交代了「密令内容」→ 不再是矛盾。
        self.put_state(2, self.state(characters=[{"name": "张三", "presence": "出场"}],
                                     events=["密令内容被当众宣读"]))
        findings = [f for f in self.check()["findings"] if f["type"] == "knowledge_conflict"]
        self.assertEqual(findings, [])

    def test_relation_conflict_severity(self):
        self.put_state(2, self.state(relations=[
            {"between": ["张三", "李四"], "change": "荒庙相遇，结为同盟"}]))
        self.put_state(4, self.state(relations=[
            {"between": ["张三", "李四"], "change": "两人互不相识"}]))
        findings = [f for f in self.check()["findings"] if f["type"] == "relation_conflict"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "confirmed")
        self.assertEqual(findings[0]["chapters"], [2, 4])
        # 「初次见面」属于可能缺少交代，只报 tentative。
        self.put_state(4, self.state(relations=[
            {"between": ["张三", "李四"], "change": "两人初次见面"}]))
        findings = [f for f in self.check()["findings"] if f["type"] == "relation_conflict"]
        self.assertEqual(findings[0]["severity"], "tentative")
        # 先认识后结盟是正常推进，不报。
        self.store.save_chapter_version(self.book_id, 4, content="第四章重写。")
        self.put_state(4, self.state(relations=[
            {"between": ["张三", "李四"], "change": "歃血结盟，生死与共"}]))
        findings = [f for f in self.check()["findings"] if f["type"] == "relation_conflict"]
        self.assertEqual(findings, [])


class CoherenceServiceTests(CoherenceTestCase):
    def setUp(self):
        super().setUp()
        self.app = _App(self.root)
        self.service = WriterService(self.app)
        self.service.pipeline = WriterPipelineManagerStub(self.service)

    def test_run_and_get_report_endpoints(self):
        self.put_state(1, self.state(characters=[{"name": "张三", "presence": "出场"}]))
        payload, status = self.service.api_run_continuity_check(self.book_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["report"]["schema"], coherence.REPORT_SCHEMA)
        self.assertFalse(payload["report"]["stale"])
        payload, status = self.service.api_get_continuity_report(self.book_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["report"]["checked_chapters"], [1])
        # 报告落盘、指向的书不存在 → 404。
        self.assertEqual(self.service.api_get_continuity_report("wb-20990101-0123456789ab")[1], 404)
        # 正文变化 → stale；重新运行 → 新报告。
        self.store.save_chapter_version(self.book_id, 1, content="第一章重写。")
        payload, status = self.service.api_get_continuity_report(self.book_id)
        self.assertEqual(status, 200)
        self.assertTrue(payload["report"]["stale"])
        payload, _ = self.service.api_run_continuity_check(self.book_id)
        self.assertFalse(payload["report"]["stale"])

    def test_run_report_writes_only_report_file(self):
        for no in range(1, 5):
            self.put_state(no, self.state(characters=[{"name": "张三", "presence": "出场"}]))
        before = {no: self.store.get_chapter(self.book_id, no) for no in range(1, 5)}
        payload, status = self.service.api_run_continuity_check(self.book_id)
        self.assertEqual(status, 200)
        for no, chapter in before.items():
            after = self.store.get_chapter(self.book_id, no)
            self.assertEqual(after["content"], chapter["content"])
            self.assertEqual(after["current_version_id"], chapter["current_version_id"])
            self.assertEqual(after["revision"], chapter["revision"])
        report_path = self.store._book_dir(self.book_id) / "continuity-report.json"
        self.assertTrue(report_path.is_file())
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["schema"],
                         coherence.REPORT_SCHEMA)


class WriterPipelineManagerStub:
    """连贯性检查不依赖流水线；占位以满足 WriterService 构造。"""

    def __init__(self, service):
        self.service = service

    def __getattr__(self, name):
        raise AttributeError(f"连贯性检查测试不应触发流水线方法：{name}")


if __name__ == "__main__":
    unittest.main()
