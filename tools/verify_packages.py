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
    required = ['pfor_qmt/diagnostics.py','pfor_qmt/catalog.py','pfor_qmt/symbols.py','pfor_qmt/migrations/001_initial.sql','pfor_qmt/migrations/002_catalog.sql','pfor_qmt/migrations/003_asset_classes.sql','qmt_scripts/PFOR_MARKET.py','web_dashboard/index.html','web_dashboard/picker.js',
                'web_dashboard/vendor/echarts.min.js','web_dashboard/vendor/lucide.min.js',
                'web_dashboard/vendor/ECHARTS-LICENSE.txt','web_dashboard/vendor/ECHARTS-NOTICE.txt',
                'web_dashboard/vendor/LUCIDE-LICENSE.txt','THIRD_PARTY_NOTICES.md','LICENSE']
    required.extend(['pfor_qmt/accounts.py','pfor_qmt/identifiers.py','pfor_qmt/tushare.py','pfor_qmt/migrations/004_sources.sql'])
    required.extend(['pfor_qmt/futures.py','pfor_qmt/migrations/005_futures_reports.sql','web_dashboard/controls.js'])
    required.extend(['pfor_qmt/reliability.py','pfor_qmt/maintenance.py','pfor_qmt/migrations/006_reliability.sql','web_dashboard/operations.js'])
    required.append('pfor_qmt/freshness.py')
    required.extend(['pfor_qmt/quality_checks.py','pfor_qmt/migrations/007_verification.sql'])
    required.append('pfor_qmt/migrations/008_settlement_weekly.sql')
    if path.name.endswith('.tar.gz'):
        required.append('docs/SETTLEMENT_WEEKLY.md')
    if path.name.endswith('.tar.gz'):
        required.append('docs/QUALITY_VERIFICATION.md')
        required.append('docs/MAINTENANCE.md')
    if path.name.endswith('.tar.gz'):
        required.extend(['tools/live_acceptance.py','tools/verify_backup.py','docs/FRESHNESS.md','docs/LIVE_ACCEPTANCE.md','docs/MULTI_ASSET_PLAN.md','pfor.ps1'])
    for suffix in required:
        assert any(name.endswith(suffix) for name in names), (path.name,suffix)
    for name in names:
        assert not any(part in {'runtime','log','exports','output','.venv','__pycache__','%NVM_SYMLINK%'} for part in Path(name).parts), name
        assert not name.endswith('.local.json'), name
        assert Path(name).name != 'config.toml' and not name.endswith('.local.toml'), name
    print(path.name + ': required resources and license files present; no runtime data')
