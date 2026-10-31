"""Employee-owned Douyin login profiles and multiple monitored-account bindings."""
import re
import threading

from douyin_login import DouyinLogin, ACTIVE_STATES
from operations import AccessError, ident, now


class EmployeeAccounts:
    def __init__(self, ops, profile_root, other_active=lambda: False):
        self.ops, self.profile_root, self.other_active = ops, profile_root, other_active
        self.sessions = {}
        self.lock = threading.RLock()

    def row(self, login_id, actor=None):
        row = self.ops.query('SELECT * FROM ops_account_logins WHERE id=?',(login_id,),one=True)
        if not row or (actor and row['owner_id'] != actor['id']):
            raise AccessError('只能管理自己的抖音登录账号',403)
        if not self.ops.query('SELECT id FROM ops_users WHERE id=? AND enabled=1',(row['owner_id'],),one=True):
            raise AccessError('员工账号已停用',403)
        if row['binding_id'] and not self.ops.query('SELECT id FROM ops_bindings WHERE id=? AND owner_id=?',(row['binding_id'],row['owner_id']),one=True):
            raise AccessError('账号已重新分配，请联系管理员',403)
        if row['binding_id'] and self.ops.query('SELECT 1 FROM ops_binding_deletions WHERE binding_id=?',(row['binding_id'],),one=True):
            raise AccessError('账号已删除，请联系管理员恢复',403)
        return row

    def public(self, row):
        session = self.sessions.get(row['id'])
        status = session.status() if session else {'state':'idle','message':''}
        return {'id':row['id'],'binding_id':row['binding_id'],'nickname':row['nickname'],
                'has_cookie':bool(row['cookie']),'identity_verified':bool(row['detected_sec_uid']),
                'updated':row['updated'],'login':{**status,'message':row['message'] if status['state'] in {'idle','saved'} and row['message'] else status['message']}}

    def list(self, actor):
        rows = self.ops.query('SELECT l.* FROM ops_account_logins l LEFT JOIN ops_bindings b ON b.id=l.binding_id '
                              "WHERE l.owner_id=? AND (l.binding_id='' OR b.owner_id=l.owner_id) AND NOT EXISTS(SELECT 1 FROM ops_binding_deletions x WHERE x.binding_id=l.binding_id) ORDER BY l.created,l.rowid",(actor['id'],))
        return [self.public(r) for r in rows]

    def session(self, row):
        if row['id'] not in self.sessions:
            self.sessions[row['id']] = DouyinLogin(self.profile_root()/row['id'],
                lambda cookie, identity: self.received(row['id'],cookie,identity),
                lambda: bool(self.row(row['id'])['cookie']),capture_profile=True)
        return self.sessions[row['id']]

    def start(self, data, actor):
        with self.lock:
            if self.other_active() or any(s.state in ACTIVE_STATES for s in self.sessions.values()):
                raise ValueError('请先完成或取消当前登录，再登录另一个账号')
            if data.get('id'):
                row = self.row(data['id'],actor)
            else:
                binding_id = data.get('binding_id','')
                binding = None
                if binding_id:
                    binding = self.ops.query('SELECT * FROM ops_bindings WHERE id=? AND owner_id=?',(binding_id,actor['id']),one=True)
                    if not binding:
                        raise AccessError('只能登录自己负责的抖音账号',403)
                    existing = self.ops.query('SELECT * FROM ops_account_logins WHERE binding_id=? AND owner_id=?',(binding_id,actor['id']),one=True)
                    if existing:
                        row = existing
                        return self._start_row(row)
                nickname = str(data.get('nickname') or (binding or {}).get('nickname') or '我的抖音账号').strip()
                if not 1 <= len(nickname) <= 80:
                    raise ValueError('账号备注名称需为 1–80 字')
                login_id = ident()
                self.ops.execute('INSERT INTO ops_account_logins(id,owner_id,binding_id,nickname,monitor_start,created,updated) VALUES(?,?,?,?,?,?,?)',
                                 (login_id,actor['id'],binding_id,nickname,now(),now(),now()))
                row = self.row(login_id,actor)
            return self._start_row(row)

    def _start_row(self,row):
        self.session(row).start()
        return self.public(self.row(row['id']))

    def received(self, login_id, cookie, identity):
        with self.lock:
            row = self.row(login_id)
            sec_uid = str((identity or {}).get('sec_uid') or '')
            if sec_uid and not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',sec_uid):
                sec_uid = ''
            if row['binding_id']:
                expected = self.ops.query('SELECT sec_uid FROM ops_bindings WHERE id=?',(row['binding_id'],),one=True)['sec_uid']
                if sec_uid and expected != sec_uid:
                    message = '登录的抖音账号与此绑定不一致，原有登录信息保留；添加新账号请使用“登录并添加账号”。'
                    self.ops.execute('UPDATE ops_account_logins SET message=? WHERE id=?',(message,login_id))
                    return message
            # Store the credential separately from bindings; never expose it in JSON or audit logs.
            self.ops.execute('UPDATE ops_account_logins SET cookie=?,detected_sec_uid=?,updated=?,message=? WHERE id=?',
                             (cookie,sec_uid,now(),'登录成功，正在关联账号',login_id))
            if sec_uid:
                try:
                    result = self.complete({'id':login_id,'homepage':sec_uid}, {'id':row['owner_id']})
                    return result['message']
                except (AccessError,ValueError) as exc:
                    message = str(exc)
            else:
                message = '已登录抖音，未能自动识别本人主页；请粘贴本次登录账号的主页链接并确认绑定。'
            self.ops.execute('UPDATE ops_account_logins SET message=? WHERE id=?',(message,login_id))
            return message

    def complete(self,data,actor):
        with self.lock:
            row = self.row(data.get('id'),actor)
            if not row['cookie']:
                raise ValueError('请先完成抖音官方登录')
            # Homepage resolution uses only the existing share API. No user-info endpoint.
            config = self.ops.platform_config(owner_id=actor['id'])
            config['cookie'] = row['cookie']
            sec_uid = self.ops.resolve(data.get('homepage',''),account=True,config=config)
            if row['detected_sec_uid'] and sec_uid != row['detected_sec_uid']:
                raise ValueError('主页与本次登录识别到的账号不一致')
            with self.ops.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT * FROM ops_account_logins WHERE id=?',(row['id'],)).fetchone()
                user = db.execute('SELECT enabled FROM ops_users WHERE id=?',(actor['id'],)).fetchone()
                if not current or not user or not user['enabled'] or current['owner_id']!=actor['id']:
                    raise AccessError('员工账号已停用或登录归属已变更',403)
                old = db.execute('SELECT * FROM ops_bindings WHERE sec_uid=?',(sec_uid,)).fetchone()
                if old and db.execute('SELECT 1 FROM ops_binding_deletions WHERE binding_id=?',(old['id'],)).fetchone():
                    raise ValueError('该抖音账号已删除，请联系管理员恢复后再绑定')
                if old and old['owner_id'] != actor['id']:
                    raise ValueError('该抖音账号已分配给其他员工，请联系管理员调整；不会自动更改归属')
                if current['binding_id']:
                    original = db.execute('SELECT * FROM ops_bindings WHERE id=?',(current['binding_id'],)).fetchone()
                    if not original or original['owner_id']!=actor['id'] or original['sec_uid']!=sec_uid:
                        raise ValueError('主页与已有绑定不一致，请使用“登录并添加账号”')
                binding_id = old['id'] if old else ident()
                if not old:
                    db.execute('INSERT INTO ops_bindings(id,sec_uid,nickname,homepage,owner_id,monitor_start) VALUES(?,?,?,?,?,?)',
                               (binding_id,sec_uid,row['nickname'],f'https://www.douyin.com/user/{sec_uid}',actor['id'],row['monitor_start']))
                message = '登录并绑定成功，可继续添加其他抖音账号' if row['detected_sec_uid'] else '本人主页已确认并绑定，可继续添加其他抖音账号'
                # One current credential per owner + binding; repeated login replaces only that account.
                db.execute("UPDATE ops_account_logins SET cookie='',message='已由最近一次登录更新' WHERE binding_id=? AND owner_id=? AND id<>?",
                           (binding_id,actor['id'],row['id']))
                db.execute('UPDATE ops_account_logins SET binding_id=?,message=?,updated=? WHERE id=?',(binding_id,message,now(),row['id']))
            self.ops.audit('INFO','account','员工登录并绑定抖音账号',title=row['nickname'])
            return {'binding_id':binding_id,'message':message}

    def cancel(self,data,actor):
        with self.lock:
            row = self.row(data.get('id'),actor)
            session = self.sessions.get(row['id'])
            if session:
                session.cancel()
            return self.public(self.row(row['id']))
