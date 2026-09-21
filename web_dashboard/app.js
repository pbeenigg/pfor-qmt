'use strict';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { view: 'market', indices: [], datasets: [], jobs: [], quotes: {}, socket: null, offset: 0, next: null, snapshot: null, authenticated: false, reconnect: null, connecting: false, watch: [] };
let catalogTimer, catalogJob, catalogSectors = [], securityOffset = 0, securityNext = null;
const names = { market: '行情', indices: '指数', boards:'行业概念', history: '历史库', futures:'期货资料', jobs: '下载任务', operations:'运行与日志', settings: '数据源设置' };
const periodNames = {'1d':'日线','1w':'周线','1mo':'月线','1m':'1 分钟','5m':'5 分钟','15m':'15 分钟','30m':'30 分钟','60m':'60 分钟'};
const minutePeriods = ['1m','5m','15m','30m','60m'];
const reportNames = {calendar:'交易日历',mapping:'主力映射',warehouse:'仓单日报',holding:'成交持仓排名',settle:'结算参数',weekly_detail:'主要品种交易周报'};
let boardRows = [], boardOffset = 0, boardNext = null, boardsRequest=0;
const statuses = { queued:'排队中', running:'运行中', retrying:'重试等待', succeeded:'已完成', completed:'已完成', partial:'部分完成', failed:'失败', blocked:'需处理', cancelled:'已取消' };
const originNames={manual:'手动创建',scheduled:'自动更新',maintenance:'维护执行一次',retry:'失败重试',repair:'缺口补数',verify:'只读核验'};
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const icon = (name) => `<i data-lucide="${name}"></i>`;
const icons = () => window.lucide?.createIcons();
const values = (form) => {
  const result=Object.fromEntries(new FormData(form));
  form.querySelectorAll('select[multiple][name]').forEach(select=>{result[select.name]=selectedValues(select).join(',');});
  return result;
};
const splitCodes = (value) => value.split(/[,，\r\n]+/).map((code) => code.trim()).filter(Boolean);
const formatTime = (value) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';
const number = (value) => value === undefined || value === null ? '—' : String(value);
const empty = (id, cols, message) => { $(id).innerHTML = `<tr><td colspan="${cols}" class="empty">${escape(message)}</td></tr>`; };
let chart;
let provider = 'qmt', accounts = [], sourceCapabilities = {};
let qmtReferenceCapabilities = {};
let accountsRequest=0, datasetsRequest=0, jobsRequest=0, securitiesRequest=0, catalogRequest=0, historyRequest=0;
let futuresRequest=0, futuresOptionsRequest=0, futuresOffset=0, futuresNext=null, futuresActive='calendar';
let jobOffset=0, jobNext=null, jobDetailRequest=0;

function notice(message) {
  $('#notice').textContent = message;
  $('#notice').hidden = !message;
}

async function api(path, payload) {
  const scoped = ['/catalog','/catalog/securities','/catalog/select','/catalog/resolve','/catalog/detail','/catalog/sync','/history','/history/query','/exports','/exports/batch','/downloads/batch','/securities'];
  const route = path.split('?')[0];
  if(window.Workspace){
    if(['/jobs/query','/events/query','/freshness/query','/operations/summary','/maintenance/query','/datasets/query'].includes(route))payload={...payload,sources:[provider]};
    if(payload===undefined&&['/health','/maintenance','/datasets','/runtime/events'].includes(route)){
      const url=new URL(path,location.origin);url.searchParams.set('source',provider);path=url.pathname+url.search;
    }
  }
  if (scoped.includes(route) || route === '/datasets' && payload !== undefined) {
    if (payload === undefined) {
      const url = new URL(path,location.origin);
      if (!url.searchParams.has('source')) url.searchParams.set('source',provider);
      path = url.pathname + url.search;
    } else payload = {source:provider, ...payload};
    if (payload?.source === 'tushare' && ['/catalog/sync','/datasets'].includes(route)) payload.account_id ||= $('#data-account').value;
  }
  const response = await fetch('/api/v1' + path, { method: payload === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' }, body: payload === undefined ? undefined : JSON.stringify(payload) });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== '/login') {
      state.authenticated = false;
      closeQuotes();
      if (!$('#login-dialog').open) $('#login-dialog').showModal();
    }
    throw new Error(result.error || '请求失败');
  }
  if(payload!==undefined&&/^\/jobs\/[^/]+\/(cancel|retry|verify|repair|delete|restore)$/.test(route))window.Workspace?.discardPendingList('job-filter');
  return result;
}

function action(callback) {
  return async (event) => {
    event?.preventDefault();
    const button = event?.submitter || (event?.currentTarget?.tagName === 'BUTTON' ? event.currentTarget : null);
    const form = event?.currentTarget?.tagName === 'FORM' ? event.currentTarget : null;
    if (button) button.disabled = true;
    try { await callback(event); } catch (error) { notice(error.message);if(form)window.Workspace?.queryFailed(form,error); }
    finally { if (button) button.disabled = button.matches('#catalog-form button[type="submit"]') && ['queued','running','retrying'].includes(catalogJob?.state); requestAnimationFrame(icons); }
  };
}

async function status() {
  const result = await api('/status');
  $('#db-state').textContent = result.database.connected ? '历史库已连接' : '历史库未连接';
  $('#db-state').classList.toggle('online', result.database.connected);
  $('#db-configured').textContent = result.settings.database_configured ? '已保存连接信息' : '尚未配置';
  $('#settings-form').elements.qmt_root.value = result.settings.qmt_root;
  $('#settings-form').elements.login_enabled.checked = result.settings.login_enabled;
  $('#settings-form').elements.password.disabled = !result.settings.login_enabled;
  window.Workspace?.configLoaded(result.settings);
  return result;
}

async function switchView(view) {
  if(window.Workspace)return Workspace.navigate(view);
  if (!names[view]) view = 'market';
  if (view==='futures' && provider!=='tushare') view='market';
  state.view = view;
  $$('.view').forEach((item) => { item.hidden = item.id !== view; });
  $$('nav button').forEach((item) => { item.classList.toggle('active', item.dataset.view === view); item.setAttribute('aria-current', item.dataset.view === view ? 'page' : 'false'); });
  $('#view-title').textContent = names[view];
  $('footer span').textContent=provider==='qmt'?'QMT · 不复权 / 空值保留 / 原始成交单位':view==='futures'?'Tushare · 原始资料 / 空值保留 / 单位以资料为准':'Tushare · 不复权 / 成交量与持仓量：手 / 成交额：元';
  history.replaceState(null, '', '#' + view);
  notice('');
  if (!state.authenticated) return;
  try {
    if (view === 'indices') { await loadCatalog(); await loadIndices(); }
    if (view === 'boards') await loadBoards();
    if (view === 'history') { await loadDatasets(); chart?.resize(); }
    if (view === 'jobs') { await loadDatasets(); await loadJobs(0); }
    if (view === 'operations') { await loadOperations(); await loadEvents(); }
    if (view === 'settings') { await status(); await loadAccounts(); }
    if (view === 'futures') { await loadFuturesOptions(); updateFuturesForm(); }
    if (view === 'market') { await loadCatalog(); await loadSecurities(); }
    await resolveSelections();
  } catch (error) { notice(error.message); }
}

async function loadSecurities(offset = 0, apply=false) {
  const selectedSource=provider, view=state.view;
  const query=window.Workspace?.listQuery('security-form',apply) || {...values($('#security-form')),active:$('#catalog-active').checked};
  const read=query=>api('/catalog/securities?' + new URLSearchParams({ ...query, limit:50, ...window.Workspace?.pageOptions('security-form'), offset }));
  const result = window.Workspace?await Workspace.readList('security-form',apply,read):await read(query);
  if(!result)return;
  const request=++securitiesRequest;
  if(request!==securitiesRequest || selectedSource!==provider || view!==state.view)return;
  const rows = result.rows;
  rememberSecurities(rows);
  securityOffset = offset; securityNext = result.next_offset;
  $('#security-prev').disabled = !offset; $('#security-next').disabled = securityNext === null;
  $('#security-page-status').textContent = `共 ${result.total} 个${rows.length ? ` · ${offset + 1}–${offset + rows.length}` : ''}`;
  if (!rows.length) return empty('#securities', 6, '没有匹配证券');
  $('#securities').innerHTML = rows.map((row) => `<tr><td>${escape(row.code)}</td><td>${escape(row.name)}</td><td>${escape(kindNames[row.kind])}<span class="muted">${escape(subtypeNames[row.subtype] || '')}</span></td><td>${escape(marketNames[row.market] || row.market)}</td><td>${formatTime(row.updated_at)}</td><td><button type="button" class="icon" data-instrument="${escape(row.code)}" title="查看合约资料" aria-label="查看 ${escape(row.name)} 资料">${icon('file-search')}</button></td></tr>`).join('');
  window.Workspace?.catalogLoaded(rows);
  icons();
}

async function loadCatalog() {
  const request=++catalogRequest, selectedSource=provider;
  clearTimeout(catalogTimer);
  if (!state.authenticated) return;
  const result = await api('/catalog');
  if(request!==catalogRequest || selectedSource!==provider)return;
  catalogSectors = result.sectors;
  filterSectors();
  const counts = [...result.counts.map((item) => `${kindNames[item.kind]} ${item.count} 个`), ...(result.boards || []).map((item) => `${item.category === 'industry' ? '行业' : '概念'} ${item.count} 个`)];
  $('#catalog-counts').textContent = counts.length ? counts.join(' · ') : '尚未同步目录';
  const wasActive = catalogJob && ['queued','running','retrying'].includes(catalogJob.state);
  catalogJob = result.job;
  const active = catalogJob && ['queued','running','retrying'].includes(catalogJob.state);
  $('#catalog-form button[type="submit"]').disabled = !!active;
  $('#catalog-cancel').hidden = !active;
  $('#catalog-progress').hidden = !active;
  $('#catalog-progress').max = catalogJob?.total_chunks || 1;
  $('#catalog-progress').value = catalogJob?.checkpoint || 0;
  $('#catalog-status').textContent = result.error || (catalogJob ? `${statuses[catalogJob.state]} · ${catalogJob.checkpoint} / ${catalogJob.total_chunks || '待发现'} 个 · 已保存 ${catalogJob.result.rows || 0} 个${catalogJob.error ? ' · ' + catalogJob.error : ''}${catalogJob.result.missing?.length ? ` · ${catalogJob.result.missing.length} 个名称不可用` : ''}` : '');
  if (wasActive && !active) { await loadSecurities(); await resolveSelections(); }
  if (active) catalogTimer = setTimeout(() => loadCatalog().catch((error) => notice(error.message)), 2000);
}

