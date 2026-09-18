'use strict';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { view: 'market', indices: [], datasets: [], jobs: [], quotes: {}, socket: null, offset: 0, next: null, snapshot: null, authenticated: false, reconnect: null, connecting: false, watch: [] };
let catalogTimer, catalogJob, catalogSectors = [], securityOffset = 0, securityNext = null;
const names = { market: '行情', indices: '指数', boards:'行业概念', history: '历史库', jobs: '下载任务', settings: '数据源设置' };
let boardRows = [], boardOffset = 0, boardNext = null;
const statuses = { queued: '排队中', running: '运行中', completed: '已完成', partial: '待核验', failed: '失败', cancelled: '已取消' };
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const icon = (name) => `<i data-lucide="${name}"></i>`;
const icons = () => window.lucide?.createIcons();
const values = (form) => Object.fromEntries(new FormData(form));
const splitCodes = (value) => value.split(/[,，\r\n]+/).map((code) => code.trim()).filter(Boolean);
const formatTime = (value) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';
const number = (value) => value === undefined || value === null ? '—' : String(value);
const empty = (id, cols, message) => { $(id).innerHTML = `<tr><td colspan="${cols}" class="empty">${escape(message)}</td></tr>`; };
let chart;
let provider = 'qmt', accounts = [], sourceCapabilities = {};
let accountsRequest=0, datasetsRequest=0, jobsRequest=0, securitiesRequest=0, catalogRequest=0, historyRequest=0;

function notice(message) {
  $('#notice').textContent = message;
  $('#notice').hidden = !message;
}

async function api(path, payload) {
  const scoped = ['/catalog','/catalog/securities','/catalog/resolve','/catalog/detail','/catalog/sync','/history','/exports','/securities'];
  const route = path.split('?')[0];
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
  return result;
}

function action(callback) {
  return async (event) => {
    event?.preventDefault();
    const button = event?.submitter || (event?.currentTarget?.tagName === 'BUTTON' ? event.currentTarget : null);
    if (button) button.disabled = true;
    try { await callback(event); } catch (error) { notice(error.message); }
    finally { if (button) button.disabled = button.matches('#catalog-form button[type="submit"]') && ['queued','running'].includes(catalogJob?.state); icons(); }
  };
}

async function status() {
  const result = await api('/status');
  $('#db-state').textContent = result.database.connected ? '历史库已连接' : '历史库未连接';
  $('#db-state').classList.toggle('online', result.database.connected);
  $('#db-configured').textContent = result.settings.database_configured ? '已保存连接信息' : '尚未配置';
  $('#settings-form [name="qmt_root"]').value = result.settings.qmt_root;
  $('#settings-form [name="login_enabled"]').checked = result.settings.login_enabled;
  $('#settings-form [name="password"]').disabled = !result.settings.login_enabled;
  return result;
}

async function switchView(view) {
  if (!names[view]) view = 'market';
  state.view = view;
  $$('.view').forEach((item) => { item.hidden = item.id !== view; });
  $$('nav button').forEach((item) => { item.classList.toggle('active', item.dataset.view === view); item.setAttribute('aria-current', item.dataset.view === view ? 'page' : 'false'); });
  $('#view-title').textContent = names[view];
  history.replaceState(null, '', '#' + view);
  notice('');
  if (!state.authenticated) return;
  try {
    if (view === 'indices') { await loadCatalog(); await loadIndices(); }
    if (view === 'boards') await loadBoards();
    if (view === 'history') { await loadDatasets(); chart?.resize(); }
    if (view === 'jobs') { await loadDatasets(); await loadJobs(); }
    if (view === 'settings') { await status(); await loadAccounts(); }
    if (view === 'market') { await loadCatalog(); await loadSecurities(); }
    await resolveSelections();
  } catch (error) { notice(error.message); }
}

