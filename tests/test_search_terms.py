import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook

from backend.agent import search_term_diagnosis as diagnosis
from backend.database import chat_groups, listing_operations, search_term_reports as reports
from scripts.accept_search_terms import FIXTURES, facts, freeze


class SearchTermTests(unittest.TestCase):
    def setUp(self):
        self.raw = (FIXTURES / "report.csv").read_bytes()
        self.report, self.rows = reports.parse_report(self.raw, "report.csv")
        self.report["version"] = 1
        self.contract = freeze()["contract"]
        self.product = {"title": self.contract["title"], "facts": facts(self.contract, 12),
                        "facts_version": 1, "title_version": 1, "capacity_l": 12,
                        "capacity_quote": self.contract["capacity_12"]}

    def test_bytes_hash_rows_and_import_idempotence(self):
        manifest = {key: value for key, value in self.report.items() if key != "version"}
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            for sql in reports.SCHEMA:
                conn.execute(sql)
            first = reports.save_import(conn, "one", manifest, self.rows, self.raw)
            self.assertEqual(reports.save_import(conn, "one", manifest, self.rows, self.raw), first)
            second = reports.save_import(conn, "two", manifest, self.rows, self.raw)
            self.assertNotEqual(second["import_id"], first["import_id"])
            with self.assertRaises(LookupError):
                reports.read_import(conn, "two", first["import_id"])
            stored, rows = reports.read_import(conn, "one", first["import_id"])
            self.assertEqual(stored["sha256"], hashlib.sha256(self.raw).hexdigest())
            self.assertEqual(rows, self.rows)

    def test_invalid_csv_lines_fail_without_partial_import(self):
        duplicate = self.raw + self.raw.splitlines(keepends=True)[-1]
        invalid_metrics = self.raw.replace(b",180,16,", b",2,16,")
        invalid_mapping = self.raw.replace(b"SP-LUNCH,SP-LUNCH", b"SP-LUNCH,CONTROL")
        multiline = self.raw.replace(b"12L lunch bag", b'"12L\nlunch bag"')
        for bad in (duplicate, invalid_metrics, invalid_mapping, multiline):
            with self.subTest(bad=bad), self.assertRaisesRegex(reports.ReportInvalid, "CSV 第 [234] 行"):
                reports.parse_report(bad, "bad.csv")

    def test_membership_is_checked_against_account_and_registered_group(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            for sql in (*listing_operations.SCHEMA, *reports.SCHEMA):
                conn.execute(sql)
            conn.execute("INSERT INTO listing_test_shops VALUES ('search-terms-test','US',17)")
            conn.execute("""INSERT INTO listing_test_products VALUES
                         ('search-terms-test','US','SP-LUNCH',1,'Trailbox Insulated Lunch Bag','[]',0,0)""")
            with self.assertRaises(reports.ReportInvalid):
                reports.verify_scope(conn, 17, self.report)
            conn.execute("INSERT INTO search_term_ad_groups VALUES ('search-terms-test','US','SP-101','AG-101','SP-LUNCH')")
            self.assertEqual([item["sku"] for item in reports.verify_scope(conn, 17, self.report)], ["SP-LUNCH"])
            with self.assertRaises(reports.ReportInvalid):
                reports.verify_scope(conn, 18, self.report)

    def test_partial_mixed_mapping_preserves_group_metrics(self):
        raw = (FIXTURES / "mixed.csv").read_bytes().replace(
            b"SP-LUNCH|SP-CONTROL,,12L", b"SP-LUNCH|SP-CONTROL,SP-LUNCH,12L", 1,
        )
        report, rows = reports.parse_report(raw, "mixed.csv")
        for sku in ("SP-LUNCH", "SP-CONTROL"):
            [item] = diagnosis.aggregate(report, rows, sku)
            self.assertEqual(item["row_lines"], [2, 3])
            self.assertEqual(item["metrics"]["clicks"], 30)
            self.assertEqual(diagnosis.score(item, self.product)[:2], ("observe", "unattributed"))

    def test_group_evidence_normalizes_legacy_metric_scope_without_rewriting_history(self):
        prior_row = {"sheet": None, "line": 8}
        other_row = {"sheet": None, "line": 26}
        legacy = {"id": 1, "action": "incomplete", "incomplete_cause": "ownership_changed",
                  "attribution": "ad_group", "metrics": {"clicks": 10}, "row_locations": [prior_row],
                  "bucket_locations": [prior_row, other_row]}
        verified = {"id": 2, "incomplete_cause": "ownership_changed", "attribution": "ad_group",
                    "metrics": {"clicks": 21}, "row_locations": [prior_row, other_row], "metric_recheck": "verified"}
        version_only = {"id": 3, "incomplete_cause": "product_version_changed", "attribution": "product",
                        "metrics": {"clicks": 10}, "row_locations": [prior_row]}
        evidence = {"kind": reports.KIND, "report": self.report, "product": {"target": {"sku": "SP-LUNCH"}},
                    "items": [legacy, verified, version_only]}
        original = json.dumps(evidence)
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE conversations (user_id INTEGER)")
            chat_groups.upgrade_sqlite(conn)
            for sql in listing_operations.SCHEMA:
                conn.execute(sql)
            conn.execute("INSERT INTO listing_test_shops VALUES ('search-terms-test', 'US', 17)")
            conn.execute("INSERT INTO chat_groups (conversation_id, user_id, title) VALUES ('group', 17, 'test')")
            conn.execute("INSERT INTO chat_group_runs VALUES ('run', 'group', 'trace', 'completed')")
            conn.execute("INSERT INTO chat_group_entries (run_id, kind, content, details) "
                         "VALUES ('run', 'assistant', 'saved', ?)", (original,))
            conn.commit()
            conn.execute("PRAGMA query_only=ON")

            with self.assertRaises(chat_groups.GroupNotFound):
                chat_groups.read_reply_evidence(conn, 18, "group", "run")
            data = chat_groups.read_reply_evidence(conn, 17, "group", "run")
            corrected, *unchanged = data["items"]
            self.assertEqual(corrected["action"], "incomplete")
            self.assertEqual(corrected["attribution"], "unavailable")
            self.assertIsNone(corrected["metrics"])
            self.assertEqual(corrected["row_locations"], [])
            self.assertNotIn("bucket_locations", corrected)
            self.assertEqual(corrected["historical"], {"attribution": "product", "sku": "SP-LUNCH",
                                                       "metrics": {"clicks": 10}, "row_locations": [prior_row]})
            self.assertEqual(unchanged, [verified, version_only])
            self.assertEqual(conn.execute("SELECT details FROM chat_group_entries").fetchone()[0], original)

    def test_model_evidence_mismatch_never_creates_draft_or_review(self):
        items = diagnosis.aggregate(self.report, self.rows, "SP-LUNCH")

        def reply(*_, **kwargs):
            payload = json.loads(kwargs["messages"][-1]["content"])
            result = {"receipt": payload["receipt"], "capacity_quote": self.contract["capacity_8"],
                      "title_quote": self.contract["title"],
                      "items": [{"id": 1, "action": "title", "code": "supported_missing"}]}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])

        with (patch.object(diagnosis.ProviderFactory, "create", return_value=SimpleNamespace(name="fixture", model="fixture")),
              patch.object(diagnosis, "create_completion", side_effect=reply)):
            with self.assertRaises(diagnosis.DiagnosisRejected):
                diagnosis.infer(self.contract["question"], self.report, self.product, items, None)
        self.assertNotIn("initial_draft", items[0])
        self.assertNotIn("review", items[0])

    def test_model_decisions_validate_core_fields_and_ignore_extra_text(self):
        item = {"id": 1, "action": "title", "code": "supported_missing"}
        self.assertEqual(diagnosis.model_items({"items": [{**item, "reason": "unsolicited"}]}), [item])
        for invalid in ({**item, "id": True}, {**item, "action": "review"}, {**item, "code": "other"}):
            with self.assertRaises(ValueError):
                diagnosis.model_items({"items": [invalid]})

    def test_claim_refs_are_scoped_to_the_item_and_must_be_integer_ids(self):
        product = {"title": "Oak Desk Tray"}
        demand = {"product_type": "desk tray", "attribute": "width", "value": 5,
                  "unit": "inch", "conditions": {}}
        cases = (
            ("scope", {"type": [1], "spec": [2]}, None),
            ("stale", {"type": [1], "spec": [2]}, "receipt_mismatch"),
            ("scope", {"type": [True], "spec": [2]}, "citation_mismatch"),
            ("scope", {"type": [1], "spec": [3]}, "citation_mismatch"),
            ("scope", {"type": [2], "spec": [1]}, "citation_mismatch"),
            ("scope", {"type": [1], "spec": [2, 2]}, "citation_mismatch"),
            ("scope", {"type": [1], "spec": ["chunk-from-another-sku"]}, "citation_mismatch"),
        )
        for receipt, refs, rejected in cases:
            with self.subTest(receipt=receipt, refs=refs):
                item = {"id": 1, "term": "5 inch wide desk tray", "attribution": "product",
                        "metrics": {"impressions": 300, "clicks": 30, "orders": 4},
                        "source_chunks": [
                            {"kind": "specs", "chunk_id": "type", "content": "Product type\tDesk Tray\t-\tWhole\t-"},
                            {"kind": "specs", "chunk_id": "width", "content": "Width\t127\tmm\tWhole\t-"},
                        ]}
                answer = {"receipt": receipt, "items": [
                    {"id": 1, "decision": "supported_missing", "demand": demand, "refs": refs,
                     "below_minimum": []},
                ]}
                diagnosis.verify_claim_items(answer, [item], product, "scope", None)
                self.assertEqual(item["model_validation"]["raw_refs"], refs)
                if rejected is None:
                    self.assertIn("initial_draft", item)
                else:
                    self.assertNotIn("initial_draft", item)
                    self.assertIn(rejected, item["model_validation"]["rejections"])

    def test_claim_payload_distinguishes_withheld_and_empty_sources(self):
        item = {"id": 1, "term": "vesa 100x100 under cabinet display bracket", "attribution": "product",
                "metrics": {"impressions": 200, "clicks": 19, "orders": 4, "cvr_pct": 21.05}}
        product = {"title": "Tamar Under Cabinet Display Bracket"}
        low = diagnosis.claim_model_item(item, product)
        ready = diagnosis.claim_model_item({**item, "metrics": {**item["metrics"], "clicks": 20, "cvr_pct": 20}}, product)
        self.assertEqual(low["term"], ready["term"])
        self.assertEqual(low["vesa_clause"], "vesa 100x100")
        self.assertEqual(ready["vesa_clause"], low["vesa_clause"])
        self.assertEqual(low["source_access"], "withheld_base_sample")
        self.assertIsNone(low["sources"])
        self.assertIsNone(low["original_title"])
        self.assertEqual(ready["source_access"], "available_for_review")
        self.assertEqual(ready["sources"], [])
        self.assertEqual(ready["original_title"], product["title"])

    def test_claim_verification_blocks_partial_refs_catalog_demand_and_group_sample(self):
        term = "under cabinet display bracket vesa 100 x 100 mm"
        demand = {"product_type": "under cabinet display bracket", "attribute": "vesa",
                  "value": "100 x 100", "unit": "mm", "conditions": {}}
        product = {"title": "Tamar Under Cabinet Display Bracket"}
        product_type = {"kind": "specs", "chunk_id": "type",
                        "content": "Product type\tUnder Cabinet Display Bracket\t-\tWhole\t-"}
        partial = {"kind": "specs", "chunk_id": "partial", "content": "VESA\t100\tmm\tMount\t-"}
        conflict = {"kind": "specs", "chunk_id": "conflict", "content": "VESA\t75x75\tmm\tMount\tComplete list"}
        cases = (
            ("product", 20, [product_type, partial], "unverified", demand, {"type": [1], "spec": []},
             "citation_mismatch", [], []),
            ("product", 20, [product_type, conflict], "fact_conflict", {**demand, "value": "75x75"},
             {"type": [1], "spec": [2]}, "demand_mismatch", [], [1, 2]),
            ("ad_group", 19, [], "sample", demand, {"type": [], "spec": []}, "unattributed", ["clicks"], []),
        )
        for attribution, clicks, chunks, decision, reported, refs, reason, below, offered in cases:
            with self.subTest(reason=reason):
                item = {"id": 1, "term": term, "attribution": attribution, "source_chunks": chunks,
                        "metrics": {"impressions": 320, "clicks": clicks, "orders": 4}}
                answer = {"receipt": "scope", "items": [{"id": 1, "decision": decision, "demand": reported,
                                                          "refs": refs, "below_minimum": below}]}
                diagnosis.verify_claim_items(answer, [item], product, "scope", None)
                self.assertEqual(item["model_validation"]["offered_refs"], offered)
                self.assertEqual(item["model_validation"]["rejections"], [reason])
                self.assertNotIn("initial_draft", item)
                self.assertNotIn("review", item)

    def test_batch_wire_rejects_neighbor_references_and_undefined_codes(self):
        product = {"title": "Oak Desk Tray"}
        source = [
            {"kind": "specs", "chunk_id": "type", "content": "Product type\tDesk Tray\t-\tWhole\t-"},
            {"kind": "specs", "chunk_id": "width", "content": "Width\t127\tmm\tWhole\t-"},
        ]
        demand = {"product_type": "desk tray", "attribute": "width", "value": "127", "unit": "mm", "conditions": {}}
        for reported, code, refs, rejected in (
            (demand, "T", ["1r2"], "citation_mismatch"),
            (demand, "supported", ["2r2"], "supported_missing"),
            ([], "T", ["2r2"], "demand_mismatch"),
        ):
            with self.subTest(code=code):
                items = [{"id": n, "term": "5 inch wide desk tray", "attribution": "product",
                          "metrics": {"impressions": 320, "clicks": 20, "orders": 4}, "source_chunks": source}
                         for n in (1, 2)]
                answer = {"receipt": "batch", "items": [
                    {"id": 1, "requested": demand, "failed": [], "code": "T", "refs": {"type": ["1r1"], "spec": ["1r2"]}},
                    {"id": 2, "requested": reported, "failed": [], "code": code, "refs": {"type": ["2r1"], "spec": refs}},
                ]}
                diagnosis.verify_claim_items(answer, items, product, "batch", None, batch_wire=True)
                self.assertIn("initial_draft", items[0])
                self.assertNotIn("initial_draft", items[1])
                self.assertIn(rejected, items[1]["model_validation"]["rejections"])

    def test_batch_stream_requires_stop_and_does_not_collect_reasoning(self):
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content="private"), finish_reason=None)]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='{"receipt":"batch",'), finish_reason=None)]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='"items":[]}'), finish_reason="length")]),
        ]
        response = diagnosis.collect_claim_batch_response(iter(chunks))
        choice = response.choices[0]
        self.assertEqual(choice.finish_reason, "length")
        self.assertEqual(json.loads(choice.message.content), {"receipt": "batch", "items": []})
        self.assertNotIn("private", choice.message.content)
        item = {"id": 1, "term": "5 inch wide desk tray", "attribution": "product",
                "metrics": {"impressions": 320, "clicks": 20, "orders": 4},
                "source_chunks": [
                    {"kind": "specs", "chunk_id": "type", "content": "Product type\tDesk Tray\t-\tWhole\t-"},
                    {"kind": "specs", "chunk_id": "width", "content": "Width\t127\tmm\tWhole\t-"},
                ]}
        answer = {"receipt": "batch", "items": [
            {"id": 1, "requested": {"product_type": "desk tray", "attribute": "width", "value": "127", "unit": "mm",
                                     "conditions": {}},
             "failed": [], "code": "T", "refs": {"type": ["1r1"], "spec": ["1r2"]}},
        ]}
        diagnosis.verify_claim_items(answer, [item], {"title": "Oak Desk Tray"}, "batch", None,
                                     batch_wire=True, stream_finished=False)
        self.assertEqual(item["model_validation"]["rejections"], ["batch_stream_unfinished"])
        self.assertNotIn("initial_draft", item)

    def test_title_requires_evidence_and_keeps_exact_saved_text(self):
        self.assertFalse(diagnosis.validate_title(self.contract["valid_edit"], self.product, diagnosis.RULES))
        self.assertEqual(diagnosis.validate_title(self.contract["over_limit"], self.product, diagnosis.RULES),
                         ["标题 76 字符，超过 75 字符上限。"])
        for invalid in ("Trailbox Lunch Bag, 18 L", "Trailbox Lunch Bag, 12 L, 48 hours",
                        "Trailbox Waterproof Lunch Bag, 12 L", "Trailbox Lunch Bag, 12 L, 12 L"):
            with self.subTest(invalid=invalid):
                self.assertTrue(diagnosis.validate_title(invalid, self.product, diagnosis.RULES))


