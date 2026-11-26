"""Read Amazon Ads Sponsored Products seller Search Term Reports."""

import csv
import hashlib
import io
import re
from contextlib import closing
from datetime import date, datetime
from pathlib import PureWindowsPath
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from backend.database import search_term_reports as reports

FORMAT = "amazon-sp-us-seller-v1"
SOURCE = "Amazon Sponsored Products Search Term Report"
FIXTURE_LABEL = "官方字段夹具 · 真实导出待联调"
MAX_ROWS = 50_000
COLUMNS = {
    "term": ("Customer Search Term",),
    "campaign": ("Campaign Name",),
    "ad_group": ("Ad Group Name",),
    "currency": ("Currency",),
    "impressions": ("Impressions",),
    "clicks": ("Clicks",),
    "spend_cents": ("Spend", "Cost"),
    "orders": ("Purchases", "Purchases (#)", "7 Day Total Orders (#)", "7 Day Total Orders",
               "14 Day Total Orders (#)", "14 Day Total Orders"),
    "sales_cents": ("Sales", "7 Day Total Sales", "14 Day Total Sales"),
}
OPTIONAL = {
    "campaign_id": "Campaign ID", "ad_group_id": "Ad Group ID",
    "sku": "Advertised SKU", "asin": "Advertised ASIN",
    "marketplace": "Marketplace", "shop_id": "Shop ID",
    "advertiser": "Advertiser ID", "window": "Attribution Window",
    "day": "Date", "start_date": "Start Date", "end_date": "End Date",
}


def _csv(raw):
    decoded = raw.decode("utf-8-sig")
    lines = io.StringIO(decoded, newline="").readlines()
    reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
    while True:
        line = reader.line_num + 1
        cells = next(reader, None)
        if cells is None:
            return
        yield line, cells, "".join(lines[line - 1:reader.line_num])


def _excel(raw):
    with ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) > 250 or sum(item.file_size for item in members) > 64 * 1024 * 1024:
            raise reports.ReportInvalid("Excel 解压后的内容超过导入范围。")
        for member in members:
            if member.filename.endswith((".xml", ".rels")):
                content = archive.read(member)
                if b"\x00" in content or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", content, re.I):
                    raise reports.ReportInvalid("Excel 工作簿包含不受支持的 XML。")
    workbook = load_workbook(io.BytesIO(raw), read_only=True, keep_links=False)
    try:
        for sheet in workbook.worksheets:
            sheet.reset_dimensions()
            yield sheet.title, (
                (line, [cell.value for cell in row], None)
                for line, row in enumerate(sheet.iter_rows(), 1)
            )
    finally:
        workbook.close()


def _date(value):
    if isinstance(value, datetime) and value.time() == datetime.min.time():
        return value.date().isoformat()
    if type(value) is date:
        return value.isoformat()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError


def _text(value):
    return "" if value is None else str(value).strip()


def _decimal_text(value):
    return re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", _text(value).removeprefix("$"))


def _mapping(headers, where):
    normalized = {header.casefold(): header for header in headers if header}
    if len(normalized) != sum(bool(header) for header in headers):
        seen = set()
        repeated = []
        for header in filter(None, headers):
            if header.casefold() in seen:
                repeated.append(header)
            seen.add(header.casefold())
        raise reports.ReportInvalid(f"{where}存在重复列名 {' / '.join(dict.fromkeys(repeated))}。")
    mapping = {}
    missing = []
    for field, aliases in COLUMNS.items():
        present = [normalized[alias.casefold()] for alias in aliases if alias.casefold() in normalized]
        if len(present) > 1:
            raise reports.ReportInvalid(f"{where}列 {' / '.join(present)} 的指标口径冲突。")
        if not present:
            missing.append(" / ".join(aliases[:2]))
        else:
            mapping[field] = present[0]
    if missing:
        raise reports.ReportInvalid(f"{where}缺必填列 {'、'.join(missing)}。")
    mapping.update({key: normalized[alias.casefold()] for key, alias in OPTIONAL.items()
                    if alias.casefold() in normalized})
    if not {"start_date", "end_date"} <= mapping.keys() and (
        "day" not in mapping or {"start_date", "end_date"} & mapping.keys()
    ):
        raise reports.ReportInvalid(f"{where}日期需要 Date 或成对的 Start Date、End Date。")
    windows = {int(match[1]) for key in ("orders", "sales_cents")
               if (match := re.match(r"(\d+) Day", mapping[key], re.I))}
    if windows - {7}:
        raise reports.ReportInvalid(
            f"{where}列 {mapping['orders']} / {mapping['sales_cents']} 与美国站卖家 7 天点击归因口径冲突。"
        )
    return mapping


