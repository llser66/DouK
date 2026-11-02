"""Create a clean source distribution; never package a live data directory."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
WEB_SUFFIXES = {'.py','.js','.css','.html','.md','.ps1','.cmd','.bat','.txt','.toml'}
EXCLUDED = {'__pycache__','.git','.venv-api','node_modules','Volume','data','Downloads','Backups'}
FORBIDDEN_SUFFIXES = {'.sqlite','.db','.log','.pyc','.part'}


def selected():
    files=[(p,Path('DouK-Web')/p.name) for p in ROOT.iterdir()
           if p.is_file() and (p.suffix.lower() in WEB_SUFFIXES or p.name=='.gitignore')]
    for directory in ('tests','docs'):
        for p in (ROOT/directory).rglob('*'):
            if p.is_file() and not EXCLUDED.intersection(p.relative_to(ROOT).parts) and p.suffix.lower() in {'.py','.md','.html'}:
                files.append((p,Path('DouK-Web')/p.relative_to(ROOT)))
    upstream=ROOT.parent/'TikTokDownloader'
    for name in ('main.py','setup.py','encipher_example.py','requirements.txt','pyproject.toml','uv.lock','license','README.md','README_EN.md','.gitignore','.python-version','Dockerfile'):
        p=upstream/name
        if p.is_file(): files.append((p,Path('TikTokDownloader')/name))
    for directory in ('src','static','locale','docs','.github'):
        for p in (upstream/directory).rglob('*'):
            if p.is_file() and not EXCLUDED.intersection(p.relative_to(upstream).parts) and p.suffix.lower() not in FORBIDDEN_SUFFIXES:
                files.append((p,Path('TikTokDownloader')/p.relative_to(upstream)))
    return files


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT.parent/'Releases')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    label='DouK-2026.11.02'
    stamp=datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    archive_path=args.output/(label+'-'+stamp+'.zip')
    if archive_path.exists(): raise SystemExit('Refusing to overwrite an existing release')
    with tempfile.TemporaryDirectory(prefix='DouK-clean-release-') as temporary:
        root=Path(temporary)/label;root.mkdir()
        for source,relative in selected():
            target=root/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        license_source=ROOT.parent/'LICENSE'
        if not license_source.is_file():license_source=ROOT.parent/'TikTokDownloader'/'license'
        if license_source.is_file():shutil.copy2(license_source,root/'LICENSE')
        (root/'开始使用.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>DouK 使用文档</title><meta http-equiv="refresh" content="0;url=DouK-Web/docs/index.html"><a href="DouK-Web/docs/index.html">打开操作与部署说明</a></html>',encoding='utf-8')
        (root/'打开老板工作台.cmd').write_bytes(b'@echo off\r\npowershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0DouK-Web\\open-workspace.ps1"\r\n')
        (root/'创建老板桌面入口.cmd').write_bytes(b'@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0DouK-Web\\setup-shortcut.ps1"\r\nif errorlevel 1 pause\r\n')
        (root/'分发说明.txt').write_text('DouK 2026.11.02 报送进度与老板交付版\n\n首次请开发者协助安装：先打开“开始使用.html”，阅读 DouK-Web/老板交付说明.md；安装环境成功后运行“创建老板桌面入口.cmd”。以后双击桌面“DouK 老板工作台”即可启动后台并打开网页，项目目录不要移动。登录启动和局域网由部署人员另行配置。安装包含源码和文档，不含 Python/Node/Chrome、个人业务库、Cookie、现有管理员数据或运行配置；这不是免安装包，首次需要联网安装依赖。\n\nTikTokDownloader 来源：https://github.com/JoeanAmier/TikTokDownloader\n其源码适用 GNU GPLv3；原 license、源文件及版权信息随包保留。本网站适配与新增代码以源码形式提供。再次分发上游及其修改版时，须保留许可、版权、免责声明，标明修改并按 GPLv3 提供相应完整源码和构建安装资料，保留接收方复制、修改、再分发权利。代码组合的适用范围应按实际方案评估，详见 DouK-Web/THIRD_PARTY_NOTICES.md 及开发文档。\n\n上游加密参数算法已不再维护，原 Web UI 尚未重新开放。本站界面独立开发，通过 API 使用已有采集能力；平台更新可能导致采集失败，Cookie 不能保证解决。仅使用获授权的账号和数据。\n\n文件校验见 SHA256SUMS.json，校验不包含清单自身。\n',encoding='utf-8')
        records=[]
        for path in sorted(p for p in root.rglob('*') if p.is_file()):
            relative=path.relative_to(root)
            if EXCLUDED.intersection(relative.parts) or path.suffix.lower() in FORBIDDEN_SUFFIXES:
                raise SystemExit('Private/runtime file rejected: '+str(relative))
            body=path.read_bytes()
            records.append({'path':relative.as_posix(),'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()})
        (root/'SHA256SUMS.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
        with zipfile.ZipFile(archive_path,'x',zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(p for p in root.rglob('*') if p.is_file()):
                archive.write(path,path.relative_to(root.parent).as_posix())
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        manifest=json.loads(archive.read(label+'/SHA256SUMS.json'))
        for item in manifest:
            body=archive.read(label+'/'+item['path'])
            assert len(body)==item['bytes'] and hashlib.sha256(body).hexdigest()==item['sha256']
    digest=hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_path.with_suffix('.zip.sha256').write_text(digest+'  '+archive_path.name+'\n',encoding='ascii')
    print(json.dumps({'path':str(archive_path),'files':len(manifest)+1,'bytes':archive_path.stat().st_size,'sha256':digest},ensure_ascii=False))


if __name__=='__main__':
    main()
