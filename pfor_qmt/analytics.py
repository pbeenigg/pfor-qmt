"""Read-only console queries, sharing filters with paged records."""
import hashlib
import json
from datetime import datetime

from .data import codes, periods, day, period_label, timestamp, SHANGHAI, json_default, filter_values
from .identifiers import provider_name
from .reliability import STATES


def ordering(params, allowed, default, tie):
    field = params.get('sort') or default
    direction = str(params.get('direction', 'asc')).lower()
    if field not in allowed or direction not in ('asc', 'desc'):
        raise ValueError('不支持的排序字段或方向')
    return field + ' ' + direction + ' NULLS LAST' + ''.join(', '+key+' ASC' for key in tie if key != field)


def history_filter(params):
    if not isinstance(params,dict): raise ValueError('查询范围必须是对象')
    source = provider_name(params.get('source', 'qmt'))
    members = codes(params.get('members') or [params['code']], source)
    selected = periods(params.get('periods') or [params.get('period', '1d')], source)
    start, end = day(params.get('start') or '1990-01-01'), day(params.get('end') or '2100-01-01')
    if start > end:
        raise ValueError('开始日期不得晚于结束日期')
    parts, args = [], [source, members]
    for period in selected:
        parts.append('(period=%s AND coalesce(trading_day,time::date) BETWEEN %s AND %s)')
        args.extend([period, period_label(start, period), period_label(end, period)])
    return 'source=%s AND code=ANY(%s) AND ('+' OR '.join(parts)+')', args


def snapshot(conn, table, where, args, params):
    # xmin also covers revisions to calendar/mapping rows without updated_at.
    version_column='row_version' if table=='current_contract_mappings' else 'xmin'
    version = conn.execute('SELECT count(*) AS total,max('+version_column+'::text::bigint) AS latest,sum('+version_column+'::text::bigint) AS revisions FROM '+table+' WHERE '+where, args).fetchone()
    key = hashlib.sha256(json.dumps([table, where, args, version], default=json_default, sort_keys=True).encode()).hexdigest()
    if params.get('snapshot') and params['snapshot'] != key:
        raise ValueError('查询范围内的数据已更新，请重新查询后继续')
    return dict(total=version['total'], snapshot=key, evaluated_at=datetime.now(SHANGHAI))


def history_page(store, params):
    where, args = history_filter(params)
    limit, offset = int(params.get('limit', 50)), int(params.get('offset', 0))
    if not 1 <= limit <= 5000 or offset < 0:
        raise ValueError('无效历史查询分页')
    order = ordering(params, ('code','period','time','open','high','low','close','volume','amount','open_interest'), 'time', ('code','period','time'))
    with store.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        meta = snapshot(conn, 'bars', where, args, params)
        rows = conn.execute('SELECT code,period,time,open,high,low,close,volume,amount,open_interest,settlement,previous_settlement,trading_day,source,as_of_date,source_fields,normalization_version FROM bars WHERE '+where+' ORDER BY '+order+' LIMIT %s OFFSET %s', (*args,limit,offset)).fetchall()
    source = params.get('source', 'qmt')
    return dict(meta, rows=rows, offset=offset, limit=limit, next_offset=offset+len(rows) if offset+len(rows)<meta['total'] else None,
                source='postgresql',provider=source,timezone='Asia/Shanghai',adjustment='none',truncated=False,
                date_basis='日线按交易日；分钟保留上海时间与未知夜盘归属；周/月按周期标签',
                units={'price':'合约报价单位','volume':'手','amount':'元','open_interest':'手'} if source=='tushare' else
                {'price':'QMT 原始报价','volume':'QMT 原始单位（未核验）','amount':'QMT 原始单位（未核验）','open_interest':'QMT 原始单位（未核验）'})


def history_summary(store, params):
    from .futures import instrument_names
    where, args = history_filter(params)
    with store.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        meta = snapshot(conn, 'bars', where, args, params)
        rows = conn.execute('SELECT source,code,period,count(*) AS count,min(time) AS first_time,max(time) AS last_time,max(high) AS high,min(low) AS low,sum(volume) AS volume,sum(amount) AS amount,(array_agg(open_interest ORDER BY time DESC))[1] AS latest_open_interest,max(updated_at) AS last_write FROM bars WHERE '+where+' GROUP BY source,code,period ORDER BY code,period',args).fetchall()
        names=instrument_names(conn,rows,params.get('source','qmt'))
    return dict(meta,groups=rows,instrument_names=names,truncated=False,scope='完整筛选范围，分别按来源、合约和周期统计；持仓取末条，空值保留',completeness=None)


