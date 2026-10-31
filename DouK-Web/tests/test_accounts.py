import base64
import hashlib
import io
import json
import socket
import struct
import threading
import unittest
from unittest.mock import patch

import test_workspace as workspace
import server as app
from douyin_login import cookie_header, cdp_request, DouyinLogin, CDPCommandError


class AccountTests(unittest.TestCase):
    setUp = workspace.WorkspaceTests.setUp
    tearDown = workspace.WorkspaceTests.tearDown
    task = workspace.WorkspaceTests.task
    wait_task = workspace.WorkspaceTests.wait_task
    def ready_account(self, name='主账号', cookie='sessionid=example-private'):
        created = app.create_account({'name': name})
        app.update_account(created['id'], {'cookie': cookie})
        return app.get_account(created['id'])

    def test_account_vault_never_exposes_credentials(self):
        account = self.ready_account()
        public = app.public_account(account)
        self.assertTrue(public['has_cookie'])
        self.assertNotIn('cookie', public)
        self.assertNotIn('example-private', json.dumps(public))
        self.assertTrue(public['is_default'])
        second = self.ready_account('另一个账号', 'sessionid=other-private')
        self.assertNotEqual(account['id'], second['id'])
        self.assertEqual(len(app.all_accounts()), 2)
        app.update_account(second['id'], {'is_default': True})
        self.assertTrue(app.public_account(app.get_account(second['id']))['is_default'])

    def test_explicit_account_is_used_without_cross_account_leaks(self):
        first = self.ready_account()
        second = self.ready_account('二号', 'sessionid=second-private')
        payload = {'action': 'collect', 'mode': 'comment', 'platform': 'douyin', 'links': '123456', 'account_id': second['id']}
        with patch.object(app, 'get_data', return_value=[{'text': 'comment'}]) as read:
            task = self.wait_task(app.create_task(payload))
        self.assertEqual(task['account_name'], '二号')
        self.assertEqual(read.call_args.args[2]['cookie'], 'sessionid=second-private')
        self.assertNotIn('second-private', json.dumps(app.public_task(task), ensure_ascii=False))
        self.assertEqual(app.get_account(first['id'])['last_request'], 0)

    def test_pending_or_paused_account_cannot_create_task(self):
        pending = app.create_account({'name': '未登录'})
        payload = {'action': 'collect', 'mode': 'detail', 'platform': 'douyin', 'links': '123456', 'account_id': pending['id']}
        with self.assertRaisesRegex(ValueError, '尚未登录'):
            app.create_task(payload)
        account = self.ready_account('已暂停')
        app.update_account(account['id'], {'paused': True})
        payload['account_id'] = account['id']
        with self.assertRaisesRegex(ValueError, '已暂停'):
            app.create_task(payload)

    def test_three_failures_pause_with_backoff(self):
        account = self.ready_account()
        for _ in range(3):
            app.account_request_result(account['id'], ValueError('平台拒绝请求'))
        latest = app.get_account(account['id'])
        self.assertTrue(latest['paused'])
        self.assertEqual(latest['failure_count'], 3)
        self.assertGreater(latest['cooldown_until'], 0)
        app.update_account(account['id'], {'paused': False})
        self.assertFalse(app.get_account(account['id'])['paused'])
        self.assertEqual(app.get_account(account['id'])['failure_count'], 0)

    def test_upstream_connection_error_does_not_blame_account(self):
        account = self.ready_account()
        app.account_request_result(account['id'], ValueError('无法连接本机 DouK API'))
        self.assertEqual(app.get_account(account['id'])['failure_count'], 0)

    def test_waiting_for_account_interval_can_be_cancelled(self):
        account = self.ready_account()
        app.save_account(account, last_request=app.time.time())
        task = self.task()
        task['request']['account_id'] = account['id']
        app.CANCEL[task['id']].set()
        with self.assertRaises(InterruptedError):
            app.prepare_account(task, app.DEFAULTS)

    def test_cookie_filter_requires_authenticated_douyin_session(self):
        guest = [{'name': 'ttwid', 'value': 'guest', 'domain': '.douyin.com', 'path': '/'}]
        self.assertIsNone(cookie_header(guest))
        cookies = guest + [
            {'name': 'sessionid', 'value': 'own-session', 'domain': '.douyin.com', 'path': '/', 'httpOnly': True},
            {'name': 'foreign', 'value': 'secret', 'domain': '.example.com', 'path': '/'},
            {'name': 'sessionid_ss', 'value': 'old', 'domain': '.douyin.com', 'path': '/', 'expires': 10},
            {'name': 'bad', 'value': 'x\r\nheader', 'domain': '.douyin.com', 'path': '/'},
        ]
        header = cookie_header(cookies, now=20)
        self.assertIn('sessionid=own-session', header)
        self.assertNotIn('foreign', header)
        self.assertNotIn('sessionid_ss', header)
        self.assertNotIn('header', header)

    def test_login_worker_saves_only_after_authenticated_cookie(self):
        account = self.ready_account()
        session = DouyinLogin(app.DATA/'test-profile', lambda c: app.update_account(account['id'], {'cookie': c}), lambda: True)
        event = threading.Event()
        with patch.object(session, '_read_endpoint', return_value=True), patch.object(session, '_cookies', return_value='sessionid=new-private'), patch.object(session, '_close'):
            session._run('chrome', event)
        self.assertEqual(session.status()['state'], 'saved')
        self.assertEqual(app.get_account(account['id'])['cookie'], 'sessionid=new-private')
        self.assertNotIn('new-private', json.dumps(session.status()))

    def test_cookie_read_uses_dedicated_browser_context_when_page_is_replaced(self):
        session=DouyinLogin(app.DATA/'redirect-test',lambda *_:None,lambda:False)
        session.endpoint=(1234,'/devtools/browser/own-profile')
        cookies=[{'name':'sessionid','value':'own-private','domain':'.douyin.com','path':'/'},
                 {'name':'sessionid','value':'foreign-private','domain':'.example.com','path':'/'}]
        with patch.object(session,'_http',side_effect=ConnectionError('page detached')) as page_read,patch('douyin_login.cdp_request',return_value={'cookies':cookies}) as command:
            self.assertEqual(session._cookies(),'sessionid=own-private')
        command.assert_called_once_with('ws://127.0.0.1:1234/devtools/browser/own-profile','Storage.getCookies')
        page_read.assert_not_called()

    def test_cancelled_login_preserves_previous_cookie(self):
        account = self.ready_account()
        session = DouyinLogin(app.DATA/'test-profile', lambda c: app.update_account(account['id'], {'cookie': c}), lambda: True)
        event = threading.Event()
        event.set()
        with patch.object(session, '_read_endpoint', return_value=True), patch.object(session, '_close'):
            session._run('chrome', event)
        self.assertEqual(session.status()['state'], 'cancelled')
        self.assertEqual(app.get_account(account['id'])['cookie'], 'sessionid=example-private')

    def test_unsupported_browser_command_falls_back_only_to_own_official_page(self):
        session=DouyinLogin(app.DATA/'compatibility-test',lambda *_:None,lambda:False)
        session.endpoint=(1234,'/devtools/browser/own-profile')
        targets=[{'type':'page','url':'https://example.com/','webSocketDebuggerUrl':'ws://127.0.0.1:1234/devtools/page/foreign'},
                 {'type':'page','url':'https://www.douyin.com/','webSocketDebuggerUrl':'ws://127.0.0.1:1234/devtools/page/own'}]
        cookies=[{'name':'sessionid','value':'own-private','domain':'.douyin.com','path':'/'}]
        with patch.object(session,'_http',return_value=targets),patch('douyin_login.cdp_request',side_effect=[CDPCommandError(-32601),{'cookies':cookies}]) as command:
            self.assertEqual(session._cookies(),'sessionid=own-private')
        self.assertEqual(command.call_args.args,('ws://127.0.0.1:1234/devtools/page/own','Network.getCookies',{'urls':['https://www.douyin.com/']}))
        with patch.object(session,'_http') as pages,patch('douyin_login.cdp_request',side_effect=CDPCommandError(-32000)):
            with self.assertRaises(CDPCommandError):
                session._cookies()
        pages.assert_not_called()

    def test_protocol_diagnostic_keeps_only_numeric_code_and_no_payload(self):
        error=CDPCommandError(-32601)
        self.assertEqual(error.code,-32601)
        self.assertNotIn('sessionid',str(error))
        self.assertIsNone(CDPCommandError('private-payload').code)

    def test_login_redirect_cookie_read_recovers_without_closing_live_window(self):
        saved=[]
        session=DouyinLogin(app.DATA/'redirect-test',lambda cookie:saved.append(cookie),lambda:False)
        event=threading.Event()
        with patch.object(session,'_read_endpoint',return_value=True),patch.object(session,'_cookies',side_effect=[ConnectionError('page changed'),TimeoutError('navigation'), 'sessionid=own-private']),patch.object(event,'wait',return_value=False),patch.object(session,'_close') as close:
            session._run('chrome',event)
        self.assertEqual(saved,['sessionid=own-private'])
        self.assertEqual(session.state,'saved')
        close.assert_called_once()

    def test_continuously_unavailable_window_stops_after_bounded_retries(self):
        saved=[]
        session=DouyinLogin(app.DATA/'closed-test',lambda cookie:saved.append(cookie),lambda:False)
        event=threading.Event()
        with patch.object(session,'_read_endpoint',return_value=True),patch.object(session,'_cookies',side_effect=ConnectionError('closed')) as read,patch.object(event,'wait',return_value=False),patch.object(session,'_close'):
            session._run('chrome',event)
        self.assertEqual(read.call_count,5)
        self.assertEqual(session.state,'failed')
        self.assertEqual(saved,[])

    def test_browser_exiting_during_startup_fails_without_waiting_for_deadline(self):
        session=DouyinLogin(app.DATA/'startup-exit',lambda *_:None,lambda:False)
        session.profile.mkdir()
        event=threading.Event()
        with patch.object(session,'_read_endpoint',return_value=False),patch('douyin_login.subprocess.Popen') as process,patch.object(event,'wait',return_value=False) as wait,patch.object(session,'_close'):
            process.return_value.poll.return_value=1
            session._run('chrome',event)
        self.assertEqual(session.state,'failed')
        self.assertEqual(wait.call_count,1)

    def test_cdp_websocket_handshake_masking_and_fragmented_response(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        received = []
        def endpoint():
            conn, _ = listener.accept()
            with conn, conn.makefile('rb') as stream:
                stream.readline()
                headers = {}
                while True:
                    line = stream.readline()
                    if line == b'\r\n':
                        break
                    key, value = line.decode().split(':', 1)
                    headers[key.lower()] = value.strip()
                accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key']+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
                conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
                a, b = stream.read(2)
                size = b & 127
                if size == 126:
                    size = struct.unpack('!H', stream.read(2))[0]
                mask = stream.read(4)
                payload = stream.read(size)
                received.append(json.loads(bytes(v ^ mask[i%4] for i,v in enumerate(payload))))
                response = json.dumps({'id': 1, 'result': {'cookies': []}}).encode()
                half = len(response)//2
                conn.sendall(bytes([1, half])+response[:half]+bytes([0x80, len(response)-half])+response[half:])
        thread = threading.Thread(target=endpoint, daemon=True)
        thread.start()
        try:
            result = cdp_request(f'ws://127.0.0.1:{port}/devtools/page/test', 'Network.getCookies', {'urls': ['https://www.douyin.com/']})
            self.assertEqual(result, {'cookies': []})
            self.assertEqual(received[0]['method'], 'Network.getCookies')
        finally:
            listener.close()
            thread.join(timeout=2)

    def test_cdp_rejects_remote_debugging_destinations(self):
        for url in ('ws://example.com/devtools/page/x', 'wss://127.0.0.1/devtools/page/x', 'ws://127.0.0.1:1234/other'):
            with self.assertRaises(ValueError):
                cdp_request(url, 'Network.getCookies')
