"""Automatic local lifecycle for the upstream API."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.request import ProxyHandler, build_opener


class APIService:
    def __init__(self, root):
        self.root = Path(root)
        self.upstream = self.root.parent / 'TikTokDownloader'
        self.python = self.root / '.venv-api/Scripts/python.exe'
        self.process = None
        self.lock = threading.RLock()
        self.dependency_check = None

    def ready(self):
        try:
            with build_opener(ProxyHandler({})).open('http://127.0.0.1:5555/openapi.json', timeout=1) as response:
                return json.load(response).get('info', {}).get('title') == 'DouK-Downloader'
        except (OSError, ValueError):
            return False

    def dependencies_ready(self):
        if not self.python.is_file():
            return False
        if self.dependency_check is None:
            modules = ['fastapi','aiofiles','aiosqlite','curl_cffi','emoji','javascript','lxml','openpyxl','pydantic','pyperclip','rich','uvicorn']
            code = 'import sys,importlib.util;sys.exit(0 if all(importlib.util.find_spec(m) for m in '+repr(modules)+') else 1)'
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            try:
                result = subprocess.run([str(self.python), '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, creationflags=flags)
                self.dependency_check = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                self.dependency_check = False
        return self.dependency_check

    def status(self):
        ready = self.ready()
        deps = self.dependencies_ready()
        if ready:
            state, message = 'ready', '后台采集服务已就绪'
        elif self.process and self.process.poll() is None:
            state, message = 'starting', '正在启动后台采集服务…'
        elif self.process and self.process.poll() not in {None, 0}:
            state, message = 'failed', '采集服务启动失败，请检查 data/api-service.log'
        elif not deps:
            state, message = 'missing_dependencies', '采集依赖尚未安装，请运行 start-enhanced-api.ps1 完成安装'
        else:
            state, message = 'stopped', '后台采集服务尚未启动'
        return {'state': state, 'message': message, 'ready': ready, 'dependencies_ready': deps, 'url': 'http://127.0.0.1:5555'}

    def start(self):
        with self.lock:
            status = self.status()
            if status['ready'] or status['state'] == 'starting':
                return status
            if not status['dependencies_ready']:
                raise ValueError(status['message'])
            command = [str(self.python), '-u', str(self.root / 'enhanced_api.py'), '--web-start']
            env = os.environ.copy()
            node = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin'
            env['PATH'] = str(node)+os.pathsep+env.get('PATH', '')
            env['PYTHONIOENCODING'] = 'utf-8'
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            (self.root / 'data').mkdir(exist_ok=True)
            with (self.root / 'data/api-service.log').open('ab') as log:
                self.process = subprocess.Popen(command, cwd=str(self.upstream), env=env,
                                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags)
            return {'state': 'starting', 'message': '正在启动后台采集服务…', 'ready': False}

    def ensure_ready(self, timeout=45):
        if self.ready():
            return
        self.start()
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if self.ready():
                return
            if self.process and self.process.poll() is not None:
                raise ValueError('后台采集服务启动失败，请查看 data/api-service.log')
            time.sleep(.25)
        raise ValueError('后台采集服务启动超时，请稍后重试')
