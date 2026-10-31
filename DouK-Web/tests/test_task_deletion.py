import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server as app
import test_workspace as workspace


class TaskDeletionTests(unittest.TestCase):
    setUp = workspace.WorkspaceTests.setUp
    tearDown = workspace.WorkspaceTests.tearDown
    task = workspace.WorkspaceTests.task

    def completed_task(self):
        task = self.task()
        app.save_task(task, status='completed', title='Task deletion fixture')
        return task

    def test_delete_removes_history_but_preserves_data_and_files(self):
        task = self.completed_task()
        app.persist_records(task, [{'cid': 'one', 'text': 'retained'}])
        app.save_task(task)
        media = app.DOWNLOADS/'retained.mp4'
        media.write_bytes(b'fixture-media')
        removed = app.delete_task(task['id'])
        self.assertTrue(removed['deleted_at'])
        self.assertEqual(app.all_tasks(), [])
        self.assertEqual(len(app.all_tasks(include_deleted=True)), 1)
        self.assertEqual(app.records(task['id'])[0]['text'], 'retained')
        self.assertEqual(media.read_bytes(), b'fixture-media')

    def test_restore_is_idempotent_and_retains_original_status(self):
        task = self.completed_task()
        first = app.delete_task(task['id'])
        second = app.delete_task(task['id'])
        self.assertEqual(first['deleted_at'], second['deleted_at'])
        restored = app.restore_task(task['id'])
        self.assertNotIn('deleted_at', restored)
        self.assertEqual(restored['status'], 'completed')
        app.restore_task(task['id'])
        self.assertEqual(len(app.all_tasks()), 1)

    def test_running_queued_and_recording_tasks_must_be_cancelled_first(self):
        task = self.task()
        for status in ('queued','running','recording'):
            app.save_task(task, status=status)
            with self.assertRaisesRegex(ValueError, '先取消'):
                app.delete_task(task['id'])
            self.assertNotIn('deleted_at', app.get_task(task['id']))

    def test_http_deleted_tab_and_restore(self):
        task = self.completed_task()
        from tools_server import ToolHandler
        server = app.ThreadingHTTPServer(('127.0.0.1',0),ToolHandler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def get(path):
            with urlopen(base+path) as response:
                return json.load(response)
        def post(path):
            req=Request(base+path,data=b'{}',headers={'Content-Type':'application/json'})
            with urlopen(req) as response:
                return json.load(response)
        try:
            post(f'/api/tasks/{task["id"]}/delete')
            self.assertEqual(get('/api/tasks'), [])
            self.assertEqual(get('/api/tasks?deleted=1')[0]['id'], task['id'])
            with patch.object(app,'ensure_connection') as connect:
                with self.assertRaises(HTTPError) as error:
                    post(f'/api/tasks/{task["id"]}/retry')
                self.assertEqual(error.exception.code, 400)
                connect.assert_not_called()
            post(f'/api/tasks/{task["id"]}/restore')
            self.assertEqual(get('/api/tasks?deleted=1'), [])
            self.assertEqual(get('/api/tasks')[0]['id'], task['id'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
