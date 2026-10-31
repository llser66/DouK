"""User-driven official Douyin login in an isolated Chrome profile.

Only the dedicated window's Douyin cookies are read. Credentials never leave
the local settings callback and are never returned by the HTTP status API.
"""
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener


LOGIN_URL = 'https://www.douyin.com/'
ACTIVE_STATES = {'opening', 'waiting', 'cancelling'}


def find_chrome():
    candidates = [
        Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('LOCALAPPDATA', '')) / 'Google/Chrome/Application/chrome.exe',
    ]
    return next((str(p) for p in candidates if p.is_file()), None) or shutil.which('google-chrome') or shutil.which('chromium')


def _read_exact(stream, count):
    output = bytearray()
    while len(output) < count:
        chunk = stream.read(count-len(output))
        if not chunk:
            raise ConnectionError('专用登录窗口已关闭')
        output.extend(chunk)
    return bytes(output)


def _send_frame(sock, data, opcode=1):
    mask = os.urandom(4)
    size = len(data)
    header = bytes([0x80 | opcode])
    header += bytes([0x80 | size]) if size < 126 else bytes([0x80 | 126]) + struct.pack('!H', size) if size < 65536 else bytes([0x80 | 127]) + struct.pack('!Q', size)
    sock.sendall(header + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(data)))


class CDPCommandError(ValueError):
    def __init__(self, code=None):
        self.code = code if type(code) is int else None
        super().__init__('读取专用登录窗口失败，请关闭窗口后重新登录')


def cdp_request(url, method, params=None):
    """One local DevTools command over RFC 6455, no extra Python dependency."""
    p = urlparse(url)
    if p.scheme != 'ws' or p.hostname not in {'127.0.0.1', 'localhost'} or not p.path.startswith('/devtools/') or p.username or p.query or p.fragment:
        raise ValueError('登录窗口连接地址无效')
    with socket.create_connection(('127.0.0.1', p.port), timeout=4) as sock:
        key = base64.b64encode(os.urandom(16)).decode()
        request = f'GET {p.path} HTTP/1.1\r\nHost: 127.0.0.1:{p.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'
        sock.sendall(request.encode('ascii'))
        with sock.makefile('rb') as stream:
            status = stream.readline(4096)
            if not status.startswith(b'HTTP/1.1 101 '):
                raise ConnectionError('无法连接专用登录窗口')
            headers = {}
            for _ in range(80):
                line = stream.readline(4096)
                if line == b'\r\n':
                    break
                if b':' not in line:
                    raise ConnectionError('登录窗口响应无效')
                name, value = line.decode('latin-1').split(':', 1)
                headers[name.lower()] = value.strip()
            else:
                raise ConnectionError('登录窗口响应过长')
            accept = base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if headers.get('sec-websocket-accept') != accept:
                raise ConnectionError('登录窗口握手验证失败')
            _send_frame(sock, json.dumps({'id': 1, 'method': method, 'params': params or {}}).encode())
            combined = bytearray()
            deadline = time.monotonic()+8
            while time.monotonic() < deadline:
                a, b = _read_exact(stream, 2)
                size = b & 127
                if size == 126:
                    size = struct.unpack('!H', _read_exact(stream, 2))[0]
                elif size == 127:
                    size = struct.unpack('!Q', _read_exact(stream, 8))[0]
                if size > 4*1024*1024 or len(combined)+size > 4*1024*1024:
                    raise ValueError('登录窗口响应过大')
                mask = _read_exact(stream, 4) if b & 128 else None
                payload = _read_exact(stream, size)
                if mask:
                    payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
                opcode = a & 15
                if opcode == 8:
                    raise ConnectionError('专用登录窗口已关闭')
                if opcode == 9:
                    _send_frame(sock, payload, 10)
                    continue
                if opcode not in {0, 1}:
                    continue
                combined.extend(payload)
                if not a & 128:
                    continue
                message = json.loads(combined)
                combined.clear()
                if message.get('id') != 1:
                    continue
                if 'error' in message:
                    raise CDPCommandError((message.get('error') or {}).get('code'))
                return message.get('result', {})
    raise TimeoutError('专用登录窗口响应超时')


