"""Exercise manual checks through FastAPI with independent SQLite readback."""

import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
from threading import Barrier
from unittest.mock import patch

import httpx

from backend.agent.tests.test_home_sse_tracing import HomeSSEFixture
from backend.agent.tests.test_plan_metrics import synthetic_artifact
from backend.config.config import settings
from backend.database import alert_review, ctr_alerts
from scripts.verify_task39 import TARGET_SQL, WINDOW_CTR_SQL


CLOCK = datetime.fromisoformat("2026-11-12T10:30:00+08:00")
DAY_1, DAY_2 = "2026-10-01", "2026-10-02"


class CtrAlertCheckTests(HomeSSEFixture):
    def connect(self, *, read_only=False):
        if not read_only:
            return super().connect()
        conn = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.enterContext(patch.object(alert_review, "now", return_value=CLOCK))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app), base_url="http://test",
        ))
        with closing(self.connect()) as conn, conn:
            conn.executemany("INSERT INTO users (id,username,password) VALUES (?,?, 'unused')",
                             [(17, "owner"), (18, "other")])
            conn.executemany("INSERT INTO ad_plans(id,plan_name,user_id) VALUES (?,'fixture',?)",
                             [(101, 17), (102, 18), (103, 17)])
        self.assertEqual(self.rows(), [])

    def rows(self):
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM alerts ORDER BY id")]

    def bind(self, counts):
        _, artifact, manifest = synthetic_artifact(self.database.parent, counts=counts)
        self.enterContext(patch.object(settings, "PLAN_METRICS_DB_PATH", str(artifact)))
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT INTO plan_metric_bindings VALUES(101,?,?,'fixture-campaign')",
                         (manifest["source_id"], manifest["data_version"]))
        return artifact

    async def post(self, plan_id=101, *, token=True):
        if token is True:
            token = self.token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return await self.client.post(f"/api/plans/{plan_id}/ctr-alert-check", headers=headers)

    def record(self, responses, **evidence):
        if folder := os.environ.get("AD_TASK39_FIXED_OUTPUT"):
            dest = Path(folder)
            dest.mkdir(parents=True, exist_ok=True)
            (dest / f"{self._testMethodName}.json").write_text(json.dumps({
                "responses": [{"method": r.request.method, "path": r.request.url.path,
                               "status": r.status_code, "json": r.json()} for r in responses],
                "alert_rows_sql": "SELECT * FROM alerts ORDER BY id", "alert_rows": self.rows(), **evidence,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    async def check(self, counts, status, reason=None):
        artifact = self.bind(counts)
        params = [("fixture-campaign", day, day) for day in (DAY_1, DAY_2)]
        with closing(sqlite3.connect(artifact.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            baseline, current = [dict(conn.execute(WINDOW_CTR_SQL, args).fetchone()) for args in params]
            threshold_params = dict(bi=baseline["impressions"], bc=baseline["clicks"],
                                    ci=current["impressions"], cc=current["clicks"])
            decision = dict(conn.execute(TARGET_SQL, threshold_params).fetchone())
        response = await self.post()
        self.record([response], window_sql=WINDOW_CTR_SQL, params=params, baseline=baseline, current=current,
                    threshold_sql=TARGET_SQL, threshold_params=threshold_params, threshold=decision)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["status"], status)
        self.assertEqual(data.get("reason"), reason)
        self.assertEqual(data["detected_at"], "2026-11-12T02:30:00+00:00")
        for label, date, oracle in (("baseline", DAY_1, baseline), ("current", DAY_2, current)):
            if oracle["days"]:
                window = data[label]
                self.assertEqual((window["start_date"], window["end_date"]), (date, date))
                for key in ("clicks", "impressions"):
                    self.assertEqual(window[key], oracle[key])
                self.assertAlmostEqual(window["ctr_pct"], oracle["ctr_pct"])
        self.assertEqual(bool(decision["qualifies"]), status == "created")
        if status == "created":
            row, = self.rows()
            self.assertEqual((row["id"], row["user_id"], row["plan_id"], row["type"], row["level"]),
                             (data["alert_id"], 17, 101, "ctr_review", "critical"))
            self.assertEqual(json.loads(row["ctr_check_evidence"]),
                             {k: v for k, v in data.items() if k not in ("alert_id", "status")})
        else:
            self.assertIsNone(data["alert_id"])
            self.assertEqual(self.rows(), [])
        return data

    async def test_qualifying(self):
        await self.check(((1000, 100), (1500, 100)), "created")

    async def test_exactly_twenty_percent(self):
        data = await self.check(((1000, 100), (1250, 100)), "created")
        self.assertEqual(data["comparison"]["relative_decline_pct"], 20)

    async def test_below_threshold(self):
        await self.check(((1000, 100), (1000, 81)), "below_threshold", "below_threshold")

    async def test_insufficient_impressions(self):
        await self.check(((999, 100), (1000, 80)), "insufficient_evidence", "insufficient_impressions")

    async def test_missing_binding(self):
        response = await self.post(103)
        self.record([response])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["reason"], "plan_history_not_mapped")
        self.assertEqual(self.rows(), [])

    async def test_no_complete_adjacent_pair(self):
        self.bind(((1000, 100),))
        response = await self.post()
        self.record([response])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["reason"], "plan_history_insufficient_windows")
        self.assertEqual(self.rows(), [])

    async def test_no_session_or_invalid_session(self):
        with (
            patch.object(ctr_alerts, "common_metric_dates") as coverage,
            patch.object(ctr_alerts, "query_plan_metrics") as metrics,
            patch.object(ctr_alerts, "_save") as save,
        ):
            responses = [await self.post(token=token) for token in (
                None, "invalid", self.sign_token(user_id=17, jti="uncommitted"),
            )]
        self.record(responses, coverage_reads=coverage.call_count, metric_reads=metrics.call_count,
                    save_attempts=save.call_count)
        self.assertEqual([r.status_code for r in responses], [401, 401, 401])
        coverage.assert_not_called()
        metrics.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.rows(), [])

    async def test_foreign_and_unknown_are_indistinguishable(self):
        with (
            patch.object(ctr_alerts, "common_metric_dates") as coverage,
            patch.object(ctr_alerts, "query_plan_metrics") as metrics,
            patch.object(ctr_alerts, "_save") as save,
        ):
            responses = [await self.post(plan) for plan in (102, 900)]
        self.record(responses, coverage_reads=coverage.call_count, metric_reads=metrics.call_count,
                    save_attempts=save.call_count)
        self.assertEqual([(r.status_code, r.json()) for r in responses], [(404, {"detail": "计划不存在"})] * 2)
        coverage.assert_not_called()
        metrics.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.rows(), [])

    async def race(self, change_sql, expected, *, after_commit=None):
        self.bind(((1000, 100), (1000, 80)))
        measured, changed = Barrier(2, timeout=10), Barrier(2, timeout=10)
        measure = ctr_alerts._measure
        order = []

        def wait_after_read(*args):
            result = measure(*args)
            self.assertIsNone(result)
            order.append("qualifying_metrics_read")
            measured.wait()
            changed.wait()
            return result

        def change():
            measured.wait()
            if change_sql:
                with closing(self.connect()) as conn, conn:
                    self.assertEqual(conn.execute(change_sql).rowcount, 1)
            if after_commit:
                after_commit()
            order.append("independent_change_committed")
            changed.wait()

        with patch.object(ctr_alerts, "_measure", side_effect=wait_after_read), patch.object(ctr_alerts, "_save") as save:
            response, _ = await asyncio.gather(self.post(), asyncio.to_thread(change))
        self.record([response], change_sql=change_sql, order=order, save_attempts=save.call_count)
        self.assertEqual(response.status_code, expected, response.text)
        self.assertNotIn("data", response.json())
        save.assert_not_called()
        self.assertEqual(self.rows(), [])

    async def test_transfer_after_metrics(self):
        await self.race("UPDATE ad_plans SET user_id=18 WHERE id=101", 404)

    async def test_rebind_after_metrics(self):
        await self.race("UPDATE plan_metric_bindings SET source_plan_id='other-campaign' WHERE plan_id=101", 409)

    async def test_source_version_after_metrics(self):
        def replace_source():
            replacement = self.database.parent / "replacement"
            replacement.mkdir()
            _, artifact, _ = synthetic_artifact(replacement, counts=((1000, 100), (1000, 90)))
            settings.PLAN_METRICS_DB_PATH = str(artifact)
        await self.race(None, 409, after_commit=replace_source)

    async def test_concurrent_checks_and_replay(self):
        self.bind(((1000, 100), (1000, 80)))
        count = 6
        barrier = Barrier(count, timeout=10)
        measure = ctr_alerts._measure

        def simultaneous(*args):
            result = measure(*args)
            barrier.wait()
            return result

        with patch.object(ctr_alerts, "_measure", side_effect=simultaneous):
            responses = await asyncio.gather(*(self.post() for _ in range(count)))
        replay = await self.post()
        self.record([*responses, replay], concurrent_requests=count)
        self.assertTrue(all(response.status_code == 200 for response in [*responses, replay]))
        results = [response.json()["data"] for response in responses]
        self.assertCountEqual([data["status"] for data in results], ["created", *(["existing"] * (count - 1))])
        row, = self.rows()
        self.assertEqual({result["alert_id"] for result in results}, {row["id"]})
        self.assertEqual(replay.json()["data"], {**results[0], "status": "existing"})
