from pathlib import Path

from psycopg import sql

from tools.generate_schema_docs import read_schema,render


def test_comments_cover_every_field_and_do_not_change_structure_or_rows(store):
    root=Path(__file__).resolve().parents[1]
    migration=root/'pfor_qmt'/'migrations'/'013_simple_schema_comments.sql'
    table=store.create_dataset({'name':'注释迁移验证','members':['000001.SZ']})
    before=read_schema(store)
    statements=migration.read_text('utf-8')
    with store.connect() as conn:
        for relation in before['relations']:
            kind='VIEW' if relation['kind']=='v' else 'TABLE'
            conn.execute(sql.SQL('COMMENT ON '+kind+' {} IS NULL').format(sql.Identifier(relation['name'])))
            for column in relation['columns']:
                conn.execute(sql.SQL('COMMENT ON COLUMN {}.{} IS NULL').format(sql.Identifier(relation['name']),sql.Identifier(column['name'])))
        conn.execute('DELETE FROM schema_version WHERE version=13')
    store.migrate()
    after=read_schema(store)
    assert after['version']==13
    assert after==before
    assert all(row['comment'] and all(c['comment'] for c in row['columns']) for row in after['relations'])
    assert store.query('SELECT * FROM datasets WHERE id=%s',(table['id'],),one=True)==table
    with store.connect() as conn:conn.execute(statements)
    store.migrate()
    assert read_schema(store)==after
    generated=render(after,store.schema)
    assert generated.count('```mermaid')==4
    assert 'jobs o|..o{ jobs' in generated
    assert 'jobs ||--o{ job_units' in generated
    assert '逻辑关系（非外键）' in generated
    committed=root/'docs'/'DATABASE_SCHEMA.md'
    if committed.exists(): assert committed.read_text('utf-8')==generated
