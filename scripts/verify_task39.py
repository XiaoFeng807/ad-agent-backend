"""Run and audit plan-button CTR generation and the resulting work-group review."""

import argparse
from collections import Counter
from contextlib import closing
from datetime import date, timedelta
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
import sys
from time import perf_counter_ns
from traceback import format_exc
from types import SimpleNamespace

from scripts import verify_task38 as prior
from scripts.audit_task38 import check_case
from scripts.task27_t3_fixture import ARTIFACT, OWNER, ROOT, write
from scripts.verify_task28 import TARGET_SQL


SUITE = "TASK-039"
SEED_ALERT = False
TARGET = 103
CASE = "generated"
WINDOWS = ("baseline", "current")
Audit = prior.Audit
instrument = prior.instrument
export = prior.export
COMMON_SQL = """WITH consecutive AS (
    SELECT metric_date, julianday(metric_date) - ROW_NUMBER() OVER (ORDER BY metric_date) AS island
    FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date BETWEEN ? AND ?
)
SELECT min(7, COUNT(*) / 2) AS width, MAX(metric_date) AS last_day
FROM consecutive GROUP BY island HAVING COUNT(*) >= 2
ORDER BY width DESC, last_day DESC LIMIT 1"""
WINDOW_CTR_SQL = """SELECT COUNT(*) AS days, SUM(impressions) AS impressions, SUM(clicks) AS clicks,
    100.0 * SUM(clicks) / NULLIF(SUM(impressions), 0) AS ctr_pct
    FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date BETWEEN ? AND ?"""


