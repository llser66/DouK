"""Optional, capability-scoped official QR login for anonymous LAN reporters.

Only an own-profile navigation detected by DouyinLogin is an identity proof.
Cookies are discarded, never returned, and never used to assign an employee.
"""
import hashlib
import re
import secrets
import shutil
import threading
import time

from douyin_login import DouyinLogin, ACTIVE_STATES
from operations import AccessError


class ReportAccounts:
    VALID_SECONDS = 7 * 86400

    def __init__(self, profile_root, connect=None):
        self.profile_root = profile_root
        self.connect = connect
        self.lock = threading.RLock()
        self.sessions = {}
        self.limits = {}

    def schema(self, db):
        db.execute('''CREATE TABLE IF NOT EXISTS ops_report_verifications(
          token_hash TEXT PRIMARY KEY,sec_uid TEXT NOT NULL,nickname TEXT NOT NULL,
          verified_at INTEGER NOT NULL,expires INTEGER NOT NULL)''')

    @staticmethod
    def _key(data):
        token = str(data.get('login_token', ''))
        if not re.fullmatch(r'[a-f0-9]{64}', token):
            raise AccessError('抖音核验已过期，请重新扫码，添加有效核验账号后才能报送', 404)
        return hashlib.sha256(token.encode()).hexdigest()

    def _entry(self, data):
        key = self._key(data)
        if self.connect:
            with self.connect() as db:
                self.schema(db)
                record = db.execute('SELECT * FROM ops_report_verifications WHERE token_hash=? AND expires>?', (key, time.time())).fetchone()
            if record:
                return {'identity':{'sec_uid':record['sec_uid'],'nickname':record['nickname'],
                                    'homepage':'https://www.douyin.com/user/'+record['sec_uid']},
                        'expires':record['expires'],'verified_at':record['verified_at'],'session':None}
        row = self.sessions.get(key)
        if not row or row.get('persisted') or row['expires'] <= time.time():
            raise AccessError('抖音核验已过期，请重新扫码，添加有效核验账号后才能报送', 404)
        return row

    def _display_identity(self, identity):
        """A stored label may fill a missing name, never establish account identity."""
        result = dict(identity)
        sec_uid = result.get('sec_uid', '')
        if result.get('nickname') and result['nickname'] != sec_uid:
            result['nickname_source'] = 'verification'
            return result
        result['nickname'] = ''
        if self.connect and sec_uid:
            with self.connect() as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                queries = []
                if {'ops_works','ops_bindings'} <= tables:
                    queries.append("SELECT w.nickname FROM ops_works w JOIN ops_bindings b ON b.id=w.binding_id WHERE b.sec_uid=? AND w.nickname<>'' AND w.nickname<>b.sec_uid ORDER BY w.updated DESC LIMIT 1")
                if 'ops_bindings' in tables:
                    queries.append("SELECT nickname FROM ops_bindings WHERE sec_uid=? AND nickname<>'' AND nickname<>sec_uid LIMIT 1")
                for query in queries:
                    record = db.execute(query, (sec_uid,)).fetchone()
                    if record:
                        result['nickname'] = record['nickname']
                        result['nickname_source'] = 'saved_record'
                        break
        return result

    def start(self, source):
        with self.lock:
            current = time.time()
            for key, row in list(self.sessions.items()):
                if row.get('persisted') and row['session'].state not in ACTIVE_STATES:
                    del self.sessions[key]
                    continue
                if row['expires'] < current:
                    row['session'].cancel()
                    if row['session'].state not in ACTIVE_STATES:
                        # Random server-owned directory below our fixed profile root only.
                        profile=row['session'].profile.resolve()
                        if profile.parent==self.profile_root().resolve() and re.fullmatch(r'[a-f0-9]{32}',profile.name):
                            shutil.rmtree(profile, ignore_errors=True)
                        del self.sessions[key]
            source = hashlib.sha256(str(source).encode()).hexdigest()
            self.limits = {k:v for k,v in self.limits.items() if v[0] > current-600}
            window, count = self.limits.get(source, (current, 0))
            if count >= 30:
                raise AccessError('扫码请求较多，请稍后再试；不扫码也可报送', 429)
            if sum(r['session'].state in ACTIVE_STATES for r in self.sessions.values()) >= 3 or len(self.sessions)>=100:
                raise AccessError('扫码窗口繁忙，请稍后再试；可先提交报送', 429)
            self.limits[source] = (window, count+1)
            token = secrets.token_hex(32)
            key = hashlib.sha256(token.encode()).hexdigest()
            row = {'identity': {}, 'expires': current+1800}
            def received(cookie, identity):
                # Do not retain the authentication cookie in memory or SQLite.
                sec_uid = str((identity or {}).get('sec_uid') or '')
                with self.lock:
                    if row['expires']>time.time() and not row['session'].event.is_set() and re.fullmatch(r'[A-Za-z0-9_-]{10,200}', sec_uid):
                        verified_at = int(time.time())
                        nickname = str((identity or {}).get('nickname') or '')[:80]
                        expires = verified_at + self.VALID_SECONDS if self.connect else row['expires']
                        if self.connect:
                            with self.connect() as db:
                                self.schema(db)
                                db.execute('DELETE FROM ops_report_verifications WHERE expires<=?', (verified_at,))
                                db.execute('INSERT OR REPLACE INTO ops_report_verifications VALUES(?,?,?,?,?)',
                                           (key,sec_uid,nickname,verified_at,expires))
                        row.update(identity={'sec_uid':sec_uid,'homepage':'https://www.douyin.com/user/'+sec_uid,
                                             'nickname':nickname},expires=expires,verified_at=verified_at,persisted=bool(self.connect))
                        return '抖音账号核验完成，可与作品发布账号对比'
                return '已登录，但未识别到本人主页；本次不能作为账号核验，请重新核验后报送'
            session = DouyinLogin(self.profile_root()/secrets.token_hex(16), received,
                                  lambda:bool(row['identity']), capture_profile=True, qr_mode=True)
            row['session'] = session
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = row
            try:
                session.start()
            except Exception:
                self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(),None)
                raise
            return {'login_token': token, **self.status({'login_token':token})}

    def status(self, data):
        with self.lock:
            row = self._entry(data)
            session = row['session']
            result = session.status() if session else {'state':'saved','message':'已恢复本浏览器保存的抖音账号核验，可继续报送'}
            return {key:result[key] for key in ('state','message')} | {
                'identity':self._display_identity(row['identity']), 'verified':bool(row['identity']),
                'qr_image':session.qr_image if session and session.state in ACTIVE_STATES else '',
                'expires_at':int(row['expires']), 'verified_at':row.get('verified_at',0),
                'remembered':bool(row['identity']) and bool(self.connect)}

    def proof(self, data):
        with self.lock:
            row = self._entry(data)
            if not row['identity']:
                raise ValueError('抖音账号尚未核验完成，请完成扫码后再报送')
            return self._display_identity(row['identity'])

    def proofs(self, data):
        """Resolve capabilities on the server; never trust posted account identities.

        Expiry/revocation of one account must not invalidate other verified accounts.
        The singular field remains supported for already-open employee pages.
        """
        tokens = data.get('login_tokens', [])
        if not isinstance(tokens, list) or len(tokens) > 100:
            raise ValueError('核验账号列表无效，一次最多使用 100 个账号')
        tokens = list(tokens)
        if data.get('login_token'):
            tokens.append(data['login_token'])
        identities = {}
        for token in dict.fromkeys(self._validated_token(t) for t in tokens):
            try:
                identity = self.proof({'login_token': token})
            except (AccessError, ValueError):
                continue  # Pending, expired or revoked proofs cannot authorize a report.
            identities[identity['sec_uid']] = identity
        return list(identities.values())

    @staticmethod
    def _validated_token(token):
        if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token):
            raise ValueError('核验凭据格式无效，请刷新页面后重新核验')
        return token

    def cancel(self, data):
        with self.lock:
            key = self._key(data)
            if self.connect:
                with self.connect() as db:
                    self.schema(db)
                    db.execute('DELETE FROM ops_report_verifications WHERE token_hash=?', (key,))
            row = self.sessions.get(key)
            if row:
                row['identity'] = {}
                row['session'].qr_image = ''
                row['session'].cancel()
                row['expires'] = 0
            return {'message':'已移除本次抖音核验；报送需保留至少一个有效核验账号'}