function filterSectors() {
  const select = $('#index-form [name="sector"]');
  const selected = select.value;
  const search = $('#sector-search').value.trim().toLowerCase();
  const options = catalogSectors.filter((name) => name.toLowerCase().includes(search) || name === selected);
  select.innerHTML = '<option value="">选择 QMT 板块</option>' + options.map((name) => `<option value="${escape(name)}">${escape(name)}</option>`).join('');
  select.value = selected;
}

function suggestSector() {
  const code = $('#index-form [name="code"]').value;
  const name = catalogNames.get(code)?.name;
  const existing = state.indices.find((item) => item.code === code)?.sector;
  $('#sector-search').value = '';
  filterSectors();
  $('#index-form [name="sector"]').value = catalogSectors.includes(existing) ? existing : catalogSectors.includes(name) ? name : '';
}

function renderQuotes(data) {
  Object.assign(state.quotes, data);
  const rows = Object.entries(state.quotes);
  if (!rows.length) return empty('#quotes', 9, '暂无行情');
  recordSets.quotes={title:'实时行情详情',rows:rows.map(([code,incoming])=>({code,...(Array.isArray(incoming)?incoming.at(-1)||{}:incoming)})),labels:{code:'证券 / 合约',lastPrice:'最新价',lastClose:'昨收',time:'行情时间',volume:'成交量',amount:'成交额',openInt:'持仓量'},note:'QMT · 原始行情字段与单位'};
  $('#quotes').innerHTML = rows.map(([code, incoming],index) => {
    const row = Array.isArray(incoming) ? incoming.at(-1) || {} : incoming;
    const price = row.lastPrice ?? row.last ?? row.close;
    const previous = row.lastClose ?? row.preClose;
    const change = price !== undefined && previous ? (Number(price) / Number(previous) - 1) * 100 : null;
    const tone = change === null || change === 0 ? '' : change > 0 ? 'positive' : 'negative';
    return `<tr><td><button type="button" class="text-link" data-record="quotes" data-record-index="${index}">${escape(catalogNames.get(code)?.name || code)}</button><span class="muted">${escape(code)}</span></td><td class="numeric ${tone}">${escape(number(price))}</td><td class="numeric">${escape(number(previous))}</td><td class="numeric ${tone}">${change === null ? '—' : change.toFixed(2) + '%'}</td><td class="numeric">${escape(number(row.lastSettlementPrice ?? row.preSettlementPrice))}</td><td class="numeric">${escape(number(row.openInt ?? row.openInterest))}</td><td class="numeric">${escape(number(row.volume))}</td><td class="numeric">${escape(number(row.amount))}</td><td>${row.time ? formatTime(row.time) : '—'}</td></tr>`;
  }).join('');
}

async function connectSocket() {
  if (!state.authenticated || state.connecting || [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.socket?.readyState)) return;
  clearTimeout(state.reconnect);
  state.connecting = true;
  try {
    const ticket = await api('/ws-ticket', {});
    if (!state.authenticated) return;
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const hostname = location.hostname.includes(':') ? `[${location.hostname}]` : location.hostname;
    const socket = new WebSocket(`${protocol}://${hostname}:${ticket.port}/?ticket=${encodeURIComponent(ticket.ticket)}`);
    state.socket = socket;
    socket.onopen = () => { if (state.watch.length) sendWatch(); };
    socket.onmessage = (event) => {
      if (state.socket !== socket || !state.authenticated) return;
      const message = JSON.parse(event.data);
      if (message.event === 'quote' && state.watch.length) renderQuotes(Object.fromEntries(Object.entries(message.data).filter(([code]) => state.watch.includes(code))));
      if (message.event === 'watch' && message.codes.join(',') === state.watch.join(',')) {
        $('#quote-status').textContent = state.watch.length ? `已订阅 ${state.watch.length} 个证券` : '已停止订阅';
        $('#quote-stop').disabled = !state.watch.length;
      }
      if (message.event === 'job' && ['jobs','exports'].includes(state.view)) loadJobs().catch((error) => notice(error.message));
      if (message.event === 'source') {
        $('#qmt-state').textContent = message.connected ? '行情桥已连接' : '行情桥未连接';
        $('#qmt-state').classList.toggle('online', !!message.connected);
        if (!message.connected && state.watch.length) $('#quote-status').textContent = '等待行情桥恢复';
      }
      if (message.event === 'error') {
        if (message.codes) {
          state.watch = message.codes;
          socket.watchKey = message.codes.join(',');
          $('#quote-stop').disabled = !state.watch.length;
          $('#quote-status').textContent = state.watch.length ? `保留原订阅：${state.watch.join(', ')}` : '未订阅';
        }
        notice(message.message);
      }
    };
    socket.onclose = () => {
      if (state.socket !== socket) return;
      state.socket = null;
      $('#qmt-state').textContent = '行情桥待检查';
      $('#qmt-state').classList.remove('online');
      if (state.watch.length) $('#quote-status').textContent = '连接已断开，等待恢复';
      if (state.authenticated) state.reconnect = setTimeout(() => connectSocket().catch((error) => notice(error.message)), 5000);
    };
  } catch (error) {
    if (state.authenticated) state.reconnect = setTimeout(() => connectSocket().catch((failure) => notice(failure.message)), 5000);
    throw error;
  } finally { state.connecting = false; }
}

function sendWatch() {
  const socket = state.socket;
  if (!state.authenticated || socket?.readyState !== WebSocket.OPEN) return;
  const key = state.watch.join(',');
  if (socket.watchKey === key) return;
  socket.send(JSON.stringify(key ? { action:'watch', codes:state.watch } : { action:'unwatch' }));
  socket.watchKey = key;
}

function closeQuotes() {
  state.watch = [];
  clearTimeout(catalogTimer);
  clearTimeout(state.reconnect);
  state.socket?.close();
  $('#quote-stop').disabled = true;
  $('#quote-status').textContent = '未订阅';
}

async function loadIndices() {
  state.indices = await api('/indices');
  suggestSector();
  if (!state.indices.length) return empty('#index-rows', 5, '尚未建立指数映射');
  $('#index-rows').innerHTML = state.indices.map((row, index) => `<tr><td>${escape(row.name)}<span class="muted">${escape(row.code)}</span></td><td>${escape(row.sector)}</td><td>${row.members?.length ?? 0}</td><td>${formatTime(row.observed_at)}</td><td><div class="actions"><button data-index-members="${index}" title="查看成分" aria-label="查看成分">${icon('list')}</button><button data-index-dataset="${index}" title="以当前成分创建数据集" aria-label="以当前成分创建数据集">${icon('folder-plus')}</button></div></td></tr>`).join('');
  window.Workspace?.filterLocalRows('indices');icons();
}

async function loadDatasets(apply=false) {
  const source=provider;
  const querying=state.view==='datasets'&&window.Workspace;
  const page=querying?await Workspace.datasetRows(apply):null;if(querying&&!page)return;
  const rows=page?page.active_rows:await api('/datasets');
  if(source!==provider)return;
  state.datasets=rows;
  const select = $('#download-form [name="dataset_id"]');
  const selected = new Set(selectedValues(select));
  select.innerHTML = '<option value="">选择数据集</option>' + state.datasets.filter(row=>row.source===provider).map((row) => `<option value="${row.id}" ${selected.has(row.id)?'selected':''}>${escape(row.name)} · ${row.members.length} 个证券 · ${escape(row.account_id || 'QMT')}</option>`).join('');
  refreshMulti(select);
  updateDownloadPeriods();
  const visible=page?page.rows:state.datasets.filter(row=>row.source===provider);
  if(!visible||source!==provider)return;
  state.datasetRows=visible;
  if (!visible.length) return empty('#datasets', 5, '没有匹配的数据集');
  $('#datasets').innerHTML = visible.map((row) => `<tr data-dataset-id="${row.id}"><td>${escape(row.name)}${row.deleted_at?`<span class="badge cancelled">回收站</span><span class="muted">${formatTime(row.deleted_at)}</span>`:""}${row.index_code || row.board_name ? `<span class="muted">${escape(row.board_name || row.index_code)} 当前成分快照</span>` : ''}</td><td>${row.members.length}</td><td>${row.periods.map(period=>periodNames[period]).join(' / ')}</td><td><input type="checkbox" data-schedule="${row.id}" ${row.deleted_at?'disabled':''} aria-label="${escape(row.name)} 交易日自动更新" ${row.scheduled ? 'checked' : ''}></td><td><div class="actions"><button data-download="${row.id}" aria-label="下载此数据集" title="下载此数据集">${icon('download')}</button><button data-dataset-detail="${row.id}" aria-label="数据集详情" title="数据集详情">${icon('file-search')}</button>${row.index_code || row.board_name ? `<button data-refresh-dataset="${row.id}" title="显式刷新成员" aria-label="显式刷新成员">${icon('refresh-cw')}</button>` : ''}</div></td></tr>`).join('');
  icons();
  $$('#datasets tr').forEach((tr,index) => {
    const row = visible[index];
    tr.children[0].insertAdjacentHTML('beforeend',`<span class="muted">${escape(row.source)}${row.account_id ? ' · ' + escape(row.account_id) : ''}</span>`);
    tr.children[3].insertAdjacentHTML('beforeend',`<span class="muted">${escape(row.schedule_time?.slice(0,5) || '17:00')}</span>`);
    if (row.source === 'tushare' && !row.deleted_at) tr.children[4].insertAdjacentHTML('beforeend',`<select data-dataset-account="${row.id}" aria-label="${escape(row.name)} 采集账号">${accounts.filter(a=>a.enabled).map(a=>`<option value="${escape(a.id)}" ${a.id===row.account_id?'selected':''}>${escape(a.name)}</option>`).join('')}</select>`);
  });
}

