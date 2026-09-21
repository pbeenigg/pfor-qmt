'use strict';
const catalogNames = new Map();
const kindNames = { future:'期货', option:'期权', stock:'股票', index:'指数', fund:'场内基金', bond:'债券' };
const marketNames = { IF:'中金所', SF:'上期所', DF:'大商所', ZF:'郑商所', INE:'能源中心', GF:'广期所', SHO:'上证期权', SZO:'深证期权', SH:'上交所', SZ:'深交所', BJ:'北交所' };
const subtypeNames = { contract:'普通合约', continuous:'连续合约', combination:'组合合约', efp:'期转现', etf:'ETF' };
const pickerTargets = {
  quotes: { input:'#quote-form [name="codes"]', label:'#quote-selection', title:'选择行情证券', multiple:true, limit:100 },
  history: { input:'#history-form [name="code"]', label:'#history-selection', title:'选择历史证券', multiple:true },
  report: { input:'#futures-form [name="contract"]', label:'#report-selection', title:'选择持仓排名月份合约', kind:'future', subtype:'contract', multiple:true },
  index: { input:'#index-form [name="code"]', label:'#index-selection', title:'选择指数', kind:'index' },
  dataset: { input:'#dataset-form [name="members"]', label:'#dataset-selection', title:'选择数据集成员', multiple:true },
  diagnostics: { input:'#diagnostic-code', label:'#diagnostic-selection', title:'选择诊断证券或合约' },
};
let pickerTarget, pickerTrigger, pickerSelection, pickerRows = [], pickerOffset = 0, pickerNext = null, pickerRequest = 0, pickerTimer;

function datasetRequiresContracts() {
  return provider==='tushare' && [...$('#dataset-form').querySelectorAll('[name=period]:checked')].some(input=>minutePeriods.includes(input.value));
}

function incompatibleDatasetMembers() {
  return datasetRequiresContracts()?splitCodes($('#dataset-form [name=members]').value).filter(code=>catalogNames.get(code)?.subtype!=='contract'):[];
}

function pickerSubtype() {
  return pickerTarget==='dataset'&&datasetRequiresContracts()?'contract':pickerTargets[pickerTarget].subtype || $('#picker-subtype').value;
}

function updateCatalogFilters() {
  for(const selector of ['#security-form [name=kind]','#security-form [name=market]','#security-form [name=subtype]','#picker-market']){
    const field=$(selector);
    for(const option of field.options){const allowed=provider!=='tushare'||!option.value||(field.name==='kind'?option.value==='future':field.name==='subtype'?['contract','continuous','combination','efp'].includes(option.value):['IF','SF','DF','ZF','INE','GF'].includes(option.value));option.disabled=option.hidden=!allowed;if(!allowed)option.selected=false;}
    refreshMulti(field);
  }
}

function rememberSecurities(rows) {
  rows.forEach((row) => catalogNames.set(row.code, row));
  for (const target of Object.values(pickerTargets)) {
    const selected = splitCodes($(target.input).value);
    $(target.label).textContent = selected.length ? selected.slice(0, 2).map((code) => securityLabel(code)).join('、') + (selected.length > 2 ? ` 等 ${selected.length} 个` : '') : '选择证券';
  }
}

function securityLabel(code) {
  return catalogNames.has(code) ? `${catalogNames.get(code).name} · ${code}` : code;
}

async function resolveSelections() {
  const targets = provider==='tushare' ? [pickerTargets.history,pickerTargets.dataset,pickerTargets.report] : Object.values(pickerTargets).filter(target=>target!==pickerTargets.report);
  const members = [...new Set(targets.flatMap((target) => splitCodes($(target.input).value)))];
  if (members.length) rememberSecurities(await api('/catalog/resolve', { members }));
  else rememberSecurities([]);
}

function pickerCount() {
  const limit=pickerTargets[pickerTarget]?.limit || 10000;
  $('#picker-count').textContent = `已选 ${pickerSelection.size} 个${limit===100?' / 最多订阅100个':''}`;
  $('#picker-apply').disabled = !pickerSelection.size || pickerSelection.size>limit;
  const count = pickerRows.filter((row) => pickerSelection.has(row.code)).length;
  $('#picker-all').checked = pickerRows.length > 0 && count === pickerRows.length;
  $('#picker-all').indeterminate = count > 0 && count < pickerRows.length;
  $('#picker-all').disabled = !pickerRows.length;
}

