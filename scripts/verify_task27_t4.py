"""Replay the T3 detail actions and a correct multi-window handoff with fixed models."""

import argparse
from collections import Counter
from contextlib import ExitStack, closing
from copy import deepcopy
from fractions import Fraction
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event, Lock
from time import perf_counter_ns
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from scripts.run_task27_t3 import Audit
from scripts.task27_t3_fixture import ALERT, ARTIFACT, LATE as WINDOWS, OWNER, ROOT, digest
from scripts.task27_t3a_fixture import BASE, CASES, MANIFEST, apply_variant
from scripts.verify_task27 import dump, fixture
from scripts.verify_task27_r1 import group_round
from scripts.verify_task27_t1 import METRICS, PLAN, receipts, server


RULE = "search_knowledge"
RULE_QUERY = "点击率下降复核 v1"
DOWNSTREAM = "review_detail"
REPRO = ROOT / "docs/task27-t3a/repro.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def original_call(row):
    return SimpleNamespace(id=row["id"], type=row["type"], function=SimpleNamespace(**row["function"]))


def selected_from_inputs(inputs):
    """Interpret only the tool receipts present in the downstream model's first input."""
    rows = [row for upstream in inputs.values() for row in upstream if row["source"] in {METRICS, RULE}]
    assert len(rows) == 5 and all(row["status"] == "succeeded" for row in rows), rows
    rule, = [row for row in rows if row["source"] == RULE]
    hit, = rule["result"]["evidence"]
    assert (hit["rule_id"], hit["version"]) == ("ctr-review#low-ctr", "v1")
    minimum, decline = re.search(r"各达到 ([\d,]+) 次.*相对下降至少 (\d+)%", hit["excerpt"]).groups()
    metrics = {(row["arguments"]["plan_id"], row["arguments"]["start_date"], row["arguments"]["end_date"]):
               row["result"] for row in rows if row["source"] == METRICS}
    assert set(metrics) == {(plan, *window) for plan in (101, 102) for window in WINDOWS}
    targets = []
    for plan in (101, 102):
        base, current = (metrics[plan, *window] for window in WINDOWS)
        assert all(row["coverage"]["complete"] for row in (base, current))
        prior, now = (Fraction(row["clicks"], row["impressions"]) for row in (base, current))
        if min(base["impressions"], current["impressions"]) >= int(minimum.replace(",", "")) and (
            100 * (prior - now) / prior >= int(decline)
        ):
            targets.append(plan)
    target, = targets
    return target


