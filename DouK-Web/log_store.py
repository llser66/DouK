"""Structured local logs; never record request bodies or credentials."""
from datetime import datetime
import re


class LogStore:
    def __init__(self, connect, secrets):
        self.connect = connect
        self.secrets = secrets

    def schema(self, db):
        db.execute('''CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            level TEXT NOT NULL, category TEXT NOT NULL, event TEXT NOT NULL,
            message TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS logs_time ON logs(created_at)')
        db.execute('CREATE INDEX IF NOT EXISTS logs_task ON logs(task_id)')

    def redact(self, text):
        text = str(text or '')
        for secret in sorted(filter(None, self.secrets()), key=len, reverse=True):
            text = text.replace(secret, '[已隐藏]')
        text = re.sub(r'(?im)\b(?:cookie|authorization)\s*:\s*[^\r\n]+', '[登录凭据已隐藏]', text)
        text = re.sub(r'(?i)\bbearer\s+[^\s]+', 'Bearer [已隐藏]', text)
        text = re.sub(r'(?i)\b(cookie|authorization|token|access_token|sessionid|sessionid_ss|sid_tt|password)\s*[:=]\s*[^\s,;]+', r'\1=[已隐藏]', text)
        return text[:4000]

    def write(self, level, category, event, message='', task_id='', title='', status='', db=None):
        values = [self.redact(v) for v in (event, message, task_id, title, status)]
        def insert(db):
            self.schema(db)
            db.execute('INSERT INTO logs(created_at,level,category,event,message,task_id,title,status) VALUES(?,?,?,?,?,?,?,?)',
                       (datetime.now().astimezone().isoformat(timespec='seconds'), level, category, *values))
        if db is not None:
            insert(db)
        else:
            with self.connect() as connection:
                insert(connection)

    def query(self, query, export=False):
        def arg(key):
            return str(query.get(key, [''])[0]).strip()
        page = max(1, int(arg('page') or 1))
        limit = 10000 if export else 50
        clauses, values = [], []
        for key in ('level', 'category', 'task_id'):
            if arg(key):
                clauses.append(key+'=?')
                values.append(arg(key))
        for key, op in (('start', '>='), ('end', '<=')):
            if arg(key):
                value = datetime.fromisoformat(arg(key))
                if value.tzinfo is None:
                    value = value.astimezone()
                if key == 'end' and len(arg(key)) == 16:
                    value = value.replace(second=59)
                clauses.append('created_at'+op+'?')
                values.append(value.isoformat(timespec='seconds'))
        if arg('search'):
            clauses.append("(instr(message,?)>0 OR instr(title,?)>0 OR instr(event,?)>0 OR instr(task_id,?)>0)")
            values.extend([arg('search')[:500]]*4)
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.connect() as db:
            self.schema(db)
            total = db.execute('SELECT COUNT(*) FROM logs'+where, values).fetchone()[0]
            page = min(page, max(1, (total+limit-1)//limit))
            rows = db.execute('SELECT * FROM logs'+where+' ORDER BY id DESC LIMIT ? OFFSET ?',
                              [*values, limit, 0 if export else (page-1)*limit]).fetchall()
        return {'items': [dict(r) for r in rows], 'total': total, 'page': page, 'page_size': limit}

    def clear(self):
        with self.connect() as db:
            self.schema(db)
            db.execute('DELETE FROM logs')
        self.write('INFO', 'system', '清空日志', '历史日志已清空；任务、采集记录和文件保留')
