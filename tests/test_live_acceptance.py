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
