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

function notice(message) {
  $('#notice').textContent = message;
  $('#notice').hidden = !message;
}

async function api(path, payload) {
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
    if (view === 'settings') await status();
    if (view === 'market') { await loadCatalog(); await loadSecurities(); }
    await resolveSelections();
  } catch (error) { notice(error.message); }
}

async function loadSecurities(offset = 0) {
  const result = await api('/catalog/securities?' + new URLSearchParams({ ...values($('#security-form')), limit:50, offset }));
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
  clearTimeout(catalogTimer);
  if (!state.authenticated) return;
  const result = await api('/catalog');
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
  state.datasets = await api('/datasets');
  const select = $('#download-form [name="dataset_id"]');
  const selected = select.value;
  select.innerHTML = '<option value="">选择数据集</option>' + state.datasets.map((row) => `<option value="${row.id}">${escape(row.name)} · ${row.members.length} 个证券</option>`).join('');
  select.value = selected;
  if (!state.datasets.length) return empty('#datasets', 5, '暂无数据集');
  $('#datasets').innerHTML = state.datasets.map((row) => `<tr><td>${escape(row.name)}${row.index_code || row.board_name ? `<span class="muted">${escape(row.board_name || row.index_code)} 当前成分快照</span>` : ''}</td><td>${row.members.length}</td><td>${row.periods.join(' / ')}</td><td><input type="checkbox" data-schedule="${row.id}" aria-label="${escape(row.name)} 交易日自动更新" ${row.scheduled ? 'checked' : ''}></td><td><div class="actions"><button data-download="${row.id}" aria-label="下载此数据集" title="下载此数据集">${icon('download')}</button>${row.index_code || row.board_name ? `<button data-refresh-dataset="${row.id}" title="显式刷新成员" aria-label="显式刷新成员">${icon('refresh-cw')}</button>` : ''}</div></td></tr>`).join('');
  icons();
}

async function loadHistory(offset = 0) {
  const query = values($('#history-form'));
  const result = await api('/history?' + new URLSearchParams({ ...query, limit: 300, offset }));
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
  state.jobs = await api('/jobs');
  const health = await api('/status');
  $('#worker-status').textContent = [health.worker && `行情调度：${health.worker}`, health.export_worker && `文件导出：${health.export_worker}`, health.catalog_worker && `目录同步：${health.catalog_worker}`].filter(Boolean).join('；');
  $('#worker-status').hidden = !$('#worker-status').textContent;
  if (!state.jobs.length) return empty('#job-rows', 6, '暂无下载或导出任务');
  $('#job-rows').innerHTML = state.jobs.map((job) => {
    const total = job.total_chunks;
    return `<tr><td>${job.id.slice(0,8)}<span class="muted">${formatTime(job.created_at)}</span></td><td>${job.kind === 'download' ? '历史回补' : job.kind === 'catalog' ? '目录同步' : job.payload.format.toUpperCase()}</td><td><span class="badge ${job.state}">${statuses[job.state] || escape(job.state)}</span></td><td>${total ? `<progress value="${job.checkpoint}" max="${total}"></progress><span class="muted">${job.checkpoint} / ${total} ${job.kind === 'catalog' ? '证券' : '分块'}</span>` : job.checkpoint + ' 行'}</td><td>${job.result.rows === undefined ? '—' : job.result.rows + ' 行'}${job.error ? `<span class="muted">${escape(job.error.slice(0,60))}</span>` : ''}</td><td><div class="actions"><button data-job-detail="${job.id}" title="任务详情" aria-label="任务详情">${icon('list')}</button>${['queued','running'].includes(job.state) ? `<button data-job="cancel" data-id="${job.id}" title="取消后续处理" aria-label="取消后续处理">${icon('square')}</button>` : ''}${['failed','cancelled','partial'].includes(job.state) ? `<button data-job="retry" data-id="${job.id}" title="重试" aria-label="重试">${icon('rotate-cw')}</button>` : ''}${job.kind === 'export' && job.state === 'completed' ? `<a href="/api/v1/files/${job.id}" title="下载文件" aria-label="下载文件">${icon('download')}</a><a href="/api/v1/files/${job.id}/metadata" title="下载口径说明" aria-label="下载口径说明">${icon('file-text')}</a>` : ''}</div></td></tr>`;
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
$('#dataset-form').addEventListener('submit', action(async () => { const form = $('#dataset-form'); const p = values(form); const selected = new FormData(form).getAll('period'); if (!p.members) throw new Error('请选择数据集成员'); if (!selected.length) throw new Error('请选择至少一个周期'); const extra = state.snapshot ? state.snapshot.board ? {board_name:state.snapshot.name, board_snapshot_id:state.snapshot.id} : { index_code:state.snapshot.code, snapshot_id:state.snapshot.snapshot_id } : {}; await api('/datasets', { name:p.name, members:splitCodes(p.members), periods:selected, ...extra }); state.snapshot = null; form.reset(); $('#dataset-form [name="members"]').value = ''; rememberSecurities([]); await loadDatasets(); notice('数据集已创建'); }));
$('#download-form').addEventListener('submit', action(async () => { const p = values($('#download-form')); await api('/downloads', { ...p, start:p.start || null, end:p.end || null }); await loadJobs(); notice('回补任务已排队'); }));
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
  if (d.download) { await switchView('jobs'); $('#download-form [name="dataset_id"]').value = d.download; }
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
  const labels = {product:'品种代码', product_name:'品种名称', underlying_code:'期权标的', underlying_market:'标的市场', strike:'行权价', option_type:'期权方向', expiry:'到期 / 退市日', created:'合约上市日', listed:'上市日', multiplier:'合约乘数', price_tick:'最小变价', option_unit:'期权单位', instrument_type:'终端类型', is_trading:'可交易标记'};
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
status().then(async () => { state.authenticated = true; await connectSocket(); await switchView(state.view); }).catch(() => { if (!$('#login-dialog').open) $('#login-dialog').showModal(); });
