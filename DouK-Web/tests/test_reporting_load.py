"""A department-sized burst must be durable without platform requests."""
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import threading
import unittest
from urllib.request import Request, urlopen
from unittest.mock import patch

import test_operations as fixtures
import server as app


class DepartmentReportingTests(unittest.TestCase):
    setUp = fixtures.OperationsTests.setUp
    tearDown = fixtures.OperationsTests.tearDown

    def test_eighty_employees_can_report_without_login_or_platform_wait(self):
        rows=[{'employee_code':f'BURST{i:03}','name':f'测试人员{i}',
               'department':'并发测试部门','group':'同一小组'} for i in range(80)]
        employees=self.ops.import_employees({'rows':rows},self.admin)['ids']
        class QuietHandler(app.Handler):
            def log_message(self,*args):
                pass
        class BurstServer(app.ThreadingHTTPServer):
            request_queue_size=128
        service=BurstServer(('127.0.0.1',0),QuietHandler)
        thread=threading.Thread(target=service.serve_forever,daemon=True)
        thread.start()
        url=f'http://127.0.0.1:{service.server_port}/api/reporting/submit'
        with app.connect_db() as db:
            app.REPORT_ACCOUNTS.schema(db)
            for i in range(80):
                token=f'{i+1:064x}'
                db.execute('INSERT INTO ops_report_verifications VALUES(?,?,?,?,?)',
                    (hashlib.sha256(token.encode()).hexdigest(),f'load-account-{i}','测试账号',1,4102444800))
        def report(pair):
            index,employee=pair
            payload={'employee_id':employee,'links':str(7000000000000+index),
                     'request_id':f'burst-request-{index:016}','login_tokens':[f'{index+1:064x}']}
            req=Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
            with urlopen(req,timeout=30) as response:
                self.assertEqual(response.status,202)
                return json.load(response)
        try:
            with patch.object(app,'upstream',side_effect=AssertionError('HTTP acceptance must not wait for the platform')):
                with ThreadPoolExecutor(max_workers=20) as pool:
                    results=list(pool.map(report,enumerate(employees)))
            self.assertEqual(len(results),80)
            self.assertEqual(len({r['receipt_token'] for r in results}),80)
            self.assertEqual(self.ops.query('SELECT COUNT(*) n FROM ops_report_items',one=True)['n'],80)
            self.assertTrue(all(len(r['items'])==1 and r['items'][0]['status']=='queued' for r in results))
        finally:
            service.shutdown()
            service.server_close()
            thread.join()
