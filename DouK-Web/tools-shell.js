/* Standalone local tools have no website account or employee session. */
const ops={ready:true,user:{role:'admin',name:'本地工具'}};
const bossPages=[],employeePages=[],opsTitles={};
function opsPages(){return [...pages,['settings','settings','连接与设置']]}
function isOpsPage(){return false}
function opsNavigation(){return pages.map(([id,i,label])=>`<button class="nav-item ${state.page===id?'active':''}" data-page="${id}">${icon(i)}${label}</button>`).join('')}
function opsProfile(){
  document.querySelector('.profile').innerHTML='<span class="avatar">D</span><span><b>独立采集工具</b><small>无需网站登录 · 本机使用</small></span><span class="online-dot"></span>';
  document.querySelector('.workspace b').textContent='采集工具';
  document.querySelector('.workspace small').textContent='独立工作区';
}
function renderOpsAuth(){document.querySelector('#content').textContent='本地工具暂时不可用，请通过“启动采集工具”重新打开。'}
async function bootstrapOps(){await refresh();render()}
function opsBeforeNavigate(){}
function opsBody(){return ''}
function loadOps(){}
document.addEventListener('DOMContentLoaded',()=>{
  side=function(){const ready=state.status.connected;return `<aside class="right-column"><section class="panel"><div class="panel-header"><span class="panel-title">${icon('link')}采集准备</span><span class="status-chip ${ready?'ok':''}">${ready?'服务已连接':'待连接'}</span></div><div class="panel-body"><div class="ops-check-row"><span>可用抖音账号</span><b>${state.accounts.filter(a=>a.has_cookie&&!a.paused).length} 个</b></div><p class="muted">首次使用先登录抖音账号，再粘贴链接创建任务。结果在任务中心查看，数据可导出 CSV、Excel 或 SQLite。</p><div class="result-actions"><button class="secondary" data-page="accounts">管理账号</button><button class="text-button" data-page="settings">连接设置 ↗</button></div></div></section></aside>`};
  render();
});
