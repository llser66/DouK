"""DouK local workspace. Python 3.12+, standard library only."""
import argparse
import csv
from contextlib import contextmanager
import hashlib
import io
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen
import uuid
import zipfile
from xml.sax.saxutils import escape as xml_escape
from douyin_login import DouyinLogin
from api_service import APIService
from log_store import LogStore
from operations import Operations, AccessError
from ops_accounts import EmployeeAccounts
from report_accounts import ReportAccounts
from lan_access import bind_host, trusted_request, network_info
from workspace_backups import BackupStore
from work_targets import WorkTargets

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DOWNLOADS = ROOT / 'Downloads'
DATA.mkdir(exist_ok=True)
DOWNLOADS.mkdir(exist_ok=True)
DB = DATA / 'workspace.sqlite'
CONFIG = DATA / 'settings.json'
LOCK = threading.RLock()
CANCEL = {}
PROCESSES = {}
JOB_LOCK = threading.Lock()
CONNECTED = {'connected': False, 'connection_message': '尚未连接本机 DouK API'}
DEFAULTS = {'api_url': 'http://127.0.0.1:5555', 'token': '', 'cookie': '', 'cookie_tiktok': '', 'default_account_id': '', 'skip': True, 'resume': True, 'incremental': True, 'earliest': '', 'latest': ''}
LOGIN_SESSIONS = {}
API_SERVICE = APIService(ROOT)
MODES = {'detail', 'account', 'mix', 'collection', 'comment', 'user', 'search/video', 'search/user', 'hot', 'live'}
TYPES = {'video', 'image', 'livephoto', 'static_cover', 'dynamic_cover'}


@contextmanager
def connect_db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


with connect_db() as db:
    db.executescript('''
    CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS files(key TEXT PRIMARY KEY, path TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
    ''')


def log_secrets():
    values = []
    config = settings()
    values.extend(config.get(k, '') for k in ('cookie', 'cookie_tiktok', 'token'))
    values.extend(a.get('cookie', '') for a in all_accounts())
    with connect_db() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='ops_account_logins'").fetchone():
            values.extend(r[0] for r in db.execute("SELECT cookie FROM ops_account_logins WHERE cookie<>''"))
    return values


LOGS = LogStore(lambda: connect_db(), log_secrets)


def settings():
    with LOCK:
        return {**DEFAULTS, **(json.loads(CONFIG.read_text('utf-8')) if CONFIG.exists() else {})}


def public_settings():
    s = settings()
    for key in ('token', 'cookie', 'cookie_tiktok'):
        s['has_' + key] = bool(s.pop(key, ''))
    return s


def all_accounts():
    with connect_db() as db:
        return [json.loads(row[0]) for row in db.execute('SELECT payload FROM accounts ORDER BY rowid')]


def get_account(id_):
    with connect_db() as db:
        row = db.execute('SELECT payload FROM accounts WHERE id=?', (id_,)).fetchone()
    if not row:
        raise ValueError('账号不存在')
    return json.loads(row[0])


def save_account(account, **updates):
    with LOCK:
        account.update(updates)
        with connect_db() as db:
            db.execute('INSERT INTO accounts(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (account['id'], json.dumps(account, ensure_ascii=False)))


def public_account(account):
    result = {k: v for k, v in account.items() if k != 'cookie'}
    result['has_cookie'] = bool(account.get('cookie'))
    result['is_default'] = settings()['default_account_id'] == account['id']
    session = LOGIN_SESSIONS.get(account['id'])
    result['login'] = session.status() if session else {'state': 'idle', 'message': '尚未开始登录'}
    return result


def create_account(data):
    name = str(data.get('name', '')).strip()
    if not 1 <= len(name) <= 40:
        raise ValueError('请填写 1 到 40 字的账号备注名称')
    account = dict(id=uuid.uuid4().hex[:12], name=name, platform='douyin', cookie='', paused=False,
                   cooldown_seconds=30, last_request=0, cooldown_until=0, failure_count=0,
                   last_error='', last_login='', created=datetime.now().astimezone().isoformat(timespec='seconds'))
    save_account(account)
    if not settings()['default_account_id']:
        save_settings({'default_account_id': account['id']})
    return public_account(account)


def update_account(id_, data):
    with LOCK:
        account = get_account(id_)
        updates = {}
        if 'name' in data:
            name = str(data['name']).strip()
            if not 1 <= len(name) <= 40:
                raise ValueError('备注名称应为 1 到 40 字')
            updates['name'] = name
        if 'cooldown_seconds' in data:
            interval = int(data['cooldown_seconds'])
            if not 5 <= interval <= 3600:
                raise ValueError('请求间隔应为 5 到 3600 秒')
            updates['cooldown_seconds'] = interval
        if 'paused' in data:
            if not isinstance(data['paused'], bool):
                raise ValueError('暂停状态无效')
            updates['paused'] = data['paused']
            if not data['paused']:
                updates.update(failure_count=0, last_error='')
        if data.get('is_default'):
            save_settings({'default_account_id': id_})
        if data.get('cookie'):
            cookie = str(data['cookie']).strip()
            if '\n' in cookie or '\r' in cookie or len(cookie) > 100000:
                raise ValueError('Cookie 应为单行请求头内容')
            updates.update(cookie=cookie, last_login=datetime.now().astimezone().isoformat(timespec='seconds'), failure_count=0, last_error='')
        save_account(account, **updates)
        return public_account(account)


def login_session(id_):
    with LOCK:
        get_account(id_)
        if id_ not in LOGIN_SESSIONS:
            LOGIN_SESSIONS[id_] = DouyinLogin(
                DATA / 'account-browsers' / id_,
                lambda cookie: update_account(id_, {'cookie': cookie}),
                lambda: bool(get_account(id_).get('cookie')),
            )
        return LOGIN_SESSIONS[id_]


def start_login(id_):
    with LOCK:
        if any(s.state in {'opening','waiting','cancelling'} for s in OPS_ACCOUNTS.sessions.values()):
            raise ValueError('请先完成或取消员工账号的登录，避免窗口混淆')
        for other_id, session in LOGIN_SESSIONS.items():
            if other_id != id_ and session.status()['state'] in {'opening', 'waiting', 'cancelling'}:
                raise ValueError('请先完成或取消另一个账号的登录，避免窗口混淆')
        session = login_session(id_)
    return session.start()


def prepare_account(t, config):
    id_ = t['request'].get('account_id')
    if not id_:
        return config
    while True:
        check_cancel(t)
        with LOCK:
            account = get_account(id_)
            if account['paused']:
                raise ValueError('所选账号已暂停，请在账号管理中检查后再恢复')
            if not account.get('cookie'):
                raise ValueError('所选账号尚未登录')
            until = max(account['last_request']+account['cooldown_seconds'], account['cooldown_until'])
            remaining = until-time.time()
            if remaining <= 0:
                save_account(account, last_request=time.time())
                return {**config, 'cookie': account['cookie']}
        save_task(t, message=f'账号「{account["name"]}」请求间隔中，还需 {int(remaining)+1} 秒')
        CANCEL[t['id']].wait(min(1, remaining))


def pool_accounts():
    return [a for a in all_accounts() if a['platform'] == 'douyin' and a.get('cookie') and not a['paused']]


def take_pool_account(t, config):
    """Round robin over enabled accounts, waiting when every account is cooling down."""
    while True:
        check_cancel(t)
        with LOCK:
            accounts = pool_accounts()
            if not accounts:
                raise ValueError('账号池中没有可用账号，请在账号管理中登录并启用至少一个账号')
            ids = [a['id'] for a in accounts]
            last = t.get('comment_state', {}).get('last_account_id')
            start = (ids.index(last)+1) % len(ids) if last in ids else 0
            ordered = accounts[start:]+accounts[:start]
            now = time.time()
            for account in ordered:
                if max(account['last_request']+account['cooldown_seconds'], account['cooldown_until']) <= now:
                    save_account(account, last_request=now)
                    return account, {**config, 'cookie': account['cookie']}
            remaining = min(max(a['last_request']+a['cooldown_seconds'], a['cooldown_until'])-now for a in ordered)
        save_task(t, message=f'账号池休息中，约 {int(remaining)+1} 秒后自动继续；已采集 {t["record_count"]} 条')
        CANCEL[t['id']].wait(min(1, max(.05, remaining)))


def get_comment_page(detail_id, job, req, config):
    body = {'detail_id': detail_id, 'cookie': config['cookie'], 'cursor': job['cursor'],
            'count': req['count_reply'] if job.get('comment_id') else req['batch_size']}
    if job.get('comment_id'):
        body['comment_id'] = job['comment_id']
    result = upstream('/douyin/comment/page', body, config)
    if not isinstance(result.get('data'), list):
        raise ValueError(result.get('message') or '未能获取评论分页')
    if not isinstance(result.get('cursor'), int) or not isinstance(result.get('has_more'), bool):
        raise ValueError('评论分页响应缺少有效游标，请更新增强 API 后重试')
    if result['has_more'] and result['cursor'] <= job['cursor']:
        raise ValueError('平台未推进评论翻页位置，已停止以避免重复采集')
    return result


def commit_comment_page(t, account, rows, seen):
    unique = []
    for row in rows:
        cid = str(row.get('cid', ''))
        if cid and cid not in seen:
            seen.add(cid)
            unique.append({**row, 'collection_account': account['name'], 'work_id': t['comment_state']['detail_id']})
    t['record_count'] += len(unique)
    usage = t.setdefault('account_usage', {}).setdefault(account['id'], {'name': account['name'], 'batches': 0, 'records': 0})
    usage['batches'] += 1
    usage['records'] += len(unique)
    t['current_account'] = account['name']
    t['message'] = f'账号「{account["name"]}」完成一批，累计 {t["record_count"]} 条；主评论 {t.get("comment_pages",0)} 页'
    # Page records and the next cursor are committed together, so a retry resumes safely.
    with LOCK, connect_db() as db:
        db.executemany('INSERT INTO records(task_id,payload) VALUES(?,?)', [(t['id'], json.dumps(row, ensure_ascii=False)) for row in unique])
        db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(t, ensure_ascii=False), t['id']))


