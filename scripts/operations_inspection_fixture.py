"""Freeze the three-SKU StorePilot inspection and its one-change controls."""

import csv
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

from scripts import task45_t1_fixture as sales
from scripts.accept_task42_t1 import read


QUESTION = "帮我看看这周有哪些商品值得优先处理，证据够的话整理成待办。"
CASES = ("complete", "8l", "low", "unattributed", "missing-day", "missing-fact")
CONTROLS = ("simple", "confirmed-zero", "unsafe-model", "stale-evidence")
THRESHOLD = 20
REPORTS = Path("tests/fixtures/search_terms")
CONTRACT = read(REPORTS / "frozen.json")
REPETITIONS = 2
SCORE_FIELDS = ("selection", "action", "evidence", "gaps")


def source(case):
    products = sales.source(case)
    tote = products[0]
    capacity = 8 if case == "8l" else 12
    tote["title"] = CONTRACT["title"]
    tote["facts_version"] = 8 if case in {"8l", "missing-fact"} else 7
    if case != "missing-fact":
        tote["facts"][0] = {"fact_id": "capacity", "statement": CONTRACT[f"capacity_{capacity}"],
                            "options": [f"{capacity} L"]}
    tote["facts"] += [
        {"fact_id": "brand", "statement": CONTRACT["brand"], "options": ["Trailbox"]},
        {"fact_id": "product_type", "statement": CONTRACT["product_type"],
         "options": ["Reusable Soft-Sided Insulated Lunch Bag, Zippered Cooler Tote"]},
    ]
    return products


def report(case):
    raw = (REPORTS / "amazon-daily.csv").read_bytes()
    reader = csv.DictReader(StringIO(raw.decode("utf-8-sig")))
    rows = list(reader)
    dates = {row["Date"] for row in rows}
    start, end = map(date.fromisoformat, sales.WINDOWS[-2:])
    for offset in range((end - start).days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        if day not in dates:
            rows.append({**rows[0], "Date": day, "Impressions": "0", "Clicks": "0", "Purchases": "0",
                         "Spend": "0.00", "Sales": "0.00"})
    out = StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        if case == "low":
            for key in ("Impressions", "Clicks", "Purchases"):
                row[key] = str(int(row[key]) // 2)
            for key in ("Spend", "Sales"):
                row[key] = f"{Decimal(row[key]) / 2:.2f}"
        writer.writerow(row)
    return out.getvalue().encode()


def membership():
    raw = (REPORTS / "amazon-advertised-products.csv").read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(StringIO(raw)))
    verified, _, mixed = rows
    start, end = sales.WINDOWS[-2:]
    return [(sales.SHOP, sales.MARKET, row["Advertiser ID"], row["Campaign ID"], row["Campaign Name"],
             verified["Ad Group ID"], verified["Ad Group Name"], start, end, sku, row["Advertised ASIN"])
            for row, sku in ((verified, sales.TOTE), (mixed, sales.RISER))]
