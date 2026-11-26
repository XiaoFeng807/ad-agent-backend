"""Verify three-product operating tasks with isolated SQLite, Edge and fixed or real GLM."""

import argparse
import json
import os
import subprocess
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from scripts import accept_task45_t1 as previous, task45_t1_fixture as fixture
from scripts.accept_task42_t1 import configure, read, serve, write


QUESTION = "帮我巡检这三款商品，看看今天有什么值得处理。需要改英文卖点的话，先给我看依据和改动，我确认后再应用。"
CASES = ("base", "swap", "missing-day", "missing-fact", "review-missing-day", "review-early", "covered", "two-eligible")
THRESHOLD = 20
TZ = ZoneInfo("Asia/Shanghai")
APPROVED = datetime.fromisoformat(fixture.AS_OF + "T12:00:00").replace(tzinfo=TZ)
REVIEWED = APPROVED + timedelta(days=8)
FOLLOWUP = [7, 7, 0, 9, 8, 9, 9]
REVIEW_MISSING = (APPROVED + timedelta(days=3)).date().isoformat()
REVIEW_SQL = Path(__file__).with_name("task45_operations_review.sql")


class Replay(previous.Replay):
    def create(self, **payload):
        messages = payload["messages"]
        if self.case == "two-eligible" and messages[0]["content"].startswith("You are the operations agent"):
            scope = json.loads(messages[0]["content"].splitlines()[1])
            pending = scope.get("pending_product_reads", [])
            if pending:
                calls = [{"id": uuid4().hex, "type": "function", "function": {"name": item["tool"],
                          "arguments": json.dumps({"sku": item["sku"]})}} for item in pending]
                response = {"content": None, "tool_calls": calls}
                self.requests.append({"role": "main", "body": payload, "response": response})
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(**response))])
        if not messages[0]["content"].startswith("Check fact coverage"):
            return super().create(**payload)
        inputs = json.loads(messages[1]["content"])
        content = json.dumps({"omissions": {sku: [row["fact_id"] for row in evidence["facts"]]
                                           for sku, evidence in inputs.items()}})
        response = {"content": content, "tool_calls": []}
        self.requests.append({"role": "coverage", "body": payload, "response": response})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(**response))])


def freeze(output, database, password):
    manifest = {"question": QUESTION, "approval_clock": APPROVED.isoformat(), "review_clock": REVIEWED.isoformat(),
                "threshold_pct": THRESHOLD, "windows": list(fixture.WINDOWS),
                "sql_sha256": {sql.name: previous.digest(sql) for sql in (previous.SQL, REVIEW_SQL)}, "cases": {}}
    for name in CASES:
        folder = output / name
        folder.mkdir()
        source = fixture.source(name)
        for row in source:
            if name == "covered" and row["target"]["sku"] == fixture.TOTE:
                row["bullets"] = [fact["options"][0] for fact in row["facts"]]
            if name == "two-eligible" and row["target"]["sku"] == fixture.RISER:
                row["series"].update({day: 7 for day in row["series"] if day >= fixture.WINDOWS[2]})
            row["series"].update({(APPROVED + timedelta(days=n)).date().isoformat(): units
                                  for n, units in enumerate(FOLLOWUP, 1)})
        if name == "review-missing-day":
            del source[0]["series"][REVIEW_MISSING]
        path = folder / "sandbox.sqlite3"
        previous.seed(path, database, password, source)
        expected = {"comparison": previous.oracle(path, fixture.WINDOWS)}
        if name in {"base", "review-missing-day", "review-early"}:
            review_clock = APPROVED if name == "review-early" else REVIEWED
            with closing(previous.sql_connection(path)) as conn:
                expected["review"] = dict(conn.execute(REVIEW_SQL.read_text(encoding="utf-8"), {
                    "baseline_start": fixture.WINDOWS[0], "baseline_end": fixture.WINDOWS[1],
                    "approved_day": APPROVED.date().isoformat(), "review_day": review_clock.date().isoformat(),
                    "shop": fixture.SHOP, "market": fixture.MARKET, "sku": fixture.TOTE,
                }).fetchone())
        write(folder / "source.json", source)
        write(folder / "sql-expected.json", expected)
        write(folder / "sql-before.json", previous.snapshot(path))
        manifest["cases"][name] = {file: previous.digest(folder / file)
                                   for file in ("source.json", "sql-expected.json")}
    write(output / "frozen.json", manifest)
    return manifest


