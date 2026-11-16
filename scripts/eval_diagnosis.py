"""Run the diagnosis acceptance regressions and retrieval benchmark offline."""

import argparse
from collections import Counter, defaultdict
from functools import wraps
from importlib import import_module
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TESTS = "backend.agent.tests."
RULES, TASK, GUARDS, REVIEWS = GROUPS = ("规则与引用", "同任务续问", "补查、预算与身份", "确认与复核单")
PYTHON_SUITES = (
    ("test_ad_diagnosis_eval.AdDiagnosisEval", 3, RULES),
    ("test_diagnosis_rag.DiagnosisRAGTests", 8, RULES),
    ("test_diagnosis_rag.ReviewRetrievalTests", 1, RULES),
    ("test_ad_diagnosis_eval.DiagnosisTaskTests", 6, TASK),
    ("test_home_sse_availability.HomeSSEAvailabilityTests", 6, TASK),
    ("test_home_sse_tracing.HomeSSETracingTests", 17, TASK),
    ("test_diagnosis_guardrails.DiagnosisGuardrailsTests", 19, GUARDS),
    ("test_diagnosis_reviews.DiagnosisReviewTests", 10, REVIEWS),
)
BROWSER_SUITES = (("home_sse_browser.cjs", 4), ("home_diagnosis_browser.cjs", 12))
CITATION_CASE = "test_cites_exact_retrieval_and_reads_original"
REQUIRED_PYTHON_CASES = {
    CITATION_CASE: RULES,
    "test_adjacent_versions_are_not_evidence": RULES,
    "test_create_and_resume_with_new_pool": TASK,
    "test_temporary_rule_search_failure_retries_and_cites_fresh_chunk": GUARDS,
    "test_retries_only_temporary_failure_and_consumes_fresh_receipt": GUARDS,
    "test_exhausted_budget_prevents_retry": GUARDS,
    "test_unauthorized_request_does_not_start_trace": GUARDS,
    "test_development_signature_is_rejected_with_matching_session": GUARDS,
    "test_unconfigured_and_development_keys_disable_login_and_verification": GUARDS,
    "test_missing_or_uncommitted_session_is_rejected": GUARDS,
    "test_expired_or_invalid_claims_are_rejected_with_matching_session": GUARDS,
    "test_same_second_relogin_replaces_token": GUARDS,
    "test_concurrent_confirmation_keeps_first_round_and_is_owner_only": REVIEWS,
}
REQUIRED_BROWSER_CASES = {
    "real FastAPI two-round demo with original and clipboard trace lookup": RULES,
    "confirm round A after round B and read its saved receipt after restart": REVIEWS,
}


class Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.passed = set()

    def addSuccess(self, test):
        self.passed.add(test.id())


def inject_wrong_citation(case):
    from backend.agent.tests.test_diagnosis_rag import CHUNK_ID
    from backend.rag.rag_knowledge import RAGKnowledge

    test = getattr(case, CITATION_CASE)
    get_chunk = RAGKnowledge.get_chunk

    def wrong_chunk(rag, chunk_id):
        chunk = get_chunk(rag, chunk_id)
        if chunk_id == CHUNK_ID and chunk:
            return {**chunk, "content": "[评测注入] 错误引用原文"}
        return chunk

    @wraps(test)
    async def run():
        with patch.object(RAGKnowledge, "get_chunk", wrong_chunk):
            await test()

    setattr(case, CITATION_CASE, run)


