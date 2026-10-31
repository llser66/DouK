"""Window continuity, conservative evidence and queue options; no platform calls."""
from datetime import datetime
import json
from unittest.mock import patch
import unittest

import server as app
from operations import AccessError, TZ, now
import test_operations as fixtures


class IncrementalInspectionTests(unittest.TestCase):
    setUp = fixtures.OperationsTests.setUp
    tearDown = fixtures.OperationsTests.tearDown
    raw = fixtures.OperationsTests.raw

    def setUp(self):
        fixtures.OperationsTests.setUp(self)
        self.clock = now()
        self.begin = self.clock - 120 * 86400
        self.ops.execute('UPDATE ops_bindings SET monitor_start=? WHERE id=?', (self.begin, self.binding_id))

    def inspect(self, rows, when=None, complete=True, full_scan=False):
        with patch('operations.now', return_value=when or self.clock), patch.object(app, 'upstream', return_value={
                'data': rows, 'params': {'workspace_check': {'complete': complete}}}) as request:
            result = self.ops.inspect(self.binding_id, full_scan=full_scan)
        return result, request.call_args.args[1]

    def progress(self):
        return self.ops.query('SELECT * FROM ops_inspection_progress WHERE binding_id=?', (self.binding_id,), one=True)

    def test_initial_scan_uses_monitor_start_and_incremental_scan_keeps_old_evidence(self):
        old = self.raw('1234567891001', create_time=self.clock - 100 * 86400)
        self.inspect([old])
        result, payload = self.inspect([self.raw('1234567891002')], when=self.clock + 86400)
        self.assertIn('增量巡检', result[1])
        self.assertEqual(payload['earliest'], datetime.fromtimestamp(self.clock - 3 * 86400, TZ).strftime('%Y/%m/%d'))
        rows = self.ops.works(self.admin, {})
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['verified'] for row in rows))
        self.assertEqual(self.progress()['full_checked_at'], self.clock)
        self.assertEqual(self.progress()['completed_through'], self.clock + 86400)

    def test_overlap_finds_delayed_posts_and_missing_overlap_work_needs_confirmation(self):
        recent = self.raw('1234567891001', create_time=self.clock - 3600)
        self.inspect([recent])
        delayed = self.raw('1234567891002', create_time=self.clock - 2 * 86400)
        self.inspect([delayed], when=self.clock + 86400)
        rows = {row['id']: row for row in self.ops.works(self.admin, {})}
        self.assertTrue(rows[delayed['aweme_id']]['verified'])
        self.assertFalse(rows[recent['aweme_id']]['verified'])
        self.assertEqual(rows[recent['aweme_id']]['state'], 'confirm')

    def test_incomplete_window_or_network_failure_does_not_advance_checkpoint(self):
        self.inspect([self.raw()])
        before = self.progress()
        self.inspect([], when=self.clock + 86400, complete=False)
        self.assertEqual(self.progress(), before)
        with patch('operations.now', return_value=self.clock + 2 * 86400), patch.object(app, 'upstream', side_effect=ValueError('offline')):
            with self.assertRaises(ValueError):
                self.ops.inspect(self.binding_id)
        self.assertEqual(self.progress(), before)
        _, payload = self.inspect([self.raw()], when=self.clock + 3 * 86400)
        self.assertEqual(payload['earliest'], datetime.fromtimestamp(self.clock - 3 * 86400, TZ).strftime('%Y/%m/%d'))

    def test_periodic_and_manual_history_scans_reconcile_old_evidence(self):
        old = self.raw(create_time=self.clock - 100 * 86400)
        self.inspect([old])
        result, payload = self.inspect([], when=self.clock + 30 * 86400)
        self.assertIn('历史补查', result[1])
        self.assertEqual(payload['earliest'], datetime.fromtimestamp(self.begin, TZ).strftime('%Y/%m/%d'))
        self.assertFalse(self.ops.works(self.admin, {})[0]['verified'])
        self.assertEqual(self.progress()['full_checked_at'], self.clock + 30 * 86400)
        result, _ = self.inspect([old], when=self.clock + 30 * 86400 + 60, full_scan=True)
        self.assertIn('历史补查', result[1])
        self.assertTrue(self.ops.works(self.admin, {})[0]['verified'])

    def test_scope_change_and_missing_checkpoint_force_history_scan(self):
        self.inspect([self.raw()])
        binding = self.ops.query('SELECT * FROM ops_bindings WHERE id=?', (self.binding_id,), one=True)
        self.ops.save_binding({**binding, 'enabled': True, 'monitor_start': self.begin - 86400}, self.admin)
        self.assertIsNone(self.progress())
        _, payload = self.inspect([])
        self.assertEqual(payload['earliest'], datetime.fromtimestamp(self.begin - 86400, TZ).strftime('%Y/%m/%d'))
        self.ops.execute('UPDATE ops_inspection_progress SET owner_id=? WHERE binding_id=?', ('old-owner', self.binding_id))
        result, _ = self.inspect([])
        self.assertIn('历史补查', result[1])

    def test_checkpoint_uses_request_start_and_survives_schema_reopen(self):
        with patch('operations.now', return_value=self.clock) as clock:
            def upstream(*args):
                clock.return_value = self.clock + 3600
                return {'data': [], 'params': {'workspace_check': {'complete': True}}}
            with patch.object(app, 'upstream', side_effect=upstream):
                self.ops.inspect(self.binding_id)
        self.assertEqual(self.progress()['completed_through'], self.clock)
        self.ops.schema()
        self.assertEqual(self.progress()['completed_through'], self.clock)

    def test_returned_author_mismatch_keeps_checkpoint_and_conservative_state(self):
        self.inspect([])
        before = self.progress()
        result, _ = self.inspect([self.raw(author={'sec_uid': 'wrong-author'})], when=self.clock + 86400)
        self.assertEqual(result[0], 'incomplete')
        self.assertEqual(self.progress(), before)

    def test_clock_rollback_requires_full_scan(self):
        self.inspect([])
        result, _ = self.inspect([], when=self.clock - 60)
        self.assertIn('历史补查', result[1])

    def test_legacy_full_scan_option_is_preserved_but_queue_no_longer_runs_it(self):
        self.inspect([])
        with self.assertRaises(AccessError):
            self.ops.enqueue('inspect', self.binding_id, self.employee, full_scan=True)
        for value in ('true', 1):
            with self.assertRaises(ValueError):
                self.ops.post('jobs', {'kind': 'inspect', 'target': self.binding_id, 'full_scan': value}, self.admin)
        job = self.ops.enqueue('inspect', self.binding_id, self.admin)
        self.assertEqual(job['id'], self.ops.enqueue('inspect', self.binding_id, self.admin, full_scan=True)['id'])
        with patch.object(self.ops, 'inspect', return_value=('complete', '历史补查完成')) as inspect:
            self.ops.run_due()
        inspect.assert_not_called()
        saved = self.ops.query('SELECT * FROM ops_jobs WHERE id=?', (job['id'],), one=True)
        self.assertEqual(saved['status'], 'cancelled')

    def test_running_job_cannot_silently_consume_full_scan_request_and_disabled_rejected(self):
        job = self.ops.enqueue('inspect', self.binding_id, self.admin)
        self.ops.execute("UPDATE ops_jobs SET status='running' WHERE id=?", (job['id'],))
        with self.assertRaisesRegex(ValueError, '正在运行'):
            self.ops.enqueue('inspect', self.binding_id, self.admin, full_scan=True)
        self.ops.execute('UPDATE ops_bindings SET enabled=0 WHERE id=?', (self.binding_id,))
        with self.assertRaisesRegex(ValueError, '停用'):
            self.ops.enqueue('inspect', self.binding_id, self.admin)

    def test_config_defaults_and_bounds(self):
        self.assertEqual(self.ops.settings()['inspection_overlap_days'], 3)
        self.assertEqual(self.ops.settings()['full_inspect_days'], 30)
        self.ops.save_settings({'inspection_overlap_days': 7, 'full_inspect_days': 60}, self.admin)
        self.assertEqual(self.ops.settings()['full_inspect_days'], 60)
        for changes in ({'inspection_overlap_days': 0}, {'full_inspect_days': 1}):
            with self.assertRaises(ValueError):
                self.ops.save_settings(changes, self.admin)

    def test_known_report_missing_from_list_blocks_success_even_when_rejected(self):
        self.inspect([self.raw()])
        before = self.progress()
        known = self.ops.normalize(self.raw('1234567891009', create_time=self.clock - 3600))
        self.ops.execute('''INSERT INTO ops_report_items
            (id,batch_id,employee_id,group_id,link,received_at,updated,status,work_id,payload)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            ('known-report','batch',self.employee_id,'','link',self.clock,self.clock,
             'rejected',known['id'],json.dumps(known)))
        result, _ = self.inspect([], when=self.clock + 86400)
        self.assertEqual(result[0], 'incomplete')
        self.assertIn('1 个同作者作品未出现在发布列表', result[1])
        self.assertEqual(self.progress(), before)
        self.assertIsNone(self.ops.query('SELECT id FROM ops_works WHERE id=?',(known['id'],),one=True))
        self.ops.execute('INSERT INTO ops_report_deletions VALUES(?,?,?)',('known-report',self.clock,self.admin['id']))
        result, _ = self.inspect([], when=self.clock + 86400)
        self.assertEqual(result[0], 'complete')

    def test_raw_count_reports_filtering_and_list_recency(self):
        with patch('operations.now', return_value=self.clock), patch.object(app, 'upstream', return_value={
            'data': [], 'params': {'workspace_check': {'complete': True, 'raw_count':18,
            'newest_published':self.begin - 3600}}}):
            status, message = self.ops.inspect(self.binding_id)
        self.assertEqual(status, 'complete')
        self.assertIn('已读取 18 个发布作品，监测期内 0 个', message)
        self.assertIn('上游日期筛选后 0 个', message)
        self.assertIn('列表最新作品发布于', message)


if __name__ == '__main__':
    unittest.main()
