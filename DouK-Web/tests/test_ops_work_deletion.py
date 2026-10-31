"""Confirmed physical deletion clears deduplication and preserves transactional logs."""
import unittest
from unittest.mock import patch
import server as app
from operations import AccessError,now
import test_operations as fixtures

class WorkDeletionTests(unittest.TestCase):
    setUp=fixtures.OperationsTests.setUp
    tearDown=fixtures.OperationsTests.tearDown
    raw=fixtures.OperationsTests.raw
    fake_inspect=fixtures.OperationsTests.fake_inspect

    def submitted(self):
        work_id=self.raw()['aweme_id']
        receipt=self.ops.receive_report({'employee_id':self.employee_id,'links':work_id,'request_id':'delete-work-report-00001'},'test',{'sec_uid':self.binding['sec_uid']})
        with patch.object(app,'upstream',return_value={'data':self.raw()}):self.ops.process_report_due()
        return work_id,receipt

    def test_deletion_clears_work_submission_related_data_and_allows_resubmission(self):
        work_id,receipt=self.submitted()
        with app.connect_db() as db:
            db.execute('INSERT INTO ops_verified VALUES(?,?)',(self.binding_id,work_id))
            db.execute('INSERT INTO ops_explanations VALUES(?,?,?,?)',(work_id,self.employee_id,'说明',now()))
            db.execute('INSERT INTO ops_reviews VALUES(?,?,?,?,?)',(work_id,'exempt','原因',self.admin['id'],now()))
        job=self.ops.enqueue('refresh',work_id,self.admin)['id']
        before=self.ops.query('SELECT COUNT(*) n FROM logs',one=True)['n']
        self.ops.post('works/delete',{'id':work_id,'confirmed':True,'permanent':True},self.admin)
        for table,column in [('ops_works','id'),('ops_submissions','work_id'),('ops_verified','work_id'),('ops_explanations','work_id'),('ops_reviews','work_id'),('ops_work_attribution','work_id'),('ops_work_deletions','work_id')]:
            self.assertFalse(self.ops.query(f'SELECT * FROM {table} WHERE {column}=?',(work_id,)),table)
        self.assertFalse(self.ops.query('SELECT * FROM ops_jobs WHERE id=?',(job,)))
        self.assertEqual(self.ops.summary(self.ops.works(self.admin,{}),self.admin)['works'],0)
        with self.assertRaises(AccessError):self.ops.report_receipt(receipt)
        self.assertGreater(self.ops.query('SELECT COUNT(*) n FROM logs',one=True)['n'],before)
        self.assertIn(work_id,str(self.ops.query("SELECT * FROM logs WHERE event='删除作品及关联报送数据'")))
        fresh=self.ops.receive_report({'employee_id':self.employee_id,'links':work_id,'request_id':'resubmit-deleted-work-0001'},'test',{'sec_uid':self.binding['sec_uid']})
        with patch.object(app,'upstream',return_value={'data':self.raw()}):self.ops.process_report_due()
        self.assertEqual(self.ops.report_receipt(fresh)['items'][0]['status'],'submitted')
        self.assertEqual(len(self.ops.query('SELECT * FROM ops_submissions')),1)

    def test_delete_is_atomic_with_its_audit_log_and_guards_running_jobs(self):
        work_id,_=self.submitted()
        with patch.object(self.ops,'audit',side_effect=RuntimeError('Log unavailable')):
            with self.assertRaises(RuntimeError):self.ops.remove_work({'id':work_id,'confirmed':True,'permanent':True},self.admin)
        self.assertTrue(self.ops.query('SELECT * FROM ops_submissions WHERE work_id=?',(work_id,)))
        for kind,target in [('refresh',work_id),('inspect',self.binding_id)]:
            job=self.ops.enqueue(kind,target,self.admin)['id']
            self.ops.execute("UPDATE ops_jobs SET status='running' WHERE id=?",(job,))
            with self.assertRaisesRegex(ValueError,'正在采集'):self.ops.remove_work({'id':work_id,'confirmed':True,'permanent':True},self.admin)
            self.ops.execute("UPDATE ops_jobs SET status='completed' WHERE id=?",(job,))

    def test_delayed_refresh_cannot_recreate_deleted_work(self):
        work_id,_=self.submitted()
        normalized=self.ops.normalize(self.raw())
        def detail(*args):
            self.ops.remove_work({'id':work_id,'confirmed':True,'permanent':True},self.admin)
            return normalized
        with patch.object(self.ops,'detail',side_effect=detail):
            with self.assertRaisesRegex(ValueError,'已删除'):self.ops.refresh_work(work_id)
        self.assertFalse(self.ops.query('SELECT * FROM ops_works'))

    def test_all_delete_endpoints_require_admin_confirmation_and_restore_is_unavailable(self):
        for resource in ('employees/delete','jobs/delete','works/delete','deleted/purge'):
            with self.assertRaises(AccessError):self.ops.post(resource,{'id':'any','confirmed':True,'permanent':True},self.employee)
            for data in ({'id':'any'},{'id':'any','confirmed':False},{'id':'any','confirmed':'true'}):
                with self.assertRaisesRegex(ValueError,'确认删除'):self.ops.post(resource,data,self.admin)
        with self.assertRaisesRegex(ValueError,'不能从页面恢复'):self.ops.post('works/restore',{'id':'any'},self.admin)
        with self.assertRaises(ValueError):self.ops.post('works/delete',{'id':'missing','confirmed':True,'permanent':True},self.admin)

if __name__=='__main__':unittest.main()
