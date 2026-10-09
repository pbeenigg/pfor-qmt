import json

import psycopg
import pytest

from pfor_qmt.client import CfquantError
from pfor_qmt.reliability import failure
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.storage import job_summary
from pfor_qmt.symbols import derivative_kind, contract_type
from pfor_qmt.tasks import Worker
from test_storage_tasks import Source, make_job


@pytest.mark.parametrize('code', ['CF701MSC17800.ZF', 'CF701MSP17800.ZF',
                                  'a2701-MS-C-4650.DF', 'c2701-MS-P-2000.DF'])
def test_ms_options_are_month_contracts(code):
    assert derivative_kind(code) == 'option'
    assert contract_type(code) == 'contract'


@pytest.mark.postgres
def test_catalog_migration_corrects_only_ms_options_and_preserves_instruments(store):
    for code in ['CF701MSC17800.ZF', 'a2701-MS-C-4650.DF', 'CF001.ZF']:
        store.save_security(code, code, 'future', {}, subtype='continuous')
    before = {row['code']: row['instrument_id'] for row in store.query('SELECT * FROM securities')}
    job = store.create_job('download', {'chunks': [{'code': 'CF701MSC17800.ZF'}]})
    store.query('DELETE FROM schema_version WHERE version=15')
    store.migrate()
    rows = {row['code']: row for row in store.query('SELECT * FROM securities')}
    assert rows['CF701MSC17800.ZF']['kind'] == rows['a2701-MS-C-4650.DF']['kind'] == 'option'
    assert rows['CF701MSC17800.ZF']['subtype'] == 'contract'
    assert rows['CF001.ZF']['kind'] == 'future' and rows['CF001.ZF']['subtype'] == 'continuous'
    assert {code: row['instrument_id'] for code, row in rows.items()} == before
    assert store.job(job['id'])['payload'] == job['payload']
    store.migrate()
    assert store.health()['version'] == 15


@pytest.mark.postgres
def test_legacy_option_directory_respects_terminal_lifecycle_without_rewriting_evidence(store, tmp_path):
    code = 'CF701MSC17800.ZF'
    store.save_security(code, code, 'future', {}, subtype='continuous',
                        metadata={'listed': 20260901, 'expiry': 20261111})
    parts = [dict(code=code, period='1m', start='2026-08-03', end='2026-08-09'),
             dict(code=code, period='1m', start='2026-08-10', end='2026-08-16')]
    original = store.create_job('download', dict(source='qmt', members=[code], periods=['1m'], chunks=parts))
    store.write_unit(original['id'], 0, parts[0], 'blocked', [], error_code='QMT_HISTORY_EMPTY', retryable=True)
    store.update_job(original['id'], state='blocked')
    before = store.job(original['id'])
    retry = store.retry_job(original['id'])
    source = Source()
    Worker(store, tmp_path, source=source).execute(retry)
    assert source.downloads == []
    saved = store.job(retry['id'])
    assert saved['state'] == 'succeeded' and saved['checkpoint'] == 2
    assert all(unit['quality_state'] == 'not_applicable' for unit in store.units_page(retry['id'])['rows'])
    assert store.job(original['id']) == before
    assert store.query('SELECT * FROM bars') == []
    with pytest.raises(ValueError, match='没有可重试'):
        store.retry_job(original['id'])


@pytest.mark.postgres
def test_real_continuous_contract_does_not_inherit_current_month_lifecycle(store, tmp_path):
    code = 'CF001.ZF'
    store.save_security(code, '棉花次主力', 'future', {}, subtype='continuous',
                        metadata={'listed': 20260915, 'expiry': 20261111})
    source = Source()
    job = make_job(store, [code])
    Worker(store, tmp_path, source=source).execute(job)
    assert len(source.downloads) == 1
    assert store.units_page(job['id'])['rows'][0]['quality_state'] != 'not_applicable'


@pytest.mark.postgres
def test_worker_and_task_subroutes_use_summary_without_full_payload(store, tmp_path, monkeypatch):
    source = Source()
    job = make_job(store)
    expected = job_summary(store.job(job['id']))
    assert store.job_summary(job['id']) == expected
    app = Application(Settings(config_path=tmp_path/'config.toml'), store, source)
    monkeypatch.setattr(store, 'job', lambda _: pytest.fail('Must not reload the full task for progress/details'))
    app.worker.execute(job)
    saved = app.dispatch('GET', '/jobs/' + str(job['id']), {'summary': '1'})
    assert saved['state'] == 'succeeded' and saved['total_chunks'] == 1
    assert 'chunks' not in saved['payload'] and 'coverage' not in saved['result']
    for route in ('units', 'events', 'links'):
        assert 'rows' in app.dispatch('GET', '/jobs/' + str(job['id']) + '/' + route, {})
    assert job_summary(saved)['total_chunks'] == 1


