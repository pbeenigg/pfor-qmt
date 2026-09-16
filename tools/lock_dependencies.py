"""Record exact tested dependencies and vendored asset hashes."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
locked = subprocess.check_output([sys.executable,'-m','pip','freeze','--exclude-editable'],text=True)
(root / 'requirements.lock').write_text('# Python 3.12; exact runtime and development dependency versions.\n' + locked,encoding='utf-8',newline='\n')
assets = []
for path in sorted((root / 'web_dashboard' / 'vendor').iterdir()):
    assets.append({'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
(root / 'docs' / 'assets-manifest.json').write_text(json.dumps({'echarts':'5.6.0','zrender':'5.6.1','lucide':'0.468.0','files':assets},indent=2),encoding='utf-8')
