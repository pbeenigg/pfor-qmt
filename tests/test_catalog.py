from types import SimpleNamespace

import pytest

from pfor_qmt.catalog import discover
from pfor_qmt.service import Application
from pfor_qmt.settings import Settings
from pfor_qmt.tasks import Worker


class Directory:
    def __init__(self):
        self.read = []
        self.missing = False

    def get_sector_list(self):
        return ['沪深京A股', '沪深指数', '京市指数', '沪深ETF', '沪深300']

    def get_stock_list_in_sector(self, sector):
        return {'沪深京A股': ['000001.SZ', '920001.BJ'], '沪深指数': ['000300.SH'],
                '京市指数': ['899050.BJ'], '沪深ETF': ['510300.SH'], '沪深300': ['000001.SZ']}[sector]

    def get_instrument_detail(self, code):
        self.read.append(code)
        if self.missing and code == '920001.BJ':
            return {}
        return {'InstrumentName': '沪深300' if code == '000300.SH' else '目录证券 ' + code}


def test_discovery_uses_directory_sectors_not_index_constituents():
    sectors, parts = discover(Directory(), ['index', 'stock', 'etf'])
    assert len(parts) == 5 and '沪深300' in sectors
    assert {item['code']: item['kind'] for item in parts}['000300.SH'] == 'index'
    with pytest.raises(ValueError, match='缺少'):
        discover(SimpleNamespace(get_sector_list=lambda: ['沪深300']), ['index'])


def test_catalog_sync_deduplication_search_and_partial_retry(store, tmp_path):
    source = Directory()
    app = Application(Settings(tmp_path, config_path=tmp_path / 'config.toml'), store, source)
    job = app.dispatch('POST', '/catalog/sync', {'kinds': ['index', 'stock', 'etf']})
    assert app.dispatch('POST', '/catalog/sync', {'kinds': ['index', 'stock', 'etf']})['id'] == job['id']
    with pytest.raises(ValueError, match='其他类别'):
        app.dispatch('POST', '/catalog/sync', {'kinds': ['future']})
    source.missing = True
    app.worker.execute(store.job(job['id']))
    result = store.job(job['id'])
    assert result['state'] == 'partial' and result['checkpoint'] == 5
    assert result['result']['missing'] == ['920001.BJ']
    assert store.securities('920001.BJ') == []
    source.missing = False
    retry = app.dispatch('POST', f"/jobs/{job['id']}/retry", {})
    app.worker.execute(retry)
    assert store.job(retry['id'])['state'] == 'succeeded'
    assert store.job(job['id'])['state'] == 'partial'
    assert len(retry['payload']['chunks']) == 1
    assert app.dispatch('GET', '/catalog/securities', {'search': '沪深300', 'kind': 'index'})['rows'][0]['code'] == '000300.SH'
    snapshot = app.dispatch('POST', '/indices/refresh', {'code': '000300.SH', 'sector': '沪深300'})
    assert snapshot['members'] == ['000001.SZ']
    assert store.query('SELECT name FROM index_mapping', one=True)['name'] == '沪深300'
    assert len(app.dispatch('GET', '/catalog', {})['sectors']) == 5
    assert store.query('SELECT count(*) AS n FROM bars', one=True)['n'] == 0


def test_catalog_checkpoint_resume_and_cancel(store, tmp_path):
    source = Directory()
    source.get_stock_list_in_sector = lambda sector: [f'{index:06}.SZ' for index in range(1, 35)]
    job = store.create_catalog_job(['stock'])
    worker = Worker(store, tmp_path, source=source)
    worker.publish = lambda event: worker.stop.set() if event['data']['checkpoint'] == 16 else None
    worker.execute(job)
    saved = store.job(job['id'])
    assert saved['checkpoint'] == 16 and saved['state'] == 'queued'
    source.read.clear()
    Worker(store, tmp_path, source=source).execute(saved)
    assert len(source.read) == 18 and '000001.SZ' not in source.read
    assert store.job(job['id'])['result']['rows'] == 34
    second = store.create_catalog_job(['stock'])
    original = source.get_instrument_detail
    def cancel(code):
        store.update_job(second['id'], cancel_requested=True)
        return original(code)
    source.get_instrument_detail = cancel
    Worker(store, tmp_path, source=source).execute(second)
    assert store.job(second['id'])['state'] == 'cancelled'
    assert store.job(second['id'])['checkpoint'] == 0


def test_catalog_pagination_has_no_thousand_security_ceiling(store):
    with store.connect() as conn:
        conn.execute("INSERT INTO securities(code,name,kind) SELECT lpad(i::text,6,'0')||'.SZ','证券'||i,'stock' FROM generate_series(1,1003) i")
    result, offset = [], 0
    while True:
        page = store.catalog_page(limit=200, offset=offset)
        result.extend(row['code'] for row in page['rows'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert len(result) == len(set(result)) == page['total'] == 1003
    assert store.catalog_page(search='001003')['rows'][0]['name'] == '证券1003'
    with pytest.raises(ValueError):
        store.catalog_page(kind='unknown')
