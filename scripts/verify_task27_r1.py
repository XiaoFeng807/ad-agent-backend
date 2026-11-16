"""Replay AD-040 against live group HTTP with fixed models and isolated databases."""

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import date, timedelta
from fractions import Fraction
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from scripts.verify_task27 import ROOT, dump, fixture
from scripts.verify_task27_t1 import FixedModel as TeamFixtureModel, METRICS, OWNER, PLAN, receipts, server


ARCHIVE = ROOT / "logs/task27-t2"
PUBLIC = ROOT / "data/plan_metrics/criteo.sqlite3"
GROUPS = "/api/chat/conversations"
ALTERNATIVES = {
    "compare_plans": {"plan_id_1": 101, "plan_id_2": 102},
    "get_plans_summary": {}, "get_activity_timeline": {}, "get_dashboard_data": {},
    "get_account_detail": {"account_id": 1}, "query_data": {"question": "查 #101 状态"},
}
UPSTREAM, DOWNSTREAM = "metrics", "detail"
LEAK_TASK_ID = "active-310"
PRIMARY = ("missing-1", "complex-1", "changed-1")
SYNTHETIC = {"zero": ("high", "zero"), "no-match": ("low", "low"),
             "both-match": ("high", "exact"), "threshold": ("exact", "low"),
             "missing-day": ("high", "partial")}
BLOCKS = {
    "missing-1": "conditional_missing_metrics", "early": "conditional_metrics_pending",
    "zero": "conditional_zero_impressions", "no-match": "conditional_no_match",
    "both-match": "conditional_multiple_matches", "missing-day": "conditional_missing_metrics",
    "wrong-window": "conditional_metrics_pending", "wrong-owner-receipt": "conditional_missing_metrics",
    "wrong-version": "conditional_missing_metrics", "cross-group": "conditional_metrics_pending",
}


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


