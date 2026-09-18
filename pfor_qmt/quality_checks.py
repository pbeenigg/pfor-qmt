"""Evidence-based checks shared by collection and offline verification."""
import re
from datetime import datetime, timedelta

from .data import SHANGHAI, day, timestamp, period_label, AGGREGATE_PERIODS, MINUTE_PERIODS
from .reliability import issue

RULE_VERSION = 'coverage-v2'


def aggregate_issues(rows, calendar, now=None):
    now = now or datetime.now(SHANGHAI)
    dates = {row['day']:row['is_open'] for row in calendar}
    issues = []
    for row in rows:
        label, as_of = row['time'].date(), row.get('as_of_date')
        first = label-timedelta(days=4) if row['period']=='1w' else label.replace(day=1)
        if as_of is None or not first <= as_of <= label:
            issues.append(issue('PERIOD_AS_OF_INVALID','rejected','计算截至日期缺失或不在周期内','核对上游周期标签与计算截至日',day=label.isoformat()))
        elif label >= now.date():
            issues.append(issue('PERIOD_OPEN','not_published','周期结束日尚未完整经过，保留当前计算值','周期结束后重新核验或更新',True,day=label.isoformat()))
        elif any(first+timedelta(days=i) not in dates for i in range((label-first).days+1)):
            issues.append(issue('PERIOD_CALENDAR_UNKNOWN','pending_verification','周期内日历未完整保存，不能确认最后交易日','同步对应交易所日历后重新核验',day=label.isoformat()))
        else:
            opened = [date for date,is_open in dates.items() if is_open and first<=date<=label]
            if not opened:
                issues.append(issue('PERIOD_NO_OPEN_DAY','pending_verification','周期内无开市日但返回行情，需核对来源口径','核对周期标签与交易所日历',day=label.isoformat()))
            elif as_of < max(opened):
                issues.append(issue('PERIOD_STALE','stale','计算截至日期落后于本周期最后交易日','更新该周期后重新核验',True,day=label.isoformat(),expected_day=max(opened).isoformat(),actual_day=as_of.isoformat()))
    return issues


def minute_issues(rows, period, metadata=None):
    # Descriptions can omit breaks or historical changes: observations are not a full-session certificate.
    if not rows:
        return [issue('MINUTE_EMPTY','pending_verification','本地未读到分钟记录，不能核验连续性','确认分钟权限、请求范围和原采集任务')]
    description = str((metadata or {}).get('trade_time_desc') or '')
    windows = []
    for a,b,c,d in re.findall(r'(\d{1,2}):(\d{2})\s*[-~～至]\s*(\d{1,2}):(\d{2})',description):
        start,end = int(a)*60+int(b),int(c)*60+int(d)
        if 0<=start<end<1440:
            windows.append((start,end))
    gaps=[]
    step=int(period[:-1])
    for previous,current in zip(rows,rows[1:]):
        first,last=timestamp(previous['time']),timestamp(current['time'])
        minutes=(last-first).total_seconds()/60
        if first.date()==last.date() and minutes>step and any(a<=first.hour*60+first.minute<last.hour*60+last.minute<=b for a,b in windows):
            gaps.append({'after':first.isoformat(),'before':last.isoformat(),'elapsed_minutes':minutes})
    result=[issue('SESSION_UNVERIFIED','pending_verification','分钟时段、时间标签及无成交返回规则尚未核验','核对上游分钟权限与合约时段；可疑间隔不能直接当缺失',rule_version=RULE_VERSION,session_description=description or None)]
    if gaps:
        result.append(issue('MINUTE_INTERVAL_GAPS','pending_verification','时段描述内存在较长记录间隔，可能是休市、无成交或缺数','逐段核对休市与无成交规则后再决定补数',gap_count=len(gaps),samples=gaps[:20],samples_truncated=len(gaps)>20))
    if any(row.get('trading_day') is None for row in rows):
        result.append(issue('TRADING_DAY_UNKNOWN','pending_verification','上游未提供交易日，原始夜盘时间已保留','取得明确交易日依据后核验；不以自然日代替交易日'))
    return result


def stored_issues(rows, part, calendar, metadata=None, now=None):
    period=part['period']
    if period in MINUTE_PERIODS:
        return minute_issues(rows,period,metadata)
    opened=[row['day'] for row in calendar if row['is_open'] and day(part['start'])<=row['day']<=day(part['end'])]
    actual={row.get('trading_day') or row['time'].date() for row in rows}
    expected={period_label(date,period) for date in opened}
    result=[issue('BARS_MISSING','missing','已保存开市日未读到行情','核对停牌、生命周期后定向回补',True,day=date.isoformat()) for date in sorted(expected-actual)]
    days=(day(part['end'])-day(part['start'])).days+1
    complete=len({row['day'] for row in calendar if day(part['start'])<=row['day']<=day(part['end'])})==days
    if not calendar:
        result.append(issue('CALENDAR_UNKNOWN','pending_verification','无已保存日历，无法确认覆盖','同步该来源日历后重新核验'))
    elif not rows and not opened:
        if complete:
            result.append(issue('NO_OPEN_DAY','not_applicable','完整日历确认该区间休市','无需补数'))
        else:
            result.append(issue('CALENDAR_UNKNOWN','pending_verification','区间日历不完整，空表不能判定不适用','同步完整日历后重新核验'))
    elif not complete:
        result.append(issue('CALENDAR_INCOMPLETE','pending_verification','已保存日历未覆盖区间全部自然日，不能推断缺日均休市','同步完整日历后重新核验'))
    if period in AGGREGATE_PERIODS:
        result.extend(aggregate_issues(rows,calendar,now))
    return result


def repair_ranges(job, units, unit_indices=None, now=None):
    """Select only dated, explicitly repairable findings within the verified request."""
    parts=job['payload']['chunks']
    if unit_indices is not None and (not isinstance(unit_indices,list) or not unit_indices or any(type(index) is not int or index<0 or index>=len(parts) for index in unit_indices)):
        raise ValueError('请选择有效的核验分块')
    today=(now or datetime.now(SHANGHAI)).date()
    selected={}
    for unit in units:
        index=unit['unit_index']
        if unit_indices is not None and index not in unit_indices: continue
        part=parts[index]
        if part['period'] in MINUTE_PERIODS: continue
        intervals=[]
        for finding in unit['issues']:
            if not finding.get('retryable') or finding.get('code') not in ('BARS_MISSING','CALENDAR_MISSING','PERIOD_STALE','PERIOD_OPEN') or not finding.get('day'): continue
            missing=day(finding['day'])
            first=missing-timedelta(days=4) if part['period']=='1w' else missing.replace(day=1) if part['period']=='1mo' else missing
            if part['period'] in AGGREGATE_PERIODS and missing>=today: continue
            if part['period']!='calendar' and missing>today: continue
            start,end=max(day(part['start']),first),min(day(part['end']),missing)
            if start<=end: intervals.append((start,end))
        merged=[]
        for start,end in sorted(set(intervals)):
            if merged and start<=merged[-1][1]+timedelta(days=1):
                merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
            else:
                merged.append((start,end))
        for start,end in merged:
            candidate=dict(part,start=start.isoformat(),end=end.isoformat())
            selected[(index,start,end)]=dict(unit_index=index,request=candidate)
            if len(selected)>100000:
                raise ValueError('补数范围超过100000分块，请缩小选择范围')
    return list(selected.values())
