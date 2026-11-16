"""Verify evidence-first independent review and pause admission in two workgroups."""

import argparse
from collections import Counter, defaultdict, deque
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from traceback import format_exc
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from scripts import run_task27_t5 as t5, run_task27_t6 as t6
from scripts.run_real_diagnosis import Redactor
from scripts.task27_t3_fixture import ARTIFACT, OWNER, ROOT, digest, write
from scripts.verify_task28 import DETAIL, MESSAGE, METRICS, RULE, Synchronization, fixture, freeze, serve
from scripts.verify_task32 import CASE


PROMPT = MESSAGE.replace("#102", "#103") + (
    " 第一批只做取证，等回执齐备后再追加独立复核。"
    "第一份 delegate_tasks 只安排指标取证和指定 v1 条文核对。"
    "收到 review_required 后，用另一份 delegate_tasks 追加独立复核，原样引用回执中的五组 task_id 和 artifact_id。"
)
CASES = ("complete", "missing")
RULE_CHUNK = "ctr-review-v1-low-ctr-01"


class LiveAudit(t6.T6Audit):
    run_cap = 100

    def case_cap(self, case):
        return 60


class FixedModel:
    """Replay the original first batch, then exercise the corrected continuation."""

    def __init__(self, audit, replay):
        self.audit = audit
        self.replay = t5.read(replay or ROOT / "tests/task36_initial_glm.json")
        self.chat = SimpleNamespace(completions=self)
        self.calls = []
        self.case = None

    def create(self, messages, tools=None, **options):
        if self.case != self.audit.case:
            self.case = self.audit.case
            self.pending = defaultdict(deque)
            for row in self.replay:
                if row.get("complete_only") and self.case != "complete":
                    continue
                self.pending[row["task_id"]].append(row["message"])
        from backend.agent.tests.test_home_delegation import dispatch, reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call

        link = self.audit.model_link.get()
        task = link["task_id"]
        returned = [json.loads(row["content"]) for row in messages if row["role"] == "tool"]
        if task == "review":
            response = reply("", tool_call("fixed-detail", name=DETAIL, plan_id=103))
        elif self.pending[task]:
            message = self.pending[task].popleft()
            calls = [SimpleNamespace(id=row["id"], function=SimpleNamespace(**row["function"]))
                     for row in message.get("tool_calls") or []]
            response = reply(message["content"], *calls)
        elif task is not None:
            response = worker_summary("本轮工具回执已交回")
        elif self.case == "missing":
            response = reply("有取证缺项，复核暂缓。")
        else:
            feedback = returned[-1]["task_sheet_feedback"]
            assert feedback["stage"] == "append"
            response = reply("", dispatch("fixed-append", {
                "task_id": "review", "objective": "依据五份回执独立复核并查询入选计划",
                "capabilities": [DETAIL], "artifact_refs": feedback["artifact_refs"],
                "expected_output": "入选计划详情回执", "stop_condition": "交回已获准的详情回执",
            }))
        response_message = response.choices[0].message
        self.calls.append({**link, "case": self.case, "body": {"messages": messages.copy(), "tools": tools or []},
                           "tool_calls": [{"id": action.id, "function": vars(action.function)}
                                          for action in response_message.tool_calls or []]})
        return response


def browser_round(url, folder, name, sandbox, password, *, message=PROMPT, suite=None, ctr_plan_id=None):
    request, result = folder / "browser-request.json", folder / "browser.json"
    write(request, {"name": name, "message": message, "sqlite": str(sandbox), "suite": suite,
                    "ctr_plan_id": ctr_plan_id})
    command = ["node", "tests/task36_browser.cjs", url, str(request), str(result)]
    completed = subprocess.run(command, cwd=ROOT, env={**t5.browser_env(), "AD_TASK36_PASSWORD": password},
                               capture_output=True, text=True, encoding="utf-8", timeout=170)
    if completed.returncode:
        (folder / "browser-error.txt").write_text(completed.stderr, encoding="utf-8")
        raise AssertionError(completed.stderr)
    return t5.read(result)


