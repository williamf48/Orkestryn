import hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest import mock
import named_access as a
class AccessTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.a=a.Access(self.root);self.admin=self.a.create('william','A long local password!',sorted(a.ROLES))
    def tearDown(self):self.a.close();self.temp.cleanup()
    def test_passwords_are_salted_and_sessions_are_stored_only_as_digests(self):
        u=self.a.create('viewer','A long local password!',['viewer'],self.admin);rows=list(self.a.db.execute('SELECT salt,password FROM users'))
        self.assertNotEqual(rows[0],rows[1]);token,csrf,p=self.a.login('viewer','A long local password!');stored=self.a.db.execute('SELECT token FROM sessions').fetchone()[0]
        self.assertEqual(stored,hashlib.sha256(token.encode()).hexdigest());self.assertNotIn('A long local password!',self.a.path.joinpath('accounts.sqlite3').read_bytes().decode('latin1'))
    def test_only_first_account_can_be_created_without_an_administrator(self):
        with self.assertRaises(a.AccessDenied):self.a.create('second','A long local password!',['admin'])
    def test_roles_are_enforced_and_an_administrator_is_not_an_implicit_owner(self):
        manager=self.a.create('manager','A long local password!',['manager'],self.admin)
        self.a.require(manager,'manager')
        with self.assertRaises(a.AccessDenied):self.a.require(manager,'owner_C')
    def test_revoking_roles_changes_an_existing_session_immediately(self):
        user=self.a.create('user','A long local password!',['manager','viewer'],self.admin);token,csrf,p=self.a.login('user','A long local password!')
        self.a.change(self.admin,user['subject'],roles=['viewer']);p,_=self.a.session(token,csrf)
        with self.assertRaises(a.AccessDenied):self.a.require(p,'manager')
    def test_disabled_account_sessions_fail_and_can_be_reenabled_by_admin(self):
        user=self.a.create('user','A long local password!',['viewer'],self.admin);token,csrf,_=self.a.login('user','A long local password!');self.a.change(self.admin,user['subject'],active=False)
        with self.assertRaises(a.AccessDenied):self.a.session(token,csrf)
        self.a.change(self.admin,user['subject'],active=True);self.assertEqual(self.a.session(token,csrf)[0]['name'],'user')
    def test_csrf_logout_and_absolute_session_expiry(self):
        token,csrf,_=self.a.login('william','A long local password!')
        with self.assertRaises(a.AccessDenied):self.a.session(token,'wrong')
        with mock.patch.object(a.time,'time',return_value=time.time()+1801):
            with self.assertRaises(a.AccessDenied):self.a.session(token,csrf)
        self.a.logout(token)
        with self.assertRaises(a.AccessDenied):self.a.session(token)
    def test_unknown_accounts_and_bad_passwords_have_a_generic_failure_and_throttle(self):
        for _ in range(5):
            with self.assertRaisesRegex(a.AccessDenied,'not accepted'):self.a.login('william','bad password')
        with self.assertRaisesRegex(a.AccessDenied,'Too many'):self.a.login('william','A long local password!')
        with self.assertRaisesRegex(a.AccessDenied,'not accepted'):self.a.login('unknown','bad password')
    def test_account_validation_rejects_unknown_roles_weak_password_and_self_lockout(self):
        for name,password,roles in [('name','short',['viewer']),('bad/name','A long local password!',['viewer']),('name','A long local password!',['god'])]:
            with self.assertRaises(ValueError):self.a.create(name,password,roles,self.admin)
        with self.assertRaises(ValueError):self.a.change(self.admin,self.admin['subject'],active=False)
        with self.assertRaises(ValueError):self.a.change(self.admin,self.admin['subject'],roles=['viewer'])
    def test_only_admin_can_change_another_users_permissions(self):
        viewer=self.a.create('viewer','A long local password!',['viewer'],self.admin)
        with self.assertRaises(a.AccessDenied):self.a.change(viewer,self.admin['subject'],roles=['viewer'])
if __name__=='__main__':unittest.main()
