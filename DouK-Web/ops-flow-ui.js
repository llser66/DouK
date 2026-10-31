/* Operational workflows; collection tools are a separate local application. */
employeePages.splice(2,0,['myaccounts','users','我的抖音账号']);
opsTitles.myaccounts=['管理我的抖音账号。','登录多个抖音账号、查看管理员分配与巡检进度。'];
ops.filtersByPage={};ops.settingsTab='rules';ops.submitDraft='';
const opsLogs={filters:{},page:1,result:null};
function opsNavigation(){const button=([id,i,name])=>`<button class="nav-item ${state.page===id?'active':''}" data-page="${id}">${icon(i)}${name}</button>`;if(ops.user?.role!=='admin')return opsPages().map(button).join('');return [['数据与作品',['overview','statistics','works']],['报送与核查',['unreported','audit']],['管理设置',['people','bindings','opssettings']]].map(([title,ids])=>`<div class="ops-nav-section">${title}</div>`+ids.map(id=>bossPages.find(p=>p[0]===id)).filter(Boolean).map(button).join('')).join('')}
function opsProfile(){
  const user=ops.user;
  document.querySelector('.profile').innerHTML=`<span class="avatar">${esc(user?.name?.slice(0,1)||'D')}</span><span><b>${esc(user?.name||'运营工作空间')}</b><small>${user?(user.role==='admin'?'管理员':'员工'):'请登录'}</small></span>${user?'<button class="text-button" id="ops-logout">退出</button>':''}`;
  document.querySelector('#ops-logout')?.addEventListener('click',async()=>{await post('/auth/logout',{});location.reload()});
  document.querySelectorAll('.sidebar-bottom [data-page],.sidebar-bottom a').forEach(el=>el.hidden=true);
  document.querySelector('.brand').href=user?.role==='employee'?'#mine':'#overview';
  document.querySelector('.workspace b').textContent=user?.role==='employee'?'我的运营工作台':'运营管理工作空间';
  document.querySelector('.workspace small').textContent='账号、作品与提交核查';
}
async function bootstrapOps(){
  try{const data=await api('/auth/status');ops.initialized=data.initialized;ops.user=data.user;ops.ready=true;
    if(ops.user&&!opsPages().some(p=>p[0]===state.page)){state.page=opsPages()[0][0];history.replaceState(null,'','#'+state.page)}
    render();
  }catch{ops.ready=true;document.querySelector('#content').textContent='本地服务暂时不可用，请通过启动脚本打开运营工作空间。'}
}
function opsBeforeNavigate(page){
  if(state.page===page)return;
  ops.filtersByPage[state.page]={...ops.filters};
  ops.filters={...(ops.filtersByPage[page]||{})};ops.page=1;
  const form=document.querySelector('#ops-submit-form');
  if(form)ops.submitDraft=form.elements.link.value;
}
function opsOpen(page,filters){ops.filtersByPage[page]={...filters};if(state.page===page){ops.filters={...filters};ops.page=1;loadOps()}else navigate(page)}
function opsQuickSubmit(id){ops.submitDraft='https://www.douyin.com/video/'+id;navigate('submit')}
function opsScopeFilter(){
  const p=ops.data.people;
  return `<section class="panel"><div class="panel-body ops-filter-panel"><form id="ops-filter" class="ops-filters"><div class="field"><label for="scope-start">发布起始时间</label><input type="datetime-local" step="60" title="北京时间，精确到分钟" id="scope-start" name="start" value="${esc(opsFilterTime(ops.filters.start))}"></div><div class="field"><label for="scope-end">发布截止时间</label><input type="datetime-local" step="60" title="北京时间，包含截止分钟" id="scope-end" name="end" value="${esc(opsFilterTime(ops.filters.end,true))}"></div>${p?`<div class="field"><label for="scope-employee">员工</label><select id="scope-employee" name="employee">${opsOpts(p.users,'id','name',ops.filters.employee)}</select></div><div class="field"><label for="scope-group">小组</label><select id="scope-group" name="group">${opsOpts(p.groups,'id','name',ops.filters.group)}</select></div>`:''}<div class="field"><label for="scope-binding">抖音账号</label><select id="scope-binding" name="binding">${opsOpts(ops.data.bindings,'id','nickname',ops.filters.binding)}</select></div><div class="ops-filter-actions"><button class="primary" type="submit">筛选统计</button><button class="secondary" type="button" id="ops-filter-reset">重置</button></div></form></div></section>`;
}
function opsActions(){
  const s=ops.data.summary,admin=ops.user.role==='admin';
  return `<div class="ops-action-grid">${[['待提交',s.pending,'pending'],['逾期未提交',s.overdue,'overdue'],['待确认 / 采集异常',s.confirm+s.error,'attention'],['已提交',s.submitted,'submitted']].map(([name,n,status])=>`<button class="ops-action-card ${status==='overdue'&&n?'needs-attention':''}" data-ops-state="${status}"><span>${name}</span><b>${n}</b><small>${admin?'查看并处理':'查看我的作品'} ↗</small></button>`).join('')}</div>`;
}
function opsSetup(){
  const p=ops.data.people;
  if(ops.data.bindings.length&&p.users.some(u=>u.role==='employee'))return '';
  return `<section class="panel"><div class="panel-header"><span class="panel-title">${icon('check')}开始建立运营分工</span></div><div class="panel-body ops-start-steps"><button class="secondary" data-page="people">1. 创建员工与小组</button><button class="secondary" data-page="bindings">2. 分配抖音账号</button><button class="secondary" data-ops-settings-tab="connection">3. 检查采集连接</button><p class="subtle">员工也能登录自己的抖音绑定账号。先完成一次发布巡检，再核对作品提交情况。</p></div></section>`;
}
function opsTodo(){
  const items=ops.data.todo.items.filter(w=>!['submitted','exempt'].includes(w.state)).sort((a,b)=>(a.state==='overdue'?-1:0)-(b.state==='overdue'?-1:0)).slice(0,5);
  return `<section class="panel"><div class="panel-header"><span class="panel-title">${icon('tasks')}需要我跟进</span><button class="text-button" data-page="history">全部作品 ↗</button></div><div class="panel-body">${items.map(w=>`<div class="ops-check-row"><div><b>${esc(w.description||w.id)}</b><small>${esc(w.nickname)} · ${esc(w.state_label)} · 截止 ${opsDate(w.deadline)}</small>${w.explanation?'<small>已补充说明，等待管理员处理</small>':''}</div><div class="ops-row-actions"><button class="primary" data-ops-quick-submit="${w.id}">提交这件作品</button><button class="text-button" data-ops-work-view="${w.id}">查看 / 说明</button></div></div>`).join('')||`<div class="empty-state">${ops.data.bindings.length?'当前没有待跟进作品。可在我的账号检查新发布作品。':'先绑定一个抖音账号，再提交作品或检查发布情况。'}</div>`}<div class="result-actions"><button class="secondary" data-page="submit">提交新作品</button><button class="text-button" data-page="myaccounts">管理我的抖音账号 ↗</button></div></div></section>`;
}
function opsWorkTimes(w){
  return `<div class="ops-work-times"><div class="ops-work-time-row"><span>发布时间</span><time>${esc(opsDate(w.published))}</time></div><div class="ops-work-time-row ops-work-time-submitted"><span>提交时间</span>${w.submitted_at?`<time>${esc(opsDate(w.submitted_at))}</time>`:'<span class="ops-work-time-empty">未提交</span>'}</div>${w.submitted_at&&w.late?'<span class="ops-work-time-late">逾期提交</span>':''}</div>`;
}
function opsWorkList(){
  const result=ops.data.works,admin=ops.user.role==='admin';
  return `<section class="panel"><div class="panel-header"><span class="panel-title">${icon('video')}${ops.filters.deleted==='1'?'已删除作品':state.page==='missing'?'作品核查':'作品记录'} <span class="subtle">${result.total} 件</span></span><button class="text-button" id="ops-export">${icon('export')}导出 Excel</button></div><div class="ops-table-wrap"><table class="ops-table ops-work-table"><thead><tr><th class="ops-work-main-heading">作品</th><th class="ops-work-owner-heading">账号 / 责任人</th><th class="ops-work-time-heading">发布 / 提交时间</th>${Object.values(metricNames).map(n=>`<th class="ops-work-metric">${n}</th>`).join('')}<th class="ops-work-state-heading">核查状态</th><th class="ops-work-actions-heading">操作</th></tr></thead><tbody>${result.items.map(w=>`<tr><td><div class="ops-work-title">${safeOpsUrl(w.cover)?`<img src="${safeOpsUrl(w.cover)}" alt="作品封面" loading="lazy" referrerpolicy="no-referrer">`:`<span class="ops-cover">${icon('video')}</span>`}<div><b>${esc(w.description||'暂无文案')}</b><a href="${safeOpsUrl(w.share_url)}" target="_blank" rel="noreferrer">${esc(w.id)} ↗</a></div></div></td><td class="ops-work-owner"><b>${esc(w.nickname||'暂无昵称')}</b><small>${esc(w.employee_name)} · ${esc(w.group_name||'未分组')}</small></td><td>${opsWorkTimes(w)}</td>${Object.keys(metricNames).map(key=>`<td class="ops-work-metric">${opsMetric(w.metrics[key])}</td>`).join('')}<td class="ops-work-state"><span class="status-chip ${['submitted','exempt'].includes(w.state)?'ok':''}">${w.deleted_at?'已删除':esc(w.state_label)}</span>${!w.submitted_at?`<small>${w.state==='exempt'?esc(w.review_note):'截止 '+opsDate(w.deadline)}</small>`:''}${w.explanation?'<small>员工已补充说明</small>':''}${w.last_error?'<small class="ops-form-error">数据刷新异常</small>':''}</td><td><div class="ops-work-actions"><button class="text-button" data-ops-work-view="${w.id}">${admin&&!w.submitted_at?'查看 / 处理':'详情'}</button>${!admin&&!['submitted','exempt'].includes(w.state)?`<button class="text-button" data-ops-quick-submit="${w.id}">快捷提交</button>`:''}${admin?`<button class="text-button" data-ops-refresh="${w.id}">刷新数据</button><button class="text-button danger-text" data-report-work-delete="${w.id}">删除</button>`:''}</div></td></tr>`).join('')||'<tr><td colspan="10"><div class="empty-state">暂无符合条件的作品。调整筛选，或等待员工报送链接。</div></td></tr>'}</tbody></table></div><div class="log-pagination ops-pages"><button class="secondary" id="ops-prev" ${ops.page<=1?'disabled':''}>上一页</button><span class="subtle">共 ${result.total} 件 · 第 ${result.page} / ${Math.max(1,Math.ceil(result.total/30))} 页</span><button class="secondary" id="ops-next" ${ops.page*30>=result.total?'disabled':''}>下一页</button></div></section>`;
}
function opsSubmitPage(){
  const accounts=opsOwnBindings().filter(b=>b.enabled);
  if(!accounts.length)return `<section class="panel">${empty('先关联你负责的抖音账号','可以自行登录添加多个账号，也可以由管理员粘贴主页分配。','users')}<div class="panel-body"><button class="primary" data-page="myaccounts">前往我的抖音账号</button></div></section>`;
  return `<section class="panel"><div class="panel-header"><span class="panel-title">${icon('plus')}提交抖音作品</span><button class="text-button" data-page="myaccounts">管理我的账号 ↗</button></div><form id="ops-submit-form" class="panel-body"><div class="field"><label for="ops-work-link">作品链接</label><textarea id="ops-work-link" name="link" required placeholder="粘贴抖音作品链接或完整分享文本">${esc(ops.submitDraft)}</textarea></div><div class="field"><label for="ops-submit-binding">核验账号</label><select id="ops-submit-binding" name="binding_id">${opsSubmitAccountOptions()}</select><p>支持你负责的 ${accounts.length} 个抖音账号；留在自动识别即可按作品作者核验。</p></div><div class="notice">${icon('info')}发布后 ${ops.data.settings.deadline_hours} 小时内提交。成功后记录真实提交时间，不能自行修改；逾期仍可补交。</div><p class="ops-form-error" id="ops-submit-error" role="alert"></p><button class="primary" type="submit">${icon('check')}核验并提交</button></form></section>`;
}
const baseOpsSettings=opsSettings;
opsSettings=function(){
  const tabs=[['rules','巡检规则'],['connection','采集连接'],['logs','操作日志']];
  const tab=ops.settingsTab;
  let content='';
  if(tab==='rules')content=baseOpsSettings().split('<section class="panel"><div class="panel-header"><span class="panel-title"><i data-icon="tasks">')[0].replace('<button class="text-button" data-page="logs">操作日志 ↗</button>','');
  if(tab==='connection')content=opsConnection();
  if(tab==='logs')content=opsLogPanel();
  return `<div class="ops-tabs" role="tablist">${tabs.map(([id,name])=>`<button type="button" role="tab" aria-selected="${tab===id}" data-ops-settings-tab="${id}">${name}</button>`).join('')}</div>${content}`;
};
function opsConnection(){const c=ops.data.connection;return `<section class="panel"><div class="panel-header"><span class="panel-title">${icon('link')}运营采集连接</span><span class="status-chip ${c.service.ready?'ok':''}">${c.service.ready?'服务已就绪':'待连接'}</span></div><form class="panel-body" id="ops-connection-form"><div class="field"><label for="ops-api-url">本机采集 API</label><input name="api_url" id="ops-api-url" type="url" value="${esc(c.api_url)}" required></div><div class="field"><label for="ops-api-token">API Token（可选）</label><input name="token" id="ops-api-token" type="password" autocomplete="off" placeholder="${c.has_token?'已保存，留空保留':'默认无需填写'}"></div><details><summary>备用采集登录信息</summary><div class="field"><label for="ops-api-cookie">备用抖音 Cookie</label><textarea name="cookie" id="ops-api-cookie" autocomplete="off" placeholder="${c.has_cookie?'已保存，留空保留':'填写经授权的采集 Cookie；留空保留原配置'}"></textarea></div></details><p class="subtle ops-footnote">采集用于解析报送链接、核验作品作者及刷新作品统计。服务已就绪仅表示本机 API 可连接。员工报送的扫码核验只保存身份凭证，不会保存采集 Cookie；后台优先使用绑定的采集凭据，其次原已配置的默认采集账号，最后使用这里的备用 Cookie。独立采集工具的账号配置单独管理。</p><div class="result-actions"><button class="primary" type="submit">保存连接配置</button><button class="secondary" type="button" id="ops-connection-test">启动并检查连接</button></div><p id="ops-connection-message" role="status" class="subtle">${esc(c.service.message)}</p></form></section>`}
function opsLogPanel(){return `<section class="panel"><div class="panel-header"><span class="panel-title">${icon('file')}运营操作日志</span><a class="link-inline" href="/api/ops/logs/export?${new URLSearchParams(opsLogs.filters)}" download>导出 CSV</a></div><div class="panel-body"><form id="ops-log-filter" class="form-grid"><div class="field"><label for="ops-log-search">事件 / 人员 / 任务关键词</label><input name="search" id="ops-log-search" value="${esc(opsLogs.filters.search||'')}"></div><div class="field"><label for="ops-log-level">日志级别</label><select name="level" id="ops-log-level"><option value="">全部</option>${['INFO','WARN','ERROR'].map(l=>`<option ${opsLogs.filters.level===l?'selected':''}>${l}</option>`).join('')}</select></div><button class="secondary" type="submit">筛选日志</button></form><div id="ops-log-results">正在读取日志…</div></div></section>`}
async function opsLoadLogs(){const r=await api('/ops/logs?'+new URLSearchParams({...opsLogs.filters,page:opsLogs.page}));if(state.page!=='opssettings'||ops.settingsTab!=='logs')return;opsLogs.result=r;opsLogs.page=r.page;document.querySelector('#ops-log-results').innerHTML=r.items.map(l=>`<article class="log-entry"><div class="log-meta"><time>${esc(l.created_at)}</time><span>${esc(l.level)}</span></div><b>${esc(l.event)}</b><p>${esc(l.title)} ${esc(l.message)}</p></article>`).join('')+`<div class="log-pagination"><button class="secondary" data-ops-log-page="-1" ${r.page<=1?'disabled':''}>上一页</button><span>共 ${r.total} 条 · ${r.page} 页</span><button class="secondary" data-ops-log-page="1" ${r.page*r.page_size>=r.total?'disabled':''}>下一页</button></div>`}
async function loadOps(){
  const seq=++ops.sequence,page=state.page;if(!ops.user||!isOpsPage(page))return;
  const admin=ops.user.role==='admin',filters=new URLSearchParams({...ops.filters,page:ops.page});
  try{
    const [bindings,people,works,summary,settings,jobs,accountLogins,connection,todo,progress]=await Promise.all([
      api('/ops/bindings'+(page==='bindings'&&ops.bindingsDeleted?'?deleted=1':'')),admin?api('/ops/people'):null,
      ['works','missing','history'].includes(page)?api('/ops/works?'+filters):null,
      ['overview','statistics','mine'].includes(page)?api('/ops/summary?'+filters):null,
      page==='opssettings'?api('/ops/settings'):page==='submit'?api('/ops/submission-rules'):null,
      page==='myaccounts'?api('/ops/my-jobs'):[],
      page==='myaccounts'?api('/ops/account-logins'):[],
      page==='opssettings'&&ops.settingsTab==='connection'?api('/ops/connection'):null,
      page==='mine'?api('/ops/todos?'+new URLSearchParams(ops.filters)):null,
      ['overview','unreported'].includes(page)?api('/ops/report-progress?'+new URLSearchParams(page==='overview'?{department:ops.filters.department||'',group:ops.filters.group||'',employee:ops.filters.employee||'',status:'all'}:{...ops.filters,page:ops.page})):null]);
    if(seq!==ops.sequence||page!==state.page)return;
    ops.data={bindings,people,works,summary,settings,jobs,accountLogins,connection,todo,progress};
    let html='';
    if(['works','missing','history'].includes(page))html+=`<section class="panel"><div class="panel-body ops-filter-panel">${opsFilter()}</div></section>`;
    if(page==='overview')html+=reportOverviewPage();
    else if(page==='statistics')html+=opsScopeFilter()+opsInteractionSummary()+opsRank();
    else if(page==='unreported')html+=reportProgressPage();
    else if(page==='mine')html+=opsActions()+opsTodo()+`<details class="ops-personal-stats"><summary>个人作品统计与时间筛选</summary>${opsScopeFilter()+opsSummary()}</details>`;
    else if(['works','history'].includes(page))html+=opsWorkList();
    else if(page==='missing')html+=`<div class="notice">${icon('info')}只对完整核查的发布记录判断漏报。可查看详情并记录无需提交的原因；账号采集情况请到巡检状态查看。</div>`+opsWorkList();
    else if(page==='audit')html+='';
    else if(page==='people')html+=opsPeople();
    else if(page==='bindings')html+=opsBindings();
    else if(page==='myaccounts')html+=`<div id="ops-my-accounts">${opsMyAccounts()}</div>`;
    else if(page==='opssettings')html+=opsSettings();
    else html+=opsSubmitPage();
    document.querySelector('#ops-body').innerHTML=html;hydrate();bindOpsControls();bindOpsFlow();
    if(page==='opssettings'&&ops.settingsTab==='logs')await opsLoadLogs();
  }catch(e){if(seq===ops.sequence&&document.querySelector('#ops-body'))document.querySelector('#ops-body').textContent=e.message;toast(e.message)}
}
function opsViewWork(id){
  const w=(ops.data.works?.items||ops.data.todo?.items||[]).find(w=>w.id===id);if(!w)return;
  const admin=ops.user.role==='admin';
  opsModal('作品详情与核查',`<div class="detail-list"><b>${esc(w.description||w.id)}</b><p>账号：${esc(w.nickname)}<br>责任人：${esc(w.employee_name)} · ${esc(w.group_name||'未分组')}<br>发布时间：${opsDate(w.published)}<br>提交时间：${opsDate(w.submitted_at)}<br>当前状态：${esc(w.state_label)}${w.late?' · 逾期提交':''}</p><p>巡检说明：${esc(w.check_message)}</p>${w.explanation?`<div class="notice">员工说明：${esc(w.explanation)}<br>${opsDate(w.explanation_at)}</div>`:''}${w.review_note?`<p>管理员处理：${esc(w.review_note)}<br>${opsDate(w.reviewed_at)}</p>`:''}${Object.entries(metricNames).map(([key,label])=>`<p>${label}：${opsMetric(w.metrics[key])} <small>最近成功 ${opsDate(w.metrics[key]?.updated_at)}</small></p>`).join('')}<a class="link-inline" href="${safeOpsUrl(w.share_url)}" target="_blank" rel="noreferrer">打开作品 ↗</a></div>${!w.submitted_at?`<form id="ops-work-action" class="section-spacer">${admin?`<div class="field"><label for="ops-review-action">核查处理</label><select id="ops-review-action" name="action"><option value="exempt">无需提交（从应提交数排除）</option><option value="reopen">恢复正常核查</option></select></div>`:''}<div class="field"><label for="ops-work-note">${admin?'处理理由':'我的情况说明'}</label><textarea id="ops-work-note" name="note" minlength="2" maxlength="1000" required>${esc(admin?'':w.explanation||'')}</textarea></div><p class="ops-form-error" id="ops-work-error" role="alert"></p><button class="primary" type="submit">${admin?'保存处理结果':'提交情况说明'}</button></form>`:''}`);
  document.querySelector('#ops-work-action')?.addEventListener('submit',async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{const r=await post(admin?'/ops/review':'/ops/explain',{id,...Object.fromEntries(new FormData(e.target))});document.querySelector('#detail-dialog').close();toast(r.message);loadOps()}catch(err){document.querySelector('#ops-work-error').textContent=err.message}finally{b.disabled=false}});
}
function bindOpsFlow(){
  document.querySelector('#ops-submit-form')?.addEventListener('submit',async e=>{e.preventDefault();const b=e.target.querySelector('[type=submit]');b.disabled=true;b.textContent='正在核验作品归属…';ops.submitDraft=e.target.elements.link.value;try{const r=await post('/ops/submit',Object.fromEntries(new FormData(e.target)));e.target.reset();ops.submitDraft='';toast(r.message);opsOpen('history',{state:'submitted'})}catch(err){document.querySelector('#ops-submit-error').textContent=err.message}finally{b.disabled=false;b.textContent='核验并提交'}});
  document.querySelector('#ops-connection-form')?.addEventListener('submit',async e=>{e.preventDefault();try{await post('/ops/connection',Object.fromEntries(new FormData(e.target)));toast('连接配置已保存');loadOps()}catch(err){toast(err.message)}});
  document.querySelector('#ops-connection-test')?.addEventListener('click',async e=>{e.target.disabled=true;try{const r=await post('/ops/connection/test',{});document.querySelector('#ops-connection-message').textContent=r.connection_message;toast(r.connection_message)}catch(err){toast(err.message)}finally{e.target.disabled=false}});
  document.querySelector('#ops-log-filter')?.addEventListener('submit',e=>{e.preventDefault();opsLogs.filters=Object.fromEntries(new FormData(e.target));opsLogs.page=1;loadOps()});
}
document.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b||!ops.user)return;
  try{
    if(b.dataset.opsState){const page=ops.user.role==='admin'?'missing':'history';opsOpen(page,{...ops.filters,state:b.dataset.opsState})}
    if(b.dataset.opsQuickSubmit)opsQuickSubmit(b.dataset.opsQuickSubmit);
    if(b.dataset.opsWorkView)opsViewWork(b.dataset.opsWorkView);
    if(b.dataset.opsSettingsTab){ops.settingsTab=b.dataset.opsSettingsTab;if(state.page==='opssettings')loadOps();else navigate('opssettings')}
    if(b.dataset.opsOwnInspect){b.disabled=true;const r=await post('/ops/jobs',{kind:'inspect',target:b.dataset.opsOwnInspect});toast(r.message);await opsRefreshAccounts()}
    if(b.dataset.opsLogPage){opsLogs.page+=Number(b.dataset.opsLogPage);await opsLoadLogs()}
  }catch(err){toast(err.message);b.disabled=false}
});
document.addEventListener('DOMContentLoaded',()=>{
  document.querySelector('#help-button').onclick=()=>opsModal('运营工作空间使用说明',ops.user?.role==='admin'?'<p class="detail-list">1. 创建员工与运营小组。<br>2. 绑定抖音主页并指定责任人，员工也可自行登录绑定。<br>3. 在系统设置检查采集连接，然后巡检账号发布列表。<br>4. 查看作品表现和提交情况，在漏报核查中处理员工说明。<br>5. 按筛选导出 Excel，操作日志可在系统设置查看。</p>':'<p class="detail-list">1. 在我的抖音账号登录并绑定账号，或使用管理员分配的账号。<br>2. 检查新作品，在个人工作台查看待办。<br>3. 粘贴链接提交，或使用作品记录中的快捷提交。<br>4. 逾期仍可补交；特殊情况可以在作品详情中填写说明。<br>5. 我的作品记录可查看统计与管理员处理结果。</p>');
});