def fixture_rows(sandbox):
    with closing(sqlite3.connect(Path(sandbox).as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {key}")]
                for table, key in (("ad_plans", "id"), ("plan_metric_bindings", "plan_id"),
                                   ("diagnosis_sandbox_targets", "plan_id"))}


def run(output, mode, replay=None, ledger=None, *, suite=None, selected=None):
    from chromadb.api.shared_system_client import SharedSystemClient
    from backend import observability
    from backend.agent import home_react, llm_provider, team_runtime, team_tools
    from backend.captcha import captcha
    from backend.config.config import settings
    from backend.database import database, diagnosis_pauses
    from backend.memory import conversation_memory
    from backend.rag import rag_knowledge
    from backend.rag.review_rule_demo import DEMO_RULES, SOURCE, seed_review_rules
    from backend.tools import tools
    from scripts.eval_review_retrieval import FixedReviewEmbedding
    from tests.home_diagnosis_server import demo_captcha

    output.mkdir(parents=True, exist_ok=False)
    protected = (ROOT / "ad_agent.db", ARTIFACT)
    before = {str(path.relative_to(ROOT)): digest(path) for path in protected}
    report = {"mode": mode, "cases": {}, "storage_before": before}
    password = token_urlsafe(24)
    redactor = Redactor()
    redactor.secrets.append(password)
    sync = Synchronization(CASE)
    audit_type = suite.Audit if suite else LiveAudit
    audit = audit_type(ledger or output / "glm-sdk-ledger.sqlite3", redactor, uuid4().hex, audit_type.run_cap)
    report.update(ledger=str(audit.ledger), ledger_run=audit.run)
    try:
        with fixture(output, None, password) as sql, TemporaryDirectory(dir=output) as state, ExitStack() as stack:
            sandbox = database.sandbox_db_path()
            assert sandbox and not settings.DIAGNOSIS_MYSQL_ENABLED
            oracle = freeze(sql, output, CASE, seed_alert=getattr(suite, "SEED_ALERT", True))["new"]
            oracle = json.loads(json.dumps(oracle))
            assert oracle["targets"] == [103] and oracle["metrics"]["103"]["qualifies"] == 1
            diagnosis_pauses.register_sandbox_target(OWNER, 103)
            sql("CREATE TABLE task34_status_updates (plan_id INTEGER, old_status TEXT, new_status TEXT)")
            sql("""CREATE TRIGGER task36_count_update AFTER UPDATE OF status ON ad_plans
                   WHEN old.status != new.status BEGIN
                     INSERT INTO task34_status_updates VALUES (old.id, old.status, new.status);
                   END""")
            complete = fixture_rows(sandbox)
            registration, = complete["diagnosis_sandbox_targets"]
            assert (registration["plan_id"], registration["user_id"]) == (103, OWNER)
            assert next(row["status"] for row in complete["ad_plans"] if row["id"] == 103) == "active"
            missing = {**complete, "plan_metric_bindings": [
                row for row in complete["plan_metric_bindings"] if row["plan_id"] != 101]}
            chunk = next(rule for rule in DEMO_RULES if rule[0] == RULE_CHUNK)
            rule = {**dict(zip(("chunk_id", "rule_id", "version", "title", "excerpt"), chunk, strict=True)),
                    "source": SOURCE}
            frozen = {"message": PROMPT, "plans": [101, 103], "dates": CASE.dates["new"], "rule": rule,
                      "complete": {"fixture": complete, "sql": oracle, "target": 103, "proposals": 1},
                      "missing": {"fixture": missing, "target": None, "proposals": 0,
                                  "gaps": {"101": "plan_history_not_mapped"},
                                  "103_metrics": oracle["metrics"]["103"]}}
            if suite:
                frozen = suite.freeze(output, sql, frozen)
            write(output / "frozen.json", frozen)
            if mode == "preflight":
                if suite and hasattr(suite, "apply_fixture"):
                    return
                sql("DELETE FROM plan_metric_bindings WHERE plan_id=101")
                assert fixture_rows(sandbox) == missing
                return
            if mode == "live":
                config = settings.llm_config()
                assert (config.provider, config.model, config.base_url) == ("glm", t5.MODEL, t5.ENDPOINT)
                assert config.api_key, "GLM_API_KEY is unavailable"
                redactor.secrets.append(config.api_key)
                provider = llm_provider.ProviderFactory.create()
                stack.callback(provider._client.close)
                hooks = provider._client._client.event_hooks
                hooks["request"].append(audit.request)
                hooks["response"].append(audit.response)
                t6.install_transport_observer(stack, provider, audit)
            else:
                provider = suite.FixedModel(audit, replay) if suite else FixedModel(audit, replay)
                stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=provider))
            stack.callback(SharedSystemClient.clear_system_cache)
            stack.callback(lambda: [system.stop() for system in SharedSystemClient._identifier_to_system.values()])
            stack.enter_context(patch.object(settings, "PLAN_METRICS_DB_PATH", str(ARTIFACT)))
            stack.enter_context(patch.object(settings, "RAG_DB_PATH", str(Path(state) / "rag")))
            for module in (rag_knowledge, conversation_memory):
                stack.enter_context(patch.object(module, "BGEEmbedding", FixedReviewEmbedding))
            stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
            for name in ("TRACE_LOG_FILE", "FUNC_LOG_FILE", "ERROR_LOG_FILE"):
                stack.enter_context(patch.object(observability, name, output / (name.lower() + ".jsonl")))
            for module in (llm_provider, team_runtime):
                stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
            for module in (home_react, team_tools):
                stack.enter_context(patch.object(module, "trace_tool_call", audit.trace_tool(module.trace_tool_call)))
            for name in ("query_plan_metrics", "query_plan_detail"):
                stack.enter_context(patch.object(tools, name, audit.wrap_read(name, getattr(tools, name))))
            runtime = team_runtime.DelegationRun
            stack.enter_context(patch.object(runtime, "_record", audit.wrap_receipt(runtime._record)))
            stack.enter_context(patch.object(runtime, "accept_direct", audit.observe_direct(runtime.accept_direct)))
            stack.enter_context(patch.object(runtime, "_schedule", sync.schedule(runtime._schedule)))

            import fast_server

            seed_review_rules(rag_knowledge.get_rag_knowledge())
            if suite:
                suite.instrument(stack, audit)
            audit.enabled = mode == "live"
            with serve(fast_server.app, sync) as url:
                previous_fixture = "complete"
                for name in selected or CASES:
                    scenario = (frozen["cases"] | frozen["holdout"])[name] if suite else {"message": PROMPT, "fixture": name}
                    fixture_name = scenario["fixture"]
                    if suite and hasattr(suite, "apply_fixture"):
                        suite.apply_fixture(sql, frozen, fixture_name)
                    elif fixture_name != previous_fixture:
                        if fixture_name == "missing":
                            sql("DELETE FROM plan_metric_bindings WHERE plan_id=101")
                        else:
                            binding = next(row for row in complete["plan_metric_bindings"] if row["plan_id"] == 101)
                            sql("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, ?)", tuple(binding.values()))
                        previous_fixture = fixture_name
                    if not suite or not hasattr(suite, "apply_fixture"):
                        assert fixture_rows(sandbox) == frozen[fixture_name]["fixture"]
                    audit.case = name
                    sync.reset(name)
                    folder = output / name
                    folder.mkdir()
                    try:
                        sample = browser_round(url, folder, name, sandbox, password, message=scenario["message"],
                                               suite=getattr(suite, "SUITE", "TASK-037") if suite else None,
                                               ctr_plan_id=scenario.get("ctr_plan_id"))
                        report["cases"][name] = {"browser": "pass", "group": sample["conversation_id"],
                                                  "run": sample["sse"][-1]["run_id"]}
                    except Exception:
                        report["cases"][name] = {"browser": "fail"}
                        write(folder / "first-deviation.json", redactor.clean({"error": format_exc()}))
                    finally:
                        write(folder / "accepted-task-sheets.json", redactor.clean(sync.task_sheets))
                        write(folder / "receipts.json", redactor.clean([row for row in audit.receipts if row["case"] == name]))
                        reads = [{**row, "arguments": {key: value for key, value in row["arguments"].items()
                                                       if key != "connect"}}
                                 for row in audit.reads if row["case"] == name]
                        write(folder / "reads.json", redactor.clean(reads))
                        calls = audit.rows() if mode == "live" else provider.calls
                        write(folder / "sdk.json", redactor.clean([row for row in calls if row["case"] == name]))
                        if suite:
                            suite.export(folder, audit, name)
    finally:
        calls = audit.rows()
        report["sdk_attempts"] = len(calls)
        report["http_statuses"] = dict(Counter(str(row["response"].get("status")) for row in calls))
        report["storage_after"] = {str(path.relative_to(ROOT)): digest(path) for path in protected}
        write(output / "report.json", report)
        assert report["storage_after"] == before
    if suite:
        return int(not suite.audit_run(output, selected))
    from scripts.audit_task36 import audit_run
    return int(not audit_run(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true")
    modes.add_argument("--preflight", action="store_true")
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--ledger", type=Path)
    args = parser.parse_args()
    mode = "live" if args.live else "preflight" if args.preflight else "fixed"
    os.environ.update(PYTHON_DOTENV_DISABLED="1", AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH="", JWT_SECRET=token_urlsafe(48), ANONYMIZED_TELEMETRY="False",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if args.live:
        from dotenv import dotenv_values
        values = dotenv_values(ROOT / ".env")
        os.environ.update(LLM_PROVIDER="glm", GLM_MODEL=t5.MODEL)
        if values.get("GLM_API_KEY"):
            os.environ.setdefault("GLM_API_KEY", values["GLM_API_KEY"])
    else:
        os.environ.update(LLM_PROVIDER="mock", API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="", GLM_API_KEY="")
    return run(args.output_dir.resolve(), mode, args.replay, args.ledger.resolve() if args.ledger else None)


if __name__ == "__main__":
    raise SystemExit(main())
