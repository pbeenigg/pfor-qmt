"""QMT reference evidence, separate from bar timestamps and observed snapshots."""
import re
from datetime import datetime, timedelta

from .data import day, timestamp, SHANGHAI
from .symbols import normalize_code, market_of, contract_type, derivative_kind
from .identifiers import TS_EXCHANGES
from .client import CfquantError
from .reliability import DataRejected, QmtCapabilityUnavailable, issue, failure, redact


def current_mapping(payload):
    return payload.get('source')=='qmt' and payload.get('resource')=='mapping' and payload.get('mapping_mode','current')=='current'


def natural_schedule(payload):
    return payload.get('source')=='qmt' and (payload.get('resource')=='calendar' or current_mapping(payload))


def call(source, method, *args):
    function=getattr(source,method,None)
    if not callable(function): raise QmtCapabilityUnavailable(method,bridge_outdated=True)
    try:
        return function(*args)
    except CfquantError as error:
        if error.remote_type=='ValueError' and 'Unsupported market action' in str(error):
            raise QmtCapabilityUnavailable(method,bridge_outdated=True) from None
        if error.remote_type in ('AttributeError','NotImplementedError','SignatureUnavailable'):
            raise QmtCapabilityUnavailable(method) from None
        if error.remote_type=='PermissionError':
            from .tushare import SourceError
            raise SourceError('QMT明确拒绝该接口权限','permission') from None
        raise
    except NotImplementedError as error:
        if isinstance(error,QmtCapabilityUnavailable): raise
        raise QmtCapabilityUnavailable(method) from None


def calendar_samples(store):
    rows=store.query("SELECT code,market FROM securities WHERE source='qmt' AND kind='future' AND subtype='continuous' AND code ~ '^[A-Za-z]+00[.][A-Z]+$' ORDER BY code")
    result={}
    for row in rows:
        codes=result.setdefault(row['market'],[])
        if len(codes)<3: codes.append(row['code'])
    return result


def observed_calendar(source,market,start,end,samples,check=lambda:None):
    dates={}
    if not samples: raise QmtCapabilityUnavailable('get_trading_calendar')
    for code in samples:
        if market_of(code)!=market: raise DataRejected('日历样本合约不属于所选市场')
        check()
        raw=call(source,'get_trading_dates',code,start.strftime('%Y%m%d'),end.strftime('%Y%m%d'),-1,'1d')
        if raw is None: continue
        if not isinstance(raw,(list,tuple)): raise DataRejected('QMT合约日线日期格式未识别，未推断日历')
        for value in raw:
            when=timestamp(value)
            if when.time()!=datetime.min.time() or not start<=when.date()<=end or when.date()>datetime.now(SHANGHAI).date():
                raise DataRejected('合约日期超出区间或包含分钟时间，未用作交易日历',sample=value)
            dates.setdefault(when.date(),[]).append({'code':code,'value':value})
    now=datetime.now(SHANGHAI)
    return [dict(source='qmt',market=market,day=date,is_open=True,pretrade_date=None,evidence='bar_observation',observed_at=now,
                 source_fields={'method':'get_trading_dates','period':'1d','samples':samples,'observations':observations,'reason':'独立交易所日历接口未提供，缺失日期保持未知'}) for date,observations in sorted(dates.items())]


def calendar(source, market, start, end, samples=(), check=lambda:None):
    first,last=day(start),day(end)
    try:
        raw=call(source,'get_trading_calendar',market,first.strftime('%Y%m%d'),last.strftime('%Y%m%d'))
    except QmtCapabilityUnavailable as error:
        if error.code!='QMT_TERMINAL_UNSUPPORTED' or not samples: raise
        return observed_calendar(source,market,first,last,samples,check)
    if raw is None or isinstance(raw,(list,tuple)) and not raw: return []
    if not isinstance(raw,(list,tuple)):
        raise DataRejected('QMT独立日历返回格式不明确，未推断休市',sample=raw)
    dates={timestamp(value).date():value for value in raw}
    if any(date<first or date>last for date in dates): raise DataRejected('日历日期超出请求区间',sample=raw)
    now=datetime.now(SHANGHAI)
    # Future dates require verified holiday coverage, unavailable in this bridge.
    return [dict(source='qmt',market=market,day=date,is_open=date in dates,pretrade_date=None,
                 evidence='calendar' if date<=now.date() else 'calendar_open',observed_at=now,
                 source_fields={'method':'get_trading_calendar','start':first.isoformat(),'end':last.isoformat(),'raw_open_date':dates.get(date)})
            for date in (first+timedelta(days=i) for i in range((last-first).days+1)) if date<=now.date() or date in dates]


