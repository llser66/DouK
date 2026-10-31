"""Local daily receipt progress; never infer publishing activity from missing data."""
from datetime import datetime, timedelta
import re


class ReportProgress:
    def __init__(self, ops):
        self.ops = ops

    def bounds(self, value=None):
        from operations import TZ
        today = datetime.now(TZ).date()
        value = str(value or today.isoformat())
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('请选择报送日期，格式为 YYYY-MM-DD')
        try:
            start = datetime.fromisoformat(value).replace(tzinfo=TZ)
        except ValueError:
            raise ValueError('请选择有效的报送日期') from None
        if start.date() > today:
            raise ValueError('不能核对未来日期的报送情况')
        return value, int(start.timestamp()), int((start + timedelta(days=1)).timestamp())

    def list(self, actor, filters):
        self.ops.admin(actor)
        day, start, end = self.bounds(filters.get('date'))
        rows = self.ops.query('''SELECT u.id,u.name,u.created,u.group_id,
            COALESCE(p.employee_code,u.username) employee_code,
            COALESCE(gl.label,g.name,'未分组') group_name,
            COALESCE(d.id,'') department_id,COALESCE(d.name,'未设置部门') department_name
            FROM ops_users u LEFT JOIN ops_employee_profiles p ON p.user_id=u.id
            LEFT JOIN ops_groups g ON g.id=u.group_id LEFT JOIN ops_group_labels gl ON gl.group_id=g.id
            LEFT JOIN ops_group_departments gd ON gd.group_id=g.id LEFT JOIN ops_departments d ON d.id=gd.department_id
            LEFT JOIN ops_employee_deletions x ON x.user_id=u.id
            WHERE u.role='employee' AND u.enabled=1 AND x.user_id IS NULL AND u.created<?''', (end,))
        receipts = self.ops.query('''SELECT r.employee_id,r.received_at,r.status FROM ops_report_items r
            LEFT JOIN ops_report_deletions x ON x.item_id=r.id WHERE x.item_id IS NULL AND r.received_at<?
            UNION ALL
            SELECT s.user_id,s.submitted_at,'submitted' FROM ops_submissions s
            JOIN ops_works w ON w.id=s.work_id LEFT JOIN ops_work_deletions x ON x.work_id=w.id
            WHERE x.work_id IS NULL AND s.submitted_at<? AND NOT EXISTS (
                SELECT 1 FROM ops_report_items r LEFT JOIN ops_report_deletions rd ON rd.item_id=r.id
                WHERE rd.item_id IS NULL AND r.employee_id=s.user_id AND
                (r.work_id=s.work_id OR (json_valid(r.payload) AND json_extract(r.payload,'$.id')=s.work_id)))''', (end,end))
        by_employee = {}
        for receipt in receipts:
            by_employee.setdefault(receipt['employee_id'], []).append(receipt)
        excuses = {r['employee_id']: r for r in self.ops.query(
            'SELECT employee_id,reason FROM ops_report_excuses WHERE day=? AND enabled=1', (day,))}
        for row in rows:
            records = by_employee.get(row['id'], [])
            daily = [r for r in records if start <= r['received_at'] < end]
            row['received_count'] = len(daily)
            row['accepted_count'] = sum(r['status']=='submitted' for r in daily)
            row['attention_count'] = sum(r['status'] not in {'submitted','duplicate'} for r in daily)
            row['last_received'] = max((r['received_at'] for r in records), default=None)
            row['excuse'] = excuses.get(row['id'], {}).get('reason','')
            row['status'] = ('excused' if row['excuse'] else 'missing' if not daily
                             else 'attention' if row['attention_count'] else 'reported')
        from daily_report_targets import DailyReportTargets
        result = {'date': day, 'items': rows}
        DailyReportTargets(self.ops).progress(result)
        for key, column in [('employee','id'),('department','department_id'),('group','group_id')]:
            if filters.get(key):
                rows = [r for r in rows if r[column]==filters[key]]
        search = str(filters.get('search') or '').strip().casefold()
        if search:
            rows = [r for r in rows if search in ' '.join(str(r[k]) for k in
                    ('name','employee_code','department_name','group_name')).casefold()]
        counts = {state: sum(r['status']==state for r in rows)
                  for state in ('missing','reported','attention','excused')}
        counts['total'] = len(rows)
        status = filters.get('status', 'missing')
        if status not in {'','all','missing','reported','attention','excused'}:
            raise ValueError('报送状态无效')
        if status not in {'','all'}:
            rows = [r for r in rows if r['status']==status]
        rows.sort(key=lambda r: (r['department_name'],r['group_name'],r['name'],r['id']))
        page = min(max(1,int(filters.get('page',1))), max(1,(len(rows)+29)//30))
        return {**result,'date':day,'counts':counts,'total':len(rows),'page':page,'page_size':30,
                'items':rows if filters.get('_export') else rows[(page-1)*30:page*30]}

    def excuse(self, actor, data):
        from operations import now
        self.ops.admin(actor)
        day, _, end = self.bounds(data.get('date'))
        employee_id = str(data.get('employee') or '')
        enabled = data.get('enabled', True)
        reason = str(data.get('reason') or '').strip()
        if not isinstance(enabled, bool) or (enabled and not 2 <= len(reason) <= 500):
            raise ValueError('请填写 2–500 字的无需报送原因')
        with self.ops.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            employee = db.execute("SELECT * FROM ops_users WHERE id=? AND role='employee' AND enabled=1 AND created<?",(employee_id,end)).fetchone()
            if not employee or db.execute('SELECT 1 FROM ops_employee_deletions WHERE user_id=?',(employee_id,)).fetchone():
                raise ValueError('请选择当日已建档的启用员工')
            db.execute('''INSERT INTO ops_report_excuses VALUES(?,?,?,?,?,?)
                ON CONFLICT(day,employee_id) DO UPDATE SET reason=excluded.reason,
                enabled=excluded.enabled,actor_id=excluded.actor_id,updated=excluded.updated''',
                (day,employee_id,reason,int(enabled),actor['id'],now()))
            self.ops.audit('INFO','task','标记无需报送' if enabled else '恢复报送要求',
                           day+' '+reason,title=employee['name'],db=db)
        return {'message':'已标记当日无需报送' if enabled else '已恢复当日报送要求'}

    def export(self, actor, filters):
        from operations import TZ
        result = self.list(actor, {**filters,'_export':True})
        labels = {'missing':'未报送','reported':'已报送','attention':'报送待处理','excused':'无需报送'}
        rows = [{'报送日期':result['date'],'姓名':r['name'],'工号':r['employee_code'],
                 '部门':r['department_name'],'小组':r['group_name'],'状态':labels[r['status']],
                 '报送条数':r['received_count'],'归档条数':r['accepted_count'],'待处理条数':r['attention_count'],
                 '最近报送时间':datetime.fromtimestamp(r['last_received'],TZ).strftime('%Y-%m-%d %H:%M') if r['last_received'] else '',
                 '无需报送原因':r['excuse']} for r in result['items']]
        if result.get('quota_mode'):
            for output, row in zip(rows, result['items']):
                output.update({'统计日期（作品发布日）':result['date'], '要求条数':row['required'],
                               '有效完成':row['completed_count'], '还差条数':row['remaining'],
                               '待处理作品':row['pending_count'], '发布时间待识别':row['unlocated_count'],
                               '补报条数':row['late_count'], '截止时间':datetime.fromtimestamp(row['cutoff_at'],TZ).strftime('%Y-%m-%d %H:%M'),
                               '规则来源':row['target']['source'],
                               '完成状态':'无需报送' if row['status']=='excused' else '已完成' if row['status']=='reported' else
                                          '未达标' if row['phase']=='overdue' else '进行中'})
                output['状态'] = output['完成状态']
        if not rows:
            raise ValueError('当前筛选没有人员可导出')
        self.ops.audit('INFO','system','导出人员报送进度',result['date']+f'，{len(rows)} 人',title=actor['name'])
        return self.ops.exporter('xlsx',rows)
