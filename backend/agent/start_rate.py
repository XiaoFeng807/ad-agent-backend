"""Account start quotas shared by Web processes on the same host and data volume."""

import sqlite3
import sys
from contextlib import closing
from functools import cache
from pathlib import Path
from time import monotonic


@cache
def _boot_id():
    # A persisted Linux uptime second can recur after the host reboots.
    if sys.platform == "linux":
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return ""


class AccountStartRate:
    def __init__(self, requests_per_second, business_db_path):
        if requests_per_second < 0:
            raise ValueError("STOREPILOT_STARTS_PER_SECOND must be nonnegative")
        self.limit = requests_per_second
        self.path = Path(str(business_db_path) + ".start-rate.sqlite3")

    def acquire(self, user_id):
        """Return (admitted, actual monotonic second) only after a successful commit."""
        if self.limit == 0:
            return False, int(monotonic())
        # Leave time for the gateway to relay a storage failure (ext_authz timeout 2s).
        with closing(sqlite3.connect(self.path, timeout=0.5)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS account_start_rate (
                user_id INTEGER PRIMARY KEY, boot_id TEXT NOT NULL,
                window_second INTEGER NOT NULL, starts INTEGER NOT NULL)""")
            boot = _boot_id()
            second = int(monotonic())
            # The write lock protects one current generation for all accounts.
            previous = conn.execute("SELECT boot_id, window_second FROM account_start_rate LIMIT 1").fetchone()
            if previous is not None and previous != (boot, second):
                conn.execute("DELETE FROM account_start_rate")
            row = conn.execute("SELECT starts FROM account_start_rate WHERE user_id=?", (user_id,)).fetchone()
            allowed = row is None or row[0] < self.limit
            if allowed:
                conn.execute("""INSERT INTO account_start_rate(user_id, boot_id, window_second, starts)
                    VALUES (?, ?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET starts=starts+1""",
                             (user_id, boot, second))
        return allowed, second
