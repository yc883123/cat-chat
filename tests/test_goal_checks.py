"""Data-only GOAL checks: frozen evidence, strict schemas and path boundaries."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from naiba.run import goal_checks as checks


class GoalChecksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="goal-check-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def capture(self, criteria):
        return checks.capture_inputs(self.root, criteria)

    def result(self, criteria):
        return checks.execute(self.capture(criteria))

    def test_file_hash_contains_and_snapshot_is_json_serializable(self):
        raw = b"\xef\xbb\xbfhello\r\nworld"
        self.write("answer.txt", raw)
        digest = hashlib.sha256(raw).hexdigest()
        snapshot = self.capture([{"path": "answer.txt", "sha256": digest.upper(), "contains": "hello"}])
        self.assertEqual(snapshot, json.loads(json.dumps(snapshot)))
        result = checks.execute(snapshot)
        self.assertTrue(result["passed"])
        self.assertEqual(result["evidence"][0]["hash"], digest)
        self.assertEqual(result["input_fingerprint"], snapshot["fingerprint"])
        self.assertEqual(checks.execute(snapshot), result)
        self.assertTrue(checks.verify_snapshot_current(snapshot))

    def test_execute_never_reads_files_or_runs_code(self):
        self.write("evil.py", "raise RuntimeError('do not execute')")
        snapshot = self.capture([{"path": "evil.py", "contains": "RuntimeError"}])
        with patch("builtins.open", side_effect=AssertionError("open")), patch.object(Path, "open", side_effect=AssertionError("path open")), patch("os.system", side_effect=AssertionError("shell")):
            self.assertTrue(checks.execute(snapshot)["passed"])

    def test_missing_is_evidence_and_creation_invalidates_snapshot(self):
        snapshot = self.capture([{"path": "missing.txt"}])
        self.assertFalse(checks.execute(snapshot)["passed"])
        self.assertEqual(checks.execute(snapshot)["evidence"][0]["reason"], "missing")
        self.assertTrue(checks.verify_snapshot_current(snapshot))
        self.write("missing.txt", "")
        self.assertFalse(checks.verify_snapshot_current(snapshot))

    def test_recheck_uses_bytes_not_only_mtime_or_length(self):
        path = self.write("a", "before")
        snapshot = self.capture([{"path": "a"}])
        old = path.stat()
        path.write_text("after!", encoding="utf-8")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertFalse(checks.verify_snapshot_current(snapshot))
        self.assertTrue(checks.execute(snapshot)["passed"])

    def test_wrong_workspace_cannot_reuse_snapshot(self):
        self.write("a", "x")
        snapshot = self.capture([{"path": "a"}])
        other = self.root / "other"
        other.mkdir()
        self.assertFalse(checks.verify_snapshot_current(snapshot, other))

    def test_delete_invalidates_snapshot(self):
        path = self.write("a", "x")
        snapshot = self.capture([{"path": "a"}])
        path.unlink()
        self.assertFalse(checks.verify_snapshot_current(snapshot))

    def test_normalization_is_strict_and_idempotent(self):
        normalized = checks.normalize_criteria([{"path": "sub\\a", "contains": "ok"}])
        self.assertEqual(normalized[0]["path"], "sub/a")
        self.assertEqual(checks.normalize_criteria(normalized), normalized)
        self.assertEqual(normalized[0]["check_id"], "file")
        for value in ([{"path": "a", "command": "do something"}], [{"kind": "shell", "path": "a"}],
                      [{"kind": "local_check", "check_id": "arbitrary", "params": {}}],
                      [{"path": "a", "check_id": "file_count"}], [{"path": "a", "sha256": "bad"}],
                      [{"path": "a", "source_line": True}], [{"path": "a", "id": "bad id"}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checks.normalize_criteria(value)
        duplicate = {"id": "same", "path": "a"}
        with self.assertRaises(ValueError):
            checks.normalize_criteria([duplicate, duplicate])

    def test_cross_platform_path_rejection(self):
        for path in ("../a", "sub/../../a", "/a", "\\\\server\\share\\a", "C:a", "C:/a", "a:stream",
                     ".env", ".ENV.local", "sub/.git/a", ".ssh/id_rsa", ".aws/config", ".venv/a",
                     "a//b", "a/./b", "a/", "*.txt", "a?b", "NUL", "sub/COM1.txt", "a.", "a "):
            with self.subTest(path=path), self.assertRaises(ValueError):
                checks.normalize_criteria([{"path": path}])

    def test_symlink_and_hardlink_rejected(self):
        target = self.write("target", "secret")
        link = self.root / "link"
        try:
            link.symlink_to(target)
        except OSError:
            pass
        else:
            with self.assertRaises(ValueError):
                self.capture([{"path": "link"}])
        try:
            os.link(target, self.root / "hard")
        except OSError:
            return
        with self.assertRaises(ValueError):
            self.capture([{"path": "hard"}])

    def test_windows_directory_junction_is_rejected(self):
        if os.name != "nt":
            self.skipTest("Windows junction")
        target = self.root / "real-dir"
        target.mkdir()
        (target / "a").write_bytes(b"secret")
        link = self.root / "junction"
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, check=False)
        if done.returncode:
            self.skipTest("Junction creation unavailable")
        self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        with self.assertRaises(ValueError):
            self.capture([{"path": "junction/a"}])
        with self.assertRaises(ValueError):
            checks.capture_inputs(link, [{"path": "a"}])

    def test_invalid_input_types_are_validation_errors(self):
        for item in ({"kind": [], "path": "a"}, {"kind": "file", "path": "a\nb"},
                     {"kind": "json_structure", "params": {"path": "a", "schema": {"properties": {1: {"type": "string"}}}}}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                checks.normalize_criteria([item])

    def test_directory_and_sensitive_workspace_rejected(self):
        (self.root / "dir").mkdir()
        with self.assertRaises(ValueError):
            self.capture([{"path": "dir"}])
        secret = self.root / ".env-dir"
        secret.mkdir()
        with self.assertRaises(ValueError):
            checks.capture_inputs(secret, [])

    def test_size_and_list_bounds(self):
        self.write("big", b"x" * (checks.MAX_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.capture([{"path": "big"}])
        with self.assertRaises(ValueError):
            checks.normalize_criteria([{"path": str(n)} for n in range(65)])
        self.write("one", b"123")
        with patch.object(checks, "MAX_TOTAL_BYTES", 2), self.assertRaises(ValueError):
            self.capture([{"path": "one"}])

    def test_json_structure_recursive_data_only(self):
        self.write("data.json", '{"rows":[{"name":"yes","n":1}],"ok":true}')
        criterion = {"kind": "json_structure", "params": {"path": "data.json", "schema": {
            "type": "object", "required": ["rows", "ok"], "properties": {
                "ok": {"type": "boolean"}, "rows": {"type": "array", "items": {
                    "type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "n": {"type": "integer"}}}}}}}}
        self.assertTrue(self.result([criterion])["passed"])
        self.write("data.json", '{"rows":[{"name":3}],"ok":true}')
        self.assertFalse(self.result([criterion])["passed"])
        self.write("data.json", '{"rows":[]}')
        self.assertFalse(self.result([criterion])["passed"])

    def test_boolean_is_not_integer_or_number(self):
        self.write("data.json", "true")
        for kind in ("integer", "number"):
            self.assertFalse(self.result([{"kind": "json_structure", "params": {"path": "data.json", "schema": {"type": kind}}}])["passed"])

    def test_json_invalid_duplicates_nonfinite_and_deep_input(self):
        criterion = {"kind": "json_structure", "params": {"path": "data.json", "schema": {"type": "object"}}}
        for value in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', "not JSON", "[" * 40 + "]" * 40):
            self.write("data.json", value)
            with self.subTest(value=value):
                self.assertEqual(self.result([criterion])["evidence"][0]["reason"], "invalid_input")

    def test_unknown_schema_code_and_unbounded_recursion_rejected(self):
        for schema in ({"type": "object", "$ref": "https://invalid"}, {"type": "object", "additionalProperties": False},
                       {"type": "string", "pattern": ".*"}, {"type": "object", "required": ["a", "a"]}, {},
                       {"type": "string", "required": ["a"]}, {"type": "object", "items": {"type": "string"}}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                checks.normalize_criteria([{"kind": "json_structure", "params": {"path": "a", "schema": schema}}])
        schema = {"type": "string"}
        for _ in range(10):
            schema = {"type": "array", "items": schema}
        with self.assertRaises(ValueError):
            checks.normalize_criteria([{"kind": "json_structure", "params": {"path": "a", "schema": schema}}])

    def test_fixed_manifest_count_does_not_scan(self):
        self.write("one", "1")
        self.write("unlisted", "2")
        criterion = {"kind": "file_count", "params": {"paths": ["one", "two"], "count": 1}}
        with patch.object(Path, "glob", side_effect=AssertionError("glob")), patch("os.scandir", side_effect=AssertionError("scan")):
            self.assertTrue(self.result([criterion])["passed"])
        self.write("two", "2")
        result = self.result([criterion])
        self.assertFalse(result["passed"])
        self.assertEqual(result["evidence"][0]["observed_count"], 2)
        for params in ({"paths": ["*"], "count": 0}, {"paths": ["one", "one"], "count": 1},
                       {"paths": ["one"], "count": True}, {"paths": ["one"], "count": 2},
                       {"paths": ["one"], "count": 1, "recursive": True}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                checks.normalize_criteria([{"kind": "file_count", "params": params}])

    def test_cancel_preserves_all_criteria_as_failed_evidence(self):
        self.write("a", "ok")
        snapshot = self.capture([{"id": "first", "path": "a"}, {"id": "second", "path": "missing"}])
        event = threading.Event()
        event.set()
        result = checks.execute(snapshot, event)
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["evidence"]), 2)
        self.assertTrue(all(row["reason"] == "cancelled" and not row["passed"] and row["hash"] for row in result["evidence"]))

    def test_snapshot_tampering_rejected(self):
        self.write("a", "ok")
        snapshot = self.capture([{"path": "a"}])
        changed = copy.deepcopy(snapshot)
        changed["inputs"][0]["sha256"] = "a" * 64
        with self.assertRaises(ValueError):
            checks.execute(changed)
        self.assertFalse(checks.verify_snapshot_current(changed))
        # Even a recomputed envelope cannot smuggle mismatched input bytes.
        changed["fingerprint"] = checks._fingerprint({k: v for k, v in changed.items() if k != "fingerprint"})
        with self.assertRaises(ValueError):
            checks.execute(changed)

    def test_empty_criteria_cannot_machine_complete(self):
        result = self.result([])
        self.assertFalse(result["passed"])
        self.assertEqual(result["evidence"], [])


if __name__ == "__main__":
    unittest.main()
