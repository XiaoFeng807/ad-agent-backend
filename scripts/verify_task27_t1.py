"""TASK-027-T1: exercise evidence handoffs through the real group HTTP and Chromium entry points."""

import argparse
from contextlib import closing, contextmanager
from copy import deepcopy
from gzip import compress
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import socket
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic_ns
from types import SimpleNamespace
from unittest.mock import patch

from scripts.verify_task27 import ROOT, FixedEmbedding, dump, fixture


PLAN = "get_plan_detail"
METRICS = "get_plan_metrics_by_date"
WINDOW = {"start_date": "2026-10-19", "end_date": "2026-10-19"}
DAY = WINDOW["start_date"]
OWNER = 17
A, B, C = "metrics-A", "detail-B", "independent-C"
OTHER_MARKER = "T1_OTHER_GROUP_ONLY"
MESSAGES = {
    "seed": "T1_SEED_OTHER_GROUP", "first": "T1_COMPARE", "second": "T1_COMPARE",
    "foreign": "T1_CROSS_GROUP", "retry": "T1_RETRY", "missing": "T1_MISSING",
}


def receipts(messages):
    return {row["tool_call_id"]: json.loads(row["content"]) for row in messages if row["role"] == "tool"}


class FixedModel:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)
        self.case = None
        self.foreign_id = None
        self.calls, self.reads, self.timeline = [], [], []
        self.waiting, self.release = Event(), Event()
        self.lock = Lock()

    def mark(self, event):
        with self.lock:
            self.timeline.append({"case": self.case, "event": event, "ns": monotonic_ns()})

    def create(self, messages, tools=None, response_format=None, **options):
        from backend.agent.tests.test_home_delegation import dispatch, reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call
        from backend.agent.tests.test_team_runtime import task

        current = next(row["content"] for row in reversed(messages) if row["role"] == "user")
        returned = receipts(messages)
        context = json.loads(current) if response_format else None
        phase = "worker" if response_format else "main" if tools else "planner"
        name = context["task"]["task_id"] if context else None
        with self.lock:
            self.calls.append(deepcopy({"case": self.case, "phase": phase, "task_id": name,
                                        "messages": messages, "tools": tools,
                                        "response_format": response_format, "options": options}))
        if phase == "worker":
            self.mark("model:" + name)
            if returned:
                if any(not row["success"] for row in returned.values()):
                    return worker_summary("计划历史指标缺项", status="missing")
                return worker_summary("已取得只读回执")
            if name == B:
                candidates = [row["result"] for row in context["inputs"][A]
                              if row["source"] == METRICS and row["status"] == "succeeded"
                              and row["result"]["coverage"]["complete"] and row["result"]["ctr_pct"] >= 8]
                selected, = candidates
                return reply("", tool_call("selected-detail", name=PLAN, plan_id=selected["plan_id"]))
            if name == C or self.case == "seed":
                return reply("", tool_call("single-detail", name=PLAN, plan_id=101 if name == C else 103))
            assert name == A
            return reply("", *(tool_call(f"metric-{plan_id}", name=METRICS, plan_id=plan_id, **WINDOW)
                               for plan_id in (101, 102)))

        if phase == "planner":
            return reply('{"steps":[]}')
        assert current == MESSAGES[self.case], (phase, self.case, current)
        upstream = {**task(A, tools=(METRICS,)),
                    "objective": f"Read historical impressions and clicks for plans 101 and 102 on {DAY}"}
        downstream = {**task(B, deps=(A,), refs=(A,)),
                      "objective": "Read detail for the sole plan with complete coverage and CTR at least 8%"}
        if self.case == "seed":
            if not returned:
                return reply("", dispatch("batch", task(A)))
            item, = returned["batch"]["evidence"]
            self.foreign_id = item["artifact_id"]
            return reply("另一工作群已取得计划详情")
        if self.case == "foreign":
            if "local-A" not in returned:
                return reply("", dispatch("local-A", upstream))
            def exact_refs(artifact_ids):
                return {**downstream, "input_refs": (),
                        "artifact_refs": [{"task_id": A, "artifact_id": artifact_id} for artifact_id in artifact_ids]}

            if "foreign-B" not in returned:
                return reply("", dispatch("foreign-B", exact_refs([self.foreign_id])))
            if "local-B" not in returned:
                return reply("", dispatch("local-B", exact_refs(
                    [item["artifact_id"] for item in returned["local-A"]["evidence"]])))
            return self.answer(returned["local-B"])
        if "batch" not in returned:
            independent = [task(C)] if self.case == "missing" else []
            return reply("", dispatch("batch", downstream, upstream, *independent))
        return self.answer(returned["batch"])

    @staticmethod
    def answer(batch):
        from backend.agent.tests.test_home_delegation import reply

        if batch["states"].get(B) == "blocked":
            missing, = [row for row in batch["gaps"] if row["source"] == METRICS]
            done, = [row for row in batch["evidence"] if row["task_id"] == C]
            return reply(f"计划 #{missing['arguments']['plan_id']} 缺少历史指标映射，"
                         f"依赖详情未完成。独立查询已取得计划 #{done['result']['id']} 的详情。")
        selected, = [row for row in batch["evidence"] if row["task_id"] == B and row["source"] == PLAN]
        return reply(f"已依据指标取得计划 #{selected['result']['id']} 的详情。")

    def observe(self, name, original):
        def read(**args):
            row = {"case": self.case, "tool": name, "arguments": args}
            with self.lock:
                self.reads.append(row)
                count = sum((item["case"], item["tool"], item["arguments"]) == (self.case, name, args)
                            for item in self.reads)
            self.mark(f"read:{name}:{args['plan_id']}")
            if name == METRICS and self.case in {"first", "second"} and args["plan_id"] == 101:
                self.waiting.set()
                if not self.release.wait(35):
                    row["exception"] = "barrier expired"
                    raise TimeoutError(row["exception"])
            if name == METRICS and self.case == "retry" and args["plan_id"] == 102 and count == 1:
                row["exception"] = "TimeoutError"
                raise TimeoutError("TASK-027-T1 synthetic transient read failure")
            row["result"] = original(**args)
            self.mark(f"returned:{name}:{args['plan_id']}")
            return row["result"]
        return read