async function loadPicker(offset = 0) {
  const request = ++pickerRequest;
  $('#picker-error').hidden = true;
  $('#picker-rows').setAttribute('aria-busy', 'true');
  pickerRows = [];
  $('#picker-rows').replaceChildren();
  $('#picker-prev').disabled=true;$('#picker-next').disabled=true;
  pickerCount();
  try {
    const search = $('#picker-search').value.trim();
    const kinds = selectedValues($('#picker-kind')), markets = selectedValues($('#picker-market'));
    const kind = kinds.join(','), market = markets.join(',');
    let result;
    if ($('#picker-selected-only').checked) {
      const subtype=pickerSubtype(),date=today().replaceAll('-','');
      const rows = [...pickerSelection].map((code) => catalogNames.get(code) || {code,name:'目录未收录',kind:''}).filter((row) => (!kinds.length || kinds.includes(row.kind)) && (!markets.length || markets.includes(row.market)) && (!subtype||row.subtype===subtype) && (!$('#picker-active').checked || (!row.metadata?.expiry||row.metadata.expiry>=date)&&(!row.metadata?.listed||row.metadata.listed<=date)) && `${row.code} ${row.name} ${row.metadata?.product||''}`.toLowerCase().includes(search.toLowerCase()));
      result = { rows:rows.slice(offset, offset + 50), total:rows.length, next_offset:offset + 50 < rows.length ? offset + 50 : null };
    } else result = await api('/catalog/securities?' + new URLSearchParams({search, kind, market, subtype:pickerSubtype(), active:$('#picker-active').checked, limit:50, offset}));
    if (request !== pickerRequest || !$('#security-picker').open) return;
    rememberSecurities(result.rows.filter((row) => row.kind));
    pickerRows = result.rows; pickerOffset = offset; pickerNext = result.next_offset;
    const target = pickerTargets[pickerTarget];
    $('#picker-rows').innerHTML = pickerRows.map((row, index) => `<tr><td><input type="${target.multiple ? 'checkbox' : 'radio'}" name="picker-security" value="${escape(row.code)}" aria-label="选择 ${escape(row.name)} ${escape(row.code)}" ${pickerSelection.has(row.code) ? 'checked' : ''}></td><td><label for="picker-row-${index}">${escape(row.code)}</label></td><td>${escape(row.name)}<span class="muted">${escape(subtypeNames[row.subtype] || '')}</span></td><td>${escape(kindNames[row.kind] || '未收录')}</td></tr>`).join('');
    // Native radio/checkbox controls preserve keyboard behavior and accessible names.
    $$('#picker-rows input').forEach((input, index) => { input.id = 'picker-row-' + index; });
    if (!pickerRows.length) empty('#picker-rows', 4, '没有匹配证券');
    $('#picker-page').textContent = `共 ${result.total} 个${pickerRows.length ? ` · ${offset + 1}–${offset + pickerRows.length}` : ''}`;
    $('#picker-prev').disabled = offset === 0;
    $('#picker-next').disabled = pickerNext === null;
    pickerCount();
  } catch (error) {
    if (request === pickerRequest) { $('#picker-error').textContent = error.message; $('#picker-error').hidden = false; }
  } finally { if (request === pickerRequest) $('#picker-rows').removeAttribute('aria-busy'); }
}

