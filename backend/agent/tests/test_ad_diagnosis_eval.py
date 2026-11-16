"""Run from the repo root with python -m backend.agent.tests.test_ad_diagnosis_eval."""

import asyncio
import json
import sqlite3
import unittest
from contextlib import closing
from copy import deepcopy
from fractions import Fraction
from unittest.mock import patch


from backend import observability
from backend.agent.agent_pool import AgentPool
from backend.agent.llm_provider import ProviderFactory
from backend.agent.tests.test_home_sse_tracing import (
    TOOL_CALLS,
    FixedModel,
    HomeSSEFixture,
    tool_call,
)
from backend.database import database
from backend.memory import memory_manager


PLAN_ID = "demo-plan-01"
WINDOWS = {
    "baseline": ("2026-10-19", "2026-10-25"),
    "current": ("2026-10-26", "2026-11-01"),
}
MESSAGE = (
    f"复盘广告计划 {PLAN_ID} 的点击率。基准窗 {' 至 '.join(WINDOWS['baseline'])}，"
    f"当前窗 {' 至 '.join(WINDOWS['current'])}。按复核规则给出建议。"
)
RULE_QUERY = "查找广告点击率下滑的人工复核规则"
RULE_ID = "ctr-review-v1#low-ctr"
BASELINE_COUNTS = (1000, 80)
EXPECTED_CALLS = {
    call_id: TOOL_CALLS[call_id] for call_id in ("call_data", "call_knowledge", "call_search")
} | {
    f"call_{window}": ("get_daily_report_by_date", "数据助手") for window in WINDOWS
}


class DiagnosisModel(FixedModel):
    """Pass the leaf receipts through the sub-agents and synthesize a test-only JSON reply."""

    def create(self, messages, tools=None, **kwargs):
        names = {tool["function"]["name"] for tool in tools or []}
        receipts = {
            message["tool_call_id"]: json.loads(message["content"])
            for message in messages if message["role"] == "tool"
        }
        self.receipts.update(receipts)

        if "query_data" in names:
            if receipts:
                data = json.loads(receipts["call_data"]["result"])
                knowledge = json.loads(receipts["call_knowledge"]["result"])
                return self.response(json.dumps(self.diagnose(data, knowledge), ensure_ascii=False), [])
            return self.response(None, [
                tool_call("call_data", query=messages[-1]["content"]),
                tool_call("call_knowledge", question=RULE_QUERY),
            ])
        if "get_daily_report_by_date" in names:
            return self.response(None, [
                tool_call(f"call_{window}", name="get_daily_report_by_date", start_date=start, end_date=end)
                for window, (start, end) in WINDOWS.items()
            ])
        if "search_knowledge" in names:
            return self.response(None, [tool_call("call_search", query=messages[-1]["content"])])
        if "call_search" in receipts:
            return self.response(receipts["call_search"], [])
        if receipts:
            data = {window: receipts[f"call_{window}"]["reports"][0] for window in WINDOWS}
            return self.response(json.dumps(data), [])
        if kwargs.get("max_tokens") == 500:
            return self.response('{"steps": []}', [])
        raise ValueError("unexpected offline model call")

    @staticmethod
    def diagnose(data, knowledge):
        baseline, current = data["baseline"], data["current"]
        baseline_ctr = baseline["clicks"] / baseline["impressions"] * 100 if baseline["impressions"] else None
        current_ctr = current["clicks"] / current["impressions"] * 100 if current["impressions"] else None
        decline = baseline_ctr - current_ctr if baseline_ctr is not None and current_ctr is not None else None
        recommendation = evidence_gap = None
        citations = []
        if decline is None:
            evidence_gap = "缺少有效曝光数据"
        elif not knowledge["sources"]:
            evidence_gap = "缺少规则依据"
        else:
            rule = json.loads(knowledge["context"])
            if min(baseline["impressions"], current["impressions"]) < rule["min_impressions"]:
                evidence_gap = "两窗曝光未满足最低样本量要求"
            elif baseline_ctr and decline / baseline_ctr * 100 >= rule["min_relative_decline_pct"]:
                recommendation = "建议人工复核"
                citations = knowledge["sources"]
        return {
            "baseline_ctr_pct": baseline_ctr,
            "current_ctr_pct": current_ctr,
            "decline_pp": decline,
            "citations": citations,
            "recommendation": recommendation,
            "evidence_gap": evidence_gap,
        }


