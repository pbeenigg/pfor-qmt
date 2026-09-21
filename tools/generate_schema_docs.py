"""Generate schema documentation from PostgreSQL metadata; never read business rows."""
import argparse
import re
from pathlib import Path

from pfor_qmt.storage import Store


GROUPS = {
    '目录、身份与行情': ('instruments','securities','bars','factors','catalog_sectors','board_snapshots','index_mapping','constituent_snapshots','datasets'),
    '任务、维护与当前映射': ('jobs','job_units','job_events','coverage','maintenance_plans','contract_mapping_snapshots','current_contract_mappings','schema_version'),
    '交易日历与期货资料': ('trading_dates','contract_mappings','futures_warehouse_receipts','futures_holdings','futures_settlements','futures_weekly_details'),
}


def read_schema(store):
    with store.connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        version=conn.execute('SELECT max(version) AS version FROM schema_version').fetchone()['version']
        relations=list(conn.execute("""SELECT c.relname AS name,c.relkind AS kind,obj_description(c.oid,'pg_class') AS comment
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relkind IN ('r','p','v') ORDER BY c.relname""",(store.schema,)))
        for table in relations:
            table['columns']=list(conn.execute("""SELECT a.attname AS name,format_type(a.atttypid,a.atttypmod) AS type,
                a.attnotnull AS required,pg_get_expr(d.adbin,d.adrelid) AS default_value,col_description(c.oid,a.attnum) AS comment
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
                LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
                WHERE n.nspname=%s AND c.relname=%s ORDER BY a.attnum""",(store.schema,table['name'])))
            table['constraints']=list(conn.execute("""SELECT con.conname AS name,con.contype AS type,
                pg_get_constraintdef(con.oid) AS definition,
                ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(num,pos)
                    JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.num ORDER BY k.pos) AS columns,
                ref.relname AS parent,
                ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(num,pos)
                    JOIN pg_attribute a ON a.attrelid=con.confrelid AND a.attnum=k.num ORDER BY k.pos) AS parent_columns
                FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_class ref ON ref.oid=con.confrelid
                WHERE n.nspname=%s AND c.relname=%s ORDER BY con.conname""",(store.schema,table['name'])))
            table['indexes']=list(conn.execute('SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s ORDER BY indexname',(store.schema,table['name'])))
            if table['kind']=='v':
                table['view']=conn.execute('SELECT pg_get_viewdef((%s||%s)::regclass,true) AS definition',(store.schema+'.',table['name'])).fetchone()['definition']
    return {'version':version,'relations':relations}


def cell(value):
    return str(value if value is not None else '无').replace('|','&#124;').replace('\n','<br>')


