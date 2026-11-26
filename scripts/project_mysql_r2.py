"""Prepare an explicit, conservative diagnostic projection from a verified cold backup."""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from backend.database.mysql_r2_schema import R2_COLUMNS
from scripts.migrate_mysql_r2 import check_source_shape, inventory, open_source, source_digest
from scripts.sqlite_store_backup import BUSINESS, read_sqlite, sqlite_inventory, staged, verify, write_json


# Ordinary archive rows can carry authorization or version bindings in excluded
# tables. This projection also keeps the retired message archive in the source.
ARCHIVE_ONLY_ROWS = frozenset(("conversations", "messages"))
ALERT_BINDINGS = ("plan_id", "ctr_check_key", "ctr_check_evidence")


def project(bundle, destination):
    if destination.is_relative_to(bundle):
        raise ValueError("Use a destination outside the backup directory")
    manifest = verify(bundle)
    source = bundle / BUSINESS
    before = source_digest(source)
    source_tables = manifest["sql"][BUSINESS]["tables"]
    retained_only = sorted(set(source_tables) - R2_COLUMNS.keys() - {"sqlite_sequence"})
    with staged(destination) as staging:
        output = staging / "diagnosis.sqlite3"
        with closing(read_sqlite(source)) as reader, closing(sqlite3.connect(output)) as writer:
            reader.backup(writer)
            writer.execute("PRAGMA foreign_keys=OFF")
            with writer:
                for name in retained_only:
                    identifier = '"' + name.replace('"', '""') + '"'
                    writer.execute(f"DROP TABLE {identifier}")
                for name in sorted(ARCHIVE_ONLY_ROWS):
                    writer.execute(f"DELETE FROM {name}")
                # Keep only alerts which can be represented without losing their current binding.
                columns = {row[1] for row in writer.execute("PRAGMA table_info(alerts)")}
                bound = tuple(col for col in ALERT_BINDINGS if col in columns)
                removed_alerts = 0
                if bound:
                    removed_alerts = writer.execute("DELETE FROM alerts WHERE " + " OR ".join(
                        f"{col} IS NOT NULL" for col in bound)).rowcount
                    writer.execute("DROP INDEX IF EXISTS alerts_ctr_check_identity")
                    for col in bound:
                        writer.execute(f"ALTER TABLE alerts DROP COLUMN {col}")
                redacted = writer.execute("UPDATE sessions SET token=NULL WHERE token IS NOT NULL").rowcount
            if writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ValueError("Could not produce a standalone projection")
        with open_source(output) as conn:
            check_source_shape(inventory(conn), {name: {} for name in R2_COLUMNS})
        projected = sqlite_inventory(output)
        written = projected["tables"]
        for name in R2_COLUMNS.keys() - ARCHIVE_ONLY_ROWS - {"sessions", "alerts"}:
            if written[name] != source_tables[name]:
                raise ValueError(f"Unexpected data change in {name}")
        if written["alerts"]["rows"] + removed_alerts != source_tables["alerts"]["rows"]:
            raise ValueError("Alert projection count mismatch")
        if before != source_digest(source):
            raise ValueError("Source archive changed during projection")
        report = {"archive_sha256": before, "projection_sha256": source_digest(output),
                  "archived_tables": {name: source_tables[name] for name in retained_only},
                  "archived_rows_in_mapped_tables": {name: source_tables[name] for name in sorted(ARCHIVE_ONLY_ROWS)},
                  "session_tokens_cleared": redacted, "bound_alerts_archived": removed_alerts,
                  "source_alerts": source_tables["alerts"], "projection": projected}
        write_json(staging / "projection.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    report = project(args.bundle.resolve(strict=True), args.destination.resolve())
    print(f"Projection {report['projection_sha256']}; archived tables: {len(report['archived_tables'])}")


if __name__ == "__main__":
    main()
