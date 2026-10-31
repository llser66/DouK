"""Run the actual admin loaders to check the separated pages and navigation."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


RUNNER = r'''
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8')),listeners={},elements=new Map(),requests=[],posts=[],opens=[];
function element(id){if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',disabled:false,hidden:false,
  addEventListener(name,fn){this[name]=fn},insertAdjacentHTML(place,html){this.innerHTML=html+this.innerHTML},
  prepend(el){this.innerHTML=el.innerHTML+this.innerHTML},remove(){},close(){},showModal(){}});return elements.get(id)}
const users=[{id:'a',name:'甲',role:'employee',group_id:'ga'},{id:'b',name:'乙',role:'employee',group_id:'gb'}];
const people={users,groups:[{id:'ga',name:'甲组',department_id:'da'},{id:'gb',name:'乙组',department_id:'db'}],departments:[{id:'da',name:'甲部'},{id:'db',name:'乙部'}]};
const bindings=users.map((u,i)=>({id:'binding-'+u.id,owner_id:u.id,group_id:u.group_id,nickname:u.name+'账号',enabled:true,check_state:'complete',check_message:'完成',last_check:1,employee_name:u.name}));
const metrics=Object.fromEntries(['play_count','digg_count','comment_count','collect_count','share_count'].map(k=>[k,{value:null,known:0,missing:0,stale:0}]));
const summary={works:0,submitted:0,pending:0,overdue:0,confirm:0,error:0,exempt:0,late_submissions:0,publication_complete:false,submission_rate:null,rate_note:'待确认',metrics,rankings:{owner_id:[],group_id:[]}};
const ctx={document:{addEventListener:(n,f)=>(listeners[n]??=[]).push(f),querySelector:element,querySelectorAll:()=>[],createElement:()=>element('created')},
  window:{addEventListener(){}},state:{page:input.page},Intl,URL,URLSearchParams,Date,
  icon:()=>'',esc:v=>String(v??''),hydrate(){},toast(){},navigate:page=>opens.push({page}),
  safeOpsUrl:()=>'',api:async path=>{requests.push(path);const route=path.split('?')[0];
    if(route==='/ops/bindings')return bindings;if(route==='/ops/people')return people;
    if(route==='/ops/report-progress')return {date:'2026-10-31',counts:{total:2,missing:1,reported:1,attention:0,excused:0},items:[{id:'a',name:'甲',employee_code:'E1',department_name:'甲部',group_name:'甲组',received_count:0,accepted_count:0,attention_count:0,status:'missing',...(input.quota?{required:3,completed_count:1,remaining:2,pending_count:1,unlocated_count:0,late_count:0,phase:'in_progress',target:{source:'个人要求'}}:{})}],...(input.quota?{quota_mode:true,rule:{cutoff:'18:00'}}:{}),total:1,page:1,page_size:30};if(route==='/ops/summary')return summary;if(route==='/ops/works')return {items:[],total:0,page:1};
    if(route==='/ops/reporting')return {items:[],total:0,page:1,page_size:30,counts:{review:2,failed:1}};
    throw Error('Unexpected API '+path)},post:async(path,body)=>{posts.push({path,body});return {message:'ok'}}};
vm.createContext(ctx);
for(const name of ['ops-ui.js','ops-flow-ui.js','reporting-admin.js'])vm.runInContext(fs.readFileSync(name,'utf8'),ctx);
// Keep all loaders and listeners in the same lexical context.
vm.runInContext("ops.user={role:'admin'};ops.filters="+JSON.stringify(input.filters||{})+";",ctx);
ctx.opsOpen=(page,filters)=>opens.push({page,filters});
(async()=>{
  await ctx.loadOps();
  const html=element('#ops-body').innerHTML,queue=element('#report-queue-content').innerHTML;
  if(input.action==='card')for(const fn of listeners.click||[])await fn({target:{closest:()=>({dataset:{opsState:'overdue'},hasAttribute:()=>false})}});
  if(input.action==='report')for(const fn of listeners.click||[])await fn({target:{closest:()=>({dataset:{reportOpenState:'review'},hasAttribute:()=>false})}});
  if(input.action==='progress')for(const fn of listeners.click||[])await fn({target:{closest:()=>({dataset:{progressStatus:'missing'},hasAttribute:()=>false})}});
  process.stdout.write(JSON.stringify({html,queue,requests,posts,opens,nav:ctx.opsNavigation()}));
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1});
'''


@unittest.skipUnless(shutil.which('node'), 'Node required for admin page integration')
class AdminSectionTests(unittest.TestCase):
    def run_page(self, page, **options):
        process=subprocess.run(['node','-e',RUNNER],input=json.dumps({'page':page,**options}),
                               cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(process.returncode,0,process.stderr)
        return json.loads(process.stdout)

    def test_overview_shows_today_roster_progress_without_inspection_claims(self):
        page=self.run_page('overview',action='progress')
        self.assertIn('已收录作品',page['html'])
        self.assertIn('人员报送进度',page['html'])
        self.assertNotIn('报送率',page['html'])
        self.assertNotIn('巡检状态',page['nav'])
        self.assertIn('每日报送进度',page['nav'])
        self.assertEqual(page['opens'][0]['page'],'unreported')
        self.assertEqual(page['opens'][0]['filters']['status'],'missing')

    def test_statistics_loads_summary_without_work_or_report_tables(self):
        page=self.run_page('statistics',filters={'department':'da'})
        self.assertIn('作品互动数据',page['html'])
        self.assertIn('运营统计排行榜',page['html'])
        self.assertFalse(any(p.startswith('/ops/works') or p.startswith('/ops/reporting') for p in page['requests']))
        self.assertTrue(any('/ops/summary?department=da' in p for p in page['requests']))

    def test_reports_keep_confirmed_batch_delete_and_dont_request_publication_works(self):
        page=self.run_page('audit')
        self.assertIn('员工报送处理',page['html'])
        for marker in ('data-report-delete-selected','report-select-all','report-queue-department','report-queue-employee'):
            self.assertIn(marker,page['html'])
        self.assertNotIn('data-report-restore-selected',page['html'])
        self.assertIn('不可从页面恢复',page['html'])
        self.assertNotIn('ops-work-time-heading',page['html'])
        self.assertFalse(any(p.startswith('/ops/works') or p.startswith('/ops/summary') for p in page['requests']))
        self.assertIn('当前没有匹配的报送记录',page['queue'])

    def test_unreported_page_has_roster_filters_export_and_no_inspection_controls(self):
        page=self.run_page('unreported',filters={'department':'da'})
        self.assertIn('报送日期',page['html'])
        self.assertIn('甲',page['html'])
        self.assertIn('report-progress/export',page['html'])
        self.assertIn('data-progress-excuse',page['html'])
        self.assertIn('data-report-import',page['html'])
        self.assertNotIn('ops-check-all',page['html'])
        self.assertNotIn('检查详情',page['html'])
        self.assertEqual(page['posts'],[])
        self.assertTrue(any('/ops/report-progress?department=da' in p for p in page['requests']))
        self.assertFalse(any(p.startswith('/ops/summary') or p.startswith('/ops/works') for p in page['requests']))

    def test_quota_progress_displays_target_shortfall_and_personal_controls(self):
        page=self.run_page('unreported',quota=True,filters={'status':'all'})
        for text in ('作品发布日期','要求 / 有效完成','还差 2 条','个人要求','0 条','进行中'):
            self.assertIn(text,page['html'])
        self.assertIn('data-daily-employee',page['html'])
        self.assertIn('data-daily-works',page['html'])
        self.assertNotIn('检查详情',page['html'])
        overview=self.run_page('overview',quota=True)
        self.assertIn('已完成',overview['html'])
        self.assertIn('18:00',overview['html'])

    def test_report_alert_preserves_organization_but_not_publication_dates(self):
        page=self.run_page('overview',filters={'department':'da','start':'2026-10-01'},action='report')
        self.assertEqual(page['opens'],[{'page':'audit','filters':{}}])
        report_path=next(p for p in page['requests'] if p.startswith('/ops/reporting'))
        self.assertIn('department=da',report_path)
        self.assertNotIn('start=',report_path)

    def test_publication_filters_support_minutes_on_all_work_and_summary_pages(self):
        for name in ('overview','statistics','works'):
            with self.subTest(page=name):
                html=self.run_page(name,filters={'start':'2026-10-01','end':'2026-10-02'})['html']
                self.assertIn('type="datetime-local"',html)
                self.assertIn('step="60"',html)
                self.assertIn('2026-10-01T00:00',html)
                self.assertIn('2026-10-02T23:59',html)
                self.assertNotIn('type="date"',html)


if __name__=='__main__':
    unittest.main()
