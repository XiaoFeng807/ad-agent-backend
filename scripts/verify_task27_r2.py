"""Fail the offline dual-database gate on any T4 admission or disclosure regression."""

from collections import Counter
from functools import wraps
import json
import re
import sys

from scripts import verify_task27_t4 as t4
from scripts.verify_task27_t4 import METRICS, OWNER, PLAN, RULE, RULE_QUERY, WINDOWS, receipts


DETAIL_FIELDS = ("id", "plan_name", "status", "daily_budget")
FABRICATED = "名称 R2_MODEL_ONLY_NAME｜状态 r2_fake_status｜日预算 987654.32"
BYPASSES = {
    "compare_plans": {"plan_id_1": 101, "plan_id_2": 102}, "get_plans_summary": {},
    "get_account_detail": {"account_id": 1}, "get_activity_timeline": {},
    "query_data": {"query": "读取 #101 和 #102 的计划详情"},
}
BOUNDARIES = {
    "wrong-window": {"metric-wrong-window": "review_metric_out_of_scope", "detail": "review_metrics_pending"},
    "wrong-version": {"rule": "review_rule_out_of_scope", "detail": "review_rule_missing"},
    "missing-rule": {"rule": "knowledge_missing", "detail": "review_rule_missing"},
    "rule-conflict": {"rule": "review_rule_conflict", "detail": "review_rule_conflict"},
    "source-version": {"detail": "review_source_mismatch"},
    "wrong-owner": {"metric-102-1": "invalid_metrics_receipt", "detail": "invalid_metrics_receipt"},
    "duplicate": {"repeat-worker": "conditional_detail_already_attempted",
                  "repeat-main": "conditional_detail_already_attempted"},
    "bypass": {**{name: "conditional_tool_out_of_scope" for name in BYPASSES},
               **{f"worker-{name}": "unauthorized_tool" for name in BYPASSES}},
    "ambiguous": {},
    "status": {},
}


def poison(plans):
    return "\n".join(f"编号 #{row['id']}｜名称 {row['plan_name']}｜状态 {row['status']}｜日预算 {row['daily_budget']}"
                     for row in plans)


