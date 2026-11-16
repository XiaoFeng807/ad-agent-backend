"""Fixed work-group replay of T6 task sheets and independent review handoffs."""

import argparse
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from scripts import run_task27_t6 as t6, verify_task27_r3 as r3, verify_task27_t4 as t4
from scripts.run_task27_r5 import ARCHIVE as R5_ARCHIVE, frozen_fixture
from scripts.run_task27_t3 import Audit
from scripts.task27_t3_fixture import ARTIFACT, OWNER, ROOT, digest
from scripts.verify_task27 import dump, fixture
from scripts.verify_task27_r2 import fields_disclosed, poison
from scripts.verify_task27_t1 import METRICS, PLAN, server


SDK = t6.ARCHIVE / "recovery/sdk.json"
MODES = {
    "sdk-10": ("branch-1", 10), "sdk-22": ("swap-1", 22),
    "same-batch": ("branch-1", 10), "append": ("swap-1", 22),
    "missing-binding": ("gap-1", 22), "missing-report": ("gap-2", 22),
    "wrong-target": ("branch-1", 10), "missing-refs": ("branch-1", 10),
    "foreign-group": ("branch-1", 10), "foreign-run": ("swap-1", 22),
    "budget": ("branch-1", 10),
    "direct-rule": ("branch-1", 10),
    "status": ("simple-1", None),
}
R6_REPLAYS = {"r6-sdk-2": ("review-branch-1", 2), "r6-sdk-7": ("review-swap-1", 7)}


class FixedModel(t4.FixedModel):
    def prepare_round(self, case, mode, audit, expected, archived, foreign=None, invalid=None):
        self.case, self.mode, self.audit, self.expected = case, mode, audit, expected
        self.calls, self.errors, self.foreign = [], [], foreign
        self.delegate = t4.original_call(archived["tool_calls"][0]) if archived else None
        self.upstream = json.loads(self.delegate.function.arguments)["tasks"] if archived else []
        self.detail_id = t4.DOWNSTREAM
        self.invalid = t4.original_call(invalid["tool_calls"][0]) if invalid else None

    def handoff_sheet(self):
        task = super().handoff_sheet()
        del task["input_refs"]
        return task

    def appendix(self, evidence):
        refs = [{key: row[key] for key in ("task_id", "artifact_id")} for row in evidence]
        if self.foreign:
            ref = next(row for row in refs if row["task_id"] == self.foreign["task_id"])
            ref["artifact_id"] = self.foreign["artifact_id"]
        check = self.handoff_sheet()
        del check["dependencies"]
        return {**check, "artifact_refs": refs}

    def record_receipt(self, record):
        return self.audit.wrap_receipt(record)

    def worker(self, context, returned):
        from backend.agent.tests.test_home_delegation import reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call

        if context["task"]["task_id"] == self.detail_id:
            if returned:
                return worker_summary("复核动作结束")
            selected = t4.selected_from_inputs(context["inputs"])
            plan = 203 - selected if self.mode == "wrong-target" else selected
            return reply("", tool_call("review-detail", name=PLAN, plan_id=plan))
        if returned:
            return worker_summary("取证结束", status="missing" if any(not r["success"] for r in returned.values())
                                  else "succeeded")
        task = context["task"]
        if t4.RULE in task["capabilities"]:
            return reply("", tool_call("exact-rule", name=t4.RULE, query=t4.RULE_QUERY))
        plans = [int(task["task_id"][-3:])] if task["task_id"].startswith("evidence-") else [101, 102]
        return reply("", *(tool_call(f"metric-{plan}-{w['start_date']}", name=METRICS, plan_id=plan,
                                     start_date=w["start_date"], end_date=w["end_date"])
                           for plan in plans for w in self.expected["metrics"][str(plan)]["windows"]))

    def main(self, returned):
        from backend.agent.tests.test_home_delegation import dispatch, reply
        from backend.agent.tests.test_home_sse_tracing import tool_call

        fake = poison(self.expected["plans"])
        if self.mode == "status":
            return reply("", tool_call("status", name=PLAN, plan_id=101)) if not returned else reply(
                self.expected["plans"][0]["status"])
        if not returned:
            return reply("", tool_call("alerts", name="get_alerts", unread_only=True))
        if self.invalid:
            if self.invalid.id not in returned:
                return reply("", self.invalid)
            if self.mode == "r6-sdk-2":
                return reply("", dispatch("corrected", *self.upstream, self.handoff_sheet()))
            if "gather" not in returned:
                return reply("", dispatch("gather", *self.upstream))
            check = self.appendix(returned["gather"]["independent_review"]["artifact_refs"])
            if "bad-pair" not in returned:
                invalid = deepcopy(check)
                invalid["artifact_refs"][0]["artifact_id"] = invalid["artifact_refs"][-1]["artifact_id"]
                return reply("", dispatch("bad-pair", invalid))
            return reply("", dispatch("corrected", check))
        if self.mode == "direct-rule":
            if len(returned) == 1:
                return reply("", tool_call("direct-rule", name=t4.RULE, query=t4.RULE_QUERY))
            return reply("", dispatch("gather", *self.upstream, self.handoff_sheet()))
        if len(returned) == 1:
            if self.mode in {"same-batch", "wrong-target", "missing-binding", "missing-report", "missing-refs"}:
                check = self.handoff_sheet()
                if self.mode == "missing-refs":
                    check["dependencies"] = check["dependencies"][:1]
                return reply("", dispatch("gather", *self.upstream, check))
            return reply("", self.delegate)
        if self.delegate.id in returned and len(returned) == 2:
            if self.mode in {"append", "foreign-group", "foreign-run"}:
                refs = returned[self.delegate.id]["independent_review"]["artifact_refs"]
                return reply("", dispatch("review", self.appendix(refs)))
            return reply(fake, tool_call("direct-detail", name=PLAN, plan_id=self.expected["target"]))
        if self.mode == "budget":
            return reply(fake, tool_call(f"blocked-{len(returned)}", name=PLAN, plan_id=self.expected["target"]))
        return reply("独立复核已完成。" + fake)


