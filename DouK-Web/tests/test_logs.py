import csv
import io
import json
import threading
import unittest
from urllib.request import urlopen, Request

import server as app
import test_workspace as workspace


class LogTests(unittest.TestCase):
    setUp = workspace.WorkspaceTests.setUp
    tearDown = workspace.WorkspaceTests.tearDown
    task = workspace.WorkspaceTests.task

    def test_task_events_persist_and_unchanged_save_is_not_duplicated(self):
        task = self.task()
        app.save_task(task)
        app.save_task(task, status='failed', message='Connection failed')
        rows = app.LOGS.query({})['items']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['level'], 'ERROR')
        self.assertEqual(rows[0]['task_id'], task['id'])
        with app.connect_db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM logs').fetchone()[0], 2)

    def test_secrets_redacted_before_storage(self):
        app.save_settings({'cookie': 'sessionid=my-secret', 'token': 'private-token'})
        app.LOGS.write('ERROR','system','错误', 'sessionid=my-secret private-token token=unknown-secret')
        with app.connect_db() as db:
            text = db.execute('SELECT message FROM logs').fetchone()[0]
        for secret in ('my-secret','private-token','unknown-secret'):
            self.assertNotIn(secret, text)

    def test_filters_pagination_and_literal_sql_search(self):
        for i in range(55):
            app.LOGS.write('INFO','task','进度',f'row {i}',task_id='one')
        app.LOGS.write('ERROR','system','异常','unique')
        self.assertEqual(app.LOGS.query({})['total'],56)
        self.assertEqual(len(app.LOGS.query({'page':['2']})['items']),6)
        result=app.LOGS.query({'level':['ERROR'],'search':['unique']})
        self.assertEqual(result['total'],1)
        self.assertEqual(app.LOGS.query({'search':["' OR 1=1 --"]})['total'],0)
        self.assertEqual(app.LOGS.query({'start':['2099-01-01T00:00']})['total'],0)
        with self.assertRaises(ValueError):
            app.LOGS.query({'start':['invalid-date']})

    def test_clear_retains_records_and_tasks(self):
        task=self.task()
        app.persist_records(task,[{'text':'keep'}])
        app.LOGS.clear()
        self.assertEqual(app.LOGS.query({})['total'],1)
        self.assertEqual(app.LOGS.query({})['items'][0]['event'],'清空日志')
        self.assertEqual(len(app.records(task['id'])),1)
        self.assertEqual(len(app.all_tasks()),1)

    def test_http_filters_and_safe_csv(self):
        app.LOGS.write('WARN','system','CSV','=unsafe')
        from tools_server import ToolHandler
        server=app.ThreadingHTTPServer(('127.0.0.1',0),ToolHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base+'/api/logs?level=WARN') as response:
                self.assertEqual(json.load(response)['total'],1)
            with urlopen(base+'/api/logs/export') as response:
                rows=list(csv.reader(io.StringIO(response.read().decode('utf-8-sig'))))
                self.assertEqual(rows[1][-1],"'=unsafe")
            with urlopen(Request(base+'/api/logs/clear',data=b'{}',headers={'Content-Type':'application/json'})) as response:
                self.assertEqual(response.status,200)
        finally:
            server.shutdown();server.server_close();thread.join()