class FixedModel(TeamFixtureModel):
    def __init__(self, frozen, repro):
        super().__init__()
        self.frozen, self.repro = frozen, repro
        self.role, self.sample = None, None
        self.foreign_artifacts = []
        self.attempts, self.worker_inputs = [], []

    def create(self, messages, tools=None, response_format=None, **options):
        from backend.agent.tests.test_home_delegation import dispatch, reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call
        from backend.agent.tests.test_team_runtime import task

        current = next(row["content"] for row in reversed(messages) if row["role"] == "user")
        returned = receipts(messages)
        phase = "worker" if response_format else "main" if tools else "planner"
        self.calls.append({"sample": self.sample, "phase": phase, "tools": tools, "messages": deepcopy(messages)})
        if phase == "planner":
            return reply('{"steps":[]}')

        def metrics():
            return [tool_call(f"metric-{plan_id}", name=METRICS, plan_id=plan_id, **(
                {**self.frozen["window"], "end_date": "2026-10-24"}
                if plan_id == 102 and self.case == "wrong-window" else self.frozen["window"]))
                    for plan_id in (101, 102)]

        def detail(plan_id, *, call_id=None):
            if call_id is None:
                call_id = (self.repro["first_divergence"]["response_calls"][0]["id"]
                           if self.case == "missing-1" else f"detail-{plan_id}")
            self.attempts.append({"sample": self.sample, "phase": phase, "call_id": call_id, "plan_id": plan_id})
            return tool_call(call_id, name=PLAN, plan_id=plan_id)

        def select(metrics_rows):
            qualifying = [row["plan_id"] for row in metrics_rows
                          if row["impressions"] and Fraction(row["clicks"], row["impressions"]) >= Fraction(1, 5)]
            return qualifying[0] if len(qualifying) == 1 else 101

        def check_missing():
            for original in self.repro["first_evidence"]:
                archived = original["receipt"]
                current_receipt = returned[f"metric-{archived['arguments']['plan_id']}"]
                assert all(current_receipt[key] == value for key, value in archived.items()
                           if key not in {"runtime_call_id", "artifact_id"}), current_receipt

        def alternative_reads():
            return [tool_call(name, name=name, **args) for name, args in ALTERNATIVES.items()]

        leak = self.repro["final_reply"]
        if phase == "worker":
            context = json.loads(current)
            worker_task = context["task"]["task_id"]
            if worker_task in {DOWNSTREAM, LEAK_TASK_ID} and not returned:
                supplied = context["inputs"].get(UPSTREAM, [])
                self.worker_inputs.append({"sample": self.sample, "task_id": worker_task, "inputs": supplied})
                return reply("", detail(select([row["result"] for row in supplied if row["source"] == METRICS])))
            if not returned:
                return reply("", *metrics(), *([detail(101)] if self.case == "early" else []))
            if worker_task == UPSTREAM and "detail-101" not in returned and self.case not in {
                "early", "complex-1", "changed-1", "threshold",
            }:
                attempt_id = self.repro["first_divergence"]["response_calls"][0]["id"]
                if attempt_id not in returned:
                    if self.case == "missing-1":
                        check_missing()
                    return reply("", detail(101), *(alternative_reads() if self.case == "missing-1" else []))
            return worker_summary(leak, status="missing" if any(not row["success"] for row in returned.values())
                                  else "succeeded")

        if self.case == "simple-1":
            if not returned:
                return reply("", detail(101))
            return reply("计划 #101 当前状态为 " + returned["detail-101"]["result"]["status"] + "。")
        if self.case == "no-tools":
            return reply(leak)
        if self.case == "cross-group":
            if not returned:
                assert len(self.foreign_artifacts) == 2
                foreign = {**task(DOWNSTREAM), "direct_refs": self.foreign_artifacts}
                return reply("", dispatch("foreign", foreign))
            if "local" not in returned:
                assert returned["foreign"]["error_code"] == "invalid_input_reference"
                return reply("", dispatch("local", task(LEAK_TASK_ID)))
            return reply(leak)
        if self.role == "worker":
            if not returned:
                capabilities = (METRICS, PLAN) if self.case not in {"complex-1", "changed-1", "threshold"} else (METRICS,)
                sheet = {**task(UPSTREAM, tools=capabilities), "objective": leak[:1000]}
                return reply("", dispatch("first", sheet))
            if self.case in {"complex-1", "changed-1", "threshold"} and "second" not in returned:
                refs = [{"task_id": UPSTREAM, "artifact_id": row["artifact_id"]}
                        for row in returned["first"]["evidence"] if row["source"] == METRICS]
                sheet = task(DOWNSTREAM, deps=(UPSTREAM,), artifacts=refs)
                return reply("", dispatch("second", sheet))
            return reply(leak)
        if not returned:
            extra = [detail(101)] if self.case == "early" else []
            return reply("", *metrics(), *extra)
        if len(returned) == 2:
            if self.case == "missing-1":
                check_missing()
            if self.case == "changed-1":
                self.foreign_artifacts = [row["artifact_id"] for row in returned.values()]
            target = select([row["result"] for row in returned.values() if row["success"]])
            if self.case == "wrong-target":
                return reply("", detail(102), detail(101), detail(101, call_id="duplicate-101"))
            return reply("", detail(target), *(alternative_reads() if self.case == "missing-1" else []))
        return reply(leak)

    def observe(self, name, original):
        def read(**args):
            observation = {"sample": self.sample, "tool": name, "arguments": args}
            self.reads.append(observation)
            result = original(**args)
            observation["result"] = deepcopy(result)
            if self.case == "wrong-owner-receipt" and name == METRICS and args["plan_id"] == 102:
                return {**result, "user_id": 18}
            return result
        return read


def synthetic(path, frozen):
    source = {**frozen["source"], "source_id": "task27-r1-synthetic", "data_version": "fixture-v1",
              "kind": "synthetic"}
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE metadata (manifest TEXT)")
        conn.execute("INSERT INTO metadata VALUES (?)", (json.dumps(source),))
        conn.execute("CREATE TABLE plan_daily_metrics (source_plan_id TEXT, metric_date TEXT, impressions INT, clicks INT)")
        start = date.fromisoformat(frozen["window"]["start_date"])
        for key, impressions, clicks in (("high", 100, 21), ("exact", 100, 20), ("low", 100, 19),
                                          ("zero", 0, 0), ("partial", 100, 19)):
            conn.executemany("INSERT INTO plan_daily_metrics VALUES (?, ?, ?, ?)",
                             [(key, (start + timedelta(days=i)).isoformat(), impressions, clicks)
                              for i in range(6 if key == "partial" else 7)])
    return source


