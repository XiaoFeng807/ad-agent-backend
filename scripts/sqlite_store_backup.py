"""Back up or restore a stopped StorePilot SQLite instance and verify its manifest."""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import file_digest, sha256
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from time import perf_counter


BUSINESS = "business.sqlite3"
METRICS = "metrics.sqlite3"
START_RATE = BUSINESS + ".start-rate.sqlite3"
DATABASES = (BUSINESS, METRICS, START_RATE)
MANIFEST = "backup-manifest.json"
DIRECTORIES = ("knowledge", BUSINESS + ".memory", "product-sources", "audit")
SIDECARS = {db + suffix for db in (BUSINESS, START_RATE) for suffix in ("-wal", "-shm", "-journal")}


def sha(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def read_sqlite(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def sqlite_inventory(path):
    with closing(read_sqlite(path)) as conn:
        conn.execute("BEGIN")
        integrity = conn.execute("PRAGMA integrity_check").fetchall()
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != [("ok",)] or violations:
            raise ValueError(f"SQLite integrity or foreign key check failed for {path.name} ({len(violations)} FK violations)")
        schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name").fetchall()
        tables = {}
        for kind, name, _, _ in schema:
            if kind != "table":
                continue
            identifier = '"' + name.replace('"', '""') + '"'
            encoded = (dumps([{"blob_sha256": sha256(v).hexdigest()} if isinstance(v, bytes) else v for v in row])
                       for row in conn.execute(f"SELECT * FROM {identifier}"))
            # Sort encoded rows so the digest is independent of the scan plan and hidden rowid.
            row_digests = sorted(sha256(row.encode()).digest() for row in encoded)
            digest = sha256()
            for row_digest in row_digests:
                digest.update(row_digest)
            tables[name] = {"rows": len(row_digests), "sha256": digest.hexdigest()}
        return {"integrity": "ok", "foreign_key_violations": 0, "tables": tables,
                "schema_sha256": sha256(dumps(schema).encode()).hexdigest()}


def files(root, *, source=False):
    if not (root / BUSINESS).is_file():
        raise ValueError(f"Missing {BUSINESS}")
    allowed = {*DATABASES, *DIRECTORIES}
    ignored = SIDECARS | {BUSINESS + ".executor-locks"} if source else {MANIFEST}
    found = {}

    def visit(path):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or path.is_junction():
            raise ValueError(f"Symlink or junction in store: {relative}")
        if path.is_dir():
            for child in sorted(path.iterdir()):
                visit(child)
        elif path.is_file():
            found[relative] = {"size": path.stat().st_size, "sha256": sha(path)}
        else:
            raise ValueError(f"Unsupported file type: {relative}")

    for item in sorted(root.iterdir()):
        if item.name in ignored:
            continue
        if item.name not in allowed:
            raise ValueError(f"Unexpected entry: {item.name}")
        if item.name in DIRECTORIES and not item.is_dir():
            raise ValueError(f"Expected a directory: {item.name}")
        visit(item)
    return found


@contextmanager
def staged(destination):
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent, prefix=".sqlite-store-") as temporary:
        staging = Path(temporary) / "store"
        staging.mkdir()
        yield staging
        if destination.exists():
            raise FileExistsError(destination)
        staging.rename(destination)


def copy_files(source, destination, names):
    for name in names:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)


def verify(bundle):
    manifest = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))
    if manifest["format"] != 1 or files(bundle) != manifest["files"]:
        raise ValueError("Backup file manifest mismatch")
    sql = {name: sqlite_inventory(bundle / name) for name in DATABASES if (bundle / name).is_file()}
    if sql != manifest["sql"]:
        raise ValueError("Backup SQL manifest mismatch")
    return manifest


def backup(source, destination):
    started = perf_counter()
    if destination.is_relative_to(source):
        raise ValueError("Use a destination outside the source directory")
    cutoff = datetime.now(timezone.utc).isoformat()
    before = files(source, source=True)
    before_sql = {name: sqlite_inventory(source / name) for name in DATABASES if name in before}
    snapshot_dbs = [name for name in (BUSINESS, START_RATE) if name in before]
    with staged(destination) as staging:
        copy_files(source, staging, before.keys() - set(snapshot_dbs))
        for name in snapshot_dbs:
            with closing(read_sqlite(source / name)) as reader, closing(sqlite3.connect(staging / name)) as writer:
                reader.backup(writer)
                if writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise ValueError(f"Could not produce a standalone snapshot of {name}")
        if files(source, source=True) != before or any(
                sqlite_inventory(source / name) != before_sql[name] for name in snapshot_dbs):
            raise ValueError("Source changed during backup")
        manifest = {"format": 1, "quiesced_at_utc": cutoff, "source_files": before, "files": files(staging),
                    "sql": before_sql, "backup_seconds": round(perf_counter() - started, 3)}
        write_json(staging / MANIFEST, manifest)
        verify(staging)
    return manifest


def restore(bundle, destination):
    started = perf_counter()
    if destination.is_relative_to(bundle):
        raise ValueError("Use a destination outside the backup directory")
    manifest = verify(bundle)
    with staged(destination) as staging:
        copy_files(bundle, staging, manifest["files"])
        if files(staging) != manifest["files"]:
            raise ValueError("Restored file manifest mismatch")
        for name, expected in manifest["sql"].items():
            if sqlite_inventory(staging / name) != expected:
                raise ValueError(f"Restored SQL manifest mismatch: {name}")
    return {"restore_seconds": round(perf_counter() - started, 3), "files": manifest["files"], "sql": manifest["sql"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("backup", "restore", "verify"):
        args = commands.add_parser(command)
        args.add_argument("source", type=Path)
        if command != "verify":
            args.add_argument("destination", type=Path)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    operation = {"backup": backup, "restore": restore, "verify": verify}[args.command]
    parameters = (source,) if args.command == "verify" else (source, args.destination.resolve())
    print(dumps(operation(*parameters)))


if __name__ == "__main__":
    main()