class FixedModel(t4.FixedModel):
    model = "task27-r2-fixed"
    observed_extra_tools = tuple(name for name in BYPASSES if name != "query_data")

    def prepare(self, case, mode, capture, audit):
        super().prepare(case, mode, capture, audit)
        self.edge = None

    def observe(self, name, original):
        @wraps(original)
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if name == RULE and self.edge in {"missing-rule", "rule-conflict"}:
                result = json.loads(result) if isinstance(result, str) else result
                hit, = result["evidence"]
                result["evidence"] = [] if self.edge == "missing-rule" else [
                    hit, {**hit, "content": hit["content"].replace("20%", "2%")},
                ]
            if name == METRICS and kwargs["plan_id"] == 102 and kwargs["start_date"] == WINDOWS[1][0]:
                if self.edge == "source-version":
                    return {**result, "data_version": "conflicting-version"}
                if self.edge == "wrong-owner":
                    return {**result, "user_id": OWNER + 1}
            return result

        return super().observe(name, read)

    def main(self, returned):
        from backend.agent.tests.test_home_delegation import reply

        if self.edge:
            return self.boundary_main(returned)
        response = super().main(returned)
        message = response.choices[0].message
        if not message.tool_calls:
            return reply(message.content + "\n" + self.poison + "\n" + FABRICATED)
        if any(call.function.name in {PLAN, "delegate_tasks"} for call in message.tool_calls):
            return reply(self.poison + "\n" + FABRICATED, *message.tool_calls)
        return response

    def worker(self, context, returned):
        from backend.agent.tests.test_home_delegation import reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call

        if self.edge == "duplicate":
            if not returned:
                return reply(self.poison, tool_call("first-worker", name=PLAN, plan_id=101),
                             tool_call("repeat-worker", name=PLAN, plan_id=101))
            return worker_summary(self.poison)
        if self.edge == "bypass":
            if not returned:
                return reply(self.poison, *(tool_call(f"worker-{name}", name=name, **args)
                                            for name, args in BYPASSES.items()))
            return worker_summary(self.poison)
        response = super().worker(context, returned)
        message = response.choices[0].message
        if not message.tool_calls:
            summary = json.loads(message.content)
            return worker_summary(self.poison, status=summary["status"])
        return reply(self.poison, *message.tool_calls) if context["task"]["task_id"] == self.detail_id else response

    def boundary_main(self, returned):
        from backend.agent.tests.test_home_delegation import dispatch, reply
        from backend.agent.tests.test_home_sse_tracing import tool_call
        from backend.agent.tests.test_team_runtime import task

        poisoned_task = {**task("detail-active"), "objective": self.poison}
        if self.edge == "status":
            if not returned:
                return reply("", tool_call("ordinary-status", name=PLAN, plan_id=101))
            result = returned["ordinary-status"]["result"]
            return reply(poison([result]))
        assert self.edge != "ambiguous", "clarification called the model"
        if not returned:
            return reply("", tool_call("alerts", name="get_alerts", unread_only=True))
        if self.edge == "bypass":
            if "bypass-worker" not in returned:
                return reply(self.poison, *(tool_call(name, name=name, **args) for name, args in BYPASSES.items()),
                             dispatch("bypass-worker", poisoned_task))
            return reply(self.poison)
        if "rule" not in returned:
            calls = []
            for plan in (101, 102):
                for index, (start, end) in enumerate(WINDOWS):
                    call_id = f"metric-{plan}-{index}"
                    if self.edge == "wrong-window" and (plan, index) == (102, 1):
                        call_id, start = "metric-wrong-window", "2026-10-25"
                    calls.append(tool_call(call_id, name=METRICS, plan_id=plan, start_date=start, end_date=end))
            query = "点击率下降复核 v2" if self.edge == "wrong-version" else RULE_QUERY
            return reply("", *calls, tool_call("rule", name=RULE, query=query))
        if self.edge == "duplicate":
            if "duplicate-worker" not in returned:
                return reply(self.poison, dispatch("duplicate-worker", poisoned_task))
            if "repeat-main" not in returned:
                return reply(self.poison, tool_call("repeat-main", name=PLAN, plan_id=101))
        elif "detail" not in returned:
            return reply(self.poison, tool_call("detail", name=PLAN, plan_id=101))
        return reply(self.poison)


def fields_disclosed(text, plan):
    plan_id = plan["id"]
    budget = re.escape(str(plan["daily_budget"]))
    return {
        # The plan identifier in window facts is expected. Check its use as a detail/list field.
        "id": bool(re.search(rf"(?:编号\s*#{plan_id}(?!\d)|^\s*-\s*#{plan_id}[，,])", text, re.M)),
        "plan_name": plan["plan_name"] in text,
        "status": bool(re.search(rf"(?<![a-zA-Z0-9_-]){re.escape(plan['status'])}(?![a-zA-Z0-9_-])", text)),
        "daily_budget": bool(re.search(rf"(?<![\d.]){budget}(?![\d.])", text)),
    }


def verify_disclosure(sample, model, sql, allowed):
    plans = sample["independent_sql"]["plans"]
    assistant = [row["content"] for row in sample["sse"] if row["type"] == "text"][0]
    history = [row for row in sample["history"]["entries"] if row["kind"] != "user"]
    committed = sql("SELECT kind, content, details FROM chat_group_entries WHERE run_id=? AND kind<>'user' ORDER BY id",
                    (sample["run_id"],), rows=True)
    public = {
        "sse": "\n".join(row.get("content", "") for row in sample["sse"] if isinstance(row.get("content"), str))
               + "\n" + json.dumps(sample["sse"], ensure_ascii=False),
        "history": "\n".join(row["content"] for row in history) + "\n" + json.dumps(history, ensure_ascii=False),
        "committed": "\n".join(row["content"] for row in committed) + "\n"
                     + json.dumps(committed, ensure_ascii=False),
    }
    proposed = "\n".join(row["response"]["content"] or "" for row in model.calls
                         if row["role"] == "orchestrator")
    disclosures = {}
    for plan in plans:
        plan_id = plan["id"]
        seen = {where: fields_disclosed(text, plan) for where, text in public.items()}
        proposed_fields = fields_disclosed(proposed, plan)
        if model.edge not in {"ambiguous", "status"}:
            assert all(proposed_fields.values()), (plan, proposed)
        assert all(value == (plan_id in allowed) for fields in seen.values() for value in fields.values()), seen
        if plan_id in allowed:
            assert all(fields_disclosed(assistant, plan).values())
        disclosures[plan_id] = {"model_proposed": proposed_fields, **seen}
    if model.edge != "status":
        assert all(probe not in text for probe in ("R2_MODEL_ONLY_NAME", "r2_fake_status", "987654.32")
                   for text in public.values())
        # A task's ID and objective are model text. Neither may be published as progress.
        assert all("task_id" not in row["event"] for row in history if row["kind"] == "worker")
        assert all(not any(fields_disclosed(row["content"], plan).values())
                   for row in history if row["kind"] == "worker" for plan in plans)
    sample["field_disclosures"] = disclosures


