import json
import re
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .data import codes, day, periods, json_default, FIELDS, MINUTE_PERIODS, AGGREGATE_PERIODS, period_label, filter_values
from .symbols import KINDS, MARKETS, market_of
from .identifiers import provider_name, source_market, identity_key, TS_EXCHANGES


def document(value):
    return Jsonb(value, dumps=lambda obj: json.dumps(obj, default=json_default, allow_nan=False))


def job_summary(job):
    result = dict(job)
    result['payload'] = dict(job['payload'])
    result['total_chunks'] = len(result['payload'].pop('chunks', []))
    result['result'] = {key:value for key,value in job['result'].items() if key != 'coverage'}
    return result


class Store:
    def __init__(self, dsn, schema='pfor_qmt'):
        if schema != 'pfor_qmt' and not re.fullmatch(r'pfor_qmt_test_[a-z0-9_]+', schema):
            raise ValueError('禁止使用其他业务 schema')
        self.dsn, self.schema = dsn, schema

    def connect(self):
        if not self.dsn:
            raise ValueError('请先配置 PostgreSQL 连接')
        conn = psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row)
        conn.execute(sql.SQL('SET search_path TO {}, pg_catalog').format(sql.Identifier(self.schema)))
        conn.execute("SET TIME ZONE 'Asia/Shanghai'")
        return conn

    def migrate(self):
        with self.connect() as conn:
            if conn.info.server_version < 140000:
                raise ValueError('需要 PostgreSQL 14 或更高版本')
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (self.schema + '.migrations',))
            conn.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.schema)))
            conn.execute('CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
            applied = {row['version'] for row in conn.execute('SELECT version FROM schema_version')}
            for path in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
                version = int(path.stem.split('_')[0])
                if version not in applied:
                    conn.execute(path.read_text('utf-8'))
                    conn.execute('INSERT INTO schema_version(version) VALUES(%s) ON CONFLICT DO NOTHING', (version,))

    def query(self, statement, args=(), one=False):
        with self.connect() as conn:
            cursor = conn.execute(statement, args)
            if not cursor.description:
                return None
            return cursor.fetchone() if one else cursor.fetchall()

    def health(self):
        try:
            row = self.query('SELECT max(version) AS version FROM schema_version', one=True)
            return {'connected': True, 'version': row['version']}
        except Exception:
            return {'connected': False, 'message': '数据库未连接或尚未初始化'}

    def securities(self, search='', kind='', source='qmt'):
        return self.query("SELECT * FROM securities WHERE source=%s AND (code ILIKE %s OR name ILIKE %s) AND (%s='' OR kind=%s OR (%s='etf' AND subtype='etf')) ORDER BY code LIMIT 1000",
                          (provider_name(source), '%' + search + '%', '%' + search + '%', kind, kind, kind))

    def catalog_page(self, search='', kind='', limit=50, offset=0, market='', subtype='', source='qmt', active=False, select_all=False):
        limit, offset = int(limit), int(offset)
        kinds = filter_values(kind, (*KINDS, 'etf'), '证券类别')
        markets = filter_values(market, MARKETS, '交易所')
        subtypes = filter_values(subtype, ('contract','continuous','combination','efp','etf'), '合约类型')
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError('无效目录筛选或分页参数')
        query = "FROM securities WHERE source=%s AND (code ILIKE %s OR name ILIKE %s OR metadata->>'product' ILIKE %s) AND (%s OR kind=ANY(%s) OR (%s AND subtype='etf')) AND (%s OR market=ANY(%s)) AND (%s OR subtype=ANY(%s)) AND (NOT %s OR (coalesce(nullif(metadata->>'expiry',''),'99999999') >= to_char(CURRENT_DATE,'YYYYMMDD') AND coalesce(nullif(metadata->>'listed',''),'00000000') <= to_char(CURRENT_DATE,'YYYYMMDD')))"
        args = (provider_name(source), '%' + search + '%', '%' + search + '%', '%' + search + '%', not kinds, kinds, 'etf' in kinds, not markets, markets, not subtypes, subtypes, active)
        with self.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            total = conn.execute('SELECT count(*) AS n ' + query, args).fetchone()['n']
            if select_all:
                if total > 10000:
                    raise ValueError(f'筛选结果共{total}个，超过单次10000个上限，请缩小范围；未截断选择')
                limit, offset = max(1, total), 0
            rows = conn.execute('SELECT code,name,kind,market,subtype,metadata,updated_at,source,instrument_id ' + query + ' ORDER BY code LIMIT %s OFFSET %s', args + (limit, offset)).fetchall()
        return dict(rows=rows, total=total, offset=offset, next_offset=offset + len(rows) if offset + len(rows) < total else None)

    def create_catalog_job(self, kinds, source='qmt', account_id=None, endpoint=None, exchanges=None):
        from .catalog import KINDS
        if not isinstance(kinds, list) or not kinds or any(kind not in KINDS for kind in kinds):
            raise ValueError('选择有效的证券或行业概念目录')
        provider_name(source)
        if source == 'tushare' and kinds != ['future']:
            raise ValueError('Tushare当前仅支持期货目录')
        selected_exchanges = filter_values(list(TS_EXCHANGES) if exchanges is None else exchanges, TS_EXCHANGES, '期货交易所') if source=='tushare' else []
        if source=='tushare' and not selected_exchanges:
            raise ValueError('请选择至少一个期货交易所')
        with self.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (self.schema + '.catalog-create',))
            active = conn.execute("SELECT * FROM jobs WHERE kind='catalog' AND coalesce(payload->>'source','qmt')=%s AND state IN ('queued','running') ORDER BY created_at LIMIT 1", (source,)).fetchone()
            if active:
                if not set(kinds).issubset(active['payload']['kinds']) or active['payload'].get('account_id') != account_id:
                    raise ValueError('已有目录同步正在进行，请等待完成后再同步其他类别')
                if source=='tushare' and not set(selected_exchanges).issubset(active['payload'].get('exchanges', list(TS_EXCHANGES))):
                    raise ValueError('已有目录同步正在进行，请等待完成后再同步其他交易所')
                return active
            return conn.execute("INSERT INTO jobs(id,kind,payload) VALUES(%s,'catalog',%s) RETURNING *",
                                (str(uuid.uuid4()), document({'kinds': list(dict.fromkeys(kinds)), 'source':source, 'account_id':account_id, 'endpoint':endpoint, **({'exchanges':selected_exchanges} if source=='tushare' else {})}))).fetchone()

    def save_security(self, code, name, kind, detail, subtype='', metadata=None, conn=None, source='qmt'):
        if kind == 'etf':
            kind, subtype = 'fund', 'etf'
        if conn is None:
            with self.connect() as connection:
                return self.save_security(code, name, kind, detail, subtype, metadata, connection, source)
        code = codes([code], source)[0]
        metadata = dict(metadata or {})
        if source == 'qmt' and kind == 'future' and subtype in ('','contract') and not metadata.get('delivery_month'):
            expiry = str(metadata.get('expiry') or '')
            product = str(metadata.get('product') or '')
            body = code.rsplit('.',1)[0]
            if re.fullmatch(r'\d{8}',expiry) and product and body.upper() in ((product + expiry[2:6]).upper(), (product + expiry[3:6]).upper()):
                metadata['delivery_month'] = expiry[:6]
        key = identity_key(code, source, kind, subtype, metadata or {})
        old = conn.execute('SELECT instrument_id FROM securities WHERE source=%s AND code=%s', (source, code)).fetchone()
        identity = conn.execute('SELECT id FROM instruments WHERE identity_key=%s', (key,)).fetchone()
        if identity is None:
            legacy = conn.execute('SELECT id FROM instruments WHERE identity_key=%s', (source + ':' + code,)).fetchone()
            if legacy:
                identity = conn.execute('UPDATE instruments SET identity_key=%s WHERE id=%s RETURNING id', (key, legacy['id'])).fetchone()
            else:
                identity = conn.execute('INSERT INTO instruments(identity_key) VALUES(%s) ON CONFLICT(identity_key) DO UPDATE SET identity_key=EXCLUDED.identity_key RETURNING id', (key,)).fetchone()
        if old and old['instrument_id'] != identity['id']:
            # Changing identity after bars exist needs explicit reconciliation, never an implicit merge.
            used = conn.execute('SELECT 1 FROM bars WHERE instrument_id=%s LIMIT 1', (old['instrument_id'],)).fetchone()
            if used:
                identity = {'id': old['instrument_id']}
        conn.execute('INSERT INTO securities(code,name,kind,details,market,subtype,metadata,source,instrument_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source,code) DO UPDATE SET name=EXCLUDED.name,kind=EXCLUDED.kind,details=EXCLUDED.details,market=EXCLUDED.market,subtype=EXCLUDED.subtype,metadata=EXCLUDED.metadata,instrument_id=EXCLUDED.instrument_id,updated_at=now()',
                     (code,name,kind,document(detail),source_market(code,source),subtype,document(metadata or {}),source,identity['id']))

    def save_board(self, part, members, conn):
        if not members:
            raise ValueError('板块成员为空，保留原快照')
        members = codes(members)
        conn.execute('INSERT INTO catalog_sectors(name,category,path) VALUES(%s,%s,%s) ON CONFLICT(name) DO UPDATE SET category=EXCLUDED.category,path=EXCLUDED.path,observed_at=now()', (part['name'], part['category'], document(part['path'])))
        conn.execute('INSERT INTO board_snapshots(id,name,category,members) VALUES(%s,%s,%s,%s)', (str(uuid.uuid4()), part['name'], part['category'], document(members)))

    def boards(self, search='', category='', limit=50, offset=0):
        limit, offset = int(limit), int(offset)
        if category not in ('', 'industry', 'concept') or not 1 <= limit <= 200 or offset < 0:
            raise ValueError('无效板块筛选或分页参数')
        query = "FROM catalog_sectors c WHERE category IN ('industry','concept') AND name ILIKE %s AND (%s='' OR category=%s)"
        args = ('%' + search + '%', category, category)
        with self.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            total = conn.execute('SELECT count(*) AS n ' + query, args).fetchone()['n']
            rows = conn.execute('SELECT name,category,path,observed_at ' + query + ' ORDER BY name LIMIT %s OFFSET %s', args + (limit, offset)).fetchall()
        return dict(rows=rows, total=total, next_offset=offset + len(rows) if offset + len(rows) < total else None)

    def board(self, name):
        row = self.query('SELECT * FROM board_snapshots WHERE name=%s ORDER BY observed_at DESC LIMIT 1', (name,), one=True)
        if not row:
            raise ValueError('板块尚未同步成员')
        return row

    def snapshot(self, code, sector, name, members):
        identifier = str(uuid.uuid4())
        members = codes(members)
        with self.connect() as conn:
            conn.execute('INSERT INTO index_mapping(code,sector,name) VALUES(%s,%s,%s) ON CONFLICT(code) DO UPDATE SET sector=EXCLUDED.sector,name=EXCLUDED.name,updated_at=now()', (codes([code])[0], sector, name))
            conn.execute('INSERT INTO constituent_snapshots(id,index_code,members) VALUES(%s,%s,%s)', (identifier, code, document(members)))
        return self.query('SELECT * FROM constituent_snapshots WHERE id=%s', (identifier,), one=True)

    def create_dataset(self, payload):
        source = provider_name(payload.get('source', 'qmt'))
        members = codes(payload['members'], source)
        selected = periods(payload.get('periods', ['1d']), source)
        name = str(payload.get('name', '')).strip()
        if not name or len(name) > 100:
            raise ValueError('数据集名称需要 1 至 100 个字符')
        if not isinstance(payload.get('scheduled', False), bool):
            raise ValueError('scheduled 必须为布尔值')
        snapshot = payload.get('snapshot_id')
        index_code = payload.get('index_code')
        board_name = payload.get('board_name')
        board_snapshot = payload.get('board_snapshot_id')
        if board_name:
            row = self.query('SELECT * FROM board_snapshots WHERE id=%s AND name=%s', (board_snapshot, board_name), one=True)
            if index_code or not row or row['members'] != members:
                raise ValueError('数据集成员必须匹配指定板块快照')
        if index_code:
            row = self.query('SELECT * FROM constituent_snapshots WHERE id=%s AND index_code=%s', (snapshot, index_code), one=True)
            if not row or row['members'] != members:
                raise ValueError('数据集成员必须匹配指定成分快照')
        schedule_time = payload.get('schedule_time') or ('19:00' if source == 'tushare' else '17:00')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule_time):
            raise ValueError('更新时间需要HH:MM格式')
        if source == 'tushare':
            found = self.query('SELECT code,subtype FROM securities WHERE source=%s AND code=ANY(%s)', (source,members))
            if len(found) != len(members) or index_code or board_name:
                raise ValueError('请先同步并选择Tushare期货目录')
            if set(selected) & set(MINUTE_PERIODS) and any(row['subtype'] != 'contract' for row in found):
                raise ValueError('分钟数据集只能包含具体月份合约')
        return self.query('INSERT INTO datasets(id,name,members,periods,index_code,snapshot_id,scheduled,board_name,board_snapshot_id,source,account_id,endpoint,schedule_time) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                          (str(uuid.uuid4()), name, document(members), document(selected), index_code, snapshot, bool(payload.get('scheduled')), board_name, board_snapshot,source,payload.get('account_id'),payload.get('endpoint'),schedule_time), one=True)

    def create_job(self, kind, payload, schedule_key=None):
        return self.query('INSERT INTO jobs(id,kind,payload,schedule_key) VALUES(%s,%s,%s,%s) ON CONFLICT(schedule_key) DO UPDATE SET schedule_key=EXCLUDED.schedule_key RETURNING *',
                          (str(uuid.uuid4()), kind, document(payload), schedule_key), one=True)

    def create_jobs(self, kind, payloads):
        if not payloads or len(payloads) > 1000 or sum(len(p.get('chunks', [])) for p in payloads) > 100000:
            raise ValueError('批量范围过大：最多1000个任务或100000个分块，请缩小范围')
        with self.connect() as conn:
            return [conn.execute('INSERT INTO jobs(id,kind,payload) VALUES(%s,%s,%s) RETURNING *',
                                 (str(uuid.uuid4()), kind, document(payload))).fetchone() for payload in payloads]

    def job(self, identifier):
        row = self.query('SELECT * FROM jobs WHERE id=%s', (identifier,), one=True)
        if not row:
            raise ValueError('任务不存在')
        if row['kind'] == 'download' and 'rows' not in row['result']:
            row['result']['rows'] = self.query('SELECT coalesce(sum(row_count),0) AS n FROM coverage WHERE job_id=%s', (identifier,), one=True)['n']
        return row

    def jobs_page(self, payload):
        states = filter_values(payload.get('states', []), ('queued','running','completed','partial','failed','cancelled'), '任务状态')
        kinds = filter_values(payload.get('kinds', []), ('download','export','catalog'), '任务类型')
        sources = filter_values(payload.get('sources', []), ('qmt','tushare'), '任务来源')
        limit, offset = int(payload.get('limit', 50)), int(payload.get('offset', 0))
        start, end = day(payload.get('start') or '1990-01-01'), day(payload.get('end') or '2100-01-01')
        if not 1 <= limit <= 200 or offset < 0 or start > end:
            raise ValueError('无效任务查询日期或分页')
        where = "FROM jobs WHERE (%s OR state=ANY(%s)) AND (%s OR kind=ANY(%s)) AND (%s OR coalesce(payload->>'source','qmt')=ANY(%s)) AND created_at::date BETWEEN %s AND %s AND (id::text ILIKE %s OR coalesce(error,'') ILIKE %s)"
        search = '%'+str(payload.get('search',''))+'%'
        args = (not states,states,not kinds,kinds,not sources,sources,start,end,search,search)
        fields = "id,kind,state,payload-'chunks' AS payload,jsonb_array_length(coalesce(payload->'chunks','[]'::jsonb)) AS total_chunks,checkpoint,attempts,cancel_requested,CASE WHEN kind='download' THEN jsonb_build_object('rows',(SELECT coalesce(sum(row_count),0) FROM coverage WHERE job_id=jobs.id)) || (result-'coverage') ELSE result-'coverage' END AS result,error,created_at,updated_at"
        with self.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            total = conn.execute('SELECT count(*) AS n '+where,args).fetchone()['n']
            rows = conn.execute('SELECT '+fields+' '+where+' ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s',(*args,limit,offset)).fetchall()
        return dict(rows=rows,total=total,offset=offset,next_offset=offset+len(rows) if offset+len(rows)<total else None)

    def update_job(self, identifier, **changes):
        allowed = {'state','checkpoint','attempts','cancel_requested','result','error','payload'}
        if not changes or set(changes) - allowed:
            raise ValueError('无效任务字段')
        assignments = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in changes)
        args = [document(value) if key in ('result','payload') else value for key, value in changes.items()]
        self.query(sql.SQL('UPDATE jobs SET {},updated_at=now() WHERE id=%s').format(assignments), args + [identifier])

    def write_chunk(self, job_id, part, rows, gaps, checkpoint, factor=None):
        source = provider_name(part.get('source', 'qmt'))
        with self.connect() as conn:
            if rows:
                with conn.cursor() as cursor:
                    fields = ('code', 'period', 'time', *FIELDS, 'trading_day', 'source', 'normalization_version', 'as_of_date', 'source_fields')
                    columns = sql.SQL(',').join(map(sql.Identifier, fields))
                    placeholders = sql.SQL(',').join(sql.Placeholder(field) for field in fields)
                    updates = sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(field), sql.Identifier(field)) for field in (*FIELDS, 'trading_day','as_of_date','source_fields','normalization_version'))
                    statement = sql.SQL('INSERT INTO bars({}) VALUES({}) ON CONFLICT(instrument_id,source,period,time) DO UPDATE SET {},updated_at=now() WHERE EXCLUDED.as_of_date IS NULL OR bars.as_of_date IS NULL OR EXCLUDED.as_of_date >= bars.as_of_date').format(columns, placeholders, updates)
                    cursor.executemany(statement, [dict({field: row.get(field) for field in fields}, source=source, source_fields=document(row.get('source_fields',{})), normalization_version='tushare-futures-v2' if part['period'] in AGGREGATE_PERIODS else 'tushare-futures-v1' if source == 'tushare' else 'qmt-raw-v1') for row in rows])
            if factor is not None:
                conn.execute('INSERT INTO factors(code,raw) VALUES(%s,%s) ON CONFLICT(code) DO UPDATE SET raw=EXCLUDED.raw,observed_at=now()', (part['code'], document(factor)))
            self.write_coverage(conn,job_id,part,len(rows),gaps,checkpoint,rows[0]['time'] if rows else None,rows[-1]['time'] if rows else None)

    def write_coverage(self, conn, job_id, part, count, gaps, checkpoint, actual_start=None, actual_end=None):
        conn.execute('INSERT INTO coverage(job_id,code,period,requested_start,requested_end,actual_start,actual_end,row_count,gaps) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id,code,period,requested_start) DO UPDATE SET actual_start=EXCLUDED.actual_start,actual_end=EXCLUDED.actual_end,row_count=EXCLUDED.row_count,gaps=EXCLUDED.gaps',
                     (job_id,part['code'],part['period'],part['start'],part['end'],actual_start,actual_end,count,document(gaps)))
        conn.execute("UPDATE jobs SET checkpoint=%s,attempts=0,result=result || jsonb_build_object('rows',(SELECT coalesce(sum(row_count),0) FROM coverage WHERE job_id=%s)),updated_at=now() WHERE id=%s", (checkpoint,job_id,job_id))

    def history(self, code, period='1d', start='1990-01-01', end='2100-01-01', limit=500, offset=0, conn=None, source='qmt'):
        code = codes([code], source)[0]
        period = periods([period],source)[0]
        limit, offset = int(limit), int(offset)
        if not 1 <= limit <= 5000 or offset < 0:
            raise ValueError('分页 limit 需要 1 至 5000，offset 不得为负')
        aggregate = period in AGGREGATE_PERIODS
        extra = ',as_of_date,source_fields' if aggregate else ''
        statement = "SELECT code,period,time,open,high,low,close,volume,amount,open_interest,settlement,previous_settlement,trading_day,source" + extra + " FROM bars WHERE source=%s AND code=%s AND period=%s AND coalesce(trading_day,time::date) BETWEEN %s::date AND %s::date ORDER BY time LIMIT %s OFFSET %s"
        args = (source, code, period, period_label(start,period), period_label(end,period), limit, offset)
        rows = conn.execute(statement, args).fetchall() if conn else self.query(statement, args)
        return {'rows': rows, 'offset': offset, 'limit': limit, 'next_offset': offset + len(rows) if len(rows) == limit else None,
                'source': 'postgresql', 'provider': source, 'normalization_version': 'tushare-futures-v2' if aggregate else 'tushare-futures-v1' if source == 'tushare' else 'qmt-raw-v1', 'adjustment': 'none', 'timezone': 'Asia/Shanghai',
                'date_basis': '按日期所在周/月的周期标签查询；time为周五/月末标签，as_of_date为上游计算截至日期，不是历史时点快照；source_fields保留上游原始字段及万元金额' if aggregate else '日线按交易日；分钟按上海自然时间查询，未提供交易日时夜盘归属待核验' if source == 'tushare' else '优先使用终端 trading_day；未提供时按行情时间的上海自然日查询，夜盘归属待核验',
                'units': {'price': '合约报价单位（见合约资料）', 'volume': '手', 'amount': '元', 'open_interest': '手'} if source == 'tushare' else {'price': 'QMT 原始报价', 'volume': 'QMT 原始成交量单位（未换算）', 'amount': 'QMT 原始成交额单位（未换算）', 'open_interest': 'QMT 原始持仓量单位（未换算）'}}

    def history_many(self, payload):
        source = provider_name(payload.get('source', 'qmt'))
        members, selected = codes(payload['members'], source), periods(payload['periods'], source)
        start, end = day(payload['start']), day(payload['end'])
        limit, offset = int(payload.get('limit', 300)), int(payload.get('offset', 0))
        if start > end or not 1 <= limit <= 5000 or offset < 0:
            raise ValueError('无效历史查询范围或分页')
        parts, args = [], [source, members]
        for period in selected:
            parts.append('(period=%s AND coalesce(trading_day,time::date) BETWEEN %s AND %s)')
            args.extend([period, period_label(start, period), period_label(end, period)])
        fields = ('code','period','time',*FIELDS,'trading_day','source','as_of_date','source_fields','normalization_version')
        rows = self.query('SELECT '+','.join(fields)+' FROM bars WHERE source=%s AND code=ANY(%s) AND ('+' OR '.join(parts)+') ORDER BY code,period,time LIMIT %s OFFSET %s', (*args, limit+1, offset))
        return dict(rows=rows[:limit], next_offset=offset+limit if len(rows)>limit else None, offset=offset,
                    source='postgresql', provider=source, timezone='Asia/Shanghai', adjustment='none',
                    normalization_versions={period: 'tushare-futures-v2' if period in AGGREGATE_PERIODS else 'tushare-futures-v1' if source=='tushare' else 'qmt-raw-v1' for period in selected},
                    units={'price':'合约报价单位（见合约资料）','volume':'手','amount':'元','open_interest':'手'} if source=='tushare' else {'price':'QMT 原始报价','volume':'QMT 原始成交量单位（未换算）','amount':'QMT 原始成交额单位（未换算）','open_interest':'QMT 原始持仓量单位（未换算）'},
                    date_basis='日线按交易日；分钟按上海自然时间；周/月按周期标签并保留计算截至日，不混合合约或周期')