def checks(name, result, browser, expected, *, record):
    def packet(key):
        return browser.get(key, {}).get("body", {}).get("data", {})

    round_data = (browser.get("rounds") or [{}])[-1]
    evidence = (round_data.get("evidence") or {}).get("body", {}).get("data") or {}
    answer = browser.get("answer", "")
    result_checks = result["checks"]
    def check(stage, label, value):
        result_checks[label] = {"stage": stage, "passed": bool(value)}

    eligible = {row["sku"] for row in expected["comparison"]
                if row["change_pct"] is not None and row["change_pct"] <= -THRESHOLD}
    chosen = next((row["sku"] for row in expected["comparison"] if row["selected"]), None)
    check("取证", "页内显示两窗口径与三款结论", all(word in answer for word in (*fixture.WINDOWS, *fixture.PRODUCTS)))
    if name != "missing-day":
        assessment = evidence.get("operations_inspection", {})
        check("取证", "达到配置阈值才核对原 Listing 和事实", assessment.get("threshold_pct") == THRESHOLD
              and {sku for sku, item in assessment.get("products", {}).items() if item.get("eligible")} == eligible)
        if name == "covered":
            check("取证", "原五点已覆盖事实时无需处理", assessment.get("products", {}).get(chosen, {}).get("omissions") == []
                  and "无需处理" in answer)
        elif name != "missing-fact":
            check("取证", "入选依据包含有原文支持的遗漏", assessment.get("products", {}).get(chosen, {}).get("omissions"))
        check("取证", "实际证据读取仅覆盖达阈值的候选", {(row["action"], row["evidence"]["arguments"]["sku"])
              for row in result["business_reads"] if row["action"] in {previous.CURRENT, previous.FACTS}}
              == {(tool, sku) for sku in eligible for tool in (previous.CURRENT, previous.FACTS)})
    for task in result["worker_first_inputs"].values():
        context = task["context"]
        check("交接", "首份输入范围与本轮一致 " + context["task"]["task_id"],
              context.get("scope", {}).get("windows") == list(fixture.WINDOWS)
              and context.get("scope", {}).get("candidates") == evidence.get("candidates")
              and all(row["arguments"]["sku"] in fixture.PRODUCTS for row in context["task"]["read_scope"]))
    if result["worker_count"] and chosen and name not in {"missing-day", "missing-fact"}:
        if any(item["context"].get("direct_inputs") for item in result["worker_first_inputs"].values()):
            check("交接", "首份输入逐原始产物对账", result["verified_handoffs"])

    if "review" in expected:
        oracle = expected["review"]
        approval, review = packet("get"), packet("review")
        receipt = (approval.get("receipt") or {}).get("receipt_id")
        check("审批", "展开五条原文且显示修改字段", "拟更新文案" in browser.get("preview", "") and
              all(item["bullet"]["statement"] in browser.get("preview", "") for item in result["bullet_sources"]))
        check("审批", "重复确认复用回执", receipt and browser.get("duplicate", {}).get("status") == 200
              and approval == packet("duplicate"))
        check("复盘", "待审提案无执行回执复盘", browser.get("reviewBeforeApproval") == 404)
        check("复盘", "复盘关联原回执且没有商品写入", receipt and review.get("receipt_id") == receipt
              and browser.get("sqlAfterApproval") == browser.get("sqlBeforeReview") == browser.get("sqlAfterReview"))
        baseline = {key: oracle[key] for key in ("start_date", "end_date", "covered_days", "units")}
        baseline["days"] = json.loads(oracle["days"])
        current = dict(zip(("start_date", "end_date", "covered_days", "units", "days"),
                           (oracle["current_start"], oracle["current_end"], oracle["current_covered"],
                            oracle["current_units"], json.loads(oracle["current_days"]))))
        missing = json.loads(oracle["current_missing"])
        check("复盘", "独立 SQL 逐日、两窗、覆盖和变化对账", review.get("baseline") == baseline
              and review.get("current") == current and review.get("missing_dates") == missing
              and review.get("units_delta") == oracle["delta"] and review.get("units_change_pct") == oracle["change_pct"])
        text = browser.get("reviewText", "")
        check("复盘", "页面逐日值与缺报文本", browser.get("reviewPoints") == [
            {"date": row["date"], "units": "unknown" if row["units"] is None else str(row["units"])}
            for row in baseline["days"] + current["days"]] and all(day in text for day in missing)
            and all(token in text for token in (str(baseline["start_date"]), str(current["end_date"]),
                                                f"{current['covered_days']}/7"))
            and ("待补齐" in text if missing else f"{oracle['change_pct']:.2f}%" in text))
    if record:
        sse = round_data.get("sse", {})
        check("选路", "真实浏览器录到任务结束前的实时进度", browser.get("recording") and any(
              row["ms"] < sse.get("terminalMs", 0) for row in sse.get("progress", [])))
    first = next((stage for stage in ("选路", "取证", "交接", "起草", "审批", "复盘")
                  if any(not row["passed"] and row["stage"] == stage for row in result_checks.values())), None)
    result.update(passed=first is None, first_deviation=first)


