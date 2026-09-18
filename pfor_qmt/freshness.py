"""Read-only freshness of explicitly maintained scopes; never infer trading sessions."""
from datetime import datetime, timedelta
from itertools import islice

from .data import SHANGHAI, MINUTE_PERIODS, AGGREGATE_PERIODS, filter_values, timestamp
from .identifiers import source_market, TS_EXCHANGES
from .quality_checks import aggregate_issues
from .futures import REPORTS


def targets(conn):
    scopes = conn.execute("SELECT id,name,source,schedule_time,schedule_from,jsonb_build_object('members',members,'periods',periods) AS payload,'dataset' AS scope_kind,'download' AS kind FROM datasets WHERE scheduled UNION ALL SELECT id,name,source,schedule_time,NULL,payload,'maintenance',kind FROM maintenance_plans WHERE enabled ORDER BY scope_kind,id").fetchall()
    for scope in scopes:
        payload = scope['payload']
        base = {key:scope[key] for key in ('name','source','schedule_time','schedule_from','scope_kind')}
        base['scope_id'] = str(scope['id'])
        if scope['kind'] == 'catalog':
            yield dict(base, resource='catalog', code='', period='catalog')
        elif payload.get('resource'):
            for target in payload.get('selections') or [payload]:
                resource = target['resource']
                yield dict(base, resource=resource, code=target.get('code') or target['exchange']+':'+target.get('symbol',''),
                           period=resource, exchange=target.get('exchange',''), symbol=target.get('symbol',''))
        else:
            for code in payload['members']:
                for period in payload['periods']:
                    yield dict(base, resource='bars', code=code, period=period)


