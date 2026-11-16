"""Offline regression for the natural-language SSE and approval contract."""

from contextlib import closing
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from backend import observability
from backend.agent import natural_diagnosis
from backend.agent.agent_pool import AgentPool
from backend.agent.llm_provider import ProviderFactory
from backend.agent.tests.test_ad_diagnosis_eval import StatefulDiagnosisModel
from backend.agent.tests.test_home_sse_tracing import tool_call
from backend.agent.tests.test_plan_metrics import synthetic_artifact
from backend.config.config import settings
from backend.database import database
from backend.database.diagnosis_pauses import PAUSE_RULE_ID, create_pause_proposal, register_sandbox_target
from backend.rag import rag_knowledge
from backend.rag.review_rule_demo import DEMO_RULES, seed_review_rules
from scripts.eval_review_retrieval import FixedReviewEmbedding


MESSAGE = "帮我找出最近点击率下滑最明显、值得暂停复核的一条投放计划。"
BODY = {"message": MESSAGE, "diagnosis": {"mode": "natural"}}


class ReceiptDrivenModel(StatefulDiagnosisModel):
    def __init__(self):
        super().__init__()
        self.wrong_nomination = False
        self.try_foreign = False
        self.after_submission = None
        self.query_limit = None
        self.inputs_by_role = []

    def create(self, messages, tools=None, tool_choice=None, **kwargs):
        tool, = [entry["function"]["name"] for entry in tools]
        self.inputs_by_role.append((tool, list(messages), tool_choice))
        tasks = json.loads(messages[1]["content"])["required_queries"]
        received = [msg for msg in messages if msg["role"] == "tool"]
        assert [msg["tool_call_id"] for msg in received] == [
            natural_diagnosis.tool_call_fields(call)[0] for msg in messages if msg["role"] == "assistant"
            for call in msg["tool_calls"]
        ]
        if tool_choice == "none":
            return self.response("已核对回执。", [])
        if tool == natural_diagnosis.METRICS:
            if self.wrong_nomination:
                return self.response(None, [tool_call("duplicate", name="submit_comparison", plan_id=102)])
            delivered = [json.loads(msg["content"]).get("arguments") for msg in received]
            tasks = [args for args in tasks if args not in delivered][:self.query_limit]
        calls = [tool_call("duplicate", name=tool, **args) for args in reversed(tasks)]
        if self.try_foreign and tool == natural_diagnosis.METRICS:
            calls.append(tool_call("duplicate", name=tool, **{**tasks[0], "plan_id": 201}))
        return self.response(None, calls)


class NaturalDiagnosisFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.business_db = str(directory / "sandbox.sqlite3")
        _, self.artifact, manifest = synthetic_artifact(directory, ((500, 50), (500, 50), (500, 30), (500, 30)))
        self.enterContext(patch.object(settings, "PLAN_METRICS_DB_PATH", str(self.artifact)))
        self.enterContext(patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", self.business_db))
        self.enterContext(patch.object(settings, "JWT_SECRET", token_urlsafe(32)))
        self.enterContext(patch.object(settings, "RAG_DB_PATH", str(directory / "chroma")))
        self.enterContext(patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding))
        self.addCleanup(rag_knowledge._open_knowledge.cache_clear)
        self.enterContext(patch.object(observability, "TRACE_LOG_FILE", directory / "traces.jsonl"))
        seed_review_rules(self.enterContext(closing(rag_knowledge.get_rag_knowledge())))
        with closing(sqlite3.connect(self.artifact)) as conn, conn:
            conn.execute("""INSERT INTO plan_daily_metrics SELECT 'second', metric_date, impressions,
                         CASE WHEN metric_date BETWEEN '2026-10-03' AND '2026-10-04'
                              THEN 46 ELSE clicks END FROM plan_daily_metrics""")
            conn.executemany("""INSERT INTO plan_daily_metrics SELECT ?, metric_date, impressions,
                             CASE WHEN metric_date BETWEEN '2026-10-03' AND '2026-10-04'
                                  THEN ? ELSE clicks END
                             FROM plan_daily_metrics WHERE source_plan_id='fixture-campaign'""",
                             [("third", 48), ("foreign", 5)])
        database.init_db()
        with closing(database.get_db()) as conn, conn:
            conn.execute("INSERT INTO users(id, username, password) VALUES (17, 'owner', ?)",
                         (database.hash_password("demo-password"),))
            conn.execute("INSERT INTO users(id, username, password) VALUES (18, 'other', 'unused')")
            conn.executemany("INSERT INTO ad_plans(id, plan_name, user_id) VALUES (?, 'fixture', ?)",
                             [(101, 17), (102, 17), (103, 17), (201, 18)])
            conn.executemany("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, ?)",
                             [(plan, manifest["source_id"], manifest["data_version"], campaign)
                              for plan, campaign in [(101, "fixture-campaign"), (102, "second"),
                                                     (103, "third"), (201, "foreign")]])
        for owner, plan in [(17, 101), (17, 102), (18, 201)]:
            register_sandbox_target(owner, plan)
        self.model = ReceiptDrivenModel()
        compare = natural_diagnosis.NaturalDiagnosisExecution.compare

        def compare_then_mutate(execution, windows, call_id):
            result = compare(execution, windows, call_id)
            if self.model.after_submission:
                self.model.after_submission()
            return result

        self.enterContext(patch.object(natural_diagnosis.NaturalDiagnosisExecution, "compare", compare_then_mutate))
        with patch.object(ProviderFactory, "create", return_value=self.model):
            server = importlib.import_module("fast_server")
            self.enterContext(patch.object(server, "agent_pool", AgentPool(min_size=1, max_size=1)))
        self.client = await self.enterAsyncContext(AsyncClient(transport=ASGITransport(app=server.app),
                                                              base_url="http://test"))
        login = await self.client.post("/api/login", json={"username": "owner", "password": "demo-password"})
        self.client.headers["authorization"] = "Bearer " + login.json()["data"]["token"]

    async def diagnose(self):
        response = await self.client.post("/api/chat/stream", json=BODY)
        self.assertEqual(response.status_code, 200)
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]

    def states(self):
        with closing(database.get_db()) as conn:
            return dict(conn.execute("SELECT id, status FROM ad_plans"))

    def action_counts(self):
        with closing(database.get_db()) as conn:
            return [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in
                    ("diagnosis_review_proposals", "diagnosis_pause_proposals", "diagnosis_reviews",
                     "diagnosis_pause_receipts")]

    async def confirm_and_read(self, done):
        proposal = done["review_proposal"]
        confirmation = await self.client.post(f"/api/diagnosis/review-proposals/{proposal['action_id']}/confirm")
        self.assertEqual(confirmation.status_code, 200)
        review = confirmation.json()["data"]
        response = await self.client.get(f"/api/diagnosis/reviews/{review['review_id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], review)
        self.assertEqual({key: review[key] for key in proposal}, proposal)
        self.assertEqual(review["source_trace_id"], done["trace_id"])
        return review


class NaturalRequestTests(NaturalDiagnosisFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        with closing(sqlite3.connect(self.artifact)) as conn, conn:
            conn.execute("""INSERT OR REPLACE INTO plan_daily_metrics
                         SELECT source_plan_id, date(metric_date, '+4 days'), impressions, clicks
                         FROM plan_daily_metrics WHERE metric_date <= '2026-10-04'""")
            conn.execute("UPDATE plan_daily_metrics SET clicks=50 WHERE metric_date <= '2026-10-04'")
            conn.execute("""UPDATE plan_daily_metrics SET clicks=CASE source_plan_id
                         WHEN 'fixture-campaign' THEN 30 WHEN 'second' THEN 40 ELSE 45 END
                         WHERE metric_date BETWEEN '2026-10-05' AND '2026-10-08'""")
            manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
            manifest["complete_end_date"] = "2026-10-08"
            conn.execute("UPDATE metadata SET manifest=?", (json.dumps(manifest),))

    async def run_message(self, message):
        response = await self.client.post('/api/chat/stream', json={**BODY, 'message': message})
        self.assertEqual(response.status_code, 200)
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]

    async def test_current_request_controls_actual_queries_and_receipts_end_without_summary(self):
        query = natural_diagnosis.query_plan_metrics
        calls = []
        def record(**kwargs):
            calls.append({key: kwargs[key] for key in ('plan_id', 'start_date', 'end_date')})
            return query(**kwargs)
        create = self.model.create
        def no_summary(*args, **kwargs):
            if kwargs.get('tool_choice') == 'none':
                raise TimeoutError('unused summary must not be called')
            return create(*args, **kwargs)
        with patch.object(natural_diagnosis, 'query_plan_metrics', side_effect=record), patch.object(self.model, 'create', side_effect=no_summary):
            for message, windows in [(MESSAGE, [('2026-10-01', '2026-10-04'), ('2026-10-05', '2026-10-08')]),
                                     ('帮我找出最近三天和之前三天点击率下滑最明显、值得暂停复核的一条投放计划。',
                                      [('2026-10-03', '2026-10-05'), ('2026-10-06', '2026-10-08')])]:
                calls.clear()
                self.model.inputs_by_role.clear()
                events = await self.run_message(message)
                self.assertEqual([x['type'] for x in events], ['thinking', 'text', 'done'])
                self.assertEqual({(x['start_date'], x['end_date']) for x in calls}, set(windows))
                self.assertEqual(len(calls), 6)
                self.assertEqual(len(self.model.inputs_by_role), 2)
                self.assertTrue(all(item[2] == 'auto' for item in self.model.inputs_by_role))
                self.assertIn('comparison', events[1])

    async def test_unrelated_and_insufficient_window_stop_before_evidence(self):
        query = natural_diagnosis.query_plan_metrics
        with patch.object(natural_diagnosis, 'query_plan_metrics', wraps=query) as metrics:
            for message in ['帮我写一封请假邮件', '帮我找出最近五天和之前五天点击率下滑最明显、值得暂停复核的一条投放计划。',
                            '帮我找出最近三天点击率下滑最明显、值得暂停复核的一条投放计划。']:
                self.model.inputs_by_role.clear()
                events = await self.run_message(message)
                self.assertEqual([x['type'] for x in events], ['thinking', 'text', 'done'])
                self.assertTrue(events[1]['evidence_gaps'])
                self.assertNotIn('review_proposal', events[-1])
                self.assertEqual(self.model.inputs_by_role, [])
            metrics.assert_not_called()
            self.assertEqual(self.action_counts(), [0, 0, 0, 0])


    async def test_followup_isolated_snapshot_and_original_approval(self):
        from backend.database.diagnosis_tasks import accept_diagnosis
        from backend.agent.diagnosis import NaturalDiagnosisRequest
        accept_diagnosis(17, NaturalDiagnosisRequest(mode="natural"), "OTHER_TASK_SENTINEL")
        with closing(database.get_db()) as conn, conn:
            conn.execute("INSERT INTO conversations (user_id, role, content) VALUES (17, 'user', 'ORDINARY_CHAT_SENTINEL')")
        first_events = await self.diagnose()
        first = first_events[-1]
        first_proposal = first["review_proposal"]
        with closing(sqlite3.connect(self.artifact)) as conn, conn:
            conn.execute("UPDATE plan_daily_metrics SET clicks=CASE source_plan_id WHEN 'fixture-campaign' THEN 48 WHEN 'second' THEN 20 ELSE 45 END WHERE metric_date BETWEEN '2026-10-06' AND '2026-10-08'")
        self.model.inputs_by_role.clear()
        response = await self.client.post("/api/chat/stream", json={"message": "继续这次比较，改成最近三天和之前三天", "diagnosis": {"mode": "natural", "task_id": first["task_id"], "source_turn_id": first["turn_id"]}})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        second = events[-1]
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertNotEqual(second["turn_id"], first["turn_id"])
        self.assertNotEqual(second["trace_id"], first["trace_id"])
        self.assertNotEqual(events[1]["plan_id"], first_proposal["plan_id"])
        for tool, messages, _ in self.model.inputs_by_role:
            queries = json.loads(messages[1]["content"])["required_queries"]
            expected = {"plan_id", "start_date", "end_date"} if tool == natural_diagnosis.METRICS else {"query"}
            self.assertTrue(all(set(query) == expected for query in queries))
            wire = json.dumps(messages, default=str)
            for forbidden in (first["trace_id"], "OTHER_TASK_SENTINEL", "ORDINARY_CHAT_SENTINEL", "2026-10-01", json.dumps(first_events[1]["metrics"], ensure_ascii=False)):
                self.assertNotIn(forbidden, wire)
        review = await self.confirm_and_read(first)
        self.assertEqual(review["baseline"], first_proposal["baseline"])
        self.assertEqual(review["comparison"], first_proposal["comparison"])
        history = (await self.client.get("/api/diagnosis/tasks/" + first["task_id"])).json()["data"]
        self.assertEqual(history["turns"][1]["snapshot"]["source_turn_id"], first["turn_id"])

    async def test_followup_rejects_unavailable_and_cross_task_sources(self):
        first = (await self.diagnose())[-1]
        other = (await self.diagnose())[-1]
        from backend.database.diagnosis_tasks import accept_diagnosis, save_diagnosis_reply
        from backend.agent.diagnosis import NaturalDiagnosisRequest
        params = NaturalDiagnosisRequest(mode="natural", task_id=first["task_id"], source_turn_id=first["turn_id"])
        pending = accept_diagnosis(17, params, "pending")
        no_comparison = accept_diagnosis(17, params, "no comparison")
        save_diagnosis_reply(17, no_comparison, "no comparison", "empty-trace", {})
        before = self.action_counts()
        self.model.inputs_by_role.clear()
        for task_id, source, expected in [(first["task_id"], other["turn_id"], 400), (first["task_id"], pending.turn_id, 400), (first["task_id"], no_comparison.turn_id, 400), ("unknown", first["turn_id"], 404)]:
            response = await self.client.post("/api/chat/stream", json={"message": "继续这次比较，改成最近三天和之前三天", "diagnosis": {"mode": "natural", "task_id": task_id, "source_turn_id": source}})
            self.assertEqual(response.status_code, expected)
        self.assertEqual(self.model.inputs_by_role, [])
        self.assertEqual(self.action_counts(), before)


    async def test_cross_user_and_out_of_order_turns_preserve_summary(self):
        from backend.database.diagnosis_tasks import accept_diagnosis, save_diagnosis_reply
        from backend.agent.diagnosis import NaturalDiagnosisRequest
        events = await self.diagnose()
        first, evidence = events[-1], events[1]
        params = NaturalDiagnosisRequest(mode="natural", task_id=first["task_id"], source_turn_id=first["turn_id"])
        self.assertIsNone(accept_diagnosis(18, params, "foreign"))
        barrier = Barrier(2)
        def accept_parallel(label):
            barrier.wait()
            return accept_diagnosis(17, params, label, trace_id=label + "-trace")
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(accept_parallel, ("a", "b")))
        older, newer = sorted(runs, key=lambda run: run.turn_id)
        revision = [{"plan_id": item["plan_id"], **{key: item["metrics"]["baseline"][key] for key in ("source_id", "data_version", "source_plan_id")}} for item in sorted(evidence["comparison"]["candidates"], key=lambda item: item["plan_id"])]
        saved = {**evidence, "candidate_revision": revision}
        changed = {**saved, "baseline": {"start_date": "2026-10-03", "end_date": "2026-10-05"}}
        save_diagnosis_reply(17, newer, "newer", "newer-trace", changed)
        save_diagnosis_reply(17, older, "older", "older-trace", saved)
        history = (await self.client.get("/api/diagnosis/tasks/" + first["task_id"])).json()["data"]
        self.assertEqual(history["baseline"], changed["baseline"])
        self.assertEqual(history["turns"][-2]["snapshot"]["source_trace_id"], "older-trace")
        self.assertEqual(history["turns"][-1]["snapshot"]["source_trace_id"], "newer-trace")


