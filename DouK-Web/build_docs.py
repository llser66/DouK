"""Build self-contained offline HTML guides using only the standard library."""
from pathlib import Path
import html
import re

ROOT = Path(__file__).resolve().parent / 'docs'
PAGES = {'index':'交付文档','usage':'操作说明','developer':'开发与部署','verification':'版本验证'}
STYLE = '''*{box-sizing:border-box}body{margin:0;background:#f5f8f6;color:#273d36;font:16px/1.8 "Microsoft YaHei",sans-serif}header{background:#fff;border-bottom:1px solid #dce8e1;padding:20px 6vw}header b{font-size:26px;color:#19876b}nav{display:flex;gap:20px;flex-wrap:wrap}a{color:#16795e}main{max-width:1040px;margin:32px auto;background:white;padding:36px 48px;border:1px solid #e1ebe5;border-radius:16px}h1{font-size:30px}h2{margin-top:42px;padding-top:12px;border-top:1px solid #e3ece6;font-size:23px}h3{margin-top:28px}pre{background:#edf4ef;border-radius:10px;padding:18px;overflow:auto;white-space:pre-wrap;word-break:break-word}code{font:14px/1.7 Consolas,monospace}li{margin:9px 0}p{overflow-wrap:anywhere}.meta{color:#6d8176;font-size:13px}footer{max-width:1040px;margin:24px auto;color:#6d8176;text-align:center}@media(max-width:650px){main{margin:12px;padding:22px}h1{font-size:25px}}@media print{body{background:white}header{display:none}main{border:0;margin:0;padding:0;max-width:none}h2{break-after:avoid}pre{break-inside:avoid}a{color:inherit}}'''


def inline(text):
    text=html.escape(text)
    text=re.sub(r'`([^`]+)`',r'<code>\1</code>',text)
    text=re.sub(r'\*\*([^*]+)\*\*',r'<strong>\1</strong>',text)
    return re.sub(r'\[([^\]]+)\]\(([^)]+)\)',r'<a href="\2">\1</a>',text)


def render(text):
    output=[]; paragraph=[]; listing=None; code=None
    def flush():
        if paragraph:
            output.append('<p>'+inline(' '.join(paragraph))+'</p>');paragraph.clear()
    def close_list():
        nonlocal listing
        if listing: output.append('</'+listing+'>');listing=None
    for line in text.splitlines():
        if line.startswith('```'):
            flush();close_list()
            if code is None: code=[]
            else: output.append('<pre><code>'+html.escape('\n'.join(code))+'</code></pre>');code=None
            continue
        if code is not None: code.append(line);continue
        if not line.strip(): flush();close_list();continue
        if match:=re.match(r'^(#{1,3}) (.*)',line):
            flush();close_list();level=len(match[1]);output.append(f'<h{level}>{inline(match[2])}</h{level}>');continue
        if match:=re.match(r'^(?:([-]) |(\d+)\. )(.*)',line):
            flush();kind='ul' if match[1] else 'ol'
            if listing!=kind: close_list();listing=kind;output.append('<'+kind+'>')
            output.append('<li>'+inline(match[3])+'</li>');continue
        close_list();paragraph.append(line)
    flush();close_list()
    if code is not None: raise ValueError('Unclosed code block')
    return '\n'.join(output)


def main():
    for name,title in PAGES.items():
        links=''.join(f'<a href="{key}.html">{html.escape(label)}</a>' for key,label in PAGES.items())
        document='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><script>if(location.protocol!=="file:"&&location.pathname==="/help"){const b=document.createElement("base");b.href="/help/";document.head.appendChild(b)}</script><title>DouK · '+title+'</title><style>'+STYLE+'</style></head><body><header><b>DouK.</b><p class="meta">2026.10.31 · 每日报送目标与强制账号核验版 · 可离线阅读</p><nav>'+links+'</nav></header><main>'+render((ROOT/(name+'.md')).read_text(encoding='utf-8'))+'</main><footer>DouK · 操作与开发文档 · 浏览器打印可另存 PDF</footer></body></html>'
        (ROOT/(name+'.html')).write_text(document,encoding='utf-8')
    print('Built 4 offline HTML guides')


if __name__=='__main__':
    main()
