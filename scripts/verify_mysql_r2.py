"""Exercise SQLite-to-MySQL migration and R1 HTTP routes on isolated copies."""

import argparse
from contextlib import closing, redirect_stderr, redirect_stdout
from functools import partial
from io import StringIO
import json
import os
from pathlib import Path
from secrets import token_hex, token_urlsafe
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pymysql

from backend.agent.tests.test_diagnosis_pauses import DiagnosisPauseTests
from backend.config.config import settings
from backend.database import database, mysql_diagnosis
from backend.database.mysql_r2_schema import R2_AUTO_IDS, R2_COLUMNS, R2_SCHEMA, R2_VERSION_TABLE
from backend.routes.auth_routes import _log_action_local
from backend.tools.tools import record_suggestion
from scripts.migrate_mysql_r2 import (
    BASE_TABLES, DDL, import_copy, source_digest, target_fingerprints, target_metadata,
)
from scripts.verify_mysql_r1 import (
    REVIEWS, TASKS, assert_committed, dump, fastapi_process, headers, login, proposal_url, read_probes,
    scenario, sse, turn_row,
)
from scripts.verify_mysql_t0 import _change, _query, _safe_error, isolated_mysql


CASES = (
    "sqlite_business_copy", "explicit_schema_version", "orphan_preflight", "invalid_value_preflight", "mid_write_failure_rollback",
    "recovery_import_all_tables", "idempotent_replay", "allocator_history", "original_http_readback",
    "cross_user_denial", "continuation_preserves_history", "reject_changed_target", "legacy_import", "legacy_diagnosis_columns",
)


