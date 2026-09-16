"""Opt-in real QMT daily-bar acceptance. No writes unless --download is set."""
import argparse
import csv
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pfor_qmt import DataClient
from pfor_qmt.data import FIELDS, SHANGHAI, day
from pfor_qmt.settings import Settings
from pfor_qmt.storage import Store


SAMPLES = ['000300.SH', '000001.SZ', '510300.SH']


def validate_range(start, end):
    try:
        start, end = day(start), day(end)
    except (TypeError, ValueError) as error:
        raise ValueError('Use --start/--end dates in YYYY-MM-DD format') from error
    if not 0 <= (end - start).days <= 14 or end > datetime.now(SHANGHAI).date():
        raise ValueError('Use a 15-day inclusive window at most, without future dates')


def wait_job(client, identifier, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.job(identifier)
        if job['state'] not in ('queued', 'running'):
            return job
        time.sleep(1)
    raise TimeoutError('Job still pending; inspect /jobs before retrying: ' + identifier)


def canonical(rows):
    return sorted((row['code'], row['period'], datetime.fromisoformat(str(row['time'])).astimezone(SHANGHAI).isoformat(),
                   *(None if row.get(field) in (None, '') else Decimal(str(row[field])) for field in FIELDS),
                   row['source']) for row in rows)


def history_rows(client, code, start, end):
    result, offset = [], 0
    while True:
        page = client.history(code, '1d', start, end, limit=2, offset=offset)
        result.extend(page['rows'])
        if page['next_offset'] is None:
            return result
        offset = page['next_offset']


def run(client, store, folder, start, end, report):
    validate_range(start, end)
    report['stage'] = 'check_active_jobs'
    if any(job['state'] in ('queued', 'running') for job in client.request('/jobs')):
        raise ValueError('Existing jobs are active; no additional download was created')
    report['stage'] = 'create_dataset'
    name = '验收小样本日线 ' + datetime.now(SHANGHAI).strftime('%Y%m%d-%H%M%S')
    dataset = client.create_dataset(name, SAMPLES, ['1d'])
    report['dataset_id'] = dataset['id']
    report['jobs'] = []
    previous = None
    for _ in range(2):
        report['stage'] = 'download_and_readback'
        job = client.download(dataset['id'], start, end)
        report['jobs'].append({'id': job['id'], 'state': job['state']})
        job = wait_job(client, job['id'])
        report['jobs'][-1].update(state=job['state'], rows=job['result'].get('rows'))
        if job['state'] not in ('completed', 'partial'):
            raise ValueError('Download did not complete; inspect job ' + job['id'])
        rows = []
        for code in SAMPLES:
            values = history_rows(client, code, start, end)
            if not values:
                raise ValueError('No stored bars for ' + code)
            rows.extend(values)
        actual = canonical(rows)
        if previous is not None and actual != previous:
            raise ValueError('Repeated download changed values or keys; inspect terminal revisions')
        previous = actual
    report['stage'] = 'database_comparison'
    database = store.query("SELECT code,period,time,open,high,low,close,volume,amount,source FROM bars WHERE code=ANY(%s) AND period='1d' AND time >= %s::date AND time < %s::date + interval '1 day'", (SAMPLES, start, end))
    if canonical(database) != previous:
        raise ValueError('Database and paginated SDK rows differ')
    report.update(rows=len(previous), duplicate_upsert='passed', database_sdk='passed', exports={})
    for format in ('csv', 'parquet'):
        report['stage'] = format + '_export_comparison'
        job = client.export(SAMPLES, '1d', start, end, format)
        report['jobs'].append({'id': job['id'], 'state': job['state']})
        job = wait_job(client, job['id'])
        report['jobs'][-1].update(state=job['state'], rows=job['result'].get('rows'))
        if job['state'] != 'completed':
            raise ValueError('Export failed; inspect job ' + job['id'])
        target = folder / ('daily.' + format)
        client.save_export(job['id'], target)
        metadata = target.with_suffix(target.suffix + '.json')
        client.save_export(job['id'], metadata, metadata=True)
        if json.loads(metadata.read_text('utf-8'))['adjustment'] != 'none':
            raise ValueError('Unexpected export adjustment')
        if format == 'csv':
            if not target.read_bytes().startswith(b'\xef\xbb\xbf'):
                raise ValueError('CSV BOM missing')
            with target.open(encoding='utf-8-sig', newline='') as stream:
                exported = list(csv.DictReader(stream))
        else:
            import pyarrow.parquet as pq
            exported = pq.read_table(target).to_pylist()
        if canonical(exported) != previous:
            raise ValueError(format + ' differs from SDK/DB')
        report['exports'][format] = 'passed'
    report['state'] = 'passed' if all(job['state'] == 'completed' for job in report['jobs']) else 'partial'
    report['stage'] = 'finished'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.toml')
    parser.add_argument('--download', action='store_true', help='Create a 3-security dataset, two downloads and exports')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--output', default='output/live-acceptance')
    args = parser.parse_args()
    if args.download:
        try:
            validate_range(args.start, args.end)
        except ValueError as error:
            parser.error(str(error))
    client = DataClient.from_config(args.config)
    client.timeout = 90
    report = {'state': 'diagnostics_only', 'diagnostics': client.request('/source/diagnostics', {})}
    if not args.download:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    folder = Path(args.output) / datetime.now(SHANGHAI).strftime('%Y%m%d-%H%M%S-%f')
    folder.mkdir(parents=True, exist_ok=True)
    report.update(state='failed', start=args.start, end=args.end, samples=SAMPLES)
    try:
        run(client, Store(Settings(config_path=args.config).dsn), folder, args.start, args.end, report)
    except Exception as error:
        # Use job IDs for detailed diagnosis, not raw driver exceptions in reports.
        report['error_type'] = type(error).__name__
    finally:
        (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print('Report: ' + str(folder.resolve() / 'report.json'))
    return 0 if report['state'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
