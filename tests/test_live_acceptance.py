from datetime import datetime
from decimal import Decimal

import pytest

from pfor_qmt.data import SHANGHAI
from tools.live_acceptance import canonical, history_rows, run


def test_acceptance_comparison_preserves_precision_null_and_timezone():
    row = dict(code='000300.SH', period='1d', time='2026-09-14T00:00:00+08:00',
               close='123.1234567890123456789', volume='1152921504606847013', source='qmt')
    db = dict(row, time=datetime(2026,9,14,tzinfo=SHANGHAI), close=Decimal(row['close']))
    csv = dict(row, amount='')
    assert canonical([row]) == canonical([db]) == canonical([csv])
    assert canonical([row]) != canonical([dict(row, close='123.1234567890123456790')])


def test_acceptance_refuses_competing_tasks_before_creating_dataset(tmp_path):
    class Active:
        def request(self, path):
            assert path == '/jobs'
            return [{'state':'running'}]
    with pytest.raises(ValueError,match='active'):
        run(Active(), None, tmp_path, '2026-09-14','2026-09-14',{})


def test_acceptance_collects_all_pages():
    class Client:
        def history(self,*args,offset,**kwargs):
            return dict(rows=[offset],next_offset=offset+2 if offset < 4 else None)
    assert history_rows(Client(),'000300.SH','2026-09-01','2026-09-14') == [0,2,4]


@pytest.mark.parametrize('start,end', [
    (None, None), ('bad-date', '2026-09-14'), ('2026-09-14', '2026-09-01'),
    ('2026-08-01', '2026-09-14'), ('2099-01-01', '2099-01-02'),
])
def test_acceptance_rejects_invalid_window_before_any_requests(tmp_path,start,end):
    with pytest.raises(ValueError):
        run(object(), None, tmp_path, start, end, {})


@pytest.mark.parametrize('samples,period,start,end', [
    ([], '1d', '2026-09-14', '2026-09-14'),
    (['bad-code'], '1d', '2026-09-14', '2026-09-14'),
    ([f'{code:06}.SH' for code in range(11)], '1d', '2026-09-14', '2026-09-14'),
    (['cu2610.SF'], 'tick', '2026-09-14', '2026-09-14'),
    (['cu2610.SF'], '1m', '2026-09-14', '2026-09-15'),
])
def test_acceptance_rejects_oversized_or_invalid_samples_before_requests(tmp_path, samples, period, start, end):
    with pytest.raises(ValueError):
        run(object(), None, tmp_path, start, end, {}, samples, period)


def test_readonly_cli_diagnoses_selected_codes_without_creating_jobs(monkeypatch, capsys):
    import json
    import sys
    from types import SimpleNamespace
    from tools import live_acceptance
    calls = []
    def request(path, payload):
        assert path == '/source/diagnostics'
        calls.append(payload['code'])
        return {'code': payload['code'], 'history_readable': False}
    monkeypatch.setattr(live_acceptance.DataClient, 'from_config', lambda path: SimpleNamespace(request=request))
    monkeypatch.setattr(sys, 'argv', ['live_acceptance.py', '--codes', 'cu2610.sf', 'HO2609-C-2500.IF', '--period', '1m'])
    assert live_acceptance.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert calls == report['samples'] == ['cu2610.SF', 'HO2609-C-2500.IF']
    assert report['state'] == 'diagnostics_only'
    assert report['period'] == '1m' and report['diagnostic_period'] == '1d'
