"""Stream the pinned public Criteo gzip into an independently read-only SQLite artifact."""

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

SOURCE_ID = "criteo-attribution-2017"
SOURCE_URL = "https://ailab.criteo.com/criteo-attribution-modeling-bidding-dataset/"
REVISION = "904188a63cbad78bee43cd26ff5ee4ac77903986"
DOWNLOAD_URL = f"https://huggingface.co/datasets/criteo/criteo-attribution-dataset/resolve/{REVISION}/criteo_attribution_dataset.tsv.gz"
EXPECTED_SHA256 = "94ac7a465564349bc7ba008602211d5990a3c53cc133abc0aadef61ea2391a98"
LICENSE = "CC-BY-NC-SA-4.0"


def sha256_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def import_metrics(raw, output, anchor="2026-10-01", *, expected_sha256=EXPECTED_SHA256,
                   source_id=SOURCE_ID, synthetic=False):
    """One pass, bounded by campaign/day cardinality. Replace atomically; never append."""
    anchor_day = date.fromisoformat(anchor)
    raw_hash = sha256_file(raw)
    if expected_sha256 and raw_hash != expected_sha256:
        raise ValueError("compressed input SHA256 mismatch")
    counts = defaultdict(lambda: [0, 0])
    rows, first, last = 0, None, None
    tsv_hash = hashlib.sha256()
    with gzip.open(raw, "rb") as stream:
        header = stream.readline()
        tsv_hash.update(header)
        fields = header.decode("utf-8").strip().split("\t")
        positions = [fields.index(name) for name in ("timestamp", "campaign", "click")]
        for line in stream:
            tsv_hash.update(line)
            values = line.rstrip(b"\r\n").split(b"\t")
            timestamp, campaign, clicked = (values[p] for p in positions)
            timestamp, clicked = int(timestamp), int(clicked)
            if timestamp < 0 or clicked not in (0, 1) or (last is not None and timestamp < last):
                raise ValueError("invalid or unsorted public event")
            if first is None:
                first = timestamp
            last = timestamp
            bucket = counts[(campaign.decode("ascii"), timestamp // 86400)]
            bucket[0] += 1
            bucket[1] += clicked
            rows += 1
    if not rows:
        raise ValueError("empty public dataset")
    version = hashlib.sha256(f"{raw_hash}|{anchor}|aggregate-v1".encode()).hexdigest()
    manifest = {
        "source_id": source_id, "data_version": version, "kind": "synthetic" if synthetic else "public",
        "source_url": SOURCE_URL if not synthetic else "test-fixture",
        "download_url": DOWNLOAD_URL if not synthetic else None, "revision": REVISION if not synthetic else None,
        "license": LICENSE if not synthetic else "test-only", "license_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        "attribution": "Diemert, Meynet, Galland, Lefortier (2017), Attribution Modeling Increases Efficiency of Bidding in Display Advertising",
        "compressed_sha256": raw_hash, "tsv_sha256": tsv_hash.hexdigest(),
        "sampling": "all rows of pinned, already sub-sampled anonymized public release; no additional row sampling" if not synthetic else "synthetic boundary fixture only",
        "rows": rows, "campaigns": len({key[0] for key in counts}), "timestamp_min": first, "timestamp_max": last,
        "time_mapping": {"anchor_date": anchor, "formula": "demo_date = anchor_date + floor(timestamp / 86400) days",
                         "timezone": "relative 86400-second bins; no original calendar date or timezone inferred",
                         "original_calendar_dates": False},
        # The final observed day may be truncated. Do not claim full coverage for it.
        "complete_start_date": (anchor_day + timedelta(days=(first + 86399) // 86400)).isoformat(),
        "complete_end_date": (anchor_day + timedelta(days=last // 86400 - 1)).isoformat(),
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="aggregate-", suffix=".sqlite3", dir=output.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(temporary)) as conn, conn:
            conn.execute("CREATE TABLE metadata (manifest TEXT NOT NULL)")
            conn.execute("INSERT INTO metadata VALUES (?)", (json.dumps(manifest, ensure_ascii=False),))
            conn.execute("""CREATE TABLE plan_daily_metrics (
                source_plan_id TEXT NOT NULL, metric_date TEXT NOT NULL,
                impressions INTEGER NOT NULL CHECK(impressions>=0),
                clicks INTEGER NOT NULL CHECK(clicks>=0 AND clicks<=impressions),
                PRIMARY KEY(source_plan_id, metric_date))""")
            conn.executemany("INSERT INTO plan_daily_metrics VALUES (?, ?, ?, ?)", (
                (campaign, (anchor_day + timedelta(days=day)).isoformat(), *counts[(campaign, day)])
                for campaign in sorted({key[0] for key in counts})
                for day in range(first // 86400, last // 86400 + 1)
            ))
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    manifest["artifact_sha256"] = sha256_file(output)
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/plan_metrics/criteo.sqlite3"))
    parser.add_argument("--anchor", default="2026-10-01")
    args = parser.parse_args()
    print(json.dumps(import_metrics(args.raw, args.output, args.anchor), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