async function loadSecurities(offset = 0) {
  const request=++securitiesRequest, selectedSource=provider;
  const result = await api('/catalog/securities?' + new URLSearchParams({ ...values($('#security-form')), active:$('#catalog-active').checked, limit:50, offset }));
  if(request!==securitiesRequest || selectedSource!==provider)return;
  const rows = result.rows;
  rememberSecurities(rows);
  securityOffset = offset; securityNext = result.next_offset;
  $('#security-prev').disabled = !offset; $('#security-next').disabled = securityNext === null;
  $('#security-page-status').textContent = `共 ${result.total} 个${rows.length ? ` · ${offset + 1}–${offset + rows.length}` : ''}`;
  if (!rows.length) return empty('#securities', 6, '没有匹配证券');
  $('#securities').innerHTML = rows.map((row) => `<tr><td>${escape(row.code)}</td><td>${escape(row.name)}</td><td>${escape(kindNames[row.kind])}<span class="muted">${escape(subtypeNames[row.subtype] || '')}</span></td><td>${escape(marketNames[row.market] || row.market)}</td><td>${formatTime(row.updated_at)}</td><td><button type="button" class="icon" data-instrument="${escape(row.code)}" title="查看合约资料" aria-label="查看 ${escape(row.name)} 资料">${icon('file-search')}</button></td></tr>`).join('');
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
  const wasActive = catalogJob && ['queued','running'].includes(catalogJob.state);
  catalogJob = result.job;
  const active = catalogJob && ['queued','running'].includes(catalogJob.state);
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
  $('#quotes').innerHTML = rows.map(([code, incoming]) => {
    const row = Array.isArray(incoming) ? incoming.at(-1) || {} : incoming;
    const price = row.lastPrice ?? row.last ?? row.close;
    const previous = row.lastClose ?? row.preClose;
    const change = price !== undefined && previous ? (Number(price) / Number(previous) - 1) * 100 : null;
    const tone = change === null || change === 0 ? '' : change > 0 ? 'positive' : 'negative';
    return `<tr><td>${escape(catalogNames.get(code)?.name || code)}<span class="muted">${escape(code)}</span></td><td class="${tone}">${escape(number(price))}</td><td>${escape(number(previous))}</td><td class="${tone}">${change === null ? '—' : change.toFixed(2) + '%'}</td><td>${escape(number(row.lastSettlementPrice ?? row.preSettlementPrice))}</td><td>${escape(number(row.openInt ?? row.openInterest))}</td><td>${escape(number(row.volume))}</td><td>${escape(number(row.amount))}</td><td>${row.time ? formatTime(row.time) : '—'}</td></tr>`;
  }).join('');
}