class MetricSource:
    def __init__(self, path, sql):
        self.path, self.sql = path, sql
        self.snapshots = {}
        self.manifest = {"source_id": "task27-t1", "data_version": "fixture-v1", "kind": "synthetic",
                    "source_url": "fixture:task27-t1", "license": "synthetic test data",
                    "sampling": "all", "time_mapping": {"anchor_date": DAY},
                    "complete_start_date": DAY, "complete_end_date": DAY}
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE metadata (manifest TEXT)")
            conn.execute("""CREATE TABLE plan_daily_metrics
                (source_plan_id TEXT, metric_date TEXT, impressions INTEGER, clicks INTEGER)""")
            for plan_id, clicks in ((101, 120), (102, 30)):
                conn.execute("INSERT INTO plan_daily_metrics VALUES (?, ?, 1000, ?)", (str(plan_id), DAY, clicks))
                sql("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, ?)",
                    (plan_id, self.manifest["source_id"], self.manifest["data_version"], str(plan_id)))
        sql("INSERT INTO ad_plans (id, user_id, plan_name) VALUES (?, ?, ?)", (103, OWNER, OTHER_MARKER))

    def prepare(self, case):
        if case == "missing":
            self.sql("DELETE FROM plan_metric_bindings WHERE plan_id=?", (102,))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            if case == "second":
                conn.execute("UPDATE plan_daily_metrics SET clicks = 150 - clicks")
            metrics = [dict(row) for row in conn.execute("SELECT * FROM plan_daily_metrics ORDER BY source_plan_id")]
            tsv = ("source_plan_id\tmetric_date\timpressions\tclicks\n" + "".join(
                "{source_plan_id}\t{metric_date}\t{impressions}\t{clicks}\n".format(**row)
                for row in metrics)).encode("utf-8")
            manifest = {**self.manifest, "tsv_sha256": sha256(tsv).hexdigest(),
                        "compressed_sha256": sha256(compress(tsv, mtime=0)).hexdigest()}
            conn.execute("DELETE FROM metadata")
            conn.execute("INSERT INTO metadata VALUES (?)", (json.dumps(manifest),))
        self.snapshots[case] = {"metrics": metrics, "manifest": manifest, "bindings": self.sql(
            "SELECT * FROM plan_metric_bindings ORDER BY plan_id", rows=True)}


