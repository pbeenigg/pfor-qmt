import ast
import json
from pathlib import Path
from xml.dom import minidom

import pytest
from pfor_qmt.deploy import prepare, activate, EMBEDDED


@pytest.fixture
def terminal(tmp_path):
    (tmp_path / 'bin.x64').mkdir()
    (tmp_path / 'bin.x64' / 'XtItClient.exe').touch()
    (tmp_path / 'config').mkdir()
    (tmp_path / 'python').mkdir()
    (tmp_path / 'config' / 'indexUserConfig.xml').write_text('<ICUserConfigFile><strategyTrade><item id="5" name="CFQ_EXISTING" account="offline-test" accountType="2" m_strAccountKey="offline-test-key" startupAutorun="1" custom="keep"/></strategyTrade><Other value="keep"/></ICUserConfigFile>','utf-8')
    return tmp_path


def test_prepare_activate_preserves_foreign_models(terminal):
    config = terminal / 'config' / 'indexUserConfig.xml'
    original = config.read_bytes()
    assert prepare(terminal,process_checker=lambda root:False)['state'] == 'waiting_import'
    assert config.read_bytes() == original
    for name in EMBEDDED:
        ast.parse((terminal / 'pfor_qmt_managed' / 'embedded' / 'pfor_qmt' / name).read_text('utf-8'),feature_version=(3,6))
    (terminal / 'python' / 'PFOR_MARKET.py').write_text('# imported test model')
    activate(terminal,process_checker=lambda root:False)
    doc = minidom.parseString(config.read_bytes())
    old = minidom.parseString(original).getElementsByTagName('item')[0]
    new = next(node for node in doc.getElementsByTagName('item') if node.getAttribute('name') == 'CFQ_EXISTING')
    assert old.toxml() == new.toxml()
    activate(terminal,process_checker=lambda root:False)
    assert len(minidom.parseString(config.read_bytes()).getElementsByTagName('item')) == 2
    assert list((terminal / 'pfor_qmt_managed' / 'backups').glob('*.xml'))


def test_refuses_running_terminal_and_unowned_collision(terminal):
    with pytest.raises(ValueError,match='退出'):
        prepare(terminal,process_checker=lambda root:True)
    (terminal / 'python' / 'PFOR_MARKET.py').write_text('user-owned')
    with pytest.raises(ValueError,match='未托管'):
        prepare(terminal,process_checker=lambda root:False)
    assert (terminal / 'python' / 'PFOR_MARKET.py').read_text() == 'user-owned'


def test_concurrent_config_change_aborts_write(terminal):
    prepare(terminal,process_checker=lambda root:False)
    (terminal / 'python' / 'PFOR_MARKET.py').touch()
    config = terminal / 'config' / 'indexUserConfig.xml'
    calls = []
    def check(root):
        calls.append(root)
        if len(calls) == 2:
            config.write_bytes(config.read_bytes() + b'\n')
        return False
    with pytest.raises(ValueError,match='并发修改'):
        activate(terminal,process_checker=check)
    assert b'PFOR_MARKET' not in config.read_bytes()


def test_deployment_only_embeds_market_settings(terminal):
    from pfor_qmt.config import get_config
    (terminal / 'pfor_qmt_managed').mkdir()
    (terminal / 'pfor_qmt_managed' / 'ownership.json').write_text('{"model":"PFOR_MARKET"}')
    (terminal / 'python' / 'PFOR_MARKET.py').write_text('# imported')
    pipe = dict(get_config(),pipe_name=r'\\.\pipe\pfor_qmt_custom',timeout=25.0)
    prepare(terminal,process_checker=lambda root:False,pipe_config=pipe)
    script = (terminal / 'python' / 'PFOR_MARKET.py').read_text('gbk')
    tree = ast.parse(script,feature_version=(3,6))
    assignment = next(node for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id == 'PFOR_PIPE' for target in node.targets))
    assert ast.literal_eval(assignment.value) == pipe
    assert 'dsn' not in script and 'api_key' not in script
