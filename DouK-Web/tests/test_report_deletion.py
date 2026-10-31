"""Physical reporting deletion, related-data scope, queue safety and old-state cleanup."""
import unittest
from unittest.mock import patch
import server as app
from operations import AccessError,now
import test_public_reports as fixtures

class ReportDeletionTests(unittest.TestCase):
    setUp=fixtures.PublicReportingTests.setUp
    tearDown=fixtures.PublicReportingTests.tearDown
    receive=fixtures.PublicReportingTests.receive
    process=fixtures.PublicReportingTests.process
    raw=fixtures.PublicReportingTests.raw

    def remove(self,ids,confirmed=True):
        return self.ops.post('reporting/delete',{'ids':ids,'confirmed':confirmed,'permanent':True},self.admin)

    def test_deleting_duplicate_clears_original_work_and_other_reports_for_that_work_only(self):
        first=self.receive(verified=True);self.process()
        duplicate=self.receive(request_id='duplicate-for-deletion-0001',verified=True);self.process()
        self.assertEqual(self.ops.report_receipt(duplicate)['items'][0]['status'],'duplicate')
        unrelated=self.receive(request_id='unrelated-for-deletion-0001',links='1234567890124',verified=True)
        self.process(self.raw(work_id='1234567890124'))
        self.ops.report_note({**first,'item_id':first['items'][0]['id'],'note':'已删除备注'})
        result=self.remove([duplicate['items'][0]['id']])
        self.assertEqual(result['reports'],2)
        self.assertEqual(result['works'],1)
        for receipt in (first,duplicate):
            with self.assertRaises(AccessError):self.ops.report_receipt(receipt)
        self.assertEqual(self.ops.report_receipt(unrelated)['items'][0]['status'],'submitted')
        self.assertEqual([r['id'] for r in self.ops.works(self.admin,{})],['1234567890124'])
        self.assertFalse(self.ops.query('SELECT * FROM ops_report_notes'))
        self.assertEqual(len(self.ops.query('SELECT * FROM ops_report_batches')),1)
        self.assertEqual(len(self.ops.query('SELECT * FROM ops_report_claims')),1)
        new=self.receive(request_id='new-after-physical-delete-0001',verified=True);self.process()
        self.assertEqual(self.ops.report_receipt(new)['items'][0]['status'],'submitted')

    def test_delete_queued_report_stops_processing_and_releases_request_id(self):
        receipt=self.receive();self.remove([receipt['items'][0]['id']])
        with patch.object(app,'upstream',side_effect=AssertionError('Deleted item must not call platform')):
            self.assertFalse(self.ops.process_report_due())
        self.assertFalse(self.ops.query('SELECT * FROM ops_report_items'))
        with self.assertRaises(AccessError):self.ops.report_receipt(receipt)
        fresh=self.receive()
        self.assertNotEqual(fresh['receipt_token'],receipt['receipt_token'])
        self.assertEqual(fresh['items'][0]['status'],'queued')

    def test_partial_batch_keeps_other_work_and_private_receipt_valid(self):
        receipt=self.receive(links='1234567890123\n1234567890124')
        removed,retained=receipt['items']
        self.remove([removed['id']])
        latest=self.ops.report_receipt(receipt)
        self.assertEqual([r['id'] for r in latest['items']],[retained['id']])
        self.assertEqual(len(self.ops.query('SELECT * FROM ops_report_claims')),1)

    def test_confirmed_but_stale_soft_delete_client_must_refresh(self):
        receipt=self.receive()
        with self.assertRaisesRegex(ValueError,'永久删除'):
            self.ops.post('reporting/delete',{'ids':[receipt['items'][0]['id']],'confirmed':True},self.admin)
        self.assertEqual(len(self.ops.query('SELECT * FROM ops_report_items')),1)

    def test_unparsed_canonical_link_for_same_work_is_deleted_before_worker_can_refile(self):
        first=self.receive(verified=True);self.process()
        pending=self.receive(request_id='same-work-new-url-0001',links='https://www.douyin.com/video/1234567890123',verified=True)
        self.ops.post('works/delete',{'id':'1234567890123','confirmed':True,'permanent':True},self.admin)
        self.assertEqual(self.ops.query('SELECT * FROM ops_report_items'),[])
        self.assertEqual(self.ops.query('SELECT * FROM ops_submissions'),[])

    def test_confirmation_authorization_and_processing_guard_are_atomic(self):
        first=self.receive();second=self.receive(request_id='delete-processing-report-0001',links='1234567890124')
        ids=[first['items'][0]['id'],second['items'][0]['id']]
        with self.assertRaisesRegex(ValueError,'确认'):self.remove(ids,False)
        for route in ('reporting/delete','reporting/restore'):
            with self.assertRaises(AccessError):self.ops.post(route,{'ids':ids,'confirmed':True,'permanent':True},self.employee)
        for invalid in ([],['missing'],[ids[0],'missing'],[None],[str(i) for i in range(101)]):
            with self.assertRaises(ValueError):self.remove(invalid)
        self.ops.execute("UPDATE ops_report_items SET status='processing' WHERE id=?",(ids[1],))
        with self.assertRaisesRegex(ValueError,'正在识别'):self.remove(ids)
        self.assertEqual(len(self.ops.query('SELECT * FROM ops_report_items')),2)
        with self.assertRaisesRegex(ValueError,'不能从页面恢复'):
            self.ops.post('reporting/restore',{'ids':ids},self.admin)

    def test_legacy_cleanup_clears_old_tombstones_submissions_and_retains_all_logs(self):
        original=self.receive(verified=True);self.process()
        duplicate=self.receive(request_id='legacy-duplicate-report-0001',verified=True);self.process()
        work_id=self.raw()['aweme_id']
        self.ops.execute('INSERT INTO ops_work_deletions VALUES(?,?,?)',(work_id,now(),self.admin['id']))
        self.ops.execute('INSERT INTO ops_report_deletions VALUES(?,?,?)',(original['items'][0]['id'],now(),self.admin['id']))
        before=self.ops.query('SELECT COUNT(*) n FROM logs',one=True)['n']
        result=self.ops.post('deleted/purge',{'confirmed':True,'permanent':True},self.admin)
        self.assertEqual((result['works'],result['reports']),(1,2))
        for table in ('ops_works','ops_submissions','ops_work_deletions','ops_report_deletions','ops_report_items','ops_report_batches','ops_report_claims'):
            self.assertEqual(self.ops.query('SELECT * FROM '+table),[],table)
        self.assertGreater(self.ops.query('SELECT COUNT(*) n FROM logs',one=True)['n'],before)
        self.assertTrue(self.ops.query('SELECT * FROM ops_users'))
        self.assertTrue(self.ops.query('SELECT * FROM ops_bindings'))
        again=self.ops.post('deleted/purge',{'confirmed':True,'permanent':True},self.admin)
        self.assertEqual((again['works'],again['reports']),(0,0))

    def test_deleted_report_cannot_recreate_work_from_late_platform_result(self):
        receipt=self.receive(verified=True)
        def detail(*args,**kwargs):
            self.ops.execute('DELETE FROM ops_report_items WHERE id=?',(receipt['items'][0]['id'],))
            return self.ops.normalize(self.raw())
        with patch.object(self.ops,'detail',side_effect=detail):self.ops.process_report_due()
        self.assertFalse(self.ops.query('SELECT * FROM ops_works'))
        self.assertFalse(self.ops.query('SELECT * FROM ops_submissions'))

if __name__=='__main__':unittest.main()
