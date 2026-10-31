/* Employee reporting is public; receipt capabilities stay in this browser. */
(()=>{
  'use strict';
  const $=id=>document.getElementById(id);
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const storage={get(key,fallback){try{return JSON.parse(localStorage.getItem(key))??fallback}catch{return fallback}},set(key,value){try{localStorage.setItem(key,JSON.stringify(value))}catch{}}};
  let directory=null,receipts=storage.get('douk.report.receipts',[]),request=null,timer=null,refreshing=false;
  const noteDrafts={},noteOpen=new Set();let saving=false,loginToken='',loginTimer=null,accountTimer=null;
  const loginKey='douk.report.login',rememberedKey='douk.report.verified',accountsKey='douk.report.verified.accounts';
  let loginAction=0,accountRevision=0,accountStates=new Map();
  const validToken=token=>typeof token==='string'&&/^[a-f0-9]{64}$/.test(token);
  function savedTokens(){const value=storage.get(accountsKey,[]);return Array.isArray(value)?[...new Set(value.filter(validToken))].slice(0,100):[]}
  function persistAccounts(tokens){storage.set(accountsKey,[...new Set(tokens.filter(validToken))]);accountRevision++;}
  function pendingToken(token){try{if(token)sessionStorage.setItem(loginKey,token);else sessionStorage.removeItem(loginKey)}catch{}}
  function resetLoginView(){clearTimeout(loginTimer);loginToken='';pendingToken('');$('report-login-panel').hidden=true;delete $('report-login-qr').dataset.pending;$('report-login-qr').removeAttribute('src');$('report-login-qr').hidden=true;$('report-login-home').hidden=true;$('report-login-validity').hidden=true;$('report-login-start').disabled=false;$('report-login-start').textContent='添加核验账号'}

  function validAccountTokens(){return savedTokens().filter(token=>{const a=accountStates.get(token);return a?.verified&&a.identity?.sec_uid&&a.expires_at*1000>Date.now()})}
  function updateSubmit(){const ready=['report-name','report-dept-name','report-group-name'].every(id=>$(id).value.trim()),verified=validAccountTokens().length>0;$('report-submit').disabled=saving||!ready||!verified;$('report-ready').textContent=!ready?'请填写部门、小组和姓名，无需网站登录。':!verified?'请先添加至少一个有效的抖音核验账号，再提交作品。':'报送信息与账号核验已就绪，可以提交作品。'}
  if(!Array.isArray(receipts))receipts=[];
  const receiptItems=items=>Array.isArray(items)?items.filter(i=>i&&typeof i==='object'&&!Array.isArray(i)):[];
  receipts=receipts.filter(r=>r&&typeof r.receipt_token==='string').slice(0,5).map(r=>({...r,items:receiptItems(r.items)}));
  const labels={queued:'等待识别',processing:'正在识别',submitted:'已报送',review:'待管理员核实',failed:'处理失败',duplicate:'已报送过',rejected:'未通过核实',removed:'管理员已移除'};
  const active=r=>(r.items||[]).some(i=>['queued','processing'].includes(i.status));
  const date=t=>{const value=new Date(Number(t)*1000);return t&&Number.isFinite(value.getTime())?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}).format(value):'时间待确认'};
  async function api(path,data){const r=await fetch('/api/reporting/'+path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json'},...(data?{body:JSON.stringify(data)}:{})});let result;try{result=await r.json()}catch{throw Error('服务暂时没有响应，请保留链接后重试。')}if(!r.ok){const error=Error(result.error||'操作失败，请稍后重试。');error.status=r.status;throw error}return result}
  function persist(){storage.set('douk.report.receipts',receipts.map(r=>({receipt_token:r.receipt_token,items:r.items,received_at:r.received_at})));}
  function safeLink(link){try{const url=new URL(link);return ['http:','https:'].includes(url.protocol)&&(url.hostname==='douyin.com'||url.hostname.endsWith('.douyin.com'))?url.href:''}catch{return ''}}
  function renderReceipts(){
    $('report-receipts-section').hidden=false;
    $('report-receipts').innerHTML=receipts.map(r=>`<div class="report-batch"><div class="report-batch-head"><span>${date(r.received_at||r.items?.[0]?.received_at)} · ${(r.items||[]).length} 个作品</span><span>${active(r)?'后台处理中':'处理结果'}</span></div>${(r.items||[]).map(i=>`<div class="report-item"><div class="report-item-info"><b>${escape(i.description||i.work_id||'抖音作品')}</b><small>${escape([i.author?'作者：'+i.author:'',i.published?'发布时间：'+date(i.published):''].filter(Boolean).join(' · '))}</small><small>${escape(i.message||({queued:'链接已保存，等待后台识别。',processing:'正在读取作品信息。',review:'报送已接收，等待管理员确认归属。',failed:'后台会按规则重试，仍失败请联系管理员。',duplicate:'系统已保留原有报送记录，无需重复提交。'}[i.status]||''))}</small>${safeLink(i.link)?`<a href="${escape(safeLink(i.link))}" target="_blank" rel="noreferrer">${escape(i.link)}</a>`:''}</div><span class="report-status ${['submitted','duplicate'].includes(i.status)?'success':i.status==='review'?'attention':['failed','rejected'].includes(i.status)?'failed':''}">${labels[i.status]||'已接收'}</span><button type="button" class="text-button report-add-note" data-note-open="${escape(i.id)}">${i.reporter_note?'查看 / 修改备注':'补充备注'}</button></div><div class="report-note-editor" ${noteOpen.has(i.id)?'':'hidden'} data-note-editor="${escape(i.id)}"><label for="note-${escape(i.id)}">给管理员的备注（最多 1000 字）</label><textarea id="note-${escape(i.id)}" data-note-input="${escape(i.id)}" maxlength="1000" rows="3" placeholder="例如：代同事报送、账号更名或本次发布的特殊情况">${escape(noteDrafts[i.id]??i.reporter_note??'')}</textarea><button type="button" class="secondary" data-note-save="${escape(i.id)}">保存备注</button><span role="status" data-note-message="${escape(i.id)}"></span></div>`).join('')}</div>`).join('')||'<p class="report-empty" >本浏览器还没有保存报送回执，提交后会显示在这里。</p>';
  }
  function receiptNotice(message){const notice=$('report-receipt-notice');notice.textContent=message;notice.hidden=!message}
  async function refreshReceipts(manual=false){
    if(refreshing)return;refreshing=true;clearTimeout(timer);if(manual)receiptNotice('');let failed=false;
    try{let removed=false;for(const receipt of [...receipts]){if(!manual&&!active(receipt))continue;try{const latest=await api('receipt',{receipt_token:receipt.receipt_token});receipt.items=latest.items||[];receipt.received_at=latest.received_at||receipt.received_at}catch(error){if(error.status===404){receipts=receipts.filter(r=>r.receipt_token!==receipt.receipt_token);removed=true}else failed=true}}persist();renderReceipts();if(removed)receiptNotice('已清理后台不存在的历史回执，不影响继续报送。如仍需登记相关作品，确认后可重新报送。');if(manual&&failed)receiptNotice((removed?'已清理失效的历史回执。':'')+'部分回执暂时无法更新，已保留本机记录，请稍后刷新。')}finally{refreshing=false;if(receipts.some(active))timer=setTimeout(()=>refreshReceipts(),failed?15000:4000)}
  }
  $('report-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!['report-name','report-dept-name','report-group-name'].every(id=>$(id).value.trim())){$('report-error').textContent='请填写部门、小组和姓名，无需网站登录。';return}
    const links=$('report-links').value.trim();if(!links)return;
    if(!validAccountTokens().length){$('report-error').textContent='请先完成至少一个抖音账号核验后再报送。';updateSubmit();return}
    const reporter={name:$('report-name').value.trim(),department:$('report-dept-name').value.trim(),group:$('report-group-name').value.trim(),employee_code:$('report-employee-code').value.trim()};const tokens=savedTokens();const fingerprint=JSON.stringify([reporter,tokens,links]);if(!request||request.fingerprint!==fingerprint)request={fingerprint,request_id:typeof crypto.randomUUID==='function'?crypto.randomUUID():Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)};
    saving=true;const button=$('report-submit');button.disabled=true;button.textContent='正在保存…';$('report-error').textContent='';
    try{const result=await api('submit',{reporter,links,request_id:request.request_id,login_tokens:tokens});if(!result.receipt_token)throw Error('服务未返回回执，请保留链接重试。');receipts=receipts.filter(r=>r.receipt_token!==result.receipt_token);receipts.unshift(result);receipts=receipts.slice(0,5);persist();renderReceipts();$('report-links').value='';request=null;updateCount();$('report-receipts-section').scrollIntoView({behavior:'smooth',block:'start'});refreshReceipts();}catch(error){$('report-error').textContent=error.message}finally{saving=false;updateSubmit();button.innerHTML='提交报送 <span aria-hidden="true">↗</span>'}
  });
  function updateCount(){const text=$('report-links').value;const matches=text.match(/https?:\/\/[^\s<>"，。]+/g)||text.split(/\s+/).filter(v=>/^\d{6,30}$/.test(v));$('report-link-count').textContent=matches.length?`${matches.length} 个链接${matches.length>20?' · 最多 20 个':''}`:'等待粘贴';}
  $('report-links').addEventListener('input',updateCount);$('report-refresh').onclick=()=>refreshReceipts(true);$('report-clear-local').onclick=()=>{receipts=[];persist();clearTimeout(timer);receiptNotice('');renderReceipts()};
  async function init(){renderReceipts();try{directory=await api('directory');directory.departments=directory.departments||[];directory.groups=directory.groups||[];directory.employees=directory.employees||[];$('report-dept-options').innerHTML=directory.departments.map(d=>`<option value="${escape(d.name)}"></option>`).join('');$('report-group-options').innerHTML=directory.groups.map(g=>`<option value="${escape(g.name)}"></option>`).join('');updateSubmit();$('report-rule').textContent=`请在发布后 ${directory.deadline_hours||24} 小时内报送；后台识别和管理员核实不会改变接收时间。`;refreshReceipts(true)}catch(error){updateSubmit();$('report-error').textContent=error.message}}
  function organizationOptions(){if(!directory)return;const dept=directory.departments.find(d=>d.name===$('report-dept-name').value.trim());const groups=directory.groups.filter(g=>!dept||g.department_id===dept.id);$('report-group-options').innerHTML=groups.map(g=>`<option value="${escape(g.name)}"></option>`).join('');const group=groups.find(g=>g.name===$('report-group-name').value.trim());$('report-name-options').innerHTML=directory.employees.filter(e=>group&&e.group_id===group.id).map(e=>`<option value="${escape(e.name)}"></option>`).join('')}
  ['report-name','report-dept-name','report-group-name'].forEach(id=>$(id).oninput=()=>{updateSubmit();organizationOptions()});
  function renderAccounts(){
    updateSubmit();
    const tokens=savedTokens();
    $('report-account-count').textContent=tokens.length?`${tokens.length} 个账号`:'尚未添加';
    $('report-account-list').innerHTML=tokens.map(token=>{
      const result=accountStates.get(token),identity=result?.identity||{},verified=result?.verified&&result.expires_at*1000>Date.now();
      const home=safeLink(identity.homepage);
      return `<div class="report-verified-row"><div><b>${escape(identity.nickname||(result?'已核验账号（昵称暂未获取）':'正在恢复账号…'))}</b><small>${verified?'已核验 · 有效至 '+date(result.expires_at):result?.error||'正在向服务器确认核验状态…'}</small>${identity.nickname_source==='saved_record'?'<small>名称来自已保存的作品或账号记录</small>':''}<small>账号标识：${escape(identity.sec_uid?(identity.sec_uid.length>24?identity.sec_uid.slice(0,14)+'…'+identity.sec_uid.slice(-6):identity.sec_uid):'正在确认')}</small>${home?`<a href="${escape(home)}" target="_blank" rel="noreferrer">查看主页 ↗</a>`:''}</div><button type="button" class="text-button" data-account-remove="${token}">移除</button></div>`;
    }).join('');
  }
  async function refreshAccounts(){
    clearTimeout(accountTimer);const revision=accountRevision,tokens=savedTokens();
    const results=await Promise.all(tokens.map(async token=>{try{return {token,result:await api('login/status',{login_token:token})}}catch(error){return {token,error}}}));
    if(revision!==accountRevision)return;
    const invalid=new Set();
    for(const {token,result,error} of results){
      if(error){if([404,410].includes(error.status))invalid.add(token);else accountStates.set(token,{...(accountStates.get(token)||{}),verified:false,error:'连接暂时中断，稍后自动重试'})}
      else if(!result.verified||result.expires_at*1000<=Date.now())invalid.add(token);
      else accountStates.set(token,result);
    }
    if(invalid.size){persistAccounts(savedTokens().filter(t=>!invalid.has(t)));for(const t of invalid)accountStates.delete(t);$('report-account-message').textContent='部分账号核验已到期或被移除，请重新添加；其余账号仍可使用。'}
    renderAccounts();if(savedTokens().length)accountTimer=setTimeout(refreshAccounts,60000);
  }
  function rememberAccount(token,result){
    const tokens=savedTokens();
    // Do not let cancellation/removal restore a late completed scan.
    const duplicates=tokens.filter(t=>t!==token&&accountStates.get(t)?.identity?.sec_uid===result.identity.sec_uid);
    const next=tokens.filter(t=>!duplicates.includes(t));if(!next.includes(token))next.push(token);
    persistAccounts(next);accountStates.set(token,result);renderAccounts();
    for(const previous of duplicates){accountStates.delete(previous);api('login/cancel',{login_token:previous}).catch(()=>{});}
    $('report-account-message').textContent=duplicates.length?'此账号已重新核验，有效期已更新。':'账号核验成功。可继续添加其他账号，报送时自动逐条匹配。';
    resetLoginView();clearTimeout(accountTimer);accountTimer=setTimeout(refreshAccounts,60000);
  }
  function showLogin(result){
    clearTimeout(loginTimer);$('report-login-panel').hidden=false;$('report-login-status').textContent=result.message||'';
    if(result.verified){rememberAccount(loginToken,result);return}
    const active=['opening','waiting','cancelling'].includes(result.state);
    $('report-login-start').disabled=active;$('report-login-start').textContent=active?'正在核验…':'重新扫码';
    const qr=$('report-login-qr'),image=result.qr_image&&/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(result.qr_image)?result.qr_image:'';
    if(image){if(qr.getAttribute('src')!==image&&qr.dataset.pending!==image){const token=loginToken,next=new Image();next.onload=()=>{if(token===loginToken&&qr.dataset.pending===image){qr.src=image;qr.hidden=false;delete qr.dataset.pending}};qr.dataset.pending=image;next.src=image}}else{delete qr.dataset.pending;qr.hidden=true;qr.removeAttribute('src')}
    if(active){pendingToken(loginToken);loginTimer=setTimeout(()=>pollLogin(loginToken),2500)}else pendingToken('');
  }
  async function pollLogin(token=loginToken){if(!token||token!==loginToken)return;try{const result=await api('login/status',{login_token:token});if(token===loginToken)showLogin(result)}catch(error){if(token!==loginToken)return;if([404,410].includes(error.status)){resetLoginView();$('report-login-panel').hidden=false;$('report-login-status').textContent=error.message}else{$('report-login-start').disabled=true;$('report-login-status').textContent='核验连接暂时中断，正在重试，无需重复扫码；已保存账号不受影响。';loginTimer=setTimeout(()=>pollLogin(token),5000)}}}
  $('report-login-start').onclick=async()=>{
    if(savedTokens().length>=100){$('report-account-message').textContent='本浏览器最多保存 100 个核验账号，请先移除不再使用的账号。';return}
    const action=++loginAction,previous=loginToken;resetLoginView();
    const button=$('report-login-start');button.disabled=true;$('report-login-panel').hidden=false;$('report-login-status').textContent='正在准备官方扫码窗口…';
    try{
      if(previous)await api('login/cancel',{login_token:previous});
      if(action!==loginAction)return;
      const result=await api('login/start',{});
      if(action!==loginAction){await api('login/cancel',{login_token:result.login_token}).catch(()=>{});return}
      loginToken=result.login_token;showLogin(result);
    }catch(error){if(action!==loginAction)return;$('report-login-status').textContent=error.message;button.disabled=false;if(previous){loginToken=previous;pendingToken(previous);pollLogin(previous)}}
  };
  $('report-login-cancel').onclick=async()=>{
    ++loginAction;const token=loginToken;resetLoginView();
    if(token)try{await api('login/cancel',{login_token:token})}catch{$('report-login-panel').hidden=false;$('report-login-status').textContent='服务器暂时无法取消，请恢复连接后再试。';loginToken=token;pendingToken(token)}
  };
  $('report-account-list').addEventListener('click',async event=>{
    const button=event.target.closest('[data-account-remove]');if(!button)return;const token=button.dataset.accountRemove;
    if(!savedTokens().includes(token))return;
    if(!window.confirm('移除此浏览器的账号核验？以后报送不再使用此账号匹配，历史报送和管理员账号不受影响。'))return;
    button.disabled=true;
    try{await api('login/cancel',{login_token:token});persistAccounts(savedTokens().filter(t=>t!==token));accountStates.delete(token);renderAccounts();refreshAccounts();$('report-account-message').textContent='已移除此账号核验；其他账号可继续使用。'}catch(error){$('report-account-message').textContent=error.message;button.disabled=false}
  });
  window.addEventListener('storage',event=>{
    if(event.key!==accountsKey&&event.key!==rememberedKey&&event.key!==null)return;
    accountRevision++;accountStates=new Map();renderAccounts();refreshAccounts();
  });
  $('report-receipts').addEventListener('input',event=>{if(event.target.dataset.noteInput)noteDrafts[event.target.dataset.noteInput]=event.target.value});
  $('report-receipts').addEventListener('click',async event=>{
    const open=event.target.closest('[data-note-open]');if(open){const id=open.dataset.noteOpen;noteOpen.has(id)?noteOpen.delete(id):noteOpen.add(id);renderReceipts();return}
    const button=event.target.closest('[data-note-save]');if(!button)return;const id=button.dataset.noteSave,receipt=receipts.find(r=>(r.items||[]).some(i=>i.id===id));if(!receipt)return;
    button.disabled=true;const message=$('report-receipts').querySelector(`[data-note-message="${id}"]`);try{const result=await api('note',{receipt_token:receipt.receipt_token,item_id:id,note:noteDrafts[id]??receipt.items.find(i=>i.id===id).reporter_note??''});receipt.items.find(i=>i.id===id).reporter_note=result.note;delete noteDrafts[id];persist();message.textContent=result.message}catch(error){message.textContent=error.message}finally{button.disabled=false}
  });
  init();
  // Migrate the old one-account credential without revoking it.
  try{const legacy=localStorage.getItem(rememberedKey);if(validToken(legacy)){const tokens=savedTokens();if(!tokens.includes(legacy))persistAccounts([...tokens,legacy]);if(savedTokens().includes(legacy))localStorage.removeItem(rememberedKey)}}catch{}
  renderAccounts();refreshAccounts();
  try{const cached=sessionStorage.getItem(loginKey);if(validToken(cached)&&!savedTokens().includes(cached)){loginToken=cached;$('report-login-start').disabled=true;pollLogin(cached)}else pendingToken('')}catch{}
})();