def history_chart(store, params):
    where, args = history_filter(params)
    if len(args[1]) != 1 or len(args) != 5:
        raise ValueError('每幅K线图请选择一个合约和一个周期')
    limit = int(params.get('limit',1000))
    if not 1 <= limit <= 5000 or params.get('before') and params.get('after'):
        raise ValueError('图表窗口需要1至5000条，前后游标不能同时使用')
    cursor = params.get('before') or params.get('after')
    window = where + (' AND time '+('>' if params.get('after') else '<')+' %s' if cursor else '')
    window_args = [*args, timestamp(cursor)] if cursor else args
    with store.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if params.get('query_scope'):
            scope_where,scope_args=history_filter(params['query_scope'])
            ranges=[scope_args[index:index+3] for index in range(2,len(scope_args),3)]
            if args[0]!=scope_args[0] or args[1][0] not in scope_args[1] or args[2:] not in ranges:
                raise ValueError('图表不属于已应用的查询范围')
            snapshot(conn,'bars',scope_where,scope_args,params['query_scope'])
        meta = snapshot(conn,'bars',where,args,params)
        order = 'ASC' if params.get('after') else 'DESC'
        rows = conn.execute('SELECT code,period,time,trading_day,as_of_date,open,high,low,close,volume,amount,open_interest,source,updated_at FROM bars WHERE '+window+' ORDER BY time '+order+' LIMIT %s',(*window_args,limit)).fetchall()
        rows.sort(key=lambda row:row['time'])
        first, last = (rows[0]['time'],rows[-1]['time']) if rows else (None,None)
        bounds = conn.execute('SELECT min(time) AS first,max(time) AS last FROM bars WHERE '+where,args).fetchone()
    return dict(meta,rows=rows,limit=limit,actual_start=first,actual_end=last,
                has_previous=bool(first and bounds['first']<first),has_next=bool(last and bounds['last']>last),
                truncated=len(rows)<meta['total'],scope='原始K线时间窗，不合成、不补零',timezone='Asia/Shanghai')


JOB_ORIGIN_SQL = "CASE WHEN payload ? 'repair_of' THEN 'repair' WHEN parent_id IS NOT NULL OR payload ? 'retry_of' THEN 'retry' WHEN kind='verify' THEN 'verify' WHEN schedule_key IS NOT NULL OR payload ? 'maintenance_id' THEN 'scheduled' WHEN payload ? 'manual_maintenance_id' THEN 'maintenance' ELSE 'manual' END"


def trash_filter(params):
    value=params.get('trash','active')
    if value not in ('active','deleted','all'): raise ValueError('无效回收站筛选')
    return {'active':'deleted_at IS NULL','deleted':'deleted_at IS NOT NULL','all':'true'}[value]


def jobs_filter(params):
    states = filter_values(params.get('states',[]),(*STATES,'completed'),'任务状态')
    states = ['succeeded' if state=='completed' else state for state in states]
    kinds = filter_values(params.get('kinds',[]),('download','export','catalog','verify'),'任务类型')
    sources = filter_values(params.get('sources',[]),('qmt','tushare'),'任务来源')
    start,end = day(params.get('start') or '1990-01-01'),day(params.get('end') or '2100-01-01')
    if start>end: raise ValueError('开始日期不得晚于结束日期')
    search = '%'+str(params.get('search',''))+'%'
    origins = filter_values(params.get('origins',[]),('manual','scheduled','maintenance','retry','repair','verify'),'任务触发方式')
    accounts = params.get('account_ids') or []
    if isinstance(accounts,str): accounts=[item for item in accounts.split(',') if item]
    if not isinstance(accounts,list) or any(not isinstance(item,str) or len(item)>64 for item in accounts):
        raise ValueError('无效采集账号筛选')
    return trash_filter(params)+" AND (%s OR state=ANY(%s)) AND (%s OR kind=ANY(%s)) AND (%s OR coalesce(payload->>'source','qmt')=ANY(%s)) AND created_at::date BETWEEN %s AND %s AND (%s OR payload->>'account_id'=ANY(%s)) AND (%s OR ("+JOB_ORIGIN_SQL+")=ANY(%s)) AND (id::text ILIKE %s OR coalesce(error,'') ILIKE %s OR payload->>'account_id' ILIKE %s OR payload->>'members' ILIKE %s OR EXISTS(SELECT 1 FROM datasets d WHERE d.id::text=jobs.payload->>'dataset_id' AND d.name ILIKE %s) OR EXISTS(SELECT 1 FROM maintenance_plans m WHERE m.id::text=coalesce(jobs.payload->>'maintenance_id',jobs.payload->>'manual_maintenance_id') AND m.name ILIKE %s))", (not states,states,not kinds,kinds,not sources,sources,start,end,not accounts,accounts,not origins,origins,*([search]*6))