def collect_comment_pool(t, config):
    req = t['request']
    entries = sources(req['links'], 'comment')
    checkpoint = t.setdefault('comment_state', {'source_index': 0, 'detail_id': None, 'main_pages': 0,
                                               'jobs': [{'cursor': 0}], 'last_account_id': None})
    seen = {str(row.get('cid')) for row in records(t['id']) if row.get('cid')}
    while checkpoint['source_index'] < len(entries):
        check_cancel(t)
        if checkpoint['detail_id'] is None:
            checkpoint['detail_id'] = identifier(entries[checkpoint['source_index']], 'comment', 'douyin', config)
            save_task(t)
        if not checkpoint['jobs']:
            checkpoint.update(source_index=checkpoint['source_index']+1, detail_id=None, main_pages=0, jobs=[{'cursor': 0}])
            save_task(t)
            continue
        job = checkpoint['jobs'][0]
        account, source_config = take_pool_account(t, config)
        save_task(t, current_account=account['name'], message=f'账号「{account["name"]}」正在采集一批{"回复" if job.get("comment_id") else "评论"}，已保存 {t["record_count"]} 条')
        try:
            page = get_comment_page(checkpoint['detail_id'], job, req, source_config)
        except Exception as exc:
            account_request_result(account['id'], exc)
            raise ValueError(f'账号「{account["name"]}」本批未完成，已保留进度：{exc}') from exc
        account_request_result(account['id'])
        check_cancel(t)
        checkpoint['jobs'].pop(0)
        checkpoint['last_account_id'] = account['id']
        if not job.get('comment_id'):
            checkpoint['main_pages'] += 1
            t['comment_pages'] = t.get('comment_pages', 0)+1
            if page['has_more'] and (req['collect_all'] or checkpoint['main_pages'] < req['pages']):
                checkpoint['jobs'].append({'cursor': page['cursor']})
            if req.get('reply'):
                replies = [{'cursor': 0, 'comment_id': str(row['cid'])} for row in page['data'] if row.get('cid') and row.get('reply_comment_total', 0)>0]
                checkpoint['jobs'][0:0] = replies
        if not req['collect_all']:
            t['progress'] = min(95, int(t.get('comment_pages', 0)/(req['pages']*len(entries))*95))
        commit_comment_page(t, account, page['data'], seen)
    save_task(t, status='completed', progress=100, message=f'已采集 {t["record_count"]} 条评论，完成 {t.get("comment_pages",0)} 页，使用 {len(t.get("account_usage",{}))} 个账号')


def account_request_result(id_, error=None):
    if not id_:
        return
    if error and any(text in str(error) for text in ('无法连接本机', '没有此接口', '无法识别', '请输入', '无法提取')):
        return
    with LOCK:
        account = get_account(id_)
        if not error:
            save_account(account, failure_count=0, last_error='')
            return
        failures = account['failure_count']+1
        save_account(account, failure_count=failures, last_error='采集请求失败，请检查 Cookie、权限或平台限制',
                     cooldown_until=time.time()+min(900, 60*2**min(failures-1, 4)),
                     paused=account['paused'] or failures >= 3)


def migrate_legacy_account():
    legacy = settings()['cookie']
    if legacy and not all_accounts():
        created = create_account({'name': '原有抖音账号'})
        update_account(created['id'], {'cookie': legacy})


def save_settings(data):
    s = settings()
    if 'api_url' in data:
        u = urlparse(data['api_url'])
        if u.scheme != 'http' or u.hostname not in {'127.0.0.1', 'localhost', '::1'} or u.username or u.path not in {'', '/'} or u.query or u.fragment:
            raise ValueError('API 地址必须是本机 HTTP 地址，例如 http://127.0.0.1:5555')
    if data.get('default_account_id'):
        get_account(data['default_account_id'])
    for k in DEFAULTS:
        if k not in data:
            continue
        if k in {'token', 'cookie', 'cookie_tiktok'} and not data[k]:
            continue
        if k in {'skip', 'resume', 'incremental'}:
            if not isinstance(data[k], bool):
                raise ValueError('规则开关必须为布尔值')
        elif not isinstance(data[k], str):
            raise ValueError('配置值类型无效')
        s[k] = data[k].rstrip('/') if k == 'api_url' else data[k]
    if data.get('clear_secrets'):
        for key in ('token', 'cookie', 'cookie_tiktok'):
            s[key] = ''
    validate_dates(s)
    with LOCK:
        tmp = CONFIG.with_suffix('.tmp')
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), 'utf-8')
        tmp.replace(CONFIG)
    return public_settings()