def assess(conn, target, now, calendars):
    result = dict(target, schedule_time=target['schedule_time'].isoformat(), evaluated_at=now, expected_day=None, actual_day=None, last_write=None)
    def finish(state, code, reason, action):
        return dict(result, freshness_state=state, reason_code=code, reason=reason, action=action)

    source, resource, code, period = (target[key] for key in ('source','resource','code','period'))
    cutoff = now.date() if now.time() >= target['schedule_time'] else now.date()-timedelta(days=1)
    result['cutoff'] = cutoff
    if target['schedule_from'] and cutoff < target['schedule_from']:
        return finish('not_due','SCHEDULE_NOT_DUE','尚未到达启用后的首次维护时间','等待设定时间')
    if resource == 'catalog':
        record = conn.execute("SELECT id,state,updated_at,checkpoint,jsonb_array_length(coalesce(payload->'chunks','[]')) AS total FROM jobs WHERE payload->>'maintenance_id'=%s AND kind='catalog' AND NOT (payload ? 'retry_of') ORDER BY created_at DESC LIMIT 1",(target['scope_id'],)).fetchone()
        result['last_write'] = record['updated_at'] if record else None
        result['expected_day']=cutoff
        if record and record['state']=='succeeded' and record['total']>0 and record['checkpoint']==record['total']:
            proof=conn.execute("SELECT count(*) AS n,bool_and(state='succeeded' AND quality_state='verified') AS verified FROM job_units WHERE job_id=%s",(record['id'],)).fetchone()
            if proof['n']==record['total'] and proof['verified']:
                result['actual_day']=record['updated_at'].date()
                if result['actual_day']>=cutoff:
                    return finish('current','CATALOG_CURRENT','该维护范围的目录分块已校验并提交','按原范围继续维护')
                return finish('stale','CATALOG_STALE','最近一次完整目录同步早于维护截止日','检查目录维护任务与行情源')
        return finish('pending_verification','CATALOG_SCOPE_UNVERIFIED','目录仅有任务执行记录，尚未确认本次目录范围完整性','查看该维护范围的目录任务和缺失对象')

    metadata = {}
    if resource == 'bars':
        security = conn.execute('SELECT metadata,subtype FROM securities WHERE source=%s AND code=%s',(source,code)).fetchone()
        metadata = security['metadata'] if security and security['subtype']=='contract' else {}
        record = conn.execute('SELECT time,trading_day,as_of_date,updated_at FROM bars WHERE source=%s AND code=%s AND period=%s ORDER BY time DESC LIMIT 1',(source,code,period)).fetchone()
        result['last_write'] = record['updated_at'] if record else None
        result['actual_day'] = (record['as_of_date'] if period in AGGREGATE_PERIODS else record['trading_day'] or record['time'].date()) if record else None
        if period in MINUTE_PERIODS:
            return finish('pending_verification','SESSION_UNVERIFIED','分钟交易时段与夜盘归属未核验，最新一条不能证明完整','核验该合约交易时段和上游交易日字段')
        market = source_market(code,source)
        if period in AGGREGATE_PERIODS and record:
            label=record['time'].date()
            first=label-timedelta(days=4) if period=='1w' else label.replace(day=1)
            period_dates=conn.execute('SELECT day,is_open FROM trading_dates WHERE source=%s AND market=%s AND day BETWEEN %s AND %s',(source,market,first,label)).fetchall()
            problems=aggregate_issues([dict(record,period=period)],period_dates,now)
            if problems:
                problem=problems[0]
                result['expected_day']=label
                return finish(problem['quality_state'],problem['code'],problem['reason'],problem['action'])
    else:
        market = source_market(code,source) if resource=='mapping' else TS_EXCHANGES[target['exchange']]
        if resource == 'calendar':
            record = conn.execute('SELECT max(day) AS actual FROM trading_dates WHERE source=%s AND market=%s',(source,market)).fetchone()
        elif resource == 'mapping':
            record = conn.execute('SELECT max(trading_day) AS actual,max(updated_at) AS updated_at FROM contract_mappings WHERE source=%s AND code=%s',(source,code)).fetchone()
        else:
            definition=REPORTS[resource]
            table,date_field=definition['table'],definition['date']
            identity='ts_code' if resource=='settle' else 'prd' if resource=='weekly_detail' else 'symbol'
            exchange = 'SHFE' if resource=='holding' and target['exchange']=='INE' else target['exchange']
            record = conn.execute('SELECT max('+date_field+') AS actual,max(updated_at) AS updated_at FROM '+table+' WHERE source=%s AND exchange=%s AND '+identity+'=%s',(source,exchange,code if resource=='settle' else target['symbol'])).fetchone()
        result['actual_day'], result['last_write'] = record['actual'], record.get('updated_at')
        if resource in ('warehouse','holding','settle','weekly_detail'):
            return finish('pending_verification','REPORT_PUBLICATION_UNVERIFIED','该品种资料适用范围和发布时间未核验，不能把空响应当缺失','核验该品种是否发布该资料及发布时间')

    try:
        listed = timestamp(metadata['listed']).date() if metadata.get('listed') else None
        expiry = timestamp(metadata['expiry']).date() if metadata.get('expiry') else None
    except ValueError:
        return finish('pending_verification','LIFECYCLE_INVALID','合约上市或到期日期无效','同步并核对合约资料')
    if listed and cutoff < listed:
        return finish('not_applicable','NOT_LISTED','调度截止日早于合约上市日期','无需补数')
    cutoff = min(cutoff, expiry) if expiry else cutoff
    start = cutoff if resource=='calendar' else max(filter(None,(listed,target['schedule_from'],cutoff-timedelta(days=366))))
    if start > cutoff:
        return finish('not_applicable','OUTSIDE_LIFECYCLE','维护起始日已超过合约存续区间','保留历史归档；无需继续更新')
    key = source,market,start,cutoff
    if key not in calendars:
        calendars[key] = conn.execute('SELECT day,is_open FROM trading_dates WHERE source=%s AND market=%s AND day BETWEEN %s AND %s ORDER BY day',(source,market,start,cutoff)).fetchall()
    calendar = calendars[key]
    opened = [row['day'] for row in calendar if row['is_open']]
    expected = opened[-1] if opened else None
    result['expected_day'] = cutoff if resource=='calendar' else expected
    if resource == 'calendar':
        if not calendar or calendar[-1]['day'] < cutoff:
            state = 'stale' if result['actual_day'] else 'missing'
            return finish(state,'CALENDAR_BEHIND','维护窗口的日历未覆盖截止日期','同步该来源与交易所的交易日历')
        return finish('current','CALENDAR_CURRENT','日历已包含调度截止日；历史缺日另查任务质量','无需新鲜度补数')
    if not expected:
        if calendar and len(calendar)==(cutoff-start).days+1:
            return finish('not_applicable','NO_OPEN_DAY','已保存日历确认维护区间均休市','无需补数')
        return finish('pending_verification','CALENDAR_UNKNOWN','日历不足，无法确认应有交易日','同步该来源与交易所的交易日历')
    if sum(row['day']>=expected for row in calendar) != (cutoff-expected).days+1:
        return finish('pending_verification','CALENDAR_UNKNOWN','最新交易日之后的日历存在缺日，不能确认是否休市','补全该来源日历后重新检查')
    actual = result['actual_day']
    if actual and actual>now.date():
        return finish('pending_verification','FUTURE_DATA_DATE','数据日期位于未来，不能确认新鲜度','核对原始日期与时区')
    if actual is None:
        return finish('missing','DATA_MISSING','应有交易日已到，尚无对应记录','检查维护任务或按该对象创建回补')
    if actual<expected:
        return finish('stale','DATA_STALE','已有记录落后于维护目标交易日','查看维护任务并补齐目标区间；核验停牌或无成交情况')
    return finish('current','DATA_CURRENT','最新数据日期已达到维护目标；不代表区间连续性通过','无需新鲜度补数；历史缺口另见任务质量')


def freshness_page(store, params, now=None):
    now = timestamp(now) if now else datetime.now(SHANGHAI)
    limit, offset = int(params.get('limit',50)), int(params.get('offset',0))
    sources = filter_values(params.get('sources',[]),('qmt','tushare'),'新鲜度来源')
    search = str(params.get('search','')).strip().casefold()
    if not 1<=limit<=200 or offset<0:
        raise ValueError('无效新鲜度分页参数')
    with store.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        # ponytail: count scans saved scopes; move filtering to SQL if scope size makes this slow.
        def matching():
            return (target for target in targets(conn) if (not sources or target['source'] in sources) and
                    (not search or search in (target['name']+' '+target['code']+' '+target['period']).casefold()))
        total = sum(1 for _ in matching())
        calendars = {}
        rows = [assess(conn,target,now,calendars) for target in islice(matching(),offset,offset+limit)]
    return dict(rows=rows,total=total,offset=offset,next_offset=offset+len(rows) if offset+len(rows)<total else None,evaluated_at=now)
