import csv
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pyarrow.parquet as pq
import pytest

from pfor_qmt.catalog import discover, instrument_metadata
from pfor_qmt.data import codes, day, normalize_bars, chunks, SHANGHAI
from pfor_qmt.market_bridge import MarketBridge
from pfor_qmt.policy import validate
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.symbols import derivative_kind, contract_type
from pfor_qmt.tasks import Worker


@pytest.mark.parametrize('code,kind', [
    ('IF2609.IF', 'future'), ('cu2610.SF', 'future'), ('AP701.ZF', 'future'),
    ('sc2610.INE', 'future'), ('lc2701.GF', 'future'), ('SP a2611&a2701.DF', 'future'),
    ('T2612(EFP).IF', 'future'), ('sc_tas001.INE', 'future'), ('rbL0.SF', 'future'),
    ('HO2609-C-2500.IF', 'option'), ('cu2610C80000.SF', 'option'),
    ('m2701-P-3000.DF', 'option'), ('SR701C6000.ZF', 'option'),
    ('10010971.SHO', 'option'), ('90007051.SZO', 'option'),
])
def test_derivative_identifiers_survive_every_boundary(code, kind):
    assert codes([code]) == [code]
    assert codes(code) == [code]
    assert derivative_kind(code) == kind
    for action, field in [('get_full_tick', 'code_list'), ('get_local_data', 'stock_list'),
                          ('download_history_data2', 'stock_list'), ('get_instrument_detail', 'stock_code')]:
        value = code if field == 'stock_code' else [code]
        assert validate('xtdata.' + action, {field: value})[field] == value


def test_code_parser_preserves_combination_spaces_and_rejects_injection():
    assert codes('cu2610.sf,SP a2611&a2701.DF') == ['cu2610.SF', 'SP a2611&a2701.DF']
    assert codes('000300.SH 000001.SZ') == ['000300.SH', '000001.SZ']
    for code in ['cu2610.US', '../a.SF', 'cu;drop.SF', '<img>.SH', 'cu\n2610.SF']:
        with pytest.raises(ValueError):
            codes([code])


def test_option_strikes_are_not_continuous_contract_suffixes():
    for code in ['HO2609-C-2500.IF', 'cu2610C80000.SF', 'm2701-P-3000.DF', 'SR701C6000.ZF']:
        assert contract_type(code) == 'contract'
    assert contract_type('SP cu2610C80000&cu2610C81000.SF') == 'combination'
    assert contract_type('cuL0.SF') == 'continuous'
    assert contract_type('cu2610.SF') == 'contract'


def test_batch_bridge_and_sector_ancestry():
    calls = []
    tree = {'': [[], ['行业', '概念']], '行业': [['银行'], []], '概念': [['人工智能'], []]}
    context = SimpleNamespace(get_sector_list=lambda node: tree[node],
                              get_instrument_detail=lambda code, complete: calls.append((code, complete)) or {'InstrumentName': code},
                              get_option_detail_data=lambda code: {'OptUndlCode': '510050'})
    bridge = MarketBridge(context, tx=object())
    assert bridge.dispatch('xtdata.get_sector_tree') == [{'name': '银行', 'path': ['行业']}, {'name': '人工智能', 'path': ['概念']}]
    assert bridge.dispatch('xtdata.get_sector_list') == ['银行', '人工智能']
    result = bridge.dispatch('xtdata.get_instrument_details', {'stock_list': ['cu2610.SF', '10010971.SHO']})
    assert list(result) == ['cu2610.SF', '10010971.SHO']
    assert calls == [('cu2610.SF', True), ('10010971.SHO', True)]
    assert bridge.dispatch('xtdata.get_option_detail_data', {'stock_code': '10010971.SHO'})['OptUndlCode'] == '510050'
    with pytest.raises(ValueError):
        bridge.dispatch('xtdata.get_instrument_details', {'stock_list': ['cu2610.SF'] * 101})


