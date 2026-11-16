"""Run business-only workgroup acceptance in Chromium and audit actual GLM decisions."""

import argparse
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
import sys
from time import perf_counter_ns
from types import SimpleNamespace
from unittest.mock import patch

from scripts import verify_task36 as shared
from scripts.task27_t3_fixture import OWNER, ROOT, write
from scripts.verify_task28 import DETAIL, METRICS, RULE


SCOPE = ("#101 和 #103，基准窗 2026-10-17 至 2026-10-23，"
         "当前窗 2026-10-24 至 2026-10-30，按《点击率下降复核》v1。")
EVIDENCE = (
    "周会上要判断这两条计划是否达到独立复核条件。" + SCOPE
    + "请根据两段数据的覆盖、曝光和点击率判断。证据齐全且确认达到复核条件后，只打开入选计划的详情，"
      "给我计划名、当前状态和日预算。有任何一窗或条文缺失就说清楚缺什么，暂缓出清单，也别打开详情。"
)
CASES = {
    "status-a": {"fixture": "complete", "kind": "status", "message": "#101 现在还在投放吗？"},
    "status-b": {"fixture": "complete", "kind": "status", "message": "帮我看一眼 #101 目前的投放状态。"},
    "known-a": {"fixture": "complete", "kind": "review", "message":
        "例会需要一份 CTR 独立复核清单。" + SCOPE +
        "请核对两窗的覆盖、曝光、点击、CTR 和相对变化，再给出入选清单。"
        "清单确定后，只查入选计划的详情，列出名称、当前状态和日预算。"},
    "known-b": {"fixture": "complete", "kind": "review", "message":
        "麻烦独立复核一下这两条计划的点击率变化，给我一份需要跟进的清单。" + SCOPE +
        "请把两窗样本和降幅核实清楚。确定清单后才看入选计划的详情，我需要名称、状态和日预算。"},
    "evidence-complete": {"fixture": "complete", "kind": "review", "message": EVIDENCE},
    "evidence-missing": {"fixture": "missing", "kind": "review", "message": EVIDENCE},
}
HOLDOUT = {"rewrite": {"fixture": "complete", "kind": "review", "message":
    "我在准备月度投放复盘。麻烦做一次点击率下滑的独立复核。" + SCOPE +
    "标出达到门槛的计划，写清两窗覆盖和降幅。完成核验后仅查看入选计划的明细，清单上放名称、状态、日预算。"
    "某窗没历史或没有可核验的条文就把缺项说清，清单暂缓。"},
    "status-rewrite": {"fixture": "complete", "kind": "status", "message": "请查看广告计划 #101 当前投放状态。"}}


class Audit(shared.LiveAudit):
    run_cap = 220


