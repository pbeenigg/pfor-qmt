"""Explicit maintenance scopes, using the existing job queue and source binding."""
import re
import uuid
from datetime import datetime, timedelta

from .data import SHANGHAI, chunks, day, timestamp
from .futures import request_chunks
from .identifiers import TS_EXCHANGES, source_market
from .reliability import failure
from .storage import document


def save_plan(store, params):
    job = store.job(params['job_id'])
    if job['kind'] not in ('catalog','download') or job['payload'].get('retry_of'):
        raise ValueError('请选择原始目录或采集任务作为维护范围，不使用局部重试任务')
    name = str(params.get('name','')).strip()
    clock = params.get('schedule_time') or ('19:00' if job['payload'].get('source')=='tushare' else '17:00')
    lookback = params.get('lookback_days',5)
    if not 1 <= len(name) <= 100 or not isinstance(clock,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',clock) or type(lookback) is not int or not 1 <= lookback <= 365:
        raise ValueError('名称需1至100字，时间为HH:MM，回读交易日数为1至365')
    payload = {key:value for key,value in job['payload'].items() if key not in ('chunks','start','end','repair_attempt')}
    if payload.get('dataset_id'):
        dataset = store.query('SELECT scheduled FROM datasets WHERE id=%s',(payload['dataset_id'],),one=True)
        if dataset and dataset['scheduled']:
            raise ValueError('该数据集已开启自动更新；请勿重复建立维护计划')
    with store.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(store.schema+'.maintenance-create',))
        duplicate = conn.execute('SELECT id FROM maintenance_plans WHERE payload=%s AND enabled',(document(payload),)).fetchone()
        if duplicate:
            raise ValueError('相同范围已有启用的维护计划')
        return conn.execute('INSERT INTO maintenance_plans(id,name,source,kind,payload,schedule_time,lookback_days) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                            (str(uuid.uuid4()),name,payload.get('source','qmt'),job['kind'],document(payload),clock,lookback)).fetchone()


def tick(worker, now=None):
    now = now or datetime.now(SHANGHAI)
    store = worker.store
    for plan in store.query('SELECT * FROM maintenance_plans WHERE source=%s AND enabled ORDER BY id',(worker.provider,)):
        cutoff = now.date() if now.time() >= plan['schedule_time'] else now.date()-timedelta(days=1)
        if plan['last_date'] and plan['last_date'] >= cutoff:
            continue
        if not plan['last_date'] and now.time() < plan['schedule_time']:
            continue
        try:
            payload = dict(plan['payload'],maintenance_id=str(plan['id']))
            adapter = worker.tushare_source(payload) if worker.provider=='tushare' else None
            if plan['kind']=='download':
                if payload.get('resource'):
                    targets = payload.get('selections') or [payload]
                    representatives = {source_market(target['code'],'tushare') if target['resource']=='mapping' else TS_EXCHANGES[target['exchange']] for target in targets}
                else:
                    representatives = {source_market(code,worker.provider) for code in payload['members']}
                starts, dates = [], []
                for market in sorted(representatives):
                    lookback_start = cutoff - timedelta(days=max(60,plan['lookback_days']*3))
                    values = worker.calendar(market,lookback_start.strftime('%Y%m%d'),cutoff.strftime('%Y%m%d'),adapter=adapter)
                    trading = sorted({timestamp(value).date() for value in values if timestamp(value).date() <= cutoff})
                    if len(trading) < plan['lookback_days']:
                        raise ValueError(market+' 日历不足，无法确认维护范围')
                    dates.append(trading[-1])
                    starts.append(trading[-plan['lookback_days']])
                end = max(dates)
                if plan['last_date'] and end <= plan['last_date']:
                    continue
                start = min(starts)
                if plan['last_date']:
                    start = min(start,plan['last_date']+timedelta(days=1))
                payload.update(start=start.isoformat(),end=end.isoformat())
                payload['chunks'] = request_chunks(payload) if payload.get('resource') else chunks(payload)
            else:
                end = cutoff
            with store.connect() as conn:
                current = conn.execute('SELECT enabled,last_date FROM maintenance_plans WHERE id=%s FOR UPDATE',(plan['id'],)).fetchone()
                if not current['enabled'] or current['last_date'] and current['last_date'] >= end:
                    continue
                key = 'maintenance:'+str(plan['id'])+':'+end.isoformat()
                job = conn.execute('INSERT INTO jobs(id,kind,payload,schedule_key) VALUES(%s,%s,%s,%s) ON CONFLICT(schedule_key) DO NOTHING RETURNING id',
                                   (str(uuid.uuid4()),plan['kind'],document(payload),key)).fetchone()
                conn.execute('UPDATE maintenance_plans SET last_date=%s,last_error=NULL,updated_at=now() WHERE id=%s',(end,plan['id']))
                if job:
                    store.event(job['id'],'MAINTENANCE_QUEUED','维护范围已排队',context={'plan_id':str(plan['id']),'date':end},conn=conn)
        except InterruptedError:
            raise
        except Exception as error:
            detail = failure(error)
            store.query('UPDATE maintenance_plans SET last_error=%s,updated_at=now() WHERE id=%s',(detail['code']+'：'+detail['message'],plan['id']))
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
