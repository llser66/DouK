from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from workspace_backups import BackupStore


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / 'workspace.sqlite'
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)')
            db.execute('INSERT INTO records(value) VALUES(?)', ('committed-data',))
            db.commit()
        self.store = BackupStore(self.database, keep=2)

    def tearDown(self):
        self.temp.cleanup()

    def test_consistent_wal_snapshot_and_original_preserved(self):
        with closing(sqlite3.connect(self.database)) as active:
            active.execute('INSERT INTO records(value) VALUES(?)', ('latest-wal',))
            active.commit()
            item = self.store.create('manual')
        with closing(sqlite3.connect(self.store.path(item['name']))) as snapshot:
            self.assertEqual(snapshot.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(snapshot.execute('SELECT COUNT(*) FROM records').fetchone()[0], 2)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM records').fetchone()[0], 2)

    def test_retention_and_path_traversal_are_scoped_to_generated_backups(self):
        self.store.backup_dir.mkdir()
        unrelated = self.store.backup_dir / 'important.sqlite'
        unrelated.write_bytes(b'leave-this-file-alone')
        first = self.store.create('manual')
        self.store.create('manual')
        self.store.create('manual')
        self.assertEqual(len(self.store.list()), 2)
        self.assertFalse((self.store.backup_dir / first['name']).exists())
        self.assertTrue(unrelated.exists())
        for value in ('../workspace.sqlite', 'important.sqlite', str(self.database)):
            with self.assertRaises(ValueError):
                self.store.path(value)

    def test_daily_snapshot_is_idempotent_with_concurrent_requests(self):
        threads = [threading.Thread(target=self.store.due) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.store.list()), 1)
        self.assertIsNone(self.store.due())


if __name__ == '__main__':
    unittest.main()