class FixedModel:
    """Replay the rejected live sheets and a malformed evidence summary on the original prompts."""

    def __init__(self, audit, replay):
        self.audit = audit
        self.chat = SimpleNamespace(completions=self)
        self.calls = []
        self.rejected_sheets = shared.t5.read(replay or ROOT / "tests/task37_initial_task_sheets.json")

    def create(self, messages, tools=None, **_):
        from backend.agent.tests.test_home_delegation import dispatch, reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call

        link = self.audit.model_link.get()
        name = self.audit.case
        case = CASES[name]
        task_id = link["task_id"]
        returned = [json.loads(row["content"]) for row in messages if row["role"] == "tool"]
        if task_id:
            if task_id == "review":
                response = reply("", tool_call("selected", name=DETAIL, plan_id=103))
            elif task_id == "rule":
                assert not returned, "verified rule should finish the rule-only worker"
                response = reply("", tool_call("v1-rule", name=RULE, query="ctr-review-v1#low-ctr"))
            elif returned:
                response = reply("规则和指标回执已交回") if task_id == "plan-103" else worker_summary("已交回")
            else:
                plan = int(task_id.split("-")[1])
                response = reply("", *(tool_call(f"{task_id}-{label}", name=METRICS, plan_id=plan, **window)
                                      for label, window in shared.CASE.dates["new"].items()))
        else:
            request = next(row["content"] for row in reversed(messages) if row["role"] == "user")
            assert case["message"] in request
            if link["step"] == "plan":
                response = reply('{"steps":[]}')
            elif case["kind"] == "status":
                assert "直接调用一次 get_plan_detail" in messages[0]["content"]
                assert not returned, "verified status should finish the turn"
                response = reply("", tool_call("status", name=DETAIL, plan_id=101))
            else:
                assert [tool["function"]["name"] for tool in tools] == ["delegate_tasks"]
                response = self.review(name, returned, dispatch, reply, tool_call)
        message = response.choices[0].message
        self.calls.append({**link, "case": name, "start_ns": perf_counter_ns(),
                           "body": {"messages": messages.copy(), "tools": tools or []},
                           "tool_calls": [{"id": action.id, "function": vars(action.function)}
                                          for action in message.tool_calls or []]})
        return response

    def review(self, name, returned, dispatch, reply, tool_call):
        def task(task_id, capability, **fields):
            return {"task_id": task_id, "objective": f"核验 {task_id}", "capabilities": [capability],
                    "expected_output": "本轮实际证据", "stop_condition": "交回回执", **fields}

        rejected = len(self.rejected_sheets) if name == "known-b" else 0
        if len(returned) < rejected:
            return reply("", tool_call(f"rejected-{len(returned)}", name="delegate_tasks",
                                       **self.rejected_sheets[len(returned)]))
        if len(returned) == rejected:
            assert all(row["error_code"] == "invalid_task_sheet" for row in returned)
            tasks = [task("plan-101", METRICS), task("plan-103", METRICS), task("rule", RULE)]
            if name.startswith("known-"):
                tasks.append(task("review", DETAIL, dependencies=["plan-101", "plan-103", "rule"]))
            return reply("", dispatch("first", *tasks))
        state = returned[-1]["independent_review"]
        if name == "evidence-complete" and state["status"] == "review_required":
            return reply("", dispatch("append", task("review", DETAIL, artifact_refs=state["artifact_refs"])))
        assert state["status"] == ("evidence_pending" if name == "evidence-missing" else "completed")
        return reply("本轮核验结束。")


def freeze(output, sql, frozen):
    from backend.agent.multi_window_review import parse_review_goal
    from backend.tools.tools import get_alerts

    status_sql = "SELECT id,plan_name,status,daily_budget FROM ad_plans WHERE id=? AND user_id=?"
    mapping_sql = "SELECT plan_id,source_id,data_version,source_plan_id FROM plan_metric_bindings WHERE plan_id IN (101,103) ORDER BY plan_id"
    alerts_sql = ("SELECT * FROM alerts WHERE user_id=? AND is_read=0 ORDER BY id DESC LIMIT 20")
    status, = sql(status_sql, (101, OWNER), rows=True)
    alerts = get_alerts(unread_only=True, user_id=OWNER)
    assert alerts == sql(alerts_sql, (OWNER,), rows=True)
    probe = ("从当前未读严重 CTR 告警找出相关计划，按《点击率下降复核》v1 独立复核并形成清单。"
             "基准窗 2026-10-17 至 2026-10-23，当前窗 2026-10-24 至 2026-10-30。"
             "清单确定后只读取入选计划的详情。")
    recognized, scope = parse_review_goal(probe)
    write(output / "alert-preflight.json", {
        "tool": "get_alerts", "arguments": {"unread_only": True, "user_id": OWNER},
        "sql": alerts_sql, "sql_params": [OWNER], "result": alerts,
        "plan_link_fields": sorted(set().union(*(row.keys() for row in alerts)) & {"plan_id", "plan_ids", "source_plan_id"}),
        "alert_fixture": "严重 CTR 波动，无计划编号", "probe": probe,
        "review_intent_recognized": recognized, "review_scope_resolved": scope is not None,
    })
    complete = sql(mapping_sql, rows=True)
    sql("DELETE FROM plan_metric_bindings WHERE plan_id=101")
    missing = sql(mapping_sql, rows=True)
    binding = next(row for row in complete if row["plan_id"] == 101)
    sql("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, ?)", tuple(binding.values()))
    assert missing == [row for row in complete if row["plan_id"] != 101]
    write(output / "business-oracle.json", {
        "status": {"sql": status_sql, "params": [101, OWNER], "result": status},
        "history_bindings": {"sql": mapping_sql, "complete": complete, "missing": missing},
        "history_sql": "sql-oracle.json", "complete_target": [103], "missing_target": [],
    })
    return {**{key: value for key, value in frozen.items() if key != "message"},
            "cases": CASES, "holdout": HOLDOUT, "status": status, "scoring": {
        "status": "回答 #101 当前 active；详情 #101 一次、状态修改和提案零；另行核算 Worker。",
        "review": "四窗按计划与日期对 SQL 和指定条文。齐证据仅 #103 详情实读和成功回执各一，"
                  "原答复仅 #103 入选、一个待审批暂停提案、执行零。",
        "missing": "#101 两窗无历史绑定，保留 #103 两窗和 v1 条文；详情实读、成功回执、暂停提案均零。",
        "path": "直查以底层调用及 Worker 核验。同批以已接受任务单、依赖和模型时序核验。"
                "分批以首批实际回执、后续 SDK 任务单和 Worker 首份请求逐对核验产物。路径不改变业务评分。",
    }}


