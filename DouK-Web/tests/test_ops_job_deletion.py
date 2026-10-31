"""Removing a finished operations job must never remove collected work data."""
import unittest

from operations import AccessError,now
import test_operations as fixtures


class OperationsJobDeletionTests(unittest.TestCase):
    setUp=fixtures.OperationsTests.setUp
    tearDown=fixtures.OperationsTests.tearDown
    raw=fixtures.OperationsTests.raw
    fake_inspect=fixtures.OperationsTests.fake_inspect

    def job(self,status='partial'):
        job=self.ops.enqueue('inspect',self.binding_id,self.admin)['id']
        self.ops.execute('UPDATE ops_jobs SET status=?,message=?,attempts=1 WHERE id=?',(status,'无法确认列表完整性',job))
        return job

    def test_partial_job_removed_and_restored_without_deleting_data_or_retrying(self):
        self.fake_inspect([self.raw()],False)
        job=self.job()
        before={table:self.ops.query('SELECT * FROM '+table) for table in ('ops_works','ops_bindings')}
        self.ops.post('jobs/delete',{'confirmed':True,'id':job},self.admin)
        self.ops.post('jobs/delete',{'confirmed':True,'id':job},self.admin)
        self.assertEqual(self.ops.get('jobs',self.admin,{}),[])
        self.assertEqual([r['id'] for r in self.ops.get('jobs',self.admin,{'deleted':'1'})],[job])
        self.assertEqual(before,{table:self.ops.query('SELECT * FROM '+table) for table in before})
        with self.assertRaisesRegex(ValueError,'已删除'):self.ops.retry(job,self.admin)
        self.ops.post('jobs/restore',{'id':job},self.admin)
        restored=self.ops.get('jobs',self.admin,{})[0]
        self.assertEqual(restored['status'],'partial')
        self.assertEqual(restored['attempts'],1)
        self.assertEqual(self.ops.get('jobs',self.admin,{'deleted':'1'}),[])

    def test_pending_running_and_retrying_tasks_cannot_be_deleted(self):
        job=self.job('queued')
        for status in ('queued','running','retrying'):
            self.ops.execute('UPDATE ops_jobs SET status=? WHERE id=?',(status,job))
            with self.assertRaises(ValueError):self.ops.post('jobs/delete',{'confirmed':True,'id':job},self.admin)
        self.assertFalse(self.ops.query('SELECT * FROM ops_job_deletions'))

    def test_admin_required_and_failed_completed_records_supported(self):
        job=self.job('failed')
        for resource in ('jobs/delete','jobs/restore'):
            with self.assertRaises(AccessError):self.ops.post(resource,{'id':job},self.employee)
            with self.assertRaises(ValueError):self.ops.post(resource,{'id':'missing'},self.admin)
        self.ops.post('jobs/delete',{'confirmed':True,'id':job},self.admin)
        self.ops.post('jobs/restore',{'id':job},self.admin)
        self.ops.execute("UPDATE ops_jobs SET status='completed' WHERE id=?",(job,))
        self.ops.post('jobs/delete',{'confirmed':True,'id':job},self.admin)
        self.assertEqual(self.ops.get('jobs',self.admin,{'deleted':'1'})[0]['status'],'completed')


if __name__=='__main__':unittest.main()
