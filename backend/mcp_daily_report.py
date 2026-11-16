"""Serve one user-scoped daily report tool over local MCP stdio."""

import argparse
import asyncio
from contextlib import closing
from pathlib import Path
import re
import sqlite3

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from backend.tools.daily_reports import query_daily_reports


TOOL = types.Tool(
    name="get_daily_report_by_date",
    description="查询已绑定用户的日报。起止日期均包含在内，最多相差 90 天。",
    inputSchema={
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "开始日期（YYYY-MM-DD）"},
            "end_date": {"type": "string", "description": "结束日期（YYYY-MM-DD）"},
        },
        "required": ["start_date", "end_date"],
        "additionalProperties": False,
    },
    annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)


def parse_user_id(value):
    if not re.fullmatch(r"[0-9]{1,19}", value) or not 0 < int(value) <= 2**63 - 1:
        raise argparse.ArgumentTypeError("must be a positive SQLite integer")
    return int(value)


async def serve(user_id, connect):
    server = Server("ad-agent-daily-report")

    @server.list_tools()
    async def list_tools():
        return [TOOL]

    @server.call_tool(validate_input=True)
    async def call_tool(name, arguments):
        if name != TOOL.name:
            raise ValueError("Unknown tool")
        try:
            result = query_daily_reports(
                arguments["start_date"], arguments["end_date"], user_id, connect
            )
        except sqlite3.Error:
            raise ValueError("无法读取演示日报库") from None
        if not result["success"]:
            raise ValueError(result["error"])
        return result

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=parse_user_id)
    parser.add_argument("--db", required=True, type=Path, help="path to an existing demo SQLite database")
    args = parser.parse_args()

    try:
        uri = args.db.resolve(strict=True).as_uri() + "?mode=ro"

        def connect():
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            return conn

        with closing(connect()) as conn:
            conn.execute("SELECT report_date, user_id FROM daily_reports LIMIT 0")
    except (OSError, sqlite3.Error):
        parser.error("--db must point to a readable demo SQLite database with daily_reports")

    asyncio.run(serve(args.user_id, connect))


if __name__ == "__main__":
    main()
