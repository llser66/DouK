import csv
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as app


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {'Content-Length': str(len(body))}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [patch.object(app, k, v) for k, v in {
            'DATA': root, 'DOWNLOADS': root / 'Downloads', 'DB': root / 'workspace.sqlite',
            'CONFIG': root / 'settings.json', 'CANCEL': {}, 'PROCESSES': {}, 'LOGIN_SESSIONS': {},
        }.items()]
        for p in self.patches:
            p.start()
        app.DOWNLOADS.mkdir()
        with app.connect_db() as db:
            db.executescript('CREATE TABLE tasks(id TEXT PRIMARY KEY,payload TEXT); CREATE TABLE records(id INTEGER PRIMARY KEY,task_id TEXT,payload TEXT); CREATE TABLE files(key TEXT PRIMARY KEY,path TEXT); CREATE TABLE accounts(id TEXT PRIMARY KEY,payload TEXT);')

    def tearDown(self):
        # A terminal status can be observed before the worker closes its final log connection.
        with app.JOB_LOCK:
            pass
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def task(self):
        t = {'id': 'ab1234567890', 'platform': 'douyin', 'action': 'download', 'created': '', 'status': 'running', 'title': 'test', 'request': {'types': ['video']}, 'files': [], 'record_count': 0}
        app.CANCEL[t['id']] = threading.Event()
        app.save_task(t)
        return t

    def wait_task(self, t):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            result = app.get_task(t['id'])
            if result['status'] not in {'queued', 'running', 'recording'}:
                return result
            time.sleep(.01)
        self.fail('Task did not complete')

    def test_secrets_never_in_public_settings(self):
        app.save_settings({'cookie': 'private-cookie', 'token': 'private-token'})
        public = app.public_settings()
        self.assertTrue(public['has_cookie'])
        self.assertNotIn('cookie', public)
        self.assertNotIn('private-token', json.dumps(public))
        app.save_settings({'cookie': ''})
        self.assertEqual(app.settings()['cookie'], 'private-cookie')
        app.save_settings({'clear_secrets': True})
        self.assertFalse(app.public_settings()['has_cookie'])

    def test_invalid_api_and_dates_rejected(self):
        for url in ('https://example.com', 'http://127.0.0.1:5555/evil', 'http://user@localhost:5555'):
            with self.assertRaises(ValueError):
                app.save_settings({'api_url': url})
        with self.assertRaises(ValueError):
            app.save_settings({'earliest': '2026-10-28', 'latest': '2026-01-01'})

    def test_identifiers_and_platform_validation(self):
        self.assertEqual(app.identifier('https://www.douyin.com/video/123456789', 'detail', 'douyin', {}), '123456789')
        self.assertEqual(app.identifier('https://www.douyin.com/user/abc-sec', 'account', 'douyin', {}), 'abc-sec')
        self.assertEqual(app.identifier('https://www.douyin.com/?mix_id=7654321', 'mix', 'douyin', {}), '7654321')
        with self.assertRaises(ValueError):
            app.identifier('https://example.com/video/123', 'detail', 'douyin', {})
        with patch.object(app, 'upstream', return_value={'url': 'https://www.douyin.com/video/123456789'}):
            self.assertEqual(app.identifier('https://v.douyin.com/abc/', 'detail', 'douyin', {}), '123456789')

    def test_upstream_payload_mapping(self):
        req = {'mode': 'comment', 'platform': 'douyin', 'pages': 3}
        with patch.object(app, 'upstream', return_value={'data': [{'text': 'comment'}]}) as upstream:
            result = app.get_data('12345', req, {**app.DEFAULTS, 'cookie': 'example'})
        self.assertEqual(result[0]['text'], 'comment')
        self.assertEqual(upstream.call_args.args[0], '/douyin/comment')
        self.assertEqual(upstream.call_args.args[1]['detail_id'], '12345')
        self.assertEqual(upstream.call_args.args[1]['pages'], 3)

    def test_asset_selection(self):
        row = {'type': '图集', 'downloads': ['https://cdn.example/a', 'https://cdn.example/b'], 'static_cover': 'https://cdn.example/c'}
        self.assertEqual(len(app.assets(row, ['image', 'static_cover'])), 3)
        self.assertEqual(app.assets(row, ['video']), [])
        live = {'type': '实况', 'downloads': ['https://cdn.example/motion']}
        self.assertEqual(app.assets(live, ['livephoto'])[0][2], '.mp4')

    def test_transfer_resumes_only_when_range_matches(self):
        t = self.task()
        dest = app.DOWNLOADS / 'video.mp4'
        dest.with_suffix('.mp4.part').write_bytes(b'abc')
        with patch.object(app, 'media_request', return_value=Response(b'def', 206, {'Content-Length': '3', 'Content-Range': 'bytes 3-5/6'})) as request:
            app.transfer('https://cdn.example/file', dest, t, app.DEFAULTS)
        self.assertEqual(dest.read_bytes(), b'abcdef')
        self.assertEqual(request.call_args.args[1]['Range'], 'bytes=3-')
        self.assertNotIn('Cookie', request.call_args.args[1])

    def test_range_ignored_restarts_safely_and_short_response_fails(self):
        t = self.task()
        dest = app.DOWNLOADS / 'video.mp4'
        dest.with_suffix('.mp4.part').write_bytes(b'old')
        with patch.object(app, 'media_request', return_value=Response(b'new')):
            app.transfer('https://cdn.example/file', dest, t, app.DEFAULTS)
        self.assertEqual(dest.read_bytes(), b'new')
        dest.unlink()
        with patch.object(app, 'media_request', return_value=Response(b'a', headers={'Content-Length': '10'})):
            with self.assertRaises(ValueError):
                app.transfer('https://cdn.example/file', dest, t, app.DEFAULTS)
        self.assertFalse(dest.exists())
        self.assertTrue(dest.with_suffix('.mp4.part').exists())

    def test_cancel_preserves_partial_file(self):
        t = self.task()
        app.CANCEL[t['id']].set()
        dest = app.DOWNLOADS / 'cancel.mp4'
        with patch.object(app, 'media_request', return_value=Response(b'content')):
            with self.assertRaises(InterruptedError):
                app.transfer('https://cdn.example/file', dest, t, app.DEFAULTS)
        self.assertFalse(dest.exists())

    def test_download_and_duplicate_skip(self):
        t = self.task()
        row = {'id': '123', 'type': '视频', 'downloads': 'https://cdn.example/file'}
        def transfer(url, path, *_):
            path.write_bytes(b'valid-file')
        with patch.object(app, 'transfer', side_effect=transfer) as mocked:
            app.download_rows([row], t, app.DEFAULTS)
            app.download_rows([row], t, app.DEFAULTS)
            self.assertEqual(mocked.call_count, 1)
        self.assertEqual(len(list(app.DOWNLOADS.iterdir())), 1)

    def test_task_records_real_success_and_failure(self):
        payload = {'action': 'collect', 'mode': 'detail', 'platform': 'douyin', 'links': '12345'}
        with patch.object(app, 'get_data', return_value=[{'id': '12345', 'desc': '中文作品'}]):
            t = self.wait_task(app.create_task(payload.copy()))
        self.assertEqual(t['status'], 'completed')
        self.assertEqual(t['record_count'], 1)
        self.assertEqual(app.records(t['id'])[0]['desc'], '中文作品')
        with patch.object(app, 'get_data', side_effect=ValueError('Cookie 失效')):
            failed = self.wait_task(app.create_task(payload.copy()))
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('Cookie 失效', failed['message'])

    def test_export_formats_and_formula_escaping(self):
        t = self.task()
        app.persist_records(t, [{'作品': '中文<&>', 'desc': '=SUM(1,2)', 'nested': {'x': 1}}])
        data, _ = app.export_data('csv', 'all')
        rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))
        self.assertEqual(rows[1][0], '中文<&>')
        self.assertEqual(rows[1][1], "'=SUM(1,2)")
        data, _ = app.export_data('xlsx', 'all')
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for file in z.namelist():
                if file.endswith('.xml') or file.endswith('.rels'):
                    ET.fromstring(z.read(file))
            self.assertIn('中文&lt;&amp;&gt;', z.read('xl/worksheets/sheet1.xml').decode())
        data, _ = app.export_data('sqlite', 'all')
        db = sqlite3.connect(':memory:')
        db.deserialize(data)
        self.assertEqual(db.execute('SELECT field_0 FROM records').fetchone()[0], '中文<&>')
        self.assertEqual(db.execute('SELECT original_name FROM field_names ORDER BY rowid').fetchone()[0], '作品')
        db.close()

    def test_http_static_and_origin_file_protection(self):
        # These legacy collection endpoints belong to the independent tools service.
        from tools_server import ToolHandler
        srv = app.ThreadingHTTPServer(('127.0.0.1', 0), ToolHandler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{srv.server_port}'
        try:
            with urlopen(base) as r:
                self.assertEqual(r.status, 200)
                self.assertIn(b'DouK', r.read())
            with self.assertRaises(HTTPError) as e:
                urlopen(Request(base+'/api/settings', headers={'Origin': 'https://untrusted.example'}))
            self.assertEqual(e.exception.code, 403)
            for path in ('/data/settings.json', '/api/file?path=../server.py'):
                with self.assertRaises(HTTPError) as e:
                    urlopen(base+path)
                self.assertEqual(e.exception.code, 404)
            with urlopen(base+'/api/tasks') as r:
                self.assertEqual(json.load(r), [])
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