class NaturalDiagnosisTests(NaturalDiagnosisFixture):
    async def test_swapped_metrics_change_the_selected_plan_and_only_approved_plan_pauses(self):
        first = (await self.diagnose())[-1]
        self.assertEqual(first["review_proposal"]["plan_id"], 101)
        self.assertNotIn("pause_proposal", first)
        with closing(sqlite3.connect(self.artifact)) as conn, conn:
            conn.execute("""UPDATE plan_daily_metrics SET clicks=CASE source_plan_id
                         WHEN 'fixture-campaign' THEN 46 ELSE 30 END
                         WHERE metric_date BETWEEN '2026-10-03' AND '2026-10-04'
                         AND source_plan_id IN ('fixture-campaign', 'second')""")
        second = (await self.diagnose())[-1]
        self.assertEqual(second["review_proposal"]["plan_id"], 102)
        self.assertNotIn("pause_proposal", second)
        self.assertEqual(self.states(), dict.fromkeys([101, 102, 103, 201], "active"))
        await self.confirm_and_read(second)

        # Recreate a TASK-022 natural pause proposal to exercise the existing sandbox path.
        legacy_review = second["review_proposal"].copy()
        with closing(database.get_db()) as conn, conn:
            for table, field in (("diagnosis_review_proposals", "turn_id"), ("diagnosis_turns", "id")):
                conn.execute(f"UPDATE {table} SET snapshot=json_remove(snapshot, '$.candidate_scope') WHERE {field}=?",
                             (legacy_review["turn_id"],))
            legacy_review.pop("candidate_scope")
            proposal = create_pause_proposal(conn, 17, legacy_review)
        action = f"/api/diagnosis/pause-actions/{proposal['action_id']}"
        receipt = (await self.client.post(action + "/confirm")).json()["data"]
        self.assertEqual((await self.client.get(action)).json()["data"], {"receipt": receipt, "actual_status": "paused"})
        self.assertEqual((await self.client.post(action + "/confirm")).json()["data"], receipt)
        self.assertEqual(self.states(), {101: "active", 102: "paused", 103: "active", 201: "active"})
        task = (await self.client.get(f"/api/diagnosis/tasks/{second['task_id']}")).json()["data"]
        self.assertEqual(task["turns"][0]["pause"]["state"], "approved")

    async def test_missing_daily_coverage_yields_no_proposals_and_recovers_history(self):
        with closing(sqlite3.connect(self.artifact)) as conn, conn:
            conn.execute("DELETE FROM plan_daily_metrics WHERE source_plan_id='second' AND metric_date='2026-10-03'")
        _, evidence, done = await self.diagnose()
        self.assertIn("计划 #102 在 2026-10-03 至 2026-10-04 缺报：2026-10-03", evidence["evidence_gaps"])
        self.assertEqual(self.action_counts(), [0, 0, 0, 0])
        task = (await self.client.get(f"/api/diagnosis/tasks/{done['task_id']}")).json()["data"]
        self.assertEqual(task["mode"], "natural")
        self.assertEqual(task["turns"][0]["snapshot"]["evidence_gaps"], evidence["evidence_gaps"])
        self.assertIsNone(task["turns"][0]["pause"])
        self.assertEqual(set(self.states().values()), {"active"})

    async def test_wrong_nomination_cannot_create_a_proposal(self):
        self.model.wrong_nomination = True
        _, evidence, done = await self.diagnose()
        self.assertIn("缺少成功的指标回执", evidence["content"])
        self.assertIn("unknown_tool", {step.get("error_code") for step in observability.get_trace(done["trace_id"])["steps"]})
        self.assertNotIn("pause_proposal", done)
        self.assertEqual(self.action_counts(), [0, 0, 0, 0])

    async def test_foreign_query_is_rejected_and_unregistered_plan_is_not_ranked(self):
        # Keep the TASK-022 case ID while asserting the revised candidate contract.
        self.model.try_foreign = True
        _, evidence, done = await self.diagnose()
        self.assertEqual([item["plan_id"] for item in evidence["comparison"]["candidates"]], [101, 102, 103])
        trace = observability.get_trace(done["trace_id"])
        self.assertIn("candidate_out_of_scope", {step.get("error_code") for step in trace["steps"]})

    async def test_candidate_status_change_before_commit_invalidates_the_comparison(self):
        def change_candidate():
            with closing(database.get_db()) as conn, conn:
                conn.execute("UPDATE ad_plans SET status='paused' WHERE id=102")
        self.model.after_submission = change_candidate
        events = await self.diagnose()
        self.assertEqual([event["type"] for event in events], ["thinking", "error"])
        self.assertEqual(events[-1]["error_code"], "candidate_revision_changed")
        self.assertEqual(self.action_counts(), [0, 0, 0, 0])

    async def test_natural_mode_requires_text_and_refuses_seeded_parameters(self):
        for body in ({"diagnosis": {"mode": "natural"}}, {**BODY, "message": " "},
                     {**BODY, "diagnosis": {"mode": "natural", "plan_id": 101}}):
            with self.subTest(body=body):
                self.assertEqual((await self.client.post("/api/chat/stream", json=body)).status_code, 422)