function initializePickers() {
  const typeLabel=document.createElement('label');typeLabel.innerHTML='合约类型<select id="picker-subtype"><option value="">全部</option>'+Object.entries(subtypeNames).map(([value,label])=>`<option value="${value}">${label}</option>`).join('')+'</select>';
  $('#picker-search-form button').before(typeLabel);
  $$('[data-kind-filter]').forEach((select) => { select.innerHTML = '<option value="">全部</option>' + Object.entries(kindNames).map(([key, name]) => `<option value="${key}">${name}</option>`).join(''); });
  $$('[data-market-filter]').forEach((select) => { select.innerHTML = '<option value="">全部</option>' + Object.entries(marketNames).map(([key, name]) => `<option value="${key}">${name}</option>`).join(''); });
  rememberSecurities([]);
  $$('[data-picker]').forEach((button) => button.addEventListener('click', async () => {
    pickerTarget = button.dataset.picker; pickerTrigger = button;
    const target = pickerTargets[pickerTarget];
    pickerSelection = new Set(splitCodes($(target.input).value));
    $('#picker-title').textContent = target.title;
    const contractOnly=pickerTarget==='dataset'&&datasetRequiresContracts();
    $('#picker-subtype').value=contractOnly?'contract':target.subtype || '';
    $('#picker-subtype').disabled=contractOnly||Boolean(target.subtype);
    $('#picker-constraint').hidden=!contractOnly;
    $('#picker-constraint').textContent=contractOnly?'分钟周期仅可选择具体月份合约；主力、连续与组合合约不包含在全选范围中。':'';
    $('#picker-search').value = '';
    $('#picker-kind').value = target.kind || (provider==='tushare'?'future':'');
    $('#picker-market').value = '';
    if(pickerTarget==='report'){
      $('#picker-title').textContent='选择'+(reportNames[futuresActive] || '期货资料')+'月份合约';
      const selected=selectedValues($('#futures-form [name=exchange]')).map(exchange=>exchangeMarkets[exchange]);
      [...$('#picker-market').options].forEach(option=>{option.selected=selected.includes(option.value);});
    }
    $('#picker-kind').disabled = !!target.kind || provider==='tushare';
    updateCatalogFilters();
    refreshMulti($('#picker-kind'));refreshMulti($('#picker-market'));
    $('#picker-selected-only').checked = false;
    $('#picker-select-matched').disabled=false;
    $('#picker-active').checked=provider==='tushare';
    $('#picker-all-label').hidden = !target.multiple;
    $('#picker-select-matched').hidden = !target.multiple;
    $('#security-picker').showModal();
    $('#picker-search').focus();
    pickerCount();
    await loadPicker();
  }));
  $('#security-picker').addEventListener('close', () => { ++pickerRequest; clearTimeout(pickerTimer); pickerTrigger?.focus(); });
  $('#security-picker').addEventListener('keydown', (event) => { if (event.key === 'Escape') { event.preventDefault(); $('#security-picker').close(); } });
  $('#picker-close').addEventListener('click', () => $('#security-picker').close());
  $('#picker-search-form').addEventListener('submit', (event) => { event.preventDefault(); clearTimeout(pickerTimer); loadPicker(); });
  $('#picker-search').addEventListener('input', () => { clearTimeout(pickerTimer); ++pickerRequest; pickerTimer = setTimeout(() => loadPicker(), 200); });
  $('#picker-kind').addEventListener('change', () => loadPicker());
  $('#picker-market').addEventListener('change', () => loadPicker());
  $('#picker-subtype').addEventListener('change', () => loadPicker());
  $('#picker-selected-only').addEventListener('change', () => {$('#picker-select-matched').disabled=$('#picker-selected-only').checked;loadPicker();});
  $('#picker-active').addEventListener('change', () => loadPicker());
  $('#picker-prev').addEventListener('click', () => loadPicker(Math.max(0, pickerOffset - 50)));
  $('#picker-next').addEventListener('click', () => loadPicker(pickerNext));
  $('#picker-clear').addEventListener('click', () => { pickerSelection.clear(); loadPicker(); });
  $('#picker-select-matched').addEventListener('click',async()=>{
    if($('#picker-selected-only').checked)return;
    const request=++pickerRequest, target=pickerTargets[pickerTarget], button=$('#picker-select-matched');
    button.disabled=true;$('#picker-error').hidden=true;
    try{
      const result=await api('/catalog/select',{source:provider,search:$('#picker-search').value.trim(),kind:selectedValues($('#picker-kind')),market:selectedValues($('#picker-market')),subtype:pickerSubtype(),active:$('#picker-active').checked});
      if(request!==pickerRequest || !$('#security-picker').open)return;
      const combined=new Set([...pickerSelection,...result.rows.map(row=>row.code)]);
      if(combined.size>(target.limit || 10000))throw new Error(`选中范围共${combined.size}个，当前操作最多${target.limit || 10000}个；请缩小筛选范围，未截断选择`);
      pickerSelection=combined;rememberSecurities(result.rows);await loadPicker(pickerOffset);
    }catch(error){if(request===pickerRequest){$('#picker-error').textContent=error.message;$('#picker-error').hidden=false;}}
    finally{button.disabled=false;}
  });
  $('#picker-all').addEventListener('change', (event) => {
    const checked = event.target.checked;
    pickerRows.forEach((row) => checked ? pickerSelection.add(row.code) : pickerSelection.delete(row.code));
    $$('#picker-rows input').forEach((input) => { input.checked = checked; });
    pickerCount();
  });
  $('#picker-rows').addEventListener('change', (event) => {
    if (!event.target.matches('input')) return;
    if (!pickerTargets[pickerTarget].multiple) pickerSelection.clear();
    if (event.target.checked) pickerSelection.add(event.target.value); else pickerSelection.delete(event.target.value);
    pickerCount();
  });
  $('#picker-apply').addEventListener('click', () => {
    if (!pickerSelection.size) return;
    $(pickerTargets[pickerTarget].input).value = [...pickerSelection].join(',');
    if (pickerTarget === 'dataset') state.snapshot = null;
    rememberSecurities([]);
    if (pickerTarget === 'index') suggestSector();
    if (pickerTarget === 'report') clearFuturesResults();
    if (pickerTarget === 'history') clearHistoryResults();
    $(pickerTargets[pickerTarget].input).dispatchEvent(new Event('change',{bubbles:true}));
    $('#security-picker').close();
  });
  $('#picker-catalog').addEventListener('click', async () => {
    $('#security-picker').close();
    await switchView('market');
    $('#catalog-form').scrollIntoView({block:'center'});
    $('#catalog-form button[type="submit"]').focus();
  });
}
