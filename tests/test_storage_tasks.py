import csv
import json
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pyarrow.parquet as pq
import pytest

from pfor_qmt.data import SHANGHAI, chunks, normalize_bars, day
from pfor_qmt.storage import Store, document
from pfor_qmt.tasks import Worker, network_error
from pfor_qmt.client import CfquantError


class Source:
    def __init__(self):
        self.downloads = []
        self.factor = {'20260914': {'factor':1.25}}
        self.fail = None
        self.on_download = lambda: None
        self.empty = False

    def download_history_data2(self, codes, period, start, end):
        self.downloads.append((codes,period,start,end))
        self.on_download()
        if self.fail:
            raise self.fail

    def get_local_data(self, **params):
        if self.empty:
            return {}
        return {params['stock_list'][0]: pd.DataFrame([{'time':20260914000000,'open':'123.1234567890123456789','high':'125','low':'120','close':'124.1234567890123456789','volume':'1152921504606847013','amount':None}])}

    def get_trading_dates(self, code, start='', end='', count=-1, period='1d'):
        dates = ['20260908','20260909','20260910','20260911','20260914']
        if count == 5:
            return dates
        return [value for value in dates if (not start or value >= start) and (not end or value <= end)]

    def get_divid_factors(self, code):
        return self.factor


def make_job(store, members=None):
    p = {'members':members or ['000300.SH'],'periods':['1d'],'start':'2026-09-14','end':'2026-09-14'}
    p['chunks'] = chunks(p)
    return store.create_job('download',p)


def test_schema_boundary():
    for name in ['public','cfquant','pfor_qmt;drop schema public cascade','pfor_qmt_test_a-b']:
        with pytest.raises(ValueError):
            Store('',name)


def test_bar_precision_timezone_and_null():
    rows = normalize_bars(pd.DataFrame([{'time':20260914,'open':None,'high':5,'low':2,'close':'3.12345678901234567890','volume':2**60+37}]),'000300.SH','1d',day('2026-09-14'),day('2026-09-14'))
    assert rows[0]['close'] == Decimal('3.12345678901234567890')
    assert rows[0]['volume'] == Decimal(2**60+37)
    assert rows[0]['open'] is None and rows[0]['amount'] is None
    assert rows[0]['time'].utcoffset().total_seconds() == 28800


@pytest.mark.parametrize('error,expected', [(ConnectionError(),True),(TimeoutError(),True),(ValueError(),False),(NotImplementedError(),False),(CfquantError('unavailable',remote_type='NotImplementedError'),False)])
def test_retry_classification(error,expected):
    assert network_error(error) is expected


@pytest.mark.postgres
def test_migrate_idempotent_and_upsert_precision(store,tmp_path):
    store.migrate()
    source = Source()
    worker = Worker(store,tmp_path,source=source)
    first, second = make_job(store), make_job(store)
    worker.execute(first)
    worker.execute(second)
    assert store.job(first['id'])['state'] == 'succeeded'
    rows = store.history('000300.SH')['rows']
    assert len(rows) == 1 and rows[0]['volume'] == Decimal('1152921504606847013')
    assert rows[0]['close'] == Decimal('124.1234567890123456789')
    assert rows[0]['amount'] is None
    assert store.query('SELECT raw FROM factors',one=True)['raw'] == source.factor
    changed = dict(rows[0],close=Decimal('125'))
    store.write_chunk(first['id'],first['payload']['chunks'][0],[changed],[],1)
    assert store.history('000300.SH')['rows'][0]['close'] == 125


@pytest.mark.postgres
def test_cancel_after_terminal_request_prevents_write(store,tmp_path):
    job = make_job(store,['000300.SH','000001.SZ'])
    source = Source()
    source.on_download = lambda: store.update_job(job['id'],cancel_requested=True)
    Worker(store,tmp_path,source=source).execute(job)
    assert store.job(job['id'])['state'] == 'cancelled'
    assert len(source.downloads) == 1 and store.history('000300.SH')['rows'] == []


