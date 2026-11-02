"""Run the employee page controller against browser-like session and image APIs."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


RUNNER=r'''
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const script=fs.readFileSync(input.script,'utf8');
const token='a'.repeat(64),requests=[],elements=new Map(),storage=new Map(),local=new Map(),timers=new Map(),windowHandlers={};
if(input.remembered)local.set('douk.report.verified',token);
if(input.receipts)local.set('douk.report.receipts',JSON.stringify(input.receipts));
if(input.action==='removed_receipt')local.set('douk.report.receipts',JSON.stringify([{receipt_token:'b'.repeat(64),items:[{id:'old-receipt',status:'duplicate'}]}]));
if(input.multi)local.set('douk.report.verified.accounts',JSON.stringify([token,'b'.repeat(64)]));
if(input.cached)storage.set('douk.report.login',token);
let expiredToken='',revoked=new Set();let startResolve;let receiptReads=0,clockOffset=0,restoredFocus=0;let timerId=0,status=input.verified?'saved':'waiting',expired=false,offline=false,qrWrites=0;
const bitmap='data:image/png;base64,dGVzdA==';
const result=(which=token)=>({state:status,verified:status==='saved',message:status==='saved'?'核验完成':'请扫码',expires_at:Math.floor(Date.now()/1000)+7*86400,verified_at:Math.floor(Date.now()/1000),remembered:status==='saved',
  identity:status==='saved'?{sec_uid:'MS4w-'+(input.same_account?'own':which[0])+'-profile',homepage:'https://www.douyin.com/user/MS4w-own-profile',nickname:'测试本人'+which[0]}:{},qr_image:status==='waiting'?bitmap:''});
function element(id){if(!elements.has(id)){
  const attrs=new Map();elements.set(id,{value:'',textContent:id==='report-login-start'?'添加核验账号':'',innerHTML:'',hidden:false,disabled:false,dataset:{},handlers:{},
    addEventListener(name,fn){this.handlers[name]=fn},getAttribute(name){return attrs.get(name)||null},
    removeAttribute(name){attrs.delete(name)},scrollIntoView(){},focus(){restoredFocus++},setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end},
    set src(value){attrs.set('src',value);if(id==='report-login-qr')qrWrites++}});
}return elements.get(id)}
const context={document:{getElementById:element,visibilityState:'visible',addEventListener:(name,fn)=>windowHandlers['document:'+name]=fn},window:{confirm:()=>input.confirm!==false,addEventListener:(name,fn)=>windowHandlers[name]=fn},
  sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  localStorage:{getItem:k=>local.get(k)||null,setItem:(k,v)=>local.set(k,v),removeItem:k=>local.delete(k)},
  Image:class{set src(value){queueMicrotask(()=>this.onload?.())}},
  setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id},clearTimeout:id=>timers.delete(id),
  Intl,URL,Date:class extends Date{static now(){return Date.now()+clockOffset}},crypto:{randomUUID:()=> 'test-request-unique-001'},
  fetch:async(path,options)=>{const body=options?.body?JSON.parse(options.body):null;requests.push({path,body});
    if(path.endsWith('/directory'))return {ok:true,json:async()=>({departments:[],groups:[],employees:[]})};
    if(path.endsWith('/receipt')){if(input.receipt_states){const next=input.receipt_states[Math.min(receiptReads++,input.receipt_states.length-1)];return {ok:true,json:async()=>({items:[{id:'note1',status:next,approval_method:next==='submitted'?'manual':''}]})}}return {ok:false,status:404,json:async()=>({error:'回执已不存在'})}};
    if(path.endsWith('/login/cancel')){revoked.add(body.login_token);return {ok:true,json:async()=>({})}};
    if(path.endsWith('/login/status')&&offline)throw Error('Network unavailable');
    if(path.endsWith('/login/status')&&(expired||body.login_token===expiredToken||revoked.has(body.login_token)))return {ok:false,status:404,json:async()=>({error:'核验已过期'})};
    if(path.endsWith('/login/start')){if(input.action==='cancel_while_start')await new Promise(resolve=>startResolve=resolve);const next=input.multi||input.new_token?'c'.repeat(64):token;return {ok:true,json:async()=>({...result(next),login_token:next})}};
    if(path.endsWith('/login/status'))return {ok:true,json:async()=>result(body.login_token)};
    if(path.endsWith('/submit'))return {ok:true,json:async()=>({receipt_token:'receipt-token',items:[]})};
    throw Error('Unexpected request '+path);
  }};
const sandbox=vm.createContext(context);
const flush=()=>new Promise(setImmediate);
async function load(){vm.runInContext(script,sandbox);await flush()}
async function poll(){const [key,timer]=[...timers].find(([,v])=>v.delay<=60000)||[];if(!timer)throw Error('Missing status poll');timers.delete(key);await timer.fn();await flush()}
(async()=>{
  await load();
  if(input.action==='receipt_watch'){
    const [key,timer]=[...timers].find(([,v])=>v.delay===30000)||[];if(!timer)throw Error('Missing slow receipt refresh');timers.delete(key);await timer.fn();await flush();
  }
  if(input.action==='receipt_focus'){
    context.document.visibilityState='hidden';windowHandlers['document:visibilitychange']();
    clockOffset=5000;context.document.visibilityState='visible';windowHandlers.focus();await flush();
  }
  if(input.action==='receipt_draft'){
    const note=element('note-note1');note.dataset.noteInput='note1';note.value='备注尚未保存';note.selectionStart=2;note.selectionEnd=4;
    context.document.activeElement=note;element('report-receipts').handlers.input({target:note});
    await element('report-refresh').onclick();await flush();
  }
  if(input.action==='reload'){await load()}
  if(input.action==='reopen'){storage.clear();await load()}
  if(input.action==='other_tab_remove'){local.delete('douk.report.verified.accounts');windowHandlers.storage({key:'douk.report.verified.accounts',newValue:null});await flush()}
  if(input.action==='cancel_while_start'){const pending=element('report-login-start').onclick();await flush();await element('report-login-cancel').onclick();startResolve();await pending;await flush()}
  if(input.action==='poll_images'){
    await element('report-login-start').onclick();await flush();await poll();await poll();
  }
  if(input.action==='expire'){expired=true;await poll()}
  if(input.action==='transient'){offline=true;await poll();offline=false;await poll()}
  if(input.action==='add')await element('report-login-start').onclick();
  if(input.action==='one_expire'){expiredToken=token;await poll()}
  if(input.action==='remove'){await element('report-account-list').handlers.click({target:{closest:()=>({dataset:{accountRemove:token},disabled:false})}});await flush()}
  if(input.action==='pending_cancel'){status='waiting';await element('report-login-start').onclick();await element('report-login-cancel').onclick();await flush()}
  if(input.action==='cancel'){await element('report-login-cancel').onclick();await flush();await load()}
  if(['submit','submit_unverified'].includes(input.action)){
    for(const id of ['report-name','report-dept-name','report-group-name'])element(id).value='测试';
    element('report-links').value='https://www.douyin.com/video/1234567890123';
    await element('report-form').handlers.submit({preventDefault(){}});await flush();
  }
  const button=element('report-login-start');
  process.stdout.write(JSON.stringify({requests,stored:!!storage.get('douk.report.login'),remembered:JSON.parse(local.get('douk.report.verified.accounts')||'[]').length>0,tokens:JSON.parse(local.get('douk.report.verified.accounts')||'[]'),accounts:element('report-account-list').innerHTML,validity:element('report-login-validity').textContent,button:button.textContent,
    disabled:button.disabled,qrWrites,homeHidden:element('report-login-home').hidden,qrHidden:element('report-login-qr').hidden,
    message:element('report-login-status').textContent,receipts:JSON.parse(local.get('douk.report.receipts')||'[]'),reportError:element('report-error').textContent,submitDisabled:element('report-submit').disabled,receiptNotice:element('report-receipt-notice').textContent,receiptHTML:element('report-receipts').innerHTML,updated:element('report-receipt-updated').textContent,timerDelays:[...timers.values()].map(t=>t.delay),restoredFocus,selection:[element('note-note1').selectionStart,element('note-note1').selectionEnd]}));
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1});
'''


@unittest.skipUnless(shutil.which('node'),'Node.js required to exercise the page controller')
class ReportLoginUITests(unittest.TestCase):
    def run_page(self,**options):
        result=subprocess.run([shutil.which('node'),'-e',RUNNER],input=json.dumps({
            'script':str(Path(__file__).resolve().parents[1]/'reports.js'),**options}),
            text=True,capture_output=True,encoding='utf-8',timeout=10,check=True)
        return json.loads(result.stdout)

    def test_review_and_failed_receipts_refresh_then_stop_after_terminal_result(self):
        for status in ('review','failed'):
            page=self.run_page(action='receipt_watch',receipts=[{'receipt_token':'b'*64,'items':[{'id':'note1','status':status}]}],receipt_states=[status,'submitted'])
            self.assertEqual(page['receipts'][0]['items'][0]['status'],'submitted')
            self.assertIn('人工确认通过',page['receiptHTML'])
            self.assertIn('最近更新',page['updated'])
            self.assertEqual(page['timerDelays'],[])

    def test_returning_to_page_refreshes_terminal_receipts_too(self):
        page=self.run_page(action='receipt_focus',receipts=[{'receipt_token':'b'*64,'items':[{'status':'submitted'}]}],receipt_states=['submitted','rejected'])
        self.assertEqual(page['receipts'][0]['items'][0]['status'],'rejected')
        self.assertEqual(page['timerDelays'],[])

    def test_refresh_preserves_unsaved_note_and_cursor(self):
        page=self.run_page(action='receipt_draft',receipts=[{'receipt_token':'b'*64,'items':[{'id':'note1','status':'review'}]}],receipt_states=['review','submitted'])
        self.assertIn('备注尚未保存',page['receiptHTML'])
        self.assertGreater(page['restoredFocus'],0)
        self.assertEqual(page['selection'],[2,4])

    def test_refresh_revalidates_saved_token_without_starting_a_new_login(self):
        page=self.run_page(cached=True,verified=True,action='reload')
        self.assertEqual(sum(r['path'].endswith('/login/start') for r in page['requests']),0)
        self.assertEqual(sum(r['path'].endswith('/login/status') for r in page['requests']),2)
        self.assertEqual(page['button'],'添加核验账号')
        self.assertIn('查看主页',page['accounts'])

    def test_malformed_cached_receipts_do_not_block_new_submission(self):
        for items in ({'unexpected':'object'}, [None, 'broken', {'id':'old','published':'invalid'}]):
            with self.subTest(items=items):
                page=self.run_page(action='submit',remembered=True,verified=True,receipts=[{
                    'receipt_token':'b'*64,'received_at':'invalid','items':items}])
                self.assertTrue(any(r['path'].endswith('/directory') for r in page['requests']))
                self.assertTrue(any(r['path'].endswith('/submit') for r in page['requests']))
                self.assertEqual(page['reportError'],'')

    def test_unverified_report_is_blocked_before_request(self):
        for options in ({},{'remembered':True,'verified':False}):
            page=self.run_page(action='submit_unverified',**options)
            self.assertFalse(any(r['path'].endswith('/submit') for r in page['requests']))
            self.assertTrue(page['submitDisabled'])
            self.assertIn('核验',page['reportError'])

    def test_deleted_server_receipt_is_removed_from_browser_cache(self):
        page=self.run_page(action='removed_receipt')
        self.assertEqual(page['receipts'],[])
        self.assertIn('可重新报送',page['receiptNotice'])
        self.assertEqual(page['reportError'],'')

    def test_browser_reopen_restores_proof_without_new_scan(self):
        page=self.run_page(remembered=True,verified=True,action='reopen')
        self.assertTrue(page['remembered'])
        self.assertEqual(page['button'],'添加核验账号')
        self.assertIn('有效至',page['accounts'])
        self.assertFalse(any(r['path'].endswith('/login/start') for r in page['requests']))

    def test_pending_scan_is_not_saved_as_a_remembered_account(self):
        page=self.run_page(action='poll_images')
        self.assertFalse(page['remembered'])

    def test_other_tab_removal_clears_current_tab_identity(self):
        page=self.run_page(remembered=True,verified=True,action='other_tab_remove')
        self.assertFalse(page['stored'])
        self.assertFalse(page['remembered'])
        self.assertNotIn('查看主页',page['accounts'])
        self.assertEqual(page['button'],'添加核验账号')

    def test_cancelling_during_start_revokes_late_response(self):
        page=self.run_page(action='cancel_while_start')
        self.assertFalse(page['stored'])
        self.assertFalse(page['remembered'])
        self.assertTrue(page['qrHidden'])
        self.assertTrue(any(r['path'].endswith('/login/cancel') for r in page['requests']))

    def test_repeated_poll_does_not_replace_unchanged_qr_bitmap(self):
        page=self.run_page(action='poll_images')
        self.assertEqual(page['qrWrites'],1)
        self.assertTrue(page['disabled'])
        self.assertEqual(page['button'],'正在核验…')
        self.assertTrue(page['stored'])

    def test_expired_proof_is_not_displayed_as_verified(self):
        page=self.run_page(remembered=True,verified=True,action='expire')
        self.assertFalse(page['remembered'])
        self.assertFalse(page['stored'])
        self.assertNotIn('查看主页',page['accounts'])
        self.assertFalse(page['disabled'])
        self.assertEqual(page['button'],'添加核验账号')

    def test_cancel_pending_scan_keeps_previously_saved_accounts(self):
        page=self.run_page(multi=True,verified=True,action='pending_cancel')
        self.assertEqual(page['tokens'],['a'*64,'b'*64])
        self.assertFalse(page['stored'])
        self.assertTrue(page['qrHidden'])
        self.assertTrue(any(r['path'].endswith('/login/cancel') and r['body']['login_token']=='c'*64 for r in page['requests']))

    def test_transient_disconnect_reuses_proof_after_connection_returns(self):
        page=self.run_page(cached=True,verified=True,action='transient')
        self.assertTrue(page['remembered'])
        self.assertEqual(page['button'],'添加核验账号')
        self.assertFalse(page['disabled'])
        self.assertFalse(any(r['path'].endswith('/login/start') for r in page['requests']))

    def test_restored_proof_is_sent_with_report_without_new_scan(self):
        page=self.run_page(cached=True,verified=True,action='submit')
        sent=next(r['body'] for r in page['requests'] if r['path'].endswith('/submit'))
        self.assertEqual(sent['login_tokens'],['a'*64])
        self.assertFalse(any(r['path'].endswith('/login/start') for r in page['requests']))

    def test_adding_third_account_preserves_existing_accounts(self):
        page=self.run_page(multi=True,verified=True,action='add')
        self.assertEqual(page['tokens'],['a'*64,'b'*64,'c'*64])
        self.assertFalse(any(r['path'].endswith('/login/cancel') for r in page['requests']))

    def test_reverification_of_same_account_replaces_only_its_old_proof(self):
        page=self.run_page(remembered=True,verified=True,same_account=True,new_token=True,action='add')
        self.assertEqual(page['tokens'],['c'*64])
        self.assertTrue(any(r['path'].endswith('/login/cancel') and r['body']['login_token']=='a'*64 for r in page['requests']))

    def test_removing_one_account_requires_confirmation_and_keeps_others(self):
        page=self.run_page(multi=True,verified=True,action='remove')
        self.assertEqual(page['tokens'],['b'*64])
        cancelled=self.run_page(multi=True,verified=True,action='remove',confirm=False)
        self.assertEqual(cancelled['tokens'],['a'*64,'b'*64])
        self.assertFalse(any(r['path'].endswith('/login/cancel') for r in cancelled['requests']))

    def test_one_expired_proof_does_not_clear_other_accounts(self):
        page=self.run_page(multi=True,verified=True,action='one_expire')
        self.assertEqual(page['tokens'],['b'*64])
        self.assertIn('测试本人b',page['accounts'])

    def test_all_saved_accounts_are_sent_in_one_report(self):
        page=self.run_page(multi=True,verified=True,action='submit')
        sent=next(r['body'] for r in page['requests'] if r['path'].endswith('/submit'))
        self.assertEqual(sent['login_tokens'],['a'*64,'b'*64])


if __name__=='__main__':unittest.main()
