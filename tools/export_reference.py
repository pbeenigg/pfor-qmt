"""Extract immutable protocol fixtures and selected upstream regression tests."""
import ast
import hashlib
import json
import subprocess
from pathlib import Path

COMMIT = '5baa4daf8dab01fb415afdd45a72cd254cea042f'
ROOT = Path(__file__).resolve().parents[1]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    args = parser.parse_args()
    folder = ROOT / 'tests' / 'reference'
    folder.mkdir(parents=True, exist_ok=True)
    manifest = []
    for path in ['cfquant/protocol.py', 'cfquant/tests/test_json_serialization.py']:
        raw = subprocess.check_output(['git','-C',args.source,'show',COMMIT + ':' + path])
        if path.endswith('/protocol.py'):
            target = folder / 'protocol.py'
            target.write_bytes(raw)
        else:
            text = raw.decode('utf-8')
            lines = text.splitlines(keepends=True)
            names = {'test_numpy_scalars_are_builtin_json_values_without_mutating_input', 'test_dataframe_cell_numbers_and_nested_keys',
                     'test_non_finite_numbers_fail_with_field_path', 'test_ambiguous_or_unsupported_types_are_not_stringified_or_unwrapped',
                     'test_circular_references_fail_but_shared_values_work', 'test_empty_dataframe_root_has_a_clear_type_error',
                     'test_response_missing_values_keep_existing_null_behavior'}
            parts = []
            for node in ast.parse(text).body:
                if isinstance(node, ast.FunctionDef) and node.name in names:
                    start = min([node.lineno] + [item.lineno for item in node.decorator_list])
                    parts.append(''.join(lines[start-1:node.end_lineno]))
            header = '"""Selected regressions from pinned cfquant, MIT Copyright (c) 2026 tao."""\nimport numpy as np\nimport pandas as pd\nimport pytest\nfrom pfor_qmt import protocol\nBIG_ID = 2 ** 60 + 37\n\n@pytest.fixture\ndef wire():\n    return protocol\n\n'
            target = ROOT / 'tests' / 'test_upstream_json.py'
            target.write_text(header + '\n\n'.join(parts) + '\n', encoding='utf-8')
        manifest.append({'source':path,'sha256':hashlib.sha256(raw).hexdigest(),'target':str(target.relative_to(ROOT))})
    (ROOT / 'docs' / 'test-upstream-manifest.json').write_text(json.dumps({'commit':COMMIT,'files':manifest},indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
