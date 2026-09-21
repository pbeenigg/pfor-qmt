"""Futures reference/report records in the shared schema and job pipeline."""
import hashlib
import json
import re
from datetime import datetime, timedelta

from psycopg import sql

from .data import SHANGHAI, day, number, timestamp
from .identifiers import TS_EXCHANGES, source_code, source_market


REPORTS = {
    'settle': {'name':'结算参数','table':'futures_settlements','date':'trade_date',
               'fields':('source','ts_code','exchange','trade_date','settle','trading_fee_rate','trading_fee','delivery_fee','b_hedging_margin_rate','s_hedging_margin_rate','long_margin_rate','short_margin_rate','offset_today_fee'),
               'numeric':('settle','trading_fee_rate','trading_fee','delivery_fee','b_hedging_margin_rate','s_hedging_margin_rate','long_margin_rate','short_margin_rate','offset_today_fee'),
               'units':'结算价为合约报价单位；手续费及费率保留上游原值，费率基准未统一注明，不自动乘100或解释为百分比；不代表券商账户实际收费'},
    'weekly_detail': {'name':'主要品种交易周报','table':'futures_weekly_details','date':'week_date',
                      'fields':('source','exchange','prd','name','week','week_date','vol','vol_yoy','amount','amout_yoy','cumvol','cumvol_yoy','cumamt','cumamt_yoy','open_interest','interest_wow','mc_close','close_wow','original_amount','original_cumamt','normalization_version'),
                      'numeric':('vol','vol_yoy','amount','amout_yoy','cumvol','cumvol_yoy','cumamt','cumamt_yoy','open_interest','interest_wow','mc_close','close_wow','original_amount','original_cumamt'),
                      'units':'成交量/累计成交量/持仓量：手；amount/cumamt：元，original_amount/original_cumamt：原始亿元；同比/环比：原始百分数；主力收盘价：报价单位；来源：中国证监会/Tushare'},
    'calendar': {'name': '交易日历', 'table': 'trading_dates', 'date': 'day',
                 'fields': ('source','market','day','is_open','pretrade_date'),
                 'units': 'is_open=true 开市，false 休市；日期为 Asia/Shanghai'},
    'mapping': {'name': '主力映射', 'table': 'contract_mappings', 'date': 'trading_day',
                'fields': ('source','code','trading_day','member_code'),
                'units': '每日上游映射，未拼接连续分钟'},
    'warehouse': {'name': '仓单日报', 'table': 'futures_warehouse_receipts', 'date': 'trade_date',
                  'fields': ('source','exchange','symbol','trade_date','fut_name','warehouse','wh_id','pre_vol','vol','vol_chg','area','year','grade','brand','place','pd','is_ct','unit'),
                  'numeric': ('pre_vol','vol','vol_chg','pd'),
                  'units': '数量按每条 unit 原始单位；不同单位不直接合计'},
    'holding': {'name': '成交持仓排名', 'table': 'futures_holdings', 'date': 'trade_date',
                'fields': ('source','exchange','symbol','trade_date','broker','vol','vol_chg','long_hld','long_chg','short_hld','short_chg'),
                'numeric': ('vol','vol_chg','long_hld','long_chg','short_hld','short_chg'),
                'units': '成交量及多空持仓：手；空值代表上游未提供，不等于零或排名末位'},
}
WSR_DIMENSIONS = ('fut_name','warehouse','wh_id','area','year','grade','brand','place','is_ct','unit')
QUERY_DIMENSIONS = {'warehouse':('warehouse','unit'), 'holding':('broker',)}


def definition_for(payload):
    definition=dict(REPORTS[payload['resource']])
    if payload.get('source','tushare')=='qmt':
        from .qmt_references import current_mapping
        if current_mapping(payload):
            definition.update(table='current_contract_mappings',date='observed_at',fields=('source','code','member_code','observed_at','trading_day'),
                              units='当前已保存映射快照；采集时间不等于交易日，不代表历史逐日关系')
        elif payload['resource']=='calendar':
            definition.update(fields=(*definition['fields'],'evidence','observed_at'),units='开市/休市仅依据QMT；缺记录为未知，K线日期不证明日历完整')
    return definition


def instrument_names(conn, rows, source='tushare'):
    identifiers={row[key] for row in rows for key in ('code','member_code','ts_code') if row.get(key)}
    if not identifiers: return {}
    return {row['code']:row['name'] for row in conn.execute('SELECT code,name FROM securities WHERE source=%s AND code=ANY(%s)',(source,list(identifiers)))}


def query_dimensions(resource, value=None):
    value={} if value is None else value
    if not isinstance(value,dict) or set(value)-set(QUERY_DIMENSIONS.get(resource,())):
        raise ValueError('当前资料不支持这些明细筛选字段')
    result={}
    for field, selected in sorted(value.items()):
        if not isinstance(selected,list) or len(selected)>1000 or any(item is not None and (not isinstance(item,str) or len(item)>300) for item in selected):
            raise ValueError('明细筛选需为最多1000个名称或NULL的列表')
        if selected: result[field]=sorted(set(selected),key=lambda item:(item is not None,item or ''))
    return result


def selection(payload):
    resource = payload.get('resource')
    source=payload.get('source','tushare')
    if resource not in REPORTS or source not in ('qmt','tushare') or source=='qmt' and resource not in ('calendar','mapping'):
        raise ValueError('当前来源不支持该期货资料类型')
    exchange = payload.get('exchange', '')
    code = source_code(payload.get('code'), source) if resource in ('mapping','settle') else ''
    mode=payload.get('mapping_mode','current' if source=='qmt' else 'history')
    if mode not in ('current','history') or source=='tushare' and mode!='history': raise ValueError('无效主力映射模式')
    if source=='qmt' and resource=='mapping':
        from .policy import validate
        validate('xtdata.get_main_contract',{'stock_code':code})
    if resource != 'mapping' and exchange not in TS_EXCHANGES:
        raise ValueError('请选择期货交易所')
    if resource=='settle' and source_market(code,'tushare')!=TS_EXCHANGES[exchange]:
        raise ValueError('结算合约与所选交易所不一致')
    symbol = str(payload.get('symbol', '')).strip().upper() if resource in ('warehouse','holding','weekly_detail') else ''
    if resource in ('warehouse','holding','weekly_detail') and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,30}', symbol):
        raise ValueError('请选择产品或合约')
    start, end = day(payload.get('start', '1990-01-01')), day(payload.get('end', datetime.now(SHANGHAI).date()))
    if start > end:
        raise ValueError('开始日期不得晚于结束日期')
    return dict(source=source, resource=resource, exchange=exchange, code=code, symbol=symbol, start=start.isoformat(), end=end.isoformat(),**({'mapping_mode':mode} if source=='qmt' and resource=='mapping' else {}))


def request_chunks(payload):
    if payload.get('selections'):
        return [dict(part, selection=target) for target in selections(payload) for part in request_chunks(target)]
    first, end = day(payload['start']), day(payload['end'])
    from .qmt_references import current_mapping
    if current_mapping(payload):
        today=datetime.now(SHANGHAI).date().isoformat()
        return [dict(code=payload['code'],period='mapping',start=today,end=today,mapping_mode='current')]
    if (end-first).days > (7305 if payload['resource']=='weekly_detail' else 3660) or payload['resource'] != 'calendar' and end > datetime.now(SHANGHAI).date():
        raise ValueError('周报最多二十年，其他资料最多十年；非日历资料不得包含未来日期')
    result = []
    width = 365 if payload['resource'] in ('calendar','mapping','weekly_detail') else 30
    while first <= end:
        last = min(end, first + timedelta(days=width-1))
        result.append(dict(code=payload['code'] or payload['exchange']+':'+payload['symbol'], period=payload['resource'], start=first.isoformat(), end=last.isoformat()))
        first = last + timedelta(days=1)
    return result