async function connectSocket() {
  if (!state.authenticated || state.connecting || [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.socket?.readyState)) return;
  clearTimeout(state.reconnect);
  state.connecting = true;
  try {
    const ticket = await api('/ws-ticket', {});
    if (!state.authenticated) return;
    const socket = new WebSocket(`ws://127.0.0.1:${ticket.port}/?ticket=${encodeURIComponent(ticket.ticket)}`);
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
      if (message.event === 'job' && state.view === 'jobs') loadJobs().catch((error) => notice(error.message));
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
  icons();
}

async function loadDatasets() {
  const request=++datasetsRequest;
  const rows=await api('/datasets');
  if(request!==datasetsRequest)return;
  state.datasets=rows;
  const select = $('#download-form [name="dataset_id"]');
  const selected = select.value;
  select.innerHTML = '<option value="">选择数据集</option>' + state.datasets.map((row) => `<option value="${row.id}">${escape(row.name)} · ${escape(row.source)} · ${row.members.length} 个证券</option>`).join('');
  select.value = selected;
  updateDownloadPeriods();
  if (!state.datasets.length) return empty('#datasets', 5, '暂无数据集');
  $('#datasets').innerHTML = state.datasets.map((row) => `<tr><td>${escape(row.name)}${row.index_code || row.board_name ? `<span class="muted">${escape(row.board_name || row.index_code)} 当前成分快照</span>` : ''}</td><td>${row.members.length}</td><td>${row.periods.join(' / ')}</td><td><input type="checkbox" data-schedule="${row.id}" aria-label="${escape(row.name)} 交易日自动更新" ${row.scheduled ? 'checked' : ''}></td><td><div class="actions"><button data-download="${row.id}" aria-label="下载此数据集" title="下载此数据集">${icon('download')}</button>${row.index_code || row.board_name ? `<button data-refresh-dataset="${row.id}" title="显式刷新成员" aria-label="显式刷新成员">${icon('refresh-cw')}</button>` : ''}</div></td></tr>`).join('');
  icons();
  $$('#datasets tr').forEach((tr,index) => {
    const row = state.datasets[index];
    tr.children[0].insertAdjacentHTML('beforeend',`<span class="muted">${escape(row.source)}${row.account_id ? ' · ' + escape(row.account_id) : ''}</span>`);
    tr.children[3].insertAdjacentHTML('beforeend',`<span class="muted">${escape(row.schedule_time?.slice(0,5) || '17:00')}</span>`);
    if (row.source === 'tushare') tr.children[4].insertAdjacentHTML('beforeend',`<select data-dataset-account="${row.id}" aria-label="${escape(row.name)} 采集账号">${accounts.filter(a=>a.enabled).map(a=>`<option value="${escape(a.id)}" ${a.id===row.account_id?'selected':''}>${escape(a.name)}</option>`).join('')}</select>`);
  });
}

function updateDownloadPeriods() {
  const form = $('#download-form');
  const dataset = state.datasets.find(row => row.id === form.elements.dataset_id.value);
  const selection = dataset ? dataset.id + ':' + (dataset.account_id || '') : '';
  const reset = form.dataset.selection !== selection;
  form.dataset.selection = selection;
  const cap = sourceCapabilities[dataset?.account_id]?.capabilities?.minutes;
  const blocked = dataset?.source === 'tushare' && ['permission','authentication','unsupported'].includes(cap?.state);
  $$('#download-form input[name="period"]').forEach(input => {
    const allowed = dataset?.periods.includes(input.value);
    if (reset) input.checked = Boolean(allowed);
    input.disabled = !allowed || blocked && input.value !== '1d';
    if (input.disabled) input.checked = false;
  });
  $('#download-capability').hidden = !dataset;
  $('#download-capability').textContent = dataset?.source === 'tushare'
    ? `Tushare · 采集账号 ${dataset.account_id} · ${blocked ? '历史分钟权限不可用' : cap?.state === 'available' ? '历史分钟接口可用' : '历史分钟权限待检测'}`
    : 'QMT';
}

async function loadHistory(offset = 0) {
  const request=++historyRequest, selectedSource=provider;
  const query = values($('#history-form'));
  const result = await api('/history?' + new URLSearchParams({ ...query, limit: 300, offset }));
  if(request!==historyRequest || selectedSource!==provider)return;
  $('#history-units').textContent = `${result.provider || 'qmt'} · 成交量：${result.units.volume} · 成交额：${result.units.amount} · 持仓量：${result.units.open_interest} · ${result.date_basis}`;
  const headings=$$('#history table')[0].querySelectorAll('th');
  [[6,'成交量','volume'],[7,'成交额','amount'],[8,'持仓量','open_interest']].forEach(([index,label,field])=>{headings[index].textContent=label+' · '+(provider==='tushare'?result.units[field]:'原始单位');});
  state.offset = offset;
  state.next = result.next_offset;
  $('#prev-page').disabled = !offset;
  $('#next-page').disabled = state.next === null;
  $('#page-status').textContent = result.rows.length ? `${offset + 1}–${offset + result.rows.length} 条` : '0 条';
  $('#chart-empty').hidden = result.rows.length > 0;
  if (!result.rows.length) empty('#bars', 11, '所选范围没有入库行情');
  else $('#bars').innerHTML = result.rows.map((row) => `<tr><td>${formatTime(row.time)}</td><td>${escape(row.trading_day || '待核验')}</td>${['open','high','low','close','volume','amount','open_interest','settlement','previous_settlement'].map((field) => `<td>${escape(number(row[field]))}</td>`).join('')}</tr>`).join('');
  if (!chart) chart = echarts.init($('#chart'));
  chart.setOption({ animation: false, grid: { left: 65, right: 25, top: 25, bottom: 60 }, tooltip: { trigger: 'axis' }, xAxis: { type: 'category', data: result.rows.map((row) => query.period === '1d' ? row.time.slice(0,10) : row.time.slice(0,16).replace('T',' ')), axisLine: { lineStyle: { color: '#c4cec7' } }, axisLabel: { color: '#819086', fontSize: 10 } }, yAxis: { scale: true, splitLine: { lineStyle: { color: '#edf1ee' } }, axisLabel: { color: '#819086', fontSize: 10 } }, dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 10, height: 18, borderColor: '#e4eae6' }], series: [{ type: 'candlestick', name: query.code, data: result.rows.map((row) => [row.open,row.close,row.low,row.high].map((value) => value === null ? '-' : Number(value))), itemStyle: { color: '#cd5656', color0: '#2c9375', borderColor: '#cd5656', borderColor0: '#2c9375' } }] }, true);
  chart.resize();
}