class MultiSource:
    groups = {'沪深京A股': ['000001.SZ'], '沪深B股': ['200011.SZ'], '沪深指数': ['000300.SH'],
              '沪深ETF': ['510300.SH'], '沪深基金': ['510300.SH', '160105.SZ'], '沪深债券': ['113001.SH'],
              '上期所': ['cu2610.SF', 'cu2610C80000.SF', 'SP au2610&au2612.SF'], '能源中心': ['sc2610.INE'],
              '上证期权': ['10010971.SHO'], '行业银行': ['000001.SZ'], '人工智能': ['000001.SZ', '200011.SZ']}

    def get_sector_list(self):
        return list(self.groups)

    def get_stock_list_in_sector(self, name):
        return self.groups[name]

    def get_sector_tree(self):
        return [{'name': '行业银行', 'path': ['行业', '申万']}, {'name': '人工智能', 'path': ['概念']}]

    def get_instrument_details(self, selected):
        return {code: {'InstrumentName': '名称' + code, 'VolumeMultiple': 5, 'PriceTick': 10, 'ExpireDate': '20261015'} for code in selected}


def test_seven_types_discovery_and_board_snapshots(store, tmp_path):
    source = MultiSource()
    selected = ['future', 'option', 'stock', 'index', 'fund', 'bond', 'board']
    _, parts = discover(source, selected)
    assert parts[0]['code'] == 'cu2610.SF'
    instruments = {item['code']: item for item in parts if 'code' in item}
    assert instruments['cu2610C80000.SF']['kind'] == 'option'
    assert instruments['SP au2610&au2612.SF']['subtype'] == 'combination'
    assert instruments['510300.SH']['kind'] == 'fund' and instruments['510300.SH']['subtype'] == 'etf'
    assert instruments['160105.SZ']['kind'] == 'fund'
    app = Application(Settings(tmp_path, config_path=tmp_path/'config.toml'), store, source)
    job = app.dispatch('POST', '/catalog/sync', {})
    app.worker.execute(store.job(job['id']))
    assert store.job(job['id'])['state'] == 'succeeded'
    assert {row['kind'] for row in store.query('SELECT DISTINCT kind FROM securities')} == {'future','option','stock','index','fund','bond'}
    assert len(store.catalog_page(kind='fund')['rows']) == 2
    assert len(store.catalog_page(kind='etf')['rows']) == 1
    assert store.catalog_page(kind='future', market='INE')['rows'][0]['code'] == 'sc2610.INE'
    detail = app.dispatch('GET', '/catalog/detail', {'code': 'cu2610.SF'})
    assert detail['metadata']['multiplier'] == 5
    assert app.dispatch('GET', '/boards', {})['total'] == 2
    snapshot = app.dispatch('GET', '/boards/members', {'name': '人工智能'})
    dataset = store.create_dataset(dict(name='概念成员', members=snapshot['members'], board_name='人工智能', board_snapshot_id=snapshot['id']))
    source.groups = dict(source.groups, 人工智能=['000001.SZ'])
    app.dispatch('POST', '/boards/refresh', {'name': '人工智能'})
    assert len(store.query('SELECT members FROM datasets WHERE id=%s', (dataset['id'],), one=True)['members']) == 2
    app.dispatch('POST', '/datasets/' + str(dataset['id']) + '/refresh', {})
    assert store.query('SELECT members FROM datasets WHERE id=%s', (dataset['id'],), one=True)['members'] == ['000001.SZ']


def test_night_bars_use_explicit_trading_day_without_moving_timestamp():
    frame = [{'time': '2026-09-11T21:01:00+08:00', 'tradingDay': 20260914, 'close': 1, 'openInterest': '12345678901234567890', 'settlementPrice': '81234.567890123456789'}]
    rows = normalize_bars(frame, 'cu2610.SF', '1m', day('2026-09-14'), day('2026-09-14'))
    assert rows[0]['time'].day == 11 and rows[0]['trading_day'].day == 14
    assert rows[0]['open_interest'] == Decimal('12345678901234567890')
    assert rows[0]['settlement'] == Decimal('81234.567890123456789')
    rows = normalize_bars([{'time': '2026-09-14T21:00:00', 'close': 1}], 'cu2610.SF', '1m', day('2026-09-14'), day('2026-09-14'))
    assert rows[0]['trading_day'] is None