def selections(payload):
    values = payload.get('selections')
    if values is None:
        return [selection(payload)]
    if not isinstance(values, list) or not 1 <= len(values) <= 10000 or any(not isinstance(item, dict) for item in values):
        raise ValueError('一次选择1至10000个资料对象')
    unique = {}
    for item in values:
        if item.get('dimensions'): raise ValueError('明细筛选请放在查询顶层dimensions中')
        target = selection({**payload, **item, 'start':payload['start'], 'end':payload['end']})
        if target['source']!=payload.get('source','tushare'): raise ValueError('一次采集不能混合来源')
        unique[(target['resource'],target['exchange'],target['symbol'],target['code'],target.get('mapping_mode'))] = target
    return list(unique.values())


def normalize_report(resource, records, exchange, symbol, start, end, stored=False):
    definition = REPORTS[resource]
    result = {}
    for raw in records:
        date = timestamp(raw.get(definition['date'])).date()
        identity='ts_code' if resource=='settle' else 'prd' if resource=='weekly_detail' else 'symbol'
        if not start <= date <= end or str(raw.get(identity,'')).upper() != symbol or raw.get('exchange') != exchange:
            raise ValueError('期货资料返回的日期、产品或交易所与请求不符')
        row = {key: raw.get(key) for key in definition['fields']}
        row.update(source='tushare',exchange=exchange,**{identity:symbol,definition['date']:date})
        for key in definition['fields']:
            if key in definition['numeric']:
                row[key] = number(row[key])
                if key in ('vol','pre_vol','long_hld','short_hld','cumvol','open_interest','amount','cumamt') and row[key] is not None and row[key]<0:
                    raise ValueError('仓单量、成交量或持仓量不得为负；变化量可为负')
            elif row[key] is not None and key != definition['date']:
                row[key] = str(row[key])
        if resource=='settle':
            if all(row[key] is None for key in definition['numeric']):
                raise ValueError('结算参数全部数值为空，未确认成功')
            if source_market(row['ts_code'],'tushare')!=TS_EXCHANGES[exchange]:
                raise ValueError('结算参数返回跨交易所合约')
            if any(row[key] is not None and row[key]<0 for key in definition['numeric'] if key!='settle'):
                raise ValueError('结算手续费或保证金率不能为负')
            key=(date,symbol)
        elif resource=='weekly_detail':
            if not row['name'] or not re.fullmatch(r'\d{4}\d{1,2}',row['week'] or '') or not 1<=int(row['week'][4:])<=53:
                raise ValueError('周报品种名称或原始周编号无效')
            if all(row[key] is None for key in ('vol','amount','open_interest','mc_close')):
                raise ValueError('周报关键数值全部为空')
            for field in ('amount','cumamt'):
                value=row['original_'+field] if stored else row[field]
                converted=scale_yi(value)
                if stored and (row[field]!=converted or row['normalization_version']!='tushare-weekly-detail-v1'):
                    raise ValueError('周报成交额转换口径不一致')
                row['original_'+field]=value
                row[field]=converted
            row['normalization_version']='tushare-weekly-detail-v1'
            key=(date,symbol)
        elif resource == 'warehouse':
            if not (row['warehouse'] or row['wh_id']) or not row['unit']:
                raise ValueError('仓单缺少仓库标识或原始单位')
            row['row_key'] = hashlib.sha256(json.dumps([row[key] for key in WSR_DIMENSIONS],ensure_ascii=False).encode()).hexdigest()
            key = (date, row['row_key'])
        else:
            if not row['broker']:
                raise ValueError('持仓排名缺少会员标识')
            key = (date, row['broker'])
        if key in result and result[key] != row:
            raise ValueError('相同资料维度出现冲突记录，未覆盖入库')
        result[key] = row
    return [result[key] for key in sorted(result)]


def scale_yi(value):
    if value is None: return None
    from decimal import Decimal
    sign,digits,exponent=value.as_tuple()
    return Decimal((sign,digits,exponent+8))


