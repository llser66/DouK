"""Deletion removes the roster; explicit suspension alone blocks reporting."""
import unittest
from unittest.mock import patch
import server as app
from operations import AccessError
import test_operations as fixtures
class EmployeeDeletionTests(unittest.TestCase):
    setUp=fixtures.OperationsTests.setUp
    tearDown=fixtures.OperationsTests.tearDown
    raw=fixtures.OperationsTests.raw
    def delete(self):return self.ops.post('employees/delete',{'id':self.employee_id,'confirmed':True,'permanent':True},self.admin)
    def test_deletion_removes_person_account_and_data_not_a_disabled_record(self):
        receipt=self.ops.receive_report({'employee_id':self.employee_id,'links':'1234567890123','request_id':'employee-delete-report-0001'},'test',{'sec_uid':self.binding['sec_uid']})
        with patch.object(app,'upstream',return_value={'data':self.raw()}):self.ops.process_report_due()
        before=self.ops.query('SELECT COUNT(*) n FROM logs',one=True)['n']
        self.delete()
        self.assertIsNone(self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.employee_id,),one=True))
        self.assertFalse(self.ops.query('SELECT * FROM ops_bindings WHERE owner_id=?',(self.employee_id,)))
        for table in ('ops_employee_deletions','ops_works','ops_submissions','ops_report_items'):
            self.assertEqual(self.ops.query('SELECT * FROM '+table),[])
        self.assertTrue(self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.other_id,)))
        self.assertGreater(self.ops.query('SELECT COUNT(*) n FROM logs',one=True)['n'],before)
    def test_disabled_person_blocks_until_enabled_but_deleting_it_releases_name(self):
        claim={'name':'同一员工','department':'测试部门','group':'一组','employee_code':'RECREATE01'}
        def receive(request):return self.ops.receive_report({'reporter':claim,'links':'1234567890123','request_id':request},'test')
        first=receive('new-person-before-disable-0001')
        person=self.ops.query('SELECT employee_id FROM ops_report_items WHERE id=?',(first['items'][0]['id'],),one=True)['employee_id']
        self.ops.save_employee({'id':person,'enabled':False},self.admin)
        with self.assertRaisesRegex(ValueError,'停用'):receive('new-person-disabled-0001')
        self.ops.post('employees/delete',{'id':person,'confirmed':True,'permanent':True},self.admin)
        again=receive('new-person-after-delete-0001')
        new_id=self.ops.query('SELECT employee_id FROM ops_report_items WHERE id=?',(again['items'][0]['id'],),one=True)['employee_id']
        self.assertNotEqual(new_id,person)
        self.assertEqual(self.ops.query('SELECT enabled FROM ops_users WHERE id=?',(new_id,),one=True)['enabled'],1)
    def test_admin_confirmation_stale_client_and_restore_guard(self):
        with self.assertRaises(AccessError):self.ops.delete_employee({'id':self.employee_id,'confirmed':True,'permanent':True},self.employee)
        with self.assertRaisesRegex(ValueError,'永久删除'):self.ops.delete_employee({'id':self.employee_id,'confirmed':True},self.admin)
        with self.assertRaises(ValueError):self.ops.delete_employee({'id':self.admin['id'],'confirmed':True,'permanent':True},self.admin)
        with self.assertRaisesRegex(ValueError,'实际删除'):self.ops.restore_employee({'id':self.employee_id},self.admin)
    def test_legacy_deleted_owner_does_not_block_new_verified_reporter(self):
        self.ops.execute('INSERT INTO ops_employee_deletions VALUES(?,?,?,?)',(self.employee_id,1,self.admin['id'],1))
        self.ops.execute("UPDATE ops_bindings SET enabled=0,check_message='员工已删除，账号巡检已暂停' WHERE id=?",(self.binding_id,))
        self.ops.execute('UPDATE ops_users SET enabled=0 WHERE id=?',(self.employee_id,))
        normal=self.ops.save_user({'name':'明确停用者','username':'explicit-disabled','password':'Explicit-test-12345'},self.admin)['id']
        self.ops.execute('UPDATE ops_users SET enabled=0 WHERE id=?',(normal,))
        self.ops.purge_legacy_directory_deletions({'confirmed':True,'permanent':True},self.admin)
        self.assertEqual(self.ops.query('SELECT enabled FROM ops_users WHERE id=?',(normal,),one=True)['enabled'],0)
        receipt=self.ops.receive_report({'employee_id':self.other_id,'links':'1234567890123','request_id':'new-owner-after-delete-0001'},'test',{'sec_uid':self.binding['sec_uid']})
        with patch.object(app,'upstream',return_value={'data':self.raw()}):self.ops.process_report_due()
        self.assertEqual(self.ops.report_receipt(receipt)['items'][0]['status'],'submitted')
        self.assertEqual(self.ops.query('SELECT owner_id,enabled FROM ops_bindings',one=True),{'owner_id':self.other_id,'enabled':1})
if __name__=='__main__':unittest.main()
