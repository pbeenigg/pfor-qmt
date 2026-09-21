from contextlib import nullcontext
from unittest.mock import Mock

import psycopg
import pytest

from pfor_qmt.reliability import failure
from pfor_qmt.storage import Store


@pytest.mark.parametrize('error,expected,retry',[
    (psycopg.errors.ConnectionTimeout('private-password'), '超时',True),
    (psycopg.errors.DiskFull('private-password'), '存储空间不足',True),
    (psycopg.errors.UndefinedTable('private-password'), '缺少所需数据表',False),
    (psycopg.errors.UniqueViolation('private-password'), '唯一约束冲突',False),
    (psycopg.errors.InvalidPassword('private-password'), '认证失败',False),
    (psycopg.OperationalError('private-password'), '连接失败或中断',True),
])
def test_database_failure_reason_is_specific_and_redacted(error,expected,retry):
    result=failure(error)
    assert expected in result['message'] and result['retryable']==retry
    assert result['code']=='DATABASE_ERROR' and 'private-password' not in str(result)
    if not isinstance(error,psycopg.errors.DiskFull):assert '检查数据库与磁盘空间' not in result['message']


def test_retry_connection_setup_only_and_preserve_transaction_errors(monkeypatch):
    conn=Mock();connect=Mock(side_effect=[psycopg.errors.ConnectionTimeout(),psycopg.OperationalError(),conn])
    monkeypatch.setattr('pfor_qmt.storage.psycopg.connect',connect)
    monkeypatch.setattr('pfor_qmt.storage.time.sleep',lambda seconds:None)
    assert Store('test').connect() is conn
    assert connect.call_count==3 and conn.execute.call_count==2
    connect.reset_mock(side_effect=True);connect.side_effect=psycopg.errors.InvalidPassword()
    with pytest.raises(psycopg.errors.InvalidPassword):Store('test').connect()
    assert connect.call_count==1
    connect.reset_mock(side_effect=True);connect.side_effect=psycopg.errors.ConnectionTimeout()
    with pytest.raises(psycopg.errors.ConnectionTimeout):Store('test').connect()
    assert connect.call_count==3
    store=Store('test');business=Mock();business.execute.side_effect=psycopg.OperationalError()
    monkeypatch.setattr(store,'connect',lambda:nullcontext(business))
    with pytest.raises(psycopg.OperationalError):store.query('INSERT INTO jobs DEFAULT VALUES')
    assert business.execute.call_count==1
