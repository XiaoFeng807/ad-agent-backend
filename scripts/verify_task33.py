"""Verify stop/answer ordering, cancellation and business isolation in Chromium."""

import argparse
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter_ns
import traceback
from unittest.mock import patch
from uuid import uuid4

from scripts.run_task27_t3 import Audit
from scripts.task27_t3_fixture import ARTIFACT, OWNER, ROOT, digest, write
from scripts.verify_task27 import fixture
from scripts.verify_task28 import DATE_CASE, FixedModel, STATUS_REQUEST, Synchronization, freeze, serve, t5


class StopSync(Synchronization):
    def prepare(self, mode):
        self.reset(mode)
        self.stages = {name: (Event(), Event()) for name in ("save", "stop", "register", "side-save", "new-save")}
        self.stages["metric"] = self.waiting, self.release
        self.metric_filter = lambda args: args[:3] == (101, "2026-10-24", "2026-10-30")
        self.save_count = 0

    def gate(self, name, **fields):
        self.mark(name + "_waiting", **fields)
        ready, resume = self.stages[name]
        ready.set()
        assert resume.wait(45), name + " barrier expired"

    def wrap_save(self, original):
        def save(run, *args, **kwargs):
            if self.mode in {"stop-first", "answer-first"}:
                self.gate("save", run_id=run.run_id)
            elif self.mode == "late":
                self.save_count += 1
                self.gate("side-save" if self.save_count == 1 else "new-save", run_id=run.run_id)
            try:
                entry_id = original(run, *args, **kwargs)
            except Exception as error:
                self.mark("save_rejected", run_id=run.run_id, error=type(error).__name__)
                raise
            self.mark("answer_committed", run_id=run.run_id, entry_id=entry_id)
            return entry_id
        return save

    def wrap_stop(self, original, sql):
        def stop(user_id, group_id, run_id):
            self.mark("stop_arrived", run_id=run_id, conversation_id=group_id, user_id=user_id)
            if self.mode == "answer-first":
                self.gate("stop", run_id=run_id)
            if self.mode == "late" and user_id == OWNER and sql(
                "SELECT 1 FROM chat_group_runs WHERE run_id=? AND conversation_id=? AND status='running'",
                (run_id, group_id), rows=True,
            ):
                self.duplicate.wait(25)
            try:
                result = original(user_id, group_id, run_id)
            except Exception as error:
                self.mark("stop_rejected", run_id=run_id, error=type(error).__name__)
                raise
            self.mark("stop_returned", **result)
            return result
        return stop

    def wrap_register(self, original):
        def register(run, execution):
            if self.mode == "pre-register":
                self.gate("register", run_id=run.run_id)
            original(run, execution)
            self.mark("execution_registered", run_id=run.run_id, stop_code=execution.stop_code())
        return register


def business_rows(sql):
    tables = ("ad_plans", "ad_accounts", "diagnosis_sandbox_targets", "diagnosis_review_proposals",
              "diagnosis_reviews", "diagnosis_pause_proposals", "diagnosis_pause_receipts")
    return {table: sql(f"SELECT * FROM {table} ORDER BY 1", rows=True) for table in tables}


def seed_pause_history(sql):
    sql("INSERT INTO ad_accounts (account_name, platform, daily_budget, user_id) VALUES ('验收账户', 'demo', 1500, 17)")
    sql("INSERT INTO diagnosis_sandbox_targets VALUES (102, 17, 'task33-registration')")
    sql("""INSERT INTO diagnosis_tasks VALUES
        (17, 'task33-pause', 102, 'ctr-review-v1#low-ctr', '2026-10-17', '2026-10-23', '2026-10-24', '2026-10-30', 'structured')""")
    sql("INSERT INTO diagnosis_turns (id, user_id, task_id, message, reply) VALUES (1, 17, 'task33-pause', '诊断', '复核已保存')")
    sql("INSERT INTO diagnosis_review_proposals VALUES ('task33-review', 1, ?)",
        (json.dumps({"source_trace_id": "task33-prior-review"}),))
    sql("INSERT INTO diagnosis_pause_proposals VALUES ('task33-pause-action', 'task33-review', 102, 'task33-registration', '南桥搜索', 'active')")
    sql("INSERT INTO diagnosis_pause_receipts (receipt_id, action_id, execution_trace_id, committed_status) VALUES "
        "('task33-prior-receipt', 'task33-pause-action', 'task33-prior-pause', 'paused')")