@pytest.mark.postgres
def test_resume_does_not_repeat_committed_chunk(store,tmp_path):
    job = make_job(store,['000300.SH','000001.SZ'])
    part = job['payload']['chunks'][0]
    rows = normalize_bars(Source().get_local_data(stock_list=['000300.SH'])['000300.SH'],'000300.SH','1d',day(part['start']),day(part['end']))
    store.write_chunk(job['id'],part,rows,[],1)
    store.update_job(job['id'],state='running')
    source = Source()
    Worker(store,tmp_path,source=source).execute(store.job(job['id']))
    assert [item[0][0] for item in source.downloads] == ['000001.SZ']
    assert store.job(job['id'])['checkpoint'] == 2


@pytest.mark.postgres
def test_empty_table_partial_and_missing_capability_not_retried(store,tmp_path):
    source = Source()
    source.empty = True
    worker = Worker(store,tmp_path,source=source)
    worker.stop.wait = lambda seconds: False
    job = make_job(store)
    worker.execute(job)
    result = store.job(job['id'])
    assert result['state'] == 'partial' and result['result']['rows'] == 0
    assert result['result']['message'] == '未读到行情，未写入 K 线'
    source.fail = NotImplementedError('Capability absent')
    job2 = make_job(store)
    worker.execute(job2)
    assert store.job(job2['id'])['state'] == 'blocked'
    assert len(source.downloads) == 2


@pytest.mark.postgres
def test_network_failure_retried_three_times(store,tmp_path):
    source = Source()
    source.fail = ConnectionError('offline')
    worker = Worker(store,tmp_path,source=source)
    worker.stop.wait = lambda seconds: False
    job = make_job(store)
    worker.execute(job)
    assert len(source.downloads) == 4
    assert store.job(job['id'])['state'] == 'failed'


@pytest.mark.postgres
def test_empty_history_and_calendar_stop_without_advancing_checkpoint(store,tmp_path):
    source = Source()
    source.empty = True
    source.get_trading_dates = lambda *args: []
    worker = Worker(store,tmp_path,source=source)
    worker.stop.wait = lambda seconds: False
    job = make_job(store,['000300.SH','000001.SZ'])
    worker.execute(job)
    result = store.job(job['id'])
    assert result['state'] == 'blocked' and result['checkpoint'] == 0
    assert '行情服务器' in result['error']
    assert len(source.downloads) == 1
    assert not store.query('SELECT * FROM bars')
    assert not store.query('SELECT * FROM coverage')
    # Retrying after recovery includes the failed chunk, not just later symbols.
    source.empty = False
    source.get_trading_dates = Source().get_trading_dates
    worker.execute(result)
    assert store.job(job['id'])['state'] == 'succeeded'
    assert len(store.query('SELECT * FROM bars')) == 2


@pytest.mark.postgres
def test_empty_calendar_is_visible_and_schedule_recovers(store,tmp_path):
    dataset = store.create_dataset({'name':'calendar-test','members':['000300.SH'],'scheduled':True})
    store.query("UPDATE datasets SET schedule_from='2026-09-14' WHERE id=%s",(dataset['id'],))
    source = Source()
    source.get_trading_dates = lambda *args: []
    worker = Worker(store,tmp_path,source=source)
    worker.schedule(datetime(2026,9,14,17,tzinfo=SHANGHAI))
    assert '交易日历为空' in worker.last_error
    assert not store.query('SELECT * FROM jobs')
    source.get_trading_dates = Source().get_trading_dates
    worker.schedule(datetime(2026,9,14,17,tzinfo=SHANGHAI))
    worker.schedule(datetime(2026,9,14,17,tzinfo=SHANGHAI))
    assert worker.last_error == ''
    assert len(store.query('SELECT * FROM jobs')) == 1


