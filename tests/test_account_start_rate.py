import multiprocessing
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from backend.agent import start_rate
from scripts.sqlite_store_backup import START_RATE, backup, restore, verify


def compete(db_path, barrier, results):
    limiter = start_rate.AccountStartRate(2, db_path)
    with patch.object(start_rate, "monotonic", return_value=71.25):
        barrier.wait(timeout=15)
        results.put(limiter.acquire(42))


class AccountStartRateTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(self.enterContext(TemporaryDirectory(dir=Path.cwd()))) / "business.sqlite3"
        self.limiter = start_rate.AccountStartRate(1, self.db_path)

    def test_concurrent_initialization_and_shared_quota(self):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(4)
        results = ctx.Queue()
        processes = [ctx.Process(target=compete, args=(self.db_path, barrier, results)) for _ in range(4)]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            decisions = [results.get(timeout=1) for _ in processes]
            self.assertCountEqual(decisions, [(True, 71)] * 2 + [(False, 71)] * 2)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()
            results.close()

    def test_accounts_rollover_and_reused_uptime_after_linux_reboot(self):
        with patch.object(start_rate, "monotonic", return_value=90.75) as clock:
            with patch.object(start_rate, "_boot_id", return_value="boot-one"):
                self.assertEqual(self.limiter.acquire(5), (True, 90))
                self.assertEqual(self.limiter.acquire(5), (False, 90))
                self.assertEqual(self.limiter.acquire(6), (True, 90))
            with patch.object(start_rate, "_boot_id", return_value="boot-two"):
                self.assertEqual(self.limiter.acquire(5), (True, 90))
                clock.return_value = 91.00
                self.assertEqual(self.limiter.acquire(6), (True, 91))
        with closing(sqlite3.connect(self.limiter.path)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM account_start_rate").fetchall(), [(6, "boot-two", 91, 1)])

    def test_waiter_uses_second_after_obtaining_write_lock(self):
        connect = sqlite3.connect
        attempted = Event()

        def observed_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None)
            return conn

        with patch.object(start_rate, "monotonic", return_value=40.9) as clock:
            self.assertEqual(self.limiter.acquire(1), (True, 40))
            with closing(connect(self.limiter.path)) as locker, ThreadPoolExecutor(1) as pool:
                locker.execute("BEGIN IMMEDIATE")
                with patch.object(sqlite3, "connect", side_effect=observed_connect):
                    future = pool.submit(self.limiter.acquire, 1)
                    self.assertTrue(attempted.wait(timeout=2))
                    clock.return_value = 41.01
                    locker.commit()
                    self.assertEqual(future.result(timeout=2), (True, 41))

    def test_failed_commit_rolls_back_counter_then_recovers(self):
        with patch.object(start_rate, "monotonic", return_value=70):
            self.assertEqual(self.limiter.acquire(1), (True, 70))
            # In SQLite rollback journal mode a reader lets the write execute but blocks COMMIT.
            with closing(sqlite3.connect(self.limiter.path)) as reader:
                reader.execute("BEGIN")
                reader.execute("SELECT * FROM account_start_rate").fetchall()
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    self.limiter.acquire(2)
                self.assertEqual(reader.execute("SELECT user_id, starts FROM account_start_rate").fetchall(), [(1, 1)])
            self.assertEqual(self.limiter.acquire(2), (True, 70))
            self.assertEqual(self.limiter.acquire(2), (False, 70))

    def test_zero_and_negative_configuration(self):
        with self.assertRaisesRegex(ValueError, "must be nonnegative"):
            start_rate.AccountStartRate(-1, self.db_path)
        limiter = start_rate.AccountStartRate(0, self.db_path)
        self.assertFalse(limiter.acquire(1)[0])
        self.assertFalse(limiter.path.exists())

    def test_cold_restore_preserves_quota_and_rejects_unexpected_files(self):
        source = self.db_path.parent / "source"
        source.mkdir()
        db = source / self.db_path.name
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY)")
        limiter = start_rate.AccountStartRate(1, db)
        bundle = source.parent / "bundle"
        target = source.parent / "restored"
        with patch.object(start_rate, "monotonic", return_value=115):
            self.assertEqual(limiter.acquire(11), (True, 115))
            backup(source, bundle)
            self.assertIn(START_RATE, verify(bundle)["sql"])
            restore(bundle, target)
            self.assertEqual(start_rate.AccountStartRate(1, target / db.name).acquire(11), (False, 115))
        (source / (START_RATE + ".unexpected")).touch()
        with self.assertRaisesRegex(ValueError, "Unexpected entry"):
            backup(source, source.parent / "invalid-source")
        (bundle / START_RATE).write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "manifest mismatch"):
            restore(bundle, source.parent / "invalid-restore")