@pytest.mark.postgres
def test_large_task_summary_retains_count_without_transferring_chunks(store):
    parts = [dict(code='CF00.ZF', period='1m', start='2026-09-14', end='2026-09-14') for _ in range(10000)]
    job = store.create_job('download', dict(source='qmt', members=['CF00.ZF'], periods=['1m'], chunks=parts))
    summary = store.job_summary(job['id'])
    assert summary['total_chunks'] == 10000 and summary['checkpoint'] == 0
    assert len(json.dumps(summary, default=str)) < 2000
    assert store.job(job['id'])['payload']['chunks'] == parts


@pytest.mark.postgres
def test_legacy_full_task_api_still_returns_execution_range(store, tmp_path):
    job = make_job(store)
    app = Application(Settings(config_path=tmp_path/'config.toml'), store, Source())
    assert app.dispatch('GET', '/jobs/' + str(job['id']), {})['payload']['chunks'] == job['payload']['chunks']


@pytest.mark.postgres
@pytest.mark.parametrize('summary', [False, True])
def test_cancel_summary_avoids_full_payload_and_preserves_legacy_response(store, tmp_path, monkeypatch, summary):
    job = make_job(store)
    app = Application(Settings(config_path=tmp_path/'config.toml'), store, Source())
    if summary:
        monkeypatch.setattr(store, 'job', lambda _: pytest.fail('Cancellation must not reload full task'))
    saved = app.dispatch('POST', '/jobs/' + str(job['id']) + '/cancel', {'summary': '1'} if summary else {})
    assert saved['state'] == 'cancelled' and saved['cancel_requested']
    assert ('chunks' not in saved['payload']) == summary


@pytest.mark.postgres
def test_old_catalog_job_checkpoint_cannot_restore_wrong_ms_option_classification(store, tmp_path):
    from types import SimpleNamespace
    code = 'CF701MSC17800.ZF'
    source = SimpleNamespace(get_instrument_details=lambda _: {code: {'InstrumentName': code, 'OpenDate': 20260901}})
    job = store.create_job('catalog', dict(kinds=['future'], chunks=[dict(code=code, kind='future', subtype='continuous')]))
    Worker(store, tmp_path, source=source).execute(job)
    security = store.catalog_page(kind='option')['rows'][0]
    assert security['code'] == code and security['subtype'] == 'contract'
    assert store.catalog_page(kind='future')['rows'] == []


@pytest.mark.postgres
def test_qmt_restart_recovers_with_bounded_retries_and_diagnostics(store, tmp_path):
    source = Source()
    worker = Worker(store, tmp_path, source=source)
    elapsed = [0]
    worker.stop.wait = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds) or False
    def restarting():
        source.fail = CfquantError('QMT pipe bridge not connected password=hidden', remote_type='ConnectionError') if elapsed[0] < 22 else None
    source.on_download = restarting
    job = make_job(store)
    worker.execute(job)
    assert store.job(job['id'])['state'] == 'succeeded'
    assert len(source.downloads) == 3 and elapsed[0] == 30
    retries = [row for row in store.events_page({'job_id': str(job['id'])})['rows'] if row['code'] == 'NETWORK_RETRY']
    assert sorted(row['context']['delay_seconds'] for row in retries) == [10, 20]
    assert all(row['context']['error_type'] == 'CfquantError' for row in retries)
    assert 'hidden' not in json.dumps(retries, default=str)


@pytest.mark.postgres
def test_cancel_interrupts_restart_wait_before_another_request(store, tmp_path):
    source = Source()
    source.fail = ConnectionError('offline')
    worker = Worker(store, tmp_path, source=source)
    job = make_job(store)
    def cancel_during_wait(seconds):
        store.update_job(job['id'], cancel_requested=True)
        return False
    worker.stop.wait = cancel_during_wait
    worker.execute(job)
    assert store.job(job['id'])['state'] == 'cancelled'
    assert len(source.downloads) == 1


