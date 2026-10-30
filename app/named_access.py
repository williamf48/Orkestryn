"""Named local users, scrypt passwords, revocable sessions and role checks."""
from __future__ import annotations
import hashlib,hmac,secrets,sqlite3,time
from pathlib import Path
import simulator as sim
ROLES={'admin','manager','owner_C','deployer','cost','traffic','viewer','consumer'}
class AccessDenied(ValueError):pass
class Access:
    def __init__(self,root):
        self.path=Path(root).absolute()/'.control_access';self.path.mkdir(mode=0o700,exist_ok=True)
        if self.path.resolve()!=self.path:raise ValueError('Access directory cannot be linked.')
        self.path.chmod(0o700);db=self.path/'accounts.sqlite3'
        if db.resolve()!=db:raise ValueError('Access database cannot be linked.')
        self.db=sqlite3.connect(db,timeout=5,isolation_level=None);db.chmod(0o600)
        self.db.executescript('CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT UNIQUE,salt TEXT,password TEXT,roles TEXT,active INTEGER); CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT,csrf TEXT,expires REAL); CREATE TABLE IF NOT EXISTS failed(name TEXT,at REAL); CREATE TABLE IF NOT EXISTS audit(sequence INTEGER PRIMARY KEY AUTOINCREMENT,at REAL,subject TEXT,action TEXT);')
    def close(self):self.db.close()
    def count(self):return self.db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    def event(self,subject,action):self.db.execute('INSERT INTO audit(at,subject,action) VALUES(?,?,?)',(time.time(),subject,action))
    def create(self,name,password,roles,creator=None):
        if not isinstance(name,str) or not 2<=len(name)<=60 or any(not (c.isalnum() or c in '-_.') for c in name):raise ValueError('Use a 2-60 character account name.')
        if not isinstance(password,str) or not 12<=len(password)<=256:raise ValueError('Use a password of 12-256 characters.')
        if not isinstance(roles,list) or not roles or len(set(roles))!=len(roles) or not set(roles).issubset(ROLES):raise ValueError('Choose registered workspace roles.')
        salt=secrets.token_bytes(16);hashed=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32);uid=secrets.token_hex(16)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if creator is not None:self.require(creator,'admin')
            elif self.count():raise AccessDenied('Only an administrator can add another user.')
            self.db.execute('INSERT INTO users VALUES(?,?,?,?,?,1)',(uid,name,salt.hex(),hashed.hex(),sim.canonical(sorted(roles))));self.event(uid,'ACCOUNT_CREATED');self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise
        return self.user(uid)
    def user(self,uid):
        row=self.db.execute('SELECT name,roles,active FROM users WHERE id=?',(uid,)).fetchone()
        if not row or not row[2]:raise AccessDenied('Account unavailable.')
        import json
        return {'subject':uid,'name':row[0],'roles':json.loads(row[1]),'scope':'AUTHENTICATED LOCAL WORKSPACE USER; NOT CLOUD IAM'}
    def require(self,principal,role):
        current=self.user(principal.get('subject',''))
        if role not in current['roles']:raise AccessDenied('This action requires the '+role+' role.')
        return current
    def login(self,name,password):
        if not isinstance(name,str) or not isinstance(password,str) or len(password)>256:raise AccessDenied('Account or password was not accepted.')
        now=time.time();self.db.execute('DELETE FROM failed WHERE at<?',(now-60,));self.db.execute('DELETE FROM sessions WHERE expires<?',(now,))
        if self.db.execute('SELECT COUNT(*) FROM failed WHERE name=?',(name,)).fetchone()[0]>=5:raise AccessDenied('Too many attempts. Try again in one minute.')
        row=self.db.execute('SELECT id,salt,password,active FROM users WHERE name=?',(name,)).fetchone()
        salt=bytes.fromhex(row[1]) if row else b'local-dummy-salt';hashed=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
        if not row or not row[3] or not hmac.compare_digest(hashed.hex(),row[2]):self.db.execute('INSERT INTO failed VALUES(?,?)',(name,now));raise AccessDenied('Account or password was not accepted.')
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);self.db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row[0],csrf,now+1800));self.event(row[0],'LOGIN');return token,csrf,self.user(row[0])
    def session(self,token,csrf=None):
        if not isinstance(token,str) or len(token)>256:raise AccessDenied('Sign in again.')
        row=self.db.execute('SELECT user_id,csrf,expires FROM sessions WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row[2]<=time.time():raise AccessDenied('Sign in again.')
        if csrf is not None and (not isinstance(csrf,str) or not hmac.compare_digest(csrf,row[1])):raise AccessDenied('Refresh the review before submitting it.')
        return self.user(row[0]),row[1]
    def logout(self,token):self.db.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),))
    def change(self,creator,uid,roles=None,active=None):
        import json
        if roles is not None and (not isinstance(roles,list) or not roles or len(set(roles))!=len(roles) or not set(roles).issubset(ROLES)):raise ValueError('Unknown role.')
        if active is not None and type(active) is not bool:raise ValueError('active must be boolean.')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.require(creator,'admin');row=self.db.execute('SELECT roles,active FROM users WHERE id=?',(uid,)).fetchone()
            if not row:raise ValueError('Unknown account.')
            newroles=roles if roles is not None else json.loads(row[0]);newactive=bool(row[1]) if active is None else active
            if uid==creator['subject'] and ('admin' not in newroles or not newactive):raise ValueError('Keep your administrator account active.')
            other_admin=any('admin' in json.loads(r) for r, in self.db.execute('SELECT roles FROM users WHERE active=1 AND id!=?',(uid,)))
            if not (newactive and 'admin' in newroles) and not other_admin:raise ValueError('Keep at least one active administrator.')
            self.db.execute('UPDATE users SET roles=?,active=? WHERE id=?',(sim.canonical(sorted(newroles)),int(newactive),uid));self.event(creator['subject'],'ACCOUNT_PERMISSIONS_CHANGED');self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise
