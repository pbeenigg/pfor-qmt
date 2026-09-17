"""Inspect built artifacts for required assets, attribution and excluded local data."""
from pathlib import Path
import tarfile
import zipfile

root = Path(__file__).resolve().parents[1]
artifacts = sorted((root / 'dist').glob('*'))
assert artifacts, 'Run python -m build first'
for path in artifacts:
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    elif path.name.endswith('.tar.gz'):
        with tarfile.open(path) as archive:
            names = archive.getnames()
    else:
        continue
    required = ['pfor_qmt/diagnostics.py','pfor_qmt/migrations/001_initial.sql','qmt_scripts/PFOR_MARKET.py','web_dashboard/index.html',
                'web_dashboard/vendor/echarts.min.js','web_dashboard/vendor/lucide.min.js',
                'web_dashboard/vendor/ECHARTS-LICENSE.txt','web_dashboard/vendor/ECHARTS-NOTICE.txt',
                'web_dashboard/vendor/LUCIDE-LICENSE.txt','THIRD_PARTY_NOTICES.md','LICENSE']
    if path.name.endswith('.tar.gz'):
        required.extend(['tools/live_acceptance.py','docs/LIVE_ACCEPTANCE.md','pfor.ps1'])
    for suffix in required:
        assert any(name.endswith(suffix) for name in names), (path.name,suffix)
    for name in names:
        assert not any(part in {'runtime','log','exports','output','.venv','__pycache__','%NVM_SYMLINK%'} for part in Path(name).parts), name
        assert not name.endswith('.local.json'), name
        assert Path(name).name != 'config.toml' and not name.endswith('.local.toml'), name
    print(path.name + ': required resources and license files present; no runtime data')
