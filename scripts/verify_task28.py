"""Replay TASK-028 with fixed completions, or run the late-read browser case on GLM."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import socket
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock, Thread
from time import perf_counter_ns
from traceback import format_exc
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI

from scripts import run_task27_t5 as t5, run_task27_t6 as t6
from scripts.run_real_diagnosis import Redactor
from scripts.run_task27_t3 import Audit
from scripts.task27_t3_fixture import ARTIFACT, OWNER, ROOT, digest, write
from scripts.verify_task27 import fixture


METRICS, RULE, DETAIL = "get_plan_metrics_by_date", "search_knowledge", "get_plan_detail"
STATUS_REQUEST = "请查看 #101 当前状态"
FIXED_MODES = ("late", "replace-first", "save-first", "duplicate")
WINDOW_SQL = """SELECT COUNT(*) AS days, SUM(impressions) AS impressions, SUM(clicks) AS clicks
                FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date BETWEEN ? AND ?"""
TARGET_SQL = """SELECT min(:bi,:ci)>=1000 AND :bc>0 AND 5*:cc*:bi<=4*:bc*:ci AS qualifies,
                100.0*(1.0-1.0*:cc*:bi/(:bc*:ci)) AS relative_decline_pct"""
DATES = {
    "old": {"baseline": {"start_date": "2026-10-17", "end_date": "2026-10-23"},
            "current": {"start_date": "2026-10-24", "end_date": "2026-10-30"}},
    "new": {"baseline": {"start_date": "2026-10-21", "end_date": "2026-10-23"},
            "current": {"start_date": "2026-10-28", "end_date": "2026-10-30"}},
}
CHANGE = "把两组 7 天改成两组 3 天"
CASE = t5.read(ROOT / "logs/task27-r6/preflight/frozen.json")["cases"][0]
MESSAGE = CASE["message"]
BINDINGS = {101: "26998434", 102: "27589150"}


@dataclass(frozen=True)
class ChangeCase:
    request: str
    dates: dict
    bindings: dict
    targets: dict
    plan_change: bool = False
    message: str = MESSAGE
    capture_wait: bool = False

    def plans(self, version):
        return (101, 103 if self.plan_change and version == "new" else 102)

    def version(self, text):
        marker = "#103" if self.plan_change else self.dates["new"]["baseline"]["start_date"]
        return "new" if marker in text else "old"

    def preview(self):
        if self.plan_change:
            return {"old_plan_ids": list(self.plans("old")), "new_plan_ids": list(self.plans("new")),
                    "remove_plan_id": 102, "add_plan_id": 103}
        return {"old_dates": self.dates["old"], "new_dates": self.dates["new"]}


DATE_CASE = ChangeCase(CHANGE, DATES, BINDINGS, {"old": 101, "new": 102})


def freeze(sql, output, case, *, seed_alert=True):
    if case.plan_change:
        sql("""INSERT INTO ad_plans (id,plan_name,status,daily_budget,user_id)
               VALUES (103,'东港搜索','active',460,?)""", (OWNER,))
    for plan, source in case.bindings.items():
        sql("INSERT INTO plan_metric_bindings VALUES (?, 'criteo-attribution-2017', ?, ?)",
            (plan, "b0dba3715c56871fad76d6b570520b9ea29a25a62af16bf1d9780451dc1ecf78", source))
    sql("UPDATE ad_plans SET plan_name='北岸搜索',status='active',daily_budget=310 WHERE id=101")
    sql("UPDATE ad_plans SET plan_name='南桥搜索',status='paused',daily_budget=720 WHERE id=102")
    if seed_alert:
        sql("""INSERT INTO alerts(type,message,level,is_read,user_id,created_at)
               VALUES ('ctr_review','严重 CTR 波动','critical',0,?,'2026-10-31 09:00:00')""", (OWNER,))
    binding_rows = sql("SELECT * FROM plan_metric_bindings ORDER BY plan_id", rows=True)
    plans = sql("SELECT id,plan_name,status,daily_budget FROM ad_plans WHERE user_id=? ORDER BY id", (OWNER,), rows=True)
    oracle = {"window_sql": WINDOW_SQL, "target_sql": TARGET_SQL, "bindings": binding_rows, "plans": plans}
    with closing(sqlite3.connect(ARTIFACT.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        for version, dates in case.dates.items():
            rows, targets = {}, []
            for binding in binding_rows:
                plan, source = binding["plan_id"], binding["source_plan_id"]
                if plan not in case.plans(version):
                    continue
                windows = []
                for window in dates.values():
                    params = [source, window["start_date"], window["end_date"]]
                    row = dict(conn.execute(WINDOW_SQL, params).fetchone())
                    assert row["days"] == (date.fromisoformat(params[2]) - date.fromisoformat(params[1])).days + 1
                    windows.append({**row, **window, "sql_params": params})
                b, c = windows
                facts = dict(conn.execute(TARGET_SQL, dict(bi=b["impressions"], bc=b["clicks"],
                                                          ci=c["impressions"], cc=c["clicks"])).fetchone())
                rows[plan] = {**facts, "windows": windows}
                if facts["qualifies"]:
                    targets.append(plan)
            oracle[version] = {"metrics": rows, "targets": targets}
    assert all(oracle[version]["targets"] == [target] for version, target in case.targets.items()), oracle
    write(output / "sql-oracle.json", oracle)
    return oracle


class FixedModel:
    def __init__(self, audit, case):
        self.audit = audit
        self.case = case
        self.chat = SimpleNamespace(completions=self)
        self.calls = []
        self.probe_old_dates = False
        self.probe_old_plan = False
        self.split_old_metrics = False
        self.worker_gate = None

    def create(self, messages, tools=None, **_options):
        from backend.agent.tests.test_home_delegation import dispatch, reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call

        link = self.audit.model_link.get()
        recorded = {**link, "messages": deepcopy(messages), "tools": deepcopy(tools)}
        self.calls.append(recorded)
        user = next(row["content"] for row in reversed(messages) if row["role"] == "user")
        case = self.case
        if link.get("step") in {"interpret_date_change", "interpret_group_change"}:
            request = json.loads(user)["request"]
            if case.plan_change:
                if request == "换成 #103":
                    return reply('{"clarification":"要把 #101 还是 #102 换成 #103？"}')
                remove, add = {
                    "unknown": (102, 999), "foreign": (102, 201), "duplicate": (102, 101),
                    "remove-outside": (999, 103),
                }.get(request, (102, 103))
                assert request in {"unknown", "foreign", "duplicate", "remove-outside", case.request}
                return reply(json.dumps({"remove_plan_id": remove, "add_plan_id": add}))
            if request == "缩短一点":
                return reply('{"clarification":"两窗各改成多少天？"}')
            if request in {"bad-calendar", "overlap", "unequal", "unchanged"}:
                windows = deepcopy(DATES["old"] if request == "unchanged" else DATES["new"])
                if request == "bad-calendar":
                    windows["baseline"]["start_date"] = "2026-02-30"
                elif request == "overlap":
                    windows["baseline"] = windows["current"]
                elif request == "unequal":
                    windows["baseline"]["start_date"] = "2026-10-20"
                return reply(json.dumps(windows))
            assert request == CHANGE
            return reply(json.dumps(DATES["new"]))
        returned = {row["tool_call_id"]: json.loads(row["content"]) for row in messages if row["role"] == "tool"}
        if link.get("role") == "team_worker":
            context = json.loads(user)
            if self.worker_gate and not returned:
                self.worker_gate(context["task"]["task_id"], link)
            if context["task"]["task_id"] == "review":
                rows = [row for receipts in context["inputs"].values() for row in receipts]
                metrics = [row["arguments"] for row in rows if row["source"] == METRICS]
                version = ("new" if any(row["plan_id"] == 103 for row in metrics) else "old") if case.plan_change else (
                    "old" if any(row["start_date"] == DATES["old"]["baseline"]["start_date"] for row in metrics) else "new"
                )
                detail = tool_call("detail", name=DETAIL, plan_id=case.targets[version])
                if version == "new" and self.probe_old_plan:
                    return reply("", tool_call("worker-detail-outside", name=DETAIL, plan_id=102), detail)
                return reply("", detail)
            if returned:
                if "stale" in returned:
                    assert returned["stale"]["error_code"] == "review_metric_out_of_scope", returned
                    assert returned["stale"]["effective_dates"] == DATES["new"], returned
                    assert "artifact_id" not in returned["stale"]
                return worker_summary("四窗和规则已取证")
            version = case.version(context["task"]["objective"])
            task = context["task"]
            calls = []
            if version == "new" and self.probe_old_dates:
                calls.append(tool_call("stale", name=METRICS, plan_id=101, **DATES["old"]["baseline"]))
            if version == "new" and self.probe_old_plan:
                calls.append(tool_call("worker-metric-outside", name=METRICS, plan_id=102, **case.dates["new"]["baseline"]))
            if METRICS in task["capabilities"]:
                plans = [int(task["task_id"][-3:])] if task["task_id"].startswith("evidence-") else case.plans(version)
                calls.extend(tool_call(f"metric-{plan}-{label}", name=METRICS, plan_id=plan, **window)
                             for plan in plans for label, window in case.dates[version].items())
            if RULE in task["capabilities"]:
                query = "CTR 复核规则 v1" if case.capture_wait else "ctr-review-v1#low-ctr"
                calls.append(tool_call("rule", name=RULE, query=query))
            return reply("", *calls)
        if not tools:
            return reply('{"steps":[]}')
        if user == STATUS_REQUEST:
            if not returned:
                return reply("", tool_call("direct-status", name=DETAIL, plan_id=101))
            return reply("#101 当前状态 active。")
        if not returned:
            return reply("", tool_call("alerts", name="get_alerts", unread_only=True))
        version = case.version(user)
        if version == "new" and self.probe_old_plan and "main-metric-outside" not in returned:
            return reply("", tool_call("main-metric-outside", name=METRICS, plan_id=102, **case.dates["new"]["baseline"]),
                         tool_call("main-detail-outside", name=DETAIL, plan_id=102))
        if "batch" not in returned:
            fields = {"expected_output": "已核验回执", "stop_condition": "取得回执后结束"}
            plans = "、".join(f"#{plan}" for plan in case.plans(version))
            evidence = [{
                **fields, "task_id": "evidence", "capabilities": [METRICS, RULE],
                "objective": f"取证 {plans} 四窗 {case.dates[version]} 及 ctr-review-v1#low-ctr",
            }]
            if version == "old" and self.split_old_metrics:
                evidence = [{**fields, "task_id": f"evidence-{plan}", "capabilities": [METRICS],
                             "objective": f"取证 #{plan} 两窗 {case.dates[version]}",
                             "read_scope": [{"tool": METRICS, "arguments": {"plan_id": plan, **window}}
                                            for window in case.dates[version].values()]}
                            for plan in case.plans(version)]
                evidence.append({**fields, "task_id": "rule", "capabilities": [RULE],
                                 "objective": "核对 ctr-review-v1#low-ctr"})
            action = dispatch("batch", *evidence, {
                **fields, "task_id": "review", "capabilities": [DETAIL],
                "dependencies": [task["task_id"] for task in evidence],
                "objective": "依据四窗指标和规则复核并查入选计划详情",
            })
            recorded["tool_calls"] = [{"id": action.id, "function": {
                "name": action.function.name, "arguments": action.function.arguments}}]
            return reply("", action)
        return reply("复核结束。")


class Synchronization:
    def __init__(self, case):
        self.case = case
        self.lock = Lock()
        self.reset("idle")

    def reset(self, mode, *, held_reads=1):
        self.mode = mode
        self.held_reads = held_reads
        self.started_reads = self.ready_reads = 0
        self.timeline = []
        self.task_sheets = []
        self.waiting, self.release = Event(), Event()
        self.metric_filter = None
        self.stages = {}
        self.confirm_waiting, self.confirm_release = Event(), Event()
        self.duplicate = Barrier(2)
        self.duplicate_enabled = False

    def mark(self, event, **fields):
        self.timeline.append({"event": event, "ns": perf_counter_ns(), **fields})

    def schedule(self, original):
        def run(runtime, plan, trace_id, dispatch_id):
            self.task_sheets.append({"trace_id": trace_id, "dispatch_id": dispatch_id,
                                     "tasks": plan.model_dump(mode="json")["tasks"]})
            return original(runtime, plan, trace_id, dispatch_id)
        return run

    def read(self, audit, original):
        def run(*args, **kwargs):
            link = audit.tool_link.get()
            old_read = args[0] == 102 if self.case.plan_change else args[1:3] in [
                tuple(window.values()) for window in self.case.dates["old"].values()]
            if self.metric_filter:
                old_read = self.metric_filter(args)
            if link and self.mode == "late" and old_read:
                with self.lock:
                    hold = self.started_reads < self.held_reads
                    if hold:
                        self.started_reads += 1
                if hold:
                    self.mark("old_metric_sent", **link, plan_id=args[0], start=args[1], end=args[2])
                    result = original(*args, **kwargs)
                    self.mark("old_metric_sql_completed", **link)
                    with self.lock:
                        self.ready_reads += 1
                        if self.ready_reads == self.held_reads:
                            self.waiting.set()
                    assert self.release.wait(80), "metric barrier expired"
                    self.mark("old_metric_returned", **link, result=result)
                    return result
            return original(*args, **kwargs)
        return run

    def save_reply(self, original):
        def run(group_run, content, priority, **kwargs):
            old = self.case.version(content) == "old"
            if old and self.mode in {"replace-first", "save-first", "duplicate"}:
                self.mark("old_save_waiting", run_id=group_run.run_id)
                self.waiting.set()
                assert self.release.wait(35), "save barrier expired"
            try:
                result = original(group_run, content, priority, **kwargs)
            except Exception as error:
                self.mark("save_rejected", run_id=group_run.run_id, error=type(error).__name__)
                raise
            self.mark("answer_committed", run_id=group_run.run_id, entry_id=result)
            return result
        return run

    def confirm(self, original):
        def run(*args, **kwargs):
            self.mark("confirmation_arrived")
            if self.mode == "save-first":
                self.confirm_waiting.set()
                assert self.confirm_release.wait(35), "confirmation barrier expired"
            if self.duplicate_enabled and args[0] == OWNER:
                self.duplicate.wait(35)
            result = original(*args, **kwargs)
            if result[0]:
                self.mark("replacement_committed", run_id=result[0].run_id)
            return result
        return run


@contextmanager
def serve(app, sync):
    control = FastAPI()

    @control.get("/wait")
    def wait():
        assert sync.waiting.wait(55), "old read did not start"
        return next((row for row in sync.timeline if row["event"] == "old_metric_sent"), {})

    @control.post("/release")
    def release():
        sync.mark("release_old_metric")
        sync.release.set()
        return {}

    @control.get("/stage/{name}")
    def wait_stage(name: str):
        assert sync.stages[name][0].wait(30), name
        return {}

    @control.post("/stage/{name}")
    def release_stage(name: str):
        sync.mark(name + "_released")
        sync.stages[name][1].set()
        return {}

    app.mount("/__task28", control)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            sync.release.set()
            sync.confirm_release.set()
            for _, release_stage_event in sync.stages.values():
                release_stage_event.set()
            sync.duplicate.abort()
            server.should_exit = True
            thread.join(35)
            assert not thread.is_alive()


def login(client, username, password):
    captcha = client.get("/api/captcha").json()
    reply = client.post("/api/login", json={"username": username, "password": password,
                        "captcha_id": captcha["captcha_id"], "captcha_text": "0726"})
    reply.raise_for_status()
    client.headers["Authorization"] = "Bearer " + reply.json()["data"]["token"]


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def browser_round(url, output, password, case, browser_fixture=None):
    request_file, result_file = output / "browser-request.json", output / "browser.json"
    write(request_file, {"message": case.message, "request": case.request,
                         "capture_wait": case.capture_wait, **case.preview(), **(browser_fixture or {})})
    completed = subprocess.run(["node", "tests/task28_browser.cjs", url, str(request_file), str(result_file)],
                               cwd=ROOT, env={**t5.browser_env(), "AD_TASK28_PASSWORD": password},
                               capture_output=True, text=True, timeout=160, encoding="utf-8")
    if completed.returncode:
        (output / "browser-error.txt").write_text(completed.stderr, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr
    return t5.read(result_file)


def fixed_order(owner, other, sync, sql, output):
    case = sync.case
    group = owner.post("/api/chat/conversations", json={"title": sync.mode}).json()["data"]["conversation_id"]
    history_path = f"/api/chat/conversations/{group}/history"
    endpoint = "changes" if case.plan_change else "date-changes"
    preview_path = f"/api/chat/conversations/{group}/{endpoint}"
    start = {"conversation_id": group, "message": MESSAGE}
    sample = {"conversation_id": group, "old_request": start}
    with ThreadPoolExecutor(max_workers=3) as pool:
        old = pool.submit(owner.post, "/api/chat/stream", json=start)
        try:
            assert sync.waiting.wait(25), "old save did not reach barrier"
            old_run = next(run for run in owner.get(history_path).json()["data"]["runs"] if run["status"] == "running")
            preview = {"run_id": old_run["run_id"], "message": case.request}
            def persisted():
                return (
                    sql("SELECT * FROM chat_group_runs WHERE conversation_id=?", (group,), rows=True),
                    sql("SELECT * FROM chat_group_entries JOIN chat_group_runs USING(run_id) "
                        "WHERE conversation_id=? AND kind='change'", (group,), rows=True),
                    sql("SELECT * FROM chat_group_date_proposals WHERE run_id=?", (old_run["run_id"],), rows=True),
                )

            if sync.mode == "duplicate":
                before = persisted()
                count = sum(row["event"] == "answer_committed" for row in sync.timeline)
                rejection = {}
                ambiguous = "换成 #103" if case.plan_change else "缩短一点"
                invalid = ("unknown", "foreign", "duplicate", "remove-outside") if case.plan_change else (
                    "bad-calendar", "overlap", "unequal", "unchanged")
                for message in (*invalid, ambiguous):
                    response = owner.post(preview_path, json={**preview, "message": message})
                    rejection[message] = {"status": response.status_code, "body": response.json()}
                    assert response.status_code == (200 if message == ambiguous else 422), rejection
                assert "clarification" in rejection[ambiguous]["body"]["data"]
                if case.plan_change:
                    assert rejection["unknown"] == rejection["foreign"]
                denied = other.post(preview_path, json=preview)
                assert denied.status_code == 404
                rejection["foreign_preview"] = denied.status_code
                assert persisted() == before
                assert sum(row["event"] == "answer_committed" for row in sync.timeline) == count
                sample["rejections"] = rejection
            proposed = owner.post(preview_path, json=preview)
            proposed.raise_for_status()
            proposal = proposed.json()["data"]
            assert all(proposal[key] == value for key, value in case.preview().items()), proposal
            field = "change" if case.plan_change else "date_change"
            confirm = {"conversation_id": group,
                       field: {key: proposal[key] for key in ("run_id", "proposal_id")}}
            sample.update(preview_request=preview, preview_response=proposal, confirmation_request=confirm)
            if sync.mode == "duplicate":
                denied = other.post("/api/chat/stream", json=confirm)
                # An unauthorized request must never reach the test barrier or the model.
                assert denied.status_code == 404, denied.text
                sample["rejections"]["foreign_confirmation"] = denied.status_code
                if case.plan_change:
                    before = persisted()
                    sql("UPDATE ad_plans SET user_id=18 WHERE id=103")
                    try:
                        denied = owner.post("/api/chat/stream", json=confirm)
                        after = persisted()
                        assert denied.status_code == 422 and denied.json() == rejection["foreign"]["body"]
                        assert before == after and before[0][0]["status"] == "running"
                        rejection["transferred_after_preview"] = {"status": denied.status_code, "body": denied.json(),
                                                                  "before": before, "after": after}
                    finally:
                        sql("UPDATE ad_plans SET user_id=? WHERE id=103", (OWNER,))
                sync.duplicate_enabled = True
            pending = pool.submit(owner.post, "/api/chat/stream", json=confirm)
            if sync.mode == "save-first":
                assert sync.confirm_waiting.wait(15)
                sync.release.set()
                sample["old_sse"] = events(old.result())
                sync.confirm_release.set()
                response = pending.result()
                sample["confirmation_status"] = response.status_code
                sample["conflict"] = response.json()["detail"]
                assert response.status_code == 409 and "发起新一轮" in sample["conflict"]
                assert sample["old_sse"][-1]["type"] == "done"
            else:
                duplicate = pool.submit(owner.post, "/api/chat/stream", json=confirm) if sync.mode == "duplicate" else None
                replies = [events(future.result()) for future in ([pending, duplicate] if duplicate else [pending])]
                sample["new_sse"], = [reply for reply in replies if not reply[-1].get("already_confirmed")]
                if duplicate:
                    sample["duplicate_sse"], = [reply for reply in replies if reply[-1].get("already_confirmed")]
                    assert sample["duplicate_sse"][-1]["run_id"] == sample["new_sse"][-1]["run_id"]
                assert sample["new_sse"][-1]["type"] == "done"
                sync.release.set()
                sample["old_sse"] = events(old.result())
                assert sample["old_sse"][-1]["type"] == "superseded"
        finally:
            sync.release.set()
            sync.confirm_release.set()
            write(output / "partial.json", sample)
    sample["history"] = owner.get(history_path).json()["data"]
    return sample


def audit_round(sample, mode, sync, audit, model_calls, sql, oracle):
    from backend.agent.multi_window_review import parse_review_goal
    from backend.memory import conversation_memory
    from backend.observability import get_trace
    from backend.rag.rule_reference import canonical_rule_query, rule_query_scope

    case = sync.case
    old = sample["old_sse"][-1]
    new = sample.get("new_sse", [None])[-1]
    group = sample["conversation_id"]
    run_ids = {old["run_id"], new["run_id"]} if new else {old["run_id"]}
    trace_ids = {old["trace_id"], new["trace_id"]} if new else {old["trace_id"]}
    rows = sql("""SELECT e.* FROM chat_group_entries e JOIN chat_group_runs r USING(run_id)
                  WHERE r.conversation_id=? ORDER BY e.id""", (group,), rows=True)
    changes = [row for row in rows if row["kind"] == "change"]
    answers = [row for row in rows if row["kind"] == "assistant"]
    assert len(changes) == int(bool(new)) and len(answers) == 1, rows
    assert answers[0]["run_id"] == (new or old)["run_id"]
    assert [row["run_id"] for row in sync.timeline if row["event"] == "answer_committed"] == [answers[0]["run_id"]]
    statuses = {run["run_id"]: run["status"] for run in sample["history"]["runs"]}
    assert set(statuses) == run_ids
    assert statuses[old["run_id"]] == ("superseded" if new else "completed")
    if new:
        assert statuses[new["run_id"]] == "completed"
        change = json.loads(changes[0]["details"])
        assert change["old_run_id"] == old["run_id"] and change["new_run_id"] == new["run_id"]
        assert change["old_trace_id"] == old["trace_id"] and change["new_trace_id"] == new["trace_id"]
        assert all(change[key] == value for key, value in case.preview().items()), change
    assert [row["content"] for row in sample["history"]["entries"] if row["kind"] == "assistant"] == [answers[0]["content"]]
    assert [event["content"] for event in sample["new_sse" if new else "old_sse"] if event["type"] == "text"] == [
        answers[0]["content"]]
    for version, terminal in (("old", old), ("new", new)):
        if terminal is None:
            continue
        frames = sample[version + "_sse"]
        assert all(frame["run_id"] == terminal["run_id"] and frame["trace_id"] == terminal["trace_id"]
                   for frame in frames)
        assert sum(frame["type"] in {"done", "superseded", "error"} for frame in frames) == 1
    memory = conversation_memory._get_collection(conversation_memory._get_client()).get(
        where={"conversation_id": group}, include=["documents", "metadatas"])
    assert len(memory["ids"]) == 1 and memory["metadatas"][0]["assistant_message_id"] == answers[0]["id"], memory
    if new:
        if case.plan_change:
            original = parse_review_goal(sample["old_request"]["message"])[1]
            replacement, = [row["content"] for row in rows if row["kind"] == "user" and row["run_id"] == new["run_id"]]
            assert replacement == sample["old_request"]["message"].replace("#102", "#103")
            expected_goal = {**original.trace_fields(), "plan_ids": case.plans("new")}
            assert parse_review_goal(replacement)[1].trace_fields() == expected_goal
            assert "#102" not in answers[0]["content"] and "南桥搜索" not in answers[0]["content"]
            assert "#102" not in memory["documents"][0] and "#103" in memory["documents"][0]
        else:
            assert DATES["old"]["baseline"]["start_date"] not in memory["documents"][0]
            assert DATES["new"]["baseline"]["start_date"] in memory["documents"][0]
    traces = {trace_id: get_trace(trace_id) for trace_id in trace_ids}
    for trace_id, trace in traces.items():
        workers = {action: {step["runtime_call_id"] for step in trace["steps"]
                            if step["stage"] == "worker" and step["action"] == action}
                   for action in ("created", "reclaimed")}
        assert workers["created"] == workers["reclaimed"] and workers["created"], (trace_id, workers)
    if mode == "late":
        timeline = sync.timeline
        when = {entry["event"]: entry["ns"] for entry in timeline}
        assert when["replacement_committed"] < when["answer_committed"] < when["release_old_metric"]
        gated = {entry["runtime_call_id"] for entry in timeline if entry["event"] == "old_metric_sent"}
        assert len(gated) == sync.held_reads
        if case.plan_change:
            assert all(entry["plan_id"] == 102 for entry in timeline if entry["event"] == "old_metric_sent")
        for call_id in gated:
            order = {entry["event"]: entry["ns"] for entry in timeline if entry.get("runtime_call_id") == call_id}
            assert order["old_metric_sent"] < order["old_metric_sql_completed"] < when["replacement_committed"]
            assert when["release_old_metric"] < order["old_metric_returned"]
        late = [step for step in traces[old["trace_id"]]["steps"]
                if step.get("evidence", {}).get("returned_after_replacement")]
        late_ids = {step["runtime_call_id"] for step in late}
        assert gated <= late_ids and all(step["error_code"] == "expired_receipt" for step in late), late
        assert not any(row["trace_id"] == old["trace_id"] and row["accepted"] and row["tool_call_id"] in late_ids
                       for row in audit.receipts)
        old_events = [json.loads(row["details"]) for row in rows
                      if row["kind"] == "worker" and row["run_id"] == old["run_id"]]
        late_events = [event for event in old_events if event.get("runtime_call_id") in late_ids]
        assert {event["runtime_call_id"] for event in late_events} == gated
        assert all(event["kind"] == "tool_started" and not event.get("artifact_id") for event in late_events)
    if mode in {"replace-first", "duplicate"}:
        assert sum(row["event"] == "replacement_committed" for row in sync.timeline) == 1
        assert [row["run_id"] for row in sync.timeline if row["event"] == "save_rejected"] == [old["run_id"]]
    if mode == "duplicate" and not case.plan_change:
        assert len([row for row in model_calls if row.get("step") == "interpret_date_change"]) == 6
    version = "new" if new else "old"
    trace_id = (new or old)["trace_id"]
    trace = traces[trace_id]
    verdict, = [step["evidence"] for step in trace["steps"]
                if step["stage"] == "conditional_detail" and step["action"] == "reply"]
    assert verdict["independent_review_status"] == "completed", verdict
    assert verdict["target_plan_ids"] == oracle[version]["targets"], verdict
    assert verdict["policy"]["chunk_id"] == "ctr-review-v1-low-ctr-01"
    for row in verdict["rows"]:
        expected = oracle[version]["metrics"][str(row["plan_id"])]
        assert abs(row["comparison"]["relative_decline_pct"] - expected["relative_decline_pct"]) < 1e-8
        for actual, window in zip(row["windows"], expected["windows"]):
            assert all(actual[key] == window[key] for key in ("start_date", "end_date", "impressions", "clicks"))
    reads = [{**row, "arguments": {key: value for key, value in row["arguments"].items() if key != "connect"}}
             for row in audit.reads if row.get("trace_id") in trace_ids]
    metrics = [row for row in reads if row["trace_id"] == trace_id and row["tool"] == "query_plan_metrics"]
    expected = [(plan, w["start_date"], w["end_date"]) for plan in case.plans(version)
                for w in case.dates[version].values()]
    actual = [(row["arguments"]["plan_id"], row["arguments"]["start_date"], row["arguments"]["end_date"])
              for row in metrics]
    assert sorted(actual) == sorted(expected), actual
    sdk_inputs = []
    for call in model_calls:
        if call.get("trace_id") != trace_id or call.get("role") != "team_worker":
            continue
        messages = call.get("body", call)["messages"]
        context = json.loads(messages[1]["content"])
        allowed = context["task"]["capabilities"]
        if DETAIL in allowed and not any(row["role"] == "tool" for row in messages):
            sdk_inputs.append({**call, "context": context})
    first, = sdk_inputs
    handoff = [row for receipts in first["context"]["inputs"].values() for row in receipts]
    assert len(handoff) == 5, handoff
    assert {(row["arguments"]["plan_id"], row["arguments"]["start_date"], row["arguments"]["end_date"])
            for row in handoff if row["source"] == METRICS} == set(expected)
    assert all(row["result"]["coverage"]["complete"] for row in handoff if row["source"] == METRICS)
    assert any(row["source"] == RULE and any(hit["chunk_id"] == verdict["policy"]["chunk_id"]
                                            for hit in row["result"]["evidence"]) for row in handoff)
    detail, = [row for row in reads if row["trace_id"] == trace_id and row["tool"] == "query_plan_detail"]
    assert detail["role"] == "team_worker" and detail["parent_call_id"] == first["worker_id"], detail
    plan = next(plan for plan in oracle["plans"] if plan["id"] == verdict["target_plan_ids"][0])
    assert detail["arguments"]["plan_id"] == plan["id"] and detail["arguments"]["user_id"] == OWNER
    assert all(detail["result"][key] == value for key, value in plan.items()), detail
    accepted = [row for row in audit.receipts if row["trace_id"] == trace_id and row["accepted"]]
    chain = []
    for artifact in handoff:
        producer, = [row for row in accepted if row["artifact_id"] == artifact["artifact_id"]]
        assert all(producer[key] == artifact[key] for key in (
            "task_id", "worker_id", "source", "status", "tool_call_id", "arguments", "result"))
        if artifact["source"] == METRICS:
            reading, = [row for row in metrics if row["runtime_call_id"] == producer["tool_call_id"]]
            assert reading["parent_call_id"] == producer["worker_id"]
        else:
            retrieval, = [step for step in trace["steps"] if step["stage"] == "tool_result"
                          and step.get("runtime_call_id") == producer["tool_call_id"]]
            assert retrieval["action"] == RULE and retrieval["parent_call_id"] == producer["worker_id"]
            query = canonical_rule_query(producer["arguments"]["query"])
            assert retrieval["evidence"]["rule_target"] == query
            sections, version = rule_query_scope(query)
            hit, = [hit for hit in producer["result"]["evidence"] if hit["chunk_id"] == verdict["policy"]["chunk_id"]]
            assert hit["rule_id"] == "ctr-review#low-ctr" and hit["rule_id"] in sections
            assert hit["version"] == version == "v1"
        chain.append({**artifact, "runtime_call_id": producer["tool_call_id"]})
    admission, = [step for step in trace["steps"] if step["stage"] == "conditional_detail"
                  and step["action"] == "allowed"]
    assert admission["runtime_call_id"] == detail["runtime_call_id"]
    proof = admission["evidence"]["review_input"]
    assert proof["worker_id"] == first["worker_id"] and proof["model_call_id"] == first["model_call_id"]
    assert set(proof["input_artifact_ids"]) == {row["artifact_id"] for row in handoff}
    if "response" in first:
        sdk_action, = [call for call in model_calls if call.get("model_call_id") == proof["action_model_call_id"]]
        action, = [tool for choice in sdk_action["response"]["body"]["choices"]
                   for tool in choice["message"].get("tool_calls", []) if tool["id"] == admission["tool_call_id"]]
        assert sdk_action["worker_id"] == first["worker_id"] and action["function"]["name"] == DETAIL
        assert json.loads(action["function"]["arguments"]) == {"plan_id": plan["id"]}
    detail_receipt, = [row for row in accepted if row["source"] == DETAIL and row["status"] == "succeeded"]
    assert detail_receipt["tool_call_id"] == detail["runtime_call_id"] and detail_receipt["worker_id"] == first["worker_id"]
    if mode == "late":
        basis = sample["refreshed"]["basis"]
        assert basis["entry_id"] == f"entry:{answers[0]['id']}"
        assert json.loads(answers[0]["details"]) == {key: value for key, value in basis.items()
                                                     if key not in {"entry_id", "run_id", "conversation_id"}}
        handoff_ids = {row["artifact_id"] for row in handoff}
        adopted_ids = {row["artifact_id"] for row in (*basis["metrics"], basis["rule"])}
        assert adopted_ids == handoff_ids
        assert [row["artifact_id"] for row in basis["details"]] == [detail_receipt["artifact_id"]]
        assert basis["details"][0]["review_input"] == proof
        events = sample["refreshed"]["new_progress"]["entries"]
        transfer, = [event for event in events if event["kind"] == "review_inputs"]
        assert set(transfer["artifact_ids"]) == handoff_ids
        assert transfer["model_call_id"] == proof["model_call_id"]
        assert transfer["worker_id"] == proof["worker_id"]
        receipts = {event["runtime_call_id"]: event for event in events if event["kind"] == "receipt"}
        for row in [*handoff, detail_receipt]:
            event = receipts[row["tool_call_id"]]
            assert event["worker_id"] == row["worker_id"]
            assert event["artifact_ids"] == [row["artifact_id"]]
    probes = []
    if case.plan_change and mode == "duplicate":
        codes = {f"{role}-{tool}-outside": "review_metric_out_of_scope" if tool == "metric" else "review_wrong_target"
                 for role in ("main", "worker") for tool in ("metric", "detail")}
        for call_id, code in codes.items():
            probe, = [step for step in trace["steps"]
                      if step["stage"] == "tool_result" and step.get("tool_call_id") == call_id]
            assert probe["error_code"] == code, probe
            assert not any(row["runtime_call_id"] == probe["runtime_call_id"] for row in reads)
            assert not any(row["tool_call_id"] == probe["runtime_call_id"] for row in accepted)
            probes.append(probe)
    if new:
        old_artifacts = {row["artifact_id"] for row in audit.receipts if row["trace_id"] == old["trace_id"]}
        assert not old_artifacts.intersection(row["artifact_id"] for row in handoff)
        assert not old_artifacts.intersection(row.get("event", {}).get("artifact_id")
                                             for row in sample["history"]["entries"] if row["run_id"] == new["run_id"])
    return {"traces": traces, "sql_entries": rows, "memory": memory, "reads": reads, "review_input": first,
            "review_detail_read": detail, "review_detail_admission": admission, "review_detail_receipt": detail_receipt,
            "handoff_chain": chain, "out_of_scope_probes": probes, "decision": verdict}


def run(output, live, case, *, extra_browser=None, audit_browser=None, prepare_browser=None,
        fixed_modes=FIXED_MODES):
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
    before = {"business": digest(ROOT / "ad_agent.db"), "metrics": digest(ARTIFACT)}
    redactor = Redactor()
    password = token_urlsafe(24)
    redactor.secrets.append(password)
    sync = Synchronization(case)
    report = {"mode": "live" if live else "fixed", "cases": {}, "storage_before": before}
    with (fixture(output, None, password) as sql,
          TemporaryDirectory(dir=output, prefix="state-") as temporary, ExitStack() as stack):
        scratch = Path(temporary)
        if live:
            config = settings.llm_config()
            assert (config.provider, config.model, config.base_url) == ("glm", t5.MODEL, t5.ENDPOINT) and config.api_key
            redactor.secrets.append(config.api_key)
            class LiveAudit(t6.T6Audit):
                def case_cap(self, case):
                    return 25

            audit = LiveAudit(output / "glm-sdk-ledger.sqlite3", redactor, uuid4().hex, 25)
            audit.case = "late"
            provider = llm_provider.ProviderFactory.create()
            stack.callback(provider._client.close)
            hooks = provider._client._client.event_hooks
            hooks["request"].append(audit.request)
            hooks["response"].append(audit.response)
            t6.install_transport_observer(stack, provider, audit)
        else:
            audit = Audit(scratch / "empty.sqlite3", redactor, uuid4().hex, 0)
            provider = FixedModel(audit, case)
            stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=provider))
        stack.callback(SharedSystemClient.clear_system_cache)
        stack.callback(lambda: [system.stop() for system in SharedSystemClient._identifier_to_system.values()])
        stack.enter_context(patch.object(settings, "PLAN_METRICS_DB_PATH", str(ARTIFACT)))
        stack.enter_context(patch.object(settings, "RAG_DB_PATH", str(scratch / "rag")))
        for module in (rag_knowledge, conversation_memory):
            stack.enter_context(patch.object(module, "BGEEmbedding", FixedReviewEmbedding))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        for name in ("TRACE_LOG_FILE", "FUNC_LOG_FILE", "ERROR_LOG_FILE"):
            stack.enter_context(patch.object(observability, name, output / (name.lower() + ".jsonl")))
        for module in (llm_provider, team_runtime):
            stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        for module in (home_react, team_tools):
            stack.enter_context(patch.object(module, "trace_tool_call", audit.trace_tool(module.trace_tool_call)))
        stack.enter_context(patch.object(team_runtime.DelegationRun, "_record",
                                         audit.wrap_receipt(team_runtime.DelegationRun._record)))
        if audit_browser:
            stack.enter_context(patch.object(team_runtime.DelegationRun, "_schedule",
                                             sync.schedule(team_runtime.DelegationRun._schedule)))
        for name in ("query_plan_metrics", "query_plan_detail"):
            original = getattr(tools, name)
            observed = audit.wrap_read(name, original)
            if name == "query_plan_metrics":
                observed = sync.read(audit, observed)
            stack.enter_context(patch.object(tools, name, observed))
        stack.enter_context(patch.object(chat_groups.GroupRun, "save_reply", sync.save_reply(chat_groups.GroupRun.save_reply)))

        import fast_server

        stack.enter_context(patch.object(fast_server, "_confirm_group_change", sync.confirm(fast_server._confirm_group_change)))
        oracle = freeze(sql, output, case)
        if case.capture_wait:
            write(output / "frozen-request.json", {"message": case.message, "change_request": case.request,
                  "dates": case.dates, "plans": case.preview(), "rule": "ctr-review-v1#low-ctr",
                  "sdk_attempt_cap": 25 if live else 0})
        seed_review_rules(rag_knowledge.get_rag_knowledge())
        if live:
            assert not audit.rows()
            audit.enabled = True
        oracle = json.loads(json.dumps(oracle))
        try:
            browser_fixture = (prepare_browser(fast_server.app, output, sync, audit, provider, sql, oracle, live)
                               if prepare_browser else None)
            with serve(fast_server.app, sync) as url:
                for mode in (("late",) if live else fixed_modes):
                    sync.reset(mode, held_reads=2 if mode == "late" and not live and not case.plan_change else 1)
                    audit.case = mode
                    start = 0 if live else len(provider.calls)
                    folder = output / mode
                    folder.mkdir()
                    try:
                        if mode == "late":
                            if not live:
                                provider.split_old_metrics = True
                            sample = browser_round(url, folder, password, case, browser_fixture)
                        else:
                            provider.split_old_metrics = False
                            provider.probe_old_dates = mode == "duplicate" and not case.plan_change
                            provider.probe_old_plan = mode == "duplicate" and case.plan_change
                            with (httpx.Client(base_url=url, timeout=80, trust_env=False) as owner,
                                  httpx.Client(base_url=url, timeout=80, trust_env=False) as other):
                                login(owner, "task27-owner", password)
                                login(other, "task27-other", password)
                                sample = fixed_order(owner, other, sync, sql, folder)
                        write(folder / "sample.json", sample)
                        calls = audit.rows() if live else provider.calls[start:]
                        findings = audit_round(sample, mode, sync, audit, calls, sql, oracle)
                        write(folder / "evidence.json", redactor.clean(findings))
                        if audit_browser and mode == "late":
                            audit_browser(sample, sync, calls, audit.receipts, findings, sql, folder)
                        report["cases"][mode] = "pass"
                    except Exception:
                        report["cases"][mode] = "fail"
                        if prepare_browser:
                            write(folder / "first-deviation.json", {"error": redactor.clean(format_exc())})
                        raise
                    finally:
                        write(folder / "timeline.json", redactor.clean(sync.timeline))
                        write(folder / "model.json", redactor.clean(audit.rows() if live else provider.calls[start:]))
                        write(folder / "receipts.json", redactor.clean(audit.receipts))
                        if audit_browser:
                            write(folder / "accepted-task-sheets.json", redactor.clean(sync.task_sheets))
                if extra_browser and not live:
                    extra_browser(url, output, password, sync, provider, audit, sql, oracle, redactor, report)
        finally:
            report["sdk_attempts"] = len(audit.rows())
            report["storage_after"] = {"business": digest(ROOT / "ad_agent.db"), "metrics": digest(ARTIFACT)}
            write(output / "report.json", report)
            assert report["storage_after"] == before
    print(json.dumps(report, ensure_ascii=False))


def main(case=DATE_CASE, description=__doc__, *, extra_browser=None, audit_browser=None, prepare_browser=None,
         fixed_modes=FIXED_MODES):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
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
    run(args.output_dir.resolve(), args.live, case, extra_browser=extra_browser, audit_browser=audit_browser,
        prepare_browser=prepare_browser, fixed_modes=fixed_modes)


if __name__ == "__main__":
    main()