def verify(sample, model, sql):
    t4.verify(sample, model, sql, repaired=True)
    attempts = sample["attempts"]
    allowed = [row for row in attempts if row["expected_allowed"]]
    blocked = [row for row in attempts if not row["expected_allowed"]]
    assert sample["detail_targets"] == [row["plan_id"] for row in allowed]
    assert sample["admission_violations"] == 0
    verify_disclosure(sample, model, sql, sample["detail_targets"])
    steps = sample["trace"]["steps"]
    assert sum(step["stage"] == "conditional_detail" and step["action"] == "frozen" for step in steps) == 1
    for row in attempts:
        step, = [step for step in steps if step["stage"] == "conditional_detail" and step.get("tool_call_id") == row["id"]]
        assert step["role"] == row["role"] and step["evidence"]["requested_plan_id"] == row["plan_id"]
        receipt = row["tool_receipt"]
        assert step["runtime_call_id"] == receipt["runtime_call_id"]
        if row["expected_allowed"]:
            assert (step["action"], row["leaf_reads"], row["accepted_receipts"]) == ("allowed", 1, 1)
            assert set(step["evidence"]["evidence_artifact_ids"]) == {row["artifact_id"] for row in sample["upstream_artifacts"]}
            assert step["evidence"]["target_plan_ids"] == [row["plan_id"]]
            assert all(receipt["result"][field] == next(plan[field] for plan in sample["independent_sql"]["plans"]
                                                       if plan["id"] == row["plan_id"]) for field in DETAIL_FIELDS)
            assert any(item["stage"] == "receipt" and item["action"] == "accepted"
                       and item.get("runtime_call_id") == receipt["runtime_call_id"]
                       and item["evidence"]["artifact_id"] == receipt["artifact_id"] for item in steps)
            result_step, = [item for item in steps if item["stage"] == "tool_result"
                            and item.get("runtime_call_id") == receipt["runtime_call_id"]]
            assert result_step["evidence"]["arguments"] == {"plan_id": row["plan_id"], "user_id": OWNER}
            assert result_step["evidence"]["result"] == {field: receipt["result"][field] for field in DETAIL_FIELDS}
        else:
            expected = {"branch-1": "review_metrics_pending", "swap-1": "review_wrong_target",
                        "gap-1": "plan_history_not_mapped"}[sample["case"]]
            assert step["action"] == "blocked" and step["error_code"] == expected
            assert row["leaf_reads"] == row["accepted_receipts"] == 0 and not receipt["success"]
            assert receipt["error_code"] == expected and "artifact_id" not in receipt and "result" not in receipt
    assert "严重 CTR 告警 1 条" in sample["answer"]
    assert sample["independent_sql"]["rule"]["chunk_id"] in sample["answer"]
    if not allowed:
        assert "暂缓" in sample["answer"]
    if sample["case"] == "gap-1" and sample["mode"] == "handoff":
        assert not any(row["task"]["task_id"] == t4.DOWNSTREAM for row in model.audit.work)
    sample.update(blocked_attempts=len(blocked), allowed_attempts=len(allowed))


