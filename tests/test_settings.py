import ast
import json
from pathlib import Path

import pytest
import tomlkit

from pfor_qmt.settings import Settings, FIELDS


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for _,_,_,name in FIELDS.values():
        if name:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv('PFOR_QMT_CONFIG', raising=False)
    monkeypatch.chdir(tmp_path)


def test_defaults_and_legacy_migration_only_once(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    legacy = runtime / 'settings.local.json'
    original = json.dumps({'dsn':'postgresql://legacy/db','api_key':'existing-secret','qmt_root':'D:/OldQMT','login_hash':''})
    legacy.write_text(original,encoding='utf-8')
    settings = Settings()
    assert settings.api_key == 'existing-secret'
    assert settings.dsn == 'postgresql://legacy/db'
    assert settings.path == tmp_path / 'config.toml'
    assert legacy.read_text('utf-8') == original
    settings.data['dsn'] = 'postgresql://new/db'
    settings.save()
    assert Settings().dsn == 'postgresql://new/db'
    assert Settings().api_key == 'existing-secret'


def test_cli_env_file_defaults_and_overrides_not_persisted(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('[server]\nport = 8801\n[database]\ndsn = "postgresql://file/db"\n[security]\napi_key = "file-key"\n',encoding='utf-8')
    monkeypatch.setenv('PFOR_QMT_DATABASE_URL','postgresql://env/db')
    monkeypatch.setenv('PFOR_QMT_API_KEY','env-key')
    monkeypatch.setenv('PFOR_QMT_PORT','8802')
    monkeypatch.setenv('PFOR_QMT_RUNTIME_DIR','env-runtime')
    settings = Settings(runtime='cli-runtime',port=8803)
    assert settings.value('host') == '127.0.0.1'
    assert settings.value('port') == 8803
    assert settings.value('ws_port') == 8767
    assert settings.runtime == tmp_path / 'cli-runtime'
    assert settings.dsn == 'postgresql://env/db' and settings.api_key == 'env-key'
    settings.save()
    stored = tomlkit.parse(path.read_text('utf-8'))
    assert stored['database']['dsn'] == 'postgresql://file/db'
    assert stored['security']['api_key'] == 'file-key'
    assert stored['server']['port'] == 8801
    assert stored['app']['runtime_dir'] == 'runtime'
    assert Settings().value('port') == 8802
    monkeypatch.delenv('PFOR_QMT_PORT')
    assert Settings().value('port') == 8801


def test_config_path_precedence_and_relative_paths(tmp_path, monkeypatch):
    file = tmp_path / 'nested' / 'custom.toml'
    file.parent.mkdir()
    file.write_text('[app]\nruntime_dir = "cache"\n[qmt]\nroot = "terminal"\n',encoding='utf-8')
    monkeypatch.setenv('PFOR_QMT_CONFIG',str(file))
    settings = Settings()
    assert settings.runtime == file.parent / 'cache'
    assert settings.qmt_root == str(file.parent / 'terminal')
    other = Settings(config_path=tmp_path / 'other.toml')
    assert other.path == tmp_path / 'other.toml'


def test_save_preserves_comments_and_toml_escaping(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('# keep header\n[database]\ndsn = "" # keep field comment\n',encoding='utf-8')
    settings = Settings()
    settings.data['dsn'] = 'postgresql://user:p"a\\ss@localhost/db'
    settings.data['qmt_root'] = 'D:\\国金\\行情终端'
    settings.set_password('本地网页登录密码-2026')
    settings.save()
    text = path.read_text('utf-8')
    assert '# keep header' in text and '# keep field comment' in text
    loaded = Settings()
    assert loaded.dsn == settings.dsn and loaded.qmt_root == settings.qmt_root
    assert loaded.check_password('本地网页登录密码-2026')
    public = json.dumps(loaded.public())
    assert loaded.dsn not in public and loaded.api_key not in public and loaded.data['login_hash'] not in public


@pytest.mark.parametrize('text', [
    '[server]\nport = "secret-wrong-type"', '[server]\nport = true',
    '[server]\nport = 8767', '[server]\nport = 65536',
    '[qmt]\ntimeout = nan', '[qmt]\nconnect_timeout_ms = 0',
    '[qmt]\npipe_name = "cfquant"', '[app]\nruntime_dir = ""',
    '[unknown]', '[server]\nprot = 123', '[database]\ndsn = "secret-unterminated',
])
def test_invalid_config_has_no_secret_in_error(tmp_path,text):
    path = tmp_path / 'config.toml'
    path.write_text(text,encoding='utf-8')
    with pytest.raises(ValueError) as caught:
        Settings()
    assert 'secret' not in str(caught.value)
    assert path.read_text('utf-8') == text


def test_invalid_environment_has_no_value_in_error(monkeypatch):
    monkeypatch.setenv('PFOR_QMT_PORT','secret-invalid')
    with pytest.raises(ValueError,match='PFOR_QMT_PORT') as caught:
        Settings()
    assert 'secret-invalid' not in str(caught.value)


def test_external_edits_not_overwritten(tmp_path):
    settings = Settings()
    external = settings.path.read_text('utf-8') + '\n# external edit\n'
    settings.path.write_text(external,encoding='utf-8')
    settings.data['qmt_root'] = 'D:/Other'
    with pytest.raises(ValueError,match='外部修改'):
        settings.save()
    assert settings.path.read_text('utf-8') == external


def test_sdk_uses_shared_config(monkeypatch):
    from pfor_qmt.sdk import DataClient
    from pfor_qmt import xtdata
    settings = Settings()
    settings.data.update(port=8899,pipe_name=r'\\.\pipe\pfor_qmt_custom',timeout=25.0)
    settings.save()
    client = DataClient.from_config()
    assert client.base_url == 'http://127.0.0.1:8899' and client.api_key == settings.api_key
    received = {}
    monkeypatch.setattr(xtdata,'configure',lambda **kwargs: received.update(kwargs))
    xtdata.configure_from_file()
    assert received == settings.pipe


def test_example_parses_and_contains_no_credentials(tmp_path):
    example = Path(__file__).resolve().parents[1] / 'config.example.toml'
    (tmp_path / 'config.toml').write_bytes(example.read_bytes())
    settings = Settings()
    assert not settings.dsn and not settings.qmt_root and settings.api_key


def test_path_runtime_is_supported(tmp_path):
    settings = Settings(runtime=tmp_path / 'local-runtime')
    assert settings.runtime == tmp_path / 'local-runtime'


def test_cli_key_uses_selected_config(tmp_path,monkeypatch,capsys):
    import sys
    from pfor_qmt.cli import main
    settings = Settings(config_path=tmp_path / 'custom.toml')
    monkeypatch.setattr(sys,'argv',['pfor-qmt','--config',str(settings.path),'key'])
    main()
    assert capsys.readouterr().out.strip() == settings.api_key
    assert not (tmp_path / 'config.toml').exists()
