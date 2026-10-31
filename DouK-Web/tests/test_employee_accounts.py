import json
import threading
import unittest
from unittest.mock import patch

import server as app
from douyin_login import DouyinLogin
from operations import AccessError, now
from ops_accounts import EmployeeAccounts
import test_operations as operations_tests


class EmployeeAccountTests(unittest.TestCase):
    setUp = operations_tests.OperationsTests.setUp
    tearDown = operations_tests.OperationsTests.tearDown

    def manager(self):
        if not hasattr(self,'accounts'):
            self.accounts=EmployeeAccounts(self.ops,lambda: app.DATA/'employee-profiles')
        return self.accounts

    def start(self,**data):
        with patch.object(DouyinLogin,'start',lambda s: s._set('waiting','测试等待登录')):
            row=self.manager().start({'nickname':'员工自己的账号',**data},self.employee)
        self.manager().sessions[row['id']]._set('idle','')
        return row['id']

    def login(self,sec_uid,**data):
        login_id=self.start(**data)
        self.manager().received(login_id,'sessionid=test-own-private',{'sec_uid':sec_uid})
        return login_id

    def test_employee_can_login_and_bind_many_accounts_with_forced_ownership(self):
        for number in range(12):
            self.login(f'MS4w-employee-account-{number}',owner_id=self.other_id)
        own=self.ops.bindings(self.employee)
        self.assertEqual(len(own),13)  # Includes the original administrator-assigned account.
        self.assertTrue(all(b['owner_id']==self.employee_id for b in own))
        public=json.dumps(self.manager().list(self.employee))
        self.assertNotIn('test-own-private',public)
        self.assertNotIn('"cookie"',public)
        other=self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.other_id,),one=True)
        self.assertEqual(self.manager().list(other),[])

    def test_employee_cannot_start_confirm_or_cancel_another_employees_login(self):
        login_id=self.login('MS4w-own-profile-01')
        other=self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.other_id,),one=True)
        for action,data in [('start',{'id':login_id}),('complete',{'id':login_id,'homepage':'MS4w-own-profile-01'}),('cancel',{'id':login_id}),('start',{'binding_id':self.binding_id})]:
            with self.assertRaises(AccessError):
                getattr(self.manager(),action)(data,other)

    def test_admin_binding_can_be_logged_in_without_creating_duplicate(self):
        login_id=self.login(self.binding['sec_uid'],binding_id=self.binding_id)
        self.assertEqual(self.manager().row(login_id)['binding_id'],self.binding_id)
        self.assertEqual(len(self.ops.bindings(self.employee)),1)
        self.assertEqual(self.ops.bindings(self.employee)[0]['monitor_start'],self.binding['monitor_start'])

    def test_wrong_account_on_relogin_preserves_previous_credentials(self):
        login_id=self.login(self.binding['sec_uid'],binding_id=self.binding_id)
        message=self.manager().received(login_id,'sessionid=wrong-private',{'sec_uid':'MS4w-wrong-account'})
        self.assertIn('不一致',message)
        self.assertEqual(self.manager().row(login_id)['cookie'],'sessionid=test-own-private')

    def test_fallback_is_pending_until_employee_confirms_homepage(self):
        login_id=self.start()
        self.manager().received(login_id,'sessionid=pending-private',None)
        self.assertEqual(len(self.ops.bindings(self.employee)),1)
        self.assertFalse(self.manager().list(self.employee)[0]['identity_verified'])
        self.manager().complete({'id':login_id,'homepage':'https://www.douyin.com/user/MS4w-manually-confirmed'},self.employee)
        self.assertEqual(len(self.ops.bindings(self.employee)),2)
        self.assertFalse(self.manager().list(self.employee)[0]['identity_verified'])

    def test_cannot_claim_other_employee_or_change_detected_identity(self):
        other=self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.other_id,),one=True)
        self.ops.save_binding({'homepage':'MS4w-other-employee','nickname':'其他员工账号','owner_id':self.other_id,'monitor_start':now()},self.admin)
        login_id=self.login('MS4w-other-employee')
        with self.assertRaisesRegex(ValueError,'其他员工'):
            self.manager().complete({'id':login_id,'homepage':'MS4w-other-employee'},self.employee)
        with self.assertRaisesRegex(ValueError,'不一致'):
            self.manager().complete({'id':login_id,'homepage':'MS4w-forged-profile'},self.employee)
        self.assertEqual(len(self.ops.bindings(other)),1)
        self.assertEqual(len(self.ops.bindings(self.employee)),1)

    def test_reassignment_and_disable_stop_credential_access(self):
        login_id=self.login(self.binding['sec_uid'],binding_id=self.binding_id)
        self.ops.save_binding({**self.binding,'owner_id':self.other_id,'enabled':True},self.admin)
        self.assertEqual(self.manager().list(self.employee),[])
        with self.assertRaises(AccessError):
            self.manager().row(login_id,self.employee)
        self.assertNotEqual(app.operations_config(binding_id=self.binding_id)['cookie'],'sessionid=test-own-private')
        self.ops.execute('UPDATE ops_users SET enabled=0 WHERE id=?',(self.employee_id,))
        with self.assertRaises(AccessError):
            self.manager().received(login_id,'sessionid=new-private',None)

    def test_inspection_and_submission_use_employee_credentials_only_from_existing_apis(self):
        self.login(self.binding['sec_uid'],binding_id=self.binding_id)
        self.login('MS4w-employee-second')
        # A paused default collector must not prevent this employee's own account being used.
        default=app.create_account({'name':'默认采集账号'})
        app.update_account(default['id'],{'paused':True})
        with patch.object(app,'upstream',return_value={'data':[]} ) as upstream:
            self.ops.inspect(self.binding_id)
        self.assertEqual(upstream.call_args.args[0],'/douyin/account')
        self.assertEqual(upstream.call_args.args[1]['cookie'],'sessionid=test-own-private')
        with patch.object(app,'upstream',side_effect=[{'url':'https://www.douyin.com/video/1234567890123'},{'data':operations_tests.OperationsTests.raw(self)}]) as upstream:
            self.ops.submit({'link':'https://v.douyin.com/test/','binding_id':self.binding_id},self.employee)
        self.assertEqual([c.args[0] for c in upstream.call_args_list],['/douyin/share','/douyin/detail'])

    def test_profile_capture_worker_handles_missing_identity_without_claiming_it(self):
        received=[]
        session=DouyinLogin(app.DATA/'capture-test',lambda c,i: received.append((c,i)) or '请确认主页',lambda:False,capture_profile=True)
        with patch.object(session,'_read_endpoint',return_value=True),patch.object(session,'_cookies',return_value='sessionid=private'),patch.object(session,'_identity',return_value=None),patch.object(session,'_close'):
            session._run('chrome',threading.Event())
        self.assertEqual(received,[('sessionid=private',None)])
        self.assertEqual(session.status()['message'],'请确认主页')
        self.assertNotIn('private',json.dumps(session.status()))


if __name__=='__main__':
    unittest.main()
