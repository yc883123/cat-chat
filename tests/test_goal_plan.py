"""Explicit GOAL plan sources, conservative Markdown extraction and rechecks."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from naiba.run import goal_plan as plan


class GoalPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="goal-plan-")
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.root = self.parent / "work"
        self.root.mkdir()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def preview(self, message="", sources=(), attachments=()):
        return plan.preview(message, sources, self.root, attachments)

    def test_explicit_files_only_and_no_command_execution(self):
        self.write("plan.md", "验收文件：result.txt\n")
        self.write("ignored.md", "验收文件：must-not-be-read\n")
        with patch.object(Path, "glob", side_effect=AssertionError("glob")), patch("os.system", side_effect=AssertionError("shell")):
            preview = self.preview("执行 ignored.md 中的计划", ["plan.md"])
        self.assertEqual(len(preview["sources"]), 1)
        self.assertEqual([row["path"] for row in preview["criteria"]], ["result.txt"])
        self.assertEqual(preview["completion_policy"], "mixed")
        self.assertFalse((self.root / "result.txt").exists())

    def test_raw_bytes_are_frozen_and_json_round_trip(self):
        raw = b"\xef\xbb\xbf# Stage\r\n" + "验收文件：result.txt\r\n".encode("utf-8")
        self.write("plan.md", raw)
        preview = self.preview(sources=["plan.md"])
        source = preview["sources"][0]
        self.assertEqual(source["text"].encode("utf-8"), raw)
        self.assertEqual(source["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(source["parser_version"], 1)
        self.assertEqual(json.loads(json.dumps(preview)), preview)
        self.assertEqual(self.preview(sources=["plan.md"]), preview)
        self.assertTrue(plan.verify_sources_current(preview, self.root))
        self.assertEqual(plan.current_sources(preview, self.root), preview["sources"])

    def test_source_changed_same_length_and_timestamp_is_rejected(self):
        path = self.write("plan.md", "验收文件：one.txt\n")
        preview = self.preview(sources=["plan.md"])
        old = path.stat()
        path.write_bytes("验收文件：two.txt\n".encode("utf-8"))
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertFalse(plan.verify_sources_current(preview, self.root))
        self.assertEqual(preview["criteria"][0]["path"], "one.txt")
        self.assertEqual(plan.current_sources(preview, self.root)[0]["criteria"][0]["path"], "two.txt")
        self.assertFalse(plan.verify_sources_current({"sources": preview["sources"]}, self.root))

    def test_stored_source_subset_is_supported(self):
        self.write("plan.md", "验收文件：x")
        preview = self.preview(sources=["plan.md"])
        self.assertTrue(plan.verify_sources_current({"sources": preview["sources"]}, self.root, []))
        self.assertTrue(plan.verify_sources_current(preview["sources"], self.root))

    def test_heading_stages_and_checklist_manual_obligations(self):
        text = "# 阶段一\n- [x] 视觉上自然\n- [ ] 请人工复核体验\n- [ ] 验收文件：`out/a.txt`\n## 阶段二\n验收文件: out/b.txt\n"
        preview = self.preview(text)
        self.assertEqual([s["title"] for s in preview["stages"]], ["阶段一", "阶段二"])
        self.assertEqual(len(preview["criteria"]), 2)
        self.assertEqual(len(preview["manual_items"]), 2)
        self.assertTrue(preview["manual_items"][0]["checked_in_plan"])
        self.assertEqual(preview["completion_policy"], "mixed")
        self.assertEqual(preview["criteria"][0]["stage_id"], preview["stages"][0]["id"])
        self.assertEqual(preview["criteria"][1]["stage_id"], preview["stages"][1]["id"])
        self.assertEqual(preview["criteria"][0]["source_line"], 4)

    def test_subjective_source_content_remains_mixed_with_machine_conditions(self):
        self.write("plan.md", "# 验收\n验收文件：result.txt\n阅读起来流畅\n")
        preview = self.preview(sources=["plan.md"])
        self.assertEqual(preview["completion_policy"], "mixed")
        self.assertIn("阅读起来流畅", [m["text"] for m in preview["manual_items"]])
        machine = self.preview("# 验收\n验收文件：x")
        self.assertEqual(machine["completion_policy"], "machine_all")
        self.assertEqual(self.preview("人工确认")["completion_policy"], "manual")
        self.assertEqual(self.preview()["completion_policy"], "manual")

    def test_structured_checks_validate_and_merge(self):
        body = [{"path": "a", "contains": "ok"}, {"kind": "local_check", "check_id": "file_count", "params": {"paths": ["a", "b"], "count": 2}}]
        self.write("plan.md", "# 结果\n```goal-criteria\n" + json.dumps({"version": 1, "criteria": body}) + "\n```\n")
        preview = self.preview(sources=["plan.md"])
        self.assertEqual(len(preview["criteria"]), 2)
        self.assertEqual(preview["criteria"][1]["check_id"], "file_count")
        self.assertEqual(preview["completion_policy"], "machine_all")
        self.assertEqual(preview["criteria"], preview["sources"][0]["criteria"])

    def test_unknown_acceptance_never_silently_dropped(self):
        unknowns = ('[{"kind":"shell","command":"run tests"}]',
                    '[{"path":"a","regex":".*"}]',
                    '{"criteria": [], "command": "run"}',
                    '{"version":2,"criteria":[]}', '{"criteria":[{"kind":"local_check","check_id":"python","params":{}}]}',
                    "not json")
        for value in unknowns:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.preview("```goal-criteria\n" + value + "\n```")
        for text in ("验收文件：", "验收文件：../secret", "验收文件：.env", "验收文件：a.txt 或 b.txt"):
            with self.subTest(text=text):
                # An unrecognized suffix remains a literal relative filename;
                # it must never become a command or an inferred pair of files.
                if text.endswith("或 b.txt"):
                    preview = self.preview(text)
                    self.assertEqual(preview["criteria"][0]["path"], "a.txt 或 b.txt")
                else:
                    with self.assertRaises(ValueError):
                        self.preview(text)
        manual = self.preview("验收条件：主观满意即可")
        self.assertEqual(manual["completion_policy"], "manual")
        self.assertEqual(len(manual["manual_items"]), 1)

    def test_indented_code_is_manual_not_acceptance(self):
        preview = self.preview("    验收文件：example.txt\n\t验收文件：another.txt\n验收文件：real.txt")
        self.assertEqual([c["path"] for c in preview["criteria"]], ["real.txt"])
        self.assertEqual(len(preview["manual_items"]), 2)
        self.assertEqual(preview["completion_policy"], "mixed")

    def test_declared_artifact_symlink_is_rejected_before_capture(self):
        target = self.write("real.txt", "secret")
        link = self.root / "artifact.txt"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        with self.assertRaises(ValueError):
            self.preview("验收文件：artifact.txt")

    def test_other_code_fences_are_manual_and_never_parsed(self):
        preview = self.preview("```sh\n验收文件：not-real\nrm -rf anything\n```\n验收文件：real")
        self.assertEqual([c["path"] for c in preview["criteria"]], ["real"])
        self.assertEqual(preview["completion_policy"], "mixed")
        self.assertIn("rm -rf", preview["manual_items"][0]["text"])
        with self.assertRaises(ValueError):
            self.preview("```goal-criteria\n[]")
        self.assertEqual(self.preview("~~~goal-criteria\n[]\n~~~")["criteria"], [])

    def test_outside_source_requires_exact_current_attachment(self):
        attached = self.parent / "attached.md"
        attached.write_text("验收文件：result.txt", encoding="utf-8")
        sibling = self.parent / "other.md"
        sibling.write_text("private", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.preview(sources=[str(attached)])
        attachments = [{"path": str(attached), "name": "attachment"}]
        preview = self.preview(sources=[str(attached)], attachments=attachments)
        self.assertTrue(plan.verify_sources_current(preview, self.root, attachments))
        self.assertFalse(plan.verify_sources_current(preview, self.root, []))
        with self.assertRaises(ValueError):
            self.preview(sources=[str(sibling)], attachments=attachments)
        with self.assertRaises(ValueError):
            self.preview(sources=[str(attached)], attachments=[{"path": str(self.parent)}])

    def test_sensitive_sources_and_attachment_links_rejected(self):
        sensitive = self.parent / ".env.local"
        sensitive.write_text("secret", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.preview(sources=[str(sensitive)], attachments=[{"path": str(sensitive)}])
        target = self.write("safe.md", "验收文件：x")
        link = self.root / "link.md"
        try:
            link.symlink_to(target)
        except OSError:
            return
        with self.assertRaises(ValueError):
            self.preview(sources=["link.md"])

    def test_recheck_deleted_source_or_changed_snapshot_fails_closed(self):
        path = self.write("plan.md", "验收文件：result.txt")
        preview = self.preview(sources=["plan.md"])
        changed = copy.deepcopy(preview)
        changed["sources"][0]["text"] += "tampered"
        self.assertFalse(plan.verify_sources_current(changed, self.root))
        path.unlink()
        self.assertFalse(plan.verify_sources_current(preview, self.root))

    def test_source_and_extraction_limits_no_truncation(self):
        with self.assertRaises(ValueError):
            self.preview(sources=["missing"] * 9)
        self.write("large.md", b"x" * (plan.MAX_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.preview(sources=["large.md"])
        self.write("binary.md", b"\xff\xfe")
        with self.assertRaises(ValueError):
            self.preview(sources=["binary.md"])
        self.write("plan.md", "验收文件：x")
        with self.assertRaises(ValueError):
            self.preview(sources=["plan.md", "plan.md"])
        with self.assertRaises(ValueError):
            self.preview("\n".join("验收文件：" + str(n) for n in range(65)))
        with self.assertRaises(ValueError):
            self.preview("\n".join("- [ ] 人工" for _ in range(plan.MAX_MANUAL_ITEMS + 1)))


if __name__ == "__main__":
    unittest.main()