def test_database_restart_and_network_timeout_have_specific_safe_diagnostics():
    restart = failure(psycopg.errors.AdminShutdown('password=hidden'))
    assert '服务端终止' in restart['message'] and restart['diagnostic']['sqlstate'] == '57P01'
    timeout = failure(CfquantError('QMT pipe bridge response timeout for action=xtdata.get_local_data password=hidden', remote_type='ConnectionError'))
    assert '超时' in timeout['message'] and timeout['diagnostic']['rpc_action'] == 'xtdata.get_local_data'
    assert 'hidden' not in json.dumps([restart, timeout], ensure_ascii=False)


@pytest.mark.parametrize('message,reason', [
    ('password authentication failed for user "hidden"', 'authentication'),
    ('no password supplied', 'authentication'),
    ('no pg_hba.conf entry for host "hidden"', 'permission'),
    ('database "hidden" does not exist', 'database_missing'),
])
def test_database_login_failures_without_sqlstate_are_permanent_and_safe(message, reason):
    from pfor_qmt.reliability import database_connection_error
    error = psycopg.OperationalError(message)
    assert error.sqlstate is None and not database_connection_error(error)
    detail = failure(error)
    assert not detail['retryable'] and detail['diagnostic']['connection_reason'] == reason
    assert 'hidden' not in json.dumps(detail)


@pytest.mark.postgres
def test_database_loss_is_not_relabelled_as_a_market_gap_or_replayed(store, tmp_path, monkeypatch):
    source = Source()
    job = make_job(store)
    worker = Worker(store, tmp_path, source=source)
    original = store.query
    error = psycopg.errors.AdminShutdown('database restarted')
    def interrupted(statement, args=(), one=False):
        if str(statement).startswith('SELECT kind,subtype,metadata FROM securities'):
            raise error
        return original(statement, args, one)
    monkeypatch.setattr(store, 'query', interrupted)
    with pytest.raises(psycopg.errors.AdminShutdown) as caught:
        worker.execute(job)
    assert caught.value is error and source.downloads == []
    assert store.units_page(job['id'])['rows'] == []
    assert store.job_summary(job['id'])['checkpoint'] == 0


@pytest.mark.postgres
def test_permanent_database_failure_stops_with_specific_evidence(store, tmp_path, monkeypatch):
    source = Source()
    job = make_job(store)
    original = store.query
    def missing_table(statement, args=(), one=False):
        if str(statement).startswith('SELECT kind,subtype,metadata FROM securities'):
            raise psycopg.errors.UndefinedTable('relation missing')
        return original(statement, args, one)
    monkeypatch.setattr(store, 'query', missing_table)
    Worker(store, tmp_path, source=source).execute(job)
    saved = store.job_summary(job['id'])
    assert saved['state'] == 'failed' and saved['error_code'] == 'DATABASE_ERROR'
    assert '缺少所需数据表' in saved['error'] and source.downloads == []
    assert store.units_page(job['id'])['rows'][0]['issues'][0]['sqlstate'] == '42P01'


@pytest.mark.postgres
def test_exhausted_rpc_error_keeps_action_without_losing_retry_instructions(store, tmp_path):
    source = Source()
    source.fail = CfquantError('QMT pipe bridge not connected', remote_type='ConnectionError')
    source.fail.rpc_action = 'xtdata.download_history_data2'
    worker = Worker(store, tmp_path, source=source)
    worker.stop.wait = lambda _: False
    job = make_job(store)
    worker.execute(job)
    saved = store.job_summary(job['id'])
    assert saved['state'] == 'failed' and saved['error_code'] == 'NETWORK_ERROR'
    assert len(source.downloads) == 4 and saved['checkpoint'] == 0
    gap = store.units_page(job['id'])['rows'][0]['issues'][0]
    assert gap['rpc_action'] == 'xtdata.download_history_data2'
    assert gap['action'] == '恢复连接后重试未完成范围'


def test_browser_disconnect_does_not_send_a_second_source_error():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from pfor_qmt.server import handler_for
    handler = object.__new__(handler_for(SimpleNamespace()))
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = Mock()
    handler.wfile.write.side_effect = ConnectionAbortedError('browser left page')
    handler.send(200, {'ok': True})
    handler.send_response.assert_called_once_with(200)
    assert handler.close_connection