function updateDownloadPeriods() {
  const form = $('#download-form');
  const ids=selectedValues(form.elements.dataset_id);
  const datasets = state.datasets.filter(row => ids.includes(row.id));
  const dataset=datasets[0];
  const selection = datasets.map(row=>row.id+':'+(row.account_id || '')).join(',');
  const reset = form.dataset.selection !== selection;
  form.dataset.selection = selection;
  const blockedPeriod = (row,period) => row.source==='tushare' && ['permission','authentication','unsupported'].includes(sourceCapabilities[row.account_id]?.capabilities?.[minutePeriods.includes(period)?'minutes':period==='1w'?'weekly':period==='1mo'?'monthly':'daily']?.state);
  $$('#download-form input[name="period"]').forEach(input => {
    const allowed = datasets.some(row=>row.periods.includes(input.value));
    if (reset) input.checked = Boolean(allowed);
    input.closest('label').hidden = !allowed;
    input.disabled = !allowed || datasets.some(row=>row.periods.includes(input.value) && blockedPeriod(row,input.value));
    if (input.disabled) input.checked = false;
  });
  $('#download-capability').hidden = !dataset;
  $('#download-capability').textContent = dataset ? `${datasets.length} 个数据集 · ${datasets.reduce((total,row)=>total+row.members.length,0)} 个成员（含重复） · ${[...new Set(datasets.map(row=>row.account_id || 'QMT'))].join('、')} · 各数据集仅回补其已有周期${datasets.some(row=>blockedPeriod(row,'1m'))?' · 历史分钟权限不可用':''}` : '';
  refreshCheckAll();
}

const barLabels={price:'价格',code:'证券 / 合约',period:'周期',time:'行情时间 / 周期标签',trading_day:'交易日',as_of_date:'计算截至日',open:'开盘',high:'最高',low:'最低',close:'收盘',volume:'成交量',amount:'成交额',open_interest:'持仓量',settlement:'结算价',previous_settlement:'前结算价',source:'来源'};
let historySelection=null;
function clearHistoryResults() {
  if(window.Workspace)return Workspace.invalidate('history');
  ++historyRequest;historySelection=null;state.offset=0;state.next=null;
  empty('#bars',14,'筛选已变更，尚未查询');$('#page-status').textContent='0 条';$('#prev-page').disabled=true;$('#next-page').disabled=true;
  chart?.clear();$('#chart-empty').hidden=false;$('#history-units').textContent='';$('#chart-selection').hidden=true;
}

async function loadHistory(offset = 0, apply = false) {
  if(window.Workspace)return Workspace.history(offset,apply);
  const request=++historyRequest, selectedSource=provider;
  const query = values($('#history-form')), members=splitCodes(query.code), periods=selectedValues($('#history-form [name=period]'));
  if(!members.length || !periods.length)throw new Error('请选择证券和查询周期');
  if(query.start>query.end)throw new Error('开始日期不得晚于结束日期');
  const multiple=members.length>1 || periods.length>1;
  const result = multiple ? await api('/history/query',{members,periods,start:query.start,end:query.end,limit:300,offset}) : await api('/history?' + new URLSearchParams({ ...query, limit:300, offset }));
  if(request!==historyRequest || selectedSource!==provider)return;
  $('#history-units').textContent = `${result.provider || 'qmt'} · 成交量：${result.units.volume} · 成交额：${result.units.amount} · 持仓量：${result.units.open_interest} · ${result.date_basis}`;
  const numericFields=['open','high','low','close','volume','amount','open_interest','settlement','previous_settlement'];
  $('#history table thead').innerHTML='<tr><th>证券 / 合约</th><th>周期</th><th>行情时间 / 周期标签</th><th>交易日 / 计算截至日</th>'+numericFields.map(field=>`<th class="numeric">${barLabels[field]}</th>`).join('')+'<th>详情</th></tr>';
  state.offset = offset;
  state.next = result.next_offset;
  $('#prev-page').disabled = !offset;
  $('#next-page').disabled = state.next === null;
  $('#page-status').textContent = result.rows.length ? `${offset + 1}–${offset + result.rows.length} 条` : '0 条';
  $('#chart-empty').hidden = result.rows.length > 0;
  recordSets.history={title:'历史行情详情',rows:result.rows,labels:barLabels,note:$('#history-units').textContent};
  if (!result.rows.length) empty('#bars', 14, '所选范围没有入库行情');
  else $('#bars').innerHTML = result.rows.map((row,index) => {
    const aggregate=['1w','1mo'].includes(row.period);
    return `<tr><td>${escape(securityLabel(row.code))}</td><td>${periodNames[row.period]}</td><td>${formatTime(row.time)}${aggregate && row.time.slice(0,10)>row.as_of_date ? '<span class="badge partial">周期未结束</span>' : ''}</td><td>${escape((aggregate ? row.as_of_date : row.trading_day) || '待核验')}</td>${numericFields.map(field=>`<td class="numeric">${escape(number(row[field]))}</td>`).join('')}<td>${recordButton('history',index)}</td></tr>`;
  }).join('');
  historySelection={members,periods,start:query.start,end:query.end,source:provider};
  for(const [id,options] of [['chart-code',members],['chart-period',periods]]){
    const select=$('#'+id), previous=select.value;
    select.innerHTML=options.map(value=>`<option value="${escape(value)}">${escape(id==='chart-code'?securityLabel(value):periodNames[value])}</option>`).join('');
    if(options.includes(previous))select.value=previous;
  }
  $('#chart-selection').hidden=!multiple;
  if(multiple)await loadChart();else renderChart(result.rows,members[0],periods[0]);
  icons();
}

function renderChart(rows,code,period) {
  $('#chart-empty').hidden=rows.length>0;
  if (!chart) chart = echarts.init($('#chart'));
  chart.setOption({ animation: false, grid: { left: 65, right: 25, top: 25, bottom: 60 }, tooltip: { trigger: 'axis' }, xAxis: { type: 'category', data: rows.map((row) => minutePeriods.includes(period) ? row.time.slice(0,16).replace('T',' ') : row.time.slice(0,10)), axisLine: { lineStyle: { color: '#c4cec7' } }, axisLabel: { color: '#819086', fontSize: 10 } }, yAxis: { scale: true, splitLine: { lineStyle: { color: '#edf1ee' } }, axisLabel: { color: '#819086', fontSize: 10 } }, dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 10, height: 18, borderColor: '#e4eae6' }], series: [{ type: 'candlestick', name:code, data: rows.map((row) => [row.open,row.close,row.low,row.high].map((value) => value === null ? '-' : Number(value))), itemStyle: { color: '#cd5656', color0: '#2c9375', borderColor: '#cd5656', borderColor0: '#2c9375' } }] }, true);
  chart.resize();
}

let chartRequest=0;
async function loadChart() {
  if(window.Workspace)return Workspace.chart();
  if(!historySelection)return;
  const request=++chartRequest, selection=historySelection, code=$('#chart-code').value, period=$('#chart-period').value;
  const result=await api('/history?'+new URLSearchParams({source:selection.source,code,period,start:selection.start,end:selection.end,limit:300}));
  if(request!==chartRequest || historySelection!==selection || selection.source!==provider)return;
  renderChart(result.rows,code,period);
}

function renderWorkerStatus(health) {
  const fallback=[['worker','qmt','schedule'],['tushare_worker','tushare','schedule'],['catalog_worker','qmt','catalog'],['tushare_catalog_worker','tushare','catalog'],['export_worker','qmt','export']];
  const issues=(health.worker_issues || fallback.filter(([key])=>health[key]).map(([key,source,lane])=>({source,lane,name:'后台服务',reason:health[key],action:'查看运行日志'}))).filter(row=>!window.Workspace||row.source===provider);
  const panel=$('#worker-status');panel.hidden=!issues.length;
  if(!issues.length) { panel.open=false;$('#worker-alert-rows').replaceChildren();return; }
  const laneNames={schedule:'自动更新',catalog:'目录同步',export:'文件导出'};
  const sources=[...new Set(issues.map(row=>row.source==='tushare'?'Tushare':row.lane==='export'?'文件导出':'QMT'))];
  const scopes=new Set(issues.filter(row=>row.scope_id).map(row=>row.source+':'+row.scope_kind+':'+row.scope_id));
  $('#worker-alert-title').textContent=issues.every(row=>row.lane==='schedule')?'自动更新受阻':'后台任务需关注';
  $('#worker-alert-count').textContent=sources.join('、')+' · '+(scopes.size?`${scopes.size} 个范围 · `:'')+`${issues.length} 项异常`;
  $('#worker-alert-rows').innerHTML=[...issues].sort((a,b)=>(a.source+a.lane).localeCompare(b.source+b.lane)).map(row=>`<tr><td data-label="数据源 / 队列">${row.source==='tushare'?'Tushare':'QMT'}<span class="muted">${laneNames[row.lane] || escape(row.lane)}</span></td><td data-label="影响范围">${escape(row.name)}</td><td data-label="市场">${escape(marketNames[row.market] || (row.market==='account'?'账号配置':row.market) || '—')}</td><td data-label="原因">${escape(row.reason)}</td><td data-label="建议动作">${escape(row.action || '查看运行日志')}</td></tr>`).join('');
  icons();
}

$('#worker-alert-logs').addEventListener('click',action(()=>switchView('operations')));

