"""TASK-026-T2 read-only Criteo recount, adjacent-window replay and sample freeze."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, timedelta
from fractions import Fraction
import gzip
import hashlib
import json
from pathlib import Path
import random
import sqlite3
from tempfile import mkdtemp

from scripts.run_task26_t1 import ARTIFACT, ROOT, read_only


RAW = ROOT / "data/public/criteo/criteo_attribution_dataset.tsv.gz"
PROVENANCE = ROOT / "docs/criteo-provenance.json"
OUTPUT = ROOT / "logs/task26-t2"
SAMPLE = ROOT / "docs/task26-t2-samples.json"
SEED = 2602009
SQL = """
WITH RECURSIVE starts(i, day) AS (
    SELECT 0, :first
    UNION ALL
    SELECT i+1, date(day, '+1 day') FROM starts WHERE date(day, '+7 day') <= :last
), plans AS (
    SELECT DISTINCT source_plan_id FROM plan_daily_metrics
), windows AS (
    SELECT p.source_plan_id, s.i, s.day, date(s.day, '+6 day') AS last,
           COUNT(m.metric_date) AS days, COALESCE(SUM(m.impressions), 0) AS impressions,
           COALESCE(SUM(m.clicks), 0) AS clicks
    FROM plans p CROSS JOIN starts s
    LEFT JOIN plan_daily_metrics m ON m.source_plan_id=p.source_plan_id
        AND m.metric_date BETWEEN s.day AND date(s.day, '+6 day')
    GROUP BY p.source_plan_id, s.i
)
SELECT b.source_plan_id, b.day, b.last, b.days, b.impressions, b.clicks,
       c.day, c.last, c.days, c.impressions, c.clicks
