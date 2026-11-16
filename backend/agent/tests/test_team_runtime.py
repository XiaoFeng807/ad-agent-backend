"""Exercise the standalone entry point with fixed completions and real tool scope checks."""

from contextlib import closing
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.agent.team_runtime import DelegationRun, delegate_tasks
from backend.config.config import settings
from backend.database import database
from backend.observability import LOG_DIR, get_trace


PLAN = "get_plan_detail"
DAILY = "get_daily_report_by_date"
METRICS = "get_plan_metrics_by_date"
USER_ID = 17
WINDOW = {"start_date": "2026-10-19", "end_date": "2026-10-19"}


def task(task_id, *, tools=(PLAN,), deps=(), refs=(), artifacts=()):
    return {"task_id": task_id, "objective": f"Collect evidence for {task_id}", "capabilities": tools,
            "dependencies": deps, "input_refs": refs, "artifact_refs": artifacts,
            "expected_output": "Evidence or a summary", "stop_condition": "Evidence collected or missing"}


def submit(run, *tasks):
    return delegate_tasks({"reason": "standalone regression", "tasks": tasks}, run=run)


def read_plan(plan_id, user_id):
    return {"id": plan_id, "user_id": user_id}


def call(name=PLAN, **args):
    return name, args


class FixedProvider:
    model = "fixed-delegation"

    def __init__(self, choose):
        self.choose = choose
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, messages, tools, **options):
        context = json.loads(messages[1]["content"])
        task_id = context["task"]["task_id"]
        evidence = [json.loads(msg["content"]) for msg in messages if msg["role"] == "tool"]
        self.requests.append({**context, "evidence": evidence, "tools": [row["function"]["name"] for row in tools or []]})
        selected = self.choose(task_id, evidence)
        tool_calls = [{"id": f"call-{len(evidence)}-{index}", "type": "function",
                       "function": {"name": name, "arguments": json.dumps(args)}}
                      for index, (name, args) in enumerate(selected)]
        message = SimpleNamespace(tool_calls=tool_calls, content=None if tool_calls else
                                  json.dumps({"status": "succeeded", "output": task_id}))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class TeamRuntimeTests(unittest.TestCase):
    def run_handle(self, choose, *, functions=None, allowed=(PLAN,), **budgets):
        provider = FixedProvider(choose)
        return DelegationRun(user_id=USER_ID, provider=provider, allowed_capabilities=allowed,
                             functions={PLAN: read_plan} if functions is None else functions, **budgets), provider

    def record(self, result, **details):
        directory = os.environ.get("AD_TEAM_TEST_EVIDENCE")
        if directory:
            report = {"test_id": self.id(), "result": result, "trace": get_trace(result["trace_id"]), **details}
            (Path(directory) / f"{self._testMethodName}.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8",
            )

    def test_parallel_workers_reclaim_before_selective_dependency_handoff(self):
        parallel_reads = Barrier(2, timeout=5)

        def read(plan_id, user_id):
            parallel_reads.wait()
            return read_plan(plan_id, user_id)

        def choose(task_id, evidence):
            return [call(plan_id={"left": 101, "right": 102}[task_id])] if task_id != "join" and not evidence else []

        run, provider = self.run_handle(choose, functions={PLAN: read}, concurrency=2)
        result = submit(run, task("join", tools=(), deps=("left", "right"), refs=("left",)), task("left"), task("right"))
        self.assertEqual(result["states"], {"join": "succeeded", "left": "succeeded", "right": "succeeded"})
        reads = {row["task_id"]: row for row in result["receipts"] if row["source"] == PLAN}
        self.assertEqual({key: row["result"]["id"] for key, row in reads.items()}, {"left": 101, "right": 102})
        self.assertNotEqual(reads["left"]["artifact_id"], reads["right"]["artifact_id"])
        self.assertEqual(run.get_artifact(reads["right"]["artifact_id"]), reads["right"])
        join = next(row for row in provider.requests if row["task"]["task_id"] == "join")
        self.assertEqual(set(join["inputs"]), {"left"})
        self.assertIn(reads["left"], join["inputs"]["left"])
        self.assertEqual(join["tools"], [])
        lifecycle = [(row["action"], row["detail"]) for row in get_trace(result["trace_id"])["steps"]
                     if row["stage"] == "worker"]
        self.assertEqual({action for action, _ in lifecycle[:2]}, {"created"})
        self.assertLess(lifecycle.index(("reclaimed", "left")), lifecycle.index(("created", "join")))
        self.assertLess(lifecycle.index(("reclaimed", "right")), lifecycle.index(("created", "join")))
        self.record(result, join_context=join)

    def test_append_exact_artifact_reuses_board_and_enforces_total_task_budget(self):
        choose = lambda name, rows: [call(plan_id=101)] if name == "upstream" and not rows else []
        run, provider = self.run_handle(choose, task_limit=2)
        first = submit(run, task("upstream"))
        receipt = next(row for row in first["receipts"] if row["source"] == PLAN)
        exact = {"task_id": "upstream", "artifact_id": receipt["artifact_id"]}
        with self.assertRaisesRegex(ValueError, "invalid_input_reference"):
            submit(run, task("rejected", tools=(), deps=("upstream",), artifacts=({**exact, "artifact_id": "unknown"},)))
        with self.assertRaisesRegex(ValueError, "duplicate_task_id"):
            submit(run, task("upstream"))
        second = submit(run, task("append", tools=(), deps=("upstream",), artifacts=(exact,)))
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["batch_task_ids"], ["append"])
        self.assertEqual(provider.requests[-1]["inputs"], {"upstream": [receipt]})
        self.assertEqual(second["remaining"]["tool"], first["remaining"]["tool"])
        self.assertEqual(second["remaining"]["model"], first["remaining"]["model"] - 1)
        with self.assertRaisesRegex(ValueError, "task_budget_exhausted"):
            submit(run, task("overflow", tools=()))
        other, _ = self.run_handle(choose)
        with self.assertRaises(KeyError):
            other.get_artifact(receipt["artifact_id"])
        self.record(second, first_trace_id=first["trace_id"], appended_context=provider.requests[-1])

    def test_graph_and_capability_validation_precede_any_model_call(self):
        run, provider = self.run_handle(lambda *_: [])
        with self.assertRaisesRegex(ValueError, "cyclic_dependency"):
            submit(run, task("a", deps=("b",)), task("b", deps=("a",)))
        with self.assertRaisesRegex(ValueError, "unauthorized_capability"):
            submit(run, task("a", tools=("update_daily_budget",)))
        self.assertEqual(provider.requests, [])

    def test_forged_identity_and_unlisted_tools_do_not_reach_the_tool_functions(self):
        def choose(_, evidence):
            if not evidence:
                return [call(plan_id=9, user_id=99), call("update_daily_budget", plan_id=9)]
            return [call(plan_id=9)] if len(evidence) == 2 else []

        actual = []
        def read(**args):
            actual.append(args)
            return read_plan(**args)

        run, provider = self.run_handle(choose, functions={PLAN: read})
        result = submit(run, task("a"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(actual, [{"plan_id": 9, "user_id": USER_ID}])
        self.assertEqual([row["error_code"] for row in provider.requests[-1]["evidence"][:2]],
                         ["invalid_tool_arguments", "unauthorized_tool"])

    def test_real_sqlite_tool_scope_filters_foreign_plans_reports_and_metrics(self):
        def choose(name, evidence):
            calls = {"owned": call(plan_id=101), "foreign": call(plan_id=102),
                     "reports": call(DAILY, **WINDOW), "metrics": call(METRICS, plan_id=102, **WINDOW)}
            return [calls[name]] if not evidence else []

        with TemporaryDirectory(dir=LOG_DIR) as fixture, patch.object(database, "DB_PATH", Path(fixture) / "business.sqlite3"), \
                patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", ""), patch.object(settings, "PLAN_METRICS_DB_PATH", ""):
            database.init_db()
            with closing(database.get_db()) as conn, conn:
                conn.executemany("INSERT INTO ad_accounts (id, account_name, platform, user_id) VALUES (?, ?, 'demo', ?)",
                                 [(101, "owned account", USER_ID), (102, "foreign account", 18)])
                conn.executemany("INSERT INTO ad_plans (id, plan_name, user_id, account_id) VALUES (?, ?, ?, ?)",
                                 [(101, "owned plan", USER_ID, 102), (102, "foreign plan", 18, 102)])
                conn.executemany("INSERT INTO daily_reports (report_date, user_id, impressions) VALUES (?, ?, ?)",
                                 [(WINDOW["start_date"], USER_ID, 1500), (WINDOW["start_date"], 18, 9876)])
            provider = FixedProvider(choose)
            run = DelegationRun(user_id=USER_ID, provider=provider, allowed_capabilities=(PLAN, DAILY, METRICS))
            result = submit(run, task("owned"), task("foreign"), task("reports", tools=(DAILY,)),
                            task("metrics", tools=(METRICS,)))
        self.assertEqual(result["states"], {"owned": "succeeded", "foreign": "failed", "reports": "succeeded", "metrics": "failed"})
        reads = {row["task_id"]: row for row in result["receipts"] if row["source"] in (PLAN, DAILY, METRICS)}
        self.assertIsNone(reads["owned"]["result"]["account_name"])
        self.assertNotIn("user_id", reads["owned"]["result"])
        self.assertEqual([row["result"] for key, row in reads.items() if key in ("foreign", "metrics")], [None, None])
        self.assertEqual(reads["foreign"]["error_code"], "plan_not_found")
        self.assertEqual(reads["metrics"]["error_code"], "plan_not_found")
        self.assertEqual([row["impressions"] for row in reads["reports"]["result"]["reports"]], [1500])
        self.record(result)

    def test_transient_retry_uses_original_arguments_and_replays_accepted_items(self):
        actual = []
        def read(**args):
            actual.append(args)
            if len(actual) == 2:
                raise TimeoutError("simulated temporary read failure")
            return read_plan(**args)

        def choose(_, evidence):
            return [call(plan_id=101), call(plan_id=102)] if len(evidence) < 4 else []

        run, provider = self.run_handle(choose, functions={PLAN: read})
        result = submit(run, task("retry"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(actual, [{"plan_id": key, "user_id": USER_ID} for key in (101, 102, 102)])
        good, first, retried = [row for row in result["receipts"] if row["source"] == PLAN]
        self.assertEqual((good["status"], first["status"], retried["status"]), ("succeeded", "temporary_failure", "succeeded"))
        self.assertEqual(retried["arguments"], first["arguments"])
        self.assertEqual(retried["retry_of"], first["tool_call_id"])
        evidence = provider.requests[-1]["evidence"]
        self.assertEqual(evidence[:2], evidence[2:])
        self.assertEqual(result["remaining"]["tool"], 9)
        self.record(result)

    def test_permanent_missing_blocks_only_dependents_and_is_not_retried(self):
        actual = []
        def read(**args):
            actual.append(args)
            return {"success": False, "message": "not found"}

        run, provider = self.run_handle(lambda name, rows: [call(plan_id=404)] if name == "missing" and not rows else [],
                                        functions={PLAN: read})
        result = submit(run, task("blocked", tools=(), deps=("missing",), refs=("missing",)),
                        task("missing"), task("independent", tools=()))
        self.assertEqual(actual, [{"plan_id": 404, "user_id": USER_ID}])
        self.assertEqual(result["states"], {"missing": "failed", "blocked": "blocked", "independent": "succeeded"})
        self.assertNotIn("blocked", [row["task"]["task_id"] for row in provider.requests])
        self.assertEqual({row["error_code"] for row in result["receipts"] if row["status"] == "failed"},
                         {"plan_not_found", "dependency_failed"})
        self.record(result)

    def test_cancel_drains_sync_read_and_rejects_late_receipts_without_new_calls(self):
        entered, release = Event(), Event()
        actual, results = [], []

        def read(**args):
            actual.append(args)
            entered.set()
            release.wait(5)
            return read_plan(**args)

        run, provider = self.run_handle(lambda *_: [call(plan_id=1), call(plan_id=2)],
                                        functions={PLAN: read}, concurrency=1)
        thread = Thread(target=lambda: results.append(submit(run, task("active"), task("queued"))), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(ValueError, "batch_in_progress"):
                submit(run, task("overlapping"))
            run.cancel()
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        result, = results
        self.assertEqual(result["states"], {"active": "cancelled", "queued": "cancelled"})
        self.assertEqual(result["stop_code"], "run_cancelled")
        self.assertEqual(result["receipts"], [])
        self.assertEqual(actual, [{"plan_id": 1, "user_id": USER_ID}])
        self.assertEqual(len(provider.requests), 1)
        steps = get_trace(result["trace_id"])["steps"]
        late = next(i for i, row in enumerate(steps) if row["stage"] == "tool_result" and row["error_code"] == "expired_receipt")
        reclaimed = next(i for i, row in enumerate(steps) if row["action"] == "reclaimed")
        self.assertLess(late, reclaimed)
        with self.assertRaisesRegex(ValueError, "run_cancelled"):
            submit(run, task("later"))
        self.record(result)

    def test_shared_call_budgets_apply_to_later_batches(self):
        run, provider = self.run_handle(lambda _, rows: [] if rows else [call(plan_id=101)], model_limit=4, tool_limit=1)
        first = submit(run, task("first"))
        second = submit(run, task("second"))
        third = submit(run, task("third"))
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["remaining"]["tool"], 0)
        self.assertEqual(len(provider.requests), 4)
        self.assertEqual({row["error_code"] for row in third["receipts"] if row["status"] == "failed"},
                         {"budget_exhausted_or_cancelled", "model_budget_exhausted"})

    def test_deadline_rejects_late_model_reply_before_any_tool_is_started(self):
        entered, release = Event(), Event()

        def choose(*_):
            entered.set()
            release.wait(5)
            return [call(plan_id=101)]

        run, _ = self.run_handle(choose, seconds=1, functions={PLAN: lambda **_: self.fail("late tool executed")})
        results = []
        thread = Thread(target=lambda: results.append(submit(run, task("late-model"))), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            release.wait(1.1)
        finally:
            release.set()
            thread.join(5)
        result, = results
        self.assertEqual(result["stop_code"], "deadline_exceeded")
        self.assertEqual(result["states"], {"late-model": "cancelled"})
        self.assertEqual(result["receipts"], [])
        self.assertEqual(result["remaining"]["tool"], 12)
        self.record(result)
