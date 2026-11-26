"""Authorized inspection evidence reads and the persisted report and sales basis."""

from datetime import date, timedelta

from backend.config.config import settings
from backend.database import listing_operations as listings, product_documents, search_term_reports as reports


REPORT = "get_inspection_report"


def _report_coverage(rows, days, amazon):
    covered, daily_activity = set(), {}
    for row in rows:
        if amazon:
            values = row["normalized"]
            start, end = values["start_date"], values["end_date"]
            daily = values["granularity"] == "daily"
        else:
            start = end = row["values"]["Date"]
            daily = True
        covered.update(day for day in days if start <= day <= end)
        if daily:
            daily_activity[start] = daily_activity.get(start, False) or any(row["metrics"].values())
    missing = [day for day in days if day not in covered]
    return {"covered_dates": [day for day in days if day in covered], "missing_dates": missing,
            "known_zero_dates": [day for day in days if daily_activity.get(day) is False],
            "complete": not missing, "basis": "daily_rows" if not amazon or daily_activity else "original_interval",
            "advertiser_id": rows[0]["advertiser_id"] if amazon else None}


def read_report(conn, user_id, conversation_id, target, windows, *, target_scoped=False):
    start, end = windows[-2:]
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    days = [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]
    empty = {"items": [], "window": [start, end], "coverage": []}
    # Select one revision. Pooling overlapping imports would mix snapshots and could double count rows.
    scope = (target["shop_id"], target["marketplace_id"], start, end) if target_scoped else None
    for report in reports.report_summaries(conn, conversation_id, target_window=scope):
        if ((report["shop_id"], report["marketplace"]) == (target["shop_id"], target["marketplace_id"])
                and report["start_date"] <= end and report["end_date"] >= start):
            result = _report_snapshot(conn, user_id, conversation_id, target, report, empty, days, target_scoped)
            if result is not None:
                return result
    return {**empty, "gap": "缺少本会话同店同站点、与当前销量窗重叠的 Sponsored Products 搜索词报表。"}


def _report_snapshot(conn, user_id, conversation_id, target, report, empty, days, target_scoped):
    start, end = empty["window"]
    try:
        products = reports.verify_scope(conn, user_id, report, include_title=False)
        rows = reports.read_original_rows(conn, conversation_id, report)
    except reports.ReportInvalid as error:
        return {**empty, "gap": str(error)}
    report_ref = {key: report[key] for key in ("import_id", "version", "sha256", "filename", "start_date", "end_date")}
    gaps = []
    amazon = bool(report.get("format"))
    if amazon:
        rows = [row for row in rows if row["normalized"]["start_date"] <= end
                and row["normalized"]["end_date"] >= start]
        def outside_window(row):
            return not start <= row["normalized"]["start_date"] <= row["normalized"]["end_date"] <= end

        if not target_scoped and any(map(outside_window, rows)):
            return {**empty, "report": report_ref, "gap": "有汇总报表行跨出当前销量窗，缺少同窗的日级来源。"}
        groups, rows = reports.resolve_amazon(conn, report, rows, products)
        resolved = {(row["sheet"], row["line"]): row for row in rows}
        sku = target["sku"]
        keys = {(group["campaign_id"] or group["campaign"].casefold(),
                 group["ad_group_id"] or group["ad_group"].casefold()) for group in groups
                if sku in group["advertised_skus"]}
        # Retain unresolved rows for known groups so an unmapped row cannot inflate SKU metrics.
        rows = [row for row in rows if row["group_key"] in keys]
        if target_scoped:
            if not any(row["mapped_sku"] in (None, sku) for row in rows):
                return None
            if any(map(outside_window, rows)):
                return {**empty, "report": report_ref, "gap": "有汇总报表行跨出当前销量窗，缺少同窗的日级来源。"}
        accounts = {}
        for row in rows:
            if row["advertiser_id"]:
                accounts.setdefault(row["group_key"], set()).add(row["advertiser_id"])
        ambiguous = {key for key, advertisers in accounts.items() if len(advertisers) > 1}
        if ambiguous:
            gaps.append("报表同一活动和广告组在当前窗内涉及多个广告账户，无法合并指标。")
        unique = {}
        for row in rows:
            if row["group_key"] not in ambiguous:
                identity = (row["group_key"], row["advertiser_id"], row["mapped_sku"], tuple(sorted(row["values"].items())))
                unique.setdefault(identity, row)
        rows = list(unique.values())
        items = reports.aggregate(report, rows, sku)
        unmapped = {(row["sheet"], row["line"]) for row in rows if row["mapped_sku"] is None}
        for item in items:
            item["unmapped_locations"] = [loc for loc in item["row_locations"] if (loc["sheet"], loc["line"]) in unmapped]
        items = [item for item in items if item["attribution"] == "product" or item["unmapped_locations"]]
        verified = []
        for item in items:
            bucket = {(loc["sheet"], loc["line"]) for loc in item.get("bucket_locations", item["row_locations"])}
            try:
                reports.verify_amazon_group_rows(target["shop_id"], item["scope"], item["term"], bucket, resolved)
            except reports.ReportInvalid as error:
                gaps.append(f"「{item['term']}」{error}")
            else:
                verified.append(item)
        items = verified
    else:
        rows = [row for row in rows if start <= row["values"]["Date"] <= end]
        items = reports.aggregate(report, rows, target["sku"])
        if target_scoped and not items:
            return None
    # Keep source lines for the conversation evidence panel. The model receives the aggregate and row locations.
    if len(items) > reports.MAX_TERMS:
        return {**empty, "report": report_ref,
                "gap": f"当前 SKU 的报表超过单轮 {reports.MAX_TERMS} 个搜索词，请缩小报表范围。"}
    locations = set()
    coverage = []
    row_index = {(row.get("sheet"), row["line"]): row for row in rows}
    for item in items:
        item["row_locations"] = item.get("row_locations") or [{"sheet": None, "line": line} for line in item["row_lines"]]
        item_locations = [(row["sheet"], row["line"]) for row in item["row_locations"]]
        locations.update(item_locations)
        if item["attribution"] == "product":
            scoped = [row_index[key] for key in item_locations]
            if not amazon or all(row["advertiser_id"] for row in scoped):
                item["coverage"] = _report_coverage(scoped, days, amazon)
            else:
                item["coverage"] = {"complete": False, "covered_dates": [], "missing_dates": days,
                                    "known_zero_dates": [], "basis": None, "advertiser_id": None}
            coverage.append({"item_id": item["id"], "term": item["term"], **item["scope"], **item["coverage"]})
    original = [row for row in rows if (row.get("sheet"), row["line"]) in locations]
    if not items and not gaps:
        gaps.append("缺少可与此 SKU 或其已登记广告组关联的搜索词报表行。")
    return {**empty, "report": report_ref, "coverage": coverage,
            "items": items, "rows": [{key: row[key] for key in ("sheet", "line", "raw", "values", "metrics") if key in row}
                                     for row in original],
            **({"gap": " ".join(dict.fromkeys(gaps))} if gaps else {})}


