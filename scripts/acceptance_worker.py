"""Isolated unittest worker for run_acceptance; writes machine-readable outcomes."""

import argparse
import json
from pathlib import Path
import unittest


SUITES = {
    "mcp": ("tests.test_mcp_daily_report.DailyReportMCPTests",),
    "provider": ("backend.agent.tests.test_provider_contract",),
    "entry": ("backend.agent.tests.test_real_diagnosis_entry.RealDiagnosisEntryTests",),
    "metrics": ("backend.agent.tests.test_plan_metrics.PlanMetricsTests",
                "backend.agent.tests.test_plan_metrics_flow.PlanMetricsFlowTests"),
    "gate": ("tests.test_acceptance_entry.AcceptanceEntryTests",),
    "memory": ("backend.agent.tests.test_chat_memory_isolation",),
    "sandbox_memory": ("backend.agent.tests.test_sandbox_memory_storage",),
    "budget": ("backend.agent.tests.test_home_sse_budget.HomeSSEBudgetTests",),
    "plan_scope": ("backend.agent.tests.test_home_plan_scope.HomePlanScopeTests",),
    "business_scope": ("backend.agent.tests.test_home_business_scope.HomeBusinessScopeTests",),
    "diagnosis_pause": ("backend.agent.tests.test_diagnosis_pauses.DiagnosisPauseTests",),
    "diagnosis_history": ("backend.agent.tests.test_diagnosis_history.DiagnosisHistoryTests",),
    "natural_diagnosis": ("backend.agent.tests.test_natural_diagnosis.NaturalDiagnosisTests",),
    "natural_review_scope": ("backend.agent.tests.test_natural_diagnosis.NaturalReviewScopeTests",),
    "natural_multi_agent": ("backend.agent.tests.test_natural_diagnosis.NaturalMultiAgentTests",),
    "natural_request": ("backend.agent.tests.test_natural_diagnosis.NaturalRequestTests",),
    "team_contracts": ("backend.agent.tests.test_team_contracts.TeamContractTests",
                       "backend.agent.tests.test_team_tools.TeamToolTests"),
    "team_runtime": ("backend.agent.tests.test_team_runtime.TeamRuntimeTests",),
    "home_delegation": ("backend.agent.tests.test_home_delegation.HomeDelegationTests",),
}
EXPECTED = {"mcp": 5, "provider": 9, "entry": 5, "metrics": 21, "gate": 8, "memory": 22, "budget": 18,
            "plan_scope": 9, "business_scope": 8, "diagnosis_pause": 12, "diagnosis_history": 7,
            "sandbox_memory": 2, "natural_diagnosis": 6, "natural_review_scope": 6, "natural_multi_agent": 8,
            "natural_request": 5, "team_contracts": 12, "team_runtime": 10, "home_delegation": 9}


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.successes = set()

    def addSuccess(self, test):
        super().addSuccess(test)
        self.successes.add(test.id())


def summarize(cases, result, discovered, expected, issues=()):
    problems = list(issues)
    failures = {}
    skipped = {test.id(): reason for test, reason in result.skipped}
    for test, detail in result.failures + result.errors + result.expectedFailures:
        name = getattr(test, "test_case", test).id()
        failures.setdefault(name, []).append(detail)
    for test in result.unexpectedSuccesses:
        failures.setdefault(test.id(), []).append("unexpected success")
    for name in failures.keys() - {case.id() for case in cases}:
        problems.append(f"{name}: {'; '.join(failures[name])}")
    rows = []
    for case in cases:
        name = case.id()
        for holder, reason in list(skipped.items()):
            if holder.startswith(("setUpClass (", "setUpModule (")) and name.rsplit(".", 1)[0] in holder:
                skipped[name] = reason
        status = ("skipped" if name in skipped else "failed" if name in failures
                  else "passed" if name in result.successes else "not_run")
        rows.append({"name": name, "status": status,
                     "details": failures.get(name, []) + ([skipped[name]] if name in skipped else [])})
    counts = {key: sum(row["status"] == key for row in rows) for key in ("passed", "failed", "skipped")}
    counts["executed"] = max(0, result.testsRun - sum(test.id() in {c.id() for c in cases} for test, _ in result.skipped))
    if discovered != expected or len(cases) != expected:
        problems.append(f"case count mismatch: discovered={discovered}, selected={len(cases)}, expected={expected}")
    if counts["executed"] == 0:
        problems.append("zero execution")
    if skipped:
        problems.append("required case skipped")
    if counts["passed"] != expected:
        problems.append("not all required cases passed")
    return {"expected": expected, "discovered": discovered, **counts, "cases": rows,
            "issues": problems, "ok": not problems and not failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=SUITES)
    parser.add_argument("output", type=Path)
    parser.add_argument("--inject-fault", choices=("critical-skip", "zero-execution"))
    args = parser.parse_args()
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(SUITES[args.group])
    cases = list(flatten(suite))
    discovered = len(cases)
    if args.inject_fault == "zero-execution":
        cases = []
    elif args.inject_fault == "critical-skip" and cases:
        case = cases[0]
        def skip():
            raise unittest.SkipTest("acceptance fault injection: critical case skipped")
        setattr(case, case._testMethodName, skip)
    trace_ids = {}
    with (args.output.parent / "python-traces.jsonl").open("w", encoding="utf-8") as archive, \
         (args.output.parent / "model-inputs.jsonl").open("w", encoding="utf-8") as model_archive:
        def archive_traces(name, path):
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    trace = json.loads(line)
                    trace_ids.setdefault(name, []).append(trace["trace_id"])
                    archive.write(json.dumps({"case": name, **trace}, ensure_ascii=False) + "\n")
        def archive_model_inputs(name, inputs):
            model_archive.write(json.dumps({"case": name, "inputs": inputs}, ensure_ascii=False,
                                           default=lambda item: vars(item)) + "\n")
        for case in cases:
            case.archive_traces = archive_traces
            case.archive_model_inputs = archive_model_inputs
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(unittest.TestSuite(cases))
    report = summarize(cases, result, discovered, EXPECTED[args.group], loader.errors)
    for case in report["cases"]:
        case["trace_ids"] = trace_ids.get(case["name"], [])
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(not report["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