@contextmanager
def server(directory, model, source):
    import uvicorn
    from chromadb.api.shared_system_client import SharedSystemClient
    from fastapi import FastAPI
    from backend import observability
    from backend.agent.llm_provider import ProviderFactory
    from backend.captcha import captcha
    from backend.config.config import settings
    from backend.memory import conversation_memory as memory
    from backend.tools import tools
    from tests.home_diagnosis_server import demo_captcha

    control = FastAPI()

    @control.post("/case/{case}")
    def prepare(case: str):
        assert case in MESSAGES
        source.prepare(case)
        model.case = case
        model.waiting.clear()
        model.release.clear()
        return {}

    @control.get("/barrier")
    def barrier():
        with model.lock:
            return {"waiting": model.waiting.is_set(),
                    "workers": [row["task_id"] for row in model.calls
                                if row["case"] == model.case and row["phase"] == "worker"],
                    "reads": deepcopy([row for row in model.reads if row["case"] == model.case])}

    @control.post("/release")
    def release():
        model.mark("release")
        model.release.set()
        return {}

    with (patch.object(ProviderFactory, "create", return_value=model),
          patch.object(memory, "BGEEmbedding", FixedEmbedding),
          patch.object(settings, "PLAN_METRICS_DB_PATH", str(source.path)),
          patch.object(tools, PLAN, model.observe(PLAN, tools.get_plan_detail)),
          patch.object(tools, METRICS, model.observe(METRICS, tools.get_plan_metrics_by_date)),
          patch.object(observability, "TRACE_LOG_FILE", directory / "traces.jsonl"),
          patch.object(observability, "FUNC_LOG_FILE", directory / "functions.jsonl"),
          patch.object(observability, "ERROR_LOG_FILE", directory / "errors.jsonl"),
          patch.object(captcha, "generate_captcha", demo_captcha),
          socket.socket() as listener):
        from fast_server import app

        app.mount("/__task27_t1", control)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        instance = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="warning", access_log=False))
        thread = Thread(target=instance.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            model.release.set()
            instance.should_exit = True
            thread.join(60)
            assert not thread.is_alive()
            for system in SharedSystemClient._identifier_to_system.values():
                system.stop()
            SharedSystemClient.clear_system_cache()


def snapshot_sql(directory, sql):
    tables = {"groups": "chat_groups", "runs": "chat_group_runs", "entries": "chat_group_entries"}
    data = {key: sql(f"SELECT * FROM {table} ORDER BY {order}", rows=True)
            for (key, table), order in zip(tables.items(), ("conversation_id", "run_id", "id"))}
    dump(directory / "sql.json", data)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mysqld", type=Path)
    args = parser.parse_args()
    if not __debug__ or (args.mysqld and not args.mysqld.is_file()):
        parser.error("Use assertions and an existing MySQL 8.4 executable.")
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH="", API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="",
                      JWT_SECRET=token_urlsafe(48), ANONYMIZED_TELEMETRY="False", LLM_PROVIDER="mock")
    from backend.database.database import DEFAULT_DB_PATH
    from scripts.task27_t1_assertions import verify

    def business_hash():
        with DEFAULT_DB_PATH.open("rb") as stream:
            return file_digest(stream, "sha256").hexdigest()

    before = business_hash()
    model, password = FixedModel(), token_urlsafe(24)
    source = None
    report = {"status": "failed", "scenarios": []}
    try:
        with (fixture(directory, args.mysqld.resolve() if args.mysqld else None, password) as sql,
              TemporaryDirectory(dir=directory, prefix="metrics-") as scratch):
            source = MetricSource(Path(scratch) / "metrics.sqlite3", sql)
            try:
                with server(directory, model, source) as url, (directory / "browser.log").open("wb") as log:
                    subprocess.run(["node", "tests/task27_t1_browser.cjs", url, str(directory)], cwd=ROOT,
                                   env={**os.environ, "AD_TASK27_FIXTURE_PASSWORD": password},
                                   stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
            finally:
                data = snapshot_sql(directory, sql)
            verify(directory, data, model, source, report)
            report["status"] = "passed"
    except Exception as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        dump(directory / "model-inputs.json", model.calls)
        dump(directory / "tool-calls.json", model.reads)
        dump(directory / "timeline.json", model.timeline)
        if source:
            dump(directory / "source-data.json", source.snapshots)
        after = business_hash()
        report.update(database="MySQL 8.4" if args.mysqld else "SQLite",
                      business_sqlite_sha256={"before": before, "after": after})
        dump(directory / "report.json", report)
        assert before == after


if __name__ == "__main__":
    main()