async function loadJobs(offset=jobOffset,apply=false) {
  if(typeof offset!=='number')offset=jobOffset;
  const view=state.view;
  const query=window.Workspace?.listQuery('job-filter',apply) || values($('#job-filter'));
  const read=query=>api('/jobs/query',{...query,limit:50,...window.Workspace?.pageOptions('job-filter'),offset});
  const result=window.Workspace?await Workspace.readList('job-filter',apply,read):await read(query);
  if(!result)return;
  const request=++jobsRequest;
  const rows=result.rows;
  if(offset&&!rows.length&&result.total)return loadJobs(Math.floor((result.total-1)/(window.Workspace?.pageSize('job-filter')||50))*(window.Workspace?.pageSize('job-filter')||50));
  if(!result.total)offset=0;
  const health = await api('/status');
  if(request!==jobsRequest || view!==state.view)return;
  state.jobs=rows;
  jobOffset=offset;jobNext=result.next_offset;
  $('#job-page').textContent=`共 ${result.total} 个${rows.length?` · ${offset+1}–${offset+rows.length}`:''}`;
  $('#job-prev').disabled=!offset;$('#job-next').disabled=jobNext===null;
  renderWorkerStatus(health);
  if (!state.jobs.length) {empty('#job-rows',9,'没有匹配任务');window.Workspace?.jobsLoaded();return;}
  $('#job-rows').innerHTML = state.jobs.map((job) => {
    const total = job.total_chunks;
    const unit = job.kind === 'catalog' ? job.payload.source === 'tushare' ? '批次' : '证券' : '分块';
    const p=job.payload, kind=job.kind==='verify'?'只读核验':job.kind==='download'?reportNames[p.resource] || '历史回补':job.kind==='catalog'?'目录同步':(p.format || '文件').toUpperCase();
    const scope=job.maintenance_name || job.dataset_name || (job.kind==='catalog'?(p.exchanges || p.kinds?.map(value=>kindNames[value]||value))?.join('、'):p.code || p.members?.slice(0,2).map(securityLabel).join('、') || p.symbol || p.selections?.slice(0,2).map(row=>row.code||row.symbol||row.exchange).join('、')) || '未记录范围名称';
    const count=p.members?.length || p.selections?.length;
    const account=accounts.find(row=>row.id===p.account_id)?.name || p.account_id || (p.source==='tushare'?(job.kind==='export'?'不适用':'未记录账号'):'本地行情桥');
    const periods=(p.periods || (p.period?[p.period]:[])).map(value=>periodNames[value]||value).join(' / ');
    return `<tr><td title="${job.id}">${job.id.slice(0,8)}<span class="muted">${formatTime(job.created_at)}</span><span class="muted">${escape(originNames[job.origin] || '手动创建')}</span></td><td class="task-scope"><strong>${escape(scope)}</strong>${count?`<span class="muted">${count} 个对象</span>`:''}<span class="muted">${p.start||p.end?escape((p.start||'未记录')+' 至 '+(p.end||'未记录')):'日期未记录或不适用'}</span></td><td>${p.source==='tushare'?'Tushare':'QMT'}<span class="muted" title="${escape(p.account_id||'')}">${escape(account)}</span></td><td>${escape(kind)}<span class="muted">${escape(periods || (p.schedule_market?marketNames[p.schedule_market]||p.schedule_market:''))}</span></td><td><span class="badge ${job.state}">${statuses[job.state] || escape(job.state)}</span></td><td>${total ? `<progress value="${job.checkpoint}" max="${total}" aria-label="任务分块进度"></progress><span class="muted">${job.checkpoint} / ${total} ${unit}</span>` : escape(job.checkpoint+' '+(job.kind==='catalog'?'批次':'行'))}</td><td class="task-result">${job.result.rows === undefined ? '—' : job.kind === 'download' ? '已入库 ' + job.result.rows + ' 行' : job.result.rows + ' 行'}${job.result.rows===0&&job.result.message?`<span class="muted">${escape(job.result.message)}</span>`:''}${job.error ? `<details><summary>${escape(({SOURCE_PERMISSION:'接口权限不足',SOURCE_UNAVAILABLE:'数据源不可用',OHLC_RANGE:'价格范围异常',INVALID_DATA:'数据校验未通过'})[job.error_code] || '查看原因')}</summary><span class="muted">${escape(job.error_code||'')}</span><p>${escape(job.error)}</p>${job.action?`<p>${escape(job.action)}</p>`:''}</details>` : ''}</td><td>${formatTime(job.updated_at)}</td><td><div class="actions"><button data-job-detail="${job.id}" title="任务详情" aria-label="任务详情">${icon('list')}</button>${['queued','running','retrying'].includes(job.state) ? `<button data-job="cancel" data-id="${job.id}" title="取消后续处理" aria-label="取消后续处理">${icon('square')}</button>` : ''}${['failed','cancelled','partial','blocked'].includes(job.state) ? `<button data-job="retry" data-id="${job.id}" title="重试" aria-label="重试">${icon('rotate-cw')}</button>` : ''}${job.kind === 'export' && job.state === 'succeeded' ? `<a href="/api/v1/files/${job.id}" title="下载文件" aria-label="下载文件">${icon('download')}</a><a href="/api/v1/files/${job.id}/metadata" title="下载口径说明" aria-label="下载口径说明">${icon('file-text')}</a>` : ''}</div></td></tr>`;
  }).join('');
  icons();
  window.Workspace?.jobsLoaded();
}

