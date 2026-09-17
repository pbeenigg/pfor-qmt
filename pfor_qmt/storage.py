import json
import re
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .data import codes, day, periods, json_default, FIELDS
from .symbols import KINDS, market_of


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

    def securities(self, search='', kind=''):
        return self.query("SELECT * FROM securities WHERE (code ILIKE %s OR name ILIKE %s) AND (%s='' OR kind=%s OR (%s='etf' AND subtype='etf')) ORDER BY code LIMIT 1000",
                          ('%' + search + '%', '%' + search + '%', kind, kind, kind))

    def catalog_page(self, search='', kind='', limit=50, offset=0, market='', subtype=''):
        limit, offset = int(limit), int(offset)
        if kind not in ('', 'etf') + KINDS or not 1 <= limit <= 200 or offset < 0:
            raise ValueError('无效目录筛选或分页参数')
        query = "FROM securities WHERE (code ILIKE %s OR name ILIKE %s) AND (%s='' OR kind=%s OR (%s='etf' AND subtype='etf')) AND (%s='' OR market=%s) AND (%s='' OR subtype=%s)"
        args = ('%' + search + '%', '%' + search + '%', kind, kind, kind, market, market, subtype, subtype)
        with self.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            total = conn.execute('SELECT count(*) AS n ' + query, args).fetchone()['n']
            rows = conn.execute('SELECT code,name,kind,market,subtype,metadata,updated_at ' + query + ' ORDER BY code LIMIT %s OFFSET %s', args + (limit, offset)).fetchall()
        return dict(rows=rows, total=total, offset=offset, next_offset=offset + len(rows) if offset + len(rows) < total else None)

    def create_catalog_job(self, kinds):
        from .catalog import KINDS
        if not isinstance(kinds, list) or not kinds or any(kind not in KINDS for kind in kinds):
            raise ValueError('选择有效的证券或行业概念目录')
        with self.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (self.schema + '.catalog-create',))
            active = conn.execute("SELECT * FROM jobs WHERE kind='catalog' AND state IN ('queued','running') ORDER BY created_at LIMIT 1").fetchone()
            if active:
                if not set(kinds).issubset(active['payload']['kinds']):
                    raise ValueError('已有目录同步正在进行，请等待完成后再同步其他类别')
                return active
            return conn.execute("INSERT INTO jobs(id,kind,payload) VALUES(%s,'catalog',%s) RETURNING *",
                                (str(uuid.uuid4()), document({'kinds': list(dict.fromkeys(kinds))}))).fetchone()

    def save_security(self, code, name, kind, detail, subtype='', metadata=None, conn=None):
        if kind == 'etf':
            kind, subtype = 'fund', 'etf'
        statement = 'INSERT INTO securities(code,name,kind,details,market,subtype,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,kind=EXCLUDED.kind,details=EXCLUDED.details,market=EXCLUDED.market,subtype=EXCLUDED.subtype,metadata=EXCLUDED.metadata,updated_at=now()'
        args = (codes([code])[0], name, kind, document(detail), market_of(code), subtype, document(metadata or {}))
        if conn is not None:
            conn.execute(statement, args)
        else:
            self.query(statement, args)

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
        members = codes(payload['members'])
        selected = periods(payload.get('periods', ['1d']))
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
        return self.query('INSERT INTO datasets(id,name,members,periods,index_code,snapshot_id,scheduled,board_name,board_snapshot_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                          (str(uuid.uuid4()), name, document(members), document(selected), index_code, snapshot, bool(payload.get('scheduled')), board_name, board_snapshot), one=True)

    def create_job(self, kind, payload, schedule_key=None):
        return self.query('INSERT INTO jobs(id,kind,payload,schedule_key) VALUES(%s,%s,%s,%s) ON CONFLICT(schedule_key) DO UPDATE SET schedule_key=EXCLUDED.schedule_key RETURNING *',
                          (str(uuid.uuid4()), kind, document(payload), schedule_key), one=True)

    def job(self, identifier):
        row = self.query('SELECT * FROM jobs WHERE id=%s', (identifier,), one=True)
        if not row:
            raise ValueError('任务不存在')
        return row

    def update_job(self, identifier, **changes):
        allowed = {'state','checkpoint','attempts','cancel_requested','result','error'}
        if not changes or set(changes) - allowed:
            raise ValueError('无效任务字段')
        assignments = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in changes)
        args = [document(value) if key == 'result' else value for key, value in changes.items()]
        self.query(sql.SQL('UPDATE jobs SET {},updated_at=now() WHERE id=%s').format(assignments), args + [identifier])

    def write_chunk(self, job_id, part, rows, gaps, checkpoint, factor=None):
        with self.connect() as conn:
            if rows:
                with conn.cursor() as cursor:
                    fields = ('code', 'period', 'time', *FIELDS, 'trading_day')
                    columns = sql.SQL(',').join(map(sql.Identifier, fields))
                    placeholders = sql.SQL(',').join(sql.Placeholder(field) for field in fields)
                    updates = sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(field), sql.Identifier(field)) for field in (*FIELDS, 'trading_day'))
                    statement = sql.SQL('INSERT INTO bars({}) VALUES({}) ON CONFLICT(code,period,time) DO UPDATE SET {},updated_at=now()').format(columns, placeholders, updates)
                    cursor.executemany(statement, [{field: row.get(field) for field in fields} for row in rows])
            conn.execute('INSERT INTO coverage(job_id,code,period,requested_start,requested_end,actual_start,actual_end,row_count,gaps) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id,code,period,requested_start) DO UPDATE SET actual_start=EXCLUDED.actual_start,actual_end=EXCLUDED.actual_end,row_count=EXCLUDED.row_count,gaps=EXCLUDED.gaps',
                         (job_id, part['code'], part['period'], part['start'], part['end'], rows[0]['time'] if rows else None, rows[-1]['time'] if rows else None, len(rows), document(gaps)))
            if factor is not None:
                conn.execute('INSERT INTO factors(code,raw) VALUES(%s,%s) ON CONFLICT(code) DO UPDATE SET raw=EXCLUDED.raw,observed_at=now()', (part['code'], document(factor)))
            conn.execute('UPDATE jobs SET checkpoint=%s,attempts=0,updated_at=now() WHERE id=%s', (checkpoint, job_id))

    def history(self, code, period='1d', start='1990-01-01', end='2100-01-01', limit=500, offset=0, conn=None):
        code = codes([code])[0]
        period = periods([period])[0]
        limit, offset = int(limit), int(offset)
        if not 1 <= limit <= 5000 or offset < 0:
            raise ValueError('分页 limit 需要 1 至 5000，offset 不得为负')
        statement = "SELECT code,period,time,open,high,low,close,volume,amount,open_interest,settlement,previous_settlement,trading_day,source FROM bars WHERE code=%s AND period=%s AND coalesce(trading_day,time::date) BETWEEN %s::date AND %s::date ORDER BY time LIMIT %s OFFSET %s"
        args = (code, period, day(start), day(end), limit, offset)
        rows = conn.execute(statement, args).fetchall() if conn else self.query(statement, args)
        return {'rows': rows, 'offset': offset, 'limit': limit, 'next_offset': offset + len(rows) if len(rows) == limit else None,
                'source': 'postgresql', 'adjustment': 'none', 'timezone': 'Asia/Shanghai',
                'date_basis': '优先使用终端 trading_day；未提供时按行情时间的上海自然日查询，夜盘归属待核验',
                'units': {'price': 'QMT 原始报价', 'volume': 'QMT 原始成交量单位（未换算）', 'amount': 'QMT 原始成交额单位（未换算）', 'open_interest': 'QMT 原始持仓量单位（未换算）'}}
