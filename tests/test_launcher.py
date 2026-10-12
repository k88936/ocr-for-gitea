"""Behavioural tests for the ``gitea-ocr`` launcher script."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "gitea-ocr"
BASH = "/bin/bash" if pathlib.Path("/bin/bash").exists() else "/usr/bin/bash"


class LauncherTests(unittest.TestCase):
    def test_runs_from_another_directory(self):
        # The reported bug: `python -m` only finds the package when the CWD is
        # the project root, so running the launcher elsewhere failed with
        # "No module named gitea_ocr_review".
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [BASH, str(LAUNCHER), "--version"],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=300,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gitea-ocr", proc.stdout)

    def test_errors_clearly_without_venv_or_uv(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "PATH": tmp,  # no uv
                "UV_PROJECT_ENVIRONMENT": os.path.join(tmp, "missing"),
            }
            proc = subprocess.run(
                [BASH, str(LAUNCHER), "--version"],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("uv sync", proc.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
