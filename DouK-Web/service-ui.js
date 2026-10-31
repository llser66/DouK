function apiServicePanel() {
  const s=state.service;
  return `<section class="panel"><div class="panel-header"><span class="panel-title">${icon('zap')}后台采集服务</span><span class="status-chip ${s?.ready?'ok':''}">${s?.ready?'已就绪':s?.state==='starting'?'启动中':'未启动'}</span></div><div class="panel-body"><p class="api-state" id="service-message" role="status">${esc(s?.message||'正在检查采集服务…')}</p><p class="muted">账号 Cookie 与后台采集服务分别连接。服务就绪后，下载、评论采集和直播等功能才能获取平台数据。</p><div class="result-actions"><button type="button" class="primary" data-start-service ${s?.ready||s?.state==='starting'?'disabled':''}>${icon('play')}重新连接后台服务</button><button type="button" class="secondary" data-check-service>${icon('refresh')}检查状态</button></div></div></section>`;
}

async function requestServiceStart() {
  toast('正在连接后台服务…');
  state.status={...state.status,...await post('/ensure-service',{})};
  await refresh();remember();render();toast(state.status.connection_message);
}

function bindServiceUI() {
  const form=document.querySelector('#collect-form');if(!form)return;
  const mode=form.elements.mode;
  const options=document.createElement('div');options.className='comment-reply-options';
  options.innerHTML=`<div class="field"><label for="pool-batch-size">每个账号每批采集条数</label><input id="pool-batch-size" name="batch_size" type="number" min="1" max="50" value="20"><p>每批完成后轮换账号，按平台实际返回数量保存。</p></div><label class="comment-reply"><input type="checkbox" name="collect_all">持续采集到没有更多评论</label><p class="muted">取消勾选则按上方「最大采集页数」停止，页数在所有账号之间合计。</p><label class="comment-reply section-spacer"><input type="checkbox" name="reply">包含评论回复（楼中楼）</label><div class="field section-spacer"><label for="reply-count">每条评论的回复数量</label><input id="reply-count" name="count_reply" type="number" min="1" max="100" value="3"><p>回复同样按批使用账号池。实际数量取决于平台返回。</p></div>`;
  const draft=state.drafts.collect||{};
  options.querySelector('[name=reply]').checked=!!draft.reply;
  options.querySelector('[name=count_reply]').value=draft.count_reply||3;
  options.querySelector('[name=batch_size]').value=draft.batch_size||20;
  options.querySelector('[name=collect_all]').checked=Object.keys(draft).length?draft.collect_all==='on':true;
  form.querySelector('.notice').before(options);
  const update=()=>{const comment=mode.value==='comment';options.hidden=!comment;options.querySelectorAll('input').forEach(input=>input.disabled=!comment);form.elements.pages.disabled=comment&&options.querySelector('[name=collect_all]').checked};
  mode.addEventListener('change',update);options.querySelector('[name=collect_all]').addEventListener('change',update);update();
}

function initServiceUI() {
  document.addEventListener('click',async e=>{
    const b=e.target.closest('button');if(!b)return;
    try{
      if(b.hasAttribute('data-start-service'))await requestServiceStart();
      if(b.hasAttribute('data-check-service')){state.service=await api('/service');await post('/connect',{});await refresh();remember();render();toast(state.service.message)}
    }catch(err){toast(err.message)}
  });
  let busy=false;
  setInterval(async()=>{
    if(busy||state.service?.state!=='starting')return;busy=true;
    try{state.service=await api('/service');const message=document.querySelector('#service-message');if(message)message.textContent=state.service.message;
      if(state.service.state!=='starting'){await post('/connect',{});await refresh();remember();render();toast(state.service.message)}
    }catch(err){toast(err.message)}finally{busy=false}
  },2000);
}