def configure_case(case, frozen, samples, local_path, local_source, sql):
    from backend.config.config import settings

    original = samples.get(case, samples["complex-1"])
    request = original["message"]
    source, path, bindings = frozen["source"], PUBLIC, original["bindings"]
    if case in SYNTHETIC:
        path, source = local_path, local_source
        bindings = dict(zip(("101", "102"), SYNTHETIC[case]))
    if case == "ambiguous":
        request = request.replace("2026-10-19 至 2026-10-25", "最近一周")
    settings.PLAN_METRICS_DB_PATH = str(path)
    sql("DELETE FROM plan_metric_bindings")
    for plan_id, source_plan_id in bindings.items():
        if source_plan_id:
            version = "incorrect-version" if case == "wrong-version" and plan_id == "102" else source["data_version"]
            sql("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, ?)",
                (int(plan_id), source["source_id"], version, source_plan_id))
    plans = sql(frozen["plan_sql"], (OWNER,), rows=True)
    assert plans == frozen["plan_rows"], plans
    metrics = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        for plan_id, source_plan_id in bindings.items():
            if source_plan_id:
                parameters = (source_plan_id, *frozen["window"].values())
                metrics[plan_id] = {**dict(conn.execute(frozen["metric_sql"], parameters).fetchone()),
                                    "parameters": parameters}
    if case in PRIMARY:
        for plan_id, row in metrics.items():
            assert all(value == original["expected"]["metrics"][plan_id][key]
                       for key, value in row.items() if key not in {"parameters", "ctr_pct"})
    return request, {"plans": plans, "metrics": metrics, "bindings": sql(
        "SELECT * FROM plan_metric_bindings ORDER BY plan_id", rows=True)}


def group_round(client, case, message, sample):
    group = client.post(GROUPS, json={"title": "27-R1 " + case})
    assert group.status_code == 201, group.text
    group_id = group.json()["data"]["conversation_id"]
    sample.update(request={"message": message, "conversation_id": group_id}, sse=[])
    with client.stream("POST", "/api/chat/stream", json=sample["request"]) as response:
        assert response.status_code == 200, response.read()
        for line in response.iter_lines():
            if line.startswith("data: "):
                sample["sse"].append(json.loads(line[6:]))
    history = client.get(f"{GROUPS}/{group_id}/history")
    history.raise_for_status()
    sample["history"] = history.json()["data"]
    sample["trace_id"] = sample["sse"][-1]["trace_id"]
    sample["run_id"] = sample["sse"][-1]["run_id"]


