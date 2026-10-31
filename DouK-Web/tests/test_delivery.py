"""Documentation must be reachable without exposing sources or runtime data."""
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server as app
import test_workspace as fixtures


class DeliveryHTTPTests(unittest.TestCase):
    setUp = fixtures.WorkspaceTests.setUp
    tearDown = fixtures.WorkspaceTests.tearDown

    def test_early_public_report_does_not_block_first_admin_setup(self):
        app.OPS.schema()
        receipt=app.OPS.receive_report({'reporter':{'name':'首次员工','department':'测试部门','group':'测试组'},
                                      'links':'1234567890123','request_id':'before-first-admin-0001'},'test')
        self.assertTrue(receipt['receipt_token'])
        self.assertFalse(app.OPS.initialized())
        result,cookie=app.OPS.login({'username':'firstadmin','password':'First-admin-test-2026'},setup=True)
        self.assertEqual(result['user']['role'],'admin')
        self.assertTrue(app.OPS.initialized())
        with self.assertRaises(ValueError):
            app.OPS.login({'username':'anotheradmin','password':'Another-test-2026'},setup=True)

    def test_release_selection_excludes_live_data_profiles_and_media(self):
        import build_release
        root=Path(self.temp.name)/'DouK-Web'
        for relative in ('server.py','data/settings.json','data/workspace.sqlite','data/report-browsers/profile/Cookies',
                         'Downloads/private.mp4','.venv-api/pyvenv.cfg','docs/usage.html','tests/test_example.py'):
            path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('test',encoding='utf-8')
        upstream=root.parent/'TikTokDownloader';upstream.mkdir()
        (upstream/'license').write_text('test license',encoding='utf-8')
        with patch.object(build_release,'ROOT',root):
            paths={relative.as_posix() for source,relative in build_release.selected()}
        self.assertEqual(paths,{'DouK-Web/server.py','DouK-Web/docs/usage.html','DouK-Web/tests/test_example.py','TikTokDownloader/license'})

    def test_help_is_public_and_static_allowlist_still_protects_private_files(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            for path in ('/help', '/help/usage.html', '/help/developer.html', '/help/verification.html'):
                with urlopen(Request(base+path, headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'})) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn('text/html', response.headers['Content-Type'])
                    self.assertIn('DouK', response.read().decode())
            for path in ('/help/../../data/settings.json', '/help/usage.md', '/help/unknown.html', '/data/workspace.sqlite', '/operations.py'):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base+path)
                self.assertIn(error.exception.code, (403, 404))
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':
    unittest.main()
