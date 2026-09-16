import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path


class Settings:
    def __init__(self, runtime=None):
        self.runtime = Path(runtime or os.environ.get('PFOR_QMT_RUNTIME_DIR', 'runtime')).resolve()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.path = self.runtime / 'settings.local.json'
        self.data = json.loads(self.path.read_text('utf-8')) if self.path.exists() else {}
        self.data.setdefault('api_key', secrets.token_urlsafe(32))
        self.data.setdefault('dsn', '')
        self.data.setdefault('qmt_root', '')
        self.data.setdefault('login_hash', '')
        self.save()

    @property
    def dsn(self):
        return os.environ.get('PFOR_QMT_DATABASE_URL') or self.data['dsn']

    def save(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    def set_password(self, password):
        if password and len(password) < 12:
            raise ValueError('网页登录密码至少 12 位')
        salt = secrets.token_hex(16)
        self.data['login_hash'] = salt + ':' + hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex() if password else ''

    def check_password(self, password):
        if not self.data['login_hash']:
            return False
        salt, expected = self.data['login_hash'].split(':')
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)

    def public(self):
        return {'database_configured': bool(self.dsn), 'qmt_root': self.data['qmt_root'],
                'login_enabled': bool(self.data['login_hash']), 'schema': 'pfor_qmt',
                'timezone': 'Asia/Shanghai', 'adjustment': 'none'}
