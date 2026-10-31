// Employee login credentials stay server-side; this view receives only scoped status.
function opsOwnBindings(){return (ops.data?.bindings||[]).filter(b=>b.owner_id===ops.user.id)}
function opsSubmitAccountOptions(){return '<option value="">按作品作者自动识别</option>'+opsOwnBindings().filter(b=>b.enabled).map(b=>`<option value="${esc(b.id)}">${esc(b.nickname)}</option>`).join('')}
function opsLoginCard(login,binding){
  const active=['opening','waiting','cancelling'].includes(login?.login?.state);
  const logged=!!login?.has_cookie;
  const label=active?'等待登录':logged?(login.identity_verified?'已登录 · 已识别主页':'已登录 · 主页由本人确认'):'管理员分配 · 待登录';
  return `<div class="ops-binding ops-own-account"><div class="ops-check-row"><div><b>${esc(binding?.nickname||login.nickname)}</b><small>${binding?'抖音 · '+(binding.enabled?'监测中':'监测已停用'):'抖音 · 等待完成绑定'}</small></div><span class="status-chip ${logged&&!active?'ok':''}">${binding?label:active?'等待登录':logged?'已登录 · 待绑定':'尚未登录'}</span></div>${binding?`<p class="subtle ops-account-uid">${esc(binding.sec_uid)}</p>`:''}${binding?`<p class="subtle ops-account-status">最近检查：${opsDate(binding.last_check)} · ${{complete:'核查完成',incomplete:'待确认',error:'无法核查',unknown:'尚未核查'}[binding.check_state]||'待确认'}${['queued','running','retrying'].includes(binding.job_status)?' · 刷新处理中':''}<br>${esc(binding.check_message)}</p>`:''}<p class="ops-account-status" role="status">${esc(login?.login?.message||(binding?'管理员已分配该账号，你可以登录并同步自己的抖音。':''))}</p><div class="result-actions">${active?`<button class="secondary" data-ops-login-cancel="${esc(login.id)}">取消登录</button>`:`<button class="secondary" ${login?`data-ops-login-retry="${esc(login.id)}"`:`data-ops-login-binding="${esc(binding.id)}"`}>${icon('external')}${logged?'重新登录':'登录抖音'}</button>`}${logged&&!active&&(!binding||!login.identity_verified)?`<button class="text-button" data-ops-login-confirm="${esc(login.id)}">${binding?'确认本人主页':'填写本人主页并绑定'}</button>`:''}${binding&&binding.enabled?`<button class="secondary" data-ops-own-inspect="${esc(binding.id)}" ${['queued','running','retrying'].includes(binding.job_status)?'disabled':''}>检查新作品</button>`:''}${binding?`<a class="link-inline" href="${safeOpsUrl(binding.homepage)}" target="_blank" rel="noreferrer">打开主页 ↗</a>`:''}</div></div>`;
}
function opsMyAccounts(){
  const bindings=opsOwnBindings(),logins=ops.data?.accountLogins||[];
  const cards=bindings.map(b=>{const options=logins.filter(l=>l.binding_id===b.id).sort((a,c)=>(c.has_cookie-a.has_cookie)||(c.updated-a.updated));return opsLoginCard(options[0],b)}).join('')+logins.filter(l=>!l.binding_id).map(l=>opsLoginCard(l,null)).join('');
  return `<section class="panel"><div class="panel-header ops-account-header"><span class="panel-title">${icon('users')}我负责的账号 <span class="subtle">${bindings.length} 个</span></span><button class="primary" data-ops-login-new>${icon('plus')}登录并添加账号</button></div><div class="panel-body"><p class="subtle ops-footnote">支持一个员工绑定多个抖音账号，也可由管理员粘贴主页分配。点击登录，在抖音官方窗口自行扫码或验证，登录信息仅保存在本机。</p>${cards||'<div class="empty-state">暂无绑定账号。登录自己的抖音即可添加，也可由管理员分配。</div>'}<p class="subtle ops-footnote">登录后优先识别本人主页；未识别时请粘贴本次登录账号的主页链接并确认。账号已归属其他员工时，需由管理员调整。</p></div></section>`;
}
let opsLoginPolling=false;
async function opsRefreshAccounts(){
  if(!ops.user||state.page!=='myaccounts')return;
  const userId=ops.user.id;
  const [bindings,logins]=await Promise.all([api('/ops/bindings'),api('/ops/account-logins')]);
  if(!ops.user||ops.user.id!==userId||state.page!=='myaccounts'||!ops.data)return;
  ops.data.bindings=bindings;ops.data.accountLogins=logins;
  const panel=document.querySelector('#ops-my-accounts');
  if(panel){panel.innerHTML=opsMyAccounts();hydrate()}
  const select=document.querySelector('#ops-submit-binding');
  if(select&&document.activeElement!==select){const value=select.value;select.innerHTML=opsSubmitAccountOptions();select.value=value}
}
async function opsStartLogin(data){
  const result=await post('/ops/account-logins/start',data);
  await opsRefreshAccounts();toast(result.login.message||'请在抖音官方窗口完成登录');
}
function opsNewLogin(){
  opsModal('登录并添加抖音账号',`<form id="ops-login-form">${opsField('账号备注名称','nickname','','text','required maxlength="80" placeholder="例如：品牌主账号 / 门店账号"')}<p class="subtle ops-footnote">每次新增使用独立的登录窗口。请在抖音官方窗口完成扫码或验证码；成功后绑定到你自己的员工账号。可以继续添加多个账号。</p><p class="ops-form-error" id="ops-login-error" role="alert"></p><button class="primary" type="submit">${icon('external')}打开抖音登录</button></form>`);
  document.querySelector('#ops-login-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{await opsStartLogin(Object.fromEntries(new FormData(e.target)));document.querySelector('#detail-dialog').close()}catch(err){document.querySelector('#ops-login-error').textContent=err.message}finally{b.disabled=false}};
}
function opsConfirmLogin(id){
  const login=ops.data.accountLogins.find(l=>l.id===id),binding=opsOwnBindings().find(b=>b.id===login?.binding_id);
  opsModal('确认本次登录账号的主页',`<form id="ops-login-confirm-form">${opsField('抖音本人主页链接 / 分享文本','homepage',binding?.homepage||'','text','required placeholder="粘贴刚才登录账号的抖音主页"')}<p class="subtle ops-footnote">请确认主页属于刚才登录的抖音账号。手动填写会标记为“主页由本人确认”；系统不会将它标记为自动识别。已由管理员分配的账号不会改变责任人。</p><p class="ops-form-error" id="ops-login-error" role="alert"></p><button class="primary" type="submit">确认并绑定到我的账号</button></form>`);
  document.querySelector('#ops-login-confirm-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{const result=await post('/ops/account-logins/complete',{id,...Object.fromEntries(new FormData(e.target))});document.querySelector('#detail-dialog').close();await opsRefreshAccounts();toast(result.message)}catch(err){document.querySelector('#ops-login-error').textContent=err.message}finally{b.disabled=false}};
}
document.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b||!ops.user)return;
  try{
    if(b.hasAttribute('data-ops-login-new'))return opsNewLogin();
    if(b.dataset.opsLoginConfirm)return opsConfirmLogin(b.dataset.opsLoginConfirm);
    if(b.dataset.opsLoginRetry||b.dataset.opsLoginBinding){b.disabled=true;try{await opsStartLogin(b.dataset.opsLoginRetry?{id:b.dataset.opsLoginRetry}:{binding_id:b.dataset.opsLoginBinding})}finally{b.disabled=false}}
    if(b.dataset.opsLoginCancel){await post('/ops/account-logins/cancel',{id:b.dataset.opsLoginCancel});await opsRefreshAccounts()}
  }catch(err){toast(err.message)}
});
setInterval(async()=>{
  if(opsLoginPolling||!ops.user||state.page!=='myaccounts'||!((ops.data?.accountLogins||[]).some(l=>['opening','waiting','cancelling'].includes(l.login.state))||(ops.data?.bindings||[]).some(b=>['queued','running','retrying'].includes(b.job_status))))return;
  opsLoginPolling=true;try{await opsRefreshAccounts()}catch{}finally{opsLoginPolling=false}
},2000);
