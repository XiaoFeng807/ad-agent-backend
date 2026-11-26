"""StorePilot's versioned Sponsored Products test report imports and title drafts."""

import csv
import hashlib
import io
import json
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import PureWindowsPath
from uuid import uuid4

from backend.database import listing_operations

KIND = "search_term_diagnosis_v1"
INSPECTION_KIND = "operations_priority_inspection_v1"
SOURCE = "Sponsored Products Search Term Test"
MARKETPLACE = "US"
TEST_MAX_BYTES = 128 * 1024
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 120
MAX_TERMS = 24
SCOPE_CHANGE_REASONS = {
    "product_version_changed": "商品资料、标题或证据版本已变化。",
    "source_rows_missing": "当前报表缺少原始行。",
    "ownership_changed": "商品访问权限或广告组逐行归属已变化。",
    "regrouped_merge": "原报表行已与其他条目合并。",
    "regrouped_split": "原报表行已拆分到多个条目。",
    "regrouped_page": "原报表行已移到另一批报表页。",
    "regrouped_scope": "原报表行的活动、广告组或搜索词范围已变化。",
}
HEADERS = (
    "Source", "Marketplace", "Shop ID", "Date", "Campaign ID", "Campaign", "Ad Group ID",
    "Ad Group", "Advertised SKUs", "Attributed SKU", "Search Term", "Impressions", "Clicks",
    "Spend USD", "Orders", "Sales USD",
)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS search_term_amazon_membership (
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, advertiser_id TEXT NOT NULL,
        campaign_id TEXT NOT NULL, campaign_name TEXT NOT NULL, ad_group_id TEXT NOT NULL,
        ad_group_name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
        sku TEXT NOT NULL, asin TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_location TEXT NOT NULL,
        PRIMARY KEY (shop_id, marketplace_id, campaign_id, ad_group_id, source_sha256, sku),
        FOREIGN KEY (shop_id, marketplace_id) REFERENCES listing_test_shops (shop_id, marketplace_id)
    )""",
    """CREATE TABLE IF NOT EXISTS search_term_amazon_row_links (
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, report_sha256 TEXT NOT NULL,
        sheet TEXT NOT NULL, line INTEGER NOT NULL, campaign_id TEXT NOT NULL, ad_group_id TEXT NOT NULL,
        sku TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_location TEXT NOT NULL,
        PRIMARY KEY (shop_id, marketplace_id, report_sha256, sheet, line),
        FOREIGN KEY (shop_id, marketplace_id) REFERENCES listing_test_shops (shop_id, marketplace_id)
    )""",
    """CREATE TABLE IF NOT EXISTS search_term_ad_groups (
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
        ad_group_id TEXT NOT NULL, sku TEXT NOT NULL,
        PRIMARY KEY (shop_id, marketplace_id, campaign_id, ad_group_id, sku),
        FOREIGN KEY (shop_id, marketplace_id, sku) REFERENCES listing_test_products (shop_id, marketplace_id, sku)
    )""",
    """CREATE TABLE IF NOT EXISTS search_term_report_imports (
        import_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES chat_groups(conversation_id),
        version INTEGER NOT NULL, sha256 TEXT NOT NULL, raw_csv BLOB NOT NULL, manifest TEXT NOT NULL,
        original_rows TEXT NOT NULL, UNIQUE(conversation_id, version), UNIQUE(conversation_id, sha256)
    )""",
    """CREATE TABLE IF NOT EXISTS search_term_title_drafts (
        run_id TEXT NOT NULL REFERENCES chat_group_runs(run_id), item_id INTEGER NOT NULL,
        revision INTEGER NOT NULL, title TEXT NOT NULL, PRIMARY KEY(run_id, item_id)
    )""",
)


class ReportInvalid(ValueError):
    pass


def historical_metrics(item, sku):
    return {"attribution": item["attribution"], "sku": sku if item["attribution"] == "product" else None,
            "metrics": item["metrics"], "row_locations": item.get("row_locations") or
            [{"sheet": None, "line": line} for line in item.get("row_lines", [])]}


def set_unavailable(item, reason):
    item.update(attribution="unavailable", metrics=None, ownership=[], row_locations=[],
                metric_recheck="unavailable", metrics_unavailable_reason=reason)
    item.pop("bucket_locations", None)
    item.pop("row_lines", None)


def normalize_legacy_metrics(evidence):
    sku = ((evidence.get("product") or {}).get("target") or {}).get("sku")
    for item in evidence["items"]:
        cause = item.get("incomplete_cause")
        if ("metric_recheck" in item or cause not in SCOPE_CHANGE_REASONS
                or cause == "product_version_changed"):
            continue
        if sku and item.get("bucket_locations"):
            # Older aggregates recorded a larger bucket only for a verified SKU subset.
            item["historical"] = historical_metrics({**item, "attribution": "product"}, sku)
        set_unavailable(item, "旧诊断未保存整组指标的核验结果。")


def _integer(value):
    if not re.fullmatch(r"[0-9]{1,9}", value):
        raise ValueError
    return int(value)


def _cents(value):
    if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", value):
        raise ValueError
    return int(Decimal(value) * 100)


def parse_report(raw, filename):
    if not 0 < len(raw) <= MAX_BYTES:
        raise ReportInvalid("报表须大于 0 字节且不超过 16 MB。")
    if PureWindowsPath(filename).suffix.lower() == ".csv":
        try:
            first_line = re.split(br"[\r\n]", raw, maxsplit=1)[0].decode("utf-8-sig")
            header = next(csv.reader(io.StringIO(first_line)), [])
        except (UnicodeDecodeError, csv.Error):
            header = []
        if tuple(header) == HEADERS or (header and header[0] == "Source" and "Attributed SKU" in header):
            return _parse_test_report(raw, filename)
    from backend.database.amazon_search_term_report import parse
    return parse(raw, filename)


def _parse_test_report(raw, filename):
    if len(raw) > TEST_MAX_BYTES:
        raise ReportInvalid("CSV 须大于 0 字节且不超过 128 KB。")
    try:
        decoded = raw.decode("utf-8-sig")
        lines = decoded.splitlines(keepends=True)
        reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
        if tuple(next(reader, ())) != HEADERS:
            raise ReportInvalid("CSV 列与 Sponsored Products 测试格式不符。")
        rows, scopes, distinct = [], {}, set()
        shop_id = None
        while True:
            line = reader.line_num + 1
            cells = next(reader, None)
            if cells is None:
                break
            if len(rows) >= MAX_ROWS:
                raise ReportInvalid(f"测试报表最多 {MAX_ROWS} 条数据行。")
            try:
                if len(cells) != len(HEADERS) or any(
                    len(cell) > 160 or any(not char.isprintable() for char in cell) for cell in cells
                ):
                    raise ValueError
                values = dict(zip(HEADERS, cells))
                source, market, shop, day, campaign_id, campaign, group_id, group, members, mapping, term = cells[:11]
                if source != SOURCE or market != MARKETPLACE or day != date.fromisoformat(day).isoformat():
                    raise ValueError
                if any(not value or value != value.strip() for value in (shop, campaign_id, campaign, group_id, group, term)):
                    raise ValueError
                if shop_id is None:
                    shop_id = shop
                members = members.split("|")
                if shop != shop_id or any(not sku or sku != sku.strip() for sku in members):
                    raise ValueError
                if len(set(members)) != len(members) or (mapping and mapping not in members):
                    raise ValueError
                impressions, clicks, spend, orders, sales = cells[11:]
                metrics = {"impressions": _integer(impressions), "clicks": _integer(clicks),
                           "spend_cents": _cents(spend), "orders": _integer(orders), "sales_cents": _cents(sales)}
                if metrics["clicks"] > metrics["impressions"]:
                    raise ValueError
                scope = {"campaign_id": campaign_id, "campaign": campaign, "ad_group_id": group_id,
                         "ad_group": group, "advertised_skus": sorted(members)}
                group_key = (campaign_id, group_id)
                if scopes.setdefault(group_key, scope) != scope:
                    raise ValueError
                row_key = (*group_key, day, term.casefold(), mapping)
                group_day_term = row_key[:-1]
                prior = distinct.intersection({row_key, (*group_day_term, "")})
                if prior or (not mapping and any(key[:-1] == group_day_term for key in distinct)):
                    raise ValueError
                distinct.add(row_key)
            except ValueError:
                raise ReportInvalid(f"CSV 第 {line} 行字段无效、范围不一致或重复。") from None
            rows.append({"line": line, "raw": "".join(lines[line - 1:reader.line_num]),
                         "values": values, "metrics": metrics})
    except (UnicodeDecodeError, csv.Error):
        raise ReportInvalid("请输入有效的 UTF-8 CSV。") from None
    if not rows:
        raise ReportInvalid("CSV 没有数据行。")
    if len({(key[0], key[1], key[3]) for key in distinct}) > MAX_TERMS:
        raise ReportInvalid(f"测试报表最多 {MAX_TERMS} 个活动、广告组与搜索词组合。")
    for row in rows:
        values = row["values"]
        if not values["Attributed SKU"]:
            scopes[(values["Campaign ID"], values["Ad Group ID"])]["has_unmapped_rows"] = True
    days = [row["values"]["Date"] for row in rows]
    manifest = {"filename": PureWindowsPath(filename).name[:180], "source": SOURCE, "marketplace": MARKETPLACE,
                "shop_id": shop_id, "test_data": True, "start_date": min(days), "end_date": max(days),
                "groups": list(scopes.values()), "row_count": len(rows), "sha256": hashlib.sha256(raw).hexdigest()}
    return manifest, rows


def has_shop_access(conn, user_id, manifest):
    return manifest["marketplace"] == MARKETPLACE and conn.execute(
        "SELECT 1 FROM listing_test_shops WHERE user_id=? AND shop_id=? AND marketplace_id=?",
        (user_id, manifest["shop_id"], manifest["marketplace"]),
    ).fetchone() is not None


def verify_scope(conn, user_id, manifest, *, include_title=True):
    shop = manifest["shop_id"]
    market = manifest["marketplace"]
    if manifest.get("format"):
        if not has_shop_access(conn, user_id, manifest):
            raise ReportInvalid("该店铺不在当前账号的美国站授权范围内。")
        return listing_operations.product_choices(conn, user_id, shop, market, include_title=include_title)
    authorized = listing_operations.product_choices(conn, user_id, shop, market, include_title=include_title)
    sku_set = {product["sku"] for product in authorized}
    for group in manifest["groups"]:
        registered = conn.execute(
            """SELECT sku FROM search_term_ad_groups WHERE shop_id=? AND marketplace_id=?
               AND campaign_id=? AND ad_group_id=?""",
            (shop, market, group["campaign_id"], group["ad_group_id"]),
        )
        stated = set(group["advertised_skus"])
        if stated != {row["sku"] for row in registered} or not stated <= sku_set:
            raise ReportInvalid("报表店铺或广告组商品与此账号已登记的测试范围不一致。")
    present = {sku for group in manifest["groups"] for sku in group["advertised_skus"]}
    return [product for product in authorized if product["sku"] in present]


def amazon_shops(conn, user_id):
    return [dict(row) for row in conn.execute(
        "SELECT shop_id FROM listing_test_shops WHERE user_id=? AND marketplace_id=? ORDER BY shop_id",
        (user_id, MARKETPLACE),
    )]


def bind_shop(conn, user_id, requested_shop):
    shops = [choice["shop_id"] for choice in amazon_shops(conn, user_id)]
    if requested_shop in shops:
        return requested_shop
    if not requested_shop and len(shops) == 1:
        return shops[0]
    raise ReportInvalid("请从当前账号授权范围选择美国站店铺。")


def _group_match(values, groups, start_date, end_date):
    by_names = not (values["campaign_id"] and values["ad_group_id"])
    overlapping = []
    for identity, snapshots in groups.items():
        if any(provided and provided != expected for provided, expected in
               zip((values["campaign_id"], values["ad_group_id"]), identity)):
            continue
        for snapshot in snapshots.values():
            sample = snapshot[0]
            if by_names and (sample["end_date"] < start_date or sample["start_date"] > end_date):
                continue
            if any(not values[id_key] and values[name_key].casefold() != sample[name_key + "_name"].casefold()
                   for id_key, name_key in (("campaign_id", "campaign"), ("ad_group_id", "ad_group"))):
                continue
            overlapping.append((identity, snapshot))
    if len({identity for identity, _ in overlapping}) != 1:
        return None
    overlapping = [snapshot for _, snapshot in overlapping
                   if snapshot[0]["end_date"] >= values["start_date"] and snapshot[0]["start_date"] <= values["end_date"]]
    # Conflicting membership in any overlapping snapshot invalidates the inference.
    signatures = {frozenset((entry["advertiser_id"], entry["sku"], entry["asin"]) for entry in snapshot)
                  for snapshot in overlapping}
    if len(signatures) != 1:
        return None
    return next((snapshot for snapshot in overlapping
                 if snapshot[0]["start_date"] <= values["start_date"] and
                 snapshot[0]["end_date"] >= values["end_date"]), None)


def resolve_amazon(conn, manifest, rows, products):
    shop, market = manifest["shop_id"], manifest["marketplace"]
    authorized = {product["sku"] for product in products}
    snapshots = {}
    advertisers = set()
    for row in conn.execute("""
        SELECT * FROM search_term_amazon_membership
        WHERE shop_id=? AND marketplace_id=? AND start_date<=? AND end_date>=?
        ORDER BY campaign_id, ad_group_id, source_sha256, sku
    """, (shop, market, manifest["end_date"], manifest["start_date"])):
        advertisers.add(row["advertiser_id"])
        identity = (row["campaign_id"], row["ad_group_id"])
        snapshot_key = tuple(row[key] for key in ("advertiser_id", "source_sha256", "start_date", "end_date",
                                                  "campaign_name", "ad_group_name"))
        snapshots.setdefault(identity, {}).setdefault(snapshot_key, []).append(row)
    links = {(row["sheet"], row["line"]): row for row in conn.execute("""
        SELECT * FROM search_term_amazon_row_links WHERE shop_id=? AND marketplace_id=? AND report_sha256=?
    """, (shop, market, manifest["sha256"]))}
    foreign_names = set()
    if any(not row["normalized"]["campaign_id"] or not row["normalized"]["ad_group_id"] for row in rows):
        foreign_names = {(entry["campaign_name"].casefold(), entry["ad_group_name"].casefold()) for entry in conn.execute("""
            SELECT DISTINCT campaign_name, ad_group_name FROM search_term_amazon_membership
            WHERE (shop_id!=? OR marketplace_id!=?) AND start_date<=? AND end_date>=?
        """, (shop, market, manifest["end_date"], manifest["start_date"]))}
    groups, resolved, matched = {}, [], {}
    for row in rows:
        values = row["normalized"]
        snapshot = None
        ambiguous_origin = (not values["advertiser"] and not (values["campaign_id"] and values["ad_group_id"])
                            and (values["campaign"].casefold(), values["ad_group"].casefold()) in foreign_names)
        if not ambiguous_origin and (not values["shop_id"] or values["shop_id"] == shop) and (
            not values["advertiser"] or advertisers == {values["advertiser"]}
        ):
            key = tuple(values[field] for field in ("campaign_id", "ad_group_id", "campaign", "ad_group",
                                                   "start_date", "end_date"))
            if key not in matched:
                matched[key] = _group_match(values, snapshots, manifest["start_date"], manifest["end_date"])
            snapshot = matched[key]
        campaign_id, ad_group_id = values["campaign_id"], values["ad_group_id"]
        mapped_sku = evidence = None
        members = []
        if snapshot:
            campaign_id, ad_group_id = snapshot[0]["campaign_id"], snapshot[0]["ad_group_id"]
            members = [entry["sku"] for entry in snapshot]
            link = links.get((row["sheet"] or "", row["line"]))
            verified_link = (link and (link["campaign_id"], link["ad_group_id"]) == (campaign_id, ad_group_id))
            candidate = next((entry for entry in snapshot if entry["sku"] == link["sku"]), None) if verified_link else None
            if len(members) == 1 and link is None:
                candidate = snapshot[0]
            if candidate and candidate["sku"] in authorized and (
                not values["sku"] or values["sku"] == candidate["sku"]
            ) and (not values["asin"] or values["asin"].upper() == candidate["asin"].upper()):
                mapped_sku = candidate["sku"]
                evidence = {"sku": mapped_sku, "asin": candidate["asin"],
                            "campaign_id": campaign_id, "ad_group_id": ad_group_id,
                            "matched_by": "ids" if values["campaign_id"] and values["ad_group_id"] else "period_names",
                            "membership_sha256": candidate["source_sha256"],
                            "membership_location": candidate["source_location"],
                            "membership_start": candidate["start_date"], "membership_end": candidate["end_date"]}
                if verified_link:
                    evidence.update(row_link_sha256=link["source_sha256"], row_link_location=link["source_location"])
        group_key = (campaign_id or values["campaign"].casefold(), ad_group_id or values["ad_group"].casefold())
        scope = groups.setdefault(group_key, {"campaign_id": campaign_id, "ad_group_id": ad_group_id,
                                              "campaign": values["campaign"], "ad_group": values["ad_group"],
                                              "advertised_skus": [], "has_unmapped_rows": False})
        scope["advertised_skus"] = sorted(set(scope["advertised_skus"]).union(members))
        scope["has_unmapped_rows"] |= mapped_sku is None
        resolved.append({**row, "group_key": group_key, "campaign_id": campaign_id, "ad_group_id": ad_group_id,
                         "advertiser_id": snapshot[0]["advertiser_id"] if snapshot else None,
                         "mapped_sku": mapped_sku, "ownership": evidence})
    return list(groups.values()), resolved


def verify_amazon_group_rows(shop_id, scope, term, bucket, resolved):
    """Verify the whole original search-term bucket before quoting ad-group or SKU metrics."""
    if not (scope["campaign_id"] and scope["ad_group_id"]):
        raise ReportInvalid("活动、广告组或搜索词身份无法唯一核验。")
    for location, row in resolved.items():
        values = row["normalized"]
        if location in bucket or values["term"].casefold() != term.casefold():
            continue
        if row["campaign_id"] and row["ad_group_id"]:
            continue
        if all(values[key + "_id"] == scope[key + "_id"] if values[key + "_id"] else
               values[key].casefold() == scope[key].casefold() for key in ("campaign", "ad_group")):
            raise ReportInvalid("存在同词报表行可能属于该广告组，无法确认整组范围。")
    rows = [row for key, row in resolved.items() if key in bucket]
    if len(rows) != len(bucket) or any(
        (row["campaign_id"], row["ad_group_id"]) != (scope["campaign_id"], scope["ad_group_id"]) for row in rows
    ):
        raise ReportInvalid("报表原行的活动或广告组身份无法唯一核验。")
    if (any(row["normalized"]["shop_id"] not in ("", shop_id) for row in rows)
            or len({row["normalized"]["advertiser"] for row in rows if row["normalized"]["advertiser"]}) > 1):
        raise ReportInvalid("报表行的店铺或广告账户范围不一致。")
    return rows


def save_import(conn, conversation_id, manifest, rows, raw):
    existing = conn.execute(
        "SELECT manifest FROM search_term_report_imports WHERE conversation_id=? AND sha256=?",
        (conversation_id, manifest["sha256"]),
    ).fetchone()
    if existing:
        previous = json.loads(existing["manifest"])
        if previous["shop_id"] != manifest["shop_id"]:
            raise ReportInvalid("同一原文件已导入到本会话的另一店铺范围。")
        return previous
    version = conn.execute("SELECT COALESCE(MAX(version), 0)+1 FROM search_term_report_imports WHERE conversation_id=?",
                           (conversation_id,)).fetchone()[0]
    manifest = {**manifest, "import_id": uuid4().hex, "version": version}
    conn.execute("INSERT INTO search_term_report_imports VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (manifest["import_id"], conversation_id, version, manifest["sha256"], raw,
                  json.dumps(manifest, ensure_ascii=False), json.dumps(rows, ensure_ascii=False)))
    return manifest


def read_import(conn, conversation_id, import_id):
    row = conn.execute(
        "SELECT manifest, original_rows FROM search_term_report_imports WHERE conversation_id=? AND import_id=?",
        (conversation_id, import_id),
    ).fetchone()
    if row is None:
        raise LookupError
    return json.loads(row["manifest"]), json.loads(row["original_rows"])


def read_original_rows(conn, conversation_id, report):
    """Resolve physical locations against the uploaded file and its recorded hash."""
    source = conn.execute("""
        SELECT raw_csv FROM search_term_report_imports
        WHERE conversation_id=? AND import_id=? AND sha256=?
    """, (conversation_id, report["import_id"], report["sha256"])).fetchone()
    if source is None:
        raise ReportInvalid("找不到对应的原始报表文件。")
    manifest, rows = parse_report(source["raw_csv"], report["filename"])
    keys = ("sha256", "format", "row_count", "start_date", "end_date", "currency", "granularity", "attribution_days")
    if any(manifest.get(key) != report.get(key) for key in keys):
        raise ReportInvalid("原始报表的文件哈希或统计口径不一致。")
    return rows


def report_summaries(conn, conversation_id, *, target_window=None):
    if target_window is not None:
        shop, marketplace, start, end = target_window
        return (json.loads(row["manifest"]) for row in conn.execute("""
            SELECT manifest FROM search_term_report_imports WHERE conversation_id=?
              AND json_extract(manifest, '$.shop_id')=? AND json_extract(manifest, '$.marketplace')=?
              AND json_extract(manifest, '$.start_date')<=? AND json_extract(manifest, '$.end_date')>=?
            ORDER BY version DESC""", (conversation_id, shop, marketplace, end, start)))
    return [json.loads(row["manifest"]) for row in conn.execute(
        "SELECT manifest FROM search_term_report_imports WHERE conversation_id=? ORDER BY version DESC LIMIT 20",
        (conversation_id,),
    )]


def read_drafts(conn, run_id):
    return {row["item_id"]: {"title": row["title"], "revision": row["revision"]} for row in conn.execute(
        "SELECT item_id, revision, title FROM search_term_title_drafts WHERE run_id=?", (run_id,))}

def sum_metrics(rows):
    metrics = {key: sum(row["metrics"][key] for row in rows) for key in rows[0]["metrics"]}
    impressions, clicks, orders = (metrics[key] for key in ("impressions", "clicks", "orders"))
    spend, sales = (metrics[key] for key in ("spend_cents", "sales_cents"))
    def ratio(numerator, denominator):
        if not denominator:
            return None
        return float((Decimal(numerator) / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {**metrics, "ctr_pct": ratio(100 * clicks, impressions), "cvr_pct": ratio(100 * orders, clicks),
            "cpc_usd": ratio(spend, 100 * clicks), "acos_pct": ratio(100 * spend, sales)}


def aggregate(report, rows, sku):
    if report.get("format"):
        return _aggregate_amazon(rows, sku)
    groups = {(group["campaign_id"], group["ad_group_id"]): group for group in report["groups"]}
    buckets = {}
    for row in rows:
        values = row["values"]
        scope = groups[(values["Campaign ID"], values["Ad Group ID"])]
        if sku in scope["advertised_skus"]:
            buckets.setdefault((scope["campaign_id"], scope["ad_group_id"], values["Search Term"].casefold()), []).append(row)
    items = []
    for (campaign, group, _), original in buckets.items():
        scope = groups[(campaign, group)]
        mixed = len(scope["advertised_skus"]) > 1
        attributed = not mixed
        items.append({"id": len(items) + 1, "term": original[0]["values"]["Search Term"],
                      "scope": scope, "attribution": "product" if attributed else "ad_group",
                      "row_lines": [row["line"] for row in original], "metrics": sum_metrics(original)})
    return items


def _aggregate_amazon(rows, sku):
    buckets = {}
    for row in rows:
        term = row["normalized"]["term"]
        buckets.setdefault((*row["group_key"], term.casefold()), []).append(row)
    items = []
    for original in buckets.values():
        verified = bool(sku) and all(row["mapped_sku"] for row in original)
        selected = [row for row in original if row["mapped_sku"] == sku] if verified else []
        first = original[0]["normalized"]
        scope = {"campaign": first["campaign"], "ad_group": first["ad_group"],
                 "campaign_id": original[0]["campaign_id"], "ad_group_id": original[0]["ad_group_id"]}
        included = selected or original
        row_locations = [{"sheet": row["sheet"], "line": row["line"]} for row in included]
        proof = [{**location, **row["ownership"]} for location, row in zip(row_locations, selected)]
        items.append({"id": len(items) + 1, "term": first["term"], "scope": scope,
                      "attribution": "product" if selected else "ad_group",
                      "row_locations": row_locations, "ownership": proof, "metrics": sum_metrics(included)})
        if len(included) != len(original):
            items[-1]["bucket_locations"] = [{"sheet": row["sheet"], "line": row["line"]} for row in original]
    return items
