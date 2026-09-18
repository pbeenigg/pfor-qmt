import csv
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import xtdata
from .client import CfquantError, CfquantTimeout
from .data import SHANGHAI, FIELDS, chunks, day, timestamp, normalize_bars, json_default, MINUTE_PERIODS, AGGREGATE_PERIODS, period_label
from .protocol import encode_value
from .storage import job_summary
from .symbols import market_of, derivative_kind
from .identifiers import source_market, TS_EXCHANGES
from .tushare import TushareSource, SourceError
from .reliability import SourceUnavailable, failure, issue, fallback_log, redact
from .quality_checks import RULE_VERSION, aggregate_issues, minute_issues, stored_issues


class Cancelled(Exception):
    pass


class LeaseLost(Exception):
    pass


def network_error(error):
    if isinstance(error, (ConnectionError, TimeoutError, OSError, CfquantTimeout)):
        return True
    if isinstance(error, CfquantError):
        return error.remote_type in ('ConnectionError', 'TimeoutError', 'OSError') or any(
            phrase in str(error).lower() for phrase in ('pipe bridge not connected', 'pipe bridge response timeout', 'pipe disconnected', 'pipe connection closed', 'pipe receive connection closed', 'pipe client closed'))
    return False


class Worker:
    def __init__(self, store, runtime, publish=None, source=None, provider='qmt', account_resolver=None, options=None):
        self.store, self.runtime = store, Path(runtime)
        self.source = source or xtdata
        self.provider, self.account_resolver = provider, account_resolver
        self.publish = publish or (lambda event: None)
        self.stop = threading.Event()
        self.thread = None
        self.last_error = ''
        self.export_error = ''
        self.catalog_error = ''
        self.last_schedule = None
        self.last_cleanup = None
        self.schedule_errors = {}
        self.schedule_issues = {}
        self.calendar_blocks = {}
        self.options = options or {}
        self.leases = threading.local()

    def status_issues(self):
        issues=[]
        if self.last_error:
            issues.extend(list(self.schedule_issues.copy().values()) or [dict(source=self.provider,lane='schedule',name='调度服务',market='',reason=redact(self.last_error),action='查看运行日志并检查数据源连接')])
        for lane,message in (('catalog',self.catalog_error),('export',self.export_error)):
            if message:
                issues.append(dict(source=self.provider,lane=lane,name='目录同步' if lane=='catalog' else '文件导出',market='',reason=redact(message),action='查看运行日志，排除故障后重试'))
        return issues

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='pfor-market-worker')
        self.thread.start()

    def check(self, identifier):
        lease = getattr(self.leases,'connection',None)
        if lease is not None:
            try:
                lease.execute('SELECT 1')
                lease.commit()
            except Exception:
                self.leases.lost = True
                raise LeaseLost('工作队列租约连接丢失；停止当前执行，等待新租约恢复') from None
        if self.stop.is_set():
            raise InterruptedError('服务停止，任务等待下次恢复')
        if self.store.job(identifier)['cancel_requested']:
            raise Cancelled('已停止后续处理；已提交的数据源请求不会撤销')

    def run(self):
        exports = threading.Thread(target=self.run_queue, args=('export',), daemon=True, name='pfor-export-worker')
        catalog = threading.Thread(target=self.run_queue, args=('catalog',), daemon=True, name='pfor-catalog-worker')
        if self.provider == 'qmt':
            exports.start()
        catalog.start()
        try:
            self.run_queue('download')
        finally:
            self.stop.set()
            if self.provider == 'qmt':
                exports.join()
            catalog.join()

    def run_queue(self, kind):
        error_field = {'download': 'last_error', 'export': 'export_error', 'catalog': 'catalog_error'}[kind]
        lock_name = {'download': '.source', 'export': '.exports', 'catalog': '.catalog'}[kind]
        if kind != 'export':
            lock_name += '.' + self.provider
        while not self.stop.is_set():
            try:
                with self.store.connect() as lease:
                    locked = lease.execute('SELECT pg_try_advisory_lock(hashtext(%s)) AS locked', (self.store.schema + lock_name,)).fetchone()['locked']
                    lease.commit()
                    if not locked:
                        self.stop.wait(2)
                        continue
                    self.leases.connection, self.leases.lost = lease, False
                    setattr(self, error_field, '')
                    self.store.query("UPDATE jobs SET state=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END WHERE state IN ('running','retrying') AND (kind=%s OR (%s='export' AND kind='verify')) AND (%s='export' OR coalesce(payload->>'source','qmt')=%s)", (kind,kind,kind,self.provider))
                    while not self.stop.is_set():
                        lease.execute('SELECT 1')
                        lease.commit()
                        if kind == 'download' and (self.last_schedule is None or (datetime.now(SHANGHAI) - self.last_schedule).total_seconds() >= 60):
                            self.schedule()
                            self.last_schedule = datetime.now(SHANGHAI)
                        job = self.store.query("SELECT * FROM jobs WHERE state='queued' AND (kind=%s OR (%s='export' AND kind='verify')) AND (%s='export' OR coalesce(payload->>'source','qmt')=%s) ORDER BY created_at LIMIT 1", (kind,kind,kind,self.provider), one=True)
                        if not job:
                            self.stop.wait(1)
                            continue
                        self.execute(job)
            except Exception as error:
                if isinstance(error,InterruptedError) and self.stop.is_set():
                    break
                detail = failure(error)
                message = detail['code'] + '：' + detail['message']
                if getattr(self,error_field) != message:
                    try:
                        fallback_log(self.runtime,self.provider,dict(detail,lane=kind))
                    except OSError:
                        pass
                setattr(self, error_field, message)
                if kind=='download':
                    self.schedule_issues = {}
                self.stop.wait(3)

    def execute(self, job):
        identifier = job['id']
        self.store.update_job(identifier, state='running', error=None, error_code=None, action=None, run_number=job.get('run_number',0)+1)
        self.store.event(identifier,'JOB_STARTED','开始执行任务',context={'kind':job['kind'],'checkpoint':job['checkpoint']})
        try:
            self.check(identifier)
            if job['kind'] == 'verify':
                result = self.verify(job)
            elif job['kind'] == 'catalog' and self.provider == 'tushare':
                result = self.sync_tushare(job)
            elif job['kind'] == 'catalog':
                from .catalog import synchronize
                result = synchronize(self, job)
            else:
                result = self.download(job) if job['kind'] == 'download' else self.export(job)
            self.check(identifier)
            state = result.pop('state', 'succeeded')
            if state == 'completed':
                state = 'succeeded'
            self.store.update_job(identifier, state=state, result=result)
        except Cancelled as error:
            self.store.update_job(identifier, state='cancelled', error=str(error))
        except InterruptedError:
            self.store.update_job(identifier, state='queued')
        except LeaseLost:
            raise
        except Exception as error:
            detail = failure(error)
            existing_rows = self.store.job(identifier)['result'].get('rows',0)
            self.store.update_job(identifier, state='partial' if existing_rows and job['kind']!='export' else detail['state'], error=detail['message'], error_code=detail['code'], action=detail['action'])
            self.store.event(identifier,detail['code'],detail['message'],'error',context={'action':detail['action']})
        finally:
            if not getattr(self.leases,'lost',False):
                current = self.store.job(identifier)
                self.store.event(identifier,'JOB_' + current['state'].upper(),'任务执行状态：' + current['state'],
                                 'error' if current['state'] in ('failed','blocked') else 'warning' if current['state']=='partial' else 'info',
                                 context={'checkpoint':current['checkpoint'],'rows':current['result'].get('rows',0)})
                self.publish({'event': 'job', 'data': job_summary(current)})

    def tushare_source(self, payload, check=None):
        try:
            account = self.account_resolver(payload.get('account_id'))
        except ValueError as error:
            raise SourceError(str(error), 'configuration') from None
        if account['endpoint'] != payload.get('endpoint'):
            raise SourceError('账号端点与任务固定端点不同，请恢复配置或新建任务', 'configuration')
        return TushareSource(account, check)

    def sync_tushare(self, job):
        identifier = job['id']
        adapter = self.tushare_source(job['payload'], lambda: self.check(identifier))
        parts = job['payload'].get('chunks') or [{'exchange':exchange,'contract_type':kind} for exchange in job['payload'].get('exchanges',TS_EXCHANGES) for kind in ('1','2')]
        payload = dict(job['payload'], chunks=parts)
        self.store.update_job(identifier, payload=payload)
        job = dict(job,payload=payload)
        def process(index, part):
            rows = self.retry_network(identifier, lambda: adapter.catalog(part['exchange'], part['contract_type']))
            self.check(identifier)
            with self.store.connect() as conn:
                for position, row in enumerate(rows):
                    if position % 100 == 0:
                        self.check(identifier)
                    self.store.save_security(**row, conn=conn, source='tushare')
                self.check(identifier)
                conn.execute("UPDATE jobs SET checkpoint=%s,attempts=0,result=result || jsonb_build_object('rows',(SELECT count(*) FROM securities WHERE source='tushare')),updated_at=now() WHERE id=%s", (index+1,identifier))
                self.store.write_unit(identifier,index,part,'succeeded',[],len(rows),conn=conn)
        self.process_parts(job,process)
        return self.download_result(identifier)

    def calendar(self, code, start, end, count=-1, adapter=None):
        if self.provider == 'qmt':
            return self.source.get_trading_dates(code, start, end, count) if count != -1 else self.source.get_trading_dates(code,start,end)
        final = datetime.strptime(end, '%Y%m%d').date()
        first = datetime.strptime(start, '%Y%m%d').date() if start else final - timedelta(days=60)
        dates = []
        while first <= final:
            last = min(first + timedelta(days=365), final)
            market = code if code in TS_EXCHANGES.values() else source_market(code,'tushare')
            exchange = next(key for key,value in TS_EXCHANGES.items() if value==market)
            records = adapter.calendar_records(exchange,first,last)
            from .futures import write_records
            with self.store.connect() as conn:
                adapter.check()
                write_records(conn,'calendar',records)
            dates.extend(row['day'] for row in records if row['is_open'])
            first = last + timedelta(days=1)
        return [date.strftime('%Y%m%d') for date in (dates[-count:] if count > 0 else dates)]

    def retry_network(self, identifier, operation):
        for attempt in range(4):
            self.check(identifier)
            try:
                result = operation()
                if attempt:
                    self.store.update_job(identifier, state='running')
                return result
            except Exception as error:
                if not network_error(error) or attempt == 3:
                    raise
                self.store.update_job(identifier, state='retrying', attempts=attempt + 1)
                self.store.event(identifier,'NETWORK_RETRY','网络请求等待重试','warning',context={'attempt':attempt+1,'delay_seconds':min(2**attempt,4)})
                if self.stop.wait(min(2 ** attempt, 4)):
                    raise InterruptedError()

    def process_parts(self, job, operation):
        identifier = job['id']
        blocked = {}
        for index, part in enumerate(job['payload']['chunks'][job['checkpoint']:], job['checkpoint']):
            self.check(identifier)
            key = part.get('period', job['payload'].get('resource','catalog'))
            if key in MINUTE_PERIODS:
                key = 'minutes'
            self.store.event(identifier,'UNIT_STARTED','开始读取分块',unit_index=index,context={'request':part})
            try:
                if key in blocked:
                    raise blocked[key]
                operation(index, part)
                self.store.update_job(identifier, state='running')
            except (Cancelled, InterruptedError, LeaseLost):
                raise
            except Exception as error:
                detail = failure(error)
                gaps = [issue(detail['code'],'rejected' if detail['state']=='failed' and detail['scope']=='unit' else 'pending_verification',detail['message'],detail['action'],detail['retryable'])]
                self.store.write_unit(identifier,index,part,detail['state'],gaps,error_code=detail['code'],retryable=detail['retryable'],
                                      advance=detail['scope']!='source',sample=getattr(error,'sample',None))
                self.store.update_job(identifier,state='running',error=detail['message'],error_code=detail['code'],action=detail['action'])
                if detail['scope'] == 'source':
                    break
                if detail['scope'] == 'interface':
                    blocked[key] = error
            self.publish({'event':'job','data':job_summary(self.store.job(identifier))})

    def download(self, job):
        if job['payload'].get('resource'):
            return self.download_report(job)
        payload, identifier = job['payload'], job['id']
        parts = payload['chunks']
        adapter = self.tushare_source(payload, lambda: self.check(identifier)) if self.provider == 'tushare' else None
        def process(index, part):
            self.check(identifier)
            code, period, start, end = part['code'], part['period'], day(part['start']), day(part['end'])
            part = dict(part, source=self.provider)
            derivative = 'future' if self.provider == 'tushare' else derivative_kind(code)
            security = self.store.query('SELECT kind,subtype,metadata FROM securities WHERE source=%s AND code=%s', (self.provider,code), one=True)
            if security and security['subtype'] == 'contract':
                metadata = security.get('metadata') or {}
                for field, is_start in [('listed',True),('expiry',False)]:
                    raw = str(metadata.get(field) or '')
                    if len(raw)==8 and raw.isdigit():
                        bound = timestamp(raw).date()
                        start, end = (max(start,bound),end) if is_start else (start,min(end,bound))
                if start > end:
                    self.store.write_chunk(identifier,part,[],[issue('OUTSIDE_LIFECYCLE','not_applicable','请求范围不在合约上市至退市区间','无需补数')],index+1)
                    return
            # Include the preceding session; only explicit terminal trading days assign night bars.
            read_start = start - timedelta(days=7) if derivative and period in MINUTE_PERIODS else start
            def read():
                result = self.source.download_history_data2([code], period, read_start.strftime('%Y%m%d'), end.strftime('%Y%m%d'))
                def rejected(value):
                    if value is False or isinstance(value, (int,float)) and value < 0:
                        return True
                    if isinstance(value, dict):
                        return any(rejected(item) for item in value.values())
                    return False
                if rejected(result):
                    raise ValueError('终端拒绝下载请求，未将旧缓存视为下载成功')
                for poll in range(6):
                    self.check(identifier)
                    data = self.source.get_local_data(stock_list=[code], period=period, start_time=read_start.strftime('%Y%m%d'), end_time=end.strftime('%Y%m%d') + '235959')
                    rows = normalize_bars((data or {}).get(code), code, period, start, end)
                    if rows:
                        return rows
                    if poll < 5:
                        self.stop.wait(1)
                return []
            try:
                rows = self.retry_network(identifier, lambda: adapter.history(code,period,start,end,security['subtype'] if security else 'contract')) if adapter else self.retry_network(identifier, read)
            except SourceError as error:
                if adapter and period in MINUTE_PERIODS and error.category == 'permission':
                    raise SourceError(str(error) + '；已入库数据保留，可只选日线回补；分钟授权后创建定向重试', error.category) from None
                raise
            self.check(identifier)
            calendar_error = None
            try:
                calendar_start = period_label(start,period)-timedelta(days=4) if period=='1w' else start.replace(day=1) if period=='1mo' else start
                calendar_end = period_label(end,period)
                calendar = self.retry_network(identifier, lambda: self.calendar(code,calendar_start.strftime('%Y%m%d'),calendar_end.strftime('%Y%m%d'),adapter=adapter))
            except SourceError as error:
                if not adapter or not rows:
                    raise
                calendar, calendar_error = [], str(error)
            if not rows and not calendar and not adapter:
                guidance = '终端行情服务器登录与连接' if self.provider == 'qmt' else 'Tushare连接与接口权限'
                raise SourceUnavailable(f'{code} / {period} / {start} 至 {end}：{self.provider.upper()} 历史行情和交易日历均为空，已停止后续下载；请检查{guidance}，恢复后重试。当前分块未推进检查点')
            trading_days = sorted({timestamp(value).date() for value in calendar})
            with self.store.connect() as conn:
                with conn.cursor() as cursor:
                    cursor.executemany('INSERT INTO trading_dates(market,day,source) VALUES(%s,%s,%s) ON CONFLICT(source,market,day) DO UPDATE SET is_open=true', [(source_market(code,self.provider), item,self.provider) for item in trading_days])
            actual = {row['trading_day'] or row['time'].date() for row in rows}
            expected = {period_label(value,period) for value in trading_days}
            gaps = [{'day': value.isoformat(), 'reason': '无行情，停牌或缺失待确认'} for value in sorted(expected) if value not in actual]
            if period in MINUTE_PERIODS and any(row['trading_day'] is None for row in rows):
                gaps = []
            if any(any(row[field] is None for field in ('open','high','low','close')) for row in rows):
                gaps.append(issue('OHLC_INCOMPLETE','pending_verification','部分行情OHLC字段为空，已保留NULL','核对上游字段与无成交时的返回口径'))
            if not rows and not trading_days:
                gaps = [issue('NO_TRADING_DAY','not_applicable','已确认区间无交易日','无需补数')]
            if period in AGGREGATE_PERIODS:
                dates=self.store.query('SELECT day,is_open FROM trading_dates WHERE source=%s AND market=%s AND day BETWEEN %s AND %s',
                                       (self.provider,source_market(code,self.provider),start-timedelta(days=31),period_label(end,period)))
                gaps.extend(aggregate_issues(rows,dates))
            if calendar_error:
                gaps.append({'reason':calendar_error})
            if not rows and (trading_days or not adapter):
                gaps.append({'range': [part['start'], part['end']], 'reason': '空表未确认为成功'})
            if not calendar and (rows or not adapter):
                gaps.append({'reason': '交易日历为空，覆盖情况未确认'})
            if period in MINUTE_PERIODS:
                gaps.extend(minute_issues(rows,period,security.get('metadata') if security else None))
            factor = None
            if not derivative and (not security or security['kind'] != 'bond'):
                try:
                    factor = encode_value(self.retry_network(identifier, lambda: self.source.get_divid_factors(code)))
                except (NotImplementedError, CfquantError) as error:
                    if not isinstance(error, NotImplementedError) and error.remote_type not in ('NotImplementedError', 'SignatureUnavailable'):
                        raise
                    gaps.append({'reason': '当前终端未提供复权因子'})
            self.check(identifier)
            if adapter and security and security['subtype'] == 'continuous':
                mappings = self.retry_network(identifier, lambda: adapter.mapping(code,start,end))
                self.check(identifier)
                with self.store.connect() as conn:
                    for mapping in mappings:
                        conn.execute('INSERT INTO contract_mappings(source,code,trading_day,member_code) VALUES(%s,%s,%s,%s) ON CONFLICT(source,code,trading_day) DO UPDATE SET member_code=EXCLUDED.member_code,updated_at=now()', (self.provider,code,mapping['trade_date'],mapping['mapping_ts_code']))
                missing = set(trading_days) - {r['trade_date'] for r in mappings}
                if missing:
                    gaps.append({'reason':'主力映射缺失，连续序列对应合约待核验','days':sorted(date.isoformat() for date in missing)})
            self.store.write_chunk(identifier, part, rows, gaps, index + 1, factor)
        self.process_parts(job, process)
        return self.download_result(identifier)

    def download_report(self, job):
        from .futures import REPORTS, write_records
        payload, identifier = job['payload'], job['id']
        resource = payload['resource']
        adapter = self.tushare_source(payload, lambda: self.check(identifier))
        def process(index, part):
            target = part.get('selection', payload)
            rows = self.retry_network(identifier, lambda: adapter.report(target,part['start'],part['end']))
            self.check(identifier)
            date_field = REPORTS[resource]['date']
            dates = sorted({row[date_field] for row in rows})
            gaps = [] if rows else [{'reason':'资料响应为空，未确认发布或覆盖情况'}]
            if resource != 'calendar':
                market = source_market(target['code'],'tushare') if resource=='mapping' else TS_EXCHANGES[target['exchange']]
                try:
                    calendar = [timestamp(value).date() for value in self.retry_network(identifier,lambda: self.calendar(market,day(part['start']).strftime('%Y%m%d'),day(part['end']).strftime('%Y%m%d'),adapter=adapter))]
                    gaps.extend({'day':date.isoformat(),'reason':'交易日无资料，待核验'} for date in calendar if date not in dates)
                except SourceError as error:
                    gaps.append({'reason':str(error)})
            self.check(identifier)
            with self.store.connect() as conn:
                write_records(conn,resource,rows)
                self.check(identifier)
                self.store.write_coverage(conn,identifier,part,len(rows),gaps,index+1,
                                          timestamp(dates[0]) if dates else None,timestamp(dates[-1]) if dates else None)
        self.process_parts(job, process)
        return self.download_result(identifier, report=True)

    def download_result(self, identifier, report=False):
        coverage = self.store.query('SELECT * FROM coverage WHERE job_id=%s ORDER BY code,period,requested_start', (identifier,))
        job = self.store.job(identifier)
        total = sum(item['row_count'] for item in coverage)
        if job['kind'] == 'catalog':
            total = self.store.query('SELECT coalesce(sum(row_count),0) AS n FROM job_units WHERE job_id=%s',(identifier,),one=True)['n']
        units = self.store.query('SELECT state,quality_state,count(*) AS count FROM job_units WHERE job_id=%s GROUP BY state,quality_state', (identifier,))
        uncertain = any(item['quality_state'] not in ('verified','not_applicable') or item['state']!='succeeded' for item in units)
        blocked = any(item['state']=='blocked' for item in units)
        failed = any(item['state']=='failed' for item in units)
        state = 'partial' if uncertain else 'succeeded'
        if not total and (failed or blocked):
            state = 'blocked' if blocked else 'failed'
        return {'state':state, 'rows':total, 'coverage':coverage, 'quality_summary':units, 'rule_version':RULE_VERSION,
                'message': ('未读到资料，未写入记录' if report else '未读到行情，未写入 K 线') if not total else '已入库，仍有未完成或待核验分块' if uncertain else '实际读回并完成入库'}

    def verify(self, job):
        from .futures import page, REPORTS, normalize_report
        identifier,payload=job['id'],job['payload']
        provider=payload.get('source','qmt')
        def process(index, part):
            self.check(identifier)
            resource=payload.get('resource')
            target=part.get('selection',payload)
            with self.store.connect() as conn:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                rows,offset=[],0
                while True:
                    self.check(identifier)
                    batch=page(self.store,dict(target,start=part['start'],end=part['end']),5000,offset,conn) if resource else self.store.history(part['code'],part['period'],part['start'],part['end'],5000,offset,conn,source=provider)
                    rows.extend(batch['rows'])
                    if batch['next_offset'] is None: break
                    offset=batch['next_offset']
                market=source_market(part['code'],provider) if not resource or resource=='mapping' else TS_EXCHANGES[target['exchange']]
                calendar=list(conn.execute('SELECT day,is_open FROM trading_dates WHERE source=%s AND market=%s AND day BETWEEN %s AND %s',(provider,market,day(part['start'])-timedelta(days=31),period_label(part['end'],part['period']))))
                if resource:
                    if resource in ('warehouse','holding'):
                        exchange='SHFE' if resource=='holding' and target['exchange']=='INE' else target['exchange']
                        normalize_report(resource,rows,exchange,target['symbol'],day(part['start']),day(part['end']))
                        gaps=[issue('REPORT_PUBLICATION_UNVERIFIED','pending_verification','已读到本地资料，但逐品种适用与发布规则未核验','核对该品种资料规则；空日期不自动当缺失')]
                    elif resource=='calendar':
                        dates={row['day'] for row in rows}
                        gaps=[issue('CALENDAR_MISSING','missing','日历缺少自然日记录','同步对应交易所日历',True,day=(day(part['start'])+timedelta(days=i)).isoformat()) for i in range((day(part['end'])-day(part['start'])).days+1) if day(part['start'])+timedelta(days=i) not in dates]
                    else:
                        mapped=[dict(time=timestamp(row['trading_day']),trading_day=row['trading_day']) for row in rows]
                        gaps=stored_issues(mapped,dict(part,period='1d'),calendar)
                    date_field=REPORTS[resource]['date']
                    times=[timestamp(row[date_field]) for row in rows]
                else:
                    normalize_bars(rows,part['code'],part['period'],period_label(part['start'],part['period']),period_label(part['end'],part['period']),provider)
                    security=conn.execute('SELECT metadata,subtype FROM securities WHERE source=%s AND code=%s',(provider,part['code'])).fetchone()
                    metadata=security['metadata'] if security else {}
                    checked=dict(part)
                    if security and security['subtype']=='contract':
                        if metadata.get('listed'): checked['start']=max(day(part['start']),timestamp(metadata['listed']).date()).isoformat()
                        if metadata.get('expiry'): checked['end']=min(day(part['end']),timestamp(metadata['expiry']).date()).isoformat()
                    gaps=[issue('OUTSIDE_LIFECYCLE','not_applicable','区间在合约存续期之外','无需补数')] if day(checked['start'])>day(checked['end']) else stored_issues(rows,checked,calendar,metadata)
                    if any(any(row[field] is None for field in ('open','high','low','close')) for row in rows):
                        gaps.append(issue('OHLC_INCOMPLETE','pending_verification','本地行情部分OHLC字段为空，未补零','核对上游无成交时的字段口径'))
                    times=[row['time'] for row in rows]
                self.check(identifier)
                self.store.write_coverage(conn,identifier,part,len(rows),gaps,index+1,min(times) if times else None,max(times) if times else None)
                self.store.event(identifier,'VERIFICATION_READ','只读核验已保存数据，未请求数据源或改写行情',unit_index=index,context={'rows':len(rows),'rule_version':RULE_VERSION,'issues':gaps},conn=conn)
        self.process_parts(job,process)
        result=self.download_result(identifier)
        result['message']='只读核验结束；原任务与行情未改写'
        return result

    def schedule(self, now=None):
        now = now or datetime.now(SHANGHAI)
        from .maintenance import tick, schedule_scope
        self.last_error = ''
        self.schedule_issues = {}
        tick(self,now)
        if self.provider=='qmt' and (self.last_cleanup is None or (now-self.last_cleanup).total_seconds() >= 86400):
            self.store.housekeeping(self.options.get('event_retention_days',90),self.options.get('sample_retention_days',30))
            self.last_cleanup = now
        for dataset in self.store.query('SELECT * FROM datasets WHERE scheduled=true AND source=%s', (self.provider,)):
            schedule_scope(self,dataset,'dataset',now)

    def export(self, job):
        import pyarrow as pa
        import pyarrow.parquet as pq
        identifier, payload = job['id'], job['payload']
        folder = self.runtime / 'exports'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (str(identifier) + '.' + payload['format'])
        temp = target.with_suffix(target.suffix + '.partial')
        fields = ['code', 'period', 'time', *FIELDS, 'trading_day', 'source']
        if payload.get('period') in AGGREGATE_PERIODS:
            fields.extend(['as_of_date','source_fields'])
        if payload.get('resource'):
            from .futures import REPORTS, page as report_page
            fields = list(REPORTS[payload['resource']]['fields'])
        total = 0
        parquet = None
        metadata = None
        self.store.update_job(identifier, checkpoint=0)
        # A repeatable snapshot keeps every export page internally consistent.
        try:
            with self.store.connect() as conn, temp.open('w', encoding='utf-8-sig', newline='') if payload['format'] == 'csv' else temp.open('wb') as stream:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                writer = csv.DictWriter(stream, fieldnames=fields) if payload['format'] == 'csv' else None
                if writer:
                    writer.writeheader()
                for code in payload.get('members', ['']):
                    offset = 0
                    while True:
                        self.check(identifier)
                        page = report_page(self.store,payload,5000,offset,conn) if payload.get('resource') else self.store.history(code, payload['period'], payload['start'], payload['end'], 5000, offset, conn, source=payload.get('source','qmt'))
                        rows = page.pop('rows')
                        metadata = page
                        if not rows:
                            break
                        for row in rows:
                            if 'source_fields' in row:
                                row['source_fields'] = json.dumps(row['source_fields'],ensure_ascii=False,default=json_default)
                        if writer:
                            writer.writerows(rows)
                        else:
                            # Decimal text preserves arbitrary PostgreSQL NUMERIC without rounding.
                            clean = [{key: value.isoformat() if hasattr(value,'isoformat') else str(value) if value is not None else None for key,value in row.items()} for row in rows]
                            schema = pa.schema([(key, pa.string()) for key in fields])
                            table = pa.Table.from_pylist(clean, schema=schema)
                            if parquet is None:
                                parquet = pq.ParquetWriter(stream, schema)
                            parquet.write_table(table)
                        total += len(rows)
                        offset += len(rows)
                        self.store.update_job(identifier, checkpoint=total)
                        if page['next_offset'] is None:
                            break
                if payload['format'] == 'parquet' and parquet is None:
                    parquet = pq.ParquetWriter(stream, pa.schema([(key, pa.string()) for key in fields]))
                if parquet:
                    parquet.close()
                    parquet = None
            self.check(identifier)
            temp.replace(target)
            note = dict(metadata or {}, rows=total, format=payload['format'], query=payload, nulls='CSV 空字段 / Parquet null',
                        numeric='PostgreSQL NUMERIC 按十进制文本导出，Parquet 数值列为 string，避免精度截断',
                        source='postgresql', adjustment='none')
            target.with_suffix(target.suffix + '.json').write_text(json.dumps(note, ensure_ascii=False, indent=2, default=json_default), 'utf-8')
            return {'rows': total, 'file': target.name, 'metadata': target.name + '.json'}
        finally:
            if parquet:
                parquet.close()
            temp.unlink(missing_ok=True)