def verify(sample, model, sql):
    answer, entries, steps, reclaimed, _ = t4.verify_lifecycle(sample, model, sql)
    reads = [row for row in model.audit.reads if row["tool"] == "query_plan_detail"]
    successes = [row for row in model.audit.receipts if row["accepted"] and row["source"] == PLAN
                 and row["status"] == "succeeded"]
    allowed = [row for row in steps if row["stage"] == "conditional_detail" and row["action"] == "allowed"]
    sample.update(answer=answer, detail_reads=reads, detail_receipts=successes, worker_reclaimed=sum(reclaimed.values()))
    if model.mode == "status":
        assert len(reads) == 1 and not reclaimed
    elif sample["baseline"]:
        assert len(reads) == len(allowed) == 1 and reads[0]["role"] == "orchestrator"
    elif model.mode in {"same-batch", "append", "direct-rule", *R6_REPLAYS}:
        read, = reads
        success, = successes
        admission, = allowed
        worker = success["worker_id"]
        first = next(row for row in model.calls if row["worker_id"] == worker)
        inputs = [row for rows in first["worker_input"]["inputs"].values() for row in rows]
        assert len(inputs) == 5
        assert read["arguments"]["plan_id"] == model.expected["target"] and read["arguments"]["user_id"] == OWNER
        assert read["parent_call_id"] == admission["parent_call_id"] == worker
        assert read["runtime_call_id"] == success["tool_call_id"] == admission["runtime_call_id"]
        proof = admission["evidence"]["review_input"]
        assert proof == {"worker_id": worker, "conversation_id": sample["request"]["conversation_id"],
                         "run_id": sample["run_id"], "model_call_id": first["model_call_id"],
                         "action_model_call_id": first["model_call_id"],
                         "input_artifact_ids": [row["artifact_id"] for row in inputs]}
        assert first["response"]["tool_calls"][0]["id"] == read["tool_call_id"] == admission["tool_call_id"]
        assert read["start_ns"] > first["start_ns"]
        for item in inputs:
            producer, = [row for row in model.audit.receipts if row["artifact_id"] == item["artifact_id"]]
            assert producer["worker_id"] != worker and producer["ns"] < first["start_ns"]
            assert producer["task_id"] == item["task_id"] and producer["result"] == item["result"]
        assert "独立复核已完成" in answer
        if model.mode == "direct-rule":
            rule, = [item for item in inputs if item["source"] == t4.RULE]
            assert sample["direct_results"]["direct-rule"]["artifact_id"] != rule["artifact_id"]
        sample["review_input"] = first
        if model.invalid:
            verify_retry(sample, model, steps, inputs)
    else:
        assert not reads and not successes and not allowed
        assert "独立复核未完成" in answer
        public = [json.dumps(row, ensure_ascii=False) for row in (*entries, *sample["sse"])
                  if row.get("kind") != "user"]
        assert all(not any(fields_disclosed(text, plan).values()) for text in public
                   for plan in model.expected["plans"])
        if model.mode.startswith("sdk"):
            result = sample["direct_results"][model.delegate.id]
            sent = next(call for row in model.calls for call in row["response"]["tool_calls"]
                        if call["id"] == model.delegate.id)
            assert sent["raw_arguments"] == model.delegate.function.arguments
            assert result["independent_review"]["status"] == "review_required"
            assert len(result["independent_review"]["artifact_refs"]) == 5
            assert sample["direct_results"]["direct-detail"]["error_code"] == "review_worker_required"
            assert not any(row["step"] == "review_final_detail" for row in model.calls)
        if model.mode.startswith("foreign"):
            assert sample["direct_results"]["review"]["error_code"] == "invalid_input_reference"
        if model.mode == "missing-binding":
            assert "未绑定历史来源" in answer
        if model.mode == "missing-report":
            assert "缺报" in answer
        if model.mode == "budget":
            stop, = [row for row in steps if row["stage"] == "react_decision" and row["action"] == "stop"]
            assert stop["evidence"]["reason"] == "no_progress" and stop["evidence"]["stalled_rounds"] == 2
            assert stop["budget"]["model"] == 7 and len(model.calls) == 9
            assert not any(row["step"] == "review_final_detail" for row in model.calls)
    sample["status"] = "passed"


