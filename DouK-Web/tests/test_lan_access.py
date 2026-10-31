import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lan_access as lan


class LANAccessTests(unittest.TestCase):
    def headers(self, host='192.168.9.17:8877', **extra):
        return {'Host': host, **extra}

    @patch.object(lan, 'local_ipv4', return_value=['127.0.0.1', '192.168.9.17'])
    def test_exact_host_origin_and_private_clients_only(self, _):
        self.assertTrue(lan.trusted_request(self.headers(Origin='http://192.168.9.17:8877'), 8877, '192.168.9.80'))
        self.assertFalse(lan.trusted_request(self.headers(), 8877, '8.8.8.8'))
        self.assertFalse(lan.trusted_request(self.headers('192.168.9.99:8877'), 8877, '192.168.9.80'))
        self.assertFalse(lan.trusted_request(self.headers('attacker.test:8877'), 8877, '192.168.9.80'))
        self.assertFalse(lan.trusted_request(self.headers(Origin='http://attacker.test:8877'), 8877, '192.168.9.80'))
        self.assertFalse(lan.trusted_request(self.headers(Origin='null'), 8877, '192.168.9.80'))
        self.assertFalse(lan.trusted_request(self.headers(**{'Sec-Fetch-Site':'cross-site'}), 8877, '192.168.9.80'))
        self.assertFalse(lan.trusted_request(self.headers(**{'X-Forwarded-For':'192.168.9.80'}), 8877, '8.8.8.8'))
        self.assertFalse(lan.trusted_request(self.headers('192.168.9.17:9999'), 8877, '192.168.9.80'))

    @patch.object(lan, 'local_ipv4', return_value=['127.0.0.1', '192.168.9.17'])
    def test_tools_are_never_made_available_to_lan(self, _):
        self.assertTrue(lan.trusted_request(self.headers('localhost:8879'), 8879, '127.0.0.1', tools_mode=True))
        self.assertFalse(lan.trusted_request(self.headers('localhost:8879'), 8879, '192.168.9.80', tools_mode=True))
        self.assertFalse(lan.trusted_request(self.headers('192.168.9.17:8879'), 8879, '127.0.0.1', tools_mode=True))

    @patch.object(lan, 'local_ipv4', return_value=['127.0.0.1', '192.168.9.17'])
    def test_external_entry_navigation_requires_explicit_document_permission(self, _):
        navigation = self.headers(**{'Sec-Fetch-Site': 'cross-site',
                                     'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})
        self.assertFalse(lan.trusted_request(navigation, 8877, '192.168.9.80'))
        self.assertTrue(lan.trusted_request(navigation, 8877, '192.168.9.80', allow_document_navigation=True))
        for changes in (
            {'Sec-Fetch-Dest': 'iframe'}, {'Sec-Fetch-Dest': 'empty'}, {'Sec-Fetch-Mode': 'cors'},
            {'Sec-Fetch-Mode': ''}, {'Sec-Fetch-Dest': ''}, {'Origin': 'http://attacker.test'},
            {'Origin': 'null'}, {'Host': 'attacker.test:8877'}, {'Host': '192.168.9.99:8877'},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(lan.trusted_request({**navigation, **changes}, 8877, '192.168.9.80',
                                                    allow_document_navigation=True))
        self.assertFalse(lan.trusted_request(navigation, 8877, '8.8.8.8', allow_document_navigation=True))
        self.assertFalse(lan.trusted_request(self.headers('localhost:8879', **{
            'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}),
            8879, '192.168.9.80', tools_mode=True, allow_document_navigation=True))

    @patch.object(lan, 'local_ipv4', return_value=['127.0.0.1', '192.168.9.17'])
    def test_config_only_binds_local_interfaces(self, _):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'network.json'
            self.assertEqual(lan.bind_host(path), '0.0.0.0')
            path.write_text(json.dumps({'listen_host':'127.0.0.1'}))
            self.assertEqual(lan.bind_host(path), '127.0.0.1')
            path.write_text(json.dumps({'listen_host':'192.168.9.17'}))
            self.assertEqual(lan.bind_host(path), '192.168.9.17')
            path.write_text(json.dumps({'listen_host':'192.168.9.99'}))
            with self.assertRaises(ValueError):
                lan.bind_host(path)


if __name__ == '__main__':
    unittest.main()