def audit_case(mode, browser, sql, trace, sync, model, audit, memory):
    run = browser["run"]
    rows = sql("SELECT * FROM chat_group_entries WHERE run_id=? ORDER BY id", (run,), rows=True)
    stops = [row for row in rows if row["kind"] == "stop"]
    answers = [row for row in rows if row["kind"] == "assistant"]
    indexed = [row for row in memory if row["user_message_id"] == rows[0]["id"]]
    result = {"entries": rows, "stop_writes": len(stops), "answer_writes": len(answers), "memory_writes": len(indexed)}
    expected = (0, 1, 1) if mode == "answer-first" else (1, 0, 0)
    assert (len(stops), len(answers), len(indexed)) == expected, result
    assert trace["status"] == ("completed" if mode == "answer-first" else "stopped")
    if stops:
        assert json.loads(stops[0]["details"]) == browser["stop"]["body"]["data"]
        assert (run, trace["trace_id"], OWNER, "task27-owner") == (
            browser["stop"]["body"]["data"]["run_id"], browser["stop"]["body"]["data"]["trace_id"],
            browser["stop"]["body"]["data"]["stopped_by_user_id"], browser["stop"]["body"]["data"]["stopped_by_name"])
    if mode == "answer-first":
        assert json.loads(answers[0]["details"]) == {
            key: value for key, value in browser["basis"].items() if key not in {"conversation_id", "run_id", "entry_id"}}
    if mode in {"stop-first", "answer-first"}:
        order = ["save_waiting", "stop_returned", "save_released", "save_rejected"] if mode == "stop-first" else [
            "stop_arrived", "stop_waiting", "save_released", "answer_committed", "stop_released", "stop_rejected"]
        indexes = [next(i for i, row in enumerate(sync.timeline) if row["event"] == name) for name in order]
        assert indexes == sorted(indexes), sync.timeline
    if mode == "pre-register":
        assert not model and not audit.reads
        assert next(row for row in sync.timeline if row["event"] == "execution_registered")["stop_code"] == "run_stopped"
    if mode == "late":
        steps = trace["steps"]
        workers = [step for step in steps if step["stage"] == "worker"]
        assert [step["action"] for step in workers] == ["created", "reclaimed"]
        assert [step["detail"] for step in workers] == ["evidence-101", "evidence-101"]
        sent, = [row for row in sync.timeline if row["event"] == "old_metric_sent"]
        call_id = sent["runtime_call_id"]
        late, = [step for step in steps if step["stage"] == "tool_result" and step.get("runtime_call_id") == call_id]
        assert late["error_code"] == "expired_receipt" and late["evidence"]["returned_after_stop"]
        assert not any(row["accepted"] and row["tool_call_id"] == call_id for row in audit.receipts)
        assert {row["task_id"] for row in model if row.get("worker_id") and row["trace_id"] == trace["trace_id"]} == {"evidence-101"}
        reads = [row for row in audit.reads if row["trace_id"] == trace["trace_id"] and row["role"] == "team_worker"]
        assert [(row["parent_call_id"], row["tool"]) for row in reads] == [(workers[0]["runtime_call_id"], "query_plan_metrics")] * 2
        entries = [json.loads(row["details"]) for row in rows if row["kind"] == "worker"]
        assert [row["kind"] for row in entries].count("queued") == 3
        assert [row["kind"] for row in entries].count("waiting") == 1
        assert [row["kind"] for row in entries].count("reclaimed") == 1
        assert not any(row.get("runtime_call_id") == call_id and row["kind"] != "tool_started" for row in entries)
        assert browser["pauseReceipt"]["body"]["data"]["receipt"]["receipt_id"] == "task33-prior-receipt"
        for completed in (browser["sideRun"], browser["newRun"]):
            assert sql("SELECT status FROM chat_group_runs WHERE run_id=?", (completed,), rows=True) == [{"status": "completed"}]
            assert len(sql("SELECT id FROM chat_group_entries WHERE run_id=? AND kind='assistant'", (completed,), rows=True)) == 1
            completed_trace = browser["streams"][completed]["events"][-1]["trace_id"]
            assert any(row["trace_id"] == completed_trace and row["step"] == "main" for row in model)
            assert any(row["trace_id"] == completed_trace and row["tool"] == "query_plan_detail" for row in audit.reads)
        result.update(late_call_id=call_id, workers=workers, worker_entries=entries,
                      queued_or_waiting_tasks={"evidence-102": 0, "rule": 0, "review": 0})
    return result