class NaturalReviewScopeTests(NaturalDiagnosisFixture):
    async def test_unregistered_first_creates_review_without_pause_sandbox(self):
        with closing(sqlite3.connect(self.artifact)) as conn, conn:
            conn.execute("""UPDATE plan_daily_metrics SET clicks=10 WHERE source_plan_id='third'
                         AND metric_date BETWEEN '2026-10-03' AND '2026-10-04'""")
        with patch.object(database, "DB_PATH", self.business_db), patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", ""):
            _, evidence, done = await self.diagnose()
            self.assertEqual(evidence["candidate_scope"]["plan_ids"], [101, 102, 103])
            self.assertEqual([item["plan_id"] for item in evidence["comparison"]["candidates"]], [103, 101, 102])
            self.assertNotIn("pause_proposal", done)
            self.assertEqual(self.action_counts(), [1, 0, 0, 0])
            review = await self.confirm_and_read(done)
            self.assertEqual(review["plan_id"], 103)
            self.assertEqual(self.action_counts(), [1, 0, 1, 0])
            self.assertEqual(self.states(), dict.fromkeys([101, 102, 103, 201], "active"))

    async def test_candidate_limit_refuses_partial_comparison(self):
        with closing(database.get_db()) as conn, conn:
            conn.executemany("INSERT INTO ad_plans(id, plan_name, user_id) VALUES (?, 'extra', 17)", [(104,), (105,)])
        _, evidence, done = await self.diagnose()
        self.assertEqual(evidence["candidate_scope"], {"kind": "owned_active_plans", "max_candidates": 4})
        self.assertIn("本轮未比较", evidence["content"])
        self.assertIn("缩小分析范围", evidence["content"])
        self.assertEqual(self.action_counts(), [0, 0, 0, 0])
        trace = observability.get_trace(done["trace_id"])
        self.assertEqual([step["action"] for step in trace["steps"] if step["stage"] == "tool_call"],
                         ["discover_analysis_candidates"])

    async def test_active_unmapped_candidate_blocks_comparison_but_inactive_plan_is_excluded(self):
        with closing(database.get_db()) as conn, conn:
            conn.execute("DELETE FROM plan_metric_bindings WHERE plan_id=103")
            conn.execute("INSERT INTO ad_plans(id, plan_name, user_id, status) VALUES (104, 'inactive', 17, 'paused')")
        _, evidence, _ = await self.diagnose()
        self.assertEqual(evidence["candidate_scope"]["plan_ids"], [101, 102, 103])
        self.assertTrue(any("#103" in gap and "plan_history_not_mapped" in gap for gap in evidence["evidence_gaps"]))
        self.assertEqual(self.action_counts(), [0, 0, 0, 0])

    async def test_source_binding_change_before_commit_invalidates_comparison(self):
        def change_binding():
            with closing(database.get_db()) as conn, conn:
                conn.execute("UPDATE plan_metric_bindings SET source_plan_id='foreign' WHERE plan_id=103")
        self.model.after_submission = change_binding
        events = await self.diagnose()
        self.assertEqual([event["type"] for event in events], ["thinking", "error"])
        self.assertEqual(events[-1]["error_code"], "candidate_revision_changed")
        self.assertEqual(self.action_counts(), [0, 0, 0, 0])

    async def test_candidate_state_or_artifact_version_change_before_review_confirmation_returns_conflict(self):
        done = (await self.diagnose())[-1]
        action = done["review_proposal"]["action_id"]
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE ad_plans SET status='paused' WHERE id=103")
        confirm_url = f"/api/diagnosis/review-proposals/{action}/confirm"
        self.assertEqual((await self.client.post(confirm_url)).status_code, 409)
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE ad_plans SET status='active' WHERE id=103")
        with closing(sqlite3.connect(self.artifact)) as conn, conn:
            manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
            conn.execute("UPDATE metadata SET manifest=?", (json.dumps({**manifest, "data_version": "changed"}),))
        self.assertEqual((await self.client.post(confirm_url)).status_code, 409)
        self.assertEqual(self.action_counts(), [1, 0, 0, 0])

    async def test_registration_change_does_not_invalidate_comparison_or_review(self):
        def unregister():
            with closing(database.get_db()) as conn, conn:
                conn.execute("DELETE FROM diagnosis_sandbox_targets WHERE plan_id=101")
        self.model.after_submission = unregister
        done = (await self.diagnose())[-1]
        self.assertEqual(done["review_proposal"]["plan_id"], 101)
        register_sandbox_target(17, 103)
        await self.confirm_and_read(done)
        self.assertEqual(self.action_counts(), [1, 0, 1, 0])


