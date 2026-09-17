"""QMT security directory discovery; no historical downloads or inferred names."""
from concurrent.futures import ThreadPoolExecutor

from .protocol import encode_value
from .storage import document, job_summary
from .symbols import KINDS as ASSET_KINDS, normalize_code, derivative_kind, contract_type, market_of
from .client import CfquantError


KINDS = ASSET_KINDS + ('etf', 'board')
EXCHANGES = ('中金所', '上期所', '大商所', '郑商所', '能源中心', '广期所')


def board_category(path):
    if any('港股' in part or '香港' in part for part in path):
        return 'other'
    if any('概念' in part for part in path):
        return 'concept'
    if any('行业' in part or part in ('申万', '中信', '证监会') for part in path):
        return 'industry'
    return 'other'


def instrument_metadata(detail):
    extended = detail.get('ExtendInfo')
    detail = dict(extended if isinstance(extended, dict) else {}, **detail)
    fields = {'underlying_code': 'OptUndlCode', 'underlying_market': 'OptUndlMarket',
              'strike': 'OptExercisePrice', 'option_type': 'OptType', 'expiry': 'ExpireDate',
              'listed': 'OpenDate', 'multiplier': 'VolumeMultiple', 'price_tick': 'PriceTick',
              'option_unit': 'OptUnit', 'instrument_type': 'InstrumentType', 'product': 'ProductID',
              'product_name': 'ProductName', 'created': 'CreateDate', 'is_trading': 'IsTrading'}
    result = {name: encode_value(detail[field]) for name, field in fields.items() if field in detail and detail[field] is not None}
    options = detail.get('option_details', {})
    for name, field in fields.items():
        if options.get(field) is not None:
            result[name] = encode_value(options[field])
    if options.get('optType'):
        result['option_type'] = options['optType']
    return result


def enrich_option(source, code, detail):
    # The native dedicated API documents these markets; commodity option fields remain as returned.
    function = getattr(source, 'get_option_detail_data', None)
    if not isinstance(detail, dict) or not function or derivative_kind(code) != 'option' or market_of(code) not in ('IF', 'SHO', 'SZO'):
        return detail
    try:
        options = function(code)
    except (NotImplementedError, CfquantError) as error:
        if isinstance(error, CfquantError) and error.remote_type not in ('NotImplementedError', 'AttributeError', 'SignatureUnavailable'):
            raise
        return detail
    return dict(detail, option_details=options) if isinstance(options, dict) and options else detail


def discover(source, selected):
    sectors = source.get_sector_list()
    if not sectors:
        raise ValueError('QMT 板块目录为空，请检查终端行情连接')
    available = set(sectors)
    groups = {
        'index': [name for name in ('沪深指数', '京市指数') if name in available],
        'stock': (['沪深京A股'] if '沪深京A股' in available else [name for name in ('沪深A股', '京市A股', '北证A股') if name in available]) + (['沪深B股'] if '沪深B股' in available else []),
        'etf': ['沪深ETF'] if '沪深ETF' in available else [name for name in ('沪市ETF', '深市ETF') if name in available],
        'fund': ['沪深基金'] if '沪深基金' in available else [name for name in ('沪市基金', '深市基金') if name in available],
        'bond': ['沪深债券'] if '沪深债券' in available else [name for name in ('沪市债券', '深市债券') if name in available],
        'future': [name for name in EXCHANGES if name in available],
        'option': [name for name in EXCHANGES + ('上证期权', '深证期权') if name in available],
    }
    members, cache = {}, {}
    def contents(sector):
        if sector not in cache:
            cache[sector] = source.get_stock_list_in_sector(sector)
        return cache[sector]
    etfs = set(code for name in groups['etf'] for code in contents(name)) if {'fund', 'etf'} & set(selected) else set()
    for kind in selected:
        if kind == 'board':
            continue
        if not groups[kind]:
            raise ValueError('终端缺少目录板块: ' + kind)
        count = 0
        for sector in groups[kind]:
            for raw in contents(sector):
                code = normalize_code(raw)
                if kind in ('future', 'option') and derivative_kind(code) != kind:
                    continue
                actual_kind = 'fund' if kind == 'etf' else kind
                if code in members and members[code]['kind'] != actual_kind:
                    raise ValueError('终端目录类别冲突: ' + code)
                members[code] = dict(code=code, kind=actual_kind, market=market_of(code),
                                     subtype='etf' if code in etfs else contract_type(code) if kind in ('future', 'option') else '')
                count += 1
        if not count:
            raise ValueError('终端证券目录为空: ' + kind)
    parts = list(members.values())
    if 'board' in selected:
        tree = source.get_sector_tree()
        boards = {}
        for item in tree:
            category = board_category(item['path'])
            if category != 'other':
                boards[item['name']] = dict(entry_type='board', name=item['name'], category=category, path=item['path'])
        if not boards:
            raise ValueError('终端未提供可识别的行业概念分类树，未按板块名称猜测分类')
        parts.extend(boards.values())
    return sectors, parts


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
        offset = job['checkpoint']
        while offset < len(parts):
            worker.check(identifier)
            if parts[offset].get('entry_type') == 'board':
                batch = []
                for part in parts[offset:offset + 16]:
                    if part.get('entry_type') != 'board':
                        break
                    batch.append(part)
                contents = worker.retry_network(identifier, lambda: list(pool.map(
                    lambda item: worker.source.get_stock_list_in_sector(item['name']), batch)))
                for part, members in zip(batch, contents):
                    worker.check(identifier)
                    with store.connect() as conn:
                        if members:
                            store.save_board(part, members, conn)
                            saved += 1
                        else:
                            failures.append('board:' + part['name'])
                        result = dict(rows=saved, missing=failures, total=len(parts), message='证券与板块目录已同步')
                        conn.execute('UPDATE jobs SET checkpoint=%s,result=%s,attempts=0,updated_at=now() WHERE id=%s', (offset + 1, document(result), identifier))
                    worker.publish({'event': 'job', 'data': job_summary(store.job(identifier))})
                    offset += 1
                continue
            bulk = getattr(worker.source, 'get_instrument_details', None)
            batch = []
            for part in parts[offset:offset + (100 if bulk else 16)]:
                if part.get('entry_type') == 'board':
                    break
                batch.append(part)
            def read():
                if bulk:
                    details = bulk([item['code'] for item in batch])
                else:
                    details = dict(zip([item['code'] for item in batch], pool.map(lambda item: worker.source.get_instrument_detail(item['code']), batch)))
                return list(pool.map(lambda item: enrich_option(worker.source, item['code'], details.get(item['code'])), batch))
            details = worker.retry_network(identifier, read)
            worker.check(identifier)
            with store.connect() as conn:
                for item, detail in zip(batch, details):
                    name = detail.get('InstrumentName') or detail.get('StockName') if isinstance(detail, dict) else None
                    if not isinstance(name, str) or not name.strip():
                        failures.append(item['code'])
                        continue
                    store.save_security(item['code'], name.strip(), item['kind'], encode_value(detail),
                                        subtype=item.get('subtype', ''), metadata=instrument_metadata(detail), conn=conn)
                    saved += 1
                result = dict(rows=saved, missing=failures, total=len(parts), message='证券目录已同步' if not failures else '部分证券名称不可用，保留旧资料')
                conn.execute('UPDATE jobs SET checkpoint=%s,result=%s,attempts=0,updated_at=now() WHERE id=%s',
                             (offset + len(batch), document(result), identifier))
            worker.publish({'event': 'job', 'data': job_summary(store.job(identifier))})
            offset += len(batch)
    return dict(result, state='partial' if failures else 'completed')