def upstream(path, payload=None, config=None):
    s = config or settings()
    req = Request(s['api_url'] + path, data=None if payload is None else json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'token': s['token']})
    try:
        with urlopen(req, timeout=180 if payload is not None else 5) as r:
            return json.load(r)
    except HTTPError as exc:
        if exc.code == 404:
            raise ValueError('当前上游 API 没有此接口，请使用 start-enhanced-api.ps1 启动增强 API') from exc
        try:
            detail = json.loads(exc.read()).get('detail', str(exc))
            if isinstance(detail, list):
                detail = '; '.join(str(x.get('msg', x)) for x in detail)
        except Exception:
            detail = str(exc)
        raise ValueError(f'上游 API 返回 {exc.code}：{detail}') from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError('无法连接本机 DouK API。请先启动原项目 Web API 模式，并在连接设置中测试。') from exc


def test_connection():
    try:
        upstream('/token')
        s = upstream('/settings')
        CONNECTED.update(connected=True, connection_message='连接成功，本机 DouK API 已就绪', douyin=bool(settings()['cookie'] or s.get('cookie') or any(a.get('cookie') for a in all_accounts())), tiktok=bool(settings()['cookie_tiktok'] or s.get('cookie_tiktok')))
    except Exception as e:
        CONNECTED.update(connected=False, connection_message=str(e), douyin=False, tiktok=False)
    return dict(CONNECTED)


def ensure_connection():
    result = test_connection()
    if result['connected']:
        return result
    if settings()['api_url'] not in {'http://127.0.0.1:5555', 'http://localhost:5555'}:
        return result
    API_SERVICE.ensure_ready()
    return test_connection()


def all_tasks(include_deleted=False):
    with connect_db() as db:
        tasks = [json.loads(r[0]) for r in db.execute('SELECT payload FROM tasks ORDER BY rowid DESC')]
    return tasks if include_deleted else [t for t in tasks if not t.get('deleted_at')]


def get_task(id_):
    with connect_db() as db:
        row = db.execute('SELECT payload FROM tasks WHERE id=?', (id_,)).fetchone()
    if not row:
        raise ValueError('任务不存在')
    return json.loads(row[0])


def save_task(t, **updates):
    with LOCK:
        with connect_db() as db:
            previous = db.execute('SELECT payload FROM tasks WHERE id=?', (t['id'],)).fetchone()
        previous = json.loads(previous[0]) if previous else None
        t.update(updates)
        with connect_db() as db:
            db.execute('INSERT OR REPLACE INTO tasks(id,payload) VALUES(?,?)', (t['id'], json.dumps(t, ensure_ascii=False)))
        keys = ('status', 'message', 'record_count', 'deleted_at')
        if previous is None or any(previous.get(k) != t.get(k) for k in keys):
            event = '任务更新'
            if previous is None:
                event = '任务创建'
            elif not previous.get('deleted_at') and t.get('deleted_at'):
                event = '删除任务记录'
            elif previous.get('deleted_at') and not t.get('deleted_at'):
                event = '恢复任务记录'
            level = 'ERROR' if t.get('status') == 'failed' else 'WARN' if t.get('status') in {'partial', 'cancelled'} else 'INFO'
            LOGS.write(level, 'task', event, t.get('message', ''), t['id'], t.get('title', ''), t.get('status', ''))


def delete_task(id_):
    with LOCK:
        task = get_task(id_)
        if task['status'] in {'queued', 'running', 'recording'}:
            raise ValueError('运行中的任务请先取消，结束后再删除')
        if not task.get('deleted_at'):
            save_task(task, deleted_at=datetime.now().astimezone().isoformat(timespec='seconds'))
        return public_task(task)


def restore_task(id_):
    with LOCK:
        task = get_task(id_)
        if task.pop('deleted_at', None):
            save_task(task)
        return public_task(task)


def public_task(t, detail=False):
    mode = t.get('request', {}).get('mode', '')
    t = {k: v for k, v in t.items() if k not in {'request'}}
    t['mode'] = mode
    if not detail:
        t.pop('result', None)
    return t


def validate_dates(data):
    for k in ('earliest', 'latest'):
        if data.get(k):
            try:
                datetime.strptime(data[k], '%Y-%m-%d')
            except (ValueError, TypeError):
                raise ValueError('日期必须为 YYYY-MM-DD 格式')
    if data.get('earliest') and data.get('latest') and data['earliest'] > data['latest']:
        raise ValueError('起始日期不能晚于截止日期')


def create_task(data):
    if data.get('action') not in {'download', 'collect', 'record'} or data.get('mode') not in MODES or data.get('platform') not in {'douyin', 'tiktok'}:
        raise ValueError('任务类型或平台无效')
    if data['mode'] != 'hot' and not str(data.get('links', '')).strip():
        raise ValueError('请填写链接、ID 或关键词')
    if len(str(data.get('links', ''))) > 100000:
        raise ValueError('输入过长，请拆分任务')
    if data['platform'] == 'tiktok' and data['mode'] not in {'detail', 'account', 'mix', 'live'}:
        raise ValueError('此功能目前仅支持抖音')
    if data['action'] == 'download' and (not data.get('types') or not isinstance(data['types'], list) or not set(data['types']) <= TYPES):
        raise ValueError('请至少选择一种有效下载类型')
    if data['action'] == 'record' and data['mode'] != 'live':
        raise ValueError('录制任务仅支持直播模式')
    if data['action'] == 'download' and data['mode'] not in {'detail', 'account', 'collection', 'mix'}:
        raise ValueError('此来源不支持文件下载')
    for k, default, cap in [('duration', 30, 720), ('pages', 1, 100)]:
        try:
            data[k] = int(data.get(k, default))
        except (ValueError, TypeError):
            raise ValueError(f'{k} 必须为整数')
        if not 1 <= data[k] <= cap:
            raise ValueError(f'{k} 应在 1 到 {cap} 之间')
    validate_dates(data)
    if data['mode'] == 'comment':
        if not isinstance(data.get('reply', False), bool):
            raise ValueError('是否包含回复必须为布尔值')
        data['count_reply'] = int(data.get('count_reply', 3))
        if not 1 <= data['count_reply'] <= 100:
            raise ValueError('回复数量应为 1 到 100')
    account_mode = data.get('account_mode') or ('pool' if data['mode'] == 'comment' and not data.get('account_id') else 'single')
    if account_mode not in {'single', 'pool'}:
        raise ValueError('账号使用方式无效')
    if account_mode == 'pool' and (data['mode'] != 'comment' or data['action'] != 'collect' or data['platform'] != 'douyin'):
        raise ValueError('自动账号池目前用于抖音评论采集')
    if not isinstance(data.get('collect_all', False), bool):
        raise ValueError('是否采集到结束必须为布尔值')
    data['collect_all'] = data.get('collect_all', False)
    data['batch_size'] = int(data.get('batch_size', 20))
    if not 1 <= data['batch_size'] <= 50:
        raise ValueError('每批条数应为 1 到 50')
    data['account_mode'] = account_mode
    if account_mode == 'pool' and not pool_accounts():
        raise ValueError('账号池没有已登录且启用的账号，请先在账号管理中登录账号')
    account_id = '' if account_mode == 'pool' else data.get('account_id') or (settings()['default_account_id'] if data['platform'] == 'douyin' else '')
    account = None
    if account_id:
        account = get_account(account_id)
        if account['platform'] != data['platform']:
            raise ValueError('所选账号与任务平台不匹配')
        if account['paused']:
            raise ValueError('所选账号已暂停，请先在账号管理中检查')
        if not account.get('cookie'):
            raise ValueError('所选账号尚未登录，请先在账号管理中登录')
    data['account_id'] = account_id
    title = {'detail': '作品链接', 'account': '账号作品', 'mix': '合集作品', 'collection': '我的收藏', 'comment': '作品评论', 'user': '账号详情', 'hot': '抖音热榜', 'live': '直播', 'search/video': '作品搜索', 'search/user': '账号搜索'}[data['mode']]
    suffix = '下载' if data['action'] == 'download' else '录制' if data['action'] == 'record' else '采集'
    t = dict(id=uuid.uuid4().hex[:12], title=f'{title}{suffix} · {str(data.get("links", ""))[:55] or "实时榜单"}', platform=data['platform'], action=data['action'], account_name='自动账号池' if account_mode == 'pool' else account['name'] if account else '', status='queued', created=datetime.now().astimezone().isoformat(timespec='seconds'), message='等待处理', progress=0, record_count=0, files=[], request=data)
    save_task(t)
    CANCEL[t['id']] = threading.Event()
    threading.Thread(target=run_task, args=(t,), daemon=True).start()
    return public_task(t)


