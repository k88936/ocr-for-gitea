import json
import os
import tempfile
import unittest
from pathlib import Path

from gitea_ocr_review.errors import OcrError
from gitea_ocr_review.ocr_runner import build_command, parse_review_json, run_ocr

SAMPLE = {
    "status": "complete",
    "llm": {"provider": "company", "model": "deepseek-v4-pro"},
    "message": "Review complete",
    "summary": {"files_reviewed": 1, "comments": 1, "total_tokens": 10},
    "comments": [
        {
            "path": "calc.py",
            "content": "bug here",
            "suggestion_code": "    return a + b",
            "existing_code": "    return a - b",
            "start_line": 2,
            "end_line": 2,
            "category": "bug",
            "severity": "critical",
        }
    ],
    "session_id": "abc-123",
}


class ParseTests(unittest.TestCase):
    def test_parse_sample(self):
        result = parse_review_json(SAMPLE)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.provider, "company")
        self.assertEqual(result.model, "deepseek-v4-pro")
        self.assertEqual(result.session_id, "abc-123")
        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.path, "calc.py")
        self.assertEqual(finding.line, 2)
        self.assertEqual(finding.severity, "critical")

    def test_parse_tolerates_missing_fields(self):
        result = parse_review_json({"comments": [{"path": "x"}]})
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.findings[0].severity, "unknown")
        self.assertIsNone(result.findings[0].line)

    def test_build_command(self):
        cmd = build_command("ocr", Path("/repo"), "base", "head", Path("/out.json"), ["--effort", "high"])
        self.assertEqual(cmd[:2], ["ocr", "review"])
        self.assertIn("--from", cmd)
        self.assertIn("base", cmd)
        self.assertIn("--to", cmd)
        self.assertIn("head", cmd)
        self.assertIn("json", cmd)
        self.assertEqual(cmd[-2:], ["--effort", "high"])


class RunOcrTests(unittest.TestCase):
    def _write_script(self, directory: Path, body: str) -> Path:
        path = directory / "fake_ocr"
        path.write_text("#!/usr/bin/env python3\n" + body)
        path.chmod(0o755)
        return path

    def test_run_ocr_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script = self._write_script(
                tmp_path,
                "import sys\n"
                "out = sys.argv[sys.argv.index('-o') + 1]\n"
                f"open(out, 'w').write({json.dumps(SAMPLE)!r})\n"
                "print('progress', file=sys.stderr)\n",
            )
            result = run_ocr(tmp_path, "a" * 40, "b" * 40, ocr_bin=str(script))
            self.assertEqual(len(result.findings), 1)
            self.assertEqual(result.status, "complete")

    def test_run_ocr_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script = self._write_script(
                tmp_path,
                "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n",
            )
            with self.assertRaises(OcrError) as ctx:
                run_ocr(tmp_path, "a" * 40, "b" * 40, ocr_bin=str(script))
            self.assertIn("boom", str(ctx.exception))

    def test_run_ocr_missing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OcrError):
                run_ocr(Path(tmp), "a" * 40, "b" * 40, ocr_bin="definitely-not-a-real-binary")

    def test_run_ocr_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script = self._write_script(
                tmp_path,
                "import sys\n"
                "out = sys.argv[sys.argv.index('-o') + 1]\n"
                "open(out, 'w').write('not json')\n",
            )
            with self.assertRaises(OcrError):
                run_ocr(tmp_path, "a" * 40, "b" * 40, ocr_bin=str(script))


if __name__ == "__main__":
    unittest.main()
