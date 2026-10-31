from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from operations import AccessError, TZ, now
from work_targets import WorkTargets
import test_operations as fixtures


class WorkTargetTests(unittest.TestCase):
    setUp = fixtures.OperationsTests.setUp
    tearDown = fixtures.OperationsTests.tearDown
    raw = fixtures.OperationsTests.raw
    fake_inspect = fixtures.OperationsTests.fake_inspect

    def target(self, **changes):
        day = datetime.fromtimestamp(now(), TZ).date().isoformat()
        self.targets = WorkTargets(self.ops)
        return self.targets.save({'scope': 'employee', 'target_id': self.employee_id,
                                  'start': day, 'end': day, 'quantity': 3, **changes}, self.admin)['id']

    def result(self):
        return self.targets.list(self.admin, {})['items'][0]

    def published_today(self):
        return int(datetime.combine(datetime.fromtimestamp(now(), TZ).date(), datetime.min.time(), TZ).timestamp()) + 1

    def test_target_requires_admin_and_rejects_invalid_quantity_and_overlap(self):
        target_id = self.target()
        with self.assertRaises(AccessError):
            self.targets.list(self.employee, {})
        with self.assertRaises(AccessError):
            self.targets.save({}, self.employee)
        with self.assertRaises(AccessError):
            self.targets.exempt({}, self.employee)
        with self.assertRaisesRegex(ValueError, '重叠'):
            self.target()
        for quantity in (None, True, 0, -1, 1.5, 'abc', 100001):
            with self.assertRaises(ValueError):
                self.target(id=target_id, quantity=quantity)

    def test_actual_uses_publication_time_and_counts_unsubmitted_works(self):
        self.target()
        yesterday = self.published_today() - 86400
        self.fake_inspect([self.raw(create_time=self.published_today()), self.raw('1234567890124', create_time=yesterday)], True)
        result = self.result()
        self.assertEqual(result['actual'], 1)
        self.assertTrue(result['verified'])
        self.assertEqual(result['shortfall'], 2)
        self.assertEqual(result['progress'], 33.3)
        self.assertEqual(self.ops.query('SELECT * FROM ops_submissions'), [])

    def test_incomplete_stale_or_late_monitoring_does_not_prove_zero_or_shortfall(self):
        self.target()
        self.fake_inspect([], False)
        result = self.result()
        self.assertFalse(result['verified'])
        self.assertIsNone(result['shortfall'])
        self.assertIsNone(result['complete'])
        self.fake_inspect([], True)
        self.ops.execute('UPDATE ops_bindings SET last_check=?', (now() - 86400,))
        self.assertFalse(self.result()['verified'])
        self.ops.execute('UPDATE ops_bindings SET last_check=?,monitor_start=?', (now(), now()))
        self.assertFalse(self.result()['verified'])
        self.assertIsNone(self.result()['shortfall'])

    def test_every_active_account_must_cover_period_and_complete(self):
        self.target()
        self.fake_inspect([], True)
        second_id = self.ops.save_binding({'homepage': 'https://www.douyin.com/user/MS4w-second-account', 'nickname': '第二账号',
                                           'owner_id': self.employee_id, 'monitor_start': now() - 86400 * 3}, self.admin)['id']
        self.assertFalse(self.result()['verified'])
        self.ops.execute("UPDATE ops_bindings SET check_state='complete',last_check=? WHERE id=?", (now(), second_id))
        self.assertTrue(self.result()['verified'])
        # Disabling an account midway through the target period must not make the remaining account prove zero.
        self.ops.save_binding({'id': second_id, 'homepage': 'https://www.douyin.com/user/MS4w-second-account', 'nickname': '第二账号',
                               'owner_id': self.employee_id, 'monitor_start': now() - 86400 * 3, 'enabled': False}, self.admin)
        self.assertFalse(self.result()['verified'])
        self.assertIsNone(self.result()['shortfall'])

    def test_exemptions_have_reasons_are_bounded_and_preserve_original_quota(self):
        target_id = self.target()
        day = self.result()['start']
        with self.assertRaises(ValueError):
            self.targets.exempt({'id': target_id, 'date': day, 'quantity': 1, 'reason': ''}, self.admin)
        self.targets.exempt({'id': target_id, 'date': day, 'quantity': 2, 'reason': '请假调整'}, self.admin)
        result = self.result()
        self.assertEqual(result['quantity'], 3)
        self.assertEqual(result['required'], 1)
        self.assertEqual(result['exempt_quantity'], 2)
        self.assertEqual(result['exemptions'][0]['reason'], '请假调整')
        with self.assertRaises(ValueError):
            self.targets.exempt({'id': target_id, 'date': day, 'quantity': 2, 'reason': '超量减免'}, self.admin)
        with self.assertRaisesRegex(ValueError, '已有减免'):
            self.target(id=target_id, quantity=4)
        self.targets.exempt({'id': target_id, 'date': day, 'quantity': 1, 'reason': '剩余减免'}, self.admin)
        self.assertTrue(self.result()['complete'])
        self.assertEqual(self.result()['required'], 0)

    def test_group_label_and_historical_work_remain_correct_after_transfer(self):
        imported = self.ops.import_employees({'rows': [
            {'name': '员工甲原名', 'employee_code': 'G001', 'department': '运营甲部', 'group': '一组'},
            {'name': '员工乙', 'employee_code': 'G002', 'department': '运营乙部', 'group': '一组'},
        ]}, self.admin)
        employee = self.ops.query('SELECT * FROM ops_users WHERE id=?', (imported['ids'][0],), one=True)
        other = self.ops.query('SELECT * FROM ops_users WHERE id=?', (imported['ids'][1],), one=True)
        self.ops.execute('UPDATE ops_bindings SET owner_id=? WHERE id=?', (employee['id'], self.binding_id))
        self.fake_inspect([self.raw(create_time=self.published_today())], True)
        self.target(scope='group', target_id=employee['group_id'])
        self.assertEqual(self.result()['name'], '一组')
        self.assertEqual(self.result()['actual'], 1)
        self.ops.save_employee({'id': employee['id'], 'group_id': other['group_id']}, self.admin)
        result = self.result()
        self.assertEqual(result['actual'], 1)
        self.assertFalse(result['verified'])
        self.assertIsNone(result['shortfall'])

    def test_reassigned_account_does_not_rewrite_historical_owner_goal(self):
        self.target()
        self.fake_inspect([self.raw(create_time=self.published_today())], True)
        self.ops.save_binding({'id': self.binding_id, 'homepage': self.binding['homepage'], 'nickname': self.binding['nickname'],
                               'owner_id': self.other_id, 'monitor_start': now() - 86400 * 3}, self.admin)
        self.ops.execute("UPDATE ops_bindings SET check_state='complete',last_check=?", (now(),))
        result = self.result()
        self.assertEqual(result['actual'], 1)
        self.assertFalse(result['verified'])
        self.assertIsNone(result['shortfall'])

    def test_future_period_never_marks_work_unfinished_before_it_starts(self):
        tomorrow = (datetime.fromtimestamp(now(), TZ).date() + timedelta(days=1)).isoformat()
        self.target(start=tomorrow, end=tomorrow)
        self.fake_inspect([], True)
        result = self.result()
        self.assertFalse(result['verified'])
        self.assertIsNone(result['shortfall'])
        self.assertFalse(result['period_ended'])

    def test_minute_range_includes_end_minute_and_excludes_surrounding_seconds(self):
        day = datetime.fromtimestamp(now(), TZ).date().isoformat()
        self.target(start=day+'T10:30', end=day+'T10:45')
        start = int(datetime.fromisoformat(day+'T10:30').replace(tzinfo=TZ).timestamp())
        seconds = (start-1, start, start+15*60+59, start+16*60)
        self.fake_inspect([self.raw(str(1234567890123+i),create_time=t) for i,t in enumerate(seconds)], True)
        self.assertEqual(self.result()['actual'], 2)
        self.assertEqual(self.result()['start_time'], day+'T10:30')
        self.assertEqual(self.result()['end_time'], day+'T10:45')
        with patch('work_targets.now', return_value=start+16*60-1):
            self.assertFalse(self.result()['period_ended'])
        with patch('work_targets.now', return_value=start+16*60):
            self.assertTrue(self.result()['period_ended'])

    def test_mixed_legacy_and_minute_ranges_detect_overlap_without_losing_whole_day(self):
        target_id = self.target()
        day = self.result()['start']
        self.assertEqual(self.result()['end_time'], day+'T23:59')
        with self.assertRaisesRegex(ValueError, '重叠'):
            self.target(start=day+'T23:59', end=day+'T23:59')
        tomorrow = (datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat()
        self.target(start=tomorrow+'T00:00', end=tomorrow+'T00:00')
        self.assertEqual(len(self.targets.list(self.admin,{})['items']), 2)

    def test_adjacent_minute_targets_allowed_but_same_minute_overlaps(self):
        day = datetime.fromtimestamp(now(), TZ).date().isoformat()
        self.target(start=day+'T09:00', end=day+'T09:59')
        self.target(start=day+'T10:00', end=day+'T10:59')
        with self.assertRaisesRegex(ValueError, '重叠'):
            self.target(start=day+'T09:59', end=day+'T10:00')
        with self.assertRaisesRegex(ValueError, '重叠'):
            self.target(start=day, end=day)

    def test_minute_exemptions_cover_start_and_end_dates_and_keep_frozen_period(self):
        day = datetime.fromtimestamp(now(), TZ).date().isoformat()
        tomorrow = (datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat()
        target_id = self.target(start=day+'T22:30', end=tomorrow+'T01:15')
        for date in (day,tomorrow):
            self.targets.exempt({'id':target_id,'date':date,'quantity':1,'reason':'轮班请假'},self.admin)
        self.assertEqual(self.result()['required'], 1)
        with self.assertRaisesRegex(ValueError,'周期内'):
            self.targets.exempt({'id':target_id,'date':'2000-01-01','quantity':1,'reason':'日期不符'},self.admin)
        with self.assertRaisesRegex(ValueError,'已有减免'):
            self.target(id=target_id,start=day+'T22:31',end=tomorrow+'T01:15')

    def test_legacy_exempt_target_can_save_equivalent_minute_bounds(self):
        target_id = self.target()
        day = self.result()['start']
        self.targets.exempt({'id':target_id,'date':day,'quantity':1,'reason':'请假调整'},self.admin)
        self.target(id=target_id,start=day+'T00:00',end=day+'T23:59',note='只修改说明')
        self.assertEqual(self.result()['exempt_quantity'], 1)
        self.assertEqual(self.result()['note'], '只修改说明')

    def test_minute_validation_rejects_ambiguous_zone_seconds_and_reverse_range(self):
        for start,end in [('2026-10-30T10:01','2026-10-30T10:00'),
                          ('2026-10-30T10:00:30','2026-10-30T11:00'),
                          ('2026-10-30T10:00+08:00','2026-10-30T11:00'),
                          ('2026-02-30T10:00','2026-10-30T11:00')]:
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.target(start=start,end=end)


if __name__ == '__main__':
    unittest.main()