class AdDiagnosisEval(HomeSSEFixture):
    model_class = DiagnosisModel

    def tool_overrides(self):
        return {"get_daily_report_by_date": self.query_data, "search_knowledge": self.query_knowledge}

    def query_data(self, start_date, end_date, user_id=None):
        if user_id != 17:
            raise ValueError("unexpected user scope")
        window_counts = {
            WINDOWS["baseline"]: BASELINE_COUNTS,
            WINDOWS["current"]: self.current_counts,
        }
        impressions, clicks = window_counts[(start_date, end_date)]
        return {"success": True, "reports": [{
            "plan_id": PLAN_ID, "report_date": start_date, "impressions": impressions, "clicks": clicks,
        }]}

    def query_knowledge(self, query):
        if query != RULE_QUERY:
            raise ValueError("unexpected knowledge query")
        if self.rule_found:
            return json.dumps({
                "context": json.dumps({"min_impressions": 1000, "min_relative_decline_pct": 20}),
                "sources": [RULE_ID],
            })
        return json.dumps({"context": "", "sources": []})

    async def test_01_ctr_drop_rule_match(self):
        await self.evaluate(
            (1000, 50), rule_found=True, citations=[RULE_ID], recommendation="建议人工复核", evidence_gap=None,
        )

    async def test_02_insufficient_impressions(self):
        await self.evaluate(
            (0, 0), rule_found=True, citations=[], recommendation=None, evidence_gap="缺少有效曝光数据",
        )

    async def test_03_rule_missing(self):
        await self.evaluate(
            (1000, 50), rule_found=False, citations=[], recommendation=None, evidence_gap="缺少规则依据",
        )

    async def evaluate(self, current_counts, *, rule_found, citations, recommendation, evidence_gap):
        self.current_counts = current_counts
        self.rule_found = rule_found
        await asyncio.wait_for(self.request(message=MESSAGE), timeout=15)

        with self.subTest(check="SSE terminal"):
            terminals = [event for event in self.events if event["type"] in {"done", "error"}]
            self.assertEqual([event["type"] for event in terminals], ["done"])
            self.assertEqual(self.events[-1], terminals[0])
            self.assertTrue(terminals[0]["trace_id"])
            self.assertEqual(self.events[0]["trace_id"], terminals[0]["trace_id"])
        with self.subTest(check="JSONL trace and tool pairs"):
            self.assert_trace("completed", expected_calls=EXPECTED_CALLS)

        reply_text = "".join(event["content"] for event in self.events if event["type"] == "text")
        with self.subTest(check="SQLite reply"):
            self.assertEqual(self.terminal_observations, [("completed", [reply_text])])
            self.assertEqual(self.assistant_replies(), [reply_text])

        reply = json.loads(reply_text)
        self.assertIsInstance(reply, dict, "reply must be a JSON object")

        # Recompute from the fixture counts, independently of the model and its forwarded receipts.
        baseline = Fraction(100 * BASELINE_COUNTS[1], BASELINE_COUNTS[0])
        impressions, clicks = current_counts
        current = Fraction(100 * clicks, impressions) if impressions else None
        expected_metrics = {
            "baseline_ctr_pct": baseline,
            "current_ctr_pct": current,
            "decline_pp": baseline - current if current is not None else None,
        }
        for field, expected in expected_metrics.items():
            with self.subTest(check=field):
                actual = reply[field]
                if expected is None:
                    self.assertIsNone(actual)
                else:
                    self.assertIn(type(actual), (int, float))
                    self.assertAlmostEqual(actual, float(expected), places=7)
        with self.subTest(check="citation provenance"):
            sources = json.loads(self.model.receipts["call_search"])["sources"]
            self.assertIsInstance(reply["citations"], list)
            for citation in reply["citations"]:
                self.assertIn(citation, sources, "citation missing from this request's knowledge receipt")
            self.assertEqual(reply["citations"], citations)
        with self.subTest(check="recommendation"):
            self.assertEqual(reply["recommendation"], recommendation)
        with self.subTest(check="evidence gap"):
            self.assertEqual(reply["evidence_gap"], evidence_gap)
        with self.subTest(check="reply fields"):
            self.assertEqual(reply.keys(), expected_metrics.keys() | {"citations", "recommendation", "evidence_gap"})


