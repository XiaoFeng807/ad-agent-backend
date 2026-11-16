"""Synthetic boundary fixtures only; public-file evidence uses verify_criteo_metrics."""

from contextlib import closing
from copy import deepcopy
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.agent.diagnosis import DiagnosisExecution, DiagnosisSnapshot
from backend.agent.tests.test_home_sse_tracing import tool_call
from backend.config.config import settings
from backend.database import database
from backend.rag.review_rule_demo import DEMO_RULES
from backend.tools import tools
from scripts.import_criteo_metrics import import_metrics, sha256_file
from scripts.verify_criteo_metrics import verify


def synthetic_artifact(directory, counts=((1000, 100), (1000, 80))):
    raw, artifact = directory / "synthetic.tsv.gz", directory / "synthetic.sqlite3"
    with gzip.open(raw, "wt", encoding="utf-8") as stream:
        stream.write("timestamp\tcampaign\tclick\n")
        for day, (impressions, clicks) in enumerate(counts):
            for i in range(impressions):
                stream.write(f"{day*86400}\tfixture-campaign\t{int(i < clicks)}\n")
        stream.write(f"{len(counts)*86400}\tfixture-campaign\t0\n")
    manifest = import_metrics(raw, artifact, expected_sha256=None, source_id="synthetic-boundary", synthetic=True)
    return raw, artifact, manifest


class PlanMetricsTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.raw, self.artifact, self.manifest = synthetic_artifact(self.directory)
        self.enterContext(patch.object(database, "DB_PATH", self.directory / "business.sqlite3"))
        self.enterContext(patch.object(settings, "PLAN_METRICS_DB_PATH", str(self.artifact)))
        database.init_db()
        with closing(database.get_db()) as conn, conn:
            conn.executemany("INSERT INTO ad_plans(id,plan_name,user_id) VALUES (?, 'fixture', ?)", [(101,17),(102,18),(103,17)])
            conn.execute("INSERT INTO plan_metric_bindings VALUES (101, ?, ?, 'fixture-campaign')",
                         (self.manifest["source_id"],self.manifest["data_version"]))
        self.snapshot = DiagnosisSnapshot(task_id="fixture-task",plan_id=101,rule_id="ctr-review-v1#low-ctr",
            baseline={"start_date":"2026-10-01","end_date":"2026-10-01"},
            current={"start_date":"2026-10-02","end_date":"2026-10-02"})

    def query(self, **changes):
        return tools.get_plan_metrics_by_date(**({"plan_id":"101","start_date":"2026-10-01","end_date":"2026-10-01","user_id":17}|changes))

    def event(self, *, baseline=None, current=None, rule=0, missing_rule=False, mutate_rule=False, rule_id=None):
        snapshot = self.snapshot if rule_id is None else self.snapshot.model_copy(update={"rule_id":rule_id})
        execution = DiagnosisExecution(snapshot, None)
        receipts = [("get_plan_detail",{"plan_id":101},{"id":101})]
        for window, changed in (("baseline",baseline),("current",current)):
            args = {"plan_id":101, **getattr(snapshot,window).model_dump()}
            result = tools.get_plan_metrics_by_date(**args,user_id=17)
            if changed:
                for key,value in changed.items():
                    result[key] = value
                if "impressions" in changed or "clicks" in changed:
                    for key in ("impressions","clicks"):
                        result["daily"][0][key]=result[key]
            receipts.append(("get_plan_metrics_by_date",args,result))
        chunk, identity, version, title, content = DEMO_RULES[rule]
        evidence = [] if missing_rule else [{"chunk_id":chunk,"rule_id":identity,"version":version,"title":title,
                                            "content":content+"changed" if mutate_rule else content}]
        receipts.append(("search_knowledge",{"query":snapshot.rule_id},{"evidence":evidence}))
        for index,(name,args,result) in enumerate(receipts):
            execution.record_receipt(name,args,result,runtime_call_id=f"call-{index}",tool_call_id=f"leaf-{index}",
                parent_call_id=None,dispatch_call_id=None)
        execution.receive()
        return execution.reply_event()

    def test_owned_mapping_and_read_only_artifact(self):
        before = sha256_file(self.artifact)
        result = self.query()
        self.assertEqual((result["impressions"], result["clicks"]),(1000,100))
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["source"]["kind"],"synthetic")
        self.assertEqual(sha256_file(self.artifact),before)
        self.assertNotIn(str(self.artifact),json.dumps(result))

    def test_ownership_checked_before_artifact_access(self):
        settings.PLAN_METRICS_DB_PATH="missing/private/path"
        self.assertEqual(self.query(user_id=18)["error_code"],"plan_not_found")
        self.assertEqual(self.query(plan_id=102)["error_code"],"plan_not_found")

    def test_missing_identity_and_unmapped_plan(self):
        self.assertEqual(self.query(user_id=None)["error_code"],"authentication_required")
        self.assertEqual(self.query(plan_id=103)["error_code"],"plan_history_not_mapped")

    def test_invalid_dates_and_source_version(self):
        for changes in ({"start_date":"2026-02-30"},{"end_date":"2026-09-30"},{"end_date":"2027-10-01"}):
            self.assertEqual(self.query(**changes)["error_code"],"invalid_date_window")
        with closing(database.get_db()) as conn,conn:
            conn.execute("UPDATE plan_metric_bindings SET data_version='wrong'")
        self.assertEqual(self.query()["error_code"],"source_version_mismatch")

    def test_missing_window_and_final_partial_day(self):
        for day in ("2026-10-03","2026-11-01"):
            r=self.query(start_date=day,end_date=day)
            self.assertFalse(r["coverage"]["complete"])
            self.assertEqual(r["coverage"]["missing_dates"],[day])

    def test_reimport_is_replacement_and_hash_is_verified(self):
        before=sha256_file(self.artifact)
        import_metrics(self.raw,self.artifact,expected_sha256=None,source_id="synthetic-boundary",synthetic=True)
        self.assertEqual(sha256_file(self.artifact),before)
        self.assertEqual(self.query()["impressions"],1000)
        with self.assertRaises(ValueError):
            import_metrics(self.raw,self.artifact,expected_sha256="invalid")
        self.assertEqual(sha256_file(self.artifact),before)

    def test_verifier_rejects_missing_extra_and_modified_days(self):
        raw, artifact = self.directory / "verifier.tsv.gz", self.directory / "verifier.sqlite3"
        with gzip.open(raw, "wt", encoding="utf-8") as stream:
            stream.write("timestamp\tcampaign\tclick\n")
            for day in range(31):
                # Another campaign keeps the global day present when the selected
                # campaign has no impressions, exercising an expected zero bucket.
                campaign = "other-campaign" if day == 3 else "verifier-campaign"
                stream.write(f"{day*86400}\t{campaign}\t1\n{day*86400+1}\t{campaign}\t0\n")
        import_metrics(raw, artifact, expected_sha256=None, source_id="synthetic-verifier", synthetic=True)
        before = artifact.read_bytes()
        result = verify(raw, artifact, campaign="verifier-campaign")
        self.assertTrue(result["passed"])
        self.assertEqual(result["daily_rows_checked"], 31)
        with closing(sqlite3.connect(artifact)) as conn:
            self.assertEqual(conn.execute("SELECT impressions, clicks FROM plan_daily_metrics "
                "WHERE source_plan_id='verifier-campaign' AND metric_date='2026-10-04'").fetchone(), (0, 0))
        mutations = (
            ("missing", "DELETE FROM plan_daily_metrics WHERE source_plan_id='verifier-campaign' "
             "AND metric_date='2026-10-21'", "campaign day coverage mismatch"),
            ("extra", "INSERT INTO plan_daily_metrics VALUES ('verifier-campaign', '2026-11-01', 0, 0)",
             "campaign day coverage mismatch"),
            ("modified", "UPDATE plan_daily_metrics SET impressions=impressions+1 "
             "WHERE source_plan_id='verifier-campaign' AND metric_date='2026-10-21'", "daily counts mismatch"),
        )
        for name, sql, error in mutations:
            with self.subTest(mutation=name):
                artifact.write_bytes(before)
                with closing(sqlite3.connect(artifact)) as conn, conn:
                    conn.execute(sql)
                with self.assertRaisesRegex(AssertionError, error):
                    verify(raw, artifact, campaign="verifier-campaign")

    def test_exact_sample_and_twenty_percent_boundaries(self):
        event=self.event()
        self.assertEqual(event["evidence_gaps"],[])
        self.assertEqual(event["calculation"]["decline_pp"],2)
        self.assertEqual(event["calculation"]["relative_decline_pct"],20)
        self.assertTrue(event["rule_evaluation"]["matched"])

    def test_below_relative_threshold_and_rising_ctr(self):
        for clicks in (81,110):
            event=self.event(current={"clicks":clicks})
            self.assertFalse(event["rule_evaluation"]["matched"])

    def test_sample_999_does_not_get_rule_judgment(self):
        event=self.event(current={"impressions":999})
        self.assertIsNone(event["rule_evaluation"])
        self.assertTrue(any("样本不足" in gap for gap in event["evidence_gaps"]))

    def test_zero_exposures_and_zero_baseline_ctr(self):
        event=self.event(current={"impressions":0,"clicks":0})
        self.assertIsNone(event["calculation"])
        self.assertIn("零曝光",event["content"])
        event=self.event(baseline={"clicks":0})
        self.assertIsNone(event["calculation"]["relative_decline_pct"])
        self.assertIsNone(event["rule_evaluation"])

    def test_incomplete_coverage_is_explicit(self):
        event=self.event(current={"coverage":{"complete":False,"expected_days":1,"observed_days":1,"missing_dates":["2026-10-02"]}})
        self.assertIn("缺窗/覆盖不全",event["content"])
        self.assertIsNone(event["rule_evaluation"])

    def test_source_version_or_campaign_mismatch(self):
        for key in ("source_id","data_version","source_plan_id"):
            event=self.event(current={key:"other"})
            self.assertIn("来源、版本或来源计划不一致",event["content"])
            self.assertIsNone(event["rule_evaluation"])

    def test_missing_adjacent_unknown_and_modified_rules(self):
        for changes in ({"missing_rule":True},{"rule":1},{"rule_id":"unknown-v1#ctr"},{"mutate_rule":True}):
            event=self.event(**changes)
            self.assertIsNone(event["rule_evaluation"])
            self.assertTrue(event["evidence_gaps"])

    def test_v2_uses_its_own_exact_sample_and_threshold(self):
        event=self.event(baseline={"impressions":2000,"clicks":200},current={"impressions":2000,"clicks":170},
                         rule=1,rule_id="ctr-review-v2#low-ctr")
        self.assertEqual(event["rule_evaluation"]["min_impressions"],2000)
        self.assertEqual(event["calculation"]["relative_decline_pct"],15)
        self.assertTrue(event["rule_evaluation"]["matched"])

    def test_totals_instead_of_daily_ctr_average(self):
        raw,artifact,manifest=synthetic_artifact(self.directory,((100,50),(900,0),(1000,100)))
        settings.PLAN_METRICS_DB_PATH=str(artifact)
        with closing(database.get_db()) as conn,conn:
            conn.execute("UPDATE plan_metric_bindings SET data_version=?",(manifest["data_version"],))
        r=self.query(end_date="2026-10-02")
        self.assertEqual(r["clicks"]/r["impressions"],.05)
        self.assertNotEqual(sum(d["clicks"]/d["impressions"] for d in r["daily"])/2,.05)

    def test_metrics_reject_identity_path_external_plan_and_window(self):
        e=DiagnosisExecution(self.snapshot,None)
        args={"plan_id":101,**self.snapshot.baseline.model_dump()}
        for change in ({"user_id":18},{"database_path":"arbitrary"},{"plan_id":"external"},{"start_date":"2026-01-01"}):
            call=tool_call("attempt",name="get_plan_metrics_by_date",**(args|change))
            admitted,error=e.admit(call,"数据助手",{"get_plan_metrics_by_date"})
            self.assertIsNone(admitted)
            self.assertFalse(error["success"])
        definition=next(t for t in tools.tools_definition if t["function"]["name"]=="get_plan_metrics_by_date")
        self.assertEqual(set(definition["function"]["parameters"]["properties"]),{"plan_id","start_date","end_date"})

    def test_malformed_receipt_cannot_produce_numbers(self):
        for changed in ({"scope":"user"}, {"source":{"kind":"public"}},
                        {"coverage":{"complete":True,"expected_days":1,"observed_days":1,"missing_dates":["2026-10-02"]}}):
            event=self.event(current=changed)
            self.assertIsNone(event["calculation"])
            self.assertIn("invalid_metrics_receipt",event["content"])


if __name__ == "__main__":
    unittest.main()
