import io
import json
import unittest
import zipfile
from datetime import datetime
from unittest.mock import patch

import server as app
from operations import TZ, now, AccessError
from report_progress import ReportProgress
import test_operations as fixtures


class ReportProgressTests(unittest.TestCase):
    setUp = fixtures.OperationsTests.setUp
    tearDown = fixtures.OperationsTests.tearDown

    def day(self):
        return ReportProgress(self.ops).bounds(datetime.fromtimestamp(now(),TZ).strftime('%Y-%m-%d'))

    def receipt(self, item, employee, received, status='submitted'):
        self.ops.execute('''INSERT INTO ops_report_items
            (id,batch_id,employee_id,group_id,link,received_at,updated,status)
            VALUES(?,?,?,?,?,?,?,?)''',(item,'batch',employee,'','https://www.douyin.com/video/1234567890123',received,now(),status))

    def result(self, **filters):
        return self.ops.get('report-progress',self.admin,{'date':self.day()[0],**filters})

    def test_roster_includes_never_reported_without_binding_and_excludes_inactive_or_future(self):
        day,start,end = self.day()
        self.ops.execute('UPDATE ops_users SET created=? WHERE role=?',(start-86400,'employee'))
        result = self.result()
        self.assertEqual({r['id'] for r in result['items']},{self.employee_id,self.other_id})
        self.assertTrue(all(r['last_received'] is None for r in result['items']))
        self.ops.execute('UPDATE ops_users SET enabled=0 WHERE id=?',(self.other_id,))
        self.assertEqual(self.result()['total'],1)
        self.ops.execute('UPDATE ops_users SET enabled=1,created=? WHERE id=?',(end,self.other_id))
        self.assertEqual(self.result()['total'],1)
        with self.assertRaises(AccessError):
            self.ops.get('report-progress',self.employee,{})

    def test_received_date_boundaries_not_publish_or_review_dates_and_states(self):
        day,start,end = self.day()
        self.receipt('before',self.employee_id,start-1)
        self.receipt('first',self.employee_id,start,'review')
        self.receipt('last',self.employee_id,end-1,'failed')
        self.receipt('tomorrow',self.other_id,end)
        result = self.result(status='all')
        rows={r['id']:r for r in result['items']}
        self.assertEqual(rows[self.employee_id]['received_count'],2)
        self.assertEqual(rows[self.employee_id]['status'],'attention')
        self.assertEqual(rows[self.other_id]['status'],'missing')
        self.ops.execute("UPDATE ops_report_items SET status='submitted',updated=? WHERE id IN ('first','last')",(end+86400,))
        result=self.result(status='reported')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['accepted_count'],2)
        # A report detail deep link uses the same date boundaries.
        self.assertEqual(self.ops.reporting_list(self.admin,{'date':day,'employee':self.employee_id})['total'],2)

    def test_deleted_receipts_excuses_and_cancel_excuse(self):
        day,start,end=self.day()
        self.receipt('deleted',self.employee_id,start)
        self.ops.execute('INSERT INTO ops_report_deletions VALUES(?,?,?)',('deleted',now(),self.admin['id']))
        self.assertEqual(self.result()['counts']['missing'],2)
        payload={'date':day,'employee':self.employee_id,'enabled':True,'reason':'今天请假'}
        self.ops.post('report-progress/excuse',payload,self.admin)
        result=self.result(status='excused')
        self.assertEqual(result['items'][0]['excuse'],'今天请假')
        self.assertEqual(self.result()['counts']['missing'],1)
        self.ops.post('report-progress/excuse',{**payload,'enabled':False},self.admin)
        self.assertEqual(self.result()['counts']['missing'],2)
        with self.assertRaises(AccessError):
            self.ops.post('report-progress/excuse',payload,self.employee)
        with self.assertRaises(ValueError):
            self.ops.post('report-progress/excuse',{**payload,'reason':''},self.admin)

    def test_export_matches_filtered_names_and_missing_status(self):
        day,start,end=self.day()
        self.receipt('valid',self.employee_id,start)
        result=self.result(search='员工乙')
        self.assertEqual([r['id'] for r in result['items']],[self.other_id])
        data,mime=self.ops.get('report-progress/export',self.admin,{'date':day,'search':'员工乙'})
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml=archive.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('员工乙',xml)
        self.assertNotIn('员工甲',xml)
        with self.assertRaises(ValueError):
            self.result(date='2099-01-01')

    def test_disabled_inspection_cannot_be_enabled_queued_or_retried_via_api(self):
        with self.assertRaisesRegex(ValueError,'已移除'):
            self.ops.post('jobs',{'kind':'inspect','target':self.binding_id},self.admin)
        with self.assertRaisesRegex(ValueError,'已移除'):
            self.ops.save_settings({'auto_inspect':True},self.admin)
        # Legacy persisted configuration cannot reenable the removed scheduler.
        self.ops.execute('UPDATE ops_settings SET payload=? WHERE id=1',(json.dumps({'auto_inspect':True}),))
        self.assertFalse(self.ops.settings()['auto_inspect'])
        job=self.ops.enqueue('inspect',self.binding_id,self.admin)
        with patch.object(app,'upstream') as upstream:
            self.ops.run_due()
        upstream.assert_not_called()
        self.assertEqual(self.ops.query('SELECT status FROM ops_jobs WHERE id=?',(job['id'],),one=True)['status'],'cancelled')
        self.ops.execute("UPDATE ops_jobs SET status='failed' WHERE id=?",(job['id'],))
        with self.assertRaisesRegex(ValueError,'已移除'):
            self.ops.retry(job['id'],self.admin)


if __name__=='__main__':
    unittest.main()