def report_filter(payload):
    targets = selections(payload)
    p = targets[0]
    if any(item['resource'] != p['resource'] or item['source']!=p['source'] or item.get('mapping_mode')!=p.get('mapping_mode') for item in targets):
        raise ValueError('每次资料查询只展示同一种资料，不能混合字段与单位')
    resource, definition = p['resource'], definition_for(p)
    where, args = 'source=%s', [p['source']]
    if resource == 'calendar':
        where += ' AND market=ANY(%s)'; args.append(sorted({TS_EXCHANGES[item['exchange']] for item in targets}))
    elif resource in ('mapping','settle'):
        where += ' AND '+('ts_code' if resource=='settle' else 'code')+'=ANY(%s)'; args.append(sorted({item['code'] for item in targets}))
    else:
        markets = {}
        for item in targets:
            exchange = 'SHFE' if resource == 'holding' and item['exchange'] == 'INE' else item['exchange']
            markets.setdefault(exchange, set()).add(item['symbol'])
        product='prd' if resource=='weekly_detail' else 'symbol'
        where += ' AND ('+' OR '.join('(exchange=%s AND '+product+'=ANY(%s))' for _ in markets)+')'
        for exchange, symbols in sorted(markets.items()):
            args.extend([exchange, sorted(symbols)])
    from .qmt_references import current_mapping
    if not current_mapping(p):
        args.extend([p['start'],p['end']])
        where+=' AND '+definition['date']+' BETWEEN %s AND %s'
    for field,values in query_dimensions(resource,payload.get('dimensions')).items():
        where+=' AND ('+field+'=ANY(%s) OR (%s AND '+field+' IS NULL))'
        args.extend([[value for value in values if value is not None],None in values])
    return resource,where,args


def filter_options(store, payload):
    from .analytics import snapshot
    resource,_,_=report_filter(payload)
    _,where,args=report_filter(dict(payload,dimensions={}))
    table=definition_for(selections(payload)[0])['table'];options={}
    with store.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        meta=snapshot(conn,table,where,args,{})
        for field in QUERY_DIMENSIONS.get(resource,()):
            rows=conn.execute('SELECT DISTINCT '+field+' AS value FROM '+table+' WHERE '+where+' ORDER BY '+field+' NULLS FIRST LIMIT 1001',args).fetchall()
            if len(rows)>1000: raise ValueError('明细选项超过1000个，请缩小品种或日期范围；未截断选项')
            options[field]=[row['value'] for row in rows]
    return dict(meta,resource=resource,options=options,source='postgresql')


def page(store, payload, limit=200, offset=0, conn=None):
    from .analytics import ordering, snapshot
    resource,where,args = report_filter(payload)
    selected=selections(payload)[0]
    definition = definition_for(selected)
    columns = definition['fields']
    limit,offset = int(limit),int(offset)
    if not 1<=limit<=5000 or offset<0: raise ValueError('分页limit需要1至5000，offset不得为负')
    order=ordering(payload,columns,definition['date'],columns)
    statement='SELECT '+','.join(columns)+' FROM '+definition['table']+' WHERE '+where+' ORDER BY '+order+' LIMIT %s OFFSET %s'
    def read(connection, include_summary=True):
        meta=snapshot(connection,definition['table'],where,args,payload) if include_summary else {}
        rows=connection.execute(statement,(*args,limit+1,offset)).fetchall()
        more=len(rows)>limit
        rows=rows[:limit]
        if include_summary and resource in ('mapping','settle'):
            meta['instrument_names']=instrument_names(connection,rows,selected['source'])
        return dict(meta,rows=rows,resource=resource,fields=list(columns),source='postgresql',provider=selected['source'],mapping_mode=selected.get('mapping_mode','history'),timezone='Asia/Shanghai',
                    dimensions=query_dimensions(resource,payload.get('dimensions')),
                    units=definition['units'],normalization_version='qmt-references-v1' if selected['source']=='qmt' else 'tushare-weekly-detail-v1' if resource=='weekly_detail' else 'tushare-reports-v1',offset=offset,limit=limit,
                    next_offset=offset+len(rows) if more else None)
    # Exports already own a repeatable-read transaction; do not count the full range per batch.
    if conn is not None: return read(conn,False)
    with store.connect() as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return read(connection)


