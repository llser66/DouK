"""Versioned daily goals, counted only from accepted, deduplicated local works."""
from datetime import date, datetime
import json
import re


class DailyReportTargets:
    def __init__(self, ops):
        self.ops = ops

    @staticmethod
    def day(value):
        text = str(value or '')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
            raise ValueError('请选择有效的生效日期')
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise ValueError('请选择有效的生效日期') from None

    def rules(self):
        return self.ops.query('SELECT * FROM ops_daily_report_rules ORDER BY start_day,id')

    def resolve(self, day, employee='', rules=None):
        rules = self.rules() if rules is None else rules
        eligible = [r for r in rules if r['start_day'] <= day and
                    (r['kind'] == 'recurring' or r['start_day'] == day)]
        defaults = [r for r in eligible if not r['employee_id']]
        base = max(defaults, key=lambda r: (r['start_day'], r['id']), default=None)
        if not base:
            return {'configured': False, 'quantity': None, 'cutoff': '23:59',
                    'weekdays': list(range(7)), 'source': '未设置', 'start_day': ''}
        weekdays = json.loads(base['weekdays'])
        quantity = base['quantity'] if date.fromisoformat(day).weekday() in weekdays else 0
        result = {'configured': True, 'quantity': quantity, 'cutoff': base['cutoff'],
                  'base_quantity': base['quantity'],
                  'weekdays': weekdays, 'source': '统一要求' if quantity else '休息日 / 无需报送',
                  'start_day': base['start_day'], 'note': base['note'], 'personal': False}
        personal = [r for r in eligible if employee and r['employee_id'] == employee]
        recurring = max((r for r in personal if r['kind'] == 'recurring'),
                        key=lambda r: (r['start_day'], r['id']), default=None)
        single = max((r for r in personal if r['kind'] == 'day'), key=lambda r: r['id'], default=None)
        chosen = single if single and single['quantity'] is not None else recurring
        if chosen and chosen['quantity'] is not None:
            # A single day can explicitly schedule work on a normal rest day.
            result.update(quantity=chosen['quantity'] if chosen['kind'] == 'day' or
                          date.fromisoformat(day).weekday() in weekdays else 0,
                          source='当日调整' if chosen['kind'] == 'day' else '个人要求',
                          start_day=chosen['start_day'], note=chosen['note'], personal=True,
                          personal_quantity=chosen['quantity'])
        return result

    def list(self, actor, filters):
        from operations import TZ
        self.ops.admin(actor)
        day = self.day(filters.get('date') or datetime.now(TZ).date().isoformat())
        rules = self.rules()
        people = self.ops.roster_people()['users']
        employees = [u for u in people if u['role'] == 'employee' and not u.get('deleted_at')]
        return {'date': day, 'default': self.resolve(day, rules=rules),
                'employees': [{k: u[k] for k in ('id', 'name', 'employee_code', 'group_id', 'enabled')} |
                              {'target': self.resolve(day, u['id'], rules)} for u in employees],
                'history': [dict(r, name=next((u['name'] for u in employees if u['id'] == r['employee_id']),
                                             '统一要求' if not r['employee_id'] else '已删除员工'),
                                 weekdays=json.loads(r['weekdays'])) for r in reversed(rules[-100:])]}

    def save(self, actor, data):
        from operations import now
        self.ops.admin(actor)
        day = self.day(data.get('start_day'))
        scope = data.get('scope')
        kind = data.get('kind', 'recurring')
        if scope not in {'default', 'employee'} or kind not in {'recurring', 'day'}:
            raise ValueError('请选择统一要求、个人要求或当日调整')
        quantity = data.get('quantity')
        inherit = data.get('inherit', False)
        if not isinstance(inherit, bool) or inherit and scope != 'employee':
            raise ValueError('继承设置无效')
        if not inherit and (isinstance(quantity, bool) or not str(quantity).isdigit() or not 0 <= int(quantity) <= 1000):
            raise ValueError('每日条数应为 0–1000 的整数，0 表示无需报送')
        cutoff = str(data.get('cutoff', '23:59'))
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', cutoff):
            raise ValueError('截止时间请填写小时和分钟')
        weekdays = data.get('weekdays', list(range(7)))
        if not isinstance(weekdays, list) or not weekdays or any(type(v) is not int or v not in range(7) for v in weekdays):
            raise ValueError('请至少选择一个执行日')
        note = str(data.get('note') or '').strip()
        if len(note) > 500 or kind == 'day' and len(note) < 2:
            raise ValueError('当日调整请填写 2–500 字原因')
        employees = data.get('employees') if scope == 'employee' else ['']
        if not isinstance(employees, list) or not 1 <= len(employees) <= 1000 or any(not isinstance(e, str) for e in employees):
            raise ValueError('请至少选择一位员工')
        if scope == 'default' and kind != 'recurring':
            raise ValueError('统一要求请按生效日期设置')
        with self.ops.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for employee in dict.fromkeys(employees):
                if scope == 'employee':
                    if not db.execute("SELECT 1 FROM ops_users u WHERE u.id=? AND u.role='employee' AND u.enabled=1 AND NOT EXISTS(SELECT 1 FROM ops_employee_deletions x WHERE x.user_id=u.id)", (employee,)).fetchone():
                        raise ValueError('所选员工不存在或已停用，请刷新人员名册')
                    if not self.resolve(day, rules=[dict(r) for r in db.execute('SELECT * FROM ops_daily_report_rules')])['configured']:
                        raise ValueError('请先设置在该日期生效的统一要求')
                db.execute('''INSERT INTO ops_daily_report_rules
                    (employee_id,kind,start_day,quantity,weekdays,cutoff,note,actor_id,created)
                    VALUES(?,?,?,?,?,?,?,?,?)''', (employee, kind, day, None if inherit else int(quantity),
                               json.dumps(sorted(set(weekdays))), cutoff, note, actor['id'], now()))
            self.ops.audit('INFO', 'system', '保存每日报送目标',
                           json.dumps({'scope': scope, 'employees': employees, 'kind': kind, 'start_day': day,
                                       'quantity': None if inherit else int(quantity), 'weekdays': weekdays,
                                       'cutoff': cutoff, 'note': note}, ensure_ascii=False), title=actor['name'], db=db)
        return {'message': '每日报送要求已保存；生效日期之前的规则保留'}

    def progress(self, result):
        """Enrich a roster before filtering/pagination, retaining legacy receipt columns."""
        from operations import TZ, now
        day = result['date']
        rules = self.rules()
        default = self.resolve(day, rules=rules)
        if not default['configured']:
            result.update(quota_mode=False, rule=default)
            return
        start = int(datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp())
        end = start + 86400
        as_of = now()
        cutoff = int(datetime.fromisoformat(day+'T'+default['cutoff']).replace(tzinfo=TZ).timestamp()) + 60
        accepted = self.ops.query('''SELECT w.id,w.owner_id,w.published,s.submitted_at
            FROM ops_works w JOIN ops_submissions s ON s.work_id=w.id
            LEFT JOIN ops_work_deletions x ON x.work_id=w.id
            WHERE x.work_id IS NULL AND w.published>=? AND w.published<?
            AND w.published<=?''', (start, end, as_of))
        accepted_ids = {w['id'] for w in accepted}
        pending = self.ops.query('''SELECT r.id,r.employee_id,r.work_id,r.payload,r.received_at,r.status
            FROM ops_report_items r LEFT JOIN ops_report_deletions x ON x.item_id=r.id
            WHERE x.item_id IS NULL AND r.status IN ('queued','processing','review','failed')''')
        by_employee = {}
        for row in accepted:
            by_employee.setdefault(row['owner_id'], []).append(row)
        attention, unknown = {}, {}
        for row in pending:
            try:
                payload = json.loads(row['payload'] or '{}')
            except (ValueError, TypeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            work_id = row['work_id'] or payload.get('id')
            if work_id in accepted_ids:
                continue
            published = payload.get('published')
            if isinstance(published, (int, float)) and not isinstance(published, bool) and start <= published < end:
                attention.setdefault(row['employee_id'], set()).add(work_id or row['id'])
            elif not isinstance(published, (int, float)) and start <= row['received_at'] < end:
                unknown.setdefault(row['employee_id'], set()).add(work_id or row['id'])
        for row in result['items']:
            target = self.resolve(day, row['id'], rules)
            required = 0 if row['excuse'] else target['quantity']
            works = by_employee.get(row['id'], [])
            actual = len({w['id'] for w in works})
            unresolved = len(attention.get(row['id'], set()))
            remaining = max(0, required-actual)
            overdue = as_of >= cutoff and remaining > 0
            row.update(target=target, required=required, completed_count=actual, remaining=remaining,
                       pending_count=unresolved, unlocated_count=len(unknown.get(row['id'], set())),
                       late_count=sum(w['submitted_at'] >= cutoff for w in works),
                       ontime_count=sum(w['submitted_at'] < cutoff for w in works),
                       status='excused' if required == 0 else 'reported' if not remaining else
                              'attention' if unresolved or unknown.get(row['id']) else 'missing',
                       phase='overdue' if overdue else 'in_progress', cutoff_at=cutoff-1,
                       latest_published=max((w['published'] for w in works), default=None))
        result.update(quota_mode=True, rule=default, cutoff_at=cutoff-1, as_of=as_of)