def run_python(groups, output, inject):
    cases = []
    case_groups = {}
    trace_ids = defaultdict(list)

    def register(case, group):
        name = case.id().rsplit(".", 1)[-1]
        case_groups[case.id()] = REQUIRED_PYTHON_CASES.get(name, group)
        case.archive_traces = archive_traces
        if inject and name == CITATION_CASE:
            inject_wrong_citation(case)
        cases.append(case)

    with (output / "python-traces.jsonl").open("w", encoding="utf-8") as archive:
        def archive_traces(name, path):
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    trace = json.loads(line)
                    trace_ids[name].append(trace["trace_id"])
                    archive.write(json.dumps({"case": name, **trace}, ensure_ascii=False) + "\n")

        for suite_name, expected, group in PYTHON_SUITES:
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromName(TESTS + suite_name)
            if loader.errors:
                groups[group]["issues"].extend(loader.errors)
                continue
            if suite.countTestCases() != expected:
                groups[group]["issues"].append(f"{suite_name} 发现 {suite.countTestCases()} 项，预期 {expected} 项")
            for case in suite:
                register(case, group)
        try:
            tracker = import_module(TESTS + "test_task_tracker")
            tracker_cases = [unittest.FunctionTestCase(function) for name, function in vars(tracker).items()
                             if name.startswith("test_") and callable(function)]
            if len(tracker_cases) != 7:
                groups[TASK]["issues"].append(f"任务追踪发现 {len(tracker_cases)} 项，预期 7 项")
            for case in tracker_cases:
                register(case, TASK)
        except Exception as error:
            groups[TASK]["issues"].append(f"任务追踪加载失败 {error}")

        result = Result()
        unittest.TestSuite(cases).run(result)

    failures = defaultdict(list)
    skipped = set()
    for test, detail in result.failures + result.errors + result.expectedFailures:
        failures[getattr(test, "test_case", test).id()].append(detail.rstrip())
    for test in result.unexpectedSuccesses:
        failures[test.id()].append("意外成功，检查预期失败标记")
    for test, reason in result.skipped:
        name = getattr(test, "test_case", test).id()
        skipped.add(name)
        failures[name].append(f"跳过 {reason}")
    for name in failures.keys() - case_groups.keys():
        group = next((group for case, group in case_groups.items() if case.rsplit(".", 1)[0] in name), TASK)
        groups[group]["issues"].extend(f"{name}\n{detail}" for detail in failures[name])
    for name, group in case_groups.items():
        errors = failures.get(name, [])
        passed = name in result.passed and not errors
        groups[group]["cases"].append({
            "name": name, "executed": name not in skipped and bool(passed or errors),
            "failures": [] if passed else errors or ["未执行"], "trace_ids": trace_ids[name],
        })
    check_required(groups, (name.rsplit(".", 1)[-1] for name in case_groups), REQUIRED_PYTHON_CASES)


def check_required(groups, names, required):
    missing = required.keys() - set(names)
    for name in sorted(missing):
        groups[required[name]]["issues"].append(f"缺少关键用例 {name}")


def run_browser(groups, output, env):
    for script, expected in BROWSER_SUITES:
        args = ["node", str(ROOT / "tests" / script)]
        if script == "home_diagnosis_browser.cjs":
            args.append(str(output))
        stdout = output / f"{script}.stdout.log"
        stderr = output / f"{script}.stderr.log"
        issues = groups[TASK]["issues"]
        exit_code = None
        try:
            with stdout.open("wb") as out, stderr.open("wb") as err:
                exit_code = subprocess.run(args, cwd=ROOT, env=env, stdout=out, stderr=err, timeout=300).returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            issues.append(f"{script} 启动或运行失败 {error}")

        names = []
        browser_failures = 0
        for line in stdout.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("AD_EVAL_CASE "):
                continue
            case = json.loads(line.removeprefix("AD_EVAL_CASE "))
            name = case["name"]
            names.append(name)
            failure = case["failure"]
            browser_failures += failure is not None
            groups[REQUIRED_BROWSER_CASES.get(name, TASK)]["cases"].append({
                "name": name, "executed": True, "failures": [] if failure is None else [failure],
                "trace_ids": case["trace_ids"],
            })
        if script == "home_diagnosis_browser.cjs":
            check_required(groups, names, REQUIRED_BROWSER_CASES)
        if len(names) != expected or len(set(names)) != expected:
            issues.append(f"{script} 完成 {len(names)} 项（唯一 {len(set(names))} 项），预期 {expected} 项")
        if exit_code is not None and exit_code != int(bool(browser_failures)):
            issues.append(f"{script} 退出码 {exit_code}，详见 {stderr.name}")