def freeze(output, sql, frozen):
    assert not sql("SELECT * FROM alerts", rows=True)
    binding, = sql("SELECT * FROM plan_metric_bindings WHERE plan_id=?", (TARGET,), rows=True)
    plan, = sql("SELECT * FROM ad_plans WHERE id=? AND user_id=?", (TARGET, OWNER), rows=True)
    with closing(sqlite3.connect(ARTIFACT.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
        assert all(binding[k] == manifest[k] for k in ("source_id", "data_version"))
        args = [binding["source_plan_id"], manifest["complete_start_date"],
                min("2026-11-11", manifest["complete_end_date"])]
        common = dict(conn.execute(COMMON_SQL, args).fetchone())
        width, end = common["width"], date.fromisoformat(common["last_day"])
        assert width == 7
        dates = {label: {"start_date": (end - timedelta(days=start)).isoformat(),
                         "end_date": (end - timedelta(days=stop)).isoformat()}
                 for label, start, stop in (("baseline", 2 * width - 1, width), ("current", width - 1, 0))}
        windows = []
        for window in dates.values():
            params = [binding["source_plan_id"], window["start_date"], window["end_date"]]
            totals = dict(conn.execute(WINDOW_CTR_SQL, params).fetchone())
            assert totals["days"] == width
            windows.append({"plan_id": TARGET, **window, **totals, "sql_params": params})
        baseline, current = windows
        params = dict(bi=baseline["impressions"], bc=baseline["clicks"],
                      ci=current["impressions"], cc=current["clicks"])
        decision = dict(conn.execute(TARGET_SQL, params).fetchone())
        assert decision["qualifies"] == 1
    oracle = {"common_sql": COMMON_SQL, "common_params": args, "common": common, "window_sql": WINDOW_CTR_SQL,
              "target_sql": TARGET_SQL, "target_params": params, "dates": dates, "candidate_plan_ids": [TARGET],
              "metrics": [{"plan_id": TARGET, "windows": windows, **decision}], "targets": [TARGET],
              "binding": binding, "plan": plan}
    write(output / "sql-oracle.json", oracle)
    return {"cases": {CASE: {"fixture": CASE, "message": prior.PROMPT, "ctr_plan_id": TARGET, "oracle": oracle}},
            "holdout": {}, "clock": prior.CLOCK.isoformat(), "rule": frozen["rule"]}


def apply_fixture(sql, frozen, case):
    assert case == CASE and not sql("SELECT * FROM alerts", rows=True)


class FixedModel:
    def __init__(self, audit, replay):
        self.audit = audit
        self.chat = SimpleNamespace(completions=self)
        self.calls = []

    def create(self, messages, tools=None, **_):
        from backend.agent.tests.test_home_delegation import dispatch, reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call

        def task(task_id, capability, objective, **fields):
            return {"task_id": task_id, "objective": objective, "capabilities": [capability],
                    "expected_output": "本轮证据回执", "stop_condition": "交回回执", **fields}

        link = self.audit.model_link.get()
        task_id = link["task_id"]
        returned = [json.loads(row["content"]) for row in messages if row["role"] == "tool"]
        if task_id == "review":
            context = json.loads(next(row["content"] for row in messages if row["role"] == "user"))
            inputs = [row for rows in context["inputs"].values() for row in rows]
            assert Counter(row["source"] for row in inputs) == {prior.METRICS: 2, prior.RULE: 1}
            plan_id, = {row["arguments"]["plan_id"] for row in inputs if row["source"] == prior.METRICS}
            response = reply("", tool_call("detail", name=prior.DETAIL, plan_id=plan_id))
        elif task_id == "rule":
            response = worker_summary("已交回") if returned else reply(
                "", tool_call("v1", name=prior.RULE, query="ctr-review-v1#low-ctr"))
        elif task_id:
            plan_id = int(task_id.removeprefix("plan-"))
            scope = json.loads(messages[0]["content"].splitlines()[-1])
            response = worker_summary("已交回") if returned else reply("", *(
                tool_call(label, name=prior.METRICS, plan_id=plan_id, **scope[label]) for label in WINDOWS))
        elif not returned:
            assert [tool["function"]["name"] for tool in tools] == [prior.ALERT]
            response = reply("", tool_call("discover", name=prior.ALERT))
        elif len(returned) == 1:
            receipt, = returned
            plan_id, = receipt["candidate_plan_ids"]
            assert receipt["associations"][0]["ctr_check"]
            response = reply("", dispatch(
                "verified-batch", task(f"plan-{plan_id}", prior.METRICS, f"取证 #{plan_id} 两窗"),
                task("rule", prior.RULE, "核对 " + receipt["review_scope"]["rule"]),
                task("review", prior.DETAIL, "独立复核", dependencies=[f"plan-{plan_id}", "rule"])))
        else:
            response = reply("本轮复核结束。")
        message = response.choices[0].message
        self.calls.append({**link, "case": CASE, "start_ns": perf_counter_ns(),
                           "body": {"messages": messages.copy(), "tools": tools or []},
                           "assistant_content": message.content,
                           "tool_calls": [{"id": call.id, "function": vars(call.function)}
                                          for call in message.tool_calls or []]})
        return response


def audit_run(output, selected=None):
    read = prior.shared.t5.read
    report, frozen = (read(output / name) for name in ("report.json", "frozen.json"))
    folder = output / CASE
    summary = {"mode": report["mode"], "passed": 0, "failed": 1}
    try:
        browser, calls, reads, receipts, sheets, selects = (
            read(folder / (name + ".json")) for name in
            ("browser", "sdk", "reads", "receipts", "accepted-task-sheets", "criteo-selects"))
        assert report["cases"][CASE]["browser"] == "pass"
        trace_id = browser["sse"][-1]["trace_id"]
        trace, = [row for row in map(json.loads, (output / "trace_log_file.jsonl").read_text(encoding="utf-8").splitlines())
                  if row["trace_id"] == trace_id]
        case = frozen["cases"][CASE]
        oracle = case["oracle"]
        checked = browser["ctr_check"]
        assert checked["request"] == {"method": "POST", "path": f"/api/plans/{TARGET}/ctr-alert-check", "body": None}
        data = checked["response"]["data"]
        row, = checked["rows"]
        assert checked["refreshed_rows"] == checked["rows"]
        assert (data["status"], data["alert_id"], data["plan_id"]) == ("created", row["id"], TARGET)
        assert (row["user_id"], row["plan_id"], row["type"], row["level"]) == (OWNER, TARGET, "ctr_review", "critical")
        assert data["binding"] == {k: v for k, v in oracle["binding"].items() if k != "plan_id"}
        assert data["detected_at"] == "2026-11-12T02:30:00+00:00"
        stored = json.loads(row["ctr_check_evidence"])
        assert stored == {k: v for k, v in data.items() if k not in ("status", "alert_id")}
        facts, = oracle["metrics"]
        window_sql_proof = []
        with closing(sqlite3.connect(ARTIFACT.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            for label, expected in zip(WINDOWS, facts["windows"], strict=True):
                result = dict(conn.execute(WINDOW_CTR_SQL, expected["sql_params"]).fetchone())
                assert all(result[k] == expected[k] for k in ("days", "impressions", "clicks"))
                assert all(data[label][k] == expected[k] for k in ("start_date", "end_date", "impressions", "clicks"))
                assert data[label]["coverage"]["observed_days"] == result["days"]
                assert abs(data[label]["ctr_pct"] - result["ctr_pct"]) < 1e-8
                window_sql_proof.append({"label": label, "params": expected["sql_params"], "result": result})
        write(output / "sql-window-ctr.json", {"sql": WINDOW_CTR_SQL, "windows": window_sql_proof})
        assert abs(data["comparison"]["relative_decline_pct"] - facts["relative_decline_pct"]) < 1e-8
        oracle["alerts"] = [{"alert_id": row["id"], "plan_id": TARGET, "created_at": row["created_at"], "gap": None,
                             "ctr_check": {k: stored[k] for k in (
                                 "detected_at", *WINDOWS, "comparison", "rule", "policy")}}]
        path = check_case("normal", case, calls, reads, receipts, sheets, selects, browser, trace["steps"])
        detail, = [r for r in reads if r["tool"] == "query_plan_detail"]
        assert detail["arguments"] == {"plan_id": TARGET, "user_id": OWNER}
        assert all(r["arguments"]["user_id"] == OWNER for r in reads if r["tool"] == "query_plan_metrics")
        assert all(detail["result"][key] == oracle["plan"][key] for key in ("id", "plan_name", "status", "daily_budget"))
        assert browser["refreshed"]["basis"]["rule"]["chunk_id"] == frozen["rule"]["chunk_id"]
        assert report["storage_before"] == report["storage_after"]
        with closing(sqlite3.connect(Path(report["ledger"]).as_uri() + "?mode=ro", uri=True)) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            sdk = [{"attempt": attempt, "run": run, "case": case, **json.loads(request),
                    "response": json.loads(response)}
                   for attempt, run, case, request, response in conn.execute(
                       "SELECT id,run,sample,request,response FROM sdk WHERE run=? ORDER BY id",
                       (report["ledger_run"],))]
        count = len(sdk)
        assert count == report["sdk_attempts"]
        usage = Counter()
        if report["mode"] == "live":
            assert sdk == calls and report["http_statuses"] == {"200": count}
            assert all(row["body"]["model"] == prior.shared.t5.MODEL for row in sdk)
            assert all(row["endpoint"] == prior.shared.t5.ENDPOINT + "/chat/completions" for row in sdk)
            for sdk_call in sdk:
                usage.update({key: value for key, value in (sdk_call["response"]["body"].get("usage") or {}).items()
                              if type(value) is int})
        else:
            assert count == 0
        write(folder / "path.json", path)
        summary.update(passed=1, failed=0, trace_id=trace_id, alert_id=row["id"], path=path["path"],
                       sdk_attempts=count, model_calls=len(calls), detail=detail["arguments"],
                       sdk_retries=sum(row.get("sdk_retry", 0) > 0 for row in sdk), sdk_usage=dict(usage),
                       chromium=browser["chromium"], run_id=browser["sse"][-1]["run_id"],
                       pause_proposals=len(browser["scoped"]["proposals"]),
                       pause_requests=len(browser["pause_requests"]), pause_receipts=len(browser["scoped"]["receipts"]))
    except Exception:
        summary["error"] = format_exc()
        deviation = folder / "first-deviation.json"
        if not deviation.exists():
            write(deviation, {"error": summary["error"]})
    write(output / "audit.json", summary)
    return summary["passed"] == 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ledger", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ("preflight", "fixed", "live", "audit"):
        mode.add_argument("--" + name, action="store_const", dest="mode", const=name)
    args = parser.parse_args()
    if args.mode == "audit":
        return int(not audit_run(args.output_dir.resolve()))
    os.environ.update(PYTHON_DOTENV_DISABLED="1", AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH="", JWT_SECRET=token_urlsafe(48), ANONYMIZED_TELEMETRY="False",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if args.mode == "live":
        from dotenv import dotenv_values
        key = dotenv_values(ROOT / ".env").get("GLM_API_KEY")
        os.environ.update(LLM_PROVIDER="glm", GLM_MODEL=prior.shared.t5.MODEL)
        if key:
            os.environ.setdefault("GLM_API_KEY", key)
    else:
        os.environ.update(LLM_PROVIDER="mock", API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="", GLM_API_KEY="")
    return prior.shared.run(args.output_dir.resolve(), args.mode, ledger=args.ledger.resolve() if args.ledger else None,
                            suite=sys.modules[__name__], selected=(CASE,))


if __name__ == "__main__":
    raise SystemExit(main())
