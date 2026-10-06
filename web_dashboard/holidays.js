'use strict';

window.ExchangeHolidays = (() => {
  const kindNames={closed:'全天休市',open:'开市',night_closed:'当晚停开夜盘',night_open:'当晚恢复夜盘'};
  let exchanges=[],applied=null,offset=0,next=null,request=0,jobOffset=0,planOffset=0;
  const label=id=>exchanges.find(e=>e.id===id)?.name || id;
  const range=()=>({exchanges:selectedValues($('#holiday-exchanges')),years:selectedValues($('#holiday-years')).map(Number),versions:$('#holiday-versions').checked});
  function setup(section){
    section.innerHTML=`<div class="section-heading"><h2>交易所休市公告</h2><span class="source">交易所官方公告 · PostgreSQL</span></div>
      <form id="holiday-filter" class="toolbar"><label>交易所<select id="holiday-exchanges" name="exchanges" multiple></select></label><label>安排年度<select id="holiday-years" name="years" multiple></select></label><label class="inline-label"><input id="holiday-versions" type="checkbox">包含历史版本</label><button type="submit">${icon('search')}查询</button><button type="reset" class="icon" title="重置筛选" aria-label="重置公告筛选">${icon('rotate-ccw')}</button><button id="holiday-sync" type="button">${icon('refresh-cw')}同步所选范围</button><button id="holiday-plan-new" type="button">${icon('calendar-clock')}保存自动同步</button></form>
      <p id="holiday-query-state" class="query-state" role="status"></p>
      <div class="table-wrap"><table><thead><tr><th>交易所</th><th>日期</th><th>安排</th><th>公告与版本</th><th>发布时间</th><th>核验</th><th>详情</th></tr></thead><tbody id="holiday-rows"></tbody></table></div>
      <div id="holiday-pager" class="pagination"><span></span><button id="holiday-prev" class="icon" title="上一页公告" aria-label="上一页公告">${icon('chevron-left')}</button><button id="holiday-next" class="icon" title="下一页公告" aria-label="下一页公告">${icon('chevron-right')}</button></div>
      <div class="section-heading"><h2>最近同步</h2><button id="holiday-refresh" class="icon" title="刷新同步结果" aria-label="刷新同步结果">${icon('refresh-cw')}</button></div><div class="table-wrap"><table><thead><tr><th>交易所 / 年度</th><th>状态</th><th>最近尝试</th><th>最近成功</th><th>结果</th></tr></thead><tbody id="holiday-status"></tbody></table></div>
      <div class="section-heading"><h2>公告同步任务</h2></div><div class="table-wrap"><table><thead><tr><th>任务</th><th>范围</th><th>状态</th><th>进度</th><th>操作</th></tr></thead><tbody id="holiday-jobs"></tbody></table></div><div id="holiday-job-pager" class="pagination"></div>
      <div class="section-heading"><h2>公告自动同步</h2></div><form id="holiday-plan-filter" class="toolbar"><label>记录<select name="trash"><option value="active">工作列表</option><option value="deleted">回收站</option></select></label><button type="submit">查询</button></form><div class="table-wrap"><table><thead><tr><th>名称</th><th>固定范围</th><th>时间</th><th>启用</th><th>操作</th></tr></thead><tbody id="holiday-plans"></tbody></table></div><div id="holiday-plan-pager" class="pagination"></div>`;
    const dialog=document.createElement('dialog');dialog.id='holiday-plan-dialog';dialog.innerHTML=`<form id="holiday-plan-form"><div class="picker-heading"><h2>保存公告自动同步</h2><button type="button" class="icon" data-close-dialog title="关闭" aria-label="关闭公告维护">${icon('x')}</button></div><p id="holiday-plan-scope"></p><label>名称<input name="name" required maxlength="100" value="期货交易所休市公告"></label><label>每天同步时间<input name="schedule_time" type="time" required value="08:00"></label><label class="inline-label"><input name="enabled" type="checkbox" checked>启用</label><div class="picker-footer"><button type="button" data-close-dialog>取消</button><button class="primary" type="submit">${icon('save')}保存</button></div></form>`;document.body.append(dialog);
    $('#holiday-filter').onsubmit=action(()=>load(true));
    $('#holiday-filter').addEventListener('reset',()=>setTimeout(()=>load(true).catch(e=>notice(e.message)),0));
    $('#holiday-sync').onclick=action(async()=>{const p=range();if(!p.exchanges.length||!p.years.length)throw new Error('请选择交易所与年度');if(!await confirmBatch('同步交易所休市公告',{交易所:p.exchanges.map(label).join('、'),安排年度:p.years.join('、')}))return;const job=await api('/exchange-holidays/sync',p);notice('公告同步已排队：'+job.id.slice(0,8));await loadJobs();});
    let draft;
    $('#holiday-plan-new').onclick=action(()=>{draft=range();if(!draft.exchanges.length||!draft.years.length)throw new Error('请选择交易所与年度');$('#holiday-plan-scope').textContent=draft.exchanges.map(label).join('、')+' · '+draft.years.join('、');$('#holiday-plan-dialog').showModal();});
    $('#holiday-plan-form').onsubmit=action(async()=>{const form=$('#holiday-plan-form');await api('/exchange-holidays/maintenance',{...draft,name:form.elements.name.value,schedule_time:form.elements.schedule_time.value,enabled:form.elements.enabled.checked});$('#holiday-plan-dialog').close();await loadPlans();notice('公告维护范围已保存');});
    $('#holiday-prev').onclick=action(async()=>{offset=Math.max(0,offset-50);await load();});$('#holiday-next').onclick=action(async()=>{offset=next;await load();});
    $('#holiday-refresh').onclick=action(()=>load());
    $('#holiday-plan-filter').onsubmit=action(async()=>{planOffset=0;await loadPlans();});
    section.addEventListener('click',event=>{
      if(!event.target.closest('[data-notice],[data-holiday-job],[data-holiday-action],[data-plan-action]'))return;
      action(async event=>{
      const button=event.target.closest('button');if(!button)return;
      if(button.dataset.notice){const row=await api('/exchange-holidays/notices/'+button.dataset.notice);recordSets.holiday={title:'公告版本详情',rows:[row],labels:{title:'标题',exchange:'交易所',url:'官方原文',published_at:'发布日期',body:'公告正文',content_hash:'版本摘要',parse_state:'解析状态',parse_detail:'解析结果',fetched_at:'首次保存',checked_at:'最近确认',is_current:'当前版本',parser_version:'解析规则',versions:'历史版本'},note:'交易所官方公告 · 未列日期保持未知'};showRecord('holiday',0);}
      if(button.dataset.holidayJob)await Workspace.navigate('task/'+button.dataset.holidayJob);
      if(button.dataset.holidayAction){await api('/jobs/'+button.dataset.id+'/'+button.dataset.holidayAction,{});await loadJobs();}
      if(button.dataset.planAction){const id=button.dataset.id,revision=Number(button.dataset.revision),operation=button.dataset.planAction;
        if(operation==='delete'&&!await confirmBatch('移入回收站',{名称:button.dataset.name}))return;
        await api('/exchange-holidays/maintenance/'+id+(operation==='toggle'?'':'/'+operation),operation==='toggle'?{revision,enabled:button.dataset.enabled!=='true'}:{revision});await loadPlans();}
      })(event);
    });
  }
  async function initialize(){
    if(exchanges.length)return;
    const options=await api('/exchange-holidays/options');exchanges=options.exchanges;
    $('#holiday-exchanges').innerHTML=exchanges.map(e=>`<option value="${e.id}" selected>${escape(e.name)}</option>`).join('');
    const years=[...new Set([2025,options.year-2,options.year-1,options.year,options.year+1])].sort();
    $('#holiday-years').innerHTML=years.map(y=>`<option value="${y}" ${y===options.year?'selected':''}>${y}</option>`).join('');
    enableMulti($('#holiday-exchanges'),'请选择交易所',true);enableMulti($('#holiday-years'),'请选择年度',true);
  }
  async function load(apply=false){
    await initialize();if(apply||!applied){applied=range();offset=0;}
    const id=++request;$('#holiday-query-state').textContent='正在查询';
    try{const result=await api('/exchange-holidays/query',{...applied,offset});if(id!==request)return;
      $('#holiday-query-state').textContent='已查询 · '+applied.exchanges.map(label).join('、')+' · '+applied.years.join('、')+' · 共 '+result.total+' 项安排';
      $('#holiday-rows').innerHTML=result.rows.map(r=>`<tr><td data-label="交易所">${escape(label(r.exchange))}</td><td data-label="日期">${escape(r.start_day || '未识别')}<span class="muted">${r.end_day!==r.start_day?escape(r.end_day):''}</span></td><td data-label="安排">${kindNames[r.kind] || '待核验'}</td><td data-label="公告与版本"><a href="${escape(r.url)}" target="_blank" rel="noopener noreferrer">${escape(r.title)}</a><span class="muted">${r.content_hash.slice(0,12)} · ${r.is_current?'当前':'历史'}</span></td><td data-label="发布时间">${escape(r.published_at || '未提供')}</td><td data-label="核验">${r.parse_state==='parsed'?'语句已解析':'待核验'}</td><td><button type="button" class="icon" data-notice="${r.id}" title="公告版本详情" aria-label="公告版本详情">${icon('file-text')}</button></td></tr>`).join('') || '<tr><td colspan="7" class="empty">没有已保存公告</td></tr>';
      next=result.next_offset;$('#holiday-pager span').textContent=`共 ${result.total} 项 · ${result.rows.length?offset+1:0} 至 ${offset+result.rows.length}`;$('#holiday-prev').disabled=offset===0;$('#holiday-next').disabled=next===null;
      const statusesByKey=new Map(result.status.map(r=>[r.exchange+':'+r.year,r]));
      $('#holiday-status').innerHTML=applied.exchanges.flatMap(e=>applied.years.map(y=>{const r=statusesByKey.get(e+':'+y);return `<tr><td data-label="交易所 / 年度">${escape(label(e))} / ${y}</td><td data-label="状态">${r?statuses[r.state]:'尚未同步'}</td><td data-label="最近尝试">${formatTime(r?.attempted_at)}</td><td data-label="最近成功">${formatTime(r?.succeeded_at)}</td><td data-label="结果">${escape(r?.detail || '未知日期未推断')}</td></tr>`;})).join('');
      await Promise.all([loadJobs(),loadPlans()]);icons();
    }catch(error){if(id===request)$('#holiday-query-state').textContent='查询失败：'+error.message;throw error;}
  }
  function pager(selector,result,current,update){
    const el=$(selector);el.replaceChildren();const span=document.createElement('span');span.textContent='共 '+result.total+' 项';el.append(span);
    for(const [name,value,glyph] of [['上一页',current?Math.max(0,current-50):null,'chevron-left'],['下一页',result.next_offset,'chevron-right']]){const button=document.createElement('button');button.type='button';button.className='icon';button.title=button.ariaLabel=name;button.innerHTML=icon(glyph);button.disabled=value===null;button.onclick=action(()=>update(value));el.append(button);}
  }
  async function loadJobs(){
    const result=await api('/exchange-holidays/jobs',{offset:jobOffset});
    $('#holiday-jobs').innerHTML=result.rows.map(j=>`<tr><td data-label="任务">${j.id.slice(0,8)}<span class="muted">${formatTime(j.created_at)}</span></td><td data-label="范围">${escape(j.payload.exchanges.map(label).join('、'))}<span class="muted">${j.payload.years.join('、')}</span></td><td data-label="状态"><span class="badge ${j.state}">${statuses[j.state]}</span></td><td data-label="进度">${j.checkpoint} / ${j.total_chunks}</td><td><button type="button" class="icon" data-holiday-job="${j.id}" title="任务详情" aria-label="公告任务详情">${icon('list-checks')}</button>${['queued','running','retrying'].includes(j.state)?`<button type="button" class="icon" data-holiday-action="cancel" data-id="${j.id}" title="取消后续处理" aria-label="取消公告任务">${icon('square')}</button>`:['failed','partial','blocked','cancelled'].includes(j.state)?`<button type="button" class="icon" data-holiday-action="retry" data-id="${j.id}" title="重试未完成范围" aria-label="重试公告任务">${icon('rotate-cw')}</button>`:''}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">没有公告同步任务</td></tr>';
    pager('#holiday-job-pager',result,jobOffset,async value=>{jobOffset=value;await loadJobs();icons();});
  }
  async function loadPlans(){
    const result=await api('/exchange-holidays/maintenance/query',{offset:planOffset,trash:$('#holiday-plan-filter [name=trash]').value});
    $('#holiday-plans').innerHTML=result.rows.map(p=>{const attrs=`data-id="${p.id}" data-revision="${p.revision}" data-name="${escape(p.name)}"`;return `<tr><td data-label="名称">${escape(p.name)}</td><td data-label="固定范围">${escape(p.payload.exchanges.map(label).join('、'))}<span class="muted">${p.payload.years.join('、')}</span></td><td data-label="时间">${escape(p.schedule_time)}</td><td data-label="启用">${p.enabled?'已启用':'已停用'}</td><td>${p.deleted_at?`<button class="icon" ${attrs} data-plan-action="restore" title="恢复（保持停用）" aria-label="恢复公告维护">${icon('undo-2')}</button>`:`<button class="icon" ${attrs} data-plan-action="toggle" data-enabled="${p.enabled}" title="${p.enabled?'停用':'启用'}" aria-label="${p.enabled?'停用':'启用'}公告维护">${icon(p.enabled?'pause':'play')}</button><button class="icon" ${attrs} data-plan-action="delete" title="移入回收站" aria-label="删除公告维护">${icon('trash-2')}</button>`}</td></tr>`;}).join('')||'<tr><td colspan="5" class="empty">没有保存的公告维护范围</td></tr>';
    pager('#holiday-plan-pager',result,planOffset,async value=>{planOffset=value;await loadPlans();icons();});
  }
  return {setup,load};
})();
