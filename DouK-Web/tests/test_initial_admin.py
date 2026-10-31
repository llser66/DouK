import unittest

import server as app
import test_workspace as workspace
from operations import AccessError, password_hash


class InitialAdminTests(unittest.TestCase):
    setUp = workspace.WorkspaceTests.setUp
    tearDown = workspace.WorkspaceTests.tearDown

    def test_initial_login_hash_and_restart_preserve_password(self):
        self.assertTrue(app.OPS.ensure_initial_admin())
        user = app.OPS.query("SELECT * FROM ops_users WHERE username='admin'", one=True)
        self.assertNotEqual(user['password'], '123456')
        result, _ = app.OPS.login({'username':'admin','password':'123456'})
        self.assertEqual(result['user']['id'], user['id'])
        app.OPS.execute('UPDATE ops_users SET password=? WHERE id=?',
                        (password_hash('Changed-password-987'), user['id']))
        self.assertFalse(app.OPS.ensure_initial_admin())
        with self.assertRaises(AccessError):
            app.OPS.login({'username':'admin','password':'123456'})
        app.OPS.login({'username':'admin','password':'Changed-password-987'})

    def test_existing_admin_and_username_collision_are_not_overwritten(self):
        app.OPS.schema()
        app.OPS.login({'username':'existing','password':'Existing-password-987'}, setup=True)
        self.assertFalse(app.OPS.ensure_initial_admin())
        self.assertIsNone(app.OPS.query("SELECT id FROM ops_users WHERE username='admin'", one=True))
        app.OPS.execute("UPDATE ops_users SET username='admin',role='employee'")
        with self.assertRaises(ValueError):
            app.OPS.ensure_initial_admin()
        self.assertEqual(app.OPS.query("SELECT role FROM ops_users", one=True)['role'], 'employee')