def check_cancel(t):
    if CANCEL[t['id']].is_set():
        raise InterruptedError('任务已取消；未完成的文件保留供重试')


def sources(text, mode):
    if mode.startswith('search/'):
        return [text.strip()]
    if mode == 'hot':
        return ['hot']
    return re.findall(r'https?://[^\s<>"，。]+', text) or [x.strip() for x in text.splitlines() if x.strip()]


def identifier(text, mode, platform, s):
    if not text.startswith('http'):
        return text
    p = urlparse(text)
    if p.scheme not in {'http', 'https'} or not p.hostname or not (p.hostname == 'douyin.com' or p.hostname.endswith('.douyin.com') or p.hostname == 'tiktok.com' or p.hostname.endswith('.tiktok.com')):
        raise ValueError('请输入抖音或 TikTok 平台链接')
    if p.hostname in {'v.douyin.com', 'vm.tiktok.com', 'vt.tiktok.com'}:
        resolved = upstream(f'/{platform}/share', {'text': text}, s).get('url')
        if not resolved:
            raise ValueError('短链接解析失败，请使用完整网页链接')
        text = resolved
        p = urlparse(text)
    query = parse_qs(p.query)
    if mode in {'account', 'user', 'collection'}:
        value = query.get('sec_uid', [None])[0] or (re.search(r'/user/([^/?]+)', p.path).group(1) if re.search(r'/user/([^/?]+)', p.path) else None)
        if not value:
            raise ValueError('无法提取账号 sec_uid。TikTok 账号请直接输入 secUid，而不是 @用户名')
        return unquote(value)
    if mode == 'mix':
        value = query.get('mix_id', query.get('collection_id', [None]))[0]
        if not value:
            m = re.search(r'/(?:collection|playlist|mix)/[^/?]*?(\d{6,})(?:/|$)', p.path)
            value = m.group(1) if m else None
        if not value:
            raise ValueError('无法识别合集 ID，请直接填写 mix_id')
        return value
    if mode == 'live':
        value = p.path.strip('/').split('/')[-1]
        if platform == 'tiktok' and not value.isdigit():
            raise ValueError('TikTok 直播请直接填写 room_id')
        return value
    m = re.search(r'/(?:video|note|photo)/(\d+)', p.path)
    value = query.get('modal_id', query.get('aweme_id', [None]))[0] or (m.group(1) if m else None)
    if not value:
        raise ValueError('无法识别作品 ID，请使用完整作品链接或直接输入数字 ID')
    return value


def get_data(source, req, s):
    mode, platform = req['mode'], req['platform']
    id_ = identifier(source, mode, platform, s) if mode not in {'hot', 'search/video', 'search/user'} else source
    payload = {'source': False}
    cookie = s['cookie_tiktok' if platform == 'tiktok' else 'cookie']
    if cookie:
        payload['cookie'] = cookie
    if mode in {'detail', 'comment'}:
        payload['detail_id'] = id_
    elif mode in {'account', 'collection', 'user'}:
        payload['sec_user_id'] = id_
    elif mode == 'mix':
        payload['mix_id'] = id_
    elif mode.startswith('search/'):
        payload.update(keyword=source, pages=req['pages'])
    elif mode == 'live':
        payload['room_id' if platform == 'tiktok' else 'web_rid'] = id_
    if mode == 'account':
        payload.update(tab=req.get('tab', 'post'), pages=req['pages'])
        for k in ('earliest', 'latest'):
            if req.get(k) or s.get(k):
                payload[k] = req.get(k) or s[k]
    if mode == 'comment':
        payload['pages'] = req['pages']
        payload['reply'] = req.get('reply') is True
        payload['count_reply'] = req.get('count_reply', 3)
    result = upstream(f'/{platform}/{mode}', payload, s)
    data = result.get('data')
    if data is None:
        raise ValueError(result.get('message') or '平台未返回数据，请检查 Cookie 和加密参数')
    return data if isinstance(data, list) else [data]


def persist_records(t, rows):
    with connect_db() as db:
        db.executemany('INSERT INTO records(task_id,payload) VALUES(?,?)', [(t['id'], json.dumps(row, ensure_ascii=False)) for row in rows])
    t['record_count'] += len(rows)


def urls(value):
    if isinstance(value, str):
        return [value] if value.startswith(('https://', 'http://')) else []
    if isinstance(value, list):
        return [u for x in value for u in urls(x)]
    if isinstance(value, dict):
        return urls(value.get('url_list', value.get('url', [])))
    return []


def assets(row, types):
    kind = str(row.get('type', ''))
    primary = row.get('downloads', [])
    result = []
    if '图' in kind or kind.lower() in {'image', 'images', 'photo'}:
        if 'image' in types:
            result.extend((u, 'image', '.jpeg') for u in urls(primary))
    elif '实况' in kind or '动图' in kind or kind.lower() == 'livephoto':
        if 'livephoto' in types:
            result.extend((u, 'livephoto', '.mp4') for u in urls(primary))
    elif 'video' in types:
        # Upstream normalized video data contains a single downloads URL.
        result.extend((u, 'video', '.mp4') for u in urls(primary)[:1])
    for k in ('static_cover', 'dynamic_cover'):
        if k in types:
            result.extend((u, k, '.jpeg' if k == 'static_cover' else '.webp') for u in urls(row.get(k))[:1])
    if 'livephoto' in types:
        result.extend((u, 'livephoto', '.mp4') for u in urls(row.get('video', row.get('videos', []))))
    return result


def safe_name(value):
    return re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', str(value)).strip(' .')[:65] or 'untitled'


def media_request(url, headers):
    p = urlparse(url)
    if p.scheme not in {'http', 'https'} or not p.hostname or p.username:
        raise ValueError('媒体下载地址无效')
    if p.hostname == 'localhost':
        raise ValueError('媒体地址不能指向本机')
    try:
        if not ipaddress.ip_address(p.hostname).is_global:
            raise ValueError('媒体地址不能指向本地网络')
    except ValueError as e:
        if '不能' in str(e):
            raise
    return urlopen(Request(url, headers=headers), timeout=40)