def instrument(stack, audit):
    from backend.tools import plan_metrics, tools

    stack.enter_context(patch.object(tools, "search_knowledge", audit.wrap_read("search_knowledge", tools.search_knowledge)))
    stack.enter_context(patch.dict(tools.function_map, {
        name: audit.wrap_read(name, tools.function_map[name]) for name in ("get_alerts", "get_plans_summary")
    }))

    audit.metric_selects = []

    def connect(database_uri, **kwargs):
        conn = sqlite3.connect(database_uri, **kwargs)
        link = audit.tool_link.get()
        if link:
            def observe(statement):
                if "FROM plan_daily_metrics" in statement:
                    audit.metric_selects.append({**link, "case": audit.case, "uri": database_uri,
                                                 "uri_enabled": kwargs.get("uri"), "sql": statement,
                                                 "ns": perf_counter_ns()})
            conn.set_trace_callback(observe)
        return conn

    stack.enter_context(patch.object(plan_metrics, "sqlite3", SimpleNamespace(
        connect=connect, Row=sqlite3.Row, Error=sqlite3.Error)))


def export(folder, audit, name):
    write(folder / "criteo-selects.json", [row for row in audit.metric_selects if row["case"] == name])


def audit_run(output, selected):
    from scripts.audit_task37 import audit_run
    return audit_run(output, selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--case", action="append", choices=(*CASES, *HOLDOUT))
    parser.add_argument("--ledger", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--fixed", action="store_true")
    args = parser.parse_args()
    os.environ.update(PYTHON_DOTENV_DISABLED="1", AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH="", JWT_SECRET=token_urlsafe(48), ANONYMIZED_TELEMETRY="False",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if args.live:
        from dotenv import dotenv_values
        config = dotenv_values(ROOT / ".env")
        os.environ.update(LLM_PROVIDER="glm", GLM_MODEL=shared.t5.MODEL)
        if config.get("GLM_API_KEY"):
            os.environ.setdefault("GLM_API_KEY", config["GLM_API_KEY"])
    else:
        os.environ.update(LLM_PROVIDER="mock", API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="", GLM_API_KEY="")
    return shared.run(args.output_dir.resolve(), "live" if args.live else "fixed" if args.fixed else "preflight",
                      ledger=args.ledger.resolve() if args.ledger else None,
                      suite=sys.modules[__name__], selected=args.case or list(CASES))


if __name__ == "__main__":
    raise SystemExit(main())