def verify_retry(sample, model, steps, inputs):
    def invocation(call_id):
        return next(row for row in model.calls if any(
            call["id"] == call_id for call in row["response"]["tool_calls"]))

    def task_schema(call):
        tool, = [tool["function"] for tool in call["tools"] if tool["function"]["name"] == "delegate_tasks"]
        definitions = tool["parameters"]["$defs"]
        return definitions["TeamTask"]["properties"], definitions.get("ArtifactRef")

    invalid_id = model.invalid.id
    invalid = invocation(invalid_id)
    sent, = invalid["response"]["tool_calls"]
    assert sent["raw_arguments"] == model.invalid.function.arguments
    properties, ref_schema = task_schema(invalid)
    assert "dependencies" in properties and ref_schema is None
    assert not properties.keys() & {"direct_refs", "input_refs", "artifact_refs"}
    rejected = sample["direct_results"][invalid_id]
    feedback = rejected["task_sheet_feedback"]
    assert rejected["status"] == "rejected" and feedback["reference_field"] == "dependencies"
    fields = {error["field"] for error in feedback["errors"] if error["reason"]}
    assert {"tasks[0].direct_refs", "tasks[2].direct_refs", "tasks[3].artifact_refs"} <= fields
    reject_index = next(i for i, step in enumerate(steps) if step.get("tool_call_id") == invalid_id
                        and step["stage"] == "tool_result")
    assert not any(step["stage"] in {"batch", "worker"} for step in steps[:reject_index])
    corrected = invocation("corrected")
    assert any(row["role"] == "tool" and row["tool_call_id"] == invalid_id
               and json.loads(row["content"]) == rejected for row in corrected["messages"])
    properties, ref_schema = task_schema(corrected)
    if model.mode == "r6-sdk-2":
        assert "dependencies" in properties and ref_schema is None
    else:
        assert "artifact_refs" in properties and not properties.keys() & {"direct_refs", "input_refs", "dependencies"}
        offered = ref_schema["properties"]
        assert set(offered["artifact_id"]["enum"]) == {row["artifact_id"] for row in inputs}
        assert set(offered["task_id"]["enum"]) == {row["task_id"] for row in inputs}
        rejected_pair = sample["direct_results"]["bad-pair"]
        assert rejected_pair["error_code"] == "invalid_input_reference"
        feedback = rejected_pair["task_sheet_feedback"]
        assert feedback["artifact_refs"] == sample["direct_results"]["gather"]["independent_review"]["artifact_refs"]
        assert feedback["errors"][0]["field"] == "tasks[0].artifact_refs[0]"