def test_terminal_option_fields_and_native_settle_name():
    result = instrument_metadata({'InstrumentName': '期权', 'VolumeMultiple': 10000,
                                  'ExtendInfo': {'OptUndlCode': '510050', 'OptUndlMarket': 'SH', 'OptExercisePrice': 2.5}})
    assert result['underlying_code'] == '510050' and result['strike'] == 2.5
    assert 'option_type' not in result
    option = instrument_metadata({'InstrumentName': '期权', 'option_details': {'OptExercisePrice': 2.85, 'OptUndlCode': '510050', 'optType': 'CALL'}})
    assert option['strike'] == 2.85 and option['option_type'] == 'CALL'
    frame = pd.DataFrame([{'time': 20260914210000, 'close': 3, 'settle': '2.1234567890123456789', 'tradingDay': 20260915.0},
                          {'time': 20260915210000, 'close': 4}])
    rows = normalize_bars(frame, 'cu2610.SF', '1m', day('2026-09-15'), day('2026-09-15'))
    assert rows[0]['settlement'] == Decimal('2.1234567890123456789')
    assert rows[0]['trading_day'] == day('2026-09-15') and rows[1]['trading_day'] is None


def test_board_checkpoint_and_empty_members_remain_partial(store, tmp_path):
    source = MultiSource()
    job = store.create_catalog_job(['board'])
    worker = Worker(store, tmp_path, source=source)
    worker.publish = lambda event: worker.stop.set() if event['data']['checkpoint'] == 1 else None
    worker.execute(job)
    assert store.job(job['id'])['state'] == 'queued'
    assert store.query('SELECT count(*) AS n FROM board_snapshots', one=True)['n'] == 1
    Worker(store, tmp_path, source=source).execute(store.job(job['id']))
    assert store.job(job['id'])['state'] == 'succeeded'
    assert store.query('SELECT count(*) AS n FROM board_snapshots', one=True)['n'] == 2
    source.groups = dict(source.groups, 人工智能=[])
    second = store.create_catalog_job(['board'])
    Worker(store, tmp_path, source=source).execute(second)
    assert store.job(second['id'])['state'] == 'partial'
    assert store.job(second['id'])['result']['missing'] == ['board:人工智能']
    assert len(store.board('人工智能')['members']) == 2
    snapshot = store.board('人工智能')
    app = Application(Settings(tmp_path, config_path=tmp_path/'config.toml'), store, source)
    with pytest.raises(ValueError, match='保留原快照'):
        app.dispatch('POST', '/boards/refresh', {'name': '人工智能'})
    assert store.board('人工智能')['id'] == snapshot['id']


def test_night_storage_and_files_filter_by_terminal_trading_day(store, tmp_path):
    rows = normalize_bars([{'time': '2026-09-11T21:01:00', 'tradingDay': 20260914, 'close': '1.1234567890123456789', 'openInterest': 123}],
                          'cu2610.SF', '1m', day('2026-09-14'), day('2026-09-14'))
    part = dict(code='cu2610.SF', period='1m', start='2026-09-14', end='2026-09-14')
    job = store.create_job('download', {'chunks': [part]})
    store.write_chunk(job['id'], part, rows, [], 1)
    page = store.history('cu2610.SF', '1m', '2026-09-14', '2026-09-14')
    assert len(page['rows']) == 1 and page['rows'][0]['time'].day == 11
    assert store.history('cu2610.SF', '1m', '2026-09-11', '2026-09-11')['rows'] == []
    worker = Worker(store, tmp_path, source=object())
    for format in ('csv', 'parquet'):
        export = store.create_job('export', dict(members=['cu2610.SF'], period='1m', start='2026-09-14', end='2026-09-14', format=format))
        worker.execute(export)
        saved = store.job(export['id'])
        assert saved['state'] == 'succeeded' and saved['result']['rows'] == 1
        path = tmp_path/'exports'/saved['result']['file']
        if format == 'csv':
            with path.open(encoding='utf-8-sig', newline='') as stream:
                actual = list(csv.DictReader(stream))[0]
        else:
            actual = pq.read_table(path).to_pylist()[0]
        assert actual['trading_day'] == '2026-09-14' and actual['time'].startswith('2026-09-11')
        assert actual['close'] == '1.1234567890123456789'


