// Local account management. Secret cookie values are never fetched from the server.
function accountBadge(a) {
  if (a.login && ['opening','waiting','cancelling'].includes(a.login.state)) return ['等待登录','running'];
  if (a.paused) return ['已暂停','paused'];
  if (a.cooldown_until > Date.now()/1000) return ['等待检查','paused'];
  if (a.has_cookie) return ['Cookie 已保存','ok'];
  return ['待登录',''];
}

function accountPicker() {
  if (state.platform !== 'douyin') return '';
  const pooling=state.page==='collect'&&document.querySelector('#collect-kind')?.value==='comment';
  if(pooling){
    const available=state.accounts.filter(a=>a.has_cookie&&!a.paused).length;
    return `<div class="field account-picker"><label>采集账号 <button type="button" class="text-button" data-page="accounts">管理账号 ↗</button></label><input type="hidden" name="account_mode" value="pool"><div class="account-pool-display"><span>${icon('users')}自动轮换全部可用账号</span><span class="status-chip ${available?'ok':''}">${available} 个可用账号</span></div><p>使用账号管理中全部已登录、启用的账号；新增可用账号会自动加入，按批轮换直到采集结束。</p></div>`;
  }
  const selected = state.account_id || state.settings.default_account_id || '';
  return `<div class="field account-picker"><label for="task-account">使用账号 <button type="button" class="text-button" data-page="accounts">管理账号 ↗</button></label><select name="account_id" id="task-account"><option value="">${state.settings.default_account_id?'使用默认账号':'沿用连接设置的 Cookie'}</option>${state.accounts.map(a=>`<option value="${esc(a.id)}" ${a.id===selected?'selected':''} ${!a.has_cookie||a.paused?'disabled':''}>${esc(a.name)}${a.paused?' · 已暂停':a.has_cookie?'':' · 待登录'}</option>`).join('')}</select><p>任务固定使用所选账号；请求间隔与异常暂停规则按该账号执行。</p></div>`;
}

function accountLoginPanel() {
  const saved = state.accounts.filter(a=>a.has_cookie).length;
  return `<section class="panel"><div class="quick-header"><h2>${icon('users')}连接抖音账号</h2><p>官方窗口登录，完成后自动同步 Cookie</p></div><div class="panel-body account-login-body"><div class="account-login-intro"><span class="platform-mark">♪</span><div><b>无需查找或复制 Cookie</b><p>在专用 Chrome 窗口点击「登录」，自行扫码或验证。检测到登录后会自动保存，并关闭专用窗口。</p></div></div><div class="result-actions"><button type="button" class="primary" data-login-default>${icon('external')}${saved?'重新登录 / 同步抖音':'登录抖音并自动同步'}</button><button type="button" class="secondary" data-page="accounts">账号管理${saved?' · '+saved+' 个':''}</button></div><p class="muted account-privacy">仅读取专用窗口的抖音登录信息，保存在本机。已保存 Cookie 不会在网页中明文显示。</p><div id="settings-login-status" role="status">${state.accounts.filter(a=>a.login&&a.login.state!=='idle').map(a=>`<p class="account-login-message">${esc(a.name)}：${esc(a.login.message)}</p>`).join('')}</div></div></section>`;
}

