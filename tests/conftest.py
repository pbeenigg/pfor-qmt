import os
import uuid
import pytest
from psycopg import sql
from pfor_qmt.storage import Store


@pytest.fixture
def store():
    dsn = os.environ.get('PFOR_QMT_TEST_DSN')
    if not dsn:
        pytest.skip('PFOR_QMT_TEST_DSN 未设置；仅连接隔离测试数据库')
    instance = Store(dsn, 'pfor_qmt_test_' + uuid.uuid4().hex)
    instance.migrate()
    try:
        yield instance
    finally:
        with instance.connect() as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(instance.schema)))
