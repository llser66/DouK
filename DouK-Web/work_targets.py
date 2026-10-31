"""Optional publication goals; calculated from existing collected works only."""
from datetime import date, datetime
import re
import secrets

from operations import TZ, now


class WorkTargets:
    def __init__(self, ops):
        self.ops = ops

    def schema(self):
        with self.ops.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS ops_targets(
              id TEXT PRIMARY KEY,scope TEXT NOT NULL,target_id TEXT NOT NULL,
              start TEXT NOT NULL,end TEXT NOT NULL,quantity INTEGER NOT NULL,
              note TEXT NOT NULL,enabled INTEGER NOT NULL,created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ops_target_exemptions(
              id TEXT PRIMARY KEY,target_id TEXT NOT NULL,date TEXT NOT NULL,
              quantity INTEGER NOT NULL,reason TEXT NOT NULL,actor_id TEXT NOT NULL,created INTEGER NOT NULL);
            ''')

    @staticmethod
    def day(value):
        text = str(value or '')
        if len(text) != 10:
            raise ValueError('日期请使用 YYYY-MM-DD')
        return date.fromisoformat(text)

    @classmethod
    def moment(cls, value, *, end=False):
        """Beijing minute bounds; legacy date-only ends still include the whole day."""
        text = str(value or '')
        try:
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
                stamp = int(datetime.combine(cls.day(text), datetime.min.time(), TZ).timestamp())
                return text, stamp + (86400 if end else 0)
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', text):
                stamp = int(datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp())
                return text, stamp + (60 if end else 0)
        except (ValueError, OverflowError, OSError):
            pass
        raise ValueError('请填写有效的北京时间，格式为 YYYY-MM-DDTHH:MM')

    @classmethod
    def bounds(cls, row):
        return cls.moment(row['start'])[1], cls.moment(row['end'], end=True)[1]

    @classmethod
    def contains_day(cls, target, day):
        start, end = cls.bounds(target)
        day_start = cls.moment(day)[1]
        return start < day_start + 86400 and end > day_start

    def save(self, data, actor):
        self.ops.admin(actor)
        self.schema()
        target_id = str(data.get('id') or secrets.token_hex(6))
        scope, subject = data.get('scope'), data.get('target_id')
        if scope not in {'employee', 'group'}:
            raise ValueError('请选择员工或小组目标')
        table = 'ops_users' if scope == 'employee' else 'ops_groups'
        sql = f'SELECT id FROM {table} WHERE id=?'
        if scope == 'employee':
            sql += " AND role='employee' AND enabled=1"
        if not self.ops.query(sql, (subject,), one=True):
            raise ValueError('目标所属员工或小组不存在')
        start, start_stamp = self.moment(data.get('start'))
        end, end_stamp = self.moment(data.get('end'), end=True)
        if end_stamp <= start_stamp or end_stamp - start_stamp > 367 * 86400:
            raise ValueError('结束时间不能早于开始时间，目标周期最长 367 天')
        quantity = data.get('quantity')
        if isinstance(quantity, bool) or not str(quantity).isdigit() or not 1 <= int(quantity) <= 100000:
            raise ValueError('目标数量应为 1–100000 的整数')
        enabled = data.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ValueError('启用状态无效')
        note = str(data.get('note') or '').strip()[:1000]
        with self.ops.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if enabled:
                others = db.execute('SELECT start,end FROM ops_targets WHERE scope=? AND target_id=? AND id<>? AND enabled=1',
                                    (scope, subject, target_id)).fetchall()
                if any(start_stamp < self.bounds(other)[1] and end_stamp > self.bounds(other)[0] for other in others):
                    raise ValueError('该员工或小组已有重叠周期的目标，请调整原目标')
            old = db.execute('SELECT * FROM ops_targets WHERE id=?', (target_id,)).fetchone()
            if old and db.execute('SELECT 1 FROM ops_target_exemptions WHERE target_id=?', (target_id,)).fetchone():
                if (old['scope'],old['target_id'],self.bounds(old),old['quantity']) != (scope,subject,(start_stamp,end_stamp),int(quantity)):
                    raise ValueError('目标已有减免记录，请停用后另建目标，保留原记录')
            db.execute('INSERT INTO ops_targets VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET scope=excluded.scope,target_id=excluded.target_id,start=excluded.start,end=excluded.end,quantity=excluded.quantity,note=excluded.note,enabled=excluded.enabled',
                       (target_id,scope,subject,start,end,int(quantity),note,int(enabled),now()))
        self.ops.audit('INFO','system','保存发布目标',title=actor['name'])
        return {'id':target_id,'message':'发布目标已保存；未设置目标的人员不判断工作缺额'}

    def exempt(self, data, actor):
        self.ops.admin(actor)
        self.schema()
        target = self.ops.query('SELECT * FROM ops_targets WHERE id=?', (data.get('id'),), one=True)
        if not target:
            raise ValueError('目标不存在')
        day = self.day(data.get('date')).isoformat()
        quantity = data.get('quantity')
        if isinstance(quantity, bool) or not str(quantity).isdigit() or int(quantity) < 1:
            raise ValueError('请填写有效减免数量')
        reason = str(data.get('reason') or '').strip()
        if not 2 <= len(reason) <= 1000 or not self.contains_day(target, day):
            raise ValueError('请填写周期内的日期和 2–1000 字减免原因')
        with self.ops.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            target = db.execute('SELECT * FROM ops_targets WHERE id=?', (target['id'],)).fetchone()
            if not target or not self.contains_day(target, day):
                raise ValueError('减免日期必须在当前目标周期内')
            total = db.execute('SELECT COALESCE(SUM(quantity),0) FROM ops_target_exemptions WHERE target_id=?', (target['id'],)).fetchone()[0]
            if total + int(quantity) > target['quantity']:
                raise ValueError('累计减免不能超过原目标数量')
            db.execute('INSERT INTO ops_target_exemptions VALUES(?,?,?,?,?,?,?)',
                       (secrets.token_hex(6),target['id'],day,int(quantity),reason,actor['id'],now()))
        self.ops.audit('INFO','system','发布目标减免',reason,title=actor['name'])
        return {'message':'减免已记录，原目标和原因均保留'}

    def list(self, actor, filters):
        self.ops.admin(actor)
        self.schema()
        rows = self.ops.query('''SELECT t.*,CASE WHEN t.scope='employee' THEN u.name ELSE COALESCE(gl.label,g.name) END AS name
          FROM ops_targets t LEFT JOIN ops_users u ON t.scope='employee' AND u.id=t.target_id
          LEFT JOIN ops_groups g ON t.scope='group' AND g.id=t.target_id
          LEFT JOIN ops_group_labels gl ON gl.group_id=g.id ORDER BY t.start DESC,t.created DESC''')
        items=[]
        for row in rows:
            if filters.get('scope') and row['scope'] != filters['scope']:
                continue
            if filters.get('target_id') and row['target_id'] != filters['target_id']:
                continue
            start, end = self.bounds(row)
            key = 'employee' if row['scope'] == 'employee' else 'group'
            works = self.ops.works(actor,{key:row['target_id']})
            works = [w for w in works if w['published'] is not None and start <= w['published'] < end and w['audit_eligible']]
            bindings = [b for b in self.ops.bindings(actor) if b['enabled'] and b['monitor_start'] <= start and
                        (b['owner_id'] == row['target_id'] if key == 'employee' else b['group_id'] == row['target_id'])]
            # A recently/partially inspected account cannot prove a whole period's total.
            active = [b for b in self.ops.bindings(actor) if b['enabled'] and
                      (b['owner_id'] == row['target_id'] if key == 'employee' else b['group_id'] == row['target_id'])]
            freshness = self.ops.settings()['interval_minutes'] * 60
            covered = now() >= start and bool(active) and len(bindings)==len(active) and all(
                b['check_state']=='complete' and b['last_check'] and b['last_check'] >= min(end,now())-freshness for b in active)
            covered = covered and all(w['verified'] for w in works)
            changed = self.ops.query('''SELECT 1 FROM ops_scope_changes WHERE scope=? AND subject_id=?
               AND changed_at>=? AND changed_at<? LIMIT 1''', (row['scope'],row['target_id'],start,end),one=True)
            if changed:
                covered = False
            exclusions=self.ops.query('SELECT * FROM ops_target_exemptions WHERE target_id=? ORDER BY created',(row['id'],))
            exempt=sum(e['quantity'] for e in exclusions)
            required=max(0,row['quantity']-exempt)
            actual=len({w['id'] for w in works})
            row.update(exempt=exempt,exempt_quantity=exempt,required=required,actual=actual,verified=covered,
                       shortfall=max(0,required-actual) if covered else None,
                       complete=(actual>=required) if covered or required==0 else None,
                       progress=round(min(100,actual/required*100),1) if covered and required else 100 if required==0 else None,
                       exemptions=exclusions,period_ended=now()>=end,
                       start_time=datetime.fromtimestamp(start,TZ).strftime('%Y-%m-%dT%H:%M'),
                       end_time=datetime.fromtimestamp(end-60,TZ).strftime('%Y-%m-%dT%H:%M'))
            items.append(row)
        # Employee and group targets may overlap; never sum them into a false company total.
        summary={scope:{'target':sum(r['required'] for r in items if r['scope']==scope and r['enabled']),
                        'unverifiable':sum(not r['verified'] for r in items if r['scope']==scope and r['enabled'])}
                 for scope in ('employee','group')}
        return {'items':items,'summary':summary}
