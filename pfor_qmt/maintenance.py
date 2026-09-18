"""Explicit maintenance scopes, using the existing job queue and source binding."""
import re
import uuid
import hashlib
from datetime import datetime, timedelta

from .data import SHANGHAI, chunks, day, timestamp
from .futures import request_chunks
from .identifiers import TS_EXCHANGES, source_market
from .reliability import failure
from .storage import document, job_summary


def validate_plan(name, clock, lookback):
    if not isinstance(name,str) or not 1<=len(name.strip())<=100 or not isinstance(clock,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',clock) or type(lookback) is not int or not 1<=lookback<=365:
        raise ValueError('名称需1至100字，时间为HH:MM，回读交易日数为1至365')


def check_conflict(conn, payload, identifier=None):
    if payload.get('dataset_id'):
        dataset=conn.execute('SELECT scheduled FROM datasets WHERE id=%s',(payload['dataset_id'],)).fetchone()
        if dataset and dataset['scheduled']:
            raise ValueError('该数据集已开启自动更新，请先停用一种维护方式')
    duplicate=conn.execute('SELECT id FROM maintenance_plans WHERE enabled AND payload=%s AND (%s::uuid IS NULL OR id!=%s::uuid)',(document(payload),identifier,identifier)).fetchone()
    if duplicate:
        raise ValueError('相同范围已有启用的维护计划')


def save_plan(store, params):
    job = store.job(params['job_id'])
    if job['kind'] not in ('catalog','download') or job['payload'].get('retry_of') or job['payload'].get('maintenance_id'):
        raise ValueError('请选择原始目录或采集任务作为维护范围，不使用局部重试任务')
    name = str(params.get('name','')).strip()
    clock = params.get('schedule_time') or ('19:00' if job['payload'].get('source')=='tushare' else '17:00')
    lookback = params.get('lookback_days',5)
    validate_plan(name,clock,lookback)
    payload = {key:value for key,value in job['payload'].items() if key not in ('chunks','start','end','repair_attempt')}
    with store.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(store.schema+'.maintenance-config',))
        check_conflict(conn,payload)
        result=conn.execute('INSERT INTO maintenance_plans(id,name,source,kind,payload,schedule_time,lookback_days) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                            (str(uuid.uuid4()),name,payload.get('source','qmt'),job['kind'],document(payload),clock,lookback)).fetchone()
        store.event(None,'MAINTENANCE_CREATED','维护范围已创建并启用',context={'source':result['source'],'plan_id':str(result['id']),'name':name},conn=conn)
        return result


def update_plan(store, identifier, params):
    if not params or set(params)-{'enabled','name','schedule_time','lookback_days'}:
        raise ValueError('仅可修改维护名称、时间、回读天数与启用状态')
    if 'enabled' in params and type(params['enabled']) is not bool:
        raise ValueError('enabled必须是布尔值')
    with store.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(store.schema+'.maintenance-config',))
        old=conn.execute('SELECT * FROM maintenance_plans WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not old: raise ValueError('维护计划不存在')
        value=dict(old,**params)
        clock=params.get('schedule_time',old['schedule_time'].strftime('%H:%M'))
        validate_plan(value['name'],clock,value['lookback_days'])
        if value['enabled']: check_conflict(conn,old['payload'],identifier)
        if value['name'].strip()==old['name'] and clock==old['schedule_time'].strftime('%H:%M') and value['lookback_days']==old['lookback_days'] and value['enabled']==old['enabled']:
            return old
        result=conn.execute('UPDATE maintenance_plans SET name=%s,schedule_time=%s,lookback_days=%s,enabled=%s,updated_at=now() WHERE id=%s RETURNING *',
                            (value['name'].strip(),clock,value['lookback_days'],value['enabled'],identifier)).fetchone()
        store.event(None,'MAINTENANCE_UPDATED','维护设置已更新，已排队任务保持原范围',context={'source':old['source'],'plan_id':str(identifier),'changes':params},conn=conn)
        return result


def set_dataset_schedule(store, identifier, enabled, now=None):
    if type(enabled) is not bool: raise ValueError('enabled必须是布尔值')
    with store.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(store.schema+'.maintenance-config',))
        old=conn.execute('SELECT * FROM datasets WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not old: raise ValueError('数据集不存在')
        if enabled and conn.execute("SELECT 1 FROM maintenance_plans WHERE enabled AND payload->>'dataset_id'=%s LIMIT 1",(str(identifier),)).fetchone():
            raise ValueError('该数据集已有启用的维护计划，请先停用一种维护方式')
        if old['scheduled'] != enabled:
            start=(now or datetime.now(SHANGHAI)).date() if enabled else old['schedule_from']
            conn.execute('UPDATE datasets SET scheduled=%s,schedule_from=%s WHERE id=%s',(enabled,start,identifier))
            store.event(None,'DATASET_SCHEDULE_UPDATED','数据集自动更新已'+('启用' if enabled else '停用'),context={'source':old['source'],'dataset_id':str(identifier),'enabled':enabled},conn=conn)
    return {'scheduled':enabled}


def market_scopes(payload, provider):
    groups={}
    if payload.get('resource'):
        for target in payload.get('selections') or [payload]:
            market=source_market(target['code'],provider) if target['resource']=='mapping' else TS_EXCHANGES[target['exchange']]
            groups.setdefault(market,[]).append({key:target[key] for key in ('resource','exchange','symbol','code') if key in target})
        return {market:dict(payload,selections=selected) for market,selected in groups.items()}
    for code in payload['members']:
        groups.setdefault(source_market(code,provider),[]).append(code)
    return {market:dict(payload,members=members) for market,members in groups.items()}


def cursors(store, payload, field):
    rows=store.query("SELECT payload->>'end' AS day,payload->>'schedule_market' AS market,CASE WHEN payload ? 'schedule_market' THEN NULL ELSE payload END AS legacy FROM jobs WHERE kind='download' AND schedule_key IS NOT NULL AND payload->>%s=%s",(field,payload[field]))
    result={}
    for row in rows:
        if not row['day']: continue
        markets=[row['market']] if row['market'] else market_scopes(row['legacy'],payload.get('source','qmt'))
        for market in markets:
            result[market]=max(result.get(market,day(row['day'])),day(row['day']))
    return result


def schedule_event(worker, scope, kind, market, message='', code='CALENDAR_NOT_READY', action='检查该范围的交易日历和数据源设置'):
    key=(kind,str(scope['id']),market)
    previous=worker.schedule_errors.get(key)
    signature=(code,message)
    context={'source':worker.provider,'scope_kind':kind,'scope_id':str(scope['id']),'name':scope['name'],'market':market,'error_code':code}
    if message:
        worker.schedule_issues[key]=dict(source=worker.provider,lane='schedule',scope_kind=kind,scope_id=str(scope['id']),name=scope['name'],market=market,code=code,reason=message,action=action)
        if previous!=signature:
            worker.store.event(None,'SCHEDULE_BLOCKED',message,'warning',context=context)
        worker.schedule_errors[key]=signature
    elif previous:
        worker.store.event(None,'SCHEDULE_RECOVERED','该维护范围的交易日历已恢复',context=context)
        worker.schedule_errors.pop(key,None)
    if not message:
        worker.schedule_issues.pop(key,None)


def enqueue(worker, scope, kind, payload, market, end):
    if worker.stop.is_set(): raise InterruptedError()
    store=worker.store
    table='datasets' if kind=='dataset' else 'maintenance_plans'
    fields=('members','periods','source','account_id','endpoint','schedule_time','schedule_from') if kind=='dataset' else ('payload','schedule_time','lookback_days')
    with store.connect() as conn:
        current=conn.execute('SELECT * FROM '+table+' WHERE id=%s FOR UPDATE',(scope['id'],)).fetchone()
        if not current or not current['scheduled' if kind=='dataset' else 'enabled'] or any(current[key]!=scope[key] for key in fields):
            return
        key=kind+':'+str(scope['id'])+':'+market+':'+end.isoformat()
        job=conn.execute('INSERT INTO jobs(id,kind,payload,schedule_key) VALUES(%s,%s,%s,%s) ON CONFLICT(schedule_key) DO NOTHING RETURNING *',
                         (str(uuid.uuid4()),scope.get('kind','download'),document(payload),key)).fetchone()
        if kind=='maintenance':
            conn.execute('UPDATE maintenance_plans SET last_date=greatest(last_date,%s) WHERE id=%s',(end,scope['id']))
        if job:
            store.event(job['id'],'MAINTENANCE_QUEUED','维护范围已排队',context={'scope_kind':kind,'scope_id':str(scope['id']),'market':market,'date':end},conn=conn)
    if job:
        worker.publish({'event':'job','data':job_summary(job)})


def schedule_scope(worker, scope, kind, now):
    import psycopg
    if worker.stop.is_set(): raise InterruptedError()
    store=worker.store
    cutoff=now.date() if now.time()>=scope['schedule_time'] else now.date()-timedelta(days=1)
    if kind=='dataset' and scope['schedule_from']>cutoff: return
    if kind=='maintenance' and not scope['last_date'] and now.time()<scope['schedule_time']: return
    payload=dict(scope['payload'],maintenance_id=str(scope['id'])) if kind=='maintenance' else dict(dataset_id=str(scope['id']),**{key:scope[key] for key in ('source','members','periods','account_id','endpoint')})
    try:
        adapter=worker.tushare_source(payload,lambda: (_ for _ in ()).throw(InterruptedError()) if worker.stop.is_set() else None) if worker.provider=='tushare' else None
    except ValueError as error:
        message=failure(error)['message']
        schedule_event(worker,scope,kind,'account',message,'SOURCE_CONFIGURATION')
        worker.last_error='；'.join(filter(None,(worker.last_error,scope['name']+'：'+message)))
        if kind=='maintenance':
            store.query('UPDATE maintenance_plans SET last_error=%s WHERE id=%s',(message,scope['id']))
        return
    schedule_event(worker,scope,kind,'account')
    if scope.get('kind')=='catalog':
        if not scope['last_date'] or scope['last_date']<cutoff:
            enqueue(worker,scope,kind,payload,'catalog',cutoff)
        store.query('UPDATE maintenance_plans SET last_error=NULL WHERE id=%s',(scope['id'],))
        return
    last_dates=cursors(store,payload,'dataset_id' if kind=='dataset' else 'maintenance_id')
    errors=[]
    for market,part in market_scopes(payload,worker.provider).items():
        if worker.stop.is_set(): raise InterruptedError()
        last=last_dates.get(market)
        if last and last>=cutoff: continue
        lookback=scope.get('lookback_days',5)
        first=cutoff-timedelta(days=max(60,lookback*3))
        earliest=last+timedelta(days=1) if last else scope.get('schedule_from',cutoff)
        first=min(first,earliest)
        representative=market if part.get('resource') else part['members'][0]
        block_key=(payload.get('account_id'),market)
        fingerprint=(adapter.account['endpoint'],adapter.account['requests_per_minute'],hashlib.sha256(adapter.account['token'].encode()).hexdigest()) if adapter else None
        blocked=worker.calendar_blocks.get(block_key)
        message,code,action='', 'CALENDAR_NOT_READY','补齐该范围的交易日历后继续更新'
        try:
            if blocked and blocked[0]==fingerprint: raise blocked[1]
            values=worker.calendar(representative,first.strftime('%Y%m%d'),cutoff.strftime('%Y%m%d'),adapter=adapter)
            dates=sorted({timestamp(value).date() for value in values if first<=timestamp(value).date()<=cutoff})
            if not dates: message=market+' 交易日历为空，无法确认到期交易日'
            with store.connect() as conn:
                for date in dates:
                    conn.execute('INSERT INTO trading_dates(source,market,day) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(worker.provider,market,date))
        except (InterruptedError,psycopg.Error): raise
        except Exception as error:
            detail=failure(error);code=detail['code']
            message='交易日历请求失败，尝试使用已保存日期'
            action=detail['action']
            if adapter and getattr(error,'category',None) in ('permission','unsupported','authentication','rate_limit'):
                worker.calendar_blocks[block_key]=(fingerprint,error)
            dates=[row['day'] for row in store.query('SELECT day FROM trading_dates WHERE source=%s AND market=%s AND is_open AND day BETWEEN %s AND %s ORDER BY day',(worker.provider,market,first,cutoff))]
        applicable=[date for date in dates if date>=earliest]
        if applicable and len(dates)<lookback:
            message=market+' 最近'+('五' if lookback==5 else str(lookback))+'个交易日尚未就绪，调度等待补齐'
        schedule_event(worker,scope,kind,market,message,code,action)
        if message: errors.append(scope['name']+'：'+message)
        if not applicable or len(dates)<lookback: continue
        end=applicable[-1]
        start=min(dates[-lookback],applicable[0])
        part.update(start=start.isoformat(),end=end.isoformat(),schedule_market=market)
        part['chunks']=request_chunks(part) if part.get('resource') else chunks(part)
        enqueue(worker,scope,kind,part,market,end)
    if errors: worker.last_error='；'.join(filter(None,(worker.last_error,*errors)))
    if kind=='maintenance':
        store.query('UPDATE maintenance_plans SET last_error=%s WHERE id=%s',('；'.join(errors) or None,scope['id']))


def tick(worker, now=None):
    now=now or datetime.now(SHANGHAI)
    for plan in worker.store.query('SELECT * FROM maintenance_plans WHERE source=%s AND enabled ORDER BY id',(worker.provider,)):
        schedule_scope(worker,plan,'maintenance',now)
    repair(worker,now)


def repair(worker, now):
    store = worker.store
    # Repair only opted-in maintenance jobs, never old ad-hoc failures or permission denials.
    jobs = store.query("SELECT j.* FROM jobs j WHERE coalesce(payload->>'source','qmt')=%s AND parent_id IS NULL AND kind!='verify' AND state IN ('partial','failed','blocked') AND NOT cancel_requested AND ((payload ? 'maintenance_id' AND EXISTS(SELECT 1 FROM maintenance_plans p WHERE p.id::text=j.payload->>'maintenance_id' AND enabled)) OR EXISTS(SELECT 1 FROM datasets d WHERE d.id::text=j.payload->>'dataset_id' AND scheduled)) AND EXISTS(SELECT 1 FROM job_units u WHERE u.job_id=j.id AND retryable) ORDER BY updated_at",(worker.provider,))
    maximum = worker.options.get('repair_attempts',3)
    for job in jobs:
        if worker.stop.is_set():
            raise InterruptedError()
        if job.get('error_code') in ('SOURCE_AUTHENTICATION','SOURCE_CONFIGURATION','SOURCE_RATE_LIMIT'):
            continue
        family=store.query("WITH RECURSIVE family AS (SELECT * FROM jobs WHERE id=%s UNION ALL SELECT j.* FROM jobs j JOIN family f ON j.parent_id=f.id) SELECT state,payload,updated_at FROM family",(job['id'],))
        if any(row['state'] in ('queued','running','retrying') for row in family):
            continue
        if max(family,key=lambda row:row['updated_at'])['state']=='cancelled':
            continue
        attempt = max(int(row['payload'].get('repair_attempt',0)) for row in family)
        delay = (900,3600,86400)[min(attempt,2)]
        if attempt >= maximum or (now-max(row['updated_at'] for row in family)).total_seconds() < delay:
            continue
        try:
            store.retry_job(job['id'],automatic=True,now=now)
        except ValueError:
            continue
