"""Run the R2 MySQL pause sandbox through FastAPI, Chromium and independent SQL."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Lock, Thread
from unittest.mock import patch

import httpx
import pymysql

from backend.agent.tests.test_plan_metrics import synthetic_artifact
from backend.config.config import settings
from backend.database import mysql_diagnosis
from backend.database.database import DEFAULT_DB_PATH, hash_password
from backend.database.diagnosis_pauses import PAUSE_RULE_ID
from scripts.migrate_mysql_r2 import DDL, import_copy, source_digest
from scripts.verify_mysql_r1 import STREAM, TASKS, dump, headers, login, proposal_url, scenario
from scripts.verify_mysql_r2 import runtime_env, target_snapshot
from scripts.verify_mysql_r3 import wait_for
from scripts.verify_mysql_t0 import _change, _query, _safe_error, isolated_mysql


SOURCE = Path("logs/mysql-r2/run-05/sanitized.sqlite3")
PAUSE_PATH = "/api/diagnosis/pause-actions/"
CASES = (
    "default_disabled", "migrated_receipt", "proposal_guards", "proposal_commit_rollback",
    "chromium_approval_and_readback", "six_concurrent_approvals", "receipt_write_rollback",
    "lost_committed_response", "invalid_session_and_cross_user", "status_changed",
    "transferred_and_reregistered", "restart_readback", "no_business_sqlite",
)


def state(connect, action_id):
    with closing(connect()) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT CONNECTION_ID() AS id")
        connection_id = cursor.fetchone()["id"]
        cursor.execute("SELECT * FROM ad_plans ORDER BY id")
        plans = list(cursor.fetchall())
        cursor.execute("SELECT * FROM diagnosis_pause_receipts WHERE action_id=%s", (action_id,))
        receipts = list(cursor.fetchall())
        for row in (*plans, *receipts):
            if row["created_at"] is not None:
                row["created_at"] = str(row["created_at"])
        return {"connection_id": connection_id, "plans": plans, "receipts": receipts}


def turn(connect, turn_id):
    row, = _query(connect, """SELECT CONNECTION_ID() AS connection_id, t.id, t.reply, t.snapshot,
        r.action_id AS review_action_id, p.action_id AS pause_action_id, p.registration_id
        FROM diagnosis_turns t LEFT JOIN diagnosis_review_proposals r ON r.turn_id=t.id
        LEFT JOIN diagnosis_pause_proposals p ON p.review_action_id=r.action_id WHERE t.id=%s""", (turn_id,))
    return row


def structured_turn_id(connect, event):
    row, = _query(connect, """SELECT id FROM diagnosis_turns WHERE task_id=%s
        AND JSON_UNQUOTE(JSON_EXTRACT(snapshot, '$.source_trace_id'))=%s""", (event["task_id"], event["trace_id"]))
    return row["id"]


def serve(port, directory):
    import uvicorn
    from backend import observability
    from backend.agent.llm_provider import ProviderFactory
    from backend.agent.tests.test_plan_metrics_flow import PlanMetricsDiagnosisModel
    from backend.captcha import captcha
    from backend.rag import rag_knowledge
    from backend.rag.review_rule_demo import seed_review_rules
    from scripts.eval_review_retrieval import FixedReviewEmbedding
    from tests.home_diagnosis_server import demo_captcha

    artifact = Path(settings.PLAN_METRICS_DB_PATH).resolve().as_uri() + "?mode=ro"
    probe_path = directory / f"probes-{os.getpid()}.jsonl"
    probe_lock = Lock()
    fault = ContextVar("fault", default="")
    sqlite_connect = sqlite3.connect
    connection = mysql_diagnosis.MySQLDiagnosisConnection
    execute, commit = connection.execute, connection.commit

    def emit(probe, **details):
        with probe_lock, probe_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"probe": probe, **details}, default=str, ensure_ascii=False) + "\n")

    def guard(filename, *args, **kwargs):
        if str(filename) == artifact and kwargs.get("uri") is True:
            emit("readonly_metrics")
            return sqlite_connect(filename, *args, **kwargs)
        emit("business_sqlite_blocked", metrics_uri=str(filename).endswith("?mode=ro"))
        raise AssertionError("Business SQLite accessed by the MySQL application")

    def observer():
        return pymysql.connect(
            host="127.0.0.1", port=settings.DIAGNOSIS_MYSQL_PORT, user=mysql_diagnosis.USER,
            password=settings.DIAGNOSIS_MYSQL_PASSWORD.get_secret_value(), database=mysql_diagnosis.schema_name(),
            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, init_command="SET time_zone='+00:00'",
        )

    def checked_execute(conn, sql, params=()):
        if sql.startswith("INSERT INTO diagnosis_pause_receipts") and fault.get() == "receipt-write":
            writer = execute(conn, "SELECT CONNECTION_ID() AS id, status FROM ad_plans WHERE id=101").fetchone()
            outside = state(observer, params[1])
            assert writer["status"] == "paused" and outside["plans"][0]["status"] == "active"
            assert writer["id"] != outside["connection_id"] and not outside["receipts"]
            emit("receipt_write_fault", action_id=params[1], writer=writer, outside=outside)
            raise mysql_diagnosis.MySQLStorageError("Injected receipt insert failure")
        result = execute(conn, sql, params)
        if sql.startswith("UPDATE ad_plans SET status='paused'"):
            emit("plan_update", plan_id=params[0], changed=result.rowcount)
        return result

    def checked_commit(conn):
        if fault.get() == "proposal-commit":
            pending = execute(conn, """SELECT CONNECTION_ID() AS writer_connection, t.id, p.action_id
                FROM diagnosis_turns t JOIN diagnosis_review_proposals r ON r.turn_id=t.id
                JOIN diagnosis_pause_proposals p ON p.review_action_id=r.action_id ORDER BY t.id DESC LIMIT 1""").fetchone()
            if pending:
                outside = turn(observer, pending["id"])
                assert pending["writer_connection"] != outside["connection_id"]
                if outside["reply"] is None:
                    assert outside["review_action_id"] is None and outside["pause_action_id"] is None
                    emit("proposal_commit_fault", writer=pending, outside=outside)
                    raise mysql_diagnosis.MySQLStorageError("Injected proposal commit failure")
        return commit(conn)

    observability.TRACE_LOG_FILE = directory / f"traces-{os.getpid()}.jsonl"
    observability.FUNC_LOG_FILE = directory / f"functions-{os.getpid()}.jsonl"
    observability.ERROR_LOG_FILE = directory / f"errors-{os.getpid()}.jsonl"
    with (patch.object(sqlite3, "connect", guard),
          patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding),
          patch.object(ProviderFactory, "create", side_effect=lambda *args, **kwargs: PlanMetricsDiagnosisModel()),
          patch.object(captcha, "generate_captcha", demo_captcha),
          patch.object(connection, "execute", checked_execute),
          patch.object(connection, "commit", checked_commit),
          closing(rag_knowledge.get_rag_knowledge()) as rag):
        seed_review_rules(rag)
        import fast_server

        validate = fast_server.mysql_auth_instance.validate_token

        def validate_with_race(token, **kwargs):
            result = validate(token, **kwargs)
            if result and not kwargs and fault.get() == "revoke-after-precheck":
                with closing(mysql_diagnosis.connect()) as conn, conn:
                    conn.execute("UPDATE sessions SET token=NULL WHERE user_id=?", (result["user_id"],))
                emit("session_revoked_after_precheck", user_id=result["user_id"])
            return result

        async def observed(scope, receive, send):
            if scope["type"] != "http":
                return await fast_server.app(scope, receive, send)
            path = scope["path"]
            token = fault.set(dict(scope["headers"]).get(b"x-r4-test", b"").decode("ascii"))

            async def checked_send(message):
                if path == "/api/health" and message["type"] == "http.response.start":
                    message["headers"].append((b"x-r4-pid", str(os.getpid()).encode("ascii")))
                body = message.get("body", b"")
                if path == STREAM and body.startswith(b"data: "):
                    event = json.loads(body[6:])
                    if event["type"] == "done":
                        row = turn(observer, structured_turn_id(observer, event))
                        proposal = event.get("pause_proposal")
                        assert row["reply"] and row["pause_action_id"] == (proposal and proposal["action_id"])
                        emit("done_before_send", turn=row, proposal=proposal,
                             state=state(observer, row["pause_action_id"]))
                if body and (path.startswith(PAUSE_PATH) or path.startswith("/api/diagnosis/review-proposals/")):
                    data = json.loads(body).get("data")
                    if data and path.endswith("/confirm"):
                        is_pause = path.startswith(PAUSE_PATH)
                        if is_pause:
                            outside = state(observer, data["action_id"])
                            emit("pause_before_send", data=data, state=outside)
                            if fault.get() == "drop-response":
                                assert outside["receipts"][0]["receipt_id"] == data["receipt_id"]
                                emit("response_dropped_after_commit", action_id=data["action_id"], state=outside)
                                raise ConnectionAbortedError("Injected HTTP response loss after commit")
                        else:
                            review, = _query(observer, """SELECT r.review_id, p.action_id AS pause_action_id
                                FROM diagnosis_reviews r LEFT JOIN diagnosis_pause_proposals p
                                ON p.review_action_id=r.action_id WHERE r.action_id=%s""", (data["action_id"],))
                            assert review["review_id"] == data["review_id"]
                            emit("review_before_send", data=data, state=state(observer, review["pause_action_id"]),
                                 pause_action_id=review["pause_action_id"])
                await send(message)

            try:
                await fast_server.app(scope, receive, checked_send)
            finally:
                fault.reset(token)

        server = uvicorn.Server(uvicorn.Config(observed, host="127.0.0.1", port=port,
                                               log_level="error", access_log=False))

        def stop():
            sys.stdin.readline()
            server.should_exit = True

        with patch.object(fast_server.mysql_auth_instance, "validate_token", validate_with_race):
            Thread(target=stop, daemon=True).start()
            server.run()
        return int(not server.started)


@contextmanager
def api_process(env, directory):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log = directory / f"api-{port}.log"
    with log.open("wb") as output:
        proc = subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", "-m", "scripts.verify_mysql_r4", "--serve", str(port),
             "--output-dir", str(directory)], env=env, stdin=subprocess.PIPE, stdout=output, stderr=output,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False) as client:
                def ready():
                    if proc.poll() is not None:
                        raise RuntimeError(f"FastAPI failed to start. See {log.name}")
                    try:
                        response = client.get("/api/health", timeout=0.5)
                    except httpx.TransportError:
                        return None
                    return response if response.status_code == 200 else None

                yield client, int(wait_for(ready).headers["x-r4-pid"])
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def run_registration(env, plan_id, user_id, *, expected=0):
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", "backend.database.diagnosis_pauses",
         "--user-id", str(user_id), "--plan-id", str(plan_id)], env=env, capture_output=True,
        timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == expected, result.stderr.decode("utf-8", errors="replace")
    if expected:
        assert "同属主" in result.stderr.decode("utf-8", errors="replace")


def fixture(connect, scratch):
    assert import_copy(SOURCE.resolve(), connect)["status"] == "imported"
    _, artifact, manifest = synthetic_artifact(scratch, (
        (1000, 100), (1000, 80), (2000, 200), (2000, 160), (2000, 100), (2000, 95),
    ))
    env = runtime_env(connect, artifact, scratch)
    env["MYSQL_CHAT_MEMORY_ROOT"] = ""
    env.pop("DIAGNOSIS_MYSQL_PAUSE_ENABLED", None)
    password = token_urlsafe(24)
    with closing(connect()) as conn, conn.cursor() as cursor:
        cursor.execute("UPDATE users SET password=%s WHERE id IN (17,18)", (hash_password(password),))
        cursor.execute("UPDATE plan_metric_bindings SET source_id=%s, data_version=%s, source_plan_id='fixture-campaign'",
                       (manifest["source_id"], manifest["data_version"]))
        conn.commit()
    return env, password


def grant_pause(connect):
    with closing(connect()) as conn, conn.cursor() as cursor:
        for table in ("diagnosis_pause_proposals", "diagnosis_pause_receipts", "diagnosis_sandbox_targets"):
            cursor.execute(f"GRANT INSERT ON mysql_r2.{table} TO 'mysql_r1_app'@'localhost'")
        cursor.execute("GRANT UPDATE (status) ON mysql_r2.ad_plans TO 'mysql_r1_app'@'localhost'")


def diagnose(client, auth, *, plan_id=101, rule=PAUSE_RULE_ID, days=(1, 2), task_id=None, fault="", terminal="done"):
    params = {"task_id": task_id} if task_id else {"plan_id": plan_id, "rule_id": rule}
    for window, day in zip(("baseline", "current"), days):
        date = f"2026-10-{day:02}"
        params[window] = {"start_date": date, "end_date": date}
    response = client.post(STREAM, headers={**auth, "x-r4-test": fault}, json={"diagnosis": params})
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event["type"] for event in events if event["type"] in {"done", "error"}] == [terminal], events
    return events


def action(client, auth, action_id, *, read=False, fault=""):
    path = PAUSE_PATH + action_id
    return client.request("GET" if read else "POST", path if read else path + "/confirm",
                          headers={**auth, "x-r4-test": fault})


def data(response):
    assert response.status_code == response.json()["code"] == 200, response.text
    return response.json()["data"]


def probes(directory, pid, name):
    path = directory / f"probes-{pid}.jsonl"
    with path.open(encoding="utf-8") as stream:
        return [row for line in stream if (row := json.loads(line))["probe"] == name]


def only_target_changed(before, after):
    assert before["plans"][0]["id"] == 101 and before["plans"][0]["status"] == "active"
    assert after["plans"] == [{**before["plans"][0], "status": "paused"}, *before["plans"][1:]]
    assert not before["receipts"] and len(after["receipts"]) == 1


def confirm_six(client, connect, auth, action_id):
    barrier = Barrier(6)

    def confirm():
        barrier.wait(timeout=5)
        return action(client, auth, action_id)

    with ThreadPoolExecutor(max_workers=6) as pool, closing(connect()) as lock, lock.cursor() as cursor:
        cursor.execute("SELECT user_id FROM sessions WHERE user_id=17 FOR UPDATE")
        futures = [pool.submit(confirm) for _ in range(6)]
        try:
            def all_waiting():
                waits = _query(connect, """SELECT DISTINCT w.REQUESTING_THREAD_ID AS thread
                    FROM performance_schema.data_lock_waits w
                    JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID
                    WHERE l.OBJECT_SCHEMA='mysql_r2' AND l.OBJECT_NAME='sessions'""")
                return waits if len(waits) == 6 else None

            waits = wait_for(all_waiting)
            assert not state(connect, action_id)["receipts"]
        finally:
            lock.rollback()
        return [data(future.result()) for future in futures], waits


def reset_plan(connect):
    assert _change(connect, "UPDATE ad_plans SET status='active' WHERE id=101 AND status='paused'") == 1


def verify(connect, env, password, directory, evidence):
    historical = json.loads(SOURCE.with_name("sqlite-business-evidence.json").read_text(encoding="utf-8"))["pause"]
    historical_id = historical["action_id"]
    with scenario(evidence, "default_disabled") as item, api_process(env, directory) as (client, pid):
        owner = headers(login(client, connect, "diagnosis-demo", password))
        reset_plan(connect)
        assert _query(connect, "SELECT registration_id FROM diagnosis_sandbox_targets WHERE plan_id=101")
        before = state(connect, historical_id)
        proposals = _query(connect, "SELECT * FROM diagnosis_pause_proposals")
        event = diagnose(client, owner)[-1]
        assert event["review_proposal"]["rule_evaluation"]["matched"] and "pause_proposal" not in event
        assert action(client, owner, historical_id).status_code == 404
        assert action(client, owner, historical_id, read=True).status_code == 404
        after = state(connect, historical_id)
        assert before["plans"] == after["plans"] and before["receipts"] == after["receipts"]
        assert proposals == _query(connect, "SELECT * FROM diagnosis_pause_proposals")
        item.update(pid=pid, before=before, after=after, done=event, confirm=404, read=404)

    # The migrated plan is restored before reading its original receipt.
    assert _change(connect, "UPDATE ad_plans SET status='paused' WHERE id=101") == 1
    grant_pause(connect)
    env["DIAGNOSIS_MYSQL_PAUSE_ENABLED"] = "1"
    with api_process(env, directory) as (client, pid):
        owner = headers(login(client, connect, "diagnosis-demo", password))
        other = headers(login(client, connect, "second-demo", password))
        with scenario(evidence, "migrated_receipt") as item:
            before = target_snapshot(connect)
            read = data(action(client, owner, historical_id, read=True))
            assert read == {"receipt": historical, "actual_status": "paused"}
            assert data(action(client, owner, historical_id)) == historical
            task = data(client.get(f"{TASKS}/{historical['task_id']}", headers=owner))
            assert task["turns"][0]["pause"]["state"] == "approved"
            assert before == target_snapshot(connect)
            item.update(read=read, sql=state(connect, historical_id), history=task["turns"][0]["pause"])

        reset_plan(connect)
        with scenario(evidence, "proposal_guards") as item:
            before = state(connect, historical_id)
            proposals = _query(connect, "SELECT * FROM diagnosis_pause_proposals")
            cases = {
                "missing_window": diagnose(client, owner, days=(1, 10))[-1],
                "below_threshold": diagnose(client, owner, days=(5, 6))[-1],
                "other_rule_version": diagnose(client, owner, rule="ctr-review-v2#low-ctr", days=(3, 4))[-1],
                "unregistered": diagnose(client, owner, plan_id=102)[-1],
                "foreign": diagnose(client, owner, plan_id=201)[-1],
            }
            assert cases["missing_window"]["review_proposal"]["evidence_gaps"]
            assert cases["below_threshold"]["review_proposal"]["rule_evaluation"]["matched"] is False
            assert all(cases[key]["review_proposal"]["rule_evaluation"]["matched"] is True
                       for key in ("other_rule_version", "unregistered"))
            assert all("pause_proposal" not in event for event in cases.values())
            assert "review_proposal" not in cases["foreign"]
            run_registration(env, 201, 17, expected=2)
            after = state(connect, historical_id)
            assert before["plans"] == after["plans"] and before["receipts"] == after["receipts"]
            assert proposals == _query(connect, "SELECT * FROM diagnosis_pause_proposals")
            item.update(cases=cases, cross_owner_registration="rejected")

        with scenario(evidence, "proposal_commit_rollback") as item:
            events = diagnose(client, owner, fault="proposal-commit", terminal="error")
            assert all(event["type"] != "text" and "pause_proposal" not in event for event in events)
            row = turn(connect, structured_turn_id(connect, events[0]))
            assert row["reply"] is None and row["pause_action_id"] is None and row["review_action_id"] is None
            probe, = probes(directory, pid, "proposal_commit_fault")
            item.update(events=events, rolled_back=row, fault=probe)

        with scenario(evidence, "chromium_approval_and_readback") as item:
            before = state(connect, None)
            browser_log = directory / "browser.log"
            with browser_log.open("wb") as output:
                subprocess.run(["node", "tests/home_mysql_pause_browser.cjs", str(client.base_url), str(directory)],
                               env={**os.environ, "R4_BROWSER_PASSWORD": password}, check=True, timeout=90,
                               stdout=output, stderr=output,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            browser = json.loads((directory / "browser.json").read_text(encoding="utf-8"))
            receipt = browser["read"]["receipt"]
            action_id = receipt["action_id"]
            proposed, = [row for row in probes(directory, pid, "done_before_send")
                         if row["turn"]["pause_action_id"] == action_id]
            review, = [row for row in probes(directory, pid, "review_before_send")
                       if row["data"]["turn_id"] == receipt["turn_id"]]
            after = state(connect, action_id)
            assert review["pause_action_id"] == action_id
            assert proposed["state"]["plans"] == review["state"]["plans"] == before["plans"]
            assert not proposed["state"]["receipts"] and not review["state"]["receipts"]
            only_target_changed(before, after)
            assert all(receipt[key] == val for key, val in proposed["proposal"].items())
            assert all(receipt[key] == val for key, val in json.loads(proposed["turn"]["snapshot"]).items())
            assert after["receipts"][0]["receipt_id"] == receipt["receipt_id"]
            item.update(before=before, proposed=proposed, review=review, after=after, browser=browser)

        # Chromium logged in as the owner and rotated the earlier session.
        owner = headers(login(client, connect, "diagnosis-demo", password))
        reset_plan(connect)
        with scenario(evidence, "six_concurrent_approvals") as item:
            proposal = diagnose(client, owner)[-1]["pause_proposal"]
            action_id = proposal["action_id"]
            task = data(client.get(f"{TASKS}/{proposal['task_id']}", headers=owner))
            assert task["turns"][0]["pause"]["state"] == "pending"
            assert action(client, owner, action_id, read=True).status_code == 404
            before = state(connect, action_id)
            updates = len(probes(directory, pid, "plan_update"))
            receipts, waits = confirm_six(client, connect, owner, action_id)
            assert receipts == [receipts[0]] * 6
            assert all(receipts[0][key] == value for key, value in proposal.items())
            after = state(connect, action_id)
            only_target_changed(before, after)
            assert len(probes(directory, pid, "plan_update")) == updates + 1
            item.update(before=before, after=after, receipts=receipts, simultaneous_lock_waits=waits, plan_updates=1)

        reset_plan(connect)
        with scenario(evidence, "receipt_write_rollback") as item:
            action_id = diagnose(client, owner)[-1]["pause_proposal"]["action_id"]
            before = state(connect, action_id)
            response = action(client, owner, action_id, fault="receipt-write")
            assert response.status_code == 503
            rolled_back = state(connect, action_id)
            assert rolled_back["plans"] == before["plans"] and not rolled_back["receipts"]
            assert action(client, owner, action_id, read=True).status_code == 404
            receipt = data(action(client, owner, action_id))
            after = state(connect, action_id)
            only_target_changed(before, after)
            item.update(http=503, fault=probes(directory, pid, "receipt_write_fault")[0], rolled_back=rolled_back,
                        retry=receipt, after=after)

        reset_plan(connect)
        with scenario(evidence, "lost_committed_response") as item:
            action_id = diagnose(client, owner)[-1]["pause_proposal"]["action_id"]
            before = state(connect, action_id)
            try:
                action(client, owner, action_id, fault="drop-response")
            except httpx.RemoteProtocolError as error:
                item["transport_error"] = type(error).__name__
            else:
                raise AssertionError("Expected a lost response")
            after_loss = state(connect, action_id)
            only_target_changed(before, after_loss)
            receipt = data(action(client, owner, action_id))
            assert receipt["receipt_id"] == after_loss["receipts"][0]["receipt_id"]
            lost_read = data(action(client, owner, action_id, read=True))
            assert lost_read == {"receipt": receipt, "actual_status": "paused"}
            after_retry = state(connect, action_id)
            assert after_retry["plans"] == after_loss["plans"] and after_retry["receipts"] == after_loss["receipts"]
            item.update(after_loss=after_loss, after_retry=after_retry, read=lost_read)

        reset_plan(connect)
        with scenario(evidence, "invalid_session_and_cross_user") as item:
            action_id = diagnose(client, owner)[-1]["pause_proposal"]["action_id"]
            stale = owner
            owner = headers(login(client, connect, "diagnosis-demo", password))
            before = target_snapshot(connect)
            for reading in (False, True):
                for auth, status in ((stale, 401), ({}, 401), (other, 404)):
                    assert action(client, auth, action_id, read=reading).status_code == status
                assert action(client, other, historical_id, read=reading).status_code == 404
            assert target_snapshot(connect) == before
            response = action(client, owner, action_id, fault="revoke-after-precheck")
            assert response.status_code == 401
            after = target_snapshot(connect)
            assert {key for key in before if before[key] != after[key]} == {"sessions"}
            item.update(stale=401, missing=401, foreign=404, after_precheck=401, business_fingerprints_equal=True)
            owner = headers(login(client, connect, "diagnosis-demo", password))

        with scenario(evidence, "status_changed") as item:
            action_id = diagnose(client, owner)[-1]["pause_proposal"]["action_id"]
            assert _change(connect, "UPDATE ad_plans SET status='paused' WHERE id=101") == 1
            before = target_snapshot(connect)
            assert action(client, owner, action_id).status_code == 409
            assert before == target_snapshot(connect)
            item.update(http=409, sql=state(connect, action_id), table_fingerprints_equal=True)

    with scenario(evidence, "restart_readback") as item, api_process(env, directory) as (client, restarted):
        assert restarted != pid
        browser_read = browser["read"]
        assert data(action(client, owner, browser_read["receipt"]["action_id"], read=True)) == browser_read
        lost_action = lost_read["receipt"]["action_id"]
        assert data(action(client, owner, lost_action, read=True)) == lost_read
        assert data(action(client, owner, lost_action)) == lost_read["receipt"]
        reset_plan(connect)
        assert data(action(client, owner, lost_action, read=True)) == {**lost_read, "actual_status": "active"}
        item.update(pid_before=pid, pid_after=restarted, browser_read=browser_read, retry_read=lost_read,
                    actual_status_after_fixture_reset="active")

        with scenario(evidence, "transferred_and_reregistered") as transfer:
            proposal = diagnose(client, owner)[-1]["pause_proposal"]
            action_id = proposal["action_id"]
            old, = _query(connect, "SELECT * FROM diagnosis_sandbox_targets WHERE plan_id=101")
            assert _change(connect, "UPDATE ad_plans SET user_id=18 WHERE id=101") == 1
            assert not _query(connect, "SELECT * FROM diagnosis_sandbox_targets WHERE plan_id=101")
            before = target_snapshot(connect)
            assert action(client, owner, action_id).status_code == 404
            assert action(client, other, action_id).status_code == 404
            assert before == target_snapshot(connect)
            assert _change(connect, "UPDATE ad_plans SET user_id=17 WHERE id=101") == 1
            run_registration(env, 101, 17)
            new, = _query(connect, "SELECT * FROM diagnosis_sandbox_targets WHERE plan_id=101")
            assert new["registration_id"] != old["registration_id"]
            run_registration(env, 101, 17)
            assert _query(connect, "SELECT * FROM diagnosis_sandbox_targets WHERE plan_id=101") == [new]
            before = target_snapshot(connect)
            assert action(client, owner, action_id).status_code == 404
            assert action(client, owner, historical_id, read=True).status_code == 404
            assert before == target_snapshot(connect)
            fresh = diagnose(client, owner)[-1]["pause_proposal"]
            assert fresh["action_id"] != action_id
            assert data(action(client, owner, fresh["action_id"]))["committed_status"] == "paused"
            transfer.update(old=old, new=new, old_action=action_id, new_action=fresh["action_id"],
                            transferred=404, reregistered=404, old_read=404, table_fingerprints_equal=True)

    with scenario(evidence, "no_business_sqlite") as item:
        for process in (evidence["scenarios"]["default_disabled"]["pid"], pid, restarted):
            assert not probes(directory, process, "business_sqlite_blocked")
        item.update(business_sqlite_connections=0, readonly_metric_connections=sum(
            len(probes(directory, process, "readonly_metrics"))
            for process in (evidence["scenarios"]["default_disabled"]["pid"], pid, restarted)
        ))


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
    files = {"business_sqlite": DEFAULT_DB_PATH, "r2_source": SOURCE}
    before = {key: source_digest(path) for key, path in files.items()}
    evidence = {"scenarios": {}}
    try:
        with TemporaryDirectory(prefix="mysql-r4-fixture-") as scratch, \
                isolated_mysql(args.mysqld.resolve(), schema=DDL, schema_name="mysql_r2") as connect:
            evidence["mysql"], = _query(connect, "SELECT VERSION() AS version, @@server_uuid AS server_uuid, "
                                                  "DATABASE() AS schema_name, @@bind_address AS bind_address")
            env, password = fixture(connect, Path(scratch))
            verify(connect, env, password, directory, evidence)
    except Exception as error:
        evidence["failure"] = _safe_error(error)
        raise
    finally:
        after = {key: source_digest(path) for key, path in files.items()}
        evidence["file_sha256"] = {key: {"before": before[key], "after": after[key]} for key in files}
        for name in CASES:
            evidence["scenarios"].setdefault(name, {"status": "not_run"})
        evidence["counts"] = {status: sum(item["status"] == status for item in evidence["scenarios"].values())
                              for status in ("passed", "failed", "not_run")}
        dump(directory / "report.json", evidence)
        assert before == after
    print(evidence["counts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