FROM windows b JOIN windows c ON b.source_plan_id=c.source_plan_id AND c.i=b.i+7
ORDER BY b.source_plan_id, b.i
"""


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_row(stream, row):
    stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def exact(value):
    return str(value) if value is not None else None


def ctr(window):
    return Fraction(window["clicks"], window["impressions"]) if window["impressions"] else None


def make_window(start, end, days, impressions, clicks):
    return dict(start_date=start, end_date=end, observed_days=days, impressions=impressions, clicks=clicks)


def pairs(conn, provenance):
    starts = dict(first=provenance["complete_start_date"], last=provenance["complete_end_date"])
    for source, *values in conn.execute(SQL, starts):
        base, current = [make_window(*values[index:index + 5]) for index in (0, 5)]
        assert base["observed_days"] == current["observed_days"] == 7
        base_ctr, current_ctr = ctr(base), ctr(current)
        relative = 1 - current_ctr / base_ctr if base_ctr and current_ctr is not None else None
        low = min(base["impressions"], current["impressions"]) < 1000
        if base_ctr is None or current_ctr is None:
            category = "zero_impressions"
        elif base_ctr == 0:
            category = "zero_baseline_ctr"
        elif low:
            category = "low_exposure"
        elif relative >= Fraction(1, 5):
            category = "review"
        elif relative > 0:
            category = "below_threshold"
        else:
            category = "flat_or_rising"
        yield dict(source_plan_id=source, windows=[base, current], category=category,
                   ctr=[exact(base_ctr), exact(current_ctr)], relative_decline=exact(relative),
                   below_1000=low, baseline_positive_exposure_zero_clicks=base_ctr == 0)


def read_source():
    expected = read_json(PROVENANCE)
    assert sha(ARTIFACT) == expected["artifact_sha256"]
    assert sha(RAW) == expected["compressed_sha256"]
    with closing(read_only(ARTIFACT)) as conn:
        manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
        assert manifest == {key: value for key, value in expected.items() if key != "artifact_sha256"}
        dates = conn.execute("SELECT DISTINCT metric_date FROM plan_daily_metrics ORDER BY metric_date").fetchall()
        rows = list(pairs(conn, manifest))
    version = f'{expected["compressed_sha256"]}|{expected["time_mapping"]["anchor_date"]}|aggregate-v1'
    assert hashlib.sha256(version.encode()).hexdigest() == expected["data_version"]
    plans = sorted({row["source_plan_id"] for row in rows}, key=int)
    assert len(rows) == 11475 and len(plans) == 675
    return expected, rows, {source: 101 + index for index, source in enumerate(plans)}, dates


def raw_recount(output):
    expected = read_json(PROVENANCE)
    raw_hash = sha(RAW)
    assert raw_hash == expected["compressed_sha256"]
    counts = defaultdict(dict)
    digest = hashlib.sha256()
    first, last, rows = None, -1, 0
    with gzip.open(RAW, "rb") as stream:
        header = stream.readline()
        digest.update(header)
        fields = header.rstrip().split(b"\t")
        ts, campaign, click = (fields.index(key) for key in (b"timestamp", b"campaign", b"click"))
        for line in stream:
            digest.update(line)
            values = line.split(b"\t", max(ts, campaign, click) + 1)
            timestamp = int(values[ts])
            clicked = int(values[click])
            assert timestamp >= last and clicked in (0, 1)
            if first is None:
                first = timestamp
            last = timestamp
            plan_days = counts[values[campaign].decode("ascii")]
            day = timestamp // 86400
            impressions, clicks = plan_days.get(day, (0, 0))
            plan_days[day] = impressions + 1, clicks + clicked
            rows += 1
    assert digest.hexdigest() == expected["tsv_sha256"]
    assert (rows, len(counts), first, last) == (
        expected["rows"], expected["campaigns"], expected["timestamp_min"], expected["timestamp_max"],
    )
    anchor = date.fromisoformat(expected["time_mapping"]["anchor_date"])
    daily = {(source, (anchor + timedelta(days=day)).isoformat()): days.get(day, (0, 0))
             for source, days in counts.items() for day in range(first // 86400, last // 86400 + 1)}
    with closing(read_only(ARTIFACT)) as conn:
        actual = {(p, d): (i, c) for p, d, i, c in conn.execute("SELECT * FROM plan_daily_metrics")}
    assert actual == daily
    write_json(output / "raw-recount.json", {
        "rows": rows, "campaigns": len(counts), "timestamp_range": [first, last],
        "raw_bytes": RAW.stat().st_size, "gzip_sha256": raw_hash, "tsv_sha256": digest.hexdigest(),
        "sqlite_sha256": sha(ARTIFACT), "matched_plan_days": len(daily),
        "zero_impression_plan_days": sum(i == 0 for i, _ in daily.values()),
    })


def message(plan, windows, request):
    first, last = windows[0], windows[-1]
    total = f'{first["start_date"]} 至 {last["end_date"]}'
    provenance = "只用计划历史曝光、点击和日期覆盖作为业务证据。"
    if request == "single":
        return (f"请直接查 #{plan} 在 {total} 这个单一窗口的总曝光、总点击、CTR 和日期覆盖，"
                f"引用知识库 v1 的窗口口径（给出引用编号）。只看该窗口的合计。{provenance}")
    span = total
    if request == "explicit":
        span = (f'基准 {first["start_date"]} 至 {first["end_date"]}，'
                f'当前 {last["start_date"]} 至 {last["end_date"]}')
    return (f"请复盘计划 #{plan} 在 {span} 的 CTR。按相邻等长七天比较，核实每窗日期覆盖、曝光、点击和 CTR。"
            f"完整且相对变化有定义时给出相对变化，按知识库 CTR 复核规则 v1 判定并给出引用编号。"
            f"如果缺报，请指出日期和哪些结论不能得出。请写清结论及依据。{provenance}")


def freeze(rows, output):
    rng = random.Random(SEED)
    shuffled = rows.copy()
    rng.shuffle(shuffled)
    selected, used, used_pairs = [], set(), set()

    def add(row, stratum, request, browser=False):
        plan = row["source_plan_id"]
        used.add(plan)
        used_pairs.add((plan, row["windows"][0]["start_date"]))
        windows = row["windows"]
        expected = row["category"]
        missing = None
        if request == "single":
            windows = [windows[rng.randrange(2)]]
            expected = "single"
        elif request == "missing":
            missing = (date.fromisoformat(windows[1]["start_date"]) + timedelta(days=3)).isoformat()
            expected = "missing"
        plan_id = 1001 + len(selected)
        case_id = f"T2-{len(selected) + 1:03d}"
        selected.append(dict(id=case_id, source_plan_id=plan, plan_id=plan_id, stratum=stratum,
                             history_category=row["category"], expected=expected, request=request,
                             transport="chromium" if browser else "asgi", windows=windows,
                             missing_date=missing, message=message(plan_id, windows, request)))

    hits = {row["source_plan_id"] for row in rows if row["category"] == "review"}
    assert len(hits) == 16
    for index, source in enumerate(sorted(hits, key=int)):
        row = next(row for row in shuffled if row["category"] == "review" and row["source_plan_id"] == source)
        add(row, "review", "overall" if index % 2 == 0 else "explicit", index < 2)

    strata = [
        ("near_threshold", 12, 2, lambda r: r["category"] == "below_threshold" and Fraction(r["relative_decline"]) >= Fraction(17, 100)),
        ("clear_rise", 17, 2, lambda r: r["category"] == "flat_or_rising" and Fraction(r["relative_decline"]) <= -Fraction(1, 5)),
        ("low_exposure", 14, 1, lambda r: r["category"] == "low_exposure"),
        ("zero_impressions", 14, 1, lambda r: r["category"] == "zero_impressions"),
        ("zero_baseline_ctr", 9, 1, lambda r: r["category"] == "zero_baseline_ctr"),
        ("single", 10, 1, lambda r: r["category"] in {"review", "below_threshold", "flat_or_rising"}),
        ("injected_missing", 8, 2, lambda r: r["category"] in {"review", "below_threshold", "flat_or_rising"}),
    ]
    for name, count, browsers, predicate in strata:
        eligible = [row for row in shuffled if predicate(row)]
        for index in range(count):
            available = [row for row in eligible if (row["source_plan_id"], row["windows"][0]["start_date"]) not in used_pairs]
            row = next((row for row in available if row["source_plan_id"] not in used), available[0])
            request = {"single": "single", "injected_missing": "missing"}.get(name)
            add(row, request=request or ("overall" if index % 2 == 0 else "explicit"),
                stratum=name, browser=index < browsers)
    assert len(selected) == 100 and len(used) >= 60
    assert sum(case["transport"] == "chromium" for case in selected) == 12
    payload = dict(seed=SEED, data_version=read_json(PROVENANCE)["data_version"],
                   source_plans=len(used), count=len(selected), cases=selected)
    if output.exists():
        assert read_json(output) == payload, "frozen sample differs"
    else:
        write_json(output, payload)
    return payload


def map_plan(conn, user, plan, source, provenance):
    conn.execute(
        "INSERT INTO ad_plans (id, plan_name, user_id, platform, cost, sales, daily_budget) "
        "VALUES (?, ?, ?, 'public-replay', NULL, NULL, NULL)",
        (plan, f"Criteo 历史映射 #{plan}", user),
    )
    conn.execute("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, ?)",
                 (plan, provenance["source_id"], provenance["data_version"], source))


def compare_tools(output, expected, rows, plans):
    from backend.config.config import settings
    from backend.database import database
    from backend.tools.tools import get_plan_metrics_by_date

    sandbox = Path(mkdtemp(prefix="tool-sandbox-", dir=output)) / "tool-mappings.sqlite3"
    settings.DIAGNOSIS_SANDBOX_DB_PATH = str(sandbox)
    settings.PLAN_METRICS_DB_PATH = str(ARTIFACT)
    database.init_db()
    with closing(database.get_db()) as conn, conn:
        for source, plan in plans.items():
            map_plan(conn, 17, plan, source, expected)
    windows = {(row["source_plan_id"], window["start_date"], window["end_date"]): window
               for row in rows for window in row["windows"]}
    failures, receipts, failed_keys = [], 0, set()
    with closing(read_only(ARTIFACT)) as conn, (output / "tool-receipts.jsonl").open("w", encoding="utf-8") as stream:
        for (source, start, end), expected_window in windows.items():
            data = get_plan_metrics_by_date(plans[source], start, end, user_id=17)
            daily = [dict(zip(("metric_date", "impressions", "clicks"), row)) for row in conn.execute(
                "SELECT metric_date, impressions, clicks FROM plan_daily_metrics "
                "WHERE source_plan_id=? AND metric_date BETWEEN ? AND ? ORDER BY metric_date", (source, start, end),
            )]
            oracle = dict(success=True, plan_id=plans[source], source_plan_id=source, scope="plan",
                          source_id=expected["source_id"], data_version=expected["data_version"],
                          source={key: expected[key] for key in ("kind", "source_url", "license",
                                  "compressed_sha256", "tsv_sha256", "sampling", "time_mapping")},
                          start_date=start, end_date=end, daily=daily,
                          impressions=expected_window["impressions"], clicks=expected_window["clicks"],
                          coverage=dict(complete=True, expected_days=7, observed_days=7, missing_dates=[]))
            receipts += 1
            diff = sorted(key for key in oracle.keys() | data.keys() if oracle.get(key) != data.get(key))
            item = {"expected_window": expected_window, "receipt": data, "different_fields": diff}
            if diff:
                failures.append(item)
                failed_keys.add((source, start, end))
            append_row(stream, item)
    failed_pairs = sum(any((row["source_plan_id"], w["start_date"], w["end_date"]) in failed_keys
                           for w in row["windows"]) for row in rows)
    return dict(actual_tool_calls=receipts, mismatched_receipts=len(failures), failed_pairs=failed_pairs,
                failures=failures, archive="tool-receipts.jsonl")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "tools", "raw"))
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--sample", type=Path, default=SAMPLE)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.mode == "raw":
        raw_recount(output)
        return
    expected, rows, plans, dates = read_source()
    sample = freeze(rows, args.sample)
    if args.mode == "freeze":
        print(json.dumps(dict(count=sample["count"], plans=sample["source_plans"], sha256=sha(args.sample))))
        return
    with (output / "comparisons.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            append_row(stream, row)
    tools = compare_tools(output, expected, rows, plans)
    metadata = dict(
        provenance=expected, raw_bytes=RAW.stat().st_size, artifact_bytes=ARTIFACT.stat().st_size,
        source_plans=len(plans), observed_dates=[row[0] for row in dates],
        pairs=len(rows), categories=dict(Counter(row["category"] for row in rows)),
        overlap_flags={flag: sum(row[flag] for row in rows)
                       for flag in ("below_1000", "baseline_positive_exposure_zero_clicks")},
        plans_by_category={key: len({r["source_plan_id"] for r in rows if r["category"] == key})
                           for key in sorted({row["category"] for row in rows})},
        tools=tools, sample_sha256=sha(args.sample), raw_sha256_after=sha(RAW), artifact_sha256_after=sha(ARTIFACT),
    )
    assert metadata["raw_sha256_after"] == expected["compressed_sha256"]
    assert metadata["artifact_sha256_after"] == expected["artifact_sha256"]
    write_json(output / "data-manifest.json", metadata)
    print(json.dumps({key: metadata[key] for key in ("source_plans", "pairs", "categories", "tools")}))
    return int(bool(tools["mismatched_receipts"]))


if __name__ == "__main__":
    raise SystemExit(main())