$$('nav button').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.view)));
$('.brand').addEventListener('click', (event) => { event.preventDefault(); switchView('market'); });
$('#login-dialog').addEventListener('cancel', (event) => event.preventDefault());
$('#login-form').addEventListener('submit', action(async () => {
  try {
    await api('/login', values($('#login-form')));
    $('#login-form').reset();
    $('#login-error').textContent = '';
    $('#login-dialog').close();
    state.authenticated = true;
    await status();
    await loadAccounts();
    await connectSocket();
    await switchView(state.view);
  } catch (error) { $('#login-error').textContent = error.message; }
}));
$('#logout').addEventListener('click', action(async () => {
  await api('/logout', {});
  state.authenticated = false;
  closeQuotes();
  $('#login-dialog').showModal();
}));
$('#security-form').addEventListener('submit', action(() => loadSecurities(0,true)));
$('#security-prev').addEventListener('click', action(() => loadSecurities(Math.max(0, securityOffset - (window.Workspace?.pageSize('security-form') || 50)))));
$('#security-next').addEventListener('click', action(() => loadSecurities(securityNext)));
$('#catalog-form').addEventListener('submit', action(async () => {
  const kinds = new FormData($('#catalog-form')).getAll('kind');
  if (!kinds.length) throw new Error('请选择至少一个目录类别');
  const exchanges=provider==='tushare'?selectedValues($('#catalog-form [name=exchanges]')):undefined;
  if(provider==='tushare'&&!exchanges.length)throw new Error('请选择至少一个交易所');
  await api('/catalog/sync', { kinds,exchanges });
  await loadCatalog();
  notice('目录同步任务已创建');
}));
$('#catalog-cancel').addEventListener('click', action(async () => { if (catalogJob) await api('/jobs/' + catalogJob.id + '/cancel', {}); await loadCatalog(); }));
$('#quote-refresh').addEventListener('click', action(async () => { const result = await api('/quotes?codes=' + encodeURIComponent(values($('#quote-form')).codes)); state.quotes = {}; renderQuotes(result); }));
$('#quote-form').addEventListener('submit', action(async () => {
  const selected = [...new Set(splitCodes(values($('#quote-form')).codes))];
  if (!selected.length) throw new Error('请选择行情证券');
  if (state.watch.join(',') === selected.join(',') && state.socket?.readyState === WebSocket.OPEN) return;
  state.watch = selected;
  state.quotes = {};
  empty('#quotes',9,'等待行情源推送');
  $('#quote-stop').disabled = false;
  $('#quote-status').textContent = '正在订阅';
  await connectSocket();
  sendWatch();
}));
$('#quote-stop').addEventListener('click', () => { state.watch = []; $('#quote-stop').disabled = true; $('#quote-status').textContent = state.socket?.readyState === WebSocket.OPEN ? '正在停止订阅' : '已停止订阅'; sendWatch(); });
$('#load-sectors').addEventListener('click', action(async () => { catalogSectors = await api('/sectors'); filterSectors(); suggestSector(); notice(`已读取 ${catalogSectors.length} 个板块`); }));
$('#sector-search').addEventListener('input', filterSectors);
$('#index-form').addEventListener('submit', action(async () => { await api('/indices/refresh', values($('#index-form'))); await loadIndices(); notice('当前成分快照已保存'); }));
$('#history-form').addEventListener('submit', action(() => loadHistory(0,true)));
$('#prev-page').addEventListener('click', action(() => loadHistory(Math.max(0,state.offset-(window.Workspace?.pageSize('history-form') || 300)))));
$('#next-page').addEventListener('click', action(() => loadHistory(state.next)));
for (const format of ['csv','parquet']) $(`#export-${format}`).addEventListener('click', action(async () => {
  if(window.Workspace)return Workspace.exportHistory(format);
  const p=values($('#history-form')), members=splitCodes(p.code), periods=selectedValues($('#history-form [name=period]'));
  if(!members.length || !periods.length)throw new Error('请选择证券和导出周期');
  if((members.length>1 || periods.length>1) && !await confirmBatch('批量导出行情',{来源:provider,合约数:members.length,周期:periods.map(value=>periodNames[value]).join('、'),日期:`${p.start} 至 ${p.end}`,文件:`${periods.length} 个 ${format.toUpperCase()} 文件`}))return;
  await api(periods.length>1?'/exports/batch':'/exports',{members,periods,period:periods[0],start:p.start,end:p.end,format});
  await switchView('jobs');notice('导出任务已创建');
}));
$('#dataset-form').addEventListener('submit', action(async () => { const form = $('#dataset-form'); const p = values(form); const selected = new FormData(form).getAll('period'); if (!p.members) throw new Error('请选择数据集成员'); if (!selected.length) throw new Error('请选择至少一个周期'); const extra = state.snapshot ? state.snapshot.board ? {board_name:state.snapshot.name, board_snapshot_id:state.snapshot.id} : { index_code:state.snapshot.code, snapshot_id:state.snapshot.snapshot_id } : {}; await api('/datasets', { name:p.name, members:splitCodes(p.members), periods:selected, schedule_time:$('#dataset-schedule-time').value, ...extra }); state.snapshot = null; form.reset(); $('#dataset-form [name="members"]').value = ''; rememberSecurities([]); await loadDatasets(); notice('数据集已创建'); }));
$('#download-form [name="dataset_id"]').addEventListener('change', updateDownloadPeriods);
$('#download-form').addEventListener('submit', action(async () => {
  const form = $('#download-form'), p = values(form);
  const periods = new FormData(form).getAll('period');
  const ids=selectedValues(form.elements.dataset_id);
  if (!ids.length || !periods.length) throw new Error('请选择数据集和至少一个回补周期');
  if(ids.length>1 && !await confirmBatch('批量历史回补',{来源:provider,数据集:ids.map(id=>state.datasets.find(row=>row.id===id)?.name || id).join('、'),周期:periods.map(value=>periodNames[value]).join('、'),日期:`${p.start || '默认开始日期'} 至 ${p.end || '今天'}`,范围:'各数据集仅回补其已有周期，账号及端点保持原绑定'}))return;
  await api(ids.length>1?'/downloads/batch':'/downloads', {source:provider,dataset_ids:ids,dataset_id:ids[0],periods,start:p.start || null,end:p.end || null});
  await loadJobs(); notice('回补任务已排队');
}));
$('#refresh-jobs').addEventListener('click', action(loadJobs));
$('#job-filter').addEventListener('submit',action(()=>loadJobs(0,true)));
$('#job-filter').addEventListener('reset',()=>setTimeout(()=>loadJobs(0,true).catch(error=>notice(error.message)),0));
$('#job-prev').addEventListener('click',action(()=>loadJobs(Math.max(0,jobOffset-(window.Workspace?.pageSize('job-filter') || 50)))));
$('#job-next').addEventListener('click',action(()=>loadJobs(jobNext)));
$('#settings-form').elements.login_enabled.addEventListener('change', (event) => { $('#settings-form').elements.password.disabled = !event.target.checked; });
$('#settings-form').addEventListener('submit', action(async () => { const p = values($('#settings-form')); p.login_enabled = $('#settings-form').elements.login_enabled.checked; await api('/settings', p); $('#settings-form').elements.dsn.value = ''; $('#settings-form').elements.password.value = ''; await status(); notice('本地配置已保存'); }));
$('#migrate').addEventListener('click', action(async () => { await api('/database/migrate', {}); await status(); notice('历史库已初始化'); }));
$('#test-source').addEventListener('click', action(async () => { const result = await api('/source/test', {}); $('#source-result').textContent = result.mode === 'market-only' ? '行情桥连接正常' : '响应已收到'; $('#qmt-state').textContent = '行情桥已连接'; $('#qmt-state').classList.add('online'); }));
$('#diagnose-source').addEventListener('click', action(async () => {
  const code = $('#diagnostic-code').value;
  $('#source-result').textContent = '正在检查 ' + securityLabel(code);
  $('#source-diagnostics').hidden = true;
  try {
    const result = await api('/source/diagnostics', {code});
    const bridgeOk = result.checks.find((check) => check.name === 'bridge')?.state === 'ok';
    $('#qmt-state').textContent = bridgeOk ? '行情桥已连接' : '行情桥未连接';
    $('#qmt-state').classList.toggle('online', bridgeOk);
    const labels = { bridge:'行情桥', snapshot:'行情快照', history:'近30天本地日线', calendar:'近30天交易日期', terminal_history:'终端最近历史请求' };
    const states = { ok:'可用', empty:'空结果', error:'失败', unverified:'未验证' };
    $('#source-checks').innerHTML = result.checks.map((check) => `<tr><td>${escape(labels[check.name])}</td><td>${escape(states[check.state])}</td><td>${escape(check.message)}${check.code ? ` · ${escape(check.code)} / ${escape(check.period)}` : ''}${check.rows !== undefined ? ` · ${check.rows} 条` : ''}${check.time ? `<span class="muted">${formatTime(check.time)}</span>` : ''}</td></tr>`).join('');
    $('#source-diagnostics').hidden = false;
    $('#source-result').textContent = result.history_readable ? '本地历史数据可读，下载覆盖未验收' : '历史链路未就绪，请检查终端行情连接与历史缓存';
  } catch (error) {
    $('#source-result').textContent = '检查失败';
    throw error;
  }
}));
$$('[data-deploy]').forEach((button) => button.addEventListener('click', action(async () => { const result = await api('/deploy/' + button.dataset.deploy, { qmt_root:values($('#settings-form')).qmt_root }); $('#deploy-result').hidden = false; $('#deploy-result').textContent = JSON.stringify(result,null,2); })));
document.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  const d = button.dataset;
  if (!['indexMembers','indexDataset','download','datasetDetail','refreshDataset','job','jobDetail','instrument','boardMembers','boardDataset','boardRefresh'].some((key) => key in d)) return;
  event.preventDefault();
  try {
  if (d.instrument) await showInstrument(d.instrument);
  if (d.boardMembers !== undefined || d.boardDataset !== undefined || d.boardRefresh !== undefined) {
    const row = boardRows[Number(d.boardMembers ?? d.boardDataset ?? d.boardRefresh)];
    const snapshot = d.boardRefresh !== undefined ? await api('/boards/refresh', {name:row.name}) : await api('/boards/members?' + new URLSearchParams({name:row.name}));
    if (d.boardDataset !== undefined) {
      if (!snapshot.members.length) throw new Error('板块成员为空，无法创建数据集');
      state.snapshot = {...snapshot, board:true};
      await switchView('history');
      $('#dataset-form [name="name"]').value = row.name + '成员';
      $('#dataset-form [name="members"]').value = snapshot.members.join(',');
      await resolveSelections();
      $('#dataset-form').scrollIntoView({block:'center'});
    } else {
      if (snapshot.members.length) rememberSecurities(await api('/catalog/resolve', {members:snapshot.members}));
      $('#board-members').hidden = false;
      const missing = snapshot.members.filter((code) => !catalogNames.has(code));
      const labels = snapshot.members.map((code) => catalogNames.has(code) ? securityLabel(code) : `${code}（资料未收录）`);
      $('#board-members').textContent = `${row.name} · ${snapshot.members.length} 个成员 · ${formatTime(snapshot.observed_at)}${missing.length ? ` · ${missing.length} 个成员资料未收录` : ''}\n${labels.join('  ·  ')}`;
      if (d.boardRefresh !== undefined) await loadBoards(boardOffset);
    }
  }
  if (d.indexMembers !== undefined) { const row = state.indices[Number(d.indexMembers)]; rememberSecurities(await api('/catalog/resolve', {members:row.members})); $('#constituents').hidden = false; $('#constituents').textContent = `${row.name} · ${row.members.length} 个成员\n${row.members.map(securityLabel).join('  ·  ')}`; }
  if (d.indexDataset !== undefined) { const row = state.indices[Number(d.indexDataset)]; state.snapshot = row; await switchView('history'); $('#dataset-form [name="name"]').value = row.name + '成分'; $('#dataset-form [name="members"]').value = row.members.join(', '); await resolveSelections(); $('#dataset-form').scrollIntoView({ block:'center' }); }
  if (d.download) { await loadDatasets(); $('#download-form [name="dataset_id"]').value = d.download; await switchView('jobs');refreshMulti($('#download-form [name=dataset_id]')); }
  if (d.datasetDetail) {
    const dataset=(state.datasetRows||state.datasets).find(row=>row.id===d.datasetDetail);
    recordSets.dataset={title:'数据集详情',rows:[dataset],labels:{id:'数据集ID',name:'名称',source:'来源',account_id:'采集账号',endpoint:'固定端点',scheduled:'自动更新',schedule_time:'更新时间',created_at:'创建时间'},extra:row=>`<h3>周期</h3><p>${row.periods.map(value=>periodNames[value]).join('、')}</p><h3>固定成员 · ${row.members.length} 个</h3><div class="member-list">${row.members.map(code=>`<span>${escape(securityLabel(code))}</span>`).join('')}</div>`};
    showRecord('dataset');
  }
  if (d.refreshDataset) { await api('/datasets/' + d.refreshDataset + '/refresh', {}); await loadDatasets(); notice('成员已按最新快照刷新，已创建任务仍使用原成员'); }
  if (d.job) { await api('/jobs/' + d.id + '/' + d.job, {}); await loadJobs(); }
  if (d.jobDetail) {
    const request=++jobDetailRequest;
    $('#record-extra').replaceChildren();$('#record-fields').replaceChildren();$('#record-title').textContent='正在读取任务';
    const job=await api('/jobs/'+d.jobDetail);
    job.links=await api(`/jobs/${d.jobDetail}/links?limit=50&offset=${Number(d.linksOffset||0)}`);
    if(request!==jobDetailRequest)return;
    recordSets.job={title:'任务详情',rows:[job],labels:{id:'任务ID',kind:'任务类型',state:'任务状态',checkpoint:'已处理检查点',attempts:'重试次数',cancel_requested:'取消请求',error:'错误信息',created_at:'创建时间',updated_at:'更新时间'},note:`${statuses[job.state]} · ${job.payload.source || 'qmt'} · ${job.result.rows ?? 0} 行`,extra:row=>renderJobDetails(row)};
    showRecord('job');
  }
  } catch (error) { notice(error.message); }
});
async function loadBoards(offset = 0,apply=false) {
  const source=provider,query=window.Workspace?.listQuery('board-form',apply) || values($('#board-form'));
  const read=query=>api('/boards?' + new URLSearchParams({...query, limit:50,...window.Workspace?.pageOptions('board-form'),offset}));
  const result = window.Workspace?await Workspace.readList('board-form',apply,read):await read(query);
  if(!result)return;
  const request=++boardsRequest;
  if(request!==boardsRequest||source!==provider)return;
  boardRows = result.rows; boardOffset = offset; boardNext = result.next_offset;
  $('#board-page').textContent = `共 ${result.total} 个`;
  $('#board-prev').disabled = !offset; $('#board-next').disabled = boardNext === null;
  if (!boardRows.length) return empty('#board-rows',5,'尚无匹配的行业概念板块');
  $('#board-rows').innerHTML = boardRows.map((row,index) => `<tr><td>${escape(row.name)}</td><td>${row.category === 'industry' ? '行业' : '概念'}</td><td>${escape(row.path.join(' / '))}</td><td>${formatTime(row.observed_at)}</td><td><div class="actions"><button type="button" data-board-members="${index}" title="查看成员" aria-label="查看成员">${icon('list')}</button><button type="button" data-board-dataset="${index}" title="以板块成员创建数据集" aria-label="以板块成员创建数据集">${icon('folder-plus')}</button><button type="button" data-board-refresh="${index}" title="刷新板块成员" aria-label="刷新板块成员">${icon('refresh-cw')}</button></div></td></tr>`).join('');
  icons();
}
async function showInstrument(code) {
  const row = await api('/catalog/detail?' + new URLSearchParams({code}));
  const labels = {product:'品种代码', product_name:'品种名称', delivery_month:'交割年月', quote_unit:'报价单位', trade_unit:'交易计量单位', per_unit:'每手交易单位', trade_time_desc:'交易时段', underlying_code:'期权标的', underlying_market:'标的市场', strike:'行权价', option_type:'期权方向', expiry:'到期 / 退市日', created:'合约上市日', listed:'上市日', multiplier:'合约乘数', price_tick:'最小变价', option_unit:'期权单位', instrument_type:'终端类型', is_trading:'可交易标记'};
  const entries = {代码:row.code, 名称:row.name, 类别:kindNames[row.kind], 交易所:marketNames[row.market] || row.market, 类型:subtypeNames[row.subtype] || '未提供'};
  Object.entries(labels).forEach(([key, label]) => { entries[label] = row.metadata[key] ?? '未提供'; });
  $('#instrument-title').textContent = row.name;
  $('#instrument-fields').innerHTML = Object.entries(entries).map(([key,value]) => `<dt>${escape(key)}</dt><dd>${escape(typeof value === 'object' ? JSON.stringify(value) : value)}</dd>`).join('');
  $('#instrument-raw').textContent = JSON.stringify(row.details,null,2);
  $('#instrument-dialog').showModal();
}
$('#instrument-close').addEventListener('click', () => $('#instrument-dialog').close());
$('#board-form').addEventListener('submit',action(() => loadBoards(0,true)));
$('#board-prev').addEventListener('click',action(() => loadBoards(Math.max(0,boardOffset-(window.Workspace?.pageSize('board-form') || 50)))));
$('#board-next').addEventListener('click',action(() => loadBoards(boardNext)));
$('#board-sync').addEventListener('click',action(async () => { await api('/catalog/sync',{kinds:['board']}); await switchView('market'); notice('行业概念同步已排队'); }));
document.addEventListener('change', action(async (event) => { const id = event.target.dataset.schedule; if (!id) return; try { await api('/datasets/' + id + '/schedule', { enabled:event.target.checked }); } catch (error) { event.target.checked = !event.target.checked; throw error; } }));
window.addEventListener('resize', () => chart?.resize());
function today(offset = 0) { return shanghaiDay(offset); }
$('#history-form [name="start"]').value = today(-365);
$('#history-form [name="end"]').value = today();
$('#clock').textContent = new Date().toLocaleDateString('zh-CN', { timeZone:'Asia/Shanghai' }) + ' · 上海时间';
$('#history-form [name="period"]').innerHTML = Object.entries(periodNames).map(([value,label])=>`<option value="${value}">${label}</option>`).join('');
for (const id of ['dataset-form','download-form']) {
  $(`#${id} fieldset`).innerHTML = `<legend>${id==='dataset-form'?'周期':'本次回补周期'}</legend>` + Object.entries(periodNames).map(([value,label])=>`<label><input type="checkbox" name="period" value="${value}" ${id==='dataset-form'&&value==='1d'?'checked':''}>${label}</label>`).join('');
}
updateCapabilities();
icons();
initializePickers();
state.view=location.hash.slice(1) || 'market';
status().then(async () => { state.authenticated = true; await loadAccounts(); await connectSocket(); await switchView(state.view); }).catch(() => { if (!$('#login-dialog').open) $('#login-dialog').showModal(); });

