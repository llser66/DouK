"""HTTP security boundaries for public reporting, using only disposable SQLite fixtures."""
import hashlib
import json
import threading
import unittest
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener
from unittest.mock import patch

import server as app
import test_operations as fixtures
from tools_server import ToolHandler


class ReportingHTTPTests(unittest.TestCase):
    def setUp(self):
        fixtures.OperationsTests.setUp(self)
        self.servers = []
        self.base = self.start_server(app.Handler)
        self.anonymous = build_opener()
        self.admin_browser = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        for server, thread in reversed(self.servers):
            server.shutdown()
            server.server_close()
            thread.join()
        fixtures.OperationsTests.tearDown(self)

    def start_server(self, handler):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return f'http://127.0.0.1:{server.server_port}'

    def request(self, path, data=None, headers=None, opener=None, base=None):
        body = json.dumps(data).encode() if data is not None else None
        request = Request((base or self.base) + path, data=body,
                          headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = (opener or self.anonymous).open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            result = json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else raw
            return response.code, result, dict(response.headers)

    def login_admin(self):
        status, body, _ = self.request('/api/auth/login', {'username': 'admin', 'password': 'Test-password-8394'},
                                       opener=self.admin_browser)
        self.assertEqual(status, 200)
        self.assertEqual(body['user']['role'], 'admin')

    def verified_token(self):
        token='d'*64
        with app.connect_db() as db:
            app.REPORT_ACCOUNTS.schema(db)
            db.execute('INSERT OR REPLACE INTO ops_report_verifications VALUES(?,?,?,?,?)',
                (hashlib.sha256(token.encode()).hexdigest(),'MS4w-http-verified','测试账号',1,4102444800))
        return token

    def test_submission_requires_valid_server_verification(self):
        data={'reporter':{'name':'尚未建档','department':'新部门','group':'新小组'},
              'links':'1234567890123','request_id':'required-proof-test-00001'}
        before=len(self.ops.public_directory()['employees'])
        for extra in ({}, {'login_tokens':['0'*64]}, {'login_identity':{'sec_uid':'forged'},'verified':True}):
            status,body,_=self.request('/api/reporting/submit',{**data,**extra})
            self.assertEqual(status,400)
            self.assertIn('核验',body['error'])
        token=self.verified_token()
        key=hashlib.sha256(token.encode()).hexdigest()
        self.ops.execute('UPDATE ops_report_verifications SET expires=1 WHERE token_hash=?',(key,))
        self.assertEqual(self.request('/api/reporting/submit',{**data,'login_tokens':[token]})[0],400)
        self.assertEqual(len(self.ops.public_directory()['employees']),before)
        self.assertEqual(self.ops.query('SELECT COUNT(*) n FROM ops_report_batches',one=True)['n'],0)
        self.verified_token()
        self.assertEqual(self.request('/api/reporting/submit',{**data,'login_tokens':['0'*64,token]})[0],202)

    def report(self, employee_id=None, request_id='http-request-00000001', link='1234567890123'):
        status, body, headers = self.request('/api/reporting/submit', {
            'employee_id': employee_id or self.employee_id, 'links': link, 'request_id': request_id,'login_tokens':[self.verified_token()]})
        self.assertEqual(status, 202)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('Set-Cookie', headers)
        return body

    def test_public_submission_receipts_are_scoped_and_not_available_in_urls(self):
        status, directory, _ = self.request('/api/reporting/directory')
        self.assertEqual(status, 200)
        self.assertEqual(len(directory['employees']), 2)
        self.assertTrue(all(set(e) <= {'id', 'name', 'employee_code', 'group_id', 'daily_target'} for e in directory['employees']))
        self.assertNotIn(self.admin['id'], json.dumps(directory))
        first = self.report()
        second = self.report(self.other_id, 'http-request-00000002', '1234567890124')
        self.assertNotEqual(first['receipt_token'], second['receipt_token'])
        for receipt in (first, second):
            status, result, _ = self.request('/api/reporting/receipt', {'receipt_token': receipt['receipt_token']})
            self.assertEqual(status, 200)
            self.assertEqual([r['id'] for r in result['items']], [r['id'] for r in receipt['items']])
            for key in ('employee_id', 'employee_name', 'payload', 'token_hash', 'cookie', 'metrics'):
                self.assertNotIn('"' + key + '"', json.dumps(result))
        for token in ('', '0' * 64, first['receipt_token'][:-1]):
            self.assertEqual(self.request('/api/reporting/receipt', {'receipt_token': token})[0], 404)
        self.assertEqual(self.request('/api/reporting/receipt?receipt_token=' + first['receipt_token'])[0], 404)
        self.assertEqual(self.ops.query('SELECT COUNT(*) n FROM ops_submissions', one=True)['n'], 0)

    def test_anonymous_cannot_read_credentials_backups_or_change_business_data(self):
        for path in ('/api/ops/people', '/api/ops/reporting', '/api/ops/connection', '/api/ops/account-logins',
                     '/api/ops/backups', '/api/ops/backups/download?name=anything.sqlite', '/api/ops/network',
                     '/api/settings', '/api/accounts', '/api/tasks', '/api/file?path=anything'):
            self.assertEqual(self.request(path)[0], 401, path)
        for path in ('/api/ops/backups', '/api/ops/reporting/review', '/api/ops/employees', '/api/ops/connection',
                     '/api/accounts', '/api/tasks'):
            self.assertEqual(self.request(path, {})[0], 401, path)
        # Even an old employee session from before migration has no management access.
        token = 'old-employee-session'
        self.ops.execute('INSERT INTO ops_sessions VALUES(?,?,?)',
                         (hashlib.sha256(token.encode()).hexdigest(), self.employee_id, 4102444800))
        self.assertEqual(self.request('/api/ops/people', headers={'Cookie': 'douk_session=' + token})[0], 401)


    def test_progress_list_export_and_excuse_require_admin_and_work_over_http(self):
        for path in ('/api/ops/report-progress','/api/ops/report-progress/export'):
            self.assertEqual(self.request(path)[0],401)
        self.assertEqual(self.request('/api/ops/report-progress/excuse',{})[0],401)
        self.login_admin()
        status,result,_=self.request('/api/ops/report-progress',opener=self.admin_browser)
        self.assertEqual(status,200)
        self.assertEqual(result['counts']['missing'],2)
        status,body,headers=self.request('/api/ops/report-progress/export',opener=self.admin_browser)
        self.assertEqual(status,200)
        self.assertTrue(body.startswith(b'PK'))
        self.assertIn('spreadsheet',headers['Content-Type'])
        status,_,_=self.request('/api/ops/report-progress/excuse',
            {'date':result['date'],'employee':self.employee_id,'enabled':True,'reason':'当日休息'},opener=self.admin_browser)
        self.assertEqual(status,200)
        self.assertEqual(self.request('/api/ops/report-progress',opener=self.admin_browser)[1]['counts']['missing'],1)
        self.assertEqual(self.request('/api/ops/jobs',{'kind':'inspect','target':self.binding_id},opener=self.admin_browser)[0],400)

    def test_daily_targets_require_admin_and_public_directory_omits_private_notes(self):
        path = '/api/ops/daily-targets'
        self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request(path, {})[0], 401)
        self.login_admin()
        day = self.request('/api/ops/report-progress', opener=self.admin_browser)[1]['date']
        status, _, _ = self.request(path, {'scope': 'default', 'quantity': 2,
            'start_day': day, 'weekdays': list(range(7)), 'cutoff': '23:59'}, opener=self.admin_browser)
        self.assertEqual(status, 200)
        status, _, _ = self.request(path, {'scope': 'employee', 'quantity': 3,
            'employees': [self.employee_id], 'start_day': day, 'note': 'private-note-for-manager'}, opener=self.admin_browser)
        self.assertEqual(status, 200)
        result = self.request('/api/ops/report-progress?status=all', opener=self.admin_browser)[1]
        self.assertTrue(result['quota_mode'])
        self.assertEqual(next(r for r in result['items'] if r['id'] == self.employee_id)['required'], 3)
        directory = self.request('/api/reporting/directory')[1]
        self.assertNotIn('private-note-for-manager', json.dumps(directory))
        for target in [directory['daily_target'], *(e['daily_target'] for e in directory['employees'])]:
            self.assertEqual(set(target), {'configured', 'quantity', 'cutoff', 'source'})
        self.assertEqual(self.request(path, {'scope': 'default', 'quantity': -1,
            'start_day': day}, opener=self.admin_browser)[0], 400)

    def test_form_builds_roster_and_notes_require_private_receipt_not_employee_name(self):
        data={'reporter':{'name':'HTTP测试员工','department':'HTTP测试部门','group':'HTTP测试组'},
              'links':'1234567890123','request_id':'http-new-roster-00001',
              'login_identity':{'sec_uid':self.binding['sec_uid']},'verified':True,'login_tokens':[self.verified_token()]}
        status,receipt,_=self.request('/api/reporting/submit',data)
        self.assertEqual(status,202)
        item=receipt['items'][0]
        # Caller-supplied identity/verified flags are ignored by the handler.
        claim=self.ops.query('SELECT login_identity FROM ops_report_claims',one=True)
        self.assertEqual(json.loads(claim['login_identity'])['sec_uid'],'MS4w-http-verified')
        self.assertTrue(any(e['name']=='HTTP测试员工' for e in self.ops.public_directory()['employees']))
        status,_,_=self.request('/api/reporting/note',{'item_id':item['id'],'note':'猜姓名无法修改'})
        self.assertEqual(status,404)
        status,_,_=self.request('/api/reporting/note',{'receipt_token':receipt['receipt_token'],'item_id':item['id'],'note':'测试代报原因'})
        self.assertEqual(status,200)
        self.assertEqual(self.request('/api/reporting/receipt',receipt)[1]['items'][0]['reporter_note'],'测试代报原因')
        self.assertEqual(self.request('/api/reporting/login/status',{'login_token':'0'*64})[0],404)
        # No GET endpoint that lists other reporters' sessions or receipts.
        self.assertEqual(self.request('/api/reporting/login/status')[0],404)

    def test_multi_account_proofs_are_server_resolved_and_raw_identities_are_ignored(self):
        tokens=['a'*64,'b'*64]
        with app.connect_db() as db:
            app.REPORT_ACCOUNTS.schema(db)
            for token,sec_uid in zip(tokens,['MS4w-http-account-A','MS4w-http-account-B']):
                db.execute('INSERT INTO ops_report_verifications VALUES(?,?,?,?,?)',
                    (hashlib.sha256(token.encode()).hexdigest(),sec_uid,'本人账号',1,4102444800))
        data={'employee_id':self.employee_id,'links':'1234567890123','request_id':'http-multi-account-000001',
              'login_tokens':tokens,'login_accounts':[{'sec_uid':'forged-profile'}]}
        status,receipt,_=self.request('/api/reporting/submit',data)
        self.assertEqual(status,202)
        snapshot=json.loads(self.ops.query('SELECT login_identity FROM ops_report_claims',one=True)['login_identity'])
        self.assertEqual({i['sec_uid'] for i in snapshot['accounts']},{'MS4w-http-account-A','MS4w-http-account-B'})
        self.assertNotIn('forged-profile',json.dumps(snapshot))
        self.assertNotIn(tokens[0],json.dumps(snapshot))
        self.assertNotIn('login_accounts',json.dumps(receipt))
        self.assertEqual(self.request('/api/reporting/submit',{**data,'login_tokens':[{}]})[0],400)
        self.assertEqual(self.request('/api/reporting/submit',{**data,'request_id':'http-multi-identity-00002','login_tokens':[]})[0],400)

    def test_employee_delete_and_restore_are_admin_only(self):
        for action in ('delete','restore'):
            self.assertEqual(self.request('/api/ops/employees/'+action,{'id':self.employee_id,'confirmed':True,'permanent':True})[0],401)
        self.login_admin()
        self.assertEqual(self.request('/api/ops/employees/delete',{'confirmed':True,'id':self.employee_id},opener=self.admin_browser)[0],400)
        self.assertEqual(self.request('/api/ops/employees/delete',{'confirmed':True,'permanent':True,'id':self.employee_id},opener=self.admin_browser)[0],200)
        self.assertFalse(any(u['id']==self.employee_id for u in self.request('/api/ops/people',opener=self.admin_browser)[1]['users']))
        self.assertEqual(self.request('/api/ops/employees/restore',{'id':self.employee_id},opener=self.admin_browser)[0],400)

    def test_ops_task_delete_and_restore_are_admin_only(self):
        job=self.ops.enqueue('inspect',self.binding_id,self.admin)['id']
        self.ops.execute("UPDATE ops_jobs SET status='partial' WHERE id=?",(job,))
        for resource in ('delete','restore'):
            self.assertEqual(self.request('/api/ops/jobs/'+resource,{'id':job})[0],401)
        self.login_admin()
        self.assertEqual(self.request('/api/ops/jobs/delete',{'confirmed':True,'id':job},opener=self.admin_browser)[0],200)
        self.assertEqual(self.request('/api/ops/jobs',opener=self.admin_browser)[1],[])
        self.assertEqual(self.request('/api/ops/jobs?deleted=1',opener=self.admin_browser)[1][0]['id'],job)
        self.assertEqual(self.request('/api/ops/jobs/restore',{'id':job},opener=self.admin_browser)[0],200)

    def test_delete_requires_confirmation_and_work_restore_is_admin_only(self):
        with app.connect_db() as db:
            self.ops.upsert_work(self.ops.normalize(fixtures.OperationsTests.raw(self)),self.binding,db)
        work_id=fixtures.OperationsTests.raw(self)['aweme_id']
        self.assertEqual(self.request('/api/ops/works/delete',{'id':work_id,'confirmed':True,'permanent':True})[0],401)
        self.login_admin()
        for resource in ('employees/delete','jobs/delete','works/delete','bindings/delete'):
            self.assertEqual(self.request('/api/ops/'+resource,{'id':work_id},opener=self.admin_browser)[0],400)
        self.assertEqual(self.request('/api/ops/works/delete',{'id':work_id,'confirmed':True,'permanent':True},opener=self.admin_browser)[0],200)
        self.assertEqual(self.request('/api/ops/works',opener=self.admin_browser)[1]['total'],0)
        self.assertEqual(self.request('/api/ops/works?deleted=1',opener=self.admin_browser)[1]['total'],0)
        self.assertEqual(self.request('/api/ops/works/restore',{'id':work_id})[0],401)
        self.assertEqual(self.request('/api/ops/works/restore',{'id':work_id},opener=self.admin_browser)[0],400)

    def test_binding_deletion_is_admin_only_permanent_and_readdable(self):
        for action in ('delete','restore'):
            self.assertEqual(self.request('/api/ops/bindings/'+action,{'id':self.binding_id,'confirmed':True,'permanent':True})[0],401)
        self.login_admin()
        self.assertEqual(self.request('/api/ops/bindings/delete',{'id':self.binding_id,'confirmed':True},opener=self.admin_browser)[0],400)
        self.assertEqual(self.request('/api/ops/bindings/delete',{'id':self.binding_id,'confirmed':True,'permanent':True},opener=self.admin_browser)[0],200)
        self.assertEqual(self.request('/api/ops/bindings',opener=self.admin_browser)[1],[])
        self.assertEqual(self.request('/api/ops/bindings?deleted=1',opener=self.admin_browser)[1],[])
        self.assertEqual(self.request('/api/ops/bindings/restore',{'id':self.binding_id},opener=self.admin_browser)[0],400)

    def test_backups_are_admin_only_and_download_paths_are_scoped(self):
        self.login_admin()
        self.assertEqual(app.backup_store().db_path, app.DB.resolve())
        status, result, _ = self.request('/api/ops/backups', {}, opener=self.admin_browser)
        self.assertEqual(status, 200)
        name = result['item']['name']
        self.assertTrue(app.backup_store().path(name).is_relative_to(app.DATA.resolve()))
        status, content, headers = self.request('/api/ops/backups/download?name=' + quote(name), opener=self.admin_browser)
        self.assertEqual(status, 200)
        self.assertTrue(content.startswith(b'SQLite format 3\x00'))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.request('/api/ops/backups/download?name=' + quote(name))[0], 401)
        self.assertEqual(self.request('/api/ops/backups/download?name=..%2Fworkspace.sqlite', opener=self.admin_browser)[0], 400)

    def test_tools_do_not_offer_public_reporting_or_business_resources(self):
        base = self.start_server(ToolHandler)
        for path in ('/api/reporting/directory', '/api/ops/backups', '/api/ops/people', '/api/auth/status'):
            self.assertEqual(self.request(path, base=base)[0], 404, path)
        for path in ('/api/reporting/submit', '/api/reporting/receipt', '/api/ops/reporting/review'):
            self.assertEqual(self.request(path, {}, base=base)[0], 404, path)
        self.assertEqual(self.request('/report', base=base)[0], 404)

    def test_unknown_hosts_wrong_ports_and_cross_origin_requests_are_rejected(self):
        port = self.servers[0][0].server_port
        with patch('lan_access.local_ipv4', return_value=['127.0.0.1', '192.168.9.17']):
            self.assertEqual(self.request('/api/reporting/directory', headers={
                'Host': f'192.168.9.17:{port}', 'Origin': f'http://192.168.9.17:{port}'})[0], 200)
            for headers in (
                {'Host': f'attacker.test:{port}'},
                {'Host': f'192.168.9.99:{port}'},
                {'Host': '192.168.9.17:1'},
                {'Origin': 'http://attacker.test'},
                {'Origin': 'null'},
                {'Sec-Fetch-Site': 'cross-site'},
            ):
                self.assertEqual(self.request('/api/reporting/directory', headers=headers)[0], 403, headers)
                self.assertEqual(self.request('/api/reporting/submit', {}, headers=headers)[0], 403, headers)

    def test_before_setup_legacy_apis_are_still_protected(self):
        with patch.object(self.ops, 'initialized', return_value=False):
            for path in ('/api/settings', '/api/accounts', '/api/tasks', '/api/records', '/api/logs',
                         '/api/ops/backups', '/api/ops/connection'):
                self.assertIn(self.request(path)[0], (401, 403), path)
            for path in ('/api/settings', '/api/accounts', '/api/tasks'):
                self.assertIn(self.request(path, {})[0], (401, 403), path)

    def test_external_document_links_open_entry_pages_without_opening_api_or_post_access(self):
        navigation = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
                      'Sec-Fetch-Dest': 'document'}
        for path in ('/', '/index.html', '/admin', '/admin/', '/report', '/report/'):
            with self.subTest(path=path):
                status, body, headers = self.request(path, headers=navigation)
                self.assertEqual(status, 200)
                self.assertIn('text/html', headers['Content-Type'])
                self.assertIn(b'<!doctype html>', body.lower())
        for path in ('/api/ops/people', '/api/ops/backups', '/api/reporting/directory', '/reports.js'):
            self.assertEqual(self.request(path, headers=navigation)[0], 403, path)
        self.assertEqual(self.request('/api/reporting/submit', {
            'employee_id': self.employee_id, 'links': '1234567890123', 'request_id': 'external-nav-000001'},
            headers=navigation)[0], 403)
        for changes in ({'Sec-Fetch-Dest': 'iframe'}, {'Sec-Fetch-Mode': 'cors'},
                        {'Origin': 'http://attacker.test'}, {'Origin': 'null'},
                        {'Host': 'attacker.test:8877'}):
            self.assertEqual(self.request('/admin', headers={**navigation, **changes})[0], 403, changes)
        self.assertEqual(self.ops.query('SELECT COUNT(*) n FROM ops_report_items', one=True)['n'], 0)


if __name__ == '__main__':
    unittest.main()