@pytest.mark.postgres
def test_failed_and_short_calendar_expose_schedule_reason(store,tmp_path):
    dataset = store.create_dataset({'name':'calendar-errors','members':['000300.SH'],'scheduled':True})
    store.query("UPDATE datasets SET schedule_from='2026-09-14' WHERE id=%s",(dataset['id'],))
    source = Source()
    def disconnected(*args):
        raise ConnectionError('private details')
    source.get_trading_dates = disconnected
    worker = Worker(store,tmp_path,source=source)
    worker.schedule(datetime(2026,9,14,17,tzinfo=SHANGHAI))
    assert '交易日历请求失败' in worker.last_error
    assert 'private' not in worker.last_error
    source.get_trading_dates = lambda *args: ['20260914']
    worker.schedule(datetime(2026,9,14,17,tzinfo=SHANGHAI))
    assert '最近五个交易日' in worker.last_error
    assert not store.query('SELECT * FROM jobs')


@pytest.mark.postgres
def test_rejected_download_cannot_succeed_from_existing_cache(store,tmp_path):
    source = Source()
    source.download_history_data2 = lambda *args: {'000300.SH':-1}
    job = make_job(store)
    Worker(store,tmp_path,source=source).execute(job)
    assert store.job(job['id'])['state'] == 'failed'
    assert store.history('000300.SH')['rows'] == []


@pytest.mark.postgres
def test_schedule_cutoff_and_deduplication(store,tmp_path):
    dataset = store.create_dataset({'name':'test-index','members':['000300.SH'],'periods':['1d'],'scheduled':True})
    store.query("UPDATE datasets SET schedule_from='2026-09-14' WHERE id=%s",(dataset['id'],))
    worker = Worker(store,tmp_path,source=Source())
    worker.schedule(datetime(2026,9,14,16,59,tzinfo=SHANGHAI))
    assert not store.query('SELECT * FROM jobs')
    worker.schedule(datetime(2026,9,14,17,0,tzinfo=SHANGHAI))
    worker.schedule(datetime(2026,9,16,17,0,tzinfo=SHANGHAI))
    jobs = store.query('SELECT * FROM jobs')
    assert len(jobs) == 1
    assert jobs[0]['payload']['start'] == '2026-09-08'


@pytest.mark.postgres
def test_index_dataset_keeps_snapshot_and_scheduled_job_keeps_members(store):
    snapshot = store.snapshot('000300.SH','test-sector','test-index',['000001.SZ'])
    dataset = store.create_dataset({'name':'index-members','index_code':'000300.SH','snapshot_id':snapshot['id'],'members':snapshot['members']})
    store.snapshot('000300.SH','test-sector','test-index',['000002.SZ'])
    assert store.query('SELECT members FROM datasets WHERE id=%s',(dataset['id'],),one=True)['members'] == ['000001.SZ']


@pytest.mark.postgres
def test_csv_parquet_roundtrip_without_qmt(store,tmp_path):
    worker = Worker(store,tmp_path,source=Source())
    worker.execute(make_job(store))
    worker.source = None
    for format in ['csv','parquet']:
        job = store.create_job('export',{'members':['000300.SH'],'period':'1d','start':'2026-09-14','end':'2026-09-14','format':format})
        worker.execute(job)
        result = store.job(job['id'])
        assert result['state'] == 'succeeded', result
        path = tmp_path / 'exports' / result['result']['file']
        if format == 'csv':
            assert path.read_bytes().startswith(b'\xef\xbb\xbf')
            with path.open(encoding='utf-8-sig',newline='') as stream:
                rows = list(csv.DictReader(stream))
            assert rows[0]['amount'] == ''
        else:
            rows = pq.read_table(path).to_pylist()
            assert rows[0]['amount'] is None
        assert rows[0]['close'] == '124.1234567890123456789'
        assert rows[0]['volume'] == '1152921504606847013'
        assert json.loads(path.with_suffix(path.suffix + '.json').read_text('utf-8'))['adjustment'] == 'none'


