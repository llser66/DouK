import io
import json
import threading
import unittest
import zipfile
from http.cookiejar import CookieJar
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor

import server as app
import test_workspace as workspace
from account_coverage import Coverage
from operations import AccessError, TZ, now, password_hash
from datetime import datetime


class OperationsTests(unittest.TestCase):
    def setUp(self):
        workspace.WorkspaceTests.setUp(self)
        app.OPS.schema()
        self.ops=app.OPS
        self.ops.login({'username':'admin','password':'Test-password-8394'},setup=True)
        self.admin=self.ops.query("SELECT * FROM ops_users WHERE username='admin'",one=True)
        self.employee_id=self.ops.save_user({'username':'employee','name':'员工甲','password':'Employee-password-8394'},self.admin)['id']
        self.employee=self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.employee_id,),one=True)
        self.other_id=self.ops.save_user({'username':'other','name':'员工乙','password':'Employee-password-8334'},self.admin)['id']
        self.binding_id=self.ops.save_binding({'homepage':'https://www.douyin.com/user/MS4wLjABAAAA-test-account','nickname':'运营账号A','owner_id':self.employee_id,'monitor_start':now()-86400*3},self.admin)['id']
        self.binding=self.ops.query('SELECT * FROM ops_bindings WHERE id=?',(self.binding_id,),one=True)

    tearDown=workspace.WorkspaceTests.tearDown

    def raw(self,work_id='1234567890123', **updates):
        return {'aweme_id':work_id,'author':{'sec_uid':self.binding['sec_uid'],'uid':'9876','nickname':'平台昵称'},
                'create_time':now()-86400*2,'desc':'作品文案','statistics':{'digg_count':123,'play_count':None,'comment_count':0},**updates}

    def fake_inspect(self,rows,complete):
        with patch.object(app,'upstream',return_value={'data':rows,'params':{'workspace_check':{'complete':complete}}}) as request:
            result=self.ops.inspect(self.binding_id)
        self.assertEqual(request.call_args.args[0],'/douyin/account')
        self.assertEqual(request.call_args.args[1]['tab'],'post')
        return result

    def test_missing_metrics_preserve_last_success_and_real_zero(self):
        raw=self.raw(statistics={'play_count':1200,'digg_count':0})
        self.fake_inspect([raw],True)
        self.fake_inspect([self.raw(statistics={'play_count':-1})],True)
        row=self.ops.works(self.employee,{})[0]
        self.assertEqual(row['metrics']['play_count']['value'],1200)
        self.assertFalse(row['metrics']['play_count']['available'])
        self.assertEqual(row['metrics']['digg_count']['value'],0)
        self.assertIsNone(row['metrics']['share_count']['value'])
        summary=self.ops.summary([row],self.employee)
        self.assertEqual(summary['metrics']['play_count']['stale'],1)
        self.assertIsNone(summary['metrics']['share_count']['value'])

    def test_publication_stats_deduplicate_reports_and_count_exact_monitor_window(self):
        start = self.binding['monitor_start']
        self.fake_inspect([self.raw(create_time=start)], False)
        payloads = [
            ('same', '1234567890123', start, self.binding['sec_uid']),
            ('new', '1234567890124', start+120, self.binding['sec_uid']),
            ('old', '1234567890125', start-1, self.binding['sec_uid']),
            ('future', '1234567890126', now()+86400, self.binding['sec_uid']),
            ('foreign', '1234567890127', start+300, 'another-author'),
            ('deleted', '1234567890128', start+400, self.binding['sec_uid']),
        ]
        for item, work_id, published, author in payloads:
            self.ops.execute('''INSERT INTO ops_report_items
                (id,batch_id,employee_id,group_id,link,received_at,updated,status,payload)
                VALUES(?,?,?,?,?,?,?,?,?)''', (item,'batch',self.other_id,'','link',now(),now(),'rejected',
                json.dumps({'id':work_id,'sec_uid':author,'published':published})))
        self.ops.execute('INSERT INTO ops_report_deletions VALUES(?,?,?)',('deleted',now(),self.admin['id']))
        latest_received = now()-120
        self.ops.execute('UPDATE ops_report_items SET received_at=? WHERE id=?',(latest_received,'new'))
        stats = self.ops.bindings(self.admin)[0]['publication_stats']
        self.assertEqual(stats['monitor_count'], 2)
        self.assertEqual(stats['latest_published'], start+120)
        self.assertEqual(stats['latest_work_id'], '1234567890124')
        self.assertEqual(stats['latest_work_reported_at'], latest_received)
        self.assertEqual(stats['period_start'], start)
        other = self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.other_id,),one=True)
        self.assertEqual(self.ops.bindings(other), [])
        self.ops.execute('UPDATE ops_bindings SET monitor_start=?,check_state=? WHERE id=?',
                         (start+121,'error',self.binding_id))
        stats = self.ops.bindings(self.employee)[0]['publication_stats']
        self.assertEqual(stats['monitor_count'], 0)
        self.assertEqual(stats['latest_published'], start+120)
        self.assertEqual(self.ops.query("SELECT status FROM ops_report_items WHERE id='new'",one=True)['status'],'rejected')

    def test_publication_stats_empty_and_deleted_works_do_not_invent_latest_time(self):
        self.assertIsNone(self.ops.bindings(self.admin)[0]['publication_stats']['latest_published'])
        self.fake_inspect([self.raw()], True)
        self.assertIsNone(self.ops.bindings(self.admin)[0]['publication_stats']['latest_work_reported_at'])
        self.ops.execute('INSERT INTO ops_work_deletions VALUES(?,?,?)',('1234567890123',now(),self.admin['id']))
        stats = self.ops.bindings(self.admin)[0]['publication_stats']
        self.assertIsNone(stats['latest_published'])
        self.assertIsNone(stats['latest_work_reported_at'])
        self.assertEqual(stats['monitor_count'], 0)

    def test_publication_receipt_uses_submission_time_not_collection_update_time(self):
        self.fake_inspect([self.raw()], True)
        received = now()-7200
        self.ops.execute('''INSERT INTO ops_submissions
            (id,work_id,user_id,submitted_at) VALUES(?,?,?,?)''',
            ('submission','1234567890123',self.employee_id,received))
        stats = self.ops.bindings(self.admin)[0]['publication_stats']
        self.assertEqual(stats['latest_work_reported_at'], received)
        self.assertLess(stats['latest_published'], received)

    def test_incomplete_or_failed_list_never_labels_missing(self):
        self.fake_inspect([self.raw()],False)
        self.assertEqual(self.ops.works(self.employee,{})[0]['state'],'confirm')
        self.fake_inspect([self.raw()],True)
        self.assertEqual(self.ops.works(self.employee,{})[0]['state'],'overdue')
        self.fake_inspect([],True)
        self.assertEqual(self.ops.works(self.employee,{})[0]['state'],'confirm')
        self.ops.execute("UPDATE ops_bindings SET check_state='error' WHERE id=?",(self.binding_id,))
        self.assertEqual(self.ops.works(self.employee,{})[0]['state'],'error')

    def test_bare_stable_api_without_completeness_is_conservative(self):
        with patch.object(app,'upstream',return_value={'data':[self.raw()]}):
            self.ops.inspect(self.binding_id)
        self.assertEqual(self.ops.works(self.employee,{})[0]['state'],'confirm')
        self.assertIsNone(self.ops.summary(self.ops.works(self.employee,{}),self.employee)['submission_rate'])

    def test_inspection_matches_upstream_slash_date_contract(self):
        def upstream(path,payload,config):
            self.assertEqual(path,'/douyin/account')
            # TikTokDownloader Account.check_date accepts this exact format.
            parsed=datetime.strptime(payload['earliest'],'%Y/%m/%d').date()
            self.assertEqual(parsed,datetime.fromtimestamp(self.binding['monitor_start'],TZ).date())
            return {'data':[self.raw()],'params':{'workspace_check':{'complete':True}}}
        with patch.object(app,'upstream',side_effect=upstream):
            status,_=self.ops.inspect(self.binding_id)
        self.assertEqual(status,'complete')
        self.assertEqual(len(self.ops.works(self.employee,{})),1)

    def test_complete_list_before_monitor_start_is_explained_and_not_treated_as_failure(self):
        status,message=self.fake_inspect([self.raw(create_time=self.binding['monitor_start']-60)],True)
        self.assertEqual(status,'complete')
        self.assertIn('日期筛选后返回 1 个作品',message)
        self.assertIn('监测期内 0 个',message)
        self.assertIn('监测开始：',message)
        self.assertEqual(self.ops.works(self.employee,{}),[])
        binding=self.ops.bindings(self.admin)[0]
        self.assertEqual(binding['check_state'],'complete')
        self.assertIsNotNone(binding['last_success'])

    def test_submit_checks_owner_locks_record_and_marks_late(self):
        with patch.object(app,'upstream',return_value={'data':self.raw()}):
            other=self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.other_id,),one=True)
            with self.assertRaisesRegex(ValueError,'不是你负责'):
                self.ops.submit({'link':'1234567890123'},other)
            self.ops.submit({'link':'1234567890123','submitted_at':1},self.employee)
            with self.assertRaisesRegex(ValueError,'不能重复'):
                self.ops.submit({'link':'1234567890123'},self.employee)
        submission=self.ops.query('SELECT * FROM ops_submissions',one=True)
        self.assertGreater(submission['submitted_at'],1)
        self.assertEqual(submission['late'],1)
        self.assertEqual(self.ops.works(self.employee,{})[0]['state'],'submitted')

    def test_employee_queries_export_and_admin_actions_are_scoped(self):
        self.fake_inspect([self.raw()],True)
        other=self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.other_id,),one=True)
        self.assertEqual(self.ops.get('works',other,{})['total'],0)
        self.assertEqual(self.ops.get('bindings',other,{}),[])
        with self.assertRaises(ValueError):
            self.ops.get('export',other,{})
        for resource in ('people','settings','jobs'):
            with self.assertRaises(AccessError):
                self.ops.get(resource,self.employee,{})
        with self.assertRaises(AccessError):
            self.ops.save_user({'name':'bad'},self.employee)
        data,mime=self.ops.get('export',self.employee,{})
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml=archive.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('暂无数据',xml)
        self.assertIn('1234567890123',xml)

    def test_ownership_is_snapshotted_and_monitor_boundary_applies(self):
        self.fake_inspect([self.raw()],True)
        self.ops.save_binding({**self.binding,'enabled':True,'owner_id':self.other_id,'monitor_start':now()-3600},self.admin)
        row=self.ops.works(self.employee,{})[0]
        self.assertEqual(row['owner_id'],self.employee_id)
        self.assertEqual(row['state'],'confirm')
        self.assertIsNone(row['deadline'])
        with patch.object(app,'upstream',return_value={'data':self.raw()}):
            self.ops.submit({'link':'1234567890123'},self.employee)
        self.assertEqual(self.ops.works(self.employee,{})[0]['state'],'submitted')

    def test_publication_count_is_unknown_when_account_cannot_be_verified(self):
        summary=self.ops.summary([],self.employee)
        self.assertFalse(summary['publication_complete'])
        self.assertIsNone(summary['submission_rate'])
        self.fake_inspect([],True)
        summary=self.ops.summary([],self.employee)
        self.assertTrue(summary['publication_complete'])
        self.assertEqual(summary['works'],0)

    def test_short_links_use_existing_share_then_detail_only(self):
        calls=[]
        def upstream(path,payload,config):
            calls.append(path)
            return {'url':'https://www.douyin.com/video/1234567890123'} if path.endswith('/share') else {'data':self.raw()}
        with patch.object(app,'upstream',side_effect=upstream):
            self.ops.submit({'link':'分享文本 https://v.douyin.com/test/'},self.employee)
        self.assertEqual(calls,['/douyin/share','/douyin/detail'])

    def test_settings_persist_and_reject_invalid_intervals(self):
        self.ops.save_settings({'auto_inspect':False,'interval_minutes':15,'deadline_hours':12,'retry_limit':0},self.admin)
        saved=self.ops.get('settings',self.admin,{})
        self.assertFalse(saved['auto_inspect'])
        self.assertEqual(saved['interval_minutes'],15)
        self.assertEqual(saved['deadline_hours'],12)
        with self.assertRaises(ValueError):
            self.ops.save_settings({'interval_minutes':0},self.admin)

    def test_failed_jobs_retry_and_preserve_check_failure(self):
        self.fake_inspect([self.raw()],True)
        job=self.ops.enqueue('refresh',self.raw()['aweme_id'],self.admin)
        self.assertEqual(job['id'],self.ops.enqueue('refresh',self.raw()['aweme_id'],self.admin)['id'])
        with patch.object(app,'upstream',side_effect=ValueError('network unavailable')):
            self.ops.run_due()
        saved=self.ops.query('SELECT * FROM ops_jobs WHERE id=?',(job['id'],),one=True)
        self.assertEqual(saved['status'],'retrying')
        work=self.ops.query('SELECT * FROM ops_works WHERE id=?',(self.raw()['aweme_id'],),one=True)
        self.assertTrue(work['last_error'])

    def test_password_is_hashed_and_disabled_sessions_rejected(self):
        self.assertNotIn('Employee-password',self.employee['password'])
        with self.assertRaises(AccessError):
            self.ops.login({'username':'employee','password':'Employee-password-8394'})
        auth,cookie=self.ops.login({'username':'admin','password':'Test-password-8394'})
        headers={'Cookie':cookie.split(';')[0]}
        self.assertEqual(self.ops.authenticate(headers)['id'],self.admin['id'])
        self.ops.execute('UPDATE ops_users SET enabled=0 WHERE id=?',(self.admin['id'],))
        self.assertIsNone(self.ops.authenticate(headers))
        with self.assertRaises(AccessError):
            self.ops.login({'username':'employee','password':'Employee-password-8394'})

    def test_http_auth_protects_legacy_and_employee_privileges(self):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        opener=build_opener(HTTPCookieProcessor(CookieJar()))
        def request(path,data=None):
            req=Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
            return opener.open(req)
        try:
            with self.assertRaises(HTTPError) as error:
                request('/api/accounts')
            self.assertEqual(error.exception.code,401)
            with self.assertRaises(HTTPError):
                request('/api/auth/login',{'username':'employee','password':'Employee-password-8394'})
            for path in ('/api/accounts','/api/tasks','/api/logs','/api/ops/people'):
                with self.assertRaises(HTTPError) as error:
                    request(path)
                self.assertEqual(error.exception.code,401)
            with request('/api/reporting/directory') as response:
                self.assertEqual(len(json.load(response)['employees']),2)
            with request('/api/auth/login',{'username':'admin','password':'Test-password-8394'}) as response:
                self.assertIn('HttpOnly',response.headers['Set-Cookie'])
                self.assertEqual(json.load(response)['user']['role'],'admin')
            with request('/api/ops/works') as response:
                self.assertEqual(json.load(response)['total'],0)
            with self.assertRaises(HTTPError):
                request('/api/auth/setup',{'username':'hijack','password':'hijack-password'})
        finally:
            server.shutdown();server.server_close();thread.join()


