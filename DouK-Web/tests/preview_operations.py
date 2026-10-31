"""Disposable browser QA workspace on port 8878. Never calls a platform or uses real data."""
from pathlib import Path
import sys
import tempfile
import argparse

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as app
from operations import now


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--simulate-login',action='store_true',help='Use disposable test login identities; never open Chrome')
    parser.add_argument('--simulate-multi-login',action='store_true',help='Use different disposable reporter identities on successive scans')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='DouK-operations-qa-') as directory:
        root=Path(directory)
        app.DATA=root
        app.DB=root/'workspace.sqlite'
        app.CONFIG=root/'settings.json'
        app.DOWNLOADS=root/'Downloads'
        app.DOWNLOADS.mkdir()
        with app.connect_db() as db:
            db.executescript('CREATE TABLE tasks(id TEXT PRIMARY KEY,payload TEXT);CREATE TABLE records(id INTEGER PRIMARY KEY,task_id TEXT,payload TEXT);CREATE TABLE files(key TEXT PRIMARY KEY,path TEXT);CREATE TABLE accounts(id TEXT PRIMARY KEY,payload TEXT);')
        ops=app.OPS
        ops.schema()
        ops.login({'username':'qa_admin','password':'Qa-preview-only-2026','name':'测试管理员'},setup=True)
        admin=ops.query("SELECT * FROM ops_users WHERE username='qa_admin'",one=True)
        department=ops.save_department({'name':'测试 · 内容运营部'},admin)['id']
        group=ops.save_group({'name':'测试 · 内容运营组','department_id':department},admin)['id']
        employee_id=ops.save_employee({'employee_code':'QA001','name':'测试员工','group_id':group},admin)['id']
        ops.save_group({'id':group,'name':'测试 · 内容运营组','department_id':department,'leader_id':employee_id},admin)
        binding_id=ops.save_binding({'nickname':'测试 · 品牌账号','homepage':'https://www.douyin.com/user/QA-preview-account-A','owner_id':employee_id,'monitor_start':now()-86400*7},admin)['id']
        binding=ops.query('SELECT * FROM ops_bindings WHERE id=?',(binding_id,),one=True)
        ops.execute('INSERT INTO ops_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('qa-partial-job','inspect',binding_id,admin['id'],'partial',1,'测试数据：发布列表完整性无法确认，暂不判定漏报',now(),now(),0))
        def raw(work_id,published=None):
            return {'aweme_id':work_id,'author':{'sec_uid':binding['sec_uid'],'uid':'qa-uid','nickname':'测试 · 品牌账号'},
                    'desc':{'1234567890001':'测试作品 · 品牌日常内容','1234567890002':'测试作品 · 新品发布预告','1234567890003':'测试作品 · 员工活动记录'}.get(work_id,'测试作品 · 新提交内容'),
                    'create_time':published or now()-7200,'statistics':{'play_count':None,'digg_count':120,'comment_count':18,'collect_count':9,'share_count':6}}
        for work_id,published in [('1234567890001',now()-86400*3),('1234567890002',now()-3600),('1234567890003',now()-86400*2)]:
            with app.connect_db() as db:
                ops.upsert_work(ops.normalize(raw(work_id,published)),binding,db)
                db.execute('INSERT INTO ops_verified VALUES(?,?)',(binding_id,work_id))
        ops.execute("UPDATE ops_bindings SET check_state='complete',check_message='测试数据：发布列表完整核查成功',last_check=?,last_success=? WHERE id=?",(now(),now(),binding_id))
        ops.execute('INSERT INTO ops_submissions VALUES(?,?,?,?,?,?)',('qa-submission','1234567890001',employee_id,now()-86400*3+3600,0,now()-86400*2))
        def fake_upstream(path,payload=None,config=None):
            if path=='/douyin/detail':
                work=raw(payload['detail_id'])
                if payload['detail_id']=='1234567890005':
                    work['author']={'sec_uid':'QA-new-account-review','nickname':'测试 · 待确认新账号','uid':'qa-new'}
                if payload['detail_id']=='1234567890006':
                    work['author']={'sec_uid':'QA-preview-account-B','nickname':'测试 · 第二账号','uid':'qa-second'}
                return {'data':work}
            if path=='/douyin/account':
                return {'data':[raw('1234567890001'),raw('1234567890002'),raw('1234567890003')], 'params':{'workspace_check':{'complete':True}}}
            if path=='/douyin/share':
                return {'url':'https://www.douyin.com/video/1234567890004'}
            if path=='/token':
                return {}
            if path=='/settings':
                return {}
            raise ValueError('浏览器测试未调用真实平台接口')
        app.upstream=fake_upstream
        if args.simulate_login or args.simulate_multi_login:
            import ops_accounts
            import report_accounts
            import threading
            class PreviewLogin(ops_accounts.DouyinLogin):
                def start(self):
                    row=ops.query('SELECT * FROM ops_account_logins WHERE id=?',(self.profile.name,),one=True)
                    identity=None if '手动' in row['nickname'] else {'sec_uid':'QA-own-profile-'+row['id']}
                    if row['binding_id'] and identity:
                        identity={'sec_uid':ops.query('SELECT sec_uid FROM ops_bindings WHERE id=?',(row['binding_id'],),one=True)['sec_uid']}
                    message=self.save_cookie('sessionid=disposable-qa-only',identity)
                    self._set('saved',message)
                    return self.status()
            ops_accounts.DouyinLogin=PreviewLogin
            class PreviewReportLogin(report_accounts.DouyinLogin):
                count=0
                def start(self):
                    self.event=threading.Event()
                    PreviewReportLogin.count+=1
                    account='A' if PreviewReportLogin.count%2 else 'B'
                    message=self.save_cookie('sessionid=disposable-qa-only',{
                        'sec_uid':'QA-preview-account-'+account if args.simulate_multi_login else 'QA-own-profile-report',
                        'nickname':'测试核验账号 '+account})
                    self._set('saved',message)
                    return self.status()
            report_accounts.DouyinLogin=PreviewReportLogin
        ops.start()
        class PreviewHandler(app.Handler):
            def send(self,data,*args,**kwargs):
                if isinstance(data,bytes) and kwargs.get('mime','').startswith('text/html'):
                    import re
                    marker='<div style="position:fixed;bottom:0;left:0;right:0;z-index:99999;background:#fff5df;color:#947124;text-align:center;font-size:11px;padding:5px">隔离测试环境 · 所有员工、作品和统计均为测试数据</div>'.encode()
                    data=re.sub(rb'(<body\b[^>]*>)',lambda match:match[1]+marker,data,count=1)
                return super().send(data,*args,**kwargs)
        server=app.ThreadingHTTPServer(('127.0.0.1',8878),PreviewHandler)
        try:
            server.serve_forever()
        finally:
            ops.scheduler_stop.set()
            server.server_close()


if __name__=='__main__':
    main()
