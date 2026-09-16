"""Read-only market checks; bridge connectivity is not data readiness."""
from datetime import datetime, timedelta

from .data import SHANGHAI, codes, normalize_bars, timestamp


def check_source(source, ping, code='000300.SH', now=None):
    code = codes([code])[0]
    now = now or datetime.now(SHANGHAI)
    end, start = now.date(), now.date() - timedelta(days=30)
    checks = []
    result = dict(code=code, checked_at=now.isoformat(), start=start.isoformat(),
                  end=end.isoformat(), checks=checks, history_readable=False)

    def run(name, operation):
        try:
            details = operation()
            checks.append(dict(name=name, **details))
            return details['state'] == 'ok'
        except Exception:
            # Never relay driver/terminal exceptions which may contain credentials.
            checks.append(dict(name=name, state='error', message='请求失败，请检查行情连接或终端接口能力'))
            return False

    def bridge():
        value = ping()
        ok = value.get('mode') == 'market-only'
        return dict(state='ok' if ok else 'error', message='纯行情桥已连接' if ok else '行情桥响应异常')

    if not run('bridge', bridge):
        for name in ('snapshot', 'history', 'calendar'):
            checks.append(dict(name=name, state='unverified', message='桥未就绪，未执行'))
        return result

    def snapshot():
        value = (source.get_full_tick([code]) or {}).get(code) or {}
        when = value.get('time')
        return dict(state='ok' if value else 'empty', message='快照可读，时效以行情时间为准' if value else '快照为空',
                    time=timestamp(when).isoformat() if when else None)

    def history():
        data = source.get_local_data(stock_list=[code], period='1d', start_time=start.strftime('%Y%m%d'),
                                     end_time=end.strftime('%Y%m%d') + '235959')
        rows = normalize_bars((data or {}).get(code), code, '1d', start, end)
        return dict(state='ok' if rows else 'empty', rows=len(rows),
                    message='本地日线可读，下载与覆盖尚需验收' if rows else '本地日线为空',
                    last_time=rows[-1]['time'].isoformat() if rows else None)

    def calendar():
        values = source.get_trading_dates(code, start.strftime('%Y%m%d'), end.strftime('%Y%m%d'))
        days = sorted({timestamp(value).date() for value in values if start <= timestamp(value).date() <= end})
        return dict(state='ok' if days else 'empty', rows=len(days), message='交易日期可读' if days else '交易日历为空')

    run('snapshot', snapshot)
    history_ok = run('history', history)
    calendar_ok = run('calendar', calendar)
    result['history_readable'] = history_ok and calendar_ok
    return result