def run(output, modes):
    from chromadb.api.shared_system_client import SharedSystemClient
    from backend import observability
    from backend.agent import home_react, llm_provider, team_runtime, team_tools
    from backend.captcha import captcha
    from backend.config.config import settings
    from backend.database import chat_groups
    from backend.memory import conversation_memory
    from backend.rag import rag_knowledge
    from backend.rag.review_rule_demo import seed_review_rules
    from backend.tools import tools
    from scripts.eval_review_retrieval import FixedReviewEmbedding
    from tests.home_diagnosis_server import demo_captcha

    output.mkdir(parents=True, exist_ok=False)
    password = token_urlsafe(24)
    before = {"business": digest(ROOT / "ad_agent.db"), "metrics": digest(ARTIFACT)}
    report = {"cases": {}, "storage_before": before}
    sync = StopSync(DATE_CASE)
    mutations, memory = [], []

    def watch(label, original, log, *, record_args=False):
        def observed(*args, **kwargs):
            log.append({"action": label, "ns": perf_counter_ns(), **(kwargs if record_args else {})})
            return original(*args, **kwargs)
        return observed

    with (fixture(output, None, password) as sql, TemporaryDirectory(dir=output) as scratch, ExitStack() as stack):
        audit = Audit(Path(scratch) / "empty.sqlite3", None, uuid4().hex, 0)
        model = FixedModel(audit, DATE_CASE)
        stack.callback(SharedSystemClient.clear_system_cache)
        stack.callback(lambda: [system.stop() for system in SharedSystemClient._identifier_to_system.values()])
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=model))
        stack.enter_context(patch.object(settings, "PLAN_METRICS_DB_PATH", str(ARTIFACT)))
        stack.enter_context(patch.object(settings, "RAG_DB_PATH", str(Path(scratch) / "rag")))
        for module in (rag_knowledge, conversation_memory):
            stack.enter_context(patch.object(module, "BGEEmbedding", FixedReviewEmbedding))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        for name in ("TRACE_LOG_FILE", "FUNC_LOG_FILE", "ERROR_LOG_FILE"):
            stack.enter_context(patch.object(observability, name, output / (name.lower() + ".jsonl")))
        for module in (llm_provider, team_runtime):
            stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        for module in (home_react, team_tools):
            stack.enter_context(patch.object(module, "trace_tool_call", audit.trace_tool(module.trace_tool_call)))
        import fast_server

        stack.enter_context(patch.object(team_runtime.DelegationRun, "_record",
                                         audit.wrap_receipt(team_runtime.DelegationRun._record)))
        stack.enter_context(patch.object(team_runtime.DelegationRun, "_schedule",
                                         sync.schedule(team_runtime.DelegationRun._schedule)))
        for name in ("query_plan_metrics", "query_plan_detail"):
            read = audit.wrap_read(name, getattr(tools, name))
            stack.enter_context(patch.object(tools, name, sync.read(audit, read) if name == "query_plan_metrics" else read))
        for module, name in ((tools, "toggle_plan_status"), (tools, "update_daily_budget"), (fast_server, "confirm_pause")):
            stack.enter_context(patch.object(module, name, watch(name, getattr(module, name), mutations)))
        stack.enter_context(patch.object(conversation_memory, "store_conversation",
                                         watch("store_conversation", conversation_memory.store_conversation, memory,
                                               record_args=True)))
        stack.enter_context(patch.object(chat_groups.GroupRun, "save_reply", sync.wrap_save(chat_groups.GroupRun.save_reply)))
        stack.enter_context(patch.object(fast_server, "_register_group_execution",
                                         sync.wrap_register(fast_server._register_group_execution)))
        stack.enter_context(patch.object(fast_server, "_stop_group_run", sync.wrap_stop(fast_server._stop_group_run, sql)))
        init_runtime = team_runtime.DelegationRun.__init__

        def init(runtime, **kwargs):
            if sync.mode == "late":
                kwargs["concurrency"] = 1  # Keep independent tasks queued while the first worker holds the SQL response.
            init_runtime(runtime, **kwargs)

        stack.enter_context(patch.object(team_runtime.DelegationRun, "__init__", init))
        freeze(sql, output, DATE_CASE)
        seed_pause_history(sql)
        seed_review_rules(rag_knowledge.get_rag_knowledge())
        try:
            with serve(fast_server.app, sync) as url:
                for mode in modes:
                    folder = output / mode
                    folder.mkdir()
                    sync.prepare(mode)
                    audit.case = mode
                    model.split_old_metrics = mode == "late"
                    model.calls.clear()
                    audit.reads.clear()
                    audit.receipts.clear()
                    memory.clear()
                    mutations.clear()
                    business_before = business_rows(sql)
                    request_file, result_file = folder / "request.json", folder / "browser.json"
                    write(request_file, {"mode": mode, "message": DATE_CASE.message, "direct": STATUS_REQUEST,
                                         "pauseReceipt": "task33-pause-action"})
                    try:
                        completed = subprocess.run(
                            ["node", "tests/task33_browser.cjs", url, str(request_file), str(result_file)],
                            cwd=ROOT, env={**t5.browser_env(), "AD_TASK33_PASSWORD": password},
                            capture_output=True, text=True, timeout=150, encoding="utf-8",
                        )
                        if completed.returncode:
                            (folder / "browser-error.txt").write_text(completed.stderr, encoding="utf-8")
                            raise AssertionError(completed.stderr)
                        browser = json.loads(result_file.read_text(encoding="utf-8"))
                        trace_id = browser["streams"][browser["run"]]["events"][-1]["trace_id"]
                        trace = observability.get_trace(trace_id)
                        write(folder / "trace.json", trace)
                        commits = audit_case(mode, browser, sql, trace, sync, model.calls, audit, memory)
                        collection = conversation_memory._get_collection(conversation_memory._get_client())
                        documents = collection.get(where={"user_message_id": commits["entries"][0]["id"]})
                        assert len(documents["ids"]) == commits["memory_writes"]
                        write(folder / "commits.json", {**commits, "indexed_documents": documents})
                        report["cases"][mode] = "pass"
                    except Exception:
                        report["cases"][mode] = "fail"
                        (folder / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
                        raise
                    finally:
                        write(folder / "timeline.json", deepcopy(sync.timeline))
                        write(folder / "model.json", deepcopy(model.calls))
                        write(folder / "receipts.json", deepcopy(audit.receipts))
                        write(folder / "memory.json", deepcopy(memory))
                        write(folder / "task-sheets.json", deepcopy(sync.task_sheets))
                        write(folder / "reads.json", [
                            {**row, "arguments": {key: value for key, value in row["arguments"].items() if key != "connect"}}
                            for row in audit.reads])
                        after = business_rows(sql)
                        write(folder / "business.json", {"before": business_before, "after": after, "mutation_calls": mutations})
                        assert business_before == after and not mutations
        finally:
            report["sdk_attempts"] = len(audit.rows())
            report["storage_after"] = {"business": digest(ROOT / "ad_agent.db"), "metrics": digest(ARTIFACT)}
            write(output / "report.json", report)
            assert before == report["storage_after"] and report["sdk_attempts"] == 0
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    modes = ("late", "stop-first", "answer-first", "pre-register")
    parser.add_argument("--case", choices=modes)
    args = parser.parse_args()
    os.environ.update(PYTHON_DOTENV_DISABLED="1", AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH="", JWT_SECRET=token_urlsafe(48), ANONYMIZED_TELEMETRY="False",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", LLM_PROVIDER="mock",
                      API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="", GLM_API_KEY="")
    run(args.output_dir.resolve(), (args.case,) if args.case else modes)