function accountsPage() {
  const accounts = state.accounts;
  return `<div class="workspace-grid"><div><section class="panel"><div class="quick-header"><h2>${icon('users')}我的抖音账号</h2><p>独立登录、清晰展示，为每个任务选择账号</p></div><div class="panel-body account-login-body"><form id="add-account-form" class="add-account-form"><div class="field"><label for="account-name">账号备注名称</label><input id="account-name" name="name" maxlength="40" required placeholder="例如：我的主账号 / 运营账号"></div><button class="primary" type="submit">${icon('plus')}添加账号</button></form><div class="account-summary"><span>${accounts.length} 个账号</span><span>${accounts.filter(a=>a.has_cookie&&!a.paused).length} 个已保存 Cookie · ${accounts.filter(a=>a.paused).length} 个已暂停</span></div></div></section><div class="account-cards">${accounts.length?accounts.map(a=>{const [label,color]=accountBadge(a);const active=a.login&&['opening','waiting','cancelling'].includes(a.login.state);return `<section class="panel account-card" data-account-card="${esc(a.id)}"><div class="panel-header"><div class="account-card-heading"><span class="account-avatar">${esc(a.name.slice(0,1))}</span><div><b>${esc(a.name)}</b><small>抖音${a.is_default?' · 默认账号':''}</small></div></div><span class="status-chip ${color}">${label}</span></div><form class="panel-body edit-account-form" data-account-id="${esc(a.id)}"><div class="form-grid"><div class="field"><label for="name-${a.id}">备注名称</label><input id="name-${a.id}" name="name" maxlength="40" value="${esc(a.name)}" required></div><div class="field"><label for="interval-${a.id}">请求间隔（秒）</label><input id="interval-${a.id}" name="cooldown_seconds" type="number" min="5" max="3600" value="${a.cooldown_seconds}" required></div></div><div class="account-meta"><span>最近同步</span><span>${a.last_login?esc(new Date(a.last_login).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})):'尚未登录'}</span></div><div class="account-meta"><span>连续请求失败</span><span>${a.failure_count} 次</span></div><p class="account-login-message ${a.last_error?'danger-text':''}" data-login-status="${esc(a.id)}" role="status">${esc(active||a.login?.state==='saved'||a.login?.state==='failed'||a.login?.state==='cancelled'?a.login.message:a.last_error||'点击登录，完成扫码后将自动保存 Cookie')}</p><div class="account-buttons"><button type="button" class="primary" data-account-login="${esc(a.id)}" ${active?'disabled':''}>${icon('external')}${a.has_cookie?'重新登录 / 同步':'登录抖音'}</button>${active?`<button type="button" class="secondary" data-account-cancel="${esc(a.id)}">取消登录</button>`:''}<button type="submit" class="secondary">保存设置</button><button type="button" class="text-button" data-account-pause="${esc(a.id)}" data-paused="${a.paused}">${a.paused?'恢复使用':'暂停使用'}</button>${!a.is_default?`<button type="button" class="text-button" data-account-default="${esc(a.id)}">设为默认</button>`:''}</div><details class="manual-cookie"><summary>手动填写此账号 Cookie</summary><div class="field"><label for="cookie-${a.id}">Cookie 请求头</label><textarea id="cookie-${a.id}" name="cookie" autocomplete="off" spellcheck="false" placeholder="留空保留已有 Cookie，填写新值后点击保存设置"></textarea></div></details></form></section>`}).join(''):`<section class="panel">${empty('添加你的第一个抖音账号','填写上方备注名称，再点击账号卡片中的「登录抖音」。','users')}</section>`}</div></div><aside class="right-column"><section class="panel"><div class="panel-header"><span class="panel-title">${icon('shield')}账号使用规则</span></div><div class="panel-body"><div class="guide-step"><span class="step-number">1</span><div><b>每个账号独立登录</b><p>扫码与验证码由你在抖音官方窗口完成。不同账号使用独立浏览器资料。</p></div></div><div class="guide-step"><span class="step-number">2</span><div><b>评论按批自动轮换</b><p>评论任务自动使用账号池；其他任务可指定账号。暂停账号不参与新的批次。</p></div></div><div class="guide-step"><span class="step-number">3</span><div><b>异常后等待并检查</b><p>请求失败会延长等待时间；连续 3 次失败后暂停账号，检查后可手动恢复。</p></div></div></div></section><div class="tip-card"><div class="tip-title">${icon('bulb')}评论采集建议</div><p>评论与回复分批提交，各账号分别遵守间隔。平台返回异常时会停止并保留进度，检查后可重试续采。</p></div></aside></div>`;
}

async function beginAccountLogin(id) {
  const result = await post(`/accounts/${id}/login`,{});
  await refresh(); render(); toast(result.message);
}

