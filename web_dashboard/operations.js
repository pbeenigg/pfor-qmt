'use strict';

const qualityNames={verified:'校验通过',pending_verification:'待核验',not_published:'等待发布',not_applicable:'不适用',missing:'缺失',rejected:'已拒绝',stale:'已过期'};
const levelNames={info:'信息',warning:'警告',error:'错误'};
let eventNext=null, eventRequest=0, operationsRequest=0, unitJob=null, unitOffset=0, unitNext=null;

async function loadOperations() {
  const request=++operationsRequest;
  const health=await api('/health'), plans=await api('/maintenance');
  if(request!==operationsRequest)return;
  const gib=bytes=>(bytes/1024**3).toFixed(2)+' GB';
  $('#operations-summary').innerHTML=detailFields({数据库:health.database.connected?'已连接':'未连接',数据库大小:gib(health.database_bytes),运行目录可用空间:gib(health.runtime_disk.free_bytes),运行目录空间状态:health.runtime_disk.low?'空间不足，请处理':'充足',数据库卷空间:'未验证（可能位于远端或容器）',活动任务:health.queues.reduce((n,row)=>n+row.count,0),采集质量:health.quality.map(row=>`${row.source} / ${qualityNames[row.quality_state]} ${row.count} 块`).join('；') || '尚无分块记录'});
  $('#attention-count').textContent='最近 '+health.attention.length+' 项';
  $('#attention-rows').innerHTML=health.attention.map(row=>`<tr><td>${escape(row.id.slice(0,8))}<span class="muted">${escape(row.source || 'qmt')}</span></td><td><span class="badge ${row.state}">${statuses[row.state]}</span></td><td class="wrap-text">${escape(row.error_code || '')}<span class="muted">${escape(row.error || '存在待核验或未完成分块')}</span></td><td class="wrap-text">${escape(row.action || '查看分块质量')}</td><td><button type="button" data-job-detail="${row.id}" title="任务详情" aria-label="任务详情">${icon('list')}</button></td></tr>`).join('') || '<tr><td colspan="5" class="empty">暂无待处理任务</td></tr>';
  $('#maintenance-rows').innerHTML=plans.map(row=>`<tr><td>${escape(row.name)}<span class="muted">${escape(row.source)}</span></td><td>${escape(row.schedule_time)}</td><td class="numeric">${row.lookback_days}</td><td>${escape(row.last_date || '尚未排队')}</td><td class="wrap-text">${row.enabled?'已启用':'已停用'}<span class="muted">${escape(row.last_error || '')}</span></td><td><button type="button" data-maintenance-id="${row.id}" data-enabled="${!row.enabled}" title="${row.enabled?'停用维护':'启用维护'}" aria-label="${row.enabled?'停用维护':'启用维护'}">${icon(row.enabled?'pause':'play')}</button>${recordButton('maintenance',plans.indexOf(row))}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无自动维护范围</td></tr>';
  recordSets.maintenance={title:'自动维护范围',rows:plans,labels:{name:'名称',source:'来源',schedule_time:'每日时间',lookback_days:'回读交易日',last_date:'最近排队日期',last_error:'调度错误'},extra:row=>'<dl class="detail-grid">'+detailFields(row.payload)+'</dl>'};
  icons();
}

async function loadEvents(before=null) {
  const request=++eventRequest;
  const result=await api('/events/query',{...values($('#event-filter')),limit:50,...(before?{before}:{})});
  if(request!==eventRequest)return;
  eventNext=result.next_before;
  $('#event-next').disabled=!eventNext;
  recordSets.events={title:'事件详情',rows:result.rows,labels:{created_at:'时间',job_id:'任务ID',run_number:'执行次数',unit_index:'分块序号（从0开始）',level:'级别',code:'事件代码',message:'说明',source:'来源'},extra:row=>'<h3>上下文</h3><dl class="detail-grid">'+detailFields(row.context)+'</dl>'+(row.sample?'<h3>异常样本</h3><pre>'+escape(JSON.stringify(row.sample,null,2))+'</pre>':'')};
  $('#event-rows').innerHTML=result.rows.map((row,index)=>`<tr><td>${formatTime(row.created_at)}<span class="muted">${escape(row.source)}</span></td><td>${escape(row.job_id?.slice(0,8) || '系统')}<span class="muted">${row.unit_index===null?'任务级':'第 '+(row.unit_index+1)+' 块'}</span></td><td><span class="badge ${row.level}">${levelNames[row.level]}</span></td><td class="wrap-text">${escape(row.code)}</td><td class="wrap-text">${escape(row.message)}</td><td>${recordButton('events',index)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">没有匹配的事件</td></tr>';
  icons();
}

async function loadUnits(id,offset=0) {
  const page=await api(`/jobs/${id}/units?limit=50&offset=${offset}`), job=await api('/jobs/'+id);
  unitJob=id;unitOffset=offset;unitNext=page.next_offset;
  $('#unit-position').textContent=page.rows.length?`${offset+1} - ${offset+page.rows.length}`:'旧任务未记录分块质量';
  $('#unit-prev').disabled=!offset;$('#unit-next').disabled=unitNext===null;
  const retryAllowed=['failed','partial','blocked','cancelled'].includes(job.state);
  $('#unit-rows').innerHTML=page.rows.map(row=>`<tr><td>第 ${row.unit_index+1} 块<span class="muted">${escape(row.request.code || row.request.exchange || row.request.name || '')} / ${escape(row.request.period || '')}</span><span class="muted">${escape(row.request.start || '')} ~ ${escape(row.request.end || '')}</span></td><td>${statuses[row.state]}</td><td>${qualityNames[row.quality_state]}</td><td class="numeric">${row.row_count}</td><td class="wrap-text">${row.issues.map(item=>escape(item.code+'：'+item.reason)+'<span class="muted">'+escape(item.action)+'</span>').join('') || '已读回并入库'}</td><td>${retryAllowed && (row.state!=='succeeded' || row.retryable)?`<button type="button" data-retry-unit="${row.unit_index}" data-unit-job="${id}" title="仅重试此分块" aria-label="仅重试此分块">${icon('rotate-cw')}</button>`:''}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">无分块记录，请查看任务原始覆盖结果</td></tr>';
  if(!$('#unit-dialog').open)$('#unit-dialog').showModal();icons();
}

$('#event-filter').addEventListener('submit',action(()=>loadEvents()));
$('#event-filter').addEventListener('reset',()=>setTimeout(()=>action(()=>loadEvents())(),0));
$('#event-next').addEventListener('click',action(()=>loadEvents(eventNext)));
$('#event-first').addEventListener('click',action(()=>loadEvents()));
$('#operations-refresh').addEventListener('click',action(async()=>{await loadOperations();await loadEvents();}));
$('#unit-prev').addEventListener('click',action(()=>loadUnits(unitJob,Math.max(0,unitOffset-50))));
$('#unit-next').addEventListener('click',action(()=>loadUnits(unitJob,unitNext)));
$('#maintenance-form').addEventListener('submit',action(async()=>{
  const payload=values($('#maintenance-form'));payload.lookback_days=Number(payload.lookback_days);
  await api('/maintenance',payload);$('#maintenance-dialog').close();await switchView('operations');notice('维护范围已保存并启用');
}));
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  const d=button.dataset;
  if(!['units','events','maintain','maintenanceId','retryUnit'].some(key=>key in d))return;
  await action(async()=>{
    if(d.units) { $('#record-dialog').close();await loadUnits(d.units); }
    if(d.events) { $('#record-dialog').close();$('#event-filter').reset();$('#event-filter [name=job_id]').value=d.events;await switchView('operations'); }
    if(d.maintain) {
      const job=await api('/jobs/'+d.maintain);$('#record-dialog').close();
      $('#maintenance-form [name=job_id]').value=d.maintain;$('#maintenance-form [name=name]').value=(reportNames[job.payload.resource] || (job.kind==='catalog'?'目录':'行情'))+'维护 '+d.maintain.slice(0,8);
      $('#maintenance-form [name=schedule_time]').value=job.payload.source==='tushare'?'19:00':'17:00';$('#maintenance-dialog').showModal();
    }
    if(d.maintenanceId) { await api('/maintenance/'+d.maintenanceId,{enabled:d.enabled==='true'});await loadOperations(); }
    if(d.retryUnit!==undefined) {
      const job=await api('/jobs/'+d.unitJob+'/retry',{unit_indices:[Number(d.retryUnit)]});$('#unit-dialog').close();notice('定向重试已创建：'+job.id.slice(0,8));await switchView('jobs');
    }
  })(event);
});

for(const select of $$('#event-filter select'))enableMulti(select,'全部');
const jobStates=$('#job-filter [name=states]');
jobStates.innerHTML='<option value="">全部</option>'+Object.entries(statuses).filter(([key])=>key!=='completed').map(([key,label])=>`<option value="${key}">${label}</option>`).join('');
refreshMulti(jobStates);