def render(schema, schema_name='pfor_qmt'):
    tables={row['name']:row for row in schema['relations']}
    expected={name for names in GROUPS.values() for name in names}
    if set(tables)!=expected: raise ValueError('表结构已变化，请更新ER图分组：'+str(sorted(set(tables)^expected)))
    missing=[name for name,t in tables.items() if not t['comment']]
    missing += [name+'.'+c['name'] for name,t in tables.items() for c in t['columns'] if not c['comment']]
    if missing: raise ValueError('缺少数据库注释：'+', '.join(missing))
    lines=['# 数据库结构、ER图与字段字典','',
           f"适用迁移版本：{schema['version']}。覆盖 {sum(t['kind']!='v' for t in tables.values())} 张表、{sum(t['kind']=='v' for t in tables.values())} 个视图、{sum(len(t['columns']) for t in tables.values())} 个字段。",'',
           '本文由 `tools/generate_schema_docs.py` 读取PostgreSQL系统目录生成，不读取业务行、账号Token或连接密码。SQL迁移中的COMMENT是说明来源；更改结构或口径后应更新迁移并重新生成。','',
           '```powershell','.venv\\Scripts\\python.exe -X utf8 -m pfor_qmt.cli migrate',
           '.venv\\Scripts\\python.exe -X utf8 tools/generate_schema_docs.py',
           '.venv\\Scripts\\python.exe -X utf8 tools/generate_schema_docs.py --check','```','',
           '## 阅读约定','',
           '- 物理ER图仅绘制真实外键，实体内列出主键、外键和单列唯一键；复合键按各组成字段标PK。全部字段见下方字典。',
           '- `||`表示恰好一个，`o|`表示零或一个，`o{`表示零或多个；非标识关系使用虚线，仍是实际外键。视图没有物理主键和外键。',
           '- JSON数组、代码匹配和任务payload引用是业务逻辑关系，不具备数据库外键约束，单独在逻辑关系图说明。',
           '- NULL保留“未提供/未确认”，不视为零；金额、成交量与价格口径按来源及转换版本解释。',
           '- 日历完整性、数据新鲜度和任务执行成功是不同概念；当前映射快照不能代替历史逐日映射。','',
           '## 物理ER图','']
    for title,names in GROUPS.items():
        lines += ['### '+title,'','```mermaid','erDiagram']
        for name in names:
            table=tables[name];keys={c['name']:[] for c in table['columns']}
            for constraint in table['constraints']:
                marker={'p':'PK','f':'FK','u':'UK'}.get(constraint['type'])
                if marker:
                    for column in constraint['columns']:keys[column].append(marker)
            lines.append('    '+name+' {')
            selected=[c for c in table['columns'] if keys[c['name']]] or table['columns'][:2]
            for column in selected:
                typ={'timestamp with time zone':'timestamptz','time without time zone':'time'}.get(column['type'],column['type'])
                typ=re.sub(r'[^a-zA-Z0-9_]','_',typ)
                markers=','.join(dict.fromkeys(keys[column['name']]))
                lines.append('        '+typ+' '+column['name']+(' '+markers if markers else ''))
            lines.append('    }')
        for child in names:
            table=tables[child];required={c['name']:c['required'] for c in table['columns']}
            primary=next((set(k['columns']) for k in table['constraints'] if k['type']=='p'),set())
            for key in table['constraints']:
                if key['type']!='f' or key['parent'] not in names: continue
                left='||' if all(required[c] for c in key['columns']) else 'o|'
                line='--' if set(key['columns'])<=primary else '..'
                lines.append('    '+key['parent']+' '+left+line+'o{ '+child+' : "'+','.join(key['columns'])+'"')
        lines += ['```','']
    lines += ['## 逻辑关系（非外键）','','以下仅表示代码中的引用或查询关系，不表示数据库强制约束，也不声明完整的关系基数。','','```mermaid','flowchart LR',
              '    index_mapping -. "index_code" .-> constituent_snapshots',
              '    catalog_sectors -. "name" .-> board_snapshots',
              '    securities -. "QMT code" .-> factors',
              '    securities -. "source + code / member_code" .-> contract_mappings',
              '    securities -. "source + code / member_code" .-> contract_mapping_snapshots',
              '    datasets -. "payload.dataset_id" .-> jobs',
              '    maintenance_plans -. "payload.maintenance_id / manual_maintenance_id" .-> jobs',
              '    jobs -. "payload.verification_of / repair_of" .-> jobs',
              '    job_units -. "job_id + unit_index" .-> contract_mapping_snapshots',
              '    job_units -. "job_id + unit_index" .-> job_events',
              '    contract_mapping_snapshots -. "每来源每代码的最新采集" .-> current_contract_mappings',
              '    trading_dates -. "同来源市场日期证据" .-> bars',
              '    trading_dates -. "同来源交易日完整性核验" .-> contract_mappings','```','',
              '成员数组、资料品种与上游原始字段继续保存在JSON或来源标识中；未额外建账号表，Token只保存在本地配置。仓单、排名、结算、周报没有到证券目录的物理外键。','',
              '## 表与字段字典','']
    def normalize(value):return value.replace(schema_name+'.','pfor_qmt.')
    for title,names in GROUPS.items():
        lines+=['### '+title,'']
        for name in names:
            table=tables[name]
            lines+=['#### '+name+('（视图）' if table['kind']=='v' else ''),'',table['comment'],'',
                    '| 字段 | PostgreSQL类型 | 可空 | 默认值 | 注释 |','| --- | --- | --- | --- | --- |']
            for c in table['columns']:
                null='视图派生' if table['kind']=='v' else '否' if c['required'] else '是'
                lines.append('| '+ ' | '.join((c['name'],c['type'],null,cell(normalize(c['default_value'])) if c['default_value'] else '无',cell(c['comment'])))+' |')
            lines.append('')
            for key in table['constraints']:lines+=['- `'+key['name']+'`：`'+normalize(key['definition'])+'`']
            if table['constraints']:lines.append('')
            if table['indexes']:
                lines+=['<details><summary>索引定义</summary>','','```sql']
                lines.extend(normalize(index['indexdef'])+';' for index in table['indexes'])
                lines+=['```','','</details>','']
            if table.get('view'):lines+=['```sql',normalize(table['view']).rstrip(),'```','']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'docs'/'DATABASE_SCHEMA.md')
    parser.add_argument('--check',action='store_true',help='Check the committed document without rewriting it')
    args=parser.parse_args()
    from pfor_qmt.settings import Settings
    store=Store(Settings(config_path=args.config).dsn)
    text=render(read_schema(store),store.schema)
    if args.check:
        if not args.output.exists() or args.output.read_text('utf-8')!=text:raise SystemExit('Schema documentation is stale; regenerate it.')
    else:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(text,encoding='utf-8',newline='\n')
    print('Schema documentation matches PostgreSQL metadata: '+str(args.output))


if __name__=='__main__':main()