class NaturalMultiAgentTests(NaturalDiagnosisFixture):
    def steps(self, done, stage):
        return [step for step in observability.get_trace(done["trace_id"])["steps"] if step["stage"] == stage]

    async def test_requested_rule_wins_over_adjacent_version(self):
        with closing(rag_knowledge.get_rag_knowledge()) as rag:
            v1, v2 = [rag.get_chunk(row[0]) for row in DEMO_RULES[:2]]
        knowledge = SimpleNamespace(generate=lambda query: {"evidence": [v2, v1]},
                                    get_chunk=lambda chunk: v1)
        with patch.object(rag_knowledge, "get_rag_knowledge", return_value=knowledge):
            _, evidence, done = await self.diagnose()
        self.assertEqual(evidence["rule_evaluation"]["min_relative_decline_pct"], 20)
        self.assertEqual(evidence["citations"][0]["version"], "v1")
        self.assertIn("review_proposal", done)

    async def test_adjacent_unreadable_and_conflicting_rules_stop(self):
        with closing(rag_knowledge.get_rag_knowledge()) as rag:
            v1, v2 = [rag.get_chunk(row[0]) for row in DEMO_RULES[:2]]
        conflict = {**v1, "chunk_id": "conflict", "content": v2["content"]}
        for hits, readback, reason in [
            ([v2], None, "rule_version_unverified"),
            ([v1, v2], {**v1, "content": "changed"}, "rule_version_unverified"),
            ([v1, conflict], v1, "rule_version_conflict"),
        ]:
            with self.subTest(reason=reason):
                knowledge = SimpleNamespace(generate=lambda query: {"evidence": hits},
                                            get_chunk=lambda chunk: readback)
                with patch.object(rag_knowledge, "get_rag_knowledge", return_value=knowledge):
                    _, evidence, done = await self.diagnose()
                self.assertTrue(any(PAUSE_RULE_ID in gap and reason in gap for gap in evidence["evidence_gaps"]))
                self.assertNotIn("submit_comparison", [step["action"] for step in self.steps(done, "tool_call")])
                self.assertEqual(self.steps(done, "retry"), [])
        self.assertEqual(self.action_counts(), [0, 0, 0, 0])

    async def test_single_temporary_failure_retries_only_the_exact_window(self):
        query = natural_diagnosis.query_plan_metrics
        attempts = Counter()

        def fail_once(**kwargs):
            key = tuple(kwargs[name] for name in ("plan_id", "start_date", "end_date"))
            attempts[key] += 1
            if key == (102, "2026-10-03", "2026-10-04") and attempts[key] == 1:
                raise TimeoutError
            return query(**kwargs)

        with patch.object(natural_diagnosis, "query_plan_metrics", side_effect=fail_once):
            _, evidence, done = await self.diagnose()
        self.assertEqual(attempts.pop((102, "2026-10-03", "2026-10-04")), 2)
        self.assertEqual(list(attempts.values()), [1] * 5)
        self.assertIn("review_proposal", done)
        failed, recovered = [step for step in self.steps(done, "evidence_received")
                             if step.get("error_code") == "temporary_tool_failure" or step.get("retry_of")]
        self.assertEqual(recovered["retry_of"], failed["runtime_call_id"])
        self.assertEqual(recovered["parent_call_id"], failed["parent_call_id"])
        self.assertNotIn("tool_call_id", recovered)
        self.assertEqual(next(row for row in evidence["comparison"]["candidates"] if row["plan_id"] == 102)["metric_calls"]["current"],
                         recovered["runtime_call_id"])

    async def test_retry_failure_stops_and_reports_the_failed_window(self):
        query = natural_diagnosis.query_plan_metrics

        def always_fail(**kwargs):
            if (kwargs["plan_id"], kwargs["end_date"]) == (102, "2026-10-04"):
                raise TimeoutError
            return query(**kwargs)

        with patch.object(natural_diagnosis, "query_plan_metrics", side_effect=always_fail):
            _, evidence, done = await self.diagnose()
        self.assertEqual(len(self.steps(done, "retry")), 1)
        self.assertTrue(any("#102" in gap and "2026-10-04" in gap and "temporary_tool_failure" in gap
                            for gap in evidence["evidence_gaps"]))
        self.assertEqual(self.action_counts(), [0, 0, 0, 0])

    def add_fourth_candidate(self):
        with closing(sqlite3.connect(self.artifact)) as conn, conn:
            conn.execute("""INSERT INTO plan_daily_metrics SELECT 'fourth', metric_date, impressions,
                         CASE WHEN metric_date BETWEEN '2026-10-03' AND '2026-10-04'
                              THEN 49 ELSE clicks END
                         FROM plan_daily_metrics WHERE source_plan_id='fixture-campaign'""")
        with closing(database.get_db()) as conn, conn:
            conn.execute("INSERT INTO ad_plans(id, plan_name, user_id) VALUES (104, 'fourth', 17)")
            conn.execute("""INSERT INTO plan_metric_bindings SELECT 104, source_id, data_version, 'fourth'
                         FROM plan_metric_bindings WHERE plan_id=101""")

    async def test_four_candidates_complete_with_single_retry_at_tool_cap(self):
        self.add_fourth_candidate()
        query = natural_diagnosis.query_plan_metrics
        failed = False

        def fail_once(**kwargs):
            nonlocal failed
            if kwargs["plan_id"] == 104 and not failed:
                failed = True
                raise TimeoutError
            return query(**kwargs)

        with patch.object(natural_diagnosis, "query_plan_metrics", side_effect=fail_once):
            _, evidence, done = await self.diagnose()
        self.assertEqual(evidence["candidate_scope"]["plan_ids"], [101, 102, 103, 104])
        self.assertEqual(len(self.steps(done, "tool_call")), 12)
        self.assertEqual(len(self.steps(done, "dispatch")), 2)
        self.assertIn("review_proposal", done)

    async def test_tool_budget_stops_before_submission(self):
        self.add_fourth_candidate()
        with patch.object(natural_diagnosis, "DIAGNOSIS_CALL_LIMIT", 10):
            _, evidence, done = await self.diagnose()
        self.assertEqual(len(self.steps(done, "tool_call")), 10)
        self.assertEqual(self.steps(done, "guardrail")[-1]["error_code"], "diagnosis_call_limit")
        self.assertTrue(evidence["evidence_gaps"])
        self.assertEqual(self.action_counts(), [0, 0, 0, 0])

    async def test_model_budget_stops_when_agent_does_not_request_all_evidence(self):
        self.model.query_limit = 1
        _, evidence, done = await self.diagnose()
        model_calls = Counter(step["role"] for step in self.steps(done, "model_call"))
        self.assertEqual(model_calls, {"data_agent": 3, "knowledge_agent": 1})
        self.assertEqual(len(self.steps(done, "dispatch")), 2)
        self.assertTrue(evidence["evidence_gaps"])
        self.assertEqual(self.steps(done, "guardrail")[-1]["error_code"], "diagnosis_model_call_limit")
        self.assertEqual(self.action_counts(), [0, 0, 0, 0])

    def test_parallel_rounds_with_duplicate_sdk_ids_keep_out_of_order_receipts(self):
        with closing(database.get_db()) as conn, conn:
            conn.execute("INSERT INTO ad_plans(id, plan_name, user_id) VALUES (202, 'second owner', 18)")
            conn.execute("""INSERT INTO plan_metric_bindings SELECT 202, source_id, data_version, 'second'
                         FROM plan_metric_bindings WHERE plan_id=102""")
        execution_class = natural_diagnosis.NaturalDiagnosisExecution
        barrier = Barrier(2)
        ready = {owner: Event() for owner in (17, 18)}
        run_role = execution_class.run_role
        trace_receipt = execution_class.trace_receipt

        def wait_for_knowledge(execution, provider, model, budget, role, tool, tasks, dispatch):
            if role == "data_agent":
                barrier.wait(timeout=10)
                if not ready[execution.user_id].wait(timeout=10):
                    raise AssertionError("knowledge dispatch did not finish before data")
            return run_role(execution, provider, model, budget, role, tool, tasks, dispatch)

        def release_data(execution, receipt):
            trace_receipt(execution, receipt)
            if receipt.tool == natural_diagnosis.KNOWLEDGE:
                ready[execution.user_id].set()

        def run(owner):
            trace_id = observability.start_trace()
            execution = execution_class(owner, trace_id)
            result = execution.run(self.model, "fixed", None, MESSAGE)
            observability.end_trace(trace_id)
            return result, observability.get_trace(trace_id)["steps"]

        with (patch.object(execution_class, "run_role", wait_for_knowledge),
              patch.object(execution_class, "trace_receipt", release_data),
              ThreadPoolExecutor(max_workers=2) as pool):
            turns = list(pool.map(run, [17, 18]))
        for (result, steps), plans in zip(turns, ([101, 102, 103], [201, 202]), strict=True):
            self.assertEqual(result["candidate_scope"]["plan_ids"], plans)
            dispatches = {step["action"]: step["runtime_call_id"] for step in steps if step["stage"] == "dispatch"}
            received = [step for step in steps if step["stage"] == "evidence_received"]
            self.assertEqual(received[0]["action"], natural_diagnosis.KNOWLEDGE)
            self.assertEqual(len(received), 2 * len(plans) + 1)
            self.assertEqual({step["tool_call_id"] for step in received}, {"duplicate"})
            self.assertEqual(len({step["runtime_call_id"] for step in received}), len(received))
            self.assertTrue(all(step["parent_call_id"] == dispatches[step["action"]] for step in received))
            metric_ids = {step["runtime_call_id"] for step in received[1:]}
            self.assertEqual({call for row in result["comparison"]["candidates"]
                              for call in row["metric_calls"].values()}, metric_ids)
