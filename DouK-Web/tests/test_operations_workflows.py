import io
import unittest
import zipfile
from unittest.mock import patch

import server as app
from operations import AccessError,now
import test_operations as fixtures


class WorkflowTests(unittest.TestCase):
    setUp=fixtures.OperationsTests.setUp
    tearDown=fixtures.OperationsTests.tearDown
    raw=fixtures.OperationsTests.raw
    fake_inspect=fixtures.OperationsTests.fake_inspect

    def test_employee_explanation_admin_exemption_and_reopen_preserve_facts(self):
        self.fake_inspect([self.raw()],True)
        work_id=self.raw()['aweme_id']
        self.ops.post('explain',{'id':work_id,'note':'这件作品属于测试发布，请核实'},self.employee)
        other=self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.other_id,),one=True)
        with self.assertRaises(AccessError):
            self.ops.explain({'id':work_id,'note':'其他员工说明'},other)
        with self.assertRaises(AccessError):
            self.ops.review({'id':work_id,'action':'exempt','note':'自行免除'},self.employee)
        self.ops.review({'id':work_id,'action':'exempt','note':'确认测试发布，无需提交'},self.admin)
        row=self.ops.works(self.employee,{})[0]
        self.assertEqual(row['state'],'exempt')
        self.assertIsNone(row['submitted_at'])
        summary=self.ops.summary([row],self.employee)
        self.assertEqual(summary['submitted'],0)
        self.assertEqual(summary['exempt'],1)
        self.assertIsNone(summary['submission_rate'])
        data,_=self.ops.get('export',self.employee,{})
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml=archive.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('这件作品属于测试发布，请核实',xml)
        self.assertIn('确认测试发布，无需提交',xml)
        self.ops.review({'id':work_id,'action':'reopen','note':'恢复正常运营提交核查'},self.admin)
        self.assertEqual(self.ops.works(self.employee,{})[0]['state'],'overdue')

    def test_review_does_not_turn_an_incomplete_inspection_into_missing(self):
        self.fake_inspect([self.raw()],False)
        work_id=self.raw()['aweme_id']
        self.ops.review({'id':work_id,'action':'exempt','note':'暂不要求提交'},self.admin)
        self.ops.review({'id':work_id,'action':'reopen','note':'恢复核查但保持完整性判断'},self.admin)
        self.assertEqual(self.ops.works(self.employee,{})[0]['state'],'confirm')
        with patch.object(app,'upstream',return_value={'data':self.raw()}):
            self.ops.submit({'link':work_id},self.employee)
        with self.assertRaises(ValueError):
            self.ops.review({'id':work_id,'action':'exempt','note':'不能改写提交事实'},self.admin)

    def test_employee_can_refresh_only_own_accounts_and_cannot_spam_queue(self):
        job=self.ops.enqueue('inspect',self.binding_id,self.employee)
        self.assertEqual(job['id'],self.ops.enqueue('inspect',self.binding_id,self.employee)['id'])
        other=self.ops.query('SELECT * FROM ops_users WHERE id=?',(self.other_id,),one=True)
        with self.assertRaises(AccessError):
            self.ops.enqueue('inspect',self.binding_id,other)
        self.assertEqual(self.ops.get('my-jobs',other,{}),[])
        self.ops.execute("UPDATE ops_jobs SET status='completed' WHERE id=?",(job['id'],))
        with self.assertRaisesRegex(ValueError,'稍后'):
            self.ops.enqueue('inspect',self.binding_id,self.employee)
        self.assertEqual(self.ops.get('submission-rules',self.employee,{}),{'deadline_hours':24})

    def test_todo_prioritizes_old_overdue_works_instead_of_recent_submitted(self):
        with self.ops.connect() as db:
            for n in range(35):
                raw=self.raw(str(1234567890000+n),create_time=now()-3600)
                self.ops.upsert_work(self.ops.normalize(raw),self.binding,db)
            raw=self.raw('9999999999999',create_time=now()-86400*2)
            self.ops.upsert_work(self.ops.normalize(raw),self.binding,db)
            db.execute("UPDATE ops_bindings SET check_state='complete'")
            db.execute('INSERT INTO ops_verified SELECT binding_id,id FROM ops_works')
        self.assertEqual(self.ops.get('todos',self.employee,{})['items'][0]['id'],'9999999999999')

    def test_publication_minute_filters_align_list_summary_and_export(self):
        from datetime import datetime
        from operations import TZ
        start = datetime.fromtimestamp(now()-3600,TZ).replace(second=0,microsecond=0)
        base = int(start.timestamp())
        ids = [str(1234567800000+i) for i in range(4)]
        self.fake_inspect([self.raw(work_id,create_time=base+offset) for work_id,offset in zip(ids,(-1,0,119,120))],True)
        filters = {'start':start.strftime('%Y-%m-%dT%H:%M'),'end':datetime.fromtimestamp(base+60,TZ).strftime('%Y-%m-%dT%H:%M')}
        self.assertEqual({w['id'] for w in self.ops.get('works',self.admin,filters)['items']},set(ids[1:3]))
        self.assertEqual(self.ops.get('summary',self.admin,filters)['works'],2)
        with patch.object(self.ops,'exporter',return_value=b'xlsx') as export:
            self.assertEqual(self.ops.get('export',self.admin,filters),b'xlsx')
        self.assertEqual({row['作品ID'] for row in export.call_args.args[1]},set(ids[1:3]))
        self.assertEqual(len(self.ops.works(self.admin,{'start':start.date().isoformat(),'end':start.date().isoformat()})),4)
        with self.assertRaisesRegex(ValueError,'截止时间'):
            self.ops.works(self.admin,{'start':datetime.fromtimestamp(base+120,TZ).strftime('%Y-%m-%dT%H:%M'),'end':filters['start']})
        with self.assertRaisesRegex(ValueError,'有效'):
            self.ops.works(self.admin,{'start':'bad-date'})