def cookie_header(cookies, now=None):
    """Accept only non-expired cookies scoped to the Douyin HTTPS homepage."""
    now = time.time() if now is None else now
    selected = {}
    for cookie in sorted(cookies, key=lambda c: c.get('path', '/') != '/'):
        domain = str(cookie.get('domain', '')).lower().lstrip('.')
        name, value = cookie.get('name'), cookie.get('value')
        if domain not in {'douyin.com', 'www.douyin.com'} or cookie.get('path', '/') != '/':
            continue
        if cookie.get('expires', -1) > 0 and cookie['expires'] <= now:
            continue
        if not isinstance(name, str) or not isinstance(value, str) or not name or any(c in name+value for c in '\r\n;'):
            continue
        selected[name] = value
    # Guest cookies such as ttwid do not prove that the user has logged in.
    if not any(selected.get(k) for k in ('sessionid', 'sessionid_ss')):
        return None
    return '; '.join(f'{name}={value}' for name, value in selected.items())


class DouyinLogin:
    def __init__(self, profile, save_cookie, has_cookie, capture_profile=False, qr_mode=False):
        self.profile = Path(profile)
        self.save_cookie = save_cookie
        self.has_cookie = has_cookie
        self.capture_profile = capture_profile
        self.qr_mode = qr_mode
        self.qr_image = ''
        self.qr_actions = {}
        self.qr_previews = {}
        self.lock = threading.RLock()
        self.event = None
        self.thread = None
        self.process = None
        self.endpoint = None
        self.state = 'idle'
        self.message = '点击登录，在官方窗口完成扫码后自动保存 Cookie'

    def status(self):
        with self.lock:
            return {'state': self.state, 'message': self.message, 'available': bool(find_chrome()), 'has_cookie': self.has_cookie()}

    def _set(self, state, message):
        with self.lock:
            self.state, self.message = state, message

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.status()
            browser = find_chrome()
            if not browser:
                raise ValueError('未找到 Chrome。请安装 Chrome，或使用下方手动填写方式')
            self.profile.mkdir(parents=True, exist_ok=True)
            self.endpoint = None
            self.process = None
            self.qr_image = ''
            self.qr_actions = {}
            self.qr_previews = {}
            self.event = threading.Event()
            self._set('opening', '正在打开抖音官方登录窗口…')
            self.thread = threading.Thread(target=self._run, args=(browser, self.event), daemon=True)
            self.thread.start()
            return self.status()

    def cancel(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                self.event.set()
                self._set('cancelling', '正在关闭专用登录窗口…')
        return self.status()

    def _http(self, path):
        # Bypass proxy environment variables for this loopback-only endpoint.
        with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{self.endpoint[0]}{path}', timeout=3) as response:
            return json.load(response)

    def _read_endpoint(self):
        try:
            lines = (self.profile / 'DevToolsActivePort').read_text('ascii').splitlines()
            port, path = int(lines[0]), lines[1]
            if not 0 < port < 65536 or not path.startswith('/devtools/browser/'):
                return False
            self.endpoint = (port, path)
            version = self._http('/json/version')
            expected = f'ws://127.0.0.1:{port}{path}'
            if version.get('webSocketDebuggerUrl') != expected:
                self.endpoint = None
                return False
            return True
        except (OSError, ValueError, IndexError):
            self.endpoint = None
            return False

    def _cookies(self):
        # Read our dedicated profile's browser context. A page target can be
        # replaced or detached during official login redirects; its lifetime
        # must not determine whether the authenticated cookie can be read.
        port, path = self.endpoint
        try:
            result = cdp_request(f'ws://127.0.0.1:{port}{path}', 'Storage.getCookies')
            return cookie_header(result.get('cookies', []))
        except CDPCommandError as exc:
            if exc.code != -32601:
                raise
            # Older Chrome versions may not expose Storage on the browser target.
            # Fall back only inside this dedicated profile, never normal Chrome.
            for target in self._http('/json/list'):
                page = urlparse(target.get('url', ''))
                endpoint = target.get('webSocketDebuggerUrl', '')
                websocket = urlparse(endpoint)
                if (target.get('type') == 'page' and page.scheme == 'https' and
                    page.hostname in {'douyin.com','www.douyin.com'} and
                    websocket.netloc == f'127.0.0.1:{port}' and websocket.path.startswith('/devtools/page/')):
                    result = cdp_request(endpoint, 'Network.getCookies', {'urls':['https://www.douyin.com/']})
                    return cookie_header(result.get('cookies', []))
            raise ConnectionError('专用登录页面暂时未就绪') from None

    def _identity(self, event):
        """Read only the official site's own-profile navigation, never a new data API.

        A generic author link or the current URL cannot identify the logged-in user.
        If the page does not expose its own-profile link, let the user confirm it.
        """
        # On the official /user/self page, the canonical profile identifies the
        # signed-in user. A canonical URL on an arbitrary author's page does not.
        expression = r'''(() => {
          const profileName=()=>{
            const heading=document.querySelector('#user_detail_element h1,[data-e2e="user-title"],h1');
            const name=(heading?.innerText||'').trim();
            if(name)return name.slice(0,80);
            const title=document.title.match(/^(.+?)的抖音(?:\s*[-–—|]\s*抖音)?$/);
            return title?title[1].trim().slice(0,80):'';
          };
          if(location.pathname==='/user/self') {
            const canonical=document.querySelector('link[rel="canonical"]')?.getAttribute('href');
            try {const u=new URL(canonical);const m=u.pathname.match(/^\/user\/([A-Za-z0-9_-]{10,200})\/?$/);
              if(u.protocol==='https:'&&u.hostname==='www.douyin.com'&&m)return {sec_uid:m[1],homepage:u.origin+u.pathname,nickname:profileName()};
            }catch(_){}
          }
          const links = Array.from(document.querySelectorAll('a[href]'));
          for (const a of links) {
            const label = [a.innerText, a.getAttribute('aria-label'), a.title].filter(Boolean).join(' ').trim();
            if (!/^(个人主页|我的主页)(\s|$)/.test(label)) continue;
            try {
              const u = new URL(a.getAttribute('href'), location.href);
              if (u.protocol !== 'https:' || !['douyin.com','www.douyin.com'].includes(u.hostname)) continue;
              const match = u.pathname.match(/^\/user\/([A-Za-z0-9_-]{10,200})\/?$/);
              if (match) return {sec_uid:match[1], homepage:u.origin + u.pathname,
                nickname:location.pathname===u.pathname?profileName():''};
            } catch (_) {}
          }
          return null;
        })()'''
        navigated = False
        for _ in range(15):
            if event.is_set():
                return None
            for target in self._http('/json/list'):
                p = urlparse(target.get('url', ''))
                if target.get('type') != 'page' or p.scheme != 'https' or p.hostname not in {'douyin.com','www.douyin.com'}:
                    continue
                if self.qr_mode and not navigated:
                    cdp_request(target['webSocketDebuggerUrl'],'Page.navigate',{'url':'https://www.douyin.com/user/self'})
                    navigated = True
                    break
                result = cdp_request(target['webSocketDebuggerUrl'], 'Runtime.evaluate', {'expression':expression,'returnByValue':True})
                identity = (result.get('result') or {}).get('value')
                if isinstance(identity, dict) and identity.get('sec_uid'):
                    return identity
            if event.wait(1):
                return None
        return None

    def _qr_preview(self):
        """Fixed actions on our isolated official page; never expose arbitrary CDP.

        Read the QR bitmap directly. Repeated Page.captureScreenshot calls can
        resize/repaint a visible Chrome surface, so a fallback capture is made
        only once for each changed QR source. Never capture a signed-in page.
        """
        script = r'''(() => {
          const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'};
          const all=[...document.querySelectorAll('button,a,div,span')].filter(visible);
          // Inspect before acting: clicking an already open login toggles/rebuilds it.
          for(const image of document.querySelectorAll('img,canvas')) {
            if(!visible(image))continue;
            const q=image.getBoundingClientRect();
            if(q.width<120||q.width>350||q.height<120||q.height>350||Math.abs(q.width-q.height)>30)continue;
            for(let e=image.parentElement;e&&e!==document.body;e=e.parentElement){
              const r=e.getBoundingClientRect();if(r.width>800||r.height>950)break;
              if(/扫码|二维码/.test(e.innerText)){
                let bitmap='';
                try{
                  if(image.tagName==='CANVAS')bitmap=image.toDataURL('image/png');
                  else if(image.complete&&image.naturalWidth){
                    const canvas=document.createElement('canvas');canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;
                    canvas.getContext('2d').drawImage(image,0,0);bitmap=canvas.toDataURL('image/png');
                  }
                }catch(_){}
                return {image:bitmap,key:image.currentSrc||image.src||'qr-canvas',clip:{x:Math.max(0,q.x-8),y:Math.max(0,q.y-8),width:q.width+16,height:q.height+16,scale:1}};
              }
            }
          }
          const dialogs=[...document.querySelectorAll('[role="dialog"],[class*="login-pannel"],[class*="login-panel"],[class*="login-modal"],[class*="loginModal"],[class*="login-container"]')].filter(visible);
          // At most one open and one QR-tab selection per page target. Later polls
          // only observe, even if the official page is slow or replaces its nodes.
          const qrTab=all.find(e=>e.textContent.trim()==='扫码登录'&&e.children.length<3);
          if(allowed.switch&&qrTab){qrTab.click();return {action:'switch'};}
          let d=dialogs.find(e=>/扫码|二维码/.test(e.innerText));
          if(!d){const t=all.find(e=>e.children.length===0&&/^(扫码登录|请使用抖音.*扫码|打开抖音.*扫码)$/.test(e.textContent.trim()));
            for(let e=t;e&&e!==document.body;e=e.parentElement){const r=e.getBoundingClientRect();if(r.width>=260&&r.width<=700&&r.height>=260&&r.height<=800&&e.querySelector('img,canvas')){d=e;break}}}
          if(d){const r=d.getBoundingClientRect();
            if(r.width>=200&&r.height>=200&&r.width<=750&&r.height<=900)return {key:'login-dialog',clip:{x:Math.max(0,r.x),y:Math.max(0,r.y),width:r.width,height:r.height,scale:1}};
          }
          if(allowed.open&&!dialogs.length&&!qrTab){
            const login=all.find(e=>e.textContent.trim()==='登录'&&e.children.length<3);
            if(login){login.click();return {action:'open'};}
          }
          return null;
        })()'''
        for target in self._http('/json/list'):
            p = urlparse(target.get('url',''))
            if target.get('type')!='page' or p.scheme!='https' or p.hostname not in {'douyin.com','www.douyin.com'}:
                continue
            ws=target['webSocketDebuggerUrl']
            actions=self.qr_actions.setdefault(ws,set())
            expression='(() => {const allowed='+json.dumps({'open':'open' not in actions,'switch':'switch' not in actions})+';return '+script+';})()'
            result=cdp_request(ws,'Runtime.evaluate',{'expression':expression,'returnByValue':True})
            preview=(result.get('result') or {}).get('value')
            if not isinstance(preview,dict):
                continue
            if preview.get('action') in {'open','switch'}:
                actions.add(preview['action'])
            bitmap=preview.get('image','')
            if isinstance(bitmap,str) and len(bitmap)<1500000 and re.fullmatch(r'data:image/png;base64,[A-Za-z0-9+/]+={0,2}',bitmap):
                self.qr_image=bitmap
                return
            clip=preview.get('clip')
            if not isinstance(clip,dict):
                continue
            source=str(preview.get('key') or 'login-dialog')
            # The official dialog may animate a timer or an icon. These do not
            # mean the QR changed, and must not trigger a screenshot every poll.
            if self.qr_previews.get(ws)==source:
                return
            picture=cdp_request(ws,'Page.captureScreenshot',{'format':'png','clip':clip,'fromSurface':False,'captureBeyondViewport':False}).get('data','')
            if picture and len(picture)<1500000:
                self.qr_image='data:image/png;base64,'+picture
                self.qr_previews[ws]=source
                return

    def _close(self):
        if self.endpoint:
            try:
                port, path = self.endpoint
                cdp_request(f'ws://127.0.0.1:{port}{path}', 'Browser.close')
            except Exception:
                pass
        if self.process and self.process.poll() is None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
        self.endpoint = None

    def _run(self, browser, event):
        outcome = ('failed', '登录未完成，请重试')
        stage='startup'
        try:
            # Reconnect only to our own dedicated profile if the local service restarted.
            if not self._read_endpoint():
                active = self.profile / 'DevToolsActivePort'
                active.unlink(missing_ok=True)
                self.process = subprocess.Popen([
                    browser, f'--user-data-dir={self.profile}', '--remote-debugging-port=0',
                    '--remote-debugging-address=127.0.0.1', '--no-first-run',
                    '--no-default-browser-check',
                    *(['--new-window','--window-size=1280,1000','--mute-audio'] if self.qr_mode else ['--new-window']),
                    'https://www.douyin.com/user/self' if self.qr_mode else LOGIN_URL,
                ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                startup = time.monotonic()+25
                while not self._read_endpoint():
                    if event.wait(.25):
                        return
                    if self.process.poll() is not None:
                        raise ValueError('专用登录窗口未能启动，请关闭旧的专用窗口后重试')
                    if time.monotonic() > startup:
                        raise ValueError('登录窗口未能就绪，请关闭专用 Chrome 窗口后重试')
            self._set('waiting', '请用手机抖音扫描下方官方二维码并确认登录；无需网站密码' if self.qr_mode else '请在新窗口点击抖音「登录」并扫码或验证，完成后将自动保存')
            deadline = time.monotonic()+600
            qr_deadline = time.monotonic()+90
            cookie_failures=0
            while time.monotonic() < deadline:
                if event.is_set():
                    return
                try:
                    stage='cookies'
                    header = self._cookies()
                    cookie_failures=0
                except (OSError, ConnectionError, ValueError) as exc:
                    # Official login redirects can replace the page target between
                    # listing it and reading cookies. Do not close a live window
                    # after one transient DevTools/navigation failure.
                    cookie_failures+=1
                    if cookie_failures>=5:
                        logging.getLogger(__name__).warning('Dedicated login cookie read failed (%s, protocol code %s)',type(exc).__name__,getattr(exc,'code',None))
                        raise ValueError('专用登录窗口已关闭或暂时无响应，请重新登录')
                    if event.wait(1.5):
                        return
                    continue
                if header:
                    self.qr_image = ''
                    identity = None
                    if self.capture_profile:
                        try:
                            stage='identity'
                            identity = self._identity(event)
                        except Exception:
                            # Profile navigation may be absent or still loading after login.
                            # Preserve the authenticated session and ask for the own homepage.
                            identity = None
                    if event.is_set():
                        return
                    stage='save'
                    message = self.save_cookie(header, identity) if self.capture_profile else self.save_cookie(header)
                    outcome = ('saved', message if isinstance(message,str) else '已检测到抖音登录并自动保存 Cookie，无需手动填写')
                    return
                if self.qr_mode:
                    try:
                        self._qr_preview()
                    except Exception:
                        pass  # Loading or official verification may temporarily hide the dialog.
                    if self.qr_image:
                        self._set('waiting','请用手机抖音扫描下方官方二维码并确认登录；无需网站密码')
                    else:
                        self._set('waiting','正在加载官方扫码登录；如长时间没有二维码，可取消后直接报送，由管理员核实')
                        if time.monotonic()>qr_deadline:
                            outcome=('failed','抖音官方二维码未能加载，请稍后重试或让管理员检查服务器登录窗口；本次可直接报送并补充备注')
                            return
                if event.wait(1.5):
                    return
            outcome = ('failed', '等待登录超过 10 分钟，请重新点击登录抖音')
        except Exception as exc:
            # Never expose DevTools payloads, URLs or cookie values in an exception.
            logging.getLogger(__name__).warning('Douyin login synchronization failed at %s (%s)',stage,type(exc).__name__)
            outcome = ('failed', '未能完成抖音登录同步。请检查登录窗口、网络后重试，或使用手动填写')
        finally:
            self._close()
            self.qr_image = ''
            if self.qr_mode:
                profile=self.profile.resolve()
                if profile.parent.name=='report-browsers' and len(profile.name)==32 and all(c in '0123456789abcdef' for c in profile.name):
                    shutil.rmtree(profile, ignore_errors=True)
            if event.is_set():
                outcome = ('cancelled', '已取消本次登录，原有 Cookie 保留')
            self._set(*outcome)
