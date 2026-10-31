import json
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import server as app
import test_workspace as fixtures
from tools_server import initialize_tools,ToolHandler


class ToolWorkspaceTests(unittest.TestCase):
    setUp=fixtures.WorkspaceTests.setUp
    tearDown=fixtures.WorkspaceTests.tearDown

    def migrated(self):
        with app.connect_db() as db:
            db.execute('CREATE TABLE ops_users(id TEXT,password TEXT)')
            db.execute("INSERT INTO ops_users VALUES('private-employee','employee-secret-hash')")
            db.execute('INSERT INTO accounts VALUES(?,?)',('abc123456789',json.dumps({'id':'abc123456789','name':'原采集账号','cookie':'sessionid=collector-only'})))
            db.execute('INSERT INTO tasks VALUES(?,?)',('abc123456789',json.dumps({'id':'abc123456789','status':'running'})))
        app.CONFIG.write_text(json.dumps({'cookie':'collector-only-config'}))
        return initialize_tools(app.DB,app.CONFIG,app.DATA/'tools')

    def test_tools_import_only_collection_tables_and_are_independent_afterwards(self):
        target=self.migrated()
        with closing(sqlite3.connect(target)) as db, db:
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('ops_users',names)
            self.assertNotIn('ops_account_logins',names)
            self.assertNotIn('logs',names)
            self.assertEqual(json.loads(db.execute('SELECT payload FROM tasks').fetchone()[0])['status'],'failed')
            db.execute("DELETE FROM tasks")
        initialize_tools(app.DB,app.CONFIG,app.DATA/'tools')
        with closing(sqlite3.connect(target)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)
        with app.connect_db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ops_users').fetchone()[0],1)

    def test_no_login_tool_api_cannot_access_operations_or_change_its_login(self):
        target=self.migrated()
        with patch.object(app,'DB',target),patch.object(app,'CONFIG',target.parent/'settings.json'):
            server=app.ThreadingHTTPServer(('127.0.0.1',0),ToolHandler)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base+'/api/health') as response:
                    self.assertEqual(json.load(response)['app'],'DouK Tools')
                with urlopen(base+'/api/accounts') as response:
                    result=json.load(response)
                    self.assertEqual(result[0]['name'],'原采集账号')
                    self.assertNotIn('collector-only',json.dumps(result))
                for path in ('/api/auth/status','/api/ops/people','/api/ops/works','/api/ops/logs','/api/ops/connection'):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(base+path)
                    self.assertEqual(error.exception.code,404)
                with self.assertRaises(HTTPError):
                    urlopen(Request(base+'/api/auth/logout',data=b'{}',headers={'Content-Type':'application/json'}))
                with urlopen(base+'/') as response:
                    html=response.read().decode()
                    self.assertIn('tools-shell.js',html)
                    self.assertNotIn('ops-ui.js',html)
            finally:
                server.shutdown();server.server_close();worker.join()