def write_records(conn, resource, rows):
    if rows and rows[0]['source']=='qmt':
        from .storage import document
        if resource=='mapping' and 'observed_at' in rows[0]:
            for row in rows:
                conn.execute('INSERT INTO contract_mapping_snapshots(source,code,member_code,observed_at,trading_day,job_id,unit_index,source_fields) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id,unit_index,code) DO NOTHING',
                             tuple(row[key] for key in ('source','code','member_code','observed_at','trading_day','job_id','unit_index'))+(document(row['source_fields']),))
            return
        if resource=='calendar':
            for row in rows:
                if row['evidence']=='bar_observation' and conn.execute("SELECT 1 FROM trading_dates WHERE source='qmt' AND market=%s AND day=%s AND evidence='calendar' AND NOT is_open",(row['market'],row['day'])).fetchone():
                    from .reliability import DataRejected
                    raise DataRejected('合约日线观察与已保存交易所休市日冲突，保留原日历',code='CALENDAR_EVIDENCE_CONFLICT',sample={'market':row['market'],'day':str(row['day'])})
                conn.execute("INSERT INTO trading_dates(source,market,day,is_open,pretrade_date,evidence,observed_at,source_fields) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source,market,day) DO UPDATE SET is_open=EXCLUDED.is_open,evidence=EXCLUDED.evidence,observed_at=EXCLUDED.observed_at,source_fields=EXCLUDED.source_fields WHERE trading_dates.evidence<>'calendar' OR EXCLUDED.evidence='calendar'",
                             tuple(row[key] for key in ('source','market','day','is_open','pretrade_date','evidence','observed_at'))+(document(row['source_fields']),))
            return
    definition = REPORTS[resource]
    if resource in ('warehouse','holding'):
        # Replace only positively observed, non-truncated daily snapshots; empty days retain old data.
        for source,exchange,symbol,date in sorted({(r['source'],r['exchange'],r['symbol'],r['trade_date']) for r in rows}):
            conn.execute(sql.SQL('DELETE FROM {} WHERE source=%s AND exchange=%s AND symbol=%s AND trade_date=%s').format(sql.Identifier(definition['table'])),(source,exchange,symbol,date))
    columns = (*definition['fields'], 'row_key') if resource == 'warehouse' else definition['fields']
    keys = {'settle':('source','ts_code','trade_date'),'weekly_detail':('source','exchange','prd','week_date'),
            'calendar':('source','market','day'), 'mapping':('source','code','trading_day'),
            'warehouse':('source','exchange','symbol','trade_date','row_key'), 'holding':('source','exchange','symbol','trade_date','broker')}[resource]
    updates = [key for key in columns if key not in keys]
    assignments=[sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(key),sql.Identifier(key)) for key in updates]
    if resource in ('settle','weekly_detail'):
        assignments.append(sql.SQL('updated_at=now()'))
    statement = sql.SQL('INSERT INTO {}({}) VALUES({}) ON CONFLICT({}) DO UPDATE SET {}').format(
        sql.Identifier(definition['table']), sql.SQL(',').join(map(sql.Identifier,columns)),
        sql.SQL(',').join(sql.Placeholder(key) for key in columns), sql.SQL(',').join(map(sql.Identifier,keys)),
        sql.SQL(',').join(assignments))
    if rows:
        with conn.cursor() as cursor:
            cursor.executemany(statement,rows)
        if resource=='calendar':
            for row in rows:
                conn.execute("UPDATE trading_dates SET evidence='calendar',observed_at=now() WHERE source=%s AND market=%s AND day=%s",(row['source'],row['market'],row['day']))
        if resource=='mapping' and rows[0]['source']=='qmt':
            for row in rows:
                conn.execute('UPDATE contract_mappings SET source_fields=%s,updated_at=now() WHERE source=%s AND code=%s AND trading_day=%s',(document(row['source_fields']),row['source'],row['code'],row['trading_day']))