def test_legacy_bridge_fallback_is_explicit_for_derivatives(monkeypatch):
    from pfor_qmt import xtdata
    from pfor_qmt.client import CfquantError
    calls = []
    def request(action, params, **kwargs):
        calls.append(action)
        if action == 'xtdata.get_instrument_details':
            raise CfquantError('Unsupported market action: xtdata.get_instrument_details', remote_type='ValueError')
        return {'InstrumentName': '平安银行'}
    monkeypatch.setattr(xtdata, 'get_client', lambda: SimpleNamespace(request=request))
    assert xtdata.get_instrument_details(['000001.SZ'])['000001.SZ']['InstrumentName'] == '平安银行'
    with pytest.raises(ValueError, match='PFOR_MARKET'):
        xtdata.get_instrument_details(['cu2610.SF'])
    assert calls == ['xtdata.get_instrument_details', 'xtdata.get_instrument_detail', 'xtdata.get_instrument_details']


def test_derivative_history_export_and_market_calendar(store, tmp_path):
    calls = []
    frame = pd.DataFrame([{'time': 20260914000000, 'close': '81234.567890123456789', 'openInterest': 12345, 'settlementPrice': 81235}])
    dates = ['20260908','20260909','20260910','20260911','20260914']
    def calendar(code, start='', end='', count=-1):
        calls.append(code)
        return dates if count == 5 else ['20260914']
    source = SimpleNamespace(download_history_data2=lambda *args: True,
                             get_local_data=lambda **params: {params['stock_list'][0]: frame}, get_trading_dates=calendar,
                             get_divid_factors=lambda code: pytest.fail('Derivatives must not request equity adjustment factors'))
    payload = dict(members=['sc2610.INE'], periods=['1d'], start='2026-09-14', end='2026-09-14')
    payload['chunks'] = chunks(payload)
    worker = Worker(store, tmp_path, source=source)
    job = store.create_job('download', payload)
    worker.execute(job)
    assert store.job(job['id'])['state'] == 'partial'
    assert store.units_page(job['id'])['rows'][0]['issues'][0]['code']=='OHLC_INCOMPLETE'
    assert store.query('SELECT market FROM trading_dates', one=True)['market'] == 'INE'
    for format in ('csv','parquet'):
        export = store.create_job('export', dict(members=payload['members'], period='1d', start=payload['start'], end=payload['end'], format=format))
        worker.execute(export)
        saved = store.job(export['id'])
        assert saved['state'] == 'succeeded', saved['error']
        path = tmp_path/'exports'/saved['result']['file']
        if format == 'csv':
            with path.open(encoding='utf-8-sig', newline='') as stream:
                row = list(csv.DictReader(stream))[0]
        else:
            row = pq.read_table(path).to_pylist()[0]
        assert row['open_interest'] == '12345' and row['settlement'] == '81235'
        assert row['trading_day'] == '2026-09-14'
    dataset = store.create_dataset(dict(name='原油', members=payload['members'], scheduled=True))
    store.query("UPDATE datasets SET schedule_from='2026-09-14' WHERE id=%s", (dataset['id'],))
    worker.schedule(datetime(2026,9,14,17,tzinfo=SHANGHAI))
    worker.schedule(datetime(2026,9,14,17,tzinfo=SHANGHAI))
    assert set(calls) == {'sc2610.INE'}
    assert len(store.query('SELECT * FROM jobs WHERE schedule_key IS NOT NULL')) == 1
