from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from pfor_qmt.data import SHANGHAI
from pfor_qmt.diagnostics import check_source


NOW = datetime(2026, 9, 16, 23, tzinfo=SHANGHAI)


def source(frame=None, dates=None):
    return SimpleNamespace(
        get_full_tick=lambda codes: {codes[0]: {'lastPrice': 10, 'time': 20260916150000}},
        get_local_data=lambda **params: {params['stock_list'][0]: frame},
        get_trading_dates=lambda *args: dates or [],
    )


def test_ping_and_snapshot_do_not_imply_history_ready():
    report = check_source(source(), lambda: {'mode': 'market-only'}, now=NOW)
    assert [item['state'] for item in report['checks']] == ['ok', 'ok', 'empty', 'empty']
    assert not report['history_readable']
    assert report['checks'][1]['time'] == '2026-09-16T15:00:00+08:00'


def test_history_check_never_downloads_or_writes_and_bounds_calendar():
    frame = pd.DataFrame([{'time': 20260914, 'close': 10}])
    report = check_source(source(frame, ['20260914', '20260917']), lambda: {'mode': 'market-only'}, now=NOW)
    assert report['history_readable']
    assert report['checks'][2]['rows'] == report['checks'][3]['rows'] == 1


def test_probe_error_redacts_secrets_and_leaves_other_checks_available():
    def failure(**params):
        raise RuntimeError('postgresql://secret:password@localhost/db')
    qmt = source(dates=['20260914'])
    qmt.get_local_data = failure
    report = check_source(qmt, lambda: {'mode': 'market-only'}, now=NOW)
    assert report['checks'][2]['state'] == 'error'
    assert report['checks'][3]['state'] == 'ok'
    assert 'secret' not in str(report) and 'password' not in str(report)


def test_offline_bridge_skips_data_probes():
    def offline():
        raise ConnectionError('private endpoint')
    report = check_source(object(), offline, now=NOW)
    assert [item['state'] for item in report['checks']] == ['error', 'unverified', 'unverified', 'unverified']
    assert not report['history_readable']


def test_probe_rejects_invalid_security_before_qmt_calls():
    with pytest.raises(ValueError):
        check_source(object(), None, 'SH000300', now=NOW)