@pytest.mark.postgres
@pytest.mark.parametrize('blocked', ['download', 'calendar'])
def test_exports_complete_while_qmt_is_blocked(store,tmp_path,blocked):
    source = Source()
    worker = Worker(store,tmp_path,source=source)
    worker.execute(make_job(store))
    entered, release = threading.Event(), threading.Event()
    def wait_for_terminal(*args):
        entered.set()
        assert release.wait(15), 'Test terminal was not released'
        return []
    if blocked == 'calendar':
        store.create_dataset({'name':'scheduled','members':['000300.SH'],'scheduled':True})
        store.query("UPDATE datasets SET schedule_from='2026-09-14'")
        source.get_trading_dates = wait_for_terminal
    else:
        source.on_download = wait_for_terminal
    pending = make_job(store)
    worker.start()
    try:
        assert entered.wait(5)
        exports = [store.create_job('export',dict(members=['000300.SH'],period='1d',start='2026-09-14',end='2026-09-14',format=format)) for format in ('csv','parquet')]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            jobs = [store.job(job['id']) for job in exports]
            if all(job['state'] == 'succeeded' for job in jobs):
                break
            time.sleep(.05)
        assert [job['state'] for job in jobs] == ['succeeded','succeeded']
        assert [job['result']['rows'] for job in jobs] == [1,1]
        assert store.job(pending['id'])['state'] == ('running' if blocked == 'download' else 'queued')
        assert store.history('000300.SH')['rows'][0]['close'] == Decimal('124.1234567890123456789')
    finally:
        worker.stop.set()
        release.set()
        worker.thread.join(timeout=5)
        assert not worker.thread.is_alive()


@pytest.mark.postgres
def test_export_recovery_does_not_reset_downloads(store,tmp_path):
    download = make_job(store)
    store.update_job(download['id'],state='running')
    payload = dict(members=['000300.SH'],period='1d',start='2026-09-14',end='2026-09-14',format='csv')
    export = store.create_job('export',payload)
    store.update_job(export['id'],state='running',checkpoint=123)
    cancelled = store.create_job('export',payload)
    store.update_job(cancelled['id'],state='running',cancel_requested=True)
    worker = Worker(store,tmp_path)
    worker.publish = lambda event: worker.stop.set()
    worker.run_queue('export')
    assert store.job(download['id'])['state'] == 'running'
    assert store.job(export['id'])['state'] == 'succeeded'
    assert store.job(export['id'])['result']['rows'] == 0
    assert store.job(export['id'])['checkpoint'] == 0
    assert store.job(cancelled['id'])['state'] == 'cancelled'


@pytest.mark.postgres
@pytest.mark.parametrize('lane', ['source','exports'])
def test_advisory_lock_excludes_second_worker(store,lane):
    with store.connect() as first, store.connect() as second:
        key = store.schema + '.' + lane
        assert first.execute('SELECT pg_try_advisory_lock(hashtext(%s)) AS locked',(key,)).fetchone()['locked']
        assert not second.execute('SELECT pg_try_advisory_lock(hashtext(%s)) AS locked',(key,)).fetchone()['locked']


@pytest.mark.postgres
def test_large_export_crosses_page_boundary_without_loss(store,tmp_path):
    job = make_job(store)
    start = datetime(2026,9,1,tzinfo=SHANGHAI)
    rows = [dict(code='000300.SH',period='1m',time=start+timedelta(minutes=index),open=Decimal('1'),high=Decimal('2'),low=Decimal('1'),close=Decimal('2'),volume=Decimal(index),amount=None) for index in range(5002)]
    store.write_chunk(job['id'],{'code':'000300.SH','period':'1m','start':'2026-09-01','end':'2026-09-10'},rows,[],1)
    page = store.history('000300.SH','1m','2026-09-01','2026-09-10',5000)
    assert len(page['rows']) == 5000 and page['next_offset'] == 5000
    for format in ('csv','parquet'):
        export = store.create_job('export',{'members':['000300.SH'],'period':'1m','start':'2026-09-01','end':'2026-09-10','format':format})
        Worker(store,tmp_path).execute(export)
        result = store.job(export['id'])
        assert result['state'] == 'succeeded',result
        path = tmp_path / 'exports' / result['result']['file']
        if format == 'csv':
            with path.open(encoding='utf-8-sig',newline='') as stream:
                output = list(csv.DictReader(stream))
        else:
            output = pq.read_table(path).to_pylist()
        assert len(output) == 5002 and output[-1]['volume'] == '5001'