async function loadAccounts() {
  const request=++accountsRequest;
  const result = await api('/sources');
  if(request!==accountsRequest)return;
  accounts = result.tushare.accounts; sourceCapabilities = result.capabilities;
  qmtReferenceCapabilities=result.sources.find(item=>item.id==='qmt')?.reference_capabilities || {};
  const jobAccount=$('#job-filter [name=account_ids]');
  if(jobAccount){const selected=new Set(selectedValues(jobAccount));jobAccount.innerHTML='<option value="">全部账号</option>'+accounts.map(account=>`<option value="${escape(account.id)}" ${selected.has(account.id)?'selected':''}>${escape(account.name)} · ${escape(account.id)}${account.enabled?'':'（已停用）'}</option>`).join('');refreshMulti(jobAccount);}
  const selected = $('#data-account').value;
  $('#data-account').innerHTML = '<option value="">选择采集账号</option>' + accounts.filter(a=>a.enabled).map(a=>`<option value="${escape(a.id)}">${escape(a.name)}${a.token_configured?'':' · Token未配置'}</option>`).join('');
  $('#data-account').value = accounts.some(a=>a.id===selected && a.enabled) ? selected : result.tushare.default_account_id;
  $('#account-rows').innerHTML = accounts.map(a=>`<tr><td>${escape(a.name)}${a.id===result.tushare.default_account_id?' · 默认':''}<span class="muted">${escape(a.id)}</span></td><td>${escape(a.endpoint)}</td><td>${a.token_configured?'已配置':'未配置'} · ${a.enabled?'启用':'停用'}${a.managed_by_env.length ? '<span class="muted">环境变量管理</span>':''}</td><td><div class="actions"><button data-account-edit="${escape(a.id)}" title="编辑账号" aria-label="编辑 ${escape(a.name)}">${icon('pencil')}</button><button data-account-test="${escape(a.id)}" title="检测接口权限" aria-label="检测 ${escape(a.name)} 接口权限">${icon('plug-zap')}</button><button data-account-delete="${escape(a.id)}" title="删除账号" aria-label="删除 ${escape(a.name)}">${icon('trash-2')}</button></div></td></tr>`).join('');
  if (!accounts.length) empty('#account-rows',4,'尚未配置 Tushare 账号');
  window.Workspace?.filterLocalRows('accounts');
  $('#account-form button[type="submit"]').disabled=false;
  updateCapabilities(); icons();
}

function updateCapabilities() {
  const cap = sourceCapabilities[$('#data-account').value]?.capabilities;
  const blocked = provider==='tushare' && ['permission','authentication','unsupported'].includes(cap?.minutes?.state);
  $$('#dataset-form input[name="period"]').forEach(input=>{
    const allowed = provider==='tushare' || ['1d','1m','5m'].includes(input.value);
    const capability=cap?.[minutePeriods.includes(input.value)?'minutes':input.value==='1w'?'weekly':input.value==='1mo'?'monthly':'daily'];
    input.closest('label').hidden=!allowed;
    input.disabled=!allowed || provider==='tushare' && ['permission','authentication','unsupported'].includes(capability?.state);
    if(input.disabled)input.checked=false;
  });
  $$('#history-form [name="period"] option').forEach(option=>{option.hidden=option.disabled=provider!=='tushare' && !['1d','1m','5m'].includes(option.value);});
  if ($('#history-form [name="period"] option:checked')?.disabled) $('#history-form [name="period"]').value='1d';
  $('#source-capability').textContent = provider==='qmt' ? '实时 / 历史' : blocked ? '期货历史 · 分钟权限不可用' : '期货历史 · ' + (cap ? '接口能力已检测' : '接口权限未检测');
  updateDownloadPeriods();
  updateFuturesForm();
  refreshMulti($('#history-form [name=period]'));refreshCheckAll();
}

async function changeSource() {
  if(window.Workspace && !await Workspace.allowSourceChange()){$('#data-source').value=provider;return;}
  window.Workspace?.sourceChanged();
  const jobAccount=$('#job-filter [name=account_ids]');if(jobAccount){[...jobAccount.options].forEach(option=>option.selected=false);refreshMulti(jobAccount);}
  provider=$('#data-source').value;
  ++futuresOptionsRequest; clearFuturesResults();
  state.watch=[]; sendWatch(); state.quotes={}; state.snapshot=null;
  catalogNames.clear(); catalogJob=null; clearTimeout(catalogTimer);
  Object.values(pickerTargets).forEach(target=>{$(target.input).value='';});
  clearHistoryResults();
  $('#account-choice').hidden=provider!=='tushare'; $('#qmt-live').hidden=provider!=='qmt'; $('#tushare-history-link').hidden=provider!=='tushare';
  $('#catalog-exchanges').hidden=provider!=='tushare';
  $('#qmt-deployment').hidden=provider!=='qmt';
  $('#qmt-state').hidden=provider!=='qmt';
  $('#market .section-heading .source').textContent=provider==='qmt'?'QMT · 国内市场':'Tushare · 国内期货';
  if(!window.Workspace){
    $$('nav [data-view="indices"],nav [data-view="boards"]').forEach(button=>{button.hidden=provider!=='qmt';});
    $('nav [data-view="futures"]').hidden=provider!=='tushare';
  }
  $$('#catalog-form input[name="kind"]').forEach(input=>{input.disabled=provider==='tushare' && input.value!=='future'; input.checked=provider==='tushare'?input.value==='future':['future','option'].includes(input.value);});
  $('#security-form [name="kind"]').value=provider==='tushare'?'future':'';
  $('#security-form [name="market"]').value=''; $('#security-form [name="subtype"]').value='';
  $$('#security-form select').forEach(refreshMulti);
  updateCatalogFilters();
  $('#catalog-active').defaultChecked=$('#catalog-active').checked=provider==='tushare'; $('#dataset-schedule-time').value=provider==='tushare'?'19:00':'17:00';
  $('footer span').textContent=provider==='tushare'?'Tushare · 不复权 / 成交量与持仓量：手 / 成交额：元':'QMT · 不复权 / 空值保留 / 原始成交单位';
  updateCapabilities(); rememberSecurities([]);
  if(window.Workspace){Workspace.rememberSource();await switchView(state.view);}
  else await switchView(['indices','boards'].includes(state.view) || state.view==='futures' && provider!=='tushare' ? 'market' : state.view);
}
$('#data-source').addEventListener('change',action(changeSource));
$('#catalog-active').addEventListener('change',action(()=>loadSecurities(0,true)));
$('#data-account').addEventListener('change',updateCapabilities);
$('#open-tushare-history').addEventListener('click',()=>switchView('history'));
$('#account-new').addEventListener('click',()=>{$('#account-form').reset();$('#account-form [name="id"]').readOnly=false;$('#account-form [name="id"]').focus();});
$('#account-form').addEventListener('submit',action(async()=>{
  const form=$('#account-form'), payload=values(form);
  for(const key of ['enabled','default','clear_token'])payload[key]=form.elements[key].checked;
  payload.timeout=Number(payload.timeout);payload.requests_per_minute=Number(payload.requests_per_minute);
  await api('/sources/tushare/accounts',payload);form.elements.token.value='';form.elements.clear_token.checked=false;
  await loadAccounts();notice('Tushare账号已保存');
}));
document.addEventListener('click',event=>{
  if(!event.target.closest('[data-account-edit],[data-account-delete],[data-account-test]'))return;
  action(async event=>{
  const button=event.target.closest('button'); if(!button)return;
  const d=button.dataset;
  if(d.accountEdit){const row=accounts.find(a=>a.id===d.accountEdit),form=$('#account-form');form.reset();for(const key of ['id','name','endpoint','timeout','requests_per_minute'])form.elements[key].value=row[key];form.elements.enabled.checked=row.enabled;form.elements.id.readOnly=true;form.scrollIntoView({block:'center'});}
  if(d.accountDelete){const account=accounts.find(row=>row.id===d.accountDelete);if(!await confirmBatch('删除 Tushare 账号',{账号:account?.name||d.accountDelete,影响:'删除本地认证配置；被数据集、维护计划或活动任务引用时不可删除'}))return;await api(`/sources/tushare/accounts/${d.accountDelete}/delete`,{});await loadAccounts();}
  if(d.accountTest){button.disabled=true;$('#account-test-status').textContent='正在检测接口权限';try{const result=await api(`/sources/tushare/accounts/${d.accountTest}/test`,{});sourceCapabilities[d.accountTest]=result;const labels={catalog:'合约目录',calendar:'期货日历',daily:'日线',minutes:'历史分钟（独立权限）',mapping:'主力映射',weekly:'周线',monthly:'月线',warehouse:'仓单日报',holding:'成交持仓排名',settle:'结算参数',weekly_detail:'主要品种交易周报',tick:'Tick'};const states={available:'可用',empty:'空结果',permission:'权限不足',authentication:'认证失败',unverified:'未验证',network:'连接失败',rate_limit:'达到限额',incomplete:'覆盖不完整',unsupported:'无API'};$('#account-capabilities').innerHTML=Object.entries(result.capabilities).map(([key,item])=>`<tr><td>${escape(labels[key] || key)}</td><td>${escape(states[item.state] || item.state)}</td><td>${escape(item.message || (item.rows+' 条'))}</td></tr>`).join('');$('#account-test-status').textContent='检测完成 · '+d.accountTest;updateCapabilities();}catch(error){$('#account-test-status').textContent='检测失败';throw error;}finally{button.disabled=false;}}
  })(event);
});
document.addEventListener('change',action(async event=>{const id=event.target.dataset.datasetAccount;if(id){await api('/datasets/'+id+'/account',{account_id:event.target.value});await loadDatasets();}}));

