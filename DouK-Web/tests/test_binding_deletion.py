"""Deleted bindings can be added again; explicit disabled bindings remain blocked."""
from datetime import datetime
import unittest
from unittest.mock import patch
from operations import AccessError,TZ,now
from work_targets import WorkTargets
import test_operations as fixtures
class BindingDeletionTests(unittest.TestCase):
    setUp=fixtures.OperationsTests.setUp
    tearDown=fixtures.OperationsTests.tearDown
    raw=fixtures.OperationsTests.raw
    fake_inspect=fixtures.OperationsTests.fake_inspect
    def delete(self):return self.ops.post('bindings/delete',{'id':self.binding_id,'confirmed':True,'permanent':True},self.admin)
    def test_deleted_binding_and_related_work_physically_removed_and_can_readd(self):
        self.fake_inspect([self.raw()],True)
        self.delete()
        self.assertEqual(self.ops.bindings(self.admin),[])
        self.assertEqual(self.ops.query('SELECT * FROM ops_binding_deletions'),[])
        self.assertEqual(self.ops.query('SELECT * FROM ops_works'),[])
        new=self.ops.save_binding({'homepage':self.binding['homepage'],'nickname':'重新添加','owner_id':self.other_id,'monitor_start':now()},self.admin)['id']
        self.assertNotEqual(new,self.binding_id)
        self.assertEqual(self.ops.query('SELECT enabled FROM ops_bindings WHERE id=?',(new,),one=True)['enabled'],1)
    def test_explicit_disable_is_preserved_not_automatically_enabled_by_report(self):
        self.ops.save_binding({**self.binding,'enabled':False},self.admin)
        receipt=self.ops.receive_report({'employee_id':self.employee_id,'links':'1234567890123','request_id':'explicit-disabled-report-0001'},'test',{'sec_uid':self.binding['sec_uid']})
        with patch.object(self.ops,'upstream',return_value={'data':self.raw()}):self.ops.process_report_due()
        self.assertEqual(self.ops.report_receipt(receipt)['items'][0]['status'],'review')
        self.assertIn('明确停用',self.ops.report_receipt(receipt)['items'][0]['message'])
        self.assertEqual(self.ops.query('SELECT enabled FROM ops_bindings',one=True)['enabled'],0)
    def test_pending_running_and_retrying_jobs_block_deletion(self):
        job=self.ops.enqueue('inspect',self.binding_id,self.admin)['id']
        for status in ('queued','running','retrying'):
            self.ops.execute('UPDATE ops_jobs SET status=? WHERE id=?',(status,job))
            with self.assertRaisesRegex(ValueError,'任务'):self.delete()
        self.ops.execute("UPDATE ops_jobs SET status='completed' WHERE id=?",(job,));self.delete()
    def test_admin_confirmation_and_restore_guard(self):
        with self.assertRaises(AccessError):self.ops.remove_binding({'id':self.binding_id,'confirmed':True,'permanent':True},self.employee)
        with self.assertRaisesRegex(ValueError,'永久删除'):self.ops.remove_binding({'id':self.binding_id,'confirmed':True},self.admin)
        with self.assertRaisesRegex(ValueError,'实际删除'):self.ops.remove_binding({'id':self.binding_id},self.admin,restore=True)
    def test_edit_rechecks_deletion_after_homepage_resolution(self):
        def resolve(*args,**kwargs):self.delete();return self.binding['sec_uid']
        with patch.object(self.ops,'resolve',side_effect=resolve):
            with self.assertRaisesRegex(ValueError,'已删除'):self.ops.save_binding({**self.binding,'enabled':True},self.admin)
    def test_group_goal_remains_unconfirmed_after_binding_deletion(self):
        day=datetime.fromtimestamp(now(),TZ).date().isoformat()
        self.fake_inspect([],True)
        targets=WorkTargets(self.ops)
        targets.save({'scope':'employee','target_id':self.employee_id,'start':day,'end':day,'quantity':3},self.admin)
        self.delete()
        self.assertIsNone(targets.list(self.admin,{})['items'][0]['shortfall'])
if __name__=='__main__':unittest.main()
