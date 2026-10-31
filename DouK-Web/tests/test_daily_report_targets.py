import io
import json
import unittest
import zipfile
from datetime import datetime, timedelta
from unittest.mock import patch

from daily_report_targets import DailyReportTargets
from operations import TZ, AccessError, now
from report_progress import ReportProgress
import test_operations as fixtures


class DailyTargetTests(unittest.TestCase):
    setUp = fixtures.OperationsTests.setUp
    tearDown = fixtures.OperationsTests.tearDown
    raw = fixtures.OperationsTests.raw

    def dates(self):
        day, start, end = ReportProgress(self.ops).bounds()
        previous = (datetime.fromisoformat(day)-timedelta(days=1)).date().isoformat()
        self.ops.execute("UPDATE ops_users SET created=? WHERE role='employee'", (start-86400*10,))
        return day, start, end, previous

    def default(self, quantity=2, **changes):
        day, _, _, previous = self.dates()
        self.ops.post('daily-targets', {'scope':'default','start_day':previous,'quantity':quantity,
                                       'cutoff':'23:59','weekdays':list(range(7)),**changes}, self.admin)

    def personal(self, **changes):
        day, _, _, _ = self.dates()
        self.ops.post('daily-targets', {'scope':'employee','employees':[self.employee_id],
                                       'start_day':day,'quantity':3,**changes}, self.admin)

    def result(self, **filters):
        return self.ops.get('report-progress', self.admin, {'status':'all', **filters})

    def work(self, work_id, published, received, reporter=None):
        normalized = self.ops.normalize(self.raw(work_id, create_time=published))
        with self.ops.connect() as db:
            self.ops.upsert_work(normalized, self.binding, db)
            db.execute('INSERT INTO ops_submissions VALUES(?,?,?,?,?,?)',
                       ('s'+work_id,work_id,reporter or self.employee_id,received,0,None))

    def pending(self, item, work_id, published, received, status='review'):
        self.ops.execute('''INSERT INTO ops_report_items
            (id,batch_id,employee_id,group_id,link,received_at,updated,status,work_id,payload)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (item,'batch',self.employee_id,'','https://www.douyin.com/video/'+work_id,received,now(),status,
             work_id,json.dumps({'id':work_id,'published':published})))

    def test_effective_versions_personal_day_inheritance_and_rest_days(self):
        day, start, end, previous = self.dates()
        self.default(2, weekdays=[datetime.fromisoformat(day).weekday()])
        self.personal(quantity=3)
        goals=DailyReportTargets(self.ops)
        self.assertEqual(goals.resolve(day,self.employee_id)['quantity'],3)
        self.personal(kind='day',quantity=1,note='临时调整')
        self.assertEqual(goals.resolve(day,self.employee_id)['quantity'],1)
        self.personal(kind='day',inherit=True,note='恢复常规')
        self.assertEqual(goals.resolve(day,self.employee_id)['quantity'],3)
        tomorrow=datetime.fromtimestamp(end,TZ).date().isoformat()
        self.assertEqual(goals.resolve(tomorrow,self.employee_id)['quantity'],0)
        self.personal(start_day=tomorrow,kind='day',quantity=4,note='休息日加班')
        self.assertEqual(goals.resolve(tomorrow,self.employee_id)['quantity'],4)
        self.personal(inherit=True)
        self.assertEqual(goals.resolve(day,self.employee_id)['quantity'],2)
        self.default(5,start_day=tomorrow)
        self.assertEqual(goals.resolve(day,self.employee_id)['quantity'],2)
        self.assertEqual(goals.resolve(tomorrow,self.other_id)['quantity'],5)
        history=goals.list(self.admin,{})['history']
        self.assertEqual(len(history),7)
        self.assertTrue(any(h['quantity'] is None for h in history))

    def test_publish_date_owner_dedup_pending_late_and_missing_time(self):
        day,start,end,previous=self.dates()
        self.default(3)
        self.work('1234567890123',start+60,start+120,reporter=self.other_id)
        self.work('1234567890124',start+120,end+60)
        self.work('1234567890125',start-60,start+60)
        self.pending('same','1234567890123',start+60,start+180)
        self.pending('review1','1234567890126',start+120,start+180)
        self.pending('review2','1234567890126',start+120,start+240)
        self.pending('unknown','1234567890127',None,start+180,'failed')
        self.pending('old','1234567890128',start-60,start+180)
        self.pending('duplicate','1234567890129',start+180,start+240,'duplicate')
        with patch('operations.now', return_value=end+60):
            rows={r['id']:r for r in self.result()['items']}
        row=rows[self.employee_id]
        self.assertEqual((row['completed_count'],row['remaining'],row['pending_count'],row['unlocated_count']),(2,1,1,1))
        self.assertEqual((row['ontime_count'],row['late_count'],row['status'],row['phase']),(1,1,'attention','overdue'))
        self.assertEqual(rows[self.other_id]['completed_count'],0)
        self.ops.execute('INSERT INTO ops_work_deletions VALUES(?,?,?)',('1234567890123',now(),self.admin['id']))
        row=next(r for r in self.result()['items'] if r['id']==self.employee_id)
        self.assertEqual(row['completed_count'],1)

    def test_cutoff_uses_original_receipt_and_excuse_overrides_goal(self):
        day,start,end,previous=self.dates()
        self.default(1,cutoff='18:00')
        cutoff=start+18*3600+60
        with patch('operations.now',return_value=cutoff-1):
            self.assertTrue(all(r['phase']=='in_progress' for r in self.result()['items']))
        with patch('operations.now',return_value=cutoff):
            self.assertTrue(all(r['phase']=='overdue' for r in self.result()['items']))
        # An approval occurring later does not turn an originally timely receipt into late work.
        self.work('1234567890123',start+60,cutoff-1)
        row=next(r for r in self.result()['items'] if r['id']==self.employee_id)
        self.assertEqual((row['status'],row['late_count']),('reported',0))
        self.ops.post('report-progress/excuse',{'date':day,'employee':self.other_id,'reason':'今天请假'},self.admin)
        row=next(r for r in self.result()['items'] if r['id']==self.other_id)
        self.assertEqual((row['status'],row['required'],row['remaining']),('excused',0,0))

    def test_future_publication_is_not_already_completed(self):
        day,start,end,previous=self.dates()
        self.default(2)
        self.work('1234567890130',start+3600,start+3700)
        self.work('1234567890131',start+7200,start+3700)
        with patch('operations.now',return_value=start+4000):
            row=next(r for r in self.result()['items'] if r['id']==self.employee_id)
        self.assertEqual((row['completed_count'],row['remaining']),(1,1))
        self.assertEqual(row['latest_published'],start+3600)

    def test_access_validation_and_atomic_batch(self):
        self.default()
        day,_,_,_=self.dates()
        for method in (lambda:self.ops.get('daily-targets',self.employee,{}),
                       lambda:self.ops.post('daily-targets',{'scope':'default'},self.employee)):
            with self.assertRaises(AccessError):method()
        count=len(self.ops.query('SELECT * FROM ops_daily_report_rules'))
        for changes in ({'quantity':True},{'quantity':-1},{'quantity':1.5},{'cutoff':'24:00'},
                        {'weekdays':[]},{'weekdays':[True]},{'start_day':'2026-02-30'}):
            with self.assertRaises(ValueError):self.default(**changes)
        with self.assertRaises(ValueError):
            self.personal(employees=[self.employee_id,'missing'])
        self.assertEqual(len(self.ops.query('SELECT * FROM ops_daily_report_rules')),count)
        self.personal(employees=[self.employee_id,self.other_id],quantity=4)
        self.assertTrue(all(r['required']==4 for r in self.result()['items']))

    def test_start_date_preserves_history_and_export_is_filtered(self):
        day,start,end,previous=self.dates()
        self.default(start_day=day)
        self.assertFalse(self.result(date=previous)['quota_mode'])
        self.assertTrue(self.result()['quota_mode'])
        data,_=self.ops.get('report-progress/export',self.admin,{'date':day,'status':'all','employee':self.employee_id})
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml=archive.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('要求条数',xml)
        self.assertIn('有效完成',xml)
        self.assertIn('员工甲',xml)
        self.assertNotIn('员工乙',xml)
        directory=self.ops.public_directory()
        self.assertTrue(directory['daily_target']['configured'])
        self.assertTrue(all('daily_target' in p for p in directory['employees']))


if __name__=='__main__':
    unittest.main()