class StatefulDiagnosisModel(FixedModel):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def create(self, messages, tools=None, **kwargs):
        names = {tool["function"]["name"] for tool in tools or []}
        self.inputs.append((names, deepcopy(messages)))
        prompt = messages[-1]["content"] if kwargs.get("max_tokens") == 500 else messages[0]["content"]
        state = json.loads(next(line.removeprefix("【本轮诊断参数】") for line in prompt.splitlines()
                                if line.startswith("【本轮诊断参数】")))
        receipts = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        if kwargs.get("max_tokens") == 500:
            return self.response('{"steps": []}', [])
        if "query_data" in names:
            if receipts:
                reply = json.loads(receipts["call_data"]["result"])
                reply["rule"] = json.loads(receipts["call_knowledge"]["result"])
                return self.response(json.dumps(reply), [])
            return self.response(None, [
                tool_call("call_data", query=messages[-1]["content"]),
                tool_call("call_knowledge", question=state["rule_id"]),
            ])
        if "get_daily_report_by_date" in names:
            return self.response(None, [
                tool_call("call_plan", name="get_plan_detail", plan_id=state["plan_id"]),
                tool_call("call_baseline", name="get_daily_report_by_date", **state["baseline"]),
                tool_call("call_current", name="get_daily_report_by_date", **state["current"]),
            ])
        if "search_knowledge" in names:
            return self.response(None, [tool_call("call_search", query=state["rule_id"])])
        if "call_search" in receipts:
            receipt = receipts["call_search"]
            return self.response(receipt if isinstance(receipt, str) else json.dumps(receipt), [])
        return self.response(json.dumps({
            "plan": receipts["call_plan"],
            "baseline": receipts["call_baseline"],
            "current": receipts["call_current"],
        }), [])


def date_window(start, end):
    return {"start_date": start, "end_date": end}


SEVEN_DAYS = {
    "baseline": date_window("2026-10-19", "2026-10-25"),
    "current": date_window("2026-10-26", "2026-11-01"),
}
THREE_DAYS = {
    "baseline": date_window("2026-10-29", "2026-10-31"),
    "current": date_window("2026-11-02", "2026-11-04"),
}
ONE_DAY = {
    "baseline": date_window("2026-11-04", "2026-11-04"),
    "current": date_window("2026-11-05", "2026-11-05"),
}
STATE_CALLS = {
    call_id: TOOL_CALLS[call_id] for call_id in ("call_data", "call_knowledge", "call_search")
} | {
    "call_plan": ("get_plan_detail", "数据助手"),
    "call_baseline": ("get_daily_report_by_date", "数据助手"),
    "call_current": ("get_daily_report_by_date", "数据助手"),
}


