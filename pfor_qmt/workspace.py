"""Versioned management actions using the existing dataset and job pipeline."""
import copy
import secrets
import time
import uuid
from datetime import datetime, timedelta

from .data import day, SHANGHAI, chunks, filter_values
from .storage import document, job_summary
from .maintenance import validate_plan, check_conflict, market_scopes
from .identifiers import provider_name, TS_EXCHANGES
from .symbols import KINDS
from .accounts import profile


def check_revision(old, params):
    if type(params.get('revision')) is not int or params['revision'] != old['revision']:
        raise ValueError('配置已变更或缺少版本，请重新读取后编辑')


def recycle(app, resource, identifier, params, restore=False):
    tables={'datasets':'datasets','maintenance':'maintenance_plans','jobs':'jobs'}
    if resource not in tables: raise ValueError('不支持回收此类记录')
    if set(params)-({'revision'} if resource!='jobs' else set()): raise ValueError('包含不支持的回收参数')
    store=app.store;table=tables[resource]
    with store.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(store.schema+'.maintenance-config',))
        old=conn.execute('SELECT * FROM '+table+' WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not old: raise ValueError('记录不存在')
        if resource!='jobs': check_revision(old,params)
        if bool(old['deleted_at'])==bool(restore):
            if resource=='jobs':
                if old['state'] in ('queued','running','retrying'): raise ValueError('活动任务不能删除，请先取消或等待结束')
            elif not restore:
                key='dataset_id' if resource=='datasets' else 'maintenance_id'
                active=conn.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running','retrying') AND (payload->>%s=%s OR (%s='maintenance_id' AND payload->>'manual_maintenance_id'=%s)) LIMIT 1",(key,identifier,key,identifier)).fetchone()
                if active: raise ValueError('仍有关联活动任务，请等待结束或先取消')
                if resource=='datasets':
                    references=conn.execute("SELECT name FROM maintenance_plans WHERE deleted_at IS NULL AND payload->>'dataset_id'=%s ORDER BY name LIMIT 5",(identifier,)).fetchall()
                    if references: raise ValueError('仍被维护计划引用，请先处理这些计划：'+'、'.join(row['name'] for row in references))
            if resource=='maintenance' and restore and old['payload'].get('dataset_id'):
                dataset=conn.execute('SELECT deleted_at FROM datasets WHERE id=%s FOR SHARE',(old['payload']['dataset_id'],)).fetchone()
                if dataset and dataset['deleted_at']: raise ValueError('关联数据集在回收站，请先恢复数据集')
            flag=',scheduled=false' if resource=='datasets' else ',enabled=false' if resource=='maintenance' else ''
            result=conn.execute('UPDATE '+table+' SET deleted_at=CASE WHEN %s THEN NULL ELSE now() END'+('' if resource=='datasets' else ',updated_at=now()')+flag+' WHERE id=%s RETURNING *',(restore,identifier)).fetchone()
            store.event(identifier if resource=='jobs' else None,'RECORD_RESTORED' if restore else 'RECORD_RECYCLED','已恢复记录，自动更新保持停用' if restore and resource!='jobs' else '已恢复记录' if restore else '已移入回收站，保留行情、导出文件及追溯记录',context={'source':old.get('source',old.get('payload',{}).get('source','qmt')),'resource':resource,'record_id':identifier},conn=conn)
            return job_summary(result) if resource=='jobs' else result
        return job_summary(old) if resource=='jobs' else old


def dataset(app, identifier, params):
    store=app.store
    allowed={'revision','name','members','periods','schedule_time','scheduled','account_id','rebind_account'}
    if set(params)-allowed: raise ValueError('数据集仅允许编辑名称、成员、周期、账号与调度')
    if 'rebind_account' in params and type(params['rebind_account']) is not bool: raise ValueError('重新绑定状态必须是布尔值')
    with store.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(store.schema+'.maintenance-config',))
        old=conn.execute('SELECT * FROM datasets WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not old: raise ValueError('数据集不存在')
        if old['deleted_at']: raise ValueError('数据集在回收站，请先恢复')
        check_revision(old,params)
        if (old['index_code'] or old.get('board_name')) and 'members' in params and params['members']!=old['members']:
            raise ValueError('快照数据集请显式刷新成员，或复制为手工数据集后修改')
        value=dict(old,**{key:value for key,value in params.items() if key not in ('revision','rebind_account')})
        value['schedule_time']=str(value['schedule_time'])[:5]
        if old['source']=='tushare':
            rebinding=value['account_id']!=old['account_id'] or params.get('rebind_account',False)
            if rebinding:
                account=app.settings.account(value['account_id'])
                value.update(account_id=account['id'],endpoint=account['endpoint'])
            if rebinding or value['periods']!=old['periods'] or value['members']!=old['members'] or value['scheduled'] and not old['scheduled']:
                validate_binding(app,value)
        value=store.dataset_values(value)
        if value['scheduled'] and conn.execute("SELECT 1 FROM maintenance_plans WHERE enabled AND payload->>'dataset_id'=%s",(identifier,)).fetchone():
            raise ValueError('此数据集已有启用维护计划，请先停用一种维护方式')
        fields=list(value)
        result=conn.execute('UPDATE datasets SET '+','.join(key+'=%s' for key in fields)+',schedule_from=CASE WHEN NOT scheduled AND %s THEN CURRENT_DATE ELSE schedule_from END WHERE id=%s RETURNING *',
                            (*(document(value[key]) if key in ('members','periods') else value[key] for key in fields),value['scheduled'],identifier)).fetchone()
        store.event(None,'DATASET_UPDATED','数据集配置已更新，原任务保持固定范围',context={'source':old['source'],'dataset_id':identifier,'revision':result['revision']},conn=conn)
        return result


def scope(app, params):
    store=app.store
    if params.get('job_id'):
        job=store.job(params['job_id'])
        if job.get('deleted_at'): raise ValueError('任务在回收站，请先恢复')
        if job['kind'] not in ('download','catalog') or any(job['payload'].get(key) for key in ('retry_of','repair_of','maintenance_id','manual_maintenance_id')):
            raise ValueError('请选择原始采集任务作为维护范围')
        kind,payload=job['kind'],copy.deepcopy(job['payload'])
    else:
        if not isinstance(params.get('payload'),dict): raise ValueError('维护范围必须是对象')
        kind,payload=params.get('kind','download'),copy.deepcopy(params['payload'])
    if payload.get('dimensions'): raise ValueError('明细筛选仅用于查询与导出，维护范围需按资料对象选择')
    source=provider_name(payload.get('source','qmt'))
    binding={key:payload[key] for key in ('account_id','endpoint') if payload.get(key)}
    new_binding=not binding.get('endpoint')
    if source=='tushare' and not binding.get('endpoint') and not (payload.get('dataset_id') and not payload.get('members')):
        account=app.settings.account(binding.get('account_id') or params.get('account_id'))
        binding={'account_id':account['id'],'endpoint':account['endpoint']}
    if source=='tushare' and binding.get('endpoint') and not binding.get('account_id'): raise ValueError('固定端点必须同时指定采集账号')
    if source=='tushare' and binding.get('endpoint'):
        profile({'id':binding['account_id'],'endpoint':binding['endpoint']},effective=False)
    payload['source']=source
    # A stored scope already owns its members, periods and account. Only a bare dataset ID resolves current settings.
    if payload.get('dataset_id') and not payload.get('members'):
        if kind!='download': raise ValueError('数据集维护必须使用行情采集任务')
        payload=app.prepare_download(dict(payload,start=payload.get('start'),end=payload.get('end')))
        binding={key:payload.get(key) for key in ('account_id','endpoint')}
    elif kind=='catalog':
        payload={'source':source,'kinds':filter_values(payload.get('kinds',['future']),['future'] if source=='tushare' else (*KINDS,'board'),'目录类别'),
                 **({'exchanges':filter_values(payload.get('exchanges',list(TS_EXCHANGES)),TS_EXCHANGES,'交易所')} if source=='tushare' else {})}
        if not payload['kinds']: raise ValueError('请选择目录范围')
        if source=='tushare' and not payload['exchanges']: raise ValueError('请选择至少一个交易所')
    elif payload.get('resource') or payload.get('selections'):
        if kind!='download': raise ValueError('期货资料维护必须使用采集任务')
        from .futures import selections
        base=dict(payload,start=payload.get('start') or datetime.now(SHANGHAI).date().isoformat(),end=payload.get('end') or datetime.now(SHANGHAI).date().isoformat())
        targets=selections(base)
        if len({(target['resource'],target.get('mapping_mode')) for target in targets})!=1: raise ValueError('每个维护计划只维护一种资料及映射模式')
        if new_binding:
            rows=store.query("SELECT code,market,subtype,upper(metadata->>'product') AS product FROM securities WHERE source=%s",(source,))
            catalog={'continuous':{row['code'] for row in rows if row['subtype']=='continuous'},
                     'products':{(row['market'],row['product']) for row in rows if row['product']},
                     'contracts':{(row['market'],row['code'].split('.')[0]) for row in rows if row['subtype']=='contract'}}
            for target in targets: app.prepare_report(target,base,False,catalog,{'id':binding['account_id'],'endpoint':binding['endpoint']} if source=='tushare' else None)
        payload=dict(targets[0],selections=targets) if 'selections' in payload else dict(targets[0])
        payload['source']=source
    elif payload.get('members') and payload.get('periods'):
        if kind!='download': raise ValueError('行情成员范围必须使用采集任务')
        checked=store.dataset_values(dict(payload,name='维护范围',schedule_time='17:00'))
        dataset_id=payload.get('dataset_id')
        payload={key:checked[key] for key in ('source','members','periods')}
        if dataset_id: payload['dataset_id']=dataset_id
    else:
        raise ValueError('请选择数据集、资料或目录维护范围')
    if payload.get('dataset_id'):
        linked=store.query('SELECT deleted_at FROM datasets WHERE id=%s',(payload['dataset_id'],),one=True)
        if linked and linked['deleted_at']: raise ValueError('关联数据集在回收站，请先恢复')
    if kind not in ('catalog','download'): raise ValueError('维护只支持目录或数据采集')
    if payload['source']=='tushare':
        payload.update(binding)
    for key in ('chunks','start','end','repair_attempt','schedule_market'):
        payload.pop(key,None)
    for target in payload.get('selections',[]):
        target.pop('start',None);target.pop('end',None)
    return kind,payload


def validate_binding(app, payload):
    if payload.get('source','qmt')!='tushare': return
    account=app.settings.account(payload.get('account_id'))
    if account['endpoint']!=payload.get('endpoint'):
        raise ValueError('账号端点已变更，请明确重新绑定采集范围后再执行')
    if payload.get('periods'): app.check_minutes(account['id'],payload['periods'])
    resources={target.get('resource') for target in payload.get('selections') or [payload]}
    capabilities=app.capabilities.get(account['id'],{}).get('capabilities',{})
    for resource in resources:
        if capabilities.get(resource,{}).get('state') in ('permission','authentication','unsupported'):
            raise ValueError('资料接口权限不可用，请先处理账号权限')


def save_maintenance(app, identifier, params):
    store=app.store
    if set(params)-{'revision','name','schedule_time','lookback_days','enabled','payload','job_id','kind','account_id'}:
        raise ValueError('包含不支持的维护配置字段')
    with store.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(store.schema+'.maintenance-config',))
        old=conn.execute('SELECT * FROM maintenance_plans WHERE id=%s FOR UPDATE',(identifier,)).fetchone() if identifier else None
        if identifier and not old: raise ValueError('维护计划不存在')
        if old and old['deleted_at']: raise ValueError('维护计划在回收站，请先恢复')
        if old: check_revision(old,params)
        changing=any(key in params for key in ('payload','job_id','kind'))
        if old and changing and old['enabled']: raise ValueError('请先停用维护计划，再预览并修改范围')
        kind,payload=scope(app,params) if not old or changing else (old['kind'],old['payload'])
        if old and payload['source']!=old['source']: raise ValueError('修改来源请新建或复制维护计划')
        name=str(params.get('name',old['name'] if old else '')).strip()
        clock=params.get('schedule_time') or (str(old['schedule_time'])[:5] if old else '19:00' if payload['source']=='tushare' else '17:00')
        lookback=params.get('lookback_days',old['lookback_days'] if old else 5)
        enabled=params.get('enabled',old['enabled'] if old else False)
        if type(enabled) is not bool: raise ValueError('启用状态必须是布尔值')
        if changing and old and enabled: raise ValueError('范围修改请先保存，再单独启用')
        validate_plan(name,clock,lookback)
        if enabled:
            validate_binding(app,payload)
            check_conflict(conn,payload,identifier)
        if old:
            result=conn.execute('UPDATE maintenance_plans SET name=%s,kind=%s,payload=%s,schedule_time=%s,lookback_days=%s,enabled=%s,last_date=CASE WHEN payload=%s THEN last_date ELSE NULL END,updated_at=now() WHERE id=%s RETURNING *',
                               (name,kind,document(payload),clock,lookback,enabled,document(payload),identifier)).fetchone()
        else:
            result=conn.execute('INSERT INTO maintenance_plans(id,name,source,kind,payload,schedule_time,lookback_days,enabled) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                               (str(uuid.uuid4()),name,payload['source'],kind,document(payload),clock,lookback,enabled)).fetchone()
        store.event(None,'MAINTENANCE_UPDATED' if old else 'MAINTENANCE_CREATED','维护配置已保存'+('并启用' if enabled else '，未启用'),context={'source':payload['source'],'plan_id':str(result['id']),'revision':result['revision']},conn=conn)
        return result


def preview(app, identifier, params):
    if set(params)-{'start','end'}: raise ValueError('执行预览仅接受开始和结束日期')
    if params.get('start'):
        if not params.get('end') or day(params['start'])>day(params['end']): raise ValueError('请指定有效的起止日期')
    elif params.get('end'):
        day(params['end'])
    old=app.store.query('SELECT * FROM maintenance_plans WHERE id=%s',(identifier,),one=True)
    if not old: raise ValueError('维护计划不存在')
    if old['deleted_at']: raise ValueError('维护计划在回收站，请先恢复')
    payload=dict(old['payload'])
    validate_binding(app,payload)
    prepared=[]
    if old['kind']=='catalog': prepared=[payload]
    else:
        from .futures import request_chunks
        for market,part in market_scopes(payload,payload['source']).items():
            end=day(params.get('end') or datetime.now(SHANGHAI).date().isoformat())
            from .qmt_references import natural_schedule,current_mapping
            if current_mapping(part): start=end=datetime.now(SHANGHAI).date()
            elif params.get('start'): start=day(params['start'])
            elif natural_schedule(part): start=end-timedelta(days=old['lookback_days']-1)
            else:
                dates=app.store.query('SELECT day FROM trading_dates WHERE source=%s AND market=%s AND is_open AND day<=%s ORDER BY day DESC LIMIT %s',(payload['source'],market,end,old['lookback_days']))
                if len(dates)<old['lookback_days']: raise ValueError('已保存交易日历不足，请同步日历或明确指定执行日期')
                start=dates[-1]['day']
            if start>end: raise ValueError('开始日期不得晚于结束日期')
            part=dict(part,start=start.isoformat(),end=end.isoformat())
            part['chunks']=request_chunks(part) if part.get('resource') else chunks(part)
            prepared.append(part)
    key=secrets.token_urlsafe(24)
    with app.lock:
        previews=getattr(app,'workspace_previews',{})
        app.workspace_previews={key:value for key,value in previews.items() if value['until']>time.monotonic()}
        app.workspace_previews[key]=dict(until=time.monotonic()+300,id=identifier,revision=old['revision'],kind=old['kind'],payloads=prepared)
    return dict(preview_key=key,revision=old['revision'],source=old['source'],account_id=payload.get('account_id'),endpoint=payload.get('endpoint'),
                ranges=[{k:v for k,v in item.items() if k!='chunks'} for item in prepared],chunks=sum(len(item.get('chunks',[])) for item in prepared),expires_in=300)


def run(app, identifier, params):
    with app.lock:
        item=getattr(app,'workspace_previews',{}).get(params.get('preview_key'))
        if not item or item['id']!=identifier or item['until']<time.monotonic(): raise ValueError('执行预览已过期，请重新预览')
        if item.get('jobs'): return {'jobs':item['jobs']}
        with app.store.connect() as conn:
            old=conn.execute('SELECT * FROM maintenance_plans WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
            if not old or old['revision']!=item['revision']: raise ValueError('维护配置已变更，请重新预览')
            payload=old['payload']
            validate_binding(app,payload)
            jobs=[]
            for p in item['payloads']:
                row=conn.execute('INSERT INTO jobs(id,kind,payload) VALUES(%s,%s,%s) RETURNING *',(str(uuid.uuid4()),item['kind'],document(dict(p,manual_maintenance_id=identifier)))).fetchone()
                jobs.append(job_summary(row))
                app.store.event(row['id'],'MANUAL_MAINTENANCE_QUEUED','维护执行一次已排队，不推进自动调度游标',context={'source':old['source'],'plan_id':identifier},conn=conn)
        item['jobs']=jobs
        return {'jobs':jobs}


def manage(app, path, params):
    parts=path.strip('/').split('/')
    if len(parts)==4 and parts[3] in ('delete','restore'):
        return recycle(app,parts[1],parts[2],params,parts[3]=='restore')
    if parts[1]=='datasets':
        if len(parts)!=3: raise LookupError('数据集更新接口不存在')
        return dataset(app,parts[2],params)
    if len(parts)==2: return save_maintenance(app,None,params)
    if len(parts)==3: return save_maintenance(app,parts[2],params)
    if parts[3]=='preview': return preview(app,parts[2],params)
    if parts[3]=='run': return run(app,parts[2],params)
    if parts[3]=='scope-preview':
        old=app.store.query('SELECT * FROM maintenance_plans WHERE id=%s',(parts[2],),one=True)
        if not old: raise ValueError('维护计划不存在')
        if old['deleted_at']: raise ValueError('维护计划在回收站，请先恢复')
        check_revision(old,params)
        kind,payload=scope(app,params)
        return {'before':old['payload'],'after':payload,'changed':old['payload']!=payload or old['kind']!=kind,'kind':kind,'revision':old['revision']}
    raise LookupError('管理接口不存在')