def verify_boundary(sample, model, sql):
    answer, _, steps, reclaimed, _ = t4.verify_lifecycle(sample, model, sql)
    edge = model.edge
    expected = BOUNDARIES[edge]
    tool_inputs = {**sample["direct_results"],
                   **{call_id: result for row in model.calls for call_id, result in receipts(row["messages"]).items()}}
    attempts = {call["id"]: call for row in model.calls for call in row["response"]["tool_calls"]}
    for call_id, code in expected.items():
        assert call_id in attempts and tool_inputs[call_id]["error_code"] == code, (call_id, tool_inputs)
        assert not tool_inputs[call_id]["success"] and "result" not in tool_inputs[call_id]
        assert any(row.get("tool_call_id") == call_id and row.get("error_code") == code for row in steps)
    calls = Counter(row["tool"] for row in model.audit.reads)
    allowed = [101] if edge in {"duplicate", "status"} else []
    leaves = [row for row in model.audit.reads if row["tool"] == "query_plan_detail"]
    assert [(row["arguments"]["plan_id"], row["arguments"]["user_id"]) for row in leaves] == [
        (plan, OWNER) for plan in allowed]
    assert calls[PLAN] == len(leaves) and not calls.keys() & BYPASSES.keys()
    for call_id, row in attempts.items():
        if row["name"] == PLAN:
            passed = call_id in {"ordinary-status", "first-worker"}
            assert tool_inputs[call_id]["success"] == passed
            assert ("artifact_id" in tool_inputs[call_id]) == passed
    if edge in {"bypass", "ambiguous", "status"}:
        assert not calls[METRICS] and not calls[RULE]
    else:
        assert calls[METRICS] == (3 if edge == "wrong-window" else 4)
        assert calls[RULE] == (0 if edge == "wrong-version" else 1)
    if edge == "ambiguous":
        assert not calls and not model.calls and "请明确" in answer
    elif not allowed:
        assert "暂缓" in answer
    if edge == "duplicate":
        assert len(reclaimed) == 1
        assert {row["action"] for row in steps if row["stage"] == "conditional_detail"
                and row.get("tool_call_id") in {"first-worker", "repeat-worker", "repeat-main"}} == {"allowed", "blocked"}
    verify_disclosure(sample, model, sql, allowed)
    sample.update(status="passed", answer=answer, detail_targets=allowed, expected_denials=expected,
                  observed_tools=dict(calls), worker_reclaimed=len(reclaimed))


def run_boundaries(directory, model, sql, client, frozen, captures, report):
    case = next(row for row in frozen["cases"] if row["id"] == "branch-1")
    expected = t4.sql_oracle(sql, frozen, case)
    report["boundaries"] = []
    for edge in BOUNDARIES:
        message = case["message"]
        if edge == "ambiguous":
            message = message.replace("》 v1", "》")
        elif edge == "status":
            message = "请查询计划 #101 的名称、当前状态和日预算。"
        name = "boundary-" + edge
        sample = {"id": name, "status": "failed", "independent_sql": expected}
        audit = t4.Audit(directory / "sdk.sqlite3", None, name, 0)
        audit.case = name
        model.prepare({**case, "message": message}, "main", captures[case["id"]], audit)
        model.edge, model.poison = edge, poison(expected["plans"])
        t4.run_sample(directory, model, sql, sample, client, verify_boundary)
        assert not audit.rows()
        report["boundaries"].append({key: sample[key] for key in (
            "id", "status", "trace_id", "run_id", "detail_targets", "expected_denials", "observed_tools", "worker_reclaimed",
        )})
        print(f"{name}: {sample['trace_id']} {sample['detail_targets']}", flush=True)


def summarize(report):
    rounds = report["rounds"]
    report["blocked_original_actions"] = sum(row["blocked_attempts"] for row in rounds)
    report["allowed_original_actions"] = sum(row["allowed_attempts"] for row in rounds)
    if len(rounds) == 10:
        assert report["blocked_original_actions"] == 12 and report["allowed_original_actions"] == 4
        assert len(report["boundaries"]) == len(BOUNDARIES)


if __name__ == "__main__":
    t4.main(sys.modules[__name__])
