"""Local roster and durable anonymous reporting; platform reads stay in Operations.detail."""
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlparse, parse_qs


def tick():
    return int(time.time())


def uid():
    return secrets.token_hex(12)


class ReportingMixin:
    def _purge_directory_records(self, db, employee_ids=(), binding_ids=()):
        employees=set(employee_ids)
        bindings=set(binding_ids)
        work_ids=set()
        report_ids=set()
        for employee_id in employees:
            bindings.update(r[0] for r in db.execute('SELECT id FROM ops_bindings WHERE owner_id=?',(employee_id,)))
            work_ids.update(r[0] for r in db.execute('SELECT id FROM ops_works WHERE owner_id=?',(employee_id,)))
            report_ids.update(r[0] for r in db.execute('SELECT id FROM ops_report_items WHERE employee_id=?',(employee_id,)))
        for binding_id in bindings:
            work_ids.update(r[0] for r in db.execute('SELECT id FROM ops_works WHERE binding_id=?',(binding_id,)))
            if db.execute("SELECT 1 FROM ops_jobs WHERE kind='inspect' AND target=? AND status IN ('queued','running','retrying')",(binding_id,)).fetchone():
                raise ValueError('账号有未结束的采集任务，请结束后再删除')
            owner=db.execute('SELECT u.id,u.group_id FROM ops_bindings b JOIN ops_users u ON u.id=b.owner_id WHERE b.id=?',(binding_id,)).fetchone()
            if owner:self.record_scope_change(db,owner['id'],owner['group_id'])
        result=self._purge_work_records(db,work_ids,report_ids)
        for binding_id in bindings:
            for table in ('ops_job_options','ops_job_deletions'):
                db.execute(f"DELETE FROM {table} WHERE job_id IN (SELECT id FROM ops_jobs WHERE kind='inspect' AND target=?)",(binding_id,))
            db.execute("DELETE FROM ops_jobs WHERE kind='inspect' AND target=?",(binding_id,))
            for table in ('ops_verified','ops_inspection_progress','ops_account_logins','ops_binding_deletions'):
                db.execute(f'DELETE FROM {table} WHERE binding_id=?',(binding_id,))
            db.execute('DELETE FROM ops_bindings WHERE id=?',(binding_id,))
        has_targets=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ops_targets'").fetchone()
        for employee_id in employees:
            employee=db.execute('SELECT group_id FROM ops_users WHERE id=?',(employee_id,)).fetchone()
            db.execute('DELETE FROM ops_report_excuses WHERE employee_id=?',(employee_id,))
            db.execute('DELETE FROM ops_daily_report_rules WHERE employee_id=?',(employee_id,))
            if employee:self.record_scope_change(db,employee_id,employee['group_id'])
            db.execute("UPDATE ops_groups SET leader_id='' WHERE leader_id=?",(employee_id,))
            for table,column in [('ops_sessions','user_id'),('ops_employee_profiles','user_id'),('ops_employee_deletions','user_id'),('ops_account_logins','owner_id'),('ops_explanations','user_id')]:
                db.execute(f'DELETE FROM {table} WHERE {column}=?',(employee_id,))
            if has_targets:
                db.execute("DELETE FROM ops_target_exemptions WHERE target_id IN (SELECT id FROM ops_targets WHERE scope='employee' AND target_id=?)",(employee_id,))
                db.execute("DELETE FROM ops_targets WHERE scope='employee' AND target_id=?",(employee_id,))
            db.execute("DELETE FROM ops_users WHERE id=? AND role='employee'",(employee_id,))
        return {**result,'employees':len(employees),'bindings':len(bindings)}

    def purge_legacy_directory_deletions(self,data,actor):
        self.admin(actor)
        if data.get('confirmed') is not True or data.get('permanent') is not True:
            raise ValueError('请管理员确认永久删除后再执行')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            employees=[r[0] for r in db.execute('SELECT user_id FROM ops_employee_deletions')]
            bindings=[r[0] for r in db.execute('SELECT binding_id FROM ops_binding_deletions')]
            result=self._purge_directory_records(db,employees,bindings)
            self.audit('INFO','account','清理旧版删除误作停用的人员和账号',
                       f"删除人员 {result['employees']}，账号 {result['bindings']}；明确停用的正常记录保留；日志保留",title=actor['name'],db=db)
        return result

    @staticmethod
    def _known_link_work_id(link):
        if re.fullmatch(r'\d{6,30}',link):
            return link
        parsed=urlparse(link)
        if parsed.hostname not in {'douyin.com','www.douyin.com'}:
            return None  # Short links cannot be identified without another platform request.
        match=re.search(r'/(?:video|note|photo)/(\d+)',parsed.path)
        value=parse_qs(parsed.query).get('modal_id',parse_qs(parsed.query).get('aweme_id',[None]))[0] or (match[1] if match else None)
        return value if value and re.fullmatch(r'\d{6,30}',value) else None

    def _purge_work_records(self, db, work_ids=(), report_ids=()):
        """One transaction removes a work and its reporting/deduplication data."""
        db.execute('CREATE TEMP TABLE purge_works(id TEXT PRIMARY KEY)')
        db.execute('CREATE TEMP TABLE purge_reports(id TEXT PRIMARY KEY)')
        db.execute('CREATE TEMP TABLE purge_batches(id TEXT PRIMARY KEY)')
        db.executemany('INSERT OR IGNORE INTO purge_works VALUES(?)',[(i,) for i in work_ids if i])
        db.executemany('INSERT OR IGNORE INTO purge_reports VALUES(?)',[(i,) for i in report_ids if i])
        db.execute("INSERT OR IGNORE INTO purge_works SELECT work_id FROM ops_report_items WHERE id IN (SELECT id FROM purge_reports) AND work_id<>''")
        db.execute("INSERT OR IGNORE INTO purge_works SELECT json_extract(payload,'$.id') FROM ops_report_items WHERE id IN (SELECT id FROM purge_reports) AND payload<>'' AND json_extract(payload,'$.id') IS NOT NULL")
        known={r[0] for r in db.execute('SELECT id FROM purge_works')}
        for row in db.execute("SELECT id,link FROM ops_report_items WHERE work_id='' AND payload=''").fetchall():
            if self._known_link_work_id(row['link']) in known:
                db.execute('INSERT OR IGNORE INTO purge_reports VALUES(?)',(row['id'],))
        db.execute('''INSERT OR IGNORE INTO purge_reports SELECT r.id FROM ops_report_items r
          WHERE r.work_id IN (SELECT id FROM purge_works)
          OR json_extract(NULLIF(r.payload,''),'$.id') IN (SELECT id FROM purge_works)
          OR r.link IN (SELECT link FROM ops_report_items WHERE id IN (SELECT id FROM purge_reports))''')
        if db.execute("SELECT 1 FROM ops_report_items WHERE id IN (SELECT id FROM purge_reports) AND status='processing' LIMIT 1").fetchone():
            raise ValueError('相关作品正在识别，暂不能删除，请等待处理结束后刷新')
        if db.execute('''SELECT 1 FROM ops_jobs WHERE status='running' AND
          ((kind='refresh' AND target IN (SELECT id FROM purge_works)) OR
           (kind='inspect' AND target IN (SELECT binding_id FROM ops_works WHERE id IN (SELECT id FROM purge_works)))) LIMIT 1''').fetchone():
            raise ValueError('相关账号或作品正在采集，暂不能删除，请等待任务结束')
        db.execute('INSERT OR IGNORE INTO purge_batches SELECT batch_id FROM ops_report_items WHERE id IN (SELECT id FROM purge_reports)')
        result={'works':db.execute('SELECT COUNT(*) FROM ops_works WHERE id IN (SELECT id FROM purge_works)').fetchone()[0],
                'reports':db.execute('SELECT COUNT(*) FROM ops_report_items WHERE id IN (SELECT id FROM purge_reports)').fetchone()[0],
                'work_ids':[r[0] for r in db.execute('SELECT id FROM purge_works ORDER BY id')]}
        for table in ('ops_report_notes','ops_report_deletions'):
            db.execute(f'DELETE FROM {table} WHERE item_id IN (SELECT id FROM purge_reports)')
        db.execute('DELETE FROM ops_report_items WHERE id IN (SELECT id FROM purge_reports)')
        db.execute('''DELETE FROM ops_report_claims WHERE batch_id IN (SELECT id FROM purge_batches)
          AND NOT EXISTS(SELECT 1 FROM ops_report_items WHERE batch_id=ops_report_claims.batch_id)''')
        db.execute('''DELETE FROM ops_report_batches WHERE id IN (SELECT id FROM purge_batches)
          AND NOT EXISTS(SELECT 1 FROM ops_report_items WHERE batch_id=ops_report_batches.id)''')
        for table in ('ops_submissions','ops_verified','ops_explanations','ops_reviews','ops_work_attribution','ops_work_deletions'):
            db.execute(f'DELETE FROM {table} WHERE work_id IN (SELECT id FROM purge_works)')
        for table in ('ops_job_options','ops_job_deletions'):
            db.execute(f"DELETE FROM {table} WHERE job_id IN (SELECT id FROM ops_jobs WHERE kind='refresh' AND target IN (SELECT id FROM purge_works))")
        db.execute("DELETE FROM ops_jobs WHERE kind='refresh' AND target IN (SELECT id FROM purge_works)")
        db.execute('DELETE FROM ops_works WHERE id IN (SELECT id FROM purge_works)')
        return result

    def purge_legacy_deleted(self, data, actor):
        self.admin(actor)
        if data.get('confirmed') is not True:
            raise ValueError('请管理员确认删除后再执行')
        if data.get('permanent') is not True:
            raise ValueError('删除方式已更新，请刷新页面后确认永久删除')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            works=[r[0] for r in db.execute('SELECT work_id FROM ops_work_deletions')]
            reports=[r[0] for r in db.execute('SELECT item_id FROM ops_report_deletions')]
            result=self._purge_work_records(db,works,reports)
            self.audit('INFO','task','清理此前已删除的作品与报送数据',
                       f"作品 {result['works']} 件，报送 {result['reports']} 条；作品ID：{','.join(result['work_ids'])}；操作日志保留",
                       title=actor['name'],db=db)
        return {**result,'message':'此前已删除的业务数据已清理，操作日志保留'}

    def reporting_schema(self, db):
        db.executescript('''
        CREATE TABLE IF NOT EXISTS ops_departments(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS ops_group_departments(group_id TEXT PRIMARY KEY,department_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ops_group_labels(group_id TEXT PRIMARY KEY,label TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ops_employee_profiles(user_id TEXT PRIMARY KEY,employee_code TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS ops_employee_deletions(user_id TEXT PRIMARY KEY,deleted_at INTEGER NOT NULL,deleted_by TEXT NOT NULL,
          was_enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS ops_job_deletions(job_id TEXT PRIMARY KEY,deleted_at INTEGER NOT NULL,deleted_by TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ops_work_deletions(work_id TEXT PRIMARY KEY,deleted_at INTEGER NOT NULL,deleted_by TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ops_binding_deletions(binding_id TEXT PRIMARY KEY,deleted_at INTEGER NOT NULL,deleted_by TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ops_scope_changes(scope TEXT NOT NULL,subject_id TEXT NOT NULL,changed_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ops_scope_changes_lookup ON ops_scope_changes(scope,subject_id,changed_at);
        CREATE TABLE IF NOT EXISTS ops_report_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ops_report_claims(batch_id TEXT PRIMARY KEY,name TEXT NOT NULL,
          department TEXT NOT NULL,group_name TEXT NOT NULL,employee_code TEXT NOT NULL,login_identity TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ops_report_notes(item_id TEXT PRIMARY KEY,note TEXT NOT NULL,updated INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ops_report_deletions(item_id TEXT PRIMARY KEY,deleted_at INTEGER NOT NULL,deleted_by TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ops_report_batches(id TEXT PRIMARY KEY,source TEXT NOT NULL,request_id TEXT NOT NULL,
          fingerprint TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,created INTEGER NOT NULL,UNIQUE(source,request_id));
        CREATE TABLE IF NOT EXISTS ops_report_items(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,employee_id TEXT NOT NULL,
          group_id TEXT NOT NULL,link TEXT NOT NULL,received_at INTEGER NOT NULL,updated INTEGER NOT NULL,
          status TEXT NOT NULL,work_id TEXT NOT NULL DEFAULT '',payload TEXT NOT NULL DEFAULT '',message TEXT NOT NULL DEFAULT '',
          attempts INTEGER NOT NULL DEFAULT 0,next_retry INTEGER NOT NULL DEFAULT 0,review_note TEXT NOT NULL DEFAULT '',
          reviewer_id TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS ops_report_due ON ops_report_items(status,next_retry,received_at);
        CREATE INDEX IF NOT EXISTS ops_report_work ON ops_report_items(work_id);
        CREATE TABLE IF NOT EXISTS ops_report_excuses(day TEXT NOT NULL,employee_id TEXT NOT NULL,
          reason TEXT NOT NULL,enabled INTEGER NOT NULL,actor_id TEXT NOT NULL,updated INTEGER NOT NULL,
          PRIMARY KEY(day,employee_id));
        CREATE TABLE IF NOT EXISTS ops_daily_report_rules(id INTEGER PRIMARY KEY AUTOINCREMENT,
          employee_id TEXT NOT NULL,kind TEXT NOT NULL,start_day TEXT NOT NULL,quantity INTEGER,
          weekdays TEXT NOT NULL,cutoff TEXT NOT NULL,note TEXT NOT NULL,actor_id TEXT NOT NULL,created INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ops_daily_rule_scope ON ops_daily_report_rules(employee_id,start_day,id);
        CREATE TABLE IF NOT EXISTS ops_report_limits(source TEXT NOT NULL,bucket INTEGER NOT NULL,count INTEGER NOT NULL,
          PRIMARY KEY(source,bucket));
        CREATE TABLE IF NOT EXISTS ops_work_attribution(work_id TEXT PRIMARY KEY,employee_name TEXT NOT NULL,
          group_name TEXT NOT NULL,department_id TEXT NOT NULL,department_name TEXT NOT NULL);
        ''')
        # Older versions set enabled=0 as a side effect of deletion. That removal
        # is not an explicit ban on future public reporting.
        if 'was_enabled' not in {row[1] for row in db.execute('PRAGMA table_info(ops_employee_deletions)')}:
            db.execute('ALTER TABLE ops_employee_deletions ADD COLUMN was_enabled INTEGER NOT NULL DEFAULT 1')
        db.execute("INSERT OR IGNORE INTO ops_report_meta VALUES('receipt_secret',?)", (secrets.token_hex(32),))
        db.execute('INSERT OR IGNORE INTO ops_group_labels SELECT id,name FROM ops_groups')
        # Existing employee rows become roster entries without replacing their IDs or records.
        db.execute("INSERT OR IGNORE INTO ops_employee_profiles SELECT id,username FROM ops_users WHERE role='employee'")
        db.execute('''INSERT OR IGNORE INTO ops_work_attribution
          SELECT w.id,u.name,COALESCE(gl.label,g.name,''),COALESCE(d.id,''),COALESCE(d.name,'') FROM ops_works w
          JOIN ops_users u ON u.id=w.owner_id LEFT JOIN ops_groups g ON g.id=w.group_id
          LEFT JOIN ops_group_labels gl ON gl.group_id=g.id
          LEFT JOIN ops_group_departments gd ON gd.group_id=w.group_id LEFT JOIN ops_departments d ON d.id=gd.department_id''')

    def roster_people(self):
        return {
            'users': [self.public_user(u) for u in self.query('''SELECT u.*,COALESCE(p.employee_code,u.username) employee_code,
                      COALESCE(x.deleted_at,0) deleted_at FROM ops_users u LEFT JOIN ops_employee_profiles p ON p.user_id=u.id
                      LEFT JOIN ops_employee_deletions x ON x.user_id=u.id ORDER BY u.created,u.id''')],
            'groups': self.query('''SELECT g.id,COALESCE(gl.label,g.name) name,g.leader_id,COALESCE(gd.department_id,'') department_id FROM ops_groups g
                        LEFT JOIN ops_group_labels gl ON gl.group_id=g.id
                        LEFT JOIN ops_group_departments gd ON gd.group_id=g.id ORDER BY name'''),
            'departments': self.query('SELECT * FROM ops_departments ORDER BY name'),
        }

    def public_directory(self):
        result = {
            'departments': self.query('SELECT id,name FROM ops_departments ORDER BY name'),
            'groups': self.query('''SELECT g.id,COALESCE(gl.label,g.name) name,COALESCE(gd.department_id,'') department_id FROM ops_groups g
                         LEFT JOIN ops_group_labels gl ON gl.group_id=g.id
                         LEFT JOIN ops_group_departments gd ON gd.group_id=g.id ORDER BY name'''),
            'employees': self.query('''SELECT u.id,u.name,COALESCE(p.employee_code,u.username) employee_code,u.group_id
                         FROM ops_users u LEFT JOIN ops_employee_profiles p ON p.user_id=u.id
                         WHERE u.enabled=1 AND u.role='employee' ORDER BY u.name,u.id'''),
            'deadline_hours': self.settings()['deadline_hours'],
        }
        from daily_report_targets import DailyReportTargets
        from operations import TZ
        from datetime import datetime
        goals = DailyReportTargets(self)
        day = datetime.now(TZ).date().isoformat()
        rules = goals.rules()
        def public_target(target):
            # The public form needs today's requirement, never private rule notes/history.
            return {key: target[key] for key in ('configured', 'quantity', 'cutoff', 'source')}
        result['daily_target'] = public_target(goals.resolve(day, rules=rules))
        for employee in result['employees']:
            employee['daily_target'] = public_target(goals.resolve(day, employee['id'], rules))
            if self.query('SELECT 1 FROM ops_report_excuses WHERE employee_id=? AND day=? AND enabled=1',
                          (employee['id'], day), one=True):
                employee['daily_target'] = {**employee['daily_target'], 'quantity': 0, 'source': '当日免报'}
        return result

    def save_department(self, data, actor):
        self.admin(actor)
        name, department_id = str(data.get('name', '')).strip(), data.get('id') or uid()
        if not 1 <= len(name) <= 40:
            raise ValueError('部门名称需为 1–40 字')
        try:
            self.execute('INSERT INTO ops_departments VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (department_id, name))
        except Exception as exc:
            if 'UNIQUE' in str(exc):
                raise ValueError('部门名称已存在') from exc
            raise
        self.audit('INFO', 'account', '保存部门', title=name)
        return {'id': department_id}

    def save_employee(self, data, actor, db=None):
        self.admin(actor)
        if db is None:
            with self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                result = self.save_employee(data, actor, conn)
            self.audit('INFO', 'account', '保存员工名册', title=str(data.get('name', '')))
            return result
        employee_id = str(data.get('id') or uid())
        existing = db.execute('SELECT * FROM ops_users WHERE id=?', (employee_id,)).fetchone()
        if existing and existing['role'] != 'employee':
            raise ValueError('管理员账号不能通过员工名册修改')
        if existing and db.execute('SELECT 1 FROM ops_employee_deletions WHERE user_id=?',(employee_id,)).fetchone():
            raise ValueError('员工已删除，请先在“已删除”中恢复后再编辑')
        profile = db.execute('SELECT employee_code FROM ops_employee_profiles WHERE user_id=?', (employee_id,)).fetchone()
        name = str(data.get('name', existing['name'] if existing else '')).strip()
        code = str(data.get('employee_code', profile['employee_code'] if profile else '')).strip()
        group = str(data.get('group_id', existing['group_id'] if existing else ''))
        enabled = data.get('enabled', bool(existing['enabled']) if existing else True)
        if not 1 <= len(name) <= 40 or not re.fullmatch(r'[\w.-]{1,40}', code) or not isinstance(enabled, bool):
            raise ValueError('请填写姓名、唯一工号（字母、数字、汉字、_.-）和启用状态')
        if not group or not db.execute('SELECT 1 FROM ops_groups WHERE id=?', (group,)).fetchone():
            raise ValueError('请选择所属小组')
        if db.execute('SELECT 1 FROM ops_employee_profiles WHERE employee_code=? AND user_id<>?', (code, employee_id)).fetchone():
            raise ValueError('员工工号已存在')
        if existing:
            if existing['group_id'] != group or bool(existing['enabled']) != enabled:
                self.record_scope_change(db, employee_id, existing['group_id'], group)
            db.execute('UPDATE ops_users SET name=?,enabled=?,group_id=? WHERE id=?', (name, int(enabled), group, employee_id))
        else:
            db.execute('INSERT INTO ops_users(id,username,name,password,role,enabled,group_id,created) VALUES(?,?,?,?,?,?,?,?)',
                       (employee_id, 'roster_' + employee_id, name, '!', 'employee', int(enabled), group, tick()))
        db.execute('INSERT INTO ops_employee_profiles VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET employee_code=excluded.employee_code',
                   (employee_id, code))
        db.execute('DELETE FROM ops_sessions WHERE user_id=?', (employee_id,))
        return {'id': employee_id}

    def delete_employee(self,data,actor):
        self.admin(actor)
        if data.get('confirmed') is not True or data.get('permanent') is not True:
            raise ValueError('请管理员确认永久删除后再执行')
        employee_id=str(data.get('id') or '')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            employee=db.execute("SELECT * FROM ops_users WHERE id=? AND role='employee'",(employee_id,)).fetchone()
            if not employee:raise ValueError('员工不存在，管理员账号不能在此删除')
            result=self._purge_directory_records(db,[employee_id])
            self.audit('INFO','account','删除员工及关联业务数据',
                       f"账号 {result['bindings']} 个、作品 {result['works']} 件、报送 {result['reports']} 条；只保留操作日志",title=employee['name'],db=db)
        return {**result,'id':employee_id,'message':'员工及关联业务记录已删除；再次报送可重新建档，不等于停用'}

    def restore_employee(self,data,actor):
        self.admin(actor)
        raise ValueError('员工已改为实际删除，不能恢复；需要时请重新添加或报送建档')

    def record_scope_change(self, db, employee_id, old_group='', new_group=''):
        entries = {('employee', employee_id)}
        entries.update(('group', value) for value in (old_group, new_group) if value)
        db.executemany('INSERT INTO ops_scope_changes VALUES(?,?,?)', [(scope, subject, tick()) for scope, subject in entries])

    def save_roster_group(self, data, actor):
        self.admin(actor)
        name, group_id = str(data.get('name', '')).strip(), data.get('id') or uid()
        if not 1 <= len(name) <= 40:
            raise ValueError('小组名称需为 1–40 字')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM ops_group_departments WHERE group_id=?', (group_id,)).fetchone()
            department_id = data.get('department_id', old['department_id'] if old else '')
            if department_id and not db.execute('SELECT 1 FROM ops_departments WHERE id=?', (department_id,)).fetchone():
                raise ValueError('请选择有效部门')
            leader = str(data.get('leader_id', ''))
            if leader and not db.execute('SELECT 1 FROM ops_users WHERE id=? AND enabled=1', (leader,)).fetchone():
                raise ValueError('负责人不存在或已禁用')
            duplicate = db.execute('''SELECT 1 FROM ops_groups g LEFT JOIN ops_group_labels gl ON gl.group_id=g.id
              LEFT JOIN ops_group_departments gd ON gd.group_id=g.id WHERE COALESCE(gl.label,g.name)=?
              AND COALESCE(gd.department_id,'')=? AND g.id<>?''', (name, department_id, group_id)).fetchone()
            if duplicate:
                raise ValueError('该部门中已存在同名小组')
            # Keep the legacy globally-unique internal column while display labels are scoped to a department.
            db.execute('INSERT INTO ops_groups VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET leader_id=excluded.leader_id',
                       (group_id, '_group_' + group_id, leader))
            db.execute('INSERT INTO ops_group_labels VALUES(?,?) ON CONFLICT(group_id) DO UPDATE SET label=excluded.label', (group_id, name))
            db.execute('INSERT OR REPLACE INTO ops_group_departments VALUES(?,?)', (group_id, department_id))
        self.audit('INFO', 'account', '保存运营小组', title=name)
        return {'id': group_id}

    def import_employees(self, data, actor):
        self.admin(actor)
        rows = data.get('rows')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000 or any(not isinstance(r, dict) for r in rows):
            raise ValueError('每次可导入 1–1000 行员工信息')
        ids = []
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for n, row in enumerate(rows, 1):
                department, group = str(row.get('department', '')).strip(), str(row.get('group', '')).strip()
                if not 1 <= len(department) <= 40 or not 1 <= len(group) <= 40:
                    raise ValueError(f'第 {n} 行缺少部门或小组名称')
                dept = db.execute('SELECT id FROM ops_departments WHERE name=?', (department,)).fetchone()
                dept_id = dept['id'] if dept else uid()
                if not dept:
                    db.execute('INSERT INTO ops_departments VALUES(?,?)', (dept_id, department))
                found = db.execute('''SELECT g.id,gd.department_id FROM ops_groups g
                  LEFT JOIN ops_group_labels gl ON gl.group_id=g.id LEFT JOIN ops_group_departments gd ON gd.group_id=g.id
                  WHERE COALESCE(gl.label,g.name)=? AND (gd.department_id=? OR COALESCE(gd.department_id,'')='')
                  ORDER BY CASE WHEN gd.department_id=? THEN 0 ELSE 1 END LIMIT 1''', (group, dept_id, dept_id)).fetchone()
                group_id = found['id'] if found else uid()
                if not found:
                    db.execute('INSERT INTO ops_groups VALUES(?,?,?)', (group_id, '_group_' + group_id, ''))
                    db.execute('INSERT INTO ops_group_labels VALUES(?,?)', (group_id, group))
                db.execute('INSERT OR REPLACE INTO ops_group_departments VALUES(?,?)', (group_id, dept_id))
                code = str(row.get('employee_code', '')).strip()
                existing = db.execute('SELECT user_id FROM ops_employee_profiles WHERE employee_code=?', (code,)).fetchone()
                ids.append(self.save_employee({**row, 'id': existing['user_id'] if existing else None, 'group_id': group_id}, actor, db)['id'])
        self.audit('INFO', 'account', '批量导入员工名册', f'导入 {len(ids)} 人', title=actor['name'])
        return {'count': len(ids), 'ids': ids, 'message': f'已导入 {len(ids)} 条员工名册，无需创建登录密码'}

    def snapshot_work(self, db, work_id, owner_id, group_id):
        db.execute('''INSERT OR IGNORE INTO ops_work_attribution
          SELECT ?,u.name,COALESCE(gl.label,g.name,''),COALESCE(d.id,''),COALESCE(d.name,'') FROM ops_users u
          LEFT JOIN ops_groups g ON g.id=? LEFT JOIN ops_group_labels gl ON gl.group_id=g.id LEFT JOIN ops_group_departments gd ON gd.group_id=g.id
          LEFT JOIN ops_departments d ON d.id=gd.department_id WHERE u.id=?''', (work_id, group_id, owner_id))

    def _report_links(self, text):
        if not isinstance(text, str) or len(text) > 20000:
            raise ValueError('请粘贴不超过 20 条抖音作品链接')
        links = re.findall(r'https?://[^\s<>"，。；、]+', text)
        if not links:
            links = re.findall(r'^\s*(\d{6,30})\s*$', text, re.M)
        links = list(dict.fromkeys(s.rstrip('.,;!?)）】]') for s in links))
        if not 1 <= len(links) <= 20:
            raise ValueError('每次请提交 1–20 条抖音作品链接或作品 ID')
        for link in links:
            if re.fullmatch(r'\d{6,30}', link):
                continue
            parsed = urlparse(link)
            if not parsed.hostname or not (parsed.hostname == 'douyin.com' or parsed.hostname.endswith('.douyin.com')) or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
                raise ValueError('仅支持抖音作品链接，不接受其他网站地址')
            if parsed.hostname != 'v.douyin.com' and not (re.search(r'/(?:video|note|photo)/\d{6,30}(?:/|$)', parsed.path) or
                        any(re.fullmatch(r'\d{6,30}', v[0]) for k, v in parse_qs(parsed.query).items() if k in {'modal_id', 'aweme_id'})):
                raise ValueError('请输入作品链接，不能使用抖音主页链接')
        return links

    def _receipt_token(self, batch, db):
        key = db.execute("SELECT value FROM ops_report_meta WHERE key='receipt_secret'").fetchone()['value']
        return hmac.new(bytes.fromhex(key), (batch['id'] + ':' + batch['fingerprint']).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _report_identities(snapshot):
        """Read both legacy single-account and multi-account server snapshots."""
        candidates = snapshot if isinstance(snapshot, list) else snapshot.get('accounts', [snapshot]) if isinstance(snapshot, dict) else []
        identities = {}
        for account in candidates:
            if isinstance(account, dict) and account.get('sec_uid'):
                item = {key: str(account.get(key) or '') for key in ('sec_uid','nickname','homepage')}
                identities[item['sec_uid']] = item
        return list(identities.values())

    def receive_report(self, data, source, login_identity=None):
        links = self._report_links(data.get('links', ''))
        request_id, employee_id = str(data.get('request_id', '')), str(data.get('employee_id', ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', request_id):
            raise ValueError('报送请求标识无效，请刷新后重试')
        source = hashlib.sha256(str(source).encode()).hexdigest()
        claim = {key: str(data.get('reporter', {}).get(key, '')).strip() for key in ('name','department','group','employee_code')} if isinstance(data.get('reporter', {}), dict) else {}
        if not employee_id and (not all(claim.get(k) for k in ('name','department','group')) or any(len(v)>40 for v in claim.values())):
            raise ValueError('无需登录网站；请填写姓名、部门和小组后报送（每项最多 40 字）')
        identities = self._report_identities(login_identity)
        # Preserve the old single-account shape for stored/old client compatibility.
        identity = identities[0] if len(identities)==1 else {'accounts':identities} if identities else {key:'' for key in ('sec_uid','nickname','homepage')}
        fingerprint = hashlib.sha256(json.dumps([employee_id, links, claim, identity], ensure_ascii=False).encode()).hexdigest()
        restored=[]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = db.execute('SELECT * FROM ops_report_batches WHERE source=? AND request_id=?', (source, request_id)).fetchone()
            if batch:
                if batch['fingerprint'] != fingerprint:
                    raise ValueError('此请求已接收，请勿更改内容后重复使用同一请求标识')
                token = self._receipt_token(batch, db)
            else:
                employee=None
                if employee_id:
                    employee=db.execute("SELECT * FROM ops_users WHERE id=? AND role='employee'",(employee_id,)).fetchone()
                    if not employee:
                        raise ValueError('请选择名册中的员工；如未列出，请填写部门、小组和姓名')
                    self._enable_report_person(db,employee,restored)
                bucket = tick() // 600
                count = db.execute('SELECT count FROM ops_report_limits WHERE source=? AND bucket=?', (source, bucket)).fetchone()
                if count and count['count'] >= 240:
                    from operations import AccessError
                    raise AccessError('报送过于频繁，请稍后重试', 429)
                employee_count = db.execute('SELECT COUNT(DISTINCT batch_id) FROM ops_report_items WHERE employee_id=? AND received_at>?', (employee_id, tick() - 3600)).fetchone()[0]
                if employee_id and employee_count >= 60:
                    from operations import AccessError
                    raise AccessError('该员工本小时报送次数较多，请稍后重试', 429)
                db.execute('DELETE FROM ops_report_limits WHERE bucket<?', (bucket - 2,))
                db.execute('INSERT INTO ops_report_limits VALUES(?,?,1) ON CONFLICT(source,bucket) DO UPDATE SET count=count+1', (source, bucket))
                batch = {'id': uid(), 'fingerprint': fingerprint}
                token = self._receipt_token(batch, db)
                db.execute('INSERT INTO ops_report_batches VALUES(?,?,?,?,?,?)', (batch['id'], source, request_id, fingerprint, hashlib.sha256(token.encode()).hexdigest(), tick()))
                db.execute('INSERT INTO ops_report_claims VALUES(?,?,?,?,?,?)',
                           (batch['id'],claim.get('name',''),claim.get('department',''),claim.get('group',''),claim.get('employee_code',''),json.dumps(identity,ensure_ascii=False)))
                if not employee_id:
                    employee_id = self._report_person(db,claim,restored)
                    employee = db.execute('SELECT id,group_id FROM ops_users WHERE id=?',(employee_id,)).fetchone()
                db.executemany('''INSERT INTO ops_report_items(id,batch_id,employee_id,group_id,link,received_at,updated,status,message)
                   VALUES(?,?,?,?,?,?,?,'queued','已接收，等待后台解析')''',
                   [(uid(), batch['id'], employee_id, employee['group_id'] if employee else '', link, tick(), tick()) for link in links])
        for name in restored:
            self.audit('INFO','account','员工再次报送恢复名册','沿用原人员与历史记录；绑定账号巡检保持原状态',title=name)
        return self.report_receipt({'receipt_token': token})

    def _enable_report_person(self,db,employee,restored):
        """Removal may be undone by a new report; explicit suspension stays blocked."""
        removed=db.execute('SELECT was_enabled FROM ops_employee_deletions WHERE user_id=?',(employee['id'],)).fetchone()
        if removed and removed['was_enabled']:
            db.execute('DELETE FROM ops_employee_deletions WHERE user_id=?',(employee['id'],))
            db.execute('UPDATE ops_users SET enabled=1 WHERE id=?',(employee['id'],))
            self.record_scope_change(db,employee['id'],'',employee['group_id'])
            restored.append(employee['name'])
        elif removed or not employee['enabled']:
            raise ValueError('该人员已停用，请联系管理员启用后再报送')

    def _report_person(self, db, claim, restored):
        """Build a passwordless roster from submitted organization fields, in one transaction."""
        name,department,group = (claim[k] for k in ('name','department','group'))
        code = claim.get('employee_code','')
        if code:
            found = db.execute('''SELECT u.* FROM ops_users u JOIN ops_employee_profiles p ON p.user_id=u.id
              WHERE p.employee_code=?''',(code,)).fetchone()
            if found:
                if found['name']!=name:
                    raise ValueError('工号对应姓名不一致，请核对信息或联系管理员')
                self._enable_report_person(db,found,restored)
                return found['id']
            if not re.fullmatch(r'[\w.-]{1,40}',code):
                raise ValueError('工号只能包含字母、数字、汉字、_.-')
        found = db.execute('''SELECT u.* FROM ops_users u JOIN ops_groups g ON g.id=u.group_id
          LEFT JOIN ops_group_labels gl ON gl.group_id=g.id JOIN ops_group_departments gd ON gd.group_id=g.id
          JOIN ops_departments d ON d.id=gd.department_id WHERE u.role='employee' AND u.name=?
          AND COALESCE(gl.label,g.name)=? AND d.name=?''',(name,group,department)).fetchall()
        if not code and len(found)==1:
            self._enable_report_person(db,found[0],restored)
            return found[0]['id']
        if not code and len(found)>1:
            raise ValueError('存在同部门小组同名人员，请填写工号区分')
        dept = db.execute('SELECT id FROM ops_departments WHERE name=?',(department,)).fetchone()
        dept_id = dept['id'] if dept else uid()
        if not dept:
            db.execute('INSERT INTO ops_departments VALUES(?,?)',(dept_id,department))
        team = db.execute('''SELECT g.id FROM ops_groups g LEFT JOIN ops_group_labels gl ON gl.group_id=g.id
          JOIN ops_group_departments gd ON gd.group_id=g.id WHERE COALESCE(gl.label,g.name)=? AND gd.department_id=?''',(group,dept_id)).fetchone()
        group_id = team['id'] if team else uid()
        if not team:
            db.execute('INSERT INTO ops_groups VALUES(?,?,?)',(group_id,'_group_'+group_id,''))
            db.execute('INSERT INTO ops_group_labels VALUES(?,?)',(group_id,group))
            db.execute('INSERT INTO ops_group_departments VALUES(?,?)',(group_id,dept_id))
        employee_id = uid()
        db.execute('INSERT INTO ops_users VALUES(?,?,?,?,?,?,?,?)',(employee_id,'roster_'+employee_id,name,'!','employee',1,group_id,tick()))
        db.execute('INSERT INTO ops_employee_profiles VALUES(?,?)',(employee_id,code or 'R'+employee_id[:12]))
        return employee_id

    def report_note(self,data):
        receipt = self.report_receipt(data)
        item_id, note = str(data.get('item_id','')),str(data.get('note','')).strip()
        if not any(item['id']==item_id for item in receipt['items']):
            from operations import AccessError
            raise AccessError('回执中不存在此作品',404)
        if len(note)>1000:
            raise ValueError('备注最多 1000 字')
        self.execute('INSERT INTO ops_report_notes VALUES(?,?,?) ON CONFLICT(item_id) DO UPDATE SET note=excluded.note,updated=excluded.updated',(item_id,note,tick()))
        return {'message':'备注已保存，管理员可查看','note':note}

    def report_receipt(self, data):
        from operations import AccessError
        token = str(data.get('receipt_token', ''))
        if not re.fullmatch(r'[a-f0-9]{64}', token):
            raise AccessError('回执不存在或访问凭据无效', 404)
        batch = self.query('SELECT id FROM ops_report_batches WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),), one=True)
        if not batch:
            raise AccessError('回执不存在或访问凭据无效', 404)
        rows = self.query('''SELECT r.id,r.status,r.link,r.received_at,r.work_id,r.payload,r.message,
          COALESCE(x.deleted_at,0) deleted_at FROM ops_report_items r LEFT JOIN ops_report_deletions x ON x.item_id=r.id
          WHERE r.batch_id=? ORDER BY r.rowid''', (batch['id'],))
        for row in rows:
            if row['deleted_at']:
                row['status']='removed'
                row['message']='管理员已移除此报送记录；已归档作品及首次报送时间仍保留。如需处理请联系管理员恢复。'
            payload = json.loads(row.pop('payload') or '{}')
            row['description'] = payload.get('description', '')
            row['author'] = payload.get('nickname', '')
            row['published'] = payload.get('published')
            note=self.query('SELECT note,updated FROM ops_report_notes WHERE item_id=?',(row['id'],),one=True)
            row['reporter_note'] = note['note'] if note else ''
        return {'receipt_token': token, 'items': rows}

    def _file_report(self, db, report, work, binding, owner_id):
        if db.execute('SELECT 1 FROM ops_submissions WHERE work_id=?', (work['id'],)).fetchone():
            db.execute("UPDATE ops_report_items SET status='duplicate',message='该作品已有有效报送记录，无需重复提交',updated=? WHERE id=?", (tick(), report['id']))
            return
        previous = db.execute('SELECT owner_id FROM ops_works WHERE id=?', (work['id'],)).fetchone()
        if previous and previous['owner_id'] != owner_id:
            raise ValueError('历史作品已有其他责任人，请核实历史归属，不能直接改写')
        self.upsert_work(work, binding, db)
        # Capture the group at initial receipt, not a later employee transfer or review time.
        if not previous and owner_id == report['employee_id']:
            db.execute('UPDATE ops_works SET group_id=? WHERE id=?', (report['group_id'], work['id']))
            db.execute('DELETE FROM ops_work_attribution WHERE work_id=?', (work['id'],))
            self.snapshot_work(db, work['id'], owner_id, report['group_id'])
        deadline = work['published'] + self.settings()['deadline_hours'] * 3600 if work['published'] and work['published'] >= binding['monitor_start'] else None
        db.execute('INSERT INTO ops_submissions VALUES(?,?,?,?,?,?)',
                   (uid(), work['id'], report['employee_id'] or owner_id, report['received_at'], int(report['received_at'] > deadline) if deadline else None, deadline))
        db.execute("UPDATE ops_report_items SET status='submitted',message='作品已报送，已记录首次接收时间',updated=? WHERE id=?", (tick(), report['id']))

    def process_report_due(self):
        if not self.report_lock.acquire(blocking=False):
            return False
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                report = db.execute("""SELECT r.* FROM ops_report_items r WHERE r.status='queued' AND r.next_retry<=?
                  AND NOT EXISTS(SELECT 1 FROM ops_report_deletions x WHERE x.item_id=r.id)
                  ORDER BY r.received_at,r.rowid LIMIT 1""", (tick(),)).fetchone()
                if not report:
                    return False
                report = dict(report)
                db.execute("UPDATE ops_report_items SET status='processing',attempts=attempts+1,updated=?,message='正在解析作品信息' WHERE id=?", (tick(), report['id']))
            try:
                work = self.detail(report['link'], owner_id=report['employee_id'])
                with self.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if not db.execute("SELECT 1 FROM ops_report_items WHERE id=? AND status='processing'",(report['id'],)).fetchone():
                        return True  # The item was removed while the platform request was in flight.
                    db.execute('UPDATE ops_report_items SET work_id=?,payload=?,updated=? WHERE id=?', (work['id'], json.dumps(work, ensure_ascii=False), tick(), report['id']))
                    binding = db.execute('SELECT * FROM ops_bindings WHERE sec_uid=?', (work['sec_uid'],)).fetchone()
                    existing_work = db.execute('SELECT owner_id FROM ops_works WHERE id=?', (work['id'],)).fetchone()
                    expected_owner = existing_work['owner_id'] if existing_work else binding['owner_id'] if binding else None
                    employee = db.execute("SELECT enabled FROM ops_users WHERE id=? AND role='employee'", (report['employee_id'],)).fetchone()
                    claim = db.execute('SELECT login_identity FROM ops_report_claims WHERE batch_id=?',(report['batch_id'],)).fetchone()
                    identities = self._report_identities(json.loads(claim['login_identity']) if claim else {})
                    matching = next((i for i in identities if i['sec_uid']==work['sec_uid']), None)
                    if db.execute('SELECT 1 FROM ops_submissions WHERE work_id=?', (work['id'],)).fetchone():
                        db.execute("UPDATE ops_report_items SET status='duplicate',message='该作品已有有效报送记录，无需重复提交',updated=? WHERE id=?", (tick(), report['id']))
                        message = None
                    elif identities and not matching:
                        message = '作品作者未匹配任何已核验抖音账号，请管理员核实代报或归属；不会自动归档'
                    elif not report['employee_id']:
                        message = '已接收姓名报送，请管理员确认名册身份和作品归属'
                    elif not employee or not employee['enabled']:
                        message = '报送人员已停用，请管理员核实责任人'
                    elif not matching:
                        message = '未完成抖音登录账号核验，请管理员核实；可在回执补充备注'
                    elif binding and not binding['enabled']:
                        message = '管理员已明确停用此账号，请管理员核实'
                    else:
                        message = ''
                    if message:
                        db.execute("UPDATE ops_report_items SET status='review',message=?,updated=? WHERE id=?", (message, tick(), report['id']))
                    elif message is not None:
                        if not binding:
                            binding_id=uid()
                            expected_owner=expected_owner or report['employee_id']
                            db.execute('INSERT INTO ops_bindings(id,sec_uid,uid,nickname,homepage,owner_id,monitor_start) VALUES(?,?,?,?,?,?,?)',
                              (binding_id,work['sec_uid'],work.get('uid',''),work.get('nickname') or '抖音账号','https://www.douyin.com/user/'+work['sec_uid'],expected_owner,report['received_at']))
                            binding=db.execute('SELECT * FROM ops_bindings WHERE id=?',(binding_id,)).fetchone()
                        self._file_report(db, report, work, binding, expected_owner)
                self.audit('INFO', 'task', '免登录作品报送已解析', task_id=report['id'])
            except Exception:
                # Never expose upstream cookies, headers, URLs or tracebacks in public receipts.
                settings = self.settings()
                retry = report['attempts'] < settings['retry_limit']
                message = '暂时无法读取作品，后台将自动重试' if retry else '作品信息读取失败，已保留链接，请管理员检查或重试'
                self.execute("UPDATE ops_report_items SET status=?,message=?,next_retry=?,updated=? WHERE id=?",
                             ('queued' if retry else 'failed', message, tick() + settings['retry_minutes'] * 60, tick(), report['id']))
                self.audit('WARN', 'task', '免登录报送解析失败', message, task_id=report['id'])
            return True
        finally:
            self.report_lock.release()

    def reporting_list(self, actor, filters):
        self.admin(actor)
        conditions, values = ['x.item_id IS NOT NULL' if filters.get('deleted')=='1' else 'x.item_id IS NULL'], []
        for key, column in [('employee', 'r.employee_id'), ('group', 'r.group_id'), ('department', 'gd.department_id')]:
            if filters.get(key):
                conditions.append(column + '=?')
                values.append(filters[key])
        if filters.get('date'):
            from report_progress import ReportProgress
            _, start, end = ReportProgress(self).bounds(filters['date'])
            conditions.extend(['r.received_at>=?', 'r.received_at<?'])
            values.extend([start,end])
        sql = '''SELECT r.*,COALESCE(u.name,c.name) employee_name,COALESCE(u.name,c.name) reporter_name,COALESCE(p.employee_code,u.username,c.employee_code) employee_code,
          COALESCE(gl.label,g.name,c.group_name) group_name,COALESCE(d.name,c.department) department_name,c.login_identity,
          COALESCE(n.note,'') reporter_note,n.updated note_updated,COALESCE(x.deleted_at,0) deleted_at,
          COALESCE(w.owner_id,b.owner_id) owner_id,owner.name owner_name FROM ops_report_items r LEFT JOIN ops_users u ON u.id=r.employee_id
          LEFT JOIN ops_report_claims c ON c.batch_id=r.batch_id
          LEFT JOIN ops_report_deletions x ON x.item_id=r.id
          LEFT JOIN ops_report_notes n ON n.item_id=r.id
          LEFT JOIN ops_employee_profiles p ON p.user_id=u.id
          LEFT JOIN ops_groups g ON g.id=r.group_id LEFT JOIN ops_group_labels gl ON gl.group_id=g.id LEFT JOIN ops_group_departments gd ON gd.group_id=g.id
          LEFT JOIN ops_departments d ON d.id=gd.department_id
          LEFT JOIN ops_bindings b ON b.sec_uid=json_extract(NULLIF(r.payload,''),'$.sec_uid')
          LEFT JOIN ops_works w ON w.id=r.work_id LEFT JOIN ops_users owner ON owner.id=COALESCE(w.owner_id,b.owner_id)'''
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        rows = self.query(sql + ' ORDER BY r.received_at DESC,r.rowid DESC', values)
        for row in rows:
            work = json.loads(row.pop('payload') or '{}')
            row.update({key: work.get(key, '') for key in ('description', 'nickname', 'sec_uid','published')})
            identities = self._report_identities(json.loads(row.pop('login_identity') or '{}'))
            matching = next((i for i in identities if i['sec_uid']==work.get('sec_uid')), None)
            row['login_accounts'] = identities
            row['login_account'] = matching or (identities[0] if len(identities)==1 else {})
            row['account_match'] = ('match' if matching else 'mismatch') if identities and work.get('sec_uid') else 'unverified'
            row.pop('batch_id', None)
        if filters.get('search'):
            search = str(filters['search']).casefold()
            rows = [r for r in rows if search in ' '.join(str(r.get(k) or '') for k in ('description', 'employee_name', 'employee_code', 'work_id', 'nickname', 'link')).casefold()]
        counts = {state: sum(r['status'] == state for r in rows) for state in ('queued', 'processing', 'submitted', 'review', 'failed', 'duplicate', 'rejected')}
        status = filters.get('status') or filters.get('state')
        if status:
            rows = [r for r in rows if r['status'] == status]
        page = min(max(1, int(filters.get('page', 1))),max(1,(len(rows)+29)//30))
        return {'items': rows[(page - 1) * 30:page * 30], 'total': len(rows), 'page': page, 'page_size': 30, 'counts': counts}

    def remove_reports(self,data,actor,restore=False):
        self.admin(actor)
        if restore:
            raise ValueError('报送数据已改为实际删除，不能从页面恢复；需要时请重新报送')
        ids=data.get('ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(i,str) or not i for i in ids):
            raise ValueError('请选择 1–100 条报送记录')
        if not restore and data.get('confirmed') is not True:
            raise ValueError('请管理员确认删除后再执行')
        if data.get('permanent') is not True:
            raise ValueError('删除方式已更新，请刷新页面后确认永久删除')
        ids=list(dict.fromkeys(ids))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT id,status FROM ops_report_items WHERE id IN (%s)' % ','.join('?' for _ in ids),ids).fetchall()
            if len(rows)!=len(ids):
                raise ValueError('部分报送记录不存在，请刷新列表')
            if any(row['status']=='processing' for row in rows):
                raise ValueError('正在识别的报送暂不能删除，请等待处理结束后刷新')
            result=self._purge_work_records(db,report_ids=ids)
            self.audit('INFO','task','删除员工报送及关联作品数据',
                       f"选中 {len(ids)} 条，清理作品 {result['works']} 件、报送 {result['reports']} 条；作品ID：{','.join(result['work_ids'])}；操作日志保留",
                       title=actor['name'],db=db)
        return {**result,'message':f"已删除 {result['reports']} 条报送及 {result['works']} 件关联作品的数据，日志保留，可重新报送"}

    def review_reports(self, data, actor):
        self.admin(actor)
        ids, action, note = data.get('ids'), data.get('action'), str(data.get('note', '')).strip()
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(not isinstance(i, str) for i in ids) or action not in {'approve', 'reject', 'retry'}:
            raise ValueError('请选择 1–100 条报送记录和有效操作')
        if len(note) > 1000 or (action == 'reject' and len(note) < 2):
            raise ValueError('驳回时请填写至少两个字的处理原因')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            new_owner = ''
            if action == 'approve' and data.get('create_reporter'):
                claims = db.execute('''SELECT r.employee_id,c.* FROM ops_report_items r
                  JOIN ops_report_claims c ON c.batch_id=r.batch_id WHERE r.id IN (%s)''' % ','.join('?' for _ in ids), ids).fetchall()
                if len(claims)!=len(set(ids)) or any(c['employee_id'] for c in claims) or len({(c['name'],c['department'],c['group_name'],c['employee_code']) for c in claims})!=1:
                    raise ValueError('仅可为同一姓名报送人建档，请分批处理不同人员')
                person = data.get('new_reporter') or {}
                name, department, group = (str(person.get(k) or '').strip() for k in ('name','department','group'))
                code = str(person.get('employee_code') or ('R'+uid()[:12])).strip()
                if not all(1<=len(v)<=40 for v in (name,department,group)):
                    raise ValueError('建档时请填写姓名、部门和小组（各 1–40 字）')
                dept = db.execute('SELECT id FROM ops_departments WHERE name=?',(department,)).fetchone()
                dept_id = dept['id'] if dept else uid()
                if not dept:
                    db.execute('INSERT INTO ops_departments VALUES(?,?)',(dept_id,department))
                found = db.execute('''SELECT g.id FROM ops_groups g LEFT JOIN ops_group_labels gl ON gl.group_id=g.id
                  LEFT JOIN ops_group_departments gd ON gd.group_id=g.id WHERE COALESCE(gl.label,g.name)=? AND gd.department_id=?''',(group,dept_id)).fetchone()
                group_id = found['id'] if found else uid()
                if not found:
                    db.execute('INSERT INTO ops_groups VALUES(?,?,?)',(group_id,'_group_'+group_id,''))
                    db.execute('INSERT INTO ops_group_labels VALUES(?,?)',(group_id,group))
                    db.execute('INSERT INTO ops_group_departments VALUES(?,?)',(group_id,dept_id))
                new_owner = self.save_employee({'name':name,'employee_code':code,'group_id':group_id},actor,db)['id']
            for report_id in dict.fromkeys(ids):
                if db.execute('SELECT 1 FROM ops_report_deletions WHERE item_id=?',(report_id,)).fetchone():
                    raise ValueError('报送记录已删除，请先恢复后再处理')
                report = db.execute('SELECT * FROM ops_report_items WHERE id=?', (report_id,)).fetchone()
                if not report or report['status'] not in {'review', 'failed'}:
                    raise ValueError('仅待核实或失败的记录可以处理，请刷新列表')
                if action == 'retry':
                    db.execute("UPDATE ops_report_items SET status='queued',attempts=0,next_retry=0,message='管理员已安排重新解析',updated=? WHERE id=?", (tick(), report_id))
                elif action == 'reject':
                    db.execute("UPDATE ops_report_items SET status='rejected',message='管理员已驳回：'||?,updated=? WHERE id=?", (note, tick(), report_id))
                else:
                    work = json.loads(report['payload'] or '{}')
                    if not work:
                        raise ValueError('作品尚未成功解析，请先重试')
                    owner_id = new_owner or str(data.get('owner_id') or report['employee_id'])
                    if not db.execute("SELECT 1 FROM ops_users WHERE id=? AND enabled=1 AND role='employee'", (owner_id,)).fetchone():
                        raise ValueError('请选择已启用的员工责任人')
                    binding = db.execute('SELECT * FROM ops_bindings WHERE sec_uid=?', (work['sec_uid'],)).fetchone()
                    existing_work = db.execute('SELECT owner_id FROM ops_works WHERE id=?', (work['id'],)).fetchone()
                    expected_owner = existing_work['owner_id'] if existing_work else binding['owner_id'] if binding else owner_id
                    if binding and (expected_owner != owner_id or not binding['enabled']):
                        raise ValueError('此账号已有其他责任人或已停用，请选择已确认责任人，或先在账号管理中处理')
                    if not binding:
                        binding_id = uid()
                        # Monitoring begins when the account is explicitly confirmed. Earlier publication can be submitted without invented overdue status.
                        db.execute('INSERT INTO ops_bindings(id,sec_uid,uid,nickname,homepage,owner_id,monitor_start) VALUES(?,?,?,?,?,?,?)',
                                   (binding_id, work['sec_uid'], work.get('uid', ''), work.get('nickname') or '新抖音账号',
                                    'https://www.douyin.com/user/' + work['sec_uid'], owner_id, report['received_at']))
                        binding = db.execute('SELECT * FROM ops_bindings WHERE id=?', (binding_id,)).fetchone()
                    self._file_report(db, report, work, binding, owner_id)
                db.execute('UPDATE ops_report_items SET review_note=?,reviewer_id=? WHERE id=?', (note, actor['id'], report_id))
        self.audit('INFO', 'task', '批量处理作品报送', f'{action}，{len(set(ids))} 条', title=actor['name'])
        return {'message': f'已处理 {len(set(ids))} 条报送记录'}
