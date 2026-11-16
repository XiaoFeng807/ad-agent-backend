"""Acceptance gate failures: zero execution, skipped critical cases and absent evidence."""

import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.acceptance_worker import Result, summarize
from scripts.run_acceptance import main, offline_environment, read_group, validate_group, verify_artifact


def result():
    return Result(unittest.runner._WritelnDecorator(io.StringIO()), True, 0)


class AcceptanceEntryTests(unittest.TestCase):
    def test_zero_execution_is_failure(self):
        report = summarize([], result(), 0, 5)
        self.assertFalse(report["ok"])
        self.assertIn("zero execution", report["issues"])

    def test_critical_skip_is_failure(self):
        class Skipped(unittest.TestCase):
            @unittest.skip("critical fixture unavailable")
            def test_critical(self):
                pass
            def test_other(self):
                pass
        cases = [Skipped("test_critical"), Skipped("test_other")]
        outcome = result()
        unittest.TestSuite(cases).run(outcome)
        report = summarize(cases, outcome, 2, 2)
        self.assertFalse(report["ok"])
        self.assertEqual((report["executed"], report["passed"], report["skipped"]), (1, 1, 1))

    def test_setup_class_skip_is_failure(self):
        class SkippedClass(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest("critical class unavailable")
            def test_critical(self):
                pass
        cases = [SkippedClass("test_critical")]
        outcome = result()
        unittest.TestSuite(cases).run(outcome)
        report = summarize(cases, outcome, 1, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["skipped"], 1)

    def test_nonzero_child_exit_cannot_be_hidden_by_pass_counts(self):
        report = {"expected": 1, "executed": 1, "passed": 1, "failed": 0, "skipped": 0, "ok": True}
        self.assertFalse(validate_group(report, 1, 1)["ok"])

    def test_missing_suite_report_cannot_succeed(self):
        with TemporaryDirectory() as temp:
            report = read_group(Path(temp) / "missing.json")
        self.assertFalse(validate_group(report, 5, 0)["ok"])

    def test_full_missing_data_returns_nonzero_and_writes_report(self):
        with TemporaryDirectory() as temp:
            with patch("scripts.run_acceptance.run_logged", side_effect=AssertionError("must fail preflight")):
                code = main(["--mode", "full", "--plan-metrics-db", str(Path(temp) / "absent.sqlite3"),
                             "--output-dir", temp])
            report = json.loads(next(Path(temp).glob("run-*/report.json")).read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertIn("public_browser", report["not_run"])

    def test_modified_aggregate_is_rejected_before_sqlite_access(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "modified.sqlite3"
            path.write_bytes(b"not the pinned public aggregate")
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                verify_artifact(path)

    def test_offline_environment_removes_local_paths_and_credentials(self):
        with patch.dict(os.environ, {"PYTHONPATH": "temporary-python", "NODE_PATH": "global-node",
                                    "API_KEY": "must-not-copy", "GLM_API_KEY": "must-not-copy",
                                    "JWT_SECRET": "must-not-copy"}):
            env = offline_environment()
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("NODE_PATH", env)
        self.assertEqual((env["API_KEY"], env["GLM_API_KEY"], env["JWT_SECRET"]), ("", "", ""))
        self.assertEqual((env["AD_OFFLINE_ACCEPTANCE"], env["PYTHON_DOTENV_DISABLED"]), ("1", "1"))
