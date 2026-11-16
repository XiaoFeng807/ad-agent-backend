"""Run MYSQL-T0 against a fresh local MySQL 8.4 data directory."""

import argparse
import io
import json
import re
import socket
import sqlite3
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from functools import partial
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from time import monotonic, sleep
from traceback import extract_tb
from unittest.mock import patch

import pymysql

from backend.agent.tests.test_diagnosis_reviews import DiagnosisReviewTests
from backend.agent.tests.test_natural_diagnosis import NaturalDiagnosisFixture
from backend.config.config import settings
from backend.database import database
from backend.database.diagnosis_tasks import CandidateRevisionChanged
from scripts.mysql_t0_review import confirm_review, read_review


SCHEMA = Path(__file__).with_name("mysql_t0_schema.sql")
TABLE_COLUMNS = {
    "ad_plans": ("id", "user_id", "status"),
    "plan_metric_bindings": ("plan_id", "source_id", "data_version", "source_plan_id"),
    "diagnosis_tasks": ("user_id", "task_id", "plan_id", "rule_id", "mode"),
    "diagnosis_turns": ("id", "user_id", "task_id", "reply", "snapshot"),
    "diagnosis_review_proposals": ("action_id", "turn_id", "snapshot"),
    "diagnosis_reviews": ("review_id", "action_id", "user_id", "turn_id", "snapshot", "created_at"),
}
OWNER, OTHER = 17, 18


def _startup_error(log_file, phase):
    log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
    codes = re.findall(r"\[ERROR\] \[(MY-\d+)\]", log)
    return RuntimeError(phase, codes)