class FixedModel:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)
        self.release = Event()

    def prepare(self, case, mode, capture, audit):
        self.case, self.mode, self.audit = case, mode, audit
        self.original = capture["original_failure"]
        self.delegate = original_call(self.original["sdk_delegate"])
        self.upstream = [task for task in json.loads(self.delegate.function.arguments)["tasks"]
                         if PLAN not in task["capabilities"]]
        self.raw_sheet = self.original["detail_worker_first_input"]["task"]
        self.detail_id = DOWNSTREAM if mode == "handoff" else self.raw_sheet["task_id"]
        if mode == "worker-local-gap":
            self.upstream = [task for task in self.upstream if "#102" not in task["objective"]]
        self.actions = [original_call(row) for row in self.original["detail_worker_sdk_actions"]]
        self.calls, self.errors = [], []
        self.release, self.ready = Event(), Event()
        self.lock = Lock()

    def observe(self, name, original):
        def read(*args, **kwargs):
            return self.audit.wrap_read(name, original)(*args, **kwargs)
        return read

    def record_receipt(self, record):
        observed = self.audit.wrap_receipt(record)

        def run(*args, **kwargs):
            accepted = observed(*args, **kwargs)
            with self.lock:
                if sum(row["accepted"] and row["source"] in {METRICS, RULE}
                       for row in self.audit.receipts) >= 5:
                    self.ready.set()
            return accepted
        return run

    def create(self, messages, tools=None, response_format=None, **options):
        from backend.agent.tests.test_home_delegation import reply

        link = self.audit.model_link.get()
        current = next(row["content"] for row in reversed(messages) if row["role"] == "user")
        context = json.loads(current) if response_format else None
        returned = receipts(messages)
        row = {**link, "start_ns": perf_counter_ns(), "messages": deepcopy(messages), "tools": tools,
               "response_format": response_format, "options": options, "worker_input": context}
        self.calls.append(row)
        try:
            if context is not None:
                response = self.worker(context, returned)
            elif link["step"] == "review_final_detail":
                # Preserve the original R2 attempt set when the optional final request opens.
                response = reply("")
            elif not tools and not returned:
                response = reply('{"steps":[]}')
            else:
                assert current == self.case["message"]
                response = self.main(returned)
            message = response.choices[0].message
            row["response"] = {"content": message.content, "tool_calls": [
                {"id": call.id, "name": call.function.name, "raw_arguments": call.function.arguments}
                for call in (message.tool_calls or [])]}
            row["end_ns"] = perf_counter_ns()
            return response
        except Exception as error:
            self.errors.append(f"{type(error).__name__}: {error}")
            self.release.set()
            raise

    def worker(self, context, returned):
        from backend.agent.tests.test_home_delegation import reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call

        task = context["task"]
        task_id = task["task_id"]
        if task_id == self.detail_id:
            if any(call.id in returned for call in self.actions) or "selected" in returned:
                self.release.set()
                return worker_summary("详情尝试已结束")
            if self.mode == "handoff":
                return reply("", tool_call("selected", name=PLAN, plan_id=selected_from_inputs(context["inputs"])))
            if self.mode == "worker-local-gap":
                if not returned:
                    return reply("", *(tool_call(f"local-gap-{start}", name=METRICS, plan_id=102,
                                                 start_date=start, end_date=end) for start, end in WINDOWS))
                assert len(returned) == 2 and all(row["error_code"] == "plan_history_not_mapped"
                                                   for row in returned.values())
            if self.case["id"] != "branch-1":
                assert self.ready.wait(10), "upstream evidence did not arrive before replay"
            return reply("", *self.actions)

        if returned:
            missing = any(not row["success"] for row in returned.values())
            return worker_summary("取证结束", status="missing" if missing else "succeeded")
        if RULE in task["capabilities"]:
            return reply("", tool_call("rule-v1", name=RULE, query=RULE_QUERY))
        plan_id = int(re.search(r"#(10[12])", task["objective"])[1])
        if self.mode == "worker" and self.case["id"] == "branch-1" and plan_id == 101:
            assert self.release.wait(10), "early detail worker did not return before the held evidence"
        return reply("", *(tool_call(f"metric-{plan_id}-{start}", name=METRICS, plan_id=plan_id,
                                     start_date=start, end_date=end) for start, end in WINDOWS))

    def main(self, returned):
        from backend.agent.tests.test_home_delegation import dispatch, reply
        from backend.agent.tests.test_home_sse_tracing import tool_call

        if not returned:
            return reply("", tool_call("alerts", name="get_alerts", unread_only=True))
        assert any(row["level"] == "critical" for row in returned["alerts"]["result"])
        if self.mode == "worker" and self.delegate.id not in returned:
            return reply("", self.delegate)
        if self.mode == "main" and self.case["id"] == "branch-1" and self.actions[0].id not in returned:
            return reply("", *self.actions)
        if self.mode != "worker" and "upstream" not in returned:
            tasks = self.upstream
            if self.mode == "handoff":
                tasks = [self.handoff_sheet(), *tasks]
            elif self.mode == "worker-local-gap":
                tasks = [*tasks, {**self.raw_sheet, "capabilities": [PLAN, METRICS]}]
            return reply("", dispatch("upstream", *tasks))
        if self.mode == "main" and self.actions[0].id not in returned:
            return reply("", *self.actions)
        # Copy only successful detail receipts available to the main model into the candidate reply.
        details, facts, gaps = [], [], []
        for call_id, row in returned.items():
            if call_id in {call.id for call in self.actions} and row.get("success"):
                details.append(row["result"])
            for item in row.get("evidence", []):
                result = item["result"]
                if item["source"] == PLAN:
                    details.append(result)
                elif item["source"] == METRICS:
                    facts.append(f"#{result['plan_id']} {result['start_date']} 至 {result['end_date']} "
                                 f"曝光 {result['impressions']}，点击 {result['clicks']}，CTR {result['ctr_pct']:.2f}%")
                elif item["source"] == RULE:
                    facts.extend(f"{hit['chunk_id']} {hit['excerpt']}" for hit in result["evidence"])
            gaps.extend(item for item in row.get("gaps", []) if item["source"] == METRICS)
        if gaps:
            facts.append("#102 未绑定历史来源，暂缓完整复核清单。")
        facts.extend(f"#{row['id']} {row['plan_name']} {row['status']} 日预算 {row['daily_budget']}" for row in details)
        return reply("\n".join(facts))

    def handoff_sheet(self):
        deps = [task["task_id"] for task in self.upstream]
        return {"task_id": DOWNSTREAM, "capabilities": [PLAN], "dependencies": deps,
                "input_refs": deps,
                "objective": "以两条计划的四窗指标和精确 v1 条文计算唯一入选计划，仅查询该计划详情。",
                "expected_output": "来自实际工具回执的计划编号、名称、状态及日预算。",
                "stop_condition": "完成一次详情读取后结束。"}


