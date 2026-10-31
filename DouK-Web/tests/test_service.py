import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from api_service import APIService
import server as app
import test_workspace as workspace


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = APIService(root / 'DouK-Web')
        self.service.root.mkdir()
        source = self.service.upstream / 'src/custom'
        source.mkdir(parents=True)
        (source / 'internal.py').write_text('DISCLAIMER_TEXT = ("原项目声明\\n" "请本人确认")', 'utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_start_is_noninteractive_and_does_not_record_notice_acceptance(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(self.service, 'ready', return_value=False), patch.object(self.service, 'dependencies_ready', return_value=True), patch('api_service.subprocess.Popen', return_value=process) as launch:
            result = self.service.start()
            self.assertEqual(result['state'], 'starting')
            self.assertIn('--web-start', launch.call_args.args[0])
            self.assertNotIn('--accept-disclaimer', launch.call_args.args[0])
        self.assertFalse((self.service.upstream/'Volume/DouK-Downloader.db').exists())

    def test_missing_dependencies_do_not_start_a_broken_process(self):
        with patch.object(self.service, 'ready', return_value=False), patch.object(self.service, 'dependencies_ready', return_value=False), patch('api_service.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):
                self.service.start()
            launch.assert_not_called()

    def test_ensure_ready_waits_for_automatic_startup(self):
        with patch.object(self.service, 'ready', side_effect=[False,False,True]), patch.object(self.service, 'start') as start, patch('api_service.time.sleep'):
            self.service.ensure_ready()
            start.assert_called_once()

    def test_running_service_is_reused_without_starting_another_process(self):
        with patch.object(self.service, 'ready', return_value=True), patch.object(self.service, 'dependencies_ready', return_value=True), patch('api_service.subprocess.Popen') as launch:
            self.assertTrue(self.service.start()['ready'])
            launch.assert_not_called()


class ServiceHTTPTests(unittest.TestCase):
    setUp=workspace.WorkspaceTests.setUp
    tearDown=workspace.WorkspaceTests.tearDown

    def test_disconnected_service_rejects_a_task_without_adding_failed_history(self):
        from tools_server import ToolHandler
        srv=app.ThreadingHTTPServer(('127.0.0.1',0),ToolHandler)
        worker=threading.Thread(target=srv.serve_forever,daemon=True)
        worker.start()
        try:
            request=Request(f'http://127.0.0.1:{srv.server_port}/api/tasks',data=json.dumps({'action':'collect','mode':'comment','platform':'douyin','links':'12345'}).encode(),headers={'Content-Type':'application/json'})
            with patch.object(app,'ensure_connection',return_value={'connected':False}):
                with self.assertRaises(HTTPError) as e:
                    urlopen(request)
            self.assertEqual(e.exception.code,503)
            self.assertEqual(app.all_tasks(),[])
        finally:
            srv.shutdown();srv.server_close();worker.join()

    def test_comment_reply_options_are_forwarded(self):
        req={'mode':'comment','platform':'douyin','pages':1,'reply':True,'count_reply':7}
        with patch.object(app,'upstream',return_value={'data':[{'text':'example'}]}) as upstream:
            app.get_data('12345',req,app.DEFAULTS)
        self.assertTrue(upstream.call_args.args[1]['reply'])
        self.assertEqual(upstream.call_args.args[1]['count_reply'],7)