def operations_summary(store, params):
    where,args = jobs_filter(params)
    with store.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        meta = snapshot(conn,'jobs',where,args,params)
        counts = conn.execute("SELECT coalesce(payload->>'source','qmt') AS source,state,count(*) AS count FROM jobs WHERE "+where+' GROUP BY 1,2 ORDER BY 1,2',args).fetchall()
        trend = conn.execute('SELECT created_at::date AS day,state,count(*) AS count FROM jobs WHERE '+where+' GROUP BY 1,2 ORDER BY 1,2',args).fetchall()
        queue = conn.execute("SELECT state,count(*) AS count,min(created_at) AS oldest FROM jobs WHERE ("+where+") AND state IN ('queued','running','retrying') GROUP BY state",args).fetchall()
    return dict(meta,counts=counts,trend=trend,queues=queue,truncated=False,scope='按上海创建日期统计任务当前状态；每次关联重试是独立任务，不表示行情完整率')


def futures_summary(store, params):
    from .futures import report_filter, definition_for, selections, instrument_names
    resource,where,args = report_filter(params)
    selected=selections(params)[0]
    definition = definition_for(selected)
    table,date = definition['table'],definition['date']
    with store.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        meta = snapshot(conn,table,where,args,params)
        bounds = conn.execute('SELECT min('+date+') AS first_date,max('+date+') AS last_date FROM '+table+' WHERE '+where,args).fetchone()
        series = conn.execute('SELECT '+','.join(definition['fields'])+' FROM '+table+' WHERE '+where+' ORDER BY '+date+' DESC,'+','.join(k for k in definition['fields'] if k not in ('source',date))+' LIMIT 5000',args).fetchall()
        series.reverse()
        names=instrument_names(conn,series,selected['source'])
    return dict(meta,**bounds,resource=resource,mapping_mode=selected.get('mapping_mode','history'),provider=selected['source'],rows=series,instrument_names=names,truncated=meta['total']>len(series),units=definition['units'],
                scope='总数为完整查询范围；图表使用明确标记的最多5000条原始记录，各维度分别展示，不混合汇总与明细')


def configuration_page(store, resource, params):
    table='datasets' if resource=='datasets' else 'maintenance_plans'
    enabled='scheduled' if resource=='datasets' else 'enabled'
    sources=filter_values(params.get('sources',[]),('qmt','tushare'),'来源')
    state=params.get('enabled','')
    if state not in ('','true','false'): raise ValueError('无效启用状态筛选')
    where=trash_filter(params)+' AND (%s OR source=ANY(%s)) AND name ILIKE %s AND (%s OR '+enabled+'=%s)'
    args=(not sources,sources,'%'+str(params.get('search',''))+'%',state=='',state=='true')
    order=ordering(params,('name','source','schedule_time','created_at',enabled),'name',('id',))
    limit,offset=int(params.get('limit',50)),int(params.get('offset',0))
    if not 1<=limit<=200 or offset<0: raise ValueError('无效配置分页')
    with store.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        total=conn.execute('SELECT count(*) AS n FROM '+table+' WHERE '+where,args).fetchone()['n']
        rows=conn.execute('SELECT * FROM '+table+' WHERE '+where+' ORDER BY '+order+' LIMIT %s OFFSET %s',(*args,limit,offset)).fetchall()
    return dict(rows=rows,total=total,offset=offset,next_offset=offset+len(rows) if offset+len(rows)<total else None)
