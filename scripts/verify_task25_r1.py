"""Fixed R1 regressions in two process timezones plus unchanged T1's 16 checks."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from scripts import verify_task25_t1 as t1


class FixedEncoder:
    """Deterministic 512D vectors; never loads a model or contacts a provider."""
    def encode(self, texts, **kwargs):
        import numpy as np
        rows = []
        for text in texts:
            vector = np.zeros(512, dtype=np.float32)
            if any(word in text for word in ("鲜花", "鲜切花", "花瓣")):
                vector[0] = 1
            else:
                vector[1] = 1
            rows.append(vector)
        return np.stack(rows)


def unit_tests(output):
    if hasattr(time, "tzset"):
        time.tzset()
    elif os.name == "nt":
        import ctypes
        ctypes.CDLL("ucrtbase")._tzset()
    local_epoch = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(0))
    assert local_epoch == {"UTC0": "1970-01-01T00:00:00", "PST8PDT": "1969-12-31T16:00:00"}[os.environ["TZ"]]
    from scripts.acceptance_worker import Result, flatten
    from backend.agent.tests import test_chat_memory_isolation as isolation

    def archive(test_id, inputs):
        path = output / "existing" / test_id.rsplit(".", 1)[-1]
        path.mkdir(parents=True, exist_ok=True)
        (path / "model-messages.json").write_text(isolation.raw(inputs), encoding="utf-8")

    isolation.HomeMemoryIsolationTests.archive_model_inputs = staticmethod(archive)
    os.environ["TASK25_R1_EVIDENCE"] = str(output / "cases")
    names = ["backend.agent.tests.test_task25_r1_memory", "backend.agent.tests.test_chat_memory_isolation",
             "backend.agent.tests.test_home_sse_tracing", "backend.agent.tests.test_sandbox_memory_storage"]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    cases = [test.id() for test in flatten(suite)]
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    failed = {test.id(): detail for test, detail in result.failures + result.errors}
    t1.write(output / "report.json", {"passed": result.wasSuccessful(), "executed": result.testsRun,
                                     "skipped": len(result.skipped), "failures": failed,
                                     "timezone_env": os.environ.get("TZ"), "local_epoch": local_epoch,
                                     "cases": [{"name": name, "passed": name in result.successes} for name in cases]})
    return int(not result.wasSuccessful() or bool(result.skipped))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("unit", "seed", "recall"))
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if args.phase:
        if args.phase == "unit":
            return unit_tests(output)
        t1.configure(output)  # Settings must see the isolated paths before their first import.
        from backend.rag import chinese_embedding
        from backend.memory import conversation_memory
        encoder = FixedEncoder()
        actual_collection = conversation_memory._get_collection

        def fixed_collection(client):
            collection = actual_collection(client)
            # Fixed fixtures contain many identical padding vectors. Search the
            # small index exhaustively instead of relying on approximate ties.
            collection.modify(configuration={"hnsw": {"ef_search": 1000}})
            return collection

        with patch.object(chinese_embedding, "_get_model", return_value=encoder), patch.object(
            t1, "preflight", return_value=(encoder, {"encoder": "FixedEncoder", "dimension": 512,
                                                    "model": "fixed-offline", "revision": "R1"})
        ), patch.object(conversation_memory, "_get_collection", side_effect=fixed_collection), patch.object(
            t1, "configure", return_value=None
        ):
            t1.serve(output, args.phase)
        return 0
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1", LLM_PROVIDER="mock",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False",
               DIAGNOSIS_MYSQL_ENABLED="0", DIAGNOSIS_SANDBOX_DB_PATH="", PLAN_METRICS_DB_PATH="",
               GLM_API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="", API_KEY="")
    stages = []
    for tz in ("UTC0", "PST8PDT"):
        directory = output / tz
        directory.mkdir()
        with (directory / "run.log").open("wb") as log:
            result = subprocess.run([sys.executable, "-u", "-B", "-X", "utf8", "-m", "scripts.verify_task25_r1",
                                     "--output-dir", str(directory), "--phase", "unit"],
                                    env={**env, "TZ": tz}, stdout=log, stderr=log, timeout=600)
        stages.append({"timezone": tz, "exit": result.returncode})
        if result.returncode:
            t1.write(output / "report.json", {"passed": False, "stages": stages})
            return 1
    replay = output / "t1-fixed"
    replay.mkdir()
    for phase in ("seed", "recall"):
        with (replay / f"{phase}.log").open("wb") as log:
            result = subprocess.run([sys.executable, "-u", "-B", "-X", "utf8", "-m", "scripts.verify_task25_r1",
                                     "--output-dir", str(replay), "--phase", phase],
                                    env=env, stdout=log, stderr=log, timeout=300)
        stages.append({"phase": phase, "exit": result.returncode})
        if result.returncode:
            t1.write(output / "report.json", {"passed": False, "stages": stages})
            return 1
    cases = t1.read(replay / "cases.json")
    passed = len(cases) == 16 and all(case["passed"] for case in cases.values())
    t1.write(output / "report.json", {"passed": passed, "stages": stages, "t1_cases": len(cases),
                                     "t1_failed": [name for name, case in cases.items() if not case["passed"]],
                                     "real_glm_requests": 0, "real_bge_calls": 0})
    print(json.dumps(t1.read(output / "report.json")))
    return int(not passed)


if __name__ == "__main__":
    sys.exit(main())