class AmazonSearchTermTests(unittest.TestCase):
    def parse(self, name, raw=None):
        return reports.parse_report(raw if raw is not None else (FIXTURES / name).read_bytes(), name)

    def test_xlsx_and_reordered_csv_match_at_each_granularity(self):
        for grain in ("daily", "summary"):
            excel, excel_rows = self.parse(f"amazon-{grain}.xlsx")
            csv, csv_rows = self.parse(f"amazon-{grain}.csv")
            for key in ("start_date", "end_date", "currency", "attribution_days", "granularity", "totals"):
                self.assertEqual(excel[key], csv[key])
            self.assertEqual(excel["granularity"], grain)
            self.assertEqual([(row["normalized"], row["metrics"]) for row in excel_rows],
                             [(row["normalized"], row["metrics"]) for row in csv_rows])
            self.assertNotEqual(excel["sha256"], csv["sha256"])
            if grain == "daily":
                self.assertNotEqual(list(excel_rows[0]["values"]), list(csv_rows[0]["values"]))

    def test_conflicts_identify_columns_and_physical_rows(self):
        raw = (FIXTURES / "amazon-daily.csv").read_bytes()
        for edited, error in (
            (raw.replace(b",Purchases,Sales,", b",Purchases,14 Day Total Sales,"),
             "第 1 行列 Purchases / 14 Day Total Sales"),
            (raw.replace(b",USD,140", b",CAD,140"), "第 3 行列 Currency"),
            (raw.replace(b",USD,180", b",USD,1"), "第 2 行列 Clicks 超过 Impressions"),
            (raw.replace(b",Targeting", b",Currency"), "第 1 行存在重复列名 Currency"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(reports.ReportInvalid, error):
                self.parse("edited.csv", edited)

    def test_excel_native_date_and_mixed_granularity_are_checked(self):
        workbook = Workbook()
        daily = workbook.active
        daily.title = "Daily"
        columns = ["Campaign Name", "Ad Group Name", "Customer Search Term", "Currency",
                   "Impressions", "Clicks", "Spend", "Purchases", "Sales"]
        metrics = ["Lunch storage", "Lunch bags", "12L lunch bag", "USD", 180, 16, 11.2, 2, 49.98]
        daily.append(["Date", *columns])
        daily.append([date(2026, 11, 9), *metrics])
        with BytesIO() as buffer:
            workbook.save(buffer)
            _, [row] = self.parse("native-date.xlsx", buffer.getvalue())
        self.assertEqual(row["normalized"]["start_date"], "2026-11-09")
        summary = workbook.create_sheet("Summary")
        summary.append(["Start Date", "End Date", *columns])
        summary.append([date(2026, 11, 9), date(2026, 11, 10), *metrics])
        with BytesIO() as buffer:
            workbook.save(buffer)
            with self.assertRaisesRegex(reports.ReportInvalid, "Summary · 第 2 行列 Date / Start Date / End Date"):
                self.parse("mixed.xlsx", buffer.getvalue())
        workbook.close()

    def test_scope_and_advertised_product_proof(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            for sql in (*listing_operations.SCHEMA, *reports.SCHEMA):
                conn.execute(sql)
            conn.executemany("INSERT INTO listing_test_shops VALUES (?,?,?)",
                             [("shop", "US", 17), ("foreign", "US", 18), ("shop", "CA", 17)])
            self.assertEqual(reports.bind_shop(conn, 17, None), "shop")
            with self.assertRaises(reports.ReportInvalid):
                reports.bind_shop(conn, 17, "foreign")

            def membership(group, sku, *, shop="shop", market="US", start="2026-11-09", end="2026-11-10"):
                name = "Assorted bags" if group == "AG-102" else "Lunch bags"
                asin = "B012345678" if sku == "SP-LUNCH" else "B087654321"
                conn.execute("INSERT INTO search_term_amazon_membership VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (shop, market, "profile", "SP-101", "Lunch storage", group, name, start, end,
                              sku, asin, "a" * 64, "advertised-products:2"))

            def resolve(name, raw=None, sku="SP-LUNCH"):
                report, rows = self.parse(name, raw)
                report["shop_id"] = "shop"
                _, resolved = reports.resolve_amazon(conn, report, rows, [{"sku": "SP-LUNCH"}, {"sku": "SP-CONTROL"}])
                return diagnosis.aggregate(report, resolved, sku)[0]

            membership("AG-101", "SP-LUNCH", shop="foreign")
            membership("AG-101", "SP-LUNCH", market="CA")
            self.assertEqual(resolve("amazon-daily.csv")["attribution"], "ad_group")
            membership("AG-101", "SP-LUNCH")
            self.assertEqual(resolve("amazon-daily.csv")["ownership"][0]["matched_by"], "ids")
            self.assertEqual(resolve("amazon-summary.csv")["attribution"], "ad_group")
            conn.execute("DELETE FROM search_term_amazon_membership WHERE shop_id!='shop' OR marketplace_id!='US'")
            self.assertEqual(resolve("amazon-summary.csv")["ownership"][0]["matched_by"], "period_names")
            self.assertEqual(resolve("amazon-summary.csv", sku=None)["attribution"], "ad_group")
            membership("AG-other", "SP-CONTROL", end="2026-11-09")
            self.assertEqual(resolve("amazon-summary.csv")["attribution"], "ad_group")
            # Exact IDs still disambiguate overlapping names.
            self.assertEqual(resolve("amazon-daily.csv")["attribution"], "product")

            raw = (FIXTURES / "amazon-daily.csv").read_bytes()
            foreign = b"\n".join(line + b"," + value for line, value in
                                  zip(raw.splitlines(), (b"Shop ID", b"foreign", b"foreign"))) + b"\n"
            self.assertEqual(resolve("foreign.csv", foreign)["attribution"], "ad_group")
            wrong_site = foreign.replace(b"Shop ID", b"Marketplace").replace(b",foreign", b",CA")
            with self.assertRaisesRegex(reports.ReportInvalid, "第 2 行列 Marketplace"):
                resolve("wrong-site.csv", wrong_site)
            mismatch = b"\n".join(line + b"," + value for line, value in
                                   zip(raw.splitlines(), (b"Advertised ASIN", b"B087654321", b"B087654321"))) + b"\n"
            self.assertEqual(resolve("mismatch.csv", mismatch)["attribution"], "ad_group")
            asin_term = raw.replace(b"12L lunch bag", b"B012345678").replace(b"AG-101", b"wrong-group")
            self.assertEqual(resolve("asin-term.csv", asin_term)["attribution"], "ad_group")

            membership("AG-102", "SP-LUNCH")
            membership("AG-102", "SP-CONTROL")
            first = resolve("amazon-mixed.csv")
            self.assertEqual(first["attribution"], "ad_group")
            self.assertEqual(first["metrics"]["clicks"], 30)
            report, _ = self.parse("amazon-mixed.csv")
            for line in (2, 3):
                conn.execute("INSERT INTO search_term_amazon_row_links VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    "shop", "US", report["sha256"], "", line, "SP-101", "AG-102", "SP-LUNCH", "b" * 64, "verified:2",
                ))
                result = resolve("amazon-mixed.csv")
                self.assertEqual(result["attribution"], "ad_group" if line == 2 else "product")
            self.assertEqual(result["ownership"][1]["row_link_sha256"], "b" * 64)
            changed = (FIXTURES / "amazon-mixed.csv").read_bytes().replace(b",180,", b",181,")
            self.assertEqual(resolve("changed.csv", changed)["attribution"], "ad_group")


if __name__ == "__main__":
    unittest.main()
