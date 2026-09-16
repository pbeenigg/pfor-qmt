import csv
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import xtdata
from .client import CfquantError, CfquantTimeout
from .data import SHANGHAI, FIELDS, chunks, day, timestamp, normalize_bars, json_default
from .protocol import encode_value


class Cancelled(Exception):
    pass


def network_error(error):
    if isinstance(error, (ConnectionError, TimeoutError, OSError, CfquantTimeout)):
        return True
    if isinstance(error, CfquantError):
        return error.remote_type in ('ConnectionError', 'TimeoutError', 'OSError') or any(
            phrase in str(error).lower() for phrase in ('pipe bridge not connected', 'pipe bridge response timeout', 'pipe disconnected', 'pipe connection closed', 'pipe receive connection closed', 'pipe client closed'))
    return False


class Worker:
    def __init__(self, store, runtime, publish=None, source=None):
        self.store, self.runtime = store, Path(runtime)
        self.source = source or xtdata
        self.publish = publish or (lambda event: None)
        self.stop = threading.Event()
        self.thread = None
        self.last_error = ''
        self.last_schedule = None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='pfor-market-worker')
        self.thread.start()

    def check(self, identifier):
        if self.stop.is_set():
            raise InterruptedError('服务停止，任务等待下次恢复')
        if self.store.job(identifier)['cancel_requested']:
            raise Cancelled('已停止后续处理；已提交的 QMT 请求不会撤销')

    def run(self):
        while not self.stop.is_set():
            try:
                with self.store.connect() as lease:
                    locked = lease.execute('SELECT pg_try_advisory_lock(hashtext(%s)) AS locked', (self.store.schema + '.source',)).fetchone()['locked']
                    lease.commit()
                    if not locked:
                        self.stop.wait(2)
                        continue
                    self.last_error = ''
                    self.store.query("UPDATE jobs SET state=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END WHERE state='running'")
                    while not self.stop.is_set():
                        lease.execute('SELECT 1')
                        lease.commit()
                        if self.last_schedule is None or (datetime.now(SHANGHAI) - self.last_schedule).total_seconds() >= 60:
                            self.schedule()
                            self.last_schedule = datetime.now(SHANGHAI)
                        job = self.store.query("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1", one=True)
                        if not job:
                            self.stop.wait(1)
                            continue
                        self.execute(job)
            except Exception:
                self.last_error = '数据库连接或初始化尚未就绪，后台任务等待恢复'
                self.stop.wait(3)

    def execute(self, job):
        identifier = job['id']
        self.store.update_job(identifier, state='running', error=None)
        try:
            self.check(identifier)
            result = self.download(job) if job['kind'] == 'download' else self.export(job)
            self.check(identifier)
            state = result.pop('state', 'completed')
            self.store.update_job(identifier, state=state, result=result)
        except Cancelled as error:
            self.store.update_job(identifier, state='cancelled', error=str(error))
        except InterruptedError:
            self.store.update_job(identifier, state='queued')
        except Exception as error:
            # Drivers can embed connection strings or credentials in messages.
            message = str(error) if isinstance(error, (ValueError, NotImplementedError, CfquantError)) else type(error).__name__ + ': 任务失败，请检查数据源与连接'
            self.store.update_job(identifier, state='failed', error=message[:800])
        finally:
            self.publish({'event': 'job', 'data': self.store.job(identifier)})

    def retry_network(self, identifier, operation):
        for attempt in range(4):
            self.check(identifier)
            try:
                return operation()
            except Exception as error:
                if not network_error(error) or attempt == 3:
                    raise
                self.store.update_job(identifier, attempts=attempt + 1)
                if self.stop.wait(min(2 ** attempt, 4)):
                    raise InterruptedError()

    def download(self, job):
        payload, identifier = job['payload'], job['id']
        parts = payload['chunks']
        for index, part in enumerate(parts[job['checkpoint']:], start=job['checkpoint']):
            self.check(identifier)
            code, period, start, end = part['code'], part['period'], day(part['start']), day(part['end'])
            def read():
                result = self.source.download_history_data2([code], period, start.strftime('%Y%m%d'), end.strftime('%Y%m%d'))
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
                    data = self.source.get_local_data(stock_list=[code], period=period, start_time=start.strftime('%Y%m%d'), end_time=end.strftime('%Y%m%d') + '235959')
                    rows = normalize_bars((data or {}).get(code), code, period, start, end)
                    if rows:
                        return rows
                    if poll < 5:
                        self.stop.wait(1)
                return []
            rows = self.retry_network(identifier, read)
            self.check(identifier)
            calendar = self.retry_network(identifier, lambda: self.source.get_trading_dates(code, start.strftime('%Y%m%d'), end.strftime('%Y%m%d')))
            trading_days = sorted({timestamp(value).date() for value in calendar})
            with self.store.connect() as conn:
                with conn.cursor() as cursor:
                    cursor.executemany('INSERT INTO trading_dates(market,day) VALUES(%s,%s) ON CONFLICT DO NOTHING', [(code[-2:], item) for item in trading_days])
            actual = {row['time'].date() for row in rows}
            gaps = [{'day': value.isoformat(), 'reason': '无行情，停牌或缺失待确认'} for value in trading_days if value not in actual]
            if not rows:
                gaps.append({'range': [part['start'], part['end']], 'reason': '空表未确认为成功'})
            if not calendar:
                gaps.append({'reason': '交易日历为空，覆盖情况未确认'})
            if period != '1d':
                gaps.append({'reason': '分钟内完整性需按终端交易时段复核，未伪造缺失分钟'})
            factor = None
            try:
                factor = encode_value(self.retry_network(identifier, lambda: self.source.get_divid_factors(code)))
            except (NotImplementedError, CfquantError) as error:
                if not isinstance(error, NotImplementedError) and error.remote_type not in ('NotImplementedError', 'SignatureUnavailable'):
                    raise
                gaps.append({'reason': '当前终端未提供复权因子'})
            self.check(identifier)
            self.store.write_chunk(identifier, part, rows, gaps, index + 1, factor)
            self.publish({'event': 'job', 'data': self.store.job(identifier)})
        coverage = self.store.query('SELECT * FROM coverage WHERE job_id=%s ORDER BY code,period,requested_start', (identifier,))
        total = sum(item['row_count'] for item in coverage)
        uncertain = any(item['gaps'] for item in coverage)
        return {'state': 'partial' if uncertain else 'completed', 'rows': total, 'coverage': coverage,
                'message': '已入库，存在未确认缺口' if uncertain else '实际读回并完成入库'}

    def schedule(self, now=None):
        now = now or datetime.now(SHANGHAI)
        cutoff = now.date() if now.hour >= 17 else now.date() - timedelta(days=1)
        datasets = self.store.query('SELECT * FROM datasets WHERE scheduled=true')
        for dataset in datasets:
            # Refresh an authoritative calendar; no weekday-based holiday guessing.
            try:
                calendar = self.source.get_trading_dates('000001.SH', dataset['schedule_from'].strftime('%Y%m%d'), cutoff.strftime('%Y%m%d'))
                dates = sorted({timestamp(value).date() for value in calendar if dataset['schedule_from'] <= timestamp(value).date() <= cutoff})
                with self.store.connect() as conn:
                    with conn.cursor() as cursor:
                        cursor.executemany('INSERT INTO trading_dates(market,day) VALUES(%s,%s) ON CONFLICT DO NOTHING', [('SH', item) for item in dates])
            except Exception:
                dates = [row['day'] for row in self.store.query("SELECT day FROM trading_dates WHERE market='SH' AND day BETWEEN %s AND %s ORDER BY day", (dataset['schedule_from'], cutoff))]
            for date in dates:
                key = str(dataset['id']) + ':' + date.isoformat()
                if self.store.query('SELECT id FROM jobs WHERE schedule_key=%s', (key,), one=True):
                    continue
                try:
                    previous = self.source.get_trading_dates('000001.SH', '', date.strftime('%Y%m%d'), 5)
                    days = sorted(timestamp(value).date() for value in previous)
                    if len(days) < 5:
                        continue
                    payload = {'dataset_id': str(dataset['id']), 'members': dataset['members'], 'periods': dataset['periods'],
                               'start': days[-5].isoformat(), 'end': date.isoformat()}
                    payload['chunks'] = chunks(payload)
                    self.store.create_job('download', payload, key)
                except Exception:
                    self.last_error = '交易日历未就绪，调度等待重试'

    def export(self, job):
        import pyarrow as pa
        import pyarrow.parquet as pq
        identifier, payload = job['id'], job['payload']
        folder = self.runtime / 'exports'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (str(identifier) + '.' + payload['format'])
        temp = target.with_suffix(target.suffix + '.partial')
        fields = ['code', 'period', 'time', *FIELDS, 'source']
        total = 0
        parquet = None
        metadata = None
        # A repeatable snapshot keeps every export page internally consistent.
        try:
            with self.store.connect() as conn, temp.open('w', encoding='utf-8-sig', newline='') if payload['format'] == 'csv' else temp.open('wb') as stream:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                writer = csv.DictWriter(stream, fieldnames=fields) if payload['format'] == 'csv' else None
                if writer:
                    writer.writeheader()
                for code in payload['members']:
                    offset = 0
                    while True:
                        self.check(identifier)
                        page = self.store.history(code, payload['period'], payload['start'], payload['end'], 5000, offset, conn)
                        rows = page.pop('rows')
                        metadata = page
                        if not rows:
                            break
                        if writer:
                            writer.writerows(rows)
                        else:
                            # Decimal text preserves arbitrary PostgreSQL NUMERIC without rounding.
                            clean = [{key: json_default(value) if value is not None and key in (*FIELDS, 'time') else value for key, value in row.items()} for row in rows]
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
