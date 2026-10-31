"""Employee operations. All platform reads go through the existing three Web APIs."""
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
from http.cookies import SimpleCookie
import json
import re
import secrets
import threading
import time
from urllib.parse import urlparse, parse_qs, unquote
from public_reports import ReportingMixin

METRICS = ('play_count', 'digg_count', 'comment_count', 'collect_count', 'share_count')
TZ = timezone(timedelta(hours=8))
STATES = {'submitted': '已提交', 'pending': '待提交', 'overdue': '逾期未提交', 'confirm': '待确认', 'error': '采集异常','exempt':'无需提交'}


def now():
    return int(time.time())


def stamp(value):
    if not value:
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        return int(float(value))
    date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return int(date.replace(tzinfo=TZ).timestamp() if date.tzinfo is None else date.timestamp())


def ident():
    return secrets.token_hex(6)


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 6 <= len(password) <= 128:
        raise ValueError('密码需为 6–128 位')
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 260000).hex()
    return salt + ':' + digest


class AccessError(Exception):
    def __init__(self, message='请先登录', status=401):
        super().__init__(message)
        self.status = status


class Operations(ReportingMixin):
    def __init__(self, connect, upstream, config, audit, exporter):
        self.connect, self.upstream, self.config = connect, upstream, config
        self.audit, self.exporter = audit, exporter
        self.job_lock = threading.Lock()
        self.report_lock = threading.Lock()
        self.scheduler_stop = threading.Event()

    def schema(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS ops_groups(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,leader_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS ops_users(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE,name TEXT NOT NULL,
              password TEXT NOT NULL,role TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,group_id TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ops_sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ops_login_limits(username TEXT PRIMARY KEY,attempts INTEGER NOT NULL,until_time INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ops_bindings(id TEXT PRIMARY KEY,sec_uid TEXT NOT NULL UNIQUE,uid TEXT NOT NULL DEFAULT '',
              nickname TEXT NOT NULL,homepage TEXT NOT NULL,owner_id TEXT NOT NULL,monitor_start INTEGER NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,last_check INTEGER,last_success INTEGER,check_state TEXT NOT NULL DEFAULT 'unknown',
              check_message TEXT NOT NULL DEFAULT '尚未巡检',next_check INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ops_works(id TEXT PRIMARY KEY,binding_id TEXT NOT NULL,owner_id TEXT NOT NULL,group_id TEXT NOT NULL,
              description TEXT NOT NULL,nickname TEXT NOT NULL,published INTEGER,share_url TEXT NOT NULL,cover TEXT NOT NULL,
              metrics TEXT NOT NULL,discovered INTEGER NOT NULL,updated INTEGER NOT NULL,last_error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS ops_submissions(id TEXT PRIMARY KEY,work_id TEXT NOT NULL UNIQUE,user_id TEXT NOT NULL,
              submitted_at INTEGER NOT NULL,late INTEGER,deadline INTEGER);
            CREATE TABLE IF NOT EXISTS ops_jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,user_id TEXT NOT NULL,
              status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL,
              updated INTEGER NOT NULL,next_retry INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ops_settings(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ops_verified(binding_id TEXT NOT NULL,work_id TEXT NOT NULL,PRIMARY KEY(binding_id,work_id));
            CREATE TABLE IF NOT EXISTS ops_inspection_progress(binding_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,
              monitor_start INTEGER NOT NULL,completed_through INTEGER NOT NULL,full_checked_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ops_job_options(job_id TEXT PRIMARY KEY,full_scan INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ops_account_logins(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,binding_id TEXT NOT NULL DEFAULT '',
              cookie TEXT NOT NULL DEFAULT '',nickname TEXT NOT NULL,monitor_start INTEGER NOT NULL,created INTEGER NOT NULL,
              updated INTEGER NOT NULL,detected_sec_uid TEXT NOT NULL DEFAULT '',message TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS ops_explanations(work_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,note TEXT NOT NULL,updated INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ops_reviews(work_id TEXT PRIMARY KEY,action TEXT NOT NULL,note TEXT NOT NULL,reviewer_id TEXT NOT NULL,updated INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS ops_work_owner ON ops_works(owner_id,published);
            CREATE INDEX IF NOT EXISTS ops_work_binding ON ops_works(binding_id);
            CREATE INDEX IF NOT EXISTS ops_job_due ON ops_jobs(status,next_retry);
            ''')
            db.execute('INSERT OR IGNORE INTO ops_settings VALUES(1,?)', (json.dumps({'interval_minutes': 60, 'deadline_hours': 24, 'retry_limit': 2, 'retry_minutes': 10, 'auto_inspect': False}),))
            self.reporting_schema(db)

    def query(self, sql, params=(), one=False):
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        data = [dict(row) for row in rows]
        return (data[0] if data else None) if one else data

    def execute(self, sql, params=()):
        with self.connect() as db:
            db.execute(sql, params)

    def initialized(self):
        self.schema()
        return bool(self.query("SELECT id FROM ops_users WHERE role='admin' LIMIT 1"))

    def ensure_initial_admin(self):
        """Create the requested initial account once; never reset an existing admin."""
        self.schema()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM ops_users WHERE role='admin' LIMIT 1").fetchone():
                return False
            if db.execute("SELECT 1 FROM ops_users WHERE username='admin'").fetchone():
                raise ValueError('admin 已被已有人员占用，请先处理账号冲突；不会覆盖已有人员')
            db.execute('INSERT INTO ops_users VALUES(?,?,?,?,?,1,?,?)',
                       (ident(), 'admin', '管理员', password_hash('123456'), 'admin', '', now()))
            self.audit('INFO', 'system', '创建初始管理员', '仅首次安装创建 admin，密码以加盐哈希保存', db=db)
        return True

    def admin(self, user):
        if not user or user['role'] != 'admin':
            raise AccessError('仅管理员可操作', 403)

    def public_user(self, user):
        return {k: v for k, v in user.items() if k != 'password'}

    def authenticate(self, headers):
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get('Cookie', ''))
            token = cookie['douk_session'].value
        except Exception:
            return None
        result = self.query("SELECT u.* FROM ops_sessions s JOIN ops_users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.enabled=1 AND u.role='admin'",
                            (hashlib.sha256(token.encode()).hexdigest(), now()), one=True)
        return self.public_user(result) if result else None

    def auth_status(self, headers):
        configured = self.initialized()
        return {'app': 'DouK Workspace', 'initialized': configured, 'user': self.authenticate(headers) if configured else None}

    def login(self, data, setup=False):
        username, password = str(data.get('username', '')).strip(), data.get('password', '')
        if setup:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM ops_users WHERE role='admin' LIMIT 1").fetchone():
                    raise ValueError('管理员已创建，请登录')
                if not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}', username):
                    raise ValueError('登录账号需为 3–40 位字母、数字或 _.-')
                user_id = ident()
                db.execute('INSERT INTO ops_users VALUES(?,?,?,?,?,1,?,?)',
                           (user_id, username, str(data.get('name') or '管理员')[:40], password_hash(password), 'admin', '', now()))
            self.audit('INFO', 'system', '创建管理员')
        else:
            limit = self.query('SELECT * FROM ops_login_limits WHERE username=?', (username,), one=True)
            if limit and limit['attempts'] >= 5 and limit['until_time'] > now():
                raise AccessError('尝试次数过多，请在 15 分钟后重试', 429)
            user = self.query('SELECT * FROM ops_users WHERE username=?', (username,), one=True)
            valid = False
            if user and user['role']=='admin' and isinstance(password, str) and 6 <= len(password) <= 128:
                salt = user['password'].split(':')[0]
                valid = hmac.compare_digest(user['password'], password_hash(password, salt))
            else:
                hashlib.pbkdf2_hmac('sha256', b'invalid', b'login-padding', 260000)
            if not valid or not user['enabled'] or user['role'] != 'admin':
                attempts = limit['attempts']+1 if limit and limit['until_time'] > now() else 1
                self.execute('INSERT OR REPLACE INTO ops_login_limits VALUES(?,?,?)', (username[:100], attempts, now()+900))
                raise AccessError('账号或密码错误，或账号已禁用')
            self.execute('DELETE FROM ops_login_limits WHERE username=?', (username,))
        user = self.query('SELECT * FROM ops_users WHERE username=?', (username,), one=True)
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('DELETE FROM ops_sessions WHERE expires<=?', (now(),))
            db.execute('INSERT INTO ops_sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['id'], now()+43200))
        self.audit('INFO', 'system', '员工系统登录', title=user['name'])
        return {'user': self.public_user(user)}, f'douk_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'

    def logout(self, headers):
        cookie = SimpleCookie()
        cookie.load(headers.get('Cookie', ''))
        if cookie.get('douk_session'):
            self.execute('DELETE FROM ops_sessions WHERE token=?', (hashlib.sha256(cookie['douk_session'].value.encode()).hexdigest(),))
        return {'message': '已退出登录'}, 'douk_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'

    def settings(self):
        return {'inspection_overlap_days':3, 'full_inspect_days':30,
                **json.loads(self.query('SELECT payload FROM ops_settings WHERE id=1', one=True)['payload']), 'auto_inspect':False}

    def save_settings(self, data, user):
        self.admin(user)
        settings = self.settings()
        for key, low, high in [('interval_minutes', 5, 10080), ('deadline_hours', 1, 720), ('retry_limit', 0, 5), ('retry_minutes', 1, 1440),
                               ('inspection_overlap_days',1,30), ('full_inspect_days',7,365)]:
            if key in data:
                value = int(data[key])
                if not low <= value <= high:
                    raise ValueError(f'{key} 应在 {low} 到 {high} 之间')
                settings[key] = value
        if 'auto_inspect' in data:
            if not isinstance(data['auto_inspect'], bool):
                raise ValueError('自动巡检开关必须为布尔值')
            if data['auto_inspect']:
                raise ValueError('自动巡检已移除，请使用未报送人员核对本网站报送记录')
            settings['auto_inspect'] = False
        self.execute('UPDATE ops_settings SET payload=? WHERE id=1', (json.dumps(settings),))
        self.audit('INFO', 'system', '保存运营设置', title=user['name'])
        return settings

    def save_user(self, data, actor):
        self.admin(actor)
        user_id = data.get('id') or ident()
        existing = self.query('SELECT * FROM ops_users WHERE id=?', (user_id,), one=True)
        username = str(data.get('username', existing['username'] if existing else '')).strip()
        name = str(data.get('name', existing['name'] if existing else '')).strip()
        role = data.get('role', existing['role'] if existing else 'employee')
        group = data.get('group_id', existing['group_id'] if existing else '')
        enabled = data.get('enabled', bool(existing['enabled']) if existing else True)
        if not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}', username) or not 1 <= len(name) <= 40 or role not in {'admin','employee'} or not isinstance(enabled, bool):
            raise ValueError('请检查账号、姓名、角色与启用状态')
        if group and not self.query('SELECT id FROM ops_groups WHERE id=?', (group,)):
            raise ValueError('小组不存在')
        if user_id == actor['id'] and (role != 'admin' or not enabled):
            raise ValueError('不能禁用或降级当前管理员')
        hashed = password_hash(data['password']) if data.get('password') else existing['password'] if existing else password_hash('')
        if existing and (role != existing['role'] or not enabled or data.get('password')):
            self.execute('DELETE FROM ops_sessions WHERE user_id=?', (user_id,))
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM ops_employee_deletions WHERE user_id=?',(user_id,)).fetchone():
                    raise ValueError('员工已删除，请先在“已删除”中恢复后再编辑')
                if existing and (existing['group_id'] != group or bool(existing['enabled']) != enabled):
                    self.record_scope_change(db, user_id, existing['group_id'], group)
                db.execute('INSERT INTO ops_users VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name,password=excluded.password,role=excluded.role,enabled=excluded.enabled,group_id=excluded.group_id',
                           (user_id, username, name, hashed, role, int(enabled), group, now()))
        except Exception as exc:
            if 'UNIQUE' in str(exc):
                raise ValueError('登录账号已存在') from exc
            raise
        self.audit('INFO', 'account', '保存员工账号', title=f'{actor["name"]} → {name}')
        return {'id': user_id}

    def save_group(self, data, actor):
        return self.save_roster_group(data, actor)

    def platform_config(self, binding_id=None, owner_id=None):
        return self.config(binding_id=binding_id, owner_id=owner_id)

    def resolve(self, text, account=False, config=None):
        text = str(text).strip()
        urls = re.findall(r'https?://[^\s<>"，。]+', text)
        if not urls:
            if account and re.fullmatch(r'[A-Za-z0-9_-]{10,200}', text):
                return text
            if not account and re.fullmatch(r'\d{6,30}', text):
                return text
            raise ValueError('请输入有效抖音作品链接或主页链接')
        def parsed(url):
            p = urlparse(url)
            if p.scheme not in {'https','http'} or not p.hostname or not (p.hostname == 'douyin.com' or p.hostname.endswith('.douyin.com')) or p.username:
                raise ValueError('仅支持抖音链接')
            return p
        url = urls[0]
        p = parsed(url)
        if p.hostname == 'v.douyin.com':
            result = self.upstream('/douyin/share', {'text':url}, config if config is not None else self.platform_config())
            url = result.get('url')
            if not isinstance(url, str):
                raise ValueError('分享链接解析失败')
            p = parsed(url)
        if account:
            match = re.search(r'/user/([^/?]+)', p.path)
            value = parse_qs(p.query).get('sec_uid',[None])[0] or (unquote(match[1]) if match else None)
        else:
            match = re.search(r'/(?:video|note|photo)/(\d+)', p.path)
            value = parse_qs(p.query).get('modal_id',parse_qs(p.query).get('aweme_id',[None]))[0] or (match[1] if match else None)
        if not value or (not account and not str(value).isdigit()):
            raise ValueError('链接中未找到账号或作品 ID')
        return value

    def save_binding(self, data, actor):
        self.admin(actor)
        binding_id = data.get('id') or ident()
        old = self.query('SELECT * FROM ops_bindings WHERE id=?', (binding_id,), one=True)
        if self.query('SELECT 1 FROM ops_binding_deletions WHERE binding_id=?', (binding_id,), one=True):
            raise ValueError('账号已删除，请先在“已删除账号”中恢复')
        owner = data.get('owner_id')
        if not self.query('SELECT id FROM ops_users WHERE id=? AND enabled=1', (owner,)):
            raise ValueError('请选择已启用的责任运营人')
        sec_uid = self.resolve(data.get('homepage') or data.get('sec_uid',''), account=True)
        monitor_start = stamp(data.get('monitor_start'))
        nickname = str(data.get('nickname','')).strip()
        if not monitor_start or not nickname or len(nickname)>80:
            raise ValueError('请填写账号备注名称和监测开始时间')
        if old and old['sec_uid'] != sec_uid:
            raise ValueError('已绑定账号不能更换 sec_uid，请另建绑定')
        enabled = data.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ValueError('启用状态必须为布尔值')
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM ops_binding_deletions WHERE binding_id=?',(binding_id,)).fetchone() or db.execute('''
                    SELECT 1 FROM ops_bindings b JOIN ops_binding_deletions x ON x.binding_id=b.id WHERE b.sec_uid=?''',(sec_uid,)).fetchone():
                    raise ValueError('账号已删除，请先在“已删除账号”中恢复')
                if not db.execute('SELECT 1 FROM ops_users WHERE id=? AND enabled=1',(owner,)).fetchone():
                    raise ValueError('员工已停用或已删除，请先恢复启用后再绑定账号')
                if old and not db.execute('SELECT 1 FROM ops_bindings WHERE id=?',(binding_id,)).fetchone():
                    raise ValueError('账号已删除，请重新添加')
                if old:
                    db.execute('DELETE FROM ops_inspection_progress WHERE binding_id=?',(binding_id,))
                    if old['owner_id'] != owner or bool(old['enabled']) != enabled:
                        old_user = db.execute('SELECT group_id FROM ops_users WHERE id=?', (old['owner_id'],)).fetchone()
                        new_user = db.execute('SELECT group_id FROM ops_users WHERE id=?', (owner,)).fetchone()
                        self.record_scope_change(db, old['owner_id'], old_user['group_id'] if old_user else '')
                        self.record_scope_change(db, owner, '', new_user['group_id'] if new_user else '')
                    db.execute('UPDATE ops_bindings SET nickname=?,owner_id=?,monitor_start=?,enabled=?,check_state=?,check_message=?,next_check=0 WHERE id=?',
                               (nickname,owner,monitor_start,int(enabled),'unknown','绑定已更新，等待重新巡检',binding_id))
                else:
                    db.execute('INSERT INTO ops_bindings(id,sec_uid,nickname,homepage,owner_id,monitor_start,enabled) VALUES(?,?,?,?,?,?,?)',
                               (binding_id,sec_uid,nickname,f'https://www.douyin.com/user/{sec_uid}',owner,monitor_start,int(enabled)))
        except Exception as exc:
            if 'UNIQUE' in str(exc):
                raise ValueError('该抖音账号已绑定') from exc
            raise
        self.audit('INFO','account','保存抖音账号绑定','管理员设置为'+('启用' if enabled else '停用'),title=nickname)
        return {'id':binding_id}

    def normalize(self, raw):
        if not isinstance(raw, dict):
            raise ValueError('作品数据格式无效')
        author = raw.get('author') if isinstance(raw.get('author'), dict) else {}
        work_id = str(raw.get('aweme_id') or raw.get('id') or '')
        sec_uid = author.get('sec_uid') or raw.get('sec_uid')
        if not re.fullmatch(r'\d{6,30}', work_id) or not sec_uid:
            raise ValueError('作品缺少 ID 或作者 sec_uid，无法核验归属')
        metrics = raw.get('statistics') if isinstance(raw.get('statistics'),dict) else raw
        clean_metrics = {}
        for key in METRICS:
            try:
                value = int(metrics[key]) if metrics.get(key) is not None and not isinstance(metrics.get(key), bool) else None
            except (ValueError,TypeError,OverflowError):
                value = None
            clean_metrics[key] = value if value is not None and value>=0 else None
        cover = raw.get('static_cover') or (raw.get('video') or {}).get('cover') or ''
        if isinstance(cover,dict):
            cover = next(iter(cover.get('url_list',[])), '')
        if isinstance(cover,list):
            cover = next(iter(cover),'')
        if not isinstance(cover,str) or not cover.startswith('https://'):
            cover = ''
        return {'id':work_id,'sec_uid':str(sec_uid),'uid':str(author.get('uid') or raw.get('uid') or ''),
                'description':str(raw.get('desc') or ''),'nickname':str(author.get('nickname') or raw.get('nickname') or ''),
                'published':stamp(raw.get('create_timestamp') or raw.get('create_time')),
                'share_url':f'https://www.douyin.com/video/{work_id}','cover':cover,'values':clean_metrics}

    def detail(self, text, binding_id=None, owner_id=None):
        config = self.platform_config(binding_id, owner_id)
        work_id = self.resolve(text, config=config)
        result = self.upstream('/douyin/detail', {'detail_id':work_id,'source':True,'cookie':config.get('cookie','')}, config)
        raw = result.get('data')
        if isinstance(raw,list):
            raw = raw[0] if len(raw)==1 else None
        if not isinstance(raw,dict):
            raise ValueError('作品详情采集失败，请检查采集登录账号与作品权限')
        work = self.normalize(raw)
        if work['id'] != work_id:
            raise ValueError('返回的作品 ID 与提交链接不一致')
        return work

    def upsert_work(self, work, binding, db):
        previous = db.execute('SELECT * FROM ops_works WHERE id=?',(work['id'],)).fetchone()
        if previous and previous['binding_id'] != binding['id']:
            raise ValueError('作品归属与已有绑定不一致')
        metrics = json.loads(previous['metrics']) if previous else {}
        for key, value in work['values'].items():
            old = metrics.get(key, {})
            metrics[key] = {'value':value if value is not None else old.get('value'),
                            'updated_at':now() if value is not None else old.get('updated_at'), 'available':value is not None}
        owner = db.execute('SELECT group_id FROM ops_users WHERE id=?',(binding['owner_id'],)).fetchone()
        db.execute('''INSERT INTO ops_works VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          description=excluded.description,nickname=excluded.nickname,published=COALESCE(excluded.published,ops_works.published),
          cover=CASE WHEN excluded.cover='' THEN ops_works.cover ELSE excluded.cover END,metrics=excluded.metrics,updated=excluded.updated,last_error='' ''',
                   (work['id'],binding['id'],binding['owner_id'],owner['group_id'],work['description'],work['nickname'],work['published'],work['share_url'],work['cover'],json.dumps(metrics),now(),now(),''))
        if work['uid']:
            db.execute('UPDATE ops_bindings SET uid=? WHERE id=?',(work['uid'],binding['id']))
        self.snapshot_work(db, work['id'], binding['owner_id'], previous['group_id'] if previous else owner['group_id'])

    def submit(self, data, actor):
        selected = data.get('binding_id')
        if selected and not self.query('SELECT id FROM ops_bindings WHERE id=? AND owner_id=? AND enabled=1',(selected,actor['id']),one=True):
            raise AccessError('只能使用自己负责的已启用账号',403)
        work = self.detail(data.get('link',''), selected, actor['id'])
        if selected and not self.query('SELECT id FROM ops_bindings WHERE id=? AND sec_uid=?',(selected,work['sec_uid']),one=True):
            raise ValueError('作品作者与所选抖音账号不一致')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM ops_users WHERE id=? AND enabled=1',(actor['id'],)).fetchone():
                raise AccessError('账号已禁用，请重新登录')
            binding = db.execute('SELECT * FROM ops_bindings WHERE sec_uid=? AND enabled=1',(work['sec_uid'],)).fetchone()
            if not binding:
                raise ValueError('该作品作者不是你负责的已绑定抖音账号')
            previous = db.execute('SELECT owner_id FROM ops_works WHERE id=?',(work['id'],)).fetchone()
            expected_owner = previous['owner_id'] if previous else binding['owner_id']
            if expected_owner != actor['id']:
                raise ValueError('该作品作者不是你负责的已绑定抖音账号，或该作品已归属其他责任人')
            if db.execute('SELECT 1 FROM ops_submissions WHERE work_id=?',(work['id'],)).fetchone():
                raise ValueError('该作品已提交，不能重复提交或修改提交记录')
            self.upsert_work(work,binding,db)
            deadline = work['published']+self.settings()['deadline_hours']*3600 if work['published'] and work['published']>=binding['monitor_start'] else None
            db.execute('INSERT INTO ops_submissions VALUES(?,?,?,?,?,?)',(ident(),work['id'],actor['id'],now(),int(now()>deadline) if deadline else None,deadline))
        self.audit('INFO','task','员工作品提交',task_id=work['id'],title=actor['name'])
        return {'id':work['id'],'message':'作品归属核验通过，提交记录已锁定'}

    def enqueue(self, kind, target, actor, full_scan=False):
        if kind not in {'inspect','refresh'}:
            raise ValueError('任务类型无效')
        if not isinstance(full_scan,bool) or (full_scan and kind!='inspect'):
            raise ValueError('历史补查参数无效')
        if full_scan:
            self.admin(actor)
        table = 'ops_bindings' if kind=='inspect' else 'ops_works'
        item=self.query(f'SELECT * FROM {table} WHERE id=?',(target,),one=True)
        if not item:
            raise ValueError('任务对象不存在')
        if actor['role']!='admin':
            if item['owner_id']!=actor['id']:
                raise AccessError('只能刷新自己负责的账号与作品',403)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if kind=='refresh' and not db.execute('SELECT 1 FROM ops_works WHERE id=?',(target,)).fetchone():
                raise ValueError('作品已删除，请刷新列表')
            binding_id=target if kind=='inspect' else item['binding_id']
            if db.execute('SELECT 1 FROM ops_binding_deletions WHERE binding_id=?',(binding_id,)).fetchone():
                raise ValueError('账号已删除，请先恢复后再采集')
            if kind=='inspect' and not db.execute('SELECT 1 FROM ops_bindings WHERE id=? AND enabled=1',(target,)).fetchone():
                raise ValueError('账号监测已停用，请启用后再巡检')
            pending = db.execute("SELECT id,status FROM ops_jobs WHERE kind=? AND target=? AND status IN ('queued','running','retrying')",(kind,target)).fetchone()
            if pending:
                if full_scan:
                    if pending['status']=='running':
                        raise ValueError('当前巡检正在运行，请结束后再补查历史')
                    db.execute('INSERT OR REPLACE INTO ops_job_options VALUES(?,1)',(pending['id'],))
                return {'id':pending['id'],'message':'已有待执行或运行中的任务'}
            if actor['role']!='admin' and db.execute('SELECT 1 FROM ops_jobs WHERE kind=? AND target=? AND created>?',(kind,target,now()-60)).fetchone():
                raise ValueError('刚刚已刷新，请稍后再试')
            job_id = ident()
            db.execute('INSERT INTO ops_jobs VALUES(?,?,?,?,?,0,?,?,?,0)',(job_id,kind,target,actor['id'],'queued','等待处理',now(),now()))
            if full_scan:
                db.execute('INSERT INTO ops_job_options VALUES(?,1)',(job_id,))
        self.audit('INFO','task','创建运营采集任务',task_id=job_id,title=kind)
        return {'id':job_id,'message':'任务已加入队列'}

    def explain(self,data,actor):
        work=self.query('SELECT * FROM ops_works WHERE id=?',(data.get('id'),),one=True)
        if not work or work['owner_id']!=actor['id']:
            raise AccessError('只能说明自己负责的作品',403)
        if self.query('SELECT 1 FROM ops_submissions WHERE work_id=?',(work['id'],)):
            raise ValueError('作品已提交，无需补充漏报说明')
        note=str(data.get('note','')).strip()
        if not 2<=len(note)<=1000:
            raise ValueError('请填写 2–1000 字的情况说明')
        self.execute('INSERT INTO ops_explanations VALUES(?,?,?,?) ON CONFLICT(work_id) DO UPDATE SET note=excluded.note,updated=excluded.updated',
                     (work['id'],actor['id'],note,now()))
        self.audit('INFO','task','员工补充核查说明',task_id=work['id'],title=actor['name'])
        return {'message':'说明已保存，管理员可在漏报核查中查看'}

    def review(self,data,actor):
        self.admin(actor)
        action=data.get('action')
        note=str(data.get('note','')).strip()
        if action not in {'exempt','reopen'} or not 2<=len(note)<=1000:
            raise ValueError('请选择处理方式并填写 2–1000 字的理由')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM ops_works WHERE id=?',(data.get('id'),)).fetchone():
                raise ValueError('作品不存在')
            if db.execute('SELECT 1 FROM ops_submissions WHERE work_id=?',(data['id'],)).fetchone():
                raise ValueError('已提交作品不能改成无需提交')
            db.execute('INSERT INTO ops_reviews VALUES(?,?,?,?,?) ON CONFLICT(work_id) DO UPDATE SET action=excluded.action,note=excluded.note,reviewer_id=excluded.reviewer_id,updated=excluded.updated',
                       (data['id'],action,note,actor['id'],now()))
        self.audit('INFO','task','核查处理：'+('无需提交' if action=='exempt' else '恢复核查'),note,task_id=data['id'],title=actor['name'])
        return {'message':'处理已保存，员工可查看结果'}

    def retry(self, job_id, actor):
        self.admin(actor)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job=db.execute('SELECT * FROM ops_jobs WHERE id=?',(job_id,)).fetchone()
            if not job or job['status'] not in {'failed','partial'}:
                raise ValueError('仅失败或无法确认的任务可重试')
            if db.execute('SELECT 1 FROM ops_job_deletions WHERE job_id=?',(job_id,)).fetchone():
                raise ValueError('任务已删除，请先恢复后再重试')
            if job['kind']=='inspect':
                raise ValueError('自动巡检已移除，历史巡检任务不能重试')
            target_work=db.execute('SELECT binding_id FROM ops_works WHERE id=?',(job['target'],)).fetchone() if job['kind']=='refresh' else None
            if job['kind']=='refresh' and not target_work:
                raise ValueError('作品已删除，不能重试旧任务')
            binding_id=job['target'] if job['kind']=='inspect' else target_work['binding_id']
            if db.execute('SELECT 1 FROM ops_binding_deletions WHERE binding_id=?',(binding_id,)).fetchone():
                raise ValueError('账号已删除，请先恢复后再重试')
            if db.execute("SELECT 1 FROM ops_jobs WHERE kind=? AND target=? AND id<>? AND status IN ('queued','running','retrying')",(job['kind'],job['target'],job_id)).fetchone():
                raise ValueError('该对象已有待执行任务')
            db.execute("UPDATE ops_jobs SET status='queued',attempts=0,next_retry=0,updated=?,message='等待手动重试' WHERE id=?",(now(),job_id))
        self.audit('INFO','task','重试运营采集任务',task_id=job_id,title=actor['name'])
        return {'message':'已加入重试队列'}

    def remove_job(self,data,actor,restore=False):
        self.admin(actor)
        job_id=str(data.get('id') or '')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job=db.execute('SELECT * FROM ops_jobs WHERE id=?',(job_id,)).fetchone()
            if not job:
                raise ValueError('任务不存在')
            if job['status'] not in {'completed','failed','partial','cancelled'}:
                raise ValueError('仅已完成、失败或无法确认的任务可删除，等待或运行中的任务暂不能删除')
            if restore:
                if not db.execute('SELECT 1 FROM ops_job_deletions WHERE job_id=?',(job_id,)).fetchone():
                    raise ValueError('任务未删除，无需恢复')
                db.execute('DELETE FROM ops_job_deletions WHERE job_id=?',(job_id,))
            else:
                db.execute('INSERT OR IGNORE INTO ops_job_deletions VALUES(?,?,?)',(job_id,now(),actor['id']))
        self.audit('INFO','task','恢复采集任务' if restore else '删除采集任务（可恢复）','保留作品、统计和操作日志',task_id=job_id,title=actor['name'])
        return {'id':job_id,'message':'任务记录已恢复，未重新执行采集' if restore else '任务记录已删除，作品数据保留；可在“已删除”中恢复'}

    def remove_work(self,data,actor,restore=False):
        self.admin(actor)
        if restore:
            raise ValueError('作品数据已改为实际删除，不能从页面恢复；需要时请重新报送')
        if data.get('confirmed') is not True:
            raise ValueError('请管理员确认删除后再执行')
        if data.get('permanent') is not True:
            raise ValueError('删除方式已更新，请刷新页面后确认永久删除')
        work_id=str(data.get('id') or '')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM ops_works WHERE id=?',(work_id,)).fetchone():
                raise ValueError('作品记录不存在')
            result=self._purge_work_records(db,work_ids=[work_id])
            self.audit('INFO','task','删除作品及关联报送数据',
                       f"清理作品 {result['works']} 件、报送 {result['reports']} 条；首次报送与统计已清除，操作日志保留",
                       task_id=work_id,title=actor['name'],db=db)
        return {**result,'id':work_id,'message':'作品及关联报送数据已删除，日志保留，可重新报送'}

    def remove_binding(self,data,actor,restore=False):
        self.admin(actor)
        if restore:raise ValueError('账号绑定已改为实际删除，不能恢复；需要时可重新添加或核验报送')
        if data.get('confirmed') is not True or data.get('permanent') is not True:
            raise ValueError('请管理员确认永久删除后再执行')
        binding_id=str(data.get('id') or '')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            binding=db.execute('SELECT * FROM ops_bindings WHERE id=?',(binding_id,)).fetchone()
            if not binding:raise ValueError('账号绑定不存在')
            result=self._purge_directory_records(db,binding_ids=[binding_id])
            self.audit('INFO','account','删除抖音账号及关联业务数据',
                       f"作品 {result['works']} 件、报送 {result['reports']} 条；只保留操作日志",title=binding['nickname'],db=db)
        return {**result,'id':binding_id,'message':'账号绑定已删除，可重新添加；未设置停用限制'}

    def inspect(self, binding_id, full_scan=False):
        binding = self.query('SELECT * FROM ops_bindings WHERE id=?',(binding_id,),one=True)
        if not binding or not binding['enabled']:
            raise ValueError('绑定账号已停用')
        # Only an entirely successful window advances this checkpoint. Use request
        # start, rather than finish, so posts published during pagination are revisited.
        started = now()
        cfg = self.settings()
        progress = self.query('SELECT * FROM ops_inspection_progress WHERE binding_id=?',(binding_id,),one=True)
        valid_progress = bool(progress and progress['owner_id']==binding['owner_id']
                              and progress['monitor_start']==binding['monitor_start']
                              and binding['monitor_start']<=progress['completed_through']<=started)
        full_scan = bool(full_scan or not valid_progress or
                         started-progress['full_checked_at']>=cfg['full_inspect_days']*86400)
        earliest = binding['monitor_start'] if full_scan else max(binding['monitor_start'],progress['completed_through']-cfg['inspection_overlap_days']*86400)
        # Upstream filters whole dates. Replace verification evidence for that same
        # whole-day window, including the overlap, and retain earlier evidence.
        earliest_day = datetime.fromtimestamp(earliest,TZ).replace(hour=0,minute=0,second=0,microsecond=0)
        window_start = max(binding['monitor_start'],int(earliest_day.timestamp()))
        config = self.platform_config(binding_id)
        result = self.upstream('/douyin/account',{'sec_user_id':binding['sec_uid'],'tab':'post','source':True,
                    'earliest':earliest_day.strftime('%Y/%m/%d'),'cookie':config.get('cookie','')},config)
        raw_rows = result.get('data')
        if not isinstance(raw_rows,list):
            raise ValueError('账号发布作品采集失败，无法核查；不能视为未发布作品')
        meta = (result.get('params') or {}).get('workspace_check') or {}
        complete = meta.get('complete') is True
        works = []
        for raw in raw_rows:
            try:
                work = self.normalize(raw)
                if work['sec_uid'] != binding['sec_uid']:
                    complete = False
                    continue
                if not work['published']:
                    complete = False
                if not work['published'] or work['published']>=binding['monitor_start']:
                    works.append(work)
            except (ValueError,TypeError,OverflowError):
                complete = False
        # A list that omits an already parsed report from this same author/window
        # cannot prove the employee's publishing total. Identity review is separate.
        found = {work['id'] for work in works}
        missing_reports = set()
        for report in self.query("""SELECT r.payload FROM ops_report_items r
                LEFT JOIN ops_report_deletions d ON d.item_id=r.id
                WHERE r.payload<>'' AND d.item_id IS NULL"""):
            try:
                known = json.loads(report['payload'])
                if (known.get('sec_uid') == binding['sec_uid'] and
                        isinstance(known.get('published'), (int, float)) and
                        window_start <= known['published'] <= started - 120 and
                        known.get('id') and known['id'] not in found):
                    missing_reports.add(known['id'])
            except (ValueError, TypeError, AttributeError):
                continue
        if missing_reports:
            complete = False
        state = 'complete' if complete else 'incomplete'
        raw_count = meta.get('raw_count')
        raw_count = raw_count if type(raw_count) is int and raw_count >= len(raw_rows) else None
        count_label = f'已读取 {raw_count} 个发布作品' if raw_count is not None else f'日期筛选后返回 {len(raw_rows)} 个作品'
        message = f'{count_label}，监测期内 {len(works)} 个；'+('发布列表核查完成' if complete else '列表完整性无法确认，暂不判定漏报')
        if raw_count is not None:
            message += f'；上游日期筛选后 {len(raw_rows)} 个'
            newest = meta.get('newest_published')
            if type(newest) in (int, float) and 0 < newest <= started:
                message += '；列表最新作品发布于 '+datetime.fromtimestamp(newest,TZ).strftime('%Y/%m/%d %H:%M')
        if missing_reports:
            message += f'；已解析的报送中有 {len(missing_reports)} 个同作者作品未出现在发布列表，不能据此判断未发布'
        message += '。'+('历史补查' if full_scan else '增量巡检')+'，本次查询自 '+earliest_day.strftime('%Y/%m/%d')+'；已保存的历史记录保留'
        if complete and not works:
            message += '。监测开始：'+datetime.fromtimestamp(binding['monitor_start'],TZ).strftime('%Y/%m/%d %H:%M')+'；更早发布的作品不计入本次漏报核查'
        with self.connect() as db:
            current = db.execute('SELECT * FROM ops_bindings WHERE id=?',(binding_id,)).fetchone()
            if not current['enabled'] or current['owner_id']!=binding['owner_id'] or current['monitor_start']!=binding['monitor_start']:
                raise ValueError('巡检期间绑定发生变更，请重新巡检')
            for work in works:
                self.upsert_work(work,current,db)
            if complete:
                if full_scan:
                    db.execute('DELETE FROM ops_verified WHERE binding_id=?',(binding_id,))
                else:
                    db.execute('DELETE FROM ops_verified WHERE binding_id=? AND work_id IN (SELECT id FROM ops_works WHERE binding_id=? AND (published>=? OR published IS NULL))',
                               (binding_id,binding_id,window_start))
                db.executemany('INSERT OR IGNORE INTO ops_verified VALUES(?,?)',[(binding_id,w['id']) for w in works])
                db.execute('INSERT OR REPLACE INTO ops_inspection_progress VALUES(?,?,?,?,?)',
                           (binding_id,binding['owner_id'],binding['monitor_start'],started,started if full_scan else progress['full_checked_at']))
            db.execute('UPDATE ops_bindings SET last_check=?,last_success=?,check_state=?,check_message=?,next_check=? WHERE id=?',
                       (now(),now() if complete else current['last_success'],state,message,now()+self.settings()['interval_minutes']*60,binding_id))
        return state,message

    def refresh_work(self, work_id):
        old = self.query('SELECT * FROM ops_works WHERE id=?',(work_id,),one=True)
        if not old:
            raise ValueError('作品已删除，无法刷新旧记录')
        work = self.detail(work_id, old['binding_id'])
        binding = self.query('SELECT * FROM ops_bindings WHERE id=?',(old['binding_id'],),one=True)
        if work['sec_uid'] != binding['sec_uid']:
            raise ValueError('作品作者与绑定不一致')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM ops_works WHERE id=?',(work_id,)).fetchone():
                raise ValueError('作品已删除，已取消本次数据写入')
            self.upsert_work(work,binding,db)
        return 'complete','作品详情与统计已刷新；缺失指标保留历史成功值'

    def run_due(self):
        if not self.job_lock.acquire(blocking=False):
            return
        try:
            self.execute("UPDATE ops_jobs SET status='cancelled',message='自动巡检已移除，任务已停止',updated=? WHERE kind='inspect' AND status IN ('queued','retrying')",(now(),))
            job = self.query("SELECT * FROM ops_jobs WHERE kind='refresh' AND status IN ('queued','retrying') AND next_retry<=? ORDER BY created LIMIT 1",(now(),),one=True)
            if not job:
                return
            self.execute("UPDATE ops_jobs SET status='running',attempts=attempts+1,updated=?,message='正在采集' WHERE id=?",(now(),job['id']))
            try:
                if job['kind']=='inspect':
                    options=self.query('SELECT full_scan FROM ops_job_options WHERE job_id=?',(job['id'],),one=True)
                    state,message=self.inspect(job['target'],full_scan=bool(options and options['full_scan']))
                else:
                    state,message=self.refresh_work(job['target'])
                status = 'completed' if state=='complete' else 'partial'
                self.execute('UPDATE ops_jobs SET status=?,message=?,updated=? WHERE id=?',(status,message,now(),job['id']))
                self.audit('INFO' if status=='completed' else 'WARN','task','运营采集任务结束',message,job['id'])
            except Exception as exc:
                # Redact through the existing log store before saving the visible error message.
                message = str(exc)
                redact = getattr(getattr(self.audit,'__self__',None),'redact',None)
                message = redact(message) if redact else '采集失败，请检查后台连接、登录凭据与作品权限'
                settings = self.settings()
                status = 'retrying' if job['attempts']<settings['retry_limit'] else 'failed'
                self.execute('UPDATE ops_jobs SET status=?,message=?,updated=?,next_retry=? WHERE id=?',(status,message,now(),now()+settings['retry_minutes']*60,job['id']))
                if job['kind']=='inspect':
                    self.execute("UPDATE ops_bindings SET check_state='error',check_message=?,last_check=?,next_check=? WHERE id=?",('无法核查：'+message,now(),now()+settings['interval_minutes']*60,job['target']))
                else:
                    self.execute('UPDATE ops_works SET last_error=? WHERE id=?',(message,job['target']))
                self.audit('ERROR','task','运营采集任务失败',message,job['id'])
        finally:
            self.job_lock.release()

    def start(self):
        self.schema()
        self.execute("UPDATE ops_jobs SET status='queued',message='服务重启，等待重新执行' WHERE status='running'")
        self.execute("UPDATE ops_report_items SET status='queued',message='服务重启，等待继续解析' WHERE status='processing'")
        self.execute('UPDATE ops_settings SET payload=? WHERE id=1',(json.dumps(self.settings()),))
        def loop():
            while not self.scheduler_stop.wait(3):
                try:
                    self.run_due()
                except Exception:
                    self.audit('ERROR','system','运营调度异常','请检查本地数据库与后台服务')
        threading.Thread(target=loop,daemon=True).start()
        def report_loop():
            delay = .5
            while not self.scheduler_stop.wait(delay):
                try:
                    delay = .05 if self.process_report_due() else .5
                except Exception:
                    self.audit('ERROR', 'system', '作品报送调度异常', '已保留报送记录，请检查本地数据库与后台连接')
        threading.Thread(target=report_loop,daemon=True).start()

    def works(self, actor, filters):
        sql = '''SELECT w.*,COALESCE(x.deleted_at,0) deleted_at,b.check_state,b.check_message,b.monitor_start,b.enabled AS binding_enabled,b.last_check,
          COALESCE(a.employee_name,u.name) AS employee_name,COALESCE(a.group_name,gl.label,g.name) AS group_name,
          COALESCE(a.department_id,'') AS department_id,COALESCE(a.department_name,'') AS department_name,
          s.user_id AS reporter_id,reporter.name AS reporter_name,s.submitted_at,s.late,s.deadline AS submitted_deadline,
          e.note AS explanation,e.updated AS explanation_at,r.action AS review_action,r.note AS review_note,r.updated AS reviewed_at,
          EXISTS(SELECT 1 FROM ops_verified v WHERE v.binding_id=w.binding_id AND v.work_id=w.id) AS verified
          FROM ops_works w JOIN ops_bindings b ON b.id=w.binding_id JOIN ops_users u ON u.id=w.owner_id
          LEFT JOIN ops_groups g ON g.id=w.group_id LEFT JOIN ops_submissions s ON s.work_id=w.id
          LEFT JOIN ops_group_labels gl ON gl.group_id=g.id
          LEFT JOIN ops_users reporter ON reporter.id=s.user_id LEFT JOIN ops_work_attribution a ON a.work_id=w.id
          LEFT JOIN ops_explanations e ON e.work_id=w.id LEFT JOIN ops_reviews r ON r.work_id=w.id
          LEFT JOIN ops_work_deletions x ON x.work_id=w.id WHERE 1=1'''
        values=[]
        if filters.get('deleted')=='1':
            self.admin(actor)
            sql+=' AND x.work_id IS NOT NULL'
        else:
            sql+=' AND x.work_id IS NULL'
        if actor['role']!='admin':
            sql+=' AND w.owner_id=?'; values.append(actor['id'])
        for name,column in [('employee','w.owner_id'),('group','w.group_id'),('binding','w.binding_id'),('department','a.department_id')]:
            if filters.get(name):
                sql+=f' AND {column}=?'; values.append(filters[name])
        bounds = {}
        for name in ('start','end'):
            if filters.get(name):
                text = str(filters[name])
                try:
                    timestamp = stamp(text)
                    if name=='end':
                        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',text):
                            timestamp += 86399
                        elif re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}',text):
                            timestamp += 59
                except (ValueError,OverflowError,OSError):
                    raise ValueError('请填写有效的作品发布时间（北京时间）') from None
                bounds[name] = timestamp
        if 'start' in bounds and 'end' in bounds and bounds['end'] < bounds['start']:
            raise ValueError('发布截止时间不能早于起始时间')
        for name,op in [('start','>='),('end','<=')]:
            if name in bounds:
                sql+=f' AND w.published{op}?'; values.append(bounds[name])
        rows = self.query(sql,values)
        hours = self.settings()['deadline_hours']
        for row in rows:
            row['metrics'] = json.loads(row['metrics'])
            eligible = bool(row['published'] and row['published']>=row['monitor_start'])
            row['deadline'] = row['submitted_deadline'] or (row['published']+hours*3600 if eligible else None)
            if row['submitted_at']:
                state='submitted'
            elif row['review_action']=='exempt':
                state='exempt'
            elif not row['binding_enabled'] or not eligible:
                state='confirm'
            elif row['check_state']=='error':
                state='error'
            elif row['check_state']!='complete' or not row['verified']:
                state='confirm'
            else:
                state='overdue' if now()>row['deadline'] else 'pending'
            row['state']=state
            row['state_label']=STATES[state]
            row['audit_eligible']=eligible
            row['late']=bool(row['late']) if row['late'] is not None else None
        if filters.get('state'):
            rows=[r for r in rows if r['state'] in {'confirm','error'}] if filters['state']=='attention' else [r for r in rows if r['state']==filters['state']]
        if filters.get('search'):
            text=filters['search'].casefold()
            rows=[r for r in rows if text in (r['description']+' '+r['id']+' '+r['nickname']).casefold()]
        sort = filters.get('sort','published')
        def sort_key(row):
            value = row['metrics'].get(sort,{}).get('value') if sort in METRICS else row.get(sort) if sort in {'published','submitted_at'} else row['published']
            return (value is not None, value or 0)
        known=[r for r in rows if sort_key(r)[0]]
        unknown=[r for r in rows if not sort_key(r)[0]]
        known.sort(key=sort_key,reverse=filters.get('order','desc')!='asc')
        rows=known+unknown
        return rows

    def summary(self, rows, actor, filters=None):
        def totals(items):
            result={}
            for key in METRICS:
                values=[r['metrics'].get(key,{}) for r in items]
                known=[v['value'] for v in values if v.get('value') is not None]
                result[key]={'value':sum(known) if known else None,'known':len(known),'missing':len(items)-len(known),
                             'stale':sum(1 for v in values if v.get('value') is not None and not v.get('available'))}
            return result
        result={'works':len(rows),'submitted':sum(r['state']=='submitted' for r in rows),'pending':sum(r['state']=='pending' for r in rows),
                'overdue':sum(r['state']=='overdue' for r in rows),'confirm':sum(r['state']=='confirm' for r in rows),'error':sum(r['state']=='error' for r in rows),
                'late_submissions':sum(r['late'] is True for r in rows),'metrics':totals(rows)}
        result['exempt']=sum(r['state']=='exempt' for r in rows)
        eligible=[r for r in rows if r['audit_eligible']]
        all_confirmed=all(r['check_state']=='complete' and r['binding_enabled'] and r['verified'] for r in eligible)
        bindings=self.bindings(actor)
        filters=filters or {}
        bindings=[b for b in bindings if (not filters.get('employee') or b['owner_id']==filters['employee'])
                  and (not filters.get('group') or b['group_id']==filters['group'])
                  and (not filters.get('binding') or b['id']==filters['binding'])]
        active_bindings=[b for b in bindings if b['enabled']]
        result['publication_complete']=bool(active_bindings) and all(b['check_state']=='complete' for b in active_bindings) and all(r['verified'] for r in eligible)
        result['unknown_accounts']=sum(b['check_state']!='complete' for b in active_bindings)
        all_confirmed=all_confirmed and all(b['check_state']=='complete' for b in bindings if b['enabled'])
        required=[r for r in eligible if r['state']!='exempt']
        result['required_works']=len(required)
        result['submission_rate']=round(sum(bool(r['submitted_at']) for r in required)/len(required)*100,1) if required and all_confirmed else None
        result['rate_note']='按监测期内、已完整核查的应提交作品计算，已排除无需提交的作品' if result['submission_rate'] is not None else '尚无完整核查结果或应提交作品，暂不计算提交率'
        rankings={}
        for key,label in [('owner_id','employee_name'),('group_id','group_name')]:
            groups={}
            for row in rows:
                entry=groups.setdefault(row[key],{'id':row[key],'name':row[label] or '未分组','items':[]})
                entry['items'].append(row)
            rankings[key]=[{'id':v['id'],'name':v['name'],'works':len(v['items']),'metrics':totals(v['items'])} for v in groups.values()]
        result['rankings']=rankings
        if actor['role']=='admin':
            counts=self.reporting_list(actor,filters)['counts']
            result['reporting_counts']=counts
            result['reporting_review']=counts['review']
            result['reporting_failed']=counts['failed']
        return result

    def bindings(self, actor, deleted=False):
        if deleted:
            self.admin(actor)
        where=' WHERE '+('x.binding_id IS NOT NULL' if deleted else 'x.binding_id IS NULL')
        if actor['role']!='admin':
            where+=' AND b.owner_id=?'
        bindings = self.query('''SELECT b.*,p.completed_through,p.full_checked_at,COALESCE(x.deleted_at,0) AS deleted_at,u.name AS employee_name,u.group_id,COALESCE(gl.label,g.name) AS group_name,
                          EXISTS(SELECT 1 FROM ops_account_logins l WHERE l.binding_id=b.id AND l.owner_id=b.owner_id AND l.cookie<>'') AS login_saved,
                          (SELECT status FROM ops_jobs j WHERE j.kind='inspect' AND j.target=b.id ORDER BY j.created DESC,j.rowid DESC LIMIT 1) AS job_status
                          FROM ops_bindings b JOIN ops_users u ON u.id=b.owner_id LEFT JOIN ops_groups g ON g.id=u.group_id
                          LEFT JOIN ops_group_labels gl ON gl.group_id=g.id LEFT JOIN ops_binding_deletions x ON x.binding_id=b.id
                          LEFT JOIN ops_inspection_progress p ON p.binding_id=b.id'''+where,
                          () if actor['role']=='admin' else (actor['id'],))
        self.publication_stats(bindings)
        return bindings

    def publication_stats(self, bindings):
        """Known publishing facts, independent of reporting attribution or API page counts."""
        if not bindings:
            return
        as_of = now()
        by_id = {b['id']: b for b in bindings}
        by_author = {b['sec_uid']: b for b in bindings}
        known = {b['id']: {} for b in bindings}
        reported = {b['id']: {} for b in bindings}

        def remember(binding, work_id, published):
            if (binding and work_id and type(published) in (int, float)
                    and 0 < published <= as_of):
                known[binding['id']][str(work_id)] = int(published)

        def remember_report(binding, work_id, received):
            if (binding and work_id and type(received) in (int, float)
                    and 0 < received <= as_of):
                items = reported[binding['id']]
                key = str(work_id)
                # Use original receipt time, never collection, refresh or approval time.
                items[key] = min(items.get(key, int(received)), int(received))

        for row in self.query('''SELECT w.id,w.binding_id,w.published,s.submitted_at FROM ops_works w
                LEFT JOIN ops_submissions s ON s.work_id=w.id
                LEFT JOIN ops_work_deletions d ON d.work_id=w.id WHERE d.work_id IS NULL'''):
            remember(by_id.get(row['binding_id']), row['id'], row['published'])
            remember_report(by_id.get(row['binding_id']), row['id'], row['submitted_at'])
        # Payloads come from parsed platform details, never from employee-entered dates.
        # A pending/rejected ownership review does not erase the author's publishing fact.
        for row in self.query('''SELECT r.payload,r.received_at FROM ops_report_items r
                LEFT JOIN ops_report_deletions d ON d.item_id=r.id
                WHERE r.payload<>'' AND d.item_id IS NULL ORDER BY r.updated,r.id'''):
            try:
                work = json.loads(row['payload'])
                remember(by_author.get(work.get('sec_uid')), work.get('id'), work.get('published'))
                remember_report(by_author.get(work.get('sec_uid')), work.get('id'), row['received_at'])
            except (ValueError, TypeError, AttributeError):
                continue
        for binding in bindings:
            dates = list(known[binding['id']].values())
            latest = max(dates, default=None)
            latest_id = max(known[binding['id']], key=lambda key: (known[binding['id']][key], key), default=None)
            binding['publication_stats'] = {
                'latest_published': latest,
                'latest_work_id': latest_id,
                'latest_work_reported_at': reported[binding['id']].get(latest_id),
                'monitor_count': sum(binding['monitor_start'] <= date <= as_of for date in dates),
                'period_start': binding['monitor_start'], 'as_of': as_of,
                'scope': 'known',
            }

    def get(self, resource, actor, filters):
        if resource=='daily-targets':
            from daily_report_targets import DailyReportTargets
            return DailyReportTargets(self).list(actor,filters)
        if resource in {'report-progress','report-progress/export'}:
            from report_progress import ReportProgress
            progress = ReportProgress(self)
            return progress.export(actor,filters) if resource.endswith('/export') else progress.list(actor,filters)
        if resource=='reporting':
            return self.reporting_list(actor,filters)
        if resource=='submission-rules':
            return {'deadline_hours':self.settings()['deadline_hours']}
        if resource=='todos':
            rows=[r for r in self.works(actor,filters) if r['state'] not in {'submitted','exempt'}]
            rows.sort(key=lambda r:({'overdue':0,'pending':1,'error':2,'confirm':3}.get(r['state'],4),r['deadline'] or now()))
            return {'items':rows[:10],'total':len(rows)}
        if resource in {'works','summary','export'}:
            rows=self.works(actor,filters)
            if resource=='summary':
                return self.summary(rows,actor,filters)
            if resource=='export':
                exported=[]
                for r in rows:
                    row={'作品ID':r['id'],'视频文案':r['description'],'抖音账号':r['nickname'],'所属员工':r['employee_name'],'所属小组':r['group_name'] or '未分组',
                         '发布时间':datetime.fromtimestamp(r['published'],TZ).isoformat() if r['published'] else '暂无数据','作品链接':r['share_url'],
                         '提交时间':datetime.fromtimestamp(r['submitted_at'],TZ).isoformat() if r['submitted_at'] else '', '核查状态':r['state_label'],
                         '逾期提交':'是' if r['late'] else '否' if r['late'] is False else '不适用','核查说明':r['check_message']}
                    row['员工说明']=r['explanation'] or ''
                    row['管理员处理']=r['review_note'] or ''
                    row['所属部门']=r['department_name'] or '未设置'
                    row['报送人']=r['reporter_name'] or ''
                    for key,name in zip(METRICS,('播放','点赞','评论','收藏','分享')):
                        metric=r['metrics'].get(key,{})
                        row[name]=metric.get('value') if metric.get('value') is not None else '暂无数据'
                        row[name+'状态']='最新值' if metric.get('available') else '历史成功值' if metric.get('value') is not None else '暂无数据'
                        row[name+'更新时间']=datetime.fromtimestamp(metric['updated_at'],TZ).isoformat() if metric.get('updated_at') else ''
                    exported.append(row)
                if not exported:
                    raise ValueError('当前筛选暂无作品可导出')
                self.audit('INFO','system','导出运营数据',f'导出 {len(exported)} 件作品',title=actor['name'])
                return self.exporter('xlsx',exported)
            page=max(1,int(filters.get('page',1)))
            return {'items':rows[(page-1)*30:page*30],'total':len(rows),'page':page,'page_size':30}
        if resource=='bindings':
            return self.bindings(actor,deleted=filters.get('deleted')=='1')
        if resource=='my-jobs':
            return self.query('SELECT * FROM ops_jobs WHERE user_id=? ORDER BY created DESC LIMIT 10',(actor['id'],))
        self.admin(actor)
        if resource=='people':
            return self.roster_people()
        if resource=='settings':
            return self.settings()
        if resource=='jobs':
            return self.query('''SELECT j.*,u.name AS employee_name,CASE WHEN j.kind='inspect' THEN b.nickname ELSE w.description END AS target_name
                              FROM ops_jobs j LEFT JOIN ops_users u ON u.id=j.user_id LEFT JOIN ops_bindings b ON j.target=b.id
                              LEFT JOIN ops_works w ON j.target=w.id LEFT JOIN ops_job_deletions x ON x.job_id=j.id
                              WHERE '''+('x.job_id IS NOT NULL' if filters.get('deleted')=='1' else 'x.job_id IS NULL')+''' ORDER BY j.created DESC LIMIT 100''')
        raise ValueError('运营接口不存在')

    def post(self, resource, data, actor):
        if resource=='daily-targets':
            from daily_report_targets import DailyReportTargets
            return DailyReportTargets(self).save(actor,data)
        if resource=='report-progress/excuse':
            from report_progress import ReportProgress
            return ReportProgress(self).excuse(actor,data)
        if resource in {'employees/delete','jobs/delete','works/delete','bindings/delete','reporting/delete','deleted/purge'}:
            self.admin(actor)
            if data.get('confirmed') is not True:
                raise ValueError('请管理员确认删除后再执行')
        if resource=='directory-deletions/purge':
            return self.purge_legacy_directory_deletions(data,actor)
        if resource=='deleted/purge':
            return self.purge_legacy_deleted(data,actor)
        if resource=='reporting/review':
            return self.review_reports(data,actor)
        if resource in {'reporting/delete','reporting/restore'}:
            return self.remove_reports(data,actor,restore=resource.endswith('/restore'))
        if resource=='employees':
            return self.save_employee(data,actor)
        if resource=='employees/delete':
            return self.delete_employee(data,actor)
        if resource=='employees/restore':
            return self.restore_employee(data,actor)
        if resource in {'jobs/delete','jobs/restore'}:
            return self.remove_job(data,actor,restore=resource.endswith('/restore'))
        if resource in {'works/delete','works/restore'}:
            return self.remove_work(data,actor,restore=resource.endswith('/restore'))
        if resource in {'bindings/delete','bindings/restore'}:
            return self.remove_binding(data,actor,restore=resource.endswith('/restore'))
        if resource=='employees/import':
            return self.import_employees(data,actor)
        if resource=='departments':
            return self.save_department(data,actor)
        if resource=='explain':
            return self.explain(data,actor)
        if resource=='review':
            return self.review(data,actor)
        if resource=='submit':
            return self.submit(data,actor)
        if resource=='users':
            return self.save_user(data,actor)
        if resource=='groups':
            return self.save_group(data,actor)
        if resource=='bindings':
            return self.save_binding(data,actor)
        if resource=='settings':
            return self.save_settings(data,actor)
        if resource=='jobs':
            if data.get('kind')=='inspect':
                raise ValueError('自动巡检已移除，请在未报送人员中查看报送进度')
            return self.enqueue(data.get('kind'),data.get('target'),actor,full_scan=data.get('full_scan',False))
        if resource=='retry':
            return self.retry(data.get('id'),actor)
        raise ValueError('运营接口不存在')