def verify(sample, model, sql, steps, leaves):
    case, role = sample["case"], sample["role"]
    selected = {"complex-1": 101, "changed-1": 102, "threshold": 101, "simple-1": 101,
                "wrong-target": 101}.get(case)
    terminal, = [event for event in sample["sse"] if event["type"] in {"done", "error"}]
    assert terminal["type"] == "done" and terminal == sample["sse"][-1], sample["sse"]
    answer, = [event["content"] for event in sample["sse"] if event["type"] == "text"]
    history = sample["history"]
    committed, = [row["content"] for row in history["entries"] if row["kind"] == "assistant"]
    persisted, = sql("SELECT content FROM chat_group_entries WHERE run_id=? AND kind='assistant'",
                     (sample["run_id"],), rows=True)
    assert answer == committed == persisted["content"]
    assert history["runs"] == [{"run_id": sample["run_id"], "trace_id": sample["trace_id"], "status": "completed"}]

    reads = [row for row in model.reads if row["sample"] == model.sample]
    details = [row for row in reads if row["tool"] == PLAN]
    assert [row["arguments"] for row in details] == ([{"plan_id": selected, "user_id": OWNER}] if selected else [])
    assert [row for row in leaves if row["sample"] == model.sample] == [
        {"sample": model.sample, **row["arguments"]} for row in details]
    assert all(row["tool"] in {PLAN, METRICS} for row in reads), reads
    conditional = [step for step in steps if step["stage"] == "conditional_detail"]
    admissions = [step for step in conditional if step["action"] in {"allowed", "blocked"}
                  and "requested_plan_id" in step["evidence"]]
    assert sum(step["evidence"]["executed"] for step in admissions) == int(selected is not None and case != "simple-1")
    detail_runtime_ids = {step["runtime_call_id"] for step in steps if step["stage"] == "tool_result"
                          and step["action"] == PLAN}
    detail_receipts = [step for step in steps if step["stage"] == "receipt" and step["action"] == "accepted"
                       and step.get("runtime_call_id") in detail_runtime_ids]
    assert len(detail_receipts) == int(selected is not None), detail_receipts
    if case in BLOCKS:
        attempted = [step for step in admissions if step["role"] == ("team_worker" if role == "worker" else "orchestrator")
                     or case == "cross-group" and step["role"] == "team_worker"]
        assert len(attempted) == 1 and attempted[0]["error_code"] == BLOCKS[case], admissions
        if case == "missing-1":
            assert attempted[0]["evidence"]["requested_plan_id"] == 101
            denied = {step["tool_call_id"] for step in steps
                      if step.get("error_code") in {"conditional_tool_out_of_scope", "unauthorized_tool"}}
            assert denied == ALTERNATIVES.keys(), steps
    if case == "wrong-target":
        assert [step.get("error_code") for step in admissions] == [
            "conditional_wrong_target", None, "conditional_detail_already_attempted"]
    if case == "ambiguous":
        assert conditional[0]["action"] == "clarification_required" and not reads
    if case == "simple-1":
        assert not conditional and "active" in answer and len(reads) == 1
    elif case != "ambiguous":
        final, = [step["evidence"] for step in conditional if step["action"] == "reply"]
        assert bool(final["detail_artifact_id"]) == bool(selected)
        for row in final["rows"]:
            if row["ctr_pct"] is not None:
                sql_ctr = sample["independent_sql"]["metrics"][str(row["plan_id"])]["ctr_pct"]
                assert abs(row["ctr_pct"] - sql_ctr) < 1e-10 and f"{sql_ctr:.2f}%" in answer
        if selected:
            assert final["target_plan_id"] == selected
            plan, = [row for row in sample["independent_sql"]["plans"] if row["id"] == selected]
            assert all(details[0]["result"][key] == value for key, value in plan.items())
            assert all(str(plan[key]) in answer for key in ("plan_name", "status", "daily_budget"))
            if role == "worker":
                received, = [row["inputs"] for row in model.worker_inputs if row["sample"] == model.sample]
                allowed, = [step for step in admissions if step["action"] == "allowed"]
                assert {row["artifact_id"] for row in received if row["source"] == METRICS} == set(
                    allowed["evidence"]["evidence_artifact_ids"])
        else:
            group_data = json.dumps([sample["sse"], history], ensure_ascii=False)
            assert all(value not in group_data for value in ("北岸搜索", "南桥搜索", "active", "paused", "310.0", "720.0")), answer
            if case == "missing-1":
                assert all(value in answer for value in ("25.55%", "#102", "未绑定历史来源", "暂缓"))
                assert len(reads) == 2 and [row["arguments"]["plan_id"] for row in reads] == [101, 102]
    sample.update(status="passed", read_counts={name: sum(row["tool"] == name for row in reads)
                                                  for name in (METRICS, PLAN)},
                  admissions=admissions, detail_receipts=len(detail_receipts),
                  answer=answer, committed_answer=persisted["content"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mysqld", type=Path)
    parser.add_argument("--case", action="append", help="Select a case. Cross-group also requires changed-1.")
    args = parser.parse_args()
    assert __debug__ and (not args.mysqld or args.mysqld.is_file())
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH="", API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="",
                      JWT_SECRET=token_urlsafe(48), ANONYMIZED_TELEMETRY="False", LLM_PROVIDER="mock")
    from backend.database.database import DEFAULT_DB_PATH
    from backend.tools import tools

    frozen_file = ARCHIVE / "matrix/frozen.json"
    frozen = json.loads(frozen_file.read_text(encoding="utf-8"))
    frozen["window"] = dict(zip(("start_date", "end_date"), frozen["window"]))
    repro = json.loads((ARCHIVE / "missing-1-repro.json").read_text(encoding="utf-8"))
    assert digest(frozen_file) == "d1379a26d08ceef042ecfcf965c4aae8baf3cf63d265f51b8fadb537d167a3b7"
    assert digest(PUBLIC) == frozen["artifact_sha256"]
    originals = {row["id"]: row for row in frozen["cases"]}
    assert repro["case"] == originals["missing-1"]
    assert all(sha256(row["message"].encode()).hexdigest() == row["message_sha256"] for row in originals.values())
    before = {str(path): digest(path) for path in (DEFAULT_DB_PATH, PUBLIC, frozen_file)}
    password = token_urlsafe(24)
    model = FixedModel(frozen, repro)
    report = {"database": "MySQL 8.4" if args.mysqld else "SQLite", "status": "failed",
              "real_model_requests": 0, "rounds": [], "metric_sql": frozen["metric_sql"], "plan_sql": frozen["plan_sql"]}
    leaves = []
    leaf_read = tools.query_plan_detail

    def observe_leaf(plan_id, user_id, connect):
        leaves.append({"sample": model.sample, "plan_id": plan_id, "user_id": user_id})
        return leaf_read(plan_id, user_id, connect)

    alternatives = {name: model.observe(name, tools.function_map[name])
                    for name in ALTERNATIVES if name in tools.function_map}
    try:
        with (fixture(directory, args.mysqld, password) as sql,
              TemporaryDirectory(dir=directory, prefix="edge-metrics-") as scratch,
              patch.object(tools, "query_plan_detail", observe_leaf),
              patch.dict(tools.function_map, alternatives),
              server(directory, model, SimpleNamespace(path=PUBLIC)) as url,
              httpx.Client(base_url=url, timeout=45, trust_env=False) as client):
            local_path = Path(scratch) / "edge.sqlite3"
            local_source = synthetic(local_path, frozen)
            for plan in frozen["plan_rows"]:
                sql("UPDATE ad_plans SET plan_name=?, status=?, daily_budget=? WHERE id=? AND user_id=?",
                    (plan["plan_name"], plan["status"], plan["daily_budget"], plan["id"], OWNER))
            captcha = client.get("/api/captcha").json()
            login = client.post("/api/login", json={"username": "task27-owner", "password": password,
                                                    "captcha_id": captcha["captcha_id"], "captcha_text": "0726"}).json()
            assert login["code"] == 200
            client.headers["Authorization"] = "Bearer " + login["data"]["token"]
            cases = [(case, role) for case in (*PRIMARY, "early", *SYNTHETIC,
                                               "wrong-window", "wrong-owner-receipt", "wrong-version")
                     for role in ("main", "worker")]
            cases.extend((case, "main") for case in ("cross-group", "wrong-target", "ambiguous", "no-tools", "simple-1"))
            for case, role in cases:
                if args.case and case not in args.case:
                    continue
                model.case, model.role, model.sample = case, role, f"{case}/{role}"
                message, expected = configure_case(case, frozen, originals, local_path, local_source, sql)
                sample = {"case": case, "role": role, "independent_sql": expected, "status": "failed"}
                report["rounds"].append(sample)
                group_round(client, case, message, sample)
                traces = [json.loads(line) for line in (directory / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
                trace, = [row for row in traces if row["trace_id"] == sample["trace_id"]]
                verify(sample, model, sql, trace["steps"], leaves)
                dump(directory / (model.sample.replace("/", "-") + ".json"), sample)
                print(f"{model.sample}: {sample['trace_id']} {sample['read_counts']}", flush=True)
            assert report["rounds"]
        report["status"] = "passed"
    except Exception as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["file_sha256_before"], report["file_sha256_after"] = before, {path: digest(Path(path)) for path in before}
        dump(directory / "report.json", report)
        dump(directory / "model-inputs.json", model.calls)
        dump(directory / "model-detail-attempts.json", model.attempts)
        dump(directory / "worker-inputs.json", model.worker_inputs)
        dump(directory / "tool-calls.json", model.reads)
        dump(directory / "detail-leaf-calls.json", leaves)
        assert report["file_sha256_before"] == report["file_sha256_after"]


if __name__ == "__main__":
    main()
