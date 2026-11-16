"""Committed SQLite changes at the actual Listing handoff and write boundaries."""

import json
import sqlite3
import sys
from contextlib import closing
from threading import Event, Thread
from time import perf_counter_ns

from scripts import accept_task45_t1 as r1
from scripts import task45_t1_fixture as fixture
from scripts.task45_t2_observe import phase


CASES = (
    ("handoff-facts", "B"), ("handoff-owner", "B"), ("proposal-sales", "A"),
    ("approval-sales", "A"), ("approval-facts", "A"), ("approval-owner", "A"),
    ("approval-listing", "A"), ("approval-success", "A"),
)


class Fault:
    def __init__(self):
        self.name = None
        self.path = None
        self.record = {}

    def arm(self, name, path):
        self.name, self.path, self.record = name, path, {}

    def commit(self, boundary):
        if not self.name or not self.name.startswith(boundary + "-"):
            return
        variant = self.name.split("-", 1)[1]
        self.name = None
        if variant == "success":
            return
        released = Event()
        self.record = {"boundary": boundary, "variant": variant, "blocked_ns": perf_counter_ns()}

        def change():
            try:
                self.record["before"] = r1.snapshot(self.path)
                with closing(sqlite3.connect(self.path)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if variant == "owner":
                        owner = conn.execute("SELECT user_id FROM listing_test_shops WHERE shop_id=?",
                                             (fixture.SHOP,)).fetchone()[0]
                        other = conn.execute("SELECT id FROM users WHERE id<>? ORDER BY id", (owner,)).fetchone()
                        if other is None:
                            other = (conn.execute("INSERT INTO users(username,password) VALUES ('transfer','unused')").lastrowid,)
                        conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=?", (other[0], fixture.SHOP))
                        self.record["owners"] = [owner, other[0]]
                    elif variant == "facts":
                        conn.execute("UPDATE listing_test_facts SET version=version+1, facts='[]' WHERE sku=?",
                                     (fixture.TOTE,))
                    elif variant == "listing":
                        conn.execute("UPDATE listing_test_products SET version=version+1 WHERE sku=?", (fixture.TOTE,))
                    else:
                        series, = conn.execute("SELECT series FROM listing_test_sales WHERE sku=?",
                                               (fixture.RISER,)).fetchone()
                        changed = {day: 0 if fixture.WINDOWS[2] <= day <= fixture.WINDOWS[3] else units
                                   for day, units in json.loads(series).items()}
                        conn.execute("UPDATE listing_test_sales SET version=version+1, series=? WHERE sku=?",
                                     (json.dumps(changed), fixture.RISER))
                self.record["after_commit"] = r1.snapshot(self.path)
                if variant == "sales":
                    self.record["ranking_after_commit"] = r1.oracle(self.path, fixture.WINDOWS)
            except Exception as error:
                self.record["error"] = repr(error)
            finally:
                self.record["released_ns"] = perf_counter_ns()
                released.set()

        Thread(target=change, daemon=True).start()
        if not released.wait(8) or self.record.get("error"):
            raise AssertionError(f"Fault was not committed at {boundary}: {self.record.get('error')}")

    def handoff(self, original):
        def wrapped(runtime, task, *args, **kwargs):
            if task.task_id == "listing":
                self.commit("handoff")
            return original(runtime, task, *args, **kwargs)
        return wrapped

    def proposal(self, original):
        def wrapped(run, *args, **kwargs):
            if (kwargs.get("evidence") or {}).get("proposal"):
                self.commit("proposal")
            return original(run, *args, **kwargs)
        return wrapped

    def approval(self, original):
        def wrapped(*args, **kwargs):
            if sys._getframe(1).f_code.co_name == "confirm_chat_group_listing":
                self.commit("approval")
            return original(*args, **kwargs)
        return wrapped

    def score(self, name, obs, browser, after):
        boundary, variant = name.split("-", 1)
        expected = "listing_not_found" if variant == "owner" else "listing_evidence_changed"
        checks = {}
        if variant == "success":
            result = browser.get("forcedConfirmation", {})
            checks["approval committed"] = result.get("status") == 200 and len(after["receipts"]) == 1
            checks["same committed Listing and receipt"] = any(
                product["sku"] == fixture.TOTE and product["version"] == after["receipts"][0]["committed_version"]
                and json.loads(product["bullets"]) == json.loads(after["receipts"][0]["committed_bullets"])
                for product in after["products"]) if after["receipts"] else False
            checks["whole comparison reread in approval"] = len({
                row["arguments"]["sku"] for row in obs.reads if row["checkpoint"] == "approval"
                and row["source"] == r1.SALES}) == 3
            return checks
        record = self.record
        checks["independent SQL commit released the barrier"] = bool(record.get("after_commit") and not record.get("error"))
        checks["zero product writes, execution receipts or new proposals"] = bool(record.get("after_commit")
            and record["after_commit"] == after and not after["receipts"]
            and len(after["proposals"]) == (1 if boundary == "approval" else 0))
        if variant == "sales":
            checks["independent SQL selects the previously unselected SKU"] = any(
                row["selected"] and row["sku"] == fixture.RISER for row in record.get("ranking_after_commit", []))
        if boundary == "handoff":
            checks["handoff blocked with exact reason"] = any(row["stage"] == "dependency_input"
                and row["action"] == "blocked" and row["detail"] == "listing" and row["error_code"] == expected
                for row in obs.trace)
            checks["no downstream SDK input contained the stale fact and no draft was called"] = not any(
                phase(row["body"]) == "draft" or (phase(row["body"]) == "worker"
                    and json.loads(row["body"]["messages"][1]["content"])["task"]["task_id"] == "listing")
                for row in obs.requests)
            checks["no downstream business reads or approval entry"] = not any(
                row["source"] == r1.CURRENT or (variant == "owner" and row["start_ns"] > record.get("released_ns", 0))
                for row in obs.reads if row["checkpoint"] == "initial") and not browser.get("approveButtons")
            checks["completed reply explains the block"] = (
                (browser.get("rounds") or [{}])[-1].get("terminal", {}).get("type") == "done"
                and ("访问权限已变化" if variant == "owner" else "商品证据已变化") in browser.get("answer", ""))
        elif boundary == "proposal":
            checks["proposal rejected with exact reason"] = any(row["stage"] == "listing_proposal"
                and row["action"] == "blocked" and row.get("error_code") == expected for row in obs.trace)
            checks["zero approval entries and explicit reply"] = (
                not browser.get("approveButtons") and "本轮提案未提交" in browser.get("answer", ""))
        else:
            checks["stale approval explicitly rejected"] = (
                browser.get("forcedConfirmation", {}).get("status") == (404 if variant == "owner" else 409)
                and any(row["stage"] == "listing_action" and row["action"] == "blocked"
                        and row.get("error_code") == expected for row in obs.trace))
            if variant == "owner":
                checks["no unauthorized evidence readers reached by approval"] = not any(
                    row["checkpoint"] == "approval" for row in obs.reads)
        return checks