def sql_oracle(sql, frozen, case):
    source = frozen["source"]
    sql("DELETE FROM plan_metric_bindings")
    for plan, campaign in case["bindings"].items():
        if campaign:
            sql("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, ?)",
                (int(plan), source["source_id"], source["data_version"], campaign))
    bindings = sql("SELECT * FROM plan_metric_bindings ORDER BY plan_id", rows=True)
    plans = sql(frozen["plan_sql"], (OWNER,), rows=True)
    campaigns = {str(row["plan_id"]): row["source_plan_id"] for row in bindings}
    rule = case["expected"]["rule"]
    metrics = {}
    with closing(sqlite3.connect(ARTIFACT.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        for plan, campaign in case["bindings"].items():
            assert campaigns.get(plan) == campaign
            windows = []
            for start, end in WINDOWS:
                params = [campaign, start, end, None]
                row = dict(conn.execute(frozen["sql"]["window"], params).fetchone())
                row["missing_dates"] = row["missing_dates"].split(",") if row["missing_dates"] else []
                windows.append({**row, "sql_params": params, "start_date": start, "end_date": end})
            base, current = windows
            params = [base["impressions"], current["impressions"], base["ctr_pct"], current["ctr_pct"],
                      rule["min_impressions"], rule["min_relative_decline_pct"]]
            metrics[plan] = {**dict(conn.execute(frozen["sql"]["review"], params).fetchone()), "windows": windows,
                             "review_sql_params": params, "unbound": campaign is None}
    assert metrics == case["expected"]["metrics"] and plans == frozen["plans"]
    targets = [int(plan) for plan, row in metrics.items() if row["qualifies"]]
    target = targets[0] if len(targets) == 1 and all(row["qualifies"] is not None for row in metrics.values()) else None
    assert target == (case["expected"]["detail"] or {}).get("id")
    return {"target": target, "bindings": bindings, "metrics": metrics, "plans": plans, "rule": rule}


def verify_lifecycle(sample, model, sql):
    audit = model.audit
    assert not model.errors, model.errors
    assert all(row["trace_id"] == sample["trace_id"] for row in (*model.calls, *audit.reads, *audit.receipts))
    terminal, = [event for event in sample["sse"] if event["type"] in {"done", "error"}]
    assert terminal == sample["sse"][-1] and terminal["type"] == "done"
    answer, = [event["content"] for event in sample["sse"] if event["type"] == "text"]
    entries = sample["history"]["entries"]
    committed = sql("SELECT kind, content FROM chat_group_entries WHERE run_id=? ORDER BY id",
                    (sample["run_id"],), rows=True)
    sample["committed_entries"] = committed
    assert committed == [{key: row[key] for key in ("kind", "content")} for row in entries]
    assert committed[0] == {"kind": "user", "content": model.case["message"]}
    assert committed[-1] == {"kind": "assistant", "content": answer}
    assert sample["history"]["runs"] == [
        {"run_id": sample["run_id"], "trace_id": sample["trace_id"], "status": "completed"}]
    trace = sample["trace"]
    assert trace["status"] == "completed"
    steps = trace["steps"]
    created, reclaimed = (Counter(step["runtime_call_id"] for step in steps
                                  if step["stage"] == "worker" and step["action"] == state)
                          for state in ("created", "reclaimed"))
    events = [row["event"] for row in entries if row["kind"] == "worker"]
    assert created == reclaimed == Counter(row["worker_id"] for row in audit.work)
    for state in ("dispatched", "reclaimed"):
        assert created == Counter(event["worker_id"] for event in events if event["kind"] == state)
    return answer, entries, steps, reclaimed, events


def verify(sample, model, sql, *, repaired=False):
    audit, case, mode = model.audit, model.case["id"], model.mode
    expected = sample["independent_sql"]
    answer, entries, steps, reclaimed, events = verify_lifecycle(sample, model, sql)

    final = next(row for row in reversed(model.calls) if row["worker_input"] is None)
    returned = receipts(final["messages"])
    batch_id = model.delegate.id if mode == "worker" else "upstream"
    first_batch = returned[batch_id] if batch_id in returned else sample["direct_results"][batch_id]
    assert all(first_batch["states"][task["task_id"]] == (
        "failed" if case == "gap-1" and "#102" in task["objective"] else "succeeded") for task in model.upstream)
    assert Counter(row["tool"] for row in audit.reads if row["tool"] not in {PLAN, "query_plan_detail"}) == {
        "get_alerts": 1, METRICS: 4, RULE: 1}
    upstream_ids = {task["task_id"] for task in model.upstream}
    evidence_ids = upstream_ids | ({model.detail_id} if mode == "worker-local-gap" else set())
    upstream = [row for row in audit.receipts if row["accepted"] and row["task_id"] in evidence_ids
                and row["source"] in {METRICS, RULE}]
    assert Counter(row["source"] for row in upstream) == {METRICS: 4, RULE: 1}
    rule, = [row for row in upstream if row["source"] == RULE]
    hit, = rule["result"]["evidence"]
    assert all(hit[key] == expected["rule"][key] for key in ("chunk_id", "rule_id", "version", "title"))
    assert hit["excerpt"] == expected["rule"]["content"] and not hit["truncated"]
    for row in upstream:
        if row["source"] == METRICS:
            args = row["arguments"]
            plan = expected["metrics"][str(args["plan_id"])]
            window, = [window for window in plan["windows"] if window["start_date"] == args["start_date"]]
            assert window["end_date"] == args["end_date"]
            if plan["unbound"]:
                assert row["error_code"] == "plan_history_not_mapped" and row["status"] == "failed"
            else:
                result = row["result"]
                assert row["status"] == "succeeded" and result["source_plan_id"] == window["sql_params"][0]
                assert all(result[key] == window[key] for key in ("impressions", "clicks"))
                assert result["coverage"]["observed_days"] == window["observed_days"]
    ready_ns = max(row["ns"] for row in upstream)
    reads = [row for row in audit.reads if row["tool"] == PLAN]
    leaves = [row for row in audit.reads if row["tool"] == "query_plan_detail"]
    assert [(row["runtime_call_id"], row["arguments"]["plan_id"], row["arguments"]["user_id"]) for row in reads] == [
        (row["runtime_call_id"], row["arguments"]["plan_id"], row["arguments"]["user_id"]) for row in leaves]
    for row in leaves:
        plan, = [plan for plan in expected["plans"] if plan["id"] == row["arguments"]["plan_id"]]
        assert row["arguments"]["user_id"] == OWNER and all(row["result"][key] == val for key, val in plan.items())

    detail_models = [row for row in model.calls if (row["worker_input"] or {}).get("task", {}).get("task_id")
                     == model.detail_id]
    attempts = []
    for model_call in model.calls:
        for call in model_call["response"]["tool_calls"]:
            if call["name"] != PLAN:
                continue
            plan = json.loads(call["raw_arguments"])["plan_id"]
            actual = [row for row in leaves if row["tool_call_id"] == call["id"]]
            admitted = [step for step in steps if step["stage"] == "receipt" and step["action"] == "accepted"
                        and step.get("runtime_call_id") in {row["runtime_call_id"] for row in actual}]
            tool_receipt = next((receipts(row["messages"])[call["id"]] for row in model.calls
                                 if row["worker_input"] == model_call["worker_input"]
                                 and call["id"] in receipts(row["messages"])), None)
            if tool_receipt is None and model_call["role"] == "orchestrator":
                tool_receipt = sample["direct_results"][call["id"]]
            assert len(actual) <= 1 and len(admitted) <= 1
            if admitted:
                assert tool_receipt["success"] and tool_receipt.get("artifact_id")
            allowed = case != "gap-1" and not (case == "branch-1" and mode != "handoff") and plan == expected["target"]
            attempts.append({**call, "plan_id": plan, "role": model_call["role"], "response_ns": model_call["end_ns"],
                             "expected_allowed": allowed, "leaf_reads": len(actual), "accepted_receipts": len(admitted),
                             "tool_receipt": tool_receipt, "sse_disclosed": disclosed(answer, plan, expected),
                             "history_disclosed": disclosed(json.dumps(entries, ensure_ascii=False), plan, expected)})
    targets = [row["arguments"]["plan_id"] for row in leaves]
    if mode == "handoff":
        if case == "gap-1":
            blocked, = [row for row in first_batch["gaps"] if row["source"] == "scheduler"]
            failed, = [task for task in upstream_ids if first_batch["states"][task] == "failed"]
            assert blocked["task_id"] == DOWNSTREAM and blocked["error_code"] == "dependency_failed"
            assert blocked["result"] == {"dependencies": [failed]} and first_batch["states"][DOWNSTREAM] == "blocked"
            assert not detail_models and not attempts and not leaves
            assert any(event.get("error_code") == "dependency_failed"
                       and (repaired or event.get("task_id") == DOWNSTREAM) for event in events)
            assert all(not disclosed(answer, plan, expected) for plan in (101, 102)) and "暂缓" in answer
            assert all(not disclosed(json.dumps(entries, ensure_ascii=False), plan, expected) for plan in (101, 102))
            sample["blocked"] = blocked
        else:
            assert len(detail_models) == 2 and targets == [expected["target"]] and len(attempts) == 1
            inputs = {task: [row for row in rows if row["source"] in {METRICS, RULE}]
                      for task, rows in detail_models[0]["worker_input"]["inputs"].items()}
            sheet = detail_models[0]["worker_input"]["task"]
            assert set(sheet["dependencies"]) == set(sheet["input_refs"]) == set(inputs) == upstream_ids
            assert {(task, row["artifact_id"]) for task, rows in inputs.items() for row in rows} == {
                (row["task_id"], row["artifact_id"]) for row in upstream}
            admitted = {row["artifact_id"]: row for row in upstream}
            assert all(all(row[key] == admitted[row["artifact_id"]][key] for key in ("source", "arguments", "result"))
                       for rows in inputs.values() for row in rows)
            assert ready_ns < detail_models[0]["start_ns"] < leaves[0]["start_ns"]
            assert attempts[0]["sse_disclosed"] and attempts[0]["history_disclosed"]
            sample["downstream_first_input"] = detail_models[0]["worker_input"]
    else:
        assert [row["id"] for row in attempts] == [call.id for call in model.actions]
        assert [row["raw_arguments"] for row in attempts] == [call.function.arguments for call in model.actions]
        assert all((row["response_ns"] < ready_ns) == (case == "branch-1") for row in attempts)
        assert all((row["start_ns"] < ready_ns) == (case == "branch-1") for row in leaves)
        if mode == "worker":
            assert detail_models[0]["worker_input"] == model.original["detail_worker_first_input"]
            dispatch_call, = [call for row in model.calls for call in row["response"]["tool_calls"]
                              if call["id"] == model.delegate.id]
            assert dispatch_call["raw_arguments"] == model.delegate.function.arguments
        if mode == "worker-local-gap" or (mode == "main" and case == "gap-1"):
            call, = [row for row in model.calls if row["end_ns"] == attempts[0]["response_ns"]]
            seen = receipts(call["messages"])
            assert (len(seen) == 2 and all(row["error_code"] == "plan_history_not_mapped" for row in seen.values())
                    if mode == "worker-local-gap" else
                    any(row["error_code"] == "plan_history_not_mapped" for row in seen["upstream"]["gaps"]))
        sample["downstream_first_input"] = detail_models[0]["worker_input"] if detail_models else None

    sample.update(status="passed", attempts=attempts, evidence_ready_ns=ready_ns, upstream_artifacts=upstream,
                  answer=answer, detail_targets=targets, downstream_model_calls=len(detail_models),
                  worker_reclaimed=len(reclaimed), read_counts={role: sum(row["role"] == role for row in leaves)
                                                              for role in ("orchestrator", "team_worker")},
                  admission_violations=sum(not row["expected_allowed"] and bool(row["leaf_reads"] or
                      row["accepted_receipts"] or row["sse_disclosed"] or row["history_disclosed"]) for row in attempts))


def disclosed(text, plan_id, expected):
    plan, = [row for row in expected["plans"] if row["id"] == plan_id]
    return all(str(plan[field]) in text for field in ("plan_name", "status", "daily_budget"))


def run_sample(directory, model, sql, sample, client, verify_sample=verify):
    from backend import observability
    from backend.agent import home_react, llm_provider, team_runtime, team_tools
    from backend.tools import tools

    audit = model.audit
    direct_results = sample["direct_results"] = {}
    original_call = home_react.HomeReAct.call

    def observe_direct(react, call_id, name, raw, **kwargs):
        result = original_call(react, call_id, name, raw, **kwargs)
        direct_results[call_id] = deepcopy(result)
        return result

    with ExitStack() as stack:
        stack.enter_context(patch.object(home_react.HomeReAct, "call", observe_direct))
        for module in (llm_provider, team_runtime):
            stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        for module in (home_react, team_tools):
            stack.enter_context(patch.object(module, "trace_tool_call", audit.trace_tool(module.trace_tool_call)))
        runtime = team_runtime.DelegationRun
        stack.enter_context(patch.object(runtime, "_record", model.record_receipt(runtime._record)))
        stack.enter_context(patch.object(runtime, "_work", audit.wrap_work(runtime._work)))
        stack.enter_context(patch.object(tools, RULE, model.observe(RULE, getattr(tools, RULE))))
        extra = ("get_alerts", *getattr(model, "observed_extra_tools", ()))
        stack.enter_context(patch.dict(tools.function_map, {name: audit.wrap_read(name, tools.function_map[name])
                                                          for name in extra}))
        stack.enter_context(patch.object(tools, "query_plan_detail", audit.wrap_read(
            "query_plan_detail", tools.query_plan_detail)))
        try:
            group_round(client, sample["id"], model.case["message"], sample)
            sample["trace"] = observability.get_trace(sample["trace_id"])
            verify_sample(sample, model, sql)
        finally:
            sample.update(model_calls=model.calls, model_errors=model.errors, tool_reads=audit.reads,
                          worker_executions=audit.work, worker_receipts=audit.receipts)
            dump(directory / (sample["id"] + ".json"), sample)


def main(repair=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mysqld", type=Path)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--mode", choices=("main", "worker", "worker-local-gap", "handoff"), action="append")
    args = parser.parse_args()
    assert __debug__ and (not args.mysqld or args.mysqld.is_file())
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH="", API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="",
                      JWT_SECRET=token_urlsafe(48), ANONYMIZED_TELEMETRY="False", LLM_PROVIDER="mock",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    from backend.agent.conditional_detail import parse_detail_condition
    from backend.config.config import settings
    from backend.database.database import DEFAULT_DB_PATH
    from backend.rag import rag_knowledge
    from backend.rag.review_rule_demo import seed_review_rules
    from scripts.eval_review_retrieval import FixedReviewEmbedding

    frozen = apply_variant(load(BASE), BASE)
    captures = {row["id"]: row for row in load(REPRO)["captures"]}
    assert digest(ARTIFACT) == frozen["source"]["artifact_sha256"]
    before = {str(path): digest(path) for path in (DEFAULT_DB_PATH, ARTIFACT, BASE, MANIFEST, REPRO)}
    password, model = token_urlsafe(24), repair.FixedModel() if repair else FixedModel()
    report = {"task": "TASK-027-R2" if repair else "TASK-027-T4",
              "database": "MySQL 8.4" if args.mysqld else "SQLite", "status": "failed",
              "ad041_admission": "unverified", "real_model_requests": 0, "rounds": [], "oracle_sql": frozen["sql"]}
    send = httpx.Client.send
    external = []

    def loopback_only(client, request, **kwargs):
        if request.url.scheme != "http" or request.url.host != "127.0.0.1":
            external.append({"scheme": request.url.scheme, "host": request.url.host})
            raise AssertionError("External HTTP is disabled during fixed replay")
        return send(client, request, **kwargs)

    try:
        with (fixture(directory, args.mysqld.resolve() if args.mysqld else None, password) as sql,
              TemporaryDirectory(dir=directory, prefix="knowledge-") as knowledge,
              patch.object(settings, "RAG_DB_PATH", knowledge),
              patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding),
              patch.object(httpx.Client, "send", loopback_only),
              server(directory, model, SimpleNamespace(path=ARTIFACT)) as url,
              httpx.Client(base_url=url, timeout=45, trust_env=False) as client):
            seed_review_rules(rag_knowledge.get_rag_knowledge())
            captcha = client.get("/api/captcha").json()
            login = client.post("/api/login", json={"username": "task27-owner", "password": password,
                                                   "captcha_id": captcha["captcha_id"], "captcha_text": "0726"})
            assert login.status_code == 200 and login.json()["code"] == 200
            client.headers["Authorization"] = "Bearer " + login.json()["data"]["token"]
            report["engine_version"] = sql(
                "SELECT @@version AS version" if args.mysqld else "SELECT sqlite_version() AS version", rows=True)[0]["version"]
            for plan in frozen["plans"]:
                sql("UPDATE ad_plans SET plan_name=?, status=?, daily_budget=? WHERE id=? AND user_id=?",
                    (plan["plan_name"], plan["status"], plan["daily_budget"], plan["id"], OWNER))
            sql("INSERT INTO alerts (type, message, level, is_read, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (*ALERT.values(), "2026-10-31 09:00:00"))
            assert sql(frozen["alert_sql"], (OWNER,), rows=True) == [ALERT]
            for case in frozen["cases"]:
                if args.case and case["id"] not in args.case:
                    continue
                capture = captures[case["id"]]
                assert capture["bindings"] == case["bindings"] and capture["expected_detail"] == case["expected"]["detail"]
                assert parse_detail_condition(case["message"]) == (False, None)
                expected = sql_oracle(sql, frozen, case)
                modes = ["main", "worker", "handoff"]
                if case["id"] == "gap-1":
                    modes.append("worker-local-gap")
                for mode in modes:
                    if args.mode and mode not in args.mode:
                        continue
                    name = f"{case['id']}-{mode}"
                    sample = {"id": name, "case": case["id"], "mode": mode, "status": "failed",
                              "independent_sql": expected, "original_trace_id": capture["original_failure"]["trace_id"]}
                    audit = Audit(directory / "sdk.sqlite3", None, name, 0)
                    audit.case = name
                    model.prepare(case, mode, capture, audit)
                    if repair:
                        model.poison = repair.poison(expected["plans"])
                    report["rounds"].append(sample)
                    run_sample(directory, model, sql, sample, client, repair.verify if repair else verify)
                    assert not audit.rows() and not external
                    print(f"{name}: {sample['trace_id']} {sample['detail_targets']} "
                          f"violations={sample['admission_violations']}", flush=True)
            if repair and not (args.case or args.mode):
                repair.run_boundaries(directory, model, sql, client, frozen, captures, report)
            assert not external
        assert report["rounds"]
        count = sum(row["admission_violations"] for row in report["rounds"])
        report.update(admission_violations=count, ad041_admission=(
            "failed" if count else "passed" if len(report["rounds"]) == 10 else "unverified"))
        if repair:
            repair.summarize(report)
        report["status"] = "passed"
    except Exception as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report.update(file_sha256_before=before, file_sha256_after={path: digest(Path(path)) for path in before},
                      blocked_external_http=external)
        report["rounds"] = [{key: row[key] for key in (
            "id", "case", "mode", "status", "trace_id", "run_id", "detail_targets", "read_counts", "worker_reclaimed",
            "downstream_model_calls", "admission_violations", "blocked_attempts", "allowed_attempts")
            if key in row} for row in report["rounds"]]
        dump(directory / "report.json", report)
        assert report["file_sha256_before"] == report["file_sha256_after"]


if __name__ == "__main__":
    main()
