'use strict';

// Pages reuse the existing forms, selectors and task actions; each has one purpose.
window.Workspace = (() => {
  const groups=[
    ['数据查询',[['catalog','证券目录','library'],['quotes','实时行情','activity'],['history','历史行情','chart-candlestick'],['indices','指数成分','list-tree'],['boards','行业概念','network']]],
    ['期货资料',[['calendar','交易日历','calendar-days'],['mapping','主力连续映射','git-compare-arrows'],['warehouse','仓单日报','warehouse'],['holding','成交持仓排名','chart-bar'],['settle','结算参数','calculator'],['weekly_detail','交易周报','chart-no-axes-combined']]],
    ['采集管理',[['datasets','数据集','folders'],['collect','采集执行','download'],['jobs','任务记录','list-checks'],['exports','导出记录','files'],['maintenance','自动维护','calendar-clock']]],
    ['质量运维',[['overview','运行概览','gauge'],['quality','数据质量','shield-check'],['events','任务日志','scroll-text'],['runtime','服务日志','terminal']]],
    ['系统设置',[['accounts','Tushare 账号','key-round'],['qmt','QMT 连接与部署','plug'],['database','数据库','database'],['access','访问与服务','settings-2']]]
  ];
  const aliases={market:'catalog',futures:'calendar',operations:'overview',settings:'accounts'};
  const pages=Object.fromEntries(groups.flatMap(([group,items])=>items.map(([id,title])=>[id,{title,group}])));
  const backing={catalog:'market',quotes:'qmt-live',datasets:'dataset-page',collect:'collect-page',accounts:'settings',qmt:'qmt-page',database:'database-page',access:'access-page',exports:'jobs'};
  Object.keys(reportNames).forEach(key=>backing[key]='futures');
  let route='',routeRequest=0,detailRequest=0,activeJob=null,plans=[],dirtyForm=null,dirtyBaseline='',leaveResolve;
  const contexts=new Map(),applied=new Map(),options=new Map(),charts=new Map(),reportColumns=new Map();
  let taskOrigin='jobs';
  let taskJobId=null,currentTaskTab='summary';
  const taskApplied=new Map();
  const reportDimensionFields={warehouse:{warehouse:'仓库',unit:'单位'},holding:{broker:'会员'}};
  const dimensionScopes=new Map();let dimensionRequest=0;
  const selectedCatalog=new Set(),selectedJobs=new Map();
  let configRevision='';
  let historyWindow=null,historySummary=null,reportSummary=null,operationsChartRequest=0;
  let summaryOffset=0;
  const reportNameCounts=new Map();
  const create=(tag,attributes={},html='')=>{const el=document.createElement(tag);Object.assign(el,attributes);el.innerHTML=html;return el;};
  function page(id){const el=create('section',{id,className:'view',hidden:true});const footer=$('main footer');if(footer)footer.before(el);else $('main').append(el);return el;}
  function move(first,until,target){while(first && first!==until){const next=first.nextElementSibling;target.append(first);first=next;}}
  function command(label,iconName,callback){const button=create('button',{type:'button'},icon(iconName)+escape(label));button.addEventListener('click',action(callback));return button;}
  function splitPages(){
    const collect=page('collect-page'),datasetPage=page('dataset-page');
    move($('#market').firstElementChild,$('#qmt-live'),collect);
    const quotes=$('#qmt-live');quotes.classList.add('view');$('main').append(quotes);
    $('#tushare-history-link').hidden=true;
    move($('#dataset-form').previousElementSibling,null,datasetPage);
    move($('#jobs').firstElementChild,$('#worker-status'),collect);
    $('#job-filter').previousElementSibling.append($('#refresh-jobs'));
    const headings=[...$('#operations').querySelectorAll(':scope > .section-heading')];
    ['overview','quality','maintenance','events','runtime'].forEach(id=>page(id));
    move(headings[0],headings[1],$('#overview'));
    move(headings[1],headings[3],$('#quality'));
    move(headings[3],headings[4],$('#maintenance'));
    move(headings[4],headings[5],$('#events'));
    move(headings[5],null,$('#runtime'));
    $('#overview').append($('#worker-status'));
    const database=page('database-page'),qmt=page('qmt-page'),access=page('access-page');
    database.append($('#settings-form').previousElementSibling,$('#settings-form'));
    qmt.append($('#qmt-deployment'));$('#qmt-deployment').hidden=false;
    // Separate visible settings fields while retaining native form association.
    const settings=$('#settings-form');
    for(const name of ['qmt_root','login_enabled','password','host','port','ws_port']){
      const field=settings.elements[name];if(!field)continue;
      const parent=field.closest('label');field.setAttribute('form','settings-form');(name==='qmt_root'?qmt:access).prepend(parent);
    }
    for(const target of [qmt,access]){const button=command('保存配置','save',()=>settings.requestSubmit());target.append(button);}
    page('task-detail').innerHTML='<div class="section-heading"><button type="button" id="task-back">'+icon('arrow-left')+'返回列表</button><span id="task-identity"></span></div><div class="result-tabs" role="tablist" id="task-tabs">'+[['summary','概览'],['units','分块质量'],['events','事件'],['links','关联任务']].map(([id,title])=>`<button type="button" data-task-tab="${id}" role="tab">${title}</button>`).join('')+'</div><div id="task-body"></div>';
    $('#task-back').onclick=()=>navigate(taskOrigin);
    const taskRefresh=command('','refresh-cw',()=>showTask(route.slice(5),currentTaskTab));taskRefresh.id='task-refresh';taskRefresh.title=taskRefresh.ariaLabel='刷新任务详情';$('#task-identity').after(taskRefresh);
    setupTaskFilters();
    $('#task-tabs').onclick=event=>{const tab=event.target.closest('[data-task-tab]');if(tab)taskTab(tab.dataset.taskTab).catch(error=>notice(error.message));};
    $('#market .section-heading').append(command('同步目录','refresh-cw',()=>navigate('collect').then(()=>collectMode('catalog'))));
    $('#futures-form [name=resource]').closest('label').hidden=true;
    $('#futures-tabs').hidden=true;
    $('#futures-sync').textContent='采集此范围';
    $('#futures-sync').addEventListener('click',event=>{event.stopImmediatePropagation();action(stageReport)(event);},true);
    const title=create('div',{className:'section-heading'},'<h2>资料采集</h2>');
    const pending=create('div',{id:'collect-report'},'<p class="empty-state">从期货资料页选择范围后进入。</p>');
    collect.append(title,pending);
    const collectHeads=[...collect.querySelectorAll(':scope > .section-heading')],panels=[];
    for(const [index,key] of ['catalog','history','report'].entries()){
      const panel=create('div',{id:'collect-'+key+'-panel'});move(collectHeads[index],collectHeads[index+1] || null,panel);panels.push(panel);
    }
    const modes=create('div',{id:'collect-modes',className:'result-tabs',role:'tablist'});
    for(const [key,label] of [['catalog','目录同步'],['history','历史回补'],['report','资料采集']]){const button=command(label,key==='catalog'?'library':'download',()=>collectMode(key));button.dataset.collectMode=key;button.setAttribute('role','tab');modes.append(button);}
    collect.append(modes,...panels);collectMode('history');$('#collect-history-panel h2').textContent='历史回补';
    const nav=$('.sidebar nav');nav.replaceChildren();
    groups.forEach(([label,items])=>{const group=create('details',{open:true,className:'nav-group'},'<summary>'+label+'</summary>');items.forEach(([id,title,glyph])=>{const legacy={catalog:'market',calendar:'futures',overview:'operations',accounts:'settings'}[id] || id;const button=create('button',{type:'button'},icon(glyph)+escape(title));button.addEventListener('click',()=>navigate(id).catch(error=>notice(error.message)));button.dataset.view=legacy;button.dataset.route=id;group.append(button);});nav.append(group);});
    $('.source-toolbar').after(create('p',{id:'source-unavailable',className:'source-unavailable',hidden:true,role:'status'}),create('p',{id:'view-loading',className:'query-state',hidden:true,role:'status'},'正在加载…'));
    $('#data-source').setAttribute('aria-label','全局数据来源');
    $('#data-source').parentElement.firstChild.textContent='全局数据来源';
    const toggle=command('菜单','menu',()=>{document.body.classList.toggle('nav-open');toggle.setAttribute('aria-expanded',document.body.classList.contains('nav-open'));});toggle.id='nav-toggle';toggle.setAttribute('aria-expanded','false');$('header').prepend(toggle);
    document.addEventListener('keydown',event=>{if(event.key==='Escape')document.body.classList.remove('nav-open');});
    for(const id of ['record-dialog','instrument-dialog','unit-dialog','maintenance-dialog'])$('#'+id).classList.add('side-panel');
    setupDatasetEditor();setupMaintenance();setupAccountEditor();setupAnalysis();setupReportDimensions();setupQueryControls();setupManagementLists();setupSmallLists();
    for(const id of ['job-filter','freshness-filter','event-filter','overview-filter','maintenance-filter']){
      const field=$('#'+id+' [name=sources]');if(field)field.closest('label').hidden=true;for(const event of ['input','change'])$('#'+id).addEventListener(event,()=>markQuery($('#'+id),'筛选尚未应用，点击查询应用'));
    }
  }
  function formState(form,sensitive=false){return [...form.elements].filter(el=>el.name && (sensitive||el.type!=='password')).map(el=>[el.name,el.type==='checkbox'?el.checked:el.multiple?[...el.selectedOptions].map(item=>item.value):el.value]);}
  function remember(){if(!route)return;contexts.set(route,{scroll:scrollY,offset:route==='catalog'?securityOffset:route==='boards'?boardOffset:['jobs','exports'].includes(route)?jobOffset:route==='quality'?freshnessOffset:route==='history'?state.offset:Object.hasOwn(reportNames,route)?futuresOffset:0,before:route==='events'?eventBefore:null,presets:[...document.querySelectorAll('.view:not([hidden]) [data-date-preset]')].map(el=>[el.dataset.datePreset,el.value]),forms:[...document.querySelectorAll('.view:not([hidden]) form')].map(form=>[form.id,formState(form)])});}
  function restore(id,withScroll=true){const context=contexts.get(id);if(!context)return;for(const [formId,entries] of context.forms){const form=$('#'+formId);if(!form)continue;for(const [name,value] of entries){const fields=[...form.elements].filter(el=>el.name===name);if(fields.length===1){const el=fields[0];if(el.multiple){[...el.options].forEach(item=>item.selected=value.includes(item.value));refreshMulti(el);}else if(el.type==='checkbox')el.checked=value;else el.value=value;}}}for(const [id,value] of context.presets||[]){const preset=$('[data-date-preset="'+id+'"]');if(preset)preset.value=value;}if(withScroll)requestAnimationFrame(()=>scrollTo(0,context.scroll));}
  async function navigate(target,fromHistory=false){
    target=aliases[target] || target;const detail=target.startsWith('task/');
    if(!pages[target]&&!detail)target='catalog';
    if(target!==route && dirtyForm && JSON.stringify(formState(dirtyForm,true))!==dirtyBaseline && !await confirmBatch('放弃未保存的修改？',{操作:'离开编辑面板，放弃尚未保存的输入'})){if(fromHistory)history.pushState({route},'','#'+route);return;}
    if(target!==route && dirtyForm){dirtyForm.reset();dirtyForm.closest('dialog')?.close();dirtyForm=null;}
    const requiredSource=Object.hasOwn(reportNames,target)&&!['calendar','mapping'].includes(target)?'tushare':['quotes','indices','boards'].includes(target)?'qmt':null;
    const previousRoute=route;if(detail&&!route.startsWith('task/'))taskOrigin=pages[route]?route:'jobs';remember();const request=++routeRequest;route=target;state.view=target;
    const id=detail?'task-detail':backing[target] || target;
    $$('.view').forEach(el=>el.hidden=el.id!==id);
    $('#view-title').textContent=detail?'任务详情':pages[target].title;if(detail)$('#task-back').innerHTML=icon('arrow-left')+'返回'+pages[taskOrigin].title;
    $('.eyebrow').textContent=detail?'采集管理 / 任务记录':pages[target].group;
    $$('nav [data-route]').forEach(el=>{el.classList.toggle('active',el.dataset.route===target);el.setAttribute('aria-current',el.dataset.route===target?'page':'false');});
    if(location.hash.slice(1)!==target)history[fromHistory?'replaceState':'pushState']({route:target},'', '#'+target);
    document.body.classList.remove('nav-open');$('#nav-toggle').setAttribute('aria-expanded','false');
    $('.source-toolbar').hidden=false;
    $('footer span').textContent='全局来源：'+(provider==='tushare'?'Tushare':'QMT')+' · Asia/Shanghai · 字段单位以记录为准';
    $('#account-choice').hidden=provider!=='tushare' || target!=='collect';$('#job-account-filter').hidden=provider!=='tushare';
    const reports=$('[data-collect-mode=report]');reports.disabled=false;reports.title='';
    $('#futures .source').textContent='PostgreSQL · '+(provider==='qmt'?'QMT':'Tushare');
    $('#qmt-state').hidden=provider!=='qmt';$('#qmt-deployment').hidden=false;$('#tushare-history-link').hidden=true;
    const unsupported=requiredSource&&requiredSource!==provider;
    $('#source-unavailable').hidden=!unsupported;
    if(unsupported){$('#'+id).hidden=true;$('#source-unavailable').textContent=(provider==='qmt'?'QMT':'Tushare')+' 当前不提供'+pages[target].title+'。可在顶部切换至 '+(requiredSource==='qmt'?'QMT':'Tushare')+'，或继续浏览当前来源支持的页面。';}
    $('#view-loading').hidden=true;
    notice('');restore(target);if(Object.hasOwn(reportNames,target))restoreDimensionChoices(contexts.get(target)?.forms.find(([id])=>id==='futures-form')?.[1]||[]);for(const form of $$('.view:not([hidden]) form'))restoreOptions(form.id,target);if(!state.authenticated||unsupported)return;
    $('#view-loading').hidden=false;
    try{
      if(detail){await showTask(target.slice(5));return;}
      if(target==='catalog'){await loadCatalog();if(request===routeRequest)await loadSecurities(contexts.get(target)?.offset || 0);}
      if(target==='quotes'){chart?.resize();}
      if(target==='history'){if(applied.has('history'))await historyLoad(contexts.get(target)?.offset || 0);else markQuery($('#history-form'),'请选择条件并查询');}
      if(target==='indices'){await loadCatalog();await loadIndices();}
      if(target==='boards')await loadBoards(contexts.get(target)?.offset || 0);
      if(target==='datasets'||target==='collect'){await loadDatasets();enhanceDatasets();}
      if(target==='jobs'||target==='exports'){
        $('#job-filter [name=kinds]').closest('label').hidden=target==='exports';
        if(!contexts.has(target)){const filter=$('#job-filter');filter.reset();for(const opt of filter.elements.kinds.options)opt.selected=target==='exports'?opt.value==='export':false;refreshMulti(filter.elements.kinds);}
        await loadJobs(contexts.get(target)?.offset || 0);
      }
      if(target==='overview'||target==='quality')await loadOperations(target);
      if(target==='overview'&&request===routeRequest){renderWorkerStatus(await api('/status'));await operationCharts();}
      if(target==='runtime')await loadRuntime();
      if(target==='maintenance')await renderMaintenance();
      if(target==='events')await loadEvents(contexts.get(target)?.before || null);
      if(['accounts','qmt','database','access'].includes(target)){await status();if(target==='accounts')await loadAccounts();else setDirty($('#settings-form'));}
      if(Object.hasOwn(reportNames,target)){
        if(previousRoute!==target){++futuresRequest;reportSummary=null;charts.get('report')?.clear();$('#report-visual').hidden=true;empty('#futures-rows',1,'正在加载当前资料');$('#futures-head').replaceChildren();$('#futures-page').textContent='';$('#futures-units').textContent='';$('#futures-prev').disabled=$('#futures-next').disabled=true;}
        const resource=$('#futures-form [name=resource]');[...resource.options].forEach(option=>option.selected=option.value===target);futuresActive=target;refreshMulti(resource);updateFuturesForm();
        $('#futures-form [name=resource]').closest('label').hidden=true;$('#futures-tabs').hidden=true;
        const form=$('#futures-form'),exchange=selectedValues(form.elements.exchange).join(','),before=Object.fromEntries(['symbol','code'].map(name=>[name,selectedValues(form.elements[name]).sort().join(',')]));
        $('#futures .section-heading h2').textContent=reportNames[target];await loadFuturesOptions();if(request!==routeRequest)return;
        // Restore dependent choices after loading their options, without replacing edits made meanwhile.
        if(exchange===selectedValues(form.elements.exchange).join(','))for(const [id,entries] of contexts.get(target)?.forms||[]){if(id!=='futures-form')continue;for(const [name,value] of entries){if(!Object.hasOwn(before,name)||!Array.isArray(value))continue;const select=form.elements[name];if(selectedValues(select).sort().join(',')!==before[name])continue;[...select.options].forEach(option=>option.selected=value.includes(option.value));refreshMulti(select);}}
        updateFuturesForm();restoreOptions('futures-form',target);if($('#report-dimensions').open)loadReportDimensions().catch(error=>notice(error.message));
        if(!applied.has(target)){empty('#futures-rows',1,'请选择条件并查询');$('#futures-head').replaceChildren();$('#report-visual').hidden=true;$('#futures-page').textContent='';$('#futures-units').textContent='';}
        else await futuresLoad(contexts.get(target)?.offset || 0);
      }
      if(request!==routeRequest)return;
      await resolveSelections();chart?.resize();charts.forEach(item=>item.resize());icons();
    }catch(error){if(request===routeRequest)notice(error.message);}
    finally{if(request===routeRequest)$('#view-loading').hidden=true;}
  }
  function optionKey(id,page=route){return ['futures-form','job-filter'].includes(id)?id+':'+page:id;}
  function pageSize(id){return Number(pageOptions(id).limit || 50);}
  function pageOptions(id,page=route){return options.get(optionKey(id,page)) || options.get(id) || {limit:50};}
  function restoreOptions(id,page=route){const tools=$('#'+id+' + .query-options');if(!tools)return;const p=pageOptions(id,page);if(id==='futures-form'){const fields=reportColumns.get(page)||[];tools.querySelector('[data-sort]').innerHTML='<option value="">默认排序</option>'+fields.map(field=>`<option value="${field}">${escape(futuresFields[field]||field)}</option>`).join('');}tools.querySelector('[data-page-size]').value=String(p.limit||50);tools.querySelector('[data-direction]').value=p.direction||'asc';if(p.sort!==undefined)tools.querySelector('[data-sort]').value=p.sort;}
  function listKey(id){return 'list:'+(id==='job-filter'?state.view:id);}
  function listQuery(id,apply=false){return !apply&&applied.has(listKey(id))?{...applied.get(listKey(id))}:listDraft(id);}
  function listDraft(id){return {...values($('#'+id)),...(id==='security-form'?{active:$('#catalog-active').checked}:id==='job-filter'&&state.view==='exports'?{kinds:'export'}:{})};}
  function listLoaded(id,query,total){applied.set(listKey(id),{...query});markQuery($('#'+id),JSON.stringify(query)===JSON.stringify(listDraft(id))?`已应用筛选${total===undefined?'':' · 共 '+total+' 项'}`:'筛选尚未应用，当前结果保持上次查询');}
  const pendingLists=new Map();
  function discardPendingList(id){pendingLists.delete(listKey(id));}
  async function readList(id,apply,read){
    const key=listKey(id),source=provider,view=state.view;
    if(!apply&&pendingLists.has(key))return null;
    const token=Symbol(),query=listQuery(id,apply);pendingLists.set(key,token);
    markQuery($('#'+id),'正在查询');
    try{const result=await read(query);if(pendingLists.get(key)!==token||source!==provider||view!==state.view)return null;listLoaded(id,query,result.total);return result;}
    catch(error){if(pendingLists.get(key)===token&&source===provider&&view===state.view){queryFailed($('#'+id),error);throw error;}return null;}
    finally{if(pendingLists.get(key)===token)pendingLists.delete(key);}
  }
  function queryFailed(form,error){markQuery(form,'查询失败，条件未应用：'+error.message);}
  function dimensionValue(value){return encodeURIComponent(JSON.stringify(value));}
  function dimensionLabel(value){return value===null?'未提供（NULL）':value===''?'空字符串':value;}
  function setupReportDimensions(){
    const panel=create('details',{id:'report-dimensions',className:'report-dimensions',hidden:true},'<summary>明细筛选<span id="report-dimension-count"></span></summary><div class="result-tools"></div><p id="report-dimension-note" role="status"></p>');$('#futures-form').append(panel);
    for(const [field,label] of Object.entries({warehouse:'仓库',unit:'单位',broker:'会员'})){
      const holder=create('label',{},label+`<select id="report-dimension-${field}" name="dimension_${field}" disabled></select>`);holder.dataset.dimensionField=field;panel.querySelector('.result-tools').append(holder);enableMulti(holder.querySelector('select'),'全部');
    }
    const load=command('刷新选项','refresh-cw',()=>loadReportDimensions({force:true}));load.id='report-dimensions-load';panel.querySelector('.result-tools').append(load,command('清空明细筛选','filter-x',()=>{for(const field of Object.keys(reportDimensionFields[futuresActive]||{})){const select=$('#report-dimension-'+field);[...select.options].forEach(option=>option.selected=false);refreshMulti(select);}updateReportDimensions();invalidate('futures');}));
    panel.addEventListener('toggle',()=>{if(panel.open&&!panel.hidden)loadReportDimensions().catch(error=>notice(error.message));});
    panel.addEventListener('change',updateReportDimensions);
    $('#futures-form').addEventListener('change',event=>{if(event.target.closest('#report-dimensions'))return;try{if(dimensionScopes.get(futuresActive)!==JSON.stringify(futuresQuery(futuresActive,false)))$('#report-dimension-note').textContent='范围已修改，请刷新选项；已选明细条件保留';}catch{$('#report-dimension-note').textContent='先选择资料对象和日期，再读取明细选项';}});
  }
  function updateReportDimensions(){
    const panel=$('#report-dimensions');if(!panel)return;
    const fields=reportDimensionFields[futuresActive]||{};panel.hidden=!Object.keys(fields).length;
    panel.querySelectorAll('[data-dimension-field]').forEach(label=>label.hidden=!Object.hasOwn(fields,label.dataset.dimensionField));
    const count=Object.keys(fields).reduce((sum,field)=>sum+selectedValues($('#report-dimension-'+field)).length,0);$('#report-dimension-count').textContent=count?' · 已选 '+count+' 项':'';
  }
  function reportDimensions(){
    const result={};for(const field of Object.keys(reportDimensionFields[futuresActive]||{})){const values=selectedValues($('#report-dimension-'+field)).map(value=>JSON.parse(decodeURIComponent(value)));if(values.length)result[field]=values;}return result;
  }
  function restoreDimensionChoices(entries=[]){
    for(const field of Object.keys({warehouse:1,unit:1,broker:1})){
      const select=$('#report-dimension-'+field),selected=entries.find(([name])=>name==='dimension_'+field)?.[1]||[];
      if(!Array.isArray(selected))continue;
      for(const value of selected)if(![...select.options].some(option=>option.value===value)){const option=new Option(dimensionLabel(JSON.parse(decodeURIComponent(value))),value);select.add(option);}
      [...select.options].forEach(option=>option.selected=selected.includes(option.value));select.disabled=!select.options.length;refreshMulti(select);
    }
    updateReportDimensions();
  }
  async function loadReportDimensions({force=false,selected=null}={}){
    const resource=futuresActive,fields=reportDimensionFields[resource];if(!fields||provider!=='tushare')return;
    let scope;try{scope=futuresQuery(resource,false);}catch(error){$('#report-dimension-note').textContent=error.message;return;}
    const key=JSON.stringify(scope);if(!force&&!selected&&dimensionScopes.get(resource)===key)return;
    const request=++dimensionRequest;$('#report-dimension-note').textContent='正在读取可选项';
    try{
      const result=await api('/futures/filter-options',scope);
      if(request!==dimensionRequest||resource!==futuresActive||provider!=='tushare'||JSON.stringify(futuresQuery(resource,false))!==key)return;
      for(const field of Object.keys(fields)){
        const select=$('#report-dimension-'+field),chosen=new Set(selected?.[field]||selectedValues(select)),items=new Map((result.options[field]||[]).map(value=>[dimensionValue(value),dimensionLabel(value)]));
        for(const value of chosen)if(!items.has(value))items.set(value,dimensionLabel(JSON.parse(decodeURIComponent(value)))+'（当前范围无记录）');
        select.innerHTML=[...items].map(([value,label])=>`<option value="${escape(value)}" ${chosen.has(value)?'selected':''}>${escape(label)}</option>`).join('');select.disabled=!items.size;refreshMulti(select);
      }
      dimensionScopes.set(resource,key);$('#report-dimension-note').textContent=`可选项来自所选对象与日期的 ${result.total} 条入库记录`;updateReportDimensions();
    }catch(error){if(request===dimensionRequest)$('#report-dimension-note').textContent='选项读取失败：'+error.message;}
  }
  async function resetQueryForm(id){
    const form=$('#'+id);form.reset();
    if(id==='futures-form'){
      [...form.elements.resource.options].forEach(option=>option.selected=option.value===route);futuresActive=route;
      form.elements.start.value=today(-7);form.elements.end.value=today(-1);restoreDimensionChoices();updateFuturesForm();await loadFuturesOptions();if($('#report-dimensions').open)await loadReportDimensions({force:true});
    }
    if(id==='history-form'){form.elements.code.value='';form.elements.start.value=today(-365);form.elements.end.value=today();form.elements.period.value='1d';rememberSecurities([]);}
    form.querySelectorAll('select[multiple]').forEach(refreshMulti);markQuery(form,'条件已重置，尚未应用');
  }
  async function reloadDisplay(id){
    if(id==='history-form'){if(applied.has('history'))await historyLoad(0);return;}
    if(id==='futures-form'){if(applied.has(route))await futuresLoad(0);return;}
    discardPendingList(id);
    if(id==='security-form')await loadSecurities(0);
    if(id==='board-form')await loadBoards(0);
    if(id==='job-filter')await loadJobs(0);
  }
  function errorIn(form,error){let note=form.querySelector('.form-error');if(!note){note=create('p',{className:'form-error',role:'alert'});form.append(note);}note.textContent=error.message;}
  function setDirty(form){dirtyForm=form;dirtyBaseline=JSON.stringify(formState(form,true));}
  function clearDirty(){dirtyForm=null;dirtyBaseline='';}
  async function allowSourceChange(){
    if(dirtyForm&&JSON.stringify(formState(dirtyForm,true))!==dirtyBaseline&&!await confirmBatch('放弃未保存的修改？',{操作:'切换全局数据来源'}))return false;
    if(dirtyForm){dirtyForm.reset();dirtyForm.closest('dialog')?.close();clearDirty();}return true;
  }
  function rememberSource(){try{localStorage.setItem('pfor:source',provider);}catch{}}
  function cancelButton(){const button=create('button',{type:'button'},icon('x')+'取消');button.dataset.closeDialog='true';return button;}
  function openPanel(id,form){$('#'+id).showModal();setDirty(form);form.querySelector('input:not([type=hidden]):not([type=password]),button')?.focus();}
  function addPanel(id,title){const dialog=create('dialog',{id,className:'side-panel'},`<div class="picker-heading"><h2>${title}</h2><button type="button" class="icon" data-close-dialog title="关闭" aria-label="关闭">${icon('x')}</button></div>`);document.body.append(dialog);dialog.addEventListener('close',()=>{if(dirtyForm?.closest('dialog')===dialog)clearDirty();});return dialog;}
  function intercept(form,callback){form.addEventListener('submit',event=>{event.preventDefault();event.stopImmediatePropagation();const submit=event.submitter; if(submit?.disabled)return;if(!form.reportValidity())return;if(submit)submit.disabled=true;Promise.resolve().then(()=>callback(event)).catch(error=>errorIn(form,error)).finally(()=>{if(submit)submit.disabled=false;});},true);}
  function compact(value){if(value==null)return '—';const raw=String(value);if(!/^-?\d+(\.\d+)?$/.test(raw))return raw;const [a,b]=raw.split('.');return a.replace(/\B(?=(\d{3})+(?!\d))/g,',')+(b?'.'+b.slice(0,6)+(b.length>6?'…':''):'');}
  function numeric(value){return value==null?null:Number(value);}
  const reportNumeric=new Set(['pre_vol','vol','vol_chg','pd','long_hld','long_chg','short_hld','short_chg','settle','trading_fee_rate','trading_fee','delivery_fee','b_hedging_margin_rate','s_hedging_margin_rate','long_margin_rate','short_margin_rate','offset_today_fee','amount','open_interest','vol_yoy','amout_yoy','cumvol','cumvol_yoy','cumamt','cumamt_yoy','interest_wow','mc_close','close_wow']);

  function setupDatasetEditor(){
    const form=$('#dataset-form'),dialog=addPanel('dataset-editor','数据集配置');
    dialog.append(form);
    const clock=$('#dataset-schedule-time').closest('label');clock.className='';clock.firstChild.textContent='自动更新时间';form.append(clock);
    form.className='settings-form';$('#dataset-schedule-time').setAttribute('form','dataset-form');
    const account=create('label',{},'采集账号<select name="account_id" id="dataset-editor-account"></select>');form.append(account);
    const warning=create('div',{id:'dataset-members-warning',className:'member-warning',hidden:true,role:'status'},'<p></p>');
    const remove=command('移除不兼容成员','list-filter',()=>{const excluded=new Set(incompatibleDatasetMembers());form.elements.members.value=splitCodes(form.elements.members.value).filter(code=>!excluded.has(code)).join(',');rememberSecurities([]);updateDatasetMembers();});remove.id='dataset-remove-incompatible';warning.append(remove);form.querySelector('fieldset').after(warning);
    form.addEventListener('change',updateDatasetMembers);
    form.append(create('label',{id:'dataset-rebind-label',className:'inline-label'},'<input type="checkbox" name="rebind_account">重新绑定所选账号的当前端点'));
    const actions=create('div',{className:'form-actions'});actions.append(form.querySelector('[type=submit]'),cancelButton());form.append(actions);
    $('#dataset-page .section-heading').append(command('新建数据集','plus',()=>editDataset()));
    intercept(form,async()=>{
      const p=values(form),periods=[...form.querySelectorAll('input[name=period]:checked')].map(input=>input.value);
      if(!p.members || !periods.length)throw new Error('请选择成员和至少一个周期');
      await resolveSelections();updateDatasetMembers();
      if(incompatibleDatasetMembers().length)throw new Error('所选分钟周期包含非月份合约，请按成员提示移除，或改用日/周/月周期');
      const payload={name:p.name,members:splitCodes(p.members),periods,schedule_time:$('#dataset-schedule-time').value,account_id:p.account_id || null};
      const id=form.dataset.editId;
      if(id)await api('/console/datasets/'+id,{...payload,rebind_account:form.elements.rebind_account.checked,revision:Number(form.dataset.revision)});
      else{const extra=state.snapshot?state.snapshot.board?{board_name:state.snapshot.name,board_snapshot_id:state.snapshot.id}:{index_code:state.snapshot.code,snapshot_id:state.snapshot.snapshot_id}:{};await api('/datasets',{...payload,...extra,source:provider});}
      discardPendingList('datasets-filter');clearDirty();dialog.close();state.snapshot=null;await navigate('datasets');notice('数据集已保存，尚未执行采集');
    });
  }
  function updateDatasetMembers(){const invalid=incompatibleDatasetMembers(),warning=$('#dataset-members-warning');if(!warning)return;warning.hidden=!invalid.length;warning.querySelector('p').textContent=invalid.length?`分钟周期不支持 ${invalid.length} 个成员：${invalid.slice(0,8).map(securityLabel).join('、')}${invalid.length>8?'…':''}。成员尚未移除。`:'';}
  async function editDataset(id,copy=false){
    const source=provider;
    if(id){const rows=await api('/datasets');if(source!==provider)return;state.datasets=rows;if(!rows.some(row=>row.id===id))throw new Error('当前来源的数据集已变化，请重新选择');}
    const form=$('#dataset-form');form.reset();form.querySelector('.form-error')?.remove();
    form.elements.name.placeholder=provider==='tushare'?'例如：沪铜月份合约分钟线':'例如：沪深300日线';
    const row=state.datasets.find(item=>item.id===id);
    delete form.dataset.editId;delete form.dataset.revision;state.snapshot=null;
    if(row){
      if(!copy){form.dataset.editId=row.id;form.dataset.revision=row.revision;}
      form.elements.name.value=row.name+(copy?' 副本':'');form.elements.members.value=row.members.join(',');
      form.querySelectorAll('[name=period]').forEach(el=>el.checked=row.periods.includes(el.value));
      $('#dataset-schedule-time').value=row.schedule_time.slice(0,5);
    }else{form.elements.members.value='';$('#dataset-schedule-time').value=provider==='tushare'?'19:00':'17:00';}
    const account=$('#dataset-editor-account');account.innerHTML=accounts.filter(item=>item.enabled || item.id===row?.account_id).map(item=>`<option value="${escape(item.id)}">${escape(item.name)}${item.enabled?'':'（已停用）'}</option>`).join('');account.value=row?.account_id || $('#data-account').value;account.closest('label').hidden=provider!=='tushare';$('#dataset-rebind-label').hidden=provider!=='tushare'||!id;
    const capabilities=()=>{const cap=sourceCapabilities[account.value]?.capabilities || {};form.querySelectorAll('input[name=period]').forEach(input=>{const allowed=provider==='tushare'||['1d','1m','5m'].includes(input.value),permission=cap[minutePeriods.includes(input.value)?'minutes':input.value==='1w'?'weekly':input.value==='1mo'?'monthly':'daily'];input.disabled=!allowed || !input.checked&&['permission','authentication','unsupported'].includes(permission?.state);input.closest('label').hidden=!allowed;});};account.onchange=capabilities;capabilities();
    const picker=form.querySelector('[data-picker=dataset]');picker.disabled=Boolean(row&&!copy&&(row.index_code||row.board_name));
    await resolveSelections();updateDatasetMembers();refreshCheckAll();form.querySelector('[type=submit]').innerHTML=icon('save')+'保存数据集';openPanel('dataset-editor',form);
  }
  function enhanceDatasets(){
    $$('#datasets tr').forEach((tr)=>{const row=(state.datasetRows||state.datasets).find(item=>item.id===tr.dataset.datasetId);if(!row)return;const actions=tr.querySelector('.actions');
      if(row.deleted_at){actions.replaceChildren(recycleButton('datasets',row));tr.children[3].textContent='已停用';return;}
      if(!actions.querySelector('[data-edit-dataset]')){const edit=command('','pencil',()=>editDataset(row.id)),copy=command('','copy',()=>editDataset(row.id,true));edit.title='编辑数据集';edit.setAttribute('aria-label','编辑数据集');copy.title='复制数据集';copy.setAttribute('aria-label','复制数据集');edit.dataset.editDataset=row.id;actions.prepend(edit,copy);}
      if(!actions.querySelector('[data-recycle]'))actions.append(recycleButton('datasets',row));
      const account=tr.querySelector('[data-dataset-account]');if(account)account.replaceWith(create('span',{className:'muted'},escape(accounts.find(a=>a.id===row.account_id)?.name || row.account_id)));
    });icons();
  }
  function trashChoice(){return create('label',{},'记录范围<select name="trash"><option value="active">当前记录</option><option value="deleted">回收站</option><option value="all">全部（含回收站）</option></select>');}
  function recycleButton(resource,row){const restore=Boolean(row.deleted_at),button=command('',restore?'undo-2':'trash-2',()=>recycleRecord(resource,row));button.title=button.ariaLabel=restore?'恢复记录':'移入回收站';button.dataset.recycle=resource;return button;}
  async function recycleRecord(resource,row){
    const restore=Boolean(row.deleted_at),label=restore?'恢复记录':'移入回收站';
    const effect=resource==='jobs'?'保留任务详情、质量证据、行情及导出文件；所属自动维护不受影响':restore?'恢复后保持自动更新停用，按需手动启用':'停用此配置的自动更新，保留行情、原任务及导出文件';
    if(!await confirmBatch(label,{对象:row.name||row.id,操作:effect}))return;
    await api((resource==='jobs'?'/jobs/':'/console/'+resource+'/')+row.id+(restore?'/restore':'/delete'),resource==='jobs'?{}:{revision:row.revision});
    discardPendingList(resource==='jobs'?'job-filter':resource+'-filter');
    selectedJobs.delete(row.id);
    if(resource==='datasets'){await loadDatasets();enhanceDatasets();}
    else if(resource==='maintenance')await renderMaintenance();
    else if(route.startsWith('task/'))await navigate(route);
    else await loadJobs();
    notice(restore?'记录已恢复'+(resource==='jobs'?'':'，自动更新保持停用'):'已移入回收站，可在“记录范围”中切换查看和恢复');
  }
  function setupMaintenance(){
    const form=$('#maintenance-form');$('#maintenance-dialog').classList.add('side-panel');
    form.insertAdjacentHTML('beforeend','<label>范围类型<select name="scope_type"><option value="dataset">数据集</option><option value="catalog">目录</option><option value="report">资料范围</option></select></label><label id="maintenance-dataset-label">数据集<select name="dataset_id"></select></label><div id="maintenance-catalog-fields"><label>目录来源<select name="scope_source"><option value="qmt">QMT</option><option value="tushare">Tushare</option></select></label><fieldset><legend>类别</legend>'+Object.entries(kindNames).filter(([key])=>key!=='etf').map(([key,label])=>`<label><input type="checkbox" name="catalog_kind" value="${key}" ${key==='future'?'checked':''}>${escape(label)}</label>`).join('')+'</fieldset></div><label id="maintenance-account-label">采集账号<select name="account_id"></select></label><p id="maintenance-scope-note" class="catalog-meta"></p><div id="maintenance-edit-actions"></div>');
    $('#maintenance-edit-actions').append(form.querySelector('[type=submit]'),cancelButton());
    $('#maintenance-account-label').after(create('label',{id:'maintenance-rebind-label',className:'inline-label'},'<input type="checkbox" name="rebind_account">重新绑定所选账号的当前端点'));
    form.elements.scope_type.insertAdjacentHTML('beforeend','<option value="fixed">原任务固定范围</option>');
    $('#maintenance-catalog-fields').append(create('label',{id:'maintenance-exchanges-label'},'交易所<select name="exchanges" id="maintenance-exchanges" multiple>'+Object.entries(exchangeMarkets).map(([exchange,market])=>`<option value="${exchange}" selected>${escape(marketNames[market])}</option>`).join('')+'</select>'));
    enableMulti(form.elements.exchanges,'选择交易所',true);form.elements.scope_source.onchange=maintenanceFields;
    form.elements.scope_type.onchange=maintenanceFields;
    $('#maintenance .section-heading').append(command('新建维护计划','plus',()=>editMaintenance()));
    intercept(form,async()=>{
      const p=values(form),id=form.dataset.editId,old=plans.find(row=>row.id===id);
      const payload={name:p.name,schedule_time:p.schedule_time,lookback_days:Number(p.lookback_days)};
      if(id)payload.revision=Number(form.dataset.revision);
      if(!old || !old.enabled && maintenanceInputs()!==maintenanceBaseline){const scope=maintenanceScope();Object.assign(payload,scope);payload.enabled=false;}
      if(old&&!old.enabled&&payload.payload){const preview=await api('/console/maintenance/'+id+'/scope-preview',payload);if(preview.changed&&!await confirmBatch('确认修改维护范围',{名称:p.name,影响:'仅后续启用时生效；原任务保持不变'},'<h3>原范围</h3><pre>'+escape(JSON.stringify(preview.before,null,2))+'</pre><h3>新范围</h3><pre>'+escape(JSON.stringify(preview.after,null,2))+'</pre>'))return;}
      await api('/console/maintenance'+(id?'/'+id:''),payload);discardPendingList('maintenance-filter');clearDirty();$('#maintenance-dialog').close();await navigate('maintenance');notice('维护配置已保存'+(old?.enabled?'':'，尚未启用'));
    });
  }
  let maintenanceReport=null,pendingReport=null,maintenanceOrigin=null,maintenanceBaseline='',maintenanceOriginal=null;
  function maintenanceInputs(){return JSON.stringify(formState($('#maintenance-form')).filter(([name])=>!['name','schedule_time','lookback_days','job_id'].includes(name)));}
  function maintenanceFields(){
    const form=$('#maintenance-form'),type=form.elements.scope_type.value,origin=maintenanceOrigin!==null;
    $('#maintenance-dataset-label').hidden=type!=='dataset'||origin;$('#maintenance-catalog-fields').hidden=type!=='catalog'||origin;
    const source=type==='catalog'?form.elements.scope_source.value:maintenanceReport?.source;
    $('#maintenance-exchanges-label').hidden=source!=='tushare';$('#maintenance-account-label').hidden=type==='dataset'||source!=='tushare';
    $('#maintenance-rebind-label').hidden=origin||$('#maintenance-account-label').hidden;
    $('#maintenance-scope-note').textContent=origin?'原任务 '+maintenanceOrigin.id.slice(0,8)+' · '+(maintenanceReport.members?.length || maintenanceReport.selections?.length || maintenanceReport.exchanges?.length || 1)+' 个对象 · '+(maintenanceReport.account_id || 'QMT')+' · 保留原任务成员、周期与端点':type==='report'?(maintenanceReport?'已选择 '+reportNames[maintenanceReport.resource || maintenanceReport.selections?.[0]?.resource]+'；成员范围保持原选择':'请先到期货资料页选择范围，再使用“保存为维护计划”'):type==='fixed'?'保存的固定范围 · '+(maintenanceReport?.members?.length || 0)+' 个成员；选择数据集可显式改用其当前范围':'保存与执行分开；修改范围后需重新启用';
    form.querySelectorAll('[name=catalog_kind]').forEach(input=>{input.closest('label').hidden=source==='tushare'&&input.value!=='future';});
  }
  function maintenanceScope(){
    const form=$('#maintenance-form'),p=values(form);
    if(maintenanceOrigin)return {job_id:maintenanceOrigin.id};
    if(p.scope_type==='dataset')return {kind:'download',payload:{dataset_id:p.dataset_id}};
    let payload;
    if(p.scope_type==='report'||p.scope_type==='fixed'){
      if(!maintenanceReport)throw new Error('请先选择资料或原任务范围');payload={...maintenanceReport};
    }else payload={source:p.scope_source,kinds:p.scope_source==='tushare'?['future']:new FormData(form).getAll('catalog_kind'),...(p.scope_source==='tushare'?{exchanges:selectedValues(form.elements.exchanges)}:{})};
    if(payload.source==='tushare'){
      const old=maintenanceReport || maintenanceOriginal;
      payload.account_id=p.account_id || old?.account_id;
      if(old?.account_id===payload.account_id && !form.elements.rebind_account.checked)payload.endpoint=old.endpoint;
      else delete payload.endpoint;
    }
    return {kind:p.scope_type==='catalog'?'catalog':'download',payload};
  }
  async function editMaintenance(id,copy=false,report=null,origin=null){
    await loadDatasets();plans=await api('/maintenance');const old=plans.find(row=>row.id===id),form=$('#maintenance-form');form.reset();form.querySelector('.form-error')?.remove();delete form.dataset.editId;delete form.dataset.revision;
    if(old&&!copy){form.dataset.editId=old.id;form.dataset.revision=old.revision;}
    maintenanceOrigin=origin;maintenanceOriginal=old?.payload || null;
    form.elements.name.value=old?old.name+(copy?' 副本':''):'';form.elements.schedule_time.value=old?old.schedule_time.slice(0,5):provider==='tushare'?'19:00':'17:00';form.elements.lookback_days.value=old?.lookback_days || 5;
    form.elements.dataset_id.innerHTML=state.datasets.filter(row=>row.source===provider||row.id===old?.payload.dataset_id).map(row=>`<option value="${row.id}">${escape(row.name)} · ${escape(row.source)}</option>`).join('');form.elements.account_id.innerHTML=accounts.filter(a=>a.enabled).map(a=>`<option value="${escape(a.id)}">${escape(a.name)}</option>`).join('');
    maintenanceReport=origin?{source:'qmt',...origin.payload}:report || (old?.payload.resource || old?.payload.members?old.payload:null);
    form.elements.scope_type.value=report || old?.payload.resource?'report':old?.kind==='catalog'?'catalog':'dataset';
    if(origin || maintenanceReport?.members&&!maintenanceReport.resource)form.elements.scope_type.value='fixed';
    if(old?.payload.dataset_id)form.elements.dataset_id.value=old.payload.dataset_id;
    const bound=maintenanceReport || old?.payload,source=bound?.source || old?.source || provider;
    const account=form.elements.account_id;account.innerHTML=accounts.filter(a=>a.enabled||a.id===bound?.account_id).map(a=>`<option value="${escape(a.id)}">${escape(a.name)}${a.enabled?'':'（已停用）'}</option>`).join('');
    form.elements.scope_source.value=source;account.value=bound?.account_id || $('#data-account').value;
    if(!old)form.elements.schedule_time.value=source==='tushare'?'19:00':'17:00';
    for(const option of form.elements.exchanges.options)option.selected=!old?.payload.exchanges || old.payload.exchanges.includes(option.value);refreshMulti(form.elements.exchanges);
    if(old?.kind==='catalog')form.querySelectorAll('[name=catalog_kind]').forEach(el=>el.checked=old.payload.kinds.includes(el.value));
    for(const name of ['scope_type','dataset_id','scope_source','account_id','exchanges','rebind_account'])form.elements[name].disabled=Boolean(origin||old?.enabled&&!copy);
    form.querySelectorAll('[name=catalog_kind]').forEach(el=>el.disabled=Boolean(old?.enabled&&!copy));
    $('#maintenance-title').textContent=old&&!copy?'编辑维护计划':'新建维护计划';form.querySelector('[type=submit]').innerHTML=icon('save')+'保存';maintenanceFields();maintenanceBaseline=maintenanceInputs();openPanel('maintenance-dialog',form);
  }
  async function renderMaintenance(apply=false){const source=provider;const visible=await configurationRows('maintenance',apply);if(!visible||source!==provider)return;plans=visible;$('#maintenance-rows').innerHTML=visible.map(row=>`<tr><td>${escape(row.name)}<span class="muted">${escape(row.source)} · ${escape(reportNames[row.payload.resource] || (row.kind==='catalog'?'目录':'历史行情'))}</span></td><td>${escape(row.schedule_time)}</td><td>${row.lookback_days}</td><td>${escape(row.last_date || '尚未排队')}</td><td><span class="badge ${row.enabled?'succeeded':''}">${row.deleted_at?'回收站':row.enabled?'已启用':'已停用'}</span><span class="muted">${escape(row.last_error || '')}</span></td><td><div class="actions"><button ${row.deleted_at?'hidden':''} data-plan="edit" data-id="${row.id}" title="编辑">${icon('pencil')}</button><button ${row.deleted_at?'hidden':''} data-plan="copy" data-id="${row.id}" title="复制">${icon('copy')}</button><button ${row.deleted_at?'hidden':''} data-plan="toggle" data-id="${row.id}" title="${row.enabled?'停用':'启用'}">${icon(row.enabled?'pause':'play')}</button><button ${row.deleted_at?'hidden':''} data-plan="run" data-id="${row.id}" title="执行一次">${icon('circle-play')}</button></div></td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无匹配维护计划</td></tr>';$$('#maintenance-rows .actions').forEach((actions,index)=>actions.append(recycleButton('maintenance',visible[index])));icons();}
  async function runPlan(id){
    const plan=plans.find(row=>row.id===id);if(!plan)throw new Error('维护计划已变化，请刷新列表');
    let panel=$('#maintenance-run');
    if(!panel){
      panel=addPanel('maintenance-run','执行一次维护');
      panel.insertAdjacentHTML('beforeend','<dl id="maintenance-run-scope" class="detail-grid"></dl><form id="maintenance-run-form" class="settings-form"><label id="run-mode-label">日期方式<select name="mode"><option value="calendar">按已保存交易日历回读</option><option value="custom">自定义日期</option></select></label><div id="run-dates" class="run-dates"><label>快捷日期<select name="preset"><option value="">自定义</option><option value="today">今天</option><option value="3d">近三天</option><option value="1w">近一周</option><option value="1m">近一个月</option><option value="3m">近三个月</option><option value="6m">近六个月</option><option value="1y">近一年</option><option value="2y">近两年</option><option value="3y">近三年</option></select></label><label>开始日期<input name="start" type="date" required></label><label>结束日期<input name="end" type="date" required></label></div><div class="actions"><button type="submit" class="primary">预览执行范围</button></div></form>');
      const form=$('#maintenance-run-form');form.lastElementChild.className='form-actions';form.lastElementChild.append(cancelButton());
      form.elements.mode.onchange=()=>{$('#run-dates').hidden=form.elements.mode.value!=='custom'||form.dataset.kind==='catalog';form.elements.start.disabled=form.elements.end.disabled=$('#run-dates').hidden;};
      form.elements.preset.onchange=()=>{if(form.elements.preset.value){const dates=dateRangeFor(form.elements.preset.value);form.elements.start.value=dates.start;form.elements.end.value=dates.end;}};
      for(const name of ['start','end'])form.elements[name].oninput=()=>{form.elements.preset.value='';};
      intercept(form,async()=>{
        const id=form.dataset.id,p=values(form),params=p.mode==='custom'&&form.dataset.kind!=='catalog'?{start:p.start,end:p.end}:{};
        const preview=await api('/console/maintenance/'+id+'/preview',params);
        if(!await confirmBatch('确认执行维护',{来源:preview.source,固定账号:preview.account_id || 'QMT',固定端点:preview.endpoint || '本地行情桥',分块:preview.chunks,范围:preview.ranges.map(row=>(row.start?row.start+' 至 '+row.end:'目录同步')+' · '+(row.members?.length || row.selections?.length || row.exchanges?.length || 1)+' 个对象').join('；')}))return;
        const result=await api('/console/maintenance/'+id+'/run',{preview_key:preview.preview_key});panel.close();await navigate('jobs');notice('已创建 '+result.jobs.length+' 个维护任务');
      });
    }
    const form=$('#maintenance-run-form');form.reset();form.querySelector('.form-error')?.remove();form.dataset.id=id;form.dataset.kind=plan.kind;
    $('#maintenance-run-scope').innerHTML=detailFields({名称:plan.name,来源:plan.source,采集账号:plan.payload.account_id || 'QMT',回读交易日:plan.lookback_days});
    form.elements.mode.value=plan.payload.resource==='calendar'?'custom':'calendar';form.elements.start.value=today(-30);form.elements.end.value=today();
    $('#run-mode-label').hidden=plan.kind==='catalog';form.elements.mode.onchange();panel.showModal();
  }
  function setupAccountEditor(){
    const form=$('#account-form'),panel=addPanel('account-editor','Tushare 账号配置');panel.append(form);form.append(cancelButton());
    intercept(form,async()=>{const p=values(form);for(const key of ['enabled','default','clear_token'])p[key]=form.elements[key].checked;p.timeout=Number(p.timeout);p.requests_per_minute=Number(p.requests_per_minute);p.config_revision=form.dataset.configRevision;await api('/sources/tushare/accounts',p);form.elements.token.value='';clearDirty();panel.close();await loadAccounts();await status();notice('Tushare账号已保存');});
    const settings=$('#settings-form');
    for(const [name,label,type] of [['host','监听地址','text'],['port','HTTP端口','number'],['ws_port','WebSocket端口','number']]){
      if(settings.elements[name])continue;
      $('#access-page').prepend(create('label',{},label+`<input name="${name}" type="${type}" form="settings-form" ${type==='number'?'min="1" max="65535"':''} required>`));
    }
    intercept(settings,async()=>{const p=values(settings);p.login_enabled=settings.elements.login_enabled.checked;p.port=Number(p.port);p.ws_port=Number(p.ws_port);p.config_revision=settings.dataset.configRevision;const result=await api('/settings',p);settings.elements.dsn.value='';settings.elements.password.value='';clearDirty();await status();notice(result.restart_required?'配置已保存；监听地址或端口将在服务重启后生效':'配置已保存');});
  }
  function configLoaded(settings){configRevision=settings.config_revision;const form=$('#settings-form');form.dataset.configRevision=configRevision;for(const key of ['host','port','ws_port'])if(form.elements[key])form.elements[key].value=settings[key];}
  function sourceChanged(){++routeRequest;++dimensionRequest;dimensionScopes.clear();restoreDimensionChoices();++operationsRequest;++freshnessRequest;++eventRequest;++runtimeRequest;++jobsRequest;++datasetsRequest;++securitiesRequest;++historyRequest;++chartRequest;++futuresRequest;++boardsRequest;++operationsChartRequest;pendingLists.clear();applied.clear();contexts.clear();selectedCatalog.clear();selectedJobs.clear();state.datasets=[];state.datasetRows=[];runtimePage={rows:[],truncated:false};pendingReport=null;historyWindow=null;historySummary=null;reportSummary=null;managementOffsets.datasets=managementOffsets.maintenance=0;freshnessOffset=0;$('#history-summary').hidden=true;$('#history-summary-rows').replaceChildren();delete recordSets.historySummary;summaryOffset=0;chart?.clear();charts.forEach(chart=>chart.clear());$('#chart-empty').hidden=false;$('#chart-empty').textContent='暂无历史行情';$('#chart-selection').hidden=true;$('#chart-window-note').textContent='';$('#history-units').textContent='';state.offset=0;state.next=null;for(const id of ['page-status','futures-page','job-page','security-page-status','freshness-page'])$('#'+id).textContent='';for(const id of ['prev-page','next-page','futures-prev','futures-next','job-prev','job-next','security-prev','security-next','freshness-prev','freshness-next'])$('#'+id).disabled=true;empty('#bars',14,'来源已切换，请查询');empty('#futures-rows',1,'来源已切换，请查询');for(const id of ['job-rows','datasets','securities','quotes','runtime-rows','freshness-rows','attention-rows','event-rows','maintenance-rows'])empty('#'+id,16,'正在切换来源');for(const id of ['operations-metrics','operations-summary','freshness-metrics'])$('#'+id).replaceChildren();$('#report-visual').hidden=true;$('#collect-report').innerHTML='<p class="empty-state">从当前来源的期货资料页选择采集范围。</p>';}
  async function stageReport(){
    const dimensions=reportDimensions();if(Object.keys(dimensions).length&&!await confirmBatch('按完整资料范围采集？',{明细筛选:Object.entries(dimensions).map(([key,values])=>futuresFields[key]+'：'+values.map(dimensionLabel).join('、')).join('；'),采集范围:'按所选交易所、品种或合约及日期采集完整资料'}))return;
    pendingReport=futuresQuery(undefined,false);await navigate('collect');collectMode('report');
    const selection=pendingReport.selections?.[0]||pendingReport,current=pendingReport.source==='qmt'&&selection.mapping_mode==='current';
    const description={来源:pendingReport.source==='qmt'?'QMT':'Tushare',资料:reportNames[selection.resource],对象数:pendingReport.selections?.length||1,范围:current?'采集执行时的当前映射快照':pendingReport.start+' 至 '+pendingReport.end};
    $('#collect-report').replaceChildren(create('dl',{className:'detail-grid'},detailFields(description)),command('预览并采集','download',async()=>{
      if(provider!==pendingReport.source)throw new Error('来源已改变，请重新选择采集范围');
      const account=provider==='tushare'?$('#data-account').value:null;
      if(!await confirmBatch('确认资料采集',{...description,账号:account||'不适用'}))return;
      await api('/futures/sync',{...pendingReport,account_id:account});await navigate('jobs');notice('资料采集已排队');
    }),command('保存为维护计划','calendar-clock',()=>editMaintenance(null,false,pendingReport)));icons();
  }

  function setupQueryControls(){
    const jobs=$('#job-filter');jobs.elements.search.previousSibling.textContent='任务 / 数据集 / 维护名称 / 代码';
    jobs.querySelector('[type=submit]').before(trashChoice());
    jobs.elements.start.previousSibling.textContent='创建开始日期';jobs.elements.end.previousSibling.textContent='创建结束日期';
    const origin=create('label',{},'触发方式<select name="origins"><option value="">全部</option>'+Object.entries(originNames).map(([value,label])=>`<option value="${value}">${label}</option>`).join('')+'</select>');
    const account=create('label',{id:'job-account-filter'},'采集账号<select name="account_ids"><option value="">全部账号</option></select>');jobs.querySelector('[type=submit]').before(origin,account);enableMulti(origin.querySelector('select'),'全部');enableMulti(account.querySelector('select'),'全部账号');
    const definitions={'history-form':['time','code','period','open','high','low','close','volume','amount','open_interest'],'futures-form':[], 'job-filter':['created_at','updated_at','state','kind'],'security-form':['code','name','kind','market'],'board-form':['name','category']};
    for(const [id,sorts] of Object.entries(definitions)){
      const form=$('#'+id),tools=create('div',{className:'result-tools'},'<label>每页<select data-page-size><option>50</option><option>100</option><option>200</option></select></label><label>排序<select data-sort>'+sorts.map(key=>`<option value="${key}">${escape(barLabels[key] || {created_at:'创建时间',updated_at:'更新时间',state:'执行状态',kind:'类型',name:'名称',market:'交易所',category:'分类'}[key] || key)}</option>`).join('')+'</select></label><label>方向<select data-direction><option value="asc">升序</option><option value="desc">降序</option></select></label><details class="column-menu"><summary>显示列</summary><div data-columns></div></details><label>已保存查询<select data-saved><option value="">选择查询</option></select></label>');
      const advanced=create('details',{className:'query-options'},'<summary>排序、显示与已保存查询</summary>');advanced.append(tools);form.after(advanced);options.set(id,{limit:50});if(id==='job-filter'){tools.querySelector('[data-direction]').value='desc';options.get(id).direction='desc';}
      tools.append(command('保存查询','bookmark-plus',async()=>{const dialog=addPanel('query-name-panel','保存查询'),editor=create('form',{className:'settings-form'},'<label>名称<input name="name" maxlength="60" required></label><div class="form-actions"><button type="submit" class="primary">保存</button><button type="button" data-close-dialog>取消</button></div>');dialog.append(editor);dialog.addEventListener('close',()=>dialog.remove());editor.onsubmit=action(async()=>{const name=editor.elements.name.value.trim();if(!name)return;const key='pfor:queries:'+id+':'+route+':'+provider,saved=readPreference(key,{});if(Object.hasOwn(saved,name)&&!await confirmBatch('覆盖已保存查询？',{名称:name,影响:'只替换此浏览器中的筛选条件'}))return;saved[name]=formState(form);localStorage.setItem(key,JSON.stringify(saved));dialog.close();refreshSaved();});dialog.showModal();}));
      tools.append(command('重置筛选','filter-x',()=>resetQueryForm(id)));
      function refreshSaved(){const field=tools.querySelector('[data-saved]'),selected=field.value,saved=readPreference('pfor:queries:'+id+':'+route+':'+provider,{});field.innerHTML='<option value="">选择查询</option>'+Object.keys(saved).map(name=>`<option>${escape(name)}</option>`).join('');if(Object.hasOwn(saved,selected))field.value=selected;}
      tools.append(command('删除查询','trash-2',async()=>{const name=tools.querySelector('[data-saved]').value;if(!name)throw new Error('请先选择已保存查询');if(!await confirmBatch('删除已保存查询？',{名称:name,影响:'只删除当前浏览器中的查询偏好，当前查询结果保持不变'}))return;const key='pfor:queries:'+id+':'+route+':'+provider,saved=readPreference(key,{});delete saved[name];localStorage.setItem(key,JSON.stringify(saved));refreshSaved();notice('已删除查询');}));
      tools.querySelector('[data-saved]').onfocus=refreshSaved;
      tools.querySelector('[data-saved]').onchange=action(async event=>{const entries=readPreference('pfor:queries:'+id+':'+route+':'+provider,{})[event.target.value];if(!Array.isArray(entries))return;const restoreFields=()=>{for(const [name,value] of entries){const field=form.elements[name];if(!field||field instanceof RadioNodeList)continue;if(field.multiple){[...field.options].forEach(item=>item.selected=!item.disabled&&Array.isArray(value)&&value.includes(item.value));refreshMulti(field);}else if(field.type==='checkbox')field.checked=value;else field.value=value;}};restoreFields();if(id==='futures-form'){updateFuturesForm();await loadFuturesOptions();restoreFields();restoreDimensionChoices(entries);await loadReportDimensions({force:true});}if(id==='history-form')await resolveSelections();const preset=form.querySelector('[data-date-preset]');if(preset)preset.value='';markQuery(form,'已恢复筛选，尚未应用');});
      tools.addEventListener('change',action(async event=>{if(!event.target.matches('[data-page-size],[data-sort],[data-direction]'))return;options.set(optionKey(id),{limit:Number(tools.querySelector('[data-page-size]').value),sort:tools.querySelector('[data-sort]').value || undefined,direction:tools.querySelector('[data-direction]').value});await reloadDisplay(id);}));
      const table=(id==='security-form'?$('#securities'):id==='board-form'?$('#board-rows'):id==='history-form'?$('#bars'):id==='futures-form'?$('#futures-rows'):$('#job-rows')).closest('table');
      function columns(){const headers=[...table.querySelectorAll('thead th')],key='pfor:columns:v2:'+id+':'+headers.map(th=>th.textContent).join('|');const hidden=readPreference(key,[]).filter(index=>index>0&&index<headers.length-1);const list=tools.querySelector('[data-columns]');list.innerHTML=headers.map((th,index)=>`<label><input type="checkbox" data-column="${index}" ${index===0||index===headers.length-1?'disabled':''} ${hidden.includes(index)?'':'checked'}>${escape(th.textContent)}</label>`).join('');[...table.rows].forEach(tr=>[...tr.cells].forEach((td,index)=>td.hidden=td.colSpan===1&&hidden.includes(index)));list.onchange=()=>{const off=[...list.querySelectorAll('input:not(:checked)')].map(el=>Number(el.dataset.column));localStorage.setItem(key,JSON.stringify(off));[...table.rows].forEach(tr=>[...tr.cells].forEach((td,index)=>td.hidden=td.colSpan===1&&off.includes(index)));};}
      new MutationObserver(columns).observe(table.querySelector('tbody'),{childList:true});columns();
      form.addEventListener('input',()=>markQuery(form,'筛选尚未应用，点击查询应用'));form.addEventListener('change',()=>markQuery(form,'筛选尚未应用，点击查询应用'));
    }
  }
  function markQuery(form,text){let note=form.querySelector('.query-state');if(!note){note=create('p',{className:'query-state',role:'status'});form.append(note);}note.textContent=text;}
  function markAppliedRange(form,query,total){
    let draft;try{const value=values(form);draft=form.id==='history-form'?{source:provider,members:splitCodes(value.code),periods:selectedValues(form.elements.period),start:value.start,end:value.end}:futuresQuery(futuresActive);}catch{}
    const {snapshot,...scope}=query;
    markQuery(form,(JSON.stringify(draft)===JSON.stringify(scope)?'已应用 ':'筛选尚未应用，当前结果为 ')+query.start+' 至 '+query.end+' · 完整范围 '+total+' 条');
  }
  function readPreference(key,fallback){try{const value=JSON.parse(localStorage.getItem(key));return value&&typeof value==='object'&&Array.isArray(value)===Array.isArray(fallback)?value:fallback;}catch{return fallback;}}
  function setupSmallLists(){for(const kind of ['accounts','indices']){
    const form=create('form',{id:kind+'-filter',className:'toolbar'},'<label class="grow">'+(kind==='accounts'?'账号名称 / ID / 端点':'已保存指数名称 / 代码 / 板块')+'<input name="search" type="search"></label>'+(kind==='accounts'?'<label>状态<select name="enabled"><option value="">全部</option><option value="true">启用</option><option value="false">停用</option></select></label>':'')+'<button type="submit">'+icon('search')+'查询</button><button type="reset" class="icon" title="重置筛选" aria-label="重置筛选">'+icon('filter-x')+'</button><span id="'+kind+'-filter-count" role="status"></span>');
    $(kind==='accounts'?'#account-rows':'#index-rows').closest('.table-wrap').before(form);form.onsubmit=action(()=>filterLocalRows(kind,true));form.onreset=()=>setTimeout(()=>filterLocalRows(kind,true),0);form.oninput=()=>markQuery(form,'筛选尚未应用，点击查询应用');
  }}
  function filterLocalRows(kind,apply=false){
    const id=kind+'-filter';if(!$('#'+id))return;
    const query=listQuery(id,apply),items=kind==='accounts'?accounts:state.indices,body=$(kind==='accounts'?'#account-rows':'#index-rows'),search=(query.search||'').trim().toLowerCase();body.querySelector('[data-filter-empty]')?.remove();let count=0;
    [...body.rows].forEach((tr,index)=>{const row=items[index];if(!row)return;const match=[row.name,row.id,row.endpoint,row.code,row.sector].filter(Boolean).join(' ').toLowerCase().includes(search)&&(kind!=='accounts'||!query.enabled||String(row.enabled)===query.enabled);tr.hidden=!match;if(match)count++;});
    if(!count&&items.length)body.insertAdjacentHTML('beforeend','<tr data-filter-empty><td class="empty" colspan="'+(kind==='accounts'?4:5)+'">没有匹配记录</td></tr>');
    $('#'+kind+'-filter-count').textContent=`匹配 ${count} / 共 ${items.length} 项`;listLoaded(id,query,count);
  }
  function invalidate(key){const form=$(key==='history'?'#history-form':'#futures-form');markQuery(form,'筛选已修改，当前结果仍为上次查询');}
  function setupAnalysis(){
    const runtimeForm=create('form',{id:'runtime-filter',className:'toolbar'},'<label class="grow">最近日志中的错误码 / 内容 / 队列<input type="search" name="search"></label><button type="submit">'+icon('search')+'查询 / 刷新</button><button type="reset" class="icon" title="清空日志筛选" aria-label="清空日志筛选">'+icon('filter-x')+'</button>');$('#runtime .section-heading').after(runtimeForm);runtimeForm.onsubmit=action(loadRuntime);runtimeForm.onreset=()=>setTimeout(renderRuntime,0);
    $('#freshness-filter').append(create('label',{},'状态<select name="states"><option value="">全部</option>'+Object.entries(freshnessNames).map(([key,label])=>`<option value="${key}">${label}</option>`).join('')+'</select>'));enableMulti($('#freshness-filter [name=states]'),'全部');
    $('#freshness-filter').before(create('div',{id:'freshness-metrics',className:'metric-strip'}));
    setupHistorySummary();
    $('#chart-selection').hidden=false;
    $('.chart-area').before($('#chart-selection'));
    $('#chart-selection').append(command('更早数据','chevron-left',()=>loadChartWindow('before')),command('更新数据','chevron-right',()=>loadChartWindow('after')));
    $('#chart-selection').insertAdjacentHTML('beforeend','<label>图表窗口<select id="chart-window"><option value="1000">1000条</option><option value="5000">5000条</option></select></label><span id="chart-window-note" role="status"></span>');$('#chart-window').onchange=()=>loadChartWindow();
    $('#futures-form').after(create('section',{id:'report-visual',hidden:true},'<div class="result-tools"><label>指标<select id="report-metric"></select></label><label>展示日期<input id="report-day" type="date"></label><label>序列<select id="report-series" multiple></select></label><button type="button" id="report-render">应用图表选择</button></div><p id="report-summary" class="catalog-meta"></p><div id="report-calendar"></div><div id="report-chart" class="analysis-chart"></div>'));
    $('#report-render').onclick=()=>renderReport();
    $('#report-render').after(create('label',{id:'calendar-layout-label',hidden:true},'日历视图<select id="calendar-layout"><option value="both">月历与列表</option><option value="month">月历</option><option value="list">列表</option></select>'));
    $('#calendar-layout').onchange=()=>calendarLayout();
    enableMulti($('#report-series'),'选择图表序列',true);
    $('#overview').prepend(create('form',{id:'overview-filter',className:'toolbar'},'<label>来源<select name="sources"><option value="">全部</option><option value="qmt">QMT</option><option value="tushare">Tushare</option></select></label><label>创建开始日期<input type="date" name="start"></label><label>创建结束日期<input type="date" name="end"></label><button type="submit">查询统计</button>'));
    $('#overview-filter [name=start]').value=today(-30);$('#overview-filter [name=end]').value=today();$('#overview-filter').onsubmit=action(operationCharts);
    attachDateRange($('#overview-filter'));
    $('#overview-filter').after(create('div',{id:'operations-metrics',className:'metric-strip'}),create('div',{id:'operations-chart',className:'analysis-chart'}));
  }
  function setupHistorySummary(){
    const panel=create('details',{id:'history-summary',className:'history-summary',open:true,hidden:true},'<summary><strong>范围汇总</strong><span id="history-summary-count"></span></summary><div class="result-tools"><label class="summary-search">合约名称 / 代码<input id="history-summary-search" type="search"></label><label>每页<select id="history-summary-size"><option value="5">5 组</option><option value="10">10 组</option><option value="20">20 组</option></select></label><span id="history-summary-range"></span></div><div class="table-wrap" tabindex="0" role="region" aria-label="行情范围汇总"><table><thead><tr><th>合约 / 周期</th><th class="numeric">记录数</th><th class="numeric">最高 / 最低</th><th class="numeric">成交量</th><th class="numeric">成交额</th><th class="numeric">最新持仓</th><th>操作</th></tr></thead><tbody id="history-summary-rows"></tbody></table></div><div class="summary-footer"><span id="history-summary-units"></span><div class="pagination"><span id="history-summary-page" role="status"></span></div></div>');
    $('#history-form').after(panel);
    for(const [id,label,glyph,step] of [['prev','上一页汇总','chevron-left',-1],['next','下一页汇总','chevron-right',1]]){const button=command('',glyph,()=>{summaryOffset+=step*Number($('#history-summary-size').value);renderHistorySummary();});button.id='history-summary-'+id;button.title=button.ariaLabel=label;panel.querySelector('.pagination').append(button);}
    $('#history-summary-search').oninput=()=>{summaryOffset=0;renderHistorySummary();};$('#history-summary-size').onchange=()=>{summaryOffset=0;renderHistorySummary();};
    $('#history-summary-rows').addEventListener('click',action(async event=>{const button=event.target.closest('[data-summary-chart]');if(!button)return;$('#chart-code').value=button.dataset.summaryChart;$('#chart-period').value=button.dataset.period;await loadChartWindow();$('.chart-area').scrollIntoView({block:'start',behavior:'instant'});}));
  }
  function renderHistorySummary(){
    const panel=$('#history-summary');panel.hidden=!historySummary;if(!historySummary)return;
    const summary=historySummary,query=applied.get('history'),names=summary.instrument_names||{},search=$('#history-summary-search').value.trim().toLocaleLowerCase(),size=Number($('#history-summary-size').value);
    const filtered=summary.groups.filter(row=>((names[row.code]||'')+' '+row.code+' '+(periodNames[row.period]||row.period)).toLocaleLowerCase().includes(search));
    summaryOffset=Math.min(Math.max(0,summaryOffset),Math.max(0,Math.ceil(filtered.length/size)-1)*size);
    const rows=filtered.slice(summaryOffset,summaryOffset+size);
    $('#history-summary-count').textContent=summary.groups.length+' 组 · '+summary.total+' 条';
    $('#history-summary-range').textContent=(query.source==='tushare'?'Tushare':'QMT')+' · '+query.start+' 至 '+query.end;
    const units=recordSets.history?.note||'';$('#history-summary-units').textContent=units;
    recordSets.historySummary={title:'行情范围汇总详情',rows:rows.map(row=>({...row,name:names[row.code]||null})),labels:{source:'数据来源',code:'合约代码',name:'合约名称',period:'周期',count:'记录数',first_time:'首条时间',last_time:'末条时间',high:'最高价',low:'最低价',volume:'成交量',amount:'成交额',latest_open_interest:'末条持仓',last_write:'最近入库时间'},note:units};
    const value=(row,key)=>`<span title="${escape(number(row[key]))}">${escape(compact(row[key]))}</span>`;
    $('#history-summary-rows').innerHTML=rows.map((row,index)=>`<tr><td><strong>${escape(names[row.code]||'名称未收录')}</strong><span class="muted">${escape(row.code)} · ${escape(periodNames[row.period]||row.period)}</span></td><td class="numeric">${row.count}</td><td class="numeric">${value(row,'high')}<span class="muted">${value(row,'low')}</span></td><td class="numeric">${value(row,'volume')}</td><td class="numeric">${value(row,'amount')}</td><td class="numeric">${value(row,'latest_open_interest')}</td><td><div class="actions"><button type="button" data-summary-chart="${escape(row.code)}" data-period="${escape(row.period)}" title="查看此组图表" aria-label="查看此组图表">${icon('chart-candlestick')}</button>${recordButton('historySummary',index)}</div></td></tr>`).join('')||'<tr><td colspan="7" class="empty">'+(summary.groups.length?'没有匹配的汇总分组':'所选范围没有入库行情')+'</td></tr>';
    $('#history-summary-page').textContent=(search?'匹配 ':'共 ')+filtered.length+' 组 · '+(rows.length?summaryOffset+1:0)+'–'+(rows.length?summaryOffset+rows.length:0);
    $('#history-summary-prev').disabled=!summaryOffset;$('#history-summary-next').disabled=summaryOffset+size>=filtered.length;icons();
  }
  async function historyLoad(offset=0,apply=false){
    const request=++historyRequest,source=provider,form=$('#history-form'),draft=values(form);
    const query=!apply&&applied.get('history')?{...applied.get('history')}: {source,members:splitCodes(draft.code),periods:selectedValues(form.elements.period),start:draft.start,end:draft.end};
    if(!query.members.length||!query.periods.length)throw new Error('请选择证券和周期');
    markQuery(form,'正在查询');
    try{
      const result=await api('/history/query',{...query,...pageOptions('history-form'),offset,console:true});
      const summary=historySummary?.snapshot===result.snapshot?historySummary:await api('/history/summary',{...query,snapshot:result.snapshot});
      if(request!==historyRequest||source!==provider||route!=='history')return;
      const previous=applied.get('history'),keepChart=!apply&&previous?.snapshot===result.snapshot&&historyWindow;historySummary=summary;
      applied.set('history',previous?.snapshot===result.snapshot?previous:{...query,snapshot:result.snapshot});state.offset=offset;state.next=result.next_offset;
      historySelection=query;
      $('#page-status').textContent=`共 ${result.total} 条 · ${result.rows.length?offset+1:0}–${offset+result.rows.length}`;$('#prev-page').disabled=!offset;$('#next-page').disabled=result.next_offset===null;
      const fields=['code','period','time','trading_day','as_of_date','open','high','low','close','volume','amount','open_interest','settlement','previous_settlement'];
      const head=$('#bars').closest('table').querySelector('thead');head.innerHTML='<tr>'+fields.map(key=>'<th>'+escape(barLabels[key]||key)+'</th>').join('')+'<th>详情</th></tr>';
      recordSets.history={title:'历史行情详情',rows:result.rows,labels:barLabels,note:Object.entries(result.units).map(([key,value])=>(barLabels[key]||key)+'：'+value).join(' · ')};
      $('#bars').innerHTML=result.rows.map((row,index)=>'<tr>'+fields.map(key=>`<td class="${fields.indexOf(key)>4?'numeric':''}" title="${escape(number(row[key]))}">${escape(key==='code'?securityLabel(row[key]):key==='period'?periodNames[row[key]]:key==='time'?formatTime(row[key]):key==='trading_day'?row[key]||'待核验':key==='as_of_date'?row[key]||'—':compact(row[key]))}${key==='as_of_date'&&['1w','1mo'].includes(row.period)&&row.as_of_date&&row.time.slice(0,10)>row.as_of_date?'<span class="badge partial">周期未结束</span>':''}</td>`).join('')+`<td>${recordButton('history',index)}</td></tr>`).join('') || '<tr><td colspan="15" class="empty">所选范围没有入库行情</td></tr>';
      $('#history-units').textContent=source+' · '+recordSets.history.note+' · '+result.date_basis;
      if(apply||previous?.snapshot!==summary.snapshot){summaryOffset=0;$('#history-summary-search').value='';}
      renderHistorySummary();
      for(const [id,items] of [['chart-code',query.members],['chart-period',query.periods]]){const select=$('#'+id),old=select.value;select.innerHTML=items.map(value=>`<option value="${escape(value)}">${escape(id==='chart-code'?securityLabel(value):periodNames[value])}</option>`).join('');if(items.includes(old))select.value=old;}
      $('#chart-selection').hidden=false;markAppliedRange(form,query,result.total);if(!keepChart)await loadChartWindow();icons();
    }catch(error){if(request!==historyRequest||source!==provider||route!=='history')return;markQuery(form,'查询失败：'+error.message);throw error;}
  }
  async function loadChartWindow(direction){
    const selection=applied.get('history');if(!selection)return;const request=++chartRequest;
    const code=$('#chart-code').value,period=$('#chart-period').value;
    const p={source:selection.source,code,period,start:selection.start,end:selection.end,limit:Number($('#chart-window').value),query_scope:selection};
    if(direction&&historyWindow?.rows.length)p[direction]=(direction==='before'?historyWindow.actual_start:historyWindow.actual_end);
    let result;try{result=await api('/history/chart',p);}catch(error){if(request===chartRequest&&selection===applied.get('history')){chart?.clear();$('#chart-empty').textContent='图表读取失败：'+error.message;$('#chart-empty').hidden=false;$('#chart-window-note').textContent='图表未更新';}return;}if(request!==chartRequest||selection!==applied.get('history'))return;
    $('#chart-empty').textContent='暂无历史行情';
    historyWindow=result;$('#chart-empty').hidden=!!result.rows.length;$('#chart-window-note').textContent=result.rows.length?`${formatTime(result.actual_start)} 至 ${formatTime(result.actual_end)} · 窗口 ${result.rows.length} / 共 ${result.total} 条`:'暂无历史行情';
    const buttons=$$('#chart-selection button');buttons[0].disabled=!result.has_previous;buttons[1].disabled=!result.has_next;
    if(!chart)chart=echarts.init($('#chart'));const rows=timelineRows(result.rows,period),dates=rows.map(row=>row.gap?'数据间隔':formatTime(row.time));
    chart.setOption({animation:false,legend:{data:['K线','成交量','持仓量']},tooltip:{trigger:'axis',formatter:items=>{const row=rows[items[0]?.dataIndex];return row?.gap?'此时间段没有记录':row?escape(formatTime(row.time))+'<br>'+['open','high','low','close','volume','open_interest'].map(key=>escape(barLabels[key])+': '+escape(number(row[key]))).join('<br>'):'';}},axisPointer:{link:[{xAxisIndex:'all'}]},grid:[{left:76,right:70,top:40,height:'48%'},{left:76,right:70,top:'66%',height:'17%'}],xAxis:[{type:'category',data:dates,axisLabel:{show:false}},{type:'category',gridIndex:1,data:dates,axisLabel:{hideOverlap:true}}],yAxis:[{scale:true},{gridIndex:1,scale:true},{gridIndex:1,scale:true,position:'right'}],dataZoom:[{type:'inside',xAxisIndex:[0,1]},{type:'slider',xAxisIndex:[0,1],bottom:8,height:20}],series:[{name:'K线',type:'candlestick',data:rows.map(row=>[row.open,row.close,row.low,row.high].map(value=>value==null?'-':Number(value))),itemStyle:{color:'#c74444',color0:'#168260',borderColor:'#c74444',borderColor0:'#168260'}},{name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:rows.map(row=>numeric(row.volume)),itemStyle:{color:'#8ba9c4'}},{name:'持仓量',type:'line',xAxisIndex:1,yAxisIndex:2,connectNulls:false,showSymbol:false,data:rows.map(row=>numeric(row.open_interest)),lineStyle:{color:'#a06c19'}}]},true);chart.resize();
  }
  async function exportHistory(format){const p=applied.get('history');if(!p)throw new Error('请先查询，再导出已应用的范围');if(!await confirmBatch('导出已查询行情',{来源:p.source,合约数:p.members.length,周期:p.periods.join('、'),日期:p.start+' 至 '+p.end,格式:format.toUpperCase()}))return;await api(p.periods.length>1?'/exports/batch':'/exports',{...p,period:p.periods[0],format});await navigate('exports');notice('导出已排队');}
  async function futuresLoad(offset=0,apply=false){
    const key=route,request=++futuresRequest,form=$('#futures-form'),p=!apply&&applied.get(key)?applied.get(key):futuresQuery(futuresActive);
    markQuery(form,'正在查询');try{
      const result=await api('/futures/records',{...p,...pageOptions('futures-form'),offset});
      const summary=reportSummary?.snapshot===result.snapshot&&reportSummary.resource===result.resource?reportSummary:await api('/futures/summary',{...p,snapshot:result.snapshot});
      if(request!==futuresRequest||key!==route||provider!==p.source)return;
      const keepChart=summary===reportSummary;
      summary.instrument_names={...summary.instrument_names,...result.instrument_names};
      applied.set(key,{...p,snapshot:result.snapshot});reportSummary=summary;futuresOffset=offset;futuresNext=result.next_offset;
      $('#futures-units').textContent=result.units+(p.dimensions?' · 已应用明细：'+Object.entries(p.dimensions).map(([key,values])=>futuresFields[key]+' '+values.map(dimensionLabel).join('、')).join('；'):'');const fields=result.fields.filter(key=>!['source','normalization_version','original_amount','original_cumamt'].includes(key));
      recordSets.futures={title:reportNames[result.resource]+'详情',rows:result.rows,labels:futuresFields,note:result.units,extra:row=>['mapping','settle'].includes(result.resource)?'<h3>合约名称</h3><dl class="detail-grid">'+detailFields(result.resource==='mapping'?{主力连续:reportInstrumentName(row.code),对应月份合约:reportInstrumentName(row.member_code)}:{合约:reportInstrumentName(row.ts_code)})+'</dl>':''};
      $('#futures-head').innerHTML='<tr>'+fields.map(key=>'<th>'+escape(futuresFields[key]||key)+'</th>').join('')+'<th>详情</th></tr>';
      $('#futures-rows').innerHTML=result.rows.map((row,index)=>'<tr>'+fields.map(key=>`<td class="${reportNumeric.has(key)?'numeric':''}" title="${escape(number(row[key]))}">${['mapping','settle'].includes(result.resource)&&['code','member_code','ts_code'].includes(key)?reportCodeCell(row[key],key==='member_code'):escape(reportDisplay(key,row[key]))}</td>`).join('')+`<td>${recordButton('futures',index)}</td></tr>`).join('') || '<tr><td class="empty">所选范围没有已入库资料</td></tr>';
      $('#futures-page').textContent=`共 ${result.total} 条 · ${result.rows.length?offset+1:0}–${offset+result.rows.length}`;$('#futures-prev').disabled=!offset;$('#futures-next').disabled=result.next_offset===null;
      reportColumns.set(key,fields);restoreOptions('futures-form',key);
      $('#report-visual').hidden=result.resource==='mapping'&&result.mapping_mode==='current';if(!keepChart)prepareReport();markAppliedRange(form,p,result.total);
      calendarLayout();
      if(result.resource==='mapping'&&result.mapping_mode==='current')markQuery(form,'当前已保存快照 · 共 '+result.total+' 个对象');
      icons();
    }catch(error){if(request!==futuresRequest||key!==route||provider!==p.source)return;markQuery(form,'查询失败：'+error.message);throw error;}
  }
  async function exportFutures(format){const p=applied.get(route);if(!p)throw new Error('请先查询，再导出已应用范围');if(!await confirmBatch('导出已查询资料',{资料:reportNames[route],开始:p.start,结束:p.end,明细筛选:p.dimensions||'全部',格式:format.toUpperCase()}))return;await api('/futures/export',{...p,format});await navigate('exports');notice('资料导出已排队');}

  function reportDisplay(key,value){
    if(key==='observed_at')return value?formatTime(value):'未提供';
    if(key==='evidence')return {calendar:'交易所日历',calendar_open:'已返回开市日期',bar_observation:'K线日期观察',legacy:'旧记录，依据未核验'}[value]||value;
    if(key==='market')return marketNames[value]||value;
    if(key==='is_open')return value===null?'未知':value?'开市':'休市';
    return reportNumeric.has(key)?compact(value):number(value);
  }
  function calendarLayout(){
    const mode=reportSummary?.resource==='calendar'?$('#calendar-layout').value:'both';
    $('#report-calendar').hidden=mode==='list';
    $('#futures-rows').closest('.table-wrap').hidden=mode==='month';
    $('#futures-page').closest('.pagination').hidden=mode==='month';
  }
  function reportKey(row,resource){return JSON.stringify(resource==='warehouse'?[row.exchange,row.symbol,row.warehouse,row.wh_id,row.area,row.year,row.grade,row.brand,row.place,row.is_ct,row.unit]:resource==='holding'?[row.exchange,row.symbol,row.broker]:resource==='settle'?[row.ts_code]:resource==='weekly_detail'?[row.exchange,row.prd]:resource==='mapping'?[row.code]:[row.market]);}
  function reportInstrumentName(code){return reportSummary?.instrument_names?.[code] || '名称未收录 · '+code;}
  function reportSeriesLabel(key){
    const parts=JSON.parse(key),resource=reportSummary?.resource;
    if(!['mapping','settle'].includes(resource))return parts.filter(value=>value!=null&&value!=='').join(' / ');
    return reportChartName(parts[0]);
  }
  function reportChartName(code){const name=reportInstrumentName(code);return reportNameCounts.get(name)>1?name+' · '+code:name;}
  function reportCodeCell(code,mapped=false){
    const name=escape(reportInstrumentName(code)),aux='<span class="muted">'+escape(code)+'</span>';
    return mapped?`<button type="button" class="text-link" data-mapped-history="${escape(code)}" title="查看 ${name} 历史行情" aria-label="查看 ${name} 历史行情">${name}${icon('arrow-up-right')}</button>${aux}`:'<span class="instrument-label">'+name+'</span>'+aux;
  }
  function prepareReport(){
    $('#report-visual').hidden=reportSummary.resource==='mapping'&&reportSummary.mapping_mode==='current';
    $('#calendar-layout-label').hidden=reportSummary.resource!=='calendar';
    const r=reportSummary.resource,metrics={calendar:['is_open'],mapping:['member_code'],warehouse:['vol','vol_chg','pre_vol'],holding:['vol','long_hld','short_hld','vol_chg','long_chg','short_chg'],settle:['settle','trading_fee_rate','trading_fee','delivery_fee','long_margin_rate','short_margin_rate','offset_today_fee'],weekly_detail:['vol','amount','open_interest','mc_close','vol_yoy','amout_yoy','interest_wow','close_wow']}[r];
    $('#report-metric').innerHTML=metrics.map(key=>`<option value="${key}">${escape(futuresFields[key]||key)}</option>`).join('');
    const groups=new Map();reportSummary.rows.forEach(row=>groups.set(reportKey(row,r),row));
    reportNameCounts.clear();for(const code of new Set(reportSummary.rows.flatMap(row=>[row.code,row.member_code,row.ts_code].filter(Boolean)))){const name=reportInstrumentName(code);reportNameCounts.set(name,(reportNameCounts.get(name)||0)+1);}
    $('#report-series').innerHTML=[...groups].map(([key,row],index)=>`<option value="${escape(key)}" ${index<6?'selected':''}>${escape(reportSeriesLabel(key))}</option>`).join('');
    refreshMulti($('#report-series'));
    $('#report-day').value=reportSummary.last_date?.slice(0,10) || today();$('#report-day').closest('label').hidden=!['calendar','holding'].includes(r);
    if(!$('#report-mode')){$('#report-day').closest('label').after(create('label',{},'视图<select id="report-mode"><option value="trend">趋势</option><option value="day">单日对比</option></select>'));}
    $('#report-mode').closest('label').hidden=r!=='holding';$('#report-mode').value=r==='holding'?'day':'trend';
    renderReport();
  }
  function renderReport(){
    if(!reportSummary)return;const r=reportSummary.resource,metric=$('#report-metric').value,keys=[...$('#report-series').selectedOptions].map(el=>el.value);
    if(keys.length>6){notice('每幅图最多比较六条序列，请缩小图表选择');return;}
    const rows=reportSummary.rows.filter(row=>keys.includes(reportKey(row,r))),dateKey=r==='calendar'?'day':r==='mapping'?'trading_day':r==='weekly_detail'?'week_date':'trade_date';
    $('#report-summary').textContent=`共 ${reportSummary.total} 条 · ${reportSummary.first_date || '无起始日期'} 至 ${reportSummary.last_date || '无结束日期'} · 图表 ${keys.length} 条序列${reportSummary.truncated?' · 图表数据仅为最新5000条，请缩小日期范围查看完整序列':''} · ${reportSummary.units}`;
    if(['mapping','settle'].includes(r))$('#report-summary').textContent=(keys.length?keys.map(reportSeriesLabel).join('、'):'未选择合约')+' · '+$('#report-summary').textContent;
    $('#report-calendar').replaceChildren();$('#report-chart').hidden=r==='calendar';
    if(r==='mapping'&&reportSummary.mapping_mode==='current'){
      $('#report-chart').hidden=true;$('#report-calendar').textContent='当前映射按最新采集快照展示；历史变化请切换历史逐日映射。';return;
    }
    if(r==='calendar'){
      const month=$('#report-day').value.slice(0,7),first=new Date(month+'-01T00:00:00Z'),last=new Date(Date.UTC(first.getUTCFullYear(),first.getUTCMonth()+1,0)).getUTCDate();
      for(const key of keys){const market=JSON.parse(key)[0],calendar=create('section',{className:'calendar-month'},'<h3>'+escape(marketNames[market]||market)+' · '+month+'</h3><div class="calendar-grid">'+['一','二','三','四','五','六','日'].map(label=>'<strong>'+label+'</strong>').join('')+'</div>'),grid=calendar.lastElementChild;
        for(let blank=0;blank<(first.getUTCDay()+6)%7;blank++)grid.append(create('span'));
        for(let d=1;d<=last;d++){const day=month+'-'+String(d).padStart(2,'0'),row=rows.find(row=>row.market===market&&row.day===day);grid.append(create('div',{className:'calendar-day '+(row?(row.is_open?'open':'closed'):'unknown')},'<b>'+d+'</b><small>'+(row?(row.is_open?'开市':'休市'):'未知')+'</small>'));}
        $('#report-calendar').append(calendar);
      }return;
    }
    if(r==='warehouse'&&new Set(rows.map(row=>row.unit)).size>1){$('#report-chart').hidden=true;$('#report-summary').textContent+=' · 所选序列单位不同，请按同一单位选择；不合计汇总与明细';return;}
    let instance=charts.get('report');if(!instance){instance=echarts.init($('#report-chart'));charts.set('report',instance);}
    const dayMode=r==='holding'&&$('#report-mode').value==='day';
    let data=rows;if(dayMode)data=rows.filter(row=>row[dateKey]===$('#report-day').value).sort((a,b)=>Number(b[metric]??-Infinity)-Number(a[metric]??-Infinity));
    const observed=[...new Set(data.map(row=>row[dateKey]))].sort(),dates=[];for(const date of observed){const previous=dates.at(-1);if(previous&&new Date(date)-new Date(previous)>(r==='weekly_detail'?7:1)*86400000)dates.push('间隔 '+previous);dates.push(date);}const lookup=new Map(data.map(row=>[reportKey(row,r)+'|'+row[dateKey],row])),members=[...new Set(rows.map(row=>row.member_code))].filter(Boolean);
    const named=['mapping','settle'].includes(r),labels=key=>named?key:reportSeriesLabel(key);
    const series=dayMode?[{name:futuresFields[metric],type:'bar',data:data.map(row=>numeric(row[metric]))}]:keys.map(key=>({name:labels(key),type:'line',connectNulls:false,showSymbol:true,step:r==='mapping'?'end':false,data:dates.map(date=>{const row=lookup.get(key+'|'+date);return row?(r==='mapping'?row.member_code:numeric(row[metric])):null;})}));
    instance.setOption({animation:false,color:['#12674a','#357ca5','#b8791d','#915b96','#bf5555','#52666e'],legend:{type:'scroll',data:series.map(item=>item.name),formatter:name=>named?reportSeriesLabel(name):name},tooltip:{trigger:'axis',confine:true,...(named?{formatter:items=>{const date=dates[items[0]?.dataIndex];return escape(date||'')+items.map(item=>{const row=lookup.get(item.seriesName+'|'+date);if(!row)return '';const code=JSON.parse(item.seriesName)[0];return '<br><strong>'+escape(reportSeriesLabel(item.seriesName))+'</strong><span class="muted">'+escape(code)+'</span>'+escape(futuresFields[metric]||metric)+'：'+(r==='mapping'?escape(reportInstrumentName(row.member_code))+'<span class="muted">'+escape(row.member_code)+'</span>':escape(number(row[metric])));}).join('');}}:{})},grid:{left:r==='mapping'?120:75,right:25,top:52,bottom:dayMode?85:65},xAxis:{type:'category',data:dayMode?data.map(row=>row.broker):dates,axisLabel:{hideOverlap:true,rotate:dayMode?25:0}},yAxis:r==='mapping'?{type:'category',data:members,axisLabel:{formatter:reportChartName,width:104,overflow:'truncate'}}:{type:'value',scale:true},dataZoom:[{type:'inside'},{type:'slider',height:20,bottom:8}],series},true);instance.resize();
  }
  async function operationCharts(){
    const request=++operationsChartRequest,source=provider,p=values($('#overview-filter'));
    if(!operationsDatabaseReady){await loadOperations('overview');if(request!==operationsChartRequest||route!=='overview'||source!==provider)return;}
    if(!operationsDatabaseReady){$('#operations-metrics').textContent='数据库不可用，统计暂不可读取';charts.get('operations')?.clear();return;}
    const result=await api('/operations/summary',p);if(request!==operationsChartRequest||route!=='overview'||source!==provider)return;
    $('#operations-metrics').innerHTML=result.counts.map(row=>`<button type="button" data-state="${row.state}" data-source="${row.source}"><span>${escape(row.source)} · ${statuses[row.state]}</span><strong>${row.count}</strong></button>`).join('') || '<p>所选范围暂无任务</p>';
    $('#operations-metrics').onclick=event=>{const button=event.target.closest('[data-state]');if(!button)return;navigate('jobs').then(()=>{const form=$('#job-filter');form.reset();form.elements.states.value=button.dataset.state;form.elements.sources.value=button.dataset.source;form.elements.start.value=p.start;form.elements.end.value=p.end;form.querySelectorAll('select[multiple]').forEach(refreshMulti);loadJobs(0,true);});};
    const dates=[...new Set(result.trend.map(row=>row.day))],states=[...new Set(result.trend.map(row=>row.state))];let instance=charts.get('operations');if(!instance){instance=echarts.init($('#operations-chart'));charts.set('operations',instance);}
    instance.setOption({animation:false,tooltip:{trigger:'axis'},legend:{data:states.map(key=>statuses[key])},grid:{left:45,right:20,bottom:50,top:45},xAxis:{type:'category',data:dates},yAxis:{type:'value',minInterval:1},series:states.map(key=>({type:'bar',stack:'jobs',name:statuses[key],data:dates.map(date=>result.trend.find(row=>row.day===date&&row.state===key)?.count||0)}))},true);instance.resize();
  }
  function setupTaskFilters(){
    for(const tab of ['units','events']){
      const unit=tab==='units',id=unit?'task-unit-filter':'task-event-filter';
      const html='<label class="grow">'+(unit?'对象 / 周期 / 错误':'事件说明 / 错误')+'<input name="search" type="search"></label>'+(unit?'<label>执行状态<select name="states"><option value="">全部</option>'+Object.entries(statuses).filter(([key])=>key!=='completed').map(([key,label])=>`<option value="${key}">${label}</option>`).join('')+'</select></label><label>数据质量<select name="quality_states"><option value="">全部</option>'+Object.entries(qualityNames).map(([key,label])=>`<option value="${key}">${label}</option>`).join('')+'</select></label>':'<label>级别<select name="levels"><option value="">全部</option>'+Object.entries(levelNames).map(([key,label])=>`<option value="${key}">${label}</option>`).join('')+'</select></label><label>开始日期<input name="start" type="date"></label><label>结束日期<input name="end" type="date"></label>')+'<label>每页<select name="limit"><option>50</option><option>100</option><option>200</option></select></label><button type="submit">'+icon('search')+'查询</button><button type="reset" class="icon" title="重置筛选" aria-label="重置筛选">'+icon('filter-x')+'</button><span id="'+(unit?'task-unit-count':'task-event-count')+'" role="status"></span>';
      const form=create('form',{id,className:'toolbar',hidden:true},html);$('#task-body').before(form);for(const select of form.querySelectorAll('select:not([name=limit])'))enableMulti(select,'全部');if(!unit)attachDateRange(form);
      form.onsubmit=action(()=>taskTab(tab,0,null,true));form.onreset=()=>setTimeout(()=>{form.querySelectorAll('select[multiple]').forEach(refreshMulti);taskTab(tab,0,null,true).catch(error=>notice(error.message));},0);
      form.oninput=()=>markQuery(form,'筛选尚未应用，点击查询应用');form.onchange=event=>{if(event.target.name==='limit')taskTab(tab).catch(error=>notice(error.message));};
    }
  }
  async function showTask(id,tab='summary'){
    if(taskJobId!==id){taskJobId=id;taskApplied.clear();for(const form of $$('#task-detail form')){form.reset();form.querySelectorAll('select[multiple]').forEach(refreshMulti);}}
    const request=++detailRequest;activeJob=null;$('#task-body').textContent='正在读取任务';$$('#task-tabs button').forEach(button=>button.disabled=true);$('#task-unit-filter').hidden=$('#task-event-filter').hidden=true;
    let job;try{job=await api('/jobs/'+id);}catch(error){if(request===detailRequest)$('#task-body').textContent='任务读取失败：'+error.message;throw error;}if(request!==detailRequest||route!=='task/'+id)return;
    activeJob=job;$('#task-identity').textContent=id;await taskTab(tab);
    if(activeJob?.id===id&&route==='task/'+id)$$('#task-tabs button').forEach(button=>button.disabled=false);
  }
  async function taskTab(tab,offset=0,before=null,apply=false){
    if(!activeJob)return;const id=activeJob.id,request=++detailRequest,previousTab=currentTaskTab;
    currentTaskTab=tab;$('#task-unit-filter').hidden=tab!=='units';$('#task-event-filter').hidden=tab!=='events';
    $$('#task-tabs button').forEach(el=>{el.classList.toggle('active',el.dataset.taskTab===tab);el.setAttribute('aria-selected',el.dataset.taskTab===tab);});
    const body=$('#task-body');if(previousTab!==tab||!body.querySelector('#task-records'))body.textContent='正在读取';
    if(tab==='summary'){
      const job=activeJob,p=job.payload;
      const qualityText=(job.result.quality_summary||[]).map(item=>(qualityNames[item.quality_state]||item.quality_state)+' '+item.count+' 块').join('、');
      body.innerHTML='<div class="metric-strip"><div><span>执行状态</span><strong>'+statuses[job.state]+'</strong></div><div><span>入库 / 核验行数</span><strong>'+escape(number(job.result.rows))+'</strong></div></div><dl class="detail-grid">'+detailFields({来源:p.source || 'qmt',账号:p.account_id || '不适用',固定端点:p.endpoint || '本地行情桥',开始:p.start,结束:p.end,成员:p.members?.join('、'),周期:p.periods?.join('、'),质量:qualityText || '暂无核验记录',处理结论:job.result.message || '未提供',核验规则:job.result.rule_version || '未核验',原因:job.error || '无',下一步:job.action || '查看质量分块及事件'})+'</dl><div class="toolbar">'+(['queued','running','retrying'].includes(job.state)?`<button data-job="cancel" data-id="${id}">取消后续处理</button>`:'')+(['failed','partial','blocked','cancelled'].includes(job.state)?`<button data-job="retry" data-id="${id}">重试未完成范围</button>`:'')+(['download','verify'].includes(job.kind)&&!['queued','running','retrying'].includes(job.state)?`<button data-verify="${id}">只读重新核验</button>`:'')+(job.kind==='verify'?`<button data-repair="${id}">预览缺口补数</button>`:'')+(['download','catalog'].includes(job.kind)&&!p.retry_of&&!p.repair_of&&!p.maintenance_id?`<button data-save-plan="${id}">保存为维护计划</button>`:'')+'</div>';if(job.deleted_at){body.querySelector('.toolbar').replaceChildren(recycleButton('jobs',job));body.prepend(create('p',{className:'query-state'},'此任务在回收站，当前只读；恢复后可继续操作。'));}icons();return;
    }
    const form=$(tab==='units'?'#task-unit-filter':'#task-event-filter'),query=tab==='links'?{}:!apply&&taskApplied.has(tab)?{...taskApplied.get(tab)}:values(form),limit=tab==='links'?50:Number(form.elements.limit.value);
    const params={...query,limit,offset,...(before?{before}:{})};let result;
    try{result=await api('/jobs/'+id+'/'+tab+'?'+new URLSearchParams(params));}catch(error){if(request!==detailRequest||route!=='task/'+id)return;if(!body.querySelector('#task-records'))body.textContent='查询失败：'+error.message;if(tab!=='links')queryFailed(form,error);throw error;}if(request!==detailRequest||route!=='task/'+id)return;
    if(tab==='links'){activeJob.links=result;body.innerHTML=renderJobLinks(activeJob);icons();return;}
    if(tab==='units'&&offset&&!result.rows.length&&result.total)return taskTab(tab,Math.floor((result.total-1)/limit)*limit);
    if(tab==='units'&&!result.total)offset=0;
    taskApplied.set(tab,{...query,limit:String(limit)});markQuery(form,JSON.stringify(taskApplied.get(tab))===JSON.stringify(values(form))?'已应用筛选':'筛选尚未应用，当前结果保持上次查询');
    $(tab==='units'?'#task-unit-count':'#task-event-count').textContent=tab==='units'?`共 ${result.total} 个分块 · ${result.rows.length?offset+1:0}–${result.rows.length?offset+result.rows.length:0}`:`本批 ${result.rows.length} 条事件`;
    const labels=tab==='units'?{unit_index:'分块 / 对象',state:'执行状态',quality_state:'数据质量',row_count:activeJob.kind==='verify'?'核验行数':'入库行数',error_code:'错误码',retryable:'可自动重试',updated_at:'记录时间'}:{created_at:'时间',level:'级别',code:'代码',message:'事件'};
    recordSets.task={title:tab==='units'?'分块详情':'事件详情',rows:result.rows,labels,extra:row=>'<h3>'+(tab==='units'?'核验依据与异常区间':'上下文')+'</h3><dl class="detail-grid">'+detailFields(tab==='units'?{请求范围:row.request,核验结果:row.issues}:row.context||{})+'</dl>'};
    body.innerHTML='<div class="table-wrap" id="task-records"><table><thead><tr>'+Object.values(labels).map(label=>'<th>'+label+'</th>').join('')+'<th>操作</th></tr></thead><tbody>'+result.rows.map((row,index)=>'<tr>'+Object.keys(labels).map(key=>`<td class="${key==='row_count'?'numeric':key==='unit_index'||key==='message'?'wrap-text':''}">${key==='unit_index'?'<strong>第 '+(row.unit_index+1)+' 块</strong><span class="muted">'+escape(row.request.code||row.request.exchange||'未提供对象')+' · '+escape(periodNames[row.request.period]||reportNames[row.request.period]||row.request.period||'')+'</span><span class="muted">'+escape(row.request.start||'')+' 至 '+escape(row.request.end||'')+'</span>':escape(key==='state'?statuses[row[key]]:key==='quality_state'?qualityNames[row[key]]:key==='level'?levelNames[row[key]]:key==='created_at'||key==='updated_at'?formatTime(row[key]):key==='retryable'?row[key]===true?'是':row[key]===false?'否':'未提供':number(row[key]))}</td>`).join('')+'<td>'+recordButton('task',index)+(tab==='units'&&['failed','partial','blocked','cancelled'].includes(activeJob.state)&&(row.state!=='succeeded'||row.retryable)?`<button data-retry-unit="${row.unit_index}" data-unit-job="${id}">重试此分块</button>`:'')+(tab==='units'&&activeJob.kind==='verify'&&row.quality_state==='missing'?`<button data-repair="${id}" data-repair-unit="${row.unit_index}">预览补数</button>`:'')+'</td></tr>').join('')+(!result.rows.length?'<tr><td class="empty" colspan="'+(Object.keys(labels).length+1)+'">没有匹配的'+(tab==='units'?'分块':'事件')+'</td></tr>':'')+'</tbody></table></div>';
    if(activeJob.deleted_at)body.querySelectorAll('[data-retry-unit],[data-repair]').forEach(button=>button.remove());
    const pager=create('div',{className:'pagination'});if(tab==='units'){const previous=command('上一页','chevron-left',()=>taskTab(tab,Math.max(0,offset-limit))),next=command('下一页','chevron-right',()=>taskTab(tab,result.next_offset));previous.disabled=!offset;next.disabled=result.next_offset===null;pager.append(previous,next);}else{if(before)pager.append(command('返回首批','chevrons-left',()=>taskTab(tab)));if(result.next_before)pager.append(command('下一批','chevron-right',()=>taskTab(tab,0,result.next_before)));}body.append(pager);icons();
  }
  function jobsLoaded(){
    const rows=$$('#job-rows tr');for(let index=0;index<rows.length;index++){const job=state.jobs[index];if(!job)continue;const actions=rows[index].querySelector('.actions');if(job.deleted_at){actions.querySelectorAll('button:not([data-job-detail]),a').forEach(el=>el.remove());rows[index].children[4].insertAdjacentHTML('beforeend','<span class="muted">回收站</span>');}if(!actions.querySelector('[data-recycle]')){const button=recycleButton('jobs',job);if(['queued','running','retrying'].includes(job.state)){button.disabled=true;button.title='活动任务请先取消或等待结束';}actions.append(button);}const cell=rows[index].firstElementChild;if(!cell.querySelector('[data-select-job]'))cell.prepend(create('input',{type:'checkbox',ariaLabel:'选择任务 '+job.id,'value':job.id}));const check=cell.querySelector('input');check.dataset.selectJob=job.id;check.checked=selectedJobs.has(job.id);if(check.checked)selectedJobs.set(job.id,job);check.onchange=()=>{check.checked?selectedJobs.set(job.id,job):selectedJobs.delete(job.id);$('#job-selection-count').textContent='已选 '+selectedJobs.size+' 个';};}
    if(!$('#job-batch')){const toolbar=create('div',{id:'job-batch',className:'result-tools'});toolbar.append(create('span',{id:'job-selection-count'}),command('选择全部匹配','list-checks',async()=>{const result=await api('/jobs/query',{...listQuery('job-filter'),select_all:true});result.rows.forEach(job=>selectedJobs.set(job.id,job));await loadJobs();}),command('选择本页','check-check',()=>$$('[data-select-job]').forEach(el=>{el.checked=true;el.onchange();})),command('清空选择','x',()=>{selectedJobs.clear();$$('[data-select-job]').forEach(el=>el.checked=false);$('#job-selection-count').textContent='已选 0 个';}),command('取消所选','square',()=>batchJobs('cancel')),command('重试所选','rotate-cw',()=>batchJobs('retry')));$('#job-filter').before(toolbar);}$('#job-selection-count').textContent='已选 '+selectedJobs.size+' 个';icons();
  }
  async function batchJobs(operation){const selected=[...selectedJobs.values()],allowed=selected.filter(job=>!job.deleted_at&&(operation==='cancel'?['queued','running','retrying']:['partial','failed','blocked','cancelled']).includes(job.state));if(!allowed.length)throw new Error('所选任务没有允许此操作的项目');if(!await confirmBatch('确认批量'+(operation==='cancel'?'取消':'重试'),{可执行:allowed.length,不适用:selected.length-allowed.length}))return;const results=[];for(const job of allowed){try{await api('/jobs/'+job.id+'/'+operation,{});results.push(job.id.slice(0,8)+'：已提交');}catch(error){results.push(job.id.slice(0,8)+'：'+error.message);}}await loadJobs();const report=addPanel('batch-results','批量操作结果');report.append(create('div',{className:'table-wrap'},'<table><thead><tr><th>任务 / 处理结果</th></tr></thead><tbody>'+results.map(text=>'<tr><td>'+escape(text)+'</td></tr>').join('')+'</tbody></table>'));report.addEventListener('close',()=>report.remove());report.showModal();icons();}
  function bind(){
    document.addEventListener('change',event=>{
      const input=event.target,id=input.dataset.schedule;if(!id)return;event.stopImmediatePropagation();
      const row=state.datasets.find(item=>item.id===id),buttons=[...input.closest('tr').querySelectorAll('.actions button')],disabled=buttons.map(button=>button.disabled);
      input.disabled=true;buttons.forEach(button=>button.disabled=true);
      action(async()=>{
        try{const saved=await api('/console/datasets/'+id,{revision:row.revision,scheduled:input.checked});Object.assign(row,saved);discardPendingList('datasets-filter');}
        catch(error){input.checked=!input.checked;throw error;}
        finally{input.disabled=false;buttons.forEach((button,index)=>button.disabled=disabled[index]);}
        await loadDatasets();enhanceDatasets();
      })();
    },true);
    window.addEventListener('hashchange',()=>navigate(location.hash.slice(1),true));window.addEventListener('resize',()=>charts.forEach(instance=>instance.resize()));
    document.addEventListener('click',event=>{
      const button=event.target.closest('button');if(!button)return;const d=button.dataset;
      if(button.id==='operations-refresh'){event.stopImmediatePropagation();action(async()=>{await loadOperations('overview');renderWorkerStatus(await api('/status'));await operationCharts();})();}
      if(button.id==='worker-alert-logs'){event.stopImmediatePropagation();navigate('runtime');}
      if(d.jobDetail){event.stopImmediatePropagation();event.preventDefault();navigate('task/'+d.jobDetail).then(()=>{if(d.linksOffset!==undefined)return taskTab('links',Number(d.linksOffset));}).catch(error=>notice(error.message));}
      if(d.units){event.stopImmediatePropagation();navigate('task/'+d.units).then(()=>taskTab('units'));}
      if(d.events){event.stopImmediatePropagation();navigate('task/'+d.events).then(()=>taskTab('events'));}
      if(d.download){event.stopImmediatePropagation();navigate('collect').then(()=>{collectMode('history');$('#download-form [name=dataset_id]').value=d.download;refreshMulti($('#download-form [name=dataset_id]'));updateDownloadPeriods();});}
      if(d.plan){event.stopImmediatePropagation();action(async()=>{const plan=plans.find(row=>row.id===d.id);if(d.plan==='edit'||d.plan==='copy')await editMaintenance(d.id,d.plan==='copy');if(d.plan==='toggle'){await api('/console/maintenance/'+d.id,{revision:plan.revision,enabled:!plan.enabled});discardPendingList('maintenance-filter');await renderMaintenance();}if(d.plan==='run')await runPlan(d.id);})();}
      if(d.savePlan||d.maintain){event.stopImmediatePropagation();action(async()=>{const job=await api('/jobs/'+(d.savePlan||d.maintain));await editMaintenance(null,false,null,job);$('#maintenance-form [name=name]').value='维护 '+(job.payload.resource?reportNames[job.payload.resource]:job.id.slice(0,8));setDirty($('#maintenance-form'));})();}
      if(button.id==='account-new'||d.accountEdit){setTimeout(()=>{$('#account-form').dataset.configRevision=configRevision;openPanel('account-editor',$('#account-form'));},0);}
      if(d.indexDataset!==undefined||d.boardDataset!==undefined){event.stopImmediatePropagation();action(async()=>{const row=d.indexDataset!==undefined?state.indices[Number(d.indexDataset)]:await api('/boards/members?'+new URLSearchParams({name:boardRows[Number(d.boardDataset)].name}));await navigate('datasets');await editDataset();state.snapshot=d.indexDataset!==undefined?row:{...row,board:true};$('#dataset-form [name=name]').value=row.name+'成员';$('#dataset-form [name=members]').value=row.members.join(',');await resolveSelections();})();}
      if('closeDialog' in d&&button.closest('dialog')?.contains(dirtyForm)&&JSON.stringify(formState(dirtyForm,true))!==dirtyBaseline){event.stopImmediatePropagation();action(async()=>{if(await confirmBatch('放弃未保存的修改？',{操作:'关闭编辑面板'})){dirtyForm.reset();clearDirty();button.closest('dialog').close();}})();}
    },true);
    document.addEventListener('cancel',event=>{if(event.target.contains(dirtyForm)&&JSON.stringify(formState(dirtyForm,true))!==dirtyBaseline){event.preventDefault();action(async()=>{if(await confirmBatch('放弃未保存的修改？',{操作:'关闭编辑面板'})){clearDirty();event.target.close();}})();}},true);
    window.addEventListener('beforeunload',event=>{if(dirtyForm&&JSON.stringify(formState(dirtyForm,true))!==dirtyBaseline){event.preventDefault();event.returnValue='';}});
    $('#dataset-form').addEventListener('invalid',event=>event.target.setAttribute('aria-invalid','true'),true);
    intercept($('#download-form'),async()=>{const form=$('#download-form'),p=values(form),ids=selectedValues(form.elements.dataset_id),periods=new FormData(form).getAll('period');if(!ids.length||!periods.length)throw new Error('请选择数据集及周期');if(!await confirmBatch('确认历史回补',{来源:provider,数据集:ids.map(id=>state.datasets.find(d=>d.id===id)?.name||id).join('、'),数据集数:ids.length,周期:periods.map(p=>periodNames[p]).join('、'),账号:ids.map(id=>state.datasets.find(d=>d.id===id)?.account_id||'QMT').join('、'),开始:p.start||'默认回补范围',结束:p.end||'今天'}))return;await api(ids.length>1?'/downloads/batch':'/downloads',{source:provider,dataset_ids:ids,dataset_id:ids[0],periods,start:p.start||null,end:p.end||null});await navigate('jobs');notice('回补任务已排队');});
  }
  function collectMode(key){for(const item of ['catalog','history','report'])$('#collect-'+item+'-panel').hidden=item!==key;$$('[data-collect-mode]').forEach(el=>{el.classList.toggle('active',el.dataset.collectMode===key);el.setAttribute('aria-selected',el.dataset.collectMode===key);});}
  function freshnessLoaded(page){const strip=$('#freshness-metrics');strip.innerHTML=Object.entries(page.summary||{}).map(([key,count])=>`<button type="button" data-freshness="${key}"><span>${freshnessNames[key]}</span><strong>${count}</strong></button>`).join('');strip.onclick=event=>{const button=event.target.closest('[data-freshness]');if(button){$('#freshness-filter [name=states]').value=button.dataset.freshness;refreshMulti($('#freshness-filter [name=states]'));loadFreshness(0,true).catch(error=>notice(error.message));}};}
  function catalogLoaded(rows){
    const table=$('#securities');table.querySelectorAll('tr').forEach((tr,index)=>{const row=rows[index];if(!row)return;const check=create('input',{type:'checkbox',checked:selectedCatalog.has(row.code),ariaLabel:'选择 '+row.name});check.dataset.catalogCode=row.code;tr.firstElementChild.prepend(check);check.onchange=()=>{check.checked?selectedCatalog.add(row.code):selectedCatalog.delete(row.code);$('#catalog-selection-count').textContent='已选 '+selectedCatalog.size+' 个';};});
    if(!$('#catalog-actions')){const bar=create('div',{id:'catalog-actions',className:'result-tools'});bar.append(create('span',{id:'catalog-selection-count'}),command('选择本页','check-check',()=>{table.querySelectorAll('[data-catalog-code]').forEach(el=>{el.checked=true;el.onchange();});}),command('选择全部匹配','list-checks',async()=>{const result=await api('/catalog/select',listQuery('security-form'));result.rows.forEach(row=>selectedCatalog.add(row.code));await loadSecurities(securityOffset);}),command('清空选择','x',async()=>{selectedCatalog.clear();await loadSecurities(securityOffset);}),command('以所选新建数据集','folder-plus',async()=>{if(!selectedCatalog.size)throw new Error('请选择证券或合约');await navigate('datasets');await editDataset();$('#dataset-form [name=members]').value=[...selectedCatalog].join(',');await resolveSelections();}));$('#security-form').before(bar);}
    $('#catalog-selection-count').textContent='已选 '+selectedCatalog.size+' 个';
  }
  const managementOffsets={datasets:0,maintenance:0};
  function setupManagementLists(){for(const [resource,id] of [['datasets','dataset-page'],['maintenance','maintenance']]){
    const form=create('form',{id:resource+'-filter',className:'toolbar'},'<label class="grow">名称<input name="search" type="search"></label>'+ (resource==='maintenance'?'<label>来源<select name="sources"><option value="">全部</option><option value="qmt">QMT</option><option value="tushare">Tushare</option></select></label>':'')+'<label>自动更新<select name="enabled"><option value="">全部</option><option value="true">已启用</option><option value="false">已停用</option></select></label><label>排序<select name="sort"><option value="name">名称</option><option value="schedule_time">更新时间</option></select></label><label>方向<select name="direction"><option value="asc">升序</option><option value="desc">降序</option></select></label><label>每页<select name="limit"><option>50</option><option>100</option><option>200</option></select></label><button type="submit">查询</button>');
    form.querySelector('[type=submit]').before(trashChoice());$('#'+id+' .section-heading').after(form);form.onsubmit=action(async()=>{managementOffsets[resource]=0;if(resource==='datasets'){await loadDatasets(true);enhanceDatasets();}else await renderMaintenance(true);});form.addEventListener('input',()=>markQuery(form,'筛选尚未应用，点击查询应用'));
    const pager=create('div',{id:resource+'-pager',className:'pagination'});$('#'+id).append(pager);
  }}
  async function configurationRows(resource,apply=false){const source=provider,form=$('#'+resource+'-filter'),params=listQuery(form.id,apply),offset=managementOffsets[resource],limit=Number(params.limit);const result=await readList(form.id,apply,async query=>{const page=await api('/'+resource+'/query',{...query,offset});if(resource==='datasets')page.active_rows=await api('/datasets');return page;});if(!result||source!==provider)return null;if(offset&&!result.rows.length&&result.total){managementOffsets[resource]=Math.floor((result.total-1)/limit)*limit;return configurationRows(resource,false);}if(!result.total)managementOffsets[resource]=0;const pager=$('#'+resource+'-pager');pager.replaceChildren(create('span',{},`共 ${result.total} 项 · ${result.rows.length?offset+1:0}–${result.rows.length?offset+result.rows.length:0}`));for(const [label,next] of [['上一页',offset?Math.max(0,offset-limit):null],['下一页',result.next_offset]]){const button=command(label,label==='上一页'?'chevron-left':'chevron-right',async()=>{managementOffsets[resource]=next;if(resource==='datasets'){await loadDatasets();enhanceDatasets();}else await renderMaintenance();});button.disabled=next===null;pager.append(button);}return resource==='datasets'?result:result.rows;}
  function timelineRows(rows,period){const span={'1m':60000,'5m':300000,'15m':900000,'30m':1800000,'60m':3600000,'1d':86400000,'1w':604800000,'1mo':2678400000}[period];const display=[];for(const row of rows){const previous=display.at(-1);if(previous&&Date.parse(row.time)-Date.parse(previous.time)>span)display.push({time:new Date((Date.parse(row.time)+Date.parse(previous.time))/2).toISOString(),gap:true});display.push(row);}return display;}
  const initial=location.hash.slice(1)||'catalog';splitPages();bind();icons();
  try{const saved=localStorage.getItem('pfor:source');if(['qmt','tushare'].includes(saved))$('#data-source').value=saved;}catch{}
  queueMicrotask(()=>($('#data-source').value===provider?navigate(initial,true):changeSource()).catch(error=>notice(error.message)));
  return {updateReportDimensions,reportDimensions,discardPendingList,filterLocalRows,navigate,pageSize,pageOptions,listQuery,listLoaded,readList,queryFailed,history:historyLoad,chart:loadChartWindow,futures:futuresLoad,exportHistory,exportFutures,invalidate,jobsLoaded,enhanceDatasets,configLoaded,sourceChanged,rememberSource,allowSourceChange,freshnessLoaded,catalogLoaded,datasetRows:apply=>configurationRows('datasets',apply)};
})();
