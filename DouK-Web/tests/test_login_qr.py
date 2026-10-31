"""Exercise real preview JavaScript against a disposable DOM, never Douyin."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from douyin_login import DouyinLogin


DOM_RUNNER = r'''
const vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
let clicks=[];
const rect={x:100,y:100,width:400,height:500};
const body={};
const modal={innerText:input.fixture.startsWith('qr')?'请使用抖音扫码登录':'手机登录',
  parentElement:body,getBoundingClientRect:()=>rect,querySelector:()=>null};
const button=label=>({textContent:label,innerText:label,children:[],parentElement:body,
  getBoundingClientRect:()=>({x:100,y:100,width:100,height:30}),
  click:()=>clicks.push(label)});
const login=button('登录'),qrTab=button('扫码登录');
const image={tagName:input.fixture==='qr-canvas'?'CANVAS':'IMG',complete:true,naturalWidth:180,naturalHeight:180,
  currentSrc:input.source||'https://example.invalid/qr-one.png',parentElement:modal,
  toDataURL:()=>input.bitmap||'data:image/png;base64,dGVzdA==',
  getBoundingClientRect:()=>({x:150,y:200,width:180,height:180})};
const all=input.fixture==='closed'?[login]:[login,qrTab];
const document={body,createElement:()=>({getContext:()=>({drawImage:()=>{if(input.fixture==='qr-cors')throw Error('cross origin')}}),toDataURL:()=>input.bitmap||'data:image/png;base64,dGVzdA=='}),
 querySelectorAll:selector=>selector==='img,canvas'?(input.fixture.startsWith('qr')?[image]:[]):
 selector==='button,a,div,span'?all:(input.fixture==='closed'?[]:[modal])};
const context=vm.createContext({document,getComputedStyle:()=>({visibility:'visible'})});
let value;
// Same page context, like repeated Runtime.evaluate: lexical declarations must not leak.
for(let i=0;i<(input.repeat||1);i++)value=vm.runInContext(input.expression,context);
process.stdout.write(JSON.stringify({value,clicks}));
'''


@unittest.skipUnless(shutil.which('node'), 'Node.js required to exercise preview DOM')
class LoginQRTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session=DouyinLogin(Path(self.temp.name)/'profile',lambda *_:None,lambda:False,qr_mode=True)
        self.target={'type':'page','url':'https://www.douyin.com/user/self',
                     'webSocketDebuggerUrl':'ws://127.0.0.1:1234/devtools/page/test'}
        self.fixture='closed'
        self.clicks=[]
        self.screenshots=0
        self.expressions=[]
        self.source='https://example.invalid/qr-one.png'

    def command(self,url,method,params):
        if method=='Page.captureScreenshot':
            self.screenshots+=1
            return {'data':'dGVzdA=='}
        self.assertEqual(method,'Runtime.evaluate')
        expression=params['expression']
        self.expressions.append(expression)
        result=subprocess.run([shutil.which('node'),'-e',DOM_RUNNER],
            input=json.dumps({'expression':expression,'fixture':self.fixture,'repeat':1,'source':self.source}),
            capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)
        data=json.loads(result.stdout)
        self.clicks.extend(data['clicks'])
        return {'result':{'value':data.get('value')}}

    def preview(self,count=1):
        with patch.object(self.session,'_http',return_value=[self.target]),patch('douyin_login.cdp_request',side_effect=self.command):
            for _ in range(count):self.session._qr_preview()

    def test_slow_login_page_is_opened_once_across_repeated_polls(self):
        self.preview(5)
        self.assertEqual(self.clicks,['登录'])
        self.assertEqual(self.screenshots,0)

    def test_sms_panel_switches_to_qr_once_without_reopening_login(self):
        self.fixture='sms'
        self.preview(5)
        self.assertEqual(self.clicks,['扫码登录'])

    def test_visible_qr_bitmap_needs_no_screenshots_or_login_clicks(self):
        self.fixture='qr'
        self.preview(5)
        self.assertEqual(self.clicks,[])
        self.assertEqual(self.screenshots,0)
        self.assertEqual(self.session.qr_image,'data:image/png;base64,dGVzdA==')

    def test_canvas_qr_bitmap_needs_no_screenshots(self):
        self.fixture='qr-canvas'
        self.preview(5)
        self.assertEqual(self.screenshots,0)
        self.assertEqual(self.clicks,[])
        self.assertEqual(self.session.qr_image,'data:image/png;base64,dGVzdA==')

    def test_cross_origin_fallback_captures_only_when_qr_source_changes(self):
        self.fixture='qr-cors'
        self.preview(5)
        self.assertEqual(self.screenshots,1)
        self.source='https://example.invalid/qr-two.png'
        self.preview(5)
        self.assertEqual(self.screenshots,2)
        self.assertEqual(self.clicks,[])

    def test_same_page_can_evaluate_repeatedly_without_declaration_errors(self):
        self.fixture='qr'
        self.preview()
        result=subprocess.run([shutil.which('node'),'-e',DOM_RUNNER],
            input=json.dumps({'expression':self.expressions[0],'fixture':'qr','repeat':5}),
            capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)
        self.assertEqual(json.loads(result.stdout)['clicks'],[])

    def test_each_dedicated_target_has_its_own_single_open(self):
        self.preview(2)
        self.target['webSocketDebuggerUrl']='ws://127.0.0.1:1234/devtools/page/other'
        self.preview(2)
        self.assertEqual(self.clicks,['登录','登录'])


if __name__=='__main__':unittest.main()