def run_retrieval(use_bge):
    results = {}
    failures = []
    for embedding in ("fixed", "bge") if use_bge else ("fixed",):
        try:
            from scripts.eval_review_retrieval import evaluate

            results[embedding] = evaluate(embedding, repetitions=5)
            if results[embedding]["queries"] != 8:
                failures.append(f"{embedding} 检索题数不等于 8")
        except Exception as error:
            failures.append(f"{embedding} 检索失败 {error}")
    return results, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "logs" / "diagnosis-eval", help="parent directory for run artifacts")
    parser.add_argument("--bge", action="store_true", help="also benchmark real BGE from the local model cache")
    parser.add_argument("--inject-wrong-citation", action="store_true", help="corrupt one chunk readback in the ASGI suite")
    args = parser.parse_args()
    # The compatibility evaluator is offline too: never read the developer's .env.
    from scripts.run_acceptance import offline_environment
    clean_env = offline_environment()
    os.environ.clear()
    os.environ.update(clean_env)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = Path(mkdtemp(prefix="run-", dir=args.output_dir.resolve()))
    print(f"产物目录 {output}", flush=True)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False")
    env = dict(os.environ, PYTHON=sys.executable, AD_EVAL_RESULTS="1")

    groups = {group: {"cases": [], "issues": []} for group in GROUPS}
    run_python(groups, output, args.inject_wrong_citation)
    run_browser(groups, output, env)
    retrieval, retrieval_failures = run_retrieval(args.bge)
    scope = {
        "固定模型执行链": "离线回归使用固定模型及固定嵌入，覆盖 FastAPI/SSE、规则回查、SQLite 和 Chrome。",
        "真实 BGE": "在同一 8 题演示语料上测量检索。" if "bge" in retrieval else "本次未完成实测，可用 --bge 读取本地模型缓存。",
        "外部模型联调": "本命令未验证外部模型 API 或模型输出质量。",
        "计划级历史 CTR": "仅固定样本复算 CTR。浏览器演示验证缺少计划历史 CTR 时提示缺项。",
    }
    total = Counter()
    failed = bool(retrieval_failures)
    for name, group in groups.items():
        counts = {"passed": sum(not case["failures"] for case in group["cases"]),
                  "executed": sum(case["executed"] for case in group["cases"])}
        if not counts["executed"]:
            group["issues"].append("零用例执行")
        group.update(counts)
        total.update(counts)
        print(f"\n{name} · 通过 {counts['passed']} / 执行 {counts['executed']}")
        for case in group["cases"]:
            traces = ",".join(case["trace_ids"]) or "-"
            status = "FAIL" if case["failures"] else "PASS"
            print(f"  {status} {case['name']} trace_id={traces}")
            for error in case["failures"]:
                print(f"    {error}")
        for issue in group["issues"]:
            print(f"  FAIL {issue}")
        failed |= bool(group["issues"]) or counts["passed"] != len(group["cases"])
    failed |= total["executed"] != 93
    print(f"\n总计通过 {total['passed']} / 执行 {total['executed']} / 基线 93")
    for embedding, report in retrieval.items():
        for mode, metrics in report["results"].items():
            hits = " / ".join(f"{round(metrics[f'Hit@{k}'] * report['queries'])}/{report['queries']}" for k in (1, 3, 5))
            print(f"{embedding} {mode} Hit@1/3/5 {hits} · p50/p95 {metrics['p50_ms']}/{metrics['p95_ms']} ms")
    for issue in retrieval_failures:
        print(f"FAIL {issue}")
    for name, description in scope.items():
        print(f"{name} · {description}")
    report = {"expected": 93, "total": dict(total), "groups": groups, "retrieval": retrieval,
              "retrieval_failures": retrieval_failures, "scope": scope, "ok": not failed}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