def run(output, fixed_results):
    fixed = fixed_results is None
    configure(output / "startup.sqlite3", "mock" if fixed else "glm")
    os.environ["LISTING_SIGNIFICANT_DECLINE_PCT"] = str(THRESHOLD)
    from scripts.run_real_diagnosis import Redactor
    from scripts.run_task27_t5 import ENDPOINT, GLMAudit, MODEL, browser_env

    if not fixed:
        credentials = dotenv_values(".env")
        os.environ.setdefault("GLM_API_KEY", credentials.get("GLM_API_KEY") or "")
        os.environ.setdefault("GLM_MODEL", credentials.get("GLM_MODEL") or MODEL)
        if not os.environ["GLM_API_KEY"]:
            raise RuntimeError("GLM_API_KEY is required")
    from backend import observability
    from backend.agent import llm_provider, team_runtime
    from backend.captcha import captcha
    from backend.database import chat_groups, database, listing_operations
    from tests.home_diagnosis_server import demo_captcha

    password = token_urlsafe(24)
    redactor = Redactor([password, os.environ["JWT_SECRET"], os.environ.get("GLM_API_KEY")])
    manifest = freeze(output, database, password)
    if not fixed and manifest != read(fixed_results / "frozen.json"):
        raise RuntimeError("Frozen fixed replay inputs differ from current inputs")
    database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(output / "startup.sqlite3")
    active = None
    accept = chat_groups.accept

    def accept_at_frozen_date(*args, **kwargs):
        group_run, context = accept(*args, **kwargs)
        return replace(group_run, requested_at=APPROVED), context

    class LiveAudit(GLMAudit):
        run_cap = 12

        def case_cap(self, case):
            return 12

        def request(self, request):
            path = output / "base" / "sandbox.sqlite3"
            if (database.sandbox_db_path() != path or previous.snapshot(path) != read(path.parent / "sql-before.json")
                    or self.model_link.get().get("step") not in {
                        "listing_operations", "listing_fact_coverage", "listing_draft", "team_worker"}):
                raise RuntimeError("GLM request outside the frozen synthetic inspection")
            return super().request(request)

    model = Replay() if fixed else llm_provider.ProviderFactory.create()
    audit = None
    results = {}
    with ExitStack() as stack:
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=model))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        stack.enter_context(patch.object(chat_groups, "accept", accept_at_frozen_date))
        if hasattr(listing_operations, "_now"):
            stack.enter_context(patch.object(listing_operations, "_now", side_effect=lambda: APPROVED))
            stack.enter_context(patch.object(listing_operations, "_review_day", side_effect=lambda:
                (APPROVED if active == "review-early" else REVIEWED).date()))
        stack.enter_context(patch("backend.memory.conversation_memory.store_conversation"))
        for attr, filename in (("TRACE_LOG_FILE", "traces.jsonl"), ("ERROR_LOG_FILE", "errors.jsonl"),
                               ("FUNC_LOG_FILE", "functions.jsonl")):
            stack.enter_context(patch.object(observability, attr, output / filename))
        if not fixed:
            stack.callback(model._client.close)
            if model._model != MODEL or str(model._client.base_url).rstrip("/") != ENDPOINT:
                raise RuntimeError("Expected the official GLM endpoint and " + MODEL)
            audit = LiveAudit(output / "sdk.sqlite3", redactor, uuid4().hex, LiveAudit.run_cap)
            hooks = model._client._client.event_hooks
            hooks["request"].append(audit.request)
            hooks["response"].append(audit.response)
            for module in (llm_provider, team_runtime):
                stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        from fast_server import app

        with serve(app) as origin:
            for active in CASES if fixed else ("base",):
                folder = output / active
                path = folder / "sandbox.sqlite3"
                database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(path)
                case = {"id": active, "message": QUESTION, "group": "task", "variant": active,
                        "title": "三商品巡检 · " + active, "expect_no_proposal": active == "covered",
                        "review": active in {"base", "review-missing-day", "review-early"}, "record": not fixed}
                if active == "two-eligible":
                    case["product_read_skus"] = [fixture.TOTE, fixture.RISER]
                case["approve"] = case["review"]
                write(folder / "scenario.json", case)
                if fixed:
                    model.case = active
                    model.requests.clear()
                else:
                    audit.case, audit.enabled = active, True
                try:
                    process = subprocess.run(["node", "tests/task45_t1_browser.cjs", origin, str(folder), str(path)],
                                             env={**browser_env(), "TASK45_BROWSER_PASSWORD": password},
                                             capture_output=True, timeout=145, check=False)
                    write(folder / "browser-process.json", redactor.clean({"exit_code": process.returncode,
                        "stderr": process.stderr.decode(errors="replace")}))
                finally:
                    if audit:
                        audit.enabled = False
                browser_file = folder / "browser.json"
                browser = redactor.clean(read(browser_file)) if browser_file.exists() else {}
                write(browser_file, browser)
                terminal = (browser.get("rounds") or [{}])[-1].get("terminal") or {}
                trace = redactor.clean(observability.get_trace(terminal.get("trace_id")) or {})
                rows = redactor.clean(list(model.requests) if fixed else audit.rows())
                write(folder / "trace.json", trace)
                execution_id = browser.get("post", {}).get("body", {}).get("data", {}).get("receipt", {}).get("execution_trace_id")
                if execution_id:
                    write(folder / "approval-trace.json", redactor.clean(observability.get_trace(execution_id) or {}))
                write(folder / ("model-replay.json" if fixed else "sdk.json"), rows)
                before, after = read(folder / "sql-before.json"), previous.snapshot(path)
                write(folder / "sql-after.json", after)
                expected = read(folder / "sql-expected.json")
                result = previous.score(case, browser, trace, before, after, rows, {}, read(folder / "source.json"),
                                        expected, fixed=False)
                checks(active, result, browser, expected, record=not fixed)
                result.update(path="delegated" if result["worker_count"] else "direct" if result["business_reads"]
                              else "before_business_read", sdk_http_attempts=0 if fixed else len(rows))
                write(folder / "result.json", result)
                results[active] = {key: result[key] for key in ("passed", "first_deviation", "expected_candidate",
                                   "path", "worker_count", "sdk_http_attempts", "trace_id")}
                write(output / "report.json", results)
                print(json.dumps({active: results[active]}, ensure_ascii=False), flush=True)
    return all(result["passed"] for result in results.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixed-results", type=Path, help="Run real GLM after every fixed replay passes")
    args = parser.parse_args()
    if args.fixed_results and not all(read(args.fixed_results / "report.json").get(case, {}).get("passed") for case in CASES):
        parser.error("Fixed replay did not pass")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    return run(output, args.fixed_results)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
