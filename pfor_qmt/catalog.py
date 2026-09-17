"""QMT security directory discovery; no historical downloads or inferred names."""
import re
from concurrent.futures import ThreadPoolExecutor

from .protocol import encode_value
from .storage import document, job_summary


KINDS = ('index', 'stock', 'etf')


def discover(source, selected):
    sectors = source.get_sector_list()
    if not sectors:
        raise ValueError('QMT 板块目录为空，请检查终端行情连接')
    available = set(sectors)
    groups = {
        'index': [name for name in ('沪深指数', '京市指数') if name in available],
        'stock': ['沪深京A股'] if '沪深京A股' in available else [name for name in ('沪深A股', '京市A股', '北证A股') if name in available],
        'etf': ['沪深ETF'] if '沪深ETF' in available else [name for name in ('沪市ETF', '深市ETF') if name in available],
    }
    members = {}
    for kind in selected:
        if not groups[kind]:
            raise ValueError('终端缺少目录板块: ' + kind)
        count = 0
        for sector in groups[kind]:
            for code in source.get_stock_list_in_sector(sector):
                if not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', code):
                    continue
                if code in members and members[code] != kind:
                    raise ValueError('终端目录类别冲突: ' + code)
                members[code] = kind
                count += 1
        if not count:
            raise ValueError('终端证券目录为空: ' + kind)
    return sectors, [{'code': code, 'kind': kind} for code, kind in members.items()]


def synchronize(worker, job):
    store, identifier = worker.store, job['id']
    parts = job['payload'].get('chunks')
    if not parts:
        sectors, parts = worker.retry_network(identifier, lambda: discover(worker.source, job['payload']['kinds']))
        worker.check(identifier)
        with store.connect() as conn:
            conn.execute('UPDATE jobs SET payload=%s WHERE id=%s', (document(dict(job['payload'], chunks=parts)), identifier))
            with conn.cursor() as cursor:
                cursor.executemany('INSERT INTO catalog_sectors(name) VALUES(%s) ON CONFLICT(name) DO UPDATE SET observed_at=now()', [(name,) for name in sectors])
        worker.publish({'event': 'job', 'data': job_summary(store.job(identifier))})
    result = dict(job['result']) if job['checkpoint'] else {}
    failures = list(result.get('missing', []))
    saved = result.get('rows', 0)
    # Existing RPC supports concurrent calls; keep at most eight in flight, on the same QMT source.
    with ThreadPoolExecutor(max_workers=8) as pool:
        for offset in range(job['checkpoint'], len(parts), 16):
            worker.check(identifier)
            batch = parts[offset:offset + 16]
            def read():
                return list(pool.map(lambda item: worker.source.get_instrument_detail(item['code']), batch))
            details = worker.retry_network(identifier, read)
            worker.check(identifier)
            with store.connect() as conn:
                for item, detail in zip(batch, details):
                    name = detail.get('InstrumentName') or detail.get('StockName') if isinstance(detail, dict) else None
                    if not isinstance(name, str) or not name.strip():
                        failures.append(item['code'])
                        continue
                    conn.execute('INSERT INTO securities(code,name,kind,details) VALUES(%s,%s,%s,%s) ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,kind=EXCLUDED.kind,details=EXCLUDED.details,updated_at=now()',
                                 (item['code'], name.strip(), item['kind'], document(encode_value(detail))))
                    saved += 1
                result = dict(rows=saved, missing=failures, total=len(parts), message='证券目录已同步' if not failures else '部分证券名称不可用，保留旧资料')
                conn.execute('UPDATE jobs SET checkpoint=%s,result=%s,attempts=0,updated_at=now() WHERE id=%s',
                             (offset + len(batch), document(result), identifier))
            worker.publish({'event': 'job', 'data': job_summary(store.job(identifier))})
    return dict(result, state='partial' if failures else 'completed')
