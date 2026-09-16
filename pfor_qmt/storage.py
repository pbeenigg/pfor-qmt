import json
import re
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .data import codes, day, periods, json_default


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
        return self.query("SELECT * FROM securities WHERE (code ILIKE %s OR name ILIKE %s) AND (%s='' OR kind=%s) ORDER BY code LIMIT 1000",
                          ('%' + search + '%', '%' + search + '%', kind, kind))

    def save_security(self, code, name, kind, detail):
        self.query('INSERT INTO securities(code,name,kind,details) VALUES(%s,%s,%s,%s) ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,kind=EXCLUDED.kind,details=EXCLUDED.details,updated_at=now()',
                   (codes([code])[0], name, kind, document(detail)))

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
        if index_code:
            row = self.query('SELECT * FROM constituent_snapshots WHERE id=%s AND index_code=%s', (snapshot, index_code), one=True)
            if not row or row['members'] != members:
                raise ValueError('数据集成员必须匹配指定成分快照')
        return self.query('INSERT INTO datasets(id,name,members,periods,index_code,snapshot_id,scheduled) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                          (str(uuid.uuid4()), name, document(members), document(selected), index_code, snapshot, bool(payload.get('scheduled'))), one=True)

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
                    cursor.executemany('INSERT INTO bars(code,period,time,open,high,low,close,volume,amount) VALUES(%(code)s,%(period)s,%(time)s,%(open)s,%(high)s,%(low)s,%(close)s,%(volume)s,%(amount)s) ON CONFLICT(code,period,time) DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,volume=EXCLUDED.volume,amount=EXCLUDED.amount,updated_at=now()', rows)
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
        statement = "SELECT code,period,time,open,high,low,close,volume,amount,source FROM bars WHERE code=%s AND period=%s AND time >= %s::date AND time < %s::date + interval '1 day' ORDER BY time LIMIT %s OFFSET %s"
        args = (code, period, day(start), day(end), limit, offset)
        rows = conn.execute(statement, args).fetchall() if conn else self.query(statement, args)
        return {'rows': rows, 'offset': offset, 'limit': limit, 'next_offset': offset + len(rows) if len(rows) == limit else None,
                'source': 'postgresql', 'adjustment': 'none', 'timezone': 'Asia/Shanghai',
                'units': {'price': 'QMT 原始报价', 'volume': 'QMT 原始成交量单位（未换算）', 'amount': 'QMT 原始成交额单位（未换算）'}}
