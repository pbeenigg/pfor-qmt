"""Restore a pfor-qmt archive in a disposable, network-isolated PostgreSQL container."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

from psycopg import sql


def verify(backup, manifest, output):
    backup = Path(backup).resolve(strict=True)
    expected = json.loads(Path(manifest).read_text('utf-8'))
    tables = {key:value for key,value in expected.items() if not key.startswith('_')}
    if not tables or any(not isinstance(row.get('columns'),list) or not row['columns'] for row in tables.values()):
        raise ValueError('Backup manifest needs table counts and original columns')
    name = 'pfor-qmt-restore-' + uuid.uuid4().hex[:12]
    created = False
    report = {'backup':backup.name,'container':name,'restored':False,'migration_preserved':False}

    def run(args, **kwargs):
        result = subprocess.run(['docker',*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180,**kwargs)
        if result.returncode:
            raise RuntimeError('Docker '+args[0]+' failed: '+result.stderr.decode('utf-8',errors='replace')[-1200:])
        return result.stdout

    def query(statement):
        return run(['exec','-i',name,'psql','-X','-qAt','-v','ON_ERROR_STOP=1','-U','postgres','-d','pfor_restore'],input=statement.encode('utf-8')).decode('utf-8').strip()

    def signature(table, columns):
        statement = sql.SQL("SET TIME ZONE 'Asia/Shanghai'; SELECT json_build_object('count',count(*),'hash',md5(coalesce(string_agg(md5(row_to_json(t)::text),'' ORDER BY md5(row_to_json(t)::text)),''))) FROM (SELECT {} FROM pfor_qmt.{}) t").format(sql.SQL(',').join(map(sql.Identifier,columns)),sql.Identifier(table)).as_string()
        return json.loads(query(statement))

    try:
        with backup.open('rb') as stream:
            report['sha256'] = hashlib.file_digest(stream,'sha256').hexdigest()
        run(['run','--detach','--name',name,'--label','pfor-qmt.verification=backup','--network','none',
             '--tmpfs','/var/lib/postgresql/data:rw,size=2147483648','-e','POSTGRES_HOST_AUTH_METHOD=trust',
             '-e','POSTGRES_DB=pfor_restore','postgres:16-alpine'])
        created = True
        for _ in range(40):
            ready = subprocess.run(['docker','exec',name,'pg_isready','-U','postgres','-d','pfor_restore'],capture_output=True,timeout=10)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Isolated PostgreSQL did not become ready')
        with backup.open('rb') as stream:
            run(['exec','-i',name,'pg_restore','--exit-on-error','--no-owner','--no-privileges','-U','postgres','-d','pfor_restore'],stdin=stream)
        before = {table:signature(table,row['columns']) for table,row in tables.items()}
        assert all(before[table]['count']==row['count'] for table,row in tables.items()), 'Restored counts differ from backup manifest'
        states=json.loads(query("SELECT coalesce(json_agg(t ORDER BY state),'[]') FROM (SELECT state,count(*) FROM pfor_qmt.jobs GROUP BY state) t"))
        assert states==expected['_states'], 'Restored task states differ'
        report.update(restored=True,restored_version=int(query('SELECT max(version) FROM pfor_qmt.schema_version')),tables=before)
        migrations=Path(__file__).resolve().parents[1]/'pfor_qmt'/'migrations'
        for migration in sorted(migrations.glob('*.sql')):
            version=int(migration.stem.split('_')[0])
            if version>report['restored_version']:
                query("BEGIN; SET LOCAL search_path TO pfor_qmt,pg_catalog; "+migration.read_text('utf-8')+f'; INSERT INTO schema_version(version) VALUES({version}) ON CONFLICT DO NOTHING; COMMIT;')
        after = {table:signature(table,row['columns']) for table,row in tables.items()}
        assert before==after, 'Restored original columns changed during migration'
        new_states=json.loads(query("SELECT coalesce(json_agg(t ORDER BY state),'[]') FROM (SELECT state,count(*) FROM pfor_qmt.jobs GROUP BY state) t"))
        assert {row['state']:row['count'] for row in new_states}=={('succeeded' if row['state']=='completed' else row['state']):row['count'] for row in states}
        report.update(migration_preserved=True,migrated_version=int(query('SELECT max(version) FROM pfor_qmt.schema_version')))
        print('Backup restored; '+str(len(tables))+' original tables preserved through migration.')
    finally:
        if created:
            run(['rm','--force',name])
            report['temporary_container_removed'] = True
        Path(output).parent.mkdir(parents=True,exist_ok=True)
        Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup')
    parser.add_argument('manifest')
    parser.add_argument('--output',default='output/backup-verification.json')
    args=parser.parse_args()
    verify(args.backup,args.manifest,args.output)
