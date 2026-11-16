"""Shared daily report query for the Agent and the local MCP tool."""

from contextlib import closing
from datetime import datetime
import re


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def query_daily_reports(start_date, end_date, user_id, connect):
    for value in (start_date, end_date):
        if not DATE_PATTERN.match(str(value)):
            return {"success": False, "error": f"日期格式错误: {value}，正确格式: YYYY-MM-DD"}

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return {"success": False, "error": "日期不合法"}
    if (end - start).days > 90:
        return {"success": False, "error": "查询范围不能超过90天，请缩小日期范围"}
    if end < start:
        return {"success": False, "error": "结束日期不能早于开始日期"}

    sql = "SELECT * FROM daily_reports WHERE report_date>=? AND report_date<=?"
    params = (start_date, end_date)
    if user_id:
        sql += " AND user_id=?"
        params += (user_id,)

    with closing(connect()) as conn:
        rows = conn.execute(sql + " ORDER BY report_date", params).fetchall()
    return {"success": True, "reports": [dict(row) for row in rows]}