const exchangeMarkets={CFFEX:'IF',SHFE:'SF',DCE:'DF',CZCE:'ZF',INE:'INE',GFEX:'GF'};
const futuresFields = {ts_code:'合约代码',settle:'结算价',trading_fee_rate:'交易手续费率（原值）',trading_fee:'交易手续费（原值）',delivery_fee:'交割手续费（原值）',b_hedging_margin_rate:'买套保保证金率（原值）',s_hedging_margin_rate:'卖套保保证金率（原值）',long_margin_rate:'买投机保证金率（原值）',short_margin_rate:'卖投机保证金率（原值）',offset_today_fee:'平今仓手续率（原值）',prd:'品种代码',name:'品种名称',week:'原始周编号',week_date:'周日期',vol_yoy:'成交量同比（%）',amount:'成交金额（元）',amout_yoy:'成交额同比（%）',cumvol:'年累计成交量（手）',cumvol_yoy:'累计成交量同比（%）',cumamt:'年累计成交额（元）',cumamt_yoy:'累计成交额同比（%）',open_interest:'持仓量（手）',interest_wow:'持仓环比（%）',mc_close:'主力收盘价',close_wow:'收盘价环比（%）',original_amount:'原始成交额（亿元）',original_cumamt:'原始累计成交额（亿元）',normalization_version:'转换版本',source:'来源',market:'市场',day:'日期',is_open:'开市状态',pretrade_date:'前交易日',code:'主力 / 连续',trading_day:'交易日',member_code:'对应月份合约',exchange:'交易所',symbol:'产品 / 合约',trade_date:'交易日',fut_name:'产品名称',warehouse:'仓库',wh_id:'仓库编号',pre_vol:'昨日仓单',vol:'数量 / 成交量',vol_chg:'变化量',area:'地区',year:'年度',grade:'等级',brand:'品牌',place:'产地',pd:'升贴水',is_ct:'折算仓单',unit:'单位',broker:'期货公司会员',long_hld:'持买仓量',long_chg:'买仓变化',short_hld:'持卖仓量',short_chg:'卖仓变化'};
$('#futures-form [name="start"]').value=today(-7);
$('#futures-form [name="end"]').value=today(-1);
Object.assign(futuresFields,{observed_at:'采集时间',evidence:'日历依据'});
$('#futures-form [name=mapping_mode]').addEventListener('change',()=>{updateFuturesForm();clearFuturesResults();});
$('#qmt-reference-test').addEventListener('click',action(async()=>{
  const form=$('#futures-form'),resource=selectedValues(form.elements.resource)[0],mode=form.elements.mapping_mode.value;
  const result=await api('/sources/qmt/references/test',{resource,mapping_mode:mode});
  qmtReferenceCapabilities[resource==='calendar'?'calendar':'mapping_'+mode]=result;
  if(provider==='qmt')updateFuturesForm();
}));

function updateFuturesForm() {
  const form=$('#futures-form'), resources=selectedValues(form.elements.resource);
  const contract=resources.includes('settle') || resources.includes('holding') && form.elements.scope.value==='contract';
  $('#futures-product-field').hidden=!resources.includes('weekly_detail') && !resources.includes('warehouse') && (!resources.includes('holding') || form.elements.scope.value==='contract');
  $('#futures-mapping-field').hidden=!resources.includes('mapping');
  $('#futures-scope-field').hidden=!resources.includes('holding');
  $('#futures-contract-field').hidden=!contract;
  $('#futures-mapping-mode').hidden=provider!=='qmt'||!resources.includes('mapping');
  const current=provider==='qmt'&&resources.includes('mapping')&&form.elements.mapping_mode.value==='current';
  for(const name of ['start','end']){form.elements[name].closest('label').hidden=current;form.elements[name].required=!current;}
  form.querySelector('[data-date-preset]')?.closest('label')?.toggleAttribute('hidden',current);
  $('#qmt-reference-test').hidden=provider!=='qmt';
  const caps=sourceCapabilities[$('#data-account').value]?.capabilities || {};
  const states={available:'接口可用',permission:'接口权限不足',authentication:'认证失败',empty:'检测返回空数据',network:'连接失败',incomplete:'覆盖待核验'};
  $('#futures-capability').textContent=resources.map(resource=>`${reportNames[resource]} · ${states[caps[resource]?.state] || '接口权限未检测'}`).join('；');
  $('#futures-sync').disabled=!resources.length || resources.some(resource=>['permission','authentication','unsupported'].includes(caps[resource]?.state));
  if(provider==='qmt'){
    const key=resources[0]==='calendar'?'calendar':'mapping_'+form.elements.mapping_mode.value;
    const labels={available:'可用',limited:'有限可用',bridge_outdated:'行情桥需更新',unsupported:'终端未提供',permission:'权限不足',empty:'空响应',connection_failed:'连接失败',invalid_response:'响应待核查',unchecked:'未检测'};
    const results=qmtReferenceCapabilities[key]?.results || [];
    $('#futures-capability').textContent=results.length?'QMT资料能力 · '+formatTime(qmtReferenceCapabilities[key].checked_at):'QMT资料能力未检测；已保存资料可离线查询';
    $('#qmt-reference-results').hidden=!results.length;
    $('#qmt-reference-results').innerHTML='<table><thead><tr><th>检测对象</th><th>状态</th><th>记录数</th><th>说明</th></tr></thead><tbody>'+results.map(item=>'<tr><td>'+escape(item.target.exchange||item.target.code)+'</td><td>'+escape(labels[item.state]||item.state)+'</td><td class="numeric">'+item.rows+'</td><td class="wrap-text">'+escape(item.reason)+'</td></tr>').join('')+'</tbody></table>';
    $('#futures-sync').disabled=!resources.length;
  }else $('#qmt-reference-results').hidden=true;
  if(!resources.includes(futuresActive))futuresActive=resources[0];
  $('#futures-tabs').innerHTML=resources.map(resource=>`<button type="button" role="tab" aria-selected="${resource===futuresActive}" data-resource-tab="${resource}">${reportNames[resource]}</button>`).join('');
  window.Workspace?.updateReportDimensions();
}

async function loadFuturesOptions() {
  const request=++futuresOptionsRequest, form=$('#futures-form'),source=provider;
  const exchange=selectedValues(form.elements.exchange).join(',');
  if(!exchange){form.elements.symbol.replaceChildren();form.elements.code.replaceChildren();return;}
  const result=await api('/futures/options?'+new URLSearchParams({source,exchange}));
  if(request!==futuresOptionsRequest || provider!==source || selectedValues(form.elements.exchange).join(',')!==exchange)return;
  for(const [name,rows,key] of [['symbol',result.products,'symbol'],['code',result.continuous,'code']]){
    const select=form.elements[name],previous=new Set(selectedValues(select));
    select.innerHTML=rows.map(row=>{
      const value=name==='symbol'?row.exchange+':'+row.symbol:row.code;
      return `<option value="${escape(value)}" ${previous.has(value)?'selected':''}>${escape(marketNames[row.market] || row.exchange)} · ${escape(row[key])}${row.name?' · '+escape(row.name):''}</option>`;
    }).join('');
    refreshMulti(select);
  }
}