function bindAccountUI() {
  const mountAccountPicker=form=>{
    form.querySelector('.account-picker')?.remove();
    form.querySelector('.platform-row')?.insertAdjacentHTML('afterend',accountPicker());
    form.querySelector('#task-account')?.addEventListener('change',e=>{state.account_id=e.target.value;remember()});
    hydrate();
  };
  document.querySelectorAll('#download-form,#collect-form,#live-form').forEach(mountAccountPicker);
  document.querySelector('#collect-kind')?.addEventListener('change',()=>mountAccountPicker(document.querySelector('#collect-form')));
  document.querySelector('#add-account-form')?.addEventListener('submit',async e=>{
    e.preventDefault(); const button=e.target.querySelector('[type=submit]'); button.disabled=true;
    try { await post('/accounts',Object.fromEntries(new FormData(e.target))); await refresh(); render(); toast('账号已添加，请点击「登录抖音」'); }
    catch(err){toast(err.message)} finally{button.disabled=false}
  });
  document.querySelectorAll('.edit-account-form').forEach(form=>form.addEventListener('submit',async e=>{
    e.preventDefault(); const data=Object.fromEntries(new FormData(form)); data.cooldown_seconds=Number(data.cooldown_seconds);
    try{await post(`/accounts/${form.dataset.accountId}`,data);await refresh();render();toast('账号设置已保存')}catch(err){toast(err.message)}
  }));
}

function initAccountUI() {
  document.addEventListener('click',async e=>{
    const button=e.target.closest('button'); if(!button)return;
    try {
      if(button.hasAttribute('data-login-default')) {
        button.disabled=true;
        let account=state.accounts.find(a=>a.id===state.settings.default_account_id);
        if(!account)account=await post('/accounts',{name:'我的抖音账号'});
        await beginAccountLogin(account.id);
      }
      if(button.dataset.accountLogin){button.disabled=true;await beginAccountLogin(button.dataset.accountLogin)}
      if(button.dataset.accountCancel){await post(`/accounts/${button.dataset.accountCancel}/cancel-login`,{});await refresh();render();toast('正在取消登录')}
      if(button.dataset.accountPause){await post(`/accounts/${button.dataset.accountPause}`,{paused:button.dataset.paused!=='true'});await refresh();render();toast(button.dataset.paused==='true'?'账号已恢复':'账号已暂停')}
      if(button.dataset.accountDefault){await post(`/accounts/${button.dataset.accountDefault}`,{is_default:true});await refresh();render();toast('已设为默认账号')}
    } catch(err) {button.disabled=false;toast(err.message)}
  });
  let busy=false;
  setInterval(async()=>{
    if(busy||!state.accounts.some(a=>a.login&&['opening','waiting','cancelling'].includes(a.login.state)))return;
    busy=true;
    try {
      const accounts=await api('/accounts');
      const completed=accounts.some(a=>['saved','failed','cancelled'].includes(a.login?.state)&&['opening','waiting','cancelling'].includes(state.accounts.find(old=>old.id===a.id)?.login?.state));
      state.accounts=accounts;
      for(const a of accounts){const el=document.querySelector(`[data-login-status="${a.id}"]`);if(el)el.textContent=a.login.message}
      const el=document.querySelector('#settings-login-status');
      if(el)el.innerHTML=accounts.filter(a=>a.login&&a.login.state!=='idle').map(a=>`<p class="account-login-message">${esc(a.name)}：${esc(a.login.message)}</p>`).join('');
      if(completed){await refresh();if(!document.querySelector('#content input:focus,#content textarea:focus'))render();toast(accounts.find(a=>['saved','failed','cancelled'].includes(a.login?.state))?.login.message||'登录状态已更新')}
    } catch(err){toast(err.message)} finally{busy=false}
  },2000);
}

function poolTaskDetails(t){
  if(t.account_name!=='自动账号池')return '';
  const usage=Object.values(t.account_usage||{});
  return `<div class="section-spacer"><div class="subheading">账号池采集进度</div><p class="muted">已完成主评论 ${t.comment_pages||0} 页 · 当前账号：${esc(t.current_account||'等待分配')}</p>${usage.length?`<div class="record-wrap"><table><thead><tr><th>账号备注</th><th>完成批次</th><th>保存条数</th></tr></thead><tbody>${usage.map(a=>`<tr><td>${esc(a.name)}</td><td>${a.batches}</td><td>${a.records}</td></tr>`).join('')}</tbody></table></div>`:''}</div>`;
}
