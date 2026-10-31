import json
import threading
import unittest
from unittest.mock import patch

import server as app
import test_workspace as workspace


class CommentPoolTests(unittest.TestCase):
    setUp = workspace.WorkspaceTests.setUp
    tearDown = workspace.WorkspaceTests.tearDown
    wait_task = workspace.WorkspaceTests.wait_task

    def account(self, name):
        account = app.create_account({'name': name})
        app.update_account(account['id'], {'cookie': f'sessionid=private-{name}'})
        account = app.get_account(account['id'])
        # Zero cooldown is an isolated test fixture; the public API enforces >=5s.
        app.save_account(account, cooldown_seconds=0)
        return account

    def payload(self, **updates):
        return {'action': 'collect', 'mode': 'comment', 'platform': 'douyin', 'links': '123456',
                'account_mode': 'pool', 'pages': 3, 'batch_size': 2, 'collect_all': False, **updates}

    def test_round_robin_uses_next_cursor_across_three_batches(self):
        a, b = self.account('A'), self.account('B')
        calls = []
        def page(detail, job, req, config):
            calls.append((job['cursor'], config['cookie']))
            cursor = {0: 11, 11: 24, 24: 40}[job['cursor']]
            return {'data': [{'cid': str(cursor), 'text': 'comment'}], 'cursor': cursor, 'has_more': cursor != 40}
        with patch.object(app, 'get_comment_page', side_effect=page):
            task = self.wait_task(app.create_task(self.payload()))
        self.assertEqual(task['status'], 'completed')
        self.assertEqual([c[0] for c in calls], [0, 11, 24])
        self.assertEqual([c[1] for c in calls], [a['cookie'], b['cookie'], a['cookie']])
        self.assertEqual(task['record_count'], 3)
        self.assertEqual(task['account_usage'][a['id']]['batches'], 2)
        self.assertNotIn('private-', json.dumps(app.public_task(task)))

    def test_collect_until_end_ignores_page_limit_and_deduplicates(self):
        self.account('A'); self.account('B')
        pages = [
            {'data': [{'cid': '1'}, {'cid': '2'}], 'cursor': 40, 'has_more': True},
            {'data': [{'cid': '2'}, {'cid': '3'}], 'cursor': 60, 'has_more': False},
        ]
        with patch.object(app, 'get_comment_page', side_effect=pages) as read:
            task = self.wait_task(app.create_task(self.payload(pages=1, collect_all=True)))
        self.assertEqual(read.call_count, 2)
        self.assertEqual(task['record_count'], 3)
        self.assertEqual(len(app.records(task['id'])), 3)

    def test_five_accounts_rotate_repeatedly_until_comment_end(self):
        accounts = [self.account(str(n)) for n in range(1, 6)]
        calls = []
        def page(detail, job, req, config):
            calls.append(config['cookie'])
            next_cursor = job['cursor']+1
            return {'data': [{'cid': str(next_cursor)}], 'cursor': next_cursor, 'has_more': next_cursor < 11}
        with patch.object(app, 'get_comment_page', side_effect=page):
            task = self.wait_task(app.create_task(self.payload(pages=1, collect_all=True)))
        self.assertEqual(calls, [accounts[n % 5]['cookie'] for n in range(11)])
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['record_count'], 11)
        self.assertEqual(len(task['account_usage']), 5)

    def test_page_limit_is_total_across_accounts(self):
        self.account('A'); self.account('B')
        with patch.object(app, 'get_comment_page', return_value={'data': [{'cid': '1'}], 'cursor': 20, 'has_more': True}) as read:
            task = self.wait_task(app.create_task(self.payload(pages=1)))
        self.assertEqual(read.call_count, 1)
        self.assertEqual(task['comment_pages'], 1)
        self.assertEqual(task['status'], 'completed')

    def test_paused_and_unlogged_accounts_are_not_used(self):
        a, b = self.account('A'), self.account('B')
        app.update_account(b['id'], {'paused': True})
        app.create_account({'name': 'Not logged in'})
        with patch.object(app, 'get_comment_page', return_value={'data': [{'cid': '1'}], 'cursor': 20, 'has_more': False}) as read:
            task = self.wait_task(app.create_task(self.payload()))
        self.assertEqual(read.call_args.args[3]['cookie'], a['cookie'])
        self.assertEqual(len(task['account_usage']), 1)

    def test_empty_pool_rejects_task_before_queueing(self):
        app.create_account({'name': 'Not logged in'})
        with self.assertRaisesRegex(ValueError, '没有已登录'):
            app.create_task(self.payload())
        self.assertEqual(app.all_tasks(), [])

    def test_failure_retains_cursor_and_retry_resumes_same_task(self):
        self.account('A'); self.account('B')
        pages = [{'data': [{'cid': '1'}], 'cursor': 20, 'has_more': True}, ValueError('Platform request failed')]
        with patch.object(app, 'get_comment_page', side_effect=pages):
            partial = self.wait_task(app.create_task(self.payload(pages=2)))
        self.assertEqual(partial['status'], 'partial')
        self.assertEqual(partial['record_count'], 1)
        self.assertEqual(partial['comment_state']['jobs'][0]['cursor'], 20)
        with patch.object(app, 'get_comment_page', return_value={'data': [{'cid': '2'}], 'cursor': 40, 'has_more': False}) as read:
            completed = self.wait_task(app.retry_comment_pool(partial['id']))
        self.assertEqual(read.call_args.args[1]['cursor'], 20)
        self.assertEqual(completed['id'], partial['id'])
        self.assertEqual(completed['record_count'], 2)
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(len(app.all_tasks()), 1)

    def test_replies_are_separate_batches_and_rotate_accounts(self):
        a, b = self.account('A'), self.account('B')
        seen = []
        def page(detail, job, req, config):
            seen.append((job.get('comment_id'), config['cookie']))
            row = {'cid': 'reply1'} if job.get('comment_id') else {'cid': 'parent1', 'reply_comment_total': 8}
            return {'data': [row], 'cursor': 20, 'has_more': False}
        with patch.object(app, 'get_comment_page', side_effect=page):
            task = self.wait_task(app.create_task(self.payload(reply=True, count_reply=3)))
        self.assertEqual(seen, [(None, a['cookie']), ('parent1', b['cookie'])])
        self.assertEqual(task['record_count'], 2)

    def test_cancel_during_cooldown_is_responsive(self):
        account = self.account('A')
        app.save_account(account, last_request=app.time.time(), cooldown_seconds=60)
        task = {'id': 'aabbccddeeff', 'record_count': 0, 'request': self.payload()}
        app.save_task(task)
        app.CANCEL[task['id']] = threading.Event()
        timer = threading.Timer(.03, app.CANCEL[task['id']].set)
        timer.start()
        try:
            with self.assertRaises(InterruptedError):
                app.take_pool_account(task, app.DEFAULTS)
            self.assertIn('账号池休息中', app.get_task(task['id'])['message'])
        finally:
            timer.join()

    def test_nonadvancing_cursor_is_rejected(self):
        with patch.object(app, 'upstream', return_value={'data': [{'cid': '1'}], 'cursor': 20, 'has_more': True}):
            with self.assertRaisesRegex(ValueError, '未推进'):
                app.get_comment_page('123', {'cursor': 20}, {'batch_size': 2, 'count_reply': 3}, app.DEFAULTS)

    def test_next_work_keeps_round_robin_order(self):
        a, b = self.account('A'), self.account('B')
        calls = []
        def page(detail, job, req, config):
            calls.append((detail, config['cookie']))
            return {'data': [{'cid': detail}], 'cursor': 20, 'has_more': False}
        with patch.object(app, 'get_comment_page', side_effect=page):
            task = self.wait_task(app.create_task(self.payload(links='11111\n22222', pages=1)))
        self.assertEqual(calls, [('11111', a['cookie']), ('22222', b['cookie'])])
        self.assertEqual(task['comment_pages'], 2)