class DiagnosisTaskTests(HomeSSEFixture):
    model_class = StatefulDiagnosisModel

    async def asyncSetUp(self):
        await super().asyncSetUp()
        database.init_db()
        self.calls = []

    def tool_overrides(self):
        return {"get_daily_report_by_date": self.report, "search_knowledge": self.rule,
                "get_plan_detail": self.plan}

    def report(self, start_date, end_date, user_id=None):
        self.calls.append(("report", start_date, end_date, user_id))
        return {"receipt": f"dates:{start_date}/{end_date}"}

    def rule(self, query):
        self.calls.append(("rule", query))
        return json.dumps({"receipt": f"rule:{query}"})

    def plan(self, plan_id, user_id=None):
        self.calls.append(("plan", plan_id, user_id))
        return {"receipt": f"plan:{plan_id}"}

    async def diagnose(self, params, message=None, **kwargs):
        events = []
        body = {"diagnosis": params}
        if message is not None:
            body["message"] = message
        await asyncio.wait_for(self.request(body=body, events=events, **kwargs), timeout=10)
        return events

    def stored(self):
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            tasks = [tuple(row) for row in conn.execute(
                "SELECT user_id, task_id, plan_id, rule_id, baseline_start, baseline_end, current_start, current_end "
                "FROM diagnosis_tasks ORDER BY rowid"
            )]
            turns = [dict(row) for row in conn.execute("SELECT * FROM diagnosis_turns ORDER BY id")]
            ordinary = [tuple(row) for row in conn.execute("SELECT * FROM conversations ORDER BY rowid")]
            return tasks, turns, ordinary

    def assert_round(self, events, *, plan_id, rule_id, windows, calls):
        self.assertEqual([event["type"] for event in events], ["thinking", "text", "done"])
        task_id, trace_id = (events[0][key] for key in ("task_id", "trace_id"))
        self.assertTrue(task_id and trace_id)
        self.assertEqual(events[-1], {"type": "done", "task_id": task_id, "trace_id": trace_id})
        self.assertIn("证据不足", events[1]["content"])
        self.assertIn("历史 CTR", events[1]["content"])
        self.assertNotIn("缺少成功回执", events[1]["content"])
        # The model still receives the scoped receipts before the server gates the reply.
        receipts = next({m["tool_call_id"]: json.loads(m["content"])["result"]
                         for m in messages if m["role"] == "tool"}
                        for names, messages in reversed(self.model.inputs)
                        if "query_data" in names and any(m["role"] == "tool" for m in messages))
        reply = json.loads(receipts["call_data"])
        self.assertEqual(reply["plan"], {"receipt": f"plan:{plan_id}"})
        self.assertEqual(json.loads(receipts["call_knowledge"]), {"receipt": f"rule:{rule_id}"})
        expected_calls = [("plan", plan_id, 17), ("rule", rule_id)]
        for key, window in windows.items():
            start, end = window["start_date"], window["end_date"]
            self.assertEqual(reply[key], {"receipt": f"dates:{start}/{end}"})
            expected_calls.append(("report", start, end, 17))
        self.assertCountEqual(calls, expected_calls)
        trace = observability.get_trace(trace_id)
        self.assertEqual(trace["status"], "completed")
        for stage in ("tool_call", "tool_result"):
            self.assertCountEqual(
                [(s["tool_call_id"], (s["action"], s["role"])) for s in trace["steps"] if s["stage"] == stage],
                STATE_CALLS.items(),
            )
        return task_id, trace_id

    async def test_create_and_resume_with_new_pool(self):
        plan_id, rule_id = "state-plan-A", "state-rule-A#ctr"
        first = await self.diagnose(dict(plan_id=plan_id, rule_id=rule_id, **SEVEN_DAYS), "private-A-first")
        task_id, first_trace = self.assert_round(
            first, plan_id=plan_id, rule_id=rule_id, windows=SEVEN_DAYS, calls=self.calls,
        )
        self.assertEqual(self.stored()[0], [(17, task_id, plan_id, rule_id, *self.window_values(SEVEN_DAYS))])
        self.calls.clear()
        with patch.object(ProviderFactory, "create", return_value=self.model):
            new_pool = AgentPool(min_size=1, max_size=1)
        with patch.object(self.server, "agent_pool", new_pool):
            second = await self.diagnose(dict(task_id=task_id, **THREE_DAYS))
        second_task, second_trace = self.assert_round(
            second, plan_id=plan_id, rule_id=rule_id, windows=THREE_DAYS, calls=self.calls,
        )
        self.assertEqual(second_task, task_id)
        self.assertNotEqual(first_trace, second_trace)
        self.assertEqual(self.pool.stats["available"], 1)
        self.assertEqual(new_pool.stats["available"], 1)
        trace_ids = [json.loads(line)["trace_id"] for line in self.trace_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(trace_ids, [first_trace, second_trace])
        tasks, turns, ordinary = self.stored()
        self.assertEqual(tasks, [(17, task_id, plan_id, rule_id, *self.window_values(THREE_DAYS))])
        self.assertEqual([turn["reply"] for turn in turns], [first[1]["content"], second[1]["content"]])
        self.assertEqual(ordinary, [])

    async def test_interleaved_tasks_keep_own_context(self):
        memory_manager.store_conversation_summary(17, "private-global-summary", "private-global-reply")
        with closing(self.connect()) as conn, conn:
            conn.execute("""INSERT INTO conversations (user_id, role, content, priority)
                VALUES (17, 'user', 'private-ordinary-chat', 1)""")
        task_ids = {}
        previous_replies = {}
        for label, plan_id in (("A", 101), ("B", 202)):
            events = await self.diagnose(
                dict(plan_id=plan_id, rule_id=f"state-rule-{label}", **SEVEN_DAYS), f"private-{label}-first",
            )
            task_ids[label], _ = self.assert_round(
                events, plan_id=plan_id, rule_id=f"state-rule-{label}", windows=SEVEN_DAYS, calls=self.calls,
            )
            previous_replies[label] = events[1]["content"]
            self.calls.clear()
        self.assertNotEqual(task_ids["A"], task_ids["B"])
        for label, other, plan_id, windows in (("A", "B", 101, THREE_DAYS), ("B", "A", 202, ONE_DAY)):
            self.model.inputs.clear()
            events = await self.diagnose(dict(task_id=task_ids[label], **windows), f"private-{label}-second")
            self.assert_round(events, plan_id=plan_id, rule_id=f"state-rule-{label}", windows=windows, calls=self.calls)
            self.calls.clear()
            for names, messages in self.model.inputs:
                text = "\n".join(m["content"] or "" for m in messages)
                for marker in (f"private-{other}-", f"state-rule-{other}", "private-global-", "private-ordinary-chat"):
                    self.assertNotIn(marker, text)
                self.assertNotIn("search_conversation_memory", names)
                if names:
                    self.assertIn(f"private-{label}-first", messages[0]["content"])
                    self.assertIn("AI: " + previous_replies[label][:60], messages[0]["content"])
        self.assertEqual(self.assistant_replies(), [])

    async def test_overlapping_runs_keep_their_accepted_windows(self):
        plan_id, rule_id = "concurrent-plan", "concurrent-rule"
        first = await self.diagnose(dict(plan_id=plan_id, rule_id=rule_id, **SEVEN_DAYS))
        task_id = first[0]["task_id"]
        self.calls.clear()
        with patch.object(ProviderFactory, "create", return_value=self.model):
            pool = AgentPool(min_size=2, max_size=2)
        older_thinking, resume_older = asyncio.Event(), asyncio.Event()

        async def pause_older():
            older_thinking.set()
            await resume_older.wait()

        with patch.object(self.server, "agent_pool", pool):
            older = asyncio.create_task(self.diagnose(
                dict(task_id=task_id, **THREE_DAYS), "private-older", on_thinking=pause_older,
            ))
            try:
                await asyncio.wait_for(older_thinking.wait(), timeout=5)
                newer = await self.diagnose(dict(task_id=task_id, **ONE_DAY), "private-newer")
                self.assert_round(newer, plan_id=plan_id, rule_id=rule_id, windows=ONE_DAY, calls=self.calls)
                self.calls.clear()
                self.model.inputs.clear()
            finally:
                resume_older.set()
                older_events = await older
        self.assert_round(older_events, plan_id=plan_id, rule_id=rule_id, windows=THREE_DAYS, calls=self.calls)
        for _, messages in self.model.inputs:
            self.assertNotIn("private-newer", "\n".join(m["content"] or "" for m in messages))
        self.assertEqual(self.stored()[0], [(17, task_id, plan_id, rule_id, *self.window_values(ONE_DAY))])
        self.assertEqual(pool.stats["available"], 2)

    async def test_reply_failure_keeps_accepted_parameters(self):
        first = await self.diagnose(dict(plan_id="error-plan", rule_id="error-rule", **SEVEN_DAYS))
        task_id = first[0]["task_id"]
        with closing(self.connect()) as conn, conn:
            conn.execute("""CREATE TRIGGER fail_diagnosis_reply BEFORE UPDATE OF reply ON diagnosis_turns
                BEGIN SELECT RAISE(ABORT, 'offline reply failure'); END""")
        second = await self.diagnose(dict(task_id=task_id, **THREE_DAYS))
        self.assertEqual([event["type"] for event in second], ["thinking", "text", "error"])
        trace_id = second[0]["trace_id"]
        self.assertEqual(second[-1]["task_id"], task_id)
        self.assertEqual(second[-1]["trace_id"], trace_id)
        self.assertEqual(observability.get_trace(trace_id)["status"], "error")
        tasks, turns, ordinary = self.stored()
        self.assertEqual(tasks, [(17, task_id, "error-plan", "error-rule", *self.window_values(THREE_DAYS))])
        self.assertEqual([turn["reply"] for turn in turns], [first[1]["content"], None])
        self.assertEqual(ordinary, [])

    async def test_rejections_leave_task_and_history_untouched(self):
        events = await self.diagnose(dict(plan_id="state-plan-A", rule_id="state-rule-A", **SEVEN_DAYS))
        task_id = events[0]["task_id"]
        accepted = self.stored()
        self.calls.clear()
        self.model.inputs.clear()
        trace = self.trace_file.read_bytes()
        other = self.sign_token(user_id=18)
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT INTO sessions (user_id, token) VALUES (?, ?)", (18, other))

        responses = []
        for lookup, token in ((task_id, other), ("unknown-task", self.token)):
            self.assertEqual(await self.diagnose(dict(task_id=lookup, **THREE_DAYS), token=token), [])
            self.assertEqual(self.status_code, 404)
            responses.append(self.response_json)
        self.assertEqual(responses[0], responses[1])

        for bad_window in (date_window("2026-02-30", "2026-03-01"),
                           date_window("2026-10-30", "2026-10-29"),
                           date_window("2026-01-01", "2026-10-31"),
                           date_window("2026-1-01", "2026-01-01")):
            for identity in ({"task_id": task_id}, {"plan_id": "new-plan", "rule_id": "new-rule"}):
                with self.subTest(window=bad_window, identity=identity):
                    self.assertEqual(await self.diagnose(dict(identity, baseline=bad_window, current=ONE_DAY["current"])), [])
                    self.assertEqual(self.status_code, 422)
        for bad_fields in (dict(task_id=task_id, plan_id="different"), dict(task_id=task_id, rule_id="different"),
                           dict(plan_id=True, rule_id="new-rule"), dict(plan_id="new-plan", rule_id=" ")):
            self.assertEqual(await self.diagnose(dict(bad_fields, **THREE_DAYS)), [])
            self.assertEqual(self.status_code, 422)
        for claims in ({}, {"user_id": 0}, {"user_id": True}, {"user_id": "17"}):
            token = self.sign_token(**claims)
            self.assertEqual(await self.diagnose(dict(task_id=task_id, **THREE_DAYS), token=token), [])
            self.assertEqual(self.status_code, 401)
        self.assertEqual(self.stored(), accepted)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.model.inputs, [])
        self.assertEqual(self.trace_file.read_bytes(), trace)

    async def test_init_creates_task_tables_in_fresh_and_existing_sqlite(self):
        legacy_db = self.database.with_name("legacy.sqlite3")
        self.database = legacy_db
        legacy_row = (1, 17, "user", "legacy row", "2026-10-18 12:00:00")
        with closing(self.connect()) as conn, conn:
            conn.execute("""CREATE TABLE conversations (
                id INTEGER PRIMARY KEY, user_id INTEGER, role TEXT, content TEXT, created_at TIMESTAMP
            )""")
            conn.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?)", legacy_row)
        for path in (legacy_db, self.database.with_name("fresh.sqlite3")):
            self.database = path
            database.init_db()
            with closing(self.connect()) as conn, conn:
                conn.execute("INSERT INTO sessions (user_id, token) VALUES (?, ?)", (17, self.token))
            events = await self.diagnose(dict(plan_id="migration-plan", rule_id="migration-rule", **SEVEN_DAYS))
            database.init_db()
            self.assertEqual(events[-1]["type"], "done")
            tasks, turns, ordinary = self.stored()
            self.assertEqual(tasks[0][1], events[-1]["task_id"])
            self.assertEqual(turns[0]["reply"], events[1]["content"])
            self.assertEqual(ordinary, [(*legacy_row, 1, None)] if path == legacy_db else [])

    @staticmethod
    def window_values(windows):
        return tuple(windows[key][field] for key in ("baseline", "current") for field in ("start_date", "end_date"))


def main():
    cases = unittest.defaultTestLoader.loadTestsFromTestCase(AdDiagnosisEval)
    total = cases.countTestCases()
    if total == 0:
        print("FAIL failed=no samples ran trace_id=-")
        return 1

    passed = 0
    for case in cases:
        result = unittest.TestResult()
        unittest.TestSuite([case]).run(result)
        failures = [
            f"{test.id().removeprefix(case.id()).strip() or 'request/reply'} {error.strip().splitlines()[-1]}"
            for test, error in result.failures + result.errors
        ]
        failures.extend(f"skipped ({reason})" for _, reason in result.skipped)
        if result.testsRun != 1 and not failures:
            failures.append("sample did not run")
        sample_id = case.id().rsplit(".", 1)[-1].removeprefix("test_")
        trace_id = next((event["trace_id"] for event in getattr(case, "events", []) if event.get("trace_id")), "-")
        status = "FAIL" if failures else "PASS"
        passed += not failures
        print(f"{sample_id} {status} failed={' | '.join(failures) or '-'} trace_id={trace_id}", flush=True)

    print(f"{passed}/{total} samples passed")
    return int(passed != total)


if __name__ == "__main__":
    raise SystemExit(main())
