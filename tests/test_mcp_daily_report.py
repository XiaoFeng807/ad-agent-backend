"""Exercise the daily report server as an independent MCP stdio client."""

from contextlib import asynccontextmanager, closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from backend.database import database


ROOT = Path(__file__).resolve().parents[1]
SERVER_ARGS = ["-B", "-m", "backend.mcp_daily_report"]
TOOL_NAME = "get_daily_report_by_date"
WINDOW = {"start_date": "2026-11-01", "end_date": "2026-11-02"}


class DailyReportMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.db = self.directory / "日报 # demo.sqlite3"
        with patch.object(database, "DB_PATH", self.db):
            database.init_db()

        self.observer = self.enterContext(closing(sqlite3.connect(self.db)))
        self.observer.execute("PRAGMA journal_mode=DELETE")
        with self.observer:
            self.observer.executemany(
                "INSERT INTO users (id, username, password) VALUES (?, ?, 'fixture')",
                [(1, "demo-one"), (2, "demo-two")],
            )
            self.observer.executemany(
                "INSERT INTO daily_reports (report_date, cost, impressions, clicks, sales, user_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("2026-11-02", 12, 1200, 24, 36, 1),
                    ("2026-11-01", 21, 2100, 63, 84, 2),
                    ("2026-11-01", 11, 1100, 22, 33, 1),
                    ("2026-11-03", 13, 1300, 26, 39, 1),
                    ("2026-10-31", 10, 1000, 20, 30, 1),
                ],
            )
        self.before = self.snapshot()

    def snapshot(self):
        return (
            self.observer.execute("PRAGMA data_version").fetchone()[0],
            tuple(self.observer.iterdump()),
            self.db.read_bytes(),
        )

    def tearDown(self):
        self.assertEqual(self.snapshot(), self.before, "MCP modified the demo database")

    @asynccontextmanager
    async def client(self, user_id):
        server = StdioServerParameters(
            command=sys.executable,
            args=[*SERVER_ARGS, "--user-id", str(user_id), "--db", str(self.db)],
            cwd=ROOT,
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                yield session

    async def test_discovers_one_read_only_tool(self):
        async with self.client(1) as session:
            tools = (await session.list_tools()).tools
        self.assertEqual([tool.name for tool in tools], [TOOL_NAME])
        tool = tools[0]
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertEqual(set(tool.inputSchema["properties"]), set(WINDOW))
        self.assertEqual(set(tool.inputSchema["required"]), set(WINDOW))
        self.assertFalse(tool.inputSchema["additionalProperties"])

    async def test_reads_inclusive_window_and_returns_empty_window(self):
        async with self.client(1) as session:
            result = await session.call_tool(TOOL_NAME, WINDOW)
            self.assertFalse(result.isError)
            self.assertEqual(result.structuredContent, {"success": True, "reports": [
                {"id": 3, "report_date": "2026-11-01", "cost": 11.0, "impressions": 1100,
                 "clicks": 22, "sales": 33.0, "user_id": 1},
                {"id": 1, "report_date": "2026-11-02", "cost": 12.0, "impressions": 1200,
                 "clicks": 24, "sales": 36.0, "user_id": 1},
            ]})
            self.assertEqual(json.loads(result.content[0].text), result.structuredContent)
            # The shared validator accepts a 90-day difference and rejects 91 days.
            empty = await session.call_tool(TOOL_NAME, {
                "start_date": "2026-01-01", "end_date": "2026-04-01",
            })
            self.assertFalse(empty.isError)
            self.assertEqual(empty.structuredContent, {"success": True, "reports": []})

    async def test_rejects_invalid_dates_and_argument_types(self):
        cases = [
            ({**WINDOW, "start_date": "2026/11/01"}, "日期格式错误"),
            ({**WINDOW, "end_date": "2026-11-31"}, "日期不合法"),
            ({**WINDOW, "start_date": "2026-11-03"}, "结束日期不能早于开始日期"),
            ({"start_date": "2026-01-01", "end_date": "2026-04-02"}, "不能超过90天"),
            ({**WINDOW, "start_date": 20261101}, "not of type 'string'"),
            ({"start_date": WINDOW["start_date"]}, "end_date"),
        ]
        async with self.client(1) as session:
            for arguments, error in cases:
                with self.subTest(arguments=arguments):
                    result = await session.call_tool(TOOL_NAME, arguments)
                    self.assertTrue(result.isError)
                    self.assertIn(error, result.content[0].text)
                    self.assertIsNone(result.structuredContent)

    async def test_isolates_both_users_and_rejects_spoofed_identity(self):
        for owner, other, costs in [(1, 2, [11, 12]), (2, 1, [21])]:
            with self.subTest(owner=owner):
                async with self.client(owner) as session:
                    await session.list_tools()
                    spoofed = await session.call_tool(TOOL_NAME, {**WINDOW, "user_id": other})
                    self.assertTrue(spoofed.isError)
                    self.assertIn("user_id", spoofed.content[0].text)
                    self.assertIsNone(spoofed.structuredContent)

                    result = await session.call_tool(TOOL_NAME, WINDOW)
                    self.assertFalse(result.isError)
                    reports = result.structuredContent["reports"]
                    self.assertEqual([report["cost"] for report in reports], costs)
                    self.assertEqual({report["user_id"] for report in reports}, {owner})

    def test_refuses_to_start_with_missing_or_invalid_configuration(self):
        missing = self.directory / "missing.sqlite3"
        invalid = self.directory / "invalid.sqlite3"
        invalid.write_bytes(b"not a SQLite database")
        db_arg = ["--db", str(self.db)]
        cases = [
            (db_arg, "--user-id"),
            (["--user-id", "1"], "--db"),
            *[(["--user-id", user_id, *db_arg], "positive SQLite integer")
              for user_id in ("0", "-1", "1.5", "9223372036854775808")],
            (["--user-id", "1", "--db", str(missing)], "readable demo SQLite database"),
            (["--user-id", "1", "--db", str(invalid)], "readable demo SQLite database"),
        ]
        for args, error in cases:
            with self.subTest(args=args):
                process = subprocess.run(
                    [sys.executable, *SERVER_ARGS, *args], cwd=ROOT, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                )
                self.assertEqual(process.returncode, 2)
                self.assertEqual(process.stdout, "")
                self.assertIn(error, process.stderr)
        self.assertFalse(missing.exists())
