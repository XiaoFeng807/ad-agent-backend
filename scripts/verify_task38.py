"""Freeze, replay, and audit the weekly alert review through the work-group UI."""

import argparse
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
import sys
from time import perf_counter_ns
from types import SimpleNamespace
from unittest.mock import patch

from scripts import verify_task36 as shared, verify_task37
from scripts.task27_t3_fixture import ARTIFACT, OWNER, ROOT, write
from scripts.verify_task28 import DETAIL, METRICS, RULE, TARGET_SQL, WINDOW_SQL


SUITE = "TASK-038"
PROMPT = "复核这周的严重 CTR 告警"
ALERT = "get_weekly_critical_ctr_alerts"
CLOCK = datetime.fromisoformat("2026-11-12T10:30:00+08:00")
LOW, HIGH = "2026-11-08 16:00:00", "2026-11-12 02:30:00"
NORMAL = ("normal", "missing", "cross")
TRANSFER_DURING = "transfer-after-discovery"
FIXED = (*NORMAL, "transfer", "deleted", TRANSFER_DURING)
FOREIGN_NAME = "TASK38_OTHER_PLAN_PRIVATE"
GAP = {"missing": (4108, "alert_plan_missing", "缺少结构化计划关联"),
       "cross": (4109, "alert_plan_unverified", "计划关联无法核验"),
       "transfer": (4110, "alert_plan_unverified", "计划关联无法核验"),
       "deleted": (4111, "alert_plan_unverified", "计划关联无法核验")}
INSERT = """INSERT INTO alerts(id,type,message,level,is_read,user_id,created_at,plan_id)
            VALUES (?,?,?,?,?,?,?,?)"""
ALERT_SQL = """SELECT id AS alert_id, created_at, CASE
       WHEN plan_id IS NULL THEN 'alert_plan_missing'
       WHEN NOT EXISTS(SELECT 1 FROM ad_plans WHERE id=alerts.plan_id AND user_id=?) THEN 'alert_plan_unverified'
       END AS gap, CASE WHEN EXISTS(SELECT 1 FROM ad_plans WHERE id=alerts.plan_id AND user_id=?) THEN plan_id END AS plan_id
       FROM alerts WHERE user_id=? AND type='ctr_review' AND level='critical'
       AND created_at BETWEEN ? AND ? ORDER BY created_at, id"""
COMMON_SQL = """WITH daily AS (
       SELECT metric_date FROM plan_daily_metrics WHERE source_plan_id IN (?,?)
       AND metric_date BETWEEN ? AND ? GROUP BY metric_date HAVING COUNT(DISTINCT source_plan_id)=2
    ), islands AS (
       SELECT metric_date, julianday(metric_date) - ROW_NUMBER() OVER (ORDER BY metric_date) AS island FROM daily
    )
    SELECT min(7, COUNT(*) / 2) AS width, MAX(metric_date) AS last_day FROM islands
    GROUP BY island HAVING COUNT(*) >= 2 ORDER BY width DESC, last_day DESC LIMIT 1"""


class Audit(shared.LiveAudit):
    run_cap = 100


def alert_row(alert_id, plan, stamp, *, owner=OWNER, kind="ctr_review", level="critical", read=0, message="CTR 波动"):
    return (alert_id, kind, message, level, read, owner, stamp, plan)


def seed(sql, case):
    sql("DELETE FROM alerts WHERE id BETWEEN 4101 AND 4111")
    sql("UPDATE ad_plans SET user_id=? WHERE id=102", (OWNER,))
    sql("DELETE FROM ad_plans WHERE id=202")
    sql("UPDATE ad_plans SET plan_name=? WHERE id=201", (FOREIGN_NAME,))
    rows = [
        alert_row(4102, 103, HIGH),
        alert_row(4104, 101, "2026-11-08 15:59:59"),
        alert_row(4105, 101, HIGH, level="warning"),
        alert_row(4106, 201, HIGH, owner=18, message=FOREIGN_NAME),
        alert_row(4107, 101, "2026-11-12 02:30:01"),
    ]
    if case in ("normal", TRANSFER_DURING):
        rows.extend([alert_row(4101, 101, LOW, read=1), alert_row(4103, 103, "2026-11-10 00:00:00")])
        sql(INSERT, alert_row(4111, 101, HIGH, kind="budget_exceed"))
    elif case == "missing":
        rows.append(alert_row(4108, None, HIGH, message="严重 CTR：请复核 #101 北岸搜索"))
    elif case == "cross":
        rows.append(alert_row(4109, 201, HIGH, message=f"严重 CTR：#201 {FOREIGN_NAME}"))
    elif case == "transfer":
        rows.append(alert_row(4110, 102, HIGH))
    elif case == "deleted":
        sql("INSERT INTO ad_plans(id,plan_name,user_id) VALUES(202,'TASK38_DELETED_PLAN',?)", (OWNER,))
        rows.append(alert_row(4111, 202, HIGH))
    for row in rows:
        sql(INSERT, row)
    if case == "transfer":
        sql("UPDATE ad_plans SET user_id=18 WHERE id=102")
    if case == "deleted":
        sql("DELETE FROM ad_plans WHERE id=202")


