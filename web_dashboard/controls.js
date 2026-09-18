'use strict';

const selectedValues = select => [...select.selectedOptions].filter(option=>option.value && !option.disabled).map(option=>option.value);
let optionSelect, optionDraft, optionTrigger, batchResolve, recordContext;
const recordSets = {};

function refreshMulti(select) {
  if (!select?.dataset.multi) return;
  const button = document.querySelector(`[data-multi-for="${select.id}"]`);
  if (!button) return;
  const options = [...select.selectedOptions].filter(option=>option.value && !option.disabled);
  button.querySelector('span').textContent = options.length ? options[0].textContent : select.dataset.empty || '请选择';
  button.querySelector('small').textContent=options.length>1?options.length+' 项':'';
  button.disabled = select.disabled;
  button.title = options.map(option=>option.textContent).join('、') || select.dataset.empty || '请选择';
}

function enableMulti(select, emptyLabel='请选择', required=false) {
  select.id ||= (select.form?.id || 'filter')+'-'+select.name;
  select.multiple = true;
  select.dataset.multi = 'true'; select.dataset.empty = emptyLabel; select.dataset.required = String(required);
  // Keep the native field for FormData and validation; the dialog edits a draft until confirmed.
  select.classList.add('native-multi'); select.tabIndex=-1; select.setAttribute('aria-hidden','true'); select.required=false;
  if (!document.querySelector(`[data-multi-for="${select.id}"]`)) {
    const button=document.createElement('button'); button.type='button';button.className='selection-button multi-trigger';button.dataset.multiFor=select.id;
    button.setAttribute('aria-haspopup','dialog');button.setAttribute('aria-controls','option-picker');
    button.setAttribute('aria-label',select.parentElement.firstChild.textContent.trim() || select.name);
    button.innerHTML='<span></span><small class="selection-count"></small>'+icon('chevron-down');select.after(button);
    select.addEventListener('change',()=>refreshMulti(select));
    new MutationObserver(()=>refreshMulti(select)).observe(select,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled','selected','label']});
  }
  refreshMulti(select);
}

function optionRows() {
  const search=$('#option-search').value.trim().toLowerCase();
  return [...optionSelect.options].filter(option=>option.value && !option.hidden && option.textContent.toLowerCase().includes(search));
}

function renderOptions() {
  const focused=$('#option-rows').contains(document.activeElement)?document.activeElement.value:null;
  const rows=optionRows(), available=rows.filter(option=>!option.disabled), count=available.filter(option=>optionDraft.has(option.value)).length;
  $('#option-rows').innerHTML=rows.map((option,index)=>`<label class="option-row ${option.disabled?'unavailable':''}"><input type="checkbox" value="${escape(option.value)}" ${optionDraft.has(option.value)?'checked':''} ${option.disabled?'disabled':''}><span>${escape(option.textContent)}</span>${option.disabled?'<small>当前不可用</small>':''}</label>`).join('') || '<p class="empty-state">没有匹配选项</p>';
  $('#option-all').checked=available.length>0 && count===available.length;
  $('#option-all').indeterminate=count>0 && count<available.length;$('#option-all').disabled=!available.length;
  $('#option-count').textContent=`已选 ${optionDraft.size} 项 / 匹配 ${rows.length} 项`;
  $('#option-apply').disabled=optionSelect.dataset.required==='true' && !optionDraft.size;
  if(focused)$('#option-rows').querySelector(`input[value="${CSS.escape(focused)}"]`)?.focus();
}

function shanghaiDay(offset=0) {
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
  const values=Object.fromEntries(parts.map(part=>[part.type,part.value]));
  const day=new Date(`${values.year}-${values.month}-${values.day}T00:00:00Z`);day.setUTCDate(day.getUTCDate()+offset);
  return day.toISOString().slice(0,10);
}

function dateRangeFor(preset, end=shanghaiDay()) {
  const date=new Date(end+'T00:00:00Z');
  const days={today:0,'3d':2,'1w':6}, months={'1m':1,'3m':3,'6m':6,'1y':12,'2y':24,'3y':36};
  if (preset in days) date.setUTCDate(date.getUTCDate()-days[preset]);
  else if (preset in months) {
    const original=date.getUTCDate();date.setUTCDate(1);date.setUTCMonth(date.getUTCMonth()-months[preset]);
    const last=new Date(Date.UTC(date.getUTCFullYear(),date.getUTCMonth()+1,0)).getUTCDate();date.setUTCDate(Math.min(original,last));
  } else throw new Error('无效日期范围');
  return {start:date.toISOString().slice(0,10),end};
}

async function confirmBatch(title, summary) {
  const dialog=$('#batch-confirm');
  if(dialog.open) return false;
  $('#batch-title').textContent=title;
  $('#batch-summary').innerHTML=detailFields(summary);
  dialog.returnValue='';dialog.showModal();
  return new Promise(resolve=>{batchResolve=resolve;});
}

function detailFields(entries) {
  return Object.entries(entries).map(([label,value])=>`<dt>${escape(label)}</dt><dd>${escape(value===null || value===undefined?'未提供':typeof value==='object'?JSON.stringify(value):value)}</dd>`).join('');
}

function recordButton(kind,index) {
  return `<button type="button" class="icon record-button" data-record="${kind}" data-record-index="${index}" title="查看记录详情" aria-label="查看记录详情">${icon('file-search')}</button>`;
}

function showRecord(kind,index=0) {
  const group=recordSets[kind];if(!group?.rows[index])return;
  recordContext={kind,index};const row=group.rows[index];
  $('#record-title').textContent=group.title;
  $('#record-position').textContent=`本页 ${index+1} / ${group.rows.length}`;
  $('#record-note').textContent=group.note || '';
  $('#record-fields').innerHTML=detailFields(Object.fromEntries(Object.entries(row).filter(([key,value])=>typeof value!=='object' || value===null).map(([key,value])=>[group.labels?.[key] || key,value])));
  $('#record-extra').innerHTML=group.extra?.(row) || '';
  $('#record-raw').textContent=JSON.stringify(row,null,2);
  $('#record-prev').disabled=index===0;$('#record-next').disabled=index===group.rows.length-1;
  if(!$('#record-dialog').open)$('#record-dialog').showModal();icons();
}

function initializeControls() {
  const catalogLabel=document.createElement('label');catalogLabel.id='catalog-exchanges';catalogLabel.hidden=provider!=='tushare';catalogLabel.textContent='同步交易所';
  const catalogSelect=document.createElement('select');catalogSelect.name='exchanges';
  catalogSelect.multiple=true;
  catalogSelect.innerHTML=Object.entries(exchangeMarkets).map(([exchange,market])=>`<option value="${exchange}" selected>${marketNames[market]}</option>`).join('');
  catalogLabel.append(catalogSelect);$('#catalog-form fieldset').after(catalogLabel);enableMulti(catalogSelect,'请选择',true);
  for(const selector of ['#security-form [name=kind]','#security-form [name=market]','#security-form [name=subtype]','#picker-kind','#picker-market','#job-filter [name=states]','#job-filter [name=kinds]','#job-filter [name=sources]']) enableMulti($(selector),'全部');
  for(const selector of ['#history-form [name=period]','#download-form [name=dataset_id]','#futures-form [name=resource]','#futures-form [name=exchange]','#futures-form [name=symbol]','#futures-form [name=code]']) enableMulti($(selector),'请选择',true);
  for(const id of ['history-form','download-form','futures-form','job-filter','event-filter']) {
    const form=$('#'+id), start=form.elements.start, end=form.elements.end;
    const range=document.createElement('div');range.className='date-range';
    start.parentElement.before(range);
    const label=document.createElement('label');label.textContent='日期范围';
    const presets=document.createElement('select');presets.dataset.datePreset=id;presets.setAttribute('aria-label','日期快捷范围');
    presets.innerHTML=Object.entries({'':'自定义',today:'今天','3d':'近三天','1w':'近一周','1m':'近一个月','3m':'近三个月','6m':'近六个月','1y':'近一年','2y':'近两年','3y':'近三年'}).map(([value,text])=>`<option value="${value}">${text}</option>`).join('');
    label.append(presets);range.append(label,start.parentElement,end.parentElement);
    presets.addEventListener('change',()=>{if(!presets.value)return;const dates=dateRangeFor(presets.value);start.value=dates.start;end.value=dates.end;form.dispatchEvent(new Event('change',{bubbles:true}));});
    for(const input of [start,end]) input.addEventListener('input',()=>{presets.value='';});
  }
  for(const fieldset of $$('#catalog-form fieldset,#dataset-form fieldset,#download-form fieldset')) {
    fieldset.insertAdjacentHTML('beforeend','<label class="inline-label select-all-control"><input type="checkbox" data-check-all>全选可用项</label>');
    fieldset.querySelector('[data-check-all]').addEventListener('change',event=>{
      fieldset.querySelectorAll('input[name]:not(:disabled)').forEach(input=>{input.checked=event.target.checked;});
      fieldset.dispatchEvent(new Event('change',{bubbles:true}));
    });
    fieldset.addEventListener('change',()=>refreshCheckAll(fieldset));
  }
  document.addEventListener('click',event=>{
    const close=event.target.closest('[data-close-dialog]');if(close)close.closest('dialog').close();
    const trigger=event.target.closest('[data-multi-for]');
    if(trigger){optionTrigger=trigger;optionSelect=$('#'+trigger.dataset.multiFor);optionDraft=new Set(selectedValues(optionSelect));$('#option-title').textContent=trigger.getAttribute('aria-label');$('#option-search').value='';renderOptions();$('#option-picker').showModal();$('#option-search').focus();}
    const record=event.target.closest('[data-record]');if(record)showRecord(record.dataset.record,Number(record.dataset.recordIndex));
  });
  $('#option-search').addEventListener('input',renderOptions);
  $('#option-picker').addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();$('#option-picker').close();}});
  $('#option-picker').addEventListener('close',()=>optionTrigger?.focus());
  $('#record-dialog').addEventListener('close',()=>{$('#record-dialog details').open=false;});
  $('#option-all').addEventListener('change',event=>{optionRows().filter(option=>!option.disabled).forEach(option=>event.target.checked?optionDraft.add(option.value):optionDraft.delete(option.value));renderOptions();});
  $('#option-clear').addEventListener('click',()=>{optionDraft.clear();renderOptions();});
  $('#option-rows').addEventListener('change',event=>{if(event.target.matches('input')){event.target.checked?optionDraft.add(event.target.value):optionDraft.delete(event.target.value);renderOptions();}});
  $('#option-apply').addEventListener('click',()=>{[...optionSelect.options].forEach(option=>{option.selected=!option.disabled && optionDraft.has(option.value);});optionSelect.dispatchEvent(new Event('change',{bubbles:true}));refreshMulti(optionSelect);$('#option-picker').close();});
  $('#batch-submit').addEventListener('click',()=>$('#batch-confirm').close('submit'));
  $('#batch-confirm').addEventListener('close',()=>{batchResolve?.($('#batch-confirm').returnValue==='submit');batchResolve=null;});
  $('#record-prev').addEventListener('click',()=>showRecord(recordContext.kind,recordContext.index-1));
  $('#record-next').addEventListener('click',()=>showRecord(recordContext.kind,recordContext.index+1));
  $('#record-copy').addEventListener('click',action(async()=>{await navigator.clipboard.writeText($('#record-raw').textContent);$('#record-copy').title='已复制';}));
  $$('.table-wrap').forEach(wrap=>{wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','数据表格');});
  refreshCheckAll();icons();
  // Reset's default action runs after event dispatch; refresh on the next task.
  document.addEventListener('reset',()=>setTimeout(()=>{$$('select[data-multi]').forEach(refreshMulti);refreshCheckAll();},0));
}

function refreshCheckAll(only) {
  for(const fieldset of only?[only]:$$('fieldset')){
    const all=fieldset.querySelector('[data-check-all]');if(!all)continue;
    const inputs=[...fieldset.querySelectorAll('input[name]:not(:disabled)')],count=inputs.filter(input=>input.checked).length;
    all.checked=inputs.length>0 && count===inputs.length;all.indeterminate=count>0&&count<inputs.length;all.disabled=!inputs.length;
  }
}