def member(source, code, mapped, products=None):
    if not isinstance(mapped,str) or not mapped: raise DataRejected('主力月份合约代码为空或格式不明确',sample=mapped)
    raw=mapped
    if re.fullmatch(r'[A-Za-z]+\d{3,4}',mapped): mapped+='.'+market_of(code)
    try: mapped=normalize_code(mapped)
    except ValueError: raise DataRejected('QMT主力月份合约返回代码格式无效',sample=raw) from None
    if market_of(code)!=market_of(mapped) or derivative_kind(mapped)!='future' or contract_type(mapped)!='contract':
        raise DataRejected('主力映射返回跨市场或非月份合约',sample=mapped)
    product=re.fullmatch(r'([A-Za-z]+)00\.[A-Z]+',code)
    actual=re.fullmatch(r'([A-Za-z]+)\d{3,4}\.[A-Z]+',mapped)
    if not product or not actual:
        raise DataRejected('主力映射返回不同品种或未知月份格式',sample=mapped)
    if product[1].lower()!=actual[1].lower():
        evidence=products if products is not None else {}
        if source is not None:
            evidence.update(request=(call(source,'get_instrument_detail',code) or {}).get('ProductID'),
                            member=(call(source,'get_instrument_detail',mapped) or {}).get('ProductID'))
        if any(not isinstance(value,str) or value.lower()!=actual[1].lower() for value in (evidence.get('request'),evidence.get('member'))):
            raise DataRejected('主力映射返回不同品种或缺少终端品种依据',sample=mapped)
    return mapped


def snapshot(source,code):
    raw=call(source,'get_main_contract',code)
    if raw in (None,''): return []
    products={}
    mapped=member(source,code,raw,products)
    details=call(source,'get_instrument_detail',mapped) or {}
    raw_day=details.get('TradingDay')
    trading_day=timestamp(raw_day).date() if raw_day not in (None,'',0,'0') else None
    return [dict(source='qmt',code=code,member_code=mapped,observed_at=datetime.now(SHANGHAI),trading_day=trading_day,
                 source_fields={'method':'get_main_contract','value':raw,'TradingDay':raw_day,'product_evidence':products})]


def history(source,code,start,end):
    first,last=day(start),day(end)
    result=call(source,'get_main_contract_history',code,first.strftime('%Y%m%d'),last.strftime('%Y%m%d')+'235959')
    if result is None: return []
    raw=result.get(code) if isinstance(result,dict) else result
    if raw is None: return []
    if hasattr(raw,'to_dict'):
        rows=[dict(record,time=record.get('time',index)) for index,record in zip(raw.index,raw.to_dict('records'))]
    elif isinstance(raw,list): rows=raw
    else: raise DataRejected('历史主力返回格式未识别，未生成逐日映射',sample=raw)
    if len(rows)>=10000:
        from .tushare import SourceError
        raise SourceError('历史映射达到10000行保护上限，完整性未确认，请缩小区间','incomplete')
    dates={}
    for row in rows:
        date=timestamp(row.get('trading_day',row.get('time'))).date()
        products={}
        mapped=member(source,code,row.get('main_contract',row.get('mainContract',row.get('member_code'))),products)
        if not first<=date<=last: raise DataRejected('历史映射日期超出请求区间',sample=row)
        if date in dates and dates[date]['member_code']!=mapped: raise DataRejected('同日历史主力映射冲突',sample=row)
        dates[date]=dict(source='qmt',code=code,trading_day=date,member_code=mapped,source_fields=dict(row,product_evidence=products))
    return [dates[date] for date in sorted(dates)]


def report(source,target,start,end,calendar_codes=None,check=lambda:None):
    if target['resource']=='calendar':
        market=TS_EXCHANGES[target['exchange']]
        return calendar(source,market,start,end,(calendar_codes or {}).get(market,()),check)
    if current_mapping(target): return snapshot(source,target['code'])
    result=call(source,'download_main_contract_history',target['code'],day(start).strftime('%Y%m%d'),day(end).strftime('%Y%m%d'))
    if result is False or isinstance(result,(int,float)) and result<0: raise DataRejected('终端拒绝历史映射下载，未用缓存冒充成功')
    return history(source,target['code'],start,end)