async function loadJobs() {
  const request=++jobsRequest;
  const rows=await api('/jobs');
  const health = await api('/status');
  if(request!==jobsRequest)return;
  state.jobs=rows;
  $('#worker-status').textContent = [health.worker && `QMT调度：${health.worker}`, health.export_worker && `文件导出：${health.export_worker}`, health.catalog_worker && `QMT目录：${health.catalog_worker}`, health.tushare_worker && `Tushare调度：${health.tushare_worker}`, health.tushare_catalog_worker && `Tushare目录：${health.tushare_catalog_worker}`].filter(Boolean).join('；');
  $('#worker-status').hidden = !$('#worker-status').textContent;
  if (!state.jobs.length) return empty('#job-rows', 6, '暂无下载或导出任务');
  $('#job-rows').innerHTML = state.jobs.map((job) => {
    const total = job.total_chunks;
    const unit = job.kind === 'catalog' ? job.payload.source === 'tushare' ? '批次' : '证券' : '分块';
    return `<tr><td>${job.id.slice(0,8)}<span class="muted">${formatTime(job.created_at)}</span></td><td>${job.kind === 'download' ? '历史回补' : job.kind === 'catalog' ? '目录同步' : job.payload.format.toUpperCase()}<span class="muted">${escape(job.payload.source || 'qmt')}${job.payload.periods ? ' · ' + escape(job.payload.periods.join(' / ')) : ''}</span></td><td><span class="badge ${job.state}">${statuses[job.state] || escape(job.state)}</span></td><td>${total ? `<progress value="${job.checkpoint}" max="${total}"></progress><span class="muted">${job.checkpoint} / ${total} ${unit}</span>` : job.checkpoint + ' 行'}</td><td>${job.result.rows === undefined ? '—' : job.kind === 'download' ? '已入库 ' + job.result.rows + ' 行' : job.result.rows + ' 行'}${job.error ? `<span class="muted">${escape(job.error)}</span>` : ''}</td><td><div class="actions"><button data-job-detail="${job.id}" title="任务详情" aria-label="任务详情">${icon('list')}</button>${['queued','running'].includes(job.state) ? `<button data-job="cancel" data-id="${job.id}" title="取消后续处理" aria-label="取消后续处理">${icon('square')}</button>` : ''}${['failed','cancelled','partial'].includes(job.state) ? `<button data-job="retry" data-id="${job.id}" title="重试" aria-label="重试">${icon('rotate-cw')}</button>` : ''}${job.kind === 'export' && job.state === 'completed' ? `<a href="/api/v1/files/${job.id}" title="下载文件" aria-label="下载文件">${icon('download')}</a><a href="/api/v1/files/${job.id}/metadata" title="下载口径说明" aria-label="下载口径说明">${icon('file-text')}</a>` : ''}</div></td></tr>`;
  }).join('');
  icons();
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
$('#security-form').addEventListener('submit', action(() => loadSecurities()));
$('#security-prev').addEventListener('click', action(() => loadSecurities(Math.max(0, securityOffset - 50))));
$('#security-next').addEventListener('click', action(() => loadSecurities(securityNext)));
$('#catalog-form').addEventListener('submit', action(async () => {
  const kinds = new FormData($('#catalog-form')).getAll('kind');
  if (!kinds.length) throw new Error('请选择至少一个目录类别');
  await api('/catalog/sync', { kinds });
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
$('#history-form').addEventListener('submit', action(() => loadHistory(0)));
$('#prev-page').addEventListener('click', action(() => loadHistory(Math.max(0,state.offset-300))));
$('#next-page').addEventListener('click', action(() => loadHistory(state.next)));
for (const format of ['csv','parquet']) $(`#export-${format}`).addEventListener('click', action(async () => { const p = values($('#history-form')); await api('/exports', { ...p, members:[p.code], format }); await switchView('jobs'); notice('导出任务已创建'); }));
$('#dataset-form').addEventListener('submit', action(async () => { const form = $('#dataset-form'); const p = values(form); const selected = new FormData(form).getAll('period'); if (!p.members) throw new Error('请选择数据集成员'); if (!selected.length) throw new Error('请选择至少一个周期'); const extra = state.snapshot ? state.snapshot.board ? {board_name:state.snapshot.name, board_snapshot_id:state.snapshot.id} : { index_code:state.snapshot.code, snapshot_id:state.snapshot.snapshot_id } : {}; await api('/datasets', { name:p.name, members:splitCodes(p.members), periods:selected, schedule_time:$('#dataset-schedule-time').value, ...extra }); state.snapshot = null; form.reset(); $('#dataset-form [name="members"]').value = ''; rememberSecurities([]); await loadDatasets(); notice('数据集已创建'); }));
$('#download-form [name="dataset_id"]').addEventListener('change', updateDownloadPeriods);
$('#download-form').addEventListener('submit', action(async () => {
  const form = $('#download-form'), p = values(form);
  const periods = new FormData(form).getAll('period');
  if (!periods.length) throw new Error('请选择至少一个回补周期');
  await api('/downloads', { dataset_id:p.dataset_id, periods, start:p.start || null, end:p.end || null });
  await loadJobs(); notice('回补任务已排队');
}));
$('#refresh-jobs').addEventListener('click', action(loadJobs));
$('#settings-form [name="login_enabled"]').addEventListener('change', (event) => { $('#settings-form [name="password"]').disabled = !event.target.checked; });
$('#settings-form').addEventListener('submit', action(async () => { const p = values($('#settings-form')); p.login_enabled = $('#settings-form [name="login_enabled"]').checked; await api('/settings', p); $('#settings-form [name="dsn"]').value = ''; $('#settings-form [name="password"]').value = ''; await status(); notice('本地配置已保存'); }));
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
  if (!['indexMembers','indexDataset','download','refreshDataset','job','jobDetail','instrument','boardMembers','boardDataset','boardRefresh'].some((key) => key in d)) return;
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
  if (d.download) { await loadDatasets(); $('#download-form [name="dataset_id"]').value = d.download; await switchView('jobs'); }
  if (d.refreshDataset) { await api('/datasets/' + d.refreshDataset + '/refresh', {}); await loadDatasets(); notice('成员已按最新快照刷新，已创建任务仍使用原成员'); }
  if (d.job) { await api('/jobs/' + d.id + '/' + d.job, {}); await loadJobs(); }
  if (d.jobDetail) { const job = await api('/jobs/' + d.jobDetail); $('#job-detail').hidden = false; $('#job-detail').open = true; $('#job-detail pre').textContent = JSON.stringify(job,null,2); }
  } catch (error) { notice(error.message); }
});
async function loadBoards(offset = 0) {
  const result = await api('/boards?' + new URLSearchParams({...values($('#board-form')), offset, limit:50}));
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
$('#board-form').addEventListener('submit',action(() => loadBoards()));
$('#board-prev').addEventListener('click',action(() => loadBoards(Math.max(0,boardOffset-50))));
$('#board-next').addEventListener('click',action(() => loadBoards(boardNext)));
$('#board-sync').addEventListener('click',action(async () => { await api('/catalog/sync',{kinds:['board']}); await switchView('market'); notice('行业概念同步已排队'); }));
document.addEventListener('change', action(async (event) => { const id = event.target.dataset.schedule; if (!id) return; try { await api('/datasets/' + id + '/schedule', { enabled:event.target.checked }); } catch (error) { event.target.checked = !event.target.checked; throw error; } }));
window.addEventListener('resize', () => chart?.resize());
function today(offset = 0) { const date = new Date(); date.setDate(date.getDate()+offset); return date.toLocaleDateString('en-CA', { timeZone:'Asia/Shanghai' }); }
$('#history-form [name="start"]').value = today(-365);
$('#history-form [name="end"]').value = today();
$('#clock').textContent = new Date().toLocaleDateString('zh-CN', { timeZone:'Asia/Shanghai' }) + ' · 上海时间';
icons();
initializePickers();
switchView(location.hash.slice(1) || 'market');
status().then(async () => { state.authenticated = true; await loadAccounts(); await connectSocket(); await switchView(state.view); }).catch(() => { if (!$('#login-dialog').open) $('#login-dialog').showModal(); });

async function loadAccounts() {
  const request=++accountsRequest;
  const result = await api('/sources');
  if(request!==accountsRequest)return;
  accounts = result.tushare.accounts; sourceCapabilities = result.capabilities;
  const selected = $('#data-account').value;
  $('#data-account').innerHTML = '<option value="">选择采集账号</option>' + accounts.filter(a=>a.enabled).map(a=>`<option value="${escape(a.id)}">${escape(a.name)}${a.token_configured?'':' · Token未配置'}</option>`).join('');
  $('#data-account').value = accounts.some(a=>a.id===selected && a.enabled) ? selected : result.tushare.default_account_id;
  $('#account-rows').innerHTML = accounts.map(a=>`<tr><td>${escape(a.name)}${a.id===result.tushare.default_account_id?' · 默认':''}<span class="muted">${escape(a.id)}</span></td><td>${escape(a.endpoint)}</td><td>${a.token_configured?'已配置':'未配置'} · ${a.enabled?'启用':'停用'}${a.managed_by_env.length ? '<span class="muted">环境变量管理</span>':''}</td><td><div class="actions"><button data-account-edit="${escape(a.id)}" title="编辑账号" aria-label="编辑 ${escape(a.name)}">${icon('pencil')}</button><button data-account-test="${escape(a.id)}" title="检测接口权限" aria-label="检测 ${escape(a.name)} 接口权限">${icon('plug-zap')}</button><button data-account-delete="${escape(a.id)}" title="删除账号" aria-label="删除 ${escape(a.name)}">${icon('trash-2')}</button></div></td></tr>`).join('');
  if (!accounts.length) empty('#account-rows',4,'尚未配置 Tushare 账号');
  $('#account-form button[type="submit"]').disabled=false;
  updateCapabilities(); icons();
}

function updateCapabilities() {
  const cap = sourceCapabilities[$('#data-account').value]?.capabilities;
  const blocked = provider==='tushare' && ['permission','authentication','unsupported'].includes(cap?.minutes?.state);
  $$('#dataset-form input[name="period"]').forEach(input=>{ if(input.value!=='1d') { input.disabled=blocked; if(blocked)input.checked=false; } });
  $('#source-capability').textContent = provider==='qmt' ? '实时 / 历史' : blocked ? '期货历史 · 分钟权限不可用' : '期货历史 · ' + (cap ? '接口能力已检测' : '接口权限未检测');
  updateDownloadPeriods();
}

$('#data-source').addEventListener('change',action(async () => {
  provider=$('#data-source').value;
  state.watch=[]; sendWatch(); state.quotes={}; state.snapshot=null;
  catalogNames.clear(); catalogJob=null; clearTimeout(catalogTimer);
  Object.values(pickerTargets).forEach(target=>{$(target.input).value='';});
  chart?.clear(); empty('#bars',11,'请选择证券查询'); $('#chart-empty').hidden=false; $('#history-units').textContent='';
  $('#account-choice').hidden=provider!=='tushare'; $('#qmt-live').hidden=provider!=='qmt'; $('#tushare-history-link').hidden=provider!=='tushare';
  $('#qmt-deployment').hidden=provider!=='qmt';
  $('#qmt-state').hidden=provider!=='qmt';
  $('#market .section-heading .source').textContent=provider==='qmt'?'QMT · 国内市场':'Tushare · 国内期货';
  $$('nav [data-view="indices"],nav [data-view="boards"]').forEach(button=>{button.hidden=provider!=='qmt';});
  $$('#catalog-form input[name="kind"]').forEach(input=>{input.disabled=provider==='tushare' && input.value!=='future'; input.checked=provider==='tushare'?input.value==='future':['future','option'].includes(input.value);});
  $('#security-form [name="kind"]').value=provider==='tushare'?'future':'';
  $('#security-form [name="market"]').value=''; $('#security-form [name="subtype"]').value='';
  $('#catalog-active').checked=provider==='tushare'; $('#dataset-schedule-time').value=provider==='tushare'?'19:00':'17:00';
  $('footer span').textContent=provider==='tushare'?'Tushare · 不复权 / 成交量与持仓量：手 / 成交额：元':'QMT · 不复权 / 空值保留 / 原始成交单位';
  updateCapabilities(); rememberSecurities([]);
  await switchView(['indices','boards'].includes(state.view)?'market':state.view);
}));
$('#catalog-active').addEventListener('change',action(()=>loadSecurities()));
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
  if(d.accountDelete){await api(`/sources/tushare/accounts/${d.accountDelete}/delete`,{});await loadAccounts();}
  if(d.accountTest){button.disabled=true;$('#account-test-status').textContent='正在检测接口权限';try{const result=await api(`/sources/tushare/accounts/${d.accountTest}/test`,{});sourceCapabilities[d.accountTest]=result;const labels={catalog:'合约目录',calendar:'期货日历',daily:'日线',minutes:'历史分钟',mapping:'主力映射'};const states={available:'可用',empty:'空结果',permission:'权限不足',authentication:'认证失败',unverified:'未验证',network:'连接失败',rate_limit:'达到限额',incomplete:'覆盖不完整'};$('#account-capabilities').innerHTML=Object.entries(result.capabilities).map(([key,item])=>`<tr><td>${escape(labels[key])}</td><td>${escape(states[item.state] || item.state)}</td><td>${escape(item.message || (item.rows+' 条'))}</td></tr>`).join('');$('#account-test-status').textContent='检测完成 · '+d.accountTest;updateCapabilities();}catch(error){$('#account-test-status').textContent='检测失败';throw error;}finally{button.disabled=false;}}
  })(event);
});
document.addEventListener('change',action(async event=>{const id=event.target.dataset.datasetAccount;if(id){await api('/datasets/'+id+'/account',{account_id:event.target.value});await loadDatasets();}}));
