"""Replay the archived T5 budget chain through the real work-group SSE endpoint."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing, nullcontext
from copy import deepcopy
import json
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, Lock
import os
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from scripts import verify_task27_t4 as t4
from scripts.run_task27_t3 import Audit
from scripts.task27_t3_fixture import ARTIFACT, OWNER, ROOT, digest
from scripts.verify_task27 import dump, fixture
from scripts.verify_task27_r2 import fields_disclosed, poison
from scripts.verify_task27_t1 import METRICS, PLAN, receipts, server


ARCHIVE = ROOT / "logs/task27-t5/formal-chromium"
LEDGER = ARCHIVE.parent / "glm-sdk-ledger.sqlite3"
T5A = ROOT / "logs/task27-t5a/live"
FINAL = "review_final_detail"
CASES = {
    "branch-1": "branch-1", "swap-1": "swap-1", "wrong-target": "branch-1",
    "text-only": "branch-1", "gap-1": "gap-1", "gap-2": "gap-2",
    "no-tools": "branch-1", "no-target": "branch-1", "already-read": "branch-1",
    "already-failed": "branch-1", "detail-failed": "branch-1", "repeat-detail": "branch-1",
}
FAILURES = {"already-failed", "detail-failed"}
EARLY = {"already-read", "already-failed"}
FINAL_CASES = {"branch-1", "swap-1", "wrong-target", "text-only", "detail-failed", "repeat-detail"}


def verify_final_payload(messages, target, adopted):
    system, user = messages
    assert set(system) == set(user) == {"role", "content"}
    assert (system["role"], user["role"]) == ("system", "user")
    assert "服务端已依据所列指标产物及已核验规则确定待查计划" in system["content"]
    assert len(system["content"]) < 160 and "get_plan_detail" in system["content"]
    windows = [row for row in adopted if row["source"] == METRICS]
    rules = [row for row in adopted if row["source"] == t4.RULE]
    assert len(windows) == 4 and len(rules) == 2
    exact, = [row for row in rules if row["arguments"]["query"] == t4.RULE_QUERY]
    hit, = exact["result"]["evidence"]
    assert not hit["truncated"]
    assert (hit["rule_id"], hit["version"], hit["chunk_id"]) == (
        "ctr-review#low-ctr", "v1", "ctr-review-v1-low-ctr-01")
    facts = json.loads(user["content"])
    assert facts == {
        "target_plan_ids": [target],
        "metric_artifact_ids": [row["artifact_id"] for row in sorted(windows, key=lambda row: (
            row["arguments"]["plan_id"], row["arguments"]["start_date"]))],
        "verified_rule": {key: hit[key] for key in ("rule_id", "version", "chunk_id")} | {
            "artifact_id": exact["artifact_id"]},
    }
    return facts


def replay_rows(case):
    rows = [row for row in t4.load(ARCHIVE / "sdk.json") if row["case"] == "branch-1"]
    assert [row["attempt"] for row in rows] == list(range(7, 19))
    main, workers = [], defaultdict(list)
    original_dates = ("2026-10-17", "2026-10-23", "2026-10-24", "2026-10-30")
    dates = [window[key] for window in case["expected"]["metrics"]["101"]["windows"]
             for key in ("start_date", "end_date")]
    for row in rows:
        message = row["response"]["body"]["choices"][0]["message"]
        calls = [t4.original_call(call) for call in message.get("tool_calls") or []]
        content = message.get("content")
        for old, new in zip(original_dates, dates):
            if content:
                content = content.replace(old, new)
            for call in calls:
                call.function.arguments = call.function.arguments.replace(old, new)
        response = (content, *calls)
        if row["role"] == "orchestrator":
            main.append(response)
        else:
            workers[row["task_id"]].append(response)
    assert len(main) == 5 and sorted(map(len, workers.values())) == [2, 2, 3]
    return main, workers


class FixedModel(t4.FixedModel):
    def prepare_round(self, case, name, audit, expected):
        self.case, self.name, self.audit = case, name, audit
        self.main_rows, self.worker_rows = replay_rows(case)
        self.calls, self.errors = [], []
        self.lock, self.indices = Lock(), Counter()
        self.metric_summaries = {key: Event() for key in ("metrics-101", "metrics-102")}
        self.target, self.poison = expected["target"], poison(expected["plans"])

    def create(self, messages, tools=None, response_format=None, **options):
        from backend.agent.tests.test_home_delegation import reply
        from backend.agent.tests.test_home_sse_tracing import tool_call

        link = self.audit.model_link.get()
        with self.lock:
            key = link["task_id"] or "main"
            index = self.indices[key]
            self.indices[key] += 1
            row = {**link, "messages": deepcopy(messages), "tools": tools, "options": options}
            self.calls.append(row)
        try:
            names = [tool["function"]["name"] for tool in tools or []]
            if key != "main":
                response = self.worker_rows[key][index]
                if key in self.metric_summaries and index == 1:
                    self.metric_summaries[key].set()
                if key == "rule-check" and index == 2:
                    # Reserve the same seven Worker calls as T5 before rule-check tries to summarize.
                    assert all(event.wait(10) for event in self.metric_summaries.values())
            else:
                current = next(msg["content"] for msg in reversed(messages) if msg["role"] == "user")
                if index and link["step"] != FINAL:
                    assert current == self.case["message"]
                response = self.main_rows[index]
                if index == 3:
                    dispatch, = response[1:]
                    assert receipts(messages)[self.main_rows[2][1].id]["error_code"] == "invalid_input_reference"
                    if self.name in EARLY:
                        response = (*response, tool_call("early-detail", name=PLAN, plan_id=101))
                    assert dispatch.function.name == "delegate_tasks"
                if index == 4 and names == [PLAN]:
                    assert link["step"] == FINAL and self.target in (101, 102)
                    if self.name == "text-only":
                        literal = json.dumps({"name": PLAN, "arguments": {"plan_id": self.target}})
                        response = (f"{response[0]}\n<tool_call>{literal}</tool_call>\n{self.poison}",)
                    elif self.name == "sdk-24":
                        from openai.types.chat import ChatCompletion

                        archived, = [row for row in t4.load(T5A / "sdk.json") if row["attempt"] == 24]
                        assert (archived["case"], archived["step"]) == ("replay-swap-1", FINAL)
                        result = ChatCompletion.model_validate(archived["response"]["body"])
                        message = result.choices[0].message
                        row["response"] = {"content": message.content, "tool_calls": [
                            {"id": call.id, "name": call.function.name, "raw_arguments": call.function.arguments}
                            for call in message.tool_calls]}
                        return result
                    else:
                        target = 203 - self.target if self.name == "wrong-target" else self.target
                        response = (self.poison, tool_call("final-detail", name=PLAN, plan_id=target))
                        if self.name == "repeat-detail":
                            response = (*response, tool_call("repeat", name=PLAN, plan_id=target),
                                        tool_call("extra", name="get_alerts", unread_only=True))
                elif index == 4:
                    assert not names and link["step"] == "main_summary"
            result = reply(*response)
            row["response"] = {"content": response[0], "tool_calls": [
                {"id": call.id, "name": call.function.name, "raw_arguments": call.function.arguments}
                for call in response[1:]]}
            return result
        except Exception as error:
            self.errors.append(f"{type(error).__name__}: {error}")
            raise

    def record_receipt(self, record):
        return self.audit.wrap_receipt(record)


def setup_oracle(sql, frozen, case, metrics_path):
    source = frozen["source"]
    sql("DELETE FROM plan_metric_bindings")
    for plan, campaign in case["bindings"].items():
        if campaign:
            sql("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, ?)",
                (int(plan), source["source_id"], source["data_version"], campaign))
    bindings = sql("SELECT * FROM plan_metric_bindings ORDER BY plan_id", rows=True)

    # Use the frozen SQL directly against the active engine's bindings and isolated metric source.
    plans = sql(frozen["plan_sql"], (OWNER,), rows=True)
    assert plans == frozen["plans"]
    bound = {str(row["plan_id"]): row["source_plan_id"] for row in bindings}
    assert bound == {p: v for p, v in case["bindings"].items() if v}
    rule = case["expected"]["rule"]
    comparisons = {}
    with closing(sqlite3.connect(metrics_path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        for plan, expected in case["expected"]["metrics"].items():
            windows = []
            for window in expected["windows"]:
                params = [bound.get(plan), window["start_date"], window["end_date"], None]
                actual = dict(conn.execute(frozen["sql"]["window"], params).fetchone())
                actual["missing_dates"] = actual["missing_dates"].split(",") if actual["missing_dates"] else []
                assert all(window[key] == value for key, value in actual.items())
                windows.append({**actual, "start_date": window["start_date"], "end_date": window["end_date"],
                                "sql_params": params})
            base, recent = windows
            params = [base["impressions"], recent["impressions"], base["ctr_pct"], recent["ctr_pct"],
                      rule["min_impressions"], rule["min_relative_decline_pct"]]
            result = dict(conn.execute(frozen["sql"]["review"], params).fetchone())
            assert all(expected[key] == value for key, value in result.items())
            comparisons[plan] = {**result, "windows": windows, "review_sql_params": params}
    targets = [int(plan) for plan, row in comparisons.items() if row["qualifies"]]
    target = targets[0] if len(targets) == 1 and all(row["qualifies"] is not None for row in comparisons.values()) else None
    assert target == (case["expected"]["detail"] or {}).get("id")
    return {"target": target, "plans": plans, "bindings": bindings, "metrics": comparisons, "rule": rule}


def verify(sample, model, sql):
    answer, entries, steps, reclaimed, _ = t4.verify_lifecycle(sample, model, sql)
    name, baseline = model.name, sample["baseline"]
    calls, reads = model.calls, model.audit.reads
    assert model.indices == {"main": 5 if baseline or name in FINAL_CASES | {"sdk-24"} else 4,
                             "metrics-101": 2, "metrics-102": 2, "rule-check": 3}
    assert len(reclaimed) == 3 and len(calls) <= 12
    assert not model.audit.rows()
    main_calls = [row for row in calls if row["role"] == "orchestrator"]
    assert [row["step"] for row in main_calls[:4]] == ["plan", "main", "main", "main"]
    for row in main_calls[1:4]:
        assert {tool["function"]["name"] for tool in row["tools"]} == {
            PLAN, METRICS, t4.RULE, "get_alerts", "delegate_tasks"}
    assert sum(step.get("error_code") == "invalid_input_reference" for step in steps) == 1
    assert any(step.get("error_code") == "model_budget_exhausted" for step in steps)
    counts = Counter(row["tool"] for row in reads)
    assert counts[METRICS] == 4 and counts[t4.RULE] == 2 and counts["get_alerts"] == 1
    assert any(step["stage"] == "tool_reuse" and step["action"] == t4.RULE for step in steps)
    reply_step, = [step["evidence"] for step in steps
                   if (step["stage"], step["action"]) == ("conditional_detail", "reply")]
    opened = [step for step in steps if (step["stage"], step["action"]) == (FINAL, "opened")]
    outcomes = [step for step in steps if step["stage"] == FINAL and step["action"] == "result"]
    if baseline:
        assert main_calls[-1]["step"] == "main_summary" and not main_calls[-1]["tools"]
        assert "<tool_call>" in main_calls[-1]["response"]["content"]
        batch = receipts(main_calls[-1]["messages"])[main_calls[3]["response"]["tool_calls"][0]["id"]]
        assert (batch["remaining"]["model"], batch["remaining"]["tool"]) == (1, 5)
        assert reply_step["target_plan_ids"] == [101] and not reply_step["error_code"]
    elif name in FINAL_CASES | {"sdk-24"}:
        last = main_calls[-1]
        schema, = last["tools"]
        assert schema["function"]["name"] == PLAN
        params = schema["function"]["parameters"]
        assert params["required"] == ["plan_id"] and set(params["properties"]) == {"plan_id"}
        assert last["step"] == FINAL and last["options"]["tool_choice"] == "auto"
        entry, = opened
        outcome, = outcomes
        assert {key: entry["budget"][key] for key in ("model", "tool")} == {"model": 1, "tool": 5}
        assert entry["evidence"]["reason"] == "eligible_detail_at_model_limit"
        assert entry["evidence"]["allowed_plan_ids"] == [model.target]
        assert entry["evidence"]["evidence_artifact_ids"] == reply_step["evidence_artifact_ids"]
        batch = sample["direct_results"][main_calls[3]["response"]["tool_calls"][0]["id"]]
        adopted = [row for row in batch["evidence"] if row["source"] in {METRICS, t4.RULE}]
        assert {row["artifact_id"] for row in adopted} == set(entry["evidence"]["evidence_artifact_ids"])
        assert verify_final_payload(last["messages"], model.target, adopted) == entry["evidence"]["request_evidence"]
        assert any(gap["error_code"] == "model_budget_exhausted" for gap in batch["gaps"])
        assert outcome["evidence"]["detail_artifact_ids"] == reply_step["detail_artifact_ids"]
        actions = outcome["evidence"]["actions"]
        errors = {"wrong-target": ["review_wrong_target"], "detail-failed": ["temporary_tool_failure"],
                  "repeat-detail": [None, "conditional_detail_already_attempted", "unauthorized_tool"],
                  "text-only": [], "sdk-24": ["unauthorized_tool"]}.get(name, [None])
        assert [row.get("error_code") for row in actions] == errors
        assert [row["tool_call_id"] for row in actions] == [row["id"] for row in last["response"]["tool_calls"]]
    else:
        assert not opened and not outcomes
        assert not any(row["step"] == "main_summary" for row in main_calls)
        assert len(main_calls) == 4

    should_read = not baseline and name in ({"branch-1", "swap-1", "repeat-detail"} | FAILURES | EARLY)
    leaves = [row for row in reads if row["tool"] == "query_plan_detail"]
    assert [(row["arguments"]["plan_id"], row["arguments"]["user_id"]) for row in leaves] == (
        [(model.target, OWNER)] if should_read else [])
    assert counts[PLAN] == len(leaves)
    successful = should_read and name not in FAILURES
    assert len(reply_step["detail_artifact_ids"]) == int(successful)
    allowed = [step for step in steps if (step["stage"], step["action"]) == ("conditional_detail", "allowed")]
    assert len(allowed) == len(leaves)
    if leaves:
        leaf, = leaves
        admission, = allowed
        assert admission["runtime_call_id"] == leaf["runtime_call_id"]
        assert admission["evidence"]["target_plan_ids"] == [model.target]
        assert admission["evidence"]["evidence_artifact_ids"] == reply_step["evidence_artifact_ids"]
        accepted = [step for step in steps if (step["stage"], step["action"]) == ("receipt", "accepted")
                    and step.get("runtime_call_id") == leaf["runtime_call_id"]]
        assert [step["evidence"]["artifact_id"] for step in accepted] == reply_step["detail_artifact_ids"]
        if name in FAILURES:
            assert leaf["exception"] == "TimeoutError"
    if name == "wrong-target":
        assert any(step["action"] == "blocked" and step.get("error_code") == "review_wrong_target"
                   and step["evidence"]["requested_plan_id"] == 102 for step in steps)
    if name == "no-tools":
        assert counts["get_alerts"] + counts[METRICS] + counts[t4.RULE] == 7
        assert any(row["stage"] == "react_decision" and row.get("budget", {}).get("tool") == 0 for row in steps)
    expected_gap = {"gap-1": "未绑定历史来源", "gap-2": "缺报 2026-10-27",
                    "no-target": "复核清单为空", "already-failed": "详情读取失败",
                    "detail-failed": "详情读取失败"}.get(name)
    if expected_gap:
        assert expected_gap in answer
    if not successful and name != "no-target":
        assert "暂缓" in answer
    assert "严重 CTR 告警 1 条" in answer and reply_step["policy"]["chunk_id"] in answer
    for row in reply_step["rows"]:
        for window in row["windows"]:
            assert f'{window["start_date"]} 至 {window["end_date"]}' in answer
            if not window["gap"]:
                assert f'曝光 {window["impressions"]:,}，点击 {window["clicks"]:,}' in answer
    committed = sql("SELECT kind, content, details FROM chat_group_entries WHERE run_id=? AND kind<>'user' ORDER BY id",
                    (sample["run_id"],), rows=True)
    for public in (sample["sse"], entries[1:], committed):
        text = "\n".join(row.get("content", "") for row in public) + "\n" + str(public)
        assert "<tool_call>" not in text
        for plan in sample["independent_sql"]["plans"]:
            expected = successful and plan["id"] == model.target
            assert all(found == expected for found in fields_disclosed(text, plan).values())
    sample.update(status="passed", model_requests=len(calls), business_reads=sum(counts.values()) - len(leaves),
                  read_counts=dict(counts), detail_targets=[row["arguments"]["plan_id"] for row in leaves],
                  detail_artifact_ids=reply_step["detail_artifact_ids"], worker_reclaimed=len(reclaimed),
                  final_sdk_step=main_calls[-1]["step"], answer=answer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mysqld", type=Path)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--case", choices=(*CASES, "sdk-24"), action="append")
    parser.add_argument("--r4", action="store_true", help="Include the unmodified T5A SDK #24 response.")
    args = parser.parse_args()
    assert __debug__ and (not args.mysqld or args.mysqld.is_file())
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
    from backend.tools import tools
    from scripts.eval_review_retrieval import FixedReviewEmbedding

    frozen = t4.load(ARCHIVE / "frozen.json")
    cases = {case["id"]: case for case in frozen["cases"]}
    before = {str(path): digest(path) for path in (
        DEFAULT_DB_PATH, ARTIFACT, LEDGER, *sorted(path for path in ARCHIVE.rglob("*") if path.is_file()),
        T5A / "sdk.json", T5A / "glm-sdk-ledger.sqlite3")}
    assert before[str(ARTIFACT)] == frozen["source"]["artifact_sha256"]
    model, password, external = FixedModel(), token_urlsafe(24), []
    report = {"task": "TASK-027-R3", "baseline": args.baseline, "status": "failed", "rounds": [],
              "database": "MySQL" if args.mysqld else "SQLite", "real_model_requests": 0, "oracle_sql": frozen["sql"]}
    send = httpx.Client.send

    def loopback_only(client, request, **kwargs):
        if request.url.scheme != "http" or request.url.host != "127.0.0.1":
            external.append(request.url.host)
            raise AssertionError("External HTTP is disabled during fixed replay")
        return send(client, request, **kwargs)

    def fail_after_query(plan_id, user_id, connect):
        query_plan_detail(plan_id, user_id, connect)
        raise TimeoutError("injected detail failure after one SQL read")

    query_plan_detail = tools.query_plan_detail
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
            assert login.status_code == 200 and login.json()["code"] == 200
            client.headers["Authorization"] = "Bearer " + login.json()["data"]["token"]
            report["engine_version"] = sql(
                "SELECT @@version AS version" if args.mysqld else "SELECT sqlite_version() AS version", rows=True)[0]["version"]
            for plan in frozen["plans"]:
                sql("UPDATE ad_plans SET plan_name=?, status=?, daily_budget=? WHERE id=? AND user_id=?",
                    (plan["plan_name"], plan["status"], plan["daily_budget"], plan["id"], OWNER))
            sql("INSERT INTO alerts (type, message, level, is_read, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (*frozen["alert"].values(), "2026-10-31 09:00:00"))
            assert sql(frozen["alert_sql"], (OWNER,), rows=True) == [frozen["alert"]]
            names = args.case or (["branch-1"] if args.baseline else [*CASES, *(["sdk-24"] if args.r4 else [])])
            assert not args.baseline or names == ["branch-1"]
            for name in names:
                case = deepcopy(cases["swap-1" if name == "sdk-24" else CASES[name]])
                if name == "no-target":
                    case["bindings"]["101"] = case["bindings"]["102"]
                    case["expected"]["metrics"]["101"] = case["expected"]["metrics"]["102"]
                    case["expected"]["detail"] = None
                metrics = ARTIFACT
                if omitted := case.get("excluded_row"):
                    metrics = Path(metric_dir) / "gap.sqlite3"
                    shutil.copyfile(ARTIFACT, metrics)
                    with closing(sqlite3.connect(metrics)) as conn, conn:
                        deleted = conn.execute("DELETE FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date=?",
                                               (omitted["source"], omitted["date"])).rowcount
                        assert deleted == 1
                expected = setup_oracle(sql, frozen, case, metrics)
                sample = {"id": name, "baseline": args.baseline, "status": "failed", "independent_sql": expected}
                audit = Audit(directory / "sdk.sqlite3", None, name, 0)
                audit.case = name
                model.prepare_round(case, name, audit, expected)
                report["rounds"].append(sample)
                import fast_server
                from backend.agent.team_tools import TeamBudget

                limit = (patch.object(fast_server, "TeamBudget", return_value=TeamBudget(tool_limit=7))
                         if name == "no-tools" else nullcontext())
                with (patch.object(settings, "PLAN_METRICS_DB_PATH", str(metrics)),
                      patch.object(tools, "query_plan_detail", fail_after_query if name in FAILURES else query_plan_detail),
                      limit):
                    t4.run_sample(directory, model, sql, sample, client, verify)
                print(name, sample["trace_id"], sample["model_requests"], sample["read_counts"], flush=True)
        assert not external
        report["status"] = "passed"
    finally:
        report["file_sha256_before"] = before
        report["file_sha256_after"] = {path: digest(Path(path)) for path in before}
        report["blocked_external_http"] = external
        report["rounds"] = [{key: row[key] for key in (
            "id", "status", "trace_id", "run_id", "model_requests", "business_reads", "read_counts",
            "detail_targets", "detail_artifact_ids", "worker_reclaimed", "final_sdk_step") if key in row}
            for row in report["rounds"]]
        dump(directory / "report.json", report)
        assert report["file_sha256_before"] == report["file_sha256_after"]


if __name__ == "__main__":
    main()