def findings(rows,target,start,end,dates=()):
    if not rows:
        historical=target['resource']=='mapping' and not current_mapping(target)
        return [issue('REFERENCE_EMPTY','pending_verification','QMT历史主力映射返回空记录；未验证该终端提供历史映射' if historical else 'QMT返回空资料，未确认该范围可用','可使用当前映射；当前快照不会回填历史，重复大范围采集不能证明历史能力' if historical else '核对能力、请求范围与终端资料')]
    if current_mapping(target): return []
    if target['resource']=='calendar':
        if any(row['evidence']=='bar_observation' for row in rows):
            return [issue('CALENDAR_OBSERVATIONS_ONLY','pending_verification','已保存合约日线中观察到的交易日，独立交易所日历不可用，其他日期未知','可查看已观察交易日；不据此判休市或完整覆盖')]
        complete={r['day'] for r in rows if r['evidence']=='calendar'}
        missing=[day(start)+timedelta(days=i) for i in range((day(end)-day(start)).days+1) if day(start)+timedelta(days=i) not in complete]
        return [issue('CALENDAR_MISSING' if date<=datetime.now(SHANGHAI).date() else 'CALENDAR_FUTURE_UNKNOWN','pending_verification','该日期缺少完整交易所日历依据','未知日期保持未知，核对节假日覆盖',date<=datetime.now(SHANGHAI).date(),day=date.isoformat()) for date in missing]
    actual={r['trading_day'] for r in rows}
    known={r['day'] for r in dates if r.get('evidence')=='calendar'}
    problems=[issue('MAPPING_MISSING','missing','已确认开市日缺少主力映射','定向重试该交易日',True,day=r['day'].isoformat()) for r in dates if r.get('evidence')=='calendar' and r['is_open'] and r['day'] not in actual]
    if len(known)!=(day(end)-day(start)).days+1:
        problems.append(issue('MAPPING_COVERAGE_UNKNOWN','pending_verification','日历未确认完整覆盖，历史映射完整性待核验','同步该来源日历后重新核验'))
    return problems


def probe(source,params,calendar_codes=None):
    resource=params.get('resource','mapping');mode=params.get('mapping_mode','current')
    start=params.get('start') or (datetime.now(SHANGHAI).date()-timedelta(days=7)).isoformat()
    end=params.get('end') or datetime.now(SHANGHAI).date().isoformat()
    if day(start)>day(end) or (day(end)-day(start)).days>31: raise ValueError('能力检测限31天内的小样本')
    targets=[dict(source='qmt',resource='calendar',exchange=exchange) for exchange in TS_EXCHANGES] if resource=='calendar' else [dict(source='qmt',resource='mapping',mapping_mode=mode,code=code) for code in ('a00.DF','cu00.SF','IF00.IF')]
    output=[]
    for target in targets:
        try:
            rows=calendar(source,TS_EXCHANGES[target['exchange']],start,end,(calendar_codes or {}).get(TS_EXCHANGES.get(target.get('exchange')),())) if resource=='calendar' else snapshot(source,target['code']) if mode=='current' else history(source,target['code'],start,end)
            limited=resource=='calendar' and any(row['evidence']!='calendar' for row in rows)
            item=dict(state='limited' if limited else 'available' if rows else 'empty',rows=len(rows),reason='仅取得已观察开市日期；完整交易所日历不可用，未返回日期保持未知' if limited else '已实际读取并校验样本' if rows else '历史映射缓存为空，尚未验证该终端支持；不代表权限不足' if resource=='mapping' and mode=='history' else '返回空表，未证明无权限',sample=rows[:2])
        except Exception as error:
            detail=failure(error);code=detail['code']
            item=dict(state='bridge_outdated' if code=='QMT_BRIDGE_OUTDATED' else 'unsupported' if code in ('SOURCE_UNSUPPORTED','QMT_TERMINAL_UNSUPPORTED') else 'permission' if code=='SOURCE_PERMISSION' else 'connection_failed' if code in ('NETWORK_ERROR','SOURCE_NOT_READY') else 'invalid_response',reason=redact(str(error)),action=detail['action'],error_code=code,rows=0)
        output.append(dict(target=target,checked_at=datetime.now(SHANGHAI),**item))
    return dict(resource=resource,mapping_mode=mode,results=output,checked_at=datetime.now(SHANGHAI))