def group_round(client, case, message, sample):
    if not (previous := sample.get("previous_group_run")):
        return ORIGINAL_GROUP_ROUND(client, case, message, sample)
    group_id = previous["request"]["conversation_id"]
    sample.update(request={"message": message, "conversation_id": group_id}, sse=[])
    with client.stream("POST", "/api/chat/stream", json=sample["request"]) as response:
        assert response.status_code == 200, response.read()
        for line in response.iter_lines():
            if line.startswith("data: "):
                sample["sse"].append(json.loads(line[6:]))
    terminal = sample["sse"][-1]
    sample.update(trace_id=terminal["trace_id"], run_id=terminal["run_id"])
    history = client.get(f"/api/chat/conversations/{group_id}/history")
    history.raise_for_status()
    original = sample["group_history"] = history.json()["data"]
    runs = {row["run_id"]: row for row in original["runs"]}
    assert len(original["runs"]) == 2 and runs.keys() == {previous["run_id"], sample["run_id"]}
    sample["history"] = {**original, "runs": [runs[sample["run_id"]]],
                         "entries": [row for row in original["entries"] if row["run_id"] == sample["run_id"]]}


ORIGINAL_GROUP_ROUND = t4.group_round


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mysqld", type=Path)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--r6", action="store_true", help="Include the two original R5 rejection and repair rounds.")
    parser.add_argument("--case", choices=MODES | R6_REPLAYS, action="append")
    args = parser.parse_args()
    names = args.case or (["sdk-10", "sdk-22"] if args.baseline else [*MODES, *(R6_REPLAYS if args.r6 else ())])
    sources = {"foreign-group": "sdk-10", "foreign-run": "sdk-22"}
    names = list(dict.fromkeys([*(source for name, source in sources.items() if name in names), *names]))
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH="", API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="",
                      GLM_API_KEY="", JWT_SECRET=token_urlsafe(48), ANONYMIZED_TELEMETRY="False",
                      LLM_PROVIDER="mock", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    from backend.config.config import settings
    from backend.database.database import DEFAULT_DB_PATH
    from backend.rag import rag_knowledge
    from backend.rag.review_rule_demo import seed_review_rules
    from scripts.eval_review_retrieval import FixedReviewEmbedding

    frozen = t4.load(r3.ARCHIVE / "frozen.json")
    cases = {case["id"]: case for case in frozen["cases"]}
    archived = {row["attempt"]: row["response"]["body"]["choices"][0]["message"]
                for row in t4.load(SDK) if row["attempt"] in {10, 22}}
    protected = [DEFAULT_DB_PATH, ARTIFACT, t6.LEDGER, *t6.ARCHIVE.glob("recovery/**/*.json")]
    invalid = {}
    if set(names) & R6_REPLAYS.keys():
        r5_frozen = t4.load(R5_ARCHIVE / "preflight-final/frozen.json")
        assert r5_frozen == frozen_fixture()
        cases.update({case["id"]: case for case in r5_frozen["cases"]})
        r5_sdk = R5_ARCHIVE / "live/sdk.json"
        invalid = {row["attempt"]: row["response"]["body"]["choices"][0]["message"]
                   for row in t4.load(r5_sdk) if row["attempt"] in {2, 7}}
        protected += [r5_sdk, R5_ARCHIVE / "glm-sdk-ledger.sqlite3"]
    before = {str(path): digest(path) for path in protected}
    model, password, external = FixedModel(), token_urlsafe(24), []
    send = httpx.Client.send

    def loopback_only(client, request, **kwargs):
        if request.url.scheme != "http" or request.url.host != "127.0.0.1":
            external.append(request.url.host)
            raise AssertionError("external HTTP during fixed replay")
        return send(client, request, **kwargs)

    report = {"database": "MySQL" if args.mysqld else "SQLite", "baseline": args.baseline, "rounds": [],
              "status": "failed"}
    archives = {}
    try:
        with (fixture(directory, args.mysqld.resolve() if args.mysqld else None, password) as sql,
              TemporaryDirectory(dir=directory, prefix="knowledge-") as knowledge,
              TemporaryDirectory(dir=directory, prefix="metrics-") as metric_dir,
              patch.object(settings, "RAG_DB_PATH", knowledge),
              patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding),
              patch.object(httpx.Client, "send", loopback_only),
              server(directory, model, argparse.Namespace(path=ARTIFACT)) as url,
              httpx.Client(base_url=url, timeout=45, trust_env=False) as client):
            seed_review_rules(rag_knowledge.get_rag_knowledge())
            captcha = client.get("/api/captcha").json()
            login = client.post("/api/login", json={"username": "task27-owner", "password": password,
                                                   "captcha_id": captcha["captcha_id"], "captcha_text": "0726"})
            client.headers["Authorization"] = "Bearer " + login.json()["data"]["token"]
            report["engine_version"] = sql(
                "SELECT @@version AS version" if args.mysqld else "SELECT sqlite_version() AS version",
                rows=True)[0]["version"]
            for plan in frozen["plans"]:
                sql("UPDATE ad_plans SET plan_name=?, status=?, daily_budget=? WHERE id=? AND user_id=?",
                    (plan["plan_name"], plan["status"], plan["daily_budget"], plan["id"], OWNER))
            sql("INSERT INTO alerts (type, message, level, is_read, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (*frozen["alert"].values(), "2026-10-31 09:00:00"))
            for name in names:
                case_id, attempt = (MODES | R6_REPLAYS)[name]
                case = deepcopy(cases[case_id])
                metrics = ARTIFACT
                if attempt and name not in R6_REPLAYS:
                    case["message"] += t6.SUFFIX
                if omitted := case.get("excluded_row"):
                    metrics = Path(metric_dir) / "gap.sqlite3"
                    shutil.copyfile(ARTIFACT, metrics)
                    with closing(sqlite3.connect(metrics)) as conn, conn:
                        assert conn.execute("DELETE FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date=?",
                                            (omitted["source"], omitted["date"])).rowcount == 1
                expected = (r3.setup_oracle(sql, frozen, case, metrics) if attempt else
                            {"plans": frozen["plans"]})
                sample = {"id": name, "baseline": args.baseline, "status": "failed", "independent_sql": expected,
                          "original_sdk_attempt": attempt}
                audit = Audit(directory / "sdk.sqlite3", None, name, 0)
                previous, foreign = archives.get(sources.get(name), (None, None))
                if name == "foreign-run":
                    sample["previous_group_run"] = {key: previous[key] for key in ("request", "run_id")}
                upstream = archived.get({2: 10, 7: 22}.get(attempt, attempt))
                model.prepare_round(case, name, audit, expected, upstream, foreign, invalid.get(attempt))
                report["rounds"].append(sample)
                with (patch.object(settings, "PLAN_METRICS_DB_PATH", str(metrics)),
                      patch.object(t4, "group_round", group_round)):
                    t4.run_sample(directory, model, sql, sample, client, verify)
                if name in sources.values():
                    archives[name] = sample, next(row for row in audit.receipts if row["source"] == METRICS
                                                  and row["status"] == "succeeded")
                if name == "foreign-group":
                    assert sample["request"]["conversation_id"] != previous["request"]["conversation_id"]
                print(name, sample["trace_id"], len(sample["detail_reads"]), flush=True)
        assert not external
        report["status"] = "passed"
    finally:
        report["real_model_requests"] = 0
        passed = sum(row["status"] == "passed" for row in report["rounds"])
        report["counts"] = {"passed": passed, "failed": len(report["rounds"]) - passed,
                            "skipped": len(names) - len(report["rounds"])}
        report["blocked_external_http"] = external
        report["rounds"] = [{key: row[key] for key in ("id", "status", "trace_id", "run_id", "worker_reclaimed")
                             if key in row} for row in report["rounds"]]
        report["archive_sha256"] = before
        dump(directory / "report.json", report)
        assert before == {str(path): digest(path) for path in protected}


if __name__ == "__main__":
    main()