def fixture_rows(sql):
    return {table: sql(f"SELECT * FROM {table} ORDER BY 1", rows=True)
            for table in ("alerts", "ad_plans", "plan_metric_bindings", "diagnosis_sandbox_targets")}


def migration(output):
    from backend.config.config import settings
    from backend.database import database
    from backend.tools.tools import create_alert, function_map, get_alerts, record_alert, tools_definition
    from tempfile import TemporaryDirectory

    ddl = """CREATE TABLE alerts(id INTEGER PRIMARY KEY, type TEXT, message TEXT,
         level TEXT DEFAULT 'warning', is_read INTEGER DEFAULT 0, user_id INTEGER,
         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
    with TemporaryDirectory(dir=output) as directory, patch.object(
        settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(Path(directory) / "legacy.sqlite3")
    ):
        with closing(database.get_db()) as conn, conn:
            conn.execute(ddl)
            conn.execute("INSERT INTO alerts VALUES(1,'ctr_review','旧告警 #901','critical',0,17,?)", (LOW,))
        database.init_db()
        database.init_db()
        with closing(database.get_db()) as conn, conn:
            conn.executemany("INSERT INTO ad_plans(id,plan_name,user_id) VALUES(?,?,?)",
                             [(901, "own", 17), (902, "foreign", 18)])
        record_alert("ctr_review", "可信来源", "critical", 17, source_plan_id=901)
        record_alert("ctr_review", "跨属主", "critical", 17, source_plan_id=902)
        create_alert("ctr_review", "只有正文 #901", "critical", 17)
        record_alert("ctr_review", "不存在", "critical", 17, source_plan_id=903)
        record_alert("ctr_review", "无显式告警属主", "critical", source_plan_id=901)
        model_tool, = [tool["function"] for tool in tools_definition if tool["function"]["name"] == "create_alert"]
        assert "source_plan_id" not in model_tool["parameters"]["properties"]
        try:
            function_map["create_alert"]("ctr_review", "模型猜测 #901", user_id=17, source_plan_id=901)
        except TypeError:
            pass
        else:
            raise AssertionError("model tool accepted a trusted source argument")
        saved = get_alerts(user_id=17)
        assert [row["plan_id"] for row in reversed(saved)] == [None, 901, None, None, None]
        assert get_alerts(user_id=1)[0]["plan_id"] is None
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE ad_plans SET user_id=18 WHERE id=901")
        after = get_alerts(user_id=17)
        assert all(row["plan_id"] is None for row in after)
    write(output / "migration.json", {"legacy_ddl": ddl, "after_two_upgrades_and_writes": saved,
                                     "after_transfer": after})


def oracle_metrics(bindings, plans):
    sources = {row["plan_id"]: row["source_plan_id"] for row in bindings}
    with closing(sqlite3.connect(ARTIFACT.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
        args = [*(sources[plan] for plan in plans), manifest["complete_start_date"],
                min("2026-11-11", manifest["complete_end_date"])]
        common = dict(conn.execute(COMMON_SQL, args).fetchone())
        width, end = common["width"], date.fromisoformat(common["last_day"])
        dates = {name: {"start_date": (end - timedelta(days=start)).isoformat(),
                        "end_date": (end - timedelta(days=stop)).isoformat()}
                 for name, start, stop in (("baseline", 2 * width - 1, width), ("current", width - 1, 0))}
        targets, metrics = [], []
        for plan in plans:
            windows = []
            for window in dates.values():
                params = [sources[plan], *window.values()]
                totals = dict(conn.execute(WINDOW_SQL, params).fetchone())
                assert totals["days"] == width
                windows.append({"plan_id": plan, **window, **totals, "sql_params": params})
            base, current = windows
            decision = dict(conn.execute(TARGET_SQL, {"bi": base["impressions"], "bc": base["clicks"],
                                                       "ci": current["impressions"], "cc": current["clicks"]}).fetchone())
            if decision["qualifies"]:
                targets.append(plan)
            metrics.append({"plan_id": plan, "windows": windows, **decision})
    return {"common": {"sql": COMMON_SQL, "params": args, "result": common},
            "windows_sql": WINDOW_SQL, "target_sql": TARGET_SQL, "dates": dates,
            "metrics": metrics, "targets": targets}


def freeze(output, sql, frozen):
    migration(output)
    cases = {}
    params = [OWNER, OWNER, OWNER, LOW, HIGH]
    for name in FIXED:
        seed(sql, name)
        matched = sql(ALERT_SQL, params, rows=True)
        ids = list(dict.fromkeys(row["plan_id"] for row in matched if row["plan_id"] is not None))
        starts_complete = name in ("normal", TRANSFER_DURING)
        assert [row["alert_id"] for row in matched] == ([4101, 4103, 4102] if starts_complete else [4102, GAP[name][0]])
        assert ids == ([101, 103] if starts_complete else [103])
        data = fixture_rows(sql)
        oracle = {"alerts": matched, "candidate_plan_ids": ids}
        if starts_complete:
            oracle.update(oracle_metrics(data["plan_metric_bindings"], ids))
            assert oracle["targets"] == [103]
        if name == TRANSFER_DURING:
            oracle["transfer"] = {"plan_id": 103, "from_owner": OWNER, "to_owner": 18}
        cases[name] = {"message": PROMPT, "fixture": name, "data": data, "oracle": oracle}
    write(output / "sql-oracle.json", {"alerts": {"sql": ALERT_SQL, "params": params},
                                      "cases": {name: case["oracle"] for name, case in cases.items()}})
    return {"message": PROMPT, "clock": CLOCK.isoformat(), "cases": cases, "holdout": {}, "rule": frozen["rule"]}


def apply_fixture(sql, frozen, case):
    sql("UPDATE ad_plans SET user_id=? WHERE id=103 AND user_id<>?", (OWNER, OWNER))
    for row in frozen["cases"][case]["data"]["diagnosis_sandbox_targets"]:
        sql("INSERT OR IGNORE INTO diagnosis_sandbox_targets (plan_id,user_id,registration_id) VALUES (?,?,?)",
            (row["plan_id"], row["user_id"], row["registration_id"]))
    seed(sql, case)
    assert fixture_rows(sql) == frozen["cases"][case]["data"]


def instrument(stack, audit):
    from backend.agent.team_runtime import DelegationRun
    from backend.database import alert_review, database
    from backend.observability import add_trace_step

    verify_task37.instrument(stack, audit)
    stack.enter_context(patch.object(alert_review, "now", return_value=CLOCK))
    stack.enter_context(patch.object(alert_review, "read_weekly_alerts",
                                    audit.wrap_read(ALERT, alert_review.read_weekly_alerts)))

    audit.transfer = None
    schedule = DelegationRun._schedule

    def transfer_before_workers(runtime, plan, trace_id, dispatch_id):
        if audit.case == TRANSFER_DURING and audit.transfer is None:
            update = "UPDATE ad_plans SET user_id=? WHERE id=? AND user_id=?"
            params = (18, 103, OWNER)
            with closing(database.get_db()) as conn, conn:
                assert conn.execute(update, params).rowcount == 1
            committed_ns = perf_counter_ns()
            with closing(database.get_db(read_only=True)) as conn:
                current = [dict(row) for row in conn.execute(ALERT_SQL, (OWNER, OWNER, OWNER, LOW, HIGH))]
            audit.transfer = {"dispatch_id": dispatch_id, "trace_id": trace_id, "committed_ns": committed_ns,
                              "update_sql": update, "update_params": params, "current_alerts": current}
            add_trace_step(trace_id, "fixed_fixture", "plan_transferred", parent_call_id=dispatch_id,
                           evidence=audit.transfer)
        return schedule(runtime, plan, trace_id, dispatch_id)

    stack.enter_context(patch.object(DelegationRun, "_schedule", transfer_before_workers))


def export(folder, audit, name):
    verify_task37.export(folder, audit, name)
    if name == TRANSFER_DURING:
        write(folder / "transfer.json", audit.transfer)


class FixedModel:
    def __init__(self, audit, replay):
        self.audit = audit
        self.chat = SimpleNamespace(completions=self)
        self.calls = []
        self.replay = shared.t5.read(ROOT / "tests/task38_initial_glm_sheet.json")

    def create(self, messages, tools=None, **_):
        from backend.agent.tests.test_home_delegation import dispatch, reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call

        def task(task_id, capability, objective, **fields):
            return {"task_id": task_id, "objective": objective, "capabilities": [capability],
                    "expected_output": "本轮证据回执", "stop_condition": "交回回执", **fields}

        def verified_dispatch(receipt):
            tasks = [task(f"plan-{plan}", METRICS, f"取证 #{plan} 两窗") for plan in receipt["candidate_plan_ids"]]
            tasks.append(task("rule", RULE, "核对 ctr-review-v1#low-ctr"))
            tasks.append(task("review", DETAIL, "独立复核", dependencies=[row["task_id"] for row in tasks]))
            return reply("", dispatch("verified-batch", *tasks))

        link = self.audit.model_link.get()
        name, task_id = self.audit.case, link["task_id"]
        returned = [json.loads(row["content"]) for row in messages if row["role"] == "tool"]
        if task_id == "review":
            response = reply("", tool_call("ineligible", name=DETAIL, plan_id=101),
                             tool_call("selected", name=DETAIL, plan_id=103))
        elif task_id == "rule":
            response = (worker_summary("复核范围已失效") if returned else
                        reply("", tool_call("v1-rule", name=RULE, query="ctr-review-v1#low-ctr")))
        elif task_id:
            scope = json.loads(messages[0]["content"].splitlines()[-1])
            response = worker_summary("已交回") if returned else reply("", *(
                tool_call(f"{task_id}-{label}", name=METRICS, plan_id=int(task_id.split("-")[1]), **scope[label])
                for label in ("baseline", "current")))
        elif not returned:
            assert [tool["function"]["name"] for tool in tools] == [ALERT]
            # Exercise the original request against an eager task-sheet call before a model has seen discovery.
            response = reply("", tool_call("discovery", name=ALERT),
                             dispatch("premature", task("guess", DETAIL, "立即查询计划")))
        elif len(returned) == 2:
            receipt, rejected = returned
            assert rejected["error_code"] == "alert_review_scope_incomplete"
            if name == TRANSFER_DURING:
                assert receipt["status"] == "ready"
                response = verified_dispatch(receipt)
            elif receipt["status"] == "ready":
                response = reply("", tool_call("original-sdk-2", name="delegate_tasks", **self.replay["normal_sheet"]))
            else:
                response = reply("", dispatch("after-gap", task("guess", DETAIL, "尝试在缺项时读取详情")))
        elif len(returned) == 3 and name == "normal":
            receipt, _, rejected = returned
            assert rejected["error_code"] == "invalid_task_sheet"
            assert "缺少规则取证任务" in rejected["task_sheet_feedback"]["errors"][0]["reason"]
            response = verified_dispatch(receipt)
        elif name in self.replay["unstructured_replies"]:
            assert not tools and "本轮派单已关闭" in messages[0]["content"]
            response = reply(self.replay["unstructured_replies"][name])
        else:
            response = reply("本轮复核结束。")
        message = response.choices[0].message
        self.calls.append({**link, "case": name, "start_ns": perf_counter_ns(),
                           "body": {"messages": messages.copy(), "tools": tools or []},
                           "assistant_content": message.content,
                           "tool_calls": [{"id": call.id, "function": vars(call.function)}
                                          for call in message.tool_calls or []]})
        return response


def audit_run(output, selected):
    from scripts.audit_task38 import audit
    return audit(output, selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--case", action="append", choices=FIXED)
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ("preflight", "fixed", "live"):
        mode.add_argument("--" + name, action="store_const", dest="mode", const=name)
    args = parser.parse_args()
    os.environ.update(PYTHON_DOTENV_DISABLED="1", AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH="", JWT_SECRET=token_urlsafe(48), ANONYMIZED_TELEMETRY="False",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if args.mode == "live":
        from dotenv import dotenv_values
        key = dotenv_values(ROOT / ".env").get("GLM_API_KEY")
        os.environ.update(LLM_PROVIDER="glm", GLM_MODEL=shared.t5.MODEL)
        if key:
            os.environ.setdefault("GLM_API_KEY", key)
    else:
        os.environ.update(LLM_PROVIDER="mock", API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="", GLM_API_KEY="")
    return shared.run(args.output_dir.resolve(), args.mode,
                      ledger=args.ledger.resolve() if args.ledger else None,
                      suite=sys.modules[__name__], selected=args.case or (NORMAL if args.mode == "live" else FIXED))


if __name__ == "__main__":
    raise SystemExit(main())