def publication_inputs(conn, user_id, conversation_id, spec):
    target = spec["publication"]["target"]
    windows = spec["windows"]
    return {
        "sales": listings.read_sales(conn, user_id, **target, **dict(zip(listings._WINDOW_KEYS, windows))),
        "report": read_report(conn, user_id, conversation_id, target, windows, target_scoped=True),
        "listing": listings.read_listing(conn, user_id, **target),
        "facts": listings.read_facts(conn, user_id, **target),
        "document_version": product_documents.active_version(conn, listings._key(target)),
        "threshold_pct": settings.LISTING_SIGNIFICANT_DECLINE_PCT,
    }


def publication_report_basis(report, artifact_id):
    if report.get("gap") or not report["coverage"] or any(not row["complete"] for row in report["coverage"]):
        return None
    return {"artifact_id": artifact_id, "report": report["report"], "window": report["window"],
            "coverage": report["coverage"], "row_locations": [
                {"sheet": row.get("sheet"), "line": row["line"]} for row in report["rows"]],
            "evidence_sha256": listings._digest(report)}


def complete_sales(sales):
    return sales is not None and all(sales[window]["units"] is not None for window in ("baseline", "current"))


def sales_correction(conn, user_id, previous, current):
    """Return comparable units only when the saved and current order reads can be verified."""
    if not previous or previous.get("target") != current["target"]:
        return None
    old_snapshot, new_snapshot = previous.get("order_snapshot"), current.get("order_snapshot")
    if (not old_snapshot or not new_snapshot or old_snapshot == new_snapshot
            or old_snapshot.get("source") != new_snapshot["source"]):
        return None
    old_window, new_window = previous.get("current") or {}, current["current"]
    units = old_window.get("units"), new_window["units"]
    if any(type(value) is not int for value in units) or units[0] == units[1]:
        return None
    start, end = new_window["start_date"], new_window["end_date"]
    if (old_window.get("start_date"), old_window.get("end_date")) != (start, end):
        return None
    historical = listings.read_order_sales_snapshot(conn, user_id, current["target"], old_snapshot["snapshot_id"])
    if historical is None or any(historical.get(key) != value for key, value in old_snapshot.items()):
        return None
    if listings._window(historical["series"], date.fromisoformat(start), date.fromisoformat(end)) == old_window:
        return units
