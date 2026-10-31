"""Anonymous claims and server-issued Douyin identity snapshots, no platform calls."""
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import server as app
from douyin_login import DouyinLogin
from operations import AccessError
from report_accounts import ReportAccounts
import hashlib
import test_public_reports as report_fixtures


class ReportIdentityTests(unittest.TestCase):
    setUp = report_fixtures.PublicReportingTests.setUp
    tearDown = report_fixtures.PublicReportingTests.tearDown
    raw = report_fixtures.PublicReportingTests.raw
    receive = report_fixtures.PublicReportingTests.receive
    process = report_fixtures.PublicReportingTests.process

    def test_unlisted_reporter_auto_creates_roster_and_idempotently_reuses_it(self):
        before=len(self.ops.public_directory()['employees'])
        claim={'name':'测试未建档员工','department':'测试部门','group':'测试小组'}
        receipt=self.receive(employee_id='',reporter=claim)
        self.assertEqual(len(self.ops.public_directory()['employees']),before+1)
        self.assertEqual(self.receive(employee_id='',reporter=claim),receipt)
        self.receive(employee_id='',reporter=claim,request_id='another-valid-request-0001')
        self.assertEqual(len(self.ops.public_directory()['employees']),before+1)
        raw=self.raw();raw['author']['sec_uid']='new-account-unlisted'
        self.process(raw)
        row=next(r for r in self.ops.reporting_list(self.admin,{})['items'] if r['status']=='review')
        self.assertEqual(row['reporter_name'],'测试未建档员工')
        self.assertEqual(row['department_name'],'测试部门')
        self.ops.review_reports({'ids':[row['id']],'action':'approve'},self.admin)
        self.assertEqual(self.ops.report_receipt(receipt)['items'][0]['status'],'submitted')
        user=self.ops.query('SELECT * FROM ops_users WHERE id=?',(row['employee_id'],),one=True)
        self.assertEqual(user['password'],'!')

    def test_verified_new_author_auto_binds_and_approves_without_admin(self):
        claim={'name':'自动核验员工','department':'自动部门','group':'自动小组'}
        self.ops.receive_report({'reporter':claim,'links':'1234567890123','request_id':'verified-new-author-0001'},'test',{'sec_uid':'new-author-verified'})
        raw=self.raw();raw['author']['sec_uid']='new-author-verified'
        self.process(raw)
        row=self.ops.reporting_list(self.admin,{})['items'][0]
        self.assertEqual(row['account_match'],'match')
        self.assertEqual(row['status'],'submitted')
        self.assertEqual(self.ops.query('SELECT owner_id FROM ops_bindings WHERE sec_uid=?',('new-author-verified',),one=True)['owner_id'],row['employee_id'])

    def test_receipt_note_is_scoped_and_does_not_change_receipt_time_or_status(self):
        receipt=self.receive();other=self.receive(request_id='other-note-request-0001')
        item=receipt['items'][0]
        with self.assertRaises(AccessError):
            self.ops.report_note({**other,'item_id':item['id'],'note':'不能跨回执修改'})
        self.ops.report_note({**receipt,'item_id':item['id'],'note':'代同事报送，等待确认'})
        latest=self.ops.report_receipt(receipt)['items'][0]
        self.assertEqual(latest['reporter_note'],'代同事报送，等待确认')
        self.assertEqual(latest['received_at'],item['received_at'])
        self.assertEqual(latest['status'],'queued')
        self.assertIn('代同事报送',str(self.ops.reporting_list(self.admin,{})))
        with self.assertRaises(ValueError): self.ops.report_note({**receipt,'item_id':item['id'],'note':'长'*1001})

    def test_identity_mismatch_holds_even_previously_bound_author_and_snapshot_survives(self):
        identity={'sec_uid':'MS4w-other-login-identity','nickname':'测试登录账号','homepage':'https://www.douyin.com/user/MS4w-other-login-identity'}
        self.ops.receive_report({'employee_id':self.employee_id,'links':'1234567890123','request_id':'identity-mismatch-00001'},'test',identity)
        identity['sec_uid']=self.binding['sec_uid']
        self.process()
        row=self.ops.reporting_list(self.admin,{})['items'][0]
        self.assertEqual(row['account_match'],'mismatch')
        self.assertEqual(row['status'],'review')
        self.assertEqual(row['published'],self.raw()['create_time'])
        self.assertEqual(self.ops.query('SELECT COUNT(*) n FROM ops_submissions',one=True)['n'],0)

    def test_matching_identity_uses_stable_id_and_does_not_require_login(self):
        self.ops.receive_report({'employee_id':self.employee_id,'links':'1234567890123','request_id':'identity-matching-00001'},'test',{'sec_uid':self.binding['sec_uid'],'nickname':'昵称可能改变'})
        self.process()
        row=self.ops.reporting_list(self.admin,{})['items'][0]
        self.assertEqual(row['account_match'],'match')
        self.assertEqual(row['status'],'submitted')
        self.assertEqual(row['login_account']['nickname'],'昵称可能改变')

    def test_login_capability_scope_expiry_no_cookies_and_no_manual_identity_claim(self):
        manager=ReportAccounts(lambda:Path(self.temp.name)/'report-login-profiles')
        def fake_start(s):
            s.event=threading.Event();s._set('waiting','测试二维码');return s.status()
        with patch.object(DouyinLogin,'start',fake_start):
            first=manager.start('test-a');second=manager.start('test-b')
        with self.assertRaises(AccessError): manager.proof({'login_token':'0'*64})
        with self.assertRaises(ValueError): manager.proof(second)
        row=manager._entry(first)
        row['session'].save_cookie('sessionid=private-do-not-store',None)
        self.assertFalse(manager.status(first)['verified'])
        row['session'].save_cookie('sessionid=private-do-not-store',{'sec_uid':'MS4w-verified-report-profile'})
        self.assertTrue(manager.status(first)['verified'])
        self.assertNotIn('private-do-not-store',repr(manager.sessions))
        self.assertNotIn('cookie',json.dumps(manager.status(first)))
        self.assertFalse(manager.status(second)['verified'])
        manager.cancel(first)
        with self.assertRaises(AccessError):manager.proof(first)
        row['session'].save_cookie('sessionid=late-callback',{'sec_uid':'MS4w-late-profile'})
        self.assertEqual(row['identity'],{})

    def remembered_manager(self):
        return ReportAccounts(lambda:Path(self.temp.name)/'report-login-profiles',lambda:app.connect_db())

    def verified_capability(self, manager, sec_uid='MS4w-remembered-profile'):
        def fake_start(session):
            session.event=threading.Event()
            message=session.save_cookie('sessionid=never-store-this-cookie',{'sec_uid':sec_uid,'nickname':'本人账号'})
            session._set('saved',message)
            return session.status()
        with patch.object(DouyinLogin,'start',fake_start):
            return manager.start('test-browser')

    def test_verified_capability_survives_restart_without_saving_cookie_or_raw_token(self):
        manager=self.remembered_manager()
        result=self.verified_capability(manager)
        self.assertTrue(result['remembered'])
        self.assertEqual(result['expires_at']-result['verified_at'],7*86400)
        restored=self.remembered_manager().status(result)
        self.assertTrue(restored['verified'])
        self.assertEqual(restored['identity']['sec_uid'],'MS4w-remembered-profile')
        self.assertEqual(restored['expires_at'],result['expires_at'])
        self.assertEqual(restored['qr_image'],'')
        row=self.ops.query('SELECT * FROM ops_report_verifications',one=True)
        self.assertEqual(row['token_hash'],hashlib.sha256(result['login_token'].encode()).hexdigest())
        self.assertNotIn(result['login_token'],json.dumps(row))
        self.assertNotIn('never-store-this-cookie',json.dumps(row)+repr(manager.sessions)+json.dumps(restored))

    def test_reuse_does_not_extend_expiry_and_expired_proof_is_rejected(self):
        manager=self.remembered_manager()
        result=self.verified_capability(manager)
        with patch('report_accounts.time.time',return_value=result['expires_at']-1):
            self.assertEqual(manager.status(result)['expires_at'],result['expires_at'])
            self.assertEqual(manager.proof(result)['sec_uid'],'MS4w-remembered-profile')
        with patch('report_accounts.time.time',return_value=result['expires_at']):
            with self.assertRaises(AccessError):manager.status(result)
            with self.assertRaises(AccessError):manager.proof(result)

    def test_removal_revokes_other_instances_and_switch_preserves_old_report_snapshot(self):
        manager=self.remembered_manager()
        first=self.verified_capability(manager)
        identity=manager.proof(first)
        self.ops.receive_report({'employee_id':self.employee_id,'links':'1234567890123','request_id':'remembered-proof-report-0001'},'test',identity)
        another=self.remembered_manager()
        another.cancel(first)
        for reader in (manager,another):
            with self.assertRaises(AccessError):reader.proof(first)
        second=self.verified_capability(manager,'MS4w-second-remembered-profile')
        self.assertNotEqual(first['login_token'],second['login_token'])
        self.assertEqual(manager.proof(second)['sec_uid'],'MS4w-second-remembered-profile')
        claim=self.ops.query('SELECT login_identity FROM ops_report_claims',one=True)
        self.assertEqual(json.loads(claim['login_identity'])['sec_uid'],'MS4w-remembered-profile')

    def test_pending_and_unidentified_sessions_never_become_remembered_proof(self):
        manager=self.remembered_manager()
        def waiting(session):
            session.event=threading.Event();session._set('waiting','等待扫码');return session.status()
        with patch.object(DouyinLogin,'start',waiting):
            result=manager.start('test-pending')
        row=manager._entry(result)
        row['session'].save_cookie('sessionid=never-store-this-cookie',None)
        self.assertFalse(manager.status(result)['remembered'])
        self.assertEqual(self.ops.query('SELECT * FROM ops_report_verifications'),[])
        with self.assertRaises(AccessError):self.remembered_manager().status(result)
        manager.cancel(result)
        row['session'].save_cookie('sessionid=late-cookie',{'sec_uid':'MS4w-too-late-profile'})
        self.assertEqual(self.ops.query('SELECT * FROM ops_report_verifications'),[])

    def test_multiple_accounts_match_each_work_independently_and_snapshot_is_immutable(self):
        identities=[{'sec_uid':'MS4w-account-A','nickname':'同名账号'},{'sec_uid':'MS4w-account-B','nickname':'同名账号'}]
        links='\n'.join('https://www.douyin.com/video/'+str(1234567890123+n) for n in range(3))
        receipt=self.ops.receive_report({'employee_id':self.employee_id,'links':links,'request_id':'multi-account-batch-00001'},'test',identities)
        identities[1]['sec_uid']='tampered-after-receipt'
        for number,author in enumerate(('MS4w-account-A','MS4w-account-B','MS4w-account-C')):
            raw=self.raw(aweme_id=str(1234567890123+number));raw['author']['sec_uid']=author
            self.process(raw)
        items=self.ops.report_receipt(receipt)['items']
        self.assertEqual([i['status'] for i in items],['submitted','submitted','review'])
        rows={r['work_id']:r for r in self.ops.reporting_list(self.admin,{})['items']}
        self.assertEqual(rows['1234567890124']['login_account']['sec_uid'],'MS4w-account-B')
        self.assertEqual(rows['1234567890125']['account_match'],'mismatch')
        self.assertEqual(len(rows['1234567890125']['login_accounts']),2)
        self.assertEqual(rows['1234567890125']['login_account'],{})
        self.assertEqual(self.ops.query('SELECT COUNT(*) n FROM ops_submissions',one=True)['n'],2)

    def test_missing_nickname_uses_only_same_account_saved_label(self):
        manager=self.remembered_manager()
        first=self.verified_capability(manager,self.binding['sec_uid'])
        self.ops.execute("UPDATE ops_report_verifications SET nickname='' WHERE sec_uid=?",(self.binding['sec_uid'],))
        identity=manager.status(first)['identity']
        self.assertEqual(identity['nickname'],self.binding['nickname'])
        self.assertEqual(identity['nickname_source'],'saved_record')
        self.assertEqual(manager.proof(first)['sec_uid'],self.binding['sec_uid'])
        self.ops.execute("UPDATE ops_report_verifications SET sec_uid='MS4w-unrelated-profile'")
        self.assertEqual(manager.status(first)['identity']['nickname'],'')

    def test_multiple_proofs_dedupe_expire_and_revoke_independently(self):
        manager=self.remembered_manager()
        first=self.verified_capability(manager,'MS4w-account-A')
        second=self.verified_capability(manager,'MS4w-account-B')
        tokens=[first['login_token'],second['login_token'],first['login_token']]
        self.assertEqual({i['sec_uid'] for i in manager.proofs({'login_tokens':tokens})},{'MS4w-account-A','MS4w-account-B'})
        manager.cancel(first)
        self.assertEqual(manager.proofs({'login_tokens':tokens})[0]['sec_uid'],'MS4w-account-B')
        self.ops.execute('UPDATE ops_report_verifications SET expires=0')
        self.assertEqual(manager.proofs({'login_tokens':tokens}),[])
        for invalid in ('not-a-list',[{}],['bad-token'],['a'*64]*101):
            with self.assertRaises(ValueError):manager.proofs({'login_tokens':invalid})

    def test_multi_account_match_still_honors_explicit_disabled_binding(self):
        self.ops.execute('UPDATE ops_bindings SET enabled=0 WHERE id=?',(self.binding_id,))
        self.ops.receive_report({'employee_id':self.employee_id,'links':'1234567890123','request_id':'multi-disabled-account-0001'},'test',
            [{'sec_uid':'MS4w-other-profile'},{'sec_uid':self.binding['sec_uid']}])
        self.process()
        row=self.ops.reporting_list(self.admin,{})['items'][0]
        self.assertEqual(row['account_match'],'match')
        self.assertEqual(row['status'],'review')
        self.assertIn('明确停用',row['message'])


if __name__=='__main__':unittest.main()