function futuresQuery(onlyResource,includeDimensions=true) {
  const form=$('#futures-form'), p=values(form), exchanges=selectedValues(form.elements.exchange);
  const resources=onlyResource?[onlyResource]:selectedValues(form.elements.resource), selections=[];
  if(provider==='qmt'&&resources.includes('mapping')&&p.mapping_mode==='current')p.start=p.end=today();
  if(!resources.length || !exchanges.length)throw new Error('请选择资料类型和交易所');
  if(!p.start || !p.end || p.start>p.end)throw new Error('请选择有效的开始和结束日期');
  for(const resource of resources){
    const items=[];
    if(resource==='calendar')exchanges.forEach(exchange=>items.push({resource,exchange}));
    if(resource==='mapping')selectedValues(form.elements.code).forEach(code=>{
      const market={CFX:'CFFEX',SHF:'SHFE',DCE:'DCE',ZCE:'CZCE',INE:'INE',GFE:'GFEX',IF:'CFFEX',SF:'SHFE',DF:'DCE',ZF:'CZCE',GF:'GFEX'}[code.split('.')[1]];
      if(!exchanges.includes(market))throw new Error('主力合约与所选交易所不一致');
      items.push({resource,code,...(provider==='qmt'?{mapping_mode:p.mapping_mode}:{})});
    });
    if(resource==='weekly_detail' || resource==='warehouse' || resource==='holding' && p.scope!=='contract'){
      selectedValues(form.elements.symbol).forEach(value=>{const [exchange,symbol]=value.split(':');if(exchanges.includes(exchange))items.push({resource,exchange,symbol});});
    }
    if(resource==='settle' || resource==='holding' && p.scope==='contract'){
      const markets={CFX:'CFFEX',SHF:'SHFE',DCE:'DCE',ZCE:'CZCE',INE:'INE',GFE:'GFEX'};
      splitCodes(p.contract).forEach(code=>{const [symbol,market]=code.split('.'), exchange=markets[market];if(!exchanges.includes(exchange))throw new Error('月份合约与所选交易所不一致');items.push(resource==='settle'?{resource,exchange,code}:{resource,exchange,symbol});});
    }
    if(!items.length)throw new Error('请选择'+reportNames[resource]+'的产品或合约');
    selections.push(...items);
  }
  const result={source:provider,start:p.start,end:p.end};
  const payload=selections.length===1?{...result,...selections[0]}:{...result,selections};
  const dimensions=includeDimensions?window.Workspace?.reportDimensions():null;
  if(dimensions&&Object.keys(dimensions).length)payload.dimensions=dimensions;
  return payload;
}

function clearFuturesResults() {
  if(window.Workspace)return Workspace.invalidate('futures');
  ++futuresRequest;
  $('#futures-head').replaceChildren(); empty('#futures-rows',1,'暂无查询结果');
  $('#futures-page').textContent=''; $('#futures-units').textContent='';
  $('#futures-prev').disabled=true; $('#futures-next').disabled=true;
  futuresOffset=0;futuresNext=null;
}

async function loadFutures(offset=0,apply=false) {
  if(window.Workspace)return Workspace.futures(offset,apply);
  const request=++futuresRequest, p=futuresQuery(futuresActive);
  empty('#futures-rows',1,'正在查询资料');$('#futures-rows').setAttribute('aria-busy','true');
  const result=p.selections?await api('/futures/records',{...p,limit:200,offset}):await api('/futures/records?'+new URLSearchParams({...p,limit:200,offset}));
  if(request!==futuresRequest || provider!=='tushare')return;
  $('#futures-rows').removeAttribute('aria-busy');
  futuresOffset=offset; futuresNext=result.next_offset;
  $('#futures-units').textContent=result.units;
  const columns={warehouse:['trade_date','exchange','symbol','fut_name','warehouse','vol','vol_chg','unit'],holding:['trade_date','exchange','symbol','broker','vol','long_hld','short_hld'],settle:['trade_date','exchange','ts_code','settle','trading_fee_rate','trading_fee','long_margin_rate','short_margin_rate'],weekly_detail:['week_date','week','exchange','prd','name','vol','amount','open_interest']}[result.resource] || result.fields;
  const numeric=['pre_vol','vol','vol_chg','pd','long_hld','long_chg','short_hld','short_chg','settle','trading_fee_rate','trading_fee','long_margin_rate','short_margin_rate','amount','open_interest'];
  recordSets.futures={title:reportNames[result.resource]+'详情',rows:result.rows,labels:futuresFields,note:result.units};
  $('#futures-head').innerHTML='<tr>'+columns.map(field=>`<th class="${numeric.includes(field)?'numeric':''}">${escape(futuresFields[field] || field)}</th>`).join('')+'<th>详情</th></tr>';
  $('#futures-rows').innerHTML=result.rows.map((row,index)=>'<tr>'+columns.map(field=>`<td class="${numeric.includes(field)?'numeric':''}">${field==='is_open'?`<span class="badge ${row[field]?'succeeded':''}">${row[field]?'开市':'休市'}</span>`:field==='member_code'?`<button type="button" class="text-link" data-mapped-history="${escape(row[field])}" title="查看月份合约历史" aria-label="查看 ${escape(row[field])} 历史">${escape(row[field])}${icon('arrow-up-right')}</button>`:escape(number(row[field]))}</td>`).join('')+`<td>${recordButton('futures',index)}</td></tr>`).join('');
  if(!result.rows.length)empty('#futures-rows',columns.length+1,'所选范围没有已入库资料');
  $('#futures-page').textContent=result.rows.length?`${offset+1}–${offset+result.rows.length} 条`:'0 条';
  $('#futures-prev').disabled=!offset; $('#futures-next').disabled=futuresNext===null;
  icons();
}

$('#futures-form').addEventListener('submit',action(()=>loadFutures(0,true)));
$('#futures-form').addEventListener('change',action(async event=>{
  clearFuturesResults();
  updateFuturesForm();
  if(event.target.name==='exchange')await loadFuturesOptions();
}));
$('#futures-sync').addEventListener('click',action(async()=>{
  const payload={...futuresQuery(),account_id:$('#data-account').value};
  if(payload.selections && !await confirmBatch('批量同步期货资料',{来源:'Tushare',采集账号:payload.account_id,资料类型:[...new Set(payload.selections.map(item=>reportNames[item.resource]))].join('、'),对象数:payload.selections.length,日期:`${payload.start} 至 ${payload.end}`}))return;
  const result=await api('/futures/sync',payload);
  await switchView('jobs'); notice(`${result.jobs?.length || 1} 个资料同步任务已排队`);
}));
for(const format of ['csv','parquet'])$(`#futures-${format}`).addEventListener('click',action(async()=>{
  if(window.Workspace)return Workspace.exportFutures(format);
  const payload={...futuresQuery(),format};
  if(payload.selections && !await confirmBatch('批量导出期货资料',{来源:'Tushare',资料类型:[...new Set(payload.selections.map(item=>reportNames[item.resource]))].join('、'),对象数:payload.selections.length,日期:`${payload.start} 至 ${payload.end}`,格式:format.toUpperCase()}))return;
  await api('/futures/export',payload); await switchView('jobs'); notice('资料导出已排队');
}));
$('#futures-prev').addEventListener('click',action(()=>loadFutures(Math.max(0,futuresOffset-(window.Workspace?.pageSize('futures-form') || 200)))));
$('#futures-next').addEventListener('click',action(()=>loadFutures(futuresNext)));
$('#futures-rows').addEventListener('click',action(async event=>{
  const code=event.target.closest('[data-mapped-history]')?.dataset.mappedHistory;
  if(!code)return;
  const start=$('#futures-form [name="start"]').value,end=$('#futures-form [name="end"]').value;
  await switchView('history');
  $('#history-form [name="code"]').value=code;
  $('#history-form [name="start"]').value=start;
  $('#history-form [name="end"]').value=end;
  await resolveSelections();await loadHistory(0,true);
}));

$('#futures-tabs').addEventListener('click',action(async event=>{
  const tab=event.target.closest('[data-resource-tab]');if(!tab)return;
  futuresActive=tab.dataset.resourceTab;clearFuturesResults();updateFuturesForm();await loadFutures();
}));
$('#history-form').addEventListener('change',clearHistoryResults);
$('#chart-code').addEventListener('change',action(loadChart));
$('#chart-period').addEventListener('change',action(loadChart));

function renderJobDetails(job) {
  const p=job.payload, coverage=job.result.coverage || [];
  const summary={来源:p.source || 'qmt',采集账号:p.account_id || '不适用',范围:`${p.start || '默认'} 至 ${p.end || '默认'}`,周期:(p.periods || (p.period?[p.period]:[])).map(value=>periodNames[value] || value).join('、') || reportNames[p.resource] || '目录',[job.kind==='verify'?'核验行数':'已入库行数']:job.result.rows ?? 0,固定对象数:p.selections?.length || p.members?.length || 1};
  const qualitySummary=(job.result.quality_summary || []).map(item=>`${qualityNames[item.quality_state] || item.quality_state} ${item.count} 块`).join('、');
  const verify=['download','verify'].includes(job.kind)&&!['queued','running','retrying'].includes(job.state)?`<button type="button" data-verify="${job.id}">${icon('scan-search')}只读重新核验</button>`:'';
  const repair=job.kind==='verify' && !['queued','running','retrying'].includes(job.state)?`<button type="button" data-repair="${job.id}">${icon('download')}预览缺口补数</button>`:'';
  return `<div class="toolbar"><button type="button" data-units="${job.id}">${icon('list-checks')}分块质量</button><button type="button" data-events="${job.id}">${icon('scroll-text')}任务日志</button>${verify}${repair}${['download','catalog'].includes(job.kind) && !p.retry_of && !p.maintenance_id && !p.repair_of?`<button type="button" data-maintain="${job.id}">${icon('calendar-clock')}设为自动维护</button>`:''}</div>`+'<h3>处理范围</h3><dl class="detail-grid">'+detailFields({...summary,数据质量:qualitySummary || '旧任务未记录分块质量',核验规则:job.result.rule_version || '旧版本',下一步:job.action || '查看分块质量与日志',原任务:job.parent_id || p.verification_of || p.repair_of || '无'})+'</dl>'+(job.error?`<p class="error-note">${escape(job.error)}</p>`:'')+(coverage.length?'<h3>覆盖与缺口 · '+coverage.length+' 个分块</h3><div class="table-wrap coverage-table"><table><thead><tr><th>对象</th><th>周期</th><th>请求区间</th><th>行数</th><th>校验结果</th></tr></thead><tbody>'+coverage.map(row=>`<tr><td>${escape(row.code)}</td><td>${escape(periodNames[row.period] || reportNames[row.period] || row.period)}</td><td>${escape(row.requested_start)} 至 ${escape(row.requested_end)}</td><td class="numeric">${row.row_count}</td><td class="wrap-text">${row.gaps?.length?row.gaps.map(gap=>escape((gap.day?gap.day+' · ':'')+gap.reason)).join('<br>'):'已执行覆盖校验'}</td></tr>`).join('')+'</tbody></table></div>':'')+renderJobLinks(job);
}

initializeControls();