def transfer(url, dest, t, s):
    part = dest.with_suffix(dest.suffix + '.part')
    offset = part.stat().st_size if s['resume'] and part.exists() else 0
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.tiktok.com/' if t['platform'] == 'tiktok' else 'https://www.douyin.com/'}
    if offset:
        headers['Range'] = f'bytes={offset}-'
    try:
        response = media_request(url, headers)
    except HTTPError as e:
        if offset and e.code == 416:
            headers.pop('Range', None)
            offset = 0
            response = media_request(url, headers)
        else:
            raise
    with response as response:
        resumed = response.status == 206 and offset > 0
        if resumed and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
            raise ValueError('服务器返回的续传位置不匹配')
        expected = int(response.headers.get('Content-Length', 0))
        written = 0
        with part.open('ab' if resumed else 'wb') as f:
            while True:
                check_cancel(t)
                block = response.read(256 * 1024)
                if not block:
                    break
                f.write(block)
                written += len(block)
        if expected and written != expected:
            raise ValueError('文件传输不完整，保留断点以便重试')
        if not part.stat().st_size:
            raise ValueError('平台返回空文件')
    part.replace(dest)


def download_rows(rows, t, s):
    req = t['request']
    targets = []
    for row in rows:
        stamp = str(row.get('create_time', ''))[:10]
        earliest, latest = req.get('earliest') or s['earliest'], req.get('latest') or s['latest']
        if earliest or latest:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', stamp):
                raise ValueError('作品没有可识别的发布日期，无法应用日期筛选')
            if (earliest and stamp < earliest) or (latest and stamp > latest):
                continue
        for index, (u, kind, ext) in enumerate(assets(row, req['types'])):
            ident = row.get('id', row.get('aweme_id', hashlib.sha256(u.split('?')[0].encode()).hexdigest()[:16]))
            key = hashlib.sha256(f'{t["platform"]}:{ident}:{kind}:{index}'.encode()).hexdigest()
            filename = safe_name(f'{ident}_{kind}_{index+1}') + ext
            target = DOWNLOADS / filename
            targets.append((u, key, target))
    if not targets:
        raise ValueError('所选内容类型或日期范围内没有可下载文件，可调整筛选后重试')
    skipped = 0
    for n, (u, key, path) in enumerate(targets):
        check_cancel(t)
        with connect_db() as db:
            recorded = db.execute('SELECT path FROM files WHERE key=?', (key,)).fetchone()
        if path.exists() and (s['skip'] or (s['incremental'] and recorded)):
            skipped += 1
        else:
            save_task(t, message=f'正在保存文件 {n+1}/{len(targets)}', progress=15 + int(n / len(targets) * 80))
            transfer(u, path, t, s)
        with connect_db() as db:
            db.execute('INSERT OR REPLACE INTO files(key,path) VALUES(?,?)', (key, path.name))
        t['files'].append({'name': path.name, 'path': path.name})
        save_task(t)
    return f'保存 {len(targets)-skipped} 个文件，跳过 {skipped} 个已有文件'


def record_live(rows, t, s):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('未检测到 FFmpeg。请安装并加入 PATH 后重启本地服务')
    row = rows[0]
    candidates = urls(list((row.get('flv_pull_url') or {}).values())) + urls(list((row.get('hls_pull_url_map') or {}).values()))
    if not candidates:
        raise ValueError('未获取到直播拉流地址，直播可能尚未开播')
    path = DOWNLOADS / f'live_{t["id"]}.mkv'
    logpath = DATA / f'ffmpeg_{t["id"]}.log'
    seconds = t['request']['duration'] * 60
    command = [ffmpeg, '-nostdin', '-y', '-i', candidates[0], '-t', str(seconds), '-c', 'copy', str(path)]
    with logpath.open('wb') as log:
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        p = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags)
        PROCESSES[t['id']] = p
        start = time.monotonic()
        save_task(t, status='recording', message='FFmpeg 正在录制直播')
        try:
            while p.poll() is None:
                if CANCEL[t['id']].is_set():
                    p.terminate()
                    p.wait(timeout=10)
                    check_cancel(t)
                save_task(t, progress=min(98, int((time.monotonic()-start)/seconds*100)))
                time.sleep(1)
        finally:
            PROCESSES.pop(t['id'], None)
            if p.poll() is None:
                p.kill()
                p.wait()
        if p.returncode != 0:
            raise ValueError('FFmpeg 录制失败：' + logpath.read_text('utf-8', errors='replace')[-600:])
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError('FFmpeg 未生成有效录制文件')
    t['files'].append({'name': path.name, 'path': path.name})
    return '直播录制完成，文件已保存'


