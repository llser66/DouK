import hashlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import server as app
from operations import AccessError, now
import test_operations as fixtures


class PublicReportingTests(unittest.TestCase):
    setUp = fixtures.OperationsTests.setUp
    tearDown = fixtures.OperationsTests.tearDown
    raw = fixtures.OperationsTests.raw
    fake_inspect = fixtures.OperationsTests.fake_inspect

    def receive(self, **changes):
        verified = changes.pop('verified',False)
        data = {'employee_id': self.employee_id, 'links': 'https://www.douyin.com/video/1234567890123',
                'request_id': 'request-valid-test-000001', **changes}
        return self.ops.receive_report(data, '192.168.2.10',{'sec_uid':self.binding['sec_uid']} if verified else None)

    def process(self, raw=None):
        with patch.object(app, 'upstream', return_value={'data': raw or self.raw()}) as request:
            self.assertTrue(self.ops.process_report_due())
        self.assertEqual(request.call_args.args[0], '/douyin/detail')
        return request

    def test_roster_department_import_is_transactional_and_requires_no_password(self):
        result = self.ops.import_employees({'rows': [
            {'name': '张三', 'employee_code': 'Y001', 'department': '运营部', 'group': '内容一组'},
            {'name': '张三', 'employee_code': 'Y002', 'department': '运营部', 'group': '内容一组'},
        ]}, self.admin)
        self.assertEqual(result['count'], 2)
        directory = self.ops.public_directory()
        self.assertEqual(len(directory['departments']), 1)
        self.assertTrue(all('password' not in e and 'username' not in e for e in directory['employees']))
        people = self.ops.roster_people()
        self.assertEqual(people['groups'][0]['department_id'], directory['departments'][0]['id'])
        added = self.ops.query('SELECT * FROM ops_users WHERE id=?', (result['ids'][0],), one=True)
        self.assertEqual(added['password'], '!')
        with self.assertRaises(AccessError):
            self.ops.login({'username': 'employee', 'password': 'Employee-password-8394'})
        before = len(self.ops.roster_people()['users'])
        with self.assertRaises(ValueError):
            self.ops.import_employees({'rows': [
                {'name': '有效行', 'employee_code': 'Y003', 'department': '新部门', 'group': '新小组'},
                {'name': '', 'employee_code': 'Y004', 'department': '新部门', 'group': '新小组'},
            ]}, self.admin)
        self.assertEqual(len(self.ops.roster_people()['users']), before)
        self.assertIsNone(self.ops.query("SELECT * FROM ops_departments WHERE name='新部门'", one=True))

    def test_receive_is_durable_idempotent_and_receipt_is_a_private_capability(self):
        receipt = self.receive()
        token = receipt['receipt_token']
        self.assertEqual(receipt['items'][0]['status'], 'queued')
        self.assertEqual(self.receive(), receipt)
        self.assertEqual(len(self.ops.query('SELECT * FROM ops_report_items')), 1)
        stored = self.ops.query('SELECT * FROM ops_report_batches', one=True)
        self.assertEqual(stored['token_hash'], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps(stored))
        for bad in ('', '0' * 64, token[:30]):
            with self.assertRaises(AccessError):
                self.ops.report_receipt({'receipt_token': bad})
        public = json.dumps(receipt, ensure_ascii=False)
        for forbidden in ('employee_name', 'employee_id', 'payload', 'metrics', 'cookie', 'token_hash'):
            self.assertNotIn('"' + forbidden + '"', public)
        with self.assertRaises(ValueError):
            self.receive(links='https://www.douyin.com/video/1234567890124')

    def test_concurrent_double_clicks_do_not_duplicate_queue(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: self.receive(), range(8)))
        self.assertEqual(len({r['receipt_token'] for r in receipts}), 1)
        self.assertEqual(len(self.ops.query('SELECT * FROM ops_report_items')), 1)

    def test_batch_accepts_full_share_text_but_rejects_non_platform_urls_and_excess(self):
        receipt = self.receive(links='分享文案 https://v.douyin.com/abcd1234/ 复制此链接打开抖音\nhttps://www.douyin.com/video/1234567890124')
        self.assertEqual(len(receipt['items']), 2)
        with self.assertRaises(ValueError):
            self.receive(links='http://127.0.0.1/private')
        with self.assertRaises(ValueError):
            self.receive(links='https://douyin.com.evil.example/video/1234567890123')
        with self.assertRaises(ValueError):
            self.receive(links='https://www.douyin.com/user/MS4w-account')
        with self.assertRaises(ValueError):
            self.receive(links='\n'.join(str(1234567890000 + n) for n in range(21)))

    def test_known_account_files_once_and_keeps_first_received_time_after_delay(self):
        received_at = now()
        with patch('public_reports.tick', return_value=received_at):
            receipt = self.receive(verified=True)
        raw = self.raw(create_time=received_at - 3600)
        with patch('public_reports.tick', return_value=received_at + 86400 * 2):
            self.process(raw)
        result = self.ops.report_receipt({'receipt_token': receipt['receipt_token']})
        self.assertEqual(result['items'][0]['status'], 'submitted')
        submission = self.ops.query('SELECT * FROM ops_submissions', one=True)
        self.assertEqual(submission['submitted_at'], received_at)
        self.assertEqual(submission['late'], 0)
        duplicate = self.receive(request_id='request-valid-test-000002')
        self.process(raw)
        self.assertEqual(self.ops.report_receipt(duplicate)['items'][0]['status'], 'duplicate')
        self.assertEqual(len(self.ops.query('SELECT * FROM ops_submissions')), 1)

    def test_unbound_author_requires_explicit_admin_approval_and_preserves_claim(self):
        receipt = self.receive()
        self.process(self.raw(author={'sec_uid': 'MS4w-new-unbound-author', 'nickname': '新账号'}))
        item = self.ops.report_receipt(receipt)['items'][0]
        self.assertEqual(item['status'], 'review')
        self.assertEqual(self.ops.query('SELECT * FROM ops_submissions'), [])
        with self.assertRaises(AccessError):
            self.ops.review_reports({'ids': [item['id']], 'action': 'approve'}, self.employee)
        self.ops.review_reports({'ids': [item['id']], 'action': 'approve'}, self.admin)
        self.assertEqual(self.ops.report_receipt(receipt)['items'][0]['status'], 'submitted')
        self.assertEqual(self.ops.query('SELECT * FROM ops_works', one=True)['owner_id'], self.employee_id)

    def test_conflicting_claim_does_not_steal_credit_and_admin_can_confirm_delegate(self):
        receipt = self.receive(employee_id=self.other_id)
        self.process()
        item = self.ops.report_receipt(receipt)['items'][0]
        self.assertEqual(item['status'], 'review')
        with self.assertRaises(ValueError):
            self.ops.review_reports({'ids': [item['id']], 'action': 'approve'}, self.admin)
        self.ops.review_reports({'ids': [item['id']], 'action': 'approve', 'owner_id': self.employee_id}, self.admin)
        self.assertEqual(self.ops.query('SELECT * FROM ops_works', one=True)['owner_id'], self.employee_id)
        self.assertEqual(self.ops.query('SELECT * FROM ops_submissions', one=True)['user_id'], self.other_id)
        self.assertEqual(self.ops.get('reporting', self.admin, {})['counts']['submitted'], 1)

    def test_failure_preserves_receipt_redacts_credentials_and_does_not_create_missing_work(self):
        self.ops.save_settings({'retry_limit': 0}, self.admin)
        receipt = self.receive()
        with patch.object(app, 'upstream', side_effect=ValueError('Cookie=session-secret-private')):
            self.ops.process_report_due()
        result = self.ops.report_receipt(receipt)
        self.assertEqual(result['items'][0]['status'], 'failed')
        self.assertNotIn('session-secret-private', json.dumps(result))
        self.assertEqual(self.ops.query('SELECT * FROM ops_works'), [])
        self.assertEqual(self.ops.query('SELECT * FROM ops_submissions'), [])
        self.ops.review_reports({'ids': [result['items'][0]['id']], 'action': 'retry'}, self.admin)
        self.assertEqual(self.ops.report_receipt(receipt)['items'][0]['status'], 'queued')

    def test_historical_group_and_department_stay_fixed_after_employee_transfer(self):
        imported = self.ops.import_employees({'rows': [{'name': '原名', 'employee_code': 'H001', 'department': '原部门', 'group': '原小组'}]}, self.admin)
        employee_id = imported['ids'][0]
        self.ops.execute('UPDATE ops_bindings SET owner_id=? WHERE id=?', (employee_id, self.binding_id))
        receipt = self.receive(employee_id=employee_id,verified=True)
        self.process()
        old = self.ops.works(self.admin, {})[0]
        other_dept = self.ops.save_department({'name': '新部门'}, self.admin)['id']
        other_group = self.ops.save_group({'name': '新小组', 'department_id': other_dept}, self.admin)['id']
        self.ops.save_employee({'id': employee_id, 'name': '新名', 'group_id': other_group}, self.admin)
        current = self.ops.works(self.admin, {'department': old['department_id']})[0]
        self.assertEqual(current['employee_name'], '原名')
        self.assertEqual(current['group_name'], '原小组')
        self.assertEqual(current['department_name'], '原部门')
        self.assertEqual(current['submitted_at'], receipt['items'][0]['received_at'])

    def test_identical_group_labels_are_scoped_to_different_departments(self):
        self.ops.import_employees({'rows': [
            {'name': '员工一', 'employee_code': 'D001', 'department': '部门甲', 'group': '一组'},
            {'name': '员工二', 'employee_code': 'D002', 'department': '部门乙', 'group': '一组'},
        ]}, self.admin)
        directory = self.ops.public_directory()
        groups = [g for g in directory['groups'] if g['name'] == '一组']
        self.assertEqual(len(groups), 2)
        self.assertNotEqual(groups[0]['department_id'], groups[1]['department_id'])
        with self.assertRaises(ValueError):
            self.ops.save_group({'name': '一组', 'department_id': groups[0]['department_id']}, self.admin)

    def test_schema_is_repeatable_and_old_employee_sessions_stop_working(self):
        self.fake_inspect([self.raw()], False)
        receipt = self.receive()
        self.ops.execute('INSERT INTO ops_sessions VALUES(?,?,?)',
                         (hashlib.sha256(b'old-employee-session').hexdigest(), self.employee_id, now() + 3600))
        self.assertIsNone(self.ops.authenticate({'Cookie': 'douk_session=old-employee-session'}))
        self.ops.schema()
        self.ops.schema()
        self.assertEqual(self.ops.report_receipt(receipt)['items'][0]['status'], 'queued')
        row = self.ops.works(self.admin, {})[0]
        self.assertEqual(row['state'], 'confirm')
        self.assertEqual(row['id'], self.raw()['aweme_id'])
        self.assertEqual(self.ops.query('SELECT id FROM ops_users WHERE id=?', (self.employee_id,), one=True)['id'], self.employee_id)


if __name__ == '__main__':
    unittest.main()
