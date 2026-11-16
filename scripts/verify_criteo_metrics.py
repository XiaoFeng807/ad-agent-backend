"""Independent raw-file recount; does not call the importer or metrics query."""

import argparse
from collections import Counter
from contextlib import closing
import csv
from datetime import date, timedelta
from fractions import Fraction
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3


def verify(raw, artifact, campaign="13289562"):
    with open(raw, "rb") as stream:
        raw_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    impressions, clicks = Counter(), Counter()
    raw_rows, first_day, last_day = 0, None, None
    with gzip.open(raw, "rt", newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            raw_rows += 1
            day = int(row["timestamp"]) // (24 * 60 * 60)
            first_day = day if first_day is None else min(first_day, day)
            last_day = day if last_day is None else max(last_day, day)
            if row["campaign"] == campaign:
                impressions[day] += 1
                clicks[day] += int(row["click"])
    assert raw_rows and impressions, "raw dataset or selected campaign is empty"
    window_pairs = (((14, 21), (21, 28)), ((18, 21), (25, 28)))
    rounds = []
    with closing(sqlite3.connect(Path(artifact).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
        assert manifest["compressed_sha256"] == raw_hash
        assert manifest["rows"] == raw_rows
        anchor = date.fromisoformat(manifest["time_mapping"]["anchor_date"])
        daily = conn.execute("SELECT metric_date, impressions, clicks FROM plan_daily_metrics WHERE source_plan_id=? ORDER BY metric_date", (campaign,)).fetchall()
        # Every campaign has a bucket for every globally observed day, including
        # zero-impression days and the final (possibly partial) observed day.
        expected_dates = [(anchor + timedelta(days=day)).isoformat()
                          for day in range(first_day, last_day + 1)]
        assert [row[0] for row in daily] == expected_dates, "campaign day coverage mismatch"
        for day, observed_i, observed_c in daily:
            offset = (date.fromisoformat(day) - anchor).days
            assert (observed_i, observed_c) == (impressions[offset], clicks[offset]), f"daily counts mismatch: {day}"
        for base_range, cur_range in window_pairs:
            windows = []
            for start, end in (base_range, cur_range):
                i = sum(impressions[day] for day in range(start, end))
                c = sum(clicks[day] for day in range(start, end))
                start_date = (anchor+timedelta(days=start)).isoformat()
                end_date = (anchor+timedelta(days=end-1)).isoformat()
                observed = conn.execute("""SELECT COALESCE(SUM(impressions), 0), COALESCE(SUM(clicks), 0)
                    FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date BETWEEN ? AND ?""",
                    (campaign, start_date, end_date)).fetchone()
                assert (i, c) == observed, f"window totals mismatch: {start_date}..{end_date}"
                windows.append({"start_date": start_date, "end_date": end_date,
                                "impressions": i, "clicks": c, "ctr_pct": float(Fraction(c, i)*100)})
            b,c = (Fraction(w["clicks"], w["impressions"]) for w in windows)
            relative = (b-c)/b*100
            rounds.append({"baseline": windows[0], "current": windows[1], "decline_pp": float((b-c)*100),
                           "relative_decline_pct": float(relative), "relative_decline_exact": str(relative),
                           "v1_matched": min(w["impressions"] for w in windows)>=1000 and relative>=20})
    return {"verification": "independent csv.DictReader full raw-file recount; all campaign days compared with SQLite",
            "raw_rows_scanned": raw_rows, "compressed_sha256": raw_hash,
            "source_id": manifest["source_id"], "data_version": manifest["data_version"],
            "source_plan_id": campaign, "daily_rows_checked": len(daily), "rounds": rounds, "passed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--output", type=Path, default=Path("logs/task12-independent-recount.json"))
    args = parser.parse_args()
    result = verify(args.raw, args.artifact)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