def run_task(t):
    # Serialize platform jobs to avoid burst requests. HTTP/UI stays concurrent.
    task_event = CANCEL.get(t['id'])
    with JOB_LOCK:
        try:
            check_cancel(t)
            save_task(t, status='running', message='正在连接平台并获取数据', progress=5)
            s = settings()
            req = t['request']
            if req.get('account_mode') == 'pool':
                collect_comment_pool(t, s)
                return
            rows, errors, messages = [], [], []
            for source in sources(req.get('links', ''), req['mode']):
                check_cancel(t)
                try:
                    fingerprint = hashlib.sha256(json.dumps({**req, 'links': source, 'rules': {k: s[k] for k in ('skip','resume','earliest','latest')}}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    prior = next((x for x in all_tasks() if x['id'] != t['id'] and fingerprint in x.get('fingerprints', []) and x['status'] == 'completed'), None)
                    if req['action'] == 'download' and req['mode'] == 'detail' and s['incremental'] and prior and prior['files'] and all((DOWNLOADS / f['path']).is_file() for f in prior['files']):
                        t['files'].extend(prior['files'])
                        messages.append('来源已完成，增量规则跳过')
                        continue
                    source_config = prepare_account(t, s)
                    try:
                        found = get_data(source, req, source_config)
                        if not found:
                            raise ValueError('平台返回空数据，请检查权限、Cookie 或查询条件')
                    except Exception as e:
                        account_request_result(req.get('account_id'), e)
                        raise
                    account_request_result(req.get('account_id'))
                    persist_records(t, found)
                    rows.extend(found)
                    if req['action'] == 'download':
                        messages.append(download_rows(found, t, s))
                    elif req['action'] == 'record':
                        messages.append(record_live(found, t, s))
                    else:
                        messages.append(f'已采集 {len(found)} 条数据')
                    t.setdefault('fingerprints', []).append(fingerprint)
                except InterruptedError:
                    raise
                except Exception as e:
                    errors.append(f'{source[:65]}：{e}')
            check_cancel(t)
            summary = '；'.join(messages + errors)
            save_task(t, status=('partial' if messages else 'failed') if errors else 'completed', progress=100, message=summary, result=rows[:5] if req['mode'] == 'live' else None)
        except InterruptedError as e:
            save_task(t, status='cancelled', message=str(e))
        except Exception as e:
            partial = t['request'].get('account_mode') == 'pool' and t['record_count'] > 0
            save_task(t, status='partial' if partial else 'failed', message=str(e))
        finally:
            if CANCEL.get(t['id']) is task_event:
                CANCEL.pop(t['id'], None)


def retry_comment_pool(id_):
    with LOCK:
        task = get_task(id_)
        if task['status'] not in {'failed', 'partial', 'cancelled'}:
            raise ValueError('此任务已在处理或已完成')
        save_task(task, status='queued', message='等待从已保存的评论游标继续')
        CANCEL[id_] = threading.Event()
        threading.Thread(target=run_task, args=(task,), daemon=True).start()
        return public_task(task)


def records(task='all', limit=None):
    query = 'SELECT payload FROM records'
    params = []
    if task != 'all':
        query += ' WHERE task_id=?'
        params.append(task)
    query += ' ORDER BY id DESC'
    if limit is not None:
        query += ' LIMIT ?'
        params.append(limit)
    with connect_db() as db:
        return [json.loads(r[0]) for r in db.execute(query, params)]


def export_data(fmt, task):
    rows = records(task)
    if not rows:
        raise ValueError('所选任务暂无数据，请先完成采集')
    return export_rows(fmt, rows)


def export_rows(fmt, rows):
    keys = list(dict.fromkeys(k for r in rows for k in r))
    def text(value):
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else '' if value is None else str(value)
    values = [[text(r.get(k)) for k in keys] for r in rows]
    if fmt == 'csv':
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        # Prevent spreadsheet formula interpretation of untrusted platform text.
        writer.writerows([["'"+v if v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row] for row in [keys, *values]])
        return output.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8'
    if fmt == 'xlsx':
        output = io.BytesIO()
        def col(n):
            s = ''
            while n:
                n, d = divmod(n-1, 26)
                s = chr(65+d) + s
            return s
        xml_rows = []
        for n, row in enumerate([keys, *values], 1):
            cells = ''.join(f'<c r="{col(i)}{n}" t="inlineStr"><is><t xml:space="preserve">{xml_escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", v[:32767]))}</t></is></c>' for i, v in enumerate(row, 1))
            xml_rows.append(f'<row r="{n}">{cells}</row>')
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
            z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            z.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="DouK 数据" sheetId="1" r:id="rId1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(xml_rows)+'</sheetData></worksheet>')
        return output.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    if fmt == 'sqlite':
        db = sqlite3.connect(':memory:')
        # Synthetic unique names avoid collisions in case-insensitive SQLite identifiers.
        columns = ','.join(f'"field_{i}" TEXT' for i in range(len(keys)))
        db.execute(f'CREATE TABLE records({columns})')
        db.execute('CREATE TABLE field_names(column_name TEXT, original_name TEXT)')
        db.executemany('INSERT INTO field_names VALUES(?,?)', [(f'field_{i}', k) for i, k in enumerate(keys)])
        db.executemany(f'INSERT INTO records VALUES({",".join("?" for _ in keys)})', values)
        db.commit()
        data = db.serialize()
        db.close()
        return data, 'application/vnd.sqlite3'
    raise ValueError('不支持的导出格式')


def operations_config(binding_id=None, owner_id=None):
    config = settings()
    with connect_db() as db:
        sql = "SELECT l.cookie FROM ops_account_logins l JOIN ops_users u ON u.id=l.owner_id LEFT JOIN ops_bindings b ON b.id=l.binding_id WHERE l.cookie<>'' AND u.enabled=1 AND (l.binding_id='' OR (b.owner_id=l.owner_id AND b.enabled=1))"
        params = []
        if binding_id:
            sql += ' AND l.binding_id=?'
            params.append(binding_id)
        if owner_id:
            sql += ' AND l.owner_id=?'
            params.append(owner_id)
        credential = db.execute(sql+' ORDER BY l.updated DESC,l.rowid DESC LIMIT 1',params).fetchone() if binding_id or owner_id else None
    if credential:
        config['cookie'] = credential['cookie']
        return config
    account_id = config.get('default_account_id')
    if account_id:
        account = get_account(account_id)
        if account.get('paused') or not account.get('cookie'):
            raise ValueError('默认采集账号未登录或已暂停，请在原有账号管理中检查')
        config['cookie'] = account['cookie']
    return config


OPS = Operations(lambda: connect_db(), lambda *args: upstream(*args), operations_config, LOGS.write, export_rows)
OPS.schema()
OPS_ACCOUNTS = EmployeeAccounts(OPS, lambda: DATA/'employee-browsers',
    lambda: any(s.state in {'opening','waiting','cancelling'} for s in LOGIN_SESSIONS.values()))
REPORT_ACCOUNTS = ReportAccounts(lambda: DATA/'report-browsers', lambda: connect_db())

_BACKUP_STORES = {}
def backup_store():
    key = str(DB.resolve())
    with LOCK:
        if key not in _BACKUP_STORES:
            _BACKUP_STORES[key] = BackupStore(DB)
        return _BACKUP_STORES[key]


class Handler(BaseHTTPRequestHandler):
    app_mode = 'operations'
    def send(self, data, status=200, mime='application/json; charset=utf-8', filename=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if getattr(self, 'response_cookie', None):
            self.send_header('Set-Cookie', self.response_cookie)
            self.response_cookie = None
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'DENY')
        if filename:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename))
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        entry_page = self.command == 'GET' and urlparse(self.path).path in {
            '/','/index.html','/admin','/admin/','/report','/report/',
            '/help','/help/','/help/index.html','/help/usage.html','/help/developer.html','/help/verification.html'}
        return trusted_request(self.headers,self.server.server_port,self.client_address[0],
                               self.app_mode=='tools',allow_document_navigation=entry_page)

    def authorize(self, path):
        if self.app_mode == 'tools':
            if path.startswith(('/api/ops/','/api/auth/','/api/reporting/')):
                raise AccessError('独立采集工具不提供员工运营接口',404)
            return None
        if path in {'/api/reporting/directory','/api/reporting/submit','/api/reporting/receipt','/api/reporting/note',
                    '/api/reporting/login/start','/api/reporting/login/status','/api/reporting/login/cancel'}:
            return None
        if not path.startswith('/api/') or path.startswith('/api/auth/'):
            return None
        if not OPS.initialized():
            raise AccessError('请先在服务器本机创建管理员')
        user = OPS.authenticate(self.headers)
        if not user:
            raise AccessError()
        OPS.admin(user)
        return user

    def do_GET(self):
        if not self.trusted():
            return self.send({'error': '仅允许本地工作台访问'}, 403)
        p = urlparse(self.path)
        q = parse_qs(p.query)
        try:
            actor = self.authorize(p.path)
            if p.path == '/api/reporting/directory':
                return self.send(OPS.public_directory())
            if p.path == '/api/health':
                return self.send({'app':'DouK Tools' if self.app_mode=='tools' else 'DouK Workspace','mode':self.app_mode})
            if p.path == '/api/ops/connection':
                OPS.admin(actor)
                return self.send({**public_settings(),'service':API_SERVICE.status()})
            if p.path == '/api/ops/network':
                OPS.admin(actor)
                return self.send(network_info(self.server.server_port))
            if p.path == '/api/ops/backups':
                OPS.admin(actor)
                return self.send({'items':backup_store().list(),'automatic':'daily','retention':14})
            if p.path == '/api/ops/backups/download':
                OPS.admin(actor)
                snapshot = backup_store().path(q.get('name',[''])[0])
                return self.send(snapshot.read_bytes(),mime='application/vnd.sqlite3',filename=snapshot.name)
            if p.path == '/api/ops/targets':
                return self.send(WorkTargets(OPS).list(actor,{k:v[0] for k,v in q.items()}))
            if p.path in {'/api/ops/logs','/api/ops/logs/export'}:
                OPS.admin(actor)
                return self.send_logs(q,p.path.endswith('/export'))
            if p.path == '/api/auth/status':
                return self.send(OPS.auth_status(self.headers))
            if p.path == '/api/ops/account-logins':
                return self.send(OPS_ACCOUNTS.list(actor))
            if p.path.startswith('/api/ops/'):
                result = OPS.get(p.path.removeprefix('/api/ops/'), actor, {k:v[0] for k,v in q.items()})
                if p.path in {'/api/ops/export','/api/ops/report-progress/export'}:
                    content, mime = result
                    return self.send(content, mime=mime, filename='DouK-operations.xlsx')
                return self.send(result)
            if p.path == '/api/status':
                files = [f for f in DOWNLOADS.iterdir() if f.is_file() and not f.name.endswith('.part')]
                with connect_db() as db:
                    count = db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
                return self.send({**CONNECTED, 'files': len(files), 'records': count, 'size': round(sum(f.stat().st_size for f in files)/1024/1024, 1), 'ffmpeg': bool(shutil.which('ffmpeg')), 'download_dir': str(DOWNLOADS)})
            if p.path in {'/api/logs', '/api/logs/export'}:
                return self.send_logs(q,p.path.endswith('/export'))
            if p.path == '/api/settings':
                return self.send(public_settings())
            if p.path == '/api/accounts':
                return self.send([public_account(a) for a in all_accounts()])
            if p.path == '/api/service':
                service = API_SERVICE.status()
                if service['ready'] and not CONNECTED['connected']:
                    test_connection()
                return self.send(service)
            if p.path == '/api/tasks':
                deleted = q.get('deleted', ['0'])[0] == '1'
                tasks = all_tasks(include_deleted=deleted)
                return self.send([public_task(t) for t in tasks if bool(t.get('deleted_at')) == deleted])
            if re.fullmatch(r'/api/tasks/[a-f0-9]{12}', p.path):
                return self.send(public_task(get_task(p.path.rsplit('/', 1)[-1]), True))
            if p.path == '/api/records':
                return self.send(records(q.get('task', ['all'])[0], min(1000, max(1, int(q.get('limit', [50])[0])))))
            if p.path == '/api/export':
                fmt = q.get('format', ['csv'])[0]
                data, mime = export_data(fmt, q.get('task', ['all'])[0])
                return self.send(data, mime=mime, filename='DouK-data.'+fmt)
            if p.path == '/api/file':
                path = (DOWNLOADS / q.get('path', [''])[0]).resolve()
                if path.parent != DOWNLOADS or not path.is_file() or path.name.endswith('.part'):
                    return self.send({'error': '文件不存在'}, 404)
                # Stream local media; avoid reading large videos into memory.
                self.send_response(200)
                self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                self.send_header('Content-Length', str(path.stat().st_size))
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''"+quote(path.name))
                self.end_headers()
                with path.open('rb') as f:
                    shutil.copyfileobj(f, self.wfile, 256 * 1024)
                return
            static = {'/': ('index.html', 'text/html; charset=utf-8'), '/index.html': ('index.html', 'text/html; charset=utf-8'), '/styles.css': ('styles.css', 'text/css; charset=utf-8'), '/accounts.css': ('accounts.css', 'text/css; charset=utf-8'), '/app.js': ('app.js', 'application/javascript; charset=utf-8'), '/account-ui.js': ('account-ui.js', 'application/javascript; charset=utf-8')}
            static.update({path:('docs/'+name,'text/html; charset=utf-8') for path,name in {
                '/help':'index.html','/help/':'index.html','/help/index.html':'index.html',
                '/help/usage.html':'usage.html','/help/developer.html':'developer.html',
                '/help/verification.html':'verification.html'}.items()})
            if self.app_mode != 'tools':
                static.update({'/report':('reports.html','text/html; charset=utf-8'),
                    '/report/':('reports.html','text/html; charset=utf-8'),
                    '/admin':('index.html','text/html; charset=utf-8'),
                    '/admin/':('index.html','text/html; charset=utf-8'),
                    '/reports.js':('reports.js','application/javascript; charset=utf-8'),
                    '/reports.css':('reports.css','text/css; charset=utf-8'),
                    '/reporting-admin.js':('reporting-admin.js','application/javascript; charset=utf-8'),
                    '/reporting-admin.css':('reporting-admin.css','text/css; charset=utf-8')})
            static['/ops-ui.js'] = ('ops-ui.js', 'application/javascript; charset=utf-8')
            static['/ops-account-ui.js'] = ('ops-account-ui.js', 'application/javascript; charset=utf-8')
            static['/ops-flow-ui.js'] = ('ops-flow-ui.js', 'application/javascript; charset=utf-8')
            static['/tools-shell.js'] = ('tools-shell.js', 'application/javascript; charset=utf-8')
            static['/tools.css'] = ('tools.css', 'text/css; charset=utf-8')
            static['/ops.css'] = ('ops.css', 'text/css; charset=utf-8')
            static['/overview.css'] = ('overview.css', 'text/css; charset=utf-8')
            static['/log-ui.js'] = ('log-ui.js', 'application/javascript; charset=utf-8')
            static['/service-ui.js'] = ('service-ui.js', 'application/javascript; charset=utf-8')
            if p.path in static:
                name, mime = static[p.path]
                if self.app_mode=='tools' and name=='index.html':
                    name='tools.html'
                return self.send((ROOT / name).read_bytes(), mime=mime)
            self.send({'error': '资源不存在'}, 404)
        except AccessError as e:
            self.send({'error': str(e)}, e.status)
        except (ValueError, TypeError) as e:
            LOGS.write('WARN', 'system', '请求失败', str(e))
            self.send({'error': str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOGS.write('ERROR', 'system', '本地服务异常', '处理请求时发生内部错误，请检查本地服务输出')
            self.send({'error': '本地服务错误，请检查终端输出'}, 500)

    def do_POST(self):
        if not self.trusted():
            # Consume a bounded request body before closing; Windows can reset a
            # socket with unread bytes before the client receives the 403 body.
            try:
                length=int(self.headers.get('Content-Length',0))
                if 0<length<=1024*1024:
                    self.connection.settimeout(2)
                    self.rfile.read(length)
            except (ValueError,OSError):
                pass
            return self.send({'error': '仅允许本地工作台访问'}, 403)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > 1024 * 1024 or length < 0:
                return self.send({'error': '请求过大'}, 413)
            data = json.loads(self.rfile.read(length) or '{}')
            if not isinstance(data, dict):
                raise ValueError('请求必须为 JSON 对象')
            path = urlparse(self.path).path
            actor = self.authorize(path)
            if path == '/api/reporting/submit':
                identity = REPORT_ACCOUNTS.proofs(data)
                if not identity:
                    raise ValueError('请先完成至少一个抖音账号核验；核验过期或已移除时，请重新添加后报送')
                return self.send(OPS.receive_report(data,self.client_address[0],identity),202)
            if path == '/api/reporting/receipt':
                return self.send(OPS.report_receipt(data))
            if path == '/api/reporting/note':
                return self.send(OPS.report_note(data))
            if path in {'/api/reporting/login/start','/api/reporting/login/status','/api/reporting/login/cancel'}:
                action=path.rsplit('/',1)[-1]
                return self.send(REPORT_ACCOUNTS.start(self.client_address[0]) if action=='start' else getattr(REPORT_ACCOUNTS,action)(data))
            if path in {'/api/auth/setup', '/api/auth/login'}:
                if path.endswith('/setup') and not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    raise AccessError('请在服务器本机创建首个管理员',403)
                OPS.schema()
                result, self.response_cookie = OPS.login(data, setup=path.endswith('/setup'))
                return self.send(result)
            if path == '/api/auth/logout':
                result, self.response_cookie = OPS.logout(self.headers)
                return self.send(result)
            if path == '/api/ops/connection':
                OPS.admin(actor)
                updates={k:v for k,v in data.items() if k in {'api_url','token','cookie'}}
                if updates.get('cookie'):
                    updates['default_account_id']=''
                result=save_settings(updates)
                LOGS.write('INFO','system','更新运营采集连接',title=actor['name'])
                return self.send(result)
            if path == '/api/ops/connection/test':
                OPS.admin(actor)
                return self.send(ensure_connection())
            if path == '/api/ops/backups':
                OPS.admin(actor)
                item = backup_store().create('manual')
                LOGS.write('INFO','system','创建数据库备份',title=actor['name'])
                return self.send({'item':item,'message':'SQLite 完整备份已创建'})
            if path in {'/api/ops/targets','/api/ops/targets/exempt'}:
                targets=WorkTargets(OPS)
                return self.send(targets.exempt(data,actor) if path.endswith('/exempt') else targets.save(data,actor))
            if path in {'/api/ops/account-logins/start','/api/ops/account-logins/complete','/api/ops/account-logins/cancel'}:
                action = path.rsplit('/',1)[-1]
                return self.send(getattr(OPS_ACCOUNTS,action)(data,actor))
            if path.startswith('/api/ops/'):
                return self.send(OPS.post(path.removeprefix('/api/ops/'), data, actor))
            if path == '/api/logs/clear':
                LOGS.clear()
                return self.send({'message': '日志已清空，采集数据和任务不受影响'})
            if path == '/api/settings':
                result = save_settings(data)
                LOGS.write('INFO', 'system', '保存设置')
                return self.send(result)
            if path == '/api/accounts':
                result = create_account(data)
                LOGS.write('INFO', 'account', '添加账号', title=result.get('name', ''))
                return self.send(result, 201)
            account_match = re.fullmatch(r'/api/accounts/([a-f0-9]{12})(?:/(login|cancel-login))?', path)
            if account_match:
                id_, action = account_match.groups()
                if action == 'login':
                    return self.send(start_login(id_))
                if action == 'cancel-login':
                    return self.send(login_session(id_).cancel())
                result = update_account(id_, data)
                LOGS.write('INFO', 'account', '更新账号', task_id=id_, title=result.get('name', ''))
                return self.send(result)
            if path == '/api/connect':
                return self.send(test_connection())
            if path == '/api/service/start':
                return self.send(API_SERVICE.start())
            if path == '/api/ensure-service':
                return self.send(ensure_connection())
            if path == '/api/tasks':
                if not ensure_connection()['connected']:
                    return self.send({'error': '后台采集服务尚未就绪。请先在「连接与设置」启动采集服务，再创建任务。'}, 503)
                return self.send(create_task(data), 201)
            m = re.fullmatch(r'/api/tasks/([a-f0-9]{12})/(cancel|retry|delete|restore)', path)
            if m:
                id_, action = m.groups()
                t = get_task(id_)
                if action == 'delete':
                    return self.send(delete_task(id_))
                if action == 'restore':
                    return self.send(restore_task(id_))
                if t.get('deleted_at'):
                    raise ValueError('此任务已删除，请先恢复后再操作')
                if action == 'retry':
                    if t['status'] not in {'failed', 'cancelled', 'partial'}:
                        raise ValueError('仅失败、取消或部分完成的任务可以重试')
                    if not ensure_connection()['connected']:
                        return self.send({'error': '采集服务尚未就绪，请先在「连接与设置」启动采集服务'}, 503)
                    if t['request'].get('account_mode') == 'pool':
                        return self.send(retry_comment_pool(id_), 201)
                    return self.send(create_task(t['request']), 201)
                if id_ in CANCEL:
                    CANCEL[id_].set()
                return self.send({'message': '已请求取消'})
            self.send({'error': '接口不存在'}, 404)
        except AccessError as e:
            self.send({'error': str(e)}, e.status)
        except (ValueError, TypeError, KeyError) as e:
            LOGS.write('WARN', 'system', '请求失败', str(e))
            self.send({'error': str(e)}, 400)
        except Exception:
            LOGS.write('ERROR', 'system', '本地服务异常', '处理请求时发生内部错误，请检查本地服务输出')
            self.send({'error': '本地服务错误，请检查终端输出'}, 500)

    def send_logs(self,query,export=False):
        result=LOGS.query(query,export=export)
        if not export:
            return self.send(result)
        output=io.StringIO(newline='')
        writer=csv.writer(output)
        writer.writerow(['时间','级别','类型','事件','任务ID','任务名称','状态','详情'])
        for row in result['items']:
            cells=[row[k] for k in ('created_at','level','category','event','task_id','title','status','message')]
            writer.writerow(["'"+v if str(v).startswith(('=','+','-','@')) else v for v in cells])
        return self.send(output.getvalue().encode('utf-8-sig'),mime='text/csv; charset=utf-8',filename='DouK-logs.csv')

    def log_message(self, fmt, *args):
        if '/api/status' not in self.path and '/api/tasks' not in self.path:
            clean = tuple(re.sub(r'([?&](?:receipt_token|token|cookie)=)[^&\s\"]+',r'\1[REDACTED]',str(arg),flags=re.I) for arg in args)
            super().log_message(fmt, *clean)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8877)
    args = parser.parse_args()
    OPS.ensure_initial_admin()
    LOGS.write('INFO', 'system', '工作台服务启动', '日志保存在本地 SQLite 数据库')
    OPS.start()
    backup_store().due()
    backup_stop = threading.Event()
    def scheduled_backups():
        while not backup_stop.wait(900):
            try:
                backup_store().due()
            except Exception:
                LOGS.write('ERROR','system','自动数据库备份失败','请检查备份目录空间与写入权限')
    threading.Thread(target=scheduled_backups,daemon=True).start()
    migrate_legacy_account()
    # Interrupted jobs are explicit failures; never resume network actions silently.
    for t in all_tasks():
        if t['status'] in {'queued', 'running', 'recording'}:
            save_task(t, status='failed', message='本地服务曾中断，可点击重试继续；已下载部分保留')
    def restore_upstream():
        if settings()['api_url'] in {'http://127.0.0.1:5555', 'http://localhost:5555'} and API_SERVICE.dependencies_ready():
            try:
                API_SERVICE.ensure_ready()
            except Exception:
                pass
        test_connection()
    threading.Thread(target=restore_upstream, daemon=True).start()
    ThreadingHTTPServer.request_queue_size = 128
    server = ThreadingHTTPServer((bind_host(), args.port), Handler)
    print(f'DouK Workspace: http://127.0.0.1:{args.port}/admin', flush=True)
    for url in network_info(args.port)['employee_urls']:
        print(f'Employee reporting: {url}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        backup_stop.set()
        OPS.scheduler_stop.set()
        for session in LOGIN_SESSIONS.values():
            session.cancel()
        for row in list(REPORT_ACCOUNTS.sessions.values()):
            row['session'].cancel()
        for event in CANCEL.values():
            event.set()
        for p in PROCESSES.values():
            if p.poll() is None:
                p.terminate()
        server.server_close()


if __name__ == '__main__':
    main()
