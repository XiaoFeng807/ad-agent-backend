"""Approve the replacement review's own pause proposal in the TASK-032 browser session."""

import json
from traceback import format_exc

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from scripts.verify_task28 import audit_round, main, write
from scripts.verify_task32 import CASE, audit_progress


def prepare(app, output, sync, audit, provider, sql, oracle, live):
    from backend.database import database, diagnosis_pauses
    from backend.rag.review_rule_demo import DEMO_RULES, SOURCE

    sandbox = database.sandbox_db_path()
    assert sandbox and not database.settings.DIAGNOSIS_MYSQL_ENABLED
    diagnosis_pauses.register_sandbox_target(17, 103)
    registered = sql("SELECT * FROM diagnosis_sandbox_targets ORDER BY plan_id", rows=True)
    assert [(row["plan_id"], row["user_id"]) for row in registered] == [(103, 17)]
    assert sql("SELECT status FROM ad_plans WHERE id=103", rows=True) == [{"status": "active"}]
    sql("CREATE TABLE task34_status_updates (plan_id INTEGER, old_status TEXT, new_status TEXT)")
    sql("""CREATE TRIGGER task34_count_update AFTER UPDATE OF status ON ad_plans
           WHEN old.status != new.status BEGIN
             INSERT INTO task34_status_updates VALUES (old.id, old.status, new.status);
           END""")
    chunk = next(rule for rule in DEMO_RULES if rule[0] == "ctr-review-v1-low-ctr-01")
    rule = {**dict(zip(("chunk_id", "rule_id", "version", "title", "excerpt"), chunk, strict=True)),
            "source": SOURCE}
    write(output / "sandbox-preflight.json", {"registrations": registered, "plan_status": "active", "rule": rule})
    folder = output / "late"
    control = FastAPI()

    @control.post("/preapproval")
    def preapproval(sample: dict):
        try:
            sdk = audit.rows()
            calls = sdk if live else provider.calls
            findings = audit_round(sample, "late", sync, audit, calls, sql, oracle)
            write(folder / "preapproval-evidence.json", audit.redactor.clean(findings))
            audit_progress(sample, sync, calls, audit.receipts, findings, sql, folder)
            basis = sample["refreshed"]["basis"]
            assert all(basis["rule"][key] == value for key, value in rule.items())
            assert findings["decision"]["policy"]["min_impressions"] == 1000
            assert findings["decision"]["policy"]["min_relative_decline_pct"] == 20
            answer = sample["refreshed"]["answer"]
            for plan_id in CASE.plans("new"):
                row = oracle["new"]["metrics"][str(plan_id)]
                for window in row["windows"]:
                    assert f"{window['impressions']:,}" in answer and f"{window['clicks']:,}" in answer
                    ctr = 100 * window["clicks"] / window["impressions"]
                    assert f"{ctr:.2f}%" in answer
                assert f"{row['relative_decline_pct']:.2f}%" in answer
            assert "独立复核已完成" in answer and rule["chunk_id"] in answer
            return {"sdk_attempts": len(sdk), "targets": findings["decision"]["target_plan_ids"]}
        except Exception:
            write(folder / "preapproval-error.json", audit.redactor.clean({"error": format_exc()}))
            return JSONResponse({"detail": "see preapproval-error.json"}, status_code=500)

    app.mount("/__task35", control)
    return {"pause": {"sqlite": str(sandbox), "ledger": str(audit.ledger) if live else None,
                      "directory": str(folder)}}


def audit_completion(sample, sync, calls, receipts, evidence, sql, folder):
    audit_progress(sample, sync, calls, receipts, evidence, sql, folder)
    pause = sample["pause"]
    receipt = pause["http"]["post"]["body"]["data"]
    assert pause["sdk_attempts"] == dict.fromkeys(
        ("before_approval", "after_first_get", "after_refresh"),
        sum("attempt" in row for row in calls),
    )
    assert pause["preapproval_audit"]["targets"] == [103]
    trace_path = folder.parent / "trace_log_file.jsonl"
    traces = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    source, = [row for row in traces if row["trace_id"] == receipt["source_trace_id"]]
    execution, = [row for row in traces if row["trace_id"] == receipt["execution_trace_id"]]
    assert source["trace_id"] == sample["new_sse"][-1]["trace_id"]
    assert source == json.loads(json.dumps(evidence["traces"][source["trace_id"]]))
    detail = (f"action={receipt['action_id']} source={source['trace_id']} "
              f"receipt={receipt['receipt_id']} status=paused")
    assert any(step["action"] == "pause_committed" and step["detail"] == detail for step in execution["steps"])
    readbacks = [row for row in traces if any(step["action"] == "read_pause_status" and step["detail"] == detail
                                             for step in row["steps"])]
    assert len(readbacks) == 2
    assert len({row["trace_id"] for row in [source, execution, *readbacks]}) == 4
    write(folder / "pause-traces.json", {"source": source, "execution": execution, "readbacks": readbacks})
    write(folder / "review-worker-first-input.json", evidence["review_input"])


if __name__ == "__main__":
    main(CASE, __doc__, prepare_browser=prepare, audit_browser=audit_completion, fixed_modes=("late",))
