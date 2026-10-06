import pytest

from pfor_qmt.tasks import Worker, Cancelled
from test_storage_tasks import Source, make_job


@pytest.mark.postgres
@pytest.mark.parametrize('next_code,next_period,next_start,next_end', [
    ('APL0.ZF', '1m', '2025-09-29', '2025-10-05'),
    ('APL0.ZF', '1d', '2025-09-22', '2025-09-28'),
    ('AP00.ZF', '1m', '2025-09-22', '2025-09-28'),
])
def test_empty_qmt_chunk_preserves_issue_and_continues(store, tmp_path, next_code, next_period, next_start, next_end):
    class EmptyFirstSource(Source):
        def get_local_data(self, **params):
            if len(self.downloads) == 1:
                return {}
            code = params['stock_list'][0]
            date = '20250929' if next_start == '2025-09-29' else '20250922'
            return {code: [dict(time=date + '090100' if params['period'] == '1m' else date, open=1, high=2, low=1, close=2)]}

        def get_trading_dates(self, *args):
            return [] if len(self.downloads) == 1 else ['20250929' if next_start == '2025-09-29' else '20250922']

    source = EmptyFirstSource()
    worker = Worker(store, tmp_path, source=source)
    worker.stop.wait = lambda _: False
    parts = [
        dict(code='APL0.ZF', period='1m', start='2025-09-22', end='2025-09-28'),
        dict(code=next_code, period=next_period, start=next_start, end=next_end),
    ]
    job = store.create_job('download', dict(source='qmt', members=list(dict.fromkeys(p['code'] for p in parts)),
                                           periods=list(dict.fromkeys(p['period'] for p in parts)), chunks=parts))
    worker.execute(job)
    saved = store.job(job['id'])
    units = store.units_page(job['id'])['rows']
    assert saved['state'] == 'partial' and saved['checkpoint'] == 2 and saved['result']['rows'] == 1
    assert len(source.downloads) == 2
    assert [unit['state'] for unit in units] == ['blocked', 'succeeded']
    assert units[0]['quality_state'] == 'pending_verification' and units[0]['row_count'] == 0
    assert units[0]['error_code'] == 'QMT_HISTORY_EMPTY' and units[0]['retryable']
    assert '不能仅据空结果判定连接故障' in units[0]['issues'][0]['reason']
    assert len(store.query('SELECT * FROM bars')) == 1
    assert len(store.query('SELECT * FROM trading_dates')) == 1
    retry = store.retry_job(job['id'])
    assert retry['parent_id'] == job['id'] and retry['payload']['chunks'] == parts[:1]
    assert store.job(job['id'])['state'] == saved['state']
    assert store.units_page(job['id'])['rows'] == units


@pytest.mark.postgres
def test_empty_qmt_chunk_checkpoint_resumes_later_parts_and_remains_retryable(store, tmp_path):
    source = Source()
    source.empty = True
    source.get_trading_dates = lambda *args: []
    worker = Worker(store, tmp_path, source=source)
    worker.stop.wait = lambda _: False
    source.on_download = lambda: worker.stop.set() if len(source.downloads) == 2 else None
    job = make_job(store, ['000300.SH', '000001.SZ'])
    worker.execute(job)
    saved = store.job(job['id'])
    assert saved['state'] == 'queued' and saved['checkpoint'] == 1
    assert store.units_page(job['id'])['rows'][0]['quality_state'] == 'pending_verification'
    source.empty = False
    source.get_trading_dates = Source().get_trading_dates
    source.on_download = lambda: None
    worker.stop.clear()
    worker.execute(saved)
    assert [item[0][0] for item in source.downloads] == ['000300.SH', '000001.SZ', '000001.SZ']
    assert store.job(job['id'])['state'] == 'partial' and store.job(job['id'])['checkpoint'] == 2
    retry = store.retry_job(job['id'])
    assert retry['payload']['chunks'] == job['payload']['chunks'][:1]


@pytest.mark.postgres
def test_actual_qmt_connection_failure_stops_later_contracts(store, tmp_path):
    source = Source()
    source.fail = ConnectionError('offline')
    worker = Worker(store, tmp_path, source=source)
    worker.stop.wait = lambda _: False
    job = make_job(store, ['000300.SH', '000001.SZ'])
    worker.execute(job)
    saved = store.job(job['id'])
    assert saved['state'] == 'failed' and saved['checkpoint'] == 0 and saved['error_code'] == 'NETWORK_ERROR'
    assert len(source.downloads) == 4 and all(item[0] == ['000300.SH'] for item in source.downloads)
    assert not store.query('SELECT * FROM bars')


@pytest.mark.postgres
def test_cancellation_check_avoids_full_payload_and_reads_current_flag(store, tmp_path, monkeypatch):
    job = make_job(store)
    worker = Worker(store, tmp_path, source=Source())
    monkeypatch.setattr(store, 'job', lambda _: pytest.fail('Cancellation check must not reload the full task payload'))
    worker.check(job['id'])
    store.update_job(job['id'], cancel_requested=True)
    with pytest.raises(Cancelled):
        worker.check(job['id'])
    with pytest.raises(ValueError, match='任务不存在'):
        worker.check('00000000-0000-0000-0000-000000000000')
