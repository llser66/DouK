"""Consistent SQLite snapshots with a bounded retention policy."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import secrets
import sqlite3
import threading

TZ = timezone(timedelta(hours=8))
NAME_PATTERN = re.compile(r'DouK-workspace-(\d{8})-\d{6}-\d{6}-[a-f0-9]{8}-[a-z0-9-]{1,32}\.sqlite')


class BackupStore:
    def __init__(self, db_path, backup_dir=None, keep=14):
        self.db_path = Path(db_path).resolve()
        self.backup_dir = Path(backup_dir).resolve() if backup_dir else self.db_path.parent / 'backups'
        self.keep = max(1, int(keep))
        self.lock = threading.RLock()
        self._last_created = None

    def list(self):
        with self.lock:
            if not self.backup_dir.exists():
                return []
            items = []
            for path in self.backup_dir.glob('DouK-workspace-*.sqlite'):
                if not NAME_PATTERN.fullmatch(path.name) or not path.is_file() or path.is_symlink():
                    continue
                if path.resolve().parent != self.backup_dir:
                    continue
                stat = path.stat()
                items.append({'name': path.name, 'size': stat.st_size,
                              'created_at': datetime.fromtimestamp(stat.st_mtime, TZ).isoformat(),
                              'reason': path.stem.split('-', 6)[-1]})
            return sorted(items, key=lambda item: item['name'], reverse=True)

    def path(self, filename):
        if not isinstance(filename, str) or not NAME_PATTERN.fullmatch(filename):
            raise ValueError('备份文件名无效')
        path = self.backup_dir / filename
        if path.resolve().parent != self.backup_dir or path.is_symlink() or not path.is_file():
            raise ValueError('备份文件不存在')
        return path

    def create(self, reason='manual'):
        with self.lock:
            if not self.db_path.is_file():
                raise ValueError('数据库尚未创建')
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            reason = re.sub(r'[^a-z0-9-]+', '-', str(reason).lower()).strip('-')[:32] or 'manual'
            created = datetime.now(TZ)
            if self._last_created and created <= self._last_created:
                created = self._last_created + timedelta(microseconds=1)
            self._last_created = created
            name = 'DouK-workspace-' + created.strftime('%Y%m%d-%H%M%S-%f') + '-' + secrets.token_hex(4) + '-' + reason + '.sqlite'
            destination = self.backup_dir / name
            temporary = destination.with_suffix('.partial')
            try:
                with closing(sqlite3.connect(self.db_path.as_uri() + '?mode=ro', uri=True, timeout=30)) as source:
                    with closing(sqlite3.connect(temporary, timeout=30)) as target:
                        source.backup(target, pages=256)
                        result = target.execute('PRAGMA quick_check').fetchone()[0]
                        if result != 'ok':
                            raise ValueError('SQLite 备份完整性校验失败')
                temporary.replace(destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
            for old in self.list()[self.keep:]:
                self.path(old['name']).unlink()
            return next(item for item in self.list() if item['name'] == name)

    def due(self):
        with self.lock:
            if not self.db_path.exists():
                return None
            day = datetime.now(TZ).strftime('%Y%m%d')
            if any(NAME_PATTERN.fullmatch(item['name']).group(1) == day for item in self.list()):
                return None
            return self.create('daily')


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Create or list a consistent DouK SQLite backup')
    parser.add_argument('--db', type=Path, default=Path(__file__).resolve().parent / 'data' / 'workspace.sqlite')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--reason', default='manual')
    args = parser.parse_args()
    store = BackupStore(args.db)
    print(json.dumps(store.list() if args.list else store.create(args.reason), ensure_ascii=False))
