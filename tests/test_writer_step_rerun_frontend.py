"""Run the writer single-step rerun frontend checks with deterministic stubs."""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend execution")
class WriterStepRerunFrontendTests(unittest.TestCase):
    def test_step_rerun_entry_preview_and_submit_interleavings(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests" / "writer_step_rerun_frontend.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("writer step rerun frontend:", result.stdout)
        self.assertIn("cases passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
