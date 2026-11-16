"""Run the FastAPI diagnosis rehearsal against a fresh loopback MySQL server."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import os
from pathlib import Path
from secrets import token_hex, token_urlsafe
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Lock, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pymysql

from backend.agent.tests.test_natural_diagnosis import BODY, ReceiptDrivenModel
from backend.agent.tests.test_plan_metrics import synthetic_artifact
from backend.agent.tests.test_plan_metrics_flow import PlanMetricsDiagnosisModel
from backend.config.config import settings
from backend.database import mysql_diagnosis
from backend.database.database import DEFAULT_DB_PATH, hash_password
from scripts.verify_mysql_t0 import _change, _query, _safe_error, isolated_mysql


SCHEMA = Path(__file__).with_name("mysql_r1_schema.sql")
STREAM = "/api/chat/stream"
REVIEWS = "/api/diagnosis/reviews"
TASKS = "/api/diagnosis/tasks"
FAULT_HEADER = {"x-mysql-r1-test": "fail-before-commit"}
CASES = (
    "startup_and_sqlite_probe", "login_commits_and_rotates_session",
    "natural_first_and_followup", "six_http_confirmations_and_independent_get",
    "rejected_requests_are_read_only", "reply_commit_failure_rolls_back",
    "structured_smoke", "readback_after_process_restart",
)


def proposal_url(action_id):
    return f"/api/diagnosis/review-proposals/{action_id}/confirm"


def dump(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False, default=str)
        stream.write("\n")


def turn_row(connect, turn_id):
    row, = _query(connect, """SELECT CONNECTION_ID() AS connection_id, t.id AS turn_id,
        t.task_id, t.reply, t.snapshot, p.action_id, p.snapshot AS proposal_snapshot
        FROM diagnosis_turns t LEFT JOIN diagnosis_review_proposals p ON p.turn_id=t.id
        WHERE t.id=%s""", (turn_id,))
    return row


def serve(port, evidence_dir):
    """Child process. Hooks are confined to the test server."""
    import uvicorn
    from backend import observability
    from backend.agent.llm_provider import ProviderFactory
    from backend.rag import rag_knowledge
    from backend.rag.review_rule_demo import seed_review_rules
    from scripts.eval_review_retrieval import FixedReviewEmbedding

    artifact_uri = Path(settings.PLAN_METRICS_DB_PATH).resolve().as_uri() + "?mode=ro"
    sqlite_connect = sqlite3.connect
    probe_path = evidence_dir / f"wire-probes-{os.getpid()}.jsonl"
    probe_lock = Lock()

    def emit(**row):
        with probe_lock, probe_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def guard(database, *args, **kwargs):
        if str(database) == artifact_uri and kwargs.get("uri") is True:
            emit(probe="readonly_metrics")
            return sqlite_connect(database, *args, **kwargs)
        emit(probe="business_sqlite_blocked")
        raise AssertionError("SQLite connection forbidden in MySQL rehearsal")

    def observer():
        return pymysql.connect(host="127.0.0.1", port=settings.DIAGNOSIS_MYSQL_PORT,
                               user=mysql_diagnosis.USER, password=settings.DIAGNOSIS_MYSQL_PASSWORD.get_secret_value(),
                               database=mysql_diagnosis.schema_name(), cursorclass=pymysql.cursors.DictCursor,
                               autocommit=True, connect_timeout=3)

    natural, structured = ReceiptDrivenModel(), PlanMetricsDiagnosisModel()

    class FixedModel:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, messages, **kwargs):
            use_natural = len(messages) > 1 and "required_queries" in (messages[1].get("content") or "")
            return (natural if use_natural else structured).create(messages, **kwargs)

    connection_class = mysql_diagnosis.MySQLDiagnosisConnection
    real_commit = connection_class.commit
    inject_failure = ContextVar("inject_failure", default=False)

    def commit(conn):
        if inject_failure.get():
            pending = conn.execute(
                """SELECT CONNECTION_ID() AS writer_connection, t.id, t.reply, p.action_id
                   FROM diagnosis_turns t JOIN diagnosis_review_proposals p ON p.turn_id=t.id
                   WHERE t.message=? AND t.reply IS NOT NULL ORDER BY t.id DESC LIMIT 1""",
                (BODY["message"],),
            ).fetchone()
            if pending and (external := turn_row(observer, pending["id"]))["reply"] is None:
                assert external["action_id"] is None
                emit(probe="before_injected_commit", turn_id=pending["id"], action_id=pending["action_id"],
                     writer_connection=pending["writer_connection"], reader_connection=external["connection_id"],
                     writer_sees_reply_and_proposal=True, reader_sees_reply_and_proposal=False)
                raise mysql_diagnosis.MySQLStorageError("Injected pre-commit failure")
        return real_commit(conn)

    def observe(app):
        async def observed(scope, receive, send):
            if scope["type"] != "http":
                return await app(scope, receive, send)
            path = scope["path"]
            injection = (b"x-mysql-r1-test", b"fail-before-commit") in scope["headers"]
            token = inject_failure.set(injection)

            async def checked_send(message):
                if path == "/api/health" and message["type"] == "http.response.start":
                    message["headers"].append((b"x-mysql-r1-pid", str(os.getpid()).encode("ascii")))
                body = message.get("body", b"")
                if path == "/api/login" and body:
                    data = json.loads(body).get("data")
                    if data and "token" in data:
                        user_id = data["user"]["user_id"]
                        session, = _query(observer, "SELECT CONNECTION_ID() AS connection_id, token FROM sessions WHERE user_id=%s", (user_id,))
                        assert session["token"] == data["token"]
                        emit(probe="before_login_response", user_id=user_id,
                             reader_connection=session["connection_id"], committed_token_matches=True)
                if path == STREAM and body.startswith(b"data: "):
                    event = json.loads(body[6:])
                    proposal = event.get("review_proposal")
                    turn_id = event.get("turn_id") or (proposal and proposal["turn_id"])
                    if turn_id and event["type"] in {"thinking", "done", "error"}:
                        row = turn_row(observer, turn_id)
                        if event["type"] == "done":
                            assert row["reply"] and row["action_id"] == (proposal and proposal["action_id"])
                            if proposal:
                                assert row["snapshot"] == row["proposal_snapshot"]
                                assert json.loads(row["snapshot"]) == {k: v for k, v in proposal.items()
                                                                       if k not in {"action_id", "turn_id"}}
                        else:
                            assert row["reply"] is None and row["action_id"] is None
                        emit(probe="before_sse_send", event=event["type"], turn_id=turn_id,
                             reader_connection=row["connection_id"], reply_visible=row["reply"] is not None,
                             proposal_visible=row["action_id"] is not None)
                await send(message)

            try:
                await app(scope, receive, checked_send)
            finally:
                inject_failure.reset(token)
        return observed

    observability.TRACE_LOG_FILE = evidence_dir / f"traces-{os.getpid()}.jsonl"
    observability.FUNC_LOG_FILE = evidence_dir / f"functions-{os.getpid()}.jsonl"
    observability.ERROR_LOG_FILE = evidence_dir / f"errors-{os.getpid()}.jsonl"
    with patch.object(sqlite3, "connect", guard), patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding), \
            patch.object(ProviderFactory, "create", return_value=FixedModel()), patch.object(connection_class, "commit", commit):
        try:
            sqlite3.connect(":memory:")
        except AssertionError:
            emit(probe="sqlite_guard_self_test", denied=True)
        seed_review_rules(rag_knowledge.get_rag_knowledge())
        from fast_server import app
        server = uvicorn.Server(uvicorn.Config(observe(app), host="127.0.0.1", port=port,
                                              log_level="critical", access_log=False, lifespan="on"))

        def stop_on_parent_exit():
            sys.stdin.readline()
            server.should_exit = True

        Thread(target=stop_on_parent_exit, daemon=True).start()
        server.run()
        return 0 if server.started else 1


@contextmanager
def fastapi_process(env, directory):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", "scripts.verify_mysql_r1",
                             "--serve", str(port), "--output-dir", str(directory)],
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=25, trust_env=False) as client:
            deadline = monotonic() + 45
            while proc.poll() is None and monotonic() < deadline:
                try:
                    readiness = client.get("/api/health", timeout=1)
                    if readiness.status_code == 200:
                        break
                except httpx.TransportError:
                    sleep(0.2)
            else:
                raise RuntimeError("FastAPI startup")
            yield client, int(readiness.headers["x-mysql-r1-pid"])
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def fixtures(connect, scratch):
    _, artifact, manifest = synthetic_artifact(scratch, ((500, 50),) * 4 + ((500, 28),) * 4)
    with closing(sqlite3.connect(artifact)) as conn, conn:
        for campaign, clicks in (("second", 44), ("third", 47), ("foreign", 2)):
            conn.execute("""INSERT INTO plan_daily_metrics SELECT ?, metric_date, impressions,
                         CASE WHEN metric_date >= '2026-10-05' THEN ? ELSE 50 END
                         FROM plan_daily_metrics WHERE source_plan_id='fixture-campaign' AND metric_date<=?""",
                         (campaign, clicks, manifest["complete_end_date"]))

    password, login_password = token_hex(32), token_urlsafe(32)
    with closing(connect()) as conn, conn.cursor() as cursor:
        cursor.execute("CREATE USER 'mysql_r1_app'@'localhost' IDENTIFIED BY %s", (password,))
        cursor.execute("GRANT SELECT ON mysql_r1.* TO 'mysql_r1_app'@'localhost'")
        for table in ("sessions", "diagnosis_tasks", "diagnosis_turns", "diagnosis_review_proposals", "diagnosis_reviews"):
            cursor.execute(f"GRANT INSERT ON mysql_r1.{table} TO 'mysql_r1_app'@'localhost'")
        for table in ("sessions", "diagnosis_tasks", "diagnosis_turns", "diagnosis_review_proposals"):
            cursor.execute(f"GRANT UPDATE ON mysql_r1.{table} TO 'mysql_r1_app'@'localhost'")
        cursor.executemany("INSERT INTO users (id, username, password) VALUES (%s,%s,%s)",
                           [(17, "owner", hash_password(login_password)), (18, "other", hash_password(login_password))])
        cursor.execute("INSERT INTO ad_accounts (id, account_name, user_id) VALUES (1, 'fixture account', 17)")
        for plan, user, campaign in ((101, 17, "fixture-campaign"), (102, 17, "second"),
                                     (103, 17, "third"), (201, 18, "foreign")):
            cursor.execute("INSERT INTO ad_plans (id, plan_name, user_id, account_id) VALUES (%s,%s,%s,%s)",
                           (plan, campaign, user, 1 if user == 17 else None))
            cursor.execute("INSERT INTO plan_metric_bindings VALUES (%s,%s,%s,%s)",
                           (plan, manifest["source_id"], manifest["data_version"], campaign))
        conn.commit()
    env = dict(os.environ, AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1",
               API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="", LLM_PROVIDER="mock",
               JWT_SECRET=token_urlsafe(48), DIAGNOSIS_SANDBOX_DB_PATH="", DIAGNOSIS_MYSQL_ENABLED="1",
               DIAGNOSIS_MYSQL_SCHEMA_VERSION="1",
               DIAGNOSIS_MYSQL_PORT=str(connect.keywords["port"]), DIAGNOSIS_MYSQL_PASSWORD=password,
               RAG_DB_PATH=str(scratch / "rag"), PLAN_METRICS_DB_PATH=str(artifact))
    return env, login_password


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def login(client, connect, name, password):
    response = client.post("/api/login", json={"username": name, "password": password})
    assert response.status_code == response.json()["code"] == 200
    data = response.json()["data"]
    row, = _query(connect, "SELECT token FROM sessions WHERE user_id=%s", (data["user"]["user_id"],))
    assert row["token"] == data["token"]
    return data["token"]


def sse(client, auth, body, *, inject=False):
    with client.stream("POST", STREAM, headers={**auth, **(FAULT_HEADER if inject else {})}, json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def assert_committed(connect, events):
    assert events[0]["type"] == "thinking"
    assert [event["type"] for event in events if event["type"] in {"error", "done"}] == ["done"]
    proposal = events[-1]["review_proposal"]
    row = turn_row(connect, proposal["turn_id"])
    assert row["reply"] and row["action_id"] == proposal["action_id"]
    assert json.loads(row["snapshot"]) == json.loads(row["proposal_snapshot"])
    assert proposal["source_trace_id"] == events[-1]["trace_id"]
    assert not proposal["evidence_gaps"] and proposal["citations"]
    return proposal, row


def fingerprints(connect):
    return {table: sha256(json.dumps(_query(connect, f"SELECT * FROM {table} ORDER BY 1, 2"),
                                     sort_keys=True, default=str).encode()).hexdigest()
            for table in sorted(mysql_diagnosis.TABLES)}


def confirm_six(client, connect, auth, action_id):
    barrier = Barrier(6)
    path = proposal_url(action_id)

    def confirm():
        barrier.wait(timeout=5)
        return client.post(path, headers=auth)

    with ThreadPoolExecutor(max_workers=6) as pool, closing(connect()) as lock, lock.cursor() as cursor:
        cursor.execute("SELECT action_id FROM diagnosis_review_proposals WHERE action_id=%s FOR UPDATE", (action_id,))
        futures = [pool.submit(confirm) for _ in range(6)]
        try:
            deadline = monotonic() + 6
            while monotonic() < deadline:
                waits = _query(connect, """SELECT DISTINCT REQUESTING_ENGINE_TRANSACTION_ID AS waiter
                    FROM performance_schema.data_lock_waits
                    WHERE BLOCKING_THREAD_ID=(SELECT THREAD_ID FROM performance_schema.threads WHERE PROCESSLIST_ID=%s)""",
                               (lock.thread_id(),))
                if len(waits) == 6:
                    break
                sleep(0.05)
            assert len(waits) == 6
            assert _query(connect, "SELECT review_id FROM diagnosis_reviews WHERE action_id=%s", (action_id,)) == []
        finally:
            lock.rollback()
        return [future.result() for future in futures], len(waits)


@contextmanager
def scenario(evidence, name):
    item = evidence["scenarios"][name] = {}
    try:
        yield item
    except Exception as error:
        item.update(status="failed", failure=_safe_error(error))
        raise
    else:
        item["status"] = "passed"


def check(connect, env, login_password, directory, evidence):
    with fastapi_process(env, directory) as (client, pid):
        with scenario(evidence, CASES[0]) as result:
            facts, = _query(connect, """SELECT VERSION() AS version, DATABASE() AS db, @@time_zone AS time_zone,
                @@transaction_isolation AS isolation, @@foreign_key_checks AS foreign_keys""")
            engines = _query(connect, """SELECT TABLE_NAME AS name, ENGINE AS engine FROM information_schema.TABLES
                                      WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME""")
            assert {item["name"]: item["engine"] for item in engines} == dict.fromkeys(mysql_diagnosis.TABLES, "InnoDB")
            assert facts["db"] == "mysql_r1" and facts["isolation"] == "REPEATABLE-READ" and facts["foreign_keys"] == 1
            assert client.get("/api/plans").status_code == 404
            result.update(facts, tables=engines, pid=pid)
        with scenario(evidence, CASES[1]) as result:
            old = headers(login(client, connect, "owner", login_password))
            auth = headers(login(client, connect, "owner", login_password))
            other = headers(login(client, connect, "other", login_password))
            statuses = [client.post(STREAM, headers=old, json=BODY).status_code,
                        client.post(proposal_url("unknown"), headers=old).status_code,
                        client.get(f"{REVIEWS}/unknown", headers=old).status_code,
                        client.get(TASKS, headers=old).status_code]
            assert statuses == [401] * 4
            assert _query(connect, "SELECT user_id FROM sessions ORDER BY user_id") == [{"user_id": 17}, {"user_id": 18}]
            assert client.post(STREAM, headers=auth, json={"message": "scope probe"}).status_code == 404
            result.update(logins=[200] * 3, old_session=statuses, committed_session_users=[17, 18])
        with scenario(evidence, CASES[2]) as result:
            first_events = result["first_sse"] = sse(client, auth, BODY)
            first, sql_first = assert_committed(connect, first_events)
            follow = {"message": "继续这次比较，改成最近三天和之前三天", "diagnosis": {
                "mode": "natural", "task_id": first["task_id"], "source_turn_id": first["turn_id"],
            }}
            next_events = result["followup_sse"] = sse(client, auth, follow)
            second, sql_second = assert_committed(connect, next_events)
            assert first["task_id"] == second["task_id"] and second["source_turn_id"] == first["turn_id"]
            for key in ("turn_id", "source_trace_id", "baseline", "current"):
                assert first[key] != second[key]
            task = client.get(f"{TASKS}/{first['task_id']}", headers=auth)
            assert task.status_code == 200
            assert [t["turn_id"] for t in task.json()["data"]["turns"]] == [first["turn_id"], second["turn_id"]]
            assert task.json()["data"]["current"] == second["current"]
            assert client.get(TASKS, headers=auth).json()["data"]["tasks"][0]["task_id"] == first["task_id"]
            citation = first["citations"][0]
            chunk = client.get(citation["url"])
            assert chunk.status_code == 200 and chunk.json()["chunk_id"] == citation["chunk_id"]
            result.update(sql_turns=[sql_first, sql_second], followup_request=follow, task_history=task.json()["data"],
                          citation_get=chunk.json())
        with scenario(evidence, CASES[3]) as result:
            before, = _query(connect, "SELECT COUNT(*) AS n FROM diagnosis_reviews")
            responses, waits = confirm_six(client, connect, auth, first["action_id"])
            assert [r.status_code for r in responses] == [200] * 6
            receipt = responses[0].json()["data"]
            assert all(r.json()["data"] == receipt for r in responses)
            assert all(receipt[key] == value for key, value in first.items())
            read = client.get(f"{REVIEWS}/{receipt['review_id']}", headers=auth)
            assert read.status_code == 200 and read.json()["data"] == receipt
            sql, = _query(connect, "SELECT CONNECTION_ID() AS connection_id, r.* FROM diagnosis_reviews r")
            assert before["n"] == 0 and sql["review_id"] == receipt["review_id"]
            assert sql["snapshot"] == sql_first["snapshot"] and sql["turn_id"] == first["turn_id"]
            result.update(confirm_statuses=[200] * 6, review_ids=[receipt["review_id"]] * 6,
                          simultaneous_lock_waiters=waits, rows_added=1, get=read.json(), sql=sql)
        with scenario(evidence, CASES[4]) as result:
            assert _change(connect, "UPDATE ad_plans SET status='paused' WHERE id=103") == 1
            before = fingerprints(connect)
            responses = {
                "foreign_pending_confirm": client.post(proposal_url(second["action_id"]), headers=other),
                "foreign_approved_confirm": client.post(proposal_url(first["action_id"]), headers=other),
                "unknown_confirm": client.post(proposal_url("unknown"), headers=auth),
                "foreign_read": client.get(f"{REVIEWS}/{receipt['review_id']}", headers=other),
                "unknown_read": client.get(f"{REVIEWS}/unknown", headers=auth),
                "foreign_continue": client.post(STREAM, headers=other, json=follow),
                "unknown_continue": client.post(STREAM, headers=auth, json={
                    **follow, "diagnosis": {**follow["diagnosis"], "task_id": "unknown"},
                }),
                "stale_confirm": client.post(proposal_url(second["action_id"]), headers=auth),
            }
            for name, response in responses.items():
                assert response.status_code == (409 if name == "stale_confirm" else 404), name
            for first_name, second_name in (("foreign_pending_confirm", "unknown_confirm"),
                                            ("foreign_approved_confirm", "unknown_confirm"),
                                            ("foreign_read", "unknown_read"), ("foreign_continue", "unknown_continue")):
                assert responses[first_name].json() == responses[second_name].json()
            after = fingerprints(connect)
            assert after == before
            result.update(http={name: {"status": r.status_code, "body": r.json()} for name, r in responses.items()},
                          all_table_fingerprints_unchanged=True, fingerprints_before=before, fingerprints_after=after)
            assert _change(connect, "UPDATE ad_plans SET status='active' WHERE id=103") == 1
        with scenario(evidence, CASES[5]) as result:
            events = result["sse"] = sse(client, auth, BODY, inject=True)
            assert [event["type"] for event in events] == ["thinking", "error"]
            row = turn_row(connect, events[-1]["turn_id"])
            assert row["reply"] is None and row["action_id"] is None
            snapshot = json.loads(row["snapshot"])
            assert "metrics" not in snapshot and "comparison" not in snapshot
            probes = read_probes(directory, pid)
            injected, = [p for p in probes if p["probe"] == "before_injected_commit"]
            assert injected["turn_id"] == row["turn_id"] and injected["writer_connection"] != injected["reader_connection"]
            assert _query(connect, "SELECT action_id FROM diagnosis_review_proposals WHERE action_id=%s",
                          (injected["action_id"],)) == []
            result.update(sql=row, in_transaction_probe=injected)
        with scenario(evidence, CASES[6]) as result:
            windows = {"baseline": {"start_date": "2026-10-01", "end_date": "2026-10-04"},
                       "current": {"start_date": "2026-10-05", "end_date": "2026-10-08"}}
            events = result["sse"] = sse(client, auth, {
                "diagnosis": {"plan_id": "101", "rule_id": "ctr-review-v1#low-ctr", **windows},
            })
            proposal, sql = assert_committed(connect, events)
            assert proposal["plan_id"] == "101" and proposal["rule_evaluation"]["matched"]
            assert "pause_proposal" not in events[-1]
            response = client.post(proposal_url(proposal["action_id"]), headers=auth)
            assert response.status_code == 200 and response.json()["data"]["plan_id"] == "101"
            assert client.get(f"{REVIEWS}/{response.json()['data']['review_id']}", headers=auth).json() == response.json()
            result.update(sql=sql, confirm=response.json())
        probes = read_probes(directory, pid)
        assert sum(p["probe"] == "before_login_response" for p in probes) == 3
        assert sum(p["probe"] == "before_sse_send" and p["event"] == "done" for p in probes) == 3
        assert sum(p["probe"] == "business_sqlite_blocked" for p in probes) == 1
        assert any(p["probe"] == "readonly_metrics" for p in probes)
        evidence["scenarios"][CASES[0]].update(business_sqlite_attempts=0, sqlite_probe_self_tests=1,
                                             readonly_metrics_connections=sum(p["probe"] == "readonly_metrics" for p in probes))
    with scenario(evidence, CASES[7]) as result, fastapi_process(env, directory) as (client, restarted_pid):
        assert pid != restarted_pid
        response = client.get(f"{REVIEWS}/{receipt['review_id']}", headers=auth)
        assert response.status_code == 200 and response.json()["data"] == receipt
        assert client.get(f"{TASKS}/{first['task_id']}", headers=auth).json()["data"] == task.json()["data"] | {
            "turns": [{**task.json()["data"]["turns"][0], "review": {
                "action_id": first["action_id"], "review_id": receipt["review_id"], "state": "approved",
            }}, task.json()["data"]["turns"][1]],
        }
        assert sum(p["probe"] == "business_sqlite_blocked" for p in read_probes(directory, restarted_pid)) == 1
        result.update(previous_pid=pid, restarted_pid=restarted_pid, status_code=200, response=response.json())


def read_probes(directory, pid):
    return [json.loads(line) for line in (directory / f"wire-probes-{pid}.jsonl").read_text(encoding="utf-8").splitlines()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mysqld", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    if args.serve:
        return serve(args.serve, directory)
    if not __debug__ or not args.mysqld or not args.mysqld.is_file() or directory.exists():
        parser.error("Use assertions, an existing mysqld binary and a new output directory")
    directory.mkdir(parents=True)
    evidence = {"scenarios": {}}
    business_before = sha256(DEFAULT_DB_PATH.read_bytes()).hexdigest() if DEFAULT_DB_PATH.is_file() else None
    try:
        with isolated_mysql(args.mysqld.resolve(), schema=SCHEMA, schema_name="mysql_r1") as connect, \
                TemporaryDirectory(prefix="mysql-r1-fixtures-") as temporary:
            env, login_password = fixtures(connect, Path(temporary))
            check(connect, env, login_password, directory, evidence)
    except Exception as error:
        evidence["interruption"] = _safe_error(error)
    for name in CASES:
        evidence["scenarios"].setdefault(name, {"status": "skipped"})
    evidence["totals"] = {status: sum(case["status"] == status for case in evidence["scenarios"].values())
                          for status in ("passed", "failed", "skipped")}
    business_after = sha256(DEFAULT_DB_PATH.read_bytes()).hexdigest() if DEFAULT_DB_PATH.is_file() else None
    evidence["business_sqlite_sha256"] = {"before": business_before, "after": business_after}
    dump(directory / "report.json", evidence)
    print(json.dumps(evidence["totals"]))
    return int(evidence["totals"]["passed"] != len(CASES) or business_before != business_after or "interruption" in evidence)


if __name__ == "__main__":
    raise SystemExit(main())
