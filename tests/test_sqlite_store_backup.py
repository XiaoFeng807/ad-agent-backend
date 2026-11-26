"""Cold backup and restore behavior independent of the web fixture."""

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from scripts.sqlite_store_backup import BUSINESS, backup, restore, sqlite_inventory, verify


class SQLiteStoreBackupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(dir=Path.cwd()))).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.source / BUSINESS
        self.bundle = self.root / "bundle"
        self.target = self.root / "target"
        self.knowledge = self.source / "knowledge"
        self.knowledge.mkdir()
        (self.knowledge / "chunks.bin").write_bytes(b"fixed vector fixture")

    def test_backup_captures_committed_wal_and_preserves_sql(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE turns(id INTEGER PRIMARY KEY AUTOINCREMENT, content BLOB)")
            writer.execute("INSERT INTO turns VALUES (81,?)", (b"committed",))
            writer.commit()
            self.assertTrue(self.database.with_name(BUSINESS + "-wal").exists())
            original = sqlite_inventory(self.database)
            manifest = backup(self.source, self.bundle)
            self.assertEqual(manifest["sql"][BUSINESS], original)
        result = restore(self.bundle, self.target)
        self.assertEqual(result["sql"][BUSINESS], original)
        self.assertEqual(result["files"], verify(self.bundle)["files"])
        self.assertEqual((self.target / "knowledge" / "chunks.bin").read_bytes(), b"fixed vector fixture")

    def test_corruption_prevents_restore_without_creating_destination(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("CREATE TABLE task (id INTEGER PRIMARY KEY)")
        backup(self.source, self.bundle)
        (self.bundle / "knowledge" / "chunks.bin").write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "manifest mismatch"):
            restore(self.bundle, self.target)
        self.assertFalse(self.target.exists())

    def test_refuses_to_replace_an_existing_instance(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("CREATE TABLE task (id INTEGER PRIMARY KEY)")
        backup(self.source, self.bundle)
        self.target.mkdir()
        sentinel = self.target / BUSINESS
        sentinel.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            restore(self.bundle, self.target)
        self.assertEqual(sentinel.read_bytes(), b"existing")

    def test_rejects_foreign_key_damage(self):
        with closing(sqlite3.connect(self.database)) as writer, writer:
            writer.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            writer.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
            writer.execute("INSERT INTO child VALUES (100)")
        with self.assertRaisesRegex(ValueError, "1 FK violations"):
            backup(self.source, self.bundle)
        self.assertFalse(self.bundle.exists())


if __name__ == "__main__":
    unittest.main()