def _sheet(raw_rows, sheet, rows):
    header = next((entry for entry in raw_rows if any(_text(cell) for cell in entry[1])), None)
    if header is None:
        return
    header_line, cells, _ = header
    headers = [_text(cell) for cell in cells]
    where = f"{sheet + ' · ' if sheet else ''}第 {header_line} 行"
    mapping = _mapping(headers, where)
    for line, cells, raw in raw_rows:
        if not any(_text(cell) for cell in cells):
            continue
        where = f"{sheet + ' · ' if sheet else ''}第 {line} 行"
        if len(rows) >= MAX_ROWS:
            raise reports.ReportInvalid(f"Amazon 报表最多 {MAX_ROWS} 条数据行。")
        if any(_text(cell) for cell in cells[len(headers):]):
            raise reports.ReportInvalid(f"{where}存在无列名的额外字段。")
        cells = cells[:len(headers)] + [None] * max(0, len(headers) - len(cells))
        source = dict(zip(headers, cells))
        values = {}
        for key, column in mapping.items():
            value = source[column]
            try:
                if key in {"day", "start_date", "end_date"}:
                    value = _date(value)
                elif key in {"impressions", "clicks", "orders", "spend_cents", "sales_cents"}:
                    parse = reports._cents if key.endswith("_cents") else reports._integer
                    if isinstance(value, float) and value.is_integer():
                        value = int(value)
                    value = parse(_decimal_text(value) if key.endswith("_cents") else _text(value))
                else:
                    value = _text(value)
                    if len(value) > 512 or (key in COLUMNS and not value):
                        raise ValueError
                    if key == "currency" and value.upper() != "USD":
                        raise ValueError
                    if key == "marketplace" and value.upper() not in {"US", "UNITED STATES", "ATVPDKIKX0DER", ""}:
                        raise ValueError
                    if key == "window" and not re.fullmatch(r"7(?:[ -]?days?|d)?", value, re.I):
                        raise ValueError
            except ValueError:
                raise reports.ReportInvalid(f"{where}列 {column} 字段无效或与美国站卖家口径冲突。") from None
            values[key] = value
        day = values.get("day")
        start = values.get("start_date", day)
        end = values.get("end_date", day)
        if start > end or (day and (start != day or end != day)):
            raise reports.ReportInvalid(f"{where}列 Date / Start Date / End Date 日期范围冲突。")
        if values["clicks"] > values["impressions"]:
            raise reports.ReportInvalid(f"{where}列 Clicks 超过 Impressions。")
        rows.append({"sheet": sheet, "line": line, "raw": raw, "values": {
            name: value.isoformat() if isinstance(value, date) else value
            for name, value in source.items() if name
        }, "normalized": {**{key: values.get(key, "") for key in (
            "term", "campaign", "ad_group", "campaign_id", "ad_group_id", "sku", "asin",
            "shop_id", "advertiser"
        )}, "start_date": start, "end_date": end, "currency": "USD",
            "granularity": "daily" if day else "summary", "attribution_days": 7},
            "metrics": {key: values[key] for key in ("impressions", "clicks", "spend_cents", "orders", "sales_cents")}})


def parse(raw, filename):
    suffix = PureWindowsPath(filename).suffix.lower()
    rows = []
    try:
        if suffix == ".xlsx":
            with closing(_excel(raw)) as sheets:
                for sheet, source in sheets:
                    _sheet(source, sheet, rows)
        elif suffix == ".csv":
            _sheet(_csv(raw), None, rows)
        else:
            raise reports.ReportInvalid("请上传 .xlsx 或 UTF-8 .csv 报表。")
    except (UnicodeDecodeError, csv.Error, BadZipFile, InvalidFileException, ParseError,
            OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, reports.ReportInvalid):
            raise
        raise reports.ReportInvalid("无法读取 Excel 或 UTF-8 CSV 报表。") from None
    if not rows:
        raise reports.ReportInvalid("报表没有数据行。")
    first = rows[0]["normalized"]
    granularity = first["granularity"]
    for row in rows:
        values = row["normalized"]
        if values["granularity"] != granularity or (
            granularity == "summary" and (values["start_date"], values["end_date"])
            != (first["start_date"], first["end_date"])
        ):
            location = f"{row['sheet']} · " if row["sheet"] else ""
            raise reports.ReportInvalid(
                f"{location}第 {row['line']} 行列 Date / Start Date / End Date 与报表粒度或汇总周期冲突。"
            )
    return {"filename": PureWindowsPath(filename).name[:180], "source": SOURCE, "format": FORMAT,
            "source_note": FIXTURE_LABEL, "marketplace": "US", "currency": "USD",
            "granularity": granularity, "attribution_days": 7,
            "start_date": min(row["normalized"]["start_date"] for row in rows),
            "end_date": max(row["normalized"]["end_date"] for row in rows),
            "row_count": len(rows), "sha256": hashlib.sha256(raw).hexdigest(),
            "totals": {key: sum(row["metrics"][key] for row in rows) for key in rows[0]["metrics"]}}, rows
