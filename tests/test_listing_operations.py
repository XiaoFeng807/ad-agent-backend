"""TASK-042 storage and transaction checks on isolated SQLite."""

import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from backend.database import database, listing_operations as listing


class ListingOperationTests(unittest.TestCase):
    def setUp(self):
        logs = Path("logs").resolve()
        logs.mkdir(exist_ok=True)
        tmp = self.enterContext(TemporaryDirectory(prefix="listing-", dir=logs))
        self.enterContext(patch.object(database, "DB_PATH", str(Path(tmp) / "listing.sqlite3")))
        self.enterContext(patch.object(database.settings, "DIAGNOSIS_SANDBOX_DB_PATH", ""))
        database.init_db()
        with closing(database.get_db()) as conn, conn:
            conn.execute("INSERT INTO users(id, username, password) VALUES (1, 'owner', 'unused'), (2, 'other', 'unused')")
            conn.execute("INSERT INTO chat_groups(conversation_id, user_id, title) VALUES ('owner-group',1,'Owner'), ('other-group',2,'Other')")
            self.target = listing.seed_test_product(conn, 1)
            listing.seed_test_product(conn, 1, sku="CONTROL")
            listing.seed_test_product(conn, 2, shop_id="other-shop")

    def read(self, conn, user=1):
        return listing.read_listing(conn, user, **self.target)

    def facts(self, conn):
        return listing.read_facts(conn, 1, **self.target)

    def sale_windows(self, conn):
        return listing.read_sales(conn, 1, *self.target.values(), *listing.TEST_BASELINE, *listing.TEST_CURRENT)

    def create_action(self, conn, *, run_id="run-1", conversation_id="owner-group", comparison=None):
        run = SimpleNamespace(user_id=1, run_id=run_id, conversation_id=conversation_id)
        facts = self.facts(conn)
        original = self.read(conn)
        sales = self.sale_windows(conn)
        refs = {"sales": "sale-art", "facts": "facts-art", "listing": "listing-art"}
        choices = [{"fact_id": fact["fact_id"], "text": fact["options"][1]} for fact in reversed(facts["facts"][:5])]
        snapshot = listing.build_proposal(original, facts, choices, sales=sales, artifact_refs=refs,
                                          comparison=comparison)
        evidence = {"kind": listing.KIND, "target": self.target, "proposal": snapshot,
                    "listing": original, "facts": facts, "metrics": sales,
                    "artifacts": {tool: {"artifact_id": refs[key]} for key, tool in (
                        ("listing", "get_listing_current"), ("facts", "get_listing_facts"), ("sales", "get_listing_sales"),
                    )}}
        if comparison is not None:
            evidence.update(comparison_receipts=comparison, comparisons=[row["result"] for row in comparison],
                            candidates=[row["result"]["target"] for row in comparison])
        conn.execute("INSERT INTO chat_group_runs VALUES (?, ?, 'source-trace', 'running')", (run_id, conversation_id))
        entry_id = conn.execute("INSERT INTO chat_group_entries(run_id,kind,content,details) VALUES (?, 'assistant', ?, ?)",
                                (run_id, "Listing draft", json.dumps(evidence))).lastrowid
        action = listing.create_group_proposal(conn, run, entry_id, evidence)
        conn.execute("UPDATE chat_group_runs SET status='completed' WHERE run_id=?", (run_id,))
        return action, snapshot

    def approve(self, action, *, run_id="run-1", conversation_id="owner-group", user=1):
        with closing(database.get_db()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            return listing.approve(conn, user, conversation_id, run_id, action, "executed")

    def test_read_scope_and_server_computed_sales(self):
        with closing(database.get_db(read_only=True)) as conn:
            self.assertIsNone(self.read(conn, 2))
            self.assertEqual(listing.scope_error(conn, 2, **self.target), "listing_not_found")
            self.assertNotIn(self.target, listing.find_targets(conn, 2, self.target["sku"]))
            self.assertIsNone(listing.read_facts(conn, 2, **self.target))
            sales = self.sale_windows(conn)
            self.assertEqual((sales["baseline"]["units"], sales["current"]["units"]), (70, 49))
            self.assertEqual((sales["units_delta"], sales["units_change_pct"], sales["conversion_rate"]), (-21, -30, None))
            partial = listing.read_sales(conn, 1, *self.target.values(), "2026-10-29", "2026-11-04", "2026-11-05", "2026-11-11")
            self.assertEqual(partial["baseline"]["covered_days"], 6)
            self.assertIsNone(partial["baseline"]["units"])
            self.assertIsNone(partial["units_delta"])
            with self.assertRaisesRegex(listing.ListingInvalid, "invalid_listing_sales_window"):
                listing.read_sales(conn, 1, *self.target.values(), *listing.TEST_BASELINE, *listing.TEST_BASELINE)

    def test_draft_selects_actual_english_options_and_blocks_new_claims(self):
        with closing(database.get_db()) as conn, conn:
            product = self.read(conn)
            facts = self.facts(conn)
            choices = [{"fact_id": fact["fact_id"], "text": fact["options"][0]} for fact in facts["facts"][:5]]
            draft = listing.build_proposal(product, facts, choices)
            self.assertEqual(draft["after_bullets"], [row["text"] for row in choices])
            invented = [{**choices[0], "text": "Keeps water cold for 72 hours."}, *choices[1:]]
            with self.assertRaisesRegex(listing.ListingInvalid, "listing_unverified_bullet"):
                listing.build_proposal(product, facts, invented)
            with self.assertRaisesRegex(listing.ListingInvalid, "listing_unverified_bullet"):
                listing.build_proposal(product, facts, [choices[0], *choices[:4]])
            listing.seed_test_product(conn, 1, variant="changed")
            changed = self.facts(conn)
            self.assertGreater(changed["version"], facts["version"])
            with self.assertRaisesRegex(listing.ListingInvalid, "listing_unverified_bullet"):
                listing.build_proposal(product, changed, choices)
            revised = [{"fact_id": fact["fact_id"], "text": fact["options"][0]} for fact in changed["facts"][:5]]
            self.assertIn("750 mL", listing.build_proposal(product, changed, revised)["after_bullets"][0])
            listing.seed_test_product(conn, 1, variant="missing")
            missing = self.facts(conn)
            self.assertEqual(len(missing["facts"]), 4)
            with self.assertRaisesRegex(listing.ListingInvalid, "listing_unverified_bullet"):
                listing.build_proposal(product, missing, revised)

    def test_comparison_tie_zero_baseline_and_changed_competitor(self):
        control = {**self.target, "sku": "CONTROL"}
        with closing(database.get_db()) as conn, conn:
            chosen = self.sale_windows(conn)
            other = listing.read_sales(conn, 1, *control.values(), *listing.TEST_BASELINE, *listing.TEST_CURRENT)
            self.assertEqual(listing.rank_sales([chosen, other]), ("tied", None))
            self.assertEqual(listing.rank_sales([chosen, {**other, "baseline": {"units": 0}}]),
                             ("baseline_zero", None))
            window = conn.execute("SELECT series FROM listing_test_sales WHERE sku='CONTROL'").fetchone()
            series = json.loads(window["series"])
            series[listing.TEST_CURRENT[1]] += 1
            conn.execute("UPDATE listing_test_sales SET series=? WHERE sku='CONTROL'", (json.dumps(series),))
            other = listing.read_sales(conn, 1, *control.values(), *listing.TEST_BASELINE, *listing.TEST_CURRENT)
            self.assertEqual(listing.rank_sales([chosen, other]), ("selected", self.target))
            action, draft = self.create_action(conn, comparison=[{"artifact_id": "sale-art", "result": chosen},
                                                                  {"artifact_id": "other-art", "result": other}])
            self.assertEqual(len(draft["comparison_basis"]), 2)
            conn.execute("UPDATE listing_test_sales SET version=version+1 WHERE sku='CONTROL'")
            pending = listing.read_action(conn, 1, "owner-group", "run-1", action, review=True)
            self.assertIn("approval_blocked", pending)
        with self.assertRaisesRegex(listing.ListingConflict, "listing_evidence_changed"):
            self.approve(action)
        with closing(database.get_db(read_only=True)) as conn:
            self.assertEqual(self.read(conn)["bullets"], draft["before_bullets"])
            self.assertEqual(conn.execute("SELECT count(*) FROM listing_change_receipts").fetchone()[0], 0)

    def test_concurrent_approval_changes_only_bullets_and_version(self):
        with closing(database.get_db()) as conn, conn:
            initial = self.read(conn)
            control = listing.read_listing(conn, 1, listing.TEST_SHOP, listing.TEST_MARKETPLACE, "CONTROL")
            other = listing.read_listing(conn, 2, "other-shop", listing.TEST_MARKETPLACE, listing.TEST_SKU)
            action, draft = self.create_action(conn)
            pending = listing.read_action(conn, 1, "owner-group", "run-1", action)
            self.assertIsNone(pending["receipt"])
            self.assertEqual(pending["listing"], initial)
        with ThreadPoolExecutor(max_workers=6) as workers:
            results = list(workers.map(self.approve, [action] * 6))
        self.assertEqual(len({result["receipt"]["receipt_id"] for result in results}), 1)
        self.assertTrue(all(result["receipt"]["committed_version"] == 2 for result in results))
        with closing(database.get_db(read_only=True)) as conn:
            readback = listing.read_action(conn, 1, "owner-group", "run-1", action)
            self.assertEqual(readback, results[0])
            self.assertEqual(readback["listing"], {**initial, "version": 2, "bullets": draft["after_bullets"]})
            self.assertEqual(readback["proposal"], draft)
            self.assertEqual(listing.read_listing(conn, 1, listing.TEST_SHOP, listing.TEST_MARKETPLACE, "CONTROL"), control)
            self.assertEqual(listing.read_listing(conn, 2, "other-shop", listing.TEST_MARKETPLACE, listing.TEST_SKU), other)
            self.assertEqual(conn.execute("SELECT count(*) FROM listing_change_receipts").fetchone()[0], 1)

    def test_invalid_scope_lifecycle_and_stale_versions_cannot_apply(self):
        with closing(database.get_db()) as conn, conn:
            action, draft = self.create_action(conn)
        for user, group, run in ((2, "owner-group", "run-1"), (1, "other-group", "run-1"), (1, "owner-group", "foreign-run")):
            with self.subTest(user=user, group=group, run=run), self.assertRaises(listing.ListingNotFound):
                self.approve(action, user=user, conversation_id=group, run_id=run)
        for status in ("running", "stopped", "superseded"):
            with closing(database.get_db()) as conn, conn:
                conn.execute("UPDATE chat_group_runs SET status=? WHERE run_id='run-1'", (status,))
            with self.subTest(status=status), self.assertRaises(listing.ListingNotFound):
                self.approve(action)
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE chat_group_runs SET status='completed' WHERE run_id='run-1'")
            conn.execute("UPDATE chat_groups SET archived=1 WHERE conversation_id='owner-group'")
        with self.assertRaisesRegex(listing.ListingConflict, "listing_group_archived"):
            self.approve(action)
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE chat_groups SET archived=0 WHERE conversation_id='owner-group'")
            listing.seed_test_product(conn, 1, current_end="2026-11-13")
        with self.assertRaisesRegex(listing.ListingConflict, "listing_evidence_changed"):
            self.approve(action)
        with closing(database.get_db()) as conn, conn:
            self.assertEqual(self.read(conn)["bullets"], draft["before_bullets"])
            self.assertEqual(conn.execute("SELECT count(*) FROM listing_change_receipts").fetchone()[0], 0)
            listing.seed_test_product(conn, 1)
            revised, _ = self.create_action(conn, run_id="run-2")
            conn.execute("UPDATE listing_test_products SET version=version+1 WHERE shop_id=? AND sku=?",
                         (listing.TEST_SHOP, listing.TEST_SKU))
        with self.assertRaisesRegex(listing.ListingConflict, "listing_evidence_changed"):
            self.approve(revised, run_id="run-2")

    def test_receipt_failure_rolls_back_listing(self):
        with closing(database.get_db()) as conn, conn:
            initial = self.read(conn)
            action, _ = self.create_action(conn)
            conn.execute("CREATE TRIGGER inject_receipt BEFORE INSERT ON listing_change_receipts BEGIN "
                         "SELECT RAISE(ABORT, 'receipt_write_failed'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "receipt_write_failed"):
            self.approve(action)
        with closing(database.get_db()) as conn, conn:
            self.assertEqual(self.read(conn), initial)
            self.assertEqual(conn.execute("SELECT count(*) FROM listing_change_receipts").fetchone()[0], 0)
            conn.execute("DROP TRIGGER inject_receipt")
        self.assertEqual(self.approve(action)["receipt"]["committed_version"], 2)

    def test_provenance_requires_exact_committed_entry_and_scope(self):
        with closing(database.get_db()) as conn, conn:
            action, _ = self.create_action(conn)
            conn.execute("UPDATE chat_group_entries SET details=? WHERE run_id='run-1'", (json.dumps({"kind": listing.KIND}),))
        with self.assertRaisesRegex(listing.ListingConflict, "listing_provenance_changed"):
            self.approve(action)
        with closing(database.get_db()) as conn, conn:
            next_action, _ = self.create_action(conn, run_id="run-2")
            conn.execute("UPDATE listing_test_shops SET user_id=2 WHERE shop_id=?", (listing.TEST_SHOP,))
        with self.assertRaises(listing.ListingNotFound):
            self.approve(next_action, run_id="run-2")
        with closing(database.get_db(read_only=True)) as conn:
            self.assertEqual(listing.group_proposals(conn, 1, "owner-group"), {})
            self.assertEqual(conn.execute("SELECT count(*) FROM listing_change_receipts").fetchone()[0], 0)
