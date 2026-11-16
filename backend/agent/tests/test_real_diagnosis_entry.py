"""Offline acceptance of the real-run entry itself; all SDK traffic stays in MockTransport."""

from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend import observability
from backend.agent.tests.test_ad_diagnosis_eval import StatefulDiagnosisModel
from backend.agent.tests.test_provider_contract import SDKCapture
from backend.config.config import settings
from backend.database import database
from backend.memory import memory_manager
from backend.rag import rag_knowledge
from scripts import run_real_diagnosis as entry


class RealDiagnosisEntryTests(unittest.TestCase):
    def isolation(self, stack):
        for module, name in ((database, "DB_PATH"), (memory_manager, "MEMORY_DIR"),
                             (settings, "RAG_DB_PATH"), (rag_knowledge, "BGEEmbedding"),
                             (observability, "TRACE_LOG_FILE"), (observability, "FUNC_LOG_FILE"),
                             (observability, "ERROR_LOG_FILE")):
            stack.enter_context(patch.object(module, name, getattr(module, name)))
        stack.enter_context(redirect_stdout(io.StringIO()))
        directory = Path(stack.enter_context(TemporaryDirectory()))
        return directory

    def run_entry(self, directory, *args):
        code = entry.main(["--embedding", "fixed", "--output-dir", str(directory), *args])
        path, = directory.glob("run-*/report.json")
        return code, json.loads(path.read_text(encoding="utf-8")), path

    def test_two_round_entry_with_sdk_transport_and_first_snapshot_readback(self):
        with ExitStack() as stack:
            directory = self.isolation(stack)
            wire = SDKCapture(StatefulDiagnosisModel())
            wire.string_plan_ids = True
            wire.install(stack, "openai")
            stack.enter_context(patch.object(settings, "JWT_SECRET", "offline-entry-signature-with-32-bytes"))
            code, report, path = self.run_entry(directory)
            self.assertEqual(code, 0, report["failures"])
            self.assertEqual(report["status"], "通过")
            self.assertEqual(len(report["rounds"]), 2)
            first, second = report["rounds"]
            self.assertNotEqual(first["trace_id"], second["trace_id"])
            self.assertEqual(set(second["request"]["diagnosis"]), {"task_id", "baseline", "current"})
            self.assertTrue(report["review"]["first_round_snapshot_preserved"])
            self.assertEqual(report["review"]["get_readback"]["baseline"], entry.WINDOWS[0]["baseline"])
            self.assertTrue(report["preflight"]["jwt"]["session_verified"])
            self.assertTrue(all(row["model"] == "openai-contract-model" for row in report["sdk_requests"]))
            self.assertTrue(all(all(result["checks"].values()) for result in report["rounds"]))
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("offline-openai", raw)
            self.assertNotIn(settings.JWT_SECRET, raw)
            self.assertNotIn("authorization", raw.lower())

    def test_missing_configuration_never_constructs_sdk_or_reports_pass(self):
        with ExitStack() as stack:
            directory = self.isolation(stack)
            for key, value in {"LLM_PROVIDER": "deepseek", "API_KEY": "", "DEEPSEEK_API_KEY": "",
                               "OPENAI_API_KEY": "", "JWT_SECRET": ""}.items():
                stack.enter_context(patch.object(settings, key, value))
            sdk = stack.enter_context(patch("openai.OpenAI", side_effect=AssertionError("must not call")))
            code, report, _ = self.run_entry(directory)
            self.assertEqual(code, 2)
            self.assertEqual(report["status"], "待运行")
            self.assertFalse(report["real_model_executed"])
            self.assertTrue(report["preflight"]["knowledge"]["ready"])
            self.assertEqual(report["sdk_requests"], [])
            self.assertIn("preflight_model_not_ready", report["failures"])
            self.assertIn("preflight_jwt_not_ready", report["failures"])
            sdk.assert_not_called()

    def test_preflight_with_ready_configuration_does_not_send_model_requests(self):
        with ExitStack() as stack:
            directory = self.isolation(stack)
            wire = SDKCapture()
            wire.install(stack, "deepseek")
            stack.enter_context(patch.object(settings, "JWT_SECRET", "offline-entry-signature-with-32-bytes"))
            code, report, _ = self.run_entry(directory, "--preflight")
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "预检通过；真实模型待运行")
            self.assertFalse(report["real_model_executed"])
            self.assertEqual(wire.requests, [])

    def test_sdk_failure_report_redacts_keys_tokens_and_headers(self):
        with ExitStack() as stack:
            directory = self.isolation(stack)
            wire = SDKCapture()
            wire.install(stack, "openai")
            stack.enter_context(patch.object(settings, "JWT_SECRET", "offline-entry-signature-with-32-bytes"))

            def fail(request):
                import httpx
                return httpx.Response(401, json={"error": {"message": "offline-openai Bearer sensitive-token"}})

            wire.handle = fail
            code, report, path = self.run_entry(directory)
            self.assertEqual(code, 1)
            self.assertEqual(report["status"], "失败")
            self.assertTrue(report["sdk_requests"])
            self.assertTrue(all(row["failure_reason"] == "provider_http_401" for row in report["sdk_requests"]))
            raw = path.read_text(encoding="utf-8")
            for secret in ("offline-openai", "sensitive-token", "Bearer", settings.JWT_SECRET, "authorization"):
                self.assertNotIn(secret, raw)

    def test_redactor_handles_empty_config_and_nested_receipts(self):
        redactor = entry.Redactor(["", 'secret-"quoted', "local-key"])
        result = redactor.clean({"receipts": ['secret-"quoted', "Bearer token-value", "local-key", None]})
        self.assertEqual(result, {"receipts": ["[REDACTED]", "[REDACTED]", "[REDACTED]", None]})


if __name__ == "__main__":
    unittest.main()