class CoverageTests(unittest.TestCase):
    def test_counts_and_dates_come_from_pages_before_filtering(self):
        c=Coverage()
        c.observe({'aweme_list':[{'create_time':100},{'create_time':200}],'has_more':1,'max_cursor':100},'aweme_list','max_cursor','has_more')
        c.observe({'aweme_list':[{'create_time':50}],'has_more':0,'max_cursor':0},'aweme_list','max_cursor','has_more')
        self.assertEqual(c.result()['raw_count'],3)
        self.assertEqual(c.result()['oldest_published'],50)
        self.assertEqual(c.result()['newest_published'],200)
        self.assertTrue(c.result()['complete'])

    def test_completion_requires_valid_end_or_boundary(self):
        c=Coverage();self.assertFalse(c.result()['complete'])
        c.observe({'aweme_list':[{}],'has_more':1,'max_cursor':123},'aweme_list','max_cursor','has_more')
        self.assertFalse(c.result()['complete'])
        c.observe({'aweme_list':[],'has_more':0,'max_cursor':0},'aweme_list','max_cursor','has_more')
        self.assertTrue(c.result()['complete'])
        c.valid=False;self.assertFalse(c.result()['complete'])

    def test_missing_metadata_and_repeated_cursor_never_complete(self):
        for page in ({'aweme_list':[]},{'aweme_list':[],'has_more':1,'max_cursor':1},{'aweme_list':[],'has_more':0,'max_cursor':0,'status_code':1}):
            c=Coverage();c.observe(page,'aweme_list','max_cursor','has_more');self.assertFalse(c.result()['complete'])
        c=Coverage();page={'aweme_list':[{}],'has_more':1,'max_cursor':1}
        c.observe(page,'aweme_list','max_cursor','has_more');c.observe(page,'aweme_list','max_cursor','has_more');c.boundary=True
        self.assertFalse(c.result()['complete'])
