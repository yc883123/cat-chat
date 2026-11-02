from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class JavaScriptModuleAnalysisTests(unittest.TestCase):
    def analyze(self, files):
        with tempfile.TemporaryDirectory(prefix="naiba_esm_gate_") as directory:
            for name, content in files.items():
                (Path(directory) / name).write_text(content, encoding="utf-8")
            result = subprocess.run(
                ["node", "--expose-internals", "--experimental-vm-modules",
                 str(ROOT / "verify/js_module_analysis.cjs"), directory],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_alias_default_namespace_and_reexports_link(self):
        result = self.analyze({
            "main.js": 'import value, {renamed as local} from "./bridge.js"; import * as ns from "./source.js"; const copy = ns.value;',
            "bridge.js": 'export {value as renamed, value as default} from "./source.js";',
            "source.js": 'export const value = 2;',
        })
        self.assertEqual(result["esm"], [])
        self.assertEqual(result["tdz"], [])

    def test_private_declaration_is_not_an_export(self):
        result = self.analyze({"main.js": 'import {hidden} from "./source.js";', "source.js": 'const hidden = 2;'})
        self.assertTrue(result["esm"])
        self.assertTrue(any("does not export" in item["message"] or "ESM linker" in item["message"] for item in result["esm"]))

    def test_scopes_patterns_regex_and_template_expressions(self):
        result = self.analyze({
            "main.js": '''
const pattern = /[}/]text/;
const object = { text: 1, note: 2 };
function local(text, {note}, ...rest) {
  const refText = text;
  const message = `${text}:${note}:${refText}`;
  return object.text + object?.note + message;
}
try {} catch (statusText) { console.log(statusText); }
for (const markdown of []) console.log(markdown);
''',
            "source.js": 'const text=1, note=2, refText=3, statusText=4, markdown=5;',
        })
        self.assertEqual(result["esm"], [])
        self.assertEqual(result["tdz"], [])

    def test_unbound_shorthand_and_template_reference_are_not_masked(self):
        result = self.analyze({"main.js": 'const item={text}; const output=`${note}`;', "source.js": 'export const text=1; export const note=2;'})
        messages = [item["message"] for item in result["esm"]]
        self.assertTrue(any("unbound text" in item for item in messages))
        self.assertTrue(any("unbound note" in item for item in messages))

    def test_eager_cycle_is_detected(self):
        result = self.analyze({"main.js": 'import "./a.js";', "a.js": 'import {b} from "./b.js"; export const a=b;', "b.js": 'import {a} from "./a.js"; export const b=a;'})
        self.assertEqual(result["esm"], [])
        self.assertTrue(any("eager read a" in item["message"] for item in result["tdz"]))

    def test_main_order_safe_but_reverse_entry_sensitive_cycle_is_reported(self):
        result = self.analyze({
            "main.js": 'import "./consumer.js";',
            "consumer.js": 'import {rule} from "./producer.js"; export const preset="prefix"+rule;',
            "producer.js": 'import {preset} from "./consumer.js"; export const rule="rules";',
        })
        self.assertTrue(any("entry-sensitive cyclic initializer" in item["message"] for item in result["tdz"]))

    def test_empty_scan_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="naiba_empty_scan_") as directory:
            result = subprocess.run(["node", "--expose-internals", "--experimental-vm-modules", str(ROOT / "verify/js_module_analysis.cjs"), directory], capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 2)
            self.assertIn("No JavaScript modules found", result.stderr)

    def test_class_static_initializer_is_eager(self):
        result = self.analyze({"main.js": 'class Early { static { console.log(later); } } const later=1;'})
        self.assertTrue(result["tdz"])

    def test_deferred_closure_cycle_is_safe(self):
        result = self.analyze({"main.js": 'import "./a.js";', "a.js": 'import {b} from "./b.js"; export const a=()=>b;', "b.js": 'import {a} from "./a.js"; export const b=()=>a;'})
        self.assertEqual(result["tdz"], [])

    def test_iife_and_direct_call_follow_eager_reads(self):
        for invocation in ('read()', '(()=>value)()'):
            with self.subTest(invocation=invocation):
                result = self.analyze({
                    "main.js": 'import "./a.js";',
                    "a.js": 'import "./b.js"; export const value=1; export function read(){return value;}',
                    "b.js": f'import {{value,read}} from "./a.js"; const eager={invocation};',
                })
                self.assertTrue(result["tdz"])

    def test_hoisted_function_and_ordered_lexical_dependency(self):
        result = self.analyze({"main.js": 'import {copy} from "./a.js";', "a.js": 'import {value,read} from "./b.js"; export const copy=read()+value;', "b.js": 'export const value=1; export function read(){return value;}'})
        self.assertEqual(result["esm"], [])
        self.assertEqual(result["tdz"], [])

    def test_same_module_tdz_and_computed_property_read(self):
        for content in ('const a=b; const b=1;', 'const a={[b]:1}; const b=1;'):
            with self.subTest(content=content):
                self.assertTrue(self.analyze({"main.js": content})["tdz"])

    def test_cli_exits_nonzero_for_real_risk(self):
        with tempfile.TemporaryDirectory(prefix="naiba_tdz_cli_") as directory:
            (Path(directory) / "main.js").write_text('const a=b; const b=1;', encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "verify/tdz_check.py"), directory], capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