def fixture(directory, scratch):
    evidence = {}
    source = directory / "sanitized.sqlite3"
    artifact = scratch / "synthetic.sqlite3"

    class Capture(DiagnosisPauseTests):
        async def capture(self):
            # Use the same SQLite routes that generate the history and local receipts.
            first = await self.diagnose(plan_id="101")
            pending = await self.diagnose(plan_id=102, days=(3, 4))
            review_url = proposal_url(first["review_proposal"]["action_id"])
            review = await self.client.post(review_url, headers=self.owner)
            self.assertEqual(review.status_code, 200)
            review = review.json()["data"]
            pause_id = first["pause_proposal"]["action_id"]
            self.assertEqual((await self.action(pause_id)).status_code, 200)
            pause = (await self.action(pause_id, read=True)).json()["data"]["receipt"]
            for label, done in (("confirmed_task", first), ("pending_task", pending)):
                response = await self.client.get(f"{TASKS}/{done['task_id']}", headers=self.owner)
                self.assertEqual(response.status_code, 200)
                evidence[label] = response.json()["data"]
            self.assertEqual(evidence["confirmed_task"]["plan_id"], "101")
            self.assertEqual(evidence["pending_task"]["plan_id"], 102)
            self.assertEqual(evidence["pending_task"]["turns"][0]["review"]["state"], "pending")
            evidence.update(review=review, pause=pause, accounts=["diagnosis-demo", "second-demo"])

            # The existing seed function addresses its sample accounts/reports by IDs 1–4.
            with closing(database.get_db()) as conn, conn:
                conn.executemany("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                                 [(uid, f"r2-synthetic-{uid}", database.hash_password(token_urlsafe(32)))
                                  for uid in range(1, 5)])
            database.seed_data()
            self.server.agent.save_conversation(17, "user", "合成历史问题", 2, chat_view_id="r2-fixture")
            self.server.agent.save_conversation(17, "assistant", "合成历史回复")
            record_suggestion("fixture", "只记录合成建议", 17)
            _log_action_local(17, "fixture", "合成审计记录")
            # Retired messages have no writer. Add a legacy row and boundary values.
            with closing(database.get_db()) as conn, conn:
                conn.execute("INSERT INTO messages (id, user_id, role, content) VALUES (37,17,'user','legacy 消息')")
                conn.execute("INSERT INTO messages (id, user_id, role, content, created_at) "
                             "VALUES (38,NULL,'assistant','','2026-10-01T08:00:00.123456+08:00')")
                conn.execute("INSERT INTO materials (name, user_id) VALUES ('合成素材',17)")
                conn.execute("INSERT INTO conversations (id,user_id,content) VALUES (900,17,'deleted synthetic row')")
                conn.execute("DELETE FROM conversations WHERE id=900")
                conn.execute("INSERT INTO daily_reports (id, user_id) VALUES (1200, 17)")
                conn.execute("DELETE FROM daily_reports WHERE id=1200")
            with closing(database.get_db(read_only=True)) as conn, closing(sqlite3.connect(source)) as backup:
                conn.backup(backup)
                # No usable JWT leaves the SQLite fixture. The placeholder tests SQL-side redaction.
                with backup:
                    backup.execute("UPDATE sessions SET token='sanitized-r2-placeholder'")
                self.assertEqual(backup.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
            shutil.copyfile(settings.PLAN_METRICS_DB_PATH, artifact)

    with patch.object(settings, "DIAGNOSIS_MYSQL_ENABLED", False), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        result = unittest.TestSuite([Capture("capture")]).run(unittest.TestResult())
    if not result.wasSuccessful():
        # Unittest's final line contains only the fixed assertion or exception text from this fixture.
        raise RuntimeError((result.errors or result.failures)[0][1].splitlines()[-1])
    return source, artifact, evidence


def legacy_copy(source, destination):
    shutil.copyfile(source, destination)
    with closing(sqlite3.connect(destination)) as conn, conn:
        conn.execute("DROP TRIGGER diagnosis_sandbox_target_deleted")
        conn.execute("DROP TRIGGER diagnosis_sandbox_target_transferred")
        for table in reversed(tuple(R2_COLUMNS)):
            if table not in BASE_TABLES:
                conn.execute(f"DROP TABLE {table}")
        for col in ("priority", "chat_view_id"):
            conn.execute(f"ALTER TABLE conversations DROP COLUMN {col}")
    return destination


def target_snapshot(connect):
    with closing(connect()) as conn, conn.cursor() as cursor:
        metadata, _ = target_metadata(cursor)
        return target_fingerprints(cursor, metadata)


def runtime_env(connect, artifact, scratch):
    password = token_hex(32)
    with closing(connect()) as conn, conn.cursor() as cursor:
        cursor.execute("CREATE USER 'mysql_r1_app'@'localhost' IDENTIFIED BY %s", (password,))
        cursor.execute("GRANT SELECT ON mysql_r2.* TO 'mysql_r1_app'@'localhost'")
        for table in ("sessions", "diagnosis_tasks", "diagnosis_turns", "diagnosis_review_proposals", "diagnosis_reviews"):
            cursor.execute(f"GRANT INSERT ON mysql_r2.{table} TO 'mysql_r1_app'@'localhost'")
        for table in ("sessions", "diagnosis_tasks", "diagnosis_turns", "diagnosis_review_proposals"):
            cursor.execute(f"GRANT UPDATE ON mysql_r2.{table} TO 'mysql_r1_app'@'localhost'")
    return dict(os.environ, AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1", ANONYMIZED_TELEMETRY="False",
                API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="", LLM_PROVIDER="mock",
                JWT_SECRET=token_urlsafe(48), DIAGNOSIS_SANDBOX_DB_PATH="", DIAGNOSIS_MYSQL_ENABLED="1",
                DIAGNOSIS_MYSQL_SCHEMA_VERSION="2", DIAGNOSIS_MYSQL_PORT=str(connect.keywords["port"]),
                DIAGNOSIS_MYSQL_PASSWORD=password, RAG_DB_PATH=str(scratch / "rag"),
                PLAN_METRICS_DB_PATH=str(artifact))


def schema_checks(connect):
    with closing(connect()) as db, db.cursor() as cursor:
        def validate(schema=R2_SCHEMA):
            mysql_diagnosis.validate_schema_connection(mysql_diagnosis.MySQLDiagnosisConnection(db, pymysql.MySQLError), schema)

        facts, = _query(connect, "SELECT VERSION() AS version, DATABASE() AS db, @@time_zone AS time_zone, "
                                 "@@foreign_key_checks AS foreign_keys, @@transaction_isolation AS isolation")
        validate()
        rejected = []
        for defect, sql, schema in (
            ("r1_selection", None, mysql_diagnosis.SCHEMA),
            ("missing_version_row", f"DELETE FROM {R2_VERSION_TABLE}", R2_SCHEMA),
        ):
            if sql:
                cursor.execute(sql)
            try:
                validate(schema)
            except mysql_diagnosis.MySQLStorageError:
                rejected.append(defect)
            finally:
                db.rollback()
        assert len(rejected) == 2
        cursor.execute("ALTER TABLE messages DROP COLUMN content")
        try:
            try:
                validate()
            except mysql_diagnosis.MySQLStorageError:
                rejected.append("missing_business_column")
        finally:
            cursor.execute("ALTER TABLE messages ADD COLUMN content LONGTEXT AFTER role")
        assert len(rejected) == 3
        validate()
        ddl = {table: _query(connect, f"SHOW CREATE TABLE {table}")[0]["Create Table"]
               for table in (*R2_COLUMNS, R2_VERSION_TABLE)}
        trigger, = _query(connect, "SHOW CREATE TRIGGER diagnosis_sandbox_target_transferred")
        return {"server": facts, "rejected": rejected, "target_ddl": ddl,
                "target_trigger": trigger["SQL Original Statement"]}


def allocator_probe(connect, watermarks):
    before = target_snapshot(connect)
    allocated = {}
    with closing(connect()) as conn, conn.cursor() as cursor:
        conn.begin()
        try:
            for table in R2_AUTO_IDS:
                if table == "diagnosis_turns":
                    continue
                cols = R2_COLUMNS[table][1:]
                selection = ", ".join(cols)
                if table == "users":
                    selection = "'r2-allocator-probe', password, role, created_at"
                elif table == "sessions":
                    selection = f"{allocated['users']}, NULL, created_at"
                cursor.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {selection} FROM {table} ORDER BY id LIMIT 1")
                allocated[table] = cursor.lastrowid
                assert allocated[table] >= watermarks[table]["next_id_min"]
        finally:
            conn.rollback()
    assert before == target_snapshot(connect)
    return allocated


def http_checks(connect, env, source_evidence, evidence, directory, watermarks):
    original = source_evidence["confirmed_task"]
    pending = source_evidence["pending_task"]
    review = source_evidence["review"]
    task_id, review_id, turn_id = original["task_id"], review["review_id"], review["turn_id"]
    task_path, review_path = f"{TASKS}/{task_id}", f"{REVIEWS}/{review_id}"
    original_sql = turn_row(connect, turn_id)
    independent_review, = _query(connect, "SELECT CONNECTION_ID() AS connection_id, review_id, user_id, "
                                "turn_id, action_id, snapshot, created_at FROM diagnosis_reviews WHERE review_id=%s", (review_id,))
    assert independent_review["user_id"] == 17
    assert independent_review["snapshot"] == original_sql["proposal_snapshot"]
    original_sessions = _query(connect, "SELECT id, user_id, token IS NULL AS is_redacted FROM sessions ORDER BY user_id")
    assert original_sessions and all(row["is_redacted"] for row in original_sessions)
    continuation = {"message": "沿用合成历史任务，比较另一组窗口", "diagnosis": {
        "task_id": task_id, "baseline": {"start_date": "2026-10-03", "end_date": "2026-10-03"},
        "current": {"start_date": "2026-10-04", "end_date": "2026-10-04"},
    }}

    def for_mysql(task):
        return {**task, "turns": [{**turn, "pause": None} for turn in task["turns"]]}

    with fastapi_process(env, directory) as (client, pid):
        owner = headers(login(client, connect, "diagnosis-demo", "demo-password"))
        other = headers(login(client, connect, "second-demo", "demo-password"))
        with scenario(evidence, "original_http_readback") as result:
            listing = client.get(TASKS, headers=owner)
            detail = client.get(task_path, headers=owner)
            waiting = client.get(f"{TASKS}/{pending['task_id']}", headers=owner)
            received = client.get(review_path, headers=owner)
            assert listing.status_code == detail.status_code == waiting.status_code == received.status_code == 200
            assert {row["task_id"] for row in listing.json()["data"]["tasks"]} == {task_id, pending["task_id"]}
            assert detail.json()["data"] == for_mysql(original)
            assert waiting.json()["data"] == for_mysql(pending)
            assert received.json()["data"] == review
            fresh_sessions = _query(connect, "SELECT id, user_id, token IS NOT NULL AS logged_in FROM sessions ORDER BY user_id")
            assert [(row["id"], row["user_id"]) for row in original_sessions] == [
                (row["id"], row["user_id"]) for row in fresh_sessions
            ]
            assert all(row["logged_in"] for row in fresh_sessions)
            result.update(pid=pid, statuses={"task_list": 200, "task": 200, "pending_task": 200, "review": 200},
                          tasks=[detail.json()["data"], waiting.json()["data"]], review=received.json()["data"],
                          original_sessions=original_sessions, fresh_sessions=fresh_sessions,
                          original_turn_sql=original_sql, independent_review=independent_review)
        with scenario(evidence, "cross_user_denial") as result:
            before = target_snapshot(connect)
            outcomes = {
                "task": client.get(task_path, headers=other).status_code,
                "review": client.get(review_path, headers=other).status_code,
                "proposal_confirm": client.post(proposal_url(review["action_id"]), headers=other).status_code,
                "continue": client.post("/api/chat/stream", json=continuation, headers=other).status_code,
            }
            own_list = client.get(TASKS, headers=other)
            assert own_list.status_code == 200 and own_list.json()["data"]["tasks"] == []
            assert set(outcomes.values()) == {404} and target_snapshot(connect) == before
            result.update(http=outcomes, other_task_list=own_list.json()["data"], table_hashes_unchanged=True)
        with scenario(evidence, "continuation_preserves_history") as result:
            events = sse(client, owner, continuation)
            proposal, new_row = assert_committed(connect, events)
            assert proposal["task_id"] == task_id and proposal["plan_id"] == "101"
            assert proposal["turn_id"] >= watermarks["diagnosis_turns"]["next_id_min"]
            assert proposal["source_trace_id"] != review["source_trace_id"]
            historical_sql = turn_row(connect, turn_id)
            assert {k: v for k, v in historical_sql.items() if k != "connection_id"} == {
                k: v for k, v in original_sql.items() if k != "connection_id"
            }
            detail = client.get(task_path, headers=owner)
            received = client.get(review_path, headers=owner)
            assert detail.status_code == received.status_code == 200
            assert detail.json()["data"]["turns"][0] == for_mysql(original)["turns"][0]
            assert detail.json()["data"]["turns"][-1]["turn_id"] == proposal["turn_id"]
            assert received.json()["data"] == review
            probes = read_probes(directory, pid)
            assert sum(p["probe"] == "business_sqlite_blocked" for p in probes) == 1  # Guard self-test.
            result.update(events=events, new_turn_sql=new_row, historical_sql=historical_sql,
                          old_turn_and_review_unchanged=True, business_sqlite_connections=0,
                          minimum_next_turn_id=watermarks["diagnosis_turns"]["next_id_min"])


def check(connect, directory, scratch, source, orphan, artifact, source_evidence, evidence):
    with scenario(evidence, "explicit_schema_version") as result:
        result.update(schema_checks(connect))
    with scenario(evidence, "orphan_preflight") as result:
        legacy_copy(source, orphan)
        with closing(sqlite3.connect(orphan)) as conn, conn:
            conn.execute("INSERT INTO messages (id, user_id, role) VALUES (1000, 99999, 'user')")
        failed = import_copy(orphan, connect)
        dump(directory / "legacy-orphan.json", failed)
        assert failed["status"] == "failed" and failed["phase"] == "source_preflight"
        assert failed["tables"]["messages"]["anomaly_rows"] == 1
        assert failed["tables"]["messages"]["anomalies"][0]["primary_key"] == (1000,)
        assert failed["source_file_sha256"]["before"] == failed["source_file_sha256"]["after"]
        assert all(item["row_count"] == 0 for item in target_snapshot(connect).values())
        result.update(file="legacy-orphan.json", affected_table="messages", affected_id=1000, target_rows=0)

    with scenario(evidence, "invalid_value_preflight") as result:
        malformed = scratch / "malformed.sqlite3"
        shutil.copyfile(source, malformed)
        with closing(sqlite3.connect(malformed)) as conn, conn:
            conn.execute("UPDATE diagnosis_turns SET snapshot='{malformed' WHERE id=1")
            conn.execute("UPDATE messages SET created_at='invalid timestamp' WHERE id=37")
        failed = import_copy(malformed, connect)
        dump(directory / "invalid-values.json", failed)
        assert failed["status"] == "failed" and failed["phase"] == "source_preflight"
        assert {table: row["anomaly_rows"] for table, row in failed["tables"].items() if row["anomaly_rows"]} == {
            "diagnosis_turns": 1, "messages": 1,
        }
        assert all(item["row_count"] == 0 for item in target_snapshot(connect).values())
        result.update(file="invalid-values.json", target_rows=0)

    with scenario(evidence, "mid_write_failure_rollback") as result:
        def inject(table, writer):
            if table != "ad_plans":
                return
            writer.execute("SELECT CONNECTION_ID() AS id, COUNT(*) AS row_count FROM ad_plans")
            visible = writer.fetchone()
            reader, = _query(connect, "SELECT CONNECTION_ID() AS id, COUNT(*) AS row_count FROM ad_plans")
            assert visible["row_count"] > 0 and reader["row_count"] == 0 and visible["id"] != reader["id"]
            result.update(writer=visible, reader=reader, injected_sql="duplicate existing users primary key")
            writer.execute("INSERT INTO users SELECT * FROM users ORDER BY id LIMIT 1")

        failed = import_copy(source, connect, after_table=inject)
        dump(directory / "mid-write-failure.json", failed)
        assert failed["status"] == "failed" and failed["phase"] == "insert" and failed["rolled_back"]
        assert failed["error"]["errno"] == 1062
        counts = {table: item["row_count"] for table, item in target_snapshot(connect).items()}
        assert not any(counts.values())
        result.update(file="mid-write-failure.json", mysql_errno=1062, independent_post_rollback_counts=counts)

    with scenario(evidence, "recovery_import_all_tables") as result:
        imported = import_copy(source, connect)
        dump(directory / "reconciliation.json", imported)
        assert imported["status"] == "imported", imported.get("error")
        assert all(table["matches"] and table["target"]["row_count"] > 0 and table["anomaly_rows"] == 0
                   for table in imported["tables"].values())
        assert imported["source_file_sha256"]["before"] == imported["source_file_sha256"]["after"]
        pause, = _query(connect, "SELECT CONNECTION_ID() AS connection_id, p.action_id, p.plan_id, "
                        "r.receipt_id, r.execution_trace_id, r.committed_status, v.turn_id, v.snapshot "
                        "FROM diagnosis_pause_receipts r JOIN diagnosis_pause_proposals p ON p.action_id=r.action_id "
                        "JOIN diagnosis_review_proposals v ON v.action_id=p.review_action_id")
        receipt = source_evidence["pause"]
        assert pause["plan_id"] == receipt["target"]["plan_id"]
        assert all(pause[key] == receipt[key] for key in ("action_id", "receipt_id", "execution_trace_id",
                                                         "committed_status", "turn_id"))
        assert json.loads(pause["snapshot"])["source_trace_id"] == receipt["source_trace_id"]
        result.update(file="reconciliation.json", matching_tables=len(imported["tables"]), pause_sql=pause)

    with scenario(evidence, "idempotent_replay") as result:
        replay = import_copy(source, connect)
        dump(directory / "replay.json", replay)
        assert replay["status"] == "already_matched"
        assert {table: item["target"] for table, item in replay["tables"].items()} == target_snapshot(connect)
        assert replay["tables"] == imported["tables"] and replay["watermarks"] == imported["watermarks"]
        result.update(file="replay.json", matching_tables=len(replay["tables"]), target_rows_rewritten=0)

    with scenario(evidence, "allocator_history") as result:
        watermarks = imported["watermarks"]
        assert watermarks["conversations"]["sqlite_sequence"] > watermarks["conversations"]["max_id"]
        assert watermarks["daily_reports"]["sqlite_sequence"] > watermarks["daily_reports"]["max_id"]
        result.update(actual_insert_ids=allocator_probe(connect, watermarks), committed_probe_rows=0)

    http_checks(connect, runtime_env(connect, artifact, scratch), source_evidence, evidence, directory, watermarks)

    with scenario(evidence, "reject_changed_target") as result:
        before = target_snapshot(connect)
        refused = import_copy(source, connect)
        dump(directory / "divergent-target.json", refused)
        assert refused["status"] == "failed" and refused["phase"] == "target_preflight"
        assert target_snapshot(connect) == before
        result.update(file="divergent-target.json", business_rows_unchanged=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mysqld", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    if not __debug__ or not args.mysqld.is_file() or directory.exists():
        parser.error("Use assertions, an existing mysqld binary and a new output directory")
    directory.mkdir(parents=True)
    evidence = {"scenarios": {}}
    business_before = source_digest(database.DEFAULT_DB_PATH) if database.DEFAULT_DB_PATH.is_file() else None
    source_before = None
    server = partial(isolated_mysql, args.mysqld.resolve(), schema=DDL, schema_name=R2_SCHEMA)
    try:
        with TemporaryDirectory(prefix="mysql-r2-fixture-") as temporary:
            scratch = Path(temporary)
            orphan = scratch / "legacy-orphan.sqlite3"
            with scenario(evidence, "sqlite_business_copy") as result:
                source, artifact, source_evidence = fixture(directory, scratch)
                source_before = source_digest(source)
                dump(directory / "sqlite-business-evidence.json", source_evidence)
                result.update(copy=source.name, evidence="sqlite-business-evidence.json", sha256=source_before)
            with server() as connect:
                check(connect, directory, scratch, source, orphan, artifact, source_evidence, evidence)
            with scenario(evidence, "legacy_import") as result, server() as connect:
                legacy = directory / "sanitized-legacy.sqlite3"
                shutil.copyfile(orphan, legacy)
                with closing(sqlite3.connect(legacy)) as conn, conn:
                    # Explicit fixture repair on a separate copy. Keep its recorded allocator history.
                    assert conn.execute("DELETE FROM messages WHERE id=1000 AND user_id=99999").rowcount == 1
                imported = import_copy(legacy, connect)
                dump(directory / "legacy-reconciliation.json", imported)
                assert imported["status"] == "imported", imported.get("error")
                assert all(item["matches"] and item["anomaly_rows"] == 0 for item in imported["tables"].values())
                assert imported["tables"]["conversations"]["legacy_defaults"] == {"priority": 1, "chat_view_id": None}
                assert imported["watermarks"]["messages"]["next_id_min"] == 1001
                assert imported["source_file_sha256"]["before"] == imported["source_file_sha256"]["after"]
                result.update(file="legacy-reconciliation.json", present=len(BASE_TABLES),
                              absent=len(R2_COLUMNS) - len(BASE_TABLES), repaired_fixture_row=1000,
                              messages_next_id=imported["watermarks"]["messages"]["mysql_next_id"])
            with scenario(evidence, "legacy_diagnosis_columns") as result, server() as connect:
                intermediate = scratch / "legacy-diagnosis.sqlite3"
                shutil.copyfile(source, intermediate)
                with closing(sqlite3.connect(intermediate)) as conn, conn:
                    conn.execute("ALTER TABLE diagnosis_tasks DROP COLUMN mode")
                    conn.execute("ALTER TABLE diagnosis_turns DROP COLUMN snapshot")
                imported = import_copy(intermediate, connect)
                dump(directory / "legacy-diagnosis-reconciliation.json", imported)
                assert imported["status"] == "imported", imported.get("error")
                assert all(item["matches"] and item["anomaly_rows"] == 0 for item in imported["tables"].values())
                assert imported["tables"]["diagnosis_tasks"]["legacy_defaults"] == {"mode": "structured"}
                assert imported["tables"]["diagnosis_turns"]["legacy_defaults"] == {"snapshot": None}
                result.update(file="legacy-diagnosis-reconciliation.json", original_review_snapshots_preserved=True)
    except Exception as error:
        evidence["interruption"] = _safe_error(error)
        if isinstance(error, RuntimeError) and len(error.args) == 1:
            evidence["interruption"]["reason"] = str(error)
    for name in CASES:
        evidence["scenarios"].setdefault(name, {"status": "skipped"})
    evidence["totals"] = {status: sum(case["status"] == status for case in evidence["scenarios"].values())
                          for status in ("passed", "failed", "skipped")}
    copy = directory / "sanitized.sqlite3"
    source_after = source_digest(copy) if copy.is_file() else None
    business_after = source_digest(database.DEFAULT_DB_PATH) if database.DEFAULT_DB_PATH.is_file() else None
    evidence["source_copy_sha256"] = {"before": source_before, "after": source_after}
    evidence["business_sqlite_sha256"] = {"before": business_before, "after": business_after}
    dump(directory / "report.json", evidence)
    print(json.dumps(evidence["totals"]))
    return int(evidence["totals"]["passed"] != len(CASES) or source_before != source_after
               or business_before != business_after or "interruption" in evidence)


if __name__ == "__main__":
    raise SystemExit(main())