@contextmanager
def isolated_mysql(mysqld, *, schema=SCHEMA, schema_name="mysql_t0"):
    with TemporaryDirectory(prefix="mysql-t0-") as directory:
        root = Path(directory)
        datadir = root / "data"
        log = root / "mysqld.log"
        init_file = root / "init.sql"
        password = token_hex(32)
        base_args = [
            str(mysqld), "--no-defaults", f"--basedir={mysqld.parent.parent}",
            f"--datadir={datadir}", f"--log-error={log}", "--innodb-buffer-pool-size=32M",
            "--innodb-redo-log-capacity=32M",
        ]
        process_options = {
            "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
        init = subprocess.run([*base_args, "--initialize-insecure"], timeout=90, **process_options)
        if init.returncode:
            raise _startup_error(log, "initialize")

        with socket.socket() as reserved_port:
            reserved_port.bind(("127.0.0.1", 0))
            port = reserved_port.getsockname()[1]
        init_file.write_text(f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{password}';\n", encoding="ascii")
        admin = partial(
            pymysql.connect, host="127.0.0.1", port=port, user="root", password=password,
            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=False,
            connect_timeout=2, read_timeout=25, write_timeout=25,
            init_command="SET time_zone = '+00:00', transaction_isolation = 'REPEATABLE-READ', "
                         "innodb_lock_wait_timeout = 15",
        )
        server = subprocess.Popen([
            *base_args, "--bind-address=127.0.0.1", f"--port={port}", "--mysqlx=OFF",
            "--skip-log-bin", "--secure-file-priv=NULL", "--performance-schema=ON", f"--init-file={init_file}",
        ], **process_options)
        ready = False
        try:
            deadline = monotonic() + 45
            while monotonic() < deadline and server.poll() is None:
                try:
                    with closing(admin()) as conn, conn.cursor() as cursor:
                        cursor.execute("SELECT @@version AS version, @@version_comment AS edition")
                        version = cursor.fetchone()
                        assert re.fullmatch(r"8\.4\.\d+", version["version"]) and "MySQL" in version["edition"]
                    ready = True
                    break
                except pymysql.OperationalError:
                    sleep(0.2)
            if not ready:
                raise _startup_error(log, "startup")
            init_file.unlink()
            with closing(admin()) as conn, conn.cursor() as cursor:
                for statement in schema.read_text(encoding="utf-8").split(";"):
                    if statement.strip():
                        cursor.execute(statement)
            yield partial(admin, database=schema_name)
        finally:
            if server.poll() is None:
                try:
                    if ready:
                        with closing(admin()) as conn:
                            conn.query("SHUTDOWN")
                    else:
                        server.terminate()
                    server.wait(timeout=20)
                except (pymysql.MySQLError, subprocess.TimeoutExpired):
                    server.kill()
                    server.wait(timeout=10)


def _query(connect, sql, args=None):
    with closing(connect()) as conn, conn.cursor() as cursor:
        cursor.execute(sql, args)
        return list(cursor.fetchall())


def _change(connect, sql, args=None):
    with closing(connect()) as conn, conn.cursor() as cursor:
        cursor.execute(sql, args)
        conn.commit()
        return cursor.rowcount


def _engine_facts(connect):
    facts, = _query(connect, """SELECT @@version AS version, @@version_comment AS edition,
        @@session.transaction_isolation AS session_isolation, @@global.transaction_isolation AS global_isolation,
        @@session.time_zone AS session_time_zone, @@foreign_key_checks AS foreign_key_checks""")
    tables = _query(connect, """SELECT TABLE_NAME AS name, ENGINE AS engine FROM information_schema.TABLES
        WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME""")
    facts["table_engines"] = {row["name"]: row["engine"] for row in tables}
    assert facts["table_engines"] == dict.fromkeys(TABLE_COLUMNS, "InnoDB")
    assert facts["session_isolation"] == "REPEATABLE-READ" and facts["foreign_key_checks"] == 1
    return facts


def _fixture_rows(sqlite):
    return {
        table: [tuple(row) for row in sqlite.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY 1, 2")]
        for table, columns in TABLE_COLUMNS.items() if table != "diagnosis_reviews"
    }


def _capture_fixtures(directory):
    fixtures = {}

    def capture(name, proposal):
        with closing(database.get_db()) as conn:
            fixtures[name] = {"rows": _fixture_rows(conn), "proposal": proposal}

    class StructuredCapture(DiagnosisReviewTests):
        async def capture_proposal(self):
            proposal = (await self.diagnose())[-1]["review_proposal"]
            capture("structured", proposal)
            response = await self.confirm(proposal["action_id"])
            self.assertEqual(response.status_code, 200)
            receipt = response.json()["data"]
            self.assertEqual((await self.read(receipt["review_id"])).json()["data"], receipt)
            fixtures["structured"]["sqlite_receipt"] = receipt

    class NaturalCapture(NaturalDiagnosisFixture):
        async def capture_proposal(self):
            done = (await self.diagnose())[-1]
            capture("natural", done["review_proposal"])
            fixtures["natural"]["sqlite_receipt"] = await self.confirm_and_read(done)
            (directory / "metrics.sqlite3").write_bytes(self.artifact.read_bytes())

    for case in (StructuredCapture("capture_proposal"), NaturalCapture("capture_proposal")):
        result = unittest.TestSuite([case]).run(unittest.TestResult())
        if not result.wasSuccessful():
            raise RuntimeError("fixture_capture", type(case).__name__)
    return fixtures


def _seed(connect, fixture):
    with closing(connect()) as conn, conn.cursor() as cursor:
        for table in reversed(TABLE_COLUMNS):
            cursor.execute(f"DELETE FROM {table}")
        for table, rows in fixture["rows"].items():
            columns = TABLE_COLUMNS[table]
            if table == "diagnosis_tasks":
                # SQLite preserves the original string/integer distinction in this untyped column.
                rows = [(*row[:2], json.dumps(row[2]), *row[3:]) for row in rows]
            cursor.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                               f"({', '.join(['%s'] * len(columns))})", rows)
        conn.commit()


def _snapshot(connect):
    with closing(connect()) as conn, conn.cursor() as cursor:
        tables = {}
        for table in TABLE_COLUMNS:
            cursor.execute(f"SELECT * FROM {table} ORDER BY 1, 2")
            tables[table] = list(cursor.fetchall())
    return {
        "counts": {table: len(rows) for table, rows in tables.items()},
        "table_sha256": {
            table: sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
            for table, rows in tables.items()
        },
        "review_rows": tables["diagnosis_reviews"],
    }


def _verify_insert(before, after, proposal, receipt):
    assert len(before["review_rows"]) == 0 and len(after["review_rows"]) == 1
    for table in TABLE_COLUMNS:
        if table != "diagnosis_reviews":
            assert before["table_sha256"][table] == after["table_sha256"][table], table
    row, = after["review_rows"]
    assert {key: receipt[key] for key in proposal} == proposal
    assert row["review_id"] == receipt["review_id"] and row["user_id"] == OWNER
    assert row["action_id"] == proposal["action_id"] and row["turn_id"] == proposal["turn_id"]


def _tracked(connect, connection_ids):
    conn = connect()
    connection_ids.append(conn.thread_id())
    return conn


def _normal(connect, fixture, details):
    proposal = fixture["proposal"]
    connection_ids = []
    tracked = partial(_tracked, connect, connection_ids)
    receipt = confirm_review(tracked, OWNER, proposal["action_id"])
    readback = read_review(tracked, OWNER, receipt["review_id"])
    stored = _query(connect, """SELECT BINARY r.snapshot = BINARY p.snapshot AS same_bytes
        FROM diagnosis_reviews r JOIN diagnosis_review_proposals p ON p.action_id=r.action_id""")
    details.update(receipt=receipt, independent_read=readback, connection_ids=connection_ids,
                   snapshot_bytes_match=stored, sqlite_receipt=fixture["sqlite_receipt"], after=_snapshot(connect))
    assert len(set(connection_ids)) == 2 and receipt == readback
    assert stored == [{"same_bytes": 1}]
    assert {key: receipt[key] for key in proposal} == {key: fixture["sqlite_receipt"][key] for key in proposal}
    _verify_insert(details["before"], details["after"], proposal, receipt)


def _concurrent(connect, fixture, details):
    proposal = fixture["proposal"]
    barrier = Barrier(6)
    inserted = Event()
    release = Event()

    def hold_insert(_cursor, _review_id):
        inserted.set()
        assert release.wait(12), "writer_release_timeout"

    def worker():
        ids = []
        conn = _tracked(connect, ids)
        try:
            barrier.wait(timeout=8)
        except Exception:
            conn.close()
            raise
        receipt = confirm_review(lambda: conn, OWNER, proposal["action_id"], after_insert=hold_insert)
        readback = read_review(partial(_tracked, connect, ids), OWNER, receipt["review_id"])
        assert receipt == readback
        return {"confirm_connection_id": ids[0], "read_connection_id": ids[1], "receipt": readback}

    with ThreadPoolExecutor(max_workers=6) as pool:
        pending = [pool.submit(worker) for _ in range(6)]
        try:
            assert inserted.wait(10), "insert_timeout"
            deadline = monotonic() + 6
            while True:
                waits = _query(connect, """SELECT DISTINCT thread.PROCESSLIST_ID AS connection_id,
                    locks.INDEX_NAME AS index_name, locks.LOCK_MODE AS lock_mode
                    FROM performance_schema.data_lock_waits waits
                    JOIN performance_schema.data_locks locks
                      ON locks.ENGINE_LOCK_ID=waits.REQUESTING_ENGINE_LOCK_ID
                    JOIN performance_schema.threads thread ON thread.THREAD_ID=locks.THREAD_ID
                    WHERE locks.OBJECT_SCHEMA=DATABASE()
                      AND locks.OBJECT_NAME='diagnosis_review_proposals' AND locks.INDEX_NAME='PRIMARY'
                    ORDER BY connection_id""")
                if len(waits) == 5 or monotonic() >= deadline:
                    break
                sleep(0.05)
            details.update(lock_waits=waits, during_insert=_snapshot(connect))
        finally:
            release.set()
        results = [future.result(timeout=20) for future in pending]
    details.update(workers=results, after=_snapshot(connect))
    confirm_ids = {item["confirm_connection_id"] for item in results}
    read_ids = {item["read_connection_id"] for item in results}
    receipt = results[0]["receipt"]
    assert len(confirm_ids) == len(read_ids) == 6 and confirm_ids.isdisjoint(read_ids)
    assert len(waits) == 5 and {row["connection_id"] for row in waits} < confirm_ids
    assert details["during_insert"] == details["before"]
    assert all(item["receipt"] == receipt for item in results)
    _verify_insert(details["before"], details["after"], proposal, receipt)
    try:
        _change(connect, """INSERT INTO diagnosis_reviews (review_id, action_id, user_id, turn_id, snapshot)
            SELECT %s, action_id, user_id, turn_id, snapshot FROM diagnosis_reviews WHERE action_id=%s""",
                (token_hex(16), proposal["action_id"]))
    except pymysql.IntegrityError as error:
        details["duplicate_action_errno"] = error.args[0]
        assert error.args[0] == 1062
    else:
        raise AssertionError("action_unique_constraint")
    assert _snapshot(connect) == details["after"]


class InjectedFailure(Exception):
    pass


def _rollback(connect, fixture, details):
    proposal = fixture["proposal"]
    action_id = proposal["action_id"]

    def fail_after_insert(cursor, review_id):
        cursor.execute("SELECT COUNT(*) AS row_count FROM diagnosis_reviews WHERE action_id=%s", (action_id,))
        details.update(own_uncommitted_count=cursor.fetchone()["row_count"], aborted_review_id=review_id,
                       independent_before_rollback=_snapshot(connect))
        raise InjectedFailure()

    try:
        confirm_review(connect, OWNER, action_id, after_insert=fail_after_insert)
    except InjectedFailure:
        details["fault"] = "InjectedFailure after INSERT before COMMIT"
    else:
        raise AssertionError("injection_did_not_run")
    details["after_rollback"] = _snapshot(connect)
    assert details["own_uncommitted_count"] == 1
    assert details["independent_before_rollback"] == details["after_rollback"] == details["before"]
    assert read_review(connect, OWNER, details["aborted_review_id"]) is None
    receipt = confirm_review(connect, OWNER, action_id)
    details.update(retry_receipt=receipt, after=_snapshot(connect))
    assert read_review(connect, OWNER, receipt["review_id"]) == receipt
    _verify_insert(details["before"], details["after"], proposal, receipt)


def _ownership(connect, fixture, details):
    action_id = fixture["proposal"]["action_id"]
    details["before_receipt"] = {
        "foreign": confirm_review(connect, OTHER, action_id),
        "unknown": confirm_review(connect, OTHER, "unknown-action"),
    }
    details["after_pending_denials"] = _snapshot(connect)
    assert details["before_receipt"] == {"foreign": None, "unknown": None}
    assert details["after_pending_denials"] == details["before"]
    receipt = confirm_review(connect, OWNER, action_id)
    review_id = receipt["review_id"]
    details["before_existing_denials"] = _snapshot(connect)
    details["after_receipt"] = {
        "foreign_confirm": confirm_review(connect, OTHER, action_id),
        "unknown_confirm": confirm_review(connect, OTHER, "unknown-action"),
        "foreign_read": read_review(connect, OTHER, review_id),
        "unknown_read": read_review(connect, OTHER, "unknown-review"),
    }
    details["after"] = _snapshot(connect)
    assert details["after_receipt"] == dict.fromkeys(details["after_receipt"])
    assert details["after"] == details["before_existing_denials"]
    _verify_insert(details["before"], details["after"], fixture["proposal"], receipt)


def _reject_invalid_target(connect, fixture, details):
    proposal = fixture["proposal"]
    cases = (
        ("uncommitted_turn", "UPDATE diagnosis_turns SET reply=NULL WHERE id=%s", proposal["turn_id"]),
        ("plan_transferred", "UPDATE ad_plans SET user_id=18 WHERE id=%s", int(proposal["plan_id"])),
    )
    for label, statement, target in cases:
        _seed(connect, fixture)
        assert _change(connect, statement, (target,)) == 1
        before = _snapshot(connect)
        returned = confirm_review(connect, OWNER, proposal["action_id"])
        after = _snapshot(connect)
        details[label] = {"before": before, "returned": returned, "after": after}
        assert returned is None and before == after and not after["review_rows"]


def _reject_stale(connect, fixture, details, *, change):
    details["fixture_mutation"] = change()
    assert details["fixture_mutation"]["rows_changed"] == 1
    details["before_confirmation"] = _snapshot(connect)
    try:
        confirm_review(connect, OWNER, fixture["proposal"]["action_id"])
    except CandidateRevisionChanged as error:
        details["error_code"] = error.code
    else:
        raise AssertionError("stale_proposal_accepted")
    details["after"] = _snapshot(connect)
    assert details["before_confirmation"] == details["after"] and not details["after"]["review_rows"]


def _artifact_change(artifact):
    with closing(sqlite3.connect(artifact)) as conn, conn:
        manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
        updated = conn.execute("UPDATE metadata SET manifest=?", (json.dumps({**manifest, "data_version": "changed"}),))
        return {"rows_changed": updated.rowcount, "version_before": manifest["data_version"], "version_after": "changed"}


def _candidate_change(connect, statement, field, new_value):
    return {"rows_changed": _change(connect, statement, (new_value,)), "plan_id": 103, field: new_value}


def _safe_error(error):
    return {
        "type": type(error).__name__,
        "errno": error.args[0] if error.args and isinstance(error.args[0], int) else None,
        "startup_codes": error.args if error.args and error.args[0] in ("initialize", "startup") else None,
        "frames": [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in extract_tb(error.__traceback__)],
    }


def _run_checks(connect, directory, evidence):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        fixtures = _capture_fixtures(directory)
    evidence["proposals"] = {kind: fixture["proposal"] for kind, fixture in fixtures.items()}
    artifact = directory / "metrics.sqlite3"
    scenarios = (
        ("structured_readback", "structured", _normal),
        ("natural_readback", "natural", _normal),
        ("concurrent_six", "structured", _concurrent),
        ("rollback_and_retry", "structured", _rollback),
        ("owner_isolation", "structured", _ownership),
        ("uncommitted_and_transferred", "structured", _reject_invalid_target),
        ("candidate_status_changed", "natural", partial(_reject_stale, change=partial(
            _candidate_change, connect, "UPDATE ad_plans SET status=%s WHERE id=103", "status", "paused"))),
        ("metric_binding_changed", "natural", partial(_reject_stale, change=partial(
            _candidate_change, connect, "UPDATE plan_metric_bindings SET source_plan_id=%s WHERE plan_id=103",
            "source_plan_id", "foreign"))),
        ("artifact_version_changed", "natural", partial(_reject_stale, change=lambda: _artifact_change(artifact))),
    )
    with patch.object(settings, "PLAN_METRICS_DB_PATH", str(artifact)):
        for label, kind, check in scenarios:
            details = evidence["scenarios"][label] = {}
            try:
                _seed(connect, fixtures[kind])
                details["before"] = _snapshot(connect)
                check(connect, fixtures[kind], details)
                details["status"] = "passed"
            except Exception as error:
                details.update(status="failed", error=_safe_error(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mysqld", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New JSON evidence file")
    args = parser.parse_args()
    if not __debug__ or not args.mysqld.is_file() or args.output.exists():
        parser.error("Use assertions, an existing mysqld binary, and a new output path.")
    evidence = {"scenarios": {}}
    try:
        with isolated_mysql(args.mysqld.resolve()) as connect:
            evidence["server"] = _engine_facts(connect)
            with TemporaryDirectory(prefix="mysql-t0-fixtures-") as directory:
                _run_checks(connect, Path(directory), evidence)
    except Exception as error:
        evidence["blocked"] = _safe_error(error)
    passed = sum(item["status"] == "passed" for item in evidence["scenarios"].values())
    failed = len(evidence["scenarios"]) - passed
    evidence["totals"] = {"passed": passed, "failed": failed, "skipped": 9 - passed - failed}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, ensure_ascii=False, default=str, indent=2)
        stream.write("\n")
    print(json.dumps(evidence["totals"]))
    return 0 if passed == 9 and "blocked" not in evidence else 1


if __name__ == "__main__":
    raise SystemExit(main())
